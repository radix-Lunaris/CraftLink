"""
MC-Link client -- FROZEN wire protocol.

============================================================================
 DO NOT CHANGE THE STRING FORMATS IN THIS FILE LIGHTLY.
============================================================================

These exact byte strings were verified against a real Minecraft 1.20.1 Forge
session (host + up to 3 players). Changing them breaks compatibility with the
already-deployed relay and with the archived reference clients.

FROZEN LINES
------------
Client  -> Server (first line, handshake):
    HOST <room>\n
    HOST_TUNNEL <room>\n
    PLAYER <room>\n

V1 additive extension (still two leading fields, still whitespace separated,
old servers ignore the extra fields and old clients simply do not send them):
    HOST <room> <client_id> <username>\n
    PLAYER <room> <client_id> <username>\n

V2 additive extension (one more optional field):
    HOST <room> <client_id> <username> <token>\n
    PLAYER <room> <client_id> <username> <token>\n

HOST_TUNNEL and every reply below are UNCHANGED in V2. The Minecraft data path
has not been touched by any version.

Server -> Client:
    HOST:         OK HOST room=<room>\n
    HOST_TUNNEL:  WAIT\n          then  START <session_id>\n
    PLAYER:       CONNECTED\n     (or ERROR <CODE>\n)

HostTunnel -> Server, after its local Minecraft connection is up:
    READY\n

After `READY` / `CONNECTED` the socket is 100% opaque Minecraft TCP data.
NO control line may ever be written into a data tunnel again.
HOST CONTROL CHANNEL ONLY (additive, never on a data tunnel):
    Client -> Server : PING <anything>\n
    Server -> Client : PONG <anything>\n
    Server -> Client : INFO players=2/10 idle_tunnels=8 waiting_players=0\n
    Server -> Client : INFO protocol=2\n
    Client -> Server : DELETE <token>\n            (delete my own room)
    Server -> Client : OK DELETE room=<room>\n
                       ERROR NOT_HOST / DELETE_ROOM_BUSY\n

房间名与房间 token（V3，纯新增）
--------------------------------
上面所有 <room> 字段都是 **room token**：一个 percent-encode 过的 ASCII
记号（对用户隐藏的"房间 ID"）。玩家看到并输入的是【房间名】，客户端在发出去
之前用 room_token() 转换（见本文件底部的两个函数）。中继用同一个记号当房间
ID，所以同名 = 同一个房间，而房间里可以有任何字符（中文、空格…）。

    CREATE/JOIN 的第 6 个字段  = 房间名编码后的 token
    OK JOIN 的回包新增 name=<房间名编码>  → 客户端显示这个名字
    ROOMS 的每一行新增 name=<房间名编码> 与 host_live=0/1
      （host_live=0 = 主机位置有人认领但还没开服）

V2 CONTROL-PLANE CONNECTIONS (new connection types, sentinel room name)
----------------------------------------------------------------------
Each one opens its OWN socket, sends exactly one line and then closes.
The sentinel `__mclink__` can never be a real room name.

    LOGIN  __mclink__ <username> <password> <client_id>
        -> OK LOGIN user=<name> admin=0/1 protocol=2 token=<token>
        -> ERROR AUTH_BAD_CREDENTIALS / AUTH_DISABLED / AUTH_RATE_LIMIT /
                 AUTH_UNAVAILABLE / AUTH_BAD_REQUEST

    ROOMS  __mclink__ <client_id> <username>
        -> ROOMS <n>
           ROOM <room> <host> <players>/<max> <idle_tunnels> <waiting> <empty|busy>
           DONE

    DELETE __mclink__ <client_id> <username> <token> <room>
        -> OK DELETE room=<room>
        -> ERROR NOT_HOST / DELETE_ROOM_BUSY / ROOM_NOT_FOUND /
                 AUTH_REQUIRED / AUTH_INVALID_TOKEN / AUTH_DISABLED

    RENAME __mclink__ <client_id> <username> <token> <new_username>
        -> OK RENAME user=<new_username>
        -> ERROR BAD_USERNAME / USERNAME_TAKEN / RENAME_LIMIT /
                 AUTH_REQUIRED / AUTH_INVALID_TOKEN / AUTH_DISABLED /
                 AUTH_DISABLED_SERVER / RENAME_FAILED
        Self-service rename: rate limited (3 per 30 days) and it NEVER
        disconnects the client -- the live room/session names are patched and
        the session token is rebound, so the new name takes effect at once.
        The panel's rename is unlimited and uses a different code path.

    LOGOUT __mclink__ <client_id> <username> <token>
        -> OK LOGOUT
        -> ERROR AUTH_REQUIRED / AUTH_INVALID_TOKEN / AUTH_DISABLED_SERVER
        Releases the session token immediately.

        Without this the panel's "在线账号" count was wrong by construction:
        it counts accounts holding a VALID TOKEN, and a token stayed valid for
        its full 12 hour TTL after the user closed the client. So logging out
        and quitting left the account showing as online until the next day.
        The client calls this when it exits cleanly.
"""

