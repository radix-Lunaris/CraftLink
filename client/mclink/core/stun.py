"""
CraftLink client -- STUN probing and NAT detection.

============================================================================
  WHERE THIS COMES FROM (read this before changing anything)
============================================================================

This is a **verbatim port of MCLanP2P's STUN/NAT stack** (`client/common.py`
plus the NAT classifier from `client/natpunch.py` and `client/protocol.py`).

WHY HIS AND NOT OURS: CraftLink grew its own half-finished STUN implementation
first, and the project owner's decision was to drop it entirely -- his is the
one verified working on real NAT3/NAT4 networks, and keeping a second
half-finished copy alongside it only creates drift. So:

  * the wire code, the probe layout, the NAT classifier, the subtype
    refinement and the labels below are HIS, unchanged;
  * the only CraftLink additions are the async wrappers at the bottom
    (`measure`, `detect`) and the docstring you are reading.

If something here looks wrong, the thing to do is compare it against his
source -- NOT to "fix" it. Several of these choices are the opposite of what
looks right, and each one cost his project a real debugging round. The three
that matter most:

  **1. The mapping follows the DESTINATION, so two destinations are needed.**
     A cone NAT reports the same mapped port to every server; a symmetric NAT
     reports a fresh one per destination. That comparison is the entire
     classification, which is why the server offers TWO UDP ports.

  **2. `p2 - p1` is NOT the allocation step.** Those are two different
     destinations, so the gap also contains however many unrelated flows the
     machine opened in between. Measured: p1=60225, p2=60392 (gap 167) while
     the true step was **+1**. The step comes from a THIRD probe -- a fresh
     socket against a server already reached -- and that is what
     `refine_nat_subtype` is for.

  **3. "Symmetric" is a question, not an answer.** Most real NAT4s allocate
     sequentially, so the next port is guessable; only a minority are truly
     random. Treating every symmetric NAT as hopeless is what made his NAT4
     direct connections fail for so long.

============================================================================
  THREADING: HIS CODE IS BLOCKING, OURS IS ASYNCIO
============================================================================

Every probe below is a SYNCHRONOUS ``sock.recvfrom`` with a timeout. CraftLink
runs the whole engine on ONE event loop, so calling any of it directly would
freeze every live connection for the length of the probe -- exactly the class
of bug in docs/V2-LEFTOVER-CONNECT-FREEZE.md.

So his code stays exactly as it is, and the async wrappers at the bottom run
it in an executor thread on a DEDICATED socket. Never call the blocking
functions from the event loop.
"""

import asyncio
import contextlib
import os
import random
import socket
import struct
import time

# a symmetric NAT gets a port-predicted spray, a cone NAT does not need one.

NAT_UNKNOWN = "unknown"
NAT_CONE = "cone"          # one mapping for every destination
NAT_SYMMETRIC = "symmetric"  # a new mapping per destination (a.k.a. NAT4)

# NAT4 is not one thing -- it is a spectrum of "how predictable is the next
# port". Treating every symmetric NAT as "hopeless" is what made NAT4 direct
# connections fail: the majority of real NAT4s allocate SEQUENTIALLY, so the
# next mapping is guessable, and only a minority are truly random.
#
# Refined by asking one extra question (see natpunch.refine_nat_subtype):
# what mapping does a brand NEW socket get from a server we already asked?
#
#   sym_easy_inc  next mapping is a little ABOVE this one (port + k)
#   sym_easy_dec  next mapping is a little BELOW this one
#   sym_hard      no usable pattern -- needs a birthday attack, or a relay
NAT_SUB_CONE = "cone"
NAT_SUB_EASY_INC = "sym_easy_inc"
NAT_SUB_EASY_DEC = "sym_easy_dec"
NAT_SUB_HARD = "sym_hard"
NAT_SUB_UNKNOWN = "unknown"

# RFC 5780 FILTERING behaviour -- the other axis.
#
# The subtypes above describe how a NAT MAPS outbound flows. They say
# nothing about what it lets back IN, and the two are independent: a NAT can
# hand out a fresh port per destination and still accept inbound packets
# from anywhere. That combination is far easier to punch than "symmetric"
# suggests, because the door is already open and only the port has to be
# guessed.
#
#   eif   endpoint-independent: any host may reach the mapping
#   adf   address-dependent:    only hosts we have written to
#   apdf  address+port dep.:    only the exact address we have written to
#
# Which of these applies decides who has to speak first -- see
# natpunch.must_open_first.
FILTER_EIF = "eif"
FILTER_ADF = "adf"
FILTER_APDF = "apdf"
FILTER_UNKNOWN = "unknown"

# Human-readable labels. Shown IN THE ROOM next to every player's name --
# see ui.nat_label. Kept here because these strings are protocol values,
# not presentation.
NAT_LABEL = {
    NAT_UNKNOWN: "未知",
    NAT_CONE: "锥型 (NAT1-3)",
    NAT_SYMMETRIC: "对称型 (NAT4)",
}
NAT_SUB_LABEL = {
    NAT_SUB_CONE: "锥型",
    NAT_SUB_EASY_INC: "对称·递增（可预测）",
    NAT_SUB_EASY_DEC: "对称·递减（可预测）",
    NAT_SUB_HARD: "对称·随机（难）",
    NAT_SUB_UNKNOWN: "未测出",
}
NAT_FILTER_LABEL = {
    FILTER_EIF: "入站宽松",
    FILTER_ADF: "入站中等",
    #: ★ 用户 2026-10-01：单 IP 的 STUN 测不出"换 IP"那一半 -> 不能说死是中等。
    #: ★ 用户 2026-10-03（最终口径）：**入站只有三档** —— 宽松 / 中等 / 严格
    #: （没测到就是未测）。不许出现"中等或更好"这类含糊话。
    #:
    #: `adf_or_eif` 是内部档位（"换端口答了、换 IP 测不了" = 不是端口受限），
    #: 展示上就是**中等**：既然后端拿不到"全锥"的硬证据，就不能显示宽松；
    #: 而它已经证明不是严格，所以显示中等。内部保留这个值只为了日志可追溯。
    "adf_or_eif": "入站中等",
    FILTER_APDF: "入站严格",
    FILTER_UNKNOWN: "入站未测",
}


def nat_label(nat, sub=None, filt=None):
    """One short line describing a member's NAT, for the room list.

    Deliberately verbose over terse: the people reading this are trying to
    work out why a connection is not working, and "NAT4" alone does not
    tell them whether that is fatal. "对称·递增（可预测）" and
    "对称·随机（难）" are both symmetric, and they are not the same problem.
    """
    # The subtype already implies the coarse type -- "对称·递增" IS
    # symmetric -- so printing both was redundant and made the room list
    # row too long to read. Only fall back to the coarse label when there
    # is nothing more specific.
    if sub and sub in (NAT_SUB_EASY_INC, NAT_SUB_EASY_DEC, NAT_SUB_HARD):
        parts = [NAT_SUB_LABEL[sub]]
    else:
        parts = [NAT_LABEL.get(nat, NAT_LABEL[NAT_UNKNOWN])]
    if filt and filt != FILTER_UNKNOWN:
        parts.append(NAT_FILTER_LABEL.get(filt, filt))
    return " · ".join(parts)

# How far apart two mappings may be and still count as "sequential".
# Beyond this the allocation is not a small step, so prediction is a waste.
NAT_EASY_MAX_STEP = 100
# Gap between two DIFFERENT server IPs that means "each destination IP gets
# its own port region".
#
# Two different IPs reporting 47524 and 41105 (real log) is not a step, it
# is two separate pools: this NAT hands out a fresh region per destination
# IP. The port it will use for the peer -- an address we have never sent to
# -- is therefore somewhere we cannot predict, and a contiguous window
# around the port it published for a STUN server is aimed at the wrong
# region entirely.
#
# It is deliberately much larger than NAT_EASY_MAX_STEP, because a SMALL
# gap between two IPs is not evidence of separate pools: consecutive flows
# to one host drift with whatever else the machine is doing (measured 167
# where the true step was 1), and the same drift shows up between two IPs.
# Only a gap no amount of background traffic explains means separate pools.
PER_IP_POOL_GAP = 512
# Share of every scan round spent on uniformly random ports when the peer's
# allocation is per-destination-IP, i.e. when a window cannot possibly be
# aimed correctly. See natpunch._run_punch_inner.
WINDOW_RANDOM_MIX_PER_IP = 0.35
# How spread out the mappings seen from one socket may be before we call it


#: His server list, kept as-is. Our own relay's STUN is prepended by the
#: caller (see default_servers), because it is the one destination the client
#: is certain to be able to reach.
STUN_SERVERS = [
    ("stun.miwifi.com", 3478),
    ("stun.chat.bilibili.com", 3478),
    ("stun.hitv.com", 3478),
    ("stun.cdnbye.com", 3478),
    ("stun.yy.com", 3478),
    ("stun.cloudflare.com", 3478),
]

