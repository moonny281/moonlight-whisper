"""Moonlit Mica design system for Moonlight Whisper (v1.4.5).

One token table per theme (`DARK`, `LIGHT`) drives everything:

* `build_qss(palette)` turns the tokens into the application stylesheet (no duplicated light/dark stylesheets);
* custom-painted widgets (`MicaBackground`, `MoonMark`, `SwitchCheckBox`, ...) read the same tokens through
  `palette()` / `qcolor()` at paint time, so a theme switch restyles them without any extra bookkeeping.

Visual language
    * "Midnight" / "Daylight" Mica: a tinted vertical gradient with two soft halos (moonlight gold top-left,
      periwinkle bottom-right) so that the translucent cards on top of it read as frosted glass.
    * Cards are `rgba` surfaces with a hairline border and a brighter top edge; inputs are inset wells.
    * One accent (moonbeam blue) for actions and focus; gold is reserved for the moon mark and progress bars.
"""

from __future__ import annotations

import ctypes
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

# --------------------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    name: str
    is_dark: bool
    # window
    bg_top: str
    bg_bottom: str
    halo_gold: str
    halo_blue: str
    # surfaces
    card: str
    card_border: str
    card_edge: str          # brighter top edge of a card
    sub_card: str
    sub_card_border: str
    inset: str              # input wells
    inset_border: str
    inset_hover: str
    # text
    text: str
    text_muted: str
    text_faint: str
    # accent
    accent: str             # focus rings, links, selected text
    accent_fill: str        # primary button / switch track
    accent_fill_hover: str
    accent_fill_press: str
    on_accent: str
    accent_soft: str
    gold: str
    # status
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    danger_border: str
    # interaction
    button: str
    button_hover: str
    button_press: str
    button_border: str
    hover: str
    select: str
    switch_off: str
    switch_off_border: str
    track: str              # progress track
    scroll: str
    scroll_hover: str
    # opaque surfaces for pop-ups (menus, drop-downs, tooltips cannot be translucent)
    popup: str
    popup_border: str
    popup_hover: str
    # console
    log_bg: str
    log_text: str


DARK = Palette(
    name="dark",
    is_dark=True,
    bg_top="#0B0F1F",
    bg_bottom="#161C38",
    halo_gold="rgba(240, 208, 138, 34)",
    halo_blue="rgba(96, 132, 255, 52)",
    card="rgba(255, 255, 255, 13)",
    card_border="rgba(255, 255, 255, 22)",
    card_edge="rgba(255, 255, 255, 46)",
    sub_card="rgba(4, 7, 20, 70)",
    sub_card_border="rgba(255, 255, 255, 16)",
    inset="rgba(5, 8, 22, 120)",
    inset_border="rgba(255, 255, 255, 28)",
    inset_hover="rgba(255, 255, 255, 56)",
    text="#EEF1FB",
    text_muted="#A3ABC8",
    text_faint="#7079A0",
    accent="#8AB4FF",
    accent_fill="#3A63DE",
    accent_fill_hover="#3F6AE6",
    accent_fill_press="#2F52C0",
    on_accent="#FFFFFF",
    accent_soft="rgba(138, 180, 255, 38)",
    gold="#F0D08A",
    success="#3DD6A0",
    success_soft="rgba(61, 214, 160, 34)",
    warning="#F5B84A",
    warning_soft="rgba(245, 184, 74, 34)",
    danger="#F58A97",
    danger_soft="rgba(242, 112, 127, 38)",
    danger_border="rgba(242, 112, 127, 90)",
    button="rgba(255, 255, 255, 20)",
    button_hover="rgba(255, 255, 255, 32)",
    button_press="rgba(255, 255, 255, 12)",
    button_border="rgba(255, 255, 255, 32)",
    hover="rgba(255, 255, 255, 18)",
    select="rgba(138, 180, 255, 46)",
    switch_off="rgba(255, 255, 255, 40)",
    switch_off_border="rgba(255, 255, 255, 56)",
    track="rgba(255, 255, 255, 26)",
    scroll="rgba(255, 255, 255, 54)",
    scroll_hover="rgba(255, 255, 255, 96)",
    popup="#1A2040",
    popup_border="#2D3660",
    popup_hover="#262E55",
    log_bg="rgba(4, 7, 18, 180)",
    log_text="#9DC1FF",
)

