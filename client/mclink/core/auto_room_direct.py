"""AUTO scheduling at the boundary of the unchanged v3 room puncher.

NAT measurement, ingress probes, candidate exchange, punch synchronization,
socket reuse and manual STUN transport all remain in the released modules.
Only AUTO retry admission and the carrier attached to a punched socket differ.
"""
import asyncio
import time

from . import room_direct as released
from .auto_peer import AutoPeer


class AutoRoomDirectLink(released.RoomDirectLink):
    def _peers(self):
        peers = super()._peers()
        now = time.monotonic()
        streams = list(self.engine.auto_manager.streams.values())
        return [peer for peer in peers
                if not any(s.peer_id == peer and s.retry_at > now for s in streams)]

    def _open_tunnel(self, peer):
        async def run():
            entry = self.links.get(peer)
            punch_link = entry.get("link") if entry else None
            if punch_link is None:
                return
            carrier = self._carriers.get(peer)
            if carrier is None or carrier.stop.is_set():
                try:
                    carrier = AutoPeer(peer, punch_link, self.engine.auto_manager,
                                       log=self.log, peer_addr=entry.get("addr"))
                except Exception as exc:
                    self.log(f"[auto] cannot attach UDP carrier for {peer}: {exc!r}")
                    return
                self._carriers[peer] = carrier
            # Preserve v3's bounded handshake retries on the SAME punch socket.
            # AUTO opens MC backends through AutoStream, never PeerLink spares.
            backoff = float(released.TUNNEL_RETRY_BACKOFF_S)
            attempts = max(1, int(released.TUNNEL_HANDSHAKE_ATTEMPTS))
            for attempt in range(attempts):
                if carrier.stop.is_set():
                    return
                try:
                    if await carrier.ensure_started(released.CONNECT_TIMEOUT_S):
                        self.log(f"[auto] direct UDP carrier ready, peer {peer}; validating before upgrade")
                        return
                except Exception as exc:
                    self.log(f"[auto] UDP carrier handshake for {peer} failed: {exc!r}")
                if self._link_socket_dead(punch_link):
                    self._drop_peer(peer)
                    return
                if attempt + 1 < attempts:
                    await asyncio.sleep(backoff)
                    backoff *= 2
            self.log(f"[auto] UDP carrier not ready for {peer}; relay remains active")

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
