"""网站账号托管与登录对话框（v1.4.2：调用系统 Edge/Chrome 的安全登录小窗 + 内置小窗 + 剪贴板导入）。"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, QTimer, Signal
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

from app.core import downloader
from app.core.browser_cookies import (
    clear_hosted_site_cookies,
    get_custom_sites_file,
    get_site_cookie_path,
    list_hosted_sites_status,
)
from app.core.browser_login import BrowserLoginSession, available_login_browsers
from app.core.site_cookies import SITES, SiteProfile, inspect_netscape, load_all_sites

LOGIN_URLS = {s.key: s.effective_login_url for s in SITES}
SITE_LABELS = {s.key: s.display_label for s in SITES}


_ALIVE_WORKERS: set = set()      # 持有后台线程引用直到其结束，避免对话框关闭时线程被提前销毁


class _Call(QThread):
    """把阻塞调用（启动浏览器、读取 Cookie、关闭浏览器）放到后台线程，避免卡住界面。"""
    done = Signal(object, object)            # (result, error)

    def __init__(self, fn, parent=None) -> None:
        super().__init__(parent)
        self._fn = fn
        _ALIVE_WORKERS.add(self)
        self.finished.connect(lambda: _ALIVE_WORKERS.discard(self))

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
        self._login_win = None
        self._session: Optional[BrowserLoginSession] = None
        self._session_site_key = ""
        self._busy: Optional[_Call] = None
        self._poll_busy = False
        self._gone_polls = 0
        self._closing = False
        self._poll_worker = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._poll_session)
        self._sites = load_all_sites(get_custom_sites_file(self.paths))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        lbl_intro = QLabel(
            "使用电脑上真实的 Edge / Chrome 打开一个专门用于登录的小窗（单窗口，无标签页；使用独立托管目录并关闭账号同步，"
            "不影响你日常浏览器的页面和账号）。Google 等站点认可这种真实浏览器环境：\n"
            "• 步骤 1：点击「打开安全登录小窗」，在小窗中登录目标网站；\n"
            "• 步骤 2：登录完成后点击「我已登录，提取并托管凭据」，凭据从小窗直接取得并保存在本机，下载时自动使用并回写刷新令牌。"
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

        browser_row = QHBoxLayout()
        browser_row.addWidget(QLabel("登录浏览器："))
        self.combo_login_browser = QComboBox()
        self._browsers = available_login_browsers()
        names = {"edge": "Microsoft Edge", "chrome": "Google Chrome"}
        for kind, _exe in self._browsers:
            self.combo_login_browser.addItem(names.get(kind, kind), kind)
        if not self._browsers:
            self.combo_login_browser.addItem("未检测到 Edge / Chrome", "")
        idx_b = self.combo_login_browser.findData(self.preferred_browser)
        if idx_b >= 0:
            self.combo_login_browser.setCurrentIndex(idx_b)
        browser_row.addWidget(self.combo_login_browser, 1)
        lay.addLayout(browser_row)

        self.btn_open_browser = QPushButton("🪟 步骤 1：打开安全登录小窗（Edge / Chrome）")
        self.btn_open_browser.setObjectName("PrimaryButton")
        self.btn_open_browser.setEnabled(bool(self._browsers))
        self.btn_finish = QPushButton("✅ 步骤 2：我已登录，提取并托管凭据")
        self.btn_finish.setObjectName("PrimaryButton")
        self.btn_finish.setEnabled(False)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.btn_open_win = QPushButton("内置小窗登录（B站/抖音等；Google 会拦截）")
        self.btn_open_win.setObjectName("GhostButton")
        self.btn_open_win.setToolTip("应用内置的 QtWebEngine 小窗；Google/YouTube 请使用上方的系统浏览器小窗")
        self.btn_paste_host = QPushButton("📋 从剪贴板导入并托管")
        self.btn_paste_host.setToolTip("在任意浏览器复制 Cookie（Netscape / JSON / F12 原始 Cookie 头）后点击此按钮直接托管")
        self.btn_clear_site = QPushButton("🗑️ 清除当前站点凭据")
        self.btn_clear_site.setObjectName("GhostButton")
        action_row.addWidget(self.btn_open_win)
        action_row.addWidget(self.btn_paste_host)
        action_row.addWidget(self.btn_clear_site)
        action_row.addStretch(1)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)

        lay.addWidget(self.btn_open_browser)
        lay.addWidget(self.btn_finish)
        lay.addLayout(action_row)
        lay.addWidget(self.lbl_status)

        self.btn_open_browser.clicked.connect(self._open_browser_login)
        self.btn_finish.clicked.connect(self._finish_browser_login)
        self.btn_open_win.clicked.connect(self._open_login_window)
        self.btn_paste_host.clicked.connect(self._import_from_clipboard_for_site)
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
            self.lbl_vault_overview.setText("当前尚未托管任何站点账号，请选择目标网站并点击「打开安全登录小窗」登录。")

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
            try:
                import PySide6

                qt_ver = str(getattr(PySide6, "__version__", "")).strip()
            except Exception:                      # noqa: BLE001
                qt_ver = ""
            pin = f"=={qt_ver}" if qt_ver else ""
            QMessageBox.warning(
                self,
                "登录小窗组件不可用",
                "无法加载 QtWebEngine 组件，登录小窗暂时不能使用。\n\n"
                f"错误信息：{exc}\n\n"
                "开发环境请在命令行执行（Addons 版本必须与当前 PySide6 版本一致）：\n"
                f"pip install \"PySide6-Addons{pin}\"\n"
                "或直接 pip install -r requirements.txt 后重启程序。\n\n"
                "临时办法：在浏览器登录后复制 Cookie，使用「从剪贴板导入并托管」。",
            )
            return
        try:
            win = login_window.LoginWindow(self.paths, site, proxy=self.proxy, parent=self)
        except Exception as exc:                   # noqa: BLE001
            QMessageBox.warning(self, "打开登录小窗失败", f"{exc}\n\n可改用「从剪贴板导入并托管」。")
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
        self._closing = True
        self._poll_timer.stop()
        session = self._session
        self._session = None
        if session is not None:
            threading.Thread(target=session.close, daemon=True).start()
        win = self._login_win
        self._login_win = None
        if win is not None:
            try:
                win.close()
            except RuntimeError:
                pass
        super().done(result)

    # ---------- 系统浏览器安全登录小窗 ----------
    def _run(self, fn, on_done) -> None:
        self._busy = _Call(fn)
        self._busy.done.connect(on_done)
        self._busy.start()

    def _open_browser_login(self) -> None:
        site = self._site()
        if self._session is not None and self._session.alive():
            if self._session_site_key == site.key:
                self.lbl_status.setText("登录小窗已经打开，请在小窗中完成登录后点击「步骤 2」。")
            else:
                self.lbl_status.setText("登录小窗仍在使用中：请先完成当前站点（或关闭小窗）后再切换站点。")
            return
        if self._busy is not None and self._busy.isRunning():
            return
        kind = str(self.combo_login_browser.currentData() or self.preferred_browser or "edge")
        geo = self.frameGeometry()
        pos = (geo.right() + 8, max(0, geo.top()))
        session = BrowserLoginSession(site, self.paths, proxy=self.proxy, preferred_browser=kind, window_pos=pos)
        self.btn_open_browser.setEnabled(False)
        self.lbl_status.setText(f"正在启动 {kind} 登录小窗…")
        self._run(session.start, lambda _r, err: self._on_session_started(session, site, err))

    def _on_session_started(self, session: BrowserLoginSession, site: SiteProfile, err) -> None:
        if self._closing:                      # 对话框已关闭：直接收掉刚启动的小窗
            threading.Thread(target=session.close, daemon=True).start()
            return
        self.btn_open_browser.setEnabled(bool(self._browsers))
        if err is not None:
            threading.Thread(target=session.close, daemon=True).start()
            self.lbl_status.setText(f"启动登录小窗失败：{err}")
            QMessageBox.warning(self, "启动登录小窗失败", f"{err}\n\n可改用「从剪贴板导入并托管」。")
            return
        self._session = session
        self._session_site_key = site.key
        self.btn_finish.setEnabled(True)
        self._poll_timer.start()
        self.lbl_status.setText(
            f"已打开 {site.display_label} 登录小窗（{session.browser_kind}）。请在小窗中完成登录；"
            "登录完成后回到本窗口点击「步骤 2」。"
        )

    def _poll_session(self) -> None:
        session = self._session
        if session is None or self._poll_busy:
            return
        site = self._site() if self._session_site_key == "" else next(
            (s for s in self._sites if s.key == self._session_site_key), self._site()
        )
        self._poll_busy = True

        def _done(state, err) -> None:
            if self._closing:
                return
            self._poll_busy = False
            if self._session is not session:
                return
            if err is not None or state is None:
                return
            if state.alive:
                self._gone_polls = 0
            else:
                self._gone_polls += 1
            if not state.alive and self._gone_polls >= 2:       # 连续两次都探测不到才认定小窗已关闭，避免一次偶发超时误判
                self._poll_timer.stop()
                self.lbl_status.setText(
                    "登录小窗已关闭。若已完成登录，仍可点击「步骤 2」从托管目录中读取凭据；否则请重新打开小窗。"
                )
            elif not state.alive:
                return
            elif state.has_auth and state.on_site:
                self.lbl_status.setText(f"✓ 已检测到 {site.display_label} 的登录凭据。确认小窗已处于登录状态后，点击「步骤 2」。")
            elif state.has_auth:
                self.lbl_status.setText("已检测到登录凭据，请等待页面回到目标网站后点击「步骤 2」。")

        worker = _Call(session.poll)
        worker.done.connect(_done)
        self._poll_worker = worker
        worker.start()

    def _finish_browser_login(self) -> None:
        session = self._session
        if session is None:
            return
        if self._busy is not None and self._busy.isRunning():
            return
        site = next((s for s in self._sites if s.key == self._session_site_key), self._site())
        self.btn_finish.setEnabled(False)
        self._poll_timer.stop()
        self.lbl_status.setText(f"正在提取并托管 {site.display_label} 的登录凭据…")
        self._run(session.finish, lambda res, err: self._on_finished(session, site, res, err))

    def _on_finished(self, session: BrowserLoginSession, site: SiteProfile, res, err) -> None:
        if self._closing:
            return
        if err is not None or res is None:
            self.btn_finish.setEnabled(True)
            self.lbl_status.setText(f"提取未成功：{err}")
            return
        if res.path is None:
            # 小窗仍开着时让用户继续登录
            self.btn_finish.setEnabled(session.alive())
            if session.alive():
                self._poll_timer.start()
            self.lbl_status.setText(res.message or "提取未成功")
            QMessageBox.warning(self, "尚未检测到登录凭据", res.message or "请先在小窗中完成登录。")
            return
        self._session = None
        self._session_site_key = ""
        self.exported_path = str(res.path)
        self.exported_site_key = site.key
        self._refresh_site_combo_and_overview(keep_key=site.key)
        extra = f"\n同时托管了小窗中已登录的其他站点：{'、'.join(res.also_hosted)}" if res.also_hosted else ""
        self.lbl_status.setText(res.report.summary())
        QMessageBox.information(
            self,
            "账号凭据托管成功",
            f"已从登录小窗取得并托管 {site.display_label} 登录凭据：\n{res.path}\n\n{res.report.summary()}{extra}",
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
