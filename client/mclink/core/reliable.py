"""
MC-Link client -- reliable, ordered byte stream over a swappable path.

============================================================================
 WHY THIS EXISTS AT ALL
============================================================================

Minecraft speaks TCP. A punched hole is UDP. Something has to bridge that, and
it cannot be the kernel: the only way to get kernel TCP onto a UDP hole is a
TUN device plus routing, which needs a signed kernel driver and administrator
rights on Windows.

The reference implementation avoids writing this layer because it moves raw IP
packets and lets the KERNEL's TCP do the work. We move byte streams, so this
layer is ours to write. It is the single most delicate part of MC-Link.

============================================================================
 SCOPE: ONE STREAM AT A TIME, ON PURPOSE
============================================================================

This is NOT a general purpose transport. It carries exactly one ordered byte
stream per session, which is what a Minecraft connection is. That removes
almost everything that makes TCP hard:

    * no connection multiplexing, no stream priorities
    * no TIME_WAIT, no half-closed states, no simultaneous close
    * no Nagle, no delayed ACK tuning, no SACK reneging
    * no congestion control in v1 (see "HONEST LIMITS" below)

============================================================================
 HONEST LIMITS (read before trusting this with a game)
============================================================================

1. NO CONGESTION CONTROL. There is a fixed receive window and an optional
   send-rate cap, but no slow start and no loss-based backoff. On a lossy
   path this can be unfair to other traffic on the same link. Minecraft moves
   roughly 10-50 KB/s with bursts while loading chunks, so the practical risk
   is low, but it is a real limitation and it is why the send rate is capped
   by default rather than left unbounded.

2. RELIABILITY IS SELECTIVE REPEAT. Cumulative ACK plus a 32-bit SACK bitmap,
   RTO with exponential backoff, fast retransmit on duplicate ACKs. This is
   the same family of algorithm KCP uses and it is well understood. It is not
   a formally verified implementation.

3. SEQUENCE SPACE IS 32 BIT. Correct wraparound comparison is used
   throughout, so a stream longer than 4 GB works, but the window is small
   (default 256 KB) and there is no protection against a pathological
   reordering delay longer than half the sequence space.

This layer is the reason the design keeps the relay open in parallel: if it
misbehaves, the session can fall back to the relay within one heartbeat
instead of dropping the player.

============================================================================
 WIRE FORMAT
============================================================================

Fixed 22 byte header, then payload. Big endian throughout.

    offset size field
    0      4    session   session id (from the handshake; rejects stray packets)
    4      4    seq       sequence number of this segment
    8      4    ack       next sequence number we expect from the peer
    12     4    window    free space in our receive buffer, in bytes
    16     1    flags     see FLAG_*
    17     1    reserved
    18     4    sack      bitmap: bit i set => ack + 1 + i was received

ACKs are piggybacked on data, and a header-only segment (empty payload) is a
pure ACK or a keepalive. Datagrams larger than the path MTU are never sent:
the sender splits, the receiver reassembles by sequence number.
"""

import asyncio
import contextlib
import enum
import struct
import time

from .transport import MAX_DATAGRAM, Path

HEADER_SIZE = 22
HEADER = struct.Struct("!IIIIBBI")

#: How much payload fits in one datagram.
MAX_SEGMENT = MAX_DATAGRAM - HEADER_SIZE

#: Receive buffer per session. Also what we advertise as our window.
DEFAULT_WINDOW = 256 * 1024

#: A zero window would deadlock the peer, so never advertise less than this
#: even when the buffer is momentarily full -- the peer will retry and by then
#: the application will have drained something.
MIN_ADVERTISED_WINDOW = 4096

#: Sender cap, in bytes per second. Deliberately modest: without congestion
#: control, an unthrottled sender is how a P2P tunnel gets its owner a
#: complaint from their ISP. See "HONEST LIMITS".
DEFAULT_SEND_RATE = 2 * 1024 * 1024

#: RTO bounds. The initial value is pessimistic on purpose: guessing too low
#: causes spurious retransmits on a slow mobile link, and this link may be 4G.
MIN_RTO = 0.2
MAX_RTO = 6.0
INITIAL_RTO = 0.6

