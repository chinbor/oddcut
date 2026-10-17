"""Local HTTP backend for the desktop app: the analyze / plan / render steps as background jobs.

    python -m oddcut.server [--port 0] [--token T]

Listens on 127.0.0.1 only. Prints one JSON line {"type": "ready", "port": N} when it is up, so the parent
process can read the port (use --port 0 for any free one). Every /api request must carry the header
X-Oddcut-Token: <token> (a web page in a browser can't add it, so it can't drive the backend).
With --parent-pid it exits when that process (the desktop app) goes away.

    GET  /api/health                 {"ok": true, "version": ...}
    GET  /api/templates              styles to pick from
    GET  /api/aspects                output aspect presets
    GET  /api/looks                  stickers and drawn styles to pick from, with small preview pictures
    GET  /api/thumbs?plan=P          a small picture of every shot + a 4-frame filmstrip of each (timeline)
    GET  /api/still?plan=P&index=I&t=S   one frame of shot I, S seconds in, cropped to the output aspect
    GET  /api/waveform?plan=P        the song under the plan: peaks per 20 ms + beat grid (timeline music track)
    POST /api/track {plan, index, place}   path of a sticker being placed (per frame, 0..1), right away
    POST /api/jobs                   {"kind": "analyze" | "plan" | "render", ...}  -> {"id": ...}
    GET  /api/jobs/<id>              status, latest progress per stage, result or error
    POST /api/jobs/<id>/cancel       stops the job at its next progress report

Job parameters (paths are absolute or relative to the project root):
    analyze  clips, music, [template], [aspect], [duration], [stickers], [drawn], [work]
             -> {"analysis": path, "clips": [...], "music": {...}, "aspect": ...}
    plan     analysis, [seed], [work], [stickers], [drawn_style]
             -> {"plan": path, "shots": [...], "duration": s, "seed": n}
             stickers: ids from /api/looks ([] = none; omitted = the original eyes / 💢 / ?)
    render   plan, [out], [preview]    -> {"video": path, "duration": s, "preview": bool}
             preview: low-res and fast, sticker / drawn shots plain unless rendered before
    music    analysis, music, [music_start], [duration]  -> like analyze; only the song is analyzed again
    edit     analysis, plan, index, [clip]  -> like plan; swaps shot `index` to `clip` (or another good clip)
    shot     analysis, plan, index, [sticker], [drawn]  -> like plan + "notes"; hand edits of one shot
    overlays analysis, plan, overlays  -> like plan; the picture-in-picture clips (whole list, see pipeline)
    grow     plan, grow                -> like plan; 万物生长 into every shot ({layers, stagger, blur}) or none (null)
             (one shot: "shot" with grow; a new plan: "plan" with grow)
    move     analysis, plan, src, dst  -> like plan; shot src moved to position dst, cuts re-snapped to beats
             sticker: null | {id, mode: follow | fixed, keys: [{t, x, y, size}], in, out}
             drawn:   null | {style, box?, force?}   (a key left out = that part unchanged)
    check    plan, index, [box]  -> {ok, found, reason, label, box, image}: can shot `index` be drawn?
    cut      analysis, plan, k, t, [snap]  -> like plan; moves the cut before shot k to t (snapped to a beat)
    retime   analysis, plan  -> like plan; the same edit on the analysis' song, cuts snapped to its beats
    plan also takes carry_from: an earlier plan whose hand edits move over (e.g. after a music job)
    live     plan  -> a preview right away without the effects not rendered yet, then each missing sticker /
             drawn shot is rendered and a new preview published: job "partial" = {video, done, total,
             pending: [shot indexes], failed}. Cancel it when the plan changes.
    GET  /api/folder?path=D          {count}: video files in a folder (新建 page)
    GET  /api/songs?near=F           audio files next to F (the music library)

Clip analyses (incl. the sticker / drawn checks, per output size), song analyses and rendered sticker /
drawn shots are cached under output/app/cache, so the same footage or song is only analyzed once and a
re-render only redoes the effect shots that changed.
"""
import argparse
import json
import secrets
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .formats import ASPECTS
from .paths import OUTPUT_DIR, PROJECT_ROOT, TEMPLATE_DIR
from .pipeline import (KEEP, Options, analyze, carry_manual, edit_shot, has_manual, load_analysis, load_plan, move_cut, move_shot,
                       plan, render_plan, replace_shot, retime, save_analysis, save_plan, set_overlays, with_music, grow_all)
