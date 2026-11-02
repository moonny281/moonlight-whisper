"""Windows 11 Mica-styled Settings Dialog (with built-in rookie-cookies browser extraction, Profile discovery, managed browser CDP login, and Cookie health check)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
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

from app.config import Config, LlmConfig
from app.core import downloader, rookie_bridge, summarizer
from app.core.errors import ErrorInfo
from app.core.site_cookies import inspect_netscape, netscape_to_header, probe_bilibili_login, resolve_site
from app.paths import PATHS, AppPaths
from app.ui.cookie_login_dialog import CookieLoginDialog
from app.ui.theme import apply_win11_mica_window_attributes, resolve_effective_theme
from app.ui.widgets import make_mica_section
from app.workers import YtDlpUpdateWorker


class SettingsDialog(QDialog):
    """Comprehensive Mica-styled settings dialog with built-in rookie-cookies support, Profile discovery, and zero row overlap."""

    def __init__(
        self,
        cfg: Config,
        paths: AppPaths = PATHS,
        initial_tab: int = 0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.paths = paths
        self._ytdlp_worker: Optional[YtDlpUpdateWorker] = None

        self.setWindowTitle("偏好设置 - Moonlight Whisper")
        self.setMinimumSize(740, 650)
        self._build_ui()
        self._load_from_config()
        if 0 <= initial_tab < self.tabs.count():
            self.tabs.setCurrentIndex(initial_tab)

        eff = resolve_effective_theme(self.cfg.ui.theme)
        apply_win11_mica_window_attributes(self, is_dark=(eff == "dark"))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_network_tab(), "网络与认证")
        self.tabs.addTab(self._build_transcribe_tab(), "转录与外观")
        self.tabs.addTab(self._build_llm_tab(), "AI 自动摘要")
        self.tabs.addTab(self._build_storage_tab(), "存储目录")

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_save = QPushButton("保存配置")
        self.btn_save.setObjectName("PrimaryButton")
        self.btn_cancel = QPushButton("取消")
        self.btn_save.clicked.connect(self._save_and_accept)
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_cancel)
        root.addLayout(btn_row)

    @staticmethod
    def _configure_form(form: QFormLayout) -> None:
        form.setContentsMargins(0, 4, 0, 0)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

    def _build_network_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        proxy_card, proxy_box = make_mica_section(
            "代理与国内镜像源",
            "支持 HTTP / HTTPS / SOCKS5 代理；留空时自动检测系统代理（自动探测到的系统代理不接管 B 站等国内站点）",
        )
        proxy_form = QFormLayout()
        self._configure_form(proxy_form)

        self.edit_proxy = QLineEdit()
        self.edit_proxy.setPlaceholderText("例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:10808（留空自动检测系统代理）")
        proxy_form.addRow("代理地址：", self.edit_proxy)

        self.chk_hf_mirror = QCheckBox("启用 HuggingFace 国内镜像 (https://hf-mirror.com)")
        proxy_form.addRow("模型下载源：", self.chk_hf_mirror)

        self.combo_pypi = QComboBox()
        self.combo_pypi.addItem("清华大学开源镜像 (tuna)", "tuna")
        self.combo_pypi.addItem("阿里云开源镜像 (aliyun)", "aliyun")
        self.combo_pypi.addItem("PyPI 官方源 (official)", "official")
        proxy_form.addRow("PyPI 镜像源：", self.combo_pypi)
        proxy_box.addLayout(proxy_form)
        layout.addWidget(proxy_card)

        rookie_ver = rookie_bridge.get_version()
        cookie_card, cookie_box = make_mica_section(
            f"站点登录与 Cookies（内置 rookie-cookies: {rookie_ver}）",
            "• 方式一（推荐）：在下方选择「从浏览器读取」，内置 rookie-cookies 原生支持 Edge / Chrome / Brave (含 Windows App-Bound v20 解密) 与 Firefox；\n"
            "• 方式二：点击「登录并获取 Cookies…」打开专用隔离浏览器登录导出，或直接导入 cookies.txt。",
        )
        cookie_form = QFormLayout()
        self._configure_form(cookie_form)

        self.combo_browser_cookies = QComboBox()
        self.combo_browser_cookies.addItem("不从浏览器读取（使用下方 Cookies 文件或匿名请求）", "")
        for b_info in rookie_bridge.list_supported_browsers():
            self.combo_browser_cookies.addItem(b_info["display_name"], b_info["id"])
        self.combo_browser_cookies.currentIndexChanged.connect(self._on_browser_changed)
        cookie_form.addRow("从浏览器读取：", self.combo_browser_cookies)

        profile_row = QHBoxLayout()
        profile_row.setSpacing(6)
        self.combo_browser_profile = QComboBox()
        self.combo_browser_profile.setEditable(True)
        if self.combo_browser_profile.lineEdit() is not None:
            self.combo_browser_profile.lineEdit().setPlaceholderText("留空自动选择默认配置，或点击右侧刷新本机 Profile 列表")
        self.btn_refresh_profiles = QPushButton("刷新配置")
        self.btn_refresh_profiles.clicked.connect(self._refresh_browser_profiles)
        profile_row.addWidget(self.combo_browser_profile, 1)
        profile_row.addWidget(self.btn_refresh_profiles)
        cookie_form.addRow("浏览器配置：", profile_row)

        self.combo_app_bound = QComboBox()
        for policy_key, policy_label in rookie_bridge.APP_BOUND_POLICIES:
            self.combo_app_bound.addItem(policy_label, policy_key)
        cookie_form.addRow("v20 解密策略：", self.combo_app_bound)

        cookie_file_row = QHBoxLayout()
        cookie_file_row.setSpacing(6)
        self.edit_cookies_file = QLineEdit()
        self.edit_cookies_file.setPlaceholderText("可选：指定本地 cookies.txt / JSON（若填写则优先于上方浏览器读取）")
        btn_paste_cookie = QPushButton("📋 从剪贴板导入")
        btn_paste_cookie.setToolTip("将剪贴板中的 Netscape / JSON Cookie 内容转换并保存为标准 cookies.txt")
        btn_paste_cookie.clicked.connect(self._import_cookies_from_clipboard)
        btn_browse_cookie = QPushButton("浏览文件...")
        btn_browse_cookie.clicked.connect(self._browse_cookies_file)
        btn_clear_cookie = QPushButton("清除")
        btn_clear_cookie.setObjectName("GhostButton")
        btn_clear_cookie.clicked.connect(self._clear_cookies_file)

        cookie_file_row.addWidget(self.edit_cookies_file, 1)
        cookie_file_row.addWidget(btn_paste_cookie)
        cookie_file_row.addWidget(btn_browse_cookie)
        cookie_file_row.addWidget(btn_clear_cookie)
        cookie_form.addRow("Cookies 文件：", cookie_file_row)

        cookie_action_row = QHBoxLayout()
        cookie_action_row.setSpacing(8)
        self.btn_login_cookie = QPushButton("登录并获取 Cookies…")
        self.btn_login_cookie.setObjectName("PrimaryButton")
        self.btn_login_cookie.clicked.connect(self._open_cookie_login)
        self.btn_check_cookie = QPushButton("检测 Cookies 有效性")
        self.btn_check_cookie.clicked.connect(self._check_cookies)
        cookie_action_row.addWidget(self.btn_login_cookie)
        cookie_action_row.addWidget(self.btn_check_cookie)
        cookie_action_row.addStretch(1)
        cookie_form.addRow("快捷工具：", cookie_action_row)

        cookie_box.addLayout(cookie_form)
        layout.addWidget(cookie_card)

        ytdlp_card, ytdlp_box = make_mica_section("yt-dlp 下载引擎与运行环境维护")
        ytdlp_row = QHBoxLayout()
        self.lbl_ytdlp_ver = QLabel(f"当前版本：{downloader.get_yt_dlp_version(self.paths)}")
        self.btn_diagnose_ytdlp = QPushButton("诊断运行环境")
        self.btn_diagnose_ytdlp.clicked.connect(self._show_youtube_diagnosis)
        self.btn_update_ytdlp = QPushButton("一键热更新 yt-dlp")
        self.btn_update_ytdlp.clicked.connect(self._start_update_ytdlp)
        ytdlp_row.addWidget(self.lbl_ytdlp_ver)
        ytdlp_row.addStretch(1)
        ytdlp_row.addWidget(self.btn_diagnose_ytdlp)
        ytdlp_row.addWidget(self.btn_update_ytdlp)
        ytdlp_box.addLayout(ytdlp_row)
        layout.addWidget(ytdlp_card)

        layout.addStretch(1)
        return page

    def _build_transcribe_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        tr_card, tr_box = make_mica_section("转录参数与外观主题")
        tr_form = QFormLayout()
        self._configure_form(tr_form)

        self.chk_allow_playlist = QCheckBox("允许解析播放列表 / 多 P 视频（自动展开加入任务队列）")
        tr_form.addRow("播放列表：", self.chk_allow_playlist)

        self.chk_vad = QCheckBox("开启 VAD 静音过滤（有效减少静音段幻觉与重复循环）")
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
        self.combo_theme.addItem("跟随系统 (System Mica)", "system")
        self.combo_theme.addItem("深色云母 (Dark Mica)", "dark")
        self.combo_theme.addItem("浅色云母 (Light Mica)", "light")
        tr_form.addRow("界面主题：", self.combo_theme)

        self.combo_log_level = QComboBox()
        for lv in ("INFO", "DEBUG", "WARNING", "ERROR"):
            self.combo_log_level.addItem(lv, lv)
        tr_form.addRow("日志级别：", self.combo_log_level)

        tr_box.addLayout(tr_form)
        layout.addWidget(tr_card)
        layout.addStretch(1)
        return page

    def _build_llm_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        llm_card, llm_box = make_mica_section(
            "OpenAI 兼容接口（自动生成 Markdown 摘要）",
            "支持 OpenAI、DeepSeek、通义千问、Moonshot 或本地 Ollama 端点；若未启用则保留 `> （待填写）` 占位。",
        )
        llm_form = QFormLayout()
        self._configure_form(llm_form)

        self.chk_llm_enabled = QCheckBox("转录完成后自动调用 LLM 生成「## 摘要」章节")
        llm_form.addRow("启用摘要：", self.chk_llm_enabled)

        self.edit_llm_base_url = QLineEdit()
        self.edit_llm_base_url.setPlaceholderText("https://api.openai.com/v1 或 https://api.deepseek.com/v1")
        llm_form.addRow("API Base URL：", self.edit_llm_base_url)

        self.edit_llm_api_key = QLineEdit()
        self.edit_llm_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_llm_api_key.setPlaceholderText("sk-...")
        llm_form.addRow("API Key：", self.edit_llm_api_key)

        self.edit_llm_model = QLineEdit()
        self.edit_llm_model.setPlaceholderText("例如 gpt-4o-mini / deepseek-chat / qwen-plus")
        llm_form.addRow("模型名称：", self.edit_llm_model)

        self.edit_llm_prompt = QPlainTextEdit()
        self.edit_llm_prompt.setMinimumHeight(90)
        self.edit_llm_prompt.setMaximumHeight(120)
        llm_form.addRow("系统提示词：", self.edit_llm_prompt)

        test_row = QHBoxLayout()
        self.btn_test_llm = QPushButton("测试 API 连接")
        self.btn_test_llm.clicked.connect(self._test_llm_api)
        self.lbl_llm_test_result = QLabel("点击左侧按钮可验证接口与 Key 是否可用")
        self.lbl_llm_test_result.setObjectName("MutedLabel")
        test_row.addWidget(self.btn_test_llm)
        test_row.addWidget(self.lbl_llm_test_result, 1)
        llm_form.addRow("连通性测试：", test_row)

        llm_box.addLayout(llm_form)
        layout.addWidget(llm_card)
        layout.addStretch(1)
        return page

    def _build_storage_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        dir_card, dir_box = make_mica_section(
            "自定义存储目录",
            "留空则默认跟随安装目录（若安装目录无写权限则自动回退至 %LOCALAPPDATA%\\MoonlightWhisper）。",
        )
        dir_form = QFormLayout()
        self._configure_form(dir_form)

        self.edit_data_dir = self._make_dir_picker_row(dir_form, "数据根目录：")
        self.edit_gpu_dir = self._make_dir_picker_row(dir_form, "GPU 运行库目录：")
        self.edit_models_dir = self._make_dir_picker_row(dir_form, "Whisper 模型目录：")
        self.edit_temp_dir = self._make_dir_picker_row(dir_form, "临时缓存目录：")
        dir_box.addLayout(dir_form)
        layout.addWidget(dir_card)

        res_card, res_box = make_mica_section("当前实际生效路径")
        res_form = QFormLayout()
        self._configure_form(res_form)
        res_form.addRow("数据根目录：", QLabel(str(self.paths.data_root)))
        res_form.addRow("GPU 运行库：", QLabel(str(self.paths.gpu_runtime_dir)))
        res_form.addRow("模型缓存：", QLabel(str(self.paths.models_dir)))
        res_form.addRow("输出目录：", QLabel(str(self.paths.output_dir)))
        res_box.addLayout(res_form)
        layout.addWidget(res_card)

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

    def _current_profile_text(self) -> str:
        data = self.combo_browser_profile.currentData()
        if isinstance(data, str) and data.strip():
            return data.strip()
        return self.combo_browser_profile.currentText().strip()

    def _on_browser_changed(self) -> None:
        browser_id = str(self.combo_browser_cookies.currentData() or "").strip()
        if browser_id and rookie_bridge.is_available():
            self._populate_profiles(browser_id, keep_text=self._current_profile_text())

    def _populate_profiles(self, browser_id: str, keep_text: str = "") -> None:
        self.combo_browser_profile.blockSignals(True)
        try:
            self.combo_browser_profile.clear()
            self.combo_browser_profile.addItem("", "")
            for p in rookie_bridge.list_browser_profiles(browser_id):
                label = f"{p.profile_id} ({p.display_name})" if p.display_name != p.profile_id else p.profile_id
                if p.is_default:
                    label += " [默认]"
                self.combo_browser_profile.addItem(label, p.profile_id)
            if keep_text:
                idx = self.combo_browser_profile.findData(keep_text)
                if idx >= 0:
                    self.combo_browser_profile.setCurrentIndex(idx)
                else:
                    self.combo_browser_profile.setEditText(keep_text)
        finally:
            self.combo_browser_profile.blockSignals(False)

    def _refresh_browser_profiles(self) -> None:
        browser_id = str(self.combo_browser_cookies.currentData() or "").strip()
        if not browser_id:
            QMessageBox.information(self, "提示", "请先在「从浏览器读取」下拉框中选择一个浏览器。")
            return
        cur = self._current_profile_text()
        self._populate_profiles(browser_id, keep_text=cur)
        count = max(0, self.combo_browser_profile.count() - 1)
        if count > 0:
            QMessageBox.information(self, "刷新完成", f"已通过 rookie-cookies 发现 {browser_id} 的 {count} 个本地配置目录。")
        else:
            QMessageBox.information(
                self,
                "未发现多配置",
                f"未检测到 {browser_id} 的额外配置目录（留空即可自动使用默认配置）。",
            )

    def _clear_cookies_file(self) -> None:
        self.edit_cookies_file.clear()
        self.cfg.managed_cookie_site = ""

    def _open_cookie_login(self) -> None:
        dlg = CookieLoginDialog(
            self.paths,
            self,
            proxy=self.edit_proxy.text().strip(),
            preferred_browser=str(self.combo_browser_cookies.currentData() or "edge"),
        )
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.exported_path:
            self.edit_cookies_file.setText(dlg.exported_path)
            self.cfg.managed_cookie_site = dlg.exported_site_key

    def _check_cookies(self) -> None:
        raw_path = self.edit_cookies_file.text().strip()
        browser_id = str(self.combo_browser_cookies.currentData() or "").strip()
        if not raw_path and not browser_id:
            QMessageBox.warning(
                self,
                "检测 Cookies",
                "请先选择「从浏览器读取」的浏览器，或指定「Cookies 文件」。",
            )
            return
        url, ok = QInputDialog.getText(self, "检测 Cookies", "输入一个需要登录才能看的视频链接：")
        if not ok or not url.strip():
            return
        target_url = url.strip()

        if raw_path:
            logs: list[str] = []
            prepared = downloader.prepare_cookie_file(
                Path(raw_path).expanduser(),
                self.paths.temp_dir,
                on_log=logs.append,
                target_url=target_url,
            )
            if prepared is None:
                QMessageBox.warning(self, "检测结果", "\n".join(logs) or "Cookies 文件无法解析")
                return
        else:
            logs = []
            try:
                prepared = rookie_bridge.extract_to_netscape_file(
                    browser=browser_id,
                    profile=self._current_profile_text(),
                    target_url=target_url,
                    temp_dir=self.paths.temp_dir,
                    app_bound=str(self.combo_app_bound.currentData() or "disabled"),
                    on_log=logs.append,
                )
            except Exception as exc:
                err = ErrorInfo.from_exception(exc)
                QMessageBox.warning(self, "浏览器提取失败", err.user_message)
                return
            if prepared is None:
                QMessageBox.warning(self, "检测结果", "\n".join(logs) or f"未能从 {browser_id} 提取到匹配的 Cookies")
                return

        report = inspect_netscape(prepared, target_url)
        msg = report.summary()
        site = resolve_site(target_url)
        if site and site.key == "bilibili" and report.ok:
            hdr = netscape_to_header(prepared, target_url)
            bili_ok = probe_bilibili_login(hdr, proxy=downloader.resolve_effective_proxy(self.edit_proxy.text(), target_url))
            if bili_ok is True:
                msg += "\n\n[B 站联网探测] 登录态有效（isLogin=True）！"
            elif bili_ok is False:
                msg += "\n\n[B 站联网探测] 服务端返回未登录（isLogin=False），SESSDATA 可能已失效，请重新登录。"
        QMessageBox.information(self, "检测结果", msg)

    def _show_youtube_diagnosis(self) -> None:
        diag = downloader.diagnose_youtube_runtime(self.paths)
        lines = [f"• {k}：{v}" for k, v in diag.items()]
        QMessageBox.information(self, "运行环境诊断", "\n".join(lines))

    def _import_cookies_from_clipboard(self) -> None:
        cb = QGuiApplication.clipboard()
        clip_text = cb.text().strip() if cb is not None else ""
        if not clip_text:
            QMessageBox.warning(
                self,
                "剪贴板为空",
                "当前剪贴板中没有文本内容。\n"
                "请先在浏览器中使用 Get cookies.txt LOCALLY 或 Cookie-Editor 插件复制 Cookies。",
            )
            return
        target_url = ""
        if not clip_text.startswith(("[", "{")) and "\t" not in clip_text:
            url_in, ok = QInputDialog.getText(
                self,
                "绑定目标站点域名",
                "检测到剪贴板为原始 Cookie 请求头，请输入对应的视频链接以绑定站点域名：",
            )
            if not ok or not url_in.strip():
                return
            target_url = url_in.strip()
        try:
            saved_path, count, sites = downloader.import_cookies_from_text(
                clip_text,
                paths=self.paths,
                target_url=target_url,
            )
            self.edit_cookies_file.setText(str(saved_path))
            self.combo_browser_cookies.setCurrentIndex(0)
            sites_desc = f"\n识别到的目标站点：{', '.join(sites)}" if sites else ""
            QMessageBox.information(
                self,
                "Cookies 导入成功",
                f"已成功从剪贴板解析并转换 {count} 条有效 Cookie 记录！{sites_desc}\n\n"
                f"已自动保存至：\n{saved_path}",
            )
        except Exception as exc:
            err = ErrorInfo.from_exception(exc)
            QMessageBox.warning(self, "Cookies 导入失败", err.user_message)

    def _browse_cookies_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Cookies 文件 (支持 Netscape .txt 或 JSON)",
            "",
            "Cookies Files (*.txt *.json);;All Files (*)",
        )
        if file_path:
            self.edit_cookies_file.setText(file_path)
            self.combo_browser_cookies.setCurrentIndex(0)

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
        elif self.cfg.cookies_from_browser:
            self.combo_browser_cookies.addItem(self.cfg.cookies_from_browser, self.cfg.cookies_from_browser)
            self.combo_browser_cookies.setCurrentIndex(self.combo_browser_cookies.count() - 1)

        self._populate_profiles(self.cfg.cookies_from_browser, keep_text=self.cfg.cookies_browser_profile)

        idx_ab = self.combo_app_bound.findData(self.cfg.cookies_app_bound)
        if idx_ab >= 0:
            self.combo_app_bound.setCurrentIndex(idx_ab)

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
        QMessageBox.information(self, "更新成功", f"yt-dlp 与 YouTube EJS 脚本已成功更新至版本 {new_ver}！")

    def _on_ytdlp_failed(self, err: ErrorInfo) -> None:
        self._ytdlp_worker = None
        self.btn_update_ytdlp.setEnabled(True)
        self.lbl_ytdlp_ver.setText(f"当前版本：{downloader.get_yt_dlp_version(self.paths)}")
        QMessageBox.warning(self, "更新失败", f"{err.user_message}\n\n{err.detail}")

    def _test_llm_api(self) -> None:
        temp_llm = LlmConfig(
            enabled=True,
            base_url=self.edit_llm_base_url.text().strip() or "https://api.openai.com/v1",
            api_key=self.edit_llm_api_key.text().strip(),
            model=self.edit_llm_model.text().strip() or "gpt-4o-mini",
            system_prompt=self.edit_llm_prompt.toPlainText().strip(),
            timeout_sec=15,
        )
        self.btn_test_llm.setEnabled(False)
        self.lbl_llm_test_result.setText("正在请求接口...")
        QApplication.processEvents()
        ok, msg = summarizer.test_llm_connection(temp_llm, proxy=self.edit_proxy.text().strip())
        self.btn_test_llm.setEnabled(True)
        self.lbl_llm_test_result.setText(msg)
        if ok:
            QMessageBox.information(self, "连接成功", msg)
        else:
            QMessageBox.warning(self, "连接失败", msg)

    def _save_and_accept(self) -> None:
        old_gpu_dir = self.paths.gpu_runtime_dir
        old_models_dir = self.paths.models_dir

        self.cfg.proxy = self.edit_proxy.text().strip()
        self.cfg.hf_mirror = self.chk_hf_mirror.isChecked()
        self.cfg.pypi_mirror = str(self.combo_pypi.currentData() or "tuna")
        self.cfg.cookies_file = self.edit_cookies_file.text().strip()
        self.cfg.cookies_from_browser = str(self.combo_browser_cookies.currentData() or "")
        self.cfg.cookies_browser_profile = self._current_profile_text()
        self.cfg.cookies_app_bound = str(self.combo_app_bound.currentData() or "disabled")

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
