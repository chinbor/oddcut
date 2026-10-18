"""What a fresh agent needs to know right now, in ~40 lines: printed by the Claude Code SessionStart hook
(.claude/settings.json). Other agents: run it yourself first.  python code/tools/session_context.py"""
import re
import subprocess
import sys
from pathlib import Path

# piped (the hook) Windows Python would write GBK; agents read UTF-8
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]


def git(*args: str) -> str:
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=ROOT, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.rstrip()  # rstrip: keep the leading space of the first "git status --short" line


def section(md: str, title: str) -> str:
    m = re.search(rf"^## {title}.*?$(.*?)(?=^## |\Z)", md, re.S | re.M)
    return m.group(1).strip() if m else ""


log = (ROOT / "docs" / "开发记录.md").read_text(encoding="utf-8")
dirty = [ln for ln in git("status", "--short").splitlines() if ln.strip()]
print("== Oddcut 接手须知：规则在 AGENTS.md（已自动加载），这里是当前状态 ==")
print("最近提交：\n" + git("log", "--oneline", "-8"))
print(f"未提交的改动：{len(dirty)} 个文件" + ("".join("\n  " + d for d in dirty[:10]) if dirty else ""))
print("\n现在做到哪：\n" + section(log, "现在做到哪"))
print("\n下一步：\n" + section(log, "下一步"))
print("\n改完先跑：.venv\\Scripts\\python code\\tools\\check.py（改渲染再加 --full）")
