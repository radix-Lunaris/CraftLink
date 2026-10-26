"""
CraftLink -- 直连承载：把「已经打通的洞」变成「能搬游戏数据的通道」。

============================================================================
 这个文件整个重写过一次，原因写在 `HANDOFF-TO-NEXT-SESSION.md` 里
============================================================================

上一版这里的职责是「把 `reliable.py` 接上核心」—— 而 `reliable.py` 是**我
自己设计**的可靠有序流（951 行），加上这里的接线（226 行），一共 1177 行，
**从来没有在真实 NAT 上成功过**。参考工程对应的实现只有 731 行，而且经过
真实 NAT3/NAT4 网络验证。

结果就是项目负责人连续十几轮看到的那件事：**洞打通了，游戏连不上**。
打洞那半是照抄的（确实能打通），承载那半是发明的（从来没通过）。

所以这一版的原则只有一条，用他的原话："能抄就抄。不许发明。"

    可靠传输        -> `core/udptunnel.py`  （UdpTunnel + UdpHub，逐字搬运）
    一条链路多条流  -> `core/channel.py`    （Stream + PeerChannel，逐字搬运）

**本文件里唯一属于我们的代码是「桥」**：我们的核心是 asyncio
（StreamReader / StreamWriter），他的核心是线程（阻塞 socket）。桥刻意做得
又薄又直白：

    mc_reader 读到的字节 -> 队列 -> 线程调用 tunnel.send
    tunnel 收到的字节   -> 线程 -> asyncio 队列 -> mc_writer

序号、累积 ACK、NAK、重传、乱序缓冲、拥塞窗口、保活判死 —— **一行都不在
这里**，全在他的 `udptunnel.py` 里。要改那部分，就去改那个文件（但别改，
它是验证过的那一份）。

============================================================================
 为什么一条链路要承载多条 Minecraft 连接（sid）
============================================================================

Minecraft 进一个世界会开**两条** TCP 连接：先一条服务器列表探测（问完就关），
再一条真正进世界。如果一条链路只承载一条连接，第二条就没地方去 ——
项目负责人遇到的就是「第一条走直连，第二次掉回中继」。

参考工程用 `sid` 把多条连接复用在一条隧道上（`channel.py`）。我们照做，而且
这**不是可选项**：他的 `UdpHub` 是「一个 socket、多个隧道」的**唯一读者**，
并且按**对端 IP** 路由；同一个对端注册两个隧道会被判成
`ambiguous packet ... dropped`，整条链路当场作废。所以一个对端只能有一个
`UdpTunnel`，多条游戏连接必须靠 sid 分用同一条。

============================================================================
 和中继的关系（这条是硬规则，不许变）
============================================================================

STUN 模式下**主流量不许碰中继**。所以：

  * 拿不到直连 -> 会话被拒绝并说明原因（由 cores 决定），**不是**悄悄走中继；
  * 直连中途断了 -> 会话结束（外层重连会重打洞），同样不回落中继；
  * 中继那条隧道一直留着，但它在 STUN 模式下只当"配对用的控制面"，
    一个游戏字节都不承载。如果它真的送来了字节，房主侧的 `DirectCarrier.pump()`
    会大声记一行 —— 那是"中继在搬游戏数据"这个事故的唯一现场证据。
"""

import asyncio
import contextlib
import queue
import threading
import time

from . import protocol as proto
from .channel import PeerChannel
from .common import enable_tcp_nodelay
from .udptunnel import UdpHub, UdpTunnel

#: 建立直连隧道的预算。洞里其实早就打通了，这一步只是 HELLO / HELLO_ACK
#: 往返，所以给短一点：失败就走「没有直连」那条路，绝不拖着会话。
#:
#: 参考工程在"命中之后"用的是 `HANDSHAKE_AFTER_HIT_S`（更短），理由一样：
#: 等太久反而会错过对端的 socket 阵列。
CONNECT_TIMEOUT_S = 8.0

#: 排队上限。满了就丢（TCP 自己会重传），**绝不阻塞事件循环** ——
#: 这是参考工程 `_deliver` 的同一套取舍，理由见 `udptunnel.py` 里
#: "Blocking for tens of seconds here stalled the whole game stream"。
UP_QUEUE_MAX = 256
DOWN_QUEUE_MAX = 1024

#: 本机 Minecraft 连不上时，重试间隔（参考工程 `_host_stream` 用 2 秒）。
LOCAL_MC_RETRY_S = 2.0

#: "开流通知"（那个空帧）重发的间隔与上限。
#:
#: ============================================================================
#:  为什么必须重发（一个会让游戏永远停在"正在连接"的竞态，实测抓到过）
#: ============================================================================
#:
#: 空帧是这条流唯一的"我来了"信号，而 UDP 隧道**是允许丢包的**（拥塞时
#: `_send_chunk` 会主动丢并让上层重传 —— 但那是**数据传输**的逻辑，控制性的
#: 空帧没有序号、也没人替它重传）。一旦丢掉，房主就不知道有这条流存在；
#: 而 Minecraft 是**服务器先说话**，玩家端不会发任何游戏数据来兜底 ——
#: 于是两边永远互等，游戏停在"正在连接"。
#:
#: 参考工程把开流通知放在**信令通道（TCP）**上，正是因为它不能丢
#: （`net_proxy._send_stream_open`）。我们不新增协议，改成"发到对端开口为止"：
#: 对端一旦回了字节（`st.down > 0`）就说明它已经认领了这条流，立刻停。
ANNOUNCE_EVERY_S = 1.0
ANNOUNCE_TIMEOUT_S = 20.0


