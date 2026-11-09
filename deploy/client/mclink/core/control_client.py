"""
MC-Link client -- V2 control-plane client.

Three independent helpers, each opening ONE short lived socket:

    login()        -> AuthSession(token, username, is_admin, protocol)
    fetch_rooms()  -> list[RoomInfo]
    delete_room()  -> (ok, message)

These are deliberately separate from HostCore / PlayerCore so the frozen data
path is untouched: a login never shares a socket with a Minecraft tunnel, and
each request is a single line plus a reply.

All of them raise `ControlError` with a friendly Chinese message, so the GUI
can show something readable instead of a raw exception.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field

from .common import close_writer, enable_tcp_nodelay
from . import protocol as proto

CONNECT_TIMEOUT = 8
REPLY_TIMEOUT = 12


class ControlError(Exception):
    """A control-plane request failed. The message is user facing."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


# ============================================================
# Data
# ============================================================

@dataclass
class AuthSession:
    token: str = ""
    username: str = ""
    is_admin: bool = False
    #: Three tier role: "root" | "admin" | "player".
    #:
    #: Empty by default ON PURPOSE. An older relay omits `role` from the LOGIN
    #: reply, and defaulting this to "player" would then override a correct
    #: is_admin=1 and label an administrator as a普通玩家. Leaving it empty
    #: lets effective_role() fall back to is_admin.
    role: str = ""
    #: Monthly self-service allowance (改名 + 改密码 共用), as reported by the
    #: relay on the LOGIN line. None means "no limit" (root/admin); None also
    #: means "this relay is older and did not say", which is why the two cases
    #: are told apart by `self_ops_known`.
    #: 服务器允许的单个房间人数上限。客户端自己的设置是"这个房间用几个人"，
    #: 服务器的数字只是天花板：超过了就在建房前拒绝，而不是连上去之后卡死。
    server_max_players: int = 0
    self_ops_left: int = None
    self_ops_limit: int = 0
    self_ops_known: bool = False
    protocol: int = 1
    server: str = ""
    logged_in_at: float = 0.0

    @property
    def ok(self):
        return bool(self.token)

    @property
    def effective_role(self):
        if self.role in ("root", "admin", "player"):
            return self.role

        return "admin" if self.is_admin else "player"

    @property
    def role_label(self):
        return {
            "root": "根管理员",
            "admin": "管理员",
            "player": "玩家",
        }.get(self.effective_role, "玩家")

    @property
    def role_unlimited(self):
        """Staff are not limited by the monthly self-service allowance."""
        return self.effective_role in ("root", "admin")

    def self_ops_text(self):
        """
        One line about the monthly allowance, or "" when nothing is known.

        Shown BEFORE the player tries to rename, because finding out by being
        refused is a worse experience than being told.
        """
        if self.role_unlimited:
            return "改名和改密码不限次数（管理员及以上）"

        if not self.self_ops_known:
            return ""

        if self.self_ops_left is None:
            return ""

        if self.self_ops_left <= 0:
            return "本月的自助修改次数已用完（每月 3 次，改名和改密码共用）"

        return f"本月还可以自助修改 {self.self_ops_left} 次（改名和改密码共用）"

    def summary(self):
        return f"{self.username}（{self.role_label}）"


