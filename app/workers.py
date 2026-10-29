"""Qt worker threads (`QThread`) and serial task queue (`TaskQueue`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from app.config import Config
from app.core import downloader, gpu_runtime, model_manager, pipeline
from app.core.cancel import CancelToken
from app.core.downloader import DownloadOptions
from app.core.errors import CancelledError, ErrorInfo
from app.core.pipeline import Callbacks, Job, PipelineResult
from app.logger import get_logger
from app.paths import PATHS, AppPaths


class TaskStatus(Enum):
    WAITING = "等待"
    RUNNING = "进行中"
    COMPLETED = "完成"
    FAILED = "失败"
    CANCELLED = "已取消"


@dataclass
class TaskItem:
    job: Job
    display_title: str
    status: TaskStatus = TaskStatus.WAITING
    stage_text: str = "等待"
    progress: int = 0
    result: Optional[PipelineResult] = None
    error_info: Optional[ErrorInfo] = None
    token: CancelToken = field(default_factory=CancelToken)


class JobWorker(QThread):
    """Executes a single `Job` through `core.pipeline.run` in a background thread."""

    stage_changed = Signal(str, str)        # (job_id, stage_text)
    progress_changed = Signal(str, int)     # (job_id, pct)
    title_updated = Signal(str, str)        # (job_id, title)
    log_emitted = Signal(str)               # log line
    job_finished = Signal(str, object)      # (job_id, PipelineResult)
    job_failed = Signal(str, object)        # (job_id, ErrorInfo)

    def __init__(
        self,
        task_item: TaskItem,
        cfg: Config,
        paths: AppPaths = PATHS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.task_item = task_item
        self.cfg = Config.from_dict(cfg.to_dict())
        self.paths = paths

    def run(self) -> None:
        job = self.task_item.job
        token = self.task_item.token
        logger = get_logger()

        def _on_stage(stage: str) -> None:
            self.stage_changed.emit(job.job_id, stage)

        def _on_progress(pct: int) -> None:
            self.progress_changed.emit(job.job_id, pct)

        def _on_log(msg: str) -> None:
            logger.info("[%s] %s", job.job_id[:6], msg)

        def _on_title(title: str) -> None:
            self.title_updated.emit(job.job_id, title)

        cb = Callbacks(
            on_stage=_on_stage,
            on_progress=_on_progress,
            on_log=_on_log,
            on_title=_on_title,
        )

        try:
            result = pipeline.run(
                job=job,
                cfg=self.cfg,
                cb=cb,
                token=token,
                paths=self.paths,
            )
            self.job_finished.emit(job.job_id, result)
        except Exception as exc:
            err = ErrorInfo.from_exception(exc)
            if not isinstance(exc, CancelledError):
                logger.error("[%s] %s | %s", job.job_id[:6], err.user_message, err.detail)
            self.job_failed.emit(job.job_id, err)


class PlaylistExpandWorker(QThread):
    """Expands input URLs that might be playlists or multi-part videos without blocking UI."""

    jobs_ready = Signal(list)               # List[Job]
    log_emitted = Signal(str)

    def __init__(
        self,
        raw_inputs: List[str],
        cfg: Config,
        paths: AppPaths = PATHS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.raw_inputs = raw_inputs
        self.cfg = Config.from_dict(cfg.to_dict())
        self.paths = paths
        self.token = CancelToken()

    def run(self) -> None:
        logger = get_logger()
        expanded: List[Job] = []
        dl_opts = DownloadOptions(
            proxy=self.cfg.proxy,
            cookies_file=self.cfg.cookies_file,
            cookies_from_browser=self.cfg.cookies_from_browser,
            allow_playlist=self.cfg.allow_playlist,
            ffmpeg_dir=self.paths.bin_dir,
        )

        for line in self.raw_inputs:
            src = line.strip()
            if not src:
                continue
            probe_job = Job(source=src)
            if not probe_job.is_local_file and self.cfg.allow_playlist:
                try:
                    logger.info("正在检查播放列表/多 P 视频：%s", src)
                    entries = downloader.extract_playlist_or_info(
                        src,
                        dl_opts,
                        token=self.token,
                        paths=self.paths,
                    )
                    if len(entries) > 1:
                        logger.info("已展开播放列表，共 %d 个视频任务", len(entries))
                    for item in entries:
                        expanded.append(Job(source=item.url, title_hint=item.title))
                except Exception as exc:
                    logger.warning("展开播放列表失败，按单链接入队 (%s)", exc)
                    expanded.append(probe_job)
            else:
                expanded.append(probe_job)

        self.jobs_ready.emit(expanded)


class GpuInstallWorker(QThread):
    """Background worker for downloading and extracting CUDA 12 / cuDNN 9 wheels."""

    progress_changed = Signal(int, str)     # (pct, detail_msg)
    install_succeeded = Signal(object)      # GpuInfo
    install_failed = Signal(object)         # ErrorInfo

    def __init__(
        self,
        mirror: str,
        proxy: str,
        paths: AppPaths = PATHS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.mirror = mirror
        self.proxy = proxy
        self.paths = paths
        self.token = CancelToken()

    def cancel(self) -> None:
        self.token.cancel()

    def run(self) -> None:
        logger = get_logger()
        try:
            info = gpu_runtime.install(
                mirror=self.mirror,
                proxy=self.proxy,
                on_progress=lambda ratio, msg: self.progress_changed.emit(int(ratio * 100), msg),
                on_log=lambda msg: logger.info("[GPU安装] %s", msg),
                token=self.token,
                paths=self.paths,
            )
            self.install_succeeded.emit(info)
        except Exception as exc:
            self.install_failed.emit(ErrorInfo.from_exception(exc))


class ModelDownloadWorker(QThread):
    """Background worker for downloading a Whisper CTranslate2 model."""

    progress_changed = Signal(int, str)     # (pct, status_msg)
    download_succeeded = Signal(str, str)   # (model_name, local_dir_str)
    download_failed = Signal(str, object)   # (model_name, ErrorInfo)

    def __init__(
        self,
        model_name: str,
        hf_mirror: bool,
        proxy: str,
        paths: AppPaths = PATHS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.model_name = model_name
        self.hf_mirror = hf_mirror
        self.proxy = proxy
        self.paths = paths
        self.token = CancelToken()

    def cancel(self) -> None:
        self.token.cancel()

    def run(self) -> None:
        logger = get_logger()
        try:
            target = model_manager.download_model(
                model_name=self.model_name,
                hf_mirror=self.hf_mirror,
                proxy=self.proxy,
                on_progress=lambda ratio, msg: self.progress_changed.emit(int(ratio * 100), msg),
                on_log=lambda msg: logger.info("[模型下载] %s", msg),
                token=self.token,
                paths=self.paths,
            )
            self.download_succeeded.emit(self.model_name, str(target))
        except Exception as exc:
            self.download_failed.emit(self.model_name, ErrorInfo.from_exception(exc))


class YtDlpUpdateWorker(QThread):
    """Background worker for hot-updating yt-dlp from PyPI."""

    update_succeeded = Signal(str)          # new_version
    update_failed = Signal(object)          # ErrorInfo

    def __init__(
        self,
        pypi_mirror: str,
        proxy: str,
        paths: AppPaths = PATHS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.pypi_mirror = pypi_mirror
        self.proxy = proxy
        self.paths = paths
        self.token = CancelToken()

    def run(self) -> None:
        logger = get_logger()
        try:
            ver = downloader.update_yt_dlp_from_pypi(
                pypi_mirror=self.pypi_mirror,
                proxy=self.proxy,
                on_log=lambda msg: logger.info("[yt-dlp更新] %s", msg),
                token=self.token,
                paths=self.paths,
            )
            self.update_succeeded.emit(ver)
        except Exception as exc:
            self.update_failed.emit(ErrorInfo.from_exception(exc))


class TaskQueue(QObject):
    """Serial scheduler managing queued transcription jobs."""

    task_added = Signal(object)                 # TaskItem
    task_updated = Signal(str)                  # job_id
    tasks_removed = Signal(list)                # List[str] (job_ids)
    queue_idle = Signal()
    task_completed_notification = Signal(str, str)  # (title, md_path_str)

    def __init__(
        self,
        cfg: Config,
        paths: AppPaths = PATHS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.paths = paths
        self._tasks: Dict[str, TaskItem] = {}
        self._order: List[str] = []
        self._current_worker: Optional[JobWorker] = None
        self._current_job_id: Optional[str] = None
        self._shutting_down: bool = False

    @property
    def is_running(self) -> bool:
        return self._current_worker is not None and self._current_worker.isRunning()

    def get_task(self, job_id: str) -> Optional[TaskItem]:
        return self._tasks.get(job_id)

    def all_tasks(self) -> List[TaskItem]:
        return [self._tasks[jid] for jid in self._order if jid in self._tasks]

    def enqueue_jobs(self, jobs: List[Job]) -> List[TaskItem]:
        added: List[TaskItem] = []
        for job in jobs:
            display = job.title_hint or (
                Path(job.source).name if job.is_local_file else job.source
            )
            item = TaskItem(job=job, display_title=display)
            self._tasks[job.job_id] = item
            self._order.append(job.job_id)
            added.append(item)
            self.task_added.emit(item)
        self._schedule_next()
        return added

    def _schedule_next(self) -> None:
        if self._shutting_down or self.is_running:
            return
        next_item = next(
            (self._tasks[jid] for jid in self._order if self._tasks[jid].status == TaskStatus.WAITING),
            None,
        )
        if next_item is None:
            self._current_job_id = None
            self.queue_idle.emit()
            return

        next_item.status = TaskStatus.RUNNING
        next_item.stage_text = "准备中"
        next_item.progress = 1
        self._current_job_id = next_item.job.job_id
        self.task_updated.emit(next_item.job.job_id)

        worker = JobWorker(next_item, cfg=self.cfg, paths=self.paths, parent=self)
        worker.stage_changed.connect(self._on_worker_stage)
        worker.progress_changed.connect(self._on_worker_progress)
        worker.title_updated.connect(self._on_worker_title)
        worker.job_finished.connect(self._on_worker_finished)
        worker.job_failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_thread_terminated)
        self._current_worker = worker
        worker.start()

    def _on_worker_stage(self, job_id: str, stage: str) -> None:
        item = self._tasks.get(job_id)
        if item:
            item.stage_text = stage
            self.task_updated.emit(job_id)

    def _on_worker_progress(self, job_id: str, pct: int) -> None:
        item = self._tasks.get(job_id)
        if item:
            item.progress = pct
            self.task_updated.emit(job_id)

    def _on_worker_title(self, job_id: str, title: str) -> None:
        item = self._tasks.get(job_id)
        if item and title:
            item.display_title = title
            self.task_updated.emit(job_id)

    def _on_worker_finished(self, job_id: str, result: PipelineResult) -> None:
        item = self._tasks.get(job_id)
        if item:
            item.status = TaskStatus.COMPLETED
            item.stage_text = "完成"
            item.progress = 100
            item.result = result
            if result.title:
                item.display_title = result.title
            self.task_updated.emit(job_id)
            self.task_completed_notification.emit(item.display_title, str(result.md_path))

    def _on_worker_failed(self, job_id: str, err: ErrorInfo) -> None:
        item = self._tasks.get(job_id)
        if item:
            if err.category == "cancelled":
                item.status = TaskStatus.CANCELLED
                item.stage_text = "已取消"
            else:
                item.status = TaskStatus.FAILED
                item.stage_text = f"失败: {err.user_message[:28]}"
            item.error_info = err
            self.task_updated.emit(job_id)

    def _on_thread_terminated(self) -> None:
        self._current_worker = None
        self._current_job_id = None
        self._schedule_next()

    def cancel_current(self) -> None:
        if self._current_job_id and self._current_job_id in self._tasks:
            item = self._tasks[self._current_job_id]
            item.stage_text = "正在取消..."
            item.token.cancel()
            self.task_updated.emit(self._current_job_id)

    def cancel_task(self, job_id: str) -> None:
        item = self._tasks.get(job_id)
        if not item:
            return
        if item.status == TaskStatus.WAITING:
            item.status = TaskStatus.CANCELLED
            item.stage_text = "已取消"
            item.token.cancel()
            self.task_updated.emit(job_id)
        elif item.status == TaskStatus.RUNNING:
            self.cancel_current()

    def cancel_all(self) -> None:
        for jid in self._order:
            item = self._tasks.get(jid)
            if item and item.status == TaskStatus.WAITING:
                item.status = TaskStatus.CANCELLED
                item.stage_text = "已取消"
                item.token.cancel()
                self.task_updated.emit(jid)
        self.cancel_current()

    def shutdown_and_wait(self, timeout_ms: int = 3000) -> None:
        """Cancel all tasks and wait for the running worker thread to exit cleanly (Fixes P2-2)."""
        self._shutting_down = True
        self.cancel_all()
        worker = self._current_worker
        if worker is not None and worker.isRunning():
            worker.wait(timeout_ms)

    def clear_inactive_tasks(self) -> List[str]:
        """Remove COMPLETED, FAILED, and CANCELLED tasks from the queue (Fixes P3-2)."""
        removed_ids: List[str] = []
        remaining_order: List[str] = []
        for jid in self._order:
            item = self._tasks.get(jid)
            if item and item.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                removed_ids.append(jid)
                self._tasks.pop(jid, None)
            else:
                remaining_order.append(jid)
        self._order = remaining_order
        if removed_ids:
            self.tasks_removed.emit(removed_ids)
        return removed_ids

    def retry_task(self, job_id: str) -> None:
        item = self._tasks.get(job_id)
        if not item or item.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            return
        item.token = CancelToken()
        item.status = TaskStatus.WAITING
        item.stage_text = "等待"
        item.progress = 0
        item.error_info = None
        self.task_updated.emit(job_id)
        self._schedule_next()
