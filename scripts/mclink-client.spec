# -*- mode: python ; coding: utf-8 -*-
"""
CraftLink client (原名 MC-Link) -- PyInstaller spec.

Build:
    cd mc-link-v1\\client
    pyinstaller ..\\scripts\\mclink-client.spec --noconfirm --clean

Result:
    mc-link-v1\\client\\dist\\MC-Link.exe      (single file, no console)

The network core is pure standard library, so there are no hidden imports to
worry about. tkinter is detected automatically.
"""

import os

block_cipher = None

project_root = os.path.abspath(os.getcwd())
entry = os.path.join(project_root, "main.py")

assets = []
_assets_dir = os.path.join(project_root, "mclink", "assets")
if os.path.isdir(_assets_dir) and os.listdir(_assets_dir):
    assets.append((_assets_dir, "mclink/assets"))

# 自制图标（草方块）：任务栏 / 资源管理器 / 标题栏都用它。
# 文件由 scripts/make_assets.py 生成；没有图标也能打包，只是回到默认羽毛。
_icon_path = os.path.join(_assets_dir, "app.ico")
_icon = _icon_path if os.path.isfile(_icon_path) else None

# Ship the diagnostics so `CraftLink.exe --selfcheck` / `--probe` work from the
# bundle (a windowed EXE has no console to explain itself otherwise).
_scripts_dir = os.path.join(os.path.dirname(project_root), "scripts")
for _name in ("frozen_selfcheck.py", "exe_window_probe.py"):
    _path = os.path.join(_scripts_dir, _name)
    if os.path.isfile(_path):
        assets.append((_path, "."))

a = Analysis(
    [entry],
    pathex=[project_root],
    binaries=[],
    datas=assets,
    hiddenimports=[
        "mclink.core.engine",
        "mclink.core.host_core",
        "mclink.core.player_core",
        "mclink.core.session",
        "mclink.core.protocol",
        "mclink.core.common",
        "mclink.core.control_client",
        # Imported lazily (inside functions), which PyInstaller cannot follow:
        # without these the exe builds fine and then fails at RUNTIME with
        # "No module named 'mclink.core.presence'" the first time that feature
        # is used. `CraftLink-console.exe --selfcheck` imports each one so this
        # cannot go unnoticed again.
        "mclink.core.room",
        "mclink.core.presence",
        "mclink.core.udptunnel",
        "mclink.core.channel",
        "mclink.core.transport",
        "mclink.core.stun",
        "mclink.gui.app",
        "mclink.gui.auth_ui",
        # 外观模块：语言表 / 配色表。app.py 直接 import，正常会被自动带上，
        # 但显式列出来才能保证"打包少文件"这种问题在下一次就暴露。
        "mclink.gui.i18n",
        "mclink.gui.theme",
        "mclink.config.settings",
        "mclink.config.credentials",
        # Imported lazily inside functions (so PyInstaller cannot see them).
        "logging.handlers",
        "queue",
        "threading",
        "tkinter.messagebox",
        "tkinter.filedialog",
        "tkinter.ttk",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy",
        "PIL",
        "matplotlib",
        "pandas",
        "scipy",
        "PyQt5",
        "PySide2",
        "PySide6",
        "pytest",
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
    name="CraftLink",
    icon=_icon,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI application: no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---------------------------------------------------------------------------
# A second, console-enabled build used ONLY for diagnostics.
#
# A windowed build hides every traceback, which makes "the exe closes
# immediately" impossible to debug. Run dist/MC-Link-console.exe from a
# terminal to see the real error, or run the self check:
#
#     dist\CraftLink-console.exe --selfcheck
# ---------------------------------------------------------------------------
exe_console = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CraftLink-console",
    icon=_icon,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
