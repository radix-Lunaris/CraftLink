"""
CraftLink -- 成员身份（登录名 / 游客名）+ 每成员到中继的 RTT + 版本门开发开关。

这一套覆盖 2026-09-26 的三项交付（第 4 项「重启服务」已被用户取消，不在
本套件里）：

  1. 房间里显示的成员名必须是**登录用户名**；未登录/免登录的连接由中继在
     握手时分配 `游客` + 6 位 [0-9a-z] 的临时名（只活在这条连接上）。
     同时修掉"没有 username / client_id 就出错"的既有 bug：空值一律兜底，
     绝不出现空 key、绝不抛异常。

  2. 中继**主动**发 `PING <token>`，成员回 `PONG <token>`，中继量每个成员
     到自己的 RTT，并放进 HOST_STATE 广播的 `rtts=`（整数毫秒）。没有数据
     的成员整条不出现 —— 绝不编数字。

  3. 面板的「允许不同版本客户端连接」（**开发调试**，默认关闭）：默认行为
     与以前逐字一致（不一致 → ERROR VERSION_MISMATCH）；打开后放行；关掉
     又拒绝。中继**运行时**读这个设置，不需要重启。

真中继子进程 + 真 socket。RTT 那一项用一个"会回 PONG 的假成员"和一个
"故意不回 PONG 的假成员"对照，证明有数据才广播。

Run:
    python tests/member_identity_rtt_test.py
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

RELAY_PORT = int(os.environ.get("MCLINK_TEST_IDENT_PORT", "19190"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_IDENT_ADMIN_PORT", "19191"))

GATE_PORT = int(os.environ.get("MCLINK_TEST_IDENT_GATE_PORT", "19192"))
GATE_ADMIN_PORT = int(os.environ.get("MCLINK_TEST_IDENT_GATE_ADMIN_PORT", "19193"))

ADMIN_TOKEN = "ident-rtt-test-token"
SERVER_VERSION = "9.9.9"

GUEST_RE = re.compile(r"^游客[0-9a-z]{6}$")

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def wait_until(predicate, timeout=20, interval=0.25, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    print(f"  !! timeout waiting for {label}", flush=True)
    return False


# ============================================================
# 中继子进程
# ============================================================

def start_relay(port, admin_port, log_name, version=None):
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

    if version is None:
        env.pop("MCLINK_VERSION", None)
        env["MCLINK_VERSION"] = ""
    else:
        env["MCLINK_VERSION"] = version

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

    return proc, log, log_path


def stop_relay(proc, log):
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
    with contextlib.suppress(Exception):
        log.flush()
        log.close()


def read_log(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ============================================================
# 假成员：会（或不会）回 PONG 的成员控制连接
# ============================================================

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


def parse_rtts_token(value):
    out = {}
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        cid, sep, raw = item.partition(":")
        if not sep or not cid:
            continue
        if not raw.isdigit():
            out[cid] = raw  # 交给断言去发现"不是整数"
            continue
        out[cid] = int(raw)
    return out


def token_of(line, key):
    for tok in line.split()[1:]:
        name, _, value = tok.partition("=")
        if name == key:
            return value
    return ""


class FakeMember:
    """一条成员控制连接：读推送、按需回 PONG、解析 HOST_STATE。"""

    def __init__(self, reader, writer, answer_ping=True):
        self.reader = reader
        self.writer = writer
        self.answer_ping = answer_ping
        self.raw_states = []
        self.names = {}
        self.rtts = {}
        self.peers = []
        self.state = {}
        self.you = None
        self.ok_reply = ""
        self.join_time = 0.0
        self._task = None

    async def join(self, line, timeout=8):
        self.writer.write((line + "\n").encode())
        await self.writer.drain()
        raw = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        self.ok_reply = raw.decode("utf-8", errors="replace").strip()
        self.you = unquote(token_of(self.ok_reply, "you"))
        self.join_time = time.time()
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
                upper = text.upper()
                if upper.startswith("PING"):
                    if self.answer_ping:
                        token = text[4:].strip()
                        self.writer.write((f"PONG {token}\n").encode())
                        await self.writer.drain()
                    continue
                if upper.startswith("HOST_STATE"):
                    self._apply_state(text)
                    continue
        except Exception:
            pass

    def _apply_state(self, text):
        self.raw_states.append(text)
        state = {}
        for tok in text.split()[1:]:
            key, _, value = tok.partition("=")
            state[key] = value
        self.state = state
        self.names = parse_names_token(state.get("names", ""))
        self.rtts = parse_rtts_token(state.get("rtts", ""))
        peers = state.get("peers", "-")
        self.peers = [] if peers in ("", "-") else peers.split(",")

    async def wait_for(self, predicate, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(self):
                return True
            await asyncio.sleep(0.1)
        return False

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()


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
# 管理 API（只用来读 /rooms、读改 /settings）
# ============================================================

def admin_request(port, method, path, payload=None, timeout=8):
    url = f"http://127.0.0.1:{port}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")

    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    if data is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

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
# 1. 身份：登录名 / 游客名 / 空值健壮
# ============================================================

async def identity_tests(port):
    print("")
    print("---- 1. 成员显示名（登录名 / 游客xxxxxx）----")

    room_token = "ident-rtt-room"

    # ---- A：完全没有 client_id / username / token 的 CREATE ----
    anon_a, reply_a = await open_member(
        port, f"CREATE __mclink__ - - - {room_token}"
    )

    check(
        "无 token / 无 client_id / 无用户名的连接能进房",
        reply_a.startswith("OK JOIN"),
        reply_a,
    )
    check(
        "OK 回包带 you=，且是分配的「游客xxxxxx」",
        GUEST_RE.match(anon_a.you or "") is not None,
        f"you={anon_a.you!r}",
    )

    # ---- B：登录用户（带中文名，验证 percent-encoding）----
    anon_b, reply_b = await open_member(
        port, f"JOIN __mclink__ cid-b 测试用户 tok-b {room_token}"
    )

    check(
        "登录用户进房，you= 就是登录名",
        reply_b.startswith("OK JOIN") and anon_b.you == "测试用户",
        f"you={anon_b.you!r} reply={reply_b!r}",
    )
    check(
        "OK 回包里的 you= 是 percent-encoding 的（中文名不会带出非 ASCII）",
        "%E6%B5%8B%E8%AF%95%E7%94%A8%E6%88%B7" in reply_b,
        reply_b,
    )

    # ---- C：只有一个用户名、没有 client_id 的成员 ----
    anon_c, reply_c = await open_member(
        port, f"JOIN __mclink__ - 乘客01 tok-c {room_token}"
    )

    check(
        "只有用户名、没有 client_id 也能进房且名字保留",
        reply_c.startswith("OK JOIN") and anon_c.you == "乘客01",
        f"you={anon_c.you!r} reply={reply_c!r}",
    )

    # ---- D：第二个完全匿名的连接（验证两个游客名必须不同）----
    anon_d, reply_d = await open_member(
        port, f"JOIN __mclink__ - - - {room_token}"
    )

    check(
        "第二个匿名连接也能进房且拿到游客名",
        reply_d.startswith("OK JOIN") and GUEST_RE.match(anon_d.you or "") is not None,
        f"you={anon_d.you!r}",
    )
    check(
        "两个匿名连接拿到两个不同的游客名",
        anon_a.you != anon_d.you,
        f"{anon_a.you!r} vs {anon_d.you!r}",
    )

    # ---- 四个人都看得到彼此的名字 ----
    complete = await anon_a.wait_for(lambda m: len(m.names) >= 4, timeout=12)

    check("匿名成员收到了 4 个人的 names= 广播", complete, str(anon_a.names))

    guest_names = [n for n in anon_a.names.values() if GUEST_RE.match(n or "")]

    check(
        "names= 里有两个不同的游客名（每个人都有自己的名字）",
        len(set(guest_names)) >= 2,
        str(anon_a.names),
    )
    check(
        "names= 里有登录名（登录用户显示登录名）",
        "测试用户" in anon_a.names.values() and "乘客01" in anon_a.names.values(),
        str(anon_a.names),
    )
    check(
        "每个成员都能看到其他成员的 client_id（peers= 没被破坏）",
        set(anon_a.peers) >= {"cid-b"},
        f"peers={anon_a.peers}",
    )

    # ---- 中继快照（面板用的 member_list）----
    room = find_room(ADMIN_PORT, room_token)
    check("管理 API 能看到这个房间", room is not None)

    if room:
        members = room.get("member_list") or []
        check("member_list 有 4 个人", len(members) == 4, str(len(members)))

        empty_display = [
            m.get("client_id") for m in members if not (m.get("display_name") or "")
        ]
        empty_user = [
            m.get("client_id") for m in members if not (m.get("username") or "")
        ]
        empty_key = [
            m.get("client_id") for m in members if not (m.get("member_key") or "")
        ]
        empty_cid = [m.get("client_id") for m in members if not (m.get("client_id") or "")]

        check(
            "member_list 里每个人都有 display_name（没有空值）",
            not empty_display,
            str(empty_display),
        )
        check("member_list 里 username 也没有空值", not empty_user, str(empty_user))
        check("member_list 里 member_key 也不是空 key", not empty_key, str(empty_key))
        check(
            "member_list 里 client_id 也不是空（匿名连接被分配了唯一值）",
            not empty_cid,
            str(empty_cid),
        )
        check(
            "member_list 里带 rtt_ms 字段（没测到就是 None，不是编的数字）",
            all("rtt_ms" in m for m in members),
            str([m.get("rtt_ms") for m in members]),
        )

        by_name = {m.get("display_name"): m for m in members}
        check(
            "匿名成员的 display_name == OK 回包给的 you=",
            by_name.get(anon_a.you) is not None
            and by_name[anon_a.you].get("username") == anon_a.you,
            f"{anon_a.you!r} in {list(by_name)}",
        )
        check(
            "匿名成员确实被分配了 anon- 开头的 client_id（不是空 key）",
            any(
                (m.get("client_id") or "").startswith("anon-")
                and m.get("display_name") == anon_a.you
                for m in members
            ),
            str([m.get("client_id") for m in members]),
        )

    for member in (anon_a, anon_b, anon_c, anon_d):
        await member.close()

    # ---- 老式 HOST 连接：没有任何身份字段也不许炸 ----
    host_reply = await send_line(port, f"HOST {room_token}-legacy")

    check(
        "老式 HOST 无 client_id / username 也能建房（不报错、不崩）",
        host_reply.startswith("OK HOST"),
        host_reply,
    )

    return room_token


# ============================================================
# 2. 每成员到中继的 RTT（中继主动 PING，成员回 PONG）
# ============================================================

async def rtt_tests(port, room_token):
    print("")
    print("---- 2. 每成员到中继的 RTT（rtts=）----")

    # 两个会回 PONG 的成员：中继必须量到它们。
    anon_a, reply_a = await open_member(
        port, f"CREATE __mclink__ rtt-a 甲 tok rtt-room", answer_ping=True
    )
    anon_b, reply_b = await open_member(
        port, f"JOIN __mclink__ rtt-b 乙 tok rtt-room", answer_ping=True
    )

    check(
        "RTT 对照组两个成员进房",
        reply_a.startswith("OK JOIN") and reply_b.startswith("OK JOIN"),
        f"{reply_a!r} / {reply_b!r}",
    )

    measured = await anon_a.wait_for(
        lambda m: "rtt-a" in m.rtts and "rtt-b" in m.rtts, timeout=15
    )

    check(
        "中继量到了两个成员到自己的 RTT（rtts= 里都有）",
        measured,
        str(anon_a.rtts),
    )
    check(
        "RTT 是整数毫秒（不是浮点、不是编的字符串）",
        all(isinstance(v, int) for v in anon_a.rtts.values()),
        str(anon_a.rtts),
    )
    check(
        "RTT 数值在合理范围（0 <= ms <= 5000）",
        all(isinstance(v, int) and 0 <= v <= 5000 for v in anon_a.rtts.values()),
        str(anon_a.rtts),
    )
    check(
        "自己也在 rtts= 里（每个成员都看得到彼此，也包括自己）",
        "rtt-a" in anon_a.rtts,
        str(anon_a.rtts),
    )

    # 第三个成员：**故意不回 PONG**。中继不许给它编一个数字。
    anon_c, reply_c = await open_member(
        port, f"JOIN __mclink__ rtt-c 丙 tok rtt-room", answer_ping=False
    )

    check("不回 PONG 的成员也能正常进房（探测不影响连接）", reply_c.startswith("OK JOIN"), reply_c)

    # C 进房会立刻触发一次广播：那时 A/B 已经有数字，C 必须整条不出现。
    joined = await anon_a.wait_for(lambda m: "rtt-c" in m.names, timeout=12)
    check("A 收到了 C 进房的广播", joined, str(anon_a.names))
    check(
        "没有 PONG 数据的成员在 rtts= 里整条不出现（绝不编数字）",
        "rtt-c" not in anon_a.rtts and "rtt-a" in anon_a.rtts,
        str(anon_a.rtts),
    )

    # 等过一整个探测超时窗口，扫过期间收到的**所有**广播：C 一次都不许出现。
    marker = len(anon_a.raw_states)
    await asyncio.sleep(5.5)

    leaked = []

    for line in anon_a.raw_states[marker:]:
        if "rtt-c" in parse_rtts_token(token_of(line, "rtts")):
            leaked.append(line)

    check(
        "等过一个探测超时窗口后，C 仍然没有任何 RTT 数字（超时不写数）",
        not leaked,
        "; ".join(leaked[:2]),
    )

    for member in (anon_a, anon_b, anon_c):
        await member.close()


# ============================================================
# 3. 版本门开发开关（默认关 / 开 / 再关）
# ============================================================

async def version_switch_tests(proc, log, log_path):
    print("")
    print("---- 3. 「允许不同版本客户端连接」开发开关 ----")

    status, payload = admin_request(GATE_ADMIN_PORT, "GET", "/settings")
    data = (payload or {}).get("data") or {}
    entry = data.get("allow_version_mismatch") or {}

    check("管理 API 上报了 allow_version_mismatch 设置项", bool(entry), str(list(data)))
    check(
        "默认是关闭（value = false）",
        entry.get("value") is False,
        repr(entry.get("value")),
    )
    check(
        "设置项文案写明是开发调试 / 可能未知 BUG / 不是兼容老版本",
        "开发调试" in (entry.get("help") or "")
        and "未知 BUG" in (entry.get("help") or "")
        and "兼容" in (entry.get("help") or ""),
        (entry.get("help") or "")[:60],
    )

    # 1) 默认关闭：版本不一致 -> 拒
    reply = await send_line(GATE_PORT, "CREATE __mclink__ cid-x - - gate-room ver=1.0.0")
    check(
        "默认关闭：版本不一致被拒（ERROR VERSION_MISMATCH）",
        reply.startswith("ERROR VERSION_MISMATCH")
        and f"server={SERVER_VERSION}" in reply
        and "client=1.0.0" in reply,
        reply,
    )

    # 2) 打开开关（运行时改，不重启中继）
    status, payload = admin_request(
        GATE_ADMIN_PORT, "PUT", "/settings", {"allow_version_mismatch": True}
    )
    check(
        "打开开关：管理 API 返回成功且值已变 true",
        status == 200
        and payload.get("ok")
        and ((payload.get("data") or {}).get("allow_version_mismatch") or {}).get("value")
        is True,
        f"{status} {payload}",
    )

    reply = await send_line(GATE_PORT, "CREATE __mclink__ cid-x - - gate-room ver=1.0.0")
    check(
        "开关打开后：版本不一致的连接被放行（CREATE 成功）",
        reply.startswith("OK JOIN"),
        reply,
    )

    log.flush()
    text = read_log(log_path)
    check(
        "放行会在中继日志里留一条 [VERSION BYPASS]（非正常状态可见）",
        "[VERSION BYPASS]" in text,
        text[-300:] if "[VERSION BYPASS]" not in text else "",
    )

    # 3) 再关掉：立刻恢复拒绝
    status, payload = admin_request(
        GATE_ADMIN_PORT, "PUT", "/settings", {"allow_version_mismatch": False}
    )
    check(
        "再关掉：管理 API 返回成功且值已变回 false",
        status == 200
        and payload.get("ok")
        and ((payload.get("data") or {}).get("allow_version_mismatch") or {}).get("value")
        is False,
        f"{status} {payload}",
    )

    reply = await send_line(GATE_PORT, "CREATE __mclink__ cid-x - - gate-room ver=1.0.0")
    check(
        "再关掉之后：版本不一致又被拒（运行时读取，无需重启）",
        reply.startswith("ERROR VERSION_MISMATCH"),
        reply,
    )

    # 同版本始终放行（开关关着也一样）—— 门本身没被这次改动弄坏
    reply = await send_line(
        GATE_PORT,
        f"CREATE __mclink__ cid-y 同版 tok gate-room2 ver={SERVER_VERSION}",
    )
    check("开关关着时同版本照常放行（门本身没坏）", reply.startswith("OK JOIN"), reply)


# ============================================================
# 4. 静态契约（服务端 / 面板 / 冻结格式）
# ============================================================

def static_tests():
    print("")
    print("---- 4. 静态契约 ----")

    def read(relative):
        try:
            with open(os.path.join(ROOT, relative), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    settings_src = read("server/settings_service.py")
    relay_src = read("server/relay_server.py")
    members_src = read("server/room_members.py")
    names_src = read("server/room_names.py")
    dashboard_src = read("panel/web/views/dashboard.php")
    i18n_src = read("panel/lib/i18n.php")
    client_room_src = read("client/mclink/core/room.py")

    check(
        "设置项注册在 settings_service（名字 = allow_version_mismatch，默认 False）",
        'SETTING_ALLOW_VERSION_MISMATCH = "allow_version_mismatch"' in settings_src
        and '"default": False' in settings_src
        and "SETTING_ALLOW_VERSION_MISMATCH: {" in settings_src,
    )
    check(
        "设置项注释明确写了「不是兼容老版本的路径」",
        "不是" in settings_src
        and "兼容老版本" in settings_src
        and "版本必须一致" in settings_src,
    )
    check(
        "中继版本门真的读这个开关（allow_version_mismatch()）",
        "def allow_version_mismatch()" in relay_src
        and "if allow_version_mismatch():" in relay_src
        and "[VERSION BYPASS]" in relay_src,
    )
    check(
        "中继运行时取值（不缓存、不重启）：allow_version_mismatch 走 SettingsService",
        'service.get_bool(SETTING_ALLOW_VERSION_MISMATCH, False)' in relay_src,
    )
    check(
        "HOST_STATE 广播带 names= / rtts=（冻结格式）",
        'f"names={names_token} "' in members_src
        and 'f"rtts={rtts_token} "' in members_src,
    )
    check(
        "中继主动发 PING 探测并匹配 PONG（冻结格式）",
        "async def member_rtt_loop" in members_src
        and "RTT_TOKEN_PREFIX" in members_src
        and "def _handle_rtt_pong" in members_src,
    )
    check(
        "显示名 percent-encoding 用共享 helper（room_names.encode_field）",
        "def encode_field(" in names_src
        and "room_names.encode_field(name)" in members_src
        and "room_names.encode_field(member.display_name)" in relay_src,
    )
    check(
        "游客名是「游客」+ 6 位 [0-9a-z]",
        'GUEST_NAME_PREFIX = "游客"' in relay_src
        and 'GUEST_NAME_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"'
        in relay_src
        and "GUEST_NAME_LENGTH = 6" in relay_src,
    )
    check(
        "OK JOIN / OK CREATE 回包带 you=（percent-encoding）",
        "you={room_names.encode_field(member.display_name)}" in relay_src,
    )

    # ---- 面板：开关按钮 + 仅管理员 ----
    check(
        "面板仪表盘有 allow_version_mismatch 开关（复用 setting_save）",
        'name" value="allow_version_mismatch"' in dashboard_src
        and 'action="index.php?action=setting_save"' in dashboard_src,
    )
    check(
        "面板开关只给管理员渲染（is_admin() 守着按钮）",
        "$version_bypass !== null" in dashboard_src
        and "is_admin()" in dashboard_src
        and "只有管理员可以修改" in dashboard_src,
    )
    check(
        "开关的中英文案都在翻译表里（切 English 不会冒中文）",
        "'开启（允许不同版本）'" in i18n_src
        and "'关闭（要求版本一致）'" in i18n_src
        and "'这是开发调试开关，不是给玩家用的；只应由管理员临时打开、测完立刻关掉。'"
        in i18n_src,
    )

    # ---- 客户端（只读校验）：冻结格式两边一致 ----
    check(
        "客户端解析 names= / rtts= / you=，并且收到 PING 会回 PONG（只读校验）",
        'key == "names"' in client_room_src
        and 'key == "rtts"' in client_room_src
        and 'key == "you"' in client_room_src
        and 'if upper.startswith("PING")' in client_room_src
        and 'f"PONG {token}\\n"' in client_room_src,
    )


# ============================================================
# main
# ============================================================

async def runtime_tests():
    proc, log, log_path = start_relay(RELAY_PORT, ADMIN_PORT, "identity_rtt_relay.log")

    listening = await wait_until(
        lambda: port_open(RELAY_PORT), timeout=25, label="relay to listen"
    )
    check("中继起来了（身份 / RTT 用）", listening)

    if listening:
        try:
            room_token = await identity_tests(RELAY_PORT)
            await rtt_tests(RELAY_PORT, room_token)
        finally:
            stop_relay(proc, log)
    else:
        stop_relay(proc, log)

    await wait_until(
        lambda: not port_open(RELAY_PORT), timeout=10, label="relay to stop"
    )

    # 版本门开发开关：单独一台中继，门开着（server=9.9.9）
    proc, log, log_path = start_relay(
        GATE_PORT, GATE_ADMIN_PORT, "identity_rtt_gate_relay.log",
        version=SERVER_VERSION,
    )

    listening = await wait_until(
        lambda: port_open(GATE_PORT), timeout=25, label="version-gate relay to listen"
    )
    check("中继起来了（版本门开关用，server=9.9.9）", listening)

    if listening:
        try:
            await version_switch_tests(proc, log, log_path)
        finally:
            stop_relay(proc, log)
    else:
        stop_relay(proc, log)


def main():
    print("=" * 64)
    print("CraftLink（方块联机）成员身份 / RTT / 版本门开发开关")
    print("=" * 64)

    static_tests()

    asyncio.run(runtime_tests())

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
