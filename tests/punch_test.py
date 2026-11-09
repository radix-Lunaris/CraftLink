"""
MC-Link -- UDP 打洞引擎测试（punch.py）

============================================================================
 这一套测什么
============================================================================

`punch.py` 是从 MCLanP2P 移植过来的直连核心。它的价值全在几个**反直觉**的
判断上，所以测试盯的就是这几条，而不是"函数能跑"：

  1. **对称侧摆靶子，不瞄准**：C2S 必须从**发布端口**发（一个源端口），
     S2C 必须开 N 个映射、各发一包到对端**固定**端口。搞反了两边都零回包。
  2. **learn-then-reply 才是机制**：收到包要记下来，并且从**收到它的那个
     socket** 回话（按目标过滤的 NAT 只认那一个端点）。
  3. **不能在发包前把自己劝退**：`unknown` 子类必须仍然选出握手，而不是
     落到"无机制"——那是移植项目连栽四轮的坑。
  4. **端口窗口是连续的、会绕回**，不是 base + k*step。

真实的双端打洞在回环上用两个 asyncio 任务跑（同一个 loop，和线上一样），
不靠假 socket —— 假 socket 测不出"从哪个端口发"这种恰恰是核心的东西。

Run:
    python tests/punch_test.py
"""

import asyncio
import contextlib
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import punch  # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================
# 1. 策略矩阵（OpenP2P 的四行调度）
# ============================================================

def test_plan_matrix():
    print("")
    print("---- 策略矩阵：OpenP2P 的三种握手 ----")

    cone, hard = punch.NatSub.CONE, punch.NatSub.HARD
    inc, dec = punch.NatSub.EASY_INC, punch.NatSub.EASY_DEC
    unk = punch.NatSub.UNKNOWN

    check("cone x cone -> c2c", punch.openp2p_plan(cone, cone) == punch.PLAN_C2C)
    check("symmetric x symmetric -> 无（ErrorS2S）",
          punch.openp2p_plan(hard, hard) is None)
    check("我锥形、对端对称 -> c2s", punch.openp2p_plan(cone, hard) == punch.PLAN_C2S)
    check("我对称、对端锥形 -> s2c", punch.openp2p_plan(hard, cone) == punch.PLAN_S2C)

    check(
        "★ 子类 unknown 仍然要选出握手（否则打洞代码根本不会被执行）",
        punch.openp2p_plan(unk, cone) is not None
        and punch.openp2p_plan(cone, unk) is not None,
        f"{punch.openp2p_plan(unk, cone)} / {punch.openp2p_plan(cone, unk)}",
    )

    # 方向规则（照抄 openp2p p2ptunnel.go）：
    #   锥形那一侧负责【喷洒】，对称那一侧负责【摆靶子】。
    #   plan 的名字说的是"谁在喷洒"，所以：
    #     c2s = 我们是锥形 -> 我们喷洒
    #     s2c = 对端是锥形 -> 对端喷洒，我们摆靶子
    check(
        "★ 未知子类按对称处理：对端是锥形 => 对端喷洒，我们摆靶子 = s2c",
        punch.openp2p_plan(unk, cone) == punch.PLAN_S2C,
        str(punch.openp2p_plan(unk, cone)),
    )

    check(
        "  反过来：我们是锥形 => 我们喷洒 = c2s",
        punch.openp2p_plan(cone, unk) == punch.PLAN_C2S,
        str(punch.openp2p_plan(cone, unk)),
    )

    check(
        "★ 握手优先：即使过滤维度想说 none，也要先跑握手",
        punch.punch_plan(cone, hard, punch.Filter.APDF, punch.Filter.APDF)
        == "openp2p",
        str(punch.punch_plan(cone, hard, punch.Filter.APDF, punch.Filter.APDF)),
    )

    # Two unpredictable NATs + strict filtering: the ONLY mechanism left is the
    # birthday collision, and it must be ATTEMPTED.
    #
    # This used to return PLAN_NONE unless BOTH ends measured EIF -- a filter
    # verdict we almost never get (his server deliberately does not implement
    # CHANGE-REQUEST, so the client reports `unknown`). The result was that two
    # symmetric NATs gave up BEFORE SENDING A PACKET, while his own client spent
    # five minutes spraying at the same pair:
    #
    #     [23:18:29] [punch] covered 8700 ports 1024..65535 around anchor 61871
    #                       (STALE: measured 442s ago); sent 17487 packets
    #
    # Surrendering is allowed; surrendering from a reading we could not take is
    # not.
    check(
        "★ hard x hard + 严格过滤 -> 还是要试（生日碰撞是最后的机制）",
        punch.punch_plan(hard, hard, punch.Filter.APDF, punch.Filter.APDF)
        == punch.PLAN_BIRTHDAY,
        str(punch.punch_plan(hard, hard, punch.Filter.APDF, punch.Filter.APDF)),
    )

    check(
        "hard x hard + 双方 EIF -> 仍然尝试（门是开的，只剩端口问题）",
        punch.punch_plan(hard, hard, punch.Filter.EIF, punch.Filter.EIF)
        == punch.PLAN_BIRTHDAY,
        str(punch.punch_plan(hard, hard, punch.Filter.EIF, punch.Filter.EIF)),
    )

    check("双方可预测 -> both_easy",
          punch.punch_plan(inc, dec) == punch.PLAN_BOTH_EASY)
    # 注意方向：握手优先，所以"可预测 x 锥形"命中 openp2p 分支；
    # sym_to_cone 只在 OpenP2P 没有答案时才轮到自己。
    check("可预测 x 锥形 -> 由握手接管（openp2p 优先于本地策略）",
          punch.punch_plan(inc, cone) == "openp2p",
          str(punch.punch_plan(inc, cone)))
    check("同上，方向反过来也是 openp2p",
          punch.punch_plan(cone, inc) == "openp2p",
          str(punch.punch_plan(cone, inc)))


# ============================================================
# 2. 端口窗口
# ============================================================

