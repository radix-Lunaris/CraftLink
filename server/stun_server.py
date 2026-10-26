"""
CraftLink relay -- built-in STUN responder.

============================================================================
  WHERE THIS COMES FROM
============================================================================

This is a VERBATIM port of the STUN responder from **MCLanP2P**
(`server/server.py`), the project whose hole punching is verified working on
real NAT3/NAT4 networks. CraftLink's own earlier STUN responder was removed
on the project owner's instruction: his is the proven one, and keeping a
half-finished second implementation alongside it only creates drift.

Only the names were adjusted to this module's conventions:

  * `log.info/warning`  -> the `_log` helper below
  * `STUN_PORT/STUN_PORT2` -> `STUN_PORTS` (read from relay_config)

Nothing about the STUN behaviour itself was touched: the wire format, the
XOR-MAPPED-ADDRESS encoding, the per-source-IP rate limiter and the
"STUN failure must never stop the relay" rule are all his.

============================================================================
  DELIBERATE LIMITS (his, unchanged, and worth understanding)
============================================================================

Only the Binding Request is implemented. There is NO CHANGE-REQUEST support,
so this server cannot answer an RFC 5780 behaviour test; a client that asks
for one gets silence, and his client correctly reports "filtering unknown"
rather than inventing an answer. That is why NAT type is inferred by
comparing two bindings against two different ports instead -- which is
exactly why there are TWO ports.

Two UDP ports on purpose: detecting a symmetric NAT needs two DIFFERENT
destinations, and a single-IP VPS can only offer that by varying the port.

============================================================================
  SECURITY: this is an unauthenticated UDP port
============================================================================

A 20-byte request produces a ~56-byte response, so a bare STUN port is a
~2.8x amplification reflector. Requiring the magic cookie raises the bar but
is not a defence. The per-source token bucket below is what keeps this port
from being useful in a reflection attack, and it is the reason a "few probes
per session" client is unaffected while an amplifier is throttled hard.
"""

import asyncio
import contextlib
import os
import socket
import struct
import time

from relay_config import STUN_ENABLED, STUN_PORTS, STUN_HOST


def _log(message):
    """Same best-effort logging the relay uses; STUN must never raise."""
    try:
        from relay_log import log_line
        log_line(message)
    except Exception:
        print(message, flush=True)


STUN_MAGIC = b"\x21\x12\xa4\x42"
STUN_BINDING_REQUEST = 0x0001
STUN_BINDING_RESPONSE = 0x0101
STUN_BINDING_ERROR = 0x0111
STUN_ATTR_MAPPED_ADDRESS = 0x0001
STUN_ATTR_XOR_MAPPED_ADDRESS = 0x0020
#: RFC 5780：这条应答实际从哪来的 / 我的备用地址（换端口应答靠它自证）。
STUN_ATTR_RESPONSE_ORIGIN = 0x802B
STUN_ATTR_OTHER_ADDRESS = 0x802C
STUN_ATTR_SOFTWARE = 0x8022

# ---------------------------------------------------------------------------
#  RFC 5780 CHANGE-REQUEST —— 我们只实现「换端口」那一半
# ---------------------------------------------------------------------------
#
# 客户端的过滤行为分类器要问三件事，第三件是「请从同一个地址的**另一个端口**
# 回我」；这一件才能把「入站宽松（谁都能进）」和「入站中等（只有我们发过包的
# 地址能进）」分开 —— 也就是客户端屏幕上那一格"入站挡位"唯一的来源。
#
# **我们能做，而且不用第二个公网 IP**：这个服务本来就同时监听 3478 和 3479，
# 让其中一个端口用另一个端口把包发回去就行。
#
# 「换 IP」那一半做不到（VPS 只有一个公网 IP），客户端对此是诚实的：它会看到
# 地址没变、于是不判 eif，落到端口那一项上。他原版服务端两半都不做（他源码
# 注释：客户端靠比较两次绑定来推断），结果是客户端那一格永远是"入站未测"。
STUN_ATTR_CHANGE_REQUEST = 0x0003
CHANGE_PORT_BIT = 0x01
CHANGE_IP_BIT = 0x02

#: 所有已绑定的 STUN transport，好让某个端口能用**另一个端口**代答。
_TRANSPORTS = []


