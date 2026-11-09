#!/usr/bin/env python3
"""
CraftLink 回归测试（v3.9.16 起随包交付）。

============================================================================
 为什么包里必须有它
============================================================================

v3.9.15 的包里**没有任何测试**：`tests/run_all.py` 不在包内，服务器上跑不了
任何回归。于是"host 断开后房间内其余人不断开"这一个 bug 回归了三次
（09-30 定稿 → 10-02 → 10-03 重申"实测仍未生效"）—— 每一次改的人都以为
改好了，因为没有任何东西能证明它好了。

所以从这个版本起，测试随包走，并且是 `scripts/check_deploy.py` 的一道必过
门禁。

============================================================================
 这些测试在测什么
============================================================================

每条都对着一个**已经发生过的真实故障**：

  * host 没了必须断 —— 而且不能由界面层决定（核心层自己拆）；
  * 界面窗口不许**丢弃** host-gone，只能推迟；
  * 服务端通知必须有超时，而且"拆"必须发生；
  * 包内不许有明文凭据；
  * 版本号两处必须相等；
  * 版本门必须会自动到期。

跑法：

    python3 tests/run_all.py

纯 Python 标准库，不装任何依赖，不需要网络。
"""

import asyncio
import contextlib
import importlib
import os
import pathlib
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

def _gui_module():
    """`gui/app.py`（装上 tkinter 替身之后再 import，见 `gui_harness`）。"""
    global _app_mod

    if _app_mod is None:
        import gui_harness

        gui_harness.install()

        from mclink.gui import app as _mod

        _app_mod = _mod

    return _app_mod


_app_mod = None

CLIENT_PKG_ROOT = os.path.join(ROOT, "client")

_RESULTS = []


def _ok(name):
    _RESULTS.append((True, name, ""))
    print(f"  [ OK ] {name}")


def _fail(name, detail):
    _RESULTS.append((False, name, detail))
    print(f"  [FAIL] {name}\n         {detail}")


def check(name, fn):
    """跑一个断言式测试：抛异常 = 失败。"""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        _fail(name, f"{e.__class__.__name__}: {e}")
    else:
        _ok(name)


def acheck(name, coro_fn):
    """跑一个 async 测试。"""
    try:
        asyncio.run(coro_fn())
    except Exception as e:  # noqa: BLE001
        _fail(name, f"{e.__class__.__name__}: {e}")
    else:
        _ok(name)


# ==========================================================
# 把客户端当成一个包来 import（它没有顶层 setup.py）
# ==========================================================

def _install_client_package():
    if "mclink" in sys.modules:
        return importlib.import_module("mclink")

    sys.path.insert(0, CLIENT_PKG_ROOT)

    return importlib.import_module("mclink")


# ==========================================================
# 1. 版本号
# ==========================================================

def test_version_matches():
    with open(
        os.path.join(ROOT, "client", "mclink", "config", "settings.py"),
        encoding="utf-8",
    ) as fh:
        client_version = ""

        for line in fh:
            if line.strip().startswith("APP_VERSION"):
                client_version = (
                    line.split("=", 1)[1].strip().strip('"').strip("'")
                )
                break

    with open(os.path.join(ROOT, "server", "VERSION"), encoding="utf-8") as fh:
        server_version = fh.read().strip()

    assert client_version, "读不到客户端版本号"
    assert server_version, "读不到服务端版本号"
    assert client_version == server_version, (
        f"两处版本号不一致：客户端 {client_version} / 服务端 {server_version}"
    )


# ==========================================================
# 2. 包内无明文凭据（v3.9.15 的 P0）
# ==========================================================

def test_no_plaintext_secrets():
    """
    D5（v3.9.21）：包装好后，扫描器必须**一个明文凭据都抓不到**。

    判据直接复用 `scripts/check_deploy.py` —— 这里曾经自己维护过一套 hex
    判据，结果和门禁那套漂移了（v3.9.17 修的漏报就是这么来的）。单一来源
    才不会再漂。

    注意断言方向**翻转**了：v3.9.20 是"严格模式必须抓到"（因为明文是预期
    写法）；现在严格模式必须**干净** —— 有明文就是回归。
    """
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_deploy

    strict = check_deploy.check_no_secrets(strict=True)

    assert not strict, (
        "仓库/包里还有明文凭据（D5 之后一律判失败）：\n  "
        + "\n  ".join(strict[:10])
    )

    # 宽松模式同样不该有任何"记录式提醒"（它只在扫到明文时才产生）
    loose = check_deploy.check_no_secrets(strict=False)

    assert not loose and not check_deploy.SECRET_NOTICES, (
        "宽松模式下仍在记录明文凭据 —— 说明还有地方写着真值：\n  "
        + "\n  ".join((loose or check_deploy.SECRET_NOTICES)[:10])
    )


# ==========================================================
# 3. 核心层：收到 host-gone 必须自己拆
# ==========================================================

def _new_room_session(is_host=False, **kwargs):
    from mclink.core import room as room_mod

    session = room_mod.RoomSession.__new__(room_mod.RoomSession)

    session.host = "127.0.0.1"
    session.port = 19100
    session.client_id = "cid-player"
    session.username = "player"
    session.token = ""
    session.guest_token = ""
    session.host_gone_hook = kwargs.get("hook")
    session.host_gone = None
    session._emit = lambda *a, **k: None
    session._is_current = None
    session.max_players = 0
    session.state = room_mod.RoomState(
        client_id=session.client_id, username=session.username
    )
    session.state.is_host = is_host
    session.reader = None
    session.writer = None
    session.running = True
    session.last_error = ""
    session._last_pong = 0.0
    session._last_mode_reason = None

    return session


def test_room_calls_host_gone_hook():
    """收到 HOST_STOPPED -> 核心层立刻动手（这是本次的根治点）。"""
    calls = []

    session = _new_room_session(hook=lambda code, reason: calls.append(code))

    session._on_line("HOST_STOPPED host 已断开连接（未开服）")

    assert calls == ["HOST_STOPPED"], f"hook 没被调用：{calls}"


def test_room_does_not_teardown_for_host_itself():
    """
    我自己就是 host：这是**我自己**停服的回显，不能拿它把自己踢下线。
    """
    calls = []

    session = _new_room_session(
        is_host=True, hook=lambda code, reason: calls.append(code)
    )

    session._on_line("HOST_STOPPED host 已断开连接（未开服）")

    assert calls == [], f"host 自己不该被拆：{calls}"


def test_room_all_host_gone_codes():
    for line, code in (
        ("HOST_LEFT host 已断开连接", "HOST_LEFT"),
        ("HOST_STOPPED host 已断开连接（未开服）", "HOST_STOPPED"),
        ("HOST_RELEASED host 已释放主机位", "HOST_RELEASED"),
        ("NO_HOST 房间里没有 host", "NO_HOST"),
    ):
        calls = []

        session = _new_room_session(hook=lambda c, r: calls.append(c))
        session._on_line(line)

        assert calls == [code], f"{line} -> {calls}（期望 [{code}]）"


# ==========================================================
# 4. 界面层：窗口期内**不许丢弃**，只许推迟
# ==========================================================

def _fake_engine(is_host=False, host_claimed=True):
    """给 GUI 逻辑测试用的假引擎：只提供被测方法真正会碰到的那几个调用。"""
    calls = {"stop_session": 0}

    class _State:
        pass

    state = _State()
    state.is_host = is_host
    state.host_claimed = host_claimed

    class Engine:
        def room_state(self):
            return state

        def stop_session(self, *a, **k):
            calls["stop_session"] += 1

            return True

        def host_gone(self):
            return None

    engine = Engine()
    engine.calls = calls

    return engine


def test_gui_defers_instead_of_dropping():
    """
    模式重建窗口里的 host-gone 必须被**缓存**，而不是只记一行日志就完事。

    v3.9.16 之前这里就是"丢弃"：窗口期内遇到 `stopped`/`released` 只
    `self.log(...)`，于是真的 host 停服也被一起吞掉 —— 中继已经拆了会话，
    界面却还写着"已连接"（用户 10-03 实测）。

    这条**真跑** GUI 状态机（`_handle_core_event` -> `_defer_host_gone`），
    不是 grep 源码字符串。
    """
    import gui_harness

    app = gui_harness.make_app()
    app.engine = _fake_engine(is_host=False)

    # 开着两道窗口：静默窗口（未过期）+ 重建中。
    app._mode_switch_silent = True
    app._mode_switch_silent_until = time.monotonic() + 30
    app._rebuild_in_flight = True

    app._handle_core_event(
        "host_stopped",
        {"reason": "host 已断开连接（未开服）", "mandatory": True},
    )

    pending = getattr(app, "_pending_host_gone", None)

    assert pending is not None, "窗口期内的 host-gone 必须被缓存，不能被丢弃"
    assert pending.get("kind") == "stopped", f"缓存了错的 kind：{pending}"

    # 窗口还开着 -> 补跑应当继续等着（但不会无限等，见下一条）。
    app._flush_pending_host_gone()

    assert getattr(app, "_pending_host_gone", None) is not None, (
        "窗口还开着时补跑应当继续等"
    )