from .progress import ProgressEvent

APP_DIR = OUTPUT_DIR / "app"
CACHE_DIR = APP_DIR / "cache"


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind: str, params: dict):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.params = params
        self.status = "queued"          # queued | running | done | failed | cancelled
        self.stages: dict[str, dict] = {}   # stage -> latest event
        self.messages: list[str] = []
        self.result: dict | None = None
        self.partial: dict | None = None    # live preview: the latest finished state while it runs
        self.error: str | None = None
        self.created = time.time()
        self.finished: float | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def on_progress(self, e: ProgressEvent) -> None:
        if self._cancel.is_set():
            raise Cancelled()
        with self._lock:
            self.stages[e.stage] = {"step": e.step, "done": e.done, "total": e.total,
                                    "fraction": round(e.fraction, 4)}
            if e.message:
                self.messages.append(e.message)

    def cancel(self) -> None:
        self._cancel.set()

    def to_dict(self) -> dict:
        with self._lock:
            return {"id": self.id, "kind": self.kind, "status": self.status, "stages": dict(self.stages),
                    "messages": list(self.messages[-20:]), "result": self.result, "error": self.error,
                    "partial": self.partial,
                    "elapsed": round((self.finished or time.time()) - self.created, 1)}


JOBS: dict[str, Job] = {}


def _path(p: str | None) -> Path | None:
    if not p:
        return None
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _work_dir(params: dict) -> Path:
    """The given work dir, or a new output/app/<time>[_n] folder (a cached analyze takes well under a second,
    so two can start in the same second; they must not share a folder)."""
    work = _path(params.get("work"))
    if work:
        work.mkdir(parents=True, exist_ok=True)
        return work
    APP_DIR.mkdir(parents=True, exist_ok=True)
    base, n = time.strftime("%Y%m%d_%H%M%S"), 1
    while True:
        work = APP_DIR / (base if n == 1 else f"{base}_{n}")
        try:
            work.mkdir()
            return work
        except FileExistsError:
            n += 1


def _template_path(t: str | None) -> Path:
    if not t:
        return TEMPLATE_DIR / "template_odd_daily.json"
    p = TEMPLATE_DIR / f"{t}.json"
    if p.exists():
        return p
    p = _path(t)
    if p and p.exists():
        return p
    for f in TEMPLATE_DIR.glob("*.json"):        # by the template's own id
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("id") == t:
                return f
        except (OSError, ValueError):
            continue
    raise ValueError(f"unknown template {t!r}")


def _analysis_result(a, out: Path) -> dict:
    return {"analysis": str(out), "work": str(out.parent), "aspect": a.aspect,
            "clips": [{"name": c.name, "path": c.path, "duration": round(c.duration, 2),
                       "size": [c.width, c.height], "rejected": c.rejected, "reason": c.reason,
                       "tagged": any(c.tags), "drawable": bool(c.drawable)} for c in a.clips],
            "music": {"path": a.music.path, "name": Path(a.music.path).name, "tempo": round(a.music.tempo, 1),
                      "start": round(a.music.start, 2)}}


def _plan_result(pl, out: Path, notes=None) -> dict:
    return {"plan": str(out), "seed": pl.seed, "duration": round(pl.duration, 2),
            "aspect": pl.template["output"]["aspect"], "fps": float(pl.template["output"]["fps"]),
            "manual": has_manual(pl), "notes": notes or [], "overlays": pl.looks.get("overlays") or [],
            "beat": round(float(pl.music.beat_period), 4),
            "shots": [{**s.to_dict(), "clip_path": s.clip.path, "src_start": s.src_start,
                       "t_start": s.t_start, "t_end": s.t_end} for s in pl.shots]}


def _unique(path: Path) -> Path:
    """path, or path with _2, _3 ... before the suffix if it exists."""
    n, p = 2, path
    while p.exists():
        p, n = path.with_name(f"{path.stem}_{n}{path.suffix}"), n + 1
    return p


def run_analyze(job: Job) -> dict:
    p = job.params
    opts = Options(aspect=p.get("aspect"), duration=p.get("duration"), music_start=p.get("music_start"),
                   stickers=p.get("stickers", True), drawn=p.get("drawn", True))
    from .sticker_stage import VISION
    with VISION:
        a = analyze(_path(p["clips"]), _path(p["music"]), _template_path(p.get("template")), opts, job.on_progress,
                cache_dir=CACHE_DIR / "clips")
    out = _work_dir(p) / "analysis.json"
    save_analysis(a, out)
    return _analysis_result(a, out)


