"""AUTO-only byte-preserving adapter around the unchanged v3 UDP tunnel.

Manual STUN imports udptunnel.UdpTunnel directly. No flags, replacements or
changed queue/ACK semantics are injected into that released implementation.
"""
import queue
import threading
import time

from .udptunnel import UdpTunnel, MTU, HDR, REORDER_MAX, T_ACK, T_DATA
from .udptunnel import WINDOW, SEND_WAIT_S, CONGEST_WAIT_S, RTO_MS


class AutoUdpTunnel(UdpTunnel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._frame_lock = threading.Lock()

    def send(self, data):
        with self._frame_lock:
            if self._stop.is_set():
                raise ConnectionError("AUTO UDP carrier closed")
            for offset in range(0, len(data), MTU - HDR):
                self._send_chunk(data[offset:offset + MTU - HDR])

    def _on_data(self, seq, payload, addr):
        with self._rx_lock:
            if seq == self._expected:
                try:
                    self.inbox.put_nowait(payload)
                except queue.Full:
                    pass  # no sequence advance, no ACK: sender retains it
                else:
                    self._reorder.pop(seq, None)
                    self._expected += 1
                    while self._expected in self._reorder:
                        try:
                            self.inbox.put_nowait(self._reorder[self._expected])
                        except queue.Full:
                            break
                        del self._reorder[self._expected]
                        self._expected += 1
            elif seq > self._expected and seq - self._expected <= REORDER_MAX:
                if len(self._reorder) < REORDER_MAX:
                    self._reorder.setdefault(seq, payload)
            ack = self._expected - 1
        self._sendto(self._build(T_ACK, 0, ack), addr)

    def _send_chunk(self, chunk):
        deadline = time.monotonic() + SEND_WAIT_S
        congest_deadline = time.monotonic() + CONGEST_WAIT_S
        while not self._stop.is_set():
            limit = max(1, int(min(self._current_window(), WINDOW)))
            with self._tx_lock:
                inflight = len(self._inflight)
            if inflight < limit:
                if self._window.acquire(timeout=0.02):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("AUTO UDP send window stalled")
            else:
                if time.monotonic() >= congest_deadline:
                    raise TimeoutError("AUTO UDP congestion window stalled")
                with self._space_cv:
                    self._space_cv.wait(0.05)
                continue
            self._backoff()
        else:
            raise ConnectionError("AUTO UDP carrier stopped during frame")
        with self._tx_lock:
            seq = self._next_seq
            self._next_seq += 1
            self._inflight[seq] = [chunk, time.time(), RTO_MS]
        self._sendto(self._build(T_DATA, seq, self._expected - 1, chunk), self.send_to)
