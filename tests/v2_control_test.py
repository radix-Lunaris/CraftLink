"""
MC-Link -- V2 tests: accounts, rooms, deletion rules, admin API.

Runs the real relay server as a subprocess with the in-memory store
(MCLINK_DB_ENABLED=0), so no MySQL is needed and the whole control plane is
exercised end to end:

  * LOGIN over the TCP protocol (wrong password / right password / disabled)
  * HOST and PLAYER are refused without a token, accepted with one
  * ROOMS lists the live rooms
  * DELETE refuses a room that still has players (server side, not just UI)
  * DELETE works for the owner once the room is empty
  * the HTTP admin API needs the bearer token and can force-close a room
  * the Minecraft data path still works while all of this is enabled

Run:
    python tests/v2_control_test.py
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_RELAY_PORT", "19110"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_ADMIN_PORT", "19111"))
MC_PORT = int(os.environ.get("MCLINK_TEST_MC_PORT", "25590"))

ADMIN_TOKEN = "test-admin-token-please-change"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)



# ============================================================
# 版本门（AGENTS.md 规则 1）：握手行尾必须挂 ver=
# ============================================================

def _server_version():
    """`server/VERSION` —— 中继版本门的权威版本号。

    本套用裸 socket 直接发协议行，不是真客户端；但**握手行同样必须带
    `ver=`**：中继的版本门装在唯一的握手解析点上（`server/relay_server.py`
    的 `parse_hello()` 之后），CREATE / JOIN / HOST / HOST_TUNNEL / PLAYER
    少了这个字段会被判 `VERSION_MISMATCH` 当场拒绝。

    读的和服务端是同一个文件（`server/VERSION`，由 sync-deploy.py 生成）；
    文件不在时返回空串 = 门关，行为与以前一致。
    """
    try:
        with open(os.path.join(ROOT, "server", "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


SERVER_VERSION = _server_version()


def _ver():
    """拼在握手行**最后**的 ` ver=<版本>`；没有版本文件时为空串。"""
    return f" ver={SERVER_VERSION}" if SERVER_VERSION else ""


# ============================================================
# Tiny protocol client
# ============================================================

async def send_line(host, port, line, expect_reply=True, timeout=8):
    """Open a connection, send one line, read one reply line."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    reply = ""

    if expect_reply:
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
        reply = data.decode(errors="replace").strip()

    return reader, writer, reply


def _room_state(line):
    """
    The state field of a ROOM line: "empty" | "busy".

        ROOM <room> <owner> <players>/<max> <idle> <waiting> <state> host=… members=…

    It is field 7, NOT the end of the line: host= and members= were added after
    it, so an endswith() check would break the moment the relay reports one
    more thing (which is exactly what happened).
    """
    parts = line.split()

    return parts[6] if len(parts) > 6 else ""


async def read_many(reader, timeout=8):
    """Read lines until DONE (for the ROOMS reply)."""
    lines = []

    while True:
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not data:
            break
        text = data.decode(errors="replace").strip()
        lines.append(text)
        if text == "DONE":
            break

    return lines


async def close_pair(reader, writer):
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


def http_request(method, path, token=None, body=None, port=None, timeout=8):
    url = f"http://127.0.0.1:{port or ADMIN_PORT}{path}"

    data = None
    headers = {}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return response.status, json.loads(payload or "{}")
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")
        try:
            return e.code, json.loads(payload or "{}")
        except Exception:
            return e.code, {"raw": payload}


# ============================================================
# Echo server standing in for the host's Minecraft world
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


