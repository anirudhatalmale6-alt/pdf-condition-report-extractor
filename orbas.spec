# -*- mode: python ; coding: utf-8 -*-
# One-folder (onedir) build. Chosen over one-file because:
#   * It starts instantly (no self-extraction to a temp folder on every launch).
#   * Windows Defender / SmartScreen rarely quarantines a folder-based app,
#     whereas a lone unsigned one-file .exe is the classic false-positive target.
# UPX is disabled on purpose - UPX-packed binaries trip AV heuristics.

# Bundle tkinterdnd2 (drag-and-drop) incl. its native tkdnd binaries.
try:
    from PyInstaller.utils.hooks import collect_all
    _dnd_datas, _dnd_binaries, _dnd_hidden = collect_all('tkinterdnd2')
except Exception:
    _dnd_datas, _dnd_binaries, _dnd_hidden = [], [], []

# Bundle a self-contained Tesseract OCR engine (for scanned/image-only reports)
# when the build has staged it into ./tesseract (see build.yml). Each file is
# copied preserving its folder structure, so the app ships tesseract/tesseract.exe
# and tesseract/tessdata/*. Absent locally -> OCR simply falls back to a system
# tesseract during development.
import os as _os
_tess_datas = []
if _os.path.isdir('tesseract'):
    for _root, _dirs, _files in _os.walk('tesseract'):
        for _f in _files:
            _full = _os.path.join(_root, _f)
            _tess_datas.append((_full, _os.path.relpath(_root)))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_dnd_binaries,
    datas=[('schemas', 'schemas'), ('assets', 'assets')] + _dnd_datas + _tess_datas,
    hiddenimports=[
        'pdfplumber',
        'pdfminer',
        'pdfminer.high_level',
        'pdfminer.layout',
        'pdfminer.pdfpage',
        'PIL',
        'PIL.Image',
        'pytesseract',
        'requests',
        'fitz',
        'tkinter',
        'tkinterdnd2',
        'src',
        'src.config',
        'src.extractor',
        'src.ocr',
        'src.cloud_sync',
        'src.license',
        'src.gui',
        'src.cli',
    ] + _dnd_hidden,
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
    icon='assets/orbas.ico',
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
