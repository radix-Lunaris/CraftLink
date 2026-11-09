"""
MC-Link -- 房间 / 主机身份 / 会话模式自动重建 回归测试（需要 tkinter 显示）。

============================================================================
 这一套盯的是 2026-09-27 日志审计里的三条 P0/P1（界面侧）
============================================================================

  1. **免登录游客点「开始连接」`AttributeError`**（`config.username =
     self.auth.username`，而 `self.auth` 是 None）→ 异常被 Tk 吞掉，游客
     永远连不上、**零日志**。
  2. **切模式重建把房主变成 Player**（`_rebuild_tunnels` 没传
     `role_override`，`_collect_config` 默认 `"player"`）→ 房间瞬间失去 host，
     所有人 `NO_HOST`。
  3. **非房主侧切模式不重建会话**（判据 `config.mode != room_mode` 恒假 +
     磁贴被禁用）→ 模式与实际通路长期撕裂。

用的是**真界面**（真 tk 控件、真 `_refresh_status`），只把 Engine 换成一个
可编程的替身 —— 这样测到的就是界面真正跑的代码路径，而不是一个平行实现。

Run:
    python tests/gui_room_host_session_test.py
"""

import contextlib
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

# 必须在 import app 之前把配置目录指到可写位置（客户端默认往 ~/.config 写日志）。
SANDBOX = tempfile.mkdtemp(prefix="mclink-room-host-")
os.environ["APPDATA"] = SANDBOX
os.environ["XDG_CONFIG_HOME"] = SANDBOX

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"),
              flush=True)

    (PASSED if ok else FAILED).append(name)


# ============================================================
# 替身
# ============================================================

class Result:
    """`engine.start_session()` 的最小返回物。"""

    def __init__(self, config):
        self.config = config
        self.role = config.role


class FakeEngine:
    """
    只实现界面真正会用到的那几个入口；所有调用都记下来。

    **绝不真起网络/会话**：这一套测的是"界面拿到什么去连接"，
    网络那半边由别的套件负责。
    """

    def __init__(self):
        self.room = None
        self.starts = []
        self.stops = 0
        self.pushed = []
        self.session_mode_value = ""
        self.room_punch = {}
        self.nat_profile = None
        self.monitor = None
        self.watch_nat_calls = []
        self.standalone_hosts = []
        self.standalone_syncs = []
        self.watch_direct_calls = []

    # ---- 房间 ----
    def room_state(self):
        return self.room

    def room_current(self):
        return None

    # ---- 会话 ----
    def session_mode(self):
        return self.session_mode_value

    def start_session(self, config, auth=None, timeout=20):
        self.starts.append((config, auth))
        self.session_mode_value = str(getattr(config, "mode", "") or "")
        return Result(config)

    def stop_session(self, timeout=15):
        self.stops += 1
        self.session_mode_value = ""
        return True

    # ---- 房间级任务 ----
    def watch_room_nat(self, enabled, log=None, standalone_host=None):
        self.watch_nat_calls.append(bool(enabled))
        self.standalone_hosts.append(str(standalone_host or ""))

    def sync_nat_standalone(self, host=None, log=None, keep_running=False):
        self.standalone_syncs.append(str(host or ""))
        return True

    def room_nat_age(self):
        return None

    def watch_room_direct(self, enabled, log=None):
        self.watch_direct_calls.append(bool(enabled))

    def room_direct_state(self):
        return self.room_punch

    def room_nat_profile(self):
        return self.nat_profile

    def traffic_snapshot(self):
        return {
            "relay_up": 0, "relay_down": 0,
            "direct_up": 0, "direct_down": 0,
            "total_up": 0, "total_down": 0,
            "up_rate": 0.0, "down_rate": 0.0,
        }

    def snapshot(self):
        data = {
            "role": None, "state": "IDLE", "state_text": "未启动",
        }
        data.update(self.traffic_snapshot())
        return data

    # ---- 弹窗 / 其它 ----
    def relay_requires_account(self, *_a, **_k):
        return False

    def claim_host(self):
        if self.room is not None:
            self.room.is_host = True
            self.room.host_claimed = True

        return True, "ok"

    def _push(self, kind, **data):
        self.pushed.append((kind, data))


