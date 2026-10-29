"""Unit tests for `app.paths` data directory strategy and `app.config` schema migration."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.paths import AppPaths


class TestPathsAndConfig(unittest.TestCase):
    def test_writable_app_dir_is_default_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "MoonlightWhisper"
            app_dir.mkdir()
            paths = AppPaths(app_dir=app_dir)
            self.assertEqual(paths.data_root, app_dir.resolve())
            self.assertEqual(paths.models_dir, (app_dir / "models").resolve())
            self.assertEqual(paths.gpu_runtime_dir, (app_dir / "gpu_runtime").resolve())

    def test_custom_data_dir_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "app"
            custom_data = Path(tmp) / "custom_data"
            paths = AppPaths(app_dir=app_dir, custom_data_dir=str(custom_data))
            self.assertEqual(paths.data_root, custom_data.resolve())
            self.assertEqual(paths.config_path, (custom_data / "config.json").resolve())

    def test_fallback_to_local_appdata_when_app_dir_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "ProgramFilesApp"
            appdata = Path(tmp) / "LocalAppData"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)}):
                with patch.object(AppPaths, "is_dir_writable", return_value=False):
                    paths = AppPaths(app_dir=app_dir)
                    self.assertEqual(paths.data_root, (appdata / "MoonlightWhisper").resolve())

    def test_directory_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "old_models"
            dst = Path(tmp) / "new_models"
            (src / "small").mkdir(parents=True)
            (src / "small" / "model.bin").write_bytes(b"12345")
            count = AppPaths.migrate_directory_contents(src, dst)
            self.assertEqual(count, 1)
            self.assertTrue((dst / "small" / "model.bin").exists())
            self.assertFalse((src / "small").exists())

    def test_config_migration_and_defaults(self) -> None:
        raw_v0 = {
            "version": 0,
            "simplify_chinese": False,
            "export_srt": True,
            "allow_playlist": True,
            "pypi_mirror": "invalid_mirror",
            "llm": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-test",
                "model": "deepseek-chat",
            },
        }
        cfg = Config.from_dict(raw_v0)
        self.assertEqual(cfg.version, 1)
        self.assertFalse(cfg.to_simplified)
        self.assertTrue(cfg.write_srt)
        self.assertTrue(cfg.allow_playlist)
        self.assertEqual(cfg.pypi_mirror, "tuna")
        self.assertTrue(cfg.llm.enabled)
        self.assertEqual(cfg.llm.model, "deepseek-chat")

    def test_config_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            cfg = Config(model="small", write_srt=True, allow_playlist=True)
            cfg.llm.enabled = True
            cfg.llm.api_key = "sk-demo"
            saved_path = cfg.save(paths=paths)
            self.assertTrue(saved_path.exists())

            loaded = Config.load(paths=paths)
            self.assertEqual(loaded.model, "small")
            self.assertTrue(loaded.write_srt)
            self.assertTrue(loaded.allow_playlist)
            self.assertTrue(loaded.llm.enabled)
            self.assertEqual(loaded.llm.api_key, "sk-demo")


if __name__ == "__main__":
    unittest.main()
