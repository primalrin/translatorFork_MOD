# -*- mode: python ; coding: utf-8 -*-
import importlib.util
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


def add_runtime_data(entries, source, destination):
    source_path = Path(source)
    if source_path.exists():
        entries.append((str(source_path), str(destination)))


datas = [
    ("config", "config"),
    ("README.md", "."),
    ("ffmpeg.exe", "."),
    ("ffprobe.exe", "."),
    ("gemini_translator\\scripts\\chatgpt_workascii_bridge.cjs", "gemini_translator\\scripts"),
    ("gemini_translator\\scripts\\chatgpt_profile_launcher.cjs", "gemini_translator\\scripts"),
]

spec = importlib.util.find_spec("playwright")
if spec and spec.origin:
    driver_dir = Path(spec.origin).parent / "driver"
    add_runtime_data(datas, driver_dir / "node.exe", "playwright_runtime")
    add_runtime_data(datas, driver_dir / "package", "playwright_runtime\\package")

localappdata = os.environ.get("LOCALAPPDATA")
if localappdata:
    add_runtime_data(datas, Path(localappdata) / "ms-playwright", "playwright_runtime\\ms-playwright")

datas += collect_data_files("PyQt6")
datas += collect_data_files("certifi")
datas += collect_data_files("docx")
datas += collect_data_files("emoji")
datas += collect_data_files("jieba")
datas += collect_data_files("lxml")
datas += collect_data_files("werkzeug")


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["PyQt6.sip", "docx", "pypdf", "playwright.sync_api", "google.genai", "google.genai.types"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="test",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["gemini_translator\\GT.ico"],
)
