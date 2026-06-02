# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller specification for FreeFlow.
#
# Build a single-file Windows executable with:
#
#     pip install pyinstaller PyQt5 -r requirements.txt
#     pyinstaller --clean freeflow.spec
#
# The resulting ``dist/FreeFlow.exe`` (Windows) or ``dist/FreeFlow``
# (Linux / macOS) is a standalone binary — no Python install is needed
# on the target machine.  Bundles Qt for the matplotlib GUI backend
# and Tk as a fallback (Tk ships with CPython so it costs nothing).
#
# Because PyInstaller produces a binary for the OS it runs *on*, a real
# Windows executable must be built from a Windows machine — either
# manually, or via the ``.github/workflows/build-windows.yml`` CI job
# that runs on every ``v*`` tag.

import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Pull in the matplotlib backends explicitly — PyInstaller can't always
# discover them from the dynamic import in ``flowcyt/app.py``.
hiddenimports = [
    "matplotlib.backends.backend_qtagg",
    "matplotlib.backends.backend_qt5agg",
    "matplotlib.backends.backend_tkagg",
    "matplotlib.backends.backend_agg",
]
# Pull in PyQt5 fully so QtAgg works at runtime.
hiddenimports += collect_submodules("PyQt5", filter=lambda n: not n.endswith(".Qsci"))

a = Analysis(
    ["flowcyt/cli.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim dead weight that matplotlib drags in but FreeFlow never uses.
        "matplotlib.tests",
        "PIL.tests",
        "tkinter.test",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="FreeFlow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=False  →  no extra cmd window pops up alongside the GUI on Windows.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,   # drop a path to a .ico here if you have a project icon
)

# macOS only: wrap the executable into a proper .app bundle so it lives
# in Finder / the Dock / Launchpad like a native Mac app and double-click
# launches the GUI without a Terminal popping up.  On Windows and Linux
# this block is skipped and the single-file ``dist/FreeFlow[.exe]``
# binary above is the final artifact.
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="FreeFlow.app",
        icon=None,
        bundle_identifier="com.angiolettiuberti.freeflow",
        info_plist={
            "CFBundleDisplayName": "FreeFlow",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
            "NSAppleScriptEnabled": False,
            "LSMinimumSystemVersion": "10.14",
        },
    )
