"""
CraftLink -- 会话生命周期回归测试（「幽灵会话」BUG）。

============================================================================
 这一套复现的是用户报的致命 BUG
============================================================================

用户原话：
  「客户端人在房间里，此时房间里3人，但显示已加入世界9人」
  「第二个舍友加入时，发现游戏连不上」

根因（两个现象同一个病）：
  中继在 PLAYER 与 HOST_TUNNEL **配对成功的那一刻**就把会话写进
  room.sessions，而显示人数是 `host + len(sessions)`。但"配上对"不等于
  "进了世界"—— 还要等房主客户端连本机 Minecraft 回 READY、玩家侧回
  CONNECTED。这段握手以前**没有超时**，所以任何一次"配上但对不上"都会
  永久占一个名额：

     1) 人数虚高（3 人显示 9 人）；
     2) 名额被幽灵占满 -> 后面的人配不上对 -> 游戏连不上。

修法：只有真的走到 CONNECTED 才算 established，显示只数 established；
      并且每一对都有握手 deadline，到点没成 => 拆掉、名额归还。

这一套用**真中继子进程 + 真协议行**验证这两点。

Run:
    python tests/session_lifecycle_test.py
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

RELAY_PORT = int(os.environ.get("MCLINK_TEST_LIFECYCLE_PORT", "19160"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_LIFECYCLE_ADMIN_PORT", "19161"))
ADMIN_TOKEN = "test-session-lifecycle-token"

#: 故意调短，测试才跑得快（生产默认 20s / 15s）
HANDSHAKE_TIMEOUT = 5
READY_TIMEOUT = 4

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail:
        line += f"  -- {detail}"

    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    (PASSED if ok else FAILED).append(name)


def http_request(method, path, body=None, timeout=8):
    url = f"http://127.0.0.1:{ADMIN_PORT}{path}"
    data = None
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
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


async def open_line(line, timeout=8):
    """Open a socket, send one line, read one reply, keep it open.

    只适用于"会立刻回一行"的连接（CREATE / JOIN / HOST / HOST_TUNNEL）。
    PLAYER 在配对上之前**不会回任何东西**（要等 CONNECTED），所以那种连接必须
    用 open_socket()，否则会一直卡在这里等，等到握手 deadline 都把会话收了。
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    data = await asyncio.wait_for(reader.readline(), timeout=timeout)

    return reader, writer, data.decode(errors="replace").strip()


async def open_socket(line, timeout=8):
    """发一行就走，**不等回复**（PLAYER 这类连接的用法）。"""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    return reader, writer


async def read_until(reader, prefix, timeout=8):
    """读到以 prefix 开头的那一行（跳过服务端主动推的 HOST_STATE / PONG）。"""
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            line = await asyncio.wait_for(
                reader.readline(), timeout=max(0.1, deadline - time.time())
            )
        except asyncio.TimeoutError:
            return ""

        if not line:
            return ""

        text = line.decode(errors="replace").strip()

        if text.upper().startswith(prefix.upper()):
            return text

    return ""


async def read_line(reader, timeout=8):
    """直接读一行（读不到就返回空串，不抛）。"""
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except asyncio.TimeoutError:
        return ""

    return data.decode(errors="replace").strip()


async def close_pair(reader, writer):
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()


def rooms_now():
    status, payload = http_request("GET", "/rooms")
    return payload.get("data") or []


def find_room(room_id):
    for row in rooms_now():
        if str(row.get("room")) == room_id:
            return row

    return None


def players_of(room_id):
    row = find_room(room_id)

    if row is None:
        return None

    return int(row.get("players") or 0)


