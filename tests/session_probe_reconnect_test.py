"""
CraftLink -- 重连探活 / 模式归因 / was_live 全路径 / HOST_TAKEN 回归网。

============================================================================
 这一套钉的是什么（用户 2026-09-29 这一轮的四条）
============================================================================

1. `SESSION __mclink__ <client_id> <username> <token>`（PROTOCOL §8.10）
   —— 被动掉线之后、决定要不要自动重连**之前**，客户端必须主动探一次
   「我的 token 还有效吗 / 我还在房间里吗」：

     * `auto_reconnect=1`（STILL_IN_ROOM / DROPPED / NOT_IN_ROOM）-> 自动重建；
     * `auto_reconnect=0`（KICKED / LEFT / TIMEOUT / ROOM_CLOSED / AUTH_*）
       -> 按退出房间处理，**不重连**；`relogin=1` 时提示重新登录；
     * 老中继 `ERROR UNKNOWN_ROLE` / 探测不到 -> **退回原来的行为**
       （问一次 ROOMS，再按错误码判定），一个字不变。

   同一规则对 Host 和 Player 生效（每个人都有自己的成员控制连接，判定点
   `RoomSession._explain_disconnect()` 是同一条）。

2. `mode_reason=owner|restored|default`（§8.9）解析 + 归因：
   容错解析（老中继没有 = `""` = 未知，绝不假装 default），只在
   `(mode, mode_reason)` 变化时写一行英文日志；`restored`（同一 Owner 重建
   恢复）必须能和"有人在乱改模式"区分开（日志里明说 not a manual change，
   而且不会触发任何 `conn_mode_changed` 强制重建）。

3. `was_live=0|1`（§8.9）：成员控制连接那条已经消费了；**等待池**那条
   `ERROR CONN_MODE_CHANGED … was_live=`（PLAYER / HOST_TUNNEL 还在等配对时
   收到）现在也解析并交给同一个消费者，热切换判定不会因为"没收到"而退回
   本地 `running` 的旧判据。

4. `HOST_TAKEN`（§8.11）：认领被拒时界面提示正确，且**不会把 host 位当成
   空闲**（`is_host` 清掉，但 `host_claimed` / `host_name` / `host_id`
   这些身份事实一个都不许动）。

Run:
    python tests/session_probe_reconnect_test.py
"""

import asyncio
import contextlib
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

SANDBOX = tempfile.mkdtemp(prefix="mclink-session-probe-")
os.environ["APPDATA"] = SANDBOX
os.environ["XDG_CONFIG_HOME"] = SANDBOX

from mclink.core import control_client as cc  # noqa: E402
from mclink.core import host_core  # noqa: E402
from mclink.core import player_core  # noqa: E402
from mclink.core import protocol as proto  # noqa: E402
from mclink.core import punch_flow as pf  # noqa: E402
from mclink.core import room as roommod  # noqa: E402

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
# 1. SESSION —— 行格式 / 解析 / 判定表（纯逻辑）
# ============================================================

