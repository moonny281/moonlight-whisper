"""Unified path resolution and data directory strategy for Moonlight Whisper.

Resolution order for mutable data root (models, gpu_runtime, output, temp, logs, config.json):
1. User-customized `data_dir` in config (if set and writable)
2. Application root directory (if `portable.flag` exists or directory is writable via probe file)
3. Fallback to `%LOCALAPPDATA%/MoonlightWhisper` (e.g., when installed into `C:\\Program Files`)
4. Project root directory in development environment
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Optional


class AppPaths:
    """Centralized path manager for application binaries and user data."""

    def __init__(
        self,
        app_dir: Optional[Path] = None,
        custom_data_dir: str = "",
        custom_gpu_dir: str = "",
        custom_models_dir: str = "",
        custom_output_dir: str = "",
        custom_temp_dir: str = "",
    ) -> None:
        self.app_dir: Path = (app_dir or self._detect_app_dir()).resolve()
        self._custom_data_dir = custom_data_dir.strip()
        self._custom_gpu_dir = custom_gpu_dir.strip()
        self._custom_models_dir = custom_models_dir.strip()
        self._custom_output_dir = custom_output_dir.strip()
        self._custom_temp_dir = custom_temp_dir.strip()
        self._data_root: Optional[Path] = None

    @staticmethod
    def is_frozen() -> bool:
        """Return True if running inside a PyInstaller bundle."""
        return bool(getattr(sys, "frozen", False))

    @classmethod
    def _detect_app_dir(cls) -> Path:
        if cls.is_frozen():
            return Path(sys.executable).resolve().parent
        # Development mode: repo root is parent of `app/`
        return Path(__file__).resolve().parent.parent

    @staticmethod
    def is_dir_writable(directory: Path) -> bool:
        """Probe whether `directory` is writable by creating and deleting a temporary file."""
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".write_probe_{uuid.uuid4().hex[:8]}.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False

    @classmethod
    def local_appdata_root(cls) -> Path:
        """Return `%LOCALAPPDATA%\\MoonlightWhisper` (or platform equivalent)."""
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "MoonlightWhisper"
        return Path.home() / ".local" / "share" / "MoonlightWhisper"

    @property
    def is_portable_flagged(self) -> bool:
        """Return True if `portable.flag` exists next to the executable."""
        return (self.app_dir / "portable.flag").exists()

    @property
    def data_root(self) -> Path:
        """Resolve the primary writable data directory according to §4 strategy."""
        if self._data_root is not None:
            return self._data_root

        # 1. Explicit custom data_dir
        if self._custom_data_dir:
            candidate = Path(self._custom_data_dir).expanduser().resolve()
            if self.is_dir_writable(candidate):
                self._data_root = candidate
                return self._data_root

        # 2. Portable flag forces app_dir
        if self.is_portable_flagged:
            self._data_root = self.app_dir
            return self._data_root

        # 3. Same directory as executable / project root if writable
        if self.is_dir_writable(self.app_dir):
            self._data_root = self.app_dir
            return self._data_root

        # 4. Fallback to %LOCALAPPDATA%/MoonlightWhisper
        fallback = self.local_appdata_root().resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        self._data_root = fallback
        return self._data_root

    def configure_overrides(
        self,
        custom_data_dir: str = "",
        custom_gpu_dir: str = "",
        custom_models_dir: str = "",
        custom_output_dir: str = "",
        custom_temp_dir: str = "",
    ) -> None:
        """Update directory overrides from loaded Config."""
        self._custom_data_dir = custom_data_dir.strip()
        self._custom_gpu_dir = custom_gpu_dir.strip()
        self._custom_models_dir = custom_models_dir.strip()
        self._custom_output_dir = custom_output_dir.strip()
        self._custom_temp_dir = custom_temp_dir.strip()
        self._data_root = None

    def _resolve_subpath(self, custom_value: str, default_name: str) -> Path:
        if custom_value:
            p = Path(custom_value).expanduser()
            if not p.is_absolute():
                p = self.data_root / p
            return p.resolve()
        return (self.data_root / default_name).resolve()

    @property
    def bootstrap_config_path(self) -> Path:
        """Locate `config.json` before custom overrides are known."""
        app_cfg = self.app_dir / "config.json"
        if app_cfg.exists() or self.is_dir_writable(self.app_dir):
            return app_cfg
        fallback = self.local_appdata_root()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / "config.json"

    @property
    def config_path(self) -> Path:
        return self.data_root / "config.json"

    @property
    def bin_dir(self) -> Path:
        """Read-only bundled binaries (`ffmpeg.exe`, `deno.exe`) always reside in `app_dir/bin`."""
        return self.app_dir / "bin"

    @property
    def assets_dir(self) -> Path:
        if self.is_frozen():
            internal_assets = self.app_dir / "_internal" / "assets"
            if internal_assets.exists():
                return internal_assets
        return self.app_dir / "assets"

    @property
    def plugins_dir(self) -> Path:
        """Directory for hot-updated wheels such as `plugins/yt_dlp`."""
        return self.data_root / "plugins"

    @property
    def models_dir(self) -> Path:
        return self._resolve_subpath(self._custom_models_dir, "models")

    @property
    def gpu_runtime_dir(self) -> Path:
        return self._resolve_subpath(self._custom_gpu_dir, "gpu_runtime")

    @property
    def output_dir(self) -> Path:
        return self._resolve_subpath(self._custom_output_dir, "output")

    @property
    def temp_dir(self) -> Path:
        return self._resolve_subpath(self._custom_temp_dir, "temp")

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    def ensure_runtime_dirs(self) -> None:
        """Ensure all required runtime directories exist."""
        for folder in (
            self.models_dir,
            self.gpu_runtime_dir,
            self.output_dir,
            self.temp_dir,
            self.logs_dir,
            self.plugins_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)

    def find_binary(self, name: str) -> Optional[Path]:
        """Locate an external tool (`ffmpeg` or `deno`) in `bin/` first, then system PATH."""
        exe_name = f"{name}.exe" if sys.platform == "win32" and not name.endswith(".exe") else name
        bundled = self.bin_dir / exe_name
        if bundled.exists() and bundled.is_file():
            return bundled
        alt_bundled = self.bin_dir / name
        if alt_bundled.exists() and alt_bundled.is_file():
            return alt_bundled
        which_path = shutil.which(name)
        return Path(which_path).resolve() if which_path else None

    @staticmethod
    def migrate_directory_contents(src: Path, dst: Path) -> int:
        """Move files from `src` to `dst` when a user changes storage directories in Settings.

        Returns the number of top-level entries migrated.
        """
        src = src.resolve()
        dst = dst.resolve()
        if src == dst or not src.exists():
            return 0
        dst.mkdir(parents=True, exist_ok=True)
        migrated = 0
        for item in src.iterdir():
            target = dst / item.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(item), str(target))
            migrated += 1
        return migrated


# Global singleton path instance
PATHS = AppPaths()