import contextlib
import struct
from urllib.parse import quote, unquote

# ------------------------------------------------------------
# Peer-link framing (the DIRECT path's own wire format)
# ------------------------------------------------------------
#
# ADDITIVE ONLY: these bytes never touch the relay and never touch a Minecraft
# socket. They exist between two peers on a punched UDP tunnel, and nowhere
# else -- the relay still forwards raw bytes and parses nothing.
#
# Ported verbatim from the reference implementation's `protocol.py`, where the
# same three constants carry the same three meanings. The reason a peer link
# needs its own framing at all is stated in that file and is worth repeating,
# because getting it wrong looks like "the second Minecraft connection always
# falls back to the relay":
#
#   One peer link carries MANY Minecraft connections (a server-list probe,
#   then the real join, then maybe a second player), and each needs its OWN
#   backend connection to the local MC server -- the MC protocol is stateful
#   and the server closes the socket after answering a status request.

#: How much to read from a local Minecraft socket at a time.
#:
#: MC sends many small packets (movement, block updates are a few dozen bytes),
#: so a big read plus TCP_NODELAY avoids Nagle batching a round trip of latency
#: onto every one of them.
BUFFER_SIZE = 64 * 1024

#: Stream payload frame: [0x02][sid:4][len:4][payload].
STREAM_DATA = 0x02

#: 1 type byte + 4 stream id + 4 payload length.
SID_HDR = 9

#: A frame larger than this is corrupt; the parser resyncs instead of trusting it.
MAX_FRAME = 8 * 1024 * 1024

#: Out-of-band notes about a stream (open / close). They travel on the
#: signalling channel, NOT inside the peer link -- see `channel.py`.
STREAM_OPEN = 0x10
STREAM_CLOSE = 0x11


def frame(sid, data):
    """
    [0x02][sid:4][len:4][payload] -- self-delimiting.

    The length is essential: a UDP tunnel hands back a large message in
    MTU-sized pieces, so the reader cannot rely on one recv() being one logical
    message. With an explicit length the receiver reassembles across an
    arbitrary split.
    """
    return (bytes([STREAM_DATA]) + struct.pack(">I", sid)
            + struct.pack(">I", len(data)) + data)


def control_frame(kind, sid):
    """A tiny out-of-band note about a stream (open / close)."""
    return bytes([kind]) + struct.pack(">I", sid)


# ------------------------------------------------------------
# Roles
# ------------------------------------------------------------

ROLE_HOST = "HOST"
ROLE_HOST_TUNNEL = "HOST_TUNNEL"
ROLE_PLAYER = "PLAYER"

# V2 control-plane roles (always with the sentinel room name).
ROLE_LOGIN = "LOGIN"
ROLE_ROOMS = "ROOMS"
ROLE_DELETE = "DELETE"
ROLE_RENAME = "RENAME"
ROLE_LOGOUT = "LOGOUT"

# V3 room membership. A member control connection stays open for as long as
# the person is IN a room -- even while they are not connected to Minecraft --
# which is what lets the relay know who is present, who owns the room and who
# holds the host position.
#
#   CREATE  makes you the room's OWNER. The room's lifetime is bound to you.
#   JOIN    enters an existing room as a member.
ROLE_JOIN = "JOIN"
ROLE_CREATE = "CREATE"
ROLE_LEAVE = "LEAVE"
ROLE_CLAIM_HOST = "CLAIM_HOST"
ROLE_RELEASE_HOST = "RELEASE_HOST"

