"""Moonlit Mica widgets (v1.4.5): background, moon mark, animated switch, cards, drag-drop input, GPU pill, task queue."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    Property,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.gpu_runtime import GpuInfo, GpuStatus
from app.ui import theme
from app.workers import TaskItem, TaskStatus

SUPPORTED_MEDIA_EXTS = {
    ".mp3", ".m4a", ".mp4", ".mkv", ".wav", ".flac", ".webm", ".mov", ".ogg", ".aac", ".wma", ".avi",
}


def repolish(widget: QWidget) -> None:
    """Re-evaluate QSS after a dynamic property (e.g. `tone`, `dragActive`) changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
        int(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def make_card_frame(parent: Optional[QWidget] = None, sub_card: bool = False) -> QFrame:
    """Create a styled Mica rounded card container."""
    frame = QFrame(parent)
    frame.setObjectName("SubCardFrame" if sub_card else "CardFrame")
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    return frame


def make_card_header(
    title: str,
    caption: str = "",
    trailing: Sequence[QWidget] = (),
) -> QHBoxLayout:
    """Header row of a card: title (+ optional caption underneath) on the left, `trailing` widgets on the right."""
    row = QHBoxLayout()
    row.setSpacing(10)
    col = QVBoxLayout()
    col.setSpacing(2)
    lbl_title = QLabel(title)
    lbl_title.setObjectName("SectionTitle")
    col.addWidget(lbl_title)
    if caption:
        lbl_cap = QLabel(caption)
        lbl_cap.setObjectName("CardCaption")
        lbl_cap.setWordWrap(True)
        col.addWidget(lbl_cap)
    row.addLayout(col, 1)
    for w in trailing:
        row.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


def make_mica_section(
    title: str,
    subtitle: str = "",
    parent: Optional[QWidget] = None,
) -> Tuple[QFrame, QVBoxLayout]:
    """Create a Mica card with a header and return `(card, content_layout)`."""
    card = make_card_frame(parent)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    layout.addLayout(make_card_header(title, subtitle))
    return card, layout


# --------------------------------------------------------------------------------------
# Custom painted widgets
# --------------------------------------------------------------------------------------


class MicaBackground(QWidget):
    """Window backdrop: tinted vertical gradient plus two soft halos that give the frosted cards something to refract."""

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        w, h = float(rect.width()), float(rect.height())

        base = QLinearGradient(0, 0, 0, h)
        base.setColorAt(0.0, theme.qcolor(pal.bg_top))
        base.setColorAt(1.0, theme.qcolor(pal.bg_bottom))
        p.fillRect(rect, base)

        # Moonlight: warm halo behind the brand, top-left.
        gold = theme.qcolor(pal.halo_gold)
        g1 = QRadialGradient(QPointF(w * 0.10, -h * 0.04), max(w, h) * 0.52)
        g1.setColorAt(0.0, gold)
        faded = QColor(gold)
        faded.setAlpha(0)
        g1.setColorAt(1.0, faded)
        p.fillRect(rect, g1)

        # Night sky: cool halo bottom-right.
        blue = theme.qcolor(pal.halo_blue)
        g2 = QRadialGradient(QPointF(w * 0.98, h * 1.02), max(w, h) * 0.60)
        g2.setColorAt(0.0, blue)
        faded_b = QColor(blue)
        faded_b.setAlpha(0)
        g2.setColorAt(1.0, faded_b)
        p.fillRect(rect, g2)
        p.end()


class MoonMark(QWidget):
    """Painted crescent moon with a soft glow — the brand mark (no bitmap needed, crisp at any DPI)."""

    def __init__(self, size: int = 44, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.size()

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = float(min(self.width(), self.height()))
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        radius = s * 0.30
        gold = theme.qcolor(pal.gold)

        glow = QRadialGradient(c, s * 0.52)
        glow_c = QColor(gold)
        glow_c.setAlpha(110 if pal.is_dark else 80)
        glow.setColorAt(0.0, glow_c)
        glow_end = QColor(gold)
        glow_end.setAlpha(0)
        glow.setColorAt(1.0, glow_end)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(c, s * 0.5, s * 0.5)

        outer = QPainterPath()
        outer.addEllipse(c, radius, radius)
        inner = QPainterPath()
        inner.addEllipse(QPointF(c.x() + radius * 0.46, c.y() - radius * 0.16), radius * 0.86, radius * 0.86)
        crescent = outer.subtracted(inner)

        fill = QLinearGradient(c.x() - radius, c.y() - radius, c.x() + radius * 0.4, c.y() + radius)
        fill.setColorAt(0.0, QColor("#FFF6DC"))
        fill.setColorAt(1.0, gold)
        p.setBrush(QBrush(fill))
        p.drawPath(crescent)

        star = QColor("#FFFFFF")
        star.setAlpha(230 if pal.is_dark else 190)
        p.setBrush(star)
        p.drawEllipse(QPointF(c.x() + radius * 0.92, c.y() - radius * 0.78), s * 0.034, s * 0.034)
        p.end()


class SwitchCheckBox(QCheckBox):
    """Drop-in `QCheckBox` painted as an animated pill switch (same API: `toggled`, `isChecked`, `setChecked`)."""

    TRACK_W = 36
    TRACK_H = 20
    GAP = 10

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._pos = 1.0 if self.isChecked() else 0.0
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._on_toggled)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # animated property -------------------------------------------------------------------
    def _get_pos(self) -> float:
        return self._pos

    def _set_pos(self, value: float) -> None:
        self._pos = float(value)
        self.update()

    knobPos = Property(float, _get_pos, _set_pos)

    def _on_toggled(self, checked: bool) -> None:
        end = 1.0 if checked else 0.0
        if not self.isVisible():
            self._set_pos(end)
            return
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(end)
        self._anim.start()

    # geometry -----------------------------------------------------------------------------
    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self.text()) if self.text() else 0
        w = self.TRACK_W + (self.GAP + text_w + 4 if text_w else 4)
        return QSize(w, max(30, fm.height() + 12))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    # painting -----------------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        pal = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        enabled = self.isEnabled()
        t = self._pos

        track = QRectF(2.0, (self.height() - self.TRACK_H) / 2.0, float(self.TRACK_W), float(self.TRACK_H))
        fill = _blend(theme.qcolor(pal.switch_off), theme.qcolor(pal.accent_fill), t)
        edge = _blend(theme.qcolor(pal.switch_off_border), theme.qcolor(pal.accent_fill), t)
        if self.underMouse() and enabled:
            fill = _blend(fill, QColor(255, 255, 255, fill.alpha()), 0.12)
        p.setOpacity(1.0 if enabled else 0.45)
        p.setPen(QPen(edge, 1.0))
        p.setBrush(fill)
        p.drawRoundedRect(track, self.TRACK_H / 2.0, self.TRACK_H / 2.0)

        knob_r = (self.TRACK_H - 6) / 2.0
        travel = self.TRACK_W - self.TRACK_H
        cx = track.left() + self.TRACK_H / 2.0 + travel * t
        knob = QColor("#FFFFFF")
        if t < 0.5 and pal.is_dark:
            knob = QColor("#D5DAEE")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(knob)
        p.drawEllipse(QPointF(cx, track.center().y()), knob_r, knob_r)

        if self.hasFocus() and self.focusPolicy() != Qt.FocusPolicy.NoFocus:
            ring = theme.qcolor(pal.accent)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(ring, 1.6))
            p.drawRoundedRect(track.adjusted(-2.5, -2.5, 2.5, 2.5), self.TRACK_H / 2.0 + 2.5, self.TRACK_H / 2.0 + 2.5)

        if self.text():
            p.setOpacity(1.0)
            p.setPen(theme.qcolor(pal.text if enabled else pal.text_faint))
            text_rect = QRectF(self.TRACK_W + self.GAP + 2.0, 0.0, max(0.0, self.width() - self.TRACK_W - self.GAP - 2.0), float(self.height()))
            elided = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, int(text_rect.width()))
            p.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), elided)
        p.end()


