"""Real relay dispatch + room membership + released HostCore/PlayerCore sockets."""
import asyncio
import contextlib
import io
import os
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "client")]
import relay_server as relay
from mclink.core.room import RoomSession
from mclink.core.host_core import HostCore
from mclink.core.player_core import PlayerCore
from mclink.core.auto_stream import AutoManager
from mclink.core.auto_peer import AutoPeer
from mclink.core.direct_carry import PeerLink
from mclink.core.engine import MCLinkEngine
from mclink.config.settings import ClientConfig


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        relay.cfg.REQUIRE_ACCOUNT = False
        relay.cfg.RELAY_VERSION = "5.1.3"
        relay.rooms.clear()
        self.handlers = set()
        def dispatch(r, w):
            task = asyncio.create_task(relay.client_connected(r, w))
            self.handlers.add(task)
            task.add_done_callback(self.handlers.discard)
        self.server = await asyncio.start_server(dispatch, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.members, self.cores, self.writers = [], [], []
        self.peers, self.udp_sockets = [], []
        self.engines = []
        self.backend_accepts = 0
        self.backend_tasks = set()
        async def echo(r, w):
            self.backend_accepts += 1
            self.writers.append(w)
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        if w.can_write_eof():
                            w.write_eof()
                        return
                    w.write(data)
                    await w.drain()
            finally:
                w.close()
        def backend(r, w):
            task = asyncio.create_task(echo(r, w))
            self.backend_tasks.add(task)
            task.add_done_callback(self.backend_tasks.discard)
        self.backend = await asyncio.start_server(backend, "127.0.0.1", 0)
        self.mc_port = self.backend.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        try:
            for engine in self.engines:
                await asyncio.to_thread(engine.shutdown)
            for peer in self.peers:
                peer.close()
            for sock in self.udp_sockets:
                sock.close()
            for core in reversed(self.cores):
                await core.stop(graceful=True)
            for member in reversed(self.members):
                await member.leave()
            for room in list(relay.rooms.values()):
                await room.close(reason="test cleanup")
            self.server.close()
            self.backend.close()
            await self.server.wait_closed()
            await self.backend.wait_closed()
            for writer in self.writers:
                writer.close()
            for task in list(self.handlers | self.backend_tasks):
                task.cancel()
            await asyncio.gather(*list(self.handlers | self.backend_tasks), return_exceptions=True)
            await asyncio.gather(*(w.wait_closed() for w in self.writers), return_exceptions=True)
            relay.rooms.clear()
        finally:
            self.redirect.__exit__(None, None, None)

    async def member(self, cid, create=False, mode="auto"):
        member = RoomSession("127.0.0.1", self.port, cid, cid)
        self.members.append(member)
        if create:
            await member.create("auto-test", conn_mode=mode)
        else:
            await member.join("auto-test")
        return member

    async def ready(self, predicate, seconds=5):
        async def wait():
            while not predicate():
                await asyncio.sleep(.02)
        try:
            await asyncio.wait_for(wait(), seconds)
        except asyncio.TimeoutError:
            self.fail("integration readiness timeout\n" + self.output.getvalue()[-7000:])

    async def start_cores(self, mode="auto"):
        host_member = await self.member("host", create=True, mode=mode)
        await host_member.claim_host()
        player_member = await self.member("player")
        def manager(member):
            history = []
            def report(state):
                history.append(dict(state))
                asyncio.create_task(member.report_auto(state))
            m = AutoManager(on_status=report)
            m.history = history
            member.auto_manager = m
            return m
        host_manager, player_manager = manager(host_member), manager(player_member)
        host = HostCore("127.0.0.1", self.port, "auto-test", mc_port=self.mc_port,
            max_tunnels=4, max_players=8, client_id="host", username=host_member.username,
            token=host_member.guest_token,
            auto_manager=host_manager if mode == "auto" else None)
        self.cores.append(host)
        await host.start()
        await self.ready(lambda: host.control_connected_at is not None)
        player = PlayerCore("127.0.0.1", self.port, "auto-test", local_port=0,
            client_id="player", username=player_member.username, token=player_member.guest_token,
            auto_manager=player_manager if mode == "auto" else None)
        self.cores.append(player)
        await player.start()
        proxy_port = player.server.sockets[0].getsockname()[1]
        return host, player, host_manager, player_manager, proxy_port

    async def test_actual_cores_relay_first_multistream_and_panel_state(self):
        host, player, hm, pm, port = await self.start_cores()
        sockets = [await asyncio.open_connection("127.0.0.1", port) for _ in range(3)]
        self.writers.extend(w for r, w in sockets)
        for index, (r, w) in enumerate(sockets):
            data = bytes([index]) + os.urandom(128000)
            w.write(data)
            await w.drain()
            received = await asyncio.wait_for(r.readexactly(len(data)), 8)
            self.assertEqual(received, data, self.output.getvalue()[-5000:])
        self.assertEqual(self.backend_accepts, 3)
        self.assertEqual(len(hm.streams), 3)
        self.assertEqual(len(pm.streams), 3)
        self.assertEqual({s.active for s in hm.streams.values()}, {"relay"})
        self.assertEqual(set(hm.streams), set(pm.streams))
        room = relay.rooms["auto-test"]
        await self.ready(lambda: all(p.auto and p.auto.snapshot()["active_path"] == "relay" for p in room.sessions.values()))
        snapshot = await room.snapshot()
        self.assertEqual(snapshot["room_mode"], "auto")
        self.assertTrue(all(s["active_path"] == "relay" for s in snapshot["sessions"]))
        self.assertTrue(all(s["public_ip"] == "127.0.0.1" for s in snapshot["sessions"]))
        self.assertEqual(self.members[0].state.member_ips["player"], "127.0.0.1")

    async def test_capacity_64_members_and_63_real_player_connections(self):
        # Match the production event loop; asyncio debug tracking thousands
        # of member broadcast tasks would dominate this capacity test.
        asyncio.get_running_loop().set_debug(False)
        from settings_service import SettingsService
        from db_store import MemoryStore
        service = SettingsService(MemoryStore(), {"max_players": 8})
        await service.load()
        ok, _ = await service.set("max_players", 64)
        self.assertTrue(ok)
        previous = relay.settings
        relay.settings = service
        try:
            owner = RoomSession("127.0.0.1", self.port, "host", "host", max_players=64)
            self.members.append(owner)
            await owner.create("auto-test", conn_mode="relay")
            await owner.claim_host()
            host = HostCore("127.0.0.1", self.port, "auto-test", mc_port=self.mc_port,
                            max_tunnels=64, max_players=64, client_id="host",
                            username=owner.username, token=owner.guest_token)
            self.cores.append(host)
            await host.start()
            await self.ready(lambda: host.control_connected_at is not None)
            applications = []
            for index in range(63):
                member = await self.member("p" + str(index))
                core = PlayerCore("127.0.0.1", self.port, "auto-test", local_port=0,
                                  client_id=member.client_id, username=member.username,
                                  token=member.guest_token)
                r, w = await core.connect_relay()
                self.writers.append(w)
                self.assertEqual(await asyncio.wait_for(r.readline(), 12), b"CONNECTED\n")
                applications.append((r, w))
            async def exchange(index, reader, writer):
                payload = ("player-%02d" % index).encode()
                writer.write(payload)
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readexactly(len(payload)), 12), payload)
            await asyncio.gather(*(exchange(i, r, w) for i, (r, w) in enumerate(applications)))
            room = relay.rooms["auto-test"]
            self.assertEqual(room.room_capacity(), 64)
            self.assertEqual(len(room.members), 64)
            self.assertEqual(self.backend_accepts, 63)
            with self.assertRaisesRegex(Exception, "ROOM_FULL|房间已满"):
                await self.member("overflow")
            self.assertEqual(len(room.members), 64)
        finally:
            relay.settings = previous

    async def test_manual_relay_core_regression(self):
        *_, port = await self.start_cores(mode="relay")
        r, w = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(w)
        data = os.urandom(200000)
        w.write(data)
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.readexactly(len(data)), 8), data)
        self.assertEqual(self.backend_accepts, 1)

    async def udp_peers(self, hm, pm, player_id="player", peer_type=AutoPeer):
        a, b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        self.udp_sockets.extend([a, b])
        def link(sock, addr):
            return SimpleNamespace(sock=sock, peer_addr=addr, carried=False,
                stop_keepalive=lambda: None, start_keepalive=lambda: None)
        if peer_type is AutoPeer:
            hp = AutoPeer(player_id, link(a, b.getsockname()), hm)
            pp = AutoPeer("host", link(b, a.getsockname()), pm)
        else:
            hp = peer_type(player_id, link(a, b.getsockname()))
            pp = peer_type("host", link(b, a.getsockname()))
        self.peers.extend([hp, pp])
        self.assertEqual(await asyncio.gather(hp.ensure_started(3), pp.ensure_started(3)), [True, True])
        candidates = getattr(hm, "test_candidates", {})
        candidates[player_id] = hp
        hm.test_candidates = candidates
        hm.candidate = candidates.get
        pm.candidate = lambda peer: pp if peer == "host" else None
        return hp, pp

    async def test_real_udp_upgrade_relay_reattach_and_fallback(self):
        host, player, hm, pm, port = await self.start_cores()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(w)
        async def exchange(size=128000):
            data = os.urandom(size)
            w.write(data)
            await w.drain()
            self.assertEqual(await asyncio.wait_for(r.readexactly(len(data)), 10), data,
                             self.output.getvalue()[-5000:])
        await exchange()
        hs, ps = next(iter(hm.streams.values())), next(iter(pm.streams.values()))
        local_sockets = id(hs.writer), id(ps.writer)
        sid = hs.session
        hp, pp = await self.udp_peers(hm, pm)
        await self.ready(lambda: hs.active == ps.active == "direct", 8)
        await exchange(256000)
        room = relay.rooms["auto-test"]
        bridge = room.sessions[sid].auto
        old_player = bridge.ends["player"]
        old_player.writer.close()
        await self.ready(lambda: bridge.ends.get("player") is not None
                         and bridge.ends["player"] is not old_player
                         and hs.path_ready("relay") and ps.path_ready("relay"), 10)
        self.assertEqual((hs.active, ps.active), ("direct", "direct"))
        await exchange()
        hp.close()
        await exchange(256000)
        await self.ready(lambda: hs.active == ps.active == "relay", 4)
        self.assertEqual(local_sockets, (id(hs.writer), id(ps.writer)))
        self.assertEqual(set(hm.streams), {sid})
        self.assertEqual(self.backend_accepts, 1)
        self.assertIs(room.sessions[sid].auto, bridge)
        self.assertFalse(hs.done.is_set())
        self.assertFalse(ps.done.is_set())

    async def test_status_refresh_and_cancel_keep_shared_udp_and_existing_tcp(self):
        host, player, hm, pm, port = await self.start_cores()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        writer.write(b"persistent")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(10), 5), b"persistent")
        hs, ps = next(iter(hm.streams.values())), next(iter(pm.streams.values()))
        sid = hs.session
        sockets = (id(hs.writer), id(ps.writer))
        hp, pp = await self.udp_peers(hm, pm)
        await self.ready(lambda: hs.active == ps.active == "direct", 8)
        udp_sockets = (hp.sock.fileno(), pp.sock.fileno())
        for index in range(12):
            probe_reader, probe_writer = await asyncio.open_connection("127.0.0.1", port)
            self.writers.append(probe_writer)
            request = (b"server-status-" + str(index).encode()) * 8
            probe_writer.write(request)
            await probe_writer.drain()
            if index % 3 == 2:
                # A new refresh cancels the preceding status request while
                # relay pairing/AutoStream admission may still be in flight.
                probe_writer.transport.abort()
            else:
                self.assertEqual(await asyncio.wait_for(
                    probe_reader.readexactly(len(request)), 8), request)
                probe_writer.close()
                await probe_writer.wait_closed()
            await asyncio.sleep(.15)
            self.assertFalse(hp.stop.is_set(), self.output.getvalue()[-3000:])
            self.assertFalse(pp.stop.is_set(), self.output.getvalue()[-3000:])
            self.assertEqual((hs.active, ps.active), ("direct", "direct"))
        # Wait beyond the original 2-second orphan-flow PONG timeout.
        await asyncio.sleep(2.7)
        await self.ready(lambda: set(hm.streams) == set(pm.streams) == {sid}, 8)
        self.assertFalse(hp.stop.is_set())
        self.assertFalse(pp.stop.is_set())
        self.assertEqual((hp.sock.fileno(), pp.sock.fileno()), udp_sockets)
        self.assertEqual((id(hs.writer), id(ps.writer)), sockets)
        self.assertEqual((hs.active, ps.active), ("direct", "direct"))
        writer.write(b"still-direct")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(12), 5), b"still-direct")

    async def test_manual_stun_uses_released_direct_transport(self):
        host, player, hm, pm, port = await self.start_cores(mode="stun")
        hp, pp = await self.udp_peers(hm, pm, peer_type=PeerLink)
        host.direct_path_provider = lambda: hp
        host.direct_backend_setter = lambda address, port: hp.set_backend(address, port)
        player.direct_path_provider = lambda: pp
        r, w = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(w)
        payload = os.urandom(48000)
        w.write(payload)
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.readexactly(len(payload)), 10), payload)
        room = relay.rooms["auto-test"]
        self.assertTrue(all(p.auto is None for p in room.sessions.values()))
        self.assertTrue(all(p.player_bytes == p.tunnel_bytes == 0 for p in room.sessions.values()))

    async def test_three_players_mixed_routes_and_new_flow_relay_first(self):
        host, player, hm, pm, port = await self.start_cores()
        managers, ports = [pm], [port]
        for cid in ("player2", "player3"):
            member = await self.member(cid)
            manager = AutoManager(on_status=lambda state, member=member: asyncio.create_task(member.report_auto(state)))
            member.auto_manager = manager
            core = PlayerCore("127.0.0.1", self.port, "auto-test", local_port=0,
                client_id=cid, username=member.username, token=member.guest_token,
                auto_manager=manager)
            self.cores.append(core)
            await core.start()
            managers.append(manager)
            ports.append(core.server.sockets[0].getsockname()[1])
        sockets = [await asyncio.open_connection("127.0.0.1", port) for port in ports]
        self.writers.extend(w for _, w in sockets)
        async def exchange(index):
            r, w = sockets[index]
            data = bytes([index]) + os.urandom(16000)
            w.write(data)
            await w.drain()
            self.assertEqual(await asyncio.wait_for(r.readexactly(len(data)), 8), data)
        await asyncio.gather(*(exchange(i) for i in range(3)))
        await self.udp_peers(hm, pm)
        hp3, _ = await self.udp_peers(hm, managers[2], "player3")
        await self.ready(lambda: all(next(iter(m.streams.values())).active == "direct" for m in (pm, managers[2])), 8)
        self.assertEqual(next(iter(managers[1].streams.values())).active, "relay")
        hp3.close()
        await self.ready(lambda: next(iter(managers[2].streams.values())).active == "relay", 4)
        self.assertEqual(next(iter(pm.streams.values())).active, "direct")
        await asyncio.gather(*(exchange(i) for i in range(3)))
        self.assertEqual(self.backend_accepts, 3)
        # A new TCP stream for an already direct player still starts framed relay.
        r, w = await asyncio.open_connection("127.0.0.1", ports[0])
        self.writers.append(w)
        w.write(b"new-flow")
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.readexactly(8), 5), b"new-flow")
        old_sids = {next(iter(m.streams)) for m in managers}
        new_sids = set(hm.streams) - old_sids
        self.assertEqual(len(new_sids), 1)
        new_sid = new_sids.pop()
        self.assertEqual([s for s in hm.history if s["session_id"] == new_sid][0]["active_path"], "relay")

    async def test_owner_permissions_and_incompatible_existing_member(self):
        owner = await self.member("owner", create=True, mode="relay")
        host = await self.member("host")
        await host.claim_host()
        ok, _ = await host.set_conn_mode("auto")
        self.assertFalse(ok)
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(w)
        w.write(b"JOIN __mclink__ old old - auto-test ver=5.1.3\n")
        await w.drain()
        self.assertTrue((await r.readline()).startswith(b"OK JOIN"))
        ok, reason = await owner.set_conn_mode("auto")
        self.assertFalse(ok)
        self.assertIn("old", reason)
        self.assertEqual(relay.rooms["auto-test"].conn_mode, "relay")

    async def test_capability_gate_ignores_version_bypass(self):
        await self.member("host", create=True)
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(w)
        with patch.object(relay, "allow_version_mismatch", return_value=True):
            w.write(b"JOIN __mclink__ old old - auto-test ver=3.9.20\n")
            await w.drain()
            response = await r.readline()
        self.assertTrue(response.startswith(b"ERROR AUTO_UNSUPPORTED"), response)
        self.assertEqual(len(relay.rooms["auto-test"].members), 1)

    async def test_named_guest_receives_reusable_server_identity(self):
        member = await self.member("named", create=True)
        self.assertTrue(member.guest_token)
        self.assertNotEqual(member.guest_token, "-")
        key = member.guest_token
        await member.leave()
        other = RoomSession("127.0.0.1", self.port, "named", "named", guest_token=key)
        self.members.append(other)
        await other.create("auto-test", conn_mode="auto")
        self.assertEqual(other.guest_token, key)

    async def test_engine_updates_credentials_without_replacing_game_socket(self):
        await self.start_cores()
        engine = MCLinkEngine()
        self.engines.append(engine)
        await asyncio.to_thread(engine.open_room, "127.0.0.1", self.port,
            "engine-player", "engine-player", "", "auto-test")
        cfg = ClientConfig(server_host="127.0.0.1", server_port=self.port,
            room="auto-test", role="player", client_id="engine-player",
            username="engine-player", local_port=0, mode="auto")
        game = await asyncio.to_thread(engine.start_session, cfg)
        port = game.core.server.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(w)
        w.write(b"before-login")
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.readexactly(12), 5), b"before-login")
        stream = next(s for s in engine.auto_manager.streams.values()
                      if s.metadata.get("channel") != "network")
        original_writer = id(stream.writer)
        old_credential = game.core.token
        async def verified_account(token):
            return (True, {"username": "signed-in"}) if token == "verified-test-account" else (False, "AUTH_INVALID_TOKEN")
        with patch.object(relay.room_members, "_identity_verify", verified_account):
            ok, message, _ = await asyncio.to_thread(engine.room_send_identity,
                "signed-in", "verified-test-account")
        self.assertTrue(ok, message + "\n" + self.output.getvalue()[-4000:])
        self.assertEqual(game.core.token, "verified-test-account")
        bridge = relay.rooms["auto-test"].sessions[stream.session].auto
        self.assertFalse(bridge.authorized("player", "engine-player", old_credential,
                                          bridge.keys["player"], bridge.generation))
        self.assertTrue(bridge.authorized("player", "engine-player", game.core.token,
                                         bridge.keys["player"], bridge.generation))
        old_attachment = bridge.ends["player"]
        old_attachment.writer.close()
        await self.ready(lambda: bridge.ends.get("player") is not None
            and bridge.ends["player"] is not old_attachment and stream.path_ready("relay"), 10)
        w.write(b"after-login")
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.readexactly(11), 5), b"after-login")
        self.assertEqual(original_writer, id(stream.writer))
        self.assertEqual(self.backend_accepts, 1)


if __name__ == "__main__":
    unittest.main()
