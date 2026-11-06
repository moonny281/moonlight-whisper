"""SenseVoiceSmall offline transcription engine.

Pure `onnxruntime` + `kaldi-native-fbank` implementation of the FunASR SenseVoiceSmall CTC model
(sherpa-onnx ONNX export, `tokens.txt` + `model.int8.onnx` on CPU / fp32 `model.onnx` on CUDA). It deliberately avoids PyTorch / FunASR
(and the sherpa-onnx wheel, which ships its own `onnxruntime.dll` that collides with the one already
used by faster-whisper's Silero VAD on Windows).

Pipeline per audio file:
    16 kHz WAV -> Silero VAD (reused from faster-whisper) -> <=15 s chunks
    -> Kaldi fbank(80) -> LFR(7, 6) -> CMVN -> ONNX CTC logits -> greedy decode -> text
"""

from __future__ import annotations

import gc
import os
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from app.core import model_manager
from app.core import transcriber as _tr
from app.core.cancel import CancelToken
from app.core.errors import CancelledError, TranscribeError
from app.core.transcriber import Segment, TranscribeOptions
from app.paths import PATHS, AppPaths

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]

SAMPLE_RATE = 16000
MAX_CHUNK_SEC = 15.0             # upper bound of one VAD chunk / fixed window (keeps subtitle lines short)
MIN_CHUNK_SAMPLES = 1600         # < 0.1 s of audio cannot yield a meaningful frame stack
SUPPORTED_LANGUAGES = ("zh", "en", "ja", "ko", "yue")
LANGUAGE_TAGS = frozenset(SUPPORTED_LANGUAGES) | {"nospeech"}

