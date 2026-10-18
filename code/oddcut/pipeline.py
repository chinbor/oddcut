"""The edit as three separate steps, each with a JSON file in between, so a front end can run them one at
a time, show progress, and let the user look at (or later edit) the result before the next step:

    analyze   clips folder + music + template -> analysis.json   clip scores, music beat grid, sticker tags
    plan      analysis.json (+ seed)          -> plan.json       the timeline: which clip, where, which effect
    render    plan.json                       -> out.mp4         (+ out.timeline.json, as before)

`run_all` chains them in memory; that is what `python -m oddcut --clips ... --music ...` does.
For a front end there are also small edits that skip the slow parts: `with_music` swaps the song on an
existing analysis (the clips aren't looked at again), `replace_shot` swaps one shot of a plan, and
`render_plan(..., preview=True)` makes a quick low-res cut without the sticker / drawn effects.
Analysis depends on the output aspect (sticker / drawn checks look inside the crop), so the aspect is fixed
at analyze time and plan / render use the one stored in the file; another aspect means re-running analyze.

The rest lives next to it and is re-exported here, so `from .pipeline import ...` keeps working:
plan_data (Options / Analysis / Plan, cache, save / load), edits (hand edits on the 主轨 shots), media_edits
(导入, insert / delete on the 主轨), overlays (画中画 checks), presets (万物生长 / 手绘 layers).
"""
import json
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np

from . import sticker_stage
from .analyze import ClipAnalysis, list_clips
from .formats import output_size
from .looks import DEFAULT_DRAWN_STYLE, check_drawn_style, check_stickers, family_of, sticker_families, sticker_id
from .planner import REUSE_PENALTY, assign_clips, plan_cuts
from .progress import Progress, Reporter
from .render import render
from .sticker_stage import presence, rule_for

# everything that used to be defined in this file, so callers keep importing it from here
from .plan_data import (Analysis, _arr, _ARRAY_FIELDS, _cache_file, _cached_clip, _cached_music, _cached_pass,
                        _clip_dict, _clip_from, ClipRef, _copy_clip, FORMAT_VERSION, link_overlays, load_analysis,
                        load_plan, load_template, MusicRef, _norm, Options, _plain, Plan, _read_cache, save_analysis,
                        save_plan, _unarr, _write_cache)  # noqa: F401
from .edits import (carry_manual, check_grow, _check_place, _clip_len, _drawn_cfg, edit_shot, _frames_between,
                    grow_all, grow_hint, grows, has_manual, KEEP, MIN_SHOT, move_cut, move_shot, _overlap, _retail,
                    retime, _shift_keys, split_shot, trim_shot)  # noqa: F401
from .media_edits import (AUDIO_IN, delete_shot, IMAGE_IN, IMAGE_SECONDS, import_media, insert_shot, remove_clip,
                          _shift_after, VIDEO_IN)  # noqa: F401
from .overlays import (_check_overlays, MIN_OVERLAY, OVERLAY_TRACKS, _resnap_overlays, set_overlays)  # noqa: F401
from .presets import (auto_grow, _downbeats, drawn_layer, drawn_to_layers, grow_preset, _grow_specs, GROW_STEPS,
                      _grow_subject, remove_grow_preset)  # noqa: F401


# ----------------------------------------------------------------------------- steps

def analyze(clips_dir: str | Path, music_path: str | Path, template_path: str | Path,
            opts: Options | None = None, progress: Progress | None = None,
            cache_dir: str | Path | None = None) -> Analysis:
    """`cache_dir`: keep each clip's analysis there (keyed by path, size and mtime), and its sticker /
    drawn checks per output size, so the same footage is only looked at once."""
    opts = opts or Options()
    report = Reporter(progress)
    template_path = str(Path(template_path).resolve())
    tpl = load_template(template_path, opts)
    w, h = output_size(tpl)

    paths = list_clips(str(Path(clips_dir).resolve()))
    report("analyze", "clips", 0, len(paths))
    clips = []
    for i, p in enumerate(paths):
        clips.append(_cached_clip(p, cache_dir))
        report("analyze", "clips", i + 1, len(paths))

    report("analyze", "music", 0, 1)
    music = _cached_music(str(Path(music_path).resolve()), opts.music_start, tpl, cache_dir)
    report("analyze", "music", 1, 1)

    tagged = False
    aspect_id = f"{w}x{h}"
    if tpl.get("stickers", {}).get("enabled") and opts.stickers:
        if sticker_stage.available():
            _cached_pass(clips, "tags", aspect_id, cache_dir,
                         lambda todo, cb: sticker_stage.tag_clips(todo, w / h, cb),
                         lambda n, t: report("analyze", "stickers", n, t))
            report("analyze", "stickers", len(clips), len(clips))
            tagged = True
        else:
            report("analyze", "stickers", 0, 0, "stickers skipped: install ultralytics + opencv-python (see README)")

    if (tpl.get("drawn_opening", {}).get("enabled") and opts.drawn
            and any(any(c.tags) for c in clips)):
        _cached_pass(clips, "drawable", aspect_id, cache_dir,
                     lambda todo, cb: sticker_stage.tag_drawable(todo, w / h, cb),
                     lambda n, t: report("analyze", "drawn", n, t))
        report("analyze", "drawn", len(clips), len(clips))
    return Analysis(tpl, template_path, clips, music, opts, tagged)


