# Moonlight Whisper

> 基于 `yt-dlp` + `网站账号托管与自动续期引擎` + `faster-whisper (CTranslate2)` 的 Windows 桌面端视频/音频转录工具。
> 粘贴视频链接或拖入本地音视频文件 → 自动挂载托管站点 Cookie 并下载最佳音轨 → 转码 16kHz WAV → Whisper 语音转录 → 输出带时间戳的 Markdown 文档与可选 SRT 字幕。

## 核心特性

- **开箱即用**：CPU 模式下无需任何额外配置即可运行；首次勾选 NVIDIA GPU 加速时支持在线按需下载 CUDA 12（cuBLAS 12 + cuDNN 9）运行库（支持国内清华/阿里云 PyPI 镜像、断点续传、SHA256 校验与离线导入）。
- **网站账号托管（Account Vault）与安全登录小窗**：点击「打开安全登录小窗」，调用电脑上真实的 Edge / Chrome 以单窗口小窗（`--app` 模式，无标签页）打开登录页，Google 等站点认可这种真实浏览器环境，可正常登录 YouTube/Google（YouTube、哔哩哔哩、抖音、TikTok、X/Twitter、Instagram、小红书，并支持 `plugins/sites.json` 扩展自定义站点）。小窗使用应用自己的独立托管目录（`account_vault/browser_profile`），与日常浏览器的配置和账号隔离，并关闭账号同步、跳过首次运行向导；登录凭据通过本地调试端口明文读取，点击「我已登录，提取并托管凭据」后写入 `cookies/<站点>.txt`，同时顺带托管小窗里已登录的其他站点。下载时按域名自动挂载凭据并原子回写轮换令牌，凭据过期时可在后台无头静默刷新。另提供应用内置小窗（QtWebEngine，适合 B站/抖音等，Google 会拦截）与「从剪贴板导入」。
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
│  └─ core/                    # 零 Qt 依赖核心层 (downloader/browser_cookies/browser_login/site_cookies/webview_login/audio/transcriber/summarizer/md_writer/gpu_runtime/model_manager/pipeline)
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

### v1.4.2
- 修复内置小窗登录 Google 时提示「浏览器环境不安全」：新增**系统浏览器安全登录小窗**，调用本机真实的 Edge / Chrome，以单窗口 `--app` 小窗打开登录页，Google 可正常登录。
- 小窗使用独立托管目录并带 `--disable-sync`、`--no-first-run`，不会读取或改动日常浏览器的配置，也避免 Edge 同步弹窗与多余标签页；登录凭据经本地调试端口直接读取，无需解密浏览器数据库。
- 登录期间自动检测登录状态（已登录且页面回到目标站点时提示）；提取时仅在目标站点凭据有效才写入并关闭小窗，否则保留小窗继续登录；若小窗已被手动关闭，则改用后台无头模式从托管目录读取。
- 登录一次后，下载时的后台无头静默刷新重新可用。
- 内置 QtWebEngine 小窗保留为可选项（适合 B站/抖音等）。

### v1.4.3
- 修复「登录成功后点击提取却提示找不到凭据，且小窗被关闭」：此前仅凭启动进程是否还在判断小窗是否存活，Edge/Chrome 把窗口转交给其他进程后会被误判为已关闭，进而走回退分支结束浏览器进程。现在以调试端口是否有响应为准，并在判定关闭前复核。
- 提取时若目标站点登录凭据尚未就绪，最多等待数秒重试；仍读不到时**不再关闭小窗**，并提示读到的 Cookie 条数与主要域名，便于排查。
- 登录期间的状态轮询改为只读浏览器级 Cookie，减少对登录页面的干扰；「小窗已关闭」需连续两次探测不到才提示。
- 优雅关闭小窗时，即使启动进程已退出，只要调试端口仍在也通过 CDP 关闭，保证新 Cookie 落盘。
