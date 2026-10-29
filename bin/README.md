# 外部二进制目录 (`bin/`)

在开发调试或執行打包脚本（`build/build.ps1`）前，请将以下 Windows x64 可执行文件放入本目录：

1. **`ffmpeg.exe`**（必需）
   - 用途：将下载的音视频流或本地文件统一转码为 `16kHz 单声道 16-bit PCM WAV`。
   - 推荐来源：[gyan.dev ffmpeg-release-essentials](https://www.gyan.dev/ffmpeg/builds/) 解压其中的 `bin/ffmpeg.exe`。
2. **`deno.exe`**（可选，推荐）
   - 用途：供 `yt-dlp` 在解析部分 YouTube 视频签名时作为外部 JS 运行时调用。
   - 若本目录存在 `deno.exe`，程序会自动将其路径传给 `yt-dlp`。
