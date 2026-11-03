"""网站账号托管与登录对话框（v1.4.0：专用登录小窗直接获取凭证 + 现有浏览器/剪贴板备用方式）。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.core import downloader, rookie_bridge
from app.core.browser_cookies import (
    clear_hosted_site_cookies,
    get_custom_sites_file,
    get_site_cookie_path,
    list_hosted_sites_status,
    open_url_in_existing_browser,
)
from app.core.site_cookies import SITES, SiteProfile, inspect_netscape, load_all_sites

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
        self.setWindowTitle("网站账号登录与凭据托管")
        self.setMinimumWidth(600)
        self.paths = paths
        self.proxy = proxy
        self.preferred_browser = (preferred_browser or "edge").strip().lower() or "edge"
        self.exported_path: Optional[str] = None
        self.exported_site_key: str = ""
        self._worker: Optional[_Call] = None
        self._login_win = None
        self._sites = load_all_sites(get_custom_sites_file(self.paths))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        lbl_intro = QLabel(
            "点击下方按钮会弹出一个专门用于登录的独立小窗（不是浏览器标签页，也不是新的浏览器配置）：\n"
            "• 步骤 1：在弹出的小窗中登录目标网站，不影响你正在使用的浏览器页面，也不会触发 Edge/Chrome 账号同步；\n"
            "• 步骤 2：登录完成后点击小窗底部的「我已登录：提取并托管凭据」，凭据将直接从小窗取得并保存在本机，下载时自动使用并回写刷新令牌。"
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

        self.btn_open_win = QPushButton("🪟 步骤 1：打开专用登录小窗并登录")
        self.btn_open_win.setObjectName("PrimaryButton")
        self.btn_open_win.setToolTip("在应用内弹出独立小窗登录；登录完成后在小窗里点击「我已登录」即可托管凭据")

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.btn_paste_host = QPushButton("📋 从剪贴板导入并托管当前站点")
        self.btn_paste_host.setToolTip("在任意浏览器复制 Cookie（Netscape / JSON / F12 原始 Cookie 头）后点击此按钮直接托管")
        self.btn_clear_site = QPushButton("🗑️ 清除当前站点凭据")
        self.btn_clear_site.setObjectName("GhostButton")
        action_row.addWidget(self.btn_paste_host)
        action_row.addWidget(self.btn_clear_site)
        action_row.addStretch(1)

        # 备用方式：沿用现有浏览器（不新建配置），仅在小窗不可用或站点拒绝内嵌登录时使用
        fb_title = QLabel("备用方式（小窗不可用或站点拒绝内嵌登录时）：在现有浏览器中登录后提取")
        fb_title.setObjectName("MutedLabel")
        browser_row = QHBoxLayout()
        browser_row.addWidget(QLabel("当前浏览器："))
        self.combo_browser = QComboBox()
        for b_info in rookie_bridge.list_supported_browsers():
            self.combo_browser.addItem(b_info["display_name"], b_info["id"])
        idx_b = self.combo_browser.findData(self.preferred_browser)
        if idx_b >= 0:
            self.combo_browser.setCurrentIndex(idx_b)
        browser_row.addWidget(self.combo_browser, 1)
        fb_row = QHBoxLayout()
        fb_row.setSpacing(8)
        self.btn_open = QPushButton("🌐 在现有浏览器打开登录页")
        self.btn_open.setObjectName("GhostButton")
        self.btn_export = QPushButton("⬇ 从现有浏览器提取并托管")
        self.btn_export.setObjectName("GhostButton")
        fb_row.addWidget(self.btn_open)
        fb_row.addWidget(self.btn_export)
        fb_row.addStretch(1)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)

        lay.addWidget(self.btn_open_win)
        lay.addLayout(action_row)
        lay.addWidget(self.lbl_status)
        lay.addSpacing(6)
        lay.addWidget(fb_title)
        lay.addLayout(browser_row)
        lay.addLayout(fb_row)

        self.btn_open_win.clicked.connect(self._open_login_window)
        self.btn_open.clicked.connect(self._open_tab_in_existing_browser)
        self.btn_export.clicked.connect(self._extract_from_existing_browser)
        self.btn_paste_host.clicked.connect(self._import_from_clipboard_for_site)
        self.btn_clear_site.clicked.connect(self._clear_current_site)

        self._refresh_site_combo_and_overview()

    def _site(self) -> SiteProfile:
        key = self.combo_site.currentData()
        for s in self._sites:
            if s.key == key:
                return s
        return self._sites[0]

    def _selected_browser_id(self) -> str:
        return str(self.combo_browser.currentData() or self.preferred_browser or "edge").strip().lower()

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
                    tag = "⚠ 凭据已过期/待更新"
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
            self.lbl_vault_overview.setText("当前尚未托管任何站点账号，请选择目标网站并点击「打开专用登录小窗」登录。")

    def _run(self, fn, on_done) -> None:
        self._worker = _Call(fn, self)
        self._worker.done.connect(on_done)
        self._worker.start()

    def _open_login_window(self) -> None:
        """弹出专用登录小窗（应用内 QtWebEngine 独立窗口）：不占用现有浏览器标签页、不新建浏览器配置、不触发 Edge 同步。"""
        site = self._site()
        win = self._login_win
        if win is not None:
            try:
                if win.site.key == site.key:
                    win.show()
                    win.raise_()
                    win.activateWindow()
                    return
                win.close()
            except RuntimeError:
                pass
            self._login_win = None
        try:
            from app.ui import login_window
        except ImportError as exc:
            QMessageBox.warning(
                self,
                "登录小窗组件不可用",
                f"当前环境缺少 QtWebEngine 组件（请安装 PySide6-Addons），无法弹出登录小窗：\n{exc}\n\n"
                "已改用备用方式：在你现有的浏览器中打开登录页，登录后点击「从现有浏览器提取并托管」，"
                "或复制 Cookie 后使用「从剪贴板导入并托管」。",
            )
            self._open_tab_in_existing_browser()
            return
        try:
            win = login_window.LoginWindow(self.paths, site, proxy=self.proxy, parent=self)
        except Exception as exc:                   # noqa: BLE001
            QMessageBox.warning(self, "打开登录小窗失败", f"{exc}\n\n请使用下方备用方式。")
            return
        win.exported.connect(self._on_window_exported)
        win.destroyed.connect(lambda *_a: setattr(self, "_login_win", None))
        self._login_win = win
        win.show_beside(self)
        self.lbl_status.setText(
            f"已弹出 {site.display_label} 登录小窗。请在小窗中完成登录，然后点击小窗底部的「我已登录：提取并托管凭据」。"
        )

    def _on_window_exported(self, site_key: str, path: str) -> None:
        custom_file = get_custom_sites_file(self.paths)
        site = next((s for s in self._sites if s.key == site_key), None) or self._site()
        report = inspect_netscape(Path(path), site.effective_refresh_url, custom_sites_file=custom_file)
        self.exported_path = path
        self.exported_site_key = site_key
        self._refresh_site_combo_and_overview(keep_key=site_key)
        self.lbl_status.setText(report.summary())
        QMessageBox.information(
            self,
            "账号凭据托管成功",
            f"已从登录小窗取得并托管 {site.display_label} 登录凭据：\n{path}\n\n{report.summary()}",
        )
        self.accept()

    def done(self, result: int) -> None:          # noqa: D401
        """关闭对话框时同步关闭仍打开的登录小窗。"""
        win = self._login_win
        self._login_win = None
        if win is not None:
            try:
                win.close()
            except RuntimeError:
                pass
        super().done(result)

    def _open_tab_in_existing_browser(self) -> None:
        """备用方式：在用户已有的浏览器中打开新标签页，绝不创建新的 --user-data-dir。"""
        site = self._site()
        b_id = self._selected_browser_id()
        url = site.effective_login_url
        ok = open_url_in_existing_browser(url, preferred_browser=b_id)
        if ok:
            self.lbl_status.setText(
                f"已在你当前的浏览器（{b_id}）中打开 {site.display_label} 新标签页。\n"
                "请在该标签页中确认处于已登录状态，然后点击「从现有浏览器提取并托管」。"
            )
        else:
            QMessageBox.warning(self, "打开标签页失败", f"未能自动调用系统浏览器打开链接，请手动在浏览器打开：\n{url}")

    def _extract_from_existing_browser(self) -> None:
        site = self._site()
        b_id = self._selected_browser_id()
        url = site.effective_refresh_url
        self.btn_export.setEnabled(False)
        self.lbl_status.setText(f"正在从当前浏览器 ({b_id}) 提取 {site.display_label} 的登录凭据…")

        def _do_extract() -> Path:
            out_dir = self.paths.data_root / "cookies"
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = get_site_cookie_path(site.key, self.paths)
            extracted = rookie_bridge.extract_to_netscape_file(
                browser=b_id,
                target_url=url,
                temp_dir=self.paths.temp_dir,
                app_bound="smart",
                force_refresh=True,
            )
            if extracted is None or not extracted.is_file():
                raise RuntimeError(
                    f"未能从 {b_id} 直接解密提取到 {site.display_label} 的登录凭据。\n"
                    "若当前使用的是启用了 v20 保护的 Edge/Chrome，可直接在刚打开的浏览器标签页中用扩展（如 Cookie-Editor / Get cookies.txt）"
                    "或按 F12 复制 Cookie，然后点击上方「📋 从剪贴板导入并托管当前站点」（仅需一次，永久自动续期）。"
                )
            shutil.copy2(extracted, dest)
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
            return dest

        self._run(_do_extract, lambda out_p, err: self._on_extracted(site, out_p, err))

    def _on_extracted(self, site: SiteProfile, out: Optional[Path], err) -> None:
        self.btn_export.setEnabled(True)
        if err or out is None:
            self.lbl_status.setText(f"提取未成功：{err}")
            return
        custom_file = get_custom_sites_file(self.paths)
        report = inspect_netscape(out, site.effective_refresh_url, custom_sites_file=custom_file)
        self._refresh_site_combo_and_overview(keep_key=site.key)
        self.lbl_status.setText(report.summary())
        if report.ok:
            self.exported_path = str(out)
            self.exported_site_key = site.key
            QMessageBox.information(
                self,
                "账号凭据托管成功",
                f"已成功从浏览器提取并托管 {site.display_label} 登录凭据：\n{out}\n\n{report.summary()}",
            )
            self.accept()

    def _import_from_clipboard_for_site(self) -> None:
        site = self._site()
        cb = QGuiApplication.clipboard()
        clip_text = cb.text().strip() if cb is not None else ""
        if not clip_text:
            QMessageBox.warning(
                self,
                "剪贴板为空",
                f"当前剪贴板中没有文本内容。\n请先在浏览器已打开的 {site.display_label} 标签页中复制 Cookie（支持 Netscape、JSON 或 F12 原始 Cookie 请求头）。",
            )
            return
        netscape_text, count, _sites = downloader.normalize_cookie_text(
            clip_text,
            target_url=site.effective_refresh_url,
        )
        if count <= 0 or not netscape_text:
            QMessageBox.warning(
                self,
                "未能识别 Cookie",
                "剪贴板中的文本不是有效的 Cookie 格式（支持 Netscape .txt、JSON 或 F12 原始 Cookie 字符串）。",
            )
            return

        dest = get_site_cookie_path(site.key, self.paths)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(netscape_text, encoding="utf-8")
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass

        custom_file = get_custom_sites_file(self.paths)
        report = inspect_netscape(dest, site.effective_refresh_url, custom_sites_file=custom_file)
        self._refresh_site_combo_and_overview(keep_key=site.key)
        self.lbl_status.setText(report.summary())
        if report.ok:
            self.exported_path = str(dest)
            self.exported_site_key = site.key
            QMessageBox.information(
                self,
                "剪贴板托管成功",
                f"已将剪贴板中的 {site.display_label} 登录凭据托管保存至：\n{dest}\n\n{report.summary()}",
            )
            self.accept()
        else:
            QMessageBox.warning(
                self,
                "缺少关键登录凭据",
                f"已解析 {count} 条 Cookie，但尚未检测到 {site.display_label} 的登录态：\n{report.summary()}",
            )

    def _clear_current_site(self) -> None:
        site = self._site()
        removed = clear_hosted_site_cookies(site.key, self.paths)
        try:
            from app.ui import login_window

            login_window.clear_site_login_data(self.paths, site)
        except Exception:                          # noqa: BLE001  小窗组件不可用或未使用过时忽略
            pass
        self._refresh_site_combo_and_overview(keep_key=site.key)
        if self.exported_site_key == site.key:
            self.exported_path = None
            self.exported_site_key = ""
        if removed:
            self.lbl_status.setText(f"已清除 {site.display_label} 的本地托管凭据文件及登录小窗中的登录状态。")
        else:
            self.lbl_status.setText(f"{site.display_label} 当前没有已保存的凭据文件。")
