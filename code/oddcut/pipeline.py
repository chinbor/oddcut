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
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

import numpy as np

from . import sticker_stage
from .analyze import ClipAnalysis, analyze_clip, list_clips
from .formats import apply_aspect, output_size
from .looks import DEFAULT_DRAWN_STYLE, check_drawn_style, check_stickers, family_of, sticker_families, sticker_id
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


# ----------------------------------------------------------------------------- hand edits (desktop app)

KEEP = object()          # edit_shot: leave this part of the shot as it is


def _frames_between(a: float, b: float, fps: float) -> int:
    return int(round(b * fps)) - int(round(a * fps))


def _check_place(place: dict) -> dict:
    keys = []
    for k in place.get("keys") or []:
        keys.append({"t": max(0.0, float(k["t"])), "x": min(1.0, max(0.0, float(k["x"]))),
                     "y": min(1.0, max(0.0, float(k["y"]))), "size": min(0.8, max(0.04, float(k.get("size", 0.22))))})
    if not keys:
        raise ValueError("a placed sticker needs at least one keyframe")
    mode = place.get("mode", "follow")
    if mode not in ("follow", "path", "still", "fixed"):
        raise ValueError(f"unknown sticker mode {mode!r}")
    out = {"mode": mode, "keys": sorted(keys, key=lambda k: k["t"]), "in": max(0.0, float(place.get("in") or 0.0))}
    if place.get("out") is not None:
        out["out"] = float(place["out"])
    return out


def check_grow(g: dict) -> dict:
    """万物生长 settings: layers 3 or 4, a layer every beat or half-beat, motion blur 0..1.5; source auto / manual
    (a manual choice wins over the automatic pick and survives a new plan); {"off": true} = turned off by hand."""
    source = g.get("source", "manual") if g.get("source") in ("auto", "manual") else "manual"
    if g.get("off"):
        return {"off": True, "source": "manual"}
    stagger = g.get("stagger", "beat")
    if stagger not in ("beat", "half"):
        raise ValueError(f"unknown stagger {stagger!r}")
    out = {"layers": 4 if int(g.get("layers", 3)) >= 4 else 3, "stagger": stagger,
           "blur": round(min(1.5, max(0.0, float(g.get("blur", 1.0)))), 2), "source": source}
    if g.get("why"):
        out["why"] = str(g["why"])[:80]
    return out


def grows(s: Shot) -> bool:
    """Does this shot grow in (万物生长 on and not turned off by hand)?"""
    return bool(s.grow) and not s.grow.get("off")


def grow_all(p: Plan, g: dict | None) -> Plan:
    """全片万物生长: every shot grows in (the first out of black), or none does (g=None)."""
    g = check_grow({**g, "source": "manual"}) if g else None
    return replace(p, shots=[replace(s, grow=g) for s in p.shots])


def _downbeats(m) -> np.ndarray:
    """True at grid points that are a bar's first beat (4/4; the phase whose beats hit hardest, as the app's
    timeline shows them)."""
    on = np.asarray(m.on_beat, bool)
    full = np.nonzero(on)[0]
    out = np.zeros(len(on), bool)
    if not len(full):
        return out
    st = np.asarray(m.strength, float)
    phase = max(range(4), key=lambda q: st[full[q::4]].mean() if len(full[q::4]) else 0)
    out[full[phase::4]] = True
    return out


def auto_grow(analysis: Analysis, p: Plan, cache_file: Path | None = None, progress=None
              ) -> tuple[Plan, list[str], list[dict]]:
    """万物生长 only where it fits: every cut is scored (growth.score_shot: layers, a whole subject, a steady
    camera, contrast between the layers; plus the beat it falls on) and the best 1-3 (about one per 9 s) above
    the bar get it, never two cuts in a row. Choices made by hand (on or off) stay as they are. Returns the plan,
    notes and a report per shot (score, picked, why / why not)."""
    from .formats import output_size
    from .growth import AUTO_EVERY, AUTO_MAX, AUTO_THRESHOLD, score_shot
    from .render import effect_rev
    w, h = output_size(p.template)
    aspect = w / h
    cache = {}
    if cache_file and cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    # earlier automatic picks go (their cuts move back); hand-made ones stay
    for g in [g for g in p.looks.get("grow_presets") or [] if g.get("source") == "auto"]:
        p, _ = remove_grow_preset(analysis, p, g["id"])
    rev = effect_rev("grow")
    m = analysis.music
    grid, strength, down = m.grid, np.asarray(m.strength, float), _downbeats(m)
    report = []
    for i, s in enumerate(p.shots):
        key = f"{_norm(s.clip.path)}|{s.src_start:.2f}|{aspect:.4f}|{rev}"
        if key not in cache:
            cache[key] = score_shot(s.clip.path, s.src_start, aspect)
        if progress:
            progress(i + 1, len(p.shots))
        sc = cache[key]
        if i == 0:
            beat, beat_why = 1.0, "开场"
        else:
            g = int(np.argmin(np.abs(grid - s.t_start)))
            beat = float(0.6 * strength[g] + 0.4 * down[g])
            beat_why = "正好在重拍上" if down[g] else ("踩在强拍上" if strength[g] > 0.6 else "")
        total = 0.75 * sc["visual"] + 0.25 * beat
        report.append({"index": i, "score": round(total, 3), "visual": sc["visual"], "beat": round(beat, 3),
                       "parts": {k: sc.get(k) for k in ("layers", "whole", "steady", "contrast", "penalty")},
                       "why": "，".join(x for x in (sc.get("why"), beat_why) if x), "why_not": sc.get("why_not", ""),
                       "picked": False})
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    manual = [g for g in p.looks.get("grow_presets") or []]
    near_manual = {i for i, s in enumerate(p.shots) for g in manual if i and abs(s.t_start - g["land"]) < 0.05}
    off = {i for i, s in enumerate(p.shots) if s.grow and s.grow.get("off")}
    k = min(AUTO_MAX, max(1, round(p.duration / AUTO_EVERY))) - len(manual)
    picked: list[int] = []
    for r in sorted(report, key=lambda r: -r["score"]):
        i = r["index"]
        if len(picked) >= k or r["score"] < AUTO_THRESHOLD:
            break
        if i == 0:
            r["why_not"] = (r["why_not"] + "，" if r["why_not"] else "") + "开场没有上一个镜头"
            continue
        if i in off or p.shots[i].drawn or any(abs(i - j) <= 1 for j in picked + sorted(near_manual)):
            continue
        try:
            p, _ = grow_preset(analysis, p, i, 3, "beat" if float(m.beat_period) >= 0.38 else "half", "auto", strict=True)
        except ValueError as e:
            r["why_not"] = (r["why_not"] + "，" if r["why_not"] else "") + str(e)
            continue
        picked.append(i)
        r["picked"] = True
    for g, i in zip([g for g in p.looks.get("grow_presets") or [] if g.get("source") == "auto"], picked):
        g["why"] = report[i]["why"][:80]
    notes = [f"自动给 {len(picked)} 个切点加了万物生长（都是画中画层 + 抠像 + 关键帧，可以直接改）" if picked
             else "没有特别适合万物生长的切点，这次没有自动加"]
    return p, notes, report