@dataclass
class RoomInfo:
    #: 房间 ID（room token，ASCII）。加入房间、删除房间用的是它。
    room: str = ""
    #: 玩家看到的房间名。老中继不发 name= 时退回 room。
    name: str = ""
    #: 建房的人（所有者）。房间一建出来就有，不用等谁认领主机。
    owner: str = ""
    #: 当前持有主机身份的人；没人认领时是空的。注意这和 owner 是两回事：
    #: 房主（建房的人）不一定就是主机。
    host: str = ""
    #: 主机位置有人，但那个人的 Minecraft 还没开（连接进不去）。
    host_live: bool = True
    players: int = 0
    max_players: int = 0
    idle_tunnels: int = 0
    waiting_players: int = 0
    #: 房间里的人数（含还没进世界的人）。
    members: int = 0
    state: str = "empty"          # "empty" | "busy"
    raw: str = ""

    @property
    def display(self):
        """界面上显示的房间名。"""
        return self.name or self.room

    @property
    def host_text(self):
        """「当前主机」那一列的文字。"""
        if not self.host:
            return "（还没人认领）"

        return self.host if self.host_live else f"{self.host}（未开服）"

    @property
    def is_empty(self):
        return self.players <= 0

    @property
    def state_text(self):
        """
        「空闲 / 有玩家」 for the room list.

        Based on `state` (which the relay derives from REAL paired players),
        not on `players` -- `players` now counts the host as being in the world,
        so every open room would read 「有玩家」 even when nobody else has
        joined. The question this answers is "is there anybody else to play
        with".
        """
        if self.state:
            return "有玩家" if self.state == "busy" else "空闲"

        return "有玩家" if self.players > 1 else "空闲"

    def players_text(self):
        if self.max_players:
            return f"{self.players} / {self.max_players}"
        return str(self.players)

    def to_dict(self):
        return {
            "room": self.room,
            "host": self.host,
            "players": self.players,
            "max_players": self.max_players,
            "idle_tunnels": self.idle_tunnels,
            "waiting_players": self.waiting_players,
            "state": self.state,
        }

    @staticmethod
    def from_dict(data):
        room = RoomInfo()
        for key, value in (data or {}).items():
            if hasattr(room, key):
                setattr(room, key, value)
        return room


# ============================================================
# Low level request
# ============================================================

async def _request(host, port, line, read_until_done=False,
                   connect_timeout=None, reply_timeout=None):
    """
    Open a socket, send one line, read the reply.

    Returns a list of reply lines. Raises ControlError on any failure.

    `connect_timeout` / `reply_timeout` are ADDITIVE (default = the module
    constants). The SESSION probe passes a much shorter budget: it runs on the
    reconnect path, where a slow answer is worse than no answer -- an old or
    unreachable relay must not delay the existing flow.
    """
    if connect_timeout is None:
        connect_timeout = CONNECT_TIMEOUT

    if reply_timeout is None:
        reply_timeout = REPLY_TIMEOUT

    reader = None
    writer = None

    try:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, int(port)),
                timeout=connect_timeout,
            )
        except asyncio.TimeoutError:
            raise ControlError(
                f"连接中转服务器超时（{host}:{port}），请检查网络和服务器状态"
            )
        except OSError as e:
            raise ControlError(f"无法连接中转服务器（{host}:{port}）：{e}")

        enable_tcp_nodelay(writer)

        writer.write((line + "\n").encode())
        await writer.drain()

        lines = []

        while True:
            try:
                data = await asyncio.wait_for(
                    reader.readline(), timeout=reply_timeout
                )
            except asyncio.TimeoutError:
                raise ControlError("服务器没有响应，请稍后再试")

            if not data:
                break

            text = data.decode("utf-8", errors="replace").strip()

            if not text:
                continue

            lines.append(text)

            if not read_until_done:
                break

            if text == proto.LINE_DONE:
                break

        if not lines:
            raise ControlError("服务器没有返回任何内容")

        return lines

    finally:
        await close_writer(writer)


def _error_from(lines):
    """If the reply is an ERROR line, return the code."""
    if not lines:
        return ""

    first = lines[0]

    if first.upper().startswith(proto.LINE_ERROR):
        parts = first.split()
        return parts[1].upper() if len(parts) > 1 else ""

    return ""


def _raise_for_error(code):
    raise ControlError(proto.describe_error(f"{proto.LINE_ERROR} {code}"), code)


# ============================================================
# LOGIN
# ============================================================

