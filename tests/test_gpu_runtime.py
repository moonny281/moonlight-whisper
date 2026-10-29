"""Unit tests for `app.core.gpu_runtime` state machine and local archive/folder import."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.core import gpu_runtime
from app.core.gpu_runtime import GpuStatus
from app.paths import AppPaths


class TestGpuRuntime(unittest.TestCase):
    def _populate_dummy_dlls(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for dll in gpu_runtime.REQUIRED_CORE_DLLS:
            (directory / dll).write_bytes(b"MZ_DUMMY_DLL")

    def test_state_no_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            with patch("app.core.gpu_runtime._check_nvcuda_dll", return_value=False):
                info = gpu_runtime.detect(paths)
                self.assertEqual(info.status, GpuStatus.NO_DRIVER)

    def test_state_driver_too_old(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            with patch("app.core.gpu_runtime._check_nvcuda_dll", return_value=True), patch(
                "app.core.gpu_runtime._query_nvidia_smi",
                return_value=("NVIDIA RTX 3060", 12.0, "512.15"),
            ):
                info = gpu_runtime.detect(paths)
                self.assertEqual(info.status, GpuStatus.DRIVER_TOO_OLD)

    def test_state_runtime_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            with patch("app.core.gpu_runtime._check_nvcuda_dll", return_value=True), patch(
                "app.core.gpu_runtime._query_nvidia_smi",
                return_value=("NVIDIA RTX 4070", 12.0, "551.61"),
            ):
                info = gpu_runtime.detect(paths)
                self.assertEqual(info.status, GpuStatus.RUNTIME_MISSING)
                self.assertTrue(len(info.missing_files) > 0)

    def test_state_load_failed_and_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            self._populate_dummy_dlls(paths.gpu_runtime_dir)

            with patch("app.core.gpu_runtime._check_nvcuda_dll", return_value=True), patch(
                "app.core.gpu_runtime._query_nvidia_smi",
                return_value=("NVIDIA RTX 4070", 12.0, "551.61"),
            ), patch("app.core.gpu_runtime.register_dll_directory", return_value=True):
                with patch(
                    "app.core.gpu_runtime._verify_ctranslate2_cuda",
                    return_value=(False, "CUDA error"),
                ):
                    info_fail = gpu_runtime.detect(paths)
                    self.assertEqual(info_fail.status, GpuStatus.LOAD_FAILED)

                with patch(
                    "app.core.gpu_runtime._verify_ctranslate2_cuda",
                    return_value=(True, "CUDA devices: 1"),
                ):
                    info_ok = gpu_runtime.detect(paths)
                    self.assertEqual(info_ok.status, GpuStatus.READY)

    def test_import_from_zip_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            zip_path = Path(tmp) / "cuda_bundle.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for dll in gpu_runtime.REQUIRED_CORE_DLLS:
                    zf.writestr(f"nvidia/bin/{dll}", b"MZ_DLL_CONTENT")

            with patch("app.core.gpu_runtime._check_nvcuda_dll", return_value=True), patch(
                "app.core.gpu_runtime._query_nvidia_smi",
                return_value=("NVIDIA RTX 4060", 8.0, "555.99"),
            ), patch("app.core.gpu_runtime.register_dll_directory", return_value=True), patch(
                "app.core.gpu_runtime._verify_ctranslate2_cuda",
                return_value=(True, "OK"),
            ):
                info = gpu_runtime.import_from(zip_path, paths=paths)
                self.assertEqual(info.status, GpuStatus.READY)
                self.assertEqual(gpu_runtime.check_runtime_files(paths.gpu_runtime_dir), [])


if __name__ == "__main__":
    unittest.main()
