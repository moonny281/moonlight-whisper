"""Windows 11 Mica Style MainWindow for Moonlight Whisper v1.2.0 (overlap-proof layout)."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
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
from app.core import downloader, gpu_runtime, model_manager
from app.core.errors import ErrorInfo
from app.core.gpu_runtime import GpuStatus
from app.core.pipeline import Job
from app.logger import LOG_MANAGER
from app.paths import PATHS, AppPaths
from app.ui.gpu_dialog import GpuRuntimeDialog
from app.ui.model_dialog import ModelManagerDialog
from app.ui.settings_dialog import SettingsDialog
from app.ui.theme import apply_theme, apply_win11_mica_window_attributes, resolve_effective_theme
from app.ui.widgets import (
    DropInputTextEdit,
    GpuStatusIndicator,
    TaskQueueTable,
    make_card_frame,
    open_path_in_os,
)
from app.workers import PlaylistExpandWorker, TaskQueue, YtDlpUpdateWorker

LANGUAGE_OPTIONS = [
    ("自动检测 (Auto)", None),
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
    """Windows 11 Mica-styled main window with spacious, DPI-safe non-overlapping layout."""

    _log_signal = Signal(str)

    def __init__(self, cfg: Config, paths: AppPaths = PATHS) -> None:
        super().__init__()
        self.cfg = cfg
        self.paths = paths
        self.queue = TaskQueue(cfg=self.cfg, paths=self.paths, parent=self)
        self._expand_worker: Optional[PlaylistExpandWorker] = None
        self._ytdlp_worker: Optional[YtDlpUpdateWorker] = None
        self._suppress_option_sync = False

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.setMinimumSize(920, 700)
        self.setAcceptDrops(True)

        self._build_ui()
        self._connect_signals()
        self._refresh_model_combo_labels()
        self._sync_controls_from_config()
        self._refresh_gpu_state(initial=True)
        self._refresh_queue_stats()

        self._log_signal.connect(self._append_log_on_main_thread, Qt.ConnectionType.QueuedConnection)
        LOG_MANAGER.register_ui_callback(self._emit_log_from_any_thread)

        eff = resolve_effective_theme(self.cfg.ui.theme)
        apply_win11_mica_window_attributes(self, is_dark=(eff == "dark"))

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # === 1. Top Mica Header Bar ===
        header_row = QHBoxLayout()
        header_row.setSpacing(12)

        self.logo_label = QLabel()
        logo_png = self.paths.assets_dir / "moon-512.png"
        logo_ico = self.paths.assets_dir / "app.ico"
        if logo_png.exists():
            pix = QPixmap(str(logo_png)).scaled(
                36, 36, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            self.logo_label.setPixmap(pix)
        elif logo_ico.exists():
            self.logo_label.setPixmap(QIcon(str(logo_ico)).pixmap(36, 36))
        header_row.addWidget(self.logo_label)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_line = QHBoxLayout()
        title_line.setSpacing(8)
        lbl_title = QLabel(__app_name__)
        lbl_title.setObjectName("AppTitle")
        lbl_ver = QLabel(f"v{__version__}")
        lbl_ver.setObjectName("VersionBadge")
        title_line.addWidget(lbl_title)
        title_line.addWidget(lbl_ver)
        title_line.addStretch(1)

        lbl_sub = QLabel("基于 yt-dlp 与 faster-whisper 的音视频智能转录工作台")
        lbl_sub.setObjectName("AppSubtitle")
        title_col.addLayout(title_line)
        title_col.addWidget(lbl_sub)
        header_row.addLayout(title_col)

        header_row.addStretch(1)

        self.gpu_indicator = GpuStatusIndicator()
        header_row.addWidget(self.gpu_indicator)

        self.btn_theme = QPushButton("切换主题")
        self.btn_theme.setToolTip("在深色云母与浅色云母主题之间切换")
        self.btn_models_top = QPushButton("模型管理")
        self.btn_settings_top = QPushButton("偏好设置")
        self.btn_about_top = QPushButton("关于")
        self.btn_about_top.setObjectName("GhostButton")

        header_row.addWidget(self.btn_theme)
        header_row.addWidget(self.btn_models_top)
        header_row.addWidget(self.btn_settings_top)
        header_row.addWidget(self.btn_about_top)
        root.addLayout(header_row)

        # === 2. Card 1: Input & Drag-and-Drop Zone ===
        input_card = make_card_frame(self)
        input_card_layout = QVBoxLayout(input_card)
        input_card_layout.setContentsMargins(16, 14, 16, 14)
        input_card_layout.setSpacing(10)

        input_top_bar = QHBoxLayout()
        input_top_bar.setSpacing(10)
        lbl_input_title = QLabel("添加视频链接或本地音视频")
        lbl_input_title.setObjectName("SectionTitle")
        self.lbl_line_count = QLabel("待添加 0 项")
        self.lbl_line_count.setObjectName("StatPill")

        self.btn_paste = QPushButton("粘贴剪贴板")
        self.btn_add_files = QPushButton("选择本地文件...")
        self.btn_clear = QPushButton("清空输入")
        self.btn_start = QPushButton("▶ 加入队列并开始转录")
        self.btn_start.setObjectName("PrimaryButton")

        input_top_bar.addWidget(lbl_input_title)
        input_top_bar.addWidget(self.lbl_line_count)
        input_top_bar.addStretch(1)
        input_top_bar.addWidget(self.btn_paste)
        input_top_bar.addWidget(self.btn_add_files)
        input_top_bar.addWidget(self.btn_clear)
        input_top_bar.addWidget(self.btn_start)
        input_card_layout.addLayout(input_top_bar)

        self.input_edit = DropInputTextEdit()
        input_card_layout.addWidget(self.input_edit)
        root.addWidget(input_card)

        # === 3. Card 2: Spacious Configuration Panel (2 distinct rows + 3x2 checkbox grid) ===
        cfg_card = make_card_frame(self)
        cfg_layout = QVBoxLayout(cfg_card)
        cfg_layout.setContentsMargins(16, 14, 16, 14)
        cfg_layout.setSpacing(12)

        # Row A: Model + Language + Output Directory
        row_selectors = QHBoxLayout()
        row_selectors.setSpacing(10)

        lbl_m = QLabel("转录模型")
        lbl_m.setObjectName("FieldLabel")
        self.combo_model = QComboBox()
        self.combo_model.setMinimumWidth(190)
        for spec in model_manager.SUPPORTED_MODELS:
            self.combo_model.addItem(spec.name, spec.name)

        lbl_l = QLabel("识别语言")
        lbl_l.setObjectName("FieldLabel")
        self.combo_lang = QComboBox()
        self.combo_lang.setMinimumWidth(135)
        for label, code in LANGUAGE_OPTIONS:
            self.combo_lang.addItem(label, code)

        lbl_out = QLabel("输出目录")
        lbl_out.setObjectName("FieldLabel")
        self.edit_output_dir = QLineEdit()
        self.btn_browse_out = QPushButton("浏览...")
        self.btn_open_out = QPushButton("打开目录")

        row_selectors.addWidget(lbl_m)
        row_selectors.addWidget(self.combo_model)
        row_selectors.addSpacing(8)
        row_selectors.addWidget(lbl_l)
        row_selectors.addWidget(self.combo_lang)
        row_selectors.addSpacing(8)
        row_selectors.addWidget(lbl_out)
        row_selectors.addWidget(self.edit_output_dir, 1)
        row_selectors.addWidget(self.btn_browse_out)
        row_selectors.addWidget(self.btn_open_out)
        cfg_layout.addLayout(row_selectors)

        # Row B: Sub-card with a spacious 3x2 Grid of feature toggles (zero crowding/overlap)
        toggles_subcard = make_card_frame(cfg_card, sub_card=True)
        toggles_grid = QGridLayout(toggles_subcard)
        toggles_grid.setContentsMargins(14, 10, 14, 10)
        toggles_grid.setHorizontalSpacing(28)
        toggles_grid.setVerticalSpacing(8)

        self.chk_gpu = QCheckBox("启用 NVIDIA GPU 加速 (CUDA 12)")
        self.chk_to_simplified = QCheckBox("繁体中文自动转简体 (OpenCC)")
        self.chk_write_srt = QCheckBox("同时导出 SRT 字幕文件 (.srt)")
        self.chk_keep_audio = QCheckBox("保留下载的原始音频文件")
        self.chk_allow_playlist = QCheckBox("允许解析播放列表 / 多 P 视频")
        self.chk_llm_summary = QCheckBox("启用 AI 自动生成摘要 (OpenAI 兼容)")

        toggles_grid.addWidget(self.chk_gpu, 0, 0)
        toggles_grid.addWidget(self.chk_to_simplified, 0, 1)
        toggles_grid.addWidget(self.chk_write_srt, 0, 2)
        toggles_grid.addWidget(self.chk_keep_audio, 1, 0)
        toggles_grid.addWidget(self.chk_allow_playlist, 1, 1)
        toggles_grid.addWidget(self.chk_llm_summary, 1, 2)
        toggles_grid.setColumnStretch(0, 1)
        toggles_grid.setColumnStretch(1, 1)
        toggles_grid.setColumnStretch(2, 1)

        cfg_layout.addWidget(toggles_subcard)
        root.addWidget(cfg_card)

        # === 4. Card 3: Task Queue & Batch Controls ===
        queue_card = make_card_frame(self)
        queue_layout = QVBoxLayout(queue_card)
        queue_layout.setContentsMargins(16, 14, 16, 14)
        queue_layout.setSpacing(10)

        queue_top = QHBoxLayout()
        queue_top.setSpacing(8)
        lbl_q_title = QLabel("任务队列")
        lbl_q_title.setObjectName("SectionTitle")
        self.lbl_queue_stats = QLabel("总计 0  ·  进行中 0  ·  等待 0  ·  已完成 0")
        self.lbl_queue_stats.setObjectName("StatPill")

        self.btn_retry_failed = QPushButton("重试失败项")
        self.btn_clear_inactive = QPushButton("清理已结束")
        self.btn_cancel_current = QPushButton("取消当前")
        self.btn_cancel_all = QPushButton("取消全部")
        self.btn_cancel_all.setObjectName("DangerButton")

        queue_top.addWidget(lbl_q_title)
        queue_top.addWidget(self.lbl_queue_stats)
        queue_top.addStretch(1)
        queue_top.addWidget(self.btn_retry_failed)
        queue_top.addWidget(self.btn_clear_inactive)
        queue_top.addWidget(self.btn_cancel_current)
        queue_top.addWidget(self.btn_cancel_all)
        queue_layout.addLayout(queue_top)

        self.queue_table = TaskQueueTable()
        queue_layout.addWidget(self.queue_table, 1)
        root.addWidget(queue_card, 1)

        # === 5. Bottom Collapsible Console Log & Maintenance Bar ===
        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(8)
        self.btn_toggle_log = QToolButton()
        self.btn_toggle_log.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_toggle_log.setArrowType(Qt.ArrowType.RightArrow)
        self.btn_toggle_log.setText(" 实时控制台日志")
        self.btn_toggle_log.setCheckable(True)

        self.btn_copy_log = QPushButton("复制日志")
        self.btn_copy_log.setObjectName("GhostButton")
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_clear_log.setObjectName("GhostButton")
        self.btn_open_logs_dir = QPushButton("日志文件夹")
        self.btn_open_logs_dir.setObjectName("GhostButton")
        self.btn_update_ytdlp = QPushButton("更新 yt-dlp")

        bottom_bar.addWidget(self.btn_toggle_log)
        bottom_bar.addWidget(self.btn_copy_log)
        bottom_bar.addWidget(self.btn_clear_log)
        bottom_bar.addWidget(self.btn_open_logs_dir)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.btn_update_ytdlp)
        root.addLayout(bottom_bar)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("ConsoleLogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(140)
        self.log_view.setVisible(False)
        root.addWidget(self.log_view)

        self.tray_icon = QSystemTrayIcon(self)
        if not self.windowIcon().isNull():
            self.tray_icon.setIcon(self.windowIcon())

    def _connect_signals(self) -> None:
        self.btn_start.clicked.connect(self._on_start_clicked)
        self.btn_paste.clicked.connect(self._on_paste_clicked)
        self.btn_add_files.clicked.connect(self._on_add_local_files)
        self.btn_clear.clicked.connect(self.input_edit.clear)
        self.input_edit.line_count_changed.connect(
            lambda n: self.lbl_line_count.setText(f"待添加 {n} 项")
        )

        self.btn_theme.clicked.connect(self._toggle_theme_quick)
        self.btn_models_top.clicked.connect(self._open_model_dialog)
        self.btn_settings_top.clicked.connect(lambda: self._open_settings_dialog(initial_tab=0))
        self.btn_about_top.clicked.connect(self._show_about_dialog)

        self.combo_model.currentIndexChanged.connect(self._on_options_changed)
        self.combo_lang.currentIndexChanged.connect(self._on_options_changed)
        self.chk_gpu.toggled.connect(self._on_gpu_toggled)
        self.gpu_indicator.install_clicked.connect(self._open_gpu_dialog)

        self.edit_output_dir.editingFinished.connect(self._on_output_dir_edited)
        self.btn_browse_out.clicked.connect(self._browse_output_dir)
        self.btn_open_out.clicked.connect(lambda: open_path_in_os(self.paths.output_dir, False))

        self.chk_keep_audio.toggled.connect(self._on_options_changed)
        self.chk_write_srt.toggled.connect(self._on_options_changed)
        self.chk_to_simplified.toggled.connect(self._on_options_changed)
        self.chk_allow_playlist.toggled.connect(self._on_options_changed)
        self.chk_llm_summary.toggled.connect(self._on_llm_summary_toggled)

        self.btn_toggle_log.toggled.connect(self._on_toggle_log)
        self.btn_copy_log.clicked.connect(self._copy_logs_to_clipboard)
        self.btn_clear_log.clicked.connect(self.log_view.clear)
        self.btn_open_logs_dir.clicked.connect(lambda: open_path_in_os(self.paths.logs_dir, False))
        self.btn_update_ytdlp.clicked.connect(self._update_ytdlp_inline)

        self.btn_cancel_current.clicked.connect(self.queue.cancel_current)
        self.btn_cancel_all.clicked.connect(self.queue.cancel_all)
        self.btn_retry_failed.clicked.connect(self._on_retry_all_failed)
        self.btn_clear_inactive.clicked.connect(self._on_clear_inactive)

        self.queue.task_added.connect(self._on_queue_task_added)
        self.queue.task_updated.connect(self._on_queue_task_updated)
        self.queue.tasks_removed.connect(self._on_queue_tasks_removed)
        self.queue.task_completed_notification.connect(self._on_task_completed_notify)
        self.queue_table.retry_requested.connect(self.queue.retry_task)
        self.queue_table.cancel_requested.connect(self.queue.cancel_task)

    def _refresh_model_combo_labels(self) -> None:
        statuses = {st.spec.name: st.installed for st in model_manager.list_models(self.paths)}
        for i in range(self.combo_model.count()):
            name = str(self.combo_model.itemData(i))
            label = f"✓ {name} (已就绪)" if statuses.get(name) else f"⬇ {name} (按需下载)"
            self.combo_model.setItemText(i, label)

    def _sync_controls_from_config(self) -> None:
        self._suppress_option_sync = True
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
            self.chk_llm_summary.setChecked(self.cfg.llm.enabled)

            effective = resolve_effective_theme(self.cfg.ui.theme)
            self.btn_theme.setText("☀ 浅色云母" if effective == "dark" else "🌙 深色云母")

            self.btn_toggle_log.setChecked(self.cfg.ui.log_expanded)
            self._on_toggle_log(self.cfg.ui.log_expanded)
        finally:
            self._suppress_option_sync = False

    def _toggle_theme_quick(self) -> None:
        current_eff = resolve_effective_theme(self.cfg.ui.theme)
        new_theme = "light" if current_eff == "dark" else "dark"
        self.cfg.ui.theme = new_theme
        self.cfg.save(paths=self.paths)
        apply_theme(QApplication.instance(), new_theme, window=self)
        self.btn_theme.setText("☀ 浅色云母" if new_theme == "dark" else "🌙 深色云母")

    def _refresh_gpu_state(self, initial: bool = False) -> None:
        info = gpu_runtime.detect(self.paths)
        self.gpu_indicator.update_from_info(info)
        if initial and self.cfg.use_gpu and info.status != GpuStatus.READY:
            self._suppress_option_sync = True
            self.chk_gpu.setChecked(False)
            self.cfg.use_gpu = False
            self._suppress_option_sync = False

    def _refresh_queue_stats(self) -> None:
        st = self.queue.stats()
        self.lbl_queue_stats.setText(
            f"总计 {st['total']}  ·  进行中 {st['running']}  ·  等待 {st['waiting']}  ·  "
            f"已完成 {st['completed']}  ·  失败/取消 {st['failed']}"
        )

    @Slot(bool)
    def _on_gpu_toggled(self, checked: bool) -> None:
        if self._suppress_option_sync:
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
            self._suppress_option_sync = True
            self.chk_gpu.setChecked(False)
            self._suppress_option_sync = False
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
            self._suppress_option_sync = True
            self.chk_gpu.setChecked(False)
            self._suppress_option_sync = False
            self.cfg.use_gpu = False
            self.cfg.save(paths=self.paths)

    @Slot(bool)
    def _on_llm_summary_toggled(self, checked: bool) -> None:
        if self._suppress_option_sync:
            return
        if checked and not self.cfg.llm.api_key.strip():
            ans = QMessageBox.question(
                self,
                "配置 AI 自动摘要接口",
                "启用 AI 自动摘要需要先配置 OpenAI 兼容 API Key（支持 OpenAI / DeepSeek / Qwen 等）。\n是否现在打开设置页面进行配置？",
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._open_settings_dialog(initial_tab=2)
            if not self.cfg.llm.api_key.strip():
                self._suppress_option_sync = True
                self.chk_llm_summary.setChecked(False)
                self._suppress_option_sync = False
                self.cfg.llm.enabled = False
                self.cfg.save(paths=self.paths)
                return
        self.cfg.llm.enabled = checked
        self.cfg.save(paths=self.paths)

    def _open_gpu_dialog(self) -> None:
        info = gpu_runtime.detect(self.paths)
        dlg = GpuRuntimeDialog(cfg=self.cfg, gpu_info=info, paths=self.paths, parent=self)
        if dlg.exec() == GpuRuntimeDialog.DialogCode.Accepted:
            updated = gpu_runtime.detect(self.paths)
            self.gpu_indicator.update_from_info(updated)
            if updated.status == GpuStatus.READY:
                self._suppress_option_sync = True
                self.chk_gpu.setChecked(True)
                self._suppress_option_sync = False
                self.cfg.use_gpu = True
                self.cfg.save(paths=self.paths)

    def _open_model_dialog(self) -> None:
        dlg = ModelManagerDialog(cfg=self.cfg, paths=self.paths, parent=self)
        dlg.exec()
        self._refresh_model_combo_labels()

    def _open_settings_dialog(self, initial_tab: int = 0) -> None:
        dlg = SettingsDialog(cfg=self.cfg, paths=self.paths, initial_tab=initial_tab, parent=self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self.queue.cfg = self.cfg
            self._refresh_model_combo_labels()
            self._sync_controls_from_config()
            self._refresh_gpu_state(initial=False)
            apply_theme(QApplication.instance(), self.cfg.ui.theme, window=self)
            from app.logger import get_logger
            get_logger().info(
                "已保存偏好设置 (代理=%s, 浏览器读取[B站/TikTok优先]=%s, YouTube托管自动续期=%s)",
                self.cfg.proxy or "自动检测系统代理/直连",
                self.cfg.cookies_from_browser or "未启用",
                "开启" if self.cfg.auto_refresh_cookies else "关闭",
            )

    def _on_options_changed(self) -> None:
        if self._suppress_option_sync:
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

    def _copy_logs_to_clipboard(self) -> None:
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(self.log_view.toPlainText())

    def _on_paste_clicked(self) -> None:
        cb = QGuiApplication.clipboard()
        if cb is None:
            return
        clip_text = cb.text().strip()
        if not clip_text:
            return
        cur = self.input_edit.toPlainText().strip()
        self.input_edit.setPlainText(f"{cur}\n{clip_text}".strip() if cur else clip_text)

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

        if self.cfg.allow_playlist and any(not Job(source=l).is_local_file and downloader.is_likely_playlist_url(l) for l in lines):
            self.btn_start.setEnabled(False)
            self.btn_start.setText("正在解析播放列表...")
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
        self.btn_start.setText("▶ 加入队列并开始转录")
        if jobs:
            self.queue.enqueue_jobs(jobs)

    def _on_queue_task_added(self, item) -> None:
        self.queue_table.add_task_row(item)
        self._refresh_queue_stats()

    def _on_queue_task_updated(self, job_id: str) -> None:
        item = self.queue.get_task(job_id)
        if item:
            self.queue_table.update_task_row(item)
            if item.status.name == "FAILED" and not self.btn_toggle_log.isChecked():
                self.btn_toggle_log.setChecked(True)
        self._refresh_queue_stats()
        self._refresh_model_combo_labels()

    def _on_queue_tasks_removed(self, job_ids: List[str]) -> None:
        self.queue_table.remove_tasks(job_ids)
        self._refresh_queue_stats()

    def _on_retry_all_failed(self) -> None:
        count = self.queue.retry_failed_tasks()
        if count == 0:
            QMessageBox.information(self, "提示", "当前队列中没有失败或已取消的任务。")

    def _on_clear_inactive(self) -> None:
        self.queue.clear_inactive_tasks()
        self._refresh_queue_stats()

    def _on_task_completed_notify(self, title: str, md_path_str: str) -> None:
        if self.tray_icon.isSystemTrayAvailable():
            self.tray_icon.show()
            self.tray_icon.showMessage(
                "转录完成 - Moonlight Whisper",
                f"《{title}》已输出至：{Path(md_path_str).name}",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def _emit_log_from_any_thread(self, msg: str) -> None:
        self._log_signal.emit(msg)

    @Slot(str)
    def _append_log_on_main_thread(self, msg: str) -> None:
        self.log_view.appendPlainText(msg)

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
            "<p>基于 <b>yt-dlp</b> 与 <b>faster-whisper (CTranslate2)</b> 的 Windows 11 云母风格桌面端视频转录工作台。</p>"
            "<ul>"
            "<li>支持粘贴单个/批量链接、播放列表自动展开与本地音视频文件拖拽入队</li>"
            "<li>支持 NVIDIA CUDA 12 运行库按需流式下载、离线导入与 CPU 智能回退</li>"
            "<li>支持 OpenAI 兼容接口（OpenAI / DeepSeek / Qwen / Ollama）自动提炼摘要</li>"
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
        LOG_MANAGER.unregister_ui_callback(self._emit_log_from_any_thread)
        self.queue.shutdown_and_wait(timeout_ms=3000)
        if self._expand_worker and self._expand_worker.isRunning():
            self._expand_worker.token.cancel()
            self._expand_worker.wait(1500)
        super().closeEvent(event)