def plan(analysis: Analysis, seed: int = 7, progress: Progress | None = None, stickers=None,
         drawn_style: str | None = None) -> Plan:
    """Same seed + same analysis -> same plan. Doesn't change `analysis` (works on copies of the clips).
    `stickers`: ids picked in 「贴纸选择」 (None = the original eyes / 💢 / ?; [] = no stickers).
    `drawn_style`: the 「手绘风格选择」 style for the drawn shots (None = the default line art)."""
    stickers = check_stickers(stickers) if stickers is not None else None
    drawn_style = check_drawn_style(drawn_style)
    report = Reporter(progress)
    report("plan", "cuts", 0, 1)
    tpl = analysis.template
    clips = [_copy_clip(c) for c in analysis.clips]
    rng = np.random.default_rng(seed)

    max_stickers = 0
    st_cfg = tpl.get("stickers", {})
    if analysis.stickers_tagged and st_cfg.get("enabled") and analysis.options.stickers:
        fams = sticker_families(stickers) if stickers is not None else {"object", "animal"}
        taggable = [c for c in clips if ("free" in fams and not c.rejected) or any(t and t[1] in fams for t in c.tags)]
        max_stickers = min(st_cfg["density"]["per_video"][1], len(taggable))
    use_drawn = (tpl.get("drawn_opening", {}).get("enabled") and analysis.options.drawn
                 and any(c.drawable for c in clips))
    drawn_clips = {c.path for c in clips if c.drawable}
    use_mid = use_drawn and tpl.get("drawn_mid", {}).get("enabled", False) and len(drawn_clips) >= 2

    segments = plan_cuts(analysis.music, tpl, rng, max_stickers, use_drawn, use_mid)
    shots = assign_clips(clips, segments, tpl, float(tpl["output"]["fps"]), rng, stickers)
    if drawn_style != DEFAULT_DRAWN_STYLE:        # the default stays implicit: old plans render the same
        for s in shots:
            if s.drawn:
                s.drawn = {**s.drawn, "style": drawn_style}
    m = analysis.music
    report("plan", "cuts", 1, 1)
    return Plan(tpl, analysis.template_path, MusicRef(m.path, float(m.start), float(m.tempo), float(m.beat_period)),
                seed, shots, {"stickers": stickers, "drawn_style": drawn_style})


def with_music(analysis: Analysis, music_path: str | Path, music_start: float | None = None,
               duration: float | None = None, progress: Progress | None = None,
               cache_dir: str | Path | None = None) -> Analysis:
    """The same footage analysis with another song (or another target duration): only the music is analyzed."""
    report = Reporter(progress)
    tpl = json.loads(json.dumps(analysis.template))
    if duration:
        tpl["output"]["duration"]["target"] = duration
    report("music", "music", 0, 1)
    music = _cached_music(str(Path(music_path).resolve()), music_start, tpl, cache_dir)
    report("music", "music", 1, 1)
    opts = replace(analysis.options, music_start=music_start, duration=duration or analysis.options.duration)
    return Analysis(tpl, analysis.template_path, analysis.clips, music, opts, analysis.stickers_tagged)


