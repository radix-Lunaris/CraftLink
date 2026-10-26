import queue
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from mclink.core.auto_udp import AutoUdpTunnel


class StrictUdpTests(unittest.TestCase):
    def setUp(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tunnel = AutoUdpTunnel(self.sock, ("127.0.0.1", 9), log=lambda *a: None)
        self.addCleanup(self.sock.close)
        self.addCleanup(self.tunnel.close)
        self.tunnel._sendto = lambda *a: None

    def test_full_receive_queue_does_not_ack_or_advance_missing_bytes(self):
        t = self.tunnel
        t.inbox = queue.Queue(maxsize=1)
        t.inbox.put(b"already-retained")
        t._on_data(1, b"first", ("127.0.0.1", 9))
        self.assertEqual(t._expected, 1)
        t._on_data(2, b"second", ("127.0.0.1", 9))
        self.assertEqual(t.inbox.get_nowait(), b"already-retained")
        t._on_data(1, b"first", ("127.0.0.1", 9))
        self.assertEqual(t.inbox.get_nowait(), b"first")
        self.assertEqual(t._expected, 2)
        t._on_data(2, b"second", ("127.0.0.1", 9))
        self.assertEqual(t.inbox.get_nowait(), b"second")
        self.assertEqual(t._expected, 3)
        self.assertFalse(t._reorder)

    def test_stalled_send_raises_instead_of_silently_skipping_chunk(self):
        t = self.tunnel
        t._inflight = {i: [b"old", 0, 300] for i in range(1, 65)}
        with patch("mclink.core.auto_udp.CONGEST_WAIT_S", 0):
            with self.assertRaises(TimeoutError):
                t.send(b"must-not-disappear")
        self.assertEqual(t._next_seq, 1)
        t.close()
        with self.assertRaises(ConnectionError):
            t.send(b"closed-path")


if __name__ == "__main__":
    unittest.main()