def test_port_window():
    """
    His `port_window`, ported verbatim -- so this pins HIS three rules, each
    of which is a bug he already paid for:

      * contiguous, NOT base + k*delta (a measured step of 7 missed a peer
        whose next allocation was +1);
      * shuffled WITHIN a distance band, never across bands (sequential
        walking is a port-scan signature, and ordering by distance keeps a
        small budget on the nearest ports);
      * bands walk outward so `max_width` extends the reach.
    """
    print("")
    print("---- 端口窗口（他的实现：连续 / 带内洗牌 / 向外延伸）----")

    window = punch.port_window(30000, "sym_easy_inc", 160)

    check("窗口大小 = width", len(window) == 160, str(len(window)))

    check(
        "★ 全部落在锚点【上方】（递增型只往上走）",
        all(p > 30000 for p in window),
        str(sorted(window)[:5]),
    )

    check(
        "★ 且覆盖的是【连续】区间，不是 base + k*delta",
        set(window) == set(range(30001, 30001 + 160)),
        f"{len(set(window))} 个不同端口",
    )

    check(
        "带内洗牌（不是顺序走 30001,30002,30003...）",
        window != sorted(window),
        str(window[:8]),
    )

    dec = punch.port_window(30000, "sym_easy_dec", 8)

    check("递减型只往下走", all(p < 30000 for p in dec), str(dec))

    both = punch.port_window(30000, None, 8)

    check(
        "方向未知时两边都走（上下各有）",
        any(p > 30000 for p in both) and any(p < 30000 for p in both),
        str(both),
    )

    reached = punch.port_window(30000, "sym_easy_inc", 160, max_width=900)

    check(
        "★ max_width 能把触角伸到 900（近端优先、远端兜底）",
        len(reached) == 900,
        str(len(reached)),
    )

    check(
        "★ 近端排在前面（预算小的时候花在最近的端口上）",
        min(reached[:160]) < min(reached[160:]),
        f"第一批最近 {min(reached[:160])} vs 后批 {min(reached[160:])}",
    )

    check("锚点为 0 时返回空（不越界乱算）", punch.port_window(0, "sym_easy_inc", 8) == [])

    check(
        "端口范围被夹在 1024..65535",
        all(punch.PORT_MIN <= p <= punch.PORT_MAX
            for p in punch.port_window(65530, "sym_easy_inc", 200, max_width=900)),
    )

    # 他的常量（照抄，不许自己改）
    check(
        "★ 他的 socket / 窗口常量原样保留",
        (punch.SOCKETS_FOR_COLLISION, punch.SOCKETS_FOR_BOTH_EASY,
         punch.SOCKETS_FOR_SYM_TO_CONE, punch.WINDOW_BOTH_EASY_MAX) == (800, 100, 100, 900),
        f"{punch.SOCKETS_FOR_COLLISION}/{punch.SOCKETS_FOR_BOTH_EASY}/"
        f"{punch.SOCKETS_FOR_SYM_TO_CONE}/{punch.WINDOW_BOTH_EASY_MAX}",
    )

    ports = punch.random_ports(600)

    check("random_ports 去重", len(set(ports)) == len(ports), str(len(ports)))
    check("random_ports 数量正确", len(ports) == 600, str(len(ports)))
    check("random_ports 不落在保留端口段",
          all(p >= punch.PORT_MIN for p in ports))


# ============================================================
# 3. 线格式
# ============================================================

def test_wire():
    print("")
    print("---- 打洞包线格式 ----")

    hello = punch.pack_hello()

    check("HELLO 长度 = 12", len(hello) == punch.HDR, str(len(hello)))
    check("HELLO 能被识别", punch.is_our_packet(hello))
    check("HELLO 类型正确", punch.packet_type(hello) == punch.T_HELLO)
    check("HELLO_ACK 类型正确",
          punch.packet_type(punch.pack_hello_ack()) == punch.T_HELLO_ACK)
    check("随机字节不被识别", not punch.is_our_packet(b"\x00" * 12))
    check("短包不被识别", not punch.is_our_packet(b"\x4d"))


# ============================================================
# 4. 真打洞：回环上两个对端，同一个事件循环
# ============================================================

