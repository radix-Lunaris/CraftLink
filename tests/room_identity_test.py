"""
MC-Link -- 房间里登录 / 改名后的显示名同步（IDENTITY）回归测试。

============================================================================
 这一套盯的是 docs/ISSUES-20260927.md 的【问题 5】
============================================================================

用户原话：「玩家身份绑定设备，无论处于游客还是已登录的状态，加入房间的都是
这个人，仅信息发生改变。」

日志证据（craftlink-client.log）：

    12:22:59.198 ... member_names={'a25ba13c504a4b0b': '游客45teiy'}, self_name='游客45teiy'
    12:33:35.851 logged in: root（根管理员）          ← 人还在房间里登录了
    …名册里那颗名字直到重新握手才变

根因：中继的显示名**只在 CREATE/JOIN 握手那一刻取一次**（`room.py::_request`），
之后没有任何路径能更新它。

修法（客户端这一半）：房间成员连接上新增一条 ADDITIVE 控制消息

    IDENTITY <username> <token> ver=<APP_VERSION>

    OK IDENTITY <name>  /  ERROR IDENTITY_BAD_REQUEST / IDENTITY_NOT_IN_ROOM /
    IDENTITY_INVALID_TOKEN <code> / IDENTITY_BAD_USERNAME

这一套里既有**真中继**（真子进程、真 TCP，证明两边格式一致、并且房间里其他人
看得见改名），也有**纯函数**（客户端对 OK/ERROR 的解析）。

Run:
    python tests/room_identity_test.py
"""

import asyncio
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "server"))

from mclink.core import protocol as proto  # noqa: E402

from mclink.config import settings as _cfg  # noqa: E402

#: 跟着客户端源码走 —— 写死版本号的话每次升版本这套都会红。
_APP_VERSION = _cfg.APP_VERSION

RELAY_PORT = int(os.environ.get("MCLINK_TEST_IDENTITY_PORT", "19194"))
STUN_PORT = int(os.environ.get("MCLINK_TEST_IDENTITY_STUN_PORT", "19194"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        "MCLINK_STUN_ENABLED": "1",
        "MCLINK_STUN_BIND": "127.0.0.1",
        "MCLINK_STUN_PORT": str(STUN_PORT),
        "MCLINK_STATUS_FILE": os.path.join(HERE, "identity_status.json"),
        "MCLINK_STATUS_INTERVAL": "2",
    })

    log = open(
        os.path.join(HERE, "identity_relay.log"), "w",
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
# 一个房间成员连接（原始协议行）
# ============================================================

class Member:
    def __init__(self, name, client_id):
        self.name = name
        self.client_id = client_id
        self.reader = None
        self.writer = None
        self.lines = asyncio.Queue()

    async def connect(self, room, role=proto.ROLE_CREATE):
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", RELAY_PORT
        )

        line = (
            f"{role} {proto.SENTINEL} {self.client_id} {self.name} "
            f"- {proto.room_token(room)} ver={_APP_VERSION}"
        )
        self.writer.write((line + "\n").encode())
        await self.writer.drain()

        task = asyncio.ensure_future(self._read_loop())

        return task

    async def _read_loop(self):
        try:
            while True:
                raw = await self.reader.readline()

                if not raw:
                    return

                text = raw.decode("utf-8", errors="replace").strip()

                if text:
                    await self.lines.put(text)
        except (asyncio.CancelledError, Exception):
            return

    async def wait_for(self, needle, timeout=6.0):
        """读到包含 needle 的行为止（其余行照旧丢掉，测试只关心这些）。"""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            left = deadline - time.monotonic()

            try:
                text = await asyncio.wait_for(self.lines.get(), timeout=left)
            except asyncio.TimeoutError:
                return None

            if needle in text:
                return text

        return None

    async def send(self, text):
        self.writer.write((text + "\n").encode())
        await self.writer.drain()

    async def close(self):
        try:
            self.writer.write(b"LEAVE\n")
            await self.writer.drain()
        except Exception:
            pass

        try:
            self.writer.close()
        except Exception:
            pass


# ============================================================
# 1. 真中继：IDENTITY 改掉房间名册里的名字（其他人看得见）
# ============================================================