class ProgressCell(QWidget):
    """Thin gold-accent progress bar with a right-aligned percentage, used in the queue table."""

    def __init__(self, value: int = 0, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setObjectName("ThinProgress")
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.lbl = QLabel("0%")
        self.lbl.setObjectName("MutedLabel")
        self.lbl.setFixedWidth(38)
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.bar, 1, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.lbl)
        self.setValue(value)

    def setValue(self, value: int) -> None:  # noqa: N802
        v = max(0, min(100, int(value)))
        self.bar.setValue(v)
        self.lbl.setText(f"{v}%")


def open_path_in_os(path: Path, reveal_in_folder: bool = False) -> None:
    """Open a file or reveal it in the native file manager."""
    resolved = path.resolve()
    if not resolved.exists():
        return
    if sys.platform == "win32":
        if reveal_in_folder and resolved.is_file():
            subprocess.Popen(["explorer", "/select,", str(resolved)])
        else:
            os.startfile(str(resolved))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        if reveal_in_folder and resolved.is_file():
            subprocess.Popen(["open", "-R", str(resolved)])
        else:
            subprocess.Popen(["open", str(resolved)])
    else:
        target = resolved.parent if (reveal_in_folder and resolved.is_file()) else resolved
        subprocess.Popen(["xdg-open", str(target)])


