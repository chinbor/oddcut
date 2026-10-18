# AGENTS.md：给接手的 AI agent 看

不管你是 Claude Code、Codex、Cursor 还是别的 agent，开始工作前先读完这份，再运行 `python code/tools/session_context.py`（打印最近提交、未提交改动、「现在做到哪」和「下一步」，约 40 行）。Claude Code 会在启动时自动跑它。
给人看的使用说明在 [README.md](README.md)。

## 项目是什么

Oddcut：本地运行的 odd 风格卡点剪辑器。用户自己的素材 + 一首歌 → 自动卡点成片 → 在剪映式编辑器里手动改。
完全本地，不上传素材，v1 不用云端和 AI 接口（只留扩展点）。AGPL-3.0，免费可商用。

## 结构

```
code/oddcut/        Python 引擎 + 桌面端后端
  server.py         127.0.0.1 HTTP 后端（token 头），任务 analyze/plan/render/music/edit，可轮询、可取消
  pipeline.py       Python API：analyze → plan → render_plan，plan.json 是编辑器的数据
  analyze.py music.py planner.py render.py   素材分析、节拍、剪辑方案、渲染
  pip.py            画中画合成（蒙版、关键帧、混合模式）
  cutout.py sam.py  抠像（MobileSAM 点选、钢笔圈选）
  growth.py drawn_reveal.py stickers.py cartoons.py looks.py   万物生长、手绘、贴纸（自动生成，目前暂停）
code/templates/     风格模板 JSON（目前只有 template_odd_daily.json）
code/tools/         辅助脚本（生成测试素材等）
desktop/            Electron + React 界面
  electron/main.cjs preload.cjs   主进程启动后端；页面只能通过 window.oddcut 调接口
  src/Timeline.jsx Viewer.jsx Toolbar.jsx Properties.jsx Library.jsx ...
assets/             示例素材和模型（不进仓库）
output/             所有生成结果（不进仓库）；output/app/<时间>/ 是每次生成，output/app/cache/ 是缓存
```

## 改什么看哪里（先 grep，别整文件读）

| 要改的 | 文件（行数） |
|---|---|
| 时间线：轨道、拖动、裁剪、吸附、滚轮 | `desktop/src/Timeline.jsx`（725） |
| 预览画面、画面上拖动、抠像点选 / 圈选 | `desktop/src/Viewer.jsx`（647） |
| 全局状态和接线、贴纸草稿、画中画操作、快捷键 | `desktop/src/App.jsx`（686） |
| 后端启动、跑任务轮询、实时预览 / 撤销重做 / 素材导入删除换歌 | `useJobs.js`（87）/ `useHistory.js`（19）/ `useMediaEdits.js`（57） |
| 提示条 / 节拍器 / 接口小工具和任务进度文字 | `Toasts.jsx`（27）/ `useMetronome.js`（31）/ `api.js`（24） |
| 工具栏 / 属性面板 / 画中画属性 / 素材库 | `Toolbar.jsx` `Properties.jsx` `OverlayProps.jsx` `Library.jsx` |
| 后端接口、任务 | `code/oddcut/server.py`（901，开头注释列了全部接口） |
| 分析 → 方案 → 渲染三步、换歌、换镜头素材 | `code/oddcut/pipeline.py`（263，也转导出下面各模块的名字，调用方照旧 `from .pipeline import`） |
| Options / Analysis / Plan、缓存、plan.json / analysis.json 读写 | `code/oddcut/plan_data.py`（292） |
| 手动编辑：镜头属性、切点、分割、修剪、重排、换歌对齐 | `code/oddcut/edits.py`（472） |
| 导入素材、主轨插入 / 删除、移除素材 | `code/oddcut/media_edits.py`（137） |
| 画中画字段检查 / 万物生长、手绘预设 | `code/oddcut/overlays.py`（171）/ `presets.py`（367） |
| 渲染、画中画合成 | `render.py`（335）、`pip.py`（502，开头注释是 overlay 字段说明） |
| 抠像 | `cutout.py`（288）、`sam.py`（83） |
| 万物生长 / 手绘 / 贴纸（暂停中，别动） | `growth.py` `drawn_reveal.py`（1424） `stickers.py` `cartoons.py` |

每个 .py 开头的 docstring 就是该模块的说明，读前 30 行通常就够。

## 省上下文

