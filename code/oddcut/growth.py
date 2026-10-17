"""万物生长 (everything grows): a shot assembles itself out of the previous one.

The incoming shot is cut into 3-4 layers along what is in it - things standing in the picture (people,
vehicles, animals: YOLO instance masks), the ground, the middle of the scene, the sky - and the layers slide up
into place from below the frame one after another, front first and sky last, each one starting on a beat (or
half-beat) of the music, fast then settling (ease-out), with vertical motion blur while they move. Underneath,
the previous shot keeps playing until it is covered; the very first shot grows out of black. Both shots keep
playing throughout (nothing freezes).

Layers without a new model:
    things  YOLO-seg instances (any class) of a sensible size
    sky     from the top of each column down while the picture stays smooth and bright / blue
    ground  below a seam that follows the strongest horizontal edges in the lower part of the picture
    mid     the rest (split by a second seam when a 4th layer is needed and there is no sky or no things)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np

SEG_H = 480                    # masks are worked out at this height and scaled up
MIN_THING = 0.006              # an instance smaller than this share of the picture is not its own layer
MAX_THING = 0.55
SHUTTER = 1.0                  # motion blur: share of a frame the "shutter" is open (x the blur setting)


def timing(length: float, beat: float, layers: int = 3, stagger: str = "beat") -> dict:
    """When each layer starts (s from the cut) and how long a rise takes. The first layer starts on the cut
    (which is on a beat); the rest follow every beat or half-beat. Squeezed to fit a short shot.
    Same formula as the app's timeline marker (Timeline.jsx growTiming)."""
    step = beat if stagger == "beat" else beat / 2
    rise = min(0.45, max(0.25, 0.9 * step))
    avail = max(0.3, length - 0.15)
    if (layers - 1) * step + rise > avail:
        step = max(0.08, (avail - rise) / max(1, layers - 1))
        if (layers - 1) * step + rise > avail:
            rise = max(0.15, avail - (layers - 1) * step)
    starts = [round(k * step, 4) for k in range(layers)]
    return {"starts": starts, "rise": round(rise, 4), "land": round(starts[-1] + rise, 4)}


# ----------------------------------------------------------------------------- layers

def _things(rgb: np.ndarray) -> np.ndarray:
    """Union of instance masks of things standing in the picture (bool, at rgb's size)."""
    from .drawn_reveal import _yolo
    from .sticker_stage import VISION
    h, w = rgb.shape[:2]
    with VISION:
        model = _yolo()
        r = model.predict(np.ascontiguousarray(rgb[..., ::-1]), conf=0.3, imgsz=640, retina_masks=True, verbose=False)[0]
    out = np.zeros((h, w), bool)
    if r.masks is None:
        return out
    for m in r.masks.data.cpu().numpy():
        m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
        if MIN_THING <= m.mean() <= MAX_THING:
            out |= m
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


def make_layers(rgb: np.ndarray, n: int = 3) -> tuple[list[np.ndarray], list[str]]:
    """The incoming shot's first frame split into `n` layers. Returns (masks float32 0..1 at rgb's size, in
    the order they rise: front first, sky last; names). The masks add up to 1 everywhere."""
    H, W = rgb.shape[:2]
    sh, sw = SEG_H, max(2, int(round(W * SEG_H / H)))
    small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
    things = _things(small)
    sky = _skyline(small, things)
    sky_line = sky if sky is not None else np.zeros(sw, int)
    lo = np.maximum(sky_line + int(0.06 * sh), int(0.5 * sh))
    hi = np.full(sw, int(0.88 * sh))
    ground_seam = _seam(small, np.minimum(lo, hi - 4), hi)
    rows = np.arange(sh)[:, None]
    is_sky = rows < sky_line[None, :]
    is_ground = rows > ground_seam[None, :]
    is_mid = ~is_sky & ~is_ground
    regions = {"things": things, "ground": is_ground & ~things, "mid": is_mid & ~things, "sky": is_sky & ~things}
    order = ["things", "ground", "mid", "sky"]
    present = [k for k in order if regions[k].mean() > 0.01]
    # too few: split the middle (or the ground) by a second seam; too many: things ride with the ground
    while len(present) < n:
        splittable = [k for k in present if k not in ("things", "sky")]
        if not splittable:
            break
        big = max(splittable, key=lambda k: regions[k].mean())
        m = regions[big]
        top = np.array([np.argmax(m[:, x]) if m[:, x].any() else sh // 2 for x in range(sw)])
        bot = np.array([sh - 1 - np.argmax(m[::-1, x]) if m[:, x].any() else sh // 2 for x in range(sw)])
        mid_lo, mid_hi = top + (bot - top) // 4, bot - (bot - top) // 4
        s2 = _seam(small, np.minimum(mid_lo, mid_hi - 2), np.maximum(mid_hi, mid_lo + 2))
        upper = m & (rows < s2[None, :])
        lower = m & ~upper
        if upper.mean() < 0.01 or lower.mean() < 0.01:
            break
        regions[big + "_low"], regions[big + "_high"] = lower, upper
        del regions[big]
        i = order.index(big)
        order[i:i + 1] = [big + "_low", big + "_high"]
        present = [k for k in order if k in regions and regions[k].mean() > 0.01]
    while len(present) > n:
        a, b = present[0], present[1]            # merge the two front-most
        regions[a] = regions[a] | regions[b]
        del regions[b]
        present.remove(b)
    label = np.zeros((sh, sw), np.int32)
    for k, name in enumerate(present):
        label[regions[name]] = k + 1
    if (label == 0).any():                       # stray pixels: nearest layer below them
        miss = label == 0
        label[miss] = cv2.dilate(label.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(np.int32)[miss]
        label[label == 0] = len(present)
    masks = []
    for k in range(len(present)):
        m = cv2.GaussianBlur((label == k + 1).astype(np.float32), (0, 0), 1.2)
        masks.append(cv2.resize(m, (W, H), interpolation=cv2.INTER_LINEAR))
    total = np.maximum(1e-6, sum(masks))
    return [m / total for m in masks], present


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
    masks, _ = make_layers(new[0], layers)
    z = list(range(len(masks)))[::-1]            # sky (risen last) lowest, things (risen first) on top
    starts, rise = tm["starts"], tm["rise"]

    def off(t: float, k: int) -> float | None:
        u = (t - starts[k]) / rise
        return None if u < 0 else (1 - _ease(min(1.0, u))) * h

    m3 = [cv2.merge([m, m, m]) for m in masks]
    spans = []                                   # rows each layer covers (only those are worked on)
    for m in masks:
        rows = np.nonzero(m.max(1) > 1e-3)[0]
        spans.append((int(rows[0]), int(rows[-1]) + 1) if len(rows) else (0, 0))
    proc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                             "-r", str(fps), "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", *encode, str(out)],
                            stdin=subprocess.PIPE)
    try:
        for f in range(frames):
            if f >= n_tr:
                proc.stdin.write(new[f].tobytes())
                continue
            t = f / fps
            acc = prev[f].astype(np.float32)
            frame = new[f].astype(np.float32)
            for k in z:
                dy = off(t, k)
                if dy is None:
                    continue
                v = abs((off(t + 0.5 / fps, k) or 0) - dy) * 2 * fps if dy > 0.5 else 0.0
                klen = int(v / fps * SHUTTER * blur) | 1
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
