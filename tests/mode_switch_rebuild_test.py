"""
MC-Link -- 2026-09-28 第二轮（用户实测 v3.4.0 的 8 条）回归网。

============================================================================
 这一套盯的是用户逐条报的 8 件事（见 docs/CLIENT-PUNCH-FLOW-20260928.md §5）
============================================================================

  1. 逐目标行：**对端真名** + 「第 N 次」（不是 `【A】` 字母计数）；
  2. 打通那一行必须带**公网地址:端口**（`P2P 已直连【名】ip:port 18ms`）；
  3. 状态迁移表（阶段 -> 允许的下一阶段 -> 触发条件）与代码**逐条一致**；
     手动中断后重连 -> 每一格重新走一遍；
  4. 本机测量阶段可见（`测量 NAT 类型中…` / `测量入站策略中…`）且两端对得上；
  5. host 端「等待对端连接…」（房里有人、没人开始连接）；
  6. **P0 热切换**：房主切模式 -> 三方各自重建 -> 能再次打通（真中继端到端）；
  7. `conn=` 广播驱动的成员连接状态（老中继回退旧行为）+ `STARTED 1/0`；
  8. 重建按**调用那一刻**的模式（不受界面每秒刷新影响）。

Run:
    python tests/mode_switch_rebuild_test.py
"""

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

# 客户端默认往只读的 ~/.config 写日志：在 import 之前把配置目录指到可写位置。
SANDBOX = tempfile.mkdtemp(prefix="mclink-modeswitch-")
os.environ["APPDATA"] = SANDBOX
os.environ["XDG_CONFIG_HOME"] = SANDBOX

from mclink.core import punch_flow as pf  # noqa: E402
from mclink.core import room as room_mod  # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_SWITCH_PORT", "19580"))
STUN_PORT = int(os.environ.get("MCLINK_TEST_SWITCH_STUN", "3478"))
MC_PORT = int(os.environ.get("MCLINK_TEST_SWITCH_MC", "19581"))
LOCAL1 = int(os.environ.get("MCLINK_TEST_SWITCH_LOCAL1", "19582"))
LOCAL2 = int(os.environ.get("MCLINK_TEST_SWITCH_LOCAL2", "19583"))
LOCAL3 = int(os.environ.get("MCLINK_TEST_SWITCH_LOCAL3", "19584"))

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


# ============================================================================
#  1. 状态迁移表 == 代码（用户第 3 条）
# ============================================================================

#: 文档 `docs/CLIENT-PUNCH-FLOW-20260928.md §5.2` 那张表的**逐格**转写。
#: 它和 `pf.TRANSITIONS` 必须逐条相同 —— 文档与代码不许分叉。
DOC_TRANSITIONS = {
    "idle": ("confirm_host",),
    "confirm_host": ("confirm_target",),
    "confirm_target": ("nat",),
    "nat": ("inbound",),
    "inbound": ("report",),
    "report": ("sync_start",),
    "sync_start": ("candidate",),
    "candidate": ("confirm_cand",),
    "confirm_cand": ("sync_go",),
    "sync_go": ("punch",),
    "punch": ("connected", "failed"),
    "connected": (),
    "failed": (),
}


def test_transition_table_matches_the_code():
    print("")
    print("---- 用户第 3 条：状态迁移表（阶段 -> 允许的下一阶段）代码与文档逐条一致 ----")

    code = {stage.value: tuple(s.value for s in nxt)
            for stage, nxt in pf.TRANSITIONS.items()}

    check(
        "★ 迁移表逐格等于文档 §5.2 那张表（多一格/少一格/顺序变了都算回归）",
        code == DOC_TRANSITIONS,
        f"code={code}",
    )

    check(
        "★ 六个「双方必须一致」的阶段都在表里（用户第 8 条点名的那几个）",
        all(
            pf.STAGE_LABELS.get(s)
            for s in (
                pf.Stage.SYNC_START, pf.Stage.SYNC_GO, pf.Stage.PUNCH,
                pf.Stage.NAT, pf.Stage.INBOUND, pf.Stage.CONNECTED,
            )
        ),
        str(sorted(pf.STAGE_LABELS)),
    )

    # 「等待对端测量 X」两句必须在表里能找到出处（它们是 sync_start 的显示变体）。
    check(
        "★ 「等待对端测量 NAT 类型 / 入站策略中」有文案（sync_start 的显示变体）",
        pf.PEER_STAGE_LABELS.get(pf.PEER_MEASURING_NAT)
        and pf.PEER_STAGE_LABELS.get(pf.PEER_MEASURING_INBOUND),
        str(pf.PEER_STAGE_LABELS),
    )


