"""faster-whisper transcription engine with model caching, progress streaming, and GPU->CPU fallback."""

from __future__ import annotations

import gc
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.cancel import CancelToken
from app.core.errors import CancelledError, ModelError, TranscribeError
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

            # Release previous model before loading a new one
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
                    "未安装 faster-whisper 组件，无法执行语音转录。",
                    detail=str(exc),
                ) from exc

            if on_log:
                on_log(f"正在加载模型：{model_path_or_size} (设备={device}, 精度={compute_type})...")

            download_root.mkdir(parents=True, exist_ok=True)
            local_dir = download_root / model_path_or_size
            target = str(local_dir) if (local_dir / "model.bin").exists() else model_path_or_size

            model = WhisperModel(
                target,
                device=device,
                device_index=0,
                compute_type=compute_type,
                cpu_threads=cpu_threads,
                download_root=str(download_root),
            )
            self._model = model
            self._key = key
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

    vad_Params = {"min_silence_duration_ms": opts.min_silence_duration_ms} if opts.vad else None

    segments_gen, info = model.transcribe(
        str(wav_path),
        language=opts.language,
        beam_size=beam_size,
        vad_filter=opts.vad,
        vad_parameters=vad_Params,
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
        collected.append(Segment(start=start, end=end, text=text))

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
                break

    raise TranscribeError(
        f"语音转录失败：{last_exc}",
        detail=repr(last_exc),
    )
