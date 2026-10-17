"""Render the planned shots: per-shot trim + crop to the output aspect + grade, concat, then lay the music under it."""
import functools
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .ffmpeg_utils import run
from .formats import output_size
from .planner import Shot
from .progress import Reporter
from .sticker_stage import render_drawn_source, render_sticker_source


def _escape_filter_path(p: str) -> str:
    return "'" + str(Path(p).resolve()).replace("\\", "/").replace(":", "\\:") + "'"


PREVIEW_SHORT_SIDE = 360   # preview renders: 9:16 -> 360x640


def preview_size(w: int, h: int) -> tuple[int, int]:
    k = PREVIEW_SHORT_SIDE / min(w, h)
    return max(2, int(round(w * k / 2)) * 2), max(2, int(round(h * k / 2)) * 2)


# the code that draws each kind of effect shot: part of the cache key, so editing it never reuses stale shots
_EFFECT_CODE = {"sticker": ("stickers.py",), "cartoon": ("stickers.py", "cartoons.py"), "drawn": ("drawn_reveal.py",)}


@functools.lru_cache(maxsize=None)
def effect_rev(kind: str) -> str:
    """Hash of the drawing code for an effect kind (line endings ignored)."""
    h = hashlib.sha1()
    for name in _EFFECT_CODE[kind]:
        h.update(Path(__file__).with_name(name).read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def _effect_key(*parts) -> str:
    """Cache key of a rendered sticker / drawn shot: the source file (path, size, mtime) + every parameter."""
    st = os.stat(parts[0])
    return hashlib.sha1(json.dumps([os.path.abspath(parts[0]), st.st_size, st.st_mtime_ns, *parts[1:]],
                                   default=str).encode()).hexdigest()[:20]


def grade_filter(grade: dict, template_dir: Path, grain: bool = True) -> str:
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
    amount = grade.get("grain", 0) if grain else 0
    if amount:
        chain.append(f"noise=alls={int(amount * 100)}:allf=t")
    return ",".join(chain)


def _render_shot(i: int, shot: Shot, src: tuple[str, float], out_dir: Path, w: int, h: int, fps: float,
                 grade: str, fade_out: float, preview: bool = False) -> Path:
    """`src` = (path, start): the original clip, or a pre-rendered sticker clip starting at 0."""
    out = out_dir / f"shot_{i:03d}.mp4"
    vf = [
        f"fps={fps}",
        # one colour tag for every shot: sticker / drawn intermediates carry none, camera files say bt709;
        # mixed tags make concat players and filter graphs re-initialise mid-stream
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        f"scale={w}:{h}:force_original_aspect_ratio=increase:flags={'bilinear' if preview else 'lanczos'}",
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
         "-c:v", "libx264", "-preset", "ultrafast" if preview else "veryfast", "-crf", "28" if preview else "18",
         "-pix_fmt", "yuv420p",
         "-color_range", "tv", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         "-r", str(fps), "-video_track_timescale", "90000", str(out)])
    return out


def _keep(tmp: Path, cached: Path | None) -> Path:
    """Move a freshly rendered effect shot into the cache (if there is one) and return where it is."""
    if cached is None:
        return tmp
    cached.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, cached)
    return cached


def render(shots: list[Shot], music, tpl: dict, template_dir: Path,
           out_path: Path, work_dir: Path, report: Reporter | None = None,
           preview: bool = False, effects_cache: Path | None = None) -> float:
    """`music` needs .path and .start (a Music, or the MusicRef a saved plan loads into).
    `preview`: small and fast; sticker / drawn shots come from `effects_cache` if there, else plain.
    `effects_cache`: rendered sticker / drawn shots are kept there and reused by later renders."""
    report = report or Reporter(None)
    full_w, full_h = output_size(tpl)
    w, h = preview_size(full_w, full_h) if preview else (full_w, full_h)
    fps = float(tpl["output"]["fps"])
    grade = grade_filter(tpl.get("grade", {}), template_dir, grain=not preview)
    outro = next(s for s in tpl["structure"] if s["section"] == "outro")
    fade = float(outro.get("fade_out", 0.6))
    work_dir.mkdir(parents=True, exist_ok=True)

    # Sticker shots first (sequential: the detector is already multithreaded). They are cropped to the
    # output aspect before tracking so the sticker can't land in the part the crop throws away.
    sources = [(s.clip.path, s.src_start) for s in shots]
    effects = [i for i, s in enumerate(shots) if s.drawn or s.sticker]
    report("render", "effects", 0, len(effects))
    background = tpl.get("drawn_opening", {}).get("background", "real")
    for n, i in enumerate(effects):
        s = shots[i]
        if s.drawn:
            style = s.drawn.get("style", "lineart")
            params = ("drawn", s.src_start, s.drawn["reveal_at"], s.drawn["tail"] + 0.15, background, full_w, full_h)
            params += (style, effect_rev("drawn"))
        else:
            params = ("sticker", s.src_start, s.frames / fps + 0.3, s.sticker, full_w, full_h,
                      effect_rev("cartoon" if "@" in s.sticker else "sticker"))
        cached = effects_cache / f"{_effect_key(s.clip.path, *params)}.mp4" if effects_cache else None
        if cached is not None and cached.exists():
            sources[i] = (str(cached), 0.0)
        elif preview:
            pass                            # preview: plain footage in its place
        elif s.drawn:
            tmp = work_dir / f"drawn_{i:03d}.mp4"
            if render_drawn_source(s.clip.path, s.src_start, s.drawn["reveal_at"], s.drawn["tail"] + 0.15, tmp,
                                   background, full_w / full_h, full_h, style):
                sources[i] = (str(_keep(tmp, cached)), 0.0)
            else:
                report("render", "effects", n, len(effects),
                       f"drawn opening not possible on {s.clip.name}: falling back to a plain hook")
                s.drawn = None
        else:
            tmp = work_dir / f"sticker_{i:03d}.mp4"
            if render_sticker_source(s.clip.path, s.src_start, s.frames / fps + 0.3, s.sticker, tmp, full_w / full_h):
                sources[i] = (str(_keep(tmp, cached)), 0.0)
            else:
                report("render", "effects", n, len(effects),
                       f"sticker '{s.sticker}' not placed on shot {i} ({s.clip.name}): target not found")
                s.sticker = None
        report("render", "effects", n + 1, len(effects))

    report("render", "shots", 0, len(shots))
    parts: list[Path | None] = [None] * len(shots)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_render_shot, i, s, sources[i], work_dir, w, h, fps, grade,
                               fade if s.role == "outro" else 0.0, preview): i for i, s in enumerate(shots)}
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
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k" if preview else "192k", "-movflags", "+faststart", str(out_path)])
    report("render", "mux", 1, 1)
    return total
