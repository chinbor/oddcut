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
from .sticker_stage import render_drawn_source, render_manual_sticker, render_sticker_source


def _escape_filter_path(p: str) -> str:
    return "'" + str(Path(p).resolve()).replace("\\", "/").replace(":", "\\:") + "'"


PREVIEW_SHORT_SIDE = 360   # preview renders: 9:16 -> 360x640


def preview_size(w: int, h: int) -> tuple[int, int]:
    k = PREVIEW_SHORT_SIDE / min(w, h)
    return max(2, int(round(w * k / 2)) * 2), max(2, int(round(h * k / 2)) * 2)


# the code that draws each kind of effect shot: part of the cache key, so editing it never reuses stale shots
_EFFECT_CODE = {"sticker": ("stickers.py",), "cartoon": ("stickers.py", "cartoons.py"), "drawn": ("drawn_reveal.py",),
                "manual": ("stickers.py", "cartoons.py", "sticker_stage.py")}


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


def _part_key(shot: Shot, src: tuple[str, float], w: int, h: int, fps: float, grade: str, fade_out: float,
              preview: bool) -> str:
    st = os.stat(src[0])
    return hashlib.sha1(json.dumps([os.path.abspath(src[0]), st.st_size, st.st_mtime_ns, round(src[1], 4), shot.frames,
                                    w, h, fps, grade, fade_out, preview]).encode()).hexdigest()[:20]


def _render_shot(i: int, shot: Shot, src: tuple[str, float], out_dir: Path, w: int, h: int, fps: float,
                 grade: str, fade_out: float, preview: bool = False, parts_cache: Path | None = None) -> Path:
    """`src` = (path, start): the original clip, or a pre-rendered sticker clip starting at 0.
    `parts_cache`: finished shots are kept there and reused (the app re-renders its preview after every edit;
    only the shots that changed are encoded again)."""
    cached = parts_cache / f"{_part_key(shot, src, w, h, fps, grade, fade_out, preview)}.mp4" if parts_cache else None
    if cached is not None and cached.exists():
        return cached
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
    return _keep(out, cached)