# Fallback ids of the FunASR `lid_dict` / `textnorm_dict` (normally read from the ONNX metadata).
_DEFAULT_LANG_IDS = {"auto": 0, "zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12, "nospeech": 13}
_DEFAULT_WITH_ITN = 14
_DEFAULT_WITHOUT_ITN = 15

_TAG_RE = re.compile(r"<\|([^|>]*)\|>")


# --------------------------------------------------------------------------------------
# Model container (ONNX session + tokens + frontend constants)
# --------------------------------------------------------------------------------------
def load_tokens(tokens_path: Path) -> List[str]:
    """Parse sherpa-onnx `tokens.txt` (`<symbol> <id>` per line) into an id-indexed list."""
    raw = tokens_path.read_text(encoding="utf-8")
    table: Dict[int, str] = {}
    for line in raw.split("\n"):
        line = line.rstrip("\r")
        if not line:
            continue
        sym, _, idx = line.rpartition(" ")
        if not sym or not idx.strip().lstrip("-").isdigit():
            continue
        table[int(idx)] = sym
    if not table:
        raise ValueError("tokens.txt 内容为空或格式不正确")
    out = [""] * (max(table) + 1)
    for i, sym in table.items():
        out[i] = sym
    return out


def _parse_float_vector(text: str) -> np.ndarray:
    return np.asarray([float(x) for x in text.split(",") if x.strip()], dtype=np.float32)


class SenseVoiceModel:
    """Loaded SenseVoice ONNX session together with its tokenizer table and frontend parameters."""

    def __init__(self, session: Any, tokens: List[str], weights_name: str, device: str = "cpu") -> None:
        self.session = session
        self.tokens = tokens
        self.weights_name = weights_name
        self.is_int8 = "int8" in weights_name.lower()
        self.device = device

        meta: Dict[str, str] = {}
        try:
            meta = dict(session.get_modelmeta().custom_metadata_map or {})
        except Exception:
            meta = {}

        try:
            self.neg_mean = _parse_float_vector(meta["neg_mean"])
            self.inv_stddev = _parse_float_vector(meta["inv_stddev"])
        except KeyError as exc:
            raise ValueError(f"模型缺少 CMVN 元数据 {exc}，不是有效的 SenseVoiceSmall ONNX 导出文件") from exc

        self.lfr_m = int(meta.get("lfr_window_size", 7))
        self.lfr_n = int(meta.get("lfr_window_shift", 6))
        self.feat_dim = 80 * self.lfr_m
        if self.neg_mean.size != self.feat_dim or self.inv_stddev.size != self.feat_dim:
            raise ValueError(
                f"CMVN 维度 ({self.neg_mean.size}) 与特征维度 ({self.feat_dim}) 不一致"
            )

        self.lang_ids: Dict[str, int] = {}
        for name, default in _DEFAULT_LANG_IDS.items():
            self.lang_ids[name] = int(meta.get(f"lang_{name}", default))
        self.with_itn_id = int(meta.get("with_itn", _DEFAULT_WITH_ITN))
        self.without_itn_id = int(meta.get("without_itn", _DEFAULT_WITHOUT_ITN))

        # `normalize_samples == 0` means the model expects int16-scale samples.
        self.sample_scale = 1.0 if int(meta.get("normalize_samples", 0)) else 32768.0

    # -- frontend ------------------------------------------------------------------
    def extract_features(self, samples: np.ndarray) -> np.ndarray:
        """Kaldi fbank(80, hamming, no dither) -> LFR(m, n) -> CMVN. Returns `(T_lfr, 560)` float32."""
        try:
            import kaldi_native_fbank as knf  # type: ignore
        except ImportError as exc:
            raise TranscribeError(
                "缺少 kaldi-native-fbank 组件，无法使用 SenseVoiceSmall，请执行 pip install -r requirements.txt。",
                detail=repr(exc),
            ) from exc

        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq = SAMPLE_RATE
        opts.frame_opts.dither = 0.0
        opts.frame_opts.snip_edges = True
        opts.frame_opts.window_type = "hamming"
        opts.frame_opts.frame_shift_ms = 10.0
        opts.frame_opts.frame_length_ms = 25.0
        opts.mel_opts.num_bins = 80

        fbank = knf.OnlineFbank(opts)
        fbank.accept_waveform(SAMPLE_RATE, (samples * self.sample_scale).astype(np.float32).tolist())
        fbank.input_finished()

        n_ready = fbank.num_frames_ready
        if n_ready <= 0:
            return np.zeros((0, self.feat_dim), dtype=np.float32)
        frames = np.stack([np.asarray(fbank.get_frame(i), dtype=np.float32) for i in range(n_ready)])
        return self._apply_lfr_cmvn(frames)

    def _apply_lfr_cmvn(self, frames: np.ndarray) -> np.ndarray:
        m, n = self.lfr_m, self.lfr_n
        t_in = frames.shape[0]
        left_pad = (m - 1) // 2
        if left_pad > 0:
            frames = np.concatenate([np.repeat(frames[:1], left_pad, axis=0), frames], axis=0)
        t_padded = frames.shape[0]
        t_lfr = int(np.ceil(t_in / n))
        # Right-pad with the last frame so the final window is complete (FunASR `apply_lfr` semantics).
        need = (t_lfr - 1) * n + m
        if need > t_padded:
            frames = np.concatenate([frames, np.repeat(frames[-1:], need - t_padded, axis=0)], axis=0)
        idx = np.arange(t_lfr)[:, None] * n + np.arange(m)[None, :]
        stacked = frames[idx].reshape(t_lfr, m * 80)
        return ((stacked + self.neg_mean) * self.inv_stddev).astype(np.float32)

    # -- decoding -------------------------------------------------------------------
    def decode_logits(self, logits: np.ndarray) -> Tuple[str, str]:
        """CTC greedy decode `(T, V)` logits. Returns `(text, language_tag)`; tags are stripped from text."""
        ids = logits.argmax(axis=-1)
        if ids.size == 0:
            return "", ""
        keep = np.empty(ids.shape, dtype=bool)
        keep[0] = True
        keep[1:] = ids[1:] != ids[:-1]
        keep &= ids != 0           # blank / <unk>
        pieces: List[str] = []
        lang = ""
        for tid in ids[keep].tolist():
            if tid >= len(self.tokens):
                continue
            tok = self.tokens[tid]
            tag = _TAG_RE.fullmatch(tok)
            if tag:
                name = tag.group(1)
                if not lang and name in LANGUAGE_TAGS:
                    lang = name
                continue
            pieces.append(tok)
        text = "".join(pieces).replace("\u2581", " ")
        text = _TAG_RE.sub("", text).strip()
        return text, lang

    def recognize(self, samples: np.ndarray, language: str, use_itn: bool = True) -> Tuple[str, str]:
        feats = self.extract_features(samples)
        if feats.shape[0] == 0:
            return "", ""
        lang_id = self.lang_ids.get(language, self.lang_ids["auto"])
        norm_id = self.with_itn_id if use_itn else self.without_itn_id
        outputs = self.session.run(
            None,
            {
                "x": feats[None, :, :],
                "x_length": np.asarray([feats.shape[0]], dtype=np.int32),
                "language": np.asarray([lang_id], dtype=np.int32),
                "text_norm": np.asarray([norm_id], dtype=np.int32),
            },
        )
        return self.decode_logits(np.asarray(outputs[0])[0])


# --------------------------------------------------------------------------------------
# Model cache (at most one engine resident in memory, shared policy with the Whisper cache)
# --------------------------------------------------------------------------------------
def _resolve_threads(cpu_threads: int) -> int:
    if cpu_threads and cpu_threads > 0:
        return int(cpu_threads)
    return max(1, min(8, os.cpu_count() or 4))


def _make_session(weights: Path, threads: int, device: str) -> Any:
    """Create an ONNX Runtime session on `device` ("cpu" | "cuda"); raises if CUDA cannot really be used."""
    import onnxruntime as ort  # type: ignore

    so = ort.SessionOptions()
    so.log_severity_level = 4
    if device == "cuda":
        providers: List[Any] = [
            (
                "CUDAExecutionProvider",
                {
                    "device_id": 0,
                    # Chunk lengths vary, so avoid exhaustive cuDNN autotuning per new shape and over-sized arenas.
                    "cudnn_conv_algo_search": "HEURISTIC",
                    "arena_extend_strategy": "kSameAsRequested",
                },
            ),
            "CPUExecutionProvider",
        ]
    else:
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        providers = ["CPUExecutionProvider"]

    source: Any = str(weights)
    if not source.isascii():
        # ONNX Runtime's C++ loader can fail on non-ASCII Windows paths: feed the bytes directly.
        source = weights.read_bytes()
    try:
        session = ort.InferenceSession(source, sess_options=so, providers=providers)
    except Exception:
        if isinstance(source, bytes):
            raise
        session = ort.InferenceSession(weights.read_bytes(), sess_options=so, providers=providers)

    if device == "cuda" and "CUDAExecutionProvider" not in session.get_providers():
        raise RuntimeError("CUDAExecutionProvider 未能加载（缺少 CUDA / cuDNN 运行库或驱动不兼容）")
    return session


class _SenseVoiceCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: Optional[Tuple[str, int, str]] = None
        self._model: Optional[SenseVoiceModel] = None

    def get_or_create(
        self,
        model_dir: Path,
        cpu_threads: int,
        device: str = "cpu",
        on_log: Optional[LogCallback] = None,
    ) -> SenseVoiceModel:
        """Load SenseVoiceSmall on `device`. A CUDA request that cannot be honoured falls back to CPU (logged)."""
        # Release a resident Whisper model first (outside our lock: the Whisper cache has its own lock).
        _tr.MODEL_CACHE.clear()

        threads = _resolve_threads(cpu_threads)
        key = (str(model_dir.resolve()), threads, device)
        with self._lock:
            if self._model is not None and self._key == key:
                if on_log:
                    on_log(f"复用已加载模型：SenseVoiceSmall ({self._model.weights_name}, {self._model.device})")
                return self._model

            if self._model is not None:
                if on_log:
                    on_log("释放旧模型缓存...")
                self._model = None
                self._key = None
                gc.collect()

            tokens_file = model_dir / "tokens.txt"
            int8_or_any = model_manager.find_sensevoice_weights(model_dir)
            if int8_or_any is None or not tokens_file.exists():
                raise TranscribeError(
                    "SenseVoiceSmall 模型文件缺失 (model.int8.onnx / tokens.txt)，请在“模型管理”中重新下载。",
                    detail=str(model_dir),
                )

            model: Optional[SenseVoiceModel] = None
            if device == "cuda":
                model = self._try_load_cuda(model_dir, tokens_file, threads, on_log)

            if model is None:
                weights = int8_or_any
                if on_log:
                    on_log(f"正在加载模型：SenseVoiceSmall ({weights.name}, CPU, {threads} 线程)...")
                try:
                    session = _make_session(weights, threads, "cpu")
                    model = SenseVoiceModel(session, load_tokens(tokens_file), weights.name, device="cpu")
                except TranscribeError:
                    raise
                except Exception as exc:
                    raise TranscribeError(
                        f"SenseVoiceSmall 模型文件 ({weights.name} / tokens.txt) 加载失败，文件可能已损坏。",
                        detail=repr(exc),
                    ) from exc

            self._model = model
            self._key = key
            if on_log:
                on_log(f"模型加载成功：SenseVoiceSmall ({model.device})")
            return model

    @staticmethod
    def _try_load_cuda(
        model_dir: Path,
        tokens_file: Path,
        threads: int,
        on_log: Optional[LogCallback],
    ) -> Optional[SenseVoiceModel]:
        """Return a CUDA-backed model, or `None` (after logging why) so the caller falls back to CPU."""

        def fallback(reason: str) -> None:
            if on_log:
                on_log(f"[GPU 回退] SenseVoiceSmall 无法使用 GPU：{reason}，自动改用 CPU 推理。")

        if not _gpu_runtime_has_cuda_provider():
            fallback("当前 onnxruntime 为 CPU 版本（需要安装 onnxruntime-gpu）")
            return None
        fp32 = model_manager.find_sensevoice_fp32(model_dir)
        if fp32 is None:
            fallback("缺少 fp32 权重 model.onnx（GPU 推理需要，约 940MB）")
            return None

        if on_log:
            on_log(f"正在加载模型：SenseVoiceSmall ({fp32.name}, GPU CUDA)...")
        try:
            session = _make_session(fp32, threads, "cuda")
            model = SenseVoiceModel(session, load_tokens(tokens_file), fp32.name, device="cuda")
            # Warm-up: cuDNN / cuBLAS problems only surface on the first real run.
            model.recognize(np.zeros(SAMPLE_RATE, dtype=np.float32), "auto")
            return model
        except Exception as exc:
            fallback(f"{type(exc).__name__}: {str(exc)[:200]}")
            return None

    def clear(self) -> None:
        with self._lock:
            self._model = None
            self._key = None
            gc.collect()


def _gpu_runtime_has_cuda_provider() -> bool:
    try:
        import onnxruntime as ort  # type: ignore

        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


SENSEVOICE_CACHE = _SenseVoiceCache()


# --------------------------------------------------------------------------------------
# Audio chunking
# --------------------------------------------------------------------------------------
def fixed_windows(n_samples: int, window_sec: float = MAX_CHUNK_SEC) -> List[Tuple[int, int]]:
    """Cut `[0, n_samples)` into consecutive windows; a tiny tail (<1 s) is merged into the previous one."""
    win = int(window_sec * SAMPLE_RATE)
    if n_samples <= 0:
        return []
    spans = [(s, min(s + win, n_samples)) for s in range(0, n_samples, win)]
    if len(spans) > 1 and spans[-1][1] - spans[-1][0] < SAMPLE_RATE:
        last = spans.pop()
        prev = spans.pop()
        spans.append((prev[0], last[1]))
    return spans


def vad_chunks(
    audio: np.ndarray,
    min_silence_duration_ms: int,
    on_log: Optional[LogCallback] = None,
) -> List[Tuple[int, int]]:
    """Speech spans (in samples) from faster-whisper's bundled Silero VAD, each at most `MAX_CHUNK_SEC` long."""
    _tr._patch_runtime_compat()
    from faster_whisper.vad import VadOptions, get_speech_timestamps  # type: ignore

    options = VadOptions(
        min_silence_duration_ms=min_silence_duration_ms,
        min_speech_duration_ms=250,
        max_speech_duration_s=MAX_CHUNK_SEC,
        speech_pad_ms=200,
    )
    stamps = get_speech_timestamps(audio, options)
    n = int(audio.shape[0])
    spans: List[Tuple[int, int]] = []
    for st in stamps:
        s = max(0, int(st["start"]))
        e = min(n, int(st["end"]))
        if e - s >= MIN_CHUNK_SAMPLES:
            spans.append((s, e))
    return spans


def _plan_chunks(
    audio: np.ndarray,
    opts: TranscribeOptions,
    on_log: Optional[LogCallback],
) -> List[Tuple[int, int]]:
    n = int(audio.shape[0])
    duration = n / SAMPLE_RATE
    if opts.vad:
        try:
            spans = vad_chunks(audio, opts.min_silence_duration_ms, on_log)
            if spans:
                return spans
            if duration > 1.5:
                if on_log:
                    on_log("[VAD 回退] 开启 VAD 后未检出人声片段，自动切换为定长分段模式重试...")
            else:
                return []
        except Exception as vad_exc:
            if on_log:
                on_log(f"[VAD 回退] VAD 语音活动检测异常 ({vad_exc})，自动切换为定长分段模式继续转录...")
    return fixed_windows(n)


# --------------------------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------------------------
def ensure_gpu_assets(
    model_name: str,
    hf_mirror: bool,
    pypi_mirror: str,
    proxy: str,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
    models_dir: Optional[Path] = None,
) -> bool:
    """Make sure everything SenseVoiceSmall needs on CUDA is present; `False` means "use the CPU instead".

    Needs: a CUDA-enabled `onnxruntime`, the cudart / cuFFT DLLs next to the cuBLAS / cuDNN ones, and the fp32
    `model.onnx` weights. Missing DLLs / weights are downloaded on demand; any failure only costs the GPU speed-up.
    """
    from app.core import gpu_runtime  # local import: avoids a hard dependency cycle at import time
    from app.core.errors import GpuError, ModelError

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    if not gpu_runtime.onnxruntime_has_cuda():
        log("[GPU 回退] 当前安装的 onnxruntime 不含 CUDA 支持（需要 onnxruntime-gpu），SenseVoiceSmall 将使用 CPU。")
        return False

    try:
        missing = gpu_runtime.check_onnx_extra_files(paths.gpu_runtime_dir)
        if missing:
            if on_stage:
                on_stage("下载 GPU 补充运行库")
            log(f"SenseVoiceSmall GPU 推理需要补充 CUDA 运行库 ({', '.join(missing)})，开始下载...")
            gpu_runtime.install_onnx_extras(
                mirror=pypi_mirror, proxy=proxy, on_progress=on_progress, on_log=on_log, token=token, paths=paths
            )
        gpu_runtime.register_dll_directory(paths.gpu_runtime_dir)

        model_dir = (models_dir or paths.models_dir) / model_name
        if model_manager.find_sensevoice_fp32(model_dir) is None:
            if on_stage:
                on_stage("下载 SenseVoice GPU 权重")
            log(f"SenseVoiceSmall GPU 推理需要 fp32 权重 (约 {model_manager.SENSEVOICE_FP32_APPROX_MB}MB)，开始下载...")
            model_manager.download_sensevoice_fp32(
                hf_mirror=hf_mirror, proxy=proxy, on_progress=on_progress, on_log=on_log, token=token, paths=paths
            )
        return True
    except CancelledError:
        raise
    except (GpuError, ModelError) as exc:
        log(f"[GPU 回退] 准备 SenseVoiceSmall GPU 文件失败 ({exc.user_message})，将使用 CPU。")
        return False
    except Exception as exc:
        log(f"[GPU 回退] 准备 SenseVoiceSmall GPU 文件失败 ({exc})，将使用 CPU。")
        return False


def _decode_all(
    model: SenseVoiceModel,
    audio: np.ndarray,
    spans: List[Tuple[int, int]],
    language: str,
    opts: TranscribeOptions,
    on_progress: Optional[ProgressCallback],
    token: Optional[CancelToken],
) -> Tuple[List[Segment], Counter]:
    duration = audio.shape[0] / SAMPLE_RATE
    segments: List[Segment] = []
    lang_votes: Counter = Counter()
    for start, end in spans:
        if token:
            token.check()
        text, lang_tag = model.recognize(audio[start:end], language=language, use_itn=True)
        if lang_tag and lang_tag != "nospeech":
            lang_votes[lang_tag] += end - start
        end_sec = end / SAMPLE_RATE
        if text:
            # Traditional->Simplified only for Chinese-script output; it would corrupt Japanese kanji.
            if opts.to_simplified and (lang_tag in ("zh", "yue") or (not lang_tag and language in ("zh", "yue"))):
                text = _tr._convert_to_simplified(text)
            segments.append(Segment(start=start / SAMPLE_RATE, end=end_sec, text=text))
        if on_progress and duration > 0:
            ratio = min(1.0, max(0.0, end_sec / duration))
            on_progress(ratio, f"[{end_sec:.1f}s / {duration:.1f}s] {text[:36]}")
    return segments, lang_votes


def transcribe_sensevoice(
    wav_path: Path,
    opts: TranscribeOptions,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> Tuple[List[Segment], Dict[str, Any]]:
    """Transcribe a 16 kHz WAV with SenseVoiceSmall (CPU int8 or CUDA fp32). Same contract as `transcriber.transcribe`."""
    if token:
        token.check()

    models_dir = opts.models_dir or paths.models_dir
    model_dir = models_dir / opts.model
    ok, missing = model_manager.verify_model_dir(model_dir, engine=model_manager.ENGINE_SENSEVOICE)
    if not ok:
        raise TranscribeError(
            f"SenseVoiceSmall 模型文件缺失或损坏 (model.int8.onnx / tokens.txt)：{', '.join(missing)}",
            detail=str(model_dir),
        )

    devices = ["cuda", "cpu"] if opts.device == "cuda" else ["cpu"]
    segments: List[Segment] = []
    lang_votes: Counter = Counter()
    model: Optional[SenseVoiceModel] = None
    language = "auto"
    duration = 0.0

    for attempt, device in enumerate(devices):
        try:
            model = SENSEVOICE_CACHE.get_or_create(model_dir, opts.cpu_threads, device=device, on_log=on_log)

            if token:
                token.check()

            audio = _tr.load_wav_as_float32_array(wav_path)
            if audio is None or audio.size == 0:
                raise TranscribeError(f"无法读取待转录 WAV 文件：{wav_path}")
            duration = audio.shape[0] / SAMPLE_RATE

            language = opts.language if opts.language in SUPPORTED_LANGUAGES else "auto"
            if attempt == 0 and opts.language and language == "auto" and on_log:
                on_log(f"[提示] SenseVoiceSmall 仅支持 zh/en/ja/ko/yue，语言“{opts.language}”将改用自动检测。")

            spans = _plan_chunks(audio, opts, on_log)
            if on_log:
                vad_label = "开启" if opts.vad else "关闭"
                on_log(
                    f"SenseVoiceSmall ({model.device})：识别语言={language}，VAD={vad_label}，总时长 {duration:.1f}s，"
                    f"共 {len(spans)} 个语音片段，开始逐段解码..."
                )

            segments, lang_votes = _decode_all(model, audio, spans, language, opts, on_progress, token)
            break
        except (CancelledError, TranscribeError):
            raise
        except Exception as exc:
            SENSEVOICE_CACHE.clear()
            if device == "cuda" and attempt + 1 < len(devices):
                if on_log:
                    on_log(f"[GPU 回退] GPU 推理出错 ({type(exc).__name__}: {str(exc)[:160]})，自动切换为 CPU 重新转录...")
                continue
            raise TranscribeError(f"转录失败 ({type(exc).__name__}): {exc}", detail=repr(exc)) from exc

    assert model is not None
    if lang_votes:
        detected = lang_votes.most_common(1)[0][0]
    else:
        detected = language if language != "auto" else "unknown"

    meta: Dict[str, Any] = {
        "model": opts.model,
        "device": model.device,
        "compute_type": "float32" if model.device == "cuda" or not model.is_int8 else "int8",
        "language": detected,
        # SenseVoice does not expose a language-ID probability.
        "language_probability": None,
        "duration": duration,
        "fallback_occurred": opts.device == "cuda" and model.device != "cuda",
    }
    return segments, meta
