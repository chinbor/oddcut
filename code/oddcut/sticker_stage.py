"""Glue between the edit pipeline and oddcut.stickers: tag clips, pick rules, render sticker shots.

Heavy deps (torch / ultralytics / cv2) are imported lazily so the pipeline still runs without them.
"""
from pathlib import Path

import numpy as np

from .analyze import ClipAnalysis
import cv2

from .ffmpeg_utils import probe, read_frames

import threading

VISION = threading.RLock()   # YOLO / YOLOE models are shared module globals: one user at a time

TAG_FPS = 1.0
MIN_AREA = 0.04          # object must cover >= 4% of the (output-aspect-cropped) frame to be worth a sticker
# share of the object's box that must survive the crop: doodles sit above an animal's head, so the
# whole animal must be in view (a face close-up has its head top cropped away); eyes sit mid-body.
MIN_INSIDE = {"animal": 0.85, "object": 0.6}
APPEAR = 0.1             # seconds after the cut before the sticker pops in


def available() -> bool:
    try:
        import cv2, ultralytics  # noqa: F401
        return True
    except ImportError:
        return False


def _center_crop_box(w: int, h: int, aspect: float) -> tuple[int, int, int, int]:
    """x0, y0, cw, ch of the centred crop with width/height = aspect (what render keeps)."""
    if w / h > aspect:
        cw = int(round(h * aspect)) // 2 * 2
        return (w - cw) // 2, 0, cw, h
    ch = int(round(w / aspect)) // 2 * 2
    return 0, (h - ch) // 2, w, ch


def tag_clips(clips: list[ClipAnalysis], aspect: float, on_clip=None) -> None:
    """1 fps YOLO pass per clip. Fills clip.tags with (t, family, name, area) or None per sample,
    measured inside the crop the edit will keep (objects mostly cut off by the crop don't count)."""
    from ultralytics import YOLO
    from .stickers import ANIMALS, FRONT_OBJECTS, OBJECT_MODEL, _in_model_dir
    with _in_model_dir():
        model = YOLO(OBJECT_MODEL)
    wanted = set(ANIMALS) | set(FRONT_OBJECTS)
    ids = [i for i, n in model.names.items() if n in wanted]
    for n, c in enumerate(clips):
        if on_clip:
            on_clip(n, len(clips), c)
        if c.rejected:
            continue
        info = probe(c.path)
        frames = read_frames(c.path, info, TAG_FPS, width=640)
        if len(frames) == 0:
            continue
        h, w = frames.shape[1:3]
        x0, y0, cw, ch = _center_crop_box(w, h, aspect)
        tags = []
        results = model.predict([np.ascontiguousarray(f[..., ::-1]) for f in frames],
                                classes=ids, conf=0.35, imgsz=640, verbose=False)
        for i, r in enumerate(results):
            best = None
            for b, k in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.cls.cpu().numpy()):
                bx0, by0 = max(b[0], x0), max(b[1], y0)
                bx1, by1 = min(b[2], x0 + cw), min(b[3], y0 + ch)
                kept = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
                full = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
                area = kept / (cw * ch)
                name = model.names[int(k)]
                family = "animal" if name in ANIMALS else "object"
                if area >= MIN_AREA and kept / full >= MIN_INSIDE[family] and (best is None or area > best[3]):
                    best = (i / TAG_FPS, family, name, float(area))
            tags.append(best)
        c.tags = tags


def presence(clip: ClipAnalysis, start: float, length: float) -> tuple[str, str] | None:
    """(family, name) if the same sticker-able object is seen at every tag sample in the window."""
    if not clip.tags:
        return None
    i0 = int(np.floor(start * TAG_FPS))
    i1 = int(np.ceil((start + length) * TAG_FPS))
    window = clip.tags[max(0, i0): min(len(clip.tags), i1 + 1)]
    if not window or any(t is None for t in window):
        return None
    families = {t[1] for t in window}
    if len(families) != 1:
        return None
    return window[0][1], max(window, key=lambda t: t[3])[2]


def rule_for(family: str, n_so_far: int, allowed=None, used=None) -> str | None:
    from .looks import rule_for as pick
    return pick(family, n_so_far, allowed, used)


