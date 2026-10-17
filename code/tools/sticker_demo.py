"""Single-shot sticker demo on the Mixkit clips: cats get tracked doodles, cups get googly eyes.

    .venv/Scripts/python tools/sticker_demo.py            # all jobs
    .venv/Scripts/python tools/sticker_demo.py 810 1778   # only clips whose name contains these
Writes output/stickers/<clip>_<rule>.mp4 plus *_sheet.jpg (clean), *_debug.jpg (boxes / raw detector
point / fused attach point), *_track.png (detector vs fused trajectory) and summary.json.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oddcut.stickers import run  # noqa: E402
from oddcut.paths import OUTPUT_DIR, PUBLIC_DIR  # noqa: E402

CLIPS = PUBLIC_DIR / "clips"
OUT = OUTPUT_DIR / "stickers"

JOBS = [
    dict(clip="32469-720.mp4", rule="anger", start=0.0, dur=4.0),
    dict(clip="22732-720.mp4", rule="question", start=2.5, dur=4.5),
    dict(clip="1778-720.mp4", rule="anger", start=0.0, dur=4.0),
    dict(clip="808-720.mp4", rule="eyes", start=0.0, dur=4.0),
    # top-down cup: default anchor lands in the coffee under the milk stream, so put the eyes on the
    # green outside wall below the rim (the planner / a VLM would pick this spot in production)
    dict(clip="810-720.mp4", rule="eyes", start=0.0, dur=4.0, anchor=(0.5, 0.82), size=0.8),
]


def main() -> None:
    only = sys.argv[1:]
    summary = []
    for job in JOBS:
        if only and not any(o in job["clip"] for o in only):
            continue
        job = dict(job)
        clip = job.pop("clip")
        rule = job.pop("rule")
        out = OUT / f"{clip.split('-')[0]}_{rule}.mp4"
        print(f"== {clip} {rule}", flush=True)
        stats = run(str(CLIPS / clip), rule, out, **job)
        print(json.dumps(stats, ensure_ascii=False), flush=True)
        summary.append(stats)
    path = OUT / "summary.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    keep = [s for s in old if (s["clip"], s["rule"]) not in {(n["clip"], n["rule"]) for n in summary}]
    path.write_text(json.dumps(keep + summary, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