def _wants_change_port(body):
    """
    请求里有没有带 CHANGE-REQUEST、而且要求「换端口」？

    `body` 是 20 字节头之后的部分。属性按 4 字节对齐，长度不足或截断一律当作
    "没要求" —— 对畸形包最安全的回答就是照常从本端口回。
    """
    offset = 0
    end = len(body)

    while offset + 4 <= end:
        atype, alen = struct.unpack(">HH", body[offset:offset + 4])
        value_at = offset + 4

        if value_at + alen > end:
            return False

        if atype == STUN_ATTR_CHANGE_REQUEST and alen >= 4:
            flags = struct.unpack(">I", body[value_at:value_at + 4])[0]
            return bool(flags & CHANGE_PORT_BIT)

        offset = value_at + alen + ((4 - alen % 4) % 4)

    return False


def _sibling_transport(mine):
    """另一个监听在不同端口上的 STUN transport（没有就 None）。"""
    try:
        my_port = mine.get_extra_info("socket").getsockname()[1]
    except Exception:
        return None

    for transport in list(_TRANSPORTS):
        if transport is mine:
            continue

        try:
            if transport.get_extra_info("socket").getsockname()[1] != my_port:
                return transport
        except Exception:
            continue

    return None




def _stun_attr(kind, value):
    return struct.pack(">HH", kind, len(value)) + value


def stun_binding_response(txid, addr, origin=None, other=None):
    """
    Build a success response carrying the peer's public endpoint.

    `origin` / `other` implement the two RFC 5780 attributes a client needs to
    tell "the server really answered from somewhere else" apart from "the
    server ignored CHANGE-REQUEST and answered from where it always does":

        RESPONSE-ORIGIN (0x802B) = where this answer actually came from
        OTHER-ADDRESS   (0x802C) = the server's alternate address/port

    手册（朋友的《入站过滤检测流程》§6）把这条写成硬要求：**只实现"换端口"、
    不回 RESPONSE-ORIGIN，会让客户端分不清"换了"和"没换"** —— 而分不清的
    后果就是所有人都被判成严或宽，而不是 `unknown`。我们的客户端两种证据都
    会看（RESPONSE-ORIGIN 优先，其次看数据报真实源地址），所以加上它以后，
    日志里能直接引用服务器**自己声明**的来源。
    """
    ip, port = addr[0], addr[1]
    try:
        packed = socket.inet_pton(socket.AF_INET, ip)
        family = 0x01
    except (OSError, ValueError):
        # IPv6 client: report it back verbatim
        try:
            packed = socket.inet_pton(socket.AF_INET6, ip)
            family = 0x02
        except (OSError, ValueError):
            return _stun_error(txid, 400)
    mapped = struct.pack(">BBH", 0, family, port) + packed

    # XOR-MAPPED-ADDRESS: obfuscated so a NAT that rewrites the payload
    # cannot mangle it. Port XORs with the top half of the cookie.
    xport = port ^ 0x2112
    xpacked = bytes(packed[i] ^ STUN_MAGIC[i] for i in range(len(packed)))
    xmapped = struct.pack(">BBH", 0, family, xport) + xpacked

    body = (_stun_attr(STUN_ATTR_MAPPED_ADDRESS, mapped)
            + _stun_attr(STUN_ATTR_XOR_MAPPED_ADDRESS, xmapped)
            + _stun_attr(STUN_ATTR_SOFTWARE, b"mclanp2p"))

    for kind, value in ((STUN_ATTR_RESPONSE_ORIGIN, origin),
                        (STUN_ATTR_OTHER_ADDRESS, other)):
        if not value:
            continue

        try:
            oip, oport = value[0], int(value[1])
            opacked = socket.inet_pton(socket.AF_INET, oip)
        except (OSError, ValueError, TypeError, IndexError):
            continue

        body += _stun_attr(kind, struct.pack(">BBH", 0, 0x01, oport) + opacked)

    return (struct.pack(">HH", STUN_BINDING_RESPONSE, len(body))
            + STUN_MAGIC + txid + body)


def _stun_error(txid, code):
    reason = b"bad request"
    body = struct.pack(">HHI", 0, code, 0)[:4] + reason
    body = struct.pack(">HH", 0x0009, len(body)) + body
    return (struct.pack(">HH", STUN_BINDING_ERROR, len(body))
            + STUN_MAGIC + txid + body)