async def login(host, port, username, password, client_id=""):
    """
    Authenticate against the relay.

    The password travels on this dedicated socket only; it is never sent on a
    HOST or PLAYER connection and never mixed with Minecraft data.
    """
    username = (username or "").strip()
    password = password or ""

    if not username:
        raise ControlError("请输入用户名", "AUTH_BAD_REQUEST")

    if not password:
        raise ControlError("请输入密码", "AUTH_BAD_REQUEST")

    if any(c.isspace() for c in username) or any(c.isspace() for c in password):
        # The wire format is space separated; a space cannot be transmitted.
        raise ControlError("用户名和密码不能包含空格", "AUTH_BAD_REQUEST")

    if len(username) > proto.MAX_USERNAME_LEN:
        raise ControlError(
            f"用户名最长 {proto.MAX_USERNAME_LEN} 个字符", "AUTH_BAD_REQUEST"
        )

    if len(password) > 128:
        raise ControlError("密码最长 128 个字符", "AUTH_BAD_REQUEST")

    line = (
        f"{proto.ROLE_LOGIN} {proto.SENTINEL} "
        f"{username} {password} {client_id or '-'}"
    )

    lines = await _request(host, port, line)
    code = _error_from(lines)

    if code:
        _raise_for_error(code)

    first = lines[0]

    if not first.startswith(proto.LINE_ACK_LOGIN):
        raise ControlError(f"服务器应答异常：{first}")

    session = AuthSession(
        username=username,
        server=f"{host}:{port}",
        logged_in_at=asyncio.get_event_loop().time(),
    )

    for token in first.split()[2:]:
        key, _, value = token.partition("=")

        if key == "user":
            session.username = value
        elif key == "admin":
            session.is_admin = value not in ("0", "")
        elif key == "role":
            session.role = value or session.role
        elif key == "max_players":
            with contextlib.suppress(ValueError):
                session.server_max_players = int(value)
        elif key == "self_ops_left":
            session.self_ops_known = True

            if value == "unlimited":
                session.self_ops_left = None
            else:
                with contextlib.suppress(ValueError):
                    session.self_ops_left = int(value)
        elif key == "self_ops_limit":
            with contextlib.suppress(ValueError):
                session.self_ops_limit = int(value)
        elif key == "protocol":
            with contextlib.suppress(ValueError):
                session.protocol = int(value)
        elif key == "token":
            session.token = value

    if not session.token:
        raise ControlError("服务器没有返回登录凭证（可能是旧版服务端）")

    return session


# ============================================================
# ROOMS
# ============================================================

async def fetch_rooms(host, port, client_id="", username=""):
    """
    Return the list of rooms currently online.

    The ROOMS header also carries the relay's live "must log in" switch as
    `auth=0|1`. It is the only unauthenticated request, so it doubles as the
    way for a client to find out whether it needs an account BEFORE it
    connects -- see `relay_requires_account()`.
    """
    line = (
        f"{proto.ROLE_ROOMS} {proto.SENTINEL} "
        f"{client_id or '-'} {username or '-'}"
    )

    lines = await _request(host, port, line, read_until_done=True)
    code = _error_from(lines)

    if code:
        _raise_for_error(code)

    rooms = []

    for text in lines:
        if not text.startswith(proto.LINE_ROOM + " "):
            continue

        parts = text.split()

        if len(parts) < 6:
            continue

        info = RoomInfo(room=parts[1], owner=parts[2], raw=text)

        with contextlib.suppress(ValueError):
            players, _, maximum = parts[3].partition("/")
            info.players = int(players)
            info.max_players = int(maximum or 0)

        with contextlib.suppress(ValueError):
            info.idle_tunnels = int(parts[4])

        with contextlib.suppress(ValueError):
            info.waiting_players = int(parts[5])

        info.state = parts[6] if len(parts) > 6 else (
            "busy" if info.players > 0 else "empty"
        )

        # 后面是新增字段（老服务器不发，老客户端忽略）：
        #   host=<当前主机>  members=<房间内人数>  name=<房间名>  host_live=0/1
        for item in parts[7:]:
            key, _, value = item.partition("=")

            if key == "host":
                info.host = "" if value in ("", "-") else value
            elif key == "members":
                with contextlib.suppress(ValueError):
                    info.members = int(value)
            elif key == "name":
                info.name = proto.room_name_from_token(value)
            elif key == "host_live":
                info.host_live = value != "0"

        rooms.append(info)

    rooms.sort(key=lambda r: (r.players, r.room), reverse=True)

    return rooms


def parse_auth_flag(lines):
    """
    Pull `auth=0|1` out of a ROOMS header line.

    Returns True, False, or None when the relay did not say. None matters: a
    relay older than this field must not be read as "no login needed".
    """
    for text in lines or []:
        if not text.startswith(proto.LINE_ROOMS):
            continue

        for token in text.split()[1:]:
            key, _, value = token.partition("=")

            if key == "auth":
                return value.strip() in ("1", "true", "True", "yes")

        return None

    return None


