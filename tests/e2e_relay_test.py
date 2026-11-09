"""
MC-Link -- end to end relay regression test (headless).

This is the test that MUST pass before any change is considered safe.

What it proves:
  1. The relay starts and speaks the frozen protocol.
  2. HostCore brings up CONTROL + a tunnel pool.
  3. PlayerCore listens locally and gets CONNECTED from the relay.
  4. Arbitrary BINARY payload survives the full path
         test client -> PlayerCore -> relay -> HostCore -> 本机 MC echo
         and back again, byte for byte.
  5. Host disconnect destroys the room immediately; the SAME room id can be
     hosted again without restarting the relay.
  6. Two players can be served at the same time.

Run:
    python tests/e2e_relay_test.py

It does NOT need Minecraft and does NOT need the VPS: everything is local.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_DIR = os.path.join(ROOT, "server")
CLIENT_DIR = os.path.join(ROOT, "client")

sys.path.insert(0, CLIENT_DIR)

from mclink.core.host_core import HostCore      # noqa: E402
from mclink.core.player_core import PlayerCore  # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_RELAY_PORT", "19101"))
MC_PORT = int(os.environ.get("MCLINK_TEST_MC_PORT", "25577"))
PLAYER_PORT = int(os.environ.get("MCLINK_TEST_PLAYER_PORT", "25578"))
ROOM = "e2e-room"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def port_open(port):
    """True when something accepts a TCP connection on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


# ============================================================
# Fake local Minecraft LAN world: a plain TCP echo server
# ============================================================

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


async def start_echo_server():
    server = await asyncio.start_server(echo_handler, "127.0.0.1", MC_PORT)
    return server


# ============================================================
# Helpers
# ============================================================

