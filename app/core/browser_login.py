"""系统浏览器登录小窗会话（v1.4.2，零 Qt 依赖）。

为什么需要它：内嵌的 QtWebEngine 小窗会被 Google 判定为「不安全的浏览器环境」而拒绝登录。
这里改为调用电脑上真实的 Microsoft Edge / Google Chrome，以 `--app=URL` 的单窗口小窗模式打开登录页：
- 是真正的 Edge/Chrome，Google 等站点不会拦截；
- 只有一个无标签栏、无地址栏的小窗，不会产生一堆标签页，也不影响你日常浏览器里已打开的窗口；
- 使用应用自己的独立托管目录（`account_vault/browser_profile`），与日常浏览器的配置、书签、登录完全隔离，
  并关闭账号同步（`--disable-sync`）与首次运行向导，避免 Edge 弹出同步/登录窗口；
- 登录凭据通过本地调试端口 (CDP) 以明文读取，不受 Windows App-Bound (v20) 加密、数据库占用、Cookie 延迟落盘影响；
- 托管目录同时让下载时的「后台无头静默刷新」重新可用。
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.browser_cookies import (
    BrowserCookieError,
    ManagedBrowser,
    find_browser,
    get_custom_sites_file,
    get_vault_profile_dir,
    refresh_hosted_site_cookies,
)
from app.core.site_cookies import CookieReport, SiteProfile, inspect_netscape, load_all_sites, rows_have_site_auth
from app.core.webview_login import export_site_from_rows, parse_proxy_url
from app.paths import PATHS, AppPaths

LOGIN_WINDOW_SIZE: Tuple[int, int] = (480, 780)
_log = logging.getLogger("moonlight_whisper")
_AUTH_WAIT_SEC = 8.0          # 点击「提取」后，最多等待这么久让登录 Cookie 就绪（如 Google 登录后回跳站点才写入的令牌）


def describe_rows(rows: List[Dict[str, Any]], limit: int = 6) -> str:
    """把读取到的 Cookie 概况写成一行诊断文字（条数 + 主要域名），用于「找不到凭据」时排查。"""
    if not rows:
        return "小窗中一条 Cookie 都没有读到"
    counts: Dict[str, int] = {}
    for r in rows:
        dom = str(r.get("domain") or "").lstrip(".").lower() or "?"
        counts[dom] = counts.get(dom, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return f"共读到 {len(rows)} 条 Cookie，主要域名：" + "、".join(f"{d}({n})" for d, n in top)


def available_login_browsers() -> List[Tuple[str, Path]]:
    """本机已安装、可用于登录小窗的浏览器：[(id, exe)]，Edge 在前。"""
    found: List[Tuple[str, Path]] = []
    for kind in ("edge", "chrome"):
        exe = find_browser(kind)
        if exe is not None:
            found.append((kind, exe))
    return found


def pick_login_browser(preferred: str = "") -> Optional[Tuple[str, Path]]:
    """优先使用 preferred（edge/chrome），否则取第一个可用浏览器；都没有返回 None。"""
    avail = available_login_browsers()
    want = (preferred or "").strip().lower()
    for kind, exe in avail:
        if kind == want:
            return kind, exe
    return avail[0] if avail else None


def chromium_proxy_arg(proxy: str) -> str:
    """设置里的代理字符串 -> Chromium `--proxy-server` 值（Chromium 不支持在该参数里携带账号密码）。"""
    cfg = parse_proxy_url(proxy)
    if cfg is None:
        return ""
    return f"{cfg['scheme']}://{cfg['host']}:{cfg['port']}"


def _url_on_site(url: str, site: SiteProfile) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and any(host == h or host.endswith("." + h) for h in site.hosts)


@dataclass
class PollState:
    alive: bool = False
    has_auth: bool = False        # 已检测到该站点的关键登录 Cookie
    on_site: bool = False         # 小窗当前页面已回到目标站点（如登录 Google 后回到 youtube.com）


@dataclass
class FinishResult:
    path: Optional[Path] = None
    report: CookieReport = field(default_factory=CookieReport)
    also_hosted: List[str] = field(default_factory=list)      # 顺带托管的其他已登录站点名称
    closed: bool = False
    message: str = ""


class BrowserLoginSession:
    """一次登录小窗会话：启动 -> 轮询登录状态 -> 提取并托管 -> 关闭。"""

    def __init__(
        self,
        site: SiteProfile,
        paths: AppPaths = PATHS,
        proxy: str = "",
        preferred_browser: str = "edge",
        window_pos: Optional[Tuple[int, int]] = None,
    ) -> None:
        self.site = site
        self.paths = paths
        self.proxy = proxy
        self.preferred_browser = preferred_browser
        self.window_pos = window_pos
        self.browser_kind = ""
        self.browser: Optional[ManagedBrowser] = None
        self._closed = False

    # ---------- 生命周期 ----------
    def start(self, timeout: float = 25.0) -> None:
        picked = pick_login_browser(self.preferred_browser)
        if picked is None:
            raise BrowserCookieError("未检测到 Microsoft Edge 或 Google Chrome，请先安装其中之一")
        self.browser_kind, exe = picked
        self.browser = ManagedBrowser(
            exe,
            get_vault_profile_dir(self.paths),
            headless=False,
            proxy=chromium_proxy_arg(self.proxy),
            app_window=True,
            window_size=LOGIN_WINDOW_SIZE,
            window_pos=self.window_pos,
        )
        self.browser.start(self.site.effective_login_url, timeout=timeout)

    def alive(self) -> bool:
        return (not self._closed) and self.browser is not None and self.browser.alive()

    def close(self) -> None:
        """幂等关闭：优雅关闭浏览器使新 Cookie 落盘，并清理会话缓存防止标签页恢复。"""
        if self._closed:
            return
        self._closed = True
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass

    # ---------- 状态轮询 ----------
    def poll(self) -> PollState:
        st = PollState()
        if not self.alive():
            return st
        st.alive = True
        try:
            rows = self.browser.get_cookies(quick=True)  # type: ignore[union-attr]
        except Exception:
            rows = []
        st.has_auth = rows_have_site_auth(
            rows, self.site.effective_refresh_url, custom_sites_file=get_custom_sites_file(self.paths)
        ) if rows else False
        st.on_site = any(_url_on_site(u, self.site) for u in self.browser.page_urls())   # type: ignore[union-attr]
        return st

    # ---------- 提取并托管 ----------
    def _read_rows_until_auth(self) -> List[Dict[str, Any]]:
        """读取小窗中的 Cookie；若还没有目标站点的登录凭据，最多再等几秒重试（页面刚回跳时令牌可能尚未写入）。"""
        custom = get_custom_sites_file(self.paths)
        deadline = time.time() + _AUTH_WAIT_SEC
        rows: List[Dict[str, Any]] = []
        while True:
            rows = self.browser.get_cookies()           # type: ignore[union-attr]
            if rows_have_site_auth(rows, self.site.effective_refresh_url, custom_sites_file=custom):
                return rows
            if time.time() >= deadline or not self.browser.alive():   # type: ignore[union-attr]
                return rows
            time.sleep(1.5)

    def finish(self) -> FinishResult:
        """提取并托管凭据。

        - 小窗仍在：读取 Cookie，校验通过才写入并关闭小窗；**读不到有效凭据时绝不关闭小窗**，让用户继续登录。
        - 小窗已被用户手动关闭：改用后台无头模式从托管目录读取（此时不再去「清理/结束」任何进程）。
        """
        res = FinishResult()
        still_open = self.alive()
        if not still_open and not self._closed:
            for _ in range(3):                 # 复核：避免一次偶发的探测超时把仍开着的小窗误判为已关闭
                time.sleep(0.7)
                if self.alive():
                    still_open = True
                    break
        if still_open:
            try:
                rows = self._read_rows_until_auth()
            except Exception as exc:
                res.message = f"读取小窗中的登录凭据失败：{exc}（小窗未关闭，可稍后重试）"
                _log.warning("[登录小窗] %s", res.message)
                return res
            dest, report = export_site_from_rows(rows, self.site, self.paths)
            res.report = report
            if dest is None:
                res.message = (
                    f"小窗中尚未检测到 {self.site.display_label} 的有效登录凭据。\n"
                    f"{report.summary()}\n{describe_rows(rows)}\n"
                    "请确认小窗已完成登录并回到目标网站页面后，再点击「提取」（小窗不会被关闭）。"
                )
                _log.warning("[登录小窗] 未检测到登录凭据：%s", describe_rows(rows))
                return res
            res.path = dest
            custom = get_custom_sites_file(self.paths)
            # 顺带托管小窗里已登录的其他站点（仅限有明确关键登录 Cookie 的站点，且校验通过才写入）
            for other in load_all_sites(custom):
                if other.key == self.site.key or not other.key_cookies:
                    continue
                if not rows_have_site_auth(rows, other.effective_refresh_url, custom_sites_file=custom):
                    continue
                o_path, _o_rep = export_site_from_rows(rows, other, self.paths)
                if o_path is not None:
                    res.also_hosted.append(other.display_label)
            self.close()
            res.closed = True
            return res

        # 小窗已被手动关闭：Cookie 已随关闭落盘到托管目录，改用后台无头模式读取
        self._closed = True
        out = refresh_hosted_site_cookies(self.site, self.paths, proxy=chromium_proxy_arg(self.proxy))
        if out is None:
            res.message = "登录小窗已关闭，且托管目录中没有检测到有效的登录凭据，请重新打开小窗登录。"
            return res
        res.path = out
        res.closed = True
        res.report = inspect_netscape(out, self.site.effective_refresh_url, custom_sites_file=get_custom_sites_file(self.paths))
        return res