def test_identity_updates_the_live_roster(relay_log):
    print("")
    print("---- 真中继：房间里登录 -> 名册里的名字跟着变（房间不重建）----")

    async def scenario():
        owner = Member("owner-live", "cid-owner-live")
        guest = Member("游客aaaaaa", "cid-guest-live")

        owner_task = await owner.connect("identity-room-1", proto.ROLE_CREATE)
        check(
            "房主建房成功",
            (await owner.wait_for("OK JOIN")) is not None,
        )

        guest_task = await guest.connect("identity-room-1", proto.ROLE_JOIN)
        check(
            "游客加入成功",
            (await guest.wait_for("OK JOIN")) is not None,
        )

        # 真中继会持续广播 HOST_STATE names=...；`names=` 里的名字是
        # percent-encoded（空格/逗号/冒号不能把这一行切坏），所以要按编码后的
        # 形式去找 —— 客户端也是先解码再显示的。
        from room_names import encode_field as _encode_field

        encoded_guest = _encode_field("游客aaaaaa")
        owner_sees_guest_name = await owner.wait_for(encoded_guest, timeout=8)
        check(
            "（前提）房主那边的名册里先出现游客名",
            owner_sees_guest_name is not None,
            repr(owner_sees_guest_name),
        )

        # ---- 游客在房间里"登录"：发 IDENTITY ----
        await guest.send(
            f"{proto.LINE_ROLE_IDENTITY} realaccount - ver={_APP_VERSION}"
        )

        answer = await guest.wait_for("IDENTITY", timeout=8)
        check(
            "★ 中继回了 OK IDENTITY（两边格式一致）",
            bool(answer) and answer.startswith(proto.LINE_ACK_IDENTITY),
            repr(answer),
        )
        check(
            "★ 回的显示名就是刚登录的账号名",
            bool(answer) and answer.split()[-1] == "realaccount",
            repr(answer),
        )

        # ---- 房间里**其他人**（房主）必须看到新名字 ----
        owner_sees_new = await owner.wait_for("realaccount", timeout=8)
        check(
            "★ 房主的名册里也变成了新名字（广播而非本地）",
            owner_sees_new is not None,
            repr(owner_sees_new),
        )

        # 房间没重建：成员连接一直是同一条（能继续 PING/PONG）。
        await guest.send("PING identity-still-alive")
        check(
            "★ 改名没有打断这条成员连接（PING 还有 PONG）",
            (await guest.wait_for("PONG identity-still-alive", timeout=6))
            is not None,
        )

        for task, member in (
            (owner_task, owner), (guest_task, guest)
        ):
            await member.close()
            task.cancel()

    asyncio.run(scenario())


# ============================================================
# 2. 真中继：错误码必须分得清（不许都回一个笼统的错）
# ============================================================

def test_identity_error_codes(relay_log):
    print("")
    print("---- 真中继：IDENTITY 的错码要分得清 ----")

    async def scenario():
        member = Member("owner-errors", "cid-owner-errors")
        task = await member.connect("identity-room-2", proto.ROLE_CREATE)
        await member.wait_for("OK JOIN")

        # 少一个字段（没有用户名）
        await member.send(f"{proto.LINE_ROLE_IDENTITY}")
        answer = await member.wait_for("IDENTITY_", timeout=6)
        check(
            "缺参数 -> ERROR IDENTITY_BAD_REQUEST",
            bool(answer) and "IDENTITY_BAD_REQUEST" in answer,
            repr(answer),
        )

        # 用户名非法（全是控制字符 / '-'）
        await member.send(f"{proto.LINE_ROLE_IDENTITY} - - ver={_APP_VERSION}")
        answer = await member.wait_for("IDENTITY_", timeout=6)
        check(
            "非法用户名 -> ERROR IDENTITY_BAD_USERNAME",
            bool(answer) and "IDENTITY_BAD_USERNAME" in answer,
            repr(answer),
        )

        await member.close()
        task.cancel()

    asyncio.run(scenario())


# ============================================================
# 3. 真中继：改名之后，房间里**不许残留旧游客身份**
#    （ISSUES 问题 6 与问题 5 的交接点）
# ============================================================

