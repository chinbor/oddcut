"""Tracked cartoon stickers on a single shot: the odd-style "物体拟人" layer.

detect  YOLO11-seg finds the object (cup / cat ...); for animals YOLOE (open vocabulary, "cat head")
        finds the head inside it
track   LK optical flow around the attach point gives exact frame-to-frame motion; the detector only
        corrects the slow drift of that flow path (zero-phase low-pass), so the sticker sticks without
        inheriting the detector's per-frame box jitter
draw    PIL hand-drawn stickers (googly eyes, 💢, red ?), supersampled, with pop-in / blink / line boil
encode  ffmpeg rawvideo pipe -> libx264

    python -m oddcut.stickers --clip assets/public/clips/808-720.mp4 --rule eyes --dur 4 --out output/stickers/808_eyes.mp4
"""
import argparse
import json
import math
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import gaussian_filter1d, median_filter

from .ffmpeg_utils import probe
from .paths import MODEL_DIR

OBJECT_MODEL = "yolo11s-seg.pt"
HEAD_MODEL = "yoloe-11s-seg.pt"

ANIMALS = ["bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"]
FRONT_OBJECTS = ["cup", "bowl", "vase", "apple", "orange", "clock", "car", "bus", "truck",
                 "teddy bear", "bottle", "wine glass", "sports ball", "potted plant"]

# anchor: attach point, normalized, in the object's body (opened mask) for eyes, in the head box for
# doodles. Head anchors sit on the top edge: on close-ups YOLOE alternates between whole-head and
# eye-region boxes, which agree on the ears far better than on the centre.
# offset / size: sticker centre (relative to the attach point) and width, in units of the attach scale
# (object width at the anchor row, or sqrt(head box area)).
# alt_offset: used for the whole shot when the preferred spot is off-screen too often (head at the top edge).
RULES = {
    "eyes":     dict(targets=FRONT_OBJECTS, head=False, anchor=(0.5, 0.42), offset=(0.0, 0.0), size=0.62),
    "anger":    dict(targets=ANIMALS, head=True, anchor=(0.8, 0.2), offset=(0.12, -0.12), size=0.40,
                     alt_offset=(0.32, 0.25)),
    "question": dict(targets=ANIMALS, head=True, anchor=(0.5, 0.1), offset=(0.12, -0.38), size=0.42,
                     alt_offset=(0.72, 0.10)),
}

RED = (232, 30, 36, 255)
WHITE = (255, 255, 255, 255)
BLACK = (18, 18, 18, 255)
SS = 3  # supersampling for antialiased PIL drawing


# ----------------------------------------------------------------------------- video io

def read_clip(path: str, start: float, dur: float) -> tuple[np.ndarray, float]:
    """Decode [start, start+dur) at native resolution. Returns (T, H, W, 3) uint8 RGB and fps."""
    info = probe(path)
    w, h = info.width, info.height
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", path, "-t", f"{dur:.3f}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdin=subprocess.DEVNULL, capture_output=True, check=True,
    )
    n = len(proc.stdout) // (w * h * 3)
    return np.frombuffer(proc.stdout[: n * w * h * 3], dtype=np.uint8).reshape(n, h, w, 3), info.fps


