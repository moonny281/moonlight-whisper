"""Unified exception hierarchy for Moonlight Whisper core modules."""

from __future__ import annotations

from dataclasses import dataclass


class MoonlightError(Exception):
    """Base exception carrying a user-friendly message and technical diagnostic detail."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(user_message if not detail else f"{user_message} ({detail})")
        self.user_message = user_message
        self.detail = detail


class DownloadError(MoonlightError):
    """Raised when yt-dlp metadata extraction or audio download fails."""


class AudioError(MoonlightError):
    """Raised when ffmpeg audio transcoding fails."""


class TranscribeError(MoonlightError):
    """Raised when faster-whisper model loading or transcription fails."""


class GpuError(MoonlightError):
    """Raised when CUDA driver/runtime detection, download, or activation fails."""


class ModelError(MoonlightError):
    """Raised when Whisper model download, verification, or manual import fails."""


class CancelledError(MoonlightError):
    """Raised when the user cancels a running task or download."""

    def __init__(self, user_message: str = "任务已取消", detail: str = "") -> None:
        super().__init__(user_message=user_message, detail=detail)


@dataclass
class ErrorInfo:
    """Structured error payload emitted to the UI."""
    category: str
    user_message: str
    detail: str = ""
    recoverable_hint: str = ""

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ErrorInfo":
        if isinstance(exc, CancelledError):
            return cls(
                category="cancelled",
                user_message=exc.user_message,
                detail=exc.detail,
            )
        if isinstance(exc, DownloadError):
            hint = "可尝试在「设置」中配置代理、导入浏览器 Cookies 或点击「更新 yt-dlp」。"
            return cls(
                category="download",
                user_message=exc.user_message,
                detail=exc.detail,
                recoverable_hint=hint,
            )
        if isinstance(exc, AudioError):
            return cls(
                category="audio",
                user_message=exc.user_message,
                detail=exc.detail,
                recoverable_hint="请确认 bin/ffmpeg.exe 存在且输入音视频文件未损坏。",
            )
        if isinstance(exc, GpuError):
            return cls(
                category="gpu",
                user_message=exc.user_message,
                detail=exc.detail,
                recoverable_hint="可在「GPU 运行库」窗口重新下载/导入，或切换至 CPU 模式运行。",
            )
        if isinstance(exc, ModelError):
            return cls(
                category="model",
                user_message=exc.user_message,
                detail=exc.detail,
                recoverable_hint="请开启 HF 国内镜像或使用「手动导入」选择本地模型目录。",
            )
        if isinstance(exc, TranscribeError):
            return cls(
                category="transcribe",
                user_message=exc.user_message,
                detail=exc.detail,
                recoverable_hint="若提示无人声，可尝试在设置中关闭 VAD 静音过滤。",
            )
        return cls(
            category="unexpected",
            user_message=f"发生未预期的错误：{exc}",
            detail=repr(exc),
        )
