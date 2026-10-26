"""
MC-Link Relay Server (V1)

============================================================================
 HARD RULE -- READ BEFORE EDITING
============================================================================

This file contains the *verified* TCP relay core.

The wire protocol and the raw-data path below are FROZEN. They are the exact
mechanics that were already tested successfully with a real Minecraft
1.20.1 Forge client:

    HOST        <room>            -> "OK HOST room=<room>"
    HOST_TUNNEL <room>            -> "WAIT"
                                    -> "START <session_id>"
                                    -> host sends "READY"
    PLAYER      <room>            -> "ERROR ..." | "CONNECTED"

After "READY" / "CONNECTED" every single byte on that socket is opaque
Minecraft TCP data. Nothing is parsed, nothing is rewritten, no JSON is ever
injected into a data tunnel.

V1 changes are LIMITED to:
  1. Lifecycle / cleanup correctness (no half-dead rooms, no leaked tasks).
  2. Waiting tunnel/player wake-ups via asyncio.Event instead of 0.05s polling.
  3. Additive, optional metadata fields after "<room>" in the first line.
     An old client that only sends "ROLE room" still works unchanged.
  4. Additive INFO / PING handling **only on the HOST control socket**,
     never on a data tunnel.
  5. Structured status output (console + JSON file) for the future admin API.

The DATA PATH (bidirectional_relay / copy_stream) is byte-identical in
behaviour to the verified build.

============================================================================
"""

import asyncio
import atexit
import contextlib
import json
import os
import secrets
import socket
import sys
import tempfile
import time
import uuid

import relay_config as cfg
import relay_events
import relay_log

from admin_api import AdminAPI, RawResponse
import room_registry
import room_members
import auto_relay
from room_members import (
    MembershipMixin,
    RoomMember,
    broadcast_member_state,
    broadcast_room_closed,
    drop_all_sessions,
    kick_member,
    member_control_loop,
    member_watchdog,
    revoke_room,
    send_kicked,
)
from db_store import (
    MySQLStore,
    build_store,
    pymysql,
    aiomysql,
    shutdown_guard,
)
import room_names
from gatekeeper import (
    SENTINEL,
    AccountService,
    LoginThrottle,
    TokenManager,
)
from settings_service import (
    KNOWN_SETTINGS,
    SETTING_ALLOW_VERSION_MISMATCH,
    SETTING_MAX_PLAYERS,
    SettingsService,
)

# ============================================================
# V2 service context
# ============================================================
#
# These are module level so the room/tunnel code can mirror state into the
# database without threading a context object through every frozen function.
# They are ALWAYS set before the listener starts, and every use is optional:
# the relay works with all of them disabled.

store = None            # SafeStore (memory or mysql)
accounts = None         # AccountService
tokens = None           # TokenManager
admin_api = None        # AdminAPI
settings = None         # SettingsService (panel-editable runtime switches)

# Room id -> monotonic timestamp when it became empty (no players).
# Used for the empty-room grace period (auto delete).
_empty_since = {}

#: Identifies THIS relay process. Every boot gets a new value.
#:
#: It is written into every `rooms` row, so this one statement at startup
#: removes the rows left behind by a previous process:
#:
#:     DELETE FROM rooms WHERE instance_id <> :INSTANCE_ID
#:
#: That is the structural fix for "ghost rooms" in the admin panel -- after a
#: restart the in-memory registry is empty but the table is not, and the panel
#: used to show those leftovers as rooms with no host and no name.
INSTANCE_ID = uuid.uuid4().hex

#: What this BUILD can do. The panel checks these and, when one is missing,
#: says so on screen with the command that fixes it.
#:
#: WHY: "面板改了没反应" was diagnosed three separate times as "the relay
#: container is still the old image" -- the panel had no way to know. A version
#: number would only say "old"; a feature list says exactly WHAT is old, and
#: the panel can turn that into an instruction instead of a mystery.
FEATURES = (
    "auto_stream_v1",
    "max_players_setting",   # 面板可改房间人数上限
    "room_registry",         # 房间注册表独立模块（关房不再报"房间不存在"）
    "owner_delete",          # 房间所有者可以删自己的房间
    "presence",              # 在线状态按真实连接统计
    "role_create",           # 建号写 role 列（root 能建管理员）
    "role_manage",           # root 可以提升/降级已有账号
    "self_service_limit",    # 每月 3 次自助修改（改名+改密码共用）
    "relay_build_info",      # 本清单本身
    "db_startup_guard",      # 数据库卡住不再拖住启动（有超时 + 有上限）
    "member_names_rtt",      # HOST_STATE 广播 names= / rtts= + OK 回包 you=
    "version_bypass_switch", # 面板的开发开关：允许不同版本客户端连接（默认关）
)

# Never let a database call that is wedged inside a worker thread keep the
# process (or `docker stop`) alive. Normal exits are unaffected: the guard
# returns immediately when no database call is in flight.
atexit.register(shutdown_guard)


def relay_build():
    """
    When this relay's code was last written, and what it supports.

    The timestamp comes from the file on disk, so it is always the truth about
    the code that is actually running -- no constant to forget to bump.
    """
    import datetime

    try:
        stamp = os.path.getmtime(os.path.abspath(__file__))
        text = datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    except OSError:
        text = "unknown"

    return {"build": text, "features": list(FEATURES)}


# ============================================================
# Utility
# ============================================================

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(*parts):
    """
    记一行日志：标准输出（docker logs 仍可看）+ 日志文件 + 内存缓冲。

    `relay_log.write()` 内部自己吞异常，所以日志出问题绝不会影响
    Minecraft 连接（这是硬要求：日志是旁路，不是数据通路的一部分）。
    """
    try:
        line = f"[{now()}] " + " ".join(str(p) for p in parts)
        print(line, flush=True)
        relay_log.write(line)
    except Exception:
        pass


def event(name, **fields):
    """
    记一条**结构化事件**（`EVENT <name> k=v k=v ...`）。

    ============================================================================
      为什么中继也要结构化（用户的要求）
    ============================================================================

    用户原话：记录**原始数据**，不要翻译，方便后续改进和审计。

    所以连接/房间这类关键行为不再只写一句话（"玩家进了房间"），而是把**原始
    字段**摆出来：role、peer、room、session、协议原文、错误码。事后能直接算
    "哪个 IP 反复握手失败""哪个房间的隧道平均活了多久"，而一句话做不到。

    格式和客户端 `client/mclink/log.py::event()` 一致，两边的日志可以直接
    并起来按 session_id / room 对齐。

    绝不影响数据通路：`relay_events.event()` 是纯字符串拼接，`log()` 自己吞
    异常；这里再包一层 suppress，日志坏了也绝不能让连接断。
    """
    with contextlib.suppress(Exception):
        log(relay_events.event(name, **fields))


def raw(keyword, line):
    """
    原样记一条协议原文（审计用）：`<keyword> <原文>`。

    绝不加工、绝不改字段顺序 —— 中继是转发者，它记下的这条就是"线上到底
    传了什么"，事后对不上协议时可以拿它当证据。
    """
    with contextlib.suppress(Exception):
        log(relay_events.raw(keyword, line))


def _redact_handshake(line):
    """
    把握手原文里的 **token 抹掉**再记日志。

    为什么不是"整行不记"：这份日志的用途之一就是**审计与重新实现客户端**，
    "客户端到底发了几个字段、房间字段长什么样"必须能从日志里查。整行不记就
    什么都没了。

    为什么不是"原样记"：第五个字段是账号 token，等同于密码。日志会被下载、
    贴到聊天里、发邮件——一个凭据进去了就收不回来。所以认得出的 token 位置
    换成 `<token>`，其余**逐字保留**（包括顺序、空格、多余字段）。

    认不出的形状（老客户端只有两个字段）就原样返回：没有 token 可抹。
    """
    parts = str(line).split()

    if len(parts) >= 5:
        parts[4] = "<token>"

    parts = ["resume=<token>" if p.startswith("resume=") else p for p in parts]

    return " ".join(parts)


def safe_peer(writer):
    try:
        return writer.get_extra_info("peername")
    except Exception:
        return "unknown"


def peer_ip(writer):
    peer = safe_peer(writer)
    if isinstance(peer, (tuple, list)) and peer:
        return str(peer[0])
    return str(peer)


def enable_tcp_nodelay(writer):
    """Disable Nagle. Minecraft is latency sensitive."""
    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


async def close_writer(writer):
    """Close a StreamWriter exactly once, never raising."""
    if writer is None:
        return

    try:
        if writer.is_closing():
            return
    except Exception:
        pass

    try:
        writer.close()
    except Exception:
        return

    try:
        await writer.wait_closed()
    except Exception:
        pass


def clean_token(value, max_len):
    """
    Validate a client supplied token (room name / username / client id).

    Returns the cleaned string, or None if it is unacceptable.

    Security: the value is only ever used as a dictionary key, a log field and
    an echo back to the same client. It is NEVER executed, NEVER used to build
    a shell command and NEVER used to build a filesystem path.
    """
    if value is None:
        return None

    if len(value) < 1 or len(value) > max_len:
        return None

    # No whitespace / control characters: this keeps the line protocol
    # unambiguous and blocks log injection.
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        return None

    # No "/" either. A room name or username travels in the URL path of the
    # panel's admin API (/rooms/<name>/revoke), where a slash would silently
    # become a path separator -- and the panel would then be unable to close a
    # room it can happily display. Names that need escaping (中文, spaces) are
    # fine now that the relay decodes the path; a slash is not a name, it is a
    # path character.
    if "/" in value:
        return None

    return value


# ============================================================
# Waiting connection tickets
# ============================================================

def _waiter_closed(waiter):
    """
    这条等待中的连接是不是已经断开了？

    存在的唯一理由：**死隧道不许配对给玩家**。

    `try_pair` 以前只检查池子里"有没有"隧道，不检查"还活着吗"。于是
    host 那边 stop 没收拾干净留下的死 socket 会一直堆在池子里（现场是 26 条，
    而房间容量只有 8），玩家一进来就被配对到第一条死隧道上：

        [PAIR] -> [TUNNEL ASSIGNED] -> failed to send START: Connection lost
        -> 整对拆掉 -> Minecraft 报「你的主机中的软件中止了一个已建立的连接」

    判断只用 StreamWriter 自己的状态，不读 socket（`is_closing()` 在
    "对端已关闭"和"本地已关闭"两种情况下都是 True）。
    writer 为 None 也算关闭。
    """
    writer = getattr(waiter, "writer", None)

    if writer is None:
        return True

    try:
        return bool(writer.is_closing())
    except Exception:
        # 拿不到状态时按"还活着"处理：宁可按老行为试一次，
        # 也不能因为读不出状态就把一条正常隧道扔掉。
        return False


class Waiter:
    """
    One HOST_TUNNEL or PLAYER socket that is waiting to be paired.

    Replaces the old `(reader, writer)` tuple. The added `assigned` Event
    removes the 0.05s polling loop and makes "the room died while I waited"
    an immediate, deterministic event instead of a stuck socket.
    """

    __slots__ = (
        "reader",
        "writer",
        "session_id",
        "assigned",
        "created_at",
        "client_id",
        "username",
        "token",
    )

    def __init__(self, reader, writer, client_id="", username="", token=""):
        self.reader = reader
        self.writer = writer
        self.session_id = None
        self.assigned = asyncio.Event()
        self.created_at = time.time()
        self.client_id = client_id
        self.username = username
        self.token = token

    def assign(self, session_id):
        self.session_id = session_id
        self.assigned.set()

    def release(self):
        """Wake the waiter without a session => caller must close the socket."""
        self.assigned.set()

    @property
    def paired(self):
        return self.session_id is not None


# ============================================================
# Session / Pair
# ============================================================

async def _pair_handshake_deadline(room, pair, timeout=None):
    """
    给一对配对设一个握手 deadline：到点还没「进入世界」就把整对拆掉。

    ============================================================================
      这一个函数修的是用户报的致命 BUG
    ============================================================================

    Pairing happens the moment a PLAYER socket and an idle HOST_TUNNEL exist
    -- but the session only becomes real after READY (host side) and CONNECTED
    (player side). Without a deadline, one stuck pairing keeps its slot forever:

      * the panel/client count it as a player  (「房间 3 人却显示 9 人」)
      * `try_pair` refuses to pair anybody else because the room looks full
        (「第二个舍友游戏连不上」)

    Both symptoms are the same leak. The deadline returns the slot.

    Never raises; a failure here must not affect the data path.
    """
    limit = timeout if timeout is not None else cfg.PAIR_HANDSHAKE_TIMEOUT

    try:
        deadline = time.monotonic() + limit

        while time.monotonic() < deadline:
            if getattr(pair, "established", False):
                return
            await asyncio.sleep(0.5)

        if getattr(pair, "established", False):
            return

        # 还没成：清掉，把名额还回去。
        async with room.lock:
            still = room.sessions.get(pair.session_id)

        if still is not pair:
            return

        log(
            "[PAIR TIMEOUT]",
            f"room={room.name}",
            f"session={pair.session_id}",
            f"player={pair.player_username or pair.player_ip or '-'}",
            f"waited={limit}s",
            "reason=handshake never completed (slot reclaimed)",
        )
        event(
            "session.pair_timeout",
            room=room.name,
            session=pair.session_id,
            player=pair.player_username or "",
            player_ip=pair.player_ip,
            host_user=pair.host_username,
            waited=limit,
            reason="handshake_never_completed",
        )

        await room.remove_session(
            pair.session_id, reason=f"handshake timeout ({limit}s)"
        )

        # 让还在排队的人有机会补上空出来的名额。
        with contextlib.suppress(Exception):
            await room.try_pair()

    except asyncio.CancelledError:
        raise

    except Exception as e:  # noqa: BLE001 - 看门狗绝不能影响别的
        log("[PAIR TIMEOUT ERROR]", f"error={e}")


class Pair:
    """One PLAYER <-> HOST_TUNNEL pairing plus its statistics."""

    __slots__ = (
        "session_id",
        "player_reader",
        "player_writer",
        "tunnel_reader",
        "tunnel_writer",
        "host_ready",
        "start_relay",
        "relay_task",
        "player_bytes",
        "tunnel_bytes",
        "created_at",
        "player_id",
        "player_username",
        "player_ip",
        #: Raw context for the log: which room this session belongs to, and the
        #: host-side endpoint it was paired with. Kept on the pair because at
        #: "RELAY END" time the room object may already be gone, and the log
        #: line is written then -- a field that is only gettable earlier is a
        #: field that ends up missing from exactly the reports that need it.
        "room_name",
        "host_username",
        "tunnel_ip",
        "logged_to_db",
        #: 真的走到 CONNECTED、开始转发数据了才算"进入世界"。
        #: 配上对但没走完握手的会话是"占位"（幽灵），不能算进人数。
        "established",
        #: 握手 deadline 的看门狗任务（到点还没 established 就拆掉整对）。
        "deadline_task",
        "auto",
    )

    def __init__(
        self,
        session_id,
        player_reader,
        player_writer,
        tunnel_reader,
        tunnel_writer,
        player_id="",
        player_username="",
        player_ip="",
    ):
        self.session_id = session_id

        self.player_reader = player_reader
        self.player_writer = player_writer
        self.tunnel_reader = tunnel_reader
        self.tunnel_writer = tunnel_writer

        self.host_ready = asyncio.Event()
        self.start_relay = asyncio.Event()

        self.relay_task = None

        self.player_bytes = 0
        self.tunnel_bytes = 0

        self.created_at = time.time()

        self.player_id = player_id
        self.player_username = player_username
        self.player_ip = player_ip

        #: Filled in by the pairing path (see Room.try_pair): these are the
        #: raw facts every connection report asks for first.
        self.room_name = ""
        self.host_username = ""
        self.tunnel_ip = ""

        self.logged_to_db = False

        self.established = False
        self.deadline_task = None
        self.auto = None


# ============================================================
# Room
# ============================================================

