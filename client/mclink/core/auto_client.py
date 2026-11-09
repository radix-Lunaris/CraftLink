"""HostCore/PlayerCore AUTO adapter and authenticated relay reattachment."""
import asyncio

from ..config.settings import APP_VERSION
from . import protocol as proto
from .auto_stream import AutoStream, TcpPath


async def run_stream(core, info, session, is_host, mc_reader, mc_writer,
                     relay_reader, relay_writer):
    async def reconnect():
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(core.server_host, core.server_port), 5)
        try:
            side = "host" if is_host else "player"
            line = (f"AUTO_RESUME {proto.room_token(core.room)} "
                    f"{core.client_id or '-'} {core.username or '-'} {core.token or '-'} "
                    f"sid={session} side={side} generation={info['generation']} "
                    f"resume={info['resume']} caps=auto_stream_v1 ver={APP_VERSION}\n")
            writer.write(line.encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), 5)
            if response.strip() != b"OK AUTO_RESUME":
                if response.startswith(b"ERROR AUTO_RESUME_DENIED"):
                    raise PermissionError("AUTO relay session revoked")
                raise ConnectionError("AUTO relay resume rejected")
            return TcpPath(reader, writer)
        except BaseException:
            writer.close()
            raise

    peer = info["player"] if is_host else info["host"]
    stream = AutoStream(core.auto_manager, session, peer, is_host,
        mc_reader, mc_writer, TcpPath(relay_reader, relay_writer), reconnect,
        metadata={"generation": info["generation"],
                  "channel": info.get("channel", "data"),
                  "public_ip": info.get("player_ip" if is_host else "host_ip", "").replace("-", "")})
    await stream.run()
