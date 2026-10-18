"""素材 after the edit was made: 导入 (videos, pictures as still videos, songs), a 素材 dropped on the 主轨,
删除 a 主轨 piece (the pieces after it close up), and taking a 素材 out of the project.
"""
from dataclasses import replace
from pathlib import Path

import numpy as np

from .planner import Shot
from .plan_data import Analysis, ClipRef, Plan, _cached_clip
from .edits import MIN_SHOT
from .overlays import _check_overlays


VIDEO_IN = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
IMAGE_IN = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
AUDIO_IN = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_SECONDS = 5.0


def import_media(a: Analysis, paths: list[str], work: Path, cache_dir: str | Path | None = None
                 ) -> tuple[Analysis, list[str], list[str]]:
    """导入, after the edit was made: videos join the project's 素材 (looked at like the first ones), a picture
    becomes a 5-second still video next to the project so it works as footage everywhere, songs are handed back
    for the 音乐 list. Returns (analysis, notes, songs)."""
    import subprocess
    from .analyze import ClipAnalysis  # noqa: F401 - the clip type the list holds
    have = {str(Path(c.path).resolve()) for c in a.clips}
    clips, notes, songs = list(a.clips), [], []
    for src in paths:
        f = Path(src)
        ext = f.suffix.lower()
        if ext in AUDIO_IN:
            songs.append(str(f))
            continue
        if ext in IMAGE_IN:
            out = Path(work) / "imported" / f"{f.stem}_still.mp4"
            if not out.exists():
                out.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(f), "-t", f"{IMAGE_SECONDS}", "-r", "30",
                                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-c:v", "libx264", "-crf", "18",
                                str(out)], stdin=subprocess.DEVNULL, capture_output=True, check=True)
            f = out
        elif ext not in VIDEO_IN:
            notes.append(f"「{f.name}」不是视频、图片或音乐，没有导入")
            continue
        if str(f.resolve()) in have:
            notes.append(f"「{f.name}」已经在素材里了")
            continue
        c = _cached_clip(str(f), cache_dir)
        c.rejected, c.reason = False, ""            # the user chose it: never left out for being soft or dark
        clips.append(c)
        have.add(str(f.resolve()))
    return replace(a, clips=clips), notes, songs


def _shift_after(p: Plan, edge: float, d: float, shots: list) -> Plan:
    """Everything that starts at or after timeline time `edge` moves by d: shots (already in `shots`), 画中画
    and 万物生长. Returns the plan with the new shots."""
    looks = dict(p.looks)
    looks["overlays"] = [({**o, "t_start": o["t_start"] + d, "t_end": o["t_end"] + d} if o["t_start"] >= edge - 1e-3 else o)
                         for o in looks.get("overlays") or []]
    looks["grow_presets"] = [({**g, "cut": round(g["cut"] + d, 4), "land": round(g["land"] + d, 4)} if g["cut"] >= edge - 1e-3 else g)
                             for g in looks.get("grow_presets") or []]
    looks["edited_cuts"] = True
    return replace(p, shots=shots, looks=looks)


def insert_shot(analysis: Analysis, p: Plan, t: float, clip_path: str) -> tuple[Plan, list[str]]:
    """A 素材 dropped on the 主轨 at timeline time `t`: it goes in at the nearest cut (one bar of the music long,
    or the whole clip when that is shorter, from its best-looking part) and everything after it moves back."""
    fps = float(p.template["output"]["fps"])
    c = next((x for x in analysis.clips if x.path == clip_path), None)
    if c is None:
        raise ValueError("这个素材不在项目里，先导入")
    k = len(p.shots)
    for i, s in enumerate(p.shots):
        if t < (s.t_start + s.t_end) / 2:
            k = i
            break
    t0 = p.shots[k].t_start if k < len(p.shots) else p.shots[-1].t_end
    length = min(4 * float(p.music.beat_period), c.duration - 0.1)
    n = int(round(length * fps))
    if n < int(np.ceil(MIN_SHOT * fps)):
        raise ValueError("这个素材太短了（至少 0.4 秒）")
    length = n / fps
    starts = c.candidate_starts(length) if len(c.times) else []
    src = max(starts, key=lambda s: c.window_score(s, length)) if starts else 0.0
    new = Shot(role="body", clip=ClipRef(c.path), src_start=float(src), t_start=t0, t_end=t0 + length, frames=n)
    after = [replace(x, t_start=x.t_start + length, t_end=x.t_end + length) for x in p.shots[k:]]
    q = _shift_after(p, t0, length, list(p.shots[:k]) + [new] + after)
    looks = dict(q.looks)
    looks["overlays"] = _check_overlays(analysis, q.duration, looks.get("overlays") or [])
    return replace(q, looks=looks), []


def delete_shot(analysis: Analysis, p: Plan, index: int) -> tuple[Plan, list[str]]:
    """删除 a 主轨 piece: the pieces after it close the gap (剪映's 主轨磁吸). A 万物生长 into or out of it goes too,
    and 画中画 that sat only over it; the others after it move up."""
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range")
    if len(p.shots) < 2:
        raise ValueError("主轨至少要留一段")
    s = p.shots[index]
    d = s.t_end - s.t_start
    gone = {g["id"] for g in p.looks.get("grow_presets") or [] if s.t_start - 1e-3 <= g["land"] <= s.t_end + 1e-3
            or s.t_start - 1e-3 <= g["cut"] <= s.t_end + 1e-3}
    looks = dict(p.looks)
    looks["grow_presets"] = [g for g in looks.get("grow_presets") or [] if g["id"] not in gone]
    looks["overlays"] = [o for o in looks.get("overlays") or []
                         if (o.get("group") or {}).get("id") not in gone
                         and not (o["t_start"] >= s.t_start - 1e-3 and o["t_end"] <= s.t_end + 1e-3)]
    rest = list(p.shots[:index]) + [replace(x, t_start=x.t_start - d, t_end=x.t_end - d) for x in p.shots[index + 1:]]
    if index == 0 and rest:
        rest[0] = replace(rest[0], role="hook" if rest[0].role == "body" else rest[0].role)
    q = _shift_after(replace(p, looks=looks), s.t_end, -d, rest)
    looks = dict(q.looks)
    looks["overlays"] = _check_overlays(analysis, q.duration, looks.get("overlays") or [])
    return replace(q, looks=looks), []


def remove_clip(analysis: Analysis, p: Plan, clip_path: str, timeline: bool) -> tuple[Analysis, Plan, list[str]]:
    """Take a 素材 out of the project (the file on disk stays). timeline: its pieces on the 主轨 and its 画中画
    go too (the 主轨 closes up); else they stay as they are."""
    clips = [c for c in analysis.clips if c.path != clip_path]
    a = replace(analysis, clips=clips)
    if not timeline:
        return a, p, []
    q = p
    for i in range(len(q.shots) - 1, -1, -1):
        if q.shots[i].clip.path == clip_path:
            if len(q.shots) < 2:
                raise ValueError("主轨只剩这个素材了，不能全部删掉")
            q, _ = delete_shot(analysis, q, i)
    looks = dict(q.looks)
    looks["overlays"] = [o for o in looks.get("overlays") or [] if o.get("clip") != clip_path]
    return a, replace(q, looks=looks), []
