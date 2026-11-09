"""
CraftLink —— 用户 2026-09-29 那一轮实测的 7 条问题里**可以离线钉死**的部分。

这一套是"先红后绿"的回归网，按用户逐条报的现象组织（每条测试名里带第几条）。
它**不连网络**：全部用替身对象 + 真模块，跑得快、不占端口。

覆盖：

  * 第 1 条：Host 已连接、对端只是 idle（没点开始连接）时，Host 不许把 §E 走到
    「等待同步起跑」——`conn=idle` 一票否决目标表，陈旧状态机也不许进显示。
  * 第 2 条：被踢的玩家重连；① 服务端 `HOST_STATE` 不许抢在 `OK JOIN` 前面写到
    那条新连接上（客户端会把它当握手应答 -> 自动重连整条失败）；
    ② 客户端 `peer_ready` 必须认"已经被隧道接手的洞"，否则打通后 3 秒又
    `new connection cycle`、界面退回「等待同步起跑」，而连接其实是通的。
  * 第 3 条：热切换"空闲态的保持"——`was_live=1` 只说明**房间里**有人连过，
    不能替一个空闲成员决定重连；自动重连窗口内按钮不许被释放。
  * 第 4 条：语言 / 主题下拉框的滚轮不许改选项（静态断言绑定存在）。
  * 第 5 条：房间人数必须和客户端能画出来的花名册行数一致
    （同一 client_id 去重 + 房间里成员的游戏会话只算一次）。
  * 第 7 条：踢人之后打洞进度的分母必须下降（陈旧状态机被清掉）。

Run:
    python tests/hotswitch_roster_state_test.py
"""

import asyncio
import contextlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "server"))

from mclink.core import punch_flow as pf  # noqa: E402
from mclink.core import room_direct as rd  # noqa: E402

import room_members  # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================================
#  替身
# ============================================================================

class FakeState:
    """`RoomState` 的最小替身（字段名与中继广播解析出来的完全一致）。"""

    def __init__(self, client_id="h-cid", peer_ids=("p1-cid",), members=2,
                 is_host=True, started_ids=None, host_id="h-cid",
                 host_name="host", self_name="host", member_names=None,
                 member_conns=None):
        self.client_id = client_id
        self.peer_ids = list(peer_ids) if peer_ids is not None else None
        self.members = members
        self.is_host = is_host
        self.started_ids = started_ids
        self.host_claimed = True
        self.host_id = host_id
        self.host_name = host_name
        self.self_name = self_name
        self.member_names = member_names or {p: p for p in (peer_ids or ())}
        #: `conn=`（PROTOCOL §8.5）。None = 老中继没有这个 token（未知）。
        self.member_conns = member_conns


class FakeRoom:
    def __init__(self):
        self.state = None


class FakeEngine:
    def __init__(self, state):
        self._state = state

    def room_state(self):
        return self._state

    def room_current(self):
        return FakeRoom()

    def server_host(self):
        return "127.0.0.1"

    def server_port(self):
        return 19100

    def loop(self):
        return asyncio.get_running_loop()

    def room_nat_profile(self):
        return None


def make_direct(state, log=None):
    logs = log if log is not None else []
    return rd.RoomDirectLink(FakeEngine(state), log=logs.append), logs


# ============================================================================
#  第 1 条：idle 的对端不许进入目标表 / 显示
# ============================================================================

