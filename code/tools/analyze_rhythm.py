"""Analyze music tempo/beats and how shot cuts align with beats.

Usage: python analyze_rhythm.py <video1> [<video2> ...]
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import librosa
import numpy as np


def detect_cuts(video: str, threshold: float = 8.0) -> list[float]:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", video,
         "-vf", f"scale=180:-2,scdet=threshold={threshold}", "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
    ).stderr
    return [float(t) for t in re.findall(r"lavfi\.scd\.time:\s*([0-9.]+)", out)]


def extract_audio(video: str, wav: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video,
                    "-ac", "1", "-ar", "22050", wav], check=True)


def analyze(video: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        wav = str(Path(td) / "a.wav")
        extract_audio(video, wav)
        y, sr = librosa.load(wav, sr=None)

    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    tempo = float(np.atleast_1d(tempo)[0])
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units="time")
    rms = librosa.feature.rms(y=y)[0]
    rms_t = librosa.times_like(rms, sr=sr)

    cuts = detect_cuts(video)
    beat_period = 60.0 / tempo if tempo else 0.5
    dists = [float(np.min(np.abs(beats - c))) for c in cuts] if len(beats) else []
    on_beat = sum(d <= 0.08 for d in dists)
    half = beat_period / 2
    half_grid = np.concatenate([beats, beats[:-1] + np.diff(beats) / 2]) if len(beats) > 1 else beats
    on_half = sum(float(np.min(np.abs(half_grid - c))) <= 0.08 for c in cuts) if len(half_grid) else 0
    shot_lens = np.diff([0.0] + cuts)

    print(f"== {Path(video).name}")
    print(f"duration {len(y)/sr:.2f}s  tempo {tempo:.1f} BPM  beat {beat_period:.3f}s  beats {len(beats)}  onsets {len(onsets)}")
    print("beats :", " ".join(f"{b:.2f}" for b in beats))
    print("cuts  :", " ".join(f"{c:.2f}" for c in cuts))
    if cuts:
        print(f"cuts on beat (±80ms): {on_beat}/{len(cuts)}   on beat or half-beat: {on_half}/{len(cuts)}")
        print(f"shot length median {np.median(shot_lens):.2f}s  min {shot_lens.min():.2f}  max {shot_lens.max():.2f}")
    # loudness curve in 1s buckets (to see intro/drop/outro shape)
    buckets = [float(rms[(rms_t >= s) & (rms_t < s + 1)].mean()) for s in range(int(len(y) / sr))]
    peak = max(buckets) or 1
    print("loudness/s:", " ".join(f"{b/peak:.1f}" for b in buckets))
    print()


if __name__ == "__main__":
    for v in sys.argv[1:]:
        analyze(v)
