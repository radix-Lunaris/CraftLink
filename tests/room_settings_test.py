"""
MC-Link -- room capacity + room closing, end to end.

Two things the panel does that had no test at all, and both of them were
reported broken from the outside:

  1. 「单个房间的人数上限」 is a RUNTIME setting. It has to change the capacity
     of a room that is created AFTERWARDS -- not only after a relay restart.
     A capacity read that bypassed the settings service behaved exactly like
     "the panel did not save anything", so the test raises the limit in the
     middle of a running relay and then walks one more member in.

  2. Closing / deleting a room has to work from the panel's two buttons:
        room_revoke  -> POST /rooms/<room>/revoke   (unconditional destroy)
        room_delete  -> revoke first, DELETE only as the fallback for a row
                        that is left over in the database
     The old panel sent DELETE for a live room, and DELETE only ever meant
     "remove a leftover row", so it answered 房间不存在 for a room the admin
     could see on screen.

Run:
    python tests/room_settings_test.py
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
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_ROOM_PORT", "19120"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_ROOM_ADMIN_PORT", "19121"))

ADMIN_TOKEN = "test-room-settings-token"

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
# Helpers
# ============================================================

def http_request(method, path, token=None, body=None, timeout=8):
    url = f"http://127.0.0.1:{ADMIN_PORT}{path}"

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


async def send_line(host, port, line, timeout=8):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    data = await asyncio.wait_for(reader.readline(), timeout=timeout)

    return reader, writer, data.decode(errors="replace").strip()


async def read_reply(reader, prefixes, timeout=8, label=""):
    """
    等一条以 prefixes 之一开头的回复，跳过中间的广播行。

    为什么需要它：成员控制连接上**同时**跑着两类东西 —— 我们请求的应答，和
    中继主动推的广播（HOST_STATE ...：有人进出房间、有人认领/释放主机）。
    广播随时可能插在应答前面，所以"读下一行"并不等于"读我的应答"。
    第一版这几条断言就是这么失败的：读到的是 HOST_STATE，不是 ERROR。
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(
                reader.readline(), timeout=max(0.1, deadline - time.time())
            )
        except asyncio.TimeoutError:
            return ""

        if not raw:
            return ""

        text = raw.decode("utf-8", errors="replace").strip()

        if not text:
            continue

        if any(text.startswith(prefix) for prefix in prefixes):
            return text

        # 广播 / 别的行：跳过，继续等我们的应答。

    print(f"  !! 等 {label or prefixes} 超时", flush=True)
    return ""


async def login(username, password):
    reader, writer, reply = await send_line(
        "127.0.0.1",
        RELAY_PORT,
        f"LOGIN __mclink__ {username} {password}",
    )

    token = ""

    if "token=" in reply:
        token = reply.split("token=", 1)[1].split()[0]

    writer.close()

    return reply, token


async def close_pair(reader, writer):
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


def rooms_now():
    status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN)
    return payload.get("data") or []


def room_ids():
    return sorted(str(r.get("room")) for r in rooms_now())


def room_display_names():
    """
    面板列表里的【房间名】（人看到的那个）。

    房间现在有两个标识：room = ASCII 房间 ID（协议/接口用），
    name = 玩家输入的房间名（中文/空格都行）。检查"某个名字的房间在不在"
    要用这个函数，不是 room_ids()。
    """
    return sorted(str(r.get("name") or r.get("room")) for r in rooms_now())


def room_now_rows():
    """Every room the panel would list, live or leftover."""
    return [
        (str(r.get("room")), bool(r.get("live", True)))
        for r in rooms_now()
    ]


def find_room(room):
    for row in rooms_now():
        if str(row.get("room")) == room:
            return row

    return None


def set_setting(name, value):
    return http_request(
        "PUT", "/settings", token=ADMIN_TOKEN, body={name: value}
    )


# ============================================================
# Main
# ============================================================

