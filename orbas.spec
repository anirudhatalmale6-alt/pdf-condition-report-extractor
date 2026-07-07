# -*- mode: python ; coding: utf-8 -*-
# One-folder (onedir) build. Chosen over one-file because:
#   * It starts instantly (no self-extraction to a temp folder on every launch).
#   * Windows Defender / SmartScreen rarely quarantines a folder-based app,
#     whereas a lone unsigned one-file .exe is the classic false-positive target.
# UPX is disabled on purpose - UPX-packed binaries trip AV heuristics.

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('schemas', 'schemas')],
    hiddenimports=[
        'pdfplumber',
        'pdfminer',
        'pdfminer.high_level',
        'pdfminer.layout',
        'pdfminer.pdfpage',
        'PIL',
        'PIL.Image',
        'requests',
        'fitz',
        'tkinter',
        'src',
        'src.config',
        'src.extractor',
        'src.cloud_sync',
        'src.license',
        'src.gui',
        'src.cli',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'webview',
        'pywebview',
        'pythonnet',
        'clr',
        'clr_loader',
        'bottle',
        'proxy_tools',
        'pytesseract',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ORBAS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ORBAS',
)
