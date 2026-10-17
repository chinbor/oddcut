# Oddcut 桌面端（骨架）

Electron + React 界面，剪辑全部交给本机的 oddcut 引擎，素材不出电脑。

## 启动

双击 `启动Oddcut.bat`。第一次会 `npm install`（需要联网，几分钟），之后每次先构建界面再打开窗口。

需要：Node.js 18+；`odd_analysis\.venv` 里装好 oddcut（和命令行版同一个环境）。

## 结构

```
desktop/
  electron/main.cjs     主进程：启动后端 python -m oddcut.server，转发界面的接口调用，media:// 播放视频
  electron/preload.cjs  给页面的固定接口（window.oddcut），页面拿不到 Node 和后端令牌
  src/App.jsx           界面：素材 → 风格 → 画幅和时长 → 方案 → 渲染 → 预览和导出
  src/styles.css
```

后端接口见 `code/oddcut/server.py` 开头的说明：`analyze` / `plan` / `render` 三种任务，界面每 0.4 秒查一次进度，可以取消。
中间文件和成片在 `output/app/<时间>/`，导出时另存一份到你选的位置。

环境变量：`ODDCUT_ROOT` 指定项目根目录（默认是 desktop 的上一级），`ODDCUT_PYTHON` 指定 Python。
`npm run dev` 会同时打开开发者工具。
