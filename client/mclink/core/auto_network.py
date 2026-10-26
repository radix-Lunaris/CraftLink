"""Build AUTO network tunnels on Start, without an application socket.

These authenticated peer sessions validate both carriers and perform automatic
upgrades/fallbacks even when neither configured application port is in use.
Application TCP streams still attach individually, relay first.
"""
import asyncio
import contextlib

from ..config.settings import APP_VERSION
from . import protocol as proto
from .auto_stream import parse_metadata
from .auto_client import run_stream


async def peer_tunnel(core, peer, is_host):
    while core.running:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(core.server_host, core.server_port), 5)
            side = "host" if is_host else "player"
            line = (f"AUTO_NETWORK {proto.room_token(core.room)} "
                    f"{core.client_id or '-'} {core.username or '-'} {core.token or '-'} "
                    f"peer={peer} side={side} caps=auto_stream_v1 ver={APP_VERSION}\n")
            writer.write(line.encode())
            await writer.drain()
            response = (await asyncio.wait_for(reader.readline(), 8)).decode().strip()
            if response.startswith("ERROR"):
                raise ConnectionError(response)
            sid, info = parse_metadata(response, "CONNECTED_AUTO_NETWORK")
            if info.get("channel") != "network":
                raise ConnectionError("server did not negotiate an independent network tunnel")
            core.log(f"[auto] network tunnel attached for {peer}; no application listener required")
            await run_stream(core, info, sid, is_host, None, None, reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            core.log(f"[auto] network tunnel for {peer} unavailable: {exc}; retrying in 3s")
        finally:
            if writer:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
        await asyncio.sleep(3)


async def supervise(core, room_provider):
    tasks = {}
    try:
        while core.running:
            room = room_provider() if room_provider else None
            state = getattr(room, "state", None)
            peers = set()
            is_host = getattr(state, "host_id", "") == core.client_id
            if state and state.conn_mode == "auto" and getattr(room, "running", False):
                if is_host:
                    started = set(state.started_ids or [])
                    conns = state.member_conns or {}
                    peers = {p for p in (state.peer_ids or [])
                             if p in started or conns.get(p) in ("waiting", "pairing", "connected")}
                elif state.host_id:
                    peers = {state.host_id}
            for peer in set(tasks) - peers:
                tasks.pop(peer).cancel()
            for peer in peers:
                if peer not in tasks or tasks[peer].done():
                    tasks[peer] = asyncio.create_task(peer_tunnel(core, peer, is_host))
            await asyncio.sleep(.2)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
