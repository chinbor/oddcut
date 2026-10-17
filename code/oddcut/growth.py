"""万物生长 (everything grows): a shot assembles itself out of the previous one.

The incoming shot is cut into 3-4 layers from the bottom up, the way the 剪映 tutorials cut it by hand: the
first layer is the bottom of the picture (the ground and what stands on it), each next one reaches higher, the
last one is the whole picture. The layers are cumulative - each contains everything below its top edge - so
there is never a hole, and the first one to rise is drawn on top: the whole picture slides in last, behind the
pieces already in place. Each layer slides up from below the frame (ease-out), one per beat or half-beat
starting on the cut, with a little vertical motion blur; the previous shot keeps playing underneath until it is
covered (the very first shot grows out of black). Nothing freezes.

Where the layers' top edges run (no new model):
    contours  seams along the strongest horizontal edges (a kerb, the top of a field, roofs), from the bottom up;
              the top one is the skyline when the shot has sky
    things    YOLO-seg instances (people, vehicles, animals, ...) go with the layer their feet are in, whole -
              a person rises with the ground they stand on; they are found again every other frame of the
              rise, so a moving subject stays in its layer
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np

SEG_H = 480                    # masks are worked out at this height and scaled up
MIN_THING = 0.006              # an instance smaller than this share of the picture is not its own layer
MAX_THING = 0.55
SHUTTER = 0.5                  # motion blur: share of a frame the "shutter" is open (x the blur setting)
MAX_BLUR = 0.06                # ... and never longer than this share of the picture height
FEATHER = 1.5                  # px (at SEG_H) of soft edge on every layer
REDETECT = 3                   # things are found again every this many frames during the rise (smaller)


def timing(length: float, beat: float, layers: int = 3, stagger: str = "beat") -> dict:
    """When each layer starts (s from the cut) and how long a rise takes. The first layer starts on the cut
    (which is on a beat); the rest follow every beat or half-beat. Squeezed to fit a short shot.
    Same formula as the app's timeline marker (Timeline.jsx growTiming)."""
    step = beat if stagger == "beat" else beat / 2
    rise = min(0.5, max(0.3, 1.0 * step))
    avail = max(0.3, length - 0.15)
    if (layers - 1) * step + rise > avail:
        step = max(0.08, (avail - rise) / max(1, layers - 1))
        if (layers - 1) * step + rise > avail:
            rise = max(0.15, avail - (layers - 1) * step)
    starts = [round(k * step, 4) for k in range(layers)]
    return {"starts": starts, "rise": round(rise, 4), "land": round(starts[-1] + rise, 4)}


# ----------------------------------------------------------------------------- layers

def _instances(rgb: np.ndarray, imgsz: int = 480) -> list[np.ndarray]:
    """Instance masks (bool, at rgb's size) of things standing in the picture, any class, of a sensible size."""
    from .drawn_reveal import _yolo
    from .sticker_stage import VISION
    h, w = rgb.shape[:2]
    with VISION:
        model = _yolo()
        r = model.predict(np.ascontiguousarray(rgb[..., ::-1]), conf=0.35, imgsz=imgsz, retina_masks=True, verbose=False)[0]
    out = []
    if r.masks is None:
        return out
    for m in r.masks.data.cpu().numpy():
        m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
        if MIN_THING <= m.mean() <= MAX_THING:
            out.append(m)
    return out


def _skyline(rgb: np.ndarray, things: np.ndarray) -> np.ndarray | None:
    """Per column, the row where the sky ends (0 = no sky there); None when the shot has no real sky."""
    h, w = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    grad = cv2.GaussianBlur(np.hypot(cv2.Sobel(gray, cv2.CV_32F, 1, 0), cv2.Sobel(gray, cv2.CV_32F, 0, 1)), (0, 0), 2)
    r, g, b = (rgb[..., k].astype(np.float32) for k in range(3))
    bright, blue = hsv[..., 2] > 140, (b > r + 8) & (b >= g - 5) & (hsv[..., 2] > 70)
    cand = (grad < 14) & (bright | blue) & ~things
    run = np.zeros(w, int)
    for x in range(w):
        col = cand[:, x]
        bad = 0
        y = 0
        for y in range(h):
            bad = bad + 1 if not col[y] else 0
            if bad >= 6:
                y -= 5
                break
        else:
            y = h
        run[x] = y
    line = np.array([np.median(run[max(0, x - 6):x + 7]) for x in range(w)])      # no single-column spikes
    if line.mean() < 0.06 * h or (line > 0.02 * h).mean() < 0.35:
        return None
    return np.clip(line, 0, 0.8 * h).astype(int)