class PeerLink:
    """
    一个对端的一条直连链路：他验证过的 `UdpTunnel` + `PeerChannel`。

    生命周期**跟着房间走**，不跟着会话走。原因很实在：

      * NAT 映射属于本地端口，关掉 socket 再开就是新映射，洞立刻作废；
      * Minecraft 的两条连接之间有间隔，会话是**短命**的（探测那条几秒就完），
        链路必须是**长命**的，否则第二条连接只能重打洞 —— 而重打洞是
        "新 socket、新映射"，等于把第一条用的洞扔掉。

    所以：链路建立一次、保活、被多条会话复用；退出房间才关。
    """

    def __init__(self, peer_id, punch_link, log=None, peer_addr=None):
        self.peer_id = peer_id
        self.punch_link = punch_link
        self.log = log or (lambda *_a: None)

        #: **命中的那个 socket**，不是发布端口。见 `direct.py::punched_sock`：
        #: 多 socket 打洞里这两者不同，而只有命中那个的映射被对端 NAT 放行。
        self.sock = getattr(punch_link, "punched_sock", None) or punch_link.sock

        #: 隧道往哪儿发 —— **必须是这一洞实际打通的地址**。
        #:
        #: ====================================================================
        #:   这里踩过一个把"局域网"整个废掉的坑（用户实测的"不稳定"根因）
        #: ====================================================================
        #:
        #: `punch_link.peer_addr` 是对端**公告**的地址（几乎总是公网那个），
        #: 而 `result.addr` 才是这一洞真正命中的地址 —— 两者在同宿舍/同路由器
        #: 的一对机器上**完全不同**。日志原文：
        #:
        #:     [punch] s2c ok: punched via port 63271
        #:     [direct] ROOM LINK UP: peer=192.168.2.128:63271      <- 命中局域网
        #:     [UDP] punching 220.196.193.15:5383 ...               <- 隧道却打公网
        #:     [direct] tunnel handshake failed ... no direct carry
        #:     [direct] tunnel handshake failed; re-punching        <- 于是无限重打
        #:
        #: 洞开在局域网上，HELLO 却往公网发，永远握不上手，房间就不停重打 ——
        #: 表现正是"偶尔说打通了、两边端口不一样、一会儿又重试"。
        self.addr = tuple(peer_addr) if peer_addr else tuple(punch_link.peer_addr)

        self.hub = None
        self.tunnel = None
        self.channel = None

        #: 本机 Minecraft 的地址（**房主侧**才需要：每条 sid 一条后端连接）。
        self._backend = None
        #: sid -> 正在跑的 asyncio 泵任务（房主侧）。
        self._tasks = {}
        #: 房主侧预先连好的本机世界连接，等着被某个 sid 认领（见 `Spare`）。
        self._spares = []

        self.stop = threading.Event()
        self._started = False
        self._start_lock = None
        self._closed_event = None
        self._closed_event_loop = None
        self._reader_thread = None
        self._demux_task = None

        #: 有几条 sid 流正在搬数据。界面上的「已直连」要在**真的搬数据**时
        #: 才亮，不能只因为"洞打通了"就亮（负责人为这个矛盾抱怨过）。
        self.active_streams = 0
        self._listeners = []

        # ================================================================
        #  字节统计（审计问题 6：直连承载的游戏流量以前一个字节都不数）
        # ================================================================
        #
        # 本机视角：up = 本机发往对端，down = 对端发来本机。
        # 每搬完一段就调 `_note_traffic()` 交给进程级账本（`core/traffic.py`），
        # 所以这里记的是**增量**，不是累计差额 —— 一条链路上有多条 sid 流时，
        # 谁搬的字节谁报，绝不会重复计数。
        self.bytes_up = 0
        self.bytes_down = 0

        #: `fn(up, down)`：由 cores 传进来（见 `DirectCarrier.on_traffic`），
        #: 把"这条路真的搬了这些字节"报给进程级账本。None = 没人要（单测）。
        self.on_traffic = None

    # ---------------------------------------------------------------- 状态

    def _note_traffic(self, up, down):
        """
        记账（审计问题 6）。**只加计数、只调一个回调**，绝不阻塞、绝不抛异常：
        它跑在数据面的读/写路径上。

        回调优先用 cores 注入的那个（单测用），否则落到进程级账本单例
        （房间级链路也要记 —— 它比任何会话都活得久）。
        """
        if up:
            self.bytes_up += int(up)

        if down:
            self.bytes_down += int(down)

        callback = self.on_traffic

        try:
            if callback is not None:
                callback(int(up or 0), int(down or 0))
            else:
                from . import traffic

                traffic.ledger().record(
                    traffic.DIRECT, up=int(up or 0), down=int(down or 0)
                )
        except Exception:
            pass

    @property
    def started(self):
        return self._started

    def idle_for(self):
        """
        Seconds since the peer was last heard on this tunnel, or None when the
        tunnel does not exist yet.

        The room's watchdog uses this as its dead-link clock (his `LINK_DEAD_S`
        = 20s): a UDP hole is not a connection, so the only symptom of a dead
        peer is silence. `UdpTunnel.idle_for()` is his own metric -- the
        keepalive every 5s keeps it near zero on a live link.
        """
        tunnel = self.tunnel

        if tunnel is None:
            return None

        try:
            return tunnel.idle_for()
        except Exception:
            return None

    @property
    def rtt_ms(self):
        """
        这条直连【到对端】的真实往返时间（毫秒），或 None。

        ============================================================================
          ADDITIVE：读的是隧道已有的 keepalive 往返，不是新测的
        ============================================================================

        `UdpTunnel` 本来就每 5 秒发一个 T_PING、对端回 T_PONG（那是保活，必须
        有的）。`udptunnel.py` 里只加了一句"记时刻、收到应答相减"，协议、包格式
        和握手步骤一个都没动 —— 见那里的注释。

        隧道还没建（洞打好了但长连接没起来）或第一次往返还没回来时是 None：
        界面显示 `-`，绝不编。
        """
        tunnel = self.tunnel

        if tunnel is None:
            return None

        try:
            return getattr(tunnel, "rtt_ms", None)
        except Exception:
            return None

    @property
    def describe(self):
        return "udp %s -> %s" % (
            (self.sock.getsockname() if self.sock is not None else ("?", 0)),
            self.addr,
        )

    def add_listener(self, fn):
        """`fn(active: bool)` -- 流开始/结束搬数据时通知（界面用）。"""
        if fn is None:
            # Callers that do not care about the UI signal pass None. Silently
            # accepting it matters: `fn(...)` on None raises inside a pump task,
            # where the exception is easy to lose (it does not fail the test,
            # it just stops the data path).
            return

        if fn not in self._listeners:
            self._listeners.append(fn)

        fn(self.active_streams > 0)

    def remove_listener(self, fn):
        if fn is None:
            return

        with contextlib.suppress(ValueError):
            self._listeners.remove(fn)

    def _announce(self):
        active = self.active_streams > 0

        for fn in list(self._listeners):
            try:
                fn(active)
            except Exception:
                pass

    def _stream_started(self):
        self.active_streams += 1

        if self.active_streams == 1:
            self._announce()

    def _stream_ended(self):
        if self.active_streams > 0:
            self.active_streams -= 1

        if self.active_streams == 0:
            self._announce()

    def set_backend(self, host, port):
        """
        房主侧：新 sid 到来时，为它开一条**自己的**本机 Minecraft 连接。

        为什么房主要自己开、而不是用会话那条已经开好的连接：会话那条连接
        对应"中继的一次配对"，而 sid 是**对端为每条 MC 连接分配**的，两者
        没有可靠的对应关系（玩家可能同时开着探测和进世界）。参考工程的做法
        就是"每个 sid 一条后端连接"，因为 MC 协议是有状态的、服务器答完
        探测就会关掉 socket。与自己的会话连接互换在物理上也等价（都是到
        同一个本机世界的新 TCP 连接），所以没有正确性问题。
        """
        self._backend = (host, int(port))

    # ---------------------------------------------------------------- 启动

    async def ensure_started(self, timeout=CONNECT_TIMEOUT_S):
        """
        幂等：第一次调用建立 hub/tunnel/读线程/demux，后面直接返回。

        True 表示"隧道已经握手成功，可以搬字节"。失败返回 False 并记原因，
        **不抛异常** —— 直连是优化，不是前置条件。
        """
        if self._started and not self.stop.is_set():
            return True

        if self.stop.is_set():
            return False

        if self._start_lock is None:
            self._start_lock = asyncio.Lock()

        async with self._start_lock:
            if self._started and not self.stop.is_set():
                return True

            if self.stop.is_set():
                return False

            if self.sock is None or not self.addr:
                self.log("[direct] no punched socket/peer; no direct carry")
                return False

            # ================================================================
            #  先停掉房间的打洞保活 —— 这是**必须**的，不是优化
            # ================================================================
            #
            # 房间保活发的是 `punch.pack_ping()`，它和隧道包**同一个 MAGIC、
            # 同一种头**（两边都是从他的 `natpunch.py` 搬来的）。后果有两个，
            # 都是坏的：
            #
            #   1. 它会被对端隧道当成"收到了对端的数据包"，于是隧道在任何
            #      真正的 HELLO 交换之前就 `connected = True`，而且
            #      `send_to` 钉在**发包那个 socket 的地址**上 —— 如果打洞
            #      命中在阵列 socket 上，那个地址就是错的，之后所有数据都
            #      发到对端的发布端口，谁也收不到；
            #   2. 它每 5 秒刷新隧道的 `last_rx_at`，于是隧道自己的
            #      "20 秒没消息就判死"永远不会触发 —— 一条真死的链路会一直
            #      显示成活的。
            #
            # 打通之后映射由**隧道自己的保活**（KEEPALIVE_S = 5.0）维持，
            # 所以停掉房间保活不会让洞过期。`DirectLink.carried` 会告诉
            # 打洞循环"这条链路正在被用"，免得它以为保活没了就重打洞。
            with contextlib.suppress(Exception):
                self.punch_link.carried = True
            with contextlib.suppress(Exception):
                self.punch_link.stop_keepalive()

            try:
                await asyncio.to_thread(self._connect_blocking, timeout)
            except Exception as e:
                self.log(
                    "[direct] tunnel handshake failed over %s: %s: %s -- "
                    "no direct carry" % (self.describe, type(e).__name__, e)
                )
                self._teardown_start()
                # Give the room its keepalive back: the hole is still good, we
                # just could not use it yet, and an un-maintained mapping
                # expires. `carried` must go back to False first or the punch
                # loop will not notice anything is wrong.
                with contextlib.suppress(Exception):
                    self.punch_link.carried = False
                with contextlib.suppress(Exception):
                    if getattr(self.punch_link, "peer_addr", None) is not None:
                        self.punch_link.start_keepalive()
                return False

            self._started = True
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name="mclink-direct-rx-%s" % (self.peer_id or "?",),
                daemon=True,
            )
            self._reader_thread.start()
            self._demux_task = asyncio.create_task(self._demux_loop())

            self.log(
                "[direct] tunnel ready (his udptunnel), peer %s, sid streams on "
                "one link" % self._peer_text()
            )
            return True

    def _connect_blocking(self, timeout):
        """他那一套：一个 UdpHub（唯一读者）+ 一个 UdpTunnel。线程里跑。"""
        sock = self.sock

        # ================================================================
        #  先把 socket 里已经排队的包丢掉
        # ================================================================
        #
        # 房间的保活刚刚还在发 `punch.pack_ping()`，而那个包和隧道的头
        # **格式完全相同**（两边都是从他的 `natpunch.py` 搬来的）。如果对端的
        # hub 在我们的 HELLO 之前先读到它，对端就会把隧道判成 `connected`，
        # 而且 `send_to` 钉在**保活包来的那个端口**上（发布端口，不是命中那个），
        # 之后每一个游戏字节都发去没人监听的地方。
        #
        # 代价很小：对端已经发出的 HELLO 顶多被丢掉一个，而 `connect()` 每
        # HELLO_S（0.2 秒）就重发一次。
        self._drain_socket(sock)

        # ================================================================
        #  必须把 socket 设回阻塞模式
        # ================================================================
        #
        # `UdpHub._run` 用的是阻塞 `recvfrom`（他的实现就是这样）。而打洞
        # 会把 socket 设成非阻塞（`punch.py` 的阵列里就有 `setblocking(False)`），
        # 非阻塞下 `recvfrom` 抛 `BlockingIOError` —— 它是 `OSError` 的子类，
        # 而他的循环里 `except OSError: break` 会把读者**永久关掉**，一声不响：
        # 隧道看起来连着，实际再也收不到任何东西。
        with contextlib.suppress(OSError):
            sock.settimeout(None)
        with contextlib.suppress(OSError):
            sock.setblocking(True)

        # 顺序有讲究：先把隧道注册进 hub，再让 hub 开始跑。
        # `UdpTunnel.__init__` 会 `hub.register(self)`；反过来的话，hub 可能
        # 在注册完成前就收到对端的 HELLO 并把它丢掉。
        self.hub = UdpHub(sock, self.log)
        self.tunnel = UdpTunnel(sock, self.addr, self.log, hub=self.hub)
        self.channel = PeerChannel(
            self.peer_id, "direct udp", self.tunnel.send, self.log
        )
        self.hub.start()

        # 阻塞等待 HELLO/HELLO_ACK。两端都会盲目地发 HELLO，这正是打通、
        # 也是完成握手的方式（他的 `connect()` docstring）。
        self.tunnel.connect(timeout)

    @staticmethod
    def _drain_socket(sock):
        """丢掉 socket 上已经排队的全部数据报（见 `_connect_blocking`）。"""
        try:
            sock.setblocking(False)
        except OSError:
            return

        try:
            while True:
                try:
                    sock.recvfrom(65535)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
        finally:
            with contextlib.suppress(OSError):
                sock.setblocking(True)
            with contextlib.suppress(OSError):
                sock.settimeout(None)

    def _peer_text(self):
        try:
            return "%s:%d" % (self.addr[0], self.addr[1])
        except Exception:
            return str(self.addr)

    # ---------------------------------------------------------------- 循环

    def _reader_loop(self):
        """`tunnel.recv()` -> `channel.inbox`。他的 `_tunnel_reader` 同一件事。"""
        tunnel = self.tunnel
        channel = self.channel

        if tunnel is None or channel is None:
            return

        while not self.stop.is_set():
            try:
                data = tunnel.recv(1.0)
            except Exception:
                break

            if data:
                channel.put(data)

    async def _demux_loop(self):
        """
        `channel.inbox` -> `channel.feed()` -> 每条 sid 自己的流。

        是他的 `_demux`，只是把"线程阻塞取队列"换成 `to_thread`，这样新 sid
        可以在事件循环上直接开后端连接。
        """
        while not self.stop.is_set():
            # Local reference: a concurrent `close()` nulls the attribute, and
            # this loop has one more use of it after the await below.
            channel = self.channel

            if channel is None:
                break

            try:
                data = await asyncio.to_thread(channel.inbox.get, True, 1.0)
            except queue.Empty:
                continue
            except (asyncio.CancelledError, RuntimeError):
                break
            except Exception:
                break

            try:
                frames = channel.feed(data)
            except Exception as e:
                self.log("[direct] frame parse error: %s" % e)
                continue

            for sid, payload in frames:
                st = channel.get_stream(sid)

                if st is None:
                    # 控制消息可能丢、也可能还没到（他 `_demux` 的原注释）：
                    # 按需创建，并给这条流配一条本机世界的连接。
                    st, created = channel.alloc_stream(sid)

                    if created:
                        self._attach_backend(st)

                if payload:
                    st.put(payload)

    def _attach_backend(self, st):
        """
        给一条新 sid 配本机 Minecraft 连接。

        优先用**房主已经连好的那条**（`Spare`：隧道循环里连上、用来发 READY
        的那条）。这不是优化 —— MC 协议是**服务器先说话**，等玩家先发数据
        才去连本机世界会让两边互等，游戏永远停在"正在连接"（见 `Spare`）。

        没有 spare 才现开一条（参考工程的 `_host_stream`：连不上就每 2 秒重试，
        因为"世界还没开"才是最常见的真实原因）。
        """
        spare = self.take_spare()

        if spare is not None:
            self._spawn_spare_stream(st, spare)
            return

        if self._backend is not None:
            self._spawn_backend(st)

    # ---- 房主侧：预先连好的本机世界连接（见 `Spare`）----

    def park_spare(self, reader, writer):
        """把一条已经连上本机世界的连接交出去，等某个 sid 认领。"""
        spare = Spare(reader, writer)
        spare.done = asyncio.Event()
        self._spares.append(spare)
        return spare

    def take_spare(self):
        """认领最早的一条 spare；没有就返回 None。"""
        while self._spares:
            spare = self._spares.pop(0)

            if spare.claimed:
                continue

            spare.claimed = True
            return spare

        return None

    def forget_spare(self, spare):
        """收回一条没人认领的 spare（会话结束时）。"""
        with contextlib.suppress(ValueError):
            self._spares.remove(spare)

    def _spawn_spare_stream(self, st, spare):
        if st.sid in self._tasks and not self._tasks[st.sid].done():
            return

        self._tasks[st.sid] = asyncio.create_task(self._pump_spare(st, spare))

    async def _pump_spare(self, st, spare):
        # Local references on purpose: a concurrent `close()` sets the
        # attributes to None, and this task's `finally` still has to run.
        channel = self.channel
        self._stream_started()

        try:
            if channel is not None:
                await _pump_stream(
                    channel, st, spare.reader, spare.writer, self.log,
                    traffic=self,
                )
        finally:
            with contextlib.suppress(Exception):
                spare.writer.close()

            spare.done.set()
            self._tasks.pop(st.sid, None)

            if channel is not None:
                channel.drop_stream(st.sid)

            self._stream_ended()

    # ---------------------------------------------------- 房主侧：本地后端

    def _spawn_backend(self, st):
        if st.sid in self._tasks and not self._tasks[st.sid].done():
            return

        self._tasks[st.sid] = asyncio.create_task(self._host_stream(st))

    async def _host_stream(self, st):
        """
        房主侧（**后备**路径）：为这条 sid 现开一条本机 Minecraft 连接并双向泵。

        只有在没有 spare 的时候才会走到这里 —— 也就是玩家比房主的会话先一步
        开始灌数据（正常的 Minecraft 不会，它等服务器先说话）。逻辑就是他的
        `_host_stream`：连不上就每 2 秒重试，因为"世界还没开"是最常见的真实
        原因，而重试比拒绝有用得多。
        """
        host, port = self._backend
        channel = self.channel
        self._stream_started()

        try:
            while not st.stop.is_set() and not self.stop.is_set():
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port), timeout=5
                    )
                except Exception as e:
                    if st.stop.is_set() or self.stop.is_set():
                        break

                    self.log(
                        "[direct] stream %d: cannot reach local Minecraft at "
                        "%s:%d (%s) -- is the LAN world open? retrying in %.0fs"
                        % (st.sid, host, port, type(e).__name__,
                           LOCAL_MC_RETRY_S)
                    )
                    await asyncio.sleep(LOCAL_MC_RETRY_S)
                    continue

                enable_tcp_nodelay(writer)
                self.log(
                    "[direct] stream %d -> local Minecraft %s:%d"
                    % (st.sid, host, port)
                )

                try:
                    if channel is not None:
                        await _pump_stream(
                            channel, st, reader, writer, self.log, traffic=self
                        )
                finally:
                    with contextlib.suppress(Exception):
                        writer.close()

                break
        finally:
            self._tasks.pop(st.sid, None)

            if channel is not None:
                channel.drop_stream(st.sid)

            self._stream_ended()

    # ---------------------------------------------------------------- 关闭

    def _teardown_start(self):
        if self.hub is not None:
            with contextlib.suppress(Exception):
                self.hub.close()
            self.hub = None

        self.tunnel = None
        self.channel = None

    def close(self):
        """退出房间时调用。关掉链路、放掉保活标记。"""
        if self.stop.is_set():
            return

        self.stop.set()

        for st in list(getattr(self.channel, "streams", {}).values()):
            st.stop.set()

        for task in list(self._tasks.values()):
            task.cancel()

        self._tasks.clear()

        if self._demux_task is not None:
            self._demux_task.cancel()
            self._demux_task = None

        # hub.close() 会关掉所有还在用这个 socket 的隧道（他的实现），
        # 但它们都不 own socket，所以 socket 本身留给房间关。
        self._teardown_start()
        self._announce()
        self._wake_closed()

        # 把交接标记还回去，让房间的保活可以重新接手（洞还在，只是暂时
        # 没有游戏在用）。
        with contextlib.suppress(Exception):
            self.punch_link.carried = False
        with contextlib.suppress(Exception):
            if getattr(self.punch_link, "peer_addr", None) is not None:
                self.punch_link.start_keepalive()

    #: asyncio 侧等待链路关闭用的事件（懒创建，必须建在事件循环上）。
    def closed_event(self):
        loop = asyncio.get_running_loop()

        if self._closed_event is None or self._closed_event_loop is not loop:
            self._closed_event = asyncio.Event()
            self._closed_event_loop = loop

        if self.stop.is_set():
            self._closed_event.set()

        return self._closed_event

    def _wake_closed(self):
        event = self._closed_event

        if event is None:
            return

        loop = self._closed_event_loop

        if loop is None or loop.is_closed():
            return

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)

    async def wait_closed(self):
        await self.closed_event().wait()


