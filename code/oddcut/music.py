"""Music analysis: tempo, beats, and the grid of allowed cut points."""
from dataclasses import dataclass

import librosa
import numpy as np

SR = 22050
HOP = 512


@dataclass
class Music:
    path: str
    start: float           # offset into the file where the edit begins
    duration: float        # usable length after `start`
    tempo: float
    beat_period: float
    grid: np.ndarray       # allowed cut times (seconds, relative to `start`)
    strength: np.ndarray   # normalized onset strength at each grid point (0..1)
    on_beat: np.ndarray    # True for full beats, False for half-beats


def _best_section_start(y: np.ndarray, beats: np.ndarray, length: float) -> float:
    """Start (on a beat) of the loudest `length`-second stretch: skips beat-less intros, lands on the hook."""
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    t = librosa.times_like(rms, sr=SR, hop_length=HOP)
    total = len(y) / SR
    best, best_score = float(beats[0]), -1.0
    for b in beats:
        if b + length > total:
            break
        window = (t >= b) & (t < b + length)
        beat_cover = np.sum((beats >= b) & (beats < b + length)) * np.median(np.diff(beats)) / length
        score = float(rms[window].mean()) * min(1.0, beat_cover)
        if score > best_score:
            best, best_score = float(b), score
    return best


def analyze_music(path: str, start: float | None = None, half_beat_below_bpm: float = 100.0,
                  bpm_range: tuple[float, float] = (75.0, 175.0), section_length: float = 20.0) -> Music:
    """`start=None` picks the loudest beat-covered section automatically."""
    y, sr = librosa.load(path, sr=SR, mono=True)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=HOP)
    tempo = float(np.atleast_1d(tempo)[0])
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
    if len(beats) < 4:
        raise RuntimeError(f"could not find a steady beat in {path}")
    # Beat trackers often lock onto half or double tempo; fold into the template's range.
    while tempo < bpm_range[0] and len(beats) > 1:
        beats = np.sort(np.concatenate([beats, beats[:-1] + np.diff(beats) / 2]))
        tempo *= 2
    while tempo > bpm_range[1] and len(beats) > 8:
        beats = beats[::2]
        tempo /= 2
    # Spectral-flux onsets peak ~1 hop after the actual hit (measured +25~34ms on test drums).
    beats = np.maximum(beats - HOP / SR, 0.0)
    if start is None:
        start = _best_section_start(y, beats, section_length)
    beats = beats[beats >= start - 1e-3] - start
    duration = len(y) / sr - start
    if len(beats) < 4:
        raise RuntimeError(f"fewer than 4 beats after {start:.2f}s in {path}")
    period = float(np.median(np.diff(beats)))

    grid, on_beat = list(beats), [True] * len(beats)
    if tempo < half_beat_below_bpm:   # slow songs: also allow cutting on the "and"
        halves = beats[:-1] + np.diff(beats) / 2
        grid += list(halves)
        on_beat += [False] * len(halves)
    order = np.argsort(grid)
    grid = np.asarray(grid)[order]
    on_beat = np.asarray(on_beat)[order]

    env_t = librosa.times_like(onset_env, sr=sr, hop_length=HOP) - HOP / SR - start
    strength = np.interp(grid, env_t, onset_env)
    strength = strength / max(strength.max(), 1e-6)
    return Music(path=path, start=start, duration=duration, tempo=tempo, beat_period=period,
                 grid=grid, strength=strength, on_beat=on_beat)
