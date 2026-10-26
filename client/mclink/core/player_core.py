"""
MC-Link client -- PLAYER core.

============================================================================
 FROZEN FILE -- the TCP mechanics below are the verified ones.
============================================================================

Behaviour is copied from `归档/player_client.py`:

  1. Listen on 127.0.0.1:<local_port> (default 25566). Minecraft connects here.
  2. For EVERY incoming Minecraft connection open a NEW socket to the relay:
         "PLAYER <room> <client_id> <username>\\n"  <- "CONNECTED"
  3. After CONNECTED: raw bidirectional TCP copy (64 KiB buffer).
  4. TCP_NODELAY on every socket.

ADDED IN V1 (no change to the data path):
  * live counters for the GUI (active connections, bytes in/out)
  * register/unregister of connection tasks so stop() is deterministic
"""

import asyncio
import contextlib

from .common import (
    BUFFER_SIZE,
    RateMeter,
    close_writer,
    enable_tcp_nodelay,
)
from . import protocol as proto


class PlayerCore:
    """Raw, GUI independent player implementation."""

    def __init__(
        self,
        server_host,
        server_port,
        room,
        local_host="127.0.0.1",
        local_port=proto.DEFAULT_LOCAL_PORT,
        client_id="",
        username="",
        token="",
        connect_timeout=10,
        on_log=None,
        on_event=None,
        direct_hook=None,
        direct_path_provider=None,
        direct_path_releaser=None,
        on_traffic=None,
        auto_manager=None,
    ):
        self.auto_manager = auto_manager
        #: V3 STUN 模式：一条**已经打通**的 UDP 通道（房间级打洞的产物）。
        #: 见 host_core 里同名参数的说明：None 时行为与 v2 完全相同。
        self.direct_path_provider = direct_path_provider

        #: 审计问题 6：`fn(mode, up, down)` —— 进程级流量账本。relay 泵记
        #: relay、直连承载记 direct，**按当时真正在用的通路**打标（绝不读
        #: config.mode，它会漂移）。None 时这一块整个不存在。
        self.on_traffic = on_traffic
        self._ledger_last = [0, 0]

        #: Called when a directly-carried connection ends, so the punched hole
        #: goes back into the pool for the next one. See release_path().
        self.direct_path_releaser = direct_path_releaser

        #: V3 STUN 模式：中继配对成功之后，拿它去尝试升级成直连。
        #: None 时行为与 v2 完全相同（这块代码不存在）。
        self.direct_hook = direct_hook
        self._direct_task = None

        self.server_host = server_host
        self.server_port = int(server_port)

        self.room = room

        self.local_host = local_host
        self.local_port = int(local_port)

        #: 版本门拒绝的原因（非空 = 致命：界面必须弹「需要更新」并停止）。
        self.version_rejected = ""

        self.client_id = client_id
        self.username = username

        # V2: login token (optional extra handshake field).
        self.token = token
        self.auth_error = ""

        self.connect_timeout = float(connect_timeout)

        self.on_log = on_log
        self.on_event = on_event

        # --------------------------------------------------------
        # Runtime state
        # --------------------------------------------------------

        self.running = False
        self.server = None

        self.connection_count = 0
        self.active_connections = 0

        self.bytes_to_relay = 0
        self.bytes_to_minecraft = 0

        # Real time speed meters (bytes/second), sampled by sample_loop().
        self._meter_up = RateMeter()
        self._meter_down = RateMeter()

        self.upload_rate = 0.0
        self.download_rate = 0.0
        self.peak_upload_rate = 0.0
        self.peak_download_rate = 0.0

        self.lock = asyncio.Lock()

        self._tasks = set()

        self.last_error = ""

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
        return {
            "role": "player",
            "room": self.room,
            "server": f"{self.server_host}:{self.server_port}",
            "local_endpoint": f"{self.local_host}:{self.local_port}",
            "listening": self.server is not None,
            "active_connections": self.active_connections,
            "total_connections": self.connection_count,
            "bytes_to_relay": self.bytes_to_relay,
            "bytes_to_minecraft": self.bytes_to_minecraft,
            "upload_rate": self.upload_rate,
            "download_rate": self.download_rate,
            "peak_upload_rate": self.peak_upload_rate,
            "peak_download_rate": self.peak_download_rate,
            "relay_ping_ms": None,
        }

    # --------------------------------------------------------
    # Real time speed sampling
    # --------------------------------------------------------

    def sample_rates(self):
        self._meter_up.update(self.bytes_to_relay)
        self._meter_down.update(self.bytes_to_minecraft)

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

        用差值而不是直接报累计值：账本是累计的（离开房间不清零），而
        `bytes_to_relay` 只活在这个 Core 里。差值保证了"每个字节只记一次"。
        """
        callback = self.on_traffic

        if callback is None:
            return

        up = int(self.bytes_to_relay)
        down = int(self.bytes_to_minecraft)
        last_up, last_down = self._ledger_last

        # 计数器被换会话重置时差值会是负的：那种情况把基线跟上去，不记负数。
        delta_up = up - last_up if up >= last_up else up
        delta_down = down - last_down if down >= last_down else down

        self._ledger_last = [up, down]

        if delta_up <= 0 and delta_down <= 0:
            return

        try:
            callback("relay", max(0, delta_up), max(0, delta_down))
        except Exception:
            pass

    async def sample_loop(self):
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
    # Connect to relay
    # --------------------------------------------------------

    async def connect_relay(self):
        self.log(
            f"[RELAY] connecting to relay "
            f"{self.server_host}:{self.server_port} ..."
        )

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.server_host,
                self.server_port,
            ),
            timeout=self.connect_timeout,
        )

        enable_tcp_nodelay(writer)

        # ---- FROZEN handshake -------------------------------
        # room token：房间名编码后的 ASCII 记号（房间名可能带中文/空格）。
        from ..config import settings as _cfg

        line = f"{proto.ROLE_PLAYER} {proto.room_token(self.room)}"
        if self.client_id:
            line += f" {self.client_id}"
            if self.username:
                line += f" {self.username}"

        # V2 additive field (only when we actually have a token).
        if self.token:
            if not self.client_id:
                line += " -"
            if not self.username:
                line += " -"
            line += f" {self.token}"

        # 版本门：`ver=` 放在**整行最后一个 token**，所以上面那些按位置解析的
        # 字段（room / client_id / username / token）一个都没挪位。
        if self.auto_manager is not None:
            line += " caps=auto_stream_v1"
        line += f" ver={_cfg.APP_VERSION}"

        writer.write((line + "\n").encode())
        # -----------------------------------------------------

        await writer.drain()

        self.log("[RELAY] PLAYER handshake sent")

        return reader, writer

    # --------------------------------------------------------
    # One Minecraft connection
    # --------------------------------------------------------

    #: 等中继配对时，允许本机 Minecraft 空闲多久（秒）。超时按旧行为处理
    #: （客户端本来就有 30 秒上限）。
    #: （客户端本来就有 30 秒上限）。
    PAIR_WAIT_S = 30.0

    class _PrependedReader:
        """
        把一个**已经读出来**的字节前缀塞回读取流的最前面。

        ============================================================================
          ★ 为什么必须有它（否则 STUN 模式会掉线 —— 这是本轮的 P0 自检）
        ============================================================================

        等中继配对时，为了盯住"MC 是不是已经走了"，我们**必须**从本机 MC 读
        （`at_eof()` 在有缓冲数据时永远是 False，光看不读识别不出对端关闭）。
        可是 MC 连上代理后**立刻**就发握手包，于是这段前缀一定会被提前读走。

        这些字节**只能跟着最终选中的那条通路走**：
          * Relay 模式 -> 走中继 socket（房主那边会把隧道的字节转发进世界）；
          * STUN 模式 -> **必须**走直连。房主在 STUN 模式下对中继隧道是
            "只读不转发"的（`direct_carry._drain_relay` 会把中继送来的游戏字节
            丢弃并记一行事故日志），所以把前缀丢给中继 = 握手包人间蒸发，
            直连流从半路开始 -> Minecraft 直接连不上。

        所以这里**不**往任何 socket 里写前缀，而是把它包成一个读取流交给
        `raw_relay()`：谁最后拿到这条通路，前缀就跟谁走。顺序、字节数都不变。
        """

        def __init__(self, prefix, reader):
            self._prefix = bytes(prefix or b"")
            self._reader = reader

        async def read(self, n=-1):
            if self._prefix:
                if n is None or n < 0:
                    chunk, self._prefix = self._prefix, b""
                else:
                    chunk, self._prefix = self._prefix[:n], self._prefix[n:]
                return chunk

            return await self._reader.read(n)

        def at_eof(self):
            # 前缀还没读完就不算 EOF（哪怕底层已经关了）。
            if self._prefix:
                return False

            try:
                return bool(self._reader.at_eof())
            except Exception:
                return False

        def __getattr__(self, name):
            return getattr(self._reader, name)

    async def _await_relay_or_mc(self, relay_reader, mc_reader, connection_id):
        """
        等中继的第一行，**同时**盯着本机 Minecraft 那条 socket。

        返回 `(relay_line, early_mc_bytes, mc_gone)`：

          * `relay_line`    —— 中继发来的第一行（bytes，可能为空 = 对端关了）；
          * `early_mc_bytes`—— 等待期间 MC 发来的字节（握手包），配对成功后必须先
                               补发给房主，一个字节都不能丢；
          * `mc_gone`       —— 本机 MC 已经关了这条连接（探测/刷新），调用方立刻收工。

        为什么要"盯着"：见 `handle_minecraft` 里那段说明（MC 刷新 -> 幽灵 PLAYER
        连接占满房间名额 -> 两人房也报「房间已满」）。

        **绝不吞数据**：读到 MC 的字节只是**暂存**并继续等中继，不丢、不解析
        （协议对客户端是不透明的 —— 规则：不发明、不解析 Minecraft 协议）。
        """
        relay_task = asyncio.ensure_future(relay_reader.readline())
        mc_task = asyncio.ensure_future(mc_reader.read(65536))
        pending = b""
        deadline = asyncio.get_running_loop().time() + float(self.PAIR_WAIT_S)

        try:
            while True:
                left = deadline - asyncio.get_running_loop().time()

                if left <= 0:
                    # 30 秒都没等到中继：和旧行为一致（抛出去给上层报错）。
                    raise asyncio.TimeoutError()

                done, _pending = await asyncio.wait(
                    {relay_task, mc_task},
                    timeout=left,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    continue

                if mc_task in done:
                    try:
                        chunk = mc_task.result()
                    except Exception:
                        chunk = b""

                    if not chunk:
                        # 本机 MC 走了（探测结束 / 刷新 / 玩家退出）：
                        # 立刻收工，把中继那条 socket 交给 finally 关掉。
                        return None, pending, True

                    pending += chunk
                    mc_task = asyncio.ensure_future(mc_reader.read(65536))
                    continue

                # 中继先回了。
                try:
                    line = relay_task.result()
                except Exception:
                    line = b""

                return line, pending, False
        finally:
            for task in (relay_task, mc_task):
                if task is not None and not task.done():
                    task.cancel()

    async def handle_minecraft(self, mc_reader, mc_writer):
        self.connection_count += 1
        connection_id = self.connection_count

        try:
            peer = mc_writer.get_extra_info("peername")
        except Exception:
            peer = "unknown"

        # Minecraft 确实连上了【本机代理】（这一句是真的），但"已经连上房主"
        # 还要等中继确认 —— 见下面。
        self.emit("minecraft_connected", connection=connection_id)

        # ============================================================================
        #  「已连接」只在【中继确认配对】之后才算数
        # ============================================================================
        #
        # 以前这里（Minecraft 刚连上本地代理的那一瞬间）就 active_connections += 1
        # 并写「Minecraft 已连接」。可是中继那边可能根本不认：账号被拒、房间里
        # 没有主机、或者 30 秒都没等到配对 —— 那些失败路径上，用户看到的是一个
        # "已经连上了"的状态，其实什么都没发生。
        #
        # 所以计数和那句日志都挪到收到 CONNECTED 之后：一个数字只代表一件真的
        # 发生过的事（和「已加入世界」用中继的数字是同一个口径）。
        paired = False

        relay_reader = None
        relay_writer = None
        #: 等配对期间从本机 Minecraft 读到的字节（握手包）。
        #: 配对成功后**原样先发给房主**，一个字节都不能丢（见下面的说明）。
        early_mc_bytes = b""

        try:
            relay_reader, relay_writer = await self.connect_relay()

            self.log(f"[PLAYER {connection_id}] waiting for the relay to pair ...")

            # ========================================================================
            #  ★ 用户 2026-09-29（第三轮）第 4 条：点几次刷新就"房间已满"
            # ========================================================================
            #
            # 旧代码在这里只等中继的第一行（最多 30 秒），**完全不看本机那条
            # Minecraft socket**：
            #
            #   * 玩家在 MC 多人列表里点一次「刷新」，MC 就向本机代理开一条 TCP
            #     （服务器列表探测），客户端于是向中继发一条 PLAYER 握手；
            #   * MC 的探测几秒就放弃、把本机 socket 关了，客户端却不知道 ——
            #     中继那条 PLAYER 连接继续挂在 `waiting_players` 里 5~27 秒
            #     （实测），**每一个都占一个房间名额**；
            #   * 中继的容量闸门本来是 `len(sessions) + len(waiting_players) >= 容量`，
            #     于是两人在一个 8 人房里也能刷出 `ERROR ROOM_FULL`，
            #     真正的游戏连接反而进不来。
            #
            # 修法：等中继第一行的同时**盯住本机 Minecraft**。
            #   * MC 关了 / 发了 EOF -> 立刻收工（`finally` 关掉中继那条 socket，
            #     中继的 `watch_player_socket` 0.2 秒内把名额还回去）；
            #   * MC 发来字节（握手包）-> **先存起来**，配对成功后原样补发给房主
            #     （顺序不变、一个字节不丢）。
            response, early_mc_bytes, mc_gone = await self._await_relay_or_mc(
                relay_reader, mc_reader, connection_id
            )

            if mc_gone:
                raise ConnectionError(
                    "本机 Minecraft 已经关掉了这条连接（探测/刷新），"
                    "不再占用房间名额"
                )

            if not response:
                raise ConnectionError("中转服务器关闭了连接")

            text = response.decode(errors="replace").strip()

            if text.startswith(proto.LINE_ERROR):
                # ============================================================
                #  §8.9 等待池里的 `ERROR CONN_MODE_CHANGED … was_live=`
                # ============================================================
                #
                # 换模式时还在等待配对的 PLAYER socket 收的就是这一条。核心的
                # 成员控制连接通常也会收到（不带 ERROR 前缀的同一条），但那条
                # 通道万一正好在抖，这里就是**唯一**的 was_live 来源 —— 把它
                # 交给和成员控制连接同一个消费者（`conn_mode_changed`），
                # 热切换"原本空闲 -> 不重连 / 原本有连接 -> 重建"的判定才不会
                # 因为"没收到"而退回本地 running 的旧判据。
                #
                # 老中继从不发这一行 -> 这里什么都不发生，行为一字不变。
                changed = proto.parse_conn_mode_changed(text)

                if changed is not None:
                    changed_reason, was_live = changed
                    self.log(
                        f"[PLAYER {connection_id}] the relay says the room mode "
                        f"changed while waiting ({changed_reason or '-'}"
                        + (f", was_live={was_live}" if was_live else "")
                        + ")"
                    )
                    self.emit(
                        "conn_mode_changed",
                        reason=changed_reason,
                        was_live=was_live,
                    )

                # 版本门拒绝：**致命，不许重试**。玩家侧的每一次 Minecraft
                # 连接都会重新握手，如果这里只是"报个错就重试"，用户看到的
                # 就是"一直在连接"，而版本不一致永远不会自己变好。
                if proto.is_version_mismatch(text):
                    error_text = proto.describe_error(text)
                    self.version_rejected = error_text
                    self.running = False
                    self.log(f"[PLAYER] version rejected: {error_text}")
                    self.emit("version_rejected", reason=error_text)
                    raise ConnectionError(error_text)

                # Surface a login problem to the UI so it can ask for a fresh
                # login instead of the user just seeing "connection failed".
                if any(
                    code in text
                    for code in (
                        proto.ERR_AUTH_REQUIRED,
                        proto.ERR_AUTH_INVALID_TOKEN,
                        proto.ERR_AUTH_DISABLED,
                    )
                ):
                    parts = text.split()
                    self.auth_error = (
                        parts[1].upper() if len(parts) > 1 else "AUTH_REQUIRED"
                    )
                    self.emit("auth_failed", reason=proto.describe_error(text))

                raise ConnectionError(proto.describe_error(text))

            auto_info = None
            if self.auto_manager is not None:
                from .auto_stream import parse_metadata
                auto_session, auto_info = parse_metadata(text, "CONNECTED_AUTO")
            elif text != proto.LINE_CONNECTED:
                raise ConnectionError(f"中转服务器应答异常: {text}")

            # ---- 从这里开始才算"真的连上了" ----
            async with self.lock:
                self.active_connections += 1

            paired = True

            self.log(f"[PLAYER {connection_id}] Minecraft connected peer={peer}")

            self.log(
                f"[PLAYER {connection_id}] connected to the host; "
                f"starting Minecraft data forwarding"
            )
            self.emit("relay_connected", connection=connection_id)

            # ---- V3: try to upgrade this session to a direct path ----
            # Fire and forget: the relay below is already carrying the game,
            # and a failed punch must cost nothing but a log line.
            if self.direct_hook is not None:
                self._spawn_direct(connection_id)

            # ================================================================
            #  等配对期间 MC 已经发来的字节：塞回读取流的最前面
            # ================================================================
            #
            # **不能**直接写进中继：STUN 模式下房主不转发中继隧道的字节
            # （`direct_carry._drain_relay` 会丢掉并记事故日志），那样握手包就
            # 没了、直连流从中途开始，游戏直接连不上。所以包一层读取流交给
            # `raw_relay()`：谁最后拿到通路，前缀就跟谁走，顺序和字节数都不变。
            # 一个字节都不解析：协议对客户端是不透明的。
            if early_mc_bytes:
                mc_reader = self._PrependedReader(early_mc_bytes, mc_reader)

                self.log(
                    f"[PLAYER {connection_id}] kept {len(early_mc_bytes)}B of "
                    f"early Minecraft bytes and re-queued them in front of the "
                    f"stream (they go out on whichever transport wins)"
                )

            # Everything after CONNECTED is raw Minecraft TCP data.
            if auto_info is not None:
                from .auto_client import run_stream
                await run_stream(self, auto_info, auto_session, False,
                                 mc_reader, mc_writer, relay_reader, relay_writer)
            else:
                await self.raw_relay(
                    connection_id, mc_reader, mc_writer, relay_reader, relay_writer,
                )

        except asyncio.CancelledError:
            raise

        except Exception as e:
            self.last_error = str(e)
            self.log(f"[PLAYER {connection_id}] error: {e}")
            self.emit("relay_error", connection=connection_id, reason=str(e))

        finally:
            await close_writer(relay_writer)
            await close_writer(mc_writer)

            # 只有真的加上去过才减，否则计数会变成负数。
            if paired:
                async with self.lock:
                    self.active_connections -= 1

            self.log(f"[PLAYER {connection_id}] disconnected")
            self.emit("minecraft_disconnected", connection=connection_id)

    # --------------------------------------------------------
    # Raw TCP relay  (FROZEN -- do not restructure)
    # --------------------------------------------------------

    def _spawn_direct(self, connection_id):
        """Run the direct-path upgrade in the background. See HostCore."""
        if self._direct_task is not None and not self._direct_task.done():
            return

        async def run():
            try:
                ok = await self.direct_hook(connection_id)

                self.emit(
                    "direct_path",
                    session=connection_id,
                    via="udp" if ok else "relay",
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"[direct] attempt failed (does not affect the connection): {e}")
                self.emit("direct_path", session=connection_id, via="relay")

        self._direct_task = asyncio.create_task(run())

    async def _try_direct_carry(self, connection_id, mc_reader, mc_writer):
        """
        试着让这个连接走已经打通的直连。True 表示已经接管并搬完了。

        拿不到就返回 False，调用方按 STUN 模式的规则处理（拒绝会话并说明原因，
        不回落中继）。Relay 模式下这个函数根本不会被调用。

        ============================================================================
          这一版换了什么（`HANDOFF-TO-NEXT-SESSION.md`）
        ============================================================================

        以前这里是"把会话挂到自研的 `reliable.ReliableStream` 上"—— 那个流
        **从来没在真实 NAT 上成功过**。现在换成参考工程验证过的
        `udptunnel.UdpTunnel`：本函数只负责"分一条 sid，然后把这条 Minecraft
        连接泵上去"，传输逻辑一行都不在这里。
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
            # See the host side: in STUN mode the relay does not carry the
            # game, so there is nothing to fall back TO. Say so plainly rather
            # than relaying and showing a "direct" screen.
            self.log(
                f"[PLAYER {connection_id}] STUN mode: no punched direct link, "
                f"and the relay does not carry game data in STUN mode -- "
                f"closing this connection (switch to Relay mode to play "
                f"through the server)"
            )
            self.emit(
                "direct_failed",
                connection=connection_id,
                reason="stun_mode_no_direct_link",
            )
            return False

        # The PLAYER is the guest side: each Minecraft connection gets its own
        # stream (`sid`) on the one shared tunnel -- a server-list probe and the
        # real join are two connections and must coexist.
        #
        # 审计问题 6：这条承载搬的字节按 **direct** 记账（不是 config.mode）。
        # 字节由隧道自己的泵累计（`PeerLink._note_traffic` -> 进程级账本），
        # 所以这里不需要再挂钩子 —— 挂两个就会重复计数。
        carrier = direct_carry.DirectCarrier(
            link, connection_id, is_host=False, log=self.log
        )

        if not await carrier.open():
            return False

        self.log(
            f"[PLAYER {connection_id}] Minecraft data over the DIRECT link"
        )

        self.emit("direct_carrying", connection=connection_id, carrying=True)

        try:
            await carrier.pump(mc_reader, mc_writer)
        finally:
            self.emit("direct_carrying", connection=connection_id, carrying=False)

            # Kept for the session layer's hook. Links are shared now (one
            # tunnel, one `sid` per connection) so there is nothing to hand
            # back; see RoomDirectLink.release_all_paths.
            if self.direct_path_releaser is not None:
                with contextlib.suppress(Exception):
                    self.direct_path_releaser()

        return True

    async def raw_relay(
        self,
        connection_id,
        mc_reader,
        mc_writer,
        relay_reader,
        relay_writer,
    ):
        # Same seam as the host side: if the room-level punch already produced a
        # usable UDP link, the game rides THAT; otherwise every line below runs
        # exactly as it did in v2.
        if self.direct_path_provider is not None:
            carried = await self._try_direct_carry(
                connection_id, mc_reader, mc_writer
            )

            if carried:
                return

            # STUN mode has nothing to fall back to: the relay refuses to pair
            # game data for a STUN room. See the host side for the reasoning.
            return

        stats = {"to_relay": 0, "to_minecraft": 0}

        self.log(f"[PLAYER {connection_id}] raw data forwarding started")

        async def minecraft_to_relay():
            try:
                while True:
                    data = await mc_reader.read(BUFFER_SIZE)

                    if not data:
                        return

                    relay_writer.write(data)
                    await relay_writer.drain()

                    stats["to_relay"] += len(data)
                    self.bytes_to_relay += len(data)

            except asyncio.CancelledError:
                raise

            except Exception:
                return

        async def relay_to_minecraft():
            try:
                while True:
                    data = await relay_reader.read(BUFFER_SIZE)

                    if not data:
                        return

                    mc_writer.write(data)
                    await mc_writer.drain()

                    stats["to_minecraft"] += len(data)
                    self.bytes_to_minecraft += len(data)

            except asyncio.CancelledError:
                raise

            except Exception:
                return

        task_mc_to_relay = asyncio.create_task(minecraft_to_relay())
        task_relay_to_mc = asyncio.create_task(relay_to_minecraft())

        try:
            done, pending = await asyncio.wait(
                [task_mc_to_relay, task_relay_to_mc],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            await asyncio.gather(*pending, return_exceptions=True)

        finally:
            task_mc_to_relay.cancel()
            task_relay_to_mc.cancel()

            await asyncio.gather(
                task_mc_to_relay,
                task_relay_to_mc,
                return_exceptions=True,
            )

            self.log(
                f"[PLAYER {connection_id}] raw data forwarding ended "
                f"up={stats['to_relay']}B down={stats['to_minecraft']}B"
            )

    # --------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------

    async def start(self):
        if self.running:
            return

        self.running = True
        self.last_error = ""
        self._tasks = set()

        sample_task = asyncio.create_task(self.sample_loop())
        self._tasks.add(sample_task)
        sample_task.add_done_callback(self._tasks.discard)

        # ====================================================================
        #  换模式重建时，端口刚被上一个会话释放，Windows 上会短暂绑不上
        # ====================================================================
        #
        # 用户实测：切「直连 → 中转」时，重建要重新绑本机代理端口 11451，
        # 而那个端口刚被上一个会话关掉 —— Windows 回 **WinError 10013**
        # （WSAEACCES，"以一种访问权限不允许的方式做了一个访问套接字的尝试"），
        # 于是"按新模式重建失败"，主机卡在半死状态：隧道建立了、控制面也在，
        # 但本机代理没起来，玩家那头握手就被中继拒掉。
        #
        # 端口不是"没权限"，是"还没彻底放开"。所以这里给几秒重试，而不是
        # 一次失败就判定整个重建失败。
        # 上一次的监听器必须**先确认关掉**，否则下面这次绑定会撞在自己身上。
        # 这是"端口占用"的最后一道保险：即便上层漏调了 stop()、或者 stop()
        # 超时被吞掉，新连接也会先把旧的释放掉，而不是直接失败。
        await self._release_listener()

        last_bind_error = None

        for attempt in range(6):
            try:
                self.server = await asyncio.start_server(
                    self._handle_minecraft_task,
                    self.local_host,
                    self.local_port,
                    # Windows 上必须显式给 reuse_address：POSIX 上它默认就是
                    # 开的，Windows 上默认**关**。关着的时候，上一个会话刚
                    # close() 掉的监听套接字还处在 TIME_WAIT，紧接着重绑同一个
                    # 端口就会 10048/10013 —— 这正是"换模式/换主机后端口还说
                    # 被占用"里我们能改的那一半。
                    #
                    # 我们只绑 127.0.0.1，端口是给本机 Minecraft 用的，所以
                    # Windows 上 SO_REUSEADDR 那点"别的进程能抢端口"的风险
                    # 局限在本机已受信任的进程之间，换来的是重绑不会再失败。
                    reuse_address=True,
                )
                break
            except OSError as e:
                last_bind_error = e

                # 只有"端口暂时不可用"这一类才值得重试
                busy = (getattr(e, "winerror", None) in (10013, 10048)
                        or e.errno in (13, 1, 48, 98))

                if not busy or attempt == 5:
                    break

                if attempt == 0:
                    self.log(
                        f"[LOCAL] port {self.local_host}:{self.local_port} "
                        f"not free yet ({type(e).__name__}); retrying"
                    )

                await asyncio.sleep(0.4 * (attempt + 1))

        if self.server is None:
            e = last_bind_error or OSError("bind failed")
            self.running = False

            if getattr(e, "winerror", None) == 10048 or e.errno in (48, 98):
                message = (
                    f"本机端口 {self.local_host}:{self.local_port} "
                    f"已被占用，请换一个本地端口"
                )
            elif getattr(e, "winerror", None) == 10013 or e.errno in (13, 1):
                message = (
                    f"本机端口 {self.local_host}:{self.local_port} "
                    f"暂时不可用（刚被上一个会话占用过），重试多次仍未成功；"
                    f"请稍等几秒再点开始连接"
                )
            else:
                message = f"无法在本机监听 {self.local_host}:{self.local_port}：{e}"

            self.last_error = message
            self.log(f"[LOCAL] start failed: {message}")

            # 光有一句"端口被占用"没法定位。这里在失败现场直接问操作系统
            # 要答案，把结果写进日志/状态卡 —— 下次复现时用户复制日志就有
            # 全部证据（谁占着、PID 是多少、是不是被系统保留了整段端口）。
            for line in self.diagnose_local_port():
                self.log(f"[LOCAL] {line}")

            self.emit("listen_failed", reason=message)
            raise e

        addresses = []
        if self.server.sockets:
            for sock in self.server.sockets:
                with contextlib.suppress(Exception):
                    addresses.append(str(sock.getsockname()))

        self.log("================================================")
        self.log("              MC-LINK  Player")
        self.log("================================================")
        self.log(f"relay server : {self.server_host}:{self.server_port}")
        self.log(f"room         : {self.room}")
        self.log(f"listening on : {', '.join(addresses) or self.local_host}")
        self.log("================================================")
        self.log("[LOCAL] connect in Minecraft to:")
        self.log(f"[LOCAL] {self.local_host}:{self.local_port}")
        self.log("================================================")

        self.emit("listening", endpoint=f"{self.local_host}:{self.local_port}")

    def diagnose_local_port(self):
        """
        本地代理端口绑不上时，问操作系统"到底是谁占着"。

        为什么值得单独写：用户实测的报错是 WinError 10013，Windows 上这个
        码既可能是"别的进程占着"，也可能是**系统把整段端口保留给 Hyper-V /
        WSL / Docker 了**（`netsh interface ipv4 show excludedportrange`）。
        这两种情况的解法完全不同，而只看到一句"端口被占用"是分不出来的。

        只读诊断：跑 netstat / ss 查一下，绝不杀进程、绝不改系统设置。
        任何一步失败都只是少一行日志，不影响主流程。
        """
        import platform
        import subprocess

        port = int(self.local_port)
        lines = []

        def _run(argv):
            try:
                out = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=6,
                    # Windows 下别弹黑框
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return (out.stdout or "") + (out.stderr or "")
            except Exception as e:
                return f"(诊断命令跑不起来：{e})"

        if platform.system() == "Windows":
            text = _run(["netstat", "-ano", "-p", "TCP"])
            hits = [
                line.strip()
                for line in text.splitlines()
                if f":{port} " in line or line.strip().endswith(f":{port}")
            ]

            if hits:
                lines.append(
                    f"本机 {port} 端口当前状态（最后一列是占用它的 PID）："
                )
                lines.extend(f"  {h}" for h in hits[:12])
                lines.append(
                    f"  → 用「任务管理器 → 详细信息」或 "
                    f"`tasklist | findstr <PID>` 看是哪个进程；"
                    f"如果是上一个 CraftLink/Minecraft 残留，结束它即可。"
                )
            else:
                lines.append(
                    f"netstat 里没有进程占着 {port} —— 那更像是被系统保留了。"
                )

            # 10013 的另一半真相：Hyper-V / WSL / Docker 会保留整段动态端口
            excluded = _run(
                ["netsh", "interface", "ipv4", "show", "excludedportrange",
                 "protocol=tcp"]
            )
            keep = [
                line for line in excluded.splitlines()
                if "---" not in line and line.strip()
            ]

            if keep:
                lines.append(f"系统保留的 TCP 端口段（netsh）：")
                lines.extend(f"  {line.strip()}" for line in keep[:12])
                lines.append(
                    "  → 只要上面某一段把 "
                    f"{port} 包住了，绑不上就不是「被占用」，而是这个端口"
                    "在 Windows 上根本不允许监听：换一个本地端口，或者用"
                    "管理员 PowerShell 跑 `net stop winnat` / `net start winnat`"
                    "（会短暂断开 WSL/Docker 的网络）。"
                )
        else:
            text = _run(["ss", "-ltnp"]) or ""
            hits = [line.strip() for line in text.splitlines() if f":{port}" in line]

            if hits:
                lines.append(f"本机 {port} 端口当前监听者：")
                lines.extend(f"  {h}" for h in hits[:12])
            else:
                lines.append(f"{port} 上没有任何监听者。")

        return lines

    async def _handle_minecraft_task(self, reader, writer):
        """
        asyncio.start_server callback. The callback itself must return so more
        connections can be accepted, so the real work goes into a tracked task.
        """
        task = asyncio.create_task(self.handle_minecraft(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _release_listener(self):
        """
        把本机代理监听器关掉并**等它真的关掉**（幂等）。

        `stop()` 会做同样的事，但这条路径要能在"stop 没跑成 / 跑超时"之后
        仍然把端口交出来 —— 用户报的"断开之后再也连不上、端口被占用"就是
        旧监听器还开着。
        """
        server = self.server
        self.server = None

        if server is None:
            return

        with contextlib.suppress(Exception):
            server.close()

        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=5)

        self.log("[LOCAL] previous listener released")

    async def abort(self):
        """
        快速强拆：关监听器、取消所有任务、断开直连链路 —— **不等**。

        `stop()` 是礼貌收尾（可能等 6 秒以上）；超时路径上必须有个"无论如何
        都把 socket 关掉"的兜底，否则端口会一直被旧会话占着。
        幂等。
        """
        self.running = False

        await self._release_listener()

        tasks = list(getattr(self, "_tasks", ()))
        self._tasks = set()

        for task in tasks:
            if not task.done():
                task.cancel()

        # 转发任务被 cancel 之后，它们自己的 finally 会关掉各自的 writer；
        # 这里不等它们（这正是 abort 与 stop 的区别）。端口由监听器的关闭
        # 保证释放，那一条是**同步生效**的。
        self.emit("stopped")

    async def stop(self, graceful=False):
        # `graceful` 对玩家侧没有含义（玩家没有"我主动停服"这回事），
        # 保留这个形参只是为了和 HostCore 的签名一致 —— 会话层同一个
        # `stop(graceful=...)` 要能同时喂给两种 core，不必分头判断类型。
        self.log("stopping player mode ...")

        self.running = False

        # Stop accepting new Minecraft connections.
        if self.server is not None:
            with contextlib.suppress(Exception):
                self.server.close()

            with contextlib.suppress(Exception):
                await self.server.wait_closed()

            self.server = None

        # Close every in-flight session.
        tasks = list(self._tasks)
        self._tasks = set()

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

        self.emit("stopped")
        self.log("player mode stopped.")
