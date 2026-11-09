"""
CraftLink -- 主机位释放 / host_claimed 与 host_name 同源 / 身份可更新（IDENTITY）

这一套盯的是 2026-09-27 交给"中继侧"的三件事（真中继子进程 + 真 socket，
不依赖任何客户端代码，因为那三条消息本身是 ADDITIVE 的新增）：

  1. **成员离开/掉线/被踢/超时后必须立刻释放主机位并广播 HOST_STATE**
     （用户报的"认领 host 的人走了，剩下的人必须断开重连才能认领"）。
     四条移除路径最后都落到同一个清理点；释放时记**唯一一行**
     `[HOST RELEASE] room=<room> by=<name> reason=leave|drop|kick|timeout`。
     房主（主机位持有者）离开时，按用户 2026-09-27 最终口径（A′）：
     房间里所有**游戏连接**（PLAYER / HOST_TUNNEL / 配对会话）被断开并收到
     可识别的 `HOST_RELEASED`（主机位空了）/ `HOST_LEFT`（只是闪断），
     **房间保留**（谁都能重新认领），无 host 期间新的
     PLAYER / HOST_TUNNEL 被拒（`ERROR NO_HOST`）；host 同时是 owner 时走既有
     "房主离开＝房间关闭"规则。

  2. **`host_claimed=true` 与 `host_name=''` 不许同时出现**。
     `host=` / `host_name=` / `host_now` 全部从
     `MembershipMixin.host_state()` 一个来源出 —— 成员认领和老式
     `HOST <room>` 控制连接都算，任何一刻都不会给出自相矛盾的一对。

  3. **身份可更新 + 游客身份是"登录会话"级的**：
     * 中继在首次分配游客名时同时发一条临时 token（OK JOIN 尾部 ADDITIVE
       `guest=<token>`）；客户端在**这一次登录会话**里把它放回握手 `token`
       字段就能复用同一个名字。退房 / 被踢 / 房间被关闭销毁**都不销毁**它；
       只有账号退出（LOGOUT 该 token、用有效账号 IDENTITY 改名 = 换账号、
       重连丢登录态=不带 token）或 TTL 到期才清。不按 client_id、不落库。
     * 新增只读控制消息 `IDENTITY <username> <token> ver=<版本>`：token 有效
       就换成登录名并立刻广播新的 `HOST_STATE`（`names=` 跟着变），
       token 无效回 `ERROR IDENTITY_*`，版本不一致被版本门拒。

Run:
    python tests/host_release_identity_test.py
"""

import asyncio
import contextlib
import json
import os
import re
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