def test_idle_peer_is_not_a_target():
    print("")
    print("---- 第 1 条：对端在房里但未连接（idle）-> 不是打洞目标 ----")

    check(
        "★ `peer_is_idle`：中继明确说 idle 才算 idle",
        pf.peer_is_idle(FakeState(member_conns={"p1-cid": "idle"}), "p1-cid")
        and not pf.peer_is_idle(
            FakeState(member_conns={"p1-cid": "connected"}), "p1-cid"
        ),
        "idle 判定",
    )
    check(
        "★ 老中继没有 `conn=` -> **未知**，绝不等于 idle",
        not pf.peer_is_idle(FakeState(member_conns=None), "p1-cid"),
        "member_conns=None",
    )
    check(
        "★ 广播里没有这个人 -> 也是未知（不是 idle）",
        not pf.peer_is_idle(FakeState(member_conns={}), "p1-cid"),
        "member_conns={}",
    )

    async def scenario():
        # Host 侧：中继的 `started=` 只包含自己（一个目标都没有），而对端曾经
        # 发过 DIRECT_HIT（`_heard_started`），现在 conn=idle。
        state = FakeState(
            client_id="h-cid",
            peer_ids=("p1-cid", "p2-cid"),
            members=3,
            is_host=True,
            started_ids=("h-cid",),
            host_id="h-cid",
            member_conns={
                "h-cid": "connected",
                "p1-cid": "idle",
                "p2-cid": "waiting",
            },
        )
        direct, _logs = make_direct(state)
        direct._heard_started = {"p1-cid", "p2-cid"}

        targets, reason = direct._select_targets_now()

        check(
            "★ idle 的对端被排除（曾经发过信标也不行）",
            targets == ["p2-cid"],
            f"targets={targets} reason={reason}",
        )
        check(
            "★ `waiting`（连接在动）的对端仍然是目标",
            "p2-cid" in targets,
            f"targets={targets}",
        )

        # 纯粹性：`_select_targets_now` 只读（隐患 6）。
        before = set(direct.sessions)
        direct._select_targets_now()
        check(
            "★ `_select_targets_now` 仍然是纯函数（不建/不改状态机）",
            set(direct.sessions) == before,
            f"{sorted(direct.sessions)}",
        )

        # 陈旧状态机：给 idle 的对端造一条走到 SYNC_START 的状态机。
        sess = direct._session("p1-cid")
        sess.stage_for(pf.Stage.SYNC_START)
        direct._sweep_links()

        check(
            "★ idle 对端的陈旧状态机被清掉（那一行不再显示「等待同步起跑」）",
            "p1-cid" not in direct.sessions,
            f"sessions={sorted(direct.sessions)}",
        )

        # ============================================================
        #  反例（P0）：正在打洞、但还没开游戏连接的玩家 `conn` 就是 idle ——
        #  他**必须**仍然是目标，否则 Host 一个目标都拉不到（stun 模式下打洞
        #  先于游戏连接）。判据是"信标还新鲜"。
        # ============================================================
        state.member_conns = {
            "h-cid": "connected",
            "p1-cid": "idle",
            "p2-cid": "idle",
        }
        state.started_ids = ["h-cid"]
        direct._heard_started = {"p1-cid"}
        direct._heard_at = {"p1-cid": time.monotonic()}

        fresh, _reason = direct._select_targets_now()

        check(
            "★★ `conn=idle` + **信标新鲜** -> 仍然是目标（不许误伤正在打洞的人）",
            fresh == ["p1-cid"],
            f"targets={fresh}",
        )

        # 信标停了（> HEARD_FRESH_S）-> 不再打他。
        direct._heard_at = {
            "p1-cid": time.monotonic() - (rd.HEARD_FRESH_S + 5.0)
        }

        stale, _reason = direct._select_targets_now()

        check(
            "★★ `conn=idle` + 信标已经停了 -> 移出目标表（第 1 条的根因）",
            stale == [],
            f"targets={stale}",
        )

    asyncio.run(scenario())


# ============================================================================
#  第 7 条：离开房间的对端，陈旧状态机必须被清（打洞进度分母跟着降）
# ============================================================================

def test_stale_sessions_are_pruned():
    print("")
    print("---- 第 7 条：踢人之后打洞进度的分母必须下降 ----")

    async def scenario():
        state = FakeState(
            client_id="h-cid",
            peer_ids=("p1-cid",),
            members=2,
            is_host=True,
            started_ids=("h-cid", "p1-cid"),
            host_id="h-cid",
            member_conns={"h-cid": "connected", "p1-cid": "pairing"},
        )
        direct, logs = make_direct(state)

        # 两个人各自一条状态机：p1 是活目标，p2 是"打了一半就离开房间"的。
        a = direct._session("p1-cid")
        a.stage_for(pf.Stage.PUNCH)
        b = direct._session("p2-cid")
        b.stage_for(pf.Stage.SYNC_START)

        direct._member_ids.update({"p1-cid", "p2-cid"})

        order = direct._session_order()
        check(
            "★ 显示只认当前目标（陈旧 session 不进显示）",
            "p2-cid" not in order,
            f"order={order}",
        )

        # 中继花名册刷新：p2 已经走了。
        state.peer_ids = ["p1-cid"]
        state.member_conns = {"h-cid": "connected", "p1-cid": "pairing"}
        direct._sweep_links()

        check(
            "★ 离开房间的对端：状态机被清（分母从 2 降到 1）",
            "p2-cid" not in direct.sessions and "p1-cid" in direct.sessions,
            f"sessions={sorted(direct.sessions)}",
        )

        summary = direct.flow_summary(punching=True)
        check(
            "★ 打洞进度分母 = 当前目标数（不再是 1/3）",
            summary["total"] == 1,
            f"total={summary['total']} progress={summary['progress']!r}",
        )

        check(
            "★ 清理落了日志（可核对）",
            any("stale connection state" in str(line) for line in logs),
            f"{logs[-3:]}",
        )

    asyncio.run(scenario())