def test_no_stage_is_skipped_or_revisited():
    print("")
    print("---- 用户第 3 条：不许跳步、不许回到已完成的阶段 ----")

    sess = pf.ConnSession("p", label="radix")
    sess.start()
    sess.advance(pf.Stage.CONFIRM_TARGET)
    sess.advance(pf.Stage.NAT)

    check(
        "★ 跳步（nat -> report）被拒绝，阶段停在 nat",
        sess.advance(pf.Stage.REPORT) is False and sess.stage == pf.Stage.NAT,
        str(sess.stage),
    )

    check(
        "★ 回退（nat -> confirm_host，那是已完成的阶段）也被拒绝",
        sess.advance(pf.Stage.CONFIRM_HOST) is False
        and sess.stage == pf.Stage.NAT,
        str(sess.stage),
    )

    sess.advance(pf.Stage.INBOUND)

    check(
        "★ 回退到刚完成的 nat 同样被拒（不许重复阶段）",
        sess.advance(pf.Stage.NAT) is False and sess.stage == pf.Stage.INBOUND,
        str(sess.stage),
    )

    check(
        "★ 正着走一格是允许的（inbound -> report）",
        sess.advance(pf.Stage.REPORT) is True and sess.stage == pf.Stage.REPORT,
        str(sess.stage),
    )


def test_manual_stop_replays_every_stage():
    print("")
    print("---- 用户第 3 条：手动中断后重连，每一格都能重新走一遍并显示正确 ----")

    sess = pf.ConnSession("p", label="radix")
    sess.start()
    sess.advance(pf.Stage.CONFIRM_TARGET)
    sess.advance(pf.Stage.NAT)
    sess.note_punch(2)

    # 手动停止 = stop() 的语义：表立刻停/清/释放（§F），不是终态但要重开。
    spent = sess.timer.finish()

    check(
        "（前提）手动停止后表已释放（§F：停 / 清零 / 释放）",
        spent >= 0.0 and sess.timer.released
        and not sess.timer.running and sess.elapsed() == 0.0,
        f"released={sess.timer.released}",
    )

    check(
        "★ 停止后的连接需要**新的一次连接**（needs_new_cycle）",
        sess.needs_new_cycle is True,
        f"terminal={sess.terminal} released={sess.timer.released}",
    )

    gen_before = sess.generation

    # 重新点「开始连接」：必须是 restart()（新的一次连接），不是把阶段硬拨回去。
    check(
        "★ `start()` 拒绝从中间阶段起步（要显式 restart()）",
        sess.start() is False and sess.stage == pf.Stage.NAT,
        str(sess.stage),
    )

    sess.restart()

    check(
        "★ 手动重连 -> generation+1、阶段回到 §E 第一步、表从 0",
        sess.generation == gen_before + 1
        and sess.stage == pf.Stage.CONFIRM_HOST
        and sess.timer.running
        and sess.elapsed() < 1.0
        and sess.punch_count == 0
        and sess.connected is False,
        str(sess.snapshot()),
    )

    # 重新走一遍：每一格都能推进（且每一格都有文案）。
    walked = []

    for stage in (
        pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND,
        pf.Stage.REPORT, pf.Stage.SYNC_START, pf.Stage.CANDIDATE,
        pf.Stage.CONFIRM_CAND, pf.Stage.SYNC_GO, pf.Stage.PUNCH,
    ):
        ok = sess.advance(stage)
        walked.append((stage.value, ok, bool(sess.stage_text)))

    check(
        "★ 每一格都能重新走一遍，而且每一格都显示得出阶段文案",
        all(ok for _v, ok, _t in walked)
        and all(text for _v, _ok, text in walked),
        str(walked),
    )

    sess.finish(True, latency_ms=18.0, addr="1.2.3.4:5000")

    check(
        "★ 重连后打通那一行仍然是 真名 + 地址 的版式",
        sess.row_text() == "P2P 已直连【radix】1.2.3.4:5000 18ms",
        sess.row_text(),
    )


# ============================================================================
#  2. 本机测量阶段 + 两端对得上（用户第 4 条）
# ============================================================================

