"""
MC-Link client -- V2 dialogs: login, and the live room list.

Dialogs use CustomTkinter views. They never touch a socket on the GUI thread:
every network call is handed to `AsyncTask`, which runs it on a worker thread
and delivers the result back through a queue that the dialog polls with
`after()`. That keeps the window responsive and prevents the classic
"tkinter freezes while connecting" problem.
"""

import contextlib
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from ..config import settings as cfgmod

from .app import (
    ACCENT,
    Card,
    BAD,
    BG,
    BORDER,
    CARD,
    CARD_ALT,
    CheckBox,
    ENTRY_STYLE,
    EyeButton,
    FG,
    FG_DIM,
    FG_MUTED,
    FONT,
    FONT_BOLD,
    FONT_MONO,
    FONT_SMALL,
    LOG_BG,
    LOG_FG,
    OK,
    ON_ACCENT,
    RoundButton,
    TILE_ON,
    WARN,
)

# 对话框的文字也跟随界面语言。翻不到就显示中文原文（tr 的兜底行为），
# 所以这里漏掉一句最坏也只是那句还是中文，绝不会变成空白或报错。
from .widgets import Dialog, Frame, Label, Entry, Button
from .i18n import tr  # noqa: E402  (放在 app 常量之后，保持导入分组清晰)


def refresh_palette():
    """
    重新从 app 模块取一遍配色。

    The import above is a SNAPSHOT of the values, which was fine while the
    palette could never change. Now that 暗色/亮色 can be switched at runtime,
    the dialogs would keep the old colours unless somebody copies the new ones
    over -- this is that copy.

    Called by app.py right after a theme change, before any dialog is built.
    """
    global ACCENT, BAD, BG, BORDER, CARD, CARD_ALT, FG, FG_DIM, FG_MUTED
    global LOG_BG, LOG_FG, OK, TILE_ON, WARN

    from . import app as app_mod

    ACCENT = app_mod.ACCENT
    BAD = app_mod.BAD
    BG = app_mod.BG
    BORDER = app_mod.BORDER
    CARD = app_mod.CARD
    CARD_ALT = app_mod.CARD_ALT
    FG = app_mod.FG
    FG_DIM = app_mod.FG_DIM
    FG_MUTED = app_mod.FG_MUTED
    LOG_BG = app_mod.LOG_BG
    LOG_FG = app_mod.LOG_FG
    OK = app_mod.OK
    TILE_ON = app_mod.TILE_ON
    WARN = app_mod.WARN

POLL_MS = 100


# ============================================================
# 对话框里的小零件（和主界面同一套画法）
# ============================================================
#
# 二级面板以前用的是 tk.Button + tk.Checkbutton + 原生 ttk.Entry 的默认样式，
# 和主界面那套圆角按钮/卡片放在一起就是两个时代的界面。下面这三个小工厂把
# 登录 / 改名对话框统一到主界面那一套控件上 —— 只换控件，不动任何流程。

def _button(parent, text, command, primary=False, width=0):
    """
    对话框按钮：主界面用的那个 RoundButton。

    primary=True  -> 强调色实心（这个框里"要做的事"，一个对话框只该有一个）
    其余          -> CARD_ALT 低调面 + 常规字重（"取消 / 另一条路"）
    """
    return RoundButton(
        parent,
        text=tr(text),
        command=command,
        width=width,
        primary=primary,
        bg=ACCENT if primary else CARD_ALT,
        fg=ON_ACCENT if primary else FG,
        font=FONT_BOLD if primary else FONT,
    )


def _field_label(parent, text, bg=None):
    """
    输入框上方那行字段名：小一号、FG_DIM（和主界面的字段名一致）。

    注意 `bg` 的默认值必须是 None、在函数体里再取 CARD：写成 `bg=CARD` 会把
    **导入那一刻**的配色钉死在默认参数上，切成亮色主题之后这几行字就顶着
    暗色的底 —— 亮色卡片上一条黑杠（这次截图里就是这么发现的）。
    """
    return Label(
        parent,
        text=tr(text),
        bg=bg or CARD,
        fg=FG_DIM,
        font=FONT_SMALL,
        anchor="w",
    )


def _entry(parent, textvariable, width=None, show=None):
    """现代输入框：样式定义在 app.MCLinkApp._build_styles() 的 Modern.TEntry。"""
    options = {"textvariable": textvariable, "style": ENTRY_STYLE}

    if width is not None:
        options["width"] = max(70, width * 9)

    if show is not None:
        options["show"] = show

    return Entry(parent, **options)


# ============================================================
# Background task helper
# ============================================================