class Room(MembershipMixin):

    def __init__(self, name, shown_name=""):
        #: 房间 ID（room token）：ASCII、无空格无斜杠。协议、rooms 字典键、
        #: 数据库、面板接口全都用它。老客户端发来的裸名字归一化后也是它。
        self.name = name

        #: 玩家看到的房间名（中文 / 空格 / emoji 都行），和 name 一一对应。
        #: 面板与客户端显示都用它。没有给的话（老房间）就显示 name 本身。
        self.display_name = shown_name or room_names.display_name(name)

        # HOST control connection
        self.host_reader = None
        self.host_writer = None
        self.host_id = ""
        self.host_username = ""
        self.host_ip = ""
        self.host_token = ""

        # 心跳：房主控制连接最后一次有数据的时间（monotonic）。
        # 喂给看门狗，用来识别"半开连接"——连接实际已断但 TCP 没发 FIN。
        self.host_last_seen = None

        #: ★ v3.9.17：房主是不是**主动**停止开服的（客户端在关连接前发一行
        #: `STOPPING`，ADDITIVE；老客户端不发 -> 恒为 False）。
        #:
        #: 用来把"房主点了停止"和"房主掉线/崩了/拔网线"分开：以前这两种在日志
        #: 和给玩家的原因里是**同一句话**，排障时根本分不清。默认 False，
        #: 所以老客户端的行为一字不变。
        #:
        #: ============================================================================
        #:  ★★ 这是一个**一次性**标记（v3.9.16 的 P1 就是它永不复位）
        #: ============================================================================
        #:
        #: v3.9.16 里它只在房间初始化时置 False，之后再没人清。于是房主只要
        #: 正常停过一次，这个房间的 2.5 秒宽限窗口就**永久失效**：之后的掉线、
        #: 崩溃、模式热切换重建全部立刻广播 `HOST_STOPPED`（把全房间踢下线），
        #: 而且措辞还被错报成"主动停止"。
        #:
        #: 所以规则是：新握手建立时清零，通知发出去之后立刻清零。
        #: 它描述的是"**这一次**断开的原因"，不是"这个房间的状态"。
        self.host_stopping = False

        # Serialises writes to the host CONTROL socket only.
        # Data tunnels never go through this lock.
        self.host_send_lock = asyncio.Lock()

        # ---- V3: members (owner + players in the room) ----
        #
        # keyed by client_id. Each entry has its own control connection that
        # stays open for as long as the person is in the room, even while they
        # are not connected to Minecraft.
        self.members = {}

        #: The room's CONNECTION MODE, chosen by the **OWNER** and shared by
        #: everyone. "relay" | "stun". A room-level property on purpose: the
        #: people in a Minecraft world all have to use the same transport, so
        #: letting each client pick its own would mean half the room is direct
        #: and half is relayed with no way to tell which connection is which.
        #:
        #: Defaults to relay, so a room created by an OLD client (which never
        #: sends one) behaves exactly as it did before.
        #:
        #: **Only an explicit Owner `CONNMODE` may change this** (PROTOCOL §8,
        #: design baseline §A). The ONE exception is a room destroyed and
        #: rebuilt by the SAME owner: `get_or_create_room` restores his
        #: remembered choice (`conn_mode_reason="restored"`) instead of silently
        #: snapping the room back to the default while nobody asked -- which is
        #: exactly the "mode jumped back to relay by itself" report.
        #: See room_registry.remember_mode / room_members.set_room_conn_mode.
        self.conn_mode = "relay"
        self.auto_generation = uuid.uuid4().hex

        #: WHY the mode is what it is: "owner" | "restored" | "default".
        #: Broadcast as `mode_reason=` so a client can tell "the owner just
        #: switched" from "this is a fresh room" (ADDITIVE, PROTOCOL §8.9).
        self.conn_mode_reason = "default"

        #: Who last set it (client id, "" while it is still the default).
        self.conn_mode_by = ""

        #: One-shot fact consumed by the NEXT broadcast: did the room have a
        #: live connection (connected/pairing) at the moment the mode was
        #: switched? None = this is not a mode-switch broadcast, so no
        #: `was_live=` token is emitted.
        self.conn_mode_was_live = None

        # Idle pool
        self.waiting_tunnels = []   # list[Waiter]
        self.waiting_players = []   # list[Waiter]

        # Active sessions
        self.sessions = {}          # session_id -> Pair

        self.closed = False
        self.created_at = time.time()
        self.total_player_bytes = 0
        self.total_tunnel_bytes = 0

        self.lock = asyncio.Lock()

        # Wakes the host INFO notifier when something changed.
        self.dirty = asyncio.Event()

    # --------------------------------------------------------
    # Status
    # --------------------------------------------------------

    def active_count(self):
        return len(self.sessions)

    def tunnel_count(self):
        return len(self.waiting_tunnels)

    def player_waiting_count(self):
        return len(self.waiting_players)

    def mark_dirty(self):
        self.dirty.set()

    # --------------------------------------------------------
    # Host
    # --------------------------------------------------------

    async def attach_host(self, reader, writer, client_id, username, token=""):
        async with self.lock:
            self.host_reader = reader
            self.host_writer = writer
            self.host_id = client_id
            self.host_username = username
            self.host_ip = peer_ip(writer)
            self.host_token = token

    async def host_alive(self):
        async with self.lock:
            return self.host_writer is not None

    def touch_host(self):
        """
        Record host activity on the CONTROL channel.

        Called for every line the host sends (the client sends PING every 3
        seconds for latency measurement, which doubles as the heartbeat).
        """
        self.host_last_seen = time.monotonic()

    def host_idle_seconds(self):
        if self.host_last_seen is None:
            return 0.0

        return time.monotonic() - self.host_last_seen

    async def send_to_host(self, line):
        """
        Send one control line to the HOST socket.

        Used for the legacy handshake reply, PING answers and INFO pushes.
        NEVER used for Minecraft payload.
        """
        async with self.host_send_lock:
            writer = self.host_writer
            if writer is None:
                return False

            try:
                writer.write(line.encode())
                await writer.drain()
                return True
            except Exception:
                return False

    # --------------------------------------------------------
    # Pairing
    # --------------------------------------------------------

    async def add_tunnel(self, reader, writer):
        waiter = Waiter(reader, writer)

        async with self.lock:
            if self.closed:
                return None
            self.waiting_tunnels.append(waiter)

        self.mark_dirty()
        await self.try_pair()
        return waiter

    #: 这个房间自己的容量上限（建房时客户端给的）。
    #:
    #: 0 = 没给，跟随服务器设置。服务器的数字是【上限】：客户端可以设得更小，
    #: 设得更大就按服务器上限算（客户端那边也会拒绝开始，这里是保险）。
    #: 这样「服务器改了上限」不会影响任何已经在跑的房间。
    max_players_room = 0

    #: 正在被关闭（管理员删房：通知 → 等客户端自己退 → 兜底强断）。
    #: 这段窗口里不再放人进来，否则刚进来就被关，又是一个"灵异房间"。
    closing = False

    #: 关闭原因，用来告诉"晚一步进来"的客户端到底发生了什么。
    closing_reason = ""

    def room_capacity(self):
        """这个房间实际允许的人数：min(建房时定下的值, 服务器上限)。"""
        server = max_players()

        if self.max_players_room and self.max_players_room > 0:
            return max(2, min(int(self.max_players_room), server))

        return server

    #: 房间已建成、人数已定下（见 set_room_capacity 的说明）。
    capacity_locked = False

    def set_room_capacity(self, member, value):
        """
        房间人数只在【建房那一刻】定下，之后不许再改（用户明确要求）。

        为什么不是"谁都能改"：

          用户的现象是「房主建房时填 8，但另一个人当 host 时只开 3 条隧道，
          房间就只能进 3 个人」—— 因为隧道数量以前跟着 **host 本机**的设置走。
          现在隧道数量跟着**房间**的容量走（客户端侧改的），所以房间容量必须
          是一个建房时就钉死、之后谁都改不动的数字：否则 host 一换，房间的
          实际容量又跟着换，问题只是换了个地方出现。

          要改人数 = 删掉房间重建（用户原话：「除非重建房间」）。

        仍然保留这个函数（而不是删掉），因为：
          * 建房路径要**第一次**写进去（`create_room` 用 capacity 参数）；
          * 老客户端可能还在发 MAXPLAYERS（比如别人拿旧 exe 当 host），
            这里必须**明确回绝**，而不是静默忽略 —— 静默忽略会让旧客户端
            以为改成功了，界面显示 8、实际还是 5。
        """
        server = max_players()

        try:
            wanted = int(value)
        except (TypeError, ValueError):
            return False, "人数必须是数字"

        if wanted < 2:
            return False, "人数至少 2 人（含主机）"

        # 已经锁定 => 拒绝。注意这里要允许"和当前值相同"的重复设置：
        # 建房流程本身会先把值写进去，重复写同一个数字不算修改。
        locked = int(self.max_players_room or 0)

        if self.capacity_locked and locked and wanted != locked:
            return False, (
                f"房间人数在建房时就定下了（{locked} 人），之后不能修改。"
                "要换人数请删掉房间重建。"
            )

        if not (member.is_owner or member.is_host):
            return False, "只有房间所有者或主机可以改人数"

        self.max_players_room = min(wanted, server)
        self.capacity_locked = True
        self.mark_dirty()

        if wanted > server:
            return True, (
                f"房间人数设为 {self.max_players_room}（服务器上限 {server}，"
                "你设的值更高，按服务器上限执行）"
            )

        return True, f"房间人数设为 {self.max_players_room}"


    def occupied_seats(self):
        """
        容量闸门的**真实占用**：按**人**（client_id / player_id）去重。

        ============================================================================
          ★ 用户 2026-09-29（第三轮）第 4 条：「两人房也提示房间已满」
        ============================================================================

        旧闸门是 `len(self.sessions) + len(self.waiting_players)` ——
        **每一条 TCP 连接各占一个名额**。而 Minecraft 多人列表每点一次「刷新」
        就会向本机代理开一条 TCP，客户端于是向中继发一条 PLAYER 握手；这些
        "还没配对的探测"全堆在 `waiting_players` 里（实测每条蹲 5~27 秒），
        8 人房被同一个人刷 8 次就满了，真正的游戏连接反而收到
        `ERROR ROOM_FULL`。

        "名额"本来该数的是**人**：同一个 client_id 开 8 条探测连接仍然是一个人。
        那些探测的清理由两条独立机制负责（客户端在 MC 关闭时立刻收工 + 服务端的
        等待池过期回收，见 `reap_stale_waiters`），这里只保证"一个人只占一个位子"。

        没有 client_id / player_id 的连接（理论上不会出现）退化成"每条各占一个"
        —— 宁可保守，也不让匿名连接互相顶掉名额。
        """
        seats = set()

        for pair in (getattr(self, "sessions", None) or {}).values():
            player_id = str(getattr(pair, "player_id", "") or "").strip()

            seats.add(("person", player_id) if player_id else ("session", id(pair)))

        for waiter in (getattr(self, "waiting_players", None) or ()):
            client_id = str(getattr(waiter, "client_id", "") or "").strip()

            seats.add(
                ("person", client_id) if client_id else ("waiter", id(waiter))
            )

        return len(seats)

    async def add_player(self, reader, writer, client_id="", username=""):
        waiter = Waiter(reader, writer, client_id, username)

        async with self.lock:

            if self.closed:
                self.mark_dirty()
                return "closed"

            if self.conn_mode == "auto":
                total = len(self.sessions) + len(self.waiting_players)
                own = sum(p.player_id == client_id for p in self.sessions.values())
                own += sum(w.client_id == client_id for w in self.waiting_players)
                if total >= auto_relay.MAX_STREAMS_PER_ROOM or own >= auto_relay.MAX_STREAMS_PER_MEMBER:
                    return "stream_limit"

            # ★ 按人去重（见 `occupied_seats` 的说明）：同一个人反复刷新 MC
            # 多人列表不再能把房间刷"满"。
            if self.conn_mode != "auto" and self.occupied_seats() >= self.room_capacity():
                self.mark_dirty()
                return "full"

            self.waiting_players.append(waiter)

        self.mark_dirty()
        await self.try_pair()
        return waiter

    async def try_pair(self):
        """
        Pair every waiting PLAYER with a waiting HOST_TUNNEL.

        The pairing logic is unchanged from the verified build: FIFO on both
        sides, server generates the session id, server sends "START <id>" to
        the tunnel.

        STUN MODE DOES NOT CHANGE THIS. Pairing opens the session's control
        channel and hands the client its session id; it does not decide what
        carries the game bytes. His own design draws the line in the same place:
        `_dispatch_mode` in MODE_DIRECT never calls `_start_server_relay`, so
        the rule lives entirely on the client.

        Refusing to pair here looked like a cheap way to guarantee "no game
        data over the relay in STUN mode", and it was wrong twice over: the
        relay cannot tell control bytes from game bytes on an opaque tunnel
        (that is the frozen design), and without the pairing the CLIENT never
        learns its session id, so it can never bind the punched link to a
        Minecraft connection at all.
        """

        #: 一次配对里最多扔掉多少条死连接（安全阀，防止池子里全是死的时候空转）。
        _MAX_STALE_DROPS = 64

        stale_players = 0
        stale_tunnels = 0

        while True:

            async with self.lock:

                if self.closed:
                    return

                if not self.waiting_players:
                    return

                if not self.waiting_tunnels:
                    return

                if self.conn_mode != "auto" and len(self.sessions) >= self.room_capacity():
                    return

                # ============================================================
                #  死隧道必须先扔掉，绝不能配对给玩家
                # ============================================================
                #
                # 现场故障（2026-09-15）：房间容量 8，池子里却堆着 26 条
                # **已经断开**的隧道（host 那边 stop 没收拾干净）。玩家进来时
                # 中继按 FIFO 挑到的是第一条 —— 那条的 socket 早就没了。
                #
                # 结果：中继兴高采烈地配对上、给玩家回 CONNECTED，然后
                # `START` 写不出去（Connection lost）→ 隧道被判定关闭 →
                # 这一对立刻拆掉 → Minecraft 那边看到的就是
                # 「你的主机中的软件中止了一个已建立的连接」。
                #
                # 所以配对之前必须确认**两条连接都还活着**。原来只检查
                # "有没有"（`if not self.waiting_tunnels`），没检查"还活着吗"，
                # 于是死隧道可以反复吃掉玩家（日志里连着 4 次 TUNNEL ERROR）。
                #
                # 顺带把死掉的玩家连接也扔掉：玩家那边可能已经超时走了。
                player_waiter = self.waiting_players.pop(0)

                if _waiter_closed(player_waiter):
                    stale_players += 1

                    if stale_players > _MAX_STALE_DROPS:
                        return

                    continue

                while True:
                    if not self.waiting_tunnels:
                        # 把这个玩家放回去，等新隧道进来再配。
                        self.waiting_players.insert(0, player_waiter)
                        return

                    tunnel_waiter = self.waiting_tunnels.pop(0)

                    if not _waiter_closed(tunnel_waiter):
                        break

                    stale_tunnels += 1

                    log(
                        "[TUNNEL DROPPED]",
                        f"room={self.name}",
                        f"peer={peer_ip(tunnel_waiter.writer)}",
                        "隧道已断开，不参与配对",
                    )

                    if stale_tunnels > _MAX_STALE_DROPS:
                        # 池子里全是死的（正常情况不该发生）：把玩家放回去，
                        # 让新隧道进来时再配，别在这里空转。
                        self.waiting_players.insert(0, player_waiter)
                        return

                session_id = uuid.uuid4().hex if self.conn_mode == "auto" else uuid.uuid4().hex[:8]

                pair = Pair(
                    session_id=session_id,
                    player_reader=player_waiter.reader,
                    player_writer=player_waiter.writer,
                    tunnel_reader=tunnel_waiter.reader,
                    tunnel_writer=tunnel_waiter.writer,
                    player_id=getattr(player_waiter, "client_id", ""),
                    player_username=getattr(player_waiter, "username", ""),
                    player_ip=peer_ip(player_waiter.writer),
                )

                # Raw context for the log, captured at the moment it is true.
                # See Pair's slots: at RELAY END time the room may be gone, so
                # anything the end-of-session line needs has to be taken now.
                pair.room_name = self.name
                pair.host_username = getattr(tunnel_waiter, "username", "")
                pair.tunnel_ip = peer_ip(tunnel_waiter.writer)

                if self.conn_mode == "auto":
                    pair.auto = auto_relay.AutoBridge(self, pair, self.remove_session, event)
                self.sessions[session_id] = pair

                player_waiter.assign(session_id)
                tunnel_waiter.assign(session_id)

            enable_tcp_nodelay(pair.player_writer)
            enable_tcp_nodelay(pair.tunnel_writer)

            log(
                "[PAIR]",
                f"room={self.name}",
                f"session={session_id}",
                f"active={len(self.sessions)}/{self.room_capacity()}",
                f"player={pair.player_username or pair.player_ip}",
            )

            # A pair is "player socket attached to host tunnel socket". The
            # handshake may still fail (see the deadline below), so this is
            # NOT yet "in the world" -- recording it separately is what makes
            # the two distinguishable in the log.
            event(
                "session.paired",
                room=self.name,
                session=session_id,
                active=len(self.sessions),
                capacity=self.room_capacity(),
                player=pair.player_username or "",
                player_ip=pair.player_ip,
                player_client_id=pair.player_id,
                host_user=pair.host_username,
                host_ip=pair.tunnel_ip,
                waiting_tunnels=len(self.waiting_tunnels),
                waiting_players=len(self.waiting_players),
            )

            self.mark_dirty()

            # ---- V2: remember the session for the web panel ----
            _mark_room_busy(self.name)

            if store is not None:
                pair.logged_to_db = True
                await store.open_session(
                    {
                        "session_id": session_id,
                        "room_id": self.name,
                        "player_username": pair.player_username,
                        "player_client_id": pair.player_id,
                        "player_ip": pair.player_ip,
                    }
                )

            # ---- 握手看门狗 ----
            #
            # 配对上不等于玩家真进世界了。给这一对一个 deadline：到点还没
            # established 就整对拆掉，把名额还给房间。
            #
            # 没有它的话：一次"配上但对不上"会永久占用一个名额，
            # 显示人数虚高，而且后面的人再也挤不进来（用户报的致命 BUG）。
            pair.deadline_task = asyncio.create_task(
                _pair_handshake_deadline(self, pair)
            )

            # Tell the Host Tunnel that this tunnel has been assigned.
            # This is the frozen "START <session_id>" line.
            try:
                pair.tunnel_writer.write(pair.auto.handshake("host", "START_AUTO") if pair.auto else f"START {session_id}\n".encode())
                await pair.tunnel_writer.drain()
            except Exception as e:
                log(
                    "[ERROR]",
                    f"session={session_id}",
                    f"failed to send START: {e}",
                )
                await self.remove_session(session_id)
                continue

            # ============================================================================
            #  "已配对"也是一个**连接状态变化** -> 立刻广播（HOST_STATE 的 conn=）
            # ============================================================================
            #
            # 用户要求状态"迅速同步"，所以 `waiting -> pairing` 这一跳也要推。
            # 少了这一句时它有两种命运，都不是"立刻"：
            #   * 玩家先等、隧道后到 -> 下一次广播要等到 CONNECTED（world= 那一次），
            #     中间这段时间所有人看到的都是 `waiting`（假的）；
            #   * 隧道先等、玩家后到 -> add_player() 里刚广播完就被 try_pair 配上，
            #     广播里是 waiting，同样要等到 CONNECTED。
            # 放在 START 之后、deadline 之前：这一刻的状态是真的（pair 已在
            # sessions 里、established 还是 False）。
            with contextlib.suppress(Exception):
                await broadcast_member_state(self)

    # --------------------------------------------------------
    # Session removal
    # --------------------------------------------------------

    def world_players(self):
        """
        「已加入世界」到底几个人 —— **只数真的进去的**，而且按人去重。

        ============================================================================
          为什么不能数 len(self.sessions)
        ============================================================================

        sessions 里的每一对只是"占位"：配上对之后还要等房主客户端连本机
        Minecraft 并回 READY、玩家侧再回 CONNECTED，才真的在同一个世界里。

        数 len(sessions) 会把"配上对但没走完握手"的幽灵会话也算成人：
        用户看到的就是「房间里 3 个人，却显示已加入世界 9 人」。

        另外这里按 **玩家名去重**：同一个人重连/多开时不该被数成两个人。
        没有名字的（老客户端）退回 client_id / IP，保证至少不会把同一条会话
        重复计数。
        """
        names = set()

        for pair in self.sessions.values():
            if not getattr(pair, "established", False):
                continue

            who = (
                getattr(pair, "player_username", "")
                or getattr(pair, "player_id", "")
                or getattr(pair, "player_ip", "")
                or getattr(pair, "session_id", "")
            )

            names.add(who)

        host = 1 if (self.host_writer is not None or self.host_claimed) else 0

        return host + len(names)

    async def remove_session(self, session_id, reason=""):
        """Idempotent: safe to call from every cleanup path."""

        async with self.lock:
            pair = self.sessions.pop(session_id, None)

        if pair is None:
            return

        if pair.auto:
            pair.auto.close()

        # 取消握手看门狗（正常结束的会话不需要它了）
        if pair.deadline_task is not None and not pair.deadline_task.done():
            pair.deadline_task.cancel()

        # 唤醒还在等的另一端。
        #
        # 少了这一步，对面那个 handler 会永远卡在
        # `await pair.host_ready.wait()` / `await pair.start_relay.wait()`：
        # session 已经没了，任务却一直挂着（任务泄漏，而且它持有的 socket
        # 也不会被关）。这两行让它立刻醒过来走自己的清理路径。
        pair.host_ready.set()
        pair.start_relay.set()

        self.total_player_bytes += pair.player_bytes
        self.total_tunnel_bytes += pair.tunnel_bytes

        log(
            "[SESSION CLOSED]",
            f"room={self.name}",
            f"session={session_id}",
            f"player_tx={pair.player_bytes}B",
            f"host_tx={pair.tunnel_bytes}B",
            f"reason={reason or 'closed'}",
        )

        if pair.relay_task is not None and pair.relay_task is not asyncio.current_task() and not pair.relay_task.done():
            pair.relay_task.cancel()
            try:
                await pair.relay_task
            except (asyncio.CancelledError, Exception):
                pass

        # Releasing the TCP sockets makes both client loops exit.
        await close_writer(pair.player_writer)
        await close_writer(pair.tunnel_writer)

        # ---- V2: mirror the finished session into the database ----
        if store is not None:
            await store.close_session(
                pair.session_id,
                pair.player_bytes,
                pair.tunnel_bytes,
                reason or "closed",
            )

        if not self.sessions:
            _mark_room_empty(self.name)

        self.mark_dirty()

        # 有人离开世界，人数也要立刻反映出来（同上）。
        with contextlib.suppress(Exception):
            await broadcast_member_state(self)

    # --------------------------------------------------------
    # Close entire room
    # --------------------------------------------------------

    async def close(self, reason=""):
        """
        Destroy this room and every resource attached to it.

        Called when the HOST socket disappears. This is the fix for the
        "host re-opened the LAN world and could not connect again" bug:
        the room, its session map and all sockets are gone the moment the
        host disconnects, so the same Room ID can be created again at once
        WITHOUT restarting the container.
        """
        async with self.lock:

            if self.closed:
                already = True
            else:
                already = False
                self.closed = True

            host_writer = self.host_writer

            waiting_tunnels = list(self.waiting_tunnels)
            waiting_players = list(self.waiting_players)
            sessions = list(self.sessions.values())
            members = list(self.members.values())

            self.host_reader = None
            self.host_writer = None

            self.waiting_tunnels.clear()
            self.waiting_players.clear()
            self.sessions.clear()

        if already:
            return

        # AUTO network readiness is independent of application sessions, but
        # follows the same room authority and must terminate on room closure.
        networks = list(getattr(self, "auto_networks", {}).values())
        for bridge in networks:
            bridge.close()
        if networks:
            await asyncio.gather(*(b.network_task for b in networks), return_exceptions=True)

        # ====================================================================
        #  房间里的人必须被告知，而且他们的连接必须被关掉
        # ====================================================================
        #
        # This used to close the host connection, the tunnels and the sessions
        # -- and leave the MEMBER control connections wide open. A member
        # connection is how a client knows it is in a room, so the members were
        # left in the worst possible state: the room no longer existed anywhere
        # (gone from the registry, gone from the panel, gone from the database),
        # but their clients kept showing 已加入房间, kept the room's buttons
        # locked, and refused to let the user create or join anything until they
        # pressed 退出房间 by hand.
        #
        # Tell them first (so the client can explain WHY, and clean up its own
        # side), then close the sockets so the read loop returns and runs the
        # normal member cleanup.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(broadcast_room_closed(self, reason), timeout=3)

        for member in members:
            member.closed = True

            if member.writer is not None:
                try:
                    member.writer.close()
                except Exception:
                    pass

        log(
            "[ROOM CLOSE]",
            f"room={self.name}",
            f"sessions={len(sessions)}",
            f"idle_tunnels={len(waiting_tunnels)}",
            f"waiting_players={len(waiting_players)}",
            f"reason={reason or 'host offline'}",
        )

        # Wake every waiter with no session => they close themselves.
        for waiter in waiting_tunnels:
            waiter.release()

        for waiter in waiting_players:
            waiter.release()

        for pair in sessions:
            if pair.relay_task is not None and not pair.relay_task.done():
                pair.relay_task.cancel()

        await close_writer(host_writer)

        for pair in sessions:
            await close_writer(pair.player_writer)
            await close_writer(pair.tunnel_writer)

        for waiter in waiting_tunnels:
            await close_writer(waiter.writer)

        for waiter in waiting_players:
            await close_writer(waiter.writer)

        _empty_since.pop(self.name, None)

        # ---- V2: the room no longer exists, remove it from the mirror ----
        # delete_room_owned() only removes the row while it still belongs to
        # THIS host control connection. A late cleanup from a previous process
        # therefore cannot delete a room that a reconnected host has already
        # re-created under the same name.
        if store is not None:
            removed = await store.delete_room_owned(self.name, self.host_token)

            if not removed:
                await store.delete_room(self.name)

        self.mark_dirty()

    # --------------------------------------------------------
    # Snapshot (admin / status file)
    # --------------------------------------------------------

    async def snapshot(self):
        async with self.lock:
            # ============================================================================
            #  「房间内在线」= **房间里的人**（不是"名字并集"）
            # ============================================================================
            #
            # 旧公式是 `len(member_names | world_names)`：把**成员显示名**和
            # **已配对游戏会话的 PLAYER 握手名**并起来按名字去重。它的初衷是对的
            # （有些朋友直接进世界、不建房间成员，只数成员会漏人），但**按名字**
            # 去重对游客是错的：
            #
            #   * 成员坐位名 = 中继分配的「游客2c12wl」（房间名册里的名字）；
            #   * 他的 PLAYER 握手报的是客户端配置名 `lubenwey`（游客没有账号
            #     token，`player_core` 那一行自然带的是表单里的名字）。
            #
            # 同一个人因此贡献两个不同的名字 -> 4 个人显示成 5 个。而**换连接模式**
            # 会 `disconnect_game_connections()` 把所有会话拆掉，`world_names` 清空，
            # 数字当场回到 4 —— 用户 2026-09-29 第 5 条就是这个：
            # 「房间内仅有四人时，却显示有五人，此时热切换连接模式，房间内人数显示
            #   恢复正常。成员列表正常。」
            #
            # 正确口径：**按 client_id 判定"这个游戏连接是不是房间里某个成员"**，
            # 是 -> 他已经算在花名册里了，不重复计；不是 -> 那才是"直接进世界的人"，
            # 补上。老式房间（完全没有成员记录）保持原来的并集口径，一字不变。
            member_cids = {
                str(getattr(m, "client_id", "") or "").strip()
                for m in self.members.values()
            }
            member_cids.discard("")

            member_names = set()

            for member in self.members.values():
                if member.username:
                    member_names.add(member.username)

            world_names = set()

            for pair in self.sessions.values():
                name = getattr(pair, "player_username", "")

                if not name:
                    continue

                player_cid = str(getattr(pair, "player_id", "") or "").strip()

                if player_cid and player_cid in member_cids:
                    # 房间里某个成员的游戏连接 —— 同一个人，只算一次。
                    continue

                world_names.add(name)

            if member_cids:
                # 成员**按记录数**算（同一个 client_id 两个账号仍然是两个人：
                # 面板这一列说的是"房间里有几个人"，不是"几个设备"）。
                # `world_names` 里只剩"没有成员记录的直连玩家"，所以不会重复。
                members = len(self.members) + len(world_names)
            elif member_names or world_names:
                members = len(member_names | world_names)
            else:
                members = 0

            if not member_cids and self.host_writer is not None:
                # Legacy room (HOST connection, no members at all): the host is
                # still a person in the room.
                members = max(members, 1)

            # 主机位状态：`host_claimed` / `host_name` / `host_now` 全部从
            # MembershipMixin.host_state() 这**一个**来源出 —— 快照里也不会
            # 出现"有主机但没有名字"这种自相矛盾的一对。
            host_is_claimed, host_display = self.host_state()

            # 谁已经"开始连接"（设计基准 §D/§E）。和 HOST_STATE 广播的
            # `started=` 同一个来源，面板排查"他说点了开始但没打洞"时能对上。
            started_ids = set(room_members.started_client_ids(self))

            # 每成员游戏连接状态（ADDITIVE）。和 HOST_STATE 广播的 `conn=`
            # 同一个来源（room_members.member_conn_states）—— 面板「成员」表
            # 要显示"这个人此刻到底连上没有"，而不是"他在房间里"。
            conn_states = room_members.member_conn_states(self)

            return {
                # 房间 ID（协议/接口用，ASCII）与房间名（给人看的，任意字符）
                # 都报出去：面板显示 name、操作时回传 room。
                "room": self.name,
                "room_mode": self.conn_mode,
                "name": self.display_name,
                "host_online": self.host_writer is not None,
                # 与 broadcast 同源的 host_claimed/host_name（ADDITIVE：老面板
                # 不认这两个键，照旧读 host_now / host_online）。
                "host_claimed": bool(host_is_claimed),
                "host_name": host_display or "",
                "host_client_id": self.host_id,
                "host_username": self.host_username,
                # 建房的人（所有者）。房间列表的「房主」列用它，所以房间一建好
                # 就有名字，不用等谁去认领主机。
                "owner_username": (
                    self.owner.username if self.owner is not None else ""
                ),
                # 当前开世界的那个位置是谁。主机有两种持有方式：旧式 HOST 控制
                # 连接（host_username），或 V3 里成员认领（host_member）——
                # 房间列表的「当前主机」列要两种都能显示。口径与 host_name 一致。
                "host_now": host_display or "",
                "host_ip": self.host_ip,
                # 「已加入世界」：开世界的那个位置有人 = 1，再加每个配对成功的
                # 玩家。房主有两种持有方式（旧式 HOST 控制连接 / V3 的成员
                # 认领），两种都要算，否则"房主一个人在游戏里"会显示 0 ——
                # 面板上那个「在线玩家 0」就是这么来的。
                "players": self.world_players(),
                # 「房间内在线」：人在房间里但可能还没进世界，两个数字分开报，
                # 客户端和面板用的是同一套口径。
                "members": members,
                "max_players": self.room_capacity(),
                "idle_tunnels": len(self.waiting_tunnels),
                "waiting_players": len(self.waiting_players),
                "bytes_player_to_host": self.total_player_bytes
                + sum(p.player_bytes for p in self.sessions.values())
                + sum(b.pair.player_bytes for b in getattr(self, "auto_networks", {}).values()),
                "bytes_host_to_player": self.total_tunnel_bytes
                + sum(p.tunnel_bytes for p in self.sessions.values())
                + sum(b.pair.tunnel_bytes for b in getattr(self, "auto_networks", {}).values()),
                "network_tunnels": [dict(session_id=b.pair.session_id,
                    player_id=b.pair.player_id, **b.snapshot())
                    for b in getattr(self, "auto_networks", {}).values() if not b.closed],
                "created_at": self.created_at,
                # 代际标识：写进数据库，用于"重启后清掉上一代残留"。
                "instance_id": INSTANCE_ID,
                "host_token": self.host_token,
                # 房间成员名单（面板「成员」表用）。`members` 是人数，是冻结
                # 字段不能动，所以名单另开一个键 —— 纯新增，老面板看不到它、
                # 也不受影响。
                #
                # client_id 是踢人接口认人的键；member_key 是房间内部的唯一键
                # （同一个 client_id 被两个账号占用时会变成 client_id:username，
                # 见 add_member），一起报出去，面板排查"同 id 两个人"时有据可查。
                "member_list": [
                    {
                        **auto_relay.member_route(self, m.client_id),
                        "public_ip": getattr(m, "public_ip", ""),
                        "client_id": m.client_id,
                        "member_key": getattr(m, "member_key", m.client_id),
                        "username": m.username,
                        # 显示名：登录名 / 中继分配的「游客xxxxxx」/ 最后兜底
                        # client_id。面板的成员表直接显示它，不再自己猜。
                        "display_name": m.display_name,
                        "label": m.label,
                        "is_owner": bool(m.is_owner),
                        "is_host": bool(m.is_host),
                        # 该成员是否已经开始连接（有 PLAYER / 开服控制连接）。
                        # ADDITIVE：老面板不认这个键，忽略即可。
                        "started": bool(m.client_id in started_ids),
                        # ============================================================
                        #  该成员此刻的游戏连接状态（ADDITIVE）
                        # ============================================================
                        #
                        # 取值永远是 4 个之一：idle | waiting | pairing |
                        # connected（精确口径见 PROTOCOL §8.5，和客户端收到的
                        # `conn=` 同一个来源 —— 面板与界面不会各说各话）。
                        "conn": conn_states.get(m.client_id, "idle"),
                        # 客户端**显式上报**的"点过开始连接"（ADDITIVE，见
                        # PROTOCOL §8.6 `STARTED`）。老客户端恒为 False，此时
                        # `started` 仍然按连接现算，行为不变。
                        "connecting": bool(getattr(m, "connecting", False)),
                        "idle": round(time.monotonic() - m.last_seen, 1),
                        "joined_at": m.joined_at,
                        # 这个成员到中继的 RTT（毫秒，中继实测）。没测到 = None，
                        # 面板显示 `-`，绝不编数字。
                        "rtt_ms": (
                            round(m.rtt_ms) if m.rtt_ms is not None else None
                        ),
                    }
                    for m in self.members.values()
                ],
                "sessions": [
                    {
                        **(p.auto.snapshot() if p.auto else {"room_mode": self.conn_mode, "active_path": "relay" if self.conn_mode == "relay" else "unknown"}),
                        "session_id": p.session_id,
                        "player_username": p.player_username,
                        "player_client_id": p.player_id,
                        "player_ip": p.player_ip,
                        "duration": round(time.time() - p.created_at, 1),
                        "bytes_player_to_host": p.player_bytes,
                        "bytes_host_to_player": p.tunnel_bytes,
                    }
                    for p in self.sessions.values()
                ],
            }


# ============================================================
# Global rooms
# ============================================================
#
# The registry lives in room_registry.py, NOT here: this module is started as
# `python relay_server.py`, so its name is `__main__` and `from relay_server
# import rooms` elsewhere would import a SECOND copy of this file with an empty
# dictionary. See the header of room_registry.py -- that copy is what made the
# panel answer 「房间不存在」 for a room that was live in memory.

rooms = room_registry.rooms
rooms_lock = room_registry.rooms_lock

# Hand our logger over, so the registry, the watchdog and the revoke path all
# print through the server's logger instead of a silent fallback.
room_registry.set_logger(log)


async def resolve_room(value):
    """
    把一个房间标识解析成内存里的 Room（找不到返回 None）。

    能接受两种写法，这是老客户端还能用的关键：
      * room token（房间 ID）—— 新客户端的 HOST/PLAYER/HOST_TUNNEL 用它
      * 裸房间名（老客户端发的就是它）—— 归一化之后同样是 token
    归一化是幂等的，所以两种写法命中的是同一个房间。
    """
    token = room_names.room_token(value)

    if not token:
        return None

    async with rooms_lock:
        return rooms.get(token)


async def get_or_create_room(room_name, shown_name="", owner_key=""):
    """
    按 token 取房间，没有就建一个。

    `owner_key` 是**建房者的身份键**（client_id）。新房间的构造默认模式是
    "relay"，但如果是**同一个 Owner** 刚刚销毁过这个 token 的房间，他上一次
    显式选过的模式会被带回来（`conn_mode_reason="restored"`）—— 房间对象的
    生死由 Owner 的连接决定（设计基准 §A），一次闪断/重连不该把他选过的
    传输方式悄悄换回默认值（用户 2026-09-29 的 P0）。别人的选择绝不继承。

    这是**唯一**允许在没有 Owner `CONNMODE` 的情况下产生非默认模式的地方，
    而且只在"房间销毁 -> 重建"这个明确场景，并且带 `mode_reason=restored`
    广播给客户端。
    """
    token = room_names.room_token(room_name)

    async with rooms_lock:
        room = rooms.get(token)

        if room is None:
            room = Room(token, shown_name=shown_name)

            if owner_key:
                remembered = room_registry.recall_mode(token, owner_key)

                if remembered in ("relay", "stun"):
                    room.conn_mode = remembered
                    room.conn_mode_reason = "restored"

            rooms[token] = room
            log(
                "[ROOM CREATE]",
                f"room={token}",
                f"name={room.display_name}",
                f"mode={room.conn_mode}",
                f"origin={room.conn_mode_reason}",
            )
            event(
                "room.created",
                room=token,
                room_name=room.display_name,
                conn_mode=room.conn_mode,
                mode_origin=room.conn_mode_reason,
            )

        return room


def _mark_room_empty(room_name):
    """Record when a room lost its last player (for the grace period)."""
    _empty_since.setdefault(room_name, time.monotonic())


def _mark_room_busy(room_name):
    _empty_since.pop(room_name, None)


def accounts_required():
    """
    Live value of the 'must log in' switch.

    Reads the runtime setting (a panel toggle stored in the database) and falls
    back to the environment variable. Every authorisation check goes through
    this, so toggling it in the panel takes effect for the NEXT connection
    while leaving every already-connected player alone.
    """
    if accounts is not None and getattr(accounts, "settings", None) is not None:
        return accounts.settings.get_bool(
            "require_account", cfg.REQUIRE_ACCOUNT
        )

    return bool(cfg.REQUIRE_ACCOUNT)



def max_players():
    """
    Live value of the per-room capacity (host included).

    The panel can change this, so nothing may cache it: a read that bypasses
    the settings service silently keeps using the boot value, which looks
    exactly like the panel failing to save. Every capacity check calls THIS.

    The bounds come from the setting's own metadata, so the relay and the
    panel cannot disagree about what is allowed.
    """
    meta = KNOWN_SETTINGS.get(SETTING_MAX_PLAYERS) or {}
    minimum = int(meta.get("minimum", 2))
    maximum = int(meta.get("maximum", 64))

    service = settings

    if service is None:
        # Same fallback chain as accounts_required(): the service is also
        # reachable through the account service.
        service = getattr(accounts, "settings", None)

    if service is None:
        # Only during the first milliseconds of boot, and in tests.
        return int(cfg.MAX_PLAYERS)

    return service.get_int(
        SETTING_MAX_PLAYERS, int(cfg.MAX_PLAYERS),
        minimum=minimum, maximum=maximum,
    )


# ============================================================
# 免登录 / 匿名连接的临时身份：「游客」+ 6 位
# ============================================================
#
# 规则（用户 2026-09-26 明确要求）：
#
#   * 登录用户：显示名就是**登录名**。
#   * 未登录 / 免登录：服务器在握手时**分配**一个临时用户名，
#     `游客` + 6 位数字/小写字母。房间成员列表、日志、快照、
#     广播里凡是"这个人是谁"的地方都用它。
#   * 临时身份**随连接销毁**：下次连接重新分配，不持久化、不与账号关联。
#
# 为什么要有它：客户端没登录时发过来的 username 是 `-`（协议占位符），
# 于是房间里所有人都是"未知"，日志里也全是 `-`。分配一个临时名字之后，
# "谁在房间里"这个问题在界面上、日志里、面板快照里都有答案。
#
# 同时修掉"空 client_id / 空 username 导致出错"的既有 bug：所有按名字 /
# client_id 建键值、日志、广播的地方都走 ensure_identity()，缺就补，
# **绝不出现空 key，也绝不抛异常**。

GUEST_NAME_PREFIX = "游客"
GUEST_NAME_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
GUEST_NAME_LENGTH = 6


def new_guest_name(taken=()):
    """
    生成一个 `游客xxxxxx`（6 位 [0-9a-z]）。

    `taken` 是同一个房间里已经在用的名字，用来避开重名（重名会让成员列表
    看起来像有两个人叫同一个名字）。极端情况下退化成 uuid 片段，绝不返回
    空字符串。
    """
    taken = {str(name) for name in (taken or ()) if name}

    for _ in range(50):
        name = GUEST_NAME_PREFIX + "".join(
            secrets.choice(GUEST_NAME_ALPHABET)
            for _ in range(GUEST_NAME_LENGTH)
        )

        if name not in taken:
            return name

    return GUEST_NAME_PREFIX + uuid.uuid4().hex[:GUEST_NAME_LENGTH]


# ============================================================================
#  游客名只是显示名；"这个游客是谁"由下面的游客会话（guest session）决定
# ============================================================================
#
# 用户否掉了两版做法，最终口径见下面 guest session 那一段：
#   * 不按 client_id / 设备记（第一版审计建议被否）；
#   * 也不是"每条连接都换一个"（那样退房再进就变成另一个人）。
# 名字本身仍然用 `new_guest_name(taken)` 现生成，`taken` 只是当前房间的
# 成员名，用来避免同一房间里两个人重名。


# ============================================================================
#  游客会话（guest session）—— 身份是"登录会话"级别的，不是"房间"级别的
# ============================================================================
#
# 用户 2026-09-27 定的口径（两次修订后的最终版）：
#
#   * 免登录/游客**不**绑本地设备（client_id / IP / MAC / 安装目录），也不落库；
#   * 但**也不**是"每条连接换一个"：它是**客户端与中继的登录会话**级别的。
#     退出房间 / 被踢 / 房间被关闭销毁 —— 这些**都不销毁**它，客户端可以拿
#     同一条临时 token 重新进房、建别的房，名字不变。
#   * 只有**账号退出**才销毁：(a) 掉线重连导致登录状态丢失（客户端不再持有
#     这条 token）、(b) 换账号登录、(c) 重启软件。下次连接重新分配一个新身份。
#
# 实现方式：中继在第一次给游客分配名字时**同时发一条临时 token**
# （`guest=<token>`，ADDITIVE 地挂在 OK JOIN 回包尾部）。客户端把它记在
# **登录态**里，之后每条握手（JOIN/CREATE/HOST/HOST_TUNNEL/PLAYER）都把它
# 放在现有的 `token` 字段里回传；中继认 token 就复用同一个名字。
# 房间成员关系怎么变都不碰这张表 —— 只有上面三种"账号退出"和 TTL 会清。
#
# 为什么不按 client_id 记：用户明确否掉了"绑设备"。token 是客户端在**这一次
# 登录会话**里自己拿着的，重启/重连丢登录态后就没了 —— 身份自然失效。
GUEST_SESSION_PREFIX = "guest-"
GUEST_SESSION_TTL_S = float(
    os.environ.get("MCLINK_GUEST_SESSION_TTL", "") or 21600
)
GUEST_SESSION_LIMIT = 4096

#: token -> {"name": str, "touched": monotonic}
_guest_sessions = {}


def _prune_guest_sessions(now=None):
    """超时 / 超量的游客会话丢掉（纯内存，不落库）。"""
    now = time.monotonic() if now is None else now

    for token, entry in list(_guest_sessions.items()):
        if now - entry.get("touched", now) > GUEST_SESSION_TTL_S:
            _guest_sessions.pop(token, None)

    while len(_guest_sessions) > GUEST_SESSION_LIMIT:
        oldest = min(
            _guest_sessions,
            key=lambda tok: _guest_sessions[tok].get("touched", 0.0),
        )
        _guest_sessions.pop(oldest, None)


def open_guest_session(name):
    """开一个游客会话：返回临时 token（客户端整个登录会话都用它）。"""
    _prune_guest_sessions()

    token = GUEST_SESSION_PREFIX + secrets.token_hex(8)
    _guest_sessions[token] = {"name": name, "touched": time.monotonic()}

    log("[GUEST SESSION]", f"name={name}", "opened=1")

    return token


