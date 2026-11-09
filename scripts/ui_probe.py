"""
Measure what the GUI actually renders, instead of guessing about fonts.

Run:
    python scripts/ui_probe.py

Prints, for each font the app uses:
  * whether the family really resolves to itself or silently falls back
  * the measured pixel height of a line of text
  * the declared size in points

and then builds the real window and reports the geometry of the main blocks, so
"the layout looks wrong" can be checked against numbers rather than opinion.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))


def main():
    import tkinter as tk
    from tkinter import font as tkfont

    from mclink.gui import app as gui_app

    root = tk.Tk()
    root.geometry("1100x820")

    print("=" * 68)
    print("字体实测")
    print("=" * 68)
    print(f"  屏幕: {root.winfo_screenwidth()}x{root.winfo_screenheight()}")
    print(f"  Tk 缩放: {root.tk.call('tk', 'scaling'):.2f}")
    print("")

    families = set(tkfont.families())

    for name in ("FONT", "FONT_BOLD", "FONT_SMALL", "FONT_TINY",
                 "FONT_TITLE", "FONT_SUBTITLE", "FONT_MONO"):
        spec = getattr(gui_app, name, None)

        if not spec:
            continue

        family, size = spec[0], spec[1]
        weight = spec[2] if len(spec) > 2 else "normal"

        available = family in families
        handle = tkfont.Font(family=family, size=size, weight=weight)

        # What Tk actually resolved to. If the family is missing, Tk quietly
        # substitutes and the text renders in something nobody chose.
        actual = handle.actual()

        sample = "在线人数 8/8 已加入世界"
        width = handle.measure(sample)
        height = handle.metrics("linespace")

        print(f"  {name:<16} {family} {size}pt {weight}")
        print(f"      字体可用      : {available}")
        print(f"      Tk 实际解析为 : {actual['family']} "
              f"{actual['size']}pt {actual['weight']}")
        print(f"      一行高度      : {height} px")
        print(f"      示例文本宽度  : {width} px   ({sample})")

        if not available:
            print("      !! 这个字体在这台机器上不存在，字体会被替换")

        print("")

    # ---- the real window ----
    print("=" * 68)
    print("布局实测")
    print("=" * 68)

    try:
        application = gui_app.MCLinkApp(root)
    except Exception as e:  # noqa: BLE001
        print(f"  界面构建失败: {type(e).__name__}: {e}")
        root.destroy()
        return 1

    root.update_idletasks()
    root.update()

    print(f"  窗口      : {root.winfo_width()}x{root.winfo_height()}")
    print("")

    widgets = [
        ("页眉", getattr(application, "role_badge", None)),
        ("滚动区", getattr(application, "page", None)),
        ("模式卡片", getattr(application, "mode_hint", None)),
        ("账号标签", getattr(application, "account_label", None)),
        ("房间状态", getattr(application, "room_state_label", None)),
        ("连接提示", getattr(application, "connect_hint", None)),
        ("日志", getattr(application, "log_text", None)),
    ]

    for label, widget in widgets:
        if widget is None:
            print(f"  {label:<10} 不存在")
            continue

        try:
            print(
                f"  {label:<10} {widget.winfo_width():>5}x"
                f"{widget.winfo_height():<5} "
                f"y={widget.winfo_rooty() - root.winfo_rooty():<5}"
                f"x={widget.winfo_rootx() - root.winfo_rootx()}"
            )
        except Exception as e:  # noqa: BLE001
            print(f"  {label:<10} 读不到: {e}")

    # The layout complaint was "a gap in the middle". Measure the two columns
    # so the gap is a number rather than an impression.
    print("")

    try:
        middle = None

        for child in root.winfo_children():
            for sub in child.winfo_children():
                if isinstance(sub, tk.Frame) and sub.winfo_height() > 200:
                    middle = sub
    except Exception:
        middle = None

    if middle is not None:
        print(f"  主内容区  : {middle.winfo_width()}x{middle.winfo_height()}")

    with open(os.path.join(HERE, "ui_probe.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"window {root.winfo_width()}x{root.winfo_height()}\n")

        for label, widget in widgets:
            if widget is None:
                continue

            try:
                fh.write(
                    f"{label} {widget.winfo_width()}x{widget.winfo_height()} "
                    f"y={widget.winfo_rooty() - root.winfo_rooty()}\n"
                )
            except Exception:
                pass

    root.destroy()

    print("")
    print("  已写入 scripts/ui_probe.txt")
    print("=" * 68)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
