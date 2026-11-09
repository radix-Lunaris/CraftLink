"""A shared AUTO frame carrier over the existing punched UDP socket.

PeerLink is used for socket ownership and the room watchdog interface only.
No PeerChannel, DirectCarrier, spare backend, or per-stream socket is opened.
"""
import asyncio
import contextlib
import itertools
import queue

from .auto_stream import Decoder, Kind
from .direct_carry import PeerLink, CONNECT_TIMEOUT_S
from .udptunnel import UdpHub
from .auto_udp import AutoUdpTunnel


class AutoPeer(PeerLink):
    def __init__(self, peer_id, punch_link, manager, log=None, peer_addr=None):
        super().__init__(peer_id, punch_link, log=log, peer_addr=peer_addr)
        self.manager = manager
        self._outgoing = asyncio.PriorityQueue(maxsize=128)
        self._sequence = itertools.count()

    def _connect_blocking(self, timeout):
        self._drain_socket(self.sock)
        self.sock.setblocking(True)
        self.hub = UdpHub(self.sock, self.log)
        self.tunnel = AutoUdpTunnel(self.sock, self.addr, self.log, hub=self.hub)
        self.hub.start()
        self.tunnel.connect(timeout)

    async def ensure_started(self, timeout=CONNECT_TIMEOUT_S):
        if self.started:
            return not self.stop.is_set()
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self.started:
                return not self.stop.is_set()
            if self.stop.is_set():
                return False
            self.punch_link.carried = True
            self.punch_link.stop_keepalive()
            try:
                await asyncio.to_thread(self._connect_blocking, timeout)
            except Exception as exc:
                self.log("[auto] UDP handshake failed: " + str(exc))
                self._teardown_start()
                self.punch_link.carried = False
                with contextlib.suppress(Exception):
                    self.punch_link.start_keepalive()
                return False
            self._started = True
            self._tasks["auto-writer"] = asyncio.create_task(self._write_frames())
            self._demux_task = asyncio.create_task(self._receive_frames())
            return True

    async def send_frame(self, frame):
        # The single writer and strict UDP frame lock preserve frame boundaries
        # even if cancellation leaves a worker thread finishing a failed send.
        if self.stop.is_set() or self.tunnel is None:
            raise ConnectionError("AUTO peer closed")
        future = asyncio.get_running_loop().create_future()
        await self._outgoing.put((10 if frame.kind == Kind.DATA else 0,
                                  next(self._sequence), frame, future))
        # close() may have run while queue.put applied backpressure.
        if self.stop.is_set():
            future.cancel()
            raise ConnectionError("AUTO peer closed")
        await future

    async def _write_frames(self):
        current = None
        try:
            while not self.stop.is_set():
                _, _, frame, current = await self._outgoing.get()
                if current.done():
                    continue
                await asyncio.wait_for(asyncio.to_thread(self.tunnel.send, frame.encode()), 2.0)
                if not current.done():
                    current.set_result(None)
                current = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log("[auto] UDP writer failed: " + repr(exc))
        finally:
            if current and not current.done():
                current.set_exception(ConnectionError("AUTO UDP writer stopped"))
            self.close()

    @staticmethod
    def _read_batch(tunnel):
        first = tunnel.recv(.2)
        if not first:
            return b""
        parts = [first]
        for _ in range(63):
            try:
                parts.append(tunnel.inbox.get_nowait())
            except queue.Empty:
                break
        return b"".join(parts)

    async def _receive_frames(self):
        decoder = Decoder()
        try:
            while not self.stop.is_set():
                tunnel = self.tunnel
                if tunnel is None:
                    return
                data = await asyncio.to_thread(self._read_batch, tunnel)
                if data:
                    for frame in decoder.feed(data):
                        await self.manager.receive_direct(self, frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log("[auto] UDP frame carrier failed: " + repr(exc))
        finally:
            self.close()

    def close(self):
        # Parent cleanup owns only this carrier here, never game sockets.
        if not self.stop.is_set():
            super().close()
            while not self._outgoing.empty():
                _, _, _, future = self._outgoing.get_nowait()
                if not future.done():
                    future.set_exception(ConnectionError("AUTO UDP carrier closed"))
            for stream in list(self.manager.streams.values()):
                path = stream.paths.get("direct")
                if path is not None and path.peer is self:
                    stream.path_failed("direct", path)