class StunServerProtocol(asyncio.DatagramProtocol):
    """Answers STUN binding requests with the observed source address.

    Rate limited per source IP.
    ---------------------------
    A 20-byte binding request produces a ~56-byte response, so an
    unauthenticated STUN port is a ~2.8x amplification reflector. It needs
    a valid magic cookie, which raises the bar, but that is not a defence --
    it is a minor inconvenience. Capping per-source traffic keeps this port
    from being useful in a reflection attack.
    """
    # generous for real clients (a handful of probes per session), tiny for
    # an amplifier that needs sustained throughput
    REFILL_PER_SEC = 5.0
    BURST = 20.0

    # ------------------------------------------------------------------
    # ★ v3.9.16：CHANGE-REQUEST 单独一份、更紧的配额
    # ------------------------------------------------------------------
    #
    # 一次换端口请求会**占用两个端口各一份**应答（本端口决定、兄弟端口代答），
    # 也就是说它的代价是普通请求的两倍，而真实客户端一次 NAT 画像只发**个位数**
    # 个换端口请求（一次探测一个，且只发一轮）。
    #
    # 所以给它一份独立的、明显更紧的令牌桶：正常打洞完全穿得过去，而"拿
    # CHANGE-REQUEST 当放大器"的玩法会被掐在每秒几次这个量级上。
    #
    # 设 `MCLINK_STUN_CHANGE_REFILL=0` 可以关掉这条额外限制（退回 v3.9.15
    # 只靠总配额的行为）。
    CHANGE_REFILL_PER_SEC = float(
        os.environ.get("MCLINK_STUN_CHANGE_REFILL", "1.0") or 0
    )
    CHANGE_BURST = float(
        os.environ.get("MCLINK_STUN_CHANGE_BURST", "8") or 0
    )

    def __init__(self, name=""):
        self.name = name
        self.transport = None
        self._budget = {}          # ip -> [tokens, last_update]
        self._last_gc = 0.0
        #: v3.9.16：CHANGE-REQUEST 的独立配额表（同一张表的清理逻辑复用）。
        self._change_budget = {}

    def _allow_change(self, ip):
        """
        换端口请求单独的令牌桶（v3.9.16）。

        返回 True = 放行。**不扣**总配额之外的东西：调用方仍然会走 `_allow()`，
        所以这里是"两道闸都要过"。配额被关掉（<=0）时恒为 True。
        """
        if self.CHANGE_REFILL_PER_SEC <= 0 or self.CHANGE_BURST <= 0:
            return True

        now = time.monotonic()
        tok, last = self._change_budget.get(ip, (self.CHANGE_BURST, now))
        tok = min(
            self.CHANGE_BURST, tok + (now - last) * self.CHANGE_REFILL_PER_SEC
        )
        self._change_budget[ip] = [tok, now]

        if tok < 1.0:
            return False

        self._change_budget[ip][0] = tok - 1.0

        # 同一张表也不能无限长：它按源 IP 记账，而 UDP 源 IP 可伪造。
        if len(self._change_budget) > 4096:
            self._change_budget = {
                k: v for k, v in self._change_budget.items()
                if v[0] > 0 or now - v[1] < 60.0
            }

            if len(self._change_budget) > 4096:
                self._change_budget.clear()

        return True

    def _allow(self, ip):
        now = time.monotonic()
        tok, last = self._budget.get(ip, (self.BURST, now))
        tok = min(self.BURST, tok + (now - last) * self.REFILL_PER_SEC)
        self._budget[ip] = [tok, now]
        if tok < 1.0:
            return False
        self._budget[ip][0] = tok - 1.0
        # keep the table bounded
        if len(self._budget) > 4096 and now - self._last_gc > 60.0:
            self._last_gc = now
            self._budget = {k: v for k, v in self._budget.items()
                            if v[0] > 0 or now - v[1] < 60.0}
            if len(self._budget) > 4096:
                self._budget.clear()
        return True

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info("socket")
        try:
            addr = sock.getsockname()
            _log("STUN 监听 udp %s:%s（%s）" % (addr[0], addr[1], self.name))
        except Exception:
            _log("STUN socket 就绪（%s）" % (self.name,))

    def datagram_received(self, data, addr):
        # 20-byte header: type(2) length(2) cookie(4) txid(12)
        if len(data) < 20:
            return
        kind, length = struct.unpack(">HH", data[:4])
        if kind != STUN_BINDING_REQUEST:
            return
        if data[4:8] != STUN_MAGIC:
            return
        if length != len(data) - 20:
            # A length mismatch means we cannot trust the framing; replying
            # to a malformed packet is how you build an amplification vector.
            return
        if not self._allow(addr[0]):
            return

        # 先决定"这条应答从哪个端口出去"，再据此填 RESPONSE-ORIGIN ——
        # 客户端就是靠它区分"服务器真的换了地方"和"服务器没理我"。
        transport = self.transport

        # RFC 5780「换端口」：用另一个端口把应答发回去。
        #
        # 这一条是客户端屏幕上"入站挡位"唯一可能得出真实结论的路径 —— 见文件
        # 上方 STUN_ATTR_CHANGE_REQUEST 的说明。**另一个端口的每源 IP 令牌桶也
        # 要扣**，否则一个换端口请求就能从两个端口各拿一份应答，放大倍数翻倍。
        if _wants_change_port(data[20:]):
            other = _sibling_transport(self.transport)

            if other is not None:
                proto = other.get_protocol()
                allowed = getattr(proto, "_allow", None)

                # ★ v3.9.16：多过一道闸 —— 换端口请求还要过它自己的配额。
                # 被挡下时**不报错、不回包**：对放大器最省事的回答就是没有回答。
                allowed_change = getattr(self, "_allow_change", None)

                if allowed_change is not None and not allowed_change(addr[0]):
                    return

                if allowed is None or allowed(addr[0]):
                    transport = other

        # RESPONSE-ORIGIN = 这条应答**实际**的源地址；OTHER-ADDRESS = 另一个端口。
        #
        # 只有**绑定到具体地址**时才写这两个属性。容器/线上通常绑 0.0.0.0，
        # 这时 sockname 是通配地址 —— 把它当成"来源"报给客户端是**撒谎**：
        # 客户端（正确地）看到 origin=0.0.0.0 ≠ 服务器地址，会以为"服务器从
        # 另一个 IP 答了" → 判成 eif（宽松）。实测就踩过这一脚。
        #
        # 手册的原则一样：没有证据就不许声称。宁可**不发**这个属性，让客户端
        # 用数据报的真实源地址（那才是它 NAT 看到的东西）去判断。
        origin = None
        sibling = None

        for target, is_origin in ((transport, True),
                                  (_sibling_transport(self.transport), False)):
            if target is None:
                continue

            try:
                name = target.get_extra_info("sockname")
                pair = (name[0], name[1])
            except Exception:
                continue

            if not pair[0] or pair[0] in ("0.0.0.0", "::", ""):
                continue        # 通配地址：不作任何声称

            if is_origin:
                origin = pair
            else:
                sibling = pair

        response = stun_binding_response(data[8:20], addr,
                                         origin=origin, other=sibling)

        try:
            transport.sendto(response, addr)
        except Exception:
            pass

    def error_received(self, exc):
        _log("STUN error: %s" % (exc,))



