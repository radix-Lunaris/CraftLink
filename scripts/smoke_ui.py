"""Construct real Tk widgets while withdrawn, using an isolated profile."""
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))


def main():
    with tempfile.TemporaryDirectory(prefix="craftlink-ui-smoke-") as profile:
        with patch.dict(os.environ, {"APPDATA": profile, "LOCALAPPDATA": profile}):
            import customtkinter as ctk
            from mclink.gui.app import MCLinkApp
            from mclink.core.connection_status import connection_rows
            root = ctk.CTk()
            root.withdraw()
            app = None
            try:
                app = MCLinkApp(root)
                app.mode_var.set("auto")
                app._on_mode_change()
                assert app.config.mode == "auto"
                state = SimpleNamespace(is_host=True, client_id="host", host_id="host",
                    peer_ids=["a", "b", "c"], member_names={"a": "玩家甲", "b": "玩家乙", "c": "玩家丙"},
                    member_ips={"a": "2001:db8:1234:5678::1", "b": "203.0.113.2", "c": "203.0.113.3"},
                    member_conns={"a": "connected", "b": "connected"},
                    started_ids=["a", "b"], conn_mode="auto")
                rows = connection_rows(state, [
                    dict(peer="a", active_path="direct", relay_ready=True, direct_ready=True,
                         switch_phase="direct", direct_endpoint="[2001:db8:1234:5678::1]:30000", direct_rtt_ms=24),
                    dict(peer="b", active_path="relay", relay_ready=True, direct_ready=False,
                         switch_phase="retry_wait", relay_rtt_ms=60)])
                with patch.object(app.engine, "snapshot", return_value={"connection_rows": rows}):
                    app._render_hole_rows(None, punching=False)
                root.update_idletasks()
                assert not root.winfo_viewable(), "smoke test must keep the window hidden"
                assert len(app._hole_rows) == 2
                widths = []
                for row in rows:
                    if row["peer"] not in app._hole_rows: continue
                    label = app._hole_rows[row["peer"]][1]
                    assert row["public_ip"] in label.cget("text")
                    widths.append(round(label.winfo_reqwidth() / label._get_widget_scaling()))
                assert max(widths) <= 530, widths
                print(json.dumps({"ui_smoke": "ok", "auto_selectable": True,
                                  "player_rows": 2, "row_widths": widths}))
            finally:
                if app:
                    app.engine.shutdown()
                root.destroy()
                logging.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
