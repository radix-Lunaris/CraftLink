"""Forward application datagrams over a punched link or an authenticated relay."""
import asyncio
import secrets
import struct
import threading
import time

from ..config.settings import APP_VERSION
from . import protocol as proto
from .common import close_writer

RELAY = b"CL1U"
DIRECT = b"CL1D"
MAX_PAYLOAD = 65507 - 25
FLOW_IDLE = 120
MAX_FLOWS = 32


class Receiver(asyncio.DatagramProtocol):
    def __init__(self, receive):
        self.receive = receive

    def datagram_received(self, data, addr):
        self.receive(data, addr)


class Peer:
    def __init__(self, forwarder, peer_id, mode):
        self.owner = forwarder
        self.peer_id = peer_id
        self.mode = mode
        self.relay = self.carrier = None
        self.relay_seen = self.direct_seen = 0
        self.challenge = b""
        self.backends = {}
        self.pending = {}
        self.direct_slots = threading.BoundedSemaphore(128)

    def send(self, flow, payload):
        if len(payload) > MAX_PAYLOAD:
            return
        frame = b"\x01" + struct.pack("!I", flow) + payload
        now = time.monotonic()
        if self.carrier and self.direct_seen and now - self.direct_seen < 4 and self.send_direct(frame):
            return
        if self.mode != "stun" and self.relay and self.relay_seen and now - self.relay_seen < 7:
            self.relay.sendto(RELAY + self.relay_key + frame)

    def send_direct(self, frame):
        carrier = self.carrier
        if carrier is None or carrier.stop.is_set():
            return False
        try:
            carrier.sock.sendto(DIRECT + self.direct_key + frame, carrier.tunnel.send_to)
            return True
        except OSError:
            return False

    def direct_receiver(self, carrier, loop):
        def receive(packet, addr):
            tunnel = carrier.tunnel
            if (tunnel and packet[:20] == DIRECT + self.direct_key and addr == tunnel.send_to
                    and self.direct_slots.acquire(blocking=False)):
                try:
                    loop.call_soon_threadsafe(self.receive_direct, carrier, packet[20:])
                except RuntimeError:
                    self.direct_slots.release()
        return receive

    def receive_direct(self, carrier, frame):
        self.direct_slots.release()
        if carrier is not self.carrier or not frame:
            return
        if frame[0] == 0 and len(frame) == 9:
            self.send_direct(b"\x02" + frame[1:])
        elif frame[0] == 2 and self.challenge and frame[1:] == self.challenge:
            self.direct_seen = time.monotonic()
        elif frame[0] == 1:
            self.deliver(frame)

    def receive_relay(self, packet, _addr):
        if packet[:20] != RELAY + self.relay_key:
            return
        frame = packet[20:]
        if frame == b"\x00":
            self.relay_seen = time.monotonic()
        elif len(frame) == 17 and frame[0] == 2:
            self.relay.sendto(RELAY + self.relay_key + frame)
        elif self.mode != "stun":
            self.deliver(frame)

    def deliver(self, frame):
        if len(frame) < 5 or frame[0] != 1:
            return
        flow = struct.unpack_from("!I", frame, 1)[0]
        payload = frame[5:]
        if not self.owner.is_host:
            slot = self.owner.clients_by_id.get(flow)
            if slot:
                self.owner.listener.sendto(payload, slot[0])
        elif flow in self.backends:
            transport, _ = self.backends[flow]
            self.backends[flow] = transport, time.monotonic()
            transport.sendto(payload)
        elif flow in self.pending:
            task, packets = self.pending[flow]
            if len(packets) < 8:
                packets.append(payload)
        elif len(self.backends) + len(self.pending) < MAX_FLOWS:
            packets = [payload]
            self.pending[flow] = asyncio.create_task(self.open_backend(flow, packets)), packets

    async def open_backend(self, flow, packets):
        core = self.owner.core
        try:
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: Receiver(lambda data, _: self.send(flow, data)),
                remote_addr=(core.mc_host, core.mc_port))
            self.backends[flow] = transport, time.monotonic()
            for data in packets:
                transport.sendto(data)
        except OSError as exc:
            core.log(f"[UDP] local backend: {exc}")
        finally:
            self.pending.pop(flow, None)

    async def tick(self):
        loop = asyncio.get_running_loop()
        while True:
            if self.relay:
                self.relay.sendto(RELAY + self.relay_key + b"\x00" + bytes(16))
            carrier = self.owner.carrier(self.peer_id) if self.mode != "relay" else None
            if carrier is not self.carrier:
                self.detach_direct()
                self.carrier = carrier
                if carrier:
                    carrier.hub.application_datagrams = self.direct_receiver(carrier, loop)
            if carrier:
                self.challenge = secrets.token_bytes(8)
                self.send_direct(b"\x00" + self.challenge)
            now = time.monotonic()
            for flow, (transport, seen) in list(self.backends.items()):
                if now - seen > FLOW_IDLE:
                    self.backends.pop(flow)
                    transport.close()
            await asyncio.sleep(1)

    def detach_direct(self):
        if self.carrier and self.carrier.hub:
            self.carrier.hub.application_datagrams = None
        self.carrier = None
        self.direct_seen = 0

    async def run(self):
        core = self.owner.core
        loop = asyncio.get_running_loop()
        while core.running:
            writer = tick = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(core.server_host, core.server_port), 5)
                side = "host" if self.owner.is_host else "player"
                token = core.token or self.owner.engine._guest_token or "-"
                writer.write((f"UDP_ATTACH {proto.room_token(core.room)} {core.client_id} "
                              f"{core.username or '-'} {token} side={side} "
                              f"peer={self.peer_id} ver={APP_VERSION}\n").encode())
                await writer.drain()
                reply = (await asyncio.wait_for(reader.readline(), 5)).split()
                if len(reply) != 4 or reply[:2] != [b"OK", b"UDP"]:
                    raise ConnectionError("UDP attachment rejected")
                self.relay_key, self.direct_key = (bytes.fromhex(v.decode()) for v in reply[2:])
                if self.mode != "stun":
                    address = writer.get_extra_info("peername")[:2]
                    self.relay, _ = await loop.create_datagram_endpoint(
                        lambda: Receiver(self.receive_relay), remote_addr=address)
                tick = asyncio.create_task(self.tick())
                await reader.read(1)
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                core.log(f"[UDP] peer {self.peer_id}: {exc}")
            finally:
                if tick:
                    tick.cancel()
                    await asyncio.gather(tick, return_exceptions=True)
                self.detach_direct()
                if self.relay:
                    self.relay.close()
                    self.relay = None
                self.relay_seen = 0
                pending = [task for task, _ in self.pending.values()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                self.pending.clear()
                for transport, _ in self.backends.values():
                    transport.close()
                self.backends.clear()
                if writer:
                    await close_writer(writer)
            await asyncio.sleep(2)


class Forwarder:
    def __init__(self, core, engine, is_host):
        self.core = core
        self.engine = engine
        self.is_host = is_host
        self.listener = None
        self.clients_by_id = {}
        self.clients = {}
        self.peers = {}

    def carrier(self, peer):
        direct = self.engine.room_direct()
        carrier = direct._carriers.get(peer) if direct else None
        if carrier and carrier.started and not carrier.stop.is_set() and carrier.hub and carrier.tunnel:
            return carrier
        return None

    def local_packet(self, payload, addr):
        flow = self.clients.get(addr)
        if flow is None:
            if len(self.clients) >= MAX_FLOWS:
                return
            flow = secrets.randbits(32)
            while flow in self.clients_by_id:
                flow = secrets.randbits(32)
            self.clients[addr] = flow
        self.clients_by_id[flow] = addr, time.monotonic()
        for peer, _ in self.peers.values():
            peer.send(flow, payload)

    async def run(self):
        try:
            if not self.is_host:
                address = self.core.server.sockets[0].getsockname()[:2]
                self.listener, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: Receiver(self.local_packet), local_addr=address)
                self.core.log(f"[UDP] listening on {address}")
            while self.core.running:
                room = self.engine.room_current()
                state = room.state if room and room.running else None
                targets = set()
                if state:
                    if self.is_host and state.is_host:
                        targets = set(state.peer_ids or ()) & set(state.started_ids or ())
                    elif not self.is_host and state.host_id:
                        targets.add(state.host_id)
                expired = []
                for key in set(self.peers) - {(target, state.conn_mode) for target in targets}:
                    _, task = self.peers.pop(key)
                    task.cancel()
                    expired.append(task)
                await asyncio.gather(*expired, return_exceptions=True)
                for target in targets:
                    key = target, state.conn_mode
                    if key not in self.peers:
                        peer = Peer(self, *key)
                        self.peers[key] = peer, asyncio.create_task(peer.run())
                now = time.monotonic()
                for flow, (addr, seen) in list(self.clients_by_id.items()):
                    if now - seen > FLOW_IDLE:
                        self.clients_by_id.pop(flow)
                        self.clients.pop(addr)
                await asyncio.sleep(.25)
        except OSError as exc:
            self.core.log(f"[UDP] cannot bind application port: {exc}")
        finally:
            tasks = [task for _, task in self.peers.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.listener:
                self.listener.close()