def run_music(job: Job) -> dict:
    p = job.params
    src = _path(p["analysis"])
    a = with_music(load_analysis(src), _path(p["music"]), p.get("music_start"), p.get("duration"), job.on_progress,
                   cache_dir=CACHE_DIR / "clips")
    out = _unique(src.parent / "analysis.json")
    save_analysis(a, out)
    return _analysis_result(a, out)


def run_plan(job: Job) -> dict:
    p = job.params
    analysis_path = _path(p["analysis"])
    seed = int(p["seed"]) if p.get("seed") is not None else secrets.randbelow(10 ** 6)
    a = load_analysis(analysis_path)
    pl = plan(a, seed, job.on_progress, p.get("stickers"), p.get("drawn_style"))
    notes = []
    if p.get("carry_from"):
        pl, notes = carry_manual(load_plan(_path(p["carry_from"])), pl, a)
    if p.get("grow"):
        pl = grow_all(pl, p["grow"])
    work = _path(p.get("work")) or analysis_path.parent
    out = _unique(work / f"plan_{seed}.json")
    save_plan(pl, out)
    return _plan_result(pl, out, notes)


def run_shot(job: Job) -> dict:
    p = job.params
    plan_path = _path(p["plan"])
    job.on_progress(ProgressEvent("shot", "edit", 0, 1))
    pl, notes = edit_shot(load_analysis(_path(p["analysis"])), load_plan(plan_path), int(p["index"]),
                          p["sticker"] if "sticker" in p else KEEP, p["drawn"] if "drawn" in p else KEEP,
                          p["grow"] if "grow" in p else KEEP)
    out = _unique(plan_path.parent / f"plan_{pl.seed}.json")
    save_plan(pl, out)
    job.on_progress(ProgressEvent("shot", "edit", 1, 1))
    return _plan_result(pl, out, notes)


def run_live(job: Job) -> dict:
    """Quick preview first, then fill in the sticker / drawn shots one by one (see the docstring at the top)."""
    from .formats import output_size
    from .render import effect_files, preview_size, render_effect
    p = job.params
    plan_path = _path(p["plan"])
    pl = load_plan(plan_path)
    cache = CACHE_DIR / "effects"
    h = preview_size(*output_size(pl.template))[1]
    stem = plan_path.stem.replace("plan", "preview")
    made: list[Path] = []

    def publish(tag: str) -> Path:
        out = _unique(plan_path.parent / f"{stem}_{tag}.mp4")
        render_plan(load_plan(plan_path), out, preview=True, effects_cache=cache, effects="cached")
        for old in made[:-1]:                 # keep the one on screen; older ones go (if the player let go)
            try:
                old.unlink()
                old.with_suffix(".timeline.json").unlink(missing_ok=True)
            except OSError:
                pass
        made.append(out)
        return out

    pending = [i for i, s in enumerate(pl.shots) if (s.drawn or s.sticker)
               and not any(f.exists() for f in effect_files(s, pl.template, cache, h))]
    job.on_progress(ProgressEvent("live", "quick", 0, 1))
    job.partial = {"video": str(publish("quick")), "done": 0, "total": len(pending), "pending": pending, "failed": []}
    job.on_progress(ProgressEvent("live", "effects", 0, len(pending)))
    failed = []
    for n, i in enumerate(pending):
        s = pl.shots[i]
        ok = render_effect(s, i, pl.template, plan_path.parent / f".{stem}_fx", cache, h)
        if not ok:
            failed.append(i)
        job.on_progress(ProgressEvent("live", "effects", n + 1, len(pending)))   # raises if cancelled
        job.partial = {"video": str(publish(f"fx{n + 1}")) if ok else job.partial["video"], "done": n + 1,
                       "total": len(pending), "pending": pending[n + 1:], "failed": failed}
    grow = [i for i, s in enumerate(pl.shots) if s.grow]
    if grow:                                  # 万物生长 transitions: one more pass that renders the missing ones
        total = len(pending) + 1
        job.partial = {**job.partial, "total": total, "pending": grow}
        job.on_progress(ProgressEvent("live", "grow", 0, 1))
        out = _unique(plan_path.parent / f"{stem}_grow.mp4")
        render_plan(load_plan(plan_path), out, preview=True, effects_cache=cache, effects="cached", grow="all")
        for old in made:
            try:
                old.unlink()
                old.with_suffix(".timeline.json").unlink(missing_ok=True)
            except OSError:
                pass
        made.append(out)
        job.on_progress(ProgressEvent("live", "grow", 1, 1))
        job.partial = {"video": str(out), "done": total, "total": total, "pending": [], "failed": failed}
    return job.partial


