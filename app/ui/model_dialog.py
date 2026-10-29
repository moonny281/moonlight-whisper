"""Dialog for managing Whisper CTranslate2 models: status view, HF mirror download, and manual import."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.core import model_manager
from app.core.errors import ErrorInfo
from app.core.model_manager import ModelStatus
from app.paths import PATHS, AppPaths
from app.ui.widgets import open_path_in_os
from app.workers import ModelDownloadWorker


class ModelManagerDialog(QDialog):
    """Displays supported Whisper models with installation status, mirror toggle, download, and manual import."""

    def __init__(
        self,
        cfg: Config,
        paths: AppPaths = PATHS,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.paths = paths
        self._worker: Optional[ModelDownloadWorker] = None
        self._statuses: List[ModelStatus] = []

        self.setWindowTitle("Whisper 模型管理")
        self.setMinimumSize(680, 420)
        self._build_ui()
        self.refresh_table()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        top_row = QHBoxLayout()
        self.chk_hf_mirror = QCheckBox("使用国内 HuggingFace 镜像 (hf-mirror.com)")
        self.chk_hf_mirror.setChecked(self.cfg.hf_mirror)
        self.chk_hf_mirror.toggled.connect(self._on_mirror_toggled)
        top_row.addWidget(self.chk_hf_mirror)
        top_row.addStretch(1)

        btn_open_dir = QPushButton("打开模型文件夹")
        btn_open_dir.clicked.connect(lambda: open_path_in_os(self.paths.models_dir, False))
        top_row.addWidget(btn_open_dir)
        root.addLayout(top_row)

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["模型名称", "预估体积", "状态", "适用场景建议"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        root.addWidget(self.pbar)

        self.lbl_status = QLabel("选择上方模型可进行在线下载或从本地文件夹手动导入。")
        root.addWidget(self.lbl_status)

        btn_row = QHBoxLayout()
        self.btn_download = QPushButton("下载选中模型")
        self.btn_download.setObjectName("PrimaryButton")
        self.btn_import = QPushButton("手动导入选中模型...")
        self.btn_cancel_dl = QPushButton("取消下载")
        self.btn_cancel_dl.setEnabled(False)
        btn_close = QPushButton("关闭")

        self.btn_download.clicked.connect(self._download_selected)
        self.btn_import.clicked.connect(self._import_selected)
        self.btn_cancel_dl.clicked.connect(self._cancel_download)
        btn_close.clicked.connect(self.close)

        btn_row.addWidget(self.btn_download)
        btn_row.addWidget(self.btn_import)
        btn_row.addWidget(self.btn_cancel_dl)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    def _on_mirror_toggled(self, checked: bool) -> None:
        self.cfg.hf_mirror = checked
        self.cfg.save(paths=self.paths)

    def refresh_table(self) -> None:
        self._statuses = model_manager.list_models(self.paths)
        self.table.setRowCount(len(self._statuses))
        for row, st in enumerate(self._statuses):
            name_item = QTableWidgetItem(st.spec.name)
            name_item.setData(Qt.ItemDataRole.UserRole, st.spec.name)
            size_str = f"~{st.spec.approx_size_mb} MB"
            status_str = f"已下载 ({st.actual_size_mb:.0f} MB)" if st.installed else "未下载"

            status_item = QTableWidgetItem(status_str)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(size_str))
            self.table.setItem(row, 2, status_item)
            self.table.setItem(row, 3, QTableWidgetItem(st.spec.recommendation))

            if st.spec.name == self.cfg.model:
                self.table.selectRow(row)

    def _selected_model_name(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._statuses):
            return None
        return self._statuses[row].spec.name

    def _set_busy(self, busy: bool) -> None:
        self.btn_download.setEnabled(not busy)
        self.btn_import.setEnabled(not busy)
        self.btn_cancel_dl.setEnabled(busy)
        self.table.setEnabled(not busy)

    def _download_selected(self) -> None:
        model_name = self._selected_model_name()
        if not model_name:
            QMessageBox.information(self, "提示", "请先在列表中选择一个模型。")
            return

        self._set_busy(True)
        self.pbar.setValue(0)
        self.lbl_status.setText(f"正在准备下载模型 {model_name}...")

        worker = ModelDownloadWorker(
            model_name=model_name,
            hf_mirror=self.chk_hf_mirror.isChecked(),
            proxy=self.cfg.proxy,
            paths=self.paths,
            parent=self,
        )
        worker.progress_changed.connect(self._on_progress)
        worker.download_succeeded.connect(self._on_succeeded)
        worker.download_failed.connect(self._on_failed)
        self._worker = worker
        worker.start()

    def _on_progress(self, pct: int, msg: str) -> None:
        self.pbar.setValue(pct)
        self.lbl_status.setText(msg)

    def _on_succeeded(self, model_name: str, local_dir: str) -> None:
        self._worker = None
        self._set_busy(False)
        self.refresh_table()
        self.lbl_status.setText(f"模型 {model_name} 下载完成：{local_dir}")
        QMessageBox.information(self, "下载完成", f"模型 {model_name} 已成功下载到：\n{local_dir}")

    def _on_failed(self, model_name: str, err: ErrorInfo) -> None:
        self._worker = None
        self._set_busy(False)
        self.lbl_status.setText(err.user_message)
        if err.category != "cancelled":
            QMessageBox.warning(self, "下载失败", f"{err.user_message}\n\n{err.detail}")

    def _cancel_download(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()

    def _import_selected(self) -> None:
        model_name = self._selected_model_name()
        if not model_name:
            QMessageBox.information(self, "提示", "请先在列表中选择要导入的目标模型名称。")
            return

        folder = QFileDialog.getExistingDirectory(
            self,
            f"选择包含 {model_name} (CTranslate2 格式，含 model.bin) 的本地文件夹",
        )
        if not folder:
            return

        try:
            target = model_manager.import_model_directory(model_name, Path(folder), paths=self.paths)
            self.refresh_table()
            QMessageBox.information(self, "导入成功", f"已成功导入模型 {model_name} 至：\n{target}")
        except Exception as exc:
            err = ErrorInfo.from_exception(exc)
            QMessageBox.warning(self, "导入失败", f"{err.user_message}\n\n{err.detail}")