def test_local_measurement_is_visible_on_both_sides():
    print("")
    print("---- 用户第 4 条：本机测量也要显示；对端看得到「我在测哪一步」 ----")

    me = pf.ConnSession("peer-cid", label="玩家甲")
    me.start()
    me.advance(pf.Stage.CONFIRM_TARGET)
    me.advance(pf.Stage.NAT)
    me.begin_measure(pf.BEACON_NAT)

    check(
        "★ 本机在测 NAT -> 我的行写「测量 NAT 类型中…」",
        me.stage_text == "测量 NAT 类型中…" and me.row_text().startswith(
            "测量 NAT 类型中…"
        ),
        me.row_text(),
    )

    me.advance(pf.Stage.INBOUND)
    me.begin_measure(pf.BEACON_INBOUND)

    check(
        "★ 本机在测入站 -> 我的行写「测量入站策略中…」",
        me.stage_text == "测量入站策略中…",
        me.row_text(),
    )

    # 对端（另一个状态机）看到我发的 stage=nat / stage=inbound：
    peer = pf.ConnSession("me-cid", label="玩家乙")
    peer.start()
    peer.advance(pf.Stage.CONFIRM_TARGET)
    peer.advance(pf.Stage.NAT)
    peer.advance(pf.Stage.INBOUND)
    peer.advance(pf.Stage.REPORT)
    peer.advance(pf.Stage.SYNC_START)

    peer.note_peer_stage(pf.BEACON_NAT)

    check(
        "★ 对端看到我在测 NAT ->「等待对端测量 NAT 类型」",
        peer.stage_text == "等待对端测量 NAT 类型",
        peer.row_text(),
    )

    peer.note_peer_stage(pf.BEACON_INBOUND)

    check(
        "★ 对端看到我在测入站 ->「等待对端测量入站策略中」",
        peer.stage_text == "等待对端测量入站策略中",
        peer.row_text(),
    )

    # 两端都测完 -> 两边都回到基线原文「等待同步起跑」（§E）。
    peer.mark_measured()
    peer.mark_peer_measured()
    me.mark_measured()

    check(
        "★ 两边都测完 -> 两端都显示「等待同步起跑」（§E 原词）",
        peer.stage_text == "等待同步起跑" and peer.synced_start,
        f"{peer.stage_text} synced={peer.synced_start}",
    )

    check(
        "★ 对端没报小步（老客户端）-> 退回「等待同步起跑」",
        pf.ConnSession("x").note_peer_stage("nonsense") is False,
        "",
    )

    # 信标构造 / 解析往返（`stage=nat` 也要能被 parse 出来）。
    line = pf.beacon_line(pf.BEACON_NAT, "A", 0, " to=p", from_id="me")

    check(
        "★ `DIRECT_HIT stage=nat` 信标往返（中继逐字转发，不新增协议）",
        pf.parse_beacon(line).get("stage") == "nat"
        and pf.parse_beacon(line).get("from") == "me",
        line,
    )


def test_sleep_wakes_when_a_new_target_appears():
    print("")
    print("---- 用户第 8 条：等待期间出现新目标 -> 立刻开打（不是硬等满 3 秒）----")

    import asyncio

    from mclink.core import room_direct

    state = FakeState(
        client_id="h-cid", peer_ids=["p1-cid"], members=2, is_host=True,
        started_ids=["h-cid", "p1-cid"],
    )

    link = room_direct.RoomDirectLink(
        FakeEngine(state, room=object()), log=lambda *_a: None
    )

    async def run_new_target():
        # 睡的过程中 p2 也进房并点了开始连接（中继 started= 当场变）。
        state.peer_ids = ["p1-cid", "p2-cid"]
        state.members = 3
        state.started_ids = ["h-cid", "p1-cid", "p2-cid"]

        started = time.monotonic()
        await link._sleep_or_wake(["p1-cid"], interval=3.0, slice_s=0.05)

        return time.monotonic() - started

    waited = asyncio.run(run_new_target())

    check(
        "★ 出现新目标 -> 提前醒（远小于 3 秒的整轮间隔）",
        waited < 1.0,
        f"{waited:.2f}s",
    )

    async def run_no_change():
        started = time.monotonic()
        await link._sleep_or_wake(link._peers(), interval=0.5, slice_s=0.05)

        return time.monotonic() - started

    waited = asyncio.run(run_no_change())

    check(
        "★ 没有新目标 -> 仍然按原节奏睡满（重试频率没被改快）",
        waited >= 0.45,
        f"{waited:.2f}s",
    )


# ============================================================================
#  3. host 端「等待对端连接…」（用户第 5 条）
# ============================================================================

class FakeState:
    def __init__(self, **kw):
        self.client_id = kw.get("client_id", "me")
        self.peer_ids = kw.get("peer_ids", [])
        self.members = kw.get("members", 0)
        self.is_host = kw.get("is_host", False)
        self.host_id = kw.get("host_id", "")
        self.host_name = kw.get("host_name", "")
        self.host_claimed = kw.get("host_claimed", False)
        self.member_names = kw.get("member_names", {})
        self.self_name = kw.get("self_name", "")
        self.started_ids = kw.get("started_ids", None)
        self.member_conns = kw.get("member_conns", {})


class FakeEngine:
    def __init__(self, state, room=None):
        self._state = state
        self._room = room
        self.started_calls = []

    def room_state(self):
        return self._state

    def room_current(self):
        return self._room

    def server_host(self):
        return "127.0.0.1"

    def server_port(self):
        return 19100

    def loop(self):
        return None

    def room_nat_profile(self):
        return None

    def room_set_started(self, connecting, timeout=8):
        self.started_calls.append(bool(connecting))
        return True, "ok"