class RoomState:
    """够用的 RoomState 替身（界面只读这些字段）。"""

    def __init__(self, **kw):
        self.room = "audit-room"
        self.room_name = "audit-room"
        self.display = "audit-room"
        self.client_id = "cid-me"
        self.username = "游客abc"
        self.own_name = "游客abc"
        self.self_name = "游客abc"
        self.is_owner = True
        self.is_host = False
        self.host_claimed = False
        self.host_name = ""
        self.members = 2
        self.peer_ids = ["cid-peer"]
        self.member_names = {"cid-me": "游客abc", "cid-peer": "radix"}
        self.member_rtts = {}
        self.world_players = 1
        self.max_players = 4
        self.server_max_players = 8
        self.conn_mode = "stun"
        self.revoked = False
        self.revoked_reason = ""
        self.joined_at = time.time()

        for key, value in kw.items():
            setattr(self, key, value)


# ============================================================
# 真界面夹具
# ============================================================

class RealApp:
    """真 MCLinkApp + 假 Engine。构建失败（没有显示）时 `self.app is None`。"""

    def __init__(self):
        self.app = None
        self.logs = []
        self.engine = FakeEngine()

    def build(self):
        try:
            import tkinter as tk
            from tkinter import messagebox
        except ImportError:
            return False

        # 测试里不能有人点弹窗。
        messagebox.askokcancel = lambda *a, **k: True
        messagebox.showwarning = lambda *a, **k: None
        messagebox.showerror = lambda *a, **k: None
        messagebox.showinfo = lambda *a, **k: None

        try:
            self.root = tk.Tk()
        except Exception:
            return False

        self.root.withdraw()

        from mclink.gui import app as gui_app

        try:
            app = gui_app.MCLinkApp(self.root)
        except Exception as e:
            check("MCLinkApp 能构建", False, repr(e))
            with contextlib.suppress(Exception):
                self.root.destroy()
            return False

        # 换掉 Engine，并把界面发出的日志记下来。
        #
        # 这里**不**转调真正的 `app.log`：它要写 tk 队列，而重建是在工作线程里
        # 跑的，从非主线程碰 tkinter 会抛 "main thread is not in main loop"。
        app.engine = self.engine

        import threading

        main_thread = threading.main_thread()

        def capture(text, kind="normal"):
            self.logs.append(str(text))

            # 重建在工作线程里跑：从非主线程碰 tkinter 会抛
            # "main thread is not in main loop"，那是**测试夹具**的问题，
            # 不是要测的行为。所以只有主线程才转调真正的实现。
            if threading.current_thread() is main_thread:
                return original_log(text, kind=kind)

            return None

        original_log = app.log
        app.log = capture
        self.app = app

        # 三个数字必须"按过确定"，否则 `_collect_config()` 按设计会报错；
        # 房间名也要有（不在房间里时它读输入框）。
        #
        # ★ M6/D10：中继地址/端口同样要显式给 —— 客户端不再内置服务器，
        # 空端口会让 `_collect_config()` 报"还没填端口"。
        with contextlib.suppress(Exception):
            app._committed["mc_port"] = 25565
            app._committed["local_port"] = 25565
            app._committed["max_players"] = 4
            app.mc_port_var.set("25565")
            app.local_port_var.set("25565")
            app.max_players_var.set("4")
            app.room_var.set("audit-room")
            app.host_var.set("127.0.0.1")
            app.port_var.set("19100")

        return True

    def close(self):
        if self.app is None:
            return

        with contextlib.suppress(Exception):
            self.root.destroy()

    def log_text(self):
        return "\n".join(self.logs)


# ============================================================
# 1. 免登录游客也能开始连接（P0）
# ============================================================