async def relay_requires_account(host, port, client_id="", timeout=None):
    """
    Ask the relay whether an account is needed, without logging in.

    Returns True / False, or None when the relay could not be reached or is
    too old to answer. The caller treats None as "assume it does" so an old
    relay keeps working exactly as before.
    """
    line = (
        f"{proto.ROLE_ROOMS} {proto.SENTINEL} {client_id or '-'} -"
    )

    try:
        if timeout is None:
            lines = await _request(host, port, line, read_until_done=True)
        else:
            lines = await asyncio.wait_for(
                _request(host, port, line, read_until_done=True),
                timeout=timeout,
            )
    except (ControlError, asyncio.TimeoutError, TimeoutError, OSError):
        return None

    if _error_from(lines):
        return None

    return parse_auth_flag(lines)


# ============================================================
# SESSION —— 重连前的只读身份探活（PROTOCOL §8.10）
# ============================================================

#: The probe's whole budget (connect + reply). Deliberately short: it runs on
#: the disconnect path, and "no answer" already has the correct fallback (the
#: pre-existing behaviour). A slow probe must never look like a hang.
SESSION_PROBE_TIMEOUT = 2.0

#: `reason` values that mean "this was a real blip" -> auto-reconnect is OK.
SESSION_REASONS_RECONNECT = ("STILL_IN_ROOM", "DROPPED", "NOT_IN_ROOM")

#: `reason` values that mean "my room identity is over" -> leave, do not
#: auto-reconnect. `AUTH_*` also lands here (and sets `relogin=1`).
SESSION_REASONS_LEAVE = ("KICKED", "LEFT", "TIMEOUT", "ROOM_CLOSED")


@dataclass
class SessionProbe:
    """
    The parsed answer to `SESSION __mclink__ ...` (PROTOCOL §8.10).

    `ok` is False whenever a verdict cannot be trusted -- unreachable relay,
    timeout, a garbled line, or an old relay answering `ERROR UNKNOWN_ROLE`
    (`old_relay=True`). The caller must then fall back to exactly the previous
    behaviour; this object never carries a half-parsed "maybe".
    """

    ok: bool = False
    old_relay: bool = False
    valid: bool = False
    in_room: bool = False
    token_kind: str = "none"
    room: str = ""
    name: str = ""
    reason: str = ""
    #: None = the relay did not say (older relay); True/False = it did.
    auto_reconnect: bool = None
    relogin: bool = False
    raw: str = ""

    @property
    def verdict(self):
        """`"reconnect"` / `"leave"` / `"unknown"` (see session_probe_verdict)."""
        return session_probe_verdict(self)


def session_reason_auto_reconnect(reason):
    """
    Map a `reason` CODE onto "may auto-reconnect?" without the boolean field.

    Returns True / False / None. None = not a code we know -> the caller must
    NOT guess (fall back to the old behaviour).
    """
    code = str(reason or "").strip().upper()

    if not code:
        return None

    if code in SESSION_REASONS_RECONNECT:
        return True

    if code in SESSION_REASONS_LEAVE or code.startswith("AUTH"):
        return False

    return None


def session_probe_verdict(probe):
    """
    The decision the reconnect path makes from a probe: reconnect / leave / unknown.

    * `"reconnect"` -- STILL_IN_ROOM / DROPPED / NOT_IN_ROOM -> rebuild.
    * `"leave"`     -- KICKED / LEFT / TIMEOUT / ROOM_CLOSED / AUTH_* ->
      treat it as leaving the room (no auto-reconnect; relogin if told to).
    * `"unknown"`   -- no usable answer (unreachable / timeout / garbled /
      `ERROR UNKNOWN_ROLE` from an old relay) -> caller keeps its old rules.

    `auto_reconnect` from the relay wins when present; the reason table is the
    tolerance path for a relay that omitted the boolean.
    """
    if probe is None or not getattr(probe, "ok", False):
        return "unknown"

    value = getattr(probe, "auto_reconnect", None)

    if value is True:
        return "reconnect"

    if value is False:
        return "leave"

    derived = session_reason_auto_reconnect(getattr(probe, "reason", ""))

    if derived is True:
        return "reconnect"

    if derived is False:
        return "leave"

    return "unknown"


