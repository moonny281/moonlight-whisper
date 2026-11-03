"""v1.4.0 专用登录小窗纯逻辑层测试（不依赖 Qt / QtWebEngine）。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.core import webview_login
from app.core.browser_cookies import get_site_cookie_path
from app.core.site_cookies import get_site_by_key
from app.paths import AppPaths


class _FakeDate:
    def __init__(self, secs: int) -> None:
        self._secs = secs

    def toSecsSinceEpoch(self) -> int:  # noqa: N802
        return self._secs


class _FakeBytes:
    """模拟 QByteArray：只提供 data()。"""

    def __init__(self, raw: str) -> None:
        self._raw = raw.encode("utf-8")

    def data(self) -> bytes:
        return self._raw


class _FakeQCookie:
    def __init__(self, domain, name, value, path="/", session=False, exp=None, http_only=True, secure=True) -> None:
        self._d, self._n, self._v, self._p = domain, name, value, path
        self._session, self._exp = session, exp if exp is not None else int(time.time()) + 86400 * 90
        self._h, self._s = http_only, secure

    def domain(self): return self._d
    def path(self): return self._p
    def name(self): return _FakeBytes(self._n)
    def value(self): return _FakeBytes(self._v)
    def isSessionCookie(self): return self._session  # noqa: N802
    def expirationDate(self): return _FakeDate(self._exp)  # noqa: N802
    def isHttpOnly(self): return self._h  # noqa: N802
    def isSecure(self): return self._s  # noqa: N802


def _paths(root: Path) -> AppPaths:
    return AppPaths(app_dir=root)


class LoginWindowCoreTests(unittest.TestCase):
    def test_sanitize_user_agent_strips_qtwebengine_token(self) -> None:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) QtWebEngine/6.7.2 Chrome/118.0.5993.220 Safari/537.36"
        out = webview_login.sanitize_user_agent(ua)
        self.assertNotIn("QtWebEngine", out)
        self.assertIn("Chrome/118.0.5993.220 Safari/537.36", out)
        self.assertEqual(webview_login.sanitize_user_agent(""), "")

    def test_qt_cookie_to_row_handles_qbytearray_and_expiry(self) -> None:
        row = webview_login.qt_cookie_to_row(_FakeQCookie(".youtube.com", "SAPISID", "abc", exp=2000000000))
        self.assertEqual(row["domain"], ".youtube.com")
        self.assertEqual(row["name"], "SAPISID")
        self.assertEqual(row["value"], "abc")
        self.assertEqual(row["expires"], 2000000000.0)
        self.assertTrue(row["httpOnly"] and row["secure"])
        sess = webview_login.qt_cookie_to_row(_FakeQCookie("www.bilibili.com", "buvid3", "x", session=True))
        self.assertEqual(sess["expires"], -1.0)

    def test_collector_add_replace_remove_and_auth_detection(self) -> None:
        site = get_site_by_key("bilibili")
        col = webview_login.CookieCollector()
        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            col.add(_FakeQCookie(".bilibili.com", "buvid3", "anon"))
            self.assertFalse(col.has_auth(site, paths))
            sess = _FakeQCookie(".bilibili.com", "SESSDATA", "v1")
            col.add(sess)
            self.assertTrue(col.has_auth(site, paths))
            col.add(_FakeQCookie(".bilibili.com", "SESSDATA", "v2"))      # 同键覆盖，不应重复
            self.assertEqual(len(col), 2)
            self.assertEqual([r["value"] for r in col.rows() if r["name"] == "SESSDATA"], ["v2"])
            col.remove(sess)
            self.assertEqual(len(col), 1)
            self.assertFalse(col.has_auth(site, paths))

    def test_raw_for_site_only_returns_site_domains(self) -> None:
        site = get_site_by_key("youtube")
        col = webview_login.CookieCollector()
        yt = _FakeQCookie(".youtube.com", "LOGIN_INFO", "1")
        g = _FakeQCookie(".google.com", "SID", "1")
        other = _FakeQCookie(".bilibili.com", "SESSDATA", "1")
        for c in (yt, g, other):
            col.add(c)
        raw = col.raw_for_site(site)
        self.assertEqual({id(x) for x in raw}, {id(yt), id(g)})
        self.assertTrue(webview_login.cookie_matches_site(yt, site))
        self.assertFalse(webview_login.cookie_matches_site(other, site))

    def test_export_writes_only_when_login_is_valid(self) -> None:
        site = get_site_by_key("bilibili")
        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            dest = get_site_cookie_path(site.key, paths)

            anon = [webview_login.qt_cookie_to_row(_FakeQCookie(".bilibili.com", "buvid3", "anon"))]
            out, rep = webview_login.export_site_from_rows(anon, site, paths)
            self.assertIsNone(out)
            self.assertFalse(rep.ok)
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_suffix(".login.tmp").exists())

            good = anon + [webview_login.qt_cookie_to_row(_FakeQCookie(".bilibili.com", "SESSDATA", "tok123"))]
            out, rep = webview_login.export_site_from_rows(good, site, paths)
            self.assertEqual(out, dest)
            self.assertTrue(rep.ok)
            self.assertIn("SESSDATA\ttok123", dest.read_text(encoding="utf-8"))
            self.assertFalse(dest.with_suffix(".login.tmp").exists())

            # 之后一次未完成登录的导出不得覆盖已有有效凭据
            out2, rep2 = webview_login.export_site_from_rows(anon, site, paths)
            self.assertIsNone(out2)
            self.assertIn("SESSDATA\ttok123", dest.read_text(encoding="utf-8"))

            # 空 Cookie 列表
            out3, rep3 = webview_login.export_site_from_rows([], site, paths)
            self.assertIsNone(out3)
            self.assertEqual(rep3.total, 0)

    def test_login_window_dir_is_not_a_vault_browser_profile(self) -> None:
        from app.core import browser_cookies

        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            d = webview_login.get_login_window_dir(paths)
            d.mkdir(parents=True)
            (d / "Default").mkdir()
            self.assertNotEqual(d, browser_cookies.get_vault_profile_dir(paths))
            self.assertFalse(browser_cookies.has_vault_profile(paths))

    def test_parse_proxy_url(self) -> None:
        self.assertIsNone(webview_login.parse_proxy_url(""))
        self.assertIsNone(webview_login.parse_proxy_url("not a proxy"))
        self.assertEqual(
            webview_login.parse_proxy_url("127.0.0.1:7890"),
            {"scheme": "http", "host": "127.0.0.1", "port": 7890, "user": "", "password": ""},
        )
        s5 = webview_login.parse_proxy_url("socks5h://u:p%40w@10.0.0.2:1080")
        self.assertEqual((s5["scheme"], s5["host"], s5["port"], s5["user"], s5["password"]), ("socks5", "10.0.0.2", 1080, "u", "p@w"))


if __name__ == "__main__":
    unittest.main()
