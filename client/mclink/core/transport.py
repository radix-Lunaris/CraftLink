"""
MC-Link client -- transport paths.

============================================================================
 WHAT A "PATH" IS
============================================================================

A path is a way to move one datagram from us to a peer and back. Nothing more.
It does not know about rooms, Minecraft, or reliability.

Three implementations exist, and they are interchangeable on purpose:

    UdpPath     a real UDP socket (the direct, hole-punched case)
    RelayPath   a logical path multiplexed over the existing relay TCP tunnel
    MemoryPath  a pair of in-process queues (tests only, injects loss)

The reason for the abstraction is the one thing the reference implementation
gets for free and we do not: EasyTier moves raw IP packets, so a peer's
address changing mid-flow is invisible to the TCP running above it. We move
byte streams, so when the punched UDP path appears we must be able to swap the
path underneath a LIVE session without the stream noticing.

The rule that makes that safe, taken from EasyTier's own design
(`socket/udp/layer.rs::UdpConnectControl::HolePunch { recv_addr }`):

    THE SESSION OUTLIVES THE PATH.

Same session id, same sequence numbers, same retransmit state -- only the
thing that carries the bytes changes. A session that tore itself down and
rebuilt on a new socket would produce exactly the stall we are trying to
avoid.
"""

import asyncio
import contextlib
import random
import socket

# A single datagram must stay well under the smallest MTU we can expect. 1200
# is the QUIC safe default and is comfortably below any tunnel overhead.
MAX_DATAGRAM = 1200


class Path:
    """Base class: something that can carry a datagram to a peer."""

    #: "udp" | "relay" | "memory" -- shown in the GUI and in logs.
    kind = "?"

    #: A path that can never be re-established (relay) behaves differently
    #: from one that may come and go (a punched UDP path), so callers can ask.
    migratable = False

    async def send(self, data: bytes):
        raise NotImplementedError

    async def recv(self, timeout=None):
        """Return one datagram, or None on timeout."""
        raise NotImplementedError

    async def close(self):
        pass

    #: Optional human readable description of where this path goes.
    def describe(self):
        return self.kind


# ============================================================
# UDP
# ============================================================

class UdpPath(Path):
    """
    A real UDP socket pointed at one peer address.

    Bound explicitly (not ephemeral) after a successful punch, because the NAT
    mapping belongs to the LOCAL PORT: closing and rebinding the socket would
    throw away the hole that was just opened.
    """

    kind = "udp"
    migratable = True

    def __init__(self, sock, peer_addr, local_addr=None, owns=True):
        self.sock = sock
        self.peer_addr = peer_addr
        self.local_addr = local_addr or sock.getsockname()
        self._loop = asyncio.get_event_loop()

        #: Whether closing this path should close the SOCKET.
        #:
        #: ====================================================================
        #:   WHY THIS FLAG EXISTS (a bug that cost a whole punch)
        #: ====================================================================
        #:
        #: A punched socket is not owned by the stream that rides it. It is owned
        #: by the ROOM: the hole is opened once, kept warm by the keepalive, and
        #: shared by every Minecraft connection that follows (the game opens
        #: several -- a server-list ping, then the real join).
        #:
        #: `ReliableStream.close()` closes its path, and the default path closes
        #: its socket. So the first connection to finish CLOSED THE HOLE. The
        #: next connection found a link that still looked ready (the keepalive
        #: thread was still there) but whose socket was dead, and the handshake
        #: went nowhere while the log said "link established".
        #:
        #: With `owns=False` the stream still stops using the path; it just does
        #: not destroy something the room still needs. The room closes it when it
        #: leaves.
        self.owns_socket = bool(owns)

    @classmethod
    async def open(cls, peer_addr, local_port=0, bind_host="0.0.0.0"):
        """Create a socket and point it at `peer_addr`."""
        loop = asyncio.get_running_loop()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)

        # A generous receive buffer: a burst of Minecraft chunks arriving
        # faster than the event loop drains them would otherwise be dropped by
        # the kernel, and a drop costs a retransmit round trip.
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)

        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)

        sock.bind((bind_host, int(local_port)))

        return cls(sock, tuple(peer_addr), sock.getsockname())

    async def send(self, data: bytes):
        await self._loop.sock_sendto(self.sock, data, self.peer_addr)

    async def recv(self, timeout=None):
        loop = asyncio.get_running_loop()

        try:
            if timeout is None:
                data, _addr = await loop.sock_recvfrom(self.sock, 65535)
            else:
                data, _addr = await asyncio.wait_for(
                    loop.sock_recvfrom(self.sock, 65535), timeout=timeout
                )
        except (asyncio.TimeoutError, TimeoutError):
            return None
        except OSError:
            return None

        return data

    async def close(self):
        if not self.owns_socket:
            # The room owns this socket (see `owns_socket`); it stays open for
            # the next connection and is closed when the room goes away.
            return

        with contextlib.suppress(OSError):
            self.sock.close()

    def describe(self):
        return f"udp {self.local_addr[0]}:{self.local_addr[1]} -> " \
               f"{self.peer_addr[0]}:{self.peer_addr[1]}"


# ============================================================
# Relay
# ============================================================

