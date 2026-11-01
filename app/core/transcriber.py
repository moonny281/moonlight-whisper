"""faster-whisper transcription engine with Chinese-path-safe CTranslate2 loading, model caching, and GPU->CPU fallback."""

from __future__ import annotations

import ctypes
import gc
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core import model_manager
from app.core.cancel import CancelToken
from app.core.errors import CancelledError, TranscribeError
from app.paths import PATHS, AppPaths

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscribeOptions:
    model: str = "large-v3-turbo"
    language: Optional[str] = None
    device: str = "cpu"                  # "cpu" | "cuda"
    compute_type: Optional[str] = None
    cpu_threads: int = 0
    vad: bool = True
    min_silence_duration_ms: int = 500
    to_simplified: bool = True
    beam_size: Optional[int] = None
    models_dir: Optional[Path] = None
    vram_gb: float = 8.0


def select_compute_type(
    device: str,
    requested: Optional[str] = None,
    vram_gb: float = 8.0,
) -> str:
    """Choose optimal CTranslate2 compute_type according to §5.3 table."""
    if requested and requested.strip():
        return requested.strip()
    if device == "cuda":
        return "float16" if vram_gb >= 6.0 else "int8_float16"
    return "int8"


def _convert_to_simplified(text: str) -> str:
    try:
        from opencc import OpenCC  # type: ignore
        converter = OpenCC("t2s")
        return str(converter.convert(text))
    except Exception:
        return text


def _try_windows_short_path(path: Path) -> Optional[str]:
    """Attempt to convert a non-ASCII Windows path to its 8.3 ASCII short path via Win32 API."""
    if sys.platform != "win32":
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        res = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
        if res > 0 and buf.value and buf.value.isascii():
            return buf.value
    except Exception:
        pass
    return None