#: SESSION -- the read-only identity probe (PROTOCOL §8.10, ADDITIVE).
#:
#: User 2026-09-29: 「连接和身份是两回事」. `KICKED` / `AUTH_INVALID_TOKEN` /
#: `ROOM REVOKED` are all PASSIVE pushes: once the socket is already dead the
#: client receives nothing, so before auto-reconnecting it has to ASK.
#:
#:     SESSION __mclink__ <client_id> <username> <token>
#:         ◄── OK SESSION valid=.. in_room=.. token=<account|guest|none>
#:                        room=.. name=.. reason=.. auto_reconnect=0|1 relogin=0|1
#:
#: It is a short-lived, one-line, read-only connection to the sentinel room.
#: An older relay answers `ERROR UNKNOWN_ROLE`; the client then falls back to
#: its previous behaviour, unchanged.
ROLE_SESSION = "SESSION"

# ------------------------------------------------------------
# V3 direct path (STUN mode) -- ADDITIVE ONLY
# ------------------------------------------------------------
#
# These ride the SAME member control connection the room already uses. That is
# deliberate (the control plane is reused rather than given a second channel),
# and it is also what keeps the change backwards compatible: an older relay
# ignores unknown lines, and an older client never sends them.
#
# The relay is a FORWARDER here, exactly as it is for Minecraft bytes. It never
# parses a candidate, never picks a plan, never touches the data path:
#
#   DIRECT_CAND    <ip> <port> [k=v ...]  -> forwarded to the room peer
#   DIRECT_PUNCH   [k=v ...]              -> forwarded; carries the start time
#   DIRECT_HIT     [k=v ...]              -> "we got through", forwarded
#   DIRECT_INGRESS <public_ip> <port>     -> the relay asks its STUN server to
#                                            send us ONE datagram from a fresh
#                                            socket (reachability test)
ROLE_DIRECT_CAND = "DIRECT_CAND"
ROLE_DIRECT_PUNCH = "DIRECT_PUNCH"
ROLE_DIRECT_HIT = "DIRECT_HIT"
ROLE_DIRECT_INGRESS = "DIRECT_INGRESS"

#: The room's CONNECTION MODE (relay / stun), chosen by the HOST and shared by
#: every member. It rides the same membership connection as everything else.
ROLE_CONNMODE = "CONNMODE"

#: The OK / the broadcast token.
LINE_ACK_CONNMODE = "OK CONNMODE"
LINE_ERR_NOT_HOST = "ERROR NOT_HOST"

#: The lines the relay forwards verbatim to the other member of the room.
#: `DIRECT_ADDR <ip> <port> from=<cid> [sub=...] [filt=...]`（ADDITIVE，2026-09-30）
#:
#: 用户要求：**直连模式下，每个对端都要把自己的地址和端口广播给房间里所有人**，
#: 这样成员列表每个人都能看到（不只是 host 能看到玩家）。
#:
#: 和 DIRECT_CAND 的区别（这条很关键，不许混）：
#:   * `DIRECT_CAND` 是**打洞候选**，中继按 `to=` 精确投递给"该打的人"
#:     （星形拓扑：player->host，host->每个 player）。它是协议的一部分，
#:     **绝不能**拿去广播 —— 那会把别人的候选喂进打洞状态机。
#:   * `DIRECT_ADDR` 是**只读的自我介绍**：不参与打洞、不改任何状态机，
#:     收到的人只把它记进"这个 peer 的公网端点"，给成员列表显示。
#:
#: 老客户端/老中继不认识它：老中继会把它当未知行忽略（客户端重试无妨），
#: 老客户端收到也不认（直接忽略）—— 两边行为都不变。
ROLE_DIRECT_ADDR = "DIRECT_ADDR"

DIRECT_FORWARDED = (
    ROLE_DIRECT_CAND,
    ROLE_DIRECT_PUNCH,
    ROLE_DIRECT_HIT,
    ROLE_DIRECT_ADDR,
)

#: Field name for the coordinated punch start (his `startIn`).
#:
#: It is a delay in SECONDS relative to the moment the line ARRIVES, never an
#: absolute timestamp: an absolute epoch would need our clock, the peer's clock
#: and the relay's clock to agree, and a peer-to-peer link is the last place to
#: assume that. With a relative delay each side starts `startIn` seconds after
#: it received the line, so the two starts differ only by the difference in
#: their round-trip times -- tens of milliseconds, however wrong the clocks are.
DIRECT_FIELD_TO = "to"

