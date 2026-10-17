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
"""
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, fields, replace
from pathlib import Path

import numpy as np

from . import sticker_stage
from .analyze import ClipAnalysis, analyze_clip, list_clips
from .formats import apply_aspect, output_size
from .music import Music, analyze_music
from .planner import REUSE_PENALTY, Shot, assign_clips, plan_cuts
from .progress import Progress, Reporter
from .render import render
from .sticker_stage import presence, rule_for

FORMAT_VERSION = 1


# ----------------------------------------------------------------------------- options / data

@dataclass
class Options:
    aspect: str | None = None        # None: the template's output.aspect
    duration: float | None = None    # override output.duration.target (seconds)
    music_start: float | None = None  # None: auto-pick the loudest section
    stickers: bool = True
    drawn: bool = True


@dataclass
class Analysis:
    template: dict                   # effective template (aspect / duration overrides applied)
    template_path: str
    clips: list[ClipAnalysis]
    music: Music
    options: Options
    stickers_tagged: bool = False    # the sticker detector ran (it's optional: needs ultralytics)

    @property
    def aspect(self) -> str:
        return self.template["output"]["aspect"]


@dataclass
class Plan:
    template: dict
    template_path: str
    music: "MusicRef"
    seed: int
    shots: list[Shot]

    @property
    def duration(self) -> float:
        return sum(s.frames for s in self.shots) / float(self.template["output"]["fps"])


@dataclass
class ClipRef:
    """What render needs of a clip: a plan loaded from disk has no analysis arrays."""
    path: str

    @property
    def name(self) -> str:
        return Path(self.path).name


@dataclass
class MusicRef:
    path: str
    start: float
    tempo: float
    beat_period: float


def load_template(path: str | Path, opts: Options | None = None) -> dict:
    opts = opts or Options()
    tpl = json.loads(Path(path).read_text(encoding="utf-8"))
    apply_aspect(tpl, opts.aspect)
    if opts.duration:
        tpl["output"]["duration"]["target"] = opts.duration
    return tpl


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


def plan(analysis: Analysis, seed: int = 7, progress: Progress | None = None) -> Plan:
    """Same seed + same analysis -> same plan. Doesn't change `analysis` (works on copies of the clips)."""
    report = Reporter(progress)
    report("plan", "cuts", 0, 1)
    tpl = analysis.template
    clips = [_copy_clip(c) for c in analysis.clips]
    rng = np.random.default_rng(seed)

    max_stickers = 0
    st_cfg = tpl.get("stickers", {})
    if analysis.stickers_tagged and st_cfg.get("enabled") and analysis.options.stickers:
        taggable = [c for c in clips if any(c.tags)]
        max_stickers = min(st_cfg["density"]["per_video"][1], len(taggable))
    use_drawn = (tpl.get("drawn_opening", {}).get("enabled") and analysis.options.drawn
                 and any(c.drawable for c in clips))
    drawn_clips = {c.path for c in clips if c.drawable}
    use_mid = use_drawn and tpl.get("drawn_mid", {}).get("enabled", False) and len(drawn_clips) >= 2

    segments = plan_cuts(analysis.music, tpl, rng, max_stickers, use_drawn, use_mid)
    shots = assign_clips(clips, segments, tpl, float(tpl["output"]["fps"]), rng)
    m = analysis.music
    report("plan", "cuts", 1, 1)
    return Plan(tpl, analysis.template_path, MusicRef(m.path, float(m.start), float(m.tempo), float(m.beat_period)),
                seed, shots)


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
    n_animal = sum(1 for j, sh in enumerate(p.shots) if j != index and sh.sticker in ("anger", "question"))

    def window(c: ClipAnalysis, want_sticker: bool) -> tuple[float, float, str | None] | None:
        """(start, score, sticker family) of the best free window, sticker-able ones first if wanted."""
        best = None
        for s in c.candidate_starts(length):
            hit = presence(c, s, length) if want_sticker else None
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
            sticker = rule_for(family, n_animal)
        else:
            role = "body"
    elif old.role == "drawn":
        role = "hook" if index == 0 else "breath"
    shots = list(p.shots)
    shots[index] = replace(old, role=role, clip=ClipRef(c.path), src_start=float(start), sticker=sticker, drawn=drawn)
    return replace(p, shots=shots)


