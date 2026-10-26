"""AUTO relay attachment lifetime, deliberately opaque to game frames."""
import asyncio
import contextlib
import hmac
import json
import secrets
import time
import uuid
from types import SimpleNamespace

CAPABILITY = "auto_stream_v1"
STATE_TTL = 10.0
DETACHED_TTL = 45.0
MAX_STREAMS_PER_MEMBER = 16
MAX_STREAMS_PER_ROOM = 256


def extras(line):
    return dict(p.split("=", 1) for p in line.split()[2:] if "=" in p)


def member_for(room, client_id):
    matches = [m for m in room.members.values()
               if m.client_id == client_id and not m.closed
               and not getattr(m, "detached", False)]
    return matches[0] if len(matches) == 1 else None


def supports(member):
    return member is not None and CAPABILITY in getattr(member, "capabilities", set())


class Attachment:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.done = asyncio.Event()

    def close(self):
        self.writer.close()
        self.done.set()


class AutoBridge:
    def __init__(self, room, pair, on_close, on_event=None):
        self.room, self.pair, self.on_close = room, pair, on_close
        self.on_event = on_event or (lambda *a, **kw: None)
        self.host_id = room.host_id
        self.generation = room.auto_generation
        self.keys = {side: secrets.token_urlsafe(32) for side in ("host", "player")}
        self.ends = {}
        for side, reader, writer in (("host", pair.tunnel_reader, pair.tunnel_writer),
                                     ("player", pair.player_reader, pair.player_writer)):
            if writer is not None:
                self.ends[side] = Attachment(reader, writer)
        self.changed = asyncio.Event()
        self.closed = False
        self.last_seen = time.monotonic()
        self.reports = {}
        self.finished = set()

    def handshake(self, side, word):
        return (f"{word} {self.pair.session_id} proto={CAPABILITY} "
                f"host={self.host_id} player={self.pair.player_id} "
                f"generation={self.generation} resume={self.keys[side]} "
                f"player_ip={self.pair.player_ip or '-'} "
                f"host_ip={self.room.host_ip or '-'} "
                f"channel={getattr(self.pair, 'channel', 'data')}\n").encode()

    def authorized(self, side, client_id, token, key, generation):
        if side not in self.keys or self.closed or self.room.closed or self.room.closing:
            return False
        if self.room.auto_generation != self.generation or generation != self.generation:
            return False
        expected = self.host_id if side == "host" else self.pair.player_id
        member = member_for(self.room, client_id)
        return bool(client_id == expected and supports(member)
                    and (token not in ("", "-") and token in (getattr(member, "token", ""),
                                             getattr(member, "guest_token", "")))
                    and hmac.compare_digest(key, self.keys[side]))

    def attach(self, side, reader, writer):
        old = self.ends.get(side)
        if old:
            old.close()
        attachment = Attachment(reader, writer)
        self.ends[side] = attachment
        self.last_seen = time.monotonic()
        self.changed.set()
        return attachment

    async def _pump(self, source, target, side):
        while True:
            data = await source.reader.read(65536)
            if not data:
                return
            target.writer.write(data)
            await target.writer.drain()
            if side == "host":
                self.pair.tunnel_bytes += len(data)
            else:
                self.pair.player_bytes += len(data)

    async def run(self):
        try:
            while not self.closed:
                self.changed.clear()
                host, player = self.ends.get("host"), self.ends.get("player")
                if host is None or player is None:
                    if time.monotonic() - self.last_seen > DETACHED_TTL:
                        return
                    try:
                        await asyncio.wait_for(self.changed.wait(), 1)
                    except asyncio.TimeoutError:
                        pass
                    continue
                pumps = [asyncio.create_task(self._pump(host, player, "host")),
                         asyncio.create_task(self._pump(player, host, "player"))]
                changed = asyncio.create_task(self.changed.wait())
                try:
                    await asyncio.wait(pumps + [changed], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in pumps + [changed]:
                        task.cancel()
                    await asyncio.gather(*pumps, changed, return_exceptions=True)
                    # Compare identities: an obsolete pump must not close a
                    # newly authenticated replacement attachment.
                    for side, attachment in (("host", host), ("player", player)):
                        attachment.close()
                        if self.ends.get(side) is attachment:
                            self.ends.pop(side, None)
        finally:
            self.close()
            await self.on_close(self.pair.session_id, reason="AUTO session ended")

    def close(self):
        self.closed = True
        self.changed.set()
        for end in list(self.ends.values()):
            end.close()
        self.ends.clear()

    def report(self, member, state):
        side = "host" if member.client_id == self.host_id else "player"
        expected = self.host_id if side == "host" else self.pair.player_id
        if member.client_id != expected or state.get("generation") != self.generation:
            return False
        allowed = {"active_path", "relay_ready", "direct_ready", "switch_phase",
                   "last_reason", "direct_endpoint", "relay_rtt_ms", "direct_rtt_ms", "epoch"}
        clean = {k: v for k, v in state.items() if k in allowed}
        if clean.get("active_path") not in ("relay", "direct", "reconnecting", "none"):
            return False
        if not isinstance(clean.get("epoch", 0), int):
            return False
        for key in ("relay_rtt_ms", "direct_rtt_ms"):
            value = clean.get(key)
            if value is not None and (not isinstance(value, (int, float)) or not 0 <= value <= 600000):
                return False
        for key in ("direct_endpoint", "last_reason", "switch_phase"):
            clean[key] = str(clean.get(key, ""))[:200]
        previous = self.reports.get(side, {})
        clean["updated_at"] = time.time()
        self.reports[side] = clean
        self.last_seen = time.monotonic()
        if clean["active_path"] == "none":
            self.finished.add(side)
        if (previous.get("active_path"), previous.get("switch_phase")) != (
                clean["active_path"], clean["switch_phase"]):
            self.on_event("auto.path.changed", room=self.room.name,
                          session=self.pair.session_id, player=self.pair.player_id,
                          side=side, **clean)
        # A logical stream with an ended endpoint cannot be resumed. Waiting
        # for the other report would leak a pair if RESET was lost on both
        # paths. FIN is reported ended only after both byte directions finish.
        if self.finished:
            self.close()
        return True

    def snapshot(self):
        now = time.time()
        host, player = self.reports.get("host", {}), self.reports.get("player", {})
        fresh = bool(host and player and now - host.get("updated_at", 0) <= STATE_TTL
                     and now - player.get("updated_at", 0) <= STATE_TTL)
        same = fresh and host.get("active_path") == player.get("active_path")
        path = host.get("active_path") if same else ("transitioning" if fresh else "unknown")
        return {"room_mode": "auto", "active_path": path,
                "relay_ready": bool(fresh and host.get("relay_ready") and player.get("relay_ready")),
                "direct_ready": bool(fresh and host.get("direct_ready") and player.get("direct_ready")),
                "switch_phase": host.get("switch_phase", "unknown") if fresh else "unknown",
                "last_reason": host.get("last_reason", "") if fresh else "状态未上报或已过期",
                "updated_at": min(host.get("updated_at", 0), player.get("updated_at", 0)),
                "public_ip": self.pair.player_ip,
                "direct_endpoint": host.get("direct_endpoint", "") if fresh else "",
                "direct_rtt_ms": host.get("direct_rtt_ms") if fresh else None,
                "relay_rtt_ms": host.get("relay_rtt_ms") if fresh else None,
                "stale": not fresh}


async def receive_report(room, member, text):
    try:
        state = json.loads(text.partition(" ")[2])
        if not isinstance(state, dict):
            return
        bridge = find_bridge(room, state.get("session_id"))
        if bridge and bridge.report(member, state):
            room.mark_dirty()
    except (ValueError, TypeError, KeyError):
        return


def member_route(room, client_id):
    pairs = [p for p in room.sessions.values() if p.player_id == client_id and p.established]
    if not pairs:
        network = [b for b in getattr(room, "auto_networks", {}).values()
                   if b.pair.player_id == client_id and not b.closed]
        if network:
            return dict(network[0].snapshot(), channel="network")
        return {"room_mode": room.conn_mode, "active_path": "none"}
    routes = [p.auto.snapshot() if getattr(p, "auto", None) else
              {"room_mode": room.conn_mode, "active_path": "relay" if room.conn_mode == "relay" else "unknown"}
              for p in pairs]
    result = dict(routes[0])
    if len({r["active_path"] for r in routes}) > 1:
        result["active_path"] = "mixed"
    if room.conn_mode == "auto":
        result["relay_ready"] = all(r.get("relay_ready") for r in routes)
        result["direct_ready"] = all(r.get("direct_ready") for r in routes)
        result["stale"] = any(r.get("stale") for r in routes)
    return result


def find_bridge(room, session):
    if room is None:
        return None
    pair = room.sessions.get(session)
    return getattr(pair, "auto", None) or getattr(room, "auto_networks", {}).get(session)


async def handle_network(room, reader, writer, client_id, token, options):
    """One authenticated network-only bridge per Host generation and Player.

    Independent of Room.sessions, idle Host workers, application ports and game
    counts. Existing game-stream pairing/forwarding is not involved.
    """
    side, peer = options.get("side"), options.get("peer")
    member = member_for(room, client_id) if room else None
    host_id = getattr(room, "host_id", "")
    player_id = peer if side == "host" else client_id
    allowed = bool(room and room.conn_mode == "auto" and not room.closed and not room.closing
                   and host_id and supports(member) and supports(member_for(room, peer))
                   and token not in ("", "-")
                   and token in (getattr(member, "token", ""), getattr(member, "guest_token", ""))
                   and CAPABILITY in options.get("caps", "").split(",")
                   and ((side == "host" and client_id == host_id and peer != host_id)
                        or (side == "player" and peer == host_id and client_id != host_id)))
    if not allowed:
        writer.write(b"ERROR AUTO_NETWORK_DENIED\n")
        await writer.drain()
        return
    if not hasattr(room, "auto_networks"):
        room.auto_networks = {}
    registry = room.auto_networks
    bridge = next((b for b in registry.values() if not b.closed
                   and b.host_id == host_id and b.generation == room.auto_generation
                   and b.pair.player_id == player_id), None)
    if bridge is None:
        if len(registry) >= MAX_STREAMS_PER_ROOM:
            writer.write(b"ERROR AUTO_NETWORK_LIMIT\n")
            await writer.drain()
            return
        sid = uuid.uuid4().hex
        player = member_for(room, player_id)
        pair = SimpleNamespace(session_id=sid, player_id=player_id,
            player_ip=getattr(player, "peer_ip", "") or getattr(player, "ip", ""),
            tunnel_reader=None, tunnel_writer=None, player_reader=None, player_writer=None,
            player_bytes=0, tunnel_bytes=0, channel="network")
        # Member.peer_ip varies between older server fixtures; take the actual
        # membership socket address if available, never the relay's own IP.
        with contextlib.suppress(Exception):
            pair.player_ip = player.writer.get_extra_info("peername")[0]
        async def removed(session, reason=""):
            room.total_player_bytes += bridge.pair.player_bytes
            room.total_tunnel_bytes += bridge.pair.tunnel_bytes
            registry.pop(session, None)
            room.mark_dirty()
        bridge = AutoBridge(room, pair, removed)
        registry[sid] = bridge

        async def lifetime():
            task = asyncio.create_task(bridge.run())
            try:
                while not task.done():
                    if (room.closed or room.closing or room.conn_mode != "auto"
                            or room.auto_generation != bridge.generation
                            or room.host_id != bridge.host_id
                            or not member_for(room, bridge.host_id)
                            or not member_for(room, player_id)):
                        bridge.close()
                    await asyncio.sleep(.5)
            finally:
                bridge.close()
                await task
        bridge.network_task = asyncio.create_task(lifetime())
    writer.write(bridge.handshake(side, "CONNECTED_AUTO_NETWORK"))
    await writer.drain()
    attachment = bridge.attach(side, reader, writer)
    room.mark_dirty()
    await attachment.done.wait()
