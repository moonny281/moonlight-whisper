"""Thread-safe cancellation token and subprocess termination helpers."""

from __future__ import annotations

import subprocess
import threading
from typing import Callable, List

from app.core.errors import CancelledError


class CancelToken:
    """Thread-safe cooperative cancellation token."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._processes: List[subprocess.Popen] = []
        self._callbacks: List[Callable[[], None]] = []

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        """Signal cancellation and immediately terminate any registered subprocesses."""
        self._event.set()
        with self._lock:
            procs = list(self._processes)
            callbacks = list(self._callbacks)

        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

        for cb in callbacks:
            try:
                cb()
            except Exception:
                pass

    def check(self) -> None:
        """Raise `CancelledError` if cancellation has been requested."""
        if self._event.is_set():
            raise CancelledError("任务已被用户取消")

    def register_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if proc not in self._processes:
                self._processes.append(proc)
        if self._event.is_set():
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    def unregister_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if proc in self._processes:
                self._processes.remove(proc)

    def add_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)
        if self._event.is_set():
            try:
                callback()
            except Exception:
                pass