# ============================================================================
#  第 2 条：peer_ready 必须认"已经被隧道接手的洞"
# ============================================================================

def test_peer_ready_accepts_a_carried_link():
    print("")
    print("---- 第 2 条：打通了、隧道也 ready 了，不许 3 秒后误判成断开 ----")

    async def scenario():
        state = FakeState(
            client_id="h-cid",
            peer_ids=("p1-cid",),
            members=2,
            is_host=True,
            started_ids=("h-cid", "p1-cid"),
            host_id="h-cid",
            member_conns={"h-cid": "connected", "p1-cid": "pairing"},
        )
        direct, _logs = make_direct(state)

        class PunchLink:
            """打洞层的账已经"掉了"（保活线程没了、carried=False）。"""

            keepalive_running = False
            closed = False

        class Carrier:
            started = True

            class _Stop:
                def is_set(self):
                    return False

            stop = _Stop()

            def idle_for(self):
                return 0.0

        direct.links["p1-cid"] = {
            "link": PunchLink(),
            "addr": ("5.6.7.8", 6000),
            "ready_at": None,
        }
        direct._carriers["p1-cid"] = Carrier()

        check(
            "★ 隧道在跑 -> `peer_ready` 为真（哪怕打洞层保活账掉了）",
            direct.peer_ready("p1-cid") is True,
            "carried",
        )
        check(
            "★ `transport_ready` 同样认它（`start()` 的幂等闸门不许挡住）",
            direct.transport_ready is True,
            "transport_ready",
        )

        sess = direct._session("p1-cid")
        sess.finish(True)

        check(
            "★★ 成功终态 + 隧道还活着 -> `cycle_action` = KEEP（不开新一轮）",
            sess.cycle_action(direct.peer_ready("p1-cid")) == pf.CYCLE_KEEP,
            str(sess.cycle_action(direct.peer_ready("p1-cid"))),
        )

        # 隧道真的关了 -> 必须能重开（不能被"引用还在"骗住）。
        carrier = direct._carriers["p1-cid"]
        carrier.started = False

        check(
            "★ 隧道停了 -> `peer_ready` 为假，允许重开（洞真死了要重打）",
            direct.peer_ready("p1-cid") is False
            and sess.cycle_action(False) == pf.CYCLE_RESTART,
            f"ready={direct.peer_ready('p1-cid')}",
        )

    asyncio.run(scenario())


# ============================================================================
#  第 3 条：空闲态的保持 + 按钮状态
# ============================================================================

def test_hot_switch_keeps_idle_members_idle():
    print("")
    print("---- 第 3 条：热切换只自动重连【切换前有连接】的成员 ----")

    from mclink.gui.app import (
        MCLinkApp, mode_switch_should_reconnect as should,
    )

    check(
        "★ `was_live=1`（房间里有人连过）**不替我决定**：我空闲就保持空闲",
        should("1", False) is False,
        f"should('1', False)={should('1', False)}",
    )
    check(
        "★ `was_live=1` + 我在连接 -> 自动重连",
        should("1", True) is True,
        f"should('1', True)={should('1', True)}",
    )
    check(
        "★ `was_live=0`（整个房间都没连接）-> 绝不重连（谁都没连，我更没有）",
        should("0", True) is False,
        f"should('0', True)={should('0', True)}",
    )
    check(
        "★ 老中继没有该字段 -> 退回本地 running（旧行为）",
        should("", True) is True and should("", False) is False,
        f"{should('', True)} {should('', False)}",
    )

    # 按钮状态：自动重连窗口里不能被释放。
    busy = MCLinkApp.__new__(MCLinkApp)
    busy.running = False
    busy._reconnect_phase = "reconnecting"
    check(
        "★ 自动重连进行中 -> 界面仍然算【忙碌】（开始连接不许被释放）",
        busy._session_busy() is True,
        "reconnecting",
    )
    busy._reconnect_phase = "running"
    check(
        "★ 重连没在进行且 running=False -> 才算真的停止",
        busy._session_busy() is False,
        "running stopped",
    )
    busy.running = True
    check(
        "★ 会话在跑 -> 忙碌",
        busy._session_busy() is True,
        "running",
    )


