"""
MC-Link -- 客户端「幽灵房」与按钮状态的端到端测试（真中继 + 真界面）。

============================================================================
 这一套为什么必须存在（用户的原话）
============================================================================

「客户端在收到服务端中断房间的请求以后，并未断开并释放当前房间的所有按钮
可用状态，而是依然正常连接……这时候还得手动退出房间才行，幽灵这不就又来了
吗？」

所以这一套不是断言字符串，而是：
  1. 真的起一个中继子进程
  2. 真的用界面去建房、认领主机、开始连接
  3. 真的从"面板"（管理 API）把房间关掉
  4. 只靠界面自己的事件循环收拾残局
  5. 然后检查【每一个按钮】的状态、引擎状态、房间标签，
     并且再建一个新房间证明客户端真的干净了

另外还覆盖：
  * 连接运行中「释放主机身份」必须被拦住（必须先停止）
  * 停止连接后主机位置仍然保留（列表显示"未开服"，不是无人认领）
  * 房间列表：显示房间名、选中一行＝填进输入框、自动刷新真的会跑

Run:
    python tests/gui_room_ghost_test.py
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

RELAY_PORT = int(os.environ.get("MCLINK_TEST_GHOST_PORT", "19150"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_GHOST_ADMIN_PORT", "19151"))
ADMIN_TOKEN = "test-ghost-token"

ROOM_NAME = "幽灵房 测试"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def http_request(method, path, body=None, timeout=40):
    url = f"http://127.0.0.1:{ADMIN_PORT}{path}"
    data = None
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(
                response.read().decode("utf-8") or "{}"
            )
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")

        with contextlib.suppress(Exception):
            return e.code, json.loads(payload or "{}")

        return e.code, {"raw": payload}


def port_open(port, host="127.0.0.1", timeout=0.4):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def state_of(widget):
    with contextlib.suppress(Exception):
        return str(widget.cget("state"))

    return "?"


def pump(root, app, seconds, until=None):
    """
    跑界面自己的事件循环（不进 mainloop）。

    root.update() 会处理 after() 排队的回调 —— 也就是 _tick，事件泵和按钮
    刷新都在里面。这就是"真界面 + 真中继"的关键：收拾残局的是产品代码，
    不是测试代码。
    """
    deadline = time.time() + seconds

    while time.time() < deadline:
        with contextlib.suppress(Exception):
            root.update()

        if until is not None and until():
            return True

        time.sleep(0.05)

    return bool(until()) if until is not None else False


def main():
    print("=" * 64)
    print("MC-Link GUI ghost-room / button state test")
    print("=" * 64)

    try:
        import tkinter as tk
    except ImportError as e:
        check("tkinter available", False, str(e))
        return 1

    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(RELAY_PORT),
            "MCLINK_DB_ENABLED": "0",
            "MCLINK_REQUIRE_ACCOUNT": "1",
            "MCLINK_BOOTSTRAP_ADMIN": "admin:adminpass123",
            "MCLINK_ADMIN_API_ENABLED": "1",
            "MCLINK_ADMIN_API_HOST": "127.0.0.1",
            "MCLINK_ADMIN_API_PORT": str(ADMIN_PORT),
            "MCLINK_ADMIN_API_TOKEN": ADMIN_TOKEN,
            "MCLINK_STATUS_FILE": os.path.join(HERE, "ghost_status.json"),
            "MCLINK_STATUS_INTERVAL": "2",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            "MCLINK_MAX_PLAYERS": "4",
            "MCLINK_VERBOSE": "0",
        }
    )

    server_log = open(
        os.path.join(HERE, "ghost_relay.log"), "w", encoding="utf-8", errors="replace"
    )

    relay = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    sandbox = tempfile.mkdtemp(prefix="mclink-ghost-")
    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox

    root = None
    warnings = []

    try:
        from mclink.config import settings
        from mclink.gui import app as gui_app

        ok = False

        for _ in range(120):
            if port_open(RELAY_PORT) and port_open(ADMIN_PORT):
                ok = True
                break

            time.sleep(0.25)

        check("relay starts with the admin API", ok)

        if not ok:
            return 1

        root = tk.Tk()
        root.withdraw()

        application = gui_app.MCLinkApp(root)

        # 只拦弹窗：测试环境里不能有人点"确定"。
        import tkinter.messagebox as mb

        mb.showwarning = lambda *a, **k: warnings.append(a)
        mb.showerror = lambda *a, **k: warnings.append(a)
        mb.showinfo = lambda *a, **k: warnings.append(a)
        mb.askokcancel = lambda *a, **k: True
        mb.askyesno = lambda *a, **k: True

        application.host_var.set("127.0.0.1")
        application.port_var.set(str(RELAY_PORT))
        application.max_players_var.set("2")
        application.mc_port_var.set("25565")
        application.local_port_var.set("25566")

        # 三个数字都要"按确定"才算提交（用户要求：输入完不会立即生效）。
        application._commit_max_players()
        application._commit_port("mc_port", application.mc_port_var)
        application._commit_port("local_port", application.local_port_var)

        check("MCLinkApp constructs", True)

        # ---- 登录（真的 LOGIN）----
        client_id = application.client_id_var.get().strip()

        try:
            application.auth = application.engine.login(
                "127.0.0.1", RELAY_PORT, "admin", "adminpass123", client_id
            )
        except Exception as e:  # noqa: BLE001
            check("login against the test relay", False, repr(e))
            return 1

        application._refresh_account_ui()

        check(
            "登录成功（界面拿到账号）",
            application.auth is not None
            and application.auth.username == "admin",
            str(application.auth),
        )

        # ============================================================
        # 1. 建房（用中文 + 空格的房间名）
        # ============================================================
        # ============================================================================
        #  必须【往输入框里打字】，不能直接 app.room_var.set(...)
        # ============================================================================
        #
        # `self.room_var` 曾经被赋值两次（房间卡一次、身份配置卡一次），而身份配置
        # 卡是后建的 —— 于是程序读的变量和输入框绑的变量不是同一个：用户在框里
        # 打字，程序读到的永远是空，于是回「请输入房间名」。
        #
        # 所有老测试都是 `app.room_var.set(...)`（正好设的是程序那个孤儿变量），
        # 所以一路绿灯。这里改成**真的往控件里输入**，走用户走的那条路。
        application.room_entry.delete(0, "end")
        application.room_entry.insert(0, ROOM_NAME)
        root.update_idletasks()

        check(
            "★ 往输入框里打字 → 程序读到的是同一份内容（绑定自检）",
            application.room_var.get() == ROOM_NAME,
            f"输入框={application.room_entry.get()!r} "
            f"变量={application.room_var.get()!r}",
        )

        check(
            "★ 所有输入框绑的都是程序在读的变量（自检函数）",
            application._verify_form_bindings() == [],
            str(application._verify_form_bindings()),
        )

        application.on_create_room()

        pump(root, application, 8, until=lambda: application.engine.room_current() is not None)

        state = application.engine.room_state()

        check(
            "用中文带空格的房间名建出了房间",
            state is not None,
            str(state),
        )

        check(
            "界面拿到的是房间名（不是那串 %E6%88%91）",
            state is not None and state.room_name == ROOM_NAME,
            getattr(state, "room_name", None),
        )

        check(
            "协议里用的是自动生成的房间 ID",
            state is not None and state.room != ROOM_NAME and state.room.isascii(),
            getattr(state, "room", None),
        )

        check(
            "建房后输入框被清空",
            application.room_var.get() == "",
            application.room_var.get(),
        )

        # ---- 按钮状态：建房后 ----
        check(
            "在房间里：创建/加入 变灰，退出/删除房间可用",
            state_of(application.create_room_button) == "disabled"
            and state_of(application.join_room_button) == "disabled"
            and state_of(application.leave_room_button) == "normal"
            and state_of(application.delete_room_button) == "normal",
            f"create={state_of(application.create_room_button)} "
            f"join={state_of(application.join_room_button)} "
            f"leave={state_of(application.leave_room_button)} "
            f"delete={state_of(application.delete_room_button)}",
        )

        check(
            "在房间里：可以认领主机",
            state_of(application.claim_host_button) == "normal"
            and state_of(application.release_host_button) == "disabled",
            f"claim={state_of(application.claim_host_button)} "
            f"release={state_of(application.release_host_button)}",
        )

        # ============================================================
        # 2. 认领主机 → 开始连接 → 释放被拦截
        # ============================================================
        application.on_claim_host()
        pump(root, application, 5, until=lambda: bool(
            application.engine.room_state() and application.engine.room_state().is_host
        ))

        state = application.engine.room_state()

        check("认领主机成功", bool(state and state.is_host), str(state))

        check(
            "认领后：释放可用、认领变灰",
            state_of(application.release_host_button) == "normal"
            and state_of(application.claim_host_button) == "disabled",
            f"release={state_of(application.release_host_button)} "
            f"claim={state_of(application.claim_host_button)}",
        )

        application.on_start()

        pump(root, application, 12, until=lambda: application.running)

        check("开始连接成功（引擎在跑）", application.running is True)

        check(
            "★ 连接运行中：释放主机身份被禁用（必须先停止）",
            state_of(application.release_host_button) == "disabled",
            state_of(application.release_host_button),
        )

        # 就算硬点（按钮状态过期的情况）也必须被拦住
        warnings.clear()
        application.on_release_host()

        state = application.engine.room_state()

        check(
            "★ 连接运行中直接调用释放 → 被拦截，主机位置没有丢",
            bool(state and state.is_host) and len(warnings) > 0,
            f"is_host={getattr(state, 'is_host', None)} 弹窗数={len(warnings)}",
        )

        check(
            "连接运行中：开始变灰、停止可用",
            state_of(application.start_button) == "disabled"
            and state_of(application.stop_button) == "normal",
            f"start={state_of(application.start_button)} "
            f"stop={state_of(application.stop_button)}",
        )

        # ============================================================
        # 3. 停止连接 → 主机位置【仍然保留】
        # ============================================================
        application.on_stop()
        pump(root, application, 6, until=lambda: not application.running)

        state = application.engine.room_state()

        check(
            "★ 停止后主机位置仍然归自己（用户 A8）",
            bool(state and state.is_host and state.host_claimed),
            f"is_host={getattr(state, 'is_host', None)} "
            f"host_claimed={getattr(state, 'host_claimed', None)}",
        )

        check(
            "停止后：释放按钮重新可用",
            state_of(application.release_host_button) == "normal",
            state_of(application.release_host_button),
        )

        # ============================================================
        # 4. 面板关房 → 客户端必须自己收拾干净（幽灵房的根治）
        # ============================================================
        room_id = application.engine.room_state().room

        warnings.clear()
        popups = []

        def record(*args, **kwargs):
            popups.append(args)

        mb.showwarning = record

        status, payload = http_request(
            "POST", "/rooms/revoke", {"room": room_id, "reason": "自动化测试关房"}
        )

        check(
            "面板成功关掉房间",
            status == 200 and payload.get("ok") is True,
            f"status={status} {payload.get('message')}",
        )

        cleaned = pump(
            root,
            application,
            25,
            until=lambda: application.engine.room_current() is None
            and not application.running,
        )

        check(
            "★ 客户端自动退出房间并停止连接（不需要手动点退出）",
            cleaned,
            f"room={application.engine.room_current()} running={application.running}",
        )

        # ---- 关键：所有按钮都回到"不在房间里" ----
        buttons = {
            "创建房间": application.create_room_button,
            "加入房间": application.join_room_button,
            "退出房间": application.leave_room_button,
            "删除我的房间": application.delete_room_button,
            "认领主机身份": application.claim_host_button,
            "释放主机身份": application.release_host_button,
            "开始连接": application.start_button,
            "停止": application.stop_button,
        }

        expected = {
            "创建房间": "normal",
            "加入房间": "normal",
            "退出房间": "disabled",
            "删除我的房间": "disabled",
            "认领主机身份": "disabled",
            "释放主机身份": "disabled",
            "开始连接": "normal",
            "停止": "disabled",
        }

        actual = {name: state_of(widget) for name, widget in buttons.items()}

        check(
            "★ 关房后所有按钮状态全部恢复（幽灵状态清零）",
            actual == expected,
            f"实际={actual}",
        )

        check(
            "房间标签回到「未加入房间」",
            application.room_state_label.cget("text") == "未加入房间",
            application.room_state_label.cget("text"),
        )

        # The log is ENGLISH by design now (the owner greps it and sends it
        # back for analysis), while room names and reasons stay verbatim --
        # they are the user's data, not our wording.
        check(
            "日志里明确写了房间被服务器关闭",
            "room closed by the server" in application.log_text.get("1.0", "end"),
        )

        # ---- 用户要的：关房要给个弹窗，而且只弹一次 ----
        closed_popups = [
            p for p in popups if p and "房间" in str(p[0])
        ]

        check(
            "★ 房间被管理关闭时弹窗告知用户",
            len(closed_popups) >= 1,
            f"弹窗={popups}",
        )

        check(
            "★ 弹窗里写明了原因和「已经自动退出」",
            bool(closed_popups)
            and "自动化测试关房" in str(closed_popups[0][1])
            and "自动退出" in str(closed_popups[0][1]),
            str(closed_popups[0] if closed_popups else None),
        )

        check(
            "★ 关房弹窗只弹一次（ROOM REVOKED + room_closed 不会连弹两次）",
            len(closed_popups) == 1,
            f"弹了 {len(closed_popups)} 次：{closed_popups}",
        )

        mb.showwarning = lambda *a, **k: warnings.append(a)

        # ============================================================
        # 5. 客户端真的干净了：马上能再建一个新房间
        # ============================================================
        # 同样走"往输入框里打字"这条真实路径
        application.room_entry.delete(0, "end")
        application.room_entry.insert(0, "关房之后的新房间")
        application.on_create_room()

        rebuilt = pump(
            root,
            application,
            10,
            until=lambda: application.engine.room_current() is not None,
        )

        check(
            "★ 关房后可以立刻新建房间（没有残留状态挡路）",
            rebuilt,
            str(application.engine.room_state()),
        )

        # ---- 自动刷新：把上次刷新时间推到很久以前，跑一拍就应该发起请求 ----
        pump(root, application, 6, until=lambda: not application._room_list_busy)

        application._room_list_fetched_at = 0.0
        application._room_list_busy = False
        application._maybe_auto_refresh_room_list()

        check(
            "★ 房间列表会【自动刷新】（不再需要手动点刷新）",
            application._room_list_busy is True,
            f"busy={application._room_list_busy}",
        )

        pump(root, application, 8, until=lambda: not application._room_list_busy)

        check(
            "自动刷新拿到了真实列表（本次测试里还有刚建的新房间）",
            any(r.display == "关房之后的新房间" for r in application.room_rows),
            f"rows={[r.display for r in application.room_rows]} "
            f"hint={application.room_list_hint.cget('text')!r}",
        )

        # 列表相关的检查要在"不在房间里"的状态下做（在房间里选房间本来就
        # 应该被拦住 —— 那条规则有它自己的检查）。
        application.on_leave_room()
        pump(root, application, 8, until=lambda: application.engine.room_current() is None)

        check(
            "点「退出房间」之后真的退出了",
            application.engine.room_current() is None,
            str(application.engine.room_current()),
        )

        # ============================================================
        # 6. 房间列表：房间名、选中一行、自动刷新
        # ============================================================
        from mclink.core.control_client import RoomInfo

        rows = [
            RoomInfo(
                room="%E6%88%91%E7%9A%84%E6%88%BF%E9%97%B4",
                name="我的房间",
                owner="alice",
                host="bob",
                host_live=False,
                players=2,
                max_players=8,
                members=3,
                state="busy",
            )
        ]

        application._fill_room_list(rows)

        items = application.rooms_tree.get_children()
        values = application.rooms_tree.item(items[0], "values") if items else ()

        check(
            "房间列表第 1 列显示【房间名】（不是房间 ID）",
            bool(values) and values[0] == "我的房间",
            str(values),
        )

        check(
            "「当前主机」列带上了未开服标记",
            len(values) > 2 and "未开服" in str(values[2]),
            str(values),
        )

        # 选中一行 = 填进输入框；然后「加入房间」按钮就能直接用
        application.room_var.set("")
        application.rooms_tree.selection_set(items[0])
        application.on_room_row_selected()

        check(
            "★ 选中列表里的一行 → 房间名自动填进输入框",
            application.room_var.get() == "我的房间",
            application.room_var.get(),
        )

        # 输入框空着 + 有选中行 → 点加入也能用（不会再提示要输入房间名）
        application.rooms_tree.selection_set(items[0])
        application.on_room_row_selected()

        check(
            "选中行之后「加入房间」拿到了目标房间名",
            application._current_room_name() == "我的房间",
            application._current_room_name(),
        )

        # 自动刷新：把上次刷新时间推到很久以前，跑一拍就应该发起请求
        application._room_list_fetched_at = 0.0
        application._room_list_busy = False
        application._maybe_auto_refresh_room_list()

        check(
            "★ 房间列表会【自动刷新】（不再需要手动点刷新）",
            application._room_list_busy is True,
            f"busy={application._room_list_busy}",
        )

    except Exception:
        check("GUI ghost test completed without exception", False)
        traceback.print_exc()
    else:
        check("GUI ghost test completed without exception", True)
    finally:
        if root is not None:
            with contextlib.suppress(Exception):
                root.destroy()

        relay.terminate()

        with contextlib.suppress(Exception):
            relay.wait(timeout=10)

        if relay.poll() is None:
            relay.kill()

        server_log.close()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