async def wait_until(predicate, timeout=25, interval=0.2, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    print(f"  !! timeout waiting for {label}", flush=True)
    return False


def make_payload(size):
    """Deterministic pseudo random binary payload."""
    out = bytearray(size)
    seed = 0x12345678
    for i in range(size):
        seed = (seed * 1103515245 + 12345) & 0xFFFFFFFF
        out[i] = (seed >> 16) & 0xFF
    return bytes(out)


async def roundtrip(port, payload, timeout=30, close_after_send=False):
    """Connect to the local proxy, send payload, read the echo back."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), timeout=10
    )

    received = bytearray()

    async def reader_task():
        try:
            while len(received) < len(payload):
                chunk = await reader.read(65536)
                if not chunk:
                    break
                received.extend(chunk)
        except Exception:
            pass

    task = asyncio.create_task(reader_task())

    writer.write(payload)
    await writer.drain()

    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass

    return bytes(received)


# ============================================================
# Main
# ============================================================

async def main():
    print("=" * 64)
    print("MC-Link end-to-end relay regression test")
    print("=" * 64)

    for port, name in (
        (RELAY_PORT, "relay port"),
        (MC_PORT, "fake MC port"),
        (PLAYER_PORT, "player proxy port"),
        (PLAYER_PORT + 1, "player proxy port 2"),
    ):
        if not port_free(port):
            print(f"!! {name} {port} is already in use; aborting.")
            return 2

    env = dict(os.environ)
    env["MCLINK_PORT"] = str(RELAY_PORT)
    env["MCLINK_STATUS_FILE"] = os.path.join(HERE, "status.json")
    env["MCLINK_STATUS_INTERVAL"] = "2"
    # v1 clients do not log in: keep the frozen protocol usable.
    env["MCLINK_REQUIRE_ACCOUNT"] = "0"
    env["MCLINK_DB_ENABLED"] = "0"

    relay = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=SERVER_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    relay_log = []

    async def pump_relay_log():
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, relay.stdout.readline)
            if not line:
                return
            relay_log.append(line.rstrip())

    log_task = asyncio.create_task(pump_relay_log())

    echo_server = None
    host = None
    player = None
    player2 = None

    try:
        # ---- 1. relay is listening -------------------------
        ok = await wait_until(
            lambda: port_open(RELAY_PORT),
            timeout=15,
            label="relay to listen",
        )
        check("relay server starts and listens", ok)

        if not ok:
            print("\n".join(relay_log[-20:]))
            return 1

        echo_server = await start_echo_server()

        # ---- 2. host: control + tunnel pool ----------------
        host = HostCore(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            room=ROOM,
            mc_host="127.0.0.1",
            mc_port=MC_PORT,
            max_tunnels=3,
            client_id="host-test-id",
            username="TestHost",
            reconnect_delay=0.5,
            on_log=lambda m: print(f"    host: {m}", flush=True),
        )

        await host.start()

        ok = await wait_until(
            lambda: host.control_online,
            timeout=15,
            label="host control connection",
        )
        check("host CONTROL handshake (HOST <room>) accepted", ok)

        ok = await wait_until(
            lambda: sum(1 for v in host.tunnel_ready.values() if v) >= 3,
            timeout=20,
            label="3 idle tunnels",
        )
        check("host tunnel pool ready (WAIT)", ok)

        # ---- 3. player listens locally ---------------------
        player = PlayerCore(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            room=ROOM,
            local_port=PLAYER_PORT,
            client_id="player-test-id",
            username="TestPlayer",
            on_log=lambda m: print(f"    player: {m}", flush=True),
        )

        await player.start()
        check("player local proxy binds", player.server is not None)

        # ---- 4. binary round trip --------------------------
        payload = make_payload(256 * 1024)

        started = time.time()
        received = await roundtrip(PLAYER_PORT, payload)
        elapsed = time.time() - started

        check(
            "256 KiB binary payload relayed intact",
            received == payload,
            f"{len(received)}/{len(payload)} bytes in {elapsed:.2f}s",
        )

        ok = await wait_until(
            lambda: host.active_sessions == set(),
            timeout=15,
            label="session release",
        )
        check("session released after the client disconnected", ok)

        # ---- 5. two players at the same time ---------------
        payload_a = make_payload(64 * 1024)
        payload_b = bytes(reversed(make_payload(96 * 1024)))

        results = await asyncio.gather(
            roundtrip(PLAYER_PORT, payload_a),
            roundtrip(PLAYER_PORT, payload_b),
            return_exceptions=True,
        )

        both_ok = (
            not isinstance(results[0], Exception)
            and not isinstance(results[1], Exception)
            and results[0] == payload_a
            and results[1] == payload_b
        )
        check("two concurrent players relayed", both_ok)

        # ---- 6. host reconnect with the SAME room ----------
        await player.stop()
        player = None

        await host.stop()
        host = None

        ok = await wait_until(
            lambda: True,
            timeout=1,
            label="settle",
        )

        # The player must now be rejected (no host).
        await asyncio.sleep(1.0)

        rejected = False
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", RELAY_PORT
            )
            writer.write(f"PLAYER {ROOM}\n".encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            text = line.decode(errors="replace").strip()
            rejected = text.startswith("ERROR NO_HOST")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except Exception as e:
            print(f"    reject probe error: {e}", flush=True)

        check("room is gone after host disconnect (ERROR NO_HOST)", rejected)

        # ---- 7. same room id can be hosted again -----------
        host = HostCore(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            room=ROOM,
            mc_host="127.0.0.1",
            mc_port=MC_PORT,
            max_tunnels=2,
            client_id="host-test-id",
            username="TestHost",
            reconnect_delay=0.5,
            on_log=lambda m: print(f"    host2: {m}", flush=True),
        )

        await host.start()

        ok = await wait_until(
            lambda: host.control_online
            and sum(1 for v in host.tunnel_ready.values() if v) >= 2,
            timeout=20,
            label="host2 ready",
        )
        check("host can re-create the SAME room without restarting relay", ok)

        player = PlayerCore(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            room=ROOM,
            local_port=PLAYER_PORT,
            client_id="player-test-id",
            username="TestPlayer",
            on_log=lambda m: print(f"    player3: {m}", flush=True),
        )

        await player.start()

        payload = make_payload(32 * 1024)
        received = await roundtrip(PLAYER_PORT, payload)

        check("relay works again after host re-connect", received == payload)

        # ---- 8. status file --------------------------------
        status_path = os.path.join(HERE, "status.json")
        ok = await wait_until(
            lambda: os.path.isfile(status_path),
            timeout=10,
            label="status file",
        )
        check("status.json written by the relay", ok)

    finally:
        for name, obj in (("player", player), ("host", host)):
            if obj is not None:
                try:
                    await obj.stop()
                except Exception as e:
                    print(f"    stop {name} error: {e}", flush=True)

        if echo_server is not None:
            echo_server.close()
            try:
                await echo_server.wait_closed()
            except Exception:
                pass

        log_task.cancel()

        relay.terminate()
        try:
            relay.wait(timeout=8)
        except Exception:
            relay.kill()

        # Drain the remaining relay output for the report.
        try:
            rest = relay.stdout.read() or ""
            relay_log.extend(rest.splitlines())
        except Exception:
            pass

        with open(
            os.path.join(HERE, "relay_test_output.log"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("\n".join(relay_log))

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        print("")
        print("Last relay output:")
        print("\n".join(relay_log[-40:]))
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
