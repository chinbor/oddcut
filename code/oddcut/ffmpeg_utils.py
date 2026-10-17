"""Thin wrappers around ffprobe / ffmpeg."""
import json
import subprocess
from dataclasses import dataclass

import numpy as np


@dataclass
class ProbeInfo:
    duration: float
    width: int   # display width (after rotation)
    height: int  # display height (after rotation)
    fps: float


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{proc.stderr[-2000:]}")


def probe(path: str) -> ProbeInfo:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate:stream_side_data=rotation:format=duration",
         "-of", "json", path],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="ignore", check=True,
    ).stdout
    data = json.loads(out)
    st = data["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    rotation = 0
    for sd in st.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    if abs(rotation) % 180 == 90:
        w, h = h, w
    num, den = st.get("r_frame_rate", "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return ProbeInfo(duration=float(data["format"]["duration"]), width=w, height=h, fps=fps)


def read_frames(path: str, info: ProbeInfo, sample_fps: float = 4.0, width: int = 160) -> np.ndarray:
    """Decode the clip at a low frame rate / resolution. Returns (N, H, W, 3) uint8 RGB."""
    height = max(2, int(round(width * info.height / info.width / 2)) * 2)
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path,
         "-vf", f"fps={sample_fps},scale={width}:{height}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdin=subprocess.DEVNULL, capture_output=True, check=True,
    )
    frame_bytes = width * height * 3
    n = len(proc.stdout) // frame_bytes
    return np.frombuffer(proc.stdout[: n * frame_bytes], dtype=np.uint8).reshape(n, height, width, 3)