#: How many times to sample "what does the NEXT mapping look like".
#:
#: One reading is not trustworthy (see `_third_probes`): the two probes are
#: seconds apart and anything else the machine does in between consumes an
#: allocation slot too, so a single sample can be inflated. Two agreeing ones
#: are enough; three is the point of diminishing returns.
THIRD_PROBE_SAMPLES = 3

#: Stop as soon as this many samples agree. The connect path is user-visible,
#: so it takes the minimum and no more.
THIRD_PROBE_MIN_SAMPLES = 2

#: Ignore "steps" larger than this: the allocation is not sequential, so
#: treating the gap as a direction to walk from is meaningless. His value.
SYM_MAX_DELTA = 1000

#: Total time allowed for endpoint discovery, his value. A hard budget
#: matters: 16 servers x 3 s would be ~48 s of apparent hang on a network
#: that blocks UDP outright.
STUN_BUDGET_S = 6.0

#: 已经有 `want` 个答案、但都来自**同一个 IP** 时，额外给多久去找第二个 IP。
#:
#: 为什么需要它：默认池的前两台是我们自己中继的 3478/3479（同一个 IP）。
#: 只按"两个答案"收工的话，分类永远只有一条 IP 可比较，分不出「全锥」和
#: 「按目标 IP 分段分配」。所以在答案够了之后再宽限一小段时间，让公网 STUN
#: 有机会给出第二个 IP。
#:
#: 但**不能**给出整个 STUN_BUDGET_S：公网 STUN 在国内基本不通，那样每次
#: 点「开始连接」都要多等 5~6 秒。2.5 秒是"多问一两台"和"不让用户等"的取中。
SECOND_HOST_GRACE_S = 2.5



# ============================================================
# STUN / reachability
# ============================================================

STUN_MAGIC = b"\x21\x12\xa4\x42"
STUN_MAGIC_INT = 0x2112A442

# --- RFC 5780: NAT behaviour discovery ---------------------------------
#
# Everything above tells us how the NAT MAPS outbound flows. It says
# nothing about what it lets back IN -- and that is the other half of the
# problem. Two NATs with identical mapping behaviour need completely
# different handling if one filters per-destination and the other does not.
#
# The attribute that makes this measurable is CHANGE-REQUEST: ask the
# server to answer from a different IP and/or port. If that answer arrives,
# the NAT is letting in traffic from somewhere we never contacted, which is
# exactly the question.
CHANGE_REQUEST = 0x0003
CHANGE_PORT = 0x00000001       # RFC 5780 flag "B"
CHANGE_IP = 0x00000002         # RFC 5780 flag "A"
OTHER_ADDRESS = 0x802C         # where the server WOULD answer from
RESPONSE_ORIGIN = 0x802B       # where this answer actually came from

# Filtering behaviour. Ordered by how strict they are -- see filter_rank.
FILTER_EIF = "eif"             # endpoint-independent: anything may come in
FILTER_ADF = "adf"             # address-dependent: only hosts we wrote to
FILTER_APDF = "apdf"           # address AND port dependent
FILTER_UNKNOWN = "unknown"

#: 过滤行为探测的总预算（他的 FILTER_PROBE_BUDGET_S）。
#:
#: 他要跑三组探测（普通 / 换 IP+端口 / 只换端口），而且**要轮着问名单上的每一台**
#: —— 因为"某台服务器无视 CHANGE-REQUEST"只说明那台不行，不说明网络被判成什么。
#: 4 秒是他在"问完所有服务器"和"不让用户等"之间的取中。
#: 用户 2026-09-25 报的「小概率出现 锥形（过滤未测）」就是这个数字太小。
#:
#: 他原来的 4.0 秒是给"三个测试"的预算；我们后来加了两个动作：
#:   * 「只换端口」没答时**重试一次**（手册 §7.5：慢网络上一次超时不该定案）；
#:   * 结尾的 **canary**（再发一次普通请求，用来区分"服务器没理我"和"包被
#:     NAT 丢了"）。
#: 于是最坏路径变成 I(1.5) + III(1.5) + III重试(1.5) + II(1.5) + canary —
#: 4 秒**装不下**，`_left()` 会被夹到 0.05 秒，canary 拿不到应答 →
#: 判成 `unknown` → 界面上就是「锥型（过滤未测）」。它是概率性的，因为要
#: 看网络有多慢/多少个包真的丢了。
#:
#: 7.0 秒是这个序列的上界（4×1.5 + 余量）。它只在这条诊断路径上花掉，
#: 而且一旦有结论就停（`measure` 里拿到非 unknown 立刻 break）。
FILTER_PROBE_BUDGET_S = 7.0


def _stun_parse(data, txid, out=None):
    """Extract (ip, port) from a binding response, validating it first.

    Both checks matter. Without them ANY datagram that happens to arrive
    first -- the peer's HELLO, a keepalive, another app entirely -- would be
    parsed as a STUN reply and an unrelated address published to the peer,
    which is worse than failing outright.
    """
    if len(data) < 20:
        return None
    kind, length = struct.unpack(">HH", data[:4])
    if kind != 0x0101:                       # binding response
        return None
    if data[4:8] != STUN_MAGIC:              # magic cookie
        return None
    if data[8:20] != txid:                   # our transaction
        return None
    if 20 + length != len(data):
        return None
    # Scan EVERY attribute before returning.
    #
    # Returning as soon as the mapped address was found -- which is what
    # this did -- means anything positioned after it is never seen. For
    # RFC 5780 that is fatal: RESPONSE-ORIGIN is the only way to tell a
    # server that honoured CHANGE-REQUEST from one that ignored it, and
    # servers are free to put the attributes in any order. Reading it as
    # absent made every such server look permissive.
    attrs = data[20:]
    p = 0
    best = None
    xored = None
    while p + 4 <= len(attrs):
        a, l = struct.unpack_from(">HH", attrs, p)
        v = attrs[p + 4:p + 4 + l]
        if out is not None and a in (RESPONSE_ORIGIN, OTHER_ADDRESS) \
                and len(v) >= 8:
            # family byte is v[1]: 1 = v4. Anything else we cannot use.
            if v[1] == 1:
                o_port = struct.unpack_from(">H", v, 2)[0]
                o_ip = ".".join(str(b) for b in v[4:8])
                key = ("origin" if a == RESPONSE_ORIGIN else "other")
                out[key] = (o_ip, o_port)
        if a == 0x0020 and len(v) >= 8:      # XOR-MAPPED, preferred
            port = struct.unpack_from(">H", v, 2)[0] ^ (STUN_MAGIC_INT >> 16)
            ip = ".".join(str(v[4 + i] ^ STUN_MAGIC[i]) for i in range(4))
            xored = (ip, port)
            if out is not None:
                out["mapped"] = xored
        elif a == 0x0001 and len(v) >= 8 and best is None:
            port = struct.unpack_from(">H", v, 2)[0]
            best = (".".join(str(b) for b in v[4:8]), port)
            if out is not None and "mapped" not in out:
                out["mapped"] = best
        p += 4 + ((l + 3) & ~3)
    return xored or best


def _stun_once(sock, host, port, timeout, hub=None, change=0, info=None):
    """One STUN binding request from `sock`. Returns (ip, port) or None.

    `sock` MUST be the punch socket: a symmetric NAT hands out a different
    mapping per destination, so a mapping learned elsewhere is useless.

    When `hub` is given the reply is collected by the hub thread instead of
    by a second recvfrom() on the same socket (see UdpHub.exchange).

    `change` is an RFC 5780 CHANGE-REQUEST: ask the server to answer from a
    different port and/or IP. When it is set the reply no longer comes from
    `addr`, so matching by source address would drop the very answer we
    asked for -- the transaction id is then the only thing we can match on,
    which is safe because it is 12 random bytes.

    `info` (a dict) receives RESPONSE-ORIGIN / OTHER-ADDRESS when present,
    which is how we tell a server that honoured CHANGE-REQUEST from one
    that silently ignored it.

    Never leaves a socket timeout behind: this socket is shared with the
    tunnel reader, and a stray timeout silently kills it.
    """
    try:
        txid = os.urandom(12)
        body = b""
        if change:
            body = struct.pack(">HHI", CHANGE_REQUEST, 4, change)
        msg = (struct.pack(">HH", 0x0001, len(body)) + STUN_MAGIC + txid
               + body)
        addr = (socket.gethostbyname(host), port)

        # With CHANGE-REQUEST the answer may legitimately arrive from
        # anywhere, so match on the transaction alone.
        strict_src = not change

        if hub is not None:
            # Where the answer really came from is recorded too, not just
            # what the server claims about it -- see _answered_from_elsewhere
            # for why an unverifiable answer must not count as permission.
            seen = {}

            def _match(d, a, _t=txid, _s=strict_src, _addr=addr):
                if len(d) < 20 or d[8:20] != _t or d[4:8] != STUN_MAGIC:
                    return False
                ok = (a[0] == _addr[0]) if _s else True
                if ok:
                    seen["src"] = (a[0], a[1])
                return ok

            data = hub.exchange(msg, addr, timeout, _match)
            if not data:
                return None
            if info is not None and seen.get("src"):
                info["src"] = seen["src"]
            return _stun_parse(data, txid, info)

        prev = sock.gettimeout()
        try:
            sock.settimeout(timeout)
            sock.sendto(msg, addr)
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                sock.settimeout(left)
                data, src = sock.recvfrom(2048)
                if strict_src and src[0] != addr[0]:
                    continue            # not our server; ignore
                got = _stun_parse(data, txid, info)
                if got:
                    if info is not None:
                        try:
                            info["src"] = (src[0], src[1])
                        except Exception:
                            pass
                    return got
        except socket.timeout:
            return None
        except Exception:
            return None
        finally:
            try:
                sock.settimeout(prev)
            except Exception:
                pass
    except Exception:
        return None
    return None