async def main():
    print("=" * 64)
    print("CraftLink session lifecycle test (幽灵会话 / 人数虚高)")
    print("=" * 64)

    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(RELAY_PORT),
            "MCLINK_DB_ENABLED": "0",
            # 账号关掉：这一套测的是会话生命周期，不是登录
            "MCLINK_REQUIRE_ACCOUNT": "0",
            "MCLINK_ADMIN_API_ENABLED": "1",
            "MCLINK_ADMIN_API_HOST": "127.0.0.1",
            "MCLINK_ADMIN_API_PORT": str(ADMIN_PORT),
            "MCLINK_ADMIN_API_TOKEN": ADMIN_TOKEN,
            "MCLINK_STATUS_FILE": os.path.join(HERE, "session_lifecycle_status.json"),
            "MCLINK_STATUS_INTERVAL": "2",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            "MCLINK_MAX_PLAYERS": "4",
            # ★ 这两条是被测对象：握手与 READY 的时限
            "MCLINK_PAIR_HANDSHAKE_TIMEOUT": str(HANDSHAKE_TIMEOUT),
            "MCLINK_PAIR_READY_TIMEOUT": str(READY_TIMEOUT),
            "MCLINK_VERBOSE": "0",
        }
    )

    server_log = open(
        os.path.join(HERE, "session_lifecycle_relay.log"),
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

    open_conns = []

    try:
        ok = await wait_until(
            lambda: port_open(RELAY_PORT) and port_open(ADMIN_PORT),
            timeout=30,
            label="relay + admin api",
        )
        check("relay starts with the admin API", ok)

        if not ok:
            return 1

        # ============================================================
        # 1. 建房 + 认领主机 + 开服（HOST 控制连接）+ 一条空闲隧道
        # ============================================================
        r_owner, w_owner, reply = await open_line(
            "CREATE __mclink__ cid-owner owner - room-lifecycle 4"
        )
        open_conns.append((r_owner, w_owner))

        check("建房成功", reply.startswith("OK JOIN"), reply)

        room_id = reply.split("room=", 1)[1].split()[0] if "room=" in reply else ""

        w_owner.write(b"CLAIM_HOST\n")
        await w_owner.drain()

        claim = await read_until(r_owner, "OK CLAIM_HOST")

        check("认领主机", claim.startswith("OK CLAIM_HOST"), claim)

        r_host, w_host, host_reply = await open_line(f"HOST {room_id} cid-owner owner")
        open_conns.append((r_host, w_host))

        check("以主机身份开服", host_reply.startswith("OK HOST"), host_reply)

        await wait_until(
            lambda: players_of(room_id) is not None, timeout=10, label="room listed"
        )

        check(
            "刚开服、没人进来时「已加入世界」= 1（房主自己）",
            players_of(room_id) == 1,
            f"players={players_of(room_id)}",
        )

        # ============================================================
        # 2. ★ 幽灵会话：PLAYER 配上了对，但房主隧道不发 READY
        # ============================================================
        r_tunnel, w_tunnel, tunnel_reply = await open_line(f"HOST_TUNNEL {room_id} cid-owner")

        check("隧道进入等待队列", tunnel_reply == "WAIT", tunnel_reply)

        r_p1, w_p1 = await open_socket(f"PLAYER {room_id} cid-p1 alice")
        open_conns.append((r_p1, w_p1))

        # 隧道应该收到 START（配对成功）
        start_line = await read_line(r_tunnel)

        check("隧道收到 START（配对成功）", start_line.startswith("START"), start_line)

        open_conns.append((r_tunnel, w_tunnel))
        await asyncio.sleep(1.0)

        # ★ 核心断言：配对 ≠ 进入世界，人数不许涨
        check(
            "★ 配上对但没走完握手时，「已加入世界」仍然 = 1（不算幽灵）",
            players_of(room_id) == 1,
            f"players={players_of(room_id)}（修复前这里会是 2，且会一路涨到 9）",
        )

        check(
            "房间里的人数（members 口径）不受影响",
            find_room(room_id) is not None
            and int(find_room(room_id).get("members") or 0) >= 1,
            str(find_room(room_id)),
        )

        # ============================================================
        # 3. ★ 握手超时后：名额归还（这才是"第二个人连不上"的解药）
        # ============================================================
        await asyncio.sleep(HANDSHAKE_TIMEOUT + 2)

        check(
            "★ 握手超时后幽灵会话被回收（没有永久占着名额）",
            players_of(room_id) == 1,
            f"players={players_of(room_id)}",
        )

        # 幽灵的玩家 socket 应该被服务端关掉
        closed = False

        try:
            data = await asyncio.wait_for(r_p1.readline(), timeout=3)

            closed = data == b""
        except asyncio.TimeoutError:
            closed = False
        except Exception:
            closed = True

        check("幽灵会话的玩家连接被服务端关闭（不是挂着）", closed)

        # ============================================================
        # 4. 名额确实还回来了：新玩家能正常走完握手进世界
        # ============================================================
        r_t2, w_t2, t2_reply = await open_line(f"HOST_TUNNEL {room_id} cid-owner")
        check("新的空闲隧道可以进来", t2_reply == "WAIT", t2_reply)

        r_p2, w_p2 = await open_socket(f"PLAYER {room_id} cid-p2 bob")
        open_conns.append((r_p2, w_p2))

        start2 = await read_line(r_t2)

        check("新玩家配对上（名额没被幽灵占死）", start2.startswith("START"), start2)

        # 这次走完握手：隧道发 READY，玩家应该收到 CONNECTED
        w_t2.write(b"READY\n")
        await w_t2.drain()

        connected = await read_line(r_p2)

        check("走完握手后玩家收到 CONNECTED（冻结协议未变）", connected == "CONNECTED", connected)

        open_conns.append((r_t2, w_t2))

        await wait_until(
            lambda: players_of(room_id) == 2, timeout=6, label="established counted"
        )

        check(
            "★ 真的进了世界的玩家被正确计入：「已加入世界」= 2",
            players_of(room_id) == 2,
            f"players={players_of(room_id)}",
        )

        # 同一个玩家名重复连接 -> 去重，不该变成 3
        r_p3, w_p3 = await open_socket(f"PLAYER {room_id} cid-p3 bob")
        r_t3, w_t3, _ = await open_line(f"HOST_TUNNEL {room_id} cid-owner")
        open_conns.append((r_p3, w_p3))
        open_conns.append((r_t3, w_t3))

        await read_line(r_t3, timeout=6)

        w_t3.write(b"READY\n")
        await w_t3.drain()

        await read_line(r_p3, timeout=6)

        await asyncio.sleep(0.8)

        check(
            "同名玩家重复连接按人去重（不会数成两个人）",
            players_of(room_id) == 2,
            f"players={players_of(room_id)}",
        )

    finally:
        for reader, writer in open_conns:
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
