# -*- mode: python ; coding: utf-8 -*-
#
# Build:  pyinstaller TimelapsePro.spec        (from this directory, in the venv)
#
# One-dir bundle. This was previously one-file, but PyInstaller rejects that
# combination for macOS .app bundles:
#
#   DEPRECATION: Onefile mode in combination with macOS .app bundles (windowed
#   mode) don't make sense (a .app bundle can not be a single file) and clashes
#   with macOS's security. Please migrate to onedir mode. This will become an
#   error in v7.0.
#
# It also meant re-extracting rawpy, cv2 and numpy on every launch.

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
    [],
    exclude_binaries=True,
    name='TimelapsePro',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TimelapsePro',
)
app = BUNDLE(
    coll,
    name='TimelapsePro.app',
    bundle_identifier='com.pislider.timelapsepro',
)