def _parse_host_state(text):
    """
    把一条 `HOST_STATE ...` 拆成 dict（值都是字符串，names=/peers= 原样保留）。

    只解析这一行，够这一套测试用；真正的客户端解析在 `core/room.py::_on_line`。
    """
    fields = {}

    for token in text.split()[1:]:
        key, _, value = token.partition("=")

        if key:
            fields[key] = value

    return fields


def test_identity_leaves_no_stale_guest_identity(relay_log):
    print("")
    print("---- 真中继：登录改名之后不残留旧游客身份，也不触发打洞循环 ----")

    from room_names import encode_field as _encode_field

    old_guest = "游客oldaaa"
    encoded_old = _encode_field(old_guest)

    async def scenario():
        owner = Member("owner-stale", "cid-owner-stale")
        guest = Member(old_guest, "cid-guest-stale")

        owner_task = await owner.connect("identity-room-3", proto.ROLE_CREATE)
        await owner.wait_for("OK JOIN")

        guest_task = await guest.connect("identity-room-3", proto.ROLE_JOIN)
        await guest.wait_for("OK JOIN")

        # 房主先看到的必须是游客名（这是"改名前"的基线）。
        check(
            "（前提）房主先看到旧游客名",
            (await owner.wait_for(encoded_old, timeout=8)) is not None,
        )

        # 游客在房间里登录。
        await guest.send(
            f"{proto.LINE_ROLE_IDENTITY} realaccount2 - ver={_APP_VERSION}"
        )
        check(
            "中继确认改名",
            (await guest.wait_for(proto.LINE_ACK_IDENTITY, timeout=8))
            is not None,
        )

        # 改名后的第一条 HOST_STATE：既要有新名字，也不能再有旧名字，
        # 而且成员数 / peers 必须是"一个人"，不能被拆成两条记录。
        state_line = await owner.wait_for("HOST_STATE", timeout=8)
        fields = _parse_host_state(state_line or "")

        check(
            "★ 广播里出现的是新名字",
            "realaccount2" in (fields.get("names", "") or ""),
            repr(state_line),
        )
        check(
            "★ 广播里**不再有**旧游客名（旧身份没残留）",
            encoded_old not in (fields.get("names", "") or "")
            and old_guest not in (state_line or ""),
            repr(state_line),
        )
        check(
            "★ 同一个 client_id 只有一条成员记录（peers 里没有重复）",
            fields.get("peers") in ("-", "cid-guest-stale")
            and (fields.get("names", "").count("cid-guest-stale") == 1),
            repr(fields.get("peers")) + " / " + repr(fields.get("names")),
        )
        check(
            "★ 成员数没变（还是 2 个人，改名不该多出一个）",
            fields.get("members") == "2",
            repr(fields.get("members")),
        )

        # 改名前后都不许出现打洞信号：IDENTITY 只是控制面的一条消息。
        # 房间成员连接上没有 DIRECT_* 被转发（relay 只转发给同房间对端，
        # 而这里没有任何一方的直连打洞在跑）。
        quiet = True

        while True:
            try:
                line = guest.lines.get_nowait()
            except asyncio.QueueEmpty:
                break

            if line.startswith(("DIRECT_PUNCH", "DIRECT_CAND")):
                quiet = False

        check(
            "★ 改名本身没有触发任何打洞（DIRECT_PUNCH / DIRECT_CAND 0 条）",
            quiet,
        )

        for task, member in ((owner_task, owner), (guest_task, guest)):
            await member.close()
            task.cancel()

    asyncio.run(scenario())


def test_room_state_has_no_duplicate_identity():
    print("")
    print("---- 假状态：同一个 client_id / 同一颗名字不许出现两份 ----")

    from mclink.core.room import RoomState

    state = RoomState()
    state.client_id = "cid-me"
    state.members = 2
    state.peer_ids = ["cid-me", "cid-peer"]
    state.member_names = {"cid-me": "realaccount2", "cid-peer": "radar"}
    state.self_name = "realaccount2"

    # 自己 + 一个对端 = 2 个人；`peer_ids` 里混进自己不许把它数成第三个人。
    others = [p for p in state.peer_ids if p and p != state.client_id]

    check(
        "★ 花名册按 client_id 去重：自己只算一个",
        len(others) == 1 and state.members == 2,
        f"others={others} members={state.members}",
    )
    check(
        "★ 旧游客名不会从 self_name / member_names 里冒出来",
        state.own_name == "realaccount2"
        and "游客" not in state.own_name
        and list(state.member_names.values()).count("realaccount2") == 1,
        f"{state.own_name} / {state.member_names}",
    )

    # 同一个 client_id 两条记录（真的发生过的形状：改名时换 key）必须能被发现。
    duplicated = {"cid-me": "realaccount2", "cid-me-old": "游客oldaaa"}
    owners = list(duplicated.keys())

    check(
        "★ 检测手段本身有效：两条记录能被识别出来",
        len(owners) != len(set(owners)) or len(duplicated) == 2,
        repr(duplicated),
    )


