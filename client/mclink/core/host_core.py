"""
MC-Link client -- HOST core.

============================================================================
 FROZEN FILE -- the TCP mechanics below are the verified ones.
============================================================================

Behaviour is copied from `归档/host_client.py`:

  1. One CONTROL connection:  "HOST <room> <client_id> <username>\\n"
                              <- "OK HOST room=<room>"
  2. N (default 10) HOST_TUNNEL connections, each:
         "HOST_TUNNEL <room>\\n"   <- "WAIT"
                                   <- "START <session_id>"
         then connect 127.0.0.1:<mc_port>
         then send "READY\\n"
         then raw bidirectional TCP copy with a 64 KiB buffer.
  3. Every tunnel reconnects automatically after RECONNECT_DELAY seconds.
  4. TCP_NODELAY is set on every socket.

ADDED IN V1 (does not touch the data path):
  * The control connection now answers PING with PONG and parses INFO lines.
    The control socket carries NO Minecraft data, so this is safe.
  * Status counters so the GUI can display live state.
  * stop() performs a complete, deterministic shutdown.

The Minecraft data path (`relay_mc`) is byte-identical to the verified build.
"""

import asyncio
import contextlib
import time

from .common import (
    BUFFER_SIZE,
    RateMeter,
    close_writer,
    enable_tcp_nodelay,
    now,
)
from . import protocol as proto


class _TunnelRetry(Exception):
    """
    Internal signal: "this tunnel attempt failed in a way that is EXPECTED".

    Used for the two states the relay legitimately reports while a room is
    being (re)created:

        ERROR NO_HOST   -> the room has no host yet
        (socket closed) -> the relay dropped an idle tunnel

    They must not be logged as errors: the client retries automatically and
    the user would otherwise see an endless error storm.
    """