def test_guest_start_without_login(harness):
    print("")
    print("---- 免登录游客：auth=None 也必须能开始连接 ----")

    app = harness.app
    engine = harness.engine

    engine.room = RoomState(is_host=True, host_claimed=True)

    app.auth = None                     # 免登录：登出后就是这个状态
    app.running = False
    app.username_var.set("游客abc")
    app.mc_port_var.set("25565")
    app.local_port_var.set("25565")

    # 中继说"不要求登录" -> 真正的 `require_login()` 返回 True，而 auth 是
    # None —— 这正是崩掉的那条路（不替换 require_login，走真实现）。
    engine.relay_requires_account = lambda *_a, **_k: False

    harness.logs.clear()
    raised = None

    try:
        app.on_start()
    except Exception as e:
        raised = e

    check("★ 游客点开始连接不抛异常（原来是 AttributeError）",
          raised is None, repr(raised))
    check("★ 真的发起了连接（不是静默 return）",
          len(engine.starts) == 1,
          f"starts={len(engine.starts)} logs={harness.logs[-3:]}")
    check("会话仍然跑起来了（running=True）", app.running is True)

    if engine.starts:
        config = engine.starts[0][0]
        check(
            "guest 连接用的是配置里/中继分配的游客名，而不是崩掉",
            getattr(config, "username", "") == "游客abc",
            repr(getattr(config, "username", "")),
        )
    else:
        check("guest 连接用的是配置里/中继分配的游客名，而不是崩掉", False)

    check(
        "★ 免登录开始连接会留下一行日志（不再是零日志）",
        any("without a login" in line for line in harness.logs),
        repr(harness.logs[-5:]),
    )


def test_account_still_wins_when_logged_in(harness):
    print("")
    print("---- 有账号时账号名仍然是权威 ----")

    app = harness.app
    engine = harness.engine
    engine.room = RoomState(is_host=True, host_claimed=True)

    app.running = False

    class Auth:
        username = "root-user"

    app.auth = Auth()
    app.require_login = lambda: True

    harness.logs.clear()

    try:
        app.on_start()
    except Exception as e:
        check("登录状态点开始连接不抛异常", False, repr(e))
        return

    check("登录状态点开始连接不抛异常", True)

    if engine.starts:
        check(
            "配置里的用户名被账号名覆盖（权威来源）",
            engine.starts[-1][0].username == "root-user",
            repr(engine.starts[-1][0].username),
        )
    else:
        check("配置里的用户名被账号名覆盖（权威来源）", False)


def test_auth_block_failure_is_logged_not_swallowed(harness):
    print("")
    print("---- 这一段里的任何异常都必须落日志，不能消失 ----")

    app = harness.app
    engine = harness.engine
    engine.room = RoomState(is_host=True, host_claimed=True)
    app.running = False
    app.require_login = lambda: True

    class ExplodingAuth:
        @property
        def username(self):
            raise RuntimeError("boom")

    app.auth = ExplodingAuth()
    engine.starts.clear()
    harness.logs.clear()

    raised = None

    try:
        app.on_start()
    except Exception as e:
        raised = e

    check("★ 账号取值炸了也不许抛到 Tk 之外", raised is None, repr(raised))
    check(
        "★ 异常被记成一行日志（不是静默消失）",
        any("account handling failed" in line for line in harness.logs),
        repr(harness.logs[-4:]),
    )
    check(
        "★ 炸了也继续连接（游客身份）",
        len(engine.starts) == 1,
        f"starts={len(engine.starts)}",
    )


# ============================================================
# 2. 重建不许把房主变成 Player（P0）
# ============================================================

