"""
The room registry: the ONE object that owns the live rooms.

============================================================================
 WHY THIS IS ITS OWN MODULE
============================================================================

The relay is started as `python relay_server.py`, so its module name is
`__main__` -- not `relay_server`. Any other module that wrote

    from relay_server import rooms

therefore did NOT find the running server. It imported a SECOND copy of
relay_server.py under the name `relay_server`, with its own empty `rooms`
dictionary and (because that copy never runs main()) no server at all.

That copy is created silently -- no error, no warning -- and everything that
used it saw an empty world:

  * the panel's 「强制关闭」 reached revoke_room_by_name(), which looked the
    room up in the empty copy and answered 「房间不存在（可能已经关闭）」 for a
    room that was right there in memory, listed on the very same page;
  * the member watchdog iterated the empty copy, so a member that went silent
    was never reaped and its room stayed locked to a host position nobody held.

A dictionary that two modules must share cannot live in a module that is also
a script. It lives here instead, and both sides import it the normal way, so
there is exactly one registry no matter how the server was started.
"""

import asyncio
import time

# ============================================================
# The registry
# ============================================================

#: room name -> Room
rooms = {}

#: Guards `rooms` and the waiting/session lists inside a Room.
rooms_lock = asyncio.Lock()

#: Presence channels: "this client is open and logged in".
#:
#: key = client id (or the username when the client sent none) ->
#:     {"username": str, "writer": StreamWriter, "since": float}
#:
#: These hold NO game state. They exist so the panel's 在线账号 can mean
#: "actually running right now": a token outlives the app that owns it, but a
#: TCP connection does not.
presences = {}

#: Rooms that were just closed, with the reason: name -> (reason, monotonic ts).
#:
#: A client that was one second too late (or a panel that still had the room on
#: screen) gets "房间已被管理员关闭：<原因>" instead of a bare "房间不存在",
#: which reads like a typo in the room name. Short lived on purpose -- it is a
#: courtesy note, not a permanent record (the audit log is that).
recently_closed = {}

#: How long such a note is kept.
CLOSED_NOTE_TTL = 90.0


def note_closed(room_name, reason=""):
    """Remember that a room was closed, and why."""
    import time

    recently_closed[str(room_name)] = (str(reason or ""), time.monotonic())


def closed_reason(room_name):
    """The reason a recently closed room is gone, or "" when there is none."""
    import time

    entry = recently_closed.get(str(room_name))

    if entry is None:
        return ""

    reason, when = entry

    if time.monotonic() - when > CLOSED_NOTE_TTL:
        recently_closed.pop(str(room_name), None)
        return ""

    return reason or "房间已被管理员关闭"


def forget_closed(room_name):
    """A room with this name exists again: drop the note."""
    recently_closed.pop(str(room_name), None)


# ============================================================
# The room's CONNECTION MODE, remembered across a destroy/rebuild
# ============================================================
#
# The room mode ("relay" | "stun") is an OWNER decision: PROTOCOL §8 / design
# baseline §A say only the owner may change it, and the ONLY wire request that
# may change it is `CONNMODE`.
#
# Why a memory lives here then: a Room object is destroyed together with its
# owner's member connection (baseline §A: the room's lifetime belongs to the
# owner). When the same owner comes straight back -- a blip, a relay restart, a
# stop-and-reopen -- the relay builds a BRAND NEW Room, whose constructor
# default is "relay". That silently rewrote the owner's explicit choice with
# nobody sending CONNMODE, and the clients (which read the room's mode from
# every `HOST_STATE`) were snapped back to the relay.
#
# So the owner's explicit choice is remembered here for a short window, keyed
# by the room token AND the owner's client id: when the SAME owner recreates
# the room, the mode comes back and the rebuild is announced (`mode_reason=
# restored`). A DIFFERENT client creating the same room name gets the plain
# default -- somebody else's choice is not inherited.
#
# This is the ONLY internal path that may produce a mode the owner did not just
# ask for, and it is exactly the "room destroyed and rebuilt" case the baseline
# allows -- with a reason attached to the broadcast.

#: How long the owner's mode survives the room object.
MODE_MEMORY_TTL = 300.0

#: room token -> (mode, owner_client_id, monotonic)
mode_memory = {}


def remember_mode(room_name, mode, owner_key=""):
    """
    Record the OWNER's explicit mode for this room token.

    Called from exactly one place: the `CONNMODE` handler (and the initial
    `CONNMODE` a client sends right after CREATE, which is the same request).
    Nothing else may write a mode -- see room_members.set_room_conn_mode().
    """
    mode = str(mode or "").strip().lower()

    if mode not in ("relay", "stun", "auto"):
        return False

    mode_memory[str(room_name)] = (mode, str(owner_key or ""), time.monotonic())

    return True