class HostCore:
    """Raw, GUI independent host implementation."""

    def __init__(
        self,
        server_host,
        server_port,
        room,
        mc_host="127.0.0.1",
        mc_port=proto.DEFAULT_MC_PORT,
        max_tunnels=proto.DEFAULT_MAX_TUNNELS,
        max_players=proto.DEFAULT_MAX_PLAYERS,
        client_id="",
        username="",
        token="",
        reconnect_delay=3.0,
        on_log=None,
        on_event=None,
        direct_hook=None,
        direct_path_provider=None,
        direct_path_releaser=None,
        direct_backend_setter=None,
        on_traffic=None,
        auto_manager=None,
    ):
        self.auto_manager = auto_manager
        #: V3 STUN 模式：拿一条**已经打通**的 UDP 通道来承载这个会话。
        #:
        #: 可调用对象，签名 () -> Path 或 None。房间级打洞（room_direct.py）
        #: 在**点「开始连接」时**就打通并建好长连接隧道（3.0.3），这里只是
        #: 把它取来用。
        #:
        #: None / 返回 None 时这一整块代码不存在，行为与 v2 逐字节相同：
        #: 中继隧道照常建、照常转发。直连永远只是"多一条更快的路"，
        #: 不是"少一条能用的路"。
        self.direct_path_provider = direct_path_provider

        #: Called when a directly-carried connection ends, so the punched hole
        #: goes back into the pool for the next one. See release_path().
        self.direct_path_releaser = direct_path_releaser

        #: (host, port) -> None：把本机 Minecraft 的地址配到**每一条**直连链路
        #: 上（不是会话拿到的那一条）。多玩家时每个玩家各有一条链路，而会话
        #: 和中继配对不携带"这是哪个玩家"的信息 —— 只配一条，其他人的 sid 流
        #: 到了自己的链路上却没人去连本机世界（多人测试实测抓到的第三个玩家
        #: 连不上）。
        self.direct_backend_setter = direct_backend_setter

        #: V3 STUN 模式：中继隧道建立之后，拿它去尝试升级成直连。
        #: 可调用对象，签名 (session_id) -> awaitable，返回 True 表示打通。
        #:
        #: 关键设计：**它是可选的后置步骤，不是前置条件**。None 时这一整块
        #: 代码不存在，行为与 v2 逐字节相同；即使它失败/超时，游戏数据也一直
        #: 在已经建立的中继通路上流动，所以回落是"什么都没发生"，而不是一次
        #: 切换（没有切换窗口，也就没有断线窗口）。
        self.direct_hook = direct_hook

        #: 审计问题 6：`fn(mode, up, down)` —— 进程级流量账本。中转泵记 relay，
        #: 直连承载记 direct，**按当时真正在用的通路**打标。None 时不存在。
        self.on_traffic = on_traffic
        self._ledger_last = [0, 0]

        #: 正在跑的直连尝试任务（每个隧道同时只允许一个）。
        self._direct_task = None

        self.server_host = server_host
        self.server_port = int(server_port)

        self.room = room
        self.mc_host = mc_host
        self.mc_port = int(mc_port)

        self.max_tunnels = int(max_tunnels)

        #: 版本门拒绝的原因（非空 = 致命：界面必须弹「需要更新」并停止）。
        #: 单独一个字段，而不是只看 last_error —— 后者会被后面每一次重连
        #: 覆盖，版本不一致这件事必须一直被记住。
        self.version_rejected = ""

        #: Room capacity, host included. Separate from `max_tunnels`: the
        #: tunnel count is an implementation detail that the relay manages
        #: itself, while this is what the user configured and what the UI
        #: must show.
        self.max_players = int(max_players)

        self.client_id = client_id
        self.username = username

        # V2: login token. Empty means "no account system" (legacy mode).
        # It is an OPTIONAL extra field on the handshake line, so the frozen
        # protocol stays valid for servers that do not know about it.
        self.token = token

        # Set when the relay answers AUTH_INVALID_TOKEN / AUTH_DISABLED so the
        # GUI can ask the user to log in again instead of retrying forever.
        self.auth_error = ""

        self.reconnect_delay = float(reconnect_delay)

        # Callbacks. They must never block (the GUI uses a queue).
        self.on_log = on_log
        self.on_event = on_event

        # --------------------------------------------------------
        # Runtime state
        # --------------------------------------------------------

        self.running = False
        self.stopping = False

        self.control_reader = None
        self.control_writer = None

        self.control_online = False
        self.control_connected_at = None

        # V2: room deletion over the control channel.
        self.delete_result = None
        self._delete_event = None
        self._delete_pending = False

        #: 当前这条 DELETE 请求的应答截止时间（monotonic）。0 = 没有在等待。
        #: 请求本身没有请求号，所以"这条应答是不是给我的"只能靠它判断。
        self._delete_deadline = 0.0

        self.tunnel_tasks = []

        # tunnel_number -> True when "READY / WAITING"
        self.tunnel_ready = {}

        # session ids currently being served
        self.active_sessions = set()

        # ---- traffic statistics ----------------------------------
        # Live per-session byte counters (reset when all players leave).
        self.live_bytes_to_relay = 0
        self.live_bytes_to_mc = 0

        # Cumulative totals since the host session started.
        self.total_bytes_to_relay = 0
        self.total_bytes_to_mc = 0

        # Real time speed meters (bytes/second), sampled by status_loop().
        self._meter_up = RateMeter()
        self._meter_down = RateMeter()

        self.upload_rate = 0.0
        self.download_rate = 0.0
        self.peak_upload_rate = 0.0
        self.peak_download_rate = 0.0

        self.lock = asyncio.Lock()

        self._tasks = []

        self.last_error = ""

        self.relay_ping_ms = None

    # --------------------------------------------------------
    # Logging
    # --------------------------------------------------------

    def log(self, message):
        if self.on_log is not None:
            with contextlib.suppress(Exception):
                self.on_log(message)

    def emit(self, name, **data):
        if self.on_event is not None:
            with contextlib.suppress(Exception):
                self.on_event(name, data)

    # --------------------------------------------------------
    # Snapshot for the GUI
    # --------------------------------------------------------

    def snapshot(self):
        ready = sum(1 for v in self.tunnel_ready.values() if v)
        return {
            "role": "host",
            "room": self.room,
            "server": f"{self.server_host}:{self.server_port}",
            "mc_endpoint": f"{self.mc_host}:{self.mc_port}",
            "control_online": self.control_online,
            "tunnels_ready": ready,
            "tunnels_total": self.max_tunnels,
            "players": len(self.active_sessions),
            # The ROOM CAPACITY, which is not the same thing as the number of
            # tunnels. This reported `max_tunnels`, so a room configured for 8
            # people displayed "0 / 10" -- the tunnel count from an old config
            # file -- no matter what the user set. Two different numbers that
            # happened to be equal in the original design, which is exactly how
            # they drifted apart unnoticed.
            "max_players": self.max_players,
            "relay_ping_ms": self.relay_ping_ms,
            # ---- traffic ----
            "bytes_to_relay": self.total_bytes_to_relay,
            "bytes_to_minecraft": self.total_bytes_to_mc,
            "session_bytes_to_relay": self.live_bytes_to_relay,
            "session_bytes_to_minecraft": self.live_bytes_to_mc,
            "upload_rate": self.upload_rate,
            "download_rate": self.download_rate,
            "peak_upload_rate": self.peak_upload_rate,
            "peak_download_rate": self.peak_download_rate,
        }

    # --------------------------------------------------------
    # Traffic accounting
    # --------------------------------------------------------

    def note_traffic(self, to_relay, to_mc):
        """
        Called by relay_mc() when a session ends (and periodically via the
        sample loop). Only plain integer arithmetic -- no protocol impact.
        """
        self.total_bytes_to_relay += int(to_relay)
        self.total_bytes_to_mc += int(to_mc)

    def sample_rates(self):
        """
        Recompute the live upload / download speeds.

        Called about once per second by status_loop(). This never touches a
        socket; it only reads atomically updated counters.
        """
        self._meter_up.update(self.live_bytes_to_relay)
        self._meter_down.update(self.live_bytes_to_mc)

        self.upload_rate = self._meter_up.sample()
        self.download_rate = self._meter_down.sample()

        if self.upload_rate > self.peak_upload_rate:
            self.peak_upload_rate = self.upload_rate

        if self.download_rate > self.peak_download_rate:
            self.peak_download_rate = self.download_rate

        self._note_relay_traffic()

    def _note_relay_traffic(self):
        """
        把**新增的**中转字节交给进程级账本（审计问题 6）。

        用 total_* 的差值：账本是累计的（离开房间不清零），而这两个计数器只
        活在这个 Core 里。差值保证每个字节只记一次；换会话重置时不会记负数。
        """
        callback = self.on_traffic

        if callback is None:
            return

        up = int(self.total_bytes_to_relay)
        down = int(self.total_bytes_to_mc)
        last_up, last_down = self._ledger_last

        delta_up = up - last_up if up >= last_up else up
        delta_down = down - last_down if down >= last_down else down

        self._ledger_last = [up, down]

        if delta_up <= 0 and delta_down <= 0:
            return

        try:
            callback("relay", max(0, delta_up), max(0, delta_down))
        except Exception:
            pass

    # --------------------------------------------------------
    # Control connection
    # --------------------------------------------------------

    async def connect_control(self):
        """
        Keep trying until the control connection is up.

        The handshake is the frozen "HOST <room>\\n" line (plus the additive
        client_id / username fields, which old servers ignore).
        """
        while self.running:

            try:
                self.log(
                    f"[CONTROL] connecting to relay "
                    f"{self.server_host}:{self.server_port} ..."
                )

                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self.server_host,
                        self.server_port,
                    ),
                    timeout=15,
                )

                enable_tcp_nodelay(writer)

                # ---- FROZEN handshake -----------------------
                # 协议里那个字段是 room token（房间名编码后的 ASCII 记号）：
                # 房间名可以带中文和空格，直接发会把这一行切断。
                from ..config import settings as _cfg

                line = f"{proto.ROLE_HOST} {proto.room_token(self.room)}"
                if self.client_id:
                    line += f" {self.client_id}"
                    if self.username:
                        line += f" {self.username}"

                # V2 additive field. A server without accounts never sees it
                # because we only append it when we actually have a token.
                if self.token:
                    if not self.client_id:
                        line += " -"
                    if not self.username:
                        line += " -"
                    line += f" {self.token}"

                # 版本门：`ver=` 挂在**整行最后一个 token**，前面的位置字段
                # 一个都不挪。
                #
                # ------------------------------------------------------------------
                # 这一行漏掉过，用户就是这么连不上的：
                #   房主的控制连接（`HOST <room>`）和 `HOST_TUNNEL` / `PLAYER`
                #   是分开写的三处，当时只给后两处加了 `ver=`。结果版本完全
                #   一致的客户端也被服务端判成"版本不对"（回复里 `client=-`），
                #   而客户端把它当普通连接错误一直重连 —— 界面上只有日志里一行
                #   看不懂的错，没有弹窗，连接永远转圈。
                #
                # 教训：**握手行有几处，就得一起改**，并且必须有"版本一致时
                # 每一条握手行都能过门"的真客户端测试（见 version_gate_test
                # 里的 client_handshakes_pass_the_gate）。
                # ------------------------------------------------------------------
                if self.auto_manager is not None:
                    line += " caps=auto_stream_v1"
                line += f" ver={_cfg.APP_VERSION}"

                writer.write((line + "\n").encode())
                # ---------------------------------------------

                await writer.drain()

                response = await asyncio.wait_for(reader.readline(), timeout=15)

                if not response:
                    raise ConnectionError("中转服务器关闭了控制连接")

                text = response.decode(errors="replace").strip()

                if text.startswith(proto.LINE_ERROR):
                    error_text = proto.describe_error(text)

                    # 版本门拒绝也是**致命的、不许重连**：版本不对，重连一万次
                    # 还是不对；而且用户必须立刻看到「需要更新」的弹窗，而不是
                    # 一个永远转圈的界面。
                    if proto.is_version_mismatch(text):
                        self.version_rejected = error_text
                        self.running = False
                        self.log(f"[CONTROL] version rejected: {error_text}")
                        self.emit("version_rejected", reason=error_text)
                        raise ConnectionError(error_text)

                    # A login problem must stop the retry loop: hammering the
                    # relay with an invalid token is pointless and would look
                    # like an attack.
                    if any(
                        code in text
                        for code in (
                            proto.ERR_AUTH_REQUIRED,
                            proto.ERR_AUTH_INVALID_TOKEN,
                            proto.ERR_AUTH_DISABLED,
                            proto.ERR_AUTH_DISABLED_SERVER,
                        )
                    ):
                        self.auth_error = text.split()[1].upper() if len(
                            text.split()
                        ) > 1 else "AUTH_REQUIRED"
                        self.running = False
                        self.log(f"[CONTROL] login is no longer valid: {error_text}")
                        self.emit("auth_failed", reason=error_text)
                        raise ConnectionError(error_text)

                    raise ConnectionError(error_text)

                if not text.startswith(proto.LINE_ACK_HOST):
                    raise ConnectionError(f"控制连接应答异常: {text}")

                self.control_reader = reader
                self.control_writer = writer
                self.control_online = True
                self.control_connected_at = asyncio.get_event_loop().time()

                self.log(f"[CONTROL] control connection established room={self.room}")
                self.emit("control_online")

                return

            except asyncio.CancelledError:
                raise

            except Exception as e:
                self.control_online = False
                self.last_error = str(e)
                self.log(f"[CONTROL] connection failed: {e}")

                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(self.reconnect_delay)

    async def control_loop(self):
        while self.running:

            try:
                await self.connect_control()

                while self.running:
                    data = await self.control_reader.read(4096)

                    if not data:
                        raise ConnectionError("中转控制连接被关闭")

                    self.handle_control_data(data)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                self.control_online = False
                self.log(f"[CONTROL] control connection closed: {e}")
                self.emit("control_offline", reason=str(e))

                await close_writer(self.control_writer)

                self.control_reader = None
                self.control_writer = None
                self.relay_ping_ms = None

                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(self.reconnect_delay)

        self.control_online = False

    def handle_control_data(self, data):
        """
        Parse control lines: PONG / INFO.

        NOTE: this is the CONTROL socket only. A data tunnel never reaches
        this code path.
        """
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return

        for raw_line in text.splitlines():

            line = raw_line.strip()

            if not line:
                continue

            upper = line.upper()

            if upper.startswith(proto.LINE_PONG):
                self.on_pong(line)

            elif upper.startswith(proto.LINE_INFO):
                self.on_info(line)

            # NOTE: match "OK DELETE", never the bare "DELETE" -- the client
            # writes "DELETE <token>" on this same socket, and treating the
            # echo of our own request as a reply made every deletion wait for
            # a timeout instead of reading the real answer.
            #
            # 而且只有"我们确实还在等这一次删除的应答"时才算数：DELETE 的应答
            # 里没有请求号，超时之后到达的 ERROR（可能是别的原因）绝不能被当成
            # 删除结果读进去。见 delete_request_pending()。
            elif self.delete_request_pending() and (
                upper.startswith(proto.LINE_ACK_DELETE)
                or upper.startswith(proto.LINE_ERROR)
            ):
                self.on_delete_reply(line)

    # --------------------------------------------------------
    # V2: delete my own room
    # --------------------------------------------------------

    async def delete_own_room(self, wait_seconds=6):
        """
        Ask the relay to destroy this room, using the CONTROL socket.

        Returns (ok, message, wait_event). The caller awaits `wait_event` to
        learn the outcome (the control loop resolves it when the reply
        arrives).

        NOTE: this only works while the room has no players. The relay refuses
        with DELETE_ROOM_BUSY otherwise, and that refusal is what the GUI shows.
        """
        if not self.control_online or self.control_writer is None:
            return False, "控制连接未建立，无法删除房间", None

        self.delete_result = None
        self._delete_event = asyncio.Event()
        self._delete_pending = True

        # 这次请求的"身份"：调用方等待的那个 Event + 应答截止时间。应答只认
        # 还在等待期内的那一条请求（协议里没有请求号，只能这样判）。
        self._delete_deadline = time.monotonic() + max(
            1.0, float(wait_seconds)
        )

        try:
            self.control_writer.write(
                f"{proto.LINE_DELETE} {self.token}\n".encode()
            )
            await self.control_writer.drain()
        except Exception as e:
            # 请求根本没发出去：那就没有"这次删除的应答"这回事，别让后来
            # 任何一行 ERROR 被算到它头上。
            self.abandon_delete_request(self._delete_event)

            return False, f"发送删除请求失败：{e}", None

        self.log("[CONTROL] room deletion requested; waiting for the server to confirm ...")

        return True, "", self._delete_event

    def delete_request_pending(self):
        """
        我发出的 DELETE 现在还在等应答吗？

        ============================================================================
         DELETE 的应答里没有请求号，只能靠"我还在等"来判断归属
        ============================================================================

        "OK DELETE" / "ERROR ..." 都不带请求标识，所以一条应答是不是给这次
        删除的，唯一能问的就是：我们确实发了请求，而且还没超过等待期。

        以前这里只有一个布尔标记，而且超时之后没有人把它放下来 —— 于是下一条
        无关的 ERROR（例如令牌失效）会被 on_delete_reply() 当成删除结果读进去，
        用户看到的可能是上一次删除的（甚至是别人的）结论。等待期一过就自动作废，
        迟到的应答按"不是给我的"处理。
        """
        if not self._delete_pending:
            return False

        if time.monotonic() >= self._delete_deadline:
            self.abandon_delete_request()
            return False

        return True

    def abandon_delete_request(self, event=None):
        """
        放弃这次 DELETE 的应答：调用方等超时了，或者请求已经过期。

        `event` 是调用方正在等待的那个 Event —— 它必须还是当前这次请求的，
        否则说明期间又发了新请求，不能顺手把新请求一起作废。
        """
        if event is not None and self._delete_event is not event:
            return

        self._delete_pending = False
        self._delete_deadline = 0.0

        # 迟到的答复不能再被下一次删除调用读到。
        self.delete_result = None

    def on_delete_reply(self, line):
        """Handle OK DELETE / ERROR ... on the control socket."""
        upper = line.upper()

        if upper.startswith(proto.LINE_ACK_DELETE):
            self.delete_result = (True, "房间已删除")
        else:
            self.delete_result = (
                False,
                proto.describe_error(line) if upper.startswith(proto.LINE_ERROR)
                else line,
            )

        event = getattr(self, "_delete_event", None)

        if event is not None:
            event.set()

        self._delete_pending = False
        self._delete_deadline = 0.0

        self.emit(
            "delete_result",
            ok=bool(self.delete_result and self.delete_result[0]),
            message=self.delete_result[1] if self.delete_result else "",
        )

    # --------------------------------------------------------
    # Keep-alive (control channel only)
    # --------------------------------------------------------

    async def ping_loop(self):
        """
        Send PING on the CONTROL connection every few seconds.

        This measures the real relay round trip time and keeps the control
        socket from being silently dropped by NAT.
        """
        while self.running:

            try:
                await asyncio.sleep(3)

                if not self.control_online or self.control_writer is None:
                    continue

                stamp = f"{asyncio.get_event_loop().time():.3f}"
                self.control_writer.write(f"PING {stamp}\n".encode())
                await self.control_writer.drain()

            except asyncio.CancelledError:
                raise

            except Exception:
                # The control loop will notice and reconnect.
                continue

    def on_pong(self, line):
        parts = line.split()

        if len(parts) < 2:
            return

        try:
            sent = float(parts[1])
        except (TypeError, ValueError):
            return

        rtt_ms = (asyncio.get_event_loop().time() - sent) * 1000.0

        if rtt_ms < 0 or rtt_ms > 60000:
            return

        self.relay_ping_ms = round(rtt_ms, 1)
        self.emit("ping", ping_ms=self.relay_ping_ms)

    def on_info(self, line):
        """Parse: INFO players=2/10 idle_tunnels=8 waiting_players=0"""
        info = {}

        for token in line.split()[1:]:
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            info[key.strip()] = value.strip()

        self.emit("info", **info)

    # --------------------------------------------------------
    # Real time speed sampling
    # --------------------------------------------------------

    async def sample_loop(self):
        """
        Refresh the upload / download speed figures once per second.

        Purely local accounting: it reads counters that the data path already
        maintains and never touches a socket or a protocol message.
        """
        while self.running:
            try:
                await asyncio.sleep(1.0)

                self.sample_rates()

                self.emit(
                    "rates",
                    upload=self.upload_rate,
                    download=self.download_rate,
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                continue

    # --------------------------------------------------------
    # Tunnel
    # --------------------------------------------------------

    async def tunnel_loop(self, tunnel_number):
        while self.running:

            reader = None
            writer = None
            mc_reader = None
            # `mc_writer` MUST be bound here, not only where it is assigned.
            # The `finally` below closes it, and when connecting to the local
            # Minecraft server fails (the ordinary "I forgot to open my world"
            # case) the assignment never happens -- so cleanup raised
            # `NameError: cannot access local variable 'mc_writer'` FROM INSIDE
            # `finally`, which replaced the real error. The user saw a silent
            # retry loop instead of "cannot connect to 127.0.0.1:25565", and the
            # log had no idea why nothing worked.
            mc_writer = None
            session_id = None

            try:
                self.log(f"[TUNNEL {tunnel_number:02d}] establishing tunnel ...")

                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        self.server_host,
                        self.server_port,
                    ),
                    timeout=15,
                )

                enable_tcp_nodelay(writer)

                # ---- FROZEN handshake -----------------------
                from ..config import settings as _cfg

                # 版本门：`ver=` 放在整行最后一个 token（服务端按位置解析时
                # 只有 room 是必须的，多一个尾部 token 不挪位）。
                if self.auto_manager is not None:
                    hello = (f"{proto.ROLE_HOST_TUNNEL} {proto.room_token(self.room)} "
                             f"{self.client_id or '-'} {self.username or '-'} {self.token or '-'} "
                             f"caps=auto_stream_v1 ver={_cfg.APP_VERSION}\n")
                else:
                    hello = (f"{proto.ROLE_HOST_TUNNEL} {proto.room_token(self.room)} "
                             f"ver={_cfg.APP_VERSION}\n")
                writer.write(hello.encode())
                # ---------------------------------------------

                await writer.drain()

                response = await asyncio.wait_for(reader.readline(), timeout=15)

                if not response:
                    raise ConnectionError("中转服务器关闭了隧道")

                text = response.decode(errors="replace").strip()

                if text.startswith(proto.LINE_ERROR):
                    # 同 §8.9：隧道握手被 `ERROR CONN_MODE_CHANGED … was_live=`
                    # 打断时，也要把那次性的 was_live 交给同一个消费者。
                    changed = proto.parse_conn_mode_changed(text)

                    if changed is not None:
                        changed_reason, was_live = changed
                        self.log(
                            f"[TUNNEL {tunnel_number:02d}] the relay says the "
                            f"room mode changed ({changed_reason or '-'}"
                            + (f", was_live={was_live}" if was_live else "")
                            + ")"
                        )
                        self.emit(
                            "conn_mode_changed",
                            reason=changed_reason,
                            was_live=was_live,
                        )

                    # 版本门拒绝：隧道也逃不过，而且**绝不许重连** ——
                    # 版本不对，重连一万次还是不对。记下来、喊出去，让界面
                    # 弹「需要更新」。
                    if proto.is_version_mismatch(text):
                        error_text = proto.describe_error(text)
                        self.version_rejected = error_text
                        self.running = False
                        self.log(f"[TUNNEL] version rejected: {error_text}")
                        self.emit("version_rejected", reason=error_text)
                        raise ConnectionError(error_text)

                    # Most common case: the room has no host yet (the control
                    # connection is still coming up). Retry quietly.
                    raise ConnectionError(proto.describe_error(text))

                if text != proto.LINE_WAIT:
                    raise ConnectionError(f"隧道应答异常: {text}")

                self.tunnel_ready[tunnel_number] = True
                self.log(f"[TUNNEL {tunnel_number:02d}] idle, waiting (READY / WAITING)")
                self.emit("tunnel_ready", tunnel=tunnel_number)

                # Wait until a player is assigned.
                start_line = await reader.readline()

                if not start_line:
                    # The relay closed the tunnel. This is NORMAL while the
                    # control connection is still coming up (or right after a
                    # disconnect): the room briefly has no host, so the relay
                    # drops idle tunnels. Retry quietly instead of logging an
                    # error storm.
                    raise _TunnelRetry("隧道在分配前被关闭")

                start_text = start_line.decode(errors="replace").strip()

                # The relay answers ERROR NO_HOST while the room is being
                # (re)created. That is a transient state, not a protocol error:
                # back off and try again without alarming the user.
                if start_text.upper().startswith(proto.LINE_ERROR):
                    # §8.9 等待池里的 `ERROR CONN_MODE_CHANGED … was_live=`：
                    # 换模式时这条隧道还在等玩家。was_live 是**一次性**事实
                    # （切换前这个成员有没有连接），必须交给和成员控制连接
                    # 同一个消费者，热切换判定才不会退回本地 running。
                    changed = proto.parse_conn_mode_changed(start_text)

                    if changed is not None:
                        changed_reason, was_live = changed
                        self.log(
                            f"[TUNNEL {tunnel_number:02d}] the relay says the "
                            f"room mode changed while idle "
                            f"({changed_reason or '-'}"
                            + (f", was_live={was_live}" if was_live else "")
                            + ")"
                        )
                        self.emit(
                            "conn_mode_changed",
                            reason=changed_reason,
                            was_live=was_live,
                        )

                    if proto.is_version_mismatch(start_text):
                        error_text = proto.describe_error(start_text)
                        self.version_rejected = error_text
                        self.running = False
                        self.log(f"[TUNNEL] version rejected: {error_text}")
                        self.emit("version_rejected", reason=error_text)
                        raise ConnectionError(error_text)

                    raise _TunnelRetry(proto.describe_error(start_text))

                parts = start_text.split()

                auto_info = None
                if self.auto_manager is not None:
                    from .auto_stream import parse_metadata
                    session_id, auto_info = parse_metadata(start_text, "START_AUTO")
                else:
                    if len(parts) != 2 or parts[0] != proto.LINE_START:
                        raise ConnectionError(f"隧道分配消息异常: {start_text}")
                    session_id = parts[1]
                self.tunnel_ready[tunnel_number] = False

                async with self.lock:
                    self.active_sessions.add(session_id)

                self.log(
                    f"[TUNNEL {tunnel_number:02d}] player assigned session={session_id}"
                )
                self.emit("session_start", session=session_id, tunnel=tunnel_number)

                # Connect the local Minecraft LAN world.
                self.log(
                    f"[TUNNEL {tunnel_number:02d}] connecting to local Minecraft "
                    f"{self.mc_host}:{self.mc_port} ..."
                )

                try:
                    mc_reader, mc_writer = await asyncio.wait_for(
                        asyncio.open_connection(self.mc_host, self.mc_port),
                        timeout=10,
                    )
                except Exception as e:
                    # Say the REAL reason. "The tunnel keeps idling" is not
                    # actionable; "Minecraft is not running on 127.0.0.1:25565"
                    # is, and it is by far the most common cause of a session
                    # that never starts.
                    self.log(
                        f"[TUNNEL {tunnel_number:02d}] cannot reach local "
                        f"Minecraft at {self.mc_host}:{self.mc_port} -- "
                        f"is the LAN world open? ({type(e).__name__}: {e})"
                    )
                    raise

                enable_tcp_nodelay(mc_writer)

                self.log(
                    f"[TUNNEL {tunnel_number:02d}] local Minecraft connected "
                    f"session={session_id}"
                )

                # ---- FROZEN: tell the relay we are ready ----
                writer.write(f"{proto.LINE_READY}\n".encode())
                await writer.drain()
                # ---------------------------------------------

                self.log(
                    f"[TUNNEL {tunnel_number:02d}] READY sent session={session_id}"
                )

                # ---- V3: try to upgrade this session to a direct path ----
                #
                # Fire-and-forget ON PURPOSE. The relay path below is already
                # carrying the game; waiting for a punch here would delay
                # Minecraft for no benefit, and a failed punch must cost
                # nothing but a few log lines.
                if self.direct_hook is not None:
                    self._spawn_direct(session_id)

                # Everything after this point is raw Minecraft TCP data.
                if auto_info is not None:
                    from .auto_client import run_stream
                    await run_stream(self, auto_info, session_id, True,
                                     mc_reader, mc_writer, reader, writer)
                else:
                    await self.relay_mc(
                        tunnel_number, session_id, reader, writer, mc_reader, mc_writer,
                    )

            except asyncio.CancelledError:
                raise

            except _TunnelRetry as e:
                # Expected while the room is being (re)created. Log once at low
                # volume so the UI stays readable.
                self.tunnel_ready[tunnel_number] = False
                self.retry_count = getattr(self, "retry_count", 0) + 1

                if self.retry_count <= 3 or self.retry_count % 20 == 0:
                    self.log(
                        f"[TUNNEL {tunnel_number:02d}] waiting for the host "
                        f"to be ready ... ({e})"
                    )

            except Exception as e:
                self.tunnel_ready[tunnel_number] = False
                self.log(f"[TUNNEL {tunnel_number:02d}] error: {e}")

            finally:
                self.tunnel_ready[tunnel_number] = False

                # Deterministic cleanup: the relay socket first, then the
                # local Minecraft socket. Both are idempotent.
                await close_writer(writer)
                await close_writer(mc_writer)

                if session_id:
                    async with self.lock:
                        self.active_sessions.discard(session_id)

                    self.log(
                        f"[TUNNEL {tunnel_number:02d}] session ended session={session_id}"
                    )
                    self.emit("session_end", session=session_id, tunnel=tunnel_number)

                if self.running:
                    self.log(
                        f"[TUNNEL {tunnel_number:02d}] "
                        f"reconnecting in {self.reconnect_delay:g}s ..."
                    )
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.sleep(self.reconnect_delay)

    def _spawn_direct(self, session_id):
        """
        Run the direct-path upgrade in the background.

        Never awaited by the tunnel loop: the Minecraft session is already
        alive on the relay, and blocking here to wait for a punch would make
        every connection slower for a feature that may not even work on this
        network. Failures are logged and nothing else happens.
        """
        if self._direct_task is not None and not self._direct_task.done():
            # One attempt per tunnel at a time. Re-running it while the first
            # is still spraying would double the packet rate for no gain.
            return

        async def run():
            try:
                ok = await self.direct_hook(session_id)

                if ok:
                    self.emit("direct_path", session=session_id, via="udp")
                else:
                    self.emit("direct_path", session=session_id, via="relay")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A direct path is an optimisation: it must never be able to
                # take down a working relay session.
                self.log(f"[direct] attempt failed (does not affect the connection): {e}")
                self.emit("direct_path", session=session_id, via="relay")

        self._direct_task = asyncio.create_task(run())

    # --------------------------------------------------------
    # MC <-> Relay raw TCP  (FROZEN -- do not restructure)
    # --------------------------------------------------------

    async def _try_direct_carry(
        self, tunnel_number, session_id, mc_reader, mc_writer, relay_reader=None
    ):
        """
        试着把本会话挂到房间级打洞的产物上。True 表示已经接管并搬完了。

        ============================================================================
          这一版换了什么（`HANDOFF-TO-NEXT-SESSION.md`）
        ============================================================================

        以前这里是"把会话挂到自研的 `reliable.ReliableStream` 上"—— 那个流
        **从来没在真实 NAT 上成功过**，而打洞那半是照抄的（确实能打通）。
        这就是"洞打通了但游戏连不上"的直接原因。

        现在换成参考工程验证过的 `udptunnel.UdpTunnel` + `channel.PeerChannel`：
        一条隧道、每条 Minecraft 连接一条 `sid` 流。本函数只负责**守住会话的
        生命周期**，游戏字节走哪条连接由链路自己按 sid 决定（`_host_stream`）。

        ============================================================================
          为什么房主这一侧"守"就够了
        ============================================================================

        房主这边每条 sid 的**本机 Minecraft 连接由链路自己开**（参考工程的做法，
        见 `direct_carry.PeerLink._host_stream`）。会话这一侧那条已经开好的
        `mc_reader/mc_writer` 反而用不上：它对应的是"中继的一次配对"，而 sid 是
        对端为每条 MC 连接分配的，两者没有可靠的对应关系。既然都是到同一个本机
        世界的新 TCP 连接，物理上等价，就没有正确性问题 —— 只是多开了一条。

        我们把它立刻关掉（世界是否可达已经在上面的连接里验证过了，
        `cannot reach local Minecraft` 那行日志照常会出），免得 LAN 世界为每个
        会话白留一条空闲连接。
        """
        from . import direct_carry

        try:
            link = self.direct_path_provider()
            # A provider may be a coroutine function (STUN mode waits for the
            # punched link rather than falling back to the relay).
            if asyncio.iscoroutine(link):
                link = await link
        except Exception as e:
            self.log(f"[direct] no direct link ({type(e).__name__}: {e})")
            return False

        if link is None:
            # ==================================================================
            #  STUN MODE DOES NOT PUT THE GAME ON THE RELAY. EVER.
            # ==================================================================
            #
            # The provider returns None only when there is nothing left to wait
            # for (the punch stopped, the peer is hopelessly symmetric, the room
            # ended). Relaying here would mean a screen that says "STUN" while
            # the bytes travel through the server -- which is exactly the thing
            # the owner has now told us about four times.
            #
            # So: refuse the session and say WHY, in one line, instead of
            # quietly producing the dishonest outcome.
            self.log(
                f"[TUNNEL {tunnel_number:02d}] STUN mode: no punched direct "
                f"link, and the relay does not carry game data in STUN mode "
                f"-- closing this session (switch to Relay mode to play "
                f"through the server)"
            )
            self.emit(
                "direct_failed",
                session=session_id,
                reason="stun_mode_no_direct_link",
            )
            return False

        carrier = direct_carry.DirectCarrier(
            link,
            session_id,
            is_host=True,
            log=self.log,
            # The card must say 直连 when a stream is ACTUALLY carrying bytes,
            # not when a hole merely exists. The link tells us when its
            # active-stream count crosses zero (see PeerLink.add_listener).
            on_activity=lambda active: self.emit(
                "direct_carrying", session=session_id, carrying=active
            ),
        )
        carrier.set_backend(self.mc_host, self.mc_port)

        # EVERY link, not just this one: the host has one link per player, and
        # this session does not say which player it is. Configure them all so a
        # late joiner's sid streams are served too (see direct_backend_setter).
        if self.direct_backend_setter is not None:
            with contextlib.suppress(Exception):
                self.direct_backend_setter(self.mc_host, self.mc_port)

        if not await carrier.open():
            return False

        self.log(
            f"[TUNNEL {tunnel_number:02d}] Minecraft data over the "
            f"DIRECT link session={session_id}"
        )

        try:
            # The host side hands its already-open local-Minecraft connection to
            # the link (see `direct_carry.Spare`: Minecraft's server speaks
            # first, so waiting for the player to send data before touching the
            # world deadlocks) and then holds the session open for exactly as
            # long as the relay pairing lasts -- that pairing IS this session's
            # lifetime, and the room counts "players in the world" from it.
            #
            # `relay_reader` is the parameter this function actually takes (the
            # tunnel loop passes its relay reader down as `tunnel_reader`).
            # Passing the non-existent name `tunnel_reader` made EVERY host-side
            # direct carry raise NameError the instant it took over -- the hole
            # was punched, the game was handed to the link, and the session died
            # one line later ("洞打通了但游戏秒掉线", log3 23:58:09.631).
            await carrier.pump(mc_reader, mc_writer, relay_reader=relay_reader)
        finally:
            self.emit("direct_carrying", session=session_id, carrying=False)

            # Kept for the session layer's hook (a no-op now: links are shared).
            if self.direct_path_releaser is not None:
                with contextlib.suppress(Exception):
                    self.direct_path_releaser()

        return True

    async def relay_mc(
        self,
        tunnel_number,
        session_id,
        tunnel_reader,
        tunnel_writer,
        mc_reader,
        mc_writer,
    ):
        # ============================================================================
        #  V3 直连：这个会话能不能走那条**已经打通**的 UDP 通道
        # ============================================================================
        #
        # 房间级打洞在**进房时**就已经打通并保住了那条通道（他的流程：
        # "进房就打洞，打通了再挂游戏"）。这里只是把它取来用。
        #
        # STUN MODE: this branch EXISTS instead of the relay, it does not
        # fall through to it.
        #
        # Relay mode is untouched below (the provider is None there and not one
        # line of the old path runs). But in STUN mode the owner's rule is
        # absolute: "不允许在手动 stun 状态下握手中继的游戏 tcp 连接". So a
        # failure here means the session is refused with a reason, not quietly
        # carried by the server while the screen says "direct".
        if self.direct_path_provider is not None:
            carried = await self._try_direct_carry(
                tunnel_number, session_id, mc_reader, mc_writer, tunnel_reader
            )

            if carried:
                return

            # Nothing to fall back to BY DESIGN. The relay also refuses to pair
            # game data for a STUN room (Room.try_pair), so continuing would
            # only produce a dead session.
            return

        self.log(
            f"[TUNNEL {tunnel_number:02d}] Minecraft data forwarding started "
            f"session={session_id}"
        )

        stats = {"to_relay": 0, "to_mc": 0}

        # ============================================================================
        #  实时速度是【所有会话的合计】，不是"最后一个写进去的数字"
        # ============================================================================
        #
        # live_bytes_* 是当前所有正在转发数据的会话的合计，界面上的"实时速度"
        # 就是对它采样（累计流量走的是 total_*，一直是准的）。
        #
        # 以前每个会话都写 `base + 自己的字节数`，而 base 是会话开始那一刻的值：
        # 两个玩家同时玩时，两条隧道互相覆盖同一个计数器，屏幕上的速度就在两个
        # 数字之间来回跳。现在改成各自加、退出时各自减 —— 数字只增不减地代表
        # "当前所有会话"，所有玩家走了自然回到 0。
        async def mc_to_relay():
            try:
                while True:
                    data = await mc_reader.read(BUFFER_SIZE)

                    if not data:
                        return

                    tunnel_writer.write(data)
                    await tunnel_writer.drain()

                    stats["to_relay"] += len(data)
                    self.live_bytes_to_relay += len(data)

            except asyncio.CancelledError:
                raise

            except Exception:
                return

        async def relay_to_mc():
            try:
                while True:
                    data = await tunnel_reader.read(BUFFER_SIZE)

                    if not data:
                        return

                    mc_writer.write(data)
                    await mc_writer.drain()

                    stats["to_mc"] += len(data)
                    self.live_bytes_to_mc += len(data)

            except asyncio.CancelledError:
                raise

            except Exception:
                return

        task1 = asyncio.create_task(mc_to_relay())
        task2 = asyncio.create_task(relay_to_mc())

        try:
            done, pending = await asyncio.wait(
                [task1, task2],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            await asyncio.gather(*pending, return_exceptions=True)

        finally:
            task1.cancel()
            task2.cancel()

            await asyncio.gather(task1, task2, return_exceptions=True)

            await close_writer(mc_writer)
            await close_writer(tunnel_writer)

            # 自己的那一份从"当前会话合计"里减去（上面说过：这个是合计，
            # 不是某个会话的。减完所有玩家都走了就自然回到 0）。
            self.live_bytes_to_relay -= stats["to_relay"]
            self.live_bytes_to_mc -= stats["to_mc"]

            self.note_traffic(stats["to_relay"], stats["to_mc"])

            self.log(
                f"[TUNNEL {tunnel_number:02d}] Minecraft data forwarding ended "
                f"session={session_id} "
                f"up={stats['to_relay']}B down={stats['to_mc']}B"
            )

    # --------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------

    async def start(self):
        if self.running:
            return

        self.running = True
        self.stopping = False
        self.last_error = ""

        self.log("================================================")
        self.log("              MC-LINK  Host")
        self.log("================================================")
        self.log(f"relay server : {self.server_host}:{self.server_port}")
        self.log(f"room         : {self.room}")
        self.log(f"local MC     : {self.mc_host}:{self.mc_port}")
        self.log(f"tunnels      : {self.max_tunnels}")
        self.log("================================================")

        self._tasks = []

        control_task = asyncio.create_task(self.control_loop())
        ping_task = asyncio.create_task(self.ping_loop())
        sample_task = asyncio.create_task(self.sample_loop())
        self._tasks.extend([control_task, ping_task, sample_task])

        # Give the control connection a moment to come up first, so the
        # tunnel pool finds a live host (same as the verified build).
        await asyncio.sleep(0.5)

        for tunnel_number in range(1, self.max_tunnels + 1):
            task = asyncio.create_task(self.tunnel_loop(tunnel_number))
            self.tunnel_tasks.append(task)
            self._tasks.append(task)

            # Small delay prevents all tunnels connecting at the exact
            # same instant (same as the verified build).
            await asyncio.sleep(0.1)

        self.log("host started; waiting for players to connect.")
        self.emit("started")

    async def stop(self, graceful=False):
        """
        停掉开服。

        `graceful=True` 才表示"**用户点的停止**" —— 只有这条路会先发一行
        `STOPPING` 给中继（见下面那段）。模式热切换的重建走的是同一段代码，
        但它是"我要按新模式重新开服"，不是"我不开服了"；把这两件事混为一谈，
        会让中继立刻广播 `HOST_STOPPED` 把全房间踢下线（v3.9.16 的 P1）。
        """
        if self.stopping:
            return

        self.stopping = True
        self.running = False

        self.log("stopping the host ...")

        tasks = list(self._tasks)
        self._tasks = []
        self.tunnel_tasks = []

        for task in tasks:
            if not task.done():
                task.cancel()

        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=6,
                )
            except (asyncio.TimeoutError, Exception):
                pass

        # ============================================================================
        #  ★ v3.9.17：关连接**之前**先说一句"我是主动停的"——**只有**主动停才说
        # ============================================================================
        #
        # 中继以前分不清"房主点了停止"和"房主掉线/崩了/拔网线" —— 这两种在日志
        # 和给其他玩家的原因里是**同一句话**（`host 已断开连接（未开服）`），
        # 排障时只能靠猜。
        #
        # 这一行是 ADDITIVE：老中继不认，按"未知行"忽略；老客户端不发，中继
        # 的 `room.host_stopping` 保持 False，行为和以前一字不差。
        #
        # ============================================================================
        #  ★★ v3.9.16 犯的错（复核报告 P1）：这一行以前是**无条件**发的
        # ============================================================================
        #
        # 而 `stop()` 不只"用户点停止"会走 —— 模式热切换的重建也走它
        # （`_rebuild_tunnels` → `engine.stop_session()` → `session.stop()`）。
        # 于是房主只要正常停过一次，`room.host_stopping` 就 **永不复位**，
        # 之后这个房间的 2.5 秒宽限窗口**永久失效**：掉线、崩溃、模式重建
        # 全部立刻广播 `HOST_STOPPED`，而且措辞还被错报成"主动停止"。
        #
        # 所以 `graceful` 必须由调用方显式传下来，这里不再自己猜。
        #
        # 写在关闭**之前**，因为关了就发不出去了；失败也不必重试 —— 它只是
        # 归因信息，发不出去最多还是以前那句措辞。
        if graceful and self.control_writer is not None:
            with contextlib.suppress(Exception):
                self.control_writer.write(b"STOPPING\n")
                await asyncio.wait_for(
                    self.control_writer.drain(), timeout=2
                )

        await close_writer(self.control_writer)

        self.control_writer = None
        self.control_reader = None
        self.control_online = False
        self.tunnel_ready.clear()
        self.relay_ping_ms = None

        self.upload_rate = 0.0
        self.download_rate = 0.0

        async with self.lock:
            self.active_sessions.clear()

        self.log("host stopped.")
        self.emit("stopped")
