"""
MC-Link -- 会话自动重连 + host 离线处理 + 单人文案 回归测试（需要 tkinter）。

============================================================================
 这一套盯的是三件事（用户 2026-09-27 下达）
============================================================================

  1. **自动重连框架**（用户已批准，见 docs/AUTO-RECONNECT-PLAN.md）
       * 触发 = 非用户意图的断开；终态（停止/退房/被踢/关房/版本门/认证失效）
         **永不**重连；
       * 退避 0.5 → 1 → 2 → 4 → 8 → 15s，总窗口 60s，到点停下 + 弹窗；
       * 同一时刻只允许一个重建在飞（沿用 `_rebuild_in_flight`）；
       * 每次重连固定顺序：停旧会话（确认端口释放）→ 角色按 host 位 →
         房间当前模式 → 立刻重排房间级任务 → **不释放 host 位**。
  2. **房主离线**（服务端新行为 `HOST_LEFT`，兼容 `NO_HOST`）
       * 收到该原因 → 按 host 三种情况分开处理（见下面第 2 节），
         **不自动重连、不刷屏**；
       * 无 host 时玩家身份点「开始连接」被拒并有清晰提示。
  3. **单人文案 BUG**
       * 房间里只有自己 → 「等待对端用户加入…」且**不**显示秒数；
       * 有对端但还没收到公告 → 「等待对端起跑公告（第 N 秒）」（有上限）；
       * 两边就绪 → 倒计时文案照旧。

用**真界面 + 假 Engine**：测的是界面真正跑的代码路径。打洞那一半由
`punch_test` / `direct_*` 负责；这里只钉状态机与文案。

Run:
    python tests/auto_reconnect_test.py
"""

import contextlib
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

SANDBOX = tempfile.mkdtemp(prefix="mclink-reconnect-")
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

class RoomState:
    """够用的 RoomState 替身（界面只读这些字段）。"""

    def __init__(self, **kw):
        self.room = "reconnect-room"
        self.room_name = "reconnect-room"
        self.display = "reconnect-room"
        self.client_id = "cid-me"
        self.username = "root"
        self.own_name = "root"
        self.self_name = "root"
        self.is_owner = True
        self.is_host = True
        self.host_claimed = True
        self.host_name = "root"
        self.members = 1
        self.peer_ids = []
        self.member_names = {"cid-me": "root"}
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


class FakeEngine:
    def __init__(self):
        self.room = None
        self.starts = []
        self.stops = 0
        self.pushed = []
        self.session_mode_value = ""
        self.room_punch = {}
        self.nat_profile = None
        self.punch_enabled = []

    def room_state(self):
        return self.room

    def room_current(self):
        return None

    def session_mode(self):
        return self.session_mode_value

    def start_session(self, config, auth=None, timeout=20):
        self.starts.append(config)
        self.session_mode_value = str(getattr(config, "mode", "") or "")

        class Result:
            def __init__(self, config):
                self.config = config
                self.role = config.role

        return Result(config)

    def stop_session(self, timeout=15):
        self.stops += 1
        self.session_mode_value = ""
        return True

    def watch_room_nat(self, enabled, log=None, standalone_host=None):
        self.punch_enabled.append(bool(enabled))

    def watch_room_direct(self, enabled, log=None):
        self.punch_enabled.append(bool(enabled))

    def room_direct_state(self):
        return self.room_punch

    def room_nat_profile(self):
        return self.nat_profile

    def room_nat_age(self):
        return None

    def traffic_snapshot(self):
        return {
            "relay_up": 0, "relay_down": 0,
            "direct_up": 0, "direct_down": 0,
            "total_up": 0, "total_down": 0,
            "up_rate": 0.0, "down_rate": 0.0,
        }

    def snapshot(self):
        data = {"role": None, "state": "IDLE", "state_text": "未启动"}
        data.update(self.traffic_snapshot())
        return data

    def relay_requires_account(self, *_a, **_k):
        return False

    def room_send_identity(self, username, token="", timeout=8):
        return True, "ok", username

    def _push(self, kind, **data):
        self.pushed.append((kind, data))

    def __getattr__(self, name):
        def stub(*_a, **_k):
            return None

        return stub