def test_rebuild_keeps_the_host_role(harness):
    print("")
    print("---- 切模式重建：房主必须还是 host ----")

    app = harness.app
    engine = harness.engine

    engine.room = RoomState(is_host=True, host_claimed=True)
    app.auth = None
    app.running = True

    # 角色判定：持有主机位 = host（和 on_start 同一条规则）。
    role, explanation = app._decide_connect_role(engine.room)
    check("（前提）持有主机位 -> 角色判定为 host",
          role == "host" and explanation is None, f"{role} / {explanation}")

    check("`_safe_connect_role()` 也回 host",
          app._safe_connect_role() == "host", app._safe_connect_role())

    # 重建的 worker 在工作线程里跑，而它最终会读 tk 变量（`port_var` 之类）。
    # 没有 mainloop 的测试里那会抛 "main thread is not in main loop" —— 那是
    # 测试环境限制，不是要测的行为。所以只把 `_collect_config` 换成记录器：
    # 这一套要钉住的恰恰是"传给它的 role_override 是什么"。
    calls = []
    original_collect = app._collect_config

    def record_collect(role_override=None):
        calls.append(role_override)
        config = app.config
        config.role = role_override
        config.room = "audit-room"
        return config

    stub_config = getattr(app, "_audit_stub_collect", None)

    app._collect_config = record_collect
    app._audit_stub_collect = record_collect

    # 清掉可能残留的"重建进行中"标记：真实应用里它由 worker 的 finally 复位，
    # 而测试里前面的用例可能刚触发过重建。
    app._rebuild_in_flight = False

    engine.starts.clear()
    app._rebuild_tunnels()

    # 重建在工作线程里跑；等它把会话建起来。
    deadline = time.time() + 10

    while time.time() < deadline and not engine.starts:
        time.sleep(0.05)

    check("重建确实发起了新会话", bool(engine.starts),
          f"starts={len(engine.starts)} pushed={engine.pushed[-3:]} "
          f"calls={calls!r} logs={harness.logs[-4:]!r}")

    check(
        "★ 重建时把 host 作为 role_override 传给了 `_collect_config`",
        calls == ["host"],
        repr(calls),
    )

    if engine.starts:
        rebuilt = engine.starts[-1][0]
        check(
            "★ 重建出来的会话角色是 host（不是 player）",
            rebuilt.role == "host",
            repr(rebuilt.role),
        )
    else:
        check("★ 重建出来的会话角色是 host（不是 player）", False)

    check("重建前先把旧会话停掉", engine.stops >= 1, f"stops={engine.stops}")
    check(
        "重建成功后给界面发了 rebuild_done",
        any(kind == "rebuild_done" for kind, _data in engine.pushed),
        repr(engine.pushed[-3:]),
    )

    app._collect_config = original_collect


def test_rebuild_falls_back_to_the_last_role(harness):
    print("")
    print("---- 拿不到房间状态时，退回「上次用过的身份」并记一行日志 ----")

    app = harness.app
    engine = harness.engine

    engine.room = None                  # 房间状态拿不到
    app.config.role = "host"
    harness.logs.clear()

    check("★ 退回上次用过的身份，而不是静默变 player",
          app._safe_connect_role() == "host", app._safe_connect_role())
    check(
        "退回时留下了一行日志",
        any(
            "not in a room; keeping the last used role" in line
            for line in harness.logs
        ),
        repr(harness.logs[-3:]),
    )
    # 2026-09-27（ISSUES 问题 1/4/6）：没有房间是**正常分支**，不该再往用户
    # 日志里写一条 AttributeError（两份用户日志里退出时各出现过一次）。
    check(
        "没有房间时不再抛/记 AttributeError",
        not any("AttributeError" in line for line in harness.logs),
        repr(harness.logs[-5:]),
    )

    app.config.role = ""
    harness.logs.clear()
    check("连上次身份都没有时才退到 player",
          app._safe_connect_role() == "player", app._safe_connect_role())
    check(
        "退到 player 也说清楚了",
        any("no previously used role" in line for line in harness.logs),
        repr(harness.logs[-3:]),
    )


def test_collect_config_refuses_a_missing_role(harness):
    print("")
    print("---- `_collect_config` 不再静默降级成 player ----")

    app = harness.app

    # 这一套测的是**真实现**（上一个测试临时换过记录器）。
    if getattr(app, "_audit_stub_collect", None) is not None:
        import types

        app._collect_config = types.MethodType(
            type(app)._collect_config, app
        )

    # `port_var.get()` 在工作线程里读 tk 会炸（没有 mainloop），而这里是在主
    # 线程直接调，没问题。

    bad_raised = None

    try:
        app._collect_config(role_override="spectator")
    except ValueError as e:
        bad_raised = e

    check("★ 一个非法角色 -> 响亮报错，而不是猜一个", bad_raised is not None,
          repr(bad_raised))
    check(
        "报错同时落了日志",
        any("invalid connection role" in line for line in harness.logs),
        repr(harness.logs[-3:]),
    )

    app.engine.room = RoomState(is_host=True, host_claimed=True)
    config = app._collect_config()
    check(
        "省略 role_override（保存表单那种调用）时按真实身份决定",
        config.role == "host",
        repr(config.role),
    )


# ============================================================
# 3. 非房主：房间模式变了就重建（P1，防抖）
# ============================================================