#: Field name for the addressed target (`to=<client_id>`), see DIRECT_FIELD_TO.
#:
#: A 3-member room breaks the relay's old "host -> earliest joiner" routing:
#: the host's candidate would always reach player 1 and the third person would
#: never receive one, so the punch could never sync (exactly the "后面进来的
#: 直接冷落在一边" complaint). The HOST (which knows every guest's client id
#: from the `peers=` roster) now addresses each direct line with `to=`; the
#: relay routes by that token and still parses nothing else. Guests never send
#: it (they cannot reliably know the host's client id, and the old "guest ->
#: host" routing is already correct for them). Old relays ignore the token --
#: two-player rooms keep working; old clients never send it -- fallback routing.
#:
DIRECT_PUNCH_FIELD_START_IN = "startIn"

#: How long each side waits before the first spray (his PUNCH_START_DELAY_S).
#:
#: Why a delay at all: a punch is a collision between two moving port cursors,
#: and it only works if both ends are spraying at the SAME time. Without a
#: shared start each end begins whenever its own candidate exchange happened to
#: finish -- and the windows, each a few hundred ports wide, pass each other in
#: silence.
#:
#: Why 1.5s: long enough that the line has certainly arrived on both sides and
#: both ends have their socket array bound, short enough that it costs nothing
#: against the peer's port cursor (measured drift is ~73 ports/second, so 1.5s
#: is ~110 ports -- well inside the 800-port spray).
PUNCH_START_DELAY_S = 1.5

#: Nobody waits longer than this for a coordinated start, whatever the line
#: says (his PUNCH_START_MAX_WAIT_S). A malformed or hostile value must not be
#: able to park a session forever.
PUNCH_START_MAX_WAIT_S = 5.0

#: "My client is open and logged in." One lightweight connection, held open,
#: so the panel's 在线账号 means "actually running right now" instead of
#: "holds a token that has not expired yet".
ROLE_PRESENCE = "PRESENCE"

# The room field used by every V2 control-plane request. It can never collide
# with a real room name because room names are validated as [^\s]{1,64} and a
# server that does not know V2 simply answers ERROR UNKNOWN_ROLE.
SENTINEL = "__mclink__"

# ------------------------------------------------------------
# Server -> client lines
# ------------------------------------------------------------

LINE_WAIT = "WAIT"
LINE_START = "START"
LINE_READY = "READY"
LINE_ACK_HOST = "OK HOST"
LINE_CONNECTED = "CONNECTED"
LINE_ERROR = "ERROR"
LINE_PONG = "PONG"
LINE_INFO = "INFO"
LINE_ACK_LOGIN = "OK LOGIN"
LINE_ROOMS = "ROOMS"
LINE_ROOM = "ROOM"
LINE_DONE = "DONE"
LINE_ACK_DELETE = "OK DELETE"
LINE_DELETE = "DELETE"
LINE_ACK_RENAME = "OK RENAME"
LINE_RENAME = "RENAME"
LINE_ACK_LOGOUT = "OK LOGOUT"
LINE_LOGOUT = "LOGOUT"

#: Reply to the read-only identity probe `SESSION __mclink__ ...` (§8.10).
LINE_ACK_SESSION = "OK SESSION"

# V3 room membership
LINE_ACK_JOIN = "OK JOIN"
LINE_ACK_LEAVE = "OK LEAVE"
LINE_ACK_CLAIM_HOST = "OK CLAIM_HOST"
LINE_ACK_RELEASE_HOST = "OK RELEASE_HOST"
LINE_ACK_PRESENCE = "OK PRESENCE"
#: Pushed to every member when the room is force-destroyed by an admin.
#: Carries a reason, e.g. "ROOM REVOKED 管理员关闭了房间".
LINE_ROOM_REVOKED = "ROOM REVOKED"
#: Pushed to every member whenever the host position changes, so a client can
#: show "房间还没有房主" without polling.
LINE_HOST_STATE = "HOST_STATE"

#: 换模式（CONNMODE）之后，中继在**每个成员的控制连接**上推的一行 ——
#: **不带 `ERROR` 前缀**（见 docs/PROTOCOL.md §8.3）：
#:
#:     CONN_MODE_CHANGED 连接模式已切换，请按新模式重新开始连接
#:
#: 它和 `HOST_LEFT` 是同一类"可识别原因"：客户端收到之后必须**严格重建全部
#: 通路**（设计基准 §D 末句）—— 旧模式下的洞/隧道/连接状态机全部作废，因为
#: 新模式的 NAT 映射和它们不是同一套。它**不是**网络抖动，不许当普通掉线重试，
#: 也不许当"被踢"退出房间。
LINE_CONN_MODE_CHANGED = "CONN_MODE_CHANGED"