RELAY_PORT = int(os.environ.get("MCLINK_TEST_HOSTREL_PORT", "19210"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_HOSTREL_ADMIN_PORT", "19211"))
GATE_PORT = int(os.environ.get("MCLINK_TEST_HOSTREL_GATE_PORT", "19212"))
GATE_ADMIN_PORT = int(
    os.environ.get("MCLINK_TEST_HOSTREL_GATE_ADMIN_PORT", "19213")
)
TIMEOUT_PORT = int(os.environ.get("MCLINK_TEST_HOSTREL_TIMEOUT_PORT", "19214"))
TIMEOUT_ADMIN_PORT = int(
    os.environ.get("MCLINK_TEST_HOSTREL_TIMEOUT_ADMIN_PORT", "19215")
)

ADMIN_TOKEN = "hostrel-test-admin-token"
ADMIN_USER = "admin"
ADMIN_PASS = "adminpass123"
SERVER_VERSION = "9.9.9"
SENTINEL = "__mclink__"

GUEST_RE = re.compile(r"^游客[0-9a-z]{6}$")

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
# 1. 纯函数 / 纯逻辑：host_state 同源 + 权威释放 + 看门狗原因
# ============================================================

def _member(room_members, cid, name, is_host=False, closed=False,
            writer=None):
    member = room_members.RoomMember(
        "unit-room", None, writer, client_id=cid, username=name
    )
    member.is_host = is_host
    member.closed = closed
    return member


class _FakeWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _make_room(members=(), host_writer=None, host_username="", host_id="",
               name="unit-room"):
    import room_members

    class FakeRoom(room_members.MembershipMixin):
        def __init__(self):
            self.name = name
            self.members = {}
            self.sessions = {}
            self.host_writer = host_writer
            self.host_username = host_username
            self.host_id = host_id
            self.dirty = False
            self.lock = asyncio.Lock()

            for member in members:
                self.members[member.member_key] = member

        def mark_dirty(self):
            self.dirty = True

    return FakeRoom()


def test_host_state_is_one_source():
    print("")
    print("---- 1. host_state()：host= 与 host_name= 的唯一来源 ----")

    import room_members

    empty = _make_room()

    check("★ 既没有成员主机位、也没有老式 HOST -> (False, '')",
          empty.host_state() == (False, ""), str(empty.host_state()))
    check("★ host_state_fields() 空 -> (0, '-')（host=0 必带 host_name=-）",
          room_members.host_state_fields(empty) == (0, "-"),
          str(room_members.host_state_fields(empty)))

    alice = _member(room_members, "cid-a", "alice", is_host=True)
    by_member = _make_room(members=[alice])

    check("★ 成员认领 -> (True, 'alice')",
          by_member.host_state() == (True, "alice"),
          str(by_member.host_state()))
    check("★ 成员认领 -> host_state_fields() = (1, 'alice')（一对同源）",
          room_members.host_state_fields(by_member) == (1, "alice"),
          str(room_members.host_state_fields(by_member)))
    check("host_claimed 属性与 host_state 同源",
          by_member.host_claimed is True, str(by_member.host_claimed))

    legacy = _make_room(host_writer=_FakeWriter(), host_username="legacyBob")

    check("★ 老式 HOST 控制连接也算有主机 -> (True, 'legacyBob')",
          legacy.host_state() == (True, "legacyBob"),
          str(legacy.host_state()))
    check("★ 老式 HOST 路径也满足 host=1 必有名字",
          room_members.host_state_fields(legacy) == (1, "legacyBob"),
          str(room_members.host_state_fields(legacy)))

    no_name = _make_room(host_writer=_FakeWriter(), host_id="legacy-cid")

    check("★ 老式 HOST 没有 username 时退回 client_id（绝不给空名字）",
          room_members.host_state_fields(no_name) == (1, "legacy-cid"),
          str(room_members.host_state_fields(no_name)))

    # 成员名里的空格 / 控制字符不许把控制行拆坏
    weird = _member(room_members, "cid-w", "bad name\x01", is_host=True)
    weird_room = _make_room(members=[weird])

    check("host_name token 里不会有空格 / 控制字符",
          room_members.clean_host_name("bad name\x01") == "badname",
          repr(room_members.clean_host_name("bad name\x01")))
    check("host_state_fields 对脏名字也保证能成一 token",
          room_members.host_state_fields(weird_room)[1] == "badname",
          str(room_members.host_state_fields(weird_room)))


def test_take_host_is_authoritative():
    print("")
    print("---- 2. take_host_from()：权威判定（看门狗先置 closed 也要放得掉）----")

    import room_members

    # 正常持有 -> 释放
    alice = _member(room_members, "cid-a", "alice", is_host=True)
    room = _make_room(members=[alice])

    check("★ 持有者 -> take_host_from 返回 True 并清掉 is_host",
          room.take_host_from(alice) is True and alice.is_host is False,
          f"{alice.is_host}")
    check("释放后 host_state 立刻是 (False, '')",
          room.host_state() == (False, ""), str(room.host_state()))

    # 看门狗先置 closed=True（host_member 会把他过滤掉）也必须是"持有者"
    bob = _member(room_members, "cid-b", "bob", is_host=True, closed=True)
    room = _make_room(members=[bob])

    check("★ 看门狗把 closed 先置 True 时，take_host_from 仍认得出他是主机",
          room.host_member is None and room.take_host_from(bob) is True,
          f"host_member={room.host_member} is_host={bob.is_host}")
    check("★ 这一条就是「认领 host 的人掉线后位置空不出来」的复现点",
          bob.is_host is False and room.host_claimed is False,
          f"is_host={bob.is_host} claimed={room.host_claimed}")

    # 已经被重连顶替的旧连接：不能动新连接的位置
    old = _member(room_members, "cid-c", "carol", is_host=True)
    new = _member(room_members, "cid-c", "carol", is_host=True)
    room = _make_room(members=[new])

    check("★ 被顶替的旧连接 take_host_from 返回 False（不动新连接）",
          room.take_host_from(old) is False and new.is_host is True,
          f"old={old.is_host} new={new.is_host}")

    # 残留标记一起清掉
    stale = _member(room_members, "cid-d", "dave", is_host=True)
    holder = _member(room_members, "cid-e", "erin", is_host=True)
    room = _make_room(members=[stale, holder])

    room.take_host_from(holder)

    check("释放时把任何残留的 is_host 一起清掉（位置空 = 没人持有）",
          not any(m.is_host for m in room.members.values()),
          str({k: m.is_host for k, m in room.members.items()}))


def test_watchdog_marks_timeout():
    print("")
    print("---- 3. 看门狗：静默超时的成员被标记 reason=timeout ----")

    import room_members

    writer = _FakeWriter()
    idle = _member(room_members, "cid-idle", "idle", is_host=True,
                   writer=writer)
    idle.last_seen = time.monotonic() - (room_members.MEMBER_IDLE_TIMEOUT + 5)

    fresh = _member(room_members, "cid-fresh", "fresh")
    fresh.last_seen = time.monotonic()

    room = _make_room(members=[idle, fresh])

    reaped = asyncio.run(room_members.reap_idle_members([room]))

    check("★ 静默超时的成员被看门狗收走（返回里只有他）",
          [m.client_id for _r, m in reaped] == ["cid-idle"],
          str([(r.name, m.client_id) for r, m in reaped]))
    check("★ 他的清理原因被标成 timeout（[HOST RELEASE] 就靠它）",
          idle.removal_reason == "timeout", idle.removal_reason)
    check("看门狗关掉了他的控制 socket（让清理路径跑起来）",
          idle.closed is True and writer.closed is True,
          f"closed={idle.closed} writer={writer.closed}")
    check("还活着的成员不受影响",
          fresh.closed is False and fresh.removal_reason == "drop",
          f"closed={fresh.closed} reason={fresh.removal_reason}")

    # 只有四条允许的 reason 能进 [HOST RELEASE]
    reasons = set()
    import room_registry

    original = room_registry.log_line

    try:
        room_registry.set_logger(lambda *p: reasons.add(" ".join(str(x) for x in p)))
        room_members.log_host_release(room, idle, "timeout")
        room_members.log_host_release(room, idle, "nonsense")
        room_members.log_host_release(room, idle, "leave")
    finally:
        room_registry.set_logger(original)

    lines = sorted(r for r in reasons if r.startswith("[HOST RELEASE]"))

    check("★ 每次释放只记一行，格式固定 room=/by=/reason=",
          len(lines) == 3
          and all("room=unit-room" in ln and "by=idle" in ln for ln in lines),
          str(lines))
    check("★ reason 只允许 leave|drop|kick|timeout（其余按 drop 记）",
          any(ln.endswith("reason=timeout") for ln in lines)
          and any(ln.endswith("reason=drop") for ln in lines)
          and any(ln.endswith("reason=leave") for ln in lines),
          str(lines))


def test_identity_unit():
    print("")
    print("---- 4. IDENTITY：版本门 + token 校验（纯逻辑，假 socket）----")

    import room_members

    class FakeMember:
        def __init__(self, cid="cid-x", name="游客aaaaaa"):
            self.client_id = cid
            self.username = name
            self.sent = []
            self.member_key = cid
            # broadcast_member_state() 会遍历 members 看主机位；给足它要的字段。
            self.is_host = False
            self.closed = False
            self.joined_at = time.time()

        @property
        def label(self):
            return self.username or self.client_id

        @property
        def display_name(self):
            return self.username or self.client_id

        async def send(self, line):
            self.sent.append(line.strip())
            return True

    def run(line, ver=None, bypass=False, verify=None):
        member = FakeMember()
        room = _make_room(members=[member], name="identity-room")

        old_ver = room_members.RELAY_VERSION
        old_bypass = room_members._version_bypass
        old_verify = room_members._identity_verify

        room_members.RELAY_VERSION = ver
        room_members._version_bypass = (lambda: bypass) if bypass else None
        room_members._identity_verify = verify

        try:
            asyncio.run(room_members.handle_member_identity(room, member, line))
        finally:
            room_members.RELAY_VERSION = old_ver
            room_members._version_bypass = old_bypass
            room_members._identity_verify = old_verify

        return member, room

    async def ok_admin(token):
        return True, {"username": "admin"}

    async def bad_token(token):
        return False, "AUTH_INVALID_TOKEN"

    # ---- 成功：改成登录名 ----
    member, room = run("IDENTITY admin -", verify=ok_admin)

    check("★ token 有效 -> OK IDENTITY <name>，且房间里的显示名换成登录名",
          member.sent[0] == "OK IDENTITY admin"
          and member.username == "admin",
          f"sent={member.sent} username={member.username}")

    # ---- 成功：没有账号体系时用请求里的名字 ----
    member, room = run("IDENTITY 新名字 -", verify=None)

    check("没有账号体系时用客户端报的登录名（token 为 -）",
          member.sent[0] == "OK IDENTITY 新名字"
          and member.username == "新名字",
          f"sent={member.sent} username={member.username}")

    # ---- token 无效 ----
    member, room = run("IDENTITY admin bad-token", verify=bad_token)

    check("★ token 无效 -> ERROR IDENTITY_INVALID_TOKEN，名字一个字符没动",
          member.sent and member.sent[0].startswith("ERROR IDENTITY_INVALID_TOKEN")
          and member.username == "游客aaaaaa",
          f"sent={member.sent} username={member.username}")

    # ---- 不在房间里 ----
    class Ghost(FakeMember):
        pass

    ghost = Ghost(cid="cid-ghost")
    room = _make_room(members=[], name="identity-room")
    asyncio.run(room_members.handle_member_identity(
        room, ghost, "IDENTITY admin -"
    ))

    check("★ 不在房间里 -> ERROR IDENTITY_NOT_IN_ROOM",
          ghost.sent and ghost.sent[0].startswith("ERROR IDENTITY_NOT_IN_ROOM"),
          str(ghost.sent))

    # ---- 版本门：不一致被拒（名字不变）----
    member, room = run(
        "IDENTITY admin - ver=1.0.0", ver="9.9.9", verify=ok_admin
    )

    check("★ 版本不一致 -> ERROR VERSION_MISMATCH（两边版本都摊开），名字不变",
          member.sent
          and member.sent[0] == "ERROR VERSION_MISMATCH server=9.9.9 client=1.0.0"
          and member.username == "游客aaaaaa",
          f"sent={member.sent} username={member.username}")

    member, room = run(
        "IDENTITY admin - ver=9.9.9", ver="9.9.9", verify=ok_admin
    )

    check("★ 版本一致 -> 正常改名（门只拦不一致的）",
          member.sent and member.sent[0] == "OK IDENTITY admin",
          str(member.sent))

    # ---- ver= 必须被摘掉，不能污染按位置解析的 token ----
    seen = {}

    async def spy(token):
        seen["token"] = token
        return True, {"username": "admin"}

    run("IDENTITY admin real-token ver=1.2.3", verify=spy)

    check("★ ver= 挂在行尾时被摘掉，不会当成 token",
          seen.get("token") == "real-token", str(seen))


# ============================================================
# 真中继子进程
# ============================================================

def start_relay(port, admin_port, log_name, version=None, require_account=0,
                extra_env=None):
    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(port),
            "MCLINK_DB_ENABLED": "0",
            "MCLINK_REQUIRE_ACCOUNT": str(require_account),
            "MCLINK_BOOTSTRAP_ADMIN": f"{ADMIN_USER}:{ADMIN_PASS}",
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
    env["MCLINK_VERSION"] = version or ""

    # 看门狗周期可以在测试里压到秒级（生产不设 = 默认 15s/3s）。
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


def port_alive(proc):
    return proc.poll() is None


# ============================================================
# 真成员连接（假客户端：只说协议行）
# ============================================================

def token_of(line, key):
    for tok in line.split()[1:]:
        name, _, value = tok.partition("=")

        if name == key:
            return value

    return ""


def parse_names_token(value):
    out = {}

    for item in str(value or "").split(","):
        item = item.strip()

        if not item:
            continue

        cid, sep, raw = item.partition(":")

        if not sep or not cid:
            continue

        out[cid] = unquote(raw)

    return out


class FakeMember:
    """一条成员控制连接：读推送、按需回 PONG、解析 HOST_STATE。"""

    def __init__(self, reader, writer, answer_ping=True):
        self.reader = reader
        self.writer = writer
        self.answer_ping = answer_ping
        self.lines = []
        self.raw_states = []
        self.names = {}
        self.state = {}
        self.you = ""
        self.ok_reply = ""
        self.kicked = None
        self.revoked = None
        self._cursor = 0
        self._event = asyncio.Event()
        self._task = None
        self.socket_closed = False

    async def join(self, line, timeout=8):
        self.writer.write((line + "\n").encode())
        await self.writer.drain()
        raw = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        self.ok_reply = raw.decode("utf-8", errors="replace").strip()
        self.you = unquote(token_of(self.ok_reply, "you"))
        self._task = asyncio.create_task(self._pump())
        return self.ok_reply

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

                if upper.startswith("PONG"):
                    continue

                if upper.startswith("HOST_STATE"):
                    self.raw_states.append(text)
                    self._apply_state(text)
                    continue

                if upper.startswith("KICKED"):
                    self.kicked = text
                    continue

                if upper.startswith("ROOM REVOKED"):
                    self.revoked = text
                    continue
        except Exception:
            pass
        finally:
            self.socket_closed = True
            self._event.set()

    def _apply_state(self, text):
        state = {}

        for tok in text.split()[1:]:
            key, _, value = tok.partition("=")
            state[key] = value

        self.state = state
        self.names = parse_names_token(state.get("names", ""))

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
        """不用 LEAVE，直接断 —— 复现"掉线"。"""
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
    member = FakeMember(reader, writer, answer_ping=answer_ping)
    reply = await member.join(line, timeout=timeout)
    return member, reply


async def send_line(port, line, timeout=8):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        reply = raw.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        reply = ""

    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()

    return reply


# ============================================================
# 管理 API（读快照：host_claimed / host_name / host_now 必须同源）
# ============================================================

def admin_request(port, method, path, payload=None, timeout=8):
    url = f"http://127.0.0.1:{port}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    if data is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")

        with contextlib.suppress(Exception):
            return error.code, json.loads(body)

        return error.code, {"ok": False, "error": body}


def find_room(port, room_id):
    status, payload = admin_request(port, "GET", "/rooms")

    if status != 200 or not payload.get("ok"):
        return None

    for room in payload.get("data") or []:
        if room.get("room") == room_id:
            return room

    return None


# ============================================================
# 5. 真中继：host 离开 -> 清主机位 + 广播 host=0 + 断开所有游戏连接
# ============================================================
#
# 用户 2026-09-27 最终口径（A′）：host 以任何方式离开/掉线/被踢/超时 ——
#   1. 主机位清空 + 唯一一行 [HOST RELEASE]；
#   2. 房间里所有**游戏连接**（PLAYER / HOST_TUNNEL / 配对会话）断开，且都能
#      拿到可识别的原因 `HOST_RELEASED`（不能静默关闭）；
#   3. **房间保留**：成员控制连接不动，谁都可以重新认领 host；
#   4. 没有 Host 时新的 PLAYER / HOST_TUNNEL 被拒（ERROR NO_HOST）；
#      Host 位有人认领但还没开服 -> ERROR HOST_NOT_READY（两者必须可区分）。
# 特例：离开的 host 同时是 owner -> 走既有规则，房间关闭。

async def wait_player_no_host(port, room, timeout=8):
    return await send_line(
        port, f"PLAYER {room} cid-late 迟到", timeout=timeout
    )


async def open_raw(port, line, timeout=8, read_reply=True):
    """
    发一条握手行，但**不关** socket（要留着用）。

    `read_reply=False` 给 PLAYER：它在配对前一句话都不回，硬等会把后面
    真正要断言的那一行（HOST_RELEASED）当成"握手回复"读掉。
    """
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


async def open_game_host(port, room, cid, name):
    """
    真 v3 房主：成员连接之外还会开一条老式 `HOST <room>` 控制连接
    （host_writer）。PLAYER / HOST_TUNNEL 的"有没有房主"看的就是它。
    """
    return await open_raw(port, f"HOST {room} {cid} {name}")


async def expect_host_left_line(reader, timeout=10):
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return raw.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        return ""


async def test_host_leaves_keeps_room_but_drops_games(port, admin_port):
    print("")
    print("---- 5a. host 掉线（身份结束）：游戏连接断开(HOST_RELEASED) + 房间保留 + 可再认领 ----")

    room = "hostrel-drop"

    owner, owner_reply = await open_member(
        port, f"CREATE __mclink__ cid-owner 房主 - {room}"
    )
    a, _ = await open_member(port, f"JOIN __mclink__ cid-a 甲 - {room}")
    b, _ = await open_member(port, f"JOIN __mclink__ cid-b 乙 - {room}")
    room_token = token_of(owner_reply, "room")

    # A 是"真房主"：成员位 + 老式 HOST 控制连接（后者才是 PLAYER 认的 host）。
    h_reader, h_writer, h_reply = await open_game_host(
        port, room_token, "cid-a", "甲"
    )

    check("5a: 房主的老式 HOST 控制连接建立", h_reply.startswith("OK HOST"),
          h_reply)

    try:
        await a.send("CLAIM_HOST")
        check("5a: A 认领 host", bool(await a.expect("OK CLAIM_HOST", timeout=8)))
        check("5a: B 先看到 host=1",
              await b.wait_state(lambda m: m.state.get("host") == "1",
                                 timeout=8), str(b.state))

        # ---- 一个正在等配对的 PLAYER（host 还在时进得来）----
        player_reader, player_writer, _ = await open_raw(
            port, f"PLAYER {room} cid-p 玩家", read_reply=False
        )
        player_task = asyncio.create_task(
            expect_host_left_line(player_reader)
        )

        await asyncio.sleep(0.5)

        # ---- A 掉线（不发 LEAVE） ----
        await a.drop_socket()

        line = await asyncio.wait_for(player_task, timeout=12)

        # ★ 用户 2026-09-30（最后一条）：这条路径是"主机位**真的空出来**了"
        #   （身份结束 / 被踢 / 超时 / 老式开服连接断开），所以原因是
        #   `HOST_RELEASED`：客户端据此断开 + 重算 + 不自动重连。
        #   只有"闪断、身份还在宽限期里"才是 `HOST_LEFT`。
        check("★ 5a: 在场的 PLAYER 被断开且收到可识别原因 HOST_RELEASED",
              line.startswith("ERROR HOST_RELEASED"), repr(line))

        freed = await b.wait_state(
            lambda m: m.state.get("host") == "0", timeout=10,
            label="B 收到 host=0",
        )

        check("★ 5a: host 掉线后广播 host=0（host_name 同时清空）",
              freed and b.state.get("host_name") in ("", "-"), str(b.state))

        # ---- A′：房间还在，谁都能重新认领 ----
        check("★ 5a: 房间**没有被销毁**（用户要的是断开游戏连接，不是关房）",
              find_room(admin_port, room_token) is not None,
              str(find_room(admin_port, room_token)))

        await b.send("CLAIM_HOST")
        check("★ 5a: host 掉线后 B 能立刻重新认领（不用等、不用重连）",
              bool(await b.expect("OK CLAIM_HOST", timeout=8)),
              "OK CLAIM_HOST")

        # ---- Host 位有人认领但 Host 还没开服 -> HOST_NOT_READY ----
        #
        # 设计基准 §B（用户 2026-09-27 确认）："有 Host 但 Host 未开始连接"
        # 时其他玩家点开始连接**不得进入正式流程**，而且原因码必须与
        # "房间里根本没有 Host" 区分开，客户端才能分别提示
        # 「Host 未连接，请等待 Host 开服」/「当前房间暂无 Host」。
        not_ready = await wait_player_no_host(port, room)

        check(
            "★ 5a: Host 位有人认领但未开服 -> ERROR HOST_NOT_READY（可识别）",
            not_ready.startswith("ERROR HOST_NOT_READY"),
            not_ready,
        )

        # ---- 真正"没有 Host"（没人认领主机位）才是 NO_HOST ----
        no_host_room = "hostrel-nohost"
        n_owner, _ = await open_member(
            port, f"CREATE __mclink__ cid-nh 无主房主 - {no_host_room}"
        )

        try:
            rejected = await wait_player_no_host(port, no_host_room)

            check(
                "★ 5a: 没人认领 Host 时新 PLAYER -> ERROR NO_HOST",
                rejected.startswith("ERROR NO_HOST"),
                rejected,
            )
        finally:
            await n_owner.close()

        with contextlib.suppress(Exception):
            player_writer.close()
    finally:
        for member in (owner, a, b):
            await member.close()

        with contextlib.suppress(Exception):
            h_writer.close()


async def test_host_leave_and_kick(port, admin_port):
    print("")
    print("---- 5b/5c. host 主动退房 / 被踢：同一条释放路径，房间保留 ----")

    # ---- 主动 LEAVE ----
    room = "hostrel-leave"

    owner, _ = await open_member(
        port, f"CREATE __mclink__ cid-owner 房主 - {room}"
    )
    a, _ = await open_member(port, f"JOIN __mclink__ cid-a 甲 - {room}")
    b, _ = await open_member(port, f"JOIN __mclink__ cid-b 乙 - {room}")

    try:
        await a.send("CLAIM_HOST")
        check("5b: A 认领 host", bool(await a.expect("OK CLAIM_HOST", timeout=8)))
        await b.wait_state(lambda m: m.state.get("host") == "1", timeout=8)

        await a.send("LEAVE")
        check("5b: A 收到 OK LEAVE", bool(await a.expect("OK LEAVE", timeout=8)))

        check("★ 5b: host 主动退房 -> B 收到 host=0",
              await b.wait_state(lambda m: m.state.get("host") == "0",
                                 timeout=10),
              str(b.state))

        kept = find_room(admin_port, room) is not None
        reclaimed = await _claim(b)

        check("★ 5b: 房间保留（不销毁），B 能重新认领",
              kept and bool(reclaimed), f"kept={kept} claim={reclaimed!r}")
    finally:
        for member in (owner, a, b):
            await member.close()

    # ---- 被 owner 踢 ----
    room2 = "hostrel-kick"

    owner2, _ = await open_member(
        port, f"CREATE __mclink__ cid-owner2 房主 - {room2}"
    )
    a2, _ = await open_member(port, f"JOIN __mclink__ cid-a2 丙 - {room2}")
    b2, _ = await open_member(port, f"JOIN __mclink__ cid-b2 丁 - {room2}")

    try:
        await a2.send("CLAIM_HOST")
        check("5c: A2 认领 host", bool(await a2.expect("OK CLAIM_HOST", timeout=8)))
        await b2.wait_state(lambda m: m.state.get("host") == "1", timeout=8)

        await owner2.send(f"KICK {room2} cid-a2")
        check("5c: owner 踢掉 host", bool(await owner2.expect("OK KICK", timeout=8)))

        check("★ 5c: host 被踢 -> B2 收到 host=0",
              await b2.wait_state(lambda m: m.state.get("host") == "0",
                                  timeout=10),
              str(b2.state))

        await b2.send("CLAIM_HOST")
        check("★ 5c: 房间保留，B2 能重新认领",
              bool(await b2.expect("OK CLAIM_HOST", timeout=8)),
              "OK CLAIM_HOST")
    finally:
        for member in (owner2, a2, b2):
            await member.close()


async def _claim(member, timeout=8):
    await member.send("CLAIM_HOST")
    return await member.expect("OK CLAIM_HOST", timeout=timeout)


# ============================================================
# 6. 真中继：host_claimed 与 host_name 永远同源（claim/release/claim/leave）
# ============================================================

def pair_is_consistent(text):
    """`host=1` 必须带真名字；`host=0` 必须没有名字。"""
    state = {}

    for tok in text.split()[1:]:
        key, _, value = tok.partition("=")
        state[key] = value

    host = state.get("host", "")
    name = state.get("host_name", "")

    if host == "1":
        return name not in ("", "-")

    if host == "0":
        return name in ("", "-")

    return False


async def test_host_pair_never_contradicts(port, admin_port):
    print("")
    print("---- 6. 真中继：host= 与 host_name= 在任何一刻都不自相矛盾 ----")

    room = "hostrel-pair"

    owner, owner_reply = await open_member(
        port, f"CREATE __mclink__ cid-owner 房主 - {room}"
    )
    a, a_reply = await open_member(port, f"JOIN __mclink__ cid-a 甲 - {room}")
    b, b_reply = await open_member(port, f"JOIN __mclink__ cid-b 乙 - {room}")

    try:
        check("★ OK JOIN 回包里 host=0 同时给出 host_name=-",
              token_of(b_reply, "host") == "0"
              and token_of(b_reply, "host_name") in ("", "-"),
              b_reply)

        # ---- claim ----
        await a.send("CLAIM_HOST")
        assert await a.expect("OK CLAIM_HOST", timeout=8)

        await b.wait_state(lambda m: m.state.get("host") == "1", timeout=8)

        await asyncio.sleep(0.3)
        snapped = find_room(admin_port, token_of(a_reply, "room"))

        check("★ 快照里 host_claimed=True 且 host_name=甲（同一个来源）",
              snapped is not None
              and snapped.get("host_claimed") is True
              and snapped.get("host_name") == "甲"
              and snapped.get("host_now") == "甲",
              str({k: (snapped or {}).get(k)
                   for k in ("host_claimed", "host_name", "host_now")}))

        # ---- release ----
        await a.send("RELEASE_HOST")
        assert await a.expect("OK RELEASE_HOST", timeout=8)

        await b.wait_state(lambda m: m.state.get("host") == "0", timeout=8)
        await asyncio.sleep(0.3)
        snapped = find_room(admin_port, token_of(a_reply, "room"))

        check("★ 释放后快照 host_claimed=False 且 host_name=''",
              snapped is not None
              and snapped.get("host_claimed") is False
              and not (snapped.get("host_name") or ""),
              str({k: (snapped or {}).get(k)
                   for k in ("host_claimed", "host_name", "host_now")}))

        # ---- claim again ----
        await a.send("CLAIM_HOST")
        assert await a.expect("OK CLAIM_HOST", timeout=8)

        # C 在 A 持有主机位时进房：OK JOIN 必须 host=1 + host_name=甲
        c, c_reply = await open_member(
            port, f"JOIN __mclink__ cid-c 丙 - {room}"
        )

        try:
            check("★ A 持有主机位时，C 的 OK JOIN 是 host=1 + host_name=甲",
                  token_of(c_reply, "host") == "1"
                  and token_of(c_reply, "host_name") == "甲",
                  c_reply)
        finally:
            await c.close()

        # ---- leave（host 退房）----
        await a.send("LEAVE")
        check("A LEAVE", bool(await a.expect("OK LEAVE", timeout=8)))

        check("★ A 离开后 B 收到 host=0（同一条广播）",
              await b.wait_state(lambda m: m.state.get("host") == "0",
                                 timeout=10),
              str(b.raw_states[-2:]))

        awaited = await b.wait_state(
            lambda m: m.state.get("host_name") in ("", "-"), timeout=3
        )
        check("★ 同一刻 host_name 一定是空的（没有「有主机但没名字」）",
              awaited, str(b.state))

        # ---- 全量扫描：任何一条 HOST_STATE 都不许自相矛盾 ----
        all_states = owner.raw_states + b.raw_states

        bad = [s for s in all_states if not pair_is_consistent(s)]

        check("★ 全过程所有 HOST_STATE 的 (host, host_name) 都一致",
              all_states and not bad,
              f"共 {len(all_states)} 条，坏 {len(bad)} 条：{bad[:3]}")
    finally:
        for member in (owner, a, b):
            await member.close()


# ============================================================
# 7. 真中继：游客身份是"登录会话"级的（房间进出不动它，账号退出才清）
# ============================================================

async def test_guest_session_across_rooms(port, admin_port):
    print("")
    print("---- 7. 真中继：游客临时 token 跨房间存活（退房/被踢/房间被关）----")

    room = "hostrel-guest"

    owner, owner_reply = await open_member(
        port, f"CREATE __mclink__ cid-owner 房主 - {room}"
    )
    room_token = token_of(owner_reply, "room")

    g1, g1_reply = await open_member(
        port, f"JOIN __mclink__ cid-guest - - {room}"
    )

    name1 = g1.you
    tok1 = token_of(g1_reply, "guest")

    try:
        check("★ 游客拿到「游客+6位」的名字",
              bool(GUEST_RE.match(name1 or "")), name1)
        check("★ OK JOIN 回包带一条临时会话 token（guest=<token>）",
              bool(tok1) and tok1.startswith("guest-"), f"guest={tok1!r}")

        # ---- 1) 主动退房：身份不销毁 ----
        await g1.send("LEAVE")
        await g1.expect("OK LEAVE", timeout=8)
        await asyncio.sleep(0.4)
        await g1.close()

        g2, g2_reply = await open_member(
            port, f"JOIN __mclink__ cid-guest - {tok1} {room}"
        )

        check("★ 退房后再进房：拿同一条临时 token，名字不变",
              g2.you == name1 and GUEST_RE.match(g2.you or ""),
              f"{name1!r} -> {g2.you!r}")

        # ---- 2) 被踢：身份不销毁 ----
        await owner.send(f"KICK {room_token} cid-guest")
        check("owner 踢掉游客", bool(await owner.expect("OK KICK", timeout=8)))
        check("游客收到 KICKED", bool(await g2.expect("KICKED", timeout=8)))
        await g2.close()
        await asyncio.sleep(0.4)

        g3, _ = await open_member(
            port, f"JOIN __mclink__ cid-guest - {tok1} {room}"
        )

        check("★ 被踢后再进房：名字仍然不变（踢人不销毁登录身份）",
              g3.you == name1, f"{name1!r} -> {g3.you!r}")

        # ---- 3) 房间被销毁（owner 退房 -> 房间关闭）：身份不销毁 ----
        await owner.send("LEAVE")
        await owner.expect("OK LEAVE", timeout=8)
        await asyncio.sleep(0.5)
        check("房间确实被销毁（owner 退房 -> 房间里所有人也被断开）",
              find_room(admin_port, room_token) is None,
              str(find_room(admin_port, room_token)))
        await g3.close()

        other = "hostrel-guest-other"
        g4, _ = await open_member(
            port, f"CREATE __mclink__ cid-guest - {tok1} {other}"
        )

        check("★ 房间被关闭后换一个房间建房：同一条 token，名字不变",
              g4.you == name1, f"{name1!r} -> {g4.you!r}")
        await g4.close()
    finally:
        await owner.close()


async def test_guest_session_ends_on_logout(port):
    print("")
    print("---- 7b. 游客会话结束（账号退出）-> 身份清除、下次连接拿新名 ----")

    room = "hostrel-guest-end"

    owner, _ = await open_member(
        port, f"CREATE __mclink__ cid-owner 房主 - {room}"
    )

    g1, g1_reply = await open_member(
        port, f"JOIN __mclink__ cid-guest - - {room}"
    )

    name1 = g1.you
    tok1 = token_of(g1_reply, "guest")

    try:
        # ---- 账号退出：LOGOUT 掉了这条临时 token ----
        out = await send_line(
            port, f"LOGOUT {SENTINEL} cid-guest - {tok1}"
        )

        check("★ 游客的临时 token 也能 LOGOUT（账号退出 = 会话结束）",
              out.startswith("OK LOGOUT"), out)

        # 再拿旧 token 进来 -> 旧身份已被清除，必须是**新**名字 + 新 token
        g2, g2_reply = await open_member(
            port, f"JOIN __mclink__ cid-guest - {tok1} {room}"
        )

        name2 = g2.you
        tok2 = token_of(g2_reply, "guest")

        check("★ 会话结束后旧 token 失效：拿到新的游客名",
              GUEST_RE.match(name2 or "") is not None and name2 != name1,
              f"{name1!r} -> {name2!r}")
        check("★ 同时发了一条新的临时 token（不复用旧的）",
              bool(tok2) and tok2 != tok1, f"{tok1!r} -> {tok2!r}")

        # ---- 登录态丢失（重连时根本不带 token）-> 也是新身份 ----
        await g2.close()

        g3, g3_reply = await open_member(
            port, f"JOIN __mclink__ cid-guest - - {room}"
        )

        check("★ 重连没带 token（登录态丢失）-> 另一个新游客名",
              GUEST_RE.match(g3.you or "") is not None
              and g3.you not in (name1, name2),
              f"{name1!r}/{name2!r} -> {g3.you!r}")
        await g3.close()
    finally:
        await owner.close()


# ============================================================
# 8. 真中继：IDENTITY 成功路径（游客 -> 登录名，全员广播刷新，不留两条记录）
# ============================================================

async def test_identity_success(port, admin_port):
    print("")
    print("---- 8. 真中继：IDENTITY 把游客名换成登录名并广播 ----")

    room = "hostrel-identity"

    owner, owner_reply = await open_member(
        port, f"CREATE __mclink__ cid-owner 房主 - {room}"
    )
    room_token = token_of(owner_reply, "room")
    guest, guest_reply = await open_member(
        port, f"JOIN __mclink__ cid-guest - - {room}"
    )
    guest_token = token_of(guest_reply, "guest")

    try:
        await owner.wait_state(
            lambda m: m.names.get("cid-guest") == guest.you, timeout=8
        )

        check("游客进房时收件人是中继分配的游客名",
              GUEST_RE.match(guest.you or "") is not None, guest.you)

        login = await send_line(
            port,
            f"LOGIN {SENTINEL} {ADMIN_USER} {ADMIN_PASS} cid-login",
        )
        token = token_of(login, "token")

        check("登录拿到 token（用于 IDENTITY）",
              login.startswith("OK LOGIN") and bool(token), login)

        await guest.send(f"IDENTITY {ADMIN_USER} {token}")
        ack = await guest.expect("OK IDENTITY", timeout=8)

        check("★ IDENTITY 回 OK IDENTITY <登录名>",
              ack == f"OK IDENTITY {ADMIN_USER}", str(ack))

        refreshed = await owner.wait_state(
            lambda m: m.names.get("cid-guest") == ADMIN_USER, timeout=8,
            label="owner 收到新的 names=",
        )

        check("★ 全员立刻收到新 HOST_STATE：names= 里那颗名字变成登录名",
              refreshed, str(owner.names))

        await asyncio.sleep(0.3)
        snapped = find_room(admin_port, room_token)
        members = (snapped or {}).get("member_list") or []
        by_cid = [m for m in members if m.get("client_id") == "cid-guest"]

        check("★ 中继侧没有留下两条成员记录（同一个成员对象，人数不变）",
              len(members) == 2 and len(by_cid) == 1,
              str([(m.get("client_id"), m.get("display_name"))
                   for m in members]))
        check("★ 那一条记录的显示名已经是登录名",
              by_cid and by_cid[0].get("display_name") == ADMIN_USER,
              str(by_cid))

        # 换账号登录 = 游客会话结束：旧的临时 token 不再有效
        await guest.close()
        await asyncio.sleep(0.3)

        again, again_reply = await open_member(
            port, f"JOIN __mclink__ cid-guest - {guest_token} {room}"
        )

        check("★ 用账号登录后，旧游客临时 token 已失效（换账号 = 会话结束）",
              again.you != guest.you and GUEST_RE.match(again.you or ""),
              f"{guest.you!r} -> {again.you!r}")
        await again.close()
    finally:
        for member in (owner, guest):
            await member.close()


# ============================================================
# 9. 版本门 / token 校验（accounts required + MCLINK_VERSION=9.9.9）
# ============================================================

async def test_identity_gate(port):
    print("")
    print("---- 9. IDENTITY：token 无效 / 版本不一致都被拒 ----")

    room = "hostrel-gate"

    login = await send_line(
        port, f"LOGIN {SENTINEL} {ADMIN_USER} {ADMIN_PASS} cid-login"
    )
    token = token_of(login, "token")

    check("门测试：登录拿到 token",
          login.startswith("OK LOGIN") and bool(token), login)

    member, reply = await open_member(
        port,
        f"CREATE __mclink__ cid-gate {ADMIN_USER} {token} {room} "
        f"ver={SERVER_VERSION}",
    )

    check("门测试：同版本的 CREATE 能进房",
          reply.startswith("OK JOIN"), reply)

    try:
        # ---- token 无效 ----
        await member.send(f"IDENTITY {ADMIN_USER} not-a-real-token "
                          f"ver={SERVER_VERSION}")
        bad = await member.expect("ERROR IDENTITY", timeout=8)

        check("★ token 无效 -> ERROR IDENTITY_INVALID_TOKEN",
              bool(bad) and bad.startswith("ERROR IDENTITY_INVALID_TOKEN"),
              str(bad))

        # ---- 版本不一致 ----
        await member.send(f"IDENTITY {ADMIN_USER} {token} ver=1.0.0")
        mismatch = await member.expect("ERROR VERSION_MISMATCH", timeout=8)

        check("★ 版本不一致 -> ERROR VERSION_MISMATCH（两边版本都摊开）",
              mismatch
              == f"ERROR VERSION_MISMATCH server={SERVER_VERSION} client=1.0.0",
              str(mismatch))

        # ---- 同版本 + 有效 token：正常 ----
        await member.send(f"IDENTITY {ADMIN_USER} {token} "
                          f"ver={SERVER_VERSION}")
        good = await member.expect("OK IDENTITY", timeout=8)

        check("★ 同版本 + 有效 token -> OK IDENTITY",
              good == f"OK IDENTITY {ADMIN_USER}", str(good))
    finally:
        await member.close()


# ============================================================
# 10. 真中继：host 仅被动超时（不作任何主动操作）也走同一条释放路径
# ============================================================

async def test_host_timeout_disconnects(port, admin_port):
    print("")
    print("---- 10. host 仅超时（看门狗）：游戏连接断开 + 房间保留 ----")

    room = "hostrel-timeout"

    owner, owner_reply = await open_member(
        port, f"CREATE __mclink__ cid-owner 房主 - {room}"
    )
    # A 认领 host 之后**不再发任何东西**、也不回 PING -> 只会被看门狗收走。
    a, _ = await open_member(
        port, f"JOIN __mclink__ cid-a 甲 - {room}", answer_ping=False
    )
    b, _ = await open_member(port, f"JOIN __mclink__ cid-b 乙 - {room}")
    room_token = token_of(owner_reply, "room")

    h_reader, h_writer, h_reply = await open_game_host(
        port, room_token, "cid-a", "甲"
    )

    try:
        await a.send("CLAIM_HOST")
        check("10: A 认领 host",
              bool(await a.expect("OK CLAIM_HOST", timeout=8)))
        check("10: B 先看到 host=1",
              await b.wait_state(lambda m: m.state.get("host") == "1",
                                 timeout=8), str(b.state))

        player_reader, player_writer, _ = await open_raw(
            port, f"PLAYER {room} cid-p 玩家", read_reply=False
        )
        player_task = asyncio.create_task(expect_host_left_line(player_reader))
        await asyncio.sleep(0.5)

        # A 从此静默。不给任何主动操作，等看门狗。
        check("★ 10: host 仅超时 -> B 收到 host=0",
              await b.wait_state(lambda m: m.state.get("host") == "0",
                                 timeout=20, label="B 收到 host=0（超时）"),
              str(b.raw_states[-3:]))

        line = await asyncio.wait_for(player_task, timeout=12)

        # ★ 用户 2026-09-30（最后一条）：超时清退 = 主机位**真的空出来**了，
        #   所以给的是 `HOST_RELEASED`（断开 + 重算 + 不自动重连），
        #   闪断（身份还在宽限期里）才是 `HOST_LEFT`。
        check("★ 10: 超时断开游戏连接时给的是 HOST_RELEASED（不是静默关闭）",
              line.startswith("ERROR HOST_RELEASED"), repr(line))

        check("★ 10: 房间保留，B 能立刻重新认领",
              find_room(admin_port, room_token) is not None
              and bool(await _claim(b)),
              str(find_room(admin_port, room_token)))

        with contextlib.suppress(Exception):
            player_writer.close()
    finally:
        for member in (owner, a, b):
            await member.close()

        with contextlib.suppress(Exception):
            h_writer.close()


# ============================================================
# 驱动
# ============================================================


async def run_real_relay_tests():
    main_proc, main_log, main_log_path = start_relay(
        RELAY_PORT, ADMIN_PORT, "host_release_identity_relay.log"
    )
    main_ok = port_alive(main_proc)

    check("主中继启动（账号可选 + 门关）", main_ok,
          "进程已退出，见 tests/host_release_identity_relay.log")

    try:
        if main_ok:
            await test_host_leaves_keeps_room_but_drops_games(RELAY_PORT, ADMIN_PORT)
            await test_host_leave_and_kick(RELAY_PORT, ADMIN_PORT)
            await test_host_pair_never_contradicts(RELAY_PORT, ADMIN_PORT)
            await test_guest_session_across_rooms(RELAY_PORT, ADMIN_PORT)
            await test_guest_session_ends_on_logout(RELAY_PORT)
            await test_identity_success(RELAY_PORT, ADMIN_PORT)
    finally:
        stop_relay(main_proc, main_log)

    # ---- 日志：四条移除路径的释放行格式 ----
    log_text = read_log(main_log_path)
    releases = [
        line for line in log_text.splitlines()
        if "[HOST RELEASE] room=" in line and "reason=" in line
    ]
    by_reason = {}

    for line in releases:
        match = re.search(r"reason=(\w+)", line)

        if match:
            by_reason.setdefault(match.group(1), []).append(line)

    check("★ 掉线（drop）释放 host 时记了一行 [HOST RELEASE]",
          bool(by_reason.get("drop")), str(by_reason.get("drop", [])))
    check("★ 明确 LEAVE 释放 host 时记了一行 reason=leave",
          bool(by_reason.get("leave")), str(by_reason.get("leave", [])))
    check("★ 被踢释放 host 时记了一行 reason=kick",
          bool(by_reason.get("kick")), str(by_reason.get("kick", [])))
    check("★ 每行都带 room= / by= / reason= 三个字段",
          releases
          and all("room=" in ln and "by=" in ln and "reason=" in ln
                  for ln in releases),
          str(releases[:4]))

    # ---- 门中继：账号必需 + 版本 9.9.9 ----
    gate_proc, gate_log, gate_log_path = start_relay(
        GATE_PORT, GATE_ADMIN_PORT, "host_release_identity_gate.log",
        version=SERVER_VERSION, require_account=1,
    )
    gate_ok = port_alive(gate_proc)

    check("门中继启动（账号必需 + MCLINK_VERSION=9.9.9）", gate_ok,
          "进程已退出，见 tests/host_release_identity_gate.log")

    try:
        if gate_ok:
            await test_identity_gate(GATE_PORT)
    finally:
        stop_relay(gate_proc, gate_log)

    gate_text = read_log(gate_log_path)

    check("★ IDENTITY 的版本门也留下 [VERSION REJECT] 记录（role=IDENTITY）",
          "[VERSION REJECT]" in gate_text and "role=IDENTITY" in gate_text,
          "见 tests/host_release_identity_gate.log")

    # ---- 超时中继：把看门狗周期压到秒级，验"仅被动超时"这条路径 ----
    timeout_proc, timeout_log, timeout_log_path = start_relay(
        TIMEOUT_PORT, TIMEOUT_ADMIN_PORT,
        "host_release_identity_timeout.log",
        extra_env={
            "MCLINK_MEMBER_IDLE_TIMEOUT": "5",
            "MCLINK_MEMBER_WATCHDOG_INTERVAL": "1",
        },
    )
    timeout_ok = port_alive(timeout_proc)

    check("超时中继启动（看门狗 1s / 静默 5s）", timeout_ok,
          "进程已退出，见 tests/host_release_identity_timeout.log")

    try:
        if timeout_ok:
            await test_host_timeout_disconnects(TIMEOUT_PORT, TIMEOUT_ADMIN_PORT)
    finally:
        stop_relay(timeout_proc, timeout_log)

    timeout_text = read_log(timeout_log_path)

    check("★ 仅超时移除 host 也记 [HOST RELEASE] reason=timeout",
          "[HOST RELEASE]" in timeout_text and "reason=timeout" in timeout_text,
          "见 tests/host_release_identity_timeout.log")


def main():
    print("=" * 66)
    print("CraftLink 中继：host 释放 / host_claimed 同源 / IDENTITY + 游客一次性身份")
    print("=" * 66)

    try:
        test_host_state_is_one_source()
    except Exception:
        import traceback
        check("host_state 同源单测没有抛异常", False)
        traceback.print_exc()
    else:
        check("host_state 同源单测没有抛异常", True)

    try:
        test_take_host_is_authoritative()
    except Exception:
        import traceback
        check("take_host_from 单测没有抛异常", False)
        traceback.print_exc()
    else:
        check("take_host_from 单测没有抛异常", True)

    try:
        test_watchdog_marks_timeout()
    except Exception:
        import traceback
        check("看门狗 timeout 单测没有抛异常", False)
        traceback.print_exc()
    else:
        check("看门狗 timeout 单测没有抛异常", True)

    try:
        test_identity_unit()
    except Exception:
        import traceback
        check("IDENTITY 单测没有抛异常", False)
        traceback.print_exc()
    else:
        check("IDENTITY 单测没有抛异常", True)

    try:
        asyncio.run(run_real_relay_tests())
    except Exception:
        import traceback
        check("真中继 host/identity 测试没有抛异常", False)
        traceback.print_exc()
    else:
        check("真中继 host/identity 测试没有抛异常", True)

    print("")
    print("=" * 66)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 66)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
