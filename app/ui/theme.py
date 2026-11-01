"""Windows 11 Mica / Fluent Design System (Light & Dark themes) for Moonlight Whisper."""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget


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
        dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(dark_val), ctypes.sizeof(dark_val)
        )

        # DWMWA_SYSTEMBACKDROP_TYPE = 38 (2 = DWMSBT_MAINWINDOW Mica)
        backdrop_val = ctypes.c_int(2)
        dwmapi.DwmSetWindowAttribute(
            hwnd, 38, ctypes.byref(backdrop_val), ctypes.sizeof(backdrop_val)
        )
    except Exception:
        pass


MICA_DARK_QSS = """
/* === Windows 11 Dark Mica Theme (深色云母风格) === */
QMainWindow, QDialog {
    background-color: #181A20;
    color: #F3F4F6;
    font-family: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
}

QWidget {
    color: #F3F4F6;
    font-family: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
}

/* Prevent child labels/checkboxes from painting opaque boxes over siblings */
QLabel, QCheckBox, QRadioButton {
    background-color: transparent;
}

/* Mica Elevated Card Surface (Layer 1) */
QFrame#CardFrame {
    background-color: #21252E;
    border: 1px solid #2E3442;
    border-top: 1px solid #363D4E;
    border-radius: 10px;
}

/* Mica Secondary Surface (Layer 2 inside cards) */
QFrame#SubCardFrame {
    background-color: #1A1D25;
    border: 1px solid #282D3A;
    border-radius: 8px;
}

/* GroupBox fallback with generous top clearance so title NEVER overlaps content */
QGroupBox {
    background-color: #21252E;
    border: 1px solid #2E3442;
    border-radius: 10px;
    margin-top: 26px;
    padding: 18px 14px 14px 14px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    top: 2px;
    padding: 2px 8px;
    color: #60CDFF;
    background-color: transparent;
    font-size: 13px;
    font-weight: 600;
}

/* Typography Tokens */
QLabel#AppTitle {
    font-size: 18px;
    font-weight: 700;
    color: #FFFFFF;
    padding: 1px 0px;
}
QLabel#AppSubtitle {
    font-size: 12px;
    color: #9CA3AF;
    padding: 1px 0px;
}
QLabel#VersionBadge {
    background-color: #262C3A;
    color: #60CDFF;
    border: 1px solid #384152;
    border-radius: 8px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#SectionTitle {
    font-size: 14px;
    font-weight: 600;
    color: #F9FAFB;
    padding: 1px 0px;
}
QLabel#FieldLabel {
    font-size: 13px;
    font-weight: 500;
    color: #D1D5DB;
    padding-right: 4px;
}
QLabel#MutedLabel {
    font-size: 12px;
    color: #9CA3AF;
}
QLabel#StatPill {
    background-color: #161920;
    border: 1px solid #2C3240;
    border-radius: 6px;
    padding: 3px 10px;
    color: #9CA3AF;
    font-size: 12px;
}

/* Inputs & Dropdowns */
QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
    background-color: #161920;
    color: #F3F4F6;
    border: 1px solid #323846;
    border-bottom: 1px solid #454D5E;
    border-radius: 6px;
    padding: 5px 10px;
    min-height: 22px;
    selection-background-color: #0078D4;
    selection-color: #FFFFFF;
}
QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    background-color: #12151B;
    border: 1px solid #60CDFF;
    border-bottom: 2px solid #60CDFF;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background-color: #21252E;
    color: #F3F4F6;
    border: 1px solid #363D4E;
    selection-background-color: #2D3546;
    selection-color: #60CDFF;
    padding: 4px;
}

QPlainTextEdit#DropInputEdit {
    border: 1px dashed #3B4354;
    border-radius: 8px;
    background-color: #15181F;
    padding: 8px 10px;
    font-size: 13px;
}
QPlainTextEdit#DropInputEdit:focus {
    border: 1px dashed #60CDFF;
    background-color: #12151B;
}

QPlainTextEdit#ConsoleLogView {
    background-color: #101217;
    color: #93C5FD;
    border: 1px solid #2A303D;
    border-radius: 8px;
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 12px;
    padding: 8px;
}

/* Fluent Buttons */
QPushButton, QToolButton {
    background-color: #2A2F3B;
    color: #F3F4F6;
    border: 1px solid #383F4E;
    border-top: 1px solid #434B5C;
    border-radius: 6px;
    padding: 5px 14px;
    min-height: 22px;
    font-weight: 500;
}
QPushButton:hover, QToolButton:hover {
    background-color: #323846;
    border-color: #4B5568;
}
QPushButton:pressed, QToolButton:pressed {
    background-color: #1E222B;
    color: #D1D5DB;
}
QPushButton:disabled, QToolButton:disabled {
    background-color: #1C1F27;
    color: #5E6675;
    border-color: #282C37;
}

QPushButton#PrimaryButton {
    background-color: #0078D4;
    color: #FFFFFF;
    border: 1px solid #1084DF;
    border-top: 1px solid #3597E6;
    font-weight: 600;
    font-size: 13px;
    padding: 6px 18px;
}
QPushButton#PrimaryButton:hover {
    background-color: #1084DF;
}
QPushButton#PrimaryButton:pressed {
    background-color: #006CBE;
}
QPushButton#PrimaryButton:disabled {
    background-color: #1E3A5F;
    color: #93C5FD;
    border-color: #1E3A5F;
}

QPushButton#DangerButton {
    background-color: #381F24;
    color: #FCA5A5;
    border: 1px solid #632B34;
}
QPushButton#DangerButton:hover {
    background-color: #4C252D;
    border-color: #823440;
}

QPushButton#GhostButton {
    background-color: transparent;
    border: 1px solid transparent;
    color: #D1D5DB;
    padding: 4px 10px;
}
QPushButton#GhostButton:hover {
    background-color: #282D3A;
    border-color: #363D4E;
    color: #FFFFFF;
}

/* Compact Inline Table Action Buttons (never clipped inside table rows) */
QPushButton#TableActionButton {
    background-color: #2A2F3B;
    color: #E5E7EB;
    border: 1px solid #3B4252;
    border-radius: 5px;
    padding: 2px 10px;
    min-height: 22px;
    max-height: 26px;
    font-size: 12px;
}
QPushButton#TableActionButton:hover {
    background-color: #363D4E;
    border-color: #60CDFF;
    color: #FFFFFF;
}
QPushButton#TableDangerButton {
    background-color: #381F24;
    color: #FCA5A5;
    border: 1px solid #632B34;
    border-radius: 5px;
    padding: 2px 10px;
    min-height: 22px;
    max-height: 26px;
    font-size: 12px;
}
QPushButton#TableDangerButton:hover {
    background-color: #4C252D;
}

/* Checkboxes */
QCheckBox {
    spacing: 8px;
    color: #E5E7EB;
    padding: 2px 4px;
    min-height: 22px;
}
QCheckBox::indicator {
    width: 17px;
    height: 17px;
    border-radius: 4px;
    border: 1px solid #525B6E;
    background-color: #161920;
}
QCheckBox::indicator:hover {
    border-color: #60CDFF;
}
QCheckBox::indicator:checked {
    background-color: #0078D4;
    border-color: #60CDFF;
}

/* Table */
QTableWidget {
    background-color: #161920;
    alternate-background-color: #1B1F28;
    border: 1px solid #2C3240;
    border-radius: 8px;
    gridline-color: #252A36;
    selection-background-color: #263147;
    selection-color: #FFFFFF;
}
QHeaderView::section {
    background-color: #21252E;
    color: #9CA3AF;
    font-weight: 600;
    font-size: 12px;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid #2E3442;
}

/* ProgressBar */
QProgressBar {
    background-color: #12151B;
    border: 1px solid #2E3442;
    border-radius: 6px;
    text-align: center;
    color: #F3F4F6;
    font-size: 11px;
    font-weight: 600;
    min-height: 18px;
    max-height: 20px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0078D4, stop:1 #60CDFF);
    border-radius: 5px;
}

/* Tabs */
QTabWidget::pane {
    border: 1px solid #2E3442;
    border-radius: 8px;
    background-color: #21252E;
    top: -1px;
    padding: 6px;
}
QTabBar::tab {
    background-color: #161920;
    color: #9CA3AF;
    border: 1px solid #2C3240;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 18px;
    margin-right: 4px;
    font-weight: 500;
}
QTabBar::tab:selected {
    background-color: #21252E;
    color: #60CDFF;
    border-bottom: 2px solid #60CDFF;
    font-weight: 600;
}
"""


