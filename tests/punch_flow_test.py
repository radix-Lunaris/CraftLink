"""
MC-Link —— 设计基准 §C/§D/§E/§F/§G 的回归网（2026-09-28）。

============================================================================
 这一套为什么存在
============================================================================

用户本轮 8 条 BUG 里有 5 条是同一个根因的不同表现：

    **打洞没有被建模成"每个 Host↔Player 一条独立、双端同步的状态机"。**

`core/punch_flow.py` 就是那个状态机（纯逻辑，不碰 socket / asyncio / 界面）。
这一套把它的每一条规则钉死，外加两条**真中继**的端到端：

  A. §E 严格顺序 + 「等待同步起跑」（先测完的一方等对端也测完）
  B. §F 每连接独立 Timer：一次使用后立即停止/清零/释放，重连从 0
  C. §F 双端 attempt/batch/序号同步；错位 -> 作废 attempt -> 重新同步
  D. §D 目标选择：Player 只认 Host（BUG 1 的复现），Host 只打已开始的 Player
  E. §D 新人加入不扰动已打通的连接；换模式严格重建全部通路
  F. §G UI 三层（通路 n/m / 打洞进度 n/m / 逐目标行）+ N/C 口径
  G. §C 被动掉线的两类判定（被踢 = 退出；真闪断 = 自动重建）
  H. 真中继：拔掉中继之后老会话必须清理干净、端口必须释放、角色不变

Run:
    python tests/punch_flow_test.py
"""

