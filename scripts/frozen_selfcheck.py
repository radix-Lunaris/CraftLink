"""
Frozen-build self check.

Run this to find out whether the single-file EXE can actually import every
module it needs. When the GUI crashes with no window, PyInstaller hides the
traceback (console=False), so this script is the way to see it:

    # from a normal Python, to see what SHOULD be importable:
    python scripts/frozen_selfcheck.py

    # from inside the frozen build, the packaged app runs it automatically
    # when MCLINK_SELFCHECK=1 is set in the environment.

Exits non-zero and prints the traceback on the first failure.
"""

import os
import sys
import traceback


def main():
    print("=" * 60)
    print("MC-Link frozen build self check")
    print("=" * 60)
    print(f"python      : {sys.version.split()[0]}")
    print(f"executable  : {sys.executable}")
    print(f"frozen      : {getattr(sys, 'frozen', False)}")
    print(f"cwd         : {os.getcwd()}")
    print(f"_MEIPASS    : {getattr(sys, '_MEIPASS', '(none)')}")
    print("")

    here = os.path.dirname(os.path.abspath(__file__))

    # Make the client package importable from a source checkout too.
    client_dir = os.path.join(os.path.dirname(here), "client")
    if os.path.isdir(client_dir) and client_dir not in sys.path:
        sys.path.insert(0, client_dir)

    modules = [
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "tkinter.filedialog",
        "asyncio",
        "logging.handlers",
        "mclink",
        "mclink.config.settings",
        "mclink.config.credentials",
        "mclink.core.protocol",
        "mclink.core.common",
        "mclink.core.host_core",
        "mclink.core.player_core",
        "mclink.core.session",
        "mclink.core.control_client",
        "mclink.core.engine",
        # Imported lazily inside functions, so PyInstaller cannot see them:
        # these MUST be in the spec's hiddenimports or the frozen exe dies at
        # runtime with ModuleNotFoundError, in whichever feature happens to
        # need them first.
        "mclink.core.room",
        "mclink.core.presence",
        "mclink.core.udptunnel",
        "mclink.core.channel",
        "mclink.core.transport",
        "mclink.core.stun",
        "mclink.gui.app",
        "mclink.gui.auth_ui",
        # 外观（语言 / 配色）是两个独立模块，app.py 是 import 它们的，
        # 所以这里也点名导入一次：真丢了的话，运行时会连界面都建不出来。
        "mclink.gui.i18n",
        "mclink.gui.theme",
    ]

    failed = 0

    for name in modules:
        try:
            __import__(name)
            print(f"  OK    {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")
            traceback.print_exc()

    print("")

    if failed:
        print(f"{failed} 个模块导入失败 —— 打包缺少 hiddenimports")
        return 1

    # Actually build the window (without entering mainloop).
    try:
        import tkinter as tk
        from mclink.gui import app as gui_app

        root = tk.Tk()
        root.withdraw()
        application = gui_app.MCLinkApp(root)
        application._on_mode_change()
        application._on_role_change()
        application._refresh_account_ui()
        root.update_idletasks()
        root.destroy()

        print("  界面构建成功")

    except Exception:
        traceback.print_exc()
        print("界面构建失败")
        return 1

    print("")
    print("全部通过：这个 exe 可以正常运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
