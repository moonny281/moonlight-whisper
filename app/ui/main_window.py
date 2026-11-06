"""Windows 11 Mica Style MainWindow for Moonlight Whisper v1.2.0 (overlap-proof layout)."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QCompleter,
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
from app.core import downloader, gpu_runtime, languages, model_manager
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
    MicaBackground,
    MoonMark,
    SwitchCheckBox,
    TaskQueueTable,
    make_card_frame,
    make_card_header,
    open_path_in_os,
)
from app.workers import PlaylistExpandWorker, TaskQueue, YtDlpUpdateWorker

# Every language Whisper supports (plus Cantonese); per-model availability is applied by `_refresh_language_options`.
LANGUAGE_OPTIONS = languages.language_options()


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
        self.setMinimumSize(980, 760)
        self._apply_initial_size()
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

    def _apply_initial_size(self) -> None:
        """Open at a comfortable size but never larger than the available screen area."""
        screen = QGuiApplication.primaryScreen()
        w, h = 1200, 900
        if screen is not None:
            avail = screen.availableGeometry()
            w, h = min(w, avail.width() - 40), min(h, avail.height() - 60)
        self.resize(max(980, w), max(760, h))

    def _build_ui(self) -> None:
        central = MicaBackground(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(26, 20, 26, 14)
        root.setSpacing(14)

        # === 1. Header: brand on the left, status + actions on the right ===
        header_row = QHBoxLayout()
        header_row.setSpacing(14)

        self.logo_label = MoonMark(48)
        header_row.addWidget(self.logo_label)

        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        title_line = QHBoxLayout()
        title_line.setSpacing(10)
        lbl_title = QLabel(__app_name__)
        lbl_title.setObjectName("AppTitle")
        lbl_ver = QLabel(f"v{__version__}")
        lbl_ver.setObjectName("VersionBadge")
        title_line.addWidget(lbl_title)
        title_line.addWidget(lbl_ver, 0, Qt.AlignmentFlag.AlignVCenter)
        title_line.addStretch(1)
        lbl_sub = QLabel("把视频和音频转成带时间轴的文字")
        lbl_sub.setObjectName("AppSubtitle")
        title_col.addLayout(title_line)
        title_col.addWidget(lbl_sub)
        header_row.addLayout(title_col)
        header_row.addStretch(1)

        self.gpu_indicator = GpuStatusIndicator()
        header_row.addWidget(self.gpu_indicator)
        header_row.addSpacing(6)

        self.btn_theme = QPushButton("切换主题")
        self.btn_theme.setObjectName("IconButton")
        self.btn_theme.setToolTip("在深色与浅色云母主题之间切换")
        self.btn_models_top = QPushButton("模型管理")
        self.btn_settings_top = QPushButton("偏好设置")
        self.btn_about_top = QPushButton("关于")
        self.btn_about_top.setObjectName("GhostButton")

        header_row.addWidget(self.btn_theme)
        header_row.addWidget(self.btn_models_top)
        header_row.addWidget(self.btn_settings_top)
        header_row.addWidget(self.btn_about_top)
        root.addLayout(header_row)

        # === 2. Top row: input card (wide) + recognition card (narrow) ===
        top_row = QHBoxLayout()
        top_row.setSpacing(14)

        input_card = make_card_frame(self)
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(18, 16, 18, 16)
        input_layout.setSpacing(12)

        self.lbl_line_count = QLabel("待添加 0 项")
        self.lbl_line_count.setObjectName("StatPill")
        input_layout.addLayout(
            make_card_header("新建转录", "每行一个链接，支持 YouTube、Bilibili 等站点，也可添加本地音视频", [self.lbl_line_count])
        )

        self.input_edit = DropInputTextEdit()
        input_layout.addWidget(self.input_edit, 1)

        input_actions = QHBoxLayout()
        input_actions.setSpacing(8)
        self.btn_paste = QPushButton("粘贴剪贴板")
        self.btn_add_files = QPushButton("选择本地文件")
        self.btn_clear = QPushButton("清空")
        self.btn_clear.setObjectName("GhostButton")
        self.btn_start = QPushButton("开始转录")
        self.btn_start.setObjectName("PrimaryButton")
        self.btn_start.setMinimumWidth(150)
        input_actions.addWidget(self.btn_paste)
        input_actions.addWidget(self.btn_add_files)
        input_actions.addWidget(self.btn_clear)
        input_actions.addStretch(1)
        input_actions.addWidget(self.btn_start)
        input_layout.addLayout(input_actions)
        top_row.addWidget(input_card, 3)

        recog_card = make_card_frame(self)
        recog_card.setMinimumWidth(340)
        recog_card.setMaximumWidth(470)
        recog_layout = QVBoxLayout(recog_card)
        recog_layout.setContentsMargins(18, 16, 18, 16)
        recog_layout.setSpacing(8)
        recog_layout.addLayout(make_card_header("识别设置", "选择模型和语言，或交给语言路由自动决定"))
        recog_layout.addSpacing(4)

        lbl_m = QLabel("转录模型")
        lbl_m.setObjectName("FieldLabel")
        self.combo_model = QComboBox()
        for spec in model_manager.SUPPORTED_MODELS:
            self.combo_model.addItem(spec.name, spec.name)
        recog_layout.addWidget(lbl_m)
        recog_layout.addWidget(self.combo_model)

        lbl_l = QLabel("识别语言")
        lbl_l.setObjectName("FieldLabel")
        self.combo_lang = QComboBox()
        for label, code in LANGUAGE_OPTIONS:
            self.combo_lang.addItem(label, code)
        # Searchable: type part of a language name or code to filter the list.
        self.combo_lang.setEditable(True)
        self.combo_lang.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_lang.setMaxVisibleItems(16)
        lang_completer = self.combo_lang.completer()
        if lang_completer is not None:
            lang_completer.setFilterMode(Qt.MatchFlag.MatchContains)
            lang_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            lang_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        lang_edit = self.combo_lang.lineEdit()
        if lang_edit is not None:
            lang_edit.setPlaceholderText("输入语言名或代码筛选")
            lang_edit.editingFinished.connect(self._restore_lang_text)
        recog_layout.addSpacing(2)
        recog_layout.addWidget(lbl_l)
        recog_layout.addWidget(self.combo_lang)

        recog_layout.addSpacing(6)
        switches_card = make_card_frame(recog_card, sub_card=True)
        switches_layout = QVBoxLayout(switches_card)
        switches_layout.setContentsMargins(14, 8, 14, 8)
        switches_layout.setSpacing(0)
        self.chk_route = SwitchCheckBox("语言路由：按语言自动选模型")
        self.chk_route.setToolTip(
            "开启后：先识别（或使用你指定的）语言，再按「偏好设置 → 语言路由」中的规则选择模型。\n"
            "例如 中文/粤语 → SenseVoiceSmall，其余语言 → Whisper。\n"
            "此时「转录模型」仅在语言检测失败或不可靠时作为默认模型。"
        )
        self.chk_gpu = SwitchCheckBox("NVIDIA GPU 加速")
        self.chk_gpu.setToolTip("使用 CUDA 12 加速 Whisper 与 SenseVoiceSmall；不可用时自动回退到 CPU")
        switches_layout.addWidget(self.chk_route)
        switches_layout.addWidget(self.chk_gpu)
        recog_layout.addWidget(switches_card)
        recog_layout.addStretch(1)
        top_row.addWidget(recog_card, 2)
        root.addLayout(top_row)

        # === 3. Output & document options ===
        out_card = make_card_frame(self)
        out_layout = QVBoxLayout(out_card)
        out_layout.setContentsMargins(18, 14, 18, 14)
        out_layout.setSpacing(10)
        out_layout.addLayout(make_card_header("输出与文档"))

        out_row = QHBoxLayout()
        out_row.setSpacing(10)
        lbl_out = QLabel("输出目录")
        lbl_out.setObjectName("FieldLabel")
        self.edit_output_dir = QLineEdit()
        self.btn_browse_out = QPushButton("浏览")
        self.btn_open_out = QPushButton("打开目录")
        out_row.addWidget(lbl_out)
        out_row.addWidget(self.edit_output_dir, 1)
        out_row.addWidget(self.btn_browse_out)
        out_row.addWidget(self.btn_open_out)
        out_layout.addLayout(out_row)

        toggles_grid = QGridLayout()
        toggles_grid.setContentsMargins(0, 2, 0, 0)
        toggles_grid.setHorizontalSpacing(24)
        toggles_grid.setVerticalSpacing(2)

        self.chk_to_simplified = SwitchCheckBox("繁体转简体")
        self.chk_to_simplified.setToolTip("使用 OpenCC 把繁体中文输出转换为简体（日文汉字不受影响）")
        self.chk_write_srt = SwitchCheckBox("导出 SRT 字幕")
        self.chk_write_srt.setToolTip("同时生成与 Markdown 同名的 .srt 字幕文件")
        self.chk_keep_audio = SwitchCheckBox("保留原始音频")
        self.chk_keep_audio.setToolTip("保留下载得到的原始音频文件")
        self.chk_allow_playlist = SwitchCheckBox("解析播放列表")
        self.chk_allow_playlist.setToolTip("允许解析播放列表 / 多 P 视频并展开为多个任务")
        self.chk_llm_summary = SwitchCheckBox("AI 自动摘要")
        self.chk_llm_summary.setToolTip("转录后调用 OpenAI 兼容接口生成摘要（需在偏好设置中配置）")
        self.chk_include_timestamps = SwitchCheckBox("文档含时间轴")
        self.chk_include_timestamps.setToolTip("在 Markdown 正文每段前加入 [00:00:00] 时间标记")
        self.chk_include_video_info = SwitchCheckBox("文档含视频信息")
        self.chk_include_video_info.setToolTip("在 Markdown 开头加入标题、作者、时长等信息表")

        switches = [
            self.chk_to_simplified,
            self.chk_write_srt,
            self.chk_keep_audio,
            self.chk_allow_playlist,
            self.chk_llm_summary,
            self.chk_include_timestamps,
            self.chk_include_video_info,
        ]
        for idx, sw in enumerate(switches):
            toggles_grid.addWidget(sw, idx // 4, idx % 4)
        for col in range(4):
            toggles_grid.setColumnStretch(col, 1)
        out_layout.addLayout(toggles_grid)
        root.addWidget(out_card)

        # === 4. Task queue ===
        queue_card = make_card_frame(self)
        queue_layout = QVBoxLayout(queue_card)
        queue_layout.setContentsMargins(18, 14, 18, 16)
        queue_layout.setSpacing(12)

        queue_top = QHBoxLayout()
        queue_top.setSpacing(8)
        lbl_q_title = QLabel("任务队列")
        lbl_q_title.setObjectName("SectionTitle")
        self.lbl_queue_stats = QLabel("共 0 项")
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

        # === 5. Bottom bar: console toggle + maintenance ===
        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(6)
        self.btn_toggle_log = QToolButton()
        self.btn_toggle_log.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_toggle_log.setArrowType(Qt.ArrowType.RightArrow)
        self.btn_toggle_log.setText(" 运行日志")
        self.btn_toggle_log.setCheckable(True)

        self.btn_copy_log = QPushButton("复制日志")
        self.btn_copy_log.setObjectName("GhostButton")
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_clear_log.setObjectName("GhostButton")
        self.btn_open_logs_dir = QPushButton("日志文件夹")
        self.btn_open_logs_dir.setObjectName("GhostButton")
        self.btn_update_ytdlp = QPushButton("更新 yt-dlp")
        self.btn_update_ytdlp.setObjectName("GhostButton")

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
        self.log_view.setFixedHeight(150)
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

        self.combo_model.currentIndexChanged.connect(self._refresh_language_options)
        self.combo_model.currentIndexChanged.connect(self._on_options_changed)
        self.combo_lang.currentIndexChanged.connect(self._on_lang_changed)
        self.chk_route.toggled.connect(self._on_route_toggled)
        self.chk_gpu.toggled.connect(self._on_gpu_toggled)
        self.gpu_indicator.install_clicked.connect(self._open_gpu_dialog)

        self.edit_output_dir.editingFinished.connect(self._on_output_dir_edited)
        self.btn_browse_out.clicked.connect(self._browse_output_dir)
        self.btn_open_out.clicked.connect(lambda: open_path_in_os(self.paths.output_dir, False))

        self.chk_keep_audio.toggled.connect(self._on_options_changed)
        self.chk_write_srt.toggled.connect(self._on_options_changed)
        self.chk_to_simplified.toggled.connect(self._on_options_changed)
        self.chk_allow_playlist.toggled.connect(self._on_options_changed)
        self.chk_include_timestamps.toggled.connect(self._on_options_changed)
        self.chk_include_video_info.toggled.connect(self._on_options_changed)
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

            self.chk_route.setChecked(self.cfg.route_enabled)

            idx_l = self.combo_lang.findData(self.cfg.language)
            if idx_l >= 0:
                self.combo_lang.setCurrentIndex(idx_l)
            self._refresh_language_options()

            self.chk_gpu.setChecked(self.cfg.use_gpu)
            self.edit_output_dir.setText(str(self.paths.output_dir))
            self.chk_keep_audio.setChecked(self.cfg.keep_audio)
            self.chk_write_srt.setChecked(self.cfg.write_srt)
            self.chk_to_simplified.setChecked(self.cfg.to_simplified)
            self.chk_allow_playlist.setChecked(self.cfg.allow_playlist)
            self.chk_include_timestamps.setChecked(self.cfg.include_timestamps)
            self.chk_include_video_info.setChecked(self.cfg.include_video_info)
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
        self.queue_table.refresh_theme()
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
            f"共 {st['total']} 项    进行中 {st['running']}    等待 {st['waiting']}    "
            f"已完成 {st['completed']}    失败或取消 {st['failed']}"
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
            self.queue_table.refresh_theme()
            from app.logger import get_logger
            get_logger().info(
                "已保存偏好设置 (代理=%s, 浏览器读取[B站/TikTok优先]=%s, YouTube托管自动续期=%s)",
                self.cfg.proxy or "自动检测系统代理/直连",
                self.cfg.cookies_from_browser or "未启用",
                "开启" if self.cfg.auto_refresh_cookies else "关闭",
            )

    def _restore_lang_text(self) -> None:
        """Snap the editable language box back to the selected entry (ignore half-typed text)."""
        edit = self.combo_lang.lineEdit()
        if edit is not None:
            edit.setText(self.combo_lang.itemText(self.combo_lang.currentIndex()))

    def _language_allowed(self, code: Optional[str]) -> bool:
        """With routing on every language is allowed (each routes to a model that supports it)."""
        if self.chk_route.isChecked() or code is None:
            return True
        model = str(self.combo_model.currentData() or self.cfg.model)
        return languages.is_language_supported(model, code)

    def _refresh_language_options(self) -> None:
        """Grey out languages the selected model cannot transcribe; fall back to auto-detect if needed."""
        model = str(self.combo_model.currentData() or self.cfg.model)
        routing = self.chk_route.isChecked()
        supported = None if routing else languages.supported_languages(model)
        item_model = self.combo_lang.model()
        for i in range(self.combo_lang.count()):
            code = self.combo_lang.itemData(i)
            allowed = code is None or supported is None or code in supported
            item = item_model.item(i) if hasattr(item_model, "item") else None
            if item is not None:
                item.setEnabled(allowed)
                item.setToolTip("" if allowed else f"{model} 不支持该语言（可开启「语言路由」自动换用其他模型）")
        current = self.combo_lang.currentData()
        if not self._language_allowed(current):
            blocker = self.combo_lang.blockSignals(True)
            try:
                self.combo_lang.setCurrentIndex(0)
            finally:
                self.combo_lang.blockSignals(blocker)
            self._restore_lang_text()
            self.cfg.language = None

    def _on_lang_changed(self) -> None:
        if self._suppress_option_sync:
            return
        if not self._language_allowed(self.combo_lang.currentData()):
            self.combo_lang.setCurrentIndex(0)  # re-enters with auto-detect
            return
        self._restore_lang_text()
        self._on_options_changed()

    def _on_route_toggled(self, checked: bool) -> None:
        if self._suppress_option_sync:
            return
        self.cfg.route_enabled = bool(checked)
        self._refresh_language_options()
        self._on_options_changed()

    def _on_options_changed(self) -> None:
        if self._suppress_option_sync:
            return
        self.cfg.model = str(self.combo_model.currentData() or "large-v3-turbo")
        self.cfg.language = self.combo_lang.currentData()
        self.cfg.route_enabled = self.chk_route.isChecked()
        self.cfg.keep_audio = self.chk_keep_audio.isChecked()
        self.cfg.write_srt = self.chk_write_srt.isChecked()
        self.cfg.to_simplified = self.chk_to_simplified.isChecked()
        self.cfg.allow_playlist = self.chk_allow_playlist.isChecked()
        self.cfg.include_timestamps = self.chk_include_timestamps.isChecked()
        self.cfg.include_video_info = self.chk_include_video_info.isChecked()
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
            "<p>把视频和音频转成带时间轴的文字。下载由 <b>yt-dlp</b> 完成，识别使用 "
            "<b>faster-whisper</b> 与 <b>SenseVoiceSmall</b>。</p>"
            "<ul>"
            "<li>粘贴单个或批量链接，展开播放列表，或直接拖入本地音视频文件</li>"
            "<li>语言路由：按语言自动选择识别模型，支持 NVIDIA CUDA 12 加速并在不可用时回退 CPU</li>"
            "<li>可接入 OpenAI 兼容接口（OpenAI、DeepSeek、Qwen、Ollama）自动生成摘要</li>"
            "</ul>"
            "<p><small>仅供个人学习与研究使用，请遵守各视频站点的服务条款与版权法规。</small></p>",
        )

    def dragEnterEvent(self, event) -> None:
        self.input_edit.dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:
        self.input_edit.set_drag_active(False)
        super().dragLeaveEvent(event)

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