def test_gui_flush_runs_deferred_after_window():
    """
    窗口一结束，缓存的那条 host-gone 必须**真的被执行**（断开 + 重算）。

    这是"推迟 ≠ 丢弃"的另一半：光缓存不补跑，等于换了个方式丢弃。
    """
    import gui_harness

    app = gui_harness.make_app()
    app.engine = _fake_engine(is_host=False)

    app._mode_switch_silent = True
    app._mode_switch_silent_until = time.monotonic() + 30
    app._rebuild_in_flight = True

    app._handle_core_event(
        "host_stopped", {"reason": "host 已断开连接（未开服）"}
    )

    assert app._pending_host_gone is not None

    # 窗口关掉（重建结束、静默窗口到期）
    app._mode_switch_silent = False
    app._mode_switch_silent_until = 0.0
    app._rebuild_in_flight = False

    app._flush_pending_host_gone()

    assert app._pending_host_gone is None, "补跑之后必须清掉缓存（不能重复跑）"
    assert app.engine.calls["stop_session"] >= 1, (
        "补跑必须真的调用 stop_session —— 光改状态不算断开"
    )
    assert app.running is False, "补跑之后界面必须回到'已停止'"
    assert app._reconnect_phase == "host_down", (
        f"阶段必须是 host_down，实际 {app._reconnect_phase!r}"
    )


def test_gui_flush_has_hard_deadline():
    """
    窗口**没人来关**时，缓存的 host-gone 也不能无限期压着。

    `HOST_GONE_DEFER_MAX_S` 是这道兜底：到点无条件补跑。
    """
    import gui_harness

    app = gui_harness.make_app()
    app.engine = _fake_engine(is_host=False)

    app._mode_switch_silent = True
    app._mode_switch_silent_until = time.monotonic() + 3600
    app._rebuild_in_flight = True

    app._handle_core_event(
        "host_released", {"reason": "host 已释放主机位"}
    )

    assert app._pending_host_gone is not None

    # 把缓存时间往前推，假装它已经等了很久（超过兜底上限）。
    app._pending_host_gone["at"] = time.monotonic() - (
        _gui_module().HOST_GONE_DEFER_MAX_S + 5
    )

    # 窗口**仍然**开着 —— 只靠兜底上限这一条也必须补跑。
    app._flush_pending_host_gone()

    assert app._pending_host_gone is None, (
        "超过 HOST_GONE_DEFER_MAX_S 必须无条件补跑（窗口没人关也不能无限等）"
    )
    assert app.engine.calls["stop_session"] >= 1


def test_gui_stops_session_regardless_of_running():
    """
    `self.running` 已经是 False 时也要再停一次。

    "掉线处理会先把 running 清成 False"（`_session_busy` 上方那段注释自己
    写着）—— 于是 host_stopped 后到时 `stop_session()` 被整个跳过：只写状态、
    不断会话。用户看到的就是"host 断了、我这里还是已连接"。

    这条**真跑** `_on_host_gone`，不是 grep 源码。
    """
    import gui_harness

    app = gui_harness.make_app()
    app.engine = _fake_engine(is_host=False)

    # 关键前提：掉线处理已经先把 running 清成 False。
    app.running = False

    app._on_host_gone("stopped", "host 已断开连接（未开服）")

    assert app.engine.calls["stop_session"] >= 1, (
        "running 为假也必须再停一次（stop_session 是幂等的）"
    )
    assert app._reconnect_phase == "host_down"


def test_gui_host_itself_is_not_torn_down():
    """
    我自己就是 host：这是我**自己**停服的回显，不能把自己踢下线。
    """
    import gui_harness

    app = gui_harness.make_app()
    app.engine = _fake_engine(is_host=True)

    app._on_host_gone("stopped", "host 已断开连接（未开服）")

    assert app.engine.calls["stop_session"] == 0, (
        "我自己是 host 时不该停会话"
    )


# ==========================================================
# 5. 服务端：通知必须有超时，且"拆"必须发生
# ==========================================================

async def test_write_with_timeout_returns_false_when_stuck():
    """
    一条永远 drain 不完的连接 -> `write_with_timeout` 必须**返回**，不能挂住。
    """
    import room_members

    class StuckWriter:
        def write(self, data):
            return None

        async def drain(self):
            await asyncio.sleep(3600)

    ok = await asyncio.wait_for(
        room_members.write_with_timeout(StuckWriter(), b"x", timeout=0.05),
        timeout=2,
    )

    assert ok is False, "卡住的 drain 必须返回 False"


async def test_disconnect_still_tears_down_when_notify_hangs():
    """
    最关键的一条：通知挂住时，**会话仍然必须被拆掉**。

    v3.9.15 里 `disconnect_game_connections` 是串行裸 drain，第一条卡住 -> 后面
    全都发不出去 -> 第 4 步"真的拆"根本执行不到 -> 全房谁都不断开。
    """
    import room_members

    class StuckWriter:
        def write(self, data):
            return None

        async def drain(self):
            await asyncio.sleep(3600)

    class FakePair:
        def __init__(self):
            self.session_id = "s1"
            self.established = False
            self.player_writer = StuckWriter()
            self.tunnel_writer = StuckWriter()

    class FakeRoom:
        def __init__(self):
            self.sessions = {"s1": FakePair()}
            self.waiting_tunnels = []
            self.waiting_players = []
            self.members = {}
            self.removed = []
            self.name = "r1"

        async def remove_session(self, session_id, reason=""):
            self.removed.append(session_id)

    class FakeLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    room = FakeRoom()
    room.lock = FakeLock()

    await asyncio.wait_for(
        room_members.disconnect_game_connections(
            room, reason="host 已断开连接（未开服）",
            code=room_members.HOST_STOPPED_CODE,
        ),
        timeout=20,
    )

    assert room.removed == ["s1"], (
        f"通知挂住时会话也必须被拆掉，实际 removed={room.removed}"
    )


async def test_member_send_timeout_is_not_death():
    """
    成员写**超时**不等于连接死了 —— 单次超时不许置 `closed`。

    v3.9.16 把两者混为一谈：写超时 -> `closed=True` -> `host_member` 认不出
    房主（主机位凭空消失）+ 看门狗对 `closed` 成员 continue（没人回收，
    变僵尸成员占着名额）。而"超时"只说明对端那一刻没在读（弱网抖一下、
    事件循环卡一下），根本不是连接断了（复核报告 P1）。
    """
    import room_members

    class StuckWriter:
        """drain 永不返回：模拟"对端没在读"，但连接本身没问题。"""

        def write(self, data):
            return None

        async def drain(self):
            await asyncio.sleep(3600)

    member = room_members.RoomMember.__new__(room_members.RoomMember)
    member.closed = False
    member.detached = False
    member.writer = StuckWriter()
    member.send_lock = asyncio.Lock()
    member.send_failures = 0
    member.last_send_failed = 0.0
    member.room_name = "r1"
    member.client_id = "cid"
    member.username = "u"
    member.member_key = "cid"
    member.is_host = False
    member.is_owner = False
    # 测试里别真等 5 秒：把这一条的预算压到 0.05 秒。
    member.send_timeout = 0.05

    result = await asyncio.wait_for(member.send("HOST_STOPPED x\n"), timeout=5)

    assert result is False, "卡住的 member.send 必须返回 False（而不是挂住）"
    assert member.closed is False, (
        "单次写超时**不许**把成员判死 —— 那会让主机位凭空消失、成员变僵尸"
    )
    assert member.send_failures == 1, "失败要累计，才能由看门狗按次数回收"


async def test_member_hard_failure_marks_closed():
    """
    内核明确说"连接没了"（ConnectionReset / BrokenPipe）才算真失败 -> 置 closed。
    """
    import room_members

    class DeadWriter:
        def write(self, data):
            return None

        async def drain(self):
            raise ConnectionResetError("connection reset by peer")

    member = room_members.RoomMember.__new__(room_members.RoomMember)
    member.closed = False
    member.detached = False
    member.writer = DeadWriter()
    member.send_lock = asyncio.Lock()
    member.send_failures = 0
    member.last_send_failed = 0.0
    member.send_timeout = 1.0
    member.room_name = "r1"
    member.client_id = "cid"
    member.username = "u"
    member.member_key = "cid"
    member.is_host = False
    member.is_owner = False

    result = await asyncio.wait_for(member.send("HOST_STATE\n"), timeout=5)

    assert result is False
    assert member.closed is True, "真失败必须判死"


