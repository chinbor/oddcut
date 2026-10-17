"""Render a template's `shooting_guide` as a one-page Markdown checklist.

Usage: python tools/render_shooting_guide.py [template.json] [out.md]
Defaults: code/templates/template_odd_daily.json -> docs/拍摄清单.md (the JSON stays the single source of truth).
"""
import json
import sys
from pathlib import Path


def rng(r) -> str:
    return f"{r[0]}～{r[1]}" if r[0] != r[1] else str(r[0])


def render(guide: dict) -> str:
    out = [f"# {guide['title']}", "", f"> {guide['summary']}", ""]

    out += ["## 一、手机设置", "", "| 项目 | 怎么做 | 为什么 |", "|---|---|---|"]
    out += [f"| {s['item']} | {s['tip']} | {s['why']} |" for s in guide["camera_settings"]]

    out += ["", f"## 二、镜头清单（共 {rng(guide['total_clips'])} 段，每段 {rng(guide['clip_seconds'])} 秒）", ""]
    for shot in guide["shot_list"]:
        share = f"，约占 {int(shot['share'] * 100)}%" if "share" in shot else ""
        out += [f"### ☐ {shot['label']}：{rng(shot['count'])} 段，每段 {rng(shot['seconds'])} 秒{share}",
                f"- **用途**：{shot['used_for']}",
                f"- **拍什么**：{'、'.join(shot['examples'])}"]
        out += [f"- {t}" for t in shot["tips"]]
        out.append("")

    out += ["## 三、拍出 odd 感的技巧", ""]
    for tech in guide["techniques"]:
        out.append(f"**{tech['name']}**")
        out += [f"- {t}" for t in tech["tips"]]
        out.append("")

    out += ["## 四、让自动剪辑剪得好", ""]
    out += [f"- {t}" for t in guide["for_auto_edit"]]

    out += ["", "## 五、常见翻车点", "", "| 问题 | 结果 |", "|---|---|"]
    out += [f"| {p['problem']} | {p['result']} |" for p in guide["pitfalls"]]
    return "\n".join(out) + "\n"


def main() -> None:
    from oddcut.paths import DEFAULT_TEMPLATE, DOCS_DIR
    tpl = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TEMPLATE
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else DOCS_DIR / "拍摄清单.md"
    guide = json.loads(tpl.read_text(encoding="utf-8"))["shooting_guide"]
    dst.write_text(render(guide), encoding="utf-8")
    print(f"wrote {dst}")


if __name__ == "__main__":
    main()
