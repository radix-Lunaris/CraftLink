"""Authenticated datagram forwarding on the relay's public UDP port."""
import asyncio
import secrets
import time
from dataclasses import dataclass, field

from auto_relay import member_for

MAGIC = b"CL1U"
LEASE = 8
MAX_PACKET = 65507


@dataclass
class Attachment:
    key: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    addr: tuple = None
    seen: float = 0
    challenge: tuple = None


class DatagramRelay(asyncio.DatagramProtocol):
    def __init__(self):
        self.pairs = {}
        self.routes = {}
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, packet, addr):
        if not 21 <= len(packet) <= MAX_PACKET or packet[:4] != MAGIC:
            return
        route = self.routes.get(packet[4:20])
        if route is None:
            return
        pair, side = route
        source = pair[side]
        target = pair.get(1 - side)
        now = time.monotonic()
        if len(packet) == 37 and packet[20] == 0:
            if addr == source.addr and now - source.seen <= LEASE:
                source.seen = now
                self.transport.sendto(MAGIC + source.key + b"\x00", addr)
            else:
                nonce = secrets.token_bytes(16)
                source.challenge = addr, nonce, now
                self.transport.sendto(MAGIC + source.key + b"\x02" + nonce, addr)
        elif len(packet) == 37 and packet[20] == 2 and source.challenge:
            wanted_addr, nonce, sent = source.challenge
            if wanted_addr == addr and now - sent <= LEASE and secrets.compare_digest(nonce, packet[21:]):
                source.addr, source.seen = addr, now
                source.challenge = None
                self.transport.sendto(MAGIC + source.key + b"\x00", addr)
        elif (len(packet) >= 25 and packet[20] == 1 and source.addr == addr
              and now - source.seen <= LEASE and target and now - target.seen <= LEASE):
            self.transport.sendto(MAGIC + target.key + packet[20:], target.addr)

    async def attach(self, room, reader, writer, client_id, token, options):
        side = options.get("side")
        member = member_for(room, client_id) if room else None
        peer = member_for(room, options.get("peer")) if room else None
        if side == "host":
            host, player, index = member, peer, 0
        else:
            host, player, index = peer, member, 1
        if (not room or room.closed or room.closing or not room.owner
                or side not in ("host", "player") or not host or not player
                or host is player or room.host_member is not host
                or token in ("", "-") or token not in (member.token, member.guest_token)):
            writer.write(b"ERROR UDP_ATTACHMENT_DENIED\n")
            await writer.drain()
            return
        identity = room, host, player, room.conn_mode
        pair = self.pairs.setdefault(identity, {"direct": secrets.token_bytes(16)})
        old = pair.get(index)
        if old:
            self.routes.pop(old.key, None)
        attachment = pair[index] = Attachment()
        self.routes[attachment.key] = pair, index
        try:
            writer.write(f"OK UDP {attachment.key.hex()} {pair['direct'].hex()}\n".encode())
            await writer.drain()
            while (pair.get(index) is attachment and not room.closed and not room.closing
                   and room.host_member is host and room.conn_mode == identity[3]
                   and member_for(room, host.client_id) is host
                   and member_for(room, player.client_id) is player):
                try:
                    await asyncio.wait_for(reader.read(1), 1)
                    break
                except asyncio.TimeoutError:
                    pass
        finally:
            self.routes.pop(attachment.key, None)
            if pair.get(index) is attachment:
                pair.pop(index)
            if len(pair) == 1 and self.pairs.get(identity) is pair:
                self.pairs.pop(identity)


relay = DatagramRelay()
