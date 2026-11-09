"""
MC-Link client -- async engine.

The whole network core is asyncio. tkinter is single threaded, so the engine
runs ONE asyncio event loop on a dedicated daemon thread and exposes a small
thread safe API:

    engine.start_host(cfg)      # called from the GUI thread
    engine.start_player(cfg)
    engine.stop_session()
    engine.drain_events()       # called from the GUI thread every 100 ms
    engine.snapshot()           # cheap, lock free status copy

Nothing in the core is ever touched from the GUI thread directly.
"""

import asyncio
import asyncio
import contextlib
import queue
import threading
import time

from .. import log as logmod
from .session import HostSession, PlayerSession


class MCLinkEngine:
    #: 打洞要开始时，手上的 NAT 画像超过这个年龄就让监测**立刻**重测一轮
    #: （见 watch_room_direct / RoomNatMonitor.poke）。30 秒与打洞自己的
    #: `direct.NAT_HINT_MAX_AGE_S` 对齐：比它旧的画像打洞本来也不会用。
    NAT_POKE_MAX_AGE_S = 30.0

    #: 两次"戳"之间至少隔这么久（那个函数每秒被调一次）。
    NAT_POKE_COOLDOWN_S = 20.0

    def __init__(self, max_events=2000):
        self._events = queue.Queue(maxsize=max_events)

        self._loop = None
        self._thread = None
        self._ready = threading.Event()

        self._session = None
        self._session_lock = threading.Lock()

        #: Live room membership (V3). Separate from the Minecraft session: a
        #: person can be in a room without being connected to a world.
        self._room = None
        self._room_lock = threading.Lock()

        #: How we got into the room we are in, so `auto_rejoin()` can repeat it
        #: after the membership connection drops. Kept deliberately as plain
        #: data: the session object it came from is gone by then.
        self._room_join_args = None

        #: Whether we were the host when the link dropped (see auto_rejoin).
        self._room_was_host = False

        #: The presence channel: one connection that says "this client is open
        #: and logged in". See start_presence().
        self._presence_task = None
        self._presence_keeper = None

        self._started_at = None

        #: The relay host we last talked to. Kept here (not read off a config
        #: object) because the room-level NAT monitor needs it while there is no
        #: session yet -- see watch_room_nat.
        self._relay_host = ""
        self._relay_port = 0

        #: The room-level NAT measurement (STUN mode). Created on demand.
        self._nat_monitor = None

        #: 游客临时 token（登录会话级；`OK JOIN`/`OK CREATE` 的 `guest=`，
        #: ADDITIVE）。同一次登录会话里换房时放回握手行 -> 名字不变；
        #: 登出 / 换账号 / 重启由 GUI 清空。**绝不落盘。**
        self._guest_token = ""

        #: 「打洞前让 NAT 画像立刻重测一轮」的防抖（见 watch_room_direct）。
        #: 那个函数每秒都被调一次，所以冷却必须比一次测量长。
        self._last_nat_poke_at = 0.0

        #: The room-level punch (STUN mode): punch on join, hold the result.
        self._room_direct = None
        from .auto_stream import AutoManager
        self.auto_manager = AutoManager(
            candidate=lambda peer: getattr(self._room_direct, "_carriers", {}).get(peer),
            on_status=self._auto_status,
            on_traffic=self.record_traffic,
            log=lambda text: self._push("log", text=text),
        )
        self._auto_log_states = {}

        #: ★ v3.9.16：最后一次"host 没了"的事实（`host_gone()` 读它）。
        #:
        #: 放在引擎（而不是房间/会话）上，是因为它跨对象：房间还在、会话已经
        #: 拆了，界面要能问出"最后一次是怎么没的"。离开房间时清空。
        self._host_gone = None

        #: The room-level PUNCH (STUN mode): it starts the moment both ends

    # --------------------------------------------------------
    # Event queue
    # --------------------------------------------------------

    def _push(self, kind, **payload):
        item = {"kind": kind, "time": time.time(), **payload}

        try:
            self._events.put_nowait(item)
        except queue.Full:
            # Drop the oldest entry: the UI is behind, but must not block the
            # network core.
            with contextlib.suppress(queue.Empty, queue.Full):
                self._events.get_nowait()
                self._events.put_nowait(item)

        # Every UI event is ALSO a line in the log file, with its raw payload.
        #
        # This is the one place that sees the whole event stream, so it is the
        # only place where "the log records what actually happened" can be
        # guaranteed for all of them -- including the events nobody remembered
        # to log individually. The payload goes in VERBATIM (session ids, room
        # tokens, reason strings exactly as the relay sent them): it is
        # evidence, and evidence that has been tidied up is no longer evidence.
        # `snapshot` is EXCLUDED, and that is not a detail.
        #
        # The GUI asks for a snapshot once a second, so logging it wrote a
        # ~1.5 KB JSON line per second, per process. The owner's host.log reached
        # **4.9 MB in fourteen hours and was almost entirely snapshots** -- the
        # handful of real events (the punch, the game sessions, the failures)
        # were buried so deep that reading the file by hand was hopeless, which
        # is exactly what a log must never do. A log that records everything at
        # 1 Hz records nothing.
        #
        # Every OTHER event still goes in verbatim: those are the transitions,
        # and there are only a few of them.
        if kind != "snapshot":
            with contextlib.suppress(Exception):
                logmod.event(
                    f"ui.{kind}",
                    **{
                        key: value
                        for key, value in payload.items()
                        if key != "time"
                    },
                )

    def _auto_status(self, state):
        sid = state["session_id"]
        key = (state["active_path"], state["switch_phase"], state["last_reason"])
        if self._auto_log_states.get(sid) != key:
            self._auto_log_states[sid] = key
            self.log_event("auto.path.changed", **state)
            self._push("auto_path", data=state)
        if state.get("switch_phase") == "ended":
            self._auto_log_states.pop(sid, None)
        room = self._room
        if room and room.running:
            async def report():
                with contextlib.suppress(Exception):
                    await room.report_auto(state)
            asyncio.create_task(report())

    def log_event(self, name, **fields):
        """
        Record one structured fact from core code.

        Core code can run before the window exists (selfcheck, headless use,
        a punch that starts before the GUI is built), so it cannot go through
        the GUI's `log()`. This forwards straight to `mclink.log`, which owns
        the file and is available from process start.
        """
        with contextlib.suppress(Exception):
            logmod.event(name, **fields)

    def drain_events(self, limit=200):
        """Called from the GUI thread. Returns a list of event dicts."""
        result = []

        for _ in range(limit):
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                break

        return result

    # --------------------------------------------------------
    # Loop thread
    # --------------------------------------------------------

    def start_loop(self):
        if self._thread is not None and self._thread.is_alive():
            return

        self._ready.clear()

        self._thread = threading.Thread(
            target=self._run_loop,
            name="mclink-asyncio",
            daemon=True,
        )
        self._thread.start()

        self._ready.wait(timeout=5)

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop

        asyncio.set_event_loop(loop)

        self._ready.set()

        try:
            loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )

            with contextlib.suppress(Exception):
                loop.close()

    def shutdown(self, timeout=8):
        """Stop everything and join the loop thread."""
        with contextlib.suppress(Exception):
            self.stop_presence(timeout=timeout)

        with contextlib.suppress(Exception):
            self.close_room(timeout=timeout)

        with contextlib.suppress(Exception):
            self.stop_session(timeout=timeout)

        loop = self._loop

        if loop is not None:
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)

        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # --------------------------------------------------------
    # Bridge
    # --------------------------------------------------------

    def submit(self, coro, timeout=20):
        """
        Schedule a coroutine on the engine loop and wait for the result.

        Runs in the GUI thread. Returns the coroutine's return value.
        Raises RuntimeError on timeout, or re-raises the coroutine exception.
        """
        loop = self._loop

        if loop is None:
            raise RuntimeError("网络线程尚未启动")

        future = asyncio.run_coroutine_threadsafe(coro, loop)

        try:
            return future.result(timeout=timeout)
        except TimeoutError as e:  # pragma: no cover - py3.11 alias
            # 超时 ≠ 那条协程停了。不等了就必须把它取消掉，否则它会继续在
            # 事件循环里跑（启动到一半的会话就是这样留下来的：界面说失败，
            # 中继那边还占着房间和隧道，而且没人再持有它的引用了）。
            with contextlib.suppress(Exception):
                future.cancel()

            raise RuntimeError("操作超时") from e
        except Exception:
            # The exception already came from the coroutine; propagate it.
            raise

    # --------------------------------------------------------
    # Presence ("I am open and logged in")
    # --------------------------------------------------------

    def start_presence(self, host, port, auth, client_id="", on_line=None):
        """
        Tell the relay we are online, and keep telling it (PING every 10 s).

        This is what makes the panel's 在线账号 honest: it is a real
        connection, so closing the client -- cleanly or by force -- ends it,
        instead of leaving a valid token behind for hours.

        Non-fatal by design: an older relay does not know the command, and
        nothing about the actual game connection depends on this.
        """
        import threading  # noqa: F401  (documents that this is cross-thread)

        from . import presence as presence_mod

        self.start_loop()

        if auth is None:
            return None

        self.stop_presence()

        keeper = presence_mod.PresenceKeeper(host, port, auth, client_id)
        keeper.on_line = on_line

        async def _spawn():
            task = asyncio.create_task(keeper.run())
            self._presence_task = task
            self._presence_keeper = keeper
            return task

        try:
            return self.submit(_spawn(), timeout=10)
        except Exception:
            return None

    def presence_rtt_ms(self):
        """
        本机 ↔ 中继 的往返时间（毫秒），由 presence 通道的 PING/PONG 量出来。

        玩家端没有别的控制通道可用（CONNECTED 之后那条 socket 只跑原始
        Minecraft 字节），所以看板上的"中转延迟"对玩家就取这里。
        """
        keeper = getattr(self, "_presence_keeper", None)

        if keeper is None:
            return None

        return getattr(keeper, "last_rtt_ms", None)

    def stop_presence(self, timeout=5):
        """Close the presence channel (logout, or exiting the app)."""
        keeper = getattr(self, "_presence_keeper", None)
        task = getattr(self, "_presence_task", None)

        if keeper is not None:
            keeper.stop()

        loop = self._loop

        if task is not None and loop is not None:
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(task.cancel)

        self._presence_task = None
        self._presence_keeper = None

    # --------------------------------------------------------
    # Session control
    # --------------------------------------------------------

    def current_session(self):
        with self._session_lock:
            return self._session

    def session_mode(self):
        """
        正在跑的这个会话用的是哪种连接模式（"relay" / "stun"），没有会话就是 ""。

        为什么需要它：**模式是会话创建时冻进 core 的**。房间的热切换（房主把
        relay 换成 STUN）只改了界面和 config，正在跑的 core 还在按旧模式搬数据
        —— 用户实测到的就是"打通了直连、界面也写着 STUN，但游戏死活连不上"
        （客户端日志里 `[RELAY] connecting to relay ...` 一条接一条，而房间模式
        显示 STUN 直连）。所以界面必须能比较"房间的模式"和"会话真正在用的模式"，
        不一致就重建。
        """
        session = self.current_session()

        if session is None:
            return ""

        return str(getattr(getattr(session, "config", None), "mode", "") or "")

    def start_session(self, config, auth=None, timeout=20):
        """
        Start a host or player session. Blocks the GUI thread only for the
        short setup phase (socket creation), never for the whole session.

        `auth` is the AuthSession from login(); it is required when the relay
        has accounts enabled.

        Returns the started session. Raises RuntimeError(message) on failure.
        """
        self.start_loop()

        # Ensure any previous session is gone first.
        self.stop_session(timeout=15)

        # ★ v3.9.16：开新会话时，上一轮"host 没了"的事实作废。
        # 它是**那一轮**的结论，不该跨越新会话继续生效。
        self._host_gone = None

        if config.role == "host":
            session = HostSession(config, self._push, auth=auth)
        else:
            session = PlayerSession(config, self._push, auth=auth)

        # V3 direct path: the candidate exchange rides the room membership
        # connection, which the ENGINE owns (not the session). Injecting a
        # provider rather than the object keeps the session from holding a
        # reference that goes stale when the user leaves and rejoins a room.
        session.room_provider = self.room_current
        # The room-level punch lives on the engine (see room_direct.py); the
        # session needs it to hand the cores a usable Path.
        session.engine_provider = self

        with self._session_lock:
            self._session = session

        self._started_at = time.time()

        try:
            self.submit(session.start(), timeout=timeout)
        except Exception as e:
            # ============================================================================
            #  启动超时 = 必须真的把它停下来，不能只把引用丢掉
            # ============================================================================
            #
            # submit() 超时只是"我们不等了"：那条协程还在事件循环里跑（控制连接、
            # 隧道、速率采样、快照推送全都起来了）。以前这里直接把 _session 置空，
            # 于是 stop_session() 一看是 None 就立刻返回 —— 界面上写着「启动失败」，
            # 中继那边却还占着房间和隧道，谁也停不掉（幽灵会话）。
            #
            # 所以先真的停一次，再清引用。
            with contextlib.suppress(Exception):
                self.submit(session.stop(), timeout=15)

            with self._session_lock:
                if self._session is session:
                    self._session = None

            self._push("log", text=f"启动失败：{e}")

            raise RuntimeError(str(e)) from e

        return session

    def stop_session(self, timeout=15, graceful=False):
        """
        Stop the Minecraft session.

        Returns True when the session was torn down cleanly. On a timeout it
        does NOT just swallow the failure (that was the bug behind "断开之后
        端口被占用、再也连不上"): it falls back to `session.abort()`, which
        closes every socket without waiting, and reports False.

        ============================================================================
          ★ v3.9.17：`graceful=True` 才表示"**用户点的停止**"
        ============================================================================

        默认 False 是刻意的：绝大多数调用方（模式热切换的重建、掉线清理、
        自动重连前的收尾、启动失败的收尾）都不是"用户不玩了"，而是"我要重新
        连"。v3.9.16 让它们一律发 `STOPPING`，结果中继那个"主动停止"标记永不
        复位，2.5 秒宽限窗口从此永久失效 —— 模式热切换照旧把全房间踢下线
        （复核报告 P1）。
        
        Callers that need the port back before starting again must check the
        return value (or just call this again — it is idempotent).

        ============================================================================
         停止【不再】释放主机位置（用户明确要求，2026-09 修改）
        ============================================================================

        This used to release the host slot, reasoning that "a client that has
        stopped is not hosting anything". The user's report showed why that is
        wrong: after pressing 停止 the room list said 「无人认领」 while the
        person had not released anything — and the two ways to stop hosting
        (停止 / 释放主机身份) became indistinguishable.

        现在的规则：
          * 停止连接 = 只是不开服了。主机位置仍然是你的，房间列表里仍然显示
            你的名字（标注"未开服"）。
          * 要交出位置 = 点「释放主机身份」（连接运行中该按钮会被拦截，
            必须先停止）。

        The ROOM itself is untouched either way: stopping is not closing.
        """
        with self._session_lock:
            session = self._session
            self._session = None

        if session is None:
            return True

        try:
            self.submit(session.stop(graceful=graceful), timeout=timeout)
            return True
        except Exception as e:
            # ================================================================
            #  超时/异常**不能吞**：旧会话的监听 socket 还开着，而我们已经
            #  把引用清空了 —— 下一次连接绑同一个本地端口就会失败，用户看到的
            #  就是"断开之后再也连不上、端口被占用"。
            #
            #  所以这里改用 abort()：取消一切、关掉所有 socket，不等。
            # ================================================================
            self.log_event("session.stop_failed", error=str(e))

            with contextlib.suppress(Exception):
                self.submit(session.abort(), timeout=5)

            return False

    # --------------------------------------------------------
    # v3.9.16：host 没了 —— 核心层自己拆
    # --------------------------------------------------------

    def host_gone(self):
        """
        最后一次"host 没了"的事实（`{"code","reason","at"}`），None = 没发生过。

        界面层用它做**幂等**：中继会重复广播（停止与释放前后脚到），而核心层
        的拆除已经做过了，界面不该再停一遍、更不该弹两次窗。
        """
        return self._host_gone

    def _on_host_gone_from_room(self, code, reason=""):
        """
        `RoomSession` 在控制连接上读到 HOST_LEFT / HOST_STOPPED /
        HOST_RELEASED / NO_HOST 时调这里（**在引擎的事件循环线程里**）。

        ============================================================================
          这是 v3.9.16 对"host 断开后房间内其余人不断开"的根治点
        ============================================================================
        以前核心层只 `push` 一个事件给界面，真正的 `stop_session()` 在 GUI
        那一层，而 GUI 有三道窗口会把 `stopped`/`released` 吞成一行日志
        （45 秒静默窗口 / 重建中 / 待重建）。结果：中继会话已拆、socket 已关，
        客户端这边 `snapshot()` 还是 `state=ONLINE`，玩家一直"已连接"。

        现在核心层自己动手。三条硬约束：

          * **绝不阻塞**：这里跑在控制连接的读循环里，任何 await 都会让那条
            连接哑掉。真正的停止排成协程，只 `run_coroutine_threadsafe`
            （不等结果）。
          * **绝不靠界面**：就算界面这次事件丢了 / 被推迟了，会话也已经停了。
          * **幂等**：中继重复广播时第二次不再拆（界面层读 `host_gone()`
            做同样的判断）。

        host 自己**不会**走到这里：`RoomSession` 在 `state.is_host` 为真时
        跳过拆除（那是他自己停服的回显，不是"host 没了"）。
        """
        now = time.time()
        previous = self._host_gone

        # 同一个码在短时间内重复到达（中继广播 + 重传）：只更新原因，不重复拆。
        if previous is not None and previous.get("code") == code and (
                now - float(previous.get("at", 0.0) or 0.0)) < 2.0:
            self._host_gone = {
                "code": code, "reason": reason or code, "at": now,
                "count": int(previous.get("count", 1)) + 1,
            }
            return

        self._host_gone = {
            "code": code, "reason": reason or code, "at": now, "count": 1,
        }

        self._push("host_gone", code=code, reason=reason or code, mandatory=True)

        self.log_event("host.gone", code=code, reason=reason or code)

        # ---- 1. 直连通路作废 ----
        #
        # P2P（STUN 直连）模式下，玩家的游戏字节根本不走中继，中继唯一能做
        # 的只是在成员控制连接上发那一行。所以**这条链路是直连模式下唯一的
        # 拆除依据** —— 不在这里清，直连的玩家就永远不知道 host 没了。
        #
        # 直连模式下这是**唯一的**判死依据（不走中继就没有物理上的"连接断了"，
        # 保活本身还会制造数据、让 `LINK_DEAD_S` 判不死），所以必须真的把洞
        # 全拆掉。`_room_direct` 本身留着（host 回来还要用它，重建一次要几秒）。
        with contextlib.suppress(Exception):
            self.release_room_direct_path()

        direct = getattr(self, "_room_direct", None)

        if direct is not None:
            with contextlib.suppress(Exception):
                direct.on_host_gone(code, reason or code)

        # ---- 2. 排一个协程去停会话（不阻塞调用方）----
        loop = self._loop

        if loop is None:
            return

        try:
            asyncio.run_coroutine_threadsafe(
                self._async_host_gone_teardown(code, reason or code), loop
            )
        except Exception as e:  # noqa: BLE001
            self.log_event("host.gone.schedule_failed", error=str(e))

    async def _async_host_gone_teardown(self, code, reason=""):
        """
        在引擎的事件循环里停掉 Minecraft 会话（host 没了）。

        `stop_session()` 是给 GUI 线程用的**阻塞**版本；这里是同一件事的
        async 版本：不拿锁等 GUI，也不让 GUI 的 `stop_session()` 有机会因为
        "看起来已经停了"而跳过。两者都跑一遍是安全的 —— session 引用在下面
        被置空之后，第二次调用拿到 None 直接返回。
        """
        with self._session_lock:
            session = self._session

            # 摘掉引用（和 stop_session 同一把锁），GUI 那侧随后就看不到它了。
            if self._session is session:
                self._session = None

        if session is None:
            return

        try:
            await asyncio.wait_for(session.stop(), timeout=10)
            self.log_event("host.gone.session_stopped", code=code)
        except Exception as e:  # noqa: BLE001
            self.log_event(
                "host.gone.session_stop_failed", code=code, error=str(e)
            )

            # 超时也必须把 socket 关掉：留着监听端口，下一次连接就会
            # "端口被占用、再也连不上"。
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.abort(), timeout=5)

    def snapshot(self):
        """
        Cheap status copy for the GUI. Called from the GUI thread.
        Never awaits, never locks the core.

        ============================================================================
          审计问题 6：累计流量是**进程级**的，没有会话时也必须给
        ============================================================================
        以前没会话时只回 `{role: None, state: IDLE}` —— 累计数字和实时速度
        整块消失，界面只能写 `—`。现在无论有没有会话，都把流量账本合进来：
        累计值（中转 / 对端分开）活满整个进程，速度没流量时是 0（而不是"没有"）。
        """
        snapshot = self.traffic_snapshot()
        from .connection_status import connection_rows
        streams = list(self.auto_manager.states.values())
        coordinator = self._room_direct
        snapshot["auto_streams"] = streams
        snapshot["connection_rows"] = connection_rows(
            self.room_state(), streams,
            coordinator.link_rows() if coordinator else (),
            getattr(coordinator, "_carriers", {}),
        )
        session = self.current_session()

        if session is None:
            snapshot.update({
                "role": None,
                "state": "IDLE",
                "state_text": "未启动",
            })
            return snapshot

        try:
            snapshot.update(session.snapshot())
            return snapshot
        except Exception:
            snapshot.update(
                {"role": session.role, "state": "ERROR", "state_text": "错误"}
            )
            return snapshot

    # --------------------------------------------------------
    # Traffic ledger (process level -- 审计问题 6)
    # --------------------------------------------------------

    def traffic_ledger(self):
        """The process-wide traffic ledger (never reset by leaving a room)."""
        from . import traffic

        return traffic.ledger()

    def record_traffic(self, mode, up=0, down=0):
        """Add bytes to the process ledger, tagged by the transport in use."""
        self.traffic_ledger().record(mode, up=up, down=down)

    def traffic_snapshot(self):
        """
        Sample the ledger and return it in the shape the status card reads.

        Sampling HERE (once per `snapshot()` call, i.e. about once per second
        from the GUI) is also what keeps the speeds live while idle: with no
        traffic the cumulative totals do not move and the rate decays to 0.
        """
        ledger = self.traffic_ledger()
        ledger.sample()
        return ledger.snapshot()

    # --------------------------------------------------------
    # Relay reachability test (used by the GUI "测试服务器" button)
    # --------------------------------------------------------

    def test_relay(self, host, port, timeout=6):
        """
        Open a short lived connection to the relay and verify the protocol.

        The relay replies "OK HOST room=..." for a HOST handshake, so this
        proves the endpoint, the port and the protocol version at once.
        """
        self.start_loop()

        coro = _probe_relay(host, int(port), timeout)

        return self.submit(coro, timeout=timeout + 5)

    # --------------------------------------------------------
    # V2 control plane (login / rooms / delete)
    # --------------------------------------------------------

    def login(self, host, port, username, password, client_id="", timeout=20):
        """
        Log in against the relay. Called from the GUI thread.

        Returns an AuthSession. Raises RuntimeError(message) with a friendly
        Chinese message on failure.
        """
        from . import control_client

        self.start_loop()

        async def _do():
            return await control_client.login(
                host, int(port), username, password, client_id
            )

        try:
            session = self.submit(_do(), timeout=timeout)
        except control_client.ControlError as e:
            raise RuntimeError(str(e)) from e
        except Exception as e:
            raise RuntimeError(f"登录失败：{e}") from e

        return session

    def relay_requires_account(self, host, port, client_id="", timeout=8):
        """
        Ask the relay whether an account is needed. Called from the GUI thread.

        Returns True / False, or None when the relay cannot be reached or is
        too old to answer. None must be treated as "assume yes": an old relay
        genuinely does require a login, and prompting unnecessarily is a much
        smaller problem than letting the user connect and fail.

        This exists because the client used to answer the question from a
        hardcoded local assumption and prompt for a login before it ever
        contacted the relay, which made the panel's "must log in" switch look
        broken no matter what the server did.
        """
        from . import control_client

        self.start_loop()

        async def _do():
            return await control_client.relay_requires_account(
                host, int(port), client_id
            )

        try:
            return self.submit(_do(), timeout=timeout)
        except Exception:
            return None

    def fetch_rooms(self, host, port, client_id="", username="", timeout=20):
        """Return the live room list. Called from the GUI thread."""
        from . import control_client

        self.start_loop()

        async def _do():
            return await control_client.fetch_rooms(
                host, int(port), client_id, username
            )

        try:
            return self.submit(_do(), timeout=timeout)
        except control_client.ControlError as e:
            raise RuntimeError(str(e)) from e
        except Exception as e:
            raise RuntimeError(f"获取房间列表失败：{e}") from e

    def delete_room(self, host, port, room, auth, client_id="", timeout=20):
        """Delete one of my own rooms (used when no session is running)."""
        from . import control_client

        self.start_loop()

        async def _do():
            return await control_client.delete_room(
                host, int(port), room, auth, client_id
            )

        try:
            return self.submit(_do(), timeout=timeout)
        except control_client.ControlError as e:
            raise RuntimeError(str(e)) from e
        except Exception as e:
            raise RuntimeError(f"删除房间失败：{e}") from e

    def rename_account(self, host, port, new_username, auth, client_id="",
                       timeout=20):
        """
        Rename the logged-in account (self-service). Called from the GUI thread.

        Also patches every live object that carries the old name -- the running
        session's auth block and its core handshake name -- so the rename takes
        effect immediately WITHOUT stopping the session or logging out.

        Returns (new_username, message). Raises RuntimeError(message).
        """
        from . import control_client

        self.start_loop()

        async def _do():
            return await control_client.rename_account(
                host, int(port), new_username, auth, client_id
            )

        try:
            confirmed, message = self.submit(_do(), timeout=timeout)
        except control_client.ControlError as e:
            raise RuntimeError(str(e)) from e
        except Exception as e:
            raise RuntimeError(f"改名失败：{e}") from e

        if auth is not None:
            auth.username = confirmed

        session = self.current_session()

        if session is not None:
            # The GUI and engine each hold a reference to the SAME AuthSession
            # object, so updating auth above is already enough for the panel --
            # but the core keeps its own copy of the name for the handshake.
            if session.auth is not None:
                session.auth.username = confirmed

            core = session.core

            if core is not None:
                core.username = confirmed

            with contextlib.suppress(Exception):
                self._push("snapshot", data=session.snapshot())

        self._push("account_renamed", username=confirmed)

        return confirmed, message

    def logout_session(self, host, port, auth, client_id="", timeout=8):
        """
        Tell the relay to release this token. Called on the way out.

        Best effort by design: this runs during application shutdown, so a
        failure must not delay or block the exit. The token expires on its own
        regardless; this just makes the panel's "online" count honest
        immediately instead of up to 12 hours later.
        """
        if auth is None or not getattr(auth, "token", ""):
            return False

        from . import control_client

        async def _do():
            return await control_client.logout(
                host, int(port), auth, client_id
            )

        try:
            return bool(self.submit(_do(), timeout=timeout))
        except Exception:
            return False

    # --------------------------------------------------------
    # V3 room membership (owner / host slot)
    # --------------------------------------------------------

    def room_current(self):
        """The live RoomSession, or None."""
        return self._room

    def loop(self):
        """
        The engine's event loop, or None before `start_loop()`.

        Handed out so room-level work that is NOT a session (the live NAT
        measurement, which starts the moment you join a STUN room) can run on
        the same loop as everything else instead of spawning a second one.
        """
        return self._loop

    def server_host(self):
        """The relay host we are talking to, for anything that needs it."""
        return self._relay_host

    def server_port(self):
        """The relay port that goes with `server_host()`."""
        return int(getattr(self, "_relay_port", 0) or 0)

    # --------------------------------------------------------
    # Live NAT measurement for the room we are standing in
    # --------------------------------------------------------

    def watch_room_nat(self, enabled, log=None, standalone_host=None):
        """
        Turn the room-level NAT measurement on or off. Called from the GUI
        thread every time the room's state could have changed.

        WHY THE ROOM AND NOT THE SESSION: in STUN mode the measurement has to
        start when you JOIN the room -- the user is looking at the status card
        before pressing "start connection", and a verdict that only appears
        after connecting is the "frozen number" behaviour the project owner
        explicitly asked to get rid of. It runs until the room ends or the room
        leaves STUN mode.

        `enabled` comes from the ROOM's mode (which the relay dictates), not
        from the local config: joining a STUN room must show STUN measurements
        even while the selector is still showing relay.

        ============================================================================
          ★ 2026-09-28（设计基准 §E —— **推翻 09-27 的"打开软件就测"**）
        ============================================================================

        基线原文（`docs/DESIGN-BASELINE-20260927.md` §E）：

            "**测量是在点「开始连接」之后按这个顺序做的**（不是打开软件就测）。"

        所以 2026-09-27 那次"打开软件就把 NAT 测起来"（ISSUES 问题 2）被
        **撤掉**了 —— 它正是用户本轮 BUG 4 里的那一半：「玩家未点「开始连接」，
        候选交换的计时/计数就已经开始」。用户日志里那一串大约每 90 秒一次的
        `[NAT] two different STUN IPs gave ports [...]` 就是它在空转。

        `standalone_host` 参数**保留**（老调用方还在传），但它现在只被**记下来**，
        不再单独触发测量：测量唯一的上游开关是 `enabled`
        （= 房间里是 STUN 模式 **且** 会话在跑，也就是用户真的点了「开始连接」）。

        NAT 画像仍然会被复用：第一条连接测完就存进缓存，后面的连接直接读
        （§F 的"每连接独立"说的是**状态**独立，不是"每条连接都必须重测一遍"）。
        """
        from .session import RoomNatMonitor

        if self._nat_monitor is None:
            self._nat_monitor = RoomNatMonitor(self, log=log)

        if standalone_host:
            # 只记地址：冷启动时 `server_host()` 可能还是空的，测量真开始的
            # 那一刻需要有个目的地。**它不再是启动测量的理由。**
            self._nat_monitor.standalone_host = str(standalone_host)

        if enabled:
            # 点「开始连接」：按房间的开关测。
            self._nat_monitor.start(standalone_host=standalone_host)
        else:
            self._nat_monitor.stop()

        # Punching is the OTHER half of "STUN mode starts when you join the
        # room": measuring the NAT is useless if nobody acts on it. See
        # room_direct.py for why this cannot live in the session.
        self.watch_room_direct(enabled, log=log)

    def sync_nat_standalone(self, host=None, log=None, keep_running=False):
        """
        【打开软件就测】的入口：没房间也要把 NAT 测起来。

        `keep_running=True`（界面 tick 走这条）表示"没房间时也让监测继续跑"；
        False 表示走原来的 room 语义（离开房间 / 离开 STUN 就停）。两者共用
        同一个 `RoomNatMonitor`，所以**永远只有一条测量循环** —— 不会出现
        "房间一份、会话一份"两个 socket 各测各的（审计问题 7 里两条相隔 20ms
        的重复测量）。

        Returns True when the monitor is (or was already) running.
        """
        from .session import RoomNatMonitor

        if self._nat_monitor is None:
            self._nat_monitor = RoomNatMonitor(self, log=log)

        if host:
            self._nat_monitor.standalone_host = str(host)

        if keep_running or self._nat_monitor.running:
            self._nat_monitor.start(standalone_host=host)
        else:
            self._nat_monitor.stop()

        return self._nat_monitor.running

    def ensure_nat_profile(self, max_age=30.0, timeout=12.0, log=None):
        """
        打洞之前用一次：画像不够新 / 还没测出来时，**在本机事件循环上**补测一次。

        为什么不是"再开一个监控"：这一段和 `RoomNatMonitor` 共用
        `measure_nat()`（唯一一份测量序列）与同一个缓存口 `room_nat_profile()`。
        有监控在跑（打开软件就测之后它基本总在跑）并且画像还新鲜时这里什么都不做
        —— 用户的规则是"当有房客加入并连接时，采用当前测量状态决定打洞方案，
        而不是重测"。

        返回 (profile, age_seconds_or_None)。任何失败都返回现有画像，绝不抛。
        """
        from .session import measure_nat, nat_profile_age

        monitor = self._nat_monitor
        profile = monitor.profile if monitor is not None else None
        age = nat_profile_age(monitor)

        if profile is not None and profile.public is not None and age <= max_age:
            return profile, age

        host = self.server_host() or (
            monitor.standalone_host if monitor is not None else ""
        )

        if not host:
            return profile, age

        self.start_loop()

        if self._loop is None:
            return profile, age

        try:
            fresh = asyncio.run_coroutine_threadsafe(
                measure_nat(host, log=log), self._loop
            ).result(timeout=timeout)
        except Exception as e:
            if log:
                with contextlib.suppress(Exception):
                    log(f"[nat] pre-punch measurement failed: {e!r}")
            return profile, age

        if monitor is not None and fresh is not None and fresh.public is not None:
            monitor._publish(fresh)

        return (monitor.profile if monitor is not None else fresh), nat_profile_age(
            monitor
        )

    def room_nat_profile(self):
        """The room monitor's latest profile, or None."""
        if self._nat_monitor is None:
            return None

        return self._nat_monitor.profile

    def room_nat_age(self):
        """那份画像测出来多久了（秒）；没有则 None。界面用它说"多久前测的"。"""
        from .session import nat_profile_age

        return nat_profile_age(self._nat_monitor)

    def room_nat_running(self):
        """Whether the room's NAT monitor is currently measuring."""
        return self._nat_monitor is not None and self._nat_monitor.running

    def watch_room_direct(self, enabled, log=None):
        """
        Start/stop the room-level punch. Called from the GUI thread.

        ============================================================================
          THIS IS THE FLOW THE OWNER ASKED FOR
        ============================================================================

        His words: "他那个是进房间就打洞，然后打通了再挂游戏" -- punch when the
        room forms, then hang the game on the result. Our first version punched
        only after a relay tunnel reached READY, i.e. after the game had already
        connected, which is why his logs contained ZERO `[direct]` lines: with
        no game running there was nothing to connect, so the punch never ran.

        Same switch as the NAT monitor, and for the same reason: it belongs to
        the ROOM, not to a connection.
        """
        from .room_direct import RoomDirectLink

        coordinator_type = RoomDirectLink
        if getattr(self.room_state(), "conn_mode", "") == "auto":
            from .auto_room_direct import AutoRoomDirectLink
            coordinator_type = AutoRoomDirectLink
        if self._room_direct is None or type(self._room_direct) is not coordinator_type:
            if self._room_direct is not None:
                self._room_direct.close()
            self._room_direct = coordinator_type(self, log=log)

        if enabled:
            self._room_direct.start()

            # 打洞要开始用 NAT 画像了。画像太旧（超过打洞的保鲜期
            # NAT_HINT_MAX_AGE_S）时让它**立刻**重测一轮，而不是等 90 秒那一拍
            # —— 用户的要求是"用当前测量状态决定打洞方案，不要重测"，而"当前"
            # 的前提是这份状态还新鲜。重测跑在监测自己的 socket 上，不会多开
            # 一个 socket，也不改打洞的走向。
            with contextlib.suppress(Exception):
                from .session import nat_profile_age

                monitor = self._nat_monitor
                now = time.time()

                # 防抖：这个函数每秒都会被调一次，绝不能每秒都去戳一轮测量。
                # 20 秒 = 一次测量(4~6s) + 余量，够它测完并刷新 `profile_at`。
                fresh_enough = (
                    now - getattr(self, "_last_nat_poke_at", 0.0)
                    >= self.NAT_POKE_COOLDOWN_S
                )

                if monitor is not None and monitor.running and fresh_enough:
                    age = nat_profile_age(monitor)

                    if age is None or age > self.NAT_POKE_MAX_AGE_S:
                        self._last_nat_poke_at = now
                        monitor.poke()
        else:
            # Leaving STUN mode or the room: the mapping is no longer useful to
            # anyone, so release the socket too.
            self._room_direct.close()

    def room_direct(self):
        """The room punch coordinator (or None)."""
        return self._room_direct

    def rebuild_room_direct_links(self, peers=None):
        """
        强制重打通路（用户 2026-09-30 第 1 条）。Called from the GUI thread.

        `peers=None` = 全部（host 重连）；`peers=[host]` = 只这一条（player 重连）。
        没有房间/没有打洞协调器时什么都不做（返回 []）。
        """
        direct = self._room_direct

        if direct is None:
            return []

        return direct.rebuild_links(peers)

    def room_direct_state(self):
        """The room punch's live state for the status card (see its `state()`)."""
        if self._room_direct is None:
            return {}

        return self._room_direct.state()

    def room_rtt_ms(self):
        """
        本机 ↔ 中继的往返时间（毫秒），来自成员连接的 PING/PONG 回声。

        None = 还没量到（刚进房、或者这条路不是我们的中继）—— 界面显示 `-`，
        绝不拿别的数字冒充。
        """
        session = self.room_current()

        if session is None:
            return None

        return getattr(session, "relay_rtt_ms", None)

    def room_direct_ready(self):
        """A punched path is available AND still fresh."""
        return self._room_direct is not None and self._room_direct.ready

    async def wait_room_direct_path(self, timeout=None):
        """
        Wait for the room's punched link, then hand it over.

        ============================================================================
          WHY WAITING IS THE CORRECT ANSWER IN STUN MODE
        ============================================================================

        The owner's rule, verbatim: "已打的通道和主流量不允许碰中继".

        So in STUN mode the game's traffic does NOT fall back to the relay when
        the punch is not ready yet -- it waits for the punch. That is only
        sound because the room punch starts at JOIN time and is kept warm
        continuously (see room_direct.py), so "not ready yet" means seconds,
        not "never": the two ends are in the same room and the keepalive is
        already holding the mapping open.

        The relay tunnel is still established and still up. It is the control
        plane and the fallback of last resort for RELAY mode; it simply does
        not carry the main traffic here.

        Returns a `direct_carry.PeerLink`, or None when the room punch has
        stopped trying (leaving the room, switching back to relay) -- the caller
        then relays, because there is nothing left to wait for.
        """
        waited = 0.0
        step = 0.2
        next_report = 5.0

        while True:
            path = self.room_direct_path()

            if path is not None:
                return path

            direct = self._room_direct

            if direct is None or not direct.transport_ready:
                # No punched link AND nothing punching any more. Waiting would
                # hang forever; the caller relays so the session stays usable.
                return None

            if timeout is not None and waited >= timeout:
                return None

            await asyncio.sleep(step)
            waited += step

            # Log the WAIT, not every poll.
            #
            # This used to emit an event every 200ms, which turned the log into
            # an unreadable wall ("这次后面加了一长串东西，很烦人" -- the
            # owner's words). One line every 5s says the same thing.
            if waited >= next_report:
                next_report = waited + 5.0
                self.log_event("direct.wait_for_link", waited=round(waited, 1))

    def release_room_direct_path(self):
        """
        Kept for the session layer's `direct_path_releaser` hook.

        It used to return the punched link so the next Minecraft connection
        could claim it; links are now shared (one tunnel, one `sid` per
        connection), so there is nothing to return. See
        `RoomDirectLink.release_all_paths`.
        """
        if self._room_direct is not None:
            self._room_direct.release_all_paths()

    def room_direct_path(self):
        """
        The room's live direct link (a `direct_carry.PeerLink`), or None.

        SHARED, not handed out once: every Minecraft connection to the same peer
        rides the same tunnel on its own `sid`. See `RoomDirectLink.take_path`
        for why a per-connection tunnel cannot work here (the peer's UdpHub
        would see two tunnels under one IP and drop everything as ambiguous).
        """
        if self._room_direct is None:
            return None

        return self._room_direct.take_path()

    def set_room_direct_backend(self, host, port):
        """
        The host's local Minecraft address, applied to EVERY direct link.

        A host has one link per player, and a session is paired by the relay
        without saying WHICH player it belongs to -- so the only correct
        answer is to configure every link (see RoomDirectLink.set_backend).
        """
        if self._room_direct is not None:
            self._room_direct.set_backend(host, port)

    def set_room_capacity(self, value, timeout=10):
        """
        Ask the relay to size the room we are in. Called from the GUI thread.

        Used when the player changes 「房间最大人数」 while already standing in
        their own room: without this, the room would keep advertising the
        number it was created with.
        """
        session = self.room_current()

        if session is None:
            return False, "还没有进入房间"

        try:
            return self.submit(session.set_capacity(value), timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def auto_rejoin(self, timeout=12):
        """
        Walk back into the room we were just in, after the membership
        connection dropped (network hiccup, wifi switch, relay restart).

        WHY: a dropped member connection used to leave the window in a
        half-state — 开始连接 reset to usable, but the room side still looked
        joined, and the only way out was pressing 退出房间 by hand. Re-joining
        is what actually makes the two agree again, and it also takes the host
        slot back if the relay has already forgotten us.

        The coordinates are kept from `open_room` rather than read off the dead
        session, precisely so this still works after the session has been torn
        down.

        Returns (ok, message). Never raises.
        """
        details = self._room_join_args

        if not details:
            return False, "没有可以重连的房间"

        was_host = bool(self._room_was_host)

        try:
            state = self.open_room(
                **details, timeout=timeout
            )
        except Exception as e:  # noqa: BLE001
            return False, str(e)

        if not was_host:
            return True, "已重新连上房间"

        # We were the host when the link dropped: take the slot back so other
        # players can keep playing. A failure here is NOT fatal — the room is
        # joined, somebody else may simply have taken the slot.
        ok, message = self.claim_host(timeout=timeout)

        if not ok:
            return True, f"已重新连上房间（主机身份未取回：{message}）"

        return True, "已重新连上房间并取回主机身份"

    def room_state(self):
        """A snapshot of the room we are in, or None."""
        session = self._room

        if session is None:
            return None

        try:
            return session.state
        except Exception:
            return None

    def open_room(self, host, port, client_id, username, token, room,
                  create=False, timeout=20, max_players=0, conn_mode=""):
        """
        Create a room (owner) or join one (member). Called from the GUI thread.

        The membership connection STAYS OPEN afterwards, which is the point:
        it is how the relay knows who is in a room and who holds the host slot,
        independently of whether anybody is connected to Minecraft yet.

        `max_players` is the player's own setting for THIS room. The server's
        value is only the ceiling -- sending it along is what makes the room's
        advertised size match what the player configured instead of silently
        becoming the server's number.
        """
        from . import room as room_mod

        self.start_loop()
        self.close_room(timeout=5)

        # Remember how we got here, so `auto_rejoin()` can do it again after a
        # dropped membership connection. Stored BEFORE connecting: a join that
        # fails halfway still leaves something to retry against.
        #
        # `create` is deliberately recorded as False: by the time this is used
        # the room is known to still exist (that is what makes a rejoin rather
        # than a "room is gone" teardown), and re-CREATEing a room that exists
        # would just be refused with ROOM_EXISTS.
        self._relay_host = host or self._relay_host
        self._relay_port = int(port or 0) or self._relay_port

        self._room_join_args = {
            "host": host,
            "port": port,
            "client_id": client_id,
            "username": username,
            "token": token,
            "room": room,
            "create": False,
            "max_players": 0,
            # The mode follows the ROOM (the relay re-broadcasts it on join),
            # but sending it again costs nothing and keeps a rejoin from
            # briefly looking like "mode unknown" on our own side.
            "conn_mode": conn_mode,
        }

        # ============================================================================
        #  房间事件适配器 —— 幽灵房的真正根因就在这里
        # ============================================================================
        #
        # RoomSession 发事件用的是 `emit(name, data)`（两个位置参数），而引擎的
        # 事件队列约定是 `_push(kind, **payload)`（见 SessionBase.event）。
        #
        # 直接把 self._push 传进去，两者对不上：`_push("room_revoked", {...})`
        # 抛 TypeError，而 RoomSession._notify 又把异常吞掉了。后果是房间相关的
        # 事件**全部静默丢失**：room_revoked / room_closed / host_state /
        # host_taken / room_gone / room_lost 一个都到不了界面。
        #
        # 表现出来就是用户报的那个"幽灵房"：中继明明把房间关了、房间状态对象里
        # revoked 也是 True，界面却一直停在"我在房间里"，按钮不恢复，必须手动
        # 点退出房间。
        #
        # 所以这里做转换，而不是把 _push 直接当回调传。
        def _room_emit(name, data=None):
            if name == "identity_updated" and self._room is session:
                game = self.current_session()
                core = getattr(game, "core", None)
                if core is not None and getattr(core, "auto_manager", None) is not None:
                    core.token = session.token or session.guest_token
                    core.username = session.state.self_name or session.username
            if name == "host_state" and self._room is session:
                state = (data or {}).get("state")
                if state is not None and state.peer_ids is not None:
                    allowed = set(state.peer_ids)
                    for stream in list(self.auto_manager.streams.values()):
                        if stream.peer_id not in allowed:
                            stream.reason = "peer_left_room"
                            stream.done.set()
            self._push("event", name=name, data=data or {})

        session = room_mod.RoomSession(
            host,
            port,
            client_id,
            username,
            token,
            emit=_room_emit,
            max_players=max_players,
            # 游客临时 token（登录会话级，ADDITIVE）。同一个登录会话里换房 /
            # 重新进房时把它放回握手行 -> 中继认得出"还是这个人"，名字不变
            # （用户 2026-09-27 口径）。登出 / 换账号 / 重启由 GUI 清空它。
            guest_token=str(getattr(self, "_guest_token", "") or ""),
            # 「你还是当前那个房间吗？」—— room.py 在发出"房间没了"这类
            # 迟到的结论之前会先问这一句。少了它，用户退出后 6 秒才到的那条
            # room_gone 会把刚建好的新房间一起拆掉（同名也一样，所以只能按
            # 对象身份判断，不能比房间名）。
            is_current=lambda: self.room_current() is session,
            # ★ v3.9.16：host 没了由**核心层**拆，不再只通知界面。
            # 界面那层有三道窗口会吞掉这类事件（见 `MCLinkEngine.
            # _on_host_gone_from_room` 的注释），它吞不吞从此不再决定断不断。
            host_gone_hook=self._on_host_gone_from_room,
        )

        async def _do():
            if create:
                # The creator's chosen transport seeds the room. Sending it
                # here (not after) is what stops the first HOST_STATE from
                # snapping the host straight back to relay.
                return await session.create(room, conn_mode=conn_mode)

            return await session.join(room)

        self.log_event(
            "room.enter",
            action="create" if create else "join",
            host=host,
            port=port,
            client_id=client_id,
            user=username,
            token=bool(token),
            room=room,
            max_players=int(max_players or 0),
            conn_mode=conn_mode,
        )

        try:
            state = self.submit(_do(), timeout=timeout)
        except room_mod.RoomError as e:
            # The session never got going, so do not leave a half-open socket.
            with contextlib.suppress(Exception):
                self.submit(session.stop(), timeout=5)

            raise RuntimeError(str(e)) from e
        except Exception as e:
            with contextlib.suppress(Exception):
                self.submit(session.stop(), timeout=5)

            raise RuntimeError(f"进入房间失败：{e}") from e

        with self._room_lock:
            self._room = session
            session.auto_manager = self.auto_manager

        # 中继这次给的游客临时 token（首次分配游客名时随 OK 回包下来）。
        # 存住它，下一次进房（同一登录会话）要放回握手行 —— 名字才不变。
        # **只活内存**：登出 / 换账号 / 重启由 GUI 清空（见 app.py）。
        guest_token = str(getattr(session, "guest_token", "") or "")

        if guest_token:
            self._guest_token = guest_token

        self.log_event(
            "room.entered",
            action="create" if create else "join",
            room=getattr(state, "room", ""),
            room_name=getattr(state, "room_name", ""),
            owner=bool(getattr(state, "is_owner", False)),
            host=bool(getattr(state, "is_host", False)),
            members=int(getattr(state, "members", 0) or 0),
            max_players=int(getattr(state, "max_players", 0) or 0),
            server_max_players=int(
                getattr(state, "server_max_players", 0) or 0
            ),
            conn_mode=getattr(state, "conn_mode", ""),
            mode_reason=getattr(state, "mode_reason", ""),
        )

        self._push("room_joined", state=state)

        return state

    def claim_host(self, timeout=15):
        """Try to take the host slot. Returns (ok, message)."""
        session = self.room_current()

        if session is None:
            return False, "还没有加入房间"

        try:
            ok, message = self.submit(session.claim_host(), timeout=timeout)
        except Exception as e:
            self.log_event("host.claim", ok=False, error=str(e))
            return False, f"操作失败：{e}"

        self.log_event(
            "host.claim",
            ok=bool(ok),
            room=getattr(session.state, "room", ""),
            message=message,
        )

        return ok, message

    def release_host(self, timeout=15):
        """Give the host slot up. Returns (ok, message)."""
        session = self.room_current()

        if session is None:
            return False, "还没有加入房间"

        try:
            ok, message = self.submit(session.release_host(), timeout=timeout)
        except Exception as e:
            self.log_event("host.release", ok=False, error=str(e))
            return False, f"操作失败：{e}"

        self.log_event(
            "host.release",
            ok=bool(ok),
            room=getattr(session.state, "room", ""),
            message=message,
        )

        return ok, message

    def kick_member(self, target_client_id, timeout=15):
        """
        把一个成员踢出当前房间。Called from the GUI thread.

        Returns (ok, message, code) -- exactly what `RoomSession.kick` gives:
        the RELAY is the authority on the permission matrix, so a client that
        shows the wrong buttons still cannot kick anybody it is not allowed to.
        """
        session = self.room_current()

        if session is None:
            return False, "还没有加入房间", "NOT_IN_ROOM"

        try:
            ok, message, code = self.submit(
                session.kick(target_client_id), timeout=timeout
            )
        except Exception as e:  # noqa: BLE001
            self.log_event("room.kick", ok=False, error=str(e))
            return False, f"操作失败：{e}", "FAILED"

        self.log_event(
            "room.kick",
            ok=bool(ok),
            room=getattr(session.state, "room", ""),
            target=target_client_id,
            code=code,
        )

        return ok, message, code

    def close_room(self, timeout=10):
        """
        Leave the room politely.

        LEAVE first so the relay frees the host slot immediately; a plain
        socket close would also tell it, but only after one watchdog tick.
        """
        with self._room_lock:
            session = self._room
            self._room = None

        # ★ v3.9.16：退房时清掉"host 没了"这个事实。
        #
        # 它是**这一轮房间**的事实：留着会让下一次进房时界面读到上一轮的
        # HOST_STOPPED，把新会话当成"该停的"直接放掉。
        self._host_gone = None

        if session is None:
            return False

        try:
            self.submit(session.leave(), timeout=timeout)
        except Exception:
            with contextlib.suppress(Exception):
                self.submit(session.stop(), timeout=timeout)

        return True

    def session_delete_room(self, room, timeout=20):
        """
        Ask the RUNNING session to delete its room (host control socket path).

        Returns (ok, message).
        """
        session = self.current_session()

        if session is None:
            return False, "当前没有正在运行的连接"

        try:
            return self.submit(
                session.delete_own_room(room), timeout=timeout
            )
        except Exception as e:
            return False, f"删除失败：{e}"

    def set_conn_mode(self, mode, timeout=10):
        """
        Ask the relay to change the room's connection mode (HOST ONLY).

        The relay enforces the permission, so this is safe to call from any
        client: a non-host gets ERROR NOT_HOST back.

        Returns (ok, message).
        """
        room = self.room_current()

        if room is None:
            return False, "还没有加入房间"

        try:
            ok, message = self.submit(room.set_conn_mode(mode), timeout=timeout)
        except Exception as e:
            self.log_event("room.conn_mode", ok=False, wanted=mode, error=str(e))
            return False, f"切换失败：{e}"

        # The RAW answer (ok/message exactly as the relay phrased it) goes in:
        # the relay is the authority on the room's mode, so its answer is the
        # fact, not our intention.
        self.log_event(
            "room.conn_mode",
            ok=bool(ok),
            wanted=mode,
            actual=getattr(getattr(room, "state", None), "conn_mode", ""),
            message=message,
        )

        return ok, message

    def room_set_started(self, connecting, timeout=8):
        """
        告诉中继"我点了开始连接 / 我停了"（`STARTED <1|0>`，PROTOCOL §8.6）。

        ============================================================================
          ★ 用户 2026-09-28 第 5/8 条：房间里有这个人 ≠ 他已经开始连接
        ============================================================================

        这是 Host 目标表（`started=`）的**用户意图**来源：STUN 模式是"先点开始
        打洞、游戏连接是之后的事"，只按连接现算的话 Host 拉不到这个玩家（见
        `punch_flow.beacon_line` 的 `from=` 说明）。

        返回值只用于日志 —— 老中继会忽略这一行（超时），那也不影响连接。

        返回 (ok, message)。
        """
        room = self.room_current()

        if room is None:
            return False, "还没有加入房间"

        try:
            ok, message = self.submit(
                room.send_started(bool(connecting)), timeout=timeout
            )
        except Exception as e:
            self.log_event(
                "room.started", ok=False, connecting=bool(connecting), error=str(e)
            )
            return False, f"上报开始连接失败：{e}"

        self.log_event(
            "room.started", ok=bool(ok), connecting=bool(connecting),
            message=str(message or ""),
        )

        return ok, message

    def room_send_identity(self, username, token="", timeout=8):
        """
        【房间成员连接】上把"我这个 client_id 现在的显示名"告诉中继。

        用户要求（docs/ISSUES-20260927.md 问题 5）：人在房间里登录 / 改名之后，
        名册里那颗名字要跟着变，房间**不重建**、正在玩的人不掉线。

        返回 (ok, message, name)。没有房间 / 没连上 -> (False, "还没有加入房间", "")。
        中继不认识这条行（老版本）时**超时**返回 False，界面照旧用原来的名字 ——
        绝不因为改名失败把连接弄断。
        """
        session = self.room_current()

        if session is None:
            return False, "还没有加入房间", ""

        try:
            ok, message, name = self.submit(
                session.send_identity(username, token), timeout=timeout
            )
        except Exception as e:
            self.log_event("room.identity", ok=False, error=str(e))
            return False, f"改名请求失败：{e}", ""

        self.log_event(
            "room.identity",
            ok=bool(ok),
            username=str(username or ""),
            resolved_name=str(name or ""),
            message=str(message or ""),
        )

        return ok, message, name

    # --------------------------------------------------------
    # NAT / STUN diagnostics (V3, READ ONLY)
    # --------------------------------------------------------

    def detect_nat(self, servers=None, relay_host="", relay_port=None,
                   timeout=None, on_progress=None, cancel=None):
        """
        Probe STUN servers and report what the outside world sees.

        ============================================================================
          This method is READ ONLY. It touches nothing that a connection uses.
        ============================================================================

        It does not join a room, does not open a session and does not share a
        socket with anything: it binds its own throwaway UDP sockets through
        `stun.detect_nat_type`, sends a handful of Binding Requests and closes
        them. Running it while a game is connected is safe, which is the whole
        point of shipping diagnostics first.

        It deliberately does NOT run on the engine's asyncio loop. Those
        probes are request/response exchanges with 3 second deadlines and
        several retransmissions, so a slow or black-holing network can hold
        the work for tens of seconds. Putting that on the loop thread would
        stall every connection the engine is carrying, which is exactly the
        class of bug `docs/V2-LEFTOVER-CONNECT-FREEZE.md` describes. A
        dedicated daemon thread gets its own loop instead.

        Runs in the GUI thread (or any thread); blocks until the probes
        finish. Returns a `stun.NatProfile`; never raises, because "no UDP at
        all" is a normal answer on a locked-down network and the UI has to be
        able to display it.
        """
        from . import stun as stun_mod

        started = time.time()

        if servers is None:
            servers = stun_mod.default_servers(
                relay_host,
                relay_port or stun_mod.DEFAULT_STUN_PORT,
            )
        else:
            servers = list(servers)

        result = {}

        def work():
            loop = asyncio.new_event_loop()

            try:
                asyncio.set_event_loop(loop)

                if on_progress is not None:
                    with contextlib.suppress(Exception):
                        on_progress("start", {"servers": list(servers)})

                async def run():
                    profile = await stun_mod.detect_nat_type(
                        servers,
                        timeout=timeout if timeout is not None
                        else stun_mod.RESPONSE_TIMEOUT,
                    )

                    if on_progress is not None:
                        with contextlib.suppress(Exception):
                            on_progress("done", {"profile": profile})

                    return profile

                result["profile"] = loop.run_until_complete(run())
            except Exception as e:  # pragma: no cover - defensive
                result["error"] = e
            finally:
                with contextlib.suppress(Exception):
                    pending = asyncio.all_tasks(loop)

                    for task in pending:
                        task.cancel()

                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )

                with contextlib.suppress(Exception):
                    loop.close()

        # No timeout on join(): the underlying probes already carry their own
        # deadline, and a thread that outlives us is a daemon that cannot hold
        # the process open. Killing a half-open probe would just leak sockets.
        thread = threading.Thread(target=work, name="nat-detect", daemon=True)
        thread.start()
        thread.join()

        profile = result.get("profile")

        if profile is None:
            profile = stun_mod.NatProfile()
            profile.errors.append(f"探测失败：{result.get('error')}")

        if cancel is not None:
            with contextlib.suppress(Exception):
                cancel.set()

        with contextlib.suppress(Exception):
            self._push(
                "nat_diag",
                nat_type=profile.nat_type.value,
                public=list(profile.public) if profile.public else None,
                elapsed=round(time.time() - started, 2),
            )

        return profile