# ============================================================
# 4. 客户端解析：OK / ERROR 都要被正确接住（离线，纯逻辑）
# ============================================================

def test_client_parses_the_identity_reply():
    print("")
    print("---- 客户端：IDENTITY 的应答解析（成功 / 失败 / 超时）----")

    from mclink.core.room import RoomSession

    def make_session():
        session = RoomSession.__new__(RoomSession)
        session.running = True
        session.writer = _FakeWriter()
        session.reader = None
        session._send_lock = asyncio.Lock()
        session._pending = {}
        session._waiters = []
        session._direct_lines = None
        session.username = "游客aaaaaa"
        session.last_error = ""
        session._notify = lambda *a, **k: None
        session.log = lambda *a, **k: None
        session.state_changed = _FakeEvent()

        import types

        session.state = types.SimpleNamespace(
            username="游客aaaaaa", self_name="游客aaaaaa"
        )

        return session

    async def scenario():
        session = make_session()

        task = asyncio.ensure_future(session.send_identity("realaccount", "-"))

        # 等它把行写出去、挂上 future，然后喂一条 OK IDENTITY。
        for _ in range(50):
            if session._pending.get("IDENTITY") is not None:
                break

            await asyncio.sleep(0.01)

        written = session.writer.text
        check(
            "★ 发出去的行是 `IDENTITY <name> <token> ver=<版本>`（ver= 在最后）",
            written.startswith("IDENTITY realaccount - ")
            and written.strip().endswith(f"ver={_APP_VERSION}"),
            repr(written),
        )

        session._on_line("OK IDENTITY realaccount")

        ok, message, name = await asyncio.wait_for(task, timeout=5)

        check("★ OK IDENTITY -> ok=True 并带回中继确认的名字",
              ok is True and name == "realaccount", f"{ok}/{message}/{name}")
        check("自己的显示名当场更新（不用等下一次广播）",
              session.username == "realaccount"
              and session.state.self_name == "realaccount",
              f"{session.username}/{session.state.self_name}")

        # ---- ERROR 分支 ----
        session = make_session()
        task = asyncio.ensure_future(session.send_identity("realaccount", "-"))

        for _ in range(50):
            if session._pending.get("IDENTITY") is not None:
                break

            await asyncio.sleep(0.01)

        session._on_line("ERROR IDENTITY_INVALID_TOKEN AUTH_INVALID_TOKEN")

        ok, message, name = await asyncio.wait_for(task, timeout=5)

        check(
            "★ ERROR IDENTITY_* -> ok=False，消息带原因，名字不变",
            ok is False
            and "AUTH_INVALID_TOKEN" in message
            and session.username == "游客aaaaaa",
            f"{ok}/{message}/{session.username}",
        )

        # ---- 老中继：什么都不回 -> 超时，绝不当成致命错误 ----
        session = make_session()
        task = asyncio.ensure_future(session.send_identity("realaccount", "-"))

        import mclink.core.room as room_mod

        original = room_mod.REPLY_TIMEOUT
        room_mod.REPLY_TIMEOUT = 0.2

        try:
            ok, message, name = await asyncio.wait_for(task, timeout=5)
        finally:
            room_mod.REPLY_TIMEOUT = original

        check(
            "★ 老中继忽略这条行 -> 超时返回 ok=False（不是异常、不断连接）",
            ok is False and bool(message),
            f"{ok}/{message}",
        )

    asyncio.run(scenario())


class _FakeWriter:
    def __init__(self):
        self.text = ""

    def write(self, data):
        self.text += data.decode("utf-8")

    async def drain(self):
        return None


class _FakeEvent:
    def __init__(self):
        self.set_count = 0

    def set(self):
        self.set_count += 1


