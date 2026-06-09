# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec para GeoTools.
Gera um único GeoTools.exe com a pasta Blocos/ embutida.

Build:
    pip install pyinstaller
    pyinstaller GeoTools.spec
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / "gpx2dxf.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # Embute a pasta Blocos/ inteira dentro do executável
        (str(ROOT / "Blocos"), "Blocos"),
    ],
    hiddenimports=[],
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
    name="GeoTools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,       # aplicativo de terminal — mantém a janela do console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
