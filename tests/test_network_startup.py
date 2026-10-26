"""Start-to-tunnel regression: actual NAT, ingress, candidates and punch.

Only STUN destination configuration changes to local real UDP responders.
No DirectLink/PeerLink, measured endpoint or punched socket is mocked.
Both configured application ports begin with NO listening application.
"""
import asyncio
import contextlib
import dataclasses
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "client"), str(ROOT / "server"), str(ROOT / "tests")]
import test_auto_integration as fixtures
import stun_server
from mclink.core import stun
from mclink.core.engine import MCLinkEngine
from mclink.config.settings import ClientConfig


class NetworkStartupTests(unittest.IsolatedAsyncioTestCase):
    member = fixtures.IntegrationTests.member
    ready = fixtures.IntegrationTests.ready

    async def asyncSetUp(self):
        await fixtures.IntegrationTests.asyncSetUp(self)
        # Free the application port. Starting the tunnel must still succeed.
        self.backend.close()
        await self.backend.wait_closed()
        self.stun_transports = []
        loop = asyncio.get_running_loop()
        for _ in range(2):
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: stun_server.StunServerProtocol("startup-test"),
                local_addr=("127.0.0.1", 0))
            protocol.REFILL_PER_SEC = 500
            protocol.BURST = 500
            protocol.CHANGE_REFILL_PER_SEC = 500
            protocol.CHANGE_BURST = 500
            self.stun_transports.append(transport)
        self.patches = [
            patch.object(stun, "DEFAULT_STUN_PORT", self.stun_transports[0].get_extra_info("sockname")[1]),
            patch.object(stun, "DEFAULT_STUN_PORT2", self.stun_transports[1].get_extra_info("sockname")[1]),
            patch.object(stun, "STUN_SERVERS", []),
            patch.object(stun_server, "_TRANSPORTS", self.stun_transports),
            patch.object(stun_server, "_server", {"transports": self.stun_transports}),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        try:
            await fixtures.IntegrationTests.asyncTearDown(self)
        finally:
            for item in reversed(self.patches):
                item.stop()
            for transport in self.stun_transports:
                transport.close()

    async def start_engines(self, mode):
        result = []
        for cid, role in (("host", "host"), ("player", "player")):
            engine = MCLinkEngine()
            self.engines.append(engine)
            await asyncio.to_thread(engine.open_room, "127.0.0.1", self.port,
                cid, cid, "", "auto-test", create=role == "host", conn_mode=mode)
            if role == "host":
                ok, reason = await asyncio.to_thread(engine.claim_host)
                self.assertTrue(ok, reason)
            cfg = ClientConfig(server_host="127.0.0.1", server_port=self.port,
                room="auto-test", role=role, client_id=cid, username=cid,
                local_port=0, mc_port=self.mc_port, mode=mode, max_tunnels=2)
            await asyncio.to_thread(engine.start_session, cfg)
            await asyncio.to_thread(engine.room_set_started, True)
            if mode != "relay":
                engine.watch_room_nat(True, log=lambda text: self.output.write(text + "\n"))
            result.append(engine)
        return result

    async def test_auto_tunnels_upgrade_and_fallback_without_applications(self):
        host, player = await self.start_engines("auto")
        def network(engine):
            return [s for s in engine.auto_manager.streams.values()
                    if s.metadata.get("channel") == "network"]
        await self.ready(lambda: len(network(host)) == len(network(player)) == 1, 8)
        await self.ready(lambda: all(s.active == "direct" for e in (host, player) for s in network(e)), 20)
        self.assertEqual(self.backend_accepts, 0)
        self.assertTrue(host.room_nat_profile().public)
        self.assertTrue(player.room_nat_profile().public)
        self.assertNotEqual(host.room_direct().inbound_state, "unknown")
        self.assertNotEqual(player.room_direct().inbound_state, "unknown")
        room = fixtures.relay.rooms["auto-test"]
        self.assertFalse(room.sessions, "network tunnels must not occupy application slots")
        self.assertEqual(len(room.waiting_tunnels), 2)
        self.assertEqual(room.world_players(), 1, "network readiness is not entry into a game")
        rows = host.snapshot()["connection_rows"]
        self.assertIn("网络隧道", rows[0]["display_text"])
        self.assertEqual(rows[0]["active_path"], "direct")
        sid = network(host)[0].session
        bridge = room.auto_networks[sid]
        old = bridge.ends["player"]
        old.writer.close()
        await self.ready(lambda: bridge.ends.get("player") is not None
            and bridge.ends["player"] is not old
            and all(network(e)[0].path_ready("relay") for e in (host, player)), 10)
        self.assertTrue(all(network(e)[0].active == "direct" for e in (host, player)))
        # Suspend further upgrades and remove the actual shared UDP carriers.
        for engine in (host, player):
            stream = network(engine)[0]
            stream.retry_at = asyncio.get_running_loop().time() + 60
            for carrier in list(engine.room_direct()._carriers.values()):
                carrier.close()
        await self.ready(lambda: all(network(e)[0].active == "relay"
            and network(e)[0].path_ready("relay") for e in (host, player)), 5)
        self.assertEqual(network(host)[0].session, sid)
        self.assertEqual(self.backend_accepts, 0)
        # An arbitrary TCP application starts AFTER all tunnel activity.
        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
        self.backend = await asyncio.start_server(echo, "127.0.0.1", self.mc_port)
        core = player.current_session().core
        port = core.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        data = b"arbitrary TCP application\x00" + os.urandom(120000)
        writer.write(data)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(data)), 8), data)

    async def test_auto_relay_ready_while_all_stun_probes_are_blackholed(self):
        for transport in self.stun_transports:
            transport.get_protocol().datagram_received = lambda data, addr: None
        host, player = await self.start_engines("auto")
        def ready(engine):
            return any(s.metadata.get("channel") == "network" and s.path_ready("relay")
                       for s in engine.auto_manager.streams.values())
        await self.ready(lambda: ready(host) and ready(player), 5)
        self.assertEqual(self.backend_accepts, 0)
        self.assertTrue(all(row["active_path"] == "relay" for row in host.snapshot()["connection_rows"]))
        self.assertFalse(fixtures.relay.rooms["auto-test"].sessions)

    async def test_hot_switch_auto_to_manual_stun_and_relay_without_applications(self):
        host, player = await self.start_engines("auto")
        await self.ready(lambda: all(any(s.metadata.get("channel") == "network" and
            s.path_ready("relay") for s in e.auto_manager.streams.values()) for e in (host, player)), 8)
        for mode in ("stun", "relay"):
            ok, reason = await asyncio.to_thread(host.set_conn_mode, mode)
            self.assertTrue(ok, reason)
            await self.ready(lambda: all(e.room_state().conn_mode == mode for e in (host, player)))
            configs = [dataclasses.replace(e.current_session().config, mode=mode) for e in (host, player)]
            for engine in (host, player):
                engine.watch_room_nat(False)
            await asyncio.gather(*(asyncio.to_thread(e.stop_session) for e in (host, player)))
            await asyncio.gather(*(asyncio.to_thread(e.start_session, cfg)
                                  for e, cfg in zip((host, player), configs)))
            for engine in (host, player):
                await asyncio.to_thread(engine.room_set_started, True)
                engine.watch_room_nat(mode == "stun", log=lambda text: self.output.write(text + "\n"))
                self.assertIsNone(engine.current_session().core.auto_manager)
                self.assertEqual(type(engine.room_direct()).__name__, "RoomDirectLink")
            if mode == "stun":
                await self.ready(lambda: all(e.room_direct().ready and
                    any(c.started and not c.stop.is_set() for c in e.room_direct()._carriers.values())
                    for e in (host, player)), 20)
            else:
                await self.ready(lambda: any(host.current_session().core.tunnel_ready.values()))
            self.assertEqual(self.backend_accepts, 0)
        self.assertFalse(fixtures.relay.rooms["auto-test"].sessions)

    async def test_network_side_and_credentials_are_authoritative(self):
        host, player = await self.start_engines("auto")
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        # Knowing member IDs/capabilities is insufficient to impersonate Host.
        writer.write(b"AUTO_NETWORK auto-test player player wrong side=host peer=host caps=auto_stream_v1 ver=5.1.3\n")
        await writer.drain()
        self.assertEqual((await reader.readline()).strip(), b"ERROR AUTO_NETWORK_DENIED")

    async def test_manual_stun_full_startup_without_application_listener(self):
        host, player = await self.start_engines("stun")
        await self.ready(lambda: all(e.room_direct().ready and
            any(c.started and not c.stop.is_set() for c in e.room_direct()._carriers.values())
            for e in (host, player)), 20)
        self.assertEqual(self.backend_accepts, 0)
        for engine in (host, player):
            self.assertTrue(engine.room_nat_profile().public)
            self.assertNotEqual(engine.room_direct().inbound_state, "unknown")
            self.assertEqual(type(engine.room_direct()).__name__, "RoomDirectLink")
            self.assertIn("隧道已就绪", engine.snapshot()["connection_rows"][0]["display_text"])
        self.assertFalse(fixtures.relay.rooms["auto-test"].sessions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
