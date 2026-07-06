# -*- mode: python ; coding: utf-8 -*-
import os
import importlib

extra_datas = [('schemas', 'schemas')]

try:
    wv = importlib.import_module('webview')
    wv_path = os.path.dirname(wv.__file__)
    wv_lib = os.path.join(wv_path, 'lib')
    if os.path.isdir(wv_lib):
        extra_datas.append((wv_lib, 'webview/lib'))
except ImportError:
    pass

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=extra_datas,
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
        'src',
        'src.config',
        'src.extractor',
        'src.cloud_sync',
        'src.license',
        'src.gui',
        'src.cli',
        'webview',
        'webview.platforms',
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        'webview.platforms.mshtml',
        'webview.http_server',
        'bottle',
        'clr',
        'clr_loader',
        'clr_loader.ffi',
        'clr_loader.ffi.coreclr',
        'clr_loader.ffi.mono',
        'clr_loader.ffi.netfx',
        'proxy_tools',
        'pythonnet',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ORBAS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=os.path.join(os.environ.get('LOCALAPPDATA', os.environ.get('TEMP', '')), 'ORBAS_runtime'),
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