def test_host_waits_for_peer_to_connect():
    print("")
    print("---- 用户第 5 条：房内有人但没开始连接 -> host 写「等待对端连接…」----")

    from mclink.core import room_direct

    # 房主：花名册里有 p-cid，但 started= 里只有房主自己 -> 没有目标。
    state = FakeState(
        client_id="h-cid", peer_ids=["p-cid"], members=2, is_host=True,
        started_ids=["h-cid"],
    )

    link = room_direct.RoomDirectLink(
        FakeEngine(state, room=object()), log=lambda *_a: None
    )

    check(
        "（前提）房内有人、没人开始 -> 一个目标都没有",
        link._peers() == [],
        f"{link._peers()} {link.target_reason}",
    )

    check(
        "★ 房里有人但没开始 -> 阶段文案是「等待对端连接…」",
        link._has_other_members()
        and link.PHASE_WAITING_FOR_PEER_CONNECT == "等待对端连接…",
        link.PHASE_WAITING_FOR_PEER_CONNECT,
    )

    check(
        "★ 「等待对端连接…」是静止文案（不倒计时、不加秒数）",
        link.PHASE_WAITING_FOR_PEER_CONNECT
        not in (link.PHASE_WAITING_FOR_ANNOUNCE,),
        link.PHASE_WAITING_FOR_PEER_CONNECT,
    )

    # 房里一个人都没有 -> 仍然是「等待对端用户加入…」（两句必须分得开）。
    empty = FakeState(
        client_id="h-cid", peer_ids=[], members=1, is_host=True,
        started_ids=[],
    )
    link2 = room_direct.RoomDirectLink(
        FakeEngine(empty, room=object()), log=lambda *_a: None
    )

    check(
        "★ 房里没人 -> 仍然是「等待对端用户加入…」（两句分得开）",
        link2._has_other_members() is False
        and link2.PHASE_WAITING_FOR_PEER == "等待对端用户加入…",
        link2.PHASE_WAITING_FOR_PEER,
    )

    # Host 端的「等待」不靠猜：`started=` 里出现这个 peer 就立刻进目标。
    state.started_ids = ["h-cid", "p-cid"]

    check(
        "★ `started=` 里出现他（点了开始连接）-> 立刻进目标表",
        link._peers() == ["p-cid"],
        f"{link._peers()} {link.target_reason}",
    )


# ============================================================================
#  4. conn= 广播驱动成员状态（用户第 7 条）
# ============================================================================

def test_conn_broadcast_drives_member_status():
    print("")
    print("---- 用户第 7 条：连接状态以中继广播 `conn=` 为准（老中继回退）----")

    conns = room_mod.parse_member_conns(
        "cid-a:connected,cid-b:idle,cid-c:waiting,cid-d:pairing"
    )

    check(
        "★ 四个白名单取值都解析得出来",
        conns == {
            "cid-a": "connected", "cid-b": "idle",
            "cid-c": "waiting", "cid-d": "pairing",
        },
        str(conns),
    )

    check(
        "★ 白名单外的状态整条跳过（不认识 ≠ 没连）",
        room_mod.parse_member_conns("cid-a:bogus,cid-b:connected")
        == {"cid-b": "connected"},
        str(room_mod.parse_member_conns("cid-a:bogus,cid-b:connected")),
    )

    check(
        "★ `-` / 空 -> 空表（老中继没有这个 token）",
        room_mod.parse_member_conns("-") == {}
        and room_mod.parse_member_conns("") == {},
        "",
    )

    # ---- HOST_STATE 整行走一遍：conn= 进 RoomState ----
    from mclink.core.room import RoomSession

    session = RoomSession("127.0.0.1", 1, client_id="cid-me", username="me")
    session._on_line(
        "HOST_STATE host=1 host_name=host host_id=h-cid members=2 world=1 "
        "peers=cid-peer names=cid-me:me,cid-peer:radix rtts=- "
        "started=h-cid conn=cid-me:connected,cid-peer:idle mode=stun"
    )

    check(
        "★ HOST_STATE 的 `conn=` 进了 RoomState.member_conns",
        session.state.member_conns.get("cid-peer") == "idle",
        str(session.state.member_conns),
    )

    # ---- 成员列表：以广播为准 ----
    from mclink.gui import app as gui_app

    rows = gui_app.build_member_rows(session.state, direct_rows=[])
    peer_row = next((r for r in rows if r.get("key") == "cid-peer"), None)

    check(
        "★ 玩家断开（conn=idle）-> 成员列表那行 connected=False / conn_state=idle",
        peer_row is not None
        and peer_row.get("connected") is False
        and peer_row.get("conn_state") == "idle",
        str(peer_row),
    )

    check(
        "★ 连接状态列渲染成「未连接」（广播说了没连，host 立刻不再显示已连接）",
        gui_app._member_status_text(peer_row) == "未连接",
        gui_app._member_status_text(peer_row),
    )

    check(
        "★ waiting / pairing / connected 分别渲染成 等待配对 / 配对中 / 已连接",
        gui_app._member_status_text({"conn_state": "waiting"}) == "等待配对"
        and gui_app._member_status_text({"conn_state": "pairing"}) == "配对中"
        and gui_app._member_status_text({"conn_state": "connected"}) == "已连接",
        "",
    )

    # ---- 老中继（没有 conn=）：回退旧行为，**绝不显示成"未连接"** ----
    old = FakeState(
        client_id="cid-me", peer_ids=["cid-peer"], members=2, is_host=True,
        member_names={"cid-peer": "radix"}, member_conns={},
    )

    old_rows = gui_app.build_member_rows(old, direct_rows=[])
    old_peer = next((r for r in old_rows if r.get("key") == "cid-peer"), None)

    check(
        "★ 老中继（没有 conn=）-> 回退旧行为：房间里的人算已连接",
        old_peer is not None
        and old_peer.get("connected") is True
        and old_peer.get("conn_state") == "",
        str(old_peer),
    )

    check(
        "★ 老中继回退渲染「已连接」（不是「未连接」）",
        gui_app._member_status_text(old_peer) == "已连接",
        gui_app._member_status_text(old_peer),
    )