class Harness:
    def __init__(self):
        self.app = None
        self.logs = []
        self.warnings = []
        self.engine = FakeEngine()

    def build(self):
        try:
            import tkinter as tk
            from tkinter import messagebox
        except ImportError:
            return False

        messagebox.askokcancel = lambda *a, **k: True
        messagebox.showwarning = (
            lambda title, text, **k: self.warnings.append((title, text))
        )
        messagebox.showerror = (
            lambda title, text, **k: self.warnings.append((title, text))
        )
        messagebox.showinfo = (
            lambda title, text, **k: self.warnings.append((title, text))
        )

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

        app.engine = self.engine
        app.log = lambda text, kind="normal": self.logs.append(str(text))

        import types

        app.config = types.SimpleNamespace(
            username="root", role="host", server_host="relay.example",
            client_id="cid-me",
        )
        app.running = True
        app._committed["mc_port"] = 25565
        app._committed["local_port"] = 25565
        app._committed["max_players"] = 4
        app.mc_port_var.set("25565")
        app.local_port_var.set("25565")
        app.max_players_var.set("4")
        app.room_var.set("reconnect-room")
        app.auth = types.SimpleNamespace(username="root", token="tok")

        self.app = app
        return True

    def close(self):
        if self.app is None:
            return

        with contextlib.suppress(Exception):
            self.root.destroy()


# ============================================================
# 1. 退避节奏 / 总窗口 / 终态不重试
# ============================================================

def test_backoff_sequence(harness):
    print("")
    print("---- 自动重连：退避 0.5 → 15s 封顶，第 3 行日志在每次尝试后出现 ----")

    app = harness.app
    harness.logs.clear()

    app.engine.room = RoomState(is_host=True)
    app._reconnect_phase = "running"
    app._reconnect_started_at = 0.0
    app._reconnect_attempt = 0
    app._rebuild_in_flight = False
    app._stopping_on_purpose = False

    # 让"等退避"这一步不真的睡：只检查调度决策。
    delays = []

    import threading

    class ImmediateThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    original_thread = threading.Thread
    threading.Thread = ImmediateThread

    try:
        for expected in (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 15.0):
            app._reconnect_next_at = 0.0
            app._reconnect_started_at = time.time()
            before = len(harness.logs)
            app._trigger_auto_reconnect("test drop")

            # 从这一轮新落的日志里读退避秒数（界面刷新也会写日志，所以按前缀找）
            fresh = [
                line for line in harness.logs[before:]
                if line.startswith("[reconnect] attempt ")
            ]
            got = float(fresh[0].split(" in ")[1].split("s ")[0])
            delays.append(got)

        check(
            "★ 退避序列 0.5/1/2/4/8/15/15（封顶 15s）",
            delays == [0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 15.0],
            repr(delays),
        )
        check(
            "★ 每次尝试都有第一行英文日志 `[reconnect] attempt N in Xs (reason=…)`",
            all(
                line.startswith("[reconnect] attempt ")
                and "(reason=" in line
                for line in harness.logs
                if "attempt" in line
            ),
            repr(harness.logs[-3:]),
        )
    finally:
        threading.Thread = original_thread


def test_total_window_gives_up(harness):
    print("")
    print("---- 自动重连：60s 总窗口用完 -> 停下 + 弹窗 + 按钮恢复 ----")

    app = harness.app
    harness.logs.clear()
    harness.warnings.clear()

    app.engine.room = RoomState(is_host=True)
    app._reconnect_phase = "running"
    app._reconnect_attempt = 7
    app._stopping_on_purpose = False
    app._rebuild_in_flight = False
    # 第一次触发是 61 秒前 -> 已经超出窗口
    app._reconnect_started_at = time.time() - 61.0

    started = app._trigger_auto_reconnect("test drop")

    check("★ 超出 60s 窗口 -> 不再调度", started is False, repr(started))
    check(
        "★ 进入终态（界面写「停止（原因）」）",
        app._reconnect_phase == "stopped"
        and "自动重连超时" in app._reconnect_state_text(),
        app._reconnect_state_text(),
    )
    check(
        "★ 弹了一次窗（不是静默放弃）",
        bool(harness.warnings),
        repr(harness.warnings),
    )
    check(
        "★ 落了一行 give up 日志",
        any("[reconnect] give up" in line for line in harness.logs),
        repr(harness.logs[-3:]),
    )


def test_terminal_reasons_never_retry(harness):
    print("")
    print("---- 终态永不重连：停止 / 被踢 / 关房 / 版本门 / 认证失效 ----")

    app = harness.app

    for reason in (
        "VERSION_MISMATCH server=3.2.0 client=3.1.0",
        "kicked from the room: KICKED",
        "room closed by the server: ROOM REVOKED",
        "ERROR AUTH_INVALID_TOKEN AUTH_INVALID_TOKEN",
        "authentication required",
    ):
        check(
            f"★ 终态：{reason.split()[0]}",
            app._is_terminal_failure(reason) is True,
            reason,
        )

    # 普通掉线**不是**终态（要重连）
    check(
        "★ 普通掉线不是终态（要重连）",
        app._is_terminal_failure("connection reset by peer") is False,
    )

    # 用户自己点停止：running=False -> 直接不重连
    app.running = False
    app._reconnect_phase = "running"
    app._reconnect_started_at = 0.0
    harness.logs.clear()
    check(
        "★ 用户点停止之后绝不重连",
        app._trigger_auto_reconnect("dropped") is False
        and not any("attempt" in line for line in harness.logs),
        repr(harness.logs),
    )


