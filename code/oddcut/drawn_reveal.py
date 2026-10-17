"""Drawn reveal on a single shot: the subject is sketched on paper, coloured in, then dissolves into the
real footage.

    line    hand-drawn strokes trace the subject: outer contour (mask edge) first, then inner lines
            (Canny inside the mask) and, for animals, eyes / nose / mouth placed from YOLOE part boxes.
            Pen progress follows an ease-out curve; strokes boil (redrawn wobble) every 3 frames.
    color   flat posterized fills (k-means on the subject, 3-4 colours) wiped in region by region with a
            zig-zag marker front and a crayon texture (streaks + paper tooth), under the lines
    reveal  the real frame dissolves in: subject first, then background, lines fade last. The real frame
            is frozen on the start frame until the reveal ends, then playback resumes.

    python -m oddcut.drawn_reveal --clip assets/public/clips/22732-720.mp4 --start 0.25 --out output/drawn/22732_cat.mp4

is_drawable(frames) decides whether a shot suits the effect: one subject, 15-60% of the frame, sharp and
steady at the start, a frontal face for animals, fully in frame for objects.
"""
import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .sticker_stage import _center_crop_box
from .stickers import (ANIMALS, FRONT_OBJECTS, HEAD_MODEL, OBJECT_MODEL, _bezier, _in_model_dir,
                       _smooth_noise, contact_sheet, read_clip, write_clip)

ASPECT = 3 / 4             # default crop; the pipeline passes its output aspect
OUT_HEIGHT = 1440          # draw at the pipeline's output size; the real frame is upscaled to match
SS = 2                     # supersampling for the line layer
PAPER = np.array([243, 237, 225], np.float32)
INK = (42, 34, 31)

# is_drawable thresholds
MIN_AREA, MAX_AREA = 0.15, 0.60
MIN_CONF = 0.5             # below this the class (and mask) is a guess: a glass dripper read as "cup" 0.41
DOMINANCE = 3.0            # largest subject must be >= 3x the next one
MIN_EDGE_SHARP = 40.0      # Laplacian variance on the subject's silhouette band (native 3:4 crop)
MIN_STABLE_IOU = 0.85      # subject mask IoU between the start frame and +0.3s
MAX_BORDER = 0.10          # objects: share of the outline allowed on the frame border (808 cup rim: 6%)
BUST_BELOW_HEAD = 0.2      # animals: draw the head + this much (x head height) of chest
BUST_SIDE = 0.06           # ... and at most this much (x head width) beside the head box


@dataclass
class Timing:
    line: float = 0.6
    color: float = 0.5
    reveal: float = 0.4
    tail: float = 0.6           # normal playback after the reveal
    reveal_at: float | None = None   # seconds from clip start at which the real subject starts showing
    hold: float = 0.0

    def resolve(self) -> "Timing":
        t = Timing(self.line, self.color, self.reveal, self.tail, None, 0.0)
        if self.reveal_at is not None:
            base = self.line + self.color
            if self.reveal_at >= base:
                t.hold = self.reveal_at - base       # finished cartoon waits for the beat
            else:
                k = self.reveal_at / base            # draw faster to hit an early beat
                t.line, t.color = self.line * k, self.color * k
        return t


@dataclass
class Subject:
    label: str
    family: str                 # animal | object
    conf: float
    box: np.ndarray             # x1 y1 x2 y2 (native crop px)
    mask: np.ndarray            # full subject mask (bool), see-through holes removed
    region: np.ndarray          # what gets drawn: mask, or head + chest bust for animals
    det_mask: np.ndarray | None = None   # cleaned detector mask (stability check compares these)
    window: np.ndarray | None = None   # bust window for animals: no outline along its edge, colour fades there
    head: np.ndarray | None = None
    eyes: list = field(default_factory=list)
    nose: np.ndarray | None = None
    others: list = field(default_factory=list)   # other subject-class detections (label, area)


# ----------------------------------------------------------------------------- models

_MODELS: dict = {}


def _yolo():
    if "seg" not in _MODELS:
        from ultralytics import YOLO
        with _in_model_dir():
            _MODELS["seg"] = YOLO(OBJECT_MODEL)
    return _MODELS["seg"]


def _yoloe(prompts: list[str]):
    """YOLOE with these classes. Per-prompt text embeddings are cached in assets/models/ (same files as
    stickers.detect_heads), so the 600 MB text encoder only loads for a prompt never seen before."""
    import torch
    from ultralytics import YOLOE
    key = ("yoloe",) + tuple(prompts)
    if key not in _MODELS:
        with _in_model_dir():
            model = YOLOE(HEAD_MODEL)
            pes = []
            for p in prompts:
                cache = Path(f"yoloe_pe_{p.replace(' ', '_')}.pt")
                if cache.exists():
                    pe = torch.load(cache)
                else:
                    pe = model.get_text_pe([p])
                    torch.save(pe, cache)
                pes.append(pe)
            model.set_classes(prompts, torch.cat(pes, 1))
        _MODELS[key] = model
    return _MODELS[key]


def _segment(frame: np.ndarray, conf: float = 0.3):
    """[(label, conf, box, mask)] for subject classes, largest mask first."""
    model = _yolo()
    wanted = set(ANIMALS) | set(FRONT_OBJECTS)
    ids = [i for i, n in model.names.items() if n in wanted]
    r = model.predict(np.ascontiguousarray(frame[..., ::-1]), classes=ids, conf=conf, imgsz=640,
                      retina_masks=True, agnostic_nms=True, verbose=False)[0]
    if r.masks is None:
        return []
    dets = []
    for b, c, k, m in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy(),
                          r.boxes.cls.cpu().numpy(), r.masks.data.cpu().numpy()):
        dets.append((model.names[int(k)], float(c), b, m > 0.5))
    # one object seen as two classes (cup + bowl, dog + bear) is still one subject
    out = []
    for d in sorted(dets, key=lambda d: -d[3].sum()):
        if all((d[3] & o[3]).sum() < 0.5 * max(1, d[3].sum()) for o in out):
            out.append(d)
    return out


def _parts(frame: np.ndarray, label: str, body_box: np.ndarray):
    """Head box, eye pair and nose for an animal (None / [] when not found frontally)."""
    prompts = [f"{label} head", f"{label} eye", f"{label} nose"]
    r = _yoloe(prompts).predict(np.ascontiguousarray(frame[..., ::-1]), conf=0.05, imgsz=640,
                                verbose=False)[0]
    dets = [(prompts[int(k)].split()[-1], float(c), b) for b, c, k in
            zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy(), r.boxes.cls.cpu().numpy())]
    bx = body_box
    m = 0.05 * (bx[2] - bx[0])
    heads = [(c, b) for n, c, b in dets if n == "head" and c >= 0.25
             and b[0] > bx[0] - m and b[2] < bx[2] + m and b[1] > bx[1] - m
             and (b[2] - b[0]) * (b[3] - b[1]) < 0.8 * (bx[2] - bx[0]) * (bx[3] - bx[1])]
    if not heads:
        return None, [], None
    head = max(heads, key=lambda h: h[0])[1]
    hw = head[2] - head[0]

    def inside(b):
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return head[0] <= cx <= head[2] and head[1] <= cy <= head[3]

    eyes = [(c, b) for n, c, b in dets if n == "eye" and c >= 0.15 and inside(b) and b[2] - b[0] < 0.35 * hw]
    best, pair = 0.0, []
    for i in range(len(eyes)):
        for j in range(i + 1, len(eyes)):
            (ci, bi), (cj, bj) = eyes[i], eyes[j]
            pi, pj = (bi[:2] + bi[2:]) / 2, (bj[:2] + bj[2:]) / 2
            dx, dy = abs(pi[0] - pj[0]), abs(pi[1] - pj[1])
            wi, wj = bi[2] - bi[0], bj[2] - bj[0]
            if 0.15 * hw < dx < 0.75 * hw and dy < 0.3 * dx and max(wi, wj) < 2 * min(wi, wj) \
                    and ci + cj > best:
                best, pair = ci + cj, sorted([bi, bj], key=lambda b: b[0])
    nose = None
    if pair:
        ex0, ex1 = (pair[0][0] + pair[0][2]) / 2, (pair[1][0] + pair[1][2]) / 2
        ey = (pair[0][3] + pair[1][3]) / 2
        noses = [(c, b) for n, c, b in dets if n == "nose" and c >= 0.15 and inside(b)
                 and ex0 < (b[0] + b[2]) / 2 < ex1 and (b[1] + b[3]) / 2 > ey and b[2] - b[0] < 0.4 * hw]
        if noses:
            nose = max(noses, key=lambda d: d[0])[1]
    return head, pair, nose