def guest_session_name(token, touch=True):
    """这条临时 token 对应的游客名；不是游客会话 / 已失效 -> ""。"""
    if not token or not str(token).startswith(GUEST_SESSION_PREFIX):
        return ""

    _prune_guest_sessions()
    entry = _guest_sessions.get(token)

    if entry is None:
        return ""

    if touch:
        entry["touched"] = time.monotonic()

    return entry.get("name", "")


def release_guest_session(token, reason=""):
    """账号退出：销毁这个游客身份（房间进出**不会**走到这里）。"""
    if not token or not str(token).startswith(GUEST_SESSION_PREFIX):
        return False

    entry = _guest_sessions.pop(token, None)

    if entry is None:
        return False

    log(
        "[GUEST SESSION]",
        f"name={entry.get('name', '-')}",
        f"released=1",
        f"reason={reason or '-'}",
    )

    return True


def is_missing_identity(value):
    """
    这个字段算不算"没有"？

    空字符串和协议占位符 `-`（客户端未登录时发的就是它）都算没有。
    """
    text = "" if value is None else str(value).strip()

    return text == "" or text == "-"


def ensure_client_id(client_id, role="", room="", peer="", log_it=True):
    """
    保证有一个非空的 client_id。

    客户端没登录、或者老客户端根本不发这个字段时，用 `anon-<12位hex>`
    兜底。**必须每个匿名连接都不同** —— 共用一个空 key 会让后进来的人把
    先进来的人顶掉，房主/所有权跟着乱（CREATE/JOIN 路径原本就有这段逻辑，
    这里抽出来给所有角色共用）。

    这个值只是**这一条连接**的键（进房间、认人、拆会话用），不是"设备身份"：
    游客身份一次性的规则见上面那段说明。
    """
    if not is_missing_identity(client_id):
        return str(client_id).strip()

    assigned = f"anon-{uuid.uuid4().hex[:12]}"

    if log_it:
        log(
            "[ANON CLIENT]",
            f"role={role or '-'}",
            f"room={room or '-'}",
            f"peer={peer or '-'}",
            f"assigned={assigned}",
        )

    return assigned


def ensure_username(username, role="", room="", peer="", taken=(), log_it=True):
    """
    保证有一个非空的**显示名**。

    登录用户的名字原样保留；没有（或 `-`）就分配一个**全新**的
    「游客xxxxxx」—— **每次连接都重新分配，不看 client_id**（游客身份是一次性
    的，见上面的说明：同一个人掉线重连也必须拿到新名字，掉线后不残留）。

    `taken` 是**当前房间里已经在用的名字**，只用来避免同一房间里两个人重名，
    不做任何跨连接的记忆。
    """
    if not is_missing_identity(username):
        return str(username).strip()

    taken = {str(name) for name in (taken or ()) if name}
    assigned = new_guest_name(taken)

    if log_it:
        log(
            "[GUEST NAME]",
            f"role={role or '-'}",
            f"room={room or '-'}",
            f"peer={peer or '-'}",
            f"assigned={assigned}",
        )

    return assigned


def allow_version_mismatch():
    """
    Live value of the 「允许不同版本客户端连接」 development switch.

    ============================================================================
     这不是"兼容老版本"的路径 —— 只是开发调试开关
    ============================================================================

    默认 False（版本必须一致，和生产行为完全一样）。管理组在面板上临时打开
    之后，版本不一致的连接会被**放行**（并在日志里留一条 [VERSION BYPASS]，
    因为它是一个"非正常状态"）。

    和 accounts_required() 同一条取值链：优先读运行时设置（面板存在数据库里
    的值），读不到就退回环境变量默认值。每次握手都重新读，所以打开/关掉
    **不需要重启中继**。
    """
    service = settings

    if service is None:
        service = getattr(accounts, "settings", None)

    if service is None:
        return False

    return service.get_bool(SETTING_ALLOW_VERSION_MISMATCH, False)


# ============================================================================
#  IDENTITY 的 token 校验 —— 注入给 room_members
# ============================================================================
#
# room_members 不能 import 本模块（本模块以 `__main__` 运行，会导入第二份
# 服务器，见 room_registry.py），所以账号体系这一小块用回调注入。
#
# 口径和 RENAME 完全一致：token 记录里的 username 是权威（客户端在 IDENTITY
# 里带的那个名字只是提示）。没有账号体系时（accounts is None）无从校验，
# 如实返回空身份，由 room_members 退回客户端报的名字。
async def verify_identity_token(token):
    """`(ok, identity_or_error_code)` —— 见 gatekeeper.AccountService.authorize。"""
    if accounts is None:
        return True, {"username": ""}

    return await accounts.authorize(token or "")


room_members.set_identity_hooks(
    verify_token=verify_identity_token,
    version_bypass=allow_version_mismatch,
    release_guest=release_guest_session,
    # 「连接断了 != 离开房间」的判据之一：这条 token 是不是一条还活着的
    # 游客会话（免登录客户端拿到的临时身份）。见 hold_member_seat。
    guest_token_name=guest_session_name,
    # 游客 token 的前缀。有了它，room_members 才不会把一条**已失效**的
    # 游客 token 当成账号 token 去校验（免登录服务器上那会返回"合法"，
    # 位子就永远不放）。
    guest_token_prefix=GUEST_SESSION_PREFIX,
)


def room_owner_username(room):
    """
    The account that owns a room, or "" when nobody does.

    A room can be owned (created with CREATE, its lifetime tied to the owner's
    member connection) and independently hosted (the position Minecraft
    connections hang off). **Only the OWNER may delete it** (2026-09-24):
    the host position can be claimed by anyone, so it is not ownership.
    """
    owner = getattr(room, "owner", None)

    return str(getattr(owner, "username", "") or "")


async def retire_room(room_name, room):
    """
    Drop a room from the registry AND from the database mirror.

    EVERY place that removes a room goes through here. Keeping the two in step
    is the whole point: a room that is gone from memory but still present as a
    row is exactly what the panel showed as 幽灵房间 (a room nobody can join,
    that the panel keeps listing, and that the close button cannot get rid of).

    Closing a room usually deletes its row already (Room.close does it), so the
    delete here is cheap insurance rather than the only mechanism.
    """
    dropped = await drop_room_if_empty(room_name, room)

    if dropped:
        await _delete_room_row(room_name)

    return dropped


async def drop_room_if_empty(room_name, room):
    """
    Remove the room from the registry only if it is the same object and
    completely empty. Prevents a stale task from deleting a brand new room
    that re-used the same Room ID.

    The implementation lives in room_registry.py so that every caller -- this
    module, the member watchdog and the panel's revoke path -- operates on the
    same dictionary.
    """
    return await room_registry.drop_room_if_empty(room_name, room)


# ============================================================
# Raw TCP relay  (FROZEN -- do not restructure)
# ============================================================

async def copy_stream(reader, writer, direction_name, pair):
    """
    Copy raw bytes one way until EOF. Returns total bytes copied.

    This function NEVER inspects the payload.
    """
    total = 0

    try:
        while True:
            data = await reader.read(cfg.BUFFER_SIZE)

            if not data:
                break

            writer.write(data)
            await writer.drain()

            total += len(data)

            if direction_name == "PLAYER->HOST":
                pair.player_bytes += len(data)
            else:
                pair.tunnel_bytes += len(data)

    except asyncio.CancelledError:
        raise

    except Exception:
        pass

    finally:
        return total


async def bidirectional_relay(pair):
    """
    THE ONLY PLACE where raw Minecraft data relay is started.

    Exactly ONE task owns both socket directions. This is what prevents the
    double-read bug that broke an earlier refactor attempt.
    """
    log("[RELAY START]", f"session={pair.session_id}")

    # The moment the game's bytes start flowing between the two sockets.
    # `pair.player...` / `pair.tunnel_ip` are the raw endpoints: "who was
    # actually connected to whom" is the first question of any connection
    # report, and it cannot be reconstructed later from anything else.
    event(
        "session.relay.start",
        session=pair.session_id,
        room=pair.room_name,
        player=pair.player_username or pair.player_ip or "",
        player_ip=pair.player_ip,
        host_user=pair.host_username,
        tunnel_ip=pair.tunnel_ip,
    )

    task_player_to_host = asyncio.create_task(
        copy_stream(pair.player_reader, pair.tunnel_writer, "PLAYER->HOST", pair)
    )

    task_host_to_player = asyncio.create_task(
        copy_stream(pair.tunnel_reader, pair.player_writer, "HOST->PLAYER", pair)
    )

    try:
        done, pending = await asyncio.wait(
            [task_player_to_host, task_host_to_player],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # One direction closed => the session has exactly one lifecycle.
        for task in pending:
            task.cancel()

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    except asyncio.CancelledError:
        task_player_to_host.cancel()
        task_host_to_player.cancel()
        await asyncio.gather(
            task_player_to_host,
            task_host_to_player,
            return_exceptions=True,
        )
        raise

    finally:
        log(
            "[RELAY END]",
            f"session={pair.session_id}",
            f"player_to_host={pair.player_bytes}B",
            f"host_to_player={pair.tunnel_bytes}B",
        )
        event(
            "session.relay.end",
            session=pair.session_id,
            room=pair.room_name,
            player_ip=pair.player_ip,
            up_bytes=pair.player_bytes,
            down_bytes=pair.tunnel_bytes,
        )


# ============================================================
# Protocol parsing helpers
# ============================================================

def parse_hello(line):
    """
    Parse the FIRST line sent by a client.

    Frozen minimum (old clients):
        "HOST <room>"
        "HOST_TUNNEL <room>"
        "PLAYER <room>"

    V1 additive (new clients), still two mandatory fields first:
        "HOST <room> <client_id> <username>"
        "PLAYER <room> <client_id> <username>"

    V2 additive (one more optional field, ignored by older servers):
        "HOST <room> <client_id> <username> <token>"
        "PLAYER <room> <client_id> <username> <token>"

    Returns (role, room_name, client_id, username, token, error) where error
    is one of None / "BAD_HANDSHAKE" / "BAD_ROOM".
    """
    parts = line.split()

    # 版本门（ADDITIVE）：新客户端在行尾多挂一个 `ver=<x.y.z>`。中继的版本
    # 检查在下面单独扫描整行，这里只是把它从**按位置解析**的字段里摘掉 ——
    # 否则 `HOST_TUNNEL <room> ver=3.0.8` 会把版本号当成 client_id。
    parts = [p for p in parts if not p.startswith(("ver=", "caps=", "sid=", "side=", "resume=", "generation=", "peer="))]

    if len(parts) < 2:
        return None, None, "", "", "", "BAD_HANDSHAKE"

    role = parts[0].upper()

    # 房间字段是 room token（ASCII）。老客户端发的裸名字在这里被归一化成同一个
    # token —— 见 room_names.py。长度上限用 token 的那一个：一个汉字编码后是
    # 9 个字符，用显示名的上限会把中文房间名全部拒掉。
    raw_room = parts[1] if len(parts) > 1 else ""
    room_name = room_names.room_token(clean_token(raw_room, cfg.MAX_ROOM_TOKEN_LEN))

    if not room_name:
        return None, None, "", "", "", "BAD_ROOM"

    client_id = ""
    username = ""
    token = ""

    if len(parts) >= 3:
        candidate = clean_token(parts[2], 64)
        if candidate is not None:
            client_id = candidate

    if len(parts) >= 4:
        candidate = clean_token(parts[3], cfg.MAX_USERNAME_LEN)
        if candidate is not None:
            username = candidate

    if len(parts) >= 5:
        candidate = clean_token(parts[4], 128)
        if candidate is not None:
            token = candidate

    return role, room_name, client_id, username, token, None


# ============================================================
# HOST
# ============================================================

def _host_position_client_id(room):
    """
    房间**主机位持有人**的 client_id（成员认领那一路），没有就空串。

    只认成员认领（`room.host_member`），不认 `room.host_id`：后者是"当前开服
    控制连接握手时报的 id"，正是要被顶替的那一条，拿它做判据等于自己跟自己比。
    老式（只有 HOST 连接、没有成员）房间里没有主机位持有人 -> 空串 ->
    `_same_host_person` 永远 False -> 保持旧的 ROOM_ALREADY_HAS_HOST 行为。
    """
    member = getattr(room, "host_member", None)

    if member is None:
        return ""

    return str(getattr(member, "client_id", "") or "").strip()


#: ★ v3.9.16：免登录模式下，"顶替 host 开服连接"要不要额外要求**同一个来源 IP**。
#:
#: 背景（分析报告 P1）：免登录模式下 `_same_host_person()` 只看 `client_id`，
#: 而 `client_id` 是随 `HOST_STATE` 的 `peers=` 广播给**房间里每一个人**的。
#: 于是房间里任何人都能顶掉 host 的开服连接 —— 一条现成的"反复打断房主开服"
#: 的路。免登录的本质是"房间名即凭证"，所以堵不住全部，但同一 IP 这条能把
#: 门槛从"知道房间名"抬到"和房主在同一个出口/同一台机器上"。
#:
#: 同一 NAT 出口下的两个人仍然互相顶得掉（他们的公网 IP 一样），但那本来
#: 就是免登录模式能接受的风险边界。设成 0 回到 v3.9.15 的行为。
HOST_SUPERSEDE_SAME_IP = cfg.env_flag("MCLINK_HOST_SUPERSEDE_SAME_IP", True)

#: ★ v3.9.17：旧开服连接静默超过这么久，就当它是"还没被收掉的尸体"，
#:
#: 免登录"同 IP"的门槛只在**旧连接还活着**时才适用 —— 见
#: `_same_host_person` 里那段。房主换出口 IP（WiFi/4G 切换、重启路由器）
#: 之后重开服，旧连接迟早会静默；到这个点就放行顶替，不然房主要等
#: `HOST_IDLE_TIMEOUT`（默认 25s）才能重新开服。
#:
#: 为什么是 10 秒：客户端心跳 3 秒一次，10 秒 = 连着错过 3 个心跳。
#: 一个**真在开服**的连接不可能静默这么久；而刚换完 IP 的旧连接
#: 通常两三个心跳内就已经不说话了。
HOST_SUPERSEDE_ACTIVE_WITHIN_S = float(
    os.environ.get("MCLINK_HOST_SUPERSEDE_ACTIVE_WITHIN_S", "10") or 0
)


def _same_host_person(client_id, host_member_id, token="", host_token="",
                      peer_ip="", host_ip="", host_idle=None):
    """
    新来的开服握手是不是**当前主机位持有人本人**。

    用于"同一个人重连开服 -> 顶替旧连接"（见 handle_host 的 [HOST SUPERSEDE]）。
    两条都要满足：

      * `client_id` 完全相同且非空（两边任何一边为空都不算同一个人）；
      * **账号体系开着时**（旧连接的 `host_token` 非空）新连接必须带**同一个
        token**。`client_id` 会随 `peers=` 广播给房间里每个人，光凭它就能顶替
        等于给了同房间的人一条"反复打断房主开服"的路；token 只在这个人的
        握手行里，别人拿不到。

    免登录 / 老客户端（token 本来就是空）**v3.9.16 起**还要看来源 IP
    （`HOST_SUPERSEDE_SAME_IP`）：client_id 是公开的，IP 不是 —— 同一个房间里
    的另一个人站不到 host 的出口 IP 上。

    两条边角都按"不加门槛"处理（保持旧行为，绝不因为这次加固把正常的重连
    挡在外面）：新连接拿不到 IP，或者旧连接没记下 IP（老式房间）。
    """
    client_id = str(client_id or "").strip()
    host_member_id = str(host_member_id or "").strip()

    if not (client_id and client_id == host_member_id):
        return False

    if str(host_token or "").strip():
        return str(token or "").strip() == str(host_token or "").strip()

    if HOST_SUPERSEDE_SAME_IP:
        _peer_ip = str(peer_ip or "").strip()
        _host_ip = str(host_ip or "").strip()

        # ============================================================================
        #  ★ v3.9.17：只在"旧连接还活着"时才要求同 IP
        # ============================================================================
        #
        # v3.9.16 是**无条件**比 IP。它挡住了"同房间的人用公开 client_id 顶替"，
        # 但也挡住了一种很常见的正常场景：房主 WiFi/4G 切换、重启路由器换出口
        # IP 后重开服 —— 旧连接还挂在内核里没被判定，新握手被判"不是同一个人"
        # -> `ERROR ROOM_ALREADY_HAS_HOST`，直到 `HOST_IDLE_TIMEOUT`（12s，
        # v3.9.16 放宽到 25s）把旧连接收掉。两个改动叠加，"开不了服"的窗口
        # 从 12 秒变成 25 秒（复核报告 P2）。
        #
        # 判据改成"旧连接还在说话吗"：
        #   * 旧连接**仍在活跃**（一直在发 PING）-> 那确实有另一个人在开服，
        #     要求同 IP 才允许顶替（这是这条加固真正要挡的）；
        #   * 旧连接**已经静默**（超过 `HOST_SUPERSEDE_ACTIVE_WITHIN_S` 没说过
        #     话）-> 它就是一具还没被收掉的尸体，顶替它不涉及任何安全收益，
        #     放行，让房主立刻能重新开服。
        #
        # 这样安全性没有下降（活人不会被顶），而"换网络后开不了服"消失。
        if _peer_ip and _host_ip:
            if host_idle is None or host_idle <= HOST_SUPERSEDE_ACTIVE_WITHIN_S:
                return _peer_ip == _host_ip

            # 旧连接已经静默很久：当它是尸体，放行。
            return True

    return True


#: ★ v3.9.16：host 开服连接断开之后，先等这么久再告诉全房间"host 停了"。
#:
#: 为什么要有这个宽限：客户端**模式热切换**（relay <-> stun）的实现是
#: "先 `stop_session()` 关掉 `HOST <room>`，再按新模式 `start_session()` 重开
#: 一条"。中继看不出这两步是同一次操作，只看到"开服连接断了"，于是立刻给全
#: 房间广播 `HOST_STOPPED` —— 所有玩家被踢下线。那不是用户要的：切个模式不该
#: 把房间里所有人踢掉。
#:
#: 所以断开后先等一小会儿再决定：
#:   * host 在窗口内回来了（模式重建实测 1~3 秒）-> 这次断开是重建的一部分，
#:     **不广播**（换模式本身已经由 `CONN_MODE_CHANGED` 清过一遍了）；
#:   * 窗口结束 host 没回来 -> 真停了，广播 `HOST_STOPPED`。
#:
#: 代价：真停时其他人的"host 已断开"提示晚 `HOST_STOP_GRACE_S` 秒。2.5 秒用户
#: 感知不到，换来的却是"不再把模式切换误判成 host 停服"。
#:
#: 设成 0 = 立刻广播（v3.9.15 的行为）。
HOST_STOP_GRACE_S = float(
    os.environ.get("MCLINK_HOST_STOP_GRACE_S", "2.5") or 0
)

#: 还在飞的"host 停服通知"任务。
#:
#: `asyncio` 有一个坑：没有外部引用的 Task 可能在跑完之前被 GC 回收。所以
#: `create_task` 之后一律丢进这个集合，完成回调里再摘掉。
_pending_notices = set()


async def announce_host_serving_gone(room, room_name, reason, code,
                                   immediate=False):
    """
    【v3.9.16】host 的开服连接结束了 —— 等一个宽限窗口，再决定要不要告诉全房间。

    放在后台任务里跑（`handle_host` 的 finally 不等待它），因为 finally 后面
    还有"没有 owner 就销毁房间"这类判定，不能被这几秒拖住。

    两个复查，缺一不可：

      1. **房间还是不是同一个？** 窗口里房间可能已经被销毁、甚至被同名重建。
         按**对象身份**比（`rooms.get(name) is room`），不比房间名 —— 同名的新
         房间是另一代人，绝不能拿上一代的结论去拆它。
      2. **host 回来了吗？** `room.host_writer` 非空 = 有人在窗口里重新开服了
         （模式重建）。那就什么都不做。

    永不抛异常：它只是"通知"，失败最多是有人没收到原因码 —— 而房间里的连接
    早就由 `disconnect_game_connections` 之外的路径（各自的 handler）拆掉了。
    """
    try:
        # ★ 房主**明确说了**要停（客户端关连接前发 `STOPPING`）时不用等：
        #   那是一次确定的操作，等 2.5 秒只会让玩家的游戏连接多挂 2.5 秒。
        #   宽限窗口只用于"说不清为什么断的"那种（掉线 / 崩溃 / 模式重建）。
        if HOST_STOP_GRACE_S > 0 and not immediate:
            await asyncio.sleep(HOST_STOP_GRACE_S)

        async with rooms_lock:
            current = rooms.get(room_name)

        if current is not room or getattr(room, "closed", False):
            log(
                "[HOST STOP]",
                f"room={room_name}",
                "reason=the room is gone (or was replaced) within the grace "
                "window -- nothing to announce",
            )
            return

        if getattr(room, "host_writer", None) is not None:
            # 模式热切换：客户端 stop 之后立刻重开了。**不广播** —— 否则房间里
            # 所有人都会因为"host 停服"被踢下线，而 host 其实一直都在。
            log(
                "[HOST STOP]",
                f"room={room_name}",
                f"grace={HOST_STOP_GRACE_S:g}s",
                "reason=the host started serving again within the grace window "
                "(mode rebuild) -- NOT announcing HOST_STOPPED",
            )
            return

        await room_members.disconnect_game_connections(
            room, reason=reason, code=code
        )

        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                broadcast_member_state(room), timeout=5
            )
    except Exception as e:  # noqa: BLE001
        log(
            "[HOST STOP NOTICE FAILED]",
            f"room={room_name}",
            f"error={e!r}",
        )


async def handle_host(reader, writer, room_name, client_id, username, token=""):
    peer = safe_peer(writer)

    enable_tcp_nodelay(writer)

    # Check whether the room is free BEFORE creating it. Creating the room
    # here and rejecting afterwards used to leave an empty phantom room in the
    # registry (visible in the room list and in the database mirror) whenever a
    # connection was refused.
    async with rooms_lock:
        room = rooms.get(room_name)
        existing_host = room.host_writer if room is not None else None
        current_host_member_id = _host_position_client_id(room)
        current_host_token = str(getattr(room, "host_token", "") or "")
        # ★ v3.9.16：免登录模式下再加一条判据 —— 旧开服连接的来源 IP。
        current_host_ip = str(getattr(room, "host_ip", "") or "")
        # ★ v3.9.17：那条判据只在**旧连接还活着**时才适用（否则房主换网络后
        #   要等 HOST_IDLE_TIMEOUT 才能重新开服）。
        current_host_idle = (
            room.host_idle_seconds() if room is not None else None
        )

    if existing_host is not None and not _same_host_person(
        client_id, current_host_member_id, token, current_host_token,
        peer_ip=peer_ip(writer), host_ip=current_host_ip,
        host_idle=current_host_idle,
    ):
        log(
            "[HOST REJECT]",
            f"room={room_name}",
            f"peer={peer}",
            "reason=room already has host",
        )
        writer.write(b"ERROR ROOM_ALREADY_HAS_HOST\n")
        await writer.drain()
        await close_writer(writer)
        return

    if existing_host is not None:
        # ============================================================================
        #  同一个人重连开服：**顶替**旧的开服控制连接，不要回 ROOM_ALREADY_HAS_HOST
        # ============================================================================
        #
        # 现场（用户报的 P0）：「房主切换后……热切换不能用了，连不上，隧道好像
        # 没有重建」。客户端换模式/停止→重开时是"先关掉旧的 `HOST <room>`，立刻
        # 再开一条新的"。服务端这边旧连接的 FIN 还没被读完时，新握手就已经到了
        # —— 旧代码一律回 `ERROR ROOM_ALREADY_HAS_HOST`，客户端只能按
        # `reconnect_delay`（默认 3s）退避重试；这 3 秒里 `host_alive()` 是假，
        # 玩家连接全部被判 `HOST_NOT_READY`，新隧道也会被旧 handler 的清理顺手
        # 打掉。表现就是"连不上、隧道没有重建"。
        #
        # 所以：当新连接证明自己是**同一个持有主机位的人**（client_id 相同）时，
        # 直接让新的接管：关掉旧 socket（旧 handler 的 finally 有身份守卫，
        # `room.host_writer is writer` 已经不成立 -> 它不会清状态、不会断连接），
        # 新连接立刻开服。一个人同一时刻仍然只有一个开服连接（§A 不变量不破）。
        #
        # 安全性：**只认"当前主机位持有人"的 client_id**（不是"任何知道这个 id
        # 的人"）。主机位本身有 CLAIM_HOST / RELEASE_HOST 一套权限；外人想顶替，
        # 先得拿下主机位（拿不到时 `claim_host` 会拒），所以没有新增可被滥用的面。
        log(
            "[HOST SUPERSEDE]",
            f"room={room_name}",
            f"client_id={client_id or '-'}",
            f"peer={peer}",
            "reason=same host position holder reconnecting",
        )

        with contextlib.suppress(Exception):
            existing_host.close()

    # 只允许**建房**（CREATE / 老式 HOST 首连）走 get_or_create：legacy
    # `HOST <room>` 本来就是老客户端的建房方式。`owner_key` 让"同一个 Owner
    # 重建同一个房间"能带回他上一次显式选过的连接模式。
    room = await get_or_create_room(room_name, owner_key=client_id)

    async with room.lock:

        if room.closed:
            # Room object died between lookup and attach.
            writer.write(b"ERROR ROOM_CLOSED\n")
            await writer.drain()
            await close_writer(writer)
            return

        # Re-check after the lock: two hosts could have raced here. 同一个人的
        # 新连接同样按上面的规则顶替（见 [HOST SUPERSEDE]）。
        if room.host_writer is not None:
            if not _same_host_person(
                client_id,
                _host_position_client_id(room),
                token,
                str(getattr(room, "host_token", "") or ""),
                peer_ip=peer_ip(writer),
                host_ip=str(getattr(room, "host_ip", "") or ""),
                host_idle=room.host_idle_seconds(),
            ):
                log(
                    "[HOST REJECT]",
                    f"room={room_name}",
                    f"peer={peer}",
                    "reason=room already has host (race)",
                )
                writer.write(b"ERROR ROOM_ALREADY_HAS_HOST\n")
                await writer.drain()
                await close_writer(writer)
                return

            # 同一个人重连：旧连接让位（见上面 [HOST SUPERSEDE] 的说明）。
            log(
                "[HOST SUPERSEDE]",
                f"room={room_name}",
                f"client_id={client_id or '-'}",
                "reason=same host position holder reconnecting (race)",
            )

            with contextlib.suppress(Exception):
                room.host_writer.close()

        room.host_reader = reader
        room.auto_generation = uuid.uuid4().hex
        room.host_writer = writer
        room.host_id = client_id
        room.host_username = username
        room.host_ip = peer_ip(writer)
        room.host_token = token
        # ============================================================================
        #  心跳基线必须跟着**新连接**一起重置
        # ============================================================================
        #
        # `host_last_seen` 是"这条开服连接最后一次说话的时刻"。顶替路径
        # （[HOST SUPERSEDE]）里旧的 `handle_host` finally 被身份守卫拦住、
        # **不会**清它 —— 如果旧连接恰好已经静默很久，host_watchdog 下一次
        # 巡检（`room.host_writer is not None and idle > timeout`）会把**刚建好
        # 的新连接**当成僵尸关掉。表现又是"刚重开就被踢，隧道重建不起来"。
        #
        # 置 None = `host_idle_seconds()` 返回 0.0，从"新连接还没说话"开始计时，
        # 由客户端的心跳（3s 一次）把它喂起来。非顶替路径本来就是 None，这里
        # 只是同一个不变量在两条路径上都成立。
        room.host_last_seen = None

        # ============================================================================
        #  ★ v3.9.17：新开服连接建立 -> "主动停止"标记清零
        # ============================================================================
        #
        # 它描述的是"**上一次**连接是怎么结束的"。新连接一建立，那个结论就
        # 过期了：这一次还没结束，谈不上"主动停止"。
        #
        # 不清的后果（v3.9.16 实测）：房主正常停一次之后，之后每一次掉线 /
        # 崩溃 / 模式重建都被当成"主动停止" —— 2.5 秒宽限窗口被永久跳过，
        # 模式热切换照旧把全房间踢下线。
        room.host_stopping = False

    log(
        "[HOST ONLINE]",
        f"room={room_name}",
        f"peer={peer}",
        f"client_id={client_id or '-'}",
        f"username={username or '-'}",
    )

    # ---- 审计：房主上线记一笔 ----
    #
    # The panel's log had no host records at all: room *sessions* are logged
    # when a player pairs, but "who opened the world, and when" was nowhere.
    # That is the single most useful line when somebody asks "那会儿是谁在开服".
    if store is not None:
        with contextlib.suppress(Exception):
            await store.add_event(
                "host_online",
                actor=username or "",
                room_id=room_name,
                detail=f"开始开服（客户端 {client_id or '-'}，来自 {peer}）",
                source_ip=peer_ip(writer),
            )

    room.mark_dirty()

    # Host 的开服控制连接 = "Host 已开始连接" 的证据：HOST_STATE 的 `started=`
    # 立刻要反映出来，房间里的 Player 才知道"Host 开服了，可以开始打洞"。
    await broadcast_member_state(room)

    notifier = asyncio.create_task(host_notifier(room))

    try:
        # ---- FROZEN handshake reply -------------------------
        # Sent through send_to_host() so it can never interleave with an
        # INFO push from the notifier task.
        await room.send_to_host(f"OK HOST room={room_name}\n")
        # -----------------------------------------------------

        # ---- V2 additive: tell the client which server it reached ----
        await room.send_to_host(
            f"INFO protocol={cfg.PROTOCOL_VERSION}\n"
        )
        # --------------------------------------------------------------

        # The host CONTROL socket carries no Minecraft data. It stays open
        # and is used for keep-alive, PING/PONG and INFO pushes.
        await host_control_loop(room, reader)

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log("[HOST ERROR]", f"room={room_name}", f"error={e}")

    finally:
        notifier.cancel()
        try:
            await notifier
        except (asyncio.CancelledError, Exception):
            pass

        log("[HOST OFFLINE]", f"room={room_name}", f"peer={peer}")

        # 审计：房主下线也记一笔（和 host_online 成对，日志里能看出开了多久）
        if store is not None:
            with contextlib.suppress(Exception):
                await store.add_event(
                    "host_offline",
                    actor=username or "",
                    room_id=room_name,
                    detail="停止开服（房间仍然存在）",
                )

        # ====================================================================
        #  身份守卫：这条 handler 还**是不是**当前的开服连接？
        # ====================================================================
        #
        # `handle_host` 的 finally 会清 `host_writer` / 断开所有游戏连接。
        # 那是对的 —— 但只有当**它自己**还是当前开服连接时才成立。
        #
        # 热切换（换模式 / 停止→立刻重开）的现场是：客户端先关掉旧的
        # `HOST <room>`，立刻再开一条新的。旧 handler 的收尾里夹着
        # `await`（DB 审计、drain、remove_session），新连接完全可能已经
        # 认领上房间并建好了隧道 —— 如果这时旧 handler 还无条件地清状态、
        # 断连接，它就会把**新的**那套一起打掉，表现就是用户说的
        # 「连不上、隧道好像没有重建」。
        #
        # `room.host_writer is writer` 就是"这条连接仍然是权威"的判据
        # （与 `drop_room_if_empty` / `delete_room_owned` 的对象身份守卫同一
        # 口径）。不是的话直接收工，把房间留给新的那条连接。
        async with room.lock:
            still_current_host = room.host_writer is writer

        if not still_current_host:
            log(
                "[HOST STALE]",
                f"room={room_name}",
                f"peer={peer}",
                "reason=a newer host control connection owns the room",
            )
            return

        # ====================================================================
        # THE HOST CONNECTION ENDING MUST NOT DESTROY THE ROOM
        # ====================================================================
        #
        # This used to be `await room.close(reason="host offline")`, on the
        # reasoning that "host disappearing means the whole room is invalid".
        # That was true in the old design, where the HOST connection WAS the
        # room: there was no other way to create one.
        #
        # It is not true now, and the old behaviour was actively harmful. The
        # HOST connection is the MINECRAFT session -- it is torn down whenever
        # the host presses 停止, or when their game connection drops for any
        # reason. Treating that as "the room is over" meant:
        #
        #     host presses 停止  ->  the room is destroyed
        #                        ->  every member is kicked out
        #                        ->  the room vanishes from the panel
        #
        # which is a catastrophic surprise: stopping the game is not the same
        # as closing the room, and the host never asked to disband anything.
        #
        # The room's lifetime belongs to the OWNER, and the owner is tracked on
        # their MEMBER connection (see room_members.py). So all this does now
        # is disconnect the players and mark the host as "not currently
        # hosting" -- the room itself, the host POSITION and everyone sitting
        # in it stay exactly where they are.
        #
        # If the owner really does leave, their member control connection
        # closes and handle_member_join() destroys the room properly.
        member = room.host_member

        if member is not None:
            # ============================================================================
            #  主机位置在 Minecraft 连接结束时【保留】（用户明确要求）
            # ============================================================================
            #
            # 这个位置属于"人"，不属于那一条隧道。连接的结束只说明"他现在没在
            # 开服"，不代表他不想当主机了 —— 后者要他自己点「释放主机身份」。
            #
            # 以前这里会把非所有者的位置直接清掉，于是出现用户报的那个矛盾：
            # 客户端明明还显示"你是主机"，房间列表却说「无人认领」。
            # 现在两边都按"位置归谁"来说话，只有三种情况会空出来：
            # 主动 RELEASE_HOST / 这个人离开房间（成员连接断了）/ 被踢。
            log(
                "[HOST IDLE]",
                f"room={room_name}",
                f"host={member.label}",
                "reason=minecraft session ended (host position kept)",
            )

        # ---- 释放 HOST「连接」占用的状态（这一步以前漏了，代价很大）----
        #
        # Clearing this is not bookkeeping, it is THE fix for three symptoms
        # that looked unrelated:
        #
        #  1. 「房主一按停止，房间就没了」—— `host_writer` 留着不清，
        #     host_watchdog 就认为"房主还连着但是不吭声了"，等
        #     HOST_IDLE_TIMEOUT 秒后调用 room.close()，**整个房间被销毁**。
        #     用户看到的是"房主断开 → 房间跟着消失"，而且晚了几十秒，
        #     根本联系不到一起。
        #  2. 房主想重新开服 → handle_host 看到 host_writer 还在，
        #     直接回 ERROR ROOM_ALREADY_HAS_HOST，再也开不起来。
        #  3. 面板/房间列表把已断开的房主算成"在线"，人数也跟着错。
        #
        # 注意这里清的是**连接**（谁在开服），不是**位置**（谁是主机）：
        # 位置还在那个成员手上（member.is_host），列表里仍然显示他的名字，
        # 只是标成「未开服」。要交出位置必须他本人点「释放主机身份」。
        async with room.lock:
            room.host_reader = None
            room.host_writer = None
            room.host_id = ""
            room.host_username = ""
            room.host_ip = ""
            room.host_token = ""
            room.host_last_seen = None

        room.mark_dirty()

        # Nobody can play without a live host connection, so every game
        # connection goes -- **including the ones still waiting in the pairing
        # pool**. `drop_all_sessions` only walked `room.sessions`, so a PLAYER
        # that had opened its socket but not yet been paired stayed in
        # `waiting_players` with no host to pair against; from that player's side
        # the host "disappeared" while the relay still showed it in the room
        # (用户 P0 #7：两端表现不一致). `disconnect_game_connections` covers
        # sessions + waiting players + waiting tunnels and gives each one the
        # recognizable `HOST_LEFT` reason.
        # ============================================================================
        #  ★ v3.9.16：**不再立刻**广播 HOST_STOPPED，交给宽限窗口去判断
        # ============================================================================
        #
        # 立刻广播的话，客户端"模式热切换"（先 stop 再 start）会被全房间当成
        # "host 停服"，所有人被踢下线 —— 而 host 其实一秒后就回来了。
        #
        # 所以这里只**排一个后台任务**（不 await，finally 后面的房间销毁判定
        # 不能被它拖住）：等 `HOST_STOP_GRACE_S` 再复查，host 没回来才广播。
        # 见 `announce_host_serving_gone` 的说明。
        #
        # 原因文本要能区分"主动停止"和"连接中断"：客户端在关闭开服连接前发一行
        # `STOPPING`（additive，老客户端不发），这里据此给出不同措辞 —— 排障时
        # 一眼能看出房主是点了停止，还是掉线/崩了。
        # ★ v3.9.17：`host_stopping` 是**一次性**的 —— 读走就清掉。
        #
        # 不清的话（v3.9.16 的行为）：房主正常停过一次之后，这个房间的每一次
        # 后续断开（掉线、崩溃、模式重建）都被当成"主动停止" —— 2.5 秒宽限窗口
        # 被永久跳过，措辞也永远是错的。
        #
        # `pop` 而不是"读完再置 False"：这里和下面的读是同一处，一次读走最
        # 不容易漏。
        _stopping = bool(getattr(room, "host_stopping", False))
        room.host_stopping = False

        _stop_reason = (
            "host 已停止开服" if _stopping
            else "host 已断开连接（未开服）"
        )

        _notice = asyncio.create_task(
            announce_host_serving_gone(
                room,
                room_name,
                # ★ 用户 2026-09-30 定稿：host **主动停止开服** -> 其他人断开 +
                #   重算按钮状态，**不自动重连**、**不弹窗**。用独立原因码，
                #   客户端据此和"闪断"（HOST_LEFT，要尽量复用/自动重连）分开处理。
                reason=_stop_reason,
                code=room_members.HOST_STOPPED_CODE,
                # 只有**明确说了**要停才跳过宽限。掉线/崩溃/模式重建必须走
                # 宽限窗口 —— 那是它存在的唯一理由。
                immediate=_stopping,
            )
        )

        # 别让"没人引用"的任务被 GC 掉（asyncio 的已知坑），也别让它变成孤立
        # 任务：房间销毁时它会自己查出来并退出。
        _pending_notices.add(_notice)
        _notice.add_done_callback(_pending_notices.discard)

        # host 不再开服这件事**立刻**广播（不等宽限窗口）：`started=` /
        # `host=` 状态在房主按下停止的那一刻就该变，房间列表不该延迟 2.5 秒
        # 才反映出来。
        await broadcast_member_state(room)

        # ---- the one case that DOES end the room ----
        #
        # A room with no OWNER has nothing keeping it alive: there is no member
        # connection whose lifetime it is tied to, so if this was the last
        # thing holding it, it would linger forever as an unplayable ghost.
        # That happens when a client creates a room the OLD way (HOST only,
        # never joining as a member), which older clients still do.
        if room.owner is None and room.member_count() == 0:
            log(
                "[ROOM CLOSE]",
                f"room={room_name}",
                "reason=no owner and the host session ended",
            )

            await room.close(reason="host offline")
            await retire_room(room_name, room)
            return

        log(
            "[ROOM KEPT]",
            f"room={room_name}",
            f"members={room.member_count()}",
            f"owner={(room.owner.label if room.owner else '(无)')}",
            "reason=the room belongs to its owner, not to the game session",
        )


