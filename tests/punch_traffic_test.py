"""
MC-Link -- 直连承载 / 打洞快路 / 流量账本 回归测试（不需要图形环境）。

============================================================================
 这一套盯的是 2026-09-27 日志审计里的几条 P0/P1/P2
============================================================================

  1. `host_core._try_direct_carry` 把不存在的 `tunnel_reader` 传给了
     `carrier.pump(...)` → 房主每一次走直连都 `NameError`（"洞打通了但游戏
     秒掉线"）。这里用一个记录 kwargs 的假承载，断言**真正的中继 reader**
     被传下去，且不抛异常。
  2. `_rebuild_tunnels` 重建时必须带 `role_override`（角色由"谁持有主机位"
     决定），并且 `_collect_config` 不再有静默的 `or "player"`。
  3. 非房主侧的"房间模式变了就重建"判定必须**只请求一次**（带防抖）。
  4. 打洞快路：房间 NAT 画像够新时不再完整测第二遍；`_warm_relay_mapping`
     不等 STUN 答案。
  5. relay / 没在打洞时不出"一人一行"的洞行。
  6. 直连承载的字节要按 direct 记进**进程级**账本；累计值不随会话消失；
     空闲时速度衰减到 0 而不是消失。
  7. 亚毫秒 RTT 不许显示成 0；隧道 keepalive 的 T_PONG 要按序号核对。

Run:
    python tests/punch_traffic_test.py
"""

import asyncio
import contextlib
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def _silent(*_a, **_k):
    return None


# ============================================================
# 1. 房主直连承载：传下去的是 relay_reader，不是 tunnel_reader
# ============================================================

def test_host_carry_passes_the_relay_reader():
    print("")
    print("---- 房主直连承载：pump 必须拿到真正的中继 reader ----")

    from mclink.core import direct_carry
    from mclink.core.host_core import HostCore

    recorded = {}

    class FakeCarrier:
        def __init__(self, link, key, is_host=False, log=None,
                     on_activity=None):
            recorded["link"] = link
            recorded["key"] = key
            recorded["is_host"] = is_host

        def set_backend(self, host, port):
            recorded["backend"] = (host, port)

        async def open(self):
            return True

        async def pump(self, mc_reader, mc_writer, relay_reader=None):
            recorded["pump_kwargs"] = {"relay_reader": relay_reader}
            recorded["mc_reader"] = mc_reader
            recorded["mc_writer"] = mc_writer

    original = direct_carry.DirectCarrier
    direct_carry.DirectCarrier = FakeCarrier

    try:
        core = HostCore(
            "127.0.0.1", 19100, "audit-room",
            on_log=_silent, on_event=_silent,
            direct_path_provider=lambda: "fake-link",
            direct_backend_setter=lambda *_a: None,
            direct_path_releaser=_silent,
        )

        relay_reader = object()
        mc_reader = object()
        mc_writer = object()

        carried = asyncio.new_event_loop().run_until_complete(
            core._try_direct_carry(
                1, "session-1", mc_reader, mc_writer, relay_reader
            )
        )

        check("★ 房主直连承载不再抛 NameError", carried is True, repr(carried))
        check(
            "★ 传到 pump 的正是那个中继 reader（不是 tunnel_reader）",
            recorded.get("pump_kwargs", {}).get("relay_reader") is relay_reader,
            repr(recorded.get("pump_kwargs")),
        )
        check(
            "pump 拿到的是本会话的 mc_reader/mc_writer",
            recorded.get("mc_reader") is mc_reader
            and recorded.get("mc_writer") is mc_writer,
        )
    finally:
        direct_carry.DirectCarrier = original


# ============================================================
# 2. 打洞快路：复用房间 NAT 画像 / 暖场不等答案
# ============================================================

def _fake_stun_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    return sock