# ============================================================================
#  会话侧：一条 Minecraft 连接挂在一条链路上
# ============================================================================


class Spare:
    """
    房主侧：一条**已经连上本机世界**的连接，等着某个 sid 来认领。

    ============================================================================
      为什么必须有这个东西（否则"列表探测"永远收不到服务器的第一句话）
    ============================================================================

    Minecraft 的协议是**服务器先说话**：客户端连上去，服务器先发第一个包
    （握手/状态响应/服务器列表横幅）。所以房主这一侧**不能等玩家先发数据**
    才去连本机世界 —— 那样两边都在等对方先开口，游戏永远停在"正在连接"。

    真实日志（`tests/direct_e2e_test.py` 复现出来的）：客户端连上玩家的本地
    代理后等 `recv()`，而房主的 `_host_stream` 在等第一条 sid 数据帧来触发，
    10 秒后客户端超时。上一版之所以"看起来"没这个问题，是因为它用的是
    **会话自己那条**早就连好的本机连接；现在按参考工程改成"每个 sid 一条
    后端连接"，就必须补上"预先连好、等 sid 认领"这一步。

    参考工程的对应做法是发一条 `peer_signal {t:"open", sid}` 控制消息让房主
    提前连（`net_proxy._send_stream_open`）。我们不复用它那条信令通道，而是
    直接用**房主这里已经开好的那条连接**（冻结的隧道循环里连的、用来发
    READY 的那条）—— 一模一样的"提前连好"，而且顺带把那条本来会白白浪费的
    连接用上了。

    它和 sid 的对应关系**不需要**是确定的：所有到同一个本机世界的连接都是
    等价的（MC 协议是按连接独立的），所以哪个 sid 认领到哪一条都无所谓。
    """

    __slots__ = ("reader", "writer", "claimed", "done")

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.claimed = False
        self.done = None            # asyncio.Event，泵跑完时 set