async def _probe_relay(host, port, timeout):
    """
    Check that a working MC-Link relay is listening.

    IMPORTANT: the probe must use a request that does NOT require a login.
    An earlier version sent `HOST __mclink_probe__` and expected `OK HOST`,
    which the V2 relay correctly refuses with ERROR AUTH_REQUIRED -- so the
    button reported "server unavailable" while the server was perfectly
    healthy.

    `ROOMS` is unauthenticated by design, and its reply also tells us how many
    rooms are online, which is more useful than a bare handshake anyway.
    """
    from . import protocol as proto

    started = time.perf_counter()

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return False, f"连接 {host}:{port} 超时，可能被防火墙/安全组拦截", None
    except Exception as e:
        return False, f"无法连接 {host}:{port}（{e}）", None

    try:
        writer.write(
            f"{proto.ROLE_ROOMS} {proto.SENTINEL} probe -\n".encode()
        )
        await writer.drain()

        rtt = None
        rooms = None
        lines = []

        # Read until DONE so we also learn the room count.
        for _ in range(500):
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)

            if not line:
                break

            text = line.decode(errors="replace").strip()

            if not text:
                continue

            if rtt is None:
                rtt = (time.perf_counter() - started) * 1000.0

            lines.append(text)

            if text == proto.LINE_DONE:
                break

        if rtt is None:
            return False, "服务器没有响应（连接被立即关闭）", None

        first = lines[0] if lines else ""

        # Expected: "ROOMS <n>" ... "DONE"
        if first.startswith(proto.LINE_ROOMS):
            parts = first.split()

            if len(parts) > 1 and parts[1].isdigit():
                rooms = int(parts[1])

            if rooms:
                return (
                    True,
                    f"中转服务器正常，往返延迟约 {rtt:.0f} ms"
                    f"（当前 {rooms} 个房间在线）",
                    round(rtt, 1),
                )

            return (
                True,
                f"中转服务器正常，往返延迟约 {rtt:.0f} ms（当前没有房间在线）",
                round(rtt, 1),
            )

        if first.startswith(proto.LINE_ERROR):
            code = first.split()[1].upper() if len(first.split()) > 1 else ""

            # A v1 relay answers ERROR UNKNOWN_ROLE here: it is alive, just an
            # older build.
            if code == proto.ERR_UNKNOWN_ROLE:
                return (
                    True,
                    f"服务器在线，但版本较旧（{rtt:.0f} ms）。"
                    "请把服务端也更新到 v2 才能使用账号登录。",
                    round(rtt, 1),
                )

            return False, f"端口有响应但协议不匹配：{first}", round(rtt, 1)

        return False, f"端口响应异常：{first}", round(rtt, 1)

    except asyncio.TimeoutError:
        return False, "连接超时，可能被防火墙/安全组拦截", None

    except Exception as e:
        return False, f"探测失败：{e}", None

    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()