def stun_mapped_address(sock, timeout=3.0, servers=None, hub=None):
    """Tiny STUN binding request. Returns (ip, port) or None."""
    for host, port in (servers if servers is not None else STUN_SERVERS):
        got = _stun_once(sock, host, port, timeout, hub=hub)
        if got:
            return got
    return None


def _answered_from_elsewhere(info, server, want_ip, want_port):
    """Did the server really answer from a different place?

    A server that does not implement RFC 5780 simply IGNORES
    CHANGE-REQUEST and answers from where it always answers. That answer
    gets through any NAT -- we just sent there -- so it would be read as
    proof of a permissive NAT when it proves nothing at all.

    RESPONSE-ORIGIN tells us where the answer actually came from, so we can
    tell the two apart, and so does the source address of the datagram we
    received -- which every server gives us, whether it speaks RFC 5780 or
    not.

    When NEITHER is available there is no evidence, and an unverifiable
    answer must not be counted as permission. This is how a real log came
    to say, about a host that drops every unsolicited packet:

        [NAT] stun.miwifi.com ignored CHANGE-REQUEST; filtering unknown
        [NAT] filtering: endpoint-independent (anything may come in)

    i.e. one server honestly reported "I ignored that", and the next one
    was believed without evidence. "The door is already open" then decided
    the whole punch strategy -- the side that should have been sending to
    the peer first sat still and waited to be found instead.

    Being wrong towards "unknown" costs a little optimism; being wrong
    towards "eif" costs the connection.
    """
    origin = info.get("origin") or info.get("src")
    if not origin:
        return False         # no evidence -> not evidence of permissiveness
    if want_ip and origin[0] == server[0]:
        return False         # did not change IP
    if want_port and origin[1] == server[1]:
        return False         # did not change port
    return True


def change_ip_possible(host, port=3478):
    """
    "换 IP 的应答"这件事**在这个服务器上有没有可能发生**？

    用户 2026-10-01 追问的正是这一点：暖端口 / 单 IP 会不会把全锥误判成
    地址受限。判据很硬 —— RFC 5780 的换 IP 要求**同一个服务器**从**另一个
    公网 IP** 应答；如果这个主机名只解析出一个 IPv4，那台服务器根本给不出
    这种应答，我们收到的任何"地址受限"结论都只是"**最多**地址受限"。

    只有解析出 >=2 个 IPv4 时，换 IP 才真的被验证过（那时"没答回来"才是
    adf 的硬证据）。
    """
    seen = set()

    with contextlib.suppress(Exception):
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM):
            family, _, _, _, sockaddr = info

            if family == socket.AF_INET and sockaddr and sockaddr[0]:
                seen.add(sockaddr[0])

    return len(seen) > 1


def stun_filtering(sock, host, port, timeout=1.5, hub=None, log=None,
                   deadline=None):
    """RFC 5780 filtering behaviour: what does the NAT let back IN?

    Mapping behaviour (what everything else here measures) tells us which
    external port a flow gets. It says NOTHING about which inbound packets
    survive -- and that is the other half of whether a hole can be punched
    at all.

    Three tests, in this order (**the order is load-bearing**, see below):
      I   plain binding request          -> proves the server is reachable
      III CHANGE-REQUEST port only       -> answer means traffic from a host
                                            we DID contact gets through,
                                            from any port (at most adf)
      II  CHANGE-REQUEST ip+port         -> answer means traffic from a host
                                            we never contacted gets through
                                            (eif / full cone)

    "Port only" MUST be asked BEFORE "ip+port". A single-IP server answers the
    ip+port request from its sibling port, and that first admission opens that
    port in an apdf NAT's table -- after which the port test would wrongly
    succeed and a port-restricted NAT (NAT3) gets reported as NAT2. Measured
    by the user: mao.fan/mynat and BitComet both say NAT3, we said NAT2.

    Why this is worth three more packets: it decides who has to speak first.
    A NAT that filters per-destination drops the peer's packets until we
    have sent to that exact address ourselves. If both ends wait for the
    other, nothing ever arrives. Knowing which end is stricter tells us
    which one must open the door.
    """
    try:
        server_ip = socket.gethostbyname(host)
    except Exception:
        return FILTER_UNKNOWN
    server = (server_ip, port)

    # One budget for all three tests. Each has its own timeout, but three
    # unreachable servers in a row used to stack into ~14s of dead time on
    # the startup path -- and on a network where UDP is blocked entirely,
    # that is time spent measuring nothing.
    if deadline is None:
        deadline = time.monotonic() + timeout * 4

    def _left():
        return max(0.05, deadline - time.monotonic())

    # Test I
    info = {}
    if not _stun_once(sock, host, port, min(timeout, _left()), hub=hub,
                      info=info):
        return FILTER_UNKNOWN

    # No translation at all: nothing is being filtered.
    mapped = info.get("mapped")
    if mapped and mapped[0] == server_ip and mapped[1] == port:
        return FILTER_EIF

    # ==================================================================
    #  顺序**必须**是：先"只换端口"，再"换 IP + 换端口"
    # ==================================================================
    #
    # 2026-09-25 用户实测报的错：他的网络是 **NAT3（端口受限）**，我们报成
    # NAT2（地址受限）—— 过滤那一档测成了 adf，实际是 apdf。参考工具
    # （mao.fan/mynat、比特彗星）都报 NAT3，是我们错。
    #
    # 根因就在这个顺序上，而且是**我们自己那台中继**造成的：
    #
    #   * 单 IP 的 VPS 收到"换 IP + 换端口"的请求，只能"换端口"应答
    #     （3478 -> 3479）；这个应答的源地址是 (服务器IP, 3479)；
    #   * 于是先做 Test II 的话，那个包就**被我们的 NAT 放行了一次** ——
    #     端口受限（apdf）的 NAT 会因此把 (服务器IP, 3479) 加进放行表；
    #   * 紧接着的 Test III（只换端口）再问一次，同一个来源这次就被放行了
    #     → 我们读成"端口不限" = adf = NAT2。
    #
    # 反过来说，只要**先问只换端口**，那一刻 (服务器IP, 3479) 还从来没被
    # 放行过、也没被我们主动发过包，apdf 的 NAT 就会老老实实把它丢掉。
    #
    # 这也是为什么过滤探测**必须用一个全新的 socket**（见 measure()）：
    # 打洞 socket 在测映射时已经往 3478 **和 3479** 都发过 STUN 请求，
    # 那本身就把 3479 暖了 —— 用那个 socket 测端口轴，apdf 永远测不出来。
    #
    # Test III -- 只换端口（先做）
    i3 = {}
    got3 = _stun_once(sock, host, port, min(timeout, _left()), hub=hub,
                      change=CHANGE_PORT, info=i3)
    port_moved = bool(got3) and _answered_from_elsewhere(i3, server, False, True)

    # 手册 §7.5：**慢网络上"换过源"的应答可能比 1 个超时更久**，那会被读成
    # "没收到" → 假 apdf（严格）。而"apdf"是个**结论**，不该由一次超时决定。
    # 所以没收到时再问一次，只有两次都没到才继续往下判。
    if not port_moved:
        i3b = {}
        got3b = _stun_once(sock, host, port, min(timeout, _left()), hub=hub,
                           change=CHANGE_PORT, info=i3b)

        if got3b and _answered_from_elsewhere(i3b, server, False, True):
            i3, got3 = i3b, got3b
            port_moved = True
        elif got3b and not got3:
            i3, got3 = i3b, got3b

    # Test II -- 换 IP + 换端口（后做：它的应答会污染端口轴）
    i2 = {}
    got2 = _stun_once(sock, host, port, min(timeout, _left()), hub=hub,
                      change=(CHANGE_IP | CHANGE_PORT), info=i2)

    if got2 and _answered_from_elsewhere(i2, server, True, True):
        # 从另一个 IP 答回来了：陌生主机能打进来 = 全锥。
        return FILTER_EIF

    if port_moved:
        # 从"我们联系过的地址、但没联系过的端口"答回来了 → **最多**地址受限。
        #
        # ★ 用户 2026-10-01：这一步以前直接返回 FILTER_ADF，界面就把它显示成
        #   「NAT2 地址受限锥形」。但"最多"不是"就是"：
        #     * 服务器有第二个公网 IP（换 IP 真的测过、没答回来）-> adf 是硬证据；
        #     * 服务器只有一个公网 IP（我们这台中继就是）-> 全锥**根本无从证明**，
        #       只能给"adf 或 eif"这个区间。
        #   判据用解析结果（见 change_ip_possible），不猜。
        possible = change_ip_possible(host, port)

        if log:
            try:
                if possible:
                    log("[NAT] %s answered from another port (%s -> %s): "
                        "filtering is at most address-dependent (adf)"
                        % (host, server, i3.get("origin") or i3.get("src")))
                else:
                    log("[NAT] %s answered from another port (%s -> %s), but "
                        "this server has ONE public IP, so full cone cannot be "
                        "ruled out: filtering is adf-or-eif (undecided)"
                        % (host, server, i3.get("origin") or i3.get("src")))
            except Exception:
                pass

        if possible:
            return FILTER_ADF

        from . import punch as punch_mod

        return punch_mod.FILTER_ADF_OR_EIF

    # ==================================================================
    #  换过源的应答一个都没进来 —— 必须分清这两种情况
    # ==================================================================
    #
    #   服务器**根本没理** CHANGE-REQUEST   → 它从原地址照答，只是没换源
    #                                        → 什么都没测到 = unknown
    #   服务器**照做了**，包被我们的 NAT 丢了 → 一条应答都收不到
    #                                        → apdf（严格，NAT3）
    #
    # 判据就是"原地址上还答不答"：
    #   * `got3` / `got2` 为真 = 它答了、只是没换源 → 它不实现 RFC 5780；
    #   * 两者都假 = 换源的包全被丢了 —— 但也要排除"服务器中途死了"，
    #     所以再发一次**普通**请求当对照：还活着 → 就是被丢了 → apdf。
    #
    # 这一步（canary）是 2026-09-25 修 NAT2/NAT3 时补上的：没有它，
    # "服务器忽略换端口"和"我们的 NAT 丢了换端口的包"分不开。
    if got3 or got2:
        if log:
            try:
                log("[NAT] %s ACKed but answered from where it always does "
                    "(port %s); it ignores CHANGE-REQUEST -> filtering unknown"
                    % (host, port))
            except Exception:
                pass
        return FILTER_UNKNOWN

    # 到了这里：两次"换端口" + 一次"换 IP+端口"**一个包都没收到**。
    #
    # 手册（§2.8）说这一格是 apdf。但"没收到"也可能是服务器刚好死了，所以先
    # 用普通请求当对照 —— 服务器还活着，就说明换过源的包是被**我们的 NAT**
    # 丢的：端口受限（apdf / NAT3）。这是**测出来的结论**，不是默认兜底。
    # canary 是这条判断的**最后一块证据**，不许因为预算见底而拿不到应答：
    # 至少给它 0.6 秒（比一个普通请求的往返多得多），宁可整体略微超出预算。
    canary_timeout = min(timeout, max(0.6, _left()))

    if _stun_once(sock, host, port, canary_timeout):
        if log:
            try:
                log("[NAT] %s is alive but every changed-source reply was "
                    "dropped (3 tries): filtering is address+port dependent "
                    "(apdf, NAT3). This is a measurement, not a fallback."
                    % host)
            except Exception:
                pass
        return FILTER_APDF

    # 连普通请求都没人答：服务器死了，什么都没证明。
    return FILTER_UNKNOWN


