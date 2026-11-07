"""Failure-driven regressions for terminated TCP paths and transient recovery."""
import asyncio
import contextlib
import sys
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('client', 'server', 'tests')]
from mclink.core import common, host_core
from mclink.core.engine import MCLinkEngine
import relay_server as relay
import test_auto_integration as fixtures


class BlockedWriter:
    def __init__(self):
        self.closed = False
        self.aborted = False
        self.transport = self
        self.finished = asyncio.Event()

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        await self.finished.wait()

    def abort(self):
        self.aborted = True
        self.finished.set()


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_close_timeout_aborts(self):
        writer = BlockedWriter()
        await asyncio.wait_for(common.close_writer(writer, timeout=.03), .3)
        self.assertTrue(writer.aborted)

    async def test_client_cancel_aborts_and_propagates(self):
        writer = BlockedWriter()
        task = asyncio.create_task(common.close_writer(writer))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(writer.aborted)

    async def test_relay_close_timeout_aborts(self):
        writer = BlockedWriter()
        await asyncio.wait_for(relay.close_writer(writer, timeout=.03), .3)
        self.assertTrue(writer.aborted)

    async def test_pair_starts_both_closes_before_waiting(self):
        room = relay.Room('recovery-test')
        player, host = BlockedWriter(), BlockedWriter()
        pair = relay.Pair('one', None, player, None, host)
        room.sessions[pair.session_id] = pair
        task = asyncio.create_task(room.remove_session(pair.session_id))
        try:
            await asyncio.sleep(.04)
            self.assertTrue(player.closed)
            self.assertTrue(host.closed, 'Player drain must not hold the Host socket open')
        finally:
            player.abort(); host.abort()
            await task


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_pong_invalidates_control_and_idle_pool(self):
        events, writers, handlers = [], [], set()
        async def respond(r, w):
            writers.append(w)
            hello = await r.readline()
            w.write(b'WAIT\n' if hello.startswith(b'HOST_TUNNEL ') else b'OK HOST room=test\n')
            await w.drain()
            await r.read()
        def accept(r, w):
            task = asyncio.create_task(respond(r, w))
            handlers.add(task); task.add_done_callback(handlers.discard)
        server = await asyncio.start_server(accept, '127.0.0.1', 0)
        core = host_core.HostCore('127.0.0.1', server.sockets[0].getsockname()[1],
            'test', max_tunnels=1, reconnect_delay=.5,
            on_event=lambda name, data: events.append((name, data)))
        try:
            with patch.object(host_core, 'CONTROL_PING_INTERVAL', .04, create=True), \
                 patch.object(host_core, 'CONTROL_PONG_TIMEOUT', .15, create=True):
                await core.start()
                deadline = time.monotonic() + 1
                while not any(n == 'control_offline' for n, _ in events) and time.monotonic() < deadline:
                    await asyncio.sleep(.02)
                self.assertTrue(any(n == 'control_offline' for n, _ in events))
                self.assertFalse(core.control_online)
                self.assertFalse(any(core.tunnel_ready.values()))
        finally:
            await core.stop()
            for w in writers:
                w.transport.abort()
            for t in list(handlers): t.cancel()
            await asyncio.gather(*list(handlers), return_exceptions=True)
            server.close(); await server.wait_closed()


