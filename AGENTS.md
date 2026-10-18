# AGENTS.md：给接手的 AI agent 看

不管你是 Claude Code、Codex、Cursor 还是别的 agent，开始工作前先读完这份，再读 [docs/开发记录.md](docs/开发记录.md) 的「现在做到哪」。
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

## 怎么跑

- 桌面端：双击 `desktop\启动Oddcut.bat`（构建界面后打开窗口）。开发：`cd desktop && npm run dev`。
- 命令行：`.venv\Scripts\python -m oddcut --clips <素材文件夹> --music <歌> --aspect 9:16 --out out.mp4`
- 只有 Windows 测过。Python 用项目根目录的 `.venv`。

## 必须遵守的规则

1. **一定要简化。** 不清楚交互怎么做就照抄剪映 / 抖音。不做用户没要求的功能，发现多余的就从界面删掉。
2. **效果必须可编辑。** 任何自动效果（万物生长、手绘、贴纸、转场）都要由用户能看到、能改、能亲手复现的编辑器元素组成：画中画图层 + 抠像 + 关键帧。不允许黑盒渲染。只放在合适的镜头上。
3. **现阶段先做手动编辑体验**，自动生成暂停。
4. **老渲染结果不能变：** 老方案的 CLI 渲染 framemd5 必须保持 `86b672000989ebefc4803b2cec152d39`，改渲染代码后要验证。
5. **提交：** `git commit -s`（DCO 必需）；`code/oddcut` 里的 .py 是 CRLF，保持不变；**不要 push，除非用户明确说推**。
6. **依赖、模型、素材必须和 AGPL 兼容**，不能用「仅限非商用」的东西。
7. 和用户交流用中文，先说结论，简短。

## Windows 上的坑

- 后端绝不能阻塞读 stdin，会让所有 ffmpeg 子进程卡死。所有 `subprocess.run` 都传 `stdin=DEVNULL`。
- 用户电脑开着 Clash 系统代理（127.0.0.1:7897），Python urllib 会走代理，测本地接口要绕过代理。
- Electron 里 `webContents.send` 前先判断 `isDestroyed` / 正在退出。
- 不要对整个项目 `find` / `du`，`.venv` 和 `output/` 很大，按子目录看。

## 每次工作结束前（防止额度用完断档）

1. 本地提交（`git commit -s`）。
2. 在 [docs/开发记录.md](docs/开发记录.md) 的「提交记录」加一行：提交号 + 一句话做了什么；更新「现在做到哪」和「下一步」。
3. 给用户看的截图或视频存到 `output\给你看的截图\`，告诉用户这个路径。

接手时：读本文件 → 读开发记录 → `git status` 和 `git log --oneline -15` → 从「现在做到哪」继续。
