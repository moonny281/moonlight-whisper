"""Transcription entry point (faster-whisper engine; SenseVoiceSmall is dispatched to `sensevoice_engine`) with PyAV 18/19 compatibility, pure-Python WAV decoding, Chinese-path-safe loading, and VAD/GPU fallback."""

from __future__ import annotations

import ctypes
import gc
import inspect
import os
import subprocess
import sys
import threading
import wave
import numpy as np
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

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


def load_wav_as_float32_array(wav_path: Path) -> Optional[Any]:
    """Read a 16kHz 16-bit PCM WAV file directly into a 1D float32 numpy array.

    Completely bypasses `faster_whisper.audio.decode_audio` / `av.open(metadata_errors="ignore")`
    (which crashes on PyAV 18/19 with `TypeError: open() got an unexpected keyword argument 'metadata_errors'`)
    as well as any C-level Windows path encoding issues.
    """
    try:
        if not wav_path.exists() or wav_path.stat().st_size <= 44:
            return None
        import numpy as np  # type: ignore
        with wave.open(str(wav_path), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            if n_frames <= 0 or sampwidth != 2:
                return None
            raw_bytes = wf.readframes(n_frames)
        if not raw_bytes:
            return None
        pcm = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if n_channels > 1:
            pcm = pcm.reshape(-1, n_channels).mean(axis=1)
        if framerate != 16000 and framerate > 0 and pcm.size > 0:
            duration = pcm.size / float(framerate)
            target_len = int(round(duration * 16000))
            if target_len > 0:
                pcm = np.interp(
                    np.linspace(0.0, 1.0, target_len, endpoint=False),
                    np.linspace(0.0, 1.0, pcm.size, endpoint=False),
                    pcm,
                ).astype(np.float32)
        return np.ascontiguousarray(pcm, dtype=np.float32)
    except Exception:
        return None


def _patch_runtime_compat() -> None:
    """Apply runtime compatibility patches for PyAV 18/19 and ONNX Runtime non-ASCII asset paths."""
    # 1. Patch PyAV 18/19 `av.open` which removed `metadata_errors="ignore"` used by faster-whisper <=1.2.1
    try:
        import av  # type: ignore
        if not getattr(av, "_moonlight_patched", False):
            orig_open = av.open
            try:
                sig = inspect.signature(orig_open)
                has_meta_err = "metadata_errors" in sig.parameters
            except (ValueError, TypeError):
                has_meta_err = False

            if not has_meta_err:
                def _compat_av_open(*args: Any, **kwargs: Any) -> Any:
                    kwargs.pop("metadata_errors", None)
                    return orig_open(*args, **kwargs)

                av.open = _compat_av_open  # type: ignore[assignment]
            av._moonlight_patched = True  # type: ignore[attr-defined]
    except Exception:
        pass

    # 2. Patch `faster_whisper.vad.SileroVADModel.__init__` to load `silero_vad_v6.onnx` via in-memory bytes
    # so ONNX Runtime C++ never fails when the app is extracted into a Chinese directory on Windows.
    try:
        import faster_whisper.vad as fw_vad  # type: ignore
        if not getattr(fw_vad, "_moonlight_vad_patched", False):
            orig_vad_init = fw_vad.SileroVADModel.__init__

            def _compat_vad_init(self: Any, path: Any) -> None:
                try:
                    import onnxruntime  # type: ignore
                    candidates = [Path(str(path))]
                    if getattr(sys, "frozen", False):
                        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal"))
                        candidates.append(meipass / "faster_whisper" / "assets" / Path(str(path)).name)
                        candidates.append(meipass / "faster_whisper" / "assets" / "silero_vad_v6.onnx")
                        candidates.append(meipass / "faster_whisper" / "assets" / "silero_vad.onnx")

                    for cand in candidates:
                        if cand.exists() and cand.is_file():
                            opts = onnxruntime.SessionOptions()
                            opts.inter_op_num_threads = 1
                            opts.intra_op_num_threads = 1
                            opts.enable_cpu_mem_arena = False
                            opts.log_severity_level = 4
                            model_bytes = cand.read_bytes()
                            self.session = onnxruntime.InferenceSession(
                                model_bytes,
                                providers=["CPUExecutionProvider"],
                                sess_options=opts,
                            )
                            return
                except Exception:
                    pass
                orig_vad_init(self, path)

            fw_vad.SileroVADModel.__init__ = _compat_vad_init  # type: ignore[method-assign]
            fw_vad._moonlight_vad_patched = True  # type: ignore[attr-defined]
    except Exception:
        pass


def _try_windows_short_path(path: Path) -> Optional[str]:
    """Attempt to convert a non-ASCII Windows path to its 8.3 ASCII short path via Win32 API."""
    if sys.platform != "win32":
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        res = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
        if res > 0 and buf.value and buf.value.isascii() and Path(buf.value).exists():
            return buf.value
    except Exception:
        pass
    return None


def _try_windows_ascii_junction(local_dir: Path) -> Optional[str]:
    """Create a zero-privilege NTFS directory junction under `C:\\Users\\Public\\MoonlightWhisper_Models`
    when `local_dir` is on a drive with 8.3 short names disabled and contains non-ASCII characters.
    """
    if sys.platform != "win32":
        return None
    try:
        public_root = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "MoonlightWhisper_Models"
        if not str(public_root).isascii():
            return None
        public_root.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "-_.")) else "_" for ch in local_dir.name)
        junction_path = public_root / (safe_name or "whisper_model")

        if junction_path.exists():
            try:
                if junction_path.resolve() == local_dir.resolve():
                    return str(junction_path)
            except Exception:
                pass
            subprocess.run(
                ["cmd", "/c", "rmdir", str(junction_path)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                timeout=5,
            )

        try:
            import _winapi  # type: ignore
            _winapi.CreateJunction(str(local_dir), str(junction_path))
        except Exception:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction_path), str(local_dir)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                timeout=5,
            )

        if junction_path.exists() and (junction_path / "model.bin").exists() and str(junction_path).isascii():
            return str(junction_path)
    except Exception:
        pass
    return None