async def host_control_loop(room, reader):
    """
    Read control lines from the HOST.

    Supported (all optional, all additive):
        PING <anything>   -> "PONG <anything>"
        JSON              -> ignored for now (reserved for V2 keep-alive)

    Anything else is ignored. Nothing here ever touches a data tunnel.
    """
    while True:

        line = await reader.readline()

        if not line:
            raise ConnectionError("host control connection closed")

        text = line.decode("utf-8", errors="replace").strip()

        if not text:
            continue

        # 任何来自房主的字节都算心跳（客户端每 3 秒发一次 PING）
        room.touch_host()

        upper = text.upper()

        # ---- v3.9.16: host 主动停止（ADDITIVE，老客户端不发）----
        if upper.startswith("STOPPING"):
            room.host_stopping = True
            continue

        # ---- frozen behaviour: answer PING ----
        if cfg.ENABLE_CONTROL_PING and upper.startswith("PING"):
            payload = text[4:].strip()
            await room.send_to_host(f"PONG {payload}\n" if payload else "PONG\n")
            continue

        # ---- V2: the host asks us to delete its own room ----
        if upper.startswith("DELETE"):
            await host_request_delete(room, text)
            continue

        if cfg.VERBOSE:
            log("[HOST MSG]", f"room={room.name}", f"line={text[:64]!r}")


async def host_request_delete(room, line):
    """
    Handle "DELETE <token>" on the HOST control socket.

    Same ownership rule as the DELETE connection type, but this avoids opening
    a second socket. Refused while players are connected.
    """
    parts = line.split()
    token = parts[1] if len(parts) > 1 else ""

    if accounts is not None and accounts_required():
        ok, identity = await accounts.authorize(token)

        if not ok:
            await room.send_to_host(f"ERROR {identity}\n")
            return

        requester = identity["username"]

        # **只认所有者**（用户 2026-09-25 再次确认："既然加门了就删掉那些兜底"）。
        #
        # 以前这里还允许"host 也能删"，之前又给"没有所有者的房间"留了一个
        # 退回 host 的兜底。版本门上了以后，能连上来的客户端一定会经过
        # CREATE/JOIN（那样房间必然有所有者），所以"无主房间"在生产环境里
        # 已经不可能出现 —— 兜底只剩"多一条能绕过所有者的路"这一种作用。
        # 删掉它的收益是：房主的身份**只有一种来源**，不会再出现"某种情况下
        # host 居然能删别人的房间"这类只能靠日志才查得清的怪事。
        owner_name = room_owner_username(room)

        if not (owner_name and requester == owner_name):
            log(
                "[DELETE DENIED]",
                f"room={room.name}",
                f"requester={requester}",
                f"owner={owner_name or '(none)'}",
                "reason=not the room owner",
            )
            await room.send_to_host("ERROR NOT_HOST\n")
            return

    else:
        # 没有账号体系时：用 client_id 证明"这是我的房间"，同样**只认所有者**。
        # `DELETE <token>` 里的 token 在这个模式下就是 client_id。
        owner = getattr(room, "owner", None)
        owner_id = str(getattr(owner, "client_id", "") or "")

        if not (owner_id and token == owner_id):
            log(
                "[DELETE DENIED]",
                f"room={room.name}",
                f"param_client={token!r}",
                f"owner_id={owner_id or '(none)'}",
                "reason=not the room owner (auth disabled)",
            )
            await room.send_to_host("ERROR NOT_HOST\n")
            return

    if room.active_count() > 0:
        log(
            "[DELETE DENIED]",
            f"room={room.name}",
            f"players={room.active_count()}",
            "reason=room busy",
        )
        await room.send_to_host("ERROR DELETE_ROOM_BUSY\n")
        return

    log("[ROOM DELETE]", f"room={room.name}", "by=host control")

    if store is not None:
        await store.add_event(
            "client_delete_room",
            actor=room.host_username,
            room_id=room.name,
            detail="房主通过控制连接删除房间",
        )

    await room.send_to_host(f"OK DELETE room={room.name}\n")

    await room.close(reason="deleted by host")
    await retire_room(room.name, room)


async def host_notifier(room):
    """
    Push small INFO lines to the HOST control socket when the room changes.

    Why the host and not the player: the host control socket already exists,
    it is not latency critical, and an old host client that ignores unknown
    lines keeps working. A PLAYER socket must receive exactly "CONNECTED"
    before raw data starts, so it is left untouched.
    """
    if not cfg.ENABLE_CONTROL_INFO:
        return

    last = None

    try:
        while True:

            room.mark_dirty()
            await room.dirty.wait()
            room.dirty.clear()

            await asyncio.sleep(0.2)  # coalesce bursts

            snap = await room.snapshot()

            info = (
                f"INFO players={snap['players']}/{snap['max_players']} "
                f"idle_tunnels={snap['idle_tunnels']} "
                f"waiting_players={snap['waiting_players']}\n"
            )

            if info == last:
                continue

            last = info

            if not await room.send_to_host(info):
                return

    except asyncio.CancelledError:
        raise

    except Exception:
        return


# ============================================================
# HOST TUNNEL
# ============================================================

async def handle_host_tunnel(reader, writer, room_name, client_id, username):
    peer = safe_peer(writer)

    enable_tcp_nodelay(writer)

    # ============================================================================
    #  【只读查找】——隧道握手绝不创建房间
    # ============================================================================
    #
    # 这里以前是 `get_or_create_room`：一个**探测性/迟到的** HOST_TUNNEL 握手
    # 会把一个全新的 Room 装进注册表，而新 Room 的连接模式是构造默认值
    # "relay" —— 于是"没有任何人操作，模式自己跳回了 relay"。
    #
    # 没有房间时下面第 1 道门禁本来就是 `ERROR NO_HOST`（新建的空房间里没有
    # Host，回复逐字相同），所以改成只读查找对客户端**没有任何行为差异**，
    # 只是不再偷偷往注册表里塞一个默认模式的房间。
    room = await resolve_room(room_name)

    if room is None:
        log(
            "[TUNNEL REJECT]",
            f"room={room_name}",
            f"peer={peer}",
            "reason=no such room (tunnel handshake never creates one)",
        )
        writer.write(b"ERROR NO_HOST\n")
        await writer.drain()
        await close_writer(writer)
        return

    # ============================================================================
    #  门禁（用户 2026-09-27 设计基准 §B）：两级，且原因码可区分
    # ============================================================================
    #
    #   * 房间里**没有 Host**（没人认领主机位）-> ERROR NO_HOST
    #   * 有人认领了主机位，但 Host **还没开始连接**（没有 `HOST <room>` 开服
    #     控制连接）-> ERROR HOST_NOT_READY
    #
    # 老客户端只认 NO_HOST；新客户端对两个码给不同提示（「暂无 Host，请等待
    # 认领」 vs 「Host 未连接，请等待 Host 开服」）。两条都是 ADDITIVE：
    # 老客户端把不认识的码当普通错误处理，行为不变。
    if not room.host_claimed:
        log(
            "[TUNNEL REJECT]",
            f"room={room_name}",
            f"peer={peer}",
            "reason=no host",
        )
        writer.write(b"ERROR NO_HOST\n")
        await writer.drain()
        await close_writer(writer)
        return

    if not await room.host_alive():
        log(
            "[TUNNEL REJECT]",
            f"room={room_name}",
            f"peer={peer}",
            "reason=host not ready",
        )
        writer.write(
            f"ERROR {room_members.HOST_NOT_READY_CODE} "
            "Host 尚未开始连接，请等待 Host 开服\n".encode()
        )
        await writer.drain()
        await close_writer(writer)
        return

    log("[TUNNEL WAIT]", f"room={room_name}", f"peer={peer}")

    # Add to the idle tunnel pool.
    waiter = await room.add_tunnel(reader, writer)

    if waiter is None:
        await close_writer(writer)
        return

    # Wait until this tunnel is paired with a player, or the room dies.
    await waiter.assigned.wait()

    session_id = waiter.session_id

    if session_id is None:
        log("[TUNNEL DROPPED]", f"room={room_name}", "reason=room closed")
        await close_writer(writer)
        return

    async with room.lock:
        pair = room.sessions.get(session_id)

    if pair is None:
        # 配对在等待期间消失了（主机停了/房间关了）。同样要给出原因。
        with contextlib.suppress(Exception):
            writer.write(b"ERROR ROOM_CLOSED\n")
            await writer.drain()
        await close_writer(writer)
        return

    log(
        "[TUNNEL ASSIGNED]",
        f"room={room_name}",
        f"session={session_id}",
    )

    try:
        # Wait for the host client to connect to its local Minecraft
        # server and send the frozen "READY" line.
        #
        # 必须有超时：房主那边的本机 Minecraft 拒连 / 卡住时，READY 永远不来，
        # 这一对就会一直占着名额（用户报的"第二个人连不上"就是这么来的）。
        try:
            line = await asyncio.wait_for(
                reader.readline(), timeout=cfg.PAIR_READY_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"host tunnel sent no READY in {cfg.PAIR_READY_TIMEOUT}s"
            )

        if not line:
            raise ConnectionError("host tunnel closed before READY")

        text = line.decode("utf-8", errors="replace").strip()

        if text != "READY":
            log(
                "[TUNNEL ERROR]",
                f"session={session_id}",
                f"expected READY, got={text[:32]!r}",
            )
            return

        log("[HOST READY]", f"room={room_name}", f"session={session_id}")

        pair.host_ready.set()

        # The PLAYER side owns starting the single relay task.
        await pair.start_relay.wait()

        if pair.relay_task is not None:
            await pair.relay_task

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log(
            "[TUNNEL ERROR]",
            f"room={room_name}",
            f"session={session_id}",
            f"error={e}",
        )

    finally:
        await room.remove_session(session_id, reason="tunnel closed")


# ============================================================
# PLAYER
# ============================================================

async def handle_player(reader, writer, room_name, client_id, username, token=""):
    peer = safe_peer(writer)

    enable_tcp_nodelay(writer)

    # ============================================================================
    #  【只读查找】——游戏连接握手绝不创建房间
    # ============================================================================
    #
    # 与 handle_host_tunnel 同一处根因：一个迟到 / 走错房间名的 PLAYER 握手
    # 会 `get_or_create_room` 出一个全新的、模式为构造默认值 "relay" 的房间，
    # 然后 Owner 一重连就落进这个"新房间"，广播里 `mode=relay` 而 Owner 从没
    # 切过 —— 这正是用户报的"没人操作，模式自己跳回中转"。
    #
    # 没有房间时下面第 1 道门禁本来就是 `ERROR NO_HOST`，回复逐字相同。
    room = await resolve_room(room_name)

    if room is None:
        log(
            "[PLAYER REJECT]",
            f"room={room_name}",
            f"peer={peer}",
            "reason=no such room (player handshake never creates one)",
        )
        writer.write(b"ERROR NO_HOST\n")
        await writer.drain()
        await close_writer(writer)
        return

    # The room must have a host, and the host must have STARTED.
    #
    # 见 handle_host_tunnel 里同一段说明：没 Host -> NO_HOST；Host 认领了但
    # 还没开服 -> HOST_NOT_READY（客户端可识别，提示「Host 未连接，请等待
    # Host 开服」）。玩家点「开始连接」时绝不允许进正式流程。
    if not room.host_claimed:
        log(
            "[PLAYER REJECT]",
            f"room={room_name}",
            f"peer={peer}",
            "reason=no host",
        )
        writer.write(b"ERROR NO_HOST\n")
        await writer.drain()
        await close_writer(writer)
        return

    if not await room.host_alive():
        log(
            "[PLAYER REJECT]",
            f"room={room_name}",
            f"peer={peer}",
            "reason=host not ready",
        )
        writer.write(
            f"ERROR {room_members.HOST_NOT_READY_CODE} "
            "Host 尚未开始连接，请等待 Host 开服\n".encode()
        )
        await writer.drain()
        await close_writer(writer)
        return

    log(
        "[PLAYER CONNECT]",
        f"room={room_name}",
        f"peer={peer}",
        f"username={username or '-'}",
    )

    waiter = await room.add_player(reader, writer, client_id, username)

    if waiter == "stream_limit":
        writer.write(b"ERROR AUTO_STREAM_LIMIT\n")
        await writer.drain()
        return

    if waiter == "full":
        log(
            "[PLAYER REJECT]",
            f"room={room_name}",
            f"peer={peer}",
            "reason=room full",
        )
        writer.write(b"ERROR ROOM_FULL\n")
        await writer.drain()
        await close_writer(writer)
        return

    if waiter == "closed":
        # 说清楚原因再关。以前这里是【一句话不说就关】，客户端只能报
        # "中转服务器关闭了连接" —— 用户看到的是一句没有任何信息的错误，
        # 而真相（房间已经在关闭中）就在这一行里。
        with contextlib.suppress(Exception):
            writer.write(b"ERROR ROOM_CLOSED\n")
            await writer.drain()
        await close_writer(writer)
        return

    # ============================================================================
    #  这个成员"已经开始连接"了 —— 立刻广播（设计基准 §D/§E 的 `started=`）
    # ============================================================================
    #
    # PLAYER 连接本身就是"点了开始连接"的证据。其他人（尤其是 Host）必须
    # 马上从 HOST_STATE 的 `started=` 里看到它，才能把打洞目标列表更新到
    # "当前全部已开始的 Player"。不广播的话，对方要等到下一次有人认领/释放
    # 主机位才知道，窗口内会对着旧列表打。
    await broadcast_member_state(room)

    # ============================================================================
    #  掉线看护：等待 / 配对阶段 socket 断了 -> 立刻广播（`conn=` 当场变 idle）
    # ============================================================================
    #
    # 等待期间 relay 不读这条 socket，所以它断开时没有任何东西会立刻发现
    # （详见 room_members.watch_player_socket 的说明）。这个任务用
    # `reader.at_eof()` 轮询 —— 一个字节都不读，对冻结的数据通路零影响。
    socket_watch = asyncio.create_task(
        room_members.watch_player_socket(room, waiter, reader)
    )

    # Wait for a tunnel assignment (or for the room to die).
    #
    # 看护任务必须和这条 handler 同生共死：等待期间被取消（服务端收工）也要
    # 把它收掉，绝不留一个没人管的轮询任务。
    try:
        await waiter.assigned.wait()
    except BaseException:
        socket_watch.cancel()
        raise

    session_id = waiter.session_id

    if session_id is None:
        log("[PLAYER DROPPED]", f"room={room_name}", "reason=room closed")
        socket_watch.cancel()
        with contextlib.suppress(Exception):
            writer.write(b"ERROR ROOM_CLOSED\n")
            await writer.drain()
        await close_writer(writer)
        return

    async with room.lock:
        pair = room.sessions.get(session_id)

    if pair is None:
        socket_watch.cancel()
        await close_writer(writer)
        return

    log(
        "[PLAYER ASSIGNED]",
        f"room={room_name}",
        f"session={session_id}",
        f"peer={peer}",
    )

    try:
        # Wait until the host client connected to its local Minecraft
        # server and sent READY.
        await pair.host_ready.wait()

        if writer.is_closing():
            return

        # ---- FROZEN: the last control line on this socket ----
        writer.write(pair.auto.handshake("player", "CONNECTED_AUTO") if pair.auto else b"CONNECTED\n")
        await writer.drain()
        # ------------------------------------------------------

        # 到这一步玩家才真的"进入世界"了（从这里开始两边都是原始字节）。
        # 显示口径只认这个标记，幽灵会话不算人。
        pair.established = True

        # 立刻告诉房间里所有人：人数变了。
        # （HOST_STATE 的 world= 就是客户端「已加入世界」那一行，不广播的话
        #   它会一直停在旧数字，直到下次有人认领/释放主机。）
        with contextlib.suppress(Exception):
            await broadcast_member_state(room)

        log(
            "[PLAYER READY]",
            f"room={room_name}",
            f"session={session_id}",
        )

        # "In the world" -- the only state that counts as a player for display
        # purposes. Recorded with its own event name so the log distinguishes
        # PAIRED (socket attached, nothing proven) from ESTABLISHED (CONNECTED
        # sent, bytes about to flow). The two used to be the same word in the
        # log, which is how a ghost session looked like a real player.
        event(
            "session.established",
            room=room_name,
            session=session_id,
            player=pair.player_username or "",
            player_ip=pair.player_ip,
            host_user=pair.host_username,
            host_ip=pair.tunnel_ip,
            waited=round(time.time() - pair.created_at, 3),
        )

        room.mark_dirty()

        # Start EXACTLY ONE bidirectional relay task.
        pair.relay_task = asyncio.create_task(pair.auto.run() if pair.auto else bidirectional_relay(pair))

        pair.start_relay.set()

        await pair.relay_task

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log(
            "[PLAYER ERROR]",
            f"room={room_name}",
            f"session={session_id}",
            f"error={e}",
        )

    finally:
        # 看护任务和这条连接同生共死（它自己也会在"已进世界"时退出）。
        socket_watch.cancel()

        with contextlib.suppress(BaseException):
            await socket_watch

        await room.remove_session(session_id, reason="player closed")


# ============================================================
# V2 -- LOGIN / ROOMS / DELETE
# ============================================================
#
# These are NEW connection types on the same port. They are strictly additive:
# a v1 client never sends them, and a v1 server would simply read
# "LOGIN __mclink__ ..." as a two-field handshake for a room named
# "__mclink__" and answer ERROR UNKNOWN_ROLE, which the v2 client reports as
# "server too old".


async def handle_login(reader, writer, username, password, client_id):
    """
    Authenticate over the control plane.

    Request :  LOGIN __mclink__ <username> <password> <client_id>
    Reply   :  OK LOGIN user=<name> admin=0/1 protocol=2 token=<token>
               ERROR AUTH_BAD_CREDENTIALS / AUTH_DISABLED / AUTH_RATE_LIMIT /
                     AUTH_UNAVAILABLE / AUTH_BAD_REQUEST
    """
    peer = safe_peer(writer)
    ip = peer_ip(writer)

    if accounts is None:
        writer.write(b"ERROR AUTH_DISABLED_SERVER\n")
        await writer.drain()
        await close_writer(writer)
        return

    log("[LOGIN]", f"user={username or '-'}", f"ip={ip}")

    ok, payload, detail = await accounts.login(
        username, password, ip=ip, client_id=client_id
    )

    if not ok:
        log("[LOGIN FAIL]", f"user={username or '-'}", f"ip={ip}", f"reason={payload}")
        writer.write(f"ERROR {payload}\n".encode())
        await writer.drain()
        await close_writer(writer)
        return

    log("[LOGIN OK]", f"user={payload['username']}", f"ip={ip}")

    # ---- additive reply: extra fields are ignored by older clients ----
    #
    # self_ops_* is the monthly self-service allowance (改名 + 改密码 共用).
    # A client that shows it can tell the player how many changes are left
    # instead of letting them find out by being refused.
    self_ops_left = payload.get("self_ops_left")
    self_ops_limit = payload.get("self_ops_limit")

    line = (
        f"OK LOGIN user={payload['username']} "
        f"admin={payload['is_admin']} "
        f"role={payload.get('role', 'player')} "
        f"protocol={cfg.PROTOCOL_VERSION} "
        # 服务器的人数上限：客户端在建房/开始前拿它和自己的设置比一下，
        # 超过就直接拒绝并说清楚，而不是连上去之后卡死超时。
        f"max_players={max_players()} "
        f"self_ops_left="
        f"{'unlimited' if payload.get('self_ops_unlimited') else self_ops_left} "
        f"self_ops_limit={self_ops_limit if self_ops_limit else 0} "
        f"self_ops_month={payload.get('self_ops_month', '')} "
        f"token={payload['token']}\n"
    )
    writer.write(line.encode())
    await writer.drain()

    await close_writer(writer)


