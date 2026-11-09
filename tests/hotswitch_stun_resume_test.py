"""
CraftLink —— 用户 2026-09-29（第二轮）第 3 条：

    「STUN 换中转可以触发自动重连……但是中转换 STUN 未触发正确自动重连，
      就是自动重建隧道并开始自动对齐流程开始打洞，这个并未触发，
      状态机显示已连接，但是按钮状态是释放掉了的，实际也并没有开启打洞流程。
      而且一个房间周期可能会多次热切换模式，所以这东西你做好活性，
      避免只第一次可以，多操作几次就不行了。
      还有热切换的文案提示……‘会断开连接当前所有人需要手动连接’，这个提示过时了。」

这一套钉住三件事：

  A. **活性**：只要"房间是 STUN 且会话在跑"，房间打洞循环就必须在跑 ——
     循环因为任何原因退出（热切换拆旧通路 / `room_current()` 短暂为 None /
     上一次 stop 没能真的取消掉旧协程）都要能自己回来。旧代码用
     `_established and transport_ready` 当第二道闸门，把"重启循环"和
     "重打这个洞"混在一起：洞/引用还在时 `start()` 永远提前 return，
     打洞再也不会恢复（用户看到的"没有开启打洞流程"）。
  B. **重建成功 = 界面回到"在连接"**：`_rebuild_tunnels` 成功只说明
     `start_session` 没抛异常，它不动 `self.running`；而 `_sync_room_nat`
     的开关是 `stun_room and self.running` —— `running` 为 False 会把刚重建
     起来的打洞**每秒关一次**（"按钮释放 + 状态机显示已连接 + 没打洞"）。
  C. **文案**：热切换的确认框 / 房主提示不许再说"所有人掉线、需要手动重连"
     （现在是自动重连；空闲的成员保持空闲）。

Run:
    python tests/hotswitch_stun_resume_test.py
"""

import asyncio
import contextlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import room_direct as rd  # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================================
#  装置：最小替身（只够跑 `start()` / `_run()` 的活性语义）
# ============================================================================

class FakeState:
    def __init__(self, mode="stun"):
        self.client_id = "p-cid"
        self.peer_ids = ["h-cid"]
        self.members = 2
        self.is_host = False
        self.started_ids = ["h-cid"]
        self.host_claimed = True
        self.host_id = "h-cid"
        self.host_name = "host"
        self.self_name = "player"
        self.member_names = {"h-cid": "host", "p-cid": "player"}
        self.member_conns = {"h-cid": "pairing", "p-cid": "idle"}
        self.conn_mode = mode


class FakeRoom:
    def __init__(self):
        self.state = None


class FakeEngine:
    """`loop()` 返回真事件循环，`room_current()` 可控（None 会让 `_run` 退出）。"""

    def __init__(self, state, room=True):
        self._state = state
        self._room = FakeRoom() if room else None
        self._loop = None

    def set_loop(self, loop):
        self._loop = loop

    def loop(self):
        return self._loop

    def room_state(self):
        return self._state

    def room_current(self):
        return self._room

    def room_nat_profile(self):
        return None

    def server_host(self):
        return "127.0.0.1"

    def server_port(self):
        return 19100


def test_start_restarts_a_dead_loop_with_a_live_link():
    print("")
    print("---- A. 活性：循环不在了，`start()` 必须能把它拉起来（洞还活着也一样）----")

    async def body():
        state = FakeState()
        engine = FakeEngine(state)
        engine.set_loop(asyncio.get_running_loop())
        punch = rd.RoomDirectLink(engine, log=lambda *a: None)

        # 先让循环真跑起来，再"伪造成已打通 + 循环不在了"：
        # 这正是热切换/`room_current()` 短暂为 None 之后现场的样子。
        punch.start()
        await asyncio.sleep(0.3)

        first_task = punch._task
        running_before = punch.running

        # 循环"没了"（`_task` 被清掉，但没有 close()），同时留一条活洞。
        class LiveLink:
            keepalive_running = True
            closed = False

        punch.links["h-cid"] = {
            "link": LiveLink(), "addr": ("5.6.7.8", 6000), "ready_at": None,
        }
        punch._established = True
        punch._task = None

        # 第一轮循环可能已经打过一次（`start()` 不清 attempts，只有 `stop()` 清）：
        # 这里量的是"重启之后有没有**再**打"。
        attempts_before = punch.attempts

        check(
            "（前提）循环本来在跑，且洞是活的",
            running_before and punch.transport_ready,
            f"running={running_before} transport={punch.transport_ready}",
        )

        punch.start()
        await asyncio.sleep(0.4)

        check(
            "★★ 循环死 + 洞活：`start()` 必须**重新起循环**"
            "（旧闸门 `_established and transport_ready` 会永远挡住 -> 打洞再也起不来）",
            punch.running,
            f"running={punch.running} task={punch._task}",
        )
        check(
            "★★ 重新起的循环**不许重打那条活洞**（洞还是同一条、attempts 没再涨）",
            punch.links.get("h-cid", {}).get("link") is not None
            and punch.attempts <= attempts_before,
            f"attempts={attempts_before} -> {punch.attempts} "
            f"links={list(punch.links)}",
        )

        punch.close()

        if first_task is not None:
            with contextlib.suppress(Exception):
                first_task.cancel()

    asyncio.run(body())