def _load_model_files_into_memory(local_dir: Path) -> Dict[str, bytes]:
    """Load CTranslate2 Whisper model files into a memory dict for `WhisperModel(..., files=files)`."""
    files: Dict[str, bytes] = {}
    for name in ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json", "vocabulary.txt"):
        fp = local_dir / name
        if fp.exists() and fp.is_file():
            files[name] = fp.read_bytes()
    return files


def _release_sensevoice_cache() -> None:
    """Free a resident SenseVoice model (only if that engine was ever imported) so one engine stays in memory."""
    mod = sys.modules.get("app.core.sensevoice_engine")
    if mod is not None:
        try:
            mod.SENSEVOICE_CACHE.clear()
        except Exception:
            pass


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
        _patch_runtime_compat()
        _release_sensevoice_cache()
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
                on_log(f"[模型校验警告] 本地模型目录 {local_dir} 缺少或损坏文件：{', '.join(missing_files)}")

            if on_log:
                on_log(f"正在加载模型：{model_path_or_size} (设备={device}, 精度={compute_type})...")

            prev_cwd: Optional[str] = None
            try:
                if is_valid_local:
                    resolved_str = str(local_dir)
                    if not resolved_str.isascii():
                        short_p = _try_windows_short_path(local_dir) or _try_windows_ascii_junction(local_dir)
                        if short_p:
                            target_arg = short_p
                        else:
                            prev_cwd = os.getcwd()
                            os.chdir(local_dir)
                            target_arg = "."
                    else:
                        target_arg = resolved_str

                    try:
                        model = WhisperModel(
                            target_arg,
                            device=device,
                            device_index=0,
                            compute_type=compute_type,
                            cpu_threads=cpu_threads,
                            local_files_only=True,
                        )
                    except Exception as path_exc:
                        # Ultimate fallback for Windows Chinese paths where 8.3 and junctions are unavailable:
                        # read files in Python (UTF-16 native support) and pass in-memory `files` dict to WhisperModel
                        if not resolved_str.isascii():
                            if on_log:
                                on_log(f"[路径兼容] 正在使用内存字节流方式加载模型 ({path_exc})...")
                            mem_files = _load_model_files_into_memory(local_dir)
                            model = WhisperModel(
                                model_path_or_size,
                                device=device,
                                device_index=0,
                                compute_type=compute_type,
                                cpu_threads=cpu_threads,
                                local_files_only=True,
                                files=mem_files,
                            )
                        else:
                            raise
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