def _seam(rgb: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """A left-to-right path y(x) between rows lo(x)..hi(x) along the strongest horizontal edges (a horizon,
    a kerb, the top of a field), moving at most 2 rows per column."""
    h, w = rgb.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32), (0, 0), 1.5)
    e = np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    e = e / (e.max() + 1e-6)
    rows = np.arange(h)[:, None]
    e = np.where((rows >= lo[None, :]) & (rows <= hi[None, :]), e, -1.0)
    acc = e[:, 0].copy()
    back = np.zeros((h, w), np.int8)
    for x in range(1, w):
        best = np.full(h, -1e9)
        arg = np.zeros(h, np.int8)
        for d in (-2, -1, 0, 1, 2):
            sh = np.roll(acc, d)
            if d > 0:
                sh[:d] = -1e9
            elif d < 0:
                sh[d:] = -1e9
            better = sh - 0.04 * abs(d) > best
            best = np.where(better, sh - 0.04 * abs(d), best)
            arg = np.where(better, d, arg)
        acc = best + e[:, x]
        back[:, x] = arg
    y = int(np.argmax(acc))
    path = np.zeros(w, int)
    for x in range(w - 1, -1, -1):
        path[x] = y
        y = min(h - 1, max(0, y - int(back[y, x])))
    return path


def _follow(old: list[np.ndarray], new: list[np.ndarray]) -> list[np.ndarray]:
    """Things found again in a later frame; one that isn't found this time keeps its last mask, so nothing
    blinks out of its layer for a frame or two."""
    out = list(new)
    for m in old:
        if not any((m & n).sum() > 0.3 * min(m.sum(), n.sum()) for n in new):
            out.append(m)
    return out


def contours(rgb: np.ndarray, n: int, things: list[np.ndarray]) -> list[np.ndarray]:
    """Top edges y(x) of layers 1..n-1 (bottom up), at rgb's size, each above the one before."""
    h, w = rgb.shape[:2]
    union = np.zeros((h, w), bool)
    for m in things:
        union |= m
    sky = _skyline(rgb, union)
    top = sky if sky is not None else np.full(w, int(0.12 * h))
    out = []
    lo_prev = np.full(w, h - 1)
    for k in range(1, n):
        if k == n - 1 and sky is not None:
            c = np.minimum(top, lo_prev - 6)
        else:
            # band around the k-th share of the room between the skyline and the bottom
            span = (lo_prev - top).astype(np.float32)
            share = 1.0 / (n - k) if sky is not None else 1.0 / (n - k + 0.6)
            mid = (lo_prev - span * share).astype(int)
            band = np.maximum(8, (span * 0.22).astype(int))
            c = _seam(rgb, np.clip(mid - band, top, lo_prev - 6), np.clip(mid + band, top + 1, lo_prev - 4))
        c = np.minimum(c, lo_prev - 6)
        out.append(np.clip(c, 0, h - 1))
        lo_prev = out[-1]
    return out


