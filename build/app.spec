# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec file for Moonlight Whisper (with Qt plugin pruning & version resource)."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

block_cipher = None
project_root = Path(SPECPATH).resolve().parent
version_file = project_root / "build" / "file_version_info.txt"

datas = [
    (str(project_root / "assets"), "assets"),
]
binaries = []
hiddenimports = [
    "yt_dlp",
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "huggingface_hub",
    "opencc",
    "requests",
    "socks",
]

for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "opencc", "yt_dlp"):
    try:
        pkg_datas, pkg_bins, pkg_hidden = collect_all(pkg)
        datas.extend(pkg_datas)
        binaries.extend(pkg_bins)
        hiddenimports.extend(pkg_hidden)
    except Exception:
        pass

a = Analysis(
    [str(project_root / "app" / "main.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=list(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "torchaudio",
        "torchvision",
        "matplotlib",
        "scipy",
        "pandas",
        "tkinter",
        # Prune unused heavy PySide6 modules (§9.1)
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQml",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DExtras",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtSensors",
        "PySide6.QtPositioning",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtWebSockets",
        "PySide6.QtWebChannel",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MoonlightWhisper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project_root / "assets" / "app.ico"),
    version=str(version_file) if version_file.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MoonlightWhisper",
)