def _clean_mask(mask: np.ndarray) -> np.ndarray:
    """Largest component, small gaps closed, small holes filled (big holes like a cup handle kept)."""
    m = mask.astype(np.uint8)
    k = max(3, int(0.012 * max(m.shape)) | 1)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n <= 1:
        return m > 0
    m = (lab == 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])).astype(np.uint8)
    inv = 1 - m
    n, lab, stats, _ = cv2.connectedComponentsWithStats(inv, 4)
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        touches = x == 0 or y == 0 or x + w == m.shape[1] or y + h == m.shape[0]
        if not touches and a < 0.01 * m.sum():
            m[lab == i] = 1
    return m > 0


def _carve_holes(frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Cut see-through holes (background inside a cup handle) out of a detector mask: YOLO masks are
    blobby and fill them. A hole = an enclosed blob of mask pixels whose colour is nearer the background
    just outside the mask than the subject's core, with a crisp edge all round (a lit band on a curved
    surface can match the background colour too, but its edge is a soft shading gradient)."""
    m = mask.astype(np.uint8)
    size = math.sqrt(float(m.sum()))
    lab = cv2.cvtColor(cv2.medianBlur(frame, 5), cv2.COLOR_RGB2LAB).astype(np.float32)
    ring = (cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(0.15 * size) | 1,) * 2)) > 0) & ~mask
    core = cv2.erode(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(0.12 * size) | 1,) * 2)) > 0
    if ring.sum() < 200 or core.sum() < 200:
        return mask
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    cv2.setRNGSeed(0)
    _, _, bg_c = cv2.kmeans(lab[ring][::4], 3, None, crit, 2, cv2.KMEANS_PP_CENTERS)
    _, _, fg_c = cv2.kmeans(lab[core][::4], 3, None, crit, 2, cv2.KMEANS_PP_CENTERS)
    px = lab[mask]
    d_bg = np.min(np.linalg.norm(px[:, None] - bg_c[None], axis=2), 1)
    d_fg = np.min(np.linalg.norm(px[:, None] - fg_c[None], axis=2), 1)
    cand = np.zeros(mask.shape, np.uint8)
    cand[mask] = d_bg < 0.75 * d_fg
    cand = cv2.morphologyEx(cand, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    g = cv2.GaussianBlur(lab[..., 0], (0, 0), 1.5)
    grad = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))

    def edge_strength(region):
        r = region.astype(np.uint8)
        band = (cv2.dilate(r, np.ones((5, 5), np.uint8)) - cv2.erode(r, np.ones((5, 5), np.uint8))) > 0
        H, W = r.shape
        band[:3], band[-3:], band[:, :3], band[:, -3:] = False, False, False, False
        return float(np.median(grad[band])) if band.any() else 0.0

    outline_edge = edge_strength(mask)
    out = mask.copy()
    n, cc, stats, _ = cv2.connectedComponentsWithStats(cand, 8)
    for i in range(1, n):
        blob = cc == i
        if stats[i, cv2.CC_STAT_AREA] < 0.005 * mask.sum():
            continue
        grown = cv2.dilate(blob.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
        if (grown & ~mask).sum() < 0.02 * grown.sum() and edge_strength(blob) > 0.25 * outline_edge:
            out &= ~(cv2.dilate(blob.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0)
    return out


def find_subject(frame: np.ndarray) -> tuple[Subject | None, str]:
    """Main subject of a (3:4 cropped) frame, with face parts for animals."""
    segs = _segment(frame)
    if not segs:
        return None, "no subject (no animal / front object detected)"
    label, conf, box, mask = segs[0]
    family = "animal" if label in ANIMALS else "object"
    det = _clean_mask(mask)
    mask = _carve_holes(frame, det) if family == "object" else det
    s = Subject(label, family, conf, box, mask, mask.copy(), det,
                others=[(l, float(m.sum())) for l, _, _, m in segs[1:]])
    if family == "animal":
        s.head, s.eyes, s.nose = _parts(frame, label, box)
        if s.head is not None:
            # the body behind / below the head is often out of focus or hidden by grass: draw a bust
            H, W = mask.shape
            hx1, hy1, hx2, hy2 = s.head
            hw, hh = hx2 - hx1, hy2 - hy1
            win = np.zeros((H, W), bool)
            x0, x1 = int(max(0, hx1 - BUST_SIDE * hw)), int(min(W, hx2 + BUST_SIDE * hw))
            win[:int(min(H, hy2 + BUST_BELOW_HEAD * hh)), x0:x1] = True
            if not win[mask].all():
                s.window = win
                s.region = _clean_mask(mask & win)
    return s, ""


# ----------------------------------------------------------------------------- suitability

def _border_share(region: np.ndarray) -> float:
    cs, _ = cv2.findContours(region.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    pts = max(cs, key=len)[:, 0]
    H, W = region.shape
    on = (pts[:, 0] <= 1) | (pts[:, 1] <= 1) | (pts[:, 0] >= W - 2) | (pts[:, 1] >= H - 2)
    return float(on.mean())


def _edge_sharpness(frame: np.ndarray, region: np.ndarray) -> float:
    """Laplacian variance on a band around the silhouette: where focus matters for tracing, and it
    works for smooth objects (a plain cup has little texture inside but a crisp outline)."""
    g = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY).astype(np.float32)
    lap = cv2.Laplacian(g, cv2.CV_32F)
    m = region.astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    band = (cv2.dilate(m, k) - cv2.erode(m, k)) > 0
    H, W = region.shape
    band[:2], band[-2:], band[:, :2], band[:, -2:] = False, False, False, False
    return float(lap[band].var()) if band.any() else 0.0


def is_drawable(frames: np.ndarray, fps: float, subject: Subject | None = None) -> tuple[bool, str, dict]:
    """Does this shot (3:4 cropped frames from the effect's start) suit the drawn reveal?
    Returns (ok, reason, measurements). The reason names the first failed check, or summarises a pass."""
    info: dict = {}
    s, why = (subject, "") if subject is not None else find_subject(frames[0])
    if s is None:
        return False, why, info
    H, W = frames[0].shape[:2]
    area = float(s.region.sum()) / (H * W)
    info.update(label=s.label, family=s.family, conf=round(s.conf, 2), area=round(area, 3),
                mask_area=round(float(s.mask.sum()) / (H * W), 3))
    if s.conf < MIN_CONF:
        return False, f"unsure what the subject is ({s.label} at {s.conf:.2f} confidence)", info
    main = float(s.mask.sum())
    if s.others:
        info["second_ratio"] = round(main / max(1.0, s.others[0][1]), 1)
        if main < DOMINANCE * s.others[0][1]:
            return False, f"multiple subjects ({s.label} + {s.others[0][0]}, only {main / s.others[0][1]:.1f}x larger)", info
    if s.family == "animal":
        info.update(head=s.head is not None, eyes=len(s.eyes), nose=s.nose is not None)
        if s.head is None:
            return False, f"no {s.label} head found", info
        if len(s.eyes) < 2 or s.nose is None:
            return False, f"face not frontal ({len(s.eyes)} level eye pair(s), nose {'found' if s.nose is not None else 'missing'})", info
        hb = s.head
        if hb[0] < 2 or hb[1] < 2 or hb[2] > W - 2:
            return False, "head cut off by the frame", info
    else:
        border = _border_share(s.region)
        info["border"] = round(border, 3)
        if border > MAX_BORDER:
            return False, f"{s.label} cut off by the frame ({100 * border:.0f}% of its outline on the border)", info
    if not MIN_AREA <= area <= MAX_AREA:
        return False, f"subject covers {100 * area:.0f}% of the frame (needs {100 * MIN_AREA:.0f}-{100 * MAX_AREA:.0f}%)", info
    sharp = _edge_sharpness(frames[0], s.region)
    info["edge_sharpness"] = round(sharp, 1)
    if sharp < MIN_EDGE_SHARP:
        return False, f"start frame soft (edge sharpness {sharp:.0f} < {MIN_EDGE_SHARP:.0f})", info
    k = min(len(frames) - 1, int(round(0.3 * fps)))
    if k > 0:
        ref = s.det_mask if s.det_mask is not None else s.mask
        iou = 0.0     # best match regardless of label (YOLO flips cup / bowl between frames)
        for _, _, _, m in _segment(frames[k]):
            m2 = _clean_mask(m)
            iou = max(iou, float((m2 & ref).sum()) / max(1.0, float((m2 | ref).sum())))
        info["stable_iou"] = round(iou, 3)
        if iou < MIN_STABLE_IOU:
            return False, f"subject moves / changes in the first 0.3s (mask IoU {iou:.2f})", info
    face = " with frontal face" if s.family == "animal" else ", fully in frame"
    return True, f"single {s.label}{face}, {100 * area:.0f}% of frame", info


# ----------------------------------------------------------------------------- strokes

@dataclass
class Stroke:
    pts: np.ndarray        # (N, 2) output px, wobble applied
    width: float
    kind: str              # outline | inner | feature
    length: float = 0.0
    widths: np.ndarray | None = None


def _resample(pts: np.ndarray, step: float, closed: bool = False) -> np.ndarray:
    if closed:
        pts = np.vstack([pts, pts[:1]])
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    if s[-1] < step:
        return pts
    u = np.arange(0, s[-1], step)
    out = np.stack([np.interp(u, s, pts[:, 0]), np.interp(u, s, pts[:, 1])], 1)
    return out


def _smooth(pts: np.ndarray, sigma: float, closed: bool = False) -> np.ndarray:
    from scipy.ndimage import gaussian_filter1d
    if len(pts) < 3 or sigma <= 0:
        return pts
    return gaussian_filter1d(pts, sigma, axis=0, mode="wrap" if closed else "nearest")


def _plen(pts: np.ndarray) -> float:
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum()) if len(pts) > 1 else 0.0


def _outline_strokes(s: Subject, scale: float, rng) -> list[np.ndarray]:
    """Outer contour (and large holes, e.g. a cup handle) in a few strokes, open where the subject
    leaves the frame or where the bust is cut. Output coordinates in output px."""
    H, W = s.region.shape
    cs, hier = cv2.findContours(s.region.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    total = float(s.region.sum())
    loops = []
    for c, h in zip(cs, hier[0]):
        area = cv2.contourArea(c)
        if h[3] < 0 and area > 0.3 * total:
            loops.append((0, c[:, 0].astype(float)))
        elif h[3] >= 0 and area > 0.01 * total:
            loops.append((1, c[:, 0].astype(float)))
    strokes = []
    for is_hole, c in sorted(loops, key=lambda l: l[0]):
        p = _smooth(_resample(c, 2.0, closed=True), 2.5, closed=True)
        keep = (p[:, 0] > 2) & (p[:, 0] < W - 3) & (p[:, 1] > 2) & (p[:, 1] < H - 3)
        if s.window is not None:
            inside = cv2.erode(s.window.astype(np.uint8), np.ones((9, 9), np.uint8))
            keep &= inside[np.clip(p[:, 1].astype(int), 0, H - 1), np.clip(p[:, 0].astype(int), 0, W - 1)] > 0
        # start at the topmost kept point (ears first for animals)
        if keep.all():
            start = int(np.argmin(p[:, 1]))
            runs = [np.roll(p, -start, 0)]
        else:
            first_gap = int(np.argmin(keep))
            p, keep = np.roll(p, -first_gap, 0), np.roll(keep, -first_gap)
            runs, cur = [], []
            for q, k in zip(p, keep):
                if k:
                    cur.append(q)
                elif cur:
                    runs.append(np.array(cur)); cur = []
            if cur:
                runs.append(np.array(cur))
        runs = [r for r in runs if _plen(r) > 0.04 * math.sqrt(total)]
        perim = sum(_plen(r) for r in runs)
        # split long runs at corners (ear tips, handle joints) into 25-45% of the perimeter
        for r in runs:
            pieces = _split_at_corners(r, 0.22 * perim, 0.45 * perim) if not is_hole else [r]
            for piece in pieces:
                strokes.append(piece * scale)
    # order: topmost stroke first, then walk to the nearest unstarted end
    return _order_strokes(strokes)


def _split_at_corners(p: np.ndarray, lo: float, hi: float) -> list[np.ndarray]:
    seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    if s[-1] <= hi:
        return [p]
    d = np.gradient(_smooth(p, 4.0), axis=0)
    ang = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
    curv = np.abs(np.gradient(_smooth(ang[:, None], 3.0)[:, 0]))
    cuts, last = [], 0.0
    while s[-1] - last > hi:
        window = (s > last + lo) & (s < min(last + hi, s[-1] - lo * 0.5))
        if not window.any():
            break
        i = int(np.argmax(np.where(window, curv, -1)))
        cuts.append(i)
        last = s[i]
    out, i0 = [], 0
    for i in cuts:
        out.append(p[i0:i + 3])     # small overlap: the next stroke restarts just before the corner
        i0 = max(0, i - 1)
    out.append(p[i0:])
    return out


def _order_strokes(strokes: list[np.ndarray]) -> list[np.ndarray]:
    if not strokes:
        return []
    rest = list(strokes)
    first = min(range(len(rest)), key=lambda i: min(rest[i][0, 1], rest[i][-1, 1]))
    cur = rest.pop(first)
    if cur[-1, 1] < cur[0, 1]:
        cur = cur[::-1]
    out = [cur]
    while rest:
        end = out[-1][-1]
        j, flip = min(((i, f) for i in range(len(rest)) for f in (0, 1)),
                      key=lambda x: np.linalg.norm((rest[x[0]][-1] if x[1] else rest[x[0]][0]) - end))
        nxt = rest.pop(j)
        out.append(nxt[::-1] if flip else nxt)
    return out


def _trace_edges(edges: np.ndarray) -> list[np.ndarray]:
    """Pixel chains of a 1-px edge map, split at junctions. Returns (N, 2) x,y arrays."""
    on = edges > 0
    H, W = on.shape
    nb = cv2.filter2D(on.astype(np.uint8), -1, np.ones((3, 3), np.float32),
                      borderType=cv2.BORDER_CONSTANT) - on.astype(np.uint8)
    visited = np.zeros_like(on)
    offs = [(0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)]
    paths = []

    def walk(y, x):
        path = [(x, y)]
        visited[y, x] = True
        while True:
            nxt = None
            for dy, dx in offs:
                yy, xx = y + dy, x + dx
                if 0 <= yy < H and 0 <= xx < W and on[yy, xx] and not visited[yy, xx]:
                    nxt = (yy, xx)
                    break
            if nxt is None:
                break
            y, x = nxt
            visited[y, x] = True
            path.append((x, y))
            if nb[y, x] > 2:
                break
        return np.array(path, float)

    ys, xs = np.nonzero(on & (nb == 1))
    for y, x in zip(ys, xs):
        if not visited[y, x]:
            paths.append(walk(y, x))
    ys, xs = np.nonzero(on)
    for y, x in zip(ys, xs):
        if not visited[y, x]:
            paths.append(walk(y, x))
    return paths


def _ellipse_pts(cx, cy, rx, ry, n=48, a0=-90.0, sweep=380.0) -> np.ndarray:
    """Ellipse drawn as one stroke, slightly over a full turn (hand-drawn closure)."""
    th = np.radians(a0 + np.linspace(0, sweep, n))
    return np.stack([cx + rx * np.cos(th), cy + ry * np.sin(th)], 1)


def _feature_strokes(s: Subject, frame: np.ndarray) -> tuple[list[np.ndarray], np.ndarray, list]:
    """Eyes, nose, mouth (+ whiskers for cats) from the YOLOE part boxes, native px.
    Returns strokes, an exclusion mask (no Canny lines there) and feature fills [(mask_fn, kind)]."""
    H, W = frame.shape[:2]
    excl = np.zeros((H, W), np.uint8)
    strokes, fills = [], []
    for b in s.eyes:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        rx, ry = 0.44 * (b[2] - b[0]), 0.44 * (b[3] - b[1])
        strokes.append(_ellipse_pts(cx, cy, rx, ry))
        cv2.ellipse(excl, (int(cx), int(cy)), (int(rx * 1.5), int(ry * 1.5)), 0, 0, 360, 1, -1)
        fills.append(("eye", (cx, cy, rx, ry)))
    if s.nose is not None:
        x1, y1, x2, y2 = s.nose
        nw, nh = x2 - x1, y2 - y1
        cx = (x1 + x2) / 2
        # rounded inverted triangle
        tl, tr, bot = (x1 + 0.12 * nw, y1 + 0.28 * nh), (x2 - 0.12 * nw, y1 + 0.28 * nh), (cx, y2 - 0.1 * nh)
        nose = np.vstack([_bezier(tl, (cx, y1 + 0.05 * nh), tr, 12),
                          _bezier(tr, (x2 - 0.15 * nw, y1 + 0.7 * nh), bot, 10)[1:],
                          _bezier(bot, (x1 + 0.15 * nw, y1 + 0.7 * nh), tl, 10)[1:]])
        strokes.append(nose)
        fills.append(("nose", nose.copy()))
        yb = y2 - 0.1 * nh
        ym = yb + 0.45 * nh
        mouth_w = 0.85 * nw
        strokes.append(np.vstack([[cx, yb], [cx, ym]]))
        for sx in (-1, 1):
            strokes.append(_bezier((cx, ym), (cx + sx * 0.45 * mouth_w, ym + 0.55 * nh),
                                   (cx + sx * mouth_w, ym + 0.05 * nh), 14))
        cv2.rectangle(excl, (int(x1 - 0.6 * nw), int(y1 - 0.2 * nh)), (int(x2 + 0.6 * nw), int(ym + 0.8 * nh)), 1, -1)
        if s.label == "cat" and s.head is not None:
            hw = s.head[2] - s.head[0]
            for sx in (-1, 1):
                for k, (dy, bend) in enumerate(((-0.05, -0.10), (0.18, 0.0), (0.42, 0.12))):
                    p0 = np.array([cx + sx * 0.75 * nw, ym + dy * nh])
                    p2 = p0 + [sx * 0.33 * hw, (bend * 1.6 + 0.02) * hw]
                    p1 = (p0 + p2) / 2 + [0, -0.03 * hw]
                    strokes.append(_bezier(p0, p1, p2, 14))
    return strokes, excl, fills


def _inner_strokes(frame: np.ndarray, s: Subject, excl: np.ndarray, budget: float) -> list[np.ndarray]:
    """Strong interior edges as stroke paths (native px), longest / strongest first within `budget`
    total length. Edges near the outline, inside excluded feature areas, too short, or out of focus
    (grass blades in front of a cat) are dropped."""
    g0 = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    focus = cv2.dilate(np.abs(cv2.Laplacian(g0.astype(np.float32), cv2.CV_32F, ksize=3)), np.ones((3, 3), np.uint8))
    g = cv2.bilateralFilter(g0, 9, 30, 7)
    g = cv2.GaussianBlur(g, (0, 0), 1.6)
    gx, gy = cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)
    mag = np.hypot(gx, gy)
    inner = cv2.erode(s.region.astype(np.uint8),
                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13))) > 0
    if not inner.any():
        return []
    hi = float(np.percentile(mag[inner], 92))
    edges = cv2.Canny(g, 0.45 * hi, hi, L2gradient=True)
    edges[~inner | (excl > 0)] = 0
    size = math.sqrt(float(s.region.sum()))
    min_len = max(16.0, 0.08 * size)
    sharp_ref = float(np.percentile(focus[edges > 0], 60)) if (edges > 0).any() else 0.0
    cands = []
    for p in _trace_edges(edges):
        if len(p) < 4:
            continue
        L = _plen(p)
        if L < min_len:
            continue
        ip = p.astype(int)
        if float(focus[ip[:, 1], ip[:, 0]].mean()) < 0.5 * sharp_ref:
            continue
        strength = float(mag[ip[:, 1], ip[:, 0]].mean()) / hi
        w = 1.0
        if s.head is not None:
            cx, cy = p[:, 0].mean(), p[:, 1].mean()
            hb = s.head
            in_head = hb[0] <= cx <= hb[2] and hb[1] <= cy <= hb[3]
            if not in_head:                    # body edges of animals are mostly fur / grass
                continue
        cands.append((L * strength * w, p))
    cands.sort(key=lambda c: -c[0])
    out, used = [], 0.0
    for score, p in cands:
        L = _plen(p)
        if used + L > budget or len(out) >= 40:
            continue
        q = _smooth(_resample(p, 2.0), 2.0)
        out.append(q)
        used += L
    return out


def build_strokes(frame: np.ndarray, s: Subject, scale: float, seed: int = 0) -> list[Stroke]:
    rng = np.random.default_rng(seed)
    Wout = frame.shape[1] * scale
    outline = _outline_strokes(s, scale, rng)
    feats, excl, _ = _feature_strokes(s, frame) if s.family == "animal" else ([], np.zeros(s.region.shape, np.uint8), [])
    perim = sum(_plen(p) for p in outline) / scale
    inner = _inner_strokes(frame, s, excl, budget=(0.9 if s.family == "animal" else 1.2) * perim)
    strokes = [Stroke(p, 0.0085 * Wout, "outline") for p in outline]
    # inner lines: top-down; features after the outline in drawing order: eyes, nose, mouth, whiskers
    strokes += [Stroke(p * scale, 0.0062 * Wout, "feature") for p in feats]
    strokes += [Stroke(p * scale, 0.0045 * Wout, "inner") for p in _order_strokes(inner)]
    for st in strokes:
        p = _resample(st.pts, 1.2 * scale)
        if len(p) < 2:
            p = st.pts
        n = len(p)
        # static hand imprecision: low-frequency wobble across the stroke
        seg = np.diff(p, axis=0)
        normal = np.stack([-seg[:, 1], seg[:, 0]], 1)
        normal = np.vstack([normal, normal[-1:]])
        normal /= np.linalg.norm(normal, axis=1, keepdims=True) + 1e-9
        amp = (0.0016 if st.kind == "outline" else 0.0011) * Wout
        st.pts = p + normal * (_smooth_noise(n, rng, knots=max(3, n // 60 + 3))[:, None] * amp)
        u = np.linspace(0, 1, n)
        press = 0.85 + 0.25 * _smooth_noise(n, rng, knots=4)       # pressure variation
        st.widths = st.width * np.clip(press, 0.6, 1.25) * (0.4 + 0.6 * np.sin(np.pi * u) ** 0.5)
        st.length = _plen(st.pts)
    return strokes


def draw_lines(strokes: list[Stroke], progress: float, size: tuple[int, int], boil: int,
               boil_amp: float, gap: float) -> Image.Image:
    """Line layer (RGBA, output size) with the first `progress` (0..1) of the total pen path drawn."""
    W, H = size
    img = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    total = sum(st.length for st in strokes) + gap * len(strokes)
    budget = progress * total
    rng = np.random.default_rng(1000 + boil)
    for st in strokes:
        if budget <= 0:
            break
        p = st.pts
        n = len(p)
        cum = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))])
        k = int(np.searchsorted(cum, budget, side="right"))
        budget -= st.length + gap
        k = max(2, min(n, k))
        seg = np.diff(p, axis=0)
        normal = np.stack([-seg[:, 1], seg[:, 0]], 1)
        normal = np.vstack([normal, normal[-1:]])
        normal /= np.linalg.norm(normal, axis=1, keepdims=True) + 1e-9
        q = (p + normal * (_smooth_noise(n, rng, knots=max(3, n // 40 + 3))[:, None] * boil_amp))[:k] * SS
        w = st.widths[:k] * SS
        if k < n:   # pen still moving: the tip is at full width, not tapered
            w = np.maximum(w, st.width * SS * np.clip(np.arange(k)[::-1] / 6.0, 0, 1) * 0.9)
        for i in range(k):
            r = w[i] / 2
            d.ellipse([q[i, 0] - r, q[i, 1] - r, q[i, 0] + r, q[i, 1] + r], fill=INK + (255,))
            if i < k - 1:
                d.line([tuple(q[i]), tuple(q[i + 1])], fill=INK + (255,), width=max(1, int(round(w[i]))))
    return img.resize((W, H), Image.LANCZOS)


# ----------------------------------------------------------------------------- fills

def _noise(shape, sigma: float, rng) -> np.ndarray:
    n = cv2.GaussianBlur(rng.normal(0, 1, shape).astype(np.float32), (0, 0), sigma)
    n -= n.mean()
    return n / (n.std() + 1e-6)


def make_paper(H: int, W: int, rng) -> np.ndarray:
    paper = np.tile(PAPER, (H, W, 1))
    paper += 4.0 * _noise((H, W), 60, rng)[..., None]
    paper += 3.0 * _noise((H, W), 0.8, rng)[..., None]
    # faint fibres: thin streaks
    fib = cv2.GaussianBlur(rng.normal(0, 1, (H, W)).astype(np.float32), (0, 0), sigmaX=6, sigmaY=0.6)
    paper -= 2.5 * np.clip(fib / (fib.std() + 1e-6) - 1.5, 0, None)[..., None]
    yy, xx = np.mgrid[0:H, 0:W]
    vig = ((xx - W / 2) / W) ** 2 + ((yy - H / 2) / H) ** 2
    paper *= (1 - 0.12 * vig)[..., None]
    return np.clip(paper, 0, 255)


@dataclass
class FillRegion:
    mask: np.ndarray        # bool, output res
    color: np.ndarray       # RGB float
    s: np.ndarray           # sweep coordinate 0..1 (float32, full frame, only valid in mask)
    q: np.ndarray           # zig-zag phase
    order: float


def _cartoon_color(rgb: np.ndarray, lift: bool = True) -> np.ndarray:
    """Marker version of a photo colour: darks lifted (shading is not the object's colour), more
    chroma, same hue."""
    lab = cv2.cvtColor(np.uint8([[np.clip(rgb, 0, 255)]]), cv2.COLOR_RGB2LAB)[0, 0].astype(float)
    if lift:
        lab[0] = 88 + 0.66 * lab[0]           # L in 0..255 (OpenCV): 0 -> 88, 255 -> 256
    lab[1:] = 128 + 1.45 * (lab[1:] - 128)
    return cv2.cvtColor(np.uint8([[np.clip(lab, 0, 255)]]), cv2.COLOR_LAB2RGB)[0, 0].astype(np.float32)


def build_fills(frame_out: np.ndarray, s: Subject, scale: float, k: int = 4, seed: int = 0):
    """Posterized flat fills on the subject (output res). Returns regions in fill order and the palette."""
    rng = np.random.default_rng(seed)
    H, W = frame_out.shape[:2]
    region = cv2.resize(s.region.astype(np.uint8), (W, H), interpolation=cv2.INTER_LINEAR) > 0
    small = cv2.resize(frame_out, (W // 2, H // 2), interpolation=cv2.INTER_AREA)
    sm = cv2.pyrMeanShiftFiltering(np.ascontiguousarray(small[..., ::-1]), 12, 22)[..., ::-1]
    lab = cv2.cvtColor(sm, cv2.COLOR_RGB2LAB).astype(np.float32)
    rs = cv2.resize(region.astype(np.uint8), (W // 2, H // 2), interpolation=cv2.INTER_NEAREST) > 0
    px = lab[rs]
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 40, 0.5)
    cv2.setRNGSeed(seed)
    _, lbl, centers = cv2.kmeans(px, k, None, crit, 4, cv2.KMEANS_PP_CENTERS)
    # merge near-identical centres (flat objects need fewer colours)
    lbl = lbl[:, 0]
    for i in range(k):
        for j in range(i):
            if np.linalg.norm(centers[i] - centers[j]) < 9 and (lbl == i).any():
                lbl[lbl == i] = j
    # clusters that match the background (grass in front of the cat, table showing through a handle)
    # are not part of the drawing: their pixels take the surrounding subject colour
    outside = ~(cv2.dilate(rs.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0)
    bg_px = lab[outside]
    dropped = []
    if len(bg_px) > 500:
        sub = bg_px[rng.choice(len(bg_px), min(20000, len(bg_px)), replace=False)]
        _, _, bg_c = cv2.kmeans(sub, 3, None, crit, 2, cv2.KMEANS_PP_CENTERS)
        for u in sorted(set(lbl.tolist())):
            share = float((lbl == u).mean())
            d_bg = float(np.min(np.linalg.norm(bg_c - centers[u], axis=1)))
            if d_bg < 14 and share < 0.3:
                dropped.append(u)
    keep = [u for u in sorted(set(lbl.tolist())) if u not in dropped]
    if not keep:
        keep, dropped = sorted(set(lbl.tolist())), []
    labels = np.full(rs.shape, -1, np.int32)
    labels[rs] = lbl
    # mode filter: blobby regions, no speckle (dropped clusters vote for nobody)
    used = keep
    votes = np.stack([cv2.GaussianBlur((labels == u).astype(np.float32), (0, 0), 5.0) for u in used], -1)
    far = votes.max(-1) < 1e-3
    if far.any():
        wide = np.stack([cv2.GaussianBlur((labels == u).astype(np.float32), (0, 0), 30.0) for u in used], -1)
        votes[far] = wide[far]
    labels = np.where(rs, np.array(used)[np.argmax(votes, -1)], -1)
    labels = cv2.resize(labels.astype(np.float32), (W, H), interpolation=cv2.INTER_NEAREST).astype(np.int32)
    labels[~region] = -1
    palette = {}
    for u in used:
        c = cv2.cvtColor(np.uint8([[np.clip(centers[u], 0, 255)]]), cv2.COLOR_LAB2RGB)[0, 0].astype(np.float32)
        palette[u] = _cartoon_color(c)
    # connected components = fill blocks; tiny ones join the surrounding colour
    regions = []
    min_area = 0.007 * region.sum()
    for u in used:
        n, cc, stats, _ = cv2.connectedComponentsWithStats((labels == u).astype(np.uint8), 8)
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] < min_area:
                blob = cc == i
                ring = (cv2.dilate(blob.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0) & ~blob & (labels >= 0)
                if ring.any():
                    vals, cnt = np.unique(labels[ring], return_counts=True)
                    labels[blob] = vals[np.argmax(cnt)]
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    for u in used:
        n, cc, stats, _ = cv2.connectedComponentsWithStats((labels == u).astype(np.uint8), 8)
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] < min_area:
                continue
            m = cv2.dilate((cc == i).astype(np.uint8), k5) > 0   # marker overlaps edges a little
            m &= cv2.dilate(region.astype(np.uint8), k5) > 0
            th = math.radians(rng.uniform(25, 65) * rng.choice([-1, 1]))
            d = np.array([math.cos(th), math.sin(th)], np.float32)
            proj = xx * d[0] + yy * d[1]
            perp = -xx * d[1] + yy * d[0]
            lo, hi = proj[m].min(), proj[m].max()
            ext = max(1.0, hi - lo)
            sweep = (proj - lo) / ext
            spacing = max(18.0, 0.07 * ext)
            regions.append(FillRegion(m, palette[u], sweep.astype(np.float32),
                                      (perp / spacing).astype(np.float32), -float(m.sum())))
    regions.sort(key=lambda r: r.order)        # big base blocks first, details last
    return regions, {u: palette[u] for u in used}


def feature_fills(frame_out: np.ndarray, s: Subject, scale: float) -> list[FillRegion]:
    """Eye iris / pupil / highlight and nose fills for animals (drawn last, over the k-means blocks)."""
    if s.family != "animal":
        return []
    _, _, feats = _feature_strokes(s, cv2.resize(frame_out, (s.region.shape[1], s.region.shape[0])))
    H, W = frame_out.shape[:2]
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    out = []

    def region(m, color, order):
        ys, xs = np.nonzero(m)
        lo, hi = xs.min(), xs.max()
        sweep = (xx - lo) / max(1, hi - lo)
        out.append(FillRegion(m, np.asarray(color, np.float32), sweep, yy / 14.0, order))

    g = cv2.cvtColor(frame_out, cv2.COLOR_RGB2GRAY)
    for kind, geo in feats:
        if kind == "eye":
            cx, cy, rx, ry = [v * scale for v in geo]
            em = np.zeros((H, W), np.uint8)
            cv2.ellipse(em, (int(cx), int(cy)), (int(rx), int(ry)), 0, 0, 360, 1, -1)
            em = em > 0
            vals = g[em]
            thr = np.percentile(vals, 30)
            iris = np.median(frame_out[em & (g > thr)], axis=0)
            region(em, _cartoon_color(iris), 1e9)
            dark = (em & (g <= np.percentile(vals, 22))).astype(np.uint8)
            dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
            cs, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            pm = np.zeros((H, W), np.uint8)
            if cs and len(max(cs, key=cv2.contourArea)) >= 5:
                (px_, py_), (a, b), ang = cv2.fitEllipse(max(cs, key=cv2.contourArea))
                a, b = min(a, 1.4 * rx), min(b, 1.8 * ry)
                cv2.ellipse(pm, ((px_, py_), (max(a, 0.35 * rx), max(b, 0.6 * ry)), ang), 1, -1)
            else:
                px_, py_ = cx, cy
                cv2.ellipse(pm, (int(cx), int(cy)), (int(0.35 * rx), int(0.6 * ry)), 0, 0, 360, 1, -1)
            pm = (pm > 0) & em
            region(pm, (34, 28, 27), 1e9 + 1)   # pupil stays dark
            hm = np.zeros((H, W), np.uint8)
            hr = max(3, int(0.2 * min(rx, ry)))
            cv2.circle(hm, (int(px_ - 0.25 * rx), int(py_ - 0.3 * ry)), hr, 1, -1)
            region((hm > 0) & em, (252, 250, 245), 1e9 + 2)
        elif kind == "nose":
            nm = np.zeros((H, W), np.uint8)
            cv2.fillPoly(nm, [np.round(geo * scale).astype(np.int32)], 1)
            nm = nm > 0
            col = np.median(frame_out[nm], axis=0)
            region(nm, _cartoon_color(col), 1e9)
    return out


def crayon_texture(H: int, W: int, rng) -> tuple[np.ndarray, np.ndarray]:
    """Brightness modulation (marker streaks, diagonal) and paper-tooth mask (where crayon skips)."""
    n = rng.normal(0, 1, (H, W)).astype(np.float32)
    L = 31
    kern = np.zeros((L, L), np.float32)
    cv2.line(kern, (0, L - 1), (L - 1, 0), 1.0, 1)
    kern /= kern.sum()
    streak = cv2.filter2D(n, -1, kern)
    streak = streak / (streak.std() + 1e-6)
    # tooth: short streaks along the stroke direction, so the gaps read as crayon, not salt
    k2 = np.zeros((9, 9), np.float32)
    cv2.line(k2, (0, 8), (8, 0), 1.0, 1)
    tn = cv2.filter2D(rng.normal(0, 1, (H, W)).astype(np.float32), -1, k2 / k2.sum())
    tooth_n = tn / (tn.std() + 1e-6) + 0.2 * _noise((H, W), 5, rng)
    tooth = np.clip((tooth_n - 1.9) / 0.6, 0, 1)
    return 1 + 0.06 * np.clip(streak, -2.5, 2.5), tooth


# ----------------------------------------------------------------------------- render

def _ease_out(x: float, p: float = 1.35) -> float:
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** p


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _dissolve(field: np.ndarray, progress: float, soft: float = 0.12) -> np.ndarray:
    return np.clip((progress * (1 + soft) - field) / soft, 0.0, 1.0)


def clean_plate(frame: np.ndarray, region: np.ndarray) -> np.ndarray:
    """The start frame with the subject removed: the (slightly grown) subject area is filled from the
    surrounding background by multi-scale normalised convolution, so the drawing starts on the real
    scene as if the subject weren't there yet. Float32 RGB, same size as `frame`."""
    H, W = region.shape
    grow = max(3, int(0.018 * W)) | 1
    hole = cv2.dilate(region.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow))) > 0
    k = 4                                          # work at 1/4 size: the fill is low-frequency anyway
    small = cv2.resize(frame, (W // k, H // k), interpolation=cv2.INTER_AREA).astype(np.float32)
    keep = cv2.resize((~hole).astype(np.float32), (W // k, H // k), interpolation=cv2.INTER_AREA)
    keep = (keep > 0.99).astype(np.float32)
    fill = None
    for sig in (0.30, 0.12, 0.05, 0.02):           # coarse to fine: fine scales win where they have support
        sg = max(1.0, sig * W / k)
        num = cv2.GaussianBlur(small * keep[..., None], (0, 0), sg)
        den = cv2.GaussianBlur(keep, (0, 0), sg)
        est = num / np.maximum(den, 1e-4)[..., None]
        if fill is None:
            fill = est
        else:
            w = np.clip(den * 2.5, 0, 1)[..., None]
            fill = w * est + (1 - w) * fill
    fill = cv2.resize(fill, (W, H), interpolation=cv2.INTER_CUBIC)
    feather = cv2.GaussianBlur(hole.astype(np.float32), (0, 0), max(1.0, 0.006 * W))[..., None]
    return frame.astype(np.float32) * (1 - feather) + fill * feather


def render(frames_out: np.ndarray, fps: float, s: Subject, strokes, regions, timing: Timing, seed: int = 0,
           background: str = "real", style: str = "lineart"):
    """Yield output frames (RGB uint8, output size). background="real": only the subject is drawn,
    on the real scene with the subject removed; "paper": the whole frame is a sheet of paper.
    `style`: one of looks.DRAWN_STYLES (how the lines and the colour are put down)."""
    tm = timing.resolve()
    T, H, W, _ = frames_out.shape
    rng = np.random.default_rng(seed)
    region_out = cv2.resize(s.region.astype(np.uint8), (W, H), interpolation=cv2.INTER_LINEAR) > 0
    # everything below draws on `paper`; in real mode that "paper" is the clean plate. The whole
    # subject is removed (for animals the drawing covers only the head bust, but the body must not
    # stick out under it); the body comes back with the background during the reveal.
    if background == "real":
        whole = region_out | (cv2.resize(s.mask.astype(np.uint8), (W, H), interpolation=cv2.INTER_LINEAR) > 0)
        paper = clean_plate(frames_out[0], whole)
    else:
        paper = make_paper(H, W, rng)
    shade, tooth = crayon_texture(H, W, rng)
    region = cv2.resize(s.region.astype(np.uint8), (W, H), interpolation=cv2.INTER_LINEAR) > 0
    # crayon colour image (all regions) built once; per-frame only the alphas change
    fill_rgb = np.zeros((H, W, 3), np.float32)
    owner = np.full((H, W), -1, np.int32)
    for i, r in enumerate(regions):
        if r.order < 1e9:
            owner[r.mask] = i
    for i, r in enumerate(regions):
        sel = owner == i
        c = r.color[None, :] * shade[sel][:, None]
        fill_rgb[sel] = c * (1 - 0.22 * tooth[sel][:, None]) + paper[sel] * 0.22 * tooth[sel][:, None]
    # bust cut: colour fades out raggedly instead of a hard edge
    fade = np.ones((H, W), np.float32)
    if s.window is not None:
        win = cv2.resize(s.window.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST)
        d = cv2.distanceTransform(win, cv2.DIST_L2, 5)
        fade = np.clip(d / (0.07 * H) + 0.35 * _noise((H, W), 6, rng), 0, 1)
    edge_n = _noise((H, W), 3, rng)

    # reveal fields
    big = _noise((H, W), 0.03 * W, rng)
    fine = _noise((H, W), 0.006 * W, rng)
    nf = 0.7 * big + 0.3 * fine
    nf = (nf - nf.min()) / (nf.max() - nf.min())
    ys, xs = np.nonzero(region)
    cy, cx = ys.mean(), xs.mean()
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    rad = np.hypot((xx - cx) / W, (yy - cy) / H)
    rad_sub = rad / max(1e-6, rad[region].max())
    field_sub = 0.55 * nf + 0.45 * rad_sub
    field_sub = (field_sub - field_sub[region].min()) / (np.ptp(field_sub[region]) + 1e-6)
    dist = cv2.distanceTransform((~region).astype(np.uint8), cv2.DIST_L2, 5)
    dist = dist / max(1.0, dist.max())
    field_bg = 0.35 * nf + 0.65 * np.sqrt(dist)
    field_bg = (field_bg - field_bg.min()) / (np.ptp(field_bg) + 1e-6)

    n_line = max(1, round(tm.line * fps))
    n_color = max(1, round(tm.color * fps))
    n_hold = round(tm.hold * fps)
    n_rev = max(1, round(tm.reveal * fps))
    n_tail = round(tm.tail * fps)
    still = frames_out[0].astype(np.float32)
    boil_amp = 0.0011 * W
    gap = 0.02 * W
    frame_no = 0
    line_cache = {}

    def lines(progress, boil):
        key = (round(progress, 4), boil)
        if key not in line_cache:
            line_cache.clear()
            img = np.asarray(draw_lines(strokes, progress, (W, H), boil, boil_amp, gap), np.float32)
            line_cache[key] = (img[..., :3], img[..., 3:] / 255.0)
        return line_cache[key]

    def fill_alpha(f: float) -> np.ndarray:
        """Coverage of all regions at colour progress f (0..1): staggered zig-zag wipes."""
        a = np.zeros((H, W), np.float32)
        n = len(regions)
        for i, r in enumerate(regions):
            if r.order >= 1e9:       # eye / nose details: last 30%
                t0, dur = 0.62 + 0.04 * (r.order - 1e9), 0.3
            else:
                t0 = 0.55 * i / max(1, n - 1) if n > 1 else 0.0
                dur = 0.45
            g = _ease_out((f - t0) / dur, 1.8)
            if g <= 0:
                continue
            if g >= 1:
                cov = r.mask.astype(np.float32)
            else:
                saw = np.abs((r.q % 1.0) - 0.5) * 2
                cov = ((r.s < g * 1.15 - 0.15 * saw) & r.mask).astype(np.float32)
            a = np.maximum(a, cov) if r.order < 1e9 else a
            if r.order >= 1e9:
                feat_layers.append((r, cov))
        return a

    def compose(fill_f: float, line_p: float, boil: int, real_sub=0.0, real_bg=0.0, line_a=1.0, base=None):
        nonlocal feat_layers
        feat_layers = []
        out = paper.copy()
        if fill_f > 0:
            a = fill_alpha(fill_f) * fade
            a = a * np.clip(1.15 + 0.25 * edge_n, 0, 1)       # crayon doesn't cover perfectly
            out = out * (1 - a[..., None]) + fill_rgb * a[..., None]
            for r, cov in feat_layers:
                col = r.color[None, None, :] * shade[..., None]
                out = out * (1 - cov[..., None]) + col * cov[..., None]
        if base is not None:
            ar = np.where(region, real_sub, real_bg)[..., None]
            out = out * (1 - ar) + base * ar
        if line_p > 0 and line_a > 0:
            rgb, al = lines(line_p, boil)
            al = al * line_a
            out = out * (1 - al) + rgb * al
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)

    feat_layers = []
    for i in range(n_line):
        p = _ease_out((i + 1) / n_line)
        yield compose(0.0, p, frame_no // 3)
        frame_no += 1
    for i in range(n_color):
        yield compose((i + 1) / n_color, 1.0, frame_no // 3)
        frame_no += 1
    for i in range(n_hold):
        yield compose(1.0, 1.0, frame_no // 3)
        frame_no += 1
    for i in range(n_rev):
        u = (i + 1) / n_rev
        a_sub = _dissolve(field_sub, _ease_out(u / 0.6, 1.6))
        a_bg = _dissolve(field_bg, _ease_out((u - 0.25) / 0.65, 1.6))
        la = 1.0 - float(_smoothstep((u - 0.55) / 0.45))
        yield compose(1.0, 1.0, frame_no // 3, a_sub, a_bg, la, still)
        frame_no += 1
    for i in range(min(n_tail, T)):
        yield frames_out[i]


# ----------------------------------------------------------------------------- style picker thumbnails

def _preview_scene(n: int = 360) -> tuple[np.ndarray, Subject]:
    """A small made-up subject (an orange mug of coffee on a table) for the 「手绘风格选择」 thumbnails."""
    img = np.zeros((n, n, 3), np.float32)
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) / n
    img[:] = np.array([206, 192, 172]) * (1 - 0.15 * yy[..., None])           # wall
    img[yy > 0.74] = np.array([150, 108, 78])                                  # table
    mask = np.zeros((n, n), np.uint8)
    x0, x1, y0, y1 = int(0.27 * n), int(0.67 * n), int(0.30 * n), int(0.80 * n)
    cx, rx, ry = (x0 + x1) // 2, (x1 - x0) // 2, int(0.05 * n)
    cv2.rectangle(mask, (x0, y0), (x1, y1 - ry), 1, -1)
    cv2.ellipse(mask, (cx, y1 - ry), (rx, ry), 0, 0, 180, 1, -1)
    cv2.ellipse(mask, (cx, y0), (rx, ry), 0, 0, 360, 1, -1)
    cv2.ellipse(mask, (x1, int(0.53 * n)), (int(0.10 * n), int(0.12 * n)), 0, -80, 80, 1, int(0.05 * n))
    body = mask > 0
    shade = 1.0 - 0.45 * np.clip((xx - 0.30) / 0.45, 0, 1) ** 1.5                # light from the left
    img[body] = (np.array([236, 122, 64]) * shade[..., None])[body]
    band = body & (np.abs(yy - 0.58) < 0.035)
    img[band] = (np.array([250, 240, 226]) * shade[..., None])[band]
    top = np.zeros((n, n), np.uint8)
    cv2.ellipse(top, (cx, y0), (int(rx * 0.86), int(ry * 0.7)), 0, 0, 360, 1, -1)
    img[top > 0] = (86, 52, 34)
    img = np.clip(img, 0, 255).astype(np.uint8)
    ys, xs = np.nonzero(body)
    s = Subject("cup", "object", 0.95, np.array([xs.min(), ys.min(), xs.max(), ys.max()], np.float32), body, body)
    return img, s


def style_preview(style: str, size: int = 200) -> Image.Image:
    """The finished drawing (before the reveal) of the preview mug in `style`."""
    from .looks import check_drawn_style
    style = check_drawn_style(style)
    frame, s = _preview_scene()
    strokes = build_strokes(frame, s, 1.0, seed=3)
    regions, _ = build_fills(frame, s, 1.0, k=3, seed=3)
    regions += feature_fills(frame, s, 1.0)
    fps, tm = 12, Timing(line=1.0, color=1.0, reveal=0.5, tail=0.0)
    last = round(tm.line * fps) + round(tm.color * fps) - 1
    for i, f in enumerate(render(frame[None], fps, s, strokes, regions, tm, seed=3, background="paper", style=style)):
        if i == last:
            return Image.fromarray(f).resize((size, size), Image.LANCZOS)
    raise RuntimeError("preview render ended early")


# ----------------------------------------------------------------------------- run

def load_shot(clip: str, start: float, dur: float, out_height: int | None = OUT_HEIGHT, aspect: float = ASPECT):
    """Frames from `start`, centre-cropped to `aspect` (w/h, native) plus the same upscaled to the output size."""
    frames, fps = read_clip(clip, start, dur)
    if len(frames) == 0:
        raise RuntimeError(f"no frames at {start}s in {clip}")
    T, H, W, _ = frames.shape
    x0, y0, cw, ch = _center_crop_box(W, H, aspect)
    native = np.ascontiguousarray(frames[:, y0:y0 + ch, x0:x0 + cw])
    if not out_height or out_height == ch:
        return native, native, fps, 1.0
    scale = out_height / ch
    ow = int(round(cw * scale)) // 2 * 2
    out = np.stack([cv2.resize(f, (ow, out_height), interpolation=cv2.INTER_LANCZOS4) for f in native])
    return native, out, fps, ow / cw


def debug_sheet(native: np.ndarray, s: Subject, strokes, regions, scale: float, out: Path) -> None:
    """Fit check: strokes over the photo, subject region, fill blocks."""
    f = native.copy()
    H, W = f.shape[:2]
    ov = f.copy()
    ov[s.region] = (0.55 * ov[s.region] + [0, 0, 110]).astype(np.uint8)
    over = cv2.resize(f, (W * 2, H * 2))
    for st in strokes:
        col = {"outline": (255, 40, 40), "feature": (255, 230, 0), "inner": (40, 160, 255)}[st.kind]
        cv2.polylines(over, [np.round(st.pts * 2 / scale).astype(np.int32)], False, col, 2, cv2.LINE_AA)
    over = cv2.resize(over, (W, H), interpolation=cv2.INTER_AREA)
    for b, col in [(s.head, (60, 220, 60))] + [(e, (255, 230, 0)) for e in s.eyes] + [(s.nose, (255, 0, 255))]:
        if b is not None:
            cv2.rectangle(ov, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), col, 2)
    blocks = np.full_like(f, 240)
    rng = np.random.default_rng(3)
    for r in regions:
        m = cv2.resize(r.mask.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST) > 0
        blocks[m] = (0.6 * r.color + 0.4 * rng.uniform(0, 255, 3)).astype(np.uint8) if r.order < 1e9 else r.color
    contact_sheet([f, ov, over, blocks], out, cols=4, width=360, labels=["frame", "region+parts", "strokes", "fill blocks"])


def run(clip: str, start: float, out: Path, timing: Timing | None = None, check: bool = True,
        seed: int = 0, out_height: int | None = OUT_HEIGHT, k: int = 4, background: str = "real",
        aspect: float = ASPECT, style: str = "lineart") -> dict:
    from .looks import check_drawn_style
    style = check_drawn_style(style)
    timing = timing or Timing()
    tm = timing.resolve()
    native, frames_out, fps, scale = load_shot(clip, start, max(tm.tail, 0.4) + 0.1, out_height, aspect)
    s, why = find_subject(native[0])
    ok, reason, info = is_drawable(native, fps, s) if s is not None else (False, why, {})
    stats = {"clip": clip, "start": start, "drawable": ok, "reason": reason, **info}
    if not ok and check:
        return stats
    strokes = build_strokes(native[0], s, scale, seed)
    regions, palette = build_fills(frames_out[0], s, scale, k=k, seed=seed)
    regions += feature_fills(frames_out[0], s, scale)
    rendered = list(render(frames_out, fps, s, strokes, regions, timing, seed, background, style))
    H, W = frames_out.shape[1:3]
    write_clip(rendered, out, fps, W, H)

    n_line, n_color = round(tm.line * fps), round(tm.color * fps)
    n_hold, n_rev = round(tm.hold * fps), round(tm.reveal * fps)
    a, b = n_line + n_color + n_hold, n_line + n_color + n_hold + n_rev
    picks = [("line 35%", int(0.35 * n_line)), ("line 70%", int(0.7 * n_line)), ("line done", n_line - 1),
             ("color 40%", n_line + int(0.4 * n_color)), ("color done", n_line + n_color - 1),
             ("reveal 35%", a + int(0.35 * n_rev)), ("reveal 70%", a + int(0.7 * n_rev)),
             ("done", min(len(rendered) - 1, b + int(0.5 * (len(rendered) - b))))]
    stem = out.with_suffix("")
    contact_sheet([rendered[i] for _, i in picks], Path(f"{stem}_sheet.jpg"), cols=4, width=360,
                  labels=[f"{l} {i / fps:.2f}s" for l, i in picks])
    debug_sheet(native[0], s, strokes, regions, scale, Path(f"{stem}_debug.jpg"))
    idx = list(range(0, len(rendered), 3))      # rhythm check: every 3rd frame
    contact_sheet([rendered[i] for i in idx], Path(f"{stem}_strip.jpg"), cols=9, width=200,
                  labels=[f"{i / fps:.2f}s" for i in idx])
    stats.update(fps=round(fps, 3), frames=len(rendered), duration=round(len(rendered) / fps, 2),
                 strokes={kd: sum(st.kind == kd for st in strokes) for kd in ("outline", "feature", "inner")},
                 fill_blocks=sum(r.order < 1e9 for r in regions),
                 palette=[[int(v) for v in c] for c in palette.values()],
                 timing={"line": round(tm.line, 3), "color": round(tm.color, 3), "hold": round(tm.hold, 3),
                         "reveal": tm.reveal, "tail": tm.tail, "reveal_starts_at": round(a / fps, 3)})
    Path(f"{stem}.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Drawn reveal: sketch -> colour -> real footage, one shot")
    ap.add_argument("--clip", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--line", type=float, default=0.6, help="line-art phase (s)")
    ap.add_argument("--color", type=float, default=0.5, help="colouring phase (s)")
    ap.add_argument("--reveal", type=float, default=0.4, help="dissolve to footage (s)")
    ap.add_argument("--tail", type=float, default=0.6, help="normal playback after the reveal (s)")
    ap.add_argument("--reveal-at", type=float, help="time (s from clip start) the real subject starts showing, e.g. a beat")
    ap.add_argument("--colors", type=int, default=4, help="k-means colours for the fills (3-4)")
    ap.add_argument("--force", action="store_true", help="render even if is_drawable rejects the shot")
    ap.add_argument("--native", action="store_true", help="render at the crop's native size, not --height")
    ap.add_argument("--aspect", default="3:4", help="crop aspect, e.g. 3:4 or 9:16")
    ap.add_argument("--height", type=int, help="output height (default: 1440 for 3:4, else the preset for --aspect)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--background", choices=["real", "paper"], default="real",
                    help="real: draw only the subject on the real scene; paper: whole frame is paper")
    a = ap.parse_args()
    from .formats import parse_aspect, resolution_for
    aw, ah = parse_aspect(a.aspect)
    height = a.height or resolution_for(a.aspect)[1]
    timing = Timing(a.line, a.color, a.reveal, a.tail, a.reveal_at)
    stats = run(a.clip, a.start, Path(a.out), timing, check=not a.force, seed=a.seed,
                out_height=None if a.native else height, k=a.colors, background=a.background, aspect=aw / ah)
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
