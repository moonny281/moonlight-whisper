"""Unit tests for the hybrid Cookie strategy:
- YouTube exclusively uses Account Vault (`data_root/cookies/youtube.txt` + silent headless auto-refresh via `account_vault/browser_profile`), never calling daily browser extraction;
- Bilibili & TikTok prioritize reading cookies from the daily browser (`rookie_bridge.extract_to_netscape_file`), falling back to Account Vault if unavailable.
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

from app.config import Config
from app.core import browser_cookies, downloader, rookie_bridge
from app.core.cancel import CancelToken
from app.core.downloader import DownloadOptions
from app.core.site_cookies import (
    export_domains_for,
    inspect_netscape,
    load_all_sites,
    resolve_site,
    rows_have_site_auth,
)
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


class TestHybridAccountVaultAndBrowserRouting(unittest.TestCase):
    def test_youtube_exclusively_uses_account_vault_and_never_reads_daily_browser(self) -> None:
        """Even when cookies_from_browser='edge' is set, YouTube URLs MUST use Account Vault and never call rookie_bridge."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            far_exp = int(time.time()) + 86400 * 30
            yt_hosted = browser_cookies.get_site_cookie_path("youtube", paths)
            yt_hosted.parent.mkdir(parents=True, exist_ok=True)
            yt_hosted.write_text(
                f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t{far_exp}\t__Secure-3PSID\tvault_yt_token\n",
                encoding="utf-8",
            )

            opts = DownloadOptions(cookies_from_browser="edge", auto_refresh_cookies=True)
            with patch("app.core.rookie_bridge.extract_to_netscape_file") as mock_rookie:
                ydl_opts = downloader._build_common_ydl_opts(
                    opts,
                    paths=paths,
                    target_url="https://www.youtube.com/watch?v=fyuaW6Uf-S8",
                    token=CancelToken(),
                )
                mock_rookie.assert_not_called()
                self.assertIn("cookiefile", ydl_opts)
                self.assertIn("vault_yt_token", Path(ydl_opts["cookiefile"]).read_text(encoding="utf-8"))

    def test_bilibili_and_tiktok_prioritize_daily_browser_cookie_reading(self) -> None:
        """For Bilibili and TikTok, cookies_from_browser is prioritized over local hosted cookies."""
        fake_rc = type(sys)("rookie_cookies")
        fake_rc.version = lambda: "0.6.0"  # type: ignore[attr-defined]

        def _fake_extract(**kw):
            doms = kw.get("domains") or []
            if "bilibili.com" in doms:
                return [
                    {
                        "domain": ".bilibili.com",
                        "path": "/",
                        "secure": True,
                        "expires": 1999999999,
                        "name": "SESSDATA",
                        "value": "bili_from_daily_browser",
                        "http_only": True,
                    }
                ]
            if "tiktok.com" in doms:
                return [
                    {
                        "domain": ".tiktok.com",
                        "path": "/",
                        "secure": True,
                        "expires": 1999999999,
                        "name": "sessionid",
                        "value": "tiktok_from_daily_browser",
                        "http_only": True,
                    }
                ]
            return []

        fake_rc.extract = _fake_extract  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as tmp, _patch_sys_module("rookie_cookies", fake_rc):
            paths = AppPaths(app_dir=Path(tmp))
            far_exp = int(time.time()) + 86400 * 30
            # Even if an older hosted bilibili.txt exists, daily browser reading takes priority
            bili_hosted = browser_cookies.get_site_cookie_path("bilibili", paths)
            bili_hosted.parent.mkdir(parents=True, exist_ok=True)
            bili_hosted.write_text(
                f"# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t{far_exp}\tSESSDATA\told_vault_bili\n",
                encoding="utf-8",
            )

            bili_opts = DownloadOptions(cookies_from_browser="edge")
            bili_ydl = downloader._build_common_ydl_opts(
                bili_opts,
                paths=paths,
                target_url="https://www.bilibili.com/video/BV1xx411c7mD",
                token=CancelToken(),
            )
            self.assertIn("cookiefile", bili_ydl)
            self.assertIn("bili_from_daily_browser", Path(bili_ydl["cookiefile"]).read_text(encoding="utf-8"))

            tk_opts = DownloadOptions(cookies_from_browser="edge")
            tk_ydl = downloader._build_common_ydl_opts(
                tk_opts,
                paths=paths,
                target_url="https://www.tiktok.com/@user/video/7696957163670998294",
                token=CancelToken(),
            )
            self.assertIn("cookiefile", tk_ydl)
            self.assertIn("tiktok_from_daily_browser", Path(tk_ydl["cookiefile"]).read_text(encoding="utf-8"))

    def test_youtube_cookie_file_does_not_block_bilibili_browser_reading(self) -> None:
        """If cookies_file happens to point to youtube.txt, downloading Bilibili still reads from the daily browser."""
        fake_rc = type(sys)("rookie_cookies")
        fake_rc.version = lambda: "0.6.0"  # type: ignore[attr-defined]
        fake_rc.extract = lambda **kw: [  # type: ignore[attr-defined]
            {
                "domain": ".bilibili.com",
                "path": "/",
                "secure": True,
                "expires": 1999999999,
                "name": "SESSDATA",
                "value": "bili_browser_ok",
                "http_only": True,
            }
        ]

        with tempfile.TemporaryDirectory() as tmp, _patch_sys_module("rookie_cookies", fake_rc):
            paths = AppPaths(app_dir=Path(tmp))
            yt_file = Path(tmp) / "youtube_only.txt"
            yt_file.write_text(
                "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\tyt_sapi\n",
                encoding="utf-8",
            )
            opts = DownloadOptions(cookies_file=str(yt_file), cookies_from_browser="edge")
            ydl_opts = downloader._build_common_ydl_opts(
                opts,
                paths=paths,
                target_url="https://www.bilibili.com/video/BV1xx411c7mD",
                token=CancelToken(),
            )
            self.assertIn("cookiefile", ydl_opts)
            self.assertIn("bili_browser_ok", Path(ydl_opts["cookiefile"]).read_text(encoding="utf-8"))

    def test_clean_profile_session_wipes_tabs_but_keeps_cookies(self) -> None:
        """Verify _prepare_clean_profile_session removes SingletonLock and Sessions tabs while keeping Cookies."""
        with tempfile.TemporaryDirectory() as tmp:
            vault_dir = Path(tmp) / "account_vault" / "browser_profile"
            default_dir = vault_dir / "Default"
            sessions_dir = default_dir / "Sessions"
            network_dir = default_dir / "Network"
            sessions_dir.mkdir(parents=True)
            network_dir.mkdir(parents=True)

            (vault_dir / "SingletonLock").write_text("locked", encoding="utf-8")
            (sessions_dir / "Tabs_133500").write_bytes(b"OLD_TABS")
            (default_dir / "Current Tabs").write_bytes(b"CURRENT_TABS")
            (network_dir / "Cookies").write_bytes(b"SQLITE_COOKIES_INTACT")
            prefs_path = default_dir / "Preferences"
            prefs_path.write_text(json.dumps({"profile": {"exit_type": "Crashed", "exited_cleanly": False}}), encoding="utf-8")

            browser_cookies._prepare_clean_profile_session(vault_dir)

            self.assertFalse((vault_dir / "SingletonLock").exists())
            self.assertFalse(sessions_dir.exists())
            self.assertFalse((default_dir / "Current Tabs").exists())
            self.assertTrue((network_dir / "Cookies").is_file())
            prefs_after = json.loads(prefs_path.read_text(encoding="utf-8"))
            self.assertEqual(prefs_after["profile"]["exit_type"], "Normal")
            self.assertTrue(prefs_after["profile"]["exited_cleanly"])

    def test_custom_sites_json_plugin_extensibility(self) -> None:
        """Verify plugins/sites.json dynamically registers new sites for hosting and URL matching."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            paths.ensure_runtime_dirs()
            sites_json = browser_cookies.get_custom_sites_file(paths)
            sites_json.write_text(
                json.dumps(
                    [
                        {
                            "key": "vimeo",
                            "label": "Vimeo (vimeo.com)",
                            "hosts": ["vimeo.com"],
                            "cookie_domains": [".vimeo.com"],
                            "key_cookies": ["vuid", "vimeo"],
                            "login_url": "https://vimeo.com/log_in",
                            "refresh_url": "https://vimeo.com/",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            all_sites = load_all_sites(sites_json)
            keys = [s.key for s in all_sites]
            self.assertIn("youtube", keys)
            self.assertIn("vimeo", keys)

            matched = resolve_site("https://vimeo.com/123456789", custom_sites_file=sites_json)
            self.assertIsNotNone(matched)
            self.assertEqual(matched.key, "vimeo")
            self.assertEqual(matched.display_label, "Vimeo (vimeo.com)")

    def test_export_logged_in_sites_from_browser_multi_site_discovery(self) -> None:
        """Verify logging into multiple sites in the hosted browser exports all authenticated sites at once."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            far = int(time.time()) + 86400 * 30

            class DummyBrowser:
                def get_cookies(self):
                    return [
                        {"domain": ".youtube.com", "path": "/", "secure": True, "httpOnly": True, "expires": far, "name": "__Secure-3PSID", "value": "yt_psid_ok"},
                        {"domain": ".google.com", "path": "/", "secure": True, "httpOnly": True, "expires": far, "name": "SID", "value": "google_sid_ok"},
                        {"domain": ".bilibili.com", "path": "/", "secure": True, "httpOnly": True, "expires": far, "name": "SESSDATA", "value": "bili_sess_ok"},
                    ]

            reports = browser_cookies.export_logged_in_sites_from_browser(
                DummyBrowser(),  # type: ignore[arg-type]
                paths=paths,
                primary_site_key="youtube",
            )
            self.assertIn("youtube", reports)
            self.assertIn("bilibili", reports)
            self.assertTrue(reports["youtube"].ok)
            self.assertTrue(reports["bilibili"].ok)
            self.assertTrue(browser_cookies.get_site_cookie_path("youtube", paths).is_file())
            self.assertTrue(browser_cookies.get_site_cookie_path("bilibili", paths).is_file())

    def test_youtube_auto_refresh_expired_cookies_and_auth_challenge_retry(self) -> None:
        """Verify expired YouTube cookies or yt-dlp bot/auth challenges automatically trigger headless refresh from Account Vault."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            vault_dir = browser_cookies.get_vault_profile_dir(paths)
            (vault_dir / "Default").mkdir(parents=True)
            (vault_dir / "Local State").write_text("{}", encoding="utf-8")

            # Write an EXPIRED youtube.txt cookie file
            yt_cookie_path = browser_cookies.get_site_cookie_path("youtube", paths)
            yt_cookie_path.parent.mkdir(parents=True, exist_ok=True)
            past_exp = int(time.time()) - 3600
            yt_cookie_path.write_text(
                f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t{past_exp}\t__Secure-3PSID\texpired_val\n",
                encoding="utf-8",
            )
            self.assertFalse(inspect_netscape(yt_cookie_path, "https://www.youtube.com/watch?v=dQw4w9WgXcQ").ok)

            far_exp = int(time.time()) + 86400 * 30

            def _fake_headless_refresh(site_or_url, paths=paths, proxy="", timeout=18.0, settle_sec=2.5, on_log=None):
                yt_cookie_path.write_text(
                    f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t{far_exp}\t__Secure-3PSID\tfresh_headless_psid\n",
                    encoding="utf-8",
                )
                if on_log:
                    on_log("[账号托管] 已成功静默提取并刷新 YouTube 的最新凭据")
                return yt_cookie_path

            opts = DownloadOptions(auto_refresh_cookies=True)
            logs: list[str] = []
            with patch("app.core.browser_cookies.refresh_hosted_site_cookies", side_effect=_fake_headless_refresh) as mock_refresh:
                resolved = downloader._resolve_hosted_cookie_for_site(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    opts,
                    paths=paths,
                    on_log=logs.append,
                )
                self.assertEqual(resolved, yt_cookie_path)
                self.assertEqual(mock_refresh.call_count, 1)
                self.assertIn("fresh_headless_psid", yt_cookie_path.read_text(encoding="utf-8"))

            # Now test reactive headless refresh when yt-dlp throws "Sign in to confirm you're not a bot"
            call_count = {"n": 0}

            class BotChallengeThenSuccessYDL:
                def __init__(self, ydl_opts):
                    self.ydl_opts = ydl_opts

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def extract_info(self, url, download=False):
                    call_count["n"] += 1
                    cf = self.ydl_opts.get("cookiefile")
                    text = Path(cf).read_text(encoding="utf-8") if cf else ""
                    if "reactive_refreshed_token" not in text:
                        raise RuntimeError("ERROR: [youtube] fyuaW6Uf-S8: Sign in to confirm you’re not a bot.")
                    return {
                        "id": "fyuaW6Uf-S8",
                        "title": "托管自动续期成功视频",
                        "uploader": "TestChannel",
                        "duration": 90.0,
                        "extractor_key": "Youtube",
                        "webpage_url": url,
                    }

            def _fake_reactive_refresh(site_or_url, paths=paths, proxy="", timeout=18.0, settle_sec=2.5, on_log=None):
                yt_cookie_path.write_text(
                    f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t{far_exp}\t__Secure-3PSID\treactive_refreshed_token\n",
                    encoding="utf-8",
                )
                return yt_cookie_path

            fake_ytdlp = type(sys)("yt_dlp")
            fake_ytdlp.YoutubeDL = BotChallengeThenSuccessYDL  # type: ignore[attr-defined]

            with _patch_sys_module("yt_dlp", fake_ytdlp), patch(
                "app.core.browser_cookies.refresh_hosted_site_cookies", side_effect=_fake_reactive_refresh
            ) as mock_reactive:
                info = downloader.fetch_info(
                    "https://www.youtube.com/watch?v=fyuaW6Uf-S8",
                    DownloadOptions(auto_refresh_cookies=True),
                    token=CancelToken(),
                    on_log=logs.append,
                    paths=paths,
                )
                self.assertEqual(info.title, "托管自动续期成功视频")
                self.assertEqual(mock_reactive.call_count, 1)
                self.assertEqual(call_count["n"], 2)

    def test_rotated_cookie_writeback_and_rookie_snapshot_recovery(self) -> None:
        """Verify rotated Set-Cookie writeback, rookie_bridge locked DB recovery, and corrupted config backup."""
        yt_url = "https://www.youtube.com/watch?v=fyuaW6Uf-S8"
        self.assertFalse(
            rows_have_site_auth([{"domain": ".youtube.com", "name": "PREF", "value": "tz=Asia.Shanghai", "expires": 0}], yt_url)
        )
        self.assertTrue(
            rows_have_site_auth([{"domain": ".youtube.com", "name": "__Secure-3PSID", "value": "secret_psid", "expires": 0}], yt_url)
        )
        self.assertEqual(export_domains_for(resolve_site(yt_url)), (".youtube.com", ".google.com"))

        with tempfile.TemporaryDirectory() as tmp:
            src_cookie = Path(tmp) / "youtube.txt"
            src_cookie.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\told_sapi_val\n"
                ".youtube.com\tTRUE\t/\tTRUE\t1999999999\t__Secure-1PSIDTS\told_rotated_ts_1\n",
                encoding="utf-8",
            )

            class RotatingCookieYoutubeDL:
                def __init__(self, opts):
                    self.opts = opts

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    cf = self.opts.get("cookiefile")
                    if cf:
                        Path(cf).write_text(
                            "# Netscape HTTP Cookie File\n"
                            ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSAPISID\told_sapi_val\n"
                            ".youtube.com\tTRUE\t/\tTRUE\t1999999999\t__Secure-1PSIDTS\tnew_rotated_ts_999\n",
                            encoding="utf-8",
                        )

                def extract_info(self, url, download=False):
                    return {"id": "dQw4w9WgXcQ", "title": "Rotated Cookie Test", "uploader": "YT", "duration": 60.0}

            fake_ytdlp = type(sys)("yt_dlp")
            fake_ytdlp.YoutubeDL = RotatingCookieYoutubeDL  # type: ignore[attr-defined]

            paths = AppPaths(app_dir=Path(tmp))
            dl_opts = DownloadOptions(cookies_file=str(src_cookie))
            logs: list[str] = []
            with _patch_sys_module("yt_dlp", fake_ytdlp):
                info = downloader.fetch_info(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    dl_opts,
                    token=CancelToken(),
                    on_log=logs.append,
                    paths=paths,
                )
                self.assertEqual(info.id, "dQw4w9WgXcQ")

            updated_src = src_cookie.read_text(encoding="utf-8")
            self.assertIn("new_rotated_ts_999", updated_src)
            self.assertTrue(any("自动续期" in l for l in logs))

            # Verify rookie_bridge profile discovery
            fake_rc = type(sys)("rookie_cookies")
            fake_rc.profiles = lambda browser_id, timeout=5: [  # type: ignore[attr-defined]
                {"profile": {"profile_id": "Default", "display_name": "Person 1", "is_default": True}},
            ]
            with _patch_sys_module("rookie_cookies", fake_rc):
                profs = rookie_bridge.list_browser_profiles("edge")
                self.assertEqual(len(profs), 1)
                self.assertEqual(profs[0].profile_id, "Default")

            # Verify corrupted config.json backup
            bad_cfg = paths.bootstrap_config_path
            bad_cfg.write_text("{broken json", encoding="utf-8")
            loaded = Config.load(paths=paths)
            self.assertTrue(loaded.auto_refresh_cookies)
            self.assertTrue(bad_cfg.with_suffix(".json.bak").is_file())


if __name__ == "__main__":
    unittest.main()