- **不要读** `docs/引擎技术说明.md`（32 KB 早期笔记）、`docs/screens_v05/` 图片、`output/`、`.venv/`、`desktop/node_modules/`，除非任务直接需要。
- 不要把别的 agent 的完整对话或日志贴进来。接手只需要：本文件 + `docs/开发记录.md` 的「现在做到哪」+ `git log --oneline -15`。
- 历史细节去 `git show <提交号>` 查，不要整理进文档。

## 用户否决过的（不要再做）

画笔修边、区域标签（前景 / 地面 / 中景 / 天空）、缓动曲线选项、运动模糊滑块、步骤说明和「为什么」的解释文字、框选抠像、轨道上的眼睛 / 锁按钮、音乐静音开关（要删就直接删音乐）。

## 质量门槛（每次提交前）

- `.venv\Scripts\python code\tools\check.py`：语法、换行（同一文件不能混用 LF / CRLF）、模块能否导入，几秒钟。
- 改了渲染、plan 或界面，加 `--full`：再跑老方案渲染比对（基准在 `code/tools/check_baseline.json`）和界面构建，约一分钟。
- pre-commit 钩子会自动跑快速检查。新克隆后启用一次：`git config core.hooksPath .githooks`。
- 检查没过不要提交，也不要用 `--no-verify` 绕过。老渲染变了只有用户同意才能 `--update-baseline`。

## 怎么跑

- 桌面端：双击 `desktop\启动Oddcut.bat`（构建界面后打开窗口）。开发：`cd desktop && npm run dev`。
- 命令行：`.venv\Scripts\python -m oddcut --clips <素材文件夹> --music <歌> --aspect 9:16 --out out.mp4`
- 只有 Windows 测过。Python 用项目根目录的 `.venv`。

## 必须遵守的规则

1. **一定要简化。** 不清楚交互怎么做就照抄剪映 / 抖音。不做用户没要求的功能，发现多余的就从界面删掉。
2. **效果必须可编辑。** 任何自动效果（万物生长、手绘、贴纸、转场）都要由用户能看到、能改、能亲手复现的编辑器元素组成：画中画图层 + 抠像 + 关键帧。不允许黑盒渲染。只放在合适的镜头上。
3. **现阶段先做手动编辑体验**，自动生成暂停。
4. **老渲染结果不能变：** 改渲染代码后跑 `check.py --full`，老方案渲染的帧哈希必须等于 `code/tools/check_baseline.json` 里的值（`e92bf85b…`，只算每帧哈希，和早期整份 framemd5 `86b67200…` 不是一个数）。
5. **提交：** `git commit -s`（DCO 必需）；每个文件保持原有换行（`code/oddcut` 里多数 .py 是 LF，少数是 CRLF；check.py 拒绝同一文件里混用）；**不要 push，除非用户明确说推**。
6. **依赖、模型、素材必须和 AGPL 兼容**，不能用「仅限非商用」的东西。
7. 和用户交流用中文，先说结论，简短。

## Windows 上的坑

- 后端绝不能阻塞读 stdin，会让所有 ffmpeg 子进程卡死。所有 `subprocess.run` 都传 `stdin=DEVNULL`。
- 用户电脑开着 Clash 系统代理（127.0.0.1:7897），Python urllib 会走代理，测本地接口要绕过代理。
- Electron 里 `webContents.send` 前先判断 `isDestroyed` / 正在退出。
- 不要对整个项目 `find` / `du`，`.venv` 和 `output/` 很大，按子目录看。

## 每次工作结束前（防止额度用完断档）

1. 本地提交（`git commit -s`）。
2. 在 [docs/开发记录.md](docs/开发记录.md) 的「提交记录」加一行：提交号 + 一句话做了什么；更新「现在做到哪」和「下一步」。开发记录保持在 80 行以内：「现在做到哪」只写现状不写过程，提交记录超过 20 行就把旧的合并成一行概括。
3. 用户新定的规则或否决的功能，当场写进本文件对应的小节。
4. 给用户看的截图或视频存到 `output\给你看的截图\`，告诉用户这个路径。

接手时：读本文件 → `python code/tools/session_context.py` → 从「现在做到哪」继续。

## 工作方式（省 token）

- 一次只做一件事，做完就提交，提交说明写清做了什么，方便下一个 agent 用 `git log` 了解历史。
- 先 grep 定位，再按行号读需要的那一段，不要整文件读大文件。
- 做之前在开发记录的「现在做到哪」写一行「正在做：xxx」，做完改成结果。中途断了，下一个 agent 就知道停在哪。