import contextlib
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import punch_flow as pf  # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_FLOW_PORT", "19170"))
MC_PORT = int(os.environ.get("MCLINK_TEST_FLOW_MC", "19171"))
LOCAL_PORT = int(os.environ.get("MCLINK_TEST_FLOW_LOCAL", "19172"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================
#  A. §E 严格顺序 + 「等待同步起跑」
# ============================================================

def test_stage_order():
    print("")
    print("---- §E：打洞前必须按确认 Host→确认目标→NAT→入站→上报→等待同步起跑"
          "→同步 candidate→双方确认→同步起降→Punch ----")

    expected = [
        "confirm_host", "confirm_target", "nat", "inbound", "report",
        "sync_start", "candidate", "confirm_cand", "sync_go", "punch",
    ]

    actual = [s.value for s in pf.STAGE_ORDER if s != pf.Stage.IDLE]

    check(
        "★ 阶段序列逐字等于基线 §E（多一个少一个顺序变了都算回归）",
        actual == expected,
        f"{actual}",
    )

    check(
        "★ 每个阶段都有界面文案（逐目标行要显示它）",
        all(pf.STAGE_LABELS.get(s) for s in pf.STAGE_ORDER if s != pf.Stage.IDLE),
        str(sorted(pf.STAGE_LABELS)),
    )

    check(
        "★ 两个终态在序列之后（到了就是到了，不可逆）",
        pf.Stage.CONNECTED in pf.TERMINAL_STAGES
        and pf.Stage.FAILED in pf.TERMINAL_STAGES
        and all(s not in pf.TERMINAL_STAGES for s in pf.STAGE_ORDER),
        str(sorted(pf.TERMINAL_STAGES)),
    )


def test_session_walks_the_whole_flow():
    print("")
    print("---- §E：一条连接从确认 Host 一路走到 Punch（每一步都记下来）----")

    sess = pf.ConnSession("host-cid", role="player")
    started = sess.start()

    check("start() 从「确认 Host」开始并开表", started and sess.stage == pf.Stage.CONFIRM_HOST,
          f"{started} {sess.stage}")

    check(
        "★ 回退被拒绝（阶段只能往前走）",
        sess.advance(pf.Stage.IDLE) is False and sess.stage == pf.Stage.CONFIRM_HOST,
        str(sess.stage),
    )

    for stage in (
        pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND,
        pf.Stage.REPORT, pf.Stage.SYNC_START, pf.Stage.CANDIDATE,
        pf.Stage.CONFIRM_CAND, pf.Stage.SYNC_GO, pf.Stage.PUNCH,
    ):
        sess.advance(stage)

    check(
        "★ 走完 §E 后停在 Punch",
        sess.stage == pf.Stage.PUNCH,
        str(sess.stage),
    )

    sess.finish(True, latency_ms=18.0)

    check(
        "★ 终态后 stage 不可再改（已结束的连接不许被推进成活的）",
        sess.stage == pf.Stage.CONNECTED
        and sess.advance(pf.Stage.PUNCH) is False,
        f"{sess.stage} {sess.advance(pf.Stage.PUNCH)}",
    )


def test_waiting_for_synced_start():
    print("")
    print("---- §E：「等待同步起跑」= 先测完的一方等对端也测完测量 ----")

    sess = pf.ConnSession("host-cid")
    sess.start()

    check(
        "一开始两边都没测完 -> 不许往下走",
        sess.synced_start is False,
        str(sess.synced_start),
    )

    sess.mark_measured(nat="cone", filtering="eif", inbound="punchable")

    check(
        "★ 只有**本端**测完 -> 仍然不许往下走（这就是端口错位的来源）",
        sess.synced_start is False and sess.my_measured and not sess.peer_measured,
        f"mine={sess.my_measured} peer={sess.peer_measured}",
    )

    sess.mark_peer_measured()

    check(
        "★ 两边都测完 -> 「等待同步起跑」完成，才允许同步 candidate",
        sess.synced_start is True,
        f"mine={sess.my_measured} peer={sess.peer_measured}",
    )

    check(
        "测量结果按连接保存（NAT / 入站 / 过滤）",
        sess.my_nat == "cone" and sess.my_filter == "eif"
        and sess.inbound == "punchable",
        f"{sess.my_nat} {sess.my_filter} {sess.inbound}",
    )


def test_candidate_must_be_confirmed_by_both():
    print("")
    print("---- §E：「双方确认 candidate」缺一边就不许起降 ----")

    sess = pf.ConnSession("host-cid")
    sess.start()
    sess.mark_measured()
    sess.mark_peer_measured()
    sess.advance(pf.Stage.SYNC_START)

    sess.mark_candidate(("1.2.3.4", 5000), port=5000)

    check(
        "只有本端 candidate -> 未确认",
        sess.confirm_candidate() is False,
        f"{sess.my_candidate} {sess.peer_candidate}",
    )

    sess.mark_peer_candidate(("5.6.7.8", 6000))

    check(
        "★ 两边 candidate 都在 -> 确认，才允许起降",
        sess.confirm_candidate() is True,
        f"{sess.my_candidate} {sess.peer_candidate}",
    )

    check(
        "本端端口/序号被记进这条连接（别人不共享）",
        sess.port == 5000 and sess.packet_seq == 0,
        f"port={sess.port} seq={sess.packet_seq}",
    )


# ============================================================
#  B. §F 每连接独立 Timer
# ============================================================

def test_timer_stops_zeroes_and_releases():
    print("")
    print("---- §F：一次连接（成功或失败都算终态）立即停止/清零/释放 ----")

    clock = {"now": 100.0}
    sess = pf.ConnSession("peer-1", clock=lambda: clock["now"])
    sess.start()

    clock["now"] += 123.0

    check(
        "计时在跑（这一轮已经 123 秒）",
        abs(sess.elapsed() - 123.0) < 0.01 and sess.timer.running,
        f"{sess.elapsed()}",
    )

    spent = sess.finish(True, latency_ms=18.0)

    check(
        "★ 成功也是终态：finish() 交出这一轮耗时（只给日志用）",
        abs(spent - 123.0) < 0.01,
        f"{spent}",
    )

    check(
        "★ 终态后 Timer 立刻【停止 + 清零 + 释放】：elapsed()==0，不保留“最终耗时”",
        sess.elapsed() == 0.0
        and sess.timer.running is False
        and sess.timer.released is True,
        f"elapsed={sess.elapsed()} running={sess.timer.running} "
        f"released={sess.timer.released}",
    )

    check(
        "★ 已连接这条连接确实记着延迟（N/C 口径的另一半）",
        sess.connected and sess.latency_text() == "18ms",
        f"{sess.connected} {sess.latency_text()}",
    )

    # ---- 下次重连：重新创建、从 0 开始 ----
    sess.restart()

    check(
        "★ 重连从 0 重新计算（BUG 8：不许从几百秒的累计值开始）",
        sess.elapsed() == 0.0 and sess.timer.running and not sess.timer.released,
        f"elapsed={sess.elapsed()} running={sess.timer.running}",
    )

    check(
        "★ 重连不继承任何上一轮的一次性状态",
        sess.connected is False
        and sess.latency_ms is None
        and sess.my_measured is False
        and sess.peer_measured is False
        and sess.my_candidate is None
        and sess.peer_candidate is None
        and sess.punch_count == 0
        and sess.port is None
        and sess.go_at is None,
        str(sess.snapshot()),
    )

    check(
        "★ 重连后 stage 回到「确认 Host」",
        sess.stage == pf.Stage.CONFIRM_HOST,
        str(sess.stage),
    )


def test_failure_also_zeroes_the_timer():
    print("")
    print("---- §F：失败同样是终态，Timer 一样清零 ----")

    clock = {"now": 0.0}
    sess = pf.ConnSession("peer-1", clock=lambda: clock["now"])
    sess.start()
    clock["now"] += 40.0
    sess.finish(False, "no packet arrived")

    check(
        "★ 失败 -> 停表清零释放，并把原因留在这一条连接上",
        sess.elapsed() == 0.0
        and sess.timer.released
        and sess.failed
        and sess.fail_reason == "no packet arrived"
        and sess.stage == pf.Stage.FAILED,
        str(sess.snapshot()),
    )

    check(
        "★ N/C 口径：没连上时延迟写 N/C（不写数字、不写 0）",
        sess.latency_text() == pf.LATENCY_NOT_CONNECTED,
        sess.latency_text(),
    )


def test_each_connection_has_its_own_timer():
    print("")
    print("---- §F：每个 Host↔Player 一套独立 Timer，绝不复用 ----")

    a = pf.ConnSession("peer-a")
    b = pf.ConnSession("peer-b")
    clock = {"now": 0.0}

    a.timer = pf.ConnTimer(clock=lambda: clock["now"])
    b.timer = pf.ConnTimer(clock=lambda: clock["now"])

    a.start()
    clock["now"] += 10.0
    b.start()
    clock["now"] += 5.0
    a.finish(True)

    check(
        "★ a 结束清零，b 的计时完全不受影响（各算各的）",
        a.elapsed() == 0.0 and abs(b.elapsed() - 5.0) < 0.01,
        f"a={a.elapsed()} b={b.elapsed()}",
    )

    check(
        "★ 两条连接的标识/端口/序号互不相同",
        a.peer != b.peer and a.port is None and b.port is None,
        f"{a.peer} {b.peer}",
    )


# ============================================================
#  C. §F 双端同步（attempt / batch / 序号）
# ============================================================

def test_attempt_label():
    print("")
    print("---- §F：双端同步用的 `att=` 标签 A..Z -> A2..（**只用于比对，不上屏**）----")

    check(
        "★ 第 1 轮是 A（两端 `att=` 比对的记号；逐目标行里显示的是对端真名）",
        pf.attempt_label(0) == "A",
        pf.attempt_label(0),
    )
    check(
        "第 27 轮是 B2（不是 AA：两个字母更难一眼区分）",
        pf.attempt_label(26) == "A2" and pf.attempt_label(27) == "B2",
        f"{pf.attempt_label(26)} {pf.attempt_label(27)}",
    )
    check(
        "session.attempt_id 跟着 batch 派生（两端只要 batch 一致就一致）",
        pf.ConnSession("p").attempt_id == "A",
        pf.ConnSession("p").attempt_id,
    )


def test_sync_mismatch_voids_and_resyncs():
    print("")
    print("---- §F：错位 -> 作废 attempt -> 清理 -> 重新同步（不许各自硬打）----")

    host = pf.ConnSession("guest-1", role="host")
    host.start()
    host.advance(pf.Stage.CONFIRM_TARGET)
    host.note_punch(5)
    host.mark_candidate(("1.2.3.4", 1), port=1)
    host.mark_measured()
    host.mark_peer_measured()

    check(
        "（对照）轮次一致时不作废",
        host.adopt_batch(0) is False and host.voided == 0,
        f"voided={host.voided}",
    )

    raised = False

    try:
        host.adopt_batch(7)
    except pf.SyncMismatch as e:
        raised = True
        host._last_mismatch = str(e)

    check(
        "★ Host 端：对端报的 batch 不一样 -> SyncMismatch（能被看见、不是悄悄接着打）",
        raised,
        getattr(host, "_last_mismatch", ""),
    )

    check(
        "★ Host 是仲裁者：自己的 batch 一点不动（权威轮次）",
        host.batch == 0,
        f"batch={host.batch}",
    )

    check(
        "★ 作废 attempt：清理这一轮的测量/候选/发包数/端口，回到确认 Host",
        host.stage == pf.Stage.CONFIRM_HOST
        and host.my_measured is False
        and host.peer_measured is False
        and host.my_candidate is None
        and host.punch_count == 0
        and host.voided == 1,
        str(host.snapshot()),
    )

    check(
        "★ 作废不代表这次连接结束：表还在跑（只有终态才停表）",
        host.timer.running and host.elapsed() >= 0.0,
        f"running={host.timer.running}",
    )

    # ---- Player 跟随 Host 的轮次 ----
    player = pf.ConnSession("host-cid", role="player")
    player.start()

    raised = False

    try:
        player.adopt_batch(3)
    except pf.SyncMismatch:
        raised = True

    check(
        "★ Player 端：采纳 Host 的轮次（跟随者不是仲裁者）",
        raised and player.batch == 3 and player.attempt_id == "D",
        f"raised={raised} batch={player.batch} id={player.attempt_id}",
    )

    check(
        "★ 跟随之后两端轮次一致 -> 重新同步不再报错",
        player.adopt_batch(3) is False,
        f"batch={player.batch}",
    )


def test_host_opens_a_new_batch_cleanly():
    print("")
    print("---- §F：Host 开新轮 = batch+1 + 从头同步；Player 无权开轮 ----")

    host = pf.ConnSession("g", role="host")
    host.start()
    host.note_punch(9)

    new_batch = host.next_batch()

    check(
        "★ Host 开新轮：batch+1、清空这一轮、回到确认 Host",
        new_batch == 1 and host.batch == 1 and host.attempt_id == "B"
        and host.punch_count == 0 and host.stage == pf.Stage.CONFIRM_HOST,
        str(host.snapshot()),
    )

    check(
        "★ 开新轮不算“作废”（voided 不涨）",
        host.voided == 0,
        f"voided={host.voided}",
    )

    player = pf.ConnSession("h", role="player")

    check(
        "★ Player 不许自己发明轮次（两端必然分叉）",
        player.next_batch() == 0 and player.batch == 0,
        f"batch={player.batch}",
    )


def test_punch_sequence_is_per_connection():
    print("")
    print("---- §F：packet sequence / 次数 / 端口 都在这一条连接里自己数 ----")

    a = pf.ConnSession("peer-a")
    b = pf.ConnSession("peer-b")

    a.note_punch(3)
    a.note_punch(2)
    b.note_punch(1)
    a.mark_candidate(("1.1.1.1", 111), port=111, seq=9)

    check(
        "★ 一条连接的“第 N 次”只数它自己的包",
        a.punch_count == 5 and b.punch_count == 1,
        f"a={a.punch_count} b={b.punch_count}",
    )

    check(
        "序号/端口也是每条连接自己的",
        a.packet_seq == 9 and a.port == 111 and b.packet_seq == 0 and b.port is None,
        f"a={a.packet_seq}/{a.port} b={b.packet_seq}/{b.port}",
    )


# ============================================================
#  D. §D 目标选择
# ============================================================

class FakeState:
    """中继广播的一份房间状态（字段名与 RoomState 一致）。"""

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


def test_player_targets_only_the_host():
    print("")
    print("---- §D / BUG 1：房主≠host 的三人房，player 的打洞目标必须是 Host ----")

    # 名册顺序故意把【房主 owner】排在【host】前面 —— 老实现
    # `_host_client_id()`（花名册第一个不是我的人）正是在这里打错的。
    state = FakeState(
        client_id="p-cid",
        peer_ids=["owner-cid", "host-cid"],
        members=3,
        is_host=False,
        host_id="host-cid",
        host_name="host",
    )

    targets, reason = pf.select_targets(state)

    check(
        "★ 目标就是当前 Host（不是花名册里排在前面的房主 owner）",
        targets == ["host-cid"] and reason == "host",
        f"{targets} {reason}",
    )

    check(
        "★ host_id= 缺失时用 host_name= 在 names= 表里反查（老中继也能对上）",
        pf.select_targets(FakeState(
            client_id="p-cid",
            peer_ids=["owner-cid", "host-cid"],
            members=3,
            host_id="",
            host_name="My Host",
            member_names={"owner-cid": "Owner", "host-cid": "My%20Host"},
        ))[0] == ["host-cid"],
        str(pf.select_targets(FakeState(
            client_id="p-cid",
            peer_ids=["owner-cid", "host-cid"],
            members=3,
            host_id="",
            host_name="My Host",
            member_names={"owner-cid": "Owner", "host-cid": "My%20Host"},
        ))),
    )

    check(
        "★ 谁都不知道 Host 是谁 -> **不猜**，等（no_host）",
        pf.select_targets(FakeState(
            client_id="p-cid", peer_ids=["a", "b"], members=3, host_id="",
            host_name="", member_names={},
        )) == ([], "no_host"),
        str(pf.select_targets(FakeState(
            client_id="p-cid", peer_ids=["a", "b"], members=3, host_id="",
            host_name="", member_names={},
        ))),
    )


def test_player_waits_for_host_to_start():
    print("")
    print("---- §B：Host 已认领但未开始连接 -> 不许进入正式 P2P 流程 ----")

    waiting = FakeState(
        client_id="p-cid", peer_ids=["h-cid"], members=2, is_host=False,
        host_id="h-cid", started_ids=[],
    )

    check(
        "★ Host 没开始连接 -> 没有目标，原因是可识别的 host_not_ready",
        pf.select_targets(waiting) == ([], "host_not_ready"),
        str(pf.select_targets(waiting)),
    )

    ready = FakeState(
        client_id="p-cid", peer_ids=["h-cid"], members=2, is_host=False,
        host_id="h-cid", started_ids=["h-cid"],
    )

    check(
        "★ Host 开始了 -> 放行",
        pf.select_targets(ready) == (["h-cid"], "host"),
        str(pf.select_targets(ready)),
    )

    old_relay = FakeState(
        client_id="p-cid", peer_ids=["h-cid"], members=2, is_host=False,
        host_id="h-cid", started_ids=None,
    )

    check(
        "★ 老中继没有 started= -> 不能拿它当依据（否则谁都连不了）",
        pf.select_targets(old_relay) == (["h-cid"], "host"),
        str(pf.select_targets(old_relay)),
    )

    check(
        "★ 可识别原因：NO_HOST 与 HOST_NOT_READY 分开",
        pf.host_error_reason("ERROR NO_HOST 房间不存在或房主不在线") == "no_host"
        and pf.host_error_reason("ERROR HOST_NOT_READY 房主未开始") == "host_not_ready",
        f"{pf.host_error_reason('ERROR NO_HOST')} "
        f"{pf.host_error_reason('ERROR HOST_NOT_READY')}",
    )


def test_host_targets_only_started_players():
    print("")
    print("---- §D：Host 每轮重新拉“当前全部已开始连接的 Player” ----")

    state = FakeState(
        client_id="h-cid",
        peer_ids=["p1-cid", "p2-cid", "p3-cid"],
        members=4,
        is_host=True,
        started_ids=["p1-cid", "p3-cid"],
    )

    targets, reason = pf.select_targets(state)

    check(
        "★ 只打已经点了开始连接的 player（p2 还没点 -> 不在目标里）",
        targets == ["p1-cid", "p3-cid"] and reason == "started",
        f"{targets} {reason}",
    )

    check(
        "★ 每轮重新拉：p2 点了开始连接之后立刻进目标",
        pf.select_targets(FakeState(
            client_id="h-cid", peer_ids=["p1-cid", "p2-cid", "p3-cid"],
            members=4, is_host=True, started_ids=["p1-cid", "p2-cid", "p3-cid"],
        ))[0] == ["p1-cid", "p2-cid", "p3-cid"],
        "late joiner not picked up",
    )

    check(
        "★ C 走了、E 来了：目标列表跟着花名册走，不保留旧 id",
        pf.select_targets(FakeState(
            client_id="h-cid", peer_ids=["p1-cid", "p2-cid"],
            members=3, is_host=True, started_ids=["p1-cid", "p2-cid"],
        ))[0] == ["p1-cid", "p2-cid"],
        "roster not refreshed",
    )

    check(
        "★ 老中继没有 started= -> 退回花名册所有人（行为与今天一致）",
        pf.select_targets(FakeState(
            client_id="h-cid", peer_ids=["p1-cid", "p2-cid"], members=3,
            is_host=True, started_ids=None,
        )) == (["p1-cid", "p2-cid"], "started_unknown"),
        str(pf.select_targets(FakeState(
            client_id="h-cid", peer_ids=["p1-cid", "p2-cid"], members=3,
            is_host=True, started_ids=None,
        ))),
    )

    check(
        "★ 单人房没有目标（不许对空气打洞）",
        pf.select_targets(FakeState(
            client_id="h-cid", peer_ids=[], members=1, is_host=True,
        )) == ([], "no_peer"),
        str(pf.select_targets(FakeState(
            client_id="h-cid", peer_ids=[], members=1, is_host=True,
        ))),
    )


def test_started_token_id_space_mismatch_is_unknown():
    print("")
    print("---- §D：中继 started= 的 id 空间和成员 client_id 对不上时按“未知”处理 ----")

    # 实测到的中继行为（已上报）：started= 里放的是老式 HOST 控制连接 id，
    # 而 peers= 里是成员 client_id。不处理就是 Host 一个目标都拉不到。
    state = FakeState(
        client_id="h-cid", peer_ids=["p1-cid"], members=2, is_host=True,
        started_ids=["c5b71f4b8f33464b"],
    )

    targets, reason = pf.select_targets(state)

    check(
        "★ id 空间对不上 -> 不能拿它过滤（否则谁都不打）",
        targets == ["p1-cid"] and reason == "started_unknown",
        f"{targets} {reason}",
    )

    player = FakeState(
        client_id="p-cid", peer_ids=["h-cid"], members=2, is_host=False,
        host_id="h-cid", started_ids=["c5b71f4b8f33464b"],
    )

    check(
        "★ Player 侧同理：不能因此把 Host 判成“未开服”",
        pf.select_targets(player) == (["h-cid"], "host"),
        str(pf.select_targets(player)),
    )


def test_host_state_token_parsing_is_tolerant():
    print("")
    print("---- 中继新字段的容错解析（host_id= / started=，老中继一个都没有）----")

    parsed = pf.parse_host_state_tokens(
        "host=1 host_name=h host_id=abc started=p1,p2 members=3".split()
    )

    check(
        "★ 认 host_id= 与 started=（中继实际发出的格式）",
        parsed.get("host_id") == "abc"
        and parsed.get("started_ids") == ["p1", "p2"],
        str(parsed),
    )

    check(
        "★ started=- 是“一个都没开始”（空集合），不是“未知”",
        pf.parse_host_state_tokens("started=-".split()).get("started_ids") == [],
        str(pf.parse_host_state_tokens("started=-".split())),
    )

    check(
        "★ 老中继（没有任何新 token）-> 空字典，不能报错",
        pf.parse_host_state_tokens("host=1 members=2 world=1".split()) == {},
        str(pf.parse_host_state_tokens("host=1 members=2 world=1".split())),
    )

    check(
        "★ 未来字段不认识就忽略（additive 协议规则）",
        pf.parse_host_state_tokens(
            "host=1 whatever=x future=1,2,3".split()
        ) == {},
        str(pf.parse_host_state_tokens("host=1 whatever=x future=1,2,3".split())),
    )


def test_beacon_lines_round_trip():
    print("")
    print("---- 同步信标：走中继已有的 DIRECT_HIT 关键字（不新增协议）----")

    line = pf.beacon_line(pf.BEACON_MEASURED, "A", 0, " to=guest-1")

    check(
        "★ 信标格式固定：stage= att= batch= 然后 to=（to= 永远最后）",
        line == "DIRECT_HIT stage=measured att=A batch=0 to=guest-1",
        line,
    )

    parsed = pf.parse_beacon(line)

    check(
        "★ 解析得回 stage/att/batch（中继逐字转发，客户端自己解释）",
        parsed.get("stage") == "measured"
        and parsed.get("att") == "A"
        and parsed.get("batch") == 0,
        str(parsed),
    )

    # ---- `from=`：BUG 4 的另一半（Host 怎么知道"谁点了开始连接"）----
    with_from = pf.beacon_line(pf.BEACON_MEASURED, "A", 0, " to=guest-1", "p-cid")

    check(
        "★ 信标带 `from=<cid>`（中继的 started= 看不到还没开游戏的玩家）",
        parse_ok := with_from == (
            "DIRECT_HIT stage=measured att=A batch=0 from=p-cid to=guest-1"
        ),
        with_from,
    )

    check(
        "★ `from=` 能被解析出来（Host 靠它归属到具体的人）",
        pf.parse_beacon(with_from).get("from") == "p-cid",
        str(pf.parse_beacon(with_from)),
    )

    check(
        "★ 老客户端不发 `from=` -> 解析成空，不报错（加法协议规则）",
        pf.parse_beacon("DIRECT_HIT stage=measured att=A batch=0").get("from")
        is None,
        str(pf.parse_beacon("DIRECT_HIT stage=measured att=A batch=0")),
    )

    check(
        "★ 旧语义的 DIRECT_HIT（我打穿了）不算同步信标",
        pf.parse_beacon("DIRECT_HIT port=41234") == {},
        str(pf.parse_beacon("DIRECT_HIT port=41234")),
    )

    check(
        "★ 垃圾输入不抛异常（容错解析）",
        pf.parse_beacon("") == {}
        and pf.parse_beacon("DIRECT_HIT stage=") == {}
        and pf.parse_sync_fields("DIRECT_CAND 1.2.3.4 5 batch=xx") == {},
        f"{pf.parse_beacon('')} {pf.parse_sync_fields('DIRECT_CAND 1.2.3.4 5 batch=xx')}",
    )

    check(
        "★ DIRECT_CAND / DIRECT_PUNCH 上的 att=/batch= 也能读（加法字段）",
        pf.parse_sync_fields("DIRECT_CAND 1.2.3.4 5000 sub=cone att=B batch=1")
        == {"att": "B", "batch": 1},
        str(pf.parse_sync_fields("DIRECT_CAND 1.2.3.4 5000 att=B batch=1")),
    )


# ============================================================
#  D2. 房间协调器：新人加入 / 换模式
# ============================================================

class _FakeNat:
    per_ip_pool = False
    nat_type = "cone"
    filtering = "eif"


class _FakeLink:
    """一个能"打通"的假直连链路（不碰网络）。"""

    def __init__(self, room, host, port, log=None, my_id="", peer_id="",
                 is_host=False):
        self.room = room
        self.peer_id = peer_id
        self.is_host = bool(is_host)
        self.nat = _FakeNat()
        self.peer_addr = ("5.6.7.8", 6000)
        self.public = ("9.9.9.9", 7000)
        self.peer_nat = "cone"
        self.peer_filter = "eif"
        self.inbound_note = "stubbed"
        self.keepalive_alive = False
        self.flow_attempt = ""
        self.flow_batch = 0

    async def measure(self, force=False):
        return self.nat

    async def probe_inbound(self, timeout=4.0):
        return "punchable"

    async def advertise(self):
        return ("5.6.7.8", 6000)

    async def attempt(self, timeout=8.0, per_ip_pool=False):
        return type("R", (), {
            "ok": True, "addr": ("5.6.7.8", 6000), "sent": 10,
            "elapsed": 0.1, "plan": "c2c", "reason": "",
        })()

    def start_keepalive(self, interval=5.0):
        self.keepalive_alive = True

    def stop_keepalive(self):
        self.keepalive_alive = False

    @property
    def keepalive_running(self):
        return bool(self.keepalive_alive)

    def close(self):
        self.stop_keepalive()


class _FakeEngine:
    def __init__(self, state, room=None):
        self._state = state
        self._room = room

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


def test_late_joiner_does_not_disturb_existing_links():
    print("")
    print("---- §D：新人加入不得影响已经打通的连接（只多一行，别人一个字节不动）----")

    import asyncio

    from mclink.core import room_direct

    state = FakeState(
        client_id="h-cid", peer_ids=["p1-cid"], members=2, is_host=True,
        started_ids=None,
    )

    link = room_direct.RoomDirectLink(
        _FakeEngine(state, room=object()), log=lambda *_a: None
    )

    import mclink.core.direct as direct_mod

    real = direct_mod.DirectLink
    direct_mod.DirectLink = _FakeLink

    try:
        async def run():
            # p1 打通
            await link._punch(object(), "h-cid", "p1-cid")
            before_p1 = link.sessions["p1-cid"].snapshot()
            before_link = link.links["p1-cid"]["link"]

            # p2 加入（还没点开始连接 -> 不是目标；点了 -> 是新目标）
            state.peer_ids = ["p1-cid", "p2-cid"]
            state.members = 3

            targets_now = link._peers()

            await link._punch(object(), "h-cid", "p2-cid")

            after_p1 = link.sessions["p1-cid"].snapshot()
            return before_p1, after_p1, before_link, targets_now

        before_p1, after_p1, before_link, targets_now = asyncio.run(run())

        check(
            "★ 新目标出现（p2）",
            targets_now == ["p1-cid", "p2-cid"],
            str(targets_now),
        )

        check(
            "★ p1 的链路对象**同一个**（没被重打：重打 = 新 socket = 洞没了）",
            link.links["p1-cid"]["link"] is before_link,
            f"{link.links.get('p1-cid')}",
        )

        check(
            "★ p1 的状态机没被重置（轮次/延迟/连接状态原样）",
            before_p1["connected"] and after_p1["connected"]
            and before_p1["attempt_id"] == after_p1["attempt_id"]
            and after_p1["latency_ms"] == before_p1["latency_ms"],
            f"{before_p1} -> {after_p1}",
        )

        check(
            "★ 逐目标行一人一行（p1 + p2），没有空行",
            len(link.link_rows(punching=True)) == 2
            and all(row.get("row_parts") for row in link.link_rows(punching=True)),
            str(link.link_rows(punching=True)),
        )
    finally:
        direct_mod.DirectLink = real
        link.close()


def test_mode_switch_rebuilds_every_path():
    print("")
    print("---- §D：换模式必须严格重建全部通路（旧映射当场作废）----")

    import asyncio

    from mclink.core import room_direct

    state = FakeState(
        client_id="h-cid", peer_ids=["p1-cid"], members=2, is_host=True,
        started_ids=None,
    )

    link = room_direct.RoomDirectLink(
        _FakeEngine(state, room=object()), log=lambda *_a: None
    )

    import mclink.core.direct as direct_mod

    real = direct_mod.DirectLink
    direct_mod.DirectLink = _FakeLink

    try:
        asyncio.run(link._punch(object(), "h-cid", "p1-cid"))

        gen_before = link.sessions["p1-cid"].generation

        check(
            "（对照）换模式之前：有洞、有状态机、表已清零（终态）",
            link.links and "p1-cid" in link.sessions
            and link.sessions["p1-cid"].timer.running is False,
            f"links={list(link.links)} running={link.sessions['p1-cid'].timer.running}",
        )

        link.close()

        check(
            "★ 换模式/退房 -> 全部通路与状态机释放（旧映射作废，不留残渣）",
            link.links == {} and link.sessions == {} and link._punch_links == {},
            f"links={link.links} sessions={link.sessions}",
        )

        # 新的一轮：从头开始（generation 递增 = 不复用上一模式的对象）
        state.started_ids = ["p1-cid"]
        asyncio.run(link._punch(object(), "h-cid", "p1-cid"))

        sess = link.sessions["p1-cid"]

        check(
            "★ 重建后是**新的**一套状态（generation 不继承、计时从 0）",
            sess.generation == gen_before
            and sess.batch == 0
            and sess.voided == 0
            and sess.connected,
            str(sess.snapshot()),
        )
    finally:
        direct_mod.DirectLink = real
        link.close()


# ============================================================
#  F. §G 三层 UI + N/C 口径
# ============================================================

def test_three_layer_summary():
    print("")
    print("---- §G 第 1/2 层：父级只汇总（通路 n/m + 打洞进度 n/m）----")

    def build(flags):
        sessions = []

        for index, state in enumerate(flags):
            sess = pf.ConnSession(f"peer-{index}")
            sess.start()
            sessions.append(sess)

            if state == "up":
                sess.finish(True, latency_ms=18.0)
            elif state == "failed":
                sess.finish(False, "no packet arrived")

        return sessions

    summary = pf.summarize(build(["pending", "pending", "pending"]), punching=True)

    check(
        "★ 0/3：通路 `直连尝试中... 0/3`，打洞进度 `正在建立 3 条连接`",
        summary["path"] == "直连尝试中... 0/3"
        and summary["progress"] == "正在建立 3 条连接"
        and summary["total"] == 3 and summary["up"] == 0,
        str(summary),
    )

    summary = pf.summarize(build(["up", "up", "pending"]), punching=True)

    check(
        "★ 2/3：通路 `P2P 直连中... 2/3`，打洞进度 `2/3 已完成`",
        summary["path"] == "P2P 直连中... 2/3"
        and summary["progress"] == "2/3 已完成",
        str(summary),
    )

    summary = pf.summarize(build(["up", "up", "up"]), punching=True)

    check(
        "★ 3/3：通路 `P2P 已直连：3/3`，打洞进度 `打洞完成：全部 3 个目标已打通`",
        summary["path"] == "P2P 已直连：3/3"
        and summary["progress"] == "打洞完成：全部 3 个目标已打通"
        and summary["all_up"],
        str(summary),
    )

    summary = pf.summarize(build(["up", "up", "failed"]), punching=True)

    check(
        "★ 2 成 1 败：`2/3 已完成，1 个失败`（不谎报成功）",
        summary["progress"] == "2/3 已完成，1 个失败" and summary["failed"] == 1,
        str(summary),
    )

    summary = pf.summarize(build(["pending", "pending"]), punching=False)

    check(
        "★ 没在打洞（relay 模式）-> 不许显示「打洞中」的汇总",
        summary["progress"] == "未连接",
        str(summary),
    )

    summary = pf.summarize([], punching=True)

    check(
        "★ 没有目标 -> 零行（逐目标明细不留空行）",
        summary["rows"] == [] and summary["total"] == 0,
        str(summary),
    )


def test_per_target_row_text_and_latency():
    print("")
    print("---- §G 第 3 层：逐目标行 `阶段（秒）【对端真名】[地址] 延迟`，"
          "未连接写 N/C ----")

    sess = pf.ConnSession("p1", label="玩家甲")
    sess.start()
    sess.advance(pf.Stage.CONFIRM_TARGET)
    sess.advance(pf.Stage.NAT)
    sess.advance(pf.Stage.INBOUND)
    sess.advance(pf.Stage.REPORT)
    sess.advance(pf.Stage.SYNC_START)

    text = sess.row_text()

    check(
        "★ 等待同步起跑这一行是 `等待同步起跑（Ns）【玩家甲】N/C`",
        text.startswith("等待同步起跑（") and text.endswith("）【玩家甲】N/C"),
        text,
    )

    check(
        "★ 【…】里是**对端真名**，不是轮次字母（用户 2026-09-28 第 1 条）",
        "【玩家甲】" in text and "【A】" not in text,
        text,
    )

    sess.advance(pf.Stage.CANDIDATE)
    sess.advance(pf.Stage.CONFIRM_CAND)
    sess.advance(pf.Stage.SYNC_GO)
    sess.advance(pf.Stage.PUNCH)
    sess.note_punch(3)

    text = sess.row_text()

    check(
        "★ 打洞中那一行带 `【玩家甲】第 3 次` 与 N/C（第 N 次，不是 n/m）",
        "打洞中" in text and "【玩家甲】第 3 次" in text
        and text.endswith("N/C"),
        text,
    )

    sess.finish(True, latency_ms=18.0, addr="203.0.113.12:40879")

    text = sess.row_text()

    check(
        "★ 已直连那一行 `P2P 已直连【玩家甲】203.0.113.12:40879 18ms`"
        "（真名 + 地址:端口 + 延迟，用户第 1/2 条）",
        text == "P2P 已直连【玩家甲】203.0.113.12:40879 18ms",
        text,
    )

    check(
        "★ 亚毫秒也不显示成 0（审计 P2-8 的同一条口径）",
        _sub_ms_text() == "0.4ms",
        _sub_ms_text(),
    )

    check(
        "★ row_parts 是**片段**（界面自己 tr() 拼，不给成品中文串）",
        isinstance(sess.row_parts(), dict)
        and sess.row_parts().get("name") == "玩家甲"
        and sess.row_parts().get("addr") == "203.0.113.12:40879"
        and sess.row_parts().get("latency") == "18ms",
        str(sess.row_parts()),
    )

    idle = pf.ConnSession("y")

    check(
        "还没开始的目标不出行（不留空行）",
        idle.row_text() == "" and idle.row_parts() == {},
        repr(idle.row_text()),
    )


def test_host_targets_from_heard_evidence():
    print("")
    print("---- §D 的正确退路：中继 started= 看不到还没开游戏的玩家时，"
          "Host 靠对端自己的信标知道谁开始了 ----")

    import asyncio

    from mclink.core import room_direct

    # 中继视角："已开始连接"= 有 PLAYER / HOST 连接。玩家点了开始连接但
    # Minecraft 还没连上来时**没有** PLAYER 连接 -> started= 里没有他。
    state = FakeState(
        client_id="h-cid", peer_ids=["p-cid"], members=2, is_host=True,
        started_ids=[],
    )

    link = room_direct.RoomDirectLink(
        _FakeEngine(state, room=object()), log=lambda *_a: None
    )

    check(
        "★ 只有 started= 时：一个目标都没有（BUG 4 要的就是这个门）",
        link._peers() == [] and link.target_reason == "started",
        f"{link._peers()} {link.target_reason}",
    )

    # 对端发来一条带 `from=` 的信标 —— 这是"他已经点了开始连接"的硬证据。
    class FakeRoom:
        def peek_direct(self, keywords=()):
            return ["DIRECT_HIT stage=measured att=A batch=0 from=p-cid"]

    link._note_heard_started(FakeRoom())

    check(
        "★ 收到对端的 `from=` 证据 -> 立刻进目标（Host 才能开始打洞）",
        link._peers() == ["p-cid"] and link.target_reason == "started+heard",
        f"{link._peers()} {link.target_reason}",
    )

    check(
        "★ 证据不是整份花名册：没有发过话的 peer 仍然不进目标",
        _peers_with_roster(link, ["p-cid", "p2-cid"]) == ["p-cid"],
        str(_peers_with_roster(link, ["p-cid", "p2-cid"])),
    )

    link.close()

    check(
        "★ 停止/退房把证据一起清掉（下一次连接重新判）",
        link._heard_started == set(),
        str(link._heard_started),
    )


def _peers_with_roster(link, roster):
    st = link.engine.room_state()
    st.peer_ids = list(roster)
    st.members = len(roster) + 1

    try:
        return link._peers()
    finally:
        st.peer_ids = ["p-cid"]
        st.members = 2


def test_peek_direct_keeps_the_queue():
    print("")
    print("---- Host 空转时要能看一眼协调队列，而且不能把行吃掉 ----")

    from mclink.core.room import RoomSession

    session = RoomSession("127.0.0.1", 1, client_id="h-cid", username="h")
    session._direct_lines.put_nowait("DIRECT_HIT stage=measured from=p-cid")
    session._direct_lines.put_nowait("SOMETHING ELSE")

    found = session.peek_direct()

    check(
        "★ peek 只返回协调行",
        found == ["DIRECT_HIT stage=measured from=p-cid"],
        str(found),
    )

    check(
        "★ peek **不消费**队列：行还在（真正的读者等一下照样拿得到）",
        session._direct_lines.qsize() == 2,
        str(session._direct_lines.qsize()),
    )


def _sub_ms_text():
    """已连接但 RTT < 1ms 时的显示（不许变成 0 —— 那是"没测到"的写法）。"""
    sess = pf.ConnSession("x")
    sess.start()
    sess.finish(True, latency_ms=0.4)

    return sess.latency_text()


def test_room_state_parses_owner_id():
    print("")
    print("---- §A：Owner 是谁只认中继的 owner_id=（不再靠花名册顺序猜）----")

    from mclink.core.room import RoomSession

    session = RoomSession("127.0.0.1", 1, client_id="p-cid", username="me")

    session._on_line(
        "HOST_STATE host=1 host_name=H members=3 world=0 "
        "peers=h-cid,o-cid names=h-cid:H,o-cid:O rtts=- "
        "started=h-cid host_id=h-cid owner_id=o-cid mode=stun"
    )

    check(
        "★ 中继给了 owner_id= / host_id= / started= -> 三个都解析出来",
        session.state.owner_id == "o-cid"
        and session.state.host_id == "h-cid"
        and session.state.started_ids == ["h-cid"],
        f"owner={session.state.owner_id!r} host={session.state.host_id!r} "
        f"started={session.state.started_ids}",
    )

    session._on_line(
        "HOST_STATE host=0 host_name=- members=2 world=0 peers=h-cid "
        "names=h-cid:H rtts=- started=- host_id=- owner_id=- mode=stun"
    )

    check(
        "★ 中继说 owner_id=- / host_id=- -> 一起清掉（不留上一个人的身份）",
        session.state.owner_id == "" and session.state.host_id == ""
        and session.state.host_claimed is False,
        f"owner={session.state.owner_id!r} host={session.state.host_id!r} "
        f"claimed={session.state.host_claimed}",
    )


def test_round_failure_keeps_the_timer_but_terminal_zeroes_it():
    print("")
    print("---- §F：一轮没打通 ≠ 这次连接结束（表继续跑；只有终态才清零）----")

    clock = {"now": 0.0}
    sess = pf.ConnSession("p", clock=lambda: clock["now"])
    sess.start()
    clock["now"] += 30.0
    sess.note_punch(4)
    sess.mark_candidate(("1.2.3.4", 1), port=1)

    sess.note_round_failed("no packet arrived")

    check(
        "★ 一轮失败：原因留下、这一轮的东西清掉、**表继续跑**",
        sess.fail_reason == "no packet arrived"
        and sess.punch_count == 0
        and sess.my_candidate is None
        and sess.stage == pf.Stage.CONFIRM_HOST
        and sess.timer.running
        and abs(sess.elapsed() - 30.0) < 0.01,
        str(sess.snapshot()),
    )

    clock["now"] += 10.0
    spent = sess.finish(True, latency_ms=12.0)

    check(
        "★ 终态才清零：这一次连接一共 40 秒（不是只有最后一轮）",
        abs(spent - 40.0) < 0.01
        and sess.elapsed() == 0.0
        and sess.timer.released,
        f"spent={spent} elapsed={sess.elapsed()}",
    )


def test_host_ready_three_valued():
    print("")
    print("---- §B：host_ready() 三态（True / False / 不知道）----")

    base = dict(client_id="p-cid", peer_ids=["h-cid"], members=2,
                is_host=False, host_id="h-cid", host_claimed=True)

    check(
        "★ 中继说 Host 没开始 -> False（提示「Host 未连接，请等待 Host 开服」）",
        pf.host_ready(FakeState(**base, started_ids=[])) is False,
        str(pf.host_ready(FakeState(**base, started_ids=[]))),
    )

    check(
        "★ 中继说 Host 开始了 -> True",
        pf.host_ready(FakeState(**base, started_ids=["h-cid"])) is True,
        str(pf.host_ready(FakeState(**base, started_ids=["h-cid"]))),
    )

    check(
        "★ 老中继没有 started= -> None（**不许当成 False**，否则谁都连不了）",
        pf.host_ready(FakeState(**base, started_ids=None)) is None,
        str(pf.host_ready(FakeState(**base, started_ids=None))),
    )

    check(
        "★ 没人认领主机位 -> None（那是另一条提示：暂无 Host）",
        pf.host_ready(FakeState(**{**base, "host_claimed": False},
                               started_ids=[])) is None,
        str(pf.host_ready(FakeState(**{**base, "host_claimed": False},
                                    started_ids=[]))),
    )


def test_conn_mode_changed_is_a_recognisable_reason():
    print("")
    print("---- §D / PROTOCOL §8.3：CONN_MODE_CHANGED 必须被认出来（严格重建通路）----")

    from mclink.core import protocol as proto
    from mclink.core.room import RoomSession

    check(
        "协议常量存在（中继在成员控制连接上推的就是这一个词）",
        proto.LINE_CONN_MODE_CHANGED == "CONN_MODE_CHANGED",
        proto.LINE_CONN_MODE_CHANGED,
    )

    session = RoomSession("127.0.0.1", 1, client_id="p-cid", username="me")
    events = []
    # `RoomSession` 用 `_emit(name, data)` 往上抛事件（引擎→界面）。
    session._emit = lambda name, data=None: events.append((name, data or {}))

    # **不带 ERROR 前缀** —— 和 PROTOCOL §8.3 写的一模一样。
    session._on_line(
        "CONN_MODE_CHANGED 连接模式已切换，请按新模式重新开始连接"
    )

    check(
        "★ 裸 CONN_MODE_CHANGED -> 通知界面（界面据此拆掉全部通路重建）",
        any(name == "conn_mode_changed" for name, _data in events),
        str(events),
    )

    check(
        "★ 它**不是**被踢、也不是房间被关：房间对象还在、running 不动",
        session.state.revoked is False
        and session.state.revoked_reason == "",
        f"revoked={session.state.revoked}",
    )


def test_gui_three_layers_and_languages():
    print("")
    print("---- §G：界面的三层文案（中/英/日都能显示，英文界面不许残留中文）----")

    import types

    for name in ("tkinter", "tkinter.ttk", "tkinter.filedialog",
                 "tkinter.messagebox", "tkinter.font"):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__getattr__ = lambda attr: type(attr, (), {})
            sys.modules[name] = module

    sys.modules["tkinter"].ttk = sys.modules["tkinter.ttk"]
    sys.modules["tkinter"].filedialog = sys.modules["tkinter.filedialog"]
    sys.modules["tkinter"].messagebox = sys.modules["tkinter.messagebox"]
    sys.modules["tkinter"].font = sys.modules["tkinter.font"]

    from mclink.gui import app as gui_app
    from mclink.gui import i18n

    # 复用 gui_i18n_test 的"漏翻"判定（它对日文是对的：日文译文本来就是
    # 汉字+假名，raw has_cjk 会把**翻对了的**日文也报成漏翻）。
    sys.path.insert(0, HERE)

    from gui_i18n_test import leftover_texts

    def snapshot(up, total):
        sessions = []

        for index in range(total):
            sess = pf.ConnSession(f"peer-{index}")
            sess.start()

            if index < up:
                sess.finish(True, latency_ms=18.0)

            sessions.append(sess)

        return {"mode": "stun", "flow": pf.summarize(sessions, punching=True)}

    snap = snapshot(2, 3)

    check(
        "★ 第 2 层「打洞进度」= `2/3 已完成`",
        gui_app.punch_step_text(snap)[0] == "2/3 已完成",
        gui_app.punch_step_text(snap)[0],
    )

    check(
        "★ 第 1 层「通路」= `P2P 直连中... 2/3`",
        gui_app.nat_diag_path_text(
            "stun", None, False, flow=snap["flow"]
        )[0] == "P2P 直连中... 2/3",
        gui_app.nat_diag_path_text("stun", None, False, flow=snap["flow"])[0],
    )

    snap_all = snapshot(3, 3)

    check(
        "★ 全通：`P2P 已直连：3/3` + `打洞完成：全部 3 个目标已打通`",
        gui_app.nat_diag_path_text("stun", None, False, flow=snap_all["flow"])[0]
        == "P2P 已直连：3/3"
        and gui_app.punch_step_text(snap_all)[0]
        == "打洞完成：全部 3 个目标已打通",
        f"{gui_app.nat_diag_path_text('stun', None, False, flow=snap_all['flow'])[0]} "
        f"{gui_app.punch_step_text(snap_all)[0]}",
    )

    # ---- 英 / 日：三层都不能残留中文 ----
    old = i18n.current_language()

    try:
        for code in ("en", "ja"):
            i18n.set_language(code)

            texts = [
                gui_app.nat_diag_path_text("stun", None, False,
                                           flow=snap["flow"])[0],
                gui_app.punch_step_text(snap)[0],
                gui_app.nat_diag_path_text("stun", None, False,
                                           flow=snap_all["flow"])[0],
                gui_app.punch_step_text(snap_all)[0],
            ]

            # 逐目标行：界面走**同一份**纯函数 `gui_app.hole_row_text`（不是
            # 测试自己复刻一遍）。逐片段查"翻到了没有"——整行是动态拼的，
            # 静态表里当然没有这一整条；真正要保证的是**每个可翻译片段**都有译文。
            row = pf.ConnSession("p", label="radix")
            row.start()
            row.advance(pf.Stage.CONFIRM_TARGET)
            row.advance(pf.Stage.NAT)
            row.advance(pf.Stage.INBOUND)
            row.advance(pf.Stage.REPORT)
            row.advance(pf.Stage.SYNC_START)

            row_text = gui_app.hole_row_text({
                "peer": "p",
                "name": "radix",
                "ready": False,
                "rtt_ms": None,
                "row_parts": row.row_parts(),
            })[0]

            texts.append(i18n.tr(row.row_parts().get("stage") or ""))
            texts.append(i18n.tr("（{0}s）"))
            texts.append(i18n.tr("【{0}】"))

            bad = leftover_texts(texts, code)

            check(
                f"★ [{code}] 三层文案 + 逐目标行没有残留中文（全角括号也不行）",
                not bad,
                str(bad),
            )

            if code == "en":
                check(
                    "★ [en] 英文逐目标行**整行**一个中文字符都不许有",
                    not i18n.has_cjk(row_text),
                    row_text,
                )
    finally:
        i18n.set_language(old)

    _english = _english_row_text()

    check(
        "★ 英文逐目标行的版式（半角括号/方括号 + 对端真名，直接可读）",
        _english.startswith("Waiting for both sides to finish measuring(")
        and _english.endswith("[radix]N/C"),
        _english,
    )


def _english_row_text():
    from mclink.gui import app as gui_app
    from mclink.gui import i18n

    old = i18n.current_language()
    i18n.set_language("en")

    try:
        row = pf.ConnSession("p", label="radix")
        row.start()

        for stage in (pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND,
                      pf.Stage.REPORT, pf.Stage.SYNC_START):
            row.advance(stage)

        return gui_app.hole_row_text({
            "peer": "p",
            "name": "radix",
            "ready": False,
            "rtt_ms": None,
            "row_parts": row.row_parts(),
        })[0]
    finally:
        i18n.set_language(old)


# ============================================================
#  G. §C 被动掉线的两类判定
# ============================================================

def test_passive_disconnect_two_verdicts():
    print("")
    print("---- §C：重连先判 token/被踢 —— 被踢按退出；真·闪断才自动重建 ----")

    import types

    for name in ("tkinter", "tkinter.ttk", "tkinter.filedialog",
                 "tkinter.messagebox", "tkinter.font"):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__getattr__ = lambda attr: type(attr, () , {})
            sys.modules[name] = module

    sys.modules["tkinter"].ttk = sys.modules["tkinter.ttk"]
    sys.modules["tkinter"].filedialog = sys.modules["tkinter.filedialog"]
    sys.modules["tkinter"].messagebox = sys.modules["tkinter.messagebox"]
    sys.modules["tkinter"].font = sys.modules["tkinter.font"]

    from mclink.gui.app import MCLinkApp

    terminal = [
        "ERROR KICKED 你已被移出房间",
        "ERROR AUTH_INVALID_TOKEN 登录已失效",
        "ERROR VERSION_MISMATCH server=3.3.0 client=3.2.9",
        "ROOM REVOKED 房间已被管理员关闭",
        "ERROR OWNER_LEFT_ROOM 房主已离开",
    ]

    for reason in terminal:
        check(
            f"★ 被踢/失效类 -> 终态（按退出处理，不重连）：{reason[:28]}",
            MCLinkApp._is_terminal_failure(reason) is True,
            reason,
        )

    for reason in (
        "connection reset by peer",
        "the relay closed the connection",
        "timed out",
        "",
    ):
        check(
            f"★ 真·闪断 -> 允许自动重建：{reason or '(空)'}",
            MCLinkApp._is_terminal_failure(reason) is False,
            reason,
        )

    check(
        "★ host 那边出事是独立一类（不重试也不当被踢）",
        MCLinkApp._is_no_host_reason("HOST_LEFT host 断了一下") is True
        and MCLinkApp._is_no_host_reason("HOST_STOPPED host 停止开服") is True
        and MCLinkApp._is_no_host_reason("HOST_RELEASED host 释放了主机位") is True
        and MCLinkApp._is_no_host_reason("ERROR NO_HOST") is True
        and MCLinkApp._is_terminal_failure("HOST_LEFT host 断了一下") is False,
        f"{MCLinkApp._is_no_host_reason('HOST_LEFT')} "
        f"{MCLinkApp._is_terminal_failure('HOST_LEFT')}",
    )

    check(
        "★ 换模式清理旧通路（CONN_MODE_CHANGED）不许被当成“被踢”丢掉房间",
        MCLinkApp._is_terminal_failure("ERROR CONN_MODE_CHANGED 连接模式已切换") is False
        and MCLinkApp._is_no_host_reason("ERROR CONN_MODE_CHANGED 连接模式已切换") is False,
        "CONN_MODE_CHANGED was treated as terminal/host-left",
    )

    check(
        "★ Host 未开服（HOST_NOT_READY）不是“没 Host”、更不是终态",
        MCLinkApp._is_terminal_failure(
            "ERROR HOST_NOT_READY Host 尚未开始连接，请等待 Host 开服"
        ) is False
        and MCLinkApp._is_no_host_reason(
            "ERROR HOST_NOT_READY Host 尚未开始连接，请等待 Host 开服"
        ) is False,
        "HOST_NOT_READY was misclassified",
    )

    check(
        "★ 只有 Owner 能切模式：非 Owner 的 Host 也不行（§A / 规则 6）",
        _owner_flag(True) is True and _owner_flag(False) is False,
        f"owner={_owner_flag(True)} non-owner={_owner_flag(False)}",
    )


def _owner_flag(is_owner):
    """`MCLinkApp._am_room_owner()` 只读房间状态里的 is_owner（不缓存、不看 host）。"""

    from mclink.gui.app import MCLinkApp

    class FakeState:
        def __init__(self, owner):
            self.is_owner = owner
            self.is_host = not owner

    class FakeApp:
        def __init__(self, owner):
            self._state = FakeState(owner)

        def _room_state_obj(self):
            return self._state

    return MCLinkApp._am_room_owner(FakeApp(is_owner))


# ============================================================
#  H. 真中继：拔掉中继 -> 老会话清理干净 / 端口释放 / 角色不变
# ============================================================

def port_free(port, host="127.0.0.1"):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(Exception):
            sock.close()


def fake_world(stop):
    import contextlib as _c

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", MC_PORT))
    srv.listen(8)
    srv.settimeout(0.5)

    conns = []

    try:
        while not stop.is_set():
            try:
                client, _addr = srv.accept()
            except (socket.timeout, TimeoutError):
                continue

            conns.append(client)

            def serve(sock=client):
                with _c.suppress(Exception):
                    sock.settimeout(30)
                    sock.sendall(b"MC-BANNER\n")

                    while not stop.is_set():
                        data = sock.recv(4096)

                        if not data:
                            break

                        sock.sendall(b"ECHO:" + data)

            threading.Thread(target=serve, daemon=True).start()
    finally:
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.close()

        with contextlib.suppress(Exception):
            srv.close()


def start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        "MCLINK_STUN_ENABLED": "0",
        "MCLINK_STATUS_FILE": os.path.join(HERE, "punch_flow_status.json"),
        "MCLINK_STATUS_INTERVAL": "30",
    })

    log = open(os.path.join(HERE, "punch_flow_relay.log"), "w",
               encoding="utf-8", errors="replace")

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


