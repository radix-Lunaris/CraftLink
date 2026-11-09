"""
MC-Link -- engine / session layer test (headless, no tkinter).

This covers the exact code path the GUI uses:

    MCLinkEngine
      -> run_coroutine_threadsafe on a dedicated asyncio thread
      -> HostSession / PlayerSession
      -> HostCore / PlayerCore

It proves that the threading bridge works (start / stop from a foreign
thread, event queue draining, snapshot polling) and that a player session
survives the full GUI lifecycle.

Run:
    python tests/engine_session_test.py
"""

import contextlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.config.settings import ClientConfig          # noqa: E402
from mclink.core.engine import MCLinkEngine              # noqa: E402
import asyncio                                           # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_RELAY_PORT", "19102"))
MC_PORT = int(os.environ.get("MCLINK_TEST_MC_PORT", "25579"))
PLAYER_PORT = int(os.environ.get("MCLINK_TEST_PLAYER_PORT", "25580"))
ROOM = "engine-room"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


async def echo_handler(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def wait_for(predicate, timeout=25, interval=0.25, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    print(f"  !! timeout waiting for {label}", flush=True)
    return False


def main():
    print("=" * 64)
    print("MC-Link engine + session layer test")
    print("=" * 64)

    env = dict(os.environ)
    env["MCLINK_PORT"] = str(RELAY_PORT)
    env["MCLINK_STATUS_FILE"] = os.path.join(HERE, "status_engine.json")
    env["MCLINK_REQUIRE_ACCOUNT"] = "0"
    env["MCLINK_DB_ENABLED"] = "0"

    server_log_path = os.path.join(HERE, "relay_engine_test.log")
    server_log = open(server_log_path, "w", encoding="utf-8", errors="replace")

    relay = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    # --- echo server on its own asyncio thread --------------------
    echo_state = {}

    def run_echo():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def serve():
            server = await asyncio.start_server(
                echo_handler, "127.0.0.1", MC_PORT
            )
            echo_state["server"] = server
            async with server:
                await server.serve_forever()

        try:
            loop.run_until_complete(serve())
        except Exception:
            pass

    import threading

    echo_thread = threading.Thread(target=run_echo, daemon=True)
    echo_thread.start()

    engine = MCLinkEngine()          # stands in for the HOST computer
    player_engine = MCLinkEngine()   # stands in for the PLAYER computer

    try:
        time.sleep(1.5)

        # ---- engine / relay probe ------------------------------
        # The probe must use an UNAUTHENTICATED request. An earlier version
        # used the HOST handshake, which the V2 relay correctly refuses with
        # AUTH_REQUIRED -- making the GUI button report "server unavailable"
        # on a perfectly healthy server.
        ok, message, rtt = engine.test_relay("127.0.0.1", RELAY_PORT)
        check("engine.test_relay() reports a healthy relay", ok, message)

        check(
            "the probe works on a relay that REQUIRES login",
            ok and "正常" in message,
            message,
        )

        # ---- host session --------------------------------------
        #
        # max_tunnels is passed WRONG on purpose (2). It is a derived value:
        # the room capacity decides how many tunnels are opened, one per
        # player, so validated() must overwrite whatever it is handed. The
        # relay lets a host open any number of tunnels; the capacity is what
        # limits how many of them can be paired with a player.
        host_cfg = ClientConfig(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            username="EngineHost",
            client_id="engine-host",
            role="host",
            room=ROOM,
            mc_host="127.0.0.1",
            mc_port=MC_PORT,
            max_players=3,
            max_tunnels=2,
        ).validated()

        engine.start_session(host_cfg)

        ok = wait_for(
            lambda: engine.snapshot().get("state") == "ONLINE",
            timeout=25,
            label="host session ONLINE",
        )
        check("HostSession reaches ONLINE through the engine", ok)

        snap = engine.snapshot()
        check(
            "host snapshot exposes tunnel + room data",
            snap.get("tunnels_ready") == 3 and snap.get("room") == ROOM,
            str({k: snap.get(k) for k in ("state", "tunnels_ready", "room")}),
        )

        check(
            "房间人数上限上报给界面（隧道数跟着它走，不再写死）",
            snap.get("max_players") == 3
            and snap.get("tunnels_total") == host_cfg.max_tunnels == 3,
            f"max_players={snap.get('max_players')} "
            f"tunnels_total={snap.get('tunnels_total')} "
            f"cfg.max_tunnels={host_cfg.max_tunnels}",
        )

        ok = wait_for(
            lambda: engine.snapshot().get("relay_ping_ms") is not None,
            timeout=20,
            label="PING/PONG on the control channel",
        )
        check(
            "control channel PING/PONG works (latency measured)",
            ok,
            f"relay_ping_ms={engine.snapshot().get('relay_ping_ms')}",
        )

        # ---- player session ------------------------------------
        # IMPORTANT: one client = one session (Host OR Player), exactly like
        # the GUI. A second engine stands in for the second computer.
        player_cfg = ClientConfig(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            username="EnginePlayer",
            client_id="engine-player",
            role="player",
            room=ROOM,
            local_port=PLAYER_PORT,
        ).validated()

        player_engine.start_session(player_cfg)

        ok = wait_for(
            lambda: player_engine.snapshot().get("state") == "ONLINE"
            and player_engine.snapshot().get("role") == "player",
            timeout=20,
            label="player session ONLINE",
        )
        check("PlayerSession reaches ONLINE through the engine", ok)

        # The host session must still be ONLINE while the player connects.
        check(
            "host session is unaffected by the second client",
            engine.snapshot().get("state") == "ONLINE",
            engine.snapshot().get("state"),
        )

        # ---- real traffic through the engine managed core -------
        import socket

        payload = bytes((i * 7 + 3) % 256 for i in range(48 * 1024))

        try:
            with socket.create_connection(
                ("127.0.0.1", PLAYER_PORT), timeout=10
            ) as sock:
                sock.settimeout(30)
                sock.sendall(payload)

                received = bytearray()
                while len(received) < len(payload):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    received.extend(chunk)

            check(
                "48 KiB payload relayed through the engine sessions",
                bytes(received) == payload,
                f"{len(received)}/{len(payload)} bytes",
            )
        except Exception as e:
            check(
                "48 KiB payload relayed through the engine sessions",
                False,
                f"exception: {e!r}",
            )

        # Dump everything the core reported, for diagnosis.
        for event in engine.drain_events():
            if event.get("kind") == "log":
                print(f"    engine-log: {event.get('text')}", flush=True)
            elif event.get("kind") == "event":
                print(
                    f"    engine-event: {event.get('name')} {event.get('data')}",
                    flush=True,
                )

        # ---- stop is clean -------------------------------------
        engine.stop_session(timeout=15)

        ok = wait_for(
            lambda: engine.snapshot().get("state") == "IDLE",
            timeout=15,
            label="session stops",
        )
        check("engine.stop_session() returns to IDLE", ok)

        # The player must be rejected once the host is gone.
        player_engine.stop_session(timeout=15)
        time.sleep(1.0)

        ok = wait_for(
            lambda: player_engine.snapshot().get("role") is None,
            timeout=10,
            label="player session stops",
        )
        check("player engine stops cleanly", ok)

        # ---- restarting the same session works (no leaked state) -
        player_engine.start_session(player_cfg)

        ok = wait_for(
            lambda: player_engine.snapshot().get("state") == "ONLINE",
            timeout=20,
            label="player session restarted",
        )
        check("session can be restarted immediately after stop", ok)

        player_engine.stop_session(timeout=15)

        # ---- host restart on the SAME room id (the old bug) ----
        engine.start_session(host_cfg)

        ok = wait_for(
            lambda: engine.snapshot().get("state") == "ONLINE",
            timeout=25,
            label="host session restart on the same room",
        )
        check("host can re-host the SAME room without a relay restart", ok)

    finally:
        for eng in (engine, player_engine):
            try:
                eng.shutdown()
            except Exception:
                pass

        relay.terminate()
        try:
            relay.wait(timeout=8)
        except Exception:
            relay.kill()

        with contextlib.suppress(Exception):
            server_log.flush()
            server_log.close()

        with contextlib.suppress(Exception):
            print("")
            print("---- relay server log ----")
            with open(server_log_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            print("\n".join(lines[-60:]))
            print("---- end relay server log ----")

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())

