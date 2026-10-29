"""CUDA 12 (cuBLAS 12 + cuDNN 9) detection, on-demand wheel download, manual import, and DLL loading."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core import http_client
from app.core.cancel import CancelToken
from app.core.errors import CancelledError, GpuError
from app.paths import PATHS, AppPaths

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]


class GpuStatus(Enum):
    NO_DRIVER = "no_driver"
    DRIVER_TOO_OLD = "driver_too_old"
    RUNTIME_MISSING = "runtime_missing"
    LOAD_FAILED = "load_failed"
    READY = "ready"


# Full CUDA 12 (cuBLAS 12) + cuDNN 9 runtime & dynamic engine DLLs required by CTranslate2 4.x (§5.4)
REQUIRED_CORE_DLLS: Tuple[str, ...] = (
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudnn64_9.dll",
    "cudnn_ops64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_graph64_9.dll",
    "cudnn_adv64_9.dll",
    "cudnn_heuristic64_9.dll",
    "cudnn_engines_precompiled64_9.dll",
    "cudnn_engines_runtime_compiled64_9.dll",
)

MIN_NVIDIA_DRIVER_MAJOR = 528

PYPI_PACKAGES: Tuple[str, ...] = (
    "nvidia-cublas-cu12",
    "nvidia-cudnn-cu12",
)

_DLL_DIR_HANDLES: Dict[str, Any] = {}


@dataclass
class GpuInfo:
    status: GpuStatus
    device_name: str = ""
    vram_gb: float = 0.0
    driver_version: str = ""
    missing_files: List[str] = field(default_factory=list)
    message: str = ""
    detail: str = ""


def required_files() -> List[str]:
    """Return the list of mandatory CUDA 12 / cuDNN 9 DLL filenames."""
    return list(REQUIRED_CORE_DLLS)


def check_runtime_files(gpu_dir: Path) -> List[str]:
    """Return missing required DLL names in `gpu_dir`."""
    if not gpu_dir.exists():
        return list(REQUIRED_CORE_DLLS)
    existing_lower = {
        p.name.lower()
        for p in gpu_dir.glob("*.dll")
        if p.is_file() and p.stat().st_size > 0
    }
    return [dll for dll in REQUIRED_CORE_DLLS if dll.lower() not in existing_lower]


def _check_nvcuda_dll() -> bool:
    """Attempt to load `nvcuda.dll` via ctypes to verify NVIDIA driver presence."""
    if sys.platform != "win32":
        return False
    try:
        ctypes.WinDLL("nvcuda.dll")
        return True
    except OSError:
        return False


def _query_nvidia_smi() -> Tuple[str, float, str]:
    """Query GPU name, VRAM (GB), and driver version via `nvidia-smi`."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            first_line = proc.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_line.split(",")]
            if len(parts) >= 3:
                name = parts[0]
                vram_mb = float(parts[1])
                driver = parts[2]
                return name, round(vram_mb / 1024.0, 2), driver
    except Exception:
        pass
    return "NVIDIA GPU", 8.0, "unknown"


def _parse_driver_major(driver_version: str) -> Optional[int]:
    if not driver_version or driver_version == "unknown":
        return None
    head = driver_version.strip().split(".")[0]
    try:
        return int(head)
    except ValueError:
        return None


def preload_dll_directory_on_startup(paths: AppPaths = PATHS) -> bool:
    """Register `gpu_runtime/` before `ctranslate2` is imported if DLLs are already present."""
    gpu_dir = paths.gpu_runtime_dir
    if not gpu_dir.exists():
        return False
    if check_runtime_files(gpu_dir):
        return False
    return register_dll_directory(gpu_dir)


def register_dll_directory(gpu_dir: Path) -> bool:
    """Add `gpu_dir` to Windows DLL search path and prepend to `PATH` (idempotent & deduplicated)."""
    if not gpu_dir.exists():
        return False
    resolved = str(gpu_dir.resolve())
    norm_key = os.path.normcase(resolved)

    current_path = os.environ.get("PATH", "")
    path_parts = current_path.split(os.pathsep) if current_path else []
    norm_parts = {os.path.normcase(p) for p in path_parts}
    if norm_key not in norm_parts:
        os.environ["PATH"] = resolved + os.pathsep + current_path

    if norm_key in _DLL_DIR_HANDLES:
        return True

    add_dll_dir = getattr(os, "add_dll_directory", None)
    if callable(add_dll_dir):
        try:
            handle = add_dll_dir(resolved)
            _DLL_DIR_HANDLES[norm_key] = handle
        except OSError:
            return False
    else:
        _DLL_DIR_HANDLES[norm_key] = True
    return True