GROW_STEPS = ["复制下一个镜头的画面，叠在画中画轨道上", "每一层抠出一个完整的部分：前景物体 → 加上地面 → 加上中景，最后一层是整个画面",
              "每层加两个关键帧：从画面下方升到原位，前快后慢，带动感模糊", "各层按节拍错开（一拍或半拍），最后一层落定时切到下一个镜头"]


def _grow_subject(info: dict, rgb: np.ndarray | None = None) -> dict | None:
    """The one whole thing a 万物生长 can open with (a person, a car, an animal standing in the lower part of the
    picture, whole and in focus - not a passer-by smeared across the lens or cut by the side of the frame), as an
    抠像 point target - or None."""
    import cv2
    from .cutout import _clean, wholeness
    sharp = None
    if rgb is not None:
        sharp = np.abs(cv2.Laplacian(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), cv2.CV_32F))
    best = None
    for m in info["things"]:
        m = _clean(m)
        a = float(m.mean())
        if not 0.04 <= a <= 0.45:
            continue
        wh = wholeness(m)
        ys = np.nonzero(m.any(1))[0]
        if wh["pieces"] != 1 or wh["solidity"] < 0.6 or ys[-1] < 0.55 * m.shape[0]:
            continue
        xs = np.nonzero(m.any(0))[0]
        if xs[0] <= 1 or xs[-1] >= m.shape[1] - 2:        # cut by the side of the frame
            continue
        if sharp is not None and sharp[m].mean() < 0.6 * sharp.mean():
            continue                                       # out of focus / motion-blurred
        if best is None or a > best[0]:
            best = (a, m)
    if best is None:
        return None
    d = cv2.distanceTransform(best[1].astype(np.uint8), cv2.DIST_L2, 5)
    y, x = np.unravel_index(int(np.argmax(d)), d.shape)      # well inside it, so the click lands on it
    return {"kind": "object", "x": round(x / d.shape[1], 4), "y": round(y / d.shape[0], 4)}


def _grow_specs(info: dict, n: int, rgb: np.ndarray | None = None) -> list[tuple[dict, str]]:
    """What each layer of a 万物生长 shows (an 抠像 target and a name), in rising order, as in the reference
    videos: the one whole subject if there is one, then solid bands up from the bottom of the picture - the
    ground with the loose things on it, then up to the middle / far part (each band holds everything below its
    top edge) - and last the whole picture, sky and all."""
    from .cutout import PART_NAMES
    area = {k: float(v.mean()) for k, v in info["regions"].items()}
    bands = [p for p in ("ground", "mid", "far") if area.get(p, 0) >= 0.03]
    if area.get("sky", 0) < 0.03 and len(bands) > 1:
        bands = bands[:-1]                        # no sky: the whole picture is the last piece anyway
    steps: list[tuple[dict, str]] = []
    subj = _grow_subject(info, rgb)
    if subj is not None:
        steps.append((subj, "主体"))
    cum = ["things"]                              # small loose things go with the ground they stand on
    for p in bands:
        cum = cum + [p]
        steps.append(({"kind": "region", "parts": list(cum), "band": True}, ("加上" if steps else "") + PART_NAMES[p]))
    want = n - 1
    if len(steps) > want:                         # the front piece, then evenly up to the last
        idx = [0] + [round(j * (len(steps) - 1) / max(1, want - 1)) for j in range(1, want)] if want > 1 else [0]
        steps = [steps[i] for i in idx]
    return steps[:n - 1] + [([], "整个画面")]


def grow_preset(analysis: Analysis, p: Plan, index: int, layers: int = 3, stagger: str = "beat", source: str = "manual",
                strict: bool = False) -> tuple[Plan, list[str]]:
    """万物生长 on the cut into shot `index`, made only of ordinary editable things: copies of the shot on 画中画
    tracks, each with a 抠像 mask of one complete part (cumulative from the front), two position keyframes (from
    below the frame to its place, ease-out) with motion blur, started a beat or half-beat apart; the main-track
    cut moves to the moment the last layer lands, so the previous shot plays on underneath. strict: refuse
    when the cut pieces don't look whole (for the automatic pick)."""
    from .cutout import _clean, _frames, analyse, first_mask, wholeness
    from .formats import output_size
    if not 1 <= index < len(p.shots):
        raise ValueError("万物生长要从上一个镜头长出来，开场镜头不能用")
    s = p.shots[index]
    fps = float(p.template["output"]["fps"])
    w, h = output_size(p.template)
    frame0 = _frames(s.clip.path, s.src_start, 1, fps, w / h)[0]
    info = analyse(frame0)
    specs = _grow_specs(info, max(2, min(4, layers)), frame0)
    if len(specs) < 2:
        raise ValueError("这个镜头分不出层次，做不了万物生长")
    beat = float(p.music.beat_period)
    # as in the references: each layer rises for 0.3-0.5 s, the whole growth takes 2-3 beats (1-1.6 s)
    rise = min(0.5, max(0.3, 0.9 * beat))
    span = min(1.6, max(1.0, 2.5 * beat))
    step = max(0.2, min(beat, (span - rise) / max(1, len(specs) - 1)))
    if stagger != "beat":
        step = max(0.15, min(step, beat / 2))
    n = len(specs)
    t_cut = s.t_start
    room = (s.t_end - s.t_start) - MIN_SHOT - 0.05     # the shot must keep its minimum length after the moved cut
    notes = []
    want = (n - 1) * step + rise
    if want > room and index + 1 < len(p.shots):
        # one click should just work: make the shot longer - its end goes to the next beat with room (the
        # next shot gives up the time); only the timing changes, so it's undone like any cut move
        need = t_cut + want + MIN_SHOT + 0.05
        grid = analysis.music.grid
        later = grid[grid >= need - 1e-6]
        try:
            q, _ = move_cut(analysis, p, index + 1, float(later[0]) if len(later) else need, snap=False)
            if q.shots[index].t_end > s.t_end + 1e-3:
                notes.append(f"万物生长需要时间，这个镜头从 {s.t_end - s.t_start:.1f} 秒拉长到 {q.shots[index].t_end - t_cut:.1f} 秒")
                p, s = q, q.shots[index]
                room = (s.t_end - s.t_start) - MIN_SHOT - 0.05
        except ValueError:
            pass
    lead = 0.0
    want = (n - 1) * step + rise
    if want > room and not strict:
        # still short (the next shot can't give more): start rising over the end of the shot before - it plays
        # on underneath anyway; the layers then show this shot's footage from that much earlier
        prev = p.shots[index - 1]
        lead = max(0.0, min(want - room, prev.t_end - prev.t_start - MIN_SHOT - 0.05, s.src_start))
        if lead < 0.02:
            lead = 0.0
        t_cut -= lead
        room += lead
    if (n - 1) * step + rise > room:
        rise = max(0.3, min(rise, room - 0.15 * (n - 1)))
        step = max(0.15, (room - rise) / max(1, n - 1))
    land = (n - 1) * step + rise
    if land > room:
        raise ValueError("这个镜头太短，放不下万物生长")
    prev = p.shots[index - 1]
    clip_a = _clip_len(analysis, prev.clip.path)
    if clip_a is not None and not strict:
        # the shot before can't play on past the end of its clip: start that much earlier instead
        over = t_cut + land - (prev.t_start + clip_a - prev.src_start - 0.05)
        if over > 1e-3:
            more = max(0.0, min(over, prev.t_end - prev.t_start - MIN_SHOT - 0.05 - lead, s.src_start - lead))
            t_cut -= more
            lead += more
            room_a = prev.t_start + clip_a - prev.src_start - 0.05 - t_cut      # and if still not enough, faster
            if land > room_a >= 0.3 + 0.15 * (n - 1):
                rise = max(0.3, min(rise, room_a - 0.15 * (n - 1)))
                step = (room_a - rise) / max(1, n - 1)
                land = (n - 1) * step + rise
    for k, (target, label) in enumerate(specs[:-1]):
        wh = wholeness(_clean(first_mask(info, target, frame0.shape[:2])))
        if wh["area"] < 0.02 or wh["pieces"] > 4 or (k == 0 and wh["solidity"] < 0.35):
            if strict:
                raise ValueError(f"「{label}」抠出来不完整")
            notes.append(f"「{label}」这一层抠得不太完整，可以在画中画面板里改抠像")
    # tracks: the group needs n free lanes over [cut, land]; the first layer to rise goes on top
    others = [o for o in p.looks.get("overlays") or []]
    busy = lambda k: any(o["track"] == k and o["t_start"] < t_cut + land and t_cut < o["t_end"] for o in others)
    base = next((b for b in range(0, OVERLAY_TRACKS - n + 1) if not any(busy(b + j) for j in range(n))), None)
    if base is None:
        raise ValueError("这里的画中画轨道不够用了，先挪开一些画中画")
    gid = f"grow{int(time.time() * 1000) % 10**9}"
    new = []
    for k, (target, label) in enumerate(specs):
        t0 = t_cut + k * step
        o = {"id": f"{gid}_{k + 1}", "track": base + (n - 1 - k), "clip": s.clip.path, "src_start": s.src_start - lead + k * step,
             "t_start": t0, "t_end": t_cut + land + 1.0 / fps, "x": 0, "y": 0, "w": 1, "h": 1, "opacity": 1, "radius": 0,
             "mblur": 1.0, "keys": [{"t": 0, "x": 0.5, "y": 1.5, "s": 1, "r": 0, "e": "out"}, {"t": round(rise, 3), "x": 0.5, "y": 0.5, "s": 1, "r": 0}],
             "group": {"id": gid, "kind": "grow", "layer": k + 1, "of": n, "label": label}}
        if target:
            o["mask"] = {"shape": "cutout", "target": target, "feather": 0.004}
        new.append(o)
    q, more = move_cut(analysis, p, index, t_cut + land, snap=False)
    if abs(q.shots[index].t_start - (t_cut + land)) > 1.5 / fps:
        raise ValueError("上一个镜头的素材不够长，放不下万物生长")
    looks = dict(q.looks)
    looks["overlays"] = _check_overlays(analysis, q.duration, others + new)
    looks["grow_presets"] = [g for g in (looks.get("grow_presets") or [])] + [
        {"id": gid, "cut": round(t_cut + lead, 4), "land": round(t_cut + land, 4), "layers": n, "stagger": stagger, "source": source}]
    return replace(q, looks=looks), notes + more


