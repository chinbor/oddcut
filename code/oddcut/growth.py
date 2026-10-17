"""万物生长 (everything grows): a shot assembles itself out of the previous one.

As in the 剪映 tutorials, the picture is cut into 3-4 layers of COMPLETE things - a whole person, car, cat or
cup, the ground, the band of buildings / trees, the sky - and the layers slide up into place from below the frame
one after another (front first, sky last), each starting on a beat or half-beat, fast then settling, with a
little vertical motion blur. The previous shot keeps playing in the parts not yet covered (the very first shot
grows out of black); nothing freezes.

Cutting (no extra model):
    things  YOLO-seg instances of any class, refined with GrabCut seeded from the YOLO mask, holes filled,
            only the largest piece kept: one clean, complete silhouette per object; they rise together, first
    stuff   the ground below a seam along strong horizontal edges, the band of buildings / trees above it, the
            sky from the top of each column down (its lower edge is the skyline); a seam never cuts a thing
With the depth model (assets/models/depth_anything_v2_small.onnx, Depth-Anything-V2-Small, Apache-2.0, run by
OpenCV on the CPU, ~0.2 s a shot) the sky is the far region joined to the top, the seams also follow depth
edges (building bases, treelines), and clearly nearer blobs YOLO doesn't know (a pole, a tree, a bench) become
things too. Without the file everything works as before.
A layer that rises behind things already in place carries, where those things are, a smeared copy of the
picture (it reads as motion), so it never shows a hole or a sharp second copy of them while it moves. Things
are followed through the rise with optical flow, so a moving subject stays one piece.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import cv2
import numpy as np

SEG_H = 480                    # masks are worked out at this height and scaled up
MIN_THING = 0.006              # an instance smaller than this share of the picture is not its own layer
MAX_THING = 0.55
SHUTTER = 0.5                  # motion blur: share of a frame the "shutter" is open (x the blur setting)
MAX_BLUR = 0.06                # ... and never longer than this share of the picture height
FEATHER = 1.2                  # px (at SEG_H) of soft edge on every layer
FLOW_EVERY = 2                 # things are moved along the optical flow every this many frames of the rise


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


_DEPTH: dict = {}
_DEPTH_LOCK = threading.Lock()
DEPTH_SIZE = 392                 # model input (a multiple of 14); 0.16 s on a laptop CPU


def depth_map(rgb: np.ndarray) -> np.ndarray | None:
    """Relative nearness of every pixel (0 far .. 1 near, at rgb's size) from Depth-Anything-V2-Small, or None
    when the model file isn't there (or can't run)."""
    from .paths import MODEL_DIR
    path = MODEL_DIR / "depth_anything_v2_small.onnx"
    with _DEPTH_LOCK:
        if "net" not in _DEPTH:
            try:
                _DEPTH["net"] = cv2.dnn.readNetFromONNX(str(path)) if path.exists() else None
            except cv2.error:
                _DEPTH["net"] = None
        net = _DEPTH["net"]
        if net is None:
            return None
        x = cv2.resize(rgb, (DEPTH_SIZE, DEPTH_SIZE), interpolation=cv2.INTER_CUBIC).astype(np.float32) / 255
        x = (x - np.float32([0.485, 0.456, 0.406])) / np.float32([0.229, 0.224, 0.225])
        net.setInput(np.ascontiguousarray(x.transpose(2, 0, 1)[None]))
        try:
            d = net.forward().reshape(DEPTH_SIZE, DEPTH_SIZE)
        except cv2.error:
            return None
    d = cv2.resize(d, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)
    lo, hi = np.percentile(d, 2), np.percentile(d, 98)
    return np.clip((d - lo) / max(1e-6, hi - lo), 0, 1).astype(np.float32)


def _sky_from_depth(depth: np.ndarray, things: np.ndarray) -> np.ndarray | None:
    """Skyline: per column, down from the top while the picture stays far away."""
    h, w = depth.shape
    far = (depth < 0.05) & ~things
    run = np.zeros(w, int)
    for x in range(w):
        col = far[:, x]
        bad = np.nonzero(~col)[0]
        run[x] = bad[0] if len(bad) else h
    line = np.array([np.median(run[max(0, x - 6):x + 7]) for x in range(w)])
    if line.mean() < 0.06 * h or (line > 0.02 * h).mean() < 0.35:
        return None
    return np.clip(line, 0, 0.8 * h).astype(int)


def _depth_things(depth: np.ndarray, things: list[np.ndarray]) -> list[np.ndarray]:
    """Blobs clearly nearer than what is around them (a pole, a tree, a bench) that YOLO didn't find."""
    h, w = depth.shape
    k = max(3, int(0.12 * min(h, w)) | 1)
    around = cv2.blur(depth, (k, k))
    near = ((depth - around > 0.10) & (depth > 0.3)).astype(np.uint8)
    near = cv2.morphologyEx(near, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    union = np.zeros((h, w), bool)
    for m in things:
        union |= m
    n, lab, st, _ = cv2.connectedComponentsWithStats(near)
    out = []
    for i in range(1, n):
        m = lab == i
        area = m.mean()
        if not (0.004 <= area <= 0.3 and (m & union).sum() < 0.3 * m.sum() and st[i, cv2.CC_STAT_HEIGHT] > 0.08 * h):
            continue
        bw_, bh_ = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        thick = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 3).max()
        if m.sum() < 0.3 * bw_ * bh_ or thick < 0.02 * min(h, w):      # wires, grass blades, spokes: not things
            continue
        out.append(_fill_holes(m & ~union))
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