def clear_all_model_caches() -> None:
    """Drop every resident ASR model (Whisper and SenseVoice), e.g. before re-downloading a corrupted model."""
    MODEL_CACHE.clear()
    _release_sensevoice_cache()


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


def _consume_transcribe_generator(
    model: Any,
    audio_input: Any,
    opts: TranscribeOptions,
    beam_size: int,
    initial_prompt: Optional[str],
    use_vad: bool,
    on_progress: Optional[ProgressCallback],
    on_log: Optional[LogCallback],
    token: Optional[CancelToken],
) -> Tuple[List[Segment], float, str, float]:
    vad_params = {"min_silence_duration_ms": opts.min_silence_duration_ms} if use_vad else None
    segments_gen, info = model.transcribe(
        audio_input,
        language=opts.language,
        beam_size=beam_size,
        vad_filter=use_vad,
        vad_parameters=vad_params,
        condition_on_previous_text=False,
        initial_prompt=initial_prompt,
        word_timestamps=False,
    )

    duration = float(getattr(info, "duration", 0.0) or 0.0)
    detected_lang = str(getattr(info, "language", opts.language or "unknown"))
    lang_prob = float(getattr(info, "language_probability", 1.0) or 1.0)

    if on_log:
        vad_label = "开启" if use_vad else "关闭"
        on_log(
            f"检测到音频语言：{detected_lang} (置信度 {lang_prob:.2f}，VAD={vad_label})，总时长 {duration:.1f}s，开始逐段解码..."
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

    return collected, duration, detected_lang, lang_prob


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

    # Prefer pure-Python wave+numpy float32 array to avoid PyAV 18/19 `metadata_errors` crash & C path issues
    pcm_array = load_wav_as_float32_array(wav_path)
    audio_input: Any = pcm_array if pcm_array is not None else str(wav_path)

    if opts.vad:
        try:
            collected, duration, detected_lang, lang_prob = _consume_transcribe_generator(
                model=model,
                audio_input=audio_input,
                opts=opts,
                beam_size=beam_size,
                initial_prompt=initial_prompt,
                use_vad=True,
                on_progress=on_progress,
                on_log=on_log,
                token=token,
            )
            if not collected and duration > 1.5:
                if on_log:
                    on_log("[VAD 回退] 开启 VAD 后未检出人声片段，自动切换为无 VAD 模式重试...")
                collected, duration, detected_lang, lang_prob = _consume_transcribe_generator(
                    model=model,
                    audio_input=audio_input,
                    opts=opts,
                    beam_size=beam_size,
                    initial_prompt=initial_prompt,
                    use_vad=False,
                    on_progress=on_progress,
                    on_log=on_log,
                    token=token,
                )
        except CancelledError:
            raise
        except Exception as vad_exc:
            # Only fallback to vad_filter=False if the error is not a CUDA OOM/DLL error
            err_lower = str(vad_exc).lower()
            if device == "cuda" and any(k in err_lower for k in ("cublas", "cudnn", "cuda", "out of memory")):
                raise
            if on_log:
                on_log(f"[VAD 回退] VAD 语音活动检测异常 ({vad_exc})，自动切换为无 VAD 模式继续转录...")
            collected, duration, detected_lang, lang_prob = _consume_transcribe_generator(
                model=model,
                audio_input=audio_input,
                opts=opts,
                beam_size=beam_size,
                initial_prompt=initial_prompt,
                use_vad=False,
                on_progress=on_progress,
                on_log=on_log,
                token=token,
            )
    else:
        collected, duration, detected_lang, lang_prob = _consume_transcribe_generator(
            model=model,
            audio_input=audio_input,
            opts=opts,
            beam_size=beam_size,
            initial_prompt=initial_prompt,
            use_vad=False,
            on_progress=on_progress,
            on_log=on_log,
            token=token,
        )

    meta = {
        "model": opts.model,
        "device": device,
        "compute_type": compute_type,
        "language": detected_lang,
        "language_probability": lang_prob,
        "duration": duration,
    }
    return collected, meta


LANGUAGE_DETECT_MAX_SECONDS = 600


def detect_language(
    wav_path: Path,
    model_name: str,
    models_dir: Optional[Path] = None,
    cpu_threads: int = 0,
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> Tuple[Optional[str], float]:
    """Identify the spoken language with a small Whisper model (CPU/int8). Returns `(code, probability)`.

    Used by language routing to decide which model transcribes the audio. Detection looks at up to three speech
    segments from the first `LANGUAGE_DETECT_MAX_SECONDS` seconds. Any failure yields `(None, 0.0)`.
    """
    import inspect

    if token:
        token.check()
    models_dir = models_dir or paths.models_dir
    try:
        model = MODEL_CACHE.get_or_create(
            model_path_or_size=model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=cpu_threads,
            download_root=models_dir,
            on_log=on_log,
        )
        if token:
            token.check()
        audio = load_wav_as_float32_array(wav_path)
        if audio is None or getattr(audio, "size", 0) == 0:
            return None, 0.0
        audio = audio[: LANGUAGE_DETECT_MAX_SECONDS * 16000]

        params = inspect.signature(model.detect_language).parameters
        attempts: List[Dict[str, Any]] = []
        vad_kwargs: Dict[str, Any] = {}
        if "vad_filter" in params:
            vad_kwargs["vad_filter"] = True
        if "language_detection_segments" in params:
            vad_kwargs["language_detection_segments"] = 3
        if vad_kwargs:
            attempts.append(vad_kwargs)
        attempts.append({})  # plain first-30-seconds detection (also the fallback when VAD finds no speech)

        last_exc: Optional[BaseException] = None
        for kwargs in attempts:
            try:
                result = model.detect_language(audio, **kwargs)
                lang, prob = str(result[0]), float(result[1])
                return (lang or None), prob
            except CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
        if on_log and last_exc is not None:
            on_log(f"[语言检测] 检测失败：{last_exc}")
        return None, 0.0
    except CancelledError:
        raise
    except Exception as exc:
        MODEL_CACHE.clear()
        if on_log:
            on_log(f"[语言检测] 无法加载语言检测模型 {model_name}：{exc}")
        return None, 0.0


def transcribe(
    wav_path: Path,
    opts: TranscribeOptions,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> Tuple[List[Segment], Dict[str, Any]]:
    """Transcribe `wav_path` with automatic GPU OOM/DLL fallback to CPU (SenseVoiceSmall runs on CPU only)."""
    if not wav_path.exists():
        raise TranscribeError(f"待转录 WAV 文件不存在：{wav_path}")

    if model_manager.get_model_engine(opts.model) == model_manager.ENGINE_SENSEVOICE:
        from app.core import sensevoice_engine

        return sensevoice_engine.transcribe_sensevoice(
            wav_path=wav_path,
            opts=opts,
            on_progress=on_progress,
            on_log=on_log,
            token=token,
            paths=paths,
        )

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

    err_type = type(last_exc).__name__ if last_exc else "Error"
    raise TranscribeError(
        f"转录失败 ({err_type}): {last_exc}",
        detail=repr(last_exc),
    )
