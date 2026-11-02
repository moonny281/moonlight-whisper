"""程序专用浏览器登录或从日常浏览器一键提取导出目标站点 Cookies 对话框。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

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

from app.core import downloader, rookie_bridge
from app.core.browser_cookies import ManagedBrowser, export_site_cookies, find_browser
from app.core.site_cookies import SITES, inspect_netscape

LOGIN_URLS = {
    "youtube": "https://www.youtube.com/",
    "bilibili": "https://passport.bilibili.com/login",
    "tiktok": "https://www.tiktok.com/login",
    "x": "https://x.com/i/flow/login",
    "instagram": "https://www.instagram.com/accounts/login/",
}

SITE_LABELS = {
    "youtube": "YouTube / Google (youtube.com)",
    "bilibili": "哔哩哔哩 (bilibili.com)",
    "tiktok": "TikTok (tiktok.com)",
    "x": "X / Twitter (x.com)",
    "instagram": "Instagram (instagram.com)",
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
    def __init__(self, paths, parent=None, proxy: str = "", preferred_browser: str = "edge") -> None:
        super().__init__(parent)
        self.setWindowTitle("获取与导出站点 Cookies")
        self.setMinimumWidth(560)
        self.paths = paths
        self.proxy = proxy
        self.preferred_browser = (preferred_browser or "edge").strip().lower() or "edge"
        self.exported_path: Optional[str] = None
        self.exported_site_key: str = ""
        self._browser: Optional[ManagedBrowser] = None
        self._worker: Optional[_Call] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        lbl_intro = QLabel(
            "请选择目标网站，并使用以下任一方式获取登录凭据（导出后永久缓存在本机，后续下载 100% 零弹窗）：\n"
            "• 方式一（免重登一键提取）：直接从你日常已登录的浏览器（Edge/Chrome/Firefox）解锁并解密导出；\n"
            "• 方式二（专用窗口登录导出）：打开独立隔离的浏览器窗口登录一次，由浏览器内核直接导出明文 Cookie（100% 绕过 Windows v20 限制与文件锁）。"
        )
        lbl_intro.setWordWrap(True)
        lay.addWidget(lbl_intro)

        site_row = QHBoxLayout()
        site_row.addWidget(QLabel("目标网站："))
        self.combo_site = QComboBox()
        for s in SITES:
            self.combo_site.addItem(SITE_LABELS.get(s.key, s.key), s.key)
        site_row.addWidget(self.combo_site, 1)
        lay.addLayout(site_row)

        self.btn_extract_now = QPushButton("⚡ 方式一：直接从日常浏览器一键提取并缓存（免重新登录）")
        self.btn_extract_now.clicked.connect(self._extract_from_daily_browser)
        lay.addWidget(self.btn_extract_now)

        self.btn_open = QPushButton("🌐 方式二 (步骤 1)：打开专用登录窗口")
        self.btn_export = QPushButton("✅ 方式二 (步骤 2)：我已在弹出窗口中登录，立即导出并保存")
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

    def _extract_from_daily_browser(self) -> None:
        site = self._site()
        url = LOGIN_URLS.get(site.key, "https://www.youtube.com/")
        b_id = self.preferred_browser or "edge"
        self.btn_extract_now.setEnabled(False)
        self.lbl_status.setText(f"正在从 {b_id} 解锁并提取 {site.key} 的登录凭据…")

        def _do_extract() -> Path:
            out_dir = self.paths.data_root / "cookies"
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"{site.key}.txt"
            extracted = rookie_bridge.extract_to_netscape_file(
                browser=b_id,
                target_url=url,
                temp_dir=self.paths.temp_dir,
                app_bound="allow_elevated_fallback",
                force_refresh=True,
            )
            if extracted is None or not extracted.is_file():
                raise RuntimeError(
                    f"未能从 {b_id} 自动提取到 {site.key} 的有效登录凭据（若受 Windows App-Bound v20 限制，请直接点击下方「方式二：打开专用登录窗口」登录一次即可 100% 解决）。"
                )
            shutil.copy2(extracted, dest)
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
            return dest

        self._run(_do_extract, lambda out_p, err: self._on_daily_extracted(site, out_p, err))

    def _on_daily_extracted(self, site, out: Optional[Path], err) -> None:
        self.btn_extract_now.setEnabled(True)
        if err or out is None:
            self.lbl_status.setText(f"方式一提取未成功：{err}\n👉 请点击下方「方式二 (步骤 1)：打开专用登录窗口」完成登录导出。")
            return
        report = inspect_netscape(out, LOGIN_URLS.get(site.key, ""))
        self.lbl_status.setText(report.summary())
        if report.ok:
            self.exported_path = str(out)
            self.exported_site_key = site.key
            QMessageBox.information(
                self,
                "提取并缓存成功",
                f"已成功从浏览器提取 {site.key} 登录凭据并永久保存至：\n{out}\n\n{report.summary()}",
            )
            self.accept()

    def _open_browser(self) -> None:
        exe = find_browser("edge") or find_browser("chrome")
        if exe is None:
            QMessageBox.warning(self, "未找到浏览器", "未检测到 Edge 或 Chrome，请改用 Firefox 读取或手动导入 cookies.txt。")
            return
        url = LOGIN_URLS.get(self._site().key, "about:blank")
        eff_proxy = downloader.resolve_effective_proxy(self.proxy, target_url=url)
        self._browser = ManagedBrowser(exe, self.paths.data_root / "browser_profile", proxy=eff_proxy)
        self.btn_open.setEnabled(False)
        self.lbl_status.setText("正在启动专用浏览器窗口…")
        self._run(lambda: self._browser.start(url), self._on_opened)

    def _on_opened(self, _res, err) -> None:
        if err:
            self.btn_open.setEnabled(True)
            self.lbl_status.setText(f"启动失败：{err}")
            return
        self.btn_export.setEnabled(True)
        self.lbl_status.setText("请在弹出的浏览器窗口中登录目标网站（如 YouTube），看到右上角头像已登录后点击上方「步骤 2：我已在弹出窗口中登录，立即导出并保存」。")

    def _export(self) -> None:
        site = self._site()
        out = self.paths.data_root / "cookies" / f"{site.key}.txt"
        self.btn_export.setEnabled(False)
        self._run(
            lambda: export_site_cookies(self._browser, out, (".youtube.com", ".google.com") if site.key == "youtube" else site.cookie_domains),
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
