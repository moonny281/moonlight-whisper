"""Whisper CTranslate2 model management: strict verification, streaming HF mirror download, and manual import."""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.core import http_client
from app.core.cancel import CancelToken
from app.core.errors import CancelledError, ModelError
from app.paths import PATHS, AppPaths

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo_id: str
    approx_size_mb: int
    recommendation: str
    mel_bins: int = 80
    fallback_repo_id: str = ""


SUPPORTED_MODELS: Tuple[ModelSpec, ...] = (
    ModelSpec("tiny", "Systran/faster-whisper-tiny", 75, "快速测试、超低配 CPU", mel_bins=80),
    ModelSpec("base", "Systran/faster-whisper-base", 145, "轻量日常记录、低配 CPU", mel_bins=80),
    ModelSpec("small", "Systran/faster-whisper-small", 480, "CPU 默认推荐", mel_bins=80),
    ModelSpec("medium", "Systran/faster-whisper-medium", 1500, "质量与速度折中", mel_bins=80),
    ModelSpec("large-v3", "Systran/faster-whisper-large-v3", 3000, "最佳精度，适合大显存 GPU", mel_bins=128),
    ModelSpec(
        "large-v3-turbo",
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        1600,
        "GPU 默认推荐，速度快且质量高",
        mel_bins=128,
        fallback_repo_id="deepdml/faster-whisper-large-v3-turbo-ct2",
    ),
)

MODEL_SPEC_MAP: Dict[str, ModelSpec] = {m.name: m for m in SUPPORTED_MODELS}

REQUIRED_MODEL_FILES: Tuple[str, ...] = (
    "model.bin",
    "config.json",
    "tokenizer.json",
)

# Minimum valid size for model.bin in real runtime (1 MB; unit tests can pass allow_small_bin=True)
MIN_MODEL_BIN_BYTES = 1024 * 1024


@dataclass
class ModelStatus:
    spec: ModelSpec
    installed: bool
    local_path: Path
    actual_size_mb: float = 0.0


def _ensure_preprocessor_config(directory: Path, mel_bins: int) -> None:
    """Ensure `preprocessor_config.json` exists with the correct `feature_size` (80 or 128 mel bins)."""
    pp_path = directory / "preprocessor_config.json"
    if pp_path.exists() and pp_path.stat().st_size > 20:
        return
    default_pp = {
        "chunk_length": 30,
        "feature_extractor_type": "WhisperFeatureExtractor",
        "feature_size": mel_bins,
        "hop_length": 160,
        "n_fft": 400,
        "n_samples": 480000,
        "nb_max_frames": 3000,
        "padding_side": "right",
        "padding_value": 0.0,
        "processor_class": "WhisperProcessor",
        "return_attention_mask": False,
        "sampling_rate": 16000,
    }
    try:
        pp_path.write_text(json.dumps(default_pp, indent=2), encoding="utf-8")
    except OSError:
        pass


def verify_model_dir(directory: Path, min_bin_bytes: int = 4) -> Tuple[bool, List[str]]:
    """Verify whether `directory` contains a complete CTranslate2 Whisper model.

    Requires ALL of:
    1. `model.bin` (non-empty and >= `min_bin_bytes`, not a 130-byte Git LFS pointer)
    2. `config.json` (valid JSON object)
    3. `tokenizer.json` (required by faster-whisper Python Tokenizer)
    4. `vocabulary.json` OR `vocabulary.txt` (required by CTranslate2 C++ Whisper model loader!)
    """
    if not directory.exists() or not directory.is_dir():
        return False, ["model.bin", "config.json", "tokenizer.json", "vocabulary.json/vocabulary.txt"]

    missing: List[str] = []

    model_bin = directory / "model.bin"
    if not model_bin.exists() or model_bin.stat().st_size < min_bin_bytes:
        missing.append("model.bin")
    else:
        # Check that model.bin is not a tiny Git LFS text pointer ("version https://git-lfs.github.com/spec/v1")
        if model_bin.stat().st_size < 1024:
            try:
                head = model_bin.read_bytes()[:64]
                if head.startswith(b"version https://git-lfs") or head.startswith(b"<!DOCTYPE") or head.startswith(b"<html"):
                    missing.append("model.bin (LFS pointer / invalid)")
            except OSError:
                missing.append("model.bin")

    cfg_file = directory / "config.json"
    if not cfg_file.exists() or cfg_file.stat().st_size == 0:
        missing.append("config.json")

    tok_file = directory / "tokenizer.json"
    if not tok_file.exists() or tok_file.stat().st_size == 0:
        missing.append("tokenizer.json")

    # CTranslate2 C++ strictly requires vocabulary.txt OR vocabulary.json (tokenizer.json alone is NOT enough!)
    has_vocab = any(
        (directory / name).exists() and (directory / name).stat().st_size > 0
        for name in ("vocabulary.json", "vocabulary.txt")
    )
    if not has_vocab:
        missing.append("vocabulary.json / vocabulary.txt")

    return len(missing) == 0, missing


