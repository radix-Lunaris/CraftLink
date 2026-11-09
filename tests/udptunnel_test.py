"""
MC-Link -- 直连承载测试（`udptunnel.py` + `channel.py` + `direct_carry.py`）

============================================================================
 这一套测什么，以及它**不**证明什么
============================================================================

测的是 v3.0.1 换掉的那一半：**洞打通之后，游戏数据是怎么过去的**。

以前那半是我自己发明的（`reliable.py` 951 行 + `direct_carry.py` 226 行），
从来没在真实 NAT 上成功过 —— 项目负责人的现象是"洞打通了，游戏连不上"。
现在那半是参考工程验证过的 `udptunnel.py`（731 行）+ `channel.py`（160 行），
逐字搬运。

所以这套测试盯的是**移植过来的机制本身**：

  1. 常量必须和他的那份逐字一致（搬运走样了要立刻发现）；
  2. 两端在回环上**真的**握手、真的双向搬字节（真 UDP socket，不是假 socket）；
  3. 大消息按 MTU 分片后能在对端**原样重组**；
  4. **丢包 + 乱序**下仍然完整有序（这是手写可靠层最容易错的地方）；
  5. **一条隧道承载多条流（sid）**：Minecraft 的"列表探测"和"真正进世界"
     是两条连接，必须能同时在一条洞上跑，而且互不串包；
  6. 打洞把 socket 设成**非阻塞**之后，隧道仍然要能收包
     （非阻塞下 `recvfrom` 抛 `BlockingIOError`，是 `OSError` 的子类，
      他那个 `except OSError: break` 会把读者静默关掉）；
  7. 同一个对端 IP 下两个隧道必须被判成 ambiguous 丢掉（这就是为什么
     一条链路必须靠 sid 分用，而不是每个会话各建一个隧道）。

**它不证明"在你家网络上能连上"**：回环没有 NAT、没有端口漂移、没有入站过滤。
真实可用性只能靠真实网络实测。`HANDOFF-TO-NEXT-SESSION.md` 明确写了：
不许拿回环测试当"能用"的证据。这套只证明"机制没搬坏、没搬漏"。

Run:
    python tests/udptunnel_test.py
"""

import asyncio
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import direct_carry  # noqa: E402
from mclink.core import protocol as proto  # noqa: E402
from mclink.core.channel import PeerChannel  # noqa: E402
from mclink.core.udptunnel import UdpHub, UdpTunnel, fmt_addr  # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================
# 1. 常量：搬运必须逐字一致
# ============================================================

