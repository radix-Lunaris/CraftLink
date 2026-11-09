"""
MC-Link client entry point.

Usage:
    python main.py                 # 启动图形界面

Windows users can also double click 启动客户端.bat .
"""

import contextlib
import os
import sys

# Make `mclink` importable when running from source or from a frozen EXE.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _setup_console():
    """
    Best effort UTF-8 console output.

    The GUI itself is always UTF-8 (tkinter uses unicode strings), this only
    affects the launch log. A frozen windowed EXE has no console at all, so
    every failure here is ignored.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _write(text):
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    try:
        stream.write(text)
    except Exception:
        pass


def main():
    _setup_console()

    # ---- diagnostic mode ------------------------------------------
    # A windowed (console=False) EXE hides every traceback, so "the exe
    # closes immediately" is otherwise impossible to debug. These two
    # switches make the frozen build self-diagnosing:
    #
    #     MC-Link.exe --selfcheck
    #     set MCLINK_STARTUP_LOG=C:\mclink-startup.log && MC-Link.exe
    # ---------------------------------------------------------------
    if "--selfcheck" in sys.argv or os.environ.get("MCLINK_SELFCHECK") == "1":
        return _run_selfcheck()

    if "--probe" in sys.argv:
        return _run_script("exe_window_probe.py")

    try:
        import tkinter  # noqa: F401
    except ImportError:
        _write(
            "找不到 tkinter，无法启动图形界面。\n"
            "请安装带 tkinter 的 Python（Windows 官方安装包默认自带）。\n"
        )
        return 2

    from mclink.config import settings
    from mclink.config import userdirs
    from mclink.gui import app

    # ★ M6：解析用户数据目录，并（第一次）把老版本目录里的配置/日志迁过来。
    #
    # 放在 `app.run()` **之前**、而且只在真正的入口里做：测试直接构造
    # MCLinkApp 时不会碰用户的真实目录。迁移结果通过 userdirs 的提示队列
    # 交给界面显示（不静默）。
    with contextlib.suppress(Exception):
        report = userdirs.ensure_ready()

        if report.get("ran"):
            _write(
                f"用户数据目录：{userdirs.data_dir()}"
                f"（已迁移 {len(report.get('copied') or [])} 个文件）\n"
            )
        else:
            _write(f"用户数据目录：{userdirs.data_dir()}\n")

    _write(f"{settings.APP_NAME} v{settings.APP_VERSION} 启动中 ...\n")

    try:
        return app.run()
    except Exception as e:  # pragma: no cover - last resort
        import traceback

        detail = traceback.format_exc()
        traceback.print_exc()
        _write(f"客户端异常退出：{e}\n")

        # Always leave a file behind when running frozen: the user has no
        # console to read.
        _write_startup_log(detail)

        try:
            import tkinter.messagebox as messagebox

            messagebox.showerror(
                "CraftLink 错误",
                f"客户端异常退出：\n{e}\n\n详细信息已写入：\n{_startup_log_path()}",
            )
        except Exception:
            pass

        return 1


def _startup_log_path():
    override = os.environ.get("MCLINK_STARTUP_LOG")

    if override:
        return override

    # ★ M6：兜底也**不写程序安装目录**（老代码最后会落到 `_HERE`）。
    # 启动失败日志只是诊断材料，放系统临时目录就够了。
    import tempfile

    base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()

    return os.path.join(base, "mclink-startup.log")


def _write_startup_log(detail):
    try:
        with open(_startup_log_path(), "a", encoding="utf-8") as fh:
            fh.write("\n" + "=" * 60 + "\n")
            fh.write(f"frozen={getattr(sys, 'frozen', False)} exe={sys.executable}\n")
            fh.write(detail)
    except Exception:
        pass


def _run_selfcheck():
    """Run scripts/frozen_selfcheck.py, from the bundle or the source tree."""
    return _run_script("frozen_selfcheck.py")


def _run_script(filename):
    """Run a helper script shipped next to the bundle (diagnostics)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, filename),
        os.path.join(here, "..", "scripts", filename),
        os.path.join(getattr(sys, "_MEIPASS", here), filename),
    ]

    for path in candidates:
        path = os.path.abspath(path)

        if not os.path.isfile(path):
            continue

        namespace = {"__name__": "__main__", "__file__": path}
        code = compile(
            open(path, "r", encoding="utf-8").read(), path, "exec"
        )

        try:
            exec(code, namespace)
        except SystemExit as e:
            return int(e.code or 0)

        return 0

    _write(f"找不到诊断脚本 {filename}。\n")
    return 3


if __name__ == "__main__":
    sys.exit(main())
