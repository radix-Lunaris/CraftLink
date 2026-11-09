"""
MC-Link -- 房间名（房间 ID）与主机位置的端到端测试。

============================================================================
 这一套为什么必须存在
============================================================================

用户的原话：「房间号那里改成房间名 任何房间名都应可以建房间」「房间ID你
自己生成去」「host断开连接后如果要手动释放，列表就必须还显示着他」。

这些都是"线上跑起来才知道对不对"的行为，所以这一套**真的起一个中继子进程**，
真的发协议行、真的开管理 API，而不是断言几个字符串。

覆盖：
  A. 房间名（中文 / 空格 / 斜杠）能建房、能被加入、重名被拒；
     玩家看到的是房间名，协议里走的是自动生成的房间 ID。
  B. 主机位置：认领后一直归认领的人 —— 开服、停服都不放手，
     只有明确释放 / 离开房间 / 被踢才会空出来（用户 A8 的要求）。
  C. 面板关房的两条路由（路径式 + body 式）对中文房间名都有效。

Run:
    python tests/room_name_host_test.py
"""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_NAME_PORT", "19140"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_NAME_ADMIN_PORT", "19141"))
ADMIN_TOKEN = "test-room-name-token"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================
# Helpers
# ============================================================

def http_request(method, path, body=None, timeout=40):
    url = f"http://127.0.0.1:{ADMIN_PORT}{path}"
    data = None
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(
                response.read().decode("utf-8") or "{}"
            )
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")

        with contextlib.suppress(Exception):
            return e.code, json.loads(payload or "{}")

        return e.code, {"raw": payload}


def port_open(port, host="127.0.0.1", timeout=0.4):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


async def wait_until(predicate, timeout=20, interval=0.25, label="condition"):
    deadline = time.time() + timeout

    while time.time() < deadline:
        if predicate():
            return True

        await asyncio.sleep(interval)

    print(f"  !! timeout waiting for {label}", flush=True)
    return False


def quote(name):
    from urllib.parse import quote as _q

    return _q(name, safe="")


def unquote(value):
    from urllib.parse import unquote as _u

    return _u(value)