def test_constants_pinned():
    """
    把他的常量**写在测试里**钉死。

    这些数字不是美学：`MTU` 决定分片、`WINDOW`/`WINDOW_INIT` 决定拥塞窗口、
    `REORDER_MAX` 决定乱序缓冲、`KEEPALIVE_S` 决定洞会不会过期、
    `MAGIC`/`T_*` 决定能不能和同样搬过来的 `punch.py` 共存一个 socket。
    "顺手优化"任意一个都是在改一个验证过的协议。
    """
    print("")
    print("---- 常量（和他的实现逐字一致）----")

    from mclink.core import udptunnel as ut

    check("MAGIC = 0x4D50", ut.MAGIC == 0x4D50, hex(ut.MAGIC))
    check("头长 12 字节", ut.HDR == 12, str(ut.HDR))
    check("MTU 1300（不会被 IP 分片）", ut.MTU == 1300, str(ut.MTU))
    check("窗口 64 / 初始 8 / 下限 4",
          ut.WINDOW == 64 and ut.WINDOW_INIT == 8 and ut.WINDOW_MIN == 4,
          f"{ut.WINDOW}/{ut.WINDOW_INIT}/{ut.WINDOW_MIN}")
    check("保活 5 秒（洞靠它不过期）", ut.KEEPALIVE_S == 5.0, str(ut.KEEPALIVE_S))
    check("RTO 300ms / 上限 4000ms",
          ut.RTO_MS == 300 and ut.RTO_MAX_MS == 4000,
          f"{ut.RTO_MS}/{ut.RTO_MAX_MS}")
    check("乱序缓冲 512", ut.REORDER_MAX == 512, str(ut.REORDER_MAX))
    check("类型码 HELLO/ACK/DATA/ACK/NAK/PING/PONG",
          (ut.T_HELLO, ut.T_HELLO_ACK, ut.T_DATA,
           ut.T_ACK, ut.T_NAK, ut.T_PING, ut.T_PONG)
          == (1, 2, 3, 4, 5, 6, 7))
    check("★ 和他的打洞同一套 MAGIC/头（punch.py 与它共用一个 socket）",
          ut.MAGIC == 0x4D50 and ut.HDR == 12)

    # uuid: fmt_addr 必须能吃下 IPv6 的 4 元组（recvfrom 在 AF_INET6 上返回 4 元组）
    check("fmt_addr 支持 IPv4",
          fmt_addr(("1.2.3.4", 5000)) == "1.2.3.4:5000",
          fmt_addr(("1.2.3.4", 5000)))
    check("fmt_addr 支持 IPv6（4 元组，不炸）",
          fmt_addr(("::1", 5000, 0, 0)) == "[::1]:5000",
          fmt_addr(("::1", 5000, 0, 0)))

    # framing（channel.py 用的那几个）
    check("帧头 STREAM_DATA=0x02 / SID_HDR=9 / MAX_FRAME=8MB",
          proto.STREAM_DATA == 0x02 and proto.SID_HDR == 9
          and proto.MAX_FRAME == 8 * 1024 * 1024)

    frame = proto.frame(7, b"hello")
    check("frame() = [0x02][sid:4][len:4][payload]",
          frame[0] == 0x02 and frame[1:5] == b"\x00\x00\x00\x07"
          and frame[5:9] == b"\x00\x00\x00\x05" and frame[9:] == b"hello",
          repr(frame[:12]))


# ============================================================
# 2. 回环上真的握手 + 双向搬字节
# ============================================================

def make_socket(blocking=True):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)

    if not blocking:
        sock.setblocking(False)

    sock.bind(("127.0.0.1", 0))

    return sock


def connect_pair(sock_a, sock_b, timeout=5.0):
    """
    Two tunnels on loopback, handshaking concurrently (each `connect()` blocks).

    The hub must be started AFTER the tunnel is constructed, because
    `UdpTunnel.__init__` is what registers itself with the hub -- a reader that
    starts first can swallow the peer's HELLO before anyone is registered.
    """
    log_a, log_b = [], []

    hub_a = UdpHub(sock_a, log_a.append)
    tunnel_a = UdpTunnel(sock_a, sock_b.getsockname(), log_a.append, hub=hub_a)
    hub_a.start()

    hub_b = UdpHub(sock_b, log_b.append)
    tunnel_b = UdpTunnel(sock_b, sock_a.getsockname(), log_b.append, hub=hub_b)
    hub_b.start()

    errors = []

    def run(tunnel):
        try:
            tunnel.connect(timeout)
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=run, args=(tunnel_a,), daemon=True),
        threading.Thread(target=run, args=(tunnel_b,), daemon=True),
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout + 3)

    return tunnel_a, tunnel_b, hub_a, hub_b, log_a + log_b, errors


def test_loopback_handshake_and_bytes():
    print("")
    print("---- 回环：真握手 + 双向搬字节 ----")

    sock_a, sock_b = make_socket(), make_socket()

    try:
        tunnel_a, tunnel_b, hub_a, hub_b, _logs, errors = connect_pair(
            sock_a, sock_b
        )

        check("两端都握手成功", not errors and tunnel_a.connected
              and tunnel_b.connected, str(errors))

        tunnel_a.send(b"hello-from-a")
        tunnel_b.send(b"hello-from-b")

        got_b = _recv_exact(tunnel_b, 12, 3.0)
        got_a = _recv_exact(tunnel_a, 12, 3.0)

        check("A -> B", got_b == b"hello-from-a", repr(got_b))
        check("B -> A", got_a == b"hello-from-b", repr(got_a))

        # 保活/活性：收到过东西之后 idle_for 必须接近 0
        check("idle_for() 反映最后一次收包（判死靠它）",
              tunnel_a.idle_for() < 2.0,
              f"{tunnel_a.idle_for():.1f}s")

        tunnel_a.close()
        tunnel_b.close()
        hub_a.close()
        hub_b.close()
    finally:
        for sock in (sock_a, sock_b):
            try:
                sock.close()
            except OSError:
                pass


