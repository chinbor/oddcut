"""Per-clip analysis: sharpness, exposure, motion, color and subject position over time."""
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .ffmpeg_utils import probe, read_frames

SAMPLE_FPS = 4.0
EDGE_MARGIN = 0.25          # seconds skipped at clip start/end (camera start/stop wobble)
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}

# Calibrated on 160px-wide frames: log10(laplacian variance) of the sharpest 3x3 tile.
SHARP_FLOOR = 1.8           # below this a frame counts as blurry (real phone footage ~2.8-3.4)
REJECT_SHARP = 2.2          # clip rejected if its 75th-percentile sharpness is below this
PHASE_CONFIDENCE = 0.08      # min phase-correlation peak to trust a global shift estimate
SHAKE_JERK = 0.03           # change of global shift between samples (fraction of width) above which it's shaky


@dataclass
class ClipAnalysis:
    path: str
    duration: float
    width: int
    height: int
    times: np.ndarray
    sharp: np.ndarray
    bright: np.ndarray
    motion: np.ndarray       # mean abs frame difference (subject/camera activity)
    shake: np.ndarray        # jerk of the global camera shift (handheld jitter)
    color: np.ndarray        # (N, 14): 12 saturation-weighted hue bins + mean sat + mean value
    subject: np.ndarray      # (N, 2): normalized (x, y) center of edge energy
    frame_score: np.ndarray
    rejected: bool = False
    reason: str = ""
    used: list[tuple[float, float]] = field(default_factory=list)
    tags: list = field(default_factory=list)   # sticker_stage: per 1fps sample (t, family, name, area) | None
    drawable: list = field(default_factory=list)   # sticker_stage: drawn-reveal starts (t, label, family, area, conf)

    @property
    def name(self) -> str:
        return Path(self.path).name

    def _idx(self, t: float) -> int:
        return int(np.clip(round(t * SAMPLE_FPS), 0, len(self.times) - 1))

    def window_score(self, start: float, length: float) -> float:
        i0, i1 = self._idx(start), self._idx(start + length)
        return float(self.frame_score[i0 : i1 + 1].mean())

    def stability(self, start: float, length: float) -> float:
        i0, i1 = self._idx(start), self._idx(start + length)
        return float(1.0 - min(1.0, self.shake[i0 : i1 + 1].mean() / SHAKE_JERK))

    def brightness(self, start: float, length: float) -> float:
        i0, i1 = self._idx(start), self._idx(start + length)
        return float(self.bright[i0 : i1 + 1].mean())

    def feature_at(self, t: float) -> tuple[np.ndarray, np.ndarray]:
        i = self._idx(t)
        return self.color[i], self.subject[i]

    def candidate_starts(self, length: float) -> list[float]:
        margin = EDGE_MARGIN if self.duration >= length + 2 * EDGE_MARGIN else 0.0
        last = self.duration - margin - length
        if last < margin - 1e-6:
            return []
        step = 1.0 / SAMPLE_FPS
        starts = list(np.arange(margin, last + 1e-6, step)) or [margin]
        return [float(s) for s in starts
                if all(s + length <= a or s >= b for a, b in self.used)]

    def best_window(self, length: float) -> tuple[float, float] | None:
        best = None
        for s in self.candidate_starts(length):
            sc = self.window_score(s, length)
            if best is None or sc > best[1]:
                best = (s, sc)
        return best


def _frame_features(frames: np.ndarray):
    f = frames.astype(np.float32) / 255.0
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    gray = 0.299 * r + 0.587 * g + 0.114 * b

    lap = (4 * gray[:, 1:-1, 1:-1] - gray[:, :-2, 1:-1] - gray[:, 2:, 1:-1]
           - gray[:, 1:-1, :-2] - gray[:, 1:-1, 2:]) * 255.0
    # Sharpness of the sharpest 3x3 tile: shallow depth of field (sharp subject, soft background)
    # and minimal scenes (one object on a plain wall) count as sharp; out-of-focus shots blur every tile.
    n, lh, lw = lap.shape
    th, tw = lh // 3, lw // 3
    tiles = (lap[:, : th * 3, : tw * 3].reshape(n, 3, th, 3, tw)
             .transpose(0, 1, 3, 2, 4).reshape(n, 9, -1).var(axis=2))
    sharp = np.log10(tiles.max(axis=1) + 1.0)

    bright = gray.reshape(len(f), -1).mean(axis=1)

    diff = np.abs(np.diff(gray, axis=0)).reshape(len(f) - 1, -1).mean(axis=1) if len(f) > 1 else np.zeros(0)
    motion = np.concatenate([diff[:1], diff]) if len(diff) else np.zeros(len(f))

    maxc, minc = f.max(axis=-1), f.min(axis=-1)
    delta = maxc - minc
    sat = np.where(maxc > 1e-6, delta / np.maximum(maxc, 1e-6), 0.0)
    safe = np.maximum(delta, 1e-6)
    hue = np.where(maxc == r, ((g - b) / safe) % 6,
          np.where(maxc == g, (b - r) / safe + 2, (r - g) / safe + 4)) / 6.0
    weight = sat * maxc
    hist = np.stack([
        np.bincount(np.minimum((hue[i] * 12).astype(int), 11).ravel(),
                    weights=weight[i].ravel(), minlength=12)
        for i in range(len(f))
    ])
    hist = hist / np.maximum(hist.sum(axis=1, keepdims=True), 1e-6)
    color = np.concatenate([hist, sat.reshape(len(f), -1).mean(1, keepdims=True),
                            maxc.reshape(len(f), -1).mean(1, keepdims=True)], axis=1)

    energy = np.abs(lap)
    h, w = energy.shape[1:]
    total = np.maximum(energy.sum(axis=(1, 2)), 1e-6)
    cx = (energy.sum(axis=1) * np.arange(w)).sum(axis=1) / total / w
    cy = (energy.sum(axis=2) * np.arange(h)).sum(axis=1) / total / h
    subject = np.stack([cx, cy], axis=1)
    return sharp, bright, motion, _shake(gray), color, subject


