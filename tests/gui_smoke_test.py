"""
MC-Link -- GUI smoke test.

Builds the complete tkinter interface (without entering mainloop), runs a few
event-loop iterations and tears it down. This catches layout/style/typo
errors that would otherwise only show up when a user double clicks the app.

A window may briefly flash on screen; that is expected.

Run:
    python tests/gui_smoke_test.py
"""

import contextlib
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail:
        line += f"  -- {detail}"

    # 控制台可能是 GBK，按钮图标（🔑 ⧉ ⟳ 这类）打印会抛 UnicodeEncodeError。
    # 那是测试输出的问题，不该让测试失败。
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    (PASSED if ok else FAILED).append(name)


def main():
    print("=" * 64)
    print("MC-Link GUI smoke test")
    print("=" * 64)

    try:
        import tkinter as tk
        from tkinter import ttk as _ttk
    except ImportError as e:
        check("tkinter available", False, str(e))
        return 1

    check("tkinter available", True)

    # ---- 用一份全新的配置目录，别读这台机器上真实的客户端配置 ----
    #
    # This test asserts the DEFAULT room capacity is 8. Reading the saved
    # config instead made it fail on a machine where the person had set 5 (a
    # perfectly normal thing to do) -- a test that fails because of the
    # tester's own settings teaches nobody anything.
    import tempfile

    sandbox = tempfile.mkdtemp(prefix="mclink-gui-smoke-")

    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox

    from mclink.config import settings
    from mclink.gui import app as gui_app

    try:
        root = tk.Tk()
    except Exception as e:
        check("Tk root can be created (display available)", False, repr(e))
        return 1

    check("Tk root can be created (display available)", True)

    try:
        root.withdraw()  # keep the smoke test unobtrusive

        application = gui_app.MCLinkApp(root)

        check("MCLinkApp constructs", True)

        # ★ M6/D10：客户端不再内置中继服务器（默认地址与端口为空），而
        # `_collect_config()` 会拒绝空地址/空端口。这套冒烟测试测的是界面
        # 本身，所以显式填一个本机地址 —— 是**测试**给的，不是客户端内置的。
        application.host_var.set("127.0.0.1")
        application.port_var.set("19100")

        # 身份必须在连接前显式选择（这是刻意的设计），所以后面的
        # _collect_config() 需要一个已选身份；这里选好，别让测试因为
        # "没选身份"而失败 —— 那条规则本身有单独的检查。
        # 身份选择器已被删除（用户要求）；连接身份由「谁持有主机位置」决定，
        # 这里只需要把三个数字"提交"好。

        # 房间号现在是必填且默认为空（不再预填 test001），测试自己填一个。
        application.room_var.set("smoketest")

        # Widgets that must exist.
        for attribute, label in (
            ("start_button", "start button"),
            ("stop_button", "stop button"),
            ("test_button", "test server button"),
            ("log_text", "log widget"),
            ("status_vars", "status variables"),
            ("mode_var", "mode selector"),
            ("login_button", "login button"),
            ("rooms_button", "room list button"),
            ("logout_button", "logout button"),
            ("rename_button", "rename button"),
            ("delete_room_button", "delete room button"),
            ("account_label", "account status label"),
        ):
            check(f"widget present: {label}", hasattr(application, attribute))

        check(
            "STUN mode is locked",
            application.mode_var.get() == gui_app.MODE_RELAY,
            f"mode={application.mode_var.get()}",
        )

        # ---- V2 account UI ----
        check(
            "starts logged out (account label warns)",
            application.auth is None
            and "未登录" in application.account_label.cget("text"),
            application.account_label.cget("text"),
        )

        check(
            "logout is disabled while logged out",
            str(application.logout_button["state"]) == "disabled",
            str(application.logout_button["state"]),
        )

        check(
            "rename is disabled while logged out",
            str(application.rename_button["state"]) == "disabled",
            str(application.rename_button["state"]),
        )

        check(
            "delete-room button starts disabled",
            str(application.delete_room_button["state"]) == "disabled",
            str(application.delete_room_button["state"]),
        )

        # ---- V2.1: copy address / scroll frame / player cap ----
        check(
            "widget present: copy-address button",
            hasattr(application, "copy_button")
            # 按钮文字现在前面带一个图标（纯观感），所以用 endswith
            and application.copy_button.cget("text").endswith("复制"),
        )

        check(
            "widget present: address label",
            hasattr(application, "addr_var")
            and application.addr_var.get() == "—",
            application.addr_var.get() if hasattr(application, "addr_var") else "",
        )

        check(
            # The control is the ROOM CAPACITY, not the number of tunnels. It
            # used to be labelled 人数上限 while actually setting how many idle
            # tunnels to pre-open, which is an implementation detail.
            "widget present: room capacity entry",
            hasattr(application, "max_players_var")
            and hasattr(application, "max_players_entry"),
            "少了人数上限输入框",
        )

        # ============================================================================
        #  第一次打开：三个数字必须是空的（用户要求）
        # ============================================================================
        #
        # 「第一次」= 这台机器没有任何配置文件。预填默认值会让人以为"默认就能用"，
        # 而端口填错是最难查的一类问题；用户要求玩家自己填一次。
        # 之后每次打开记忆上一次提交过的值。
        check(
            "第一次打开时三个数字是空的（要玩家自己填一次）",
            application.mc_port_var.get() == ""
            and application.local_port_var.get() == ""
            and application.max_players_var.get() == "",
            f"mc={application.mc_port_var.get()!r} "
            f"local={application.local_port_var.get()!r} "
            f"cap={application.max_players_var.get()!r}",
        )

        check(
            "空的输入框旁边那个「确定」不会崩（点了只是提示要填）",
            callable(getattr(application, "_commit_max_players", None))
            and callable(getattr(application, "_commit_port", None)),
            "缺少提交函数",
        )

        check(
            "the page is scrollable WITHOUT a visible scrollbar",
            hasattr(application, "page")
            and not any(
                isinstance(child, _ttk.Scrollbar)
                for child in application.page.winfo_children()
            ),
            str([type(c).__name__ for c in application.page.winfo_children()]),
        )

        # Copying with no address must warn instead of raising.
        original_showinfo = gui_app.messagebox.showinfo
        gui_app.messagebox.showinfo = lambda *a, **k: None
        try:
            application.addr_var.set("—")
            application.on_copy_address()
        finally:
            gui_app.messagebox.showinfo = original_showinfo

        check("copying without an address does not raise", True)

        # Copying a real guest address must fill the clipboard.
        application.addr_var.set("127.0.0.1:25566")
        application.on_copy_address()

        check(
            "copy puts the address on the clipboard",
            application.root.clipboard_get() == "127.0.0.1:25566",
            application.root.clipboard_get(),
        )

        # The room capacity must be REFUSED when out of range, not silently
        # clamped. Somebody who typed 20 and quietly got 8 would never learn
        # why their friends could not join.
        #
        # 校验发生在**按「确定」**的时候（用户要求：输入完不会立即生效）。
        # 所以这里走提交函数，而不是 _collect_config()。
        gui_app.messagebox.showwarning = lambda *a, **k: None

        # 先把三个数字都正常提交一次，作为"已提交的合法值"的基线。
        application.mc_port_var.set("25565")
        application._commit_port("mc_port", application.mc_port_var)
        application.local_port_var.set("25566")
        application._commit_port("local_port", application.local_port_var)
        application.max_players_var.set("8")
        application._commit_max_players()

        application.max_players_var.set("999")
        application._commit_max_players()

        check(
            "人数上限超出 8 时被拒绝（不会悄悄夹到 8）",
            application._committed.get("max_players") == "8",
            f"committed={application._committed.get('max_players')!r}",
        )

        # Half-typed / non-numeric is refused too.
        application.max_players_var.set("abc")
        application._commit_max_players()

        check(
            "人数上限填了非数字会被拒绝",
            application._committed.get("max_players") == "8",
            f"committed={application._committed.get('max_players')!r}",
        )

        # A valid value is accepted, and the tunnel count is derived from it
        # rather than being a separate thing the user has to set.
        application.max_players_var.set("6")
        application._commit_max_players()

        cfg = application._collect_config()

        check(
            "合法的上限被采用",
            cfg.max_players == 6,
            str(cfg.max_players),
        )

        check(
            "隧道条数由人数上限推导（一个玩家一条，房主不占隧道但要留余量）",
            cfg.max_tunnels == 6,
            str(cfg.max_tunnels),
        )

        # And the boundary values.
        application.max_players_var.set("8")
        application._commit_max_players()
        check(
            "上限正好等于服务器最大值 8 时被接受",
            application._collect_config().max_players == 8,
        )

        # 太小也要拒绝（同样是"按确定"时校验）。
        application.max_players_var.set("1")
        application._commit_max_players()

        check(
            "人数上限少于 2 会被拒绝（房间至少要房主一人）",
            application._committed.get("max_players") == "8",
            f"committed={application._committed.get('max_players')!r}",
        )

        check(
            "上限低于 2 会被拒绝（房间里至少有房主）",
            application._committed.get("max_players") == "8",
            f"committed={application._committed.get('max_players')!r}",
        )

        application.max_players_var.set("8")
        application._commit_max_players()

        # require_login() would open a MODAL login dialog and block the test,
        # so stub the interactive part out and only assert the decision logic.
        original_on_login = application.on_login
        application.on_login = lambda: None
        try:
            check(
                "require_login() reports not logged in",
                application.require_login() is False,
            )
        finally:
            application.on_login = original_on_login

        # Simulate a successful login without any network access.
        from mclink.core.control_client import AuthSession

        application.auth = AuthSession(
            token="test-token",
            username="tester",
            is_admin=True,
            protocol=2,
            server="127.0.0.1:19100",
        )
        application._refresh_account_ui()

        check(
            "account label renders a logged in session",
            # The label carries the ROLE now: root / admin / player. This
            # session is is_admin=True with no explicit role, so it reads as
            # 管理员.
            "tester" in application.account_label.cget("text")
            and "管理员" in application.account_label.cget("text"),
            application.account_label.cget("text"),
        )

        check(
            "角色色块按角色上色（root 橙 / admin 绿 / player 蓝）",
            application.role_badge.cget("text").strip() == "管理员"
            and application.role_badge.cget("bg") == gui_app.ROLE_COLORS["admin"],
            f"text={application.role_badge.cget('text')!r} "
            f"bg={application.role_badge.cget('bg')!r}",
        )

        check(
            "logout becomes available after login",
            str(application.logout_button["state"]) == "normal",
            str(application.logout_button["state"]),
        )

        check(
            # Renaming is deliberately allowed WHILE a session runs: the relay
            # patches the live name and never disconnects the player.
            "rename becomes available after login (even while running)",
            str(application.rename_button["state"]) == "normal",
            str(application.rename_button["state"]),
        )

        check(
            "the account card exposes rename in its button groups",
            any(
                application.rename_button in group
                for group in application._account_buttons()
            ),
        )

        check(
            "require_login() is satisfied once logged in",
            application.require_login() is True,
        )

        # Losing the token must reset the UI (no messagebox in the smoke test).
        original_showwarning = gui_app.messagebox.showwarning
        gui_app.messagebox.showwarning = lambda *a, **k: None
        try:
            application._handle_auth_lost("AUTH_INVALID_TOKEN")
        finally:
            gui_app.messagebox.showwarning = original_showwarning

        check(
            "losing the token resets the account UI",
            application.auth is None
            and "未登录" in application.account_label.cget("text"),
            application.account_label.cget("text"),
        )

        # Mode / config callbacks must not raise.
        application._on_mode_change()
        application._on_role_change()
        check("mode/config callbacks run", True)

        # ============================================================================
        #  身份选择器已经被删除（用户要求）
        # ============================================================================
        #
        # 它有三个问题：和真实身份无关、会把另一个端口框藏起来（间接影响开服）、
        # 默认值 host 导致第一次点没反应。现在三个数字全部平铺在「基准配置」里，
        # 每一个旁边都有「确定」。
        check(
            "身份选择器已删除（没有 role_var，也没有 host/player 瓷砖）",
            not hasattr(application, "role_var"),
            "role_var 还在 —— 用户要求删掉它",
        )

        check(
            "「基准配置」三个数字都平铺出来了（不再二选一）",
            all(
                hasattr(application, name)
                for name in (
                    "mc_port_var",
                    "local_port_var",
                    "max_players_var",
                    "mc_port_entry",
                    "local_port_entry",
                    "max_players_entry",
                )
            ),
            "少了输入框",
        )

        check(
            "每个数字旁边都有「确定」按钮（改完必须提交）",
            all(
                button is not None
                for button in getattr(application, "_config_rows", [])
            )
            and len(getattr(application, "_config_rows", [])) == 3,
            f"rows={len(getattr(application, '_config_rows', []))}",
        )

        # Config collection and validation.
        application.username_var.set("测试用户")
        application.room_var.set("smoke-test")
        application.mc_port_var.set("25565")
        application.local_port_var.set("25566")
        application.max_players_var.set("8")

        # 三个数字都要"按确定"才算提交（用户要求：输入完不会立即生效）。
        application._commit_port("mc_port", application.mc_port_var)
        application._commit_port("local_port", application.local_port_var)
        application._commit_max_players()

        config = application._collect_config()
        check(
            "form values validate into a ClientConfig",
            config.room == "smoke-test" and config.username == "测试用户",
            f"room={config.room} user={config.username} id={config.client_id}",
        )

        # Invalid input must raise a friendly ValueError.
        #
        # 房间名规则改过（用户要求"任何房间名都应可以建房间"）：空格、中文、
        # 斜杠现在**都是合法的**，协议层会把名字 percent-encode 成房间 ID。
        # 所以这里改成检查真正非法的那些：空、只有空格、控制字符。
        application.room_var.set("带 空格 的名字")
        try:
            space_config = application._collect_config()
            check(
                "房间名里的空格是允许的（新规则）",
                space_config.room == "带 空格 的名字",
                space_config.room,
            )
        except ValueError as e:
            check("房间名里的空格是允许的（新规则）", False, str(e))

        application.room_var.set("换行\n注入")
        try:
            application._collect_config()
            check("房间名里的控制字符被拒绝", False)
        except ValueError:
            check("房间名里的控制字符被拒绝", True)

        application.room_var.set("smoke-test")

        # 端口校验同样发生在"按确定"的时候。提交一个非数字必须被拒绝，
        # 而且**已提交的值不能变**（否则就会拿一个乱七八糟的端口去建隧道）。
        application.mc_port_var.set("not-a-port")
        application._commit_port("mc_port", application.mc_port_var)

        check(
            "非数字端口会被拒绝，且已提交的值不变",
            application._committed.get("mc_port") == "25565",
            f"committed={application._committed.get('mc_port')!r}",
        )

        application.mc_port_var.set("99999")
        application._commit_port("mc_port", application.mc_port_var)

        check(
            "超范围端口（>65535）会被拒绝，且已提交的值不变",
            application._committed.get("mc_port") == "25565",
            f"committed={application._committed.get('mc_port')!r}",
        )

        application.mc_port_var.set("25565")
        try:
            application._collect_config()
            check("提交后的端口能被正常使用", True)
        except ValueError as e:
            check("提交后的端口能被正常使用", False, str(e))

        application.mc_port_var.set("25565")

        # Log rendering + event pump.
        application.log("smoke test log line", kind="info")
        for _ in range(6):
            root.update_idletasks()
            root.update()

        application._drain_events()
        application._append_logs()
        application._refresh_status()

        content = application.log_text.get("1.0", "end")
        check(
            "log line reaches the log widget",
            "smoke test log line" in content,
        )

        # Simulated engine events must be handled without raising.
        for name, data in (
            ("control_online", {}),
            ("tunnel_ready", {"tunnel": 1}),
            ("session_start", {"session": "abc123", "tunnel": 1}),
            ("info", {"players": "2/10"}),
            ("session_end", {"session": "abc123"}),
            ("control_offline", {"reason": "test"}),
            ("minecraft_connected", {"connection": 1}),
            ("relay_connected", {"connection": 1}),
            ("relay_error", {"reason": "test"}),
            ("minecraft_disconnected", {"connection": 1}),
            ("listen_failed", {"reason": "test"}),
            ("listening", {"endpoint": "127.0.0.1:25566"}),
        ):
            application._handle_core_event(name, data)

        check("all core events render without raising", True)

        # Snapshot rendering for both roles.
        snapshots = [
            {
                "role": "host",
                "state": "ONLINE",
                "state_text": "运行中",
                "room": "smoke-test",
                "players": 2,
                "max_players": 10,
                "tunnels_ready": 8,
                "relay_ping_ms": 42.0,
                "mc_endpoint": "127.0.0.1:25565",
                "upload_rate": 2048.0,
                "download_rate": 65536.0,
                "bytes_to_relay": 1024 * 1024,
                "bytes_to_minecraft": 8 * 1024 * 1024,
                # 进程级流量账本（审计问题 6）：累计按通路分开 + 实时速度。
                # 卡片现在读的这几个字段，正好就是 engine.snapshot() 合进来的。
                "up_rate": 2048.0,
                "down_rate": 65536.0,
                "relay_up": 1024 * 1024,
                "relay_down": 8 * 1024 * 1024,
                "direct_up": 0,
                "direct_down": 0,
                "total_up": 1024 * 1024,
                "total_down": 8 * 1024 * 1024,
            },
            {
                "role": "player",
                "state": "ONLINE",
                "state_text": "运行中",
                "room": "smoke-test",
                "active_connections": 1,
                "bytes_to_relay": 12345,
                "bytes_to_minecraft": 54321,
                "upload_rate": 1024.0,
                "download_rate": 2048.0,
                "local_endpoint": "127.0.0.1:25566",
                "up_rate": 1024.0,
                "down_rate": 2048.0,
                # 这一条带一点**对端**流量：卡片必须把它和中转分开显示。
                "relay_up": 4096,
                "relay_down": 8192,
                "direct_up": 8192,
                "direct_down": 8192,
                "total_up": 12288,
                "total_down": 16384,
            },
            {},
        ]

        rendered = {}

        original_snapshot = application.engine.snapshot

        try:
            for snapshot in snapshots:
                application.last_snapshot = snapshot
                application.engine.snapshot = lambda s=snapshot: s
                application._refresh_status()

                rendered[snapshot.get("role") or "none"] = {
                    key: var.get()
                    for key, var in application.status_vars.items()
                }
        finally:
            application.engine.snapshot = original_snapshot

        check("status panel renders every role/state", True)

        check(
            "host snapshot renders players / ping / tunnels",
            # 已加入世界 is now its own row with a plain count. The
            # "房间内在线" row comes from the room membership channel, which
            # this snapshot has no session for, so it correctly shows a dash
            # rather than a misleading 0.
            rendered["host"]["players"] == "2"
            and "42 ms" in rendered["host"]["ping"]
            and "优秀" in rendered["host"]["ping"]
            and "8" in rendered["host"]["traffic"],
            str(rendered["host"]),
        )

        check(
            "没有加入房间时「房间内在线」显示 — 而不是 0",
            rendered["host"]["room_members"] == "—",
            rendered["host"]["room_members"],
        )

        check(
            "host snapshot renders the live speed line",
            "↑ 2.0 KB/s" in rendered["host"]["speed"]
            and "↓ 64.0 KB/s" in rendered["host"]["speed"],
            rendered["host"]["speed"],
        )

        check(
            "host traffic line shows cumulative totals",
            # 累计按通路拆开：中转 1.0 MB / 对端 0 B（审计问题 6）。
            "1.0 MB" in rendered["host"]["traffic"]
            and "8.0 MB" in rendered["host"]["traffic"]
            and "对端 0 B" in rendered["host"]["traffic"],
            rendered["host"]["traffic"],
        )

        check(
            "player snapshot renders the local endpoint traffic",
            rendered["player"]["role"] == "玩家 Player"
            # 累计流量现在按通路拆开（审计问题 6）：合计 12.0 KB（中转 4.0 KB
            # ／对端 8.0 KB）。
            and "12.0 KB" in rendered["player"]["traffic"]
            and "中转 4.0 KB" in rendered["player"]["traffic"]
            and "对端 8.0 KB" in rendered["player"]["traffic"],
            str(rendered["player"]),
        )

        check(
            "player latency shows a dash with no misleading value",
            rendered["player"]["ping"] == "—",
            rendered["player"]["ping"],
        )

        # ---- the 0.0 ms bug (regression) ---------------------
        application.engine.snapshot = lambda: {
            "role": "host",
            "state": "ONLINE",
            "state_text": "运行中",
            "relay_ping_ms": 0.0,
        }

        application._refresh_status()

        check(
            "a 0.0 ms latency renders as a value, never as a dash",
            application.status_vars["ping"].get().startswith("0.0 ms"),
            application.status_vars["ping"].get(),
        )

        check(
            "live speed row exists in the status panel",
            "speed" in application.status_vars,
        )

        application.engine.snapshot = original_snapshot

        # ============================================================
        # The login dialog must survive having a stored password.
        #
        # This is a REGRESSION TEST for a real bug: "remember my password"
        # worked on the first login and broke every launch afterwards. With a
        # stored credential, _prefill_from_store() ran during _build() and
        # reached self.status_label BEFORE it was created, so the
        # AttributeError aborted the rest of _build: no password box, no
        # buttons, nothing clickable, and the dialog landed in the top-left
        # corner because its size was never resolved.
        #
        # It is exercised here with a real stored credential in a sandbox
        # directory, because the bug ONLY appears in that state -- a test that
        # built the dialog without one would have passed and proved nothing.
        # ============================================================
        import shutil
        import tempfile

        from mclink.config import credentials
        from mclink.gui import auth_ui

        sandbox = tempfile.mkdtemp(prefix="mclink-guidlg-")
        original_appdata = os.environ.get("APPDATA")

        try:
            os.environ["APPDATA"] = sandbox
            import importlib

            importlib.reload(credentials)

            # A credential written by THIS version, so it is not discarded as
            # stale -- otherwise the dialog would take the no-password path and
            # the bug would be invisible again.
            credentials.save("tester", "Secret123", settings.APP_VERSION)

            stored_user, stored_password = credentials.load(
                settings.APP_VERSION
            )

            check(
                "前置条件：沙箱里确实存了一个本版本的密码",
                stored_user == "tester" and stored_password == "Secret123",
                f"{stored_user!r}, {stored_password!r}",
            )

            dialog = None
            error = None

            try:
                dialog = auth_ui.LoginDialog(
                    root,
                    application.engine,
                    "127.0.0.1",
                    19100,
                    username="",
                    client_id="smoke-cid",
                )

                root.update_idletasks()
                root.update()
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"

            check(
                "有保存的密码时，登录窗口能完整构建（不会中途抛异常）",
                error is None,
                error or "",
            )

            if dialog is not None:
                # The widgets that went MISSING before. Checking the password
                # box and the buttons is the whole point: the text fields were
                # created before the crash and the buttons after it.
                widgets_ok = all(
                    getattr(dialog, name, None) is not None
                    for name in ("password_entry", "login_button",
                                 "status_label", "remember_var")
                )

                check(
                    "登录窗口的密码框 / 登录按钮 / 状态栏都在（修复前这些会缺失）",
                    widgets_ok,
                    "有控件没建出来",
                )

                check(
                    "保存的账号被自动填入密码框",
                    dialog.password_var.get() == "Secret123",
                    dialog.password_var.get(),
                )

                check(
                    "「记住密码」保持勾选",
                    dialog.remember_var.get() is True,
                )

                # A dialog with no size never resolves its geometry, which is
                # what put it in the corner.
                dialog.update_idletasks()

                check(
                    "登录窗口解析出了真实尺寸（不是 1x1，否则会被摆到屏幕角落）",
                    dialog.winfo_reqwidth() > 300
                    and dialog.winfo_reqheight() > 200,
                    f"{dialog.winfo_reqwidth()}x{dialog.winfo_reqheight()}",
                )

                geometry = dialog.winfo_geometry()
                x_pos = int(geometry.split("+")[1]) if "+" in geometry else -1

                check(
                    "登录窗口没有被摆到屏幕外/左上角",
                    x_pos >= 0,
                    geometry,
                )

                if dialog is not None:
                    with contextlib.suppress(Exception):
                        dialog.grab_release()
                        dialog.destroy()

        finally:
            if original_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = original_appdata

            shutil.rmtree(sandbox, ignore_errors=True)

            # Leave the module pointing at the real location again.
            import importlib

            importlib.reload(credentials)

        root.update_idletasks()
        root.update()

    except Exception:
        check("GUI smoke test completed without exception", False)
        traceback.print_exc()
    else:
        check("GUI smoke test completed without exception", True)
    finally:

        with contextlib.suppress(Exception):
            root.destroy()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
