"""Unit tests for built-in `rookie-cookies` (teng-lin/rookie-cookies) integration."""

from __future__ import annotations

import contextlib
import http.cookiejar
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

from app.config import Config
from app.core import browser_cookies, downloader, rookie_bridge
from app.core.cancel import CancelToken
from app.core.downloader import DownloadOptions
from app.core.errors import DownloadError
from app.paths import AppPaths


@contextlib.contextmanager
def _patch_sys_module(name: str, fake_mod: Any) -> Iterator[None]:
    had_prev = name in sys.modules
    prev_mod = sys.modules.get(name)
    sys.modules[name] = fake_mod
    try:
        yield
    finally:
        if had_prev and prev_mod is not None:
            sys.modules[name] = prev_mod
        else:
            sys.modules.pop(name, None)


class TestRookieIntegration(unittest.TestCase):
    def test_no_browser_window_spawn_defaults_and_migration(self) -> None:
        """Regression test for v1.2.9: downloading must NEVER spawn a new Edge/Chrome window by default."""
        # 1. Default Config and DownloadOptions must use app_bound="disabled"
        cfg = Config()
        self.assertEqual(cfg.cookies_app_bound, "disabled")
        dl_opts = DownloadOptions()
        self.assertEqual(dl_opts.cookies_app_bound, "disabled")

        # 2. Existing config.json with old default "injection_only" must auto-migrate to "disabled"
        migrated_cfg = Config.from_dict({"version": 1, "cookies_from_browser": "edge", "cookies_app_bound": "injection_only"})
        self.assertEqual(migrated_cfg.cookies_app_bound, "disabled")

        # 3. refresh_managed_cookies must NOT spawn ManagedBrowser by default
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            with patch("app.core.browser_cookies.ManagedBrowser") as mock_mb:
                res = browser_cookies.refresh_managed_cookies("bilibili", paths)
                self.assertIsNone(res)
                mock_mb.assert_not_called()

    def test_youtube_dual_domain_extraction_and_netscape_output(self) -> None:
        self.assertEqual(
            rookie_bridge.extraction_domains_for("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            ["youtube.com", "google.com"],
        )
        self.assertEqual(
            rookie_bridge.extraction_domains_for("https://www.bilibili.com/video/BV1xx411c7mD"),
            ["bilibili.com"],
        )

        fake_rc = type(sys)("rookie_cookies")
        fake_rc.version = lambda: "0.6.0"  # type: ignore[attr-defined]
        extract_calls = []

        def _fake_extract(**kwargs):
            extract_calls.append(kwargs)
            return [
                {
                    "domain": ".youtube.com",
                    "path": "/",
                    "secure": True,
                    "expires": 0,  # session cookie should get positive future timestamp
                    "name": "SAPISID",
                    "value": "sapi_123",
                    "http_only": False,
                },
                {
                    "domain": ".youtube.com",
                    "path": "/",
                    "secure": True,
                    "expires": 1999999999,
                    "name": "__Secure-3PSID",
                    "value": "psid_456",
                    "http_only": True,
                },
            ]

        fake_rc.extract = _fake_extract  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as tmp, _patch_sys_module("rookie_cookies", fake_rc):
            self.assertTrue(rookie_bridge.is_available())
            self.assertEqual(rookie_bridge.get_version(), "0.6.0")

            logs = []
            cookie_file = rookie_bridge.extract_to_netscape_file(
                browser="edge",
                profile="Profile 1",
                target_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                temp_dir=Path(tmp),
                token=CancelToken(),
                on_log=logs.append,
            )
            self.assertIsNotNone(cookie_file)
            self.assertTrue(cookie_file.exists())
            self.assertEqual(len(extract_calls), 1)
            self.assertEqual(extract_calls[0]["browser"], "edge")
            self.assertEqual(extract_calls[0]["profile"], "Profile 1")
            self.assertEqual(extract_calls[0]["domains"], ["youtube.com", "google.com"])
            self.assertEqual(extract_calls[0]["app_bound"], "disabled")

            # Verify MozillaCookieJar loads the file cleanly
            jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
            jar.load(ignore_discard=True, ignore_expires=True)
            self.assertEqual(len(jar), 2)
            self.assertTrue(any("已包含登录凭据" in l for l in logs))

    def test_downloader_uses_rookie_bridge_instead_of_ydl_cookiesfrombrowser(self) -> None:
        fake_rc = type(sys)("rookie_cookies")
        fake_rc.version = lambda: "0.6.0"  # type: ignore[attr-defined]
        fake_rc.extract = lambda **kw: [  # type: ignore[attr-defined]
            {
                "domain": ".bilibili.com",
                "path": "/",
                "secure": True,
                "expires": 1999999999,
                "name": "SESSDATA",
                "value": "bili_sess_ok",
                "http_only": True,
            }
        ]

        with tempfile.TemporaryDirectory() as tmp, _patch_sys_module("rookie_cookies", fake_rc):
            paths = AppPaths(app_dir=Path(tmp))
            opts = DownloadOptions(
                cookies_from_browser="chrome",
                cookies_browser_profile="Default",
            )
            logs = []
            ydl_opts = downloader._build_common_ydl_opts(
                opts,
                on_log=logs.append,
                paths=paths,
                target_url="https://www.bilibili.com/video/BV1xx411c7mD",
                token=CancelToken(),
            )
            self.assertIn("cookiefile", ydl_opts)
            self.assertNotIn("cookiesfrombrowser", ydl_opts)
            self.assertTrue(Path(ydl_opts["cookiefile"]).exists())
            self.assertTrue(any("SESSDATA" in l for l in logs))

    def test_disabled_policy_does_not_spawn_second_pass_and_translates_errors(self) -> None:
        fake_rc = type(sys)("rookie_cookies")
        calls = []

        def _empty_extract(**kw):
            calls.append(kw.get("app_bound"))
            return []

        fake_rc.extract = _empty_extract  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as tmp, _patch_sys_module("rookie_cookies", fake_rc):
            out = rookie_bridge.extract_to_netscape_file(
                browser="edge",
                target_url="https://www.bilibili.com/video/BV1",
                temp_dir=Path(tmp),
                token=CancelToken(),
            )
            self.assertIsNone(out)
            # Must only call once with "disabled", NEVER spawning "injection_only" or "allow_elevated_fallback"
            self.assertEqual(calls, ["disabled"])

        class FakeRookieEngineError(RuntimeError):
            kind = "engine"
            code = "app_bound_decrypt_failed"
            stop_reason = None

        def _failing_extract(**kw):
            raise FakeRookieEngineError("IElevator injection blocked by endpoint security")

        fake_rc.extract = _failing_extract  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as tmp, _patch_sys_module("rookie_cookies", fake_rc):
            with self.assertRaises(DownloadError) as ctx:
                rookie_bridge.extract_to_netscape_file(
                    browser="edge",
                    target_url="https://www.bilibili.com/video/BV1",
                    temp_dir=Path(tmp),
                    token=CancelToken(),
                )
            self.assertIn("App-Bound (v20)", ctx.exception.user_message)

    def test_edge_locked_db_with_cancel_token_falls_back_cleanly_for_tiktok(self) -> None:
        """Regression test for v1.2.8+: when `token=CancelToken()` is passed and Edge DB is share-locked (OS error 32),
        it must NOT raise `TypeError: 'bool' object is not callable`, and TikTok download must still succeed."""
        fake_rc = type(sys)("rookie_cookies")

        class FakeCancellationHandle:
            def cancel(self):
                pass

        class FakeShareLockedError(RuntimeError):
            kind = "engine"
            code = "source_inspection_failed"
            stop_reason = None

        def _locked_extract(**kw):
            raise FakeShareLockedError("Windows browser database is share-locked at Default/Network/Cookies (OS error 32)")

        fake_rc.CancellationHandle = FakeCancellationHandle  # type: ignore[attr-defined]
        fake_rc.extract = _locked_extract  # type: ignore[attr-defined]

        class BrokenYoutubeDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download=False):
                raise RuntimeError("ERROR: [TikTok] 7696957163670998294: Unexpected response from webpage request")

        fake_ytdlp = type(sys)("yt_dlp")
        fake_ytdlp.YoutubeDL = BrokenYoutubeDL  # type: ignore[attr-defined]

        fake_tikwm_resp = {
            "code": 0,
            "data": {
                "id": "7696957163670998294",
                "title": "TikTok Edge 锁库回退测试",
                "duration": 15.0,
                "play": "https://cdn.example.com/7696957163670998294.mp4",
                "author": {"nickname": "tester"},
            },
        }

        with tempfile.TemporaryDirectory() as tmp, _patch_sys_module("rookie_cookies", fake_rc), _patch_sys_module(
            "yt_dlp", fake_ytdlp
        ), patch("app.core.http_client.fetch_json", return_value=fake_tikwm_resp):
            downloader._DIRECT_MEDIA_CACHE.clear()
            paths = AppPaths(app_dir=Path(tmp))
            dl_opts = DownloadOptions(cookies_from_browser="edge")
            logs: list[str] = []
            info = downloader.fetch_info(
                "https://vt.tiktok.com/ZSgwwQnPH/",
                dl_opts,
                token=CancelToken(),
                on_log=logs.append,
                paths=paths,
            )
            self.assertEqual(info.id, "7696957163670998294")
            self.assertEqual(info.title, "TikTok Edge 锁库回退测试")
            self.assertTrue(any("OS error 32" in l for l in logs))

    def test_profile_discovery_parsing(self) -> None:
        fake_rc = type(sys)("rookie_cookies")
        fake_rc.profiles = lambda browser_id, timeout=5: [  # type: ignore[attr-defined]
            {"profile": {"profile_id": "Default", "display_name": "Person 1", "is_default": True}},
            {"profile": {"profile_id": "Profile 1", "display_name": "Work Account", "is_default": False}},
        ]
        with _patch_sys_module("rookie_cookies", fake_rc):
            profs = rookie_bridge.list_browser_profiles("chrome")
            self.assertEqual(len(profs), 2)
            self.assertEqual(profs[0].profile_id, "Default")
            self.assertEqual(profs[0].display_name, "Person 1")
            self.assertTrue(profs[0].is_default)
            self.assertEqual(profs[1].profile_id, "Profile 1")


if __name__ == "__main__":
    unittest.main()