class DirectCarrier:
    """
    一个会话的直连承载。

    玩家侧：分一个 sid，把自己那条 Minecraft 连接挂上去。
    房主侧：把本机世界那条已经连好的连接交出去（`Spare`），然后**守住会话**。
    """

    def __init__(self, link, key, is_host, log=None, on_activity=None):
        self.link = link
        self.key = key
        self.is_host = bool(is_host)
        self.log = log or (lambda *_a: None)
        #: `fn(active: bool)` -- 由 cores 传进来，把"真的在搬数据"报给界面。
        self._on_activity = on_activity
        self.stream = None
        self.mc_host = None
        self.mc_port = None

    @property
    def describe(self):
        return self.link.describe if self.link is not None else "none"

    def set_backend(self, host, port):
        self.mc_host = host
        self.mc_port = port

    async def open(self):
        """
        True 表示"可以开始搬游戏数据"。任何失败都返回 False，不抛异常。
        """
        if self.link is None:
            return False

        try:
            if not await self.link.ensure_started():
                return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log(
                "[direct] stream setup failed over %s: %s: %s -- no direct carry"
                % (self.describe, type(e).__name__, e)
            )
            return False

        if self.is_host:
            if self.mc_host is not None:
                self.link.set_backend(self.mc_host, self.mc_port)
        else:
            # 玩家侧是**发起方**：每条 Minecraft 连接一条流，sid 由本端分配
            # （参考工程 `_guest_proxy_loop` 里的 `ch.next_stream()`）。
            self.stream = self.link.channel.next_stream()

        return True

    async def pump(self, mc_reader, mc_writer, relay_reader=None):
        """
        双向搬这一个会话的字节。两侧的语义不同，但入口是同一个：

          * 玩家侧：把这条 Minecraft 连接挂成一条新的 sid 流；
          * 房主侧：把这条到本机世界的连接交给链路（`Spare`），然后守住会话。

        任一方向结束就收工。**刻意不关链路、不关 socket**：链路是房间的资产，
        Minecraft 的第二条连接（真正进世界那条）马上还要用它。
        """
        if self.is_host:
            await self._hold_host(mc_reader, mc_writer, relay_reader)
            return

        if self.stream is None:
            return

        # 洞可能在这条流开始泵之前就被判死/拆掉了（`channel` 已经 None）。
        # 那就没必要再挂上去：直接在还没计流的情况下收工，绝不抛异常。
        if self.link.channel is None:
            self.log(
                "[direct] the link was torn down before this stream started; "
                "not carrying it over the direct path"
            )
            return

        self.link._stream_started()
        self._emit(True)

        # ================================================================
        #  先发一个【空帧】当"开流通知"—— 这是那半个死锁的解药
        # ================================================================
        #
        # Minecraft 是**服务器先说话**：客户端连上玩家的本地代理后，会先等
        # 服务器的第一个字节，自己什么都不发。而房主那边要收到一条 sid 帧
        # 才知道"有一条新流要服务"，才会去连本机世界 —— 两边互等，游戏永远
        # 停在"正在连接"（`tests/direct_e2e_test.py` 真的复现了 10 秒超时）。
        #
        # 参考工程的做法是在**信令通道**上发一条 `peer_signal {t:"open", sid}`
        # 让房主提前连（`net_proxy._send_stream_open`）。我们不新增协议：直接
        # 用他自己的帧格式发一条**负载为空**的数据帧。
        #
        # 为什么这样是对的，而不是"发明"：
        #   * `channel.feed()` 本来就把它解析成 `(sid, b"")`；
        #   * 房主的 `_demux` 本来就"看到不认识的 sid 就建流"（他源码里那句
        #     "control message lost or not yet arrived: create on demand"）；
        #   * `if payload:` 那一行本来就跳过空负载。
        #
        # 也就是说：**它走的正是他为"控制消息丢了"准备的那条兜底路径**，只是
        # 我们主动走一次，而不是等第一条游戏数据来触发。
        #
        # 一次不够：它是**不可靠**的（见 ANNOUNCE_EVERY_S 的说明），所以由
        # `_keep_announcing` 发到对端开口为止。
        announce = asyncio.ensure_future(
            self._keep_announcing(self.link.channel, self.stream)
        )

        try:
            await _pump_stream(
                self.link.channel, self.stream, mc_reader, mc_writer, self.log,
                traffic=self.link,
            )
        finally:
            announce.cancel()

            # `gather(..., return_exceptions=True)` and NOT `suppress(Exception)`:
            # awaiting a just-cancelled task raises CancelledError, which is a
            # BaseException -- `suppress(Exception)` would let it through and
            # SKIP every line below, leaving the stream counted as active forever
            # (measured: `active_streams` stuck at 2 after both pumps ended).
            await asyncio.gather(announce, return_exceptions=True)

            self._emit(False)
            self.link._stream_ended()

            # ================================================================
            #  ★ 链路可能已经先一步被拆掉了（`channel` 已经是 None）
            # ================================================================
            #
            # 用户日志（docs/ISSUES-20260927.md 问题 1/7，2026-09-27
            # 12:17:12.736 / 12:29:45.187，craftlink-client.log）：
            #
            #     [PLAYER 1] error: 'NoneType' object has no attribute 'drop_stream'
            #     [PLAYER 1] disconnected
            #
            # 时序：房间的判死看门狗把这条洞判死（`link died (20s silent)`）→
            # `PeerLink.close()` 的 `_teardown_start()` 把 `self.channel` 置
            # None → 挂在这条旧洞上的 Minecraft 流泵到收尾，这里再去访问
            # `self.link.channel` 就炸在 None 上。异常穿过 `pump()` 传到会话层
            # → 这条玩家连接"莫名断开"，界面看到的正是用户报的
            # 「新建立的连接莫名断开后再次重连」。
            #
            # 同一文件里 `_pump_spare` / `_host_stream` 早就用
            # `channel = self.channel` + `if channel is not None` 防着这一手
            # （见那两处注释 "a concurrent close() sets the attributes to
            # None"），只有玩家侧这一条漏了。链路已经没了就不用再摘流 ——
            # channel 连同它那张表已经被关掉、扔掉了。
            channel = self.link.channel

            if channel is not None:
                channel.drop_stream(self.stream.sid)

    async def _keep_announcing(self, channel, st):
        """
        反复发那个空帧，直到对端开口（`st.down > 0`）或超时。

        丢一个包不再等于"这条流永远不存在" —— 见 ANNOUNCE_EVERY_S 的说明。
        上限 20 秒是刻意的：真到了那一步说明问题不在丢包，继续发只是噪音。
        """
        deadline = time.monotonic() + ANNOUNCE_TIMEOUT_S

        while (not st.stop.is_set() and st.down == 0
               and time.monotonic() < deadline):
            try:
                channel.send(proto.frame(st.sid, b""))
            except Exception:
                pass

            await asyncio.sleep(ANNOUNCE_EVERY_S)

    # ------------------------------------------------------------ 房主侧

    async def _hold_host(self, mc_reader, mc_writer, relay_reader):
        """
        房主侧：交出本机世界那条连接，并守住这个会话直到它结束。

        为什么"守"是必要的工作：会话的寿命就是**中继配对的寿命**，房间靠它
        数"已加入世界"的人。会话这一侧不需要占住任何一条具体连接（每条 sid
        的后端连接由链路自己安排），但这条协程必须活着，否则隧道循环会立刻
        关掉中继那条连接，房间就看不见这个世界里还有人了。

        为什么这里【不】等 `link.wait_closed()`（3.0.3 修正）：链路现在是
        **打洞那一刻就建好的长连接**，和会话没有一一对应 —— 房主有多个玩家，
        `take_path()` 拿到的可能不是这个会话所属的那条，等它关闭等于"别人的
        链路断了我这个会话也结束"。会话的结束信号有且只有两个：
          1. 这个 spare 被某条 sid 认领后泵完了（游戏连接结束）；
          2. 中继那条配对关闭（玩家停止/掉线 → 房间按这个数"已加入世界"）。
        玩家这一侧断了，链路本身由房间的判死看门狗单独处理，不经过这里。
        """
        spare = self.link.park_spare(mc_reader, mc_writer)
        self.link.add_listener(self._on_activity)

        tasks = {
            asyncio.ensure_future(spare.done.wait()),
        }

        if relay_reader is not None:
            tasks.add(asyncio.ensure_future(self._drain_relay(relay_reader)))

        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            self.link.remove_listener(self._on_activity)

            # Never claimed: nobody ever opened a stream for it (the player did
            # not connect, or already had a connection of its own).
            #
            # Close it HERE rather than leaving that to the caller. The caller
            # (the tunnel loop) does close it too, but only after this coroutine
            # returns -- and this coroutine can return on the LINK-closed path
            # while the tunnel loop is still waiting on something else. In the
            # test harness that turned into a ten-second-to-forever stall:
            # `asyncio.Server.wait_closed()` waits for every connection handler
            # to finish, and a handler whose client socket nobody closed never
            # finishes. `close()` is idempotent, so doing it here costs nothing.
            if not spare.claimed:
                self.link.forget_spare(spare)

                with contextlib.suppress(Exception):
                    spare.writer.close()

    async def _drain_relay(self, relay_reader):
        """
        看着中继那条连接，直到它关闭（= 这个会话结束）。

        STUN 模式下它**不该**送来任何游戏字节。真送来了就大声记一行：那是
        "中继在搬游戏数据"这个事故的唯一现场证据（`HANDOFF-TO-NEXT-SESSION.md`
        §8 列的判据之一）。
        """
        while True:
            try:
                data = await relay_reader.read(4096)
            except Exception:
                return

            if not data:
                return

            self.log(
                "[direct] STUN mode: the relay delivered %d game bytes for "
                "session=%s -- the relay must not carry game data in STUN "
                "mode; ignoring them" % (len(data), self.key)
            )

    def _on_activity(self, active):
        self._emit(active)

    def _emit(self, active):
        if self._on_activity is None:
            return

        try:
            self._on_activity(bool(active))
        except Exception:
            pass


