"""
CraftLink -- direct (STUN) path coordinator.

============================================================================
  WHERE THIS SITS
============================================================================

    host_core / player_core          (unchanged: they speak Path)
              │
              │  relay tunnel opens exactly as it always did
              ▼
        DirectLink (this file)       <- v3 adds this
              │
     ┌────────┴────────┐
     │                 │
  RelayPath        UdpPath          (transport.py, unchanged)
  (already up)     (punched)

============================================================================
  THE ONE DESIGN DECISION THAT KEEPS v2 SAFE
============================================================================

The direct path is **added on top of a working relay tunnel, never instead of
one**. The relay tunnel comes up first, exactly as in v2 -- same frozen
handshake, same pairing, same byte forwarding -- and only then do we try to
punch. If the punch succeeds we hand the caller a UDP path; if it fails we
hand back nothing and the caller simply carries on with the relay it already
has.

Three consequences, and they are the whole reason for this shape:

  * **Relay behaviour cannot regress.** Nothing in the relay path is
    conditional on STUN mode. Turn the feature off and the code is v2.
  * **Fallback is free and instant.** "Fall back to the relay" is not an
    action we take; it is what is already happening. There is no window where
    the user is disconnected while we decide.
  * **A failed punch costs latency, not a connection.** The window is short
    and bounded (PUNCH_BUDGET_S), and game traffic is flowing the entire time.

============================================================================
  WHY THE PUNCH SOCKET IS ITS OWN SOCKET
============================================================================

The mapping belongs to the local port, so the socket that measures the NAT
must be the socket that punches -- and it must be the socket whose mapped
endpoint we advertise to the peer. A throwaway probe socket would measure a
different mapping and publish an address that means nothing.

That socket is opened once, kept for the session, and is also the socket the
inbound reachability probe answers on. See punch.probe_nat / probe_ingress.

Standard library only.
"""

import asyncio
import contextlib
import socket
import threading
import time

from .. import log as logmod
from . import protocol as proto
from . import punch
from .punch import (
    INBOUND_BLOCKED,
    INBOUND_OPEN,
    INBOUND_PROBE_TIMEOUT_S,
    INBOUND_PUNCHABLE,
    INBOUND_UNKNOWN,
)
from . import stun as stun_mod

#: How long we are willing to spend trying to upgrade to a direct path.
#:
#: Deliberately short. Game traffic is already flowing over the relay, so this
#: is an optimisation, not a connection attempt -- and a user staring at a
#: spinner is worse than a user on the relay. The ported project's own
#: analysis reached the same number for the same reason.
PUNCH_BUDGET_S = 8.0

#: How long to wait for the peer's candidate to arrive over the control plane.
PEER_CAND_TIMEOUT_S = 10.0

#: How long we cache our own NAT measurement. Re-measuring costs allocations,
#: and on a symmetric NAT every extra destination shifts the very mapping the
#: peer is aiming at -- so measure once and reuse.
NAT_CACHE_S = 30.0

#: 房间级 NAT 画像（RoomNatMonitor）多旧就不再拿来当"提示"用。
#: 它在房间模式是 STUN 时一直在测（NAT_RECHECK_S ≈ 90s），而打洞的第一次测量
#: 就发生在"点开始连接"那几秒里 —— 拿一份几分钟前的结论去替代现场测量是
#: 不负责任的，所以这里比 NAT_CACHE_S 更保守。
NAT_HINT_MAX_AGE_S = 30.0

#: 用房间画像做提示时，只在这一个 socket 上做**一次**端点探测。
#: 2 秒比正常 STUN 往返（几十 ms）宽很多，又远小于一次完整测量的 4~6 秒。
ENDPOINT_PROBE_TIMEOUT_S = 2.0

#: How long we hold off before the FIRST spray when there is no coordinated
#: start to wait for (his "grace"). An empty wait is the old behaviour: both
#: ends fire the moment their own candidate exchange finished, which is
#: precisely the case the sync exists to remove. 0.3s is enough for the
#: DIRECT_PUNCH we sent ourselves to come back on an ordinary link, so a peer
#: that does speak up still lands inside the window.
PUNCH_START_GRACE_S = 0.3

#: 等对端"起跑公告"的总上限（秒）。用户实测：一边早十几二十秒点时，1.5 秒
#: 的窗口几乎不可能和对面重叠 —— 要七八次才连上。这里给它足够长的时间去等，
#: 并且期间每秒重播我们的公告（见 `_wait_punch_start`）。
#: 上限存在的意义：对端一直不来不能永远不打。
PUNCH_SYNC_WAIT_TOTAL_S = 20.0

#: 等待期间每隔多久重播一次 `DIRECT_PUNCH startIn=...`（秒）。
PUNCH_REANNOUNCE_S = 1.0

#: How long to wait for the peer's DIRECT_PUNCH line once we have announced
#: ours. Short on purpose: the line was sent the moment the candidate arrived,
#: so a peer that is going to send one has already sent it. Waiting longer
#: would only eat the punch window.
PUNCH_SYNC_WAIT_S = 1.5

#: How often to send a keepalive on a punched link (his KEEPALIVE_S = 5.0).
#:
#: MUST be well under the shortest NAT UDP mapping timeout we can expect. If a
#: hole goes quiet for longer than the NAT's memory of it, the mapping is
#: dropped silently and every packet after that is discarded by the router --
#: with both clients still reporting a healthy link. Five seconds is his
#: number and it is the right one.
KEEPALIVE_S = 5.0

#: How much of the budget a LAN/global candidate may spend before we move on to
#: the public one. Deliberately small: a LAN address is either reachable in a
#: moment or not reachable at all, and the public punch is the one that needs
#: the window (the peer's port cursor is moving the whole time).
LOCAL_TRY_BUDGET_S = 1.5


def _parse_start_in(line):
    """
    Pull the start delay out of a DIRECT_PUNCH line. 0.0 when absent or junk.

    The line is `DIRECT_PUNCH startIn=1.50`, but it is parsed as key=value
    tokens rather than by position: the relay forwards these lines verbatim and
    new fields get appended, so a future line must not shift this one out of
    place.
    """
    field = proto.DIRECT_PUNCH_FIELD_START_IN

    for token in str(line).split()[1:]:
        key, _, value = token.partition("=")

        if key != field:
            continue

        try:
            return float(value)
        except ValueError:
            return 0.0

    return 0.0


