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
from .pipeline import (Options, analyze, load_analysis, load_plan, plan, render_plan, replace_shot, save_analysis,
                       save_plan, with_music)
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


def _plan_result(pl, out: Path) -> dict:
    return {"plan": str(out), "seed": pl.seed, "duration": round(pl.duration, 2),
            "aspect": pl.template["output"]["aspect"],
            "shots": [{**s.to_dict(), "clip_path": s.clip.path} for s in pl.shots]}


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
    pl = plan(load_analysis(analysis_path), seed, job.on_progress, p.get("stickers"), p.get("drawn_style"))
    work = _path(p.get("work")) or analysis_path.parent
    out = _unique(work / f"plan_{seed}.json")
    save_plan(pl, out)
    return _plan_result(pl, out)


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


RUNNERS = {"analyze": run_analyze, "music": run_music, "plan": run_plan, "edit": run_edit, "render": run_render}


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
        from .stickers import sticker_preview

        def url(img) -> str:
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

        _LOOKS = {
            "sticker_groups": [{"id": k, "name": v} for k, v in STICKER_GROUPS.items()],
            "stickers": [{"id": s["id"], "name": s["name"], "group": s["group"], "families": list(s["families"]),
                          "preview": url(sticker_preview(s["id"]))} for s in STICKERS],
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