def _overlay_pass(video: Path, overlays: list[dict], w: int, h: int, fps: float, grade: str, preview: bool,
                  work_dir: Path, frames: int) -> Path:
    """Lay the picture-in-picture clips over the joined edit (lower track first, so higher ones end up on
    top): each is cut from its clip, filled into its box, given rounded corners / opacity (an alpha mask)
    and an optional white border, and shown from t_start to t_end."""
    from PIL import Image, ImageDraw
    args = ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    chains, last, n_in = [], "[0:v]", 1
    for k, o in enumerate(sorted(overlays, key=lambda o: (o["track"], o["t_start"]))):
        bw, bh = max(2, round(o["w"] * w / 2) * 2), max(2, round(o["h"] * h / 2) * 2)
        x, y = round(o["x"] * w), round(o["y"] * h)
        t0, t1 = float(o["t_start"]), float(o["t_end"])
        dur = t1 - t0
        r = int(float(o.get("radius", 0)) * min(bw, bh))
        alpha = int(255 * float(o.get("opacity", 1.0)))
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=r, fill=alpha)
        mask_png = work_dir / f"ov_mask_{k}.png"
        mask.save(mask_png)
        args += ["-ss", f"{float(o['src_start']):.3f}", "-t", f"{dur + 0.2:.3f}", "-i", o["clip"],
                 "-framerate", str(fps), "-loop", "1", "-t", f"{dur + 0.2:.3f}", "-i", str(mask_png)]
        vi, mi = n_in, n_in + 1
        n_in += 2
        on = f"enable='between(t,{t0:.3f},{t1:.3f})'"
        chains.append(
            f"[{vi}:v]fps={fps},setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
            f"scale={bw}:{bh}:force_original_aspect_ratio=increase:flags={'bilinear' if preview else 'lanczos'},"
            f"crop={bw}:{bh},setsar=1{',' + grade if grade else ''},format=yuva420p[c{k}];"
            f"[{mi}:v]format=gray[m{k}];[c{k}][m{k}]alphamerge,setpts=PTS-STARTPTS+{t0:.3f}/TB[o{k}];"
            f"{last}[o{k}]overlay={x}:{y}:eof_action=pass:{on}[b{k}]")
        last = f"[b{k}]"
        if o.get("border"):
            bd = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            ImageDraw.Draw(bd).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=r, outline=(255, 255, 255, alpha),
                                                 width=max(2, round(min(w, h) * 0.006)))
            bd_png = work_dir / f"ov_border_{k}.png"
            bd.save(bd_png)
            args += ["-framerate", str(fps), "-loop", "1", "-t", f"{dur + 0.2:.3f}", "-i", str(bd_png)]
            chains.append(f"[{n_in}:v]format=rgba,setpts=PTS-STARTPTS+{t0:.3f}/TB[d{k}];"
                          f"{last}[d{k}]overlay={x}:{y}:eof_action=pass:{on}[e{k}]")
            n_in += 1
            last = f"[e{k}]"
    out = work_dir / "video_overlays.mp4"
    run(args + ["-filter_complex", ";".join(chains), "-map", last, "-frames:v", str(frames), "-an",
                "-c:v", "libx264", "-preset", "ultrafast" if preview else "veryfast", "-crf", "28" if preview else "18",
                "-pix_fmt", "yuv420p", "-color_range", "tv", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-colorspace", "bt709", "-r", str(fps), str(out)])
    return out


def _keep(tmp: Path, cached: Path | None) -> Path:
    """Move a freshly rendered effect shot into the cache (if there is one) and return where it is."""
    if cached is None:
        return tmp
    cached.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, cached)
    return cached


def _is_manual(s: Shot) -> bool:
    return bool(s.sticker_at) or bool(s.drawn and s.drawn.get("manual"))


def _effect_params(s: Shot, tpl: dict, eh: int) -> tuple:
    """Everything a rendered sticker / drawn shot depends on (with the source file: its cache key).
    `eh`: the height hand-made effects are rendered at (preview size in previews)."""
    full_w, full_h = output_size(tpl)
    fps = float(tpl["output"]["fps"])
    background = tpl.get("drawn_opening", {}).get("background", "real")
    if s.drawn:
        params = ("drawn", s.src_start, s.drawn["reveal_at"], s.drawn["tail"] + 0.15, background, full_w, full_h,
                  s.drawn.get("style", "lineart"), effect_rev("drawn"))
        if _is_manual(s):
            params += (s.drawn.get("box"), bool(s.drawn.get("force")), eh)
        return params
    if s.sticker_at:
        return ("placed", s.src_start, s.frames / fps + 0.3, s.sticker, json.dumps(s.sticker_at, sort_keys=True),
                full_w, full_h, eh, effect_rev("manual"))
    return ("sticker", s.src_start, s.frames / fps + 0.3, s.sticker, full_w, full_h,
            effect_rev("cartoon" if "@" in s.sticker else "sticker"))


def effect_files(s: Shot, tpl: dict, effects_cache: Path | None, preview_h: int | None) -> list[Path]:
    """Cache files that could stand in for this shot's effect, best first. Hand-made effects exist at preview
    size and at full size; a preview can use either, a full render only the full-size one."""
    if effects_cache is None or not (s.drawn or s.sticker):
        return []
    full_h = output_size(tpl)[1]
    sizes = [preview_h, full_h] if preview_h and _is_manual(s) else [full_h]
    return [effects_cache / f"{_effect_key(s.clip.path, *_effect_params(s, tpl, eh))}.mp4" for eh in sizes]


def render_effect(s: Shot, i: int, tpl: dict, work_dir: Path, effects_cache: Path | None,
                  preview_h: int | None = None, report: Reporter | None = None) -> str | None:
    """Render one sticker / drawn shot (hand-made ones at `preview_h` if given, the rest at full size) and keep
    it in the cache. Returns the file, or None when the effect can't be placed (then the shot is cleared of it
    so the edit still renders, as before)."""
    from .sticker_stage import VISION
    report = report or Reporter(None)
    full_w, full_h = output_size(tpl)
    fps = float(tpl["output"]["fps"])
    background = tpl.get("drawn_opening", {}).get("background", "real")
    manual = _is_manual(s)
    eh = preview_h if preview_h and manual else full_h
    cached = effects_cache / f"{_effect_key(s.clip.path, *_effect_params(s, tpl, eh))}.mp4" if effects_cache else None
    if cached is not None and cached.exists():
        return str(cached)
    work_dir.mkdir(parents=True, exist_ok=True)
    with VISION:                                  # the detectors aren't safe to run from two threads at once
        if s.sticker_at:
            tmp = work_dir / f"placed_{i:03d}.mp4"
            if render_manual_sticker(s.clip.path, s.src_start, s.frames / fps + 0.3, s.sticker, s.sticker_at, tmp,
                                     full_w / full_h, eh):
                return str(_keep(tmp, cached))
            return None
        if s.drawn:
            tmp = work_dir / f"drawn_{i:03d}.mp4"
            if render_drawn_source(s.clip.path, s.src_start, s.drawn["reveal_at"], s.drawn["tail"] + 0.15, tmp,
                                   background, full_w / full_h, eh, s.drawn.get("style", "lineart"), s.drawn.get("box"),
                                   bool(s.drawn.get("force"))):
                return str(_keep(tmp, cached))
            report("render", "effects", 0, 0, f"drawn opening not possible on {s.clip.name}: falling back to a plain hook")
            s.drawn = None
            return None
        tmp = work_dir / f"sticker_{i:03d}.mp4"
        if render_sticker_source(s.clip.path, s.src_start, s.frames / fps + 0.3, s.sticker, tmp, full_w / full_h):
            return str(_keep(tmp, cached))
        report("render", "effects", 0, 0, f"sticker '{s.sticker}' not placed on shot {i} ({s.clip.name}): target not found")
        s.sticker = None
        return None


def render(shots: list[Shot], music, tpl: dict, template_dir: Path,
           out_path: Path, work_dir: Path, report: Reporter | None = None,
           preview: bool = False, effects_cache: Path | None = None, effects: str | None = None,
           overlays: list[dict] | None = None) -> float:
    """`music` needs .path and .start (a Music, or the MusicRef a saved plan loads into).
    `preview`: small and fast. `effects_cache`: rendered sticker / drawn shots (and, in previews, finished
    shots) are kept there and reused by later renders.
    `effects`: "all" renders every sticker / drawn shot that isn't cached (default for full renders);
    "manual" (default for previews) only the hand-made ones, at preview size; "cached" none: shots whose
    effect isn't in the cache yet show plain footage (the app's quick preview, filled in afterwards).
    `overlays`: picture-in-picture clips laid over the joined shots (see pipeline._check_overlays)."""
    report = report or Reporter(None)
    effects = effects or ("manual" if preview else "all")
    full_w, full_h = output_size(tpl)
    w, h = preview_size(full_w, full_h) if preview else (full_w, full_h)
    fps = float(tpl["output"]["fps"])
    grade = grade_filter(tpl.get("grade", {}), template_dir, grain=not preview)
    outro = next(s for s in tpl["structure"] if s["section"] == "outro")
    fade = float(outro.get("fade_out", 0.6))
    work_dir.mkdir(parents=True, exist_ok=True)
    parts_cache = effects_cache / "parts" if preview and effects_cache else None

    # Sticker shots first (sequential: the detector is already multithreaded). They are cropped to the
    # output aspect before tracking so the sticker can't land in the part the crop throws away.
    sources = [(s.clip.path, s.src_start) for s in shots]
    todo = [i for i, s in enumerate(shots) if s.drawn or s.sticker]
    report("render", "effects", 0, len(todo))
    for n, i in enumerate(todo):
        s = shots[i]
        have = next((f for f in effect_files(s, tpl, effects_cache, h if preview else None) if f.exists()), None)
        if have is not None:
            sources[i] = (str(have), 0.0)
        elif effects == "all" or (effects == "manual" and _is_manual(s)):
            done = render_effect(s, i, tpl, work_dir, effects_cache, h if preview else None, report)
            if done:
                sources[i] = (done, 0.0)
        report("render", "effects", n + 1, len(todo))

    report("render", "shots", 0, len(shots))
    parts: list[Path | None] = [None] * len(shots)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_render_shot, i, s, sources[i], work_dir, w, h, fps, grade,
                               fade if s.role == "outro" else 0.0, preview, parts_cache): i for i, s in enumerate(shots)}
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

    if overlays:
        video_only = _overlay_pass(video_only, overlays, w, h, fps, grade, preview, work_dir,
                                   sum(s.frames for s in shots))

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