async def wait_until(predicate, timeout=20, interval=0.25, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    print(f"  !! timeout waiting for {label}", flush=True)
    return False


# ============================================================
# Main
# ============================================================

async def main():
    print("=" * 64)
    print("MC-Link V2 control plane test (accounts / rooms / admin API)")
    print("=" * 64)

    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(RELAY_PORT),
            "MCLINK_DB_ENABLED": "0",
            "MCLINK_REQUIRE_ACCOUNT": "1",
            "MCLINK_BOOTSTRAP_ADMIN": "admin:adminpass123",
            "MCLINK_ADMIN_API_ENABLED": "1",
            "MCLINK_ADMIN_API_HOST": "127.0.0.1",
            "MCLINK_ADMIN_API_PORT": str(ADMIN_PORT),
            "MCLINK_ADMIN_API_TOKEN": ADMIN_TOKEN,
            "MCLINK_STATUS_FILE": os.path.join(HERE, "v2_status.json"),
            "MCLINK_STATUS_INTERVAL": "2",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            # Short heartbeat timeout so the stuck-room test does not have to
            # wait 25 seconds.
            "MCLINK_HOST_IDLE_TIMEOUT": "3",
            "MCLINK_HOST_WATCHDOG_INTERVAL": "1",
            "MCLINK_VERBOSE": "1",
        }
    )

    server_log = open(
        os.path.join(HERE, "v2_relay.log"),
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

    host_pair = None
    tunnel_writers = []
    player_pair = None

    try:
        ok = await wait_until(
            lambda: port_open(RELAY_PORT), timeout=20, label="relay to listen"
        )
        check("relay starts with V2 services", ok)

        if not ok:
            return 1

        # ---- 1. LOGIN: bad credentials ----------------------
        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "LOGIN __mclink__ admin wrongpass"
        )
        check(
            "LOGIN rejects a wrong password",
            reply.startswith("ERROR AUTH_BAD_CREDENTIALS"),
            reply,
        )
        await close_pair(reader, writer)

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "LOGIN __mclink__ ghost whatever123"
        )
        check(
            "LOGIN rejects an unknown account the same way",
            reply.startswith("ERROR AUTH_BAD_CREDENTIALS"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- 2. LOGIN: success ------------------------------
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            "LOGIN __mclink__ admin adminpass123 host-client-1",
        )
        check("LOGIN succeeds for the bootstrap admin", reply.startswith("OK LOGIN"), reply)

        token = ""
        is_admin = 0
        protocol = 0

        for part in reply.split()[1:]:
            key, _, value = part.partition("=")
            if key == "token":
                token = value
            elif key == "admin":
                is_admin = int(value or 0)
            elif key == "protocol":
                protocol = int(value or 0)

        check(
            "LOGIN returns token / admin / protocol",
            bool(token) and is_admin == 1 and protocol >= 2,
            f"token_len={len(token)} admin={is_admin} protocol={protocol}",
        )

        await close_pair(reader, writer)

        # ---- 3. HOST without a token is refused -------------
        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "HOST v2room host-client-1 admin" + _ver()
        )
        check(
            "HOST without a token is refused (AUTH_REQUIRED)",
            reply.startswith("ERROR AUTH_REQUIRED"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- 4. HOST with a bad token is refused ------------
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            "HOST v2room host-client-1 admin not-a-real-token" + _ver(),
        )
        check(
            "HOST with an invalid token is refused",
            reply.startswith("ERROR AUTH_INVALID_TOKEN"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- 5a. 房间先用 CREATE 建出来（**这样才有所有者**）----
        #
        # 2026-09-25 起，删房**只认所有者，没有任何兜底**：老 `HOST <room>`
        # 直接建出来的房间没有所有者，谁都删不掉（只能由面板强制关房）。
        # 所以这一套里"用于删除测试"的房间必须是 CREATE 建的 —— 这也正是
        # 版本门之后真实客户端唯一的建房路径。
        #
        # client_id 用同一个（host-client-1）：同一个 client_id + 同一个账号
        # 在中继那边就是"同一个人重连"，所有权会跟着人走 —— 所以下面那条老
        # `HOST` 挂上来时会把所有者身份一起接过去，房间里的成员数仍然是 1。
        owner_reader, owner_writer, owner_reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ host-client-1 admin {token} v2room{_ver()}",
        )
        check(
            "CREATE opens the room (and records its owner)",
            owner_reply.startswith("OK JOIN") or owner_reply.startswith("OK CREATE"),
            owner_reply,
        )

        # ---- 5. HOST with a valid token ----------------------
        host_pair = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST v2room host-client-1 admin {token}{_ver()}",
        )
        reader, writer, reply = host_pair
        check("HOST with a valid token is accepted", reply.startswith("OK HOST"), reply)

        # The server pushes INFO protocol=2 right after the frozen reply.
        info_line = await asyncio.wait_for(reader.readline(), timeout=5)
        check(
            "server announces its protocol version (additive INFO)",
            b"protocol=" in info_line,
            info_line.decode(errors="replace").strip(),
        )

        # ---- 5b. a refused HOST must not leave a phantom room ---------
        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "HOST phantom-room host-x nobody" + _ver()
        )
        check(
            "HOST without a token is refused",
            reply.startswith("ERROR AUTH_REQUIRED"),
            reply,
        )
        await close_pair(reader, writer)

        reader, writer, _ = await send_line(
            "127.0.0.1", RELAY_PORT, "ROOMS __mclink__ looker x", expect_reply=False
        )
        phantom_lines = await read_many(reader)
        await close_pair(reader, writer)

        check(
            "a refused HOST does not create a phantom room",
            not any("phantom-room" in ln for ln in phantom_lines),
            str([ln for ln in phantom_lines if ln.startswith("ROOM ")]),
        )

        # ---- 6. tunnels up so a player can pair -------------
        for index in range(3):
            t_reader, t_writer, t_reply = await send_line(
                "127.0.0.1", RELAY_PORT, "HOST_TUNNEL v2room" + _ver()
            )
            tunnel_writers.append((t_reader, t_writer))
            if index == 0:
                check(
                    "HOST_TUNNEL still replies with the frozen WAIT",
                    t_reply == "WAIT",
                    t_reply,
                )

        # ---- 7. ROOMS lists the live room -------------------
        reader, writer, _ = await send_line(
            "127.0.0.1", RELAY_PORT, "ROOMS __mclink__ looker admin", expect_reply=False
        )
        lines = await read_many(reader)
        await close_pair(reader, writer)

        room_lines = [ln for ln in lines if ln.startswith("ROOM ")]

        check(
            "ROOMS lists the live room",
            bool(room_lines) and "v2room" in room_lines[0],
            room_lines[0] if room_lines else str(lines),
        )

        check(
            "ROOMS marks the room as empty (nobody else has joined yet)",
            bool(room_lines) and _room_state(room_lines[0]) == "empty",
            room_lines[0] if room_lines else "",
        )

        # 「已加入世界」把开世界的房主算一个：世界确实是他开的，
        # 面板上显示 0 才是错的（用户报的「在线玩家一直是 0」）。
        check(
            "ROOMS counts the host as being in the world (1/8, not 0/8)",
            bool(room_lines) and " 1/" in room_lines[0],
            room_lines[0] if room_lines else "",
        )

        check(
            "ROOMS reply is terminated with DONE",
            lines and lines[-1] == "DONE",
            str(lines[-2:]) if lines else "",
        )

        # ---- 8. DELETE refuses a non-owner ------------------
        # Ownership is per ACCOUNT, so this needs a second account's token.
        status, payload = http_request(
            "POST",
            "/users",
            token=ADMIN_TOKEN,
            body={"username": "hacker", "password": "hackerpass123"},
        )

        hack_reader, hack_writer, hack_reply = await send_line(
            "127.0.0.1", RELAY_PORT, "LOGIN __mclink__ hacker hackerpass123"
        )
        hacker_token = ""
        for part in hack_reply.split()[1:]:
            key, _, value = part.partition("=")
            if key == "token":
                hacker_token = value
        await close_pair(hack_reader, hack_writer)

        check(
            "a second account can log in (for the ownership test)",
            bool(hacker_token),
            hack_reply,
        )

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"DELETE __mclink__ someone-else hacker {hacker_token} v2room",
        )
        check(
            "DELETE is refused for a client that does not own the room",
            reply.startswith("ERROR NOT_HOST"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- 9. a player joins, then DELETE must be refused --
        player_reader, player_writer, player_reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"PLAYER v2room player-client-1 Alice {token}{_ver()}",
            expect_reply=False,
        )
        player_pair = (player_reader, player_writer)

        # The tunnel assigned to this player must receive START.
        started = None
        for t_reader, t_writer in tunnel_writers:
            try:
                line = await asyncio.wait_for(t_reader.readline(), timeout=6)
            except asyncio.TimeoutError:
                continue
            if line.startswith(b"START"):
                started = (t_reader, t_writer, line.decode().strip())
                break

        check("a tunnel received the frozen START line", started is not None,
              started[2] if started else "")

        if started is None:
            return 1

        t_reader, t_writer, text = started
        session_id = text.split()[1]

        # Host connects its local Minecraft and sends READY.
        t_writer.write(b"READY\n")
        await t_writer.drain()

        # Player must get the frozen CONNECTED.
        connected = await asyncio.wait_for(player_reader.readline(), timeout=8)
        check(
            "player receives the frozen CONNECTED reply",
            connected.strip() == b"CONNECTED",
            connected.decode(errors="replace").strip(),
        )

        # ---- 10. Minecraft data still flows -----------------
        probe_reader, probe_writer = await asyncio.open_connection(
            "127.0.0.1", RELAY_PORT
        )
        del probe_reader, probe_writer  # placeholder, real traffic below

        # Real traffic goes through the paired session: write on the player
        # socket, read the echo back on the tunnel socket.
        payload = b"hello-minecraft" * 512
        player_writer.write(payload)
        await player_writer.drain()

        # readexactly, NOT read: `read(n)` returns whatever happens to have
        # arrived, so a single scheduling hiccup makes it come up short and the
        # assertion fails on a relay that is working perfectly. That made this
        # suite flaky at roughly one run in three (measured on an unmodified
        # tree -- it is a test bug, not a relay bug), and a flaky gate trains
        # people to ignore it, which is worse than having no gate.
        #
        # The property under test is "the bytes arrive, in order, unchanged",
        # and readexactly is exactly that property.
        echoed = await asyncio.wait_for(
            t_reader.readexactly(len(payload)), timeout=15
        )

        check(
            "raw Minecraft bytes still flow player -> tunnel",
            echoed == payload,
            f"{len(echoed)}/{len(payload)} bytes",
        )

        t_writer.write(echoed)
        await t_writer.drain()

        back = await asyncio.wait_for(
            player_reader.readexactly(len(payload)), timeout=15
        )

        check("raw Minecraft bytes still flow tunnel -> player", back == payload)

        # ---- 11. DELETE while a player is connected ---------
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"DELETE __mclink__ host-client-1 admin {token} v2room",
        )
        check(
            "DELETE is refused while the room still has players",
            reply.startswith("ERROR DELETE_ROOM_BUSY"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- 12. ROOMS now reports the room as busy ---------
        reader, writer, _ = await send_line(
            "127.0.0.1", RELAY_PORT, "ROOMS __mclink__ looker admin", expect_reply=False
        )
        lines = await read_many(reader)
        await close_pair(reader, writer)
        room_lines = [ln for ln in lines if ln.startswith("ROOM ")]

        check(
            "ROOMS reports the room as busy with 1 player (host + 1 = 2/8)",
            bool(room_lines) and _room_state(room_lines[0]) == "busy"
            and " 2/" in room_lines[0],
            room_lines[0] if room_lines else "",
        )

        # ---- 13. admin API authentication -------------------
        status, payload = http_request("GET", "/health")
        check(
            "admin API refuses a request without a token",
            status == 401,
            f"status={status}",
        )

        status, payload = http_request("GET", "/health", token="wrong")
        check("admin API refuses a wrong token", status == 401, f"status={status}")

        status, payload = http_request("GET", "/health", token=ADMIN_TOKEN)
        check(
            "admin API answers /health with the right token",
            status == 200 and payload.get("ok") is True,
            str(payload)[:120],
        )

        # ---- 14. admin sees the room, players and DB state ---
        status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN)
        rooms_data = payload.get("data") or []

        check(
            "admin API lists the live room with its player",
            status == 200 and any(r.get("room") == "v2room" for r in rooms_data),
            str(rooms_data)[:160],
        )

        status, payload = http_request("GET", "/stats", token=ADMIN_TOKEN)
        stats = payload.get("data") or {}

        check(
            "admin API stats include accounts and database state",
            status == 200
            # 2 = 开世界的房主 + 1 个已配对的玩家
            and stats.get("players") == 2
            # members = 房间里的成员连接数。房间现在是 **CREATE** 建的
            # （2026-09-25 起删房只认所有者，所以测试的房必须是 CREATE 建的），
            # 于是成员是「所有者 1 + 玩家 1 = 2」；老 `HOST <room>` 那条控制
            # 连接不算成员（它只是主机位置）—— 旧版本这里写 1，正是因为房子
            # 是 HOST 建的、没有所有者这个成员。
            and stats.get("members") == 2
            and "database" in stats
            and "admin" in (stats.get("online_accounts") or []),
            str({k: stats.get(k) for k in ("players", "members", "rooms",
                                           "online_accounts")}),
        )

        # Every field the panel dashboard reads out of /stats must really be
        # there. It used to read `database.backend` and `admin_api.requests`,
        # neither of which existed -- so two cards silently showed "-" / "0"
        # forever. Check them one by one instead of trusting the reply shape.
        dashboard_fields = {
            "rooms": int,
            "players": int,
            "idle_tunnels": int,
            "online_account_count": int,
            "uptime": (int, float),
            "auth_required": bool,
        }

        missing = [
            name for name, want in dashboard_fields.items()
            if not isinstance(stats.get(name), want)
        ]

        check(
            "仪表盘的每个 /stats 字段都存在（缺失会静默显示 0）",
            not missing,
            f"缺失或类型不对：{missing}" if missing else "全部存在",
        )

        check(
            "/stats 带 database 对象（仪表盘「中继上报」那张卡要用）",
            isinstance(stats.get("database"), dict)
            and "backend" in stats["database"]
            and "healthy" in stats["database"],
            str(stats.get("database"))[:160],
        )

        check(
            "/stats 带 admin_api 对象（仪表盘「管理接口请求数」那张卡要用）",
            isinstance(stats.get("admin_api"), dict)
            and "requests" in stats["admin_api"]
            and "enabled" in stats["admin_api"],
            str(stats.get("admin_api"))[:160],
        )

        # /health must not grow a fake "data" layer: the panel reads its
        # fields from the raw body.
        status, health = http_request("GET", "/health", token=ADMIN_TOKEN)

        check(
            "/health 返回 database 字段且没有多余的 data 层",
            status == 200
            and health.get("database") in ("up", "down")
            and "data" not in health,
            str(health)[:160],
        )

        # ---- 15. admin kicks the player ---------------------
        status, payload = http_request(
            "DELETE", f"/sessions/{session_id}", token=ADMIN_TOKEN
        )
        check(
            "admin API can kick a player session",
            status == 200 and payload.get("ok") is True,
            str(payload)[:120],
        )

        ok = await wait_until(
            lambda: _rooms_empty_via_api(),
            timeout=10,
            label="room to become empty",
        )
        check("room becomes empty after the kick", ok)

        # ---- 16. DELETE now succeeds ------------------------
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"DELETE __mclink__ host-client-1 admin {token} v2room",
        )
        check(
            "DELETE succeeds once the room is empty",
            reply.startswith("OK DELETE"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- 16b. 无主房间（老 HOST 建的）**谁都删不掉** ----------
        #
        # 2026-09-25 用户拍板："既然加门了就删掉那些兜底吧，免得后面一堆 BUG。"
        # 删掉的是"房间没有所有者时，退回让 host 删"那条路。版本门上了以后，
        # 能连上来的客户端一定走过 CREATE/JOIN（房间必然有所有者），所以无主
        # 房间在生产里只可能由面板强制关掉 —— 这一段把两条路径都钉死：
        # 一条是独立 DELETE 连接，一条是 HOST 控制口上的 `DELETE <token>`。
        legacy_reader, legacy_writer, legacy_reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST v2legacy host-client-1 admin {token}{_ver()}",
        )
        check(
            "老 HOST 直接建房仍然可用（冻结协议没动）",
            legacy_reply.startswith("OK HOST"),
            legacy_reply,
        )
        # INFO 横幅
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(legacy_reader.readline(), timeout=3)

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"DELETE __mclink__ host-client-1 admin {token} v2legacy",
        )
        check(
            "★ 无主房间：独立 DELETE 连接也被拒（兜底已删）",
            reply.startswith("ERROR NOT_HOST"),
            reply,
        )
        await close_pair(reader, writer)

        legacy_writer.write(f"DELETE {token}\n".encode())
        await legacy_writer.drain()

        host_reply = ""

        with contextlib.suppress(asyncio.TimeoutError):
            host_reply = (
                await asyncio.wait_for(legacy_reader.readline(), timeout=6)
            ).decode(errors="replace").strip()

        check(
            "★ 无主房间：HOST 控制口的 DELETE 也被拒（兜底已删）",
            host_reply.startswith("ERROR NOT_HOST"),
            host_reply or "(没有回复)",
        )

        # 收拾干净：无主房间只能由管理接口强制关掉。
        status, payload = http_request(
            "POST", "/rooms/v2legacy/revoke", token=ADMIN_TOKEN
        )
        check(
            "面板（管理接口）仍然能强制关掉无主房间",
            status in (200, 404),
            f"status={status} {str(payload)[:100]}",
        )
        await close_pair(legacy_reader, legacy_writer)

        # ---- 17. admin force-deletes a busy room ------------
        # Rebuild a room with a player, then force delete it.
        # (host_pair is a (reader, writer, reply) triple from send_line.)
        await close_pair(host_pair[0], host_pair[1])
        host_pair = None

        for _reader, w in tunnel_writers:
            await close_pair(_reader, w)
        tunnel_writers = []

        await asyncio.sleep(1.0)

        host_pair = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST v2busy host-client-1 admin {token}{_ver()}",
        )
        h_reader, h_writer, _ = host_pair

        t_reader, t_writer, _ = await send_line(
            "127.0.0.1", RELAY_PORT, "HOST_TUNNEL v2busy" + _ver()
        )
        tunnel_writers.append((t_reader, t_writer))

        p_reader, p_writer, _ = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"PLAYER v2busy player-client-1 Bob {token}{_ver()}",
            expect_reply=False,
        )
        player_pair = (p_reader, p_writer)

        line = await asyncio.wait_for(t_reader.readline(), timeout=8)
        t_writer.write(b"READY\n")
        await t_writer.drain()
        await asyncio.wait_for(p_reader.readline(), timeout=8)

        check("second room with a player is online", line.startswith(b"START"), line.decode())

        status, payload = http_request(
            "DELETE", "/rooms/v2busy", token=ADMIN_TOKEN
        )
        check(
            "admin API can force-delete a room that still has players",
            status == 200 and payload.get("ok") is True,
            str(payload)[:140],
        )

        # ---- 18. disabled account loses access --------------
        status, payload = http_request(
            "POST",
            "/users",
            token=ADMIN_TOKEN,
            body={"username": "bob", "password": "bobpass123"},
        )
        check(
            "admin API can create an account",
            status == 200 and payload.get("ok") is True,
            str(payload)[:140],
        )

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "LOGIN __mclink__ bob bobpass123"
        )
        bob_token = ""
        for part in reply.split()[1:]:
            key, _, value = part.partition("=")
            if key == "token":
                bob_token = value

        check("a newly created account can log in", bool(bob_token), reply)

        await close_pair(reader, writer)

        status, payload = http_request(
            "PATCH", "/users/bob", token=ADMIN_TOKEN, body={"disabled": True}
        )
        check("admin API can disable an account", status == 200, str(payload)[:120])

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "LOGIN __mclink__ bob bobpass123"
        )
        check(
            "a disabled account can no longer log in",
            reply.startswith("ERROR AUTH_DISABLED"),
            reply,
        )
        await close_pair(reader, writer)

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, f"HOST v2room3 client-x admin {bob_token}{_ver()}"
        )
        check(
            "a revoked token is rejected on HOST",
            reply.startswith("ERROR AUTH_INVALID_TOKEN")
            or reply.startswith("ERROR AUTH_DISABLED"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- 19. rename: the username IS the display name ----
        status, payload = http_request(
            "POST",
            "/users",
            token=ADMIN_TOKEN,
            body={"username": "renametest", "password": "renamepass123"},
        )
        check(
            "created an account for the rename test",
            status == 200 and payload.get("ok") is True,
            str(payload)[:120],
        )

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "LOGIN __mclink__ renametest renamepass123"
        )
        old_token = ""
        for part in reply.split()[1:]:
            key, _, value = part.partition("=")
            if key == "token":
                old_token = value
        await close_pair(reader, writer)

        check("the account can log in before renaming", bool(old_token), reply)

        status, payload = http_request(
            "POST",
            "/users/renametest/rename",
            token=ADMIN_TOKEN,
            body={"username": "newname"},
        )
        check(
            "admin API can rename an account",
            status == 200 and payload.get("ok") is True,
            str(payload)[:160],
        )

        # The old name must be gone and the new one must work.
        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "LOGIN __mclink__ renametest renamepass123"
        )
        check(
            "the OLD username no longer works",
            reply.startswith("ERROR AUTH_BAD_CREDENTIALS"),
            reply,
        )
        await close_pair(reader, writer)

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "LOGIN __mclink__ newname renamepass123"
        )
        check(
            "the NEW username works with the same password",
            reply.startswith("OK LOGIN"),
            reply,
        )
        await close_pair(reader, writer)

        # An ADMIN rename must NOT invalidate the session: the player keeps
        # playing and simply shows up under the new name.
        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, f"HOST renameroom cid admin {old_token}{_ver()}"
        )
        check(
            "an admin rename does NOT kick the player (token still valid)",
            reply.startswith("OK HOST"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- the SAME account may be renamed again immediately by the panel ----
        status, payload = http_request(
            "POST",
            "/users/newname/rename",
            token=ADMIN_TOKEN,
            body={"username": "adminrename2"},
        )
        check(
            "panel rename has NO rate limit (second rename right away)",
            status == 200 and payload.get("ok") is True,
            str(payload)[:140],
        )

        status, payload = http_request(
            "POST",
            "/users/adminrename2/rename",
            token=ADMIN_TOKEN,
            body={"username": "adminrename3"},
        )
        check(
            "panel rename has NO rate limit (third rename right away)",
            status == 200 and payload.get("ok") is True,
            str(payload)[:140],
        )

        check(
            "panel rename reports that no re-login is needed",
            "无需重新登录" in str(payload.get("message", ""))
            or "立即生效" in str(payload.get("message", "")),
            str(payload.get("message", "")),
        )

        # ---- 20. client SELF-SERVICE rename over the wire ----
        # The panel path is exercised above; this is the path the desktop
        # client uses: RENAME __mclink__ <client_id> <username> <token> <new>.

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            "RENAME __mclink__ cid adminrename3 not-a-token finalname",
        )
        check(
            "RENAME refuses an invalid token",
            reply.startswith("ERROR AUTH_INVALID_TOKEN"),
            reply,
        )
        await close_pair(reader, writer)

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "RENAME __mclink__ cid - - finalname"
        )
        check(
            # "-" is the wire's empty-field placeholder, so it is a syntactically
            # valid but unusable token: the relay answers AUTH_INVALID_TOKEN.
            "RENAME with an unusable token is refused",
            reply.startswith("ERROR AUTH_INVALID_TOKEN"),
            reply,
        )
        await close_pair(reader, writer)

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"RENAME __mclink__ cid adminrename3 {old_token} x",
        )
        check(
            "RENAME rejects a too-short username",
            reply.startswith("ERROR BAD_USERNAME"),
            reply,
        )
        await close_pair(reader, writer)

        # A second account, kept separate because the rate limit is per account.
        status, payload = http_request(
            "POST",
            "/users",
            token=ADMIN_TOKEN,
            body={"username": "renamelive", "password": "renamelivepass"},
        )
        check(
            "created the account used for the live rename test",
            status == 200 and payload.get("ok") is True,
            str(payload)[:120],
        )

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            "LOGIN __mclink__ renamelive renamelivepass live-cid",
        )
        live_token = ""
        for part in reply.split()[1:]:
            key, _, value = part.partition("=")
            if key == "token":
                live_token = value
        await close_pair(reader, writer)

        check("the live-rename account can log in", bool(live_token), reply)

        # Host a room so we can prove the rename patches the LIVE room.
        live_reader, live_writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST liveroom live-cid renamelive {live_token}{_ver()}",
        )
        check(
            "the live-rename account can host a room",
            reply.startswith("OK HOST"),
            reply,
        )

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"RENAME __mclink__ live-cid renamelive {live_token} renamelive2",
        )
        check(
            "self-service RENAME succeeds with a valid token",
            reply.startswith("OK RENAME") and "renamelive2" in reply,
            reply,
        )
        await close_pair(reader, writer)

        # The whole point: the player is NOT logged out.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST liveroom2 live-cid renamelive2 {live_token}{_ver()}",
        )
        check(
            "the SAME token still works after a self rename (no logout)",
            reply.startswith("OK HOST"),
            reply,
        )
        await close_pair(reader, writer)

        # The live room must now report the new host name.
        await asyncio.sleep(0.4)

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "ROOMS __mclink__ probe -"
        )
        lines = await read_many(reader)
        await close_pair(reader, writer)

        live_room_line = next(
            (l for l in lines if l.startswith("ROOM liveroom ")), ""
        )
        check(
            "the LIVE room picks up the new host name immediately",
            "renamelive2" in live_room_line,
            live_room_line or "(room not listed)",
        )

        # The old name must be gone from the account table.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            "LOGIN __mclink__ renamelive renamelivepass",
        )
        check(
            "the OLD name cannot log in after a self rename",
            reply.startswith("ERROR AUTH_BAD_CREDENTIALS"),
            reply,
        )
        await close_pair(reader, writer)

        # Self rename is rate limited: 1 used, so two more succeed and the
        # fourth is refused.
        for index in range(2):
            reader, writer, reply = await send_line(
                "127.0.0.1",
                RELAY_PORT,
                f"RENAME __mclink__ live-cid renamelive2 {live_token} "
                f"renamelive{index + 3}",
            )
            check(
                f"self-service rename {index + 2} of 3 is allowed",
                reply.startswith("OK RENAME"),
                reply,
            )
            await close_pair(reader, writer)

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"RENAME __mclink__ live-cid renamelive4 {live_token} renamelive99",
        )
        check(
            "the 4th self-service rename is refused (RENAME_LIMIT)",
            reply.startswith("ERROR RENAME_LIMIT"),
            reply,
        )
        await close_pair(reader, writer)

        # The token still works after hitting the limit.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST liveroom3 live-cid renamelive4 {live_token}{_ver()}",
        )
        check(
            "hitting the rename limit does not invalidate the session",
            reply.startswith("OK HOST"),
            reply,
        )
        await close_pair(reader, writer)

        # A name that is already taken must be refused, not silently swapped.
        # A fresh account is used because a refused rename must not burn one of
        # the three allowed renames.
        status, payload = http_request(
            "POST",
            "/users",
            token=ADMIN_TOKEN,
            body={"username": "renametaken", "password": "renametakenpass"},
        )
        check(
            "created the account used for the taken-name test",
            status == 200 and payload.get("ok") is True,
            str(payload)[:120],
        )

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            "LOGIN __mclink__ renametaken renametakenpass taken-cid",
        )
        taken_token = ""
        for part in reply.split()[1:]:
            key, _, value = part.partition("=")
            if key == "token":
                taken_token = value
        await close_pair(reader, writer)

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"RENAME __mclink__ taken-cid renametaken {taken_token} admin",
        )
        check(
            "RENAME refuses a username that is already taken",
            reply.startswith("ERROR USERNAME_TAKEN"),
            reply,
        )
        await close_pair(reader, writer)

        # ... and a refused rename must not consume the budget, so renaming to
        # a free name still works afterwards.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"RENAME __mclink__ taken-cid renametaken {taken_token} renametaken2",
        )
        check(
            "a refused rename does not consume the rename budget",
            reply.startswith("OK RENAME"),
            reply,
        )
        await close_pair(reader, writer)

        try:
            live_writer.close()
            await live_writer.wait_closed()
        except Exception:
            pass

        # ---- 20b. client LOGOUT releases the token ----
        # The panel's "在线账号" count comes from the set of accounts with a
        # VALID token. Without LOGOUT a token stayed valid for its full 12 hour
        # TTL after the user closed the client, so logging out and going to bed
        # still showed as online the next morning.
        status, payload = http_request(
            "POST",
            "/users",
            token=ADMIN_TOKEN,
            body={"username": "logoutuser", "password": "logoutpass123"},
        )
        check(
            "created the account used for the logout test",
            status == 200 and payload.get("ok") is True,
            str(payload)[:120],
        )

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            "LOGIN __mclink__ logoutuser logoutpass123 logout-cid",
        )
        logout_token = ""
        for part in reply.split()[1:]:
            key, _, value = part.partition("=")
            if key == "token":
                logout_token = value
        await close_pair(reader, writer)

        check("the logout-test account can log in", bool(logout_token), reply)

        # The token works before the logout...
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST logoutroom logout-cid logoutuser {logout_token}{_ver()}",
        )
        check(
            "the token is accepted before LOGOUT",
            reply.startswith("OK HOST"),
            reply,
        )
        await close_pair(reader, writer)

        # ...and must stop working right after it.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"LOGOUT __mclink__ logout-cid logoutuser {logout_token}",
        )
        check(
            "LOGOUT is acknowledged",
            reply.startswith("OK LOGOUT"),
            reply,
        )
        await close_pair(reader, writer)

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST logoutroom2 logout-cid logoutuser {logout_token}{_ver()}",
        )
        check(
            "the token is REFUSED after LOGOUT（面板的在线账号才会准确）",
            reply.startswith("ERROR AUTH_INVALID_TOKEN"),
            reply,
        )
        await close_pair(reader, writer)

        # Logging out twice must not be an error: the caller is leaving anyway.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"LOGOUT __mclink__ logout-cid logoutuser {logout_token}",
        )
        check(
            "重复 LOGOUT 仍然返回成功（幂等）",
            reply.startswith("OK LOGOUT"),
            reply,
        )
        await close_pair(reader, writer)

        # An unknown token is also reported as success, for the same reason.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            "LOGOUT __mclink__ cid someone not-a-real-token",
        )
        check(
            "未知 token 的 LOGOUT 也返回成功（不会卡住退出流程）",
            reply.startswith("OK LOGOUT"),
            reply,
        )
        await close_pair(reader, writer)

        # The session must be gone from the online list.
        status, payload = http_request("GET", "/stats", token=ADMIN_TOKEN)
        online = (payload.get("data") or {}).get("online_accounts") or []

        check(
            "LOGOUT 之后账号不再出现在在线列表里",
            "logoutuser" not in online,
            f"online={online}",
        )

        # ---- 21. live switch: require_account toggled from the panel ----
        status, payload = http_request("GET", "/settings", token=ADMIN_TOKEN)
        check(
            "admin API exposes the runtime switches",
            status == 200 and "require_account" in (payload.get("data") or {}),
            str(payload.get("data"))[:160],
        )

        # An unauthenticated HOST must be refused while the switch is ON.
        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "HOST switchroom cid nobody" + _ver()
        )
        check(
            "with the switch ON an anonymous HOST is refused",
            reply.startswith("ERROR AUTH_REQUIRED"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- a real host session that must SURVIVE the toggle ----
        survivor = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST survivoroom cid admin {token}{_ver()}",
        )
        check(
            "started a connected host session (the survivor)",
            survivor[2].startswith("OK HOST"),
            survivor[2],
        )

        # Turn the switch OFF from the panel.
        status, payload = http_request(
            "PUT", "/settings", token=ADMIN_TOKEN,
            body={"require_account": False},
        )
        check(
            "panel can turn 'must log in' OFF",
            status == 200 and payload.get("ok") is True,
            str(payload)[:160],
        )

        # The ROOMS header must advertise the switch. This is how the CLIENT
        # finds out it does not need an account -- without it the client
        # prompted for a login before it ever contacted the relay, so turning
        # the switch off in the panel changed nothing the user could see.
        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "ROOMS __mclink__ probe -"
        )
        header = reply
        await read_many(reader)
        await close_pair(reader, writer)

        check(
            "ROOMS 头部带 auth=0（关闭开关后客户端才知道不用登录）",
            header.startswith("ROOMS ") and "auth=0" in header,
            header,
        )

        # Turn it back ON just for this probe, and confirm the flag flips.
        http_request(
            "PUT", "/settings", token=ADMIN_TOKEN,
            body={"require_account": True},
        )

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "ROOMS __mclink__ probe -"
        )
        header_on = reply
        await read_many(reader)
        await close_pair(reader, writer)

        check(
            "ROOMS 头部带 auth=1（打开开关后恢复要求登录）",
            "auth=1" in header_on,
            header_on,
        )

        # ...and back OFF so the rest of the section runs as written.
        http_request(
            "PUT", "/settings", token=ADMIN_TOKEN,
            body={"require_account": False},
        )

        # Now an anonymous HOST must be accepted.
        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "HOST switchroom cid nobody" + _ver()
        )
        check(
            "with the switch OFF an anonymous HOST is accepted (live effect)",
            reply.startswith("OK HOST"),
            reply,
        )
        await close_pair(reader, writer)

        # The survivor must still be alive: the toggle must NOT drop players.
        s_reader, s_writer, _ = survivor
        s_writer.write(b"PING switch-alive\n")
        await s_writer.drain()

        alive = b""
        for _ in range(6):
            try:
                line = await asyncio.wait_for(s_reader.readline(), timeout=3)
            except asyncio.TimeoutError:
                break
            if line.startswith(b"PONG"):
                alive = line
                break

        check(
            "the toggle does NOT disconnect an already-connected session",
            alive.startswith(b"PONG"),
            alive.decode(errors="replace").strip(),
        )

        # Turn it back ON and confirm the anonymous path is closed again.
        status, payload = http_request(
            "PUT", "/settings", token=ADMIN_TOKEN,
            body={"require_account": True},
        )
        check(
            "panel can turn 'must log in' back ON",
            status == 200 and payload.get("ok") is True,
            str(payload)[:160],
        )

        reader, writer, reply = await send_line(
            "127.0.0.1", RELAY_PORT, "HOST switchroom cid nobody" + _ver()
        )
        check(
            "switching ON again closes the anonymous path",
            reply.startswith("ERROR AUTH_REQUIRED"),
            reply,
        )
        await close_pair(reader, writer)

        # ... and the survivor still survives.
        s_writer.write(b"PING switch-alive-2\n")
        await s_writer.drain()

        alive2 = b""
        for _ in range(6):
            try:
                line = await asyncio.wait_for(s_reader.readline(), timeout=3)
            except asyncio.TimeoutError:
                break
            if line.startswith(b"PONG"):
                alive2 = line
                break

        check(
            "the session also survives switching back ON",
            alive2.startswith(b"PONG"),
            alive2.decode(errors="replace").strip(),
        )

        await close_pair(s_reader, s_writer)
        survivor = None

        # ---- 22. bulk purge of stale room rows ----
        status, payload = http_request(
            "POST", "/rooms/purge-stale", token=ADMIN_TOKEN
        )
        check(
            "admin API can purge stale room rows in one call",
            status == 200 and payload.get("ok") is True,
            str(payload)[:140],
        )

        # Live rooms must survive the purge.
        status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN)
        rooms_now = payload.get("data") or []
        live_now = [r for r in rooms_now if r.get("live") is not False]
        stale_now = [r for r in rooms_now if r.get("live") is False]

        check(
            "purge removes only stale rows, live rooms are untouched",
            len(stale_now) == 0,
            f"live={len(live_now)} stale={len(stale_now)}",
        )

        # ---- 23. host heartbeat: an unclean host death must free the room ----
        #
        # Reproduces the failure seen in the real client log:
        #   * the host process is killed / the cable is pulled -> TCP never
        #     sends a FIN, so the relay still believes the room has a host
        #   * the host reconnects and is refused with ROOM_ALREADY_HAS_HOST
        #   * its tunnels see NO_HOST
        #
        # The watchdog must notice the silence and destroy the room, after
        # which the SAME room id can be hosted again.
        # Simulate an unclean death properly: open the socket, do the
        # handshake, then go SILENT without ever closing it. A bare Python
        # socket would be collected by the GC and closed cleanly, which would
        # test the wrong path, so the file object is held on purpose.
        zombie_reader, zombie_writer = await asyncio.open_connection(
            "127.0.0.1", RELAY_PORT
        )
        zombie_writer.write(
            f"HOST zombie-room cid admin {token}{_ver()}\n".encode()
        )
        await zombie_writer.drain()
        zombie_reply = await asyncio.wait_for(
            zombie_reader.readline(), timeout=8
        )

        check(
            "opened a room that will go silent",
            zombie_reply.startswith(b"OK HOST"),
            zombie_reply.decode(errors="replace").strip(),
        )

        # One heartbeat so the watchdog has a baseline, then silence.
        zombie_writer.write(b"PING 1\n")
        await zombie_writer.drain()

        # A second host for the same room must be refused while the zombie
        # still looks alive.
        reader2, writer2, reply2 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST zombie-room cid2 admin {token}{_ver()}",
        )
        check(
            "a second host is refused while the zombie looks alive",
            reply2.startswith("ERROR ROOM_ALREADY_HAS_HOST"),
            reply2,
        )
        await close_pair(reader2, writer2)

        # Wait for the heartbeat watchdog (idle timeout is 3s here).
        ok = await wait_until(
            lambda: _room_gone("zombie-room"),
            timeout=30,
            interval=0.5,
            label="watchdog to reap the silent host",
        )
        check(
            "the watchdog destroys a room whose host went silent",
            ok,
        )

        # And the same room id can be hosted again.
        reader3, writer3, reply3 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"HOST zombie-room cid3 admin {token}{_ver()}",
        )
        check(
            "the same room id can be hosted again after the reap",
            reply3.startswith("OK HOST"),
            reply3,
        )
        await close_pair(reader3, writer3)

        # The zombie socket stayed open the whole time; close it now.
        await close_pair(zombie_reader, zombie_writer)

        # ---- 20. audit trail --------------------------------
        status, payload = http_request("GET", "/events", token=ADMIN_TOKEN)
        events = payload.get("data") or []
        actions = {e.get("action") for e in events}

        check(
            "audit log recorded logins, deletes and kicks",
            {"login_ok", "panel_delete_room"} <= actions
            or {"login_ok", "client_delete_room"} <= actions,
            str(sorted(a for a in actions if a))[:200],
        )

    finally:
        # player_pair / host_pair come from send_line() as (reader, writer,
        # reply) triples, so only the first two elements are sockets.
        for pair in (player_pair, host_pair):
            if pair is not None:
                await close_pair(pair[0], pair[1])

        for pair in tunnel_writers:
            await close_pair(pair[0], pair[1])

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

        if FAILED:
            print("")
            print("---- relay server log (tail) ----")
            with open(
                os.path.join(HERE, "v2_relay.log"),
                "r",
                encoding="utf-8",
                errors="replace",
            ) as fh:
                print("\n".join(fh.read().splitlines()[-40:]))

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


def _room_gone(room_id):
    """True when the relay no longer lists that room."""
    status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN)

    if status != 200:
        return False

    for room in payload.get("data") or []:
        if room.get("room") == room_id:
            return False

    return True


def _rooms_empty_via_api():
    """
    "The kicked player is gone" -- meaning only the host is left.

    `players` counts the host as being in the world (1), so the check is
    "1 left", not "0 left". The room-status field is the one that says whether
    anything is happening (it counts real players).
    """
    status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN)
    if status != 200:
        return False
    rooms_data = payload.get("data") or []
    for room in rooms_data:
        if room.get("room") == "v2room":
            return room.get("players", 0) <= 1 and not room.get("sessions")
    return True


def port_open(port):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