def stun_order(servers):
    """Order candidates so the first two ANSWERS are likely different IPs.

    NAT typing compares the mapped port seen by two destinations. If both
    destinations share an IP -- e.g. the built-in STUN on 3478/3479 -- a NAT
    that allocates per destination *IP* reports the same port for both, and
    gets misclassified as cone. That is the common CGNAT case, and the
    misclassification silently disables port prediction for exactly the
    users who need it.

    The whole list comes back, NOT just a pair of endpoints: stun_probe
    keeps asking until two servers have answered, because a pair chosen up
    front left nothing to fall back on when one of the two was unreachable
    -- one sample means NAT_UNKNOWN and no prediction at all, which is
    exactly what happened with a reachable built-in STUN plus a blocked
    public one.

    Order: EVERY entry of the first IP first, then round-robin across the
    remaining IPs (one each, then a second each, ...).

    Strict round-robin used to interleave the first entry of every IP
    before the second entry of ANY, which put the operator's second
    built-in port behind every public server on the list. When those are
    unreachable -- the normal case in mainland China, and the exact reason
    the built-in STUN exists -- the budget ran out before that second port
    was ever asked, so the client got one sample, reported NAT_UNKNOWN, and
    switched port prediction off for precisely the deployment it was built
    for.

    Asking two ports on the SAME IP is still a real measurement: it does
    not separate per-IP allocation from cone, but it does detect the common
    case, per-destination-port allocation. Two samples from one reachable
    server beat one sample plus a timeout.
    """
    by_ip = {}
    order = []
    for entry in servers:
        if entry[0] not in by_ip:
            by_ip[entry[0]] = []
            order.append(entry[0])
        by_ip[entry[0]].append(entry)
    if not order:
        return []
    out = list(by_ip[order[0]])          # the operator's own server, all of it
    rest = [by_ip[ip] for ip in order[1:]]
    idx = 0
    while True:
        rung = [lst[idx] for lst in rest if idx < len(lst)]
        if not rung:
            return out
        out.extend(rung)
        idx += 1


def _third_probe(host, port, timeout):
    """What mapping does a brand NEW socket get from a server we asked?

    This is the measurement that separates "predictable NAT4" from "hopeless
    NAT4", and it is the one that is almost always missing. Comparing two
    servers on ONE socket only tells you the mapping depends on the
    destination. Asking the same server from a SECOND socket tells you where
    the next mapping lands -- and "next" is precisely what the peer has to
    guess when it punches.

    A throwaway socket is correct here: we are measuring the NAT's
    allocation behaviour, not a mapping we intend to use.
    """
    return _third_probes(host, port, timeout, 1)[0]


def _third_probes(host, port, timeout, n=3, want=None):
    """Several samples of "what does the NEXT mapping look like".

    One sample is not trustworthy. Between two probes the machine may open
    any number of unrelated UDP flows (DNS, QUIC, a game, a browser), and
    every one of them consumes an allocation slot -- so a single reading
    can be arbitrarily far from the real step, and a far reading means
    "hopeless NAT4", i.e. we give up on the connection.

    Extra slots can only make the observed gap LARGER, never smaller, so the
    smallest gap across samples is the best estimate of the true step.

    `n` is how many to gather, and it governs both the attempt count and
    the early exit. There used to be a separate `want` that only moved the
    exit point, so asking for 4 while attempting 3 still yielded 3 -- the
    background re-check asked for more samples than the connect path and
    quietly got the same number.

    Small n on the connect path (the user is watching), larger off it
    (nothing is waiting, and every extra sample is another chance to see
    the true step behind the machine's other traffic).
    """
    if want is None:
        want = n
    out = []
    # Total budget for the whole sampling run, not per sample. When the
    # server is unreachable every probe runs to its full timeout, so three
    # samples meant 4.5s of dead time on every connect -- and on a
    # reconnect, where the user is already waiting, that is very visible.
    deadline = time.monotonic() + max(0.5, min(timeout, 3.0))
    for _ in range(max(1, n)):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        tmp = None
        try:
            tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tmp.bind(("", 0))
            got = _stun_once(tmp, host, port, min(1.5, left))
            if got:
                out.append(got[1])
        except Exception:
            pass
        finally:
            if tmp is not None:
                try:
                    tmp.close()
                except Exception:
                    pass
        if len(out) >= max(2, want):
            # Enough samples to trust the step. Two is right on the
            # connect path, where asking more costs latency the user can
            # see; a background re-check has time to spare and should take
            # more, because every extra sample is another chance to catch
            # the true step behind the noise of the machine's other flows.
            break
    return out


def stun_probe(sock, timeout=3.0, servers=None, hub=None, want=2, log=None):
    got = stun_probe2(sock, timeout=timeout, servers=servers, hub=hub,
                      want=want, log=log)
    if not got:
        return None
    return got[0], got[1], got[2], got[3]


