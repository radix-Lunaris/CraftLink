"""
CraftLink -- UDP hole punching (direct path).

============================================================================
  WHAT THIS IS, AND WHERE IT COMES FROM
============================================================================

This is a port of the punching core from **MCLanP2P** (a friend's project that
got NAT3 <-> NAT4 and NAT4 <-> NAT4 working on real networks). Only the direct
connection logic is ported; the relay, the auto scheduler and the desktop
shell are deliberately NOT.

The three ideas that make it work -- all of them learned the hard way, from
real logs, and all of them contrary to the obvious implementation:

  **1. THE SYMMETRIC SIDE OPENS TARGETS; IT DOES NOT AIM.**

     A cone NAT's mapping never moves, so there is nothing to guess: one
     packet to its published port is enough. The symmetric side's job is to
     create as many mappings as it can and let the other side shoot at them.
     The ported project spent a long time aiming the WRONG WAY: it had the
     symmetric side scanning thousands of ports at the cone side's port that
     never moves. 8100 guesses, zero replies.

  **2. LEARNING BEATS PREDICTION.**

     A packet that ARRIVES proves the peer's NAT created that mapping and is
     willing to talk to us. No prediction can beat an address we have already
     been reached from. So every arriving packet is recorded -- together with
     WHICH socket it arrived on, because on a per-destination-filtering NAT
     only a reply from that same socket can get back out.

  **3. DO NOT TALK YOURSELF OUT OF TRYING.**

     An inbound probe that does not arrive is the NORMAL case on an
     address+port-filtering NAT, not evidence that the network is blocked.
     The ported project used to conclude "blocked, no amount of punching can
     help" before sending a single packet. Most of its later failures were
     this, not the NAT.

============================================================================
  WHY THIS FILE IS ASYNC (and the ported one is not)
============================================================================

The original runs the array on a selector thread. CraftLink is asyncio end to
end -- the engine owns ONE event loop and everything rides on it -- so a
thread-based port would put punch packets on a second loop and reintroduce
exactly the cross-thread problem the transport abstraction exists to avoid.

`SOCKETS = 250` sockets on one asyncio loop is fine: the original project's
own analysis notes that binding ~250 sockets is the cost driver (hundreds of
milliseconds on Windows), not the packet rate. We bind once, then only send.

Standard library only.
"""

import asyncio
import contextlib
import enum
import os
import random
import socket
import struct
import time

from . import stun as stun_mod

# ============================================================
# Wire format
# ============================================================
#
# Deliberately identical to the ported project's `udptunnel` header, so the
# two implementations could interoperate during debugging. 12 bytes:
#
#     MAGIC(2) TYPE(1) FLAGS(1) STREAM_ID(4) SEQ(4)
#
# HELLO / HELLO_ACK leave the last eight bytes zero -- they are punch probes,
# not tunnel data.

MAGIC = 0x4D50          # 'MP'
T_HELLO = 1
T_HELLO_ACK = 2

#: Keepalive. His `T_PING` (udptunnel.py), and its whole reason for existing:
#:
#:     "keepalive every few seconds, otherwise the mapping expires and the
#:      hole closes"
#:
#: A punched UDP hole is NOT a connection: the NAT keeps the mapping only while
#: it sees traffic, and silently forgets it after a while (30-120s on common
#: routers). Nothing tells either side; the peer simply stops receiving.
#:
#: This is why "已打通但通道已经过期" must never exist as a screen: a link that
#: is kept alive does not expire, and a link that is NOT kept alive was never
#: ours to hand to a game.
T_PING = 3
T_DATA = 3

HDR = 12
HEADER = struct.Struct(">HBBII")


def pack_hello():
    return HEADER.pack(MAGIC, T_HELLO, 0, 0, 0)


def pack_hello_ack():
    return HEADER.pack(MAGIC, T_HELLO_ACK, 0, 0, 0)


def pack_ping():
    """One keepalive datagram (his `_build(T_PING, ...)`)."""
    return HEADER.pack(MAGIC, T_PING, 0, 0, 0)


def is_our_packet(data):
    return len(data) >= HDR and struct.unpack_from(">H", data, 0)[0] == MAGIC


def packet_type(data):
    if not is_our_packet(data):
        return None
    return data[2]


# ============================================================
# NAT classification (mapping behaviour)
# ============================================================

class NatSub(enum.Enum):
    """Mapping behaviour. Matches the ported project's vocabulary."""

    CONE = "cone"
    EASY_INC = "sym_easy_inc"
    EASY_DEC = "sym_easy_dec"
    HARD = "sym_hard"
    UNKNOWN = "unknown"



    # ---- labels (HIS text, so the GUI keeps his wording) ----
    #
    # These call into stun.NAT_SUB_LABEL / NAT_FILTER_LABEL, which are his
    # strings ("对称·递增（可预测）" etc.), because the user-facing wording is
    # part of what was ported. Keeping the property here means the diagnostics
    # card did not have to change its rendering code at all.

    @property
    def label(self):
        from . import stun as _stun

        return _stun.NAT_SUB_LABEL.get(self.value, self.value)

    @property
    def advice(self):
        """
        One sentence telling the user what this result MEANS.

        Ours, not his: his project shows the verdict in the room list
        (`nat_label`) but has no place for advice text. The wording follows
        his framing, which is the part that matters -- a symmetric NAT is not
        automatically hopeless, only an UNPREDICTABLE one is.
        """
        return NAT_ADVICE.get(
            self,
            "先做一次网络诊断；判不出通常说明 UDP 被拦了。",
        )


class Filter(enum.Enum):
    """Filtering behaviour (RFC 5780). Independent of the mapping axis."""

    EIF = "eif"        # endpoint independent: anybody may reach the mapping
    ADF = "adf"        # address dependent: only hosts we wrote to
    #: ★ 用户 2026-10-01：换端口到了、换 IP 根本测不了（单 IP 的 STUN）——
    #: 只能说"最多地址受限"，不能说"就是地址受限"。档位顺序与 ADF 相同
    #: （`FILTER_RANK`），所以打洞计划的选择逻辑完全不受影响。
    ADF_OR_EIF = "adf_or_eif"
    APDF = "apdf"      # address+port dependent: only the exact endpoint
    UNKNOWN = "unknown"



    @property
    def label(self):
        from . import stun as _stun

        return _stun.NAT_FILTER_LABEL.get(self.value, self.value)

#: What each verdict means for the user. Keyed by the enum, so it cannot
#: drift out of sync with the values themselves.
NAT_ADVICE = {
    NatSub.CONE: "最优。映射对所有目的地一致，打洞几乎必成。",
    NatSub.EASY_INC: "对称但端口递增可预测，可以用端口窗口打洞，成功率取决于规律稳定性。",
    NatSub.EASY_DEC: "对称但端口递减可预测，可以用端口窗口打洞，成功率取决于规律稳定性。",
    NatSub.HARD: "对称且端口不可预测，打洞基本无解；会直接走中转，这本身是正确结果。",
    NatSub.UNKNOWN: "先做一次网络诊断；判不出通常说明 UDP 被拦了。",
}


# ---------------------------------------------------------------------------
#  ANCHOR FRESHNESS -- the single most important number for a NAT4 punch
# ---------------------------------------------------------------------------
#
# The peer scans a window around the port we PUBLISHED. That port was measured
# when we started, and on a NAT whose allocation cursor keeps moving it is
# already wrong by the time anyone looks. His real measurement, same socket,
# same LAN address, 90 seconds apart:
#
#     23:20:23  published 220.178.180.180:22116
#     23:21:53  published 220.178.180.180:28745
#
# ~73 ports per SECOND. A punch starting 140s after the last publish aimed its
# scan ~10,000 ports from where the socket array actually opened, so every
# packet went to ports the peer was never allocated -- and NO window width
# covers an error that size (his bench: at drift 300, both_easy measures 0%
# even at max reach 2000).
#
# So: re-measure right before punching, and treat 15 seconds as "already too
# old". `ANCHOR_REFRESH_TIMEOUT_S` bounds that extra STUN round trip, and it
# is deliberately NOT done every round -- each probe is a new destination,
# which on a symmetric NAT consumes an allocation slot and nudges the very
# cursor the peer is scanning for.
ANCHOR_STALE_S = 15.0
ANCHOR_REFRESH_TIMEOUT_S = 1.2

#: How far the port walk may go before it stops generating new bands. Walking
#: is banded and shuffled WITHIN a band, so this bounds "distinct ports tried"
#: rather than a window width.
PORT_WALK_LIMIT = 20000

#: Plan names as he spells them (used by his helper functions).
METHOD_NONE = "none"
METHOD_CONE = "cone_to_cone"
METHOD_SYM_TO_CONE = "sym_to_cone"
METHOD_BOTH_EASY = "both_easy_sym"
METHOD_BIRTHDAY = "birthday"

#: `both_easy` only works if BOTH ends spray at once, so it needs a floor: a
#: 2s budget (what a fallback would hand it) sees one side stop before the
#: other has started.
BOTH_EASY_MIN_S = 8.0

#: One spray round per 100 ms. He measured that going faster than this does
#: not improve the hit rate, it just looks more like an attack.
SPRAY_INTERVAL_S = 0.1

#: Birthday-attack shaping: start wide, decay each round, and never go below
#: the floor. Sample exhaustion is the failure mode these prevent.
BIRTHDAY_MIN_PROBES = 180
BIRTHDAY_DECAY = 0.75

#: Compliance ceiling. An uncapped spray is indistinguishable from a port
#: scan, and some ISPs will treat it as an attack. These three are a set:
#: MAX_CONCURRENT_PUNCHES x MIN_PUNCH_PPS_PER_ATTEMPT == MAX_PUNCH_PPS.
MAX_PUNCH_PPS = 2400
MIN_PUNCH_PPS_PER_ATTEMPT = 600
MAX_CONCURRENT_PUNCHES = 4
MAX_PUNCH_PACKETS = 40000

#: Cap on one attempt, well under his 25s connect budget.
PUNCH_TIMEOUT_CAP_S = 12.0

#: How many ports the hub (cone) side sprays per round.
HUB_SPRAY_PER_ROUND = 800

#: Fraction of each batch replaced by uniformly random ports when the peer
#: allocates per destination IP. His measured value is 0.0 for the normal
#: path (the window is fine) and the per-IP path uses a full random batch.
WINDOW_RANDOM_MIX = 0.0