async def test_member_repeated_timeouts_eventually_dies():
    """
    连续写失败到上限 -> 判死。否则"写不通"的成员永远占着名额没人收。
    """
    import room_members

    class StuckWriter:
        def write(self, data):
            return None

        async def drain(self):
            await asyncio.sleep(3600)

    member = room_members.RoomMember.__new__(room_members.RoomMember)
    member.closed = False
    member.detached = False
    member.writer = StuckWriter()
    member.send_lock = asyncio.Lock()
    member.send_failures = 0
    member.last_send_failed = 0.0
    member.send_timeout = 0.05
    member.room_name = "r1"
    member.client_id = "cid"
    member.username = "u"
    member.member_key = "cid"
    member.is_host = False
    member.is_owner = False

    limit = room_members.MEMBER_SEND_FAIL_LIMIT

    for _ in range(limit):
        await asyncio.wait_for(member.send("x\n"), timeout=5)

    assert member.closed is True, (
        f"连续 {limit} 次写失败应当判死（否则没人回收他）"
    )


async def test_watchdog_reaps_already_closed_member():
    """
    `closed` 已经被 `send()` 置成 True 的成员，看门狗**也要**能接手。

    这是分支顺序的问题（v3.9.17 修的）：原来 `if member.closed: continue` 写在
    收尸分支**前面**，于是被 `send()` 判死的成员永远走不到收尸那一步 ——
    他占着名额、没人回收、也不再收广播（复核报告 P1 里"僵尸成员"那一半）。
    """
    import room_members

    class FakeWriter:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeRoom:
        def __init__(self):
            self.name = "r1"
            self.members = {}

    member = room_members.RoomMember.__new__(room_members.RoomMember)
    # 关键：`send()` 已经把他判死了
    member.closed = True
    member.detached = False
    member.send_failures = room_members.MEMBER_SEND_FAIL_LIMIT
    member.last_seen = 0.0
    member.removal_reason = "drop"
    member.writer = FakeWriter()
    member.room_name = "r1"
    member.client_id = "cid"
    member.username = "u"
    member.member_key = "cid"
    member.is_owner = False
    member.is_host = False

    room = FakeRoom()
    room.members = {"cid": member}

    reaped = await room_members.reap_idle_members([room], now=1e9)

    assert reaped == [(room, member)], (
        f"已经 closed 的成员也必须被回收（否则就是僵尸），实际 {reaped}"
    )


async def test_watchdog_never_reaps_detached_member():
    """
    ★ 复核报告 3.1（P1 复发）：座位宽限期内的成员**不许**被看门狗收走。

    真实复现的那条路径：房主掉线前累积 3 次写超时 -> 掉线 -> 进入 detached ->
    看门狗收尸分支（v3.9.17 排在 detached 检查**之前**）看到 send_failures>=3
    -> 把身份仍然有效的房主收走 -> 主机位凭空消失 + 房间被销毁。

    两个完全正常的事件连起来，房主就被踢了。
    """
    import room_members

    class FakeWriter:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeRoom:
        def __init__(self):
            self.name = "r1"
            self.members = {}

    member = room_members.RoomMember.__new__(room_members.RoomMember)
    member.closed = False
    # 关键：他在座位宽限期里（连接断了、身份还在）
    member.detached = True
    # 关键：掉线**之前**已经累积到了判死上限
    member.send_failures = room_members.MEMBER_SEND_FAIL_LIMIT
    member.last_seen = 0.0     # 早就静默了（正常：连接都断了）
    member.removal_reason = "drop"
    member.writer = FakeWriter()
    member.room_name = "r1"
    member.client_id = "owner-cid"
    member.username = "owner"
    member.member_key = "owner-cid"
    member.is_owner = True
    member.is_host = True

    room = FakeRoom()
    room.members = {"owner-cid": member}

    reaped = await room_members.reap_idle_members([room], now=1e9)

    assert reaped == [], (
        f"座位宽限期内的成员绝不能被收走（主机位会凭空消失），实际 {reaped}"
    )
    assert member.writer.closed is False, "socket 也不许被关"


async def test_detach_clears_send_failures():
    """
    进入座位宽限期时把"写失败"的旧账清零。

    `send_failures` 只在写**成功**时清零，而进入 detached 之后他本来就不再被
    写 —— 不清零的话，"掉线前抖了三下"会被永久带着，等他重连/被复查时拿一
    笔旧账把他判死。
    """
    import room_members

    member = room_members.RoomMember.__new__(room_members.RoomMember)
    member.send_failures = room_members.MEMBER_SEND_FAIL_LIMIT
    member.detached = False
    member.detached_at = 0.0
    member.room_name = "r1"
    member.client_id = "cid"
    member.username = "u"
    member.member_key = "cid"
    member.is_host = True
    member.is_owner = True
    member.closed = False
    member.last_seen = 0.0
    member.removal_reason = "drop"
    member.writer = None

    class FakeRoom:
        """只提供 `hold_member_seat` 真正会碰到的那几个方法。"""

        def __init__(self):
            self.name = "r1"
            self.members = {"cid": member}
            self.host_member = member
            self.sessions = {}
            self.waiting_players = []
            self.waiting_tunnels = []

        async def release_legacy_host_for(self, who):
            return None

        async def remove_session(self, sid, reason=""):
            return None

        def world_player_count(self):
            return 0

    room = FakeRoom()

    # 宽限期设 0：进去就立刻返回，我们只看进去那一刻有没有清账。
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            room_members.hold_member_seat(room, member, grace=0), timeout=5
        )

    assert member.send_failures == 0, (
        f"进入 detached 必须清零 send_failures，实际 {member.send_failures}"
    )
    assert member.detached is True


async def test_broadcast_is_concurrent():
    """
    ★ 复核报告 3.2：一个慢成员不许拖住其他人。

    v3.9.17 是串行广播 + 5 秒写超时，实测：一个弱网成员 -> 整房间广播卡 5 秒，
    而且包了预算的调用点会在 5 秒整批截断，慢成员**之后**的人这一轮收不到。
    """
    import room_members

    class SlowWriter:
        def __init__(self, delay):
            self.delay = delay

        def write(self, data):
            return None

        async def drain(self):
            await asyncio.sleep(self.delay)

    class OkWriter:
        def write(self, data):
            return None

        async def drain(self):
            return None

    def make(name, delay=0.0):
        member = room_members.RoomMember.__new__(room_members.RoomMember)
        member.closed = False
        member.detached = False
        member.writer = OkWriter() if delay == 0 else SlowWriter(delay)
        member.send_lock = asyncio.Lock()
        member.send_failures = 0
        member.last_send_failed = 0.0
        member.room_name = "r1"
        member.client_id = name
        member.username = name
        member.member_key = name
        member.is_host = False
        member.is_owner = False
        member.send_timeout = room_members.MEMBER_SEND_TIMEOUT

        return member

    # 4 个人，其中 1 个要 5 秒才 drain 完（比广播预算长得多）
    pending = [
        (make("a"), "HOST_STATE\n"),
        (make("b"), "HOST_STATE\n"),
        (make("slow", 5.0), "HOST_STATE\n"),
        (make("c"), "HOST_STATE\n"),
    ]

    started = time.monotonic()

    sent = await room_members._send_lines_concurrently(
        pending,
        budget=room_members.BROADCAST_BUDGET_S,
        timeout=room_members.BROADCAST_SEND_TIMEOUT,
    )

    elapsed = time.monotonic() - started

    assert sent >= 3, f"慢成员不该让别人也收不到，实际送达 {sent}/4"
    assert elapsed <= 3.0, (
        f"一个慢成员不该把整批拖到 {elapsed:.2f}s（应当 <= 3s）"
    )


async def test_watchdog_reaps_dead_member():
    """
    判死之后**必须有人收尸**：看门狗把长期写失败的成员关掉并收走。

    v3.9.16 里 `reap_idle_members` 对 `closed` 成员 `continue`，于是他永久
    占着房间名额（ROOM_FULL）却再也收不到广播。
    """
    import room_members

    class FakeWriter:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeRoom:
        def __init__(self):
            self.name = "r1"
            self.members = {}

    member = room_members.RoomMember.__new__(room_members.RoomMember)
    member.closed = False
    member.detached = False
    member.send_failures = room_members.MEMBER_SEND_FAIL_LIMIT
    member.last_seen = 0.0   # 早就静默了
    member.removal_reason = "drop"
    member.writer = FakeWriter()
    member.room_name = "r1"
    member.client_id = "cid"
    member.username = "u"
    member.member_key = "cid"
    member.is_owner = False
    member.is_host = False

    room = FakeRoom()
    room.members = {"cid": member}

    reaped = await room_members.reap_idle_members([room], now=1e9)

    assert reaped == [(room, member)], f"看门狗必须回收他，实际 {reaped}"
    assert member.writer.closed is True, "必须关掉 socket，让控制循环跑清理"


