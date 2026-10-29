#!/usr/bin/env python3
"""Command-line interface for Moonlight Whisper (Milestone M1 & batch scripting)."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from typing import List

from app.config import Config
from app.core import downloader, gpu_runtime, pipeline
from app.core.cancel import CancelToken
from app.core.downloader import DownloadOptions
from app.core.errors import CancelledError, ErrorInfo
from app.core.pipeline import Callbacks, Job
from app.logger import LOG_MANAGER
from app.paths import PATHS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moonlight-whisper-cli",
        description="Moonlight Whisper 命令行视频/音频转录工具 (yt-dlp + faster-whisper)",
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="视频链接 (URL) 或本地音视频文件路径，支持多个输入",
    )
    parser.add_argument("-m", "--model", help="Whisper 模型名称 (如 tiny/base/small/medium/large-v3/large-v3-turbo)")
    parser.add_argument("-l", "--language", help="音频语言代码 (如 zh/en/ja，留空为自动检测)")
    parser.add_argument("-o", "--output-dir", help="输出目录路径")
    parser.add_argument("--cpu", action="store_true", help="强制使用 CPU 模式")
    parser.add_argument("--gpu", action="store_true", help="启用 NVIDIA GPU 加速")
    parser.add_argument("--srt", action="store_true", help="同时输出 .srt 字幕文件")
    parser.add_argument("--keep-audio", action="store_true", help="保留下载的原始音频文件")
    parser.add_argument("--playlist", action="store_true", help="允许展开播放列表 / 多 P 视频")
    parser.add_argument("--no-vad", action="store_true", help="关闭 VAD 静音过滤")
    parser.add_argument("--proxy", help="HTTP/SOCKS5 代理地址")
    parser.add_argument("--detect-gpu", action="store_true", help="仅检测当前机器的 NVIDIA GPU 与 CUDA 运行库状态")
    parser.add_argument("--install-gpu-runtime", action="store_true", help="在线下载并安装 CUDA 12 运行库")
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = Config.load(paths=PATHS)
    gpu_runtime.preload_dll_directory_on_startup(PATHS)
    logger = LOG_MANAGER.setup(level=cfg.log_level, paths=PATHS)

    if args.model:
        cfg.model = args.model
    if args.language:
        cfg.language = None if args.language == "auto" else args.language
    if args.output_dir:
        cfg.output_dir = args.output_dir
    if args.cpu:
        cfg.use_gpu = False
    if args.gpu:
        cfg.use_gpu = True
    if args.srt:
        cfg.write_srt = True
    if args.keep_audio:
        cfg.keep_audio = True
    if args.playlist:
        cfg.allow_playlist = True
    if args.no_vad:
        cfg.vad = False
    if args.proxy is not None:
        cfg.proxy = args.proxy

    cfg.apply_to_paths(PATHS)
    PATHS.ensure_runtime_dirs()

    token = CancelToken()

    def _sigint_handler(signum: int, frame: object) -> None:
        print("\n[中断] 收到 Ctrl+C，正在安全取消当前任务...", file=sys.stderr)
        token.cancel()

    signal.signal(signal.SIGINT, _sigint_handler)

    if args.detect_gpu:
        info = gpu_runtime.detect(PATHS)
        print(f"GPU 状态: {info.status.value}")
        print(f"设备名称: {info.device_name or 'N/A'}")
        print(f"显存大小: {info.vram_gb:.1f} GB")
        print(f"驱动版本: {info.driver_version or 'N/A'}")
        print(f"说明信息: {info.message}")
        if info.detail:
            print(f"详细诊断: {info.detail}")
        return 0

    if args.install_gpu_runtime:
        print("开始下载并安装 CUDA 运行库...")
        info = gpu_runtime.install(
            mirror=cfg.pypi_mirror,
            proxy=cfg.proxy,
            on_progress=lambda r, m: print(f"\r[{r * 100:5.1f}%] {m}", end="", flush=True),
            on_log=lambda m: print(f"\n{m}"),
            token=token,
            paths=PATHS,
        )
        print(f"\n安装后状态: {info.status.value} - {info.message}")
        return 0 if info.status == gpu_runtime.GpuStatus.READY else 1

    if not args.inputs:
        parser.print_help()
        return 1

    jobs: List[Job] = []
    dl_opts = DownloadOptions(
        proxy=cfg.proxy,
        cookies_file=cfg.cookies_file,
        cookies_from_browser=cfg.cookies_from_browser,
        allow_playlist=cfg.allow_playlist,
        ffmpeg_dir=PATHS.bin_dir,
    )

    for raw_input in args.inputs:
        candidate = raw_input.strip()
        if not candidate:
            continue
        temp_job = Job(source=candidate)
        if not temp_job.is_local_file and cfg.allow_playlist:
            try:
                entries = downloader.extract_playlist_or_info(
                    candidate,
                    dl_opts,
                    token=token,
                    on_log=lambda m: logger.info(m),
                    paths=PATHS,
                )
                for entry in entries:
                    jobs.append(Job(source=entry.url, title_hint=entry.title))
            except Exception:
                jobs.append(temp_job)
        else:
            jobs.append(temp_job)

    exit_code = 0
    for idx, job in enumerate(jobs, start=1):
        print(f"\n========== 任务 [{idx}/{len(jobs)}] {job.source} ==========")
        cb = Callbacks(
            on_stage=lambda s: print(f"[阶段] {s}"),
            on_progress=lambda p: print(f"\r[进度] {p:3d}%", end="", flush=True),
            on_log=lambda m: print(f"\n  {m}"),
            on_title=lambda t: print(f"\n[标题] {t}"),
        )
        try:
            res = pipeline.run(job=job, cfg=cfg, cb=cb, token=token, paths=PATHS)
            print(f"\n[成功] 输出文件: {res.md_path}")
            if res.srt_path:
                print(f"[成功] 字幕文件: {res.srt_path}")
        except CancelledError:
            print("\n[已取消] 任务被用户中止。")
            return 130
        except Exception as exc:
            err = ErrorInfo.from_exception(exc)
            print(f"\n[失败] {err.user_message}", file=sys.stderr)
            if err.detail:
                print(f"[详情] {err.detail}", file=sys.stderr)
            if err.recoverable_hint:
                print(f"[建议] {err.recoverable_hint}", file=sys.stderr)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
