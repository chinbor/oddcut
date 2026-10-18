"""One-command self check. Run it before every commit (the pre-commit hook runs the quick part).

    python code/tools/check.py                    quick: compile, CRLF, imports (a few seconds)
    python code/tools/check.py --full             + legacy render check + desktop UI build (about a minute)
    python code/tools/check.py --update-baseline  re-record the legacy render hash (only when the user agreed
                                                  that old renders may change)

Use the project interpreter: .venv\\Scripts\\python code\\tools\\check.py
Exit code 0 = all good. Stdlib only, so it also runs from the git hook.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "code" / "oddcut"
BASELINE = Path(__file__).with_name("check_baseline.json")
# A fixed old-style edit: synthetic clips + 120 BPM drums, seed 7, default template (3:4), no vision models.
LEGACY = {"clips": "assets/test/clips", "music": "assets/test/music/drums_120bpm.wav", "seed": "7"}

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def check_compile() -> None:
    bad = 0
    for f in sorted(PKG.glob("*.py")) + sorted((ROOT / "code" / "tools").glob("*.py")):
        try:  # compile in memory: py_compile refuses os.devnull ("nul") as cfile on Windows
            compile(f.read_bytes(), str(f), "exec", dont_inherit=True)
        except SyntaxError as e:
            fail(f"syntax: {f.relative_to(ROOT)}:{e.lineno}: {e.msg}")
            bad += 1
    if not bad:
        ok("all .py compile")


def check_crlf() -> None:
    """code/oddcut/*.py are CRLF; a mixed file means an editor wrote LF lines into it."""
    mixed = []
    for f in sorted(PKG.glob("*.py")):
        b = f.read_bytes()
        crlf = b.count(b"\r\n")
        lf = b.count(b"\n") - crlf
        if crlf and lf:
            mixed.append(f"{f.name} ({lf} LF lines)")
    if mixed:
        fail("mixed line endings, convert to CRLF: " + ", ".join(mixed))
    else:
        ok("line endings consistent")


def check_imports() -> None:
    code = "import oddcut, oddcut.pipeline, oddcut.server, oddcut.render, oddcut.pip; print(oddcut.__version__)"
    r = run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(ROOT / "code")})
    if r.returncode:
        fail("import oddcut modules: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    else:
        ok(f"imports (oddcut {r.stdout.strip()})")


def legacy_hash() -> str | None:
    if not all((ROOT / LEGACY[k]).exists() for k in ("clips", "music")):
        fail("legacy render: test assets missing, run code/tools/make_test_assets.py first")
        return None
    if not shutil.which("ffmpeg"):
        fail("legacy render: ffmpeg not on PATH")
        return None
    with tempfile.TemporaryDirectory(prefix="oddcut_check_") as tmp:
        out = Path(tmp) / "legacy.mp4"
        r = run([sys.executable, "-m", "oddcut", "--clips", LEGACY["clips"], "--music", LEGACY["music"],
                 "--seed", LEGACY["seed"], "--no-stickers", "--no-drawn", "--out", str(out)],
                env={**os.environ, "PYTHONPATH": str(ROOT / "code")})
        if r.returncode or not out.exists():
            fail("legacy render failed: " + (r.stderr.strip().splitlines() or ["?"])[-1])
            return None
        md5 = Path(tmp) / "frames.md5"
        r = run(["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:v", "-f", "framemd5", str(md5)])
        if r.returncode:
            fail("framemd5 failed: " + r.stderr.strip())
            return None
        # keep only the hash column: container timestamps are not what we care about
        lines = [ln.rsplit(",", 1)[-1].strip() for ln in md5.read_text().splitlines() if ln and ln[0] != "#"]
        return hashlib.md5("\n".join(lines).encode()).hexdigest()


def check_legacy(update: bool) -> None:
    h = legacy_hash()
    if h is None:
        return
    if update or not BASELINE.exists():
        BASELINE.write_text(json.dumps({"legacy_frames_md5": h, **LEGACY}, indent=2) + "\n", encoding="utf-8",
                            newline="\n")  # LF: core.safecrlf refuses a CRLF .json
        ok(f"legacy render baseline recorded: {h} (commit {BASELINE.name})")
        return
    want = json.loads(BASELINE.read_text(encoding="utf-8"))["legacy_frames_md5"]
    if h == want:
        ok("legacy render unchanged")
    else:
        fail(f"legacy render CHANGED ({h} != {want}). Old plans must render the same; "
             "only if the user agreed, run --update-baseline")


def check_ui() -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm or not (ROOT / "desktop" / "node_modules").exists():
        fail("desktop build: npm or desktop/node_modules missing (run desktop\\启动Oddcut.bat once)")
        return
    r = subprocess.run([npm, "run", "build"], cwd=ROOT / "desktop", stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        fail("desktop build: " + "\n".join((r.stdout + r.stderr).strip().splitlines()[-5:]))
    else:
        ok("desktop UI builds")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="also legacy render + desktop build")
    ap.add_argument("--update-baseline", action="store_true", help="re-record the legacy render hash")
    a = ap.parse_args()
    print("oddcut check")
    check_compile()
    check_crlf()
    check_imports()
    if a.full or a.update_baseline:
        check_legacy(a.update_baseline)
    if a.full:
        check_ui()
    print("ALL GOOD" if not failures else f"{len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
