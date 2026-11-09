"""
让 `gui/app.py` 在**没有 tkinter** 的环境里也能被 import 的小脚手架。

============================================================================
 为什么需要它
============================================================================

v3.9.16 的 GUI 测试是**源码 grep**（断言某个字符串在不在 app.py 里）。这类
测试改个变量名就红、逻辑写错却绿，信号很低（复核报告 P3）。

要"真跑 GUI 逻辑"，就得真的 import `app.py` —— 而它 `import tkinter`。
CI / 服务器往往没有 tkinter（本沙箱就没有），所以这里提供一个最小的
tkinter 替身：只提供 import 与类定义所需的符号，不提供任何真实控件。

**它不代表界面能跑起来** —— 它只让"和 tkinter 无关的那部分逻辑"
（`_defer_host_gone` / `_flush_pending_host_gone` / `_on_host_gone` 这些
纯状态机）可以被真正执行到。
"""

import sys
import types


class _Any:
    """接受任意属性访问/调用的替身（控件、常量、字体……）。"""

    def __init__(self, *args, **kwargs):
        self._args = args
        self._kwargs = kwargs

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)

        return _Any()

    def __call__(self, *args, **kwargs):
        return _Any()

    def __setitem__(self, key, value):
        return None

    def __getitem__(self, key):
        return _Any()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True

    def __str__(self):
        return ""

    def __int__(self):
        return 0


def install():
    """把 tkinter 相关的模块塞进 sys.modules（幂等）。"""
    if "tkinter" in sys.modules and not getattr(
        sys.modules["tkinter"], "_is_stub", False
    ):
        return  # 真 tkinter 在场，别动它

    tk = types.ModuleType("tkinter")
    tk._is_stub = True

    for name in (
        "Tk", "Toplevel", "Frame", "Label", "Button", "Entry", "Text",
        "Canvas", "Checkbutton", "Radiobutton", "Scale", "Scrollbar",
        "Listbox", "Menu", "Menubutton", "LabelFrame", "PanedWindow",
        "Spinbox", "Message", "PhotoImage", "BitmapImage", "Widget",
        "StringVar", "IntVar", "BooleanVar", "DoubleVar", "Variable",
        "TclError", "TclVersion", "TkVersion", "END", "N", "S", "E", "W",
        "NS", "EW", "NSEW", "CENTER", "LEFT", "RIGHT", "TOP", "BOTTOM",
        "BOTH", "X", "Y", "NONE", "HORIZONTAL", "VERTICAL", "DISABLED",
        "NORMAL", "ACTIVE", "HIDDEN", "SOLID", "RAISED", "SUNKEN",
        "FLAT", "RIDGE", "GROOVE", "WORD", "CHAR", "INSERT", "SEL",
        "TRUE", "FALSE", "YES", "NO", "ON", "OFF",
    ):
        setattr(tk, name, _Any() if name[0].isupper() else _Any())

    # 这些在 import 期就会被**调用**（`tkfont.Font(...)` 之类），返回替身即可。
    tk.Tcl = _Any
    tk.mainloop = lambda *a, **k: None

    ttk = types.ModuleType("tkinter.ttk")
    ttk._is_stub = True

    for name in (
        "Frame", "Label", "Button", "Entry", "Combobox", "Notebook",
        "Progressbar", "Scrollbar", "Separator", "Sizegrip", "Style",
        "Treeview", "Checkbutton", "Radiobutton", "LabelFrame",
        "PanedWindow", "Spinbox", "LabeledScale", "OptionMenu",
    ):
        setattr(ttk, name, _Any)

    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog._is_stub = True

    for name in (
        "askopenfilename", "asksaveasfilename", "askdirectory",
        "askopenfilenames", "askopenfile", "asksaveasfile",
    ):
        setattr(filedialog, name, lambda *a, **k: None)

    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox._is_stub = True
    #: 记录**被弹出来的东西**，测试据此断言"弹了什么 / 弹了几次"。
    messagebox.shown = []

    for name in (
        "showinfo", "showwarning", "showerror", "askyesno", "askokcancel",
        "askretrycancel", "askyesnocancel", "askquestion",
    ):
        def _record(_name=name):
            def _fn(*args, **kwargs):
                messagebox.shown.append((_name, args, kwargs))

                return None

            return _fn

        setattr(messagebox, name, _record())

    font = types.ModuleType("tkinter.font")
    font._is_stub = True
    font.Font = _Any
    font.families = lambda *a, **k: []
    font.nametofont = lambda *a, **k: _Any()

    tk.ttk = ttk
    tk.filedialog = filedialog
    tk.messagebox = messagebox
    tk.font = font

    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.filedialog"] = filedialog
    sys.modules["tkinter.messagebox"] = messagebox
    sys.modules["tkinter.font"] = font


def make_app():
    """
    造一个**不启动任何界面**的 `MCLinkApp` 实例，用来跑纯状态逻辑。

    只填被测方法真正会碰到的那几个属性；其余走 `__getattr__` 缺失时由测试
    自己补。刻意不用 `MCLinkApp.__init__`（那会真的去建控件）。
    """
    install()

    from mclink.gui import app as app_mod

    instance = app_mod.MCLinkApp.__new__(app_mod.MCLinkApp)

    instance.logs = []
    instance.running = True
    instance._reconnect_phase = ""
    instance._reconnect_attempt = 0
    instance._reconnect_started_at = 0.0
    instance._reconnect_next_at = 0.0
    instance._reconnect_stop_reason = ""
    instance._rebuild_in_flight = False
    instance._mode_switch_silent = False
    instance._mode_switch_silent_until = 0.0
    instance._mode_rebuild_pending = False
    instance._mode_rebuild_pending_until = 0.0
    instance._pending_host_gone = None
    instance.engine = None
    # `messagebox.showinfo(..., parent=self.root)` 会碰到它；替身即可。
    instance.root = _Any()

    def _log(text="", kind="normal"):
        instance.logs.append((kind, text))

    instance.log = _log

    #: 被测方法会调这些"界面"方法，替身掉它们。
    instance._refresh_room_ui = lambda *a, **k: None
    instance._host_down_hint = lambda text: None

    return instance
