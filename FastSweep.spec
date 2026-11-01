# -*- mode: python ; coding: utf-8 -*-
# Recette de build utilisée par build.ps1 (source unique des options PyInstaller).
# Build recipe used by build.ps1 (single source of the PyInstaller options).
import os

ROOT = os.path.abspath(SPECPATH)

a = Analysis(
    [os.path.join(ROOT, 'main.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, 'assets', 'fastsweep.ico'), 'assets')],
    hiddenimports=[],
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
    name='FastSweep',
    icon=os.path.join(ROOT, 'assets', 'fastsweep.ico'),
    version=os.path.join(ROOT, 'version_info.txt'),
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
)