class AsyncTask:
    """
    Run `func(*args)` on a worker thread and poll for the result.

    Usage:
        task = AsyncTask(widget, func, on_success=..., on_error=...)
        task.start()
    """

    def __init__(self, widget, func, on_success=None, on_error=None,
                 on_finally=None):
        self.widget = widget
        self.func = func
        self.on_success = on_success
        self.on_error = on_error
        self.on_finally = on_finally
        self.queue = queue.Queue(maxsize=1)
        self.thread = None
        self.running = False

    def start(self, *args, **kwargs):
        if self.running:
            return

        self.running = True
        self.thread = threading.Thread(
            target=self._run, args=args, kwargs=kwargs, daemon=True
        )
        self.thread.start()

        self.widget.after(POLL_MS, self._poll)

    def _run(self, *args, **kwargs):
        try:
            result = self.func(*args, **kwargs)
            self.queue.put(("ok", result))
        except Exception as e:  # noqa: BLE001 - reported to the UI
            self.queue.put(("error", e))

    def _poll(self):
        try:
            kind, payload = self.queue.get_nowait()
        except queue.Empty:
            if self.running:
                self.widget.after(POLL_MS, self._poll)
            return

        self.running = False

        try:
            if kind == "ok":
                if self.on_success:
                    self.on_success(payload)
            else:
                if self.on_error:
                    self.on_error(payload)
                else:
                    messagebox.showerror("操作失败", str(payload))
        finally:
            if self.on_finally:
                with contextlib.suppress(Exception):
                    self.on_finally()


# ============================================================
# Login dialog
# ============================================================