async def handle_rooms_query(writer, room_name=""):
    """
    List the rooms currently online.

    Request :  ROOMS __mclink__ <client_id> <username>
    Reply   :  ROOMS <n> auth=<0|1>\n
               ROOM <room> <host> <players>/<max> <idle_tunnels> <waiting> <empty|busy>\n   (n times)
               DONE\n

    The `auth=` field is additive: older clients ignore unknown key=value
    pairs on the ROOMS line, so the frozen part of the protocol is untouched.

    WHY IT IS HERE: this is the only request the relay answers WITHOUT a
    token, which makes it the natural place for a client to ask "do I need to
    log in at all?". The client used to answer that question from a local
    hardcoded assumption ("accounts are always required") and prompt for a
    login before it ever contacted the relay -- so turning the switch off in
    the panel had no visible effect on the client whatsoever.

    Deliberately no IP addresses: the panel shows those, players do not.
    Player usernames ARE listed because they are visible in game anyway.
    """
    async with rooms_lock:
        room_list = list(rooms.values())

    snapshots = []

    for room in room_list:
        try:
            snapshots.append(await room.snapshot())
        except Exception:
            continue

    auth_flag = 1 if accounts_required() else 0

    writer.write(f"ROOMS {len(snapshots)} auth={auth_flag}\n".encode())

    for snap in snapshots:
        # 第 2 列 = 【房主】= 建房的那个人（所有者），建房就显示，不用等谁去认领主机。
        owner_name = snap.get("owner_username") or ""
        host_name = snap.get("host_now") or snap.get("host_username") or ""

        # ============================================================================
        #  ★ 用户 2026-09-30 实测：**不许把 host 当成房主**
        # ============================================================================
        #
        # 旧代码是 `creator = owner_name or host_name or ...`。那个 host 兜底是给
        # **老式房间**（只有 `HOST <room>` 控制连接、根本没有成员记录，创建者就是
        # 开服的人）准备的。可是 v3 房间在"房主此刻不在"时（掉线被宽限 / 已经离开
        # 但房间还没销毁 / 房间被顶替成无主状态）`owner_username` 也是空 —— 于是
        # **host 就被显示成房主**。
        #
        # 用户的现场：房主网卡断了，朋友（host）在房间列表里同时是「房主」和
        # 「当前主机」，而他甚至已经不在房里了。
        #
        # 现在只有**真的没有成员记录**的老式房间才退回 host；v3 房间 owner 不在
        # 就是没有房主（那一列显示 `-`，绝不冒充）。
        legacy_room = not snap.get("member_list")

        if owner_name:
            creator = owner_name
        elif legacy_room:
            creator = host_name or snap.get("host_client_id") or "-"
        else:
            creator = "-"

        creator = "".join(
            c for c in str(creator) if ord(c) >= 33 and ord(c) != 127
        ) or "-"

        # 第 7 个字段之后是新增的：谁在当主机。老客户端读到 state 就停了，
        # 不认识的字段会被忽略，所以协议依旧兼容。
        current_host = "".join(
            c for c in str(host_name) if ord(c) >= 33 and ord(c) != 127
        )

        room_id = "".join(
            c for c in str(snap.get("room", "")) if ord(c) >= 33 and ord(c) != 127
        )

        # 房间名（玩家看到的那个）可以含空格和中文，所以在这里 percent-encode
        # 之后作为附加字段发出去；第 1 个字段始终是 ASCII 的 room id（加入房间
        # 时用的就是它）。老客户端读到不认识的字段会忽略，协议依旧兼容。
        room_display = room_names.room_token(snap.get("name") or room_id)

        # 状态说的是「房间里有没有别人」，不是「世界里有没有人」。
        #
        # players 现在把开世界的房主算进去了（房主独自在游戏里 = 1），所以
        # 用它判断状态的话，任何开着的房间都会显示 busy。用配对会话数才对：
        # 只有房主一个人的房间，对想加入的人来说仍然是"空闲可进"。
        state = "busy" if snap.get("sessions") else "empty"

        writer.write(
            (
                f"ROOM {room_id} {creator} "
                f"{snap.get('players', 0)}/{snap.get('max_players', 0)} "
                f"{snap.get('idle_tunnels', 0)} "
                f"{snap.get('waiting_players', 0)} "
                f"{state}"
                f" host={current_host or '-'}"
                f" members={snap.get('members', 0)}"
                f" name={room_display}"
                # 主机位置有人认领，但那个人的 Minecraft 还没开 → 列表里显示
                # 「名字（未开服）」，而不是让人对着一个连不进去的房间发呆。
                f" host_live={1 if snap.get('host_online') else 0}\n"
            ).encode()
        )

    writer.write(b"DONE\n")
    await writer.drain()

    await close_writer(writer)


async def handle_rename(writer, client_id, username, token, new_username):
    """
    Self-service rename over the control plane.

    Request :  RENAME __mclink__ <client_id> <username> <token> <new_username>
    Reply   :  OK RENAME user=<new_username>
               ERROR BAD_USERNAME / RENAME_LIMIT / AUTH_REQUIRED /
                     AUTH_INVALID_TOKEN / AUTH_DISABLED / AUTH_UNAVAILABLE /
                     AUTH_DISABLED_SERVER / RENAME_FAILED

    The rename is rate limited (3 per 30 days) because this is the self-service
    path; the panel's rename is unlimited. It NEVER disconnects the player: the
    live room/session names are patched in place and the session tokens are
    rebound to the new name, so a client that was already logged in stays
    logged in and the new name shows up in the room list on the next refresh.
    """
    if accounts is None:
        writer.write(b"ERROR AUTH_DISABLED_SERVER\n")
        await writer.drain()
        await close_writer(writer)
        return

    ok, identity = await accounts.authorize(token or "")

    if not ok:
        log("[RENAME DENIED]", f"ip={peer_ip(writer)}", f"reason={identity}")
        writer.write(f"ERROR {identity}\n".encode())
        await writer.drain()
        await close_writer(writer)
        return

    # The token decides WHO is renaming; the name in the request is only a
    # hint, so a client cannot rename somebody else's account with its own
    # token.
    requester = identity["username"]

    if not requester:
        writer.write(b"ERROR AUTH_REQUIRED\n")
        await writer.drain()
        await close_writer(writer)
        return

    target = clean_token(new_username or "", cfg.MAX_USERNAME_LEN)

    if not target:
        writer.write(b"ERROR BAD_USERNAME\n")
        await writer.drain()
        await close_writer(writer)
        return

    log("[RENAME]", f"user={requester!r}", f"new={target!r}")

    renamed, message, info = await accounts.rename_user(
        requester, target, actor=requester, by_panel=False
    )

    if not renamed:
        code = "RENAME_FAILED"

        if "次数" in message:
            code = "RENAME_LIMIT"
        elif "占用" in message:
            code = "USERNAME_TAKEN"
        elif (
            "字符" in message
            or "空格" in message
            or "相同" in message
            or "至少" in message
            or "最长" in message
        ):
            code = "BAD_USERNAME"

        log("[RENAME FAIL]", f"user={requester!r}", f"code={code}", f"msg={message}")

        # The reason travels with the error, so the client can show the real
        # sentence ("本月的自助修改次数已用完…") instead of a bare code.
        writer.write(f"ERROR {code} {message}\n".encode())
        await writer.drain()
        await close_writer(writer)
        return

    log("[RENAME OK]", f"user={requester!r}", f"new={target!r}")

    # remaining= is additive: an older client ignores it. A player who can see
    # "还剩 2 次" does not have to discover the limit by being refused.
    budget = info or {}
    remaining = budget.get("remaining")
    unlimited = bool(budget.get("unlimited"))

    writer.write(
        (
            f"OK RENAME user={target} "
            f"remaining={'unlimited' if unlimited else (remaining if remaining is not None else 0)} "
            f"limit={budget.get('limit') or 0} "
            f"month={budget.get('month', '')}\n"
        ).encode()
    )
    await writer.drain()

    await close_writer(writer)


async def handle_member_join(reader, writer, room_name, client_id, username,
                             token, create=False, capacity=0, capabilities=()):
    """
    Enter a room and stay in it.

    Request :  JOIN   __mclink__ <client_id> <username> <token> <room-name> [max_players]
               CREATE __mclink__ <client_id> <username> <token> <room-name> [max_players]
    Reply   :  OK JOIN room=<room-id> name=<房间名,percent-encoded> owner=0/1
                         host=0/1 members=<n> max_players=<n>
                         server_max_players=<n> you=<我的显示名,percent-encoded>
               then the connection STAYS OPEN and carries:
                   PING <x>            -> PONG <x>
                   PING <token>        -> 成员回 PONG <token>（中继量 RTT 用）
                   HOST_STATE host=<> members=<> world=<> peers=<cid,...>
                              names=<cid>:<名字,percent-encoded>,...
                              rtts=<cid>:<整数毫秒>,...
                   ROOM REVOKED <原因>
                   KICK <room> <id>    -> OK KICK <id> [OWNER_LEFT_ROOM]
                                          / ERROR KICK_SELF / KICK_FORBIDDEN
                                          / KICK_NO_SUCH_MEMBER
               ERROR ROOM_NOT_FOUND / ROOM_EXISTS / ROOM_FULL / BAD_ROOM / AUTH_*

    CREATE makes the caller the room's OWNER. JOIN enters an existing room.

    `you=` / `names=` / `rtts=` 都是 ADDITIVE：老客户端按空格分词，不认识的
    token 直接忽略。`you=` 与 `names=` 里的名字是 percent-encoding 的
    （登录名可以含中文/空格）；`rtts=` 是整数毫秒，没测到的成员整条不出现。

    ============================================================================
     房间名 vs 房间 ID
    ============================================================================

    The room field the client sends is the display NAME, percent-encoded
    (room_names.room_token). The relay turns it into the room ID, which is the
    ASCII token everything else uses: `rooms` keys, the DB, the panel's admin
    API, and the HOST/PLAYER handshakes. Two people typing the same name get
    the same token, so "same name = same room" and duplicates collide exactly
    where they should (ROOM_EXISTS on CREATE).

    Both sides get told both values: room= (id) and name= (what humans read).

    The room's lifetime belongs to the OWNER (unchanged from how the relay
    always behaved), while the host POSITION is what Minecraft connections
    hang off. Keeping the two apart is what allows the owner to sit in their
    own room as an ordinary player and let somebody else host.
    """
    room = rooms.get(room_name)

    if create and (room is None or room.closed):
        room = await get_or_create_room(
            room_name,
            shown_name=room_names.display_name(room_name),
            # 同一个 Owner 重建同一个房间时，带回他上一次显式选过的连接模式
            # （`mode_reason=restored`）。见 get_or_create_room 的说明。
            owner_key=client_id,
        )
    elif create and room is not None:
        # ============================================================================
        #  已有房间，不许再"创建"一次
        # ============================================================================
        #
        # CREATE used to fall through to the existing room. That is how a
        # second person pressing 创建 on a name that was already in use ended up
        # in the SAME room -- and, when the room had no owner at that instant,
        # became its owner. The original players were left in a room whose
        # lifetime now belonged to somebody else, and when that somebody walked
        # away everybody was dropped.
        #
        # One exception, and only one: the SAME client re-creating the room it
        # already owns. That is a reconnect, not a collision.
        owner = room.owner
        same_client = bool(
            owner is not None
            and client_id
            and owner.client_id == client_id
        )

        if not same_client:
            writer.write(b"ERROR ROOM_EXISTS\n")
            await writer.drain()
            await close_writer(writer)
            return

    if room is None or room.closed:
        # A room that was just closed by an admin: say WHY, instead of the
        # "房间不存在" that sends people looking for a typo in the room name.
        reason = room_registry.closed_reason(room_name)

        if reason and not create:
            writer.write(f"ERROR ROOM_REVOKED {reason}\n".encode())
            await writer.drain()
            await close_writer(writer)
            return

        writer.write(b"ERROR ROOM_NOT_FOUND\n")
        await writer.drain()
        await close_writer(writer)
        return

    if room.conn_mode == "auto" and auto_relay.CAPABILITY not in capabilities:
        writer.write(b"ERROR AUTO_UNSUPPORTED auto_stream_v1 required\n")
        await writer.drain()
        return

    if room.closing and not create:
        writer.write(
            f"ERROR ROOM_REVOKED {room.closing_reason or '房间正在关闭'}\n".encode()
        )
        await writer.drain()
        await close_writer(writer)
        return

    # 建房是把这个名字重新用起来：清掉"刚被关闭"的便签。
    if create:
        room_registry.forget_closed(room_name)

    # 建房的人带来的容量就是这个房间的容量（服务器设置只是上限）。
    #
    # 这一步同时把它【锁死】：建房之后谁都不能再改（set_room_capacity 会
    # 拒绝），因为隧道数量现在跟着这个数字走，而 host 是可能换人的。
    if create and capacity:
        room.max_players_room = int(capacity)
        room.capacity_locked = True

    # The owner is a member like everybody else: `room.members` holds one
    # entry per person in the room, owner included. This used to allow
    # `max_players() + 1` members -- a room limited to 8 accepted 9 people,
    # which is the sort of off-by-one that only shows up as "the panel says
    # 0/8 but there are nine of us in here".
    #
    # ============================================================================
    #  本人重连（同一个 client_id + 同一个显示名）不算"新人"
    # ============================================================================
    #
    # 这件事在"连接 != 身份"（AGENTS.md 规则 6 补充）之后变得关键：控制连接
    # 断了但身份还在时，中继会把他的**座位**原地留着（见 hold_member_seat）。
    # 如果这时房间已经满员，本人重连会被 `len(room.members) >= capacity` 挡在
    # 门外 —— 明明他是回来坐自己的位子，却被判 `ERROR ROOM_FULL`，然后宽限期
    # 一到座位才释放，房间反而丢了人。所以重连（会**顶替**同一个 key 的旧条目，
    # 成员数不会增加）必须放行。
    #
    # 判据刻意和 `add_member()` 判 same_person 的口径一致（同一个 client_id，
    # 而且认得出是同一个人）：换个账号、换个名字进来，仍然受人数上限约束。
    # 游客重连时客户端发的 username 是 `-`，靠 token 认人（中继发的游客 token
    # / 还活着的游客会话名 / 同一条账号 token）。
    def _same_seat(other):
        if str(getattr(other, "client_id", "") or "") != str(client_id or ""):
            return False

        if str(getattr(other, "username", "") or "") == str(username or ""):
            return True

        if not token:
            return False

        if str(getattr(other, "guest_token", "") or "") == str(token):
            return True

        with contextlib.suppress(Exception):
            if guest_session_name(token) and (
                guest_session_name(token) == str(getattr(other, "username", "") or "")
            ):
                return True

        return str(getattr(other, "token", "") or "") == str(token)

    reconnect_same_seat = any(
        _same_seat(other) for other in room.members.values()
    )

    if (
        len(room.members) >= room.room_capacity()
        and not reconnect_same_seat
    ):
        writer.write(b"ERROR ROOM_FULL\n")
        await writer.drain()
        await close_writer(writer)
        return

    peer = safe_peer(writer)
    member_ip = peer_ip(writer)

    # ============================================================================
    #  谁才算"所有者"
    # ============================================================================
    #
    # ONLY a CREATE makes you the owner, and only when nobody owns the room yet.
    #
    # This used to be `room.owner is None` regardless of CREATE or JOIN, so
    # somebody joining a room that happened to have no owner (a room left by a
    # legacy HOST connection, or one whose owner's connection had just gone)
    # silently BECAME its owner -- and with ownership comes the room's lifetime:
    # when that person left, the room was destroyed under everybody else's feet.
    # That is exactly the reported "根管理加入别人的房间也会把他们挤掉成为房主".
    is_owner = bool(create) and room.owner is None

    # ============================================================================
    #  显示名：登录名 / 中继分配的「游客xxxxxx」
    # ============================================================================
    #
    # 用户规则：房间里显示的成员名必须是**登录用户名**；没登录/免登录的连接，
    # 服务器在握手时分配一个临时名字（`游客` + 6 位 [0-9a-z]）。
    #
    # 这个临时身份绑定在**客户端与中继的登录会话**上（不是房间，也不是设备）：
    #   * 客户端把上次拿到的临时 token（`guest=<token>`）放在 `token` 字段里
    #     回传 -> 复用同一个名字；退房 / 被踢 / 房间被关都不会让它失效；
    #   * 登录态丢了（重连后不带了）/ 换账号 / 重启 -> 没有 token，重新分配。
    # 见上面 guest session 那一段的说明。
    #
    # 分配放在这里（而不是握手解析处）是因为只有这里拿得到房间当前的成员
    # 名单，可以避开重名。client_id 也在这里再兜一次：handle_member_join 是
    # 所有成员进入房间的唯一入口，即使被别的路径直接调用也不能出现空 key。
    client_id = ensure_client_id(
        client_id, role="CREATE" if create else "JOIN",
        room=room_name, peer=peer, log_it=False,
    )

    guest_token = ""

    if is_missing_identity(username):
        remembered = guest_session_name(token)

        if remembered:
            username = remembered
            guest_token = token

    if is_missing_identity(username):
        username = ensure_username(
            username,
            role="CREATE" if create else "JOIN",
            room=room_name,
            peer=peer,
            taken=[m.username for m in room.members.values()],
        )
        # 新游客：发一条临时 token，客户端在**这次登录会话**里一直带着它。
        guest_token = open_guest_session(username)

    # V5 data/resume sockets must prove membership even when accounts are off.
    # A caller-supplied display name is not an identity credential. Give named
    # anonymous V5 members the same server-issued guest session as unnamed
    # guests, and reuse it on subsequent joins without trusting a bare CID.
    if auto_relay.CAPABILITY in capabilities and not accounts_required():
        remembered = guest_session_name(token)
        if remembered:
            username, guest_token = remembered, token
        elif token in ("", "-", None) and not guest_token:
            guest_token = open_guest_session(username)

    member = RoomMember(
        room_name,
        reader,
        writer,
        client_id=client_id,
        username=username,
        token=token,
        is_owner=is_owner,
    )

    # 这条临时 token 是"登录会话"级别的钥匙：房间进出不动它，换账号登录才
    # 销毁（见 guest session 那一段）。放在成员对象上，IDENTITY 改名时用它。
    member.guest_token = guest_token or ""
    member.capabilities = set(capabilities)
    member.public_ip = peer_ip(writer)

    # ★ 在把自己的 OK 回包写出去之前，这条连接**一条广播都不许收**（见
    # `RoomMember.ready` 的说明：先到的 HOST_STATE 会被客户端当成握手应答，
    # 直接毁掉这一次进房 / 自动重连）。
    member.ready = False

    room.add_member(member)

    log(
        "[ROOM JOIN]",
        f"room={room_name}",
        f"user={username or '-'}",
        f"client={client_id or '-'}",
        f"owner={is_owner}",
        f"members={room.member_count()}",
        f"capacity={room.room_capacity()}",
        f"peer={peer}",
    )

    # 房间成员连接的建立：谁、从哪、进了哪个房间、房间当时多大、连接模式是什么。
    # `room_name` 已归一化成 room token；`shown` 是玩家看到的名字，两个都记
    # （token 用来和协议/数据库对齐，显示名用来和用户的截图对齐）。
    event(
        "room.member.join",
        room=room_name,
        room_name=room.display_name,
        user=username or "",
        client_id=client_id,
        owner=bool(member.is_owner),
        host=bool(member.is_host),
        members=room.member_count(),
        capacity=room.room_capacity(),
        conn_mode=room_members.room_conn_mode(room),
        peer=peer,
        ip=member_ip,
        action="create" if create else "join",
    )

    # ---- 审计：建房 / 进房各记一笔（面板「房间」类操作日志）----
    #
    # 只在房间控制面（CREATE / JOIN）发生，不进 Minecraft 数据通路；一次就是
    # 一条 INSERT，频率极低。记不进去也绝不能把进房这个动作搞失败。
    if store is not None:
        with contextlib.suppress(Exception):
            await store.add_event(
                "room_created" if create else "room_joined",
                actor=username or "-",
                room_id=room_name,
                detail=(
                    f"创建房间（房间内 {room.member_count()} 人）"
                    if create
                    else f"加入房间（房间内 {room.member_count()} 人）"
                ),
                source_ip=member_ip,
            )

    # ============================================================================
    #  `host=` 与 `host_name=` 必须同源（ADDITIVE 的 host_name=）
    # ============================================================================
    #
    # 旧回包只有 `host=`，没有名字。客户端把它解成 host_claimed=True、host_name
    # 还是默认的空串 —— 这正是用户日志里那条自相矛盾的
    # `host_claimed=True, host_name=''`（界面显示成「有房主 / 主机：」）。
    # 现在两个字段从同一个 host_state_fields() 出：有主机就一定有名字。
    #
    # 老客户端不认 host_name=，按空格分词解析时直接忽略（additive 规则）。
    host_flag, host_name_token = room_members.host_state_fields(room)

    # 当前 Host 的 client_id（ADDITIVE）。`host_name=` 是显示名，可能重名；
    # 客户端要在 `peers=` 里精确挑出当前 Host 就得有一个 id。
    host_id_token = room_members.host_client_id(room)

    writer.write(
        (
            f"OK JOIN room={room_name} "
            # 房间名（给人看的那个）随回包带回去：客户端输入框里写的是什么，
            # 之后的提示、列表、日志就显示什么，而不是这串 %E6%88%91...
            f"name={room_names.room_token(room.display_name)} "
            # ================================================================
            #  ★ 用户 2026-09-30 第 3 条：`owner=` 必须取**入房之后**的权威值
            # ================================================================
            #
            # `is_owner` 是在 `add_member()` **之前**算出来的（"CREATE 且房间
            # 没有 owner"）。可是 `add_member()` 里有一条**本人重连**的路径：
            # 座位宽限期（`hold_member_seat`）内本人回来时，所有权会**交回给
            # 新连接**（`member.is_owner = True`）。回包若还用旧的那个局部变量，
            # 房主重连后拿到的是 `owner=0` —— 界面上他的身份就变成
            # 「玩家·主机」（用户实测："在房主首次释放 host 时身份从房主变成
            # 玩家"），而中继其实一直认他是房主。
            #
            # 读成员对象上的权威值：`add_member()` 跑完之后它就是事实。
            f"owner={1 if member.is_owner else 0} "
            f"host={host_flag} "
            f"host_id={host_id_token} "
            # 人数用**广播口径**（按 client_id 去重，见 room_members.roster_count）：
            # 回包里的人数必须和随后 `HOST_STATE members=` / `peers=` 能画出来的
            # 行数一致，否则用户又会看到"5 人 / 4 行"。
            f"members={room_members.roster_count(room)} "
            # 房间自己的容量和服务器上限都告诉客户端，客户端就能在建房前
            # 判断"我的设置超过了服务器上限"，而不是连上去之后卡死。
            f"max_players={room.room_capacity()} "
            f"server_max_players={max_players()} "
            # host_name= 也放在尾部（ADDITIVE）：老客户端按空格分词，不认识的
            # token 直接忽略，位置字段一个都没动。
            f"host_name={host_name_token} "
            # ================================================================
            #  `guest=` —— 中继发给游客的**临时会话 token**（ADDITIVE）
            # ================================================================
            #
            # 免登录连接拿到它之后，在**这一次登录会话**里的每条握手都把它放
            # 在 `token` 字段里回传（退房 / 被踢 / 房间被关都不算会话结束），
            # 中继就认得出"还是同一个游客"，名字不变。登录用户这里是 `-`。
            # 老客户端不认这个 token，按空格分词直接忽略。
            f"guest={guest_token or '-'} "
            # ================================================================
            #  `you=` —— 中继给【我】的显示名（ADDITIVE）
            # ================================================================
            #
            # 登录用户 = 登录名；免登录/游客 = 中继分配的「游客xxxxxx」。
            # percent-encoding，和房间名（name=）同一套规则 —— 客户端解一次
            # 就得到能直接显示的名字，不必知道它是不是游客。
            #
            # 老客户端不认这个 token，按空格分词解析时直接忽略（additive
            # 协议规则），所以放在**行尾**是安全的。
            f"you={room_names.encode_field(member.display_name)}\n"
        ).encode()
    )
    await writer.drain()

    # ============================================================================
    #  ★ OK 回包已经写出去 —— 现在这条连接才允许收到广播
    # ============================================================================
    #
    # `room.add_member()` 到这一行之间有好几个 await（事件、审计入库）。别的成员
    # 在这段时间里触发 `broadcast_member_state()` 时，会遍历到这条**还没收到
    # OK JOIN** 的连接，把 `HOST_STATE ...` 写进它的读缓冲 —— 客户端只读第一行
    # 当握手应答，读到 `HOST_STATE` 就判「服务器应答异常」并关掉连接，自动重连
    # 整条失败（用户 2026-09-29 实测）。所以广播侧按 `member.ready` 过滤，这里
    # 在回包真的发出去之后才把它打开（下面的广播正好给这一条送上第一份快照）。
    member.ready = True

    # Tell the others the roster changed.
    await broadcast_member_state(room)

    try:
        await member_control_loop(room, member)

    except asyncio.CancelledError:
        raise

    except Exception as e:  # noqa: BLE001 - one lost member is not fatal
        if not member.closed:
            log(
                "[ROOM MEMBER ERROR]",
                f"room={room_name}",
                f"user={username or '-'}",
                f"error={e}",
            )

    finally:
        # ====================================================================
        #  成员离开 → 立刻释放主机位 + 广播（LEAVE / 掉线 / 踢 / 超时，全走这里）
        # ====================================================================
        #
        # 四条移除路径（明确 LEAVE、socket 断开、被踢、看门狗超时）最后都会
        # 落到这个 finally，所以"释放 + 广播"只需要在这里做一次 —— 用户在
        # 界面上的「认领主机」按钮就靠这条 HOST_STATE 刷新。以前只有 leave/drop
        # 顺手广播，被踢/超时时 host 位清没清、别人收不收得到，全看运气。
        #
        # 释放必须发生在 remove_member() **之前**：那之后成员已经不在
        # room.members 里，权威判定（见 room_members.take_host_from）就认不出
        # "他是不是主机"了。
        removal_reason = getattr(member, "removal_reason", "") or "drop"

        # 房间已经销毁时，"drop" 的真实原因是房间没了（PROTOCOL §9 的
        # `ROOM_CLOSED`），重连判定据此区分「房间被关」和「我被踢了」。
        if removal_reason == "drop" and getattr(room, "closed", False):
            removal_reason = "room_closed"

        # ====================================================================
        #  连接断了 != 离开房间（用户 2026-09-29：连接和身份是两回事）
        # ====================================================================
        #
        # 控制连接**断了**，但身份凭证（账号 token / 游客会话 token）仍然
        # 认得出这个人 -> 这不是"离开房间"：座位（含**主机位**与所有权）原地
        # 留住，只断游戏连接、广播 `conn=` 变 idle（UI：`xxx（未开服）`），
        # 等他自己重连回来（宽限期 MEMBER_SEAT_GRACE）。
        #
        # 只有身份**结束**才走下面那条唯一的释放路径：
        #   * 主动 LEAVE / 被踢 / 看门狗真正超时清退；
        #   * token 失效 / 房间被销毁 / 没有任何 token 能证明他是同一个人。
        # 注意 `room.closing`：管理员强制关房（`revoke_room`）会先置它、
        # 再逐个断开成员，最后才 `room.close()`。那段时间里 `room.closed`
        # 还是假 —— 但"房间正在被销毁"本身就是身份结束，绝不能再给座位宽限
        # （否则 7 个成员各挂 45 秒，面板那个 POST 直接超时）。
        if (
            removal_reason == "drop"
            and not getattr(room, "closed", False)
            and not getattr(room, "closing", False)
        ):
            if await room_members.member_identity_alive(member):
                handed_over = await room_members.hold_member_seat(room, member)

                if handed_over:
                    # 本人已经在宽限期内重连，座位归新连接了 —— 这里必须
                    # 立刻收工，绝不能释放主机位 / 移除成员 / 广播成"没有 Host"。
                    room_registry.note_removal(
                        client_id, room_name, "reconnected"
                    )
                    return

                # 宽限期结束。两种收场：
                #   * 真的没人回来 -> `timeout`（超时清退）；
                #   * 宽限期内被人**踢了 / 显式移除** -> 用那个原因（kick），
                #     绝不能改写成 timeout：重连探测（SESSION）要靠它区分
                #     「被踢（不自动重连）」和「闪断（自动重连）」。
                latest = str(getattr(member, "removal_reason", "") or "")

                removal_reason = (
                    latest if latest not in ("", "drop") else "timeout"
                )

        # 记一笔"这个人为什么不在房间里了"，给重连探测（SESSION）用。
        with contextlib.suppress(Exception):
            room_registry.note_removal(client_id, room_name, removal_reason)

        released_host = room.take_host_from(member)

        # 老式 `HOST <room>` 开服控制连接如果属于这个人，一并释放（v3 客户端的
        # 成员连接和开服控制连接用同一个 client_id）。只释放自己那条：按
        # client_id / username 判定归属，绝不动别人的开服连接。
        legacy_writer = await room.release_legacy_host_for(member)

        if released_host or legacy_writer is not None:
            # **唯一一行**可核对的释放日志（leave|drop|kick|timeout）。
            room_members.log_host_release(room, member, removal_reason)

        was_owner = room.remove_member(member)

        log(
            "[ROOM LEAVE]",
            f"room={room_name}",
            f"user={username or '-'}",
            f"owner={was_owner}",
            f"members={room.member_count()}",
        )

        # ---- 审计：退房记一笔（面板「房间」类操作日志）----
        #
        # source_ip 用的是进房时早早抓好的 member_ip：走到这里 member 的连接
        # 可能已经断了，再向 writer 要 peername 不一定拿得到。房主离开时房间
        # 会在下面一并关闭，所以把"房主离开，房间关闭"如实补进说明。
        if store is not None:
            with contextlib.suppress(Exception):
                leave_detail = f"退出房间（剩余 {room.member_count()} 人）"

                if was_owner:
                    leave_detail += "，房主离开，房间关闭"

                await store.add_event(
                    "room_left",
                    actor=username or "-",
                    room_id=room_name,
                    detail=leave_detail,
                    source_ip=member_ip,
                )

        if was_owner:
            # The owner left, so the room goes with them. This is the rule the
            # relay always had; only the host position is new.
            log("[ROOM CLOSE]", f"room={room_name}", "reason=owner left")

            await room.close(reason="owner left")
            await retire_room(room_name, room)
        elif (released_host or legacy_writer is not None) and not room.host_claimed:
            # ================================================================
            #  host 真的离开房间：游戏连接全断 + 房间保留（用户 2026-09-27 口径 A′）
            # ================================================================
            #
            # 用户明确保留现行 host 方案：**没有"host 继承"**，但也**不销毁
            # 房间**。host 一走（主动退房 / **被踢** / 掉线被清退 / 看门狗超时，
            # 四条路都汇到这个 finally）：
            #   1. 主机位已经清空（上面）+ 唯一一行 [HOST RELEASE]；
            #   2. 房间里所有**游戏连接**（PLAYER / HOST_TUNNEL / 配对会话）
            #      全部断开，每个都拿到可识别的原因；
            #   3. 房间和成员控制连接**保留**：别人可以重新认领
            #      （kick_test 里"host 掉线后 B 能认领"仍然成立）；
            #   4. 无 host 期间新的 PLAYER / HOST_TUNNEL 一律被拒（ERROR NO_HOST）。
            #
            # ★ 用户 2026-09-30（最后一条）：这里的原因码必须是 **`HOST_RELEASED`**
            #   而不是 `HOST_LEFT`。区别是"主机位还在不在"：
            #
            #     * `HOST_LEFT`     = **闪断**（`hold_member_seat`：身份/主机位
            #       原地留着等他回来）-> 客户端"能复用就复用，否则自动重连"；
            #     * `HOST_RELEASED` = 主机位**已经空出来了**（真离开 / 被踢 /
            #       超时清退）-> 客户端断开 + 重算 + 提示「host 已断开连接，
            #       host 位置暂无人认领」，**不自动重连**。
            #
            #   以前这里发的是 `HOST_LEFT`：于是**踢掉 host 之后房主自己**
            #   （踢人的那个人，他不是 host）也被当成"闪断"，客户端自动重连，
            #   界面一直停在连接态、按钮也不重算 —— 用户实测到的那一个点。
            #
            # 特例（上面 was_owner 分支）：host 同时是 owner -> 房主离开 =
            # 房间关闭，这条既有规则不变。
            await room_members.disconnect_game_connections(
                room,
                reason="host 已断开连接，host 位置暂无人认领",
                code=room_members.HOST_RELEASED_CODE,
            )

            log(
                "[HOST LEFT]",
                f"room={room_name}",
                "games_disconnected=1",
                "room_kept=1",
            )

            await broadcast_member_state(room)
        else:
            # 走的不是主机（或者主机位还在别人手上）：会话该拆的拆，房间留着。
            #
            # 为什么还要看 host_writer：光凭 `host_member is None` 分不出
            # "走掉的是主机" 和 "老式 HOST 控制的房间里走掉了一个普通成员" ——
            # 后者 host_member 本来就是 None，但房主还开着服。以前
            # drop_all_sessions() 是个静默空操作，所以看不出来；它修好之后
            # （见 room_members.drop_all_sessions），这里若不加上 host_writer
            # 这一半，会把一个正常开着的房间里的所有玩家踢掉。
            if room.host_member is None and room.host_writer is None:
                await drop_all_sessions(room, reason="host left")

            await broadcast_member_state(room)