def test_measure_reuses_the_room_profile():
    print("")
    print("---- 打洞快路：房间已经测过的 NAT 不再完整测第二遍 ----")

    from mclink.core import direct as direct_mod
    from mclink.core import punch as punch_mod
    from mclink.core import stun as stun_mod

    sock = _fake_stun_socket()
    calls = {"probe": 0, "full": 0, "filter": 0}

    hint = stun_mod.NatProfile()
    hint.public = ("203.0.113.5", 11111)          # 不同 socket -> 端口不可复用
    hint.nat_type = punch_mod.NatSub.EASY_INC
    hint.filtering = punch_mod.Filter.APDF
    hint.coarse = stun_mod.NAT_SYMMETRIC
    hint.step = 1
    hint.per_ip_pool = False
    hint.servers = [("127.0.0.1", 3478, ("203.0.113.5", 11111))]
    hint.asked = 1
    hint.pool_size = 1

    def fake_probe(sock_, timeout=0, servers=None, hub=None, want=2,
                   log=None, third_n=None, extra=None):
        # `stun.run_blocking` 在线程池里调用它 —— 它必须是**同步**函数
        # （和真的 `stun_probe2` 一样）。
        calls["probe"] += 1
        return ("203.0.113.9", 44444, stun_mod.NAT_SYMMETRIC, 1,
                punch_mod.NatSub.EASY_INC.value)

    async def fake_full_measure(sock_, servers=None, log=None, **kwargs):
        calls["full"] += 1
        return stun_mod.NatProfile()

    original_probe = stun_mod.stun_probe2
    original_measure = stun_mod.measure
    original_filter = stun_mod.stun_filtering
    stun_mod.stun_probe2 = fake_probe
    stun_mod.measure = fake_full_measure

    async def fake_filter(*_a, **_k):
        calls["filter"] += 1
        return stun_mod.FILTER_UNKNOWN

    stun_mod.stun_filtering = fake_filter

    try:
        link = direct_mod.DirectLink(object(), "127.0.0.1", 19100,
                                     log=_silent)
        link.stun_port = 3478
        link.sock = sock
        link.nat_hint = hint
        link.nat_hint_at = time.monotonic()

        profile = asyncio.new_event_loop().run_until_complete(link.measure())

        check(
            "★ 走的是「只探端点」的快路，没有跑完整测量",
            calls["probe"] == 1 and calls["full"] == 0,
            f"probe={calls['probe']} full={calls['full']}",
        )
        check(
            "★ 公网端点来自**本 socket**（不是房间那份的端口）",
            profile.public == ("203.0.113.9", 44444),
            repr(profile.public),
        )
        check(
            "映射类型 / 过滤档位复用房间的结论",
            profile.nat_type == punch_mod.NatSub.EASY_INC
            and profile.filtering == punch_mod.Filter.APDF,
            f"{profile.nat_type} / {profile.filtering}",
        )
        check(
            "★ 快路不再跑过滤探测（省下 FILTER_PROBE_BUDGET_S）",
            calls["filter"] == 0,
            f"filter={calls['filter']}",
        )
    finally:
        stun_mod.stun_probe2 = original_probe
        stun_mod.measure = original_measure
        stun_mod.stun_filtering = original_filter
        with contextlib.suppress(OSError):
            sock.close()


def test_measure_falls_back_when_the_hint_is_stale_or_unknown():
    print("")
    print("---- 快路不适用时必须回到完整测量 ----")

    from mclink.core import direct as direct_mod
    from mclink.core import punch as punch_mod
    from mclink.core import stun as stun_mod

    calls = {"full": 0}

    async def fake_full_measure(sock_, servers=None, log=None, **kwargs):
        calls["full"] += 1
        profile = stun_mod.NatProfile()
        profile.public = ("198.51.100.7", 22222)
        profile.nat_type = punch_mod.NatSub.CONE
        profile.filtering = punch_mod.Filter.UNKNOWN
        return profile

    original_measure = stun_mod.measure
    stun_mod.measure = fake_full_measure

    try:
        for label, mutate in (
            ("没有画像", lambda link: None),
            ("画像太旧", lambda link: setattr(link, "nat_hint_at", 0.0)),
            (
                "分类仍是 unknown",
                lambda link: setattr(link.nat_hint, "nat_type",
                                     punch_mod.NatSub.UNKNOWN),
            ),
        ):
            sock = _fake_stun_socket()

            hint = stun_mod.NatProfile()
            hint.public = ("203.0.113.5", 11111)
            hint.nat_type = punch_mod.NatSub.EASY_INC
            hint.filtering = punch_mod.Filter.APDF
            hint.servers = [("127.0.0.1", 3478, ("203.0.113.5", 11111))]

            link = direct_mod.DirectLink(object(), "127.0.0.1", 19100,
                                         log=_silent)
            link.stun_port = 3478
            link.sock = sock
            link.nat_hint = hint
            link.nat_hint_at = time.monotonic()
            mutate(link)

            before = calls["full"]
            profile = asyncio.new_event_loop().run_until_complete(link.measure())

            check(
                f"★ {label} -> 走完整测量（行为与以前一致）",
                calls["full"] == before + 1
                and profile.public == ("198.51.100.7", 22222),
                f"full={calls['full']} public={profile.public}",
            )

            with contextlib.suppress(OSError):
                sock.close()
    finally:
        stun_mod.measure = original_measure