# ============================================================================
#  第 4 条：下拉框滚轮
# ============================================================================

def test_combobox_wheel_is_blocked():
    print("")
    print("---- 第 4 条：语言 / 主题下拉框的滚轮不许改选项 ----")

    source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()

    check(
        "★ 存在专门的【滚轮不许改 Combobox】绑定函数",
        "def _block_wheel_select(" in source,
        "missing _block_wheel_select",
    )
    check(
        "★ 语言 / 主题两个下拉框都装上了它",
        source.count("_block_wheel_select(") >= 3,
        f"count={source.count('_block_wheel_select(')}",
    )
    check(
        "★ 控件级绑定 return break（TCombobox 的类绑定才轮不到）",
        'return "break"' in source.split("def _block_wheel_select(")[1][:2000],
        "no break",
    )


# ============================================================================
#  第 5 条：人数必须和花名册行数一致（服务端）
# ============================================================================

class _FakeMember:
    def __init__(self, client_id, username, **kw):
        self.client_id = client_id
        self.username = username
        self.member_key = client_id
        self.ready = True
        self.display_name = username
        self.rtt_ms = None
        self.detached = False
        self.is_owner = False
        self.is_host = False
        self.connecting = False
        self.token = ""
        self.guest_token = ""
        self.removal_reason = ""
        self.closed = False
        for key, value in kw.items():
            setattr(self, key, value)

    @property
    def label(self):
        return self.username or self.client_id

    async def send(self, line):
        self.sent = getattr(self, "sent", [])
        self.sent.append(line)


class _FakeRoom(room_members.MembershipMixin):
    """`room_members` 用到的最小房间替身。

    继承真 `MembershipMixin`：`host_member` / `host_state` / `host_client_id`
    这些只读查询直接用生产代码，替身只补"房间"那一层的字段。
    """

    def __init__(self, members):
        self.name = "r"
        self.members = {m.member_key: m for m in members}
        self.conn_mode = "stun"
        self.conn_mode_reason = "owner"
        self.conn_mode_was_live = None
        self.closed = False
        self.host_id = ""
        self.host_username = ""
        self.host_writer = None
        self.waiting_tunnels = []
        self.waiting_players = []
        self.sessions = {}
        self.lock = asyncio.Lock()

    def mark_dirty(self):
        pass

    def room_capacity(self):
        return 8

    def world_player_count(self):
        return 0


def test_roster_count_matches_rows():
    print("")
    print("---- 第 5 条：人数 == 客户端能画出来的行数 ----")

    a = _FakeMember("cidA", "radix")
    b = _FakeMember("cidB", "root")
    # 同一个 client_id 的两条记录（防篡夺分支留下的形状）：
    dup = _FakeMember("cidB", "root2", member_key="cidB:root2")

    room = _FakeRoom([a, b, dup])

    check(
        "★ `_roster_members` 按 client_id 去重",
        [m.client_id for m in room_members._roster_members(room)]
        == ["cidA", "cidB"],
        f"{[m.client_id for m in room_members._roster_members(room)]}",
    )
    check(
        "★ `roster_count` = 去重后人数（不是字典长度 3）",
        room_members.roster_count(room) == 2,
        f"count={room_members.roster_count(room)} len={len(room.members)}",
    )

    async def scenario():
        await room_members.broadcast_member_state(room)

        lines = getattr(a, "sent", [])

        check("★ 广播真的发出去了", bool(lines), f"{lines}")

        if lines:
            line = lines[0]
            members = [t for t in line.split() if t.startswith("members=")]
            peers = [t for t in line.split() if t.startswith("peers=")]

            check(
                "★ `members=` 与 `peers=` 自洽（1 + len(peers)）",
                members
                and members[0] == f"members={1 + len(peers[0][6:].split(','))}",
                f"{members} {peers}",
            )

    asyncio.run(scenario())


