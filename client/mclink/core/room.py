"""
MC-Link client -- room membership.

============================================================================
 WHAT THIS IS
============================================================================

Being IN a room is separate from being connected to Minecraft. This module
owns the connection that represents "I am in room X":

    CREATE __mclink__ <client> <user> <token> <room>   -> I own this room
    JOIN   __mclink__ <client> <user> <token> <room>   -> I am a member
    CLAIM_HOST / RELEASE_HOST                          -> take/give the host slot
    LEAVE                                              -> walk out

The server keeps this connection open and pushes back:

    PONG <x>                              reply to our PING
    HOST_STATE host=0/1 members=<n> world=<n>
    ROOM REVOKED <reason>                 an admin destroyed the room

============================================================================
 WHY IT IS SEPARATE FROM THE MINECRAFT TUNNEL
============================================================================

A paired tunnel is raw Minecraft bytes: writing a control line into it would
corrupt the game protocol, and that path is frozen precisely so it can never
be broken. So membership gets its own small connection, which is also what
makes it possible to sit in a room -- watching the ping, waiting for a host --
without being in anybody's world.

The cost is one TCP connection per person in a room and a few dozen bytes of
state on the relay. It does not touch the Minecraft path at all.
"""

import asyncio
import json
import contextlib
import time
from dataclasses import dataclass, field

from .common import close_writer, enable_tcp_nodelay
from . import protocol as proto
from .protocol import room_name_from_token

#: How often we ping. The relay's watchdog tolerates three misses.
PING_INTERVAL = 3.0

#: 多久没有 PONG 就认定这条连接已经死了：两个完整的 ping 周期。
#: The relay answers every PING with PONG, so complete silence for two whole
#: intervals means the path is gone (half-open TCP after a NAT reboot / mobile
#: handover) even though the socket still looks open.
PONG_TIMEOUT = PING_INTERVAL * 2.0

#: Connection timeout.
CONNECT_TIMEOUT = 8.0

#: How long to wait for the OK after CREATE/JOIN.
REPLY_TIMEOUT = 10.0


