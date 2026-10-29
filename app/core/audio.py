"""FFmpeg audio transcoding wrapper (16kHz mono PCM s16le WAV)."""

from __future__ import annotations

import re
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from app.core.cancel import CancelToken
from app.core.errors import AudioError, CancelledError
from app.paths import PATHS, AppPaths

ProgressCallback = Callable[[float], None]
DurationCallback = Callable[[float], None]
LogCallback = Callable[[str], None]

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _hms_to_seconds(hours: str, minutes: str, seconds: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _creation_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def convert_to_wav(
    input_path: Path,
    output_wav: Path,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    ffmpeg_path: Optional[Path] = None,
    paths: AppPaths = PATHS,
    on_duration: Optional[DurationCallback] = None,
) -> Path:
    """Convert any audio/video file to 16kHz mono 16-bit PCM WAV using ffmpeg."""
    if token:
        token.check()

    if not input_path.exists():
        raise AudioError(f"输入文件不存在：{input_path}")

    ffmpeg_bin = ffmpeg_path or paths.find_binary("ffmpeg")
    if ffmpeg_bin is None or not ffmpeg_bin.exists():
        raise AudioError(
            "未找到 ffmpeg，请确认安装目录的 bin/ffmpeg.exe 存在。",
            detail=f"searched in {paths.bin_dir} and PATH",
        )

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg_bin),
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_wav),
    ]

    if on_log:
        on_log(f"开始音频转码 (16kHz 单声道 WAV)：{input_path.name}")

    tail_lines: deque[str] = deque(maxlen=20)
    total_duration: float = 0.0

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise AudioError("启动 ffmpeg 进程失败", detail=str(exc)) from exc

    if token:
        token.register_process(proc)

    try:
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            if token and token.is_cancelled:
                proc.terminate()
                raise CancelledError("音频转码已取消")

            line = raw_line.strip()
            if not line:
                continue
            tail_lines.append(line)

            if total_duration <= 0.0:
                m_dur = _DURATION_RE.search(line)
                if m_dur:
                    total_duration = _hms_to_seconds(m_dur.group(1), m_dur.group(2), m_dur.group(3))
                    if on_duration and total_duration > 0.0:
                        on_duration(total_duration)

            if total_duration > 0.0 and on_progress:
                m_time = _TIME_RE.search(line)
                if m_time:
                    elapsed = _hms_to_seconds(m_time.group(1), m_time.group(2), m_time.group(3))
                    on_progress(min(1.0, max(0.0, elapsed / total_duration)))

        ret = proc.wait()
    finally:
        if token:
            token.unregister_process(proc)

    if token and token.is_cancelled:
        raise CancelledError("音频转码已取消")

    if ret != 0 or not output_wav.exists() or output_wav.stat().st_size == 0:
        stderr_tail = "\n".join(tail_lines)
        raise AudioError(
            "ffmpeg 音频转码失败，请检查源文件是否包含有效音轨。",
            detail=f"exit_code={ret}\n{stderr_tail}",
        )

    if on_progress:
        on_progress(1.0)
    if on_log:
        on_log(f"音频转码完成：{output_wav.name}")

    return output_wav