def stun_probe2(sock, timeout=3.0, servers=None, hub=None, want=2, log=None,
                third_n=None, extra=None):
    """stun_probe plus a NAT subtype. Returns (ip, port, nat, delta, subtype).

    The 4-tuple form is kept for every existing caller; this one exists
    because "symmetric" is not an answer, it is a question. See
    natpunch.refine_nat_subtype.

    `third_n`: how many "where does the next port land" samples to gather
    before deciding. More samples make a downgrade to "random" less likely
    to be a fluke, at the cost of time -- so it is worth it off the connect
    path, and not worth it on it.
    """
    return _stun_probe_full(sock, timeout, servers, hub, want, log,
                            third_n=third_n, extra=extra)


def _stun_probe_full(sock, timeout, servers, hub, want, log, third_n=None,
                     extra=None):
    """Endpoint discovery + NAT type. Returns (ip, port, nat_type, delta).

    Asks two DIFFERENT STUN destinations from the SAME socket:
      * same mapped port for both  -> cone NAT (one mapping, every peer sees
        the same endpoint, so a single target port is enough)
      * different ports            -> symmetric NAT (a fresh mapping per
        destination), and the difference is the allocation increment.

    The port reported is the LAST one: on a symmetric NAT it is the closest
    to whatever the next allocation will be.

    It keeps going down the list until `want` servers have ANSWERED (or the
    budget runs out) rather than committing to two endpoints up front: a
    silent server must not cost us the second sample.
    """
    pool = stun_order(list(servers if servers is not None else STUN_SERVERS))
    if not pool:
        return None

    results = []      # [(mapped_ip, mapped_port), server_host, server_port]
    askable = []      # hosts we actually sent a probe to (for the card)

    # ---- 「两个答案」不够，要「两个不同 IP 的答案」----
    #
    # 默认池的前两个就是**同一个 IP 的两个端口**（我们自己的中继 3478/3479）。
    # 老条件 `len(results) >= want` 在那里直接收工，于是 hosts 永远只有一个
    # 元素，分类永远走"单 IP"分支 —— 而单 IP 分不出「全锥」和「按目标 IP 分段
    # 分配」，也分不出「同一目标内部递增」和「全局递增」。表现就是用户看到的
    # 「配了好几台 STUN，结果还是 unknown/说不准」。
    #
    # 所以：数目够了还不行，还得有 **2 个不同 IP** 才能停；池里本来就只有
    # 一个 IP 时（他的单服务器部署）不硬求，照旧走他那个单 IP 的 openp2p
    # 判定（cone vs symmetric）。
    pool_hosts = {host for host, _port in pool}
    need_hosts = min(2, len(pool_hosts))

    # Hard budget: 16 servers x 3s would be ~48s of apparent hang on a
    # network that blocks UDP.
    deadline = time.monotonic() + STUN_BUDGET_S
    host_grace_deadline = None

    for host, port in pool:
        answered_enough = len(results) >= want
        hosts_ok = len({r[1] for r in results}) >= need_hosts

        if answered_enough and hosts_ok:
            break

        if answered_enough:
            # 答案够了但只有一个 IP：再宽限一小段时间找第二个 IP。
            if host_grace_deadline is None:
                host_grace_deadline = time.monotonic() + SECOND_HOST_GRACE_S

            if time.monotonic() >= host_grace_deadline:
                if log:
                    try:
                        log("[NAT] %d 台答了但都是同一个 IP（%s）；"
                            "宽限 %.1fs 内没等到第二个 IP，按单 IP 判定"
                            % (len(results), ",".join(sorted({r[1] for r in results})),
                               SECOND_HOST_GRACE_S))
                    except Exception:
                        pass
                break

        left = deadline - time.monotonic()
        if left <= 0.05:
            break
        askable.append((host, port))
        got = _stun_once(sock, host, port, min(timeout, left), hub=hub)
        if got:
            # got is the MAPPED (public) endpoint; (host, port) is the
            # SERVER we asked. Keep both -- conflating them is what sent the
            # third probe to the mapped port instead of the server, where
            # nothing listens, so it always timed out and every symmetric
            # NAT was mis-classified as unsolvable.
            results.append((got, host, port))
    if not results:
        return None

    ip, port = results[-1][0]
    if len(results) < 2:
        # Only one server ever answered, so there is nothing to compare
        # against: the NAT type is genuinely unknown and prediction stays
        # off. Say so, because "unknown" here usually means "one of your
        # STUN servers is unreachable", which is fixable.
        if log:
            try:
                log("[NAT] only %d STUN server answered (%s) -> unknown type"
                    % (len(results), ", ".join(sorted(r[1] for r in results))))
            except Exception:
                pass
        return ip, port, NAT_UNKNOWN, 1, NAT_SUB_UNKNOWN

    hosts = {r[1] for r in results}
    ports = [r[0][1] for r in results]   # mapped ports
    delta = ports[-1] - ports[0]

    # Separate port region per destination IP?
    #
    # Real log, one machine: two DIFFERENT STUN IPs reported 47524 and
    # 41105 -- 6419 apart. That is not a step, it is two pools, so the
    # port this NAT will use for the peer (an address we have never sent
    # to) is in a region nothing we measured points at. A contiguous
    # window around its published port then cannot hit, no matter how
    # wide: it is aimed at the STUN pool.
    #
    # Flagged rather than folded into the subtype, because the subtype
    # describes the allocator WITHIN one destination (which really is
    # sequential -- "+35" per flow) and that is still useful. What is
    # unusable is the window, so the scan mixes in random ports instead.
    if isinstance(extra, dict):
        extra["per_ip_pool"] = bool(abs(delta) > PER_IP_POOL_GAP)
        extra["sample_ports"] = list(ports)
        extra["sample_hosts"] = sorted(hosts)
        # ---- CraftLink addition (NOT his code) ----
        #
        # Record WHICH server answered with WHAT. Without this, the caller can
        # only see the set of mapped ports, and then has to GUESS which server
        # produced them -- which is what made the diagnostics card report
        # "1/7 servers responded" while six of them were actually answering.
        #
        # `results` is his own list at this point, so this is pure bookkeeping:
        # it changes no probe, no timeout and no classification.
        extra["sample_results"] = [
            (host, srv_port, mapped) for mapped, host, srv_port in results
        ]
    if abs(delta) > PER_IP_POOL_GAP and log:
        try:
            log("[NAT] two different STUN IPs gave ports %s (%d apart): "
                "this NAT allocates a separate port region per destination "
                "IP, so the port it will use for the peer is NOT near the "
                "one it published. Mixing random ports into the scan."
                % (ports, abs(delta)))
        except Exception:
            pass

    # WHAT KIND of gap is `delta`? It decides whether prediction applies
    # to the peer at all, and getting this wrong silently disables every
    # mechanism.
    #
    # Punching needs to predict the port we get for a brand NEW
    # destination -- the peer's IP, which we have never sent to. So the
    # only relevant measurement is the gap between two DIFFERENT server
    # IPs. A gap between two ports of the SAME IP says "consecutive flows
    # to one host differ by this much", which tells us nothing about a new
    # host.
    #
    # Real log, one machine, seconds apart:
    #
    #   09:01:24  two ports of ONE ip -> 44019, 44020   (gap 1)
    #   09:01:27  two DIFFERENT ips   -> 44020, 7647    (gap -36373)
    #
    # Same NAT. Reading the first as "sequential, therefore predictable"
    # is what made the scan aim at 44020..49940 while the array actually
    # opened in a completely different region -- 10,600 packets, none of
    # which could ever arrive.
    same_ip_gap = len(hosts) < 2
    if isinstance(extra, dict):
        # 给卡片用：只有一条 IP 有响应时，用户需要在界面上看到"为什么只有
        # 一个 IP"——通常就是服务器上的 UDP 3478/3479 没全开，或者公网
        # STUN 被运营商拦了。
        extra["asked_servers"] = list(askable)
        extra["answered_hosts"] = sorted(hosts)
        extra["single_ip_host"] = (sorted(hosts)[0] if same_ip_gap else "")
    if same_ip_gap and log:
        try:
            log("[NAT] both samples came from one IP (%s): the step "
                "between them describes consecutive flows to ONE host, "
                "not what a brand-new destination gets -- the peer's "
                "address is a brand-new destination"
                % ",".join(sorted(hosts)))
        except Exception:
            pass

    # Diagnosed but not treated, which is worse than either: the log line
    # above says the step is meaningless for a new destination, and then
    # the code went ahead and published it anyway.
    #
    # Real log, one machine:
    #
    #   09:01:24  two ports of ONE ip -> 44019, 44020   (step 1)
    #   09:01:27  two DIFFERENT ips   -> 44020, 7647    (a different region)
    #
    # Publishing "sym_easy_inc, delta 1" from the first line sends every
    # peer to walk 44020, 44021, 44022... while our array opens somewhere
    # else entirely. Measured: 10600 packets, none of which could arrive.
    #
    # With one IP there is no way to tell "sequential overall" from
    # "sequential within one host", so the honest answer is UNKNOWN --
    # and that is not a downgrade, because _punch_plan_for upgrades
    # unknown to a real mechanism (the array still runs), whereas a wrong
    # subtype sends the array to the wrong place.
    if same_ip_gap:
        # One IP, two ports: this is exactly openp2p's test, and it is
        # enough to tell cone from symmetric.
        #
        #   nat.go:  port1 == port2  ->  NATCone   else NATSymmetric
        #
        # It asks one question only: does changing the DESTINATION PORT
        # change the mapping? If not, every peer sees the same endpoint,
        # which is the definition of cone. If it does, the mapping is
        # per-flow and the peer's port is not the one we published.
        #
        # Returning UNKNOWN here was the wrong call, and it is why a cone
        # peer behind a server with only one STUN endpoint could never be
        # recognised as cone: the openp2p handshakes are chosen purely on
        # cone-vs-symmetric, so "unknown" silently disabled all three.
        if delta == 0:
            if log:
                try:
                    log("[NAT] cone: the same mapped port %s answered for "
                        "two destination ports of one IP, so every peer "
                        "sees this endpoint" % (port,))
                except Exception:
                    pass
            return ip, port, NAT_CONE, 0, NAT_SUB_CONE
        # Different port per destination port: symmetric. The STEP is
        # still not generalisable (see above) -- only the class is.
        if log:
            try:
                log("[NAT] symmetric: two destination ports of one IP got "
                    "mapped ports %s, so the mapping is per-flow"
                    % (ports,))
            except Exception:
                pass
        return ip, port, NAT_SYMMETRIC, 0, NAT_SUB_HARD

    # Only worth spending a round trip on when the first two disagree --
    # otherwise there is nothing to refine.
    #
    # Imported here, not at module scope: natpunch pulls in udptunnel, and a
    # top-level import would make common <-> natpunch circular.
    # 他原来从 natpunch 引入；在这里 refine_nat_subtype / nat_step
    # 就在本模块下方，是同一个移植块的一部分。

    p3 = None
    p3s = []
    if delta != 0:
        # Ask a server we ALREADY reached, from a brand new socket. The
        # server's own (host, port) -- not the mapped port from its reply.
        first_host, first_port = pool[0]
        for r in results:
            if r[1] == first_host:
                first_host, first_port = r[1], r[2]
                break
        p3s = _third_probes(first_host, first_port, timeout,
                            n=(third_n or THIRD_PROBE_MIN_SAMPLES))
        p3 = p3s[0] if p3s else None
    sub = refine_nat_subtype(ports[0], ports[-1], p3s)
    # The step comes from p3, not from the two-destination gap. Publishing
    # the gap (measured: 167 for a NAT whose step was 1) tells every peer
    # to walk the wrong distance.
    step = nat_step(ports[0], ports[-1], p3s)
    if step:
        delta = step
    if log:
        try:
            log("[NAT] %s (ports %s, next from a new socket: %s)"
                % (sub, ports, p3))
        except Exception:
            pass

    if delta == 0:
        # "Same port for two ports of the same host" is consistent with a
        # cone NAT AND with a NAT that allocates per destination IP -- and
        # with only ONE server to ask, the two are indistinguishable.
        #
        # It used to return cone here. That is the single most damaging
        # verdict in the whole program, because cone switches the entire
        # NAT4 mechanism off:
        #
        #   punch_plan(cone, *)      -> METHOD_CONE
        #   _multi_socket_first()    -> False   (no array, no window)
        #   _await_punch_start()     -> returns without waiting
        #   _refresh_anchor_if_stale -> never called
        #   _should_scan_window()    -> False
        #
        # leaving one socket aimed at the port the peer published for ITS
        # STUN server -- which a per-destination NAT drops on sight. The
        # user sees "cannot connect" and no punch ever ran.
        #
        # With several distinct IPs answering, delta == 0 really does mean
        # cone: a per-destination-IP allocator would have given different
        # ports to different IPs. Only the single-IP case is ambiguous, and
        # there UNKNOWN is right -- _punch_plan_for upgrades unknown to a
        # real mechanism, so the array still runs, whereas cone does not.
        if len(hosts) < 2:
            if log:
                try:
                    log("[NAT] both STUN answers came from one IP (%s): a "
                        "NAT that allocates per destination IP looks "
                        "identical to a cone NAT here -- reporting UNKNOWN "
                        "so punching still runs"
                        % ",".join(sorted(hosts)))
                except Exception:
                    pass
            return ip, port, NAT_UNKNOWN, 0, NAT_SUB_UNKNOWN
        return ip, port, NAT_CONE, 0, NAT_SUB_CONE
    if abs(delta) > SYM_MAX_DELTA:
        # allocation is not sequential -- prediction is pointless
        return ip, port, NAT_SYMMETRIC, 0, NAT_SUB_HARD
    return ip, port, NAT_SYMMETRIC, delta, sub




