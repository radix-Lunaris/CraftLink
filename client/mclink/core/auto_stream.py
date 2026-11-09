"""V5 end-to-end streams. Physical paths never own the Minecraft socket.

ACK acknowledges bytes retained by this process (not UDP/TCP transport ACKs).
The receiver advertises an absolute right edge backed by reserved memory.
All state is owned by one asyncio loop; UDP adapters marshal onto that loop.
"""
import asyncio
import contextlib
import enum
import struct
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass

CAPABILITY = "auto_stream_v1"
MAGIC = b"MCA5"
HEADER = struct.Struct("!4sBBH16sQQI")
MAX_PAYLOAD = 64 * 1024
CHUNK = 16 * 1024
FIN_ACK = 1


class Kind(enum.IntEnum):
    DATA = 1
    ACK = 2
    PING = 3
    PONG = 4
    PREPARE = 5
    READY = 6
    COMMIT = 7
    FALLBACK = 8
    FIN = 9
    RESET = 10


@dataclass(frozen=True)
class Config:
    send_limit: int = 4 * 1024 * 1024
    receive_limit: int = 256 * 1024  # initial reserved window; <= 4 MiB ceiling
    memory_limit: int = 128 * 1024 * 1024
    stable_seconds: float = 1.0
    probe_count: int = 3
    direct_ping: float = 1.0
    relay_ping: float = 2.0
    relay_dead: float = 6.0
    blackhole_min: float = 2.0
    blackhole_max: float = 5.0
    unavailable_timeout: float = 15.0
    retry_delays: tuple = (5.0, 15.0, 30.0, 60.0)
    relay_retry: tuple = (1.0, 2.0, 4.0, 8.0, 10.0)
    flap_window: float = 60.0
    flap_count: int = 3
    flap_cooldown: float = 120.0


@dataclass(frozen=True)
class Frame:
    kind: Kind
    session: str
    offset: int = 0
    value: int = 0
    payload: bytes = b""
    flags: int = 0

    def encode(self):
        if len(self.payload) > MAX_PAYLOAD:
            raise ValueError("AUTO frame too large")
        return HEADER.pack(MAGIC, 1, int(self.kind), self.flags,
                           uuid.UUID(hex=self.session).bytes,
                           self.offset, self.value, len(self.payload)) + self.payload

    @staticmethod
    async def read(reader):
        raw = await reader.readexactly(HEADER.size)
        kind, session, offset, value, length, flags = decode_header(raw)
        payload = await reader.readexactly(length)
        return Frame(kind, session, offset, value, payload, flags)


def decode_header(raw):
    magic, version, kind, flags, sid, offset, value, length = HEADER.unpack(raw)
    if magic != MAGIC or version != 1 or length > MAX_PAYLOAD or flags & ~FIN_ACK:
        raise ValueError("invalid AUTO frame header")
    return Kind(kind), uuid.UUID(bytes=sid).hex, offset, value, length, flags


