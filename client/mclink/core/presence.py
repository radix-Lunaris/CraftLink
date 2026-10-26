"""
MC-Link client -- the presence channel.

============================================================================
 WHY THIS EXISTS
============================================================================

「在线账号」 has to mean "this person's client is open and logged in", and it
has to stop meaning that the moment they close it. Two wrong answers were
tried before this one:

  * online = holds an unexpired LOGIN token. A token lives for hours, so
    somebody who force-closed the client (or logged out while the message
    never arrived) stayed 在线 in the panel until it expired. That is the
    complaint: 「我人都关软件了，后台还显示在线」.

  * online = has a room membership or a Minecraft session. Fixes the above,
    but a client that is open, logged in, and simply not in a room yet shows
    as offline -- also wrong, just in the other direction.

So the client opens ONE extra lightweight connection right after login and
keeps it open (PING every 10 s). The relay counts it as presence, and the
connection dies with the process -- which is exactly the moment the person
stops being online.

Additive by design: an older relay answers ERROR to the unknown command and
this module simply gives up for that session. Nothing else is affected.
"""

import asyncio
import contextlib
import time

from . import protocol as proto

#: How often to prove we are still here. Comfortably under the relay's
#: 30 second presence timeout, so a couple of lost packets change nothing.
PING_INTERVAL = 10.0

#: Wait before reconnecting after the relay dropped us.
RETRY_DELAY = 5.0


class PresenceKeeper:
    """
    Holds the presence connection open for as long as the client is logged in.

    One instance per login. Never raises out of run(): presence is a nicety,
    and failing to hold it must not disturb the actual game connection.
    """

    def __init__(self, host, port, session, client_id=""):
        self.host = host
        self.port = int(port)
        self.session = session
        self.client_id = client_id or "-"
        self.on_line = None          # optional callback(text) for the log
        self.running = False
        self.supported = True        # False once a relay says "unknown command"
        #: 最近一次 PING/PONG 的往返时间（毫秒）。玩家端没有别的控制通道，
        #: 这条连接是它唯一能测"本机 ↔ 中继"延迟的地方。
        self.last_rtt_ms = None

    # --------------------------------------------------------

    async def run(self):
        """Keep the channel open until cancelled."""
        self.running = True

        while self.running and self.supported:
            try:
                await self._hold_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if not self.supported:
                    return

                self._log(f"在线状态连接断开（{e}），{int(RETRY_DELAY)} 秒后重连")

            if not self.running or not self.supported:
                return

            await asyncio.sleep(RETRY_DELAY)

    async def _hold_once(self):
        token = getattr(self.session, "token", "") or ""
        username = getattr(self.session, "username", "") or "-"

        if not token:
            self.running = False
            return

        reader, writer = await asyncio.open_connection(self.host, self.port)

        try:
            line = (
                f"{proto.ROLE_PRESENCE} {proto.SENTINEL} "
                f"{self.client_id} {username} {token}"
            )

            writer.write((line + "\n").encode())
            await writer.drain()

            reply = (await asyncio.wait_for(reader.readline(), timeout=10))
            text = reply.decode(errors="replace").strip()

            if text.startswith("ERROR"):
                # An older relay does not know PRESENCE. That is fine: presence
                # is only how the panel shows 在线, and the relay will use its
                # own (connection based) answer instead.
                self.supported = False
                self._log("服务器不支持在线状态上报（旧版中继），跳过")
                return

            if not text.startswith(proto.LINE_ACK_PRESENCE):
                self.supported = False
                self._log(f"在线状态上报被拒绝：{text}")
                return

            self._log("已上报在线状态")

            while self.running:
                await asyncio.sleep(PING_INTERVAL)

                # PING 之后把 PONG 读掉：顺便量一次往返时间。
                #
                # The PLAYER session has no control channel of its own -- after
                # CONNECTED its socket carries raw Minecraft bytes and nothing
                # else -- so this is the one place the player's client can
                # measure "本机 ↔ 中继服务器" latency. The dashboard used to show
                # "—" for players while the log showed a number, which read like
                # a broken feature.
                sent_at = time.monotonic()

                writer.write(b"PING presence\n")
                await writer.drain()

                try:
                    raw = await asyncio.wait_for(
                        reader.readline(), timeout=PING_INTERVAL
                    )
                except asyncio.TimeoutError:
                    continue

                if not raw:
                    raise ConnectionError("在线状态连接被关闭")

                text = raw.decode("utf-8", errors="replace").strip()

                if text.startswith("PONG"):
                    # clamp 到 >= 0：单调时钟理论上不会倒流，但"负数毫秒"
                    # 一旦漏到界面上比没有数字更糟（审计问题 5 的口径加固）。
                    self.last_rtt_ms = max(
                        0.0, (time.monotonic() - sent_at) * 1000.0
                    )

        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    def stop(self):
        self.running = False

    def _log(self, message):
        if self.on_line is not None:
            with contextlib.suppress(Exception):
                self.on_line(message)
