"""
MC-Link client -- session layer.

A "session" is the object the GUI talks to. It owns a HostCore or a
PlayerCore, translates its raw callbacks into UI friendly events and exposes
a `snapshot()` dictionary for the status panel.

States (identical for host and player):
    IDLE        not running
    CONNECTING  trying to reach the relay / trying to bind the local port
    WAITING     relay reachable, but not fully operational yet
                (host: control up but no idle tunnel yet, or vice versa)
    ONLINE      fully operational
    ERROR       stopped after a failure
"""

import asyncio
import contextlib
import socket as _socket
import time

from . import punch as punch_mod
from .. import log as logmod
from .host_core import HostCore
from .player_core import PlayerCore

STATE_IDLE = "IDLE"
STATE_CONNECTING = "CONNECTING"
STATE_WAITING = "WAITING"
STATE_ONLINE = "ONLINE"
STATE_ERROR = "ERROR"

#: Session logs go to the file logger as well as to the UI, so a connection
#: that failed before the window was built is still on disk. Imported lazily to
#: keep `session.py` importable without the config package in odd environments.
STATE_TEXT = {
    STATE_IDLE: "未启动",
    STATE_CONNECTING: "连接中",
    STATE_WAITING: "等待中",
    STATE_ONLINE: "运行中",
    STATE_ERROR: "错误",
}


#: 每 90 秒重测一次自己的 NAT（他的 NAT_RECHECK_S）。
#:
#: WHY PERIODIC AND NOT ONCE: a NAT is not a property of the machine, it is a
#: property of the PATH -- and the path changes. A laptop moves between
#: networks, a router re-dials, a CGNAT reassigns. His own words: "连接时测出
#: 的结果描述的是一个我们可能已经不在的网络". A verdict cached at connect time
#: can be describing a network we left ten minutes ago, while the punch strategy
#: is still being chosen from it.
NAT_RECHECK_S = 90.0

#: Failure blacklist / backoff for the DIRECT path (his client/protocol.py).
#:
#: Two levels, both deliberately gentle: a growing wait between attempts, and
#: after DIRECT_FAIL_LIMIT consecutive failures the PEER is parked for
#: DIRECT_FAIL_BLACKLIST_S. His note on why they are this small: 60s/4/(3,6,12,24)
#: meant "nearly two minutes before the peer got another real try", which is
#: punitive on a network that came back after a blip -- and NAT4 in particular
#: often succeeds on a LATER round.
DIRECT_FAIL_LIMIT = 3              # consecutive full failures to trigger a park
DIRECT_FAIL_BLACKLIST_S = 12.0     # park this peer for this long, then retry
DIRECT_BACKOFF_MAX_S = 8.0         # ceiling on the growing wait


def nat_profile_age(monitor):
    """
    那份 NAT 画像测出来多久了（秒），没测过返回 None。

    只读一个属性，给"打洞要不要复用这份画像"（`NAT_HINT_MAX_AGE_S`）和界面上
    「多久前测的」共用 —— 两处必须是同一个数字来源。
    """
    if monitor is None:
        return None

    at = float(getattr(monitor, "profile_at", 0.0) or 0.0)

    if at <= 0.0:
        return None

    return max(0.0, time.monotonic() - at)


async def measure_nat(server_host, log=None, sock=None):
    """
    One NAT measurement, on `sock` or on a throwaway socket.

    THE ONLY COPY of this sequence on purpose. The session's periodic
    re-measurement, the room-level monitor and the punch all have to agree --
    if two of them probed differently, the status card and the punch could
    disagree about the same network, and the one the user is looking at would
    be the wrong one.

    `sock` is reusable (the monitor keeps one for the life of the room) because
    on a symmetric NAT the source port is part of the measurement: binding a
    fresh port per probe would make consecutive probes non-comparable.

    Returns a `stun.NatProfile`. Raises only if the local socket cannot be set
    up; "no server answered" comes back as a profile whose `public` is None.
    """
    from . import stun as stun_mod

    own = sock is None

    if own:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)

    try:
        if own:
            sock.bind(("0.0.0.0", 0))

        return await stun_mod.measure(
            sock,
            servers=stun_mod.default_servers(server_host),
            timeout=stun_mod.RESPONSE_TIMEOUT,
            want=4,
            log=log,
        )
    finally:
        if own:
            with contextlib.suppress(OSError):
                sock.close()