def test_superseded_loop_exits_by_itself():
    print("")
    print("---- A2. 旧循环必须自己退出（不许两条 `_run()` 同时在跑）----")

    async def body():
        state = FakeState()
        engine = FakeEngine(state)
        engine.set_loop(asyncio.get_running_loop())
        punch = rd.RoomDirectLink(engine, log=lambda *a: None)

        punch.start()
        await asyncio.sleep(0.3)

        stale_task = punch._task
        stale_token = punch._run_token

        # 模拟"stop() 没能真的取消掉正在跑的协程"，然后又 start() 了一轮。
        punch._task = None
        punch.start()
        await asyncio.sleep(0.4)

        check(
            "★ 新循环在跑",
            punch.running,
            f"running={punch.running}",
        )
        check(
            "★ 旧 token 已经过期（旧循环抬头就会退出）",
            punch._run_token is not stale_token,
            "token",
        )

        punch.close()

        for task in (stale_task,):
            with contextlib.suppress(Exception):
                task.cancel()

    asyncio.run(body())


# ============================================================================
#  B. 界面：重建成功 -> running 回来 -> 打洞被拉起来（连续多次）
# ============================================================================

class _GuiEngine:
    """只记录 `watch_room_nat` / `watch_room_direct` / `session_mode` 的引擎替身。"""

    def __init__(self, mode="relay"):
        self.room = _GuiRoom(mode)
        self.session_mode_value = mode
        self.nat_calls = []
        self.direct_calls = []
        self.pushed = []

    def room_state(self):
        return self.room

    def room_current(self):
        return object()

    def session_mode(self):
        return self.session_mode_value

    def start_session(self, config, auth=None, timeout=20):
        self.session_mode_value = str(config.mode)
        return object()

    def stop_session(self, timeout=15):
        self.session_mode_value = ""
        return True

    def watch_room_nat(self, enabled, log=None, standalone_host=None):
        self.nat_calls.append(bool(enabled))
        self.watch_room_direct(enabled, log=log)

    def watch_room_direct(self, enabled, log=None):
        self.direct_calls.append(bool(enabled))

    def room_direct_state(self):
        return {}

    def room_nat_profile(self):
        return None

    def room_nat_age(self):
        return None

    def room_rtt_ms(self):
        return None

    def room_set_started(self, connecting, timeout=8):
        return True, "ok"

    def traffic_snapshot(self):
        return {
            "relay_up": 0, "relay_down": 0, "direct_up": 0, "direct_down": 0,
            "total_up": 0, "total_down": 0, "up_rate": 0.0, "down_rate": 0.0,
        }

    def snapshot(self):
        data = {"role": None, "state": "IDLE", "state_text": "未启动"}
        data.update(self.traffic_snapshot())
        return data

    def relay_requires_account(self, *_a, **_k):
        return False

    def _push(self, kind, **data):
        self.pushed.append((kind, data))

    def drain_events(self):
        out, self.pushed = self.pushed, []
        return [{"kind": k, **d} for k, d in out]


class _GuiRoom:
    def __init__(self, mode):
        self.conn_mode = mode
        self.is_host = False
        self.is_owner = False
        self.host_claimed = True
        self.host_id = "h-cid"
        self.host_name = "host"
        self.members = 2
        self.client_id = "p-cid"
        self.peer_ids = ["h-cid"]
        self.started_ids = ["h-cid"]
        self.member_conns = {"h-cid": "pairing", "p-cid": "idle"}
        self.member_names = {"h-cid": "host", "p-cid": "player"}
        self.member_rtts = {}
        self.self_name = "player"
        self.username = "player"
        self.own_name = "player"
        self.world_players = 0
        self.max_players = 8
        self.server_max_players = 8
        self.mode_reason = "owner"
        self.revoked = False
        self.revoked_reason = ""
        self.display = "78"
        self.joined_at = 0.0
        self.owner_id = ""


def _build_app():
    from tkinter import messagebox

    messagebox.askokcancel = lambda *a, **k: True
    messagebox.showwarning = lambda *a, **k: None
    messagebox.showerror = lambda *a, **k: None

    import tkinter as tk

    root = tk.Tk()
    root.withdraw()

    from mclink.gui import app as gui_app

    app = gui_app.MCLinkApp(root)
    engine = _GuiEngine("relay")
    app.engine = engine
    app.log = lambda *a, **k: None
    app.running = True
    app._punch_mode = "relay"
    app._reconnect_phase = "running"
    app._rebuild_in_flight = False
    return root, app, engine


