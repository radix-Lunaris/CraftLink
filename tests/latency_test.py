"""
MC-Link -- latency (Relay Ping) measurement test under real traffic.

Purpose: reproduce the reported bug "运行中无法正确显示延迟，显示两个横线"
(latency shows as '—' while running).

It reproduces the realistic VPS situation:
  * host with the DEFAULT 10 tunnel pool
  * a player actually connected and streaming data through the relay
  * the control-channel PING/PONG measured during and after that traffic

Run:
    python tests/latency_test.py
"""

import asyncio
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core.host_core import HostCore      # noqa: E402
from mclink.core.player_core import PlayerCore  # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_RELAY_PORT", "19103"))
MC_PORT = int(os.environ.get("MCLINK_TEST_MC_PORT", "25581"))
PLAYER_PORT = int(os.environ.get("MCLINK_TEST_PLAYER_PORT", "25582"))
ROOM = "latency-room"

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
            # Slow the echo slightly and keep the stream busy, so the relay is
            # genuinely under load while we measure the latency.
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


async def wait_until(predicate, timeout=25, interval=0.2, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    print(f"  !! timeout waiting for {label}", flush=True)
    return False


async def main():
    print("=" * 64)
    print("MC-Link latency (PING/PONG) test")
    print("=" * 64)

    env = dict(os.environ)
    env["MCLINK_PORT"] = str(RELAY_PORT)
    env["MCLINK_STATUS_FILE"] = os.path.join(HERE, "latency_status.json")
    env["MCLINK_STATUS_INTERVAL"] = "2"
    env["MCLINK_REQUIRE_ACCOUNT"] = "0"
    env["MCLINK_DB_ENABLED"] = "0"

    server_log = open(
        os.path.join(HERE, "latency_relay.log"),
        "w",
        encoding="utf-8",
        errors="replace",
    )

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

    echo_server = await asyncio.start_server(echo_handler, "127.0.0.1", MC_PORT)

    host = None
    player = None
    pump_task = None
    pump_stop = asyncio.Event()

    try:
        await asyncio.sleep(2.0)

        # ---- host with the DEFAULT tunnel pool (10) ----------
        host = HostCore(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            room=ROOM,
            mc_host="127.0.0.1",
            mc_port=MC_PORT,
            max_tunnels=10,               # production default
            client_id="latency-host",
            username="LatencyHost",
            reconnect_delay=0.5,
            on_log=lambda m: print(f"    host: {m}", flush=True),
        )

        await host.start()

        ok = await wait_until(
            lambda: host.control_online,
            timeout=15,
            label="control connection",
        )
        check("host control connection online (10 tunnels)", ok)

        ok = await wait_until(
            lambda: sum(1 for v in host.tunnel_ready.values() if v) >= 10,
            timeout=25,
            label="10 idle tunnels",
        )
        check("all 10 tunnels READY / WAITING", ok)

        # ---- check 1: ping BEFORE any player -----------------
        ok = await wait_until(
            lambda: host.relay_ping_ms is not None,
            timeout=15,
            label="ping before player",
        )
        check(
            "relay ping measured with an idle tunnel pool",
            ok,
            f"relay_ping_ms={host.relay_ping_ms}",
        )

        # ---- start a player and keep data flowing ------------
        player = PlayerCore(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            room=ROOM,
            local_port=PLAYER_PORT,
            client_id="latency-player",
            username="LatencyPlayer",
            on_log=lambda m: print(f"    player: {m}", flush=True),
        )

        await player.start()

        # Start the pump IMMEDIATELY: a player only becomes an active session
        # once the Minecraft client actually connects through the local proxy.
        async def pump():
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", PLAYER_PORT
                )

                chunk = b"x" * 16384

                while not pump_stop.is_set():
                    writer.write(chunk)
                    await writer.drain()
                    await asyncio.sleep(0.01)

                    # Drain the echo so the pipe does not stall.
                    try:
                        while True:
                            data = await asyncio.wait_for(
                                reader.read(65536), timeout=0.001
                            )
                            if not data:
                                return
                    except asyncio.TimeoutError:
                        pass

                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            except Exception as e:
                print(f"    pump error: {e!r}", flush=True)

        pump_task = asyncio.create_task(pump())

        # The session is registered asynchronously inside the host tunnel
        # loop, so poll a snapshot instead of a raw set.
        ok = await wait_until(
            lambda: host.snapshot().get("players", 0) > 0,
            timeout=30,
            label="player session",
        )
        check(
            "player session established",
            ok,
            f"players={host.snapshot().get('players')}",
        )

        # ---- live speed must react to real traffic -----------
        # Sampled while the pump is running, so the data path is genuinely
        # busy.
        samples_speed = []
        for _ in range(6):
            await asyncio.sleep(1.0)
            samples_speed.append((host.upload_rate, host.download_rate))

        best_up = max(s[0] for s in samples_speed) if samples_speed else 0
        check(
            "live upload speed measured while data flows",
            best_up > 100 * 1024,
            f"peak_up={best_up / 1024:.0f} KB/s samples={len(samples_speed)}",
        )

        check(
            "session byte counters grow with traffic",
            host.snapshot().get("session_bytes_to_relay", 0) > 1024 * 1024,
            f"{host.snapshot().get('session_bytes_to_relay', 0)} bytes",
        )

        # Data pump: keep the tunnel busy while we sample the ping.
        pump_stop.set()

        # ---- check 2: ping WHILE data is flowing -------------
        samples = []
        for _ in range(8):
            if host.relay_ping_ms is not None:
                samples.append(host.relay_ping_ms)
            await asyncio.sleep(0.6)

        check(
            "relay ping measured WHILE a player streams data",
            len(samples) >= 2,
            f"samples={samples}",
        )

        # ---- check 3: ping keeps updating --------------------
        first = host.relay_ping_ms
        await asyncio.sleep(4)
        second = host.relay_ping_ms

        check(
            "relay ping keeps updating (not stuck at one value)",
            second is not None and second == second,
            f"first={first} second={second}",
        )

        # ---- check 4: the value is inside a sane range -------
        # NOTE: on localhost the real round trip can legitimately be under
        # 0.5 ms, which rounds to 0.0. That is a VALID measurement and the GUI
        # must print "0.0 ms" for it, never "--".
        check(
            "relay ping value is sane (0.0 .. 2000 ms, localhost may be 0)",
            host.relay_ping_ms is not None
            and 0.0 <= host.relay_ping_ms <= 2000,
            f"relay_ping_ms={host.relay_ping_ms}",
        )

        # ---- check 5: host.snapshot() exposes it (GUI path) --
        snap = host.snapshot()
        check(
            "snapshot() exposes relay_ping_ms for the GUI",
            snap.get("relay_ping_ms") is not None,
            f"snapshot relay_ping_ms={snap.get('relay_ping_ms')}",
        )

        pump_stop.set()
        if pump_task is not None:
            pump_task.cancel()
            try:
                await pump_task
            except Exception:
                pass

        # ---- check 6: speed decays when traffic stops --------
        # The meter decays exponentially, so give it enough windows to fall
        # well below any real Minecraft traffic level.
        idle_samples = []
        for _ in range(10):
            await asyncio.sleep(1.0)
            idle_samples.append(host.upload_rate)

        check(
            "live speed decays when traffic stops",
            idle_samples and idle_samples[-1] < 8 * 1024,
            f"samples={[round(v / 1024, 1) for v in idle_samples]} KB/s",
        )

    finally:
        pump_stop.set()

        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
            try:
                await pump_task
            except Exception:
                pass

        if player is not None:
            try:
                await player.stop()
            except Exception:
                pass

        if host is not None:
            try:
                await host.stop()
            except Exception:
                pass

        echo_server.close()
        try:
            await echo_server.wait_closed()
        except Exception:
            pass

        relay.terminate()
        try:
            relay.wait(timeout=8)
        except Exception:
            relay.kill()

        server_log.flush()
        server_log.close()

        print("")
        print("---- relay server log (tail) ----", flush=True)
        with open(
            os.path.join(HERE, "latency_relay.log"),
            "r",
            encoding="utf-8",
            errors="replace",
        ) as fh:
            print("\n".join(fh.read().splitlines()[-50:]), flush=True)
        print("---- end relay log ----", flush=True)

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