class RoomNatMonitor:
    """
    Live NAT measurement for as long as we are IN a STUN room.

    ============================================================================
      WHY THIS EXISTS SEPARATELY FROM THE SESSION
    ============================================================================

    His behaviour, and the project owner asked for it in these words: in STUN
    mode the NAT is measured from the moment you JOIN THE ROOM, and it keeps
    being measured -- it is not a one-off verdict that freezes until you press
    a button.

    A session cannot own that measurement, because there is no session yet at
    the point where it has to start: the user has joined a room and is looking
    at the status card, but has not pressed "start connection". So the monitor
    belongs to the ROOM, not to the connection, and it stops when the room ends
    or the room leaves STUN mode.

    The first measurement is IMMEDIATE (the card must not say "not measured"
    for a minute and a half), then every NAT_RECHECK_S.
    """

    def __init__(self, engine, log=None):
        self.engine = engine
        self.log = log or (lambda *_a: None)
        self.profile = None
        #: 这份画像是什么时候测出来的（monotonic）。打洞那边要判断它够不够新
        #: 才能拿来当"分类提示"（见 direct.DirectLink.nat_hint）。
        self.profile_at = 0.0
        self._task = None
        #: 【打开软件就测】用的那台中继（见 start()）。engine 里有更新的地址时
        #: 以 engine 为准；这一份只是冷启动时的一个初值。
        self.standalone_host = ""
        #: 已经跑过至少一次测量（不管成没成）。GUI 用它说"测过了 / 还没测"。
        self.measured_at_least_once = False
        #: 「立刻再测一次」的信号（见 poke()）。`_run()` 用它把 90 秒的等待
        #: 提前结束 —— 打洞要用的画像太旧时，用户不必等下一轮。
        self._wake = None

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    def _wake_event(self):
        """
        拿到（必要时创建）"立刻再测"事件。

        必须创建在**事件循环所在的那个线程**上：从 GUI 线程 `asyncio.Event()`
        在 3.10+ 已经不带 loop 参数、是懒绑定的，但跨线程 `set()` 仍然必须走
        `loop.call_soon_threadsafe`（见 poke()）。
        """
        if self._wake is None:
            self._wake = asyncio.Event()

        return self._wake

    def poke(self):
        """
        让 `_run()` **现在**就重测一轮，而不是等满 NAT_RECHECK_S（90 秒）。

        用在"打洞要开始、可手上的画像是 90 秒那轮留下的、按 30 秒的保鲜期
        不能用"的时候：与其让打洞自己再完整测一遍（多一个 socket、多 4~6 秒），
        不如把这一轮测量提前。线程安全：只做一次 call_soon_threadsafe。
        """
        loop = self.engine.loop()

        if loop is None or self._wake is None:
            return

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._wake.set)

    def start(self, standalone_host=None):
        """
        Begin measuring. Idempotent; safe to call from the GUI thread.

        ============================================================================
          2026-09-27（用户要求，问题 2）：打开软件就测，不是进房才测
        ============================================================================

        `standalone_host` 是"还没进任何房间"时拿来当 STUN 目的地的那台中继。
        `_run()` 每次都问 `engine.server_host()`（它比这里的初值新 —— 登录 /
        进房 / 点「测试服务器」都会更新它），所以这里给的只是**冷启动的那一个**：
        软件刚打开、既没登录也没进房，但网络已经该被测一次了。

        没有 `standalone_host` 且 engine 也还没有中继地址时按老路子直接返回：
        没地方可测。
        """
        if self.running:
            return

        loop = self.engine.loop()

        if loop is None:
            # No engine loop yet: there is nothing to measure, and there is no
            # point in starting a thread of our own -- the room connection that
            # made this necessary runs on that loop.
            self.log("[nat] room monitor not started: the network loop is not up")
            return

        if standalone_host:
            self.standalone_host = str(standalone_host)

        if not (self.engine.server_host() or self.standalone_host):
            self.log(
                "[nat] room monitor not started: no relay/STUN server known yet"
            )
            return

        self._task = asyncio.run_coroutine_threadsafe(self._run(), loop)

    @property
    def effective_host(self):
        """现在该往哪台服务器测：engine 里的最新中继地址优先，其次启动时的那个。"""
        return self.engine.server_host() or self.standalone_host

    def stop(self):
        """
        Stop measuring. The last profile is KEPT: the status card should still
        be able to show what the network was when we left, rather than blanking
        out and looking like a failure.
        """
        task = self._task

        if task is None:
            return

        self._task = None

        if not task.done():
            task.cancel()

    async def _run(self):
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)

        try:
            sock.bind(("0.0.0.0", 0))
        except OSError as e:
            with contextlib.suppress(OSError):
                sock.close()

            self.log(f"[nat] room monitor has no socket: {e}")
            return

        # 本循环自己的"立刻再测"事件。创建在这里 = 一定跑在引擎的事件循环上，
        # GUI 线程只能通过 `poke()` 的 call_soon_threadsafe 触发它。
        self._wake = asyncio.Event()

        try:
            while True:
                try:
                    host = self.effective_host

                    if not host:
                        # 还没有任何中继地址（用户还没登录 / 还没进房）。
                        # 什么都不测，也不落噪音 —— 等 UI 下一轮把地址给进来。
                        await self._wait_next_round()
                        continue

                    profile = await measure_nat(host, log=self.log, sock=sock)

                    self._publish(profile)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.log(f"[nat] room measurement failed (will retry): {e!r}")

                await self._wait_next_round()
        except asyncio.CancelledError:
            # stop() / 进程退出：正常收工，socket 在 finally 里关。
            pass
        finally:
            with contextlib.suppress(OSError):
                sock.close()
            self._wake = None

    async def _wait_next_round(self):
        """
        等 NAT_RECHECK_S（90 秒），除非有人 `poke()` 让我们立刻重测。

        `asyncio.wait_for` 超时会抛 TimeoutError；被 `set()` 叫醒则正常返回。
        两种都只是"下一轮开始"，没有错误含义。
        """
        wake = self._wake_event()

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=NAT_RECHECK_S)

        wake.clear()

    def _publish(self, profile):
        """
        Store the profile and speak only when it MOVED.

        A 90-second "still the same" heartbeat would drown the one line that
        matters ("your network changed"), which is the whole reason the
        re-measurement exists. The FIRST measurement always speaks, because
        until it lands the status card has nothing to show.
        """
        before = self.profile
        self.profile = profile
        self.profile_at = time.monotonic()
        self.measured_at_least_once = True

        if profile is None or profile.public is None:
            self.log(
                "[nat] no public endpoint yet (no STUN server answered) "
                "-- will try again"
            )
            from .network_diagnostics import log_failure
            asyncio.create_task(log_failure(self.effective_host, self.log))
            return

        changed = (
            before is None
            or before.public != profile.public
            or before.nat_type != profile.nat_type
        )

        if changed:
            self.log(
                f"[nat] room measurement: type={profile.nat_type.value} "
                f"filter={profile.filtering.value} public={profile.public} "
                f"step={profile.step} per_ip_pool={profile.per_ip_pool}"
            )



class SessionBase:
    role = "?"

    def __init__(self, config, emit, auth=None):
        self.config = config
        self._emit = emit

        #: V3：谁来提供"房间成员连接"（候选交换要走它）。
        #: 引擎在 start_session 时注入，因为房间成员连接由引擎持有。
        self.room_provider = None

        #: V3：引擎本身。房间级打洞（进房就打洞）的产物挂在引擎上，
        #: 会话要通过它才能拿到那条已打通的通道。
        self.engine_provider = None

        #: 本次会话的直连状态：None=没试过 / "udp"=已直连 / "relay"=走中转。
        #: 界面读它显示"直连 / 中转"。
        self.direct_via = None

        #: True only while a session is actually PUSHING BYTES over the punched
        #: link. The punch succeeding is a different fact (see room_direct): the
        #: card distinguishes "punched, waiting for the game" from "carrying".
        self.direct_carrying = False