def test_warm_relay_mapping_is_fire_and_forget():
    print("")
    print("---- 暖场只发数据报，不等 STUN 答案 ----")

    from mclink.core import direct as direct_mod
    from mclink.core import stun as stun_mod

    calls = {"measure": 0, "sent": 0}

    async def fake_measure(*_a, **_k):
        calls["measure"] += 1
        raise AssertionError("warm-up must not measure STUN any more")

    original_measure = stun_mod.measure
    stun_mod.measure = fake_measure

    try:
        link = direct_mod.DirectLink(object(), "127.0.0.1", 19100, log=_silent)
        # stun_port = 0 -> 和原来一样落到 DEFAULT_STUN_PORT。
        # public 保持 None：这不是"已经量过那个目的地"的情形，所以走发送分支。
        link.stun_port = 0
        link.public = None
        link.sock = object()      # 只要不是 None（发送走假 loop）

        class FakeLoop:
            async def sock_sendto(self, sock, data, target):
                calls["sent"] += 1

        started = time.monotonic()
        result = asyncio.new_event_loop().run_until_complete(
            _run_warm(link, FakeLoop())
        )
        elapsed = time.monotonic() - started

        check(
            "★ 暖场不再为了端口名去 await STUN（省 ~2s）",
            calls["measure"] == 0,
            f"measure={calls['measure']}",
        )
        check(
            "暖场把 outbound 包发出去了（映射靠它建立）",
            calls["sent"] == 3,
            f"sent={calls['sent']}",
        )
        check(
            "★ 立刻返回（不等答案），调用方退回 self.public",
            result is None and elapsed < 1.0,
            f"result={result!r} elapsed={elapsed:.3f}s",
        )
    finally:
        stun_mod.measure = original_measure


async def _run_warm(link, fake_loop):
    import mclink.core.direct as direct_mod

    original = asyncio.get_running_loop
    asyncio.get_running_loop = lambda: fake_loop

    try:
        return await link._warm_relay_mapping()
    finally:
        asyncio.get_running_loop = original


# ============================================================
# 5. relay / 没打洞时不出洞行
# ============================================================

def test_hole_rows_are_gated_by_punching():
    print("")
    print("---- 没在打洞就不许出「打洞中」的行 ----")

    from mclink.core.room_direct import RoomDirectLink

    class FakeEngine:
        def __init__(self):
            self.state = None

        def room_state(self):
            return self.state

        def log(self, *_a):
            return None

    engine = FakeEngine()

    class State:
        client_id = "me"
        host_name = "peer-1"
        members = 2
        is_host = False
        peer_ids = ["peer-1"]

    engine.state = State()
    link = RoomDirectLink(engine, log=_silent)

    rows = link.link_rows()
    check(
        "（对照）花名册里有人 -> 默认仍然出行",
        len(rows) == 1 and rows[0]["peer"] == "peer-1",
        repr(rows),
    )

    check(
        "★ punching=False -> 一行都不出（relay 模式的纯误导）",
        link.link_rows(punching=False) == [],
        repr(link.link_rows(punching=False)),
    )

    state = link.state()
    check(
        "★ 没在跑打洞时 state()['links'] 是空的",
        state.get("links") == [] and state.get("punching") is False,
        repr(state.get("links")),
    )

    # 已经打通的 peer：即便 punching 传 False 也不能把它抹掉（老中继/老快照）。
    class WarmLink:
        keepalive_running = True

    link.links["peer-1"] = {"link": WarmLink(), "addr": ("1.2.3.4", 5),
                            "ready_at": time.monotonic()}
    rows = link.link_rows(punching=False)
    check(
        "★ 已打通的洞不会因为 punching 传了 False 就消失",
        len(rows) == 1 and rows[0]["ready"] is True,
        repr(rows),
    )


