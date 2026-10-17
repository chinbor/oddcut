"""Render the planned shots: per-shot trim + crop to the output aspect + grade, concat, then lay the music under it."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .ffmpeg_utils import run
from .formats import output_size
from .planner import Shot
from .progress import Reporter
from .sticker_stage import render_drawn_source, render_sticker_source


def _escape_filter_path(p: str) -> str:
    return "'" + str(Path(p).resolve()).replace("\\", "/").replace(":", "\\:") + "'"


def grade_filter(grade: dict, template_dir: Path) -> str:
    """Moody film look: slightly darker, more contrast, warm mids, lifted blacks, grain, vignette."""
    lut = grade.get("lut")
    if lut and (template_dir / lut).exists():
        chain = [f"lut3d=file={_escape_filter_path(str(template_dir / lut))}"]
    else:
        chain = [
            f"eq=contrast={grade.get('contrast', 1.08)}:brightness={grade.get('exposure', -0.05) * 0.2:.3f}:saturation=0.92",
            "colorbalance=rs=0.03:bs=-0.04:rm=0.04:gm=0.01:bm=-0.04:rh=0.02:bh=-0.02",
            "curves=all='0/0.06 0.25/0.25 0.75/0.77 1/0.96'",
        ]
    chain.append("vignette=PI/7")
    grain = grade.get("grain", 0)
    if grain:
        chain.append(f"noise=alls={int(grain * 100)}:allf=t")
    return ",".join(chain)


def _render_shot(i: int, shot: Shot, src: tuple[str, float], out_dir: Path, w: int, h: int, fps: float,
                 grade: str, fade_out: float) -> Path:
    """`src` = (path, start): the original clip, or a pre-rendered sticker clip starting at 0."""
    out = out_dir / f"shot_{i:03d}.mp4"
    vf = [
        f"fps={fps}",
        # one colour tag for every shot: sticker / drawn intermediates carry none, camera files say bt709;
        # mixed tags make concat players and filter graphs re-initialise mid-stream
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos",
        f"crop={w}:{h}",
        "setsar=1",
        grade,
        "tpad=stop_mode=clone:stop=15",   # never run short at the end of a source clip
    ]
    if fade_out:
        dur = shot.frames / fps
        vf.append(f"fade=t=out:st={max(0.0, dur - fade_out):.3f}:d={fade_out:.3f}")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{src[1]:.3f}", "-i", src[0],
         "-vf", ",".join(vf), "-frames:v", str(shot.frames), "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
         "-color_range", "tv", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         "-r", str(fps), "-video_track_timescale", "90000", str(out)])
    return out


def render(shots: list[Shot], music, tpl: dict, template_dir: Path,
           out_path: Path, work_dir: Path, report: Reporter | None = None) -> float:
    """`music` needs .path and .start (a Music, or the MusicRef a saved plan loads into)."""
    report = report or Reporter(None)
    w, h = output_size(tpl)
    fps = float(tpl["output"]["fps"])
    grade = grade_filter(tpl.get("grade", {}), template_dir)
    outro = next(s for s in tpl["structure"] if s["section"] == "outro")
    fade = float(outro.get("fade_out", 0.6))
    work_dir.mkdir(parents=True, exist_ok=True)

    # Sticker shots first (sequential: the detector is already multithreaded). They are cropped to the
    # output aspect before tracking so the sticker can't land in the part the crop throws away.
    sources = [(s.clip.path, s.src_start) for s in shots]
    effects = [i for i, s in enumerate(shots) if s.drawn or s.sticker]
    report("render", "effects", 0, len(effects))
    for n, i in enumerate(effects):
        s = shots[i]
        if s.drawn:
            tmp = work_dir / f"drawn_{i:03d}.mp4"
            if render_drawn_source(s.clip.path, s.src_start, s.drawn["reveal_at"], s.drawn["tail"] + 0.15, tmp,
                                   tpl.get("drawn_opening", {}).get("background", "real"), w / h, h):
                sources[i] = (str(tmp), 0.0)
            else:
                report("render", "effects", n, len(effects),
                       f"drawn opening not possible on {s.clip.name}: falling back to a plain hook")
                s.drawn = None
        else:
            tmp = work_dir / f"sticker_{i:03d}.mp4"
            if render_sticker_source(s.clip.path, s.src_start, s.frames / fps + 0.3, s.sticker, tmp, w / h):
                sources[i] = (str(tmp), 0.0)
            else:
                report("render", "effects", n, len(effects),
                       f"sticker '{s.sticker}' not placed on shot {i} ({s.clip.name}): target not found")
                s.sticker = None
        report("render", "effects", n + 1, len(effects))

    report("render", "shots", 0, len(shots))
    parts: list[Path | None] = [None] * len(shots)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_render_shot, i, s, sources[i], work_dir, w, h, fps, grade,
                               fade if s.role == "outro" else 0.0): i for i, s in enumerate(shots)}
        try:
            for n, fut in enumerate(as_completed(futures)):
                parts[futures[fut]] = fut.result()
                report("render", "shots", n + 1, len(shots))
        except BaseException:              # failed or cancelled: don't start the shots still queued
            for fut in futures:
                fut.cancel()
            raise

    concat_list = work_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    video_only = work_dir / "video_only.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c", "copy", str(video_only)])

    report("render", "mux", 0, 1)
    total = sum(s.frames for s in shots) / fps
    afade = min(0.8, total / 4)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video_only),
         "-ss", f"{music.start:.3f}", "-i", music.path,
         "-map", "0:v", "-map", "1:a", "-t", f"{total:.3f}",
         "-af", f"afade=t=out:st={total - afade:.3f}:d={afade:.3f}",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)])
    report("render", "mux", 1, 1)
    return total
