"""
MC-Link -- 踢人（KICK）回归测试

============================================================================
 这一套测什么
============================================================================

房间里的"踢人"是**踢出一个成员**，不是删房：

  * 所有者可以踢除自己以外的任何人；
  * 当前主机（不是所有者）可以踢**非所有者**的成员；
    踢所有者 -> ERROR KICK_FORBIDDEN（用户原话：host 非房主时除了当前房主
    和 host 自己，他都能踢出）；
  * 其他人发 KICK -> ERROR KICK_FORBIDDEN；
  * 踢自己 -> ERROR KICK_SELF，踢不在房间里的人 -> ERROR KICK_NO_SUCH_MEMBER；
  * 被踢的人收到 `KICKED by=<谁> reason=<原因>`，然后连接被关掉；
  * 房间里剩下的人**不受影响**（连接照常、还能 PING/PONG）；
  * 被踢的人如果在游戏里，他的游戏隧道要一起拆掉（不留幽灵会话）；
    踢的是主机时，全房会话都要断（主机位没了谁都不能玩）；
  * **host 会换人**：权限必须按"发 KICK 那一刻的当前 host"判，不能缓存。
    换人路径（CLAIM_HOST / RELEASE_HOST / 掉线 / 被踢 / 重连顶替 / 老式
    HOST 直连）都要立刻生效：新 host 马上能踢，卸任的旧 host 马上 FORBIDDEN，
    host 位空着时除 owner 外没人能踢；owner 兼任 host 时走 owner 规则；
    老式 `HOST <room>` 直连不授予成员踢人权；被顶替的旧连接不能拿过期的
    is_host 继续踢人；
  * "踢所有者 = 房间解散"走的是**管理 API / 面板**那条路
    （`POST /rooms/<room>/kick {"client_id": ...}`，Bearer token 保护，
    管理员可以踢任何人）：被踢者收到 reason=owner_kicked，其余人收到
    ROOM REVOKED，房间消失；
  * drop_all_sessions() 单测：真 bug 回归（它以前调不存在的 Pair.close()，
    异常被吞 -> 一个会话都不拆；RELEASE_HOST / host 离开 / revoke 都受影响）。

真起中继子进程 + 真 TCP 连接：踢人是**连接之间**的行为，假对象测不出来。

Run:
    python tests/kick_test.py
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_KICK_PORT", "19194"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_KICK_ADMIN_PORT", "19195"))

ADMIN_TOKEN = "kick-test-admin-token"

SENTINEL = "__mclink__"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        # 账号系统关掉：这一套测的是"谁在房间里能踢谁"（身份由 CREATE/JOIN
        # 决定），不是账号令牌。开着只会让每条握手多一个无关变量。
        "MCLINK_REQUIRE_ACCOUNT": "0",
        "MCLINK_ADMIN_API_ENABLED": "1",
        "MCLINK_ADMIN_API_HOST": "127.0.0.1",
        "MCLINK_ADMIN_API_PORT": str(ADMIN_PORT),
        "MCLINK_ADMIN_API_TOKEN": ADMIN_TOKEN,
        "MCLINK_STATUS_FILE": os.path.join(HERE, "kick_status.json"),
        "MCLINK_STATUS_INTERVAL": "2",
        "MCLINK_VERBOSE": "1",
    })

    log = open(
        os.path.join(HERE, "kick_relay.log"), "w",
        encoding="utf-8", errors="replace",
    )

    process = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env, stdout=log, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    deadline = time.time() + 25

    while time.time() < deadline:
        try:
            probe = socket.create_connection(("127.0.0.1", RELAY_PORT), 1)
            probe.close()
            return process, log
        except OSError:
            if process.poll() is not None:
                return process, log
            time.sleep(0.3)

    return process, log


# ============================================================
# A member control connection
# ============================================================

class Member:
    """One room membership connection, spoken as raw protocol lines."""

    def __init__(self, name):
        self.name = name
        self.reader = None
        self.writer = None
        self.lines = asyncio.Queue()
        #: Every line ever received, in order. expect() consumes the queue, so
        #: an assertion about a broadcast that may have interleaved with a
        #: reply must look here instead (otherwise the reply wait eats it).
        self.history = []
        self.closed = asyncio.Event()

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", RELAY_PORT
        )
        asyncio.create_task(self._reader())
        return self

    async def _reader(self):
        try:
            while True:
                line = await self.reader.readline()

                if not line:
                    break

                text = line.decode(errors="replace").strip()
                self.history.append(text)
                await self.lines.put(text)

                # 真客户端收到这两行会主动离开房间（KICKED 按"房间已撤销"
                # 处理）。这里照做，否则关房的协同等待要白等满 6 秒。
                if text.startswith("KICKED") or text.startswith("ROOM REVOKED"):
                    break
        except Exception:
            pass
        finally:
            self.closed.set()

            if self.writer is not None:
                try:
                    self.writer.close()
                except Exception:
                    pass

    async def wait_history(self, predicate, timeout=8.0):
        """Wait until any received line satisfies predicate (non-consuming)."""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if any(predicate(line) for line in self.history):
                return True

            await asyncio.sleep(0.05)

        return False

    async def send(self, text):
        self.writer.write((text + "\n").encode())
        await self.writer.drain()

    async def expect(self, prefix, timeout=8.0):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(
                    self.lines.get(),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
            except (asyncio.TimeoutError, TimeoutError):
                return None

            if line.upper().startswith(prefix.upper()):
                return line

        return None

    async def expect_closed(self, timeout=8.0):
        try:
            await asyncio.wait_for(self.closed.wait(), timeout=timeout)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            return False

    async def close(self):
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass


async def join(member, room, client_id, owner=False, username=None):
    """Enter a room. Returns (ok, room_token)."""
    role = "CREATE" if owner else "JOIN"
    who = username if username is not None else client_id

    await member.send(f"{role} {SENTINEL} {client_id} {who} - {room}")

    reply = await member.expect("OK JOIN") or await member.expect("OK CREATE")

    if not reply:
        return False, ""

    token = ""

    for part in reply.split()[1:]:
        key, _, value = part.partition("=")
        if key == "room":
            token = value
            break

    return True, token or room


async def ping_alive(member, timeout=6.0):
    """PING must still be answered => the connection is still live."""
    await member.send("PING kick-test")
    return await member.expect("PONG", timeout=timeout)


def http_request(method, path, token=None, body=None, timeout=12):
    url = f"http://127.0.0.1:{ADMIN_PORT}{path}"

    data = None
    headers = {}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )

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
# Raw connections for a real Minecraft session (HOST + TUNNEL + PLAYER)
# ============================================================

async def raw_open(line, expect_reply=True, timeout=8):
    reader, writer = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
    writer.write((line + "\n").encode())
    await writer.drain()

    reply = ""

    if expect_reply:
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=timeout)
            reply = data.decode(errors="replace").strip()
        except (asyncio.TimeoutError, TimeoutError):
            reply = ""

    return reader, writer, reply


async def read_line(reader, timeout=8):
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return None

    return data.decode(errors="replace").strip() if data else None


async def socket_closed(reader, timeout=8):
    """True when the peer sent FIN (read() returns b"")."""
    try:
        data = await asyncio.wait_for(reader.read(1), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return False

    return data == b""


async def establish_session(room, host_cid, player_cid, player_name):
    """
    Walk the frozen handshake far enough for one PAIRED + ESTABLISHED session:
    HOST (host control socket) -> HOST_TUNNEL (WAIT) -> PLAYER -> READY ->
    CONNECTED. Returns the three (reader, writer) pairs plus the handshake
    lines so the caller can assert on them.
    """
    host = await raw_open(f"HOST {room} {host_cid} {host_cid}")
    tunnel = await raw_open(f"HOST_TUNNEL {room} {host_cid} {host_cid}")
    player = await raw_open(f"PLAYER {room} {player_cid} {player_name}", expect_reply=False)

    start = await read_line(tunnel[0])

    tunnel[1].write(b"READY\n")
    await tunnel[1].drain()

    connected = await read_line(player[0])

    return host, tunnel, player, start, connected


def close_raw(conn):
    try:
        conn[1].close()
    except Exception:
        pass


# ============================================================
# Tests
# ============================================================

def test_drop_all_sessions_unit():
    """
    drop_all_sessions() 必须真的拆会话。

    它以前内部调的是不存在的 `Pair.close()`，`except Exception` 把
    AttributeError 静默吞掉 —— 函数名说"拆掉全部会话"，实际一个都没拆。
    这里用鸭子类型的假房间（只提供 sessions / lock / remove_session）断言：
    它对每个会话都调用了有效的 remove_session，结束后 sessions 为空。
    """
    print("")
    print("---- 0. drop_all_sessions() 单测：真的会拆会话 ----")

    sys.path.insert(0, os.path.join(ROOT, "server"))

    import room_members

    class FakeRoom:
        def __init__(self):
            self.name = "unit-room"
            self.lock = asyncio.Lock()
            # 每个"会话"必须像真 Pair 一样有 session_id：drop_all_sessions 就是
            # 靠它调 remove_session 的。
            self.sessions = {
                "s1": SimpleNamespace(session_id="s1"),
                "s2": SimpleNamespace(session_id="s2"),
            }
            self.calls = []

        async def remove_session(self, session_id, reason=""):
            self.calls.append((session_id, reason))
            self.sessions.pop(session_id, None)

    room = FakeRoom()

    async def run():
        return await room_members.drop_all_sessions(room, reason="unit")

    count = asyncio.run(run())

    check(
        "★ 旧 bug 复现点：drop_all_sessions() 真的对每个会话调 remove_session",
        sorted(room.calls) == [("s1", "unit"), ("s2", "unit")],
        str(room.calls),
    )
    check(
        "★ drop_all_sessions() 之后 room.sessions 为空",
        room.sessions == {},
        str(room.sessions),
    )
    check("drop_all_sessions() 返回拆掉的会话数", count == 2, str(count))


async def test_owner_kicks_player():
    print("")
    print("---- 1. 所有者踢玩家：被踢者断开，其他人不受影响 ----")

    owner = await Member("owner").connect()
    p1 = await Member("p1").connect()
    p2 = await Member("p2").connect()

    try:
        _, room = await join(owner, "kick-room-1", "k1-owner", owner=True)
        ok1, _ = await join(p1, "kick-room-1", "k1-p1")
        ok2, _ = await join(p2, "kick-room-1", "k1-p2")

        check("房间里三个人都进来了", bool(room) and ok1 and ok2, room)

        await owner.send(f"KICK {room} k1-p1")

        reply = await owner.expect("OK KICK", timeout=8)

        check(
            "所有者踢人成功 -> OK KICK <target>",
            bool(reply) and reply == "OK KICK k1-p1",
            str(reply),
        )

        kicked = await p1.expect("KICKED", timeout=8)

        check(
            "被踢者收到 KICKED by=... reason=...",
            bool(kicked)
            and "by=k1-owner" in kicked
            and "reason=kicked" in kicked,
            str(kicked),
        )

        check("被踢者的连接随后被关闭", await p1.expect_closed(8))

        # 名单广播也要跟上（剩下两个人）。用 history 判断而不是 expect()：
        # 这条 HOST_STATE 可能插在 OK KICK 之前到达，expect("OK KICK") 会把它
        # 当垃圾行吃掉；它到底先来后到不影响事实。
        members_two = await owner.wait_history(
            lambda line: line.startswith("HOST_STATE") and "members=2" in line,
            timeout=6,
        )

        check(
            "广播的人数变成 2（房间里只剩所有者 + 一个玩家）",
            members_two,
        )

        # 其他人必须原样留着，还能正常说话。
        check("第二个玩家仍在房间里（PING 有 PONG）", bool(await ping_alive(p2)))
        check("所有者自己仍在房间里（PING 有 PONG）", bool(await ping_alive(owner)))
    finally:
        await owner.close()
        await p1.close()
        await p2.close()


async def test_host_kicks_player_and_owner_kicks_host():
    print("")
    print("---- 2. 主机（非所有者）踢人；所有者踢主机后主机位可再认领 ----")

    owner = await Member("owner").connect()
    host = await Member("host").connect()
    p1 = await Member("p1").connect()

    try:
        _, room = await join(owner, "kick-room-2", "k2-owner", owner=True)
        okh, _ = await join(host, "kick-room-2", "k2-host")
        okp, _ = await join(p1, "kick-room-2", "k2-p1")

        await host.send("CLAIM_HOST")
        claimed = await host.expect("OK CLAIM_HOST", timeout=8)

        check("房间建好、主机位被 host 认领", bool(room) and okh and okp and bool(claimed))

        # ---- 主机（不是所有者）踢一个普通玩家 ----
        await host.send(f"KICK {room} k2-p1")

        reply = await host.expect("OK KICK", timeout=8)
        kicked = await p1.expect("KICKED", timeout=8)

        check(
            "★ 主机（非所有者）可以踢普通玩家 -> OK KICK",
            bool(reply) and reply == "OK KICK k2-p1",
            str(reply),
        )
        check(
            "被主机踢的玩家收到 KICKED，by= 是主机的名字",
            bool(kicked) and "by=k2-host" in kicked,
            str(kicked),
        )
        check("被主机踢的玩家连接关闭", await p1.expect_closed(8))
        check("所有者没有被误伤（PING 有 PONG）", bool(await ping_alive(owner)))

        # ---- 所有者踢主机：允许，而且主机位要被释放 ----
        await owner.send(f"KICK {room} k2-host")

        reply2 = await owner.expect("OK KICK", timeout=8)
        host_kicked = await host.expect("KICKED", timeout=8)

        check(
            "所有者可以踢当前主机 -> OK KICK",
            bool(reply2) and reply2 == "OK KICK k2-host",
            str(reply2),
        )
        check("被踢的主机收到 KICKED", bool(host_kicked), str(host_kicked))
        check("被踢的主机连接关闭", await host.expect_closed(8))

        # 主机位必须空出来：所有者现在能认领成功。若 is_host 没清掉，
        # claim_host() 会回 ERROR HOST_TAKEN。
        await owner.send("CLAIM_HOST")
        reclaimed = await owner.expect("OK CLAIM_HOST", timeout=8)

        check(
            "★ 被踢主机的位置被释放，可以被重新认领",
            bool(reclaimed),
            str(reclaimed),
        )
    finally:
        await owner.close()
        await host.close()
        await p1.close()


async def test_forbidden_self_and_unknown():
    print("")
    print("---- 3. 权限：普通玩家不能踢人；踢自己 / 踢不存在的人 ----")

    owner = await Member("owner").connect()
    p1 = await Member("p1").connect()
    p2 = await Member("p2").connect()

    try:
        _, room = await join(owner, "kick-room-3", "k3-owner", owner=True)
        ok1, _ = await join(p1, "kick-room-3", "k3-p1")
        ok2, _ = await join(p2, "kick-room-3", "k3-p2")

        check("房间建好、两个普通玩家进来", bool(room) and ok1 and ok2)

        # 普通玩家踢别人 -> FORBIDDEN
        await p1.send(f"KICK {room} k3-p2")
        forbidden = await p1.expect("ERROR KICK_FORBIDDEN", timeout=8)

        check(
            "★ 普通玩家踢人 -> ERROR KICK_FORBIDDEN",
            bool(forbidden),
            str(forbidden),
        )

        # 踢自己 -> SELF（放在权限之前，任何身份都一样）
        await p1.send(f"KICK {room} k3-p1")
        self_kick = await p1.expect("ERROR KICK_SELF", timeout=8)

        check(
            "踢自己 -> ERROR KICK_SELF（普通玩家也一样）",
            bool(self_kick),
            str(self_kick),
        )

        # 所有者踢自己 -> 同样 SELF
        await owner.send(f"KICK {room} k3-owner")
        owner_self = await owner.expect("ERROR KICK_SELF", timeout=8)

        check("所有者踢自己 -> ERROR KICK_SELF", bool(owner_self), str(owner_self))

        # 不存在的目标 -> NO_SUCH_MEMBER
        await owner.send(f"KICK {room} nobody-here")
        unknown = await owner.expect("ERROR KICK_NO_SUCH_MEMBER", timeout=8)

        check(
            "踢不在房间里的人 -> ERROR KICK_NO_SUCH_MEMBER",
            bool(unknown),
            str(unknown),
        )

        # 以上全是被拒绝的动作：三个人一个都不能少。
        check("被拒绝后所有人都还在（PING 有 PONG）",
              bool(await ping_alive(owner))
              and bool(await ping_alive(p1))
              and bool(await ping_alive(p2)))
    finally:
        await owner.close()
        await p1.close()
        await p2.close()


async def test_host_kicks_owner_forbidden():
    print("")
    print("---- 4. 非 owner 的主机踢所有者 -> KICK_FORBIDDEN（房主高一档） ----")

    owner = await Member("owner").connect()
    host = await Member("host").connect()
    p2 = await Member("p2").connect()

    try:
        _, room = await join(owner, "kick-room-4", "k4-owner", owner=True)
        okh, _ = await join(host, "kick-room-4", "k4-host")
        ok2, _ = await join(p2, "kick-room-4", "k4-p2")

        await host.send("CLAIM_HOST")
        claimed = await host.expect("OK CLAIM_HOST", timeout=8)

        check("房间建好、host 认领主机位", bool(room) and okh and ok2 and bool(claimed))

        # ★ 用户原话：「host 非房主时就是除了当前房主和 host 他都能踢出」。
        # 所以这一脚必须被拒，房间绝对不能散。
        await host.send(f"KICK {room} k4-owner")

        reply = await host.expect("ERROR KICK_FORBIDDEN", timeout=8)

        check(
            "★ 主机（非所有者）踢所有者 -> ERROR KICK_FORBIDDEN",
            bool(reply),
            str(reply),
        )

        check(
            "所有者没有被踢（PING 有 PONG）",
            bool(await ping_alive(owner)),
        )
        check(
            "被拒之后房间还在、其他人也还在（PING 有 PONG）",
            bool(await ping_alive(p2)),
        )

        # 房间绝不能被解散：新成员仍然进得来。
        late = await Member("late").connect()

        try:
            ok_late, _ = await join(late, "kick-room-4", "k4-late")

            check("★ 房间没有被解散（新成员仍可加入）", ok_late)
        finally:
            await late.close()
    finally:
        await owner.close()
        await host.close()
        await p2.close()


async def test_escaped_room_name():
    print("")
    print("---- 5. 房间名需要转义（中文）时两条路径都认 room token ----")

    owner = await Member("owner").connect()
    p1 = await Member("p1").connect()

    try:
        # 真客户端发的就是 percent-encoded 的 room token（不是裸中文名）。
        encoded = quote("踢人房间", safe="")

        _, room = await join(owner, encoded, "k7-owner", owner=True)
        okp, _ = await join(p1, encoded, "k7-p1")

        check(
            "中文房间名建好，回包里的 room 是 percent-encoded token",
            bool(room) and "%" in room and okp,
            room,
        )

        # 真客户端发的就是这个 token（不是裸名字）。room_token() 是幂等的，
        # 所以中继这边做校验 / 归一化都不会走样。
        await owner.send(f"KICK {room} k7-p1")

        reply = await owner.expect("OK KICK", timeout=8)

        check(
            "★ 成员连接的 KICK 认 percent-encoded 的房间 token",
            bool(reply) and reply == "OK KICK k7-p1",
            str(reply),
        )
        check("被踢者收到 KICKED", bool(await p1.expect("KICKED", timeout=8)))
    finally:
        await owner.close()
        await p1.close()

    # ---- 同一条转义链路走管理 API（面板 rawurlencode + 中继 unquote） ----
    owner = await Member("owner").connect()
    p1 = await Member("p1").connect()

    try:
        encoded2 = quote("踢人房间2", safe="")

        _, room = await join(owner, encoded2, "k8-owner", owner=True)
        okp, _ = await join(p1, encoded2, "k8-p1")

        check("第二个中文房间建好", bool(room) and "%" in room and okp, room)

        # 面板是 rawurlencode($room)，这里用 quote(..., safe="") 等价模拟。
        status, payload = http_request(
            "POST",
            "/rooms/" + quote(room, safe="") + "/kick",
            token=ADMIN_TOKEN,
            body={"client_id": "k8-p1"},
        )

        check(
            "★ 中文房间名走管理 API 也能踢（路径 round-trip 正确）",
            status == 200 and payload.get("ok") is True,
            f"{status} {payload}",
        )
        check("被踢者收到 KICKED", bool(await p1.expect("KICKED", timeout=8)))
    finally:
        await owner.close()
        await p1.close()


async def test_admin_api():
    print("")
    print("---- 6. 面板管理 API：token 保护 + 管理员可踢任何人 ----")
    # ---- 6a. 没有 token -> 401 ----
    status, payload = http_request(
        "POST", "/rooms/kick-room-5/kick", token=None,
        body={"client_id": "k5-p1"},
    )

    check(
        "管理 API 踢人接口需要 Bearer token（无 token -> 401）",
        status == 401 and payload.get("error") == "unauthorized",
        f"{status} {payload}",
    )

    # ---- 6b. 管理员踢一个普通玩家 ----
    owner = await Member("owner").connect()
    p1 = await Member("p1").connect()

    try:
        _, room = await join(owner, "kick-room-5", "k5-owner", owner=True)
        okp, _ = await join(p1, "kick-room-5", "k5-p1")

        check("房间建好、一个普通玩家进来", bool(room) and okp, room)

        status, payload = http_request(
            "POST", f"/rooms/{room}/kick", token=ADMIN_TOKEN,
            body={"client_id": "k5-p1"},
        )

        check(
            "★ 管理员踢普通玩家 -> 200 且报「已踢出玩家」",
            status == 200 and payload.get("ok") is True
            and "已踢出玩家" in str(payload.get("message", "")),
            f"{status} {payload}",
        )

        kicked = await p1.expect("KICKED", timeout=8)

        check("被管理员踢的玩家收到 KICKED", bool(kicked), str(kicked))
        check("被管理员踢的玩家连接关闭", await p1.expect_closed(8))
        check("房间还在，所有者没被踢（PING 有 PONG）", bool(await ping_alive(owner)))

        # 房间里还有人 -> 列表里仍然是 live
        status, rooms_payload = http_request("GET", "/rooms", token=ADMIN_TOKEN, timeout=8)

        live = [
            r for r in (rooms_payload.get("data") or [])
            if r.get("room") == room and r.get("live")
        ]

        check(
            "踢一个玩家之后房间仍然在线（踢人 != 删房）",
            status == 200 and bool(live),
            f"{status} {[r.get('room') for r in (rooms_payload.get('data') or [])]}",
        )
    finally:
        await owner.close()
        await p1.close()

    # ---- 6c. 管理员踢所有者 -> 房间解散 ----
    owner = await Member("owner").connect()
    p1 = await Member("p1").connect()
    p2 = await Member("p2").connect()

    try:
        _, room2 = await join(owner, "kick-room-6", "k6-owner", owner=True)
        o1, _ = await join(p1, "kick-room-6", "k6-p1")
        o2, _ = await join(p2, "kick-room-6", "k6-p2")

        check("第二个房间建好、两个玩家进来", bool(room2) and o1 and o2, room2)

        status, payload = http_request(
            "POST", f"/rooms/{room2}/kick", token=ADMIN_TOKEN,
            body={"client_id": "k6-owner"},
        )

        check(
            "★ 管理员踢所有者 -> 200 且房间解散",
            status == 200 and payload.get("ok") is True
            and "解散" in str(payload.get("message", "")),
            f"{status} {payload}",
        )

        owner_kicked = await owner.expect("KICKED", timeout=10)

        check(
            "被踢的所有者收到 reason=owner_kicked",
            bool(owner_kicked) and "reason=owner_kicked" in owner_kicked,
            str(owner_kicked),
        )
        check("其余玩家收到 ROOM REVOKED", bool(await p2.expect("ROOM REVOKED", timeout=10)))

        status, rooms_payload = http_request("GET", "/rooms", token=ADMIN_TOKEN, timeout=8)

        still_there = [
            r for r in (rooms_payload.get("data") or [])
            if r.get("room") == room2
        ]

        check(
            "★ 解散后房间里没有这个房间了",
            status == 200 and not still_there,
            f"{still_there}",
        )
    finally:
        await owner.close()
        await p1.close()
        await p2.close()


async def test_kick_drops_game_sessions():
    print("")
    print("---- 7. 踢人要把游戏隧道一起拆掉（不留幽灵会话） ----")

    # ---- 7a. 踢普通玩家：只拆他自己的会话 ----
    owner = await Member("owner").connect()
    p1 = await Member("p1").connect()
    raw = []

    try:
        _, room = await join(owner, "kick-room-7", "k9-owner", owner=True)
        okp, _ = await join(p1, "kick-room-7", "k9-p1")

        check("房间和成员就绪", bool(room) and okp, room)

        host_c, tun_c, pl_c, start, connected = await establish_session(
            room, "k9-owner", "k9-p1", "alice"
        )
        raw = [host_c, tun_c, pl_c]

        check(
            "真的建立了一条游戏会话（隧道 START + 玩家 CONNECTED）",
            bool(start) and start.startswith("START") and connected == "CONNECTED",
            f"start={start!r} connected={connected!r}",
        )

        status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN, timeout=8)

        live = [r for r in (payload.get("data") or []) if r.get("room") == room]

        check(
            "中继快照里能看到这个会话",
            status == 200 and bool(live)
            and len(live[0].get("sessions") or []) == 1,
            str(live)[:200],
        )

        await owner.send(f"KICK {room} k9-p1")
        reply = await owner.expect("OK KICK", timeout=8)

        check("踢玩家成功", bool(reply) and reply == "OK KICK k9-p1", str(reply))
        check(
            "★ 被踢玩家的游戏隧道被拆掉（PLAYER socket 收到 EOF）",
            await socket_closed(pl_c[0], 8),
        )

        status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN, timeout=8)

        live = [r for r in (payload.get("data") or []) if r.get("room") == room]

        check(
            "★ 会话从中继快照里消失（没有幽灵会话）",
            status == 200 and bool(live)
            and len(live[0].get("sessions") or []) == 0,
            str(live)[:200],
        )
    finally:
        for conn in raw:
            close_raw(conn)
        await owner.close()
        await p1.close()

    # ---- 7b. 踢当前主机：主机位没了，所有人的游戏会话都要断 ----
    owner = await Member("owner").connect()
    host = await Member("host").connect()
    p1 = await Member("p1").connect()
    raw = []

    try:
        _, room = await join(owner, "kick-room-8", "k10-owner", owner=True)
        okh, _ = await join(host, "kick-room-8", "k10-host")
        okp, _ = await join(p1, "kick-room-8", "k10-p1")

        await host.send("CLAIM_HOST")
        claimed = await host.expect("OK CLAIM_HOST", timeout=8)

        check(
            "房间就绪 + host 认领主机位",
            bool(room) and okh and okp and bool(claimed),
        )

        host_c, tun_c, pl_c, start, connected = await establish_session(
            room, "k10-host", "k10-p1", "bob"
        )
        raw = [host_c, tun_c, pl_c]

        check(
            "另一条游戏会话建立",
            bool(start) and start.startswith("START") and connected == "CONNECTED",
            f"start={start!r} connected={connected!r}",
        )

        await owner.send(f"KICK {room} k10-host")
        reply = await owner.expect("OK KICK", timeout=8)

        check("踢主机成功", bool(reply) and reply == "OK KICK k10-host", str(reply))
        check("被踢主机收到 KICKED", bool(await host.expect("KICKED", timeout=8)))

        check(
            "★ 主机被踢后，其他玩家的游戏隧道也断开（没人能玩了）",
            await socket_closed(pl_c[0], 8),
        )

        status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN, timeout=8)

        live = [r for r in (payload.get("data") or []) if r.get("room") == room]

        check(
            "★ 主机被踢后中继快照里没有残留会话",
            status == 200 and bool(live)
            and len(live[0].get("sessions") or []) == 0,
            str(live)[:200],
        )
    finally:
        for conn in raw:
            close_raw(conn)
        await owner.close()
        await host.close()
        await p1.close()

    # ---- 7c. 主机自己 RELEASE_HOST：同一条 drop_all_sessions() 路径 ----
    owner = await Member("owner").connect()
    host = await Member("host").connect()
    p1 = await Member("p1").connect()
    raw = []

    try:
        _, room = await join(owner, "kick-room-9", "k11-owner", owner=True)
        okh, _ = await join(host, "kick-room-9", "k11-host")
        okp, _ = await join(p1, "kick-room-9", "k11-p1")

        await host.send("CLAIM_HOST")
        claimed = await host.expect("OK CLAIM_HOST", timeout=8)

        check(
            "第三个房间就绪 + host 认领主机位",
            bool(room) and okh and okp and bool(claimed),
        )

        host_c, tun_c, pl_c, start, connected = await establish_session(
            room, "k11-host", "k11-p1", "carol"
        )
        raw = [host_c, tun_c, pl_c]

        check(
            "第三条游戏会话建立",
            bool(start) and start.startswith("START") and connected == "CONNECTED",
            f"start={start!r} connected={connected!r}",
        )

        await host.send("RELEASE_HOST")
        reply = await host.expect("OK RELEASE_HOST", timeout=8)

        check("主机释放主机位成功", bool(reply), str(reply))
        check(
            "★ RELEASE_HOST 之后会话真的被拆掉（drop_all_sessions 生效）",
            await socket_closed(pl_c[0], 8),
        )

        status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN, timeout=8)

        live = [r for r in (payload.get("data") or []) if r.get("room") == room]

        check(
            "★ RELEASE_HOST 之后中继快照里没有残留会话",
            status == 200 and bool(live)
            and len(live[0].get("sessions") or []) == 0,
            str(live)[:200],
        )
    finally:
        for conn in raw:
            close_raw(conn)
        await owner.close()
        await host.close()
        await p1.close()


async def wait_room_state(room, predicate, timeout=10, label=""):
    """
    Poll GET /rooms until the live entry for `room` satisfies predicate.

    Used for host hand-offs, where the authority is the relay's CURRENT room
    state (not something the test cached).
    """
    deadline = time.time() + timeout
    last = None

    while time.time() < deadline:
        _status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN, timeout=8)

        for entry in (payload.get("data") or []):
            if entry.get("room") == room:
                last = entry

                if predicate(entry):
                    return entry

        await asyncio.sleep(0.2)

    print(f"  !! timeout waiting for room state {label}: {last}", flush=True)

    return None


async def test_host_transfer_permissions():
    print("")
    print("---- 8. host 换人：权限按发 KICK 那一刻的当前 host 判定 ----")

    owner = await Member("owner").connect()
    a = await Member("A").connect()
    b = await Member("B").connect()
    p1 = await Member("p1").connect()
    p2 = await Member("p2").connect()

    try:
        _, room = await join(owner, "kick-room-10", "m-owner", owner=True)
        oka, _ = await join(a, "kick-room-10", "m-a")
        okb, _ = await join(b, "kick-room-10", "m-b")
        ok1, _ = await join(p1, "kick-room-10", "m-p1")
        ok2, _ = await join(p2, "kick-room-10", "m-p2")

        check(
            "房间就绪（owner + A + B + 两个普通玩家）",
            bool(room) and oka and okb and ok1 and ok2,
            room,
        )

        # ---- A 当 host：能踢普通玩家 ----
        await a.send("CLAIM_HOST")
        check("A 认领 host", bool(await a.expect("OK CLAIM_HOST", timeout=8)))

        await a.send(f"KICK {room} m-p1")
        reply = await a.expect("OK KICK", timeout=8)

        check(
            "★ A 是当前 host -> 能踢普通玩家",
            bool(reply) and reply == "OK KICK m-p1",
            str(reply),
        )
        check("p1 收到 KICKED", bool(await p1.expect("KICKED", timeout=8)))

        # ---- A 卸任：立刻失去权力 ----
        await a.send("RELEASE_HOST")
        check("A 释放 host", bool(await a.expect("OK RELEASE_HOST", timeout=8)))

        await a.send(f"KICK {room} m-p2")
        gone = await a.expect("ERROR KICK_FORBIDDEN", timeout=8)

        check(
            "★ 卸任的 A 再发 KICK -> ERROR KICK_FORBIDDEN（权力立刻失效）",
            bool(gone),
            str(gone),
        )

        # ---- host 位空着：B（普通成员）不能踢 ----
        await b.send(f"KICK {room} m-p2")
        empty_forbidden = await b.expect("ERROR KICK_FORBIDDEN", timeout=8)

        check(
            "★ host 位空着时，普通成员不能踢人（没人有 host 权力）",
            bool(empty_forbidden),
            str(empty_forbidden),
        )

        # ---- B 接任：立刻获得权力 ----
        await b.send("CLAIM_HOST")
        check("B 认领 host", bool(await b.expect("OK CLAIM_HOST", timeout=8)))

        await b.send(f"KICK {room} m-p2")
        reply2 = await b.expect("OK KICK", timeout=8)

        check(
            "★ 新 host B -> 能踢普通玩家（权力立刻生效）",
            bool(reply2) and reply2 == "OK KICK m-p2",
            str(reply2),
        )
        check("p2 收到 KICKED", bool(await p2.expect("KICKED", timeout=8)))

        # ---- 已卸任的 A 不能踢当前 host B ----
        await a.send(f"KICK {room} m-b")
        a_denied = await a.expect("ERROR KICK_FORBIDDEN", timeout=8)

        check(
            "★ 已卸任的 A 不能踢当前 host B",
            bool(a_denied),
            str(a_denied),
        )

        # ---- B 还占着 host：owner 此刻认领会 HOST_TAKEN ----
        await owner.send("CLAIM_HOST")
        taken = await owner.expect("ERROR HOST_TAKEN", timeout=8)

        check("B 仍占着 host（owner 此刻认领会 HOST_TAKEN）", bool(taken), str(taken))

        # ---- owner 踢当前 host B：允许，且 host 位要真的释放 ----
        await owner.send(f"KICK {room} m-b")
        reply3 = await owner.expect("OK KICK", timeout=8)

        check(
            "★ owner 可以踢当前 host B",
            bool(reply3) and reply3 == "OK KICK m-b",
            str(reply3),
        )
        check("B 收到 KICKED", bool(await b.expect("KICKED", timeout=8)))

        await owner.send("CLAIM_HOST")
        reclaimed = await owner.expect("OK CLAIM_HOST", timeout=8)

        check(
            "★ 当前 host 被踢后，主机位真的释放、可再认领",
            bool(reclaimed),
            str(reclaimed),
        )

        # ---- owner 同时是 host：走 owner 规则（不被 host 规则削窄） ----
        await owner.send(f"KICK {room} m-a")
        reply4 = await owner.expect("OK KICK", timeout=8)

        check(
            "★ owner 兼任 host 时走 owner 规则（能踢 A）",
            bool(reply4) and reply4 == "OK KICK m-a",
            str(reply4),
        )
    finally:
        await owner.close()
        await a.close()
        await b.close()
        await p1.close()
        await p2.close()


async def test_host_transfer_via_disconnect_and_legacy():
    print("")
    print("---- 9. 掉线换 host；老式 HOST 直连不授予成员踢人权 ----")

    owner = await Member("owner").connect()
    a = await Member("A").connect()
    b = await Member("B").connect()
    p1 = await Member("p1").connect()
    p2 = await Member("p2").connect()

    try:
        _, room = await join(owner, "kick-room-11", "n-owner", owner=True)
        oka, _ = await join(a, "kick-room-11", "n-a")
        okb, _ = await join(b, "kick-room-11", "n-b")
        ok1, _ = await join(p1, "kick-room-11", "n-p1")
        ok2, _ = await join(p2, "kick-room-11", "n-p2")

        check("房间就绪", bool(room) and oka and okb and ok1 and ok2, room)

        await a.send("CLAIM_HOST")
        check("A 认领 host", bool(await a.expect("OK CLAIM_HOST", timeout=8)))
        await a.send(f"KICK {room} n-p1")
        check("A 作为 host 踢掉 p1", bool(await a.expect("OK KICK", timeout=8)))
        check("p1 收到 KICKED", bool(await p1.expect("KICKED", timeout=8)))

        # ---- A 掉线：host 位自动空出（房间此刻的状态） ----
        await a.close()

        freed = await wait_room_state(
            room,
            lambda r: not (r.get("host_now") or ""),
            timeout=10,
            label="host freed after A disconnected",
        )

        check("★ A 掉线后 host 位自动空出", bool(freed), str(freed))

        # ---- host 位空着：B 不能踢 ----
        await b.send(f"KICK {room} n-p2")
        check(
            "★ host 位空着时 B 不能踢",
            bool(await b.expect("ERROR KICK_FORBIDDEN", timeout=8)),
        )

        # ---- B 认领后立刻能踢 ----
        await b.send("CLAIM_HOST")
        check("B 认领 host", bool(await b.expect("OK CLAIM_HOST", timeout=8)))
        await b.send(f"KICK {room} n-p2")
        check("★ 新 host B 能踢", bool(await b.expect("OK KICK", timeout=8)))
        check("p2 收到 KICKED", bool(await p2.expect("KICKED", timeout=8)))

        # ---- 老式 HOST 控制连接：不给普通成员踢人权 ----
        legacy = await raw_open(f"HOST {room} n-legacy legacy")
        check("老式 HOST 控制连接建立", legacy[2].startswith("OK HOST"), legacy[2])

        c = await Member("C").connect()

        try:
            okc, _ = await join(c, "kick-room-11", "n-c")
            check("新成员 C 进房", okc)

            await c.send(f"KICK {room} n-owner")
            check(
                "★ 房间里只有老式 HOST 直连、C 是普通成员 -> 踢人 FORBIDDEN",
                bool(await c.expect("ERROR KICK_FORBIDDEN", timeout=8)),
            )

            # B 是当前成员 host；老式 HOST 连上不影响他的权力。
            await b.send(f"KICK {room} n-c")
            check(
                "★ 老式 HOST 连上后，成员 host B 仍能踢人",
                bool(await b.expect("OK KICK", timeout=8)),
            )
            check("C 收到 KICKED", bool(await c.expect("KICKED", timeout=8)))
        finally:
            await c.close()
            close_raw(legacy)

        # ---- 成员 host 释放、老式 HOST 还在：成员这边没人有踢人权 ----
        await b.send("RELEASE_HOST")
        check("B 释放 host", bool(await b.expect("OK RELEASE_HOST", timeout=8)))

        # 注意：此刻 room.host_member 为 None，但老式 HOST 的 host_writer 还在。
        # 权限必须只认"成员主机位"，不能把老式 HOST 当成有权力的人。
        await b.send(f"KICK {room} n-owner")
        check(
            "★ host 位空（只剩老式 HOST 直连）时 B 不能踢",
            bool(await b.expect("ERROR KICK_FORBIDDEN", timeout=8)),
        )
    finally:
        await owner.close()
        await a.close()
        await b.close()
        await p1.close()
        await p2.close()


async def test_superseded_connection_cannot_kick():
    print("")
    print("---- 10. 重连顶替：旧连接不能拿过期的 is_host 继续踢人 ----")

    owner = await Member("owner").connect()
    a1 = await Member("A1").connect()
    p = await Member("p").connect()
    a2 = None

    try:
        _, room = await join(owner, "kick-room-12", "o-owner", owner=True)
        oka, _ = await join(a1, "kick-room-12", "o-a", username="alice")
        okp, _ = await join(p, "kick-room-12", "o-p")

        check("房间就绪", bool(room) and oka and okp, room)

        await a1.send("CLAIM_HOST")
        check("A1 认领 host", bool(await a1.expect("OK CLAIM_HOST", timeout=8)))

        # 同 client_id + 同账号重连 = 同一个人换连接；host 身份会搬到新连接，
        # 旧连接对象上的 is_host 不会清（socket 也还开着）。
        a2 = await Member("A2").connect()
        oka2, _ = await join(a2, "kick-room-12", "o-a", username="alice")

        check("A2 用同一身份重连进房（顶替旧连接 A1）", oka2)

        carried = await wait_room_state(
            room,
            lambda r: (r.get("host_now") or "") == "alice",
            timeout=10,
            label="host carried to A2",
        )

        check("host 身份跟到了新连接 A2", bool(carried), str(carried))

        # 旧连接 A1 发 KICK：它对象上的 is_host 是过期值，权限必须按房间
        # 此刻的 host（A2）判 -> 拒绝，而且绝不能真的踢掉 p。
        await a1.send(f"KICK {room} o-p")
        await asyncio.sleep(0.6)

        check(
            "★ 被顶替的旧连接发 KICK 没有踢掉任何人（p 仍 PING/PONG）",
            bool(await ping_alive(p)),
        )

        # 新连接 A2 是当前 host，能踢。
        await a2.send(f"KICK {room} o-p")
        check(
            "★ 新连接 A2（当前 host）能踢",
            bool(await a2.expect("OK KICK", timeout=8)),
        )
        check("p 收到 KICKED", bool(await p.expect("KICKED", timeout=8)))
    finally:
        if a2 is not None:
            await a2.close()

        await owner.close()
        await a1.close()
        await p.close()


async def main():
    print("=" * 64)
    print("MC-Link 踢人（KICK）回归测试")
    print("=" * 64)

    process, log = start_relay()

    try:
        if process.poll() is not None:
            check("中继启动成功", False, f"退出码 {process.returncode}")
            return 1

        check("中继启动成功", True)

        await test_owner_kicks_player()
        await test_host_kicks_player_and_owner_kicks_host()
        await test_forbidden_self_and_unknown()
        await test_host_kicks_owner_forbidden()
        await test_escaped_room_name()
        await test_admin_api()
        await test_kick_drops_game_sessions()
        await test_host_transfer_permissions()
        await test_host_transfer_via_disconnect_and_legacy()
        await test_superseded_connection_cannot_kick()
    finally:
        process.terminate()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

        log.close()

    print("")
    print("=" * 64)
    print(f" 通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    print("=" * 64)

    if FAILED:
        for name in FAILED:
            print(f"  [FAIL] {name}")
        return 1

    return 0


if __name__ == "__main__":
    # 单测先跑（不用中继；它自己起 event loop），结果和下面的行为测试一起汇总。
    test_drop_all_sessions_unit()
    sys.exit(asyncio.run(main()))