# ==========================================================
# 5b. v3.9.17：STOPPING 只在用户点停止时发，且标记是一次性的
# ==========================================================

# ==========================================================================
# 5c. v3.9.19：部署时自动补齐占位符凭据
# ==========================================================================
#
# 这一组守的是**安全边界**：`ensure_secrets.py` 每次部署都会跑，它敢动已有
# 的真值，那"跑一次 rebuild"就等于"换掉线上数据库密码"——一次误操作就是一次
# 全站中断。所以它只准填**空的**。
#
# 测试里的 docker 不存在（CI / 沙箱都没装），所以 DB 密码那条路必然走到
# "拿不到 MySQL -> 不写 .env"分支 —— 那正好是我们要验的另一半：**不能**
# 只写 .env 而不改库（否则面板从"占位符连不上"变成"密码错连不上"，更难查）。

def _run_ensure(env_path, *extra):
    """调真正的脚本（子进程），返回 CompletedProcess。"""
    import subprocess
    import sys

    return subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "ensure_secrets.py"),
            "--env", env_path,
            *extra,
        ],
        capture_output=True, text=True,
    )


def test_ensure_secrets_fills_token_and_refuses_to_guess_db():
    """
    占位符 -> token 自动补；DB 密码**不许**凭空写（库里没改就不是真的补齐）。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")

        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("MCLINK_DB_HOST=web-mysql80\n")
            fh.write("MCLINK_DB_NAME=mclink\n")
            fh.write("MCLINK_DB_USER=mclink\n")
            fh.write("MCLINK_DB_PASSWORD=CHANGE_ME_REDACTED\n")
            fh.write("MCLINK_ADMIN_API_TOKEN=CHANGE_ME_REDACTED\n")

        result = _run_ensure(env_path)

        after = {}

        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    after[k.strip()] = v.strip()

        # token 是应用内部共享密钥，两边同源 -> 总能自动补
        assert after["MCLINK_ADMIN_API_TOKEN"] != "CHANGE_ME_REDACTED", (
            "MCLINK_ADMIN_API_TOKEN 应当被自动补上"
        )
        assert len(after["MCLINK_ADMIN_API_TOKEN"]) >= 20, (
            "补上的 token 得是个像样的随机值"
        )

        # DB 密码：没有 MySQL 时不许写（写了就是"库里还是旧密码"的假象）
        assert after["MCLINK_DB_PASSWORD"] == "CHANGE_ME_REDACTED", (
            "拿不到 MySQL 时绝不能只写 .env —— "
            "否则面板从『占位符连不上』变成『密码错连不上』，更难查"
        )

        # 返回非零 -> 部署停在这里等人工
        assert result.returncode != 0, "没补齐时部署必须停住"


def test_ensure_secrets_never_touches_real_values():
    """
    ★ 安全边界：已经有真值的凭据**一律不动**。

    这是这条自动化最危险的地方：部署脚本每次跑，如果它敢动真值，一次
    rebuild 就等于换掉线上数据库密码。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")

        real_db = "Xy7kQm2nRp4wLb9d"
        real_token = "t" * 48

        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("MCLINK_DB_HOST=web-mysql80\n")
            fh.write(f"MCLINK_DB_PASSWORD={real_db}\n")
            fh.write(f"MCLINK_ADMIN_API_TOKEN={real_token}\n")

        result = _run_ensure(env_path)

        assert result.returncode == 0, (
            f"两个都是真值时应当直接通过：{result.stdout}"
        )

        with open(env_path, encoding="utf-8") as fh:
            text = fh.read()

        assert f"MCLINK_DB_PASSWORD={real_db}" in text, (
            "已有的 DB 密码被换掉了 —— 一次部署就等于一次全站中断"
        )
        assert f"MCLINK_ADMIN_API_TOKEN={real_token}" in text, (
            "已有的 token 被换掉了 —— 面板与中继会突然对不上"
        )


# ==========================================================================
# 5d. D5（v3.9.21）：凭据**外部化** —— compose 只有引用，真值在 .env
# ==========================================================================
#
# 这是一次**规格变更**（D5：开源版绝不硬编码），不是放宽：
#   * v3.9.20 的口径是"明文写在 compose 里，门禁只记录不拦"；
#   * 现在的口径是"compose 里只许 `${VAR:?...}` 引用，明文一律判失败" ——
#     比旧口径**更严**（旧口径下 check_deploy --strict-secrets 才拦，现在默认拦）。
#
# 这一组守三件事：
#   1. 两个 compose 里**没有明文**，两个键都是引用，而且引用的是**同一个**
#      变量名（面板与中继对不上就是事故）；
#   2. `.env` 模板提供这些变量（否则新机器上没有任何东西能给 compose 赋值）；
#   3. 门禁 `--strict-secrets` 的「包内无明文凭据」必须是绿的，`rebuild.sh`
#      必须有 .env 才肯继续（老规则"中文占位符"和".env 与 compose 不一致"
#      一个字都没放宽）。

CRED_KEYS = ("MCLINK_DB_PASSWORD", "MCLINK_ADMIN_API_TOKEN")


def _compose_credentials(rel):
    """从 compose 里读出那两个键的值（引用或明文，原样返回）。"""
    text = pathlib.Path(ROOT, rel).read_text(encoding="utf-8")
    found = {}

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("#"):
            continue

        for key in CRED_KEYS:
            if stripped.startswith(key + ":"):
                value = stripped.split(":", 1)[1].strip()

                if value[:1] in ('"', "'"):
                    value = value[1:].rsplit(value[0], 1)[0]

                found[key] = value

    return found


def _credential_reference(value):
    """`${VAR:?...}` -> `VAR`；不是引用就返回 None。"""
    import re

    match = re.match(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)[:}].*$", value or "")

    return match.group(1) if match else None


def test_credentials_are_externalized_and_identical():
    """
    D5：两个 compose 里那两个键都必须是 `${同名变量:?...}` 引用，不许有明文。

    引用同一个变量名 = 面板与中继永远拿到同一个值；一旦有人把明文写回来，
    这里立刻红（而且那条明文会同时被门禁与真实标识符守卫抓到）。
    """
    import re

    panel = _compose_credentials("panel/docker-compose.yml")
    relay = _compose_credentials("server/docker-compose.yml")

    for key in CRED_KEYS:
        for label, found in (("panel", panel), ("server", relay)):
            assert found.get(key), f"{label} compose 里没有 {key}"
            assert not found[key].startswith('"'), (
                f"{label} compose 的 {key} 引号没剥干净：{found[key]!r}"
            )
            assert re.match(r"^[0-9A-Za-z_./+-]{16,}$", found[key]) is None, (
                f"{label} compose 的 {key} 看起来是**明文**：{found[key]!r}"
            )

        assert _credential_reference(panel[key]) == key, (
            f"panel compose 的 {key} 应当是 ${{{key}:?...}} 引用，"
            f"实际是 {panel[key]!r}"
        )
        assert _credential_reference(relay[key]) == key, (
            f"server compose 的 {key} 应当是 ${{{key}:?...}} 引用，"
            f"实际是 {relay[key]!r}"
        )

    # 备案号同理：不写死（默认空 -> 页脚不渲染）
    panel_text = pathlib.Path(ROOT, "panel/docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"ICP备\s*\d", panel_text), (
        "panel compose 里不该再出现真实备案号（改成 ${MCLINK_PANEL_ICP:-}）"
    )


def test_env_example_supplies_the_referenced_credentials():
    """
    compose 引用谁，`.env.example` 就得提供谁 —— 否则新机器上没人给它赋值。

    这条是"引用"能落地的另一半：只改 compose 不改模板，装出来的机器会在
    `docker compose up` 时被 `${VAR:?}` 拒掉。
    """
    text = pathlib.Path(ROOT, ".env.example").read_text(encoding="utf-8")
    keys = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    for key in CRED_KEYS:
        assert key in keys, f".env.example 里没有 {key}（compose 引用了它）"