def parse_session_probe(lines):
    """
    Parse the reply lines of a SESSION probe into a `SessionProbe`.

    Never raises. A reply that is not `OK SESSION ...` comes back with
    `ok=False`; `ERROR UNKNOWN_ROLE` is flagged as `old_relay` so the caller
    can tell "this relay is older" from "the network failed".
    """
    if not lines:
        return SessionProbe(ok=False)

    first = str(lines[0] or "").strip()
    code = _error_from(lines)

    if code:
        return SessionProbe(
            ok=False, old_relay=(code == proto.ERR_UNKNOWN_ROLE), raw=first
        )

    if not first.startswith(proto.LINE_ACK_SESSION):
        return SessionProbe(ok=False, raw=first)

    probe = SessionProbe(ok=True, raw=first)
    saw_auto = False

    for token in first.split()[2:]:
        key, _, value = token.partition("=")
        lowered = value.strip().lower()

        if key == "valid":
            probe.valid = lowered in ("1", "true", "yes")
        elif key == "in_room":
            probe.in_room = lowered in ("1", "true", "yes")
        elif key == "token":
            probe.token_kind = lowered or "none"
        elif key == "room":
            probe.room = "" if value in ("", "-") else value
        elif key == "name":
            probe.name = (
                "" if value in ("", "-") else proto.room_name_from_token(value)
            )
        elif key == "reason":
            probe.reason = value.strip().upper()
        elif key == "auto_reconnect":
            saw_auto = True
            probe.auto_reconnect = lowered in ("1", "true", "yes")
        elif key == "relogin":
            probe.relogin = lowered in ("1", "true", "yes")

    if not saw_auto:
        probe.auto_reconnect = session_reason_auto_reconnect(probe.reason)

    return probe


async def probe_session(host, port, client_id="", username="", token="",
                        timeout=SESSION_PROBE_TIMEOUT):
    """
    Ask the relay whether OUR login / room identity is still alive.

    One short-lived, READ-ONLY connection to the sentinel room; it creates
    nothing and changes nothing. NEVER raises -- any failure comes back as a
    `SessionProbe` with `ok=False`, so the reconnect path keeps its old rules.
    """
    line = (
        f"{proto.ROLE_SESSION} {proto.SENTINEL} "
        f"{client_id or '-'} {username or '-'} {token or '-'}"
    )

    try:
        lines = await asyncio.wait_for(
            _request(
                host,
                port,
                line,
                connect_timeout=timeout,
                reply_timeout=timeout,
            ),
            timeout=timeout,
        )
    except (ControlError, asyncio.TimeoutError, TimeoutError, OSError):
        return SessionProbe(ok=False)
    except Exception:  # noqa: BLE001 -- a probe must never break the caller
        return SessionProbe(ok=False)

    return parse_session_probe(lines)


# ============================================================
# DELETE
# ============================================================

async def delete_room(host, port, room, session, client_id=""):
    """
    Delete one of MY OWN rooms.

    The client should NOT offer this for a room that still has players; the
    server refuses it anyway with DELETE_ROOM_BUSY. That is intentional: the UI
    is a convenience, the server is the authority.
    """
    room = (room or "").strip()

    if not room:
        raise ControlError("房间名为空", "BAD_ROOM")

    token = session.token if session else ""

    if not token:
        raise ControlError("请先登录", "AUTH_REQUIRED")

    username = (session.username if session else "") or "-"

    # 协议里传的是 room token（房间名 percent-encode 之后的样子）：
    # 房间名可以是中文或带空格，直接塞进这一行会把协议切断。
    line = (
        f"{proto.ROLE_DELETE} {proto.SENTINEL} "
        f"{client_id or '-'} {username} {token} {proto.room_token(room)}"
    )

    lines = await _request(host, port, line)
    code = _error_from(lines)

    if code:
        _raise_for_error(code)

    first = lines[0]

    if not first.startswith(proto.LINE_ACK_DELETE):
        raise ControlError(f"服务器应答异常：{first}")

    return True, f"房间 {room} 已删除"