# ============================================================
# Rate limiting and per-attempt budget
# ============================================================
#
# WHY THIS IS NOT OPTIONAL: an uncapped spray is indistinguishable from a port
# scan, and some ISPs will treat it as an attack. His wording: "无上限的喷洒跟
# 端口扫描没有区别". The three numbers below are a SET and must stay
# consistent -- without the concurrency cap, "every attempt has a floor" and
# "the process has a ceiling" fight each other:
#
#     MAX_CONCURRENT_PUNCHES x MIN_PUNCH_PPS_PER_ATTEMPT == MAX_PUNCH_PPS
#            4             x            600             ==      2400
#
# Original: a shared token bucket in a worker thread (`RateGuard.shared()`).
# We are single-loop asyncio, so this is the same algorithm with the sleep
# made async -- no lock is needed because the loop serialises the callers.
# The semantics are his: a per-attempt slice with a floor, plus an aggregate
# backstop that is never exceeded.

class RateGuard:
    """
    Process-wide spray ceiling, shared by every concurrent punch.

    A single shared bucket is right in principle (the number an ISP sees is
    the aggregate). But first-come-first-served inside one bucket makes the
    HOST side suffer, because the host is exactly the side punching to several
    peers at once: measured ~400 pps each instead of 600, so an 8 s window held
    13 rounds instead of 66. So the ceiling is DIVIDED between active attempts,
    with a floor so one attempt is never squeezed below the point where a round
    fits.
    """

    _shared = None

    def __init__(self, pps=MAX_PUNCH_PPS, min_pps=MIN_PUNCH_PPS_PER_ATTEMPT):
        self.pps = int(pps)
        self.min_pps = int(min_pps)
        self._window_start = time.monotonic()
        self._in_window = 0
        self._active = 0

    @classmethod
    def shared(cls):
        """One guard for the whole process (his `RateGuard.shared()`)."""
        if cls._shared is None:
            cls._shared = cls()

        return cls._shared

    def share(self):
        """This attempt's slice of the ceiling, in packets/second."""
        return max(self.min_pps, self.pps // max(1, self._active))

    async def allow(self, n=1, share=None):
        """
        Take `n` packets from the bucket, sleeping only the REMAINDER of the
        second when the window is full.

        Sleeping a whole second on every overshoot halves throughput at
        exactly the moment the spray starts working (his note), and re-reading
        the clock after the sleep matters too: using the pre-sleep timestamp
        drifted the window start backwards on every overshoot.
        """
        limit = share if share else self.pps

        now = time.monotonic()

        if now - self._window_start >= 1.0:
            self._window_start = now
            self._in_window = 0

        if self._in_window + n > limit:
            gap = 1.0 - (now - self._window_start)

            if gap > 0:
                await asyncio.sleep(gap)

            self._window_start = time.monotonic()
            self._in_window = 0

        self._in_window += n

        return True

    def add_attempt(self):
        self._active += 1

    def remove_attempt(self):
        self._active = max(0, self._active - 1)


class AttemptBudget:
    """
    One attempt's packet allowance, drawn against the shared rate.

    Kept separate from the rate ceiling on purpose. They used to be the same
    counter, and resetting it per attempt meant five concurrent punches each
    cleared a counter they all shared -- so the per-attempt cap never held for
    anyone, while the (intended to be persistent) rate window was cleared too.
    """

    def __init__(self, guard, total, log=None):
        self.guard = guard
        self.total = int(total)
        self.left = int(total)
        self.log = log or (lambda *_a: None)
        self._share = guard.share()

    def __enter__(self):
        self.guard.add_attempt()
        self._share = self.guard.share()
        return self

    def __exit__(self, *_exc):
        self.guard.remove_attempt()
        return False

    async def allow(self, n=1):
        """True when `n` more packets are within this attempt's allowance."""
        if self.left <= 0:
            return False

        if n > self.left:
            n = self.left

        await self.guard.allow(n, share=self._share)
        self.left -= n

        return True


# ============================================================
# HIS PUNCH POLICY HELPERS -- ported verbatim
# ============================================================
#
# Everything from here to the end of this block is MCLanP2P's own code
# (`client/natpunch.py`), copied unchanged. Each function carries the note he
# wrote next to it, because those notes ARE the specification: every one of
# them describes a failure he already paid for.
#
# The string constants mirror his protocol values. Our `NatSub` / `Filter`
# enums carry the same values, so call `.value` at the boundary -- that keeps
# his code working on plain strings exactly as written.

NAT_SUB_CONE = "cone"
NAT_SUB_EASY_INC = "sym_easy_inc"
NAT_SUB_EASY_DEC = "sym_easy_dec"
NAT_SUB_HARD = "sym_hard"
NAT_SUB_UNKNOWN = "unknown"

FILTER_EIF = "eif"
FILTER_ADF = "adf"
FILTER_APDF = "apdf"
FILTER_UNKNOWN = "unknown"

#: ★ 用户 2026-10-01：**"最多地址受限"不等于"就是地址受限"**。
#:
#: 换端口应答到了、换 IP 那一半却测不了（STUN 只有一个公网 IP，或服务器不实现
#: CHANGE-IP），这时既不能断言 NAT2 地址受限，也不能断言 NAT1 全锥 —— 只能给
#: 这个区间值。显示成「NAT1/2 未定（全锥或地址受限）」。
#:
#: `FILTER_RANK` 故意与 `FILTER_ADF` **同档**：打洞计划的选择逻辑因此一个字节
#: 都不会变（连接核心不受影响），它只影响界面文案与颜色。
FILTER_ADF_OR_EIF = "adf_or_eif"


FILTER_RANK = {FILTER_EIF: 0, FILTER_ADF: 1, FILTER_APDF: 2,
               FILTER_ADF_OR_EIF: 1, FILTER_UNKNOWN: 1}


def _wrap_port(p):
    """Fold a port into the usable range instead of dropping it.

    The walk used to discard anything outside 1024..65535, which meant "run
    out of ports" the moment it reached the top of the range. Real log, the
    anchor at 57492, walking upward:

        [punch] window exhausted at 19999 ports
        [punch] covered 8043 ports 57493..65535

    A 19999-port window covered 8043 ports -- and the 56,000 ports BELOW
    the anchor were never tried, because the code read 65535 as the end of
    the world. The port space is circular; there is plenty further to go.

    Wrapping is not a re-run of the same ports: it only happens after every
    port on the near side of the anchor has been tried, so nothing is
    repeated. And it is worth doing even for a peer judged "sequential",
    because the same peer measured 1 apart on one sample and 350 and 760
    apart on others -- its allocator is not a pure +1 walk, so the far side
    of the space is not the lost cause a strict reading would make it.
    """
    lo, hi = 1024, 65535
    span = hi - lo + 1
    return lo + ((int(p) - lo) % span)


def _next_ports(state, want):
    """The next `want` ports to try: near-first, outward, never repeated.

    An unbounded walk rather than a fixed list, because the target moves
    (see the note at the scan site). Banded like port_window -- shuffle
    within a band so it does not read as a sequential port scan, never
    shuffle across bands, so a peer that barely drifted is still found in
    the first rounds.

    Wraps around the port range instead of stopping at it -- see
    _wrap_port. Stopping at 65535 silently cost a real session 60% of the
    window it had asked for.
    """
    out = []
    while len(out) < want:
        if not state["pre"]:
            band = state["next"] // state["width"]
            if band * state["width"] >= PORT_WALK_LIMIT:
                break
            lo = band * state["width"] + 1
            hi = lo + state["width"]
            sub = state["sub"]
            if sub in (NAT_SUB_EASY_INC, NAT_SUB_EASY_DEC):
                signs = (-1,) if sub == NAT_SUB_EASY_DEC else (1,)
            else:
                # Unknown: walk BOTH ways.
                #
                # _peer_subtype defaults a peer that only reported the
                # coarse "symmetric" to EASY_INC, and an older build does
                # exactly that. If such a peer decrements, walking only
                # upward covers half the space and misses every time --
                # port_window used to cover both directions, so this was a
                # regression, not a new limitation.
                signs = (1, -1) if (band % 2 == 0) else (-1, 1)
            pre = []
            for sign in signs:
                for k in range(lo, hi):
                    pre.append(_wrap_port(state["base"] + sign * k))
            state["pre"] = pre
            random.shuffle(state["pre"])     # inside the band only
            state["next"] = hi - 1
        take = min(want - len(out), len(state["pre"]))
        out.extend(state["pre"][:take])
        state["pre"] = state["pre"][take:]
    return out


def _should_spray_random(my_sub, peer_sub):
    """In a birthday attack, should THIS end spray at random ports?

    One end has to spray and one end has to open mappings, and which is
    which depends on the pair:

      * cone x hard  -> the CONE side sprays (its own NAT accepts
        anything, so whichever mapping it hits answers). The hard side
        only opens mappings.
      * hard x hard  -> NEITHER side is a cone, so the old "only the non-
        hard side sprays" rule left nobody spraying at all. With EIF on
        both ends the door is already open and the port is the only
        obstacle, so both spray.
    """
    if my_sub != NAT_SUB_HARD:
        return True              # I am the cone side: I spray
    return peer_sub == NAT_SUB_HARD   # both hard -> both spray


#: ★ 用户 2026-10-01：让**两边都有机会当"后发方"**（全锥证据只可能被后发方看到）。
#:
#: `should_lead()` 是按 client_id 排序的确定性判据 —— 一对固定对端里永远是同一
#: 边当 leader；leader 先发包，所以它**永远拿不到自己的全锥证据**（证据要求
#: "对端的包在我发包之前到"）。用户实测"只有手动断开重连才偶发测出 NAT1"就是
#: 这个原因。
#:
#: 这里按**时间片**（墙钟，两端算出来一样）轮换领导权：半个时间片你后发、半个
#: 我后发。轮换只改"谁先等那一拍"，**不改发包次数/内容/超时**；即使两端在同一
#: 瞬间跨了时间片边界（算错），最坏也只是"两边都等一拍"（`SPRAY_INTERVAL_S`），
#: 打洞照样成立。设为 0 即完全回到旧行为。
LEAD_PARITY_WINDOW_S = 30.0


def resolve_lead(my_id, peer_id, evidence_active=False, now=None):
    """
    谁先发包（`should_lead`），需要收集全锥证据时按时间片轮换。

    **纯函数**（`now` 可注入）；`evidence_active=False` 时与 `should_lead()` 一字不差。
    """
    lead = should_lead(my_id, peer_id)

    if not evidence_active or LEAD_PARITY_WINDOW_S <= 0:
        return lead

    stamp = time.time() if now is None else now
    slot = int(stamp / LEAD_PARITY_WINDOW_S)

    return (not lead) if (slot % 2) else lead


def should_lead(my_id, peer_id):
    """True if this side starts first when both ends spray at once.

    Both sides compute the same answer without a round trip. The follower is
    only delayed, never silent: it still has to send, or no hole opens.
    """
    if not my_id or not peer_id:
        return True
    return str(my_id) <= str(peer_id)


def _mix_random(batch, tried, frac):
    """
    Fill a round's batch with ports this attempt has NOT aimed at yet.

    ============================================================================
      THE PORT BLACKLIST APPLIES TO THE WINDOW HALF TOO
    ============================================================================

    See the note at the scan site: on a per-destination NAT the window can be
    aimed at a region the peer was never allocated in, and then only the random
    guesses can land.

    But the blacklist is not only about the random half. The window half used to
    be re-sent unchanged every round, because a fresh batch of window ports is
    generated each time -- so after N rounds the peer saw the same window ports
    N times while its cursor had moved on, and the round's effort covered far
    fewer DISTINCT ports than the log's "sent" count suggested. Those repeats
    are exactly the failure the project owner described: repeated guesses at
    ports already missed, crowding out guesses that could still land.

    So: drop blacklisted ports from the window batch, and top the batch back up
    to its original size from the random end. Every port leaving here is added
    to `tried`. (His `_mix_random(batch, array.tried_ports, mix)`, with the
    same intent.)
    """
    if not batch:
        return batch

    out = [port for port in batch if port not in tried]

    frac = max(0.0, float(frac or 0.0))
    n_rand = max(1, int(len(batch) * frac)) if frac > 0 else 0

    # Guarantee the random share even when the window half came back almost
    # intact: a window guess is worthless the moment the anchor is stale, and
    # the random guesses are the only ones that can land then.
    if frac > 0:
        n_rand = max(n_rand, 1)

    for _ in range(n_rand * 8):
        if len(out) >= len(batch):
            break

        port = random.randint(PORT_MIN, PORT_MAX)

        if port in tried or port in out:
            continue

        tried.add(port)
        out.append(port)

    # Shuffle only when the batch was actually restructured: with no random
    # share the caller asked for its own order back, and shuffling it would
    # make "frac=0 means leave it alone" false.
    if frac > 0:
        random.shuffle(out)

    for port in out:
        tried.add(port)

    return out


def _should_scan_window(plan, my_sub, peer_sub):
    """Should we also walk a window of predicted ports?

    Only a CONE peer is exempt: its port never changes, so a window around
    it is 50 targets that cannot possibly answer -- that was the whole of
    sym_to_cone's wasted round.

    A random peer is the opposite case and used to fall into the same
    branch, which is the bug: with the peer's allocation unpredictable the
    scan was switched off entirely and every socket hammered the ONE port
    the peer published for its STUN server. Real log, 100 sockets and
    13 seconds:

        [punch] sent 8989 packets to 23653 (anchor measured 2s ago)

    One port, 8989 times. On a per-destination NAT the peer is not on
    23653 when talking to us, so those packets had nothing to hit -- while
    its own array sat somewhere in the 64k space, unguessed.
    """
    if plan == METHOD_BOTH_EASY:
        return True          # both allocations move
    if plan == METHOD_SYM_TO_CONE:
        if peer_sub == NAT_SUB_CONE:
            return False     # cannot move; a window buys nothing
        # predictable -> a contiguous window; unpredictable -> a wide
        # random spray, which is the only thing that can find it
        return True
    return False


def _unpredictable(sub):
    """A port allocation we cannot aim a window at.

    HARD (measured random) and UNKNOWN (never characterised) are the same
    problem for a scan: the published port tells us nothing about where the
    next mapping lands, so a contiguous window around it is a guess about
    a number that was never related to the answer.
    """
    return sub in (NAT_SUB_HARD, NAT_SUB_UNKNOWN)


def filter_rank(f):
    """How strict a filter is. Unknown sits in the middle.

    Unknown deliberately does NOT rank as strict. Treating it as strict made
    every undiagnosed network look like the worst case, and undiagnosed is
    the common case -- most servers silently ignore CHANGE-REQUEST. The
    failure mode of ranking it loosely is optimism; the failure mode of
    ranking it strictly is giving up on networks that would have worked.
    """
    return FILTER_RANK.get(f, 1)


def must_open_first(my_filter, peer_filter, my_id="", peer_id=""):
    """True if WE have to send before the peer's packets can get in.

    This is the question RFC 5780 exists to answer.

    A NAT that filters per destination drops the peer's packets until we
    have sent to that address ourselves. If BOTH ends are like that and
    both wait for the other, nothing ever arrives -- not because the hole
    cannot be punched but because neither side opened the door. The
    stricter end has to go first.

    Ties fall back to the id comparison so both sides still agree without a
    round trip.
    """
    a, b = filter_rank(my_filter), filter_rank(peer_filter)
    if a != b:
        return a > b
    return should_lead(my_id, peer_id)


def sym_punch_enabled():
    """Master switch.

    EasyTier ships --disable-sym-hole-punching for exactly this reason: the
    spray can be identified and blocked by an ISP. Anyone who does not want
    it must be able to turn it off without patching code.
    """
    return os.environ.get("DISABLE_SYM_PUNCH", "").strip().lower() not in (
        "1", "true", "yes", "on")


def subtype_rank(sub):
    """How pessimistic a subtype is. Higher = worse.

    Ordering is by CONSEQUENCE, not by how "symmetric" the NAT is.
    hard is last because hard x hard is the one combination that gives up
    on punching entirely (see punch_plan); everything else still tries.
    """
    return {NAT_SUB_CONE: 0,
            NAT_SUB_EASY_INC: 1,
            NAT_SUB_EASY_DEC: 1,
            NAT_SUB_UNKNOWN: 2,
            NAT_SUB_HARD: 3}.get(sub, 2)


def subtype_regressed(new, old):
    """Is `new` a worse verdict than `old`?"""
    return subtype_rank(new) > subtype_rank(old)


def effective_filter(measured, inbound="unknown"):
    """The filtering verdict to act on, given BOTH measurements.

    Two independent measurements of the same property, and they can
    disagree -- in a real log they did, on both ends of one pair:

        [NAT] filtering: endpoint-independent (anything may come in)
        [NAT] inbound UDP is BLOCKED: ... it never arrived

    If anyone could reach us, the server's packet would have arrived. So a
    miss disproves eif and nothing else: a NAT that filters per address
    (adf) also drops a packet from a host we never wrote to, and it is the
    ordinary case -- the peer's packets are accepted from the moment we
    send to it, which is what punching is.

    Old behaviour: a miss was reported as "no amount of punching can help",
    which vetoed the punch for every adf host -- i.e. for most of the
    users this project exists for.

      measured   what the STUN tests concluded
      inbound    "open" | "punchable" | "blocked" | "unknown"
    """
    measured = measured or FILTER_UNKNOWN
    if inbound == "open":
        # An unrelated host got in: either genuinely open, or the server's
        # address was warmed first (adf). Trust the STUN verdict when it
        # has one; eif is the only thing a cold miss would have ruled out.
        return measured if measured != FILTER_UNKNOWN else FILTER_EIF
    if inbound == "blocked":
        # Nothing unsolicited arrives, so eif is impossible. adf would have
        # been classified as "punchable" by the probe path; here the filter
        # says apdf, or the measurement is missing entirely.
        if measured == FILTER_EIF:
            return FILTER_ADF
        return measured
    if inbound == "punchable":
        # A miss that adf explains: never claim eif, it was disproved.
        if measured == FILTER_EIF:
            return FILTER_ADF
        return measured
    return measured


def _anchor_age_text(seconds):
    """How stale the anchor this scan was built around is."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "age unknown"
    if seconds < 0:
        return "age unknown"
    if seconds < ANCHOR_STALE_S:
        return "measured %.0fs ago" % seconds
    return "STALE: measured %.0fs ago" % seconds


# ============================================================
# Addresses: what to try, and in what order (his common.py helpers)
# ============================================================


def is_ipv6(text):
    """True for a literal IPv6 address (the parser needs to know)."""
    return ":" in (text or "") and not text.replace(".", "").isdigit()


def is_public_ipv4(text):
    """
    Routable public IPv4 -- i.e. worth counting as a punch attempt.

    Loopback, link-local, RFC1918 and CGNAT space are all "somewhere we can
    reach without punching", so failing against them says nothing about whether
    a hole is achievable and must not advance the retry budget.
    """
    ip = (text or "").strip()

    if not ip or is_ipv6(ip):
        return False

    parts = ip.split(".")

    if len(parts) != 4:
        return False

    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False

    if a in (0, 10, 127) or a >= 224:
        return False

    if a == 169 and b == 254:
        return False

    if a == 172 and 16 <= b <= 31:
        return False

    if a == 192 and b == 168:
        return False

    if a == 100 and 64 <= b <= 127:      # CGNAT
        return False

    return True


def same_subnet(a, b):
    """
    True only for a same-/24 IPv4 pair.

    A /16 match used to be accepted too, which is far too loose: CGNAT ranges
    (10.x, 100.64/10) and large corporate networks would make complete
    strangers look like LAN neighbours. Worse, LAN endpoints are published to
    every room member, so being wrong also leaks internal addresses to
    unrelated peers.

    /24 covers the overwhelming majority of home routers. Anything larger is
    treated as public, which is the safe failure direction.
    """
    try:
        pa = [int(x) for x in str(a).split(".")]
        pb = [int(x) for x in str(b).split(".")]
    except (ValueError, AttributeError):
        return False

    if len(pa) != 4 or len(pb) != 4:
        return False

    # Never treat a loopback or link-local address as a LAN peer.
    if pa[0] in (127, 169) or pb[0] in (127, 169):
        return False

    return pa[:3] == pb[:3]


def _split_endpoint(value):
    """
    ("1.2.3.4:5000" | ("1.2.3.4", 5000)) -> ("1.2.3.4", 5000).

    Returns (None, None) for anything unparseable and NEVER raises: these
    values come off the wire from a peer, so a bad one has to be skipped rather
    than take the session down.
    """
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        try:
            return str(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None, None

    ip, _, port = str(value).rpartition(":")

    try:
        return ip, int(port)
    except ValueError:
        return None, None


def local_endpoints(port=0):
    """
    This host's LAN IPv4 endpoints as "ip:port" strings.

    Matters because two players on the same LAN must NOT be sent out to the
    public internet and back -- that needs hairpinning on the router, which is
    widely unsupported, and it needlessly burns the relay.

    Loopback is excluded even though it is a perfectly good address locally:
    these strings are published to every room member, so offering
    "127.0.0.1:30000" tells the peer to dial ITSELF. That is not theoretical --
    a real log had a peer advertising exactly that.
    """
    ips = []

    with contextlib.suppress(Exception):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()

    with contextlib.suppress(Exception):
        for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET
        ):
            ips.append(info[4][0])

    out, seen = [], set()

    for ip in ips:
        if not ip or ip in seen:
            continue

        # Link-local is never routable, not even inside the LAN.
        if ip.startswith("169.254.") or ip.startswith("127."):
            continue

        seen.add(ip)
        out.append("%s:%d" % (ip, int(port or 0)))

    return out


def ipv6_endpoints(port=0):
    """
    This host's globally-routable IPv6 endpoints as "ip:port" strings.

    IPv6 does not go through NAT at all, so if both ends have a global v6
    address they can simply connect -- no prediction, no socket array, no
    birthday attack. It is the cleanest bypass there is.

    Only GLOBAL addresses count: link-local (fe80::) is no more routable than
    169.254, and ::1 means "yourself".

    NOTE: these are COLLECTED and published, but the punch itself is IPv4-only
    for now -- PunchArray binds AF_INET. Collecting them keeps the candidate
    list honest and makes that gap reportable instead of invisible.
    """
    out, seen = [], set()

    def add(ip):
        if not ip:
            return

        ip = str(ip).split("%")[0]      # scope id is only meaningful locally
        low = ip.lower()

        if low.startswith("fe80:") or low == "::1" or low in seen:
            return

        seen.add(low)
        out.append("%s:%d" % (ip, int(port or 0)))

    # Ask the kernel which source address it would actually pick. Resolving the
    # hostname only works when the name resolves to a global address at all --
    # on most Linux boxes it resolves to ::1 and nothing else, so the v6 list
    # came back empty even on machines with a perfectly good global address.
    with contextlib.suppress(Exception):
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))
        add(s.getsockname()[0])
        s.close()

    with contextlib.suppress(Exception):
        for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET6
        ):
            add(info[4][0])

    return out


def is_lan_candidate(ip, my_locals):
    """
    这个候选地址是不是"和我同一个 /24 的邻居"。

    和 `same_subnet` 的区别只有一个：这里的 `my_locals` 是客户端自己那一串
    "ip:port"（`local_endpoints` 的格式），所以要先把端口摘掉再比。写成
    独立函数是为了让调用方能明确地问这个问题，而不是各自去拆字符串。
    """
    if not ip or is_ipv6(ip):
        return False

    for value in my_locals or ():
        mine, _port = _split_endpoint(value)

        if mine and not is_ipv6(mine) and same_subnet(ip, mine):
            return True

    return False


def order_candidates(peer_public, peer_locals, my_locals, log=None):
    """
    Connection attempts in priority order: LAN, then IPv6, then public.

    LAN first because it is cheap and always works where it applies, and
    because two players in the same dorm should not be sent out to a global
    address and back (hairpinning, widely unsupported).

    Everything is still ATTEMPTED in the end: a candidate that looks wrong on
    paper can be the reachable one, so the order is a preference, not a filter.

    ==================================================================
     「是不是同一个局域网」是怎么定的（用户 2026-09 提的问题）
    ==================================================================
    结论先说：**只看私网地址的前三段（同 /24），不看公网 IP，也不 ping。**

    为什么不用"公网 IP 相同"当判据：它两个方向都会错。
      * 同一个公网 IP ≠ 同一个局域网：一栋楼/一个小区宽带共用 CGNAT 出口，
        两台机器公网地址一模一样，私网却各自在 192.168.1.x —— 这种"看起来
        像邻居"会把它们引到根本不存在的直连路上。
      * 不同的公网 IP ≠ 不在同一个局域网：双 WAN 路由、办公网多出口、
        一部分机器走 CGNAT 一部分走公网出口时，同一根网线两边的公网地址
        就是不一样的，而它们之间的 LAN 直连明明是最好的路。
    所以公网地址只能当"降级后的兜底候选"，不能当"同局域网"的证据。

    为什么不用 ping 当判据：Windows 默认防火墙就挡 ICMP，很多企业网也挡。
    "ping 不通"完全不能推出"不在一个局域网"，用它当闸门只会把本来能直连的
    邻居挡在外面。

    VLAN 的情况：如果交换机把两台机器隔离在不同 VLAN，我们的 LAN 候选就是
    不发包也不回包。因为这里排的是【优先级】而不是【过滤】—— LAN 候选失败
    之后公网候选照打，晚到的玩家也不会被冷落；而且 LAN 每次只花
    `LOCAL_TRY_BUDGET_S`（1.5 秒），不会吃掉公网打洞的窗口。

    **真正的判据是应用层握手**：候选地址只是"往哪发"，是不是通的由 punch 的
    HELLO / HELLO_ACK 说了算 —— 只有对方真的在同一段网里收到并回了包，这条
    LAN 路才算成立。这比任何"看起来像邻居"的推断都硬，而且它同时把
    "/24 相同但其实中间隔着 NAT/VLAN"的情况自动排除掉了。
    """
    mine = [_split_endpoint(m)[0] for m in (my_locals or [])]
    mine = [m for m in mine if m]

    out, seen = [], set()

    if log is not None:
        with contextlib.suppress(Exception):
            log(
                f"[punch] peer locals={list(peer_locals or [])} "
                f"public={peer_public}"
            )

    # ---- LAN IPv4 first ----
    for value in peer_locals or []:
        pip, pport = _split_endpoint(value)

        if pip is None or is_ipv6(pip):
            continue

        if any(same_subnet(pip, m) for m in mine):
            key = (pip, pport)

            if key not in seen:
                seen.add(key)
                out.append(key)

    # ---- IPv6 next, and only when routable ----
    #
    # same_subnet() only understands IPv4, so a global v6 address would
    # otherwise never match "LAN" and would be dropped from the list entirely,
    # making the one path that needs no NAT unreachable.
    for value in peer_locals or []:
        pip, pport = _split_endpoint(value)

        if pip is None or not is_ipv6(pip):
            continue

        low = pip.lower()

        if low.startswith("fe80:") or low == "::1":
            continue

        key = (pip, pport)

        if key not in seen:
            seen.add(key)
            out.append(key)

    # ---- The public endpoint last ----
    if peer_public:
        pip, pport = _split_endpoint(peer_public)

        if pip is not None and (pip, pport) not in seen:
            seen.add((pip, pport))
            out.append((pip, pport))

    return out


#: The payload the relay's STUN responder fires at our published endpoint when
#: we ask for an inbound probe. It is the marker that tells a probe apart from a
#: punch packet -- without it, "something arrived" could not be attributed.
#:
#: Must match `server/stun_server.py`'s b"MCLINK-INGRESS-PROBE".
INGRESS_MAGIC = b"MCLINK-INGRESS-PROBE"

#: Inbound verdicts (his `_inbound_state`). `unknown` is a real answer and is
#: reported with its REASON -- see DirectLink.probe_inbound.
INBOUND_OPEN = "open"
INBOUND_PUNCHABLE = "punchable"
INBOUND_BLOCKED = "blocked"
INBOUND_UNKNOWN = "unknown"

#: How long to wait for the relay's probe to land (his
#: INBOUND_PROBE_TIMEOUT_S). Generous on purpose: the relay has to be asked,
#: then send, and the packet has to cross back -- and a timeout is reported as
#: "we do not know", never as "blocked".
INBOUND_PROBE_TIMEOUT_S = 4.0

#: How far above/below the observed range still counts as "the next one".
NAT_EASY_MAX_STEP = 100

#: Two destinations whose mapped ports differ by more than this are probably
#: allocating from per-destination-IP pools. Then the published anchor is in a
#: region that does not apply to the peer AT ALL, and window walking is
#: pointless -- random collision is the only mechanism left.
PER_IP_POOL_GAP = 512

#: Ignore "steps" larger than this: the allocation is not sequential, so
#: walking one port at a time from the anchor is meaningless.
SYM_MAX_DELTA = 1000

# ---------------------------------------------------------------------------
#  HIS TUNING (ported unchanged -- these numbers are the result of his
#  offline bench, not guesses)
# ---------------------------------------------------------------------------
#
#  Sockets are the EXPENSIVE dimension: each one is an fd plus a NAT mapping,
#  and a home router's UDP table is often 512-1024 entries that evict the
#  OLDEST -- which is the hub socket's published port, the one thing the whole
#  handshake depends on. Ports are cheap: one packet each from one socket.
SOCKETS_FOR_OPENP2P = 250        # his S2C target count
SOCKETS_FOR_BOTH_EASY = 100      # both ends predictable
SOCKETS_FOR_SYM_TO_CONE = 100    # one end predictable, one cone
SOCKETS_FOR_COLLISION = 800      # NO window is usable -> random collision
SPRAY_PORTS = 800                # the cone side's guesses, ONE socket
BIRTHDAY_PROBES = 600            # random ports per round (EasyTier: 600-800)

WINDOW_BOTH_EASY = 160           # contiguous ports the peer walks
WINDOW_BOTH_EASY_MAX = 900       # ... and how far it may reach if they miss
WINDOW_SYM_TO_CONE = 160
WINDOW_SYM_TO_CONE_MAX = 900

OPENP2P_ROUNDS = 5
OPENP2P_TIMEOUT_S = 7.0

#: Neighbourhood sprayed around an address we actually heard the peer from.
LEARN_NEIGHBOR_WINDOW = 64

PORT_MIN = 1024
PORT_MAX = 65535


def random_ports(count, tried=None):
    """
    `count` random distinct ports, drawn against a shared `tried` set.

    Sampling against `tried` is what makes the birthday attack work at all:
    re-guessing a port we already missed adds nothing, and the whole point is
    to spread guesses as widely as possible across rounds. (His `_fresh_ports`.)

    This is the PORT BLACKLIST's other half: `tried` is blacklisted ports, and
    every port returned here is added to it. Callers MUST pass one shared set
    for the whole attempt -- a fresh set per round is a fresh set of repeats.
    """
    tried = tried if tried is not None else set()
    out = []
    attempts = 0

    while len(out) < count and attempts < count * 20:
        attempts += 1
        port = random.randint(PORT_MIN, PORT_MAX)

        if port in tried:
            continue

        tried.add(port)
        out.append(port)

    return out


def predicted_ports(base_port, delta, count):
    """Contiguous ports to spray, one direction, starting one step out.

    CONTIGUOUS, not "base + k*delta". Stepping by the measured delta was the
    reason symmetric NATs never punched: with a measured step of 7 it tried
    +7 +14 +21 ..., while a sequential allocator's very next mapping is +1.
    The step tells us the DIRECTION, not the stride -- so we walk every port
    in between and let the direction decide which way.

    A negative delta simply walks downwards, so the sign is preserved
    rather than folded away with abs().
    """
    step = 1 if (delta or 1) > 0 else -1
    if abs(delta or 0) > SYM_MAX_DELTA:
        step = 1 if delta > 0 else -1
    out, seen = [], set()
    for k in range(1, count + 1):
        cand = base_port + k * step
        if 1024 <= cand <= 65535 and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def port_window(base_port, subtype, width, max_width=None):
    """
    The contiguous ports worth trying, in the order to try them.

    PORTED VERBATIM from his `natpunch.port_window`. Read the three rules
    before touching it -- each one is a bug he already paid for:

      * **Contiguous, NOT `base + k*delta`.** With a measured step of 7 the
        old code tried +7 +14 +21 ... and missed a peer whose next allocation
        was +1. The step gives the DIRECTION, not the stride.
      * **Shuffled WITHIN a band, never across bands.** Walking 5000,5001,
        5002... is the signature of a port scan, and getting an unrelated host
        flagged is not an acceptable side effect of playing Minecraft. Ordering
        by distance also means a 200-packet budget is spent on the 200 NEAREST
        ports rather than 200 scattered ones.
      * **Bands walk outward, `max_width` extends the reach.** A peer whose
        allocator barely drifted is found in the first band, and one that has
        drifted past the initial window is still reachable later.

    `subtype` decides the direction; UNKNOWN walks both ways, which is why the
    per-direction span is halved there (otherwise two directions at full span
    double the target count and break the scan-shaped-traffic cap).
    """
    if not base_port:
        return []

    span = max(width, max_width or width)

    if subtype == "sym_easy_dec":
        signed = [-1]
    elif subtype == "sym_easy_inc":
        signed = [1]
    else:
        signed = [-1, 1]
        span = max(1, span // 2)

    out = []
    start = 1

    while start <= span:
        stop = min(start + width, span + 1)
        band = []

        for k in range(start, stop):
            for sg in signed:
                p = base_port + sg * k

                if PORT_MIN <= p <= PORT_MAX:
                    band.append(p)

        random.shuffle(band)
        out.extend(band)
        start = stop

    return out


# ============================================================
# The punch itself
# ============================================================

#: ============================================================================
#:  ★ 用户 2026-10-01：**全锥（eif）证据**的收集桶（**只观察，不改行为**）
#: ============================================================================
#:
#: 全锥的硬判据只有一条："来自一个我们**从未联系过**的端点的包，进得来"。
#: 我们自己的 STUN 只有一个公网 IP，所以"换 IP"那一半永远测不了；但是**对端**
#: 就是另一个公网 IP —— 打洞时它本来就会朝我们发包。于是：
#:
#:   * 记下"我们已经往哪些端点发过包"（`sent_to`）；
#:   * 收到**对端**的包时，如果那个端点**还不在** `sent_to` 里 -> 说明这个包是在
#:     我们发任何东西之前就进来的 -> 我们的 NAT 放行了"陌生端点" -> **全锥**。
#:
#: 两个关键点（否则会误判，用户专门问过"暖端口"）：
#:   1. 判据是"**本会话**从未联系过该端点"——上一次打洞/上一次连接联系过同一个
#:      IP:端口 的话，地址受限的 NAT 也会放行，那就**不是**全锥证据。所以
#:      `sent_to` 是**房间会话级**的集合，重连不重置（更保守，不会误报）。
#:   2. 只有**对端**的包算数（`note_peer` 里已经先校验了 `peer_ip` + 我们的包
#:      格式），端口扫描/杂包不会污染。
#:
#: 默认 `None` -> 什么都不记（所有其它调用方、所有单测行为不变）。
EVIDENCE = None

#: 已经为收集全锥证据开过几次打洞（诊断用；每开一次就重置一次证据桶）。
EVIDENCE_ATTEMPTS = 0

#: ★ 用户 2026-10-01：**本进程**里我们联系过的对端端点（永不清理）。
#:
#: 它是"暖端口"防护的保守版本：证据要求"本进程从未联系过这个端点"。
#: 为什么不用"本房间/本连接"：NAT 的放行表是**跨连接**存在的 —— 十分钟前在
#: 另一个房间往同一个朋友的 IP:端口 发过包，地址受限的 NAT 现在照样放行；
#: 用房间级集合就会把这种包误读成"陌生端点能进来"= 假全锥。宁可少拿几次证据，
#: 也不许误报（用户专门问了这一点）。
SENT_EVER = set()


def set_evidence(evidence):
    """房间打洞协调器在开始一轮房间打洞前放一个桶进来（None = 关闭）。"""
    global EVIDENCE
    EVIDENCE = evidence


class PunchArray:
    """
    N UDP sockets; every one of them is another mapping the peer can hit.

    One socket is one target. Against a NAT whose allocation is unpredictable
    there is nothing to aim at, so the only lever is having more targets.

    All sockets are read by ONE event loop, which is what lets us answer on
    the exact socket a packet arrived on -- on a per-destination-filtering NAT
    only that socket's reply can get out.
    """

    def __init__(self, count, log=None):
        self.count = max(1, int(count))
        self.log = log or (lambda *_a: None)
        #: Shared process-wide rate ceiling. Every send goes through it, so a
        #: spray can never look like an uncapped port scan (see RateGuard).
        self.guard = RateGuard.shared()
        #: Per-attempt packet allowance; set by the caller when it opens one.
        self.budget = None
        self.socks = []
        self.learned = {}        # addr -> socket (None = the hub socket)
        self.hit = None          # (sock, addr)
        self.sent = 0
        self._tasks = []
        self._closed = False
        self.hub_sock = None
        self.peer_ip = None
        #: ★ 全锥证据桶（模块级 `EVIDENCE`，由房间打洞协调器设置；默认 None）。
        #:
        #: **每次构造 = 一次新的本端映射**（一次打洞一套新 socket），放行表因此
        #: 是空的 -> 证据桶在这里重置。"暖端口不误判"的正确边界就是：只排除
        #: **本次映射**里已经联系过的端点。
        global EVIDENCE_ATTEMPTS
        self.evidence = EVIDENCE

        if self.evidence is not None:
            EVIDENCE_ATTEMPTS += 1
            self.evidence["sent_to"] = set()
            self.evidence["eif"] = False
            self.evidence["peer"] = None

        #: THE PORT BLACKLIST (his `array.tried_ports`).
        #:
        #: Every port this attempt has already aimed at, and therefore must not
        #: aim at again. It is a blacklist, not bookkeeping: on a NAT4 the
        #: peer's mapping for a given port either answers or it does not, and
        #: if it did not, that mapping is GONE -- its cursor drifts tens of
        #: ports per second, so the same port number is a DIFFERENT mapping a
        #: second later. Re-guessing a port we already missed is not a fresh
        #: guess, and spending a round's allowance on repeats is exactly how a
        #: punch ends up "spraying 800 ports" while covering only a couple of
        #: hundred distinct ones.
        #:
        #: Shared across EVERY round of one attempt and handed to
        #: random_ports() / _mix_random(), so the guesses genuinely widen
        #: instead of orbiting a handful of numbers.
        self.tried_ports = set()

    # ---- the port blacklist ----

    def ports_left(self):
        """How many ports in the usable range have NOT been blacklisted yet."""
        return max(0, (PORT_MAX - PORT_MIN + 1) - len(self.tried_ports))

    def note_tried(self, ports):
        """
        Blacklist `ports`. Returns how many were NOT already blacklisted.

        The return value is what keeps the caller's log honest: "0 new ports"
        means every guess this round was a repeat, which is a defect in the
        round's port generation and must be visible rather than silently
        counted as effort.
        """
        fresh = 0

        for port in ports:
            if port not in self.tried_ports:
                self.tried_ports.add(port)
                fresh += 1

        return fresh

    def port_span(self):
        """(low, high) of everything blacklisted, or None when empty."""
        if not self.tried_ports:
            return None

        return min(self.tried_ports), max(self.tried_ports)

    # ---- lifecycle ----

    def open(self):
        """Bind every socket. Fewer than asked is fine; zero is not."""
        for _ in range(self.count):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setblocking(False)

                # A punch burst is short and loud; the default receive buffer
                # drops the very replies we are waiting for.
                with contextlib.suppress(OSError):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 18)

                s.bind(("0.0.0.0", 0))
                self.socks.append(s)
            except OSError as e:
                self.log(f"[punch] socket array stopped at {len(self.socks)}: {e}")
                break

        return len(self.socks)

    def local_ports(self):
        return [s.getsockname()[1] for s in self.socks]

    def start_readers(self, peer_ip):
        """Register every socket on the running loop, and never stop early.

        Three deliberate departures from the obvious implementation, the first
        two from the ported project:

          * it does NOT stop at the first packet. A NAT can deliver one stray
            datagram from an unrelated flow, and a reader that returns on the
            first thing it sees has no second chance.
          * every packet is a LEARNING opportunity, not just a possible hit:
            the source port is the mapping the peer's NAT actually created --
            precisely the number we could not predict.
          * the HUB socket is registered too, and that is not an optimisation.
            It is the port the peer is AIMING AT, so it is where the answer
            arrives. On the c2c/c2s roles WE speak first, so if we are not
            reading it, both ends spray into a socket that neither is
            listening on. The ported project gets this from its UdpHub
            catch-all; here we own the socket, so we read it.

            (Found by the loopback test: without this, c2c "sent 5" and
            received nothing, on both sides.)
        """
        self.peer_ip = peer_ip

        for s in self.socks:
            self._tasks.append(asyncio.create_task(self._read_loop(s)))

        if self.hub_sock is not None and self.hub_sock not in self.socks:
            self._tasks.append(
                asyncio.create_task(self._read_loop(self.hub_sock))
            )

    async def _read_loop(self, sock):
        loop = asyncio.get_running_loop()

        while not self._closed:
            try:
                data, addr = await loop.sock_recvfrom(sock, 2048)
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except OSError:
                return

            self.note_peer(data, addr, sock)

    def stop_readers(self):
        for task in self._tasks:
            task.cancel()

        self._tasks = []

    # ---- learning ----

    def note_peer(self, data, addr, sock=None):
        """
        Record a source endpoint the peer actually reached us from.

        Returns True when this was one of our own punch packets. Called for
        packets arriving on the array's sockets AND (via the hub catch-all)
        on the published-port socket, because the peer may be answering
        either.
        """
        if not data or (self.peer_ip is not None and addr[0] != self.peer_ip):
            return False

        if not is_our_packet(data):
            return False

        addr = (addr[0], addr[1])

        # ★ 用户 2026-10-01：全锥证据（**只观察**：一个字节都不改行为/时序）。
        #   这个包来自一个我们**本会话从未发过包**的端点 -> 我们的 NAT 放行了
        #   陌生端点 => 全锥（eif）。地址受限/端口受限的 NAT 在这种时刻会把它
        #   丢掉，所以永远不会误报成"全锥"。
        evidence = self.evidence

        if (
            evidence is not None
            and addr not in evidence.get("sent_to", ())
            and not evidence.get("eif")
        ):
            evidence["eif"] = True
            evidence["peer"] = addr

        # setdefault: the FIRST socket we heard it on is the one that can
        # reply. Later arrivals on other sockets must not overwrite it.
        self.learned.setdefault(addr, sock)

        if self.hit is None:
            self.hit = (sock, addr)

        # Answer immediately. Knowing the peer's mapping is worthless unless
        # we send something back through it -- that reply is what opens the
        # hole from OUR side, and it tells the peer its guess landed.
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.sendto(pack_hello_ack(), addr)

        return True

    def learned_addrs(self):
        return list(self.learned.keys())

    # ---- sending ----

    def send_all(self, data, addr):
        """One packet from every socket -- opens one mapping per socket.

        The HUB socket goes too, and it matters more than the rest: its
        mapping IS the published port, so it is the one address the peer is
        certain to be aiming at, and it is also where the reply can land
        (the hub owns the catch-all).
        """
        socks = list(self.socks)

        if self.hub_sock is not None:
            socks.insert(0, self.hub_sock)

        for s in socks:
            if not self._may_send():
                break

            with contextlib.suppress(OSError):
                s.sendto(data, addr)
                self.sent += 1

        # ★ 用户 2026-10-01：登记"我们联系过这个端点"。它让"对端首包早于我们
        #   发包"这条判据**只在本会话第一次接触时**成立（暖过的端点不算证据）。
        evidence = self.evidence

        if evidence is not None and addr:
            evidence.setdefault("sent_to", set()).add((addr[0], addr[1]))

    def _may_send(self, n=1):
        """
        Last check before a packet leaves: the per-attempt allowance.

        The RATE ceiling is enforced between rounds (see the punch loops),
        because these sends are synchronous -- awaiting inside a send loop per
        packet would make one round take a second and destroy the timing the
        punch depends on. His own design has the same split.
        """
        if self.budget is not None and self.budget.left <= 0:
            return False

        if self.budget is not None:
            self.budget.left -= n

        return True

    def send_from_hub(self, data, ip, ports):
        """
        Guess the peer's port from ONE source port: the published one.

        This is all of OpenP2P's `handshakeC2S`. Many destination ports, ONE
        source port -- because a peer that filters per address AND port admits
        packets from exactly the endpoint it spoke to, and drops every other.
        """
        if not ports or self.hub_sock is None:
            return 0

        n = 0

        for port in ports:
            if not self._may_send():
                break

            with contextlib.suppress(OSError):
                self.hub_sock.sendto(data, (ip, port))
                self.sent += 1
                n += 1

        return n

    def send_scatter(self, data, ip, ports):
        """
        One packet per socket, each to a DIFFERENT port, randomly paired.

        Random pairing is the point: tying socket i to port i creates a
        congruence between the two cursors that leaves about half of real
        sessions unwinnable at any window width (the ported project's
        derivation: hit needs 2r == d_a + d_b (mod N), and 2r is always even).
        """
        if not ports:
            return 0

        socks = list(self.socks)
        random.shuffle(socks)
        n = 0

        for index, s in enumerate(socks):
            port = ports[index % len(ports)]

            with contextlib.suppress(OSError):
                s.sendto(data, (ip, port))
                self.sent += 1
                n += 1

        return n

    def reply_learned(self, data):
        """Answer every address we have heard the peer from. Known-good."""
        n = 0

        for addr, sock in list(self.learned.items()):
            target = sock if sock is not None else self.hub_sock

            if target is None:
                continue

            with contextlib.suppress(OSError):
                target.sendto(data, addr)
                self.sent += 1
                n += 1

        return n

    def spray_learned_neighborhood(self, data, window=LEARN_NEIGHBOR_WINDOW):
        """
        Spray AROUND an address we actually heard the peer from.

        This is the mechanism that survives a NAT allocating per destination
        IP: the published port may be in a completely different region, but
        once a packet FROM the peer arrives we know a real mapping in the
        region that applies to US -- and the peer's allocations there are as
        consecutive as anywhere else. Its other sockets are nearby.
        """
        heard = self.learned_addrs()

        if not heard or not self.socks:
            return 0

        n = 0
        socks = list(self.socks)

        for index, s in enumerate(socks):
            ip, port = heard[index % len(heard)]
            offset = (index // max(1, len(heard))) + 1

            target = (port + offset) if (index % 2 == 0) else (port - offset)

            if not (PORT_MIN <= target <= PORT_MAX):
                continue

            with contextlib.suppress(OSError):
                s.sendto(data, (ip, target))
                self.sent += 1
                n += 1

        return n

    # ---- waiting ----

    async def wait_hit(self, timeout):
        """Wait for the first inbound packet. Returns (sock, addr) or None."""
        deadline = time.monotonic() + max(0.0, timeout)

        while time.monotonic() < deadline:
            if self.hit is not None:
                return self.hit

            await asyncio.sleep(0.02)

        return self.hit

    def detach(self, sock):
        """Hand one socket to the caller and stop reading it."""
        if sock is None:
            return None

        if sock in self.socks:
            self.socks.remove(sock)

        return sock

    def close(self):
        self._closed = True
        self.stop_readers()

        for s in self.socks:
            with contextlib.suppress(OSError):
                s.close()

        self.socks = []


# ============================================================
# Strategy selection
# ============================================================


PLAN_NONE = "none"
PLAN_C2C = "c2c"
PLAN_C2S = "c2s"
PLAN_S2C = "s2c"
PLAN_BOTH_EASY = "both_easy"
PLAN_SYM_TO_CONE = "sym_to_cone"
PLAN_BIRTHDAY = "birthday"


def openp2p_plan(my_sub, peer_sub):
    """
    Which of OpenP2P's three handshakes applies, or None.

    Mirrors openp2p's `core/p2ptunnel.go`, which is four lines:

        cone x cone        -> handshakeC2C
        symmetric x sym    -> ErrorS2S        (no UDP punch at all)
        peer sym, me cone  -> handshakeC2S
        peer cone, me sym  -> handshakeS2C

    Only the coarse cone/symmetric distinction matters: openp2p does not
    subdivide symmetric, and does not predict anything. An UNKNOWN subtype is
    treated as symmetric (NATSymmetric is openp2p's own default), which still
    leaves the pair punchable whenever the other end is a cone.

    Refusing to decide here is the bug that killed the ported project's
    handshakes for four rounds: `unknown` fell through to a plan value the
    caller skipped, so the punch code was never executed.
    """
    if my_sub is None or peer_sub is None:
        return None

    me_cone = my_sub is NatSub.CONE
    peer_cone = peer_sub is NatSub.CONE

    if me_cone and peer_cone:
        return PLAN_C2C

    if not me_cone and not peer_cone:
        # ErrorS2S. Both cursors move unpredictably.
        return None

    return PLAN_C2S if me_cone else PLAN_S2C


def punch_plan(my_sub, peer_sub, my_filter=None, peer_filter=None):
    """
    What to do for this pair. OpenP2P decides first; this only covers what it
    has no answer for (symmetric x symmetric).

    The ported project's hardest lesson is encoded in the ordering: the
    handshakes are the PRIMARY decision, and the local policy is a voter, not
    a gate. Having the policy go first meant a single "no mechanism" line
    disabled every handshake on every real pair.
    """
    if openp2p_plan(my_sub, peer_sub) is not None:
        return "openp2p"

    # Both sides unpredictable: the ONLY mechanism left is the birthday
    # collision -- keep every socket open and spray widely enough that a
    # collision happens. It is a long shot, but it is not nothing.
    #
    # This used to return PLAN_NONE unless BOTH ends measured EIF filtering
    # (which almost nothing does, because his server deliberately does not
    # implement CHANGE-REQUEST and the client therefore reports `unknown`).
    # The effect was that two symmetric NATs gave up BEFORE TRYING: the owner's
    # log is full of
    #
    #     [direct] no viable punching mechanism for this pair
    #
    # while his own client, on the same pair, spent a full five minutes
    # spraying thousands of packets with `multi-socket sym_to_cone` and the
    # birthday attack. Giving up is a decision the client may make; making it
    # from a filter reading we could not measure is not.

    both_easy = (
        my_sub in (NatSub.EASY_INC, NatSub.EASY_DEC)
        and peer_sub in (NatSub.EASY_INC, NatSub.EASY_DEC)
    )

    if both_easy:
        return PLAN_BOTH_EASY

    if my_sub in (NatSub.EASY_INC, NatSub.EASY_DEC):
        return PLAN_SYM_TO_CONE

    if peer_sub in (NatSub.EASY_INC, NatSub.EASY_DEC):
        return PLAN_SYM_TO_CONE

    if my_sub is NatSub.HARD or peer_sub is NatSub.HARD:
        return PLAN_BIRTHDAY

    return PLAN_C2C


def _report_step(step, text):
    """
    Send a live phase update to whoever is drawing a status card.

    Deliberately tiny and failure-proof: a status line is decoration, and it
    must never be able to interrupt a punch. `step` is None when nobody is
    listening (tests, direct callers).
    """
    if step is None:
        return

    with contextlib.suppress(Exception):
        step(str(text))


async def await_punch_start(start_at, log=None, step=None):
    """
    Hold off until the COORDINATED start instant, then return.

    ============================================================================
      WHY A SHARED START IS THE WHOLE MECHANISM (his `_await_punch_start`)
    ============================================================================

    A punch is not "send packets at the peer". It is a collision: each end
    walks a cursor of candidate ports and opens mappings, and a hit needs both
    ends to be inside the other's window at the same moment. The windows are a
    few hundred ports wide and the cursors move ~73 ports/second, so two ends
    that start on their own schedules overlap for a fraction of a second -- or
    not at all.

    With a shared start both ends are loud in the same interval, and the
    birthday collision has hundreds of packets on each side inside it.

    `start_at` is a MONOTONIC deadline, computed by the caller as
    "arrival + startIn" -- see protocol.PUNCH_START_DELAY_S for why the delay
    is relative to arrival and never an absolute timestamp.

    Returns True when we actually waited, False when there was nothing to wait
    for (no deadline, or it had already passed). A deadline in the past is NOT
    an error: it means the coordinated moment has arrived, and sleeping for it
    would be exactly the bug his code calls out by name.
    """
    log = log or (lambda *_a: None)

    if start_at is None:
        return False

    remaining = float(start_at) - time.monotonic()

    if remaining <= 0:
        return False

    log(f"[punch] waiting {remaining:.1f}s for the coordinated start")
    _report_step(
        step,
        f"waiting {remaining:.1f}s for the coordinated start",
    )

    # Report the countdown while we wait: "waiting" with no number is exactly
    # the uninformative status the user objected to. 0.25s granularity is
    # plenty for a human and costs nothing.
    deadline = time.monotonic() + remaining
    while True:
        left = deadline - time.monotonic()

        if left <= 0:
            break

        await asyncio.sleep(min(0.25, left))
        _report_step(
            step,
            f"waiting {max(0.0, left - 0.25):.1f}s for the coordinated start",
        )

    _report_step(step, "coordinated start reached")

    return True


class PunchResult:
    """What came out of one punch attempt."""

    def __init__(self, sock=None, addr=None, plan=PLAN_NONE, sent=0,
                 learned=None, elapsed=0.0, reason=""):
        self.sock = sock
        self.addr = addr
        self.plan = plan
        self.sent = sent
        self.learned = learned or []
        self.elapsed = elapsed
        self.reason = reason

    @property
    def ok(self):
        return self.sock is not None and self.addr is not None

    def __repr__(self):  # pragma: no cover - diagnostics only
        return (
            f"PunchResult(plan={self.plan}, ok={self.ok}, "
            f"addr={self.addr}, sent={self.sent})"
        )


async def openp2p_punch(peer_ip, peer_port, plan, hub_sock=None, log=None,
                        timeout=OPENP2P_TIMEOUT_S, sockets=SOCKETS_FOR_OPENP2P,
                        spray_ports=SPRAY_PORTS, rounds=OPENP2P_ROUNDS,
                        per_ip_pool=False, lead=True, guard=None,
                        budget_total=MAX_PUNCH_PACKETS, start_at=None,
                        anchor_age_s=None, step=None):
    """
    Run one of OpenP2P's three handshakes. Returns a PunchResult.

    `hub_sock` is the socket bound to our PUBLISHED port. It is required for
    the cone-side roles:
      * C2S sprays from it, because a peer filtering per address+port only
        admits the endpoint it spoke to;
      * C2C and C2S both need it to RECEIVE the answer, since that is the port
        the peer is aiming at.
    A missing hub socket is reported, never silently worked around: sending
    from a fresh socket would produce 800 packets that cannot arrive while the
    log still reads as if the handshake ran.

    `lead` (his `should_lead`): when both ends spray at once, only one of them
    starts immediately -- otherwise two machines open hundreds of sockets in
    the same millisecond and the timing benefit of coordinating is lost. The
    FOLLOWER IS NOT SILENT: it still sends, just a beat later.

    `anchor_age_s` is only used to SAY how stale the peer's port was when the
    punch missed -- a stale anchor and an unreachable peer look identical in a
    log that does not say which, and they need opposite responses.

    `start_at` (his `startIn`): a MONOTONIC instant both ends agreed on. The
    spray does not begin before it. The socket array is opened FIRST, so the
    mappings have had a moment to settle, and the wait happens with the array
    already listening -- a start we arrive late for is still a start.
    """
    log = log or (lambda *_a: None)

    hello = pack_hello()
    hello_ack = pack_hello_ack()

    if plan == PLAN_S2C:
        count = sockets
    else:
        count = 0

    if plan in (PLAN_C2C, PLAN_C2S) and hub_sock is None:
        log("[punch] c2c/c2s needs the socket bound to the published port; "
            "skipping (a silent fallback would send packets that cannot arrive)")
        return PunchResult(plan=plan, reason="no hub socket")

    array = PunchArray(count, log=log)
    array.hub_sock = hub_sock

    if count:
        array.open()

    if not array.socks and hub_sock is None:
        log("[punch] no usable socket for punching")
        return PunchResult(plan=plan, reason="no socket")

    array.start_readers(peer_ip)

    guard = guard or RateGuard.shared()

    # THE COORDINATED START. Both ends stop here until the agreed instant, so
    # both sprays are loud at the same time. Placed AFTER the array is open and
    # reading: a packet that arrives during the wait is not lost.
    await await_punch_start(start_at, log, step)

    # The budget starts NOW, not when the caller asked us to punch: the wait
    # above was spent waiting for the peer, and charging the punch window for
    # it would silently shorten the collision the wait exists to create.
    started = time.monotonic()

    # His follower ritual: the leader starts now, the follower waits one beat.
    # Both still spray -- a follower that stayed silent would never open its
    # side of the hole.
    if not lead:
        await asyncio.sleep(SPRAY_INTERVAL_S)

    try:
        with AttemptBudget(guard, budget_total, log=log) as budget:
            array.budget = budget

            if plan == PLAN_C2C:
                array.send_all(hello, (peer_ip, peer_port))
            elif plan == PLAN_C2S:
                # Drawn against the attempt's blacklist, so round 2 cannot
                # repeat round 1's misses. See PunchArray.tried_ports.
                ports = random_ports(spray_ports, array.tried_ports)

                if peer_port and peer_port not in array.tried_ports:
                    ports.insert(0, peer_port)
                    array.note_tried([peer_port])

                sent = array.send_from_hub(hello, peer_ip, ports)

                if not sent:
                    log("[punch] c2s cannot run without a hub socket")
                    return PunchResult(plan=plan, reason="c2s without hub")

                log(
                    f"[punch] c2s: spraying {len(ports)} ports from the "
                    f"published port (peer is symmetric: it opens {sockets} "
                    f"mappings and we must hit one); "
                    f"{array.ports_left()} ports still unguessed"
                )
            else:
                array.send_all(hello, (peer_ip, peer_port))
                log(
                    f"[punch] s2c: opened {len(array.socks)} mappings, one "
                    f"packet each to the peer's fixed port {peer_port}"
                )

            per = max(SPRAY_INTERVAL_S, timeout / max(1, rounds))

            for _round in range(1, max(1, rounds)):
                hit = await array.wait_hit(per)

                if hit:
                    sock, addr = hit
                    array.detach(sock)
                    log(f"[punch] {plan} ok: punched via port {addr[1]}")
                    return PunchResult(
                        sock=sock, addr=addr, plan=plan, sent=array.sent,
                        learned=array.learned_addrs(),
                        elapsed=time.monotonic() - started,
                    )

                # LEARNED ADDRESSES FIRST: an address we have actually been
                # reached from beats any guess, and replying is what opens the
                # hole from our side.
                if array.learned:
                    array.reply_learned(hello_ack)
                    array.spray_learned_neighborhood(hello)

                if plan == PLAN_C2C:
                    array.send_all(hello, (peer_ip, peer_port))
                elif plan == PLAN_C2S:
                    # FRESH ports every round, never a repeat: the blacklist is
                    # the whole reason the birthday attack widens instead of
                    # orbiting the same handful of numbers.
                    batch = random_ports(spray_ports, array.tried_ports)

                    if not batch:
                        log(
                            "[punch] c2s: port pool exhausted after "
                            f"{len(array.tried_ports)} distinct guesses; "
                            "stopping rather than repeating ourselves"
                        )
                        break

                    array.send_from_hub(hello, peer_ip, batch)
                else:
                    array.send_all(hello, (peer_ip, peer_port))

            hit = await array.wait_hit(max(0.5, timeout / max(1, rounds)))

            if hit:
                sock, addr = hit
                array.detach(sock)
                log(f"[punch] {plan} ok: punched via port {addr[1]}")
                return PunchResult(
                    sock=sock, addr=addr, plan=plan, sent=array.sent,
                    learned=array.learned_addrs(),
                    elapsed=time.monotonic() - started,
                )

            span = array.port_span()

            # Say WHAT was covered, in numbers, because "punch failed" is not
            # actionable and the two causes need opposite responses:
            #   * we sent thousands of packets and nothing came back -> inbound
            #     is filtered/blocked (our NAT, theirs, or an ISP between)
            #   * we sent almost nothing -> the punch never really ran, and our
            #     own budget/rate limiter is to blame
            # On a per-IP-pool peer a "span a..b" would be meaningless (uniformly
            # random guesses cover nearly the whole space and printing a range
            # invites reading it as "we covered everything", which we did not).
            covered = (
                f"covered {len(array.tried_ports)} distinct ports "
                f"{span[0]}..{span[1]}" if span else "covered no ports"
            ) if not per_ip_pool else (
                f"tried {len(array.tried_ports)} ports spread uniformly at "
                f"random (no window: this peer allocates a separate port "
                f"region per destination IP)"
            )

            log(
                f"[punch] {plan} miss: sent={array.sent} {covered} "
                f"learned={len(array.learned)} budget_left="
                f"{budget.left} anchor={_anchor_age_text(anchor_age_s)}"
            )

            return PunchResult(
                plan=plan, sent=array.sent, learned=array.learned_addrs(),
                elapsed=time.monotonic() - started, reason="no packet arrived",
            )
    finally:
        array.close()


async def sym_to_cone_punch(peer_ip, peer_port, hub_sock=None, log=None,
                            timeout=OPENP2P_TIMEOUT_S,
                            sockets=SOCKETS_FOR_SYM_TO_CONE, my_sub=None,
                            peer_subtype=None, per_ip_pool=False,
                            lead=True, guard=None,
                            budget_total=MAX_PUNCH_PACKETS,
                            anchor_age_s=None, start_at=None, step=None):
    """
    One side predictable, one cone.

    The predictable side parks a batch of mappings and STAYS PUT; the cone side
    walks a contiguous window over the anchor. OpenP2P has no answer for this
    pair, so this plan is his.

    `start_at` is the coordinated start instant (see await_punch_start). The
    target side parks its mappings first and then waits: the mappings are the
    thing being aimed at, so they must exist before the instant, not after it.
    """
    log = log or (lambda *_a: None)
    hello = pack_hello()
    hello_ack = pack_hello_ack()

    mine_easy = my_sub in (NatSub.EASY_INC, NatSub.EASY_DEC)
    guard = guard or RateGuard.shared()

    if not lead:
        await asyncio.sleep(SPRAY_INTERVAL_S)

    started = time.monotonic()

    if mine_easy:
        # I am the predictable one: I am the TARGET. Open mappings and wait.
        array = PunchArray(sockets, log=log)
        array.hub_sock = hub_sock
        array.open()
        array.start_readers(peer_ip)

        # The parked mappings ARE the target the cone side aims at, so they
        # must exist before the coordinated instant. Open first, then wait.
        await await_punch_start(start_at, log, step)

        try:
            with AttemptBudget(guard, budget_total, log=log) as budget:
                array.budget = budget
                array.send_all(hello, (peer_ip, peer_port))
                log(
                    f"[punch] sym_to_cone: my ports are predictable, parking "
                    f"{len(array.socks)} targets, peer is the cone side"
                )

                deadline = time.monotonic() + timeout

                while time.monotonic() < deadline:
                    hit = await array.wait_hit(SPRAY_INTERVAL_S)

                    if hit:
                        sock, addr = hit
                        array.detach(sock)
                        return PunchResult(
                            sock=sock, addr=addr, plan=PLAN_SYM_TO_CONE,
                            sent=array.sent, learned=array.learned_addrs(),
                            elapsed=time.monotonic() - started,
                        )

                    if array.learned:
                        array.reply_learned(hello_ack)
                        array.spray_learned_neighborhood(hello)

                    array.send_all(hello, (peer_ip, peer_port))

                log(
                    f"[punch] sym_to_cone target side miss: sent={array.sent} "
                    f"learned={len(array.learned)}"
                )
                return PunchResult(
                    plan=PLAN_SYM_TO_CONE, sent=array.sent,
                    learned=array.learned_addrs(),
                    elapsed=time.monotonic() - started,
                    reason="window missed",
                )
        finally:
            array.close()

    # I am the cone side: my port does not move, so I do the walking.
    if hub_sock is None:
        return PunchResult(plan=PLAN_SYM_TO_CONE, reason="no hub socket")

    count = SOCKETS_FOR_COLLISION if per_ip_pool else 0
    array = PunchArray(count, log=log)
    array.hub_sock = hub_sock
    array.start_readers(peer_ip)

    age_text = _anchor_age_text(anchor_age_s)

    if per_ip_pool:
        # Drawn against the blacklist: the random path is the ONLY path that
        # can land when the peer allocates per destination IP, so repeating a
        # guess here wastes the entire mechanism (his `_fresh_ports`).
        walk = random_ports(WINDOW_SYM_TO_CONE_MAX, array.tried_ports)
        log(
            f"[punch] sym_to_cone: peer allocates per destination IP, so a "
            f"window cannot be aimed ({age_text}) -- using "
            f"{len(walk)} random ports across {SOCKETS_FOR_COLLISION} sockets"
        )
    else:
        walk = port_window(
            peer_port, peer_subtype, WINDOW_SYM_TO_CONE,
            max_width=WINDOW_SYM_TO_CONE_MAX,
        )
        log(
            f"[punch] sym_to_cone: I am the cone side, walking "
            f"{len(walk)} ports near-first from the peer's anchor ({age_text})"
        )

    # The cursor: near-first, outward, never repeating, wrapping at the top.
    # Restarting the same window every round spends the whole budget on ports
    # already tried -- his bench found 9 rounds and 120 rounds hit the same.
    state = {"base": peer_port, "next": 0, "sub": peer_subtype,
             "width": WINDOW_SYM_TO_CONE, "pre": []}

    # The walk begins at the coordinated instant, with the scout sockets
    # already bound and listening.
    await await_punch_start(start_at, log, step)
    started = time.monotonic()

    try:
        with AttemptBudget(guard, budget_total, log=log) as budget:
            array.budget = budget
            deadline = time.monotonic() + timeout
            sent_total = 0
            rnd = 0

            while time.monotonic() < deadline:
                rnd += 1

                if per_ip_pool:
                    batch = random_ports(HUB_SPRAY_PER_ROUND, array.tried_ports)

                    if not batch:
                        log(
                            "[punch] sym_to_cone: port pool exhausted after "
                            f"{len(array.tried_ports)} distinct guesses; "
                            "stopping rather than repeating ourselves"
                        )
                        break
                else:
                    batch = _next_ports(state, HUB_SPRAY_PER_ROUND)

                    # The window cursor already never repeats, but record what
                    # it covered anyway: the blacklist is what the miss summary
                    # reads, and "covered 0 ports" would be a lie.
                    array.note_tried(batch)

                if not batch:
                    log("[punch] sym_to_cone: port walk exhausted")
                    break

                sent_total += array.send_from_hub(hello, peer_ip, batch)

                hit = await array.wait_hit(SPRAY_INTERVAL_S)

                if hit:
                    sock, addr = hit
                    array.detach(sock)
                    log(
                        f"[punch] sym_to_cone ok after {rnd} rounds: "
                        f"punched via port {addr[1]}"
                    )
                    return PunchResult(
                        sock=sock, addr=addr, plan=PLAN_SYM_TO_CONE,
                        sent=sent_total, learned=array.learned_addrs(),
                        elapsed=time.monotonic() - started,
                    )

                if array.learned:
                    array.reply_learned(hello_ack)
                    array.spray_learned_neighborhood(hello)

            log(
                f"[punch] sym_to_cone cone side miss after {rnd} rounds: "
                f"sent={sent_total} learned={len(array.learned)} "
                f"budget_left={budget.left} anchor={age_text}"
            )
            return PunchResult(
                plan=PLAN_SYM_TO_CONE, sent=sent_total,
                learned=array.learned_addrs(),
                elapsed=time.monotonic() - started, reason="window missed",
            )
    finally:
        array.close()


async def run_punch(peer_ip, peer_port, my_sub, peer_sub, hub_sock=None,
                    log=None, timeout=PUNCH_TIMEOUT_CAP_S,
                    my_filter=None, peer_filter=None,
                    sockets=SOCKETS_FOR_OPENP2P, per_ip_pool=False,
                    my_id="", peer_id="", anchor_age_s=None, guard=None,
                    start_at=None, step=None, lan=False):
    """
    Decide and run the right mechanism for this pair.

    `my_id` / `peer_id` decide who LEADS when both ends spray at once
    (`should_lead`) and who must speak first when the filters differ
    (`must_open_first`). Both sides compute the same answer without a round
    trip, which is why neither has to wait to find out.

    `anchor_age_s` is how stale the peer's published port is. It is logged,
    because "the anchor was 50s old" and "the peer never answered" look
    identical in a log that does not say which.

    `start_at` is the coordinated start instant both ends agreed on (his
    `startIn`, see await_punch_start). It is passed down to whichever mechanism
    runs; "no mechanism" ignores it, because there is nothing to synchronise.
    """
    log = log or (lambda *_a: None)

    # ==================================================================
    #  同网段的候选：**不套 NAT 分类**
    # ==================================================================
    #
    # NAT 类型是对"公网目的地"测出来的 —— 它描述的是数据包出了路由器之后
    # 怎么被改写。同一个 /24 里的两台机器之间根本不经过那条路：直接发就
    # 到了。把硬 NAT 的结论（对称 / 按目标 IP 分段）套到同网段邻居上，会把
    # 一次本来 1 个包就完成的握手变成喷 800 个包的端口扫描。
    #
    # 实测（本机回环 + 真互联网）：同一份代码，同网段候选用 NAT 分类时
    # `plan=s2c sent=801 elapsed=17.8s`，按同网段处理时
    # `plan=c2c sent=1 elapsed=1.5s`。用户要的"真局域网内的玩家不要走公网"
    # 就是这一条。
    #
    # 只影响这一个候选：LAN 打不通（VLAN 隔离、对端并不真在同一段），调用方
    # 会继续用公网候选，那条路仍然用真实的 NAT 分类。
    if lan:
        my_sub = NatSub.CONE
        peer_sub = NatSub.CONE
        my_filter = Filter.EIF
        peer_filter = Filter.EIF
        per_ip_pool = False

    plan = punch_plan(my_sub, peer_sub, my_filter, peer_filter)

    # Who goes first. His two rules, applied together:
    #
    #   * the STRICTER filter end must open the door, or both ends wait for
    #     the other and nothing ever arrives (must_open_first);
    #   * when the filters are equal, the id comparison decides, so the two
    #     ends cannot both open hundreds of sockets in the same millisecond.
    #
    # ★ 全锥证据只在打洞时收集（`EVIDENCE` 由房间协调器设置过）；没有它时
    #   `resolve_lead` 与 `should_lead` 完全等价 —— 其它调用方行为不变。
    lead = resolve_lead(my_id, peer_id, evidence_active=EVIDENCE is not None)

    if must_open_first(
        (my_filter or Filter.UNKNOWN).value,
        (peer_filter or Filter.UNKNOWN).value,
        my_id, peer_id,
    ):
        lead = True

    log(
        f"[punch] plan={plan} me={my_sub.value}/"
        f"{(my_filter or Filter.UNKNOWN).value} "
        f"peer={peer_sub.value}/{(peer_filter or Filter.UNKNOWN).value} "
        f"per_ip_pool={per_ip_pool} lead={lead} anchor={_anchor_age_text(anchor_age_s)}"
    )

    if plan == "openp2p":
        handshake = openp2p_plan(my_sub, peer_sub)
        return await openp2p_punch(
            peer_ip, peer_port, handshake, hub_sock=hub_sock, log=log,
            timeout=timeout, sockets=sockets, per_ip_pool=per_ip_pool,
            lead=lead, guard=guard, start_at=start_at,
            anchor_age_s=anchor_age_s, step=step,
        )

    if plan == PLAN_SYM_TO_CONE:
        return await sym_to_cone_punch(
            peer_ip, peer_port, hub_sock=hub_sock, log=log,
            timeout=timeout, sockets=SOCKETS_FOR_SYM_TO_CONE, my_sub=my_sub,
            peer_subtype=peer_sub.value, per_ip_pool=per_ip_pool,
            lead=lead, guard=guard, anchor_age_s=anchor_age_s,
            start_at=start_at, step=step,
        )

    if plan in (PLAN_BIRTHDAY, PLAN_BOTH_EASY):
        # BOTH_EASY: both ends predictable -> both open targets and both walk.
        # BIRTHDAY : neither is -> the only lever is the random collision.
        #
        # When the peer allocates per destination IP, a contiguous window is
        # provably wrong (its port for us is in a different region from the one
        # it published), so his design drops the window and opens
        # SOCKETS_FOR_COLLISION mappings instead.
        handshake = PLAN_C2C if plan == PLAN_BOTH_EASY else PLAN_S2C
        count = SOCKETS_FOR_COLLISION if per_ip_pool else sockets

        return await openp2p_punch(
            peer_ip, peer_port, handshake, hub_sock=hub_sock, log=log,
            timeout=timeout, sockets=count, per_ip_pool=per_ip_pool,
            lead=lead, guard=guard, start_at=start_at,
            anchor_age_s=anchor_age_s, step=step,
        )

    log("[punch] no mechanism for this pair (relay is the correct answer)")
    return PunchResult(plan=PLAN_NONE, reason="no mechanism for this pair")


__all__ = [
    "NatSub",
    "Filter",
    "PunchArray",
    "PunchResult",
    "await_punch_start",
    "openp2p_plan",
    "punch_plan",
    "openp2p_punch",
    "sym_to_cone_punch",
    "run_punch",
    "port_window",
    "random_ports",
    "pack_hello",
    "pack_hello_ack",
    # NAT detection lives in stun.py (the ported MCLanP2P stack). It used
    # to be duplicated here; the old copy was deleted on the project
    # owner's instruction so there is exactly one implementation.
    "INGRESS_MAGIC",
    "INBOUND_OPEN",
    "INBOUND_PUNCHABLE",
    "INBOUND_BLOCKED",
    "INBOUND_UNKNOWN",
    "is_our_packet",
    "MAGIC",
    "T_HELLO",
    "T_HELLO_ACK",
    "SOCKETS_FOR_OPENP2P",
    "SPRAY_PORTS",
]