class DirectLink:
    """
    Owns one punch socket and, at most, one successful direct path.

    Not a session: it is created per (room, peer) and thrown away when the
    tunnel it belongs to ends. All methods are called from the engine loop.
    """

    def __init__(self, room, host, port, log=None, my_id="", peer_id="",
                 is_host=False):
        #: The RoomSession whose control connection carries the candidate
        #: exchange. None is allowed -- the punch simply cannot be attempted.
        self.room = room
        self.host = host
        self.port = int(port)
        #: Port of the relay's built-in STUN responder, when it differs from the
        #: TCP port. None means "same as `port`", which is the common case in
        #: production (the relay answers STUN on 3478/3479 AND on its own port).
        self.stun_port = 0
        self.log = log or (lambda *_a: None)

        #: Identities used to break ties deterministically (`should_lead`) and
        #: to decide who must speak first (`must_open_first`). Both ends
        #: compute the same answer with no round trip.
        self.my_id = str(my_id or "")
        self.peer_id = str(peer_id or "")

        #: Am I the room's HOST? Only the host addresses its direct lines with
        #: `to=<peer>` (see `_to_field`): the host knows every guest's exact
        #: client id from the roster, while a guest can only guess the host's
        #: id -- and a wrong `to=` would route the line to the wrong person.
        self.is_host = bool(is_host)

        #: §F 的双端同步标识，由 `room_direct` 在每次开打前写入。
        #:
        #: 「这一轮」的编号（`batch`）和它的短标签（`att`，就是界面上的 `【A】`）
        #: 会**加法地**挂在 `DIRECT_CAND` / `DIRECT_PUNCH` 行的末尾。中继对这两
        #: 条行只做逐字转发，所以这里一个字节都不新增协议：老客户端不认识这两
        #: 个 token 就忽略，新客户端读到就能发现"两端轮次对不上"。
        #:
        #: 空 = 不挂（单测里的假 room / 老路径），行为与以前完全一致。
        self.flow_attempt = ""
        self.flow_batch = 0

        self.sock = None

        #: The socket the PUNCH actually succeeded on.
        #:
        #: ====================================================================
        #:   NOT NECESSARILY `self.sock` -- and that distinction was the bug
        #: ====================================================================
        #:
        #: `PunchArray.detach()` hands the winning socket to the caller and
        #: `array.close()` then closes everything still inside. On the roles
        #: where we open a socket array (s2c / birthday / sym_to_cone) the hit
        #: lands on an ARRAY socket, not on the published-port hub socket --
        #: the peer heard us from that array socket, so that is the mapping its
        #: NAT now admits.
        #:
        #: I was keeping only `self.sock` (the hub), so:
        #:   * the array closed the winning socket at the end of the punch;
        #:   * the keepalive kept warming the WRONG port;
        #:   * the session's reliable stream was built on the wrong socket.
        #:
        #: Result: the punch "succeeded", the card said 直连, and the game timed
        #: out -- exactly the report ("显示打通了，实际根本连不上一点").
        #: His design keeps the socket the peer was heard on and uses it as the
        #: tunnel; this is that.
        self.punched_sock = None

        #: True once a `direct_carry.PeerLink` has taken this hole over as a
        #: live tunnel. All the room asks of a link is "is it usable?", and the
        #: answer used to be "the keepalive thread is alive". That is no longer
        #: the whole answer: when the tunnel adopts the socket the ROOM's
        #: keepalive is stopped on purpose (its packets share the tunnel's MAGIC
        #: and would fake a handshake -- see `direct_carry.PeerLink`),
        #: and the tunnel's OWN keepalive (KEEPALIVE_S = 5.0) takes over.
        #:
        #: Without this flag `room_direct.peer_ready()` would read False the
        #: instant the tunnel took over, conclude the link had died, and
        #: RE-PUNCH -- which opens a NEW socket, i.e. a NEW NAT mapping, and
        #: throws away the very hole the game is using. That is the punch storm
        #: this file's history is a monument to.
        self.carried = False

        #: Our NAT measurement (stun.NatProfile) and when it was taken.
        self.nat = None
        self.nat_at = 0.0

        #: ==================================================================
        #:  房间级 NAT 画像的**提示**（审计问题 7：开局别测两遍）
        #: ==================================================================
        #:
        #: `RoomNatMonitor` 在 STUN 房间里一直测（它自己的 socket），而打洞的
        #: `DirectLink` 又在自己的 socket 上完整测一遍 —— 用户日志里能看到相隔
        #: 20ms 的两条 `[NAT] sym_easy_inc`，白白多花 4~6 秒。
        #:
        #: 这里放一份 (profile, 取用时刻)。**只**用它跳过"映射类型 + 步长"那部分
        #: 探测，并且只在它是确定的、够新的、且探测服务器集合一致时才用；
        #: 公网端点永远来自**打洞 socket 自己**的一次端点探测（映射属于端口，
        #: 拿别的 socket 的端口去公布等于公布一扇没人的门）。
        #:
        #: None / 过期 / 判不定 -> 完全走原来的完整测量，行为不变。
        self.nat_hint = None
        self.nat_hint_at = 0.0

        #: 量出 `public` 的那次测量问的是哪个 STUN 目的地 (host, port)。
        #: `_warm_relay_mapping` 用它判断"这个地址是不是已经发过包了"。
        self.stun_target = None
        self.peer_addr = None
        #: The peer's other endpoints (LAN / global IPv6), and ours. Both carry
        #: the SAME port as the public one, because the mapping belongs to the
        #: port -- see parse_local_candidates.
        self.peer_locals = []
        self.local_addrs = []
        #: The ordered attempt list, rebuilt from the peer's candidate. LAN
        #: first, then IPv6, then public (his `order_candidates`).
        self.candidates = []
        #: When we received the peer's candidate. The peer's published port is
        #: an ANCHOR that its NAT keeps moving (~73 ports/second measured on a
        #: real CGNAT), so its AGE is the single most useful number when a
        #: punch misses: an anchor 50s old is off by thousands of ports, and no
        #: window width fixes that. His `_anchor_age_text` exists for this.
        self.peer_cand_at = None
        #: The coordinated start. `punch_at` is the monotonic instant WE
        #: 这个链路已经等过几次"对端起跑公告"（第一次给长窗口，之后给短的）。
        self._sync_waits = 0

        #: announced we would start at, so a silent peer costs us only the
        #: grace period instead of the whole wait. See `_announce_punch`.
        self.punch_at = None
        self.peer_punch_at = None

        #: Set once a punch succeeds. The keepalive THREAD runs until the room
        #: ends, and it is the ONLY thing standing between "we punched" and "the
        #: hole silently died while the user was still setting up".
        self.keepalive_thread = None
        self.peer_nat = None
        self.peer_filter = None
        #: True when the PEER's DIRECT_CAND carried a `to=<id>` field.
        #:
        #: `to=` is host-only by protocol (`_to_field`), so this is a real,
        #: verbatim-forwarded signal that the peer holds the host slot. The
        #: member list uses it to label the host's row honestly instead of
        #: guessing; nothing in the punch depends on it.
        self.peer_is_host = False
        self.public = None
        self._closed = False

        #: The inbound verdict (his `_inbound_state`) and a human note for why.
        #: See probe_inbound.
        self.inbound_state = INBOUND_UNKNOWN
        self.inbound_note = ""

        #: Live phase text for the status card (his `status()`). The session or
        #: the room punch points this at its own setter; None means nobody is
        #: watching, and a status line must never be able to break a punch.
        self.step = None

    # --------------------------------------------------------
    # Inbound reachability (his inbound_probe)
    # --------------------------------------------------------

    async def probe_inbound(self, timeout=INBOUND_PROBE_TIMEOUT_S):
        """
        Can an UNRELATED host reach our punch socket? Ask the relay to prove it.

        ============================================================================
          WHY THIS EXISTS, AND WHY "unknown" WAS NOT GOOD ENOUGH
        ============================================================================

        Everything else we measure describes the OUTBOUND side: which public
        port our NAT gave us and how it allocates. None of it says whether a
        packet arriving from a host we have NEVER spoken to can get IN. That is
        a property of the FILTERING side of the NAT, and on many networks it is
        the difference between "the punch should work" and "nothing will ever
        arrive".

        His mechanism, ported: ask the server to fire one datagram at our
        published endpoint from a socket that has never spoken to us -- exactly
        what the peer's first punch packet looks like to our NAT -- and see
        whether it lands.

          * it lands                       -> OPEN. A failed punch is then OUR
                                             side to fix.
          * the relay confirms sending and  -> PUNCHABLE. The probe came from a
            nothing arrives                    host we never wrote to, so at
                                             worst the filtering is
                                             address-dependent -- which the
                                             punch itself handles the moment it
                                             starts sending.
          * the relay never confirms       -> UNKNOWN, and we say WHY. An older
                                             relay answers nothing, and
                                             reporting "blocked" for that blames
                                             the user's network for our own
                                             un-upgraded deployment. His source
                                             calls that exact mistake out by
                                             name.

        Returns the state string. Never raises: this is diagnosis, and a
        diagnostic that can break a working session is worse than none.
        """
        if self.room is None or self.sock is None or self.public is None:
            self.inbound_state = INBOUND_UNKNOWN
            self.inbound_note = "no public endpoint yet"
            return self.inbound_state

        # ====================================================================
        #  WARM THE MAPPING TOWARDS THE RELAY FIRST (his `_warm_server_mapping`)
        # ====================================================================
        #
        # This is the step I was missing, and it is the reason the row sat at
        # 未测 forever on his network while his own client measured it fine.
        #
        # The probe is sent BY THE RELAY. A NAT that filters per address only
        # admits packets from a host we have already sent to -- and the relay is
        # a host we speak TCP to but never UDP. So a probe aimed at the mapping
        # we opened towards a STUN server is dropped by design on every
        # restricted-cone and every symmetric NAT, and the miss was then
        # reported as "not measured".
        #
        # Sending a few datagrams AT the relay first opens (or reuses) a mapping
        # the relay is allowed to answer, so a probe that arrives actually means
        # something: a host we send to can reach us from a port we never wrote
        # to -- address-dependent filtering, which the punch handles fine.
        warmed = await self._warm_relay_mapping()

        ip, port = warmed or self.public
        line = f"{proto.ROLE_DIRECT_INGRESS} {ip} {port}"

        try:
            confirmed = await self.room.probe_ingress(line, timeout=timeout)
        except Exception as e:
            self.inbound_state = INBOUND_UNKNOWN
            self.inbound_note = f"probe could not be sent: {e}"
            self.log(f"[nat] inbound probe inconclusive: {self.inbound_note}")
            return self.inbound_state

        if confirmed is None:
            self.inbound_state = INBOUND_UNKNOWN
            self.inbound_note = "relay did not confirm the probe"
            self.log(
                "[nat] inbound probe inconclusive: the relay never confirmed "
                "sending it, so this tells us nothing. Upgrade the relay, or "
                "ignore this line."
            )
            return self.inbound_state

        if await self._await_ingress(timeout):
            # ================================================================
            #  "到了"到底证明了什么，取决于**我们有没有先暖过那个映射**
            # ================================================================
            #
            # 2026-09-25 按用户实测修正：他说参考工具（mao.fan/mynat、
            # 比特彗星）判他是 NAT3（端口受限），而朋友的客户端显示"入站宽松"。
            # 审计结论：**"到达"不等于全锥** ——
            #
            #   * 我们**暖过**映射（往中继那个 IP 发过包）才到达 → 证明的是
            #     "我们发过包的地址能回进来" = **地址受限(adf)**，不是 eif。
            #     地址受限的 NAT 也会放行，所以这一条推不出"宽松"。
            #   * 只有**没暖过**也能到达，才是"陌生主机能打进来" = eif。
            #     而我们每次探测都会先暖（否则在受限 NAT 上探针根本进不来，
            #     那才是之前一直"未测"的原因），所以这一条在我们这里几乎不会
            #     出现 —— eif 要留给"换 IP"那一项去证明。
            #
            # 朋友的客户端在这一点上是**乐观**的：它的备用暖场不会置
            # `_inbound_probe_warmed`，于是一个端口受限的 NAT 也会被读成
            # "open" → 宽松。我们不做这个推断。
            if warmed:
                self.inbound_state = INBOUND_PUNCHABLE
                self.inbound_note = (
                    "the probe reached the mapping we warmed towards the relay "
                    "(at most address-dependent)"
                )
                self.log(
                    f"[nat] inbound UDP works at {ip}:{port} on the mapping we "
                    f"warmed towards the relay: that is address-dependent "
                    f"filtering at worst (not proof of full cone), and the "
                    f"punch handles it"
                )
            else:
                self.inbound_state = INBOUND_OPEN
                self.inbound_note = "an unrelated host reached us"
                self.log(
                    f"[nat] inbound UDP works without any warm-up: an "
                    f"unrelated host reached us at {ip}:{port} (endpoint-"
                    f"independent filtering), so a failed punch is ours to fix"
                )
            return self.inbound_state

        # A miss RULES OUT endpoint-independent filtering and nothing else.
        #
        # It is NOT "blocked": the probe came from a host we had never written
        # to, and an address-dependent NAT (what every home router and every
        # CGNAT does) drops exactly that while admitting the peer perfectly well
        # once WE have sent first. Calling a miss "blocked" is what his own code
        # calls out by name as the old mistake.
        self.inbound_state = INBOUND_PUNCHABLE
        self.inbound_note = (
            "warmed probe did not arrive" if warmed else "probe did not arrive"
        )
        self.log(
            "[nat] the relay's probe did not arrive from a host we never wrote "
            "to; that rules out endpoint-independent filtering and nothing "
            "else -- an address-dependent NAT drops exactly that, and the "
            "punch itself handles it, so direct is still worth attempting"
        )
        return self.inbound_state

    async def _warm_relay_mapping(self):
        """
        Send a few datagrams at the relay and report our mapped endpoint there.

        Returns (ip, port) of the warmed mapping, or None when the relay does
        not answer STUN and the warm-up cannot be measured -- in which case the
        caller falls back to the published endpoint and the row may legitimately
        read "not measured".

        His `_warm_server_mapping`: the point is not the answer, it is the
        OUTBOUND packet, because that is what opens the mapping the relay is
        then allowed to send into. We still ask STUN so the log can name the
        port instead of guessing.

        ============================================================================
          审计问题 7：这里**不等** STUN 的答案
        ============================================================================
        原来这里 `await stun.measure(timeout=2.0)`，为了"能报出端口名"白等 2 秒
        （失败还要再等 0.6 秒的退避发送）—— 而入站探测本来就跑在候选公布之后、
        打洞之旁，代码自己写着"the punch itself handles it"。

        现在：**同步发出那几个数据报（0/0.05/0.1 秒间隔），立刻返回**。
        映射是那次 outbound 建立的，不等答案也一样成立；端口名由打洞那次的
        公网端点提供（`self.public`），不需要为了日志再测一遍。

        ============================================================================
          已经测过这个 STUN 目的地时，连那几个包都省掉
        ============================================================================
        暖场的意义是"让中继那个地址在放行表里"。如果 `stun_port` 指向的正是
        量出发布端点的那个 STUN 服务（生产里就是中继本身），我们早就往它发过包
        了 —— 再发一次纯属多余，而且会改变中继看到的入站结论。直接返回发布端点。
        """
        if self.sock is None:
            return None

        # 目的端口必须和原来**逐字一致**：`stun_port` 为 0（测试里直接构造
        # DirectLink 就是这种情况）时原来用的是 `DEFAULT_STUN_PORT`，**不是**
        # 中继的 TCP 端口。两个端口在测试环境里恰好不同，改错就会把"暖场"
        # 打到中继身上，从而改变中继看到的入站结论（`direct_cand_test` 抓到了）。
        target = (
            self.host,
            getattr(self, "stun_port", 0) or stun_mod.DEFAULT_STUN_PORT,
        )

        if self.public is not None and target == self.stun_target:
            # 这个目的地已经发过包（就是量出 `public` 的那次 STUN 请求）。
            return self.public

        with contextlib.suppress(Exception):
            loop = asyncio.get_running_loop()

            # 三发：一发就够开映射，多发两次是为了扛住单个 UDP 包的丢失。
            # 间隔很短（0.05s）而不是原来的 0.2s —— 这里只是"把包发出去"。
            for index in range(3):
                if index:
                    await asyncio.sleep(0.05)

                await loop.sock_sendto(self.sock, b"MCLINK-WARM", target)

        # 返回 None = "暖过，但没有单独量它"：调用方会退回 `self.public`
        # （那本来就是这次探测要打的端点）。
        #
        # 这里**刻意不去问 STUN 要端口名**。原来那句
        # `await stun.measure(timeout=2.0)` 只是为了日志好看，代价是 2 秒起
        # （失败还要再等退避发送 0.6 秒），审计问题 7 点名的就是它。
        #
        # 试过"返回已经量出来的 public"来省掉这一次测量，结果是错的：那句话
        # 会让 `probe_inbound` 认定这次探测是 **warmed** 的（`warmed` 非空），
        # 从而把"探针到达"读成「地址受限（PUNCHABLE）」而不是「宽松（OPEN）」——
        # 结论变了，不是省时间。`direct_cand_test` 抓到了这一点。
        return None

    async def _await_ingress(self, timeout):
        """
        Wait for the relay's probe to land on the punch socket.

        Deliberately its own read: the punch's normal reader is looking for
        MCLINK punch packets and would treat an ingress probe as noise. The
        probe carries its own magic, so a peer cannot fake it into meaning
        something.
        """
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + max(0.2, timeout)
        was_blocking = self.sock.getblocking()

        try:
            self.sock.setblocking(False)

            while time.monotonic() < deadline:
                left = max(0.01, deadline - time.monotonic())

                try:
                    data, _addr = await asyncio.wait_for(
                        loop.sock_recvfrom(self.sock, 2048), timeout=left
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    return False

                if data.startswith(punch.INGRESS_MAGIC):
                    return True
        except Exception:
            return False
        finally:
            with contextlib.suppress(OSError):
                self.sock.setblocking(was_blocking)

        return False

    # --------------------------------------------------------
    # Socket + measurement
    # --------------------------------------------------------

    def open_socket(self):
        """
        One long-lived punch socket.

        Bound to an ephemeral port with no SO_REUSEADDR on purpose: the port
        must be exclusively ours, or two clients on one machine could both
        "bind" the same number and silently steal each other's packets.
        """
        if self.sock is not None:
            return self.sock

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)

        # A punch reply is a single small datagram, but the peer may be
        # spraying; a default-sized buffer drops exactly the packet we want.
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)

        sock.bind(("0.0.0.0", 0))
        self.sock = sock

        return sock

    def _set_stun_target(self, profile):
        """
        记下"量出 public 的那次测量问的是哪台 STUN"。

        `_warm_relay_mapping` 用它判断"这个目的地是不是早就发过包"：是的话
        暖场那几个包纯属多余，而且会改变中继看到的入站结论。
        """
        for host, port, mapped in (getattr(profile, "servers", None) or []):
            if mapped:
                self.stun_target = (str(host), int(port))
                return

        self.stun_target = (
            self.host,
            int(self.stun_port or self.port),
        )

    async def measure(self, force=False):
        """
        NAT subtype + filtering + our public endpoint, on the punch socket.

        Cached: on a symmetric NAT every probe to a new destination consumes an
        allocation slot and moves the mapping the peer is trying to hit, so
        re-measuring for no reason actively makes the punch harder.

        ============================================================================
          审计问题 7：房间已经测过一遍的 NAT，不要在这里再完整测一遍
        ============================================================================
        用户日志里同一时刻有两条 `[NAT] sym_easy_inc (ports [...])`，相隔 20ms：
        `RoomNatMonitor`（它自己的 socket）和这里各测了一次，白白多花 4~6 秒。

        所以：当房间画像够新、分类确定、且投影出的探测服务器集合和这里要问的
        一致时，走一条**快路** ——
          * 映射类型 / 步长 / per-IP 池：复用房间画像是**为了分类**（NAT 行为
            对同一台机器是同一个事实，两个 socket 测的是同一个 NAT）；
          * 公网端点：仍然在本 socket 上做**一次**端点探测（映射属于端口）。
        映射探针那段"等第二个 IP"的宽限（SECOND_HOST_GRACE_S = 2.5s）也就
        不用付了。

        任何一条不满足就完全走原来的完整测量，行为与以前逐字节相同。
        """
        now = time.monotonic()

        if (
            not force
            and self.nat is not None
            and now - self.nat_at < NAT_CACHE_S
        ):
            return self.nat

        sock = self.open_socket()

        servers = stun_mod.default_servers(
            self.host, self.stun_port or self.port
        )

        if not force:
            fast = await self._measure_with_room_hint(sock, servers, now)

            if fast is not None:
                self.nat = fast
                self.nat_at = now
                self.public = fast.public
                self._set_stun_target(fast)
                return self.nat

        # Detection comes from the PORTED stack (stun.py = his implementation),
        # NOT from anything CraftLink wrote. His probes are the verified ones,
        # and the mapping they measure belongs to THIS socket -- which is the
        # only mapping the punch can use.
        #
        # The relay port is passed as the STUN port on purpose. In production
        # the built-in STUN service sits on 3478/3479 while the TCP relay is on
        # 19100, but they are NOT always the same number (a test relay, or an
        # operator who moved STUN). Probing a port nothing listens on yields no
        # public endpoint, which makes `candidate_line()` return None and the
        # punch log say "no public endpoint yet" -- which reads exactly like
        # "punching is broken" when the real cause is "we never measured".
        self.nat = await stun_mod.measure(sock, servers=servers, log=self.log)
        self.nat_at = now
        self.public = self.nat.public
        self._set_stun_target(self.nat)

        return self.nat

    def _room_profile_is_usable(self, servers):
        """
        房间画像能不能当"分类提示"用。四道门，缺一不可：

          1. **够新**（NAT_HINT_MAX_AGE_S）：几分钟前的结论不能替代现场测量；
          2. **分类确定**：`nat_type` 不是 unknown。unknown 意味着房间那次只拿到
             一个 STUN 应答，拿它去替代完整测量会把"可预测"降级成"乱猜"；
          3. **服务器集合一致**：分类是相对"问了哪些目的地"而言的，
             问的服务器不一样，步长就不可比；
          4. 投影端点有效。
        """
        profile = self.nat_hint

        if profile is None:
            return False

        if time.monotonic() - float(self.nat_hint_at or 0.0) > NAT_HINT_MAX_AGE_S:
            return False

        if getattr(profile, "public", None) is None:
            return False

        subtype = getattr(profile, "nat_type", None)

        if subtype is None or str(getattr(subtype, "value", subtype)) == "unknown":
            return False

        if getattr(profile, "filtering", None) is None:
            return False

        # 同一批服务器（顺序无关）。他那边探测表的顺序由 stun_order 定，但
        # "问了谁"这件事必须一致，否则步长是跨目的地的比较。
        asked_now = {tuple(item) for item in stun_mod._as_tuples(servers)}
        asked_then = {
            (host, int(port))
            for host, port, _mapped in (getattr(profile, "servers", None) or [])
        }

        if asked_then and not asked_then.issubset(asked_now):
            return False

        return True

    async def _measure_with_room_hint(self, sock, servers, now):
        """
        快路：复用房间画像的分类 + 在**本 socket** 上只探一次端点。

        返回 NatProfile，或 None 表示"这条快路不适用，请走完整测量"。
        任何异常都返回 None（测量失败都不该由快路负责）。
        """
        if not self._room_profile_is_usable(servers):
            return None

        # His probe blocks; the punch socket is used in non-blocking mode by the
        # event loop, so switch it for the duration exactly like
        # `stun.measure` does (including restoring it afterwards).
        previous = stun_mod._prepare(sock)

        try:
            got = await stun_mod.run_blocking(
                stun_mod.stun_probe2,
                sock,
                timeout=min(ENDPOINT_PROBE_TIMEOUT_S, stun_mod.RESPONSE_TIMEOUT),
                servers=stun_mod._as_tuples(servers),
                hub=None,
                want=1,
                log=self.log,
            )
        except Exception as e:
            self.log(
                f"[NAT] quick endpoint probe failed ({type(e).__name__}: {e}); "
                f"falling back to a full measurement"
            )
            return None
        finally:
            stun_mod._restore(sock, previous)

        if not got:
            self.log(
                "[NAT] quick endpoint probe got no answer on the punch socket; "
                "falling back to a full measurement"
            )
            return None

        ip, port = got[0], got[1]

        hint = self.nat_hint

        try:
            profile = stun_mod.NatProfile()
            profile.public = (ip, port)
            profile.nat_type = hint.nat_type
            profile.filtering = hint.filtering
            profile.coarse = getattr(hint, "coarse", None)
            profile.step = getattr(hint, "step", 0)
            profile.per_ip_pool = bool(getattr(hint, "per_ip_pool", False))
            profile.mapped_ports = [port]
            profile.servers = list(getattr(hint, "servers", None) or [])
            profile.asked = int(getattr(hint, "asked", 0) or 0)
            profile.pool_size = int(getattr(hint, "pool_size", 0) or 0)
            profile.allocation = None
        except Exception as e:
            self.log(
                f"[NAT] cannot reuse the room profile ({type(e).__name__}: {e}); "
                f"falling back to a full measurement"
            )
            return None

        try:
            profile.label = stun_mod.nat_label(
                profile.coarse,
                profile.nat_type,
                getattr(profile.filtering, "value", None),
            )
        except Exception:
            profile.label = ""

        self.log(
            f"[NAT] reused the room's NAT profile ({profile.label or 'measured'}) "
            f"and only probed the punch socket's endpoint {ip}:{port} "
            f"(saves a second full measurement)"
        )

        return profile

    # --------------------------------------------------------
    # Candidate exchange over the room control connection
    # --------------------------------------------------------

    def _local_candidates(self):
        """
        Our own LAN / global endpoints on the punch socket's port.

        The port is the PUNCH socket's, not a fresh one: a peer that dials our
        LAN address has to land on the socket that is actually listening and
        that owns the mapping we published. Handing out a different port would
        advertise a door with nothing behind it.
        """
        port = 0

        if self.sock is not None:
            with contextlib.suppress(OSError):
                port = self.sock.getsockname()[1]

        if not port:
            return []

        out = punch.local_endpoints(port)
        out += punch.ipv6_endpoints(port)

        return out

    def candidate_line(self):
        """
        The line we publish to the peer.

        Field names are stable and the relay forwards them verbatim, so new
        fields can be appended later without either side breaking. `filt` may
        be "unknown" and that is meaningful: it means "do not assume strict",
        which is the permissive default the ported project settled on.

        `local=` carries this host's LAN/global endpoints on the SAME port as
        the public one, because the mapping belongs to the port: a peer that
        can reach us directly on the LAN should not be sent out through the
        internet and back (hairpinning), and it should not have to punch at all.
        Addresses contain ':' so the list is space separated -- and it is ONE
        token per field, so an older parser that splits on spaces still reads
        the fields before it correctly.
        """
        if self.public is None:
            return None

        nat = self.nat

        line = (
            f"{proto.ROLE_DIRECT_CAND} {self.public[0]} {self.public[1]} "
            f"sub={nat.nat_type.value} filt={nat.filtering.value} "
            f"step={nat.step} pool={1 if nat.per_ip_pool else 0}"
            f"{self._flow_field()}"
            f"{self._to_field()}"
        )

        if self.local_addrs:
            # Comma separated: the addresses contain ':' (IPv6) so ':' cannot
            # be the separator, and a space would split the field into several
            # tokens that an older parser would read as unknown key=value junk.
            line += " local=" + ",".join(self.local_addrs)

        return line

    @staticmethod
    def parse_candidate(text):
        """
        Parse a DIRECT_CAND line into (addr, subtype, filtering).

        Kept as a 3-tuple because that is what the callers already unpack. The
        LAN/global candidates are read separately by `parse_candidates()`, so a
        new field cannot shift the meaning of the existing ones.
        """
        parts = text.split()

        if len(parts) < 3 or parts[0].upper() != proto.ROLE_DIRECT_CAND:
            return None, None, None

        try:
            addr = (parts[1], int(parts[2]))
        except (ValueError, IndexError):
            return None, None, None

        subtype = punch.NatSub.UNKNOWN
        filtering = punch.Filter.UNKNOWN

        for token in parts[3:]:
            key, _, value = token.partition("=")

            if key == "sub":
                with contextlib.suppress(ValueError):
                    subtype = punch.NatSub(value)
            elif key == "filt":
                with contextlib.suppress(ValueError):
                    filtering = punch.Filter(value)

        return addr, subtype, filtering

    @staticmethod
    def parse_local_candidates(text):
        """
        The `local=` list from a DIRECT_CAND line, as "ip:port" strings.

        Read separately from parse_candidate on purpose: this field is
        ADDITIVE, and a peer running an older build simply does not send it.
        An empty list means "no LAN/global candidates offered", which is a
        normal answer, not an error.
        """
        for token in str(text).split()[3:]:
            key, _, value = token.partition("=")

            if key == "local" and value:
                # Addresses contain ':' so they cannot be comma-separated
                # without ambiguity -- the whole field is one space-separated
                # list, and it is the LAST field on the line.
                return [
                    item for item in value.split(",") if item
                ]

        return []

    def _flow_field(self):
        """
        §F 的双端同步字段：` att=A batch=0`。没设过就是空串（老行为）。

        **追加在行末**（`local=` 之前、`to=` 之前），理由和握手行的 `ver=` 一样：
        按位置解析老字段的代码不会被新 token 挤歪。中继对 DIRECT_CAND /
        DIRECT_PUNCH 是逐字转发，不解析这两个 token。
        """
        if not self.flow_attempt:
            return ""

        return f" att={self.flow_attempt} batch={int(self.flow_batch)}"

    def _to_field(self):
        """
        The `to=<peer_id>` suffix, HOST ONLY.

        ============================================================================
          为什么必须加这个（第三人永远打不上洞的根因）
        ============================================================================

        The relay's old routing sends the host's direct lines to the EARLIEST
        joiner. With three people in the room that is always player 1 -- player
        2 never receives the host's candidate, so the punch can never sync and
        the third person sits at "正在打洞" forever (exactly the owner's
        complaint: "后面进来的直接冷落在一边").

        His server avoids this by addressing every message (`targetId` in
        `start_punch` / `peer_signal`). Ours is a line forwarder, so the field
        rides the line: the relay routes by `to=` and still parses nothing else.

        Guests send no `to=` on purpose: a guest cannot reliably know the
        host's client id (the relay broadcasts only the display name), and the
        old "guest -> host" routing is already exactly right for them. An old
        relay ignores the token, which keeps the two-player case working; an
        old client never sends it, which keeps the fallback routing.
        """
        if not self.is_host or not self.peer_id:
            return ""

        return f" {proto.DIRECT_FIELD_TO}={self.peer_id}"

    async def advertise(self):
        """
        Publish our candidate and wait for the peer's.

        Uses the room membership connection, which is already open and already
        authenticated -- no second channel, and nothing new to keep alive.

        WHO GOES FIRST: both sides just send and then wait. The relay forwards
        to the other member, so neither side can deadlock on "the peer has not
        started yet", and the wait is bounded.

        The socket is opened (and, if needed, measured) BEFORE publishing: the
        LAN candidates we advertise carry the PUNCH socket's port, so opening
        it later would advertise a port nothing is listening on.
        """
        if self.room is None:
            return None

        if self.nat is None:
            # NOT wrapped in suppress(): swallowing the failure here produced
            # the single worst symptom this feature can have -- no candidate
            # published, no punch, and a log that never says why. A measurement
            # failure has to be visible; the caller retries either way.
            try:
                await self.measure()
            except Exception as e:
                self.log(
                    f"[direct] NAT measurement failed, so there is no "
                    f"candidate to publish yet: {e!r}"
                )
                if callable(self.step):
                    with contextlib.suppress(Exception):
                        self.step("NAT 测量失败，正在重试")

                return None

        if self.sock is None:
            with contextlib.suppress(OSError):
                self.open_socket()

        self.local_addrs = self._local_candidates()

        line = self.candidate_line()

        if line is None:
            self.log("[direct] no public endpoint yet; cannot publish a candidate")
            return None

        try:
            await self.room.send_direct(line)
        except Exception as e:
            self.log(f"[direct] publishing candidate failed: {e}")
            return None

        self.log(
            f"[direct] candidate published: "
            f"{self.public[0]}:{self.public[1]} "
            f"local={self.local_addrs or 'none'}"
        )

        # The wire line itself, verbatim: the relay is a forwarder, so this is
        # the exact text the peer received. Anything derived from it is a
        # summary; this is the evidence.
        logmod.event("direct.cand.sent", line=line)

        peer_line = await self.room.wait_direct(
            (proto.ROLE_DIRECT_CAND,), timeout=PEER_CAND_TIMEOUT_S
        )

        if peer_line is None:
            self.log("[direct] no candidate received from the peer")
            return None

        addr, subtype, filtering = self.parse_candidate(peer_line)

        if addr is None:
            self.log(f"[direct] cannot parse peer candidate: {peer_line!r}")
            return None

        self.peer_addr = addr
        self.peer_cand_at = time.monotonic()
        self.peer_nat = subtype
        self.peer_filter = filtering

        self.log(
            f"[direct] peer candidate: {addr[0]}:{addr[1]} "
            f"subtype={subtype.value} filter={filtering.value}"
        )

        logmod.event(
            "direct.cand.recv",
            line=peer_line,
            addr=str(addr),
            subtype=subtype.value,
            filter=filtering.value,
        )

        # Remember the peer's other endpoints (LAN / global IPv6). The attempt
        # ORDER is built in attempt(), where our own freshly measured endpoint
        # list is known -- see order_candidates.
        self.peer_locals = self.parse_local_candidates(peer_line)

        # ============================================================================
        #  ★ `peer_is_host` 不再从 `to=` 推断（2026-09-28，设计基准 §D / 规则 6）
        # ============================================================================
        #
        # 以前这里靠"对端那行带了 `to=`"反推"它是主机" —— 那是**推断**，不是事实：
        # `to=` 的真实用途只有一个（Host 用它把行**路由**给某个 guest，见
        # `_to_field`），它既不证明身份、也会因为老/新客户端混用而失真。
        #
        # 现在"谁是当前 Host"只有一个来源：中继在 `HOST_STATE` / `OK JOIN` 上
        # 广播的 `host_id=`（docs/PROTOCOL.md §8.1/§8.2）。那个字段由
        # `RoomDirectLink.link_rows()` 直接和 `peers=` 做精确匹配，**不经过这里**。
        #
        # 这个属性保留成常量 False 只为兼容旧调用方（它不再被任何判断使用）。
        self.peer_is_host = False

        if self.peer_locals:
            self.log(
                f"[direct] peer also offered local candidates: "
                f"{self.peer_locals}"
            )

        # The candidate is out and the peer's is in, so from here on both ends
        # know where to aim. Announce WHEN to fire before doing anything else:
        # the announcement itself needs time to cross, and every millisecond of
        # that comes out of the grace period otherwise.
        await self._announce_punch()

        return addr

    # --------------------------------------------------------
    # Coordinated start (his `startIn` / `_await_punch_start`)
    # --------------------------------------------------------

    async def _announce_punch(self):
        """
        Publish the instant we intend to start spraying.

        ========================================================================
          WHY BOTH ENDS ANNOUNCE BEFORE THEY PUNCH
        ========================================================================

        A punch works when both ends are spraying over the SAME interval. Two
        ends that each start when their own candidate exchange happens to finish
        overlap for however long the two schedules accidentally agree on --
        which on a real pair is often not at all. The logs from the project
        owner show exactly that shape: one side sprayed 801 ports, the other had
        already stopped, and neither knew the other's window had passed.

        So each side states a DELAY, and both sides run their own clock from the
        moment the line reached them:

            my start   = arrival_of_your_line  + delay
            your start = arrival_of_my_line    + delay

        The two starts therefore differ only by the difference in the two
        one-way delays -- tens of milliseconds, not the seconds that separate
        two independent schedules. The delay is relative on purpose: an absolute
        timestamp would need our clock, the relay's and the peer's to agree, and
        a peer-to-peer link is the last place to assume that.

        We ALSO remember our own deadline, so a peer that never answers costs us
        the grace period rather than the whole wait.
        """
        if self.room is None:
            return

        delay = proto.PUNCH_START_DELAY_S

        line = (
            f"{proto.ROLE_DIRECT_PUNCH} "
            f"{proto.DIRECT_PUNCH_FIELD_START_IN}={delay:.2f}"
            f"{self._flow_field()}"
            f"{self._to_field()}"
        )

        try:
            sent = await self.room.send_direct(line)
        except Exception as e:
            self.log(f"[direct] announcing the punch start failed: {e}")
            return

        if not sent:
            return

        self.punch_at = time.monotonic() + delay

        self.log(
            f"[direct] punch start announced: in {delay:.1f}s "
            f"(peer should start within one RTT of the same instant)"
        )

        logmod.event(
            "direct.punch.announce",
            line=line,
            delay=round(delay, 3),
            start_at=round(self.punch_at, 3),
        )

    async def _wait_punch_start(self):
        """
        Wait for the peer's announcement and return OUR absolute start instant.

        Returns a monotonic deadline, or None when there is nothing to wait for
        at all. A peer that stays silent does NOT stall the punch: we fall back
        to our own announced instant, and if even that is missing, to a short
        grace period. Being late to a window that has passed is worth less than
        starting late by a known amount.
        """
        raw = None

        # ==================================================================
        #  ★ 「等待同步起降」：等对端**真的准备好**，而不是等 1.5 秒就自己开打
        # ==================================================================
        #
        # 用户 2026-09-25 的实测（两份客户端日志）：第一次测两三次就通了，重启
        # 之后一边早点了十几二十秒，结果前几次全是
        # `MISS ... reason=no packet arrived`，要七八次才连上。
        #
        # 根因就是这里只等 `PUNCH_SYNC_WAIT_S`（1.5 秒）：对端还没进来的时候
        # 我们已经在打自己的了；等对端进来时，我们又在下一轮 7 秒的测量里 ——
        # 两边的窗口几乎不重叠，只能靠运气撞上一次。
        #
        # 所以改成：**在对端出现之前一直等**（有上限），并且每秒重新广播一次
        # 我们的 `startIn` —— 这样后点的那一边一进房就能立刻拿到我们的时刻，
        # 两边的起跑线就对齐了（他的 `startIn` 语义本来就是"从收到那一刻算起"，
        # 所以重播不会把已经对齐的时刻弄乱）。
        #
        # 上限用 `PUNCH_SYNC_WAIT_TOTAL_S`：对端一直不来也不能永远不打 ——
        # 到了就退回"自己的宽限"，和以前一样。
        # 没有房间连接时（单测里的假链路）也要有值：下面那句日志会用到。
        patience = PUNCH_SYNC_WAIT_S

        def _say(text):
            """状态栏 + 日志都写一句（用户要"看得到在等谁"）。"""
            if callable(self.step):
                with contextlib.suppress(Exception):
                    self.step(text)
            with contextlib.suppress(Exception):
                self.log(f"[direct] {text}")

        # ==================================================================
        #  ★ 「等待同步起降」：等对端**真的准备好**，而不是等 1.5 秒就自己开打
        # ==================================================================
        #
        # 用户 2026-09-25 的实测（两份客户端日志）：第一次测两三次就通了，重启
        # 之后一边早点了十几二十秒，结果前几次全是
        # `MISS ... reason=no packet arrived`，要七八次才连上。
        #
        # 根因就是这里只等 `PUNCH_SYNC_WAIT_S`（1.5 秒）：对端还没进来的时候
        # 我们已经在打自己的了；等对端进来时，我们又在下一轮 7 秒的测量里 ——
        # 两边的窗口几乎不重叠，只能靠运气撞上一次。
        #
        # 所以：**在对端出现之前一直等**（有上限），期间每秒重播一次我们的
        # `startIn`；后点的那一边一进房就能立刻拿到我们的时刻，两边对齐。
        #
        # 上限：第一次给 `PUNCH_SYNC_WAIT_TOTAL_S`（20s，兜住"晚十几二十秒"），
        # 之后每次只给短窗口（那时对端已经在广播了）—— 这样"对端是坏的"也不会
        # 把重试周期拖成 20 秒一轮。
        if self.room is not None:
            patience = (
                PUNCH_SYNC_WAIT_TOTAL_S
                if self._sync_waits == 0
                else PUNCH_SYNC_WAIT_S
            )
            self._sync_waits += 1

            deadline = time.monotonic() + patience
            announced_at = 0.0

            _say(
                f"等待同步起降：等 {self.peer_id or '对端'} 的起跑时刻"
                f"（最多 {patience:.0f}s，每 {PUNCH_REANNOUNCE_S:.0f}s 重播我们的）"
            )

            while raw is None:
                # 重播放在每轮开头：无论是"没收到"还是"收到一条过期的"，
                # 都保证我们的时刻一直在对外广播。
                now = time.monotonic()

                if now - announced_at >= PUNCH_REANNOUNCE_S:
                    announced_at = now
                    with contextlib.suppress(Exception):
                        await self._announce_punch()

                if now >= deadline:
                    break

                left = max(0.1, deadline - now)

                with contextlib.suppress(Exception):
                    raw = await self.room.wait_direct(
                        (proto.ROLE_DIRECT_PUNCH,),
                        timeout=min(PUNCH_SYNC_WAIT_S, left),
                    )

                if raw is None:
                    continue

                # Anchored to ARRIVAL, never to "now + delay": by the time the
                # punch loop reaches this line it may have spent seconds
                # elsewhere, and that cost is not shared with the peer.
                arrived = time.monotonic()
                delay = _parse_start_in(raw)

                if delay <= 0:
                    self.log(
                        f"[direct] peer announced a punch with no usable delay "
                        f"({raw!r}); waiting for a fresh one"
                    )
                    raw = None
                    continue

                candidate = arrived + min(delay, proto.PUNCH_START_MAX_WAIT_S)

                # ==========================================================
                #  陈旧的公告不作数
                # ==========================================================
                #
                # 队列里可能留着**上一轮**的 `DIRECT_PUNCH`（我们不再无脑清
                # 队列，因为它同时是"对端准备好了"的凭证）。但如果它的时刻已经
                # 过去，说明对端上一轮就打完了 —— 拿它当真只会让我们"立刻自己
                # 开打"，又回到两边窗口不重叠的老问题。所以继续等一条新的。
                if candidate < time.monotonic() - 1.0:
                    self.log(
                        "[direct] ignoring a stale punch announcement "
                        f"({raw!r}); waiting for a fresh one"
                    )
                    raw = None
                    continue

                self.peer_punch_at = candidate
                _say(
                    f"已同步：和 {self.peer_id or '对端'} 同时开打"
                    f"（{max(0.0, candidate - time.monotonic()):.1f}s 后）"
                )
                self.log(
                    f"[direct] peer announced its punch start: "
                    f"in {delay:.1f}s from arrival, so we start in "
                    f"{max(0.0, candidate - arrived):.1f}s"
                )

                logmod.event(
                    "direct.punch.recv",
                    line=raw,
                    delay=round(delay, 3),
                    start_at=round(candidate, 3),
                )

                return candidate

            self.log(
                f"[direct] peer never announced a punch start within "
                f"{patience:.0f}s "
                f"(we re-announced every {PUNCH_REANNOUNCE_S:.0f}s); using "
                f"{self._grace_text()}"
            )

        if self.punch_at is not None:
            return self.punch_at

        return time.monotonic() + PUNCH_START_GRACE_S

    def _grace_text(self):
        """How the fallback start reads in the log -- one place, not three."""
        if self.punch_at is None:
            return f"a {PUNCH_START_GRACE_S:.1f}s grace period"

        remaining = max(0.0, self.punch_at - time.monotonic())

        return f"our own announcement ({remaining:.1f}s left of it)"

    # --------------------------------------------------------
    # The punch
    # --------------------------------------------------------

    async def attempt(self, timeout=PUNCH_BUDGET_S, per_ip_pool=False):
        """
        Run the punch. Returns a PunchResult.

        Runs on the engine loop like everything else. The array is bounded
        (punch.SOCKETS_FOR_OPENP2P) and the whole thing is inside `timeout`, so
        a hostile or silent peer can only cost us that budget -- never the
        connection, which is on the relay the whole time.
        """
        if self.sock is None:
            self.open_socket()

        if self.peer_addr is None:
            return punch.PunchResult(
                plan=punch.PLAN_NONE, reason="no peer candidate"
            )

        if self.nat is None:
            await self.measure()

        my_sub = (
            self.nat.nat_type if self.nat is not None else punch.NatSub.UNKNOWN
        )
        peer_sub = self.peer_nat or punch.NatSub.UNKNOWN

        my_filter = (
            self.nat.filtering if self.nat is not None else punch.Filter.UNKNOWN
        )

        plan = punch.punch_plan(
            my_sub, peer_sub, my_filter, self.peer_filter
        )

        self.log(
            f"[direct] plan={plan} me={my_sub.value}/{my_filter.value} "
            f"peer={peer_sub.value}/"
            f"{(self.peer_filter or punch.Filter.UNKNOWN).value}"
        )

        if plan == punch.PLAN_NONE:
            # Saying so up front is the correct answer, not a failure: two
            # unpredictable symmetric NATs with strict filtering have no
            # mechanism left, and burning the budget proves nothing.
            self.log(
                "[direct] no viable punching mechanism for this pair "
                "(NAT combination is hopeless; relay is the correct answer)"
            )
            return punch.PunchResult(
                plan=punch.PLAN_NONE, reason="no mechanism for this pair"
            )

        started = time.monotonic()

        # Nothing from an earlier round may leak into this one. See
        # RoomSession.drain_direct: a stale `startIn` reads as "start now",
        # which is the opposite of what this round is trying to do.
        if self.room is not None:
            with contextlib.suppress(Exception):
                dropped = await self.room.drain_direct()

                if dropped:
                    self.log(
                        f"[direct] dropped {dropped} stale direct line(s) "
                        f"from an earlier round"
                    )

        # When do both ends start spraying? Ask the peer (see _wait_punch_start)
        # and pass the answer down into the punch, which holds every socket
        # until that instant. This is the difference between "two schedules that
        # happen to overlap" and "one window with both ends inside it".
        start_at = await self._wait_punch_start()

        # How stale is the port the peer told us about? Passed down so a miss
        # can say "anchor measured 48s ago" instead of just "no packet
        # arrived" -- which points at the peer for what is usually OUR stale
        # measurement.
        anchor_age = (
            time.monotonic() - self.peer_cand_at
            if self.peer_cand_at is not None
            else None
        )

        # ====================================================================
        #  WHICH ADDRESS? LAN first, IPv6 next, public last (his order)
        # ====================================================================
        #
        # Two players in the same dorm should reach each other directly, not go
        # out to a global address and back -- that needs hairpinning on the
        # router (widely unsupported) and it burns the relay for no reason.
        #
        # The order is rebuilt HERE rather than at advertise time because our
        # own endpoint list is only trustworthy once the punch socket exists,
        # and every candidate is still ATTEMPTED: a candidate that looks wrong
        # on paper can be the reachable one.
        self.local_addrs = self._local_candidates()
        self.candidates = punch.order_candidates(
            self.peer_addr, self.peer_locals, self.local_addrs, log=self.log
        )

        tried = []
        lan_candidates = []
        result = None

        for index, (ip, port) in enumerate(self.candidates):
            # The punch array is IPv4 (PunchArray binds AF_INET), so a global
            # v6 address cannot be used yet. Said out loud rather than silently
            # dropped: "it should have worked over v6" is a real question and
            # the log has to be able to answer it.
            if punch.is_ipv6(ip):
                self.log(
                    f"[direct] skipping IPv6 candidate {ip}:{port}: the punch "
                    f"array is IPv4-only in this build"
                )
                continue

            tried.append((ip, port))

            last = index == len(self.candidates) - 1

            # 同网段候选记下来：它们只拿到 1.5 秒的"先看一眼"预算，见下面
            # 二轮补打。`is_lan` 也在这里算，一轮和二轮用的是同一个判断。
            is_lan = punch.is_lan_candidate(ip, self.local_addrs)

            if is_lan:
                lan_candidates.append((ip, port))
            # The public endpoint gets the whole budget; the cheap local ones
            # get a short look so a wrong LAN guess cannot eat the window the
            # public punch needs.
            budget = timeout if last else min(timeout, LOCAL_TRY_BUDGET_S)

            if len(self.candidates) > 1:
                self.log(
                    f"[direct] trying {ip}:{port} "
                    f"({'public' if last else 'local'}, {budget:.1f}s)"
                )

            # 同网段候选走"直接握手"，不套 NAT 分类（见 run_punch 里的说明）。
            result = await punch.run_punch(
                ip, port,
                my_sub, peer_sub,
                hub_sock=self.sock,
                log=self.log,
                timeout=budget,
                my_filter=my_filter,
                peer_filter=self.peer_filter,
                per_ip_pool=per_ip_pool,
                anchor_age_s=anchor_age,
                my_id=self.my_id,
                peer_id=self.peer_id,
                start_at=start_at,
                lan=is_lan,
                step=self.step,
            )

            if result.ok:
                break

        # ====================================================================
        #  二轮：给同网段的候选一次**完整预算**的机会
        # ====================================================================
        #
        # 用户 2026-09-25 问的："万一有些网络环境比较差，1.5 秒没应答上，
        # 但其实确实是一个局域网，会不会被误判到公网去打？"
        #
        # 会 —— 如果只打一轮的话。第一轮里 LAN 候选只拿 1.5 秒，那是为了
        # "一个猜错的 LAN 候选不许吃掉公网打洞的窗口"；但无线环境差、机器
        # 卡一下的时候，握手可能真的没在 1.5 秒内完成。而公网那条路对真正的
        # 邻居往往是**走不通**的（路由器基本不做 hairpinning），于是"局域网
        # 明明能通"却整个失败。
        #
        # 所以：只有当**一条都没成**的时候，把同网段的候选再打一遍，这次给
        # 完整预算。代价只在"本来就要失败"的那条路上才付出，正常情况一秒都
        # 不多花；换来的是"真邻居永远不会因为手慢被判死"。
        if not result.ok and lan_candidates:
            # 日志正文一律英文（见 gui_i18n_test：日志是分析材料，不跟界面
            # 语言走）。说明留在上面那段中文注释里。
            self.log(
                f"[direct] first pass missed; retrying the same-subnet "
                f"candidate(s) with the full budget "
                f"({', '.join(f'{ip}:{port}' for ip, port in lan_candidates)}, "
                f"{timeout:.0f}s)"
            )

            for ip, port in lan_candidates:
                result = await punch.run_punch(
                    ip, port,
                    my_sub, peer_sub,
                    hub_sock=self.sock,
                    log=self.log,
                    timeout=timeout,
                    my_filter=my_filter,
                    peer_filter=self.peer_filter,
                    per_ip_pool=per_ip_pool,
                    anchor_age_s=anchor_age,
                    my_id=self.my_id,
                    peer_id=self.peer_id,
                    start_at=start_at,
                    lan=True,
                    step=self.step,
                )

                if result.ok:
                    self.log(
                        f"[direct] same-subnet candidate punched on the "
                        f"second pass: {ip}:{port} (the 1.5s first look was "
                        f"not enough, but the path really is open)"
                    )
                    break

        if result is None:
            # Nothing was even attemptable (every candidate was IPv6, or the
            # list came back empty). Worth distinguishing from "we tried and
            # missed": the two have completely different next steps.
            self.log(
                "[direct] nothing to attempt: no usable IPv4 candidate "
                f"(candidates={self.candidates or 'none'})"
            )
            result = punch.PunchResult(
                plan=plan, reason="no usable candidate to attempt"
            )

        if result.ok:
            # Keep the socket the hit landed on (see `punched_sock`). `detach`
            # already removed it from the array, so `array.close()` did not
            # close it -- we just have to hold the reference.
            self.punched_sock = result.sock or self.sock

            if self.punched_sock is not self.sock:
                self.log(
                    "[direct] the hit landed on an array socket, not the "
                    "published one -- keeping THAT socket for the session "
                    "(its mapping is the one the peer's NAT admits)"
                )

        result.elapsed = time.monotonic() - started

        if result.ok:
            self.log(
                f"[direct] PUNCHED: peer={result.addr[0]}:{result.addr[1]} "
                f"plan={result.plan} sent={result.sent} "
                f"elapsed={result.elapsed:.1f}s "
                f"candidates_tried={len(tried)}"
            )
        else:
            self.log(
                f"[direct] MISS: plan={result.plan} sent={result.sent} "
                f"learned={len(result.learned)} "
                f"reason={result.reason or 'no packet arrived'}"
            )

        return result

    # --------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------

    # --------------------------------------------------------
    # Keepalive: a punched hole is not a connection
    # --------------------------------------------------------

    @property
    def keepalive_running(self):
        """
        Is this hole still usable?

        Two ways to be usable, and both count:

          * the ROOM's keepalive thread is alive (no tunnel yet -- the hole is
            being held open for a game that has not connected);
          * a live tunnel has adopted the socket (`carried`). In that case the
            room keepalive is stopped deliberately (see `carried` above) and the
            tunnel's own keepalive maintains the mapping.
        """
        if getattr(self, "carried", False):
            return True

        thread = getattr(self, "keepalive_thread", None)

        return thread is not None and thread.is_alive()

    def start_keepalive(self, interval=KEEPALIVE_S):
        """
        Hold the NAT mapping open until `stop_keepalive()`.

        ========================================================================
          WHY THIS EXISTS (and why its absence was a real bug)
        ========================================================================

        His `udptunnel.py`, line 19, states it as a requirement of the design:

            "keepalive every few seconds, otherwise the mapping expires and the
             hole closes"

        My first version punched at room-join and then relied on a 120-second
        "freshness" window with no keepalive at all. On the owner's network that
        produced the worst possible outcome: the punch succeeded, the user went
        to open their world, and two minutes later the client announced
        "已打通但通道已过期" and handed the game to the relay -- while the relay
        was never supposed to carry the traffic in STUN mode at all.

        A mapping that is kept warm does not expire. So the keepalive is not an
        optimisation, it is what makes "punch on join, play later" correct --
        and "play later" is the whole requirement: this tool is not tied to any
        game process, so the gap between joining and playing is unbounded.
        """
        if self.peer_addr is None or self.sock is None:
            return

        if self.keepalive_running:
            return

        # A PLAIN THREAD, not an asyncio task. Two reasons, both learned the
        # hard way from the owner's log:
        #
        #   1. A task's lifetime is entangled with whatever task created it. The
        #      punch runs inside a session's task tree, and when that session is
        #      torn down the keepalive was cancelled as collateral damage --
        #      even wrapped in `asyncio.shield`. The link then reported itself
        #      as not-ready while its socket was still perfectly good, and the
        #      next Minecraft connection waited forever for a link that was
        #      right there.
        #   2. An idle-ish timer belongs on a thread. There is nothing to await,
        #      and the mapping must survive every session, every reconnect and
        #      every cancelled coroutine.
        #
        # `daemon=True`: a keepalive must never hold the process open.
        self.keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            args=(interval,),
            name=f"mclink-keepalive-{id(self) & 0xFFFF}",
            daemon=True,
        )
        self.keepalive_thread.start()

    def stop_keepalive(self):
        """Signal the keepalive thread to stop. Safe to call from anywhere."""
        self.keepalive_thread = None

    def _keepalive_loop(self, interval):
        """
        Send a keepalive every `interval` until stopped.

        ========================================================================
          A PUNCHED HOLE IS NOT A CONNECTION
        ========================================================================

        His `udptunnel.py`, line 19, states the requirement:

            "keepalive every few seconds, otherwise the mapping expires and the
             hole closes"

        Nothing tells either side when it happens. The peer simply stops
        receiving, both clients still say "connected", and the only cure is to
        keep the mapping warm in the first place.

        ========================================================================
          SEND ONLY. NEVER READ THIS SOCKET HERE.
        ========================================================================

        My first version also drained the socket, on the theory that a full
        receive buffer would make the kernel drop game packets. It does the
        opposite of helping: TWO readers on one socket RACE, and the keepalive
        thread won often enough to swallow the handshake datagrams. The symptom
        was a stream that timed out on BOTH ends while the punch was fine
        ("可靠流握手超时（对端没有响应）" on one side, silence on the other).

        The rule that comes out of it, and it is the same rule the rest of this
        file follows:

            whoever carries the game OWNS the reads; the keepalive only writes.

        Refreshing a NAT mapping is an OUTBOUND act -- that is the whole
        mechanism -- so sending is all this thread needs to do.
        """
        ping = punch.pack_ping()
        addr = self.peer_addr

        while not self._closed and self.keepalive_thread is not None:
            with contextlib.suppress(OSError):
                self.sock.sendto(ping, addr)

            # Sleep in small slices so stop_keepalive() takes effect promptly.
            slept = 0.0

            while slept < interval:
                if self._closed or self.keepalive_thread is None:
                    return

                time.sleep(0.25)
                slept += 0.25

    @property
    def closed(self):
        return self._closed

    def close(self):
        self._closed = True

        # Stop the keepalive -- but NEVER cancel it from inside itself.
        #
        # `_keepalive_loop` calls `close()` when its socket dies, and
        # `stop_keepalive()` cancels its own task in that case: the task is then
        # cancelled at its next await, which tears the link down while the rest
        # of the client still believes it is up. (A test caught exactly that:
        # the punch succeeded and `ready` was still False.)
        # The keepalive runs on its own thread now, so there is no
        # "cancelling myself" hazard: just signal it.
        self.stop_keepalive()

        # Close BOTH sockets, and the punched one matters most.
        #
        # `PunchArray.detach()` handed the winning socket to us and then closed
        # everything still inside the array -- so `punched_sock` has no other
        # owner. Nothing used to close it, which leaked one UDP port per
        # successful punch for the life of the process, and (now that a
        # `direct_carry.PeerLink` reads it through a `UdpHub`) also left that
        # hub's reader thread parked in a blocking `recvfrom` forever.
        #
        # Closing it is what wakes that thread: `recvfrom` raises OSError and
        # his loop breaks on OSError. Order matters in `room_direct.close()` --
        # the carriers are torn down first, so nobody is mid-send here.
        for target in (self.punched_sock, self.sock):
            if target is not None:
                with contextlib.suppress(OSError):
                    target.close()

        self.punched_sock = None
        self.sock = None


def make_udp_path(sock, peer_addr):
    """
    DEPRECATED -- kept only because a regression test still imports it.

    It used to wrap the punched socket as a `transport.UdpPath` for the
    self-written reliable stream to ride. That stream is gone (see
    `direct_carry.py`'s header): the direct path now rides the reference
    project's `udptunnel.UdpTunnel`, which owns its socket through a `UdpHub`.
    Nothing in the live path calls this.
    """
    from .transport import UdpPath

    return UdpPath(sock, peer_addr)


__all__ = [
    "DirectLink",
    "make_udp_path",
    "PUNCH_BUDGET_S",
    "PEER_CAND_TIMEOUT_S",
    "NAT_CACHE_S",
    "PUNCH_START_GRACE_S",
    "PUNCH_SYNC_WAIT_S",
    "LOCAL_TRY_BUDGET_S",
]