def test_session_protocol_and_verdicts():
    print("")
    print("---- 1. SESSION 探活：协议行 + 解析 + 判定表 ----")

    check(
        "★ 协议里有 SESSION 角色与 OK SESSION 应答（§8.10）",
        proto.ROLE_SESSION == "SESSION"
        and proto.LINE_ACK_SESSION == "OK SESSION"
        and proto.SENTINEL == "__mclink__",
        f"{proto.ROLE_SESSION!r} {proto.LINE_ACK_SESSION!r}",
    )

    reply = (
        "OK SESSION valid=1 in_room=1 token=account room=room-1 "
        "name=%E6%88%BF%E9%97%B4 reason=STILL_IN_ROOM "
        "auto_reconnect=1 relogin=0"
    )
    probe = cc.parse_session_probe([reply])

    check(
        "★ 解析真实应答：valid / in_room / token / room / name / reason / 两个布尔",
        probe.ok
        and probe.valid
        and probe.in_room
        and probe.token_kind == "account"
        and probe.room == "room-1"
        and probe.name == "房间"
        and probe.reason == "STILL_IN_ROOM"
        and probe.auto_reconnect is True
        and probe.relogin is False,
        repr(probe),
    )

    # 容错：中继没给 auto_reconnect -> 用 reason 表推导（老/半新中继）。
    no_bool = cc.parse_session_probe(
        ["OK SESSION valid=1 in_room=0 token=account room=- name=- "
         "reason=NOT_IN_ROOM relogin=0"]
    )

    check(
        "★ 没有 auto_reconnect 字段时按 reason 推导（NOT_IN_ROOM -> 可以重连）",
        no_bool.auto_reconnect is True
        and cc.session_probe_verdict(no_bool) == "reconnect",
        repr(no_bool),
    )

    # 老中继：ERROR UNKNOWN_ROLE -> 明确标成「不认识」，判定必须是 unknown。
    old = cc.parse_session_probe(["ERROR UNKNOWN_ROLE"])

    check(
        "★ 老中继 ERROR UNKNOWN_ROLE -> old_relay + verdict=unknown（退回旧行为）",
        old.ok is False and old.old_relay is True
        and cc.session_probe_verdict(old) == "unknown",
        repr(old),
    )

    check(
        "★ 空应答 / 垃圾应答 / 别的错误 -> verdict=unknown（绝不瞎猜）",
        cc.session_probe_verdict(cc.parse_session_probe([])) == "unknown"
        and cc.session_probe_verdict(cc.parse_session_probe(["???"])) == "unknown"
        and cc.session_probe_verdict(
            cc.parse_session_probe(["ERROR AUTH_UNAVAILABLE"])
        ) == "unknown"
        and cc.session_probe_verdict(None) == "unknown",
        "",
    )

    print("")
    print("---- 1b. §8.10 的完整 reason 判定表 ----")

    # (reason, auto_reconnect, relogin, 期望判定)
    matrix = [
        ("STILL_IN_ROOM", "1", "0", "reconnect"),
        ("DROPPED", "1", "0", "reconnect"),
        ("NOT_IN_ROOM", "1", "0", "reconnect"),
        ("KICKED", "0", "0", "leave"),
        ("LEFT", "0", "0", "leave"),
        ("TIMEOUT", "0", "0", "leave"),
        ("ROOM_CLOSED", "0", "0", "leave"),
        ("AUTH_INVALID_TOKEN", "0", "1", "leave"),
        ("AUTH_REQUIRED", "0", "1", "leave"),
    ]

    bad = []

    for reason, auto, relogin, want in matrix:
        parsed = cc.parse_session_probe([
            f"OK SESSION valid=1 in_room=1 token=account room=r name=n "
            f"reason={reason} auto_reconnect={auto} relogin={relogin}"
        ])

        if cc.session_probe_verdict(parsed) != want:
            bad.append(
                f"{reason}: {cc.session_probe_verdict(parsed)} != {want}"
            )

    check(
        "★ 完整 reason 表：STILL_IN_ROOM/DROPPED/NOT_IN_ROOM->reconnect；"
        "KICKED/LEFT/TIMEOUT/ROOM_CLOSED/AUTH_*->leave",
        not bad,
        repr(bad),
    )

    # 未知 reason + 没有布尔 -> unknown（不猜）。
    check(
        "★ 未知 reason 且没有 auto_reconnect -> unknown（不猜）",
        cc.session_reason_auto_reconnect("SOMETHING_NEW") is None
        and cc.session_probe_verdict(
            cc.parse_session_probe(
                ["OK SESSION valid=1 in_room=1 token=account reason=SOMETHING_NEW"]
            )
        ) == "unknown",
        "",
    )


# ============================================================
# 2. 真 socket：探测行真的是 SESSION __mclink__ …（假 relay）
# ============================================================

async def _probe_against(reply, timeout=cc.SESSION_PROBE_TIMEOUT):
    """一个假 relay：收一行、记下来、回一行（或干脆不回）。"""
    received = []

    async def handler(reader, writer):
        try:
            line = (await asyncio.wait_for(reader.readline(), 5)).decode(
                errors="replace"
            ).strip()
            received.append(line)

            if reply is not None:
                writer.write((reply + "\n").encode())
                await writer.drain()
            else:
                # 永不回答：探测必须靠自己的短超时退出。
                await asyncio.sleep(5)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    started = time.monotonic()
    try:
        probe = await cc.probe_session(
            "127.0.0.1", port, "cid-1", "alice", "tok-1", timeout=timeout
        )
    finally:
        elapsed = time.monotonic() - started
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()

    return probe, received, elapsed