def write_clip(frames, out: Path, fps: float, w: int, h: int) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
         "-r", f"{fps:.6f}", "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
         "-pix_fmt", "yuv420p", str(out)],
        stdin=subprocess.PIPE,
    )
    for f in frames:
        proc.stdin.write(np.ascontiguousarray(f).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg encode failed: {out}")


# ----------------------------------------------------------------------------- detection

@contextmanager
def _in_model_dir():
    """Ultralytics downloads weights into the cwd; keep them all in assets/models/."""
    MODEL_DIR.mkdir(exist_ok=True)
    cwd = os.getcwd()
    os.chdir(MODEL_DIR)
    try:
        yield
    finally:
        os.chdir(cwd)


@dataclass
class Det:
    box: np.ndarray          # x1 y1 x2 y2
    conf: float
    name: str
    attach: np.ndarray | None = None   # anchor point derived from the mask (eyes rule)
    scale: float = 0.0                 # object width at the anchor row


def _mask_attach(mask: np.ndarray, box: np.ndarray, anchor) -> tuple[np.ndarray, float] | None:
    """Anchor point + width on the object's body: open the mask to drop thin parts (handles, spoons)."""
    x1, y1, x2, y2 = box
    k = max(3, int(0.14 * (x2 - x1)) | 1)
    body = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    ys, xs = np.nonzero(body)
    if len(xs) < 50:
        return None
    by1, by2 = np.percentile(ys, 2), np.percentile(ys, 98)
    ya = by1 + anchor[1] * (by2 - by1)
    band = np.abs(ys - ya) < max(3.0, 0.05 * (by2 - by1))
    if band.sum() < 10:
        return None
    lo, hi = np.percentile(xs[band], 3), np.percentile(xs[band], 97)
    return np.array([lo + anchor[0] * (hi - lo), ya]), float(hi - lo)


def detect_objects(frames: np.ndarray, targets: list[str], anchor, batch: int = 8) -> list[list[Det]]:
    from ultralytics import YOLO
    with _in_model_dir():
        model = YOLO(OBJECT_MODEL)
    ids = [i for i, n in model.names.items() if n in targets]
    out: list[list[Det]] = []
    for i in range(0, len(frames), batch):
        bgr = [np.ascontiguousarray(f[..., ::-1]) for f in frames[i:i + batch]]
        for r in model.predict(bgr, classes=ids, conf=0.2, imgsz=640, retina_masks=True, verbose=False):
            dets = []
            masks = r.masks.data.cpu().numpy() > 0.5 if r.masks is not None else [None] * len(r.boxes)
            for b, c, k, m in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy(),
                                  r.boxes.cls.cpu().numpy(), masks):
                d = Det(b, float(c), model.names[int(k)])
                if m is not None:
                    a = _mask_attach(m, b, anchor)
                    if a is not None:
                        d.attach, d.scale = a
                dets.append(d)
            out.append(dets)
    return out


def detect_heads(frames: np.ndarray, prompt: str, batch: int = 8) -> list[list[Det]]:
    """Open-vocabulary head boxes. Text embeddings are cached so the 600 MB text encoder loads once."""
    import torch
    from ultralytics import YOLOE
    with _in_model_dir():
        model = YOLOE(HEAD_MODEL)
        cache = Path(f"yoloe_pe_{prompt.replace(' ', '_')}.pt")
        if cache.exists():
            pe = torch.load(cache)
        else:
            pe = model.get_text_pe([prompt])
            torch.save(pe, cache)
        model.set_classes([prompt], pe)
    out = []
    for i in range(0, len(frames), batch):
        bgr = [np.ascontiguousarray(f[..., ::-1]) for f in frames[i:i + batch]]
        for r in model.predict(bgr, conf=0.05, imgsz=640, verbose=False):
            out.append([Det(b, float(c), prompt) for b, c in
                        zip(r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy())])
    return out


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def pick_track(dets: list[list[Det]], gate=None, min_iou: float = 0.3) -> list[Det | None]:
    """One object through the shot: seed on the most confident large detection, then greedy IoU
    association forwards and backwards. `gate(t, det)` can veto candidates (e.g. head outside body)."""
    cands = [[d for d in ds if gate is None or gate(t, d)] for t, ds in enumerate(dets)]
    best, seed = -1.0, None
    for t, ds in enumerate(cands):
        for j, d in enumerate(ds):
            area = (d.box[2] - d.box[0]) * (d.box[3] - d.box[1])
            s = d.conf * math.sqrt(area)
            if s > best:
                best, seed = s, (t, j)
    track: list[Det | None] = [None] * len(dets)
    if seed is None:
        return track
    track[seed[0]] = cands[seed[0]][seed[1]]
    for rng in (range(seed[0] + 1, len(dets)), range(seed[0] - 1, -1, -1)):
        last = track[seed[0]]
        for t in rng:
            scored = [(_iou(last.box, d.box) * (0.5 + d.conf), d) for d in cands[t]
                      if _iou(last.box, d.box) >= min_iou]
            if scored:
                track[t] = last = max(scored, key=lambda x: x[0])[1]
    return track


# ----------------------------------------------------------------------------- tracking

def _interp(values: np.ndarray) -> np.ndarray:
    """Fill NaN rows by linear interpolation (nearest at the ends). values: (T, D)."""
    out = values.copy()
    t = np.arange(len(values))
    for d in range(values.shape[1]):
        ok = ~np.isnan(values[:, d])
        out[:, d] = np.interp(t, t[ok], values[ok, d])
    return out


