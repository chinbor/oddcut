"""Picture-in-picture compositor: overlay clips (and 宫格 grids of clips) laid over the edit.

An overlay (plan.looks.overlays[i], checked by pipeline._check_overlays):
    clip, src_start, t_start, t_end, track     what, from where in the clip, when on the edit, which lane
    x, y, w, h                                 its box (0..1 of the frame, top-left), rot (degrees), sc (scale)
    opacity, blend                             normal | screen | multiply | overlay | lighten | darken | add
    radius, border                             rounded corners (share of the box's short side), white outline
    mask {shape, cx, cy, size, rot, feather, invert}
                                               none | circle | heart | star | rect | linear | mirror, in box space
    keys [{t, x, y, s, r, a, e}]               keyframes (t from the overlay's start): centre, scale, rotation,
                                               opacity (a, 0..1, times the layer's opacity);
                                               e = how it moves on to the next one: smooth | out | in | linear
    mblur                                      motion blur while it moves (0..1.5)
    anim_in / anim_out {type, beats}           pop | slide_l | slide_r | slide_u | slide_d | zoom | fade | spin
    fx {kind: "drawn", style, keys: [{t, v}]}  手绘 on the layer: v = 绘制进度, 0 blank -> 1 drawn -> 2 the real
                                               picture again (drawn_reveal.Drawing), linear between keyframes
    grid {layout, cells: [{clip, src_start}], gap}   a 宫格 instead of one clip (layout 2v 2h 3v 4 9)

The same pose formulas live in the app (desktop/src/pip.js), so the box on the preview matches the render.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import cv2
import numpy as np

BLENDS = ("normal", "screen", "multiply", "overlay", "lighten", "darken", "add")
MASKS = ("none", "circle", "heart", "star", "rect", "linear", "mirror", "cutout")
EASES = ("smooth", "out", "in", "linear")
ANIMS = ("none", "pop", "slide_l", "slide_r", "slide_u", "slide_d", "zoom", "fade", "spin")
LAYOUTS = {"2v": (1, 2), "2h": (2, 1), "3v": (1, 3), "4": (2, 2), "9": (3, 3)}   # (columns, rows)


# ----------------------------------------------------------------------------- pose over time

def _smooth(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def _out_cubic(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return 1 - (1 - u) ** 3


def _out_back(u: float) -> float:
    u = min(1.0, max(0.0, u))
    c = 1.70158
    return 1 + (c + 1) * (u - 1) ** 3 + c * (u - 1) ** 2


def _ease_by(kind: str, u: float) -> float:
    u = min(1.0, max(0.0, u))
    if kind == "out":
        return _out_cubic(u)
    if kind == "in":
        return u ** 3
    if kind == "linear":
        return u
    return _smooth(u)


def fx_progress(o: dict, local: float) -> float:
    """The layer's 绘制进度 at `local` s (linear between its keyframes, held outside them)."""
    keys = (o.get("fx") or {}).get("keys") or []
    if not keys:
        return 2.0
    if local <= keys[0]["t"]:
        return float(keys[0]["v"])
    for a, b in zip(keys, keys[1:]):
        if local <= b["t"]:
            u = (local - a["t"]) / max(1e-6, b["t"] - a["t"])
            return float(a["v"] + (b["v"] - a["v"]) * u)
    return float(keys[-1]["v"])


def key_alpha(o: dict, local: float) -> float:
    """Keyframed opacity (1 without keyframes), between keyframes as the position moves."""
    keys = o.get("keys") or []
    if not keys:
        return 1.0
    if local <= keys[0]["t"]:
        return float(keys[0].get("a", 1.0))
    for a, b in zip(keys, keys[1:]):
        if local <= b["t"]:
            u = _ease_by(a.get("e", "smooth"), (local - a["t"]) / max(1e-6, b["t"] - a["t"]))
            return float(a.get("a", 1.0) + (b.get("a", 1.0) - a.get("a", 1.0)) * u)
    return float(keys[-1].get("a", 1.0))