class RecoveryIntegrationTests(fixtures.IntegrationTests):
    # Reuse setup only; inherited integration tests are not duplicated below.
    async def test_unexpected_control_loss_is_not_active_stop(self):
        host, player, *_ = await self.start_cores('relay')
        notices = []
        member = self.members[1]
        original = member._on_line
        def receive(line):
            if line.startswith(('HOST_LEFT', 'HOST_STOPPED')):
                notices.append(line.split()[0])
            original(line)
        member._on_line = receive
        host.reconnect_delay = 5
        relay.rooms['auto-test'].host_writer.transport.abort()
        await self.ready(lambda: bool(notices), 4)
        self.assertEqual(notices, ['HOST_LEFT'])

    async def test_host_control_rebuild_accepts_new_application_without_tool_restart(self):
        host, player, _, _, port = await self.start_cores('relay')
        async def exchange(payload):
            r, w = await asyncio.open_connection('127.0.0.1', port)
            self.writers.append(w)
            w.write(payload); await w.drain()
            self.assertEqual(await asyncio.wait_for(r.readexactly(len(payload)), 8), payload)
            w.close()
        await exchange(b'before-network-change')
        old = relay.rooms['auto-test'].host_writer
        old.transport.abort()
        await self.ready(lambda: relay.rooms['auto-test'].host_writer is not None
                         and relay.rooms['auto-test'].host_writer is not old
                         and any(host.tunnel_ready.values()), 10)
        await exchange(b'after-network-change')
        self.assertTrue(player.running)

    async def test_authenticated_owner_recovery_keeps_same_room_and_role(self):
        from mclink.config.settings import ClientConfig
        engine = MCLinkEngine(); self.engines.append(engine)
        await asyncio.to_thread(engine.open_room, '127.0.0.1', self.port,
            'owner', 'owner', '', 'auto-test', create=True)
        self.assertTrue((await asyncio.to_thread(engine.claim_host))[0])
        cfg = ClientConfig(server_host='127.0.0.1', server_port=self.port,
            client_id='owner', username='owner', room='auto-test', role='host',
            mode='relay', mc_port=self.mc_port, max_tunnels=1)
        await asyncio.to_thread(engine.start_session, cfg)
        room = relay.rooms['auto-test']; old = engine.room_current()
        engine._room_was_host = True
        epoch = engine.recovery_epoch
        # Keep the old server member active, as during a change of network.
        with patch.object(old, 'stop', return_value=None):
            ok, message = await asyncio.to_thread(engine.auto_rejoin)
        self.assertTrue(ok, message)
        self.assertIs(relay.rooms.get('auto-test'), room)
        self.assertTrue(engine.room_state().is_owner)
        self.assertTrue(engine.room_state().is_host)
        self.assertEqual(engine.recovery_epoch, epoch)
        await old.stop()
        await asyncio.to_thread(engine.start_session, cfg, recovery_epoch=epoch)
        await self.ready(lambda: engine._session.core.control_online
                         and any(engine._session.core.tunnel_ready.values()))

    async def test_player_disconnect_releases_host_backend_and_allows_rejoin(self):
        host, player, _, _, port = await self.start_cores('relay')
        for index in range(5):
            r, w = await asyncio.open_connection('127.0.0.1', port)
            self.writers.append(w)
            data = b'x' * 128000
            w.write(data); await w.drain()
            self.assertEqual(await asyncio.wait_for(r.readexactly(len(data)), 8), data)
            w.transport.abort()
            await self.ready(lambda: not host.active_sessions and not relay.rooms['auto-test'].sessions, 5)
        self.assertTrue(player.running)
        self.assertLessEqual(len(host._tunnel_workers), host.max_tunnels)

    async def test_recovery_wrong_credential_cannot_take_owner_seat(self):
        from mclink.core.room import RoomSession, RoomError
        owner = await self.member('host', create=True, mode='relay')
        room = relay.rooms['auto-test']
        candidate = RoomSession('127.0.0.1', self.port, 'host', 'host', 'invalid-test-token')
        candidate.recovering = True
        self.members.append(candidate)
        with self.assertRaises(RoomError):
            await candidate.join('auto-test')
        self.assertIs(relay.rooms.get('auto-test'), room)
        self.assertIs(room.owner, room.members['host'])
        self.assertTrue(owner.running)

    async def test_recovery_cannot_bypass_kick(self):
        from mclink.core.room import RoomSession, RoomError
        owner = await self.member('host', create=True, mode='relay')
        player = await self.member('player')
        credential = player.guest_token
        await owner.kick('player')
        await self.ready(lambda: relay.room_registry.recall_removal('player')[1] == 'kick')
        candidate = RoomSession('127.0.0.1', self.port, 'player', 'player', credential)
        candidate.recovering = True
        self.members.append(candidate)
        with self.assertRaises(RoomError):
            await candidate.join('auto-test')


class StaleTeardownTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_host_gone_does_not_remove_new_session(self):
        engine = MCLinkEngine()
        old, new = object(), object()
        engine._session = new
        await engine._async_host_gone_teardown('HOST_LEFT', expected=old)
        self.assertIs(engine._session, new)


class BackpressureResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_behind_paused_reader_does_not_leave_host_backend(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import audit_recovery_stalls
        results = await audit_recovery_stalls.run([0])
        self.assertEqual(len(results), 1)
        self.assertLess(results[0]['cleanup_seconds'], 5)


class ControllerRecoveryTests(unittest.TestCase):
    def make_controller(self, requested):
        from mclink.gui.controller import ClientController
        c = ClientController.__new__(ClientController)
        c.engine = MCLinkEngine()
        c.engine._connection_requested = requested
        c.engine.room_state = lambda: SimpleNamespace(is_host=False)
        c.running = requested
        c._refresh_room_ui = lambda: None
        c._host_down_hint = lambda text: None
        c.log = lambda *a, **k: None
        c.attempts = []
        c._trigger_auto_reconnect = lambda reason: c.attempts.append(reason)
        return c

    def test_transient_host_loss_recovers_only_previously_started_member(self):
        for requested in (True, False):
            c = self.make_controller(requested)
            c._on_host_gone('left', 'network interruption')
            self.assertEqual(bool(c.attempts), requested)

    def test_explicit_host_stop_cancels_intent(self):
        c = self.make_controller(True)
        c._on_host_gone('stopped', 'explicit stop')
        self.assertFalse(c.engine.connection_requested)
        self.assertFalse(c.attempts)


# Only new tests belong to this suite; the original suite runs independently.
for _name in tuple(vars(fixtures.IntegrationTests)):
    if _name.startswith('test_'):
        setattr(RecoveryIntegrationTests, _name, None)


class IntentTests(unittest.TestCase):
    def test_explicit_stop_invalidates_late_recovery(self):
        engine = MCLinkEngine()
        engine._connection_requested = True
        old = engine.recovery_epoch
        engine.stop_session(graceful=True)
        self.assertFalse(engine.connection_requested)
        self.assertNotEqual(engine.recovery_epoch, old)


if __name__ == '__main__':
    unittest.main()
