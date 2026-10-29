"""Application logging with 7-day daily file rotation and UI callback forwarding."""

from __future__ import annotations

import datetime as dt
import logging
import threading
from pathlib import Path
from typing import Callable, List, Optional

from app.paths import PATHS, AppPaths

LogCallback = Callable[[str], None]


class UIForwardingHandler(logging.Handler):
    """Forwards formatted log lines to registered UI callbacks in a thread-safe manner."""

    def __init__(self) -> None:
        super().__init__()
        self._callbacks: List[LogCallback] = []
        self._lock = threading.Lock()

    def add_callback(self, callback: LogCallback) -> None:
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def remove_callback(self, callback: LogCallback) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._lock:
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(msg)
            except Exception:
                pass


class AppLoggerManager:
    """Manages daily log files (`logs/app-YYYYMMDD.log`) with 7-day retention."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("moonlight_whisper")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.ui_handler = UIForwardingHandler()
        self.ui_handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        )
        self._file_handler: Optional[logging.FileHandler] = None
        self._initialized = False

    def setup(
        self,
        level: str = "INFO",
        paths: AppPaths = PATHS,
        retention_days: int = 7,
    ) -> logging.Logger:
        numeric_level = getattr(logging, level.upper(), logging.INFO)
        self.logger.setLevel(numeric_level)

        if self.ui_handler not in self.logger.handlers:
            self.logger.addHandler(self.ui_handler)

        try:
            logs_dir = paths.logs_dir
            logs_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup_old_logs(logs_dir, retention_days=retention_days)

            today_str = dt.date.today().strftime("%Y%m%d")
            log_file = logs_dir / f"app-{today_str}.log"

            if self._file_handler is not None:
                self.logger.removeHandler(self._file_handler)
                self._file_handler.close()

            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            self.logger.addHandler(fh)
            self._file_handler = fh
        except OSError:
            pass

        self._initialized = True
        return self.logger

    @staticmethod
    def _cleanup_old_logs(logs_dir: Path, retention_days: int = 7) -> None:
        cutoff = dt.datetime.now() - dt.timedelta(days=retention_days)
        for path in logs_dir.glob("app-*.log"):
            try:
                mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
                if mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def register_ui_callback(self, callback: LogCallback) -> None:
        self.ui_handler.add_callback(callback)

    def unregister_ui_callback(self, callback: LogCallback) -> None:
        self.ui_handler.remove_callback(callback)


LOG_MANAGER = AppLoggerManager()


def get_logger() -> logging.Logger:
    return LOG_MANAGER.logger