#: 踢人（KICK）—— 房主 / 当前主机在成员连接上发起的请求。
#:
#: 请求：KICK <room_token> <target_client_id>
#: 应答：OK KICK <target_client_id> [OWNER_LEFT_ROOM]
LINE_ACK_KICK = "OK KICK"
#: 推给被踢的人：KICKED by=<label> reason=<text>，随后中继关掉他的成员连接。
LINE_KICKED = "KICKED"

#: 成员显示名更新（IDENTITY）—— 纯新增，老中继忽略、老客户端不发。
#:
#: 用户要求（docs/ISSUES-20260927.md 问题 5）：玩家身份绑在**设备**上
#: （`client_id`）。人还在房间里的时候登录 / 改名，房间名册里那颗名字必须跟着变，
#: 而不是等到下次 CREATE/JOIN 握手才更新 —— 中继的显示名原来只在握手那一刻取一次。
#:
#: 请求：IDENTITY <username> <token> ver=<APP_VERSION>
#:       * `ver=` 按 V3-AGENTS §0.1 挂在**整行最后一个 token**；
#:       * 没有 token（免登录）就发 `-`。
#: 应答：OK IDENTITY <name>            （<name> = 中继记下的新显示名）
#:       ERROR IDENTITY_INVALID / IDENTITY_NO_ROOM / IDENTITY_PENDING …
LINE_ROLE_IDENTITY = "IDENTITY"
LINE_ACK_IDENTITY = "OK IDENTITY"
LINE_ERR_IDENTITY = "ERROR IDENTITY"

#: 「我点了开始连接 / 我停了」的显式上报（STARTED，PROTOCOL §8.6）——
#: 纯新增，老中继忽略未知行、老客户端从不发。
#:
#: 用户口径（2026-09-28）：「已开始连接」= **用户在客户端点过「开始连接」**，
#: 而不是"已经建立了 PLAYER / HOST 连接"。STUN 模式是"先点开始打洞、游戏连接
#: 是之后的事"，只按连接现算的话 Host 一个目标都拉不到（见 CLIENT-PUNCH-FLOW
#: §1.1）；显式上报把这件事说清楚，中继把它并进 `started=`。
#:
#: 请求：STARTED <1|0>
#: 应答：OK STARTED started=<1|0>
#:       ERROR BAD_STARTED 必须是 STARTED 0 或 STARTED 1
LINE_STARTED = "STARTED"
LINE_ACK_STARTED = "OK STARTED"
LINE_ERR_STARTED = "ERROR BAD_STARTED"

# ------------------------------------------------------------
# Error codes returned by the relay
# ------------------------------------------------------------

ERR_NO_HOST = "NO_HOST"

#: 客户端与服务端版本不一致（版本门）。服务端会在同一行给出两边版本。
ERR_VERSION_MISMATCH = "VERSION_MISMATCH"
ERR_ROOM_FULL = "ROOM_FULL"
ERR_ROOM_ALREADY_HAS_HOST = "ROOM_ALREADY_HAS_HOST"
ERR_BAD_HANDSHAKE = "BAD_HANDSHAKE"
ERR_BAD_ROOM = "BAD_ROOM"
ERR_UNKNOWN_ROLE = "UNKNOWN_ROLE"
ERR_ROOM_CLOSED = "ROOM_CLOSED"

# V2: authentication
ERR_AUTH_REQUIRED = "AUTH_REQUIRED"
ERR_AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"
ERR_AUTH_DISABLED = "AUTH_DISABLED"
ERR_AUTH_BAD_CREDENTIALS = "AUTH_BAD_CREDENTIALS"
ERR_AUTH_RATE_LIMIT = "AUTH_RATE_LIMIT"
ERR_AUTH_UNAVAILABLE = "AUTH_UNAVAILABLE"
ERR_AUTH_BAD_REQUEST = "AUTH_BAD_REQUEST"
ERR_AUTH_DISABLED_SERVER = "AUTH_DISABLED_SERVER"

