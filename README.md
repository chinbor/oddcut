# Oddcut

**English summary:** Oddcut is a local, beat-synced auto-editing tool. Give it a folder of clips and a song, and it cuts an odd-style vertical video (9:16 or other aspect ratios) on the music's beats. Everything runs on your own computer: no cloud, no upload. Status: early v0.1 skeleton. License: AGPL-3.0.

---

Oddcut：本地运行的卡点自动剪辑工具。选一个素材文件夹和一首音乐，自动剪成 odd 风格的竖屏（9:16，也支持 3:4、4:5、1:1、16:9）卡点短视频。全程在你自己的电脑上运行，不用云端，素材不会上传。

**状态：** 早期 v0.1 骨架，功能和接口还会变。

## 环境要求

- Windows（目前只在 Windows 上测过）
- Python 3.11+，在项目根目录建 `.venv`，并安装 `code/pyproject.toml` 里的依赖：
  `python -m venv .venv`，然后 `.venv\Scripts\python -m pip install -e "code[vision]"`
  （不需要贴纸和手绘开场的话，装 `-e code` 即可）
- ffmpeg 在 PATH 上
- Node.js 18+（桌面端用）

## 运行

- **桌面端：** 双击 `desktop\启动Oddcut.bat`。第一次会自动 `npm install`。
- **命令行，一次跑完：**
  `.venv\Scripts\python -m oddcut --clips <素材文件夹> --music <音乐文件> --aspect 9:16 --out out.mp4`
- **命令行，分步：**
  ```
  python -m oddcut analyze --clips <dir> --music <file> --aspect 9:16 --out work/analysis.json
  python -m oddcut plan    --analysis work/analysis.json --seed 7 --out work/plan.json
  python -m oddcut render  --plan work/plan.json --out out.mp4
  ```

## 目录

```
code/oddcut/      剪辑引擎（素材分析、音乐节拍、剪辑方案、渲染、贴纸、手绘开场、桌面端后端 server.py）
code/templates/   风格模板（JSON）
code/tools/       辅助脚本
desktop/          桌面端（Electron + React）
docs/             风格拆解、拍摄清单
```

## 示例素材不在仓库里

示例视频和音乐（`assets/public/clips`、`assets/public/music`）、合成测试素材（`assets/test`）、参考视频抽帧（`assets/reference`）和模型权重（`assets/models`）都没有放进仓库。
示例素材的来源见 `assets/public/SOURCES.md`，需要自己下载。合成测试素材可以用 `code/tools/make_test_assets.py` 生成。

**模型（`assets/models/`）：** 只有贴纸和手绘开场需要，基础剪辑不需要。不用手动下载：装好 `code[vision]` 依赖后，第一次分析素材时，Ultralytics 会从它的 GitHub Releases 自动把模型下载到 `assets/models/`（需要联网，共约 650 MB）：
- `yolo11s-seg.pt`（约 20 MB）：识别杯子、猫狗等物体
- `yoloe-11s-seg.pt`（约 28 MB）：定位动物的头、眼睛、鼻子
- `mobileclip_blt.ts`（约 600 MB）：YOLOE 的文字编码器，用来生成 `yoloe_pe_*.pt` 文字向量缓存（同样自动生成，存在 `assets/models/`）

如果 Ultralytics 的全局权重目录里已经有同名文件，会直接用那份，不重新下载。

## 许可证

本项目以 [GNU AGPL-3.0](LICENSE) 发布：可以免费使用，包括商用；如果把修改后的版本提供给别人使用（包括通过网络提供服务），必须以 AGPL 公开修改后的源代码。
贴纸和手绘开场功能依赖的 [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) 同样是 AGPL-3.0。

参与贡献见 [CONTRIBUTING.md](CONTRIBUTING.md)。

更详细的引擎技术说明（分析、节拍、时间轴、渲染、贴纸和手绘 demo）见 [docs/引擎技术说明.md](docs/引擎技术说明.md)。