def run_cut(job: Job) -> dict:
    p = job.params
    plan_path = _path(p["plan"])
    pl, notes = move_cut(load_analysis(_path(p["analysis"])), load_plan(plan_path), int(p["k"]), float(p["t"]),
                         bool(p.get("snap", True)))
    out = _unique(plan_path.parent / f"plan_{pl.seed}.json")
    save_plan(pl, out)
    return _plan_result(pl, out, notes)


def run_move(job: Job) -> dict:
    p = job.params
    plan_path = _path(p["plan"])
    pl, notes = move_shot(load_analysis(_path(p["analysis"])), load_plan(plan_path), int(p["src"]), int(p["dst"]))
    out = _unique(plan_path.parent / f"plan_{pl.seed}.json")
    save_plan(pl, out)
    return _plan_result(pl, out, notes)


def run_overlays(job: Job) -> dict:
    p = job.params
    plan_path = _path(p["plan"])
    pl, notes = set_overlays(load_analysis(_path(p["analysis"])), load_plan(plan_path), p.get("overlays") or [])
    out = _unique(plan_path.parent / f"plan_{pl.seed}.json")
    save_plan(pl, out)
    return _plan_result(pl, out, notes)


def clip_thumbs(analysis_path: Path) -> dict:
    """A small picture of every clip of the project (the 素材 library), in the clip's own shape."""
    from concurrent.futures import ThreadPoolExecutor
    a = load_analysis(analysis_path)
    clips = [c for c in a.clips if not c.rejected]
    with ThreadPoolExecutor(8) as ex:
        pics = list(ex.map(lambda c: frame_at(c.path, min(1.0, c.duration * 0.3), c.width / c.height, 120), clips))
    return {"thumbs": {c.path: p for c, p in zip(clips, pics)}}


def run_grow(job: Job) -> dict:
    """全片万物生长 on (grow = settings) or off (grow = null) for every shot."""
    p = job.params
    plan_path = _path(p["plan"])
    pl = grow_all(load_plan(plan_path), p.get("grow"))
    out = _unique(plan_path.parent / f"plan_{pl.seed}.json")
    save_plan(pl, out)
    return _plan_result(pl, out)


def run_retime(job: Job) -> dict:
    p = job.params
    analysis_path = _path(p["analysis"])
    pl, notes = retime(load_analysis(analysis_path), load_plan(_path(p["plan"])))
    out = _unique(analysis_path.parent / f"plan_{pl.seed}.json")
    save_plan(pl, out)
    return _plan_result(pl, out, notes)


def run_check(job: Job) -> dict:
    from .drawn_reveal import check_shot
    from .formats import output_size
    p = job.params
    pl = load_plan(_path(p["plan"]))
    s = pl.shots[int(p["index"])]
    from .sticker_stage import VISION
    w, h = output_size(pl.template)
    job.on_progress(ProgressEvent("check", "check", 0, 1))
    with VISION:
        r = check_shot(s.clip.path, s.src_start, w / h, p.get("box"))
    if r.get("image") is not None:
        r["image"] = _data_url(r["image"])
    job.on_progress(ProgressEvent("check", "check", 1, 1))
    return r


def _data_url(img, fmt: str = "JPEG") -> str:
    import base64
    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, fmt, quality=82) if fmt == "JPEG" else img.save(buf, fmt)
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


_FRAMES: dict = {}


def frame_at(clip: str, t: float, aspect: float, height: int) -> str:
    """One frame of `clip` at `t` seconds, centre-cropped to `aspect` (w/h), as a JPEG data URL (cached)."""
    key = (clip, round(t, 3), round(aspect, 5), height)
    if key not in _FRAMES:
        import subprocess

        from PIL import Image
        crop = f"crop=w='min(iw,ih*{aspect:.6f})':h='min(ih,iw/{aspect:.6f})',scale=-2:{height}"
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", clip, "-frames:v", "1",
                            "-vf", crop, "-f", "image2pipe", "-vcodec", "png", "-"],
                           stdin=subprocess.DEVNULL, capture_output=True, check=True)
        import io
        _FRAMES[key] = _data_url(Image.open(io.BytesIO(r.stdout)))
        if len(_FRAMES) > 600:
            _FRAMES.pop(next(iter(_FRAMES)))
    return _FRAMES[key]