def replace_shot(analysis: Analysis, p: Plan, index: int, clip_path: str | None = None,
                 seed: int | None = None) -> Plan:
    """Swap the footage of one shot; its place and length on the timeline stay the same.
    `clip_path`: use that clip (its best free window); None: pick another good clip automatically.
    A sticker shot keeps its sticker if the new footage has a sticker-able object the whole time,
    otherwise it becomes a plain shot; a drawn shot becomes a plain one."""
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range (plan has {len(p.shots)})")
    rng = np.random.default_rng(seed)
    by_path = {_norm(c.path): _copy_clip(c) for c in analysis.clips}
    old = p.shots[index]
    length = old.t_end - old.t_start
    usage: dict[str, int] = {}
    for j, sh in enumerate(p.shots):         # footage the other shots show is taken
        k = _norm(sh.clip.path)
        if j == index or k not in by_path:
            continue
        by_path[k].used.append((sh.src_start, sh.src_start + (sh.t_end - sh.t_start)))
        usage[k] = usage.get(k, 0) + 1
    allowed = p.looks.get("stickers")
    families = sticker_families(allowed) if allowed is not None else None
    placed = {"object": 0, "animal": 0, "free": 0}
    for j, sh in enumerate(p.shots):
        if j != index and family_of(sh.sticker) in placed:
            placed[family_of(sh.sticker)] += 1

    def window(c: ClipAnalysis, want_sticker: bool) -> tuple[float, float, str | None] | None:
        """(start, score, sticker family) of the best free window, sticker-able ones first if wanted."""
        best = None
        for s in c.candidate_starts(length):
            hit = presence(c, s, length) if want_sticker else None
            if hit and families is not None and hit[0] not in families:
                hit = None
            if want_sticker and hit is None and families is not None and "free" in families:
                hit = ("free", None)
            sc = c.window_score(s, length) + (5.0 if hit else 0.0)
            if best is None or sc > best[1]:
                best = (s, sc, hit[0] if hit else None)
        return best

    want_sticker = old.role == "sticker"
    if clip_path:
        c = by_path.get(_norm(clip_path))
        if c is None:
            raise ValueError(f"{Path(clip_path).name} is not one of the analyzed clips")
        if c.duration < length:
            raise ValueError(f"{c.name} is {c.duration:.1f}s, too short for a {length:.1f}s shot")
        w = window(c, want_sticker)
        if w is None:                       # every part of it is already on screen: allow showing it twice
            c.used = []
            w = window(c, want_sticker)
        pick = (c, *w)
    else:
        neighbours = {_norm(p.shots[j].clip.path) for j in (index - 1, index, index + 1) if 0 <= j < len(p.shots)}
        cands = []
        for k, c in by_path.items():
            if c.rejected or k in neighbours:
                continue
            w = window(c, want_sticker)
            if w is None:
                continue
            cands.append((w[1] - REUSE_PENALTY * usage.get(k, 0), c, *w))
        if not cands:
            raise RuntimeError("no other footage fits this shot")
        cands.sort(key=lambda x: -x[0])
        top = cands[:3]
        pr = np.exp(np.array([x[0] for x in top]) / 0.3)
        _, *pick = top[int(rng.choice(len(top), p=pr / pr.sum()))]
    c, start, _, family = pick

    role, sticker, drawn = old.role, None, None
    if old.role == "sticker":
        if family:
            used = {k: sum(1 for j, sh in enumerate(p.shots) if j != index and sticker_id(sh.sticker) == k)
                    for k in allowed or ()}
            sticker = rule_for(family, placed[family], allowed, used)
        else:
            role = "body"
    elif old.role == "drawn":
        role = "hook" if index == 0 else "breath"
    shots = list(p.shots)
    sticker_at = None
    if old.sticker_at:                       # a hand-placed sticker stays where the user put it
        sticker, sticker_at = old.sticker, old.sticker_at
    shots[index] = replace(old, role=role, clip=ClipRef(c.path), src_start=float(start), sticker=sticker, drawn=drawn,
                           sticker_at=sticker_at)
    return replace(p, shots=shots)


def render_plan(p: Plan, out_path: str | Path, keep_temp: bool = False,
                progress: Progress | None = None, preview: bool = False,
                effects_cache: str | Path | None = None, effects: str | None = None, grow: str | None = None) -> dict:
    """Render the video and write <out>.timeline.json next to it. Returns the timeline dict.
    `preview`: low-res, fast encode, sticker / drawn shots as plain footage unless already in the cache.
    `effects_cache`: folder to keep rendered sticker / drawn shots in, so re-renders reuse them."""
    report = Reporter(progress)
    out = Path(out_path)
    work = out.parent / f".{out.stem}_work"
    total = render(p.shots, p.music, p.template, Path(p.template_path).parent, out, work, report,
                   preview=preview, effects_cache=Path(effects_cache) if effects_cache else None, effects=effects,
                   overlays=p.looks.get("overlays"), grow=grow, silent=bool(p.looks.get("music_off")))
    timeline = {
        "output": str(out), "duration": round(total, 3), "tempo": round(p.music.tempo, 2),
        "music": p.music.path, "music_start": p.music.start, "seed": p.seed,
        "aspect": p.template["output"]["aspect"], "resolution": list(output_size(p.template)),
        "preview": preview, "shots": [s.to_dict() for s in p.shots],
    }
    out.with_suffix(".timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    if not keep_temp:
        shutil.rmtree(work, ignore_errors=True)
    report("render", "done", 1, 1)
    return timeline


def run_all(clips_dir, music_path, template_path, out_path, opts: Options | None = None, seed: int = 7,
            keep_temp: bool = False, progress: Progress | None = None) -> dict:
    a = analyze(clips_dir, music_path, template_path, opts, progress)
    return render_plan(plan(a, seed, progress), out_path, keep_temp, progress)
