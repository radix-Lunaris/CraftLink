import asyncio
import sys
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from auto_relay import AutoBridge, CAPABILITY, STATE_TTL


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


if __name__ == "__main__":
    unittest.main()
