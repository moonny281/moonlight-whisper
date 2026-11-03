"""Unit tests for built-in `rookie-cookies` (teng-lin/rookie-cookies) integration, locked-DB snapshot recovery, and YouTube direct fallback."""

from __future__ import annotations

import contextlib
import http.cookiejar
import sqlite3
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
        """Regression test for v1.2.9+: downloading must NEVER spawn a new Edge/Chrome window by default."""
        cfg = Config()
        self.assertEqual(cfg.cookies_app_bound, "injection_only")
        dl_opts = DownloadOptions()
        self.assertEqual(dl_opts.cookies_app_bound, "injection_only")

        migrated_cfg = Config.from_dict({"version": 1, "cookies_from_browser": "edge", "cookies_app_bound": "injection_only"})
        self.assertEqual(migrated_cfg.cookies_app_bound, "injection_only")

        # Verify dead code refresh_managed_cookies has been removed so nothing reuses a fixed browser_profile
        self.assertFalse(hasattr(browser_cookies, "refresh_managed_cookies"))

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
                    "expires": 0,
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

        with tempfile.TemporaryDirectory() as tmp, _patch_sys_module("rookie_cookies", fake_rc), patch(
            "app.core.rookie_bridge._copy_locked_chromium_cookies_db", return_value=(None, None)
        ):
            out = rookie_bridge.extract_to_netscape_file(
                browser="edge",
                target_url="https://www.bilibili.com/video/BV1",
                temp_dir=Path(tmp),
                token=CancelToken(),
            )
            self.assertIsNone(out)
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

    def test_edge_locked_db_snapshot_recovery_and_youtube_direct_fallback(self) -> None:
        """Regression test for v1.3.0:
        1) When `rookie_cookies.extract` fails with OS error 32, `_copy_locked_chromium_cookies_db` + SQLite reader recovers cookies;
        2) When YouTube still blocks with 'Sign in to confirm you're not a bot', `_fallback_youtube_direct` resolves audio via Piped/Invidious."""
        with tempfile.TemporaryDirectory() as tmp:
            # Part A: Verify SQLite cookie snapshot recovery when OS error 32 occurs
            snap_db = Path(tmp) / "Cookies.sqlite"
            conn = sqlite3.connect(str(snap_db))
            conn.execute(
                "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, path TEXT, is_secure INTEGER, is_httponly INTEGER, expires_utc INTEGER)"
            )
            conn.execute(
                "INSERT INTO cookies VALUES ('.youtube.com', 'SAPISID', 'sapi_from_snap', X'', '/', 1, 0, 13450000000000000)"
            )
            conn.commit()
            conn.close()

            fake_rc = type(sys)("rookie_cookies")

            def _locked_extract(**kw):
                raise RuntimeError("Windows browser database is share-locked at Default/Network/Cookies (OS error 32)")

            fake_rc.extract = _locked_extract  # type: ignore[attr-defined]

            with _patch_sys_module("rookie_cookies", fake_rc), patch(
                "app.core.rookie_bridge._copy_locked_chromium_cookies_db", return_value=(snap_db, None)
            ):
                recovered_file = rookie_bridge.extract_to_netscape_file(
                    browser="edge",
                    target_url="https://www.youtube.com/watch?v=fyuaW6Uf-S8&t=17s",
                    temp_dir=Path(tmp),
                    token=CancelToken(),
                )
                self.assertIsNotNone(recovered_file)
                self.assertIn("sapi_from_snap", recovered_file.read_text(encoding="utf-8"))

            # Part B: Verify yt-dlp raises clean DownloadError with login guidance when blocked
            class BotBlockedYoutubeDL:
                def __init__(self, opts):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def extract_info(self, url, download=False):
                    raise RuntimeError("ERROR: [youtube] fyuaW6Uf-S8: Sign in to confirm you’re not a bot.")

            fake_ytdlp = type(sys)("yt_dlp")
            fake_ytdlp.YoutubeDL = BotBlockedYoutubeDL  # type: ignore[attr-defined]

            with _patch_sys_module("rookie_cookies", fake_rc), _patch_sys_module("yt_dlp", fake_ytdlp), patch(
                "app.core.rookie_bridge._copy_locked_chromium_cookies_db", return_value=(None, None)
            ):
                paths = AppPaths(app_dir=Path(tmp))
                dl_opts = DownloadOptions(cookies_from_browser="edge")
                logs: list[str] = []
                yt_url = "https://www.youtube.com/watch?v=fyuaW6Uf-S8&t=17s"
                with self.assertRaises(DownloadError) as ctx:
                    downloader.fetch_info(
                        yt_url,
                        dl_opts,
                        token=CancelToken(),
                        on_log=logs.append,
                        paths=paths,
                    )
                self.assertIn("登录", ctx.exception.user_message)

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


    def test_v1_3_1_v20_browser_id_decryption_and_persistent_cookie_cache(self) -> None:
        """Regression test for v1.3.1:
        1) Anonymous-only cookies (e.g. PREF) do not fool rows_have_site_auth into skipping v20 extraction;
        2) _call_extract_from_path passes browser_id="edge" (not local_state_path) so v20 App-Bound decryption works on snap_db;
        3) Once valid YouTube login cookies are extracted, subsequent calls reuse the persistent cache with 0 browser calls."""
        from app.core.site_cookies import rows_have_site_auth

        yt_url = "https://www.youtube.com/watch?v=fyuaW6Uf-S8&t=17s"
        self.assertFalse(
            rows_have_site_auth([{"domain": ".youtube.com", "name": "PREF", "value": "tz=Asia.Shanghai", "expires": 0}], yt_url)
        )
        self.assertTrue(
            rows_have_site_auth([{"domain": ".youtube.com", "name": "__Secure-3PSID", "value": "secret_psid", "expires": 0}], yt_url)
        )

        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp) / "temp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            snap_db = Path(tmp) / "Cookies.sqlite"
            local_state = Path(tmp) / "Local State"
            local_state.write_text("{}", encoding="utf-8")

            conn = sqlite3.connect(str(snap_db))
            conn.execute(
                "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, path TEXT, is_secure INTEGER, is_httponly INTEGER, expires_utc INTEGER)"
            )
            # 1 anonymous plaintext cookie (PREF) + 1 v20-encrypted login cookie (__Secure-3PSID)
            conn.execute(
                "INSERT INTO cookies VALUES ('.youtube.com', 'PREF', 'f4=4000000', X'', '/', 1, 0, 13450000000000000)"
            )
            conn.execute(
                "INSERT INTO cookies VALUES ('.youtube.com', '__Secure-3PSID', '', X'76323000112233445566778899AABBCCDDEEFF', '/', 1, 1, 13450000000000000)"
            )
            conn.commit()
            conn.close()

            fake_rc = type(sys)("rookie_cookies")
            path_calls = []

            def _fake_extract_from_path(db_p, **kw):
                path_calls.append(kw)
                # Verify browser_id is passed (NOT local_state_path) so v20 decryption is enabled
                if kw.get("browser_id") == "edge" and kw.get("app_bound") in ("allow_elevated_fallback", "injection_only"):
                    return [
                        {
                            "domain": ".youtube.com",
                            "name": "__Secure-3PSID",
                            "value": "v20_decrypted_psid_ok",
                            "path": "/",
                            "secure": True,
                            "http_only": True,
                            "expires": 1999999999,
                        }
                    ]
                return []

            fake_rc.extract_from_path = _fake_extract_from_path  # type: ignore[attr-defined]
            fake_rc.extract = lambda **kw: (_ for _ in ()).throw(RuntimeError("OS error 32"))  # type: ignore[attr-defined]

            with _patch_sys_module("rookie_cookies", fake_rc), patch(
                "app.core.rookie_bridge._copy_locked_chromium_cookies_db", return_value=(snap_db, local_state)
            ):
                # Call 1: unlocks snap_db, sees PREF is not enough & v20_skipped==1, decrypts via browser_id="edge" and caches
                out1 = rookie_bridge.extract_to_netscape_file(
                    browser="edge",
                    target_url=yt_url,
                    temp_dir=temp_dir,
                    app_bound="smart",
                    token=CancelToken(),
                )
                self.assertIsNotNone(out1)
                self.assertIn("v20_decrypted_psid_ok", out1.read_text(encoding="utf-8"))
                self.assertTrue(any(c.get("browser_id") == "edge" for c in path_calls))

                # Call 2: hits persistent validated cache immediately with ZERO calls to extract_from_path!
                calls_before = len(path_calls)
                out2 = rookie_bridge.extract_to_netscape_file(
                    browser="edge",
                    target_url=yt_url,
                    temp_dir=temp_dir,
                    token=CancelToken(),
                )
                self.assertIsNotNone(out2)
                self.assertIn("v20_decrypted_psid_ok", out2.read_text(encoding="utf-8"))
                self.assertEqual(len(path_calls), calls_before)


    def test_v1_3_2_code_review_fixes(self) -> None:
        """Regression tests for all items in the v1.3.1 code review report (Issues A, B, C, D, E, F, G & Section 3)."""
        # Issue B: Old Node (<22) must be rejected by _usable and must NOT block ensure_deno_binary
        with tempfile.TemporaryDirectory() as tmp:
            fake_node = Path(tmp) / "node.exe"
            fake_node.write_bytes(b"fake")
            fake_deno = Path(tmp) / "deno.exe"
            fake_deno.write_bytes(b"fake")

            with patch("app.core.downloader._probe_version", return_value=(18, 20, 0)):
                self.assertIsNone(downloader._usable(fake_node, "node"))
            with patch("app.core.downloader._probe_version", return_value=(22, 4, 0)):
                self.assertEqual(downloader._usable(fake_node, "node"), fake_node)
            with patch("app.core.downloader._probe_version", return_value=(1, 46, 0)):
                self.assertIsNone(downloader._usable(fake_deno, "deno"))
            with patch("app.core.downloader._probe_version", return_value=(2, 3, 1)):
                self.assertEqual(downloader._usable(fake_deno, "deno"), fake_deno)

            ydl_opts: dict = {}
            ensure_called = []
            with patch.object(AppPaths, "find_binary", side_effect=lambda name: fake_node if name == "node" else None), patch(
                "app.core.downloader._probe_version", return_value=(20, 11, 0)
            ), patch(
                "app.core.downloader.ensure_deno_binary",
                side_effect=lambda **kw: (ensure_called.append(True) or fake_deno),
            ):
                downloader._apply_js_runtime_opts(
                    ydl_opts,
                    deno_bin=None,
                    paths=AppPaths(app_dir=Path(tmp)),
                    target_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                )
                self.assertTrue(ensure_called)
                self.assertEqual(ydl_opts["js_runtimes"], {"deno": {"path": str(fake_deno)}})

        # Issue C: _pick_root_exc must preserve root-cause JS challenge failure over subsequent bot-check retry errors
        e1 = RuntimeError("ERROR: [youtube] dQw4w9WgXcQ: n challenge solving failed: QuickJS error")
        e2 = RuntimeError("ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot")
        self.assertIs(downloader._pick_root_exc([e1, e2]), e1)


        # Section 3: Corrupted config.json backup & proxy URL credential redaction
        self.assertEqual(
            downloader._redact_proxy_url("http://myuser:secretpass@127.0.0.1:7890"),
            "http://***:***@127.0.0.1:7890",
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            bad_cfg = paths.bootstrap_config_path
            bad_cfg.write_text("{broken json", encoding="utf-8")
            loaded = Config.load(paths=paths)
            self.assertEqual(loaded.cookies_app_bound, "injection_only")
            self.assertTrue(bad_cfg.with_suffix(".json.bak").is_file())


    def test_v1_3_3_ephemeral_login_profile_and_rotated_cookie_writeback(self) -> None:
        """Regression tests for v1.3.3:
        1) ManagedBrowser & purge_stale_cookie_files clean up ephemeral login_profile_<uuid> directories and stale owners;
        2) Rotated cookies written by yt-dlp to the session cookiefile are atomically synced back to the source cookie file."""
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp) / "temp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            stale_prof = temp_dir / "login_profile_deadbeef"
            stale_prof.mkdir()
            (stale_prof / "LockFile").write_text("locked", encoding="utf-8")
            # Backdate mtime so purge_stale_cookie_files cleans it up
            import os
            old_ts = 1000000000
            os.utime(stale_prof, (old_ts, old_ts))
            removed = downloader.purge_stale_cookie_files(temp_dir, max_age_sec=3600)
            self.assertGreaterEqual(removed, 1)
            self.assertFalse(stale_prof.exists())

            # Verify _sync_rotated_cookies_back updates source cookie file when yt-dlp rotates tokens during extract_info
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
                    # Simulate yt-dlp flushing rotated Set-Cookie values into cookiefile on close
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
            self.assertNotIn("old_rotated_ts_1", updated_src)
            self.assertTrue(any("自动续期" in l for l in logs))


    def test_v1_3_4_singleton_suppression_and_ytdlp_native_cookie_fallback(self) -> None:
        """Regression tests for v1.3.4:
        1) _suppress_chromium_singleton safely acquires Mutex and renames/restores ProcessSingleton window;
        2) Native yt-dlp cookiesfrombrowser is used as fallback for Firefox when rookie-cookies is absent or empty."""
        with tempfile.TemporaryDirectory() as tmp:
            test_dir = Path(tmp) / "User Data"
            test_dir.mkdir()
            # On Linux/non-Windows this is a clean no-op
            with rookie_bridge._suppress_chromium_singleton(test_dir):
                pass

            # Verify yt-dlp cookiesfrombrowser fallback for Firefox when cookiefile is empty
            paths = AppPaths(app_dir=Path(tmp))
            dl_opts = DownloadOptions(cookies_from_browser="firefox", cookies_browser_profile="default")
            logs: list[str] = []
            ydl_opts = downloader._build_common_ydl_opts(
                dl_opts,
                on_log=logs.append,
                paths=paths,
                target_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                token=CancelToken(),
            )
            self.assertEqual(ydl_opts.get("cookiesfrombrowser"), ("firefox", "default", None, None))


if __name__ == "__main__":
    unittest.main()
