"""Main application window for Moonlight Whisper (§6.1 & §6.2)."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QEvent, Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app import __app_name__, __version__
from app.config import Config
from app.core import gpu_runtime, model_manager
from app.core.errors import ErrorInfo
from app.core.gpu_runtime import GpuStatus
from app.core.pipeline import Job
from app.logger import LOG_MANAGER
from app.paths import PATHS, AppPaths
from app.ui.gpu_dialog import GpuRuntimeDialog
from app.ui.model_dialog import ModelManagerDialog
from app.ui.settings_dialog import SettingsDialog
from app.ui.widgets import (
    DropInputTextEdit,
    GpuStatusIndicator,
    TaskQueueTable,
    open_path_in_os,
)
from app.workers import PlaylistExpandWorker, TaskQueue, YtDlpUpdateWorker

LANGUAGE_OPTIONS = [
    ("自动检测", None),
    ("中文 (zh)", "zh"),
    ("英语 (en)", "en"),
    ("日语 (ja)", "ja"),
    ("韩语 (ko)", "ko"),
    ("法语 (fr)", "fr"),
    ("德语 (de)", "de"),
    ("西班牙语 (es)", "es"),
    ("俄语 (ru)", "ru"),
]


class MainWindow(QMainWindow):
    """Primary GUI window implementing the exact layout and interactive behaviors in §6."""

    def __init__(self, cfg: Config, paths: AppPaths = PATHS) -> None:
        super().__init__()
        self.cfg = cfg
        self.paths = paths
        self.queue = TaskQueue(cfg=self.cfg, paths=self.paths, parent=self)
        self._expand_worker: Optional[PlaylistExpandWorker] = None
        self._ytdlp_worker: Optional[YtDlpUpdateWorker] = None
        self._suppress_gpu_toggle = False

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.setMinimumSize(780, 600)
        self.setAcceptDrops(True)

        self._build_ui()
        self._connect_signals()
        self._sync_controls_from_config()
        self._refresh_gpu_state(initial=True)

        LOG_MANAGER.register_ui_callback(self._append_log_threadsafe)

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # 1. Top input area: [ 粘贴链接，每行一个…… ] [开始] [添加文件] [清空]
        input_row = QHBoxLayout()
        self.input_edit = DropInputTextEdit()
        input_row.addWidget(self.input_edit, 1)

        input_btn_col = QVBoxLayout()
        self.btn_start = QPushButton("开始")
        self.btn_start.setMinimumHeight(34)
        self.btn_start.setStyleSheet("font-weight: bold;")
        self.btn_add_files = QPushButton("添加本地文件...")
        self.btn_clear = QPushButton("清空")

        input_btn_col.addWidget(self.btn_start)
        input_btn_col.addWidget(self.btn_add_files)
        input_btn_col.addWidget(self.btn_clear)
        input_row.addLayout(input_btn_col)
        root.addLayout(input_row)

        root.addWidget(self._make_hline())

        # 2. Options panel
        opts_layout = QVBoxLayout()
        opts_layout.setSpacing(8)

        row_model_gpu = QHBoxLayout()
        row_model_gpu.addWidget(QLabel("模型"))
        self.combo_model = QComboBox()
        for spec in model_manager.SUPPORTED_MODELS:
            self.combo_model.addItem(spec.name, spec.name)
        row_model_gpu.addWidget(self.combo_model)

        self.btn_models = QPushButton("模型管理...")
        row_model_gpu.addWidget(self.btn_models)

        row_model_gpu.addSpacing(12)
        row_model_gpu.addWidget(QLabel("语言"))
        self.combo_lang = QComboBox()
        for label, code in LANGUAGE_OPTIONS:
            self.combo_lang.addItem(label, code)
        row_model_gpu.addWidget(self.combo_lang)

        row_model_gpu.addSpacing(16)
        self.chk_gpu = QCheckBox("GPU 加速（NVIDIA）")
        row_model_gpu.addWidget(self.chk_gpu)

        row_model_gpu.addWidget(QLabel("状态："))
        self.gpu_indicator = GpuStatusIndicator()
        row_model_gpu.addWidget(self.gpu_indicator)
        row_model_gpu.addStretch(1)
        opts_layout.addLayout(row_model_gpu)

        row_out = QHBoxLayout()
        row_out.addWidget(QLabel("输出目录"))
        self.edit_output_dir = QLineEdit()
        self.btn_browse_out = QPushButton("浏览")
        self.btn_open_out = QPushButton("打开")
        row_out.addWidget(self.edit_output_dir, 1)
        row_out.addWidget(self.btn_browse_out)
        row_out.addWidget(self.btn_open_out)
        opts_layout.addLayout(row_out)

        row_flags = QHBoxLayout()
        self.chk_keep_audio = QCheckBox("保留音频")
        self.chk_write_srt = QCheckBox("同时输出 SRT")
        self.chk_to_simplified = QCheckBox("繁体转简体")
        self.chk_allow_playlist = QCheckBox("允许播放列表/多 P")
        row_flags.addWidget(self.chk_keep_audio)
        row_flags.addSpacing(12)
        row_flags.addWidget(self.chk_write_srt)
        row_flags.addSpacing(12)
        row_flags.addWidget(self.chk_to_simplified)
        row_flags.addSpacing(12)
        row_flags.addWidget(self.chk_allow_playlist)
        row_flags.addStretch(1)
        opts_layout.addLayout(row_flags)

        root.addLayout(opts_layout)
        root.addWidget(self._make_hline())

        # 3. Task Queue
        lbl_queue = QLabel("任务队列（双击已完成任务可直接打开 Markdown 文档）")
        root.addWidget(lbl_queue)
        self.queue_table = TaskQueueTable()
        root.addWidget(self.queue_table, 1)

        # 4. Collapsible Log Area
        log_header_row = QHBoxLayout()
        self.btn_toggle_log = QToolButton()
        self.btn_toggle_log.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_toggle_log.setArrowType(Qt.ArrowType.RightArrow)
        self.btn_toggle_log.setText(" 实时日志（点击展开/折叠）")
        self.btn_toggle_log.setCheckable(True)
        log_header_row.addWidget(self.btn_toggle_log)
        log_header_row.addStretch(1)
        root.addLayout(log_header_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1500)
        self.log_view.setFixedHeight(140)
        self.log_view.setVisible(False)
        root.addWidget(self.log_view)

        root.addWidget(self._make_hline())

        # 5. Bottom action bar: [取消当前] [取消全部] [清理已结束]      设置 | 更新 yt-dlp | 关于
        bottom_row = QHBoxLayout()
        self.btn_cancel_current = QPushButton("取消当前")
        self.btn_cancel_all = QPushButton("取消全部")
        self.btn_clear_inactive = QPushButton("清理已结束")
        bottom_row.addWidget(self.btn_cancel_current)
        bottom_row.addWidget(self.btn_cancel_all)
        bottom_row.addWidget(self.btn_clear_inactive)
        bottom_row.addStretch(1)

        self.btn_settings = QPushButton("设置")
        self.btn_update_ytdlp = QPushButton("更新 yt-dlp")
        self.btn_about = QPushButton("关于")
        bottom_row.addWidget(self.btn_settings)
        bottom_row.addWidget(self.btn_update_ytdlp)
        bottom_row.addWidget(self.btn_about)
        root.addLayout(bottom_row)

        self.tray_icon = QSystemTrayIcon(self)
        if not self.windowIcon().isNull():
            self.tray_icon.setIcon(self.windowIcon())

    @staticmethod
    def _make_hline() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _connect_signals(self) -> None:
        self.btn_start.clicked.connect(self._on_start_clicked)
        self.btn_add_files.clicked.connect(self._on_add_local_files)
        self.btn_clear.clicked.connect(self.input_edit.clear)

        self.combo_model.currentIndexChanged.connect(self._on_options_changed)
        self.combo_lang.currentIndexChanged.connect(self._on_options_changed)
        self.chk_gpu.toggled.connect(self._on_gpu_toggled)
        self.gpu_indicator.install_clicked.connect(self._open_gpu_dialog)
        self.btn_models.clicked.connect(self._open_model_dialog)

        self.edit_output_dir.editingFinished.connect(self._on_output_dir_edited)
        self.btn_browse_out.clicked.connect(self._browse_output_dir)
        self.btn_open_out.clicked.connect(lambda: open_path_in_os(self.paths.output_dir, False))

        self.chk_keep_audio.toggled.connect(self._on_options_changed)
        self.chk_write_srt.toggled.connect(self._on_options_changed)
        self.chk_to_simplified.toggled.connect(self._on_options_changed)
        self.chk_allow_playlist.toggled.connect(self._on_options_changed)

        self.btn_toggle_log.toggled.connect(self._on_toggle_log)

        self.btn_cancel_current.clicked.connect(self.queue.cancel_current)
        self.btn_cancel_all.clicked.connect(self.queue.cancel_all)
        self.btn_clear_inactive.clicked.connect(self.queue.clear_inactive_tasks)
        self.btn_settings.clicked.connect(self._open_settings_dialog)
        self.btn_update_ytdlp.clicked.connect(self._update_ytdlp_inline)
        self.btn_about.clicked.connect(self._show_about_dialog)

        self.queue.task_added.connect(self.queue_table.add_task_row)
        self.queue.task_updated.connect(self._on_queue_task_updated)
        self.queue.tasks_removed.connect(self.queue_table.remove_tasks)
        self.queue.task_completed_notification.connect(self._on_task_completed_notify)
        self.queue_table.retry_requested.connect(self.queue.retry_task)
        self.queue_table.cancel_requested.connect(self.queue.cancel_task)

    def _sync_controls_from_config(self) -> None:
        self._suppress_gpu_toggle = True
        try:
            idx_m = self.combo_model.findData(self.cfg.model)
            if idx_m >= 0:
                self.combo_model.setCurrentIndex(idx_m)

            idx_l = self.combo_lang.findData(self.cfg.language)
            if idx_l >= 0:
                self.combo_lang.setCurrentIndex(idx_l)

            self.chk_gpu.setChecked(self.cfg.use_gpu)
            self.edit_output_dir.setText(str(self.paths.output_dir))
            self.chk_keep_audio.setChecked(self.cfg.keep_audio)
            self.chk_write_srt.setChecked(self.cfg.write_srt)
            self.chk_to_simplified.setChecked(self.cfg.to_simplified)
            self.chk_allow_playlist.setChecked(self.cfg.allow_playlist)

            self.btn_toggle_log.setChecked(self.cfg.ui.log_expanded)
            self._on_toggle_log(self.cfg.ui.log_expanded)
        finally:
            self._suppress_gpu_toggle = False

    def _refresh_gpu_state(self, initial: bool = False) -> None:
        info = gpu_runtime.detect(self.paths)
        self.gpu_indicator.update_from_info(info)
        if initial and self.cfg.use_gpu and info.status != GpuStatus.READY:
            self._suppress_gpu_toggle = True
            self.chk_gpu.setChecked(False)
            self.cfg.use_gpu = False
            self._suppress_gpu_toggle = False

    @Slot(bool)
    def _on_gpu_toggled(self, checked: bool) -> None:
        if self._suppress_gpu_toggle:
            return
        if not checked:
            self.cfg.use_gpu = False
            self.cfg.save(paths=self.paths)
            return

        info = gpu_runtime.detect(self.paths)
        self.gpu_indicator.update_from_info(info)

        if info.status == GpuStatus.READY:
            self.cfg.use_gpu = True
            self.cfg.save(paths=self.paths)
            return

        if info.status in (GpuStatus.NO_DRIVER, GpuStatus.DRIVER_TOO_OLD):
            QMessageBox.warning(self, "无法启用 GPU 加速", info.message)
            self._suppress_gpu_toggle = True
            self.chk_gpu.setChecked(False)
            self._suppress_gpu_toggle = False
            self.cfg.use_gpu = False
            self.cfg.save(paths=self.paths)
            return

        dlg = GpuRuntimeDialog(cfg=self.cfg, gpu_info=info, paths=self.paths, parent=self)
        dlg.exec()
        updated_info = gpu_runtime.detect(self.paths)
        self.gpu_indicator.update_from_info(updated_info)

        if updated_info.status == GpuStatus.READY:
            self.cfg.use_gpu = True
            self.cfg.save(paths=self.paths)
        else:
            self._suppress_gpu_toggle = True
            self.chk_gpu.setChecked(False)
            self._suppress_gpu_toggle = False
            self.cfg.use_gpu = False
            self.cfg.save(paths=self.paths)

    def _open_gpu_dialog(self) -> None:
        info = gpu_runtime.detect(self.paths)
        dlg = GpuRuntimeDialog(cfg=self.cfg, gpu_info=info, paths=self.paths, parent=self)
        if dlg.exec() == GpuRuntimeDialog.DialogCode.Accepted:
            updated = gpu_runtime.detect(self.paths)
            self.gpu_indicator.update_from_info(updated)
            if updated.status == GpuStatus.READY:
                self._suppress_gpu_toggle = True
                self.chk_gpu.setChecked(True)
                self._suppress_gpu_toggle = False
                self.cfg.use_gpu = True
                self.cfg.save(paths=self.paths)

    def _open_model_dialog(self) -> None:
        dlg = ModelManagerDialog(cfg=self.cfg, paths=self.paths, parent=self)
        dlg.exec()

    def _open_settings_dialog(self) -> None:
        dlg = SettingsDialog(cfg=self.cfg, paths=self.paths, parent=self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self._sync_controls_from_config()
            self._refresh_gpu_state(initial=False)
            apply_theme(QApplication.instance(), self.cfg.ui.theme)

    def _on_options_changed(self) -> None:
        if self._suppress_gpu_toggle:
            return
        self.cfg.model = str(self.combo_model.currentData() or "large-v3-turbo")
        self.cfg.language = self.combo_lang.currentData()
        self.cfg.keep_audio = self.chk_keep_audio.isChecked()
        self.cfg.write_srt = self.chk_write_srt.isChecked()
        self.cfg.to_simplified = self.chk_to_simplified.isChecked()
        self.cfg.allow_playlist = self.chk_allow_playlist.isChecked()
        self.cfg.save(paths=self.paths)

    def _on_output_dir_edited(self) -> None:
        val = self.edit_output_dir.text().strip()
        if val:
            self.cfg.output_dir = val
            self.cfg.save(paths=self.paths)

    def _browse_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录", str(self.paths.output_dir))
        if folder:
            self.edit_output_dir.setText(folder)
            self.cfg.output_dir = folder
            self.cfg.save(paths=self.paths)

    def _on_toggle_log(self, expanded: bool) -> None:
        self.log_view.setVisible(expanded)
        self.btn_toggle_log.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.cfg.ui.log_expanded = expanded
        self.cfg.save(paths=self.paths)

    def _on_add_local_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择本地音视频文件",
            "",
            "音视频文件 (*.mp3 *.m4a *.mp4 *.mkv *.wav *.flac *.webm *.mov *.ogg *.aac *.wma *.avi);;所有文件 (*)",
        )
        if not files:
            return
        cur = self.input_edit.toPlainText().strip()
        joined = "\n".join(files)
        self.input_edit.setPlainText(f"{cur}\n{joined}".strip() if cur else joined)

    def _on_start_clicked(self) -> None:
        raw_text = self.input_edit.toPlainText().strip()
        if not raw_text:
            QMessageBox.information(self, "提示", "请先粘贴至少一条视频链接或拖入本地音视频文件。")
            return

        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not lines:
            return

        self.input_edit.clear()

        if self.cfg.allow_playlist and any(not Job(source=l).is_local_file for l in lines):
            self.btn_start.setEnabled(False)
            self.btn_start.setText("解析中...")
            worker = PlaylistExpandWorker(raw_inputs=lines, cfg=self.cfg, paths=self.paths, parent=self)
            worker.jobs_ready.connect(self._on_expanded_jobs_ready)
            self._expand_worker = worker
            worker.start()
        else:
            jobs = [Job(source=l) for l in lines]
            self.queue.enqueue_jobs(jobs)

    def _on_expanded_jobs_ready(self, jobs: List[Job]) -> None:
        self._expand_worker = None
        self.btn_start.setEnabled(True)
        self.btn_start.setText("开始")
        if jobs:
            self.queue.enqueue_jobs(jobs)

    def _on_queue_task_updated(self, job_id: str) -> None:
        item = self.queue.get_task(job_id)
        if item:
            self.queue_table.update_task_row(item)

    def _on_task_completed_notify(self, title: str, md_path_str: str) -> None:
        if self.tray_icon.isSystemTrayAvailable():
            self.tray_icon.show()
            self.tray_icon.showMessage(
                "转录完成 - Moonlight Whisper",
                f"《{title}》已输出至：{Path(md_path_str).name}",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def _append_log_threadsafe(self, msg: str) -> None:
        app = QApplication.instance()
        if app is not None:
            app.postEvent(self, _LogEvent(msg))

    def customEvent(self, event) -> None:
        if isinstance(event, _LogEvent):
            self.log_view.appendPlainText(event.message)
        else:
            super().customEvent(event)

    def _update_ytdlp_inline(self) -> None:
        self.btn_update_ytdlp.setEnabled(False)
        self.btn_update_ytdlp.setText("更新中...")
        worker = YtDlpUpdateWorker(
            pypi_mirror=self.cfg.pypi_mirror,
            proxy=self.cfg.proxy,
            paths=self.paths,
            parent=self,
        )
        worker.update_succeeded.connect(self._on_inline_ytdlp_ok)
        worker.update_failed.connect(self._on_inline_ytdlp_fail)
        self._ytdlp_worker = worker
        worker.start()

    def _on_inline_ytdlp_ok(self, ver: str) -> None:
        self._ytdlp_worker = None
        self.btn_update_ytdlp.setEnabled(True)
        self.btn_update_ytdlp.setText("更新 yt-dlp")
        QMessageBox.information(self, "更新完成", f"yt-dlp 已更新至最新版本：{ver}")

    def _on_inline_ytdlp_fail(self, err: ErrorInfo) -> None:
        self._ytdlp_worker = None
        self.btn_update_ytdlp.setEnabled(True)
        self.btn_update_ytdlp.setText("更新 yt-dlp")
        QMessageBox.warning(self, "更新失败", f"{err.user_message}\n\n{err.detail}")

    def _show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            f"关于 {__app_name__}",
            f"<h3>{__app_name__} v{__version__}</h3>"
            "<p>基于 <b>yt-dlp</b> 与 <b>faster-whisper (CTranslate2)</b> 的桌面端视频转录工具。</p>"
            "<ul>"
            "<li>支持粘贴视频链接或拖入本地音视频一键生成结构化 Markdown 与 SRT 字幕</li>"
            "<li>支持 NVIDIA CUDA 12 运行库按需下载与 CPU 自动回退</li>"
            "<li>支持可选 OpenAI 兼容接口自动生成内容摘要</li>"
            "</ul>"
            "<p><small>仅供个人学习与研究使用，请遵守各视频站点服务条款与版权法规。</small></p>",
        )

    def dragEnterEvent(self, event) -> None:
        self.input_edit.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        self.input_edit.dropEvent(event)

    def closeEvent(self, event) -> None:
        if self.queue.is_running:
            ans = QMessageBox.question(
                self,
                "确认退出",
                "当前仍有转录任务正在进行中，确定要取消任务并退出程序吗？",
            )
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        LOG_MANAGER.unregister_ui_callback(self._append_log_threadsafe)
        self.queue.shutdown_and_wait(timeout_ms=3000)
        if self._expand_worker and self._expand_worker.isRunning():
            self._expand_worker.token.cancel()
            self._expand_worker.wait(1500)
        super().closeEvent(event)


class _LogEvent(QEvent):
    EVENT_TYPE = QEvent.Type(QEvent.registerEventType())

    def __init__(self, message: str) -> None:
        super().__init__(self.EVENT_TYPE)
        self.message = message


def apply_theme(app: Optional[QApplication], theme: str) -> None:
    """Apply system, dark, or light palette/stylesheet."""
    if app is None:
        return
    if theme == "dark":
        app.setStyleSheet(
            """
            QWidget { background-color: #1e222d; color: #e5e9f0; font-size: 13px; }
            QPlainTextEdit, QLineEdit, QTableWidget, QComboBox, QSpinBox {
                background-color: #282c37; color: #eceff4; border: 1px solid #3b4252; border-radius: 4px; padding: 4px;
            }
            QPushButton {
                background-color: #3b4252; color: #eceff4; border: 1px solid #4c566a; border-radius: 4px; padding: 5px 12px;
            }
            QPushButton:hover { background-color: #434c5e; }
            QHeaderView::section { background-color: #2e3440; color: #d8dee9; padding: 4px; border: 1px solid #3b4252; }
            QProgressBar { border: 1px solid #4c566a; border-radius: 4px; text-align: center; }
            QProgressBar::chunk { background-color: #5e81ac; }
            """
        )
    elif theme == "light":
        app.setStyleSheet(
            """
            QWidget { font-size: 13px; }
            """
        )
    else:
        app.setStyleSheet("")
