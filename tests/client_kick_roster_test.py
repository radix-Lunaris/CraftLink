"""
MC-Link -- 房间成员列表 + 踢人（客户端半边）回归测试

============================================================================
 这一套测什么（客户端，不碰服务端/面板）
============================================================================

服务端的权限矩阵已经在 `tests/kick_test.py` 里钉死了（真中继 + 裸协议连接）。
这一套补的是**客户端这一半**：

  1. **名单数据**（`gui/app.build_member_rows`，纯函数）：
     从 RoomState + 每人一条的直连状态拼出「用户名/身份/连接状态/NAT 类型/
     入站档位/IP:端口/延迟/对端延迟」；拿不到的一律留空（界面显示 -），不许编。
     用户名不再是「未知」：中继广播 `names=` 就用真名，老中继回退到 client_id
     （见 tests/member_name_rtt_test.py 的完整覆盖）。

  2. **踢人按钮可见性**（权限矩阵的显示层）：
       房主（owner）        -> 除自己外都能踢
       当前主机（非 owner） -> 不能踢房主，其他都能踢
       普通玩家             -> 一个按钮都不显示
     真正能不能踢由服务端判定（第 3 部分用真中继验证）。

  3. **真中继 + 真客户端**：用 `RoomSession`（客户端真正跑的那套代码）建房/进房，
     跑一遍 KICK 的五种应答：OK / OWNER_LEFT_ROOM（解析层）/
     KICK_FORBIDDEN / KICK_NO_SUCH_MEMBER / KICK_SELF，并验证：
       * 被踢的人真的收到 `KICKED by=... reason=...`（客户端事件）；
       * 被踢的人连接被关掉，而**其他人不受影响**（还能 PING/PONG）；
       * 本机延迟来自 PING/PONG 回声（真实数字，不是编的）。

  4. **被踢 = 本地拆干净**（真界面）：`kicked` 事件走 `_teardown_room_locally`，
     弹一句中文告知，所有房间状态复位 —— 和"管理关房"同一条路径，不留幽灵态。

  5. **一人一行**（`RoomDirectLink.state()["links"]` + 运行状态卡片）：
     来一个对端多一行、走一个少一行，**绝不出现空行**；行控件复用（不闪）。

Run:
    python tests/client_kick_roster_test.py
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "server"))

from mclink.core import protocol as proto  # noqa: E402
from mclink.core.room import RoomSession, RoomState  # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_CLIENTKICK_PORT", "19186"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    (PASSED if ok else FAILED).append(name)


# ============================================================
# 1. 名单数据（纯函数，不需要图形环境）
# ============================================================

def _state(**kw):
    """一个和 RoomState 同形状的假房间状态。"""
    state = RoomState()
    state.room = kw.pop("room", "r1")
    state.room_name = kw.pop("room_name", "测试房")
    state.client_id = kw.pop("client_id", "cid-me")
    state.username = kw.pop("username", "我")
    state.is_owner = kw.pop("is_owner", False)
    state.is_host = kw.pop("is_host", False)
    state.host_claimed = kw.pop("host_claimed", False)
    state.host_name = kw.pop("host_name", "")
    state.members = kw.pop("members", 1)
    state.peer_ids = list(kw.pop("peer_ids", []) or [])
    state.world_players = kw.pop("world_players", 0)

    for key, value in kw.items():
        setattr(state, key, value)

    return state


def _row(rows, key):
    for row in rows:
        if row.get("key") == key:
            return row

    return None


def test_roster_builder():
    print("")
    print("---- 名单数据（未知就留空，不编）----")

    import importlib

    gui_app = importlib.import_module("mclink.gui.app")

    # ---- 没有房间 -> 空名单 ----
    check("不在房间里 -> 名单为空", gui_app.build_member_rows(None) == [])

    # ---- 房主自己 + 两个对端 ----
    state = _state(
        client_id="cid-owner", username="房主本人",
        is_owner=True, members=3, peer_ids=["cid-a", "cid-b"],
    )
    rows = gui_app.build_member_rows(state)

    check("房间内 3 人 -> 3 行（自己 + 每个对端一行）", len(rows) == 3,
          str([r["key"] for r in rows]))

    me = _row(rows, "cid-owner")
    check("自己那行用的是真用户名", me and me["name"] == "房主本人", str(me))
    check("自己那行的身份 = 房主", me and me["role"] == gui_app.ROLE_OWNER)
    check("自己永远不显示踢出按钮", me and me["can_kick"] is False)

    peer = _row(rows, "cid-a")
    check("★ 老中继没广播名字 -> 回退到 client_id（不是「未知」、不是空）",
          peer is not None and peer["name"] == "cid-a", str(peer))
    check("对端没有直连数据时 NAT/入站/IP/延迟 全留空（显示 -）",
          peer is not None
          and peer["nat"] == "" and peer["inbound"] == ""
          and peer["addr"] == "" and peer["ping_ms"] is None,
          str(peer))
    check("没打通时对端延迟不适用（peer_direct=False -> 渲染层 N/C）",
          peer is not None and peer["peer_direct"] is False
          and peer["peer_rtt_ms"] is None,
          str(peer))
    check("★ 房主可以踢所有人（两个对端都有按钮）",
          all(_row(rows, key)["can_kick"] for key in ("cid-a", "cid-b")),
          str([(r["key"], r["can_kick"]) for r in rows]))

    # ---- 当前主机（非房主）：不能踢房主 ----
    #
    # 2026-09-28（AGENTS.md 规则 6 §A）：Owner 是谁**只认中继的 `owner_id=`**，
    # 不许再"花名册第一个就是房主"。老中继没有这个 token 时**谁都不标房主**
    # （标错 = 用户看到的"host 继承了房主身份"，BUG 2 的客户端那一半）。
    state = _state(
        client_id="cid-host", username="主机本人",
        is_host=True, is_owner=False, host_claimed=True, host_name="主机本人",
        members=3, peer_ids=["cid-owner", "cid-d"],
        owner_id="cid-owner",
    )
    rows = gui_app.build_member_rows(state)

    owner_row = _row(rows, "cid-owner")
    other = _row(rows, "cid-d")

    check("★ 中继给了 owner_id -> 那且只有那一行是房主（不是靠顺序猜）",
          owner_row is not None and owner_row["role"] == gui_app.ROLE_OWNER
          and other is not None and other["role"] != gui_app.ROLE_OWNER,
          f"{owner_row} {other}")
    check("★ 当前主机不能踢房主（那一行没有按钮）",
          owner_row is not None and owner_row["can_kick"] is False)
    check("★ 当前主机可以踢其他非房主成员",
          other is not None and other["can_kick"] is True, str(other))

    # ---- 老中继没有 owner_id：**不许猜**（BUG 2 的客户端那一半）----
    state = _state(
        client_id="cid-host2", username="主机本人",
        is_host=True, is_owner=False, host_claimed=True, host_name="主机本人",
        members=3, peer_ids=["cid-x", "cid-y"],
    )
    rows = gui_app.build_member_rows(state)

    check(
        "★ owner_id 未知 -> 一个对端都不许标「房主」（顺序不是身份）",
        all(
            _row(rows, key)["role"] != gui_app.ROLE_OWNER
            for key in ("cid-x", "cid-y")
        ),
        str([(r["key"], r["role"]) for r in rows]),
    )

    # ---- 普通玩家：一个都不许踢 ----
    state = _state(
        client_id="cid-p", username="路人",
        is_host=False, is_owner=False, host_claimed=True, host_name="某人",
        members=3, peer_ids=["cid-owner", "cid-host2"],
    )
    rows = gui_app.build_member_rows(state)

    check("★ 普通玩家没有任何踢人按钮（身份都是未知也照样不给）",
          all(r["can_kick"] is False for r in rows
              if not r["is_self"]),
          str([(r["key"], r["role"], r["can_kick"]) for r in rows]))

    # ---- 对端直连数据（DIRECT_CAND 的 sub=/filt=/地址）----
    from mclink.core import punch as punch_mod

    state = _state(
        client_id="cid-owner", username="房主",
        is_owner=True, members=2, peer_ids=["cid-x"],
    )
    direct = [{
        "peer": "cid-x",
        "addr": "203.0.113.12:40879",
        "ready": True,
        "attempts": 0,
        "peer_is_host": False,
        "nat": punch_mod.NatSub.CONE,
        "filtering": punch_mod.Filter.ADF,
    }]
    rows = gui_app.build_member_rows(state, direct_rows=direct)
    peer = _row(rows, "cid-x")

    # ★ 用户 2026-10-02：NAT1 与 NAT2 合并成一档（完全锥形/地址受限锥形），
    #   对端的 `DIRECT_CAND`（锥型 + adf）现在显示成合并档。
    check("★ 对端的 NAT 类型来自它的 DIRECT_CAND（锥型 + adf -> NAT1/2 合并档）",
          peer and "NAT1/2" in peer["nat"], str(peer and peer["nat"]))
    check("★ 对端的入站档位来自它的 filt=（adf -> 入站中等）",
          peer and peer["inbound"] == "入站中等", str(peer and peer["inbound"]))
    check("对端 IP:端口用的是打洞拿到的真实地址",
          peer and peer["addr"] == "203.0.113.12:40879", str(peer and peer["addr"]))

    # ---- 主机身份由 `to=` 信号认定（真数据，不是猜）----
    direct = [{
        "peer": "cid-h", "addr": "", "ready": False, "attempts": 1,
        "peer_is_host": True, "nat": None, "filtering": None,
    }]
    state = _state(client_id="cid-owner2", username="另一个房主",
                   is_owner=True, host_claimed=True, host_name="真主机名",
                   members=2, peer_ids=["cid-h"])
    rows = gui_app.build_member_rows(state, direct_rows=direct)
    host_row = _row(rows, "cid-h")

    check("★ 对端在 DIRECT_CAND 里带 to= -> 判定为当前主机",
          host_row and host_row["role"] == gui_app.ROLE_HOST, str(host_row))
    check("主机那行显示中继给的 host_name（这是唯一的真用户名）",
          host_row and host_row["name"] == "真主机名", str(host_row))

    # ---- 没有主机位：对端都是普通玩家（不是未知）----
    state = _state(client_id="cid-owner3", username="房主",
                   is_owner=True, host_claimed=False,
                   members=2, peer_ids=["cid-z"])
    rows = gui_app.build_member_rows(state)

    check("没人持有主机位时，对端身份 = 普通玩家（说得出来，不是未知）",
          _row(rows, "cid-z")["role"] == gui_app.ROLE_PLAYER,
          str(_row(rows, "cid-z")))

    # ---- 角色文案 ----
    check("角色文案：房主/主机/玩家/未知",
          [gui_app.role_text(r) for r in
           (gui_app.ROLE_OWNER, gui_app.ROLE_HOST, gui_app.ROLE_PLAYER, "?")]
          == ["房主", "主机", "玩家", "未知"])


def test_host_transfer():
    """
    host 在一个房间生命周期内会换人：权限和身份都必须跟着【当前这一刻】走。

    这一段全部用假房间状态驱动（不依赖谁去手动点按钮）：A 是 host 时它的客户端
    有按钮；host 转给 B 之后，A 卸任 -> 按钮全消失，B 那边 -> 按钮出现；
    房主（owner）始终不受影响。
    """
    print("")
    print("---- host 换人：踢人权限与身份实时跟着换（不缓存）----")

    import importlib

    gui_app = importlib.import_module("mclink.gui.app")

    ROOM = ("cid-owner", "cid-A", "cid-B")

    def state_for(me, is_host, host_claimed=True, host_name="",
                  host_peer=None):
        state = _state(
            client_id=me, username=me, is_owner=False, is_host=is_host,
            host_claimed=host_claimed, host_name=host_name, members=3,
            peer_ids=[p for p in ROOM if p != me],
            room="r-transfer", room_name="换人房",
            # 2026-09-28（规则 6 §A）：Owner 只认中继的 owner_id=，
            # 不再"花名册第一个就是房主"。这里的 owner 一直是 cid-owner。
            owner_id="cid-owner",
        )
        direct = []

        if host_peer:
            direct = [{
                "peer": host_peer, "addr": "", "ready": False, "attempts": 0,
                "peer_is_host": True, "nat": None, "filtering": None,
            }]

        return state, direct

    # ---- 阶段 1：A 是当前主机（非 owner）----
    state, direct = state_for("cid-A", True, host_name="A")
    rows = gui_app.build_member_rows(state, direct_rows=direct)

    check("A 在任时：自己那行身份 = 主机",
          _row(rows, "cid-A")["role"] == gui_app.ROLE_HOST,
          str(_row(rows, "cid-A")))
    check("A 在任时：能踢非房主成员 B",
          _row(rows, "cid-B")["can_kick"] is True)
    check("A 在任时：不能踢房主",
          _row(rows, "cid-owner")["can_kick"] is False)

    # ---- 阶段 2：host 转给 B，A 卸任 ----
    state, direct = state_for("cid-A", False, host_name="B",
                              host_peer="cid-B")
    rows = gui_app.build_member_rows(state, direct_rows=direct)

    check("★ A 卸任后：自己那行身份 = 玩家（不再是主机）",
          _row(rows, "cid-A")["role"] == gui_app.ROLE_PLAYER,
          str(_row(rows, "cid-A")))
    check("★ A 卸任后：所有踢人按钮立刻消失（权限不缓存）",
          all(not r["can_kick"] for r in rows if not r["is_self"]),
          str([(r["key"], r["role"], r["can_kick"]) for r in rows]))
    check("★ 身份列实时反映新 host：B 那行 = 主机（来自它的 to= 信号）",
          _row(rows, "cid-B")["role"] == gui_app.ROLE_HOST,
          str(_row(rows, "cid-B")))

    # ---- 阶段 3：B 那边的客户端（B 是 host）----
    state, direct = state_for("cid-B", True, host_name="B")
    rows = gui_app.build_member_rows(state, direct_rows=direct)

    check("★ B 接任后：B 能踢 A（非房主）",
          _row(rows, "cid-A")["can_kick"] is True)
    check("★ B 接任后：B 仍然不能踢房主",
          _row(rows, "cid-owner")["can_kick"] is False)

    # ---- 阶段 4：host 位空着（谁都没认领）----
    state, direct = state_for("cid-A", False, host_claimed=False)
    rows = gui_app.build_member_rows(state, direct_rows=direct)

    check("host 位空着时：对端身份都是普通玩家（说得出来，不是未知）",
          _row(rows, "cid-B")["role"] == gui_app.ROLE_PLAYER
          and _row(rows, "cid-owner")["role"] == gui_app.ROLE_OWNER,
          str(rows))

    # ---- 阶段 5：owner 始终可踢除自己外所有人（host 换人不影响）----
    owner_state = _state(
        client_id="cid-owner", username="房主", is_owner=True, is_host=False,
        host_claimed=True, host_name="B", members=3,
        peer_ids=["cid-A", "cid-B"],
    )
    rows = gui_app.build_member_rows(owner_state)

    check("★ owner 的权限与 host 是谁无关：两个对端都能踢",
          all(_row(rows, key)["can_kick"] for key in ("cid-A", "cid-B")),
          str([(r["key"], r["can_kick"]) for r in rows]))



# ============================================================
# 2. KICKED / OK KICK / ERROR KICK_* 的解析（客户端协议层）
# ============================================================

def test_session_line_parsing():
    print("")
    print("---- KICK 协议行的客户端解析 ----")

    # ---- KICKED：设置状态 + 发事件 ----
    events = []
    session = RoomSession(
        "127.0.0.1", 1, "cid-me", "我", "",
        emit=lambda name, data: events.append((name, data)),
    )

    session._on_line("KICKED by=alice reason=kicked")

    check("★ KICKED 行被认出来（kicked=True）", session.kicked is True)
    check("KICKED 的 by= 被解析", session.kicked_by == "alice", session.kicked_by)
    check("KICKED 的 reason= 被解析", session.kicked_reason == "kicked",
          session.kicked_reason)
    check("★ KICKED 会通知界面（kicked 事件，带 by/reason）",
          any(name == "kicked"
              and data.get("by") == "alice"
              and data.get("reason") == "kicked"
              for name, data in events),
          str(events))
    check("KICKED 之后房间状态标记为不可用（不留幽灵态）",
          session.state.revoked is True)

    # ---- OK KICK / OWNER_LEFT_ROOM / ERROR KICK_* 解析 ----
    async def parse_acks():
        results = {}

        s = RoomSession("127.0.0.1", 1, "cid-me", "我", "",
                        emit=lambda *a: None)

        loop = asyncio.get_running_loop()

        for label, line in (
            ("ok", f"{proto.LINE_ACK_KICK} cid-x"),
            ("owner", f"{proto.LINE_ACK_KICK} cid-x OWNER_LEFT_ROOM"),
            ("forbidden", f"ERROR {proto.ERR_KICK_FORBIDDEN} 只有房主或当前主机可以踢人"),
            ("gone", f"ERROR {proto.ERR_KICK_NO_SUCH_MEMBER} 该成员不在房间里"),
            ("self", f"ERROR {proto.ERR_KICK_SELF} 不能踢出自己"),
        ):
            future = loop.create_future()
            s._pending["KICK"] = future
            s._on_line(line)
            results[label] = await asyncio.wait_for(future, timeout=2)

        return results

    results = asyncio.run(parse_acks())

    check("OK KICK -> (True, ..., OK)",
          results["ok"][0] is True and results["ok"][2] == "OK",
          str(results["ok"]))
    check("★ OK KICK ... OWNER_LEFT_ROOM 被识别（房间随之解散）",
          results["owner"][0] is True
          and results["owner"][2] == "OWNER_LEFT_ROOM",
          str(results["owner"]))
    check("ERROR KICK_FORBIDDEN -> 失败 + code 给界面用",
          results["forbidden"][0] is False
          and results["forbidden"][2] == proto.ERR_KICK_FORBIDDEN,
          str(results["forbidden"]))
    check("ERROR KICK_NO_SUCH_MEMBER -> 失败 + code",
          results["gone"][0] is False
          and results["gone"][2] == proto.ERR_KICK_NO_SUCH_MEMBER,
          str(results["gone"]))
    check("ERROR KICK_SELF -> 失败 + code",
          results["self"][0] is False
          and results["self"][2] == proto.ERR_KICK_SELF,
          str(results["self"]))
    check("五种应答的中文文案都拿得到",
          "踢" in proto.describe_error(
              f"ERROR {proto.ERR_KICK_FORBIDDEN}"
          ),
          proto.describe_error(f"ERROR {proto.ERR_KICK_FORBIDDEN}"))


# ============================================================
# 3. 真中继 + 真 RoomSession：权限矩阵
# ============================================================

def start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        "MCLINK_STUN_ENABLED": "0",
        "MCLINK_STATUS_FILE": os.path.join(HERE, "client_kick_status.json"),
        "MCLINK_STATUS_INTERVAL": "5",
        "MCLINK_VERBOSE": "0",
    })

    log = open(
        os.path.join(HERE, "client_kick_relay.log"), "w",
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
            probe = socket.create_connection(("127.0.0.1", RELAY_PORT), 1)
            probe.close()
            return process, log
        except OSError:
            if process.poll() is not None:
                return process, log

            time.sleep(0.3)

    return process, log


async def _open(client_id, name, events, room, create=False):
    session = RoomSession(
        "127.0.0.1", RELAY_PORT, client_id, name, "",
        emit=lambda kind, data, cid=client_id: events.append((cid, kind, data)),
    )

    if create:
        await session.create(room)
    else:
        await session.join(room)

    return session


async def test_kick_matrix():
    print("")
    print("---- 真中继 + 真客户端：权限矩阵与 KICKED ----")

    relay, log = start_relay()

    sessions = []
    events = []

    try:
        if relay.poll() is not None:
            check("中继启动", False, "进程已退出")
            return

        room = "client-kick-room"

        owner = await _open("cid-owner", "房主", events, room, create=True)
        guest = await _open("cid-guest", "玩家甲", events, room)
        host = await _open("cid-host", "主机乙", events, room)
        guest2 = await _open("cid-guest2", "玩家丙", events, room)
        sessions = [owner, guest, host, guest2]

        ok, message = await host.claim_host()
        check("第二个人可以认领主机位（host != owner）", ok, message)

        # 等中继把 HOST_STATE 广播齐
        await asyncio.sleep(0.6)

        check("Roster：房间里 4 个人（owner + 3）",
              owner.state.members == 4, str(owner.state.members))
        check("Roster：peers= 里能看到其余 3 个人（client_id）",
              set(owner.state.peer_ids or []) == {"cid-guest", "cid-host",
                                                  "cid-guest2"},
              str(owner.state.peer_ids))

        # ---- 普通玩家：没有任何权限 ----
        ok, message, code = await guest.kick("cid-owner")
        check("★ 普通玩家踢人 -> ERROR KICK_FORBIDDEN",
              ok is False and code == proto.ERR_KICK_FORBIDDEN,
              f"{ok} {code} {message}")
        check("被拒绝后房间照旧（自己还在、房主还在）",
              guest.running and owner.running
              and owner.state.members == 4,
              f"{guest.running} {owner.running} {owner.state.members}")

        # ---- 当前主机（非 owner）：不能踢房主 ----
        ok, message, code = await host.kick("cid-owner")
        check("★ 当前主机踢房主 -> ERROR KICK_FORBIDDEN",
              ok is False and code == proto.ERR_KICK_FORBIDDEN,
              f"{ok} {code} {message}")

        # ---- 当前主机：可以踢非 owner ----
        ok, message, code = await host.kick("cid-guest2")
        check("★ 当前主机踢普通成员 -> OK KICK",
              ok is True and code == "OK", f"{ok} {code} {message}")

        kicked_events = [
            data for cid, kind, data in events
            if cid == "cid-guest2" and kind == "kicked"
        ]

        check("★ 被踢的人收到 KICKED（带 by= 发起者）",
              bool(kicked_events)
              and kicked_events[0].get("by") == "主机乙",
              str(kicked_events))
        check("★ 被踢的人连接被中继关掉（running=False）",
              await _wait_false(lambda: guest2.running),
              f"running={guest2.running}")

        await asyncio.sleep(0.5)

        check("踢掉一个人后房间还在，人数少 1（4 -> 3）",
              owner.state.members == 3, str(owner.state.members))
        check("★ 其他人不受影响（房主还能 PING/PONG）",
              owner.running and await _ping_ok(owner),
              f"running={owner.running}")

        # ---- 房主踢人：可以踢任何人（除自己） ----
        ok, message, code = await owner.kick("cid-guest")
        check("★ 房主踢普通成员 -> OK KICK",
              ok is True and code == "OK", f"{ok} {code} {message}")

        kicked_events = [
            data for cid, kind, data in events
            if cid == "cid-guest" and kind == "kicked"
        ]

        check("被踢者收到 KICKED（by=房主的显示名）",
              bool(kicked_events) and kicked_events[0].get("by") == "房主",
              str(kicked_events))
        check("被踢者连接断开", await _wait_false(lambda: guest.running))

        # ---- 踢自己 / 踢不存在的人 ----
        ok, message, code = await owner.kick("cid-owner")
        check("★ 踢自己 -> ERROR KICK_SELF",
              ok is False and code == proto.ERR_KICK_SELF,
              f"{ok} {code} {message}")

        ok, message, code = await owner.kick("cid-nobody")
        check("★ 踢不在房间里的人 -> ERROR KICK_NO_SUCH_MEMBER",
              ok is False and code == proto.ERR_KICK_NO_SUCH_MEMBER,
              f"{ok} {code} {message}")

        # ---- 房主也可以踢当前主机（host 不是 owner）----
        ok, message, code = await owner.kick("cid-host")
        check("★ 房主踢当前主机 -> OK KICK",
              ok is True and code == "OK", f"{ok} {code} {message}")

        # ---- 真实延迟：PING/PONG 回声 ----
        got_rtt = None

        for _ in range(30):
            if owner.relay_rtt_ms is not None:
                got_rtt = owner.relay_rtt_ms
                break

            await asyncio.sleep(0.25)

        check("★ 本机延迟是中继 PING/PONG 回声量出来的真实数字（不是编的）",
              got_rtt is not None and got_rtt >= 0.0,
              f"rtt={got_rtt}")

    finally:
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.stop()

        with contextlib.suppress(Exception):
            relay.terminate()
            relay.wait(timeout=8)

        with contextlib.suppress(Exception):
            log.close()


async def _wait_false(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if not predicate():
            return True

        await asyncio.sleep(0.1)

    return not predicate()


async def _ping_ok(session):
    """还能收到 PONG 吗？（PING/PONG 是成员连接自己的心跳）"""
    before = session._last_pong

    for _ in range(40):
        if session._last_pong > before:
            return True

        await asyncio.sleep(0.1)

    return False


def test_engine_kick_wrapper():
    """
    `MCLinkEngine.kick_member()` 这层转发：界面点一下走的就是它。

    它必须把 `RoomSession.kick()` 的三元组原样带回界面（ok/message/code），
    没进房间时也要给一个明确的 code，而不是抛异常。
    """
    print("")
    print("---- 引擎转发层（界面点的就是这里）----")

    from types import SimpleNamespace

    from mclink.core.engine import MCLinkEngine

    engine = MCLinkEngine()

    class FakeRoom:
        state = SimpleNamespace(room="r-kick")

        async def kick(self, target):
            return (True, f"kicked {target}", "OK")

    try:
        # 真实路径上房间里一定已经有引擎事件循环了（open_room 会 start_loop）。
        engine.start_loop()
        engine._room = FakeRoom()

        ok, message, code = engine.kick_member("cid-x")

        check("★ engine.kick_member 原样带回 (ok, message, code)",
              ok is True and message == "kicked cid-x" and code == "OK",
              f"{ok} {message} {code}")

        engine._room = None

        ok, message, code = engine.kick_member("cid-x")

        check("不在房间里时给出明确 code（不抛异常）",
              ok is False and code == "NOT_IN_ROOM", f"{ok} {code}")
    finally:
        with contextlib.suppress(Exception):
            engine.shutdown()


# ============================================================
# 4. 真界面：成员窗口 / 踢人按钮可见性 / 被踢拆干净 / 一人一行
# ============================================================

def _member_window_texts(win):
    found = []

    def visit(node):
        with contextlib.suppress(Exception):
            klass = node.winfo_class()

            if klass not in ("Text", "Entry", "TEntry", "Listbox"):
                try:
                    bound = str(node.cget("textvariable") or "")
                except Exception:
                    bound = ""

                if not bound:
                    try:
                        text = str(node.cget("text") or "")
                    except Exception:
                        text = ""

                    if text.strip():
                        found.append(text)

        for child in node.winfo_children():
            visit(child)

    visit(win)

    return found


def test_gui():
    print("")
    print("---- 真界面：成员列表 / 踢人按钮 / 被踢 / 一人一行 ----")

    try:
        import tkinter as tk
    except ImportError as e:  # pragma: no cover
        check("tkinter available", False, str(e))
        return

    sandbox = tempfile.mkdtemp(prefix="mclink-kick-roster-")
    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox

    from mclink.gui import app as gui_app
    from mclink.gui import i18n
    from mclink.core import punch as punch_mod

    # 弹窗一律拦下来（测试里没人点"确定"）。
    warnings = []

    gui_app.messagebox.showwarning = (
        lambda *a, **k: warnings.append({"args": a, "kwargs": k})
    )
    gui_app.messagebox.showerror = lambda *a, **k: None
    gui_app.messagebox.showinfo = lambda *a, **k: None
    gui_app.messagebox.askokcancel = lambda *a, **k: True
    gui_app.messagebox.askyesno = lambda *a, **k: True

    try:
        root = tk.Tk()
    except Exception as e:  # pragma: no cover
        check("Tk root can be created (display available)", False, repr(e))
        return

    try:
        root.withdraw()
        i18n.set_language("zh")

        application = gui_app.MCLinkApp(root)

        # ---- 用一个假房间状态驱动界面 ----
        class FakeEngine:
            """只提供成员列表需要的那几个只读接口。"""

            def __init__(self, engine):
                self._engine = engine
                self.state = None
                self.direct = {}
                self.profile = None
                self.ping = None

            def room_state(self):
                return self.state

            def room_direct_state(self):
                return {"links": list(self.direct.get("links") or [])}

            def room_nat_profile(self):
                return self.profile

            def room_rtt_ms(self):
                return self.ping

            def __getattr__(self, name):
                return getattr(self._engine, name)

        fake = FakeEngine(application.engine)
        application.engine = fake

        owner_state = _state(
            client_id="cid-owner", username="房主本人",
            is_owner=True, members=3, peer_ids=["cid-a", "cid-b"],
            room="r-gui", room_name="界面房",
        )

        fake.state = owner_state
        fake.ping = 37.0

        application._open_member_window()
        root.update_idletasks()
        application._refresh_member_list(force=True)

        check("房间成员窗口能建出来",
              application.member_window is not None
              and bool(application.member_window.winfo_exists()))
        check("窗口标题 = 房间成员",
              application.member_window.title() == "房间成员",
              application.member_window.title())

        # 未知值：用户名 / NAT / 入站 / IP:端口 / 延迟
        cells = application.member_rows["cid-a"]["vars"]

        check("★ 对端没有中继广播的名字时显示 client_id（不再有「未知」）",
              cells["name"].get() == "cid-a",
              cells["name"].get())
        check("★ 对端 NAT 未知 -> 显示「-」", cells["nat"].get() == "-",
              cells["nat"].get())
        check("★ 对端入站未知 -> 显示「-」", cells["inbound"].get() == "-",
              cells["inbound"].get())
        check("★ 对端地址未知 -> 显示「-」", cells["addr"].get() == "-",
              cells["addr"].get())
        check("★ 对端延迟未知 -> 显示「-」", cells["ping"].get() == "-",
              cells["ping"].get())
        check("自己那行显示真用户名 + 自己量到的延迟",
              application.member_rows["cid-owner"]["vars"]["name"].get()
              == "房主本人"
              and application.member_rows["cid-owner"]["vars"]["ping"].get()
              == "37 ms",
              str(application.member_rows["cid-owner"]["vars"]["ping"].get()))

        # ---- 房主：两个对端都有踢出按钮 ----
        check("★ 房主：所有对端都有「踢出」按钮",
              application.member_rows["cid-a"]["kick_visible"] is True
              and application.member_rows["cid-b"]["kick_visible"] is True)
        check("房主自己那行没有按钮",
              application.member_rows["cid-owner"]["kick_visible"] is False)

        # ---- 行控件复用（不闪 / 不重建）----
        frame_before = application.member_rows["cid-a"]["frame"]
        application._refresh_member_list(force=True)
        root.update_idletasks()

        check("★ 刷新复用同一批控件（不重建 = 不闪、不抢焦点）",
              application.member_rows["cid-a"]["frame"] is frame_before)

        # ---- 有人走了：那一行消失，不留空行 ----
        owner_state.peer_ids = ["cid-a"]
        owner_state.members = 2
        application._refresh_member_list(force=True)
        root.update_idletasks()

        check("★ 对端离开 -> 那一行消失（没有空行）",
              "cid-b" not in application.member_rows
              and "cid-a" in application.member_rows,
              str(sorted(application.member_rows)))

        # ---- 普通玩家：一个按钮都没有 ----
        fake.state = _state(
            client_id="cid-p", username="路人", is_host=False, is_owner=False,
            host_claimed=True, host_name="某人",
            members=3, peer_ids=["cid-owner", "cid-host2"],
        )
        application._refresh_member_list(force=True)
        root.update_idletasks()

        check("★ 普通玩家：所有对端都没有踢出按钮",
              all(not application.member_rows[key]["kick_visible"]
                  for key in ("cid-owner", "cid-host2")),
              str({k: application.member_rows[k]["kick_visible"]
                   for k in application.member_rows}))

        # ---- 当前主机（非 owner）：房主那行没有按钮，别人有 ----
        fake.state = _state(
            client_id="cid-host", username="主机本人", is_host=True,
            is_owner=False, host_claimed=True, host_name="主机本人",
            members=3, peer_ids=["cid-owner", "cid-d"],
            owner_id="cid-owner",
        )
        application._refresh_member_list(force=True)
        root.update_idletasks()

        check("★ 当前主机：房主那行没有按钮、其他人有",
              application.member_rows["cid-owner"]["kick_visible"] is False
              and application.member_rows["cid-d"]["kick_visible"] is True,
              str({k: application.member_rows[k]["kick_visible"]
                   for k in application.member_rows}))

        # ---- ★ host 换人：同一个窗口、不重开，按钮与身份都要立刻跟着换 ----
        fake.direct = {}

        fake.state = _state(
            client_id="cid-A", username="A", is_host=True, is_owner=False,
            host_claimed=True, host_name="A", members=3,
            peer_ids=["cid-owner", "cid-B"], room="r-gui",
            room_name="gui-room",
        )
        application._refresh_member_list(force=True)
        root.update_idletasks()

        # ★ 用户 2026-10-03 冻结口径：非房主的 host 只显示「主机」
        #   （房主那一份身份他不具备；房主的组合见 owner+host/owner+player）。
        check("host=A 时：A 自己有按钮、身份列写「主机」",
              application.member_rows["cid-B"]["kick_visible"] is True
              and application.member_rows["cid-A"]["vars"]["role"].get()
              == "主机",
              str({k: (application.member_rows[k]["kick_visible"],
                       application.member_rows[k]["vars"]["role"].get())
                   for k in application.member_rows}))

        # host 转给 B：A 卸任（同一个窗口）
        fake.state = _state(
            client_id="cid-A", username="A", is_host=False, is_owner=False,
            host_claimed=True, host_name="B", members=3,
            peer_ids=["cid-owner", "cid-B"], room="r-gui",
            room_name="gui-room",
        )
        fake.direct = {"links": [{
            "peer": "cid-B", "addr": "", "ready": False, "attempts": 0,
            "peer_is_host": True, "nat": None, "filtering": None,
        }]}
        application._refresh_member_list(force=True)
        root.update_idletasks()

        check("★ A 卸任后（同一窗口）：所有踢出按钮立刻消失",
              all(not application.member_rows[key]["kick_visible"]
                  for key in application.member_rows),
              str({k: application.member_rows[k]["kick_visible"]
                   for k in application.member_rows}))
        # ★ 用户 2026-10-03 冻结口径：非房主 host = 「主机」（不再拼「玩家·主机」）。
        check("★ A 卸任后：身份列实时变成「玩家」，B 那行变成「主机」",
              application.member_rows["cid-A"]["vars"]["role"].get() == "玩家"
              and application.member_rows["cid-B"]["vars"]["role"].get()
              == "主机",
              str({k: application.member_rows[k]["vars"]["role"].get()
                   for k in application.member_rows}))

        # B 接任：B 那边的客户端按钮出现
        fake.state = _state(
            client_id="cid-B", username="B", is_host=True, is_owner=False,
            host_claimed=True, host_name="B", members=3,
            peer_ids=["cid-owner", "cid-A"], room="r-gui",
            room_name="gui-room", owner_id="cid-owner",
        )
        fake.direct = {}
        application._refresh_member_list(force=True)
        root.update_idletasks()

        check("★ B 接任后：B 有按钮（A 那行可踢、房主那行不可踢）",
              application.member_rows["cid-A"]["kick_visible"] is True
              and application.member_rows["cid-owner"]["kick_visible"] is False,
              str({k: application.member_rows[k]["kick_visible"]
                   for k in application.member_rows}))

        # ---- 服务端拒绝 -> 界面给清晰中文提示（仍然以服务端的答复为准）----
        warnings.clear()
        application.engine.kick_member = lambda target, timeout=15: (
            False, "只有房主或当前主机可以踢人 [KICK_FORBIDDEN]",
            "KICK_FORBIDDEN",
        )

        application.on_kick_member("cid-A")
        root.update_idletasks()

        check("★ 服务端回 KICK_FORBIDDEN -> 弹中文提示（不是静默失败）",
              any("权限" in str(w.get("args"))
                  for w in warnings),
              str(warnings))

        infos = []
        gui_app.messagebox.showinfo = (
            lambda *a, **k: infos.append({"args": a, "kwargs": k})
        )
        application.engine.kick_member = lambda target, timeout=15: (
            True, "已踢出该成员", "OK",
        )

        application.on_kick_member("cid-A")
        root.update_idletasks()

        check("★ 服务端回 OK KICK -> 界面确认「已踢出成员」",
              any((i.get("args") or ("",))[0] == "已踢出成员" for i in infos),
              str(infos))

        application.engine.kick_member = lambda target, timeout=15: (
            True, "已踢出房主，房间已解散", "OWNER_LEFT_ROOM",
        )
        infos.clear()

        application.on_kick_member("cid-owner")
        root.update_idletasks()

        check("★ OWNER_LEFT_ROOM -> 提示房间已解散",
              any("解散" in str(i.get("args")) for i in infos),
              str(infos))

        # ---- 有直连数据时，NAT / 入站 / 地址都真的填上 ----
        fake.direct = {"links": [{
            "peer": "cid-owner", "addr": "1.2.3.4:5000", "ready": True,
            "attempts": 0, "peer_is_host": False,
            "nat": punch_mod.NatSub.CONE, "filtering": punch_mod.Filter.APDF,
        }]}

        application._refresh_member_list(force=True)
        root.update_idletasks()

        cells = application.member_rows["cid-owner"]["vars"]

        check("有直连数据时：NAT = NAT3、入站 = 入站严格、地址是真的",
              "NAT3" in cells["nat"].get()
              and cells["inbound"].get() == "入站严格"
              and cells["addr"].get() == "1.2.3.4:5000",
              f"{cells['nat'].get()} / {cells['inbound'].get()} / "
              f"{cells['addr'].get()}")

        fake.direct = {}

        # ---- i18n：英文界面上成员窗口不许残留中文 ----
        #
        # 房间名是**数据**（用户自己取的），不是界面文案：把它换成 ASCII 才
        # 能单纯地判定"界面文字有没有漏翻"。
        owner_state.room_name = "gui-room"

        application.language_var.set("English")
        application.on_language_change()
        root.update_idletasks()

        fake.state = owner_state

        application._open_member_window()
        root.update_idletasks()
        application._refresh_member_list(force=True)
        root.update_idletasks()

        texts = _member_window_texts(application.member_window)
        leftovers = [t for t in texts if i18n.has_cjk(t)]

        check("★ [en] 成员窗口没有残留中文",
              not leftovers, "漏翻：" + " | ".join(leftovers[:10]))
        check("[en] 没有广播名字时显示 client_id（不是 Unknown、不是中文）",
              application.member_rows["cid-a"]["vars"]["name"].get()
              == "cid-a",
              application.member_rows["cid-a"]["vars"]["name"].get())

        # ---- 被踢：走管理关房同一条拆除路径 + 一句中文告知 ----
        i18n.set_language("zh")
        application.language_var.set("中文")
        application.on_language_change()
        root.update_idletasks()

        application._room_close_notified = False
        warnings.clear()

        closed = {"count": 0}

        def fake_close(*_a, **_k):
            # 引擎的 close_room() 会把房间对象扔掉：界面随后读到的就是"不在
            # 任何房间里"。假引擎也要照这个事实走，否则测的是假状态。
            closed["count"] += 1
            fake.state = None
            return False

        application.engine.close_room = fake_close
        application.engine.room_state = lambda: fake.state

        application._handle_core_event(
            "kicked", {"by": "alice", "reason": "kicked"}
        )
        root.update_idletasks()

        shown = " | ".join(str(w) for w in warnings)

        check("★ 被踢 -> 调 close_room 把成员连接退干净",
              closed["count"] >= 1, str(closed))
        check("★ 被踢 -> 弹窗告知（标题「你已被踢出房间」）",
              any((w.get("args") or ("",))[0] == "你已被踢出房间"
                  for w in warnings),
              shown)
        check("★ 被踢弹窗里带发起者（alice）",
              any("alice" in str(w.get("args")) for w in warnings),
              shown)
        check("★ 被踢原因翻成中文（不是把中继的英文记号丢给用户）",
              any(len(w.get("args") or ()) > 1
                  and "被踢出房间" in str(w["args"][1])
                  and "kicked" not in str(w["args"][1])
                  for w in warnings),
              shown)
        check("被踢之后本地房间输入框清空、标签复位（无幽灵态）",
              application.room_var.get() == ""
              and "未加入房间" in application.room_state_label.cget("text"),
              f"{application.room_var.get()!r} / "
              f"{application.room_state_label.cget('text')!r}")

        application._close_member_window()

        # ---- 一人一行：来一个多一行，走一个少一行 ----
        print("")
        print("---- 运行状态卡片：一人一行（一个对端一条洞）----")

        application._render_hole_rows([
            {"peer": "cid-b", "addr": "203.0.113.12:40879", "ready": True,
             "attempts": 0, "peer_is_host": False, "nat": None,
             "filtering": None},
            {"peer": "cid-c", "addr": "", "ready": False, "attempts": 2,
             "peer_is_host": False, "nat": None, "filtering": None},
        ])
        root.update_idletasks()

        check("两个对端 -> 两行",
              len(application._hole_rows) == 2,
              str(sorted(application._hole_rows)))

        texts = [label.cget("text")
                 for _row, label in application._hole_rows.values()]

        check("已打通的那行：直连 + 对端 + ip:port + 已打通",
              any("直连" in t and "203.0.113.12:40879" in t and "已打通" in t
                  for t in texts),
              str(texts))
        check("打洞中的那行：打洞中 + 第 2 次",
              any("打洞中" in t and "第 2 次" in t for t in texts),
              str(texts))
        check("★ 没有任何空行（每一行都有文字）",
              all(t.strip() for t in texts), str(texts))

        row_before = application._hole_rows["cid-b"][0]

        application._render_hole_rows([
            {"peer": "cid-b", "addr": "203.0.113.12:40879", "ready": True,
             "attempts": 0, "peer_is_host": False, "nat": None,
             "filtering": None},
        ])
        root.update_idletasks()

        check("★ 走了一个对端 -> 少一行（没有空行留下）",
              len(application._hole_rows) == 1
              and "cid-c" not in application._hole_rows,
              str(sorted(application._hole_rows)))
        check("留下的那行还是同一个控件（不闪）",
              application._hole_rows["cid-b"][0] is row_before)

        application._render_hole_rows([])
        root.update_idletasks()

        check("对端都没了 -> 一行都没有（不出现空行）",
              application._hole_rows == {}, str(application._hole_rows))

    except Exception:
        check("客户端踢人/名单测试完成且没有抛异常", False)
        traceback.print_exc()
    else:
        check("客户端踢人/名单测试完成且没有抛异常", True)
    finally:
        with contextlib.suppress(Exception):
            root.destroy()

        i18n.set_language("zh")


# ============================================================

def main():
    print("=" * 64)
    print("MC-Link 客户端：房间成员列表 + 踢人（KICK）")
    print("=" * 64)

    test_roster_builder()
    test_host_transfer()
    test_session_line_parsing()
    test_engine_kick_wrapper()

    try:
        asyncio.run(test_kick_matrix())
    except Exception:
        check("真中继权限矩阵测试没有抛异常", False)
        traceback.print_exc()
    else:
        check("真中继权限矩阵测试没有抛异常", True)

    test_gui()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
