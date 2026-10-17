"""Drawn-reveal demo on the Mixkit clips: render the effect on a frontal cat and a cup, check that
is_drawable rejects unsuitable shots, and scan every clip for drawable start points.

    .venv/Scripts/python tools/drawn_demo.py            # render + checks + scan
    .venv/Scripts/python tools/drawn_demo.py render     # only the renders
    .venv/Scripts/python tools/drawn_demo.py scan       # only the is_drawable scan
Writes output/drawn/<clip>_<name>.mp4 with *_sheet.jpg (phases), *_debug.jpg (fit check) and *.json,
plus checks.json and scan.json.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oddcut.drawn_reveal import Timing, find_subject, is_drawable, load_shot, run  # noqa: E402
from oddcut.ffmpeg_utils import probe  # noqa: E402
from oddcut.paths import OUTPUT_DIR, PUBLIC_DIR  # noqa: E402

CLIPS = PUBLIC_DIR / "clips"
OUT = OUTPUT_DIR / "drawn"

RENDERS = [
    dict(clip="22732-720.mp4", name="cat", start=0.25),
    dict(clip="808-720.mp4", name="cup", start=4.25),
    # the finished cartoon waits for a beat 1.5s in (hold), then the footage shows
    dict(clip="22732-720.mp4", name="cat_beat", start=0.25, timing=Timing(reveal_at=1.5)),
    # rejected shot rendered anyway, to show why it is rejected
    dict(clip="32469-720.mp4", name="forced", start=1.0, force=True),
    # low-confidence "cup" (a glass pour-over dripper): mask runs into the stand
    dict(clip="816-720.mp4", name="forced", start=13.75, force=True),
]

CHECKS = [
    dict(clip="22732-720.mp4", start=0.25, expect=True, note="frontal cat in grass"),
    dict(clip="808-720.mp4", start=4.25, expect=True, note="cup, side view"),
    dict(clip="32469-720.mp4", start=1.0, expect=False, note="cat looking down at the stone"),
    dict(clip="32469-720.mp4", start=8.0, expect=False, note="same cat later"),
    dict(clip="1778-720.mp4", start=1.0, expect=False, note="kitten close-up, chin scratched"),
    dict(clip="4451-720.mp4", start=2.0, expect=False, note="night street, no subject"),
    dict(clip="810-720.mp4", start=1.0, expect=False, note="cup from above, milk pouring"),
    dict(clip="816-720.mp4", start=13.75, expect=False, note="glass dripper seen as a 0.41 'cup'"),
]


def check(clip: str, start: float) -> tuple[bool, str, dict]:
    native, _, fps, _ = load_shot(str(CLIPS / clip), start, 0.4, None)
    s, why = find_subject(native[0])
    return is_drawable(native, fps, s) if s is not None else (False, why, {})


def do_renders() -> list:
    out = []
    for job in RENDERS:
        stats = run(str(CLIPS / job["clip"]), job["start"], OUT / f"{job['clip'].split('-')[0]}_{job['name']}.mp4",
                    job.get("timing"), check=not job.get("force", False))
        print(f"{job['clip']} {job['name']}: {stats['reason']} -> {stats.get('duration', '-')}s")
        out.append(stats)
    return out


def do_checks() -> list:
    rows = []
    for c in CHECKS:
        ok, reason, info = check(c["clip"], c["start"])
        rows.append({**c, "ok": ok, "pass": ok == c["expect"], "reason": reason, **info})
        print(f"{'OK ' if ok == c['expect'] else 'BAD'} {c['clip']}@{c['start']}: {ok} - {reason}")
    (OUT / "checks.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return rows


def do_scan(step: float = 1.5) -> list:
    rows = []
    for path in sorted(CLIPS.glob("*.mp4")):
        dur = probe(str(path)).duration
        t = 0.25
        while t < dur - 1.0:
            ok, reason, info = check(path.name, t)
            rows.append({"clip": path.name, "start": t, "ok": ok, "reason": reason, **info})
            print(f"  {path.name}@{t:.2f}: {'YES' if ok else 'no '} {reason}")
            t += step
    (OUT / "scan.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    what = sys.argv[1:] or ["render", "check", "scan"]
    if "render" in what:
        do_renders()
    if "check" in what:
        do_checks()
    if "scan" in what:
        do_scan()


if __name__ == "__main__":
    main()