def render_plan(p: Plan, out_path: str | Path, keep_temp: bool = False,
                progress: Progress | None = None, preview: bool = False,
                effects_cache: str | Path | None = None) -> dict:
    """Render the video and write <out>.timeline.json next to it. Returns the timeline dict.
    `preview`: low-res, fast encode, sticker / drawn shots as plain footage unless already in the cache.
    `effects_cache`: folder to keep rendered sticker / drawn shots in, so re-renders reuse them."""
    report = Reporter(progress)
    out = Path(out_path)
    work = out.parent / f".{out.stem}_work"
    total = render(p.shots, p.music, p.template, Path(p.template_path).parent, out, work, report,
                   preview=preview, effects_cache=Path(effects_cache) if effects_cache else None)
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


# ----------------------------------------------------------------------------- save / load

_ARRAY_FIELDS = ("times", "sharp", "bright", "motion", "shake", "color", "subject", "frame_score")


def _arr(a: np.ndarray) -> dict:
    return {"dtype": str(a.dtype), "data": a.tolist()}


def _unarr(d: dict) -> np.ndarray:
    return np.asarray(d["data"], dtype=d["dtype"])


def _copy_clip(c: ClipAnalysis) -> ClipAnalysis:
    return ClipAnalysis(**{f.name: getattr(c, f.name) for f in fields(ClipAnalysis)
                           if f.name not in ("used", "tags", "drawable")},
                        used=list(c.used), tags=list(c.tags), drawable=list(c.drawable))


def _plain(v):
    """tuples -> lists and numpy scalars -> Python numbers, for JSON."""
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, np.generic):
        return v.item()
    return v


def _clip_dict(c: ClipAnalysis) -> dict:
    return {"path": c.path, "duration": float(c.duration), "width": c.width, "height": c.height,
            "rejected": bool(c.rejected), "reason": c.reason,
            "tags": _plain(c.tags), "drawable": _plain(c.drawable),
            **{k: _arr(getattr(c, k)) for k in _ARRAY_FIELDS}}


def _clip_from(c: dict) -> ClipAnalysis:
    return ClipAnalysis(path=c["path"], duration=c["duration"], width=c["width"], height=c["height"],
                        rejected=c["rejected"], reason=c["reason"],
                        tags=[tuple(t) if t is not None else None for t in c.get("tags", [])],
                        drawable=[tuple(x) for x in c.get("drawable", [])],
                        **{k: _unarr(c[k]) for k in _ARRAY_FIELDS})


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def _cache_file(cache_dir: str | Path, kind: str, path: str, extra: str = "") -> Path:
    """Cache entry for one clip: keyed by the file (path, size, mtime) and whatever else the result depends on."""
    st = os.stat(path)
    key = hashlib.sha1(f"{FORMAT_VERSION}|{kind}|{_norm(path)}|{st.st_size}|{st.st_mtime_ns}|{extra}".encode())
    return Path(cache_dir) / f"{kind}_{key.hexdigest()[:20]}.json"


def _read_cache(f: Path):
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except (OSError, ValueError):
        return None                          # unreadable entry: compute again


def _write_cache(f: Path, data) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, f)


def _cached_clip(path: str, cache_dir: str | Path | None) -> ClipAnalysis:
    if cache_dir is None:
        return analyze_clip(path)
    f = _cache_file(cache_dir, "clip", path)
    d = _read_cache(f)
    if d is not None:
        try:
            c = _clip_from(d)
            c.path = path
            return c
        except (KeyError, TypeError, ValueError):
            pass
    c = analyze_clip(path)
    d = _clip_dict(c)
    d["tags"], d["drawable"] = [], []       # aspect-dependent: cached separately
    _write_cache(f, d)
    return c