def remove_grow_preset(analysis: Analysis, p: Plan, gid: str, remember: bool = False) -> tuple[Plan, list[str]]:
    """Take a 万物生长 away again: its layers go and the cut moves back to the beat it was on. remember: the
    user took it away - the automatic pick won't put one on that cut again."""
    g = next((x for x in p.looks.get("grow_presets") or [] if x["id"] == gid), None)
    if g is None:
        return p, []
    k = next((i for i, s in enumerate(p.shots) if i and abs(s.t_start - g["land"]) < 0.05), None)
    q = p
    if k is not None:
        q, _ = move_cut(analysis, p, k, g["cut"], snap=False)
    if remember and k is not None:
        q = replace(q, shots=[replace(s, grow={"off": True, "source": "manual"}) if i == k else s for i, s in enumerate(q.shots)])
    looks = dict(q.looks)
    looks["overlays"] = [o for o in looks.get("overlays") or [] if (o.get("group") or {}).get("id") != gid]
    looks["grow_presets"] = [x for x in looks.get("grow_presets") or [] if x["id"] != gid]
    return replace(q, looks=looks), []


def drawn_layer(analysis: Analysis, p: Plan, index: int, x: float | None = None, y: float | None = None,
                style: str = "lineart", reveal_at: float | None = None) -> tuple[Plan, list[str]]:
    """手绘 made of ordinary editable things: a 画中画 layer of shot `index`'s own footage with a 抠像 of one object
    (the one at x, y - or the biggest whole one), held from the first frame and a little wider, carrying the
    手绘 effect in `style` with its 绘制进度 keyframes: 0 blank -> 1 drawn (by ~1.1 s, waiting for the beat) ->
    2 the real picture again, ending on the strongest beat in the shot's reveal window (or `reveal_at`)."""
    from .cutout import _clean, _frames, analyse, first_mask
    from .formats import output_size
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range")
    s = p.shots[index]
    fps = float(p.template["output"]["fps"])
    w, h = output_size(p.template)
    frame0 = _frames(s.clip.path, s.src_start, 1, fps, w / h)[0]
    info = analyse(frame0)
    if x is None or y is None:
        whole = [m for m in info["things"] if 0.04 <= m.mean() <= 0.6]
        if not whole:
            raise ValueError("这个镜头里没找到能画的完整物体，可以点「点选物体」自己选一个")
        m = max(whole, key=lambda m: m.sum())
        ys, xs = np.nonzero(m)
        x, y = float(xs.mean() / m.shape[1]), float(ys.mean() / m.shape[0])
    target = {"kind": "object", "x": round(float(x), 4), "y": round(float(y), 4)}
    m = _clean(first_mask(info, target, frame0.shape[:2]))
    if m.mean() < 0.01:
        raise ValueError("点的地方没有能画的物体，换个位置点一下")
    notes: list[str] = []
    if reveal_at is None:
        # the timing of the drawn shots (edit_shot): reveal on the strongest beat of the window, the shot made
        # longer up to a strong beat when it's too short (the next shots give up the time)
        q, notes = edit_shot(analysis, p, index, drawn={"style": style})
        s = q.shots[index]
        reveal_at = s.t_start + float(s.drawn["reveal_at"])
        p = replace(q, shots=[replace(x, drawn=None, role="drawn" if j == index else x.role) if j == index else x
                              for j, x in enumerate(q.shots)])
        s = p.shots[index]
    t0 = s.t_start
    R = reveal_at - t0
    if R < 0.4 or R + 0.4 > s.t_end - t0 + 1e-6:
        raise ValueError("这个镜头太短，放不下手绘（画线、上色、变回实拍至少要 0.8 秒）")
    drawn_by = min(1.1, R)
    keys = [{"t": 0, "v": 0}, {"t": round(drawn_by, 3), "v": 1}]
    if R > drawn_by + 1e-3:
        keys.append({"t": round(R, 3), "v": 1})
    keys.append({"t": round(R + 0.4, 3), "v": 2})
    others = list(p.looks.get("overlays") or [])
    t1 = t0 + R + 0.4 + 2.0 / fps
    track = next((k for k in range(OVERLAY_TRACKS) if not any(o["track"] == k and o["t_start"] < t1 and t0 < o["t_end"] for o in others)), None)
    if track is None:
        raise ValueError("这里的画中画轨道不够用了")
    from .looks import DRAWN_STYLES
    name = next((d["name"] for d in DRAWN_STYLES if d["id"] == style), "手绘")
    gid = f"drawn{int(time.time() * 1000) % 10**9}"
    o = {"id": gid, "track": track, "clip": s.clip.path, "src_start": s.src_start, "t_start": t0, "t_end": min(t1, s.t_end),
         "x": 0, "y": 0, "w": 1, "h": 1, "opacity": 1, "radius": 0,
         "mask": {"shape": "cutout", "target": target, "hold": True, "dilate": 0.025, "feather": 0.004},
         "fx": {"kind": "drawn", "style": style, "keys": keys},
         "group": {"id": gid, "kind": "drawn", "layer": 1, "of": 1, "label": name}}
    looks = {**p.looks, "overlays": _check_overlays(analysis, p.duration, others + [o])}
    return replace(p, looks=looks), notes