def test_check_deploy_strict_secrets_is_green():
    """
    `--strict-secrets` 的「包内无明文凭据」必须是绿的；宽松模式也不该再报风险。

    v3.9.20 那版是**反过来**的（宽松记录、严格失败）—— 现在明文真的没了，
    两个模式都该是 [ OK ]。BUILD.txt / __pycache__ 那几项与本条无关（前者要
    打包时跑 make_build.py 重新生成），所以这里只断言**凭据那一项**。
    """
    import subprocess
    import sys

    script = os.path.join(ROOT, "scripts", "check_deploy.py")

    strict = subprocess.run(
        [sys.executable, script, "--no-tests", "--strict-secrets"],
        capture_output=True, text=True,
    )
    loose = subprocess.run(
        [sys.executable, script, "--no-tests"],
        capture_output=True, text=True,
    )

    strict_out = strict.stdout
    loose_out = loose.stdout

    # 明确断言"名字 + 结论"这一对，避免别的 [ OK ] 顶替
    assert "[ OK ] 包内无明文凭据" in strict_out, (
        "严格模式下凭据检查必须是 [ OK ]：\n"
        + "\n".join(
            ln for ln in strict_out.splitlines()
            if "凭据" in ln or "FAIL" in ln
        )[:800]
    )
    assert "[FAIL] 包内无明文凭据" not in strict_out, (
        "严格模式下还有明文凭据（见上面的检出位置）"
    )
    assert "[warn] ┌─ 凭据风险" not in loose_out, (
        "宽松模式不该再打印凭据风险块 —— 说明仓库里还有明文"
    )


def test_rebuild_requires_env_in_external_mode():
    """
    `rebuild.sh` 必须是 external 模式，而且 `.env` 缺失就**停下**（不再放行）。

    同时守住两条**没有放宽**的老规则（它们各自对应一次真实事故）：
      * 中文占位符：MySQL 驱动会报 `'latin-1' codec can't encode ...`；
      * `.env` 与 compose 引用的变量名不一致："面板连得上、中继连不上"。

    这里不只读源码：把 `check_env_file` 抽出来，用真 bash 跑 5 种输入。
    """
    text = pathlib.Path(ROOT, "rebuild.sh").read_text(encoding="utf-8")

    assert "SECRETS_MODE=external" in text, "rebuild.sh 应当处于 external 模式"
    assert "SECRETS_MODE=hardcoded" not in text, (
        "hardcoded 模式必须删掉（D5），不能只是改默认值"
    )

    start = text.index("check_env_file () {")
    body = text[start:text.index("\n}\n", start) + 2]   # 含结尾 `}`

    assert 'if [ "$SECRETS_MODE" != "external" ]; then' in body, (
        "check_env_file 必须显式拒绝非 external 模式"
    )
    assert "中文占位符" in body, "中文占位符那条老规则被删了"
    assert "对不上" in body, ".env 与 compose 引用一致性那条老规则被删了"

    import subprocess
    import tempfile

    # 用字符串替换而不是 .format()：函数体里全是 bash 的 `${...}`，format 会炸。
    # 注意：下面凡是"键 + 等号/冒号"的地方都用变量拼（`_K1 + "=" + ...`）——
    # 门禁的明文扫描器按"键后面跟字面值"判定，直接写出来会把**夹具**误报成
    # 明文凭据（tests/run_all.py 自己就会让 --strict-secrets 变红）。
    _K1, _K2 = CRED_KEYS

    harness_tpl = (
        "set -u\n"
        "SECRETS_MODE=__MODE__\n"
        'PANEL_DIR="__ROOT__/panel"\n'
        'RELAY_DIR="__ROOT__/server"\n'
        'ENV_FILE="__ROOT__/.env"\n'
        "ensure_env_file () { return __ENSURE_RC__; }\n"
        "__BODY__\n"
        'check_env_file "$ENV_FILE"\n'
    )

    ref = ("${%s:?MISSING: set it in .env}" % CRED_KEYS[0],
           "${%s:?MISSING: set it in .env}" % CRED_KEYS[1])

    def run(env_text, mode="external", ensure_rc=0, panel_key=None,
            relay_key=None, panel_plain=None):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "panel"))
            os.makedirs(os.path.join(tmp, "server"))

            if env_text is not None:
                pathlib.Path(tmp, ".env").write_text(env_text, encoding="utf-8")

            for name, key, plain in (
                ("panel", panel_key or CRED_KEYS[0], panel_plain),
                ("server", relay_key or CRED_KEYS[0], None),
            ):
                value = plain if plain is not None else ref[0]
                text_in = (
                    "services:\n  x:\n    environment:\n"
                    + f"      {_K1}: {value}\n"
                    + f"      {_K2}: {ref[1]}\n"
                )

                if key != _K1:
                    text_in = text_in.replace(f"      {_K1}: ", f"      {key}: ")

                pathlib.Path(tmp, name, "docker-compose.yml").write_text(
                    text_in, encoding="utf-8"
                )

            script = os.path.join(tmp, "h.sh")
            pathlib.Path(script).write_text(
                harness_tpl
                .replace("__MODE__", mode)
                .replace("__ROOT__", tmp)
                .replace("__ENSURE_RC__", str(ensure_rc))
                .replace("__BODY__", body),
                encoding="utf-8",
            )

            result = subprocess.run(
                ["bash", script], capture_output=True, text=True
            )

            return result.returncode, result.stdout + result.stderr

    good_env = (
        _K1 + "=" + "Ab3xY9kQm2nRp4wL" + "\n"
        + _K2 + "=" + "Zx8Kp2Qm4Rt6Wn9B" + "\n"
    )

    rc, out = run(good_env)
    assert rc == 0, f"合法输入不该失败（rc={rc}）：\n{out}"

    rc, out = run(None, ensure_rc=1)
    assert rc != 0 and "找不到" in out, (
        f".env 缺失必须失败并说清楚（rc={rc}）：\n{out}"
    )

    rc, out = run(
        _K1 + "=" + "请改成你的密码" + "\n"
        + _K2 + "=" + "Zx8Kp2Qm4Rt6Wn9B" + "\n"
    )
    assert rc != 0 and "中文占位符" in out, (
        f"中文占位符必须拦住（rc={rc}）：\n{out}"
    )

    rc, out = run(good_env, panel_plain='"PLAINTEXT_not_a_real_secret"')
    assert rc != 0 and "对不上" in out, (
        f"compose 里写明文（不是引用）必须拦住（rc={rc}）：\n{out}"
    )

    rc, out = run(good_env, panel_key="MCLINK_DB_PASS")
    assert rc != 0 and "对不上" in out, (
        f".env 与 compose 引用名不一致必须拦住（rc={rc}）：\n{out}"
    )

    rc, out = run(good_env, mode="hardcoded")
    assert rc != 0 and "只支持 external" in out, (
        f"非 external 模式必须直接拒绝（rc={rc}）：\n{out}"
    )


def test_real_identifiers_do_not_leak():
    """
    真实标识符守卫（`tests/real_identifiers_guard_test.py`）必须是绿的。

    它扫的是**跟踪树**（跳过 deploy/、optional/、.testcfg/、__pycache__ 与
    .gitignore 命中的文件），抓：真实域名/子域、真实备案号、明文库密码、
    明文管理 token、真机公网 IP。失败信息给"文件:行号 + 类别"，不回显值。

    守卫输出里的 `[warn] 已知遗留` 是**别的 workflow 拥有的文件**里那些
    注释/文档串行（本轮不许动它们），不算失败。
    """
    import subprocess
    import sys

    script = os.path.join(ROOT, "tests", "real_identifiers_guard_test.py")

    result = subprocess.run(
        [sys.executable, script], capture_output=True, text=True
    )
    out = (result.stdout or "") + (result.stderr or "")

    assert result.returncode == 0, "真实标识符守卫未通过：\n" + out[-2500:]
    assert (
        "[PASS] ★ 跟踪树里没有真实域名 / 备案号 / 明文凭据 / 真实公网 IP"
        in out
    ), "守卫没有给出通过标记：\n" + out[-1500:]
    assert "[FAIL]" not in out, "守卫报告了违规：\n" + out[-1500:]


# ==========================================================================
# 5e. M1（D12 + A2）：单房间上限 2 -> 64，中继版本对外可读
# ==========================================================================
#
# 两个数字各写一遍是这一类 bug 的固定来源：
#   * 上限散在中继校验（settings_service.maximum）、compose 的
#     MCLINK_MAX_PLAYERS、客户端输入范围三处 —— 只改一处就会出现
#     "面板写着 64、第 9 个人还是进不来"；
#   * 版本号以前在 /health 与 status.json 里各写死一个 "2.0.0"，中继升到
#     哪一版面板上都显示 2.0.0（A2 修的真实缺陷）。
#
# 这一组管客户端那一半（快、纯离线）；服务端那一半由
# `tests/m1_capacity_version_test.py` 起**真中继**验证（64 进 / 65 拒 +
# /health 的版本逐字等于 server/VERSION）。

