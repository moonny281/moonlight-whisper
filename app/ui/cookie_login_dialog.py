"""程序专用浏览器登录并导出目标站点 Cookies 对话框（基于 Chrome DevTools Protocol）。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QComboBox, QDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout

from app.core.browser_cookies import ManagedBrowser, export_site_cookies, find_browser
from app.core.site_cookies import SITES, inspect_netscape

LOGIN_URLS = {
    "youtube": "https://www.youtube.com/",
    "bilibili": "https://passport.bilibili.com/login",
    "tiktok": "https://www.tiktok.com/login",
    "x": "https://x.com/i/flow/login",
    "instagram": "https://www.instagram.com/accounts/login/",
}


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
    def __init__(self, paths, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("登录并获取 Cookies")
        self.setMinimumWidth(520)
        self.paths = paths
        self.exported_path: Optional[str] = None
        self.exported_site_key: str = ""
        self._browser: Optional[ManagedBrowser] = None
        self._worker: Optional[_Call] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        lbl_intro = QLabel(
            "将打开一个本程序专用的浏览器窗口（与你日常浏览器的数据完全隔离）。\n"
            "请在其中登录目标网站，登录成功后回到这里点击「我已登录，导出」。\n"
            "导出只包含所选网站的 Cookies，保存在本机，不会上传。"
        )
        lbl_intro.setWordWrap(True)
        lay.addWidget(lbl_intro)
        self.combo_site = QComboBox()
        for s in SITES:
            self.combo_site.addItem(s.key, s.key)
        lay.addWidget(self.combo_site)
        self.btn_open = QPushButton("1. 打开登录窗口")
        self.btn_export = QPushButton("2. 我已登录，导出")
        self.btn_export.setObjectName("PrimaryButton")
        self.btn_export.setEnabled(False)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        for w in (self.btn_open, self.btn_export, self.lbl_status):
            lay.addWidget(w)
        self.btn_open.clicked.connect(self._open_browser)
        self.btn_export.clicked.connect(self._export)

    def _site(self):
        return next(s for s in SITES if s.key == self.combo_site.currentData())

    def _run(self, fn, on_done) -> None:
        self._worker = _Call(fn, self)
        self._worker.done.connect(on_done)
        self._worker.start()

    def _open_browser(self) -> None:
        exe = find_browser("edge") or find_browser("chrome")
        if exe is None:
            QMessageBox.warning(self, "未找到浏览器", "未检测到 Edge 或 Chrome，请改用 Firefox 读取或手动导入 cookies.txt。")
            return
        self._browser = ManagedBrowser(exe, self.paths.data_root / "browser_profile")
        url = LOGIN_URLS.get(self._site().key, "about:blank")
        self.btn_open.setEnabled(False)
        self.lbl_status.setText("正在启动浏览器…")
        self._run(lambda: self._browser.start(url), self._on_opened)

    def _on_opened(self, _res, err) -> None:
        if err:
            self.btn_open.setEnabled(True)
            self.lbl_status.setText(f"启动失败：{err}")
            return
        self.btn_export.setEnabled(True)
        self.lbl_status.setText("请在弹出的窗口中完成登录。")

    def _export(self) -> None:
        site = self._site()
        out = self.paths.data_root / "cookies" / f"{site.key}.txt"
        self.btn_export.setEnabled(False)
        self._run(
            lambda: export_site_cookies(self._browser, out, site.cookie_domains),
            lambda n, e: self._on_exported(site, out, n, e),
        )

    def _on_exported(self, site, out: Path, count, err) -> None:
        if err:
            self.btn_export.setEnabled(True)
            self.lbl_status.setText(f"导出失败：{err}")
            return
        report = inspect_netscape(out, LOGIN_URLS.get(site.key, ""))
        self.lbl_status.setText(report.summary())
        if report.ok:
            self.exported_path = str(out)
            self.exported_site_key = site.key
            self.accept()
        else:
            self.btn_export.setEnabled(True)       # 让用户继续登录后再导出一次

    def done(self, result: int) -> None:           # 对话框关闭时一并关闭专用浏览器，保证 Cookie 落盘
        if self._browser is not None:
            self._browser.close()
        super().done(result)
