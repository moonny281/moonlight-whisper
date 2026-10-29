"""Modern open-source UI design system (Moonlight Dark & Crisp Light themes) for PySide6."""

from __future__ import annotations

from typing import Optional
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication


def resolve_effective_theme(theme_pref: str) -> str:
    """Resolve `'system'` to either `'dark'` or `'light'` based on OS color scheme."""
    if theme_pref in ("dark", "light"):
        return theme_pref
    try:
        hints = QGuiApplication.styleHints()
        scheme = hints.colorScheme()
        # Qt.ColorScheme.Dark == 2
        if int(getattr(scheme, "value", scheme)) == 2:
            return "dark"
        if int(getattr(scheme, "value", scheme)) == 1:
            return "light"
    except Exception:
        pass
    return "dark"


DARK_QSS = """
/* === Moonlight Dark Theme (Modern Open-Source Studio) === */
QMainWindow, QDialog {
    background-color: #0B1120;
    color: #E2E8F0;
    font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif;
    font-size: 13px;
}
QWidget {
    color: #E2E8F0;
    font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif;
    font-size: 13px;
}

/* Card Containers */
QFrame#CardFrame, QGroupBox {
    background-color: #131C31;
    border: 1px solid #1E293B;
    border-radius: 10px;
}
QGroupBox {
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
    color: #F8FAFC;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: #94A3B8;
    font-size: 12px;
}

/* Header & Typography */
QLabel#AppTitle {
    font-size: 18px;
    font-weight: 700;
    color: #F8FAFC;
}
QLabel#AppSubtitle {
    font-size: 12px;
    color: #94A3B8;
}
QLabel#VersionBadge {
    background-color: #1E293B;
    color: #F5D076;
    border: 1px solid #334155;
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#SectionTitle {
    font-size: 13px;
    font-weight: 600;
    color: #F1F5F9;
}
QLabel#MutedLabel {
    font-size: 12px;
    color: #64748B;
}
QLabel#StatPill {
    background-color: #0F172A;
    border: 1px solid #1E293B;
    border-radius: 6px;
    padding: 3px 9px;
    color: #94A3B8;
    font-size: 12px;
}

/* Inputs & Text Editors */
QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
    background-color: #0F172A;
    color: #F1F5F9;
    border: 1px solid #26334D;
    border-radius: 7px;
    padding: 6px 10px;
    selection-background-color: #4F46E5;
}
QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #6366F1;
    background-color: #0B1325;
}
QPlainTextEdit#DropInputEdit {
    border: 1px dashed #334155;
    border-radius: 8px;
    background-color: #0B1120;
    padding: 8px;
    font-size: 13px;
}
QPlainTextEdit#DropInputEdit:focus {
    border: 1px dashed #6366F1;
}
QPlainTextEdit#ConsoleLogView {
    background-color: #070B14;
    color: #A5B4FC;
    border: 1px solid #1E293B;
    border-radius: 8px;
    font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", monospace;
    font-size: 12px;
    padding: 8px;
}

/* Buttons */
QPushButton, QToolButton {
    background-color: #1E293B;
    color: #E2E8F0;
    border: 1px solid #334155;
    border-radius: 7px;
    padding: 6px 13px;
    font-weight: 500;
}
QPushButton:hover, QToolButton:hover {
    background-color: #283548;
    border-color: #475569;
}
QPushButton:pressed, QToolButton:pressed {
    background-color: #0F172A;
}
QPushButton:disabled, QToolButton:disabled {
    background-color: #0F172A;
    color: #475569;
    border-color: #1E293B;
}
QPushButton#PrimaryButton {
    background-color: #4F46E5;
    color: #FFFFFF;
    border: 1px solid #6366F1;
    font-weight: 600;
    padding: 7px 18px;
}
QPushButton#PrimaryButton:hover {
    background-color: #6366F1;
    border-color: #818CF8;
}
QPushButton#PrimaryButton:pressed {
    background-color: #4338CA;
}
QPushButton#DangerButton {
    background-color: #2A1621;
    color: #FCA5A5;
    border: 1px solid #7F1D1D;
}
QPushButton#DangerButton:hover {
    background-color: #451A2B;
    border-color: #991B1B;
}
QPushButton#GhostButton {
    background-color: transparent;
    border: 1px solid transparent;
    color: #94A3B8;
    padding: 5px 10px;
}
QPushButton#GhostButton:hover {
    background-color: #1E293B;
    color: #F8FAFC;
    border-color: #334155;
}

/* Checkboxes */
QCheckBox {
    spacing: 7px;
    color: #CBD5E1;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #475569;
    background-color: #0F172A;
}
QCheckBox::indicator:hover {
    border-color: #6366F1;
}
QCheckBox::indicator:checked {
    background-color: #4F46E5;
    border-color: #6366F1;
}

/* Table */
QTableWidget {
    background-color: #0F172A;
    alternate-background-color: #131C31;
    border: 1px solid #1E293B;
    border-radius: 8px;
    gridline-color: #1E293B;
    selection-background-color: #1E293B;
    selection-color: #F8FAFC;
}
QHeaderView::section {
    background-color: #131C31;
    color: #94A3B8;
    font-weight: 600;
    font-size: 12px;
    padding: 7px 10px;
    border: none;
    border-bottom: 1px solid #1E293B;
}

/* ProgressBar */
QProgressBar {
    background-color: #0B1120;
    border: 1px solid #26334D;
    border-radius: 6px;
    text-align: center;
    color: #E2E8F0;
    font-size: 11px;
    font-weight: 600;
    min-height: 18px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4F46E5, stop:1 #06B6D4);
    border-radius: 5px;
}

/* Tabs */
QTabWidget::pane {
    border: 1px solid #1E293B;
    border-radius: 8px;
    background-color: #131C31;
    top: -1px;
}
QTabBar::tab {
    background-color: #0F172A;
    color: #94A3B8;
    border: 1px solid #1E293B;
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 8px 16px;
    margin-right: 4px;
    font-weight: 500;
}
QTabBar::tab:selected {
    background-color: #131C31;
    color: #F8FAFC;
    border-bottom: 2px solid #6366F1;
    font-weight: 600;
}
"""