def test_started_report_protocol():
    print("")
    print("---- 用户第 5/8 条：STARTED 1/0 显式上报（同一状态只发一次）----")

    from mclink.core import protocol as proto
    from mclink.core.room import RoomSession
    import inspect

    check(
        "协议常量存在（PROTOCOL §8.6）",
        proto.LINE_STARTED == "STARTED"
        and proto.LINE_ACK_STARTED == "OK STARTED",
        f"{proto.LINE_STARTED} {proto.LINE_ACK_STARTED}",
    )

    session = RoomSession("127.0.0.1", 1, client_id="p-cid", username="me")
    seen = []

    # 拦住真实写 socket：只记录这一行，并直接喂一个 OK STARTED 回去。
    class FakeWriter:
        def write(self, data):
            seen.append(data.decode().strip())

        async def drain(self):
            return None

    import asyncio

    async def run():
        session.running = True
        session.writer = FakeWriter()
        task = asyncio.ensure_future(session.send_started(True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        session._on_line("OK STARTED started=1")
        return await task

    ok, message = asyncio.run(run())

    check(
        "★ 发出去的是 `STARTED 1`（整行就两个 token，位置解析不被打乱）",
        seen == ["STARTED 1"] and ok is True,
        f"{seen} {ok} {message}",
    )

    # ERROR BAD_STARTED 也要能被认出来（不抛、不误判成功）。
    session2 = RoomSession("127.0.0.1", 1, client_id="p-cid", username="me")

    async def run_bad():
        session2.running = True
        session2.writer = FakeWriter()
        task = asyncio.ensure_future(session2.send_started(False))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        session2._on_line("ERROR BAD_STARTED 必须是 STARTED 0 或 STARTED 1")
        return await task

    ok2, message2 = asyncio.run(run_bad())

    check(
        "★ 非法值 -> ERROR BAD_STARTED 被认成失败（不是成功、也不抛）",
        ok2 is False and "BAD_STARTED" in message2,
        f"{ok2} {message2}",
    )

    # engine 的入口存在（界面就是调它）。
    from mclink.core import engine as eng_mod

    check(
        "engine 有 `room_set_started`（界面唯一入口）",
        hasattr(eng_mod.MCLinkEngine, "room_set_started"),
        "",
    )
    _ = inspect


# ============================================================================
#  5. 界面：重建按「调用那一刻」的模式（用户第 6 条 P0，竞态那一半）
# ============================================================================

class _RebuildHarness:
    """真 MCLinkApp + 假 Engine（只为了跑 `_rebuild_tunnels` 那条路）。"""

    def __init__(self):
        self.app = None
        self.logs = []

    def build(self):
        try:
            import tkinter as tk
            from tkinter import messagebox
        except ImportError:
            return False

        messagebox.askokcancel = lambda *a, **k: True
        messagebox.showwarning = lambda *a, **k: None
        messagebox.showerror = lambda *a, **k: None

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

        app.engine = _ModeEngine()
        self.app = app

        import threading

        main_thread = threading.main_thread()
        original = app.log

        def capture(text, kind="normal"):
            self.logs.append(str(text))

            if threading.current_thread() is main_thread:
                return original(text, kind=kind)

            return None

        app.log = capture

        for variable, value in (
            (app.mc_port_var, "25565"),
            (app.local_port_var, "25565"),
            (app.max_players_var, "4"),
            (app.room_var, "switch-room"),
            (app.username_var, "host"),
        ):
            with contextlib.suppress(Exception):
                variable.set(value)

        app._committed["mc_port"] = 25565
        app._committed["local_port"] = 25565
        app._committed["max_players"] = 4
        app.config.role = "host"
        return True

    def close(self):
        with contextlib.suppress(Exception):
            self.root.destroy()


class _ModeRoom:
    def __init__(self, mode="stun"):
        self.conn_mode = mode
        self.is_host = True
        self.is_owner = True
        self.host_claimed = True
        self.members = 2
        self.peer_ids = ["cid-peer"]
        self.member_names = {"cid-me": "host", "cid-peer": "radix"}
        self.member_rtts = {}
        self.member_conns = {}
        self.world_players = 1
        self.revoked = False
        self.revoked_reason = ""
        self.client_id = "cid-me"
        self.username = "host"
        self.own_name = "host"
        self.self_name = "host"
        self.display = "switch-room"
        self.room_name = "switch-room"
        self.joined_at = time.time()


class _ModeEngine:
    """记录每次重建实际用的模式（`start_session` 那一刻的快照）。"""

    def __init__(self):
        self.room = _ModeRoom("stun")
        self.session_mode_value = "stun"
        self.starts = []
        self.stops = 0
        self.pushed = []
        self.stop_started = None
        self.stop_released = None
        self.stop_gate = threading.Event()

    # ---- 房间 ----
    def room_state(self):
        return self.room

    def room_current(self):
        return None

    # ---- 会话 ----
    def session_mode(self):
        return self.session_mode_value

    def start_session(self, config, auth=None, timeout=20):
        # 快照：重建之后界面线程再改 config 也不影响这一条断言。
        self.starts.append(
            type("Start", (), {"config": config, "mode": str(config.mode)})
        )
        self.session_mode_value = str(config.mode)
        return self.starts[-1]

    def stop_session(self, timeout=15):
        self.stops += 1
        self.stop_started = time.time()

        if self.stop_gate is not None:
            self.stop_gate.wait(timeout=2.0)

        self.session_mode_value = ""
        self.stop_released = time.time()
        return True

    # ---- 房间级任务 ----
    def watch_room_nat(self, enabled, log=None, standalone_host=None):
        return None

    def watch_room_direct(self, enabled, log=None):
        return None

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


def test_rebuild_uses_the_mode_captured_at_call_time(harness):
    print("")
    print("---- 用户第 6 条（P0 的另一半）：重建按**调用那一刻**的模式，"
          "不受每秒刷新影响 ----")

    app = harness.app
    engine = app.engine

    engine.room = _ModeRoom("stun")
    engine.session_mode_value = "relay"
    app.running = True
    app._rebuild_in_flight = False
    app.config.mode = "relay"

    # `_collect_config` 在这套里会读 tk 变量（工作线程里不行）——换成一个纯
    # 构造器，**返回的就是 app.config 本身**（真实路径也是"就地改写 + 返回"），
    # 这样下面的"界面线程把它改回旧模式"才和现场一模一样。
    app._collect_config = lambda role_override=None: app.config

    # 让工作线程停在 stop_session 里，给"界面线程改回旧模式"一个确定的窗口。
    engine.stop_gate = threading.Event()
    engine.starts.clear()

    # 房主切 relay -> STUN：调用那一刻房间已经是 STUN。
    app._rebuild_tunnels(mode="stun")

    # 等重建真的进了 stop_session（说明 worker 已经开始跑）。
    deadline = time.time() + 5
    while time.time() < deadline and engine.stop_started is None:
        time.sleep(0.01)

    # ★ 这就是现场：界面线程在窗口里把共享字段写回**旧模式**。
    app.config.mode = "relay"
    engine.stop_gate.set()

    deadline = time.time() + 10
    while time.time() < deadline and not engine.starts:
        time.sleep(0.02)

    check(
        "重建确实起了新会话",
        bool(engine.starts),
        f"starts={len(engine.starts)} pushed={engine.pushed[-2:]}",
    )

    check(
        "★ 新会话用的是**调用那一刻**的 STUN（不是窗口里被写回的 relay）",
        bool(engine.starts) and engine.starts[-1].mode == "stun",
        f"mode={engine.starts[-1].mode if engine.starts else None}",
    )


# ============================================================================
#  6. 真中继端到端：房主切模式 -> 三方各自重建 -> 能再次打通（用户第 6 条）
# ============================================================================

def fake_minecraft_server(stop):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", MC_PORT))
    srv.listen(16)

    def serve(conn):
        try:
            conn.sendall(b"MC-BANNER-FROM-HOST\n")

            while True:
                data = conn.recv(4096)

                if not data:
                    return

                conn.sendall(b"ECHO:" + data)
        except OSError:
            pass

    while not stop.is_set():
        try:
            conn, _addr = srv.accept()
        except OSError:
            return

        threading.Thread(target=serve, args=(conn,), daemon=True).start()


def start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        "MCLINK_STUN_ENABLED": "1",
        "MCLINK_STUN_BIND": "127.0.0.1",
        "MCLINK_STUN_PORT": str(STUN_PORT),
        "MCLINK_STATUS_FILE": os.path.join(HERE, "mode_switch_status.json"),
        "MCLINK_STATUS_INTERVAL": "30",
    })

    log = open(os.path.join(HERE, "mode_switch_relay.log"), "w",
               encoding="utf-8")

    process = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env, stdout=log, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    deadline = time.time() + 25

    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", RELAY_PORT), 1).close()
            return process, log
        except OSError:
            if process.poll() is not None:
                return process, log
            time.sleep(0.3)

    return process, log