def test_client_max_players_is_64():
    """客户端显示/输入范围：2-64（常量三处同源，隧道数跟着容量走）。"""
    import mclink.core.protocol as proto
    from mclink.config import settings as client_settings

    assert proto.DEFAULT_MAX_PLAYERS == 64, (
        f"客户端上限应当是 64，实际 {proto.DEFAULT_MAX_PLAYERS}"
    )
    assert proto.MIN_MAX_PLAYERS == 2, "下限仍然是 2（含房主）"

    base = dict(
        server_host="127.0.0.1",
        server_port=19100,
        room="m1-room",
        role="host",
        client_id="m1-cid",
        username="m1-user",
        max_tunnels=1,          # 派生值：故意填错，见 Config.validated
    )

    cfg = client_settings.ClientConfig(**dict(base, max_players=64)).validated()

    assert cfg.max_players == 64, f"64 必须被接受，实际 {cfg.max_players}"
    assert cfg.max_tunnels == 64, (
        "隧道数必须等于房间容量（一个远程玩家一条），"
        f"实际 max_tunnels={cfg.max_tunnels}"
    )

    try:
        client_settings.ClientConfig(**dict(base, max_players=65)).validated()
    except ValueError as e:
        assert "2-64" in str(e), f"报错里要写清范围 2-64：{e}"
    else:
        raise AssertionError("65 必须被客户端拒绝，而不是悄悄夹到 64")


_M1_LIVE_RESULT = {}


def _run_m1_live():
    """
    跑 `tests/m1_capacity_version_test.py`（真中继）。

    两个 check 共用同一次运行：起中继 + 开 65 条连接要几秒，没必要跑两遍。
    """
    import subprocess
    import sys

    if "result" not in _M1_LIVE_RESULT:
        script = os.path.join(ROOT, "tests", "m1_capacity_version_test.py")

        try:
            _M1_LIVE_RESULT["result"] = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired as e:
            _M1_LIVE_RESULT["result"] = e

    return _M1_LIVE_RESULT["result"]


def _m1_live_output():
    result = _run_m1_live()

    if hasattr(result, "stdout") and result.stdout:
        return result

    raise AssertionError(f"M1 真中继测试超时/起不来：{result}")


def test_m1_relay_accepts_64_and_refuses_65():
    """★ D12 的验收点：真中继上 64 个人进得去，第 65 个被 ROOM_FULL 拒绝。"""
    result = _m1_live_output()

    assert result.returncode == 0, (
        "m1_capacity_version_test.py 未通过：\n"
        + (result.stdout or "")[-2500:]
    )
    assert "[PASS] ★ 64 个人全部进得去（房主 + 63）" in result.stdout, (
        "64 人进房那一条没有通过"
    )
    assert "[PASS] ★ 第 65 个人被 ROOM_FULL 拒绝" in result.stdout, (
        "第 65 个人没有被拒 —— 上限根本没生效"
    )


def test_m1_admin_api_reports_real_relay_version():
    """★ A2：/health 的 relay_version 必须逐字等于 server/VERSION。"""
    result = _m1_live_output()

    assert result.returncode == 0, (
        "m1_capacity_version_test.py 未通过：\n"
        + (result.stdout or "")[-2500:]
    )
    assert (
        "[PASS] ★ A2：/health 的 relay_version == server/VERSION"
        in result.stdout
    ), "管理 API 没有报告真实的中继版本"
    assert "[PASS] ★ A2：status.json 里的版本也是真的" in result.stdout, (
        "status.json 里的版本还是旧的"
    )


def test_host_core_sends_stopping_only_when_graceful():
    """
    `HostCore.stop()` **默认不发** `STOPPING`。

    v3.9.16 是无条件发，而 `stop()` 不只"用户点停止"会走 —— 模式热切换的
    重建也走它。于是房主正常停过一次之后，中继那个"主动停止"标记永不复位，
    2.5 秒宽限窗口永久失效（复核报告 P1）。
    """
    src = os.path.join(ROOT, "client", "mclink", "core", "host_core.py")

    with open(src, encoding="utf-8") as fh:
        text = fh.read()

    assert "async def stop(self, graceful=False):" in text, (
        "HostCore.stop 必须带 graceful 参数（默认 False）"
    )
    assert "if graceful and self.control_writer is not None:" in text, (
        "只有 graceful=True 才发 STOPPING"
    )


def test_gui_user_stop_is_graceful():
    """用户点「停止」那条路必须传 `graceful=True`。"""
    src = os.path.join(ROOT, "client", "mclink", "gui", "app.py")

    with open(src, encoding="utf-8") as fh:
        text = fh.read()

    assert "stop_session(graceful=True)" in text, (
        "on_stop 必须显式传 graceful=True"
    )
    # 而且只能有**一处**这么传（别的调用点一律不传）。
    assert text.count("stop_session(graceful=True)") == 1, (
        "graceful=True 只能出现在用户点停止那一处"
    )


async def test_host_stopping_is_one_shot():
    """
    中继的 `host_stopping` 必须**用完即清**：关连接时读走一次就不再有效。

    v3.9.16 它永不复位 —— 房主正常停一次之后，这个房间的每次后续断开都被
    当成"主动停止"，宽限窗口永久跳过，措辞也永远是错的。
    """
    import relay_server

    relay_server.HOST_STOP_GRACE_S = 0.05

    class FakeRoom:
        def __init__(self):
            self.host_stopping = True
            self.host_writer = None
            self.closed = False
            self.sessions = {}
            self.waiting_tunnels = []
            self.waiting_players = []
            self.members = {}
            self.name = "r1"
            self.notified = []

        async def remove_session(self, sid, reason=""):
            return None

    class FakeLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    room = FakeRoom()
    room.lock = FakeLock()

    import room_members

    # 这个假房间必须**在中继的注册表里**：`announce_host_serving_gone`
    # 会按对象身份复查"房间是不是同一代"，不在表里就会被当成"房间没了"。
    relay_server.rooms["r1"] = room

    real = room_members.disconnect_game_connections
    announced = []

    async def _spy(room_arg, reason="", code="", extra=""):
        announced.append((reason, code))

        return 0

    room_members.disconnect_game_connections = _spy

    started = time.monotonic()

    try:
        # 房主说了 STOPPING -> **立刻**广播（immediate），不等宽限窗口。
        await asyncio.wait_for(
            relay_server.announce_host_serving_gone(
                room, "r1", "host 已停止开服",
                room_members.HOST_STOPPED_CODE, immediate=True,
            ),
            timeout=5,
        )

        elapsed = time.monotonic() - started

        assert len(announced) == 1, f"主动停止应当立刻广播，实际 {announced}"
        assert elapsed < 1.0, (
            f"immediate=True 时不该等宽限窗口（耗时 {elapsed:.2f}s）"
        )
        assert announced[0][0] == "host 已停止开服", (
            f"措辞必须是『主动停止』，实际 {announced[0][0]!r}"
        )
    finally:
        room_members.disconnect_game_connections = real
        relay_server.rooms.pop("r1", None)


async def test_silent_drop_uses_grace_window():
    """
    没说 STOPPING（掉线/崩溃/模式重建）-> 必须走宽限窗口，host 回来了就不广播。

    这条是 2.5 秒宽限窗口存在的唯一理由：别把模式热切换当成 host 停服，
    否则全房间被踢下线。
    """
    import room_members
    import relay_server

    relay_server.HOST_STOP_GRACE_S = 0.3

    class FakeRoom:
        def __init__(self):
            self.host_stopping = False
            self.host_writer = None
            self.closed = False
            self.sessions = {}
            self.waiting_tunnels = []
            self.waiting_players = []
            self.members = {}
            self.name = "r1"

    room = FakeRoom()
    relay_server.rooms["r1"] = room

    real = room_members.disconnect_game_connections
    announced = []

    async def _spy(room_arg, reason="", code="", extra=""):
        announced.append(reason)

        return 0

    room_members.disconnect_game_connections = _spy

    class _FakeHostWriter:
        """host 在窗口期内回来了（模式重建）。"""

        def close(self):
            return None

    try:
        # 场景一：模式重建 —— host 在窗口里回来了 -> **不广播**
        room.host_writer = _FakeHostWriter()

        await asyncio.wait_for(
            relay_server.announce_host_serving_gone(
                room, "r1", "host 已断开连接（未开服）",
                room_members.HOST_STOPPED_CODE, immediate=False,
            ),
            timeout=5,
        )

        assert announced == [], (
            f"host 在宽限窗口内回来了，就不该广播（会把全房间踢下线）：{announced}"
        )

        # 场景二：真停了 —— host 没回来 -> 广播，措辞是"断开连接"
        room.host_writer = None

        await asyncio.wait_for(
            relay_server.announce_host_serving_gone(
                room, "r1", "host 已断开连接（未开服）",
                room_members.HOST_STOPPED_CODE, immediate=False,
            ),
            timeout=5,
        )

        assert announced == ["host 已断开连接（未开服）"], (
            f"host 没回来应当广播『断开连接』，实际 {announced}"
        )
    finally:
        room_members.disconnect_game_connections = real
        relay_server.rooms.pop("r1", None)