def _seam(rgb: np.ndarray, lo: np.ndarray, hi: np.ndarray, depth: np.ndarray | None = None) -> np.ndarray:
    """A left-to-right path y(x) between rows lo(x)..hi(x) along the strongest horizontal edges (a horizon,
    a kerb, the top of a field) - and depth steps, when there is a depth map - moving at most 2 rows per column."""
    h, w = rgb.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32), (0, 0), 1.5)
    e = np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    e = e / (np.percentile(e, 99.5) + 1e-6)
    if depth is not None:
        dz = np.abs(cv2.Sobel(cv2.GaussianBlur(depth, (0, 0), 1.0), cv2.CV_32F, 0, 1, ksize=3))
        e = np.minimum(e, 1.0) + 1.5 * np.minimum(dz / (np.percentile(dz, 99.5) + 1e-6), 1.0)
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


def _refine(rgb: np.ndarray, m: np.ndarray) -> np.ndarray | None:
    """A YOLO mask made into one clean, complete silhouette: GrabCut seeded from it (sure inside its eroded
    core, maybe inside the mask, maybe outside a ring around it, outside beyond), holes filled, largest piece."""
    h, w = m.shape
    k = max(3, int(0.012 * min(h, w)) | 1)
    core = cv2.erode(m.astype(np.uint8), np.ones((k, k), np.uint8), iterations=2) > 0
    ring = cv2.dilate(m.astype(np.uint8), np.ones((k, k), np.uint8), iterations=3) > 0
    gc = np.full((h, w), cv2.GC_BGD, np.uint8)
    gc[ring] = cv2.GC_PR_BGD
    gc[m] = cv2.GC_PR_FGD
    gc[core] = cv2.GC_FGD
    ys, xs = np.nonzero(ring)
    if not len(ys) or not core.any():
        return None
    y0, y1, x0, x1 = max(0, ys.min() - 4), min(h, ys.max() + 5), max(0, xs.min() - 4), min(w, xs.max() + 5)
    sub = gc[y0:y1, x0:x1].copy()
    try:
        cv2.grabCut(np.ascontiguousarray(rgb[y0:y1, x0:x1, ::-1]), sub, None, np.zeros((1, 65), np.float64),
                    np.zeros((1, 65), np.float64), 3, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return m
    out = np.zeros((h, w), bool)
    out[y0:y1, x0:x1] = (sub == cv2.GC_FGD) | (sub == cv2.GC_PR_FGD)
    n, lab, st, _ = cv2.connectedComponentsWithStats(out.astype(np.uint8))
    if n < 2:
        return None
    out = lab == 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    inter = (out & m).sum()
    if out.sum() > 1.4 * m.sum() or inter < 0.75 * m.sum():     # GrabCut leaked into a look-alike background
        out = m.copy()
    return _fill_holes(cv2.morphologyEx(out.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)) > 0)


def _fill_holes(m: np.ndarray) -> np.ndarray:
    """m with every hole filled (whatever the outside can't reach)."""
    h, w = m.shape
    flood = np.pad(m.astype(np.uint8), 1)
    cv2.floodFill(flood, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 2)
    return m | (flood[1:-1, 1:-1] == 0)


