"""Application entrypoint for Moonlight Whisper GUI."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

# Set critical runtime environment variables BEFORE importing ctranslate2, onnxruntime, or huggingface_hub:
# 1. Prevent OpenMP duplicate library abort (libiomp5md.dll conflict between ctranslate2 & onnxruntime on Windows)
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
# 2. Avoid Windows privilege errors when huggingface_hub creates cache symlinks
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# 3. Default HuggingFace endpoint to hf-mirror.com before huggingface_hub.constants is imported
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from app import __app_name__, __version__
from app.config import Config
from app.core import downloader, gpu_runtime
from app.logger import LOG_MANAGER
from app.paths import PATHS


def _register_bundled_dll_dirs() -> None:
    """Ensure PyInstaller bundled native libraries (`ctranslate2`, `onnxruntime`, `av.libs`) are on DLL search path."""
    if not getattr(sys, "frozen", False):
        return
    meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal"))
    candidates = [
        meipass,
        meipass / "ctranslate2",
        meipass / "ctranslate2.libs",
        meipass / "onnxruntime" / "capi",
        meipass / "onnxruntime.libs",
        meipass / "av.libs",
        meipass / "tokenizers.libs",
    ]
    for d in candidates:
        if d.exists() and d.is_dir():
            gpu_runtime.register_dll_directory(d)


def main(argv: Optional[List[str]] = None) -> int:
    # 1. Load configuration and configure custom data/GPU/model directories
    cfg = Config.load(paths=PATHS)
    PATHS.ensure_runtime_dirs()

    if not cfg.hf_mirror:
        os.environ["HF_ENDPOINT"] = "https://huggingface.co"

    # 2. Pre-register bundled DLLs and gpu_runtime/ BEFORE importing ctranslate2 or UI modules (§5.4)
    _register_bundled_dll_dirs()
    gpu_runtime.preload_dll_directory_on_startup(PATHS)
    downloader.ensure_plugin_yt_dlp_on_path(PATHS)

    # 3. Setup daily rotating logger
    logger = LOG_MANAGER.setup(level=cfg.log_level, paths=PATHS)
    logger.info("启动 %s v%s (数据目录: %s)", __app_name__, __version__, PATHS.data_root)

    # 4. Initialize Qt Application
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    from app.ui.main_window import MainWindow
    from app.ui.theme import apply_theme

    app = QApplication(argv or sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)

    ico_path: Path = PATHS.assets_dir / "app.ico"
    svg_path: Path = PATHS.assets_dir / "moon.svg"
    if ico_path.exists():
        app.setWindowIcon(QIcon(str(ico_path)))
    elif svg_path.exists():
        app.setWindowIcon(QIcon(str(svg_path)))

    window = MainWindow(cfg=cfg, paths=PATHS)
    apply_theme(app, cfg.ui.theme, window=window)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
