"""抠像 (cutout) for picture-in-picture: a mask for every frame of part of a clip.

What to cut out (overlay.mask = {"shape": "cutout", "target": ..., "brush": [...], "feather": ...}):
    {"kind": "object", "x": .., "y": ..}   the thing under that point (0..1 of the box) in the first frame:
                                           YOLO-seg refined with GrabCut, or a depth blob when YOLO has no name for it
    {"kind": "region", "parts": [...]}     regions of the picture (growth.plan_layers), any of
                                           things (things standing low in front), ground, mid, far, sky;
                                           several together make one cumulative piece ("ground and what is on it")
brush: [{"op": "add" | "sub", "r": radius (share of the box's short side), "pts": [[x, y], ...]}]
    strokes painted on top, in box coordinates (they stay put; the cut itself follows the picture).

The first frame is cut, then the cut is carried through the clip along the optical flow and cleaned up (holes
filled, crumbs dropped, edges smoothed) so it stays one whole piece. Results are cached on disk.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

from . import growth

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


def analyse(rgb: np.ndarray) -> dict:
    """Things and regions of one frame (SEG_H tall): what the cutout targets choose from."""
    things = growth._things(rgb)
    depth = growth.depth_map(rgb)
    if depth is not None:
        things = things + growth._depth_things(depth, things)
    own, names, _ = growth.plan_layers(rgb, 5, things, depth)
    regions = {p: np.zeros(rgb.shape[:2], bool) for p in PARTS}
    for m, n in zip(own, names):
        for p in n.split("+"):
            if p in regions:
                regions[p] |= m
    return {"things": things, "regions": regions, "depth": depth}


def _clean(m: np.ndarray) -> np.ndarray:
    """One whole piece: smooth edge, holes filled, crumbs (< 0.3% of the picture) dropped."""
    f = cv2.GaussianBlur(m.astype(np.float32), (0, 0), 1.5) > 0.5
    n, lab, st, _ = cv2.connectedComponentsWithStats(f.astype(np.uint8))
    keep = np.zeros_like(f)
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] >= 0.003 * f.size:
            keep |= lab == i
    return growth._fill_holes(keep)


def first_mask(info: dict, target: dict, shape) -> np.ndarray:
    h, w = shape
    if target.get("kind") == "object":
        x, y = int(np.clip(target["x"], 0, 0.999) * w), int(np.clip(target["y"], 0, 0.999) * h)
        hit = [m for m in info["things"] if m[y, x]]
        if not hit and info["things"]:                     # nearest thing to the click
            ys, xs = np.mgrid[0:h, 0:w]
            hit = [min(info["things"], key=lambda m: np.min(np.hypot(xs[m] - x, ys[m] - y)) if m.any() else 1e9)]
        return hit[0].copy() if hit else np.zeros(shape, bool)
    m = np.zeros(shape, bool)
    for p in target.get("parts") or []:
        if p in info["regions"]:
            m |= info["regions"][p]
    return m


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
                 brush: list | None = None, cache_dir: Path | None = None) -> np.ndarray:
    """Masks (frames x SEG_H x w, bool) of `target` in the clip from `start`, followed along the optical flow."""
    cached = None
    if cache_dir:
        cached = Path(cache_dir) / f"cut_{key_of(path, start, frames, fps, aspect, target)}.npz"
        if cached.exists():
            try:
                z = np.load(cached)
                n, h, w = int(z["n"]), int(z["h"]), int(z["w"])
                masks = np.unpackbits(z["m"], axis=-1)[:n, :h, :w].astype(bool)
                return np.array([_brush(x, brush) for x in masks])
            except (OSError, ValueError, KeyError):
                pass
    clip = _frames(path, start, frames, fps, aspect)
    n, h, w = clip.shape[:3]
    m = _clean(first_mask(analyse(clip[0]), target, (h, w)))
    out = np.zeros((n, h, w), bool)
    out[0] = m
    for f in range(1, n):
        if m.any():
            moved = growth._flow_masks(clip[f - 1], clip[f], [m])
            m = _clean(moved[0]) if moved else np.zeros_like(m)
        out[f] = m
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cached, m=np.packbits(out, axis=-1), n=n, h=h, w=w)
    return np.array([_brush(x, brush) for x in out])


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