def _global_shifts(gray: np.ndarray) -> np.ndarray:
    """Phase-correlation translation between consecutive frames, as a fraction of frame size. (N, 2)
    Rows are NaN where the two frames do not correlate (a cut, or the scene changed too much to tell)."""
    n, h, w = gray.shape
    win = np.outer(np.hanning(h), np.hanning(w))
    spec = np.fft.fft2((gray - gray.mean(axis=(1, 2), keepdims=True)) * win)
    shifts = np.zeros((n, 2))
    for i in range(1, n):
        cross = spec[i] * np.conj(spec[i - 1])
        corr = np.abs(np.fft.ifft2(cross / np.maximum(np.abs(cross), 1e-9)))
        py, px = np.unravel_index(np.argmax(corr), corr.shape)
        if corr[py, px] < PHASE_CONFIDENCE:
            shifts[i] = np.nan
            continue
        shifts[i] = [(px + w // 2) % w - w // 2, (py + h // 2) % h - h // 2]
    shifts[:, 0] /= w
    shifts[:, 1] /= h
    return shifts


def _shake(gray: np.ndarray) -> np.ndarray:
    """Smooth pans have a steady shift; handheld jitter makes the shift jump around."""
    shifts = _global_shifts(gray)
    jerk = np.linalg.norm(np.diff(shifts, axis=0), axis=1) if len(shifts) > 1 else np.zeros(0)
    jerk = np.nan_to_num(jerk, nan=0.0)          # unknown steps don't count as shake
    return np.concatenate([jerk[:1], jerk]) if len(jerk) else np.zeros(len(gray))


def _score(sharp, bright, motion, shake) -> np.ndarray:
    sharp_term = np.clip((sharp - SHARP_FLOOR) / 1.2, 0.0, 1.5)
    dark = np.maximum(0.0, 0.08 - bright) / 0.08
    blown = np.maximum(0.0, bright - 0.92) / 0.08
    shaky = np.maximum(0.0, shake - SHAKE_JERK) * 20.0
    action = np.minimum(motion, 0.06) * 3.0        # a little movement keeps shots alive
    return sharp_term + action - dark - blown - shaky


def analyze_clip(path: str) -> ClipAnalysis:
    info = probe(path)
    frames = read_frames(path, info, SAMPLE_FPS)
    if len(frames) == 0:
        raise RuntimeError(f"no frames decoded from {path}")
    sharp, bright, motion, shake, color, subject = _frame_features(frames)
    clip = ClipAnalysis(
        path=path, duration=info.duration, width=info.width, height=info.height,
        times=np.arange(len(frames)) / SAMPLE_FPS, sharp=sharp, bright=bright, motion=motion,
        shake=shake, color=color, subject=subject, frame_score=_score(sharp, bright, motion, shake),
    )
    if info.duration < 0.6:
        clip.rejected, clip.reason = True, "too short"
    elif np.percentile(sharp, 75) < REJECT_SHARP:
        clip.rejected, clip.reason = True, f"blurry (sharp p75={np.percentile(sharp, 75):.2f})"
    elif np.median(shake) > SHAKE_JERK * 1.5:
        clip.rejected, clip.reason = True, f"too shaky (jerk median={np.median(shake):.3f})"
    elif np.median(bright) < 0.035:                # moody/dark shots are part of the style
        clip.rejected, clip.reason = True, "too dark"
    return clip


def list_clips(folder: str) -> list[str]:
    paths = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if not paths:
        raise RuntimeError(f"no video files in {folder}")
    return [str(p) for p in paths]


def analyze_folder(folder: str, on_clip=None) -> list[ClipAnalysis]:
    """`on_clip(done, total, clip)` is called after each clip (progress reporting)."""
    paths = list_clips(folder)
    clips = []
    for i, p in enumerate(paths):
        clips.append(analyze_clip(p))
        if on_clip:
            on_clip(i + 1, len(paths), clips[-1])
    return clips
