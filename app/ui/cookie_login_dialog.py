"""网站账号托管与自动续期中心对话框（YouTube 首选：独立持久化 Account Vault + CDP 明文提取 + 无头静默刷新）。"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.core import downloader
from app.core.browser_cookies import (
    ManagedBrowser,
    _kill_stale_profile_owner,
    clear_hosted_site_cookies,
    export_logged_in_sites_from_browser,
    find_browser,
    get_custom_sites_file,
    get_site_cookie_path,
    get_vault_profile_dir,
    list_hosted_sites_status,
    refresh_hosted_site_cookies,
)
from app.core.site_cookies import SITES, CookieReport, SiteProfile, load_all_sites

LOGIN_URLS = {s.key: s.effective_login_url for s in SITES}
SITE_LABELS = {s.key: s.display_label for s in SITES}


class _Call(QThread):
    """把阻塞调用放到后台线程，避免卡住界面。"""
    done = Signal(object, object)            # (result, error)

    def __init__(self, fn, parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn(), None)
        except Exception as exc:             # noqa: BLE001
            self.done.emit(None, exc)


class CookieLoginDialog(QDialog):
    def __init__(self, paths, parent=None, proxy: str = "", preferred_browser: str = "edge") -> None:
        super().__init__(parent)
        self.setWindowTitle("网站账号托管与自动续期中心")
        self.setMinimumWidth(600)
        self.paths = paths
        self.proxy = proxy
        self.preferred_browser = (preferred_browser or "edge").strip().lower() or "edge"
        self.exported_path: Optional[str] = None
        self.exported_site_key: str = ""
        self._browser: Optional[ManagedBrowser] = None
        self._worker: Optional[_Call] = None
        self._sites = load_all_sites(get_custom_sites_file(self.paths))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        lbl_intro = QLabel(
            "在此将 YouTube 等网站账号直接托管到程序专用的隔离浏览器库中（与日常浏览器完全独立、互不干扰）：\n"
            "• 首次使用点击「步骤 1」打开托管浏览器完成登录，再点击「步骤 2」提取并保存凭据；\n"
            "• YouTube 下载将专属使用此托管账号，当 Cookie 过期失效时自动在后台以无头模式静默刷新提取新 Cookie（B站 / TikTok 则优先使用日常浏览器读取，也可在此托管作为兜底）。"
        )
        lbl_intro.setWordWrap(True)
        lay.addWidget(lbl_intro)

        self.lbl_vault_overview = QLabel("")
        self.lbl_vault_overview.setWordWrap(True)
        self.lbl_vault_overview.setObjectName("MutedLabel")
        lay.addWidget(self.lbl_vault_overview)

        site_row = QHBoxLayout()
        site_row.addWidget(QLabel("目标网站："))
        self.combo_site = QComboBox()
        site_row.addWidget(self.combo_site, 1)
        lay.addLayout(site_row)

        self.btn_open = QPushButton("🌐 步骤 1：打开托管浏览器窗口登录账号")
        self.btn_export = QPushButton("✅ 步骤 2：我已在窗口中完成登录，立即提取并托管凭据")
        self.btn_export.setObjectName("PrimaryButton")
        self.btn_export.setEnabled(False)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.btn_refresh_silent = QPushButton("🔄 后台无头静默刷新当前站点 Cookie")
        self.btn_clear_site = QPushButton("🗑️ 清除当前站点凭据")
        self.btn_clear_site.setObjectName("GhostButton")
        action_row.addWidget(self.btn_refresh_silent)
        action_row.addWidget(self.btn_clear_site)
        action_row.addStretch(1)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)

        lay.addWidget(self.btn_open)
        lay.addWidget(self.btn_export)
        lay.addLayout(action_row)
        lay.addWidget(self.lbl_status)

        self.btn_open.clicked.connect(self._open_browser)
        self.btn_export.clicked.connect(self._export)
        self.btn_refresh_silent.clicked.connect(self._refresh_current_site_silently)
        self.btn_clear_site.clicked.connect(self._clear_current_site)

        self._refresh_site_combo_and_overview()

    def _site(self) -> SiteProfile:
        key = self.combo_site.currentData()
        for s in self._sites:
            if s.key == key:
                return s
        return self._sites[0]

    def _refresh_site_combo_and_overview(self, keep_key: str = "") -> None:
        current_key = keep_key or str(self.combo_site.currentData() or "")
        statuses = list_hosted_sites_status(self.paths)
        self._sites = tuple(s for s, _p, _r in statuses) or load_all_sites(get_custom_sites_file(self.paths))

        self.combo_site.blockSignals(True)
        try:
            self.combo_site.clear()
            ok_names = []
            for site, c_path, rep in statuses:
                if rep.ok:
                    tag = "✓ 已托管"
                    ok_names.append(site.display_label)
                elif c_path.is_file():
                    tag = "⚠ 凭据已过期/待刷新"
                else:
                    tag = "未托管"
                self.combo_site.addItem(f"{site.display_label}  [{tag}]", site.key)
            if current_key:
                idx = self.combo_site.findData(current_key)
                if idx >= 0:
                    self.combo_site.setCurrentIndex(idx)
        finally:
            self.combo_site.blockSignals(False)

        if ok_names:
            self.lbl_vault_overview.setText(f"当前已托管有效站点（{len(ok_names)} 个）：{'、'.join(ok_names)}")
        else:
            self.lbl_vault_overview.setText("当前尚未托管任何站点账号，请选择下方网站并点击「步骤 1」登录托管。")

    def _run(self, fn, on_done) -> None:
        self._worker = _Call(fn, self)
        self._worker.done.connect(on_done)
        self._worker.start()

    def _close_active_browser(self) -> None:
        """关闭当前打开的托管浏览器窗口，并清理旧版遗留目录（保留持久化 account_vault/browser_profile 登录态）。"""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        legacy_profile = self.paths.data_root / "browser_profile"
        if legacy_profile.exists():
            _kill_stale_profile_owner(legacy_profile)
            shutil.rmtree(legacy_profile, ignore_errors=True)

    def _open_browser(self) -> None:
        exe = find_browser("edge") or find_browser("chrome")
        if exe is None:
            QMessageBox.warning(self, "未找到浏览器", "未检测到 Microsoft Edge 或 Google Chrome，请先安装其中任意一款浏览器。")
            return
        self._close_active_browser()
        site = self._site()
        url = site.effective_login_url
        eff_proxy = downloader.resolve_effective_proxy(self.proxy, target_url=url)
        vault_profile = get_vault_profile_dir(self.paths)
        self._browser = ManagedBrowser(exe, vault_profile, headless=False, proxy=eff_proxy)
        self.btn_open.setEnabled(False)
        self.btn_refresh_silent.setEnabled(False)
        self.lbl_status.setText(f"正在启动托管浏览器并打开 {site.display_label}…")
        self._run(lambda: self._browser.start(url), self._on_opened)

    def _on_opened(self, _res, err) -> None:
        if err:
            self._close_active_browser()
            self.btn_open.setEnabled(True)
            self.btn_refresh_silent.setEnabled(True)
            self.lbl_status.setText(f"启动失败：{err}")
            return
        self.btn_export.setEnabled(True)
        self.lbl_status.setText(
            "请在弹出的托管浏览器窗口中登录目标网站（如 YouTube），"
            "确认已登录后点击上方「步骤 2：立即提取并托管凭据」。"
        )

    def _export(self) -> None:
        if self._browser is None:
            return
        site = self._site()
        self.btn_export.setEnabled(False)
        self.lbl_status.setText("正在从托管浏览器提取登录凭据并同步落盘…")
        self._run(
            lambda: export_logged_in_sites_from_browser(self._browser, paths=self.paths, primary_site_key=site.key),
            lambda reports, err: self._on_exported(site, reports, err),
        )

    def _on_exported(self, site: SiteProfile, reports: Optional[Dict[str, CookieReport]], err) -> None:
        if err or not reports:
            self.btn_export.setEnabled(True)
            self.lbl_status.setText(f"提取失败：{err or '未获取到任何 Cookie'}")
            return

        primary_rep = reports.get(site.key) or CookieReport()
        self._refresh_site_combo_and_overview(keep_key=site.key)

        if primary_rep.ok:
            out_path = get_site_cookie_path(site.key, self.paths)
            self.exported_path = str(out_path)
            self.exported_site_key = site.key
            self._close_active_browser()
            self.btn_open.setEnabled(True)
            self.btn_refresh_silent.setEnabled(True)

            all_ok_keys = [k for k, r in reports.items() if r.ok]
            extra_msg = f"\n同时已自动识别并托管站点：{', '.join(all_ok_keys)}" if len(all_ok_keys) > 1 else ""
            self.lbl_status.setText(f"✓ 托管成功：{primary_rep.summary()}{extra_msg}")
            QMessageBox.information(
                self,
                "账号托管成功",
                f"已成功提取并托管 {site.display_label} 的登录凭据！\n\n"
                f"{primary_rep.summary()}{extra_msg}\n\n"
                "后续下载将自动使用该凭据，并在过期时由程序后台无头静默刷新。",
            )
            self.accept()
        else:
            self.btn_export.setEnabled(True)
            self.lbl_status.setText(
                f"当前窗口尚未检测到 {site.display_label} 的有效登录凭据（{primary_rep.summary()}）。\n"
                "请先在弹出的托管浏览器窗口中完成登录，再点击「步骤 2」。"
            )

    def _refresh_current_site_silently(self) -> None:
        self._close_active_browser()
        site = self._site()
        eff_proxy = downloader.resolve_effective_proxy(self.proxy, target_url=site.effective_refresh_url)
        self.btn_refresh_silent.setEnabled(False)
        self.btn_open.setEnabled(False)
        self.lbl_status.setText(f"正在后台无头启动托管浏览器，静默提取 {site.display_label} 的最新 Cookie…")

        logs: list[str] = []

        def _do_refresh() -> Optional[Path]:
            return refresh_hosted_site_cookies(
                site,
                paths=self.paths,
                proxy=eff_proxy,
                on_log=logs.append,
            )

        self._run(_do_refresh, lambda out_p, err: self._on_silent_refreshed(site, out_p, logs, err))

    def _on_silent_refreshed(self, site: SiteProfile, out_path: Optional[Path], logs: list[str], err) -> None:
        self.btn_refresh_silent.setEnabled(True)
        self.btn_open.setEnabled(True)
        self._refresh_site_combo_and_overview(keep_key=site.key)
        if err:
            self.lbl_status.setText(f"静默刷新失败：{err}")
            return
        if out_path is not None and out_path.is_file():
            self.exported_path = str(out_path)
            self.exported_site_key = site.key
            self.lbl_status.setText("\n".join(logs) or f"✓ 已成功在后台静默刷新 {site.display_label} 凭据！")
            QMessageBox.information(self, "静默刷新成功", self.lbl_status.text())
        else:
            self.lbl_status.setText(
                "\n".join(logs)
                or f"未能静默刷新 {site.display_label}：请先点击「步骤 1」在托管浏览器中登录一次该网站。"
            )

    def _clear_current_site(self) -> None:
        site = self._site()
        removed = clear_hosted_site_cookies(site.key, self.paths)
        self._refresh_site_combo_and_overview(keep_key=site.key)
        if self.exported_site_key == site.key:
            self.exported_path = None
            self.exported_site_key = ""
        if removed:
            self.lbl_status.setText(f"已清除 {site.display_label} 的本地导出凭据文件。")
        else:
            self.lbl_status.setText(f"{site.display_label} 当前没有已保存的凭据文件。")

    def done(self, result: int) -> None:
        self._close_active_browser()
        super().done(result)