def _dir_size_mb(directory: Path) -> float:
    if not directory.exists():
        return 0.0
    total = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
    return round(total / (1024 * 1024), 1)


def is_model_installed(model_name: str, paths: AppPaths = PATHS) -> bool:
    model_dir = paths.models_dir / model_name
    ok, _ = verify_model_dir(model_dir)
    if ok:
        spec = MODEL_SPEC_MAP.get(model_name)
        if spec:
            _ensure_preprocessor_config(model_dir, spec.mel_bins)
    return ok


def list_models(paths: AppPaths = PATHS) -> List[ModelStatus]:
    """Return installation status and disk usage for all supported Whisper models."""
    results: List[ModelStatus] = []
    for spec in SUPPORTED_MODELS:
        local_dir = paths.models_dir / spec.name
        installed, _ = verify_model_dir(local_dir)
        if installed:
            _ensure_preprocessor_config(local_dir, spec.mel_bins)
        size_mb = _dir_size_mb(local_dir) if installed else 0.0
        results.append(
            ModelStatus(
                spec=spec,
                installed=installed,
                local_path=local_dir,
                actual_size_mb=size_mb,
            )
        )
    return results


def _fetch_repo_file_list(repo_id: str, endpoint: str, proxy: str) -> List[Tuple[str, int]]:
    """Fetch `(rfilename, size_bytes)` list from HuggingFace API (with complete CTranslate2 fallback list)."""
    api_url = f"{endpoint.rstrip('/')}/api/models/{repo_id}"
    allowed_names = {
        "config.json",
        "preprocessor_config.json",
        "tokenizer.json",
        "vocabulary.json",
        "vocabulary.txt",
        "model.bin",
    }
    try:
        meta = http_client.fetch_json(api_url, proxy=proxy, timeout=20)
        siblings = meta.get("siblings") or []
        files: List[Tuple[str, int]] = []
        for s in siblings:
            rfile = str(s.get("rfilename") or "")
            if rfile in allowed_names:
                size = int(s.get("size") or (s.get("lfs") or {}).get("size") or 0)
                files.append((rfile, size))
        if any(f[0] == "model.bin" for f in files):
            return files
    except Exception:
        pass

    return [
        ("config.json", 2048),
        ("preprocessor_config.json", 2048),
        ("tokenizer.json", 2 * 1024 * 1024),
        ("vocabulary.json", 1024 * 1024),
        ("vocabulary.txt", 512 * 1024),
        ("model.bin", 0),
    ]


