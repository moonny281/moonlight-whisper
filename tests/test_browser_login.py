"""v1.4.2 系统浏览器（Edge/Chrome）安全登录小窗会话测试（全部使用假浏览器，不启动真实进程）。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core import browser_cookies, browser_login
from app.core.browser_cookies import get_site_cookie_path
from app.core.site_cookies import get_site_by_key
from app.paths import AppPaths


def _row(domain: str, name: str, value: str) -> dict:
    return {"domain": domain, "path": "/", "name": name, "value": value,
            "expires": time.time() + 86400 * 90, "httpOnly": True, "secure": True}


class _FakeBrowser:
    def __init__(self, rows, urls, alive=True) -> None:
        self.rows, self.urls, self._alive, self.closed = rows, urls, alive, False

    def alive(self): return self._alive and not self.closed
    def get_cookies(self, quick=False): return list(self.rows)
    def page_urls(self): return list(self.urls)
    def close(self): self.closed = True


def _session(site_key: str, paths: AppPaths, fake: _FakeBrowser) -> browser_login.BrowserLoginSession:
    s = browser_login.BrowserLoginSession(get_site_by_key(site_key), paths)
    s.browser = fake  # type: ignore[assignment]
    return s


class BrowserLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        # 不真实等待：缩短凭据等待时间，并让 sleep 立即返回
        for p in (patch.object(browser_login, "_AUTH_WAIT_SEC", 0.0), patch.object(browser_login.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def test_chromium_proxy_arg(self) -> None:
        self.assertEqual(browser_login.chromium_proxy_arg(""), "")
        self.assertEqual(browser_login.chromium_proxy_arg("127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(browser_login.chromium_proxy_arg("socks5h://u:p@10.0.0.2:1080"), "socks5://10.0.0.2:1080")

    def test_pick_login_browser_prefers_requested_then_first(self) -> None:
        avail = [("edge", Path("e.exe")), ("chrome", Path("c.exe"))]
        with patch.object(browser_login, "available_login_browsers", return_value=avail):
            self.assertEqual(browser_login.pick_login_browser("chrome")[0], "chrome")
            self.assertEqual(browser_login.pick_login_browser("firefox")[0], "edge")
        with patch.object(browser_login, "available_login_browsers", return_value=[]):
            self.assertIsNone(browser_login.pick_login_browser("edge"))

    def test_start_uses_isolated_vault_profile_and_app_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = AppPaths(app_dir=Path(td))
            s = browser_login.BrowserLoginSession(get_site_by_key("youtube"), paths, proxy="127.0.0.1:7890", window_pos=(900, 40))
            with patch.object(browser_login, "pick_login_browser", return_value=("edge", Path("msedge.exe"))), \
                 patch.object(browser_login, "ManagedBrowser") as mb:
                s.start()
            kwargs = mb.call_args.kwargs
            self.assertEqual(mb.call_args.args[1], browser_cookies.get_vault_profile_dir(paths))
            self.assertFalse(kwargs["headless"])
            self.assertTrue(kwargs["app_window"])
            self.assertEqual(kwargs["proxy"], "http://127.0.0.1:7890")
            self.assertEqual(kwargs["window_pos"], (900, 40))
            mb.return_value.start.assert_called_once()
            self.assertEqual(mb.return_value.start.call_args.args[0], "https://www.youtube.com/")

    def test_start_without_browser_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            s = browser_login.BrowserLoginSession(get_site_by_key("youtube"), AppPaths(app_dir=Path(td)))
            with patch.object(browser_login, "pick_login_browser", return_value=None):
                with self.assertRaises(browser_cookies.BrowserCookieError):
                    s.start()

    def test_managed_browser_app_window_args_disable_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mb = browser_cookies.ManagedBrowser(
                Path("msedge.exe"), Path(td) / "prof", headless=False, proxy="http://127.0.0.1:7890",
                app_window=True, window_size=(480, 780), window_pos=(10, 20),
            )
            popen = MagicMock()
            popen.poll.return_value = None
            sess = MagicMock()
            sess.get.return_value.ok = True
            with patch.object(browser_cookies, "_kill_stale_profile_owner"), \
                 patch.object(browser_cookies.subprocess, "Popen", return_value=popen) as pp, \
                 patch.object(browser_cookies, "_local_session", return_value=sess):
                mb.start("https://www.youtube.com/")
            args = pp.call_args.args[0]
            self.assertIn("--app=https://www.youtube.com/", args)
            self.assertIn("--window-size=480,780", args)
            self.assertIn("--window-position=10,20", args)
            self.assertIn("--disable-sync", args)
            self.assertIn("--no-first-run", args)
            self.assertIn("--proxy-server=http://127.0.0.1:7890", args)
            self.assertIn(f"--user-data-dir={Path(td) / 'prof'}", args)
            self.assertNotIn("https://www.youtube.com/", args)       # 不能再作为位置参数，否则会多开一个标签页

    def test_poll_reports_auth_and_on_site(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = AppPaths(app_dir=Path(td))
            fake = _FakeBrowser([_row(".bilibili.com", "SESSDATA", "t")], ["https://www.bilibili.com/"])
            st = _session("bilibili", paths, fake).poll()
            self.assertTrue(st.alive and st.has_auth and st.on_site)
            fake2 = _FakeBrowser([_row(".bilibili.com", "buvid3", "a")], ["https://passport.bilibili.com/login"])
            st2 = _session("bilibili", paths, fake2).poll()
            self.assertTrue(st2.alive)
            self.assertFalse(st2.has_auth)
            st3 = _session("bilibili", paths, _FakeBrowser([], [], alive=False)).poll()
            self.assertFalse(st3.alive)

    def test_finish_exports_primary_and_other_logged_in_sites_then_closes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = AppPaths(app_dir=Path(td))
            fake = _FakeBrowser(
                [_row(".bilibili.com", "SESSDATA", "tok"), _row(".douyin.com", "sessionid", "dy"), _row(".x.com", "guest_id", "g")],
                [],
            )
            res = _session("bilibili", paths, fake).finish()
            self.assertEqual(res.path, get_site_cookie_path("bilibili", paths))
            self.assertTrue(res.closed and fake.closed)
            self.assertEqual(res.also_hosted, ["抖音 (douyin.com)"])
            self.assertTrue(get_site_cookie_path("douyin", paths).is_file())
            self.assertFalse(get_site_cookie_path("x", paths).exists())

    def test_finish_without_login_keeps_window_open_and_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = AppPaths(app_dir=Path(td))
            dest = get_site_cookie_path("bilibili", paths)
            dest.parent.mkdir(parents=True)
            dest.write_text("# keep me\n", encoding="utf-8")
            fake = _FakeBrowser([_row(".bilibili.com", "buvid3", "anon")], [])
            res = _session("bilibili", paths, fake).finish()
            self.assertIsNone(res.path)
            self.assertFalse(res.closed)
            self.assertFalse(fake.closed)
            self.assertEqual(dest.read_text(encoding="utf-8"), "# keep me\n")
            self.assertTrue(res.message)

    def test_finish_after_user_closed_window_falls_back_to_headless_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = AppPaths(app_dir=Path(td))
            fake = _FakeBrowser([], [], alive=False)
            out = get_site_cookie_path("bilibili", paths)
            out.parent.mkdir(parents=True)
            out.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t%d\tSESSDATA\tz\n" % (time.time() + 86400), encoding="utf-8")
            with patch.object(browser_login, "refresh_hosted_site_cookies", return_value=out) as rf:
                res = _session("bilibili", paths, fake).finish()
            rf.assert_called_once()
            self.assertEqual(res.path, out)
            self.assertTrue(res.report.ok)
            with patch.object(browser_login, "refresh_hosted_site_cookies", return_value=None):
                res2 = _session("bilibili", paths, _FakeBrowser([], [], alive=False)).finish()
            self.assertIsNone(res2.path)
            self.assertTrue(res2.message)

    def test_alive_uses_debug_port_when_launcher_process_exited(self) -> None:
        mb = browser_cookies.ManagedBrowser(Path("msedge.exe"), Path("prof"), headless=False)
        proc = MagicMock()
        proc.poll.return_value = 0                      # 启动进程已退出（浏览器已转交给其他进程继续运行）
        mb._proc = proc
        with patch.object(mb, "cdp_reachable", return_value=True):
            self.assertTrue(mb.alive())
        with patch.object(mb, "cdp_reachable", return_value=False):
            self.assertFalse(mb.alive())
        proc.poll.return_value = None
        with patch.object(mb, "cdp_reachable", return_value=False):
            self.assertTrue(mb.alive())

    def test_finish_failure_never_closes_window_and_reports_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = AppPaths(app_dir=Path(td))
            fake = _FakeBrowser([_row(".bilibili.com", "buvid3", "anon")], [])
            with patch.object(browser_login, "_AUTH_WAIT_SEC", 0.0), patch.object(browser_login.time, "sleep"):
                res = _session("bilibili", paths, fake).finish()
            self.assertIsNone(res.path)
            self.assertFalse(fake.closed)
            self.assertIn("共读到 1 条 Cookie", res.message)
            self.assertIn("bilibili.com", res.message)

    def test_finish_waits_for_login_cookie_to_appear(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = AppPaths(app_dir=Path(td))
            fake = _FakeBrowser([_row(".bilibili.com", "buvid3", "anon")], [])
            calls = {"n": 0}
            orig = fake.get_cookies

            def late(quick=False):
                calls["n"] += 1
                if calls["n"] == 3:
                    fake.rows.append(_row(".bilibili.com", "SESSDATA", "late"))
                return orig(quick)

            fake.get_cookies = late  # type: ignore[assignment]
            with patch.object(browser_login, "_AUTH_WAIT_SEC", 60.0):
                res = _session("bilibili", paths, fake).finish()
            self.assertEqual(res.path, get_site_cookie_path("bilibili", paths))
            self.assertTrue(fake.closed)

    def test_finish_rechecks_before_treating_window_as_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = AppPaths(app_dir=Path(td))
            fake = _FakeBrowser([_row(".bilibili.com", "SESSDATA", "t")], [])
            states = iter([False, False, True, True, True, True, True])
            fake.alive = lambda: next(states, True)       # type: ignore[assignment]
            with patch.object(browser_login.time, "sleep"), \
                 patch.object(browser_login, "refresh_hosted_site_cookies") as rf:
                res = _session("bilibili", paths, fake).finish()
            rf.assert_not_called()                          # 复核后发现窗口仍开着，不得走会清理进程的无头回退
            self.assertEqual(res.path, get_site_cookie_path("bilibili", paths))

    def test_close_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake = _FakeBrowser([], [])
            s = _session("bilibili", AppPaths(app_dir=Path(td)), fake)
            s.close()
            s.close()
            self.assertTrue(fake.closed)
            self.assertFalse(s.alive())


if __name__ == "__main__":
    unittest.main()