def drawn_to_layers(analysis: Analysis, p: Plan) -> tuple[Plan, list[str]]:
    """The planner's 手绘开场 shots as editable 手绘 layers (the app's plans; the CLI keeps rendering them as before)."""
    notes = []
    for i, s in enumerate(p.shots):
        if not s.drawn:
            continue
        meta = s.drawn
        try:
            p, _ = drawn_layer(analysis, p, i, style=meta.get("style", "lineart"), reveal_at=s.t_start + float(meta["reveal_at"]))
        except ValueError as e:
            notes.append(f"镜头 {i + 1} 的手绘没做成：{e}")
        p = replace(p, shots=[replace(x, drawn=None) if j == i else x for j, x in enumerate(p.shots)])
    return p, notes


def grow_hint(analysis: Analysis, p: Plan, index: int, cache_file: Path | None = None) -> str | None:
    """When a shot is turned on by hand: a short warning if it doesn't suit growing in."""
    from .formats import output_size
    from .growth import AUTO_THRESHOLD, score_shot
    w, h = output_size(p.template)
    s = p.shots[index]
    sc = score_shot(s.clip.path, s.src_start, w / h)
    if sc["visual"] >= AUTO_THRESHOLD * 0.9 or not sc.get("why_not"):
        return None
    return f"这个镜头不太适合万物生长（{sc['why_not']}），效果可能不明显"


def edit_shot(analysis: Analysis, p: Plan, index: int, sticker=KEEP, drawn=KEEP, grow=KEEP) -> tuple[Plan, list[str]]:
    """Hand edits from the app's shot inspector. Returns the new plan and notes for the user (Chinese).
    sticker: None = no sticker; {"id", "mode", "keys", "in", "out"} = placed by hand (any catalog sticker).
    drawn:   None = not drawn; {"style", "box"?, "force"?} = draw this shot. A drawn shot needs time to draw,
             so a short one is lengthened to a strong beat; the following shot(s) give up that time (their
             cuts stay on the beat grid and the total length is unchanged)."""
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range (plan has {len(p.shots)})")
    tpl = p.template
    fps = float(tpl["output"]["fps"])
    shots = [replace(s) for s in p.shots]
    s = shots[index]
    notes: list[str] = []
    if grow is not KEEP:
        s.grow = check_grow(grow) if grow else None
        if s.grow and not s.grow.get("off"):
            hint = grow_hint(analysis, p, index)
            if hint:
                notes.append(hint)
    if sticker is not KEEP:
        if sticker is None:
            s.sticker, s.sticker_at = None, None
            if s.role == "sticker":
                s.role = "body"
        else:
            sid = check_stickers([sticker["id"]])[0]
            s.sticker, s.sticker_at = sid, _check_place(sticker)
            if s.drawn:                       # one effect per shot
                s.drawn = None
                s.role = "hook" if index == 0 else "body"
                notes.append("这个镜头原来的手绘已换成贴纸")
    if drawn is not KEEP:
        if drawn is None:
            if s.drawn:
                s.drawn = None
                s.role = "hook" if index == 0 else ("outro" if index == len(shots) - 1 else "body")
        else:
            style = check_drawn_style(drawn.get("style"))
            cfg = tpl.get("drawn_opening" if index == 0 else "drawn_mid") or tpl["drawn_opening"]
            lo, hi = cfg.get("reveal_window", (1.0, 1.7))
            rs = float(cfg.get("reveal_seconds", 0.4))
            tlo, thi = cfg.get("tail", (0.5, 1.1))
            grid, strength = analysis.music.grid, analysis.music.strength
            t0 = s.t_start
            m = (grid >= t0 + lo) & (grid <= t0 + hi)
            reveal = float(grid[m][np.argmax(strength[m])]) if m.any() else t0 + (lo + hi) / 2
            need = reveal + rs + tlo
            if s.t_end < need - 1e-6:
                if index == len(shots) - 1:
                    raise ValueError("最后一个镜头太短，放不下手绘（画线、上色、变回实拍至少要 1.9 秒）")
                em = (grid >= need) & (grid <= reveal + rs + thi)
                new_end = float(grid[em][np.argmax(strength[em])]) if em.any() else need
                clip_len = next((c.duration for c in analysis.clips if _norm(c.path) == _norm(s.clip.path)), None)
                if clip_len is not None and s.src_start + (new_end - t0) > clip_len:
                    raise ValueError(f"这段素材（{s.clip.name}）从这里开始不够长，放不下手绘")
                old_len = s.t_end - t0
                k = index + 1
                merged = 0
                while k < len(shots) and shots[k].t_start < new_end - 1e-6:
                    nxt = shots[k]
                    if nxt.t_end - new_end >= 0.4 or k == len(shots) - 1:
                        if nxt.t_end - new_end < 0.4:
                            raise ValueError("后面只剩结尾镜头，没有足够的时间拉长这个镜头")
                        cut = new_end - nxt.t_start
                        nxt.src_start += cut
                        nxt.t_start = new_end
                        nxt.frames = _frames_between(nxt.t_start, nxt.t_end, fps)
                        if nxt.sticker_at:
                            nxt.sticker_at = _shift_keys(nxt.sticker_at, -cut)
                        break
                    shots.pop(k)              # swallowed whole by the longer drawn shot
                    merged += 1
                s.t_end = new_end
                s.frames = _frames_between(t0, new_end, fps)
                notes.append(f"手绘需要时间，这个镜头从 {old_len:.1f} 秒拉长到 {new_end - t0:.1f} 秒（停在重拍上）"
                             + (f"，后面 {merged} 个镜头被合并掉了" if merged else "，后一个镜头相应缩短"))
            s.sticker, s.sticker_at = None, None
            s.role = "drawn"
            meta = {"reveal_at": round(reveal - t0, 3), "tail": round(s.t_end - reveal - rs, 3),
                    "label": drawn.get("label") or (s.drawn or {}).get("label") or "手绘", "manual": True}
            if style != DEFAULT_DRAWN_STYLE:
                meta["style"] = style
            if drawn.get("box"):
                meta["box"] = [round(min(1.0, max(0.0, float(v))), 4) for v in drawn["box"]]
            if drawn.get("force"):
                meta["force"] = True
            s.drawn = meta
    return replace(p, shots=shots), notes