async def start_stun():
    """Bind the STUN sockets. Returns (transports, [host, port], [host, port2]).

    Failure here must never stop the signalling server -- STUN is an
    optimisation, not a dependency.
    """
    if not STUN_ENABLED:
        _log("内置 STUN 已禁用（STUN_ENABLED=0）")
        return [], None, None

    loop = asyncio.get_running_loop()
    transports = []
    for port in STUN_PORTS:
        if not port:
            continue
        try:
            tr, _pr = await loop.create_datagram_endpoint(
                lambda p=port: StunServerProtocol("udp/%d" % p),
                # 绑定地址跟着配置走（默认 0.0.0.0 = 线上行为不变）。
                # 绑到具体地址时，RESPONSE-ORIGIN 才有意义 —— 见下面那段。
                local_addr=((STUN_HOST or "0.0.0.0"), port))
            transports.append(tr)
        except OSError as e:
            _log("无法绑定 STUN udp/%d：%s（是不是别的程序占了？）" % (port, e))
        except Exception as e:
            _log("STUN udp/%d 启动失败：%s" % (port, e))

    if not transports:
        _log("内置 STUN 不可用：两个端口都没绑上")
        return [], None, None

    # 登记给 `_sibling_transport` 用：某个端口要替另一个端口回包（RFC 5780
    # 换端口）。只在两个端口都绑上时才有意义，所以放在这里、而不是每个
    # endpoint 创建时。
    _TRANSPORTS[:] = transports

    return (
        transports,
        STUN_PORTS[0],
        STUN_PORTS[1] if len(STUN_PORTS) > 1 else None,
    )