# ============================================================
# 6. 进程级流量账本
# ============================================================

def test_traffic_ledger_counts_and_survives():
    print("")
    print("---- 进程级流量账本：分通路 / 不清零 / 速度永远有 ----")

    from mclink.core.traffic import DIRECT, RELAY, TrafficLedger

    ledger = TrafficLedger()

    ledger.record(RELAY, up=1000, down=2000)
    ledger.record(DIRECT, up=300, down=400)

    snap = ledger.snapshot()
    check(
        "★ 中转和对端分开记账",
        (snap["relay_up"], snap["relay_down"]) == (1000, 2000)
        and (snap["direct_up"], snap["direct_down"]) == (300, 400),
        repr(snap),
    )
    check(
        "合计 = 中转 + 对端",
        (snap["total_up"], snap["total_down"]) == (1300, 2400),
        repr(snap),
    )

    check(
        "未知通路被忽略，绝不抛异常",
        _record_unknown(ledger) is None,
    )
    check(
        "记一笔负数/垃圾值不会把账本弄坏",
        _record_junk(ledger)
        and ledger.snapshot()["total_up"] == 1300,
        repr(ledger.snapshot()),
    )

    # 速度：喂了流量就该有非 0，停下来就该衰减到 0。
    time.sleep(1.0)
    ledger.sample()
    up = ledger.snapshot()["up_rate"]
    check("★ 实时速度按累计值算出非 0", up > 0, f"{up:.0f} B/s")

    for _ in range(8):
        time.sleep(0.25)
        ledger.sample()

    snap = ledger.snapshot()
    check(
        "★ 空闲时速度衰减到 0（而不是消失 / 写 —）",
        snap["up_rate"] < 1024 and snap["down_rate"] < 1024,
        f"up={snap['up_rate']:.0f} down={snap['down_rate']:.0f}",
    )
    check(
        "空闲也不影响累计值（离开房间不清零）",
        snap["total_up"] == 1300 and snap["total_down"] == 2400,
        repr(snap),
    )


def _record_unknown(ledger):
    ledger.record("wireless", up=10, down=10)


def _record_junk(ledger):
    from mclink.core.traffic import RELAY as _RELAY

    ledger.record(_RELAY, up=-5, down="x")
    return True


def test_direct_pump_bytes_go_to_the_ledger():
    print("")
    print("---- 直连承载真的在数字节（原来一个都不数）----")

    from mclink.core import direct_carry
    from mclink.core.traffic import DIRECT, TrafficLedger

    ledger = TrafficLedger()

    class FakeTunnel:
        def send(self, *_a, **_k):
            return None

    class FakeChannel:
        def __init__(self):
            self.inbox = None

        def send(self, framed):
            return None

    class FakeStream:
        def __init__(self):
            self.sid = 1
            self.stop = None
            self.q = None
            self.up = 0
            self.down = 0

    class FakeLink:
        def __init__(self):
            self.bytes_up = 0
            self.bytes_down = 0
            self.on_traffic = None

        _note_traffic = direct_carry.PeerLink._note_traffic

    link = FakeLink()
    # 让 `_note_traffic` 落到这个新账本上。
    original_ledger = direct_carry.__dict__.get("_LEDGER_OVERRIDE", None)

    import mclink.core.traffic as traffic_mod

    original_factory = traffic_mod.ledger
    traffic_mod.ledger = lambda: ledger

    try:
        check(
            "★ PeerLink._note_traffic 把 direct 字节交给账本",
            _note_and_read(link, ledger) is None,
        )
        snap = ledger.snapshot()
        check(
            "★ 直连 up/down 都被数进去了",
            (snap["direct_up"], snap["direct_down"]) == (111, 222),
            repr(snap),
        )
        check(
            "链路自己的累计也跟着涨",
            (link.bytes_up, link.bytes_down) == (111, 222),
            f"{link.bytes_up}/{link.bytes_down}",
        )
    finally:
        traffic_mod.ledger = original_factory