#: 每个隧道一个直连协调器（HostCore 可能同时有多个隧道）。
        self._direct_links = {}

        #: 最近的直连画像，供运行状态卡片显示（取代原来的独立诊断栏目）。
        self._direct_attempts = 0

        #: Consecutive failed rounds, and the monotonic time a park expires.
        #: See _direct_note_failure / _direct_blacklisted.
        self._direct_fails = 0
        self._direct_blacklist_until = 0.0

        #: What the punch is doing RIGHT NOW, as one short line for the status
        #: card. His status() strings, e.g. "direct punch #3 (public)" and
        #: "waiting for the coordinated start". "punching" alone told the user
        #: nothing about which round, what phase, or how long until the next
        #: try -- and those are exactly the questions a stuck punch raises.
        self._direct_step = ""
        #: The inbound verdict for this session (his `_inbound_state`) plus the
        #: reason when it is unknown.
        self._inbound_state = punch_mod.INBOUND_UNKNOWN

        #: 这个会话是否是被 abort() 强拆的（日志/排查用）。
        self._aborted = False
        self._inbound_note = ""
        self._direct_peer = ""
        self._nat_profile = None

        #: 周期重测任务 + 一个"打洞进行中"标记。
        #: 打洞期间不重测：重测要用同一个 socket（对称 NAT 上换 socket 学到的
        #: 映射毫无意义），而在他正等回包的时候抢那个 socket，等于把要等的包
        #: 弄丢。漏掉一次 90 秒的 tick 没有代价，打扰一次打洞是断线。
        self._nat_task = None
        self._punching = False

        # V2 login session (AuthSession). None means "no account system",
        # which is only valid when the relay has MCLINK_REQUIRE_ACCOUNT=0.
        self.auth = auth

        self.state = STATE_IDLE
        self.core = None
        self._supervisor = None
        self._auto_network_task = None
        self._stopped = asyncio.Event()

    # --------------------------------------------------------
    # V3 direct path
    # --------------------------------------------------------

    def on_direct_carrying(self, carrying):
        """A session started or stopped carrying the game over the direct link."""
        self.direct_carrying = bool(carrying)

    def release_direct_path(self):
        """Give the room's punched link back for the next connection."""
        engine = self.engine_provider

        if engine is None:
            return

        with contextlib.suppress(Exception):
            engine.release_room_direct_path()

    def set_direct_backend(self, host, port):
        """
        Tell the room's direct coordinator where the LOCAL Minecraft world is.

        It applies the address to EVERY per-player link -- not just whichever
        one a session happened to be handed: a late joiner's sid streams arrive
        on THEIR link, and that link must be able to open the world too.
        """
        engine = self.engine_provider

        if engine is None:
            return

        with contextlib.suppress(Exception):
            engine.set_room_direct_backend(host, port)

    def traffic_callback(self):
        """
        `fn(mode, up, down)` for the cores' relay pumps（审计问题 6）。

        账本是**进程级**的：每个会话结束时回收自己的 Core，但累计数字留在
        引擎那本账上 —— 这正是用户要的"离开房间不清零，只有重启客户端才清零"。
        """
        engine = self.engine_provider

        if engine is None:
            return None

        def record(mode, up, down):
            with contextlib.suppress(Exception):
                engine.record_traffic(mode, up, down)

        return record

    def make_direct_path_provider(self):
        """
        Give the cores the ROOM's punched link, if there is one.

        ============================================================================
          WHY THIS IS A SEPARATE PROVIDER FROM `make_direct_hook`
        ============================================================================

        They are two different things and conflating them is what made "direct"
        a status word instead of a transport:

          * `direct_hook`    the OLD path: punch AFTER the game connected, and
                             only ever report success to the UI;
          * `direct_path_provider` the NEW path: the room already punched when
                             we JOINED (room_direct.py), so the game can ride
                             the result immediately.

        Returns a callable producing a `direct_carry.PeerLink` or None. None
        means the cores behave exactly as they did before this feature existed:
        the relay carries everything.
        """
        if getattr(self.config, "mode", "relay") != "stun":
            return None

        engine = self.engine_provider

        if engine is None:
            return None

        async def provider():
            # The ENGINE owns the room-level punch (room_direct.py): the room
            # membership object knows nothing about sockets, and asking it would
            # return None forever -- which is precisely the "we punched but
            # nobody used it" failure this whole path exists to remove.
            #
            # STUN mode does NOT fall back to the relay for main traffic (the
            # owner's rule: "已打的通道和主流量不允许碰中继"), so this WAITS for
            # the link instead of returning None on the first look. It returns
            # None only when the punch has stopped trying altogether, which is
            # when relaying is the only way to keep the session alive.
            try:
                return await engine.wait_room_direct_path()
            except Exception:
                return None

        return provider

    def make_auto_manager(self):
        if getattr(self.config, "mode", "relay") == "auto":
            if self.engine_provider is None:
                raise RuntimeError("AUTO requires the room engine")
            return self.engine_provider.auto_manager
        return None

    def make_direct_hook(self):
        """
        Build the `direct_hook` the cores call once a relay tunnel is up.

        Returns None when this session is not in STUN mode -- and then the
        cores never even look at the direct path, which is what keeps Relay
        mode byte-for-byte identical to v2.

        The contract with the cores is deliberately narrow: "try to upgrade;
        return True if a direct path exists". Whether the returned path is
        actually put to use is decided here, not there, so the cores stay
        unaware of STUN entirely.
        """
        if getattr(self.config, "mode", "relay") != "stun":
            return None

        # ============================================================================
        #  有房间级打洞时，这个"事后升级"必须让路
        # ============================================================================
        #
        # 两条路都在 STUN 模式下生效，但它们抢的是**同一个 socket**：
        #
        #   * 房间级打洞（room_direct.py）：进房就打，打通后把 socket 交给游戏；
        #   * 这里的 direct_hook：游戏连上之后再打一次，并"顺手"把通道认领走。
        #
        # 结果就是游戏还没用上，通道已经被 direct_hook 认领（`take_path()` 只能
        # 给一次），然后它又用同一个 key 重新打一遍洞 —— 上一个洞白打了，
        # 会话还是走中继。实测症状：HOST 日志已经 `Minecraft data over the
        # DIRECT link`，PLAYER 那边却卡在中继上，游戏数据一个字节也过不去。
        #
        # 所以：房间级打洞在的时候，这里直接不装 hook。它存在的前提（"中继隧道
        # 先跑着，再尝试升级"）已经被"进房就打洞、打通了再挂游戏"取代了。
        if self.make_direct_path_provider() is not None:
            return None

        async def hook(key):
            return await self._try_direct(key)

        return hook

    def start_nat_recheck(self):
        """
        Begin the periodic NAT re-measurement, from the moment we are in STUN
        mode. Idempotent.

        His design: the measurement starts when the mode is chosen (not when a
        connection is attempted) and then repeats forever, because a NAT can
        change while the software is running. The status card therefore shows
        a LIVE verdict that can update by itself, instead of a number frozen
        at whatever the first probe said.
        """
        if self._nat_task is not None and not self._nat_task.done():
            return

        self._nat_task = asyncio.create_task(self._nat_recheck_loop())

    def stop_nat_recheck(self):
        if self._nat_task is None:
            return

        self._nat_task.cancel()
        self._nat_task = None

    async def _nat_recheck_loop(self):
        """
        Re-measure every NAT_RECHECK_S, skipping while a punch is in flight.

        The first tick is DELAYED, not immediate: the punch path has just
        measured on its own socket, so measuring again right now would be pure
        duplication -- and his note is explicit that this loop must never
        steal the punch's attention, because on a symmetric NAT the probe has
        to use the same socket the punch is using.
        """
        try:
            while True:
                await asyncio.sleep(NAT_RECHECK_S)

                if self._stopped.is_set():
                    return

                if self._punching:
                    self.log(
                        "[nat] re-check skipped: a punch is in flight "
                        "(the probe would use the same socket)"
                    )
                    continue

                try:
                    await self._recheck_nat()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.log(f"[nat] re-check failed (will retry): {e!r}")
        except asyncio.CancelledError:
            return

    async def _recheck_nat(self):
        """
        One re-measurement, on a throwaway socket.

        Deliberately NOT the punch socket here: this runs while no punch is in
        flight (checked above), so there is nothing to disturb, and the
        periodic probe keeps away from the very allocation the next punch
        depends on.

        `measure_nat` is the same sequence the room monitor and the punch use,
        so a change of network cannot be reported differently depending on who
        happened to look.
        """
        profile = await measure_nat(self.config.server_host, log=self.log)

        # The measurement is recorded with every field the classifier produced:
        # these are the numbers the punch strategy is chosen FROM, so a report
        # that omits them cannot be analysed.
        with contextlib.suppress(Exception):
            logmod.event(
                "nat.measured",
                where="session",
                nat_type=getattr(getattr(profile, "nat_type", None), "value", ""),
                filtering=getattr(
                    getattr(profile, "filtering", None), "value", ""
                ),
                public=str(getattr(profile, "public", "")),
                step=getattr(profile, "step", 0),
                per_ip_pool=bool(getattr(profile, "per_ip_pool", False)),
                servers=len(getattr(profile, "servers", []) or []),
                errors=len(getattr(profile, "errors", []) or []),
            )

        before = self._nat_profile

        self._nat_profile = profile

        # Only speak when something actually MOVED. His rule: re-publish only
        # on a change, otherwise a 90s heartbeat turns the log into noise and
        # the interesting line ("my network changed") gets lost in it.
        changed = (
            before is None
            or before.public != profile.public
            or before.nat_type != profile.nat_type
        )

        if changed:
            self.log(
                f"[nat] network changed: type={profile.nat_type.value} "
                f"public={profile.public} "
                f"step={profile.step} per_ip_pool={profile.per_ip_pool}"
            )
        else:
            self.log(
                f"[nat] re-check confirmed unchanged: "
                f"type={profile.nat_type.value} public={profile.public}"
            )

        return profile

    async def _try_direct(self, key):
        """
        Keep trying to punch a direct path until it works or the session ends.

        ============================================================================
          WHY IT RETRIES FOREVER (and why our first version was wrong)
        ============================================================================

        MCLanP2P's own wording for this mode is:

            "只走 UDP 打洞，每 3 秒重试直到连上，永不走中转"

        Punching is a collision between two MOVING cursors. The first attempt
        tells you almost nothing: the peer's NAT allocates a fresh port per
        destination, its cursor drifts every second, and its socket array
        occupies whatever band the cursor happened to be in. An attempt that
        misses now can hit 20 seconds later.

        Our first version gave up after ONE attempt and reported failure. On a
        real NAT4 pair that is guaranteed to look broken even though the punch
        is working exactly as designed -- and the logs the project owner sent
        show precisely that: one `c2s` spray that found nothing, then a
        surrender, while the same peer pair had already punched successfully
        moments earlier (c2c, port 19312).

        Never touches the relay: that would be AUTO's behaviour, and AUTO is
        v6. This mode is a manual "direct only".

        Runs in the background (the caller fire-and-forgets it), so the game
        keeps flowing over whatever path it already had while we retry.
        """
        from . import direct as direct_mod

        room = self.room_provider() if self.room_provider is not None else None

        if room is None:
            self.log("[direct] no room membership connection; cannot exchange candidates")
            self.direct_via = None
            return False

        link = self._direct_links.get(key)

        if link is None or link.closed:
            # Identities decide who leads when both ends spray at once. Our
            # own is the client id we already publish; the peer's is taken from
            # the room's host slot (the star topology's only other endpoint
            # that matters), falling back to the room's own id so the tie-break
            # is never empty.
            state = getattr(room, "state", None)
            peer_id = (
                getattr(state, "host_name", "") or getattr(state, "client_id", "")
                if state is not None else ""
            )

            link = direct_mod.DirectLink(
                room,
                self.config.server_host,
                self.config.server_port,
                log=self.log,
                my_id=getattr(self.config, "client_id", "") or self.account,
                peer_id=peer_id,
            )
            # The link feeds the live phase straight into the status card.
            link.step = self._set_direct_step
            self._direct_links[key] = link

        link.step = self._set_direct_step

        attempt = 0

        # ---- Inbound reachability, ONCE per link, before the first punch ----
        #
        # His order: measure, then run the inbound probe, then punch. The probe
        # answers a question nothing else can ("can an UNRELATED host reach us
        # at all?"), and without it a failed punch cannot be told apart from an
        # impossible one. It is also what fills in the 入站 row -- leaving it at
        # "unknown" while both players are connected told the user nothing.
        try:
            self._set_direct_step("checking inbound reachability")
            state = await link.probe_inbound()
            self._inbound_state = state
            self._inbound_note = link.inbound_note

            # Fold the verdict into the published profile, **but only when the
            # RFC 5780 measurement has nothing to say**.
            #
            # 2026-09-25 用户实测报的错：他是 NAT3（端口受限），我们报 NAT2。
            # 有两个原因，这是第二个（第一个在 core/stun.py 的"热端口"）：
            # 这里原来**无条件**用入站探测的结论覆盖测出来的过滤档 ——
            #   * 探测包到达（而且我们是先暖过映射的）→ 写成 eif；
            #   * 于是"换端口测出来是 apdf"这个**硬证据**被一个暖场后的到达
            #     覆盖掉了，NAT3 就被显示成 NAT1/NAT2 那一侧。
            #
            # 现在：测量值不是 unknown 就不许被覆盖 —— 一次"换端口应答从别的
            # 端口回来/没回来"是 RFC 5780 的直接测量；而"暖过映射之后到达"
            # 只能证明最多是地址受限（adf）。只有测量失败（服务器不支持
            # CHANGE-REQUEST，或者包被丢光）时，才让入站探测来补这一档。
            if state == punch_mod.INBOUND_OPEN:
                self._apply_inbound_filter(punch_mod.Filter.ADF)
            elif state == punch_mod.INBOUND_PUNCHABLE:
                self._apply_inbound_filter(punch_mod.Filter.ADF)
        except Exception as e:
            self.log(f"[nat] inbound probe skipped: {e!r}")

        while not self._stopped.is_set():
            attempt += 1
            self._direct_attempts = attempt
            self._punching = True

            try:
                # Re-measure EVERY round, deliberately.
                #
                # This is the anchor-freshness rule from his code: on a NAT
                # whose cursor moves (measured ~73 ports/second on a real
                # CGNAT), a port measured 15s ago is thousands of ports from
                # where the array will actually open. `force=True` keeps the
                # anchor seconds old, which is what makes the window land.
                nat = await link.measure(force=True)

                # Publish it so the status card can show "what my NAT is"
                # WITHOUT a separate diagnostics panel.
                self._nat_profile = nat

                if nat.public is None:
                    self.log(
                        f"[direct] attempt {attempt}: no public endpoint "
                        f"(STUN unreachable) -- retrying"
                    )
                    await self._direct_sleep(attempt)
                    continue

                self.log(
                    f"[direct] attempt {attempt}: local nat="
                    f"{nat.nat_type.value} filter={nat.filtering.value} "
                    f"public={nat.public[0]}:{nat.public[1]} "
                    f"step={nat.step} per_ip_pool={nat.per_ip_pool} "
                    f"inbound={self._inbound_state}"
                )
                self._set_direct_step(
                    f"punch #{attempt}: publishing our candidate "
                    f"({nat.nat_type.value})"
                )

                if await link.advertise() is None:
                    self.log(
                        f"[direct] attempt {attempt}: peer candidate not "
                        f"received -- retrying"
                    )
                    self._set_direct_step(
                        f"punch #{attempt}: no candidate from the peer yet"
                    )
                    await self._direct_sleep(attempt)
                    continue

                result = await link.attempt(
                    per_ip_pool=bool(nat.per_ip_pool)
                )

                self._direct_peer = (
                    f"{result.addr[0]}:{result.addr[1]}" if result.addr else ""
                )

                if result.ok:
                    # Success clears the blacklist: the count is "this peer is
                    # not answering right now", never a permanent verdict.
                    self._direct_fails = 0
                    self._direct_blacklist_until = 0.0
                    self.direct_via = "udp"

                    with contextlib.suppress(Exception):
                        logmod.event(
                            "punch.attempt",
                            attempt=attempt,
                            ok=True,
                            plan=result.plan,
                            sent=result.sent,
                            learned=len(result.learned),
                            peer=str(result.addr),
                            elapsed=round(float(result.elapsed or 0.0), 3),
                        )
                    self.log(
                        f"[direct] PUNCHED after {attempt} attempt(s): "
                        f"peer={result.addr[0]}:{result.addr[1]} "
                        f"sent={result.sent} elapsed={result.elapsed:.1f}s"
                    )
                    return True

                self.direct_via = "failed"
                action = self._direct_note_failure()

                with contextlib.suppress(Exception):
                    logmod.event(
                        "punch.attempt",
                        attempt=attempt,
                        ok=False,
                        plan=result.plan,
                        sent=result.sent,
                        learned=len(result.learned),
                        reason=result.reason or "no packet arrived",
                        elapsed=round(float(result.elapsed or 0.0), 3),
                        action=action,
                        peer_nat=getattr(
                            getattr(link, "peer_nat", None), "value", ""
                        ),
                        peer_filter=getattr(
                            getattr(link, "peer_filter", None), "value", ""
                        ),
                        per_ip_pool=bool(
                            getattr(getattr(link, "nat", None),
                                    "per_ip_pool", False)
                        ),
                    )
                self.log(
                    f"[direct] attempt {attempt} failed: "
                    f"plan={result.plan} sent={result.sent} "
                    f"learned={len(result.learned)} "
                    f"reason={result.reason or 'no packet arrived'} "
                    f"-> {action}"
                )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"[direct] attempt {attempt} error: {e!r} -- retrying")
            finally:
                # Never leave the flag set: a stuck "punching" flag would stop
                # the periodic re-check forever.
                self._punching = False

            await self._direct_sleep(attempt)

        return False

    def _set_direct_step(self, text):
        """Publish the live punch phase for the status card."""
        self._direct_step = str(text or "")

    def _apply_inbound_filter(self, filt):
        """
        Fill in the filtering grade from the inbound probe **only when the
        RFC 5780 measurement failed**.

        The probe used to be treated as the stronger evidence and overwrote
        whatever CHANGE-REQUEST had measured. That is backwards, and it is half
        of the user's "NAT3 被测成 NAT2" report:

          * "换端口" test  = a direct RFC 5780 measurement of the PORT axis
                             (does a reply from another port of a host we
                             contacted get in?) -> decides apdf vs adf;
          * inbound probe  = a datagram from the relay's IP, and we **warm the
                             mapping towards the relay first** (otherwise it is
                             dropped on every restricted NAT and the row sits at
                             "not measured" forever). So "it arrived" only ever
                             proves "not apdf" -- at most address-dependent.

        A warm-up arrival can therefore never upgrade us to eif, and it must
        never wipe out a measured apdf. It is only used to fill a blank.
        """
        profile = self._nat_profile

        if profile is None:
            return

        before = getattr(profile, "filtering", None)

        if before is not None and before.value != "unknown":
            # 已经有硬测量了，不许被推断覆盖。
            self.log(
                f"[nat] inbound probe says {filt.value}, but CHANGE-REQUEST "
                f"measured {before.value}; keeping the measurement"
            )
            return

        if before == filt:
            return

        profile.filtering = filt
        self.log(
            f"[nat] filtering: {filt.value} "
            f"(from the inbound probe; CHANGE-REQUEST had nothing)"
        )

    async def _direct_sleep(self, attempt):
        """
        Backoff between punch attempts: his growing ladder, then a real PARK.

        ============================================================================
          WHY A BLACKLIST AND NOT JUST "KEEP TRYING"
        ============================================================================

        Retrying an unreachable peer every 3 seconds forever is not persistence,
        it is a loop with no exit: the user watches "punching" and nothing ever
        changes. His design (client/protocol.py) has two levels:

          * a growing backoff between attempts -- (1, 2, 4), capped at 8s;
          * after DIRECT_FAIL_LIMIT (3) consecutive failures, the PEER is
            blacklisted for DIRECT_FAIL_BLACKLIST_S (12s), and the failure
            count is dropped so the next cycle starts from a clean slate.

        The numbers are small on purpose. His own note: a 45s backoff plus a 60s
        park meant "nearly two minutes before the peer got another real try",
        which is punitive on a network that came back after a blip -- and NAT4 in
        particular often succeeds on a LATER round, so parking too long throws
        away the attempts that would have worked.

        The retry itself is still forever (STUN mode never falls back to the
        relay), so the park is a pause, never a surrender.
        """
        # A blacklisted peer is parked: sleep out the rest of the park in one
        # go, then start the ladder again.
        left = self._direct_blacklisted()

        if left > 0:
            delay = left
            self.log(
                f"[direct] peer is blacklisted for {left:.0f}s more "
                f"(failed {DIRECT_FAIL_LIMIT} rounds in a row); parking"
            )
        else:
            ladder = (1.0, 2.0, 4.0)
            delay = min(ladder[min(attempt - 1, len(ladder) - 1)],
                        DIRECT_BACKOFF_MAX_S)
            self.log(f"[direct] next attempt in {delay:.0f}s")

        # Report the countdown, not just "waiting": the number is what makes a
        # stuck punch diagnosable from the status card alone.
        self._set_direct_step(
            f"attempt {attempt} done; next try in {delay:.0f}s"
        )

        deadline = time.monotonic() + delay

        while True:
            left = deadline - time.monotonic()

            if left <= 0 or self._stopped.is_set():
                break

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=0.25)

            self._set_direct_step(
                f"attempt {attempt} done; next try in {max(0.0, left):.0f}s"
            )

    def _direct_note_failure(self):
        """
        One full round failed. Returns the action taken ("retry" or "blacklist").

        Consecutive failures only: any success resets the count, which is what
        makes this a "this peer is not answering RIGHT NOW" signal rather than a
        permanent verdict.
        """
        self._direct_fails += 1

        if self._direct_fails >= DIRECT_FAIL_LIMIT:
            self._direct_blacklist_until = (
                time.monotonic() + DIRECT_FAIL_BLACKLIST_S
            )
            # Drop the count so the next cycle starts clean, exactly as his
            # `_on_round_failed` does -- otherwise it would be permanently at
            # the limit and park after a single failure forever after.
            self._direct_fails = 0
            self.log(
                f"[direct] {DIRECT_FAIL_LIMIT} rounds in a row failed; "
                f"blacklisting this peer for {DIRECT_FAIL_BLACKLIST_S:.0f}s"
            )
            return "blacklist"

        return "retry"

    def _direct_blacklisted(self):
        """Seconds left on this peer's park, 0 when it may be tried."""
        until = self._direct_blacklist_until

        if not until:
            return 0.0

        left = until - time.monotonic()

        if left <= 0:
            self._direct_blacklist_until = 0.0
            return 0.0

        return left

    def _close_direct_links(self):
        for link in list(self._direct_links.values()):
            with contextlib.suppress(Exception):
                link.close()

        self._direct_links = {}

    # --------------------------------------------------------

    @property
    def token(self):
        token = self.auth.token if self.auth is not None else ""
        if not token and getattr(self.config, "mode", "relay") == "auto":
            token = getattr(self.engine_provider, "_guest_token", "")
        return token

    @property
    def account(self):
        return self.auth.username if self.auth is not None else ""

    # --------------------------------------------------------

    def log(self, message):
        self._emit("log", text=message)

    def event(self, name, **data):
        # Track the one core event that changes what the status card may CLAIM.
        #
        # `direct_carrying` means "a session is pushing game bytes over the
        # punched link right now". A successful punch is NOT that: the card
        # reads this to avoid showing "已直连" while the connection is still
        # being set up.
        if name == "direct_carrying":
            self.on_direct_carrying(data.get("carrying"))

        self._emit("event", name=name, data=data)

    def set_state(self, state, reason=""):
        if state == self.state:
            return

        self.state = state
        self._emit("state", state=state, reason=reason)

    async def start(self):
        raise NotImplementedError

    async def abort(self):
        """
        快速、确定地拆掉这个会话 —— 不等，只保证**东西都关掉**。

        为什么需要它：`stop()` 是"有礼貌"的收尾（会等日志、等任务、等
        socket 关干净），慢中继或卡住的连接上可能超过调用方的超时。而超时
        之后如果只是把异常吞掉，旧会话的**监听 socket 还开着**、引擎却已经
        不持有它了 —— 下一次连接在同一个本地端口上再绑就会失败（用户报的
        "端口占用/无法再建立新的连接"）。

        所以超时的路径要再走一遍这个：取消一切、关掉所有 socket，不等。
        它是幂等的，`stop()` 正常跑完后再调也没有副作用。
        """
        self._aborted = True

        if self._supervisor is not None:
            self._supervisor.cancel()
            self._supervisor = None

        core = self.core

        if core is not None:
            closer = getattr(core, "abort", None)

            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer()

        with contextlib.suppress(Exception):
            self.stop_nat_recheck()

        with contextlib.suppress(Exception):
            self._close_direct_links()

        self.set_state(STATE_IDLE)
        self._stopped.set()

    async def stop(self):
        raise NotImplementedError

    def snapshot(self):
        return {
            "role": self.role,
            "state": self.state,
            "state_text": STATE_TEXT.get(self.state, self.state),
            "account": self.account,
            "logged_in": self.auth is not None,
            "is_admin": bool(self.auth.is_admin) if self.auth else False,
            #: V3 直连状态：None=没试过/未知，"udp"=已打通直连，"relay"=走中转。
            #: 界面据此显示"通路：直连 / 中转"。它只是**显示**，
            #: 不参与任何连接判断。
            "mode": getattr(self.config, "mode", "relay"),
            "direct_via": self.direct_via,
            # Live direct-path state for the status card: attempts so far and
            # the local NAT picture. These replaced the standalone
            # diagnostics card (removed on request).
            "direct_attempts": self._direct_attempts,
            "direct_carrying": self.direct_carrying,
            "direct_peer": self._direct_peer,
            #: The live punch phase ("direct punch #3 (public)", "waiting for
            #: the coordinated start", "paused 12s", ...) and the inbound
            #: verdict, so the card can be specific instead of saying "punching".
            "direct_step": self._direct_step,
            "inbound_state": self._inbound_state,
            "inbound_note": self._inbound_note,
            "nat_profile": self._nat_profile,
        }

    # --------------------------------------------------------

    async def _supervise(self):
        """
        Derive the session state from the core and push a snapshot to the GUI
        about once per second.
        """
        try:
            if getattr(self.core, "auto_manager", None) is not None:
                from .auto_network import supervise
                self._auto_network_task = asyncio.create_task(supervise(self.core, self.room_provider))
            while True:
                with contextlib.suppress(Exception):
                    self.refresh_state()

                with contextlib.suppress(Exception):
                    self._emit("snapshot", data=self.snapshot())

                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            raise
        finally:
            if self._auto_network_task is not None:
                self._auto_network_task.cancel()
                await asyncio.gather(self._auto_network_task, return_exceptions=True)
                self._auto_network_task = None

    def refresh_state(self):
        raise NotImplementedError

    # --------------------------------------------------------
    # V2: login bookkeeping
    # --------------------------------------------------------

    def invalidate_auth(self, reason=""):
        """
        Drop the login session because the relay rejected it.

        After this the GUI must require a fresh login before starting again.
        """
        if self.auth is None and not reason:
            return

        self.auth = None
        self.event("auth_invalidated", reason=reason or "登录状态已失效")

    async def delete_own_room(self, room):
        """
        Delete one of my rooms.

        The host deletes through its CONTROL socket (no extra connection);
        a player uses a short lived DELETE connection. Both are refused by the
        server while the room still has players.
        """
        raise NotImplementedError

    async def rename_own_account(self, new_username):
        """
        Rename my account from inside a RUNNING session.

        Uses a short lived control connection (never the data tunnel) and then
        adopts the new name locally, so an active room keeps working and simply
        starts reporting the new host/player name.
        """
        from . import control_client

        try:
            confirmed, message = await control_client.rename_account(
                self.config.server_host,
                self.config.server_port,
                new_username,
                self.auth,
                self.config.client_id,
            )
        except control_client.ControlError as e:
            return False, str(e)

        if self.auth is not None:
            self.auth.username = confirmed

        if self.core is not None:
            self.core.username = confirmed

        self.event("account_renamed", username=confirmed)

        return True, message