# ============================================================
# Relay integration (OURS -- the STUN logic above is his)
# ============================================================
#
# The relay process holds the STUN listeners and needs four things from this
# module. None of them are STUN: they are the glue between his responder and
# CraftLink's control plane, so they are written for this project.
#
# Everything above this line is a verbatim port and must stay that way.

#: The running listeners. Set by start_stun_server().
_server = None


async def start_stun_server():
    """
    Bind the STUN listeners. Returns True when at least one port is up.

    Failure is NEVER fatal: STUN is an optimisation, and the relay must keep
    serving the frozen TCP data path even when UDP is unavailable or blocked
    by the cloud security group. Clients fall back to public STUN servers on
    their own (see the client's stun_order()).
    """
    global _server

    if not STUN_ENABLED:
        _log("内置 STUN 已禁用（MCLINK_STUN_ENABLED=0）")
        return False

    transports, port, port2 = await start_stun()

    if not transports:
        return False

    _server = {
        "transports": transports,
        "port": port,
        "port2": port2,
    }

    _log(
        "STUN 服务已启动：udp://%s（%s 个端口%s）"
        % (
            port,
            len(transports),
            "，备用 %s" % port2 if port2 else "",
        )
    )

    return True


async def stop_stun_server():
    """Close the listeners. Idempotent and never raises."""
    global _server

    if _server is None:
        return

    for transport in _server.get("transports") or ():
        with contextlib.suppress(Exception):
            # abort(), not close(): a datagram transport has nothing worth
            # flushing, and abort() releases the socket immediately. On
            # Windows that difference decides whether a restart can rebind
            # the same port straight away.
            transport.abort()

    _server = None


def stun_snapshot():
    """
    Stats for the admin API / the status file. None when not running.

    Only the counters the relay actually reports: this responder has no
    CHANGE-REQUEST support, so there is nothing to say about ip/port changes.
    """
    if _server is None:
        return None

    return {
        "enabled": True,
        "listen": "udp://%s" % _server.get("port"),
        "alt": (
            "udp://%s" % _server["port2"] if _server.get("port2") else ""
        ),
        "ports": [p for p in (_server.get("port"), _server.get("port2")) if p],
    }


def probe_client(target, requester=("", 0)):
    """
    Fire ONE datagram at a client from a socket that has never spoken to it.

    This answers a question the punch path cannot answer on its own:
    "I received nothing from the peer" means EITHER the peer's packets were
    dropped OR our own mapping is unreachable, and those have opposite
    responses. A datagram from a brand-new source port is exactly what the
    peer's first punch packet looks like to the client's NAT.

    Returns True when the probe was actually sent. The caller MUST report
    that distinction: without it the client cannot tell "the probe did not
    arrive" from "this relay never fired one", and the ported project spent
    several rounds reporting the second as the first.
    """
    if _server is None or target is None:
        return False

    payload = b"MCLINK-INGRESS-PROBE"
    family = socket.AF_INET6 if ":" in str(target[0]) else socket.AF_INET
    bind = ("::" if family == socket.AF_INET6 else "0.0.0.0", 0)

    try:
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(bind)
    except OSError as e:
        _log("入站探测 socket 建立失败：%s" % (e,))
        return False

    try:
        sock.sendto(payload, tuple(target))
    except OSError as e:
        _log("入站探测发送失败 -> %s：%s" % (target, e))
        return False
    finally:
        with contextlib.suppress(OSError):
            sock.close()

    _log(
        "入站探测：已从全新 socket 向 %s:%s 发送（请求者 %s）"
        % (target[0], target[1], requester[0] if requester else "?")
    )

    return True
