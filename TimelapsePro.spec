# -*- mode: python ; coding: utf-8 -*-
#
# Build:  pyinstaller TimelapsePro.spec        (from this directory, in the venv)
#
# Kept as one-file to match how this app has always been shipped. If launch
# feels slow, switching to a COLLECT/one-dir bundle (as MattePro uses) avoids
# re-extracting rawpy, cv2 and numpy on every start.

a = Analysis(
    ['timelapse_pro.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # tifffile is imported lazily inside Pipeline._run_tiff so that ProRes
    # export still works when it is absent. PyInstaller does trace imports
    # inside functions, but it is declared here so TIFF output cannot quietly
    # go missing from a build. imagecodecs comes with it: tifffile routes
    # deflate compression through imagecodecs when it is available, and the
    # TIFF sequence is written with compression="zlib".
    hiddenimports=[
        'tifffile',
        'imagecodecs',
    ],
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
    name='TimelapsePro',
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
app = BUNDLE(
    exe,
    name='TimelapsePro.app',
    icon=None,
    bundle_identifier='com.pislider.timelapsepro',
)
