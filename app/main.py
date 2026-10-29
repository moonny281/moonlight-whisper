"""Application entrypoint for Moonlight Whisper GUI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from app import __app_name__, __version__
from app.config import Config
from app.core import downloader, gpu_runtime
from app.logger import LOG_MANAGER
from app.paths import PATHS


def main(argv: Optional[List[str]] = None) -> int:
    # 1. Load configuration and configure custom data/GPU/model directories
    cfg = Config.load(paths=PATHS)
    PATHS.ensure_runtime_dirs()

    # 2. Pre-register gpu_runtime/ DLL directory BEFORE importing ctranslate2 or UI modules (§5.4)
    gpu_runtime.preload_dll_directory_on_startup(PATHS)
    downloader.ensure_plugin_yt_dlp_on_path(PATHS)

    # 3. Setup daily rotating logger
    logger = LOG_MANAGER.setup(level=cfg.log_level, paths=PATHS)
    logger.info("启动 %s v%s (数据目录: %s)", __app_name__, __version__, PATHS.data_root)

    # 4. Initialize Qt Application
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    from app.ui.main_window import MainWindow, apply_theme

    app = QApplication(argv or sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)

    ico_path: Path = PATHS.assets_dir / "app.ico"
    svg_path: Path = PATHS.assets_dir / "moon.svg"
    if ico_path.exists():
        app.setWindowIcon(QIcon(str(ico_path)))
    elif svg_path.exists():
        app.setWindowIcon(QIcon(str(svg_path)))

    apply_theme(app, cfg.ui.theme)

    window = MainWindow(cfg=cfg, paths=PATHS)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
