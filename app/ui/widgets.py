"""Modern reusable PySide6 widgets: drag-drop input editor, GPU pill badge, and 4-column task queue table."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QWidget,
)

from app.core.gpu_runtime import GpuInfo, GpuStatus
from app.workers import TaskItem, TaskStatus

SUPPORTED_MEDIA_EXTS = {
    ".mp3", ".m4a", ".mp4", ".mkv", ".wav", ".flac", ".webm", ".mov", ".ogg", ".aac", ".wma", ".avi",
}


def make_card_frame(parent: Optional[QWidget] = None) -> QFrame:
    """Create a styled rounded card container (`QFrame#CardFrame`)."""
    frame = QFrame(parent)
    frame.setObjectName("CardFrame")
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    return frame


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
            "在此粘贴视频链接（每行一个，支持 YouTube / Bilibili / 播客等），或将本地音视频文件/文件夹直接拖入窗口任意位置..."
        )
        self.setFixedHeight(88)
        self.textChanged.connect(self._emit_line_count)

    def _emit_line_count(self) -> None:
        lines = [line.strip() for line in self.toPlainText().splitlines() if line.strip()]
        self.line_count_changed.emit(len(lines))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasText():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
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
    """Modern pill badge displaying GPU readiness and a one-click install/repair button."""

    install_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.badge_label = QLabel("○ GPU 未检测")
        self.badge_label.setStyleSheet(
            "background-color: rgba(148, 163, 184, 0.15); color: #94A3B8; "
            "border: 1px solid rgba(148, 163, 184, 0.3); border-radius: 10px; padding: 3px 10px; font-size: 12px;"
        )
        self.action_btn = QPushButton("安装运行库")
        self.action_btn.setFixedHeight(26)
        self.action_btn.clicked.connect(self.install_clicked.emit)

        layout.addWidget(self.badge_label)
        layout.addWidget(self.action_btn)

    def update_from_info(self, info: GpuInfo) -> None:
        if info.status == GpuStatus.READY:
            short_dev = info.device_name.replace("NVIDIA GeForce ", "").strip() or "CUDA"
            self.badge_label.setText(f"● {short_dev} ({info.vram_gb:.0f}GB) 就绪")
            self.badge_label.setStyleSheet(
                "background-color: rgba(16, 185, 129, 0.15); color: #34D399; "
                "border: 1px solid rgba(16, 185, 129, 0.4); border-radius: 10px; padding: 3px 10px; font-weight: 600; font-size: 12px;"
            )
            self.action_btn.setVisible(False)
        elif info.status == GpuStatus.RUNTIME_MISSING:
            self.badge_label.setText("○ 缺少 CUDA 运行库")
            self.badge_label.setStyleSheet(
                "background-color: rgba(245, 158, 11, 0.15); color: #FBBF24; "
                "border: 1px solid rgba(245, 158, 11, 0.4); border-radius: 10px; padding: 3px 10px; font-weight: 600; font-size: 12px;"
            )
            self.action_btn.setText("一键安装")
            self.action_btn.setVisible(True)
        elif info.status == GpuStatus.LOAD_FAILED:
            self.badge_label.setText("● 运行库加载异常")
            self.badge_label.setStyleSheet(
                "background-color: rgba(249, 115, 22, 0.15); color: #FB923C; "
                "border: 1px solid rgba(249, 115, 22, 0.4); border-radius: 10px; padding: 3px 10px; font-weight: 600; font-size: 12px;"
            )
            self.action_btn.setText("修复运行库")
            self.action_btn.setVisible(True)
        elif info.status == GpuStatus.DRIVER_TOO_OLD:
            self.badge_label.setText(f"● 驱动过旧 ({info.driver_version})")
            self.badge_label.setStyleSheet(
                "background-color: rgba(239, 68, 68, 0.15); color: #F87171; "
                "border: 1px solid rgba(239, 68, 68, 0.4); border-radius: 10px; padding: 3px 10px; font-weight: 600; font-size: 12px;"
            )
            self.action_btn.setVisible(False)
        else:
            self.badge_label.setText("○ CPU 模式 (未检测到 N 卡)")
            self.badge_label.setStyleSheet(
                "background-color: rgba(148, 163, 184, 0.12); color: #94A3B8; "
                "border: 1px solid rgba(148, 163, 184, 0.25); border-radius: 10px; padding: 3px 10px; font-size: 12px;"
            )
            self.action_btn.setVisible(False)
        self.setToolTip(info.message + (f"\n{info.detail}" if info.detail else ""))


class TaskQueueTable(QTableWidget):
    """Modern 4-column task queue table with status badges, smooth progress bars, and inline quick actions."""

    retry_requested = Signal(str)       # job_id
    cancel_requested = Signal(str)      # job_id

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["视频标题 / 输入源", "当前阶段", "进度", "快捷操作"])
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(2, 165)
        self.setColumnWidth(3, 150)

        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(40)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.customContextMenuRequested.connect(self._show_context_menu)
        self.cellDoubleClicked.connect(self._on_double_clicked)

        self._row_by_job_id: Dict[str, int] = {}
        self._items_by_job_id: Dict[str, TaskItem] = {}

    def add_task_row(self, item: TaskItem) -> None:
        row = self.rowCount()
        self.insertRow(row)
        job_id = item.job.job_id
        self._row_by_job_id[job_id] = row
        self._items_by_job_id[job_id] = item

        title_item = QTableWidgetItem(item.display_title)
        title_item.setData(Qt.ItemDataRole.UserRole, job_id)
        title_item.setToolTip(item.job.source)
        self.setItem(row, 0, title_item)

        status_item = QTableWidgetItem(item.stage_text)
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setItem(row, 1, status_item)

        pbar = QProgressBar()
        pbar.setRange(0, 100)
        pbar.setValue(item.progress)
        pbar.setTextVisible(True)
        pbar.setFormat("%p%")
        pbar_wrap = QWidget()
        pbar_layout = QHBoxLayout(pbar_wrap)
        pbar_layout.setContentsMargins(6, 6, 6, 6)
        pbar_layout.addWidget(pbar)
        self.setCellWidget(row, 2, pbar_wrap)

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
            status_cell.setText(f"  {item.stage_text}  ")
            if item.status == TaskStatus.COMPLETED:
                status_cell.setForeground(QColor("#10B981"))
            elif item.status == TaskStatus.RUNNING:
                status_cell.setForeground(QColor("#6366F1"))
            elif item.status == TaskStatus.FAILED:
                status_cell.setForeground(QColor("#EF4444"))
            elif item.status == TaskStatus.CANCELLED:
                status_cell.setForeground(QColor("#F59E0B"))
            else:
                status_cell.setForeground(QColor("#94A3B8"))

            if item.error_info:
                tip = item.error_info.user_message
                if item.error_info.recoverable_hint:
                    tip += f"\n建议：{item.error_info.recoverable_hint}"
                status_cell.setToolTip(tip)

        pbar_wrap = self.cellWidget(row, 2)
        if pbar_wrap:
            pbar = pbar_wrap.findChild(QProgressBar)
            if pbar:
                pbar.setValue(item.progress)

        # Inline action buttons in column 3
        action_wrap = QWidget()
        action_layout = QHBoxLayout(action_wrap)
        action_layout.setContentsMargins(4, 4, 4, 4)
        action_layout.setSpacing(4)

        job_id = item.job.job_id
        if item.status == TaskStatus.COMPLETED and item.result and item.result.md_path.exists():
            md_path = item.result.md_path
            btn_open = QPushButton("打开")
            btn_open.setFixedHeight(26)
            btn_open.clicked.connect(lambda _=False, p=md_path: open_path_in_os(p, False))
            btn_dir = QPushButton("目录")
            btn_dir.setFixedHeight(26)
            btn_dir.clicked.connect(lambda _=False, p=md_path: open_path_in_os(p, True))
            action_layout.addWidget(btn_open)
            action_layout.addWidget(btn_dir)
        elif item.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            btn_retry = QPushButton("重试")
            btn_retry.setFixedHeight(26)
            btn_retry.clicked.connect(lambda _=False, j=job_id: self.retry_requested.emit(j))
            action_layout.addWidget(btn_retry)
        elif item.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
            btn_cancel = QPushButton("取消")
            btn_cancel.setObjectName("DangerButton")
            btn_cancel.setFixedHeight(26)
            btn_cancel.clicked.connect(lambda _=False, j=job_id: self.cancel_requested.emit(j))
            action_layout.addWidget(btn_cancel)

        self.setCellWidget(row, 3, action_wrap)

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
