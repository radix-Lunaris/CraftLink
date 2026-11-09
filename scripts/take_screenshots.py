"""
临时脚本：把界面真的画出来，截图存成 PNG，人（我）肉眼看圆角有没有出来。

用真的 Tk 窗口（不 withdraw），update 之后用 ImageGrab 抓窗口区域。
"""

import contextlib
import os
import sys
import tempfile
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "client"))

sandbox = tempfile.mkdtemp(prefix="mclink-shot-")
os.environ["APPDATA"] = sandbox
os.environ["XDG_CONFIG_HOME"] = sandbox

from PIL import ImageGrab  # noqa: E402

from mclink.gui import app as gui_app  # noqa: E402
from mclink.gui import theme as theme_mod  # noqa: E402

out_dir = os.path.join(ROOT, "docs", "shots")
os.makedirs(out_dir, exist_ok=True)


def shoot(root, name):
    # 强制置顶再抓图：否则别的窗口（聊天工具之类）会盖在上面，
    # 截出来的就是别人的界面。
    with contextlib.suppress(Exception):
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()

    root.update_idletasks()
    root.update()
    time.sleep(0.7)
    root.update()

    x = root.winfo_rootx()
    y = root.winfo_rooty()
    w = root.winfo_width()
    h = root.winfo_height()

    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    path = os.path.join(out_dir, name)

    img.save(path)
    print("saved", path, img.size, flush=True)

    with contextlib.suppress(Exception):
        root.attributes("-topmost", False)


root = tk.Tk()
application = gui_app.MCLinkApp(root)
application._on_mode_change()
application._on_role_change()
application._refresh_account_ui()
application._refresh_room_ui()
application._refresh_status()

# 用程序自己的默认尺寸（这样看到的就是用户实际看到的）
root.deiconify()
root.lift()

# 抓图必须让 Tk 的坐标 == 屏幕像素，否则 bbox 会整体偏移（截出来是半张窗口，
# 甚至把桌面上别的窗口一起截进去 —— 那是用户的隐私，不能留）。
# 程序里默认 scaling=1.3，这里先改回 1.0 再重建界面。
with contextlib.suppress(Exception):
    root.call("tk", "scaling", 1.0)
    application.rebuild_ui()

shoot(root, "client-dark.png")

# 页面下半部分（房间列表 / 运行状态 / 日志）：单列布局之后它们在下方
with contextlib.suppress(Exception):
    root.update_idletasks()
    application.page.canvas.yview_moveto(1.0)
    root.update()
    time.sleep(0.3)

shoot(root, "client-dark-bottom.png")

# 放大连接模式里"选中"的那块瓷砖，看那圈怪东西到底是什么
with contextlib.suppress(Exception):
    application.page.canvas.yview_moveto(0.0)
    root.update()
    time.sleep(0.3)
    x = root.winfo_rootx() + 60
    y = root.winfo_rooty() + 330
    img = ImageGrab.grab(bbox=(x, y, x + 420, y + 130))
    img = img.resize((img.width * 3, img.height * 3))
    img.save(os.path.join(out_dir, "tile-zoom.png"))
    print("saved tile zoom", flush=True)


# 亮色主题
application.theme_var.set("亮色")
application.on_theme_change()

shoot(root, "client-light.png")

# 放大看一个卡片的左上角（圆角在这里）
img = ImageGrab.grab(
    bbox=(
        root.winfo_rootx() + 8,
        root.winfo_rooty() + 60,
        root.winfo_rootx() + 260,
        root.winfo_rooty() + 230,
    )
)
img = img.resize((img.width * 3, img.height * 3))
img.save(os.path.join(out_dir, "client-corner-zoom.png"))
print("saved corner zoom", flush=True)

# ---- 登录窗口（二级窗口也做了圆角）----
from mclink.gui import auth_ui  # noqa: E402

dialog = auth_ui.LoginDialog(
    root, application.engine, "127.0.0.1", 19100,
    username="", client_id="shot",
)

dialog.update_idletasks()
dialog.update()
time.sleep(0.8)
dialog.update_idletasks()
dialog.update()
time.sleep(0.4)

# 把登录框挪到屏幕中间再抓图：它平时是"居中到主窗口"的，而脚本里主窗口比屏幕
# 还宽，于是它会跑到屏幕外，截出来只有半张。
with contextlib.suppress(Exception):
    dialog.geometry(
        f"+{max(0, (dialog.winfo_screenwidth() - dialog.winfo_width()) // 2)}+80"
    )
    dialog.lift()

dialog.update_idletasks()
dialog.update()
time.sleep(0.4)

x = dialog.winfo_rootx()
y = dialog.winfo_rooty()
w = dialog.winfo_width()
h = dialog.winfo_height()
print("dialog geometry", x, y, w, h, flush=True)

img = ImageGrab.grab(bbox=(x - 4, y - 4, x + w + 4, y + h + 4))
img.save(os.path.join(out_dir, "client-login-dialog.png"))
print("saved login dialog", img.size, flush=True)

with contextlib.suppress(Exception):
    dialog.grab_release()
    dialog.destroy()

root.destroy()