def test_room_snapshot_counts_a_member_once():
    print("")
    print("---- 第 5 条：成员的游戏会话不许把人数 +1（游客座位名 vs 游戏名）----")

    import relay_server as rs

    async def scenario():
        room = rs.Room("snaproom")

        class _W:
            def close(self):
                pass

            async def drain(self):
                pass

        owner = room_members.RoomMember("snaproom", None, _W(),
                                        client_id="cidA", username="radix")
        guest = room_members.RoomMember("snaproom", None, _W(),
                                        client_id="cidB",
                                        username="游客2c12wl")
        room.add_member(owner)
        room.add_member(guest)

        # 游客的 PLAYER 握手报的是客户端表单名（和座位名不同）——
        # 旧公式 `len(member_names | world_names)` 会把他数成两个人。
        pair = rs.Pair("s1", object(), object(), object(), object())
        pair.player_id = "cidB"
        pair.player_username = "lubenwey"
        room.sessions["s1"] = pair

        snap = await room.snapshot()

        check(
            "★ 成员的游戏会话不重复计数（2 人，不是 3 人）",
            snap.get("members") == 2,
            f"members={snap.get('members')}",
        )

        # 真正"直接进世界、没有房间成员"的连接仍然要算上。
        stray = rs.Pair("s2", object(), object(), object(), object())
        stray.player_id = "cidZ"
        stray.player_username = "stray"
        room.sessions["s2"] = stray

        snap = await room.snapshot()

        check(
            "★ 没有成员记录的直连玩家仍然算进「房间内在线」（老初衷不丢）",
            snap.get("members") == 3,
            f"members={snap.get('members')}",
        )

    asyncio.run(scenario())


# ============================================================================
#  第 2 / 7 条：服务端 —— OK 回包之前不许广播 / 踢人立刻结束座位宽限
# ============================================================================

def test_broadcast_skips_members_without_their_ok():
    print("")
    print("---- 第 2 条：还没收到自己的 OK 回包的连接，一条广播都不许推 ----")

    async def scenario():
        ready = _FakeMember("cidA", "radix")
        joining = _FakeMember("cidB", "root", ready=False)
        room = _FakeRoom([ready, joining])

        await room_members.broadcast_member_state(room)

        check(
            "★ 已就绪的成员收到广播",
            bool(getattr(ready, "sent", [])),
            f"{getattr(ready, 'sent', [])}",
        )
        check(
            "★ 还没拿到 OK JOIN 的成员**一条都没收到**（否则它会把 HOST_STATE "
            "当成握手应答，自动重连整条失败）",
            not getattr(joining, "sent", []),
            f"{getattr(joining, 'sent', [])}",
        )

        joining.ready = True
        await room_members.broadcast_member_state(room)

        check(
            "★ 打开 ready 之后立刻能收到（正常广播不受影响）",
            len(getattr(joining, "sent", [])) == 1,
            f"{getattr(joining, 'sent', [])}",
        )

    asyncio.run(scenario())


def test_kick_cancels_the_seat_grace():
    print("")
    print("---- 第 7 条：宽限期内被踢 -> 立刻结束宽限（不许替他留座继续打洞）----")

    async def scenario():
        member = _FakeMember("cidB", "游客2c12wl", detached=True)
        room = _FakeRoom([member])

        task = asyncio.ensure_future(
            room_members.hold_member_seat(room, member, grace=30.0)
        )

        await asyncio.sleep(0.6)

        check(
            "★ 没人踢他时仍在宽限里等",
            not task.done(),
            f"done={task.done()}",
        )

        # 管理员踢人（kick_member 就是这两件事）。
        member.removal_reason = "kick"
        member.closed = True

        handed_over = await asyncio.wait_for(task, timeout=3.0)

        check(
            "★★ 被踢之后立刻结束宽限（返回 False = 走真正的移除路径）",
            handed_over is False,
            f"handed_over={handed_over}",
        )
        check(
            "★ 成员仍然是 detached=False 的干净状态（调用方要能继续清理）",
            member.detached is False,
            f"detached={member.detached}",
        )

    asyncio.run(scenario())


def test_kick_reason_is_not_rewritten_as_timeout():
    print("")
    print("---- 第 7 条：被踢的原因不许被改写成 timeout（重连探测靠它分流）----")

    source = open(
        os.path.join(ROOT, "server", "relay_server.py"), encoding="utf-8"
    ).read()

    check(
        "★ 宽限结束后按**最新** removal_reason 定原因（kick 不被写成 timeout）",
        'latest = str(getattr(member, "removal_reason", "") or "")' in source
        and 'latest if latest not in ("", "drop") else "timeout"' in source,
        "relay_server finally",
    )