# ============================================================
# HOST session
# ============================================================

class HostSession(SessionBase):
    role = "host"

    async def start(self):
        cfg = self.config

        self.core = HostCore(
            server_host=cfg.server_host,
            server_port=cfg.server_port,
            room=cfg.room,
            mc_host=cfg.mc_host,
            mc_port=cfg.mc_port,
            max_tunnels=cfg.max_tunnels,
            max_players=cfg.max_players,
            client_id=cfg.client_id,
            username=cfg.username or self.account,
            token=self.token,
            reconnect_delay=cfg.reconnect_delay,
            on_log=self.log,
            on_event=self.event,
            direct_hook=self.make_direct_hook(),
            direct_path_provider=self.make_direct_path_provider(),
            direct_path_releaser=self.release_direct_path,
            direct_backend_setter=self.set_direct_backend,
            auto_manager=self.make_auto_manager(),
            on_traffic=self.traffic_callback(),
        )

        self.set_state(STATE_CONNECTING)
        self._stopped.clear()

        await self.core.start()

        self._supervisor = asyncio.create_task(self._supervise())

        # STUN mode: start the live NAT measurement now, while a connection is
        # being set up -- not on demand from a button. See start_nat_recheck.
        if self.make_direct_hook() is not None:
            self.start_nat_recheck()

    async def stop(self, graceful=False):
        """
        停掉这个会话。

        `graceful=True` = **用户点的停止**（只有这条路会告诉中继"我是主动停的"）。

        模式热切换的重建、掉线清理、自动重连前的收尾都走 `graceful=False`：
        它们都是"我要重新连 / 我掉了"，不是"我不玩了"。v3.9.16 把这两件事混
        成一件，结果房主只要正常停过一次，中继那个"主动停止"标记就永不复位，
        2.5 秒宽限窗口从此永久失效（复核报告 P1）。
        """
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None

        if self.core is not None:
            await self.core.stop(graceful=graceful)

        self.stop_nat_recheck()
        self._close_direct_links()

        self.set_state(STATE_IDLE)
        self._stopped.set()

    def refresh_state(self):
        if self.core is None:
            return

        online_tunnels = sum(
            1 for v in self.core.tunnel_ready.values() if v
        )

        if self.core.auth_error:
            self.invalidate_auth(self.core.auth_error)
            self.core.auth_error = ""

        # ====================================================================
        #  版本门优先：它必须**立刻**变成「错误」，界面才会弹「需要更新」
        # ====================================================================
        #
        # 放在最前面，是因为版本不一致时 `control_online` / `running` 这些
        # 字段的状态是"正在重连"—— 用户看到的就是"一直在连接"，而真相是
        # "永远连不上，去更新"。这一条必须压过其他所有状态判断。
        rejected = getattr(self.core, "version_rejected", "")

        if rejected:
            self.set_state(STATE_ERROR, reason=rejected)
            return

        if self.core.control_online:
            if online_tunnels > 0:
                self.set_state(STATE_ONLINE)
            else:
                self.set_state(STATE_WAITING)
        elif self.core.running:
            self.set_state(STATE_CONNECTING)
        elif self.core.last_error:
            # ============================================================================
            #  核心自己停了 = 会话必须变成「错误」
            # ============================================================================
            #
            # The core stops itself when the relay rejects the token or the
            # account is disabled (HostCore.connect_control sets running=False
            # and records the reason in last_error). None of the three cases
            # above matched, so the session stayed on 「运行中」 forever: the
            # header kept claiming 「已连接中转服务器」 and every traffic /
            # tunnel number kept showing its last value.
            #
            # The error itself is reported once, by core.last_error; this only
            # makes the STATE honest.
            self.set_state(STATE_ERROR, reason=self.core.last_error)

    async def delete_own_room(self, room=None):
        room = room or self.config.room

        if self.core is None:
            return False, "尚未启动，无法删除房间"

        if room != self.core.room:
            # A different room: fall back to a short lived DELETE connection.
            return await self._delete_via_connection(room)

        ok, message, event = await self.core.delete_own_room(wait_seconds=8)

        if not ok or event is None:
            return False, message

        try:
            await asyncio.wait_for(event.wait(), timeout=8)
        except asyncio.TimeoutError:
            # ============================================================================
            #  放弃等待 = 必须把请求也作废
            # ============================================================================
            #
            # DELETE 的应答没有请求标识（就一行 OK DELETE / ERROR）。如果超时
            # 之后还把"我在等删除应答"这个标记留着，下一条无关的 ERROR 就会被
            # 当成这次删除的结果 —— 用户看到的结论可能根本不是他那次删除的。
            #
            # 两个 8 秒必须一致：短了会把还属于自己的应答丢掉，长了会留出
            # "应答回来时已经没人认领"的窗口。
            self.core.abandon_delete_request(event)

            return False, "服务器没有确认删除请求（可能需要重试）"

        return self.core.delete_result or (False, "未知结果")

    async def _delete_via_connection(self, room):
        from . import control_client

        try:
            return await control_client.delete_room(
                self.config.server_host,
                self.config.server_port,
                room,
                self.auth,
                self.config.client_id,
            )
        except control_client.ControlError as e:
            return False, str(e)

    def snapshot(self):
        data = super().snapshot()

        if self.core is not None:
            data.update(self.core.snapshot())
            data["tunnels_ready"] = sum(
                1 for v in self.core.tunnel_ready.values() if v
            )

        data["state"] = self.state
        data["state_text"] = STATE_TEXT.get(self.state, self.state)

        return data


