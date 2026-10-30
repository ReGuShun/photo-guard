# 一键隐身 · 照片主动防护平台

**面向姿态驱动人物视频生成的照片主动防护平台**。

集成 Dormant 图片保护、YOLO 人物及人脸定位、DWPose 姿态提取和 MusePose 视频生成，提供实时日志、独立结果展示与对照流程。勾选图片保护后，保护失败即停止任务。

## 1. 无 GPU 体验可点击原型

双击 `prototype/index.html`；或安装 Python 3 后运行：

```bash
python scripts/serve_prototype.py --port 6007
```

浏览器打开 `http://localhost:6007`。Windows 可使用 `py` 替代 `python`。

GitHub 版本使用程序绘制的人物示意图与静态示意视频，不包含用户照片和历史人物视频。原型中的处理、参数与日志均为模拟，不能用于判断防护效果。它支持图片上传预览、保护演示、保护失败停止、结果切换和记录导出。

## 2. 配置真实推理系统

建议 Linux + NVIDIA GPU，安装 Git、Conda、FFmpeg（含 ffprobe）。CPU 不作为本项目完整推理的已验证运行环境。

```bash
python scripts/prepare_upstreams.py
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements/frontend.txt
cp .env.example .env
```

在独立的推理环境中，参照 `Dormant/environment.yaml` 与 `MusePose/requirements.txt` 配置依赖。`requirements/*-observed.txt` 是服务器当时安装包的版本记录，不是已验证的跨机器安装锁文件。PyTorch / CUDA 请按显卡环境选择，勿在同一环境直接混装两份上游依赖清单。

编辑 `.env`，指定 `MUSEPOSE_PYTHON`、`DORMANT_PYTHON` 为推理环境 Python 的绝对路径。按 [权重说明](docs/weights.md) 放置模型，然后运行：

```bash
bash scripts/start.sh
```

访问 `http://localhost:6006`。在远程服务器启动时，需通过平台端口映射或 SSH 转发访问；浏览器的 localhost 指浏览器所在电脑。默认端口被占用时会报错，不自动切换端口。

## 3. 仓库结构

| 路径 | 内容 |
|---|---|
| `app.py` | Gradio 界面与任务编排 |
| `prototype/` | 可离线打开的交互原型 |
| `patches/` | 相对固定上游版本的项目修改 |
| `upstream-lock.json` | 上游地址与提交号 |
| `scripts/` | 初始化及启动脚本 |
| `docs/` | 权重说明、验证边界与流程图 |

模型权重、输入图片、输出视频、缓存、私密配置和比赛问卷不进入仓库。原服务器项目保持不变，此目录是单独整理的发布副本。

## 4. 当前验证范围

本次整理检查代码语法、上游补丁可应用性及本地资源完整性；未完成新机器端到端 GPU 推理验证。模型配置完整、权重存在不等同于已验证防护有效；本项目不宣称具有已量化验证的抗压缩性能。

## 5. 上游与使用条款

- [MusePose](https://github.com/TMElyralab/MusePose)
- [Dormant](https://github.com/Manu21JC/Dormant)
- [DWPose](https://github.com/IDEA-Research/DWPose)
- [Ultralytics YOLO](https://github.com/ultralytics/ultralytics)

项目集成与修改不改变上游代码和模型的许可。`patches/` 中包含上游派生修改，使用、再分发或商业部署前请核对各上游许可；本仓库暂不统一授予开源许可证。FaceFusion 换脸/修复功能已从发布界面和任务编排中移除，人脸定位仅使用单独的 YOLOFace 模型文件。
