"""Whisper CTranslate2 model management: status inspection, streaming HF mirror download, and manual import."""

from __future__ import annotations

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


SUPPORTED_MODELS: Tuple[ModelSpec, ...] = (
    ModelSpec("tiny", "Systran/faster-whisper-tiny", 75, "快速测试、超低配 CPU"),
    ModelSpec("base", "Systran/faster-whisper-base", 145, "轻量日常记录、低配 CPU"),
    ModelSpec("small", "Systran/faster-whisper-small", 480, "CPU 默认推荐"),
    ModelSpec("medium", "Systran/faster-whisper-medium", 1500, "质量与速度折中"),
    ModelSpec("large-v3", "Systran/faster-whisper-large-v3", 3000, "最佳精度，适合大显存 GPU"),
    ModelSpec("large-v3-turbo", "deepdml/faster-whisper-large-v3-turbo-ct2", 1600, "GPU 默认推荐，速度快且质量高"),
)

MODEL_SPEC_MAP: Dict[str, ModelSpec] = {m.name: m for m in SUPPORTED_MODELS}

REQUIRED_MODEL_FILES: Tuple[str, ...] = (
    "model.bin",
    "config.json",
)


@dataclass
class ModelStatus:
    spec: ModelSpec
    installed: bool
    local_path: Path
    actual_size_mb: float = 0.0


def verify_model_dir(directory: Path) -> Tuple[bool, List[str]]:
    """Verify whether `directory` contains a valid CTranslate2 Whisper model."""
    if not directory.exists() or not directory.is_dir():
        return False, list(REQUIRED_MODEL_FILES)

    missing: List[str] = []
    for req in REQUIRED_MODEL_FILES:
        f = directory / req
        if not f.exists() or f.stat().st_size == 0:
            missing.append(req)

    has_vocab = any(
        (directory / name).exists()
        for name in ("vocabulary.txt", "vocabulary.json", "tokenizer.json")
    )
    if not has_vocab:
        missing.append("vocabulary.* / tokenizer.json")

    return len(missing) == 0, missing


def _dir_size_mb(directory: Path) -> float:
    if not directory.exists():
        return 0.0
    total = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
    return round(total / (1024 * 1024), 1)


def is_model_installed(model_name: str, paths: AppPaths = PATHS) -> bool:
    model_dir = paths.models_dir / model_name
    ok, _ = verify_model_dir(model_dir)
    return ok


def list_models(paths: AppPaths = PATHS) -> List[ModelStatus]:
    """Return installation status and disk usage for all supported Whisper models."""
    results: List[ModelStatus] = []
    for spec in SUPPORTED_MODELS:
        local_dir = paths.models_dir / spec.name
        installed, _ = verify_model_dir(local_dir)
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
    """Fetch `(rfilename, size_bytes)` list from HuggingFace API (with fallback to default CTranslate2 files)."""
    api_url = f"{endpoint.rstrip('/')}/api/models/{repo_id}"
    try:
        meta = http_client.fetch_json(api_url, proxy=proxy, timeout=20)
        siblings = meta.get("siblings") or []
        files: List[Tuple[str, int]] = []
        for s in siblings:
            rfile = str(s.get("rfilename") or "")
            if not rfile or rfile.startswith(".") or rfile.endswith(".md"):
                continue
            size = int(s.get("size") or (s.get("lfs") or {}).get("size") or 0)
            files.append((rfile, size))
        if files:
            return files
    except Exception:
        pass

    return [
        ("config.json", 2048),
        ("tokenizer.json", 2 * 1024 * 1024),
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
    repo_id = spec.repo_id if spec else model_name
    approx_total_bytes = (spec.approx_size_mb if spec else 500) * 1024 * 1024
    target_dir = paths.models_dir / model_name
    target_dir.mkdir(parents=True, exist_ok=True)

    endpoint = "https://hf-mirror.com" if hf_mirror else "https://huggingface.co"
    os.environ["HF_ENDPOINT"] = endpoint

    if on_log:
        on_log(f"开始下载模型 {model_name} ({repo_id})，源：{endpoint}")
    if on_progress:
        on_progress(0.02, f"正在连接 {endpoint} 获取模型文件列表...")

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
            quoted_path = urllib.parse.quote(rfile, safe="/")
            file_url = f"{endpoint.rstrip('/')}/{repo_id}/resolve/main/{quoted_path}"
            dest_file = target_dir / rfile

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
                # Optional vocabulary file variations (e.g. vocabulary.json vs vocabulary.txt)
                if rfile.startswith("vocabulary.") and (target_dir / "tokenizer.json").exists():
                    continue
                raise file_exc

            actual_sz = dest_file.stat().st_size if dest_file.exists() else declared_size
            completed_prior_bytes += actual_sz
    except CancelledError:
        raise
    except Exception as exc:
        raise ModelError(
            f"模型 {model_name} 下载失败，请检查网络、开启 HF 镜像或使用手动导入（支持重试断点续传）。",
            detail=str(exc),
        ) from exc

    ok, missing = verify_model_dir(target_dir)
    if not ok:
        raise ModelError(
            f"模型 {model_name} 下载不完整",
            detail="缺失文件: " + ", ".join(missing),
        )

    if on_progress:
        on_progress(1.0, f"模型 {model_name} 下载完成")
    if on_log:
        on_log(f"模型 {model_name} 已就绪：{target_dir}")
    return target_dir


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
            f"所选文件夹不是有效的 CTranslate2 Whisper 模型目录（缺少 {', '.join(missing)}）。",
            detail=str(source_dir),
        )

    target_dir = paths.models_dir / model_name
    if source_dir != target_dir.resolve():
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir)

    if on_log:
        on_log(f"已手动导入模型 {model_name}：{target_dir}")
    return target_dir