def test_ledger_survives_a_session_stop():
    print("")
    print("---- 会话停了，累计流量也不许清零（只有重启客户端才清零）----")

    from mclink.core.engine import MCLinkEngine
    from mclink.core.traffic import DIRECT, RELAY

    engine = MCLinkEngine()

    check("（前提）没有会话时也能拿到账本快照",
          engine.snapshot().get("state") == "IDLE")

    engine.record_traffic(RELAY, up=500, down=600)
    engine.record_traffic(DIRECT, up=100, down=200)

    before = engine.traffic_snapshot()

    # 停一个根本不存在的会话：正是"离开房间"那条路。
    engine.stop_session(timeout=1)

    after = engine.snapshot()

    check(
        "★ 会话停了之后，累计数字原样还在（离开房间不清零）",
        after["total_up"] == 600 and after["total_down"] == 800,
        f"before={before} after_up={after.get('total_up')} "
        f"after_down={after.get('total_down')}",
    )
    check(
        "★ 没会话时速度仍然有数字（0），不是 `—`",
        isinstance(after.get("up_rate"), (int, float))
        and isinstance(after.get("down_rate"), (int, float)),
        repr((after.get("up_rate"), after.get("down_rate"))),
    )
    check(
        "中转 / 对端的拆分也原样保留",
        (after["relay_up"], after["relay_down"]) == (500, 600)
        and (after["direct_up"], after["direct_down"]) == (100, 200),
        repr(after),
    )


def _note_and_read(link, ledger):
    # 直接调真实实现；`traffic.ledger()` 已被替换成本账本。
    link._note_traffic(111, 222)


# ============================================================
# 7. 亚毫秒 RTT / T_PONG 序号
# ============================================================

def test_sub_millisecond_rtt_is_never_zero():
    print("")
    print("---- 亚毫秒 RTT 不许显示成 0 ----")

    os.environ.setdefault("DISPLAY", ":0")

    from mclink.gui import app as gui_app

    check(
        "★ 0.4ms -> `<1 ms`（不是 `0 ms`）",
        gui_app.p2p_rtt_text(0.4) == "<1 ms",
        gui_app.p2p_rtt_text(0.4),
    )
    check(
        "0.0ms 也是 `<1 ms`（真实但极短，不是「没测到」）",
        gui_app.p2p_rtt_text(0.0) == "<1 ms",
        gui_app.p2p_rtt_text(0.0),
    )
    check(
        "1.4ms -> `1 ms`（正常四舍五入）",
        gui_app.p2p_rtt_text(1.4) == "1 ms",
        gui_app.p2p_rtt_text(1.4),
    )
    check(
        "34.4ms -> `34 ms`",
        gui_app.p2p_rtt_text(34.4) == "34 ms",
        gui_app.p2p_rtt_text(34.4),
    )
    check(
        "None -> `-`（还没测到）",
        gui_app.p2p_rtt_text(None) == "-",
        gui_app.p2p_rtt_text(None),
    )
    check(
        "N/C 语义不变：不适用时不显示数字",
        gui_app.peer_latency_text(0.4, False) == "N/C",
        gui_app.peer_latency_text(0.4, False),
    )
    check(
        "适用时亚毫秒也标出来",
        gui_app.peer_latency_text(0.2, True) == "<1 ms",
        gui_app.peer_latency_text(0.2, True),
    )