class LoginDialog(Dialog):
    """
    Account login.

    Returns `self.result` (an AuthSession) when the user logs in, or None when
    they close the dialog. The caller reads it after wait_window().
    """

    def __init__(self, master, engine, host, port, username="", client_id="",
                 allow_cancel=True, reason=""):
        super().__init__(master)

        self.engine = engine
        self.client_id = client_id
        self.result = None
        self.allow_cancel = allow_cancel

        self.title(tr("CraftLink 登录"))
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Created BEFORE _build(), because _build() reaches it through
        # _prefill_from_store() -> _status() while the rest of the widgets are
        # still being constructed. Leaving it undefined there raised
        # AttributeError part way through _build, which aborted the rest of the
        # dialog: no password box, no buttons, nothing to click. That only
        # happened when a stored password existed, so it looked like "remember
        # my password breaks the login window on the second launch".
        self.status_label = None

        self._build(host, port, username, reason)

        # The window has no usable size until Tk has processed the geometry of
        # everything just packed, so this must come first: without it
        # winfo_reqwidth() reports ~1 and the centring maths throws the dialog
        # into the top-left corner.
        self.update_idletasks()
        self._center_on(master)

        with contextlib.suppress(Exception):
            self.grab_set()

        # Focus the password box when there is one to fill in, otherwise the
        # login button, so Enter and typing always go somewhere useful.
        self.after(120, self._focus_first_field)

    def _focus_first_field(self):
        """Focus the password box when a username is already filled in."""
        with contextlib.suppress(Exception):
            if self.password_entry is not None and self.username_var.get():
                self.password_entry.focus_set()
            elif self.login_button is not None:
                self.login_button.focus_set()

    # --------------------------------------------------------

    def _build(self, host, port, username, reason):
        # 「一点空气」：四周留白从 22/20 提到 26/24，块与块之间固定用 6 / 10 /
        # 14 三档间距。二级面板看着挤，一半原因就是每样东西都贴着下一件。
        outer = Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=26, pady=24)

        Label(
            outer,
            text=tr("登录 MC-Link 账号"),
            bg=BG,
            fg=FG,
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(anchor="w")

        Label(
            outer,
            text=tr("房主和玩家都需要账号（服务器已开启账号验证）"),
            bg=BG,
            fg=FG_DIM,
            font=FONT_SMALL,
        ).pack(anchor="w", pady=(6, 0))

        if reason:
            # reason 是调用方给的句子（例如"切换账号会断开当前连接…"）。以前
            # 直接贴上去，切到英文/日文时这一行还是中文；过一遍 tr() 用的是
            # 表里本来就有的译文，中文下原样返回。
            Label(
                outer,
                text=tr(reason),
                bg=BG,
                fg=WARN,
                font=FONT_SMALL,
                wraplength=380,
                justify="left",
            ).pack(anchor="w", pady=(10, 0))

        # 圆角面板：和主界面卡片用同一套画法（纯观感，控件一个没动）
        form = Card(outer)
        form.pack(fill="x", pady=(16, 0))

        body = form.body

        # ---- server ----
        _field_label(body, "中转服务器").pack(fill="x")

        server_row = Frame(body, bg=CARD)
        server_row.pack(fill="x", pady=(6, 14))

        self.host_var = tk.StringVar(value=host)
        self.host_entry = _entry(server_row, self.host_var)
        self.host_entry.pack(side="left", fill="x", expand=True)

        self.port_var = tk.StringVar(value=str(port))
        self.port_entry = _entry(server_row, self.port_var, width=8)
        self.port_entry.pack(side="left", padx=(10, 0))

        # ---- username ----
        _field_label(body, "用户名").pack(fill="x")

        self.username_var = tk.StringVar(value=username)
        self.username_entry = _entry(body, self.username_var)
        self.username_entry.pack(fill="x", pady=(6, 14))

        # ---- password ----
        _field_label(body, "密码").pack(fill="x")

        password_row = Frame(body, bg=CARD)
        password_row.pack(fill="x", pady=(6, 0))

        self.password_var = tk.StringVar()
        self.password_entry = _entry(
            password_row, self.password_var, show="●"
        )
        self.password_entry.pack(side="left", fill="x", expand=True)
        self.password_entry.bind("<Return>", lambda _e: self.on_login())

        # 「显示密码」从复选框换成小眼睛：不再是系统方框，而且只占一个图标位。
        # 变量和语义一个字没变（show_password_var + _toggle_password）。
        self.show_password_var = tk.BooleanVar(value=False)

        self.eye_button = EyeButton(
            password_row,
            revealed=self.show_password_var.get(),
            command=self._on_eye_click,
        )
        self.eye_button.pack(side="left", padx=(10, 0))

        # ---- remember the password ----
        #
        # Opt in, never on by default: storing a password is a decision the
        # user makes, not one made for them.
        #
        # 自绘的圆角复选框（CheckBox），不是系统那个方框；变量还是同一个
        # remember_var，下面的 _prefill_from_store / _store_credential 读的
        # 还是它。
        self.remember_var = tk.BooleanVar(value=False)

        self.remember_box = CheckBox(
            body,
            text=tr("记住密码（加密保存在本机；每次更新版本后需重新输入一次）"),
            variable=self.remember_var,
            bg=CARD,
            fg=FG_DIM,
            font=FONT_SMALL,
            wraplength=360,
        )
        self.remember_box.pack(anchor="w", pady=(14, 0))

        # Fill in whatever was stored, unless the caller already supplied a
        # username (a deliberate "switch account" should win).
        self._prefill_from_store(username)

        # ---- status ----
        self.status_label = Label(
            outer, text="", bg=BG, fg=FG_MUTED, font=FONT_SMALL,
            wraplength=380, justify="left",
        )
        self.status_label.pack(fill="x", pady=(14, 0))

        # ---- buttons ----
        buttons = Frame(outer, bg=BG)
        buttons.pack(fill="x", pady=(12, 0))

        self.login_button = _button(buttons, "登录", self.on_login, primary=True)
        self.login_button.pack(side="left")

        if self.allow_cancel:
            _button(buttons, "取消", self._on_close).pack(
                side="left", padx=(10, 0)
            )

        Label(
            outer,
            text=tr("账号由管理员创建；忘记密码请联系服主重置。"),
            bg=BG,
            fg=FG_MUTED,
            font=FONT_SMALL,
            wraplength=380,
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

    # --------------------------------------------------------

    def _center_on(self, master):
        """
        Centre the dialog over the main window, clamped to the screen.

        The clamping matters: when the main window has not been laid out yet
        (which is the case on a fresh launch, because the login dialog is
        raised before the main window is mapped), its width reads as 1 and the
        naive centring maths puts the dialog far off to the left -- on screen
        it appears wedged into the top-left corner with its right-hand side
        clipped away.
        """
        with contextlib.suppress(Exception):
            self.update_idletasks()

            width = self.winfo_reqwidth() or 420
            height = self.winfo_reqheight() or 480

            master_width = master.winfo_width()
            master_x = master.winfo_rootx()

            if master_width > 100:
                x = master_x + (master_width // 2) - (width // 2)
            else:
                # The main window is not laid out yet (or is not there at all):
                # centre on the screen instead of on a 1 pixel rectangle.
                x = (self.winfo_screenwidth() // 2) - (width // 2)

            y = master.winfo_rooty() + 80

            # Never let any edge go off screen, whatever the arithmetic said.
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()

            x = max(0, min(x, screen_w - width))
            y = max(0, min(y, screen_h - height - 40))

            self.geometry(f"+{int(x)}+{int(y)}")

    def _prefill_from_store(self, requested_username):
        """
        Prefill the form from the stored credential, if there is one.

        Two separate things are restored:
          * the remember CHOICE, which survives app updates
          * the password itself, which does NOT (every update asks once more)

        So after an update this leaves the checkbox ticked and the password box
        empty, which is the intended behaviour rather than a bug: the password
        is typed once and remembered again from then on.

        Wrapped as a whole: this runs in the middle of building the dialog, and
        a problem reading a preference file must never be able to leave the
        user staring at a half-built window with no way to log in.
        """
        try:
            self._prefill_from_store_inner(requested_username)
        except Exception:
            # Nothing to restore, and definitely nothing to report: the dialog
            # works fine without a stored password.
            pass

    def _prefill_from_store_inner(self, requested_username):
        from ..config import credentials

        # The choice first, so an update does not silently switch the feature
        # off. Somebody who never opted in still gets an unticked box.
        try:
            self.remember_var.set(credentials.remember_choice())
        except Exception:
            pass

        try:
            stored_user, stored_password = credentials.load(
                cfgmod.APP_VERSION
            )
        except Exception:
            return

        if not stored_user or not stored_password:
            if self.remember_var.get():
                self._status(
                    "已更新到新版本，请再输一次密码（之后会继续记住）",
                    WARN,
                )
            return

        # An explicit username from the caller means "switch account", so the
        # stored password does not match and must not be offered.
        if requested_username and requested_username != stored_user:
            return

        self.username_var.set(stored_user)
        self.password_var.set(stored_password)
        self.remember_var.set(True)

        self._status(f"已填入保存的账号 {stored_user}", OK)

    def _on_eye_click(self):
        """
        小眼睛被点了：翻 show_password_var，然后走原来那条同步逻辑。

        和以前 tk.Checkbutton 的 command= 完全一样的两步（改变量 -> 同步），
        所以任何读 show_password_var 的老代码看到的还是同一个值。
        """
        self.show_password_var.set(not self.show_password_var.get())
        self._toggle_password()

    def _toggle_password(self):
        """
        按 show_password_var 把密码框在小圆点 / 明文之间切换。

        这是唯一一处动 `show=` 的地方（原来也是），另外顺手把那只小眼睛的
        样子同步过去 —— 眼睛只是这个变量的一个"显示"，不是第二个状态源。
        """
        self.password_entry.configure(
            show="" if self.show_password_var.get() else "●"
        )

        eye = getattr(self, "eye_button", None)

        if eye is not None:
            with contextlib.suppress(Exception):
                eye.set_revealed(bool(self.show_password_var.get()))

    def _on_close(self):
        if not self.allow_cancel:
            if not messagebox.askokcancel(
                "退出", "还没有登录，确定要退出 CraftLink 吗？"
            ):
                return

        self.result = None
        with contextlib.suppress(Exception):
            self.grab_release()
        self.destroy()

    # --------------------------------------------------------

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.login_button.configure(
            state=state, bg=CARD_ALT if busy else ACCENT
        )

    def _status(self, text, colour=FG_MUTED):
        """
        Show a status line. Never raises.

        The label may not exist yet (this is called during _build), and a
        missing status line must never be able to abort the dialog -- that is
        exactly how the login window once came up with no buttons at all.
        """
        label = getattr(self, "status_label", None)

        if label is None:
            return

        with contextlib.suppress(Exception):
            label.configure(text=text, fg=colour)

    def on_login(self):
        host = self.host_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get()

        if not host:
            messagebox.showerror("参数错误", "请填写中转服务器地址", parent=self)
            return

        try:
            port = int(self.port_var.get())
        except (TypeError, ValueError):
            messagebox.showerror("参数错误", "端口必须是数字", parent=self)
            return

        if not username:
            messagebox.showerror("参数错误", "请填写用户名", parent=self)
            return

        if not password:
            messagebox.showerror("参数错误", "请填写密码", parent=self)
            return

        self._set_busy(True)
        self._status(f"正在登录 {host}:{port} ...", WARN)

        AsyncTask(
            self,
            self.engine.login,
            on_success=self._on_success,
            on_error=self._on_error,
            on_finally=lambda: self._set_busy(False),
        ).start(host, port, username, password, self.client_id)

    def _on_success(self, session):
        self.result = session
        self._status(f"登录成功：{session.summary()}", OK)

        self._store_credential(session)

        with contextlib.suppress(Exception):
            self.grab_release()

        self.after(120, self.destroy)

    def _store_credential(self, session):
        """
        Save or forget the password according to the checkbox.

        Only ever called AFTER a successful login, so a typo can never be
        stored and then replayed on the next launch.
        """
        from ..config import credentials

        if not self.remember_var.get():
            # Unticking the box must actually forget it, otherwise "don't
            # remember" would silently keep the old password on disk.
            credentials.clear()
            return

        saved = credentials.save(
            session.username,
            self.password_var.get(),
            cfgmod.APP_VERSION,
        )

        if not saved:
            self._status(
                "登录成功，但密码没能保存到本机（可能是目录权限问题）",
                WARN,
            )

    def _on_error(self, error):
        message = str(error)
        self._status(message, BAD)
        self.password_var.set("")
        self.password_entry.focus_set()


def ask_login(master, engine, host, port, username="", client_id="",
              allow_cancel=True, reason=""):
    """Show the login dialog and return an AuthSession (or None)."""
    dialog = LoginDialog(
        master,
        engine,
        host,
        port,
        username=username,
        client_id=client_id,
        allow_cancel=allow_cancel,
        reason=reason,
    )

    master.wait_window(dialog)

    return dialog.result


# ============================================================
# Room list dialog
# ============================================================

class RoomsDialog(Dialog):
    """
    Live room browser.

    * 刷新   reload the list from the relay
    * 加入   copy the selected room id into the main window
    * 删除   only for a room you host AND that has no players; the server
             refuses it otherwise (DELETE_ROOM_BUSY), which is shown here.
    """

    def __init__(self, master, engine, host, port, session, client_id,
                 on_join=None):
        super().__init__(master)

        self.engine = engine
        self.host = host
        self.port = port
        self.session = session          # AuthSession
        self.client_id = client_id
        self.on_join = on_join

        self.rooms = []

        # 自动刷新状态（见 _schedule_auto_refresh）
        self._auto_refresh_on = True
        self._auto_timer = None
        self._busy = False

        self.title("CraftLink 房间列表")
        self.configure(bg=BG)
        # Bigger than the old 720x460: the fonts grew by a point, the button
        # row needs ~520 px for its labels, and the table gained a row of
        # height. Opening at a size where the buttons are already squeezed is
        # the whole bug being fixed here.
        self.geometry("820x560")
        self.minsize(760, 460)
        self.transient(master)

        self._build()
        self._center_on(master)

        with contextlib.suppress(Exception):
            self.grab_set()

        self.refresh()
        self._schedule_auto_refresh()

    # --------------------------------------------------------

    def _build(self):
        outer = Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=18, pady=16)

        header = Frame(outer, bg=BG)
        header.pack(fill="x")

        Label(
            header, text="在线房间", bg=BG, fg=FG,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(side="left")

        self.account_label = Label(
            header,
            text=f"登录账号：{self.session.summary()}" if self.session else "",
            bg=BG,
            fg=FG_MUTED,
            font=FONT_SMALL,
        )
        self.account_label.pack(side="right")

        Label(
            outer,
            text=(
                "只有「你自己开的」并且「房间里没有玩家」的房间才能删除。"
                "房主也可以在主界面点「删除我的房间」。"
            ),
            bg=BG,
            fg=FG_DIM,
            font=FONT_SMALL,
            wraplength=660,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        # ---- table ----
        table_frame = Card(outer)
        table_frame.pack(fill="both", expand=True, pady=(12, 0))

        columns = ("room", "owner", "host", "inside", "players", "tunnels",
                   "state")

        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=10,
            # Dark.Treeview is defined in app.py's _build_styles; a Treeview
            # does not inherit the dark palette and defaults to white.
            style="Craft.Treeview",
        )

        headings = {
            "room": ("房间号", 160),
            # 房主 = 建房的人（房间一建出来就有名字）。
            "owner": ("房主（建房的人）", 140),
            # 主机 = 当前开世界的那个位置，可能还没人认领。
            "host": ("当前主机", 120),
            # 房间内 = 人在房间里（可能还没进世界）。
            # 这一列和"玩家"分开显示：两个人待在房间里但没人开服时，
            # 只看"玩家"会显示 0，看着像房间是空的。
            "inside": ("房间内", 80),
            "players": ("已加入世界", 100),
            "tunnels": ("空闲隧道", 90),
            "state": ("状态", 80),
        }

        for key, (title, width) in headings.items():
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="center")

        self.tree.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview,
            style="Dark.Vertical.TScrollbar",
        )
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.tag_configure("busy", foreground=WARN)
        self.tree.tag_configure("empty", foreground=OK)
        self.tree.tag_configure("mine", background=TILE_ON)
        self.tree.tag_configure("mine_busy", background=TILE_ON, foreground=WARN)

        self.tree.bind("<Double-1>", lambda _e: self.on_join_selected())

        # ---- status ----
        self.status_label = Label(
            outer, text="正在加载 ...", bg=BG, fg=FG_MUTED, font=FONT_SMALL
        )
        self.status_label.pack(fill="x", pady=(10, 0))

        # ---- buttons ----
        buttons = Frame(outer, bg=BG)
        buttons.pack(fill="x", pady=(8, 0))

        # Explicit widths so Tk cannot squeeze these when the window is a
        # little narrow. Left-packed buttons lose their label before they lose
        # their padding, so without a width they collapse into unreadable
        # stubs -- which is exactly what happened once the fonts grew.
        #
        # The widths are in TEXT UNITS (average character), and every label
        # here is Chinese, which renders about twice as wide as the Latin
        # average Tk assumes. "加入选中的房间" is 7 characters, so it needs
        # roughly 14 units to breathe.
        self.refresh_button = self._button(
            buttons, "刷新", self.refresh, primary=True, width=6
        )
        self.refresh_button.pack(side="left")

        self.join_button = self._button(
            buttons, "加入选中的房间", self.on_join_selected, width=15
        )
        self.join_button.pack(side="left", padx=(8, 0))

        self.delete_button = self._button(
            buttons, "删除选中的房间", self.on_delete, danger=True, width=15
        )
        self.delete_button.pack(side="left", padx=(8, 0))

        self._button(buttons, "关闭", self._close, width=8).pack(side="right")

    def _button(self, parent, text, command, primary=False, danger=False,
                width=None):
        if primary:
            bg, fg = ACCENT, "#ffffff"
        elif danger:
            bg, fg = "#43262a", BAD
        else:
            bg, fg = CARD_ALT, FG

        options = {}

        if width is not None:
            options["width"] = max(70, width * 9)

        return Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            font=FONT_BOLD if primary else FONT,
            **options,
        )

    def _center_on(self, master):
        with contextlib.suppress(Exception):
            # Half of the dialog's REAL width, not a hardcoded 360 that was
            # half of the old 720. Hardcoding it drifted the window off centre
            # as soon as the size changed.
            width = self.winfo_reqwidth() or 820
            x = master.winfo_rootx() + (master.winfo_width() // 2) - width // 2
            y = master.winfo_rooty() + 70
            self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _close(self):
        self._auto_refresh_on = False

        if getattr(self, "_auto_timer", None) is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._auto_timer)
            self._auto_timer = None

        with contextlib.suppress(Exception):
            self.grab_release()
        self.destroy()

    # --------------------------------------------------------

    def _set_busy(self, busy):
        for button in (self.refresh_button, self.join_button,
                       self.delete_button):
            button.configure(state="disabled" if busy else "normal")

    def _schedule_auto_refresh(self):
        """
        这个窗口每隔几秒自己刷新一次。

        以前只有点「刷新」才会更新，于是房主一断开、主机一释放，列表里还挂着
        上一次看到的"空闲隧道 3"和旧主机名 —— 看起来像状态没变，其实是列表
        根本没再问过服务器。这是个只读监控窗口，让它自动跟着变才对。
        """
        if not self._auto_refresh_on:
            return

        def tick():
            self._auto_timer = None

            if not self._auto_refresh_on:
                return

            # 正在忙（上一次还没回来）就跳过这一拍，别堆请求。
            if not self._busy:
                self.refresh(silent=True)

            self._schedule_auto_refresh()

        with contextlib.suppress(Exception):
            self._auto_timer = self.after(4000, tick)

    def refresh(self, silent=False):
        self._busy = True
        self._set_busy(True)

        if not silent:
            self.status_label.configure(text="正在获取房间列表 ...", fg=WARN)

        AsyncTask(
            self,
            self.engine.fetch_rooms,
            on_success=self._on_rooms,
            on_error=self._on_rooms_error,
            on_finally=self._refresh_done,
        ).start(
            self.host,
            self.port,
            self.client_id,
            self.session.username if self.session else "",
        )

    def _refresh_done(self):
        self._busy = False
        self._set_busy(False)

    def _on_rooms(self, rooms):
        self.rooms = rooms

        for item in self.tree.get_children():
            self.tree.delete(item)

        mine = (self.session.username if self.session else "") or ""

        for index, room in enumerate(rooms):
            # ONE tag per row, chosen explicitly. Relying on tag precedence
            # (does "mine" override "busy"?) is exactly the kind of thing that
            # silently renders the wrong colour, so the combinations are
            # spelled out.
            #
            # "Mine" now means "I created this room" (owner), not "I happen to
            # hold the host slot": the room belongs to its creator.
            is_mine = bool(mine) and room.owner == mine

            if is_mine and room.players > 0:
                tag = "mine_busy"
            elif is_mine:
                tag = "mine"
            elif room.players > 0:
                tag = "busy"
            else:
                tag = "empty"

            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    room.room,
                    room.owner or "-",
                    # 没人认领主机时是一个破折号，而不是空白 ——
                    # 空白看着像"这一列坏了"。
                    room.host or "（还没人认领）",
                    room.members,
                    room.players_text(),
                    room.idle_tunnels,
                    room.state_text,
                ),
                tags=(tag,),
            )

        busy = sum(1 for r in rooms if r.players > 0)

        if rooms:
            self.status_label.configure(
                text=(
                    f"共 {len(rooms)} 个房间，其中 {busy} 个有玩家"
                    f"（{time.strftime('%H:%M:%S')} 刷新）"
                ),
                fg=OK,
            )
        else:
            self.status_label.configure(
                text="当前没有在线房间（让房主先点「开始连接」）",
                fg=FG_MUTED,
            )

    def _on_rooms_error(self, error):
        self.status_label.configure(text=f"获取失败：{error}", fg=BAD)

    # --------------------------------------------------------

    def _selected(self):
        selection = self.tree.selection()

        if not selection:
            return None

        try:
            return self.rooms[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def on_join_selected(self):
        room = self._selected()

        if room is None:
            messagebox.showinfo("提示", "请先选中一个房间", parent=self)
            return

        if self.on_join:
            self.on_join(room.room)

        self.status_label.configure(
            text=f"已选择房间 {room.room}，回到主界面点「开始连接」",
            fg=OK,
        )

    def on_delete(self):
        room = self._selected()

        if room is None:
            messagebox.showinfo("提示", "请先选中一个房间", parent=self)
            return

        mine = (self.session.username if self.session else "") or ""

        # ---- client side guard (the server enforces this too) ----
        #
        # 能删房的是【建房的人（所有者）】或者当前持有主机身份的人 —— 这两种
        # 都可能被中继接受。以前只看房主那一列，于是"我是建房的但还没认领
        # 主机"会被自己客户端拦住。
        if mine and mine not in (room.owner, room.host):
            messagebox.showwarning(
                "不能删除",
                f"房间 {room.room} 是「{room.owner or '未知'}」建的，"
                f"当前主机是「{room.host or '还没人认领'}」，都不是你。\n"
                "只有建房的人或当前主机能删除房间。",
                parent=self,
            )
            return

        if room.players > 0:
            messagebox.showwarning(
                "不能删除",
                f"房间 {room.room} 里还有 {room.players} 名玩家。\n\n"
                "请先让玩家退出，或者让服主在管理面板强制关闭。",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "确认删除",
            f"确定要删除房间 {room.room} 吗？\n\n"
            "删除后房间会立即关闭，其他玩家将无法加入。",
            parent=self,
        ):
            return

        self._set_busy(True)
        self.status_label.configure(text=f"正在删除房间 {room.room} ...", fg=WARN)

        AsyncTask(
            self,
            self.engine.delete_room,
            on_success=self._on_deleted,
            on_error=self._on_delete_error,
            on_finally=lambda: self._set_busy(False),
        ).start(
            self.host,
            self.port,
            room.room,
            self.session,
            self.client_id,
        )

    def _on_deleted(self, result):
        ok, message = result

        if ok:
            self.status_label.configure(text=message, fg=OK)
            self.refresh()
        else:
            self.status_label.configure(text=message, fg=BAD)
            messagebox.showwarning("删除失败", message, parent=self)

    def _on_delete_error(self, error):
        self.status_label.configure(text=f"删除失败：{error}", fg=BAD)
        messagebox.showerror("删除失败", str(error), parent=self)


def open_rooms_dialog(master, engine, host, port, session, client_id,
                      on_join=None):
    return RoomsDialog(
        master, engine, host, port, session, client_id, on_join=on_join
    )


# ============================================================
# Self-service rename dialog
# ============================================================

class RenameDialog(Dialog):
    """
    Rename the logged-in account.

    Self-service renames are rate limited by the relay (3 per 30 days) but they
    do NOT log the client out and do NOT stop a running room: the new name
    takes effect immediately and the live session simply starts reporting it.
    That is why this dialog can be opened while a room is running.
    """

    #: Monthly self-service allowance, shared with password changes. The relay
    #: is the enforcer; these numbers are only for the label.
    SELF_LIMIT = 3

    def __init__(self, master, engine, host, port, session, client_id=""):
        super().__init__(master)

        self.engine = engine
        self.host = host
        self.port = port
        self.session = session
        self.client_id = client_id
        self.result = None

        self.title(tr("CraftLink 修改用户名"))
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)

        self.protocol("WM_DELETE_WINDOW", self._close)

        self._build()

        self.update_idletasks()
        self._center_on(master)

        with contextlib.suppress(Exception):
            self.grab_set()

        self.after(120, self.name_entry.focus_set)

    # --------------------------------------------------------

    def _build(self):
        # 和登录框同一套留白 / 同一套字段样式（见 LoginDialog._build）。
        outer = Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=26, pady=24)

        Label(
            outer,
            text=tr("修改用户名"),
            bg=BG,
            fg=FG,
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(anchor="w")

        Label(
            outer,
            text=tr(
                "用户名就是游戏里和房间列表里显示的名字。改名立即生效，"
                "不会掉线，也不用重新登录。"
            ),
            bg=BG,
            fg=FG_DIM,
            font=FONT_SMALL,
            wraplength=400,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        # 圆角面板：和主界面卡片用同一套画法（纯观感，控件一个没动）
        form = Card(outer)
        form.pack(fill="x", pady=(16, 0))

        body = form.body

        current = (self.session.username if self.session else "") or "-"

        _field_label(body, "当前用户名").pack(fill="x")

        self.current_label = Label(
            body,
            text=current,
            bg=CARD,
            fg=FG,
            font=FONT_BOLD,
            anchor="w",
        )
        self.current_label.pack(fill="x", pady=(6, 14))

        _field_label(body, "新的用户名").pack(fill="x")

        self.name_var = tk.StringVar()

        self.name_entry = _entry(body, self.name_var, width=30)
        self.name_entry.pack(fill="x", pady=(6, 0))
        self.name_entry.bind("<Return>", lambda _event: self.on_rename())

        # The remaining allowance comes from the relay (it counts for BOTH the
        # client and the panel). Showing it before the attempt is the whole
        # point: nobody should discover a limit by being refused.
        #
        # 两句分开翻译：拼成一整句再查表的话，句式表的 ^...$ 谁也匹配不上
        # （这两句在表里各有一条），于是切到英文/日文时这一整段还是中文。
        allowance = ""

        if self.session is not None:
            allowance = self.session.self_ops_text()

        hint = tr("2-32 个字符，不能有空格。")

        if allowance:
            hint += tr(allowance)
        else:
            hint += tr(
                f"普通账号每月最多 {self.SELF_LIMIT} 次自助修改"
                "（改名和改密码共用）；管理员不受限制。"
            )

        Label(
            body,
            text=hint,
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            wraplength=380,
            justify="left",
        ).pack(fill="x", pady=(8, 0))

        self.status_label = Label(
            outer,
            text="",
            bg=BG,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            wraplength=400,
            justify="left",
        )
        self.status_label.pack(fill="x", pady=(14, 0))

        buttons = Frame(outer, bg=BG)
        buttons.pack(fill="x", pady=(12, 0))

        # 和登录框一样：RoundButton，主操作实心强调色，取消是低调面。
        self.rename_button = _button(
            buttons, "确认改名", self.on_rename, primary=True
        )
        self.rename_button.pack(side="left")

        _button(buttons, "取消", self._close).pack(side="right")

    def _center_on(self, master):
        with contextlib.suppress(Exception):
            x = master.winfo_rootx() + (master.winfo_width() // 2) - (
                self.winfo_width() // 2
            )
            y = master.winfo_rooty() + 130
            self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _close(self):
        with contextlib.suppress(Exception):
            self.grab_release()
        self.destroy()

    # --------------------------------------------------------

    def _set_busy(self, busy):
        self.rename_button.configure(
            state="disabled" if busy else "normal",
            bg=CARD_ALT if busy else ACCENT,
        )

    def on_rename(self):
        new_name = self.name_var.get().strip()
        current = (self.session.username if self.session else "") or ""

        # ---- client side validation (the server validates as well) ----
        if not new_name:
            messagebox.showerror("参数错误", "请填写新的用户名", parent=self)
            return

        if new_name == current:
            messagebox.showerror(
                "参数错误", "新用户名和当前用户名相同", parent=self
            )
            return

        if len(new_name) < 2:
            messagebox.showerror(
                "参数错误", "用户名至少 2 个字符", parent=self
            )
            return

        if len(new_name) > 32:
            messagebox.showerror(
                "参数错误", "用户名最长 32 个字符", parent=self
            )
            return

        if any(c.isspace() for c in new_name):
            messagebox.showerror(
                "参数错误", "用户名不能包含空格", parent=self
            )
            return

        if not messagebox.askokcancel(
            "确认改名",
            f"确定要把用户名从「{current}」改成「{new_name}」吗？\n\n"
            f"改名立即生效，不会掉线。\n"
            f"自助改名每 {self.RENAME_WINDOW_DAYS} 天最多 "
            f"{self.RENAME_LIMIT} 次。",
            parent=self,
        ):
            return

        self._set_busy(True)
        self.status_label.configure(text="正在改名 ...", fg=WARN)

        AsyncTask(
            self,
            self.engine.rename_account,
            on_success=self._on_success,
            on_error=self._on_error,
            on_finally=lambda: self._set_busy(False),
        ).start(
            self.host,
            self.port,
            new_name,
            self.session,
            self.client_id,
        )

    def _on_success(self, result):
        confirmed, message = result
        self.result = confirmed

        self.status_label.configure(text=message, fg=OK)
        self.current_label.configure(text=confirmed)
        self.name_var.set("")

        messagebox.showinfo(
            "改名成功",
            f"{message}\n\n"
            "如果房间正在运行，房间列表里的房主名字会在下一次刷新时更新。",
            parent=self,
        )

        self._close()

    def _on_error(self, error):
        message = str(error)
        self.status_label.configure(text=f"改名失败：{message}", fg=BAD)
        messagebox.showerror("改名失败", message, parent=self)


def ask_rename(master, engine, host, port, session, client_id=""):
    """Show the rename dialog and return the new username (or None)."""
    dialog = RenameDialog(master, engine, host, port, session, client_id)

    master.wait_window(dialog)

    return dialog.result