def _shift_keys(place: dict, dt: float) -> dict:
    """Keyframe times after the shot start moved by dt (the shot now starts later / earlier in its clip)."""
    out = dict(place)
    out["keys"] = [{**k, "t": max(0.0, k["t"] + dt)} for k in place["keys"]]
    out["in"] = max(0.0, float(place.get("in") or 0.0) + dt)
    if place.get("out") is not None:
        out["out"] = max(0.0, float(place["out"]) + dt)
    return out


def has_manual(p: Plan) -> bool:
    return bool(p.looks.get("edited_cuts")) or any(s.sticker_at or (s.drawn and s.drawn.get("manual")) for s in p.shots)


MIN_SHOT = 0.4           # shortest shot a cut edit may leave


def _drawn_cfg(tpl: dict, index: int) -> tuple[float, float]:
    """(reveal seconds, shortest tail) of a drawn shot."""
    cfg = tpl.get("drawn_opening" if index == 0 else "drawn_mid") or tpl["drawn_opening"]
    return float(cfg.get("reveal_seconds", 0.4)), float(cfg.get("tail", (0.5, 1.1))[0])


def _retail(s: Shot, rs: float) -> None:
    """A drawn shot's tail after its length or reveal moved."""
    s.drawn = {**s.drawn, "tail": round(s.t_end - s.t_start - s.drawn["reveal_at"] - rs, 3)}


def _clip_len(analysis: Analysis, path: str) -> float | None:
    return next((c.duration for c in analysis.clips if _norm(c.path) == _norm(path)), None)


def move_cut(analysis: Analysis, p: Plan, k: int, t: float, snap: bool = True) -> tuple[Plan, list[str]]:
    """Move the cut between shot k-1 and shot k to timeline time `t` (snapped to the nearest beat unless
    snap=False). The shot before ends there, the one after starts there and shows the same footage it showed
    at that moment (its start in the clip moves with the cut), so nothing else moves and the total stays."""
    if not 1 <= k < len(p.shots):
        raise IndexError(f"cut {k} out of range (plan has {len(p.shots)} shots)")
    fps = float(p.template["output"]["fps"])
    shots = [replace(s) for s in p.shots]
    a, b = shots[k - 1], shots[k]
    lo, hi = a.t_start + MIN_SHOT, b.t_end - MIN_SHOT
    if a.drawn:                                   # the drawing has to finish before the cut
        rs, tail = _drawn_cfg(p.template, k - 1)
        lo = max(lo, a.t_start + a.drawn["reveal_at"] + rs + tail)
    if b.drawn:                                   # and a drawn shot after it needs its drawing time
        rs, tail = _drawn_cfg(p.template, k)
        hi = min(hi, b.t_start + b.drawn["reveal_at"] - 0.5, b.t_end - rs - tail)
    clip_a = _clip_len(analysis, a.clip.path)
    if clip_a is not None:                        # can't show more of a clip than there is
        hi = min(hi, a.t_start + (clip_a - a.src_start) - 0.05)
    lo = max(lo, b.t_start - b.src_start)         # nor start the next shot before its clip starts
    if lo > hi:
        raise ValueError("这两个镜头已经不能再调了（太短，或者手绘需要的时间不够）")
    if snap:
        grid = analysis.music.grid
        inside = grid[(grid >= lo - 1e-6) & (grid <= hi + 1e-6)]
        if len(inside):
            t = float(inside[np.argmin(np.abs(inside - t))])
    t = float(min(max(t, lo), hi))
    d = t - b.t_start
    a.t_end = t
    a.frames = _frames_between(a.t_start, a.t_end, fps)
    b.t_start = t
    b.src_start += d
    b.frames = _frames_between(b.t_start, b.t_end, fps)
    if b.sticker_at:
        b.sticker_at = _shift_keys(b.sticker_at, -d)
    if b.drawn:
        b.drawn = {**b.drawn, "reveal_at": round(b.drawn["reveal_at"] - d, 3)}
        _retail(b, _drawn_cfg(p.template, k)[0])
    if a.drawn:
        _retail(a, _drawn_cfg(p.template, k - 1)[0])
    return replace(p, shots=shots, looks={**p.looks, "edited_cuts": True}), []


def split_shot(analysis: Analysis, p: Plan, index: int, t: float) -> tuple[Plan, list[str]]:
    """分割: shot `index` becomes two at timeline time `t` (to the frame): the same footage carries on across
    the new cut. A placed sticker's keyframes go to the half they fall in; a drawing / 万物生长 stays with the
    first half."""
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range")
    fps = float(p.template["output"]["fps"])
    s = p.shots[index]
    t = round(t * fps) / fps
    if t - s.t_start < MIN_SHOT - 1e-6 or s.t_end - t < MIN_SHOT - 1e-6:
        raise ValueError("分割点离片段两头太近了（每段至少 0.4 秒）")
    d = t - s.t_start
    a = replace(s, t_end=t, frames=_frames_between(s.t_start, t, fps))
    b = replace(s, t_start=t, src_start=s.src_start + d, frames=_frames_between(t, s.t_end, fps), drawn=None, grow=None,
                role="body" if s.role in ("hook", "drawn") else s.role)
    if s.sticker_at:
        keys_a = [k for k in s.sticker_at["keys"] if k["t"] < d]
        keys_b = [{**k, "t": round(k["t"] - d, 4)} for k in s.sticker_at["keys"] if k["t"] >= d]
        a = replace(a, sticker_at={**s.sticker_at, "keys": keys_a} if keys_a else None, sticker=s.sticker if keys_a else None)
        b = replace(b, sticker_at=_shift_keys({**s.sticker_at, "keys": keys_b}, 0) if keys_b else None, sticker=s.sticker if keys_b else None)
        if b.sticker_at:
            b.sticker_at["in"] = max(0.0, float(s.sticker_at.get("in") or 0) - d)
    if s.drawn and s.drawn["reveal_at"] + _drawn_cfg(p.template, index)[0] > d:
        a = replace(a, drawn=None, role="hook" if index == 0 else "body")
    shots = list(p.shots[:index]) + [a, b] + list(p.shots[index + 1:])
    return replace(p, shots=shots, looks={**p.looks, "edited_cuts": True}), []


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