LIGHT = Palette(
    name="light",
    is_dark=False,
    bg_top="#F4F6FC",
    bg_bottom="#E3E9F7",
    halo_gold="rgba(238, 186, 86, 70)",
    halo_blue="rgba(112, 142, 255, 56)",
    card="rgba(255, 255, 255, 176)",
    card_border="rgba(28, 42, 96, 26)",
    card_edge="rgba(255, 255, 255, 240)",
    sub_card="rgba(236, 241, 252, 190)",
    sub_card_border="rgba(28, 42, 96, 20)",
    inset="rgba(255, 255, 255, 215)",
    inset_border="rgba(28, 42, 96, 40)",
    inset_hover="rgba(28, 42, 96, 78)",
    text="#141A2E",
    text_muted="#566083",
    text_faint="#8790AE",
    accent="#3B63D9",
    accent_fill="#3B63D9",
    accent_fill_hover="#3057C8",
    accent_fill_press="#264AB0",
    on_accent="#FFFFFF",
    accent_soft="rgba(59, 99, 217, 30)",
    gold="#C08A1E",
    success="#12805A",
    success_soft="rgba(18, 128, 90, 28)",
    warning="#A86607",
    warning_soft="rgba(184, 115, 10, 30)",
    danger="#C03045",
    danger_soft="rgba(199, 58, 75, 26)",
    danger_border="rgba(199, 58, 75, 90)",
    button="rgba(255, 255, 255, 205)",
    button_hover="rgba(255, 255, 255, 255)",
    button_press="rgba(226, 232, 248, 255)",
    button_border="rgba(28, 42, 96, 44)",
    hover="rgba(28, 42, 96, 16)",
    select="rgba(59, 99, 217, 36)",
    switch_off="rgba(28, 42, 96, 56)",
    switch_off_border="rgba(28, 42, 96, 70)",
    track="rgba(28, 42, 96, 28)",
    scroll="rgba(28, 42, 96, 70)",
    scroll_hover="rgba(28, 42, 96, 120)",
    popup="#FFFFFF",
    popup_border="#D3DAEE",
    popup_hover="#EDF1FC",
    log_bg="rgba(255, 255, 255, 200)",
    log_text="#2B4C9B",
)

_PALETTES: Dict[str, Palette] = {"dark": DARK, "light": LIGHT}
_current: Palette = DARK


def palette() -> Palette:
    """The palette of the theme currently applied (dark until `apply_theme` runs)."""
    return _current


_RGBA_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)")


def qcolor(value: str, alpha_scale: float = 1.0) -> QColor:
    """Parse `#RRGGBB` or `rgba(r, g, b, a)` (a = 0..255, like the QSS dialect we emit) into a `QColor`."""
    m = _RGBA_RE.fullmatch(value.strip())
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = int(m.group(4)) if m.group(4) is not None else 255
        return QColor(r, g, b, max(0, min(255, int(a * alpha_scale))))
    c = QColor(value)
    if alpha_scale != 1.0:
        c.setAlpha(max(0, min(255, int(c.alpha() * alpha_scale))))
    return c


# --------------------------------------------------------------------------------------
# Native window integration & theme resolution
# --------------------------------------------------------------------------------------


def resolve_effective_theme(theme_pref: str) -> str:
    """Resolve `'system'` to either `'dark'` or `'light'` based on OS color scheme."""
    if theme_pref in ("dark", "light"):
        return theme_pref
    try:
        hints = QGuiApplication.styleHints()
        scheme = hints.colorScheme()
        if int(getattr(scheme, "value", scheme)) == 2:
            return "dark"
        if int(getattr(scheme, "value", scheme)) == 1:
            return "light"
    except Exception:
        pass
    return "dark"