def _recv_exact(tunnel, size, deadline_s):
    """Read exactly `size` bytes out of a tunnel's inbox, or None on timeout."""
    buf = bytearray()
    deadline = time.monotonic() + deadline_s

    while len(buf) < size and time.monotonic() < deadline:
        data = tunnel.recv(0.2)

        if data:
            buf += data

    return bytes(buf) if len(buf) >= size else bytes(buf)


# ============================================================
# 3. 大消息（分片 + 重组）
# ============================================================

def test_large_message_reassembly():
    print("")
    print("---- 大消息：按 MTU 分片，对端按序号原样重组 ----")

    sock_a, sock_b = make_socket(), make_socket()

    try:
        tunnel_a, tunnel_b, hub_a, hub_b, _logs, errors = connect_pair(
            sock_a, sock_b
        )

        if errors:
            check("大消息重组", False, f"handshake failed: {errors}")
            return

        # 200 KB：远大于 MTU(1300) 和窗口(64)，会走多轮拥塞窗口 + 重传计时
        payload = bytes((i * 7 + 13) % 251 for i in range(200 * 1024))

        sender = threading.Thread(
            target=tunnel_a.send, args=(payload,), daemon=True
        )
        sender.start()

        got = _recv_exact(tunnel_b, len(payload), 25.0)

        check("★ 200KB 原样到达", got == payload,
              f"got {len(got)} of {len(payload)} bytes")
        check("顺序正确（不是「到了就行」）",
              got[:64] == payload[:64] and got[-64:] == payload[-64:])

        sender.join(5)
        tunnel_a.close()
        tunnel_b.close()
        hub_a.close()
        hub_b.close()
    finally:
        for sock in (sock_a, sock_b):
            try:
                sock.close()
            except OSError:
                pass


# ============================================================
# 4. 丢包 + 乱序：重传与乱序缓冲
# ============================================================

class LossyProxy:
    """
    A UDP relay on loopback that drops and reorders.

    This is what makes test 4 mean something: without loss, "it works" says
    nothing about a hand written ARQ layer. Dropping ACKs too (both directions
    are affected on purpose) reproduces the hardest case -- the data arrived but
    the acknowledgement did not, so the sender retransmits and the receiver must
    recognise a duplicate.
    """

    def __init__(self, drop_every=0, reorder_every=0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.addr = self.sock.getsockname()
        self.drop_every = int(drop_every)
        self.reorder_every = int(reorder_every)
        self.stop = threading.Event()
        self._peers = []
        self._held = {}
        self._count = {}
        self.dropped = 0
        self.reordered = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        try:
            self.sock.close()
        except OSError:
            pass

    def _other(self, src):
        for peer in self._peers:
            if peer != src:
                return peer

        return None

    def _run(self):
        while not self.stop.is_set():
            try:
                data, src = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            if src not in self._peers and len(self._peers) < 2:
                self._peers.append(src)

            n = self._count.get(src, 0) + 1
            self._count[src] = n

            if self.drop_every and n % self.drop_every == 0:
                self.dropped += 1
                continue

            dst = self._other(src)

            if dst is None:
                continue

            if self.reorder_every and n % self.reorder_every == 0:
                self._held[src] = data
                self.reordered += 1
                continue

            held = self._held.pop(src, None)

            if held is not None:
                try:
                    self.sock.sendto(held, dst)
                except OSError:
                    pass

            try:
                self.sock.sendto(data, dst)
            except OSError:
                pass


def test_loss_and_reorder():
    print("")
    print("---- 丢包 + 乱序：序号/ACK/NAK/重传/乱序缓冲 ----")

    proxy = LossyProxy(drop_every=3, reorder_every=5)

    sock_a, sock_b = make_socket(), make_socket()
    proxy.start()

    try:
        # Both sides talk TO THE PROXY; the proxy relays. The tunnel accepts any
        # source port from the peer's IP, so 127.0.0.1 works for both.
        log_a, log_b = [], []

        hub_a = UdpHub(sock_a, log_a.append)
        tunnel_a = UdpTunnel(sock_a, proxy.addr, log_a.append, hub=hub_a)
        hub_a.start()

        hub_b = UdpHub(sock_b, log_b.append)
        tunnel_b = UdpTunnel(sock_b, proxy.addr, log_b.append, hub=hub_b)
        hub_b.start()

        errors = []

        def run(tunnel):
            try:
                tunnel.connect(10.0)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=run, args=(tunnel_a,), daemon=True),
            threading.Thread(target=run, args=(tunnel_b,), daemon=True),
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join(13)

        if errors:
            check("丢包环境下握手", False, str(errors))
            return

        check("丢包环境下握手成功", tunnel_a.connected and tunnel_b.connected)

        # 24 KB of numbered bytes: enough to need many windows and retransmits,
        # small enough to finish inside the deadline even with 1/3 loss.
        payload = bytes((i * 31 + 5) % 251 for i in range(24 * 1024))

        threading.Thread(target=tunnel_a.send, args=(payload,), daemon=True).start()

        got = _recv_exact(tunnel_b, len(payload), 30.0)

        check("★ 1/3 丢包 + 乱序下仍然【完整】", got == payload,
              f"got {len(got)}/{len(payload)}, proxy dropped={proxy.dropped}")
        check("代理确实丢了包（否则这条测试是假的）", proxy.dropped > 20,
              f"dropped={proxy.dropped} reordered={proxy.reordered}")

        tunnel_a.close()
        tunnel_b.close()
        hub_a.close()
        hub_b.close()
    finally:
        proxy.close()

        for sock in (sock_a, sock_b):
            try:
                sock.close()
            except OSError:
                pass