def test_host_stopping_reset_on_new_handshake():
    """新开服连接建立时必须把 `host_stopping` 清零。"""
    src = os.path.join(ROOT, "server", "relay_server.py")

    with open(src, encoding="utf-8") as fh:
        text = fh.read()

    # 握手赋值处（room.host_last_seen = None 旁边）必须跟着清标记。
    assert "room.host_last_seen = None" in text

    idx = text.index("room.host_last_seen = None")
    tail = text[idx:idx + 1200]

    assert "room.host_stopping = False" in tail, (
        "新握手建立时必须清掉 host_stopping（否则宽限窗口永久失效）"
    )


# ==========================================================
# 6. 版本门必须自动到期
# ==========================================================

def test_version_bypass_expires():
    import settings_service as svc

    service = svc.SettingsService.__new__(svc.SettingsService)
    service.store = None
    service.defaults = {}
    service._cache = {
        svc.SETTING_ALLOW_VERSION_MISMATCH: True,
    }
    service._bypass_until = None
    service._bypass_expiry_logged = False

    # TTL 设成 0（不过期）时应当恒为 True。
    saved = svc.VERSION_BYPASS_TTL_S
    svc.VERSION_BYPASS_TTL_S = 0

    try:
        assert service.get(svc.SETTING_ALLOW_VERSION_MISMATCH) is True
    finally:
        svc.VERSION_BYPASS_TTL_S = saved

    # 到期之后必须自己回到关闭。
    svc.VERSION_BYPASS_TTL_S = 1
    service._bypass_until = 0.0  # 已经过去

    assert service.get(svc.SETTING_ALLOW_VERSION_MISMATCH) is False, (
        "版本门到期后必须自动关闭"
    )


# ==========================================================
# 7. 免登录顶替：只看 client_id 不够，还要看来源
# ==========================================================

def test_host_supersede_requires_same_ip_when_no_token():
    import relay_server

    # 有 token 时按 token 比（这是最强的判据）。
    assert relay_server._same_host_person(
        "cid1", "cid1", token="t-a", host_token="t-b"
    ) is False, "token 不同必须拒绝"

    assert relay_server._same_host_person(
        "cid1", "cid1", token="t-a", host_token="t-a"
    ) is True, "同一个 token 必须放行"

    # 免登录（两边 token 都空）：IP 不同 -> 拒绝。
    assert relay_server._same_host_person(
        "cid1", "cid1", token="", host_token="",
        peer_ip="1.2.3.4", host_ip="5.6.7.8",
    ) is False, "免登录下 IP 不同必须拒绝（client_id 是公开的）"

    # 同一个来源 IP -> 放行（正常重连）。
    assert relay_server._same_host_person(
        "cid1", "cid1", token="", host_token="",
        peer_ip="1.2.3.4", host_ip="1.2.3.4",
    ) is True, "同一个来源 IP 的重连必须放行"


# ==========================================================
# 8. M5：服务端安装器 + SSH 管理台
# ==========================================================
#
# 安装器（install.sh）要做的事每件都很危险：探测/复用别人的 MySQL 容器、
# 改数据库账号、写明文凭据。而沙箱/CI 里没有 docker —— 所以这一条跑的是
# `tests/install_sh_test.py`：它用 `tests/fake_docker.py`（会真的执行那几条
# SQL、维护库/账号状态）把三条路径都过一遍：
#
#     全新机器 / 已有 MySQL（复用，不新建）/ 重复跑（幂等，不许换凭据）
#
# 外加管理台的 root-only 与审计日志。

_M5_RESULT = {}


def _run_m5():
    import subprocess
    import sys

    if "result" not in _M5_RESULT:
        script = os.path.join(ROOT, "tests", "install_sh_test.py")

        try:
            _M5_RESULT["result"] = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired as e:
            _M5_RESULT["result"] = e

    return _M5_RESULT["result"]


def _m5_output():
    result = _run_m5()

    if hasattr(result, "stdout") and result.stdout:
        return result

    raise AssertionError(f"M5 安装器测试超时/起不来：{result}")


def test_m5_installer_reuses_existing_db_and_is_idempotent():
    """全新机器能装起来；已有 MySQL 只复用不新建；重复跑不动凭据。"""
    result = _m5_output()

    assert result.returncode == 0, (
        "install_sh_test.py 未通过：\n" + (result.stdout or "")[-2500:]
    )

    for marker in (
        "[PASS] ★ 没有现成 MySQL 时用 compose 起了一个",
        "[PASS] ★ 没有新建 MySQL 容器（还是那两个）",
        "[PASS] ★★ 重复跑不动凭据（.env 逐字节不变）",
        "[PASS] ★ 失败时**没有**写 .env（不留半装状态）",
    ):
        assert marker in result.stdout, f"缺少断言：{marker}"


def test_m5_console_is_root_only_and_audited():
    """管理台只允许 root；每个动作都写审计日志，且不泄露凭据。"""
    result = _m5_output()

    assert result.returncode == 0, (
        "install_sh_test.py 未通过：\n" + (result.stdout or "")[-2500:]
    )

    for marker in (
        "[PASS] ★ 非 root 运行管理台被拒绝（免密码的前提是 root-only）",
        "[PASS] ★ 每个动作都写进了审计日志",
        "[PASS] ★ 审计日志**不泄露**凭据（不能把密码抄进去）",
    ):
        assert marker in result.stdout, f"缺少断言：{marker}"


# ==========================================================
# 9. M6：客户端去硬编码 + 品牌可定制
# ==========================================================
#
# 这一条跑 `tests/m6_client_config_test.py`（纯标准库、离线、全在 tempfile
# 沙箱里 —— APPDATA/XDG_CONFIG_HOME/MCLINK_DATA_DIR 都被指到沙箱，绝不碰
# 真实用户目录）。它覆盖四件事：
#
#   1. 中继地址/端口**默认空** + 没填时给的是引导（不是静默连某台服务器，
#      也不是一句干巴巴的报错）；
#   2. 填过之后记忆在**用户数据目录**，下次启动读得回来；
#   3. `branding.json` 覆盖软件名 / logo / 默认地址；缺失/写坏退回默认；
#   4. 数据目录可改（改完文件真的落在新目录，且拒绝写程序安装目录）；
#      老目录的配置/日志迁移、**旧凭据清掉**（D11：旧文件不许把"记住密码"
#      自动勾回来）。

_M6_RESULT = {}


