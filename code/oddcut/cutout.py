"""抠像 (cutout) for picture-in-picture: a mask for every frame of part of a clip.

What to cut out (overlay.mask = {"shape": "cutout", "target": ..., "brush": [...], "feather": ...}):
    {"kind": "object", "x": .., "y": ..,   the thing under that point (0..1 of the box) in the frame at t (seconds
     "t": ..}                              from the overlay's start; the first frame without it):
                                           YOLO-seg refined with GrabCut, or a depth blob when YOLO has no name for it
    {"kind": "region", "parts": [...]}     regions of the picture (growth.plan_layers), any of
                                           things (things standing low in front), ground, mid, far, sky;
                                           several together make one cumulative piece ("ground and what is on it")
        band: true                         ...as one solid band up from the bottom of the picture to their top
                                           edge (loose crumbs, gaps and holes below it are filled in)
    {"kind": "lasso", "pts": [[x, y], ...], "t": ..}
                                           the region drawn by hand (钢笔圈选) in the frame at t
extra: [{"op": "add" | "sub", "target": {...}}]
    more selections (智能点选 / 钢笔圈选) added to or taken away from the first, each followed through the clip.
brush: [{"op": "add" | "sub", "r": radius (share of the box's short side), "pts": [[x, y], ...]}]
    strokes painted on top, in box coordinates (they stay put; the cut itself follows the picture).

With MobileSAM there (sam.py) the outlines of the things and the skyline are redrawn by it, and a click on
something YOLO has no name for asks it directly. The first frame is cut, then the cut is carried through the clip along the optical flow and cleaned up (holes
filled, crumbs dropped, edges smoothed) so it stays one whole piece. Results are cached on disk.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

from . import growth, sam

SEG_H = growth.SEG_H
PARTS = ("things", "ground", "mid", "far", "sky")
PART_NAMES = {"things": "前景物体", "ground": "地面", "mid": "中景", "far": "远景", "sky": "天空"}
REV = "1"


def _frames(path: str, start: float, frames: int, fps: float, aspect: float) -> np.ndarray:
    """The clip from `start`, `frames` frames at `fps`, centre-cropped to `aspect`, SEG_H tall (RGB)."""
    w = max(2, int(round(SEG_H * aspect)) // 2 * 2)
    vf = (f"fps={fps},crop=w='min(iw,ih*{aspect:.6f})':h='min(ih,iw/{aspect:.6f})',scale={w}:{SEG_H},"
          "tpad=stop_mode=clone:stop=-1")
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", path, "-vf", vf,
                        "-frames:v", str(frames), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
    n = len(r.stdout) // (w * SEG_H * 3)
    out = np.frombuffer(r.stdout[:n * w * SEG_H * 3], np.uint8).reshape(n, SEG_H, w, 3)
    if n < frames:
        out = np.concatenate([out, np.repeat(out[-1:], frames - n, 0)]) if n else np.zeros((frames, SEG_H, w, 3), np.uint8)
    return out


_ANALYSED: dict = {}


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    return float((a & b).sum() / max(1, (a | b).sum()))


def _inside(m: np.ndarray) -> tuple[int, int]:
    d = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 5)
    y, x = np.unravel_index(int(np.argmax(d)), d.shape)
    return int(x), int(y)


def _sam_things(im, things: list[np.ndarray]) -> list[np.ndarray]:
    """Each thing's outline redrawn by MobileSAM (its box and a point inside it as the prompt); kept as it was
    when MobileSAM sees something else there."""
    h, w = things[0].shape if things else (0, 0)
    out = []
    for m in things:
        ys, xs = np.nonzero(m)
        px, py = int(0.03 * w), int(0.03 * h)
        r = im.box(max(0, xs.min() - px), max(0, ys.min() - py), min(w - 1, xs.max() + px), min(h - 1, ys.max() + py), pos=_inside(m))
        if r is not None and _iou(r, m) >= 0.5:
            r = growth._fill_holes(r)
            for o in out:
                r &= ~o
            if r.mean() >= growth.MIN_THING:
                out.append(r)
                continue
        out.append(m & ~np.logical_or.reduce(out) if out else m)
    return out


def _sam_sky(im, regions: dict) -> dict:
    """The skyline redrawn by MobileSAM: points in the sky to keep, points low in the picture to leave out."""
    sky = regions["sky"]
    if sky.mean() < 0.03:
        return regions
    h, w = sky.shape
    d = cv2.distanceTransform(sky.astype(np.uint8), cv2.DIST_L2, 5)
    pos = []
    for x0 in (0, w // 3, 2 * w // 3):                     # one deep point in each third that has sky
        part = d[:, x0:x0 + w // 3]
        if part.max() > 3:
            y, x = np.unravel_index(int(np.argmax(part)), part.shape)
            pos.append((x0 + x, y))
    low = ~sky & ~regions["things"]
    neg = [(x, y) for x, y in ((w // 4, int(h * 0.9)), (w // 2, int(h * 0.9)), (3 * w // 4, int(h * 0.9))) if low[y, x]]
    for p in ("far", "mid"):                               # and one on the buildings / far part under the sky
        if regions[p].mean() > 0.02:
            neg.append(_inside(regions[p]))
    r = im.points(pos, neg) if pos else None
    if r is None or _iou(r, sky) < 0.5:
        return regions
    r = growth._fill_holes(r) & ~regions["things"]
    out = dict(regions)
    under = next((p for p in ("far", "mid", "ground") if regions[p].any()), "far")
    out[under] = (regions[under] | (sky & ~r)) & ~r
    for p in ("ground", "mid", "far"):
        if p != under:
            out[p] = regions[p] & ~r
    out["sky"] = r
    return out


def analyse(rgb: np.ndarray) -> dict:
    """Things and regions of one frame (SEG_H tall): what the cutout targets choose from."""
    key = hashlib.sha1(rgb.tobytes()).hexdigest()
    if key in _ANALYSED:
        return _ANALYSED[key]
    things = growth._things(rgb)
    depth = growth.depth_map(rgb)
    if depth is not None:
        things = things + growth._depth_things(depth, things)
    with sam.image(rgb) as im:
        if im is not None and things:
            things = _sam_things(im, things)
        own, names, _ = growth.plan_layers(rgb, 5, things, depth)
        regions = {p: np.zeros(rgb.shape[:2], bool) for p in PARTS}
        for m, n in zip(own, names):
            for p in n.split("+"):
                if p in regions:
                    regions[p] |= m
        if im is not None:
            regions = _sam_sky(im, regions)
    info = {"things": things, "regions": regions, "depth": depth, "rgb": rgb}
    if len(_ANALYSED) > 6:
        _ANALYSED.pop(next(iter(_ANALYSED)))
    _ANALYSED[key] = info
    return info


def _clean(m: np.ndarray) -> np.ndarray:
    """One whole piece: smooth edge, holes filled, crumbs (< 0.3% of the picture) dropped."""
    f = cv2.GaussianBlur(m.astype(np.float32), (0, 0), 1.5) > 0.5
    n, lab, st, _ = cv2.connectedComponentsWithStats(f.astype(np.uint8))
    keep = np.zeros_like(f)
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] >= 0.003 * f.size:
            keep |= lab == i
    return growth._fill_holes(keep)


def _band(m: np.ndarray) -> np.ndarray:
    """Everything below the top edge of `m`, column by column: one solid piece standing on the bottom of the
    picture. Crumbs don't count, and the edge is smoothed so poles and gaps make no spikes."""
    h, w = m.shape
    n, lab, st, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8))
    keep = np.zeros_like(m, bool)
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] >= 0.01 * m.size:
            keep |= lab == i
    if not keep.any():
        return keep
    has = keep.any(0)
    top = np.where(has, keep.argmax(0), h).astype(np.float32)
    xs = np.arange(w)
    if not has.all():
        top[~has] = np.interp(xs[~has], xs[has], top[has])
    k = max(3, int(w * 0.04)) | 1
    pad = np.pad(top, k // 2, mode="edge")
    top = np.median(np.lib.stride_tricks.sliding_window_view(pad, k), axis=1).astype(np.float32)
    top = cv2.GaussianBlur(top.reshape(1, -1), (0, 0), sigmaX=max(1.0, w * 0.008)).ravel()
    return np.arange(h)[:, None] >= top[None, :]


def first_mask(info: dict, target: dict, shape) -> np.ndarray:
    h, w = shape
    if target.get("kind") == "lasso":
        pts = np.array([[p[0] * w, p[1] * h] for p in target.get("pts") or []], np.int32)
        img = np.zeros((h, w), np.uint8)
        if len(pts) >= 3:
            cv2.fillPoly(img, [pts], 255, cv2.LINE_AA)
        return img > 127
    if target.get("kind") == "object":
        x, y = int(np.clip(target["x"], 0, 0.999) * w), int(np.clip(target["y"], 0, 0.999) * h)
        hit = [m for m in info["things"] if m[y, x]]
        if not hit and info.get("rgb") is not None:        # something YOLO has no name for: ask MobileSAM
            with sam.image(info["rgb"]) as im:
                r = im.points([(x, y)]) if im is not None else None
            if r is not None and 0.005 <= r.mean() <= 0.7:
                return growth._fill_holes(r)
        if not hit and info["things"]:                     # nearest thing to the click
            ys, xs = np.mgrid[0:h, 0:w]
            hit = [min(info["things"], key=lambda m: np.min(np.hypot(xs[m] - x, ys[m] - y)) if m.any() else 1e9)]
        return hit[0].copy() if hit else np.zeros(shape, bool)
    m = np.zeros(shape, bool)
    for p in target.get("parts") or []:
        if p in info["regions"]:
            m |= info["regions"][p]
    return _band(m) if target.get("band") else m


def _brush(m: np.ndarray, strokes: list[dict]) -> np.ndarray:
    if not strokes:
        return m
    h, w = m.shape
    img = m.astype(np.uint8) * 255
    for s in strokes:
        r = max(1, int(float(s.get("r", 0.04)) * min(h, w)))
        pts = np.array([[p[0] * w, p[1] * h] for p in s.get("pts") or []], np.int32)
        if not len(pts):
            continue
        val = 255 if s.get("op", "add") == "add" else 0
        if len(pts) == 1:
            cv2.circle(img, tuple(pts[0]), r, val, -1)
        else:
            cv2.polylines(img, [pts], False, val, 2 * r, cv2.LINE_AA)
    return img > 127


def key_of(path: str, start: float, frames: int, fps: float, aspect: float, target: dict) -> str:
    from .render import effect_rev                     # the cutting code is part of the key
    st = Path(path).stat()
    return hashlib.sha1(json.dumps([str(Path(path).resolve()), st.st_size, st.st_mtime_ns, round(start, 3), frames, fps,
                                    round(aspect, 4), target, REV, effect_rev("cutout")]).encode()).hexdigest()[:20]


def cutout_masks(path: str, start: float, frames: int, fps: float, aspect: float, target: dict,
                 brush: list | None = None, cache_dir: Path | None = None, extra: list | None = None) -> np.ndarray:
    """Masks (frames x SEG_H x w, bool) of `target` in the clip from `start`, followed along the optical flow,
    with the `extra` selections added / taken away, then the brush strokes."""
    out = _tracked(path, start, frames, fps, aspect, target, cache_dir)
    for e in extra or []:
        m = _tracked(path, start, frames, fps, aspect, e.get("target") or {}, cache_dir)
        out = (out & ~m) if e.get("op") == "sub" else (out | m)
    return np.array([_brush(x, brush) for x in out])


def _tracked(path: str, start: float, frames: int, fps: float, aspect: float, target: dict,
             cache_dir: Path | None = None) -> np.ndarray:
    """One selection, cut in the frame at target.t and followed forwards and backwards through the clip."""
    cached = None
    if cache_dir:
        cached = Path(cache_dir) / f"cut_{key_of(path, start, frames, fps, aspect, target)}.npz"
        if cached.exists():
            try:
                z = np.load(cached)
                n, h, w = int(z["n"]), int(z["h"]), int(z["w"])
                return np.unpackbits(z["m"], axis=-1)[:n, :h, :w].astype(bool)
            except (OSError, ValueError, KeyError):
                pass
    clip = _frames(path, start, frames, fps, aspect)
    n, h, w = clip.shape[:3]
    k0 = min(n - 1, max(0, int(round(float(target.get("t") or 0) * fps))))   # the frame the user clicked on
    info = {"things": [], "regions": {}} if target.get("kind") == "lasso" else analyse(clip[k0])
    m0 = _clean(first_mask(info, target, (h, w)))
    out = np.zeros((n, h, w), bool)
    out[k0] = m0
    for rng in (range(k0 + 1, n), range(k0 - 1, -1, -1)):              # followed forwards, then backwards
        m = m0
        for f in rng:
            if m.any():
                moved = growth._flow_masks(clip[f - 1 if rng.step > 0 else f + 1], clip[f], [m])
                m = _clean(moved[0]) if moved else np.zeros_like(m)
            out[f] = m
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cached, m=np.packbits(out, axis=-1), n=n, h=h, w=w)
    return out


def wholeness(m: np.ndarray) -> dict:
    """How much a mask looks like one hand-cut piece: area, pieces, solidity, smooth edge."""
    area = float(m.mean())
    n, lab, st, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8))
    big = [i for i in range(1, n) if st[i, cv2.CC_STAT_AREA] >= 0.01 * m.size]
    cs, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    hull = sum(cv2.contourArea(cv2.convexHull(c)) for c in cs) or 1.0
    solidity = float(m.sum() / hull)
    perim = sum(cv2.arcLength(c, True) for c in cs)
    rough = float(perim / max(1.0, np.sqrt(m.sum())))       # higher = more ragged outline
    return {"area": round(area, 4), "pieces": len(big), "solidity": round(solidity, 3), "rough": round(rough, 2)}