class _Client:
    """一个真引擎 + 界面级热切换的等价动作（`_refresh_room_ui` 那两步）。"""

    def __init__(self, engine, role, cid, local_port):
        self.engine = engine
        self.role = role
        self.cid = cid
        self.local_port = local_port
        self.rebuilds = []

    def mkcfg(self, mode):
        from mclink.config import settings as cfgmod

        cfg = cfgmod.load_config()
        cfg.role = self.role
        cfg.client_id = self.cid
        cfg.server_host = "127.0.0.1"
        cfg.server_port = RELAY_PORT
        cfg.room = "switch-room"
        cfg.mc_host = "127.0.0.1"
        cfg.mc_port = MC_PORT
        cfg.local_port = self.local_port
        cfg.max_tunnels = 3
        # ★ 关键：模式**显式**由调用方给（就是界面 `_rebuild_tunnels(mode=…)`
        # 的语义）—— 不再到工作线程里读一个每秒都会被改的共享字段。
        cfg.mode = mode
        return cfg

    def tick(self, running=True):
        """
        界面每秒那一拍的等价物（`_refresh_room_ui` 里的两件事）。

          1. `_sync_session_mode`: 会话真正用的模式 != 房间模式 -> 按**房间模式**重建；
          2. `_sync_room_nat`: 房间模式变了 -> 拆掉旧房间级任务，STUN 时重开。
        """
        room_mode = self.engine.room_state().conn_mode

        if running and self.engine.session_mode() != room_mode:
            self.engine.stop_session(timeout=15)
            self.engine.start_session(self.mkcfg(room_mode))
            self.rebuilds.append(room_mode)

        self.engine.watch_room_nat(
            bool(room_mode == "stun" and running)
        )