def test_tunnel_pong_must_match_the_ping_sequence():
    print("")
    print("---- 隧道 keepalive：迟到/重复的 T_PONG 不许冒充新的一次 ----")

    from mclink.core import udptunnel as ut

    tunnel = ut.UdpTunnel.__new__(ut.UdpTunnel)
    tunnel._stop = __import__("threading").Event()
    tunnel._ping_sent_at = None
    tunnel._ping_sent_seq = None
    tunnel._ping_seq = 0
    tunnel.rtt_ms = None
    tunnel.log = _silent
    tunnel.send_to = ("127.0.0.1", 1234)
    tunnel._peer = ("127.0.0.1", 1234)
    tunnel.connected = True
    tunnel.last_rx_at = 0.0
    tunnel._expected = 1
    tunnel._last_nak = 0.0
    tunnel._reorder = {}
    tunnel._rx_lock = __import__("threading").Lock()
    tunnel._tx_lock = __import__("threading").Lock()
    tunnel._inflight = {}
    tunnel.hub = None
    sent = []
    tunnel._sendto = lambda data, addr: sent.append(data)

    def pong(seq):
        return struct.pack(">HBBII", ut.MAGIC, ut.T_PONG, 0, seq, 0)

    # 第 1 次 T_PING：序号 7，记下时刻。
    tunnel._ping_seq = 7
    tunnel._ping_sent_seq = 7
    tunnel._ping_sent_at = time.monotonic() - 0.030

    # 一份**迟到的旧 PONG**（旧序号）先到：不许拿它去减新时刻。
    tunnel._handle(pong(6), ("127.0.0.1", 1234))
    check(
        "★ 旧序号的 T_PONG 被忽略（不会算出假的 0ms）",
        tunnel.rtt_ms is None,
        repr(tunnel.rtt_ms),
    )

    # 正确序号的 PONG：量出 ~30ms。
    tunnel._handle(pong(7), ("127.0.0.1", 1234))
    check(
        "★ 序号对得上才算 RTT（~30ms）",
        tunnel.rtt_ms is not None and 20.0 <= tunnel.rtt_ms <= 200.0,
        repr(tunnel.rtt_ms),
    )

    # 老对端（永远回 seq=0）仍然要能用。
    tunnel.rtt_ms = None
    tunnel._ping_sent_seq = 9
    tunnel._ping_sent_at = time.monotonic() - 0.025
    tunnel._handle(pong(0), ("127.0.0.1", 1234))
    check(
        "★ 老对端固定回 seq=0 时仍然能量出 RTT（兼容性不能破）",
        tunnel.rtt_ms is not None and 15.0 <= tunnel.rtt_ms <= 200.0,
        repr(tunnel.rtt_ms),
    )

    # T_PONG 必须把收到的序号原样回过去（新对端）。
    sent.clear()
    tunnel._handle(
        struct.pack(">HBBII", ut.MAGIC, ut.T_PING, 0, 42, 0),
        ("127.0.0.1", 1234),
    )
    check(
        "★ 收到 T_PING 时，T_PONG 回的就是那个序号",
        bool(sent)
        and struct.unpack(">HBBII", sent[-1][:ut.HDR])[3] == 42,
        repr([struct.unpack(">HBBII", d[:ut.HDR]) for d in sent]),
    )


def test_presence_rtt_is_clamped():
    print("")
    print("---- presence 的 last_rtt_ms 不许是负数 ----")

    from mclink.core import presence

    monitor = presence.PresenceKeeper.__new__(presence.PresenceKeeper)
    monitor.last_rtt_ms = None
    monitor.running = False

    check(
        "PresenceKeeper 定义了 last_rtt_ms 字段",
        hasattr(monitor, "last_rtt_ms"),
    )


# ============================================================
# 8. 链路被拆掉之后，还挂着的那条流不许把会话打崩
#    （用户日志里的 `'NoneType' object has no attribute 'drop_stream'`）
# ============================================================