async def open_line(line, timeout=8):
    """Open a connection, send one line, read one reply. Keep it open."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    data = await asyncio.wait_for(reader.readline(), timeout=timeout)

    return reader, writer, data.decode(errors="replace").strip()


async def close_pair(reader, writer):
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()


async def read_until(reader, prefix, timeout=8):
    """
    读到以 prefix 开头的那一行。

    成员连接是长连接，中继会往上面推 HOST_STATE / PONG，所以"发一条读一条"
    会读到推送而不是我们要的回执。这里按前缀找，把推送跳过。
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())

        try:
            line = await asyncio.wait_for(reader.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            return ""

        if not line:
            return ""

        text = line.decode(errors="replace").strip()

        if text.upper().startswith(prefix.upper()):
            return text

    return ""


def parse_kv(reply):
    """'OK JOIN room=x name=y owner=1' -> {'room': 'x', 'name': 'y', ...}"""
    out = {}

    for token in reply.split()[1:]:
        key, _, value = token.partition("=")

        if key:
            out[key] = value

    return out


def rooms_now():
    status, payload = http_request("GET", "/rooms")
    return payload.get("data") or []


def find_room(name, rows=None):
    """
    按【房间名】找一个房间。

    面板列表里 room 是房间 ID、name 是房间名；这里按 name 找，找不到就按
    room（兼容只有 ID 的旧行）。
    """
    for row in rows if rows is not None else rooms_now():
        if str(row.get("name") or "") == name or str(row.get("room")) == name:
            return row

    return None


async def main():
    print("=" * 64)
    print("MC-Link room NAME / host position test")
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
            "MCLINK_STATUS_FILE": os.path.join(HERE, "room_name_status.json"),
            "MCLINK_STATUS_INTERVAL": "2",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            "MCLINK_MAX_PLAYERS": "6",
            "MCLINK_VERBOSE": "0",
        }
    )

    log_path = os.path.join(HERE, "room_name_relay.log")
    server_log = open(log_path, "w", encoding="utf-8", errors="replace")

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

    open_connections = []

    try:
        ok = await wait_until(
            lambda: port_open(RELAY_PORT) and port_open(ADMIN_PORT),
            timeout=30,
            label="relay + admin api",
        )
        check("relay starts with the admin API", ok)

        if not ok:
            return 1

        # ---- login (accounts are on) ----
        _r, w, reply = await open_line(
            "LOGIN __mclink__ admin adminpass123"
        )
        token = reply.split("token=", 1)[1].split()[0] if "token=" in reply else ""
        await close_pair(_r, w)

        check("admin logs in", bool(token), reply)

        # ============================================================
        # A. 房间名：中文 + 空格，照样能建房
        # ============================================================
        NAME = "我的房间 一号"

        r1, w1, reply1 = await open_line(
            f"CREATE __mclink__ cid-owner admin {token} {quote(NAME)} 4"
        )
        open_connections.append((r1, w1))

        info1 = parse_kv(reply1)

        check(
            "中文带空格的房间名可以建房",
            reply1.startswith("OK JOIN"),
            reply1,
        )

        check(
            "协议里回来的是自动生成的房间 ID（ASCII，不是中文名）",
            bool(info1.get("room"))
            and info1["room"].isascii()
            and " " not in info1["room"]
            and info1["room"] != NAME,
            f"room={info1.get('room')!r}",
        )

        check(
            "同时把房间名原样告诉客户端（玩家看到的是名字）",
            unquote(info1.get("name", "")) == NAME,
            f"name={info1.get('name')!r}",
        )

        room_id = info1.get("room", "")

        # 同一个名字 = 同一个房间：再来一次 CREATE 必须被拒
        r2, w2, reply2 = await open_line(
            f"CREATE __mclink__ cid-other admin {token} {quote(NAME)} 4"
        )
        await close_pair(r2, w2)

        check(
            "同名建房被拒绝（房间名就是唯一标识）",
            reply2.startswith("ERROR ROOM_EXISTS"),
            reply2,
        )

        # 第二个人用同样的名字 JOIN -> 进的是同一个房间
        r3, w3, reply3 = await open_line(
            f"JOIN __mclink__ cid-friend admin {token} {quote(NAME)}"
        )
        open_connections.append((r3, w3))

        info3 = parse_kv(reply3)

        check(
            "另一个人用同名加入 → 同一个房间（同一个房间 ID）",
            reply3.startswith("OK JOIN")
            and info3.get("room") == room_id
            and info3.get("owner") == "0",
            reply3,
        )

        # 老客户端发的是裸名字（不编码）：也必须能建房、并且房间 ID 就是它
        r4, w4, reply4 = await open_line(
            f"CREATE __mclink__ cid-legacy admin {token} legacy-room"
        )
        info4 = parse_kv(reply4)
        await close_pair(r4, w4)

        check(
            "老客户端发裸名字也能建房（向后兼容）",
            reply4.startswith("OK JOIN") and info4.get("room") == "legacy-room",
            reply4,
        )

        # 斜杠：协议和面板接口都不能被它搞坏
        SLASH_NAME = "a/b 测试"
        r5, w5, reply5 = await open_line(
            f"CREATE __mclink__ cid-slash admin {token} {quote(SLASH_NAME)} 4"
        )
        open_connections.append((r5, w5))

        info5 = parse_kv(reply5)

        check(
            "带斜杠的房间名也能建（斜杠只是名字里的一个字符）",
            reply5.startswith("OK JOIN")
            and unquote(info5.get("name", "")) == SLASH_NAME,
            reply5,
        )

        slash_id = info5.get("room", "")

        check(
            "带斜杠的房间 ID 被编码过（不会变成路径分隔符）",
            "/" not in slash_id and "%2F" in slash_id.upper(),
            slash_id,
        )

        # ---- 房间列表里显示的是名字 ----
        rows = rooms_now()
        mine = find_room(NAME, rows)

        check(
            "房间列表里有这个房间，并且带上了房间名",
            mine is not None and str(mine.get("name")) == NAME,
            str(mine),
        )

        check(
            "房间列表里 room 仍是 ASCII 房间 ID（面板操作用它）",
            mine is not None and str(mine.get("room")) == room_id,
            str(mine),
        )

        # ============================================================
        # B. 主机位置：认领之后一直归认领的人
        # ============================================================
        _r, w, reply = await open_line("PING x")  # 占位，确保 socket 可用
        await close_pair(_r, w)

        # 认领主机
        w1.write(b"CLAIM_HOST\n")
        await w1.drain()
        claim_reply = await read_until(r1, "OK CLAIM_HOST")

        if not claim_reply:
            claim_reply = await read_until(r1, "ERROR")

        check("建房的人可以认领主机", claim_reply.startswith("OK CLAIM_HOST"), claim_reply)

        await asyncio.sleep(0.4)

        row = find_room(NAME, rooms_now())

        check(
            "列表的「当前主机」= 认领的人（不是房主那一列）",
            row is not None and str(row.get("host_now")) == "admin",
            str(row),
        )

        check(
            "还没开服 → host_live=0（列表要能显示「未开服」）",
            row is not None and row.get("host_online") is False,
            f"host_online={row.get('host_online') if row else None}",
        )

        # ---- 开服（真的开一条 HOST 数据连接）----
        rh, wh, host_reply = await open_line(
            f"HOST {room_id} cid-owner admin {token}"
        )
        check(
            "以主机身份开服（HOST 用房间 ID）",
            host_reply.startswith("OK HOST"),
            host_reply,
        )

        await wait_until(
            lambda: (find_room(NAME, rooms_now()) or {}).get("host_online") is True,
            timeout=6,
            label="host online",
        )

        row = find_room(NAME, rooms_now())
        check(
            "开服后列表显示 host_live=1",
            row is not None and row.get("host_online") is True,
            f"host_online={row.get('host_online') if row else None}",
        )

        # ---- 停止（关掉 HOST 连接）→ 位置必须【还在】----
        await close_pair(rh, wh)

        await wait_until(
            lambda: (find_room(NAME, rooms_now()) or {}).get("host_online") is False,
            timeout=8,
            label="host offline",
        )

        row = find_room(NAME, rooms_now())

        check(
            "★ 停止开服后主机位置【仍然归他】（用户 A8：不能显示无人认领）",
            row is not None and str(row.get("host_now")) == "admin",
            str(row),
        )

        check(
            "停止后列表显示 host_live=0（位置在、但没在开服）",
            row is not None and row.get("host_online") is False,
            f"host_online={row.get('host_online') if row else None}",
        )

        # 别人抢不走
        w3.write(b"CLAIM_HOST\n")
        await w3.drain()
        taken_reply = await read_until(r3, "ERROR")

        check(
            "位置还占着时，别人认领会被拒绝",
            "OK CLAIM_HOST" not in taken_reply and bool(taken_reply),
            taken_reply or "（没有收到拒绝）",
        )

        # ---- 明确释放 → 位置空出来 ----
        w1.write(b"RELEASE_HOST\n")
        await w1.drain()
        release_reply = await read_until(r1, "OK RELEASE_HOST")

        check(
            "明确释放主机身份",
            release_reply.startswith("OK RELEASE_HOST"),
            release_reply or "（没收到回执）",
        )

        await wait_until(
            lambda: str((find_room(NAME, rooms_now()) or {}).get("host_now") or "") == "",
            timeout=8,
            label="host freed",
        )

        row = find_room(NAME, rooms_now())

        check(
            "释放之后列表才显示无人认领",
            row is not None and not str(row.get("host_now") or ""),
            str(row),
        )

        # 现在别人可以认领
        w3.write(b"CLAIM_HOST\n")
        await w3.drain()
        reply3b = await read_until(r3, "OK CLAIM_HOST")

        check("释放之后别人可以认领", reply3b.startswith("OK CLAIM_HOST"), reply3b or "（没收到）")

        # ============================================================
        # C. 面板关房：两条路由 + 中文房间名
        # ============================================================
        status, payload = http_request(
            "POST", "/rooms/revoke", {"room": room_id, "reason": "测试：body 路由"}
        )

        check(
            "面板可以用 body 路由关掉中文名房间（room ID）",
            status == 200 and payload.get("ok") is True,
            f"status={status} {payload}",
        )

        check(
            "关房回执里写的是房间名，不是那串 %E6%88%91",
            NAME in str(payload.get("message", "")),
            str(payload.get("message")),
        )

        # 成员应该收到 ROOM REVOKED（协同关闭的第一步）
        revoked_line = await read_until(r3, "ROOM REVOKED", timeout=10)

        check(
            "房间成员收到 ROOM REVOKED（客户端据此立刻清理界面）",
            revoked_line.startswith("ROOM REVOKED"),
            revoked_line or "（超时，什么都没收到）",
        )

        await wait_until(
            lambda: find_room(NAME) is None,
            timeout=10,
            label="room gone",
        )

        check("关房后列表里不再有这个房间", find_room(NAME) is None)

        # 路径式路由（老面板）也要能用，并且要能吃下带斜杠的名字
        status, payload = http_request(
            "POST",
            "/rooms/" + urllib.parse.quote(slash_id, safe="") + "/revoke",
            {"reason": "测试：路径路由"},
        )

        check(
            "面板的路径式路由仍然可用（老面板兼容）",
            status == 200 and payload.get("ok") is True,
            f"status={status} {payload}",
        )

    finally:
        for reader, writer in open_connections:
            await close_pair(reader, writer)

        relay.terminate()

        with contextlib.suppress(Exception):
            relay.wait(timeout=10)

        if relay.poll() is None:
            relay.kill()

        server_log.close()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
