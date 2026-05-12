# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


PROJECT_ROOT = Path.cwd()


def discover_ranobelib_source_data():
    base_dir = PROJECT_ROOT / "ranobelib"
    if not (base_dir / "main_window.py").exists():
        return []

    return [
        (str(file_path), "ranobelib")
        for file_path in sorted(base_dir.iterdir())
        if file_path.is_file()
        and file_path.suffix.lower() in {".py", ".mjs"}
        and ".bak" not in file_path.name
    ]


datas = [
    ('config', 'config'),
    ('README.md', '.'),
    ('ffmpeg.exe', '.'),
    ('ffprobe.exe', '.'),
    ('gemini_translator\\scripts\\chatgpt_workascii_bridge.cjs', 'gemini_translator\\scripts'),
    ('gemini_translator\\scripts\\chatgpt_profile_launcher.cjs', 'gemini_translator\\scripts'),
    ('playwright_runtime\\node.exe', 'playwright_runtime'),
    ('playwright_runtime\\package', 'playwright_runtime\\package'),
    ('playwright_runtime\\ms-playwright', 'playwright_runtime\\ms-playwright'),
]
datas += discover_ranobelib_source_data()
datas += collect_data_files('PyQt6')
datas += collect_data_files('certifi')
datas += collect_data_files('docx')
datas += collect_data_files('emoji')
datas += collect_data_files('jieba')
datas += collect_data_files('lxml')
datas += collect_data_files('werkzeug')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['PyQt6.sip', 'docx', 'pypdf', 'playwright.sync_api', 'google.genai', 'google.genai.types'],
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
    name='translatorFork-full',
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
    icon=['gemini_translator\\GT.ico'],
)
