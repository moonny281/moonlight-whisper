"""Settings dialog covering network/proxy, cookies, directories with file migration, LLM summary, and yt-dlp update."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.core import downloader
from app.core.errors import ErrorInfo
from app.paths import PATHS, AppPaths
from app.workers import YtDlpUpdateWorker


class SettingsDialog(QDialog):
    """Comprehensive settings dialog implementing §6.3 + playlist & OpenAI summary hooks."""

    def __init__(
        self,
        cfg: Config,
        paths: AppPaths = PATHS,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.paths = paths
        self._ytdlp_worker: Optional[YtDlpUpdateWorker] = None

        self.setWindowTitle("设置 - Moonlight Whisper")
        self.setMinimumSize(640, 520)
        self._build_ui()
        self._load_from_config()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_network_tab(), "网络与认证")
        self.tabs.addTab(self._build_transcribe_tab(), "转录与参数")
        self.tabs.addTab(self._build_llm_tab(), "LLM 自动摘要")
        self.tabs.addTab(self._build_storage_tab(), "存储目录")

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_save = QPushButton("保存")
        self.btn_cancel = QPushButton("取消")
        self.btn_save.clicked.connect(self._save_and_accept)
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_cancel)
        root.addLayout(btn_row)

    def _build_network_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        proxy_group = QGroupBox("代理与镜像设置")
        proxy_form = QFormLayout(proxy_group)
        self.edit_proxy = QLineEdit()
        self.edit_proxy.setPlaceholderText("例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080（留空则直连）")
        proxy_form.addRow("代理地址：", self.edit_proxy)

        self.chk_hf_mirror = QCheckBox("启用 HuggingFace 国内镜像 (https://hf-mirror.com)")
        proxy_form.addRow("模型下载源：", self.chk_hf_mirror)

        self.combo_pypi = QComboBox()
        self.combo_pypi.addItem("清华大学开源镜像 (tuna)", "tuna")
        self.combo_pypi.addItem("阿里云开源镜像 (aliyun)", "aliyun")
        self.combo_pypi.addItem("PyPI 官方源 (official)", "official")
        proxy_form.addRow("PyPI 镜像源：", self.combo_pypi)
        layout.addWidget(proxy_group)

        cookie_group = QGroupBox("站点登录与 Cookies（用于需要登录/年龄验证的视频）")
        cookie_form = QFormLayout(cookie_group)

        cookie_file_row = QHBoxLayout()
        self.edit_cookies_file = QLineEdit()
        self.edit_cookies_file.setPlaceholderText("Netscape 格式 cookies.txt 路径（留空则不使用）")
        btn_browse_cookie = QPushButton("浏览...")
        btn_browse_cookie.clicked.connect(self._browse_cookies_file)
        cookie_file_row.addWidget(self.edit_cookies_file, 1)
        cookie_file_row.addWidget(btn_browse_cookie)
        cookie_form.addRow("Cookies 文件：", cookie_file_row)

        self.combo_browser_cookies = QComboBox()
        self.combo_browser_cookies.addItem("不从浏览器读取", "")
        self.combo_browser_cookies.addItem("Chrome", "chrome")
        self.combo_browser_cookies.addItem("Edge", "edge")
        self.combo_browser_cookies.addItem("Firefox", "firefox")
        cookie_form.addRow("从浏览器读取：", self.combo_browser_cookies)
        layout.addWidget(cookie_group)

        ytdlp_group = QGroupBox("yt-dlp 下载组件维护")
        ytdlp_row = QHBoxLayout(ytdlp_group)
        self.lbl_ytdlp_ver = QLabel(f"当前版本：{downloader.get_yt_dlp_version(self.paths)}")
        self.btn_update_ytdlp = QPushButton("一键更新 yt-dlp")
        self.btn_update_ytdlp.clicked.connect(self._start_update_ytdlp)
        ytdlp_row.addWidget(self.lbl_ytdlp_ver)
        ytdlp_row.addStretch(1)
        ytdlp_row.addWidget(self.btn_update_ytdlp)
        layout.addWidget(ytdlp_group)

        layout.addStretch(1)
        return page

    def _build_transcribe_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        tr_group = QGroupBox("转录与输出规则")
        tr_form = QFormLayout(tr_group)

        self.chk_allow_playlist = QCheckBox("允许解析播放列表 / 多 P 视频（自动展开加入队列）")
        tr_form.addRow("播放列表：", self.chk_allow_playlist)

        self.chk_vad = QCheckBox("开启 VAD 静音过滤（减少静音段幻觉与重复）")
        tr_form.addRow("静音过滤：", self.chk_vad)

        self.spin_vad_ms = QSpinBox()
        self.spin_vad_ms.setRange(100, 5000)
        self.spin_vad_ms.setSingleStep(100)
        self.spin_vad_ms.setSuffix(" ms")
        tr_form.addRow("最小静音间隔：", self.spin_vad_ms)

        self.spin_cpu_threads = QSpinBox()
        self.spin_cpu_threads.setRange(0, 64)
        self.spin_cpu_threads.setSpecialValueText("0 (自动)")
        tr_form.addRow("CPU 线程数：", self.spin_cpu_threads)

        self.edit_filename_tpl = QLineEdit()
        self.edit_filename_tpl.setPlaceholderText("支持变量：{title}、{uploader}、{id}、{date}、{language}")
        tr_form.addRow("文件名模板：", self.edit_filename_tpl)

        self.combo_theme = QComboBox()
        self.combo_theme.addItem("跟随系统 (system)", "system")
        self.combo_theme.addItem("深色模式 (dark)", "dark")
        self.combo_theme.addItem("浅色模式 (light)", "light")
        tr_form.addRow("界面主题：", self.combo_theme)

        self.combo_log_level = QComboBox()
        for lv in ("INFO", "DEBUG", "WARNING", "ERROR"):
            self.combo_log_level.addItem(lv, lv)
        tr_form.addRow("日志级别：", self.combo_log_level)

        layout.addWidget(tr_group)
        layout.addStretch(1)
        return page

    def _build_llm_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        llm_group = QGroupBox("OpenAI 兼容接口（自动生成 Markdown 摘要）")
        llm_form = QFormLayout(llm_group)

        self.chk_llm_enabled = QCheckBox("转录完成后自动调用 LLM 生成「## 摘要」章节")
        llm_form.addRow("启用摘要：", self.chk_llm_enabled)

        self.edit_llm_base_url = QLineEdit()
        self.edit_llm_base_url.setPlaceholderText("https://api.openai.com/v1 或 DeepSeek / 本地 Ollama 地址")
        llm_form.addRow("API Base URL：", self.edit_llm_base_url)

        self.edit_llm_api_key = QLineEdit()
        self.edit_llm_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_llm_api_key.setPlaceholderText("sk-...")
        llm_form.addRow("API Key：", self.edit_llm_api_key)

        self.edit_llm_model = QLineEdit()
        self.edit_llm_model.setPlaceholderText("例如 gpt-4o-mini / deepseek-chat / qwen-plus")
        llm_form.addRow("模型名称：", self.edit_llm_model)

        self.edit_llm_prompt = QPlainTextEdit()
        self.edit_llm_prompt.setMaximumHeight(110)
        llm_form.addRow("系统提示词：", self.edit_llm_prompt)

        layout.addWidget(llm_group)
        hint = QLabel("说明：若未启用或接口调用失败，文档中的「## 摘要」将自动保留 `> （待填写）` 占位，不影响转录正文。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return page

    def _build_storage_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        dir_group = QGroupBox("自定义存储目录（留空则使用默认同目录/回退策略）")
        dir_form = QFormLayout(dir_group)

        self.edit_data_dir = self._make_dir_picker_row(dir_form, "数据根目录：")
        self.edit_gpu_dir = self._make_dir_picker_row(dir_form, "GPU 运行库目录：")
        self.edit_models_dir = self._make_dir_picker_row(dir_form, "Whisper 模型目录：")
        self.edit_temp_dir = self._make_dir_picker_row(dir_form, "临时缓存目录：")

        layout.addWidget(dir_group)

        resolved_group = QGroupBox("当前实际生效路径")
        res_form = QFormLayout(resolved_group)
        res_form.addRow("数据目录：", QLabel(str(self.paths.data_root)))
        res_form.addRow("GPU 运行库：", QLabel(str(self.paths.gpu_runtime_dir)))
        res_form.addRow("模型缓存：", QLabel(str(self.paths.models_dir)))
        res_form.addRow("输出目录：", QLabel(str(self.paths.output_dir)))
        layout.addWidget(resolved_group)

        layout.addStretch(1)
        return page

    def _make_dir_picker_row(self, form: QFormLayout, label: str) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText("留空使用默认策略")
        btn = QPushButton("选择...")
        btn.clicked.connect(lambda: self._pick_directory_into(edit))
        row.addWidget(edit, 1)
        row.addWidget(btn)
        form.addRow(label, row)
        return edit

    def _pick_directory_into(self, edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择目录", edit.text().strip() or str(self.paths.data_root))
        if folder:
            edit.setText(folder)

    def _browse_cookies_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Netscape 格式 cookies.txt 文件",
            "",
            "Text Files (*.txt);;All Files (*)",
        )
        if file_path:
            self.edit_cookies_file.setText(file_path)

    def _load_from_config(self) -> None:
        self.edit_proxy.setText(self.cfg.proxy)
        self.chk_hf_mirror.setChecked(self.cfg.hf_mirror)

        idx_pypi = self.combo_pypi.findData(self.cfg.pypi_mirror)
        if idx_pypi >= 0:
            self.combo_pypi.setCurrentIndex(idx_pypi)

        self.edit_cookies_file.setText(self.cfg.cookies_file)
        idx_browser = self.combo_browser_cookies.findData(self.cfg.cookies_from_browser)
        if idx_browser >= 0:
            self.combo_browser_cookies.setCurrentIndex(idx_browser)

        self.chk_allow_playlist.setChecked(self.cfg.allow_playlist)
        self.chk_vad.setChecked(self.cfg.vad)
        self.spin_vad_ms.setValue(self.cfg.min_silence_duration_ms)
        self.spin_cpu_threads.setValue(self.cfg.cpu_threads)
        self.edit_filename_tpl.setText(self.cfg.filename_template)

        idx_theme = self.combo_theme.findData(self.cfg.ui.theme)
        if idx_theme >= 0:
            self.combo_theme.setCurrentIndex(idx_theme)

        idx_log = self.combo_log_level.findData(self.cfg.log_level)
        if idx_log >= 0:
            self.combo_log_level.setCurrentIndex(idx_log)

        self.chk_llm_enabled.setChecked(self.cfg.llm.enabled)
        self.edit_llm_base_url.setText(self.cfg.llm.base_url)
        self.edit_llm_api_key.setText(self.cfg.llm.api_key)
        self.edit_llm_model.setText(self.cfg.llm.model)
        self.edit_llm_prompt.setPlainText(self.cfg.llm.system_prompt)

        self.edit_data_dir.setText(self.cfg.data_dir)
        self.edit_gpu_dir.setText(self.cfg.gpu_runtime_dir)
        self.edit_models_dir.setText(self.cfg.models_dir)
        self.edit_temp_dir.setText(self.cfg.temp_dir)

    def _start_update_ytdlp(self) -> None:
        self.btn_update_ytdlp.setEnabled(False)
        self.lbl_ytdlp_ver.setText("正在检查并下载更新...")
        worker = YtDlpUpdateWorker(
            pypi_mirror=str(self.combo_pypi.currentData() or "tuna"),
            proxy=self.edit_proxy.text().strip(),
            paths=self.paths,
            parent=self,
        )
        worker.update_succeeded.connect(self._on_ytdlp_updated)
        worker.update_failed.connect(self._on_ytdlp_failed)
        self._ytdlp_worker = worker
        worker.start()

    def _on_ytdlp_updated(self, new_ver: str) -> None:
        self._ytdlp_worker = None
        self.btn_update_ytdlp.setEnabled(True)
        self.lbl_ytdlp_ver.setText(f"当前版本：{new_ver} (已更新)")
        QMessageBox.information(self, "更新成功", f"yt-dlp 已成功更新至版本 {new_ver}！")

    def _on_ytdlp_failed(self, err: ErrorInfo) -> None:
        self._ytdlp_worker = None
        self.btn_update_ytdlp.setEnabled(True)
        self.lbl_ytdlp_ver.setText(f"当前版本：{downloader.get_yt_dlp_version(self.paths)}")
        QMessageBox.warning(self, "更新失败", f"{err.user_message}\n\n{err.detail}")

    def _save_and_accept(self) -> None:
        old_gpu_dir = self.paths.gpu_runtime_dir
        old_models_dir = self.paths.models_dir

        self.cfg.proxy = self.edit_proxy.text().strip()
        self.cfg.hf_mirror = self.chk_hf_mirror.isChecked()
        self.cfg.pypi_mirror = str(self.combo_pypi.currentData() or "tuna")
        self.cfg.cookies_file = self.edit_cookies_file.text().strip()
        self.cfg.cookies_from_browser = str(self.combo_browser_cookies.currentData() or "")

        self.cfg.allow_playlist = self.chk_allow_playlist.isChecked()
        self.cfg.vad = self.chk_vad.isChecked()
        self.cfg.min_silence_duration_ms = self.spin_vad_ms.value()
        self.cfg.cpu_threads = self.spin_cpu_threads.value()
        self.cfg.filename_template = self.edit_filename_tpl.text().strip() or "{title}"
        self.cfg.ui.theme = str(self.combo_theme.currentData() or "system")
        self.cfg.log_level = str(self.combo_log_level.currentData() or "INFO")

        self.cfg.llm.enabled = self.chk_llm_enabled.isChecked()
        self.cfg.llm.base_url = self.edit_llm_base_url.text().strip() or "https://api.openai.com/v1"
        self.cfg.llm.api_key = self.edit_llm_api_key.text().strip()
        self.cfg.llm.model = self.edit_llm_model.text().strip() or "gpt-4o-mini"
        self.cfg.llm.system_prompt = self.edit_llm_prompt.toPlainText().strip() or self.cfg.llm.system_prompt

        self.cfg.data_dir = self.edit_data_dir.text().strip()
        self.cfg.gpu_runtime_dir = self.edit_gpu_dir.text().strip()
        self.cfg.models_dir = self.edit_models_dir.text().strip()
        self.cfg.temp_dir = self.edit_temp_dir.text().strip()

        self.cfg.save(paths=self.paths)
        self.paths.ensure_runtime_dirs()

        new_gpu_dir = self.paths.gpu_runtime_dir
        new_models_dir = self.paths.models_dir

        # Prompt to migrate existing GPU runtime or models if user changed their directories (§4)
        if new_gpu_dir != old_gpu_dir and old_gpu_dir.exists() and any(old_gpu_dir.iterdir()):
            ans = QMessageBox.question(
                self,
                "迁移 GPU 运行库文件",
                f"检测到 GPU 运行库目录已更改。\n是否将原目录 ({old_gpu_dir}) 下的已有文件迁移至新目录 ({new_gpu_dir})？",
            )
            if ans == QMessageBox.StandardButton.Yes:
                AppPaths.migrate_directory_contents(old_gpu_dir, new_gpu_dir)

        if new_models_dir != old_models_dir and old_models_dir.exists() and any(old_models_dir.iterdir()):
            ans = QMessageBox.question(
                self,
                "迁移模型文件",
                f"检测到模型目录已更改。\n是否将原目录 ({old_models_dir}) 下的已有模型迁移至新目录 ({new_models_dir})？",
            )
            if ans == QMessageBox.StandardButton.Yes:
                AppPaths.migrate_directory_contents(old_models_dir, new_models_dir)

        self.accept()