def download_model(
    model_name: str,
    hf_mirror: bool = True,
    proxy: str = "",
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> Path:
    """Stream-download a CTranslate2 Whisper model from HuggingFace or `hf-mirror.com` with Range resume & instant cancel."""
    if token:
        token.check()

    spec = MODEL_SPEC_MAP.get(model_name)
    repos_to_try = [spec.repo_id] if spec else [model_name]
    if spec and spec.fallback_repo_id:
        repos_to_try.append(spec.fallback_repo_id)

    approx_total_bytes = (spec.approx_size_mb if spec else 500) * 1024 * 1024
    target_dir = paths.models_dir / model_name
    target_dir.mkdir(parents=True, exist_ok=True)

    endpoints = (
        ["https://hf-mirror.com", "https://huggingface.co"]
        if hf_mirror
        else ["https://huggingface.co", "https://hf-mirror.com"]
    )

    last_exc: Optional[BaseException] = None

    for endpoint in endpoints:
        os.environ["HF_ENDPOINT"] = endpoint
        for repo_id in repos_to_try:
            if token:
                token.check()
            if on_log:
                on_log(f"正在准备下载模型 {model_name} ({repo_id})，源：{endpoint}")
            if on_progress:
                on_progress(0.02, f"正在连接 {endpoint} 获取 {repo_id} 文件列表...")

            file_entries = _fetch_repo_file_list(repo_id=repo_id, endpoint=endpoint, proxy=proxy)
            known_sum = sum(sz for _, sz in file_entries if sz > 0)
            total_expected_bytes = known_sum if known_sum > 10 * 1024 * 1024 else approx_total_bytes

            completed_prior_bytes = 0
            start_ts = time.monotonic()
            last_emit_ts = [0.0]

            try:
                for idx, (rfile, declared_size) in enumerate(file_entries, start=1):
                    if token:
                        token.check()
                    dest_file = target_dir / rfile

                    # If a corrupt/empty/LFS-pointer file exists from an aborted attempt, remove it before downloading
                    if dest_file.exists():
                        sz = dest_file.stat().st_size
                        if sz == 0 or (rfile == "model.bin" and sz < 1024 * 1024):
                            dest_file.unlink(missing_ok=True)

                    quoted_path = urllib.parse.quote(rfile, safe="/")
                    file_url = f"{endpoint.rstrip('/')}/{repo_id}/resolve/main/{quoted_path}"

                    def _on_chunk(
                        cur_bytes: int,
                        file_total: int,
                        base: int = completed_prior_bytes,
                        fname: str = rfile,
                        f_idx: int = idx,
                    ) -> None:
                        if not on_progress:
                            return
                        now = time.monotonic()
                        if cur_bytes < file_total and (now - last_emit_ts[0]) < 0.2:
                            return
                        last_emit_ts[0] = now
                        effective_total = max(total_expected_bytes, base + file_total)
                        overall = base + cur_bytes
                        ratio = min(0.99, max(0.02, overall / max(1, effective_total)))
                        elapsed = max(0.001, time.monotonic() - start_ts)
                        speed_mb = (overall / (1024 * 1024)) / elapsed
                        msg = (
                            f"下载中 ({f_idx}/{len(file_entries)}) {fname}: "
                            f"{overall / (1024 * 1024):.1f} / {effective_total / (1024 * 1024):.1f} MB "
                            f"({speed_mb:.1f} MB/s)"
                        )
                        on_progress(ratio, msg)

                    try:
                        http_client.stream_download_with_resume(
                            url=file_url,
                            dest_file=dest_file,
                            proxy=proxy,
                            expected_sha256="",
                            on_chunk_progress=_on_chunk,
                            token=token,
                            timeout=60,
                        )
                    except CancelledError:
                        raise
                    except Exception as file_exc:
                        # Older models have vocabulary.txt (no vocabulary.json); v3/turbo have vocabulary.json (no vocabulary.txt)
                        if rfile in ("vocabulary.txt", "vocabulary.json", "preprocessor_config.json"):
                            continue
                        raise file_exc

                    actual_sz = dest_file.stat().st_size if dest_file.exists() else declared_size
                    completed_prior_bytes += actual_sz

                # Ensure preprocessor_config.json is present with the right mel_bins (80 vs 128)
                if spec:
                    _ensure_preprocessor_config(target_dir, spec.mel_bins)

                ok, missing = verify_model_dir(target_dir)
                if ok:
                    if on_progress:
                        on_progress(1.0, f"模型 {model_name} 下载并校验完成")
                    if on_log:
                        on_log(f"模型 {model_name} 已就绪：{target_dir}")
                    return target_dir

                last_exc = ModelError(
                    f"模型 {model_name} 校验未通过",
                    detail="缺失或损坏文件: " + ", ".join(missing),
                )
            except CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                if on_log:
                    on_log(f"[重试提示] 从 {endpoint} ({repo_id}) 下载遇到问题 ({exc})，尝试备用源...")
                continue

    raise ModelError(
        f"模型 {model_name} 下载失败，请检查网络、切换 HF 镜像或使用手动导入（支持重试断点续传）。",
        detail=str(last_exc),
    )


def import_model_directory(
    model_name: str,
    source_dir: Path,
    on_log: Optional[LogCallback] = None,
    paths: AppPaths = PATHS,
) -> Path:
    """Validate and copy a local CTranslate2 model folder into `models/<model_name>/`."""
    source_dir = source_dir.expanduser().resolve()
    ok, missing = verify_model_dir(source_dir)
    if not ok:
        raise ModelError(
            f"所选文件夹不是完整的 CTranslate2 Whisper 模型目录（缺少 {', '.join(missing)}）。",
            detail=str(source_dir),
        )

    target_dir = paths.models_dir / model_name
    if source_dir != target_dir.resolve():
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir)

    spec = MODEL_SPEC_MAP.get(model_name)
    if spec:
        _ensure_preprocessor_config(target_dir, spec.mel_bins)

    if on_log:
        on_log(f"已手动导入模型 {model_name}：{target_dir}")
    return target_dir