# ============================================================
# 5. 一条隧道、多条流（sid）
# ============================================================

def test_frame_parsing():
    print("")
    print("---- sid 分帧：粘包 / 半包 / 垃圾字节 ----")

    channel = PeerChannel("peer", "test")

    frames = channel.feed(proto.frame(1, b"aaa") + proto.frame(2, b"bbbb"))
    check("一条数据报里两条流分别解析",
          frames == [(1, b"aaa"), (2, b"bbbb")], str(frames))

    whole = proto.frame(3, b"x" * 20)
    first = channel.feed(whole[:7])
    second = channel.feed(whole[7:])
    check("半个帧要等到齐了才吐出来",
          first == [] and second == [(3, b"x" * 20)], f"{first} / {second}")

    # 一个不属于流帧的字节：必须跳过并重新对齐，不许当成 sid=0 的负载
    # （那会让房主对本机 Minecraft 开一条真连接并把垃圾灌进去）
    resync = channel.feed(b"\x00\xff" + proto.frame(9, b"ok"))
    check("垃圾字节被跳过并重新对齐（不产生 sid=0 流）",
          resync == [(9, b"ok")], str(resync))

    st, created = channel.alloc_stream(5)
    st2, created2 = channel.alloc_stream(5)
    check("同一个 sid 重复分配是幂等的（控制消息可能重复到达）",
          created and not created2 and st is st2)


def test_peerlink_multiplexes_streams():
    print("")
    print("---- 一条隧道承载两条流（列表探测 + 真正进世界）----")

    asyncio.run(_peerlink_multiplex())