def apply_win11_mica_window_attributes(window: Optional[QWidget], is_dark: bool) -> None:
    """Apply native Windows 11 DWM immersive dark titlebar and Mica backdrop if supported by OS."""
    if window is None or sys.platform != "win32":
        return
    try:
        hwnd = int(window.winId())
        dwmapi = ctypes.windll.dwmapi

        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        dark_val = ctypes.c_int(1 if is_dark else 0)
        dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_val), ctypes.sizeof(dark_val))

        # DWMWA_SYSTEMBACKDROP_TYPE = 38 (2 = DWMSBT_MAINWINDOW Mica)
        backdrop_val = ctypes.c_int(2)
        dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop_val), ctypes.sizeof(backdrop_val))
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# Icon assets referenced from QSS (`image: url(...)`) — generated once per theme
# --------------------------------------------------------------------------------------


def _draw_chevron(path: Path, color: QColor, up: bool = False, size: int = 24) -> bool:
    try:
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(color, 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        s = size / 24.0
        if up:
            p.drawPolyline([_pt(6 * s, 14.5 * s), _pt(12 * s, 8.5 * s), _pt(18 * s, 14.5 * s)])
        else:
            p.drawPolyline([_pt(6 * s, 9.5 * s), _pt(12 * s, 15.5 * s), _pt(18 * s, 9.5 * s)])
        p.end()
        return bool(img.save(str(path)))
    except Exception:
        return False


def _pt(x: float, y: float):
    from PySide6.QtCore import QPointF

    return QPointF(x, y)


def _icon_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "moonlight-whisper-ui"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_icons(p: Palette) -> Dict[str, str]:
    """Write the chevron PNGs for palette `p`; returns `{name: posix_path}` for those that were created."""
    out: Dict[str, str] = {}
    try:
        d = _icon_dir()
    except Exception:
        return out
    muted = qcolor(p.text_muted)
    for name, up in (("chevron_down", False), ("chevron_up", True)):
        fp = d / f"{p.name}_{name}.png"
        if _draw_chevron(fp, muted, up=up):
            out[name] = fp.as_posix()
    return out


# --------------------------------------------------------------------------------------
# Stylesheet
# --------------------------------------------------------------------------------------

_FONT_UI = '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", sans-serif'
_FONT_MONO = '"Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace'

_QSS_TEMPLATE = """
/* ===== Moonlit Mica (@name@) ===== */
* { outline: 0; }

QMainWindow { background-color: @bg_top@; }
QDialog, QMessageBox {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 @bg_top@, stop:1 @bg_bottom@);
}

QWidget {
    color: @text@;
    font-family: @font_ui@;
    font-size: 13px;
}
QMainWindow > QWidget { background: transparent; }
QLabel, QCheckBox, QRadioButton { background: transparent; }

/* ---------- Cards ---------- */
QFrame#CardFrame {
    background-color: @card@;
    border: 1px solid @card_border@;
    border-top-color: @card_edge@;
    border-radius: 16px;
}
QFrame#SubCardFrame {
    background-color: @sub_card@;
    border: 1px solid @sub_card_border@;
    border-radius: 12px;
}
QFrame#HeaderBar {
    background: transparent;
    border: none;
}
QGroupBox {
    background-color: @card@;
    border: 1px solid @card_border@;
    border-radius: 14px;
    margin-top: 26px;
    padding: 18px 14px 14px 14px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: 2px;
    padding: 2px 8px;
    color: @accent@;
    background: transparent;
    font-weight: 600;
}

/* ---------- Typography ---------- */
QLabel#AppTitle { font-size: 20px; font-weight: 700; color: @text@; letter-spacing: 0.3px; }
QLabel#AppSubtitle { font-size: 12px; color: @text_muted@; }
QLabel#VersionBadge {
    background-color: transparent;
    color: @gold@;
    border: 1px solid @gold@;
    border-radius: 9px;
    padding: 1px 8px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#SectionTitle { font-size: 14px; font-weight: 650; color: @text@; }
QLabel#CardCaption { font-size: 12px; color: @text_muted@; }
QLabel#FieldLabel { font-size: 12px; font-weight: 500; color: @text_muted@; }
QLabel#MutedLabel { font-size: 12px; color: @text_muted@; }
QLabel#HintLabel { font-size: 12px; color: @text_muted@; line-height: 150%; }
QLabel#StatPill {
    background-color: @sub_card@;
    border: 1px solid @sub_card_border@;
    border-radius: 11px;
    padding: 3px 11px;
    color: @text_muted@;
    font-size: 12px;
}
QLabel#GpuPill {
    border-radius: 12px;
    padding: 2px 12px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid @card_border@;
    background-color: transparent;
    color: @text_muted@;
}
QLabel#GpuPill[tone="ok"] { background-color: @success_soft@; color: @success@; border-color: @success@; }
QLabel#GpuPill[tone="warn"] { background-color: @warning_soft@; color: @warning@; border-color: @warning@; }
QLabel#GpuPill[tone="error"] { background-color: @danger_soft@; color: @danger@; border-color: @danger_border@; }
QLabel#GpuPill[tone="idle"] { background-color: transparent; color: @text_muted@; border-color: @card_border@; }

/* ---------- Inputs ---------- */
QPlainTextEdit, QTextEdit, QTextBrowser, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background-color: @inset@;
    color: @text@;
    border: 1px solid @inset_border@;
    border-radius: 10px;
    padding: 6px 11px;
    min-height: 22px;
    selection-background-color: @accent_fill@;
    selection-color: @on_accent@;
}
QPlainTextEdit:hover, QTextEdit:hover, QLineEdit:hover, QComboBox:hover, QSpinBox:hover {
    border-color: @inset_hover@;
}
QPlainTextEdit:focus, QTextEdit:focus, QTextBrowser:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid @accent@;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QPlainTextEdit:disabled {
    color: @text_faint@;
    background-color: @sub_card@;
}
QLineEdit[readOnly="true"] { color: @text_muted@; }

QComboBox { padding-right: 30px; }
QComboBox::drop-down { border: none; width: 28px; subcontrol-origin: padding; subcontrol-position: center right; }
@combo_arrow@
QComboBox QLineEdit { border: none; background: transparent; padding: 0; min-height: 0; }
QComboBox QAbstractItemView, QCompleter QAbstractItemView, QListView#CompleterPopup {
    background-color: @popup@;
    color: @text@;
    border: 1px solid @popup_border@;
    border-radius: 10px;
    padding: 5px;
    outline: 0;
    selection-background-color: @popup_hover@;
    selection-color: @accent@;
}
QComboBox QAbstractItemView::item { min-height: 26px; padding: 2px 8px; border-radius: 6px; }
QComboBox QAbstractItemView::item:hover { background-color: @popup_hover@; }
QComboBox QAbstractItemView::item:disabled { color: @text_faint@; }

QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: none; background: transparent; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: @hover@; border-radius: 5px; }

QPlainTextEdit#DropInputEdit {
    border: 1px dashed @inset_hover@;
    border-radius: 12px;
    background-color: @inset@;
    padding: 10px 12px;
    font-size: 13px;
}
QPlainTextEdit#DropInputEdit:focus { border: 1px dashed @accent@; }
QPlainTextEdit#DropInputEdit[dragActive="true"] {
    border: 2px dashed @accent@;
    background-color: @accent_soft@;
}
QPlainTextEdit#ConsoleLogView {
    background-color: @log_bg@;
    color: @log_text@;
    border: 1px solid @card_border@;
    border-radius: 12px;
    font-family: @font_mono@;
    font-size: 12px;
    padding: 10px;
}

/* ---------- Buttons ---------- */
QPushButton, QToolButton {
    background-color: @button@;
    color: @text@;
    border: 1px solid @button_border@;
    border-radius: 10px;
    padding: 6px 15px;
    min-height: 22px;
    font-weight: 500;
}
QPushButton:hover, QToolButton:hover { background-color: @button_hover@; }
QPushButton:pressed, QToolButton:pressed { background-color: @button_press@; }
QPushButton:focus { border-color: @accent@; }
QPushButton:disabled, QToolButton:disabled {
    background-color: @sub_card@;
    color: @text_faint@;
    border-color: @sub_card_border@;
}
QToolButton { padding: 4px 10px; }
QToolButton:checked { background-color: @accent_soft@; color: @accent@; border-color: @accent@; }

QPushButton#PrimaryButton {
    background-color: @accent_fill@;
    color: @on_accent@;
    border: 1px solid @accent_fill@;
    font-weight: 650;
    padding: 8px 22px;
}
QPushButton#PrimaryButton:hover { background-color: @accent_fill_hover@; border-color: @accent_fill_hover@; }
QPushButton#PrimaryButton:pressed { background-color: @accent_fill_press@; border-color: @accent_fill_press@; }
QPushButton#PrimaryButton:disabled { background-color: @accent_soft@; color: @text_faint@; border-color: transparent; }

QPushButton#DangerButton {
    background-color: @danger_soft@;
    color: @danger@;
    border: 1px solid @danger_border@;
}
QPushButton#DangerButton:hover { background-color: @danger_border@; color: @on_accent@; }

QPushButton#GhostButton {
    background-color: transparent;
    border: 1px solid transparent;
    color: @text_muted@;
    padding: 5px 11px;
}
QPushButton#GhostButton:hover { background-color: @hover@; color: @text@; }
QPushButton#GhostButton:pressed { background-color: @button_press@; }

QPushButton#IconButton {
    background-color: transparent;
    border: 1px solid @card_border@;
    border-radius: 12px;
    padding: 5px 13px;
    color: @text@;
}
QPushButton#IconButton:hover { background-color: @hover@; }

QPushButton#TableActionButton, QPushButton#TableDangerButton {
    border-radius: 8px;
    padding: 2px 11px;
    min-height: 22px;
    max-height: 26px;
    font-size: 12px;
}
QPushButton#TableActionButton:hover { border-color: @accent@; color: @accent@; }
QPushButton#TableDangerButton {
    background-color: @danger_soft@;
    color: @danger@;
    border: 1px solid @danger_border@;
}
QPushButton#TableDangerButton:hover { background-color: @danger_border@; color: @on_accent@; }

/* ---------- Check / radio (plain ones; switches paint themselves) ---------- */
QCheckBox, QRadioButton { spacing: 9px; padding: 2px 3px; min-height: 22px; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 17px; height: 17px;
    border: 1px solid @switch_off_border@;
    background-color: @inset@;
}
QCheckBox::indicator { border-radius: 5px; }
QRadioButton::indicator { border-radius: 9px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: @accent@; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background-color: @accent_fill@; border-color: @accent_fill@; }

/* ---------- Tables & lists ---------- */
QTableWidget, QTableView, QListWidget, QListView, QTreeView {
    background-color: transparent;
    alternate-background-color: transparent;
    border: 1px solid @card_border@;
    border-radius: 12px;
    gridline-color: transparent;
    selection-background-color: @select@;
    selection-color: @text@;
}
QTableWidget::item, QListWidget::item { padding: 4px 8px; border: none; }
QTableWidget::item:hover, QListWidget::item:hover { background-color: @hover@; }
QTableWidget::item:selected, QListWidget::item:selected { background-color: @select@; color: @text@; }
QTableCornerButton::section { background: transparent; border: none; }
QHeaderView { background: transparent; }
QHeaderView::section {
    background-color: transparent;
    color: @text_muted@;
    font-weight: 600;
    font-size: 12px;
    padding: 9px 12px;
    border: none;
    border-bottom: 1px solid @card_border@;
    text-align: left;
}

/* ---------- Progress ---------- */
QProgressBar {
    background-color: @track@;
    border: none;
    border-radius: 8px;
    text-align: center;
    color: @text@;
    font-size: 11px;
    font-weight: 600;
    min-height: 16px;
    max-height: 16px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 @accent_fill@, stop:1 @gold@);
    border-radius: 8px;
}
QProgressBar#ThinProgress { border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar#ThinProgress::chunk { border-radius: 3px; }

/* ---------- Tabs (pill style) ---------- */
QTabWidget::pane { border: none; background: transparent; top: 6px; }
QTabBar { background: transparent; }
QTabBar::tab {
    background-color: transparent;
    color: @text_muted@;
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 7px 17px;
    margin-right: 5px;
    font-weight: 500;
}
QTabBar::tab:hover { background-color: @hover@; color: @text@; }
QTabBar::tab:selected {
    background-color: @accent_soft@;
    color: @accent@;
    border-color: @accent@;
    font-weight: 650;
}

/* ---------- Scroll bars ---------- */
QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: @scroll@; border-radius: 4px; min-height: 36px; }
QScrollBar::handle:horizontal { background: @scroll@; border-radius: 4px; min-width: 36px; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: @scroll_hover@; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ---------- Menus & tooltips (opaque: they are top-level windows) ---------- */
QMenu {
    background-color: @popup@;
    color: @text@;
    border: 1px solid @popup_border@;
    border-radius: 10px;
    padding: 6px;
}
QMenu::item { padding: 7px 24px 7px 14px; border-radius: 7px; }
QMenu::item:selected { background-color: @popup_hover@; color: @accent@; }
QMenu::separator { height: 1px; background: @popup_border@; margin: 5px 8px; }
QToolTip {
    background-color: @popup@;
    color: @text@;
    border: 1px solid @popup_border@;
    border-radius: 8px;
    padding: 6px 9px;
}
"""

_COMBO_ARROW_TEMPLATE = """
QComboBox::down-arrow { image: url(@chevron_down@); width: 14px; height: 14px; }
QSpinBox::up-arrow { image: url(@chevron_up@); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url(@chevron_down@); width: 10px; height: 10px; }
"""


def _fill(template: str, values: Dict[str, str]) -> str:
    return re.sub(r"@(\w+)@", lambda m: values.get(m.group(1), m.group(0)), template)


def build_qss(p: Palette, icons: Optional[Dict[str, str]] = None) -> str:
    """Render the application stylesheet for palette `p`."""
    values: Dict[str, str] = {k: str(v) for k, v in p.__dict__.items()}
    values["font_ui"] = _FONT_UI
    values["font_mono"] = _FONT_MONO
    icons = icons or {}
    if "chevron_down" in icons and "chevron_up" in icons:
        values["combo_arrow"] = _fill(_COMBO_ARROW_TEMPLATE, icons)
    else:
        values["combo_arrow"] = ""
    return _fill(_QSS_TEMPLATE, values)


def get_qss(theme: str) -> str:
    """Stylesheet for `'dark'` / `'light'` (includes generated icon assets)."""
    p = _PALETTES.get(theme, DARK)
    return build_qss(p, ensure_icons(p))


# Backwards-compatible names (the pre-1.4.5 API exposed two static stylesheet strings).
MICA_DARK_QSS = build_qss(DARK)
MICA_LIGHT_QSS = build_qss(LIGHT)


def apply_theme(
    app: Optional[QApplication],
    theme_pref: str,
    window: Optional[QWidget] = None,
) -> str:
    """Apply the Moonlit Mica dark or light theme (QSS + DWM backdrop) and return the active theme name."""
    global _current
    effective = resolve_effective_theme(theme_pref)
    _current = _PALETTES[effective]
    if app is not None:
        app.setStyleSheet(get_qss(effective))
        # Custom-painted widgets read the palette in paintEvent: ask them to repaint with the new tokens.
        for top in app.topLevelWidgets():
            top.update()
            for child in top.findChildren(QWidget):
                child.update()
    if window is not None:
        apply_win11_mica_window_attributes(window, is_dark=_current.is_dark)
    return effective