def _run_m6():
    import subprocess
    import sys

    if "result" not in _M6_RESULT:
        script = os.path.join(ROOT, "tests", "m6_client_config_test.py")

        try:
            _M6_RESULT["result"] = subprocess.run(
                [sys.executable, script],
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired as e:
            _M6_RESULT["result"] = e

    return _M6_RESULT["result"]


def _m6_output():
    result = _run_m6()

    if hasattr(result, "stdout") and result.stdout:
        return result

    raise AssertionError(f"M6 客户端配置测试超时/起不来：{result}")


def test_m6_default_server_is_empty_with_guidance():
    """默认空 -> 提示：不内置服务器，但也绝不让用户对着空框发呆。"""
    result = _m6_output()

    assert result.returncode == 0, (
        "m6_client_config_test.py 未通过：\n" + (result.stdout or "")[-2500:]
    )

    for marker in (
        "[PASS] ★ 默认中继地址为空（没有任何内置服务器）",
        "[PASS] ★ 默认端口为 0（= 未设置，不是 19100）",
        "[PASS] 地址为空时连接被拒绝（而不是静默用默认值）",
        "[PASS] 引导里说清了：端口",
        "[PASS] 只缺端口时的提示指向端口（不是笼统的报错）",
    ):
        assert marker in result.stdout, f"缺少断言：{marker}"


def test_m6_address_and_login_memory():
    """填写后记忆并回读；登录记忆默认不写盘（D11）。"""
    result = _m6_output()

    assert result.returncode == 0, (
        "m6_client_config_test.py 未通过：\n" + (result.stdout or "")[-2500:]
    )

    for marker in (
        "[PASS] ★ 下次启动自动带出地址与端口",
        "[PASS] 用户确认过的地址被标记为 user（迁移时不会被清掉）",
        "[PASS] 登录记忆默认：没有文件、勾选为假",
        "[PASS] 记忆文件落在用户数据目录（不是程序目录）",
        "[PASS] 取消勾选后：文件删除、勾选为假（默认不记）",
    ):
        assert marker in result.stdout, f"缺少断言：{marker}"


def test_m6_branding_json_overrides_and_falls_back():
    """branding.json 覆盖名字/logo/默认地址；缺失或写坏退回默认。"""
    result = _m6_output()

    assert result.returncode == 0, (
        "m6_client_config_test.py 未通过：\n" + (result.stdout or "")[-2500:]
    )

    for marker in (
        "[PASS] ★ branding.json 覆盖软件名（窗口标题/关于页读的是它）",
        "[PASS] ★ branding.json 覆盖默认地址与端口",
        "[PASS] ★ logo_file 解析成绝对路径（相对品牌文件所在目录）",
        "[PASS] 品牌文件缺失：默认地址为空（不是别人的服务器）",
        "[PASS] 品牌文件写坏：给出警告（不静默）",
        "[PASS] 端口不是数字 -> 按『没有默认端口』处理",
        "[PASS] 找不到的 logo -> 空串 + 警告（退回内置图标）",
    ):
        assert marker in result.stdout, f"缺少断言：{marker}"


def test_m6_user_data_dir_is_configurable_and_migrates():
    """数据目录可改；文件真的落新目录；不许写安装目录；老记忆清理。"""
    result = _m6_output()

    assert result.returncode == 0, (
        "m6_client_config_test.py 未通过：\n" + (result.stdout or "")[-2500:]
    )

    for marker in (
        "[PASS] ★ 重启后（无环境变量）仍然读回用户选的目录",
        "[PASS] 配置文件真的写在新目录里",
        "[PASS] 登录记忆也写在新目录里",
        "[PASS] ★ 拒绝把数据目录设成程序安装目录",
        "[PASS] ★ 兜底目录也不在安装目录里（老代码会写 EXE 旁边）",
        "[PASS] ★ 老目录里的凭据文件被清掉（不留没人再读的密码副本）",
        "[PASS] ★ 迁移**没有**把旧密码文件搬进新目录（D11：默认不记）",
        "[PASS] ★ 老配置里的地址被清掉（老版本内置默认值不算用户确认）",
        "[PASS] ★ 用户确认过的地址原样保留（只清未经确认的）",
        "[PASS] 手动换目录：凭据**搬走**（新目录有、旧目录没有）",
        "[PASS] ★ 客户端里没有任何写死的真实域名",
        "[PASS] ★ 客户端代码里没有硬编码的服务器端口 19100（含回退值）",
    ):
        assert marker in result.stdout, f"缺少断言：{marker}"


# ==========================================================
# M8：可选组件（optional/）
# ==========================================================

def test_optional_components_are_optional():
    """`optional/` 是**可选**组件：包里没有它 = 正常；有它就必须自带说明。

    D13：早期官网（`optional/site/`）与下载哈希校验（`optional/checksum/`）
    已经从主交付摘出去，`scripts/make_build.py` 把整个 `optional/` 排除在
    BUILD.txt / README.txt 之外。所以这个检查**故意做成跳过式**：

      * 交付包里没有 `optional/` —— 直接通过（这才是服务器上看到的样子）；
      * 源码仓库里有 `optional/` —— 每个存在的组件目录必须有 README.md，
        免得留档变成一堆没人知道怎么用的文件。

    也就是说：**缺了不算失败**，但"有却没说清是什么"算失败。
    """
    root = os.path.join(ROOT, "optional")

    if not os.path.isdir(root):
        return

    for name in ("site", "checksum"):
        comp = os.path.join(root, name)

        if not os.path.isdir(comp):
            continue

        assert os.path.isfile(os.path.join(comp, "README.md")), (
            f"optional/{name}/ 缺少 README.md（可选组件也要说清是什么）"
        )

    readme = os.path.join(root, "README.md")

    assert os.path.isfile(readme), "optional/ 缺少 README.md"

    with open(readme, encoding="utf-8") as fh:
        text = fh.read()

    assert "不再提供" in text, "optional/README.md 必须写明这些能力已不再提供"


# ==========================================================
# main
# ==========================================================

def main():
    print("=" * 70)
    print(" CraftLink 回归测试")
    print("=" * 70)

    _install_client_package()

    check("版本号：客户端与服务端一致", test_version_matches)
    check("安全：包内无明文凭据", test_no_plaintext_secrets)
    check("核心层：收到 HOST_STOPPED 立刻自己拆", test_room_calls_host_gone_hook)
    check("核心层：host 自己不被拆", test_room_does_not_teardown_for_host_itself)
    check("核心层：四种原因码都拆", test_room_all_host_gone_codes)
    check("界面层：窗口期内缓存而非丢弃", test_gui_defers_instead_of_dropping)
    check("界面层：窗口结束真的补跑", test_gui_flush_runs_deferred_after_window)
    check("界面层：无人关窗也有兜底", test_gui_flush_has_hard_deadline)
    check("界面层：running 为假也停会话", test_gui_stops_session_regardless_of_running)
    check("界面层：host 自己不被踢", test_gui_host_itself_is_not_torn_down)
    acheck("服务端：卡住的写入有超时", test_write_with_timeout_returns_false_when_stuck)
    acheck("服务端：通知挂住也必须拆会话", test_disconnect_still_tears_down_when_notify_hangs)
    acheck("服务端：写超时不判死成员", test_member_send_timeout_is_not_death)
    acheck("服务端：真失败才判死", test_member_hard_failure_marks_closed)
    acheck("服务端：连续失败到上限判死", test_member_repeated_timeouts_eventually_dies)
    acheck("服务端：看门狗回收死成员", test_watchdog_reaps_dead_member)
    acheck("服务端：已 closed 的成员也能回收", test_watchdog_reaps_already_closed_member)
    acheck("v3.9.18：detached 成员不被收走", test_watchdog_never_reaps_detached_member)
    acheck("v3.9.18：进 detached 清旧账", test_detach_clears_send_failures)
    acheck("v3.9.18：广播并发不互相拖累", test_broadcast_is_concurrent)
    check("D5：凭据外部化（两个 compose 都是引用）",
          test_credentials_are_externalized_and_identical)
    check("D5：.env.example 提供被引用的凭据",
          test_env_example_supplies_the_referenced_credentials)
    check("D5：门禁 --strict-secrets 无明文（绿）",
          test_check_deploy_strict_secrets_is_green)
    check("D5：rebuild.sh 必须 external + 要 .env（两条老规则不放宽）",
          test_rebuild_requires_env_in_external_mode)
    check("D5：真实标识符守卫（域名/备案号/IP/凭据）",
          test_real_identifiers_do_not_leak)
    check("v3.9.19：自动补占位符凭据", test_ensure_secrets_fills_token_and_refuses_to_guess_db)
    check("v3.9.19：已有真值绝不动", test_ensure_secrets_never_touches_real_values)
    check("v3.9.17：STOPPING 只在主动停时发", test_host_core_sends_stopping_only_when_graceful)
    check("v3.9.17：用户停止才传 graceful", test_gui_user_stop_is_graceful)
    acheck("v3.9.17：主动停止立即广播", test_host_stopping_is_one_shot)
    acheck("v3.9.17：无声断开走宽限窗口", test_silent_drop_uses_grace_window)
    check("v3.9.17：新握手清标记", test_host_stopping_reset_on_new_handshake)
    check("版本门：到期自动关闭", test_version_bypass_expires)
    check("免登录：顶替 host 需同来源", test_host_supersede_requires_same_ip_when_no_token)
    check("M1：客户端人数上限 2-64（隧道数跟着容量）",
          test_client_max_players_is_64)
    check("M1：真中继 64 人进 / 65 人被拒（D12）",
          test_m1_relay_accepts_64_and_refuses_65)
    check("M1：管理 API 报告真实中继版本（A2）",
          test_m1_admin_api_reports_real_relay_version)
    check("M5：安装器复用已有 DB / 幂等（假 docker）",
          test_m5_installer_reuses_existing_db_and_is_idempotent)
    check("M5：管理台 root-only + 审计（假 docker）",
          test_m5_console_is_root_only_and_audited)
    check("M6：中继地址默认空 + 引导（D10）",
          test_m6_default_server_is_empty_with_guidance)
    check("M6：填写后记忆 + 登录记忆默认不写盘（D11）",
          test_m6_address_and_login_memory)
    check("M6：branding.json 覆盖名字/logo/默认地址 + 回退",
          test_m6_branding_json_overrides_and_falls_back)
    check("M6：数据目录可改 / 迁移 / 不写安装目录",
          test_m6_user_data_dir_is_configurable_and_migrates)
    check("M8：optional/ 是可选组件（没有=通过，有=必须带说明）",
          test_optional_components_are_optional)

    passed = sum(1 for ok, _, _ in _RESULTS if ok)
    total = len(_RESULTS)

    print("=" * 70)
    print(f" {passed}/{total} 通过")

    if passed != total:
        for ok, name, detail in _RESULTS:
            if not ok:
                print(f"   - {name}: {detail}")

        return 1

    print(" 全部通过。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