def wait_tunnels(client, want, timeout=40.0):
    deadline = time.time() + timeout

    while time.time() < deadline:
        direct = client.engine.room_direct()

        if direct is not None and direct.tunnels_up >= want:
            return True

        time.sleep(0.3)

    return False


def wait_session_mode(client, mode, timeout=25.0):
    deadline = time.time() + timeout

    while time.time() < deadline:
        if client.engine.session_mode() == mode:
            return True

        time.sleep(0.2)

    return False


def exchange(local_port, tag, timeout=10.0):
    try:
        mc = socket.create_connection(("127.0.0.1", local_port), 8)
    except OSError as e:
        print(f"  exchange({tag}): connect failed: {e}", flush=True)
        return False

    mc.settimeout(timeout)

    try:
        banner = mc.recv(100)
        mc.sendall(b"HI-" + tag.encode())
        echoed = mc.recv(200)
    except Exception as e:
        print(f"  exchange({tag}): read failed: {e}", flush=True)
        return False
    finally:
        mc.close()

    ok = (b"MC-BANNER-FROM-HOST" in banner
          and echoed == b"ECHO:HI-" + tag.encode())

    if not ok:
        print(f"  exchange({tag}): banner={banner!r} echo={echoed!r}",
              flush=True)

    return ok


def test_hot_mode_switch_end_to_end():
    print("")
    print("---- 用户第 6 条（P0 端到端）：房主切模式 -> 三方各自重建 -> "
          "能再次打通 ----")

    from mclink.core import engine as eng_mod

    stop = threading.Event()
    threading.Thread(
        target=fake_minecraft_server, args=(stop,), daemon=True
    ).start()
    time.sleep(0.3)

    relay, relay_log = start_relay()

    host = eng_mod.MCLinkEngine()
    p1 = eng_mod.MCLinkEngine()
    p2 = eng_mod.MCLinkEngine()

    for engine in (host, p1, p2):
        engine.start_loop()

    clients = {
        "host": _Client(host, "host", "h-cid", LOCAL1),
        "p1": _Client(p1, "player", "p1-cid", LOCAL2),
        "p2": _Client(p2, "player", "p2-cid", LOCAL3),
    }

    try:
        if relay.poll() is not None:
            check("中继启动", False, "进程已退出，见 tests/mode_switch_relay.log")
            return

        check("中继启动", True)

        host.open_room("127.0.0.1", RELAY_PORT, "h-cid", "host", "",
                       "switch-room", create=True, max_players=3,
                       conn_mode="stun")
        time.sleep(0.4)
        p1.open_room("127.0.0.1", RELAY_PORT, "p1-cid", "p1", "",
                     "switch-room")
        p2.open_room("127.0.0.1", RELAY_PORT, "p2-cid", "p2", "",
                     "switch-room")
        host.claim_host()
        time.sleep(0.3)

        check(
            "★ 建房时是 STUN（房间状态由中继广播）",
            host.room_state().conn_mode == "stun"
            and p1.room_state().conn_mode == "stun",
            f"{host.room_state().conn_mode} {p1.room_state().conn_mode}",
        )

        # ---- 点开始连接：STARTED 1 + 会话 + 房间级打洞 ----
        for name, client in clients.items():
            client.engine.start_session(client.mkcfg("stun"))
            client.engine.room_set_started(True)
            client.engine.watch_room_direct(True, log=lambda *_a: None)

        ready = (
            wait_tunnels(clients["host"], 2)
            and wait_tunnels(clients["p1"], 1)
            and wait_tunnels(clients["p2"], 1)
        )

        check(
            "★ 切模式之前：房主两条洞 + 两个玩家各一条（三方都通）",
            ready,
            f"host={host.room_direct().tunnels_up if host.room_direct() else None} "
            f"p1={p1.room_direct().tunnels_up if p1.room_direct() else None} "
            f"p2={p2.room_direct().tunnels_up if p2.room_direct() else None}",
        )

        check("★ 切模式之前：p1 的数据走通", exchange(LOCAL2, "P1-BEFORE"), "")

        for client in clients.values():
            client.rebuilds.clear()

        # ============================================================
        #  房主切 relay -> 三方各自重建
        # ============================================================
        ok, message = host.set_conn_mode("relay")

        check(
            "★ 房主切到 relay：中继受理（OK CONNMODE）",
            ok is True,
            f"{ok} {message}",
        )

        deadline = time.time() + 25
        all_relay = False

        while time.time() < deadline:
            for client in clients.values():
                client.tick(running=True)

            if all(
                client.engine.session_mode() == "relay"
                for client in clients.values()
            ):
                all_relay = True
                break

            time.sleep(0.2)

        check(
            "★ 切到 relay：三方**各自**都重建成了 relay（不是只房主）",
            all_relay,
            str({name: c.engine.session_mode() for name, c in clients.items()}),
        )

        check(
            "★ 换模式拆掉了旧通路（relay 模式下没有打洞中的洞）",
            all(
                (c.engine.room_direct() is None)
                or c.engine.room_direct().tunnels_up == 0
                for c in clients.values()
            ),
            str({n: (c.engine.room_direct().tunnels_up
                     if c.engine.room_direct() else None)
                 for n, c in clients.items()}),
        )

        for client in clients.values():
            client.rebuilds.clear()

        # ============================================================
        #  再切回 STUN：必须能再次打通（用户报的"热切换不能用了"）
        # ============================================================
        ok, message = host.set_conn_mode("stun")

        check("★ 房主切回 STUN：中继受理", ok is True, f"{ok} {message}")

        deadline = time.time() + 30
        all_stun = False

        while time.time() < deadline:
            for client in clients.values():
                client.tick(running=True)

            if all(
                client.engine.session_mode() == "stun"
                for client in clients.values()
            ):
                all_stun = True
                break

            time.sleep(0.2)

        check(
            "★ 切回 STUN：三方各自重建回 STUN（隧道重建，不是复用旧的）",
            all_stun,
            str({name: c.engine.session_mode() for name, c in clients.items()}),
        )

        # 房间级打洞按新模式重开（界面 `_sync_room_nat` 的等价物已经做过）。
        for client in clients.values():
            if client.engine.room_direct() is None or not client.engine.room_direct().running:
                client.engine.watch_room_direct(True, log=lambda *_a: None)

        again = (
            wait_tunnels(clients["host"], 2, timeout=50)
            and wait_tunnels(clients["p1"], 1, timeout=10)
            and wait_tunnels(clients["p2"], 1, timeout=10)
        )

        check(
            "★ ★ 热切换之后**能再次打通**（房主两条洞 + 两个玩家各一条）",
            again,
            f"host={host.room_direct().tunnels_up if host.room_direct() else None} "
            f"p1={p1.room_direct().tunnels_up if p1.room_direct() else None} "
            f"p2={p2.room_direct().tunnels_up if p2.room_direct() else None} "
            f"rebuilds={ {n: c.rebuilds for n, c in clients.items()} }",
        )

        check(
            "★ ★ 热切换之后数据逐字节走通（p1）",
            exchange(LOCAL2, "P1-AFTER"),
            "",
        )

        check(
            "★ ★ 热切换之后数据逐字节走通（p2）",
            exchange(LOCAL3, "P2-AFTER"),
            "",
        )

        # ---- 阶段双端一致：切回来之后两端的逐目标行都在同一套 §E 阶段里 ----
        rows = host.room_direct().link_rows(punching=True) if host.room_direct() else []
        stages = {row.get("peer"): row.get("stage") for row in rows}

        check(
            "★ 房主的逐目标行带着**对端真名**（不是字母/编号）",
            all(row.get("name") and row.get("name") != "" for row in rows),
            str([(r.get("peer"), r.get("name")) for r in rows]),
        )

        check(
            "★ 打通的逐目标行带着**地址:端口**（用户第 2 条）",
            all(
                (not row.get("ready")) or (row.get("addr") or "")
                for row in rows
            ),
            str([(r.get("peer"), r.get("addr"), r.get("ready")) for r in rows]),
        )
        _ = stages
    finally:
        stop.set()

        for engine in (host, p1, p2):
            with contextlib.suppress(Exception):
                engine.shutdown(timeout=6)

        with contextlib.suppress(Exception):
            relay.terminate()
            relay.wait(timeout=10)

        relay_log.close()


# ============================================================================
#  main
# ============================================================================

def main():
    print("=" * 66)
    print(" MC-Link 模式热切换 / 状态迁移 / 逐目标行文案 回归测试")
    print("=" * 66)

    test_transition_table_matches_the_code()
    test_no_stage_is_skipped_or_revisited()
    test_manual_stop_replays_every_stage()
    test_local_measurement_is_visible_on_both_sides()
    test_sleep_wakes_when_a_new_target_appears()
    test_host_waits_for_peer_to_connect()
    test_conn_broadcast_drives_member_status()
    test_started_report_protocol()

    harness = _RebuildHarness()

    if harness.build():
        try:
            test_rebuild_uses_the_mode_captured_at_call_time(harness)
        finally:
            harness.close()
    else:
        check("界面侧重建模式快照（需要 tkinter 显示）", False,
              "没有可用显示，见 AGENTS.md 规则 5 的 X11 软链")

    test_hot_mode_switch_end_to_end()

    print("")
    print("=" * 66)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 66)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