def render_sticker_source(path: str, src_start: float, dur: float, rule_name: str,
                          out: Path, aspect: float) -> bool:
    """Crop the shot to the output aspect, track + draw the sticker, write an intermediate clip.
    Returns False (caller falls back to the plain shot) if the target can't be found."""
    from .stickers import RULES, apply_rule, read_clip, sticker_centers, track, write_clip
    frames, fps = read_clip(path, src_start, dur)
    if len(frames) == 0:
        return False
    T, H, W, _ = frames.shape
    x0, y0, cw, ch = _center_crop_box(W, H, aspect)
    frames = np.ascontiguousarray(frames[:, y0:y0 + ch, x0:x0 + cw])
    if "@" in rule_name:                        # cartoon object: follow the subject, else free decoration
        from .cartoons import apply_follow, apply_free, follow_rule, split
        name, mode = split(rule_name)
        if mode != "free":
            rule = follow_rule(mode)
            try:
                tr = track(frames, fps, rule)
                if rule["head"]:
                    tr.scale = np.minimum(tr.scale, 0.45 * min(cw, ch))
                _, sizes, visible = sticker_centers(tr, rule, cw, ch)
                if visible.mean() >= 0.5 and np.median(sizes) >= 0.12 * cw:
                    write_clip(apply_follow(frames, fps, name, rule, tr, APPEAR), out, fps, cw, ch)
                    return True
            except RuntimeError:
                pass                            # subject not found: still decorate the shot
        write_clip(apply_free(frames, fps, name, APPEAR), out, fps, cw, ch)
        return True
    # eyes: a top-down cup puts the default anchor on the coffee surface (tiny eyes); retry on the wall
    attempts = [dict(RULES[rule_name])]
    if rule_name == "eyes":
        attempts.append({**RULES[rule_name], "anchor": (0.5, 0.8)})
    for rule in attempts:
        try:
            tr = track(frames, fps, rule)
        except RuntimeError:
            return False
        # On a narrow 3:4 / 9:16 crop the head box can come out nearly frame-wide; a head can't be bigger than
        # ~half the short side when the whole animal is in view. Oversized scales hide the sticker
        # (its "near the edge" margin grows with scale) and blow up its size.
        if rule["head"]:
            tr.scale = np.minimum(tr.scale, 0.45 * min(cw, ch))
        _, sizes, visible = sticker_centers(tr, rule, cw, ch)
        if visible.mean() >= 0.5 and np.median(sizes) >= 0.15 * cw:
            write_clip(apply_rule(frames, fps, rule_name, rule, tr, APPEAR), out, fps, cw, ch)
            return True
    return False


# ----------------------------------------------------------------------------- drawn-reveal opening

DRAWN_STEP = 1.0          # seconds between candidate start points
DRAWN_MAX_PER_CLIP = 2


def tag_drawable(clips: list[ClipAnalysis], aspect: float = 3 / 4, on_clip=None) -> None:
    """For clips already tagged with an animal / object, find start points that suit the drawn reveal
    (single frontal subject, sharp, steady). Fills clip.drawable with (t, label, family, area, conf)."""
    from .drawn_reveal import find_subject, is_drawable, load_shot
    for n, c in enumerate(clips):
        if on_clip:
            on_clip(n, len(clips), c)
        c.drawable = []
        if c.rejected or not any(c.tags):
            continue
        for t in np.arange(0.25, max(0.25, c.duration - 1.5), DRAWN_STEP):
            if presence(c, float(t), 0.5) is None:
                continue
            try:
                native, _, fps, _ = load_shot(c.path, float(t), 0.4, out_height=None, aspect=aspect)
            except RuntimeError:
                continue
            s, _ = find_subject(native[0])
            if s is None:
                continue
            ok, _, info = is_drawable(native, fps, s)
            if ok:
                c.drawable.append((float(t), info["label"], info["family"], info["area"], info["conf"]))
                if len(c.drawable) >= DRAWN_MAX_PER_CLIP:
                    break


def render_drawn_source(path: str, src_start: float, reveal_at: float, tail: float, out: Path,
                        background: str = "real", aspect: float = 3 / 4, out_height: int = 1440,
                        style: str = "lineart", box=None, force: bool = False) -> bool:
    """Sketch -> colour -> reveal clip, already cropped to `aspect` at `out_height`. False if the subject
    can't be drawn."""
    from .drawn_reveal import Timing, run
    stats = run(path, src_start, out, Timing(reveal_at=reveal_at, tail=tail), check=not force, background=background,
                out_height=out_height, aspect=aspect, style=style, box=box)
    return (force or bool(stats.get("drawable"))) and out.exists()


# ----------------------------------------------------------------------------- manual stickers (desktop app)

