# Moonlight Whisper

> 基于 `yt-dlp` + `网站账号托管与自动续期引擎` + `faster-whisper (CTranslate2)` 的 Windows 桌面端视频/音频转录工具。
> 粘贴视频链接或拖入本地音视频文件 → 自动挂载托管站点 Cookie 并下载最佳音轨 → 转码 16kHz WAV → Whisper 语音转录 → 输出带时间戳的 Markdown 文档与可选 SRT 字幕。

## 核心特性

- **开箱即用**：CPU 模式下无需任何额外配置即可运行；首次勾选 NVIDIA GPU 加速时支持在线按需下载 CUDA 12（cuBLAS 12 + cuDNN 9）运行库（支持国内清华/阿里云 PyPI 镜像、断点续传、SHA256 校验与离线导入）。
- **网站账号托管（Account Vault）与安全登录小窗**：点击「打开安全登录小窗」，调用电脑上真实的 Edge / Chrome 以单窗口小窗（`--app` 模式，无标签页）打开登录页，Google 等站点认可这种真实浏览器环境，可正常登录 YouTube/Google（YouTube、哔哩哔哩、抖音、TikTok、X/Twitter、Instagram、小红书，并支持 `plugins/sites.json` 扩展自定义站点）。小窗使用应用自己的独立托管目录（`account_vault/browser_profile`），与日常浏览器的配置和账号隔离，并关闭账号同步、跳过首次运行向导；登录凭据通过本地调试端口明文读取，点击「我已登录，提取并托管凭据」后写入 `cookies/<站点>.txt`，同时顺带托管小窗里已登录的其他站点。下载时按域名自动挂载凭据并原子回写轮换令牌，凭据过期时可在后台无头静默刷新。另提供应用内置小窗（QtWebEngine，适合 B站/抖音等，Google 会拦截）与「从剪贴板导入」。
- **SenseVoiceSmall 模型支持**：模型下拉框与「模型管理」中提供 `SenseVoiceSmall`（阿里 FunASR，中/英/日/韩/粤语，自带标点与数字规整）。CPU 使用 sherpa-onnx 导出的 int8 ONNX 模型（约 240 MB，按需下载，也可手动导入含 `model.int8.onnx` + `tokens.txt` 的文件夹）；推理复用 `onnxruntime`，特征提取使用 `kaldi-native-fbank`，不依赖 PyTorch / FunASR；语音分段复用 faster-whisper 内置的 Silero VAD（单段最长 15 秒，遵循「VAD 静音过滤」设置）。「繁体转简体」只作用于中文/粤语输出，不会改动日文汉字。
- **SenseVoiceSmall GPU 加速**：勾选「启用 NVIDIA GPU 加速」后，SenseVoiceSmall 通过 ONNX Runtime CUDA 推理。首次使用会按需补充下载 `cudart` / `cuFFT`（装入现有 `gpu_runtime/`）与 fp32 权重 `model.onnx`（约 940 MB；int8 版本大量使用 MatMulInteger / DynamicQuantizeLinear 整数算子，CUDA 上缺乏对应支持，GPU 上改用 fp32）。任何一步失败、或 CUDA 推理中途出错，都会自动回退到 CPU int8 并写入日志，不会中断任务。
- **全语言选择与过滤**：主界面「识别语言」覆盖 Whisper 支持的全部语言（含粤语），可输入语言名或代码搜索；当前模型不支持的语言会置灰（如 SenseVoiceSmall 仅 zh/en/ja/ko/yue，Whisper tiny~medium 不含粤语），始终保留「自动检测」。
- **语言路由**：勾选「按语言自动选择模型」后，按「偏好设置 → 语言路由」中的规则为每个任务选模型。指定了语言则直接查规则；选「自动检测」则先用小型 Whisper 模型（默认 `base`，约 145 MB，CPU）识别语言，再查规则。默认规则：中文/粤语 → SenseVoiceSmall，其余语言 → 默认模型（`large-v3-turbo`）。语言检测失败或置信度低于 0.4 时，使用主界面所选模型。命令行：`--route` / `--no-route`。
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
│  └─ core/                    # 零 Qt 依赖核心层 (downloader/browser_cookies/browser_login/site_cookies/webview_login/audio/transcriber/sensevoice_engine/router/languages/summarizer/md_writer/gpu_runtime/model_manager/pipeline)
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

### v1.4.4
- 新增 **SenseVoiceSmall** 语音识别模型（中/英/日/韩/粤语），CPU int8 与 CUDA fp32 均可用，GPU 不可用时自动回退 CPU。
- 新增 **语言路由**：按语言自动选择识别模型，规则可在「偏好设置 → 语言路由」编辑；自动检测语言时用小型 Whisper 模型预先识别。
- 「识别语言」扩展为全部 100 种语言，支持搜索，并按所选模型置灰不支持的语言；Whisper tiny~medium 选择粤语时不再报错，而是提示并改用自动检测。
- 依赖变更：`onnxruntime` 改为 `onnxruntime-gpu>=1.22,<1.25`（CUDA 12 构建，两者互斥，升级前请先 `pip uninstall onnxruntime`），新增 `kaldi-native-fbank`。安装包体积会增大约 200 MB（CUDA 执行提供程序）。
- 配置新增 `route_enabled`、`route_rules`、`route_fallback_model`、`route_detect_model`，旧版 `config.json` 自动补默认值。