def key_pose(o: dict, local: float) -> tuple[float, float, float, float]:
    """Centre (0..1 of the frame), scale (1 = the box) and rotation (degrees) at `local` s into the overlay.
    Without keyframes: the box, unscaled, at its rotation. Between keyframes: smoothstep."""
    keys = o.get("keys") or []
    if not keys:
        return o["x"] + o["w"] / 2, o["y"] + o["h"] / 2, float(o.get("sc", 1.0)), float(o.get("rot", 0.0))
    if local <= keys[0]["t"]:
        k = keys[0]
        return k["x"], k["y"], k["s"], k["r"]
    for a, b in zip(keys, keys[1:]):
        if local <= b["t"]:
            u = _ease_by(a.get("e", "smooth"), (local - a["t"]) / max(1e-6, b["t"] - a["t"]))
            return tuple(a[n] + (b[n] - a[n]) * u for n in ("x", "y", "s", "r"))
    k = keys[-1]
    return k["x"], k["y"], k["s"], k["r"]


def anim_pose(o: dict, local: float, beat: float) -> tuple[float, float, float, float, float]:
    """Entrance / exit on top of the pose: (dx, dy as share of the frame, scale x, rotation +, opacity x)."""
    length = o["t_end"] - o["t_start"]
    dx = dy = rot = 0.0
    s = alpha = 1.0
    for which in ("anim_in", "anim_out"):
        a = o.get(which) or {}
        kind = a.get("type", "none")
        if kind == "none":
            continue
        d = max(0.05, float(a.get("beats", 1)) * beat)
        u = local / d if which == "anim_in" else (length - local) / d    # 0 at the edge .. 1 settled
        if u >= 1:
            continue
        e = _out_cubic(u)
        if kind == "pop":
            s *= max(0.0, 0.5 + 0.5 * _out_back(u))
            alpha *= min(1.0, u * 3)
        elif kind == "zoom":
            s *= max(0.01, e)
            alpha *= min(1.0, u * 2)
        elif kind == "fade":
            alpha *= _smooth(u)
        elif kind == "spin":
            rot += -180 * (1 - e) * (1 if which == "anim_in" else -1)
            s *= 0.3 + 0.7 * e
            alpha *= min(1.0, u * 2)
        elif kind.startswith("slide_"):
            far = {"slide_l": (-1, 0), "slide_r": (1, 0), "slide_u": (0, -1), "slide_d": (0, 1)}[kind]
            dx += far[0] * (1 - e) * 1.1
            dy += far[1] * (1 - e) * 1.1
    return dx, dy, s, rot, alpha


# ----------------------------------------------------------------------------- masks

def _shape_points(shape: str, n: int = 96) -> np.ndarray:
    """Unit outline (radius ~1, centred on 0) of a heart or a star."""
    if shape == "heart":
        t = np.linspace(0, 2 * np.pi, n, endpoint=False)
        x = 16 * np.sin(t) ** 3
        y = -(13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t))
        p = np.stack([x, y + 2.5], 1) / 17.0
        return p
    k = np.arange(10)
    r = np.where(k % 2 == 0, 1.0, 0.45)
    ang = -np.pi / 2 + k * np.pi / 5
    return np.stack([r * np.cos(ang), r * np.sin(ang)], 1)