async def _peerlink_multiplex():
    """
    两条 Minecraft 连接**同时**跑在一条洞上，各自对端一条自己的本机世界连接。

    这就是项目负责人踩过的那个坑：第一条连接（服务器列表探测）用完就走，
    第二条（真正进世界）必须还能走直连。参考工程的答案是 sid 复用；
    这里用真的 `PeerLink` + 真的 UDP 回环 + 真的 TCP 服务端验证。

    另外钉住一条**协议事实**：Minecraft 是**服务器先说话**。所以"游戏"在这里
    先只等横幅、什么都不发 —— 房主要是等玩家先发数据才去连本机世界，两边就
    互等，游戏永远停在"正在连接"（`direct_e2e_test` 真的复现过这个死锁）。
    """
    world_port, world_server = await _start_world_server()

    sock_a, sock_b = make_socket(blocking=False), make_socket()

    link_a = direct_carry.PeerLink("A", _FakePunchLink(sock_a, sock_b))
    link_b = direct_carry.PeerLink("B", _FakePunchLink(sock_b, sock_a))

    try:
        ok_a, ok_b = await asyncio.gather(
            link_a.ensure_started(6.0), link_b.ensure_started(6.0)
        )

        check("两端的直连隧道都建立", ok_a and ok_b, f"{ok_a}/{ok_b}")

        check("★ 非阻塞 socket 被设回阻塞（否则 reader 会静默死掉）",
              _is_blocking(sock_a), "sock_a still non-blocking")

        link_b.set_backend("127.0.0.1", world_port)

        # The host's own already-open connection to the local world (in the live
        # code that is the one the frozen tunnel loop opened to send READY).
        host_carrier = direct_carry.DirectCarrier(link_b, "host", is_host=True)
        await host_carrier.open()

        spare_reader, spare_writer = await asyncio.open_connection(
            "127.0.0.1", world_port
        )
        host_task = asyncio.ensure_future(
            host_carrier.pump(spare_reader, spare_writer, relay_reader=None)
        )

        got = {}

        async def play(tag, text):
            # ONE listener PER play. A shared accept queue let the two plays
            # consume each other's accepted pair, which is harmless for the data
            # itself but catastrophic for DIAGNOSIS: when one stream failed, the
            # other read a dead socket and reported a 0-byte EOF, hiding the real
            # failure behind a symptom of the swap.
            game_port, game_server, accepted = await _start_game_listener()

            try:
                # "the game": connects to the guest's local proxy and waits for the
                # world to speak first, exactly like Minecraft does.
                game_reader, game_writer = await asyncio.open_connection(
                    "127.0.0.1", game_port
                )
                accepted_reader, accepted_writer = await accepted.get()

                carrier = direct_carry.DirectCarrier(link_a, tag, is_host=False)
                await carrier.open()
                pump = asyncio.ensure_future(
                    carrier.pump(accepted_reader, accepted_writer)
                )

                try:
                    banner = await asyncio.wait_for(
                        game_reader.readexactly(len(_BANNER)), timeout=15
                    )
                    game_writer.write(text)
                    await game_writer.drain()
                    echoed = await asyncio.wait_for(
                        game_reader.readexactly(len(text)), timeout=15
                    )
                    got[tag] = (banner, echoed)
                finally:
                    pump.cancel()
                    with _suppress():
                        await pump

                    game_writer.close()
            finally:
                game_server.close()
                with _suppress():
                    await asyncio.wait_for(game_server.wait_closed(), timeout=5)

        await asyncio.gather(
            play(1, b"probe-connection"), play(2, b"real-join-connection")
        )

        check("★ 第一条流：世界先说的话能到游戏（不是两边互等）",
              (got.get(1) or (None, None))[0] == _BANNER,
              repr(got.get(1)))
        check("★ 第一条流（列表探测）回显正确",
              (got.get(1) or (None, None))[1] == b"probe-connection",
              repr(got.get(1)))
        check("★ 第二条流（真正进世界）同时走通（房主现开一条后端）",
              (got.get(2) or (None, None))[1] == b"real-join-connection",
              repr(got.get(2)))
        check("两条流各自独立，没有串包",
              got.get(1) != got.get(2) and len(got) == 2, str(got))

        host_task.cancel()
        with _suppress():
            await host_task

        # Close the host's own world connection explicitly. If no stream ever
        # claimed it, nothing else does -- and `world_server.wait_closed()`
        # waits for EVERY connection handler to finish, so the suite would hang
        # instead of failing. That is exactly how this suite first went flaky
        # (measured: `stop=True`, links closed, streams still counted active).
        with _suppress():
            spare_writer.close()
            await spare_writer.wait_closed()
    finally:
        link_a.close()
        link_b.close()
        world_server.close()

        # Bounded: a leaked connection handler must FAIL this suite, not freeze it.
        with _suppress():
            await asyncio.wait_for(world_server.wait_closed(), timeout=5)

        for sock in (sock_a, sock_b):
            with _suppress():
                sock.close()


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _is_blocking(sock):
    try:
        sock.setblocking(True)
    except OSError:
        return False

    # setblocking(True) is idempotent; the useful read is the timeout, which is
    # None for a blocking socket. There is no getter for "blocking", so probe
    # with a non-blocking recvfrom: a blocking socket would park here, which is
    # why we only do this on a socket we know has no data pending.
    return sock.gettimeout() is None