def test_player_stream_survives_a_link_teardown():
    print("")
    print("---- 洞被判死后收尾的玩家流：不许 drop_stream 到 None 上 ----")

    import logging
    import queue as queue_mod
    import threading

    from mclink.core import protocol as proto_mod
    from mclink.core.direct_carry import DirectCarrier

    class FakeChannel:
        """最小可用的 data channel：记下摘流，并让泵安静地立刻收工。"""

        def __init__(self):
            self.dropped = []
            self.streams = {7: None}

        def send(self, _framed):
            return None

        def drop_stream(self, sid):
            self.dropped.append(sid)
            self.streams.pop(sid, None)

    class FakeState:
        """`_pump_stream` 要的"一条 sid 流"的全部字段。"""

        def __init__(self):
            self.sid = 7
            self.stop = threading.Event()
            self.q = queue_mod.Queue()
            self.up = 0
            self.down = 0

    class FakeReader:
        """立刻 EOF：Minecraft 那边已经断了。"""

        async def read(self, _n=-1):
            return b""

    class FakeWriter:
        def write(self, _data):
            return None

        async def drain(self):
            return None

    class FakeLink:
        def __init__(self, channel):
            self.channel = channel
            self.bytes_up = 0
            self.bytes_down = 0
            self.active_streams = 0

        def _stream_started(self):
            self.active_streams += 1

        def _stream_ended(self):
            self.active_streams = max(0, self.active_streams - 1)

        def _note_traffic(self, _up=0, _down=0):
            return None

    async def scenario():
        channel = FakeChannel()
        link = FakeLink(channel)
        carrier = DirectCarrier(link, "peer-1", is_host=False, log=_silent)
        carrier.stream = FakeState()

        # ① 正常收尾：channel 还在 -> 必须按 sid 摘流，且不抛。
        await asyncio.wait_for(
            carrier.pump(FakeReader(), FakeWriter()), timeout=5
        )

        # ② 洞被判死：`PeerLink.close()` 已经把 channel 置 None，这条流才收尾。
        #    这就是用户日志里 12:17:12.736 / 12:29:45.187 那一幕。
        teardown_channel = FakeChannel()
        teardown_link = FakeLink(teardown_channel)
        teardown_carrier = DirectCarrier(
            teardown_link, "peer-1", is_host=False, log=_silent
        )
        teardown_carrier.stream = FakeState()
        teardown_link.channel = None          # ← close() 干的事

        await asyncio.wait_for(
            teardown_carrier.pump(FakeReader(), FakeWriter()), timeout=5
        )

        return channel, link, teardown_channel, teardown_link

    logging.disable(logging.CRITICAL)

    try:
        channel, link, teardown_channel, teardown_link = asyncio.run(scenario())
        raised = None
    except Exception as e:  # noqa: BLE001
        channel = link = teardown_channel = teardown_link = None
        raised = e
    finally:
        logging.disable(logging.NOTSET)

    check(
        "★ 链路已被拆掉时，玩家侧的 pump 不抛 AttributeError",
        raised is None,
        f"{type(raised).__name__}: {raised}",
    )
    check(
        "正常收尾仍然摘掉它那一节流",
        channel is not None and channel.dropped == [7],
        repr(channel.dropped if channel is not None else None),
    )
    check(
        "链路已经没了就不用再摘（channel 已是 None）",
        teardown_link is not None and teardown_link.channel is None,
        repr(teardown_link.channel if teardown_link else None),
    )
    check(
        "两条流都算收工（active_streams 归零）",
        link is not None
        and teardown_link is not None
        and link.active_streams == 0
        and teardown_link.active_streams == 0,
        f"{getattr(link, 'active_streams', '?')}/"
        f"{getattr(teardown_link, 'active_streams', '?')}",
    )


# ============================================================
# 9. NAT 画像：底层监测器的 standalone 能力 + 打洞前"太旧就立刻重测"
# ============================================================
#
# ★ 2026-09-28（设计基准 §E）：**策略**已经改成"点开始连接才测"，由
#   `engine.watch_room_nat()` 把关（见 gui_room_host_session_test 那条
#   「没点开始连接就不许测」）。这里测的是**底层 `RoomNatMonitor` 本身**
#   仍然支持"没有房间也能测"这个能力（网络诊断卡片 / 单测需要它），
#   它不再等于"打开软件就测"。

def test_room_nat_monitor_starts_without_a_room():
    print("")
    print("---- 底层监测器：给了地址就能测（策略层的开关在 engine）----")

    from mclink.core.session import RoomNatMonitor

    measured = {"calls": 0, "waits": 0}

    class FakeEngine:
        def __init__(self):
            self._loop = asyncio.new_event_loop()
            self.host = ""

        def loop(self):
            return self._loop

        def server_host(self):
            return self.host

    class FakeProfile:
        public = ("203.0.113.9", 41234)

    engine = FakeEngine()

    import mclink.core.session as session_mod

    async def fake_measure_nat(_host, log=None, sock=None):
        measured["calls"] += 1
        return FakeProfile()

    original = session_mod.measure_nat
    session_mod.measure_nat = fake_measure_nat

    # 把 90 秒那一拍压到很短的假实现（只动这次测试里的等待）。
    original_recheck = session_mod.NAT_RECHECK_S
    session_mod.NAT_RECHECK_S = 0.05

    monitor = RoomNatMonitor(engine, log=_silent)

    try:
        # 没有 standalone_host、engine 也没有中继地址：什么都不测（不空转）。
        monitor.start()
        engine._loop.run_until_complete(asyncio.sleep(0.1))
        check(
            "没有中继地址时不测（也不落噪音）",
            measured["calls"] == 0,
            repr(measured),
        )

        # 打开软件：交给它一个中继地址 —— 必须**立刻**开测，不用等进房间。
        engine.host = "relay.example"
        monitor.start(standalone_host="relay.example")
        engine._loop.run_until_complete(asyncio.sleep(0.2))

        check(
            "★ 没房间也开测了（打开软件就测）",
            measured["calls"] >= 1,
            repr(measured),
        )
        check(
            "★ 测出来的结果就是「当前状态」（画像已发布 + 记了时间）",
            monitor.profile is not None and monitor.profile.public is not None,
            repr(monitor.profile),
        )
        check(
            "画像年龄可读（界面/打洞共用同一个来源）",
            session_mod.nat_profile_age(monitor) is not None,
            repr(session_mod.nat_profile_age(monitor)),
        )

        # 静默重测：等够一拍，必须自己再来一轮（覆盖旧结果）。
        before = measured["calls"]
        engine._loop.run_until_complete(asyncio.sleep(0.25))
        check(
            "★ 间隔到了会静默重测（覆盖此前结果）",
            measured["calls"] > before,
            f"{before} -> {measured['calls']}",
        )
    finally:
        session_mod.measure_nat = original
        session_mod.NAT_RECHECK_S = original_recheck
        monitor.stop()
        engine._loop.run_until_complete(asyncio.sleep(0.05))
        engine._loop.close()