def refine_nat_subtype(p1, p2, p3):
    """Split "symmetric" into predictable and hopeless.

    p1  mapping seen by server A from socket 1
    p2  mapping seen by server B from socket 1   (same socket, other server)
    p3  mapping(s) seen by server A from socket 2 (new socket, same server)

    p3 may be a single port or a LIST of samples. A single reading is not
    trustworthy: between two probes the machine can open any number of
    unrelated UDP flows, and each consumes an allocation slot, inflating the
    observed gap. Extra slots can only make the gap LARGER, never smaller,
    so with several samples the smallest one is the best estimate.

    The third probe is the one that matters and the one everybody omits.
    p1 vs p2 tells you the mapping depends on the destination; only p3 tells
    you WHERE THE NEXT ONE LANDS, and "next" is what the peer has to guess.

    Returns one of NAT_SUB_*.
    """
    if not p1 or not p2:
        return NAT_SUB_UNKNOWN
    if p1 == p2:
        # one mapping for every destination
        return NAT_SUB_CONE

    # p3 is the primary evidence and it is judged FIRST.
    #
    # |p2 - p1| used to be checked here and returned sym_hard outright.
    # That is wrong, and it is how a perfectly predictable NAT came to be
    # reported as random: p1 and p2 are mappings for two DIFFERENT
    # destinations, so their gap is not a step -- it is the sum of the
    # step and however many unrelated flows the machine opened in between.
    # Observed: p1=60225, p2=60392 (gap 167) while p3=60393, i.e. the real
    # step was +1. The NAT had not changed at all; the reading was busy.
    #
    # p3 answers the actual question -- "where does the NEXT mapping land"
    # -- by asking a server we already reached from a fresh socket, so the
    # two allocations are consecutive.
    samples = p3 if isinstance(p3, (list, tuple, set)) else ([p3] if p3 else [])
    samples = [x for x in samples if x]
    if samples:
        hi = max(p1, p2)
        lo = min(p1, p2)
        # Only POSITIVE gaps are evidence. Two samples landing on opposite
        # sides of [lo, hi] is a perfectly ordinary sequential allocator
        # (one slot before, one after) -- but taking a signed min over both
        # directions yields a negative "gap" in one and discards the other,
        # so the pair was read as unpredictable and the connection was
        # abandoned. Judge each direction on its own positive evidence.
        ups = [s - hi for s in samples if 0 < s - hi < NAT_EASY_MAX_STEP]
        downs = [lo - s for s in samples if 0 < lo - s < NAT_EASY_MAX_STEP]
        if ups:
            return NAT_SUB_EASY_INC
        if downs:
            return NAT_SUB_EASY_DEC
        # We DID get samples and every one of them landed far away. That
        # is real evidence of randomness -- not an absence of evidence --
        # so it must not fall through to the optimistic branch below,
        # which is only for when the probe never answered at all.
        return NAT_SUB_HARD

    # No p3 AT ALL (the probe failed or was never run), so fall back to
    # the gap between destinations.
    # Asymmetric costs decide this: calling a random NAT predictable costs
    # one failed punch and then relaying, while calling a predictable NAT
    # random gives up on the direct connection outright. So a small gap is
    # read optimistically; only a large one, with nothing better to go on,
    # is called random.
    if abs(p2 - p1) <= NAT_EASY_MAX_STEP:
        return NAT_SUB_EASY_INC if p2 > p1 else NAT_SUB_EASY_DEC
    return NAT_SUB_HARD