def test_hot_switch_liveness_and_button_state():
    print("")
    print("---- B. 热切换（连续多次）：重建成功 -> running 回来 -> 打洞被拉起 ----")

    try:
        root, app, engine = _build_app()
    except Exception as e:  # noqa: BLE001
        check("界面侧热切换活性（需要 tkinter 显示）", False, repr(e))
        return

    rebuilds = []

    def fake_rebuild(mode=None):
        rebuilds.append(str(mode or app._room_conn_mode() or "?"))

    app._rebuild_tunnels = fake_rebuild

    def switch_to(mode):
        app._handle_core_event(
            "conn_mode_changed",
            {"reason": "连接模式已切换，请按新模式重新开始连接",
             "was_live": "1"},
        )
        engine.room.conn_mode = mode
        app._refresh_room_ui()

        engine.session_mode_value = mode
        engine._push("rebuild_done")
        app._drain_events()
        app._refresh_room_ui()

    try:
        # ① STUN -> relay -> STUN -> relay -> STUN：每一跳都要真的重建 + 对齐
        for mode in ("stun", "relay", "stun", "relay", "stun"):
            before = len(rebuilds)
            switch_to(mode)

            check(
                f"★ 切到 {mode}：真的请求了按新模式重建",
                len(rebuilds) > before and rebuilds[-1] == mode,
                f"rebuilds={rebuilds}",
            )
            check(
                f"★ 切到 {mode}：打洞开关 = {mode == 'stun'}"
                f"（不是只第一次可以）",
                engine.nat_calls[-1] is (mode == "stun"),
                f"nat_calls={engine.nat_calls[-3:]}",
            )
            check(
                f"★ 切到 {mode}：按钮状态 = 在连接（running 没有掉）",
                app.running is True and app._session_busy() is True,
                f"running={app.running} busy={app._session_busy()}",
            )

        # ② 关键回归：重建**成功那一刻 running 曾经是 False**
        #    （掉线处理 / 别的路径把它清了）-> 重建完成后必须补回来，
        #    否则 `_sync_room_nat` 会把刚起来的打洞每秒关一次。
        app.running = False
        engine.room.conn_mode = "stun"
        engine.nat_calls.clear()
        engine._push("rebuild_done")
        app._drain_events()

        check(
            "★★ 重建成功 -> `running` 被补回 True（用户："
            "状态机显示已连接、按钮却是释放的）",
            app.running is True,
            f"running={app.running}",
        )
        check(
            "★★ 同一次里打洞被重新拉起（watch_room_nat(True)）",
            engine.nat_calls and engine.nat_calls[-1] is True,
            f"nat_calls={engine.nat_calls}",
        )
    finally:
        with contextlib.suppress(Exception):
            root.destroy()


# ============================================================================
#  C. 文案：不许再说"所有人掉线、需要手动重连"
# ============================================================================

def test_mode_switch_text_is_not_outdated():
    print("")
    print("---- C. 热切换文案：自动重连（没有【手动】这回事了）----")

    app_source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()
    i18n_source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "i18n_tables.py"),
        encoding="utf-8",
    ).read()

    check(
        "★ 确认框不再说「所有正在游戏中的玩家掉线，需要他们重新连接一次」",
        "需要他们重新连接一次" not in app_source
        and "需要他们重新连接一次" not in i18n_source,
        "outdated confirm text",
    )
    check(
        "★ 确认框改成了「重建全部隧道 + 自动重连 + 空闲保持空闲」",
        "切换会【重建全部隧道】" in app_source
        and "切换会【重建全部隧道】" in i18n_source,
        "new confirm text",
    )
    check(
        "★ 房主那行提示也不再让人以为要手动重连",
        "客户端会自动重连（无需手动操作）" in app_source
        and "客户端会自动重连（无需手动操作）" in i18n_source,
        "owner hint",
    )
    check(
        "★ 英/日两套译文都跟着改了（不是只改中文）",
        "REBUILDS every tunnel" in i18n_source
        and "すべてのトンネルを再構築" in i18n_source,
        "translations",
    )
    check(
        "★ 日志不再写「players will drop and need to reconnect once」",
        "players will drop and need to reconnect once" not in app_source,
        "log wording",
    )


# ============================================================================

def main():
    print("=" * 70)
    print(" 热切换（中继 -> STUN）活性 / 按钮状态 / 文案")
    print("=" * 70)

    test_start_restarts_a_dead_loop_with_a_live_link()
    test_superseded_loop_exits_by_itself()
    test_hot_switch_liveness_and_button_state()
    test_mode_switch_text_is_not_outdated()

    print("")
    print(f"passed: {len(PASSED)}   failed: {len(FAILED)}")

    if FAILED:
        print("failed checks:")

        for name in FAILED:
            print(f"  - {name}")

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
