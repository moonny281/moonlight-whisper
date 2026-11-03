"""专用登录小窗的纯 Python 逻辑层（零 Qt 依赖，便于单元测试）。

设计目标（v1.4.0）：
1. 登录发生在应用自带的一个独立小窗（QtWebEngine）里，而不是用户日常浏览器的某个标签页：
   - 不占用、不打扰用户正在使用的浏览器窗口与标签页；
   - 不是浏览器 `--user-data-dir` 配置，因此不会触发 Microsoft Edge / Chrome 的账号同步，也不会弹出一堆窗口和标签页；
   - Cookie 由 `QWebEngineCookieStore` 以明文事件直接回传，不经过磁盘上的 SQLite，也就不存在 Windows App-Bound (v20) 解密、
     数据库被占用或 Cookie 尚未落盘等「拿不到登录凭证」的问题。
2. 本模块负责：Qt Cookie 对象 -> 通用 Cookie 字典、Cookie 收集器、User-Agent 清洗、按站点导出 Netscape 文件（先校验后替换，
   不会用一份无效凭据覆盖已有的有效托管凭据）。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.browser_cookies import (
    _write_netscape_atomic,
    cdp_cookies_to_netscape,
    get_custom_sites_file,
    get_site_cookie_path,
)
from app.core.site_cookies import (
    CookieReport,
    SiteProfile,
    export_domains_for,
    inspect_netscape,
    rows_have_site_auth,
)
from app.paths import PATHS, AppPaths

_QTWEBENGINE_TOKEN = re.compile(r"\s*QtWebEngine/[0-9.]+", re.IGNORECASE)
_HEADLESS_TOKEN = re.compile(r"Headless(?=Chrome)", re.IGNORECASE)


def get_login_window_dir(paths: AppPaths = PATHS) -> Path:
    """登录小窗自己的数据目录（QtWebEngine 存储，不是浏览器配置目录，也不会被 `has_vault_profile` 识别为托管浏览器库）。"""
    return paths.data_root / "account_vault" / "login_window"


def sanitize_user_agent(ua: str) -> str:
    """去掉 UA 中的 `QtWebEngine/x.y.z` 标记，让站点（尤其是 Google 登录页）把小窗当作普通 Chromium 浏览器。"""
    cleaned = _QTWEBENGINE_TOKEN.sub("", ua or "")
    cleaned = _HEADLESS_TOKEN.sub("", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _to_text(value: Any) -> str:
    """QByteArray / bytes / str -> str。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    data = value
    if not isinstance(value, (bytes, bytearray)):
        getter = getattr(value, "data", None)
        if callable(getter):
            try:
                data = getter()
            except Exception:  # noqa: BLE001
                data = value
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", errors="replace")
    try:
        return bytes(data).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return str(data)


def qt_cookie_to_row(qc: Any) -> Dict[str, Any]:
    """`QNetworkCookie`（或同接口对象）-> 与 CDP 导出一致的 Cookie 字典，可直接交给 `cdp_cookies_to_netscape`。"""
    expires = -1.0                                   # 会话 Cookie
    try:
        if not qc.isSessionCookie():
            exp = qc.expirationDate()
            if hasattr(exp, "toSecsSinceEpoch"):
                expires = float(exp.toSecsSinceEpoch())
            else:
                expires = float(exp)
    except Exception:  # noqa: BLE001
        expires = -1.0
    return {
        "domain": _to_text(qc.domain()),
        "path": _to_text(qc.path()) or "/",
        "name": _to_text(qc.name()),
        "value": _to_text(qc.value()),
        "expires": expires,
        "httpOnly": bool(qc.isHttpOnly()),
        "secure": bool(qc.isSecure()),
    }


def _row_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    return (str(row.get("domain") or ""), str(row.get("path") or "/"), str(row.get("name") or ""))


def _domain_matches(domain: str, allowed: Iterable[str]) -> bool:
    bare = (domain or "").lstrip(".").lower()
    for a in allowed:
        a_bare = a.lstrip(".").lower()
        if bare == a_bare or bare.endswith("." + a_bare):
            return True
    return False


class CookieCollector:
    """登录小窗 Cookie 收集器：接收 `cookieAdded` / `cookieRemoved` 事件，维护当前全部 Cookie 快照。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._raw: Dict[Tuple[str, str, str], Any] = {}

    def add(self, qc: Any) -> None:
        row = qt_cookie_to_row(qc)
        if not row["name"] or not row["domain"]:
            return
        key = _row_key(row)
        with self._lock:
            self._rows[key] = row
            self._raw[key] = qc

    def remove(self, qc: Any) -> None:
        key = _row_key(qt_cookie_to_row(qc))
        with self._lock:
            self._rows.pop(key, None)
            self._raw.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()
            self._raw.clear()

    def rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._rows.values()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def has_auth(self, site: SiteProfile, paths: AppPaths = PATHS) -> bool:
        """当前收集到的 Cookie 中是否已包含该站点的关键登录凭据。"""
        return rows_have_site_auth(
            self.rows(),
            site.effective_refresh_url,
            custom_sites_file=get_custom_sites_file(paths),
        )

    def raw_for_site(self, site: SiteProfile) -> List[Any]:
        """返回属于该站点导出域名的原始 Cookie 对象（用于从小窗存储中删除）。"""
        allowed = export_domains_for(site)
        with self._lock:
            return [self._raw[k] for k, r in self._rows.items() if _domain_matches(str(r.get("domain") or ""), allowed)]


def cookie_matches_site(qc: Any, site: SiteProfile) -> bool:
    return _domain_matches(_to_text(qc.domain()), export_domains_for(site))


def export_site_from_rows(
    rows: Iterable[Dict[str, Any]],
    site: SiteProfile,
    paths: AppPaths = PATHS,
) -> Tuple[Optional[Path], CookieReport]:
    """把小窗里的 Cookie 导出为 `data_root/cookies/<site>.txt`。

    先写临时文件并体检，只有体检通过（含关键登录 Cookie）才原子替换正式托管文件，
    避免一次未完成的登录把原本有效的托管凭据覆盖掉。返回 (正式文件路径或 None, 体检报告)。
    """
    custom_file = get_custom_sites_file(paths)
    text, count = cdp_cookies_to_netscape(list(rows), export_domains_for(site))
    dest = get_site_cookie_path(site.key, paths)
    if count <= 0:
        return None, CookieReport()

    tmp = dest.with_suffix(".login.tmp")
    _write_netscape_atomic(tmp, text)
    try:
        report = inspect_netscape(tmp, site.effective_refresh_url, custom_sites_file=custom_file)
        if not report.ok:
            return None, report
        _write_netscape_atomic(dest, text)
        return dest, inspect_netscape(dest, site.effective_refresh_url, custom_sites_file=custom_file)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def parse_proxy_url(proxy: str) -> Optional[Dict[str, Any]]:
    """把设置里的代理字符串解析成 {scheme, host, port, user, password}；空串或无法解析返回 None。"""
    import urllib.parse

    raw = (proxy or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        u = urllib.parse.urlparse(raw)
        host, port = u.hostname, u.port
    except ValueError:
        return None
    if not host or not port:
        return None
    scheme = (u.scheme or "http").lower()
    return {
        "scheme": "socks5" if scheme.startswith("socks") else "http",
        "host": host,
        "port": int(port),
        "user": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password or ""),
    }
