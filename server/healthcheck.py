"""
MC-Link relay -- container health check.

Used by `docker healthcheck` / `docker compose`. Standard library only, so it
works inside the slim image without installing curl.

It verifies the two things that actually matter:
  1. The process is still listening on the relay TCP port.
  2. The status file is being refreshed (the event loop is not stuck).
"""

import json
import os
import socket
import sys
import time


def main():
    port = int(os.environ.get("MCLINK_PORT", "19100"))
    status_file = os.environ.get("MCLINK_STATUS_FILE", "/app/status/status.json")

    # ---- 1. TCP listener -------------------------------------
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
    except OSError as e:
        print(f"unhealthy: cannot connect to 127.0.0.1:{port} ({e})")
        return 1

    # ---- 2. status file freshness ----------------------------
    try:
        age = time.time() - os.path.getmtime(status_file)

        with open(status_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        if not isinstance(data, dict) or "uptime" not in data:
            print("unhealthy: status file malformed")
            return 1

    except FileNotFoundError:
        # The first status write happens after STATUS_INTERVAL seconds.
        # Do not fail the container during the very first seconds.
        if time.time() - float(os.environ.get("MCLINK_BOOT_TS", "0")) > 60:
            print("unhealthy: status file missing")
            return 1
        print("starting: status file not written yet")
        return 0

    except Exception as e:
        print(f"unhealthy: status file unreadable ({e})")
        return 1

    # Two missed intervals => the loop is stuck.
    max_age = float(os.environ.get("MCLINK_STATUS_INTERVAL", "10")) * 3 + 10

    if age > max_age:
        print(f"unhealthy: status file is {age:.0f}s old (> {max_age:.0f}s)")
        return 1

    players = data.get("players_total", 0)
    rooms = data.get("rooms_total", 0)

    database = data.get("database") or {}
    auth_required = bool(data.get("auth_required"))
    accounts = len(data.get("online_accounts") or [])

    # The relay is HEALTHY as long as it is serving Minecraft traffic, even if
    # MySQL is unreachable -- the frozen TCP path never depends on the
    # database. A database problem is reported, not fatal, so that a broken
    # panel connection cannot cause Docker to restart-kill live game sessions.
    notes = []

    if database.get("enabled"):
        if database.get("healthy"):
            notes.append("db=up")
        else:
            notes.append("db=DOWN(面板不可用,联机不受影响)")

        if database.get("degraded"):
            notes.append(f"db_errors={database.get('errors', 0)}")
    else:
        notes.append("db=off")

    notes.append("auth=" + ("on" if auth_required else "off"))
    notes.append(f"accounts={accounts}")

    print(
        f"healthy: rooms={rooms} players={players} "
        f"uptime={int(data.get('uptime', 0))}s " + " ".join(notes)
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
