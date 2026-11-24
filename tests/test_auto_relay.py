import asyncio
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from auto_relay import AutoBridge, CAPABILITY, RelayAccounting, STATE_TTL
from admin_api import AdminAPI
from auth import hash_password
from db_store import FileAccountStore
from gatekeeper import AccountService, LoginThrottle, TokenManager

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from mclink.core.auto_stream import Frame, Kind


class Writer:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = SimpleNamespace(client_id="host", closed=False, detached=False,
            capabilities={CAPABILITY}, token="host-token", guest_token="")
        self.player = SimpleNamespace(client_id="player", closed=False, detached=False,
            capabilities={CAPABILITY}, token="-", guest_token="player-guest-token")
        self.room = SimpleNamespace(name="test", host_id="host", host_ip="203.0.113.1",
            auto_generation=uuid.uuid4().hex, closed=False, closing=False,
            members={"host": self.host, "player": self.player})
        pair = SimpleNamespace(session_id=uuid.uuid4().hex, player_id="player",
            tunnel_reader=None, player_reader=None, tunnel_writer=Writer(),
            player_writer=Writer(), player_ip="203.0.113.2")
        self.closed = []
        async def close(sid, reason=""):
            self.closed.append(sid)
        self.bridge = AutoBridge(self.room, pair, close)

    def report(self, path="direct", **overrides):
        return dict(generation=self.room.auto_generation, active_path=path,
            relay_ready=True, direct_ready=True, switch_phase=path,
            epoch=1, **overrides)

    def test_resume_requires_endpoint_identity_key_and_current_generation(self):
        b = self.bridge
        args = ("player", "player", "player-guest-token", b.keys["player"], b.generation)
        self.assertTrue(b.authorized(*args))
        for index, bad in ((0, "host"), (1, "host"), (2, "-"), (2, "forged"),
                           (3, b.keys["host"]), (4, uuid.uuid4().hex)):
            altered = list(args)
            altered[index] = bad
            self.assertFalse(b.authorized(*altered))
        self.player.detached = True
        self.assertFalse(b.authorized(*args))
        self.player.detached = False
        self.room.auto_generation = uuid.uuid4().hex
        self.assertFalse(b.authorized(*args))

    def test_stale_or_disagreeing_routes_never_claim_direct(self):
        b = self.bridge
        self.assertEqual(b.snapshot()["active_path"], "unknown")
        b.report(self.host, self.report())
        b.report(self.player, self.report("relay"))
        self.assertEqual(b.snapshot()["active_path"], "transitioning")
        b.report(self.player, self.report())
        self.assertEqual(b.snapshot()["active_path"], "direct")
        b.reports["host"]["updated_at"] = time.time() - STATE_TTL - 1
        state = b.snapshot()
        self.assertTrue(state["stale"])
        self.assertEqual(state["active_path"], "unknown")
        self.assertFalse(state["relay_ready"])

    def test_replacement_survives_obsolete_attachment_cleanup(self):
        b = self.bridge
        old = b.ends["player"]
        new = b.attach("player", None, Writer())
        old.close()
        self.assertIs(b.ends["player"], new)
        self.assertFalse(new.writer.closed)

    def test_terminal_report_revokes_resume_even_if_other_reset_lost(self):
        b = self.bridge
        b.report(self.host, self.report())
        b.report(self.player, self.report("none", last_reason="stopped"))
        self.assertTrue(b.closed)
        self.assertFalse(b.ends)
        self.assertFalse(b.authorized("player", "player", "player-guest-token",
                                      b.keys["player"], b.generation))

    async def test_pump_counts_split_data_and_excludes_control_payload(self):
        bridge = self.bridge
        bridge.pair.tunnel_bytes = 0
        session = bridge.pair.session_id
        raw = (Frame(Kind.PING, session, payload=b"probe").encode()
               + Frame(Kind.DATA, session, payload=b"game bytes").encode())
        reader = asyncio.StreamReader()
        for start in range(0, len(raw), 7):
            reader.feed_data(raw[start:start + 7])
        reader.feed_eof()
        # Exercise TCP fragmentation independently of StreamReader coalescing.
        async def read(_):
            return await reader.read(7)
        forwarded = bytearray()
        async def drain():
            pass
        await bridge._pump(SimpleNamespace(reader=SimpleNamespace(read=read)),
                           SimpleNamespace(writer=SimpleNamespace(write=forwarded.extend, drain=drain)),
                           "host")
        self.assertEqual(forwarded, raw)
        self.assertEqual(bridge.pair.tunnel_bytes, len(raw))
        self.assertEqual(bridge.snapshot()["relay_data_bytes"], {"host": 10, "player": 0})

    def test_accounting_never_realigns_to_a_foreign_session(self):
        session = self.bridge.pair.session_id
        accounting = RelayAccounting(session)
        foreign = Frame(Kind.DATA, uuid.uuid4().hex, payload=b"foreign").encode()
        correct = Frame(Kind.DATA, session, payload=b"correct").encode()
        self.assertEqual(accounting.forwarded_data_bytes(foreign + correct), 0)
        self.assertFalse(accounting.valid)
        self.assertEqual(accounting.forwarded_data_bytes(correct), 0)


class LightweightRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_accounts_login_and_deletion_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            path.write_text(json.dumps({"alice": hash_password("test-password")}), encoding="utf-8")
            store = FileAccountStore(path)
            await store.connect()
            service = AccountService(store, TokenManager(), LoginThrottle())
            ok, payload, _ = await service.login("alice", "test-password", "127.0.0.1")
            self.assertTrue(ok)
            self.assertEqual(payload["username"], "alice")
            self.assertFalse(await store.delete_user("alice"))
            path.write_text("{}", encoding="utf-8")
            await store.connect()
            self.assertIsNone(await store.get_user("alice"))

    async def test_lightweight_panel_preserves_admin_api_auth(self):
        async def stats():
            return {"rooms": 1}

        api = AdminAPI("127.0.0.1", 0, {"stats": stats}, None, None, None, token="test-admin-token")
        server = await api.start()
        self.addAsyncCleanup(api.stop)
        port = server.sockets[0].getsockname()[1]

        async def get(path, token=None):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                authorization = f"Authorization: Bearer {token}\r\n" if token else ""
                writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n{authorization}\r\n".encode())
                await writer.drain()
                return await asyncio.wait_for(reader.read(), 2)
            finally:
                writer.close()
                await writer.wait_closed()

        with patch.dict("os.environ", {"MCLINK_LITE_PANEL": "1"}):
            panel = await get("/")
            self.assertIn(b"text/html", panel)
            self.assertNotIn(b"test-admin-token", panel)
            self.assertIn(b"401 Unauthorized", await get("/stats"))
            self.assertIn(b"200 OK", await get("/stats", "test-admin-token"))


if __name__ == "__main__":
    unittest.main()