def test_probe_uses_a_real_short_lived_connection():
    print("")
    print("---- 2. 真 socket：SESSION 探活真的发那条只读行 ----")

    probe, received, _ = asyncio.run(
        _probe_against(
            "OK SESSION valid=1 in_room=1 token=guest room=r name=n "
            "reason=STILL_IN_ROOM auto_reconnect=1 relogin=0"
        )
    )

    check(
        "★ 发出去的就是 `SESSION __mclink__ <client_id> <username> <token>`",
        received == ["SESSION __mclink__ cid-1 alice tok-1"],
        repr(received),
    )
    check(
        "★ 真 socket 的应答被解析成 verdict=reconnect",
        probe.ok and cc.session_probe_verdict(probe) == "reconnect",
        repr(probe),
    )

    old, received, _ = asyncio.run(_probe_against("ERROR UNKNOWN_ROLE"))

    check(
        "★ 老中继（真 socket）回 UNKNOWN_ROLE -> 能认出来且不抛异常",
        old.old_relay is True and cc.session_probe_verdict(old) == "unknown",
        repr(old),
    )

    silent, _, elapsed = asyncio.run(_probe_against(None, timeout=0.6))

    check(
        "★ 探测不回答 -> 自己的短超时退出（不拖住重连流程）",
        silent.ok is False and elapsed < 2.0,
        f"elapsed={elapsed:.2f}s ok={silent.ok}",
    )

    # 连不上的端口（真 relay 不在）也一样：返回 ok=False，绝不抛异常。
    async def _dead_port():
        async def _nothing(_reader, writer):
            with contextlib.suppress(Exception):
                writer.close()

        srv = await asyncio.start_server(_nothing, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        srv.close()
        with contextlib.suppress(Exception):
            await srv.wait_closed()
        return port

    dead = asyncio.run(_dead_port())

    unreachable = asyncio.run(
        cc.probe_session("127.0.0.1", dead, "cid", "u", "t", timeout=0.6)
    )

    check(
        "★ relay 不可达 -> ok=False（探测失败不许影响连接流程）",
        unreachable.ok is False
        and cc.session_probe_verdict(unreachable) == "unknown",
        repr(unreachable),
    )


# ============================================================
# 3. 核心决策：_explain_disconnect 走探活（host / player 同规则）
# ============================================================

def _run_explainer(probe, exists=True, is_host=False):
    events = []

    async def go():
        session = roommod.RoomSession(
            "127.0.0.1",
            1,
            "cid-me",
            "me",
            token="tok-me",
            emit=lambda name, data: events.append((name, data)),
            is_current=lambda: True,
        )
        session.state.room = "room-1"
        session.state.is_host = is_host
        session.last_error = "connection reset by peer"

        async def fake_probe(*_a, **_k):
            return probe

        async def fake_exists(*_a, **_k):
            return exists

        with contextlib.suppress(Exception):
            cc.probe_session = fake_probe
            roommod._room_exists = fake_exists
            await session._explain_disconnect()

    try:
        asyncio.run(go())
    finally:
        pass

    return events


def _names(events):
    return [name for name, _data in events]


def test_explainer_decides_from_the_probe():
    print("")
    print("---- 3. 被动掉线：探活说闪断 -> 自动重连；说身份没了 -> 按退出 ----")

    original_probe = cc.probe_session
    original_exists = roommod._room_exists

    try:
        blip = cc.parse_session_probe([
            "OK SESSION valid=1 in_room=1 token=account room=r name=n "
            "reason=STILL_IN_ROOM auto_reconnect=1 relogin=0"
        ])
        kicked = cc.parse_session_probe([
            "OK SESSION valid=0 in_room=0 token=guest room=r name=n "
            "reason=KICKED auto_reconnect=0 relogin=0"
        ])
        expired = cc.parse_session_probe([
            "OK SESSION valid=0 in_room=0 token=account room=- name=- "
            "reason=AUTH_INVALID_TOKEN auto_reconnect=0 relogin=1"
        ])

        # ---- player 侧 ----
        events = _run_explainer(blip, is_host=False)
        check(
            "★ Player + STILL_IN_ROOM -> room_lost（唯一自动重连入口）",
            _names(events)[-1] == "room_lost",
            _names(events),
        )

        events = _run_explainer(kicked, is_host=False)
        last = events[-1]

        check(
            "★ Player + KICKED -> session_ended / code=KICKED / 不重连",
            last[0] == "session_ended"
            and last[1].get("code") == "KICKED"
            and last[1].get("relogin") is False,
            repr(last),
        )

        events = _run_explainer(expired, is_host=False)
        last = events[-1]

        check(
            "★ Player + AUTH_INVALID_TOKEN -> session_ended / relogin=1",
            last[0] == "session_ended"
            and last[1].get("code") == "AUTH_INVALID_TOKEN"
            and last[1].get("relogin") is True,
            repr(last),
        )

        # ---- host 侧：同一条规则 ----
        events_host = _run_explainer(blip, is_host=True)
        events_player = _run_explainer(blip, is_host=False)

        check(
            "★ Host 与 Player 的判定完全一致（同一规则，不按职位分叉）",
            _names(events_host) == _names(events_player)
            and _names(events_host)[-1] == "room_lost",
            f"{_names(events_host)} vs {_names(events_player)}",
        )

        events_host = _run_explainer(kicked, is_host=True)
        events_player = _run_explainer(kicked, is_host=False)

        check(
            "★ Host 被踢同样按退出房间处理（不自动重连）",
            _names(events_host)[-1] == "session_ended"
            and events_host[-1][1].get("code") == "KICKED"
            and _names(events_host) == _names(events_player),
            repr(events_host[-1]),
        )

        # ---- 老中继 / 探不到：退回原来的行为 ----
        old = cc.parse_session_probe(["ERROR UNKNOWN_ROLE"])
        events = _run_explainer(old, exists=True, is_host=False)

        check(
            "★ 老中继 + 房间还在 -> 照旧 room_lost（行为一字不变）",
            _names(events)[-1] == "room_lost",
            _names(events),
        )

        events = _run_explainer(old, exists=False, is_host=False)

        check(
            "★ 老中继 + 房间没了 -> 照旧 room_gone（行为一字不变）",
            _names(events)[-1] == "room_gone",
            _names(events),
        )

        events = _run_explainer(cc.SessionProbe(ok=False), exists=True)

        check(
            "★ 探测不到（超时/垃圾）-> 退回 ROOMS 判定，不是硬失败",
            _names(events)[-1] == "room_lost",
            _names(events),
        )

        # 探活说闪断时，**绝不**再碰 ROOMS（快、且语义单一）。
        called = {"exists": 0}

        async def go_count():
            session = roommod.RoomSession(
                "127.0.0.1", 1, "cid", "u", token="t",
                emit=lambda *a, **k: None, is_current=lambda: True,
            )
            session.state.room = "r"
            session.last_error = "x"

            async def fake_probe(*_a, **_k):
                return blip

            async def counting_exists(*_a, **_k):
                called["exists"] += 1
                return True

            cc.probe_session = fake_probe
            roommod._room_exists = counting_exists
            await session._explain_disconnect()

        asyncio.run(go_count())

        check(
            "★ 探活已经给出结论时不再多发一次 ROOMS 查询",
            called["exists"] == 0,
            repr(called),
        )
    finally:
        cc.probe_session = original_probe
        roommod._room_exists = original_exists


# ============================================================
# 4. mode_reason：解析 + 归因日志 + restored 不被误判
# ============================================================

def test_mode_reason_parsing_and_attribution():
    print("")
    print("---- 4. mode_reason=owner|restored|default 解析 + 归因 ----")

    events = []
    session = roommod.RoomSession(
        "127.0.0.1", 1, "cid-me", "me", token="t",
        emit=lambda name, data: events.append((name, data)),
        is_current=lambda: True,
    )

    # 老中继：没有 mode_reason -> 未知，绝不当成 default。
    session._on_line(
        "HOST_STATE host=1 host_name=A host_id=cid-a members=2 world=1 "
        "peers=cid-a started=cid-a conn=cid-a:connected mode=stun"
    )

    check(
        "★ 老中继没有 mode_reason -> 未知（\"\"），不是 default",
        session.state.mode_reason == ""
        and session.state.conn_mode == "stun"
        and not any("[mode]" in str(data.get("text", ""))
                    for _n, data in events),
        repr(session.state.mode_reason),
    )

    events.clear()
    session._on_line(
        "HOST_STATE host=1 host_name=A host_id=cid-a members=2 world=1 "
        "peers=cid-a started=cid-a conn=cid-a:connected mode=stun "
        "mode_reason=owner"
    )

    mode_logs = [str(d.get("text", "")) for n, d in events if n == "log"
                 and "[mode]" in str(d.get("text", ""))]

    check(
        "★ mode_reason=owner -> 解析 + 说明是 Owner 显式改的",
        session.state.mode_reason == "owner"
        and len(mode_logs) == 1
        and "explicitly" in mode_logs[0],
        repr(mode_logs),
    )

    # 同一条广播重复来：不刷屏。
    session._on_line(
        "HOST_STATE host=1 host_name=A host_id=cid-a members=2 world=1 "
        "peers=cid-a started=cid-a conn=cid-a:connected mode=stun "
        "mode_reason=owner"
    )

    check(
        "★ 同一个 (mode, mode_reason) 重复广播 -> 不重复写日志",
        len([1 for n, d in events if n == "log"
             and "[mode]" in str(d.get("text", ""))]) == 1,
        repr([d.get("text") for n, d in events if n == "log"]),
    )

    events.clear()
    session._on_line(
        "HOST_STATE host=1 host_name=A host_id=cid-a members=2 world=1 "
        "peers=cid-a started=cid-a conn=cid-a:connected mode=stun "
        "mode_reason=restored"
    )

    restored_logs = [str(d.get("text", "")) for n, d in events if n == "log"
                     and "[mode]" in str(d.get("text", ""))]

    check(
        "★ mode_reason=restored -> 明说「同一 Owner 重建恢复，不是手动改的」",
        session.state.mode_reason == "restored"
        and len(restored_logs) == 1
        and "restored" in restored_logs[0]
        and "not a manual change" in restored_logs[0],
        repr(restored_logs),
    )

    check(
        "★ restored 的 HOST_STATE 绝不触发 `conn_mode_changed`（不强制重建）",
        not any(n == "conn_mode_changed" for n, _d in events),
        repr(_names_local(events)),
    )

    # 容错：不认识的值整条忽略（保持上一次的归因，不污染成 default）。
    session._on_line(
        "HOST_STATE host=1 host_name=A host_id=cid-a members=2 world=1 "
        "peers=cid-a started=cid-a conn=cid-a:connected mode=stun "
        "mode_reason=some_future_value"
    )

    check(
        "★ 不认识的 mode_reason 取值 -> 忽略（保持上次），不写成 default",
        session.state.mode_reason == "restored",
        repr(session.state.mode_reason),
    )

    session._on_line(
        "HOST_STATE host=1 host_name=A host_id=cid-a members=2 world=1 "
        "peers=cid-a started=cid-a conn=cid-a:connected mode=relay "
        "mode_reason=default"
    )

    check(
        "★ mode_reason=default -> 全新房间的默认值归因",
        session.state.mode_reason == "default"
        and "default" in roommod.describe_mode_reason("default"),
        repr(session.state.mode_reason),
    )

    check(
        "★ describe_mode_reason 容错：未知 -> \"\"（不编原因）",
        roommod.describe_mode_reason("") == ""
        and roommod.describe_mode_reason("bogus") == "",
        repr(roommod.describe_mode_reason("bogus")),
    )

    stats = session.stats()

    check(
        "★ 状态快照带 conn_mode + mode_reason",
        stats.get("conn_mode") == "relay" and stats.get("mode_reason") == "default",
        repr({k: stats.get(k) for k in ("conn_mode", "mode_reason")}),
    )

    # ---- restored 不会被误判成"有人在乱改模式" ----
    # 会话已经在 restored 的模式上 -> 没有任何"重建"请求（真实现，假对象）。
    from mclink.gui.app import MCLinkApp

    class _FakeRebuild:
        #: 真实现里的冷却常量（`_session_mode_rebuild_delta` 会读它）。
        SESSION_MODE_REBUILD_COOLDOWN_S = 5.0

        def __init__(self, running, session_mode, room_mode):
            self.running = running
            self._session_mode = session_mode
            self._room_mode = room_mode
            self.rebuilds = []
            self._rebuild_in_flight = False
            self._mode_rebuild_pending = False
            self._last_session_mode_rebuild_at = 0.0
            self._session_mode_rebuild_target = None
            self.engine = types.SimpleNamespace(
                session_mode=lambda: self._session_mode
            )

        def _room_conn_mode(self):
            return self._room_mode

        def _rebuild_tunnels(self, mode=None):
            self.rebuilds.append(mode)

    _FakeRebuild._session_mode_rebuild_delta = MCLinkApp._session_mode_rebuild_delta
    restored = _FakeRebuild(running=True, session_mode="stun", room_mode="stun")

    check(
        "★ restored（会话已在新模式上）-> 不请求任何重建（不是'有人在乱改'）",
        MCLinkApp._session_mode_rebuild_delta(restored) is None
        and restored.rebuilds == [],
        f"delta={MCLinkApp._session_mode_rebuild_delta(restored)} "
        f"rebuilds={restored.rebuilds}",
    )


def _names_local(events):
    return [name for name, _data in events]


# ============================================================
# 5. was_live：等待池的 ERROR CONN_MODE_CHANGED … was_live=
# ============================================================

def test_conn_mode_changed_line_parse():
    print("")
    print("---- 5. §8.9 CONN_MODE_CHANGED 两种形式 + was_live ----")

    control = proto.parse_conn_mode_changed(
        "CONN_MODE_CHANGED 连接模式已切换，请按新模式重新开始连接 was_live=0"
    )
    waiting = proto.parse_conn_mode_changed(
        "ERROR CONN_MODE_CHANGED 连接模式已切换，请按新模式重新开始连接 was_live=1"
    )
    no_field = proto.parse_conn_mode_changed("CONN_MODE_CHANGED 切换了")

    check(
        "★ 成员控制连接形式（无 ERROR 前缀）+ was_live=0",
        control == ("连接模式已切换，请按新模式重新开始连接", "0"),
        repr(control),
    )
    check(
        "★ 等待池形式（ERROR 前缀）+ was_live=1",
        waiting == ("连接模式已切换，请按新模式重新开始连接", "1"),
        repr(waiting),
    )
    check(
        "★ 老中继没有 was_live -> \"\"（界面退回本地 running）",
        no_field == ("切换了", ""),
        repr(no_field),
    )
    check(
        "★ 不是 CONN_MODE_CHANGED 的行 -> None（绝不误吃别的 ERROR）",
        proto.parse_conn_mode_changed("ERROR NO_HOST") is None
        and proto.parse_conn_mode_changed("ERROR CONN_MODE_CHANGES x") is None
        and proto.parse_conn_mode_changed("") is None,
        "",
    )


def test_player_waiting_pool_consumes_was_live():
    print("")
    print("---- 5b. 真 PLAYER 等待池：ERROR CONN_MODE_CHANGED 被消费 ----")

    events = []

    async def go():
        received = []

        async def relay_handler(reader, writer):
            try:
                line = (await asyncio.wait_for(reader.readline(), 5)).decode(
                    errors="replace"
                ).strip()
                received.append(line)
                writer.write(
                    (
                        "ERROR CONN_MODE_CHANGED 连接模式已切换，请按新模式重新"
                        "开始连接 was_live=1\n"
                    ).encode()
                )
                await writer.drain()
            except Exception:
                pass
            finally:
                with contextlib.suppress(Exception):
                    writer.close()

        relay = await asyncio.start_server(relay_handler, "127.0.0.1", 0)
        relay_port = relay.sockets[0].getsockname()[1]

        async def mc_handler(_reader, writer):
            await asyncio.sleep(3)
            with contextlib.suppress(Exception):
                writer.close()

        mc = await asyncio.start_server(mc_handler, "127.0.0.1", 0)
        mc_port = mc.sockets[0].getsockname()[1]

        core = player_core.PlayerCore(
            "127.0.0.1",
            relay_port,
            "room-1",
            local_host="127.0.0.1",
            local_port=0,
            client_id="cid-p",
            username="p",
            on_event=lambda name, data: events.append((name, data)),
            on_log=lambda _m: None,
        )

        mc_reader, mc_writer = await asyncio.open_connection(
            "127.0.0.1", mc_port
        )

        try:
            await asyncio.wait_for(
                core.handle_minecraft(mc_reader, mc_writer), timeout=8
            )
        except asyncio.TimeoutError:
            pass

        with contextlib.suppress(Exception):
            mc_writer.close()
            relay.close()
            mc.close()

        return received

    received = asyncio.run(go())

    changed = [d for n, d in events if n == "conn_mode_changed"]

    check(
        "★ PLAYER 握手行照旧是 PLAYER 角色（等待池路径没被改坏）",
        received and received[0].startswith("PLAYER "),
        repr(received),
    )
    check(
        "★ 等待池的 ERROR CONN_MODE_CHANGED 带 was_live=1 -> 核心 emit 同一事件",
        len(changed) == 1 and str(changed[0].get("was_live")) == "1",
        repr(changed),
    )
    check(
        "★ 而且它照样是「错误」语义：relay_error 也照旧发出（老行为不变）",
        any(n == "relay_error" for n, _d in events),
        repr(_names_local(events)),
    )


def test_host_tunnel_waiting_pool_consumes_was_live():
    print("")
    print("---- 5c. 真 HOST_TUNNEL 等待池：ERROR CONN_MODE_CHANGED 被消费 ----")

    events = []
    received = []

    async def go():
        async def relay_handler(reader, writer):
            try:
                line = (await asyncio.wait_for(reader.readline(), 5)).decode(
                    errors="replace"
                ).strip()
                received.append(line)

                if line.startswith("HOST_TUNNEL"):
                    writer.write(b"WAIT\n")
                    await writer.drain()
                    writer.write(
                        (
                            "ERROR CONN_MODE_CHANGED 连接模式已切换，请按新模式"
                            "重新开始连接 was_live=0\n"
                        ).encode()
                    )
                    await writer.drain()
                    await asyncio.sleep(1)
                elif line.startswith("HOST "):
                    writer.write(b"OK HOST room=room-1\n")
                    await writer.drain()
                    await asyncio.sleep(3)
            except Exception:
                pass
            finally:
                with contextlib.suppress(Exception):
                    writer.close()

        relay = await asyncio.start_server(relay_handler, "127.0.0.1", 0)
        relay_port = relay.sockets[0].getsockname()[1]

        core = host_core.HostCore(
            "127.0.0.1",
            relay_port,
            "room-1",
            mc_host="127.0.0.1",
            mc_port=1,
            max_tunnels=1,
            max_players=2,
            client_id="cid-h",
            username="h",
            reconnect_delay=0.2,
            on_event=lambda name, data: events.append((name, data)),
            on_log=lambda _m: None,
        )

        try:
            await core.start()

            for _ in range(60):
                if any(n == "conn_mode_changed" for n, _d in events):
                    break

                await asyncio.sleep(0.1)
        finally:
            with contextlib.suppress(Exception):
                await core.stop()

            relay.close()

    asyncio.run(go())

    changed = [d for n, d in events if n == "conn_mode_changed"]

    check(
        "★ 隧道握手行照旧是 HOST_TUNNEL / HOST（冻结握手没动）",
        any(line.startswith("HOST_TUNNEL ") for line in received)
        and any(line.startswith("HOST ") for line in received),
        repr(received),
    )
    check(
        "★ HOST_TUNNEL 等待池的 was_live=0 -> 核心 emit 同一事件（同一消费者）",
        len(changed) >= 1 and str(changed[0].get("was_live")) == "0",
        repr(changed),
    )


# ============================================================
# 6. HOST_TAKEN：认领被拒 = 身份仍在（不是"没有 host"）
# ============================================================

def test_host_taken_keeps_identity():
    print("")
    print("---- 6. HOST_TAKEN：认领被拒后 host 位仍是'有人' ----")

    events = []

    async def go():
        session = roommod.RoomSession(
            "127.0.0.1", 1, "cid-me", "me", token="t",
            emit=lambda name, data: events.append((name, data)),
            is_current=lambda: True,
        )

        # 身份事实：B 占着 host 位（但连接是 idle = 未开服）。
        session.state.host_claimed = True
        session.state.host_name = "B"
        session.state.host_id = "cid-b"
        session.state.member_conns = {"cid-b": "idle"}
        session.state.peer_ids = ["cid-b"]
        session.state.members = 2

        future = asyncio.get_running_loop().create_future()
        session._pending["CLAIM_HOST"] = future

        session._on_line("ERROR HOST_TAKEN 房间已有房主（B），无法重复认领")

        return session, future

    session, future = asyncio.run(go())

    check(
        "★ 认领被拒：future 回 (False, 服务器的中文原因)",
        future.done()
        and future.result()[0] is False
        and "已有房主" in future.result()[1],
        repr(future.result() if future.done() else None),
    )
    check(
        "★ is_host 变 False（我不是 host），但**身份没被清**："
        "host_claimed / host_name / host_id 全部保留",
        session.state.is_host is False
        and session.state.host_claimed is True
        and session.state.host_name == "B"
        and session.state.host_id == "cid-b",
        repr((session.state.is_host, session.state.host_claimed,
              session.state.host_name, session.state.host_id)),
    )
    check(
        "★ 界面收到 host_taken（带服务器的原因），不是'暂无 Host'",
        [n for n, _d in events] == ["host_taken"]
        and "已有房主" in events[0][1].get("reason", ""),
        repr(events),
    )
    check(
        "★ host_availability 仍是 host_not_connected（identity gate），"
        "绝不回 no_host（否则认领按钮会亮）",
        pf.host_availability(session.state, "cid-me") == pf.HOST_NOT_CONNECTED,
        repr(pf.host_availability(session.state, "cid-me")),
    )


# ============================================================
# 7. 界面侧：session_ended / host_taken 的按钮与提示
# ============================================================

class _GuiRoomState:
    def __init__(self, **kw):
        self.room = "r"
        self.room_name = "r"
        self.display = "r"
        self.client_id = "cid-me"
        self.username = "root"
        self.own_name = "root"
        self.self_name = "root"
        self.is_owner = True
        self.is_host = False
        self.host_claimed = False
        self.host_name = ""
        self.host_id = ""
        self.members = 1
        self.peer_ids = []
        self.member_names = {"cid-me": "root"}
        self.member_rtts = {}
        self.member_conns = {}
        self.world_players = 1
        self.max_players = 4
        self.server_max_players = 8
        self.conn_mode = "relay"
        self.mode_reason = "default"
        self.started_ids = None
        self.owner_id = "cid-me"
        self.revoked = False
        self.revoked_reason = ""
        self.joined_at = time.time()

        for key, value in kw.items():
            setattr(self, key, value)


class _FakeEngine:
    def __init__(self):
        self.room = None
        self.stops = 0
        self.session_mode_value = ""
        self.punch_enabled = []
        self.closed = 0
        self.claim_result = (False, "房间已有房主（B），无法重复认领")

    def room_state(self):
        return self.room

    def room_current(self):
        return None

    def session_mode(self):
        return self.session_mode_value

    def stop_session(self, timeout=15):
        self.stops += 1
        self.session_mode_value = ""
        return True

    def close_room(self):
        self.closed += 1

    def claim_host(self, timeout=15):
        return self.claim_result

    def watch_room_nat(self, enabled, log=None, standalone_host=None):
        self.punch_enabled.append(("nat", bool(enabled)))

    def watch_room_direct(self, enabled, log=None):
        self.punch_enabled.append(("direct", bool(enabled)))

    def room_direct_state(self):
        return {}

    def room_nat_profile(self):
        return None

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

    def __getattr__(self, name):
        def stub(*_a, **_k):
            return None

        return stub


class _GuiHarness:
    def __init__(self):
        self.app = None
        self.logs = []
        self.warnings = []

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

        app.engine = _FakeEngine()
        app.log = lambda text, kind="normal": self.logs.append(str(text))
        app.config = types.SimpleNamespace(
            username="root", role="host", server_host="relay.example",
            client_id="cid-me", mode="relay",
        )
        app.running = True
        app._committed["mc_port"] = 25565
        app._committed["local_port"] = 25565
        app._committed["max_players"] = 4
        app.mc_port_var.set("25565")
        app.local_port_var.set("25565")
        app.max_players_var.set("4")
        app.room_var.set("r")
        app.auth = types.SimpleNamespace(username="root", token="tok")

        self.app = app
        return True

    def close(self):
        if self.app is None:
            return

        with contextlib.suppress(Exception):
            self.root.destroy()


def test_gui_session_ended_never_reconnects(harness):
    print("")
    print("---- 7. 界面：session_ended 一律按退出房间处理（Host/Player 同理）----")

    app = harness.app

    for role in ("host", "player"):
        app.config.role = role
        app.running = True
        app.auth = types.SimpleNamespace(username="root", token="tok")
        app._reconnect_phase = "running"
        app._reconnect_started_at = 0.0
        app._reconnect_attempt = 0
        app._rebuild_in_flight = False
        app._stopping_on_purpose = False
        # 进新房间时界面本来就会复位这个"已经告知过"标记（room_joined）。
        app._room_close_notified = False
        harness.warnings.clear()
        harness.logs.clear()

        app._handle_core_event(
            "session_ended",
            {
                "reason": "connection reset",
                "code": "KICKED",
                "relogin": False,
                "detail": "ERROR KICKED",
            },
        )

        check(
            f"★ [{role}] 被踢（SESSION 探活）-> 不重连（running=False，没有调度）",
            app.running is False
            and app._trigger_auto_reconnect("after session_ended") is False,
            f"running={app.running}",
        )
        check(
            f"★ [{role}] 界面提示是「被踢」而不是普通掉线",
            bool(harness.warnings)
            and "踢" in harness.warnings[-1][0] + harness.warnings[-1][1],
            repr(harness.warnings[-1:]),
        )
        check(
            f"★ [{role}] 房间被清干净（close_room 调过 + 房间名清空）",
            app.engine.closed >= 1 and app.room_var.get() == "",
            f"closed={app.engine.closed} room={app.room_var.get()!r}",
        )
        check(
            f"★ [{role}] 日志是英文（i18n 硬要求）",
            any("SESSION probe" in line and "no automatic reconnect" in line
                for line in harness.logs),
            repr(harness.logs[-3:]),
        )

    # 已经通过被动推送（KICKED / ROOM REVOKED）告知过用户 -> 探活的
    # session_ended 只负责对齐状态，**不许再弹第二个对话框**。
    app.running = True
    app._room_close_notified = True
    harness.warnings.clear()
    app._handle_core_event(
        "session_ended",
        {"reason": "x", "code": "KICKED", "relogin": False, "detail": ""},
    )

    check(
        "★ 已经被告知过（KICKED 推送先到）-> session_ended 只对齐状态、不重复弹窗",
        not harness.warnings and app.running is False,
        repr(harness.warnings),
    )


def test_gui_session_ended_relogin(harness):
    print("")
    print("---- 7b. 界面：relogin=1 -> 提示需要重新登录（且只弹一次）----")

    app = harness.app
    app.config.role = "player"
    app.running = True
    app.auth = types.SimpleNamespace(username="root", token="tok")
    app._reconnect_phase = "running"
    app._reconnect_started_at = 0.0
    app._room_close_notified = False
    harness.warnings.clear()
    harness.logs.clear()

    app._handle_core_event(
        "session_ended",
        {
            "reason": "connection reset",
            "code": "AUTH_INVALID_TOKEN",
            "relogin": True,
            "detail": "登录状态已失效，请重新登录 [AUTH_INVALID_TOKEN]",
        },
    )

    check(
        "★ token 失效 -> 登录态被清掉（需要重新登录）",
        app.auth is None,
        repr(app.auth),
    )
    check(
        "★ 只弹一次「需要重新登录」（不叠第二个对话框）",
        len(harness.warnings) == 1
        and "重新登录" in harness.warnings[0][0],
        repr(harness.warnings),
    )
    check(
        "★ 而且照旧不自动重连",
        app.running is False
        and app._trigger_auto_reconnect("after relogin") is False,
        f"running={app.running}",
    )


def test_gui_host_taken_not_free(harness):
    print("")
    print("---- 7c. 界面：认领被拒 -> 提示正确 + host 位不当成空闲 ----")

    app = harness.app
    harness.warnings.clear()
    harness.logs.clear()

    # 身份在（B 占着），但连接是 idle（未开服）—— 认领按钮必须还是灰的。
    app.engine.room = _GuiRoomState(
        is_owner=False,
        is_host=False,
        host_claimed=True,
        host_name="B",
        host_id="cid-b",
        member_conns={"cid-b": "idle"},
        peer_ids=["cid-b"],
        members=2,
    )
    app.running = False
    app._refresh_room_ui()

    check(
        "★ host 位有人、连接 idle -> 「认领主机身份」仍然 disabled（身份优先）",
        str(app.claim_host_button["state"]) == "disabled",
        str(app.claim_host_button["state"]),
    )

    # 点认领 -> 服务器 HOST_TAKEN -> 提示必须带服务器的原因（不是"暂无 Host"）。
    app.on_claim_host()

    check(
        "★ 认领被拒的弹窗照搬服务器原因（房间已有房主（B）…）",
        bool(harness.warnings)
        and "已有房主" in harness.warnings[-1][1],
        repr(harness.warnings[-1:]),
    )

    app._handle_core_event("host_taken", {"reason": "房间已有房主（B），无法重复认领"})

    check(
        "★ host_taken 事件只刷新界面，不会把 host 位改成空闲",
        getattr(app.engine.room, "host_claimed", True) is True
        and str(app.claim_host_button["state"]) == "disabled",
        f"claimed={getattr(app.engine.room, 'host_claimed', None)} "
        f"button={app.claim_host_button['state']}",
    )


# ============================================================

def main():
    print("=" * 66)
    print(" CraftLink —— SESSION 探活 / mode_reason / was_live / HOST_TAKEN")
    print("=" * 66)

    test_session_protocol_and_verdicts()
    test_probe_uses_a_real_short_lived_connection()
    test_explainer_decides_from_the_probe()
    test_mode_reason_parsing_and_attribution()
    test_conn_mode_changed_line_parse()
    test_player_waiting_pool_consumes_was_live()
    test_host_tunnel_waiting_pool_consumes_was_live()
    test_host_taken_keeps_identity()

    try:
        import tkinter  # noqa: F401
    except ImportError as e:
        check("tkinter available", False, str(e))
        return 1

    harness = _GuiHarness()

    if not harness.build():
        check("真界面能构建（需要 X display）", False, "Tk 建不起来")
        return 1

    try:
        test_gui_session_ended_never_reconnects(harness)
        test_gui_session_ended_relogin(harness)
        test_gui_host_taken_not_free(harness)
    finally:
        harness.close()

    print("")
    print("=" * 66)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 66)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
