# Moonlight Whisper

> 基于 `yt-dlp` + `faster-whisper (CTranslate2)` 的 Windows 桌面端视频/音频转录工具。
> 粘贴视频链接或拖入本地音视频文件 → 自动下载最佳音轨 → 转码 16kHz WAV → Whisper 语音转录 → 输出带时间戳的 Markdown 文档与可选 SRT 字幕。

## 核心特性

- **开箱即用**：CPU 模式下无需任何额外配置即可运行；首次勾选 NVIDIA GPU 加速时支持在线按需下载 CUDA 12（cuBLAS 12 + cuDNN 9）运行库（支持国内清华/阿里云 PyPI 镜像、断点续传、SHA256 校验与离线导入）。
- **智能容错与回退**：自动遵循 `GPU float16 → GPU int8_float16 → CPU int8` 降级回退链，显存不足或运行库异常时平滑切换。
- **支持播放列表与批量队列**：支持批量粘贴链接、拖入本地音视频文件（`.mp3`, `.m4a`, `.mp4`, `.mkv`, `.wav` 等），可选开启播放列表/多 P 视频自动展开。
- **结构化 Markdown 导出**：智能段落合并、防幻觉相邻句去重、繁体转简体（OpenCC）、文件名自动清洗与防重名。
- **OpenAI 兼容自动摘要接口**：在设置中配置任意 OpenAI 兼容 API（如 OpenAI、DeepSeek、Qwen、本地 Ollama 等）即可自动生成 `## 摘要` 章节；未启用或失败时自动保留 `> （待填写）` 占位。

## 目录结构

```text
moonlight-whisper/
├─ app/
│  ├─ main.py                  # GUI 启动入口（含 DLL 目录预注册）
│  ├─ paths.py                 # 数据目录策略与外部二进制定位
│  ├─ config.py                # config.json 读写、校验与迁移
│  ├─ logger.py                # 7 天轮转日志 + UI 实时日志转发
│  ├─ workers.py               # QThread 封装与串行任务队列 TaskQueue
│  ├─ ui/                      # PySide6 界面层 (主窗口/GPU向导/模型管理/设置)
│  └─ core/                    # 零 Qt 依赖核心层 (downloader/audio/transcriber/summarizer/md_writer/gpu_runtime/model_manager/pipeline)
├─ cli.py                      # 命令行入口
├─ bin/                        # 放置 ffmpeg.exe 与可选 deno.exe
├─ assets/                     # 图标资源 (moon.svg / moon-512.png / app.ico)
├─ tests/                      # 自动化单元测试
├─ build/                      # PyInstaller app.spec / Inno Setup installer.iss / build.ps1
└─ requirements.txt
```

## 快速开始（开发环境）

1. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```
2. **准备 `ffmpeg.exe`**
   将 `ffmpeg.exe`（Windows 环境）放入 `bin/ffmpeg.exe`，或确保系统 `PATH` 中已安装 `ffmpeg`。
3. **运行命令行模式 (`cli.py`)**
   ```bash
   # 转录单个视频链接或本地文件（CPU 模式）
   python cli.py "https://www.bilibili.com/video/BV1xx411c7mD" --cpu --srt

   # 检测当前 GPU 与 CUDA 运行库状态
   python cli.py --detect-gpu
   ```
4. **启动图形界面**
   ```bash
   python -m app.main
   ```

## 运行单元测试

```bash
python -m unittest discover -s tests -v
# 或使用 pytest
pytest -v
```

## 打包与制作安装包 (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1 -Version 1.0.0
```

## 合规声明

本软件仅供个人学习、课堂笔记整理与研究使用，请严格遵守各视频网站的服务条款与版权法律法规。
