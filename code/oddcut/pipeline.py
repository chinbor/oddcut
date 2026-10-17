"""The edit as three separate steps, each with a JSON file in between, so a front end can run them one at
a time, show progress, and let the user look at (or later edit) the result before the next step:

    analyze   clips folder + music + template -> analysis.json   clip scores, music beat grid, sticker tags
    plan      analysis.json (+ seed)          -> plan.json       the timeline: which clip, where, which effect
    render    plan.json                       -> out.mp4         (+ out.timeline.json, as before)

`run_all` chains them in memory; that is what `python -m oddcut --clips ... --music ...` does.
Analysis depends on the output aspect (sticker / drawn checks look inside the crop), so the aspect is fixed
at analyze time and plan / render use the one stored in the file; another aspect means re-running analyze.
"""
import json
import shutil
import time
from dataclasses import dataclass, fields
from pathlib import Path

import numpy as np

from . import sticker_stage
from .analyze import ClipAnalysis, analyze_clip, list_clips
from .formats import apply_aspect, output_size
from .music import Music, analyze_music
from .planner import Shot, assign_clips, plan_cuts
from .progress import Progress, Reporter
from .render import render

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
            opts: Options | None = None, progress: Progress | None = None) -> Analysis:
    opts = opts or Options()
    report = Reporter(progress)
    template_path = str(Path(template_path).resolve())
    tpl = load_template(template_path, opts)
    w, h = output_size(tpl)

    paths = list_clips(str(Path(clips_dir).resolve()))
    report("analyze", "clips", 0, len(paths))
    clips = []
    for i, p in enumerate(paths):
        clips.append(analyze_clip(p))
        report("analyze", "clips", i + 1, len(paths))

    report("analyze", "music", 0, 1)
    music = analyze_music(str(Path(music_path).resolve()), opts.music_start,
                          tpl["music"].get("half_beat_below_bpm", 100),
                          tuple(tpl["music"].get("bpm_range", (75, 175))),
                          tpl["output"]["duration"]["target"] + 2.0)
    report("analyze", "music", 1, 1)

    tagged = False
    if tpl.get("stickers", {}).get("enabled") and opts.stickers:
        if sticker_stage.available():
            sticker_stage.tag_clips(clips, w / h, lambda n, t, c: report("analyze", "stickers", n, t))
            report("analyze", "stickers", len(clips), len(clips))
            tagged = True
        else:
            report("analyze", "stickers", 0, 0, "stickers skipped: install ultralytics + opencv-python (see README)")

    if (tpl.get("drawn_opening", {}).get("enabled") and opts.drawn
            and any(any(c.tags) for c in clips)):
        sticker_stage.tag_drawable(clips, w / h, lambda n, t, c: report("analyze", "drawn", n, t))
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


def render_plan(p: Plan, out_path: str | Path, keep_temp: bool = False,
                progress: Progress | None = None) -> dict:
    """Render the video and write <out>.timeline.json next to it. Returns the timeline dict."""
    report = Reporter(progress)
    out = Path(out_path)
    work = out.parent / f".{out.stem}_work"
    total = render(p.shots, p.music, p.template, Path(p.template_path).parent, out, work, report)
    timeline = {
        "output": str(out), "duration": round(total, 3), "tempo": round(p.music.tempo, 2),
        "music": p.music.path, "music_start": p.music.start, "seed": p.seed,
        "aspect": p.template["output"]["aspect"], "resolution": list(output_size(p.template)),
        "shots": [s.to_dict() for s in p.shots],
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
        "clips": [{"path": c.path, "duration": float(c.duration), "width": c.width, "height": c.height,
                   "rejected": bool(c.rejected), "reason": c.reason,
                   "tags": _plain(c.tags), "drawable": _plain(c.drawable),
                   **{k: _arr(getattr(c, k)) for k in _ARRAY_FIELDS}} for c in a.clips],
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
    clips = [ClipAnalysis(path=c["path"], duration=c["duration"], width=c["width"], height=c["height"],
                          rejected=c["rejected"], reason=c["reason"],
                          tags=[tuple(t) if t is not None else None for t in c["tags"]],
                          drawable=[tuple(x) for x in c["drawable"]],
                          **{k: _unarr(c[k]) for k in _ARRAY_FIELDS}) for c in d["clips"]]
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