def trim_shot(analysis: Analysis, p: Plan, index: int, side: str, t: float, snap: bool = False) -> tuple[Plan, list[str]]:
    """修剪, as in 剪映: drag shot `index`'s start edge ('in') or end edge ('out') to timeline time `t`. The shot
    gets shorter or longer and everything after it moves up or back (the 画中画 layers and 万物生长 that start
    after its end too), so the video gets shorter or longer. 'in' starts the footage later (or earlier) in the
    clip; the shot itself stays where it starts. snap: the edge goes onto the nearest beat."""
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range")
    fps = float(p.template["output"]["fps"])
    s = p.shots[index]
    if snap:
        grid = analysis.music.grid
        if len(grid):
            t = float(grid[np.argmin(np.abs(grid - t))])
    clip_len = _clip_len(analysis, s.clip.path) or 1e9
    min_n = int(np.ceil(MIN_SHOT * fps))
    if side == "out":
        n = int(round((t - s.t_start) * fps))
        n = max(min_n, min(n, int((clip_len - s.src_start - 0.05) * fps)))
        dn, src = n - s.frames, s.src_start
    else:
        cut = int(round((t - s.t_start) * fps))           # frames taken off the front (negative: put back)
        cut = max(-int(s.src_start * fps), min(cut, s.frames - min_n))
        dn, src = -cut, s.src_start + cut / fps
    if dn == 0 and src == s.src_start:
        return p, []
    d = dn / fps
    if s.drawn:
        rs, tail = _drawn_cfg(p.template, index)
        if s.drawn["reveal_at"] + rs + tail > (s.frames + dn) / fps + 1e-6:
            raise ValueError("手绘需要时间，这个镜头不能再短了")
    edge = s.t_end
    first = replace(s, src_start=src, t_end=s.t_end + d, frames=s.frames + dn)
    if s.sticker_at and side == "in":
        first = replace(first, sticker_at=_shift_keys(s.sticker_at, -(src - s.src_start)))
    after = [replace(x, t_start=x.t_start + d, t_end=x.t_end + d) for x in p.shots[index + 1:]]
    shots = list(p.shots[:index]) + [first] + after
    looks = dict(p.looks)
    moved = []
    for o in looks.get("overlays") or []:
        if o["t_start"] >= edge - 1e-3:
            o = {**o, "t_start": o["t_start"] + d, "t_end": o["t_end"] + d}
        moved.append(o)
    looks["grow_presets"] = [({**g, "cut": round(g["cut"] + d, 4), "land": round(g["land"] + d, 4)} if g["cut"] >= edge - 1e-3 else g)
                             for g in looks.get("grow_presets") or []]
    looks["edited_cuts"] = True
    q = replace(p, shots=shots, looks=looks)
    looks["overlays"] = _check_overlays(analysis, q.duration, moved)
    return replace(q, looks=looks), []


def move_shot(analysis: Analysis, p: Plan, src: int, dst: int) -> tuple[Plan, list[str]]:
    """Move shot `src` to position `dst` (as in a timeline drag). Every shot keeps its footage (clip and start
    in it), its sticker and drawing; the shots are laid end to end in the new order and each cut then goes to
    the nearest beat (at least MIN_SHOT per shot, never past the end of a clip, drawn shots keep room to draw,
    the total length is unchanged). Drawn shots reveal on the strongest beat near where they did before."""
    n = len(p.shots)
    if not (0 <= src < n and 0 <= dst < n):
        raise IndexError(f"shot {src} -> {dst} out of range (plan has {n})")
    if src == dst:
        return p, []
    fps = float(p.template["output"]["fps"])
    grid, strength = analysis.music.grid, analysis.music.strength
    shots = [replace(s) for s in p.shots]
    s = shots.pop(src)
    shots.insert(dst, s)
    total = p.shots[-1].t_end
    lens = [x.t_end - x.t_start for x in shots]
    # cuts: nominal = shots end to end with their own lengths, then the nearest allowed beat
    cuts, prev, nominal = [], 0.0, 0.0
    for i in range(1, n):
        nominal += lens[i - 1]
        a = shots[i - 1]
        lo = prev + MIN_SHOT
        hi = total - MIN_SHOT * (n - i)
        if a.drawn:                                    # room to draw, reveal and show the tail
            rs, tail = _drawn_cfg(p.template, i - 1)
            lo = max(lo, prev + min(lens[i - 1], 0.6 + rs + tail))
        clip_a = _clip_len(analysis, a.clip.path)
        if clip_a is not None:                         # can't show more of a clip than there is
            hi = min(hi, prev + (clip_a - a.src_start) - 0.05)
        hi = max(hi, lo)
        cand = grid[(grid >= lo - 1e-6) & (grid <= hi + 1e-6)]
        t = float(cand[np.argmin(np.abs(cand - nominal))]) if len(cand) else min(max(nominal, lo), hi)
        cuts.append(t)
        prev = t
    bounds = [0.0] + cuts + [total]
    notes = []
    for i, x in enumerate(shots):
        x.t_start, x.t_end = bounds[i], bounds[i + 1]
        x.frames = _frames_between(x.t_start, x.t_end, fps)
        if x.role in ("hook", "outro", "body"):        # opening / closing roles belong to positions
            x.role = "hook" if i == 0 else ("outro" if i == n - 1 else "body")
        if x.drawn:
            rs, tail = _drawn_cfg(p.template, i)
            want = x.t_start + x.drawn["reveal_at"]
            m = (grid >= x.t_start + 0.6) & (grid <= x.t_end - rs - tail)
            reveal = float(grid[m][np.argmax(strength[m] - 0.3 * np.abs(grid[m] - want))]) if m.any() else None
            if reveal is None:
                x.drawn = None
                x.role = "hook" if i == 0 else ("outro" if i == n - 1 else "body")
                notes.append(f"镜头 {i + 1} 移动后太短，放不下手绘，已改成普通镜头")
            else:
                x.drawn = {**x.drawn, "reveal_at": round(reveal - x.t_start, 3)}
                _retail(x, rs)
        if x.sticker_at and x.sticker_at.get("out") is not None:
            x.sticker_at = {**x.sticker_at, "out": min(float(x.sticker_at["out"]), x.t_end - x.t_start)}
    return replace(p, shots=shots, looks={**p.looks, "edited_cuts": True}), notes


OVERLAY_TRACKS = 6       # 画中画 1..6 above the main track; a higher track is drawn on top
MIN_OVERLAY = 0.3