def _cached_music(path: str, start: float | None, tpl: dict, cache_dir) -> Music:
    args = (start, tpl["music"].get("half_beat_below_bpm", 100), tuple(tpl["music"].get("bpm_range", (75, 175))),
            tpl["output"]["duration"]["target"] + 2.0)
    f = _cache_file(cache_dir, "music", path, json.dumps(args)) if cache_dir else None
    d = _read_cache(f) if f else None
    if d is not None:
        try:
            return Music(path=path, start=d["start"], duration=d["duration"], tempo=d["tempo"],
                         beat_period=d["beat_period"], grid=_unarr(d["grid"]), strength=_unarr(d["strength"]),
                         on_beat=_unarr(d["on_beat"]))
        except (KeyError, TypeError, ValueError):
            pass
    m = analyze_music(path, *args)
    if f:
        _write_cache(f, {"start": float(m.start), "duration": float(m.duration), "tempo": float(m.tempo),
                         "beat_period": float(m.beat_period), "grid": _arr(m.grid), "strength": _arr(m.strength),
                         "on_beat": _arr(m.on_beat)})
    return m


def _cached_pass(clips: list[ClipAnalysis], field_name: str, size: str, cache_dir, run, report) -> None:
    """Fill clip.<field_name> (tags or drawable) from the cache where possible; `run(todo, on_clip)` computes
    the rest, which is then cached. Rejected clips are never looked at, so they aren't cached either."""
    files = {id(c): _cache_file(cache_dir, field_name, c.path, size) for c in clips} if cache_dir else {}
    todo = []
    for c in clips:
        d = _read_cache(files[id(c)]) if cache_dir and not c.rejected else None
        if d is None:
            todo.append(c)
        elif field_name == "tags":
            c.tags = [tuple(t) if t is not None else None for t in d]
        else:
            c.drawable = [tuple(x) for x in d]
    done = len(clips) - len(todo)
    report(done, len(clips))
    if todo:
        run(todo, lambda n, t, c: report(done + n, len(clips)))
    for c in todo:
        if cache_dir and not c.rejected:
            _write_cache(files[id(c)], _plain(getattr(c, field_name)))


def save_analysis(a: Analysis, path: str | Path) -> None:
    m = a.music
    data = {
        "kind": "oddcut.analysis", "version": FORMAT_VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "aspect": a.aspect, "resolution": list(output_size(a.template)),
        "template_path": a.template_path, "template": a.template,
        "options": vars(a.options), "stickers_tagged": a.stickers_tagged,
        "music": {"path": m.path, "start": float(m.start), "duration": float(m.duration), "tempo": float(m.tempo),
                  "beat_period": float(m.beat_period), "grid": _arr(m.grid), "strength": _arr(m.strength),
                  "on_beat": _arr(m.on_beat)},
        "clips": [_clip_dict(c) for c in a.clips],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_analysis(path: str | Path) -> Analysis:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if d.get("kind") != "oddcut.analysis":
        raise ValueError(f"{path} is not an oddcut analysis file")
    m = d["music"]
    music = Music(path=m["path"], start=m["start"], duration=m["duration"], tempo=m["tempo"],
                  beat_period=m["beat_period"], grid=_unarr(m["grid"]), strength=_unarr(m["strength"]),
                  on_beat=_unarr(m["on_beat"]))
    clips = [_clip_from(c) for c in d["clips"]]
    return Analysis(d["template"], d["template_path"], clips, music, Options(**d["options"]), d["stickers_tagged"])


def save_plan(p: Plan, path: str | Path) -> None:
    data = {
        "kind": "oddcut.plan", "version": FORMAT_VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "aspect": p.template["output"]["aspect"], "resolution": list(output_size(p.template)),
        "duration": round(p.duration, 3), "seed": p.seed,
        "template_path": p.template_path, "template": p.template, "music": vars(p.music),
        # full precision here (the timeline.json rounds), so rendering a saved plan is frame-identical
        "shots": [{**s.to_dict(), "clip_path": s.clip.path, "src_start": s.src_start,
                   "t_start": s.t_start, "t_end": s.t_end} for s in p.shots],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_plan(path: str | Path) -> Plan:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if d.get("kind") != "oddcut.plan":
        raise ValueError(f"{path} is not an oddcut plan file")
    shots = [Shot(role=s["role"], clip=ClipRef(s["clip_path"]), src_start=s["src_start"], t_start=s["t_start"],
                  t_end=s["t_end"], frames=s["frames"], sticker=s.get("sticker"), drawn=s.get("drawn"))
             for s in d["shots"]]
    return Plan(d["template"], d["template_path"], MusicRef(**d["music"]), d["seed"], shots)
