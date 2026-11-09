"""
事件泵的离线回归（P0-1 / P0-2）。

两条必须钉死的行为：

  P0-1  静默窗口开着的时候，rebuild_done / rebuild_failed / snapshot / state
        四类事件**必须**各自走进自己的分支，不许被静默兜底吞掉。
  P0-2  静默窗口的过期**不依赖事件到达**：队列空 + 时间推过上限，`_tick` 每秒
        调用的 `_expire_silent_window()` 也要把窗口关掉。

跑法：  python3 -m pytest tests/test_event_pump.py -q
        （或者直接 python3 tests/test_event_pump.py）
"""

import sys
import types
import unittest
from unittest.mock import MagicMock

# tkinter 在这个环境里装不了（也用不上）：桩掉即可，我们只测事件分派逻辑。
for _name in (
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.ttk",
    "tkinter.font",
):
    sys.modules.setdefault(_name, MagicMock())

_ROOT = __file__.rsplit("/", 2)[0]
sys.path.insert(0, _ROOT + "/client")

from mclink.gui.app import MCLinkApp  # noqa: E402


class FakeEngine:
    """只提供事件泵用到的那一个方法。"""

    def __init__(self, events):
        self._events = list(events)
        self.room = object()

    def drain_events(self):
        events, self._events = self._events, []
        return events

    def room_current(self):
        return self.room


class SilentPump(unittest.TestCase):
    """一个最小替身：只带事件泵真正会碰到的属性。"""

    def make_app(self, events, silent=True, until=None):
        app = types.SimpleNamespace()
        app.engine = FakeEngine(events)
        app.log = lambda text, kind="normal": app.logs.append((kind, text))
        app.logs = []
        app.running = False
        app.last_snapshot = None
        app.config = types.SimpleNamespace(role="host")
        app._reconnect_phase = "stopped"
        app._mode_switch_silent = silent
        app._mode_switch_silent_until = (
            FakeClock.NOW if until is None else until
        )
        app.state_events = []
        app.core_events = []
        app.rebuild_done = 0
        app.rebuild_failed = 0

        # 事件泵用到的分支动作，全部换成记录用的替身。
        app._handle_core_event = lambda name, data: app.core_events.append(
            (name, data)
        )
        app._reconnect_succeeded = lambda: app.logs.append(("ok", "reconnected"))
        app._is_terminal_failure = lambda reason: False
        app._reconnect_terminal = lambda reason: None
        app._trigger_auto_reconnect = lambda why: app.logs.append(
            ("warn", f"retry:{why}")
        )
        app._update_nat = lambda: None
        app._sync_room_nat = lambda: None
        app._refresh_room_ui = lambda: None
        app.connect_hint = MagicMock()
        app._refuse_outdated = lambda reason: False

        # 绑上真实实现：被测的就是这三个方法本身。
        app._drain_events = types.MethodType(MCLinkApp._drain_events, app)
        app._hold_silent_rebuild_ui = types.MethodType(
            MCLinkApp._hold_silent_rebuild_ui, app
        )
        app._expire_silent_window = types.MethodType(
            MCLinkApp._expire_silent_window, app
        )
        return app

    def test_rebuild_done_not_swallowed(self):
        """P0-1：静默窗口开着时 rebuild_done 仍要走到自己的分支。"""
        app = self.make_app([{"kind": "rebuild_done"}], silent=True)

        app._drain_events()

        # 分支执行过 = 窗口被关掉（原来这里是永远关不上的）。
        self.assertFalse(app._mode_switch_silent)

    def test_rebuild_failed_not_swallowed(self):
        """P0-1：rebuild_failed 要触发自动重试，而不是被静默吞掉。"""
        app = self.make_app(
            [
                {
                    "kind": "rebuild_failed",
                    "data": {"text": "boom"},
                }
            ],
            silent=True,
        )

        app._drain_events()

        self.assertFalse(app._mode_switch_silent)
        self.assertTrue(any("retry:rebuild failed" in t for _, t in app.logs))
        # 原来的 NameError（分支里引用了不存在的 data）已经不存在了。
        self.assertFalse(any("NameError" in t for _, t in app.logs))

    def test_state_error_not_swallowed(self):
        """P0-1：state=ERROR 要立刻进日志，不能被压到窗口过期。"""
        app = self.make_app(
            [
                {
                    "kind": "state",
                    "state": "ERROR",
                    "reason": "server said no",
                }
            ],
            silent=True,
        )

        app._drain_events()

        self.assertTrue(any("server said no" in t for _, t in app.logs))

    def test_snapshot_not_swallowed(self):
        """P0-1：snapshot 要更新 last_snapshot。"""
        app = self.make_app(
            [{"kind": "snapshot", "data": {"members": 3}}], silent=True
        )

        app._drain_events()

        self.assertEqual(app.last_snapshot, {"members": 3})

    def test_silent_window_still_holds_ui(self):
        """静默窗口仍然要把界面按在"在连接"——只是不能再吞事件。"""
        app = self.make_app([{"kind": "log", "text": "hi"}], silent=True)

        app._drain_events()

        self.assertTrue(app.running)
        self.assertEqual(app._reconnect_phase, "reconnecting")

    def test_expire_closes_window_without_events(self):
        """P0-2：队列空 + 时间已过，也要把窗口关掉。"""
        app = self.make_app([], silent=True, until=0.0)

        app._expire_silent_window()

        self.assertFalse(app._mode_switch_silent)

    def test_window_stays_open_before_deadline(self):
        """没到点、人还在房间里，窗口不许提前关。"""
        app = self.make_app([], silent=True, until=1e9)

        app._expire_silent_window()

        self.assertTrue(app._mode_switch_silent)

    def test_window_closes_when_room_gone(self):
        """离开房间 -> 无条件恢复，不看时间。"""
        app = self.make_app([], silent=True, until=1e9)
        app.engine.room = None

        app._expire_silent_window()

        self.assertFalse(app._mode_switch_silent)


class FakeClock:
    NOW = 0.0


if __name__ == "__main__":
    unittest.main()
