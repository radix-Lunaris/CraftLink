"""
CraftLink -- 中继回归网：设计基准 §A/§B/C/D/§12（2026-09-27 用户永久基准）

这一套是**防回归**用的：`docs/DESIGN-BASELINE-20260927.md` 里这些规则大多数
是项目早期就有、用户早就验收过的，后来被改坏了。每条都用真中继子进程 + 真
socket 钉住，任何"顺手重构"把它们改回去都会红。

  1. **Owner 不可转移 / 不可继承**（基准 §A）
     `add_member` 以前在"同 client_id + 同账号重连"时把 `previous.is_owner`
     复制到新成员对象上 —— 创建者的连接被顶替后，房间所有权跟着新连接走了。
     现在：Owner 只在创建者本人那条连接上成立；创建者的连接被顶替 = 本人已经
     不在，`remove_member()` 仍然返回 True，房间走唯一的销毁路径。

  2. **一个房间最多一个 Host**（基准 §A）
     host 位置可以跟着"同一个人"的新连接走（kick_test 钉住的既有行为），但
     搬运时必须把其它任何 `is_host` 一起清掉，否则 `host_member` 会取到错的
     人 -> HOST_STATE 报错 host_name、player 的打洞目标指向错的人（P0 #1）。

  3. **无 Host / Host 未开始连接的门禁**（基准 §B）
     * 没人认领主机位 -> `ERROR NO_HOST`；
     * 有人认领但还没开服 -> `ERROR HOST_NOT_READY`（两个码必须可区分）；
     * Host 已开服 -> PLAYER / HOST_TUNNEL 才允许进入配对流程。

  4. **`started=`**（基准 §D/§E，ADDITIVE）
     HOST_STATE 里给出"哪些成员已经开始连接"的 client_id 列表，玩家只向
     "当前 Host" 打、Host 只向"已开始连接的 Player"打。

  5. **只有 Owner 能切连接模式 + 切换瞬间清理旧配对/隧道**（基准 §A/§D/§E）
     非 Owner 的 Host 一律 `ERROR NOT_HOST`；Owner 切成功后广播，并把旧的
     PLAYER / HOST_TUNNEL / 配对会话全部作废（原因码 `CONN_MODE_CHANGED`）。

  6. **Host 掉线 = 真正离开；房间保留**（基准 §B/§12）
     * 成员心跳超时清理 Host：释主机位 + 断开所有游戏连接（HOST_LEFT）+
       广播 host=0 + 房间保留可再认领 + 一行 `[HOST RELEASE] reason=timeout`；
     * 开服控制连接静默掉线（host_watchdog）：**只拆这条连接**，带 Owner 的
       房间不许被销毁（房间寿命绑在 Owner 的成员连接上）。

Run:
    python tests/relay_baseline_test.py
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "server"))

RELAY_PORT = int(os.environ.get("MCLINK_TEST_BASELINE_PORT", "19320"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_BASELINE_ADMIN_PORT", "19321"))
TIMEOUT_PORT = int(os.environ.get("MCLINK_TEST_BASELINE_TIMEOUT_PORT", "19322"))
TIMEOUT_ADMIN_PORT = int(
    os.environ.get("MCLINK_TEST_BASELINE_TIMEOUT_ADMIN_PORT", "19323")
)
HOSTWD_PORT = int(os.environ.get("MCLINK_TEST_BASELINE_HOSTWD_PORT", "19324"))
HOSTWD_ADMIN_PORT = int(
    os.environ.get("MCLINK_TEST_BASELINE_HOSTWD_ADMIN_PORT", "19325")
)

ADMIN_TOKEN = "baseline-test-admin-token"
ADMIN_USER = "admin"
ADMIN_PASS = "adminpass123"
SENTINEL = "__mclink__"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail:
        line += f"  -- {detail}"

    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"),
              flush=True)

    (PASSED if ok else FAILED).append(name)


# ============================================================
# 纯逻辑：Owner 不可转移 / 单 Host 不变量 / started= 取值
# ============================================================

class _FakeWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _make_room(members=(), sessions=None, waiting_players=(),
               host_writer=None, host_id="", host_username="",
               name="unit-room"):
    import room_members

    class FakeRoom(room_members.MembershipMixin):
        def __init__(self):
            self.name = name
            self.members = {}
            self.sessions = dict(sessions or {})
            self.waiting_players = list(waiting_players)
            self.waiting_tunnels = []
            self.host_writer = host_writer
            self.host_id = host_id
            self.host_username = host_username
            self.dirty = False
            self.lock = asyncio.Lock()

            for member in members:
                self.members[member.member_key] = member

        def mark_dirty(self):
            self.dirty = True

    return FakeRoom()


def _member(cid, name, **kw):
    import room_members

    member = room_members.RoomMember(
        "unit-room", None, kw.pop("writer", None),
        client_id=cid, username=name, is_owner=kw.pop("is_owner", False),
    )
    member.is_host = kw.pop("is_host", False)
    return member


def test_owner_never_transfers_unit():
    print("")
    print("---- 1. [防回归] Owner 不可转移：创建者连接被顶替 -> 房间必须销毁 ----")

    import room_members

    old_writer = _FakeWriter()
    old = _member("cid-o", "alice", is_owner=True, writer=old_writer)
    room = _make_room([old])

    new = _member("cid-o", "alice")
    room.add_member(new)

    check("★ 同 client_id + 同账号的新连接**不继承** Owner（基准 §A）",
          new.is_owner is False, f"new.is_owner={new.is_owner}")
    check("标记了 owner_superseded（给日志/排查用）",
          room.owner_superseded is True, str(room.owner_superseded))
    check("创建者那条旧 socket 被关掉（让唯一清理路径立刻跑起来）",
          old_writer.closed is True, f"closed={old_writer.closed}")
    check("★ 对'已被顶替的 owner 对象'，remove_member() 仍然返回 True"
          "（房间据此销毁，而不是把所有权搬走）",
          room.remove_member(old) is True, "remove_member(old)")
    check("新连接不是 owner，房间此刻确实没有 owner",
          room.owner is None, str(room.owner))

    # 非创建者的同 id 同账号重连：不涉及 owner，remove_member 仍然是 False
    p1 = _member("cid-p", "bob")
    room2 = _make_room([p1])
    p2 = _member("cid-p", "bob")
    room2.add_member(p2)
    check("普通成员被顶替时 remove_member 返回 False（房间不受影响）",
          room2.remove_member(p1) is False, "remove_member(p1)")


def test_single_host_invariant_unit():
    print("")
    print("---- 2. [防回归] 一个房间最多一个 Host（搬运时清掉残留 is_host）----")

    h1 = _member("cid-h", "host", is_host=True)
    stale = _member("cid-s", "someone", is_host=True)
    room = _make_room([h1, stale])

    h2 = _member("cid-h", "host")
    room.add_member(h2)

    holders = [m.client_id for m in room.members.values() if m.is_host]

    check("★ 同一个人重连后 host 位置跟到新连接（既有行为，kick_test 钉住）",
          h2.is_host is True, f"h2.is_host={h2.is_host}")
    check("★ 残留的第二个 is_host 被清掉（否则 host_member 会取错人 -> P0 #1）",
          holders == ["cid-h"], str(holders))
    check("host_member 是那个人（不是最早进房的其它人）",
          room.host_member is h2, str(getattr(room.host_member, "client_id", None)))

    # release_host 也要把任何残留 is_host 清掉（位置空 = 没人持有）
    a = _member("cid-a", "a", is_host=True)
    b = _member("cid-b", "b", is_host=True)
    room2 = _make_room([a, b])
    room2.release_host(a)

    check("★ release_host 之后没有任何 is_host 残留（host_member 为空）",
          room2.host_member is None
          and not any(m.is_host for m in room2.members.values()),
          str({m.client_id: m.is_host for m in room2.members.values()}))


def test_started_token_unit():
    print("")
    print("---- 4. [防回归] started= / started_client_ids() 的取值口径 ----")

    class Waiter:
        def __init__(self, cid):
            self.client_id = cid

    class Pair:
        def __init__(self, cid):
            self.player_id = cid

    import room_members

    room = _make_room(
        host_writer=object(), host_id="cid-h",
        waiting_players=[Waiter("cid-p1")],
        sessions={"s1": Pair("cid-p2")},
    )

    check("★ host 控制连接 -> host_id 进 started",
          "cid-h" in room_members.started_client_ids(room),
          str(room_members.started_client_ids(room)))
    check("★ 等待配对的 PLAYER（waiter.client_id）进 started",
          "cid-p1" in room_members.started_client_ids(room),
          str(room_members.started_client_ids(room)))
    check("★ 已配对的 PLAYER（pair.player_id）进 started",
          "cid-p2" in room_members.started_client_ids(room),
          str(room_members.started_client_ids(room)))
    check("★ started_token 格式：逗号分隔、空时是 `-`",
          room_members.started_token(room) == "cid-h,cid-p1,cid-p2",
          room_members.started_token(room))
    check("空房间 -> `-`",
          room_members.started_token(_make_room()) == "-",
          room_members.started_token(_make_room()))


def test_broadcast_carries_started():
    print("")
    print("---- 4b. [防回归] HOST_STATE 广播必须带 started= / host= / host_name= ----")

    import room_members

    class FakeMember:
        def __init__(self, cid, name):
            self.client_id = cid
            self.username = name
            self.member_key = cid
            self.is_host = False
            self.closed = False
            self.joined_at = time.time()
            self.sent = []

        @property
        def label(self):
            return self.username

        @property
        def display_name(self):
            return self.username

        async def send(self, line):
            self.sent.append(line.strip())
            return True

    # ============================================================
    #  ★ 防回归：`host_id=` 与 `started=` 必须是**同一个 cid 来源**
    # ============================================================
    #
    #  以前 started_client_ids() 加的是 room.host_id（老式 `HOST <room>` 控制
    #  连接握手时报的 client_id），而 host_id= 用的是 host_client_id(room)
    #  （V3 成员认领时是**成员**的 client_id）。真实客户端上这两个 id 不一样
    #  （开服控制连接常带中继补的匿名 id），实测广播成了
    #  `host_id=h-cid` 但 `started=c5b71f4b8f33464b`：
    #  Host 侧 `started ∩ peers = ∅`（一个目标都拉不到），Player 侧永远
    #  host_not_ready。
    #
    #  所以这里**故意**让成员 cid（cid-a）和老式 host_id（legacy-cid）不同：
    #  host_id= 必须报成员的 cid-a，started= 里也必须是 cid-a，绝不能是
    #  legacy-cid。
    alice = FakeMember("cid-a", "alice")
    alice.is_host = True
    room = _make_room([alice], host_writer=object(), host_id="legacy-cid")

    asyncio.run(room_members.broadcast_member_state(room))

    line = alice.sent[-1] if alice.sent else ""

    check("广播里有 started= 字段",
          "started=" in line, line)
    check("★ started= 是逗号分隔的 client_id（这里只有开服的 host）",
          "started=cid-a" in line.split(), line)
    check("★ 广播里有 host_id=，且就是当前 Host 的 client_id（精确挑目标用）",
          "host_id=cid-a" in line.split(), line)
    check("★ 不变量：`host_id=X` 时 `started=` 必须包含 X（两套 id 不许混用）",
          room_members.host_client_id(room) in room_members.started_client_ids(room)
          and "legacy-cid" not in room_members.started_client_ids(room),
          f"host_id={room_members.host_client_id(room)} "
          f"started={room_members.started_client_ids(room)}")
    check("★ 没有 Host 时 host_id=-（不编一个假 id）",
          room_members.host_client_id(_make_room()) == "-",
          room_members.host_client_id(_make_room()))
    check("started= 排在 mode= 之前（additive 字段都在尾部，老客户端按空格忽略）",
          line.index("started=") < line.index("mode="), line)


# ============================================================
# 真中继
# ============================================================

def start_relay(port, admin_port, log_name, extra_env=None):
    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(port),
            "MCLINK_DB_ENABLED": "0",
            "MCLINK_REQUIRE_ACCOUNT": "0",
            "MCLINK_MAX_PLAYERS": "8",
            "MCLINK_ADMIN_API_ENABLED": "1",
            "MCLINK_ADMIN_API_HOST": "127.0.0.1",
            "MCLINK_ADMIN_API_PORT": str(admin_port),
            "MCLINK_ADMIN_API_TOKEN": ADMIN_TOKEN,
            "MCLINK_STATUS_FILE": os.path.join(
                HERE, log_name.replace(".log", "_status.json")
            ),
            "MCLINK_STATUS_INTERVAL": "5",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            "MCLINK_VERBOSE": "1",
        }
    )
    env["MCLINK_VERSION"] = ""

    for key, value in (extra_env or {}).items():
        env[key] = str(value)

    log_path = os.path.join(HERE, log_name)
    log = open(log_path, "w", encoding="utf-8", errors="replace")

    proc = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    deadline = time.time() + 25

    while time.time() < deadline:
        try:
            probe = socket.create_connection(("127.0.0.1", port), 1)
            probe.close()
            return proc, log, log_path
        except OSError:
            if proc.poll() is not None:
                return proc, log, log_path

            time.sleep(0.3)

    return proc, log, log_path


def stop_relay(proc, log):
    with contextlib.suppress(Exception):
        proc.terminate()
        proc.wait(timeout=8)

    with contextlib.suppress(Exception):
        log.flush()
        log.close()


def read_log(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def token_of(line, key):
    for tok in str(line or "").split()[1:]:
        name, _, value = tok.partition("=")

        if name == key:
            return value

    return ""


def parse_started(text):
    """`started=<cid>,<cid>` -> ["cid", ...]（空 / `-` 时是 []）。"""
    value = token_of(text, "started")

    return [item for item in value.split(",") if item and item != "-"]


class Member:
    """一条房间成员控制连接：读推送、回 PING、解析 HOST_STATE。"""

    def __init__(self, reader, writer, answer_ping=True):
        self.reader = reader
        self.writer = writer
        self.answer_ping = answer_ping
        self.lines = []
        self.state = {}
        self.raw_states = []
        self._cursor = 0
        self._event = asyncio.Event()
        self._task = None

    async def join(self, line, timeout=8):
        self.writer.write((line + "\n").encode())
        await self.writer.drain()
        raw = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        self._task = asyncio.create_task(self._pump())
        return raw.decode("utf-8", errors="replace").strip()

    async def _pump(self):
        try:
            while True:
                raw = await self.reader.readline()

                if not raw:
                    break

                text = raw.decode("utf-8", errors="replace").strip()

                if not text:
                    continue

                self.lines.append(text)
                self._event.set()
                upper = text.upper()

                if upper.startswith("PING"):
                    if self.answer_ping:
                        self.writer.write(
                            (f"PONG {text[4:].strip()}\n").encode()
                        )
                        await self.writer.drain()
                    continue

                if upper.startswith("HOST_STATE"):
                    self.raw_states.append(text)
                    state = {}

                    for tok in text.split()[1:]:
                        key, _, value = tok.partition("=")
                        state[key] = value

                    self.state = state
        except Exception:
            pass
        finally:
            self._event.set()

    async def send(self, line):
        self.writer.write((line + "\n").encode())
        await self.writer.drain()

    async def expect(self, prefix, timeout=8):
        deadline = time.time() + timeout

        while True:
            for index in range(self._cursor, len(self.lines)):
                if self.lines[index].startswith(prefix):
                    self._cursor = index + 1
                    return self.lines[index]

            remaining = deadline - time.time()

            if remaining <= 0:
                return None

            self._event.clear()

            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    async def wait_state(self, predicate, timeout=12, label="HOST_STATE"):
        deadline = time.time() + timeout

        while time.time() < deadline:
            if predicate(self):
                return True

            await asyncio.sleep(0.1)

        print(f"  !! timeout waiting for {label}: {self.raw_states[-3:]}",
              flush=True)
        return False

    async def close(self, send_leave=False):
        if send_leave:
            with contextlib.suppress(Exception):
                await self.send("LEAVE")
                await asyncio.sleep(0.2)

        if self._task is not None:
            self._task.cancel()

            with contextlib.suppress(BaseException):
                await self._task

        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()

    async def drop_socket(self):
        if self._task is not None:
            self._task.cancel()

            with contextlib.suppress(BaseException):
                await self._task

        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()

        await asyncio.sleep(0.2)


async def open_member(port, line, answer_ping=True, timeout=8):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), timeout=timeout
    )
    member = Member(reader, writer, answer_ping=answer_ping)
    reply = await member.join(line, timeout=timeout)
    return member, reply


async def open_raw(port, line, timeout=8, read_reply=True):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    reply = ""

    if read_reply:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
            reply = raw.decode(errors="replace").strip()
        except asyncio.TimeoutError:
            reply = ""

    return reader, writer, reply


async def read_line(reader, timeout=8):
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return raw.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        return ""


async def read_until(reader, prefix, timeout=8, attempts=8):
    """读到以 prefix 开头的一行为止（中间可能有 INFO 之类的旁路推送）。"""
    deadline = time.time() + timeout

    for _ in range(attempts):
        remaining = deadline - time.time()

        if remaining <= 0:
            return ""

        line = await read_line(reader, timeout=remaining)

        if line.startswith(prefix):
            return line

        if not line:
            return ""

    return ""


async def send_line(port, line, timeout=8):
    reader, writer, reply = await open_raw(port, line, timeout=timeout)
    writer.close()

    with contextlib.suppress(Exception):
        await writer.wait_closed()

    return reply


async def host_tunnel_reply(port, line, timeout=8):
    """HOST_TUNNEL 会先收到冻结的 `WAIT`，然后才是接受/拒绝行。"""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    first = await read_line(reader, timeout)
    second = await read_line(reader, timeout)

    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()

    return first, second


def admin_request(port, path, timeout=8):
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")

        with contextlib.suppress(Exception):
            return error.code, json.loads(body)

        return error.code, {"ok": False, "error": body}


def find_room(port, room_id):
    status, payload = admin_request(port, "/rooms")

    if status != 200 or not payload.get("ok"):
        return None

    for room in payload.get("data") or []:
        if room.get("room") == room_id:
            return room

    return None


async def wait_room(port, room_id, predicate, timeout=12, label="room"):
    deadline = time.time() + timeout

    while time.time() < deadline:
        room = find_room(port, room_id)

        if room is not None and predicate(room):
            return room

        await asyncio.sleep(0.2)

    print(f"  !! timeout waiting for {label}: {find_room(port, room_id)}",
          flush=True)
    return None


async def establish_relay_session(port, room, host_cid, player_cid):
    """HOST 控制连接 + HOST_TUNNEL + PLAYER -> READY -> CONNECTED。"""
    h_reader, h_writer, h_reply = await open_raw(
        port, f"HOST {room} {host_cid} Host"
    )
    t_reader, t_writer, t_reply = await open_raw(
        port, f"HOST_TUNNEL {room} {host_cid} {host_cid}"
    )
    p_reader, p_writer, _ = await open_raw(
        port, f"PLAYER {room} {player_cid} Player", read_reply=False
    )

    start = await read_line(t_reader)

    t_writer.write(b"READY\n")
    await t_writer.drain()

    connected = await read_line(p_reader)

    return {
        "host": (h_reader, h_writer, h_reply),
        "tunnel": (t_reader, t_writer, t_reply),
        "player": (p_reader, p_writer, ""),
        "start": start,
        "connected": connected,
    }


# ============================================================
# 1b. 真中继：创建者的连接被顶替 -> 房间立即销毁，新连接不得继承 Owner
# ============================================================

async def test_owner_supersede_destroys_room(port, admin_port):
    print("")
    print("---- 1b. 真中继：Owner 连接被顶替 -> 房间销毁 + 新连接不是 Owner ----")

    room = "base-owner"

    owner, owner_reply = await open_member(
        port, f"CREATE {SENTINEL} cid-owner Owner - {room}"
    )
    room_token = token_of(owner_reply, "room")

    player, _ = await open_member(
        port, f"JOIN {SENTINEL} cid-player Player - {room}"
    )

    imposter = None

    try:
        check("Owner 建房（owner=1）",
              token_of(owner_reply, "owner") == "1", owner_reply)

        # 同 client_id + 同账号再来一条连接 = 创建者那条连接被顶替。
        imposter, imposter_reply = await open_member(
            port, f"JOIN {SENTINEL} cid-owner Owner - {room}"
        )

        check("★ 顶替连接**不继承** Owner（owner=0，基准 §A）",
              token_of(imposter_reply, "owner") == "0", imposter_reply)

        revoked = await imposter.expect("ROOM REVOKED", timeout=8)

        check("★ 创建者本人已不在 -> 房间销毁，顶替连接收到 ROOM REVOKED",
              bool(revoked), str(revoked))

        gone = False
        deadline = time.time() + 10

        while time.time() < deadline:
            if find_room(admin_port, room_token) is None:
                gone = True
                break

            await asyncio.sleep(0.2)

        check("★ 房间里所有人都被断开、房间从注册表消失（立即销毁）",
              gone, str(find_room(admin_port, room_token)))

        player_revoked = await player.expect("ROOM REVOKED", timeout=8)

        check("★ 房间里的其他成员也收到 ROOM REVOKED",
              bool(player_revoked), str(player_revoked))
    finally:
        for member in (owner, player, imposter):
            if member is not None:
                await member.close()


# ============================================================
# 3. 真中继：无 Host / Host 未就绪 的门禁
# ============================================================

async def test_host_gate(port):
    print("")
    print("---- 3. 真中继：NO_HOST / HOST_NOT_READY 两级门禁 ----")

    room = "base-gate"

    owner, owner_reply = await open_member(
        port, f"CREATE {SENTINEL} cid-owner Owner - {room}"
    )

    try:
        # ---- 没人认领主机位 -> NO_HOST ----
        player_reply = await send_line(port, f"PLAYER {room} cid-x X")
        check("★ 没人认领 Host：PLAYER -> ERROR NO_HOST",
              player_reply.startswith("ERROR NO_HOST"), player_reply)

        first, second = await host_tunnel_reply(port, f"HOST_TUNNEL {room}")
        check("★ 没人认领 Host：HOST_TUNNEL -> WAIT + ERROR NO_HOST",
              first == "WAIT" and second.startswith("ERROR NO_HOST"),
              f"{first!r} / {second!r}")

        # ---- 有人认领主机位但还没开服 -> HOST_NOT_READY ----
        await owner.send("CLAIM_HOST")
        check("Owner 认领主机位",
              bool(await owner.expect("OK CLAIM_HOST", timeout=8)))

        player_reply = await send_line(port, f"PLAYER {room} cid-x X")
        check("★ Host 已认领但未开服：PLAYER -> ERROR HOST_NOT_READY",
              player_reply.startswith("ERROR HOST_NOT_READY"), player_reply)

        first, second = await host_tunnel_reply(port, f"HOST_TUNNEL {room}")
        check("★ Host 已认领但未开服：HOST_TUNNEL -> WAIT + ERROR HOST_NOT_READY",
              first == "WAIT" and second.startswith("ERROR HOST_NOT_READY"),
              f"{first!r} / {second!r}")

        # ---- Host 开服 -> 门打开，PLAYER 才能进配对流程 ----
        h_reader, h_writer, h_reply = await open_raw(
            port, f"HOST {room} cid-owner Owner"
        )
        check("Host 开服（HOST 控制连接建立）", h_reply.startswith("OK HOST"),
              h_reply)

        t_reader, t_writer, t_wait = await open_raw(port, f"HOST_TUNNEL {room}")

        check("Host 已开服：HOST_TUNNEL 收到 WAIT（进等待池）",
              t_wait == "WAIT", str(t_wait))

        p_reader, p_writer, _ = await open_raw(
            port, f"PLAYER {room} cid-p Player", read_reply=False
        )

        start = await read_line(t_reader)

        check("★ Host 已开服：PLAYER 才被允许进入正式流程（配对成功 START）",
              start.startswith("START"), start)

        for writer in (p_writer, t_writer, h_writer):
            with contextlib.suppress(Exception):
                writer.close()
    finally:
        await owner.close()


# ============================================================
# 4 + P0 #1：started= 广播 + player 只打当前 Host
# ============================================================

async def test_started_and_direct_target(port):
    print("")
    print("---- 4. 真中继：started= 广播 + player 的打洞目标 = 当前 Host ----")

    room = "base-started"

    owner, _ = await open_member(
        port, f"CREATE {SENTINEL} cid-owner Owner - {room}"
    )
    host, _ = await open_member(port, f"JOIN {SENTINEL} cid-host Host - {room}")
    player, _ = await open_member(
        port, f"JOIN {SENTINEL} cid-player Player - {room}"
    )

    try:
        await host.send("CLAIM_HOST")
        check("Host 认领主机位",
              bool(await host.expect("OK CLAIM_HOST", timeout=8)))

        claimed = await owner.wait_state(
            lambda m: m.state.get("host") == "1", timeout=8
        )
        check("Owner 收到 host=1 + host_name=Host",
              claimed and owner.state.get("host_name") == "Host",
              str(owner.state))
        check("★ HOST_STATE 带 host_id=cid-host（客户端据此在 peers 里精确挑目标）",
              owner.state.get("host_id") == "cid-host",
              str(owner.state.get("host_id")))
        check("★ Host 只是认领、还没开服：started 里**不含** cid-host",
              "cid-host" not in parse_started(owner.raw_states[-1])
              if owner.raw_states else False,
              f"started={parse_started(owner.raw_states[-1]) if owner.raw_states else []}")

        # ---- Host 开服：started 立刻包含 host ----
        #
        # ★ 开服控制连接**故意**用一个和成员不同的 client_id
        #   （cid-host-legacy）：真实客户端上这两个 id 就不一样。广播里的
        #   `host_id=` 是成员 cid-host，`started=` 也必须含 cid-host，
        #   绝不能出现 cid-host-legacy（否则 Host 侧一个目标都拉不到）。
        h_reader, h_writer, h_reply = await open_raw(
            port, f"HOST {room} cid-host-legacy Host"
        )
        check("Host 开服", h_reply.startswith("OK HOST"), h_reply)

        check("★ Host 开服后 started= 里出现 cid-host",
              await owner.wait_state(
                  lambda m: "cid-host" in parse_started(
                      m.raw_states[-1] if m.raw_states else ""
                  ),
                  timeout=8, label="started 含 host",
              ),
              f"started={parse_started(owner.raw_states[-1]) if owner.raw_states else []}")

        latest = owner.raw_states[-1] if owner.raw_states else ""

        check("★ 不变量：`host_id=X` 时 `started=` 必须包含 X（同一个 cid 来源）",
              token_of(latest, "host_id") == "cid-host"
              and "cid-host" in parse_started(latest)
              and "cid-host-legacy" not in parse_started(latest),
              f"host_id={token_of(latest, 'host_id')!r} "
              f"started={parse_started(latest)}")

        # ---- Player 点开始连接 -> started 里出现 player ----
        p_reader, p_writer, _ = await open_raw(
            port, f"PLAYER {room} cid-player Player", read_reply=False
        )

        check("★ Player 打开 PLAYER 连接后 started= 里出现 cid-player",
              await owner.wait_state(
                  lambda m: "cid-player" in parse_started(
                      m.raw_states[-1] if m.raw_states else ""
                  ),
                  timeout=8, label="started 含 player",
              ),
              f"started={parse_started(owner.raw_states[-1]) if owner.raw_states else []}")

        # ============================================================
        #  P0 #1：房主≠host 的三人房里，player 的默认打洞目标必须是
        #  **当前 Host**（cid-host），绝不是创建房间的 Owner（cid-owner）。
        #
        #  DIRECT_CAND 走的是**成员控制连接**（房间控制面），不是 PLAYER
        #  数据 socket。
        # ============================================================
        owner.lines.clear()
        owner._cursor = 0

        await player.send("DIRECT_CAND 203.0.113.9 40000")

        forwarded = await host.expect("DIRECT_CAND", timeout=8)

        check("★ player 的 DIRECT_CAND 被转发给当前 Host",
              bool(forwarded) and "203.0.113.9" in (forwarded or ""),
              str(forwarded))

        await asyncio.sleep(0.5)
        leaked = [ln for ln in owner.lines if ln.startswith("DIRECT_CAND")]

        check("★ 创建房间的 Owner **没有**收到这条候选（不能打向房主）",
              not leaked, str(leaked))

        for writer in (p_writer, h_writer):
            with contextlib.suppress(Exception):
                writer.close()
    finally:
        for member in (owner, host, player):
            await member.close()


# ============================================================
# 5. 真中继：只有 Owner 能切模式 + 切换清理旧配对
# ============================================================

async def test_connmode_owner_only_and_rebuild(port, admin_port):
    print("")
    print("---- 5. 真中继：只有 Owner 能切模式 + 切换瞬间清旧通路 ----")

    room = "base-mode"

    owner, owner_reply = await open_member(
        port, f"CREATE {SENTINEL} cid-owner Owner - {room}"
    )
    room_token = token_of(owner_reply, "room")
    host, _ = await open_member(port, f"JOIN {SENTINEL} cid-host Host - {room}")

    session = None

    try:
        await host.send("CLAIM_HOST")
        check("Host 认领主机位",
              bool(await host.expect("OK CLAIM_HOST", timeout=8)))

        # ---- 非 Owner 的 Host 无权改模式 ----
        await host.send("CONNMODE stun")
        refused = await host.expect("ERROR", timeout=8)

        check("★ 非 Owner 的 Host 改模式 -> ERROR NOT_HOST（基准 §A）",
              bool(refused) and "NOT_HOST" in refused.upper(), str(refused))

        # ---- Owner 改模式成功 ----
        await owner.send("CONNMODE stun")
        ack = await owner.expect("OK CONNMODE", timeout=8)

        check("★ Owner 能切模式（房主建房后 host=0 也允许）",
              bool(ack) and "mode=stun" in ack, str(ack))

        check("Host 也收到 mode=stun 的 HOST_STATE（广播及时）",
              await host.wait_state(
                  lambda m: m.state.get("mode") == "stun", timeout=8
              ),
              str(host.state))

        # ---- 建一条真正的 relay 配对，再切回去，验证旧通路被作废 ----
        await owner.send("CONNMODE relay")
        check("先切回 relay",
              bool(await owner.expect("OK CONNMODE", timeout=8)), "OK CONNMODE")

        session = await establish_relay_session(port, room, "cid-host", "cid-p")

        check("relay 会话走完握手（CONNECTED）",
              session["connected"] == "CONNECTED", session["connected"])

        await wait_room(
            admin_port, room_token,
            lambda r: int(r.get("players") or 0) >= 2, timeout=8,
            label="玩家进世界",
        )

        # ---- 切到 stun：旧配对 / 隧道必须被清理 ----
        await owner.send("CONNMODE stun")

        # 顺序固定：清理通知（CONN_MODE_CHANGED）先于 OK CONNMODE 回到成员连接
        changed = await owner.expect("CONN_MODE_CHANGED", timeout=8)

        check("★ 成员控制连接收到可识别的 CONN_MODE_CHANGED 原因",
              bool(changed), str(changed))

        ack = await owner.expect("OK CONNMODE", timeout=8)

        check("Owner 切到 stun", bool(ack) and "mode=stun" in ack, str(ack))

        # 旧的 PLAYER / TUNNEL socket 必须被关掉（established 会话只关 sock）
        closed_player = await read_line(session["player"][0], timeout=8) == ""
        check("★ 旧 PLAYER socket 被关掉（不留'已配对但模式已变'的脏会话）",
              closed_player, "EOF")

        cleared = await wait_room(
            admin_port, room_token,
            lambda r: not r.get("sessions") and not r.get("idle_tunnels")
            and not r.get("waiting_players"),
            timeout=10, label="旧配对/隧道清空",
        )

        check("★ 快照里 sessions / idle_tunnels / waiting_players 全空",
              cleared is not None,
              str({k: (find_room(admin_port, room_token) or {}).get(k)
                   for k in ("sessions", "idle_tunnels", "waiting_players")}))

        with contextlib.suppress(Exception):
            session["tunnel"][1].close()
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                session["host"][1].close()

        for member in (owner, host):
            await member.close()


# ============================================================
# 6a. 真中继（快看门狗）：Host 成员心跳超时 = 真正离开 + 房间保留
# ============================================================

async def test_host_member_timeout(port, admin_port):
    print("")
    print("---- 6a. 真中继：Host 心跳超时 -> 释放 + 断游戏 + 房间保留 ----")

    room = "base-timeout"

    owner, owner_reply = await open_member(
        port, f"CREATE {SENTINEL} cid-owner Owner - {room}"
    )
    room_token = token_of(owner_reply, "room")
    # host 成员连接：认领 host 之后不再吭声、也不回 PING（等看门狗收）
    host, _ = await open_member(
        port, f"JOIN {SENTINEL} cid-host Host - {room}", answer_ping=False
    )

    try:
        await host.send("CLAIM_HOST")
        # 认领回包可能收不到（host 的 pump 不回 PING，但读仍在跑），宽容处理
        await asyncio.sleep(0.3)

        h_reader, h_writer, h_reply = await open_raw(
            port, f"HOST {room} cid-host Host"
        )
        check("Host 开服（建立 host_writer）", h_reply.startswith("OK HOST"),
              h_reply)

        # 一个还在等待配对的 PLAYER：超时清理时必须收到 HOST_LEFT
        p_reader, p_writer, _ = await open_raw(
            port, f"PLAYER {room} cid-p Player", read_reply=False
        )

        # Host 静默 -> 成员看门狗（2s 静默 / 1s 巡检）收走他
        check("★ Host 成员心跳超时 -> Owner 收到 host=0",
              await owner.wait_state(
                  lambda m: m.state.get("host") == "0", timeout=20,
                  label="owner host=0",
              ),
              str(owner.raw_states[-3:]))

        left = await read_line(p_reader, timeout=10)

        # ★ 用户 2026-09-30（最后一条）：host 被清退 / 离开 = 主机位**真的空出来**
        #   了 -> `HOST_RELEASED`（客户端断开 + 重算 + 不自动重连）。
        #   只有"闪断、身份还在宽限期里"才是 `HOST_LEFT`。
        check("★ 还在等待配对的 PLAYER 被断开并收到 HOST_RELEASED",
              left.startswith("ERROR HOST_RELEASED"), repr(left))

        kept = find_room(admin_port, room_token)

        check("★ 房间保留（离开的不是 Owner）", kept is not None,
              str(kept))

        # 别人可以重新认领
        await owner.send("CLAIM_HOST")
        check("★ Host 超时后 Owner 能立刻重新认领（位置已释放）",
              bool(await owner.expect("OK CLAIM_HOST", timeout=8)),
              "OK CLAIM_HOST")

        with contextlib.suppress(Exception):
            p_writer.close()
    finally:
        with contextlib.suppress(Exception):
            h_writer.close()

        for member in (owner, host):
            await member.close()


# ============================================================
# 6b. 真中继（快主机看门狗）：开服控制连接静默 -> 带 Owner 的房间不许销毁
# ============================================================

async def test_host_control_silence_keeps_owner_room(port, admin_port):
    print("")
    print("---- 6b. 真中继：开服控制连接静默掉线 -> 房间保留（host_watchdog）----")

    room = "base-hostwd"

    owner, owner_reply = await open_member(
        port, f"CREATE {SENTINEL} cid-owner Owner - {room}"
    )
    room_token = token_of(owner_reply, "room")
    host, _ = await open_member(port, f"JOIN {SENTINEL} cid-host Host - {room}")

    try:
        await host.send("CLAIM_HOST")
        await host.expect("OK CLAIM_HOST", timeout=8)

        # 只建立开服控制连接，先发一次 PING（给看门狗一个 last_seen 基线），
        # 然后**永远不再 PING** -> host_watchdog 才会判定它掉线。
        h_reader, h_writer, h_reply = await open_raw(
            port, f"HOST {room} cid-host Host"
        )
        check("Host 开服", h_reply.startswith("OK HOST"), h_reply)

        h_writer.write(b"PING 1\n")
        await h_writer.drain()
        check("Host 开服连接有一次心跳（看门狗基线）",
              (await read_until(h_reader, "PONG", timeout=8)).startswith("PONG"),
              "PONG")

        # host 成员连接照常回 PING（不会被成员看门狗收走），只有开服连接静默。
        reaped = await wait_room(
            admin_port, room_token,
            lambda r: r.get("host_online") is False,
            timeout=30, label="host_watchdog 拆掉开服连接",
        )

        check("★ host_watchdog 拆掉了静默的开服控制连接",
              reaped is not None,
              str((reaped or {}).get("host_online")))

        kept = find_room(admin_port, room_token)

        check("★ 带 Owner 的房间**没有被销毁**（房间寿命绑在 Owner，基准 §A）",
              kept is not None, str(kept))
        check("★ 主机位仍然保留（UI 显示 xxx（未开服），不新增'离线'档）",
              kept is not None and kept.get("host_claimed") is True
              and kept.get("host_name") == "Host",
              str({k: (kept or {}).get(k)
                   for k in ("host_claimed", "host_name", "host_online")}))
    finally:
        with contextlib.suppress(Exception):
            h_writer.close()

        for member in (owner, host):
            await member.close()


# ============================================================
# 驱动
# ============================================================

async def run_real_relay_tests():
    main_proc, main_log, main_log_path = start_relay(
        RELAY_PORT, ADMIN_PORT, "relay_baseline_relay.log"
    )
    main_ok = main_proc.poll() is None

    check("主中继启动", main_ok, "进程已退出，见 tests/relay_baseline_relay.log")

    try:
        if main_ok:
            await test_owner_supersede_destroys_room(RELAY_PORT, ADMIN_PORT)
            await test_host_gate(RELAY_PORT)
            await test_started_and_direct_target(RELAY_PORT)
            await test_connmode_owner_only_and_rebuild(RELAY_PORT, ADMIN_PORT)
    finally:
        stop_relay(main_proc, main_log)

    # ---- 快看门狗：成员超时 = 真正离开 ----
    timeout_proc, timeout_log, timeout_log_path = start_relay(
        TIMEOUT_PORT, TIMEOUT_ADMIN_PORT, "relay_baseline_relay_timeout.log",
        extra_env={
            # 5s > 中继自己的 RTT 探测周期（3s），所以"会回 PING 的成员"
            # 永远活着，"不回 PING 的成员"才会被收走。设成 2s 会把所有人
            # 都收掉（host_release_identity_test 用的是同一个口径）。
            "MCLINK_MEMBER_IDLE_TIMEOUT": "5",
            "MCLINK_MEMBER_WATCHDOG_INTERVAL": "1",
        },
    )
    timeout_ok = timeout_proc.poll() is None

    check("超时中继启动（成员静默 5s / 巡检 1s）", timeout_ok,
          "见 tests/relay_baseline_relay_timeout.log")

    try:
        if timeout_ok:
            await test_host_member_timeout(TIMEOUT_PORT, TIMEOUT_ADMIN_PORT)
    finally:
        stop_relay(timeout_proc, timeout_log)

    # 日志必须在进程停掉之后再读：stdout 重定向的行可能还在缓冲里。
    timeout_text = read_log(timeout_log_path)

    check("★ 日志里有唯一一行 [HOST RELEASE] + reason=timeout",
          "[HOST RELEASE]" in timeout_text and "reason=timeout" in timeout_text,
          "见 tests/relay_baseline_relay_timeout.log")

    # ---- 快主机看门狗：开服连接静默不销毁带 Owner 的房间 ----
    hostwd_proc, hostwd_log, _ = start_relay(
        HOSTWD_PORT, HOSTWD_ADMIN_PORT, "relay_baseline_relay_hostwd.log",
        extra_env={
            "MCLINK_HOST_IDLE_TIMEOUT": "1",
            "MCLINK_HOST_WATCHDOG_INTERVAL": "1",
        },
    )
    hostwd_ok = hostwd_proc.poll() is None

    check("主机看门狗中继启动", hostwd_ok,
          "见 tests/relay_baseline_relay_hostwd.log")

    try:
        if hostwd_ok:
            await test_host_control_silence_keeps_owner_room(
                HOSTWD_PORT, HOSTWD_ADMIN_PORT
            )
    finally:
        stop_relay(hostwd_proc, hostwd_log)


def main():
    print("=" * 70)
    print("CraftLink 中继回归网：设计基准 §A/§B/§C/§D（Owner / Host / 门禁 / started / 换模式）")
    print("=" * 70)

    for fn, label in (
        (test_owner_never_transfers_unit, "Owner 不可转移单测"),
        (test_single_host_invariant_unit, "单 Host 不变量单测"),
        (test_started_token_unit, "started= 取值单测"),
        (test_broadcast_carries_started, "HOST_STATE 广播含 started 单测"),
    ):
        try:
            fn()
        except Exception:
            import traceback
            check(f"{label}没有抛异常", False)
            traceback.print_exc()
        else:
            check(f"{label}没有抛异常", True)

    try:
        asyncio.run(run_real_relay_tests())
    except Exception:
        import traceback
        check("真中继 baseline 测试没有抛异常", False)
        traceback.print_exc()
    else:
        check("真中继 baseline 测试没有抛异常", True)

    print("")
    print("=" * 70)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 70)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