async def main():
    print("=" * 64)
    print("MC-Link room capacity + room closing test")
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
            "MCLINK_STATUS_FILE": os.path.join(HERE, "room_status.json"),
            "MCLINK_STATUS_INTERVAL": "2",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            # 2026-09-29「连接和身份是两回事」（AGENTS.md 规则 6 补充）：
            # 成员控制连接掉线、但 token 仍有效，座位（含 Owner 身份）会保留
            # 一个宽限期等他重连 —— 房间不会在断线的那一瞬间就消失。
            # 这一套测的是"不会留下幽灵房间"，所以把宽限压到 3 秒，
            # 然后等宽限期走完再看房间是否真的销毁。
            "MCLINK_MEMBER_SEAT_GRACE": "3",
            # The room capacity from the environment. The panel must be able to
            # move away from this value WITHOUT a restart.
            "MCLINK_MAX_PLAYERS": "5",
            "MCLINK_VERBOSE": "1",
        }
    )

    server_log = open(
        os.path.join(HERE, "room_settings_relay.log"),
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

    members = []
    stop_pinger = asyncio.Event()

    async def pinger():
        """
        Keep the member connections alive.

        A member that goes quiet for MEMBER_IDLE_TIMEOUT is reaped by the
        relay's watchdog. The real client pings every 3 s; this stands in for
        that, and covers the PING path while it is at it.

        It only WRITES. Reading here would steal the ROOM REVOKED line the test
        is waiting for.
        """
        while not stop_pinger.is_set():
            for _reader, writer in list(members):
                try:
                    writer.write(b"PING test\n")
                    await writer.drain()
                except Exception:
                    pass

            try:
                await asyncio.wait_for(stop_pinger.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

    pinger_task = asyncio.create_task(pinger())

    try:
        ok = await wait_until(
            lambda: port_open(RELAY_PORT) and port_open(ADMIN_PORT),
            timeout=25,
            label="relay + admin api",
        )
        check("relay starts with the admin API", ok)

        if not ok:
            return 1

        # ---- 1. the setting is reported with its bounds --------------
        status, payload = http_request("GET", "/settings", token=ADMIN_TOKEN)
        data = payload.get("data") or {}
        cap = data.get("max_players") or {}

        check(
            "GET /settings reports the room capacity",
            status == 200 and cap.get("type") == "int",
            f"status={status} max_players={cap}",
        )
        check(
            # ★ M1（D12）：上限 2-8 -> 2-64。载体（面板/管理 API）本来就要把
            #   边界原样报出去，这里钉的是"边界真的变成了 64"，不是文案。
            "the capacity carries its bounds for the panel (M1: 2-64)",
            cap.get("minimum") == 2 and cap.get("maximum") == 64,
            f"min={cap.get('minimum')} max={cap.get('maximum')}",
        )
        check(
            "the capacity starts at the environment value (MCLINK_MAX_PLAYERS=5)",
            cap.get("value") == 5,
            f"value={cap.get('value')}",
        )

        # ---- 2. the panel's own requests ----------------------------
        #
        # The panel sends the value AS TYPED. It used to force it into a
        # boolean, so "6" arrived as false and the relay answered
        # "必须是数字" -- for a number the user had typed correctly.
        status, payload = set_setting("max_players", "6")
        check(
            "a number typed in the panel is accepted",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

        status, payload = http_request("GET", "/settings", token=ADMIN_TOKEN)
        check(
            "the new capacity is read back",
            ((payload.get("data") or {}).get("max_players") or {}).get("value") == 6,
            f"max_players={((payload.get('data') or {}).get('max_players'))}",
        )

        status, payload = set_setting("max_players", "99")
        check(
            "an out-of-range capacity is refused with the reason",
            status == 400 and "2-64" in str(payload.get("error", "")),
            f"status={status} error={payload.get('error')}",
        )

        status, payload = set_setting("max_players", "abc")
        check(
            "a non-numeric capacity is refused",
            status == 400,
            f"status={status} error={payload.get('error')}",
        )

        status, payload = http_request("GET", "/stats", token=ADMIN_TOKEN)
        check(
            "the relay reports the live capacity in /stats",
            (payload.get("data") or {}).get("max_players") == 6,
            f"max_players={(payload.get('data') or {}).get('max_players')}",
        )

        # ---- 3. it applies to a room created afterwards -------------
        reply, token = await login("admin", "adminpass123")
        check("admin logs in", token != "", reply)

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ cap-cid admin {token} caproom{_ver()}",
        )
        check(
            "CREATE opens a room",
            reply.startswith("OK JOIN") and "owner=1" in reply,
            reply,
        )
        members.append((reader, writer))

        room = find_room("caproom")
        check(
            "the room reports the live capacity",
            room is not None and int(room.get("max_players") or 0) == 6,
            f"room={room}",
        )

        # Fill it up: 6 people (owner included).
        for index in range(2, 7):
            reader, writer, reply = await send_line(
                "127.0.0.1",
                RELAY_PORT,
                f"JOIN __mclink__ cap-{index} admin {token} caproom{_ver()}",
            )
            check(f"member {index} joins", reply.startswith("OK JOIN"), reply)
            members.append((reader, writer))

        # The 7th must be refused: the room holds 6.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"JOIN __mclink__ cap-7 admin {token} caproom{_ver()}",
        )
        check(
            "the room refuses one person too many",
            reply.startswith("ERROR ROOM_FULL"),
            reply,
        )
        await close_pair(reader, writer)

        # ---- 4. raising the limit takes effect WITHOUT a restart ----
        #
        # This is the whole point of the setting. The relay is the same
        # process it was a moment ago.
        status, payload = set_setting("max_players", "8")
        check(
            "the panel raises the limit to 8",
            status == 200,
            f"status={status} body={payload}",
        )

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"JOIN __mclink__ cap-8 admin {token} caproom{_ver()}",
        )
        check(
            "the same room now accepts the person it refused (live effect)",
            reply.startswith("OK JOIN"),
            reply,
        )
        members.append((reader, writer))

        status, payload = http_request("GET", "/stats", token=ADMIN_TOKEN)
        check(
            "/stats follows the change as well",
            (payload.get("data") or {}).get("max_players") == 8,
            f"max_players={(payload.get('data') or {}).get('max_players')}",
        )

        # ---- 5. the panel's close button ----------------------------
        status, payload = http_request(
            "POST",
            "/rooms/caproom/revoke",
            token=ADMIN_TOKEN,
            body={"reason": "测试强制关闭"},
        )
        check(
            "POST /rooms/<room>/revoke closes a live room with people in it",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

        # The members must have been TOLD, not just disconnected: a client
        # that is not told treats the drop as a network blip and reconnects
        # into a room that no longer exists.
        #
        # Earlier broadcasts (HOST_STATE) may still be in the buffer, so read
        # until the line we are looking for, or until the relay hangs up.
        told = False

        for _ in range(20):
            try:
                data = await asyncio.wait_for(members[0][0].readline(), timeout=5)
            except Exception:
                break

            if not data:
                break

            text = data.decode(errors="replace").strip()

            if text.startswith("ROOM REVOKED"):
                told = True
                break

        check("every member is told ROOM REVOKED before being disconnected", told)

        check(
            "the closed room is gone from the panel's list",
            "caproom" not in room_ids(),
            f"rooms={room_ids()}",
        )

        status, payload = http_request(
            "POST", "/rooms/caproom/revoke", token=ADMIN_TOKEN,
            body={"reason": "再来一次"},
        )
        check(
            "revoking it a second time answers 404 instead of pretending",
            status == 404 and payload.get("ok") is False,
            f"status={status} body={payload}",
        )

        # ---- 6. the panel's fallback for a leftover row -------------
        status, payload = http_request(
            "DELETE", "/rooms/caproom", token=ADMIN_TOKEN
        )
        check(
            "DELETE on a room that is nowhere reports what it checked",
            status == 404 and "caproom" in str(payload.get("message", "")),
            f"status={status} message={payload.get('message')}",
        )

        # ---- 7. DELETE still works for a live room ------------------
        #
        # Kept working on purpose: the relay's own DELETE is the second half
        # of the panel's room_delete fallback, and it must not answer
        # "房间不存在" for a room that is right there in memory.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ del-cid admin {token} delroom{_ver()}",
        )
        check("CREATE opens the room we are about to delete", reply.startswith("OK JOIN"), reply)
        members.append((reader, writer))

        status, payload = http_request("DELETE", "/rooms/delroom", token=ADMIN_TOKEN)
        check(
            "DELETE closes a live room (panel room_delete fallback)",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )
        check(
            "the deleted room is gone from the list",
            "delroom" not in room_ids(),
            f"rooms={room_ids()}",
        )

        # ---- 8. the owner's own connection may close its room -------
        #
        # This is what the client's 「关闭房间」 button does when it is not in a
        # session: a short lived DELETE connection.
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ own-cid admin {token} ownroom{_ver()}",
        )
        check("CREATE opens the owner's room", reply.startswith("OK JOIN"), reply)

        # A second connection deletes it, which is the shape the client uses.
        reader2, writer2, reply2 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"DELETE __mclink__ own-cid admin {token} ownroom",
        )
        check(
            "the owner can delete their own room over DELETE",
            reply2.startswith("OK DELETE"),
            reply2,
        )

        await close_pair(reader2, writer2)

        check(
            "the owner's room is gone",
            "ownroom" not in room_ids(),
            f"rooms={room_ids()}",
        )

        await close_pair(reader, writer)

        # The owner's control connection goes away with the room; the relay
        # must not resurrect it or crash on the cleanup.
        await asyncio.sleep(0.5)
        check(
            "the relay is still serving after all of that",
            port_open(RELAY_PORT),
            "port closed",
        )

        # ---- 9. root 的身份与权限（面板提权/建号全靠这条链）----
        #
        # The panel reads `role` from the RELAY (login reply, and the actor
        # lookup when creating/promoting), so a relay that reports root as a
        # plain admin makes the owner of the server unable to create
        # administrators or promote anybody -- while the panel's own account
        # list (which reads the column from SQL) happily shows 根管理员.
        # That contradiction was reported as 「我登录的不是根账号吗？」
        reply, root_token = await login("admin", "adminpass123")

        check(
            "LOGIN 回复里 root 的 role=root（没有被降级成 admin）",
            "role=root" in reply,
            reply,
        )

        status, payload = http_request(
            "POST", "/users", token=ADMIN_TOKEN,
            body={
                "username": "newadmin",
                "password": "newpass123",
                "role": "admin",
                "actor": "admin",
            },
        )
        check(
            "root 能创建管理员账号（actor 校验通过、不是 403）",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

        reply, _ = await login("newadmin", "newpass123")

        check(
            "新建的管理员登录后确实是 admin，而不是玩家",
            "role=admin" in reply,
            reply,
        )

        status, payload = http_request(
            "POST", "/users", token=ADMIN_TOKEN,
            body={
                "username": "trying",
                "password": "newpass123",
                "role": "admin",
                "actor": "newadmin",
            },
        )
        check(
            "普通管理员不能创建管理员账号（403 由中继判定，不只是界面锁）",
            status == 403,
            f"status={status} body={payload}",
        )

        # ---- 10. 提升 / 降级 ----
        http_request(
            "POST", "/users", token=ADMIN_TOKEN,
            body={
                "username": "playerone",
                "password": "newpass123",
                "role": "player",
                "actor": "admin",
            },
        )

        status, payload = http_request(
            "PATCH", "/users/playerone", token=ADMIN_TOKEN,
            body={"role": "admin", "actor": "admin"},
        )
        check(
            "root 能把已有账号提升为管理员",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

        reply, _ = await login("playerone", "newpass123")

        check(
            "提升后该账号登录就是 admin",
            "role=admin" in reply,
            reply,
        )

        status, payload = http_request(
            "PATCH", "/users/playerone", token=ADMIN_TOKEN,
            body={"role": "player", "actor": "admin"},
        )
        check(
            "root 能把管理员降回玩家",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

        status, payload = http_request(
            "PATCH", "/users/newadmin", token=ADMIN_TOKEN,
            body={"role": "player", "actor": "newadmin"},
        )
        check(
            "普通管理员不能改别人的权限",
            status == 400 and "根管理员" in str(payload.get("message", "")),
            f"status={status} body={payload}",
        )

        status, payload = http_request(
            "PATCH", "/users/admin", token=ADMIN_TOKEN,
            body={"role": "player", "actor": "admin"},
        )
        check(
            "root 自己不能被降级（否则没人能管理面板了）",
            status == 400,
            f"status={status} body={payload}",
        )

        # ---- 11. 房间人数口径 + 幽灵房间 ----
        #
        # 「已加入世界」必须把开世界的那个位置算进去，否则房主独自在游戏里
        # 时面板显示 0；「房间内在线」是房间里的人（可能还没进世界）。
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ count-cid admin {root_token} countroom{_ver()}",
        )
        check("CREATE 建了用于人数统计的房间", reply.startswith("OK JOIN"), reply)

        # CLAIM_HOST 是发在【成员控制连接】上的裸命令（不是新开一条连接），
        # 所以这里直接写在刚 CREATE 出来的那条 socket 上。
        writer.write(b"CLAIM_HOST\n")
        await writer.drain()

        reply2 = ""

        for _ in range(20):
            try:
                data = await asyncio.wait_for(reader.readline(), timeout=5)
            except Exception:
                break

            if not data:
                break

            text = data.decode(errors="replace").strip()

            if text.startswith(("OK CLAIM_HOST", "ERROR")):
                reply2 = text
                break

        check("成员身份可以认领主机", reply2.startswith("OK CLAIM_HOST"), reply2)

        await asyncio.sleep(0.5)

        room = find_room("countroom")

        check(
            "认领主机后「已加入世界」= 1（房主自己算一个）",
            room is not None and int(room.get("players") or 0) == 1,
            f"room={room}",
        )
        check(
            "「房间内在线」= 1（人在房间里）",
            room is not None and int(room.get("members") or 0) == 1,
            f"room={room}",
        )

        # 所有者断开 = 房间必须消失，而且数据库行也要跟着走（幽灵房间）。
        #
        # 注意宽限期（MCLINK_MEMBER_SEAT_GRACE=3，见 relay 环境）：token 仍有
        # 效的掉线**先保留座位**（连接≠身份），宽限期走完才按"真正离开"销毁。
        # 所以这里等的是"宽限 + 销毁"，不是"断线即销毁"。
        await close_pair(reader, writer)

        gone = await wait_until(
            lambda: find_room("countroom") is None,
            timeout=25,
            interval=0.5,
            label="room to disappear after the owner left (seat grace + destroy)",
        )

        check(
            "所有者关掉客户端后房间自动销毁（不是幽灵房间）",
            gone,
            f"rooms={room_ids()}",
        )

        await asyncio.sleep(0.5)

        check(
            "销毁后数据库里也不留残留行（面板列表里不再出现 live:false 的记录）",
            "countroom" not in room_ids(),
            f"rooms={room_now_rows()}",
        )

        # ---- 12. 人数：房间按客户端设置，服务器只是上限 ----
        #
        # 用户报的原话：服务器设 6、客户端设 8 → 房间建得出来，但连上去卡死
        # 超时；客户端改 5 就能连，可房间列表还是显示 6（服务器的数字）。
        # 正确的关系是：服务器 = 天花板，客户端 = 这个房间实际用几个。
        status, payload = set_setting("max_players", "6")
        check("把服务器上限设成 6", status == 200, f"status={status}")

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ cap-cid admin {root_token} caproom2 5{_ver()}",
        )
        check(
            "建房时带上自己的容量（5）",
            reply.startswith("OK JOIN") and "max_players=5" in reply,
            reply,
        )
        check(
            "登录/建房回复里带上服务器上限，客户端才能提前判断",
            "server_max_players=6" in reply,
            reply,
        )

        room = find_room("caproom2")
        check(
            "房间列表显示的是客户端设置（5），不是服务器上限（6）",
            room is not None and int(room.get("max_players") or 0) == 5,
            f"room={room}",
        )

        members.append((reader, writer))

        # 客户端要的比服务器上限还大 → 按服务器上限执行
        reader2, writer2, reply2 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ cap-cid2 admin {root_token} caproom3 8{_ver()}",
        )
        check(
            "客户端要 8、服务器上限 6 时，建房仍成功",
            reply2.startswith("OK JOIN"),
            reply2,
        )

        room3 = find_room("caproom3")
        check(
            "但房间容量被夹到服务器上限（6）",
            room3 is not None and int(room3.get("max_players") or 0) == 6,
            f"room={room3}",
        )

        members.append((reader2, writer2))

        # 改服务器上限不许影响已经开着的房间
        status, payload = set_setting("max_players", "7")
        room = find_room("caproom2")

        check(
            "改服务器上限不会销毁/改动已有的房间",
            status == 200 and room is not None
            and int(room.get("max_players") or 0) == 5,
            f"status={status} room={room}",
        )

        # ---- 12b. 房间人数【建房时定下、之后不许改】（用户要求）----
        #
        # 用户报的现象：房间主人建房时填 8，但另一个人当 host 时本机填 3，
        # 房间就只开 3 条隧道（隧道数量以前跟着 host 本机设置走），第 4 个人
        # 永远配不上对 → 在 Minecraft 里就是"连不上"。
        #
        # 修法有两半，这一半是服务端：房间人数在建房那一刻锁死，之后谁都不能
        # 改（要改只能删房重建）。隧道数量那一半在客户端（跟着房间容量走）。
        #
        # 所以这里必须验证：建房后发 MAXPLAYERS 会被明确拒绝，而且房间容量
        # 一点都没变 —— 静默忽略是最糟的，旧客户端会以为改成功了。
        writer.write(b"MAXPLAYERS 3\n")
        await writer.drain()

        locked_reply = await read_reply(
            reader,
            ("ERROR ", "OK "),
            label="MAXPLAYERS 的应答",
        )

        check(
            "建房后再改人数会被明确拒绝（不是静默忽略）",
            locked_reply.startswith("ERROR BAD_MAXPLAYERS"),
            f"回复：{locked_reply!r}",
        )

        check(
            "拒绝的理由说清楚了「建房时定下 / 要改就重建房间」",
            "重建" in locked_reply or "定下" in locked_reply,
            f"回复：{locked_reply!r}",
        )

        room_after = find_room("caproom2")

        check(
            "被拒绝之后房间人数一点没变（还是建房时的 5）",
            room_after is not None
            and int(room_after.get("max_players") or 0) == 5,
            f"room={room_after}",
        )

        # 同一个数字再发一次也不算"修改"——老客户端在进房流程里会重复发一次
        # 自己当前的设置，那不该被当成篡改而报错。
        writer.write(b"MAXPLAYERS 5\n")
        await writer.drain()

        same_reply = await read_reply(
            reader,
            ("OK MAXPLAYERS", "ERROR "),
            label="MAXPLAYERS 重复设置的应答",
        )

        check(
            "重复发同一个数字不算修改（旧客户端进房流程会重发一次）",
            same_reply.startswith("OK MAXPLAYERS"),
            f"回复：{same_reply!r}",
        )

        # ---- 12c. 非房主也绕不过去（用户要求：直接构造请求也不行）----
        #
        # 用户原话：「在房间内的玩家，设置代理端口和人数那些参数会被客户端拦截，
        # 如果直接构造请求也不会被服务器认可哈」。
        #
        # 上面那两条验证的是"房间已锁定"这个理由。这一条验证的是另一道门：
        # **一个普通成员**（既不是所有者也不是 host）直接把 MAXPLAYERS 发过来，
        # 服务端必须用"你没有这个权限"拒绝 —— 客户端拦截只是体验，服务端才是
        # 真正的规矩。少了这一道，任何人写个脚本就能改掉别人的房间人数。
        member_reader, member_writer, member_reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"JOIN __mclink__ plain-cid normalguy {root_token} caproom2{_ver()}",
        )

        check(
            "普通成员能加入房间（作为后续越权测试的基线）",
            member_reply.startswith("OK JOIN"),
            member_reply,
        )

        members.append((member_reader, member_writer))

        member_writer.write(b"MAXPLAYERS 2\n")
        await member_writer.drain()

        member_locked = await read_reply(
            member_reader,
            ("ERROR ", "OK "),
            label="普通成员越权改人数的应答",
        )

        check(
            "普通成员直接发 MAXPLAYERS 也被服务端拒绝（不是只靠客户端拦截）",
            member_locked.startswith("ERROR BAD_MAXPLAYERS"),
            f"回复：{member_locked!r}",
        )

        room_after_member = find_room("caproom2")

        check(
            "越权请求之后房间人数仍然没变（还是建房时的 5）",
            room_after_member is not None
            and int(room_after_member.get("max_players") or 0) == 5,
            f"room={room_after_member}",
        )

        await close_pair(member_reader, member_writer)

        await close_pair(reader, writer)
        await close_pair(reader2, writer2)

        # ---- 13. 房间列表的「房主」= 建房的人，另报当前主机 ----
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ who-cid admin {root_token} whoroom{_ver()}",
        )
        check("建房成功（用于房主/主机列）", reply.startswith("OK JOIN"), reply)
        members.append((reader, writer))

        reader2, writer2, reply2 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            "ROOMS __mclink__ who-cid admin",
        )
        lines = []

        for _ in range(30):
            data = await asyncio.wait_for(reader2.readline(), timeout=5)

            if not data:
                break

            text = data.decode(errors="replace").strip()
            lines.append(text)

            if text == "DONE":
                break

        await close_pair(reader2, writer2)

        row = next((l for l in lines if l.startswith("ROOM whoroom ")), "")

        check(
            "还没人认领主机时，房主列已经是建房的人（不是 -）",
            row.startswith("ROOM whoroom admin "),
            row or str(lines[:3]),
        )
        check(
            "另有一列报告当前主机（还没人认领时是 -）",
            "host=-" in row,
            row,
        )

        # 房主认领主机之后，当前主机列要有名字
        writer.write(b"CLAIM_HOST\n")
        await writer.drain()
        await asyncio.sleep(0.3)

        reader3, writer3, _ = await send_line(
            "127.0.0.1", RELAY_PORT, "ROOMS __mclink__ who-cid admin"
        )
        lines3 = []

        for _ in range(30):
            data = await asyncio.wait_for(reader3.readline(), timeout=5)

            if not data:
                break

            text = data.decode(errors="replace").strip()
            lines3.append(text)

            if text == "DONE":
                break

        await close_pair(reader3, writer3)

        row3 = next((l for l in lines3 if l.startswith("ROOM whoroom ")), "")

        check(
            "认领主机后，当前主机列显示认领的人",
            "host=admin" in row3,
            row3 or str(lines3[:3]),
        )

        await close_pair(reader, writer)

        # ---- 14. 协同关闭：通知 → 客户端自己退 → 中继兜底 ----
        #
        # 用户的设计：服务端不直接踹断连接，而是"标记关闭 + 通知"，
        # 由客户端自己完成退出；服务端只等一小会儿（有超时保护），
        # 等不到才强制断开。这样客户端不会卡在"已连接/未退出房间"。
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ coop-cid admin {root_token} cooproom{_ver()}",
        )
        check("建立用于协同关闭的房间", reply.startswith("OK JOIN"), reply)

        reader2, writer2, reply2 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"JOIN __mclink__ coop-cid2 admin {root_token} cooproom{_ver()}",
        )
        check("第二个成员进入房间", reply2.startswith("OK JOIN"), reply2)

        # 关闭请求放在后台跑：它会等客户端自己退出
        closer = asyncio.create_task(
            asyncio.to_thread(
                http_request,
                "POST",
                "/rooms/cooproom/revoke",
                ADMIN_TOKEN,
                {"reason": "协同关闭测试"},
                30,
            )
        )

        await asyncio.sleep(0.8)

        # 两个客户端都应该收到 ROOM REVOKED（前面可能还排着 HOST_STATE 广播，
        # 所以要一直读到看见它为止）
        told = []

        for member_reader in (reader, reader2):
            seen = ""

            for _ in range(20):
                try:
                    data = await asyncio.wait_for(
                        member_reader.readline(), timeout=5
                    )
                except Exception as e:  # noqa: BLE001
                    seen = f"(读不到: {e})"
                    break

                if not data:
                    seen = seen or "(连接已关闭)"
                    break

                text = data.decode(errors="replace").strip()

                if text.startswith("ROOM REVOKED"):
                    seen = text
                    break

            told.append(seen)

        check(
            "协同关闭：每个客户端都先收到 ROOM REVOKED 通知",
            all(t.startswith("ROOM REVOKED") for t in told),
            str(told),
        )

        # 客户端"自己退出"：发 LEAVE 然后断开（真实客户端就是这么做的）
        for member_writer in (writer, writer2):
            with contextlib.suppress(Exception):
                member_writer.write(b"LEAVE\n")
                await member_writer.drain()

        await asyncio.sleep(0.3)

        for member_writer in (writer, writer2):
            await close_pair(None, member_writer)

        status, payload = await asyncio.wait_for(closer, timeout=40)

        check(
            "协同关闭成功（服务端不需要强制断开）",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

        data = payload.get("data") or {}

        check(
            "报告里说明有几个客户端是自行退出的",
            int(data.get("left_voluntarily") or 0) >= 1,
            str(data),
        )
        check(
            "报告里说明有没有人被强制断开（本次应该是 0）",
            int(data.get("forced") or 0) == 0,
            str(data),
        )
        check(
            "关闭后房间从列表消失",
            "cooproom" not in room_ids(),
            f"rooms={room_ids()}",
        )

        # ---- 15. 晚一步进来的客户端得到明确原因 ----
        late_reader, late_writer, late_reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"JOIN __mclink__ late-cid admin {root_token} cooproom{_ver()}",
        )
        check(
            "刚被关闭的房间：晚到的 JOIN 会被告知「已被管理员关闭」",
            late_reply.startswith("ERROR ROOM_REVOKED"),
            late_reply,
        )
        await close_pair(late_reader, late_writer)

        # ---- 16. host 释放后，名字要立刻从广播里消失 ----
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ rel-cid admin {root_token} relroom{_ver()}",
        )
        check("建立用于 host 释放测试的房间", reply.startswith("OK JOIN"), reply)
        members.append((reader, writer))

        writer.write(b"CLAIM_HOST\n")
        await writer.drain()
        await asyncio.sleep(0.4)

        # 读掉 CLAIM_HOST 的应答和随后的 HOST_STATE，直到看到 host_name=admin
        claimed_line = ""

        for _ in range(10):
            try:
                data = await asyncio.wait_for(reader.readline(), timeout=3)
            except Exception:
                break

            text = data.decode(errors="replace").strip()

            if "HOST_STATE" in text and "host_name=admin" in text:
                claimed_line = text
                break

        check(
            "认领主机后广播里带着主机名字（host_name=admin）",
            "host_name=admin" in claimed_line,
            claimed_line,
        )

        writer.write(b"RELEASE_HOST\n")
        await writer.drain()

        released_line = ""

        for _ in range(10):
            try:
                data = await asyncio.wait_for(reader.readline(), timeout=3)
            except Exception:
                break

            text = data.decode(errors="replace").strip()

            if "HOST_STATE" in text and "host=0" in text:
                released_line = text
                break

        check(
            "释放主机后广播立刻变成 host=0 且名字被清空（host_name=-）",
            "host=0" in released_line and "host_name=-" in released_line,
            released_line,
        )

        await close_pair(reader, writer)

        # ---- 17. 中文 / 需要转义的房间名也必须能关闭 ----
        #
        # 这一条是那个"服务端永远关不掉房间"的真凶：
        #
        # 面板按 HTTP 规矩发 rawurlencode(房间名)，而中继用 urlparse().path
        # 直接当房间名去查表 —— urlparse **不会**解码。于是只要房间名里有
        # 一个非 ASCII 字符（中文房间名几乎必然如此），中继查的是
        # "%E6%88%91%E7%9A%84%E4%B8%96%E7%95%8C"，永远查不到，永远回
        # 「房间不存在（可能已经关闭）」。
        #
        # 之前的测试全用 "caproom"/"delroom" 这种纯 ASCII 名字，所以一路绿灯。
        #
        # 房间名规则在用户要求下放宽了：中文、空格、斜杠都能用（协议里发出去的
        # 是 percent-encode 之后的房间 ID，所以空格不会再截断协议行）。
        # 这里发的是【裸中文名】（老客户端就是这么发的）：中继会把它归一化成
        # 同一个房间 ID，房间名照旧显示成中文。
        fancy_room = "我的世界测试"

        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ fancy-cid admin {root_token} {fancy_room}{_ver()}",
        )
        check(
            "可以建立纯中文名的房间（需要 URL 转义的名字）",
            reply.startswith("OK JOIN"),
            reply,
        )
        members.append((reader, writer))

        check(
            "这个房间确实出现在列表里（按房间名显示）",
            fancy_room in room_display_names(),
            f"rooms={room_display_names()}",
        )

        status, payload = http_request(
            "POST",
            "/rooms/" + quote(fancy_room, safe="") + "/revoke",
            token=ADMIN_TOKEN,
            body={"reason": "中文房间名回归测试"},
        )
        check(
            "中文房间名也能被面板关闭（URL 解码回归）",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

        check(
            "关闭后中文房间也消失了",
            fancy_room not in room_display_names(),
            f"rooms={room_display_names()}",
        )

        # ---- 18. 重复建房必须被拒，而且不能动到原来的房间 ----
        #
        # 用户报的致命问题：房间已经有人在玩，第二个人（当时是根管理员）
        # 再点「创建」，房间被"抢"了过来 —— 原房主失去所有权，所有权跟着
        # 新连接走，于是原来的人全部掉线。
        #
        # 根因有两个，都已经修了：CREATE 遇到已有房间会复用（应该拒绝），
        # 以及 is_owner 只看"有没有 owner"而不看是不是 CREATE（JOIN 也能
        # 白捡所有权）。
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ coll-cid1 admin {root_token} collroom{_ver()}",
        )
        check(
            "第一个人建房成功（他是所有者）",
            reply.startswith("OK JOIN") and "owner=1" in reply,
            reply,
        )
        members.append((reader, writer))

        reader2, writer2, reply2 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ coll-cid2 admin {root_token} collroom{_ver()}",
        )
        check(
            "第二个人用同一个房间号建房 → 被拒绝（ERROR ROOM_EXISTS）",
            reply2.startswith("ERROR ROOM_EXISTS"),
            reply2,
        )
        await close_pair(reader2, writer2)

        # 原房间必须原封不动：还在、还是原来那个人、人数没变
        room = find_room("collroom")

        check(
            "被拒绝之后，原房间还在，所有者没被抢走",
            room is not None
            and int(room.get("members") or 0) == 1
            and room.get("owner_username") == "admin",
            f"room={room}",
        )

        writer.write(b"PING x\n")
        await writer.drain()
        await asyncio.sleep(0.2)

        check(
            "原房主的连接没有被踢掉（他还能正常通信）",
            "collroom" in room_ids(),
            f"rooms={room_ids()}",
        )

        # JOIN 也不能白捡所有权：即使这个房间暂时没有 owner
        reader3, writer3, reply3 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"JOIN __mclink__ coll-cid3 admin {root_token} collroom{_ver()}",
        )
        check(
            "第三个人加入（JOIN）不可能是所有者",
            reply3.startswith("OK JOIN") and "owner=0" in reply3,
            reply3,
        )
        members.append((reader3, writer3))

        await close_pair(reader, writer)
        await close_pair(reader3, writer3)

        # ---- 19. 房间内在线 = 成员 + 世界里的玩家（去重）----
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ count2-cid admin {root_token} countroom2{_ver()}",
        )
        check("建立用于人数统计的房间 v2", reply.startswith("OK JOIN"), reply)
        members.append((reader, writer))

        room = find_room("countroom2")

        check(
            "只有房主在房间里时，房间内在线 = 1",
            room is not None and int(room.get("members") or 0) == 1,
            f"room={room}",
        )

        await close_pair(reader, writer)

        # ---- 20. host 停止开服不能销毁房间，也不能卡住重开服 ----
        #
        # 用户报的：host 手动断开连接 → 房间跟着被销毁、其他人再也进不来、
        # 房间列表空了。真因是 handle_host 结束时没清 host_writer，
        # 于是 host_watchdog 认为"房主还连着但不吭声"，等 HOST_IDLE_TIMEOUT
        # 秒后调用 room.close()，把整个房间销毁（还有第二个症状：
        # 房主想重新开服会被 ROOM_ALREADY_HAS_HOST 拒绝）。
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ life-cid admin {root_token} liferoom{_ver()}",
        )
        check("建房（用于房间生命测试）", reply.startswith("OK JOIN"), reply)
        members.append((reader, writer))

        h_reader, h_writer, h_reply = await send_line(
            "127.0.0.1", RELAY_PORT,
            f"HOST liferoom life-cid admin {root_token}{_ver()}",
        )
        check("房主开始开服（HOST 连接建立）", h_reply.startswith("OK HOST"), h_reply)

        await asyncio.sleep(0.4)
        room = find_room("liferoom")

        check(
            "开服中：房间显示已加入世界 1（房主自己）",
            room is not None and int(room.get("players") or 0) == 1,
            f"room={room}",
        )

        # ---- 房主点「停止」：断开 HOST 连接 ----
        h_writer.close()

        with contextlib.suppress(Exception):
            await h_writer.wait_closed()

        await asyncio.sleep(1.2)

        room = find_room("liferoom")

        check(
            "房主停止开服后，房间依然存在（不会跟着被销毁）",
            room is not None,
            f"rooms={room_ids()}",
        )
        check(
            "停止后房间不再算作有人在世界里（已加入世界 = 0）",
            room is not None and int(room.get("players") or 0) == 0,
            f"room={room}",
        )
        check(
            "停止后人不在了：房间内在线只剩还在房间里的人",
            room is not None and int(room.get("members") or 0) >= 1,
            f"room={room}",
        )

        # ---- 重新开服必须成功（以前会 ROOM_ALREADY_HAS_HOST）----
        h2_reader, h2_writer, h2_reply = await send_line(
            "127.0.0.1", RELAY_PORT,
            f"HOST liferoom life-cid admin {root_token}{_ver()}",
        )
        check(
            "房主可以重新开服（不再报 ROOM_ALREADY_HAS_HOST）",
            h2_reply.startswith("OK HOST"),
            h2_reply,
        )

        h2_writer.close()

        with contextlib.suppress(Exception):
            await h2_writer.wait_closed()

        await asyncio.sleep(0.8)

        check(
            "再次停止后房间仍在",
            find_room("liferoom") is not None,
            f"rooms={room_ids()}",
        )

        await close_pair(reader, writer)

        # 同上：token 仍有效的掉线先保留座位（连接≠身份），宽限期
        # （MCLINK_MEMBER_SEAT_GRACE=3）走完才按"真正离开"销毁房间。
        gone = await wait_until(
            lambda: find_room("liferoom") is None,
            timeout=25,
            interval=0.5,
            label="liferoom destroyed after the seat grace expired",
        )

        check(
            "房主（所有者）真的离开房间后，房间才销毁",
            gone,
            f"rooms={room_ids()}",
        )

        # ---- 21. 房间被销毁时，房间里的人必须收到通知 ----
        #
        # Room.close() 以前只关 host / 隧道 / 会话，**不关成员连接**：
        # 房间在任何地方都不存在了，但成员客户端还显示"已加入房间"，
        # 按钮还锁着，必须手动点「退出房间」才能继续 —— 就是用户报的那个。
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ notify-cid admin {root_token} notifyroom{_ver()}",
        )
        check("建房（用于关闭通知测试）", reply.startswith("OK JOIN"), reply)

        reader2, writer2, reply2 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"JOIN __mclink__ notify-cid2 admin {root_token} notifyroom{_ver()}",
        )
        check("第二个人加入", reply2.startswith("OK JOIN"), reply2)

        # 用面板的强制关闭（= revoke）：两个人都应该收到 ROOM REVOKED
        status, payload = http_request(
            "POST", "/rooms/notifyroom/revoke", token=ADMIN_TOKEN,
            body={"reason": "通知测试"}, timeout=30,
        )
        check(
            "强制关闭成功",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

        notified = 0

        for member_reader in (reader, reader2):
            for _ in range(10):
                try:
                    data = await asyncio.wait_for(member_reader.readline(), timeout=4)
                except Exception:
                    break

                if not data:
                    break

                if data.decode(errors="replace").strip().startswith("ROOM REVOKED"):
                    notified += 1
                    break

        check(
            "房间里每个人都收到了 ROOM REVOKED（客户端才能自动退房）",
            notified == 2,
            f"notified={notified}",
        )

        await close_pair(reader, writer)
        await close_pair(reader2, writer2)

        # ---- 22. 同 client_id 的不同账号不能顶掉房主 ----
        #
        # client_id 是客户端自己报的。以前"同 client_id = 同一个人"，
        # 于是别人只要用一样的 client_id 就能把房主顶掉并继承房间
        # （用户报的"个别情况下房主被篡夺"）。现在只有同账号才算重连。
        reader, writer, reply = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"CREATE __mclink__ shared-cid admin {root_token} hijackroom{_ver()}",
        )
        check("建房（用于防篡夺测试）", reply.startswith("OK JOIN"), reply)
        members.append((reader, writer))

        http_request(
            "POST", "/users", token=ADMIN_TOKEN,
            body={
                "username": "hijacker",
                "password": "hijack123",
                "role": "player",
                "actor": "admin",
            },
        )
        _, hijack_token = await login("hijacker", "hijack123")

        # 同一个 client_id，不同账号
        reader2, writer2, reply2 = await send_line(
            "127.0.0.1",
            RELAY_PORT,
            f"JOIN __mclink__ shared-cid hijacker {hijack_token} hijackroom{_ver()}",
        )
        check(
            "另一个账号用同样的 client_id 加入：不会成为所有者",
            reply2.startswith("OK JOIN") and "owner=0" in reply2,
            reply2,
        )

        room = find_room("hijackroom")

        check(
            "原房主没有被顶掉（所有者仍然是 admin）",
            room is not None and room.get("owner_username") == "admin",
            f"room={room}",
        )

        check(
            "两个人并存（房间内在线 = 2），没人被踢",
            room is not None and int(room.get("members") or 0) == 2,
            f"room={room}",
        )

        await close_pair(reader2, writer2)
        await close_pair(reader, writer)

    finally:
        stop_pinger.set()
        pinger_task.cancel()

        try:
            await pinger_task
        except (asyncio.CancelledError, Exception):
            pass

        for reader, writer in members:
            await close_pair(reader, writer)

        relay.terminate()

        try:
            relay.wait(timeout=10)
        except Exception:
            relay.kill()

        server_log.close()

    print("")
    print(f"passed: {len(PASSED)}   failed: {len(FAILED)}")

    if FAILED:
        print("failed checks:")
        for name in FAILED:
            print(f"  - {name}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