def test_engine_pokes_a_stale_nat_profile_before_punching():
    print("")
    print("---- 打洞要开始时，太旧的画像让监测立刻重测一轮（带冷却）----")

    from mclink.core.engine import MCLinkEngine

    engine = MCLinkEngine.__new__(MCLinkEngine)
    engine._nat_monitor = None
    engine._room_direct = None
    engine._last_nat_poke_at = 0.0

    pokes = {"n": 0}

    class FakeMonitor:
        running = True

        def __init__(self):
            self.profile_at = 0.0        # 从来没测过 = 太旧

        def poke(self):
            pokes["n"] += 1

    class FakePunch:
        def start(self):
            return None

        def close(self):
            return None

    engine._nat_monitor = FakeMonitor()

    import mclink.core.room_direct as room_direct_mod

    original = room_direct_mod.RoomDirectLink
    room_direct_mod.RoomDirectLink = lambda *a, **k: FakePunch()

    # 压缩冷却，测试才能在几百毫秒里验证"不会每秒都戳"。
    original_cd = MCLinkEngine.NAT_POKE_COOLDOWN_S
    MCLinkEngine.NAT_POKE_COOLDOWN_S = 0.05

    try:
        engine._room_direct = FakePunch()

        engine.watch_room_direct(True, log=_silent)
        check("画像从没测过 -> 立刻戳一次", pokes["n"] == 1, repr(pokes))

        # 一秒一拍：冷却期内不许再戳（否则就是每秒重测一遍）。
        engine.watch_room_direct(True, log=_silent)
        engine.watch_room_direct(True, log=_silent)
        check("冷却期内不重复戳", pokes["n"] == 1, repr(pokes))

        import time as time_mod

        time_mod.sleep(0.08)
        engine.watch_room_direct(True, log=_silent)
        check("冷却过了才允许再戳", pokes["n"] == 2, repr(pokes))

        # 画像新鲜时什么都不做（用户规则：用当前测量状态，不重测）。
        engine._nat_monitor.profile_at = time_mod.monotonic()
        engine._last_nat_poke_at = 0.0
        engine.watch_room_direct(True, log=_silent)
        check("画像够新 -> 不戳（不重测）", pokes["n"] == 2, repr(pokes))
    finally:
        room_direct_mod.RoomDirectLink = original
        MCLinkEngine.NAT_POKE_COOLDOWN_S = original_cd


# ============================================================

def main():
    print("=" * 64)
    print("MC-Link direct-carrier / punch fast path / traffic ledger tests")
    print("=" * 64)

    test_host_carry_passes_the_relay_reader()
    test_measure_reuses_the_room_profile()
    test_measure_falls_back_when_the_hint_is_stale_or_unknown()
    test_warm_relay_mapping_is_fire_and_forget()
    test_hole_rows_are_gated_by_punching()
    test_traffic_ledger_counts_and_survives()
    test_ledger_survives_a_session_stop()
    test_direct_pump_bytes_go_to_the_ledger()
    test_player_stream_survives_a_link_teardown()
    test_room_nat_monitor_starts_without_a_room()
    test_engine_pokes_a_stale_nat_profile_before_punching()
    test_sub_millisecond_rtt_is_never_zero()
    test_tunnel_pong_must_match_the_ping_sequence()
    test_presence_rtt_is_clamped()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
