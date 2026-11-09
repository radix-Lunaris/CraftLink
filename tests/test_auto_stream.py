import asyncio
import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from mclink.core.auto_stream import (AutoManager, AutoStream, Config, Decoder, DirectPath, MemoryBudget,
    Frame, Kind, TcpPath, HEADER)


async def socket_pair():
    queue = asyncio.Queue()
    server = await asyncio.start_server(lambda r, w: queue.put_nowait((r, w)), "127.0.0.1", 0)
    left = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    right = await queue.get()
    server.close()
    await server.wait_closed()
    return left, right


class Peer:
    def __init__(self, peer_id, manager):
        self.peer_id, self.manager = peer_id, manager
        self.stop = threading.Event()
        self.started = True
        self.addr = ("127.0.0.1", 30000)
        self.other = None
        self.drop_data = False
        self.frames = []

    async def send_frame(self, frame):
        if self.stop.is_set():
            raise ConnectionError("injected path close")
        self.frames.append(frame)
        if self.drop_data and frame.kind == Kind.DATA:
            return
        # Exercise real wire encoding even with the deterministic carrier.
        parsed = Decoder().feed(frame.encode())[0]
        await self.other.manager.receive_direct(self.other, parsed)

    def close(self):
        self.stop.set()


class WireTests(unittest.TestCase):
    def test_fragmented_frames_and_invalid_header(self):
        sid = uuid.uuid4().hex
        frames = [Frame(Kind.DATA, sid, offset=i * 1024, payload=os.urandom(1024)) for i in range(3)]
        raw = b"".join(frame.encode() for frame in frames)
        decoder, got = Decoder(), []
        for byte in raw:
            got.extend(decoder.feed(bytes([byte])))
        self.assertEqual(frames, got)
        raw = bytearray(frames[0].encode())
        raw[:4] = b"oops"
        with self.assertRaises(ValueError):
            Decoder().feed(raw)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.writers = []
        self.tasks = []
        self.managers = []
        self.states = []

    async def asyncTearDown(self):
        for manager in self.managers:
            manager.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for writer in self.writers:
            writer.close()
        await asyncio.gather(*(w.wait_closed() for w in self.writers), return_exceptions=True)
        for manager in self.managers:
            self.assertEqual(manager.budget.used, 0)

    async def pair(self, config=None):
        app, local = await socket_pair()
        backend, hostlocal = await socket_pair()
        relay_a, relay_b = await socket_pair()
        self.writers.extend([app[1], local[1], backend[1], hostlocal[1], relay_a[1], relay_b[1]])
        candidates_a, candidates_b = {}, {}
        a = AutoManager(candidate=candidates_a.get, config=config,
            on_status=lambda s: self.states.append((time.monotonic(), "a", s["active_path"], s["last_reason"])))
        b = AutoManager(candidate=candidates_b.get, config=config,
            on_status=lambda s: self.states.append((time.monotonic(), "b", s["active_path"], s["last_reason"])))
        self.managers.extend([a, b])
        sid = uuid.uuid4().hex
        sa = AutoStream(a, sid, "host", False, *local, TcpPath(*relay_a))
        sb = AutoStream(b, sid, "player", True, *hostlocal, TcpPath(*relay_b))
        self.tasks.extend([asyncio.create_task(sa.run()), asyncio.create_task(sb.run())])
        pa, pb = Peer("host", a), Peer("player", b)
        pa.other, pb.other = pb, pa
        return app, backend, sa, sb, pa, pb, candidates_a, candidates_b

    async def wait_direct(self, a, b):
        async def wait():
            while a.active != "direct" or b.active != "direct":
                await asyncio.sleep(.02)
        await asyncio.wait_for(wait(), 6)

    async def exchange(self, app, backend, data):
        app[1].write(data)
        await app[1].drain()
        self.assertEqual(await asyncio.wait_for(backend[0].readexactly(len(data)), 10), data)
        response = os.urandom(len(data))
        backend[1].write(response)
        await backend[1].drain()
        self.assertEqual(await asyncio.wait_for(app[0].readexactly(len(response)), 10), response)

    async def test_relay_first_upgrade_blackhole_replay_same_sockets(self):
        app, backend, a, b, pa, pb, ca, cb = await self.pair()
        writer_ids = (id(a.writer), id(b.writer))
        await self.exchange(app, backend, os.urandom(128 * 1024))
        self.assertEqual((a.active, b.active), ("relay", "relay"))
        ca["host"], cb["player"] = pa, pb
        await self.wait_direct(a, b)
        await self.exchange(app, backend, os.urandom(512 * 1024))
        pa.drop_data = True  # PING/PONG remain healthy; DATA alone disappears
        start = time.monotonic()
        data = os.urandom(512 * 1024)
        app[1].write(data)
        await app[1].drain()
        first = await asyncio.wait_for(backend[0].readexactly(1), 5)
        recovered = time.monotonic() - start
        self.assertLess(recovered, 3.0,
                        [(round(t-start, 3), side, path, reason) for t, side, path, reason in self.states if t >= start])
        rest = await asyncio.wait_for(backend[0].readexactly(len(data) - 1), 10)
        self.assertEqual(first + rest, data)
        await self.exchange(app, backend, os.urandom(128 * 1024))
        self.assertEqual((a.active, b.active), ("relay", "relay"))
        self.assertEqual(writer_ids, (id(a.writer), id(b.writer)))
        self.assertFalse(a.done.is_set())
        self.assertFalse(b.done.is_set())

    async def test_half_close_preserves_response_and_releases_memory(self):
        app, backend, a, b, *_ = await self.pair()
        request = os.urandom(180000)
        app[1].write(request)
        await app[1].drain()
        app[1].write_eof()
        self.assertEqual(await asyncio.wait_for(backend[0].read(), 5), request)
        response = os.urandom(80000)
        backend[1].write(response)
        await backend[1].drain()
        backend[1].write_eof()
        self.assertEqual(await asyncio.wait_for(app[0].read(), 5), response)
        await asyncio.wait_for(asyncio.gather(*self.tasks), 5)

    async def test_stale_commit_and_wrong_player_are_ignored(self):
        app, backend, a, b, pa, pb, ca, cb = await self.pair()
        await self.exchange(app, backend, b"first")
        ca["host"], cb["player"] = pa, pb
        await self.wait_direct(a, b)
        old = a.epoch
        a.fallback("test")
        await a.receive(Frame(Kind.COMMIT, a.session, value=old), "relay")
        self.assertEqual(a.active, "relay")
        impostor = Peer("another-player", a.manager)
        await a.manager.receive_direct(impostor, Frame(Kind.DATA, a.session, payload=b"wrong"))
        self.assertEqual(a.rx_accepted, 5)

    async def test_zero_window_backpressure_keeps_direct_healthy(self):
        config = Config(receive_limit=4096, memory_limit=24576)
        app, backend, a, b, pa, pb, ca, cb = await self.pair(config)
        ca["host"], cb["player"] = pa, pb
        await self.wait_direct(a, b)
        gate = asyncio.Event()
        actual_writer = b.writer
        class SlowWriter:
            def write(self, data):
                actual_writer.write(data)
            async def drain(self):
                await gate.wait()
                await actual_writer.drain()
        b.writer = SlowWriter()
        data = os.urandom(32000)
        app[1].write(data)
        await app[1].drain()
        await asyncio.sleep(2.5)
        self.assertEqual((a.active, b.active), ("direct", "direct"))
        self.assertLessEqual(b.rx_accepted - b.rx_delivered, config.receive_limit)
        self.assertLessEqual(a.manager.budget.used, config.memory_limit)
        gate.set()
        self.assertEqual(await asyncio.wait_for(backend[0].readexactly(len(data)), 8), data)
        b.writer = actual_writer

    async def test_out_of_order_and_duplicate_frames_deliver_once(self):
        app, backend, a, b, *_ = await self.pair()
        await self.exchange(app, backend, b"start")
        offset = b.rx_accepted
        second = Frame(Kind.DATA, b.session, offset + 4, payload=b"5678")
        first = Frame(Kind.DATA, b.session, offset, payload=b"1234")
        await b.receive(second, "relay")
        await b.receive(second, "relay")
        await b.receive(first, "relay")
        await b.receive(first, "relay")
        self.assertEqual(await asyncio.wait_for(backend[0].readexactly(8), 2), b"12345678")

    async def test_stop_sends_reset_without_closing_shared_peer(self):
        app, backend, a, b, pa, pb, ca, cb = await self.pair()
        ca["host"], cb["player"] = pa, pb
        await self.wait_direct(a, b)
        self.tasks[0].cancel()
        await asyncio.gather(self.tasks[0], return_exceptions=True)
        await asyncio.wait_for(b.done.wait(), 2)
        self.assertFalse(pa.stop.is_set())
        self.assertFalse(pb.stop.is_set())

    async def test_all_paths_timeout_and_global_budget_cancellation(self):
        app, backend, a, b, *_ = await self.pair(Config(unavailable_timeout=.3))
        await self.exchange(app, backend, b"before-outage")
        a.path_failed("relay")
        self.assertEqual(a.snapshot()["active_path"], "reconnecting")
        await asyncio.wait_for(a.done.wait(), 2)
        self.assertEqual(a.reason, "all_paths_timeout")
        budget = MemoryBudget(128)
        await budget.reserve(128)
        blocked = asyncio.create_task(budget.reserve(1))
        await asyncio.sleep(.01)
        self.assertFalse(blocked.done())
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)
        budget.release(128)
        self.assertEqual(budget.used, 0)

    async def test_application_timeout_detaches_without_closing_shared_carrier(self):
        app, backend, a, b, pa, pb, ca, cb = await self.pair()
        ca["host"], cb["player"] = pa, pb
        await self.wait_direct(a, b)
        orphan = AutoStream(a.manager, uuid.uuid4().hex, "host", False,
                            None, None, a.paths["relay"])
        orphan.network_only = False
        orphan.peer_confirmed = True
        orphan.paths["direct"] = DirectPath(pa)
        orphan._state("direct")["last_pong"] -= 3
        orphan._state("relay").update(good=1, last_ping=time.monotonic())
        a.manager.streams[orphan.session] = orphan
        previous = a.manager.on_status
        a.manager.on_status = lambda state: orphan.done.set() if state["session_id"] == orphan.session else None
        try:
            await asyncio.wait_for(orphan._monitor(), 1)
            self.assertFalse(pa.stop.is_set())
            self.assertEqual((a.active, b.active), ("direct", "direct"))
            await self.exchange(app, backend, b"unaffected")
        finally:
            a.manager.on_status = previous
            a.manager.streams.pop(orphan.session, None)
            for task in list(orphan.tasks):
                task.cancel()
            await asyncio.gather(*list(orphan.tasks), return_exceptions=True)

    async def test_invalid_logical_frame_only_resets_its_stream(self):
        app, backend, a, b, pa, pb, ca, cb = await self.pair()
        ca["host"], cb["player"] = pa, pb
        await self.wait_direct(a, b)
        await a.manager.receive_direct(pa, Frame(Kind.ACK, a.session, offset=a.tx_next + 1))
        self.assertTrue(a.done.is_set())
        self.assertEqual(a.reason, "protocol_error")
        self.assertFalse(pa.stop.is_set())

    async def test_confirmed_network_timeout_invalidates_physical_carrier(self):
        app, backend, a, b, pa, pb, ca, cb = await self.pair()
        ca["host"], cb["player"] = pa, pb
        await self.wait_direct(a, b)
        a.network_only = True
        a._state("direct")["last_pong"] -= 3
        a.manager.on_status = lambda state: a.done.set() if pa.stop.is_set() else None
        await asyncio.wait_for(a._monitor(), 1)
        self.assertTrue(pa.stop.is_set())

    async def test_flapping_delays_upgrade_and_invalid_partial_ack_rejected(self):
        app, backend, a, b, pa, pb, ca, cb = await self.pair()
        await self.exchange(app, backend, b"start")
        ca["host"], cb["player"] = pa, pb
        await self.wait_direct(a, b)
        for _ in range(3):
            a.commit(a.epoch + 1)
            a.fallback("injected_flap")
        self.assertGreater(a.retry_at - time.monotonic(), 119)
        offset = a.tx_next
        await a.manager.budget.reserve(4)
        a.tx[offset] = b"abcd"
        a.tx_next += 4
        a.tx_bytes += 4
        with self.assertRaises(ValueError):
            await a.receive(Frame(Kind.ACK, a.session, offset + 1, a.tx_next), "relay")


if __name__ == "__main__":
    unittest.main()