#: TCP-style RTT smoothing.
RTO_ALPHA = 0.125
RTO_BETA = 0.25

#: Retransmit after this many duplicate ACKs, without waiting for the timer.
DUP_ACK_THRESHOLD = 3

#: Send a keepalive when nothing has been sent for this long. Must be well
#: under a typical NAT UDP mapping timeout (30-120 s).
KEEPALIVE_INTERVAL = 10.0

#: Declare the session dead after this long with no ACK progress at all.
#: Kept short so the caller can fall back to the relay quickly -- that is the
#: whole point of keeping the relay open.
DEAD_TIMEOUT = 12.0

#: Handshake retry.
SYN_INTERVAL = 0.2
SYN_TIMEOUT = 6.0

#: Sequence numbers for the handshake itself. Both peers must agree, and they
#: do so implicitly: the numbering is part of the protocol, not negotiated.
#:   SYN_SEQ            the initiator's SYN
#:   SYN_ACK_SEQ        the responder's SYN-ACK (and the ACK of the SYN)
#:   DATA_SEQ_START     the first data sequence number
SYN_SEQ = 0
SYN_ACK_SEQ = 1
DATA_SEQ_START = 2


class Flag(enum.IntFlag):
    NONE = 0x00
    SYN = 0x01
    ACK = 0x02
    FIN = 0x04
    RST = 0x08
    #: Pure acknowledgement with no payload and nothing new to report.
    KEEPALIVE = 0x10


def seq_lt(a, b):
    """
    Is sequence number `a` before `b`, with 32 bit wraparound?

    Plain `<` is wrong across the wrap: after 0xFFFFFFFF comes 0, and a naive
    comparison would treat every later segment as ancient. This is the
    standard signed-difference trick.
    """
    return ((a - b) & 0xFFFFFFFF) > 0x7FFFFFFF


def seq_le(a, b):
    return a == b or seq_lt(a, b)


def seq_diff(a, b):
    """How far `a` is ahead of `b` (signed, wrap-safe)."""
    diff = (a - b) & 0xFFFFFFFF

    return diff - 0x100000000 if diff > 0x7FFFFFFF else diff


class Segment:
    """One datagram, parsed or about to be built."""

    __slots__ = ("session", "seq", "ack", "window", "flags", "sack", "payload")

    def __init__(self, session=0, seq=0, ack=0, window=0, flags=Flag.NONE,
                 sack=0, payload=b""):
        self.session = session
        self.seq = seq
        self.ack = ack
        self.window = window
        self.flags = Flag(flags)
        self.sack = sack
        self.payload = payload

    def encode(self):
        return HEADER.pack(
            self.session & 0xFFFFFFFF,
            self.seq & 0xFFFFFFFF,
            self.ack & 0xFFFFFFFF,
            self.window & 0xFFFFFFFF,
            int(self.flags) & 0xFF,
            0,
            self.sack & 0xFFFFFFFF,
        ) + self.payload

    @staticmethod
    def decode(data):
        """Parse a datagram. Returns None for anything malformed."""
        if data is None or len(data) < HEADER_SIZE:
            return None

        try:
            session, seq, ack, window, flags, _reserved, sack = HEADER.unpack_from(
                data, 0
            )
        except struct.error:
            return None

        return Segment(
            session=session,
            seq=seq,
            ack=ack,
            window=window,
            flags=flags,
            sack=sack,
            payload=data[HEADER_SIZE:],
        )


def build_sack(received, base):
    """
    Bitmap of which sequence numbers after `base` have arrived.

    Only 32 slots are available. That is enough because the advertised window
    caps how far ahead a sender may run, and it keeps the header fixed size --
    a variable length option list would need re-parsing on every packet for a
    benefit this workload does not have.
    """
    bitmap = 0

    for index in range(32):
        if (base + 1 + index) & 0xFFFFFFFF in received:
            bitmap |= 1 << index

    return bitmap


# ============================================================
# Session
# ============================================================