def mask_image(o: dict, bw: int, bh: int) -> np.ndarray:
    """Alpha (0..1, float32, bh x bw) of the overlay's box: its mask shape and rounded corners."""
    m = o.get("mask") or {}
    shape = m.get("shape", "none")
    short = min(bw, bh)
    if shape in ("none", "rect"):
        img = np.zeros((bh, bw), np.uint8)
        if shape == "none":
            x0, y0, x1, y1 = 0, 0, bw - 1, bh - 1
            r = int(float(o.get("radius", 0)) * short)
            angle = 0.0
        else:
            sz = float(m.get("size", 0.8))
            hw, hh = sz * bw / 2, sz * bh / 2
            cx, cy = float(m.get("cx", 0.5)) * bw, float(m.get("cy", 0.5)) * bh
            x0, y0, x1, y1 = cx - hw, cy - hh, cx + hw, cy + hh
            r = int(float(o.get("radius", 0.1)) * short)
            angle = float(m.get("rot", 0))
        _rounded_rect(img, x0, y0, x1, y1, r, angle)
        alpha = img.astype(np.float32) / 255
    else:
        cx, cy = float(m.get("cx", 0.5)) * bw, float(m.get("cy", 0.5)) * bh
        size = float(m.get("size", 0.8)) * short / 2
        ang = math.radians(float(m.get("rot", 0)))
        img = np.zeros((bh, bw), np.uint8)
        if shape == "circle":
            cv2.ellipse(img, (int(cx), int(cy)), (max(1, int(size)), max(1, int(size))), 0, 0, 360, 255, -1, cv2.LINE_AA)
            alpha = img.astype(np.float32) / 255
        elif shape in ("heart", "star"):
            p = _shape_points(shape) * size
            rot = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
            p = p @ rot.T + [cx, cy]
            cv2.fillPoly(img, [np.round(p * 4).astype(np.int32)], 255, cv2.LINE_AA, shift=2)
            alpha = img.astype(np.float32) / 255
        else:                                   # linear: one side of a line; mirror: a band along it
            yy, xx = np.mgrid[0:bh, 0:bw].astype(np.float32)
            nx, ny = -math.sin(ang), math.cos(ang)  # normal of a line at angle `rot`
            d = (xx - cx) * nx + (yy - cy) * ny
            if shape == "linear":
                alpha = (d > 0).astype(np.float32)
            else:
                alpha = (np.abs(d) < size).astype(np.float32)
    feather = float(m.get("feather", 0)) * short
    if feather >= 1:
        alpha = cv2.GaussianBlur(alpha, (0, 0), feather / 2)
    if m.get("invert"):
        alpha = 1 - alpha
    if shape != "none" and float(o.get("radius", 0)) > 0 and shape != "rect":   # keep the box's own corners
        corner = np.zeros((bh, bw), np.uint8)
        _rounded_rect(corner, 0, 0, bw - 1, bh - 1, int(float(o["radius"]) * short), 0)
        alpha *= corner.astype(np.float32) / 255
    return alpha


def _rounded_rect(img: np.ndarray, x0, y0, x1, y1, r, angle):
    """Filled rounded rectangle (anti-aliased), rotated by `angle` degrees about its centre."""
    w, h = x1 - x0, y1 - y0
    r = max(0, min(int(r), int(min(w, h) / 2)))
    pts = []
    for (cx, cy, a0) in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for k in range(9):
            a = math.radians(a0 + k * 90 / 8)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    p = np.array(pts)
    if angle:
        c = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
        t = math.radians(angle)
        rot = np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
        p = (p - c) @ rot.T + c
    cv2.fillPoly(img, [np.round(p * 4).astype(np.int32)], 255, cv2.LINE_AA, shift=2)