# V2: room management
ERR_ROOM_NOT_FOUND = "ROOM_NOT_FOUND"
ERR_NOT_HOST = "NOT_HOST"
ERR_DELETE_ROOM_BUSY = "DELETE_ROOM_BUSY"

#: Somebody else already owns a room with this name. CREATE is refused rather
#: than silently joining/stealing it.
ERR_ROOM_EXISTS = "ROOM_EXISTS"

#: The room was closed by an admin: the reason follows the code.
ERR_ROOM_REVOKED = "ROOM_REVOKED"

# ---- 踢人（KICK）----
#
# 服务端（server/room_members.py::handle_member_kick）的权限矩阵：
#   * 请求者 == 目标                -> KICK_SELF
#   * 房间所有者                    -> 可以踢除自己以外的任何人
#   * 当前主机（非所有者）          -> 只能踢非所有者、且不是自己的人
#   * 其他人                        -> KICK_FORBIDDEN
ERR_KICK_FORBIDDEN = "KICK_FORBIDDEN"
ERR_KICK_NO_SUCH_MEMBER = "KICK_NO_SUCH_MEMBER"
ERR_KICK_SELF = "KICK_SELF"

# V2: self-service rename
ERR_BAD_USERNAME = "BAD_USERNAME"
ERR_USERNAME_TAKEN = "USERNAME_TAKEN"
ERR_RENAME_LIMIT = "RENAME_LIMIT"
ERR_RENAME_FAILED = "RENAME_FAILED"

# ------------------------------------------------------------
# Limits (must stay <= server limits)
# ------------------------------------------------------------
#
# 注意：必须定义在 ERROR_TEXT 之前 —— 下面的文案里用到了它们。
# （踩过一次：放到后面会在导入时报 NameError，整个客户端起不来。）

MAX_ROOM_LEN = 64
MAX_USERNAME_LEN = 32
MAX_CLIENT_ID_LEN = 64

# Human readable (Chinese) descriptions used by the GUI.
ERROR_TEXT = {
    "AUTO_UNSUPPORTED": "AUTO 房间要求支持 v5 自动连接的客户端，请先更新不兼容成员的客户端",
    "AUTO_UNSUPPORTED_OR_IDENTITY": "AUTO 连接身份未通过验证，请重新进入房间后再开始连接",
    "AUTO_RESUME_DENIED": "原游戏会话已失效，无法恢复备用中继，请重新连接游戏",
    "AUTO_STREAM_LIMIT": "同时进行的游戏连接过多，请关闭多余的服务器探测后重试",
    ERR_NO_HOST: "房间不存在或房主不在线",    ERR_ROOM_FULL: "房间已满（达到最大人数）",
    ERR_ROOM_ALREADY_HAS_HOST: "该房间已经有房主了",
    ERR_BAD_HANDSHAKE: "协议握手被服务器拒绝",
    ERR_BAD_ROOM: (
        "房间名不能为空。可以写中文、英文、数字和空格，"
        f"最长 {MAX_ROOM_LEN} 个字符。"
    ),
    ERR_UNKNOWN_ROLE: "服务器不认识该角色（客户端与服务端版本不匹配）",
    ERR_ROOM_CLOSED: "房间已关闭",
    ERR_VERSION_MISMATCH: (
        "客户端与服务器版本不一致，请更新到同一版本后再使用"
    ),
    # ---- V2 ----
    ERR_AUTH_REQUIRED: "请先登录账号",
    ERR_AUTH_INVALID_TOKEN: "登录状态已失效，请重新登录",
    ERR_AUTH_DISABLED: "账号已被管理员禁用",
    ERR_AUTH_BAD_CREDENTIALS: "用户名或密码错误",
    ERR_AUTH_RATE_LIMIT: "尝试过于频繁，请稍后再试",
    ERR_AUTH_UNAVAILABLE: "服务器数据库暂时不可用，请稍后再试",
    ERR_AUTH_BAD_REQUEST: "用户名或密码格式不正确",
    ERR_AUTH_DISABLED_SERVER: "服务器没有开启账号系统",
    ERR_ROOM_NOT_FOUND: "房间不存在（可能已经关闭）",
    ERR_ROOM_EXISTS: (
        "这个房间名已经有人在用了。\n"
        "建房间请换一个名字，或者直接「加入房间」进他的房间。"
    ),
    ERR_ROOM_REVOKED: "房间已被管理员关闭",
    ERR_KICK_FORBIDDEN: "只有房主或当前主机可以踢人",
    ERR_KICK_NO_SUCH_MEMBER: "该成员不在房间里",
    ERR_KICK_SELF: "不能踢出自己",
    ERR_NOT_HOST: "你不是这个房间的房主，只有房主能删除",
    ERR_DELETE_ROOM_BUSY: "房间里还有玩家，不能删除（请先让玩家离开）",
    ERR_BAD_USERNAME: "用户名不合法（2-32 个字符，不能有空格）",
    ERR_USERNAME_TAKEN: "这个用户名已经被别人用了",
    ERR_RENAME_LIMIT: "本月的自助修改次数已用完（改名和改密码共用 3 次）",
    ERR_RENAME_FAILED: "改名失败，请联系管理员",
}

