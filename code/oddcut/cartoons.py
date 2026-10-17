"""Cartoon object stickers (卡通小物件): car, plane, camera, sparkle, heart, music note.

Drawn like the other stickers (PIL, supersampled, wobbly hand-drawn outline, white sticker rim) and
animated while idle: the car bobs with spinning wheels, the plane rocks with a spinning propeller,
the camera flashes, the sparkles twinkle, the heart beats, the notes sway.

Two ways onto a shot (the planner encodes which in the sticker id, e.g. "plane@animal"):
    follow  the shot has a tracked cat / dog / cup ...: the sticker sits beside it and moves with it
            (stickers.track does the tracking); the plane circles it, the notes float up from it
    free    no tracked subject: a screen-space decoration in the emptiest part of the frame; the car
            drives along the bottom, the plane flies across the top, the others sit in a quiet corner
"""
import math

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .stickers import (ANIMALS, BLACK, FRONT_OBJECTS, SS, WHITE, _smooth_noise, composite, presence,
                       sticker_centers)

NAMES = ("car", "plane", "camera", "sparkle", "heart", "note")

RED = (232, 72, 60, 255)
SKY = (120, 188, 245, 255)
BLUE = (64, 120, 220, 255)
GLASS = (182, 226, 248, 255)
TYRE = (44, 44, 52, 255)
GREY = (170, 176, 186, 255)
DARK = (58, 60, 70, 255)
YELLOW = (255, 210, 60, 255)
PINK = (240, 82, 120, 255)
PURPLE = (112, 92, 226, 255)


def split(sticker: str) -> tuple[str, str]:
    """"plane@animal" -> ("plane", "animal")."""
    name, _, mode = sticker.partition("@")
    return name, mode or "free"


# ----------------------------------------------------------------------------- shapes

def _ell(cx, cy, rx, ry, n=40, a0=0.0, a1=360.0) -> np.ndarray:
    a = np.radians(np.linspace(a0, a1, n))
    return np.stack([cx + rx * np.cos(a), cy + ry * np.sin(a)], 1)


def _rrect(x0, y0, x1, y1, r, n=8) -> np.ndarray:
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    corners = [(x1 - r, y0 + r, -90, 0), (x1 - r, y1 - r, 0, 90), (x0 + r, y1 - r, 90, 180), (x0 + r, y0 + r, 180, 270)]
    return np.vstack([_ell(cx, cy, r, r, n, a0, a1) for cx, cy, a0, a1 in corners])


def _star(cx, cy, r_out, r_in, points=4, rot=0.0) -> np.ndarray:
    a = np.radians(rot) + np.pi * np.arange(2 * points) / points - np.pi / 2
    r = np.where(np.arange(2 * points) % 2 == 0, r_out, r_in)
    pts = np.stack([cx + r * np.cos(a), cy + r * np.sin(a)], 1)
    # soften the star into a hand-drawn bulgy one: midpoints pulled in
    out = []
    for i in range(len(pts)):
        p, q = pts[i], pts[(i + 1) % len(pts)]
        out.append(p)
        mid = (p + q) / 2
        out.append(mid + (np.array([cx, cy]) - mid) * 0.18)
    return np.array(out)


def _heart(cx, cy, s, n=60) -> np.ndarray:
    t = np.linspace(0, 2 * np.pi, n)
    x = 16 * np.sin(t) ** 3
    y = -(13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t))
    return np.stack([cx + x * s / 32, cy + (y + 2) * s / 32], 1)


