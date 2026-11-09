# -*- mode: python ; coding: utf-8 -*-
"""
CraftLink-Builder -- PyInstaller spec（把 Builder 打成单个 exe）。

构建（在 Windows 上，仓库根目录）：

    cd installer\\builder
    pyinstaller CraftLink-Builder.spec --noconfirm --clean

产物：

    installer\\builder\\dist\\CraftLink-Builder.exe

为什么要 `console=True` 而不是 `--noconsole`：

    Builder 是 GUI/CLI 二合一的。CI 里用的是
    `CraftLink-Builder.exe --non-interactive --json 输入.json`，必须有 stdout；
    而双击运行时又不该弹黑框。所以：**带控制台构建**，程序自己在开 GUI 前
    调用 `hide_own_console()`（只在"这个控制台是本进程独占"时隐藏，见
    craftlink_builder.py）。

注意：这个 spec **不碰** `scripts/mclink-client.spec`（客户端那套打包照旧）。
"""

import os

block_cipher = None

here = os.path.abspath(os.getcwd())
tests_dir = os.path.abspath(os.path.join(here, "..", "tests"))

# 自检模块（--selftest）需要一起打包：它在源码模式下是 installer/tests/ 下的
# 一个脚本，冻结后没有"源码目录"可用。
pathex = [here]

if os.path.isdir(tests_dir):
    pathex.append(tests_dir)

hidden = [
    "builder_core",
    "builder_gui",
    "imageconv",
    # tkinter 的子模块是延迟导入的，显式列出来免得"打包少文件"在用户那里才暴露。
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.ttk",
]

if os.path.isfile(os.path.join(tests_dir, "builder_selftest.py")):
    hidden.append("builder_selftest")

a = Analysis(
    [os.path.join(here, "craftlink_builder.py")],
    pathex=pathex,
    binaries=[],
    datas=[],
    hiddenimports=hidden,
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
    name="CraftLink-Builder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,              # CLI 需要 stdout；GUI 模式自己隐藏黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