def _check_overlays(analysis: Analysis, total: float, overlays) -> list[dict]:
    """Picture-in-picture clips laid over the edit (see pip.py for every field). A 宫格 (grid) carries its
    clips in grid.cells instead of one clip; its first cell's clip stands in as the overlay's clip."""
    from .pip import ANIMS, BLENDS, EASES, LAYOUTS, MASKS
    clips = {_norm(c.path): c for c in analysis.clips}

    def clip_of(path: str):
        c = clips.get(_norm(path))
        if c is None:
            raise ValueError(f"画中画的素材不在这个项目里：{Path(path).name}")
        return c

    def num(v, lo, hi, default):
        try:
            return min(hi, max(lo, float(v)))
        except (TypeError, ValueError):
            return default

    out = []
    for n, o in enumerate(overlays or []):
        grid = o.get("grid")
        if grid:
            layout = grid.get("layout")
            if layout not in LAYOUTS:
                raise ValueError(f"unknown grid layout {layout!r}")
            k = LAYOUTS[layout][0] * LAYOUTS[layout][1]
            cells = []
            for cell in (grid.get("cells") or [])[:k]:
                c = clip_of(cell["clip"])
                cells.append({"clip": c.path, "name": c.name,
                              "src_start": round(num(cell.get("src_start"), 0, max(0.0, c.duration - MIN_OVERLAY), 0.0), 3)})
            if len(cells) < k:
                raise ValueError(f"这个宫格需要 {k} 段素材")
            c = clip_of(cells[0]["clip"])
            src = cells[0]["src_start"]
            room = min(clip_of(x["clip"]).duration - x["src_start"] for x in cells)
        else:
            c = clip_of(o["clip"])
            src = num(o.get("src_start"), 0, max(0.0, c.duration - MIN_OVERLAY), 0.0)
            room = c.duration - src
        t0 = num(o.get("t_start"), 0, total - MIN_OVERLAY, 0.0)
        t1 = min(num(o.get("t_end"), 0, total, total), t0 + room)
        if t1 - t0 < MIN_OVERLAY - 1e-6:
            raise ValueError("画中画片段太短（至少 0.3 秒）")
        w = num(o.get("w"), 0.05, 1.0, 0.5)
        h = num(o.get("h"), 0.05, 1.0, 0.5)
        item = {"id": str(o.get("id") or f"ov{n}"), "track": int(num(o.get("track"), 0, OVERLAY_TRACKS - 1, 0)),
                "clip": c.path, "name": c.name, "src_start": round(src, 3), "t_start": round(t0, 3), "t_end": round(t1, 3),
                "x": round(num(o.get("x"), 0.02 - w, 0.98, 0.0), 4), "y": round(num(o.get("y"), 0.02 - h, 0.98, 0.0), 4),
                "w": round(w, 4), "h": round(h, 4), "rot": round(num(o.get("rot"), -360, 360, 0.0), 2),
                "sc": round(num(o.get("sc"), 0.05, 5, 1.0), 4),
                "opacity": round(num(o.get("opacity"), 0.05, 1, 1.0), 3),
                "radius": round(num(o.get("radius"), 0, 0.5, 0.0), 3), "border": bool(o.get("border")),
                "blend": o.get("blend") if o.get("blend") in BLENDS else "normal"}
        if o.get("link") and not grid:
            item["link"] = True
        if grid:
            item["grid"] = {"layout": grid["layout"], "cells": cells, "gap": round(num(grid.get("gap"), 0, 0.08, 0.01), 3)}
            item["name"] = {"2v": "上下分屏", "2h": "左右分屏", "3v": "三宫格", "4": "四宫格", "9": "九宫格"}[grid["layout"]]
        m = o.get("mask") or {}
        if m.get("shape") == "cutout" and not grid:
            def cut_target(tg):
                if tg.get("kind") == "object":
                    target = {"kind": "object", "x": round(num(tg.get("x"), 0, 1, 0.5), 4), "y": round(num(tg.get("y"), 0, 1, 0.5), 4)}
                elif tg.get("kind") == "lasso":
                    target = {"kind": "lasso", "pts": [[round(num(q[0], 0, 1, 0), 4), round(num(q[1], 0, 1, 0), 4)]
                                                       for q in (tg.get("pts") or [])[:600]]}
                else:
                    from .cutout import PARTS
                    target = {"kind": "region", "parts": [p_ for p_ in (tg.get("parts") or []) if p_ in PARTS] or ["things"]}
                    if tg.get("band"):
                        target["band"] = True
                if tg.get("t") and target["kind"] != "region":
                    target["t"] = round(num(tg.get("t"), 0, 3600, 0), 3)
                return target
            target = cut_target(m.get("target") or {})
            brush = [{"op": "sub" if b.get("op") == "sub" else "add", "r": round(num(b.get("r"), 0.005, 0.3, 0.04), 4),
                      "pts": [[round(num(q[0], 0, 1, 0), 4), round(num(q[1], 0, 1, 0), 4)] for q in (b.get("pts") or [])[:400]]}
                     for b in (m.get("brush") or [])[:60]]
            item["mask"] = {"shape": "cutout", "target": target, "brush": brush,
                            "feather": round(num(m.get("feather"), 0, 0.1, 0.004), 4), "invert": bool(m.get("invert"))}
            extra = [{"op": "sub" if e.get("op") == "sub" else "add", "target": cut_target(e.get("target") or {})}
                     for e in (m.get("extra") or [])[:20] if isinstance(e, dict)]
            if extra:
                item["mask"]["extra"] = extra
            st = m.get("stroke")
            if isinstance(st, dict):
                col = str(st.get("color") or "#ffffff")
                if not (len(col) == 7 and col[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in col[1:])):
                    col = "#ffffff"
                item["mask"]["stroke"] = {"color": col.lower(), "width": round(num(st.get("width"), 0.003, 0.08, 0.02), 4)}
            if m.get("hold"):
                item["mask"]["hold"] = True
            if m.get("dilate"):
                item["mask"]["dilate"] = round(num(m.get("dilate"), 0, 0.1, 0.02), 4)
        elif m.get("shape", "none") != "none" and not grid:
            if m["shape"] not in MASKS:
                raise ValueError(f"unknown mask {m['shape']!r}")
            item["mask"] = {"shape": m["shape"], "cx": round(num(m.get("cx"), -0.5, 1.5, 0.5), 4),
                            "cy": round(num(m.get("cy"), -0.5, 1.5, 0.5), 4), "size": round(num(m.get("size"), 0.05, 2, 0.8), 4),
                            "rot": round(num(m.get("rot"), -360, 360, 0), 2), "feather": round(num(m.get("feather"), 0, 0.5, 0), 3),
                            "invert": bool(m.get("invert"))}
        keys = []
        for k_ in o.get("keys") or []:
            key = {"t": round(num(k_.get("t"), 0, t1 - t0, 0), 3), "x": round(num(k_.get("x"), -1, 2, 0.5), 4),
                   "y": round(num(k_.get("y"), -1, 2, 0.5), 4), "s": round(num(k_.get("s"), 0.05, 5, 1), 4),
                   "r": round(num(k_.get("r"), -720, 720, 0), 2), "a": round(num(k_.get("a"), 0, 1, 1), 3)}
            if k_.get("e") in EASES and k_.get("e") != "smooth":
                key["e"] = k_["e"]
            keys.append(key)
        if keys:
            item["keys"] = sorted(keys, key=lambda k_: k_["t"])
        if o.get("mblur"):
            item["mblur"] = round(num(o.get("mblur"), 0, 1.5, 0), 2)
        fx = o.get("fx") or {}
        if fx.get("kind") == "drawn" and not grid:
            fk = sorted(({"t": round(num(k_.get("t"), 0, t1 - t0, 0), 3), "v": round(num(k_.get("v"), 0, 2, 0), 3)}
                         for k_ in (fx.get("keys") or [])[:40]), key=lambda k_: k_["t"])
            item["fx"] = {"kind": "drawn", "style": check_drawn_style(fx.get("style")), "keys": fk or [{"t": 0, "v": 2}]}
        if o.get("group"):                            # made by a preset (万物生长 ...): {id, kind, layer, of, label}
            g_ = o["group"]
            item["group"] = {"id": str(g_.get("id")), "kind": str(g_.get("kind")), "layer": int(g_.get("layer", 0)),
                             "of": int(g_.get("of", 1)), "label": str(g_.get("label", ""))[:20]}
        for which in ("anim_in", "anim_out"):
            a = o.get(which) or {}
            if a.get("type", "none") != "none":
                if a["type"] not in ANIMS:
                    raise ValueError(f"unknown animation {a['type']!r}")
                item[which] = {"type": a["type"], "beats": 0.5 if float(a.get("beats", 1)) < 0.75 else 1}
        out.append(item)
    return out