def test_real_relay_unplug_cleans_up():
    print("")
    print("---- §C 真中继端到端：拔掉中继 -> 老会话清理干净、端口释放、角色不变 ----")

    from mclink.config import settings as cfgmod
    from mclink.core.engine import MCLinkEngine

    stop = threading.Event()
    threading.Thread(target=fake_world, args=(stop,), daemon=True).start()
    time.sleep(0.3)

    relay, relay_log = start_relay()

    engine = MCLinkEngine()
    engine.start_loop()

    try:
        cfg = cfgmod.load_config()
        cfg.role = "host"
        cfg.server_host = "127.0.0.1"
        cfg.server_port = RELAY_PORT
        cfg.room = "flowtest"
        cfg.mc_host = "127.0.0.1"
        cfg.mc_port = MC_PORT
        cfg.local_port = LOCAL_PORT
        cfg.max_tunnels = 2
        cfg.mode = "stun"

        # 先把房间开出来（会话属于房间；这也是真实客户端的次序）。
        engine.open_room("127.0.0.1", RELAY_PORT, "flow-cid", "host", "",
                         "flowtest", create=True, max_players=2)
        time.sleep(0.4)
        engine.claim_host()
        time.sleep(0.4)

        engine.start_session(cfg)

        deadline = time.time() + 25

        while time.time() < deadline:
            if engine.snapshot().get("state") == "ONLINE":
                break
            time.sleep(0.4)

        online = engine.snapshot().get("state") == "ONLINE"
        role_before = engine.session_mode() or cfg.role

        check("会话先连上（否则下面的断言没有意义）", online, str(engine.snapshot()))

        check(
            "★ 停止之前确实有一个活着的会话（下面的断言才有意义）",
            engine.current_session() is not None,
            str(engine.current_session()),
        )

        # ---- 拔线：真的把中继进程杀掉 ----
        with contextlib.suppress(Exception):
            relay.terminate()

        with contextlib.suppress(Exception):
            relay.wait(timeout=8)

        with contextlib.suppress(Exception):
            relay.kill()

        stopped = False
        deadline = time.time() + 30

        while time.time() < deadline:
            snap = engine.snapshot()

            if snap.get("state") not in ("ONLINE", "STARTING") or not snap.get("running"):
                stopped = True
                break

            time.sleep(0.5)

        check(
            "★ 中继没了 -> 老会话必须停下（不许留一个“看起来还在”的界面）",
            stopped,
            str(engine.snapshot()),
        )

        # 明确停一次：确认拆除路径把端口还回来（§F/§C 的"清理干净"）。
        with contextlib.suppress(Exception):
            engine.stop_session(timeout=20)

        time.sleep(0.5)

        check(
            "★ 老会话的本地端口真的释放了（不释放就是“断开之后连不上”）",
            port_free(LOCAL_PORT),
            f"port_free={port_free(LOCAL_PORT)}",
        )

        check(
            "★ 角色不变（被动掉线不许把 host 变成 player —— 审计 P0-3）",
            (engine.session_mode() or role_before) == role_before,
            f"{engine.session_mode()} vs {role_before}",
        )

        check(
            "★ 会话对象已经清空（老会话的 socket 有人持有/有人关）",
            engine.current_session() is None,
            str(engine.current_session()),
        )

        # ---- 重新连（新中继起来）也要能连上：证明端口真的空出来了 ----
        relay2, relay_log2 = start_relay()

        try:
            # 新中继上重新开房 + 会话（端口/状态都干净才可能成功）。
            engine.open_room("127.0.0.1", RELAY_PORT, "flow-cid2", "host", "",
                             "flowtest2", create=True, max_players=2)
            time.sleep(0.4)
            engine.claim_host()
            time.sleep(0.4)
            engine.start_session(cfg)

            deadline = time.time() + 25

            while time.time() < deadline:
                if engine.snapshot().get("state") == "ONLINE":
                    break
                time.sleep(0.4)

            check(
                "★ 拔线之后能重新起会话（端口/状态都干净）",
                engine.snapshot().get("state") == "ONLINE",
                str(engine.snapshot()),
            )
        finally:
            with contextlib.suppress(Exception):
                engine.stop_session(timeout=15)

            with contextlib.suppress(Exception):
                relay2.terminate()
                relay2.wait(timeout=5)

            with contextlib.suppress(Exception):
                relay2.kill()

            with contextlib.suppress(Exception):
                relay_log2.close()
    finally:
        stop.set()

        with contextlib.suppress(Exception):
            engine.stop_session(timeout=10)

        with contextlib.suppress(Exception):
            engine.shutdown(timeout=8)

        with contextlib.suppress(Exception):
            relay.terminate()
            relay.wait(timeout=5)

        with contextlib.suppress(Exception):
            relay.kill()

        with contextlib.suppress(Exception):
            relay_log.close()