def nat_step(p1, p2, p3):
    """The port-allocation step we actually believe, signed.

    NOT `p2 - p1`. Same reason as in refine_nat_subtype: those are two
    different destinations, and their gap is mostly "how busy was the
    machine". Measured 167 where the true step was 1 -- and that number is
    published as natDelta, so the peer strides by it.

    p3 gives two consecutive allocations, which is what a step is.
    """
    samples = p3 if isinstance(p3, (list, tuple, set)) else ([p3] if p3 else [])
    samples = [x for x in samples if x]
    if p1 and p2 and samples:
        hi = max(p1, p2)
        lo = min(p1, p2)
        ups = [s - hi for s in samples if 0 < s - hi < NAT_EASY_MAX_STEP]
        downs = [lo - s for s in samples if 0 < lo - s < NAT_EASY_MAX_STEP]
        if ups:
            return min(ups)
        if downs:
            return -min(downs)
    return (p2 - p1) if (p1 and p2) else 0

# ============================================================
# CraftLink async bridge (OURS -- everything above is his)
# ============================================================
#
# WHY THIS EXISTS
#
# His probes are synchronous `recvfrom` calls. The engine runs one event loop,
# so they must never run on it. These wrappers:
#
#   * run his code in an EXECUTOR THREAD, on the socket the caller passed in
#     (the punch socket, whose mapping is the one we must measure);
#   * force the socket into blocking mode for the duration, because his
#     `settimeout()` calls are meaningless on a non-blocking socket and would
#     turn every probe into an instant failure;
#   * restore everything afterwards, so the socket is exactly as we found it.
#
# Nothing here re-implements detection: it is all his functions.

#: Kept for callers that still pass "host or host:port" strings.
DEFAULT_STUN_PORT = 3478

#: The second built-in STUN port. MCLanP2P's deployment opens a CONSECUTIVE
#: PAIR (3478 + 3479) precisely so a single-IP VPS can offer two different
#: destinations; the client should use both.
DEFAULT_STUN_PORT2 = 3479
RESPONSE_TIMEOUT = 3.0


def pick_ipv4(infos):
    """
    The IPv4 entry from a getaddrinfo() result.

    (Ours, not his.) `stun.hitv.com` resolves to an IPv6 address FIRST, and
    taking `infos[0]` then building an AF_INET socket raises
    `gaierror: Address family for hostname not supported`, which aborted a
    whole probe run with a message that named neither the server nor the
    family. Preferring IPv4 is deliberate: the punch and the relay are IPv4,
    and a mapping measured on another family is meaningless for them.
    """
    if not infos:
        return None

    for family, _socktype, _proto, _canon, sockaddr in infos:
        if family == socket.AF_INET:
            return family, sockaddr

    family, _socktype, _proto, _canon, sockaddr = infos[0]

    return family, sockaddr


def default_servers(relay_host="", relay_port=None):
    """
    Servers to probe, best first.

    Our own relay's STUN responder comes FIRST, because it is the one
    destination the client is certainly able to reach -- it is already talking
    to that host over TCP -- and because it is the only one whose ports we
    control. His public list follows as a fallback.

    Note the port: the STUN pair is a fixed 3478/3479, NOT the relay's TCP
    port number. Earlier CraftLink builds put STUN on the same number as the
    TCP relay; his layout is the one both his client and his server expect.
    """
    servers = []

    host = str(relay_host or "").strip()

    # A compose placeholder or an empty field must never reach getaddrinfo.
    if host and not host.startswith("$") and host.lower() != "localhost":
        # BOTH built-in ports, same IP.
        #
        # Why this matters when public STUN is unreachable (the normal case in
        # mainland China, and reported by a real user whose peer could not
        # reach ANY public STUN server): these two are then the only
        # destinations left. They are enough to classify, because the mapping
        # comparison asks "does the mapped port depend on the destination",
        # and two ports on one IP are two different destinations.
        #
        # The caveat is his: two ports on ONE IP cannot separate "cone" from
        # "per-destination-IP pool", so such a run may read `cone` when the
        # NAT is really a per-IP allocator. That is a known limit, and it is
        # still far better than "no verdict at all" -- the public servers
        # further down the list settle it whenever they are reachable.
        servers.append((host, DEFAULT_STUN_PORT))
        servers.append((host, DEFAULT_STUN_PORT2))

    for entry in STUN_SERVERS:
        if entry not in servers:
            servers.append(entry)

    return servers


def _as_tuples(servers):
    """Accept "host", "host:port" and (host, port) alike."""
    out = []

    for entry in servers or ():
        if isinstance(entry, (tuple, list)):
            out.append((str(entry[0]), int(entry[1])))
        else:
            host, _, port = str(entry).partition(":")

            with contextlib.suppress(ValueError):
                out.append((host, int(port) if port else DEFAULT_STUN_PORT))

    return out


async def run_blocking(func, *args, **kwargs):
    """Run one of his blocking probes off the event loop."""
    loop = asyncio.get_running_loop()

    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


def _prepare(sock):
    """Switch a socket to blocking for his probes; return the previous state."""
    try:
        previous = sock.getblocking()
        sock.setblocking(True)
        return previous
    except Exception:
        return None


def _restore(sock, previous):
    if previous is None:
        return

    with contextlib.suppress(Exception):
        sock.setblocking(previous)


class NatProfile:
    """
    The result of one detection run, in the shape CraftLink already used.

    Ours, not his: his probes return tuples, and a lot of existing code (the
    engine, the GUI card, the tests) reads named attributes. Keeping the old
    shape means the port did not have to touch all of them -- and the values
    all come from his classifier.

    `nat_type` is a `punch.NatSub` and `filtering` a `punch.Filter`, so the
    callers that compare against those enums keep working while the strings
    underneath are his.
    """

    def __init__(self):
        self.nat_type = None          # punch.NatSub
        self.filtering = None         # punch.Filter
        self.public = None            # (ip, port)
        self.coarse = None            # his NAT_CONE / NAT_SYMMETRIC / NAT_UNKNOWN
        self.step = 0                 # his natDelta
        self.per_ip_pool = False
        self.mapped_ports = []
        self.servers = []             # (host, port, ok)
        self.errors = []
        self.label = ""               # his nat_label() output
        self.elapsed = 0.0
        #: Kept at None for the GUI card, which used to render a
        #: cross-socket port-allocation report here. His stack measures the
        #: same thing (through `mapped_ports` / `step` / `per_ip_pool`) and
        #: reports it as those fields, so nothing is missing -- the attribute
        #: just has to exist so the card's detail block does not crash.
        self.allocation = None
        #: How many servers were ASKED, and how big the candidate list was.
        #: `responded_servers` alone is misleading when the probe stops early
        #: by design (his `want=2`).
        self.asked = 0
        self.pool_size = 0

    # ---- compatibility surface used by the GUI card ----

    @property
    def probes(self):
        """Per-server results, so the card can list latency/failures."""
        out = []

        for host, port, mapped in self.servers:
            probe = _ServerLine(host, port, mapped)
            out.append(probe)

        return out

    @property
    def responded_servers(self):
        return sum(1 for _h, _p, mapped in self.servers if mapped)

    @property
    def observed_ports(self):
        return list(self.mapped_ports)

    @property
    def port_spread(self):
        if len(self.mapped_ports) < 2:
            return 0
        return max(self.mapped_ports) - min(self.mapped_ports)

    def summary_lines(self):
        lines = [
            f"NAT 类型         : {self.label or '未测出'}",
            f"公网映射         : "
            + (f"{self.public[0]}:{self.public[1]}" if self.public else "未知"),
            f"响应的 STUN 服务器: {self.responded_servers}",
        ]

        for host, port, mapped in self.servers:
            if mapped:
                lines.append(f"  · {host}:{port}: 正常（映射 {mapped[1]}）")
            else:
                lines.append(f"  · {host}:{port}: 无响应")

        for error in self.errors:
            lines.append(f"[!] {error}")

        return lines


class _ServerLine:
    """One server's row, shaped like the old ServerProbe for the GUI."""

    def __init__(self, host, port, mapped):
        self.server = f"{host}:{port}"
        self.mapped = mapped
        self.usable = mapped is not None
        self.rtt_ms = None
        self.error = "" if mapped else "没有收到响应"


def _to_subtype(value):
    from . import punch

    with contextlib.suppress(ValueError):
        return punch.NatSub(value)

    return punch.NatSub.UNKNOWN


def _to_filter(value):
    from . import punch

    with contextlib.suppress(ValueError):
        return punch.Filter(value)

    return punch.Filter.UNKNOWN