def _wobble(pts: np.ndarray, amp: float, rng) -> np.ndarray:
    n = len(pts)
    return pts + np.stack([_smooth_noise(n, rng, max(4, n // 12)), _smooth_noise(n, rng, max(4, n // 12))], 1) * amp


class _Pen:
    """Draws filled shapes with a white sticker rim behind everything and a wobbly dark outline."""

    def __init__(self, n: int, seed: int):
        self.n = n
        self.img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        self.rim = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        self.d, self.dr = ImageDraw.Draw(self.img), ImageDraw.Draw(self.rim)
        self.rng = np.random.default_rng(seed)
        self.line = max(2, int(0.035 * n))

    def shape(self, pts, fill, outline=True, rim=True, line=None):
        pts = _wobble(np.asarray(pts, float), 0.006 * self.n, self.rng)
        poly = [tuple(p) for p in pts]
        w = line or self.line
        if rim:
            self.dr.polygon(poly, fill=WHITE)
            self.dr.line(poly + [poly[0]], fill=WHITE, width=int(w + 0.07 * self.n), joint="curve")
        self.d.polygon(poly, fill=fill)
        if outline:
            self.d.line(poly + [poly[0]], fill=BLACK, width=w, joint="curve")

    def stroke(self, pts, width=None, color=BLACK, rim=False):
        pts = _wobble(np.asarray(pts, float), 0.004 * self.n, self.rng)
        poly = [tuple(p) for p in pts]
        w = int(width or self.line)
        if rim:
            self.dr.line(poly, fill=WHITE, width=int(w + 0.07 * self.n), joint="curve")
        self.d.line(poly, fill=color, width=w, joint="curve")

    def done(self, size: int) -> Image.Image:
        out = self.rim.copy()
        out.alpha_composite(self.img)
        return out.resize((size, size), Image.LANCZOS)


def draw_car(size: int, t: float, seed: int) -> Image.Image:
    N = size * SS
    p = _Pen(N, seed)
    bob = 0.015 * N * math.sin(2 * math.pi * 3.0 * t)
    y = bob
    p.shape(_rrect(0.16 * N, 0.30 * N + y, 0.66 * N, 0.56 * N + y, 0.10 * N), RED)            # cabin
    p.shape(_rrect(0.24 * N, 0.36 * N + y, 0.40 * N, 0.50 * N + y, 0.04 * N), GLASS, rim=False)
    p.shape(_rrect(0.44 * N, 0.36 * N + y, 0.60 * N, 0.50 * N + y, 0.04 * N), GLASS, rim=False)
    p.shape(_rrect(0.06 * N, 0.48 * N + y, 0.94 * N, 0.72 * N + y, 0.10 * N), RED)            # body
    p.shape(_ell(0.88 * N, 0.56 * N + y, 0.035 * N, 0.035 * N), YELLOW, rim=False)            # headlight
    spin = -t * 720.0
    for wx in (0.28, 0.74):
        cx, cy, r = wx * N, 0.74 * N, 0.12 * N
        p.shape(_ell(cx, cy, r, r), TYRE)
        p.shape(_ell(cx, cy, 0.5 * r, 0.5 * r), GREY, rim=False, line=max(1, p.line // 2))
        a = math.radians(spin)
        p.stroke([(cx - 0.45 * r * math.cos(a), cy - 0.45 * r * math.sin(a)),
                  (cx + 0.45 * r * math.cos(a), cy + 0.45 * r * math.sin(a))], width=p.line * 0.7)
    for k in range(2):                                                                     # exhaust puffs
        ph = (t * 1.6 + 0.5 * k) % 1.0
        r = (0.03 + 0.05 * ph) * N
        p.shape(_ell((0.04 - 0.08 * ph) * N + r, (0.66 - 0.06 * ph) * N, r, r), (226, 228, 234, int(255 * (1 - ph))),
                outline=False, rim=False)
    return p.done(size)


def draw_plane(size: int, t: float, seed: int) -> Image.Image:
    N = size * SS
    p = _Pen(N, seed)
    p.shape([(0.18 * N, 0.50 * N), (0.10 * N, 0.24 * N), (0.24 * N, 0.24 * N), (0.36 * N, 0.46 * N)], BLUE)  # tail fin
    p.shape(_ell(0.50 * N, 0.52 * N, 0.36 * N, 0.13 * N), SKY)                                                # fuselage
    p.shape([(0.40 * N, 0.54 * N), (0.62 * N, 0.54 * N), (0.50 * N, 0.82 * N), (0.36 * N, 0.82 * N)], BLUE)  # wing
    for wx in (0.52, 0.64):
        p.shape(_ell(wx * N, 0.47 * N, 0.04 * N, 0.04 * N), GLASS, rim=False, line=max(1, p.line // 2))
    p.shape(_ell(0.87 * N, 0.52 * N, 0.04 * N, 0.04 * N), RED, rim=False)                                   # nose
    blade = abs(math.sin(2 * math.pi * 6.0 * t))                                                         # propeller
    p.shape(_ell(0.90 * N, 0.52 * N, 0.025 * N, (0.05 + 0.14 * blade) * N), (90, 90, 100, 230), rim=False,
            line=max(1, p.line // 2))
    return p.done(size)


def draw_camera(size: int, t: float, seed: int) -> Image.Image:
    N = size * SS
    p = _Pen(N, seed)
    ph = (t % 1.4) / 1.4
    flash = max(0.0, 1.0 - abs(ph - 0.15) / 0.08)                       # a short flash every 1.4 s
    if flash > 0:
        p.shape(_star(0.70 * N, 0.20 * N, (0.10 + 0.14 * flash) * N, 0.05 * N, points=6, rot=15), YELLOW, outline=False)
    p.shape(_rrect(0.30 * N, 0.20 * N, 0.52 * N, 0.34 * N, 0.04 * N), DARK)                                 # top bump
    p.shape(_rrect(0.10 * N, 0.28 * N, 0.90 * N, 0.78 * N, 0.10 * N), DARK)                                 # body
    p.shape(_rrect(0.66 * N, 0.33 * N, 0.82 * N, 0.42 * N, 0.02 * N),
            (255, 255, 255, 255) if flash > 0 else (220, 224, 230, 255), rim=False, line=max(1, p.line // 2))
    p.shape(_ell(0.50 * N, 0.54 * N, 0.20 * N, 0.20 * N), GREY, rim=False)                                  # lens ring
    p.shape(_ell(0.50 * N, 0.54 * N, 0.12 * N, 0.12 * N), (40, 70, 110, 255), rim=False)                    # glass
    p.shape(_ell(0.46 * N, 0.50 * N, 0.035 * N, 0.035 * N), WHITE, outline=False, rim=False)
    return p.done(size)


def draw_sparkle(size: int, t: float, seed: int) -> Image.Image:
    N = size * SS
    p = _Pen(N, seed)
    for cx, cy, r, phase in ((0.42, 0.50, 0.30, 0.0), (0.78, 0.24, 0.13, 0.33), (0.76, 0.76, 0.10, 0.66)):
        tw = 0.75 + 0.25 * math.sin(2 * math.pi * (1.8 * t + phase))
        p.shape(_star(cx * N, cy * N, r * tw * N, 0.30 * r * tw * N, rot=8 * math.sin(2 * math.pi * t)), YELLOW)
    return p.done(size)


def draw_heart(size: int, t: float, seed: int) -> Image.Image:
    N = size * SS
    p = _Pen(N, seed)
    beat = 1.0 + 0.10 * max(0.0, math.sin(2 * math.pi * 1.6 * t)) ** 6
    p.shape(_heart(0.5 * N, 0.48 * N, 0.78 * N * beat), PINK)
    p.shape(_ell(0.34 * N, 0.36 * N, 0.06 * N, 0.04 * N), (255, 255, 255, 220), outline=False, rim=False)
    return p.done(size)


def draw_note(size: int, t: float, seed: int) -> Image.Image:
    N = size * SS
    p = _Pen(N, seed)
    heads = [(0.30 * N, 0.74 * N), (0.70 * N, 0.66 * N)]
    w = p.line * 1.2
    p.stroke([(0.38 * N, 0.74 * N), (0.38 * N, 0.24 * N)], width=w, rim=True)
    p.stroke([(0.78 * N, 0.66 * N), (0.78 * N, 0.16 * N)], width=w, rim=True)
    p.shape([(0.36 * N, 0.22 * N), (0.80 * N, 0.12 * N), (0.80 * N, 0.24 * N), (0.36 * N, 0.34 * N)], PURPLE)
    for hx, hy in heads:
        p.shape(_ell(hx, hy, 0.10 * N, 0.075 * N), PURPLE)
    return p.done(size)


DRAW = {"car": draw_car, "plane": draw_plane, "camera": draw_camera, "sparkle": draw_sparkle,
        "heart": draw_heart, "note": draw_note}


def cartoon_frame(name: str, t: float, frame_idx: int, size: float, pop: float):
    """Sticker image at time t (seconds since it appeared) and its rotation (deg)."""
    if pop <= 0.02:
        return None, 0.0
    img = DRAW[name](max(8, int(size * pop)), t, frame_idx // 3)        # line boil every 3 frames
    angle = {"plane": 6 * math.sin(2 * math.pi * 0.8 * t), "note": 10 * math.sin(2 * math.pi * 1.1 * t),
             "heart": 4 * math.sin(2 * math.pi * 0.7 * t)}.get(name, 0.0)
    return img, angle


def preview(name: str, size: int = 96) -> Image.Image:
    img, angle = cartoon_frame(name, 0.32, 0, size * 0.9, 1.0)
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True)
    img.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(img, ((size - img.width) // 2, (size - img.height) // 2))
    return canvas


# ----------------------------------------------------------------------------- follow a tracked subject

def follow_rule(family: str) -> dict:
    """Tracking rule (see stickers.RULES) that puts the sticker beside an object or above an animal's head."""
    if family == "animal":
        return dict(targets=ANIMALS, head=True, anchor=(0.5, 0.1), offset=(0.85, -0.35), size=0.55,
                    alt_offset=(-0.85, -0.35))
    return dict(targets=FRONT_OBJECTS, head=False, anchor=(0.5, 0.2), offset=(0.62, -0.45), size=0.45,
                alt_offset=(-0.62, -0.45))


def _orbit(name: str, t: float) -> tuple[float, float]:
    """Idle motion around the placement point, in units of the sticker size."""
    if name == "plane":                      # circles its subject
        return 0.55 * math.cos(2 * math.pi * 0.45 * t), 0.22 * math.sin(2 * math.pi * 0.45 * t)
    if name == "note":                       # floats up and drifts
        return 0.10 * math.sin(2 * math.pi * 0.9 * t), -0.25 * ((0.6 * t) % 1.0)
    if name == "car":                        # little shuttle back and forth
        return 0.25 * math.sin(2 * math.pi * 0.5 * t), 0.0
    if name == "heart":
        return 0.0, -0.08 * math.sin(2 * math.pi * 0.6 * t)
    return 0.0, 0.0


def apply_follow(frames: np.ndarray, fps: float, name: str, rule: dict, tr, appear: float = 0.15):
    T, H, W, _ = frames.shape
    centers, sizes, visible = sticker_centers(tr, rule, W, H)
    sizes = np.clip(sizes, 0.18 * min(W, H), 0.40 * min(W, H))    # readable next to a small cup, not huge
    pops = presence(visible, fps, appear)
    for i in range(T):
        t = max(0.0, i / fps - appear)
        img, ang = cartoon_frame(name, t, i, sizes[i], pops[i])
        if img is None:
            yield frames[i]
            continue
        dx, dy = _orbit(name, t)
        c = centers[i] + np.array([dx, dy]) * sizes[i]
        c = np.clip(c, [img.width / 2 + 4, img.height / 2 + 4], [W - img.width / 2 - 4, H - img.height / 2 - 4])
        yield composite(frames[i], img, c, ang)


# ----------------------------------------------------------------------------- free decoration

def _busy(frame: np.ndarray) -> np.ndarray:
    """Detail map (0..1): where the picture is busy; decorations go where it is quiet."""
    g = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY).astype(np.float32)
    mag = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    mag = cv2.GaussianBlur(mag, (0, 0), 0.03 * max(frame.shape[:2]))
    return mag / (mag.max() + 1e-6)


def free_path(frames: np.ndarray, fps: float, name: str, appear: float = 0.15):
    """Per-frame (centre, size) of a screen-space decoration, plus the travel direction (+1 / -1)."""
    T, H, W, _ = frames.shape
    busy = _busy(frames[min(T - 1, int(appear * fps))])
    s = min(W, H)
    t = np.arange(T) / fps
    if name in ("car", "plane"):
        size = (0.30 if name == "car" else 0.32) * s
        bands = {"bottom": (0.80, 0.94), "top": (0.06, 0.22)} if name == "car" else {"top": (0.08, 0.26), "bottom": (0.72, 0.90)}
        band = min(bands, key=lambda b: busy[int(bands[b][0] * H):int(bands[b][1] * H)].mean()
                   + (0.0 if list(bands).index(b) == 0 else 0.05))        # prefer the natural band
        y = (sum(bands[band]) / 2) * H
        # quieter half is where it starts; it travels across the whole width during the shot
        left = busy[:, : W // 2].mean() <= busy[:, W // 2:].mean()
        u = np.clip((t - appear) / max(0.6, t[-1] - appear), 0, 1)
        x0, x1 = -0.6 * size, W + 0.6 * size
        x = x0 + (x1 - x0) * u if left else x1 - (x1 - x0) * u
        dy = (-0.10 * s * u if name == "plane" and band == "top" else 0.0)
        centers = np.stack([x, np.full(T, y) + dy], 1)
        return centers, np.full(T, size), (1 if left else -1)
    size = 0.24 * s
    m = 0.06 * s + size / 2
    corners = {(m, m): None, (W - m, m): None, (m, H - m): None, (W - m, H - m): None}
    best = min(corners, key=lambda c: busy[int(max(0, c[1] - size / 2)):int(c[1] + size / 2),
                                           int(max(0, c[0] - size / 2)):int(c[0] + size / 2)].mean())
    centers = np.tile(np.array(best, float), (T, 1))
    centers[:, 1] += -0.04 * s * np.sin(2 * np.pi * 0.7 * t)               # gentle float
    return centers, np.full(T, size), 1


def apply_free(frames: np.ndarray, fps: float, name: str, appear: float = 0.15):
    T, H, W, _ = frames.shape
    centers, sizes, direction = free_path(frames, fps, name, appear)
    travelling = name in ("car", "plane")
    pops = np.ones(T) if travelling else presence(np.ones(T, bool), fps, appear)
    if travelling:
        pops[: int(round(appear * fps))] = 0.0
    for i in range(T):
        t = max(0.0, i / fps - appear)
        img, ang = cartoon_frame(name, t, i, sizes[i], pops[i])
        if img is None:
            yield frames[i]
            continue
        if direction < 0:                    # drawn facing right; mirror when travelling left
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
            ang = -ang
        yield composite(frames[i], img, centers[i], ang)
