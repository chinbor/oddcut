"""CLI.

One go (as before):
    python -m oddcut --clips <dir> --music <file> [--template t.json] [--aspect 9:16] [--out out.mp4]

Step by step (what the desktop app calls):
    python -m oddcut analyze --clips <dir> --music <file> [--aspect 9:16] --out work/analysis.json
    python -m oddcut plan    --analysis work/analysis.json [--seed 7] --out work/plan.json
    python -m oddcut render  --plan work/plan.json --out output/edits/x.mp4

--progress-json prints one JSON object per line ({"type": "progress", ...}, then {"type": "result", ...})
instead of the human-readable log.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from .formats import ASPECTS
from .paths import DEFAULT_TEMPLATE, EDITS_DIR
from .pipeline import (Analysis, Options, Plan, analyze, load_analysis, load_plan, plan, render_plan,
                       save_analysis, save_plan)
from .progress import console, json_lines

STEPS = ("analyze", "plan", "render")


def _add_analyze_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--clips", required=True, help="folder of source video clips")
    ap.add_argument("--music", required=True, help="music file (mp3/wav/m4a...)")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    ap.add_argument("--aspect", help=f"output aspect, overrides the template ({', '.join(ASPECTS)} or any W:H)")
    ap.add_argument("--music-start", type=float, default=None,
                    help="seconds into the song where the edit starts (default: auto-pick the loudest section)")
    ap.add_argument("--duration", type=float, help="override target duration (seconds)")
    ap.add_argument("--no-stickers", action="store_true", help="skip the cartoon eyes / doodle layer")
    ap.add_argument("--no-drawn", action="store_true", help="skip the hand-drawn reveal opening")


def _options(args) -> Options:
    return Options(aspect=args.aspect, duration=args.duration, music_start=args.music_start,
                   stickers=not args.no_stickers, drawn=not args.no_drawn)


def _print_analysis(a: Analysis, t0: float) -> None:
    print(f"[1/4] analyzed {len(a.clips)} clips for {a.aspect} ({time.time() - t0:.1f}s)")
    for c in a.clips:
        flag = f"REJECTED: {c.reason}" if c.rejected else f"score {c.frame_score.mean():.2f}"
        print(f"      {c.name:<28} {c.duration:5.1f}s {c.width}x{c.height}  {flag}")
    m = a.music
    print(f"[2/4] music {m.tempo:.1f} BPM, beat {m.beat_period:.3f}s, starts at {m.start:.2f}s, "
          f"{len(m.grid)} cut points, {m.duration:.1f}s usable")
    if a.stickers_tagged:
        print(f"      sticker tags: {sum(1 for c in a.clips if any(c.tags))} clips with cats/dogs/cups...")
    found = [(c.name, d[0], d[1]) for c in a.clips for d in c.drawable]
    if found:
        listed = ", ".join(f"{n}@{t:.2f} {lab}" for n, t, lab in found[:6])
        print(f"      drawn-reveal scan: {len(found)} drawable starts {listed}")


def _print_plan(p: Plan) -> None:
    print(f"[3/4] planned {len(p.shots)} shots ({p.duration:.2f}s, seed {p.seed})")
    for s in p.shots:
        tag = f"  [{s.sticker}]" if s.sticker else ""
        if s.drawn:
            tag = f"  [drawn {s.drawn['label']}, reveal @{s.drawn['reveal_at']:.2f}s]"
        print(f"      {s.t_start:6.2f}-{s.t_end:6.2f}  {s.role:<7} {s.clip.name:<28} @ {s.src_start:5.2f}s{tag}")


def _result(json_mode: bool, **kw) -> None:
    if json_mode:
        print(json.dumps({"type": "result", **kw}, ensure_ascii=False), flush=True)


def main_steps(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="python -m oddcut", description="oddcut, one step at a time")
    sub = ap.add_subparsers(dest="step", required=True)
    a = sub.add_parser("analyze", help="score clips, find the beat grid, tag stickers -> analysis.json")
    _add_analyze_args(a)
    a.add_argument("--out", required=True, help="analysis JSON to write")
    p = sub.add_parser("plan", help="analysis.json -> plan.json (the timeline)")
    p.add_argument("--analysis", required=True)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", required=True, help="plan JSON to write")
    r = sub.add_parser("render", help="plan.json -> video (+ .timeline.json)")
    r.add_argument("--plan", required=True)
    r.add_argument("--out", default=str(EDITS_DIR / "odd_cut.mp4"))
    r.add_argument("--keep-temp", action="store_true")
    for s in (a, p, r):
        s.add_argument("--progress-json", action="store_true", help="machine-readable progress on stdout")
    args = ap.parse_args(argv)
    cb = json_lines if args.progress_json else console
    t0 = time.time()
    try:
        if args.step == "analyze":
            res = analyze(args.clips, args.music, args.template, _options(args), cb)
            save_analysis(res, args.out)
            if not args.progress_json:
                _print_analysis(res, t0)
                print(f"      -> {args.out}")
            _result(args.progress_json, step="analyze", out=str(args.out), aspect=res.aspect,
                    clips=len(res.clips), rejected=sum(c.rejected for c in res.clips),
                    tempo=round(res.music.tempo, 2))
        elif args.step == "plan":
            res = plan(load_analysis(args.analysis), args.seed, cb)
            save_plan(res, args.out)
            if not args.progress_json:
                _print_plan(res)
                print(f"      -> {args.out}")
            _result(args.progress_json, step="plan", out=str(args.out), shots=len(res.shots),
                    duration=round(res.duration, 3))
        else:
            tl = render_plan(load_plan(args.plan), args.out, args.keep_temp, cb)
            if not args.progress_json:
                print(f"[4/4] rendered {args.out} ({tl['duration']:.2f}s) in {time.time() - t0:.1f}s")
            _result(args.progress_json, step="render", out=str(args.out), duration=tl["duration"])
    except Exception as e:
        if not args.progress_json:
            raise
        print(json.dumps({"type": "error", "step": args.step, "message": str(e)}, ensure_ascii=False), flush=True)
        sys.exit(1)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in STEPS:
        return main_steps(sys.argv[1:])
    ap = argparse.ArgumentParser(description="Template-driven auto edit (beat-synced cuts, loop bookends, film grade).")
    _add_analyze_args(ap)
    ap.add_argument("--out", default=str(EDITS_DIR / "odd_cut.mp4"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--keep-temp", action="store_true")
    ap.add_argument("--save-steps", action="store_true",
                    help="also write <out>.analysis.json and <out>.plan.json (re-plan / re-render without re-analyzing)")
    ap.add_argument("--progress-json", action="store_true", help="machine-readable progress on stdout")
    args = ap.parse_args()
    cb = json_lines if args.progress_json else console
    t0 = time.time()

    a = analyze(args.clips, args.music, args.template, _options(args), cb)
    p = plan(a, args.seed, cb)
    out = Path(args.out)
    if args.save_steps:
        save_analysis(a, out.with_suffix(".analysis.json"))
        save_plan(p, out.with_suffix(".plan.json"))
    if not args.progress_json:
        _print_analysis(a, t0)
        _print_plan(p)
    tl = render_plan(p, out, args.keep_temp, cb)
    if not args.progress_json:
        print(f"[4/4] rendered {out} ({tl['duration']:.2f}s) in {time.time() - t0:.1f}s")
    _result(args.progress_json, step="all", out=str(out), duration=tl["duration"])


if __name__ == "__main__":
    main()