async def handle_logout(writer, client_id, username, token):
    """
    Release a session token.

    Request :  LOGOUT __mclink__ <client_id> <username> <token>
    Reply   :  OK LOGOUT
               ERROR AUTH_REQUIRED / AUTH_INVALID_TOKEN / AUTH_DISABLED_SERVER

    WHY THIS EXISTS: a token stays valid for its full TTL (12 hours), so
    without this, somebody who logged out and went to bed still showed as
    online the next morning. The panel's 在线账号 is now built on live
    connections (see online_accounts_now), and this also drops the account's
    presence channel so the effect is immediate rather than relying on the
    client closing its socket.

    Idempotent on purpose: logging out twice, or logging out with a token that
    already expired, is reported as success. The caller is leaving either way
    and there is nothing useful to tell it.

    游客的临时会话 token 也走这条：账号退出 = 该游客身份销毁（房间进出**不**
    走这里）。没有账号体系的中继以前会回 AUTH_DISABLED_SERVER，于是游客的
    登录态永远清不掉 —— 现在先认临时 token。
    """
    if release_guest_session(token, reason="logout"):
        log("[LOGOUT]", f"user={username or '-'!r}", "guest session released")
        await drop_presence(username)
        writer.write(b"OK LOGOUT\n")
        await writer.drain()
        await close_writer(writer)
        return

    if accounts is None or tokens is None:
        writer.write(b"ERROR AUTH_DISABLED_SERVER\n")
        await writer.drain()
        await close_writer(writer)
        return

    if not token:
        writer.write(b"ERROR AUTH_REQUIRED\n")
        await writer.drain()
        await close_writer(writer)
        return

    record = await tokens.validate(token, touch=False)

    if record is None:
        # Already gone: still a success from the client's point of view.
        log("[LOGOUT]", f"user={username or '-'!r}", "token already invalid")
        await drop_presence(username)
        writer.write(b"OK LOGOUT\n")
        await writer.drain()
        await close_writer(writer)
        return

    await tokens.revoke(token)

    log("[LOGOUT]", f"user={record.get('username', '')!r}")

    await drop_presence(record.get("username", "") or username)

    with contextlib.suppress(Exception):
        await store.add_event(
            "logout",
            actor=record.get("username", "") or (username or ""),
            detail="客户端退出登录",
        )

    writer.write(b"OK LOGOUT\n")
    await writer.drain()
    await close_writer(writer)


# ============================================================
# SESSION —— "重连之前先问一句：我的登录态/房间身份还在吗？"
# ============================================================
#
# 用户 2026-09-29（设计基准 §C / AGENTS.md 规则 6 补充）：
#
#     连接断了 != 离开房间。重连之前必须先判断
#       ① token 是否仍有效 / 是否已被踢；
#       ② 我是不是还在房间里（房间身份是否仍在）。
#
#     * 闪断（token 仍有效、房间身份仍在）-> **自动重连**；
#     * 被清退 / token 失效 / 房间被关 -> 不算闪断：需要重新登录 / 重新加入。
#
# `KICKED` / `ERROR AUTH_INVALID_TOKEN` / `ROOM REVOKED` 都是**被动推送**，
# 只在客户端当时还连着的时候才收得到；socket 已经断了的时候客户端什么都
# 收不到，只能靠猜。所以这里补一条**主动、只读**的探测（ADDITIVE）：
#
#     SESSION __mclink__ <client_id> <username> <token>
#         ◄── OK SESSION valid=<0|1> in_room=<0|1> token=<account|guest|none>
#                        room=<room-token|-> name=<percent-encoded|->
#                        reason=<CODE> auto_reconnect=<0|1> relogin=<0|1>
#
# `reason` 取值（精确口径，PROTOCOL §8.10）：
#   STILL_IN_ROOM   还认得他这条成员连接（真·闪断：位子/身份都还在）
#   DROPPED         连接掉了，但身份/ token 没问题（可自动重连）
#   NOT_IN_ROOM     服务端没有任何记录（例如中继刚重启过）—— 可以自动重连
#   KICKED          被踢出房间      -> 不自动重连，按退出处理
#   LEFT            本人主动退房    -> 不自动重连
#   TIMEOUT         服务端超时清退  -> 不自动重连
#   ROOM_CLOSED     房间已销毁（房主离开 / 管理员关闭）-> 不自动重连
#   AUTH_*          token 已失效    -> relogin=1，必须重新登录
#
# 这条探测**只读**：不建房间、不改任何状态、不碰数据通路。老中继不认识
# `SESSION`，回 `ERROR UNKNOWN_ROLE`，客户端据此退回旧行为（additive 规则）。
async def handle_session_probe(writer, client_id, username, token):
    token = str(token or "").strip()
    missing = is_missing_identity(token)

    # ---- ① token 还有效吗（登录态 / 游客会话）----
    guest_name = ""

    if not missing:
        with contextlib.suppress(Exception):
            guest_name = guest_session_name(token) or ""

    if missing:
        token_kind = "none"
    elif token.startswith(GUEST_SESSION_PREFIX):
        # 游客 token 只可能是游客会话：会话被 LOGOUT / 过期之后就是**失效**，
        # 绝不能拿它去账号校验（免登录服务器上那会返回"合法"）。
        token_kind = "guest"
    else:
        token_kind = "account"

    valid = 0
    auth_code = ""

    if missing:
        # 免登录服务器里"没有 token"本来就是合法状态；要求登录的服务器上
        # 就是"你得先登录"。
        if accounts_required():
            auth_code = "AUTH_REQUIRED"
        else:
            valid = 1
    elif token_kind == "guest":
        if guest_name:
            valid = 1
        else:
            auth_code = "AUTH_INVALID_TOKEN"
    elif accounts is None:
        valid = 1
    else:
        ok, identity = await accounts.authorize(token)

        if ok:
            valid = 1
        else:
            auth_code = identity if isinstance(identity, str) else "AUTH_INVALID_TOKEN"

    # ---- ② 房间身份还在吗 ----
    room, member = await room_registry.find_member(client_id)

    in_room = 1 if member is not None else 0
    room_token = room.name if room is not None else ""
    noted_room, noted = ("", "")

    if not in_room:
        with contextlib.suppress(Exception):
            noted_room, noted = room_registry.recall_removal(client_id)

        room_token = room_token or noted_room

    if in_room:
        reason = "STILL_IN_ROOM"
    elif not valid:
        reason = auth_code or "AUTH_INVALID_TOKEN"
    elif noted == "kick":
        reason = "KICKED"
    elif noted == "leave":
        reason = "LEFT"
    elif noted == "timeout":
        reason = "TIMEOUT"
    elif noted == "room_closed":
        reason = "ROOM_CLOSED"
    elif noted == "drop":
        reason = "DROPPED"
    elif room_registry.closed_reason(room_token):
        reason = "ROOM_CLOSED"
    else:
        reason = "NOT_IN_ROOM"

    auto_reconnect = 1 if (
        valid and reason in ("STILL_IN_ROOM", "DROPPED", "NOT_IN_ROOM")
    ) else 0
    relogin = 0 if valid else 1

    display = room_names.display_name(room_token) if room_token else ""
    name_token = room_names.encode_field(display) if display else "-"

    log(
        "[SESSION PROBE]",
        f"client={client_id or '-'}",
        f"user={username or '-'}",
        f"valid={valid}",
        f"in_room={in_room}",
        f"reason={reason}",
    )

    if store is not None:
        with contextlib.suppress(Exception):
            await store.add_event(
                "session_probe",
                actor=username or "-",
                room_id=room_token or "-",
                detail=f"重连判定探测：valid={valid} in_room={in_room} reason={reason}",
                source_ip=peer_ip(writer),
            )

    writer.write(
        (
            f"OK SESSION valid={valid} in_room={in_room} token={token_kind} "
            f"room={room_token or '-'} name={name_token} reason={reason} "
            f"auto_reconnect={auto_reconnect} relogin={relogin}\n"
        ).encode()
    )
    await writer.drain()
    await close_writer(writer)


async def drop_presence(username):
    """
    Close this account's presence channel.

    Called when somebody logs out (or is kicked): the connection is how the
    panel knows they are online, so leaving it open would keep showing them
    as 在线 until the client happened to close its socket.
    """
    username = (username or "").strip()

    if not username:
        return

    for key, record in list(room_registry.presences.items()):
        if (record.get("username") or "").strip() != username:
            continue

        room_registry.presences.pop(key, None)

        writer = record.get("writer")

        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()


async def handle_room_delete(writer, room_name, client_id, username, token):
    """
    Delete one of MY OWN rooms.

    Request :  DELETE __mclink__ <client_id> <username> <token> <room>
               (legacy order also accepted: DELETE <room> <client_id> <username> <token>)
    Reply   :  OK DELETE room=<room>
               ERROR NOT_HOST / DELETE_ROOM_BUSY / AUTH_REQUIRED /
                     AUTH_INVALID_TOKEN / AUTH_DISABLED / ROOM_NOT_FOUND

    Rules:
      * only the room OWNER may delete it (2026-09-24: 主机不算 —— host 只是
        房间里一个任何人都能认领的位置，让 host 能删房等于谁都能炸别人的房)
      * a room that still has players CANNOT be deleted from a client
        (the server enforces this, the UI merely disables the button)
      * the web panel may force-delete any room in any state

    `room_name` may be either the room ID (token) or the plain room name —
    room_names.room_token() normalises both to the same key, so an old client
    that sends the bare name still finds its room.
    """
    room_name = room_names.room_token(room_name)
    room = rooms.get(room_name)

    if room is None:
        writer.write(b"ERROR ROOM_NOT_FOUND\n")
        await writer.drain()
        await close_writer(writer)
        return

    if accounts is not None and accounts_required():
        ok, identity = await accounts.authorize(token)

        log(
            "[DELETE AUTH]",
            f"room={room_name!r}",
            f"ok={ok}",
            f"identity={identity!r}",
            f"host_username={room.host_username!r}",
            f"host_id={room.host_id!r}",
            f"param_client={client_id!r}",
            f"param_user={username!r}",
        )

        if not ok:
            writer.write(f"ERROR {identity}\n".encode())
            await writer.drain()
            await close_writer(writer)
            return

        requester = identity["username"]

        # 只允许【房间所有者】删房 —— 用户 2026-09-24 明确要求。
        #
        # 改之前这里是"所有者 **或** 主机"。问题在于 host 只是房间里的一个
        # **位置**，任何人都能认领（用户原话："我们房主是创建房间的人，不是
        # host，任何人都能认领 host"）。于是任何一个玩家认领了 host，就能把
        # 别人的房间直接删掉。
        #
        # 所有者检查本身是必须的（CREATE 出来的房间一开始还没有 host，
        # 旧的"只有 host 能删"规则会让创建者删不掉自己的房间）。
        #
        # 空名字必须 DENY：否则一个没记下所有者名字的房间会变成谁都能删。
        owner_name = room_owner_username(room)

        # **规则：判【所有者身份】，不判位置。**
        #
        #   * host 恰好就是所有者 → requester == owner_name → 允许
        #     （所以不会出现"host 同时是房主，却没人能删"）；
        #   * host 不是所有者     → 拒绝（host 只是任何人都能认领的位置）。
        #
        # **只认所有者，没有兜底**（用户 2026-09-25："既然加门了就删掉那些
        # 兜底吧，免得后面一堆 BUG"）。
        #
        # 以前这里在"房间没有所有者"时退回"主机可删"。版本门上了以后，能连上
        # 来的客户端一定走过 CREATE/JOIN（房间必然有所有者），无主房间在生产
        # 环境里已经不可能出现 —— 那个兜底只剩下"多一条绕过所有者的路"。
        # 现在无主房间只能由面板强制关房（管理接口走的是 `force_close`，
        # 不经过这里）。
        allowed = bool(owner_name) and requester == owner_name

        if not allowed:
            log(
                "[DELETE DENIED]",
                f"room={room_name}",
                f"requester={requester}",
                f"owner={owner_name or '(none)'}",
                f"host={room.host_username or '(none)'}",
            )
            writer.write(b"ERROR NOT_HOST\n")
            await writer.drain()
            await close_writer(writer)
            return

    else:
        # 关掉账号时的兜底：用 client_id 证明"这是我的房间"。
        # 这里同样**只认所有者**（`room.owner.client_id`），不认 host ——
        # 否则关掉账号的服务器上，任何人认领 host 就能删别人的房间。
        owner = getattr(room, "owner", None)
        owner_id = str(getattr(owner, "client_id", "") or "")

        # 同样**只认所有者**，没有退回主机的兜底（见上面的说明）。
        allowed = bool(owner_id) and client_id == owner_id

        if not allowed:
            log(
                "[DELETE DENIED]",
                f"room={room_name}",
                f"param_client={client_id!r}",
                f"owner_id={owner_id or '(none)'}",
                "reason=not the room owner (auth disabled)",
            )
            writer.write(b"ERROR NOT_HOST\n")
            await writer.drain()
            await close_writer(writer)
            return

    if room.active_count() > 0:
        log(
            "[DELETE DENIED]",
            f"room={room_name}",
            f"players={room.active_count()}",
            "reason=room busy",
        )
        writer.write(b"ERROR DELETE_ROOM_BUSY\n")
        await writer.drain()
        await close_writer(writer)
        return

    log("[ROOM DELETE]", f"room={room_name}", f"by={username or client_id or '-'}")

    if store is not None:
        await store.add_event(
            "client_delete_room",
            actor=username or "",
            room_id=room_name,
            detail="房主主动删除房间",
            source_ip=peer_ip(writer),
        )

    await room.close(reason="deleted by host")
    await retire_room(room_name, room)

    writer.write(f"OK DELETE room={room_name}\n".encode())
    await writer.drain()

    await close_writer(writer)


# ============================================================
# Connection dispatch
# ============================================================

