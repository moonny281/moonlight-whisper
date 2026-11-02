import ast
import os
import tempfile
import time
import unittest
from pathlib import Path

from app.core import downloader
from app.core.browser_cookies import cdp_cookies_to_netscape
from app.core.site_cookies import cookie_domains_for, inspect_netscape


class CookieHardeningTests(unittest.TestCase):
    def test_raw_header_only_bound_to_target_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "h.txt"
            f.write_text("Cookie: SESSDATA=tok; buvid3=x", encoding="utf-8")
            out = downloader.prepare_cookie_file(f, Path(tmp) / "t", target_url="https://www.bilibili.com/video/BV1")
            doms = {l.split("\t")[0] for l in out.read_text().splitlines() if l and not l.startswith("#")}
            self.assertEqual(doms, {".bilibili.com"})

    def test_raw_header_without_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "h.txt"
            f.write_text("Cookie: a=b", encoding="utf-8")
            self.assertIsNone(downloader.prepare_cookie_file(f, Path(tmp) / "t", target_url=""))

    def test_cookie_files_live_in_cookies_subdir_and_stale_ones_are_purged(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "t" / "cookies"
            t.mkdir(parents=True)
            old = t / "cookies_session_old.txt"
            old.write_text("x")
            past = time.time() - 7200
            os.utime(old, (past, past))
            self.assertEqual(downloader.purge_stale_cookie_files(Path(tmp) / "t"), 1)
            self.assertFalse(old.exists())

    def test_inspect_detects_wrong_site_and_missing_login_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.txt"
            far = int(time.time()) + 9999
            p.write_text(f"# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t{far}\tbuvid3\tx\n", encoding="utf-8")
            self.assertFalse(inspect_netscape(p, "https://www.bilibili.com/video/BV1").ok)   # 缺 SESSDATA
            self.assertEqual(inspect_netscape(p, "https://www.youtube.com/watch?v=aaaaaaaaaaa").matched, 0)

    def test_cdp_export_filters_foreign_domains(self):
        text, n = cdp_cookies_to_netscape(
            [
                {"name": "a", "value": "1", "domain": ".bilibili.com", "path": "/", "expires": -1},
                {"name": "b", "value": "2", "domain": ".google.com", "path": "/", "expires": -1},
            ],
            [".bilibili.com"],
        )
        self.assertEqual(n, 1)
        self.assertNotIn("google", text)

    def test_domain_binding(self):
        self.assertEqual(cookie_domains_for("https://youtu.be/xxxxxxxxxxx"), [".youtube.com"])
        self.assertEqual(cookie_domains_for("https://b23.tv/abc"), [".bilibili.com"])

    def test_task_queue_and_main_window_signal_attributes(self):
        root = Path(__file__).resolve().parent.parent
        mw_tree = ast.parse((root / "app" / "ui" / "main_window.py").read_text(encoding="utf-8"))
        wk_tree = ast.parse((root / "app" / "workers.py").read_text(encoding="utf-8"))

        tq_attrs = {"cfg", "paths"}
        for node in ast.walk(wk_tree):
            if isinstance(node, ast.ClassDef) and node.name == "TaskQueue":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        tq_attrs.add(item.name)
                    elif isinstance(item, ast.Assign):
                        for t in item.targets:
                            if isinstance(t, ast.Name):
                                tq_attrs.add(t.id)

        referenced = set()
        for node in ast.walk(mw_tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
                if isinstance(node.value.value, ast.Name) and node.value.value.id == "self" and node.value.attr == "queue":
                    referenced.add(node.attr)

        self.assertTrue(referenced.issubset(tq_attrs), f"Missing TaskQueue attributes: {referenced - tq_attrs}")
        for required_method in ("cancel_current", "cancel_all", "cancel_task", "clear_inactive_tasks", "retry_failed_tasks", "shutdown_and_wait", "stats"):
            self.assertIn(required_method, tq_attrs)


if __name__ == "__main__":
    unittest.main()
