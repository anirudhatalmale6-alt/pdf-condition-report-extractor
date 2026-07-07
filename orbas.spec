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

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_dnd_binaries,
    datas=[('schemas', 'schemas'), ('assets', 'assets')] + _dnd_datas,
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
        'tkinterdnd2',
        'src',
        'src.config',
        'src.extractor',
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
