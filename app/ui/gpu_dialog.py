"""Windows 11 Mica-styled dialog for downloading or importing CUDA 12 / cuDNN 9 runtime DLLs."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.core import gpu_runtime
from app.core.errors import ErrorInfo
from app.core.gpu_runtime import GpuInfo, GpuStatus
from app.paths import PATHS, AppPaths
from app.ui.theme import apply_win11_mica_window_attributes, resolve_effective_theme
from app.ui.widgets import make_mica_section
from app.workers import GpuInstallWorker


class GpuRuntimeDialog(QDialog):
    """Interactive Mica wizard for installing or importing CUDA 12 runtime DLLs into `gpu_runtime/`."""

    def __init__(
        self,
        cfg: Config,
        gpu_info: Optional[GpuInfo] = None,
        paths: AppPaths = PATHS,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.paths = paths
        self.gpu_info = gpu_info or gpu_runtime.detect(paths)
        self._worker: Optional[GpuInstallWorker] = None

        self.setWindowTitle("NVIDIA GPU 加速运行库安装向导")
        self.setMinimumWidth(560)
        self._build_ui()
        self._refresh_info_labels()

        eff = resolve_effective_theme(self.cfg.ui.theme)
        apply_win11_mica_window_attributes(self, is_dark=(eff == "dark"))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        hw_card, hw_box = make_mica_section(
            "硬件与环境检测",
            "启用 NVIDIA GPU 加速需下载 CUDA 12 (cuBLAS 12 + cuDNN 9) 运行库（约 1GB，一次下载永久复用）。",
        )
        hw_form = QFormLayout()
        hw_form.setHorizontalSpacing(14)
        hw_form.setVerticalSpacing(8)
        self.lbl_device = QLabel()
        self.lbl_driver = QLabel()
        self.lbl_status = QLabel()
        self.lbl_missing = QLabel()
        self.lbl_missing.setWordWrap(True)
        hw_form.addRow("显卡型号：", self.lbl_device)
        hw_form.addRow("驱动版本：", self.lbl_driver)
        hw_form.addRow("当前状态：", self.lbl_status)
        hw_form.addRow("缺失组件：", self.lbl_missing)
        hw_box.addLayout(hw_form)
        root.addWidget(hw_card)

        dl_card, dl_box = make_mica_section("下载与离线导入")
        mirror_row = QHBoxLayout()
        mirror_row.addWidget(QLabel("下载镜像源："))
        self.combo_mirror = QComboBox()
        self.combo_mirror.addItem("清华大学开源镜像 (tuna - 推荐)", "tuna")
        self.combo_mirror.addItem("阿里云开源镜像 (aliyun)", "aliyun")
        self.combo_mirror.addItem("PyPI 官方源 (official)", "official")
        idx = self.combo_mirror.findData(self.cfg.pypi_mirror)
        if idx >= 0:
            self.combo_mirror.setCurrentIndex(idx)
        mirror_row.addWidget(self.combo_mirror, 1)
        dl_box.addLayout(mirror_row)

        self.pbar = QProgressBar()
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        dl_box.addWidget(self.pbar)

        self.lbl_progress_detail = QLabel("准备就绪（支持断点续传与 SHA-256 校验）")
        self.lbl_progress_detail.setObjectName("MutedLabel")
        self.lbl_progress_detail.setWordWrap(True)
        dl_box.addWidget(self.lbl_progress_detail)
        root.addWidget(dl_card)

        btn_row = QHBoxLayout()
        self.btn_download = QPushButton("在线下载并安装")
        self.btn_download.setObjectName("PrimaryButton")
        self.btn_import_zip = QPushButton("导入压缩包 (.zip/.whl)...")
        self.btn_import_dir = QPushButton("导入本地文件夹...")
        self.btn_cancel = QPushButton("关闭")

        self.btn_download.clicked.connect(self._start_download)
        self.btn_import_zip.clicked.connect(self._import_archive)
        self.btn_import_dir.clicked.connect(self._import_folder)
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)

        btn_row.addWidget(self.btn_download)
        btn_row.addWidget(self.btn_import_zip)
        btn_row.addWidget(self.btn_import_dir)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_cancel)
        root.addLayout(btn_row)

    def _refresh_info_labels(self) -> None:
        info = self.gpu_info
        self.lbl_device.setText(
            f"{info.device_name or '未识别'} ({info.vram_gb:.1f} GB)" if info.device_name else "未检测到 NVIDIA GPU"
        )
        self.lbl_driver.setText(info.driver_version or "未知")
        self.lbl_status.setText(info.message or info.status.value)
        if info.missing_files:
            self.lbl_missing.setText(", ".join(info.missing_files))
        else:
            self.lbl_missing.setText("无（所需 10 个核心与引擎 DLL 已齐全）")

    def _set_busy(self, busy: bool) -> None:
        self.btn_download.setEnabled(not busy)
        self.btn_import_zip.setEnabled(not busy)
        self.btn_import_dir.setEnabled(not busy)
        self.combo_mirror.setEnabled(not busy)

    def _start_download(self) -> None:
        mirror = str(self.combo_mirror.currentData() or "tuna")
        self.cfg.pypi_mirror = mirror
        self.cfg.save(paths=self.paths)

        self._set_busy(True)
        self.lbl_progress_detail.setText("正在连接镜像源获取下载信息...")
        worker = GpuInstallWorker(mirror=mirror, proxy=self.cfg.proxy, paths=self.paths, parent=self)
        worker.progress_changed.connect(self._on_dl_progress)
        worker.install_succeeded.connect(self._on_dl_succeeded)
        worker.install_failed.connect(self._on_dl_failed)
        self._worker = worker
        worker.start()

    def _on_dl_progress(self, pct: int, msg: str) -> None:
        self.pbar.setValue(pct)
        self.lbl_progress_detail.setText(msg)

    def _on_dl_succeeded(self, info: GpuInfo) -> None:
        self._worker = None
        self._set_busy(False)
        self.gpu_info = info
        self._refresh_info_labels()
        if info.status == GpuStatus.READY:
            QMessageBox.information(self, "安装成功", "CUDA 运行库已安装并验证通过，GPU 加速已就绪！")
            self.accept()
        else:
            QMessageBox.warning(
                self,
                "需重启或检查环境",
                f"运行库文件已解压完成，但当前状态为：{info.message}\n{info.detail}\n建议重启程序后再试。",
            )
            self.accept()

    def _on_dl_failed(self, err: ErrorInfo) -> None:
        self._worker = None
        self._set_busy(False)
        self.lbl_progress_detail.setText(err.user_message)
        if err.category != "cancelled":
            QMessageBox.critical(
                self,
                "下载失败",
                f"{err.user_message}\n\n详情：{err.detail}\n\n支持重新点击「在线下载并安装」断点续传。",
            )

    def _import_archive(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择包含 CUDA 12 / cuDNN 9 DLL 的压缩包或 wheel 文件",
            "",
            "Archive Files (*.zip *.whl)",
        )
        if not file_path:
            return
        self._run_local_import(Path(file_path))

    def _import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择包含 CUDA 12 / cuDNN 9 DLL 的文件夹")
        if not folder:
            return
        self._run_local_import(Path(folder))

    def _run_local_import(self, path: Path) -> None:
        try:
            info = gpu_runtime.import_from(path, paths=self.paths)
            self.gpu_info = info
            self._refresh_info_labels()
            QMessageBox.information(self, "导入成功", f"CUDA 运行库导入完成！\n当前状态：{info.message}")
            self.accept()
        except Exception as exc:
            err = ErrorInfo.from_exception(exc)
            QMessageBox.warning(self, "导入未完成", f"{err.user_message}\n\n{err.detail}")

    def _on_cancel_clicked(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(2000)
        self.reject()