def _flow_path(gray: list, start: int, end: int, x: float, y: float, r: int) -> np.ndarray:
    """Path of the point (x, y) of frame `start` to frame `end` (either direction, both included): LK optical
    flow on corners around it, median motion per step (robust to a few bad tracks), re-seeded when too few
    survive. No detector involved, so it works on any footage."""
    step = 1 if end >= start else -1
    idx = list(range(start, end + step, step))
    path = np.zeros((len(idx), 2), np.float32)
    path[0] = (x, y)

    def seed(g, cx, cy):
        m = np.zeros_like(g)
        cv2.circle(m, (int(cx), int(cy)), r, 255, -1)
        return cv2.goodFeaturesToTrack(g, 40, 0.01, max(3, r // 8), mask=m)

    pts = seed(gray[idx[0]], x, y)
    for k in range(1, len(idx)):
        a, b = gray[idx[k - 1]], gray[idx[k]]
        if pts is not None and len(pts) >= 4:
            nxt, st, _ = cv2.calcOpticalFlowPyrLK(a, b, pts, None, winSize=(21, 21), maxLevel=3)
            ok = st[:, 0] == 1
            if ok.sum() >= 3:
                path[k] = path[k - 1] + np.median((nxt[ok] - pts[ok])[:, 0, :], axis=0)
                pts = nxt[ok].reshape(-1, 1, 2)
                continue
        path[k] = path[k - 1]
        pts = seed(b, *path[k])
    return path


TRACK_HEIGHT = 640     # sticker paths are worked out at this size everywhere (editor overlay, preview, full
                       # render) and scaled, so all three show the sticker in exactly the same place
MODES = ("follow", "path", "still")   # 跟着物体 / 按我画的路线 / 停在原地 ("fixed" = old name of "path")


def _catmull(ki: list[int], v: np.ndarray, T: int) -> np.ndarray:
    """Smooth curve through the keyframe values (Catmull-Rom, held flat before the first / after the last)."""
    out = np.empty(T, np.float32)
    n = len(ki)
    for f in range(T):
        if f <= ki[0]:
            out[f] = v[0]
            continue
        if f >= ki[-1]:
            out[f] = v[-1]
            continue
        j = int(np.searchsorted(ki, f, side="right")) - 1
        p0, p1, p2, p3 = v[max(0, j - 1)], v[j], v[j + 1], v[min(n - 1, j + 2)]
        u = (f - ki[j]) / max(1, ki[j + 1] - ki[j])
        out[f] = 0.5 * (2 * p1 + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u * u + (-p0 + 3 * p1 - 3 * p2 + p3) * u ** 3)
    return out


def sticker_path(frames: np.ndarray, fps: float, place: dict) -> tuple[np.ndarray, np.ndarray]:
    """Per-frame centre (px) and size (px) of a placed sticker. `place` = {mode, keys: [{t: s from shot start,
    x, y (0..1 of the frame), size (share of the short side)}, ...]}.
    follow (跟着物体): the point under the first keyframe is tracked through the shot (LK flow). A later keyframe
        is a correction: the track gets there by spreading the difference over the stretch before it (no jump),
        and carries on tracking from the corrected spot.
    path (按我画的路线): a smooth curve through the keyframes, nothing tracked.
    still (停在原地): the first keyframe's spot for the whole shot.
    Sizes interpolate linearly between keyframes in every mode."""
    T, H, W, _ = frames.shape
    mode = place.get("mode", "follow")
    mode = "path" if mode == "fixed" else mode
    keys = sorted(place.get("keys") or [{"t": 0.0, "x": place.get("x", 0.5), "y": place.get("y", 0.5),
                                         "size": place.get("size", 0.22)}], key=lambda k: k["t"])
    ki = [int(np.clip(round(k["t"] * fps), 0, T - 1)) for k in keys]
    for j in range(1, len(ki)):                    # two keys on one frame: keep them in order
        ki[j] = max(ki[j], ki[j - 1])
    kx = np.array([k["x"] * W for k in keys], np.float32)
    ky = np.array([k["y"] * H for k in keys], np.float32)
    ks = np.array([k.get("size", 0.22) * min(W, H) for k in keys], np.float32)
    frame_idx = np.arange(T)
    sizes = np.interp(frame_idx, ki, ks)
    if mode == "still":
        return np.tile([kx[0], ky[0]], (T, 1)).astype(np.float32), sizes
    if mode == "path":
        if len(ki) == 1 or ki[-1] == ki[0]:
            return np.tile([kx[0], ky[0]], (T, 1)).astype(np.float32), sizes
        return np.stack([_catmull(ki, kx, T), _catmull(ki, ky, T)], 1), sizes
    gray = [cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in frames]
    r = int(0.08 * min(W, H))
    centers = np.zeros((T, 2), np.float32)
    if ki[0] > 0:
        centers[: ki[0] + 1] = _flow_path(gray, ki[0], 0, kx[0], ky[0], r)[::-1]
    for j, s in enumerate(ki):
        last = j + 1 == len(ki)
        e = T - 1 if last else ki[j + 1]
        if e > s:
            seg = _flow_path(gray, s, e, kx[j], ky[j], r)
            if not last:                           # ease into the next keyframe instead of jumping onto it
                err = np.array([kx[j + 1], ky[j + 1]], np.float32) - seg[-1]
                seg = seg + err[None, :] * np.linspace(0, 1, len(seg), dtype=np.float32)[:, None]
            centers[s:e + 1] = seg
        else:
            centers[s] = (kx[j], ky[j])
    from scipy.ndimage import gaussian_filter1d
    sm = gaussian_filter1d(centers, 0.7, axis=0, mode="nearest")
    for f, x, y in zip(ki, kx, ky):               # keyframes stay exactly where they were put
        sm[f] = (x, y)
    return sm, sizes


_SHOT_FRAMES: dict = {}


def shot_frames(path: str, src_start: float, dur: float, aspect: float, out_height: int = TRACK_HEIGHT):
    """Frames of a shot, cropped to the output aspect and scaled to `out_height` (the timeline editor works on
    these to draw a sticker's path while it's being placed); the last few shots are kept in memory."""
    from .stickers import read_clip
    key = (path, round(src_start, 3), round(dur, 3), round(aspect, 5), out_height)
    if key not in _SHOT_FRAMES:
        frames, fps = read_clip(path, src_start, dur)
        if len(frames) == 0:
            raise RuntimeError(f"no frames at {src_start}s in {path}")
        T, H, W, _ = frames.shape
        x0, y0, cw, ch = _center_crop_box(W, H, aspect)
        ow = int(round(cw * out_height / ch)) // 2 * 2
        small = np.stack([cv2.resize(f[y0:y0 + ch, x0:x0 + cw], (ow, out_height), interpolation=cv2.INTER_AREA)
                          for f in frames])
        _SHOT_FRAMES[key] = (small, fps)
        while len(_SHOT_FRAMES) > 6:
            _SHOT_FRAMES.pop(next(iter(_SHOT_FRAMES)))
    return _SHOT_FRAMES[key]


def preview_path(path: str, src_start: float, dur: float, aspect: float, place: dict) -> dict:
    """Where a placed sticker will be on every frame of its shot (0..1 of the frame; size as a share of the
    short side), for drawing its path over the preview while editing. Same code as the render."""
    frames, fps = shot_frames(path, src_start, dur, aspect)
    T, H, W, _ = frames.shape
    centers, sizes = sticker_path(frames, fps, place)
    return {"fps": fps, "frames": T, "x": [round(float(v), 4) for v in centers[:, 0] / W],
            "y": [round(float(v), 4) for v in centers[:, 1] / H], "size": [round(float(v), 4) for v in sizes / min(W, H)]}


def render_manual_sticker(path: str, src_start: float, dur: float, sticker: str, place: dict, out: Path,
                          aspect: float, out_height: int | None = None) -> bool:
    """A sticker the user placed (see sticker_path); `place` may also carry `in` / `out` (seconds from the
    shot start) for when it pops in and out. With `out_height` the work happens at that (preview) size.
    The path comes from the same TRACK_HEIGHT frames as the editor overlay, scaled to this size."""
    from .cartoons import NAMES as CARTOONS
    from .cartoons import cartoon_frame
    from .stickers import composite, presence, read_clip, sticker_frame, write_clip
    frames, fps = read_clip(path, src_start, dur)
    if len(frames) == 0:
        return False
    T, H, W, _ = frames.shape
    x0, y0, cw, ch = _center_crop_box(W, H, aspect)
    frames = np.ascontiguousarray(frames[:, y0:y0 + ch, x0:x0 + cw])
    if out_height and out_height != ch:      # preview: work at the small size, it's much faster
        ow = int(round(cw * out_height / ch)) // 2 * 2
        frames = np.stack([cv2.resize(f, (ow, out_height), interpolation=cv2.INTER_AREA) for f in frames])
        T, ch, cw = len(frames), out_height, ow
    p = preview_path(path, src_start, dur, aspect, place)
    n = min(T, p["frames"])
    xs, ys, ss = np.array(p["x"][:n]), np.array(p["y"][:n]), np.array(p["size"][:n])
    if n < T:                                # a frame more decoded here: hold the last position
        xs, ys, ss = (np.concatenate([a, np.full(T - n, a[-1])]) for a in (xs, ys, ss))
    centers = np.stack([xs * cw, ys * ch], 1)
    sizes = ss * min(cw, ch)
    sid = sticker.partition("@")[0]
    t_in = float(place.get("in") or 0.0) + APPEAR
    t_out = place.get("out")
    shown = np.arange(T) / fps >= t_in - APPEAR
    if t_out is not None:
        shown &= np.arange(T) / fps < float(t_out)
    pops = presence(shown, fps, 0.0)

    def frames_out():
        for i in range(T):
            t = max(0.0, i / fps - t_in)
            if sid in CARTOONS:
                img, ang = cartoon_frame(sid, t, i, sizes[i], pops[i])
            else:
                img, ang = sticker_frame(sid, t + APPEAR, i, sizes[i], pops[i], APPEAR)
            yield composite(frames[i], img, centers[i], ang) if img is not None else frames[i]

    write_clip(frames_out(), out, fps, cw, ch)
    return True