class _ModelCache:
    """Singleton cache keeping at most one loaded WhisperModel in memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: Optional[Tuple[str, str, str, int]] = None
        self._model: Any = None

    def get_or_create(
        self,
        model_path_or_size: str,
        device: str,
        compute_type: str,
        cpu_threads: int,
        download_root: Path,
        on_log: Optional[LogCallback] = None,
    ) -> Any:
        key = (model_path_or_size, device, compute_type, cpu_threads)
        with self._lock:
            if self._model is not None and self._key == key:
                if on_log:
                    on_log(f"复用已加载模型：{model_path_or_size} ({device}/{compute_type})")
                return self._model

            if self._model is not None:
                if on_log:
                    on_log("释放旧模型缓存...")
                self._model = None
                self._key = None
                gc.collect()

            try:
                from faster_whisper import WhisperModel  # type: ignore
            except ImportError as exc:
                raise TranscribeError(
                    "加载 faster-whisper 组件失败，请检查运行环境依赖。",
                    detail=repr(exc),
                ) from exc

            download_root.mkdir(parents=True, exist_ok=True)
            local_dir = (download_root / model_path_or_size).resolve()
            spec = model_manager.MODEL_SPEC_MAP.get(model_path_or_size)
            if spec and local_dir.exists():
                model_manager._ensure_preprocessor_config(local_dir, spec.mel_bins)

            is_valid_local, missing_files = model_manager.verify_model_dir(local_dir)
            if local_dir.exists() and not is_valid_local and on_log:
                on_log(f"[模型校验警告] 本地模型目录 {local_dir} 缺少文件：{', '.join(missing_files)}")

            if on_log:
                on_log(f"正在加载模型：{model_path_or_size} (设备={device}, 精度={compute_type})...")

            # Workaround for CTranslate2 Windows C++ std::ifstream failing on Chinese / non-ASCII directory paths:
            # When loading from `local_dir` whose path contains non-ASCII characters, temporarily chdir into `local_dir`
            # and pass `"."` (pure ASCII) so CTranslate2 opens `"./model.bin"`, `"./config.json"`, `"./vocabulary.json"`.
            prev_cwd: Optional[str] = None
            try:
                if is_valid_local:
                    resolved_str = str(local_dir)
                    if not resolved_str.isascii():
                        short_p = _try_windows_short_path(local_dir)
                        if short_p:
                            target_arg = short_p
                        else:
                            prev_cwd = os.getcwd()
                            os.chdir(local_dir)
                            target_arg = "."
                    else:
                        target_arg = resolved_str

                    model = WhisperModel(
                        target_arg,
                        device=device,
                        device_index=0,
                        compute_type=compute_type,
                        cpu_threads=cpu_threads,
                        local_files_only=True,
                    )
                else:
                    model = WhisperModel(
                        model_path_or_size,
                        device=device,
                        device_index=0,
                        compute_type=compute_type,
                        cpu_threads=cpu_threads,
                        download_root=str(download_root),
                    )
            finally:
                if prev_cwd is not None:
                    try:
                        os.chdir(prev_cwd)
                    except OSError:
                        pass

            self._model = model
            self._key = key
            if on_log:
                on_log(f"模型加载成功：{model_path_or_size} ({device}/{compute_type})")
            return model

    def clear(self) -> None:
        with self._lock:
            self._model = None
            self._key = None
            gc.collect()


MODEL_CACHE = _ModelCache()


def build_fallback_chain(opts: TranscribeOptions) -> List[Tuple[str, str]]:
    """Build `(device, compute_type)` fallback sequence according to §5.3:
    `GPU float16 -> GPU int8_float16 -> CPU int8`.
    """
    primary_ct = select_compute_type(opts.device, opts.compute_type, vram_gb=opts.vram_gb)
    if opts.device == "cuda":
        chain: List[Tuple[str, str]] = [("cuda", primary_ct)]
        if primary_ct != "int8_float16":
            chain.append(("cuda", "int8_float16"))
        chain.append(("cpu", "int8"))
        return chain
    return [("cpu", primary_ct)]


def _run_single_attempt(
    wav_path: Path,
    opts: TranscribeOptions,
    device: str,
    compute_type: str,
    on_progress: Optional[ProgressCallback],
    on_log: Optional[LogCallback],
    token: Optional[CancelToken],
    paths: AppPaths,
) -> Tuple[List[Segment], Dict[str, Any]]:
    if token:
        token.check()

    models_dir = opts.models_dir or paths.models_dir
    model = MODEL_CACHE.get_or_create(
        model_path_or_size=opts.model,
        device=device,
        compute_type=compute_type,
        cpu_threads=opts.cpu_threads,
        download_root=models_dir,
        on_log=on_log,
    )

    if token:
        token.check()

    beam_size = opts.beam_size if opts.beam_size is not None else (5 if device == "cuda" else 2)
    initial_prompt: Optional[str] = None
    if opts.language in (None, "zh") and opts.to_simplified:
        initial_prompt = "以下是普通话的句子。"

    vad_params = {"min_silence_duration_ms": opts.min_silence_duration_ms} if opts.vad else None

    segments_gen, info = model.transcribe(
        str(wav_path),
        language=opts.language,
        beam_size=beam_size,
        vad_filter=opts.vad,
        vad_parameters=vad_params,
        condition_on_previous_text=False,
        initial_prompt=initial_prompt,
        word_timestamps=False,
    )

    duration = float(getattr(info, "duration", 0.0) or 0.0)
    detected_lang = str(getattr(info, "language", opts.language or "unknown"))
    lang_prob = float(getattr(info, "language_probability", 1.0) or 1.0)

    if on_log:
        on_log(
            f"检测到音频语言：{detected_lang} (置信度 {lang_prob:.2f})，总时长 {duration:.1f}s，开始逐段解码..."
        )

    collected: List[Segment] = []
    for raw_seg in segments_gen:
        if token:
            token.check()
        start = float(raw_seg.start)
        end = float(raw_seg.end)
        text = str(raw_seg.text).strip()
        if not text:
            continue
        if opts.to_simplified:
            text = _convert_to_simplified(text)
        collected.append(Segment(start=start, end=seg_end if (seg_end := end) else end, text=text))

        if on_progress and duration > 0:
            ratio = min(1.0, max(0.0, end / duration))
            on_progress(ratio, f"[{end:.1f}s / {duration:.1f}s] {text[:36]}")

    meta = {
        "model": opts.model,
        "device": device,
        "compute_type": compute_type,
        "language": detected_lang,
        "language_probability": lang_prob,
        "duration": duration,
    }
    return collected, meta


def transcribe(
    wav_path: Path,
    opts: TranscribeOptions,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> Tuple[List[Segment], Dict[str, Any]]:
    """Transcribe `wav_path` with automatic GPU OOM/DLL fallback to CPU."""
    if not wav_path.exists():
        raise TranscribeError(f"待转录 WAV 文件不存在：{wav_path}")

    chain = build_fallback_chain(opts)
    last_exc: Optional[BaseException] = None

    for idx, (device, compute_type) in enumerate(chain):
        if token:
            token.check()
        try:
            segments, meta = _run_single_attempt(
                wav_path=wav_path,
                opts=opts,
                device=device,
                compute_type=compute_type,
                on_progress=on_progress,
                on_log=on_log,
                token=token,
                paths=paths,
            )
            meta["fallback_occurred"] = idx > 0
            return segments, meta
        except CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            MODEL_CACHE.clear()
            if idx + 1 < len(chain):
                next_dev, next_ct = chain[idx + 1]
                if on_log:
                    on_log(
                        f"[回退提示] {device}/{compute_type} 执行失败 ({exc})，"
                        f"自动降级至 {next_dev}/{next_ct} 重试..."
                    )
            else:
                if on_log:
                    on_log(f"[转录错误] {device}/{compute_type} 最终执行失败：{exc!r}")
                break

    raise TranscribeError(
        f"模型加载或语音转录失败：{last_exc}",
        detail=repr(last_exc),
    )