# ============================================================================
#  asyncio <-> 线程 的桥
# ============================================================================


async def _pump_stream(channel, st, mc_reader, mc_writer, log, traffic=None):
    """
    双向泵：asyncio 的 Minecraft 连接 <-> 线程的隧道。

    两个方向各自有一处"越过边界"，用的是两种不同的办法，都是有意的：

      * Minecraft -> 隧道：异步读，塞进**有界** `queue.Queue`；一个专门的
        线程把它取出来调 `channel.send`。`tunnel.send` 在拥塞时会阻塞最多
        `CONGEST_WAIT_S`（3 秒），**绝对不能**在事件循环线程上调用 —— 那正是
        项目里那个"整个界面卡住"的同类事故。有界队列给了真正的背压。

      * 隧道 -> Minecraft：一个专门的线程从 `st.q` 取（他的投递队列），
        再 `call_soon_threadsafe` 投到 asyncio 队列，由事件循环写回 MC。
        满了就丢并记一行 —— 与他的 `_deliver` 同一取舍（"TCP 会重传"）。

    两个线程、两个协程，任一方向结束就收工。

    审计问题 6：字节数由 `st` 所属的对端链路自己累计（见 `PeerLink.
    _note_traffic`）—— 那条链路上可能同时跑着多条 sid 流，谁搬的字节谁报，
    不会重复计数，也不会因为会话结束就把数字丢掉。
    """
    loop = asyncio.get_running_loop()
    stopped = st.stop
    up_q = queue.Queue(maxsize=UP_QUEUE_MAX)
    down_q = asyncio.Queue(maxsize=DOWN_QUEUE_MAX)

    def peer_writer():
        try:
            while not stopped.is_set():
                try:
                    data = up_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                if data is None:
                    break

                channel.send(proto.frame(st.sid, data))
        except Exception:
            pass
        finally:
            stopped.set()

    def peer_reader():
        try:
            while not stopped.is_set():
                try:
                    data = st.q.get(timeout=0.5)
                except queue.Empty:
                    continue

                if data is None:
                    break

                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_offer_down, down_q, data, log)
        except Exception:
            pass
        finally:
            stopped.set()

    async def mc_to_peer():
        try:
            while not stopped.is_set():
                data = await mc_reader.read(proto.BUFFER_SIZE)

                if not data:
                    return

                st.up += len(data)

                # 审计问题 6：这条字节**已经**从 Minecraft 读到了，正在往对端
                # 发。记账在这里（读到的时刻），和 st.up 同一个语义。
                if traffic is not None:
                    traffic._note_traffic(len(data), 0)

                while not stopped.is_set():
                    try:
                        up_q.put(data, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        finally:
            stopped.set()
            with contextlib.suppress(Exception):
                up_q.put_nowait(None)

    async def peer_to_mc():
        try:
            while not stopped.is_set():
                data = await down_q.get()

                if data is None:
                    return

                mc_writer.write(data)
                await mc_writer.drain()
                st.down += len(data)

                # 审计问题 6：写回 Minecraft 成功了才算"收到并交付"。
                if traffic is not None:
                    traffic._note_traffic(0, len(data))
        except Exception:
            return
        finally:
            stopped.set()

    threads = [
        threading.Thread(target=peer_writer, name="mclink-tx", daemon=True),
        threading.Thread(target=peer_reader, name="mclink-rx", daemon=True),
    ]

    tasks = [
        asyncio.ensure_future(mc_to_peer()),
        asyncio.ensure_future(peer_to_mc()),
    ]

    for thread in threads:
        thread.start()

    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.set()

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        # 摇醒还卡在队列上的线程，别让它们留到进程结束。
        with contextlib.suppress(Exception):
            up_q.put_nowait(None)
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(_offer_down, down_q, None, log)

        for thread in threads:
            thread.join(timeout=1.0)


def _offer_down(down_q, data, log):
    """在事件循环上投递一条"对端 -> Minecraft"的数据。满了就丢（TCP 重传）。"""
    if data is None:
        # 结束信号：清空队列，让下游的 `await down_q.get()` 立刻返回。
        with contextlib.suppress(Exception):
            while True:
                down_q.get_nowait()

        with contextlib.suppress(Exception):
            down_q.put_nowait(None)

        return

    try:
        down_q.put_nowait(data)
    except asyncio.QueueFull:
        try:
            log(
                "[direct] peer->Minecraft queue full, dropped %d bytes "
                "(TCP will retransmit)" % len(data)
            )
        except Exception:
            pass


__all__ = [
    "PeerLink",
    "DirectCarrier",
    "CONNECT_TIMEOUT_S",
]