def _things(rgb: np.ndarray) -> list[np.ndarray]:
    """Complete silhouettes of the things in the picture, largest first, no two sharing pixels."""
    out = []
    for m in sorted(_instances(rgb), key=lambda m: -m.sum()):
        r = _refine(rgb, m)
        if r is None or r.mean() < MIN_THING:
            continue
        for o in out:
            r &= ~o
        if r.mean() >= MIN_THING:
            out.append(r)
    return out


def _flow_masks(prev_rgb: np.ndarray, rgb: np.ndarray, masks: list[np.ndarray]) -> list[np.ndarray]:
    """Things moved along the optical flow from one frame to the next (they stay whole)."""
    g0, g1 = (cv2.cvtColor(x, cv2.COLOR_RGB2GRAY) for x in (prev_rgb, rgb))
    flow = cv2.calcOpticalFlowFarneback(g1, g0, None, 0.5, 3, 21, 3, 5, 1.1, 0)   # where each pixel came from
    h, w = g0.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    mx, my = gx + flow[..., 0], gy + flow[..., 1]
    moved = [cv2.remap(m.astype(np.float32), mx, my, cv2.INTER_LINEAR) > 0.5 for m in masks]
    return [m for m in moved if m.any()]


def plan_layers(rgb: np.ndarray, n: int, things: list[np.ndarray], depth: np.ndarray | None = None
                ) -> tuple[list[np.ndarray], list[str], tuple]:
    """Own pixels of each layer (bool, disjoint, covering the picture), in rising order, their names, and the
    seams (ground seam, skyline) so the same cut can be redrawn when things move."""
    h, w = rgb.shape[:2]
    union = np.zeros((h, w), bool)
    for m in things:
        union |= m
    sky = _sky_from_depth(depth, union) if depth is not None else None
    if sky is None:
        sky = _skyline(rgb, union)
    sky_line = sky if sky is not None else np.zeros(w, int)
    lo = np.maximum(sky_line + int(0.06 * h), int(0.5 * h))
    hi = np.full(w, int(0.9 * h))
    ground_seam = _seam(rgb, np.minimum(lo, hi - 4), hi, depth)
    extra = None
    if sky is None or n >= 4:                                   # a second seam splits the middle band
        top = np.maximum(sky_line + 4, int(0.08 * h))
        extra = _seam(rgb, np.minimum(top + (ground_seam - top) // 4, ground_seam - 8),
                      np.maximum(ground_seam - (ground_seam - top) // 4, top + 8), depth)
    return _assemble((h, w), n, things, ground_seam, sky_line if sky is not None else None, extra)


def _assemble(shape, n, things, ground_seam, sky_line, extra):
    """Own pixels per layer, front / low first. Each thing goes, whole, with the region its bottom edge is in:
    standing on the ground it rises first (or with the ground), a lantern hanging high rises with the top."""
    h, w = shape
    rows = np.arange(h)[:, None]
    union = np.zeros((h, w), bool)
    for m in things:
        union |= m
    ground = (rows > ground_seam[None, :]) & ~union
    sky = (rows < sky_line[None, :]) & ~union if sky_line is not None else np.zeros((h, w), bool)
    mid = ~union & ~ground & ~sky
    split = extra is not None and (n >= 4 or sky_line is None)
    far = mid & (rows < extra[None, :]) if split else np.zeros((h, w), bool)
    mid = mid & ~far
    low, high = np.zeros((h, w), bool), {"mid": np.zeros((h, w), bool), "far": np.zeros((h, w), bool), "sky": np.zeros((h, w), bool)}
    for m in things:
        ys, xs = np.nonzero(m)
        if not len(ys):                                         # moved out of the picture
            continue
        y = int(ys.max())
        x = int(np.median(xs[ys >= y - max(2, h // 60)]))
        if y > ground_seam[x] - h // 40:
            low |= m
        elif split and y < extra[x]:
            high["far"] |= m
        elif sky_line is not None and y < sky_line[x]:
            high["sky"] |= m
        else:
            high["mid"] |= m
    parts = [("things", low), ("ground", ground), ("mid", mid | high["mid"])]
    if split:
        parts.append(("far", far | high["far"]))
    if sky_line is not None:
        parts.append(("sky", sky | high["sky"]))
    parts = [(nm, m) for nm, m in parts if m.mean() > 0.004]
    while len(parts) > n:                                       # too many: join the two front-most
        (a, ma), (b, mb) = parts[0], parts[1]
        parts[:2] = [(f"{a}+{b}", ma | mb)]
    seams = (ground_seam, sky_line, extra)
    return [m for _, m in parts], [nm for nm, _ in parts], seams


def supports(own: list[np.ndarray]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """What each layer carries while it rises: its own pixels plus, below its top edge, the pixels of the
    layers in front of it (to be filled with the picture behind them). Returns (support, filled) masks."""
    h, w = own[0].shape
    rows = np.arange(h)[:, None]
    front = np.zeros((h, w), bool)
    sup, fill = [], []
    for m in own:
        cols = m.any(0)
        top = np.where(cols, m.argmax(0), h)
        top = np.array([top[max(0, x - 4):x + 5].min() for x in range(w)])     # bridge thin gaps
        f = front & (rows >= top[None, :])
        sup.append(m | f)
        fill.append(f)
        front |= m
    return sup, fill


def make_layers(rgb: np.ndarray, n: int = 3):
    """Everything the render needs for a shot's first frame (worked out at SEG_H)."""
    H, W = rgb.shape[:2]
    sh, sw = SEG_H, max(2, int(round(W * SEG_H / H)))
    small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
    things = _things(small)
    depth = depth_map(small)
    if depth is not None:
        things = things + _depth_things(depth, things)
    own, names, seams = plan_layers(small, n, things, depth)
    return small, things, own, names, seams


def _feathered(masks: list[np.ndarray], size) -> list[np.ndarray]:
    return [cv2.resize(cv2.GaussianBlur(m.astype(np.float32), (0, 0), FEATHER), size, interpolation=cv2.INTER_LINEAR)
            for m in masks]


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
    small, things, own, names, seams = make_layers(new[0], layers)
    sh, sw = small.shape[:2]
    n_layers = len(own)
    blur_sigma = 0.03 * h                         # behind things in front: a smeared copy (reads as motion)
    z = list(range(n_layers))[::-1]              # the first layer to rise is drawn on top
    starts, rise = tm["starts"], tm["rise"]

    def off(t: float, k: int) -> float | None:
        u = (t - starts[k]) / rise
        return None if u < 0 else (1 - _ease(min(1.0, u))) * h

    def prepare(own_masks):
        sup, fill = supports(own_masks)
        a = _feathered(sup, (w, h))
        fl = _feathered(fill, (w, h))
        m3 = [cv2.merge([m, m, m]) for m in a]
        f3 = [cv2.merge([m, m, m]) if m.max() > 1e-3 else None for m in fl]
        spans = []                               # rows each layer covers (only those are worked on)
        for m in a:
            rows = np.nonzero(m.max(1) > 1e-3)[0]
            spans.append((int(rows[0]), int(rows[-1]) + 1) if len(rows) else (0, 0))
        return m3, f3, spans

    m3, f3, spans = prepare(own)
    prev_small = small
    proc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                             "-r", str(fps), "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", *encode, str(out)],
                            stdin=subprocess.PIPE)
    try:
        for f in range(frames):
            if f >= n_tr:
                proc.stdin.write(new[f].tobytes())
                continue
            t = f / fps
            if things and f % FLOW_EVERY == 0 and 0 < f < n_tr - 1:   # moving things stay whole, in their layer
                small = cv2.resize(new[f], (sw, sh), interpolation=cv2.INTER_AREA)
                things = _flow_masks(prev_small, small, things)
                prev_small = small
                own, _, _ = _assemble((sh, sw), layers, things, *seams)
                if len(own) == n_layers:
                    m3, f3, spans = prepare(own)
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
                src = frame[r0:r0 + n]
                if f3[k] is not None:                    # behind the things in front: a smeared copy of them
                    tiny = cv2.resize(src, (max(1, w // 8), max(1, n // 8)), interpolation=cv2.INTER_AREA)
                    smear = cv2.resize(cv2.GaussianBlur(tiny, (0, 0), blur_sigma / 8), (w, n), interpolation=cv2.INTER_LINEAR)
                    src = src * (1 - f3[k][r0:r0 + n]) + smear * f3[k][r0:r0 + n]
                col = cv2.multiply(src, m3[k][r0:r0 + n])                  # premultiplied
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
