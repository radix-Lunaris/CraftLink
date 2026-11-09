"""量一下两列到底各需要多宽（默认窗口大小应该照这个来）。"""

import os
import sys
import tempfile
import tkinter as tk

sys.path.insert(0, "client")
sandbox = tempfile.mkdtemp(prefix="probe-width-")
os.environ["APPDATA"] = sandbox
os.environ["XDG_CONFIG_HOME"] = sandbox

from mclink.gui import app as gui_app  # noqa: E402

root = tk.Tk()
root.withdraw()
a = gui_app.MCLinkApp(root)
a._on_mode_change()
a._on_role_change()
root.update_idletasks()

middle = None

for child in a.page.inner.winfo_children():
    for sub in child.winfo_children():
        if sub.winfo_class() == "Frame" and len(sub.winfo_children()) == 2:
            middle = sub

print("screen   :", root.winfo_screenwidth(), "x", root.winfo_screenheight())
print("tk scaling:", root.call("tk", "scaling"))
print("outer req:", a.page.inner.winfo_reqwidth())
print("page req :", a.page.winfo_reqwidth())

if middle is not None:
    left, right = middle.winfo_children()[:2]
    print("left  req:", left.winfo_reqwidth(), "actual:", left.winfo_width())
    print("right req:", right.winfo_reqwidth(), "actual:", right.winfo_width())

    # 每张卡片各自要多少
    for column, name in ((left, "left"), (right, "right")):
        for card in column.winfo_children():
            print(f"  {name} card req:", card.winfo_reqwidth())

root.destroy()