class RelayPath(Path):
    """
    A logical path over the relay's TCP connection.

    This is the fallback that must ALWAYS work, and it is deliberately kept
    open even after a direct path succeeds: it is what makes an egress change
    (or a NAT rebinding) a stutter instead of a dropped game.

    Frames are length prefixed because TCP is a byte stream, not a datagram
    channel -- without the length prefix two frames that happened to be
    written close together would be read as one.
    """

    kind = "relay"
    migratable = False

    def __init__(self, reader, writer, session_id=0):
        self.reader = reader
        self.writer = writer
        self.session_id = session_id
        self.closed = False

    async def send(self, data: bytes):
        if self.closed:
            raise ConnectionError("relay path is closed")

        # 2 byte big endian length + payload. Deliberately tiny: the relay is
        # a dumb pipe and this framing only exists to preserve message
        # boundaries.
        self.writer.write(len(data).to_bytes(2, "big") + data)
        await self.writer.drain()

    async def recv(self, timeout=None):
        if self.closed:
            return None

        async def _read():
            header = await self.reader.readexactly(2)
            length = int.from_bytes(header, "big")
            return await self.reader.readexactly(length)

        try:
            if timeout is None:
                return await _read()

            return await asyncio.wait_for(_read(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self.closed = True
            return None

    async def close(self):
        self.closed = True

        if self.writer is not None:
            with contextlib.suppress(Exception):
                self.writer.close()
                await self.writer.wait_closed()

    def describe(self):
        return "relay"


# ============================================================
# In-memory (tests)
# ============================================================

class MemoryPath(Path):
    """
    A path backed by queues, with an optional independent return channel.

    The return channel matters for loss testing: a path that can only drop
    packets in ONE direction cannot reproduce the case that matters most --
    the data arrives but the acknowledgement is lost, so the sender
    retransmits and the receiver must recognise a duplicate. That is the
    single most common bug in a hand written ARQ layer, so the test harness
    has to be able to produce it.
    """

    kind = "memory"
    migratable = True

    def __init__(self, name="mem", loss=0.0, reorder_every=0,
                 reverse_loss=-1.0):
        self.name = name
        self.inbox = asyncio.Queue()
        self.outbox = None

        #: Separate probability for the reverse direction. -1 means "same as
        #: `loss`"; 0.0 means "never drop an ACK". Must be an ATTRIBUTE, not
        #: just a constructor argument: peer() reads it to configure the far
        #: end, and reading a missing attribute here raised AttributeError.
        self.reverse_loss = float(reverse_loss)

        self.closed = False
        self.loss = float(loss)
        self.reorder_every = int(reorder_every)

        self.sent = 0
        self.dropped = 0
        self._held = []
        self._rng = random.Random(90210)
        self.peer_ref = None

    # --------------------------------------------------------

    def peer(self, name=None):
        """
        Create the other end and wire both directions.

        The two ends share a random source so a test that seeds it is fully
        reproducible regardless of which side drops a packet.
        """
        other = MemoryPath(
            name or (self.name + "-peer"),
            loss=self.reverse_loss if self.reverse_loss >= 0 else self.loss,
            reorder_every=self.reorder_every,
        )
        # Cross-wire the queues: what this end sends lands in the other end's
        # inbox, and vice versa. Without these two lines send() has no outbox
        # and the pair is silently dead.
        self.outbox = other.inbox
        other.outbox = self.inbox

        other.peer_ref = self
        other._rng = self._rng

        self.peer_ref = other

        return other

    async def send(self, data: bytes):
        if self.closed or self.outbox is None:
            raise ConnectionError("memory path is closed")

        self.sent += 1

        if self.loss and self._rng.random() < self.loss:
            self.dropped += 1
            return

        if self.reorder_every and self.sent % self.reorder_every == 0:
            # Park this datagram so the next one overtakes it.
            self._held.append(data)
            return

        if self._held:
            for held in reversed(self._held):
                await self.outbox.put(held)
            self._held.clear()

        await self.outbox.put(data)

    async def recv(self, timeout=None):
        if self.closed:
            return None

        try:
            if timeout is None:
                return await self.inbox.get()

            return await asyncio.wait_for(self.inbox.get(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None

    async def close(self):
        self.closed = True

        # Flush anything still parked so a test never hangs on it.
        if self.outbox is not None:
            for held in self._held:
                with contextlib.suppress(Exception):
                    self.outbox.put_nowait(held)

        self._held.clear()

    def describe(self):
        return f"memory:{self.name}"


def memory_pair(name="a", loss=0.0, reverse_loss=-1.0, reorder_every=0):
    """
    Two MemoryPaths wired to each other, ready to hand to two sessions.

    Returns (side_a, side_b).
    """
    side_a = MemoryPath(name, loss=loss, reorder_every=reorder_every)
    side_b = side_a.peer(name + "-peer")

    if reverse_loss >= 0:
        side_b.loss = float(reverse_loss)
        side_a.reverse_loss = float(reverse_loss)

    return side_a, side_b


__all__ = [
    "Path",
    "UdpPath",
    "RelayPath",
    "MemoryPath",
    "memory_pair",
    "MAX_DATAGRAM",
]