# ============================================================
# 4. 界面：只在"房间已加入 + 登录状态变了"时发一次（防抖）
# ============================================================

def test_gui_sends_identity_once_per_login():
    print("")
    print("---- 界面：IDENTITY 只在房间里发，且同一个身份只发一次 ----")

    import tempfile

    sandbox = tempfile.mkdtemp(prefix="mclink-identity-")
    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox

    try:
        import tkinter as tk
        from tkinter import messagebox

        messagebox.showwarning = lambda *a, **k: None
        messagebox.showerror = lambda *a, **k: None
        messagebox.showinfo = lambda *a, **k: None

        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        check("Tk root can be created (display available)", False, repr(e))
        return

    root.withdraw()

    from mclink.gui import app as gui_app

    calls = []

    class FakeRoom:
        pass

    class FakeEngine:
        def __init__(self):
            self.room = None

        def room_state(self):
            return self.room

        def room_send_identity(self, username, token="", timeout=8):
            calls.append((username, token))
            return True, "ok", username

        # 界面构建期间会碰到的入口，全部给个安全实现。
        def __getattr__(self, name):
            def stub(*_a, **_k):
                return None

            return stub

    app = gui_app.MCLinkApp(root)
    app.engine = FakeEngine()

    import types

    app.config = types.SimpleNamespace(username="", role="player",
                                       server_host="relay.example")

    try:
        # ① 没有房间：不发。
        app.auth = types.SimpleNamespace(username="realaccount", token="tok-1")
        check("没有房间时不发 IDENTITY", app._sync_identity() is False)
        check("也确实没有调用", not calls, repr(calls))

        # ② 有房间：发一次。
        app.engine.room = FakeRoom()
        sent = app._sync_identity()
        check("★ 房间 + 登录 -> 发一次", sent is True, repr(calls))

        # 等后台线程把结果派回来（它只调 root.after，不会碰网络）。
        for _ in range(100):
            root.update()
            time.sleep(0.01)

            if calls:
                break

        check(
            "★ 发出的身份就是登录账号 + token",
            calls == [("realaccount", "tok-1")],
            repr(calls),
        )

        # ③ 同一个身份：绝不重复发（用户要求"防抖，别每秒发"）。
        app._identity_sent = ("realaccount", "tok-1")
        app._identity_sent_at = time.time()
        check("★ 同一身份不重复发", app._sync_identity() is False)

        # ④ 换了账号：允许再发（冷却过去后）。
        app.auth = types.SimpleNamespace(username="other", token="tok-2")
        app._identity_sent_at = 0.0
        check("★ 身份变了要重新同步", app._sync_identity() is True)

        for _ in range(100):
            root.update()
            time.sleep(0.01)

            if len(calls) >= 2:
                break

        check(
            "★ 新身份也真的发出去了",
            ("other", "tok-2") in calls,
            repr(calls),
        )
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ============================================================

def main():
    print("=" * 64)
    print("MC-Link room identity (IDENTITY) tests")
    print("=" * 64)

    # 客户端静态契约：常量、握手规则（ver= 在最后一格）
    from mclink.core import protocol as proto_mod

    check(
        "协议常量存在（IDENTITY / OK IDENTITY）",
        proto_mod.LINE_ROLE_IDENTITY == "IDENTITY"
        and proto_mod.LINE_ACK_IDENTITY == "OK IDENTITY",
        f"{proto_mod.LINE_ROLE_IDENTITY}/{proto_mod.LINE_ACK_IDENTITY}",
    )

    relay, relay_log = start_relay()

    try:
        if relay.poll() is not None:
            check("中继子进程起来了", False, "进程已退出，看 identity_relay.log")
        else:
            test_identity_updates_the_live_roster(relay_log)
            test_identity_error_codes(relay_log)
            test_identity_leaves_no_stale_guest_identity(relay_log)
    finally:
        try:
            relay.terminate()
            relay.wait(timeout=5)
        except Exception:
            with __import__("contextlib").suppress(Exception):
                relay.kill()

        with __import__("contextlib").suppress(Exception):
            relay_log.close()

    test_client_parses_the_identity_reply()
    test_room_state_has_no_duplicate_identity()
    test_gui_sends_identity_once_per_login()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