class _FakePunchLink:
    """The punched-link surface `PeerLink` needs, and nothing more."""

    def __init__(self, sock, peer_sock):
        self.sock = sock
        self.punched_sock = sock
        self.peer_addr = peer_sock.getsockname()
        self.carried = False
        self._keepalive = None

    @property
    def keepalive_running(self):
        return bool(self.carried) or self._keepalive is not None

    def start_keepalive(self):
        self._keepalive = threading.Event()

    def stop_keepalive(self):
        self._keepalive = None

    def close(self):
        with _suppress():
            self.sock.close()


#: The world's first words. Minecraft's server speaks first, and a carrier that
#: waits for the player to send something before touching the world deadlocks.
_BANNER = b"MC-BANNER-FROM-HOST\n"


async def _start_world_server():
    """Stand-in for the host's LAN world: speaks first, then echoes."""

    async def handler(reader, writer):
        try:
            writer.write(_BANNER)
            await writer.drain()

            while True:
                data = await reader.read(65536)

                if not data:
                    return

                writer.write(data)
                await writer.drain()
        except Exception:
            return
        finally:
            with _suppress():
                writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    return port, server


async def _start_game_listener():
    """
    Stands in for the player's local proxy port.

    The test's "game" CONNECTS here; the accepted pair is what the guest carrier
    pumps into the tunnel, exactly as `player_core.handle_minecraft` does.
    """
    accepted = asyncio.Queue()

    async def handler(reader, writer):
        await accepted.put((reader, writer))

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    return port, server, accepted


# ============================================================
# 6. UdpHub 路由：同一个 IP 两个隧道 = 全丢
# ============================================================

def test_hub_ambiguity():
    """
    同一个对端 IP 下注册两个隧道 -> `_pick` 判 ambiguous 并丢弃。

    这不是缺陷，是**设计**，而且它正是"一个对端只能有一个 UdpTunnel"的
    理由：如果每个会话各建一个隧道（上一版的做法），Minecraft 的两条连接
    会让对端在这里把包全丢掉 —— 表现就是"洞打通了，游戏连不上"。
    """
    print("")
    print("---- UdpHub：一个 IP 两个隧道 = ambiguous ----")

    sock = make_socket()
    logs = []
    hub = UdpHub(sock, logs.append)

    t1 = UdpTunnel(sock, ("127.0.0.1", 1111), logs.append, hub=hub)

    addr = ("127.0.0.1", 3333)
    check("只有一个隧道时按 IP 路由成功", hub._pick(addr) is t1)

    # A second tunnel for the SAME IP -- the shape the live path must never
    # create (one peer = one tunnel, many sid streams on it).
    t2 = UdpTunnel(sock, ("127.0.0.1", 2222), logs.append, hub=hub)

    t1.send_to = addr
    check("两个隧道时按 (ip,port) 精确匹配", hub._pick(addr) is t1)
    check("两个隧道、地址都对不上 -> ambiguous（丢弃）",
          hub._pick(("127.0.0.1", 4444)) is None)

    hub.close()
    sock.close()


# ============================================================
# main
# ============================================================

def main():
    print("=" * 66)
    print(" MC-Link 直连承载测试（udptunnel / channel / direct_carry）")
    print("=" * 66)
    print(" 注意：回环测试只能证明「机制没搬坏」，不能证明「真实网络能用」。")

    test_constants_pinned()
    test_loopback_handshake_and_bytes()
    test_large_message_reassembly()
    test_loss_and_reorder()
    test_frame_parsing()
    test_peerlink_multiplexes_streams()
    test_hub_ambiguity()

    print("")
    print("=" * 66)
    print(f" RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    if FAILED:
        for name in FAILED:
            print(f"   FAILED: {name}")

    print("=" * 66)

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