def layer_masks(shape: tuple[int, int], cs: list[np.ndarray], things: list[np.ndarray], out_size=None) -> list[np.ndarray]:
    """Cumulative masks (float32 0..1), rising order: layer k = everything below contour k plus the things
    whose feet are below it (whole); the last layer is the whole picture."""
    h, w = shape
    rows = np.arange(h)[:, None]
    feet = []
    for m in things:
        ys, xs = np.nonzero(m)
        if len(ys):
            y = ys.max()
            x = int(np.median(xs[ys >= y - max(2, h // 60)]))
            feet.append((m, y, x))
    masks = []
    for c in cs:
        m = rows > c[None, :]
        for thing, y, x in feet:
            if y > c[x]:
                m = m | thing
        masks.append(m)
    out = []
    for m in masks:
        f = cv2.GaussianBlur(m.astype(np.float32), (0, 0), FEATHER)
        out.append(cv2.resize(f, out_size, interpolation=cv2.INTER_LINEAR) if out_size else f)
    full = np.ones(out_size[::-1] if out_size else shape, np.float32)
    return out + [full]


def make_layers(rgb: np.ndarray, n: int = 3) -> tuple[list[np.ndarray], list[np.ndarray], list[np.ndarray]]:
    """(masks at rgb's size in rising order, the contours, the things) for the first frame of a shot."""
    H, W = rgb.shape[:2]
    sh, sw = SEG_H, max(2, int(round(W * SEG_H / H)))
    small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
    things = _instances(small)
    cs = contours(small, n, things)
    return layer_masks((sh, sw), cs, things, (W, H)), cs, things


# ----------------------------------------------------------------------------- frames

def _read(path: Path, w: int, h: int, frames: int, start: float = 0.0) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(path), "-frames:v", str(frames),
                        "-vf", f"scale={w}:{h}:in_color_matrix=bt709:in_range=tv", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
    a = np.frombuffer(r.stdout, np.uint8)
    n = len(a) // (w * h * 3)
    out = a[:n * w * h * 3].reshape(n, h, w, 3)
    if n < frames:                               # hold the last frame
        out = np.concatenate([out, np.repeat(out[-1:] if n else np.zeros((1, h, w, 3), np.uint8), frames - n, 0)])
    return out


def _ease(u: float) -> float:
    return 1 - (1 - u) ** 3


def render_grow(new_part: Path, prev_part: Path | None, out: Path, fps: float, w: int, h: int, frames: int,
                tm: dict, layers: int, blur: float, encode: list[str]) -> Path:
    """The incoming shot's rendered part with its opening replaced by the growth: `prev_part` (the previous
    shot carrying on, same look) or black underneath, layers of `new_part` rising over it."""
    n_tr = min(frames, int(np.ceil(tm["land"] * fps)) + 1)
    new = _read(new_part, w, h, frames)
    prev = _read(prev_part, w, h, n_tr) if prev_part else np.zeros((n_tr, h, w, 3), np.uint8)
    masks, cs, things = make_layers(new[0], layers)
    sh, sw = SEG_H, max(2, int(round(w * SEG_H / h)))
    z = list(range(len(masks)))[::-1]            # whole picture (rises last) lowest, first layer on top
    starts, rise = tm["starts"], tm["rise"]

    def off(t: float, k: int) -> float | None:
        u = (t - starts[k]) / rise
        return None if u < 0 else (1 - _ease(min(1.0, u))) * h

    def prepare(ms):
        m3 = [cv2.merge([m, m, m]) for m in ms]
        spans = []                               # rows each layer covers (only those are worked on)
        for m in ms:
            rows = np.nonzero(m.max(1) > 1e-3)[0]
            spans.append((int(rows[0]), int(rows[-1]) + 1) if len(rows) else (0, 0))
        return m3, spans

    m3, spans = prepare(masks)
    proc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                             "-r", str(fps), "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", *encode, str(out)],
                            stdin=subprocess.PIPE)
    try:
        for f in range(frames):
            if f >= n_tr:
                proc.stdin.write(new[f].tobytes())
                continue
            t = f / fps
            if f and f % REDETECT == 0 and f < n_tr - 1:      # moving things stay in their layer
                small = cv2.resize(new[f], (sw, sh), interpolation=cv2.INTER_AREA)
                things = _follow(things, _instances(small, 320))
                m3, spans = prepare(layer_masks((sh, sw), cs, things, (w, h)))
            acc = prev[f].astype(np.float32)
            frame = new[f].astype(np.float32)
            for k in z:
                dy = off(t, k)
                if dy is None:
                    continue
                v = abs((off(t + 0.5 / fps, k) or 0) - dy) * 2 * fps if dy > 0.5 else 0.0
                klen = int(min(v / fps * SHUTTER * blur, MAX_BLUR * h * blur)) | 1
                r0, r1 = spans[k]
                r0, r1 = max(0, r0 - klen), min(h, r1 + klen)
                o0 = r0 + int(round(dy))
                o1 = min(h, r1 + int(round(dy)))
                if o0 >= o1:
                    continue
                n = o1 - o0                          # rows of the layer that are inside the frame
                col = cv2.multiply(frame[r0:r0 + n], m3[k][r0:r0 + n])     # premultiplied
                a = m3[k][r0:r0 + n]
                if klen >= 3:                        # vertical motion blur, as long as the move in a frame
                    col, a = cv2.blur(col, (1, klen)), cv2.blur(a, (1, klen))
                acc[o0:o1] = cv2.add(cv2.multiply(acc[o0:o1], 1.0 - a), col)
            proc.stdin.write(np.clip(acc, 0, 255).astype(np.uint8).tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise RuntimeError("ffmpeg failed while encoding the growth transition")
    return out
