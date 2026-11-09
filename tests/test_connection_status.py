import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from mclink.core.connection_status import connection_rows, auto_summary
from mclink.core.auto_stream import format_endpoint
from mclink.gui.app import MODE_AUTO, mode_owner_label_text, punch_step_text


class StatusTests(unittest.TestCase):
    def state(self, **overrides):
        data = dict(peer_ids=["a", "b", "c"], is_host=True, host_id="host", client_id="host",
            member_names={"a": "玩家甲", "b": "玩家乙", "c": "玩家丙"},
            member_ips={"a": "2001:db8::1", "b": "203.0.113.2"},
            member_conns={"a": "connected", "b": "connected", "c": "idle"},
            started_ids=["a", "b"], conn_mode="auto")
        data.update(overrides)
        return SimpleNamespace(**data)

    def test_routes_ips_unknown_values_and_mixed_streams(self):
        streams = [dict(peer="a", active_path="direct", relay_ready=True, direct_ready=True,
                        switch_phase="direct", direct_endpoint="[2001:db8::1]:30000", direct_rtt_ms=23),
                   dict(peer="b", active_path="relay", relay_ready=True, direct_ready=False,
                        switch_phase="retry_wait", relay_rtt_ms=60)]
        rows = connection_rows(self.state(), streams)
        self.assertEqual(len(rows), 3)
        self.assertIn("2001:db8::1", rows[0]["display_text"])
        self.assertIn("[2001:db8::1]:30000", rows[0]["display_text"])
        self.assertIn("备用就绪", rows[0]["display_text"])
        self.assertIn("203.0.113.2", rows[1]["display_text"])
        self.assertIn("稍后重试", rows[1]["display_text"])
        self.assertIn("未知", rows[2]["display_text"])
        self.assertIn("未开始", rows[2]["display_text"])
        self.assertIn("直连 1 人／中继 1 人", auto_summary(rows))
        streams.append(dict(peer="a", active_path="relay", relay_ready=True, switch_phase="ready"))
        self.assertEqual(connection_rows(self.state(), streams)[0]["active_path"], "mixed")

    def test_punch_ready_does_not_mean_game_carrying(self):
        rows = connection_rows(self.state(), direct_rows=[dict(peer="a", ready=True, addr="203.0.113.1:42")])
        self.assertEqual(rows[0]["active_path"], "none")
        self.assertIn("候选", rows[0]["display_text"])
        self.assertNotIn("P2P 直连", rows[0]["display_text"])

    def test_player_only_shows_host_and_manual_relay_has_ip(self):
        state = self.state(is_host=False, client_id="a", peer_ids=["host", "b"], conn_mode="relay",
                           member_ips={"host": "203.0.113.10"}, member_conns={"host": "connected"})
        rows = connection_rows(state)
        self.assertEqual([r["peer"] for r in rows], ["host"])
        self.assertIn("203.0.113.10", rows[0]["display_text"])
        self.assertEqual(format_endpoint(("2001:db8::2", 42)), "[2001:db8::2]:42")

    def test_gui_knows_auto_and_uses_runtime_summary(self):
        self.assertEqual(MODE_AUTO, "auto")
        self.assertIn("AUTO", mode_owner_label_text("auto", False, True))
        text, _ = punch_step_text({"mode": "auto", "connection_rows": [{"active_path": "relay"}]})
        self.assertIn("中继 1 人", text)

    def test_manual_progress_is_preserved_before_application_connection(self):
        state = self.state(conn_mode="stun")
        rows = connection_rows(state, direct_rows=[dict(peer="a", ready=False,
            row_text="等待同步起跑 2.1s【玩家甲】N/C")])
        self.assertIn("等待同步起跑 2.1s", rows[0]["display_text"])
        self.assertNotIn("连接准备中", rows[0]["display_text"])

    def test_network_readiness_is_visible_and_data_paths_take_precedence(self):
        network = dict(peer="a", channel="network", active_path="direct",
                       relay_ready=True, direct_ready=True, switch_phase="direct")
        row = connection_rows(self.state(), [network])[0]
        self.assertIn("P2P 直连（网络隧道）", row["display_text"])
        data = dict(peer="a", channel="data", active_path="relay",
                    relay_ready=True, direct_ready=False, switch_phase="relay")
        row = connection_rows(self.state(), [network, data])[0]
        self.assertEqual(row["active_path"], "relay")
        self.assertEqual(row["network_active_path"], "direct")
        self.assertIn("P2P 网络隧道仍就绪", row["display_text"])
        self.assertIn("当前应用连接由中继承载", row["display_text"])


if __name__ == "__main__":
    unittest.main()