def test_guest_reconnect_by_token_is_the_same_person():
    print("")
    print("---- 第 5 条：同一个 client_id + 同一个凭证 = 本人重连（不许多出一条记录）----")

    async def scenario():
        room = _FakeRoom([])

        first = _FakeMember("cidB", "游客aaaaaa")
        first.guest_token = "gtok-1"
        first.token = ""

        handed = room_members.RoomMember(
            "r", None, None, client_id="cidB", username="游客bbbbbb", token="gtok-1"
        )
        handed.guest_token = "gtok-1"

        room.members["cidB"] = first
        room_members.MembershipMixin.add_member(room, handed)

        check(
            "★ 凭证相同 -> 座位交接，成员数不变（不会 4 人显示 5 人）",
            len(room.members) == 1 and room.members.get("cidB") is handed,
            f"members={list(room.members)}",
        )

        # 反例：拿不出凭证的不同人 -> 仍然并存（防篡夺一个字没改）。
        other = room_members.RoomMember(
            "r", None, None, client_id="cidB", username="别人", token=""
        )
        room_members.MembershipMixin.add_member(room, other)

        check(
            "★ 不同凭证 -> 仍然是两个人（防篡夺不变）",
            len(room.members) == 2,
            f"members={list(room.members)}",
        )

    asyncio.run(scenario())


def test_reconnect_wiring_is_not_dead_code():
    print("")
    print("---- 第 3 条：自动重连的三个接线点（顺序 / 成功恢复 / 让路重试）----")

    source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()

    # (1) 掉线处理必须先起重连、再清 running（闸门看的就是 running）。
    marker = "the room connection dropped while the session was live"
    room_lost = source.split(marker, 1)[1][:1200] if marker in source else ""
    trigger_at = room_lost.find("self._trigger_auto_reconnect(")
    clear_at = room_lost.find("self.running = False")

    check(
        "(1) 普通掉线：先 `_trigger_auto_reconnect()` 再清 `running`"
        "（以前顺序反了 -> 整条自动重连是死代码）",
        trigger_at >= 0 and clear_at >= 0 and trigger_at < clear_at,
        f"trigger@{trigger_at} clear@{clear_at}",
    )

    # (2) 重建成功必须把界面恢复成"在连接"。
    check(
        "(2) `rebuild_done` 里会调 `_reconnect_succeeded()`（按钮 / 阶段复位）",
        'kind == "rebuild_done"' in source
        and "self._reconnect_succeeded()" in source,
        "rebuild_done wiring",
    )

    # (3) 撞上"另一次重建还在跑"时，让路重试而不是静默放弃。
    check(
        "(3) `_reconnect_session()` 遇到 `_rebuild_in_flight` 会让路重试（有界）",
        "RECONNECT_DEFER_LIMIT" in source and "retrying in 0.5s" in source,
        "defer wiring",
    )

    # ★ 2026-10-04：按钮判据仍是 `_session_busy()`（不是裸 running），但多了一层
    #   "host 已经走了（host_down / stopped）就释放"的覆盖 —— 回环测试抓到的现场：
    #   host 停服时，玩家的后台重建还在跑，按钮却因为"忙"而没释放。
    check(
        "(4) 自动重连/重建窗口内按钮按 `_session_busy()` 判定"
        "（且 host_down/stopped 时释放）",
        "_busy = self._session_busy() and (" in source
        and 'not in ("host_down", "stopped")' in source
        and "enable(self.start_button, not _busy)" in source
        and "enable(self.stop_button, bool(_busy))" in source,
        "button wiring",
    )


# ============================================================================

def main():
    print("=" * 70)
    print(" 用户 2026-09-29 实测 7 条问题的离线回归网")
    print("=" * 70)

    test_idle_peer_is_not_a_target()
    test_stale_sessions_are_pruned()
    test_peer_ready_accepts_a_carried_link()
    test_hot_switch_keeps_idle_members_idle()
    test_combobox_wheel_is_blocked()
    test_roster_count_matches_rows()
    test_room_snapshot_counts_a_member_once()
    test_broadcast_skips_members_without_their_ok()
    test_kick_cancels_the_seat_grace()
    test_kick_reason_is_not_rewritten_as_timeout()
    test_guest_reconnect_by_token_is_the_same_person()
    test_reconnect_wiring_is_not_dead_code()

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