# ------------------------------------------------------------
# Defaults
# ------------------------------------------------------------

#: 房间名默认留空：不再预填一个 test001。
#:
#: A prefilled name is how two people ended up in the same room by accident:
#: whoever pressed 创建 first took "test001", and the next person pressed the
#: same button with the same prefilled name. The field is empty now and the
#: user types their own name.
DEFAULT_ROOM = ""


# ------------------------------------------------------------
# 房间名 <-> 房间 token（房间 ID）
# ------------------------------------------------------------
#
# 玩家输入的是【房间名】：中文、英文、空格、emoji 都行。
# 协议行是空格分隔的，所以发给中继之前必须先 percent-encode 成一个 ASCII
# 记号（room token）。中继那边用同一个记号当房间 ID，于是：
#
#     同名 = 同一个房间（重名自然被拒），房间名想怎么取就怎么取。
#
# 见 server/room_names.py（两边必须是同一套规则）。

def room_token(name):
    """
    房间名 / 房间 ID -> 协议里用的 room token（ASCII，无空格无斜杠）。

    ============================================================================
      ★ 必须**幂等**（用户 2026-09-29 第三轮第 2 条：客户端踢人报
        「玩家不在房内 / 目标不在这个房间里」）
    ============================================================================

    中继用的是 `room_names.room_token()`，它**先 unquote 再 quote**，所以
    `room_token(room_token(x)) == room_token(x)`（服务端那个文件里写明了）。

    客户端这里以前只做 `quote()`：把**已经是 token** 的值（`OK JOIN` 回包里的
    `room=`、房间列表里的 `room=`、`RoomState.room`）再编码一次 —— 中文房间名
    就成了 `%25E6%99%9A...`（多一层 `%25`）。于是 `KICK` / `LEAVE` / `DELETE`
    带上的是一个**对不上的房间**，中继只能回
    `ERROR KICK_NO_SUCH_MEMBER 目标不在这个房间里`。

    现在两端语义一致：先 unquote（把已有编码还原）再 quote。对"用户刚输入的
    裸名字"结果一个字不变；对"已经是 token"的输入不再二次编码。
    """
    if not name:
        return ""

    text = str(name).strip()

    with contextlib.suppress(Exception):
        text = unquote(text)

    return quote(text, safe="")


def room_name_from_token(token):
    """room token -> 玩家看到的房间名。"""
    if not token:
        return ""

    with contextlib.suppress(Exception):
        return unquote(str(token))

    return str(token)


def room_name_problem(name, max_len=None):
    """
    房间名合法性。返回 None 表示可用，否则返回给用户看的中文原因。

    规则只有三条（和服务器端 room_names.name_problem 一致）：
    非空、没有控制字符、不超过长度上限。空格、中文、emoji 都允许。
    """
    limit = max_len or MAX_ROOM_LEN

    if name is None or not str(name).strip():
        return "请填写房间名"

    text = str(name).strip()

    if len(text) > limit:
        return f"房间名最长 {limit} 个字符（你填了 {len(text)} 个）"

    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return "房间名不能包含换行、制表符等控制字符"

    return None


DEFAULT_MC_PORT = 25565
DEFAULT_LOCAL_PORT = 25566

#: Room capacity: default 8, configurable ceiling 64 (host included).
DEFAULT_MAX_PLAYERS = 8
MIN_MAX_PLAYERS = 2
MAX_MAX_PLAYERS = 64

