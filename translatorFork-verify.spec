# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [('config', 'config'), ('README.md', '.'), ('ffmpeg.exe', '.'), ('ffprobe.exe', '.'), ('gemini_translator\\scripts\\chatgpt_workascii_bridge.cjs', 'gemini_translator\\scripts'), ('gemini_translator\\scripts\\chatgpt_profile_launcher.cjs', 'gemini_translator\\scripts'), ('.venv\\Lib\\site-packages\\playwright\\driver\\node.exe', 'playwright_runtime'), ('.venv\\Lib\\site-packages\\playwright\\driver\\package', 'playwright_runtime\\package'), ('C:\\Users\\shest\\AppData\\Local\\ms-playwright', 'playwright_runtime\\ms-playwright')]
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
    [],
    exclude_binaries=True,
    name='translatorFork-verify',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['gemini_translator\\GT.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='translatorFork-verify',
)
