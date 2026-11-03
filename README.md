# Moonlight Whisper

> 基于 `yt-dlp` + `网站账号托管与自动续期引擎` + `faster-whisper (CTranslate2)` 的 Windows 桌面端视频/音频转录工具。
> 粘贴视频链接或拖入本地音视频文件 → 自动挂载托管站点 Cookie 并下载最佳音轨 → 转码 16kHz WAV → Whisper 语音转录 → 输出带时间戳的 Markdown 文档与可选 SRT 字幕。

## 核心特性

- **开箱即用**：CPU 模式下无需任何额外配置即可运行；首次勾选 NVIDIA GPU 加速时支持在线按需下载 CUDA 12（cuBLAS 12 + cuDNN 9）运行库（支持国内清华/阿里云 PyPI 镜像、断点续传、SHA256 校验与离线导入）。
- **网站账号托管（Account Vault）与专用登录小窗**（v1.4.0）：点击「打开专用登录小窗」即在应用内弹出一个独立的小窗口（QtWebEngine）登录各大视频网站（YouTube、哔哩哔哩、抖音、TikTok、X/Twitter、Instagram、小红书，并支持 `plugins/sites.json` 扩展自定义站点）。小窗与日常浏览器的窗口/标签页互不干扰，也**不是浏览器配置目录**，不会触发 Edge/Chrome 账号同步；登录凭据由小窗直接回传（无需解密浏览器 Cookie 数据库），点击「我已登录」后写入 `cookies/<站点>.txt`。下载时按域名自动挂载对应站点凭据并原子回写轮换令牌。小窗不可用或站点拒绝内嵌登录时，可在浏览器登录后复制 Cookie，用「从剪贴板导入并托管」。
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
│  ├─ ui/                      # PySide6 界面层 (主窗口/GPU向导/模型管理/设置/网站账号托管中心/专用登录小窗 login_window.py)
│  └─ core/                    # 零 Qt 依赖核心层 (downloader/browser_cookies/site_cookies/webview_login/audio/transcriber/summarizer/md_writer/gpu_runtime/model_manager/pipeline)
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

   # 查看已托管网站账号与凭据状态
   python cli.py --hosted-sites

   # 后台无头静默刷新指定托管站点的 Cookie
   python cli.py --refresh-cookies youtube

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
powershell -ExecutionPolicy Bypass -File build\build.ps1 -Version 1.3.7
```

## 合规声明

本软件仅供个人学习、课堂笔记整理与研究使用，请严格遵守各视频网站的服务条款与版权法律法规。

## 更新记录

### v1.4.0
- 账号托管中心新增**专用登录小窗**：登录时弹出独立小窗，不再在现有浏览器里新增标签页（现有页面不受干扰、无需再从浏览器数据库里读取 Cookie）；小窗不使用浏览器配置目录，不会触发 Microsoft Edge 同步导致弹出大量窗口和标签页。
- 小窗继承设置中的代理；支持 `window.open` 弹窗式登录（如「使用 Google 登录」）；登录状态持久保存在 `account_vault/login_window/`，「清除当前站点凭据」会同时清除小窗中该站点的登录状态。
- 导出凭据先校验后替换：未完成的登录不会覆盖已有的有效托管凭据。
- 依赖新增 `PySide6-Addons`（提供 QtWebEngine）；打包配置不再排除 QtWebEngine，安装包体积会明显增大。

### v1.4.1
- 修复登录小窗提示缺少 `PySide6-Addons` 的问题：`requirements.txt` 改用 `PySide6` 元包，保证 Essentials 与 Addons 版本严格一致；加载失败时提示与当前 PySide6 版本匹配的安装命令及具体错误。
- 删除无法使用的「从现有浏览器提取并托管」功能及浏览器选择框；保留登录小窗与剪贴板导入。