async def measure(sock, servers=None, timeout=RESPONSE_TIMEOUT, log=None,
                  third_n=None, want=2):
    """
    Full detection on `sock`, using his probes. Returns a NatProfile.

    `sock` MUST be the socket whose mapping matters (the punch socket).

    `want` = how many servers must ANSWER before probing stops. His default
    is 2, and it is the right default on the CONNECT path: two destinations
    are all the classifier needs, and every extra probe costs time the user
    is waiting.

    The DIAGNOSTICS path should pass a bigger number (see
    `detect_nat_type`): there the question is not "is my NAT punchable" but
    "which of my servers can I actually reach", and stopping at two makes a
    healthy network look broken -- it reports `2/7` because it only ever
    asked two, and a reader naturally concludes five servers are blocked.
    """
    started = time.monotonic()
    profile = NatProfile()

    pool = _as_tuples(servers) or default_servers()

    extra = {}
    previous = _prepare(sock)

    try:
        got = await run_blocking(
            stun_probe2, sock, timeout=timeout, servers=pool, hub=None,
            want=max(2, int(want)), log=log, third_n=third_n, extra=extra,
        )
    finally:
        _restore(sock, previous)

    if not got:
        # 这个提示必须点名端口：客户端第一、二台 STUN 就是我们自己的中继
        # 3478/3479。中继的 compose 只映射了其中一个端口（或服务器防火墙/
        # 安全组只放行了 TCP）时，表现就是这里 0/N —— 而界面上只看得出
        # "未测"，看不出"少开了一个 UDP 端口"。
        profile.coarse = NAT_UNKNOWN
        profile.errors.append(
            "所有 STUN 服务器都没有响应。请确认服务器的 UDP 3478 和 3479 "
            "**都**开着（compose 的两个端口都要映射，防火墙/安全组也要放行 "
            "UDP），本机网络也要允许 UDP 出站。"
        )
        profile.nat_type = _to_subtype("unknown")
        profile.filtering = _to_filter("unknown")
        profile.label = nat_label("unknown")
        profile.elapsed = time.monotonic() - started
        # Still record the shape of what we tried: the card must be able to say
        # "0/2 answered" instead of leaving the reader to guess.
        profile.servers = [(h, p, None) for h, p in pool]
        profile.asked = len(pool)
        profile.pool_size = len(pool)
        return profile

    ip, port, coarse, step, subtype = got[:5]

    profile.public = (ip, port)
    profile.coarse = coarse
    profile.step = step
    profile.per_ip_pool = bool(extra.get("per_ip_pool"))
    profile.mapped_ports = list(extra.get("sample_ports") or [port])
    profile.nat_type = _to_subtype(subtype)

    # ---- filtering (his third axis) ----
    #
    # Only worth asking a server that exists; his function reports
    # FILTER_UNKNOWN when the server ignores CHANGE-REQUEST, which is the
    # honest answer and NOT evidence of strict filtering.
    # ---- filtering (his third axis) ----
    #
    # HIS LOOP, not one shot at the first server.
    #
    # His `_probe_filtering_bg` walks the WHOLE server list inside a 4-second
    # budget and keeps the first verdict that is not `unknown`, because a single
    # server is allowed to ignore CHANGE-REQUEST (most do) and one "unknown"
    # says nothing about the network.
    #
    # We used to ask `pool[0]` exactly once -- and pool[0] is our OWN relay, the
    # one server we know does not implement CHANGE-REQUEST -- so the answer was
    # `unknown` every single time and the card could never show anything but
    # 入站未测. That is precisely why the owner never once saw a real 入站挡位 in
    # his own client, while his friend's client (which loops) showed 宽松/严格.
    if pool:
        filt = "unknown"
        deadline = time.monotonic() + FILTER_PROBE_BUDGET_S

        # ==================================================================
        #  ★ 过滤探测**必须用一个全新的 socket**，不能用打洞 socket
        # ==================================================================
        #
        # 用户的实测（2026-09-25）：他的网络是 NAT3（端口受限），我们报
        # NAT2，参考工具（mao.fan/mynat、比特彗星）报 NAT3 —— 我们错。
        #
        # 直接原因：打洞 socket 在"测映射"阶段已经往**我们中继的 3478 和
        # 3479 都发过** STUN 请求（默认探测表的前两台就是这两个端口）。
        # 对端口受限（apdf）的 NAT 来说，往 (服务器IP, 3479) 发过包 = 放行
        # 表里多了这一条 —— 于是"换端口"的应答照样进得来，我们就读成
        # "端口不限" = adf = NAT2。
        #
        # 换一个**全新的 socket**（只发过它自己要问的那一个端口）来测，
        # 端口轴才是干净的：apdf 会把换端口的应答丢掉 → 判出 apdf → NAT3。
        #
        # 过滤是 NAT 的整体行为，新 socket 的结论对打洞 socket 同样成立；
        # 而且这也正是参考实现和 RFC 5780 客户端工具的做法。
        #
        # 每台服务器单独一个 socket：同一趟里问完 A 的换端口、再去问 B，
        # 如果 B 恰好是 A 的"兄弟端口"，A 那一问就会把 B 暖掉。
        for host, port in pool:
            if time.monotonic() >= deadline - 0.1:
                break

            got = FILTER_UNKNOWN
            probe_sock = None

            try:
                probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                probe_sock.bind(("0.0.0.0", 0))
                saved = _prepare(probe_sock)

                try:
                    # 单次 1.5s（他的推荐区间 1.0–1.5s）：换过源的应答路径更长，
                    # 1.0s 在跨运营商/CGNAT 上会偶尔不够，而"没收到"会被读成
                    # 端口受限。总预算仍是 4s，不会拖慢启动。
                    got = await run_blocking(
                        stun_filtering, probe_sock, host, port,
                        timeout=1.5, hub=None, log=log, deadline=deadline,
                    )
                finally:
                    with contextlib.suppress(Exception):
                        _restore(probe_sock, saved)
            except OSError as e:
                if log:
                    with contextlib.suppress(Exception):
                        log(f"[NAT] filter probe socket failed: {e!r}")
            finally:
                if probe_sock is not None:
                    with contextlib.suppress(Exception):
                        probe_sock.close()

            if got and got != FILTER_UNKNOWN:
                filt = got
                break

        profile.filtering = _to_filter(filt)
    else:
        profile.filtering = _to_filter("unknown")

    # ---- per-server bookkeeping, for the diagnostics card ----
    #
    # USE WHAT THE PROBE REPORTED, do not guess. The previous version tried to
    # infer this from `mapped_ports` and got it badly wrong: it credited every
    # port to the first server, so a run where six servers answered was shown
    # as "1/7 responded" -- which reads like a broken network when the network
    # is fine. His probe knows exactly who answered; ask it.
    answered = extra.get("sample_results") or []

    if answered:
        profile.servers = [(h, p, m) for h, p, m in answered]
    else:
        # No detail available (older/partial path): fall back to the public
        # endpoint against the first server rather than inventing rows.
        for host, srv_port in pool:
            first = (host, srv_port) == (pool[0][0], pool[0][1])
            profile.servers.append(
                (host, srv_port, profile.public if first else None)
            )

    # 只有一个 IP 有响应：分类仍然给（他自己的单 IP 判定：cone/symmetric），
    # 但要在卡片上说清这个结论的边界，否则用户会拿它当"完全准确的 NAT 类型"。
    only_host = extra.get("single_ip_host") or ""

    if only_host and not profile.errors:
        profile.errors.append(
            f"只有一个 STUN 服务器 IP（{only_host}）有响应：映射类型按单 IP "
            "判定（能分出全锥/对称，但分不出「按目标 IP 分段分配」）。"
            "想要更准的结果，请确认服务器 UDP 3478 和 3479 都开着。"
        )

    profile.label = nat_label(coarse, subtype, profile.filtering.value)
    profile.elapsed = time.monotonic() - started

    # Which servers did we actually ASK? The card must say "问了 2 台" rather
    # than implying it tried the whole list and most of it failed.
    # 问过的台数：真跑过的那些（他能回答"问了 2 台、2 台都答了"），
    # 而不是"有回答的台数"——后者会把"没问"说成"没答"。
    asked_servers = extra.get("asked_servers") or []

    profile.asked = len(asked_servers) or len(profile.servers)
    profile.pool_size = len(pool)

    return profile


async def detect_nat_type(servers=None, timeout=RESPONSE_TIMEOUT,
                        want=4, **kwargs):
    """
    Compatibility entry point: opens a throwaway socket and measures.

    Prefer `measure(sock)` with the punch socket: a mapping belongs to the
    LOCAL PORT, so a throwaway socket measures a different mapping from the
    one the punch will have. This exists for the diagnostics path, which runs
    before any punch socket exists.

    `want=4` by default HERE (his punch path uses 2): this is the "which of my
    servers work" question, and stopping at two makes the card report `2/7`
    on a network where six servers answer.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        sock.bind(("0.0.0.0", 0))
        return await measure(
            sock, servers=servers, timeout=timeout, want=want, **kwargs
        )
    finally:
        with contextlib.suppress(OSError):
            sock.close()