#: Number of HOST_TUNNEL connections to open, when no capacity is known yet.
#:
#: The tunnel count FOLLOWS the room capacity (see Config.validated): the room
#: can never hold more players than it has tunnels, so a fixed number here was
#: simply wrong -- it opened ten channels for a room limited to eight, and the
#: status card printed that ten as if it were the player limit.
DEFAULT_MAX_TUNNELS = DEFAULT_MAX_PLAYERS


def version_mismatch_peers(line):
    """
    版本门拒绝时的两边版本。

    "ERROR VERSION_MISMATCH server=3.0.8 client=3.0.7" -> ("3.0.8", "3.0.7")
    不是版本门（或没带版本）时返回 ("", "")。
    """
    text = str(line or "")
    server = ""
    client = ""

    for tok in text.split():
        if tok.startswith("server="):
            server = tok[7:]
        elif tok.startswith("client="):
            client = tok[7:]

    # 大多数调用方拿到的是已经 `describe_error()` 过的中文文案（异常消息一路
    # 传到界面时原始 token 已经没了），所以这里再把文案里的两个版本捞回来 ——
    # 否则弹窗只能写"服务器版本：未知"，而那句话明明就在字符串里。
    # 认的是 describe_error 里那个括号格式「（服务器 X / 本机 Y）」——
    # 不能只找"服务器"三个字：前面那句"客户端与服务器版本不一致"里也有。
    if not server and not client and "（服务器" in text:
        with contextlib.suppress(Exception):
            after = text.split("（服务器", 1)[1]
            server = after.split("/", 1)[0].strip(" ）")
            client = after.split("本机", 1)[1].split("）", 1)[0].strip()

    return server, client


def is_version_mismatch(line):
    """这一行/这段文字是不是版本门的拒绝（界面据此弹"需要更新"）。"""
    text = str(line or "")

    if ERR_VERSION_MISMATCH in text:
        return True

    # 兜底：describe_error 出来的中文文案里也可能只剩标题。
    return "版本不一致" in text


def describe_error(line):
    """
    Turn a raw server line such as "ERROR NO_HOST" into a friendly message.
    Never raises.
    """
    if not line:
        return "未知错误"

    parts = str(line).strip().split()

    if not parts:
        return "未知错误"

    if parts[0].upper() != LINE_ERROR:
        return str(line)

    code = parts[1].upper() if len(parts) > 1 else ""

    if code == ERR_VERSION_MISMATCH:
        # 版本门：把两边的版本都摊给用户看，否则"不一致"没法排查。
        server, client = version_mismatch_peers(line)
        detail = ""

        if server or client:
            detail = f"（服务器 {server or '?'} / 本机 {client or '?'}）"

        return f"{ERROR_TEXT[code]}{detail} [{code}]"

    if code in ERROR_TEXT:
        return f"{ERROR_TEXT[code]} [{code}]"

    return f"服务器返回错误：{code or line}"


def parse_conn_mode_changed(line):
    """
    Parse a `CONN_MODE_CHANGED` line in BOTH of its forms (PROTOCOL §8.9).

    The relay pushes one line per member control connection **without** the
    `ERROR` prefix, and the same reason wrapped in `ERROR` to every PLAYER /
    HOST_TUNNEL socket still sitting in the waiting pool:

        CONN_MODE_CHANGED <reason> [was_live=0|1]
        ERROR CONN_MODE_CHANGED <reason> [was_live=0|1]

    Returns `(reason, was_live)` where `was_live` is `"0"`, `"1"` or `""`
    (an older relay does not send it), or `None` when this is not such a line.

    `was_live` is a ONE-SHOT fact: "did this member have a live connection
    BEFORE the switch". The `conn=` in the same broadcast is already the
    cleaned-up `idle`, so the decision must read `was_live`, never `conn=`.
    """
    text = str(line or "").strip()

    if not text:
        return None

    parts = text.split()
    index = 0

    if parts[0].upper() == LINE_ERROR:
        index = 1

    if index >= len(parts) or parts[index].upper() != LINE_CONN_MODE_CHANGED:
        return None

    was_live = ""
    reason_tokens = []

    for token in parts[index + 1:]:
        key, _, value = token.partition("=")

        if key == "was_live" and value in ("0", "1"):
            was_live = value
        elif "=" not in token:
            reason_tokens.append(token)

    return " ".join(reason_tokens).strip(), was_live