MICA_LIGHT_QSS = """
/* === Windows 11 Light Mica Theme (浅色云母风格) === */
QMainWindow, QDialog {
    background-color: #F0F3F9;
    color: #1B1F24;
    font-family: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
}

QWidget {
    color: #1B1F24;
    font-family: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
}

QLabel, QCheckBox, QRadioButton {
    background-color: transparent;
}

/* Mica Elevated Card Surface (Layer 1) */
QFrame#CardFrame {
    background-color: #FFFFFF;
    border: 1px solid #E2E6F0;
    border-bottom: 1px solid #D5DBE8;
    border-radius: 10px;
}

/* Mica Secondary Surface (Layer 2 inside cards) */
QFrame#SubCardFrame {
    background-color: #F7F9FC;
    border: 1px solid #E5E9F2;
    border-radius: 8px;
}

QGroupBox {
    background-color: #FFFFFF;
    border: 1px solid #E2E6F0;
    border-radius: 10px;
    margin-top: 26px;
    padding: 18px 14px 14px 14px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    top: 2px;
    padding: 2px 8px;
    color: #0067C0;
    background-color: transparent;
    font-size: 13px;
    font-weight: 600;
}

/* Typography Tokens */
QLabel#AppTitle {
    font-size: 18px;
    font-weight: 700;
    color: #111827;
    padding: 1px 0px;
}
QLabel#AppSubtitle {
    font-size: 12px;
    color: #5E6675;
    padding: 1px 0px;
}
QLabel#VersionBadge {
    background-color: #EBF3FC;
    color: #0067C0;
    border: 1px solid #BFE0F8;
    border-radius: 8px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#SectionTitle {
    font-size: 14px;
    font-weight: 600;
    color: #111827;
    padding: 1px 0px;
}
QLabel#FieldLabel {
    font-size: 13px;
    font-weight: 500;
    color: #374151;
    padding-right: 4px;
}
QLabel#MutedLabel {
    font-size: 12px;
    color: #6B7280;
}
QLabel#StatPill {
    background-color: #F4F6FA;
    border: 1px solid #E1E6EF;
    border-radius: 6px;
    padding: 3px 10px;
    color: #4B5563;
    font-size: 12px;
}

/* Inputs & Dropdowns */
QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
    background-color: #FBFcfe;
    color: #111827;
    border: 1px solid #D0D7E2;
    border-bottom: 1px solid #9AA5B8;
    border-radius: 6px;
    padding: 5px 10px;
    min-height: 22px;
    selection-background-color: #0067C0;
    selection-color: #FFFFFF;
}
QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    background-color: #FFFFFF;
    border: 1px solid #0067C0;
    border-bottom: 2px solid #0067C0;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background-color: #FFFFFF;
    color: #111827;
    border: 1px solid #D0D7E2;
    selection-background-color: #EBF3FC;
    selection-color: #0067C0;
    padding: 4px;
}

QPlainTextEdit#DropInputEdit {
    border: 1px dashed #B0BAC9;
    border-radius: 8px;
    background-color: #F7F9FC;
    padding: 8px 10px;
    font-size: 13px;
}
QPlainTextEdit#DropInputEdit:focus {
    border: 1px dashed #0067C0;
    background-color: #FFFFFF;
}

QPlainTextEdit#ConsoleLogView {
    background-color: #181C24;
    color: #E2E8F0;
    border: 1px solid #CBD5E1;
    border-radius: 8px;
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 12px;
    padding: 8px;
}

/* Fluent Buttons */
QPushButton, QToolButton {
    background-color: #FBFcfe;
    color: #1F2937;
    border: 1px solid #D4DAE4;
    border-bottom: 1px solid #BFC7D5;
    border-radius: 6px;
    padding: 5px 14px;
    min-height: 22px;
    font-weight: 500;
}
QPushButton:hover, QToolButton:hover {
    background-color: #F3F6FA;
    border-color: #B8C2D1;
}
QPushButton:pressed, QToolButton:pressed {
    background-color: #E8ECF4;
    color: #4B5563;
}
QPushButton:disabled, QToolButton:disabled {
    background-color: #F3F4F6;
    color: #9CA3AF;
    border-color: #E5E7EB;
}

QPushButton#PrimaryButton {
    background-color: #0067C0;
    color: #FFFFFF;
    border: 1px solid #005BA8;
    border-bottom: 1px solid #004E92;
    font-weight: 600;
    font-size: 13px;
    padding: 6px 18px;
}
QPushButton#PrimaryButton:hover {
    background-color: #005BA8;
}
QPushButton#PrimaryButton:pressed {
    background-color: #004E92;
}
QPushButton#PrimaryButton:disabled {
    background-color: #93C5FD;
    color: #FFFFFF;
    border-color: #93C5FD;
}

QPushButton#DangerButton {
    background-color: #FEF2F2;
    color: #DC2626;
    border: 1px solid #FECACA;
}
QPushButton#DangerButton:hover {
    background-color: #FEE2E2;
    border-color: #FCA5A5;
}

QPushButton#GhostButton {
    background-color: transparent;
    border: 1px solid transparent;
    color: #4B5563;
    padding: 4px 10px;
}
QPushButton#GhostButton:hover {
    background-color: #E6EAF2;
    border-color: #D5DBE8;
    color: #111827;
}

/* Compact Inline Table Action Buttons */
QPushButton#TableActionButton {
    background-color: #FFFFFF;
    color: #1F2937;
    border: 1px solid #CBD5E1;
    border-radius: 5px;
    padding: 2px 10px;
    min-height: 22px;
    max-height: 26px;
    font-size: 12px;
}
QPushButton#TableActionButton:hover {
    background-color: #EBF3FC;
    border-color: #0067C0;
    color: #0067C0;
}
QPushButton#TableDangerButton {
    background-color: #FEF2F2;
    color: #DC2626;
    border: 1px solid #FECACA;
    border-radius: 5px;
    padding: 2px 10px;
    min-height: 22px;
    max-height: 26px;
    font-size: 12px;
}
QPushButton#TableDangerButton:hover {
    background-color: #FEE2E2;
}

/* Checkboxes */
QCheckBox {
    spacing: 8px;
    color: #1F2937;
    padding: 2px 4px;
    min-height: 22px;
}
QCheckBox::indicator {
    width: 17px;
    height: 17px;
    border-radius: 4px;
    border: 1px solid #8A94A6;
    background-color: #FFFFFF;
}
QCheckBox::indicator:hover {
    border-color: #0067C0;
}
QCheckBox::indicator:checked {
    background-color: #0067C0;
    border-color: #0067C0;
}

/* Table */
QTableWidget {
    background-color: #FFFFFF;
    alternate-background-color: #F8FAFC;
    border: 1px solid #E2E6F0;
    border-radius: 8px;
    gridline-color: #EEF2F7;
    selection-background-color: #EBF3FC;
    selection-color: #111827;
}
QHeaderView::section {
    background-color: #F7F9FC;
    color: #4B5563;
    font-weight: 600;
    font-size: 12px;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid #E2E6F0;
}

/* ProgressBar */
QProgressBar {
    background-color: #E6EAF2;
    border: 1px solid #D0D7E2;
    border-radius: 6px;
    text-align: center;
    color: #111827;
    font-size: 11px;
    font-weight: 600;
    min-height: 18px;
    max-height: 20px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0067C0, stop:1 #0099BC);
    border-radius: 5px;
}

/* Tabs */
QTabWidget::pane {
    border: 1px solid #E2E6F0;
    border-radius: 8px;
    background-color: #FFFFFF;
    top: -1px;
    padding: 6px;
}
QTabBar::tab {
    background-color: #F0F3F9;
    color: #5E6675;
    border: 1px solid #DCE2EC;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 18px;
    margin-right: 4px;
    font-weight: 500;
}
QTabBar::tab:selected {
    background-color: #FFFFFF;
    color: #0067C0;
    border-bottom: 2px solid #0067C0;
    font-weight: 600;
}
"""


def apply_theme(
    app: Optional[QApplication],
    theme_pref: str,
    window: Optional[QWidget] = None,
) -> str:
    """Apply the Windows 11 Mica Dark or Light QSS and DWM backdrop, returning active theme."""
    effective = resolve_effective_theme(theme_pref)
    is_dark = effective == "dark"
    if app is not None:
        app.setStyleSheet(MICA_DARK_QSS if is_dark else MICA_LIGHT_QSS)
    if window is not None:
        apply_win11_mica_window_attributes(window, is_dark=is_dark)
    return effective