def clear_dll_directory_handles() -> None:
    """Close and clear all registered Windows DLL directory handles."""
    for handle in list(_DLL_DIR_HANDLES.values()):
        close_fn = getattr(handle, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
    _DLL_DIR_HANDLES.clear()


def _verify_ctranslate2_cuda() -> Tuple[bool, str]:
    """Verify `ctranslate2.get_cuda_device_count() > 0`."""
    try:
        import ctranslate2  # type: ignore
        count = int(ctranslate2.get_cuda_device_count())
        if count > 0:
            return True, f"CUDA 设备数量: {count}"
        return False, "ctranslate2.get_cuda_device_count() 返回 0（可能需要重启程序使新加载的 DLL 生效）"
    except Exception as exc:
        return False, str(exc)


def activate(paths: AppPaths = PATHS) -> bool:
    """Register `gpu_runtime/` DLL directory and verify CTranslate2 recognizes CUDA."""
    gpu_dir = paths.gpu_runtime_dir
    if check_runtime_files(gpu_dir):
        return False
    if not register_dll_directory(gpu_dir):
        return False
    ok, _ = _verify_ctranslate2_cuda()
    return ok


def detect(paths: AppPaths = PATHS) -> GpuInfo:
    """Execute the 6-step GPU detection state machine defined in §5.4."""
    if not _check_nvcuda_dll():
        return GpuInfo(
            status=GpuStatus.NO_DRIVER,
            message="未检测到 NVIDIA 显卡驱动（无法加载 nvcuda.dll），已使用 CPU 模式。",
        )

    device_name, vram_gb, driver_version = _query_nvidia_smi()
    major = _parse_driver_major(driver_version)
    if major is not None and major < MIN_NVIDIA_DRIVER_MAJOR:
        return GpuInfo(
            status=GpuStatus.DRIVER_TOO_OLD,
            device_name=device_name,
            vram_gb=vram_gb,
            driver_version=driver_version,
            message=(
                f"当前 NVIDIA 驱动版本 ({driver_version}) 过旧，"
                f"CUDA 12 需要 {MIN_NVIDIA_DRIVER_MAJOR}.0 及以上版本，请升级显卡驱动。"
            ),
        )

    gpu_dir = paths.gpu_runtime_dir
    missing = check_runtime_files(gpu_dir)
    if missing:
        return GpuInfo(
            status=GpuStatus.RUNTIME_MISSING,
            device_name=device_name,
            vram_gb=vram_gb,
            driver_version=driver_version,
            missing_files=missing,
            message="未安装完整的 CUDA 运行库，首次启用 GPU 加速需下载或导入运行库（约 1GB）。",
            detail="缺失 DLL: " + ", ".join(missing),
        )

    register_dll_directory(gpu_dir)
    ok, detail = _verify_ctranslate2_cuda()
    if not ok:
        return GpuInfo(
            status=GpuStatus.LOAD_FAILED,
            device_name=device_name,
            vram_gb=vram_gb,
            driver_version=driver_version,
            message="CUDA 运行库加载失败，建议重启程序或重新下载运行库。",
            detail=detail,
        )

    return GpuInfo(
        status=GpuStatus.READY,
        device_name=device_name,
        vram_gb=vram_gb,
        driver_version=driver_version,
        message=f"就绪 ({device_name}, {vram_gb:.1f}GB)",
        detail=detail,
    )


def _extract_dlls_from_archive(archive_path: Path, target_dir: Path) -> int:
    """Extract all `.dll` files from a wheel or zip archive into `target_dir`."""
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            filename = Path(member.filename).name
            if filename.lower().endswith(".dll"):
                dest = target_dir / filename
                with zf.open(member, "r") as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                count += 1
    return count


def install(
    mirror: str = "tuna",
    proxy: str = "",
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> GpuInfo:
    """Download `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` wheels, verify SHA256, and install DLLs."""
    if token:
        token.check()

    wheels_meta: List[Tuple[str, Dict[str, Any]]] = []
    total_estimated_bytes = 0
    for pkg in PYPI_PACKAGES:
        if token:
            token.check()
        if on_log:
            on_log(f"正在解析 {pkg} 下载地址 (镜像: {mirror})...")
        try:
            w = http_client.resolve_pypi_wheel(pkg, mirror=mirror, proxy=proxy, win_amd64_only=True)
        except Exception as exc:
            raise GpuError(f"获取 {pkg} 下载信息失败，请检查网络或切换镜像源。", detail=str(exc)) from exc
        wheels_meta.append((pkg, w))
        total_estimated_bytes += int(w.get("size") or 500 * 1024 * 1024)

    dl_cache_dir = paths.temp_dir / "gpu_wheels"
    dl_cache_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = paths.temp_dir / "gpu_runtime_staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    completed_prior_bytes = 0
    start_ts = time.monotonic()
    downloaded_wheels: List[Path] = []

    for pkg, w in wheels_meta:
        filename = str(w["filename"])
        url = str(w["url"])
        est_size = int(w.get("size") or 500 * 1024 * 1024)
        sha256 = str(w.get("digests", {}).get("sha256") or "")
        dest_file = dl_cache_dir / filename

        if on_log:
            on_log(f"开始下载 {filename}...")

        def _on_chunk(cur_bytes: int, file_total: int, base: int = completed_prior_bytes, pkg_label: str = pkg) -> None:
            if not on_progress:
                return
            effective_total = max(total_estimated_bytes, base + file_total)
            overall = base + cur_bytes
            ratio = min(0.92, max(0.0, overall / max(1, effective_total)))
            elapsed = max(0.001, time.monotonic() - start_ts)
            speed_mb = (overall / (1024 * 1024)) / elapsed
            rem_bytes = max(0, effective_total - overall)
            eta_sec = int(rem_bytes / max(1.0, overall / elapsed)) if overall > 0 else 0
            msg = (
                f"正在下载 {pkg_label}: {overall / (1024 * 1024):.1f} / {effective_total / (1024 * 1024):.1f} MB "
                f"({speed_mb:.1f} MB/s, 剩余约 {eta_sec}s)"
            )
            on_progress(ratio, msg)

        try:
            wheel_path = http_client.stream_download_with_resume(
                url=url,
                dest_file=dest_file,
                proxy=proxy,
                expected_sha256=sha256,
                on_chunk_progress=_on_chunk,
                token=token,
                timeout=60,
            )
        except CancelledError:
            raise
        except Exception as exc:
            raise GpuError(f"下载运行库文件失败 ({filename})，支持重试断点续传。", detail=str(exc)) from exc

        downloaded_wheels.append(wheel_path)
        completed_prior_bytes += wheel_path.stat().st_size if wheel_path.exists() else est_size

    if on_progress:
        on_progress(0.95, "正在解压并校验 CUDA DLL 文件...")
    if on_log:
        on_log("正在解压 CUDA 运行库 DLL...")

    for whl in downloaded_wheels:
        if token:
            token.check()
        _extract_dlls_from_archive(whl, staging_dir)

    missing = check_runtime_files(staging_dir)
    if missing:
        raise GpuError(
            "解压后的运行库缺少必要 DLL 文件",
            detail="缺失: " + ", ".join(missing),
        )

    target_dir = paths.gpu_runtime_dir
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = target_dir.with_name(target_dir.name + "_old")
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    if target_dir.exists():
        target_dir.rename(backup_dir)
    shutil.move(str(staging_dir), str(target_dir))
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)

    shutil.rmtree(dl_cache_dir, ignore_errors=True)

    if on_progress:
        on_progress(1.0, "CUDA 运行库安装完成")
    if on_log:
        on_log(f"CUDA 运行库已安装至：{target_dir}")

    return detect(paths)


def import_from(
    source_path: Path,
    on_log: Optional[LogCallback] = None,
    paths: AppPaths = PATHS,
) -> GpuInfo:
    """Import CUDA DLLs from a local `.zip`/`.whl` archive or a directory."""
    source_path = source_path.expanduser().resolve()
    if not source_path.exists():
        raise GpuError(f"所选路径不存在：{source_path}")

    staging_dir = paths.temp_dir / "gpu_import_staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    if paths.gpu_runtime_dir.exists():
        for existing_dll in paths.gpu_runtime_dir.glob("*.dll"):
            shutil.copy2(existing_dll, staging_dir / existing_dll.name)

    if source_path.is_file():
        if source_path.suffix.lower() not in (".zip", ".whl"):
            raise GpuError("手动导入仅支持 .zip / .whl 压缩包或包含 DLL 的文件夹。")
        _extract_dlls_from_archive(source_path, staging_dir)
    elif source_path.is_dir():
        for dll_path in source_path.rglob("*.dll"):
            if dll_path.is_file():
                shutil.copy2(dll_path, staging_dir / dll_path.name)
        for archive in list(source_path.glob("*.whl")) + list(source_path.glob("*.zip")):
            _extract_dlls_from_archive(archive, staging_dir)

    missing = check_runtime_files(staging_dir)
    if missing:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise GpuError(
            "导入的文件中缺少必要的 CUDA 12 / cuDNN 9 运行库 DLL。",
            detail="仍缺失: " + ", ".join(missing),
        )

    target_dir = paths.gpu_runtime_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    for dll in staging_dir.glob("*.dll"):
        shutil.copy2(dll, target_dir / dll.name)
    shutil.rmtree(staging_dir, ignore_errors=True)

    if on_log:
        on_log(f"成功从 {source_path} 导入 CUDA 运行库。")

    return detect(paths)