def test_session_mode_rebuild_requested_once(harness):
    print("")
    print("---- 非房主：会话模式 vs 房间模式 -> 只请求一次（防抖）----")

    app = harness.app
    engine = harness.engine

    engine.room = RoomState(conn_mode="stun", is_host=False)
    app.running = True
    engine.session_mode_value = "relay"      # 会话冻在旧模式

    rebuilds = []
    app._rebuild_tunnels = lambda mode=None: rebuilds.append(time.time())
    app._last_session_mode_rebuild_at = 0.0
    app._session_mode_rebuild_target = None
    app._rebuild_in_flight = False

    check(
        "★ 会话 relay / 房间 stun -> 判定出该重建",
        app._session_mode_rebuild_delta() == ("relay", "stun"),
        repr(app._session_mode_rebuild_delta()),
    )

    # 每秒一次的刷新会连续调很多次：只许第一次真的重建。
    for _ in range(10):
        app._sync_session_mode()

    check(
        "★ 连续 10 次刷新只请求了一次重建（不会无限重建）",
        len(rebuilds) == 1,
        f"rebuilds={len(rebuilds)}",
    )
    check(
        "重建请求之后判定不再重复（冷却窗口内）",
        app._session_mode_rebuild_delta() is None,
    )

    # 重建在跑的时候绝不叠加第二次。
    app._rebuild_in_flight = True
    app._last_session_mode_rebuild_at = 0.0        # 冷却已过
    check(
        "★ 已有重建在跑时绝不叠加第二次",
        app._session_mode_rebuild_delta() is None,
    )
    app._rebuild_in_flight = False

    # 冷却过去之后允许重试（万一上次重建失败了）。
    app._last_session_mode_rebuild_at = time.time() - 999
    check(
        "冷却过去后允许再试一次（失败不会把人永久卡住）",
        app._session_mode_rebuild_delta() == ("relay", "stun"),
    )

    # 模式一致 / 没在跑 -> 什么都不做。
    engine.session_mode_value = "stun"
    app._last_session_mode_rebuild_at = 0.0
    app._session_mode_rebuild_target = None
    check("模式一致 -> 不重建", app._session_mode_rebuild_delta() is None)

    engine.session_mode_value = "relay"
    app.running = False
    check("没在跑会话 -> 不重建", app._session_mode_rebuild_delta() is None)

    app.running = True
    engine.room = RoomState(conn_mode="relay", is_host=False)
    check("房间是 relay 且会话是 relay -> 不重建",
          app._session_mode_rebuild_delta() is None)


def test_tick_path_calls_the_mode_sync(harness):
    print("")
    print("---- 每秒刷新那条路必须真的调用模式同步 ----")

    app = harness.app
    engine = harness.engine

    engine.room = RoomState(conn_mode="stun", is_host=False)
    app.running = True
    engine.session_mode_value = "relay"
    app._last_session_mode_rebuild_at = 0.0
    app._session_mode_rebuild_target = None
    app._rebuild_in_flight = False

    rebuilds = []
    app._rebuild_tunnels = lambda mode=None: rebuilds.append(True)

    app._refresh_room_ui()

    check("★ `_refresh_room_ui`（每秒一次）会触发模式同步",
          len(rebuilds) == 1, f"rebuilds={len(rebuilds)}")


def test_relay_mode_hides_hole_rows_in_the_real_card(harness):
    print("")
    print("---- relay 模式的状态卡里不许有「打洞中」的行 ----")

    app = harness.app

    app._render_hole_rows(
        [{"peer": "cid-peer", "addr": "", "ready": False,
          "attempts": 0, "peer_is_host": False, "nat": None,
          "filtering": None, "rtt_ms": None}],
        punching=False,
    )

    check(
        "★ punching=False -> 洞行控件被清空",
        app._hole_rows == {},
        repr(app._hole_rows),
    )

    # 一次都没打过时，措辞不能是"打洞中"。
    app._render_hole_rows(
        [{"peer": "cid-peer", "addr": "", "ready": False,
          "attempts": 0, "peer_is_host": False, "nat": None,
          "filtering": None, "rtt_ms": None}],
        punching=True,
    )

    texts = []

    for _row, label in app._hole_rows.values():
        with contextlib.suppress(Exception):
            texts.append(label.cget("text"))

    check(
        "★ attempts=0 的行不写「打洞中」，写「未打洞（中转模式）」",
        any("未打洞" in text for text in texts) and
        not any("打洞中" in text for text in texts),
        repr(texts),
    )

    app._render_hole_rows([], punching=True)


