"""专用登录小窗（v1.4.0）。

在应用内弹出一个独立的小窗口（QtWebEngine）完成网站登录并直接获取登录凭证：
- 与用户日常浏览器的窗口、标签页完全无关，不会打断正在浏览的页面；
- 不是浏览器配置目录（不使用 Edge/Chrome 的 `--user-data-dir`），因此不会触发 Edge 账号同步弹出多个窗口和标签页；
- Cookie 通过 `QWebEngineCookieStore` 事件以明文直接回传，不受 App-Bound (v20) 加密、数据库占用、Cookie 延迟落盘影响。

本模块顶层直接导入 QtWebEngine；若环境未安装 `PySide6-Addons`，导入会抛 ImportError，由调用方降级到备用方式。
"""
from __future__ import annotations

import time
from typing import Dict, Optional

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkProxy
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.browser_cookies import get_custom_sites_file
from app.core.site_cookies import SiteProfile
from app.core.webview_login import (
    CookieCollector,
    cookie_matches_site,
    export_site_from_rows,
    get_login_window_dir,
    parse_proxy_url,
    sanitize_user_agent,
)
from app.paths import PATHS, AppPaths

_WINDOW_W = 480
_WINDOW_H = 780


class _LoginProfile(QObject):
    """全应用共享的一个登录小窗 Profile（QtWebEngine 自己的存储，不是浏览器配置）。持久化 Cookie，下次打开仍保持登录。"""

    cookiesChanged = Signal()

    def __init__(self, paths: AppPaths) -> None:
        super().__init__(QApplication.instance())
        root = get_login_window_dir(paths)
        (root / "storage").mkdir(parents=True, exist_ok=True)
        (root / "cache").mkdir(parents=True, exist_ok=True)

        self.collector = CookieCollector()
        self._purge: Dict[str, "tuple[SiteProfile, float]"] = {}

        self.profile = QWebEngineProfile("MoonlightWhisperLogin", self)
        self.profile.setPersistentStoragePath(str(root / "storage"))
        self.profile.setCachePath(str(root / "cache"))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self.profile.setHttpUserAgent(sanitize_user_agent(self.profile.httpUserAgent()))
        self.profile.setHttpAcceptLanguage("zh-CN,zh;q=0.9,en;q=0.8")
        self.profile.downloadRequested.connect(lambda item: item.cancel())    # 登录小窗不接收任何下载

        self.store = self.profile.cookieStore()
        self.store.cookieAdded.connect(self._on_added)
        self.store.cookieRemoved.connect(self._on_removed)
        self.store.loadAllCookies()                                           # 触发已持久化 Cookie 的 cookieAdded 回放

    def _on_added(self, qc) -> None:
        for site, until in list(self._purge.values()):
            if time.time() < until and cookie_matches_site(qc, site):
                self.store.deleteCookie(qc)
                return
        self.collector.add(qc)
        self.cookiesChanged.emit()

    def _on_removed(self, qc) -> None:
        self.collector.remove(qc)
        self.cookiesChanged.emit()

    def purge_site(self, site: SiteProfile) -> None:
        """删除小窗存储中属于该站点的全部 Cookie（含尚在异步加载中的持久化 Cookie）。"""
        self._purge[site.key] = (site, time.time() + 6.0)
        for qc in self.collector.raw_for_site(site):
            self.store.deleteCookie(qc)


_HOLDER: Optional[_LoginProfile] = None


def get_login_profile(paths: AppPaths = PATHS) -> _LoginProfile:
    global _HOLDER
    if _HOLDER is None:
        _HOLDER = _LoginProfile(paths)
    return _HOLDER


def clear_site_login_data(paths: AppPaths, site: SiteProfile) -> None:
    """「清除当前站点凭据」时同步清除登录小窗里该站点的登录状态，便于切换账号重新登录。"""
    get_login_profile(paths).purge_site(site)


def _apply_proxy(proxy: str) -> Optional[QNetworkProxy]:
    """把设置里的代理应用到登录小窗（QtWebEngine 使用 QNetworkProxy 应用级代理）。返回应用前的代理，用于关闭小窗时恢复；无需改动返回 None。"""
    cfg = parse_proxy_url(proxy)
    if cfg is None:
        return None
    previous = QNetworkProxy.applicationProxy()
    kind = QNetworkProxy.ProxyType.Socks5Proxy if cfg["scheme"] == "socks5" else QNetworkProxy.ProxyType.HttpProxy
    QNetworkProxy.setApplicationProxy(QNetworkProxy(kind, cfg["host"], cfg["port"], cfg["user"], cfg["password"]))
    return previous


class _LoginPage(QWebEnginePage):
    """支持 window.open 弹窗（如「使用 Google 登录」）：在另一个小窗里打开，并共享同一个 Profile。"""

    def __init__(self, profile: QWebEngineProfile, parent: QWidget) -> None:
        super().__init__(profile, parent)
        self._owner = parent

    def createWindow(self, _type):  # noqa: N802 (Qt 接口命名)
        popup = _PopupWindow(self.profile(), self._owner)
        popup.show()
        return popup.page