def test_no_host_reasons_are_recognised():
    print("")
    print("---- 房主离线原因识别：HOST_LEFT / NO_HOST 都认 ----")

    from mclink.gui import app as gui_app

    for reason in (
        "HOST_LEFT",
        "ERROR NO_HOST",
        "host_left: the host disconnected",
        "no_host",
    ):
        check(
            f"★ 识别为'无 host'：{reason}",
            gui_app.MCLinkApp._is_no_host_reason(reason) is True,
            reason,
        )

    check(
        "普通网络错误不算无 host",
        gui_app.MCLinkApp._is_no_host_reason("connection reset") is False,
    )


# ============================================================
# 2. host 那边出事 —— 三种情况（用户 2026-09-30 定稿）
# ============================================================
#
# 设计里**没有"房主已离线"这个概念**：房主真离开房间 = 房间直接销毁。
# host 角色出事分三种：闪断（尽量复用/自动重连）、主动停止开服（断开+重算，
# 不自动重连、不弹窗）、主动释放主机位（断开+重算+弹窗，不自动重连）。

def test_host_left_flash_reconnects(harness):
    print("")
    print("---- host 闪断：连接态 -> 自动重连（复用优先）；**不弹窗** ----")

    app = harness.app
    harness.warnings.clear()
    harness.logs.clear()

    app.engine.room = RoomState(is_host=False, host_claimed=True)
    app.running = True
    app._reconnect_phase = "running"
    app._reconnect_started_at = 0.0

    app._on_host_gone("left", "HOST_LEFT")

    check(
        # ★ 用户 2026-10-03：host 一走，全房一并断开（不再自动重连）。
        "★★ host 断开（HOST_LEFT）+ 我在连接态 -> 断开并进入 host_down（不自动重连）",
        app._reconnect_phase == "host_down" and app.running is False,
        app._reconnect_phase,
    )
    check(
        "★★ 不再有「房主已离线」这种弹窗（用户点名删掉）",
        not any("房主已离线" in title for title, _text in harness.warnings),
        repr(harness.warnings),
    )
    check(
        "★ 行内提示说的是 host（不是「房主」）",
        "host" in app.connect_hint.cget("text"),
        app.connect_hint.cget("text"),
    )


def test_host_stopped_and_released(harness):
    print("")
    print("---- host 主动停止开服 / 主动释放主机位：断开 + 不自动重连 ----")

    app = harness.app

    # ---- ① 主动停止开服：断开 + 不弹窗 ----
    harness.warnings.clear()
    harness.logs.clear()
    app.engine.room = RoomState(is_host=False, host_claimed=True)
    app.running = True
    app._reconnect_phase = "running"
    app._reconnect_started_at = 0.0
    stops_before = app.engine.stops

    app._on_host_gone("stopped", "HOST_STOPPED")

    check(
        "★★ host 主动停止 -> 会话停掉 + 不自动重连（phase=host_down）",
        app.engine.stops == stops_before + 1
        and app.running is False
        and app._reconnect_phase == "host_down"
        and app._can_auto_reconnect() is False,
        f"stops={app.engine.stops} running={app.running} "
        f"phase={app._reconnect_phase}",
    )
    check(
        "★ host 主动停止 -> **不弹窗**（用户口径）",
        harness.warnings == [],
        repr(harness.warnings),
    )

    # ---- ② 主动释放主机位：断开 + 弹「host 已断开连接」 ----
    harness.warnings.clear()
    app.engine.room = RoomState(is_host=False, host_claimed=True)
    app.running = True
    app._reconnect_phase = "running"
    stops_before = app.engine.stops

    app._on_host_gone("released", "HOST_RELEASED")

    check(
        "★★ host 主动释放 -> 会话停掉 + 弹窗「host 已断开连接」+ 不自动重连",
        app.engine.stops == stops_before + 1
        and app._reconnect_phase == "host_down"
        and any(
            "host 已断开连接" in title for title, _text in harness.warnings
        ),
        f"stops={app.engine.stops} phase={app._reconnect_phase} "
        f"warnings={harness.warnings}",
    )