STRIP = (0.12, 0.37, 0.62, 0.87)   # where in a shot the timeline filmstrip frames come from


def plan_thumbs(plan_path: Path) -> dict:
    """A small picture of every shot, plus a filmstrip of 4 frames across it (the timeline shows as many as fit)."""
    from concurrent.futures import ThreadPoolExecutor

    from .formats import output_size
    pl = load_plan(plan_path)
    w, h = output_size(pl.template)
    jobs = [(s.clip.path, s.src_start + 0.1) for s in pl.shots]
    jobs += [(s.clip.path, s.src_start + f * (s.t_end - s.t_start)) for s in pl.shots for f in STRIP]
    with ThreadPoolExecutor(8) as ex:
        pics = list(ex.map(lambda j: frame_at(j[0], j[1], w / h, 96), jobs))
    n = len(pl.shots)
    return {"thumbs": pics[:n], "strips": [pics[n + 4 * i:n + 4 * i + 4] for i in range(n)]}


def plan_still(plan_path: Path, index: int, t: float) -> dict:
    from .formats import output_size
    pl = load_plan(plan_path)
    w, h = output_size(pl.template)
    s = pl.shots[index]
    t = min(max(0.0, t), max(0.0, s.t_end - s.t_start - 0.02))
    return {"image": frame_at(s.clip.path, s.src_start + t, w / h, 640), "t": t}


def track_sticker(body: dict) -> dict:
    from .formats import output_size
    from .pipeline import _check_place
    from .sticker_stage import preview_path
    pl = load_plan(_path(body["plan"]))
    s = pl.shots[int(body["index"])]
    w, h = output_size(pl.template)
    fps = float(pl.template["output"]["fps"])
    return preview_path(s.clip.path, s.src_start, s.frames / fps + 0.3, w / h, _check_place(body["place"]))


_WAVES: dict = {}