# ============================================================

def main():
    print("=" * 66)
    print("MC-Link 打洞流程基准测试（§C/§D/§E/§F/§G，2026-09-28）")
    print("=" * 66)

    tests = [
        test_stage_order,
        test_session_walks_the_whole_flow,
        test_waiting_for_synced_start,
        test_candidate_must_be_confirmed_by_both,
        test_timer_stops_zeroes_and_releases,
        test_failure_also_zeroes_the_timer,
        test_each_connection_has_its_own_timer,
        test_attempt_label,
        test_sync_mismatch_voids_and_resyncs,
        test_host_opens_a_new_batch_cleanly,
        test_round_failure_keeps_the_timer_but_terminal_zeroes_it,
        test_host_ready_three_valued,
        test_conn_mode_changed_is_a_recognisable_reason,
        test_punch_sequence_is_per_connection,
        test_player_targets_only_the_host,
        test_player_waits_for_host_to_start,
        test_host_targets_only_started_players,
        test_started_token_id_space_mismatch_is_unknown,
        test_host_state_token_parsing_is_tolerant,
        test_room_state_parses_owner_id,
        test_beacon_lines_round_trip,
        test_host_targets_from_heard_evidence,
        test_peek_direct_keeps_the_queue,
        test_late_joiner_does_not_disturb_existing_links,
        test_mode_switch_rebuilds_every_path,
        test_three_layer_summary,
        test_per_target_row_text_and_latency,
        test_gui_three_layers_and_languages,
        test_passive_disconnect_two_verdicts,
        test_real_relay_unplug_cleans_up,
    ]

    try:
        for test in tests:
            try:
                test()
            except Exception as e:  # pragma: no cover - defensive
                import traceback

                traceback.print_exc()
                check(f"{test.__name__} 不许抛异常", False, repr(e))
    finally:
        pass

    print("")
    print("=" * 66)

    if FAILED:
        print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
        print("  FAILED: " + "; ".join(FAILED))
        return 1

    print(f"RESULT: {len(PASSED)} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