def _robust_lowpass(raw: np.ndarray, sigma: float, thresh: np.ndarray, iters: int = 4):
    """Slowly varying fit through `raw` (T, D; NaN = missing) that ignores outlier runs: a long running
    median seeds the fit, then points further than `thresh` (per frame) are dropped and it is refit."""
    T = len(raw)
    have = ~np.isnan(raw[:, 0])
    ok = have.copy()
    fit = median_filter(_interp(raw), size=(max(3, int(6 * sigma) | 1), 1), mode="nearest")
    for _ in range(iters):
        res = np.full(T, np.inf)
        res[have] = np.linalg.norm(raw[have] - fit[have], axis=1)
        new_ok = have & (res < thresh)
        if new_ok.sum() < max(3, 0.25 * have.sum()):
            break
        ok = new_ok
        fit = gaussian_filter1d(_interp(np.where(ok[:, None], raw, np.nan)), sigma, axis=0, mode="nearest")
    return fit, ok


def flow_deltas(gray: list[np.ndarray], boxes: np.ndarray, points: np.ndarray):
    """Per-step motion of `points[t]` from LK features inside `boxes[t]` (similarity fit, RANSAC).
    Returns (T-1, 2) deltas, (T-1,) scale factors and a validity mask."""
    T = len(gray)
    deltas = np.zeros((T - 1, 2))
    scales = np.ones(T - 1)
    valid = np.zeros(T - 1, bool)
    h, w = gray[0].shape
    lk = dict(winSize=(21, 21), maxLevel=3,
              criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
    for t in range(T - 1):
        x1, y1, x2, y2 = boxes[t]
        mask = np.zeros((h, w), np.uint8)
        mask[int(max(0, y1)):int(min(h, y2)), int(max(0, x1)):int(min(w, x2))] = 255
        pts = cv2.goodFeaturesToTrack(gray[t], maxCorners=300, qualityLevel=0.005, minDistance=6, mask=mask)
        if pts is None or len(pts) < 8:
            continue
        nxt, st1, _ = cv2.calcOpticalFlowPyrLK(gray[t], gray[t + 1], pts, None, **lk)
        back, st2, _ = cv2.calcOpticalFlowPyrLK(gray[t + 1], gray[t], nxt, None, **lk)
        good = (st1[:, 0] == 1) & (st2[:, 0] == 1) & (np.linalg.norm(back - pts, axis=2)[:, 0] < 1.0)
        if good.sum() < 8:
            continue
        M, inl = cv2.estimateAffinePartial2D(pts[good], nxt[good], method=cv2.RANSAC,
                                             ransacReprojThreshold=2.0)
        if M is None or inl.sum() < 6:
            continue
        p = points[t]
        deltas[t] = M[:, :2] @ p + M[:, 2] - p
        scales[t] = math.hypot(M[0, 0], M[1, 0])
        valid[t] = True
    return deltas, scales, valid


@dataclass
class Track:
    fps: float
    attach: np.ndarray        # (T, 2) fused attach point
    scale: np.ndarray         # (T,) fused attach scale (px)
    obs: np.ndarray           # (T, 2) raw detector attach point (NaN = missing)
    obs_scale: np.ndarray
    inlier: np.ndarray        # (T,) detector points that agreed with the flow path
    boxes: np.ndarray         # (T, 4) object boxes (interpolated)
    head_boxes: np.ndarray | None
    flow_valid: np.ndarray
    flow_path: np.ndarray
    label: str = ""
    notes: list = field(default_factory=list)

    def stats(self) -> dict:
        ok = self.inlier
        s = float(np.median(self.scale))
        dev = np.linalg.norm(self.obs[ok] - self.attach[ok], axis=1)
        acc_obs = np.linalg.norm(np.diff(_interp(self.obs), 2, axis=0), axis=1)
        acc_fused = np.linalg.norm(np.diff(self.attach, 2, axis=0), axis=1)
        return {
            "frames": int(len(self.attach)),
            "detected_pct": round(100 * float(np.mean(~np.isnan(self.obs[:, 0]))), 1),
            "inlier_pct": round(100 * float(ok.mean()), 1),
            "flow_valid_pct": round(100 * float(self.flow_valid.mean()), 1),
            "median_scale_px": round(s, 1),
            "detector_vs_sticker_rms_pct": round(100 * float(np.sqrt(np.mean(dev ** 2))) / s, 1) if ok.any() else None,
            "accel_rms_raw_px": round(float(np.sqrt(np.mean(acc_obs ** 2))), 2),
            "accel_rms_fused_px": round(float(np.sqrt(np.mean(acc_fused ** 2))), 2),
        }


def _fuse(gray, region, obs, obs_s, fps, corr_sigma):
    """Flow path (exact short-term motion) + robust low-passed detector correction (no long-term drift)."""
    T = len(gray)
    obs_i = _interp(obs)
    s_i = _interp(obs_s[:, None])[:, 0]
    deltas, scales, valid = flow_deltas(gray, region, obs_i)
    deltas[~valid] = np.diff(obs_i, axis=0)[~valid]
    scales[~valid] = (s_i[1:] / s_i[:-1])[~valid]
    flow = np.vstack([obs_i[:1], obs_i[:1] + np.cumsum(deltas, axis=0)])
    flow_s = s_i[0] * np.concatenate([[1.0], np.cumprod(scales)])
    sig = max(1.0, corr_sigma * fps)
    corr, inl = _robust_lowpass(obs - flow, sig, 0.15 * flow_s)
    attach = gaussian_filter1d(flow + corr, 1.0, axis=0, mode="nearest")
    # size: flow carries relative changes, the detector only the slow absolute level (head boxes
    # switch between whole-head and eye-region sizes for seconds at a time)
    log_corr, _ = _robust_lowpass((np.log(obs_s) - np.log(flow_s))[:, None], 4 * sig,
                                  np.full(T, math.log(1.3)))
    scale = flow_s * np.exp(log_corr[:, 0])
    return attach, scale, inl, valid, flow


def track(frames: np.ndarray, fps: float, rule: dict, corr_sigma: float = 0.4) -> Track:
    T, H, W, _ = frames.shape
    notes = []
    objs = detect_objects(frames, rule["targets"], rule["anchor"])
    obj = pick_track(objs)
    if all(d is None for d in obj):
        raise RuntimeError(f"no {rule['targets']} found in the shot")
    label = next(d.name for d in obj if d is not None)
    boxes = _interp(np.array([d.box if d is not None else [np.nan] * 4 for d in obj], float))

    obs = np.full((T, 2), np.nan)
    obs_s = np.full(T, np.nan)
    head_boxes = None
    region = boxes
    if rule["head"]:
        heads = detect_heads(frames, f"{label} head")

        def inside_body(t, d):
            bx = boxes[t]
            cx, cy = (d.box[0] + d.box[2]) / 2, (d.box[1] + d.box[3]) / 2
            m = 0.1 * (bx[2] - bx[0])
            area = (d.box[2] - d.box[0]) * (d.box[3] - d.box[1])
            return (bx[0] - m < cx < bx[2] + m and bx[1] - m < cy < bx[3] + m
                    and area < 0.9 * (bx[2] - bx[0]) * (bx[3] - bx[1]))

        head = pick_track(heads, gate=inside_body, min_iou=0.2)
        if any(d is not None for d in head):
            ax, ay = rule["anchor"]
            for t, d in enumerate(head):
                if d is not None:
                    obs[t] = [d.box[0] + ax * (d.box[2] - d.box[0]), d.box[1] + ay * (d.box[3] - d.box[1])]
                    obs_s[t] = math.sqrt((d.box[2] - d.box[0]) * (d.box[3] - d.box[1]))
            head_boxes = _interp(np.array([d.box if d is not None else [np.nan] * 4 for d in head], float))
            region = head_boxes
        else:
            notes.append("no head found, falling back to the top of the body box")
    if np.isnan(obs[:, 0]).all():
        ax, ay = rule["anchor"]
        for t, d in enumerate(obj):
            if d is None:
                continue
            if d.attach is not None and not rule["head"]:
                obs[t], obs_s[t] = d.attach, d.scale
            else:
                x1, y1, x2, y2 = d.box
                obs[t] = [x1 + ax * (x2 - x1), y1 + ay * (y2 - y1)]
                obs_s[t] = 0.45 * (x2 - x1) if rule["head"] else (x2 - x1)

    gray = [cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in frames]
    attach, scale, inl, valid, flow = _fuse(gray, region, obs, obs_s, fps, corr_sigma)
    if rule["head"]:
        # second pass: sample flow features around the fused head, not inside (possibly wrong) head boxes
        ax, ay = rule["anchor"]
        region = np.stack([attach[:, 0] - ax * scale, attach[:, 1] - ay * scale,
                           attach[:, 0] + (1 - ax) * scale, attach[:, 1] + (1 - ay) * scale], 1)
        attach, scale, inl, valid, flow = _fuse(gray, region, obs, obs_s, fps, corr_sigma)
    return Track(fps, attach, scale, obs, obs_s, inl, boxes, head_boxes, valid, flow, label, notes)


# ----------------------------------------------------------------------------- drawing

def _smooth_noise(n: int, rng: np.random.Generator, knots: int = 6) -> np.ndarray:
    k = rng.normal(0, 1, knots)
    return np.interp(np.linspace(0, knots - 1, n), np.arange(knots), k)


def _stroke(draw: ImageDraw.ImageDraw, pts: np.ndarray, width: float, color, rng, wobble: float,
            taper=(0.45, 1.0)) -> None:
    """Brush stroke: wobbly polyline, thick in the middle, round joins."""
    n = len(pts)
    seg = np.diff(pts, axis=0)
    normal = np.stack([-seg[:, 1], seg[:, 0]], 1)
    normal = np.vstack([normal, normal[-1:]])
    normal /= np.linalg.norm(normal, axis=1, keepdims=True) + 1e-9
    pts = pts + normal * (_smooth_noise(n, rng)[:, None] * wobble)
    u = np.linspace(0, 1, n)
    widths = width * (taper[0] + (taper[1] - taper[0]) * np.sin(np.pi * u) ** 0.7)
    for i in range(n):
        r = widths[i] / 2
        draw.ellipse([pts[i, 0] - r, pts[i, 1] - r, pts[i, 0] + r, pts[i, 1] + r], fill=color)
        if i < n - 1:
            draw.line([tuple(pts[i]), tuple(pts[i + 1])], fill=color, width=int(round(widths[i])))


def _bezier(p0, p1, p2, n=24) -> np.ndarray:
    t = np.linspace(0, 1, n)[:, None]
    return (1 - t) ** 2 * np.array(p0) + 2 * (1 - t) * t * np.array(p1) + t ** 2 * np.array(p2)


def _canvas(w: int, h: int):
    img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _finish(img: Image.Image, w: int, h: int) -> Image.Image:
    return img.resize((w, h), Image.LANCZOS)


def draw_anger(size: int, seed: int) -> Image.Image:
    """💢: four corner strokes bowing towards the centre, red with a white rim."""
    N = size * SS
    img, d = _canvas(size, size)
    rng = np.random.default_rng(seed)
    c, R, g = N / 2, 0.40 * N, 0.10 * N
    strokes = []
    for sx, sy in [(1, -1), (1, 1), (-1, 1), (-1, -1)]:
        j = rng.normal(0, 0.025 * N, 4)
        p0 = (c + sx * (g + j[0] * 0.3), c + sy * (R + j[1]))
        p2 = (c + sx * (R + j[2]), c + sy * (g + j[3] * 0.3))
        p1 = (c + sx * g * 0.55, c + sy * g * 0.55)
        strokes.append(_bezier(p0, p1, p2))
    w = 0.13 * N
    for s in strokes:
        _stroke(d, s, w + 0.07 * N, WHITE, np.random.default_rng(seed), 0.012 * N, taper=(0.7, 1.0))
    for s in strokes:
        _stroke(d, s, w, RED, np.random.default_rng(seed), 0.012 * N, taper=(0.55, 1.0))
    return _finish(img, size, size)


def draw_question(size: int, seed: int) -> Image.Image:
    """Hand-written red ?, slightly slanted. Canvas is size x 1.3*size."""
    Wd, Hd = size, int(size * 1.3)
    img, d = _canvas(Wd, Hd)
    rng = np.random.default_rng(seed)
    N = Wd * SS
    cx, cy, r = 0.5 * N, 0.36 * N, 0.24 * N
    th = np.radians(np.linspace(165, 400, 30))
    arc = np.stack([cx + r * np.cos(th), cy + r * np.sin(th)], 1)
    stem = _bezier(arc[-1], (0.5 * N, 0.62 * N), (0.48 * N, 0.82 * N), 12)
    path = np.vstack([arc, stem[1:]])
    path[:, 0] -= 0.12 * (path[:, 1] - 0.6 * N)            # slant
    w = 0.13 * N
    dot = np.array([0.47 * N - 0.12 * (1.05 * N - 0.6 * N), 1.05 * N])
    dot += rng.normal(0, 0.012 * N, 2)
    for color, extra in ((WHITE, 0.07 * N), (RED, 0.0)):
        _stroke(d, path, w + extra, color, np.random.default_rng(seed), 0.015 * N, taper=(0.55, 1.0))
        rr = (0.085 * N + extra / 2)
        d.ellipse([dot[0] - rr, dot[1] - rr, dot[0] + rr, dot[1] + rr], fill=color)
    return _finish(img, Wd, Hd)


def draw_eyes(size: int, look=(0.0, 0.0), blink: float = 0.0) -> Image.Image:
    """A pair of googly eyes, `size` wide. look in [-1, 1]^2, blink 0 (open) .. 1 (closed)."""
    Wd, Hd = size, int(size * 0.62)
    img, d = _canvas(Wd, Hd)
    N = Wd * SS
    outline = max(2, int(0.035 * N))
    for ex, k in ((0.27, 0.93), (0.73, 1.0)):
        cx, cy = ex * N, 0.5 * Hd * SS
        rx, ry = 0.215 * N * k, 0.28 * N * k
        if blink > 0.75:  # closed: a happy curve
            arc = _bezier((cx - rx, cy), (cx, cy + 0.6 * ry), (cx + rx, cy), 16)
            _stroke(d, arc, outline * 1.6, BLACK, np.random.default_rng(0), 0, taper=(1, 1))
            continue
        ry_b = ry * (1 - blink)
        d.ellipse([cx - rx, cy - ry_b, cx + rx, cy + ry_b], fill=WHITE, outline=BLACK, width=outline)
        pr = 0.105 * N * k
        px = cx + look[0] * (rx - pr - outline) * 0.75
        py = cy + look[1] * (ry_b - pr - outline) * 0.75
        pr_y = min(pr, max(0.0, ry_b - outline))
        d.ellipse([px - pr, py - pr_y, px + pr, py + pr_y], fill=BLACK)
        hr = 0.032 * N * k
        if pr_y > 2 * hr:
            d.ellipse([px - 0.45 * pr - hr, py - 0.45 * pr - hr, px - 0.45 * pr + hr, py - 0.45 * pr + hr],
                      fill=WHITE)
    return _finish(img, Wd, Hd)


# ----------------------------------------------------------------------------- animation

def _ease_out_back(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    c1 = 1.70158
    return 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2


def _eyes_state(t: float) -> tuple[tuple[float, float], float]:
    """Glance left, back, glance right; blink twice. Quick saccades, held poses."""
    keys = [(0.0, (0.0, 0.15)), (1.1, (0.0, 0.15)), (1.2, (-0.7, 0.05)), (1.9, (-0.7, 0.05)),
            (2.0, (0.0, 0.15)), (2.7, (0.0, 0.15)), (2.8, (0.7, 0.05)), (3.5, (0.7, 0.05)),
            (3.6, (0.0, 0.15))]
    look = keys[-1][1]
    for (t0, a), (t1, b) in zip(keys, keys[1:]):
        if t0 <= t < t1:
            u = (t - t0) / (t1 - t0)
            look = (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)
            break
    blink = 0.0
    for tb in (0.8, 3.15):
        dt = abs(t - tb)
        if dt < 0.07:
            blink = 1.0 - dt / 0.07 * 0.3
    return look, blink


def presence(visible: np.ndarray, fps: float, appear: float) -> np.ndarray:
    """Per-frame size multiplier: overshooting pop-in after `appear`, quick shrink-out while the
    object is off-screen, pop back in when it returns."""
    target = visible.astype(float)
    target[: int(round(appear * fps))] = 0.0
    out, e = np.zeros(len(target)), 0.0
    for i, g in enumerate(target):
        if g > e:
            e = min(g, e + 1 / (0.28 * fps))
            out[i] = _ease_out_back(e)
        else:
            e = max(g, e - 1 / (0.15 * fps))
            out[i] = e
    return out


def sticker_frame(rule_name: str, t: float, frame_idx: int, size: float, pop: float, appear: float = 0.0):
    """Sticker image for time t (seconds since shot start) plus rotation (deg)."""
    if pop <= 0.02:
        return None, 0.0
    boil = frame_idx // 3          # redraw wobble every 3 frames = hand-drawn line boil
    if rule_name == "eyes":
        look, blink = _eyes_state(t - appear)
        return draw_eyes(max(8, int(size * pop)), look, blink), 0.0
    if rule_name == "anger":
        throb = 1 + 0.12 * (0.5 + 0.5 * math.cos(2 * math.pi * 2.2 * (t - appear))) ** 4
        return draw_anger(max(8, int(size * pop * throb)), boil), 0.0
    if rule_name == "question":
        tilt = 9 * math.sin(2 * math.pi * 0.9 * (t - appear))
        return draw_question(max(8, int(size * pop)), boil), tilt
    raise ValueError(rule_name)


def sticker_preview(rule_name: str, size: int = 96, t: float = 0.5) -> Image.Image:
    """Still of a sticker for the 「贴纸选择」 picker: transparent, size x size, centred."""
    img, angle = sticker_frame(rule_name, t, 0, size * 0.78, 1.0)
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True)
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(img, ((size - img.width) // 2, (size - img.height) // 2))
    return canvas


def composite(frame: np.ndarray, sticker: Image.Image, center, angle: float = 0.0) -> np.ndarray:
    if angle:
        sticker = sticker.rotate(angle, resample=Image.BICUBIC, expand=True)
    H, W = frame.shape[:2]
    sw, sh = sticker.size
    x = int(round(center[0] - sw / 2))
    y = int(round(center[1] - sh / 2))
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + sw), min(H, y + sh)
    if x1 <= x0 or y1 <= y0:
        return frame
    s = np.asarray(sticker, dtype=np.float32)[y0 - y:y1 - y, x0 - x:x1 - x]
    a = s[..., 3:4] / 255.0
    out = frame.copy()
    region = out[y0:y1, x0:x1].astype(np.float32)
    out[y0:y1, x0:x1] = (region * (1 - a) + s[..., :3] * a + 0.5).astype(np.uint8)
    return out


def sticker_centers(tr: Track, rule: dict, W: int, H: int):
    """Sticker centre / width per frame, plus whether the attach point is on screen."""
    size = rule["size"] * tr.scale
    half = size[:, None] * np.array([0.5, 0.65])[None, :]
    m = 0.25 * tr.scale   # hide once the attach point is within a quarter object-size of the edge
    visible = ((tr.attach[:, 0] > m) & (tr.attach[:, 0] < W - m) & (tr.attach[:, 1] > -m) & (tr.attach[:, 1] < H - m))

    def place(offset):
        c = tr.attach + np.array(offset)[None, :] * tr.scale[:, None]
        fits = ((c - half >= 0) & (c + half <= [W, H])).all(axis=1)
        return c, float(fits[visible].mean()) if visible.any() else 0.0

    centers, fit = place(rule["offset"])
    if fit < 0.8 and "alt_offset" in rule:
        alt, alt_fit = place(rule["alt_offset"])
        if alt_fit > fit:
            centers = alt
    # keep the whole sticker on screen
    centers[:, 0] = np.clip(centers[:, 0], half[:, 0] + 8, W - half[:, 0] - 8)
    centers[:, 1] = np.clip(centers[:, 1], half[:, 1] + 8, H - half[:, 1] - 8)
    return centers, size, visible


def apply_rule(frames: np.ndarray, fps: float, rule_name: str, rule: dict, tr: Track, appear: float = 0.15):
    T, H, W, _ = frames.shape
    centers, sizes, visible = sticker_centers(tr, rule, W, H)
    pops = presence(visible, fps, appear)
    for i in range(T):
        img, ang = sticker_frame(rule_name, i / fps, i, sizes[i], pops[i], appear)
        yield composite(frames[i], img, centers[i], ang) if img is not None else frames[i]


# ----------------------------------------------------------------------------- inspection

def contact_sheet(frames: list[np.ndarray], out: Path, cols: int = 4, width: int = 400, labels=None) -> None:
    thumbs = []
    for i, f in enumerate(frames):
        h = int(round(f.shape[0] * width / f.shape[1]))
        th = cv2.resize(f, (width, h), interpolation=cv2.INTER_AREA)
        if labels:
            cv2.putText(th, labels[i], (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(th, labels[i], (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        thumbs.append(th)
    while len(thumbs) % cols:
        thumbs.append(np.zeros_like(thumbs[0]))
    rows = [np.hstack(thumbs[r:r + cols]) for r in range(0, len(thumbs), cols)]
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), cv2.cvtColor(np.vstack(rows), cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 88])


def debug_frame(frame: np.ndarray, tr: Track, i: int, center) -> np.ndarray:
    f = frame.copy()
    b = tr.boxes[i].astype(int)
    cv2.rectangle(f, (b[0], b[1]), (b[2], b[3]), (60, 220, 60), 2)
    if tr.head_boxes is not None:
        hb = tr.head_boxes[i].astype(int)
        cv2.rectangle(f, (hb[0], hb[1]), (hb[2], hb[3]), (60, 200, 255), 2)
    if not np.isnan(tr.obs[i, 0]):   # yellow = detector inlier, grey = rejected as outlier
        cv2.circle(f, tuple(tr.obs[i].astype(int)), 7, (255, 230, 0) if tr.inlier[i] else (150, 150, 150), -1)
    p = tuple(tr.attach[i].astype(int))
    cv2.drawMarker(f, p, (255, 0, 0), cv2.MARKER_CROSS, 26, 3)
    cv2.circle(f, tuple(np.asarray(center).astype(int)), 5, (255, 0, 255), -1)
    return f


def plot_track(tr: Track, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t = np.arange(len(tr.attach)) / tr.fps
    fig, axes = plt.subplots(2, 1, figsize=(8, 4.5), sharex=True)
    for k, ax in enumerate(axes):
        ax.plot(t[tr.inlier], tr.obs[tr.inlier, k], ".", ms=3, color="#d9a400", label="detector")
        out_ = ~tr.inlier & ~np.isnan(tr.obs[:, 0])
        ax.plot(t[out_], tr.obs[out_, k], "x", ms=3, color="#888888", label="rejected")
        ax.plot(t, tr.flow_path[:, k], "-", lw=1, color="#999999", label="flow only")
        ax.plot(t, tr.attach[:, k], "-", lw=1.6, color="#d62728", label="fused (sticker)")
        ax.set_ylabel("xy"[k] + " (px)")
        ax.invert_yaxis() if k == 1 else None
    axes[0].legend(loc="best", fontsize=8)
    axes[1].set_xlabel("t (s)")
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------- cli

def run(clip: str, rule_name: str, out: Path, start: float = 0.0, dur: float = 4.0,
        anchor=None, offset=None, size=None, appear: float = 0.15, corr_sigma: float = 0.4) -> dict:
    rule = dict(RULES[rule_name])
    if anchor is not None:
        rule["anchor"] = anchor
    if offset is not None:
        rule["offset"] = offset
    if size is not None:
        rule["size"] = size
    frames, fps = read_clip(clip, start, dur)
    T, H, W, _ = frames.shape
    tr = track(frames, fps, rule, corr_sigma)
    rendered = list(apply_rule(frames, fps, rule_name, rule, tr, appear))
    write_clip(rendered, out, fps, W, H)

    centers, sizes, _ = sticker_centers(tr, rule, W, H)
    picks = np.linspace(int(fps * (appear + 0.3)), T - 1, 8).astype(int)
    labels = [f"{i / fps:.2f}s" for i in picks]
    stem = out.with_suffix("")
    contact_sheet([rendered[i] for i in picks], Path(f"{stem}_sheet.jpg"), labels=labels)
    contact_sheet([debug_frame(rendered[i], tr, i, centers[i]) for i in picks],
                  Path(f"{stem}_debug.jpg"), labels=labels)
    plot_track(tr, Path(f"{stem}_track.png"))
    # lock strip: 8 consecutive frames around the fastest motion, cropped on the sticker. If the sticker
    # sticks, the object stays put inside the crops while the background slides.
    speed = np.linalg.norm(np.diff(tr.attach, axis=0), axis=1)
    speed[: int(fps * (appear + 0.3))] = 0
    i0 = int(np.clip(np.argmax(np.convolve(speed, np.ones(8), "same")) - 4, 0, T - 8))
    half = int(min(H, W) * 0.3)
    crops = []
    for i in range(i0, i0 + 8):
        cx, cy = np.clip(centers[i], half, [W - half, H - half]).astype(int)
        crops.append(rendered[i][cy - half:cy + half, cx - half:cx + half])
    contact_sheet(crops, Path(f"{stem}_lock.jpg"), cols=8, width=240,
                  labels=[f"#{i} {speed[min(i, T - 2)]:.0f}px" for i in range(i0, i0 + 8)])
    stats = {"clip": clip, "rule": rule_name, "label": tr.label, "start": start, "fps": round(fps, 3),
             **tr.stats(), "notes": tr.notes}
    Path(f"{stem}.track.json").write_text(json.dumps({
        **stats,
        "sticker_center": np.round(centers, 1).tolist(),
        "sticker_size": np.round(sizes, 1).tolist(),
    }, ensure_ascii=False), encoding="utf-8")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Tracked cartoon stickers on one shot")
    ap.add_argument("--clip", required=True)
    ap.add_argument("--rule", choices=sorted(RULES), required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--dur", type=float, default=4.0)
    ap.add_argument("--anchor", help="x,y attach point in the object box (0..1), eyes rule")
    ap.add_argument("--offset", help="x,y sticker offset in units of object/head size")
    ap.add_argument("--size", type=float, help="sticker width in units of object/head size")
    ap.add_argument("--appear", type=float, default=0.15, help="seconds before the sticker pops in")
    a = ap.parse_args()
    pair = lambda s: tuple(float(v) for v in s.split(",")) if s else None
    stats = run(a.clip, a.rule, Path(a.out), a.start, a.dur, pair(a.anchor), pair(a.offset), a.size, a.appear)
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