class _PopupWindow(QWidget):
    def __init__(self, profile: QWebEngineProfile, owner: QWidget) -> None:
        super().__init__(owner, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("登录验证")
        self.resize(460, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.page = _LoginPage(profile, self)
        self.view.setPage(self.page)
        self.page.windowCloseRequested.connect(self.close)
        self.view.titleChanged.connect(lambda t: self.setWindowTitle(t or "登录验证"))
        lay.addWidget(self.view)


class LoginWindow(QWidget):
    """专用登录小窗：打开站点登录页 -> 用户登录 -> 点击「完成」提取并托管凭据。"""

    exported = Signal(str, str)           # (site_key, 已托管的 Cookie 文件路径)

    def __init__(
        self,
        paths: AppPaths,
        site: SiteProfile,
        proxy: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.paths = paths
        self.site = site
        self._prev_proxy = _apply_proxy(proxy)
        self._proxy_applied = self._prev_proxy is not None
        self._holder = get_login_profile(paths)
        self._custom_sites_file = get_custom_sites_file(paths)
        self._done = False

        self.setWindowTitle(f"登录小窗 · {site.display_label}")
        self.setMinimumSize(400, 560)
        self.resize(_WINDOW_W, _WINDOW_H)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        nav = QHBoxLayout()
        nav.setSpacing(4)
        self.btn_back = QToolButton()
        self.btn_back.setText("←")
        self.btn_forward = QToolButton()
        self.btn_forward.setText("→")
        self.btn_reload = QToolButton()
        self.btn_reload.setText("⟳")
        self.edit_url = QLineEdit()
        self.edit_url.setReadOnly(True)
        for w in (self.btn_back, self.btn_forward, self.btn_reload):
            nav.addWidget(w)
        nav.addWidget(self.edit_url, 1)
        lay.addLayout(nav)

        self.view = QWebEngineView(self)
        self.page = _LoginPage(self._holder.profile, self)
        self.view.setPage(self.page)
        lay.addWidget(self.view, 1)

        self.lbl_state = QLabel("")
        self.lbl_state.setWordWrap(True)
        lay.addWidget(self.lbl_state)

        bottom = QHBoxLayout()
        self.btn_done = QPushButton("✅ 我已登录：提取并托管凭据")
        self.btn_done.setObjectName("PrimaryButton")
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("GhostButton")
        bottom.addWidget(self.btn_done, 1)
        bottom.addWidget(self.btn_cancel)
        lay.addLayout(bottom)

        self.btn_back.clicked.connect(self.view.back)
        self.btn_forward.clicked.connect(self.view.forward)
        self.btn_reload.clicked.connect(self.view.reload)
        self.view.urlChanged.connect(lambda u: self.edit_url.setText(u.toString()))
        self.btn_done.clicked.connect(self.export_now)
        self.btn_cancel.clicked.connect(self.close)
        self.page.windowCloseRequested.connect(self.close)

        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(600)
        self._check_timer.timeout.connect(self._refresh_state)
        self._holder.cookiesChanged.connect(self._schedule_check)

        self._refresh_state()
        self.view.load(QUrl(site.effective_login_url))

    # ---------- 状态 ----------
    def _schedule_check(self) -> None:
        self._check_timer.start()

    def _refresh_state(self) -> None:
        if self._holder.collector.has_auth(self.site, self.paths):
            self.lbl_state.setText("✓ 已检测到登录凭据。请确认页面已处于登录状态，然后点击「我已登录：提取并托管凭据」。")
        else:
            hint = "请在上方页面完成登录（含验证码/二次验证）。"
            if self.site.key == "youtube":
                hint += "登录 Google 账号后请回到 YouTube 首页，再点击下方按钮。"
            self.lbl_state.setText("尚未检测到登录凭据。" + hint)

    # ---------- 导出 ----------
    def export_now(self) -> None:
        rows = self._holder.collector.rows()
        dest, report = export_site_from_rows(rows, self.site, self.paths)
        if dest is None:
            QMessageBox.warning(
                self,
                "尚未检测到登录凭据",
                f"当前小窗中还没有 {self.site.display_label} 的有效登录凭据：\n{report.summary()}\n\n"
                "请先在小窗中完成登录（若页面已显示已登录，请稍等几秒或刷新页面后再试）。",
            )
            return
        self._done = True
        self.exported.emit(self.site.key, str(dest))
        QTimer.singleShot(0, self.close)

    # ---------- 窗口摆放 ----------
    def show_beside(self, anchor: Optional[QWidget]) -> None:
        screen = None
        if anchor is not None and anchor.screen() is not None:
            screen = anchor.screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        h = min(_WINDOW_H, max(560, avail.height() - 60))
        self.resize(_WINDOW_W, h)
        x = avail.left() + (avail.width() - _WINDOW_W) // 2
        y = avail.top() + (avail.height() - h) // 2
        if anchor is not None:
            geo = anchor.frameGeometry()
            x = geo.right() + 8
            if x + _WINDOW_W > avail.right():
                x = geo.left() - _WINDOW_W - 8
            if x < avail.left():
                x = avail.left() + (avail.width() - _WINDOW_W) // 2
            y = max(avail.top(), min(geo.top(), avail.bottom() - h))
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._holder.cookiesChanged.disconnect(self._schedule_check)
        except (RuntimeError, TypeError):
            pass
        try:
            self.view.stop()
        except RuntimeError:
            pass
        if self._proxy_applied and self._prev_proxy is not None:
            QNetworkProxy.setApplicationProxy(self._prev_proxy)
            self._proxy_applied = False
        super().closeEvent(event)