async def read_initial_line(reader):
    try:
        line = await asyncio.wait_for(
            reader.readline(),
            timeout=cfg.HANDSHAKE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return None

    if not line:
        return None

    try:
        return line.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


async def client_connected(reader, writer):
    peer = safe_peer(writer)

    enable_tcp_nodelay(writer)

    try:
        line = await read_initial_line(reader)

        if not line:
            await close_writer(writer)
            return

        role, room_name, client_id, username, token, err = parse_hello(line)

        # ================================================================
        #  版本门：客户端与服务端必须同版本，否则不给用
        # ================================================================
        #
        # 加在**唯一的握手解析点**上，所以 CREATE / JOIN / HOST / HOST_TUNNEL /
        # PLAYER 全都被这一道门覆盖（LOGIN / ROOMS / PRESENCE / LOGOUT 等
        # 只读或账号操作**不拦** —— 面板诊断脚本和"先登录再被告知要更新"
        # 都靠它们还能用）。
        #
        # `RELAY_VERSION` 为空 = 门关（本地与测试环境就是这样），
        # 线上由 deploy/server/VERSION 提供（sync-deploy.py 生成）。
        #
        # 「允许不同版本客户端连接」的面板开关（**开发调试**，默认关闭）在这里
        # 生效：门开着的时候，版本不一致的连接被**放行**，并留一条
        # `[VERSION BYPASS]`。默认关闭时行为与以前**逐字一致**。
        #
        # 这不是兼容老版本的路径：见 settings_service 里这个设置项的注释。
        if cfg.RELAY_VERSION and (
            role in ("CREATE", "JOIN", "HOST", "HOST_TUNNEL", "PLAYER", "AUTO_RESUME", "AUTO_NETWORK")
        ):
            _ver = ""

            for _tok in line.split():
                if _tok.startswith("ver="):
                    _ver = _tok[4:]
                    break

            if _ver != cfg.RELAY_VERSION:
                if allow_version_mismatch():
                    log(
                        "[VERSION BYPASS]",
                        f"room={room_name or '-'}",
                        f"role={role}",
                        f"client={_ver or '-'}",
                        f"server={cfg.RELAY_VERSION}",
                        "开发开关=开，已放行（可能产生未知 BUG）",
                    )
                    event(
                        "relay.version.bypass",
                        role=role,
                        room=room_name,
                        peer=peer,
                        client_version=_ver or "",
                        server_version=cfg.RELAY_VERSION,
                    )
                else:
                    log(
                        "[VERSION REJECT]",
                        f"room={room_name or '-'}",
                        f"role={role}",
                        f"client={_ver or '-'}",
                        f"server={cfg.RELAY_VERSION}",
                    )

                    with contextlib.suppress(Exception):
                        writer.write(
                            f"ERROR VERSION_MISMATCH server={cfg.RELAY_VERSION} "
                            f"client={_ver or '-'}\n".encode()
                        )
                        await writer.drain()

                    await close_writer(writer)
                    return

        if err is not None:
            event(
                "relay.handshake.reject",
                peer=peer,
                code=err,
                line=_redact_handshake(line),
            )
            writer.write(f"ERROR {err}\n".encode())
            await writer.drain()
            await close_writer(writer)
            return

        log(
            "[CONNECT]",
            f"role={role}",
            f"room={room_name}",
            f"peer={peer}",
        )

        # ---- 结构化：连接 + 握手原文 ----
        #
        # The handshake line is recorded VERBATIM (minus the token, which is a
        # credential and has no business sitting in a log that gets emailed
        # around). This is the audit trail: "what exactly did the client say"
        # has to be answerable from the log alone, because a re-implementation
        # of the client is the whole point of the protocol being documented.
        #
        # `token_present` records the FACT that a token was offered without
        # recording the token.
        event(
            "relay.connect",
            role=role,
            room=room_name,
            peer=peer,
            client_id=client_id,
            user=username,
            token_present=bool(token),
            line_fields=len(line.split()),
        )
        raw("HELLO", _redact_handshake(line))

        # ----------------------------------------------------
        # V2: LOGIN / ROOMS / DELETE
        # ----------------------------------------------------
        # These only trigger when the sentinel room name is used, so they can
        # never collide with a real room. They are parsed directly (not via
        # parse_hello) because they carry a password or token in the fields
        # that normally hold client_id / username.

        if room_name == SENTINEL:
            parts = line.split()

            if role == "LOGIN":
                # LOGIN __mclink__ <username> <password> [client_id]
                login_user = parts[2] if len(parts) >= 3 else ""
                login_password = parts[3] if len(parts) >= 4 else ""
                login_client = parts[4] if len(parts) >= 5 else ""

                await handle_login(
                    reader,
                    writer,
                    clean_token(login_user, cfg.MAX_USERNAME_LEN) or "",
                    login_password[:128],
                    clean_token(login_client, 64) or "",
                )

            elif role == "ROOMS":
                # ROOMS __mclink__ <client_id> <username>
                await handle_rooms_query(writer, room_name)

            elif role == "DELETE":
                # Canonical sentinel form (what the v2 client sends, 6 fields):
                #   DELETE __mclink__ <client_id> <username> <token> <room>
                # Legacy form (old clients, 5 fields):
                #   DELETE <room> <client_id> <username> <token>
                #
                # 用【字段个数】区分这两种排布，而不是"哪个字段看起来像房间号"：
                # 房间名现在会被 percent-encode（中文房间名很长），拿长度/合法性
                # 去猜会在长名字上猜错，进而删错房间。
                if len(parts) >= 6:
                    target_room = parts[5]
                    del_client = clean_token(parts[2], 64)
                    del_user = clean_token(parts[3], cfg.MAX_USERNAME_LEN)
                    del_token = clean_token(parts[4], 128)
                else:
                    target_room = parts[1] if len(parts) > 1 else ""
                    del_client = clean_token(
                        parts[2] if len(parts) >= 3 else "", 64
                    )
                    del_user = clean_token(
                        parts[3] if len(parts) >= 4 else "", cfg.MAX_USERNAME_LEN
                    )
                    del_token = clean_token(
                        parts[4] if len(parts) >= 5 else "", 128
                    )

                # 房间标识统一转成 room token（老客户端发裸名字也能对上）。
                target_room = clean_token(
                    room_names.room_token(target_room or ""),
                    cfg.MAX_ROOM_TOKEN_LEN,
                )

                if cfg.VERBOSE:
                    log(
                        "[DELETE]",
                        f"room={target_room!r}",
                        f"client={del_client!r}",
                        f"user={del_user!r}",
                        f"token_len={len(del_token or '')}",
                    )

                if target_room is None:
                    writer.write(b"ERROR BAD_HANDSHAKE\n")
                    await writer.drain()
                    await close_writer(writer)
                else:
                    await handle_room_delete(
                        writer,
                        target_room,
                        del_client or "",
                        del_user or "",
                        del_token or "",
                    )

            elif role == "RENAME":
                # RENAME __mclink__ <client_id> <username> <token> <new_username>
                rename_client = clean_token(
                    parts[2] if len(parts) >= 3 else "", 64
                )
                rename_user = clean_token(
                    parts[3] if len(parts) >= 4 else "", cfg.MAX_USERNAME_LEN
                )
                rename_token = clean_token(
                    parts[4] if len(parts) >= 5 else "", 128
                )
                rename_target = clean_token(
                    parts[5] if len(parts) >= 6 else "", cfg.MAX_USERNAME_LEN
                )

                if cfg.VERBOSE:
                    log(
                        "[RENAME]",
                        f"client={rename_client!r}",
                        f"user={rename_user!r}",
                        f"token_len={len(rename_token or '')}",
                        f"new={rename_target!r}",
                    )

                await handle_rename(
                    writer,
                    rename_client or "",
                    rename_user or "",
                    rename_token or "",
                    rename_target or "",
                )

            elif role in ("JOIN", "CREATE"):
                # JOIN   __mclink__ <client_id> <username> <token> <room> [max_players]
                # CREATE __mclink__ <client_id> <username> <token> <room> [max_players]
                #
                # CREATE makes you the room's OWNER, and the room dies with
                # your connection. JOIN enters an existing room as a member.
                # Either way the connection STAYS OPEN for as long as you are
                # in the room, which is what makes "who is here" and "who holds
                # the host position" knowable at all.
                #
                # The trailing max_players is ADDITIVE and optional: an older
                # client does not send it (room follows the server setting), and
                # an older relay ignores it. It is the room's OWN capacity --
                # the operator's setting is the ceiling, the player's setting is
                # what the room actually uses.
                member_client = clean_token(
                    parts[2] if len(parts) >= 3 else "", 64
                )
                member_user = clean_token(
                    parts[3] if len(parts) >= 4 else "", cfg.MAX_USERNAME_LEN
                )
                member_token = clean_token(
                    parts[4] if len(parts) >= 5 else "", 128
                )
                member_room = clean_token(
                    parts[5] if len(parts) >= 6 else "", cfg.MAX_ROOM_TOKEN_LEN
                )

                member_capacity = 0

                if len(parts) >= 7:
                    with contextlib.suppress(ValueError):
                        member_capacity = int(parts[6])

                # ============================================================================
                #  匿名的 client_id 必须各不相同
                # ============================================================================
                #
                # 客户端上报的 id 是身份的一部分（"同一个人重连"靠它认）。
                # 但没登录的时候客户端可能发空值或 "-"（协议里的占位符），
                # 那样**所有匿名客户端都会用同一个 key** —— 后进来的会把先进来的
                # 顶掉，房主、所有权都跟着乱。
                #
                # 所以 relay 自己兜一个唯一值。这不改变任何有账号的场景：
                # 那时身份由账号决定（见 room_members.add_member）。
                member_client = ensure_client_id(
                    member_client, role=role, room=member_room, peer=peer
                )

                if accounts_required():
                    ok, identity = await accounts.authorize(
                        member_token or ""
                    )

                    if not ok:
                        writer.write(f"ERROR {identity}\n".encode())
                        await writer.drain()
                        await close_writer(writer)
                        return

                    member_user = identity["username"] or member_user

                if member_room is None:
                    writer.write(b"ERROR BAD_ROOM\n")
                    await writer.drain()
                    await close_writer(writer)
                    return

                # 未登录 / 免登录的显示名（`游客xxxxxx`）在 handle_member_join
                # 里分配：那里才拿得到房间当前的成员名单，可以避开重名。
                await handle_member_join(
                    reader,
                    writer,
                    member_room,
                    member_client or "",
                    member_user or "",
                    member_token or "",
                    create=(role == "CREATE"),
                    capacity=member_capacity,
                    capabilities=set(auto_relay.extras(line).get("caps", "").split(",")),
                )

            elif role == "PRESENCE":
                # PRESENCE __mclink__ <client_id> <username> <token>
                #
                # "My client is open and logged in." Held open, answers PING,
                # and disappears with the process -- see handle_presence for
                # why the panel's 在线账号 is built on this instead of on the
                # token table.
                presence_client = clean_token(
                    parts[2] if len(parts) >= 3 else "", 64
                )
                presence_user = clean_token(
                    parts[3] if len(parts) >= 4 else "", cfg.MAX_USERNAME_LEN
                )
                presence_token = clean_token(
                    parts[4] if len(parts) >= 5 else "", 128
                )

                await handle_presence(
                    reader,
                    writer,
                    presence_client or "",
                    presence_user or "",
                    presence_token or "",
                )

            elif role == "LOGOUT":
                # LOGOUT __mclink__ <client_id> <username> <token>
                logout_client = clean_token(
                    parts[2] if len(parts) >= 3 else "", 64
                )
                logout_user = clean_token(
                    parts[3] if len(parts) >= 4 else "", cfg.MAX_USERNAME_LEN
                )
                logout_token = clean_token(
                    parts[4] if len(parts) >= 5 else "", 128
                )

                await handle_logout(
                    writer,
                    logout_client or "",
                    logout_user or "",
                    logout_token or "",
                )

            elif role == "SESSION":
                # SESSION __mclink__ <client_id> <username> <token>
                #
                # 「我的登录态/房间身份还在吗」——重连前的**只读**探测
                # （ADDITIVE，PROTOCOL §8.10）。见 handle_session_probe。
                session_client = clean_token(
                    parts[2] if len(parts) >= 3 else "", 64
                )
                session_user = clean_token(
                    parts[3] if len(parts) >= 4 else "", cfg.MAX_USERNAME_LEN
                )
                session_token = clean_token(
                    parts[4] if len(parts) >= 5 else "", 128
                )

                await handle_session_probe(
                    writer,
                    session_client or "",
                    session_user or "",
                    session_token or "",
                )

            else:
                writer.write(b"ERROR UNKNOWN_ROLE\n")
                await writer.drain()
                await close_writer(writer)

            return

        # ----------------------------------------------------
        # V2: every room connection must be authenticated when required
        # ----------------------------------------------------

        if accounts_required() and role in ("HOST", "PLAYER"):
            if accounts is None:
                writer.write(b"ERROR AUTH_DISABLED_SERVER\n")
                await writer.drain()
                await close_writer(writer)
                return

            ok, identity = await accounts.authorize(token)

            if not ok:
                log(
                    "[AUTH REJECT]",
                    f"role={role}",
                    f"room={room_name}",
                    f"peer={peer}",
                    f"reason={identity}",
                )
                event(
                    "relay.auth.reject",
                    role=role,
                    room=room_name,
                    peer=peer,
                    user=username,
                    client_id=client_id,
                    code=identity,
                )
                writer.write(f"ERROR {identity}\n".encode())
                await writer.drain()
                await close_writer(writer)
                return

            # The account name is authoritative for logging, the display name
            # sent by the client stays as-is.
            username = username or identity["username"]

        # ============================================================================
        #  HOST / HOST_TUNNEL / PLAYER：也要有"这个人是谁"
        # ============================================================================
        #
        # 老式控制连接（HOST）和 Minecraft 隧道（HOST_TUNNEL / PLAYER）同样
        # 需要 client_id 与显示名：日志、房间状态、面板快照都按它们记人。
        # 客户端未登录时这两个字段都是 `-`，不补的话日志里全是 `-`、并且
        # 多个匿名连接会共用同一个空 key。
        #
        # 游客会话：客户端把 `guest=<token>` 放在 token 字段里回传时，这几条
        # 连接也要用同一个显示名（同一个人的房间成员连接 / 开服控制连接 /
        # 隧道必须叫同一个名字）。没有 token 就新分配一个临时名 —— 但这里不
        # 开新会话（回包没有地方带 token），游客会话由 CREATE/JOIN 那条路开。
        # 登录用户不受影响（identity["username"] 已经填上了真实登录名）。
        if role in ("HOST", "HOST_TUNNEL", "PLAYER"):
            client_id = ensure_client_id(
                client_id, role=role, room=room_name, peer=peer
            )

            if is_missing_identity(username):
                username = guest_session_name(token)

            if is_missing_identity(username):
                username = ensure_username(
                    username, role=role, room=room_name, peer=peer
                )

        if role == "AUTO_NETWORK":
            room = await resolve_room(room_name)
            await auto_relay.handle_network(room, reader, writer, client_id, token,
                                            auto_relay.extras(line))
            return

        if role == "AUTO_RESUME":
            room = await resolve_room(room_name)
            options = auto_relay.extras(line)
            bridge = auto_relay.find_bridge(room, options.get("sid"))
            side = options.get("side", "")
            if (not bridge or not bridge.authorized(side, client_id, token,
                    options.get("resume", ""), options.get("generation", ""))):
                writer.write(b"ERROR AUTO_RESUME_DENIED\n")
                await writer.drain()
                return
            writer.write(b"OK AUTO_RESUME\n")
            await writer.drain()
            attachment = bridge.attach(side, reader, writer)
            await attachment.done.wait()
            return

        room = await resolve_room(room_name)
        if room and room.conn_mode == "auto" and role in ("HOST", "HOST_TUNNEL", "PLAYER"):
            member = auto_relay.member_for(room, client_id)
            if (auto_relay.CAPABILITY not in auto_relay.extras(line).get("caps", "").split(",")
                    or not auto_relay.supports(member)
                    or token in ("", "-") or token not in (getattr(member, "token", ""), getattr(member, "guest_token", ""))
                    or (role in ("HOST", "HOST_TUNNEL") and room_members.host_client_id(room) != client_id)):
                writer.write(b"ERROR AUTO_UNSUPPORTED_OR_IDENTITY\n")
                await writer.drain()
                return

        if role == "HOST":
            await handle_host(
                reader, writer, room_name, client_id, username, token
            )

        elif role == "HOST_TUNNEL":
            # ---- FROZEN: first tunnel reply ----
            writer.write(b"WAIT\n")
            await writer.drain()
            # ------------------------------------
            await handle_host_tunnel(
                reader, writer, room_name, client_id, username
            )

        elif role == "PLAYER":
            await handle_player(
                reader, writer, room_name, client_id, username, token
            )

        else:
            writer.write(b"ERROR UNKNOWN_ROLE\n")
            await writer.drain()
            await close_writer(writer)

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log("[CONNECTION ERROR]", f"peer={peer}", f"error={e}")

    finally:
        await close_writer(writer)


# ============================================================
# V2 -- database mirror + admin API plumbing
# ============================================================


def _find_pair(session_id):
    for room in list(rooms.values()):
        pair = room.sessions.get(session_id)
        if pair is not None:
            return room, pair
    return None, None


def rename_live_identity(old_name, new_name):
    """
    Adopt a new username in the live rooms/sessions.

    Called by the account service after a successful rename. Without this the
    relay would keep showing the old name until the player reconnected --
    and the old implementation forced a logout, which is worse: a cosmetic
    rename must never drop somebody out of their Minecraft world.

    Pure in-memory rewrite, no socket is touched.
    """
    changed = 0

    for room in list(rooms.values()):
        try:
            if room.host_username == old_name:
                room.host_username = new_name
                changed += 1

            for pair in room.sessions.values():
                if pair.player_username == old_name:
                    pair.player_username = new_name
                    changed += 1
                if pair.player_id and pair.player_id == old_name:
                    pair.player_id = new_name

            for waiter in room.waiting_players:
                if getattr(waiter, "username", "") == old_name:
                    waiter.username = new_name
                    changed += 1

            room.mark_dirty()
        except Exception:
            continue

    if changed:
        log(
            "[RENAME]",
            f"{old_name} -> {new_name}",
            f"live_refs={changed}",
            "connections kept",
        )

    return changed


async def handle_presence(reader, writer, client_id, username, token):
    """
    "My client is open and logged in." One lightweight connection, held open.

    Request :  PRESENCE __mclink__ <client_id> <username> <token>
    Reply   :  OK PRESENCE user=<name>
               then the connection STAYS OPEN and answers PING.
               ERROR AUTH_* when the token is not usable.

    ============================================================================
     WHY THIS IS NOT "JUST READ THE TOKEN TABLE"
    ============================================================================

    Counting accounts that hold an unexpired token made the panel show people
    as 在线 for hours after they closed the client -- the token outlives the
    process, and a LOGOUT that never arrived leaves one behind. Counting only
    room/session connections over-corrected: a client that is open and logged
    in but not in a room yet looked offline.

    A connection that the client opens after login and that dies with the
    process answers both cases exactly.
    """
    if accounts is None:
        writer.write(b"ERROR AUTH_DISABLED_SERVER\n")
        await writer.drain()
        await close_writer(writer)
        return

    ok, identity = await accounts.authorize(token or "")

    if not ok:
        writer.write(f"ERROR {identity}\n".encode())
        await writer.drain()
        await close_writer(writer)
        return

    name = identity.get("username") or username or ""
    key = client_id or name or peer_ip(writer)

    room_registry.presences[key] = {
        "username": name,
        "writer": writer,
        "since": time.time(),
        "peer": peer_ip(writer),
    }

    log("[PRESENCE]", f"user={name or '-'}", f"client={key}", "online")

    writer.write(f"OK PRESENCE user={name}\n".encode())
    await writer.drain()

    try:
        while True:
            line = await reader.readline()

            if not line:
                break

            text = line.decode("utf-8", errors="replace").strip()

            if not text:
                continue

            if text.upper().startswith("PING"):
                payload = text[4:].strip()
                writer.write(
                    f"PONG {payload}\n".encode() if payload else b"PONG\n"
                )
                await writer.drain()

    except asyncio.CancelledError:
        raise

    except Exception:
        pass

    finally:
        # Only remove OUR entry: a client that reconnected under the same key
        # must not be deleted by the death of its previous connection.
        current = room_registry.presences.get(key)

        if current is not None and current.get("writer") is writer:
            room_registry.presences.pop(key, None)

        log("[PRESENCE]", f"user={name or '-'}", f"client={key}", "offline")

        await close_writer(writer)


async def online_accounts_now():
    """
    Accounts that are ACTUALLY connected right now.

    NOT "accounts holding an unexpired token". A token stays valid for hours
    after the app is closed -- and a logout that never reached us leaves one
    behind too -- so the panel listed people as 在线 who had shut their client
    down. That is the complaint: 「我人都关软件了，后台还显示在线」.

    A live connection means one of these:
      * a presence channel (the client is open and logged in -- see
        handle_presence)
      * a room member's control connection (who is in a room right now)
      * a room's host control connection
      * a paired Minecraft session (a player in the world)

    All of them are torn down by the moment the client actually goes away: a
    clean close makes the socket read return immediately, and a cable pull is
    reaped by the member watchdog.
    """
    seen = set()

    # Presence first: it is the "logged in and running" case, and it is what
    # makes closing the app take effect immediately instead of after the token
    # expires.
    for record in list(room_registry.presences.values()):
        name = (record.get("username") or "").strip()

        if name:
            seen.add(name)

    async with rooms_lock:
        room_list = list(rooms.values())

    for room in room_list:
        for member in list(room.members.values()):
            if member.closed:
                continue

            name = (member.username or "").strip()

            if name:
                seen.add(name)

        if room.host_writer is not None:
            name = (room.host_username or "").strip()

            if name:
                seen.add(name)

        for pair in list(room.sessions):
            name = (getattr(pair, "player_username", "") or "").strip()

            if name:
                seen.add(name)

    return sorted(seen)


async def admin_stats():
    """Summary for the panel dashboard."""
    async with rooms_lock:
        room_list = list(rooms.values())

    total_players = 0
    total_members = 0
    total_idle = 0
    total_bytes_to_host = 0
    total_bytes_to_player = 0

    for room in room_list:
        try:
            snap = await room.snapshot()
        except Exception:
            continue

        total_players += snap.get("players", 0)
        total_members += snap.get("members", 0)
        total_idle += snap.get("idle_tunnels", 0)
        total_bytes_to_host += snap.get("bytes_player_to_host", 0)
        total_bytes_to_player += snap.get("bytes_host_to_player", 0)

    active_users = await online_accounts_now()

    # ---- database state ----
    # The panel dashboard renders "数据库（中继上报）" straight out of this
    # reply, and it used to read `database.backend` from a key that only the
    # status FILE had -- so the card showed "-" forever while the status file
    # was perfectly fine. Same shape as the status file now.
    try:
        database_healthy = bool(await store.healthy()) if store is not None else False
    except Exception:
        database_healthy = False

    # ---- built-in STUN server ----
    # Reported so the panel can show whether clients have a local STUN
    # endpoint to use, and so a client can discover it without hardcoding a
    # port. None means the relay was started without it.
    stun_info = None

    with contextlib.suppress(Exception):
        import stun_server

        stun_info = stun_server.stun_snapshot()

    return {
        "uptime": round(time.time() - STARTED_AT, 1),
        "rooms": len(room_list),
        # 两个数字口径不同，都要报：
        #   members = 人在房间里（可能还没进世界）
        #   players = 已加入世界（含开世界的房主）
        "members": total_members,
        "players": total_players,
        "idle_tunnels": total_idle,
        # The live room capacity. Two names for the same number on purpose:
        # the panel has read `max_players_per_room` since V2, and `max_players`
        # is what a client and /stats' own status file call it. A panel that
        # shows a different number from the one the relay enforces is exactly
        # the bug this key exists to make visible.
        "max_players_per_room": max_players(),
        "max_players": max_players(),
        "bytes_player_to_host": total_bytes_to_host,
        "bytes_host_to_player": total_bytes_to_player,
        "online_accounts": active_users,
        "online_account_count": len(active_users),
        # Tokens are NOT the online list (see online_accounts_now): this is
        # only how many valid login credentials exist, which stays high for
        # hours after people close the app.
        "active_tokens": (await tokens.count()) if tokens is not None else 0,
        "database": {
            "enabled": bool(cfg.DB_ENABLED),
            "backend": store.name if store is not None else "none",
            "healthy": database_healthy,
            "degraded": bool(getattr(store, "degraded", False)),
            "errors": int(getattr(store, "errors", 0) or 0),
            "last_error": str(getattr(store, "last_error", "") or "")[:200],
        },
        "admin_api": {
            "enabled": bool(cfg.ADMIN_API_ENABLED),
            "port": cfg.ADMIN_API_PORT,
            "requests": int(getattr(admin_api, "request_count", 0) or 0),
        },
        "stun": stun_info,
        "auth_required": bool(accounts_required()),
        # What this build can do. The panel compares it against what the panel
        # needs and shows a loud banner when the relay is behind -- the whole
        # point is that "面板改了没反应" stops being a mystery.
        "relay": relay_build(),
    }


async def admin_rooms():
    """
    Full room list for the panel.

    Source of truth is the relay's own memory (that is where the TCP state
    lives). Rooms that only exist in the database -- which happens after a
    relay restart, because the in-memory registry starts empty while the
    `rooms` table still holds the previous rows -- are listed too, flagged
    with `live: false`, so the panel never shows a stale room as if it were
    real and always offers a way to delete it.
    """
    async with rooms_lock:
        room_list = list(rooms.values())

    out = []
    live_ids = set()

    for room in room_list:
        try:
            snap = await room.snapshot()
        except Exception:
            continue

        live_ids.add(snap.get("room"))

        snap["empty"] = snap.get("players", 0) == 0
        snap["host_online"] = bool(snap.get("host_online"))
        snap["live"] = True
        out.append(snap)

    # ---- database-only rows (stale after a restart) ----
    if store is not None:
        try:
            rows = await store.list_rooms()
        except Exception:
            rows = []

        for row in rows or []:
            room_id = row.get("room_id")

            if not room_id or room_id in live_ids:
                continue

            out.append(
                {
                    "room": room_id,
                    "host_online": False,
                    "host_client_id": row.get("host_client_id") or "",
                    "host_username": row.get("host_username") or "",
                    "host_ip": row.get("host_ip") or "",
                    "players": 0,
                    "max_players": int(row.get("max_players") or 0),
                    "idle_tunnels": 0,
                    "waiting_players": 0,
                    "bytes_player_to_host": int(row.get("bytes_to_host") or 0),
                    "bytes_host_to_player": int(row.get("bytes_to_player") or 0),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "sessions": [],
                    "empty": True,
                    "live": False,
                }
            )

    out.sort(key=lambda r: (r.get("live", False), r.get("players", 0)), reverse=True)

    return out


async def admin_delete_room(room_id):
    """
    Panel action: force delete a room in ANY state.

    Handles both cases:
      * a LIVE room  -> close every socket and drop it from memory
      * a STALE row   -> only present in the database (e.g. left over from a
                         relay restart); just remove the row.

    ============================================================================
     WHY THE FAILURE MESSAGE IS SO SPECIFIC
    ============================================================================

    "房间不存在（可能已经关闭）" was reported repeatedly while the room was
    visibly listed in the panel. That message is useless for diagnosis: it does
    not say WHICH name was looked up, whether memory or the database was
    searched, or what the close actually returned.

    So a failure now reports all of it, including the received room name, and
    a suspicious name (empty, whitespace, control characters, too long) is
    rejected separately -- a mangled name reaching this function looks exactly
    like a missing room from the outside.
    """
    if room_id is None:
        return False, "面板没有传来房间标识（请求参数为空）"

    room_id = str(room_id)

    # A value that could not possibly be a room: report it as such instead of
    # letting it fall through to "not found".
    if room_id == "":
        return False, "房间标识为空（面板请求里没有 room 字段）"

    if room_id != room_id.strip():
        return False, f"房间标识两端有空白字符：{room_id!r}"

    # 面板传来的是 room token（房间 ID），但为了兼容旧面板/手工调用，
    # 裸房间名也接受：归一化之后是同一把钥匙。
    token = room_names.room_token(room_id)

    if len(token) > cfg.MAX_ROOM_TOKEN_LEN:
        return False, (
            f"房间标识过长（{len(token)} > {cfg.MAX_ROOM_TOKEN_LEN}）："
            f"{room_id[:32]!r}…"
        )

    if any(ord(c) < 33 or ord(c) == 127 for c in token):
        return False, f"房间标识含非法字符：{room_id!r}"

    room = rooms.get(token)

    # 给人看的名字（面板消息里用房间名，而不是那串 %E6%88%91...）
    shown = room.display_name if room is not None else room_names.display_name(token)

    if room is None:
        # Not in memory. It may still exist as a database row.
        deleted_row = False
        row_count = None

        if store is not None:
            try:
                before = await store.list_rooms()

                if before is None:
                    before = []

                existed = any(
                    str(r.get("room_id")) == token for r in before
                )

                deleted_row = bool(await store.delete_room(token))

                if deleted_row:
                    log(
                        "[PANEL DELETE]",
                        f"room={token}",
                        "action=remove stale row",
                    )

                    await store.add_event(
                        "panel_delete_stale_room",
                        actor="panel",
                        room_id=token,
                        detail="清理数据库中残留的房间记录",
                    )

                    return True, (
                        f"已清理残留房间记录「{shown}」"
                        "（该房间不在内存里，只存在于数据库）"
                    )

                row_count = "有" if existed else "没有"
            except Exception as e:  # noqa: BLE001
                log("[PANEL DELETE ERROR]", f"room={token}", f"error={e}")
                return False, f"查询数据库失败：{e}"

        # Genuinely nowhere. Say exactly what was checked, because this is the
        # message that used to send people in circles.
        live = sorted(rooms.keys())[:10]

        return False, (
            f"中继上没有名为「{shown}」的房间。"
            f"内存里的房间：{('、'.join(live) if live else '（空）')}；"
            f"数据库中该房间的记录：{row_count or '（未检查）'}。"
            "如果面板列表上还能看到它，请刷新页面 —— "
            "很可能是中继重启前的残留显示。"
        )

    players = room.active_count()
    members = room.member_count()

    log(
        "[PANEL DELETE]",
        f"room={token}",
        f"players={players}",
        f"members={members}",
        "action=force close",
    )

    await room.close(reason="deleted by admin panel")

    # Remove the row as well. Closing a room does not always delete it: the
    # host's own cleanup may already have taken it, and a room that is still
    # empty afterwards would otherwise linger in the panel.
    row_removed = False

    if store is not None:
        try:
            row_removed = bool(await store.delete_room(token))
        except Exception:
            pass

    await retire_room(token, room)

    still_listed = token in rooms

    return True, (
        f"房间「{shown}」已强制关闭"
        f"（断开 {players} 名玩家、{members} 名成员）"
        + ("，数据库记录已清理" if row_removed else "")
        + ("；注意它仍在内存里，请再点一次" if still_listed else "")
    )


async def admin_purge_stale_rooms():
    """
    Panel action: delete EVERY database room row that has no live counterpart.

    Housekeeping for the leftover rows a relay restart leaves behind. It only
    touches the database mirror -- live rooms and connected players are
    untouched.
    """
    async with rooms_lock:
        live_ids = set(rooms.keys())

    if store is None:
        return False, "数据库未启用"

    try:
        rows = await store.list_rooms()
    except Exception as e:
        return False, f"读取房间列表失败：{e}"

    removed = []
    failed = []

    for row in rows or []:
        room_id = row.get("room_id")

        if not room_id or room_id in live_ids:
            continue

        try:
            if await store.delete_room(room_id):
                removed.append(room_id)
        except Exception:
            failed.append(room_id)

    log(
        "[PANEL PURGE]",
        f"removed={len(removed)}",
        f"failed={len(failed)}",
        f"live={len(live_ids)}",
    )

    if removed:
        await store.add_event(
            "panel_purge_stale_rooms",
            actor="panel",
            detail=f"清理 {len(removed)} 条残留记录",
        )

    if not removed and not failed:
        return True, "没有需要清理的残留记录"

    message = f"已清理 {len(removed)} 条残留记录"

    if failed:
        message += f"，{len(failed)} 条失败"

    return True, message


async def admin_kick_player(session_id):
    """Panel action: disconnect one player session."""
    room, pair = _find_pair(session_id)

    if pair is None:
        return False, "会话不存在（可能已经断开）"

    log("[PANEL KICK]", f"room={room.name}", f"session={session_id}")

    if store is not None:
        await store.add_event(
            "panel_kick_player",
            actor="panel",
            room_id=room.name,
            session_id=session_id,
            detail="被管理员踢出",
        )

    await room.remove_session(session_id, reason="kicked by admin")

    return True, f"已踢出会话 {session_id}"


async def admin_kick_user(username):
    """Panel action: revoke every token of an account and drop its rooms."""
    if tokens is None:
        return False, "服务未启用账号系统"

    revoked = await tokens.revoke_user(username)

    closed = []

    for room in list(rooms.values()):
        # 判【所有者**或**主机】而不是只看主机：房间的归属按所有者算，
        # 但"踢人"要连他正开着世界的房间一起收掉，两种身份都不能漏。
        if room_owner_username(room) == username or room.host_username == username:
            await room.close(reason="host account kicked")
            await retire_room(room.name, room)
            closed.append(room.name)

    log(
        "[PANEL KICK USER]",
        f"user={username}",
        f"tokens={len(revoked)}",
        f"rooms={closed}",
    )

    return True, f"已吊销 {len(revoked)} 个令牌，关闭房间 {len(closed)} 个"


async def admin_kick_member(room_id, client_id):
    """
    Panel action: remove ONE member from a room.

    Request : POST /rooms/<room>/kick   {"client_id": "<id>"}
    Reply   : (ok, message)

    ============================================================================
     和管理员踢单个游戏会话（admin_kick_player）不是一回事
    ============================================================================

    admin_kick_player 断的是 Minecraft 会话：人还在房间里，还可以重新进世界。
    这一个断的是**房间成员身份**：人离开房间，游戏会话一并拆掉，不留幽灵
    隧道（drop_member_sessions 走的就是房间关闭路径用的 remove_session）。

    面板的调用者已经过 require_operator() 鉴权，管理 API 本身又由 Bearer
    token 保护，所以这里**不受**成员连接上的 KICK 权限矩阵限制：管理员可以
    踢任何人，**包括所有者**。踢所有者 = 房间解散，走的是面板「强制关闭
    房间」的同一条 revoke_room()，两边行为完全一致。
    """
    raw_room = str(room_id or "").strip()

    if not raw_room:
        return False, "面板没有传来房间标识（请求参数为空）"

    token = room_names.room_token(raw_room)
    room = rooms.get(token)

    if room is None or getattr(room, "closed", False):
        return False, (
            f"房间不存在（可能已经关闭）：{room_names.display_name(token)!r}"
        )

    wanted = str(client_id or "").strip()

    if not wanted:
        return False, "没有指定要踢出的成员（client_id 为空）"

    target = None

    for member in list(room.members.values()):
        if member.client_id == wanted \
                or getattr(member, "member_key", "") == wanted:
            target = member
            break

    if target is None:
        return False, "该成员不在房间里（可能已经离开）"

    # 实时判定：target 是从 room.members 里现找的活成员，不读可能过期的对象标记。
    is_owner = room.owner is target
    shown = room.display_name

    log(
        "[PANEL KICK MEMBER]",
        f"room={token}",
        f"member={target.label}",
        f"client_id={target.client_id}",
        f"owner={is_owner}",
        f"was_host={bool(target.is_host)}",
    )

    if store is not None:
        with contextlib.suppress(Exception):
            await store.add_event(
                "panel_kick_member",
                actor="panel",
                room_id=token,
                detail=(
                    f"踢出成员 {target.label}"
                    + ("（房主，房间随之解散）" if is_owner else "")
                ),
            )

    # ---- 被踢的是所有者：房间解散，和「强制关闭房间」同一条路径 ----
    if is_owner:
        await send_kicked(
            target, requester_label="管理员", reason="owner_kicked"
        )

        report = await revoke_room(room, reason="管理员踢出房主，房间解散")

        log(
            "[PANEL KICK OWNER]",
            f"room={token}",
            f"member={target.label}",
            f"members={report.get('members', 0)}",
            f"sessions={report.get('sessions', 0)}",
        )

        return True, f"已踢出房主 {target.label}，房间「{shown}」已解散"

    # ---- 普通成员：只踢他一个，房间继续 ----
    dropped = await kick_member(
        room, target, requester_label="管理员", reason="kicked_by_admin"
    )
    await broadcast_member_state(room)

    message = f"已踢出玩家 {target.label}"

    if dropped:
        message += f"（并断开 {dropped} 个游戏会话）"

    return True, message


# ============================================================
# 服务端日志（面板「预览 / 下载」用）
# ============================================================

async def admin_logs_tail(query):
    """
    GET /logs/tail?lines=N

    面板用来预览服务端日志，以及显示"日志文件多大 / 缓冲里多少行"。
    返回原始日志行数组（新的在后）。
    """
    raw = ""

    try:
        values = query.get("lines") or []
        raw = values[0] if values else ""
    except Exception:
        raw = ""

    try:
        count = int(raw) if raw else 2000
    except Exception:
        count = 2000

    count = max(1, min(count, 20000))

    meta = relay_log.info()
    lines = relay_log.tail(count)
    buffered = int(meta.get("buffered_lines") or len(lines))

    return {
        "lines": lines,
        "returned": len(lines),
        "total": buffered,
        "file": meta.get("file", ""),
        "bytes": meta.get("bytes", 0),
        "truncated": len(lines) < buffered,
    }


async def admin_logs_clear(query):
    """
    POST /logs/clear -> 清空服务端日志（内存缓冲 + 文件 + 轮转备份）。

    ============================================================================
      为什么这个接口必须存在（用户报的现象）
    ============================================================================

    用户在面板上「手动删除日志后，列表内容也要一并清理」。以前只清数据库里
    的审计事件，中继的日志文件和内存缓冲都没动，而面板上那个「中继日志当前：
    N 行」和预览列表读的正是 /logs/tail —— 于是清空之后列表还是原来那些行。

    这里把三样一起清掉，并把"清了多少"如实回报，面板再刷新一次，
    用户看到的就是空的。

    审计：调用者（面板）随后会写一条 panel_clear_relay_log 事件，所以
    "谁清的"这件事留在审计日志里，而**不**留在被清掉的日志里——那必然会被
    自己删掉。
    """
    actor = ""

    try:
        values = query.get("actor") or []

        if values:
            actor = str(values[0])[:120]
    except Exception:
        actor = ""

    result = relay_log.clear(reason=actor or "panel")

    log(
        "[LOG CLEAR]",
        f"lines={result.get('lines', 0)}",
        f"bytes={result.get('bytes', 0)}",
        f"backups_removed={result.get('backups_removed', 0)}",
        f"actor={actor or '-'}",
    )

    return {
        "ok": True,
        "cleared": result,
        "message": f"已清空服务端日志（{result.get('lines', 0)} 行）",
    }


async def admin_logs_export(query):
    """
    GET /logs/export -> 纯文本，浏览器直接下载。

    这是用户要的「服务端日志能在 web 端导出下载到本地」。
    返回 RawResponse：由 admin_api 原样写正文，不套 JSON 外壳。
    """
    body = relay_log.export()

    return RawResponse(
        body=body,
        content_type="text/plain; charset=utf-8",
        filename=f"craftlink-relay-{time.strftime('%Y%m%d-%H%M%S')}.log",
    )


def build_admin_api():
    if not cfg.ADMIN_API_ENABLED:
        return None

    return AdminAPI(
        host=cfg.ADMIN_API_HOST,
        port=cfg.ADMIN_API_PORT,
        handlers={
            "stats": admin_stats,
            "rooms": admin_rooms,
            "delete_room": admin_delete_room,
            "purge_stale_rooms": admin_purge_stale_rooms,
            "kick_player": admin_kick_player,
            "kick_member": admin_kick_member,
            "kick_user": admin_kick_user,
            # 服务端日志：面板预览 + 下载
            "logs_tail": admin_logs_tail,
            "logs_export": admin_logs_export,
            "logs_clear": admin_logs_clear,
        },
        gatekeeper=accounts,
        store=store,
        tokens=tokens,
        settings=settings,
        enabled=cfg.ADMIN_API_ENABLED,
        token=cfg.ADMIN_API_TOKEN,
    )


async def db_sync_loop():
    """
    Mirror the live room state into the database and clean up stale rows.

    Runs on its own interval, fully guarded: a database problem can only make
    the panel show stale data, never affect a Minecraft connection.
    """
    while True:
        try:
            await asyncio.sleep(max(1, cfg.DB_SYNC_INTERVAL))

            if store is None:
                continue

            async with rooms_lock:
                room_list = list(rooms.values())

            for room in room_list:
                try:
                    snap = await room.snapshot()
                except Exception:
                    continue

                await store.upsert_room(
                    {
                        "room_id": snap["room"],
                        "host_username": snap.get("host_username"),
                        "host_client_id": snap.get("host_client_id"),
                        "host_ip": snap.get("host_ip"),
                        "players": snap.get("players", 0),
                        "max_players": snap.get("max_players", 0),
                        "idle_tunnels": snap.get("idle_tunnels", 0),
                        "waiting_players": snap.get("waiting_players", 0),
                        "bytes_to_host": snap.get("bytes_player_to_host", 0),
                        "bytes_to_player": snap.get("bytes_host_to_player", 0),
                    }
                )

            await store.purge_stale_rooms(cfg.DB_STALE_ROOM_SECONDS)

        except asyncio.CancelledError:
            raise

        except Exception as e:
            log("[DB SYNC ERROR]", f"{e}")


async def empty_room_sweeper():
    """
    Auto delete rooms that have no players and no host activity.

    The requirement is "房间无人时自动双端删除": when a host stops (or its
    connection drops) the room is already destroyed immediately by
    handle_host(). This sweeper covers the remaining case of a room that is
    alive but has been sitting with zero players for longer than
    ROOM_EMPTY_GRACE seconds.

    ============================================================================
     为什么现在【无论 grace 是多少】都会清理"彻底空的"房间
    ============================================================================

    It used to `continue` when ROOM_EMPTY_GRACE was 0, which disabled the whole
    sweeper. Together with an owner who leaves while other members are still
    connected -- the room then has NO owner, so nobody's departure triggers the
    "owner left -> close the room" rule -- that produced exactly what was
    reported: a room with nobody in it that the panel still lists, and a
    database row left behind after it finally went away ("幽灵房间").

    A room that is COMPLETELY empty (no host connection, no members, no
    sessions, no waiters) is not a judgement call: nothing can ever happen in
    it again. It is closed immediately, row included, and grace only applies to
    rooms that still hold people.
    """
    grace = max(0, cfg.ROOM_EMPTY_GRACE)

    while True:
        try:
            await asyncio.sleep(5)

            now = time.monotonic()

            for room in list(rooms.values()):
                # ---- ① 彻底空房：立刻销毁，不等 grace ----
                if (
                    room.host_writer is None
                    and room.member_count() == 0
                    and not room.sessions
                    and not room.waiting_tunnels
                    and not room.waiting_players
                    and room.owner is None
                ):
                    log(
                        "[ROOM AUTO DELETE]",
                        f"room={room.name}",
                        "reason=completely empty (no host, no members, no sessions)",
                    )

                    await _delete_room_row(room.name)
                    await room.close(reason="auto deleted (nobody inside)")
                    await retire_room(room.name, room)
                    _empty_since.pop(room.name, None)
                    continue

                # ---- ② 有人在房间里但世界里没人：按 grace 计时 ----
                if grace <= 0:
                    continue

                if room.active_count() > 0:
                    _mark_room_busy(room.name)
                    continue

                since = _empty_since.get(room.name)
                if since is None:
                    _mark_room_empty(room.name)
                    continue

                if now - since < grace:
                    continue

                log(
                    "[ROOM AUTO DELETE]",
                    f"room={room.name}",
                    f"empty_for={int(now - since)}s",
                )

                if store is not None:
                    await store.add_event(
                        "auto_delete_empty_room",
                        room_id=room.name,
                        detail="房间长时间无人，自动删除",
                    )

                await room.close(reason="auto deleted (empty)")
                await retire_room(room.name, room)

        except asyncio.CancelledError:
            raise

        except Exception as e:
            log("[SWEEPER ERROR]", f"{e}")


async def _delete_room_row(room_name):
    """
    Remove a room's database row, whatever state it is in.

    Called right before dropping a room from memory, so the panel can never
    show a room that no longer exists ("幽灵房间"). Failure is logged, never
    raised: a database problem must not stop the relay from cleaning up.
    """
    if store is None:
        return False

    try:
        return bool(await store.delete_room(room_name))
    except Exception as e:  # noqa: BLE001
        log("EVENT db.room_delete.failed error=", f"room={room_name}", f"{e}")

        return False


# ============================================================================
#  审计日志保留期：每 10 天自动清理一次
# ============================================================================
#
# The panel's log page used to grow forever. The requirement is "每 10 天自动清理
# 一次", which the relay is the right place to do: it runs all the time, while
# the panel only runs when somebody opens it.
#
# WHAT IT DOES: one DELETE, `relay_events WHERE created_at < NOW() - INTERVAL 10
# DAY`, on the same connection pool everything else uses. It touches NOTHING
# else -- no other table, no schema change, and definitely not the frozen data
# path (copy_stream / the handshake). A failure is logged and retried on the
# next pass; it can never affect a Minecraft connection.
#
# The panel has a matching fallback (panel_purge_old_events() in
# panel/lib/common.php, run when the log page is opened) so retention still
# happens on a deployment where only the panel was rebuilt. Keep the two numbers
# in step.

#: 保留多少天。
EVENTS_RETENTION_DAYS = 10

#: 多久检查一次。默认 6 小时：清理本身很便宜（走 idx_events_created 索引），
#: 但没必要每分钟跑一遍；真到期的行早几小时删掉没有任何区别。
EVENTS_RETENTION_INTERVAL = 6 * 3600


async def events_retention_loop():
    """
    Periodically drop audit events older than EVENTS_RETENTION_DAYS.

    Started once from main(); never raises (the loop catches everything, and the
    store facade swallows database errors as well).

    第一遍只等 60 秒：重 build 之后表里可能已经躺着十天前的旧行，没必要再等
    一个完整周期才清 —— 而且这样"清理到底跑没跑"重启一分钟后就能在日志里看到。
    之后每 EVENTS_RETENTION_INTERVAL（6 小时）检查一次。
    """
    first_pass = True

    while True:
        try:
            # 先等一会儿再动手：启动瞬间数据库可能还在连，而且此刻清理没有
            # 任何时间压力。
            await asyncio.sleep(
                60 if first_pass else max(60, EVENTS_RETENTION_INTERVAL)
            )

            first_pass = False

            if store is None:
                continue

            removed = await store.purge_events(EVENTS_RETENTION_DAYS)
            removed = int(removed or 0)

            if removed > 0:
                log(
                    "[EVENTS RETENTION]",
                    f"已清理超过 {EVENTS_RETENTION_DAYS} 天的审计日志："
                    f"{removed} 条",
                )

                # 记一笔，让面板的日志页看得出"自动清理跑过了"。
                with contextlib.suppress(Exception):
                    await store.add_event(
                        "events_purged",
                        actor="relay",
                        detail=f"自动清理超过 {EVENTS_RETENTION_DAYS} 天的日志："
                               f"{removed} 条",
                    )

        except asyncio.CancelledError:
            raise

        except Exception as e:  # noqa: BLE001 - 清理失败绝不能影响中继
            log("[EVENTS RETENTION ERROR]", f"{e}")


async def host_watchdog():
    """
    Reap host control connections that went silent.

    WHY THIS EXISTS
    ---------------
    A host that dies uncleanly (cable pulled, process killed, wifi switched)
    leaves a TCP connection that the kernel never reports as closed. Without a
    heartbeat the room stays "hosted" by a dead socket forever:

        host reconnects  -> ERROR ROOM_ALREADY_HAS_HOST
        its tunnels      -> ERROR NO_HOST
        panel            -> shows a room nobody can use
        DELETE           -> cannot clear it

    那个组合正是客户端日志里反复出现的东西。

    ============================================================================
      2026-09-27（设计基准 §A/§B）：这里**不许再销毁带 Owner 的房间**
    ============================================================================

    以前超时直接 `room.close()`：只要房主的开服控制连接静默，**整个房间**
    连同所有成员一起被销毁。那违反基准 §A —— 房间的寿命绑在 **Owner** 的
    成员连接上，不绑在开服控制连接上。

    正确做法是**只拆这条连接**，让 `handle_host` 唯一的清理路径跑起来：
      * 没 Owner、也没成员的老式房间（旧客户端只用 `HOST <room>` 建房）：
        清理路径自己会销毁它 —— 与以前一样；
      * 有 Owner 的房间：清掉开服连接、断开所有游戏连接（带 HOST_LEFT）、
        广播 `HOST_STATE`，房间和主机位都保留，房主回来还能重新开服。
    """
    timeout = max(10, cfg.HOST_IDLE_TIMEOUT)

    while True:
        try:
            await asyncio.sleep(max(2, cfg.HOST_WATCHDOG_INTERVAL))

            for room in list(rooms.values()):
                idle = room.host_idle_seconds()

                if room.host_writer is None or idle <= timeout:
                    continue

                log(
                    "[HOST TIMEOUT]",
                    f"room={room.name}",
                    f"silent_for={int(idle)}s",
                    "-> 判定房主开服连接掉线，拆连接（房间保留）",
                )

                if store is not None:
                    await store.add_event(
                        "host_timeout",
                        room_id=room.name,
                        detail=f"控制连接静默 {int(idle)} 秒，自动拆掉开服连接（房间保留）",
                    )

                # 关掉这条 socket -> host_control_loop 抛错 -> handle_host 的
                # finally 走**唯一**的清理路径（里面决定"房间销毁还是保留"）。
                with contextlib.suppress(Exception):
                    room.host_writer.close()

        except asyncio.CancelledError:
            raise

        except Exception as e:
            log("[WATCHDOG ERROR]", f"{e}")


# ============================================================
# Bounded database steps (startup must never hang on MySQL)
# ============================================================

class _DbTimeout:
    """Sentinel returned by `db_step()` when a database call ran out of time."""

    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<db step timed out>"


#: Returned instead of a result when `db_step()` gave up waiting.
DB_TIMEOUT = _DbTimeout()


def db_boot_timeout():
    """Seconds any single startup database step may take."""
    try:
        return max(1.0, float(getattr(cfg, "DB_BOOT_TIMEOUT", 20) or 20))
    except (TypeError, ValueError):
        return 20.0


async def db_step(coro, label, timeout=None):
    """
    Await one database operation with a hard deadline.

    THE FAILURE THIS PREVENTS (measured, not hypothetical): the relay's whole
    V2 startup ran before `asyncio.start_server()`, and one database call in
    it -- `SELECT 1` for the health probe, or an `ALTER TABLE` migration
    waiting behind a metadata lock -- had no deadline at all. The result was a
    container that never opened 19100:

        [DB] connecting to mysql://mclink@web-mysql80:3306/mclink
        <nothing, forever>

    A database problem must only ever cost the PANEL and the login system.
    The frozen TCP data path has never needed MySQL, so the relay is no longer
    allowed to wait for it without a limit.

    Returns the coroutine's value, `DB_TIMEOUT` when the deadline passed, or
    None when the call raised (SafeStore normally swallows those itself).
    """
    limit = db_boot_timeout() if timeout is None else float(timeout)

    try:
        return await asyncio.wait_for(coro, timeout=limit)

    except asyncio.TimeoutError:
        log(
            f"EVENT db.operation.timeout operation={label} "
            f"timeout_s={limit:.0f} action=give_up_and_continue"
        )
        log(
            "EVENT db.timeout effect=minecraft_relay_unaffected "
            "panels_and_accounts_unavailable "
            "hint=check_mysql_long_transaction_or_metadata_lock"
        )
        return DB_TIMEOUT

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log(
            f"EVENT db.operation.failed operation={label} action=continue "
            f"error={type(e).__name__}: {e}"
        )
        return None


async def setup_services():
    """
    Initialise the V2 services before the listener starts.

    Returns True when at least the in-memory fallback is usable. The relay
    never refuses to start because of a database problem.

    ------------------------------------------------------------------
     为什么每一步都要有超时（2026-09-15 的现场故障）
    ------------------------------------------------------------------
    这一整个函数跑在监听 19100 **之前**。所以数据库只要"慢"，中继就
    "还没开始监听" —— 容器 unhealthy、玩家全部连不上、面板显示旧数据，
    而原始 TCP 转发其实一个字节都没问题。故障现场就是日志停在

        [DB] connecting to mysql://mclink@web-mysql80:3306/mclink

    既没有下一行，也没有任何报错（连接是通的，只是那条 SQL 一直不回）。

    从这一版起：数据库初始化只是一段**有上限**的准备工作。超时就降级
    ——面板/账号不可用，联机照旧。中继绝不能因为数据库而"起不来"。
    """
    global store, accounts, tokens, admin_api, settings

    # Fail fast and loudly on a broken configuration file: a missing setting
    # would otherwise surface as a mysterious per-connection error later.
    _required_settings = (
        "VERBOSE",
        "ENABLE_CONTROL_PING",
        "ENABLE_CONTROL_INFO",
        "HANDSHAKE_TIMEOUT",
        "MAX_PLAYERS",
        "MAX_ROOM_LEN",
        "MAX_USERNAME_LEN",
        "BUFFER_SIZE",
        "STREAM_LIMIT",
        "DB_ENABLED",
        "REQUIRE_ACCOUNT",
        "ADMIN_API_ENABLED",
        "PROTOCOL_VERSION",
    )

    missing = [name for name in _required_settings if not hasattr(cfg, name)]

    if missing:
        raise RuntimeError(
            "relay_config.py 缺少配置项： " + ", ".join(missing)
        )

    tokens = TokenManager(ttl_seconds=cfg.TOKEN_TTL)

    store = build_store(cfg)

    connected = False

    # Always connect: the memory store needs this too, and it is what makes
    # `store.healthy()` authoritative for the "database down" decision.
    connect_ok = await db_step(store.connect(), "连接数据库")

    if cfg.DB_ENABLED:
        log(
            "[DB]",
            f"connecting to mysql://{cfg.DB_USER}@{cfg.DB_HOST}:"
            f"{cfg.DB_PORT}/{cfg.DB_NAME}",
        )

        # A timeout here means "no answer within DB_BOOT_TIMEOUT". We do NOT
        # declare the database healthy just because the connect call itself
        # returned -- the probe below is what decides that.
        if connect_ok is not DB_TIMEOUT:
            connected = await db_step(store.healthy(), "数据库探活") is True

        if connected:
            # 建表（含 ALTER 迁移）同样有硬上限：一条等待元数据锁的
            # ALTER TABLE 可以卡到天荒地老，而它卡住的是「中继能不能
            # 开始监听」。超时只降级建表，不放弃整个启动。
            schema_ok = await db_step(store.ensure_schema(), "建表/迁移")

            if schema_ok is DB_TIMEOUT:
                log(
                    "EVENT db.migrate.timeout error=",
                    "表结构可能不完整，中继照常联机；"
                    "面板若报错请单独排查数据库锁。",
                )
            elif schema_ok:
                log("EVENT db.ready schema=up_to_date")
            else:
                log("EVENT db.migrate.failed effect=relay_continues panels_and_accounts_may_be_unavailable")
        else:
            # Print the REAL driver error: "Access denied", "Unknown
            # database", "Authentication plugin ... cannot be loaded", etc.
            # Without this the log only says "unavailable" and nothing can
            # be diagnosed.
            log(
                "EVENT db.unavailable error=",
                f"{getattr(store, 'last_error', '') or '(没有错误信息)'}"[:400],
            )
            log(
                "EVENT db.unavailable.checks first=host second=port third=user_password fourth=database_exists",
                "1) 库是否存在 2) 账号密码是否正确 3) 账号是否有该库权限",
            )
    else:
        connected = True
        log("EVENT db.mode storage=memory accounts=disabled reason=MCLINK_DB_ENABLED=0")

    # Runtime settings: panel-editable switches that must not require a
    # container restart. Store first so the panel can talk to it.
    settings = SettingsService(
        store=store,
        defaults={
            "require_account": bool(cfg.REQUIRE_ACCOUNT),
            # The environment variable is the FIRST-BOOT default; after that
            # the stored (panel-edited) value wins. Without this entry a fresh
            # database would silently fall back to the built-in 8 and ignore
            # MCLINK_MAX_PLAYERS from compose.
            "max_players": int(cfg.MAX_PLAYERS),
        },
    )

    accounts = AccountService(
        store=store,
        tokens=tokens,
        throttle=LoginThrottle(
            max_attempts=cfg.LOGIN_MAX_ATTEMPTS,
            window_seconds=cfg.LOGIN_WINDOW,
        ),
        require_account=cfg.REQUIRE_ACCOUNT,
        bootstrap_admin=cfg.BOOTSTRAP_ADMIN,
        on_rename=rename_live_identity,
        on_rename_tokens=tokens.rebind_user,
        settings=settings,
    )

    # The environment variable is only the DEFAULT for the first boot; after
    # that the stored value wins, so a toggle survives a restart.
    #
    # Timed like everything else here: a settings read is a database query, and
    # a query that never returns must not keep the relay from listening.
    loaded_settings = await db_step(settings.load(), "读取运行时设置")

    if loaded_settings is DB_TIMEOUT:
        loaded_settings = settings.load_defaults()

    log(
        "[SETTINGS]",
        " ".join(f"{k}={v}" for k, v in loaded_settings.items()),
        f"(来源: {'数据库' if connected else '环境变量'})",
    )

    if connected:
        # ---- 幽灵房间的结构性根治 ----
        #
        # 每次进程启动都会生成一个新的 INSTANCE_ID，并把它写进每条房间记录。
        # 所以启动时这一条语句就能清掉"上一代进程留下的房间"——
        # 那些内存里已经不存在、却还留在数据库里的残留行。
        #
        # 参考项目的做法是"房间状态只存在内存里，所以不可能有残留"；
        # 我们为了面板需要镜像到 MySQL，就必须补上这一步。
        try:
            ghosts = await db_step(
                store.purge_other_instances(INSTANCE_ID), "清理残留房间"
            )

            if ghosts is DB_TIMEOUT:
                pass  # 已经报过超时了
            elif ghosts:
                log(
                    "EVENT db.stale_rooms.purged count=",
                    f"{len(ghosts)} 条 -> {ghosts[:10]}",
                )
            else:
                log("EVENT db.stale_rooms none=1 clean_start=true")
        except Exception as e:
            log("EVENT db.stale_room_purge.failed effect=minecraft_relay_unaffected error=", f"{e}")

        created = await db_step(accounts.bootstrap(), "创建初始管理员")

        if created and created is not DB_TIMEOUT:
            log("EVENT db.bootstrap_admin.created user=", created)

    if accounts_required() and not connected:
        log(
            "EVENT db.unavailable auth=required "
            "effect=host_and_player_connections_rejected "
            "hint=fix_the_database_or_set_MCLINK_REQUIRE_ACCOUNT=0"
        )

    admin_api = build_admin_api()

    if admin_api is not None:
        server = await admin_api.start()

        if server is not None:
            log(
                "[ADMIN API] listening on",
                f"{cfg.ADMIN_API_HOST}:{cfg.ADMIN_API_PORT}",
                "(仅 Docker 内网可访问)",
            )

    return True


async def teardown_services():
    global admin_api

    if admin_api is not None:
        await admin_api.stop()
        admin_api = None

    if store is not None:
        with contextlib.suppress(Exception):
            await store.close()


# ============================================================
# Status / metrics
# ============================================================

STARTED_AT = time.time()


async def collect_status():
    async with rooms_lock:
        room_list = list(rooms.values())

    snapshots = []
    for room in room_list:
        try:
            snapshots.append(await room.snapshot())
        except Exception:
            pass

    # ---- V2 service health (database / accounts) ----
    database_healthy = False

    if store is not None:
        try:
            # Bounded for the same reason as the startup path: this runs inside
            # the status loop, and a query with no deadline would stop the
            # status file from being refreshed -- which the container health
            # check reads. The panel's log download lives on the same loop.
            #
            # Kept short (it is a probe, not work): two missed status intervals
            # are what the health check tolerates.
            healthy = await db_step(store.healthy(), "状态探活", timeout=5)
            database_healthy = healthy is True
        except Exception:
            database_healthy = False

    # The status file answers the same question as the panel, so it must use
    # the same (connection based) definition of "online".
    try:
        online_accounts = await online_accounts_now()
    except Exception:
        online_accounts = []

    return {
        "service": "mc-link-relay",
        "version": "2.0.0",
        "protocol": cfg.PROTOCOL_VERSION,
        "started_at": STARTED_AT,
        "uptime": round(time.time() - STARTED_AT, 1),
        "listen": f"{cfg.HOST}:{cfg.PORT}",
        "max_players": max_players(),
        "rooms_total": len(snapshots),
        "players_total": sum(r["players"] for r in snapshots),
        "idle_tunnels_total": sum(r["idle_tunnels"] for r in snapshots),
        "rooms": snapshots,
        # ---- V2 ----
        "auth_required": bool(accounts_required()),
        "online_accounts": online_accounts,
        "database": {
            "enabled": bool(cfg.DB_ENABLED),
            "backend": store.name if store is not None else "none",
            "healthy": database_healthy,
            "degraded": bool(getattr(store, "degraded", False)),
            "errors": int(getattr(store, "errors", 0) or 0),
            # The raw driver error. Without this, status.json only said
            # "unhealthy" and every investigation started from zero.
            "last_error": str(getattr(store, "last_error", "") or "")[:400],
        },
        "admin_api": {
            "enabled": bool(cfg.ADMIN_API_ENABLED),
            "port": cfg.ADMIN_API_PORT,
            "requests": int(getattr(admin_api, "request_count", 0) or 0),
        },
        "generated_at": time.time(),
        "relay": relay_build(),
    }


def write_status_file(data):
    path = cfg.STATUS_FILE
    if not path:
        return

    directory = os.path.dirname(path)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except Exception:
            return

    try:
        fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=".status-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


async def status_loop():
    while True:
        try:
            await asyncio.sleep(cfg.STATUS_INTERVAL)

            data = await collect_status()

            write_status_file(data)

            print("")
            log("================= STATUS =================")

            if not data["rooms"]:
                log("Rooms: 0   (waiting for a host)")
            else:
                for room in data["rooms"]:
                    log(
                        f"room={room['room']} "
                        f"host={'ONLINE' if room['host_online'] else 'OFFLINE'} "
                        f"players={room['players']}/{room['max_players']} "
                        f"idle_tunnels={room['idle_tunnels']} "
                        f"waiting_players={room['waiting_players']}"
                    )

            log(
                f"TOTAL rooms={data['rooms_total']} "
                f"players={data['players_total']} "
                f"idle_tunnels={data['idle_tunnels_total']} "
                f"uptime={int(data['uptime'])}s"
            )

            database = data.get("database") or {}
            admin = data.get("admin_api") or {}

            log(
                "SERVICES "
                f"db={database.get('backend', 'none')}"
                f"/{'UP' if database.get('healthy') else 'DOWN'} "
                f"auth={'ON' if data.get('auth_required') else 'OFF'} "
                f"accounts={len(data.get('online_accounts') or [])} "
                f"admin_api={'ON' if admin.get('enabled') else 'OFF'} "
                f"(port {admin.get('port')}, {admin.get('requests', 0)} req)"
            )

            if database.get("degraded"):
                log(
                    "EVENT db.write.error "
                    f"errors={database.get('errors', 0)} "
                    "effect=panel_data_may_be_incomplete"
                )

            log("=========================================")
            print("")

        except asyncio.CancelledError:
            raise

        except Exception as e:
            log("[STATUS ERROR]", f"{e}")


# ============================================================
# Main
# ============================================================

BANNER = """
================================================================
                     MC-LINK TCP RELAY  V1
================================================================

Listen          : {host}:{port}
Max Players     : {max_players}
Status File     : {status_file}

Roles (frozen protocol):
  HOST          - Host control connection
  HOST_TUNNEL   - Minecraft data tunnel
  PLAYER        - Player connection

Minecraft payload is NOT parsed.
Relay forwards raw TCP bytes only.

Architecture:
  PLAYER <-> RELAY <-> HOST_TUNNEL
  HOST_TUNNEL <-> Host Minecraft LAN world

================================================================
"""


async def db_selfcheck():
    """
    Standalone database diagnostic.

    Prints the REAL driver error instead of hiding it behind "unavailable",
    which is the difference between a 10 second fix and an hour of guessing.

    Run it with:
        docker compose exec mc-relay python relay_server.py --dbcheck
    """
    print("=" * 62)
    print(" MC-Link 数据库自检")
    print("=" * 62)
    print(f" 主机   : {cfg.DB_HOST}:{cfg.DB_PORT}")
    print(f" 库名   : {cfg.DB_NAME}")
    print(f" 账号   : {cfg.DB_USER}")
    print(f" 密码   : {'已设置 (%d 位)' % len(cfg.DB_PASSWORD) if cfg.DB_PASSWORD else '!! 空 !!'}")
    print(f" 启用   : {cfg.DB_ENABLED}")
    print("-" * 62)

    if not cfg.DB_ENABLED:
        print("MCLINK_DB_ENABLED=0，数据库功能已关闭，无需检查。")
        return 2

    if not cfg.DB_PASSWORD:
        print("!! MCLINK_DB_PASSWORD 是空的，请在 docker-compose.yml 里填写。")
        return 3

    # ---- 1. DNS ----
    try:
        address = socket.gethostbyname(cfg.DB_HOST)
        print(f"[1/4] DNS 解析成功：{cfg.DB_HOST} -> {address}")
    except Exception as e:
        print(f"[1/4] !! DNS 解析失败：{e}")
        print("      中继容器没有加入 MySQL 所在的 Docker 网络。")
        print("      检查：docker inspect mc-relay --format "
              "'{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'")
        return 1

    # ---- 2. TCP ----
    try:
        with socket.create_connection((cfg.DB_HOST, cfg.DB_PORT), timeout=5):
            print(f"[2/4] TCP 连通：{cfg.DB_HOST}:{cfg.DB_PORT}")
    except Exception as e:
        print(f"[2/4] !! TCP 连接失败：{e}")
        print("      MySQL 容器没在运行，或端口不对。")
        return 1

    # ---- 3. driver ----
    driver = "aiomysql" if aiomysql is not None else (
        "pymysql" if pymysql is not None else None
    )

    if driver is None:
        print("[3/4] !! 没有可用的 MySQL 驱动（aiomysql / PyMySQL 都没装）。")
        print("      重新构建镜像：docker compose up -d --build")
        return 1

    print(f"[3/4] 驱动：{driver}")

    # ---- 4. login + query ----
    try:
        store = MySQLStore(cfg)
        await store.connect()

        row = await store.fetchone("SELECT VERSION() AS v, DATABASE() AS db")
        print(f"[4/4] 登录成功！MySQL 版本 {row['v']}，当前库 {row['db']}")

        tables = await store.fetchall("SHOW TABLES")
        names = [list(t.values())[0] for t in tables]
        print(f"      已有表：{names or '（还没有表，中继启动时会自动创建）'}")

        # ---- roles: the columns the panel now depends on ----
        #
        # A missing `users.role` makes every panel login return a bare 500,
        # with nothing anywhere saying why. Checking it here names the problem
        # while it is still cheap to fix.
        print("")
        print("      三级角色相关字段：")

        for column in ("role", "root_marker"):
            try:
                row = await store.fetchone(
                    "SELECT COUNT(*) AS n FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = 'users' AND column_name = %s",
                    (column,),
                )
                present = int((row or {}).get("n") or 0) > 0
            except Exception as e:  # noqa: BLE001
                print(f"        users.{column}: 查询失败 {e}")
                continue

            print(f"        users.{column}: {'OK' if present else '缺失 !!'}")

        try:
            rows = await store.fetchall(
                "SELECT username, role, is_admin FROM users ORDER BY id"
            )

            if rows:
                print("")
                print("      账号与角色：")

                for row in rows:
                    role = str(
                        row.get("role")
                        or ("admin" if int(row.get("is_admin") or 0) else "player")
                    )
                    print(f"        {row.get('username')}: {role}")
            else:
                print("")
                print("      账号表是空的 —— 中继还没有创建任何账号。")
                print("      在 compose 里设 MCLINK_BOOTSTRAP_ADMIN=用户名:密码")
                print("      然后重启中继。")
        except Exception as e:  # noqa: BLE001
            print("")
            print(f"      读取账号失败：{e}")
            print("      这说明 users 表结构不对，面板登录也会失败。")

        await store.close()

        print("-" * 62)
        print("结论：数据库配置正确 ✅  可以重启中继了：")
        print("      docker compose restart mc-relay")
        return 0

    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        print(f"[4/4] !! 登录或查询失败：\n      {message}")
        print("-" * 62)

        lowered = str(e).lower()

        if "access denied" in lowered:
            print("原因：账号或密码不对。")
            print("     1) 确认 docker-compose.yml 的 MCLINK_DB_PASSWORD")
            print("        和 /opt/mc-link/凭据.txt 里的『数据库密码』完全一致")
            print("     2) 在 MySQL 里重设一次（会强制生效）：")
            print("        docker exec -i web-mysql80 mysql -uroot -p'root密码' \\")
            print("          -e \"ALTER USER 'mclink'@'%' IDENTIFIED BY '凭据里的密码'; FLUSH PRIVILEGES;\"")
        elif "unknown database" in lowered:
            print("原因：数据库 mclink 不存在。执行：")
            print("     docker exec -i web-mysql80 mysql -uroot -p'root密码' \\")
            print("       -e \"CREATE DATABASE mclink DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\"")
        elif "caching_sha2_password" in lowered or "cryptography" in lowered:
            print("原因：镜像里缺少 cryptography。重新构建：")
            print("     docker compose up -d --build")
        elif "denied" in lowered or "permission" in lowered:
            print("原因：账号没有该库的权限。执行：")
            print("     docker exec -i web-mysql80 mysql -uroot -p'root密码' \\")
            print("       -e \"GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,INDEX,ALTER ON mclink.* TO 'mclink'@'%'; FLUSH PRIVILEGES;\"")
        else:
            print("把上面这行错误原文发出来即可定位。")

        return 1


async def rooms_selfcheck():
    """
    Diagnose "the client room list is empty but the panel shows rooms".

    The client uses the ROOMS control query; the panel uses the admin API +
    database. They are different paths, so they can disagree. This checks the
    exact path the CLIENT uses.

    Run it with:
        docker compose exec mc-relay python relay_server.py --rooms
    """
    print("=" * 62)
    print(" MC-Link 房间列表自检（客户端走的那条路）")
    print("=" * 62)

    async with rooms_lock:
        live = list(rooms.values())

    print(f"内存中的房间（真正在线的）: {len(live)}")

    for room in live:
        try:
            snap = await room.snapshot()
        except Exception as e:
            print(f"  !! {room.name} snapshot 失败: {e}")
            continue

        print(
            f"  - {snap['room']:20s} "
            f"host={snap.get('host_username') or '(未登录)':12s} "
            f"players={snap.get('players')}/{snap.get('max_players')} "
            f"idle_tunnels={snap.get('idle_tunnels')}"
        )

    stale = []

    if store is not None:
        try:
            rows = await store.list_rooms()
        except Exception as e:
            rows = []
            print(f"!! 读取数据库房间失败: {e}")

        live_ids = {r.name for r in live}
        stale = [r for r in (rows or []) if r.get("room_id") not in live_ids]

    print(f"数据库里的残留记录        : {len(stale)}")

    for row in stale:
        print(
            f"  - {str(row.get('room_id')):20s} "
            f"updated_at={row.get('updated_at')}"
        )

    print("-" * 62)

    if not live and stale:
        print("结论：现在没有任何在线房间，客户端列表为空是【正常】的。")
        print("      那几条是历史残留记录，面板会单独列出来，可以一键清理。")
    elif live:
        print("结论：有在线房间，客户端的「房间列表」应该能显示出来。")
        print("      如果客户端仍然为空，说明客户端是旧版 exe —— 重新打包即可。")
    else:
        print("结论：没有房间，也没有残留记录，一切正常。")

    print("")
    print("提示：客户端房间列表为空 + 面板有房间，通常是下面两种之一：")
    print("  1) 中继是旧版（不支持 ROOMS 查询）-> docker compose up -d --build")
    print("  2) 面板显示的是数据库残留记录，不是真正在线的房间")

    return 0


async def main():
    if "--dbcheck" in sys.argv:
        return await db_selfcheck()

    if "--rooms" in sys.argv or "--roomcheck" in sys.argv:
        return await rooms_selfcheck()

    print(
        BANNER.format(
            host=cfg.HOST,
            port=cfg.PORT,
            max_players=cfg.MAX_PLAYERS,
            status_file=cfg.STATUS_FILE or "(disabled)",
        ),
        flush=True,
    )

    # ---- V2 services (database, accounts, admin API) ----
    await setup_services()

    # ---- V2.1: built-in STUN server (UDP, independent of the TCP relay) ----
    #
    # Deliberately started after the relay's own services and never allowed to
    # be fatal: if the UDP port is unavailable (or blocked by the cloud
    # security group) the relay must keep serving exactly as before. Clients
    # fall back to public STUN servers on their own.
    try:
        import stun_server

        started = await stun_server.start_stun_server()

        if started:
            log("EVENT stun.ready role=self_service note=no_public_server_needed")
    except Exception as e:  # noqa: BLE001 - never fatal
        log("EVENT stun.start.failed effect=relay_unaffected error=", str(e)[:200])

    server = await asyncio.start_server(
        client_connected,
        cfg.HOST,
        cfg.PORT,
        limit=cfg.STREAM_LIMIT,
    )

    addresses = ", ".join(
        str(sock.getsockname()) for sock in server.sockets
    )

    log(f"Relay listening on: {addresses}")

    status_task = asyncio.create_task(status_loop())
    sync_task = asyncio.create_task(db_sync_loop())
    sweeper_task = asyncio.create_task(empty_room_sweeper())
    watchdog_task = asyncio.create_task(host_watchdog())

    # V3: reap members whose control connection went silent. A stranded host
    # position is worse than a stranded room -- nobody else can claim it, so
    # the room becomes unusable while still looking perfectly healthy.
    member_watchdog_task = asyncio.create_task(member_watchdog())

    # 审计日志保留期（超过 EVENTS_RETENTION_DAYS 的行定期删掉）。
    retention_task = asyncio.create_task(events_retention_loop())

    background_tasks = (
        status_task,
        sync_task,
        sweeper_task,
        watchdog_task,
        member_watchdog_task,
        retention_task,
    )

    try:
        async with server:
            await server.serve_forever()

    except asyncio.CancelledError:
        pass

    finally:
        for task in background_tasks:
            task.cancel()

        await asyncio.gather(*background_tasks, return_exceptions=True)

        # Final cleanup so `docker stop` is graceful.
        async with rooms_lock:
            room_list = list(rooms.values())

        for room in room_list:
            try:
                await room.close(reason="server shutdown")
            except Exception:
                pass

        await teardown_services()

        with contextlib.suppress(Exception):
            import stun_server

            await stun_server.stop_stun_server()

        log("Relay stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("")
        log("Relay stopped by user.")