# ============================================================
# RENAME
# ============================================================

async def rename_account(host, port, new_username, session, client_id=""):
    """
    Rename MY OWN account (self-service).

    The token in `session` decides which account is renamed -- the username is
    only carried along for the log, so a client can never rename somebody else.

    The server rate limits this path (3 per calendar month, SHARED with
    self-service password changes, players only) and it does NOT log the client
    out: the caller only has to adopt the returned name.

    Returns (new_username, message, session) -- the session is updated in place
    with the remaining allowance so the UI can show it right away.
    """
    new_username = (new_username or "").strip()

    if not new_username:
        raise ControlError("请输入新的用户名", proto.ERR_BAD_USERNAME)

    if len(new_username) < 2:
        raise ControlError("用户名至少 2 个字符", proto.ERR_BAD_USERNAME)

    if len(new_username) > proto.MAX_USERNAME_LEN:
        raise ControlError(
            f"用户名最长 {proto.MAX_USERNAME_LEN} 个字符", proto.ERR_BAD_USERNAME
        )

    if any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in new_username):
        # The wire format is space separated, so a space cannot be transmitted.
        raise ControlError(
            "用户名不能包含空格或特殊字符", proto.ERR_BAD_USERNAME
        )

    token = session.token if session else ""

    if not token:
        raise ControlError("请先登录账号再改名", "AUTH_REQUIRED")

    old_username = (session.username if session else "") or "-"

    if new_username == old_username:
        raise ControlError("新用户名和当前用户名相同", proto.ERR_BAD_USERNAME)

    line = (
        f"{proto.ROLE_RENAME} {proto.SENTINEL} "
        f"{client_id or '-'} {old_username} {token} {new_username}"
    )

    lines = await _request(host, port, line)
    code = _error_from(lines)

    if code:
        _raise_for_error(code)

    first = lines[0]

    if not first.startswith(proto.LINE_ACK_RENAME):
        raise ControlError(f"服务器应答异常：{first}")

    confirmed = new_username

    for item in first.split()[2:]:
        key, _, value = item.partition("=")

        if key == "user" and value:
            confirmed = value
        elif key == "remaining" and session is not None:
            session.self_ops_known = True

            if value == "unlimited":
                session.self_ops_left = None
            else:
                with contextlib.suppress(ValueError):
                    session.self_ops_left = int(value)
        elif key == "limit" and session is not None:
            with contextlib.suppress(ValueError):
                session.self_ops_limit = int(value)

    if session is not None:
        session.username = confirmed

    tail = ""

    if session is not None and session.self_ops_known and not session.role_unlimited:
        if session.self_ops_left is not None:
            tail = f"（本月还剩 {session.self_ops_left} 次自助修改）"

    return confirmed, f"已改名为 {confirmed}，立即生效（无需重新登录）{tail}"


# ============================================================
# LOGOUT
# ============================================================

async def logout(host, port, session, client_id=""):
    """
    Release the session token on the relay.

    Called when the client exits or the user logs out. Without it the relay
    keeps the token alive for its full TTL, so the panel shows the account as
    online long after the person went to bed.

    Never raises: this runs on the way out, and a failure to say goodbye is
    not something the user can act on. The token expires on its own anyway.
    Returns True when the relay confirmed.
    """
    token = session.token if session else ""

    if not token:
        return False

    username = (session.username if session else "") or "-"

    line = (
        f"{proto.ROLE_LOGOUT} {proto.SENTINEL} "
        f"{client_id or '-'} {username} {token}"
    )

    try:
        lines = await _request(host, port, line)
    except ControlError:
        return False

    code = _error_from(lines)

    if code:
        return False

    return bool(lines) and lines[0].startswith(proto.LINE_ACK_LOGOUT)


# ============================================================
# HOST control-channel delete (no extra socket)
# ============================================================

def host_delete_line(session):
    """The line a host sends on its CONTROL socket to delete its own room."""
    token = session.token if session else ""
    return f"{proto.LINE_DELETE} {token}"
