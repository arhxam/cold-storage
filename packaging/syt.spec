# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone `syt` binary (onedir).

Build from the repo root with:

    uv run pyinstaller packaging/syt.spec --noconfirm

Output lands in dist/syt/ (dist/syt/syt is the executable).
"""

a = Analysis(
    ["syt_entry.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # keyring discovers its backends dynamically via entry points, which
        # PyInstaller cannot see statically. Pull in the macOS Keychain backend
        # (and the fallback chainer) explicitly so `syt init` can cache the key.
        "keyring.backends.macOS",
        "keyring.backends.chainer",
        "keyring.backends.fail",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Optional Phase-2 live connectors; not shipped in the app bundle.
        "telethon",
        "praw",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="syt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
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
    name="syt",
)