class ReliableStream:
    """
    One reliable byte stream over one path, migratable to another path.

    Lifecycle:

        stream = await ReliableStream.connect(path, session_id)
        ...  stream.send(data) / await stream.recv()  ...
        await stream.migrate(new_path)      # the punched UDP path appeared
        await stream.close()

    The migration method is the reason this class exists in the shape it does.
    It swaps the carrier while keeping the session id, sequence numbers,
    retransmit state and receive buffer, so no byte is lost and no round trip
    is wasted.
    """

    def __init__(self, path, session_id=0, is_initiator=True,
                 window=DEFAULT_WINDOW, send_rate=DEFAULT_SEND_RATE):
        self.path = path
        self.session_id = session_id & 0xFFFFFFFF
        self.is_initiator = bool(is_initiator)

        self.window = int(window)
        self.send_rate = int(send_rate)

        # ---- send side ----
        self.send_next = 0            # next sequence number to use
        self.send_unacked = {}        # seq -> (Segment, first_sent_at, sends)
        self.send_buffer = bytearray()  # unsent application bytes
        self.peer_window = self.window
        self.peer_ack = 0
        self.dup_acks = 0
        self.last_ack_seen = 0
        #: True once the peer has acknowledged DATA, not merely the handshake.
        #: Until then a repeated ACK value is not a duplicate-ACK signal.
        self.peer_acked_data = False

        # ---- receive side ----
        self.recv_next = 0            # next sequence number we expect
        self.recv_out_of_order = {}   # seq -> Segment
        self.recv_buffer = bytearray()
        self.recv_ready = asyncio.Event()

        # ---- timers / state ----
        self.rto = INITIAL_RTO
        self.srtt = None
        self.rttvar = None
        self.last_send_at = 0.0
        self.last_ack_at = time.monotonic()
        self.established = False
        self.closed = False
        self.peer_closed = False
        self.reset = False
        self.last_error = ""

        #: Bumped on every path change so a stale receive loop can exit.
        self.path_generation = 0
        self._tasks = []
        self._send_lock = asyncio.Lock()
        self._bytes_sent = 0

    # --------------------------------------------------------
    # Handshake
    # --------------------------------------------------------

    @classmethod
    async def connect(cls, path, session_id, timeout=SYN_TIMEOUT, **kwargs):
        """
        Open a session as the initiator.

        The handshake is deliberately tiny -- one SYN, one SYN-ACK -- because
        both sides already agreed on a session id over the control channel.
        Its only jobs are to prove the path works in both directions and to
        agree on the initial sequence numbers.
        """
        stream = cls(path, session_id=session_id, is_initiator=True, **kwargs)

        # ---- one agreed sequence space, starting at DATA_SEQ_START ----
        #
        # The handshake itself occupies the first two sequence numbers: 0 is
        # the initiator's SYN, 1 is the responder's SYN-ACK. DATA starts at 2,
        # and BOTH SIDES use the same numbering for the same direction.
        #
        # Getting this wrong is silent and total: if each side picked its own
        # random initial number, every data segment would arrive with a
        # sequence number the peer considers far in the future, land in the
        # out-of-order buffer, and the stream would deliver exactly zero bytes
        # while looking perfectly healthy.
        stream.recv_next = DATA_SEQ_START
        stream.send_next = DATA_SEQ_START

        deadline = time.monotonic() + timeout
        syn_seq = SYN_SEQ

        while time.monotonic() < deadline:
            syn = Segment(
                session=stream.session_id,
                seq=syn_seq,
                ack=0,
                window=stream._advertised_window(),
                flags=Flag.SYN,
            )

            with contextlib.suppress(Exception):
                await path.send(syn.encode())

            reply = await path.recv(timeout=SYN_INTERVAL)

            if reply is None:
                continue

            segment = Segment.decode(reply)

            if segment is None or segment.session != stream.session_id:
                continue

            if not (segment.flags & (Flag.SYN | Flag.ACK)):
                continue

            # The responder acknowledges our SYN with ack == SYN_SEQ + 1.
            stream.peer_ack = segment.ack

            if segment.flags & Flag.ACK and segment.ack == SYN_ACK_SEQ:
                stream.established = True
                stream.last_ack_at = time.monotonic()
                stream._start_tasks()

                # Complete the handshake so the peer stops retransmitting.
                ack = Segment(
                    session=stream.session_id,
                    seq=stream.send_next,
                    ack=stream.recv_next,
                    window=stream._advertised_window(),
                    flags=Flag.ACK,
                )
                with contextlib.suppress(Exception):
                    await path.send(ack.encode())

                return stream

        raise TimeoutError("可靠流握手超时（对端没有响应）")

    @classmethod
    def accept(cls, path, session_id, first_segment=None, **kwargs):
        """
        Open a session as the responder.

        The SYN has usually already been read by whoever is demultiplexing
        datagrams, so it is passed in rather than read here. Its only job is to
        let us sanity check the session id; the sequence numbering is fixed by
        the protocol, not by anything in the SYN.
        """
        stream = cls(path, session_id=session_id, is_initiator=False, **kwargs)

        stream.recv_next = DATA_SEQ_START
        stream.send_next = DATA_SEQ_START

        return stream

    async def confirm_accept(self):
        """
        Send the SYN-ACK for an accepted session.

        Sequence accounting, which is easy to get subtly wrong:

            the initiator's SYN  is sequence SYN_SEQ      (0)
            our SYN-ACK          is sequence SYN_ACK_SEQ  (1)
            data starts at       DATA_SEQ_START           (2)

        so we acknowledge SYN_SEQ + 1 == SYN_ACK_SEQ. Acknowledging anything
        else leaves the initiator waiting forever for an ACK of the number it
        will actually use first, and the handshake silently never completes.
        """
        syn_ack = Segment(
            session=self.session_id,
            seq=SYN_ACK_SEQ,
            ack=SYN_ACK_SEQ,
            window=self._advertised_window(),
            flags=Flag.SYN | Flag.ACK,
        )

        await self.path.send(syn_ack.encode())

        self.peer_ack = SYN_SEQ
        self.established = True
        self.last_ack_at = time.monotonic()
        self._start_tasks()

        return self

    # --------------------------------------------------------
    # Path migration -- the important part
    # --------------------------------------------------------

    async def migrate(self, new_path):
        """
        Continue this session over a different path.

        Everything that defines the stream is kept: session id, both sequence
        numbers, the unacknowledged segment table, the receive buffer and the
        RTT estimate. Only the carrier changes.

        WHY THIS IS SAFE: the peer identifies us by session id in the header,
        not by our source address. A packet arriving from a new address with a
        known session id is accepted and simply updates where we send replies.
        That is exactly how a NAT rebinding or a new hole-punched path is
        absorbed without a teardown.

        The old path is NOT closed here. The caller decides: keeping the relay
        open is what makes the next egress change survivable.
        """
        if self.closed:
            raise RuntimeError("会话已关闭")

        old = self.path
        self.path = new_path
        self.path_generation += 1

        # Restart the receive loop on the new path immediately: the old loop
        # is blocked in recv() on a socket nobody will write to again.
        self._restart_receive_loop()

        # Everything outstanding is still outstanding -- resend it on the new
        # path rather than waiting for an RTO that was measured on the old one.
        for segment, sent_at, sends in list(self.send_unacked.values()):
            segment.flags |= Flag.ACK
            with contextlib.suppress(Exception):
                await new_path.send(segment.encode())

            self.send_unacked[segment.seq] = (segment, time.monotonic(), sends)

        # Reset the RTT estimate: it described a different route.
        self.srtt = None
        self.rttvar = None
        self.rto = INITIAL_RTO
        self.last_ack_at = time.monotonic()

        return old

    def _restart_receive_loop(self):
        self._tasks = [t for t in self._tasks if not t.done()]

        receiver = asyncio.create_task(self._receive_loop(self.path_generation))
        self._tasks.append(receiver)

    # --------------------------------------------------------
    # Tasks
    # --------------------------------------------------------

    def _start_tasks(self):
        if self._tasks:
            return

        self._restart_receive_loop()
        self._tasks.append(asyncio.create_task(self._retransmit_loop()))

    async def _receive_loop(self, generation):
        while not self.closed and generation == self.path_generation:
            try:
                data = await self.path.recv(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a dead path is not fatal
                self.last_error = str(e)
                await asyncio.sleep(0.05)
                continue

            if data is None:
                continue

            segment = Segment.decode(data)

            if segment is None:
                continue

            # A packet from a foreign session must never be able to corrupt
            # this stream, so it is dropped before anything else happens.
            if segment.session != self.session_id:
                continue

            try:
                await self._on_segment(segment)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = f"处理数据包失败：{e}"

    async def _retransmit_loop(self):
        while not self.closed:
            await asyncio.sleep(0.02)

            now = time.monotonic()

            # ---- RTO expiry ----
            for seq, (segment, sent_at, sends) in list(
                self.send_unacked.items()
            ):
                if now - sent_at < self.rto:
                    continue

                if sends >= 12:
                    # Twelve unanswered retransmits on a path that also has a
                    # working relay in parallel is not worth continuing.
                    self.last_error = "重传次数超限，路径可能已失效"
                    self.reset = True
                    self.closed = True
                    self.recv_ready.set()
                    return

                with contextlib.suppress(Exception):
                    await self.path.send(segment.encode())

                self.send_unacked[seq] = (segment, now, sends + 1)

                # Exponential backoff, capped. Without this a 4G link with a
                # 3 second stall would produce a retransmit storm.
                self.rto = min(MAX_RTO, self.rto * 1.5)

            # ---- keepalive ----
            if (
                self.established
                and now - self.last_send_at > KEEPALIVE_INTERVAL
            ):
                with contextlib.suppress(Exception):
                    await self._send_control(Flag.KEEPALIVE)

            # ---- dead peer ----
            if self.established and now - self.last_ack_at > DEAD_TIMEOUT:
                self.last_error = "对端长时间无响应"
                self.reset = True
                self.closed = True
                self.recv_ready.set()
                return

            # ---- drain the send buffer ----
            with contextlib.suppress(Exception):
                await self._flush()

    # --------------------------------------------------------
    # Sending
    # --------------------------------------------------------

    def _advertised_window(self):
        free = self.window - len(self.recv_buffer)

        return max(MIN_ADVERTISED_WINDOW, free)

    async def _send_control(self, flags, payload=b""):
        segment = Segment(
            session=self.session_id,
            seq=self.send_next,
            ack=self.recv_next,
            window=self._advertised_window(),
            flags=flags,
            sack=build_sack(self.recv_out_of_order, self.recv_next),
            payload=payload,
        )

        if payload or (flags & Flag.SYN):
            # Consumes a sequence number.
            self.send_next = (self.send_next + 1) & 0xFFFFFFFF

        await self.path.send(segment.encode())
        self.last_send_at = time.monotonic()

        return segment

    async def _flush(self):
        """
        Send as much buffered data as the peer's window and our rate cap allow.
        """
        if not self.established or self.closed:
            return

        async with self._send_lock:
            now = time.monotonic()

            # ---- rate cap ----
            if self.send_rate > 0:
                elapsed = now - getattr(self, "_rate_window_start", 0.0)

                if elapsed >= 1.0:
                    self._rate_window_start = now
                    self._bytes_in_window = 0
                elif getattr(self, "_bytes_in_window", 0) >= self.send_rate:
                    return

            # ---- in-flight limit (the peer's advertised window) ----
            in_flight = sum(
                len(seg.payload) for seg, _t, _s in self.send_unacked.values()
            )

            budget = max(0, min(self.peer_window, self.window) - in_flight)

            while self.send_buffer and budget > 0:
                chunk = bytes(self.send_buffer[:min(MAX_SEGMENT, budget)])
                del self.send_buffer[:len(chunk)]

                segment = Segment(
                    session=self.session_id,
                    seq=self.send_next,
                    ack=self.recv_next,
                    window=self._advertised_window(),
                    flags=Flag.ACK,
                    sack=build_sack(self.recv_out_of_order, self.recv_next),
                    payload=chunk,
                )

                self.send_unacked[self.send_next] = (segment, now, 0)
                self.send_next = (self.send_next + 1) & 0xFFFFFFFF
                self._bytes_in_window = getattr(self, "_bytes_in_window", 0) + len(chunk)

                try:
                    await self.path.send(segment.encode())
                    self.last_send_at = now
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
                    # Put the bytes back so a path failure does not silently
                    # eat application data.
                    self.send_buffer[:0] = chunk
                    self.send_unacked.pop(segment.seq, None)
                    self.send_next = segment.seq
                    return

                budget -= len(chunk)

    def send(self, data):
        """
        Queue bytes. Returns immediately; delivery is asynchronous.

        The caller (a TCP proxy reading from the local Minecraft connection)
        must apply backpressure itself, which it does by only reading from its
        socket when `writable()` says there is room.
        """
        if self.closed:
            raise ConnectionError("会话已关闭")

        self.send_buffer.extend(data)

        return len(data)

    def writable(self):
        """True when the peer's window has room for more data."""
        if self.closed or not self.established:
            return False

        in_flight = sum(
            len(seg.payload) for seg, _t, _s in self.send_unacked.values()
        )

        return (
            in_flight < min(self.peer_window, self.window)
            and len(self.send_buffer) < self.window
        )

    # --------------------------------------------------------
    # Receiving
    # --------------------------------------------------------

    async def _on_segment(self, segment):
        now = time.monotonic()

        # ---- handshake completion ----
        if segment.flags & Flag.SYN and not self.established:
            if not self.is_initiator:
                self.recv_next = segment.seq
                await self.confirm_accept()

        if segment.flags & Flag.RST:
            self.last_error = "对端重置了连接"
            self.reset = True
            self.closed = True
            self.recv_ready.set()
            return

        # ---- process the acknowledgement ----
        if segment.flags & Flag.ACK:
            self._on_ack(segment)
            self.peer_window = max(MIN_ADVERTISED_WINDOW, segment.window)
            self.last_ack_at = now

        # ---- payload ----
        if segment.payload:
            self._on_data(segment)

        # ---- FIN ----
        if segment.flags & Flag.FIN:
            self.peer_closed = True
            self.recv_ready.set()

        # Anything that acknowledges progress is worth an immediate reply: it
        # keeps the RTT estimate fresh and stops the peer retransmitting.
        if segment.payload or (segment.flags & (Flag.SYN | Flag.FIN)):
            with contextlib.suppress(Exception):
                await self._send_control(Flag.ACK)

    def _on_ack(self, segment):
        ack = segment.ack

        if seq_lt(ack, self.peer_ack):
            return  # stale ACK, older than one we already processed

        if ack == self.peer_ack and self.peer_acked_data:
            # The peer repeated itself. Only count it as a duplicate once it
            # has actually acknowledged data: the handshake leaves peer_ack at
            # the SYN's value, so without this flag the FIRST data ACK looks
            # like a duplicate, which both blocks RTT sampling and fires a
            # spurious fast retransmit.
            self.dup_acks += 1

            if self.dup_acks >= DUP_ACK_THRESHOLD and self.send_unacked:
                # Fast retransmit: the same gap has been reported three times.
                first = min(self.send_unacked.keys(), key=_SeqKey)
                seg, _t, sends = self.send_unacked[first]
                self.send_unacked[first] = (seg, time.monotonic(), sends + 1)

                with contextlib.suppress(Exception):
                    asyncio.create_task(self.path.send(seg.encode()))

                self.dup_acks = 0
        else:
            self.dup_acks = 0
            self.peer_ack = ack

        # ---- RTT sampling ----
        #
        # Only from a segment that was sent exactly once: a retransmitted
        # segment's ACK may be answering the original, and using it would
        # inflate the estimate (the classic Karn's algorithm problem).
        for seq, (seg, sent_at, sends) in list(self.send_unacked.items()):
            if seq_le(seq, (ack - 1) & 0xFFFFFFFF):
                if sends == 0 and seg.payload:
                    self.peer_acked_data = True
                    self._update_rtt(time.monotonic() - sent_at)

                del self.send_unacked[seq]
            elif seg.payload:
                self.peer_acked_data = True

        # ---- selective retransmit from the SACK bitmap ----
        if segment.sack:
            for index in range(32):
                if segment.sack & (1 << index):
                    seq = (ack + index) & 0xFFFFFFFF
                    entry = self.send_unacked.get(seq)

                    if entry is not None:
                        seg, _t, sends = entry
                        self.send_unacked[seq] = (
                            seg, time.monotonic(), sends
                        )

    def _update_rtt(self, sample):
        """
        Fold one RTT sample into the smoothed estimate.

        The only sample rejected is a nonsensical one. In particular a very
        SMALL sample is kept: a LAN or loopback round trip really can be under
        a millisecond, and discarding those would leave the estimate unset on
        exactly the paths where MC-Link is fastest. A negative sample means
        the monotonic clock misbehaved, and an absurd one is stale.
        """
        if sample < 0 or sample > MAX_RTO:
            return

        if self.srtt is None:
            self.srtt = sample
            self.rttvar = sample / 2
        else:
            self.rttvar = (
                (1 - RTO_BETA) * self.rttvar
                + RTO_BETA * abs(self.srtt - sample)
            )
            self.srtt = (1 - RTO_ALPHA) * self.srtt + RTO_ALPHA * sample

        # RTO is floored well above the measured RTT so a jittery mobile link
        # does not produce a retransmit storm.
        self.rto = min(MAX_RTO, max(MIN_RTO, self.srtt + 4 * self.rttvar))

    def _on_data(self, segment):
        seq = segment.seq

        if seq_lt(seq, self.recv_next):
            # Already delivered: a duplicate from a lost ACK. Ignored, but the
            # caller still sends an ACK above so the peer can make progress.
            return

        if seq == self.recv_next:
            self.recv_buffer.extend(segment.payload)
            self.recv_next = (self.recv_next + 1) & 0xFFFFFFFF
            self.recv_ready.set()

            # Drain anything that was waiting for exactly this gap to fill.
            while self.recv_next in self.recv_out_of_order:
                buffered = self.recv_out_of_order.pop(self.recv_next)
                self.recv_buffer.extend(buffered.payload)
                self.recv_next = (self.recv_next + 1) & 0xFFFFFFFF

            return

        # Out of order: hold it, but never let the buffer grow without bound.
        if len(self.recv_out_of_order) < 4096:
            self.recv_out_of_order[seq] = segment

    async def recv(self, max_bytes=65536, timeout=None):
        """
        Return up to `max_bytes` of delivered-in-order data.

        Returns b"" when the peer closed cleanly, and None on timeout.
        """
        while True:
            if self.recv_buffer:
                chunk = bytes(self.recv_buffer[:max_bytes])
                del self.recv_buffer[:len(chunk)]

                # Room freed => tell the peer straight away, otherwise it sits
                # blocked on a stale zero window.
                with contextlib.suppress(Exception):
                    await self._send_control(Flag.ACK)

                return chunk

            if self.peer_closed or self.closed:
                return b""

            self.recv_ready.clear()

            try:
                if timeout is None:
                    await self.recv_ready.wait()
                else:
                    await asyncio.wait_for(self.recv_ready.wait(), timeout)
            except (asyncio.TimeoutError, TimeoutError):
                return None

    # --------------------------------------------------------
    # Closing
    # --------------------------------------------------------

    async def close(self, graceful=True):
        if self.closed:
            return

        if graceful and self.established:
            with contextlib.suppress(Exception):
                await self._send_control(Flag.FIN | Flag.ACK)

        self.closed = True
        self.recv_ready.set()

        for task in self._tasks:
            task.cancel()

        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        self._tasks = []

        await self.path.close()

    # --------------------------------------------------------

    def stats(self):
        return {
            "session": self.session_id,
            "path": self.path.describe(),
            "established": self.established,
            "rtt_ms": round(self.srtt * 1000, 1) if self.srtt else None,
            "rto_ms": round(self.rto * 1000, 1),
            "in_flight": len(self.send_unacked),
            "send_buffer": len(self.send_buffer),
            "recv_buffer": len(self.recv_buffer),
            "peer_window": self.peer_window,
            "closed": self.closed,
            "last_error": self.last_error,
        }


class _SeqKey:
    """Ordering helper for wrap-safe min() over sequence numbers."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return seq_lt(self.value, other.value)

    def __eq__(self, other):
        return self.value == other.value


__all__ = [
    "ReliableStream",
    "Segment",
    "Flag",
    "seq_lt",
    "seq_le",
    "seq_diff",
    "build_sack",
    "HEADER_SIZE",
    "MAX_SEGMENT",
    "DEFAULT_WINDOW",
    "SYN_SEQ",
    "SYN_ACK_SEQ",
    "DATA_SEQ_START",
]
