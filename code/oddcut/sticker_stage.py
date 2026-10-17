"""Glue between the edit pipeline and oddcut.stickers: tag clips, pick rules, render sticker shots.

Heavy deps (torch / ultralytics / cv2) are imported lazily so the pipeline still runs without them.
"""
from pathlib import Path

import numpy as np

from .analyze import ClipAnalysis
from .ffmpeg_utils import probe, read_frames

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


def rule_for(family: str, n_animal_so_far: int) -> str:
    if family == "object":
        return "eyes"
    return "anger" if n_animal_so_far % 2 == 0 else "question"


def render_sticker_source(path: str, src_start: float, dur: float, rule_name: str,
                          out: Path, aspect: float) -> bool:
    """Crop the shot to the output aspect, track + draw the sticker, write an intermediate clip.
    Returns False (caller falls back to the plain shot) if the target can't be found."""
    from .stickers import RULES, apply_rule, read_clip, track, write_clip
    frames, fps = read_clip(path, src_start, dur)
    if len(frames) == 0:
        return False
    T, H, W, _ = frames.shape
    x0, y0, cw, ch = _center_crop_box(W, H, aspect)
    frames = np.ascontiguousarray(frames[:, y0:y0 + ch, x0:x0 + cw])
    from .stickers import sticker_centers
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
                        background: str = "real", aspect: float = 3 / 4, out_height: int = 1440) -> bool:
    """Sketch -> colour -> reveal clip, already cropped to `aspect` at `out_height`. False if the subject
    can't be drawn."""
    from .drawn_reveal import Timing, run
    stats = run(path, src_start, out, Timing(reveal_at=reveal_at, tail=tail), check=True, background=background,
                out_height=out_height, aspect=aspect)
    return bool(stats.get("drawable")) and out.exists()