LIGHT_QSS = """
/* === Crisp Light Theme (Modern Open-Source Studio) === */
QMainWindow, QDialog {
    background-color: #F1F5F9;
    color: #0F172A;
    font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif;
    font-size: 13px;
}
QWidget {
    color: #0F172A;
    font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif;
    font-size: 13px;
}

QFrame#CardFrame, QGroupBox {
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 10px;
}
QGroupBox {
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
    color: #0F172A;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: #475569;
    font-size: 12px;
}

QLabel#AppTitle {
    font-size: 18px;
    font-weight: 700;
    color: #0F172A;
}
QLabel#AppSubtitle {
    font-size: 12px;
    color: #64748B;
}
QLabel#VersionBadge {
    background-color: #EEF2FF;
    color: #4338CA;
    border: 1px solid #C7D2FE;
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#SectionTitle {
    font-size: 13px;
    font-weight: 600;
    color: #1E293B;
}
QLabel#MutedLabel {
    font-size: 12px;
    color: #64748B;
}
QLabel#StatPill {
    background-color: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    padding: 3px 9px;
    color: #475569;
    font-size: 12px;
}

QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
    background-color: #F8FAFC;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    border-radius: 7px;
    padding: 6px 10px;
    selection-background-color: #4F46E5;
    selection-color: #FFFFFF;
}
QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #4F46E5;
    background-color: #FFFFFF;
}
QPlainTextEdit#DropInputEdit {
    border: 1px dashed #94A3B8;
    border-radius: 8px;
    background-color: #F8FAFC;
    padding: 8px;
}
QPlainTextEdit#DropInputEdit:focus {
    border: 1px dashed #4F46E5;
    background-color: #FFFFFF;
}
QPlainTextEdit#ConsoleLogView {
    background-color: #0F172A;
    color: #E2E8F0;
    border: 1px solid #CBD5E1;
    border-radius: 8px;
    font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", monospace;
    font-size: 12px;
    padding: 8px;
}

QPushButton, QToolButton {
    background-color: #FFFFFF;
    color: #1E293B;
    border: 1px solid #CBD5E1;
    border-radius: 7px;
    padding: 6px 13px;
    font-weight: 500;
}
QPushButton:hover, QToolButton:hover {
    background-color: #F8FAFC;
    border-color: #94A3B8;
}
QPushButton:pressed, QToolButton:pressed {
    background-color: #E2E8F0;
}
QPushButton:disabled, QToolButton:disabled {
    background-color: #F1F5F9;
    color: #94A3B8;
    border-color: #E2E8F0;
}
QPushButton#PrimaryButton {
    background-color: #4F46E5;
    color: #FFFFFF;
    border: 1px solid #4338CA;
    font-weight: 600;
    padding: 7px 18px;
}
QPushButton#PrimaryButton:hover {
    background-color: #4338CA;
}
QPushButton#DangerButton {
    background-color: #FEF2F2;
    color: #DC2626;
    border: 1px solid #FECACA;
}
QPushButton#DangerButton:hover {
    background-color: #FEE2E2;
}
QPushButton#GhostButton {
    background-color: transparent;
    border: 1px solid transparent;
    color: #475569;
    padding: 5px 10px;
}
QPushButton#GhostButton:hover {
    background-color: #E2E8F0;
    color: #0F172A;
}

QCheckBox {
    spacing: 7px;
    color: #334155;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #94A3B8;
    background-color: #FFFFFF;
}
QCheckBox::indicator:checked {
    background-color: #4F46E5;
    border-color: #4F46E5;
}

QTableWidget {
    background-color: #FFFFFF;
    alternate-background-color: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    gridline-color: #F1F5F9;
    selection-background-color: #EEF2FF;
    selection-color: #1E293B;
}
QHeaderView::section {
    background-color: #F8FAFC;
    color: #475569;
    font-weight: 600;
    font-size: 12px;
    padding: 7px 10px;
    border: none;
    border-bottom: 1px solid #E2E8F0;
}

QProgressBar {
    background-color: #E2E8F0;
    border: 1px solid #CBD5E1;
    border-radius: 6px;
    text-align: center;
    color: #0F172A;
    font-size: 11px;
    font-weight: 600;
    min-height: 18px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4F46E5, stop:1 #0EA5E9);
    border-radius: 5px;
}

QTabWidget::pane {
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    background-color: #FFFFFF;
    top: -1px;
}
QTabBar::tab {
    background-color: #F1F5F9;
    color: #64748B;
    border: 1px solid #E2E8F0;
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 8px 16px;
    margin-right: 4px;
    font-weight: 500;
}
QTabBar::tab:selected {
    background-color: #FFFFFF;
    color: #4F46E5;
    border-bottom: 2px solid #4F46E5;
    font-weight: 600;
}
"""


def apply_theme(app: Optional[QApplication], theme_pref: str) -> str:
    """Apply the modern Moonlight Dark or Crisp Light QSS to `app` and return the active theme name."""
    effective = resolve_effective_theme(theme_pref)
    if app is not None:
        app.setStyleSheet(DARK_QSS if effective == "dark" else LIGHT_QSS)
    return effective
