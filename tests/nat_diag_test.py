"""
MC-Link -- NAT 诊断（只读的「网络诊断」卡片）

============================================================================
 这一套测什么
============================================================================

v3 的第一刀是「网络诊断」界面：把 NAT 判定结果显示出来。STUN 栈已经整体
换成 MCLanP2P 的移植版（见 `client/mclink/core/stun.py` 头部），所以这一套
盯的是**卡片与移植栈的接缝**，而不是重新测一遍探测算法（那是
`stun_stack_test.py` 的活）：

  1. **只读**：诊断跑的时候引擎事件循环必须照常跑。他的探测是阻塞的，我们
     用 executor 线程兜住 —— 兜漏了就是线上"整个客户端卡死"。
  2. **字段齐全**：卡片要显示 NAT 类型 / 公网映射 / 服务器响应数 / 结论，
     而新 profile 的字段名和旧的**不一样**了（`nat_type` 取代 `subtype`）。
     这一层不测，界面就会在运行时 AttributeError。
  3. **文案走 tr()**：英文/日文界面不许残留中文。

Run:
    python tests/nat_diag_test.py
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

from mclink.core import punch  # noqa: E402
from mclink.core import stun  # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def make_profile(nat_type=punch.NatSub.CONE, filtering=punch.Filter.EIF,
                 public=("203.0.113.7", 41000), servers=None):
    """A profile shaped exactly like the ported stack produces."""
    profile = stun.NatProfile()
    profile.public = public
    profile.nat_type = nat_type
    profile.filtering = filtering
    profile.coarse = "cone" if nat_type is punch.NatSub.CONE else "symmetric"
    profile.step = 1
    profile.mapped_ports = [public[1]] if public else []
    profile.label = stun.nat_label(profile.coarse, nat_type.value,
                                   filtering.value)

    for host, port, mapped in (servers or ()):
        profile.servers.append((host, port, mapped))

    return profile


# ============================================================
# 1. 卡片要读的字段，移植后必须仍然齐全
# ============================================================

def test_profile_contract():
    print("")
    print("---- 诊断卡片读的字段（移植后契约不能变）----")

    profile = make_profile(servers=[
        ("relay.example", 3478, ("203.0.113.7", 41000)),
        ("stun.miwifi.com", 3478, None),
    ])

    check("公网映射可读", profile.public == ("203.0.113.7", 41000),
          str(profile.public))
    check("NAT 子类型可读", profile.nat_type is punch.NatSub.CONE)
    check("过滤行为可读", profile.filtering is punch.Filter.EIF)
    check("人话结论可读（他的 nat_label）", bool(profile.label), profile.label)

    check(
        "★ 服务器列表可读（卡片要按服务器显示成功/失败）",
        len(profile.servers) == 2,
        str(profile.servers),
    )
    check("响应计数正确", profile.responded_servers == 1,
          str(profile.responded_servers))

    probes = profile.probes

    check("probes 兼容层可用（每行有 usable / error）",
          len(probes) == 2 and probes[0].usable and not probes[1].usable,
          str([(p.server, p.usable) for p in probes]))

    check("摘要能打出来且不炸", len(profile.summary_lines()) >= 3,
          str(len(profile.summary_lines())))

    empty = stun.NatProfile()

    check("刚构造的空 profile 也不会让界面炸",
          empty.public is None and empty.responded_servers == 0
          and empty.observed_ports == [],
          f"{empty.public} {empty.responded_servers}")


# ============================================================
# 2. GUI 纯函数：用移植后的 profile 渲染
# ============================================================

def test_card_renderers():
    print("")
    print("---- 卡片渲染（不需要显示器）----")

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

    try:
        from mclink.gui import app as gui_app
    except Exception as e:  # pragma: no cover - defensive
        check("gui/app.py 可导入（stub tkinter 后）", False, repr(e))
        return

    check("gui/app.py 可导入（stub tkinter 后）", True)

    profile = make_profile(servers=[
        ("relay.example", 3478, ("203.0.113.7", 41000)),
        ("stun.miwifi.com", 3478, None),
    ])

    text, _hint = gui_app.nat_diag_value("type", profile)
    check(
        "★ NAT 类型那行用他的文案（含「锥型」）",
        "锥型" in text and profile.nat_type.value in text,
        text,
    )

    text, _hint = gui_app.nat_diag_value("mapping", profile)
    check("公网映射那行是 ip:port", text == "203.0.113.7:41000", text)

    text, hint = gui_app.nat_diag_value("servers", profile)
    check("部分应答 -> 警告色含义", hint == "warn" and "/" in text,
          f"{text} hint={hint}")

    text, _hint = gui_app.nat_diag_value("advice", profile)
    check("结论建议非空（他的判定 + 我们的解释）", bool(text), text)

    hard = make_profile(
        nat_type=punch.NatSub.HARD,
        filtering=punch.Filter.APDF,
        servers=[("relay.example", 3478, ("203.0.113.7", 41000))],
    )
    text, _hint = gui_app.nat_diag_value("type", hard)

    check(
        "★ 对称·随机那种结论也能显示出来（不是只认锥形）",
        "对称" in text,
        text,
    )

    detail = "\n".join(gui_app.nat_diag_detail_lines(hard))
    check("明细里列出了每个服务器的结果", "relay" in detail or "stun" in detail,
          detail[:80])

    from mclink.gui import i18n

    missing = [key for key, _kind in gui_app.NAT_DIAG_ROWS if key not in i18n.EN]
    check("卡片每个字段名都有英文翻译", not missing, str(missing))


# ============================================================
# 3. 只读：诊断期间引擎循环照常跑
# ============================================================

def test_engine_loop_survives_diagnostics():
    """
    整条链路（engine.detect_nat -> executor -> 他的阻塞探测）都不能冻住循环。
    """
    print("")
    print("---- 只读铁律：诊断期间引擎事件循环照常跑 ----")

    from mclink.core.engine import MCLinkEngine

    engine = MCLinkEngine()
    engine.start_loop()

    beats = []

    async def ticker():
        for _ in range(400):
            beats.append(time.monotonic())
            await asyncio.sleep(0.02)

    future = asyncio.run_coroutine_threadsafe(ticker(), engine._loop)
    time.sleep(0.25)
    before = len(beats)

    started = time.time()

    # 黑洞地址（RFC 5737 TEST-NET-1）：拖满预算，证明"等过了"而不是空跑。
    profile = engine.detect_nat(servers=["192.0.2.1:3478"], timeout=2.0)
    elapsed = time.time() - started
    during = len(beats)

    check("黑洞服务器确实拖了时间（不是空跑）", elapsed >= 1.5,
          f"{elapsed:.2f}s")
    check(
        "★ 诊断期间看门狗不停跳（引擎 loop 没被冻住）",
        during > before + 10,
        f"诊断前 {before} 跳，诊断后 {during} 跳",
    )
    check(
        "黑洞服务器 -> 未知类型，而且界面能解释为什么",
        profile.nat_type is punch.NatSub.UNKNOWN and bool(profile.errors),
        f"{profile.nat_type} {profile.errors}",
    )
    check("一次诊断不会把引擎线程/会话搞坏",
          engine.current_session() is None and engine._thread.is_alive())

    future.cancel()
    engine.shutdown(timeout=5)


def test_room_monitor_contract():
    """
    「选了 STUN 模式、加入房间就开始实时检测，而且一直检测，不是下个结论就不变了」

    ============================================================================
      WHY THIS TEST IS SHAPED LIKE THIS
    ============================================================================

    That requirement contains three separate claims, and each can fail alone:

      1. it starts WITHOUT a session (you have joined a room but not pressed
         "start connection"), which is why the monitor belongs to the room;
      2. it keeps going, and every measurement reuses the SAME socket -- the
         source port is part of what is being measured, so a fresh port per
         probe would make consecutive verdicts describe different allocations;
      3. leaving the room (or leaving STUN mode) stops it WITHOUT throwing the
         last profile away: a blanked row reads as "failed".

    The probe is stubbed on purpose: this test is about the SCHEDULE and the
    plumbing. A real probe against a real STUN server is stun_stack_test.py's
    job.
    """
    print("")
    print("---- 房间级实时检测：加入 STUN 房间就开始，一直在测，离开才停 ----")

    from mclink.core import session as sess_mod
    from mclink.core.engine import MCLinkEngine

    engine = MCLinkEngine()
    engine.start_loop()
    engine._relay_host = "127.0.0.1"

    calls = []

    async def fake_measure(server_host, log=None, sock=None):
        calls.append((server_host, sock))
        profile = stun.NatProfile()
        profile.public = ("203.0.113.9", 40000 + len(calls))
        profile.nat_type = punch.NatSub.CONE
        profile.filtering = punch.Filter.EIF
        return profile

    real_measure = sess_mod.measure_nat
    real_interval = sess_mod.NAT_RECHECK_S

    # Shorten the interval so the test does not take 90 seconds, and stub the
    # probe so it needs no internet.
    sess_mod.measure_nat = fake_measure
    sess_mod.NAT_RECHECK_S = 0.25

    try:
        engine.watch_room_nat(True, log=lambda *_a: None)

        deadline = time.time() + 4.0

        while len(calls) < 3 and time.time() < deadline:
            time.sleep(0.05)

        check(
            "★ 没有会话也开测（加入房间就开始，不必先按开始连接）",
            len(calls) >= 1,
            f"calls={len(calls)}",
        )
        check(
            "★ 一直在测，不是只下一个结论（重测周期到了会再测）",
            len(calls) >= 2,
            f"calls={len(calls)}",
        )
        check(
            "★ 每次都用同一个 socket（源端口是判定的一部分，换了就没法比）",
            len({id(sock) for _host, sock in calls}) == 1,
            f"{len({id(sock) for _host, sock in calls})} sockets",
        )
        check(
            "★ 测量用的是当前中继主机地址（STUN 服务器由它派生）",
            all(host == "127.0.0.1" for host, _sock in calls),
            str(calls[:2]),
        )

        profile = engine.room_nat_profile()

        check(
            "测量结果挂到引擎上，状态卡片能读到",
            profile is not None and profile.public is not None,
            str(profile),
        )

        # ---- 停止：离开房间 / 切回 relay ----
        engine.watch_room_nat(False)

        check(
            "★ 停止之后最后一次结果仍然保留（不能变回「未检测」）",
            engine.room_nat_profile() is profile,
            str(engine.room_nat_profile()),
        )

        settled = len(calls)
        time.sleep(0.7)

        check(
            "★ 停止之后不再探测",
            len(calls) == settled,
            f"{settled} -> {len(calls)}",
        )
    finally:
        sess_mod.measure_nat = real_measure
        sess_mod.NAT_RECHECK_S = real_interval
        engine.shutdown(timeout=5)


def test_room_punch_starts_with_the_room():
    """
    房间级打洞：两个人在房间里就开始，不等 Minecraft。

    ============================================================================
      WHY THIS TEST WAS REWRITTEN (and what it must keep proving)
    ============================================================================

    It used to drive `session.RoomPunch`. That class was an earlier attempt at
    the same thing, it was superseded by `core/room_direct.py`, and the two were
    BOTH LIVE at one point -- which is what produced the owner's punch storm:
    41 punches and 13 "ROOM LINK UP" in one session, because two rival loops
    fought over the same peer, each reopening the socket (a new NAT mapping) and
    therefore destroying the hole the other had just made.

    So this test now guards the properties that make the storm impossible:

      1. a single member does NOT punch (nobody to collide with);
      2. a second member starts it, without any Minecraft session;
      3. the inbound probe runs BEFORE the first punch;
      4. once it succeeds the loop STOPS and the link is KEPT -- re-punching
         would open a new socket and lose the hole;
      5. the punch state is handed to the status card in ONE shape, so the rows
         cannot contradict each other.

    Deliberately offline: DirectLink is stubbed, because this is about WHEN the
    loop runs. The real punch is covered by punch_test / direct_cand_test /
    direct_e2e_test.
    """
    print("")
    print("---- 房间级打洞：两个人在房间里就开始，不等 Minecraft ----")

    from mclink.core import room_direct

    class FakeState:
        def __init__(self, members):
            self.members = members
            self.host_name = "peer-name"
            self.client_id = "my-cid"
            self.is_host = False

    class FakeRoom:
        def __init__(self, members):
            self.state = FakeState(members)

    class FakeEngine:
        def __init__(self, room):
            self._room = room
            self._host = "127.0.0.1"
            self._port = 19100

        def room_current(self):
            return self._room

        def room_state(self):
            return self._room.state if self._room else None

        def server_host(self):
            return self._host

        def server_port(self):
            return self._port

        def loop(self):
            return None

    order = []

    class FakeNat:
        per_ip_pool = False

    class FakeLink:
        def __init__(self, room, host, port, log=None, my_id="", peer_id="",
                     is_host=False):
            self.closed = False
            self.step = None
            self.nat = FakeNat()
            self.inbound_note = "stubbed"
            self.sock = None
            self.peer_addr = None
            self.keepalive_alive = False
            # 3.0.3: the real DirectLink gained `is_host` (the host addresses its
            # direct lines with `to=<peer>`). The stub must accept the same
            # signature, or the punch loop fails at construction.
            self.is_host = bool(is_host)

        async def probe_inbound(self, timeout=4.0):
            order.append("inbound")
            return "punchable"

        async def measure(self, force=False):
            order.append("measure")
            return self.nat

        async def advertise(self):
            order.append("advertise")
            return ("5.6.7.8", 6000)

        async def attempt(self, timeout=8.0, per_ip_pool=False):
            order.append("punch")
            return type("R", (), {
                "ok": True, "addr": ("5.6.7.8", 6000), "sent": 10,
                "elapsed": 0.1, "plan": "c2c", "reason": "",
            })()

        # A real successful punch starts the keepalive, and readiness is
        # defined as "punched AND being kept warm" -- because a mapping that is
        # not kept warm is on its way out (see DirectLink.start_keepalive).
        # The stub has to honour that contract or it is testing a world that
        # cannot exist.
        # The real link keeps the hole warm on its own THREAD, so that the
        # keepalive outlives the punch loop and every session that rides it.
        # (An asyncio task was entangled with the session's task tree and got
        # cancelled with it -- see DirectLink.start_keepalive.)
        def start_keepalive(self, interval=5.0):
            self.keepalive_alive = True

        def stop_keepalive(self):
            self.keepalive_alive = False

        @property
        def keepalive_running(self):
            return bool(getattr(self, "keepalive_alive", False))

        def close(self):
            self.closed = True
            self.stop_keepalive()

    import asyncio

    import mclink.core.direct as direct_mod

    real_link = direct_mod.DirectLink
    direct_mod.DirectLink = FakeLink

    try:
        import asyncio

        # ---- 1. one member: must NOT punch ----
        punch = room_direct.RoomDirectLink(
            FakeEngine(FakeRoom(1)), log=lambda *a, **k: None
        )

        async def one_member():
            task = asyncio.get_running_loop().create_task(punch._run())
            await asyncio.sleep(0.15)
            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(one_member())

        check(
            "★ 房间里只有一个人时不打洞（没有对端，喷了也没人接）",
            order == [],
            str(order),
        )
        check("而且状态说明还在等第二个人", not punch.ready, punch.describe())

        # ---- 2. two members: inbound first, then advertise + punch ----
        punch = room_direct.RoomDirectLink(
            FakeEngine(FakeRoom(2)), log=lambda *a, **k: None
        )
        order.clear()

        captured = {}

        async def two_members():
            task = asyncio.get_running_loop().create_task(punch._run())

            for _ in range(150):
                await asyncio.sleep(0.02)

                if punch.ready:
                    break

            # Give the loop a chance to (wrongly) re-punch after success.
            await asyncio.sleep(0.4)

            # Read the state WHILE the loop is alive.
            #
            # `ready` means "punched AND being kept warm", and the keepalive is
            # a task on THIS loop -- so after `asyncio.run` returns, every task
            # has been cancelled and readiness legitimately reads False. The
            # assertion has to be made in here, which is also where a real
            # client lives.
            captured["ready"] = punch.ready
            captured["peer"] = punch.peer_addr
            captured["state"] = punch.state()
            captured["punches"] = order.count("punch")

            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(two_members())

        check(
            "★ 第二个人一进房就开始打洞（不等 Minecraft 连接）",
            "punch" in order,
            str(order),
        )
        check(
            "★ 入站探测在第一次打洞之前跑（否则永远是「入站未测」）",
            "inbound" in order
            and order.index("inbound") < order.index("punch"),
            str(order),
        )
        check(
            "★ 打通后记下直连路径，并且通道是【被保温的】（ready 看的是 keepalive）",
            captured.get("ready") and captured.get("peer") == ("5.6.7.8", 6000),
            f"ready={captured.get('ready')} peer={captured.get('peer')}",
        )
        check(
            "★ 打通后【不再重打】—— 重打就是新 socket = 新映射 = 洞没了",
            captured.get("punches") == 1,
            f"punched {captured.get('punches')} time(s): {order}",
        )
        check(
            "入站结论被保留（不是打完洞就丢了）",
            punch.inbound_state == "punchable",
            punch.inbound_state,
        )

        # ---- 3. the card shape: ONE story, no contradictions ----
        state = captured.get("state") or {}

        check(
            "★ 状态卡一次拿到全部字段（不会四行读四个对象）",
            state.get("direct_via") == "udp"
            and "5.6.7.8" in (state.get("direct_peer") or "")
            and state.get("inbound_state") == "punchable"
            and state.get("direct_step") == "",
            str(state),
        )
        # 注意 `direct_attempts` 现在**打通后归零**（用户 2026-09-24 要求：
        # 每次连上以后重新算），所以这里不能再要求它 >= 1 —— 那是在钉旧行为。
        check(
            "★ 已直连时不再同时说「等待开始/正在尝试」",
            state.get("direct_step") == "",
            str(state),
        )
    finally:
        direct_mod.DirectLink = real_link


def test_punched_socket_is_the_one_used():
    """
    打洞命中的是【阵列里的那个 socket】，隧道必须建在它上面，不是发布端口。

    ============================================================================
      THIS IS THE "显示打通了但根本连不上" BUG, PINNED DOWN
    ============================================================================

    `PunchArray.detach()` hands the winning socket to the caller and
    `array.close()` closes everything still inside it. On the s2c / birthday /
    sym_to_cone roles the hit lands on an ARRAY socket, never on the
    published-port hub socket -- the peer heard us from that array socket, so
    that is the mapping its NAT admits.

    I kept only the hub socket, so the array closed the winning socket at the
    end of the punch and the direct link was built on the wrong port. The card
    said 直连, the punch was real, and every game connection timed out.

    Since v3.0.1 the carrier is the reference project's `UdpTunnel`, so what
    matters is which socket the `PeerLink` hands to it -- that is what this
    checks. (`PeerLink.__init__` also `carried`-marks the punch link so the
    room does not re-punch; the fake link needs those attributes.)
    """
    print("")
    print("---- 打洞命中的 socket 必须被会话使用 ----")

    import socket

    import mclink.core.direct_carry as carry

    class FakeLink:
        def __init__(self):
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("127.0.0.1", 0))
            self.punched_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.punched_sock.bind(("127.0.0.1", 0))
            self.peer_addr = ("127.0.0.1", 9)
            self.carried = False
            self.keepalive_thread = None

    link = FakeLink()
    peer = carry.PeerLink("peer-a", link)

    check(
        "★ 直连隧道用的是【命中那个 socket】的端口，不是发布端口",
        peer.sock is link.punched_sock and peer.sock is not link.sock,
        f"link uses {peer.sock.getsockname()}",
    )

    # ========================================================================
    #  隧道必须发往【这一洞命中的地址】，不是对端公告的地址
    # ========================================================================
    #
    # 同宿舍/同路由器的一对机器上，命中的是局域网地址（192.168.x.x:port），
    # 而对端公告的是公网地址 —— 两者端口都不一样。隧道若发往公告地址，HELLO
    # 永远握不上手，房间就会不停重打。用户实测日志原文：
    #
    #   [direct] ROOM LINK UP: peer=192.168.2.128:63271   <- 命中局域网
    #   [UDP] punching 203.0.113.14:5383 ...            <- 隧道却发公网
    #   [direct] tunnel handshake failed; re-punching      <- 无限重打
    hit = ("192.168.2.128", 63271)
    peer_hit = carry.PeerLink("peer-a", link, peer_addr=hit)

    check(
        "★ 隧道发往【命中地址】（局域网），而不是对端公告的公网地址",
        tuple(peer_hit.addr) == hit and peer_hit.addr != tuple(link.peer_addr),
        f"addr={peer_hit.addr} 公告={link.peer_addr}",
    )

    check(
        "没有命中地址时才退回公告地址（不炸）",
        tuple(peer.addr) == tuple(link.peer_addr),
        f"addr={peer.addr}",
    )

    # No punched socket recorded (older/an odd path): fall back to the hub.
    link2 = FakeLink()
    link2.punched_sock = None

    peer2 = carry.PeerLink("peer-b", link2)

    check(
        "没有记录命中 socket 时退回发布端口（不会炸）",
        peer2.sock is link2.sock,
    )

    link.sock.close()
    link.punched_sock.close()
    link2.sock.close()


# ============================================================
# 4. 运行状态「通路」行：不许断言不存在的通路
# ============================================================

def test_path_row():
    """
    The 通路 row got this wrong twice, both in the same direction: it
    claimed a path that did not exist. Pinned down here.
    """
    print("")
    print("---- 运行状态「通路」行 ----")

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

    f = gui_app.nat_diag_path_text

    # ★ 用户报的 bug：STUN 模式 + 没连接 -> 以前显示"中转"
    text, hint = f("stun", None, connected=False)

    check(
        "★ 未连接时不许显示「中转」（STUN 模式下尤其）",
        text != "中转",
        f"显示的是 {text!r}",
    )
    check("未连接时显示「未连接」", text == "未连接", text)

    # 用户报的第二个：没连接时 mode 拿不到，于是掉进 else 显示中转
    check(
        "★ 未连接 + relay 模式也不谎报「中转」",
        f("relay", None, connected=False)[0] == "未连接",
    )

    # 连上之后各状态
    check(
        "连上 + 正在走直连 -> 直连 P2P",
        f("stun", "udp", True, carrying=True)[0].startswith("直连 P2P"),
    )

    # ============================================================================
    #  【2026-09-24 用户明确推翻旧规则】打通了就是直连，不等待任何游戏
    # ============================================================================
    #
    # 上一版这里钉的是「已打通（等待游戏）」—— 理由是"洞通了不等于游戏跑起来，
    # 不许谎报直连"。方向没错，但**模型错了**：用户的原话是
    # "通道是长连接一直在的，不要等也不要依靠任何游戏或者进程的会话"。
    # 也就是说直连**不是**某个游戏会话的附属状态，洞通了那条 P2P 通道就是
    # 存在的、可用的；说"等待游戏"反而是错的。
    #
    # 所以这一条现在反过来钉：只打通、没有任何游戏时，也必须说「直连 P2P」，
    # 而且**绝不许再出现"等待游戏"这几个字**。
    ready_text = f("stun", "udp", False, peer="1.2.3.4:5", punch_ready=True)[0]

    check(
        "★ 打通了就是直连（长连接），不许写「等待游戏」",
        ready_text == "直连 P2P 1.2.3.4:5" and "等待游戏" not in ready_text,
        f"显示的是 {ready_text!r}",
    )
    check(
        "★ 连上 + 直连失败 -> 「直连失败」而不是「中转」",
        f("stun", "failed", True)[0] == "直连失败",
        str(f("stun", "failed", True)),
    )
    check("连上 + relay 模式 -> 中转", f("relay", None, True)[0] == "中转")
    check("连上 + STUN 模式还在打洞 -> 直连尝试中",
          f("stun", None, True)[0] == "直连尝试中")

    check(
        "每种结果都有颜色含义（界面不会拿到 None）",
        all(f(m, v, c)[1] for m in ("relay", "stun")
            for v in (None, "udp", "failed", "relay") for c in (True, False)),
    )



# ============================================================

def test_nat123_grading():
    """
    NAT1 / NAT2 / NAT3 细分（用户 2026-09-25 报的错）。

    用户原话：「这个 NAT 类型测量不准确，我这里被测出来是 NAT2 类型，但是实际上
    我是 NAT3，现实中 NAT2 还是比较少见的，你可以参考 https://mao.fan/mynat
    这里测 NAT 还是挺准的，还有比特彗星测量也很准。」

    根因（两条，都在 `core/stun.py`）：
      * 过滤探测**顺序错了**：先问"换 IP+端口"，而单 IP 的中继只能从兄弟端口
        (3479) 答；那次放行把 apdf NAT 的放行表暖了，后面"只换端口"再问就
        通过了 → 读成 adf = NAT2；
      * 过滤探测**用了打洞 socket**，而它测映射时已经往 3478 **和 3479** 都
        发过包 —— 等于自己把端口轴暖了。

    现在：先"只换端口"、后"换 IP+端口"、并且过滤探测用**全新 socket**。
    这一套把"显示成什么"钉死（探测算法本身在 stun_stack_test 里）。
    """
    print("")
    print("---- NAT1/2/3 细分：锥型 + 过滤那一档 ----")

    import importlib

    app = importlib.import_module("mclink.gui.app")

    # ---- 组合规则 ----
    cases = [
        # (nat 子类型, 过滤档, 入站结论, 期望文案, 期望挡位)
        # ★ 用户 2026-10-02：NAT1 与 NAT2 **合并**成一档（完全锥形 / 地址受限
        #   在 P2P 上难度相同），所以两者都期望同一句话、同一个颜色。
        (
            punch.NatSub.CONE,
            punch.Filter.EIF,
            "unknown",
            "NAT1/2（完全锥形或地址受限锥形·P2P 轻松）",
            "eif",
        ),
        (
            punch.NatSub.CONE,
            punch.Filter.ADF,
            "unknown",
            "NAT1/2（完全锥形或地址受限锥形·P2P 轻松）",
            "adf",
        ),
        (punch.NatSub.CONE, punch.Filter.APDF, "blocked", "NAT3 端口受限", "apdf"),
        (punch.NatSub.HARD, punch.Filter.APDF, "blocked", "NAT4", "apdf"),
    ]

    for sub, filt, inbound, want_text, want_grade in cases:
        profile = make_profile(nat_type=sub, filtering=filt)
        snapshot = {"inbound_state": inbound}
        values = app.nat_diag_status_values(profile, snapshot, "stun")
        nat_text = values["nat"][0]

        check(
            f"★ {sub.value}/{filt.value} → {want_text}",
            want_text in nat_text,
            nat_text,
        )

        grade = app._filter_grade(inbound, filt)
        check(
            f"  {sub.value}/{filt.value} 的过滤挡位 = {want_grade}",
            grade == want_grade,
            grade,
        )

    # ---- ★ 用户这一台的场景：NAT3（端口受限）+ 换端口测出来 ----
    profile = make_profile(nat_type=punch.NatSub.CONE, filtering=punch.Filter.APDF)
    values = app.nat_diag_status_values(profile, {"inbound_state": "blocked"}, "stun")

    check(
        "★★ 用户的场景：锥型 + apdf 必须显示 NAT3 端口受限（不是 NAT2）",
        "NAT3 端口受限" in values["nat"][0],
        values["nat"][0],
    )

    # ---- 入站那一行也必须跟着一致（中等 ↔ 严格不能互相打架）----
    for inbound, filt, want in (
        ("unknown", punch.Filter.APDF, "入站严格"),
        ("blocked", punch.Filter.APDF, "入站严格"),
        ("unknown", punch.Filter.ADF, "入站中等"),
        ("unknown", punch.Filter.EIF, "入站宽松"),
    ):
        text, _hint = app.inbound_status_text(
            {"inbound_state": inbound}, filt
        )

        check(
            f"入站行：measured={filt.value} inbound={inbound} → {want}",
            text == want,
            text,
        )

    # ---- ★ 「探测到达」只能证明"不是端口受限"，不能证明全锥 ----
    # 中继的探测包源地址就是我们中继那个公网 IP，而我们为测 STUN 已经往它
    # 发过包 —— 地址受限(adf) 的 NAT 一样会放行。
    # ★ 用户 2026-10-01：老断言是"探测到达 → 中等(adf)"。但探测的源地址正是
    #   我们为了测 STUN 联系过的那个中继 IP —— 地址受限的 NAT 一样放行，所以
    #   它只能证明"不是 apdf"，证明不了"就是 adf"。现在给区间（未定）。
    # `punch.Filter.ADF` 这个值本身现在是**硬证据**：`stun.py` 只有在"换 IP
    # 真的测过（服务器有第二个公网 IP）却没答回来"时才返回它。所以这里仍然是
    # adf —— 变的只是"单 IP 的 STUN 不再返回 adf"（那时返回 adf_or_eif）。
    check(
        "★★ 换端口答了 adf（硬证据，多 IP 服务器）+ 探测到达 → 中等",
        app._filter_grade("open", punch.Filter.ADF) == "adf",
        app._filter_grade("open", punch.Filter.ADF),
    )
    check(
        # ★ 用户 2026-10-03：入站档位**只由 STUN 测**。STUN 没给出过滤档
        #   （服务器不实现 CHANGE-REQUEST）时就是「未测」—— 中继入站探测
        #   （源地址是我们联系过的 IP）**不许**造出一个"中等"来。
        "★★ 服务器没实现 CHANGE-REQUEST 时，探测到达 → 未测（STUN 没测到就是没测到）",
        app._filter_grade("open", stun.FILTER_UNKNOWN) == "unknown",
        app._filter_grade("open", stun.FILTER_UNKNOWN),
    )
    check(
        "★ 有换 IP 硬证据（多 IP 服务器）时才是 adf",
        app._filter_grade("unknown", punch.Filter.ADF) == "adf",
        app._filter_grade("unknown", punch.Filter.ADF),
    )
    check(
        "只有「换 IP 的应答真的从另一个 IP 回来」才算全锥（宽松）",
        app._filter_grade("open", punch.Filter.EIF) == "eif",
        app._filter_grade("open", punch.Filter.EIF),
    )

    # ---- 过滤没测出来时不猜 ----
    text, _hint = app.nat_grade_text(punch.NatSub.CONE, stun.FILTER_UNKNOWN)
    check(
        "过滤未测时不硬塞一个 NAT1-3（写成「待定」并给出原因）",
        "待定" in text,
        text,
    )


def main():
    print("=" * 64)
    print("MC-Link NAT 诊断测试（只读界面 · 移植版栈）")
    print("=" * 64)

    test_profile_contract()
    test_card_renderers()
    test_engine_loop_survives_diagnostics()
    test_room_monitor_contract()
    test_room_punch_starts_with_the_room()
    test_punched_socket_is_the_one_used()
    test_path_row()
    test_nat123_grading()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