def test_start_is_refused_while_waiting_for_host(harness):
    print("")
    print("---- 无 host 时，玩家身份点「开始连接」被拒并提示 ----")

    app = harness.app
    harness.warnings.clear()
    harness.logs.clear()

    app.engine.room = RoomState(
        is_host=False, is_owner=False, host_claimed=False, host_name="",
        member_names={"cid-me": "root", "cid-peer": "radar"}, members=2,
        peer_ids=["cid-peer"],
    )
    app.running = False
    app._reconnect_phase = "waiting_host"

    app.on_start()

    check(
        "★ 没有开始连接（没有调用 start_session）",
        not app.engine.starts,
        repr(app.engine.starts),
    )
    check(
        "★ 给了清晰提示（host 未开服）",
        bool(harness.warnings),
        repr(harness.warnings),
    )
    check(
        "★★ 提示里说的是 **host**（设计里没有「房主已离线」这个说法）",
        any("host" in text for _t, text in harness.warnings)
        and not any("房主" in text for _t, text in harness.warnings),
        repr(harness.warnings),
    )


# ============================================================
# 3. 单人文案 BUG：等待对端用户加入…（不倒计时）
# ============================================================

def test_single_room_waits_without_a_timer():
    print("")
    print("---- 单人房间：显示「等待对端用户加入…」且不显示秒数 ----")

    from mclink.core.room_direct import RoomDirectLink

    link = RoomDirectLink.__new__(RoomDirectLink)
    link._punch_phase = ""
    link._phase_entered = 0.0
    link.attempts = 0

    link._set_phase(RoomDirectLink.PHASE_WAITING_FOR_PEER)

    # 假装已经等了 123 秒
    link._phase_entered = time.monotonic() - 123.0

    text = link.direct_step()

    check(
        "★ 文案是「等待对端用户加入…」",
        text == RoomDirectLink.PHASE_WAITING_FOR_PEER,
        repr(text),
    )
    check(
        "★ 不倒计时 / 也不正计时（没有秒数）",
        "s）" not in text and "秒" not in text and "123" not in text,
        repr(text),
    )


def test_announce_wait_has_a_bound():
    print("")
    print("---- 有对端但未收到公告：有上限的'第 N 秒'文案 ----")

    from mclink.core.room_direct import RoomDirectLink

    link = RoomDirectLink.__new__(RoomDirectLink)
    link._punch_phase = ""
    link._phase_entered = 0.0
    link.attempts = 0
    link._set_phase(RoomDirectLink.PHASE_WAITING_FOR_ANNOUNCE)
    link._phase_entered = time.monotonic() - 7.0

    text = link.direct_step()

    check(
        "★ 显示「等待对端起跑公告（第 7 秒）」",
        text.startswith(RoomDirectLink.PHASE_WAITING_FOR_ANNOUNCE)
        and "第 7 秒" in text,
        repr(text),
    )

    # 两边就绪（punch 报的倒计时）照旧原样显示
    link._set_phase("等待同步起降：0.9s for the coordinated start")
    text = link.direct_step()

    check(
        "★ 已收到公告时，倒计时文案原样保留",
        text.startswith("等待同步起降："),
        repr(text),
    )


def test_single_room_card_shows_waiting_not_a_timer(harness):
    print("")
    print("---- 真界面状态卡：单人房间显示「等待对端用户加入…」，不是正计时 ----")

    app = harness.app

    # 假打洞协调器：单人房间 = 花名册里除我之外没人。
    app.engine.room_punch = {
        "direct_step": "等待对端用户加入…",
        "direct_attempts": 0,
        "direct_via": None,
        "direct_peer": "",
        "inbound_state": "",
        "inbound_note": "",
        "stun_ready": False,
        "punching": True,
        "links": [],
        "direct_links_hint": "",
        "phase": "stun",
    }

    app.engine.room = RoomState(members=1, peer_ids=[], is_host=True)
    app.running = True
    app.config.mode = "stun"

    with contextlib.suppress(Exception):
        app._refresh_status()

    text = ""

    with contextlib.suppress(Exception):
        text = str(app.status_vars["punch"].get())

    check(
        "★ 状态卡的「打洞进度」写「等待对端用户加入…」",
        "等待对端用户加入" in text,
        repr(text),
    )
    check(
        "★ 且没有秒数（不倒计时 / 不正计时）",
        "s）" not in text and "秒" not in text,
        repr(text),
    )


# ============================================================

def main():
    print("=" * 64)
    print("MC-Link auto-reconnect / host-left / single-player wording tests")
    print("=" * 64)

    try:
        import tkinter  # noqa: F401
    except ImportError as e:
        check("tkinter available", False, str(e))
        return 1

    harness = Harness()

    if not harness.build():
        check("真界面能构建（需要 X display）", False, "Tk 建不起来")
        return 1

    try:
        test_no_host_reasons_are_recognised()
        test_backoff_sequence(harness)
        test_total_window_gives_up(harness)
        test_terminal_reasons_never_retry(harness)
        test_host_left_flash_reconnects(harness)
        test_host_stopped_and_released(harness)
        test_start_is_refused_while_waiting_for_host(harness)
        test_single_room_waits_without_a_timer()
        test_announce_wait_has_a_bound()
        test_single_room_card_shows_waiting_not_a_timer(harness)
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