def plan_waveform(plan_path: Path) -> dict:
    """Peaks of the song under the plan (one per 20 ms of timeline) plus the beat grid, for the music track."""
    pl = load_plan(plan_path)
    dur = pl.duration
    key = (pl.music.path, round(pl.music.start, 3), round(dur, 3))
    if key not in _WAVES:
        import subprocess

        import numpy as np
        sr = 4000
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{pl.music.start:.3f}", "-t", f"{dur:.3f}", "-i", pl.music.path,
                            "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], stdin=subprocess.DEVNULL, capture_output=True,
                           check=True)
        x = np.abs(np.frombuffer(r.stdout, np.int16).astype(np.float32))
        hop = sr // 50
        n = max(1, len(x) // hop)
        peaks = x[: n * hop].reshape(n, hop).max(axis=1)
        peaks = peaks / max(1.0, float(np.percentile(peaks, 99)))
        _WAVES[key] = {"step": 0.02, "peaks": [round(float(v), 3) for v in np.clip(peaks, 0, 1)]}
    out = dict(_WAVES[key])
    grid = pl.looks.get("grid")
    if grid is None:
        try:
            from .pipeline import _cached_music
            m = _cached_music(pl.music.path, pl.music.start, pl.template, CACHE_DIR / "clips")
            grid = {"t": [round(float(v), 3) for v in m.grid if v <= dur + 0.01],
                    "strength": [round(float(v), 3) for v, g in zip(m.strength, m.grid) if g <= dur + 0.01],
                    "on_beat": [bool(v) for v, g in zip(m.on_beat, m.grid) if g <= dur + 0.01]}
        except Exception:  # noqa: BLE001 - the waveform alone is still useful
            grid = {"t": [], "strength": [], "on_beat": []}
    out["grid"] = {**grid, **_bars(grid)}
    out["tempo"] = round(pl.music.tempo, 1)
    return out


def _bars(grid: dict) -> dict:
    """Beat numbers and downbeats (重拍) for the timeline. Assumes 4 beats to the bar (pop / hip-hop): of the four
    ways to group the full beats into bars, the one whose first beats hit hardest (mean onset strength) wins.
    Half-beats (slow songs) get beat_no 0."""
    on = grid.get("on_beat") or []
    st = grid.get("strength") or []
    full = [i for i, b in enumerate(on) if b]
    if not full:
        return {"beat_no": [0] * len(on), "downbeat": [False] * len(on), "bar": [0] * len(on)}
    phase = max(range(4), key=lambda p: sum(st[i] for k, i in enumerate(full) if k % 4 == p) / max(1, len(full[p::4])))
    beat_no, down, bar = [0] * len(on), [False] * len(on), [0] * len(on)
    for k, i in enumerate(full):
        beat_no[i] = k + 1
        if k % 4 == phase:
            down[i] = True
        bar[i] = (k - phase) // 4 + 1 if k >= phase else 0      # 0 = pickup beats before the first bar
    return {"beat_no": beat_no, "downbeat": down, "bar": bar}


def run_edit(job: Job) -> dict:
    p = job.params
    plan_path = _path(p["plan"])
    job.on_progress(ProgressEvent("edit", "edit", 0, 1))
    pl = replace_shot(load_analysis(_path(p["analysis"])), load_plan(plan_path), int(p["index"]),
                      str(_path(p["clip"])) if p.get("clip") else None, secrets.randbelow(10 ** 6))
    out = _unique(plan_path.parent / f"plan_{pl.seed}.json")
    save_plan(pl, out)
    job.on_progress(ProgressEvent("edit", "edit", 1, 1))
    return _plan_result(pl, out)


def run_render(job: Job) -> dict:
    p = job.params
    plan_path = _path(p["plan"])
    preview = bool(p.get("preview"))
    name = plan_path.stem.replace("plan", "preview" if preview else "video")
    out = _path(p.get("out")) or plan_path.parent / f"{name}.mp4"
    tl = render_plan(load_plan(plan_path), out, progress=job.on_progress, preview=preview,
                     effects_cache=CACHE_DIR / "effects")
    return {"video": str(out), "duration": tl["duration"], "preview": preview,
            "timeline": str(out.with_suffix(".timeline.json"))}


RUNNERS = {"analyze": run_analyze, "music": run_music, "plan": run_plan, "edit": run_edit, "render": run_render,
           "shot": run_shot, "check": run_check, "cut": run_cut, "move": run_move, "overlays": run_overlays, "grow": run_grow, "retime": run_retime, "live": run_live}


_LOOKS: dict | None = None


def list_looks() -> dict:
    """The 「贴纸选择」 / 「手绘风格选择」 catalog with PNG previews (data URLs), drawn by the same code that
    renders them. Built once per backend (the drawn previews take ~0.5 s each)."""
    global _LOOKS
    if _LOOKS is None:
        import base64
        import io

        from .drawn_reveal import style_preview
        from .looks import DRAWN_STYLES, STICKER_GROUPS, STICKERS
        from .cartoons import NAMES as CARTOONS
        from .cartoons import preview as cartoon_preview
        from .stickers import sticker_preview

        def url(img) -> str:
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

        _LOOKS = {
            "sticker_groups": [{"id": k, "name": v} for k, v in STICKER_GROUPS.items()],
            "stickers": [{"id": s["id"], "name": s["name"], "group": s["group"], "families": list(s["families"]),
                          "preview": url(cartoon_preview(s["id"]) if s["id"] in CARTOONS
                                         else sticker_preview(s["id"]))} for s in STICKERS],
            "drawn_styles": [{**d, "preview": url(style_preview(d["id"], 160))} for d in DRAWN_STYLES],
        }
    return _LOOKS


def start_job(kind: str, params: dict) -> Job:
    if kind not in RUNNERS:
        raise ValueError(f"unknown job kind {kind!r}")
    job = Job(kind, params)
    JOBS[job.id] = job

    def work():
        job.status = "running"
        try:
            job.result = RUNNERS[kind](job)
            job.status = "done"
        except Cancelled:
            job.status = "cancelled"
        except Exception as e:  # noqa: BLE001 - reported to the UI
            job.status, job.error = "failed", str(e) or e.__class__.__name__
            traceback.print_exc(file=sys.stderr)
        job.finished = time.time()

    threading.Thread(target=work, daemon=True, name=f"job-{job.id}").start()
    return job


def list_templates() -> list[dict]:
    out = []
    for f in sorted(TEMPLATE_DIR.glob("*.json")):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": f.stem, "name": t.get("name", f.stem), "description": t.get("description", ""),
                    "aspect": t.get("output", {}).get("aspect", "3:4"),
                    "duration": t.get("output", {}).get("duration", {}).get("target")})
    return out