class DropInputTextEdit(QPlainTextEdit):
    """Multi-line link input editor supporting drag-and-drop of URLs and local audio/video files."""

    files_dropped = Signal(list)  # List[str]
    line_count_changed = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("DropInputEdit")
        self.setAcceptDrops(True)
        self.setPlaceholderText(
            "粘贴视频链接，每行一个（YouTube、Bilibili、播客等）。\n也可以把本地音视频文件或文件夹拖到这里。"
        )
        self.setMinimumHeight(96)
        self.setMaximumHeight(150)
        self.textChanged.connect(self._emit_line_count)

    def _emit_line_count(self) -> None:
        lines = [line.strip() for line in self.toPlainText().splitlines() if line.strip()]
        self.line_count_changed.emit(len(lines))

    def set_drag_active(self, active: bool) -> None:
        if bool(self.property("dragActive")) != active:
            self.setProperty("dragActive", active)
            repolish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasText():
            self.set_drag_active(True)
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self.set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.set_drag_active(False)
        mime = event.mimeData()
        added_lines: List[str] = []
        if mime.hasUrls():
            for qurl in mime.urls():
                if qurl.isLocalFile():
                    local_p = Path(qurl.toLocalFile())
                    if local_p.is_file() and local_p.suffix.lower() in SUPPORTED_MEDIA_EXTS:
                        added_lines.append(str(local_p))
                    elif local_p.is_dir():
                        for child in sorted(local_p.iterdir()):
                            if child.is_file() and child.suffix.lower() in SUPPORTED_MEDIA_EXTS:
                                added_lines.append(str(child))
                else:
                    added_lines.append(qurl.toString())
        elif mime.hasText():
            for line in mime.text().splitlines():
                if line.strip():
                    added_lines.append(line.strip())

        if added_lines:
            existing = self.toPlainText().strip()
            combined = (existing + "\n" + "\n".join(added_lines)).strip() if existing else "\n".join(added_lines)
            self.setPlainText(combined)
            self.files_dropped.emit(added_lines)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class GpuStatusIndicator(QWidget):
    """Status pill showing GPU readiness (tone is themed through QSS) and a one-click install/repair button."""

    install_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.badge_label = QLabel("GPU 未检测")
        self.badge_label.setObjectName("GpuPill")
        self._set_tone("idle")
        self.action_btn = QPushButton("安装运行库")
        self.action_btn.setObjectName("TableActionButton")
        self.action_btn.clicked.connect(self.install_clicked.emit)

        layout.addWidget(self.badge_label)
        layout.addWidget(self.action_btn)

    def _set_tone(self, tone: str) -> None:
        self.badge_label.setProperty("tone", tone)
        repolish(self.badge_label)

    def update_from_info(self, info: GpuInfo) -> None:
        if info.status == GpuStatus.READY:
            short_dev = info.device_name.replace("NVIDIA GeForce ", "").strip() or "CUDA"
            self.badge_label.setText(f"●  {short_dev} ({info.vram_gb:.0f}GB) 就绪")
            self._set_tone("ok")
            self.action_btn.setVisible(False)
        elif info.status == GpuStatus.RUNTIME_MISSING:
            self.badge_label.setText("●  缺少 CUDA 运行库")
            self._set_tone("warn")
            self.action_btn.setText("一键安装")
            self.action_btn.setVisible(True)
        elif info.status == GpuStatus.LOAD_FAILED:
            self.badge_label.setText("●  运行库加载异常")
            self._set_tone("warn")
            self.action_btn.setText("修复运行库")
            self.action_btn.setVisible(True)
        elif info.status == GpuStatus.DRIVER_TOO_OLD:
            self.badge_label.setText(f"●  驱动过旧 ({info.driver_version})")
            self._set_tone("error")
            self.action_btn.setVisible(False)
        else:
            self.badge_label.setText("○  CPU 模式")
            self._set_tone("idle")
            self.action_btn.setVisible(False)
        self.setToolTip(info.message + (f"\n{info.detail}" if info.detail else ""))


