# -*- mode: python ; coding: utf-8 -*-
# One-folder (onedir) build. Chosen over one-file because:
#   * It starts instantly (no self-extraction to a temp folder on every launch).
#   * Windows Defender / SmartScreen rarely quarantines a folder-based app,
#     whereas a lone unsigned one-file .exe is the classic false-positive target.
# UPX is disabled on purpose - UPX-packed binaries trip AV heuristics.

# Windows version metadata. Until v3.7.19 the exe carried NO version resource at
# all: right-click -> Properties -> Details was blank, with no publisher, product
# or version. That is exactly the shape Defender's heuristics score as suspicious
# on an unsigned binary, and v3.7.18 was duly quarantined on the client's machine
# as "a virus or potentially unwanted software" hours after release. Metadata is
# not a substitute for code signing - it just stops us handing the scanner an
# anonymous executable. Single-sourced from src/config.py so there is one version
# string in the project, parsed rather than imported to keep the spec side-effect
# free.
import re as _re
with open('src/config.py', encoding='utf-8') as _fh:
    _VERSION = _re.search(r'^VERSION\s*=\s*"([^"]+)"', _fh.read(), _re.M).group(1)
_vtuple = tuple(int(p) for p in _VERSION.split('.'))[:3] + (0,)
_vstr = '.'.join(str(p) for p in _vtuple)
_version_res = f"""
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_vtuple}, prodvers={_vtuple},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'ORBAS'),
      StringStruct('FileDescription', 'ORBAS Condition Report Extractor'),
      StringStruct('FileVersion', '{_vstr}'),
      StringStruct('InternalName', 'ORBAS'),
      StringStruct('LegalCopyright', 'Copyright ORBAS'),
      StringStruct('OriginalFilename', 'ORBAS.exe'),
      StringStruct('ProductName', 'ORBAS Condition Report Extractor'),
      StringStruct('ProductVersion', '{_vstr}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
_version_file = 'build_version_info.txt'
with open(_version_file, 'w', encoding='utf-8') as _fh:
    _fh.write(_version_res)

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
        'PIL.ImageOps',
        'numpy',
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
    version=_version_file,
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