def recall_mode(room_name, owner_key=""):
    """
    The remembered mode for this room token, or "".

    A memory that belongs to a DIFFERENT owner is dropped rather than reused:
    a new person creating a room with a recycled name must get the default, not
    the previous owner's transport.
    """
    token = str(room_name)
    entry = mode_memory.get(token)

    if entry is None:
        return ""

    mode, key, when = entry

    if time.monotonic() - when > MODE_MEMORY_TTL:
        mode_memory.pop(token, None)
        return ""

    if key and owner_key and key != owner_key:
        # Somebody else's room name now: never inherit their mode.
        mode_memory.pop(token, None)
        return ""

    return mode


def forget_mode(room_name):
    """Drop the memory for a token (used when a room is closed for good)."""
    mode_memory.pop(str(room_name), None)


# ============================================================
# Why was I removed? (for the reconnect SESSION probe)
# ============================================================
#
# PROTOCOL §9: after a reconnect the client must be able to tell
#     "a blip -- my token is still fine and I can rebuild"
# apart from
#     "I was kicked / the room is gone -- stop and make me log in again".
#
# The relay is the only side that knows, and by the time the client asks, the
# member record is long gone (that is what "the socket died" means). So the
# removal reason is kept here for a short window, keyed by client id.
#
# Reasons are the same ones the member cleanup path already computes:
# leave | drop | kick | timeout | room_closed.

#: How long a removal note is kept (long enough for a client's backoff).
REMOVAL_NOTE_TTL = 180.0

#: client id -> (room token, reason, monotonic)
removal_memory = {}


def note_removal(client_id, room_name, reason):
    """Remember why this client id left / was removed from a room."""
    client_id = str(client_id or "").strip()

    if not client_id:
        return False

    removal_memory[client_id] = (
        str(room_name or ""),
        str(reason or "drop"),
        time.monotonic(),
    )

    return True


def recall_removal(client_id):
    """`(room token, reason)` for this client id, or `("", "")`."""
    import time

    client_id = str(client_id or "").strip()

    if not client_id:
        return "", ""

    entry = removal_memory.get(client_id)

    if entry is None:
        return "", ""

    room_name, reason, when = entry

    if time.monotonic() - when > REMOVAL_NOTE_TTL:
        removal_memory.pop(client_id, None)
        return "", ""

    return room_name, reason


async def find_member(client_id):
    """
    `(room, member)` for a live member connection with this client id.

    "Still in the room" is the relay-side fact that separates a blip (the
    member control connection is still registered right now) from everything
    else. A client id identifies one person across their reconnect, which is
    the same key `add_member` uses.
    """
    client_id = str(client_id or "").strip()

    if not client_id:
        return None, None

    async with rooms_lock:
        for room in rooms.values():
            for member in room.members.values():
                if str(getattr(member, "client_id", "") or "") == client_id:
                    return room, member

    return None, None


# ============================================================
# Logging
# ============================================================
#
# relay_server owns the real logger (it knows about VERBOSE and the timestamps),
# but importing it from here would bring back the second-copy problem above.
# So it hands its logger over instead, and anything that runs before the server
# does falls back to print().

_log_hook = None


def set_logger(function):
    """Called once by relay_server with its own log()."""
    global _log_hook
    _log_hook = function


def log_line(*parts):
    """Log through the server's logger, or print if it is not up yet."""
    if _log_hook is not None:
        try:
            _log_hook(*parts)
            return
        except Exception:
            pass

    print(" ".join(str(part) for part in parts), flush=True)


# ============================================================
# Helpers that must see the same registry
# ============================================================

async def drop_room_if_empty(room_name, room):
    """
    Remove the room from the registry only if it is the same object and
    completely empty. Prevents a stale task from deleting a brand new room
    that re-used the same Room ID.
    """
    async with rooms_lock:
        current = rooms.get(room_name)

        if current is not room:
            return False

        if room.closed:
            rooms.pop(room_name, None)
            return True

        if (
            room.host_writer is None
            and not room.waiting_tunnels
            and not room.waiting_players
            and not room.sessions
            # 还有成员控制连接 = 房间里还有人，绝不是"空房"。少了这一句，
            # 一个 STUN 房间（没有开服连接、没有配对会话）会在成员还坐着的
            # 时候被从注册表里摘掉，下一次 PLAYER/HOST_TUNNEL 握手就会
            # `get_or_create_room` 出一个全新的 Room —— 连接模式随之静默
            # 变回默认的 relay（用户报的"没人操作模式自己跳回中转"）。
            and not room.members
        ):
            rooms.pop(room_name, None)
            return True

    return False


__all__ = [
    "rooms",
    "rooms_lock",
    "presences",
    "recently_closed",
    "note_closed",
    "closed_reason",
    "forget_closed",
    "set_logger",
    "log_line",
    "drop_room_if_empty",
    # 连接模式的"所有者选择"记忆（跨房间销毁/重建）
    "MODE_MEMORY_TTL",
    "mode_memory",
    "remember_mode",
    "recall_mode",
    "forget_mode",
    # 重连判定：为什么我被移除 / 我现在还在不在房间里
    "REMOVAL_NOTE_TTL",
    "removal_memory",
    "note_removal",
    "recall_removal",
    "find_member",
]