def set_overlays(analysis: Analysis, p: Plan, overlays) -> tuple[Plan, list[str]]:
    return replace(p, looks={**p.looks, "overlays": _check_overlays(analysis, p.duration, overlays)}), []


def _resnap_overlays(analysis: Analysis, overlays: list[dict], total: float) -> tuple[list[dict], list[str]]:
    """Overlays on another song: same times, their ends moved to the nearest beats of the new one."""
    grid = analysis.music.grid
    near = lambda t: float(grid[np.argmin(np.abs(grid - t))]) if len(grid) else t
    out, dropped = [], 0
    for o in overlays:
        if o["t_start"] >= total - MIN_OVERLAY:
            dropped += 1
            continue
        t0, t1 = near(o["t_start"]), near(o["t_end"])
        if t1 - t0 < MIN_OVERLAY:
            t0, t1 = o["t_start"], o["t_end"]
        out.append({**o, "t_start": t0, "t_end": min(t1, total)})
    try:
        out = _check_overlays(analysis, total, out)
    except ValueError:
        out = [o for o in out if o["t_end"] - o["t_start"] >= MIN_OVERLAY]
    return out, ([f"{dropped} 个画中画在新歌结束之后，已去掉"] if dropped else [])


def retime(analysis: Analysis, p: Plan) -> tuple[Plan, list[str]]:
    """Keep this edit (shots, footage, stickers, drawings) but put it on another song: every cut snaps to the
    nearest beat of the new song, drawn shots reveal on its strongest beat, the music reference changes."""
    fps = float(p.template["output"]["fps"])
    grid, strength = analysis.music.grid, analysis.music.strength
    shots = [replace(s) for s in p.shots]
    n = len(shots)
    total = min(p.duration, float(analysis.music.duration) - 0.3)
    cuts, prev = [], 0.0
    for i, s in enumerate(shots[1:], start=1):
        room = total - MIN_SHOT * (n - i)
        cand = grid[(grid >= prev + MIN_SHOT) & (grid <= room)]
        t = float(cand[np.argmin(np.abs(cand - s.t_start))]) if len(cand) else min(max(s.t_start, prev + MIN_SHOT), room)
        cuts.append(t)
        prev = t
    ends = grid[(grid >= prev + 0.8) & (grid <= total + 0.3)]
    end = float(ends[np.argmin(np.abs(ends - total))]) if len(ends) else total
    bounds = [0.0] + cuts + [end]
    notes = []
    for i, s in enumerate(shots):
        old_start = s.t_start
        s.t_start, s.t_end = bounds[i], bounds[i + 1]
        s.frames = _frames_between(s.t_start, s.t_end, fps)
        if s.drawn:
            rs, tail = _drawn_cfg(p.template, i)
            want = old_start + s.drawn["reveal_at"]
            m = (grid >= s.t_start + 0.6) & (grid <= s.t_end - rs - tail)
            reveal = float(grid[m][np.argmax(strength[m] - 0.3 * np.abs(grid[m] - want))]) if m.any() else None
            if reveal is None:
                s.drawn = None
                s.role = "hook" if i == 0 else "body"
                notes.append(f"镜头 {i + 1} 在新歌里太短，放不下手绘，已改成普通镜头")
                continue
            s.drawn = {**s.drawn, "reveal_at": round(reveal - s.t_start, 3)}
            _retail(s, rs)
        if s.sticker_at and s.sticker_at.get("out") is not None:
            s.sticker_at = {**s.sticker_at, "out": min(float(s.sticker_at["out"]), s.t_end - s.t_start)}
    m = analysis.music
    music = MusicRef(m.path, float(m.start), float(m.tempo), float(m.beat_period))
    looks = {**p.looks, "edited_cuts": True}
    if p.looks.get("overlays"):
        looks["overlays"], more = _resnap_overlays(analysis, p.looks["overlays"], bounds[-1])
        notes += more
    return replace(p, shots=shots, music=music, looks=looks), notes


def _overlap(a: Shot, b: Shot) -> float:
    """Seconds of the same footage two shots share."""
    if _norm(a.clip.path) != _norm(b.clip.path):
        return 0.0
    return min(a.src_start + (a.t_end - a.t_start), b.src_start + (b.t_end - b.t_start)) - max(a.src_start, b.src_start)


def carry_manual(old: Plan, new: Plan, analysis: Analysis) -> tuple[Plan, list[str]]:
    """Hand edits of `old` re-applied to `new` (another song or plan): each goes to the new shot that shows the
    same footage (same clip, overlapping time in it). Ones that no longer have a home are dropped and listed."""
    notes, dropped = [], 0
    for s in old.shots:                           # 万物生长 turned on / off by hand: the shot with that footage
        if not (s.grow and s.grow.get("source") == "manual"):
            continue
        best = max(range(len(new.shots)), key=lambda j: _overlap(s, new.shots[j]), default=None)
        if best is not None and _overlap(s, new.shots[best]) > 0:
            new = replace(new, shots=[replace(n, grow=dict(s.grow)) if j == best else n for j, n in enumerate(new.shots)])
    for s in old.shots:
        manual_drawn = bool(s.drawn and s.drawn.get("manual"))
        if not (s.sticker_at or manual_drawn):
            continue
        best, overlap = None, 0.0
        for j, n in enumerate(new.shots):
            if _norm(n.clip.path) != _norm(s.clip.path):
                continue
            ov = min(s.src_start + (s.t_end - s.t_start), n.src_start + (n.t_end - n.t_start)) - max(s.src_start, n.src_start)
            if ov > overlap:
                best, overlap = j, ov
        if best is None:
            dropped += 1
            continue
        n = new.shots[best]
        try:
            if s.sticker_at:
                new, _ = edit_shot(analysis, new, best, sticker={"id": s.sticker.partition("@")[0],
                                                                 **_shift_keys(s.sticker_at, s.src_start - n.src_start)})
            else:
                new, more = edit_shot(analysis, new, best, drawn={**s.drawn, "style": s.drawn.get("style")})
                notes += more
        except ValueError:
            dropped += 1
    if dropped:
        notes.append(f"{dropped} 处手动加的贴纸 / 手绘在新方案里找不到对应镜头，没有保留")
    return new, notes


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
