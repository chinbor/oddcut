"""The data the pipeline passes around and keeps on disk: Options, Analysis, Plan (and the ClipRef / MusicRef it
holds), the template loader, the analysis cache (one entry per clip / song, keyed by the file and the settings it
depends on), and save / load of analysis.json and plan.json (save_plan also keeps 跟随主画面 overlays in step).
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass, field, fields
from pathlib import Path

import numpy as np

from .analyze import ClipAnalysis, analyze_clip
from .formats import apply_aspect, output_size
from .music import Music, analyze_music
from .planner import Shot

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
    looks: dict = field(default_factory=dict)   # {"stickers": [...] or None, "drawn_style": ...}

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


def link_overlays(p: Plan) -> None:
    """跟随主画面: a 画中画 copied from a main-track shot (link) reads the same source clock as the shot of that clip
    it is nearest to (the one it leads into when it sits just before it), so wherever they meet the frames match:
    src_start = shot.src_start - (shot.t_start - t_start). Kept up to date whenever a plan is saved, so moving or
    trimming either side keeps the seam. Not enough footage before the shot: from the clip's start, short = the
    seconds missing."""
    ovs = p.looks.get("overlays") or []
    if not any(o.get("link") for o in ovs):
        return
    out = []
    for o in ovs:
        same = [s for s in p.shots if s.clip.path == o["clip"]] if o.get("link") else []
        if same:
            gap = lambda s: max(0.0, s.t_start - o["t_end"], o["t_start"] - s.t_end)
            s = min(same, key=lambda s: (round(gap(s), 3), -s.t_start))
            src = s.src_start - (s.t_start - o["t_start"])
            o = {k: v for k, v in o.items() if k != "short"}
            o["src_start"] = round(max(0.0, src), 3)
            if src < -1e-3:
                o["short"] = round(-src, 2)
        out.append(o)
    p.looks["overlays"] = out


def save_plan(p: Plan, path: str | Path) -> None:
    link_overlays(p)
    data = {
        "kind": "oddcut.plan", "version": FORMAT_VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "aspect": p.template["output"]["aspect"], "resolution": list(output_size(p.template)),
        "duration": round(p.duration, 3), "seed": p.seed,
        "template_path": p.template_path, "template": p.template, "music": vars(p.music), "looks": p.looks,
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
                  t_end=s["t_end"], frames=s["frames"], sticker=s.get("sticker"), drawn=s.get("drawn"),
                  sticker_at=s.get("sticker_at"), grow=s.get("grow"))
             for s in d["shots"]]
    return Plan(d["template"], d["template_path"], MusicRef(**d["music"]), d["seed"], shots, d.get("looks") or {})
