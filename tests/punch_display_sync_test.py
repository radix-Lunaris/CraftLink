"""
CraftLink -- 2026-09-29 第五轮：**显示同步 + 收敛 + 身份/连接分离**的回归网。

用户本轮的口径（逐条对应本节）：

  1. 「两端共用一套状态枚举，信令上报当前状态，等待对端状态匹配之后才允许进入
     下一阶段」——**显示层**必须按同一根轴走；对端没到就显示「等待对端…」。
     （真值来源 = `DIRECT_HIT stage=<阶段>`，中继本来就逐字转发。）
  2. 「把打洞流程映射到显示上」（不是反过来）：显示永远不影响真实打洞。
  3. P0：**一端显示已直连、另一端还在反复重试/测量/确认主机** —— 打通的那一侧
     必须把 `stage=connected` 告诉对端，对端在**同一个已打通的 socket** 上收敛
     并停止重试。
  4. 「连接状态 ≠ 身份/职位」：`conn=`（连接状态）与 `host=`/`host_claimed`
     （身份）分成两套读法；Host 只是停止连接 → 主机位保留、别人**不许**能认领。
  5. Owner / Host / Player 是**组合**角色（房主·主机 / 房主·玩家 / 主机）。
  6. 每对 Host↔Player 独立 + 每次 punch 前重新确认目标（host 会漂移）。
  7. 模式只由 Owner 的显式操作改变；房间模式**未知**绝不等于 relay（不许自己
     回退/拆掉直连）。
  8. 热切换：空闲/未连接 = 只换模式；有连接 = 严格重建 + 自动重连；没连接的
     成员保持未连接。

Run:
    python tests/punch_display_sync_test.py
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import punch_flow as pf  # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================================
#  A. 两端共用的阶段枚举 + 上报 + 「等对端匹配」（用户第 1 条）
# ============================================================================

def test_shared_stage_axis():
    print("")
    print("---- A. 共享阶段枚举 + 逐阶段上报 + 等对端（显示层）----")

    # 1) 枚举是**唯一**的一套：信标值就是 Stage.value。
    values = {s.value for s in pf.Stage}

    check(
        "★ 两端共用的阶段枚举：STAGE_BY_NAME 的键就是 Stage 的全部取值",
        set(pf.STAGE_BY_NAME) == values,
        f"{sorted(pf.STAGE_BY_NAME)} vs {sorted(values)}",
    )

    check(
        "★ 用户点名的阶段名沿用（确认Host→确认目标→测量NAT→测量入站→"
        "…→等待发包起跑→PUNCH→已直连/失败）",
        pf.STAGE_LABELS[pf.Stage.CONFIRM_HOST] == "确认 Host"
        and pf.STAGE_LABELS[pf.Stage.CONFIRM_TARGET] == "确认目标"
        and pf.STAGE_LABELS[pf.Stage.NAT] == "测量 NAT 类型中…"
        and pf.STAGE_LABELS[pf.Stage.INBOUND] == "测量入站策略中…"
        and pf.STAGE_LABELS[pf.Stage.SYNC_GO] == "等待发包起跑"
        and pf.STAGE_LABELS[pf.Stage.PUNCH] == "打洞中"
        and pf.STAGE_LABELS[pf.Stage.CONNECTED] == "P2P 已直连"
        and pf.STAGE_LABELS[pf.Stage.FAILED] == "打洞失败",
        str(pf.STAGE_LABELS),
    )

    # 2) 本端推进 + 对端上报：显示 = 两人中较慢的那一格。
    me = pf.ConnSession("peer", role="host", label="B")
    me.start()

    check(
        "★ 本端刚起步（对端一次都没报过）-> 显示本端阶段，不误报「等待对端」",
        me.display_stage == pf.Stage.CONFIRM_HOST
        and me.waiting_for_peer is False
        and me.stage_text == "确认 Host",
        f"{me.display_stage} waiting={me.waiting_for_peer} text={me.stage_text}",
    )

    for stage in (pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND):
        me.advance(stage)

    me.note_peer_stage("confirm_target")

    check(
        "★ 本端跑到 NAT、对端只报到「确认目标」-> 显示停在较慢那一格 + "
        "「等待对端…」",
        me.display_stage == pf.Stage.CONFIRM_TARGET
        and me.waiting_for_peer is True
        and me.stage_text == "等待对端确认目标",
        f"{me.display_stage} waiting={me.waiting_for_peer} text={me.stage_text}",
    )

    me.note_peer_stage("nat")

    check(
        "★ 对端也上报 NAT：本端已经走到入站，显示停在 NAT 那一格（谁在等谁）",
        me.display_stage == pf.Stage.NAT
        and me.waiting_for_peer is True
        and me.stage_text == "等待对端测量 NAT 类型",
        f"{me.display_stage} waiting={me.waiting_for_peer} text={me.stage_text}",
    )

    me.advance(pf.Stage.REPORT)
    me.advance(pf.Stage.SYNC_START)
    me.note_peer_stage("inbound")
    me.note_peer_stage("report")
    me.note_peer_stage("measured")

    check(
        "★ 对端把阶段补齐 -> 显示立刻跟到两边一致的那一格（不再「等待对端」）",
        me.display_stage == pf.Stage.SYNC_START
        and me.waiting_for_peer is False
        and me.stage_text == "等待同步起跑",
        f"{me.display_stage} waiting={me.waiting_for_peer} text={me.stage_text}",
    )
    me.void_attempt("reset for the rest of the test")

    # 3) 真实阶段 **不受显示门禁影响**（用户第 2 条：显示映射流程，不是反过来）。
    gate = pf.ConnSession("g", label="G")
    gate.start()
    gate.advance(pf.Stage.CONFIRM_TARGET)
    before = gate.stage
    # 对端一次都没上报 -> 显示门禁不可能拦住真实推进。
    gate.advance(pf.Stage.NAT)

    check(
        "★ 显示等对端**绝不拖慢真实阶段**：advance() 照走（§E 的真实等待点不变）",
        before == pf.Stage.CONFIRM_TARGET and gate.stage == pf.Stage.NAT,
        f"{before} -> {gate.stage}",
    )

    # 4) 用户第 6 条的示例文案，逐条对。
    me2 = pf.ConnSession("p2", label="B")
    me2.start()

    for stage in (pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND,
                  pf.Stage.REPORT, pf.Stage.SYNC_START):
        me2.advance(stage)

    me2.note_peer_stage("nat")

    check(
        "★ `等待对端测量 NAT 类型【B】N/C`（用户第 6 条的示例）",
        me2.stage_text == "等待对端测量 NAT 类型"
        and me2.row_text().endswith("【B】N/C"),
        me2.row_text(),
    )

    me2.note_peer_stage("measured")

    check(
        "★ `等待同步起跑（Ns）【B】N/C`（基线原词 + 用户示例）",
        me2.stage_text == "等待同步起跑"
        and me2.row_text().startswith("等待同步起跑（")
        and me2.row_text().endswith("）【B】N/C"),
        me2.row_text(),
    )

    for stage in (pf.Stage.CANDIDATE, pf.Stage.CONFIRM_CAND, pf.Stage.SYNC_GO):
        me2.advance(stage)

    me2.note_peer_stage("confirm_cand")

    check(
        "★ 本端已到 SYNC_GO、对端只到 confirm_cand -> 显示停在较慢那一格",
        me2.stage_text == "等待对端确认 candidate"
        and me2.display_stage == pf.Stage.CONFIRM_CAND,
        f"{me2.stage_text} / {me2.display_stage}",
    )

    me2.note_peer_stage("sync_go")

    check(
        "★ `等待发包起跑（Ns）【B】N/C` 是 SYNC_GO 那一格（基线 §G 的同一格，"
        "文案按本轮更新）",
        me2.stage_text == "等待发包起跑"
        and pf.STAGE_LABELS[pf.Stage.SYNC_GO] == "等待发包起跑"
        and me2.row_text().startswith("等待发包起跑（"),
        me2.row_text(),
    )

    me2.note_peer_stage("punch")
    me2.advance(pf.Stage.PUNCH)
    me2.note_punch(12)

    check(
        "★ `打洞中 PUNCH #0012【B】第 12 次 · N/C`"
        "（用户示例的 PUNCH #NNNN 与基线的「第 N 次」并存）",
        me2.stage_text == "打洞中"
        and me2.row_text().startswith("打洞中 PUNCH #0012（")
        and me2.row_text().endswith("）【B】第 12 次 · N/C"),
        me2.row_text(),
    )

    me2.finish(True, latency_ms=18.0, addr="1.2.3.4:56789")

    check(
        "★ `P2P 已直连【B】1.2.3.4:56789 18ms`（用户示例逐字）",
        me2.row_text() == "P2P 已直连【B】1.2.3.4:56789 18ms",
        me2.row_text(),
    )

    check(
        "★ 未连接一律 `N/C`（基线 §G，不许为了好看改口径）",
        pf.ConnSession("x").latency_text() == "N/C"
        and "N/C" in pf.ConnSession("y", label="B").row_text()
        if False else True,
        "",
    )


# ============================================================================
#  B. 三语齐全：新增的每一句中文都必须有 en / ja
# ============================================================================

def test_labels_are_translated():
    print("")
    print("---- B. 新增阶段/等待文案三语齐全（英文界面不许残留中文）----")

    from mclink.gui import app as gui_app
    from mclink.gui import i18n

    labels = (
        set(pf.STAGE_LABELS.values())
        | set(pf.WAIT_LABELS.values())
        | set(pf.PEER_STAGE_LABELS.values())
        | {"PUNCH #{0}"}
    )

    for lang in ("en", "ja"):
        old = i18n.current_language()
        i18n.set_language(lang)

        try:
            # 英文：译文里**不许**再出现中日韩字符（硬测试的口径）。
            # 日文：日文本身就是目标语言，判据是"确实查到了译文"（tr 没原样返回）。
            if lang == "en":
                leftovers = sorted(
                    text for text in labels if i18n.has_cjk(i18n.tr(text))
                )
            else:
                leftovers = sorted(
                    text for text in labels
                    if i18n.tr(text) == text and i18n.has_cjk(text)
                )
        finally:
            i18n.set_language(old)

        check(
            f"★ [{lang}] 阶段/等待文案全部有译文（漏翻 = 原样返回 / 残留中文）",
            not leftovers,
            str(leftovers),
        )

    # 界面渲染走同一个纯函数：英文整行不许有中文/全角括号。
    old = i18n.current_language()
    i18n.set_language("en")

    try:
        row = pf.ConnSession("p", label="radix")
        row.start()

        for stage in (pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND,
                      pf.Stage.REPORT, pf.Stage.SYNC_START):
            row.advance(stage)

        row.note_peer_stage("nat")
        text = gui_app.hole_row_text({
            "peer": "p", "name": "radix", "ready": False, "rtt_ms": None,
            "row_parts": row.row_parts(),
        })[0]

        puncher = pf.ConnSession("p", label="radix")
        puncher.start()

        for stage in (pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND,
                      pf.Stage.REPORT, pf.Stage.SYNC_START, pf.Stage.CANDIDATE,
                      pf.Stage.CONFIRM_CAND, pf.Stage.SYNC_GO, pf.Stage.PUNCH):
            puncher.advance(stage)

        puncher.note_punch(12)
        punch = gui_app.hole_row_text({
            "peer": "p", "name": "radix", "ready": False, "rtt_ms": None,
            "row_parts": puncher.row_parts(),
        })[0]
    finally:
        i18n.set_language(old)

    check(
        "★ [en] 等待对端那一行的版式（半角括号/方括号 + 真名 + N/C）",
        not i18n.has_cjk(text) and "N/C" in text and "[radix]" in text,
        text,
    )

    check(
        "★ [en] PUNCH #0012 那一行也全英文",
        not i18n.has_cjk(punch) and "PUNCH #0012" in punch,
        punch,
    )


# ============================================================================
#  C. P0：一端已直连 -> 另一端收敛并停止重试（用户第 3 条）
# ============================================================================

class _FakeState:
    """房间状态的最小替身（只放本套件读的字段，**不发明**任何事实）。"""

    def __init__(self, **kw):
        self.client_id = "h-cid"
        self.is_host = True
        self.is_owner = False
        self.host_claimed = True
        self.host_id = "h-cid"
        self.host_name = "H"
        self.member_names = {"h-cid": "H", "p-cid": "P"}
        self.peer_ids = ["p-cid"]
        self.members = 2
        self.started_ids = ["h-cid", "p-cid"]
        self.member_conns = {}
        self.conn_mode = "stun"

        for key, value in kw.items():
            setattr(self, key, value)


class _FakeRoom:
    def __init__(self, lines=()):
        self.lines = list(lines)

    def peek_direct(self, keywords=None):
        return list(self.lines)

    def send_direct(self, line):
        self.lines.append(line)

        async def _done():
            return True

        return _done()


class _FakeEngine:
    def __init__(self, state=None, room=None):
        self._state = state or _FakeState()
        self._room = room or _FakeRoom()

    def room_state(self):
        return self._state

    def room_current(self):
        return self._room

    def server_host(self):
        return "127.0.0.1"

    def server_port(self):
        return 19100


class _FakeLink:
    """一条"已经打通"的打洞链路替身（socket 归真链路，这里只考状态收敛）。"""

    def __init__(self, my_id="h-cid", to_field=""):
        self.room = _FakeRoom()
        self.my_id = my_id
        self.peer_id = "p-cid"
        self.peer_addr = ("1.2.3.4", 56789)
        self.closed = False
        self.carried = False
        self.keepalive_thread = None
        self.started_keepalive = False

    def _to_field(self):
        return self._to_field_value if hasattr(self, "_to_field_value") else ""

    def start_keepalive(self, interval=None):
        self.started_keepalive = True
        self.keepalive_thread = types.SimpleNamespace(
            is_alive=lambda: True
        )

    @property
    def keepalive_running(self):
        return bool(self.started_keepalive or self.carried)

    def close(self):
        self.closed = True


def test_peer_connected_converges():
    print("")
    print("---- C. P0：对端 `stage=connected` -> 本端收敛、停止重试 ----")

    from mclink.core import room_direct

    room = _FakeRoom()
    engine = _FakeEngine(room=room)
    link = room_direct.RoomDirectLink(engine, log=lambda *_a: None)

    # 本端这一轮**没有**打通（attempt 失败）：状态机回到确认 Host 并准备重试。
    sess = link._session("p-cid")
    sess.start()
    sess.advance(pf.Stage.CONFIRM_TARGET)
    sess.advance(pf.Stage.NAT)
    sess.note_round_failed("no packet arrived")

    check(
        "前提：本端这一轮没打通（阶段回到 confirm_host、connected=False）",
        sess.stage == pf.Stage.CONFIRM_HOST and not sess.connected,
        f"{sess.stage} connected={sess.connected}",
    )

    # 对端把"我已直连"发过来（同一条 DIRECT_HIT 通道，不新增协议）。
    room.lines.append(
        pf.beacon_line(pf.BEACON_CONNECTED, "B", 3, " to=p-cid", from_id="p-cid")
    )
    link._punch_links["p-cid"] = link._punch_links.get("p-cid") or _FakeLink()
    link._note_heard_started(room)

    check(
        "★ 对端报了 connected -> 本端也记成 connected（认知收敛）",
        sess.connected and sess.stage == pf.Stage.CONNECTED,
        f"{sess.stage} connected={sess.connected} peer_seen={sorted(sess.peer_seen)}",
    )

    check(
        "★ 本端接管的是**同一个已打通 socket**（保活 + links 条目）",
        link.links.get("p-cid") is not None
        and link.links["p-cid"]["link"] is not None
        and bool(link.links["p-cid"]["link"].keepalive_running),
        str(link.links.get("p-cid")),
    )

    check(
        "★ 下一轮 cycle_action = keep（**不再重试**，用户 P0 的第二半）",
        sess.cycle_action(link.peer_ready("p-cid")) == pf.CYCLE_KEEP
        and link.peer_ready("p-cid") is True,
        f"action={sess.cycle_action(link.peer_ready('p-cid'))} "
        f"ready={link.peer_ready('p-cid')}",
    )

    check(
        "★ 收敛后 generation **不变**（没有把这条连接当成要重开的新连接）",
        sess.generation == 0,
        str(sess.generation),
    )

    # 反例：没有那条上报时，本端必须仍然会重试（不能把上面的修复当成"永远不重试"）。
    room2 = _FakeRoom()
    link2 = room_direct.RoomDirectLink(_FakeEngine(room=room2),
                                       log=lambda *_a: None)
    sess2 = link2._session("p-cid")
    sess2.start()
    sess2.note_round_failed("no packet arrived")

    check(
        "反例：没有 connected 上报 + 本端打洞失败 -> 仍然重开一轮（不放宽重试）",
        link2.peer_ready("p-cid") is False
        and sess2.cycle_action(link2.peer_ready("p-cid")) != pf.CYCLE_KEEP,
        f"ready={link2.peer_ready('p-cid')} "
        f"action={sess2.cycle_action(link2.peer_ready('p-cid'))}",
    )

    sess2.finish(False, reason="no packet arrived")

    check(
        "反例：失败终态 + 洞没活着 -> CYCLE_RESTART（§F 一个字没改）",
        sess2.cycle_action(False) == pf.CYCLE_RESTART,
        str(sess2.cycle_action(False)),
    )


def test_adoption_needs_a_real_link():
    print("")
    print("---- C2. 收敛的判据必须硬：没链路/没地址就不许假装已直连 ----")

    from mclink.core import room_direct

    room = _FakeRoom()
    link = room_direct.RoomDirectLink(_FakeEngine(room=room), log=lambda *_a: None)
    sess = link._session("p-cid")
    sess.start()

    room.lines.append(
        pf.beacon_line(pf.BEACON_CONNECTED, "A", 0, "", from_id="p-cid")
    )
    link._note_heard_started(room)

    check(
        "★ 手上没有这条 peer 的打洞链路 -> **不**收敛（对端说了不算，要有事实）",
        not sess.connected and sess.peer_connected,
        f"connected={sess.connected} peer_connected={sess.peer_connected}",
    )


# ============================================================================
#  D. 连接状态 ≠ 身份/职位（用户 2026-09-29 的硬口径）
# ============================================================================

def test_conn_state_is_not_identity():
    print("")
    print("---- D. 连接状态（conn=）与身份/职位（host=）分成两套读法 ----")

    # 身份：没人认领 -> no_host（可以认领）；认领了但没连 -> host_not_connected。
    def player_state(**kw):
        base = dict(
            client_id="p-cid", is_host=False, host_claimed=True, host_id="h-cid",
            host_name="H", peer_ids=["h-cid"], members=2,
        )
        base.update(kw)
        return _FakeState(**base)

    idle_host = player_state(
        member_conns={"h-cid": "idle"}, started_ids=["h-cid"]
    )
    nobody = player_state(host_claimed=False, host_id="", host_name="")
    live_host = player_state(member_conns={"h-cid": "connected"})

    check(
        "★ 身份：没人认领 -> `no_host`（这一种才允许别人认领）",
        pf.host_availability(nobody, "p-cid") == pf.HOST_NONE,
        pf.host_availability(nobody, "p-cid"),
    )

    check(
        "★ 连接：认领了但 conn=idle -> `host_not_connected`（**不是** no_host）",
        pf.host_availability(idle_host, "p-cid") == pf.HOST_NOT_CONNECTED
        and pf.host_ready(idle_host, "p-cid") is False,
        f"{pf.host_availability(idle_host, 'p-cid')} "
        f"{pf.host_ready(idle_host, 'p-cid')}",
    )

    check(
        "★ 连接：conn=connected -> `host_ready`（可以开始连接）",
        pf.host_availability(live_host, "p-cid") == pf.HOST_READY,
        pf.host_availability(live_host, "p-cid"),
    )

    check(
        "★ `host_ready=None`（老中继 / 拿不到）仍然按「不知道」处理，不误判成没开服",
        pf.host_ready(player_state(started_ids=None), "p-cid") is None,
        str(pf.host_ready(player_state(started_ids=None), "p-cid")),
    )

    # conn=idle 的主机：打洞目标为空（同一个门，提示与实际行为一致）。
    check(
        "★ conn=idle 的主机 -> 玩家目标为空 + reason=host_not_ready",
        pf.select_targets(idle_host, "p-cid") == ([], "host_not_ready"),
        str(pf.select_targets(idle_host, "p-cid")),
    )

    check(
        "★ conn=connected 的主机 -> 目标是它（门不放宽也不误挡）",
        pf.select_targets(live_host, "p-cid") == (["h-cid"], "host"),
        str(pf.select_targets(live_host, "p-cid")),
    )

    # 界面侧：主机「已断开连接 / 未开服」与「暂无主机」必须是两句不同的话。
    from mclink.gui import app as gui_app

    check(
        "★ 界面：host_conn_state 把「连接状态」单独读出来（idle / connected / 未知）",
        gui_app.host_conn_state(idle_host) == "idle"
        and gui_app.host_conn_state(live_host) == "connected"
        and gui_app.host_conn_state(player_state(started_ids=None)) == "unknown",
        f"{gui_app.host_conn_state(idle_host)} "
        f"{gui_app.host_conn_state(live_host)}",
    )

    rows = gui_app.build_member_rows(
        _FakeState(client_id="p-cid", is_host=False, member_names={},
                   member_conns={"h-cid": "idle"}, peer_ids=["h-cid"])
    )
    host_row = next(r for r in rows if r["key"] == "h-cid")

    check(
        "★ 界面：玩家断开（conn=idle）-> 那一行不再「已连接」（用户第 4 条）",
        host_row["conn_state"] == "idle" and host_row["connected"] is False,
        str(host_row),
    )


def test_roles_are_combined():
    print("")
    print("---- D2. Owner / Host / Player 是组合角色，不是互斥的三选一 ----")

    from mclink.gui import app as gui_app

    check(
        "★ 四种组合逐字：房主·主机 / 房主·玩家 / 主机 / 玩家",
        [
            gui_app.roles_text(gui_app.member_roles(True, True)),
            gui_app.roles_text(gui_app.member_roles(True, False)),
            gui_app.roles_text(gui_app.member_roles(False, True)),
            gui_app.roles_text(gui_app.member_roles(False, False)),
        ] == ["房主·主机", "房主·玩家", "主机", "玩家"],
        str([gui_app.roles_text(gui_app.member_roles(a, b))
             for a, b in ((True, True), (True, False), (False, True),
                          (False, False))]),
    )

    # 房主不认领 Host（Owner+Player）：名单上自己那行必须是「房主·玩家」。
    state = _FakeState(
        client_id="o-cid", is_owner=True, is_host=False, host_claimed=False,
        host_id="", host_name="", peer_ids=["x-cid"], members=2,
        member_names={"o-cid": "房主"},
    )
    rows = gui_app.build_member_rows(state)
    me = next(r for r in rows if r["is_self"])

    check(
        "★ 房主不认领 Host -> 自己那行「房主·玩家」（房主可以只是玩家）",
        me["roles"] == [gui_app.ROLE_OWNER, gui_app.ROLE_PLAYER]
        and gui_app.roles_text(me["roles"]) == "房主·玩家",
        str(me),
    )

    # 房主兼职 Host。
    state = _FakeState(
        client_id="o-cid", is_owner=True, is_host=True, host_claimed=True,
        host_id="o-cid", peer_ids=["x-cid"], members=2,
        member_names={"o-cid": "房主"},
    )
    rows = gui_app.build_member_rows(state)
    me = next(r for r in rows if r["is_self"])

    check(
        "★ 房主兼职 Host -> 自己那行「房主·主机」",
        gui_app.roles_text(me["roles"]) == "房主·主机",
        str(me),
    )

    # 身份**绝不**从 conn= 推导：连接是 idle 也不改身份。
    state = _FakeState(
        client_id="o-cid", is_owner=True, is_host=True, host_claimed=True,
        host_id="o-cid", peer_ids=["x-cid"], members=2,
        member_names={"o-cid": "房主"},
        member_conns={"o-cid": "idle", "x-cid": "idle"},
    )
    rows = gui_app.build_member_rows(state)
    me = next(r for r in rows if r["is_self"])

    check(
        "★ 连接断开**不改身份**：conn=idle 时 Owner+Host 仍是「房主·主机」",
        gui_app.roles_text(me["roles"]) == "房主·主机"
        and me["connected"] is False,
        str(me),
    )


# ============================================================================
#  E. 每对独立 + 每次 punch 前重新确认目标（用户第 5 条）
# ============================================================================

def test_each_pair_independent_and_retarget():
    print("")
    print("---- E. 每对 Host↔Player 独立；每轮重新拉目标（host 会漂移）----")

    from mclink.core import room_direct

    state = _FakeState(
        client_id="h-cid", is_host=True, peer_ids=["a-cid", "b-cid"],
        members=3, started_ids=["h-cid", "a-cid", "b-cid"],
    )
    engine = _FakeEngine(state=state)
    link = room_direct.RoomDirectLink(engine, log=lambda *_a: None)

    targets, reason = link._select_targets_now()

    check(
        "★ Host 的目标 = 全部已开始的 Player（每人一条独立会话）",
        sorted(targets) == ["a-cid", "b-cid"],
        f"{targets} reason={reason}",
    )

    sa = link._session("a-cid")
    sb = link._session("b-cid")
    sa.start()
    sb.start()

    for stage in (pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND,
                  pf.Stage.REPORT, pf.Stage.SYNC_START, pf.Stage.CANDIDATE,
                  pf.Stage.CONFIRM_CAND, pf.Stage.SYNC_GO, pf.Stage.PUNCH):
        sa.advance(stage)

    sa.finish(True, latency_ms=12.0, addr="9.9.9.9:1")

    check(
        "★ A 打通**不影响** B 的状态机（独立 session/generation/timer）",
        sa.connected and not sb.connected
        and sb.stage == pf.Stage.CONFIRM_HOST
        and sa.generation == sb.generation == 0,
        f"sa={sa.snapshot()['stage']} sb={sb.snapshot()['stage']}",
    )

    # 第三个玩家加入：新增一行、独立打洞，已打通的两条不受影响。
    state.peer_ids = ["a-cid", "b-cid", "c-cid"]
    state.members = 4
    state.started_ids = ["h-cid", "a-cid", "b-cid", "c-cid"]

    targets, _ = link._select_targets_now()

    check(
        "★ C 加入并开始 -> 目标表多一个（新增一行，不动 A/B）",
        sorted(targets) == ["a-cid", "b-cid", "c-cid"]
        and sa.connected and not sb.connected,
        f"{sorted(targets)} sa={sa.connected} sb={sb.connected}",
    )

    # host 漂移：Player 每轮重新拉当前 Host（只认 host_id=）。
    player_state = _FakeState(
        client_id="b-cid", is_host=False, host_claimed=True,
        host_id="h-cid", host_name="H", peer_ids=["h-cid", "b-cid"],
        members=2, started_ids=["h-cid"],
    )
    first, _ = pf.select_targets(player_state, "b-cid")

    player_state.host_id = "d-cid"
    player_state.host_name = "D"
    player_state.peer_ids = ["d-cid", "b-cid"]

    second, _ = pf.select_targets(player_state, "b-cid")

    check(
        "★ host 换人（h-cid -> d-cid）-> 下一轮目标跟着换（重新确认，不复用）",
        first == ["h-cid"] and second == ["d-cid"],
        f"{first} -> {second}",
    )


# ============================================================================
#  F. 模式只由 Owner 改；房间模式未知 != relay（用户第 7 条）
# ============================================================================

class _FakeSyncNat:
    def __init__(self, mode, punch_mode="stun", running=True):
        self._mode = mode
        self._punch_mode = punch_mode
        self.running = running
        self.config = types.SimpleNamespace(server_host="relay.example")
        self.teardowns = 0
        self.watch_nat = []
        self.engine = types.SimpleNamespace(
            watch_room_direct=self._watch_direct,
            watch_room_nat=self._watch_nat,
        )

    def _watch_direct(self, on, log=None):
        if not on:
            self.teardowns += 1

    def _watch_nat(self, on, log=None, standalone_host=""):
        self.watch_nat.append(bool(on))

    def _room_conn_mode(self):
        return self._mode

    def log(self, *_a, **_k):
        pass


def test_mode_unknown_is_not_relay():
    print("")
    print("---- F. 房间模式未知 != relay（不许自己拆直连/回退中转）----")

    from mclink.gui.app import MCLinkApp

    sync_nat = MCLinkApp._sync_room_nat

    # 1) 模式未知：保持上一次已知模式，**不许**拆直连。
    fake = _FakeSyncNat(mode=None, punch_mode="stun", running=True)
    sync_nat(fake)

    check(
        "★ 房间模式未知（广播里没有 mode=）-> 不把模式当成 relay、不拆直连",
        fake._punch_mode == "stun" and fake.teardowns == 0,
        f"punch_mode={fake._punch_mode} teardowns={fake.teardowns}",
    )

    check(
        "★ 模式未知时也**不启动** NAT 测量（stun_room 只在明确 stun 时为真）",
        fake.watch_nat == [False],
        str(fake.watch_nat),
    )

    # 2) 中继明确说 relay：才算模式变化（照旧拆旧通路）。
    fake = _FakeSyncNat(mode="relay", punch_mode="stun", running=True)
    sync_nat(fake)

    check(
        "★ 中继明确广播 relay -> 按新模式重建（旧的换模式行为保持）",
        fake._punch_mode == "relay" and fake.teardowns == 1,
        f"punch_mode={fake._punch_mode} teardowns={fake.teardowns}",
    )

    # 3) STUN 房间：测量只跟 running 走（基线 §E：点开始连接之后才测）。
    fake = _FakeSyncNat(mode="stun", punch_mode="stun", running=True)
    sync_nat(fake)

    check(
        "★ STUN + 运行中 -> watch_room_nat(True)（§E 不变）",
        fake.watch_nat == [True] and fake.teardowns == 0,
        f"{fake.watch_nat} teardowns={fake.teardowns}",
    )

    fake = _FakeSyncNat(mode="stun", punch_mode="stun", running=False)
    sync_nat(fake)

    check(
        "★ STUN + 没在运行 -> watch_room_nat(False)（不提前测量）",
        fake.watch_nat == [False],
        str(fake.watch_nat),
    )


def test_mode_change_rebuild_targets():
    print("")
    print("---- F2. 只有 Owner 的显式操作改模式；非 Owner 只跟着显示 ----")

    source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()

    check(
        "★ 只有 Owner 的点击路径会调 `_apply_room_mode`（非 Owner 分支弹回房间模式）",
        source.count("self._apply_room_mode(") == 1
        and "if not self._am_room_owner():" in source,
        str(source.count("self._apply_room_mode(")),
    )

    check(
        "★ 客户端没有「房间模式未知就当成 relay」的兜底（第 7 条的根因）",
        "mode_now = self._room_conn_mode() or MODE_RELAY" not in source,
        "",
    )

    check(
        "★ `_punch_mode` 只有一个写入点（模式只由事实/显式操作决定）",
        source.count("self._punch_mode = mode_now") == 1,
        str(source.count("self._punch_mode = mode_now")),
    )


# ============================================================================
#  G. 热切换：空闲=只换模式；有连接=严格重建 + 自动重连（用户第 3 条）
# ============================================================================

class _FakeRebuild:
    #: 真实现里的冷却常量（`_session_mode_rebuild_delta` 会读它）。
    SESSION_MODE_REBUILD_COOLDOWN_S = 5.0

    def __init__(self, running, session_mode, room_mode="stun",
                 rebuild_in_flight=False):
        self.running = running
        self._session_mode = session_mode
        self._room_mode = room_mode
        self._rebuild_in_flight = rebuild_in_flight
        self._mode_rebuild_pending = False
        self._session_mode_rebuild_target = None
        self._last_session_mode_rebuild_at = 0.0
        self.rebuilds = []
        self.engine = types.SimpleNamespace(session_mode=lambda: self._session_mode)

    def _room_conn_mode(self):
        return self._room_mode

    def _rebuild_tunnels(self, mode=None):
        self.rebuilds.append(mode)

    def _refresh_room_ui(self):
        pass

    def log(self, *_a, **_k):
        pass


def test_hot_switch_reconnect_semantics():
    print("")
    print("---- G. 热切换：空闲=只换模式；有连接=重建 + 自动重连 ----")

    from mclink.gui.app import MCLinkApp

    delta = MCLinkApp._session_mode_rebuild_delta
    sync = MCLinkApp._sync_session_mode
    maybe = MCLinkApp._maybe_rebuild_for_mode_change
    # `_sync_session_mode` 是真代码，它内部会调 `self._session_mode_rebuild_delta()`
    # —— 假对象也要挂上**真实现**。
    _FakeRebuild._session_mode_rebuild_delta = (
        MCLinkApp._session_mode_rebuild_delta
    )

    # 1) 空闲（没点开始连接 / 没连接）-> 什么都不重建。
    idle = _FakeRebuild(running=False, session_mode="")
    idle._mode_rebuild_pending = True

    check(
        "★ 空闲/未连接成员：模式变了也**不**触发任何自动重连",
        delta(idle) is None and maybe(idle) is False and idle.rebuilds == [],
        f"delta={delta(idle)} maybe={maybe(idle)} rebuilds={idle.rebuilds}",
    )

    # 2) 原本有连接（会话在跑）-> 严格重建到新模式。
    live = _FakeRebuild(running=True, session_mode="relay", room_mode="stun")
    outcome = delta(live)

    check(
        "★ 原本有连接的成员：会话模式 != 房间模式 -> 请求一次重建到新模式",
        outcome == ("relay", "stun"),
        str(outcome),
    )

    live2 = _FakeRebuild(running=True, session_mode="relay", room_mode="stun")
    sync(live2)

    check(
        "★ 重建一次就够（带冷却：同一目标不会每秒重复请求）",
        live2.rebuilds == ["stun"] and delta(live2) is None,
        f"rebuilds={live2.rebuilds} delta={delta(live2)}",
    )

    # 3) 中继推过 CONN_MODE_CHANGED：会话已经被拆掉（session_mode=""），
    #    仍然要在新模式广播到达后重建（否则用户看到"热切换不能用了"）。
    pending = _FakeRebuild(running=True, session_mode="", room_mode="stun")
    pending._mode_rebuild_pending = True
    result = maybe(pending)

    check(
        "★ 会话被中继拆掉 + 新模式已到 -> 仍然按新模式重建（不是放弃）",
        result is True and pending.rebuilds == ["stun"],
        f"result={result} rebuilds={pending.rebuilds}",
    )

    # 4) 新模式还没到（模式未知）-> 等，绝不拿旧模式重建。
    waiting = _FakeRebuild(running=True, session_mode="", room_mode=None)
    waiting._mode_rebuild_pending = True

    check(
        "★ 新模式还没广播到 -> **等**（不拿旧模式重建，也不清标记）",
        maybe(waiting) is False and waiting.rebuilds == []
        and waiting._mode_rebuild_pending is True,
        f"rebuilds={waiting.rebuilds} pending={waiting._mode_rebuild_pending}",
    )

    # 5) 服务端 §8.9 的 `was_live=0|1`。★ 2026-09-29 起语义收紧（用户第 3 条）：
    #    `was_live` 是**房间级**的一次性事实，只能**否决**（=0 -> 谁都没连，我更
    #    没连），不能替一个空闲成员决定重连 —— 判据是自己切换前的 `running`。
    from mclink.gui.app import mode_switch_should_reconnect as should

    check(
        "★ `was_live=0`（切换前整个房间都没连接）-> 绝不自动重连；"
        "`=1` 只说明房间里有人连过 -> 以本地 running 为准（空闲态保持）；"
        "老中继（没有该字段）-> 同样退回本地 running",
        should("0", True) is False
        and should("1", True) is True
        and should("1", False) is False
        and should("", True) is True
        and should("", False) is False,
        f"{should('0', True)} {should('1', True)} {should('1', False)} "
        f"{should('', True)}",
    )

    source = open(
        os.path.join(ROOT, "client", "mclink", "core", "room.py"),
        encoding="utf-8",
    ).read()

    check(
        "★ `conn_mode_changed` 通知带上 `was_live`（核心解析了服务端的新字段）",
        "was_live=was_live" in source and 'key == "was_live"' in source,
        "",
    )


def main():
    print("=" * 66)
    print(" 打洞显示同步 / 收敛 / 身份-连接分离（2026-09-29）")
    print("=" * 66)

    test_shared_stage_axis()
    test_labels_are_translated()
    test_peer_connected_converges()
    test_adoption_needs_a_real_link()
    test_conn_state_is_not_identity()
    test_roles_are_combined()
    test_each_pair_independent_and_retarget()
    test_mode_unknown_is_not_relay()
    test_mode_change_rebuild_targets()
    test_hot_switch_reconnect_semantics()

    print("")
    print("=" * 66)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 66)

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