class Decoder:
    """Incremental parser; partial frames persist across physical reads."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= HEADER.size:
            kind, sid, off, val, length, flags = decode_header(self.buffer[:HEADER.size])
            size = HEADER.size + length
            if len(self.buffer) < size:
                break
            result.append(Frame(kind, sid, off, val,
                                bytes(self.buffer[HEADER.size:size]), flags))
            del self.buffer[:size]
        if len(self.buffer) > HEADER.size + MAX_PAYLOAD:
            raise ValueError("AUTO parser overflow")
        return result


class MemoryBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0
        self.changed = asyncio.Event()

    async def reserve(self, size):
        if size > self.limit:
            raise ValueError("AUTO reservation exceeds process budget")
        while self.used + size > self.limit:
            self.changed.clear()
            await self.changed.wait()
        self.used += size

    def release(self, size):
        self.used -= size
        if self.used < 0:
            raise AssertionError("AUTO memory accounting underflow")
        self.changed.set()


class TcpPath:
    name = "relay"

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.alive = True
        self.lock = asyncio.Lock()

    async def send(self, frame):
        async with self.lock:
            if not self.alive or self.writer.is_closing():
                raise ConnectionError("relay path closed")
            self.writer.write(frame.encode())
            await asyncio.wait_for(self.writer.drain(), 6.0)

    def close(self):
        self.alive = False
        self.writer.close()


class DirectPath:
    name = "direct"

    def __init__(self, peer):
        self.peer = peer
        self.detached = False

    @property
    def alive(self):
        return not self.detached and self.peer.started and not self.peer.stop.is_set()

    async def send(self, frame):
        await self.peer.send_frame(frame)

    def close(self):
        # A stream owns a subscription, not the shared UDP carrier. AutoPeer's
        # writer/decoder own physical failures (including partial frame sends).
        self.detached = True


class AutoStream:
    def __init__(self, manager, session, peer_id, is_host, reader, writer,
                 relay, reconnect=None, metadata=None):
        self.manager, self.config = manager, manager.config
        self.session, self.peer_id, self.is_host = session, peer_id, is_host
        self.reader, self.writer = reader, writer
        self.network_only = reader is None and writer is None
        self.reconnect = reconnect
        self.metadata = metadata or {}
        self.paths = {"relay": relay}
        self.health = {}
        self.active = "relay"
        self.phase = "relay"
        self.reason = "relay_first"
        self.epoch = 0
        self.cancelled_epoch = -1
        self.prepared = -1
        self.retry_at = 0.0
        self.failures = 0
        self.flaps = deque()
        self.tx = OrderedDict()
        self.tx_next = self.tx_ack = self.tx_cursor = 0
        self.tx_bytes = 0
        self.remote_window = 0
        self.peer_confirmed = False
        self.last_progress = time.monotonic()
        self.rx = {}
        self.rx_accepted = self.rx_delivered = 0
        self.rx_fin = None
        self.local_eof = self.fin_acked = self.remote_eof = False
        self.done = asyncio.Event()
        self.tx_wake, self.rx_wake = asyncio.Event(), asyncio.Event()
        self.read_wake = asyncio.Event()
        self.tasks = set()
        self.last_status = None
        self._reserved = False
        self._outage_at = None
        self._reconnect_task = None
        self._last_prepare = self._last_fin = 0.0
        self._nonce = 0
        self.bytes_up = self.bytes_down = 0

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _state(self, path):
        return self.health.setdefault(path, {"last_pong": time.monotonic(),
            "first": None, "good": 0, "pending": {}, "last_ping": 0.0,
            "rtt_ms": None})

    def qualified(self):
        path = self.paths.get("direct")
        state = self.health.get("direct", {})
        return bool(path and path.alive and state.get("first") is not None
                    and state.get("good", 0) >= self.config.probe_count
                    and time.monotonic() - state["first"] >= self.config.stable_seconds)

    def path_ready(self, name):
        path = self.paths.get(name)
        state = self.health.get(name)
        dead = self.config.relay_dead if name == "relay" else self.failure_timeout()
        return bool(path and path.alive and state and state["good"] >= 1
                    and time.monotonic() - state["last_pong"] < dead)

    def failure_timeout(self):
        rtt = self.health.get("direct", {}).get("rtt_ms") or 0
        return min(self.config.blackhole_max,
                   max(self.config.blackhole_min, rtt / 1000 * 4))

    def snapshot(self):
        direct = self.paths.get("direct")
        addr = getattr(getattr(direct, "peer", None), "addr", None)
        selected = self.paths.get(self.active)
        observed = self.active if selected and selected.alive else "reconnecting"
        return dict(session_id=self.session, peer=self.peer_id,
            active_path=observed, relay_ready=self.path_ready("relay"),
            direct_ready=self.qualified(), switch_phase=self.phase,
            last_reason=self.reason, updated_at=time.time(),
            direct_endpoint=(format_endpoint(addr) if addr else ""),
            relay_rtt_ms=self.health.get("relay", {}).get("rtt_ms"),
            direct_rtt_ms=self.health.get("direct", {}).get("rtt_ms"),
            epoch=self.epoch, **self.metadata)

    def publish(self):
        state = self.snapshot()
        key = tuple((k, v) for k, v in state.items() if k != "updated_at")
        if key != self.last_status:
            self.last_status = key
            self.manager.publish(state)

    async def send_on(self, name, frame):
        path = self.paths.get(name)
        if path is None or not path.alive:
            return False
        try:
            await path.send(frame)
            if frame.kind == Kind.DATA:
                self.manager.traffic(name, len(frame.payload), 0)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self.path_failed(name, path)
            return False

    async def both(self, frame):
        await asyncio.gather(*(self.send_on(name, frame) for name in list(self.paths)))

    async def ack(self, name=None):
        frame = Frame(Kind.ACK, self.session, self.rx_accepted,
                      self.rx_delivered + self.config.receive_limit,
                      flags=FIN_ACK if self.remote_eof else 0)
        if name:
            await self.send_on(name, frame)
        else:
            await self.both(frame)

    def path_failed(self, name, path=None):
        current = self.paths.get(name)
        if current is None or (path is not None and current is not path):
            return
        self.paths.pop(name, None)
        current.close()
        self.health.pop(name, None)
        if name == "direct" or self.active == name:
            self.fallback(name + "_unavailable")
        self.tx_wake.set()
        self.publish()

    def fallback(self, reason, epoch=None):
        was_direct = self.active == "direct" or self.phase in ("preparing", "ready")
        self.epoch = max(self.epoch + 1, epoch or 0)
        self.cancelled_epoch = self.epoch
        self.prepared = -1
        self.active, self.phase, self.reason = "relay", "retry_wait", reason
        self.tx_cursor = self.tx_ack
        self.last_progress = time.monotonic()
        self.tx_wake.set()
        if was_direct:
            now = time.monotonic()
            self.flaps.append(now)
            while self.flaps and self.flaps[0] < now - self.config.flap_window:
                self.flaps.popleft()
            index = min(self.failures, len(self.config.retry_delays) - 1)
            # stable deterministic jitter per stream, bounded to +/- 10%
            jitter = .9 + (int(self.session[:4], 16) % 201) / 1000
            self.retry_at = now + self.config.retry_delays[index] * jitter
            self.failures += 1
            if len(self.flaps) >= self.config.flap_count:
                self.retry_at = max(self.retry_at, now + self.config.flap_cooldown)
        self.publish()
        if epoch is None:
            self.spawn(self.both(Frame(Kind.FALLBACK, self.session, value=self.epoch)))

    def commit(self, epoch):
        self.epoch = epoch
        self.active, self.phase, self.reason = "direct", "direct", "direct_verified"
        self.tx_cursor = self.tx_ack
        self.last_progress = time.monotonic()
        self.tx_wake.set()
        self.publish()

    async def receive(self, frame, name, source=None):
        if self.done.is_set() or frame.session != self.session:
            return
        if source is not None and self.paths.get(name) is not source:
            return  # obsolete relay reader cannot affect its replacement
        if frame.kind == Kind.DATA:
            if self.network_only:
                raise ValueError("application bytes on network control session")
            end = frame.offset + len(frame.payload)
            if not frame.payload or end > self.rx_delivered + self.config.receive_limit:
                raise ValueError("AUTO receive window exceeded")
            if self.rx_fin is not None and end > self.rx_fin:
                raise ValueError("AUTO data past FIN")
            if end <= self.rx_accepted:
                await self.ack(name)
                return
            if frame.offset < self.rx_accepted:
                raise ValueError("AUTO overlapping data")
            old = self.rx.get(frame.offset)
            if old is not None and old != frame.payload:
                raise ValueError("AUTO conflicting replay")
            if old is None:
                # Reject overlaps before reserving dictionary entries. Normal
                # replay preserves the original frame boundaries exactly.
                if any(off < end and off + len(data) > frame.offset
                       for off, data in self.rx.items()):
                    raise ValueError("AUTO overlapping ranges")
                self.rx[frame.offset] = frame.payload
                self.bytes_down += len(frame.payload)
                self.manager.traffic(name, 0, len(frame.payload))
            while self.rx_accepted in self.rx:
                self.rx_accepted += len(self.rx[self.rx_accepted])
            self.rx_wake.set()
            await self.ack(name)
        elif frame.kind == Kind.ACK:
            if frame.offset > self.tx_next or frame.value < frame.offset:
                raise ValueError("AUTO invalid acknowledgement")
            if frame.offset > self.tx_ack and frame.offset != self.tx_next and frame.offset not in self.tx:
                raise ValueError("AUTO acknowledgement splits a retained frame")
            if name == "relay":
                self.peer_confirmed = True
            if frame.offset >= self.tx_ack:
                if frame.offset > self.tx_ack:
                    self.last_progress = time.monotonic()
                self.tx_ack = frame.offset
                self.remote_window = max(self.remote_window, frame.value)
                for offset in list(self.tx):
                    if offset + len(self.tx[offset]) <= self.tx_ack:
                        data = self.tx.pop(offset)
                        self.tx_bytes -= len(data)
                        self.manager.budget.release(len(data))
                self.tx_wake.set()
                self.read_wake.set()
            if frame.flags & FIN_ACK and self.local_eof and frame.offset == self.tx_next:
                self.fin_acked = True
            self.check_finished()
        elif frame.kind == Kind.PING:
            await self.send_on(name, Frame(Kind.PONG, self.session,
                value=frame.value, payload=frame.payload))
        elif frame.kind == Kind.PONG:
            health = self._state(name)
            pending = health["pending"].pop(frame.value, None)
            if pending is not None and pending[1] == frame.payload:
                now = time.monotonic()
                health["last_pong"] = now
                health["rtt_ms"] = max(0, (now - pending[0]) * 1000)
                health["first"] = health["first"] or now
                health["good"] += 1
        elif frame.kind == Kind.PREPARE and not self.is_host:
            if frame.value > self.cancelled_epoch and self.qualified() and self.path_ready("relay"):
                self.prepared = self.epoch = frame.value
                self.phase = "ready"
                await self.send_on("relay", Frame(Kind.READY, self.session, value=frame.value))
        elif frame.kind == Kind.READY and self.is_host:
            if (self.phase == "preparing" and frame.value == self.epoch
                    and frame.value > self.cancelled_epoch and self.qualified()
                    and self.path_ready("relay")):
                self.commit(frame.value)
                await self.both(Frame(Kind.COMMIT, self.session, value=frame.value))
        elif frame.kind == Kind.COMMIT and not self.is_host:
            if frame.value == self.prepared and frame.value > self.cancelled_epoch and self.qualified():
                self.commit(frame.value)
        elif frame.kind == Kind.FALLBACK:
            if frame.value > self.cancelled_epoch:
                self.fallback("peer_fallback", frame.value)
        elif frame.kind == Kind.FIN:
            if frame.offset < self.rx_accepted or (self.rx_fin is not None and frame.offset != self.rx_fin):
                raise ValueError("AUTO invalid FIN")
            self.rx_fin = frame.offset
            self.rx_wake.set()
            await self.ack(name)
        elif frame.kind == Kind.RESET:
            self.reason = "peer_reset"
            self.done.set()

    def check_finished(self):
        if self.local_eof and self.fin_acked and self.remote_eof:
            self.reason = "finished"
            self.done.set()

    async def _read_local(self):
        reserved = 0
        try:
            while not self.done.is_set():
                size = min(CHUNK, self.config.send_limit - self.tx_bytes,
                           max(0, self.remote_window - self.tx_next))
                if not size:
                    self.read_wake.clear()
                    await self.read_wake.wait()
                    continue
                await self.manager.budget.reserve(size)
                reserved = size
                data = await self.reader.read(size)
                self.manager.budget.release(reserved - len(data))
                reserved = len(data)
                if not data:
                    self.local_eof = True
                    self.tx_wake.set()
                    return
                if not self.tx:
                    self.last_progress = time.monotonic()
                self.tx[self.tx_next] = data
                self.tx_next += len(data)
                self.tx_bytes += len(data)
                reserved = 0
                self.bytes_up += len(data)
                self.tx_wake.set()
        finally:
            if reserved:
                self.manager.budget.release(reserved)

    async def _send_data(self):
        while not self.done.is_set():
            self.tx_wake.clear()
            path = self.paths.get(self.active)
            if path and path.alive:
                for offset, data in list(self.tx.items()):
                    if offset < self.tx_cursor:
                        continue
                    if offset + len(data) > self.remote_window:
                        break
                    chosen = self.active
                    epoch = self.epoch
                    if not await self.send_on(chosen, Frame(Kind.DATA, self.session, offset, payload=data)):
                        break
                    if epoch != self.epoch or chosen != self.active or self.paths.get(chosen) is not path:
                        break  # fallback changed cursor while physical send awaited
                    self.tx_cursor = offset + len(data)
            if self.local_eof and time.monotonic() - self._last_fin >= .5:
                self._last_fin = time.monotonic()
                await self.both(Frame(Kind.FIN, self.session, self.tx_next))
            try:
                await asyncio.wait_for(self.tx_wake.wait(), .5)
            except asyncio.TimeoutError:
                pass

    async def _deliver(self):
        while not self.done.is_set():
            self.rx_wake.clear()
            while self.rx_delivered in self.rx:
                offset = self.rx_delivered
                data = self.rx[offset]
                self.writer.write(data)
                await self.writer.drain()
                self.rx.pop(offset)
                self.rx_delivered += len(data)
                await self.ack()
            if self.rx_fin is not None and self.rx_delivered == self.rx_fin and not self.remote_eof:
                if self.writer.can_write_eof():
                    self.writer.write_eof()
                    await self.writer.drain()
                self.remote_eof = True
                await self.ack()
                self.check_finished()
            await self.rx_wake.wait()

    async def _relay_reader(self, path):
        try:
            while not self.done.is_set() and self.paths.get("relay") is path:
                frame = await Frame.read(path.reader)
                if frame.session != self.session:
                    raise ValueError("AUTO session mismatch")
                await self.receive(frame, "relay", path)
        except asyncio.CancelledError:
            raise
        except Exception:
            if (self.paths.get("relay") is path and not self.network_only
                    and not self.peer_confirmed):
                # The peer cancelled before the opening ACK. No application
                # bytes have been read: _read_local waits for its window.
                # Do not keep an unpaired status probe alive as a resume job.
                self.reason = "peer_not_attached"
                self.done.set()
            else:
                self.path_failed("relay", path)

    async def attach_relay(self, path):
        old = self.paths.get("relay")
        if old and old is not path:
            old.close()
        self.paths["relay"] = path
        self.health.pop("relay", None)
        self._state("relay")
        self.spawn(self._relay_reader(path))
        if self.active == "relay":
            self.tx_cursor = self.tx_ack
        self.tx_wake.set()
        await self.ack("relay")

    async def _restore_relay(self):
        attempt = 0
        while not self.done.is_set() and "relay" not in self.paths:
            try:
                path = await self.reconnect()
                if self.done.is_set():
                    path.close()
                    return
                await self.attach_relay(path)
                if self.paths.get("relay") is path:
                    return
                raise ConnectionError("AUTO relay attachment failed")
            except asyncio.CancelledError:
                raise
            except PermissionError:
                self.reason = "relay_resume_denied"
                self.done.set()
                return
            except Exception as exc:
                self.reason = "relay_restore: " + type(exc).__name__
            delay = self.config.relay_retry[min(attempt, len(self.config.relay_retry) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _monitor(self):
        while not self.done.is_set():
            now = time.monotonic()
            if "relay" not in self.paths and self.reconnect and (
                    self._reconnect_task is None or self._reconnect_task.done()):
                self._reconnect_task = self.spawn(self._restore_relay())
            candidate = self.manager.candidate(self.peer_id)
            direct = self.paths.get("direct")
            if self.peer_confirmed and candidate and candidate.started and not candidate.stop.is_set() and (
                    direct is None or direct.peer is not candidate):
                if direct:
                    self.path_failed("direct", direct)
                self.paths["direct"] = DirectPath(candidate)
                self.health.pop("direct", None)
            for name, path in list(self.paths.items()):
                state = self._state(name)
                if not path.alive:
                    self.path_failed(name, path)
                    continue
                dead = self.config.relay_dead if name == "relay" else self.failure_timeout()
                if now - state["last_pong"] > dead:
                    # Only the paired, persistent network session can condemn
                    # a healthy-looking shared carrier. An application flow's
                    # missing PONG/ACK only removes that flow's direct route.
                    if name == "direct" and self.network_only and state["good"]:
                        path.peer.close()
                    self.path_failed(name, path)
                    continue
                interval = self.config.relay_ping if name == "relay" else self.config.direct_ping
                if now - state["last_ping"] >= interval:
                    self._nonce += 1
                    # Large echoed payload verifies the actual framed data
                    # carrier, not merely the punch/UDP keepalive packets.
                    payload = bytes([self._nonce % 251]) * (1024 if state["good"] < 3 else 32)
                    state["pending"][self._nonce] = (now, payload)
                    while len(state["pending"]) > 8:
                        state["pending"].pop(next(iter(state["pending"])))
                    state["last_ping"] = now
                    self.spawn(self.send_on(name, Frame(Kind.PING, self.session,
                                                       value=self._nonce, payload=payload)))
            if self.active == "direct" and self.tx and self.remote_window > self.tx_ack:
                if now - self.last_progress > self.failure_timeout():
                    self.path_failed("direct")
            if self.active == "relay" and self.is_host and self.qualified() and self.path_ready("relay") and now >= self.retry_at:
                if now - self._last_prepare >= 1.0:
                    if self.phase != "preparing":
                        self.epoch = max(self.epoch, self.cancelled_epoch) + 1
                    self.phase = "preparing"
                    self._last_prepare = now
                    self.spawn(self.send_on("relay", Frame(Kind.PREPARE, self.session, value=self.epoch)))
            if not any(self.path_ready(name) for name in self.paths):
                self._outage_at = self._outage_at or now
                if now - self._outage_at >= self.config.unavailable_timeout:
                    self.reason = "all_paths_timeout"
                    self.done.set()
            else:
                self._outage_at = None
            self.publish()
            await asyncio.sleep(.1)

    async def run(self):
        await self.manager.budget.reserve(self.config.receive_limit)
        self._reserved = True
        self.manager.streams[self.session] = self
        try:
            await self.attach_relay(self.paths["relay"])
            methods = (self._monitor,) if self.network_only else (
                self._read_local, self._send_data, self._deliver, self._monitor)
            for method in methods:
                task = self.spawn(method())
                task.add_done_callback(self._task_finished)
            await self.done.wait()
            if self.reason != "finished" and self.reason != "peer_reset":
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.both(Frame(Kind.RESET, self.session)), 1)
        except asyncio.CancelledError:
            # User stop/mode rebuild must also terminate the remote logical
            # stream, including when its direct path would otherwise stay up.
            self.reason = "stopped"
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.both(Frame(Kind.RESET, self.session)), 1)
            raise
        finally:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
            relay = self.paths.get("relay")
            if relay:
                relay.close()
            self.manager.streams.pop(self.session, None)
            self.manager.budget.release(self.tx_bytes + self.config.receive_limit)
            self.tx.clear()
            self.rx.clear()
            self.manager.publish(dict(self.snapshot(), active_path="none", switch_phase="ended"))

    def _task_finished(self, task):
        if task.cancelled() or self.done.is_set():
            return
        error = task.exception()
        if error is not None:
            self.manager.log("[auto] stream failed: " + repr(error))
            self.reason = type(error).__name__
            self.done.set()


class AutoManager:
    def __init__(self, candidate=None, on_status=None, on_traffic=None, log=None, config=None):
        self.config = config or Config()
        self.budget = MemoryBudget(self.config.memory_limit)
        self.streams = {}
        self.candidate = candidate or (lambda peer: None)
        self.on_status, self.on_traffic = on_status, on_traffic
        self.log = log or (lambda *_: None)
        self.states = {}

    def publish(self, state):
        self.states[state["session_id"]] = state
        if state.get("switch_phase") == "ended":
            self.states.pop(state["session_id"], None)
        if self.on_status:
            self.on_status(state)

    def traffic(self, path, up, down):
        if self.on_traffic:
            self.on_traffic("direct" if path == "direct" else "relay", up, down)

    async def receive_direct(self, peer, frame):
        if peer.stop.is_set() or not peer.started:
            return
        stream = self.streams.get(frame.session)
        if stream is None or stream.peer_id != peer.peer_id:
            return  # UUID alone never selects a different player's stream
        if not stream.peer_confirmed:
            return  # relay opening ACK precedes direct admission for this UUID
        path = stream.paths.get("direct")
        if path is None:
            if self.candidate(peer.peer_id) is not peer:
                return
            path = stream.paths["direct"] = DirectPath(peer)
        if path.peer is peer:
            try:
                await stream.receive(frame, "direct", path)
            except ValueError as exc:
                # A valid outer frame with invalid per-stream offsets/window
                # terminates that UUID, not the other streams on this carrier.
                self.log("[auto] invalid logical stream frame: " + str(exc))
                stream.reason = "protocol_error"
                stream.done.set()

    def close(self):
        for stream in list(self.streams.values()):
            stream.reason = "stopped"
            stream.done.set()


def format_endpoint(addr):
    host, port = addr[:2]
    return f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"


def parse_metadata(line, expected):
    parts = line.split()
    if len(parts) < 3 or parts[0] != expected:
        raise ValueError("invalid AUTO handshake")
    uuid.UUID(hex=parts[1])
    values = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
    if values.get("proto") != CAPABILITY or not all(values.get(k) for k in ("host", "player", "generation", "resume")):
        raise ValueError("incomplete AUTO handshake")
    return parts[1], values
