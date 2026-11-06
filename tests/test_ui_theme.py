"""Tests for the v1.4.5 Moonlit Mica theme and redesigned widgets (run headless via Qt's offscreen platform)."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # PySide6 may be missing in minimal CI images
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False

from app.paths import AppPaths

if HAVE_QT:
    from app.config import Config
    from app.core.errors import ErrorInfo
    from app.core.pipeline import Job
    from app.ui import theme
    from app.ui.main_window import MainWindow
    from app.ui.widgets import (
        MicaBackground,
        MoonMark,
        ProgressCell,
        SwitchCheckBox,
        TaskQueueTable,
        make_mica_section,
    )
    from app.workers import TaskItem, TaskStatus

_app = None


def setUpModule() -> None:  # noqa: N802
    global _app
    if HAVE_QT:
        _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ThemeTokenTests(unittest.TestCase):
    def test_both_palettes_fully_populated(self) -> None:
        for pal in (theme.DARK, theme.LIGHT):
            for f in fields(pal):
                value = getattr(pal, f.name)
                self.assertTrue(value not in ("", None), f"{pal.name}.{f.name} is empty")
        self.assertTrue(theme.DARK.is_dark)
        self.assertFalse(theme.LIGHT.is_dark)

    def test_qss_has_no_unresolved_placeholders_and_balanced_braces(self) -> None:
        for name in ("dark", "light"):
            qss = theme.get_qss(name)
            self.assertEqual(re.findall(r"@\w+@", qss), [], f"unresolved tokens in {name}")
            self.assertEqual(qss.count("{"), qss.count("}"))
            for selector in (
                "QFrame#CardFrame",
                "QPushButton#PrimaryButton",
                "QProgressBar#ThinProgress",
                "QLabel#GpuPill[tone=\"ok\"]",
                "QPlainTextEdit#DropInputEdit[dragActive=\"true\"]",
                "QTabBar::tab:selected",
                "QScrollBar:vertical",
            ):
                self.assertIn(selector, qss)

    def test_every_object_name_used_by_the_ui_has_a_rule(self) -> None:
        ui_dir = Path(__file__).resolve().parents[1] / "app" / "ui"
        used = set()
        for fp in ui_dir.glob("*.py"):
            if fp.name == "theme.py":
                continue
            used |= set(re.findall(r'setObjectName\("(\w+)"\)', fp.read_text(encoding="utf-8")))
        qss = theme.get_qss("dark")
        missing = sorted(n for n in used if f"#{n}" not in qss)
        self.assertEqual(missing, [], f"object names without QSS rules: {missing}")

    def test_legacy_constants_still_exposed(self) -> None:
        self.assertIn("CardFrame", theme.MICA_DARK_QSS)
        self.assertIn("CardFrame", theme.MICA_LIGHT_QSS)

    def test_qcolor_parses_hex_and_rgba(self) -> None:
        c = theme.qcolor("#4F7DF3")
        self.assertEqual((c.red(), c.green(), c.blue(), c.alpha()), (0x4F, 0x7D, 0xF3, 255))
        c = theme.qcolor("rgba(255, 255, 255, 13)")
        self.assertEqual((c.red(), c.green(), c.blue(), c.alpha()), (255, 255, 255, 13))
        self.assertEqual(theme.qcolor("rgba(10, 20, 30, 200)", alpha_scale=0.5).alpha(), 100)

    def test_apply_theme_switches_palette(self) -> None:
        self.assertEqual(theme.apply_theme(_app, "light"), "light")
        self.assertIs(theme.palette(), theme.LIGHT)
        self.assertEqual(theme.apply_theme(_app, "dark"), "dark")
        self.assertIs(theme.palette(), theme.DARK)
        self.assertIn("Moonlit Mica (dark)", _app.styleSheet())

    def test_dark_and_light_text_contrast_is_readable(self) -> None:
        def lum(c) -> float:
            def ch(v: int) -> float:
                v /= 255
                return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

            return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())

        def ratio(a, b) -> float:
            la, lb = sorted((lum(a), lum(b)), reverse=True)
            return (la + 0.05) / (lb + 0.05)

        for pal in (theme.DARK, theme.LIGHT):
            bg = theme.qcolor(pal.bg_bottom)
            self.assertGreaterEqual(ratio(theme.qcolor(pal.text), bg), 7.0, pal.name)
            self.assertGreaterEqual(ratio(theme.qcolor(pal.text_muted), bg), 4.5, pal.name)
            self.assertGreaterEqual(ratio(theme.qcolor(pal.on_accent), theme.qcolor(pal.accent_fill)), 4.5, pal.name)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class WidgetTests(unittest.TestCase):
    def test_switch_keeps_checkbox_api(self) -> None:
        sw = SwitchCheckBox("测试开关")
        seen = []
        sw.toggled.connect(seen.append)
        self.assertFalse(sw.isChecked())
        sw.setChecked(True)
        self.assertTrue(sw.isChecked())
        self.assertEqual(seen, [True])
        self.assertEqual(sw.knobPos, 1.0)          # not shown yet -> jumps without animating
        sw.click()
        self.assertFalse(sw.isChecked())
        self.assertEqual(seen, [True, False])
        self.assertEqual(sw.knobPos, 0.0)
        self.assertGreater(sw.sizeHint().width(), SwitchCheckBox.TRACK_W)
        sw.setEnabled(False)
        sw.click()
        self.assertFalse(sw.isChecked())

    def test_switch_and_decorations_render(self) -> None:
        for name in ("dark", "light"):
            theme.apply_theme(_app, name)
            for w in (SwitchCheckBox("渲染"), MoonMark(48), MicaBackground()):
                w.resize(200, 60)
                img = w.grab().toImage()
                self.assertFalse(img.isNull())
                self.assertTrue(any(img.pixelColor(x, y).alpha() > 0 for x in range(0, 40, 4) for y in range(0, 40, 4)))

    def test_progress_cell_clamps_and_formats(self) -> None:
        cell = ProgressCell()
        cell.setValue(37)
        self.assertEqual((cell.bar.value(), cell.lbl.text()), (37, "37%"))
        cell.setValue(250)
        self.assertEqual((cell.bar.value(), cell.lbl.text()), (100, "100%"))
        cell.setValue(-5)
        self.assertEqual(cell.bar.value(), 0)

    def test_make_mica_section_returns_card_and_layout(self) -> None:
        card, layout = make_mica_section("标题", "说明文字")
        self.assertEqual(card.objectName(), "CardFrame")
        self.assertGreaterEqual(layout.count(), 1)

    def _item(self, src: str, status, stage: str, progress: int):
        it = TaskItem(job=Job(source=src), display_title=src)
        it.status, it.stage_text, it.progress = status, stage, progress
        return it

    def test_queue_table_rows_empty_state_and_theme_refresh(self) -> None:
        table = TaskQueueTable()
        table.resize(800, 300)
        self.assertFalse(table.grab().toImage().isNull())            # empty-state painting must not raise

        running = self._item("a.mp4", TaskStatus.RUNNING, "转录中", 40)
        failed = self._item("b.mp4", TaskStatus.FAILED, "失败", 10)
        failed.error_info = ErrorInfo(category="download", user_message="无法下载", recoverable_hint="检查网络")
        table.add_task_row(running)
        table.add_task_row(failed)
        self.assertEqual(table.rowCount(), 2)
        self.assertIsInstance(table.cellWidget(0, 2), ProgressCell)
        self.assertEqual(table.cellWidget(0, 2).lbl.text(), "40%")
        self.assertTrue(table.item(0, 1).text().endswith("转录中"))
        self.assertIn("检查网络", table.item(1, 1).toolTip())

        running.progress = 85
        table.update_task_row(running)
        self.assertEqual(table.cellWidget(0, 2).lbl.text(), "85%")

        theme.apply_theme(_app, "light")
        table.refresh_theme()
        light_fg = table.item(0, 1).foreground().color().name()
        theme.apply_theme(_app, "dark")
        table.refresh_theme()
        self.assertNotEqual(light_fg, table.item(0, 1).foreground().color().name())

        table.remove_tasks([failed.job.job_id])
        self.assertEqual(table.rowCount(), 1)
        table.remove_tasks([running.job.job_id])
        self.assertFalse(table.grab().toImage().isNull())


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class MainWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = AppPaths(app_dir=Path(self._tmp.name))
        self.cfg = Config()
        self.window = MainWindow(self.cfg, self.paths)

    def tearDown(self) -> None:
        self.window.close()
        self._tmp.cleanup()

    def test_window_exposes_all_controls_used_by_the_logic(self) -> None:
        names = [
            "logo_label", "gpu_indicator", "btn_theme", "btn_models_top", "btn_settings_top", "btn_about_top",
            "lbl_line_count", "btn_paste", "btn_add_files", "btn_clear", "btn_start", "input_edit",
            "combo_model", "combo_lang", "edit_output_dir", "btn_browse_out", "btn_open_out",
            "chk_gpu", "chk_to_simplified", "chk_write_srt", "chk_keep_audio", "chk_allow_playlist",
            "chk_llm_summary", "chk_include_timestamps", "chk_include_video_info", "chk_route",
            "lbl_queue_stats", "btn_retry_failed", "btn_clear_inactive", "btn_cancel_current", "btn_cancel_all",
            "queue_table", "btn_toggle_log", "btn_copy_log", "btn_clear_log", "btn_open_logs_dir",
            "btn_update_ytdlp", "log_view", "tray_icon",
        ]
        missing = [n for n in names if not hasattr(self.window, n)]
        self.assertEqual(missing, [])
        for n in ("chk_gpu", "chk_route", "chk_to_simplified", "chk_write_srt", "chk_keep_audio",
                  "chk_allow_playlist", "chk_llm_summary", "chk_include_timestamps", "chk_include_video_info"):
            self.assertIsInstance(getattr(self.window, n), SwitchCheckBox, n)
        self.assertIsInstance(self.window.logo_label, MoonMark)
        self.assertIsInstance(self.window.centralWidget(), MicaBackground)

    def test_switches_drive_config(self) -> None:
        self.window.chk_write_srt.setChecked(True)
        self.assertTrue(self.cfg.write_srt)
        self.window.chk_route.setChecked(True)
        self.assertTrue(self.cfg.route_enabled)
        self.window.chk_route.setChecked(False)
        self.assertFalse(self.cfg.route_enabled)

    def test_language_filtering_still_works_in_the_new_layout(self) -> None:
        w = self.window
        w.combo_model.setCurrentIndex(w.combo_model.findData("SenseVoiceSmall"))
        fr = w.combo_lang.findData("fr")
        self.assertFalse(w.combo_lang.model().item(fr).isEnabled())
        w.combo_lang.setCurrentIndex(fr)
        self.assertIsNone(w.combo_lang.currentData())
        w.chk_route.setChecked(True)
        self.assertTrue(w.combo_lang.model().item(fr).isEnabled())

    def test_minimum_size_and_screenshot_in_both_themes(self) -> None:
        self.assertGreaterEqual(self.window.minimumWidth(), 980)
        for name in ("dark", "light"):
            theme.apply_theme(_app, name, window=self.window)
            self.window.resize(1100, 800)
            self.window.show()
            img = self.window.grab().toImage()
            self.assertGreaterEqual(img.width(), 980)
            self.assertFalse(img.isNull())

    def test_stats_label_has_no_dot_separators(self) -> None:
        self.window._refresh_queue_stats()
        self.assertNotIn("·", self.window.lbl_queue_stats.text())
        self.assertIn("共 0 项", self.window.lbl_queue_stats.text())

    def test_drag_highlight_property_toggles(self) -> None:
        edit = self.window.input_edit
        edit.set_drag_active(True)
        self.assertTrue(edit.property("dragActive"))
        edit.set_drag_active(False)
        self.assertFalse(edit.property("dragActive"))


if __name__ == "__main__":
    unittest.main()