# ============================================================
# PLAYER session
# ============================================================

class PlayerSession(SessionBase):
    role = "player"

    async def start(self):
        cfg = self.config

        self.core = PlayerCore(
            server_host=cfg.server_host,
            server_port=cfg.server_port,
            room=cfg.room,
            local_host=cfg.local_host,
            local_port=cfg.local_port,
            client_id=cfg.client_id,
            username=cfg.username or self.account,
            token=self.token,
            on_log=self.log,
            on_event=self.event,
            direct_hook=self.make_direct_hook(),
            direct_path_provider=self.make_direct_path_provider(),
            direct_path_releaser=self.release_direct_path,
            auto_manager=self.make_auto_manager(),
            on_traffic=self.traffic_callback(),
        )

        self.set_state(STATE_CONNECTING)
        self._stopped.clear()

        if self.make_direct_hook() is not None:
            self.start_nat_recheck()

        try:
            await self.core.start()
        except OSError:
            self.set_state(STATE_ERROR, reason=self.core.last_error)
            raise

        self.set_state(STATE_ONLINE)

        self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self, graceful=False):
        """
        停掉这个会话。

        `graceful=True` = **用户点的停止**（只有这条路会告诉中继"我是主动停的"）。

        模式热切换的重建、掉线清理、自动重连前的收尾都走 `graceful=False`：
        它们都是"我要重新连 / 我掉了"，不是"我不玩了"。v3.9.16 把这两件事混
        成一件，结果房主只要正常停过一次，中继那个"主动停止"标记就永不复位，
        2.5 秒宽限窗口从此永久失效（复核报告 P1）。
        """
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None

        if self.core is not None:
            await self.core.stop(graceful=graceful)

        self.stop_nat_recheck()
        self._close_direct_links()

        self.set_state(STATE_IDLE)
        self._stopped.set()

    def refresh_state(self):
        if self.core is None:
            return

        # 版本门：玩家侧也一样，先变成「错误」让界面弹「需要更新」——
        # 否则这里只会把状态收回 IDLE，看起来像"自己停了"，用户根本不知道
        # 是版本不对（他报的就是这个：只在日志里看到一行，界面一直在连）。
        rejected = getattr(self.core, "version_rejected", "")

        if rejected:
            self.set_state(STATE_ERROR, reason=rejected)
            return

        if self.core.auth_error:
            self.invalidate_auth(self.core.auth_error)
            self.core.auth_error = ""

        if not self.core.running and self.state == STATE_ONLINE:
            self.set_state(STATE_IDLE)

    async def delete_own_room(self, room=None):
        return await self._delete_via_connection(room or self.config.room)

    async def _delete_via_connection(self, room):
        from . import control_client

        try:
            return await control_client.delete_room(
                self.config.server_host,
                self.config.server_port,
                room,
                self.auth,
                self.config.client_id,
            )
        except control_client.ControlError as e:
            return False, str(e)

    def snapshot(self):
        data = super().snapshot()

        if self.core is not None:
            data.update(self.core.snapshot())

        data["state"] = self.state
        data["state_text"] = STATE_TEXT.get(self.state, self.state)

        return data