class RoomError(Exception):
    """A room request failed. The message is user facing."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


# ============================================================
# 中继广播的两张附加表：每成员显示名 / 每成员中转 RTT
# ============================================================
#
# 格式由协调者冻结（老中继不发 -> 解析结果为空表，界面走回退，绝不报错）：
#
#     names=<cid>:<name>,<cid>:<name>      名字是 percent-encoded（可含中文/空格）
#     rtts=<cid>:<ms>,<cid>:<ms>           ms 整数；没有数据的成员整条不出现
#
# `cid` 是客户端自己生成的 ASCII 记号，**不可能含 `:`**，所以一律按**第一个**
# `:` 切分。这两张表是纯数据：一个畸形条目被跳过，不影响同一行里其它成员。

def parse_member_names(value):
    """
    `names=<cid>:<name>,...` -> {client_id: 显示名}。

    空名 / 没有 `:` 的畸形条目直接跳过（回退到 client_id 比显示一个空名字好）。
    名字做 percent-decode：用户名可以带中文、空格、emoji。
    """
    names = {}

    for item in str(value or "").split(","):
        item = item.strip()

        if not item:
            continue

        cid, sep, raw = item.partition(":")

        if not sep or not cid:
            continue

        name = room_name_from_token(raw) if raw else ""

        if name:
            names[cid] = name

    return names


def parse_member_rtts(value):
    """
    `rtts=<cid>:<ms>,...` -> {client_id: 毫秒(float)}。

    只接受**真实的非负数字**：中继没给这个成员（或给了个看不懂的值）时它不在
    表里，界面显示 `-`，绝不编一个数字出来。
    """
    rtts = {}

    for item in str(value or "").split(","):
        item = item.strip()

        if not item:
            continue

        cid, sep, raw = item.partition(":")

        if not sep or not cid:
            continue

        with contextlib.suppress(ValueError):
            ms = float(raw)

            if ms >= 0:
                rtts[cid] = ms

    return rtts


#: `conn=` 允许出现的全部取值（PROTOCOL §8.5）。**白名单**：中继哪天多给一个
#: 我们还不认识的状态，就当"这条没信息"跳过，而**不是**把它塞进界面显示一个
#: 看不懂的英文单词。
CONN_STATES = ("idle", "waiting", "pairing", "connected")

#: `mode_reason=` 允许出现的全部取值（PROTOCOL §8.9）。白名单的理由和 `conn=`
#: 一样：不认识的值 = 没有信息（保持 `""` = 未知），绝不当成 `default`。
MODE_REASON_OWNER = "owner"
MODE_REASON_RESTORED = "restored"
MODE_REASON_DEFAULT = "default"
MODE_REASONS = (MODE_REASON_OWNER, MODE_REASON_RESTORED, MODE_REASON_DEFAULT)


def describe_mode_reason(reason):
    """
    `mode_reason` -> 一句英文归因（日志正文必须是英文，见 gui_i18n_test）。

    未知 / 老中继（`""`）返回 `""`：宁可什么都不说，也不编一个原因。
    """
    return {
        MODE_REASON_OWNER: "the owner changed it explicitly",
        MODE_REASON_RESTORED: (
            "the room was recreated by the same owner "
            "(restored, not a manual change)"
        ),
        MODE_REASON_DEFAULT: "the room's default (nobody changed it)",
    }.get(str(reason or "").strip().lower(), "")


def parse_member_conns(value):
    """
    `conn=<cid>:<state>,...` -> {client_id: state}（PROTOCOL §8.5）。

    ============================================================================
      ★ 用户 2026-09-28：「玩家断开连接，host 还显示已连接，未及时更正」
    ============================================================================

    中继从这个成员**此刻的连接本身**现算，每一次变化立刻再广播一遍。客户端
    「连接状态」列以它为准 —— 这是"玩家断了，host 那边立刻不再显示已连接"的
    唯一来源。

    容错规则：
      * 按 `,` 切分，每条按**第一个** `:` 切分（cid 不可能含 `:`）；
      * 只接受白名单里的 4 个取值；不认识的整条跳过（连人一起跳过，不写成
        `idle` —— "不认识"不等于"没连"）；
      * 空值 / `-` -> 空表（老中继没有这个 token 时也走这条）。
    """
    conns = {}

    for item in str(value or "").split(","):
        item = item.strip()

        if not item:
            continue

        cid, sep, raw = item.partition(":")

        if not sep or not cid:
            continue

        state = raw.strip().lower()

        if state in CONN_STATES:
            conns[cid] = state

    return conns


@dataclass
class RoomState:
    """What the client knows about the room it is in."""

    #: 房间 token（房间 ID）：协议、删除房间、面板接口用的都是它。
    #: ASCII，玩家看不到它。
    room: str = ""

    #: 玩家看到的房间名（中文 / 空格 / emoji 都行）。界面上的每一句话都用它；
    #: 空的时候（老中继不回 name=）退回 room。
    room_name: str = ""

    client_id: str = ""
    username: str = ""
    is_owner: bool = False
    is_host: bool = False

    #: True when somebody (possibly us) holds the host slot, i.e. the room can
    #: be joined in Minecraft.
    host_claimed: bool = False
    #: 当前持有主机身份的人的名字（中继广播，可能为空 = 没人认领）。
    host_name: str = ""

    #: 当前 Host 的 **client_id**（中继新增字段 `host_id=`，ADDITIVE）。
    #:
    #: 为什么必须有它：BUG 1（房主≠host 的三人房）里，player 侧的"打洞目标"
    #: 以前是**猜**出来的——`room_direct._host_client_id()` 取花名册里第一个
    #: 不是自己的条目。房主（owner）排在 host 前面时，那个猜测把 owner 当成了
    #: host，于是 player 打向 owner，而中继把候选转给了真正的 host ——
    #: 一端显示"已打通"、另一端连不上。
    #:
    #: 老中继不发这个 token -> 空串 -> `punch_flow.host_id_of()` 退回
    #: "用 host_name 在 names= 表里反查"，再没有就返回空（**绝不猜**）。
    host_id: str = ""

    #: 房间里哪些成员**已开始连接**（中继新增字段 `started=`，ADDITIVE）。
    #:
    #: `None` = 老中继没有这个 token = **未知**（Host 退回"花名册所有人"的老
    #: 行为）；`[]` = 中继明确说了"现在一个都没开始"。两者语义完全不同，
    #: 所以这里用 None 而不是空列表 —— 见 `punch_flow.started_ids_of`。
    started_ids: list = None

    #: **Owner（建房的人）的 client_id**（中继新增字段 `owner_id=`，ADDITIVE）。
    #:
    #: ============================================================================
    #:  ★ 为什么必须有它（AGENTS.md 规则 6 §A：Owner 不可转移、不被继承）
    #: ============================================================================
    #:
    #: 界面以前是**猜** owner 的：`build_member_rows()` 里
    #: `owner_peer = peers[0]` —— "花名册里第一个对端就是房主"。那和 BUG 1
    #: 是同一种错误（把顺序当成身份），后果是**host 那一行被贴上了「房主」**，
    #: 也就是用户报的"host 错误继承了房主的位置/身份"（BUG 2 的客户端那一半）。
    #:
    #: 老中继不发这个 token -> 空串 -> 界面**不再猜**（对端只显示 主机/玩家，
    #: 不显示「房主」）；中继发了就精确匹配。
    owner_id: str = ""

    #: How many people are in the room, owner included.
    members: int = 0

    #: Client IDs of the OTHER members (additive `peers=` token from the relay).
    #:
    #: The host needs this to punch to EVERY guest. Without it its guests were
    #: anonymous, so only the first one ever got a direct link and everybody
    #: else waited forever at "正在打洞".
    peer_ids: list = None

    #: 中继广播的每成员【显示名】：client_id -> 名字（`names=` token，ADDITIVE）。
    #:
    #: 登录用户是中继记的登录名，免登录/游客是它分配的「游客xxxxxx」。老中继
    #: 不发这个 token -> 空表；界面回退到 client_id，绝不显示「未知」或空白。
    member_names: dict = field(default_factory=dict)

    #: 中继广播的每成员【到中继的 RTT】（毫秒）：client_id -> float（`rtts=`）。
    #:
    #: 中继自己量出来的（它发 `PING <token>`、成员回 `PONG <token>`），量不到的
    #: 成员**整条不出现** -> 界面显示 `-`。绝不拿别处的数字冒充。
    member_rtts: dict = field(default_factory=dict)

    #: 中继广播的每成员【游戏连接状态】：client_id -> state（`conn=`，PROTOCOL §8.5）。
    #:
    #: ============================================================================
    #:   用户 2026-09-28：「玩家断开连接，host 还显示已连接，未及时更正」
    #: ============================================================================
    #:
    #: 取值只有 `idle` / `waiting` / `pairing` / `connected` 四个（中继从连接
    #: 本身现算，每次变化立刻广播）。成员列表的「连接状态」列**以它为准**；
    #: 老中继没有这个 token -> 空表 -> 回退旧行为（房间里的人一律「已连接」）。
    member_conns: dict = field(default_factory=dict)

    #: 中继在这次登录取的【我自己的】显示名（`OK CREATE` / `OK JOIN` 的 `you=`，
    #: 其次到 `names=` 里按自己的 client_id 查）。游客也有名字。
    self_name: str = ""

    #: How many players are actually in somebody's world: the host counts as
    #: one as soon as they hold the slot, and each paired player adds one.
    world_players: int = 0

    #: This room's own capacity (the number the client sent when it created the
    #: room, clamped by the server limit). _request() fills these from the
    #: OK reply; they were assigned without being declared here.
    max_players: int = 0

    #: The server's per-room ceiling, for the "your setting is above the server
    #: limit" hint.
    server_max_players: int = 0

    #: 房间的连接模式（relay / stun），由房主决定，随 HOST_STATE 广播过来。
    #: 不是本地设置：同房间的人必须用同一种传输，否则一半直连一半中转而
    #: 且界面上分不出来。
    conn_mode: str = "relay"
    member_ips: dict = field(default_factory=dict)

    #: 中继对「当前 mode 为什么是这样」的**归因**（PROTOCOL §8.9，ADDITIVE）：
    #:
    #:   owner    = Owner 刚才显式 CONNMODE（唯一"有人切模式"的正常情况）；
    #:   restored = 房间销毁后由**同一个 Owner** 重建，`mode=` 沿用他上次显式
    #:              选过的值（房间参数属于 Owner，一次闪断不该把它换掉）；
    #:   default  = 全新房间的构造默认值（没人改过）。
    #:
    #: `""` = 老中继没有这个 token = **未知**（绝不当成 `default`）。
    #: 用途：归因与日志 —— `restored` 必须能和"有人在乱改模式"区分开。
    mode_reason: str = ""

    #: Set when the relay force-destroyed the room.
    revoked: bool = False
    revoked_reason: str = ""

    joined_at: float = 0.0

    @property
    def display(self):
        """界面上显示的房间名。"""
        return self.room_name or self.room

    def name_of(self, client_id):
        """
        某个成员的显示名。

        中继广播了就用它（登录名 / 游客名）；老中继没广播 -> **回退到 client_id**
        （那是我们真实知道的身份），绝不返回空串，也绝不显示「未知」。
        """
        cid = str(client_id or "")

        if not cid:
            return ""

        return (self.member_names or {}).get(cid) or cid

    @property
    def own_name(self):
        """
        我自己的显示名（含游客名）。

        优先级：中继在 OK 回包里给的 `you=` -> 广播表里按我的 client_id 查 ->
        我登录用的用户名 -> client_id。老中继 / 免登录时最后两级保证永远有值。
        """
        return (
            (self.self_name or "").strip()
            or (self.member_names or {}).get(self.client_id or "")
            or (self.username or "").strip()
            or self.client_id
            or ""
        )

    @property
    def can_play(self):
        """Whether the room currently has a host, so Minecraft can connect."""
        return self.host_claimed and not self.revoked

    def summary(self):
        if self.revoked:
            return "房间已被关闭"

        role = "房主" if self.is_owner else "成员"
        host = "持有房主身份" if self.is_host else (
            (
                f"主机：{self.host_name}"
                if self.host_claimed and self.host_name
                else ("有房主" if self.host_claimed else "无房主")
            )
        )

        return f"{self.display}（{role}，{host}，房间内 {self.members} 人）"


async def _room_exists(host, port, room_name):
    """
    One-shot "is this room still there?" question, used after a disconnect.

    Returns True / False, or None when the answer cannot be obtained (the relay
    itself is unreachable, in which case guessing would be worse than saying
    nothing).
    """
    if not room_name:
        return None

    from . import control_client

    try:
        rooms = await control_client.fetch_rooms(host, port, "", "")
    except Exception:
        return None

    return any(getattr(r, "room", "") == room_name for r in rooms)


class RoomSession:
    """
    The client half of a room membership connection.

    Usage:

        session = RoomSession(host, port, client_id, username, token, emit)
        await session.create("myroom")      # or .join("myroom")
        ...
        await session.claim_host()
        ...
        await session.leave()

    `emit` is called with (event_name, data) for things the GUI should show.
    It must never raise; a UI problem must not break the network loop.
    """

    def __init__(self, host, port, client_id, username, token="", emit=None,
                 max_players=0, is_current=None, guest_token="",
                 host_gone_hook=None):
        self.host = host
        self.port = int(port)
        self.client_id = client_id or ""
        self.username = username or ""
        self.token = token or ""

        # ============================================================================
        #  ★ v3.9.16：host 没了是**硬事实**，核心层必须自己动手拆
        # ============================================================================
        #
        # v3.9.15 及以前，这里只 `_notify()` 一声就完事 —— 真正的"停会话 +
        # 清隧道 + 重算按钮"全在 GUI 那一层，而 GUI 有三道窗口（静默 /
        # 重建中 / 待重建）会把这类事件**降级成一行日志**。于是中继明明已经
        # 把房间里的游戏连接全断了、会话也拆了，客户端的会话状态却还是
        # `ONLINE`（实测：收到事件 8 秒后仍是 ONLINE），玩家停在"已连接"。
        #
        # 所以现在核心层**自己动手**：收到这几个码时立刻调 `host_gone_hook`
        # （由 MCLinkEngine 注入），由它停会话 / 清直连通路。GUI 那层只负责
        # 文案与按钮，它吞不吞事件都不再决定"断不断"。
        self.host_gone_hook = host_gone_hook

        #: 最后一次"host 没了"的 (code, reason)；None = 没发生过。
        self.host_gone = None

        #: 中继第一次给我们的**游客临时 token**（`OK JOIN`/`OK CREATE` 回包尾部的
        #: `guest=<token>`，ADDITIVE；老中继不发 = 空）。
        #:
        #: 用户 2026-09-27 口径：游客身份是**登录会话级**的，
        #:   * 同一个登录会话里换房 / 重新进房 —— 把这个 token 放回握手行的
        #:     `token` 字段，中继就认得出"还是这个人"，**名字不变**；
        #:   * 退房 / 被踢 / 房间被关 —— **不清**（下一次进房还用）；
        #:   * LOGOUT / 登录态丢失 / 换账号 / 重启 —— 由上层清掉，下次拿新的。
        #: 它**只活在内存里**，绝不写进配置文件。
        self.guest_token = str(guest_token or "")
        self._emit = emit or (lambda *a, **k: None)

        #: 「我还是引擎当前用的那个房间吗？」
        #:
        #: 迟到 6 秒的 room_gone / room_lost 必须能问出这个问题：如果用户
        #: 已经退出、甚至又建了一个同名的新房间，那条迟到的结论说的是【旧】
        #: 房间，再用它去拆界面就会把新房间一起拆掉。
        self._is_current = is_current

        #: 这个房间用几个人（客户端自己的设置）。服务器的数字是上限，
        #: 建房时把这个值报给中继，房间就按它算容量。
        self.max_players = int(max_players or 0)

        self.state = RoomState(
            client_id=self.client_id,
            username=self.username,
        )

        self.reader = None
        self.writer = None
        self.running = False
        self.last_error = ""

        #: 最后一次收到 PONG 的时间（monotonic）。见 _ping_loop 的看门狗。
        self._last_pong = 0.0

        #: 上一次记过日志的 (conn_mode, mode_reason) —— 归因日志只在它变了时
        #: 写一行，绝不每条 HOST_STATE 都刷屏。None = 还一条都没记过。
        self._last_mode_reason = None

        #: 本机 ↔ 中继的往返时间（毫秒），由 PING/PONG 的**回声**量出来：中继把
        #: 我们发过去的 token 原样回给我们，两个时刻相减就是这条路真实的 RTT。
        #: 房间成员列表的「延迟」列用它；量不到就是 None（界面显示 `-`）。
        self.relay_rtt_ms = None

        #: 被中继踢出（KICKED）时填上，界面据此拆干净并告知用户。
        self.kicked = False
        self.kicked_by = ""
        self.kicked_reason = ""

        self._tasks = []
        self._send_lock = asyncio.Lock()

        #: Set when ROOM REVOKED arrives, so the GUI and the tunnel can react.
        self.revoked = asyncio.Event()

        #: 事件发不出去时记在这里（见 _notify）。空列表 = 一切正常。
        self.emit_errors = []

        #: Signalled on every HOST_STATE, for callers that want to wait.
        self.state_changed = asyncio.Event()

        self._pending = {}   # "CLAIM_HOST" -> asyncio.Future

        #: 我**最后一次上报**给中继的连接状态（`STATE <...>`，纯显示）。
        #: 服务端据此填 HOST_STATE 的 `conn=`，成员列表照它显示。
        self._reported_conn = ""

        #: 上一次上报的时间（monotonic），用于"值没变也要 1Hz 心跳"的节流。
        self._reported_conn_at = 0.0

        #: V3 直连协调：中继转发过来的候选/协调行。
        #:
        #: 用队列而不是 future：这些行是"推"的，而且可能比我们开始等更早
        #: 到达（房主先发候选、玩家后加入）。future 会丢掉早到的那一条，
        #: 而队列不会 —— 丢一条候选 = 这次直连直接失败。
        self._direct_lines = asyncio.Queue()

        #: 每个对端**最后一次发来直连协调行**的时刻（monotonic），按 `from=` 归属。
        #:
        #: ============================================================================
        #:  ★ 为什么要在**收到的那一刻**记，而不是在 peek 的时候记
        #: ============================================================================
        #: `peek_direct()` 是"取出来再放回去"的非破坏性读，所以同一条旧信标会被
        #: 反复读到。如果新鲜度是在读的时候盖时间戳，一个早就不在连接的 peer 会
        #: 靠队列里那条旧行**永远保持新鲜**，永远留在 Host 的目标表里（用户第三轮
        #: 第 6 条：被踢的玩家一回来就被无限打洞、§E 一轮一轮空转）。
        #:
        #: 收到的那一刻记，就只反映"他真的还在发信标"= 他真的还在连接。
        #: 只读用途：`RoomDirectLink._beacon_fresh()` 拿它判"他现在还在连吗"。
        self.direct_rx_at = {}

        #: 一次性等待者：set_conn_mode 用它等 OK CONNMODE。
        self._waiters = []

        #: The inbound-probe confirmation. The relay answers
        #: `OK DIRECT_INGRESS` / `ERROR DIRECT_INGRESS` and this is what
        #: probe_ingress waits on. Kept as an explicit handshake rather than a
        #: timed guess: "the relay never answered" and "the relay sent it and it
        #: was filtered" need opposite conclusions about the user's network.
        self._ingress_ack = None
        self._ingress_result = None

    # --------------------------------------------------------
    # Events
    # --------------------------------------------------------

    def _notify(self, event_name, **data):
        """
        发一个事件给上层（引擎 → 界面）。

        ============================================================================
         失败绝不能静默
        ============================================================================

        This used to swallow every exception. When the emit convention did not
        match the engine's (`emit(name, data)` vs `_push(kind, **payload)`),
        EVERY room event was dropped with nobody the wiser: the room could be
        destroyed server-side and the window kept showing "you are in a room".

        So failures are recorded in `emit_errors`, and the GUI reports them.
        """
        name = event_name
        manager = getattr(self, "auto_manager", None)
        if manager and name in ("kicked", "room_revoked", "room_closed", "room_lost", "room_gone"):
            manager.close()
        try:
            self._emit(name, data)
        except Exception as e:  # noqa: BLE001
            self.emit_errors.append(f"{name}: {e!r}")

    def _host_gone_teardown(self, code, reason=""):
        """
        【v3.9.16】核心层动手拆：把"host 没了"变成**已经断开**这个事实。

        ============================================================================
          为什么必须有它
        ============================================================================
        v3.9.15 的这条链路上，"停会话"只写在 GUI 那一层，而 GUI 有三道窗口
        （`_mode_switch_silent` 45 秒 / `_rebuild_in_flight` / `_mode_rebuild_pending`）
        会把 `stopped`/`released` 降级成一行日志。实测：中继已经把会话拆了、
        游戏 socket 也关了，客户端这边 8 秒后 `snapshot()` 仍是
        `state=ONLINE`，玩家一直停在"已连接"。

        所以拆的动作下沉到核心层：这里**不等、不判断、不看任何窗口**，收到
        就拆。界面那层从此只决定"提示什么时候显示"。

        ============================================================================
          绝不阻塞
        ============================================================================
        这一段跑在控制连接的读循环里。任何 `await` / 阻塞都会让整条控制连接
        哑掉（那正是"通知发不出去"的一种表现），所以：

          * 这里**只**调用引擎注入的 hook（同步、立即返回）；
          * 真正的 `session.stop()` 由 hook 在事件循环里排一个协程去做；
          * hook 抛异常只记日志 —— 拆不动也要把界面通知出去（见调用处）。
        """
        if self.host_gone_hook is None:
            self.log(
                f"[room] host gone ({code}) but the engine gave us no teardown "
                "hook; falling back to the GUI only (pre-3.9.16 behaviour)"
            )
            return

        try:
            self.host_gone_hook(code, reason or code)
        except Exception as e:  # noqa: BLE001
            self.log(f"[room] host-gone teardown failed: {e!r}")

    def log(self, message):
        self._notify("log", text=message)

    def _note_mode_reason(self):
        """
        把「模式是谁改的」写进日志（PROTOCOL §8.9 的 `mode_reason=`）。

        ============================================================================
          `restored` 必须和"有人在乱改模式"区分开
        ============================================================================
        用户报过的 P0 是「没有任何人操作，模式自己跳回了 relay」。中继现在如实
        广播 `mode_reason=owner|restored|default`，客户端据此归因：

          * `owner`    —— Owner 的显式操作，正常切模式；
          * `restored` —— 房间被销毁后由**同一个 Owner** 重建，沿用他上次选过的
            模式。这是"恢复"，不是"有人改的"；日志必须说清楚。
          * `default`  —— 全新房间的默认值，没人改过。

        只在 `(conn_mode, mode_reason)` 真的变了时写一行（每秒一条广播会刷屏）；
        老中继没有 `mode_reason`（`""`）时一行都不写，行为与以前一致。
        """
        mode = str(getattr(self.state, "conn_mode", "") or "")
        reason = str(getattr(self.state, "mode_reason", "") or "")
        key = (mode, reason)

        if key == self._last_mode_reason:
            return

        self._last_mode_reason = key

        text = describe_mode_reason(reason)

        if not text:
            return

        self.log(f"[mode] room mode {mode or '?'}: {text}")

    # --------------------------------------------------------
    # Connect / disconnect
    # --------------------------------------------------------

    async def _connect(self):
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RoomError(
                f"连接中转服务器超时（{self.host}:{self.port}）"
            )
        except OSError as e:
            raise RoomError(f"无法连接中转服务器（{self.host}:{self.port}）：{e}")

        enable_tcp_nodelay(self.writer)

    async def _request(self, role, room, want_ok):
        """
        Send a CREATE/JOIN line and read the OK.

        The connection deliberately stays open afterwards; only the reply is
        consumed here.
        """
        await self._connect()

        # The trailing capacity is ADDITIVE and optional: an older relay
        # ignores it, and a room created without it follows the server setting.
        # It is what the player configured -- the server's number is the
        # ceiling, not the value every room has to use.
        capacity = f" {int(self.max_players)}" if self.max_players else ""

        # 版本门：服务端开着门时，这一行必须带 `ver=`，否则直接拒（"旧版
        # 不给用"）。放在**最后一个 token**，所以前面那些按位置解析的字段
        # （client_id / username / token / room / capacity）一个都没挪位。
        from ..config import settings as _cfg

        # ================================================================
        #  免登录的游客：**同一个登录会话**里报上中继给的临时 token
        # ================================================================
        #
        # 用户 2026-09-27 口径：游客身份是登录会话级的 —— 换房 / 重新进房
        # 名字必须不变，退房 / 被踢 / 房间被关都不销毁它。中继认的就是这个
        # token（它在首次分配游客名时随 `OK JOIN` 尾部发下来，见 guest_token）。
        #
        # 老中继不发这个字段 -> 这里就是空 -> 原样用 `-`，行为与以前一字不差。
        handshake_token = self.token or self.guest_token or "-"

        line = (
            f"{role} {proto.SENTINEL} "
            f"{self.client_id or '-'} {self.username or '-'} "
            f"{handshake_token} {proto.room_token(room)}{capacity}"
            f" caps=auto_stream_v1 ver={_cfg.APP_VERSION}"
        )

        self.writer.write((line + "\n").encode())
        await self.writer.drain()

        try:
            raw = await asyncio.wait_for(
                self.reader.readline(), timeout=REPLY_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise RoomError("服务器没有响应，请稍后再试")

        if not raw:
            raise RoomError("服务器没有响应就关闭了连接")

        text = raw.decode("utf-8", errors="replace").strip()

        if text.upper().startswith(proto.LINE_ERROR):
            code = text.split()[1].upper() if len(text.split()) > 1 else ""

            raise RoomError(
                proto.describe_error(text),
                code or "ROOMS_FAILED",
            )

        if not text.startswith(want_ok):
            raise RoomError(f"服务器应答异常：{text}")

        # OK JOIN room=<房间ID> name=<房间名> owner=0/1 host=0/1 members=<n>
        #
        # 本地先按输入的房间名算一份，中继回包再覆盖：老中继不回 name=，
        # 那种情况下客户端至少还知道自己输入的名字。
        self.state.room_name = room_name_from_token(proto.room_token(room))

        for token in text.split()[2:]:
            key, _, value = token.partition("=")

            if key == "room" and value:
                self.state.room = value
            elif key == "name" and value:
                # 中继是权威：名字以它为准（同名 → 同一个房间）。
                self.state.room_name = room_name_from_token(value)
            elif key == "you" and value:
                # ============================================================
                #  中继给我分配的显示名（ADDITIVE，老中继不发）
                # ============================================================
                #
                # 免登录/游客连接拿到的是中继分配的「游客xxxxxx」；登录用户是它的
                # 登录名。percent-encoding，和房间名同一套规则。
                self.state.self_name = room_name_from_token(value)
            elif key == "guest" and value:
                # ============================================================
                #  游客临时 token（ADDITIVE，老中继不发）
                # ============================================================
                #
                # 中继**首次**分配游客名时随回包发下来。存住它，同一个登录会话里
                # 换房 / 重新进房时放进握手行的 `token` 字段 -> 中继认出"还是
                # 这个人" -> 名字不变（用户 2026-09-27 口径）。
                #
                # 只存内存、不落盘；退房 / 被踢 / 关房不清，登出 / 换账号 /
                # 重启由 GUI 清（见 app.py 的 _forget_guest_identity）。
                self.guest_token = str(value)
            elif key == "owner":
                self.state.is_owner = value == "1"
            elif key == "host":
                self.state.host_claimed = value == "1"
            elif key == "host_id":
                # `OK JOIN` / `OK CREATE` 回包里的当前 Host client_id（§8.2）。
                # 和 `HOST_STATE` 的同一个字段同源 —— 进房那一刻就能精确知道
                # 谁是 Host，不用等第一条广播，也**不用猜**。
                self.state.host_id = "" if value in ("", "-") else value
            elif key == "members":
                with contextlib.suppress(ValueError):
                    self.state.members = int(value)
            elif key == "max_players":
                # 这个房间自己的容量（服务器按客户端设置给的，已被上限夹住）
                with contextlib.suppress(ValueError):
                    self.state.max_players = int(value)
            elif key == "server_max_players":
                with contextlib.suppress(ValueError):
                    self.state.server_max_players = int(value)

        # The owner always holds the host slot implicitly? No -- ownership and
        # hosting are independent, so nothing is assumed here.

        self.state.joined_at = time.time()
        self.running = True

        # 看门狗的起算点：从"刚进房间"开始算，第一条 PING 大约 3 秒后发出，
        # 中继一定回 PONG。两个周期内一条都没有 = 这条路已经不通了。
        self._last_pong = time.monotonic()

        self._tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._ping_loop()),
            asyncio.create_task(self._auto_report_loop()),
        ]

        # Back to the caller in the shape it asked for: this is the ACK TUPLE,
        # not the state. create() reads the ok flag from it before optionally
        # seeding the room's connection mode, and only then returns the state.
        return True, text

    async def create(self, room, conn_mode=""):
        """
        Create a room and become its OWNER. The room dies when we leave.

        `conn_mode` seeds the room's transport with the creator's choice.

        WHY IT HAS TO BE SENT AT CREATE TIME: the relay's default is "relay",
        and the room's mode is authoritative (only the host may change it, and
        everyone displays what the relay reports). Without this, a host who
        picked STUN before creating would be snapped straight back to relay by
        the first HOST_STATE -- which reads as "the selector is locked to
        relay and I cannot open a STUN room".
        """
        ok, message = await self._request(
            proto.ROLE_CREATE, room, proto.LINE_ACK_JOIN
        )

        if ok and conn_mode in ("relay", "stun", "auto"):
            # Best effort: the room exists either way, and a failure here just
            # leaves it on the relay default (the host can still switch).
            if conn_mode == "auto":
                changed, reason = await self.set_conn_mode(conn_mode)
                if not changed or self.state.conn_mode != "auto":
                    await self.leave()
                    raise RoomError(reason or "服务器不支持 AUTO")
            else:
                with contextlib.suppress(Exception):
                    await self.set_conn_mode(conn_mode)

        # The STATE, not `(ok, message)`: every caller (engine.open_room, the
        # auto-rejoin path, the GUI) expects the same RoomState that join()
        # returns. Returning the raw ack tuple here made "create a room" fail
        # with "cannot unpack non-iterable RoomState object".
        return self.state

    async def set_capacity(self, value):
        """
        Tell the relay how many people THIS room takes.

        The server's number is the ceiling; this is the player's own setting,
        and sending it is what keeps the room list, the tunnel count and the
        client's own display on the same number.
        """
        if not self.running or self.writer is None:
            return False, "还没有进入房间"

        try:
            wanted = int(value)
        except (TypeError, ValueError):
            return False, "人数必须是数字"

        try:
            self.writer.write(f"MAXPLAYERS {wanted}\n".encode())
            await self.writer.drain()
        except Exception as e:  # noqa: BLE001
            return False, f"发送失败：{e}"

        self.max_players = wanted

        return True, f"房间人数已设为 {wanted}"

    async def join(self, room):
        """
        Enter an existing room as a member.

        Returns the ROOM STATE, exactly like create(). Both entry points have to
        hand the engine the same shape: `engine.open_room` forwards whatever it
        gets straight to the GUI, so the raw `(ok, message)` ack tuple made
        "加入房间" fail with "cannot unpack non-iterable RoomState object".
        """
        ok, message = await self._request(
            proto.ROLE_JOIN, room, proto.LINE_ACK_JOIN
        )

        return self.state

    async def leave(self):
        """
        Walk out of the room.

        Sends LEAVE so the relay can free the host slot immediately, then
        closes. If the send fails the socket close still tells the relay, just
        one watchdog tick later.
        """
        if self.writer is not None and self.running:
            with contextlib.suppress(Exception):
                self.writer.write(b"LEAVE\n")
                await self.writer.drain()

                raw = await asyncio.wait_for(
                    self.reader.readline(), timeout=3
                )

                if raw and not raw.decode(
                    "utf-8", errors="replace"
                ).strip().startswith(proto.LINE_ACK_LEAVE):
                    pass  # best effort; the close below is what matters

        await self.stop()

    async def stop(self):
        """Close without the polite LEAVE. Used on shutdown."""
        self.running = False

        for task in self._tasks:
            task.cancel()

        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        self._tasks = []

        for future in self._pending.values():
            if not future.done():
                future.cancel()

        self._pending.clear()

        await close_writer(self.writer)

    async def _leave_after_revoke(self, reason):
        """
        Finish a server-side close from OUR side.

        ============================================================================
         注意：这个方法【目前不在活动路径上】
        ============================================================================

        Nothing calls it any more. The live path is: ROOM REVOKED arrives in
        _read_loop -> the GUI is told (room_revoked) -> the GUI tears the room
        down in its own tick and calls engine.close_room() -> leave() below,
        which sends the polite LEAVE and closes the socket. Wiring this method
        up as well would put a second LEAVE/room_closed path next to the
        verified ghost-room fix, for no behavioural gain.

        It is kept because the sequence it documents is still the intended one:

          1. give the notification a moment to be delivered (it arrived, we are
             handling it, but the GUI has not processed its event yet)
          2. say LEAVE -- a normal, polite exit the relay is waiting for; it is
             what makes "关闭" count as cooperative rather than a severed cable
          3. close our own socket
          4. tell the GUI, so it drops the room, stops the Minecraft side, and
             makes the buttons usable again

        Every step tolerates failure: the relay has a timeout of its own and will
        force-close us if any of this does not happen.
        """
        with contextlib.suppress(Exception):
            await asyncio.sleep(0.2)

            if self.writer is not None:
                self.writer.write(b"LEAVE\n")
                await self.writer.drain()

        with contextlib.suppress(Exception):
            await self.stop()

        self._notify("room_closed", reason=reason or "房间已被关闭")
        self.writer = None
        self.reader = None

    # --------------------------------------------------------
    # Host slot
    # --------------------------------------------------------

    # --------------------------------------------------------
    # V3 direct coordination
    # --------------------------------------------------------

    async def send_direct(self, line):
        """
        Send one direct-coordination line to the peer (via the relay).

        Returns True when it went out. Never raises: a direct path that cannot
        be negotiated must not be able to break a room that is working.
        """
        if self.writer is None or not self.running:
            return False

        try:
            self.writer.write((line + "\n").encode())
            await self.writer.drain()
            return True
        except Exception:
            return False

    def report_conn_state(self, state, max_age=5.0):
        """
        上报**我自己**的连接状态：`STATE <idle|waiting|pairing|connected>`。

        ============================================================================
          ★ 用户 2026-10-01 第 3 条：成员列表的「连接状态」以客户端自述为准
        ============================================================================

        连接到底通没通，只有客户端自己最清楚（STUN 模式下数据根本不走中继）。
        所以由客户端读**本地**状态、在变化时上报一次（另有 1Hz 心跳，和成员列表
        刷新同频），中继存下来并在 `conn=` 里优先用它。

        **纯显示**：这条信令不影响配对 / 打洞 / 主机位——服务端只把它写进
        HOST_STATE 的 `conn=`。不认识的旧中继会把它当未知行忽略（我们退化成
        旧行为），所以是 ADDITIVE。

        返回 True 表示这一行真的写出去了。
        """
        state = str(state or "").strip().lower()

        if state not in ("idle", "waiting", "pairing", "connected"):
            return False

        if self.writer is None or not self.running:
            return False

        now = time.monotonic()

        if state == self._reported_conn and (
            now - float(self._reported_conn_at or 0.0)
        ) < float(max_age or 0.0):
            # 值没变、也没到重发间隔 -> 不发（中继只在变化时广播，本来也不刷屏）。
            return False

        try:
            self.writer.write(f"STATE {state}\n".encode())
        except Exception:
            return False

        self._reported_conn = state
        self._reported_conn_at = now

        return True

    def peek_direct(self, keywords=("DIRECT_HIT", "DIRECT_CAND", "DIRECT_PUNCH")):
        """
        看一眼直接协调队列里现在有什么（**不改动内容**），返回命中的行。

        ============================================================================
          为什么需要"看一眼"（BUG 4 的另一半）
        ============================================================================

        Host 在"还没有已开始连接的玩家"时**不许**打洞（基线 §D），于是它不会
        进 `_wait_synced_start`，也就没有机会通过 `wait_direct` 收到对端的
        第一条信标。可是那条信标正是"这个 peer 已经点了开始连接"的**硬证据**
        （见 `punch_flow.beacon_line` 的 `from=`）。

        所以 Host 的空转回合里要能"看一眼"队列：读到谁发过话，就把谁加进目标。

        实现是"取出来再放回去"（同步、不 await）：调用点都在引擎事件循环上，
        和 `wait_direct` 是同一条线程，不会抢包。行的相对顺序可能旋转，但
        `wait_direct` 本来也不保证顺序（它自己也是取出来再放回）。
        """
        wanted = tuple(str(k).upper() for k in (keywords or ()))
        held = []
        found = []

        while True:
            try:
                line = self._direct_lines.get_nowait()
            except Exception:
                break

            held.append(line)

            head = line.split()[0:1]

            if not wanted or (head and head[0].upper() in wanted):
                found.append(line)

        for line in held:
            with contextlib.suppress(asyncio.QueueFull):
                self._direct_lines.put_nowait(line)

        return found

    async def wait_direct(self, keywords, timeout=10.0):
        """
        Wait for a forwarded line whose keyword is one of `keywords`.

        Lines that do not match are put BACK, so a caller waiting for
        DIRECT_CAND does not swallow the DIRECT_PUNCH that arrives behind it.
        """
        wanted = tuple(k.upper() for k in keywords)
        deadline = time.monotonic() + timeout
        held = []

        try:
            while True:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    return None

                try:
                    line = await asyncio.wait_for(
                        self._direct_lines.get(), timeout=remaining
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    return None

                if line.split()[0:1] and line.split()[0].upper() in wanted:
                    return line

                held.append(line)
        finally:
            for line in held:
                with contextlib.suppress(asyncio.QueueFull):
                    self._direct_lines.put_nowait(line)

    async def probe_ingress(self, line, timeout=6.0):
        """
        Ask the relay to fire ONE datagram at our published endpoint, and tell
        us whether it actually did.

        Returns True when the relay confirms sending, None when it never
        answered. None is NOT a failure of our network, and the caller says so:
        an older relay ignores the keyword, and reporting "blocked" for that
        blames the user's firewall for an un-upgraded server.

        His server answers `inbound_probe_sent` unconditionally and for exactly
        this reason -- "telling a user their firewall is at fault when the real
        cause is an un-upgraded server is worse than saying nothing".
        """
        if self.writer is None or not self.running:
            return None

        # A fresh event every call: a leftover set from the previous probe would
        # make the next one report success it did not observe.
        ack = asyncio.Event()
        self._ingress_ack = ack
        self._ingress_result = None

        try:
            if not await self.send_direct(line):
                return None

            try:
                await asyncio.wait_for(ack.wait(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                return None

            return bool(self._ingress_result)
        finally:
            self._ingress_ack = None

    async def drain_direct(self, keep=("DIRECT_PUNCH", "DIRECT_CAND")):
        """
        Drop the junk that an earlier round left in the queue — **but not the
        two lines that actually matter**.

        ============================================================================
          2026-09-25：原来这里是"全丢"，代价是丢掉了对端已经到的那条公告
        ============================================================================
        用户实测（重启之后一边早十几二十秒点）：两边都在等对方的
        `DIRECT_PUNCH`，而它可能**在上一轮就已经到了队列里** —— 全丢之后我们
        要一直等到对端**下一轮**再说（对端那时可能正在 7 秒的测量里），于是
        "要七八次才连上"。

        `DIRECT_CAND` 更不能丢：那是对方的地址，丢了就真的没得打了。

        所以现在只清"旧一轮留下的杂项"，并且对这两类关键字**保留最后一条**：
          * `DIRECT_PUNCH` 的语义是"从我到你那一刻起 +startIn"，一条旧公告
            只会让我们"现在就开打"（而不是傻等），这比没有公告更接近正确；
          * 对端在等待期间每秒重播一次公告，所以吃了一条旧的也不影响对齐。

        Returns how many lines were dropped, so the caller can say so.
        """
        kept = []
        dropped = 0
        wanted = tuple(k.upper() for k in (keep or ()))

        while True:
            try:
                line = self._direct_lines.get_nowait()
            except asyncio.QueueEmpty:
                break

            keyword = (line.split()[0:1] or [""])[0].upper()

            if keyword in wanted:
                # 同类只留最后一条（最新的那条覆盖前面的）。
                kept = [item for item in kept
                        if (item.split()[0:1] or [""])[0].upper() != keyword]
                kept.append(line)
            else:
                dropped += 1

        for line in kept:
            self._direct_lines.put_nowait(line)

        return dropped

    async def set_conn_mode(self, mode):
        """
        Ask the relay to change the room's connection mode. HOST ONLY.

        Returns (ok, message). The relay is the authority: a non-host gets
        ERROR NOT_HOST even if the client UI were bypassed, so the room cannot
        be split into mismatched transports.
        """
        if mode not in ("relay", "stun", "auto"):
            return False, "模式必须是 relay 或 stun"

        try:
            self.writer.write(f"{proto.ROLE_CONNMODE} {mode}\n".encode())
            await self.writer.drain()
        except Exception as e:
            return False, f"发送失败：{e}"

        line = await self.wait_line(
            (proto.LINE_ACK_CONNMODE, "ERROR"), timeout=6.0
        )

        if line is None:
            return False, "中继没有回应"

        if line.upper().startswith(proto.LINE_ACK_CONNMODE):
            for token in line.split()[2:]:
                key, _, value = token.partition("=")

                if key == "mode" and value in ("relay", "stun", "auto"):
                    self.state.conn_mode = value

            self._notify("conn_mode", mode=self.state.conn_mode)
            return True, "连接模式已切换"

        return False, line

    async def wait_line(self, prefixes, timeout=6.0):
        """
        Wait for a control line starting with one of `prefixes`.

        Used by set_conn_mode. The membership connection already has a reader
        loop, so this parks a one-shot queue and lets _on_line fill it.
        """
        wanted = tuple(p.upper() for p in prefixes)
        queue = asyncio.Queue()
        self._waiters.append((wanted, queue))

        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None
        finally:
            with contextlib.suppress(ValueError):
                self._waiters.remove((wanted, queue))

    async def claim_host(self):
        """
        Try to take the host slot. Returns (ok, message).

        The relay decides: first come, first served, one at a time. A refusal
        is a normal answer (somebody else hosts), not an error.
        """
        return await self._control_request(
            "CLAIM_HOST", proto.LINE_ACK_CLAIM_HOST
        )

    async def release_host(self):
        """Give the host slot up, keeping the room membership."""
        return await self._control_request(
            "RELEASE_HOST", proto.LINE_ACK_RELEASE_HOST
        )

    def can_send_identity(self):
        """现在这条房间连接上发 IDENTITY 有意义吗（在房间里 + 连接还活着）。"""
        return bool(self.running and self.writer is not None)

    async def send_started(self, connecting):
        """
        告诉中继"我点了开始连接 / 我停了"（`STARTED <1|0>`，PROTOCOL §8.6）。

        ============================================================================
          ★ 用户 2026-09-28 第 5/8 条：房间里有这个人 ≠ 他已经开始连接
        ============================================================================

        中继原来的 `started=` 只按**连接本身**现算（有 PLAYER 连接 / 有开服控制
        连接）。可这套客户端是"先点开始连接打洞、游戏连接是之后的事"，于是
        "点了开始、正在打洞、还没开游戏"的玩家在中继眼里**根本没开始** ——
        Host 拉目标时把他过滤掉，洞永远打不起来（见 CLIENT-PUNCH-FLOW §1.1）。

        显式上报把用户意图说清楚，中继把它并入 `started=`：

          * `STARTED 1` = 我点了「开始连接」；
          * `STARTED 0` = 我停了（用户停 / 退房 / 离开房间）。

        纯新增：老中继不认识这一行 -> 忽略（这里超时返回 False，**绝不影响
        连接**）；老客户端从不发 -> 中继行为逐字不变。

        返回 (ok, message)；调用方**不需要**关心失败 —— 它只是让目标表更准。
        """
        if not self.running or self.writer is None:
            return False, "还没有加入房间"

        line = f"{proto.LINE_STARTED} {1 if connecting else 0}"

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self._pending["STARTED"] = future

        try:
            async with self._send_lock:
                self.writer.write((line + "\n").encode())
                await self.writer.drain()

            try:
                return await asyncio.wait_for(future, timeout=REPLY_TIMEOUT)
            except asyncio.TimeoutError:
                # 老中继会直接忽略这条行（连 ERROR 都不回）。说清楚"没被确认"，
                # 界面照旧靠连接现算的 started= 工作。
                return (
                    False,
                    "服务器没有确认开始连接上报（旧版中继会忽略这条消息）",
                )
        finally:
            self._pending.pop("STARTED", None)

    async def send_identity(self, username, token=""):
        """
        告诉中继"我这个 client_id 现在的显示名是这个"，让它更新房间名册。

        ============================================================================
          用户要求（docs/ISSUES-20260927.md 问题 5）：身份绑在设备上
        ============================================================================

        用户原话：「玩家身份绑定设备，无论处于游客还是已登录的状态，加入房间的
        都是这个人，仅信息发生改变。」

        中继的显示名原来**只在 CREATE/JOIN 握手那一刻取一次**（`_request()` 里
        把 `self.username` 写进握手行），所以人在房间里登录之后，名册里还是
        「游客xxxxxx」，要退出再进房才更新（日志证据：craftlink-client.log
        `12:22:59 self_name='游客45teiy'` → 只有重新握手后才变 `root`）。

        这条消息是**纯新增**（V3-AGENTS §1：只许新增）：
          * 老中继不认识 `IDENTITY` → 忽略，房间照旧；
          * 老客户端从不发 → 行为一点没变；
          * `ver=` 按 §0.1 挂在**整行最后一个 token**，位置解析的字段一个没挪。

        返回 (ok, message, name)：
          * ok=True  → name 是中继确认的显示名（成功时用它刷新界面）；
          * ok=False → message 是给用户看的中文原因，**不影响连接**。

        调用方必须在"房间已加入 + 登录状态真的变了"时调用，且自己防抖
        （见 `gui/app.py::_sync_identity`）—— 这个函数本身不做去重。
        """
        if not self.can_send_identity():
            return False, "还没有加入房间", ""

        from ..config import settings as _cfg

        name = str(username or "").strip() or "-"
        tok = str(token or "").strip() or "-"

        line = f"{proto.LINE_ROLE_IDENTITY} {name} {tok} ver={_cfg.APP_VERSION}"

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self._pending["IDENTITY"] = future

        try:
            async with self._send_lock:
                self.writer.write((line + "\n").encode())
                await self.writer.drain()

            try:
                ok, message, ack_name = await asyncio.wait_for(
                    future, timeout=REPLY_TIMEOUT
                )
            except asyncio.TimeoutError:
                # 超时不是错误答案：老中继会直接忽略这条行（连 ERROR 都不回）。
                # 说清楚"没被确认"，让界面照旧用原来的名字。
                return (
                    False,
                    "服务器没有确认改名请求（旧版中继会忽略这条消息）",
                    "",
                )
        finally:
            self._pending.pop("IDENTITY", None)

        if ok:
            # 中继认了这个名字：自己的显示名当场生效，不用等下一次广播。
            if tok != "-":
                self.token = tok
                if self.guest_token != tok:
                    self.guest_token = ""
            self.username = "" if name == "-" else name
            self.state.username = self.username
            self.state.self_name = ack_name or self.username
            self.state_changed.set()
            self._notify("identity_updated", name=self.state.self_name)
        else:
            self.last_error = message
            self.log(f"[room] IDENTITY rejected: {message}")

        return ok, message, (ack_name if ok else "")

    async def _control_request(self, word, want_ok):
        if not self.running or self.writer is None:
            return False, "还没有加入房间"

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self._pending[word] = future

        try:
            async with self._send_lock:
                self.writer.write((word + "\n").encode())
                await self.writer.drain()

            try:
                return await asyncio.wait_for(future, timeout=REPLY_TIMEOUT)
            except asyncio.TimeoutError:
                return False, "服务器没有响应，请稍后再试"
        finally:
            self._pending.pop(word, None)

    async def kick(self, target_client_id):
        """
        把一个成员踢出房间：`KICK <room_token> <target_client_id>`。

        ============================================================================
          这一步走的是【房间成员连接】，不是删房那条路
        ============================================================================

        KICK 和 DELETE（删房）是两件事：踢人只关掉**那一个人**的成员连接 +
        他的游戏隧道，房间继续存在。只有被踢的是所有者时房间才解散 —— 那条路
        由面板/管理 API 走，客户端这里按钮根本不提供（见 gui 的权限矩阵）。

        服务端是权威：即使界面把按钮显示错了，也会收到 `ERROR KICK_FORBIDDEN`
        之类的应答，而不是真的把谁踢掉。

        Returns (ok, message, code):
          * ok      True 表示中继确认踢掉了；
          * message 中文，给用户看（成功/失败两种都有）；
          * code    "OK" / "OWNER_LEFT_ROOM" / "KICK_FORBIDDEN" / ... / "TIMEOUT"
        """
        if not self.running or self.writer is None:
            return False, "还没有加入房间", "NOT_IN_ROOM"

        target = (target_client_id or "").strip()

        if not target:
            # 服务端也会回 KICK_NO_SUCH_MEMBER，这里先给出同一句话，省一次往返。
            return False, proto.describe_error(
                f"ERROR {proto.ERR_KICK_NO_SUCH_MEMBER}"
            ), proto.ERR_KICK_NO_SUCH_MEMBER

        room_token = proto.room_token(self.state.room or self.state.room_name)
        line = f"KICK {room_token or '-'} {target}"

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self._pending["KICK"] = future

        # 发出去的那一行原样进日志（协议审计：踢人是有后果的操作）。
        self.log(f"[room] sending KICK target={target}")

        try:
            async with self._send_lock:
                self.writer.write((line + "\n").encode())
                await self.writer.drain()

            try:
                return await asyncio.wait_for(future, timeout=REPLY_TIMEOUT)
            except asyncio.TimeoutError:
                return False, "服务器没有响应，请稍后再试", "TIMEOUT"
        except Exception as e:  # noqa: BLE001
            return False, f"发送失败：{e}", "SEND_FAILED"
        finally:
            self._pending.pop("KICK", None)

    # --------------------------------------------------------
    # Loops
    # --------------------------------------------------------

    async def _read_loop(self):
        while self.running:
            try:
                raw = await self.reader.readline()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                break

            if not raw:
                break

            text = raw.decode("utf-8", errors="replace").strip()

            if not text:
                continue

            self._on_line(text)

        # The connection ended. If we did not ask for that, we need to know WHY
        # before telling the user anything: "the room was closed" and "your
        # network hiccupped" look identical from here.
        if self.running:
            self.running = False
            manager = getattr(self, "auto_manager", None)
            if manager:
                manager.close()

            self.last_error = self.last_error or "与服务器的连接已断开"

            self._schedule_explainer()

    def _schedule_explainer(self):
        """
        在后台问清楚"房间还在不在"，然后说该说的话。

        ============================================================================
         这个任务必须被跟踪，也必须先确认"我还是当前那个房间"
        ============================================================================

        `_explain_disconnect()` 现在先发一次**只读探活**（`SESSION`，短超时，§8.10），
        问不到才退回原来那次 ROOMS 查询（最长 6 秒）。以前它是一个没人管的
        create_task：

          * 用户在这 6 秒里点了「退出房间」、或者干脆又建了一个同名的新房间；
          * 那条迟到的 room_gone / room_lost 照样发到界面；
          * 界面按"房间没了"收拾残局 —— 把【新】房间一起拆了，还弹一句
            「房间已不存在」。

        放进 self._tasks 之后，stop() 会把它取消；通知之前再问一次
        still_current()，双保险。
        """
        task = asyncio.create_task(self._explain_disconnect())
        self._tasks.append(task)

        return task

    def still_current(self):
        """
        这个会话还是引擎正在用的那个房间吗？

        没接 is_current 回调时（单元测试、单独用 RoomSession 的场景）一律当真，
        保持原来的行为。
        """
        if self._is_current is None:
            return True

        with contextlib.suppress(Exception):
            return bool(self._is_current())

        return True

    async def _probe_identity(self, timeout=None):
        """
        只读探活：`SESSION __mclink__ <client_id> <username> <token>`（§8.10）。

        NEVER raises and never returns None-able surprises: any failure comes
        back as a `SessionProbe` with `ok=False`, which the caller treats as
        "no answer" -> keep the old behaviour. The probe has its own short
        timeout, so it cannot delay the existing disconnect flow by more than
        that budget.
        """
        from . import control_client

        # The member connection authenticates with the account token, or with
        # the relay-issued guest token when there is no account (room.py's
        # `_request` uses exactly the same choice).
        token = self.token or self.guest_token or ""

        kwargs = {}

        if timeout is not None:
            kwargs["timeout"] = timeout

        try:
            return await control_client.probe_session(
                self.host,
                self.port,
                self.client_id,
                self.username,
                token,
                **kwargs,
            )
        except Exception:  # noqa: BLE001 -- a probe must never break the flow
            return control_client.SessionProbe(ok=False)

    async def _explain_disconnect(self):
        """
        Before deciding anything, ASK the relay whether my identity is still alive.

        ============================================================================
          用户口径（设计基准 §C / AGENTS.md 规则 6 补充）
        ============================================================================
        「重连后检查账号 token 是否还有效、是否被服务器踢掉：被踢掉了就按退出
        房间处理；如果没有（真就是闪了一小下的那种）那就自动重建」。

        So the order is:

          1. `SESSION`（只读，短超时）—— 真·闪断 -> `room_lost`（自动重连的
             唯一入口）；被踢/退房/超时/房间没了/token 失效 -> `session_ended`
             （按退出房间处理，**不重连**；`relogin=1` 时界面提示重新登录）。
          2. 老中继（`ERROR UNKNOWN_ROLE`）/ 探测不到 -> **退回原来的行为**：
             问一次 ROOMS "房间还在不在"，然后照旧发 `room_gone` / `room_lost`。

        这套判定对 Host 和 Player 是同一条路径（每个人都有自己的成员控制连接），
        用户要求的"同一规则对 host 和 player 都要生效"就是它。
        """
        reason = self.last_error
        room_name = self.state.room or ""

        from . import control_client

        probe = await self._probe_identity()
        verdict = control_client.session_probe_verdict(probe)

        # The verdict is only worth anything if it is still about the room the
        # user is in RIGHT NOW (see _schedule_explainer).
        if not self.still_current():
            return

        if verdict == "reconnect":
            self.log(
                "[reconnect] the relay's SESSION probe says my room identity "
                f"is still there (reason={probe.reason or '-'}, "
                f"auto_reconnect=1); this was a blip"
            )
            self._notify("room_lost", reason=reason)
            return

        if verdict == "leave":
            code = probe.reason or ""

            self.log(
                "[reconnect] the relay's SESSION probe says my room identity "
                f"is gone (reason={code or '-'}, relogin="
                f"{1 if probe.relogin else 0}); leaving instead of reconnecting"
            )
            self._notify(
                "session_ended",
                reason=reason,
                code=code,
                relogin=bool(probe.relogin),
                detail=proto.describe_error(f"{proto.LINE_ERROR} {code}")
                if code
                else "",
            )
            return

        # ---- unknown (old relay / unreachable / garbled): old behaviour ----
        if probe.old_relay:
            self.log(
                "[reconnect] this relay does not know SESSION "
                "(ERROR UNKNOWN_ROLE); falling back to the room-exists check"
            )

        exists = None

        with contextlib.suppress(Exception):
            rooms = await asyncio.wait_for(
                # Local import: control_client imports protocol only, no cycle.
                _room_exists(self.host, self.port, room_name),
                timeout=6,
            )
            exists = rooms

        if not self.still_current():
            return

        if exists is False:
            self._notify(
                "room_gone",
                reason=reason or "房间不存在（可能已被管理员关闭）",
            )
            return

        self._notify("room_lost", reason=reason)

    def _on_line(self, text):
        upper = text.upper()

        if upper.startswith("PONG"):
            # 心跳的回声：证明这条路两个方向都还通。看门狗看的就是这个时间。
            now = time.monotonic()
            self._last_pong = now

            # ============================================================================
            #  真实的中转延迟：中继把 PING 的 token 原样回给我们
            # ============================================================================
            #
            # `_ping_loop` 发的是 `PING <monotonic>`，中继的成员控制循环回的是
            # `PONG <同一个 token>`。于是这次回声的 RTT 是一个**测出来的**数字，
            # 不是估的：房间成员列表的「延迟」列用它。老中继 / 别的进程回一句
            # 没有 token 的 PONG（或者 token 根本不是数字）时保持 None ——
            # 界面显示 `-`，绝不编一个数字。
            token = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""

            if token:
                with contextlib.suppress(ValueError):
                    self.relay_rtt_ms = max(0.0, (now - float(token)) * 1000.0)

            return

        # ---- 中继反过来 PING 我们：原样回声 ----
        #
        # ============================================================================
        #  这是中继量【每个成员的中转延迟】的唯一手段（协调者冻结的行为）
        # ============================================================================
        #
        # 中继发 `PING <token>`，成员回 `PONG <token>`，中继用往返时间填 `rtts=`。
        # 我们**不新增任何握手步骤**：就是同一条控制连接上的一次回声，老中继
        # 从不发这条行，所以老连接的行为一点没变。
        #
        # 直接写、不 await：写入的是 asyncio 传输缓冲，读循环下一次让出就会发出去；
        # 一个 PONG 行很小，不需要 drain（而且 _on_line 是同步的，这里也不能 await）。
        if upper.startswith("PING"):
            token = text[4:].strip()

            with contextlib.suppress(Exception):
                self.writer.write(
                    (f"PONG {token}\n" if token else "PONG\n").encode()
                )

            return

        # ---- V3 direct coordination, forwarded by the relay ----
        #
        # The relay does not understand these; it just carries them to the
        # other member. We push them onto a queue and let the direct-path
        # coordinator consume them, so the control loop never blocks on a
        # punch and the punch never misses a line that arrived early.
        for keyword in proto.DIRECT_FORWARDED:
            if upper.startswith(keyword):
                with contextlib.suppress(asyncio.QueueFull):
                    self._direct_lines.put_nowait(text)

                # ★ 收到的那一刻就记下"谁还在发"（见 direct_rx_at 的说明）。
                # 只认加法字段 `from=`；老客户端不发它 -> 不记（判据退回旧行为）。
                with contextlib.suppress(Exception):
                    for token in text.split()[1:]:
                        if token.startswith("from="):
                            sender = token[5:].strip()

                            if sender:
                                self.direct_rx_at[sender] = time.monotonic()

                            break

                return

        # ---- The relay's answer to our inbound-probe request ----
        #
        # `OK DIRECT_INGRESS` means the relay DID send it, so a packet that
        # never arrives is evidence about the user's NAT. Without this
        # confirmation the two cases are indistinguishable, and guessing
        # "blocked" blames the user for our own un-upgraded relay.
        if upper.startswith("OK DIRECT_INGRESS") or \
                upper.startswith("ERROR DIRECT_INGRESS") or \
                upper.startswith("ERROR BAD_INGRESS"):
            ack = self._ingress_ack

            if ack is not None:
                self._ingress_result = upper.startswith("OK DIRECT_INGRESS")
                ack.set()

            return

        # ---- HOST_STATE host=0 members=2 world=1 ----
        if upper.startswith(proto.LINE_HOST_STATE):
            # 中继新增字段的**容错**解析（`host_id=` / `started=` 等，见
            # punch_flow 文件末尾的"假设"）。老中继 -> 空字典 -> 下面的循环
            # 照旧；新中继多给字段 -> 这里也认。**绝不因为不认识就报错**。
            from . import punch_flow

            extra = punch_flow.parse_host_state_tokens(text.split()[1:])

            if "host_id" in extra:
                self.state.host_id = extra["host_id"]

            if "started_ids" in extra:
                self.state.started_ids = extra["started_ids"]

            # `owner_id=` —— 当前 Owner 的 client_id（ADDITIVE）。界面靠它
            # 精确标出「房主」，不再靠"花名册第一个"猜（规则 6 §A）。
            for token in text.split()[1:]:
                key, _, value = token.partition("=")

                if key in ("owner_id", "owner_cid"):
                    # `-` = 中继明确说"现在没有 owner"（房间将被销毁）。
                    self.state.owner_id = "" if value in ("", "-") else value
                    break

            for token in text.split()[1:]:
                key, _, value = token.partition("=")

                if key == "host":
                    self.state.host_claimed = value == "1"

                    # If we no longer hold it, our own flag must follow.
                    if value != "1":
                        self.state.is_host = False
                        # 名字也要跟着清掉，否则界面上会一直挂着上一个人的名字。
                        self.state.host_name = ""
                        # BUG 1 的同一类问题：host 没了，**它的 client_id 也必须
                        # 一起清**，否则打洞目标会指向一个已经不在主机位的人。
                        self.state.host_id = ""
                elif key == "host_name":
                    self.state.host_name = "" if value in ("", "-") else value
                elif key == "members":
                    with contextlib.suppress(ValueError):
                        self.state.members = int(value)
                elif key == "peers":
                    # `-` means "nobody else"; an older relay sends nothing.
                    self.state.peer_ids = (
                        [] if value in ("", "-")
                        else [v for v in value.split(",") if v]
                    )
                elif key == "names":
                    # ADDITIVE：每成员显示名（登录名 / 中继分配的游客名）。
                    # 老中继不发 -> 空表 -> 界面回退到 client_id，不显示「未知」。
                    self.state.member_names = parse_member_names(value)
                elif key == "ips":
                    self.state.member_ips = parse_member_names(value)
                elif key == "rtts":
                    # ADDITIVE：每成员到中继的 RTT（中继自己量的）。没有数据的
                    # 成员不在表里 -> 界面显示 `-`，绝不编。
                    self.state.member_rtts = parse_member_rtts(value)
                elif key == "conn":
                    # ADDITIVE：每成员此刻的游戏连接状态（PROTOCOL §8.5）。
                    # 用户 2026-09-28：玩家断开后 host 那边必须立刻不再显示
                    # 「已连接」——成员列表的「连接状态」列以它为准。
                    self.state.member_conns = parse_member_conns(value)
                elif key == "world":
                    with contextlib.suppress(ValueError):
                        self.state.world_players = int(value)
                elif key == "mode":
                    # The room's transport, as the RELAY sees it. Only the host
                    # can change it; everyone else displays it locked.
                    if value in ("relay", "stun", "auto"):
                        self.state.conn_mode = value
                elif key == "mode_reason":
                    # ADDITIVE（PROTOCOL §8.9）：模式归因 `owner|restored|default`。
                    # 容错：不认识的取值整条忽略（保持 `""` = 未知），老中继
                    # 没有这个 token 时行为一字不变。
                    if value in MODE_REASONS:
                        self.state.mode_reason = value

            # 归因与日志（只在 (mode, mode_reason) 真的变了时写一行）：
            # `restored` = 同一 Owner 重建恢复，**不是**有人在乱改模式。
            self._note_mode_reason()

            # Our own host flag is confirmed by the OK, not by this broadcast;
            # but if the relay says somebody holds it and we think we do not,
            # trust the relay.
            self.state_changed.set()

            self._notify("host_state", state=self.state)

            return

        # ---- OK IDENTITY <name> / ERROR IDENTITY_* ----
        #
        # 中继认了我们的新显示名（问题 5：身份绑设备）。名字由中继回，
        # 因为**只有它知道**房间里那颗名字最终是什么（它可能回我们发的
        # 登录名，也可能按策略回一个它分配的游客名）—— 客户端绝不自己编。
        if upper.startswith(proto.LINE_ACK_IDENTITY):
            future = self._pending.get("IDENTITY")
            ack_name = " ".join(text.split()[2:]).strip()

            if future is not None and not future.done():
                future.set_result((True, ack_name or self.username, ack_name))

            return

        # ---- OK STARTED started=<1|0> / ERROR BAD_STARTED …（PROTOCOL §8.6）----
        #
        # 「我点了开始连接」的显式上报。它只影响 Host 的目标表（中继并进
        # `started=`），不影响任何门禁 —— 所以这里只是把应答交回等待的调用方。
        if upper.startswith(proto.LINE_ERR_STARTED):
            future = self._pending.get("STARTED")

            if future is not None and not future.done():
                future.set_result((False, proto.describe_error(text)))

            return

        if upper.startswith(proto.LINE_ACK_STARTED):
            future = self._pending.get("STARTED")

            if future is not None and not future.done():
                future.set_result((True, text.split(" ", 2)[-1].strip()))

            return

        # ---- KICKED by=<label> reason=<text> ----
        #
        # ============================================================================
        #  被踢 = 只对【我这一个人】生效的"房间关闭"
        # ============================================================================
        #
        # The relay tells us why it is about to close this connection, then closes
        # it. Treating that as an ordinary disconnect is exactly the ghost-state
        # bug: `_explain_disconnect()` would spend six seconds asking whether the
        # room still exists (it does -- the other people are still in it), decide
        # "network problem", and leave the user sitting in a room he was removed
        # from.
        #
        # So it gets its own event, and the GUI tears the room down locally -- the
        # same cleanup the admin-revoked path uses, with the right wording.
        if upper.startswith(proto.LINE_KICKED):
            by = ""
            reason = ""

            for token in text.split()[1:]:
                key, _, value = token.partition("=")

                if key == "by":
                    by = "" if value in ("", "-") else value
                elif key == "reason":
                    reason = "" if value in ("", "-") else value

            self.kicked = True
            self.kicked_by = by
            self.kicked_reason = reason

            # Mirror the revoked bookkeeping so anything that reads the state
            # (status card, summary) cannot claim we are still fine in the room.
            self.state.revoked = True
            self.state.revoked_reason = "你已被踢出房间"
            self.last_error = "你已被踢出房间"

            self.state_changed.set()

            self.log(
                f"[room] KICKED by={by or '-'} reason={reason or '-'}"
            )

            self._notify("kicked", by=by, reason=reason)

            return

        # ============================================================================
        #  ---- HOST_LEFT <reason>（**不带 ERROR 前缀**）----
        # ============================================================================
        #
        #  ★ 用户 2026-09-30 第 1 条：host 离线后其他玩家必须断开、清空状态与
        #    隧道、并重算按钮状态（这是以前做好的，这轮回归了）
        #
        # 中继在"房主（主机位持有者）离开 / 掉线 / 被踢 / 超时"时会把房间里
        # **所有游戏连接**断开，并且在**每个成员的控制连接**上发这一行
        # （见 `room_members.disconnect_game_connections` 的第 3 步 —— 已经进
        # 世界的玩家那条 PLAYER socket 是纯字节，读不到文本，所以这一行是他们
        # 唯一的"原因"）。
        #
        # 客户端以前**没有这个分支**：这一行被静默丢掉，于是界面上
        # `_on_host_left` 从来没被调用过 —— 玩家侧保持"运行中"、隧道和状态机
        # 都不清、按钮也不重算（用户实测：host 死后玩家侧 ONLINE 挂了 6 分 46 秒）。
        #
        # ★ 用户 2026-09-30 定稿：**三个码对应三种处理**，不能混：
        #
        #   `HOST_LEFT`      —— host **闪断**（他的成员连接断了、身份还在宽限期
        #                       里保留）。客户端：**能复用就复用现有连接，
        #                       不能用就自动重连**；原本空闲的人不重连。
        #   `HOST_STOPPED`   —— host **主动停止开服**（host 位还在、人还在房间）。
        #                       客户端：断开 + 重算按钮，**不自动重连、不弹窗**。
        #   `HOST_RELEASED`  —— host **主动释放主机位**。客户端：断开 + 重算 +
        #                       **弹窗「host 已断开连接」**，不自动重连。
        #   `NO_HOST`        —— 房间里根本没有 host（和 HOST_LEFT 同一处理）。
        #
        # ★ v3.9.16：**不再**"只通知"。核心层先自己动手拆（下面第 1 步），
        # 再通知界面（第 2 步）。界面的三道窗口（静默 / 重建中 / 待重建）
        # 从此只能决定**文案什么时候显示**，决定不了"断不断"。
        # 老中继不发这几行 -> 行为与以前一致。
        for _code, _event in (
            ("HOST_LEFT", "host_left"),
            ("HOST_STOPPED", "host_stopped"),
            ("HOST_RELEASED", "host_released"),
            ("NO_HOST", "host_left"),
        ):
            if upper.startswith(_code):
                reason = text.split(" ", 1)[1].strip() if " " in text else ""

                if reason and reason in ("-",):
                    reason = ""

                self.log(
                    f"[room] the relay says the host is gone ({_code})"
                    + (f": {reason}" if reason else "")
                )

                self.host_gone = (_code, reason)

                # ---- 0. 我自己就是 host：这是我**自己**停服的回显 ----
                #
                # 中继的这条广播是发给房间里**所有人**的，host 自己也会收到
                # （他的主机位还在，只是不开服了）。对他来说这不是"host 没了"，
                # 而是"我刚做的那件事"—— 拿它去拆会话等于把自己踢下线。
                # 所以这一支**只通知界面重算**，核心层不动手。
                if not getattr(self.state, "is_host", False):
                    self._host_gone_teardown(_code, reason or _code)

                # ---- 1. 核心层自己拆（v3.9.16）----
                #
                # 这一步**必须**在通知界面之前：就算界面那次事件丢了、被吞
                # 了、或者界面还没建好，会话与隧道也已经停了 —— 玩家不可能
                # 继续停在"已连接"。
                #
                # ---- 2. 通知界面（文案 / 按钮）----
                #
                # `mandatory=True`：告诉界面"这不是可丢的提示"。界面在模式
                # 重建窗口里遇到它时**不许丢弃**，只能**推迟**到窗口结束再
                # 补跑（见 gui/app.py 的 `_pending_host_gone`）。
                self._notify(
                    _event,
                    code=_code,
                    reason=reason or _code,
                    mandatory=True,
                )

                return

        # ---- CONN_MODE_CHANGED <reason>（**不带 ERROR 前缀**，§8.3）----
        #
        # Owner 切模式时，中继在**每个成员的控制连接**上发这一行（等待池里的
        # PLAYER/HOST_TUNNEL 收的是带 `ERROR` 前缀的同一条）。
        #
        # 客户端口径（基线 §D 末句 + PROTOCOL §8.3）：这是**模式热切换**触发的，
        # 不是网络抖动 —— 要**严格重建全部通路**：旧模式下的洞/隧道/状态机全部
        # 作废（新模式的映射和它们不是一套），然后按新模式重新走 §E。
        #
        # 这里只**通知**（`_notify("conn_mode_changed")`），真正的重建由界面那层
        # 做（它才知道当前会话/配置）；核心不自己拆东西。
        if upper.startswith(proto.LINE_CONN_MODE_CHANGED):
            # 2026-09-29（服务端 §8.9 ADDITIVE）：这一行可能带 `was_live=0|1`
            # = "切换前这个成员**真的在连接吗**"。客户端据此决定要不要自动重连
            # （原本空闲 -> 只换模式；原本有连接 -> 重建 + 自动重连）。
            # 老中继不发 -> `was_live=""` -> 界面退回旧判据（`self.running`）。
            parts = text.split()[1:]
            was_live = ""
            reason_tokens = []

            for token in parts:
                key, _, value = token.partition("=")

                if key == "was_live" and value in ("0", "1"):
                    was_live = value
                elif "=" not in token:
                    reason_tokens.append(token)

            reason = " ".join(reason_tokens).strip()

            self.last_error = reason or "连接模式已切换"
            self._notify(
                "conn_mode_changed", reason=self.last_error, was_live=was_live
            )

            return

        # ---- ROOM REVOKED <reason> ----
        if upper.startswith(proto.LINE_ROOM_REVOKED):
            reason = text[len(proto.LINE_ROOM_REVOKED):].strip()

            self.state.revoked = True
            self.state.revoked_reason = reason or "管理员关闭了房间"
            self.last_error = self.state.revoked_reason

            self.revoked.set()
            self.state_changed.set()

            self._notify("room_revoked", reason=self.state.revoked_reason)

            return

        # ---- answers to our own control requests ----
        if upper.startswith(proto.LINE_ACK_CLAIM_HOST):
            future = self._pending.get("CLAIM_HOST")

            if future is not None and not future.done():
                self.state.is_host = True
                self.state.host_claimed = True
                self.state.host_name = self.username or ""
                future.set_result((True, "已获得房主身份"))

            return

        if upper.startswith(proto.LINE_ACK_RELEASE_HOST):
            future = self._pending.get("RELEASE_HOST")

            if future is not None and not future.done():
                self.state.is_host = False
                # The relay decides whether the slot is now free; it will
                # broadcast. Assume free so the UI is not stuck on "有房主",
                # and drop the name at the same time -- a stale name next to
                # "无房主" is exactly the confusion being fixed here.
                self.state.host_claimed = False
                self.state.host_name = ""
                # 同上：主机位的 client_id 必须一起清（BUG 1 的同一类残留）。
                self.state.host_id = ""
                future.set_result((True, "已释放房主身份"))

            return

        # ---- OK KICK <target_client_id> [OWNER_LEFT_ROOM] ----
        if upper.startswith(proto.LINE_ACK_KICK):
            tokens = text.split()
            owner_left = any(
                token.upper() == "OWNER_LEFT_ROOM" for token in tokens[3:]
            )

            future = self._pending.get("KICK")

            if future is not None and not future.done():
                if owner_left:
                    # Kicking the owner dissolves the room (panel/admin path).
                    future.set_result(
                        (True, "已踢出房主，房间已解散", "OWNER_LEFT_ROOM")
                    )
                else:
                    future.set_result((True, "已踢出该成员", "OK"))

            return

        # One-shot waiters (set_conn_mode) see every line and decide for
        # themselves; they are removed as soon as they are answered.
        for wanted, queue in list(self._waiters):
            if any(upper.startswith(w) for w in wanted):
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(text)

        if upper.startswith(proto.LINE_ERROR):
            parts = text.split()
            code = parts[1].upper() if len(parts) > 1 else ""

            message = " ".join(parts[2:]) or proto.describe_error(text)

            # IDENTITY 的错码长这样：`ERROR IDENTITY_INVALID 原因` —— 它和
            # CLAIM_HOST / RELEASE_HOST / KICK 不一样，**不共享** word，
            # 所以单独判断前缀。
            if code.startswith("IDENTITY"):
                future = self._pending.get("IDENTITY")

                if future is not None and not future.done():
                    future.set_result((False, message, ""))

                return

            for word in ("CLAIM_HOST", "RELEASE_HOST", "KICK"):
                future = self._pending.get(word)

                if future is not None and not future.done():
                    if word == "KICK":
                        # KICK 的调用方要 3 元组（ok, message, code）：界面靠
                        # code 决定弹哪一句中文提示（FORBIDDEN / 目标不在…）。
                        future.set_result((False, message, code))
                    else:
                        future.set_result((False, message))

            if code == "HOST_TAKEN":
                self.state.is_host = False
                self._notify("host_taken", reason=message)

            return

    async def _auto_report_loop(self):
        while self.running:
            manager = getattr(self, "auto_manager", None)
            if manager and self.writer and self.state.conn_mode == "auto":
                for state in list(manager.states.values()):
                    await self.report_auto(state)
            await asyncio.sleep(2)

    async def report_auto(self, state):
        if self.running and self.writer:
            async with self._send_lock:
                self.writer.write(("AUTO_STATE " + json.dumps(state, ensure_ascii=True, separators=(",", ":")) + "\n").encode())
                await self.writer.drain()

    async def _ping_loop(self):
        while self.running:
            try:
                await asyncio.sleep(PING_INTERVAL)

                if not self.running:
                    continue

                # ============================================================================
                #  PONG 看门狗：半开 TCP 上的"我还在房间里"是假的
                # ============================================================================
                #
                # A half-open connection (mobile / symmetric NAT, or a NAT
                # reboot) looks perfectly healthy from here: the socket is open,
                # writes still succeed, and read() simply never returns because
                # no FIN ever arrives. Meanwhile the relay's 15 s member
                # watchdog has already dropped us and freed the host position.
                #
                # The relay answers EVERY PING with PONG, so silence across more
                # than two complete intervals means the path is gone -- treat it
                # exactly like a lost connection and let _explain_disconnect()
                # tell the user and clean up. Lagging by a second or two must
                # NOT trip this: the window is two full intervals (checked once
                # per interval, so it fires after 6-9 s of total silence), well
                # inside the relay's own 15 s watchdog.
                if time.monotonic() - self._last_pong > PONG_TIMEOUT:
                    await self._report_connection_dead()
                    return

                if self.writer is None:
                    continue

                async with self._send_lock:
                    self.writer.write(
                        f"PING {time.monotonic():.3f}\n".encode()
                    )
                    await self.writer.drain()

            except asyncio.CancelledError:
                raise

            except Exception:
                # The read loop notices and reports.
                continue

    async def _report_connection_dead(self):
        """
        心跳断了：把这条成员连接按"已经断开"处理。

        Exactly the same outcome as the read loop noticing EOF, which is what
        the relay has already done on its side by the time we get here:

          1. mark it down so nothing thinks we are still in the room
          2. close the socket, so the read loop stops waiting on a dead TCP
             connection forever (this is what made the UI freeze on stale
             「房间内 N 人」 / 「主机：某人」)
          3. ask _explain_disconnect() to say the right thing to the user

        The reason is the phrase this file already uses for a lost connection,
        so the user-facing popup/log stays fully translated.
        """
        self.last_error = self.last_error or "与服务器的连接已断开"
        self.running = False

        with contextlib.suppress(Exception):
            await close_writer(self.writer)

        self._schedule_explainer()

    # --------------------------------------------------------

    def stats(self):
        return {
            "room": self.state.room,
            "is_owner": self.state.is_owner,
            "is_host": self.state.is_host,
            "host_claimed": self.state.host_claimed,
            "members": self.state.members,
            "world_players": self.state.world_players,
            "revoked": self.state.revoked,
            "kicked": self.kicked,
            "relay_rtt_ms": self.relay_rtt_ms,
            "connected": self.running,
            # 归因也进状态快照：模式是谁改的（PROTOCOL §8.9）。老中继 = ""。
            "conn_mode": self.state.conn_mode,
            "mode_reason": self.state.mode_reason,
        }


__all__ = ["RoomSession", "RoomState", "RoomError", "PING_INTERVAL",
           "parse_member_names", "parse_member_rtts", "parse_member_conns",
           "describe_mode_reason", "CONN_STATES", "MODE_REASONS",
           "MODE_REASON_OWNER", "MODE_REASON_RESTORED", "MODE_REASON_DEFAULT"]