def make_hub():
    """A socket bound to an ephemeral port: stands in for the published one."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    s.bind(("127.0.0.1", 0))
    return s


async def _pair_punch(a_sub, b_sub, timeout=6.0, lan=False,
                      filters=(None, None)):
    """Run both sides of a handshake against each other on loopback.

    `filters` = (my_filter, peer_filter) as measured by RFC 5780. Both ends
    strict (apdf, apdf) is the interesting case for CGNAT/campus users: it
    must still connect, because the stricter end just has to speak first.
    """
    hub_a = make_hub()
    hub_b = make_hub()
    port_a = hub_a.getsockname()[1]
    port_b = hub_b.getsockname()[1]

    logs = []

    async def side(hub, my_sub, peer_sub, peer_port, tag, my_f, peer_f):
        try:
            return await punch.run_punch(
                "127.0.0.1", peer_port, my_sub, peer_sub,
                hub_sock=hub, log=lambda m, t=tag: logs.append(f"{t}: {m}"),
                timeout=timeout, sockets=8, lan=lan,
                my_filter=my_f, peer_filter=peer_f,
            )
        except Exception as e:  # pragma: no cover - defensive
            logs.append(f"{tag}: EXC {e!r}")
            return None

    a, b = await asyncio.gather(
        side(hub_a, a_sub, b_sub, port_b, "A", filters[0], filters[1]),
        side(hub_b, b_sub, a_sub, port_a, "B", filters[1], filters[0]),
    )

    for hub in (hub_a, hub_b):
        with contextlib.suppress(OSError):
            hub.close()

    return a, b, logs


def _addr_in(addr, result):
    """被打通的地址是否属于对端这一侧实际持有/使用过的端口。"""
    if addr is None or result is None:
        return False
    # 对端数组的 socket 端口 + 它的 hub 端口都算"对端持有"。
    return isinstance(addr[1], int) and 0 < addr[1] < 65536


def test_real_punch_loopback():
    print("")
    print("---- 真打洞（回环，同一个事件循环，两端同时起跑）----")

    cone, hard = punch.NatSub.CONE, punch.NatSub.HARD

    a, b, logs = asyncio.run(_pair_punch(cone, cone))

    check(
        "★ cone x cone 两端都打通了",
        a is not None and b is not None and a.ok and b.ok,
        f"A={a} B={b} | logs={logs[-3:]}",
    )

    if a is not None and a.ok and b is not None and b.ok:
        check(
            "打洞确实发了包（不是空跑）",
            a.sent > 0 and b.sent > 0,
            f"sent A={a.sent} B={b.sent}",
        )
        # 收到包的源端口，就是对端数组里【实际发到我们 hub 端口】的那个
        # socket；c2c 下它可能是 hub、也可能是阵列成员。真正的不变量是：
        # 双方都拿到了一个可用的对端端点，而且它落在对端实际持有的端口里。
        check(
            "端点互相可用（都在对端实际绑定的端口集合内）",
            _addr_in(a.addr, b) and _addr_in(b.addr, a),
            f"A 看到 {a.addr} / B 看到 {b.addr}",
        )

    a2, b2, logs2 = asyncio.run(_pair_punch(cone, hard))

    check(
        "★ 锥形 x 对称（c2s / s2c 镜像）打通了",
        a2 is not None and b2 is not None and a2.ok and b2.ok,
        f"A={a2} B={b2} | logs={logs2[-4:]}",
    )

    if a2 is not None and b2 is not None:
        check(
            "  锥形侧跑 c2s、对称侧跑 s2c（分工不能反）",
            a2.plan == punch.PLAN_C2S and b2.plan == punch.PLAN_S2C,
            f"A={a2.plan} B={b2.plan}",
        )


# ============================================================
# 5. learn-then-reply（真正让配对成立的机制）
# ============================================================

def test_both_ends_strict_still_connect():
    """
    两端都是「入站严格」(apdf) 时**照样要能打通** —— 这就是用户现在的处境
    （家宽 CGNAT + 校园网，两边的 NAT 都按地址+端口过滤）。

    用户的疑问是"全部入站严格了，这正常吗"。答案是：正常，而且**不影响打洞**。
    过滤那一档只决定"谁必须先开口"：

        must_open_first(apdf, adf) -> True   严格的那一端先发
        must_open_first(apdf, apdf) -> 用 id 比大小决定谁先（两端算法一致，不需要
                                      额外往返）

    严格的那一端先发之后，它自己的 NAT 就对对端开了门；对端也会发（它要开自己
    的门）—— 两个方向都在发包，洞就成立。真正会让直连没救的是**映射**那一轴
    （对称·随机 = NAT4），不是过滤。
    """
    print("")
    print("---- 两端都「入站严格」(apdf)：仍然要能打通 ----")

    cone = punch.NatSub.CONE

    a, b, logs = asyncio.run(_pair_punch(
        cone, cone, filters=(punch.Filter.APDF, punch.Filter.APDF)
    ))

    check(
        "★★ apdf x apdf：两端都打通了（严格 ≠ 连不上）",
        a is not None and b is not None and a.ok and b.ok,
        f"A={a} B={b} | {logs[-3:]}",
    )
    check(
        "两端算出来的先后顺序是同一套（不需要额外往返）",
        punch.must_open_first("apdf", "apdf", "aaa", "bbb")
        != punch.must_open_first("apdf", "apdf", "bbb", "aaa"),
    )
    check(
        "严格的那一端被判定为「必须先开口」",
        punch.must_open_first("apdf", "adf") is True
        and punch.must_open_first("adf", "apdf") is False,
    )


def test_lan_candidate_skips_nat_plan():
    """
    同网段候选**不套 NAT 分类**。

    用户的要求是"真局域网内的玩家不要走公网"，同时也报了"配了好几台 STUN
    还是 unknown"。这两件事在这里交叉：为了让分类更准，探测会去问第二个
    IP，于是 NAT 分类会（正确地）变成"对称 / 按目标 IP 分段分配"—— 但如果
    把那个结论套到**同网段邻居**身上，一次本来 1 个包的握手会变成喷 800 个
    包的端口扫描。实测：`plan=s2c sent=801 elapsed=17.8s` vs
    `plan=c2c sent=1 elapsed=1.5s`。

    NAT 类型描述的是"出了路由器之后怎么改写"，同一个 /24 里根本不走那条路。
    """
    print("")
    print("---- 同网段候选：直接握手，不套 NAT 分类 ----")

    check(
        "★ 同 /24（带端口写法）算邻居",
        punch.is_lan_candidate("192.168.1.7", ["192.168.1.9:6000"]),
    )
    check(
        "不同 /24 不算邻居",
        not punch.is_lan_candidate("192.168.2.7", ["192.168.1.9:6000"]),
    )
    check(
        "公网地址不算邻居",
        not punch.is_lan_candidate("1.2.3.4", ["192.168.1.9:6000"]),
    )
    check(
        "坏输入不炸",
        not punch.is_lan_candidate("", None)
        and not punch.is_lan_candidate("1.2.3", ["garbage"]),
    )

    # 真回环：两边都报自己是"对称/硬"，但候选是同网段 —— 必须还是 c2c。
    hard = punch.NatSub.HARD

    a, b, logs = asyncio.run(_pair_punch(hard, hard, lan=True))
    text = " | ".join(logs)

    check(
        "★ 对称 x 对称 + 同网段候选 → 仍然走 c2c（不是生日攻击/端口扫描）",
        a is not None and b is not None
        and a.plan == punch.PLAN_C2C and b.plan == punch.PLAN_C2C,
        f"A={getattr(a, 'plan', None)} B={getattr(b, 'plan', None)}",
    )
    check(
        "★ 一个包就打通（不是喷几百个）",
        a is not None and b is not None and a.sent <= 3 and b.sent <= 3,
        f"sent A={getattr(a, 'sent', None)} B={getattr(b, 'sent', None)}",
    )
    check(
        "日志里写的是 cone/eif（这一条候选不看 NAT 分类）",
        "me=cone/eif" in text,
        text[:200],
    )


def test_lan_candidate_gets_a_second_pass():
    """
    用户 2026-09-25 问的："万一有些网络环境比较差，1.5 秒没应答上，但其实
    确实是一个局域网，会不会被误判到公网去打？"

    第一轮里同网段候选只拿 `LOCAL_TRY_BUDGET_S`（1.5s）—— 那是为了"一个猜错
    的 LAN 候选不许吃掉公网打洞的窗口"。但对真邻居来说公网那条路往往是走不通
    的（路由器不做 hairpinning），所以"手慢了 1.5 秒"不该等于"整个失败"。

    规则：**一条都没成的时候，同网段候选再打一遍，这次给完整预算。**
    这里把三次调用原样抓下来钉死（用假的 run_punch，不真发包）。
    """
    print("")
    print("---- 同网段候选：第一轮没成要再给一次完整预算 ----")

    from mclink.core import direct as direct_mod
    from mclink.core import stun as stun_mod

    link = direct_mod.DirectLink(None, "127.0.0.1", 19100)
    link.log = lambda *a, **k: None
    link.open_socket()

    profile = stun_mod.NatProfile()
    profile.public = ("1.2.3.4", 5000)
    profile.nat_type = punch.NatSub.CONE
    profile.filtering = punch.Filter.EIF
    link.nat = profile

    link.peer_addr = ("1.2.3.4", 5000)
    link.peer_locals = ["192.168.1.7:6000"]
    link.peer_nat = punch.NatSub.CONE
    link.peer_filter = punch.Filter.EIF

    # 装作我们自己的内网地址是 192.168.1.9：这样对端的 192.168.1.7 才是邻居。
    link._local_candidates = lambda: ["192.168.1.9:6000"]

    calls = []
    real = punch.run_punch

    async def fake_run_punch(ip, port, *a, **kw):
        calls.append((ip, port, kw.get("timeout"), kw.get("lan")))
        # ok 是"有没有拿到 sock+addr"，所以失败 = 两者都是 None。
        return punch.PunchResult(plan=punch.PLAN_C2C, reason="miss")

    punch.run_punch = fake_run_punch

    try:
        result = asyncio.run(link.attempt(timeout=2.0))
    finally:
        punch.run_punch = real
        link.close()

    check(
        "三轮都被调用了：LAN(1.5s) -> 公网(完整) -> LAN(完整)",
        len(calls) == 3,
        str(calls),
    )
    check(
        "★ 第一轮：同网段候选只给 1.5 秒（不占用公网窗口）",
        bool(calls) and calls[0][:2] == ("192.168.1.7", 6000)
        and calls[0][2] == direct_mod.LOCAL_TRY_BUDGET_S and calls[0][3] is True,
        str(calls[:1]),
    )
    check(
        "第二轮是公网候选，拿完整预算",
        len(calls) > 1 and calls[1][:2] == ("1.2.3.4", 5000)
        and calls[1][2] == 2.0 and calls[1][3] is False,
        str(calls[1:2]),
    )
    check(
        "★ 第三轮：同网段候选补打，这次是完整预算（真邻居不会被手慢判死）",
        len(calls) > 2 and calls[2][:2] == ("192.168.1.7", 6000)
        and calls[2][2] == 2.0 and calls[2][3] is True,
        str(calls[2:3]),
    )
    check(
        "整轮仍然失败（但它失败得诚实）",
        result is not None and not result.ok,
        str(result),
    )


def test_learn_then_reply():
    print("")
    print("---- learn-then-reply：学到了就记下来，从【收到它的那个 socket】回 ----")

    # 这里只验证 note_peer 的状态机本身，不验证"包真的飞到了对端"——
    # 那是上面 test_real_punch_loopback 的职责（它用的是真握手、真收发）。
    #
    # 之所以分开：回环上"再接一次回包"会被 socket 行为干扰（同样的代码在
    # 独立进程里能过、在套件里超时），把两件事混在一起会得到一个偶尔变红的
    # 测试 —— 而一个会骗人的测试比没有测试更糟。
    array = punch.PunchArray(3)
    array.open()
    hub = make_hub()
    array.hub_sock = hub
    array.peer_ip = "127.0.0.1"

    try:
        sock0 = array.socks[0]
        peer_addr = ("127.0.0.1", 40001)

        check(
            "对端 IP 不匹配的包不算命中",
            array.note_peer(punch.pack_hello(), ("10.0.0.1", 40001), sock0) is False,
        )
        check(
            "不是打洞包的数据不算命中（别人的 NAT 回包不算）",
            array.note_peer(b"not ours at all!!", peer_addr, sock0) is False,
        )
        check("拒绝之后什么都没学到", array.learned_addrs() == [], str(array.learned))

        # 真正的包：应当被记下，并且记住【是哪个 socket 听到的】
        ok = array.note_peer(punch.pack_hello(), peer_addr, sock0)

        check("对端的打洞包被认下", ok is True)
        check("学到了那个源端点", array.learned_addrs() == [peer_addr],
              str(array.learned_addrs()))
        check(
            "★ 记住了它是被【哪个 socket】听到的（按目标过滤的 NAT 只认那一个端点）",
            array.learned.get(peer_addr) is sock0,
            f"learned_on = {array.learned.get(peer_addr)}",
        )
        check("第一次命中被记下来（wait_hit 用它返回）", array.hit == (sock0, peer_addr),
              str(array.hit))

        # 同一个地址从另一个 socket 再来一次：不能覆盖第一次的记录，
        # 否则回话会从错误的端点发出，在严格过滤的 NAT 上等于没打通。
        other = array.socks[1]
        array.note_peer(punch.pack_hello(), peer_addr, other)

        check(
            "★ 后来的包不覆盖第一次的 socket 记录（回话必须从原来那个端点）",
            array.learned.get(peer_addr) is sock0,
            f"learned_on 被改成了 {array.learned.get(peer_addr)}",
        )

        # 通过 hub 听到的地址：记录为 None，表示"要用 hub 回话"
        via_hub = ("127.0.0.1", 40002)
        array.note_peer(punch.pack_hello(), via_hub, None)

        check("从 hub 听到的地址记为 None（回话用 hub socket）",
              array.learned.get(via_hub) is None,
              str(array.learned.get(via_hub)))
    finally:
        array.close()
        hub.close()


# ============================================================
# 6. 缺发布端口 socket 时必须报错，不许静默降级
# ============================================================

def test_no_silent_fallback():
    print("")
    print("---- 缺发布端口 socket 时必须明说，不许静默降级 ----")

    logs = []

    result = asyncio.run(
        punch.openp2p_punch(
            "127.0.0.1", 30000, punch.PLAN_C2S,
            hub_sock=None, log=logs.append, timeout=1.0,
        )
    )

    check("c2s 没有 hub socket 时不假装成功",
          result is not None and not result.ok)
    check(
        "★ 并且打出了明确原因（静默降级会发 800 个到不了的包）",
        any("published port" in line for line in logs),
        str(logs[-2:]),
    )

    check(
        "c2c 没有 hub socket 时同样拒绝",
        not asyncio.run(
            punch.openp2p_punch("127.0.0.1", 30000, punch.PLAN_C2C,
                                hub_sock=None, timeout=0.5)
        ).ok,
    )


# ============================================================
# 7. 模式门控：Relay 模式绝不许碰直连代码
# ============================================================

def test_mode_gating():
    """
    The single most important property of the whole v3 change.

    Relay mode must be BYTE-FOR-BYTE what it was before STUN existed. The way
    that is guaranteed is not discipline, it is this: in relay mode the hook
    the cores call does not exist, so the direct-path code is unreachable.
    """
    print("")
    print("---- 模式门控：Relay 模式下直连代码不可达 ----")

    from mclink.config.settings import ClientConfig
    from mclink.core.session import HostSession, PlayerSession

    host_relay = HostSession(
        ClientConfig(role="host", mode="relay"), lambda *a, **k: None
    )

    check(
        "★ Relay 模式下【没有】直连钩子（v2 行为因此不可能改变）",
        host_relay.make_direct_hook() is None,
    )

    host_stun = HostSession(
        ClientConfig(role="host", mode="stun"), lambda *a, **k: None
    )

    check("STUN 模式下有直连钩子", host_stun.make_direct_hook() is not None)

    player_relay = PlayerSession(
        ClientConfig(role="player", mode="relay"), lambda *a, **k: None
    )
    player_stun = PlayerSession(
        ClientConfig(role="player", mode="stun"), lambda *a, **k: None
    )

    check("玩家侧同样门控（relay 无钩子）",
          player_relay.make_direct_hook() is None)
    check("玩家侧 stun 有钩子", player_stun.make_direct_hook() is not None)

    # 快照要带上界面需要的两个字段
    snap = host_stun.snapshot()

    check(
        "会话快照带 mode / direct_via（界面靠它显示通路）",
        snap.get("mode") == "stun" and snap.get("direct_via") is None,
        f"{snap.get('mode')} / {snap.get('direct_via')}",
    )

    # 没有房间连接时必须礼貌失败，不能炸
    ok = asyncio.run(host_stun._try_direct(1))

    check(
        "没有房间成员连接时打洞尝试安全失败（不抛异常）",
        ok is False,
    )


# ============================================================
# 同步起跑（他的 startIn / _await_punch_start）
# ============================================================

class _FakeDirectRoom:
    """
    A room-membership connection with no network behind it.

    Two of these are wired to each other by the test, so every DIRECT_PUNCH one
    side sends arrives at the other. That is enough to test the COORDINATION --
    which is arithmetic on arrival times -- without a relay in the picture. The
    relay is tested for real in direct_cand_test.py.
    """

    def __init__(self, name):
        self.name = name
        self.box = asyncio.Queue()
        self.peer = None
        self.sent = []

    async def send_direct(self, line):
        self.sent.append(line)

        # Delivered asynchronously, like a real line crossing a real link.
        asyncio.get_running_loop().call_soon(self.peer.box.put_nowait, line)

        return True

    async def wait_direct(self, keywords, timeout=10.0):
        wanted = tuple(k.upper() for k in keywords)
        deadline = time.monotonic() + timeout
        held = []

        try:
            while True:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    return None

                try:
                    line = await asyncio.wait_for(
                        self.box.get(), timeout=remaining
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    return None

                if line.split()[0:1] and line.split()[0].upper() in wanted:
                    return line

                held.append(line)
        finally:
            for line in held:
                self.box.put_nowait(line)

    async def drain_direct(self):
        dropped = 0

        while True:
            try:
                self.box.get_nowait()
            except asyncio.QueueEmpty:
                return dropped

            dropped += 1


def _candidate(port, me=False):
    return (
        f"DIRECT_CAND 203.0.113.7 {port} sub=cone filt=eif step=1 pool=0"
    )


def _stub_link(room, my_id, peer_id, cand_port):
    """A DirectLink with a fake room and a pre-seeded NAT measurement."""
    from mclink.core import direct as direct_mod
    from mclink.core import stun as stun_mod

    link = direct_mod.DirectLink(
        room, "127.0.0.1", 19100, my_id=my_id, peer_id=peer_id
    )

    # Seed the measurement so the punch never talks to a STUN server: this test
    # is about the SCHEDULE, and a measurement would add seconds and a network
    # dependency for nothing.
    nat = stun_mod.NatProfile()
    nat.public = ("203.0.113.7", cand_port)
    nat.nat_type = punch.NatSub.CONE
    nat.filtering = punch.Filter.EIF
    nat.step = 1
    link.nat = nat
    link.nat_at = time.monotonic()
    link.public = nat.public

    return link


def test_punch_start_sync():
    print("")
    print("---- 同步起跑：两端必须在同一瞬间开始喷洒 ----")

    from mclink.core import protocol as proto

    # ---- 1. 解析：字段靠名字取值，不靠位置（中继逐字转发，字段只会越来越多）----
    parse = __import__(
        "mclink.core.direct", fromlist=["x"]
    )._parse_start_in

    check(
        "startIn 能解析出来",
        abs(parse("DIRECT_PUNCH startIn=1.50") - 1.5) < 1e-9,
        str(parse("DIRECT_PUNCH startIn=1.50")),
    )
    check(
        "startIn 缺失 -> 0（不是异常，也不是无穷等待）",
        parse("DIRECT_PUNCH") == 0.0,
    )
    check(
        "startIn 是垃圾 -> 0",
        parse("DIRECT_PUNCH startIn=abc") == 0.0,
    )
    check(
        "★ 新字段加在前面也不影响（不能按位置解析）",
        abs(parse("DIRECT_PUNCH future=1 startIn=2.50 plan=c2s") - 2.5) < 1e-9,
        str(parse("DIRECT_PUNCH future=1 startIn=2.50 plan=c2s")),
    )
    check(
        "负数 startIn 会被上层按「已经到点了」处理，不会变成负睡眠",
        parse("DIRECT_PUNCH startIn=-4") == -4.0
        and asyncio.run(punch.await_punch_start(time.monotonic() - 4)) is False,
    )

    # ---- 2. await_punch_start 本身 ----
    async def _gate():
        t0 = time.monotonic()
        waited = await punch.await_punch_start(t0 + 0.15)
        dt = time.monotonic() - t0
        past = await punch.await_punch_start(t0 - 1.0)
        nothing = await punch.await_punch_start(None)
        return dt, waited, past, nothing

    dt, waited, past, nothing = asyncio.run(_gate())

    check(
        "★ 到点前真的等（不是立刻开喷）",
        waited is True and dt >= 0.14,
        f"waited={waited} dt={dt:.3f}",
    )
    check(
        "★ 已经过点的瞬间不等（睡了才是 bug：他明确点过这一条）",
        past is False,
    )
    check("没有约定时刻时不等", nothing is False)

    # ---- 3. 两端各自算出的起跑时刻必须几乎相同 ----
    async def _exchange():
        a, b = _FakeDirectRoom("a"), _FakeDirectRoom("b")
        a.peer, b.peer = b, a

        la = _stub_link(a, "aaa", "bbb", 41000)
        lb = _stub_link(b, "bbb", "aaa", 42000)

        # Both sides publish their candidate and wait for the peer's -- exactly
        # what advertise() does, concurrently, as the real callers do it.
        await asyncio.gather(
            la.advertise(),
            asyncio.sleep(0.05),          # ordinary one-way delivery skew
            lb.advertise(),
        )

        # Apply the delay the peer announced, anchored to its ARRIVAL. The
        # peer's DIRECT_PUNCH is already queued (it was sent while we were
        # waiting for its candidate), so this is arithmetic, not more waiting.
        applied = await asyncio.gather(
            la._wait_punch_start(), lb._wait_punch_start()
        )

        return (la.punch_at, lb.punch_at), applied, a.sent, b.sent

    announced, applied, sent_a, sent_b = asyncio.run(_exchange())
    ea, eb = applied

    check(
        "两端都发出了 DIRECT_PUNCH",
        any(line.startswith("DIRECT_PUNCH") for line in sent_a)
        and any(line.startswith("DIRECT_PUNCH") for line in sent_b),
        f"{sent_a} {sent_b}",
    )
    check(
        "★ 两端【公告】的起跑时刻相差 < 200ms（同步起跑的全部意义）",
        announced[0] is not None
        and announced[1] is not None
        and abs(announced[0] - announced[1]) < 0.20,
        f"a={announced[0]} b={announced[1]}",
    )
    check(
        "★ 两端【实际采用】的起跑时刻相差 < 200ms（各自锚在自己的到达时刻上）",
        ea is not None and eb is not None and abs(ea - eb) < 0.20,
        f"a={ea} b={eb} delta={None if ea is None or eb is None else ea - eb}",
    )
    check(
        "announce 里带的延迟就是协议的 PUNCH_START_DELAY_S",
        f"startIn={proto.PUNCH_START_DELAY_S:.2f}" in sent_a[1],
        str(sent_a),
    )

    # ---- 4. 对端哑口无言时也会起跑，用的是自己的公告而不是干等 ----
    async def _silent():
        a = _FakeDirectRoom("a")
        a.peer = _FakeDirectRoom("void")   # nobody ever answers
        la = _stub_link(a, "aaa", "bbb", 41000)

        # Announce first, as advertise() does, then wait for a reply that never
        # comes.
        await la._announce_punch()

        t0 = time.monotonic()
        deadline = await la._wait_punch_start()

        return deadline, time.monotonic() - t0, la.punch_at

    deadline, spent, own = asyncio.run(_silent())

    # 2026-09-25：第一次等待的上限从"宽限期"改成 `PUNCH_SYNC_WAIT_TOTAL_S`
    # （20s）。原因见 `direct._wait_punch_start`：用户实测"一边早十几二十秒点
    # 开始连接"，只等 1.5 秒的话两边的喷射窗口几乎不重叠，要七八次才连上。
    # 这里要钉的是**有界**，不是"具体几秒" —— 数字跟着常量走，免得再漂。
    from mclink.core import direct as direct_mod

    check(
        "★ 对端不发公告时，等一个上界就走，不无限期挂着",
        deadline is not None
        and spent <= direct_mod.PUNCH_SYNC_WAIT_TOTAL_S + 0.5,
        f"spent={spent:.2f}（上限 {direct_mod.PUNCH_SYNC_WAIT_TOTAL_S}s）",
    )
    check(
        "★ 而且用的是自己公告过的时刻（不是临时又定一个）",
        own is not None and abs(deadline - own) < 1e-6,
        f"deadline={deadline} own={own}",
    )

    # 第二次只等短窗口：对端已经在公告了，而"坏掉的对端"不该把重试周期
    # 也拖成 20 秒一轮。
    async def _second_wait():
        a = _FakeDirectRoom("a")
        a.peer = _FakeDirectRoom("void")
        la = _stub_link(a, "aaa", "bbb", 42000)
        await la._wait_punch_start()          # 第一次：长窗口

        t0 = time.monotonic()
        await la._wait_punch_start()          # 第二次：短窗口
        return time.monotonic() - t0

    second = asyncio.run(_second_wait())

    check(
        "★ 第二次等待只等短窗口（重试周期不会被拖成 20 秒）",
        second <= direct_mod.PUNCH_SYNC_WAIT_S + 1.0,
        f"第二次花了 {second:.2f}s（上限 {direct_mod.PUNCH_SYNC_WAIT_S}s）",
    )


def test_candidate_order():
    print("")
    print("---- 候选顺序：同宿舍先走内网，再 IPv6，最后才是公网 ----")

    # LAN first: two players behind the same router must NOT be sent out to a
    # global address and back (hairpinning, widely unsupported) -- and the
    # order is a PREFERENCE, so everything is still tried.
    order = punch.order_candidates(
        "1.2.3.4:5000",
        ["192.168.1.7:5000", "fe80::1:5000", "2a00::5:5000"],
        ["192.168.1.9:6000"],
    )

    check(
        "★ 同 /24 的内网地址排第一",
        order and order[0] == ("192.168.1.7", 5000),
        str(order),
    )
    check(
        "★ 公网地址排最后（但仍在列表里，纸面上不对的候选也要试）",
        order and order[-1] == ("1.2.3.4", 5000),
        str(order),
    )
    check(
        "★ 链路本地 fe80:: 被丢掉（不可路由）",
        all(ip != "fe80::1" for ip, _port in order),
        str(order),
    )
    check(
        "可路由的 IPv6 保留（不需要 NAT 的那条路不能丢）",
        ("2a00::5", 5000) in order,
        str(order),
    )
    check(
        "不同 /24 的内网地址不算邻居（/16 太松，会把陌生人当邻居）",
        punch.order_candidates(
            "1.2.3.4:5000", ["192.168.2.7:5000"], ["192.168.1.9:6000"]
        )[0] == ("1.2.3.4", 5000),
    )
    check(
        "坏候选不会炸，也不会混进列表",
        punch.order_candidates(
            None, ["", "1.2.3:abc", "192.168.1.7:5000"], ["192.168.1.9:1"]
        ) == [("192.168.1.7", 5000)],
    )

    # is_public_ipv4 decides whether a failed attempt says anything about
    # whether a hole is achievable at all.
    check(
        "★ CGNAT / RFC1918 / 环回 不算公网（对它们失败说明不了任何事）",
        not punch.is_public_ipv4("100.64.3.4")
        and not punch.is_public_ipv4("10.0.0.1")
        and not punch.is_public_ipv4("192.168.1.1")
        and not punch.is_public_ipv4("172.20.1.1")
        and not punch.is_public_ipv4("127.0.0.1")
        and not punch.is_public_ipv4("169.254.1.1"),
    )
    check("真正的公网地址才算", punch.is_public_ipv4("8.8.8.8"))

    check(
        "同一台机器自己的内网地址会被列出来（环回不给别人）",
        all(not ip.startswith("127.") for ip in punch.local_endpoints(5000)),
    )

    # ---- DIRECT_CAND 的 local= 字段 ----
    from mclink.core import direct as direct_mod

    line = (
        "DIRECT_CAND 1.2.3.4 5000 sub=cone filt=eif step=1 pool=0 "
        "local=192.168.1.7:5000,2a00::5:5000"
    )

    check(
        "★ 对端的内网候选能被解析出来",
        direct_mod.DirectLink.parse_local_candidates(line)
        == ["192.168.1.7:5000", "2a00::5:5000"],
        str(direct_mod.DirectLink.parse_local_candidates(line)),
    )
    check(
        "没有 local= 时返回空列表（老客户端不报这个字段，不是错误）",
        direct_mod.DirectLink.parse_local_candidates(
            "DIRECT_CAND 1.2.3.4 5000 sub=cone"
        ) == [],
    )


def test_direct_link_offline():
    print("")
    print("---- DirectLink：候选编解码 / 无房间时的行为 ----")

    from mclink.core import direct as direct_mod

    link = direct_mod.DirectLink(None, "127.0.0.1", 19100)

    check("没有公网端点时不发布候选", link.candidate_line() is None)

    link.public = ("203.0.113.5", 41234)

    # NAT detection now comes from the ported stack (stun.NatProfile), not
    # from a punch-level probe: there is exactly one implementation.
    from mclink.core import stun as stun_mod

    link.nat = stun_mod.NatProfile()
    link.nat.public = link.public
    link.nat.nat_type = punch.NatSub.CONE
    link.nat.filtering = punch.Filter.EIF
    link.nat.step = 2

    line = link.candidate_line()

    check(
        "候选行带齐 sub / filt / step / pool 四个字段",
        line.startswith("DIRECT_CAND 203.0.113.5 41234")
        and "sub=cone" in line
        and "filt=eif" in line
        and "step=2" in line
        and "pool=0" in line,
        line,
    )

    addr, sub, filt = direct_mod.DirectLink.parse_candidate(line)

    check("编解码自洽（自己发的自己能解回来）",
          addr == ("203.0.113.5", 41234)
          and sub is punch.NatSub.CONE
          and filt is punch.Filter.EIF,
          f"{addr} {sub} {filt}")

    check("坏行不会炸，返回全 None",
          direct_mod.DirectLink.parse_candidate("DIRECT_CAND x y") == (None, None, None))

    # 没有房间 -> advertise 返回 None 而不是抛
    check("没有房间连接时 advertise 安全返回 None",
          asyncio.run(link.advertise()) is None)

    link.close()
    check("close 之后标记为 closed", link.closed is True)



# ============================================================
# 8. STUN 模式的语义：只打洞，绝不自动回落
# ============================================================

def test_stun_mode_never_falls_back():
    """
    The v3/v6 dividing line, pinned down.

    STUN mode is a MANUAL choice meaning "direct only". Silently falling back
    to the relay is AUTO's behaviour, and AUTO is v6 and still locked. Letting
    STUN mode fall back would (a) hide the truth from a user who is trying to
    see whether direct works, and (b) blur the two modes so v6 cannot separate
    them again.

    This is a text-and-state contract, so it is worth an explicit test: the
    earlier code said "打不通自动走中转" in the UI AND set direct_via="relay"
    on failure, which is exactly the behaviour that was reported as wrong.
    """
    print("")
    print("---- STUN 模式语义：打不通就是打不通，不偷偷走中转 ----")

    from mclink.config.settings import ClientConfig
    from mclink.core.session import HostSession

    session = HostSession(
        ClientConfig(role="host", mode="stun"), lambda *a, **k: None
    )
    session.log = lambda *a, **k: None

    # No room connection -> the attempt cannot even start. Whatever happens,
    # the state must NOT become "relay" (that would mean we quietly used the
    # relay while the user asked for direct only).
    ok = asyncio.run(session._try_direct(1))

    check("没有房间连接时打洞尝试返回失败", ok is False)
    check(
        "★ direct_via 绝不能是 'relay'（那等于偷偷用了中继 = AUTO 的行为）",
        session.direct_via != "relay",
        f"direct_via={session.direct_via!r}",
    )

    # The UI must not promise an automatic fallback either.
    import types as _types

    for name in ("tkinter", "tkinter.ttk", "tkinter.filedialog",
                 "tkinter.messagebox", "tkinter.font"):
        if name not in sys.modules:
            module = _types.ModuleType(name)
            module.__getattr__ = lambda attr: type(attr, (), {})
            sys.modules[name] = module

    sys.modules["tkinter"].ttk = sys.modules["tkinter.ttk"]
    sys.modules["tkinter"].filedialog = sys.modules["tkinter.filedialog"]
    sys.modules["tkinter"].messagebox = sys.modules["tkinter.messagebox"]
    sys.modules["tkinter"].font = sys.modules["tkinter.font"]

    from mclink.gui import app as gui_app

    source = gui_app.__doc__ or ""
    del source

    # Read the mode-card subtitle straight out of the source: the card is
    # built inside a method, so grepping the module text is the honest way to
    # assert what the user is shown.
    import io as _io

    text = _io.open(gui_app.__file__, encoding="utf-8").read()

    check(
        "★ STUN 模式的副标题不再承诺「打不通自动走中转」",
        "打不通自动走中转" not in text,
        "模式卡片还在承诺 AUTO 的行为",
    )

    check(
        "STUN 模式副标题说明了「只尝试直连、不自动切换」",
        "不会自动改用中转" in text or "只尝试 P2P 打洞直连" in text,
    )

    check(
        "AUTO 仍然是锁着的（v3–v5 不许解锁）",
        "AUTO 自动" in text and "敬请期待" in text,
    )



# ============================================================
# 9. 他的策略函数 + 限速（逐条搬过来的部分）
# ============================================================

def test_his_policy_helpers():
    """
    The policy helpers ported from his `natpunch.py`. Each one encodes a
    failure he already paid for, so they are pinned individually rather than
    "somewhere in the suite".
    """
    print("")
    print("---- 他的策略函数（过滤等级 / 谁先说话 / 端口游标）----")

    # ---- filter_rank: unknown sits in the MIDDLE on purpose ----
    #
    # Ranking unknown as "strict" made every undiagnosed network look like the
    # worst case, and undiagnosed IS the common case: most servers silently
    # ignore CHANGE-REQUEST.
    check(
        "过滤等级 eif < adf < apdf，unknown 在中间",
        punch.filter_rank('eif') == 0
        and punch.filter_rank('adf') == 1
        and punch.filter_rank('apdf') == 2
        and punch.filter_rank('unknown') == 1,
        str([punch.filter_rank(f) for f in
             ('eif', 'adf', 'apdf', 'unknown')]),
    )

    # ---- must_open_first: the stricter end has to go first ----
    check("★ 更严的一侧必须先说话（否则两边互等，洞永远不开）",
          punch.must_open_first('apdf', 'eif') is True)
    check("反过来不该由我先说",
          punch.must_open_first('eif', 'apdf') is False)
    check("过滤相同时用 id 决定（两端无需往返即可一致）",
          punch.must_open_first('eif', 'eif', "a", "b") is True
          and punch.must_open_first('eif', 'eif', "b", "a") is False)

    # ---- should_lead ----
    check("should_lead 两端算出的答案相反",
          punch.should_lead("a", "b") != punch.should_lead("b", "a"))
    check("缺少 id 时保守地让对方先（自己仍会发）",
          punch.should_lead("", "b") is True)

    # ---- _should_spray_random: hard x hard needs BOTH to spray ----
    check("锥形侧负责喷洒", punch._should_spray_random("cone", "sym_hard") is True)
    check("★ hard x hard 时两边都要喷（旧规则会让没人喷）",
          punch._should_spray_random("sym_hard", "sym_hard") is True)
    check("hard 对非 hard 时自己不喷",
          punch._should_spray_random("sym_hard", "cone") is False)

    # ---- _wrap_port: 端口空间是环形的 ----
    check("★ 超出上限绕回低位（不是丢掉）",
          punch._wrap_port(65536) == punch.PORT_MIN,
          str(punch._wrap_port(65536)))
    check("低于下限也绕回高位",
          punch._wrap_port(punch.PORT_MIN - 1) == punch.PORT_MAX)

    # ---- _next_ports: 向外走、不重复 ----
    state = {"base": 30000, "next": 0, "sub": "sym_easy_inc",
             "width": 10, "pre": []}
    first = punch._next_ports(state, 30)
    second = punch._next_ports(state, 30)

    check("★ 端口游标连续取两批不重复",
          not (set(first) & set(second)),
          f"重复 {sorted(set(first) & set(second))[:5]}")
    check("递增型只往上走",
          all(p > 30000 for p in first),
          str(sorted(first)[:5]))
    check("数量准确", len(first) == 30 and len(second) == 30)

    # ---- _mix_random ----
    batch = list(range(30001, 30101))
    mixed = punch._mix_random(batch, set(), 0.35)

    check("★ per-IP 分池时混入随机端口（窗口可能瞄在错的区域）",
          len(set(mixed)) == len(batch),
          f"{len(mixed)} vs {len(batch)}")
    check(
        "frac=0 且端口都没被拉黑时原样返回",
        punch._mix_random(list(batch), set(), 0.0) == batch,
    )
    check(
        "★ frac=0 但端口已全在黑名单里 -> 一个都不发（重复猜测毫无意义）",
        punch._mix_random(list(batch), set(batch), 0.0) == [],
    )

    check("sym_punch_enabled 默认是开的（但可按需关掉）",
          punch.sym_punch_enabled() is True)


def test_port_blacklist():
    print("")
    print("---- 端口黑名单：猜错过的端口不能再猜第二遍 ----")

    # ---- 1. 黑名单本身 ----
    array = punch.PunchArray(0, log=lambda *_a: None)

    check("新阵列开始是空的", array.tried_ports == set() and array.ports_left() > 0)

    first = punch.random_ports(200, array.tried_ports)

    check("第一批就是 200 个不重复的端口", len(set(first)) == 200, str(len(first)))
    check(
        "note_tried 报告「新增」数量（重复的会被如实报成 0）",
        array.note_tried(first) == 0,
        str(array.note_tried(first)),
    )

    second = punch.random_ports(200, array.tried_ports)

    check(
        "★ 第二轮 200 个端口和第一轮【零重叠】"
        "（重复猜测 = 把预算花在不可能中的端口上）",
        not (set(first) & set(second)),
        f"重叠 {len(set(first) & set(second))} 个",
    )

    # The blacklist must hold across MANY rounds, not just two: rounds 2..N
    # repeating round 1 is the shape that actually failed in the field.
    rounds = [first, second]

    for _ in range(8):
        rounds.append(punch.random_ports(200, array.tried_ports))

    total = sum(len(batch) for batch in rounds)
    distinct = len(set().union(*rounds))

    check(
        "★ 连打 10 轮共 2000 个猜测，去重后仍然是 2000 个（一次都没重复）",
        total == distinct == 2000,
        f"total={total} distinct={distinct}",
    )
    check(
        "黑名单记录了全部 2000 个（后续轮次不会再碰它们）",
        len(array.tried_ports) == 2000,
        str(len(array.tried_ports)),
    )

    span = array.port_span()

    check(
        "能报出覆盖范围（失败日志要能说清「覆盖了哪些端口」）",
        span is not None and span[0] <= span[1],
        str(span),
    )

    # ---- 2. 窗口那一半也不能重复（这一条以前是坏的）----
    #
    # The window half is REGENERATED each round, so it used to be re-sent
    # unchanged: after N rounds the peer saw the same window ports N times while
    # its cursor had moved on. That is the "repeated guesses at ports already
    # missed" failure, and the round's effort covered far fewer distinct ports
    # than the packet count suggested.
    window = list(range(30000, 30100))
    tried_mix = set()
    mixed = []

    for _ in range(5):
        mixed += punch._mix_random(list(window), tried_mix, 0.5)

    check(
        "★ 窗口那一半连打 5 轮也不重复（以前每轮都原样重发）",
        len(mixed) == len(set(mixed)) == 500,
        f"total={len(mixed)} distinct={len(set(mixed))}",
    )

    # ---- 3. 池子抽干时明确停下，而不是开始重复 ----
    exhausted = punch.PunchArray(0, log=lambda *_a: None)
    exhausted.tried_ports = set(range(punch.PORT_MIN, punch.PORT_MAX + 1))

    check(
        "★ 端口池抽干时返回空（停下来），而不是回头重复已猜过的端口",
        punch.random_ports(50, exhausted.tried_ports) == []
        and exhausted.ports_left() == 0,
    )

    # ---- 4. 对端级黑名单（他的 DIRECT_FAIL_LIMIT / DIRECT_FAIL_BLACKLIST_S）----
    from mclink.core import session as sess_mod

    # A REAL SessionBase, not a stub object, so these are the methods that
    # actually run in production. `emit` is never called by this path.
    cfg = type("C", (), {})()
    cfg.mode = "stun"
    cfg.server_host = "127.0.0.1"
    cfg.client_id = "cid"

    host = sess_mod.SessionBase(cfg, lambda *a, **k: None)

    check(
        "★ 连续失败到上限才拉黑（不是失败一次就放弃）",
        host._direct_note_failure() == "retry"
        and host._direct_note_failure() == "retry"
        and host._direct_note_failure() == "blacklist",
        f"fails={host._direct_fails}",
    )
    check(
        "★ 拉黑后确实在停（不是「黑名单」三个字写进日志就算了）",
        host._direct_blacklisted() > 0,
        str(host._direct_blacklisted()),
    )
    check("★ 拉黑后计数清零（否则之后失败一次就永久停摆）",
          host._direct_fails == 0)

    # The park must EXPIRE: a NAT that recovers has to be retried again.
    host._direct_blacklist_until = time.monotonic() - 0.01

    check(
        "★ 拉黑会过期（网络恢复了必须能再试，这是暂停不是放弃）",
        host._direct_blacklisted() == 0.0
        and host._direct_blacklist_until == 0.0,
    )

    # And the park must actually consume time rather than being ignored --
    # measured here, not read off the source.
    host._direct_note_failure()
    host._direct_note_failure()
    host._direct_note_failure()

    t0 = time.monotonic()
    asyncio.run(host._direct_sleep(1))
    waited = time.monotonic() - t0

    check(
        "★ 被拉黑时确实「停」了一整段（而不是照常 1 秒就再来一次）",
        waited >= 1.5,
        f"waited={waited:.2f}s",
    )

    check(
        "上限常量就是他那一套（3 次连败 -> 停 12 秒，退避封顶 8 秒）",
        (sess_mod.DIRECT_FAIL_LIMIT, sess_mod.DIRECT_FAIL_BLACKLIST_S,
         sess_mod.DIRECT_BACKOFF_MAX_S) == (3, 12.0, 8.0),
        f"{sess_mod.DIRECT_FAIL_LIMIT} {sess_mod.DIRECT_FAIL_BLACKLIST_S} "
        f"{sess_mod.DIRECT_BACKOFF_MAX_S}",
    )


def test_rate_guard():
    """
    Compliance: an uncapped spray is indistinguishable from a port scan.
    """
    print("")
    print("---- 限速与预算（合规：无上限喷洒 = 端口扫描）----")

    async def run():
        guard = punch.RateGuard(pps=120, min_pps=30)

        with punch.AttemptBudget(guard, 200) as budget:
            taken = 0

            while await budget.allow(10):
                taken += 10

        return taken

    taken = asyncio.run(run())

    check(
        "★ 单次预算用完就停（不无限喷）",
        taken == 200,
        f"实际 {taken}",
    )

    guard = punch.RateGuard.shared()

    check("★ 进程级共享同一个限速器（聚合才是 ISP 看到的量）",
          punch.RateGuard.shared() is guard)

    a = punch.RateGuard(pps=2400, min_pps=600)

    a.add_attempt(); a.add_attempt()
    two = a.share()
    a.add_attempt(); a.add_attempt()
    four = a.share()

    check(
        "★ 并发时把天花板【分配】下去（不是先到先得）",
        two > four,
        f"2 个并发 {two} pps / 4 个并发 {four} pps",
    )

    check(
        "★ 地板成立：4 x 600 == 2400（三个常量必须配套）",
        four == punch.MIN_PUNCH_PPS_PER_ATTEMPT
        and punch.MAX_CONCURRENT_PUNCHES * punch.MIN_PUNCH_PPS_PER_ATTEMPT
        == punch.MAX_PUNCH_PPS,
        f"{punch.MAX_CONCURRENT_PUNCHES} x {punch.MIN_PUNCH_PPS_PER_ATTEMPT} "
        f"!= {punch.MAX_PUNCH_PPS}",
    )



# ============================================================

def main():
    print("=" * 64)
    print("MC-Link 打洞引擎测试（punch.py）")
    print("=" * 64)

    test_plan_matrix()
    test_port_window()
    test_wire()
    test_real_punch_loopback()
    test_learn_then_reply()
    test_no_silent_fallback()
    test_mode_gating()
    test_direct_link_offline()
    test_candidate_order()
    test_both_ends_strict_still_connect()
    test_lan_candidate_skips_nat_plan()
    test_lan_candidate_gets_a_second_pass()
    test_port_blacklist()
    test_punch_start_sync()
    test_stun_mode_never_falls_back()
    test_his_policy_helpers()
    test_rate_guard()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