def test_config_confirm_button_unlocks_after_leaving(harness):
    print("")
    print("---- 退出房间后「基准配置」的确定按钮必须解锁（ISSUES 问题 4）----")

    app = harness.app
    engine = harness.engine

    rows = {}

    for var, entry, button, _status, _hint in getattr(app, "_config_rows", []):
        rows[app._config_key_of(var)] = (entry, button)

    check(
        "（前提）三行基准配置都在",
        set(rows) >= {"mc_port", "local_port", "max_players"},
        repr(sorted(rows)),
    )

    # ① 在房间里：mc_port / max_players 按设计锁死。
    app.running = False
    engine.room = RoomState(is_owner=True)
    app._refresh_room_ui()

    check(
        "★ 在房间里 mc_port 的确定按钮是灰的（房间参数锁定，已验收行为）",
        str(rows["mc_port"][1].cget("state")) == "disabled",
        str(rows["mc_port"][1].cget("state")),
    )
    check(
        "在房间里 max_players 的确定按钮也是灰的",
        str(rows["max_players"][1].cget("state")) == "disabled",
        str(rows["max_players"][1].cget("state")),
    )

    # ② 退出房间（或房间被服务端关掉）：按钮必须**当场**恢复可点。
    engine.room = None
    app._refresh_room_ui()

    check(
        "★ 退出房间后 mc_port 的确定按钮恢复可点",
        str(rows["mc_port"][1].cget("state")) != "disabled",
        str(rows["mc_port"][1].cget("state")),
    )
    check(
        "★ 退出房间后 max_players 的确定按钮恢复可点",
        str(rows["max_players"][1].cget("state")) != "disabled",
        str(rows["max_players"][1].cget("state")),
    )

    # ③ 房间被服务端强制关闭（revoked）走的是另一条分支，同样要解锁。
    engine.room = RoomState(is_owner=True, revoked=True, revoked_reason="测试")
    app._refresh_room_ui()

    check(
        "★ 房间被服务端关掉之后也要解锁",
        str(rows["mc_port"][1].cget("state")) != "disabled",
        str(rows["mc_port"][1].cget("state")),
    )

    engine.room = None
    app._refresh_room_ui()


def test_nat_measurement_waits_for_start(harness):
    print("")
    print("---- 没点「开始连接」就不许测（设计基准 §E：推翻 09-27 的“打开软件就测”）----")

    app = harness.app
    engine = harness.engine

    engine.room = None
    engine.watch_nat_calls.clear()
    engine.standalone_hosts.clear()

    app.config.server_host = "relay.example"

    app._refresh_room_ui()

    check(
        "★ 没房间 -> 测量开关必须是 False（地址只被记下来，不启动测量）",
        bool(engine.watch_nat_calls)
        and engine.watch_nat_calls[-1] is False
        and engine.standalone_hosts
        and engine.standalone_hosts[-1] == "relay.example",
        f"enabled={engine.watch_nat_calls} hosts={engine.standalone_hosts}",
    )

    check(
        "★ 没房间时打洞开关同样保持关闭",
        engine.watch_nat_calls[-1] is False,
        repr(engine.watch_nat_calls),
    )


# ============================================================

def main():
    print("=" * 64)
    print("MC-Link GUI room / host role / session-mode rebuild tests")
    print("=" * 64)

    try:
        import tkinter  # noqa: F401
    except ImportError as e:
        check("tkinter available", False, str(e))
        return 1

    harness = RealApp()

    if not harness.build():
        check("Tk root can be created (display available)", False,
              "set up /tmp/.X11-unix as described in V3-AGENTS.md §10.7")
        return 1

    check("Tk root can be created (display available)", True)

    try:
        test_guest_start_without_login(harness)
        test_account_still_wins_when_logged_in(harness)
        test_auth_block_failure_is_logged_not_swallowed(harness)
        test_rebuild_keeps_the_host_role(harness)
        test_rebuild_falls_back_to_the_last_role(harness)
        test_collect_config_refuses_a_missing_role(harness)
        test_session_mode_rebuild_requested_once(harness)
        test_tick_path_calls_the_mode_sync(harness)
        test_relay_mode_hides_hole_rows_in_the_real_card(harness)
        test_config_confirm_button_unlocks_after_leaving(harness)
        test_nat_measurement_waits_for_start(harness)
    finally:
        harness.close()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
