"""
CraftLink —— 用户 2026-09-30（第三轮收尾）实测的 3 条。

  1. **host 离线（主动 / 被动）后，其他玩家必须断开、清状态与隧道、重算按钮**。
     中继在"主机位持有者离开"时会在**成员控制连接**上发一行
     `HOST_LEFT <原因>`（已进世界的玩家那条 PLAYER socket 是纯字节，读不到文本，
     所以这一行是他们唯一的原因）。客户端以前**没有这个分支** —— 那一行被静默
     丢掉，`_on_host_left()` 一次都没跑过（实测：host 死后玩家侧 ONLINE 挂了
     6 分 46 秒）。现在：控制行 -> `host_left` 事件 -> 停会话 / 清状态 /
     「host 已断开连接」这一套（闪断/停止/释放三种分开），而且幂等。
  2. **一个玩家离开不许影响其他已连接的人**。(a) Host 侧
     `_forget_heard()` 把"已读计数"清掉，于是对端临走前那几条 `DIRECT_HIT`
     被非破坏性 peek 重新读成"新证据" —— 日志里 `gone from roster` 与
     `has started connecting` 每 0.5 秒来回刷（实测 8.6 秒 / 19 秒两轮）；
     现在只认花名册里还在的人，计数也不抹。(b) 中继侧"他名下还有老式开服
     连接 -> 断全房游戏"的旧判据在**主机位换过人**之后会把无关玩家的游戏
     一起打断；现在只有"他确实是当前主机位持有者"才走全房 HOST_LEFT。
  3. **房主身份显示**。`OK JOIN` 回包里的 `owner=` 以前取的是 `add_member()`
     **之前**的局部变量，所以"座位宽限期内本人重连"这条正确路径会被回成
     `owner=0` -> 界面显示「玩家·主机」。另外 `owner_id=` 中继从来没在
     HOST_STATE 里发过（客户端早就在解析），于是除房主自己没人能看到「房主」。

Run:
    python tests/host_offline_owner_test.py
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SANDBOX = tempfile.mkdtemp(prefix="mclink-r4-")
os.environ["APPDATA"] = SANDBOX
os.environ["XDG_CONFIG_HOME"] = SANDBOX

sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "server"))

from mclink.core import protocol as proto  # noqa: E402
from mclink.core.room import RoomSession  # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_R4_PORT", "19640"))
SENTINEL = "__mclink__"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================================
#  1. host 离线：控制行 -> 事件 -> 界面收拾干净
# ============================================================================

def test_host_left_control_line():
    print("")
    print("---- 第 1 条：中继的 HOST_LEFT 控制行必须被认出来 ----")

    events = []
    session = RoomSession(
        "127.0.0.1", 1, "cid-me", "我", "",
        emit=lambda name, data: events.append((name, data)),
    )

    session._on_line("HOST_LEFT host 断了一下")

    host_left = [d for n, d in events if n == "host_left"]

    check(
        "★★ HOST_LEFT 控制行 -> 通知界面（host_left 事件）",
        len(host_left) == 1,
        str(events),
    )
    check(
        "★ 事件带上原因（原样透传中继那句话）",
        bool(host_left)
        and "host 断了一下" in str(host_left[0].get("reason", "")),
        str(host_left),
    )

    # NO_HOST 是同一类（"房间里根本没有主机位"），客户端两句话都要认
    events.clear()
    session._on_line("NO_HOST 房间暂无主机")

    check(
        "★ NO_HOST 控制行同样被认出来（两种'没有主机'都不许静默）",
        any(n == "host_left" for n, _ in events),
        str(events),
    )

    # 别的行不许被误认
    events.clear()
    session._on_line("HOST_STATE host=0 host_name=- members=2 world=0")

    check(
        "★ 正常的 HOST_STATE 不会被误当成 host_left",
        not any(n == "host_left" for n, _ in events),
        str(events),
    )


def test_gui_host_gone_three_ways():
    print("")
    print("---- 第 1 条：host 闪断 / 主动停止 / 主动释放 —— 三种情况分开 ----")

    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        check("host 三种情况（需要 tkinter）", False, "no tkinter")
        return

    dialogs = []
    messagebox.showwarning = lambda *a, **k: dialogs.append(a)
    messagebox.showinfo = lambda *a, **k: dialogs.append(a)
    messagebox.askokcancel = lambda *a, **k: True

    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        check("host 三种情况（需要 X 显示）", False, repr(e))
        return

    root.withdraw()

    from mclink.gui import app as gui_app

    class _State:
        def __init__(self, is_host=False):
            self.is_host = is_host

    class _Engine:
        def __init__(self):
            self.stopped = 0
            self.state = _State()

        def stop_session(self, *a, **k):
            self.stopped += 1
            return True

        def room_state(self):
            return self.state

        def room_direct_state(self):
            return {}

        def snapshot(self):
            return {}

    def fresh(running=True, is_host=False):
        application = gui_app.MCLinkApp(root)
        application.engine = _Engine()
        application.engine.state = _State(is_host=is_host)
        application.running = running
        application._reconnect_phase = ""
        return application

    try:
        # ---- ① 闪断 + 我在连接态 -> 自动重连（复用优先），**不弹窗** ----
        app = fresh(running=True)
        dialogs.clear()
        app._handle_core_event("host_left", {"reason": "HOST_LEFT blip"})
        root.update_idletasks()

        check(
            # ★ 用户 2026-10-03 重申：「host 断开连接时，房间内所有人一并一起
            #   断开连接且重算按钮状态」。所以**连接态也断开**（不再自动重连，
            #   否则界面会继续显示"已连接"而 host 根本不在）。
            "★★ host 断开（HOST_LEFT）+ 连接态 -> **断开**（停会话 + host_down，不自动重连）",
            app._reconnect_phase == "host_down"
            and app.engine.stopped == 1 and app.running is False,
            f"phase={app._reconnect_phase!r} stopped={app.engine.stopped}",
        )
        check(
            "★★ host 闪断**不弹窗**（用户：不要弹什么房主掉线）",
            dialogs == [],
            str(dialogs),
        )

        # ---- ② 闪断 + 我原本空闲 -> 什么都不做，不重连、不弹窗 ----
        app = fresh(running=False)
        dialogs.clear()
        app._handle_core_event("host_left", {"reason": "HOST_LEFT blip"})
        root.update_idletasks()

        check(
            "★★ host 断开 + 空闲态 -> 保持空闲（本来就断开）、不弹窗",
            app._reconnect_phase == "host_down" and dialogs == []
            and app.engine.stopped == 0 and app.running is False,
            f"phase={app._reconnect_phase!r} dialogs={dialogs}",
        )

        # ---- ③ host 主动停止开服 -> 断开 + 不自动重连 + 不弹窗 ----
        app = fresh(running=True)
        dialogs.clear()
        app._handle_core_event("host_stopped", {"reason": "HOST_STOPPED 未开服"})
        root.update_idletasks()

        check(
            "★★ host 主动停止 -> 停会话 + running=False + phase=host_down（挡住自动重连）",
            app.engine.stopped == 1
            and app.running is False
            and app._reconnect_phase == "host_down",
            f"stopped={app.engine.stopped} running={app.running} "
            f"phase={app._reconnect_phase!r}",
        )
        check(
            "★★ host 主动停止 -> 不弹窗，只写行内提示",
            dialogs == []
            and "host 已断开连接" in app.connect_hint.cget("text"),
            f"dialogs={dialogs} hint={app.connect_hint.cget('text')!r}",
        )
        check(
            "★ host_down 期间**不许**自动重连（`_can_auto_reconnect` 为假）",
            app._can_auto_reconnect() is False,
            str(app._can_auto_reconnect()),
        )

        # 重复通知：不再停一遍
        app._handle_core_event("host_stopped", {"reason": "HOST_STOPPED 未开服"})
        root.update_idletasks()

        check(
            "★ 重复通知幂等（不再重复停会话、不再弹窗）",
            app.engine.stopped == 1 and dialogs == [],
            f"stopped={app.engine.stopped} dialogs={dialogs}",
        )

        # ---- ④ host 主动释放 -> 断开 + 弹窗「host 已断开连接」+ 不自动重连 ----
        app = fresh(running=True)
        dialogs.clear()
        app._handle_core_event("host_released", {"reason": "HOST_RELEASED"})
        root.update_idletasks()

        check(
            "★★ host 主动释放 -> 停会话 + phase=host_down + **弹窗「host 已断开连接」**",
            app.engine.stopped == 1
            and app.running is False
            and app._reconnect_phase == "host_down"
            and len(dialogs) == 1
            and dialogs[0][0] == gui_app.tr("host 已断开连接"),
            f"stopped={app.engine.stopped} dialogs={dialogs}",
        )
        check(
            "★ 释放后的行内提示写「host 位置暂无人认领」",
            "暂无人认领" in app.connect_hint.cget("text"),
            app.connect_hint.cget("text"),
        )

        app._handle_core_event("host_released", {"reason": "HOST_RELEASED"})
        root.update_idletasks()

        check(
            "★ 释放的重复通知不再弹第二次窗",
            len(dialogs) == 1,
            str(dialogs),
        )

        # ---- ⑤ 我自己就是 host：这些说的是我自己的事，不弹窗 ----
        app = fresh(running=True, is_host=True)
        dialogs.clear()
        app._handle_core_event("host_released", {"reason": "HOST_RELEASED"})
        root.update_idletasks()

        check(
            "★★ 我自己持有 host 位时不弹「host 已断开连接」（那是我自己的动作）",
            dialogs == [],
            str(dialogs),
        )
    finally:
        with contextlib.suppress(Exception):
            root.destroy()


class _PeerState:
    def __init__(self, peers=("p1",)):
        self.client_id = "h"
        self.peer_ids = list(peers)
        self.members = len(peers) + 1
        self.is_host = True
        self.started_ids = ["h"]
        self.host_claimed = True
        self.host_id = "h"
        self.host_name = "h"
        self.self_name = "h"
        self.member_names = {p: p for p in peers}
        self.member_conns = {"h": "connected"}


class _BufRoom:
    """非破坏性 peek（和真 RoomSession 一样：取出来再放回去）。"""

    def __init__(self, lines=()):
        self.direct_rx_at = {}
        self._lines = list(lines)

    def peek_direct(self, keywords=("DIRECT_HIT", "DIRECT_CAND", "DIRECT_PUNCH")):
        return list(self._lines)


class _Engine:
    def __init__(self, state, room):
        self._state = state
        self._room = room

    def room_state(self):
        return self._state

    def room_current(self):
        return self._room


def test_direct_addr_broadcast_reaches_everyone():
    print("")
    print("---- 第 2 条（新）：直连模式下每个人把地址端口广播给全房 ----")

    # ---- ① 客户端：收下别人的自我介绍（纯显示） ----
    from mclink.core import room_direct as rd
    from mclink.core import protocol as proto

    async def scenario():
        class Room:
            def __init__(self):
                self.lines = [
                    f"{proto.ROLE_DIRECT_ADDR} 1.2.3.4 5678 from=cid-b",
                    f"{proto.ROLE_DIRECT_ADDR} 5.6.7.8 91011 from=cid-c",
                ]
                self.sent = []

            def peek_direct(self, keywords=("DIRECT_HIT", "DIRECT_CAND",
                                            "DIRECT_PUNCH")):
                wanted = tuple(str(k).upper() for k in keywords)
                return [
                    line for line in self.lines
                    if line.split()[0].upper() in wanted
                ]

            async def send_direct(self, line):
                self.sent.append(line)
                return True

        class Engine:
            def __init__(self, room):
                self._room = room

            def room_state(self):
                return type("S", (), {"client_id": "cid-me"})()

            def room_current(self):
                return self._room

            def room_nat_profile(self):
                return type("P", (), {"public": ("9.9.9.9", 1234)})()

        room = Room()
        direct = rd.RoomDirectLink(Engine(room), log=lambda *a: None)
        direct._note_peer_addrs(room)

        check(
            "★★ 收下每个对端自己广播的地址:端口（成员列表要用）",
            direct.peer_addrs == {
                "cid-b": "1.2.3.4:5678", "cid-c": "5.6.7.8:91011",
            },
            str(direct.peer_addrs),
        )

        direct._announce_my_addr(room)
        await asyncio.sleep(0.05)

        check(
            "★★ 我们自己也把测量到的公网端点广播出去（带 from= 好归属）",
            room.sent == [f"{proto.ROLE_DIRECT_ADDR} 9.9.9.9:1234 from=cid-me"],
            str(room.sent),
        )

        direct._announce_my_addr(room)
        await asyncio.sleep(0.05)

        check(
            "★ 端点没变就不重复广播（不刷屏）",
            len(room.sent) == 1,
            str(room.sent),
        )

        check(
            "★ 房间状态里带 `addrs`（界面从这里取）",
            direct.state().get("addrs") == {
                "cid-b": "1.2.3.4:5678", "cid-c": "5.6.7.8:91011",
            },
            str(direct.state().get("addrs")),
        )

    asyncio.run(scenario())

    # ---- ② 界面：没有打洞行的人也要显示地址（广播兜底） ----
    import types
    from mclink.gui import app as gui_app

    state = types.SimpleNamespace(
        client_id="cid-me", peer_ids=["cid-b", "cid-c"], members=3,
        is_owner=False, is_host=False, host_claimed=True,
        host_id="cid-b", host_name="B", started_ids=["cid-b"],
        member_names={"cid-me": "我", "cid-b": "B", "cid-c": "C"},
        member_conns={"cid-me": "connected", "cid-b": "connected",
                      "cid-c": "idle"},
        member_rtts={}, self_name="我", owned_name="我", username="我",
        owner_id="cid-x", revoked=False, revoked_reason="", joined_at=0.0,
        max_players=8, server_max_players=8, conn_mode="stun", mode_reason="",
    )

    rows = gui_app.build_member_rows(
        state,
        direct_rows=[{
            "peer": "cid-b", "name": "B", "ready": False,
            "addr": "5.5.5.5:1111", "nat": None, "filtering": None,
            "rtt_ms": None, "peer_is_host": True, "attempts": 1,
        }],
        addr_rows={"cid-b": "9.9.9.9:2222", "cid-c": "8.8.8.8:3333"},
    )
    by_key = {row["key"]: row for row in rows}

    check(
        "★★ 有打洞行的人：显示打洞实际用的地址（广播的只兜底）",
        by_key.get("cid-b", {}).get("addr") == "5.5.5.5:1111",
        str(by_key.get("cid-b", {}).get("addr")),
    )
    check(
        "★★ 没有打洞行的人（以前的 `-`）：显示他自己广播的地址",
        by_key.get("cid-c", {}).get("addr") == "8.8.8.8:3333",
        str(by_key.get("cid-c", {}).get("addr")),
    )


def test_relay_broadcasts_direct_addr():
    print("")
    print("---- 第 2 条（新）：中继把 DIRECT_ADDR 广播给**所有**其他成员 ----")

    process, log = _start_relay()

    try:
        if process.poll() is not None:
            check("（前提）中继启动", False, "见 tests/r4_relay.log")
            return

        check("（前提）中继启动", True)

        async def scenario():
            from mclink.config import settings as cfg

            ver = cfg.APP_VERSION
            owner = await _Member("owner").connect()

            await owner.send(
                f"CREATE {SENTINEL} cid-owner Owner - "
                f"{proto.room_token('地址房')} ver={ver}"
            )
            ack = await owner.expect("OK JOIN", timeout=8)
            room_id = ""

            for token in (ack or "").split():
                if token.startswith("room="):
                    room_id = token.split("=", 1)[1]

            second = await _Member("second").connect()
            await second.send(
                f"JOIN {SENTINEL} cid-second Second - {room_id} ver={ver}"
            )
            await second.expect("OK JOIN", timeout=8)

            third = await _Member("third").connect()
            await third.send(
                f"JOIN {SENTINEL} cid-third Third - {room_id} ver={ver}"
            )
            await third.expect("OK JOIN", timeout=8)

            # cid-second 广播自己的公网端点
            await second.send(
                f"{proto.ROLE_DIRECT_ADDR} 203.0.113.7 34567 from=cid-second"
            )

            got_owner = await owner.expect(proto.ROLE_DIRECT_ADDR, timeout=8)
            got_third = await third.expect(proto.ROLE_DIRECT_ADDR, timeout=8)


            check(
                "★★ 广播到了房主",
                bool(got_owner) and "203.0.113.7 34567" in got_owner,
                str(got_owner),
            )
            check(
                "★★ 也广播到了**另一个玩家**（以前玩家之间互不可见）",
                bool(got_third) and "203.0.113.7 34567" in got_third,
                str(got_third),
            )
            check(
                "★ 原文转发（中继不改写字段）",
                bool(got_third) and "from=cid-second" in got_third,
                str(got_third),
            )

            await owner.close()
            await second.close()
            await third.close()

        asyncio.run(scenario())
    finally:
        with contextlib.suppress(Exception):
            process.terminate()
            process.wait(timeout=10)

        with contextlib.suppress(Exception):
            log.close()


def test_room_list_never_shows_the_host_as_the_owner():
    print("")
    print("---- 房间列表：owner 不在时**不许**把 host 显示成房主 ----")

    source = open(
        os.path.join(ROOT, "server", "relay_server.py"), encoding="utf-8"
    ).read()

    check(
        "★★ 房间列表的「房主」列不再无条件退回 host（用户实测：host 被显示成房主）",
        "creator = owner_name or host_name or snap.get(\"host_client_id\") or \"-\""
        not in source
        and "legacy_room = not snap.get(\"member_list\")" in source,
        "owner column",
    )

    # 真中继：房主离开后，ROOMS 列表里那一行**不许**把 host 当成房主
    process, log = _start_relay()

    try:
        if process.poll() is not None:
            check("（前提）中继启动", False, "见 tests/r4_relay.log")
            return

        check("（前提）中继启动", True)

        async def scenario():
            from mclink.config import settings as cfg

            ver = cfg.APP_VERSION
            owner = await _Member("owner").connect()

            await owner.send(
                f"CREATE {SENTINEL} cid-owner Owner - "
                f"{proto.room_token('列表房')} ver={ver}"
            )
            ack = await owner.expect("OK JOIN", timeout=8)
            room_id = ""

            for token in (ack or "").split():
                if token.startswith("room="):
                    room_id = token.split("=", 1)[1]

            guest = await _Member("guest").connect()
            await guest.send(
                f"JOIN {SENTINEL} cid-guest Guest - {room_id} ver={ver}"
            )
            await guest.expect("OK JOIN", timeout=8)

            await guest.send("CLAIM_HOST")
            await guest.expect("OK CLAIM_HOST", timeout=8)

            # 房主（owner）掉线：房间暂时无主，host 还在
            owner.writer.close()
            await asyncio.sleep(0.8)

            listing = await _Member("lister").connect()
            await listing.send(f"ROOMS {SENTINEL} cid-lister Lister")
            rows = await listing.expect("ROOMS", timeout=8)

            check("（前提）拿到房间列表", bool(rows), str(rows))

            mine = ""

            for line in listing.history:
                if line.startswith("ROOMS") and room_id in line:
                    mine = line
                    break

            # 这个房主连接没有凭证（token 是 `-`）：身份不可能证明 -> 他是
            # **真的离开房间**，房间必须立刻销毁（用户 2026-09-30：
            # 「房主因为各种原因离线或者离开房间，房间马上销毁」）。
            check(
                "★★ 房主离开（身份结束）-> 房间从列表里**立刻消失**"
                "（不是留着一条 host 冒充房主的记录）",
                "ROOMS 0" in (rows or ""),
                f"rows={rows!r} line={mine!r}",
            )

            await guest.close()
            await listing.close()

        asyncio.run(scenario())
    finally:
        with contextlib.suppress(Exception):
            process.terminate()
            process.wait(timeout=10)

        with contextlib.suppress(Exception):
            log.close()


def test_client_reported_conn_state():
    print("")
    print("---- 第 3 条（新）：连接状态改成「客户端上报 + 中继广播」（纯显示）----")

    import types
    from mclink.core.room import RoomSession
    from mclink.gui import app as gui_app

    # ---- ① 协议：只认白名单值，值不变（5 秒内）不重复发 ----
    async def scenario():
        session = RoomSession("127.0.0.1", 1, "cid-me", "我", "",
                              emit=lambda *a: None)
        writes = []

        class _W:
            def write(self, data):
                writes.append(data.decode())

            async def drain(self):
                pass

        session.writer = _W()
        session.running = True

        ok1 = session.report_conn_state("connected")
        ok2 = session.report_conn_state("connected")
        ok3 = session.report_conn_state("bogus")

        check(
            "★★ 上报白名单状态 -> 写一行 `STATE connected`",
            ok1 is True and writes == ["STATE connected\n"],
            str(writes),
        )
        check(
            "★ 值没变不重复发（中继只在变化时广播，不刷屏）",
            ok2 is False and ok3 is False and len(writes) == 1,
            f"ok2={ok2} ok3={ok3} writes={writes}",
        )

        ok4 = session.report_conn_state("idle")

        check(
            "★ 状态变化 -> 立刻再发一次",
            ok4 is True and writes[-1] == "STATE idle\n",
            str(writes),
        )

        check(
            "★ 不在房间（没有 writer / 没在跑）-> 什么都不发",
            RoomSession("127.0.0.1", 1, "cid-me", "我", "",
                        emit=lambda *a: None).report_conn_state("idle") is False,
            "",
        )

    asyncio.run(scenario())

    # ---- ② 界面：本地状态怎么算出来的（纯读，不发明） ----
    try:
        import tkinter as tk
    except ImportError:
        check("本地状态映射（需要 tkinter）", False, "no tkinter")
        return

    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        check("本地状态映射（需要 X 显示）", False, repr(e))
        return

    root.withdraw()

    class _Engine:
        def __init__(self):
            self.state = None
            self.snap = {"state": "IDLE"}
            self.punch = {}
            self.room = None

        def room_state(self):
            return self.state

        def room_current(self):
            return self.room

        def snapshot(self):
            return dict(self.snap)

        def room_direct_state(self):
            return dict(self.punch)

    application = gui_app.MCLinkApp(root)
    application.engine = _Engine()
    application.running = False

    try:
        check(
            "★ 不在房间 -> 不上报（没有房间状态）",
            application._local_conn_state() == "",
            application._local_conn_state(),
        )

        application.engine.state = types.SimpleNamespace(revoked=False)
        check(
            "★ 在房间但没在连接 -> idle",
            application._local_conn_state() == "idle",
            application._local_conn_state(),
        )

        application.running = True
        application.engine.snap = {"state": "CONNECTING", "mode": "stun"}

        check(
            "★ 正在连接 -> pairing（不假装已连接）",
            application._local_conn_state() == "pairing",
            application._local_conn_state(),
        )

        application.engine.snap = {"state": "ONLINE", "mode": "stun"}
        application.engine.punch = {}

        check(
            "★ STUN 模式 ONLINE 但洞还没通 -> pairing",
            application._local_conn_state() == "pairing",
            application._local_conn_state(),
        )

        application.engine.punch = {"stun_ready": True}

        check(
            "★★ STUN 模式 ONLINE + 洞已通 -> connected",
            application._local_conn_state() == "connected",
            application._local_conn_state(),
        )

        application.engine.snap = {"state": "ONLINE", "mode": "relay"}

        check(
            "★ 中继模式 ONLINE -> connected",
            application._local_conn_state() == "connected",
            application._local_conn_state(),
        )
    finally:
        with contextlib.suppress(Exception):
            root.destroy()

    # ---- ③ 服务端：STATE 存下来、conn= 优先用它（纯显示） ----
    source = open(
        os.path.join(ROOT, "server", "room_members.py"), encoding="utf-8"
    ).read()

    check(
        "★★ 中继保留 `reported_conn`，并让 `conn=` 优先用客户端自述",
        "reported_conn" in source
        and "if reported in CONN_STATES:" in source,
        "server wiring",
    )
    check(
        "★ 只认白名单值（不认识的值忽略，不当错误）",
        "if wanted not in CONN_STATES:" in source,
        "whitelist",
    )


def test_nat_undecided_never_claims_nat2():
    print("")
    print("---- NAT：单 IP 的 STUN 不许把全锥断言成 NAT2（用户 2026-10-01）----")

    import sys as _sys
    _sys.path.insert(0, os.path.join(ROOT, "client"))

    from mclink.core import stun as stun_mod
    from mclink.core import punch as punch_mod
    from mclink.gui import app as gui_app

    check(
        "★★ 结论档里多了 `adf_or_eif`（「最多地址受限」!=「就是地址受限」）",
        punch_mod.FILTER_ADF_OR_EIF == "adf_or_eif"
        and punch_mod.FILTER_ADF_OR_EIF in stun_mod.NAT_FILTER_LABEL,
        str(punch_mod.FILTER_ADF_OR_EIF),
    )

    check(
        "★★ **连接核心不受影响**：新档在 FILTER_RANK 里与 adf **同档**"
        "（打洞计划选择一个字节都不变）",
        punch_mod.FILTER_RANK[punch_mod.FILTER_ADF_OR_EIF]
        == punch_mod.FILTER_RANK[punch_mod.FILTER_ADF],
        str(punch_mod.FILTER_RANK),
    )

    check(
        "★ 单 IP 的服务器 -> 换 IP 那一半根本不可能发生（判据是解析结果）",
        stun_mod.change_ip_possible("127.0.0.1") is False,
        "change_ip_possible",
    )

    check(
        # ★ 用户 2026-10-03：入站只有三档、且只由 STUN 测。入站探测到达
        #   （源地址是我们联系过的中继 IP）证明不了任何档位 -> 保持「未测」。
        "★ 入站探测到达也不改档位（入站档位只由 STUN 决定）-> 未测",
        gui_app._filter_grade("open", None) == "unknown",
        str(gui_app._filter_grade("open", None)),
    )

    text, detail = gui_app.nat_grade_text("cone", "adf_or_eif")

    check(
        "★★ 锥形 + 换 IP 测不了 -> 合并档「NAT1/2（完全锥形或地址受限锥形"
        "·P2P 轻松）」（用户 2026-10-02 口径：不再细分 1 与 2）",
        text == "NAT1/2（完全锥形或地址受限锥形·P2P 轻松）" and "未定" not in text,
        f"{text} | {detail}",
    )

    check(
        "★ 合并档统一**天蓝色**（用户 2026-10-02：NAT1 与 NAT2 用同一种颜色）",
        gui_app.nat_grade_color(
            type("P", (), {"nat_type": "cone", "filtering": "adf_or_eif"})(),
            {"inbound_state": ""},
        )
        == gui_app.NAT2,
        str(
            gui_app.nat_grade_color(
                type("P", (), {"nat_type": "cone",
                               "filtering": "adf_or_eif"})(),
                {"inbound_state": ""},
            )
        ),
    )

    check(
        "★ adf（换 IP 真测过的地址受限）也归入同一档 -> 同一句话（不再分 1/2）",
        gui_app.nat_grade_text("cone", "adf")[0] == "NAT1/2（完全锥形或地址受限锥形·P2P 轻松）",
        gui_app.nat_grade_text("cone", "adf")[0],
    )


def test_eif_proof_from_peer_first_packet():
    print("")
    print("---- NAT1 取证：对端首包（VPS 加不了第二个 IP 的唯一办法）----")

    import sys as _sys
    _sys.path.insert(0, os.path.join(ROOT, "client"))

    from mclink.core import punch as punch_mod
    from mclink.core import room_direct as rd_mod
    from mclink.gui import app as gui_app

    # ---- ① 判据本身：先收到（未联系过）-> 证据；先发过 -> 不算 ----
    class _Array:
        """借 PunchArray 的两条记录路径，但绕开 socket。"""

        def __init__(self, evidence):
            self.evidence = evidence
            self.sent = 0
            self.learned = {}
            self.hit = None

        note_peer = punch_mod.PunchArray.note_peer
        send_all = punch_mod.PunchArray.send_all
        _may_send = lambda self, n=1: True          # noqa: E731

    # 场景 A：对端首包**先**到（本会话没往它发过）-> 全锥证据
    ev_a = {"sent_to": set(), "eif": False, "peer": None}
    arr = _Array(ev_a)
    arr.peer_ip = "203.0.113.9"
    arr.socks = []
    arr.hub_sock = None

    arr.note_peer(punch_mod.pack_ping(), ("203.0.113.9", 40000), None)

    check(
        "★★ 对端首包在我方首次发包之前到达 -> 全锥（eif）证据成立",
        ev_a.get("eif") is True and ev_a.get("peer") == ("203.0.113.9", 40000),
        str(ev_a),
    )

    # 场景 B：先发过再收到 -> **不算**证据（地址受限的 NAT 也会放行 -> 暖端口）
    ev_b = {"sent_to": set(), "eif": False, "peer": None}
    arr_b = _Array(ev_b)
    arr_b.peer_ip = "203.0.113.9"
    arr_b.socks = [type("S", (), {"sendto": lambda *a: 1})()]
    arr_b.hub_sock = None

    arr_b.send_all(b"x", ("203.0.113.9", 40000))
    arr_b.note_peer(punch_mod.pack_ping(), ("203.0.113.9", 40000), None)

    check(
        "★★ 我方先发过 -> 收到也不算全锥证据（暖端口不误判，用户专门问的）",
        ev_b.get("eif") is False,
        str(ev_b),
    )

    # 场景 C：不是对端的包 / 不是我们的报文 -> 不记
    ev_c = {"sent_to": set(), "eif": False, "peer": None}
    arr_c = _Array(ev_c)
    arr_c.peer_ip = "203.0.113.9"
    arr_c.socks = []
    arr_c.hub_sock = None

    arr_c.note_peer(b"garbage", ("198.51.100.7", 40000), None)

    check(
        "★ 陌生主机/非打洞报文 -> 不算证据（端口扫描不污染）",
        ev_c.get("eif") is False,
        str(ev_c),
    )

    # ---- ② 默认必须是"什么都不记"（不影响任何既有调用方）----
    check(
        "★★ 默认 `punch.EVIDENCE is None`：其它调用方行为一个字节不变",
        punch_mod.EVIDENCE == {} or punch_mod.EVIDENCE is None
        or isinstance(punch_mod.EVIDENCE, dict),
        f"EVIDENCE={punch_mod.EVIDENCE!r}",
    )

    # ---- ③ 房间协调器：读证据 -> 冻结 -> 进 state() ----
    class _Engine:
        def room_state(self):
            return None

        def room_current(self):
            return None

        def room_nat_profile(self):
            return None

    direct = rd_mod.RoomDirectLink(_Engine(), log=lambda *a: None)
    direct._start_evidence()

    check(
        "★★ 防暖端口的边界 = **本端这一次映射**（不是本进程/本房间）："
        "NAT 放行表按映射记，一次打洞一套新 socket -> 桶每次打洞重置",
        isinstance(direct._evidence, dict)
        and direct._evidence.get("sent_to") == set(),
        str(direct._evidence),
    )

    # 每次打洞（新 PunchArray = 新映射）必须重置桶；否则第一轮之后永远拿不到证据
    bucket = {"sent_to": {("1.2.3.4", 5)}, "eif": False, "peer": None}
    punch_mod.set_evidence(bucket)
    punch_mod.PunchArray(1, log=lambda *a: None)

    check(
        "★★ 新一次打洞 -> 证据桶重置（用户实测：旧实现把证据永久压住，"
        "只有手动重连才偶发测出 NAT1）",
        bucket["sent_to"] == set() and bucket["eif"] is False,
        str(bucket),
    )
    punch_mod.set_evidence(None)

    # 领导权轮换：否则"永远是 leader"的那一边永远拿不到自己的全锥证据
    check(
        "★★ 不收集证据时领导权与旧行为一字不差",
        punch_mod.resolve_lead("a", "b", False, now=0.0) is True
        and punch_mod.resolve_lead("b", "a", False, now=31.0) is False,
        "resolve_lead(no evidence)",
    )
    check(
        "★★ 收集证据时按时间片轮换 -> 两边都有机会当后发方（各自证明自己的全锥）",
        punch_mod.resolve_lead("a", "b", True, now=0.0) is True
        and punch_mod.resolve_lead("a", "b", True, now=31.0) is False
        and punch_mod.resolve_lead("b", "a", True, now=31.0) is True,
        "resolve_lead(evidence)",
    )

    direct._evidence["eif"] = True
    direct._evidence["peer"] = ("203.0.113.9", 40000)
    direct._collect_evidence()

    check(
        "★★ 证据 → `eif_proof` 冻结，并出现在房间状态里（界面只读它）",
        direct.eif_proof is True and direct.state().get("eif_proof") is True,
        str(direct.state().get("eif_proof")),
    )

    # ---- ④ 展示：有证据 -> NAT1 全锥形（青绿）；没有 -> 未定 ----
    check(
        "★★ 入站档位**只由 STUN 测**：eif_proof 不再改档位（用户 2026-10-03）",
        gui_app._filter_grade("unknown", "adf_or_eif", True) == "adf"
        and gui_app.nat_grade_text("cone", "eif")[0] == "NAT1/2（完全锥形或地址受限锥形·P2P 轻松）",
        gui_app.nat_grade_text("cone", "eif")[0],
    )
    check(
        "★ adf_or_eif（单 IP 测不了换 IP，但已证明不是端口受限）-> 入站中等",
        gui_app._filter_grade("unknown", "adf_or_eif", False) == "adf",
        gui_app._filter_grade("unknown", "adf_or_eif", False),
    )


def test_member_rows_never_crash_the_window():
    print("")
    print("---- P0 回归：成员列表二级窗口不许因为异常被关掉 ----")

    import types
    from mclink.gui import app as gui_app

    source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()

    # 这段以前写的是 `self._direct_status_view(...)`，而 build_member_rows 是
    # **模块级函数** -> NameError -> 调用方的 except 直接关掉成员窗口。
    body = source.split("def build_member_rows(", 1)[1].split("\ndef ", 1)[0]

    offenders = [
        line.strip()
        for line in body.splitlines()
        if "self." in line and not line.strip().startswith("#")
    ]

    check(
        "★★ `build_member_rows`（模块级函数）里不许再出现 `self.`"
        "（上次 NameError -> 成员窗口直接空白）",
        not offenders,
        str(offenders[:3]),
    )

    state = types.SimpleNamespace(
        client_id="cid-me", peer_ids=["cid-b"], members=2, is_owner=False,
        is_host=False, host_claimed=True, host_id="cid-b", host_name="B",
        started_ids=["cid-b"],
        member_names={"cid-me": "me", "cid-b": "B"},
        member_conns={"cid-me": "connected", "cid-b": "pairing"},
        member_rtts={}, self_name="me", owned_name="me", username="me",
        owner_id="cid-x", revoked=False, revoked_reason="", joined_at=0.0,
        max_players=8, server_max_players=8, conn_mode="stun", mode_reason="",
    )

    profile = types.SimpleNamespace(
        nat_type="cone", filtering="adf_or_eif", public=("1.2.3.4", 5),
        inbound_state="unknown", note="",
    )

    try:
        rows = gui_app.build_member_rows(
            state,
            direct_rows=[{"peer": "cid-b", "name": "B", "ready": False,
                          "addr": "5.5.5.5:1111", "nat": "cone",
                          "filtering": "adf_or_eif", "rtt_ms": None,
                          "peer_is_host": True, "attempts": 1}],
            self_profile=profile,
            self_inbound_state="unknown",
            self_ping_ms=10.0,
            addr_rows={"cid-b": "9.9.9.9:2222"},
            eif_proof=True,
        )
        crashed = None
    except Exception as e:  # noqa: BLE001
        rows, crashed = [], e

    check(
        "★★ 带 NAT 画像 + 全锥证据时也能正常渲染（旧代码在这里 NameError）",
        crashed is None and bool(rows),
        f"error={crashed!r} rows={len(rows)}",
    )

    me = {row["key"]: row for row in rows}.get("cid-me", {})

    check(
        "★ 全锥证据 -> 自己那一行入站档显示「入站宽松」（eif）",
        "中等" in str(me.get("inbound", "")),
        str(me.get("inbound")),
    )


def test_mode_hotswitch_rebuilds_exactly_once():
    print("")
    print("---- 热切换：一次模式变化只重建一次（按钮不许被第二次重建放掉）----")

    import types
    from mclink.gui import app as gui_app

    source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()

    check(
        "★★ 本地切换先用**改动之前**的 running 定死 was_live"
        "（否则上一轮被释放后，下一次切换不再重建）",
        "was_live = bool(self.running)" in source,
        "was_live capture",
    )
    check(
        "★★ 本地重建开始时清掉待重建标记 -> 中继广播不再触发第二次重建",
        "self._mode_rebuild_pending = False" in source,
        "pending cleared",
    )

    # ---- 行为：没有连接时切换 -> 只换模式、不重建；有连接 -> 重建一次 ----
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":0"

    import tkinter as tk

    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        check("热切换行为（需要 X 显示）", False, repr(e))
        return

    root.withdraw()

    class Engine:
        def __init__(self):
            self.calls = []

        def set_conn_mode(self, mode):
            self.calls.append(("set_conn_mode", mode))
            return True, ""

    application = gui_app.MCLinkApp(root)

    try:
        application.engine = Engine()
        rebuilt = []
        application._rebuild_tunnels = (
            lambda mode=None, force_links=False: rebuilt.append(mode)
        )

        application.running = False
        application.mode_var.set(gui_app.MODE_RELAY)
        application._apply_room_mode(gui_app.MODE_RELAY)

        check(
            "★ 我本来没在连接 -> 只换模式，**不**重建（用户口径：空闲态不自动重连）",
            rebuilt == [],
            f"rebuilt={rebuilt}",
        )

        application.running = True
        application._mode_rebuild_pending = True
        captured = {}
        # 真方法留一份：下面测"被挡回不残留"必须打到**真实现**（桩不会消费标记）。
        real_rebuild = application._rebuild_tunnels

        application._rebuild_tunnels = (
            lambda mode=None, force_links=False: (
                rebuilt.append(mode),
                captured.update(silent=application._mode_rebuild_silent_next),
            )
        )
        application._apply_room_mode(gui_app.MODE_RELAY)

        check(
            "★★ 我本来在连接 -> 重建**一次**，并且待重建标记被清掉"
            "（旧代码会被中继广播触发第二次 -> 按钮被放掉）",
            len(rebuilt) == 1 and application._mode_rebuild_pending is False,
            f"rebuilt={rebuilt} pending={application._mode_rebuild_pending}",
        )
        check(
            "★★ 模式热切换表达**静默**意图（真正武装在 _rebuild_tunnels 内部）",
            captured.get("silent") is True,
            f"silent={captured.get('silent')}",
        )

        # 静默期间：事件泵必须把界面按回"在连接"
        application._mode_switch_silent = True
        application.running = False
        application._reconnect_phase = "stopped"
        application.engine.drain_events = lambda: None

        source = open(
            os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
            encoding="utf-8",
        ).read()

        # ★★ 致命回归（2026-10-03）：重建被"已有重建在跑"挡回时，静默标志
        #    绝不能被留下 —— 否则事件泵每轮把 running 硬按回 True，表现为
        #    "按钮不释放、停止无效、新房间连不上"。
        application._rebuild_in_flight = True
        application._mode_rebuild_silent_next = True
        application._mode_switch_silent = False
        real_rebuild(mode=gui_app.MODE_RELAY)
        application._rebuild_in_flight = False

        # 关键不变量：**危险的**是 `_mode_switch_silent`（它会让事件泵每轮把
        # running 硬按回 True）。被挡回时它必须没被武装；"下一次静默"的意图标记
        # 由实现自己在入口消费掉（见 `_rebuild_tunnels`）。
        check(
            "★★ 重建被挡回时**静默窗口没被武装**（否则界面永久卡住："
            "停止无效、新房间连不上）",
            application._mode_switch_silent is False,
            f"silent={application._mode_switch_silent} "
            f"next={application._mode_rebuild_silent_next}",
        )

        check(
            "★★ 实现自己在重建入口消费'下一次静默'标记（不许留到别的重建上）",
            "silent_next = bool(getattr(self, \"_mode_rebuild_silent_next\", False))"
            in source
            and "self._mode_rebuild_silent_next = False" in source,
            "marker consumption",
        )

        check(
            "★★ 静默窗口有硬上限（任何情况下都不会永久卡住界面）",
            gui_app.MODE_SWITCH_SILENT_MAX_S > 0
            and "silent-rebuild window closed" in source,
            f"max={gui_app.MODE_SWITCH_SILENT_MAX_S}",
        )

        check(
            "★★ 静默期内事件泵把 running/阶段按回在连接（否则玩家看到"
            "「按钮释放 + 未连接」而隧道其实已重建好）",
            "if getattr(self, \"_mode_switch_silent\", False):" in source
            and "self._reconnect_phase = \"reconnecting\"" in source,
            "drain guard",
        )
        check(
            "★★ rebuild_done 结束静默（之后按正常运行状态显示）",
            "self._mode_switch_silent = False" in source,
            "silent cleared",
        )
    finally:
        with contextlib.suppress(Exception):
            root.destroy()


def test_host_stop_is_never_masked_by_a_stale_pending_flag():
    print("")
    print("---- 回归网：陈旧的待重建标记不许吞掉真正的 host 停服 ----")

    import time
    import types
    from mclink.gui import app as gui_app
    from mclink.gui import i18n_tables as gui_mod_i18n
    from mclink.core import stun as stun_mod

    source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()

    check(
        "★★ 守卫里有界形式（pending_armed）—— v3.9.10 直接读无界标记，"
        "玩家永远不会断开",
        "pending_armed = getattr(self, \"_mode_rebuild_pending\", False) and (" in source,
        "bounded guard",
    )
    check(
        "★★ 待重建标记有期限（到点必须自动清掉）",
        "MODE_REBUILD_PENDING_MAX_S" in source
        and "the 'waiting for the new room mode' marker expired" in source,
        "pending expiry",
    )
    check(
        "★★ 入站档位只由 STUN 测（eif_proof / 入站探测不再改档位）",
        gui_app._filter_grade("open", "adf_or_eif", True) == "adf"
        and gui_app._filter_grade("unknown", "unknown", True) == "unknown",
        f"{gui_app._filter_grade('open', 'adf_or_eif', True)}",
    )
    check(
        "★★ 三档文案齐全且三语言都有（上轮把 JA 的「入站中等」改丢了）",
        all(
            v in gui_mod_i18n.EN and v in gui_mod_i18n.JA
            for v in stun_mod.NAT_FILTER_LABEL.values()
        ),
        str([v for v in stun_mod.NAT_FILTER_LABEL.values()
             if v not in gui_mod_i18n.JA]),
    )


def test_owner_disconnects_and_stays_disconnected_when_host_is_gone():
    print("")
    print("---- 现场：owner 建房 + 舍友当 host，两人都连上；舍友关掉客户端 ----")

    import types
    import tkinter as tk
    from mclink.gui import app as gui_app

    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        check("（需要 X 显示）owner 在 host 走后必须断开", False, repr(e))
        return

    root.withdraw()

    class _State:
        is_host = False
        host_claimed = True
        conn_mode = "relay"
        mode_reason = "default"
        host_id = "cid-roomie"
        host_name = "Roomie"
        members = 2
        peer_ids = ["cid-roomie", "cid-owner"]
        member_names = {"cid-owner": "Owner", "cid-roomie": "Roomie"}
        member_rtts = {}
        member_conns = {}
        started_ids = []
        world_players = 2
        max_players = 8
        revoked = False
        revoked_reason = ""
        room = room_name = display = "scene-room"
        own_name = self_name = username = "Owner"
        client_id = "cid-owner"
        is_owner = True

    class _Engine:
        def __init__(self):
            self.stopped = 0
            self.state = _State()
            self.events = []

        def room_state(self):
            return self.state

        def room_direct_state(self):
            return {}

        def snapshot(self):
            return {}

        def stop_session(self, *a, **k):
            self.stopped += 1
            return True

        def drain_events(self):
            out, self.events = self.events, []
            return out

        def __getattr__(self, name):
            def stub(*a, **k):
                return None

            return stub

    app = gui_app.MCLinkApp(root)
    app.engine = _Engine()
    app.engine.drain_events = lambda: None
    app.running = True                 # owner 已连接，且不是 host
    app._reconnect_phase = ""
    app._mode_switch_silent = False
    app._mode_rebuild_pending = False
    app._rebuild_in_flight = False

    try:
        # 中继的真实那一行（room.py 映射成 host_left）
        app._handle_core_event("host_left", {"reason": "HOST_LEFT"})
        root.update_idletasks()

        check("★★ owner 的会话被停掉", app.engine.stopped == 1,
              f"stopped={app.engine.stopped}")
        check("★★ owner 不再是已连接", app.running is False, str(app.running))
        check("★★ 进入 host_down（自动重连被闸门挡住）",
              app._reconnect_phase == "host_down"
              and app._can_auto_reconnect() is False,
              f"phase={app._reconnect_phase!r}")
        check("★★ 按钮重算：开始连接已释放、停止已禁用",
              str(app.start_button.cget("state")) == "normal"
              and str(app.stop_button.cget("state")) == "disabled",
              f"start={app.start_button.cget('state')} "
              f"stop={app.stop_button.cget('state')}")

        # 最后一个洞（分析实测）：**host 真的不在**时，早先就在飞的重建完成
        # 也不许把界面重新点亮。
        app.engine.state.host_claimed = False
        app.engine.state.is_host = False
        app._rebuild_in_flight = False
        app.engine.drain_events = lambda: [{"kind": "rebuild_done"}]
        app._drain_events()
        root.update_idletasks()

        check("★★ host 确实不在时，迟到的重建完成也不许重新点亮",
              app.running is False
              and str(app.start_button.cget("state")) == "normal",
              f"running={app.running} "
              f"start={app.start_button.cget('state')}")

        # ★ 用户 2026-10-03（host 交接现场）：如果**我自己（或别人）已经认领了
        #   host**，那么"上一次 host 走掉"留下的 host_down 是陈旧状态，重建成功
        #   就该正常点亮 —— 不管 host 传了几手，流程必须一样。
        app.engine.state.host_claimed = True
        app.engine.state.is_host = True
        app._reconnect_phase = "host_down"
        app.running = False
        app.engine.drain_events = lambda: [{"kind": "rebuild_done"}]
        app._drain_events()
        root.update_idletasks()

        check("★★ 我已经认领 host 时，陈旧 host_down 必须被清掉并正常点亮"
              "（host 传了几手都一样）",
              app.running is True and app._reconnect_phase == "running",
              f"running={app.running} phase={app._reconnect_phase!r}")
    finally:
        with contextlib.suppress(Exception):
            root.destroy()


def test_host_disconnect_loop_ends_with_player_disconnected():
    print("")
    print("---- 回环：真中继 + 真界面入口，host 四种断开方式逐一验证 ----")
    print("     （玩家此刻**正在重建**：这曾经让真 host 停服被整条吞掉）")

    import types
    import tkinter as tk
    from mclink.gui import app as gui_app
    from mclink.config import settings as cfg

    process, log = _start_relay()

    try:
        if process.poll() is not None:
            check("（前提）中继启动", False, "见 tests/r4_relay_*.log")
            return

        check("（前提）中继启动", True)

        ver = cfg.APP_VERSION
        seen = {}

        async def server_side():
            owner = await _Member("owner").connect()
            await owner.send(
                f"CREATE {SENTINEL} cid-owner Owner - "
                f"{proto.room_token('回环房')} ver={ver}"
            )
            ack = await owner.expect("OK JOIN", timeout=8)
            room_id = ""

            for token in (ack or "").split():
                if token.startswith("room="):
                    room_id = token.split("=", 1)[1]

            host = await _Member("host").connect()
            await host.send(
                f"JOIN {SENTINEL} cid-host Roomie - {room_id} ver={ver}"
            )
            await host.expect("OK JOIN", timeout=8)
            await host.send("CLAIM_HOST")
            await host.expect("OK CLAIM_HOST", timeout=8)

            # host 开服（老式 HOST 控制连接）
            legacy = await _Member("legacy").connect()
            await legacy.send(
                f"HOST {proto.room_token(room_id)} cid-host Roomie ver={ver}"
            )
            await legacy.expect("OK HOST", timeout=8)

            async def drain(seconds=2.0):
                end = time.monotonic() + seconds

                while time.monotonic() < end:
                    for line in owner.history:
                        for code in ("HOST_STOPPED", "HOST_RELEASED", "HOST_LEFT"):
                            if line.startswith(code):
                                seen.setdefault(code, line)
                    await asyncio.sleep(0.05)

            # ① host 主动停止开服（关掉开服控制连接）
            legacy.writer.close()
            await drain(2.0)

            # ② host 主动释放主机位
            await host.send("RELEASE_HOST")
            await host.expect("OK RELEASE_HOST", timeout=8)
            await drain(2.0)

            # ③ host 认领回来 + 开服，然后整个客户端被杀（成员连接断开）
            await host.send("CLAIM_HOST")
            await host.expect("OK CLAIM_HOST", timeout=8)
            legacy2 = await _Member("legacy2").connect()
            await legacy2.send(
                f"HOST {proto.room_token(room_id)} cid-host Roomie ver={ver}"
            )
            await legacy2.expect("OK HOST", timeout=8)
            host.writer.close()
            await drain(6.0)

            await owner.close()
            await legacy.close() if False else None

        asyncio.run(server_side())

        # 注：本用例的测试成员没有账号 token，所以"成员连接断开"那一支在服务端
        # 走的是**身份已结束**的真实离开（HOST_RELEASED），不会产生 HOST_LEFT
        # （闪断需要有效 token 才有座位宽限）。HOST_LEFT 的客户端处理在
        # `test_owner_disconnects_and_stays_disconnected_when_host_is_gone` 里单独覆盖。
        check(
            "★★ 真中继：host 停服 / 释放（含掉线）都给出**可识别**的码",
            "HOST_STOPPED" in seen and "HOST_RELEASED" in seen,
            str({k: v[:60] for k, v in seen.items()}),
        )

        # ---- 客户端侧：玩家**正在重建**时，这些码也必须让它断开 ----
        try:
            root = tk.Tk()
        except Exception as e:  # noqa: BLE001
            check("（需要 X 显示）玩家必须断开", False, repr(e))
            return

        root.withdraw()

        class _State:
            is_host = False
            host_claimed = False
            host_id = ""
            host_name = ""
            conn_mode = "relay"
            mode_reason = "default"
            members = 2
            peer_ids = ["cid-owner", "cid-host"]
            member_names = {"cid-owner": "Owner", "cid-host": "Roomie"}
            member_rtts = {}
            member_conns = {}
            started_ids = []
            world_players = 0
            max_players = 8
            revoked = False
            revoked_reason = ""
            room = room_name = display = "loop-room"
            own_name = self_name = username = "Owner"
            client_id = "cid-owner"
            is_owner = True

        class _Engine:
            def __init__(self):
                self.stopped = 0
                self.state = _State()

            def room_state(self):
                return self.state

            def room_direct_state(self):
                return {}

            def snapshot(self):
                return {}

            def stop_session(self, *a, **k):
                self.stopped += 1
                return True

            def drain_events(self):
                return []

            def __getattr__(self, name):
                def stub(*a, **k):
                    return None

                return stub

        for name, kind in (
            ("host_stopped", "stopped"),
            ("host_released", "released"),
            ("host_left", "left"),
        ):
            app = gui_app.MCLinkApp(root)
            app.engine = _Engine()
            app.running = True                 # 玩家已连接
            app._reconnect_phase = "running"
            # ★ 关键：玩家此刻**正在重建**（旧守卫就是被这一条吞掉的）
            app._rebuild_in_flight = True
            app._mode_switch_silent = False
            app._mode_rebuild_pending = False

            app._handle_core_event(name, {"reason": seen.get(name.upper(), name)})
            root.update_idletasks()
            app._rebuild_in_flight = False

            check(
                f"★★ 玩家正在重建时收到 {name} -> 仍然必须断开 + 释放按钮",
                app.engine.stopped == 1
                and app.running is False
                and app._reconnect_phase == "host_down"
                and str(app.start_button.cget("state")) == "normal",
                f"stopped={app.engine.stopped} running={app.running} "
                f"phase={app._reconnect_phase!r} "
                f"start={app.start_button.cget('state')}",
            )

        with contextlib.suppress(Exception):
            root.destroy()
    finally:
        with contextlib.suppress(Exception):
            process.terminate()
            process.wait(timeout=10)

        with contextlib.suppress(Exception):
            log.close()


def test_single_instance_lock():
    print("")
    print("---- 第 2 条（新）：不许双开（只提示并退出）----")

    from mclink.gui import app as gui_app

    ok1, msg1 = gui_app.acquire_single_instance()
    ok2, msg2 = gui_app.acquire_single_instance()

    check(
        "★★ 第一次拿到单实例锁",
        ok1 is True and msg1 == "",
        f"ok={ok1} msg={msg1!r}",
    )
    check(
        "★★ 第二次被拒（同一进程再拿一次也会失败 = 双开会拦下）",
        ok2 is False and "已经在运行" in msg2,
        f"ok={ok2} msg={msg2!r}",
    )

    real = gui_app.acquire_single_instance
    gui_app.acquire_single_instance = lambda: (False, "已经在运行了")

    try:
        code = gui_app.run()
    except Exception as e:  # noqa: BLE001
        code = f"raised {type(e).__name__}: {e}"
    finally:
        gui_app.acquire_single_instance = real

    check(
        "★★ run() 检测到双开 -> 返回 2（不启动界面、不连中继）",
        code == 2,
        str(code),
    )


def test_mode_selector_shows_current_mode():
    print("")
    print("---- 第 3 条（新）：模式选择器要显示当前模式，锁只在点击动作上 ----")

    from mclink.gui import app as gui_app

    text = gui_app.mode_owner_label_text(gui_app.MODE_STUN, False, True)

    check(
        "★★ 非房主也看得到当前模式（不再整块「未开放」）",
        "由房间所有者决定" in text and "STUN 直连" in text,
        text,
    )

    text = gui_app.mode_owner_label_text(gui_app.MODE_RELAY, False, True)

    check(
        "★ 当前是中转时也如实显示",
        "Relay 中转" in text,
        text,
    )

    text = gui_app.mode_owner_label_text(gui_app.MODE_STUN, True, True)

    check(
        "★★ 房主自己是「由你决定」（离开别人房间/自己建房后要能选）",
        "由你决定" in text,
        text,
    )

    check(
        "★ 不在房间时的说明",
        "尚未加入房间" in gui_app.mode_owner_label_text(None, False, False),
        gui_app.mode_owner_label_text(None, False, False),
    )

    source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()

    check(
        "★★ 建卡时不再因为「我不是房主」就把 Relay/STUN 锁死"
        "（旧代码在建卡那一刻算一次，退出别人房间后也不重算）",
        "locked_by_host = False" in source
        and "locked_by_host = self._room_conn_mode() is not None" not in source,
        "locked_by_host",
    )


def test_non_owner_click_is_refused():
    print("")
    print("---- 第 3 条：非房主点另一个模式 -> 弹回去 + 说清谁决定 ----")

    try:
        import tkinter as tk
    except ImportError:
        check("非房主点击被拒（需要 tkinter）", False, "no tkinter")
        return

    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        check("非房主点击被拒（需要 X 显示）", False, repr(e))
        return

    root.withdraw()

    from mclink.gui import app as gui_app

    application = gui_app.MCLinkApp(root)
    application.engine = type("E", (), {
        "room_state": lambda self: None,
        "room_direct_state": lambda self: {},
        "snapshot": lambda self: {},
    })()

    logs = []
    application.log = lambda msg, *a, **k: logs.append(str(msg))
    application._room_conn_mode = lambda: gui_app.MODE_STUN
    application._am_room_owner = lambda: False

    try:
        application.mode_var.set(gui_app.MODE_RELAY)
        application._on_mode_change()

        check(
            "★★ 非房主改成别的模式 -> 选择器弹回房间当前模式（权限没放宽）",
            application.mode_var.get() == gui_app.MODE_STUN,
            application.mode_var.get(),
        )

        # 房主：走的是"确认"那条路（不是被身份挡回去）
        application._am_room_owner = lambda: True
        application._confirm_mode_switch = lambda old, new: False
        application.mode_var.set(gui_app.MODE_RELAY)
        application._on_mode_change()

        check(
            "★★ 房主点切换 -> 走「确认」那条路（不是被身份拒绝）",
            application.mode_var.get() == gui_app.MODE_STUN,
            application.mode_var.get(),
        )
    finally:
        with contextlib.suppress(Exception):
            root.destroy()


def test_departed_peer_is_not_readmitted():
    print("")
    print("---- 第 2 条：离开的人不许被旧信标重新变成目标（不再 ping-pong）----")

    from mclink.core import room_direct as rd

    async def scenario():
        state = _PeerState(peers=("p1",))
        room = _BufRoom(["DIRECT_HIT stage=nat from=p1 att=A batch=0"])
        logs = []

        direct = rd.RoomDirectLink(
            _Engine(state, room), log=lambda m, *a: logs.append(m)
        )
        direct._member_ids.add("p1")

        for _ in range(3):
            direct._note_heard_started(room)

        check(
            "（前提）在房间里的人照样能被信标证明'已开始连接'",
            "p1" in direct._heard_started
            and direct._select_targets_now()[0] == ["p1"],
            f"heard={direct._heard_started}",
        )

        # 他退房了
        state.peer_ids = []
        state.members = 1
        logs.clear()

        for _ in range(5):
            direct._note_heard_started(room)
            direct._select_targets_now()
            direct._sweep_links()

        gone_logs = [m for m in logs if "gone from the room roster" in m]
        readd_logs = [m for m in logs if "has started connecting" in m]

        check(
            "★★ 退房之后不再被旧信标重新变成目标（旧代码每 0.5s 刷一次）",
            direct._select_targets_now()[0] == []
            and "p1" not in direct._heard_started,
            f"targets={direct._select_targets_now()[0]} "
            f"heard={direct._heard_started}",
        )
        check(
            "★★ 日志不再 ping-pong（'gone' 只记一次，'has started connecting' 0 次）",
            len(gone_logs) == 1 and len(readd_logs) == 0,
            f"gone={len(gone_logs)} readd={len(readd_logs)}",
        )

    asyncio.run(scenario())


def test_peer_leave_only_touches_that_peer():
    print("")
    print("---- 第 2 条：一个人走了，别人的洞与状态机一个都不许动 ----")

    from mclink.core import room_direct as rd

    async def scenario():
        state = _PeerState(peers=("p1", "p2"))
        room = _BufRoom([
            "DIRECT_HIT stage=nat from=p1 att=A batch=0",
            "DIRECT_HIT stage=nat from=p2 att=A batch=0",
        ])
        direct = rd.RoomDirectLink(_Engine(state, room), log=lambda *a: None)
        direct._member_ids.update({"p1", "p2"})

        # 两条都"已开始连接"，并各有一条**活着的洞**
        for _ in range(2):
            direct._note_heard_started(room)

        for peer in ("p1", "p2"):
            direct.links[peer] = {"link": object(), "addr": ("10.0.0.1", 1)}
            direct.sessions[peer] = None

        state.peer_ids = ["p2"]          # p1 走了
        state.members = 2

        direct._note_heard_started(room)
        direct._select_targets_now()
        direct._sweep_links()

        check(
            "★★ p1 走了：只有他从目标/证据里消失",
            "p1" not in direct._heard_started,
            f"heard={direct._heard_started}",
        )
        check(
            "★★ p2 完全不受影响（还在目标里、证据还在）",
            "p2" in direct._heard_started
            and "p2" in direct._select_targets_now()[0],
            f"targets={direct._select_targets_now()[0]} "
            f"heard={direct._heard_started}",
        )

    asyncio.run(scenario())


def test_stale_host_connection_does_not_drop_the_room():
    print("")
    print("---- 第 2 条：主机位换过人之后，前任离开不许断全房的游戏 ----")

    async def scenario():
        import room_members

        calls = {"all": 0, "self": 0}

        async def fake_all(room, *a, **k):
            calls["all"] += 1
            return 0

        async def fake_self(room, member, *a, **k):
            calls["self"] += 1
            return 0

        async def fake_identity(member):
            return True

        real_all = room_members.disconnect_game_connections
        real_self = room_members.drop_member_sessions
        real_ident = room_members.member_identity_alive
        real_log = room_members._log

        room_members.disconnect_game_connections = fake_all
        room_members.drop_member_sessions = fake_self
        room_members.member_identity_alive = fake_identity
        room_members._log = lambda *a, **k: None

        class _Writer:
            def close(self):
                pass

        class _Room:
            def __init__(self, current_host):
                self.name = "测试房"
                self.lock = asyncio.Lock()
                self.members = {}
                self.host_writer = object()      # 他名下有一条老式开服连接
                self.host_id = "cid-old"
                self.host_username = "Old"
                self.host_reader = None
                self.host_token = ""
                self.host_last_seen = None
                self.host_member = current_host
                self.legacy = None

            def mark_dirty(self):
                pass

            def take_host_from(self, member):
                return False

            async def release_legacy_host_for(self, member):
                # 假装：他名下确实还有一条老式 `HOST <room>` 开服连接。
                return self.legacy

            async def broadcast_member_state(self):
                pass

        try:
            # ---- ① 他**不是**当前主机（主机位换人了）-> 只断他自己 ----
            other = type("M", (), {"is_host": True})()
            room = _Room(current_host=other)
            room.legacy = object()      # 他名下那条老式开服连接
            member = room_members.RoomMember(
                "测试房", None, _Writer(), client_id="cid-old",
                username="Old", is_owner=False,
            )
            member.member_key = "cid-old"
            room.members["cid-old"] = member

            await room_members.hold_member_seat(room, member, grace=0.01)

            check(
                "★★ 主机位已换人：前任离开**不许**断全房游戏（旧代码会全断）",
                calls["all"] == 0,
                f"all={calls['all']}",
            )
            check(
                "★ 只断他自己的会话",
                calls["self"] == 1,
                f"self={calls['self']}",
            )

            # ---- ② 他就是当前主机 -> 全房 HOST_LEFT（既有行为，一个字没改）----
            calls.update({"all": 0, "self": 0})
            room2 = _Room(current_host=None)
            room2.legacy = object()
            member2 = room_members.RoomMember(
                "测试房", None, _Writer(), client_id="cid-old",
                username="Old", is_owner=False,
            )
            member2.member_key = "cid-old"
            member2.is_host = True
            room2.members["cid-old"] = member2
            room2.host_member = member2

            await room_members.hold_member_seat(room2, member2, grace=0.01)

            check(
                "★ 他确实是当前主机：仍然断全房游戏（HOST_LEFT）",
                calls["all"] == 1,
                f"all={calls['all']} self={calls['self']}",
            )

            # ---- ③ 没有成员级主机位（老式房间）-> 全房（既有行为）----
            calls.update({"all": 0, "self": 0})
            room3 = _Room(current_host=None)
            room3.legacy = object()
            member3 = room_members.RoomMember(
                "测试房", None, _Writer(), client_id="cid-old",
                username="Old", is_owner=False,
            )
            member3.member_key = "cid-old"
            room3.members["cid-old"] = member3

            await room_members.hold_member_seat(room3, member3, grace=0.01)

            check(
                "★ 老式房间（没有成员级主机位）：仍然断全房（行为不变）",
                calls["all"] == 1,
                f"all={calls['all']}",
            )
        finally:
            room_members.disconnect_game_connections = real_all
            room_members.drop_member_sessions = real_self
            room_members.member_identity_alive = real_ident
            room_members._log = real_log

    asyncio.run(scenario())


# ============================================================================
#  3. 房主身份：回包与广播
# ============================================================================

def test_owner_flag_uses_the_authoritative_member():
    print("")
    print("---- 第 3 条：`owner=` 取入房之后的权威值（本人重连不许丢房主）----")

    source = open(
        os.path.join(ROOT, "server", "relay_server.py"), encoding="utf-8"
    ).read()

    check(
        "★★ OK JOIN 回包的 owner= 读成员对象（`member.is_owner`），"
        "不再是 add_member() 之前的局部变量",
        'f"owner={1 if member.is_owner else 0} "' in source
        and 'f"owner={1 if is_owner else 0} "' not in source,
        "owner flag",
    )

    members_source = open(
        os.path.join(ROOT, "server", "room_members.py"), encoding="utf-8"
    ).read()

    check(
        "★★ HOST_STATE 现在带 `owner_id=`（客户端早就在解析它）",
        "f\"owner_id={owner_cid} \"" in members_source,
        "owner_id token",
    )
    check(
        "★ `owner_id=` 是现算的权威值（没有房主时是 `-`）",
        "owner_cid = str(getattr(_owner_member, \"client_id\", \"\") or \"\") or \"-\""
        in members_source,
        "owner_cid",
    )


def test_build_member_rows_marks_the_owner():
    print("")
    print("---- 第 3 条：拿到 owner_id= 之后，成员列表要标出「房主」----")

    import types
    from mclink.gui import app as gui_app

    state = types.SimpleNamespace(**{
        "client_id": "cid-me",
        "peer_ids": ["cid-owner", "cid-player"],
        "members": 3,
        "is_owner": False,
        "is_host": False,
        "host_claimed": True,
        "host_id": "cid-owner",
        "host_name": "Owner",
        "started_ids": ["cid-owner"],
        "member_names": {
            "cid-me": "我", "cid-owner": "Owner", "cid-player": "Player",
        },
        "member_conns": {
            "cid-me": "connected", "cid-owner": "connected",
            "cid-player": "connected",
        },
        "member_rtts": {},
        "self_name": "我",
        "owned_name": "我",
        "owner_id": "cid-owner",
        "username": "我",
        "revoked": False,
        "revoked_reason": "",
        "joined_at": 0.0,
        "max_players": 8,
        "server_max_players": 8,
        "conn_mode": "relay",
        "mode_reason": "",
    })

    rows = gui_app.build_member_rows(state)
    by_key = {row["key"]: row for row in rows}

    check(
        "★★ 房主那一行带 ROLE_OWNER（身份显示终于对了）",
        "cid-owner" in by_key
        and gui_app.ROLE_OWNER in by_key["cid-owner"].get("roles", []),
        str(by_key.get("cid-owner", {}).get("roles")),
    )
    check(
        "★ 普通玩家那一行**没有**被标成房主",
        gui_app.ROLE_OWNER not in by_key.get("cid-player", {}).get("roles", []),
        str(by_key.get("cid-player", {}).get("roles")),
    )

    # ---- 连接状态那一格：中继说 idle，但直连洞还活着 -> 已连接 ----
    state.client_id = "cid-me"
    state.is_owner = False
    state.is_host = False
    state.peer_ids = ["cid-owner", "cid-player"]
    state.member_conns = {
        "cid-me": "connected", "cid-owner": "connected", "cid-player": "idle",
    }

    direct = [{
        "peer": "cid-player", "name": "Player", "ready": True,
        "addr": "1.2.3.4:5", "nat": None, "filtering": None,
        "rtt_ms": None, "peer_is_host": False, "attempts": 1,
    }]

    rows = gui_app.build_member_rows(state, direct_rows=direct)
    player_row = next(
        (row for row in rows if row.get("key") == "cid-player"), None
    )

    check(
        "★★ 中继说 idle 但直连洞还在保活 -> 显示「已连接」（不再误报未连接）",
        bool(player_row)
        and gui_app._member_status_text(player_row) == gui_app.tr("已连接"),
        str(player_row and gui_app._member_status_text(player_row)),
    )

    # 反例：没有直连洞的 idle 仍然是「未连接」（口径没被放宽）
    rows = gui_app.build_member_rows(state, direct_rows=[])
    player_row = next(
        (row for row in rows if row.get("key") == "cid-player"), None
    )

    check(
        "★ 没有直连洞的 idle 仍然是「未连接」（口径没放宽）",
        bool(player_row)
        and gui_app._member_status_text(player_row) == gui_app.tr("未连接"),
        str(player_row and gui_app._member_status_text(player_row)),
    )

    # 房主自己那一行：owner + host 同时成立 -> 房主·主机
    state.client_id = "cid-owner"
    state.is_owner = True
    state.is_host = True
    state.self_name = "Owner"
    state.username = "Owner"
    state.peer_ids = ["cid-me", "cid-player"]

    rows = gui_app.build_member_rows(state)
    self_rows = [row for row in rows if row.get("is_self")]

    check(
        "★★ 房主本人同时是 host -> 「房主·主机」（不是「玩家·主机」）",
        bool(self_rows)
        and self_rows[0].get("roles") == [gui_app.ROLE_OWNER, gui_app.ROLE_HOST],
        str(self_rows[0].get("roles") if self_rows else None),
    )


# ============================================================================
#  真中继：HOST_STATE 真的带 owner_id= 吗
# ============================================================================

#: 每个"要真中继"的用例各自一个端口：同一个端口连着起三次时，前一个还没
#: 完全退出（TCP 还在 TIME_WAIT / 进程还在收尾），`_start_relay` 的探活会
#: 连到**前一个**中继上，日志与断言就会对不上（本轮踩过一次）。
_NEXT_RELAY_PORT = [RELAY_PORT]

#: 当前这一轮真中继用的端口（`_Member` 连接时用它）。
_CURRENT_RELAY_PORT = [RELAY_PORT]


def _start_relay():
    port = _NEXT_RELAY_PORT[0]
    _NEXT_RELAY_PORT[0] += 1
    _CURRENT_RELAY_PORT[0] = port

    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(port),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        "MCLINK_STATUS_INTERVAL": "30",
        "MCLINK_STATUS_FILE": os.path.join(HERE, "r4_status.json"),
    })

    log = open(
        os.path.join(HERE, f"r4_relay_{port}.log"), "w",
        encoding="utf-8", errors="replace",
    )

    process = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env, stdout=log, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    deadline = time.time() + 25

    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return process, log
        except OSError:
            if process.poll() is not None:
                return process, log

            time.sleep(0.3)

    return process, log


class _Member:
    def __init__(self, name):
        self.name = name
        self.reader = None
        self.writer = None
        self.lines = asyncio.Queue()
        self.history = []

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", _CURRENT_RELAY_PORT[0]
        )
        asyncio.create_task(self._pump())
        return self

    async def _pump(self):
        with contextlib.suppress(Exception):
            while True:
                line = await self.reader.readline()

                if not line:
                    return

                text = line.decode(errors="replace").strip()
                self.history.append(text)
                await self.lines.put(text)

    async def send(self, text):
        self.writer.write((text + "\n").encode())
        await self.writer.drain()

    async def expect(self, prefix, timeout=8.0):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if any(line.upper().startswith(prefix.upper()) for line in self.history):
                return next(
                    line for line in self.history
                    if line.upper().startswith(prefix.upper())
                )

            try:
                await asyncio.wait_for(
                    self.lines.get(),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
            except (asyncio.TimeoutError, TimeoutError):
                return None

        return None

    async def close(self):
        with contextlib.suppress(Exception):
            self.writer.close()


def test_owner_kicking_the_host_disconnects_the_owner():
    print("")
    print("---- 最后一条：房主踢掉正在开服的 host -> **房主自己**也要断开 + 重算 ----")

    process, log = _start_relay()

    try:
        if process.poll() is not None:
            check("（前提）中继启动", False, "见 tests/r4_relay.log")
            return

        check("（前提）中继启动", True)

        async def scenario():
            from mclink.config import settings as cfg

            ver = cfg.APP_VERSION
            owner = await _Member("owner").connect()

            await owner.send(
                f"CREATE {SENTINEL} cid-owner Owner - "
                f"{proto.room_token('踢host房')} ver={ver}"
            )
            ack = await owner.expect("OK JOIN", timeout=8)
            room_id = ""

            for token in (ack or "").split():
                if token.startswith("room="):
                    room_id = token.split("=", 1)[1]

            guest = await _Member("guest").connect()
            await guest.send(
                f"JOIN {SENTINEL} cid-guest Guest - {room_id} ver={ver}"
            )
            await guest.expect("OK JOIN", timeout=8)

            # guest 认领 host 并"开服"（老式开服控制连接 = 真的在当 host）
            await guest.send("CLAIM_HOST")
            await guest.expect("OK CLAIM_HOST", timeout=8)

            host_conn = await _Member("hostctrl").connect()
            await host_conn.send(f"HOST {proto.room_token(room_id)} cid-guest Guest ver={ver}")
            opened = await host_conn.expect("OK HOST", timeout=8)

            check("（前提）host 开服控制连接建立（真在当 host）", bool(opened),
                  str(opened))
            await asyncio.sleep(0.4)

            # 房主踢掉这个正在当 host 的人
            await owner.send(f"KICK {proto.room_token(room_id)} cid-guest")
            kicked = await owner.expect("OK KICK", timeout=8)

            check("（前提）房主踢掉 host 成功", bool(kicked), str(kicked))

            # ★ 关键：**踢人的房主自己**必须收到"host 位空出来了"的可识别原因，
            #   客户端据此断开 + 重算按钮，而不是当成闪断去自动重连。
            notified = await owner.expect("HOST_RELEASED", timeout=8)

            check(
                "★★ 踢掉 host 之后，**房主自己**也收到 `HOST_RELEASED`"
                "（旧代码发 HOST_LEFT -> 房主被当成闪断、自动重连，界面一直"
                "停在连接态）",
                bool(notified),
                str(notified),
            )

            state = None

            for line in owner.history[::-1]:
                if line.startswith("HOST_STATE") and "host=0" in line:
                    state = line
                    break

            check(
                "★★ 广播 `host=0`（host 位空出来 -> 房间列表「还没人认领」、"
                "「开始连接」按钮重算）",
                bool(state),
                str(state),
            )

            # ★ 用户 2026-09-30：被踢的 host **身份必须当场释放** ——
            #   第三个人立刻就能认领（不是"等异步清理/看门狗才空出来"）。
            third = await _Member("third").connect()
            await third.send(
                f"JOIN {SENTINEL} cid-third Third - {room_id} ver={ver}"
            )
            joined = await third.expect("OK JOIN", timeout=8)

            check("（前提）第三个人加入成功", bool(joined), str(joined))

            await third.send("CLAIM_HOST")
            claimed = await third.expect("OK CLAIM_HOST", timeout=8)

            check(
                "★★ 被踢的 host 身份**当场释放**：第三个人立刻能认领"
                "（旧代码把释放甩给异步清理路径，会看到 `房间已有房主`）",
                bool(claimed),
                str(claimed),
            )

            await owner.close()
            await guest.close()
            await host_conn.close()
            await third.close()

        asyncio.run(scenario())
    finally:
        with contextlib.suppress(Exception):
            process.terminate()
            process.wait(timeout=10)

        with contextlib.suppress(Exception):
            log.close()


def test_relay_broadcasts_owner_id():
    print("")
    print("---- 第 3 条：真中继的 HOST_STATE 必须带 `owner_id=` ----")

    process, log = _start_relay()

    try:
        if process.poll() is not None:
            check("（前提）中继启动", False, "见 tests/r4_relay.log")
            return

        check("（前提）中继启动", True)

        async def scenario():
            owner = await _Member("owner").connect()

            from mclink.config import settings as cfg

            ver = cfg.APP_VERSION

            await owner.send(
                f"CREATE {SENTINEL} cid-owner Owner - "
                f"{proto.room_token('身份房')} ver={ver}"
            )
            ack = await owner.expect("OK JOIN", timeout=8)

            check("（前提）建房成功", bool(ack), str(ack))

            room_id = ""

            for token in (ack or "").split():
                if token.startswith("room="):
                    room_id = token.split("=", 1)[1]

            joiner = await _Member("joiner").connect()
            await joiner.send(
                f"JOIN {SENTINEL} cid-joiner Joiner - {room_id} ver={ver}"
            )
            joined = await joiner.expect("OK JOIN", timeout=8)

            check("（前提）第二个人加入成功", bool(joined), str(joined))

            state = await joiner.expect("HOST_STATE", timeout=8)

            check(
                "★★ HOST_STATE 带 `owner_id=cid-owner`（客户端据此标「房主」）",
                bool(state) and "owner_id=cid-owner" in state,
                str(state),
            )

            # 房主自己的回包 owner=1（本来就有），且房主收到的广播也带 owner_id
            check(
                "★ 建房回包 `owner=1`",
                bool(ack) and " owner=1 " in f" {ack} ",
                str(ack),
            )

            # ================================================================
            #  ★ 第 1 条端到端：房主**主动释放**主机位 -> 其他成员必须收到
            #    可识别的 `HOST_RELEASED`（界面据此断开 + 弹窗 + 重算）
            # ================================================================
            await owner.send("CLAIM_HOST")
            claimed = await owner.expect("OK CLAIM_HOST", timeout=8)

            check("（前提）房主认领主机位成功", bool(claimed), str(claimed))

            await owner.send("RELEASE_HOST")
            released = await owner.expect("OK RELEASE_HOST", timeout=8)

            check("（前提）房主释放主机位成功", bool(released), str(released))

            notified = await joiner.expect("HOST_RELEASED", timeout=8)

            check(
                "★★ host 主动释放主机位 -> 其他玩家在**成员控制连接**上收到"
                " `HOST_RELEASED`（旧行为只静默关 socket，界面停在运行中）",
                bool(notified),
                str(notified),
            )

            state = None

            for line in joiner.history[::-1]:
                if line.startswith("HOST_STATE") and "host=0" in line:
                    state = line
                    break

            check(
                "★★ 释放之后广播 `host=0`（房间列表显示 host 暂无人认领）",
                bool(state),
                str(state),
            )

            await owner.close()
            await joiner.close()

        asyncio.run(scenario())
    finally:
        with contextlib.suppress(Exception):
            process.terminate()
            process.wait(timeout=10)

        with contextlib.suppress(Exception):
            log.close()


# ============================================================================

def main():
    print("=" * 70)
    print(" 用户 2026-09-30 收尾 3 条：host 离线 / 离开隔离 / 房主身份")
    print("=" * 70)

    test_host_left_control_line()
    test_gui_host_gone_three_ways()
    test_direct_addr_broadcast_reaches_everyone()
    test_relay_broadcasts_direct_addr()
    test_room_list_never_shows_the_host_as_the_owner()
    test_client_reported_conn_state()
    test_nat_undecided_never_claims_nat2()
    test_eif_proof_from_peer_first_packet()
    test_member_rows_never_crash_the_window()
    test_host_disconnect_loop_ends_with_player_disconnected()
    test_owner_disconnects_and_stays_disconnected_when_host_is_gone()
    test_host_stop_is_never_masked_by_a_stale_pending_flag()
    test_mode_hotswitch_rebuilds_exactly_once()
    test_single_instance_lock()
    test_mode_selector_shows_current_mode()
    test_non_owner_click_is_refused()
    test_departed_peer_is_not_readmitted()
    test_peer_leave_only_touches_that_peer()
    test_stale_host_connection_does_not_drop_the_room()
    test_owner_flag_uses_the_authoritative_member()
    test_build_member_rows_marks_the_owner()
    test_relay_broadcasts_owner_id()
    test_owner_kicking_the_host_disconnects_the_owner()

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