class Handler(BaseHTTPRequestHandler):
    token = ""
    server_version = "oddcut"

    def log_message(self, fmt, *args):   # quiet: stdout is the parent's control channel
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if secrets.compare_digest(self.headers.get("X-Oddcut-Token", ""), self.token):
            return True
        self._send(401, {"error": "missing or wrong X-Oddcut-Token"})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        if not self._authorized():
            return
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts[:1] == ["api"] and len(parts) == 2 and parts[1] in ("thumbs", "still", "waveform", "folder", "songs", "clipthumbs"):
            from urllib.parse import parse_qs, urlsplit
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                if parts[1] == "folder":
                    from .analyze import VIDEO_EXTS
                    d = _path(q["path"])
                    n = sum(1 for f in d.iterdir() if f.suffix.lower() in VIDEO_EXTS) if d and d.is_dir() else 0
                    return self._send(200, {"count": n, "name": d.name if d else ""})
                if parts[1] == "songs":
                    f = _path(q["near"])
                    exts = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
                    songs = sorted(str(x) for x in f.parent.iterdir() if x.suffix.lower() in exts) if f else []
                    return self._send(200, {"songs": songs})
                if parts[1] == "clipthumbs":
                    return self._send(200, clip_thumbs(_path(q["analysis"])))
                plan_path = _path(q["plan"])
                if parts[1] == "thumbs":
                    return self._send(200, plan_thumbs(plan_path))
                if parts[1] == "still":
                    return self._send(200, plan_still(plan_path, int(q["index"]), float(q.get("t", 0))))
                return self._send(200, plan_waveform(plan_path))
            except Exception as e:  # noqa: BLE001 - reported to the UI
                traceback.print_exc(file=sys.stderr)
                return self._send(400, {"error": str(e) or e.__class__.__name__})
        if parts == ["api", "health"]:
            return self._send(200, {"ok": True, "version": __version__, "root": str(PROJECT_ROOT)})
        if parts == ["api", "templates"]:
            return self._send(200, {"templates": list_templates()})
        if parts == ["api", "looks"]:
            try:
                return self._send(200, list_looks())
            except Exception as e:  # noqa: BLE001 - e.g. the optional vision packages are missing
                traceback.print_exc(file=sys.stderr)
                return self._send(500, {"error": str(e) or e.__class__.__name__})
        if parts == ["api", "aspects"]:
            return self._send(200, {"aspects": [{"id": k, "size": v} for k, v in ASPECTS.items()]})
        if len(parts) == 3 and parts[:2] == ["api", "jobs"] and parts[2] in JOBS:
            return self._send(200, JOBS[parts[2]].to_dict())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        parts = self.path.split("?")[0].strip("/").split("/")
        try:
            body = self._body()
        except ValueError:
            return self._send(400, {"error": "body must be JSON"})
        if parts == ["api", "jobs"]:
            try:
                job = start_job(body.pop("kind", ""), body)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(202, {"id": job.id})
        if parts == ["api", "track"]:
            try:
                return self._send(200, track_sticker(body))
            except Exception as e:  # noqa: BLE001 - reported to the UI
                traceback.print_exc(file=sys.stderr)
                return self._send(400, {"error": str(e) or e.__class__.__name__})
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel" and parts[2] in JOBS:
            JOBS[parts[2]].cancel()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


def _exit_with_parent(pid: int) -> None:
    # Not a blocking read on stdin: on Windows that holds the stdin handle and freezes every later
    # subprocess launch (ffmpeg), i.e. the whole backend.
    import os
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            os._exit(0)
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such process
            os._exit(0)
        return
    import time
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            os._exit(0)
        except PermissionError:
            pass
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="oddcut local backend for the desktop app")
    ap.add_argument("--port", type=int, default=0, help="0 = any free port")
    ap.add_argument("--token", help="required X-Oddcut-Token header (default: random, printed)")
    ap.add_argument("--parent-pid", type=int, help="exit when this process exits")
    args = ap.parse_args()
    Handler.token = args.token or secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    if args.parent_pid:
        threading.Thread(target=_exit_with_parent, args=(args.parent_pid,), daemon=True).start()
    ready = {"type": "ready", "port": httpd.server_address[1], "version": __version__}
    if not args.token:
        ready["token"] = Handler.token
    print(json.dumps(ready), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