def grid_cells(o: dict, bw: int, bh: int) -> list[tuple[int, int, int, int]]:
    """Pixel rectangles (x, y, w, h) of a 宫格's cells inside its box, row by row."""
    cols, rows = LAYOUTS[o["grid"]["layout"]]
    gap = int(float(o["grid"].get("gap", 0.01)) * min(bw, bh))
    cw, ch = (bw - gap * (cols - 1)) / cols, (bh - gap * (rows - 1)) / rows
    out = []
    for r in range(rows):
        for c in range(cols):
            x, y = round(c * (cw + gap)), round(r * (ch + gap))
            out.append((x, y, max(2, round(cw) // 2 * 2), max(2, round(ch) // 2 * 2)))
    return out


# ----------------------------------------------------------------------------- frames

class _Reader:
    """Frames of a clip from `start`, filled into w x h, graded like the edit, one at a time."""

    def __init__(self, path: str, start: float, frames: int, w: int, h: int, fps: float, grade: str, preview: bool):
        self.w, self.h, self.last = w, h, np.zeros((h, w, 3), np.uint8)
        vf = (f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase:flags={'bilinear' if preview else 'lanczos'},"
              f"crop={w}:{h},setsar=1" + (f",{grade}" if grade else "")
              + ",tpad=stop_mode=clone:stop=-1,scale=in_color_matrix=bt709:in_range=tv")
        self.proc = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", path, "-vf", vf,
                                      "-frames:v", str(frames), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self) -> np.ndarray:
        b = self.proc.stdout.read(self.w * self.h * 3)
        if len(b) == self.w * self.h * 3:
            self.last = np.frombuffer(b, np.uint8).reshape(self.h, self.w, 3)
        return self.last

    def close(self):
        try:
            self.proc.stdout.close()
        finally:
            self.proc.kill()
            self.proc.wait()


def _blend(a: np.ndarray, b: np.ndarray, mode: str) -> np.ndarray:
    """a = what is underneath, b = the overlay, both 0..1."""
    if mode == "screen":
        return 1 - (1 - a) * (1 - b)
    if mode == "multiply":
        return a * b
    if mode == "overlay":
        return np.where(a < 0.5, 2 * a * b, 1 - 2 * (1 - a) * (1 - b))
    if mode == "lighten":
        return np.maximum(a, b)
    if mode == "darken":
        return np.minimum(a, b)
    if mode == "add":
        return np.minimum(1.0, a + b)
    return b


class _Layer:
    """One overlay while it is on screen: its reader(s), box-space alpha and border."""

    def __init__(self, o: dict, w: int, h: int, fps: float, grade: str, preview: bool, cache_dir: Path | None = None):
        self.o = o
        self.cut = None                              # 抠像: one mask per frame (box space, SEG_H tall)
        self.f = 0
        self.fps = fps
        self.drawing = None                          # 手绘: built from the first frame
        self.preview = preview
        self.bw, self.bh = max(2, round(o["w"] * w / 2) * 2), max(2, round(o["h"] * h / 2) * 2)
        n = int(math.ceil((o["t_end"] - o["t_start"]) * fps)) + 2
        if o.get("grid"):
            self.cells = grid_cells(o, self.bw, self.bh)
            self.readers = [_Reader(c["clip"], c.get("src_start", 0.0), n, cw, ch, fps, grade, preview)
                            for c, (_, _, cw, ch) in zip(o["grid"]["cells"], self.cells)]
            alpha = np.zeros((self.bh, self.bw), np.float32)
            r = int(float(o.get("radius", 0)) * min(self.bw, self.bh) / 3)
            for (x, y, cw, ch) in self.cells:
                cell = np.zeros((ch, cw), np.uint8)
                _rounded_rect(cell, 0, 0, cw - 1, ch - 1, r, 0)
                alpha[y:y + ch, x:x + cw] = np.maximum(alpha[y:y + ch, x:x + cw], cell[:self.bh - y, :self.bw - x] / 255)
        else:
            self.readers = [_Reader(o["clip"], o.get("src_start", 0.0), n, self.bw, self.bh, fps, grade, preview)]
            m = o.get("mask") or {}
            if m.get("shape") == "cutout":
                from .cutout import cutout_masks
                self.cut = cutout_masks(o["clip"], o.get("src_start", 0.0), n, fps, self.bw / self.bh, m.get("target") or {},
                                        m.get("brush"), cache_dir)
                alpha = np.ones((self.bh, self.bw), np.float32)
            else:
                alpha = mask_image(o, self.bw, self.bh)
        self.alpha = alpha.astype(np.float32)
        self.cache = None
        self.border = None
        if o.get("border"):
            edge = np.zeros((self.bh, self.bw), np.uint8)
            cs, _ = cv2.findContours((alpha > 0.5).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            cv2.drawContours(edge, cs, -1, 255, max(2, round(min(w, h) * 0.006)), cv2.LINE_AA)
            self.border = edge.astype(np.float32) / 255

    def cut_alpha(self) -> np.ndarray | None:
        """This frame's 抠像 mask at box size (soft edge, feathered as asked), or None. hold: the first frame's
        mask throughout (a drawing doesn't move); dilate: a little wider than the object (covers it fully)."""
        if self.cut is None:
            return None
        mk = self.o.get("mask") or {}
        m = self.cut[0 if mk.get("hold") else min(self.f, len(self.cut) - 1)].astype(np.float32)
        if mk.get("dilate"):
            k = max(1, int(float(mk["dilate"]) * m.shape[0])) | 1
            m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        feather = float(mk.get("feather", 0.0))
        m = cv2.GaussianBlur(m, (0, 0), 0.8 + feather * m.shape[0] * 0.5)
        a = cv2.resize(m, (self.bw, self.bh), interpolation=cv2.INTER_LINEAR)
        if (self.o.get("mask") or {}).get("invert"):
            a = 1 - a
        return a

    def _drawn(self, live: np.ndarray, local: float) -> np.ndarray:
        """手绘: the drawing at this frame's 绘制进度 (the first frame is drawn; the live picture comes back)."""
        from . import drawn_reveal as dr
        if self.drawing is None:
            m = self.cut[0] if self.cut is not None else np.ones((8, 8), bool)
            m = cv2.resize(m.astype(np.uint8), (self.bw, self.bh), interpolation=cv2.INTER_NEAREST) > 0
            ys, xs = np.nonzero(m)
            box = np.array([xs.min(), ys.min(), xs.max(), ys.max()], np.float32) if len(xs) else np.array([0, 0, self.bw, self.bh], np.float32)
            s = dr.Subject("object", "object", 1.0, box, m, m)
            try:                                     # an animal / known object: its head, eyes, nose too
                from .sticker_stage import VISION
                with VISION:
                    found, _ = dr.find_subject(live)
                if found is not None and (found.mask & m).sum() > 0.5 * max(1, (found.mask | m).sum()):
                    s = found
            except Exception:                        # noqa: BLE001 - the plain object drawing still works
                pass
            strokes = dr.build_strokes(live, s, 1.0, 0)
            regions, _ = dr.build_fills(live, s, 1.0, k=4, seed=0)
            regions += dr.feature_fills(live, s, 1.0)
            self.drawing = dr.Drawing(live, s, strokes, regions, 0, "real", (self.o.get("fx") or {}).get("style", "lineart"))
        p = fx_progress(self.o, local)
        if p >= 2:
            return live
        return self.drawing.at(p, int(local * self.fps) // 3, live.astype(np.float32))

    def frame(self) -> np.ndarray:
        """The box's picture for the next frame (uint8)."""
        self.f += 1
        if not self.o.get("grid"):
            live = self.readers[0].read()
            if (self.o.get("fx") or {}).get("kind") == "drawn":
                return self._drawn(live, (self.f - 1) / self.fps)
            return live
        rgb = np.zeros((self.bh, self.bw, 3), np.uint8)
        for rd, (x, y, cw, ch) in zip(self.readers, self.cells):
            rgb[y:y + ch, x:x + cw] = rd.read()[:self.bh - y, :self.bw - x]
        return rgb

    def close(self):
        for r in self.readers:
            r.close()


def _place(base: np.ndarray, layer: _Layer, local: float, beat: float):
    """Draw one overlay frame onto `base` (uint8, in place). Only the overlay's own region is touched; its
    warped mask is reused while it doesn't move (blendLinear does the mixing on uint8)."""
    o = layer.o
    H, W = base.shape[:2]
    cx, cy, s, r = key_pose(o, local)
    dx, dy, s2, r2, a2 = anim_pose(o, local, beat)
    s, r = s * s2, r + r2
    opacity = float(o.get("opacity", 1.0)) * a2 * key_alpha(o, local)
    if s <= 0.01 or opacity <= 0.003:
        layer.frame()                           # keep the clip running
        return
    cut = layer.cut_alpha()
    rgb = layer.frame()
    X, Y = (cx + dx) * W, (cy + dy) * H
    M = cv2.getRotationMatrix2D((layer.bw / 2, layer.bh / 2), -r, s)
    M[0, 2] += X - layer.bw / 2
    M[1, 2] += Y - layer.bh / 2
    corners = np.array([[0, 0, 1], [layer.bw, 0, 1], [0, layer.bh, 1], [layer.bw, layer.bh, 1]], np.float64) @ M.T
    x0, y0 = max(0, int(corners[:, 0].min()) - 1), max(0, int(corners[:, 1].min()) - 1)
    x1, y1 = min(W, int(corners[:, 0].max()) + 2), min(H, int(corners[:, 1].max()) + 2)
    if x0 >= x1 or y0 >= y1:
        return
    M[0, 2] -= x0
    M[1, 2] -= y0
    size = (x1 - x0, y1 - y0)
    key = (x0, y0, x1, y1, round(opacity, 4), *np.round(M, 4).ravel())
    if cut is not None or layer.cache is None or layer.cache[0] != key:
        a_w = cv2.warpAffine(layer.alpha if cut is None else cut, M, size, flags=cv2.INTER_LINEAR) * opacity
        b_w = cv2.warpAffine(layer.border, M, size, flags=cv2.INTER_LINEAR) * opacity if layer.border is not None else None
        layer.cache = (key, a_w, 1.0 - a_w, b_w, None if b_w is None else 1.0 - b_w)
    _, a_w, inv, b_w, binv = layer.cache
    img = cv2.warpAffine(rgb, M, size, flags=cv2.INTER_LINEAR)
    mb = float(o.get("mblur", 0) or 0)
    if mb > 0:                                   # motion blur: smear along the move between frames
        nx, ny, _, _ = key_pose(o, local + 1.0 / 30)
        ddx, ddy = (nx - cx) * W, (ny - cy) * H
        k = int(min(0.06 * H, 0.5 * mb * max(abs(ddx), abs(ddy))))
        if k >= 3:                               # a soft (gaussian) trail, not a box: no hard-edged ghost copies
            kk, sg = (2 * k) | 1, k / 2.5
            ks = (1, kk) if abs(ddy) >= abs(ddx) else (kk, 1)
            sx, sy = (0.1, sg) if abs(ddy) >= abs(ddx) else (sg, 0.1)
            img = cv2.GaussianBlur(img, ks, sigmaX=sx, sigmaY=sy)
            a_w = cv2.GaussianBlur(a_w, ks, sigmaX=sx, sigmaY=sy)
            inv = 1.0 - a_w
    roi = base[y0:y1, x0:x1]
    mode = o.get("blend", "normal")
    if mode != "normal":
        img = (np.clip(_blend(roi.astype(np.float32) / 255, img.astype(np.float32) / 255, mode), 0, 1) * 255).astype(np.uint8)
    out = cv2.blendLinear(roi, img, inv, a_w)
    if b_w is not None:
        out = cv2.blendLinear(out, np.full_like(out, 255), binv, b_w)
    roi[:] = out


def render_overlays(video: Path, overlays: list[dict], out: Path, w: int, h: int, fps: float, frames: int,
                    grade: str, preview: bool, beat: float, encode: list[str], cache_dir: Path | None = None) -> Path:
    """The joined edit with every overlay drawn on it (lower track first, so higher ones end up on top)."""
    order = sorted(overlays, key=lambda o: (o["track"], o["t_start"]))
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(video), "-frames:v", str(frames),
                            "-vf", "scale=in_color_matrix=bt709:in_range=tv", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                            "-r", str(fps), "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", *encode, str(out)],
                           stdin=subprocess.PIPE)
    live: dict[int, _Layer] = {}
    size = w * h * 3
    last = None
    try:
        for f in range(frames):
            raw = dec.stdout.read(size)
            if len(raw) < size:
                raw = last if last is not None else bytes(size)
            last = raw
            t = f / fps
            on = [k for k, o in enumerate(order) if o["t_start"] - 1e-6 <= t < o["t_end"] - 1e-6]
            for k in list(live):
                if k not in on:
                    live.pop(k).close()
            if not on:
                enc.stdin.write(raw)
                continue
            base = np.frombuffer(raw, np.uint8).reshape(h, w, 3).copy()
            for k in on:
                if k not in live:
                    live[k] = _Layer(order[k], w, h, fps, grade, preview, cache_dir)
                _place(base, live[k], t - order[k]["t_start"], beat)
            enc.stdin.write(base.tobytes())
    finally:
        for layer in live.values():
            layer.close()
        enc.stdin.close()
        enc.wait()
        dec.stdout.close()
        dec.wait()
    if enc.returncode:
        raise RuntimeError("ffmpeg failed while encoding the picture-in-picture pass")
    return out