class TaskQueueTable(QTableWidget):
    """Task queue table: flat rows, thin progress bars, colour-coded stage and inline actions, friendly empty state."""

    retry_requested = Signal(str)       # job_id
    cancel_requested = Signal(str)      # job_id

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["视频标题 / 输入源", "当前阶段", "进度", "快捷操作"])
        header = self.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(1, 190)
        self.setColumnWidth(2, 190)
        self.setColumnWidth(3, 170)

        self.setMinimumHeight(170)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(52)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.customContextMenuRequested.connect(self._show_context_menu)
        self.cellDoubleClicked.connect(self._on_double_clicked)

        self._row_by_job_id: Dict[str, int] = {}
        self._items_by_job_id: Dict[str, TaskItem] = {}

    def add_task_row(self, item: TaskItem) -> None:
        row = self.rowCount()
        self.insertRow(row)
        self.setRowHeight(row, 52)
        job_id = item.job.job_id
        self._row_by_job_id[job_id] = row
        self._items_by_job_id[job_id] = item

        title_item = QTableWidgetItem(item.display_title)
        title_item.setData(Qt.ItemDataRole.UserRole, job_id)
        title_item.setToolTip(item.job.source)
        self.setItem(row, 0, title_item)

        status_item = QTableWidgetItem(item.stage_text)
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setItem(row, 1, status_item)

        self.setCellWidget(row, 2, ProgressCell(item.progress))

        self._refresh_row_appearance(row, item)

    def update_task_row(self, item: TaskItem) -> None:
        job_id = item.job.job_id
        self._items_by_job_id[job_id] = item
        row = self._row_by_job_id.get(job_id)
        if row is None or row >= self.rowCount():
            return
        self._refresh_row_appearance(row, item)

    def _refresh_row_appearance(self, row: int, item: TaskItem) -> None:
        title_cell = self.item(row, 0)
        if title_cell:
            title_cell.setText(item.display_title)
            if item.result and item.result.md_path:
                title_cell.setToolTip(f"双击打开：{item.result.md_path}")

        status_cell = self.item(row, 1)
        if status_cell:
            pal = theme.palette()
            status_cell.setText(f"●  {item.stage_text}")
            if item.status == TaskStatus.COMPLETED:
                color = pal.success
            elif item.status == TaskStatus.RUNNING:
                color = pal.accent
            elif item.status == TaskStatus.FAILED:
                color = pal.danger
            elif item.status == TaskStatus.CANCELLED:
                color = pal.warning
            else:
                color = pal.text_muted
            status_cell.setForeground(QBrush(theme.qcolor(color)))

            if item.error_info:
                tip = item.error_info.user_message
                if item.error_info.recoverable_hint:
                    tip += f"\n建议：{item.error_info.recoverable_hint}"
                status_cell.setToolTip(tip)

        progress_cell = self.cellWidget(row, 2)
        if isinstance(progress_cell, ProgressCell):
            progress_cell.setValue(item.progress)

        action_wrap = QWidget()
        action_layout = QHBoxLayout(action_wrap)
        action_layout.setContentsMargins(6, 0, 6, 0)
        action_layout.setSpacing(6)
        action_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        job_id = item.job.job_id
        if item.status == TaskStatus.COMPLETED and item.result and item.result.md_path.exists():
            md_path = item.result.md_path
            btn_open = QPushButton("打开")
            btn_open.setObjectName("TableActionButton")
            btn_open.clicked.connect(lambda _=False, p=md_path: open_path_in_os(p, False))
            btn_dir = QPushButton("文件夹")
            btn_dir.setObjectName("TableActionButton")
            btn_dir.clicked.connect(lambda _=False, p=md_path: open_path_in_os(p, True))
            action_layout.addWidget(btn_open)
            action_layout.addWidget(btn_dir)
        elif item.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            btn_retry = QPushButton("重试")
            btn_retry.setObjectName("TableActionButton")
            btn_retry.clicked.connect(lambda _=False, j=job_id: self.retry_requested.emit(j))
            action_layout.addWidget(btn_retry)
        elif item.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
            btn_cancel = QPushButton("取消")
            btn_cancel.setObjectName("TableDangerButton")
            btn_cancel.clicked.connect(lambda _=False, j=job_id: self.cancel_requested.emit(j))
            action_layout.addWidget(btn_cancel)

        self.setCellWidget(row, 3, action_wrap)

    def refresh_theme(self) -> None:
        """Re-apply palette-dependent colours (stage text) after a theme switch."""
        for jid, row in list(self._row_by_job_id.items()):
            item = self._items_by_job_id.get(jid)
            if item is not None and 0 <= row < self.rowCount():
                self._refresh_row_appearance(row, item)
        self.viewport().update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.rowCount() > 0:
            return
        pal = theme.palette()
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.viewport().rect()
        base_px = p.font().pixelSize()
        if base_px <= 0:
            base_px = 13
        title_font = QFont(p.font())
        title_font.setPixelSize(base_px + 2)
        title_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(title_font)
        p.setPen(theme.qcolor(pal.text_muted))
        top = QRectF(rect.left(), rect.center().y() - 32, rect.width(), 28)
        p.drawText(top, int(Qt.AlignmentFlag.AlignCenter), "队列是空的")
        body_font = QFont(p.font())
        body_font.setPixelSize(max(11, base_px - 1))
        body_font.setWeight(QFont.Weight.Normal)
        p.setFont(body_font)
        p.setPen(theme.qcolor(pal.text_faint))
        bottom = QRectF(rect.left(), rect.center().y() + 0, rect.width(), 24)
        p.drawText(bottom, int(Qt.AlignmentFlag.AlignCenter), "在上方粘贴链接或拖入文件，然后点击“开始转录”")
        p.end()

    def remove_tasks(self, job_ids: List[str]) -> None:
        """Remove rows for `job_ids` and rebuild `_row_by_job_id` index mapping."""
        remove_set = set(job_ids)
        for r in range(self.rowCount() - 1, -1, -1):
            jid = self._job_id_at_row(r)
            if jid in remove_set:
                self.removeRow(r)
                self._items_by_job_id.pop(jid, None)

        self._row_by_job_id.clear()
        for r in range(self.rowCount()):
            jid = self._job_id_at_row(r)
            if jid:
                self._row_by_job_id[jid] = r

    def _job_id_at_row(self, row: int) -> Optional[str]:
        if row < 0 or row >= self.rowCount():
            return None
        cell = self.item(row, 0)
        return str(cell.data(Qt.ItemDataRole.UserRole)) if cell else None

    def _on_double_clicked(self, row: int, _col: int) -> None:
        job_id = self._job_id_at_row(row)
        if not job_id:
            return
        item = self._items_by_job_id.get(job_id)
        if item and item.result and item.result.md_path.exists():
            open_path_in_os(item.result.md_path, reveal_in_folder=False)

    def _show_context_menu(self, pos) -> None:
        row = self.indexAt(pos).row()
        job_id = self._job_id_at_row(row)
        if not job_id:
            return
        item = self._items_by_job_id.get(job_id)
        if not item:
            return

        menu = QMenu(self)
        if item.result and item.result.md_path.exists():
            act_open = QAction("打开 Markdown 文档", self)
            act_open.triggered.connect(lambda: open_path_in_os(item.result.md_path, False))
            menu.addAction(act_open)

            act_reveal = QAction("在文件夹中显示", self)
            act_reveal.triggered.connect(lambda: open_path_in_os(item.result.md_path, True))
            menu.addAction(act_reveal)

        if item.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            act_retry = QAction("重试该任务", self)
            act_retry.triggered.connect(lambda: self.retry_requested.emit(job_id))
            menu.addAction(act_retry)

        if item.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
            act_cancel = QAction("取消该任务", self)
            act_cancel.triggered.connect(lambda: self.cancel_requested.emit(job_id))
            menu.addAction(act_cancel)

        if not menu.isEmpty():
            menu.exec(self.viewport().mapToGlobal(pos))
