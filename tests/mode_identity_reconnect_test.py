"""
CraftLink —— 房间模式 / 房间身份 / 重连判定（真中继 + 真握手）

用户 2026-09-29 的三条要求，一条一套断言：

  **① 「模式被自动跳到中转」的 P0**
  「STUN(直连) → relay → 切回 STUN 打通后，直连玩一会儿游戏断开，并且
    没有任何人操作，模式自己跳回了 relay，连接也断了。」
  规则：**房间模式只能由 Owner 的显式 `CONNMODE` 改变**；任何内部路径
  （清理 / 重试 / 看门狗 / 兜底默认值 / 迟到握手）都不得改写它。唯一的例外
  是"房间销毁 -> 由**同一个 Owner** 重建"，这时带 `mode_reason=restored`
  广播原因；换了人建房就是全新房间（`mode_reason=default`）。
  根因（file:line）：
    * `relay_server.handle_player` / `handle_host_tunnel` 以前用
      `get_or_create_room`，一个**探测性/迟到的** PLAYER / HOST_TUNNEL 握手
      就会把一个新的默认 `relay` 房间塞进注册表；
    * `broadcast_member_state` 用 `getattr(room, "conn_mode", "relay")` 兜底，
      没有字段时凭空报 "relay"。
  修法：只读查找（`resolve_room`）+ 唯一写入点（`set_room_conn_mode`）+
  读口径（`room_conn_mode`，不再凭空造 relay）+ Owner 模式记忆。

  **② 重连时的 token 有效性 / host 漂移 / 热切换语义（ADDITIVE）**
  * `SESSION __mclink__ <cid> <user> <token>`（PROTOCOL §8.10）：只读探测，
    明确告诉客户端「闪断（token 有效、身份还在）→ 自动重连」还是
    「被清退 / token 失效 / 房间没了 → 重新登录 / 重新加入」。
  * host 漂移：A 主动释放 -> 主机位释放 + 广播；B 认领 -> 广播新 `host_id=`；
    B 开服后玩家真的在**新 host_id** 上完成配对（HOST_TUNNEL -> START ->
    READY -> PLAYER CONNECTED）。`host_id=` / `started=` / `conn=` 三者
    在每一步都自洽；**不放宽** §B 门禁。
  * 热切换：换模式那一次广播必须带上**切换前**的真实连接状态
    （`was_live=0|1`，PROTOCOL §8.5 / §8.9），客户端才能决定"原本空闲 ->
    不自动重连 / 原本有连接 -> 自动重连"。

  **③ 连接 != 身份/职位（用户 2026-09-29 反复强调，AGENTS.md 规则 6 补充）**
  * Host 停服 / 开服控制连接断掉（token 仍有效）→ **主机位保留**：
    `host=1` + `host_name=<他>`、`conn=idle`（UI：`xxx（未开服）`）；
    别人 `CLAIM_HOST` 必须被拒（`ERROR HOST_TAKEN`）；新 PLAYER 收到
    `ERROR HOST_NOT_READY`（**不是** `NO_HOST`）。
  * 只有两种情况释放主机位：① 离开房间（主动 LEAVE / 被踢 / 超时清退 /
    token 失效）；② 主动 `RELEASE_HOST`。
  * 成员控制连接**断了**、但身份 token 仍有效 → 座位宽限期：身份/职位保留，
    只断游戏连接，本人宽限期内重连就**拿回座位**（房间不销毁、模式不变）；
    宽限期满才按"真正离开"处理。
  * Owner / Host / Player 只是**房间角色**：Owner 兼职 Host、Owner 只当
    Player、非 Owner 当 Host 三种组合都要能正常工作；房间参数创建后冻结
    （非 Owner 改 `CONNMODE` / `MAXPLAYERS` 一律被拒）。

全部 ADDITIVE：老客户端不认识 `mode_reason=` / `was_live=` / `SESSION`，
按空格分词忽略 / 收到 `ERROR UNKNOWN_ROLE` 即可。

Run:
    python tests/mode_identity_reconnect_test.py
"""

import asyncio
import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_MODE_PORT", "19710"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_MODE_ADMIN_PORT", "19711"))
ADMIN_TOKEN = "mode-identity-test-admin-token"
SENTINEL = "__mclink__"

#: 座位宽限期（秒）。压到几秒，测试不必真等 45 秒。
SEAT_GRACE = 4

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


# ============================================================
# 解析（和客户端要照做的解析一模一样）
# ============================================================

def token_of(line, key):
    for token in str(line or "").split()[1:]:
        name, _, value = token.partition("=")

        if name == key:
            return value

    return ""


def parse_started(text):
    value = token_of(text, "started")

    return [item for item in value.split(",") if item and item != "-"]


def parse_conn(text):
    value = token_of(text, "conn")

    if not value or value == "-":
        return {}

    table = {}

    for item in value.split(","):
        cid, _, state = item.partition(":")

        if cid:
            table[cid] = state

    return table


def is_host_state(text):
    return str(text or "").startswith("HOST_STATE")


# ============================================================
# 真中继子进程
# ============================================================

def start_relay(log_name="mode_identity_relay.log", extra_env=None):
    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(RELAY_PORT),
            "MCLINK_DB_ENABLED": "0",
            "MCLINK_REQUIRE_ACCOUNT": "0",
            "MCLINK_MAX_PLAYERS": "8",
            "MCLINK_ADMIN_API_ENABLED": "1",
            "MCLINK_ADMIN_API_HOST": "127.0.0.1",
            "MCLINK_ADMIN_API_PORT": str(ADMIN_PORT),
            "MCLINK_ADMIN_API_TOKEN": ADMIN_TOKEN,
            "MCLINK_STATUS_FILE": os.path.join(
                HERE, log_name.replace(".log", "_status.json")
            ),
            "MCLINK_STATUS_INTERVAL": "5",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            "MCLINK_MEMBER_SEAT_GRACE": str(SEAT_GRACE),
            # 成员心跳放长：这一套量的是"身份/职位"，不许被超时清理干扰。
            "MCLINK_MEMBER_IDLE_TIMEOUT": "30",
            "MCLINK_MEMBER_WATCHDOG_INTERVAL": "2",
            "MCLINK_HOST_IDLE_TIMEOUT": "30",
            "MCLINK_HOST_WATCHDOG_INTERVAL": "2",
            "MCLINK_VERBOSE": "1",
        }
    )
    env["MCLINK_VERSION"] = ""

    for key, value in (extra_env or {}).items():
        env[key] = str(value)

    log_path = os.path.join(HERE, log_name)
    log = io.open(log_path, "w", encoding="utf-8", errors="replace")

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
            socket.create_connection(("127.0.0.1", RELAY_PORT), 1).close()
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
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ============================================================
# 管理快照
# ============================================================

def admin_rooms():
    request = urllib.request.Request(
        f"http://127.0.0.1:{ADMIN_PORT}/rooms",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )

    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.load(response)
    except Exception:
        return []

    if isinstance(payload, dict):
        payload = payload.get("data", payload)

    return payload if isinstance(payload, list) else []


def find_room(room_token):
    for room in admin_rooms():
        if room.get("room") == room_token:
            return room

    return None


# ============================================================
# 成员控制连接：读推送、回 PING、按时刻记录每一次 HOST_STATE
# ============================================================

class Member:
    def __init__(self, reader, writer, answer_ping=True):
        self.reader = reader
        self.writer = writer
        self.answer_ping = answer_ping
        self.lines = []
        self.states = []
        self._mark = 0
        self._line_mark = 0
        self._task = None

    def mark(self):
        """只有这之后到达的 HOST_STATE / 控制行才算数（防"匹配到旧状态"）。"""
        self._mark = len(self.states)
        self._line_mark = len(self.lines)

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

                if text.upper().startswith("PING"):
                    if self.answer_ping:
                        self.writer.write(
                            (f"PONG {text[4:].strip()}\n").encode()
                        )
                        await self.writer.drain()
                    continue

                if is_host_state(text):
                    self.states.append((time.monotonic(), text))
        except Exception:
            pass

    async def send(self, line):
        self.writer.write((line + "\n").encode())
        await self.writer.drain()

    async def expect(self, prefix, timeout=8):
        deadline = time.time() + timeout

        while time.time() < deadline:
            for line in self.lines:
                if line.startswith(prefix):
                    return line

            await asyncio.sleep(0.05)

        return None

    def latest(self):
        return self.states[-1][1] if self.states else ""

    async def wait_state(self, predicate, timeout=10, label="HOST_STATE"):
        deadline = time.time() + timeout
        base = self._mark

        while time.time() < deadline:
            for _stamp, raw in reversed(self.states[base:]):
                with contextlib.suppress(Exception):
                    if predicate(raw):
                        return raw

            await asyncio.sleep(0.05)

        print(f"  !! timeout waiting for {label}: {[s for _, s in self.states[-3:]]}",
              flush=True)
        return None

    async def wait_line(self, prefix, timeout=8):
        deadline = time.time() + timeout

        while time.time() < deadline:
            for line in self.lines[self._line_mark:]:
                if line.startswith(prefix):
                    return line

            await asyncio.sleep(0.05)

        return None

    async def close(self, leave=False):
        if leave:
            with contextlib.suppress(Exception):
                await self.send("LEAVE")
                await asyncio.sleep(0.2)

        if self._task is not None:
            self._task.cancel()

            with contextlib.suppress(BaseException):
                await self._task

        with contextlib.suppress(Exception):
            self.writer.close()


async def open_member(line, answer_ping=True, timeout=8):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=timeout
    )
    member = Member(reader, writer, answer_ping=answer_ping)
    reply = await member.join(line, timeout=timeout)
    return member, reply


async def open_raw(line, read_reply=True, timeout=8):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    reply = ""

    if read_reply:
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        reply = raw.decode("utf-8", errors="replace").strip()

    return reader, writer, reply


async def read_line(reader, timeout=8):
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except asyncio.TimeoutError:
        return ""

    return raw.decode("utf-8", errors="replace").strip()


async def probe_session(client_id, username="-", token="-"):
    _reader, writer, reply = await open_raw(
        f"SESSION {SENTINEL} {client_id} {username} {token}"
    )
    writer.close()
    return reply


async def player_reply(room, client_id, username):
    """连一条 PLAYER，读第一行（可能是 CONNECTED，也可能是 ERROR）。"""
    reader, writer, reply = await open_raw(
        f"PLAYER {room} {client_id} {username}"
    )
    writer.close()
    return reply


async def tunnel_wait(room):
    """HOST_TUNNEL 握手：固定先回 WAIT（冻结协议），返回 (reader, writer, 后续行)。"""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), 8
    )
    writer.write((f"HOST_TUNNEL {room}\n").encode())
    await writer.drain()
    first = await read_line(reader)
    return reader, writer, first


async def pair_player(room, player_id="p-raw", player_name="P"):
    """
    一条**已经开好服**的 HOST_TUNNEL + 一条 PLAYER，走完 START/READY/CONNECTED。

    返回 (player_reader, player_writer, tunnel_reader, tunnel_writer, start_line,
    reply)。`reply` 必须是 CONNECTED —— 那是"玩家真的走通了新 Host 的通路"的
    唯一证据（不是"广播里写了 host_id"）。
    """
    tunnel_reader, tunnel_writer, wait = await tunnel_wait(room)

    if wait != "WAIT":
        return None, None, tunnel_reader, tunnel_writer, wait, ""

    player_reader, player_writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), 8
    )
    player_writer.write((f"PLAYER {room} {player_id} {player_name}\n").encode())
    await player_writer.drain()

    start = await read_line(tunnel_reader, timeout=8)

    if start.startswith("START"):
        tunnel_writer.write(b"READY\n")
        await tunnel_writer.drain()

    reply = await read_line(player_reader, timeout=8)
    return player_reader, player_writer, tunnel_reader, tunnel_writer, start, reply


def close_writers(*pairs):
    for writer in pairs:
        if writer is None:
            continue

        with contextlib.suppress(Exception):
            writer.close()


# ============================================================
# 1. P0：任何内部路径都不许改房间模式
# ============================================================

async def test_internal_paths_never_touch_mode(port, admin_port, log_path):
    print("")
    print("---- 1. 内部路径（迟到握手 / 清理 / 停服 / 重连）不许动 mode ----")

    room = "modekeep"

    owner, owner_reply = await open_member(
        f"CREATE {SENTINEL} cid-owner Owner - {room}"
    )
    room_token = token_of(owner_reply, "room")

    try:
        owner.mark()
        await owner.send("CONNMODE stun")
        await owner.expect("OK CONNMODE", 8)
        await owner.wait_state(lambda s: "mode=stun" in s, timeout=8)
        latest = owner.latest()

        check("Owner 显式切 stun -> mode=stun，来源标成 owner",
              "mode=stun" in latest and "mode_reason=owner" in latest,
              latest)

        # ---- 迟到 / 走错房间名的握手：必须只读，不许"造"出一个默认 relay 房 ----
        stray = f"{room}-ghost"
        reply = await player_reply(stray, "cid-ghost", "Ghost")

        check("迟到 PLAYER 握手（房间不存在）-> ERROR NO_HOST",
              reply.startswith("ERROR NO_HOST"), reply)

        _tr, tw, first = await tunnel_wait(stray)
        second = await read_line(_tr, timeout=8)
        close_writers(tw)
        check("迟到 HOST_TUNNEL 握手（房间不存在）-> WAIT 然后 ERROR NO_HOST",
              first == "WAIT" and second.startswith("ERROR NO_HOST"),
              f"{first!r} / {second!r}")

        check("★ 迟到握手没有在注册表里造出房间",
              find_room(stray) is None, str(find_room(stray)))

        # ---- Host 开服 -> 玩家配对 -> 玩家 PLAYER 掉线 ----
        await owner.send("CLAIM_HOST")
        await owner.expect("OK CLAIM_HOST", 8)
        _hr, hw, hreply = await open_raw(f"HOST {room_token} cid-owner Owner")
        check("Host 开服", hreply.startswith("OK HOST"), hreply)

        owner.mark()
        p_reader, p_writer, _tr, tw, start, connected = await pair_player(
            room_token, "p-cid", "P"
        )
        check("玩家在 Owner 的 Host 上配对成功（START -> READY -> CONNECTED）",
              start.startswith("START") and connected == "CONNECTED",
              f"{start!r} / {connected!r}")

        closed = await owner.wait_state(
            lambda s: parse_conn(s).get("cid-owner") == "connected"
            and "mode=stun" in s, timeout=8, label="host connected"
        )
        check("★ 配对之后广播仍是 mode=stun", closed is not None, owner.latest())

        # 玩家 PLAYER 掉线（会话清理路径）
        owner.mark()
        p_writer.close()
        await asyncio.sleep(1.0)
        after_drop = owner.latest()
        check("★ 玩家 PLAYER 掉线后广播仍是 mode=stun",
              "mode=stun" in after_drop, after_drop)

        # Host 停服（开服控制连接断掉）：模式不许变，主机位也不许丢
        owner.mark()
        hw.close()
        stopped = await owner.wait_state(
            lambda s: "mode=stun" in s
            and parse_conn(s).get("cid-owner") == "idle", timeout=10,
            label="host stopped, still stun+host",
        )
        check("★ Host 停服后广播仍是 mode=stun", stopped is not None,
              owner.latest())
        check("★ 停服只改连接状态： host=1 / host_name=Owner / started 不含他",
              stopped is not None
              and token_of(stopped, "host") == "1"
              and token_of(stopped, "host_name") == "Owner"
              and "cid-owner" not in parse_started(stopped),
              owner.latest())

        # Host 重开服（新连接）—— 重连/顶替路径
        owner.mark()
        _hr2, hw2, hreply2 = await open_raw(f"HOST {room_token} cid-owner Owner")
        check("Host 重新开服", hreply2.startswith("OK HOST"), hreply2)
        reopened = await owner.wait_state(
            lambda s: "mode=stun" in s and "cid-owner" in parse_started(s),
            timeout=8, label="host reopened",
        )
        check("★ 重开服（顶替旧开服连接）后仍是 mode=stun",
              reopened is not None, owner.latest())

        # 切换隧道清理：重复切 relay/stun 时旧的配对必须被清掉
        owner.mark()
        await owner.send("CONNMODE relay")
        await owner.expect("OK CONNMODE", 8)
        relay_state = await owner.wait_state(
            lambda s: "mode=relay" in s and "mode_reason=owner" in s,
            timeout=8, label="relay",
        )
        check("Owner 显式切 relay -> mode=relay（只有显式请求才变）",
              relay_state is not None, owner.latest())

        owner.mark()
        await owner.send("CONNMODE stun")
        await owner.expect("OK CONNMODE", 8)

        deadline = time.time() + 8

        while time.time() < deadline:
            tail = [
                token_of(x, "mode") for _t, x in owner.states[owner._mark:]
            ]

            if tail and tail[-1] == "stun":
                break

            await asyncio.sleep(0.1)

        tail = [token_of(x, "mode") for _t, x in owner.states[owner._mark:]]
        check("Owner 显式切回 stun -> mode=stun",
              bool(tail) and tail[-1] == "stun", owner.latest())

        # 全量扫描这一轮收到的每一条 HOST_STATE：切到 stun 之后只允许出现
        # **一次** relay（就是那次显式 CONNMODE），最后停在 stun。
        modes = [token_of(x, "mode") for _t, x in owner.states]
        after_stun = modes[modes.index("stun"):] if "stun" in modes else modes
        check("★ 切到 stun 之后只出现过 1 次 relay（就是那次显式 CONNMODE），"
              "最后停在 stun",
              after_stun.count("relay") == 1 and modes[-1] == "stun",
              f"modes={modes}")

        close_writers(hw2, p_writer, tw)
    finally:
        await owner.close()


# ============================================================
# 2. 房间销毁 / 重建：同一个 Owner 恢复模式，换了人就是默认
# ============================================================

async def test_mode_across_destroy_rebuild(port, admin_port):
    print("")
    print("---- 2. 房间销毁 -> 重建：同 Owner 恢复模式（带原因），换人回默认 ----")

    room = "moderebuild"

    owner, reply = await open_member(f"CREATE {SENTINEL} cid-rebuild A - {room}")
    room_token = token_of(reply, "room")
    owner.mark()
    await owner.send("CONNMODE stun")
    await owner.expect("OK CONNMODE", 8)
    await owner.wait_state(lambda s: "mode=stun" in s, timeout=8)

    check("重建前：mode=stun", "mode=stun" in owner.latest(),
          owner.latest())

    # Owner 的连接结束（没有 token -> 身份结束）-> 房间销毁
    await owner.close()
    deadline = time.time() + 10
    while time.time() < deadline and find_room(room_token) is not None:
        await asyncio.sleep(0.2)

    check("Owner 的身份结束 -> 房间被销毁", find_room(room_token) is None,
          str(find_room(room_token)))

    # 同一个 Owner 重建同一个房间：恢复他显式选过的 stun
    same, same_reply = await open_member(
        f"CREATE {SENTINEL} cid-rebuild A - {room}"
    )

    try:
        restored = await same.wait_state(
            lambda s: "mode=stun" in s, timeout=8, label="restored stun"
        )
        check("★ 同一个 Owner 重建 -> mode 恢复 stun（不是被跳回 relay）",
              restored is not None, same.latest())
        check("★ 而且是**带原因**的恢复：mode_reason=restored",
              restored is not None
              and token_of(restored, "mode_reason") == "restored",
              same.latest())
    finally:
        await same.close()

    # 换一个人建同名房间：绝对不能继承别人的模式
    deadline = time.time() + 10
    while time.time() < deadline and find_room(room_token) is not None:
        await asyncio.sleep(0.2)

    other, other_reply = await open_member(
        f"CREATE {SENTINEL} cid-other B - {room}"
    )

    try:
        fresh = await other.wait_state(
            lambda s: "mode=" in s, timeout=8, label="fresh default"
        )
        check("★ 换人重建同一个房间名 -> mode=relay + mode_reason=default"
              "（不继承别人的模式）",
              fresh is not None and token_of(fresh, "mode") == "relay"
              and token_of(fresh, "mode_reason") == "default",
              other.latest())
    finally:
        await other.close()


# ============================================================
# 3. SESSION —— 重连判定（token 有效性 vs 房间身份）
# ============================================================

async def test_session_probe(port, admin_port):
    print("")
    print("---- 3. SESSION 探测：闪断 / 被踢 / 退房 / 房间没了 ----")

    room = "probe"

    owner, reply = await open_member(f"CREATE {SENTINEL} cid-owner Owner - {room}")
    room_token = token_of(reply, "room")

    try:
        # 在房间里
        probe = await probe_session("cid-owner", "Owner", "-")
        check("★ 在房间里 -> in_room=1 / STILL_IN_ROOM / auto_reconnect=1",
              "OK SESSION" in probe and "in_room=1" in probe
              and "reason=STILL_IN_ROOM" in probe
              and "auto_reconnect=1" in probe and "relogin=0" in probe,
              probe)

        # 从没进过房的人
        probe = await probe_session("cid-nobody", "Nobody", "-")
        check("★ 没有任何记录 -> in_room=0 / NOT_IN_ROOM / 可自动重连",
              "in_room=0" in probe and "reason=NOT_IN_ROOM" in probe
              and "auto_reconnect=1" in probe, probe)

        # 游客：真实客户端免登录时拿到的身份
        guest, guest_reply = await open_member(
            f"JOIN {SENTINEL} cid-guest - - {room}"
        )
        guest_token = token_of(guest_reply, "guest")
        guest_name = token_of(guest_reply, "you")

        check("游客拿到 guest=<token>",
              bool(guest_token) and guest_token.startswith("guest-"),
              guest_token)

        probe = await probe_session("cid-guest", guest_name, guest_token)
        check("★ 游客在房间里 -> token=guest / STILL_IN_ROOM",
              "token=guest" in probe and "reason=STILL_IN_ROOM" in probe,
              probe)

        # ---- 被踢 ----
        await owner.send(f"KICK {room_token} cid-guest")
        check("Owner 踢掉游客", bool(await owner.expect("OK KICK", 8)))
        await asyncio.sleep(0.6)

        probe = await probe_session("cid-guest", guest_name, guest_token)
        check("★ 被踢之后 -> reason=KICKED / auto_reconnect=0（不许自动重连）",
              "reason=KICKED" in probe and "auto_reconnect=0" in probe,
              probe)

        # ---- 掉线（token 仍有效）-> 座位宽限期 -> 超时清退 ----
        guest2, guest2_reply = await open_member(
            f"JOIN {SENTINEL} cid-guest2 - - {room}"
        )
        tok2 = token_of(guest2_reply, "guest")
        name2 = token_of(guest2_reply, "you")

        guest2.writer.close()
        await asyncio.sleep(1.0)

        probe = await probe_session("cid-guest2", name2, tok2)
        check("★ 连接断了但 token 仍有效 -> 身份还在（in_room=1 / STILL_IN_ROOM）"
              " —— 「连接 != 身份」的落点",
              "in_room=1" in probe and "reason=STILL_IN_ROOM" in probe
              and "auto_reconnect=1" in probe, probe)

        await asyncio.sleep(SEAT_GRACE + 2.0)

        probe = await probe_session("cid-guest2", name2, tok2)
        check("★ 宽限期满（真正清退）-> in_room=0 / TIMEOUT / auto_reconnect=0",
              "in_room=0" in probe and "reason=TIMEOUT" in probe
              and "auto_reconnect=0" in probe, probe)

        # ---- 游客会话被 LOGOUT -> token 失效 ----
        guest3, guest3_reply = await open_member(
            f"JOIN {SENTINEL} cid-guest3 - - {room}"
        )
        tok3 = token_of(guest3_reply, "guest")
        name3 = token_of(guest3_reply, "you")
        await guest3.close()

        _r, w, logout = await open_raw(
            f"LOGOUT {SENTINEL} cid-guest3 - {tok3}"
        )
        w.close()
        check("游客 token 可以 LOGOUT", logout.startswith("OK LOGOUT"), logout)

        probe = await probe_session("cid-guest3", name3, tok3)
        check("★ token 已失效 -> valid=0 / relogin=1（必须重新登录）",
              "valid=0" in probe and "relogin=1" in probe
              and "auto_reconnect=0" in probe, probe)

        # ---- 主动退房 ----
        guest4, guest4_reply = await open_member(
            f"JOIN {SENTINEL} cid-guest4 - - {room}"
        )
        tok4 = token_of(guest4_reply, "guest")
        name4 = token_of(guest4_reply, "you")
        await guest4.send("LEAVE")
        await guest4.expect("OK LEAVE", 8)
        await asyncio.sleep(0.6)

        probe = await probe_session("cid-guest4", name4, tok4)
        check("★ 主动退房 -> reason=LEFT / auto_reconnect=0",
              "reason=LEFT" in probe and "auto_reconnect=0" in probe,
              probe)

        # ---- 房间被销毁（Owner 离开）----
        guest5, guest5_reply = await open_member(
            f"JOIN {SENTINEL} cid-guest5 - - {room}"
        )
        tok5 = token_of(guest5_reply, "guest")
        name5 = token_of(guest5_reply, "you")
        await owner.send("LEAVE")
        await owner.expect("OK LEAVE", 8)
        await asyncio.sleep(1.0)

        probe = await probe_session("cid-guest5", name5, tok5)
        check("★ 房间被销毁（Owner 离开）-> reason=ROOM_CLOSED / auto_reconnect=0",
              "reason=ROOM_CLOSED" in probe and "auto_reconnect=0" in probe,
              probe)

        await guest5.close()
    finally:
        await owner.close()


# ============================================================
# 4. host 漂移：释放 -> 认领 -> 新 host 上真的配对
# ============================================================

async def test_host_drift(port, admin_port):
    print("")
    print("---- 4. host 漂移：A 释放 -> B 认领 -> 玩家在新 host_id 上配对 ----")

    room = "hostdrift"

    a, a_reply = await open_member(f"CREATE {SENTINEL} cid-a A - {room}")
    room_token = token_of(a_reply, "room")
    b, _ = await open_member(f"JOIN {SENTINEL} cid-b B - {room}")

    try:
        # A = Owner + Host：认领 + 开服 + 一条隧道
        await a.send("CLAIM_HOST")
        check("A 认领 Host", bool(await a.expect("OK CLAIM_HOST", 8)))
        a.mark()
        _ar, aw, areply = await open_raw(f"HOST {room_token} cid-a A")
        check("A 开服", areply.startswith("OK HOST"), areply)

        paired = await a.wait_state(
            lambda s: token_of(s, "host_id") == "cid-a"
            and "cid-a" in parse_started(s), timeout=8, label="A hosting",
        )
        check("A 持有时 host_id=cid-a 且 started 含他", paired is not None,
              a.latest())

        # ---- A 主动释放 ----
        b.mark()
        await a.send("RELEASE_HOST")
        check("A RELEASE_HOST", bool(await a.expect("OK RELEASE_HOST", 8)))

        freed = await b.wait_state(
            lambda s: token_of(s, "host") == "0"
            and token_of(s, "host_name") == "-"
            and token_of(s, "host_id") == "-",
            timeout=8, label="B 看到 host=0",
        )
        check("★ A 释放 -> 广播 host=0 / host_name=- / host_id=-",
              freed is not None, b.latest())

        # 释放必须把 A 那条**开服控制连接**一起放掉，否则中继会一边报
        # host_id=-、一边继续拿 A 的隧道配对（host_id/started/conn 自洽，
        # 数据却进了卸任的人）。
        reply = await player_reply(room_token, "p1-cid", "P1")
        check("★ 释放之后新 PLAYER 被拒（主机位真的空了 -> NO_HOST）",
              reply.startswith("ERROR NO_HOST"), reply)

        # ---- B 认领 ----
        b.mark()
        await b.send("CLAIM_HOST")
        check("B 认领 Host", bool(await b.expect("OK CLAIM_HOST", 8)))

        claimed = await b.wait_state(
            lambda s: token_of(s, "host") == "1"
            and token_of(s, "host_name") == "B"
            and token_of(s, "host_id") == "cid-b",
            timeout=8, label="B claimed",
        )
        check("★ B 认领 -> 广播 host_id=cid-b / host_name=B",
              claimed is not None, b.latest())
        check("★ B 还没开服：started 里**不许**有他（门禁不放宽）",
              claimed is not None and "cid-b" not in parse_started(claimed),
              b.latest())

        reply = await player_reply(room_token, "p2-cid", "P2")
        check("★ B 已认领未开服 -> 新 PLAYER 收到 ERROR HOST_NOT_READY"
              "（与 NO_HOST 严格区分）",
              reply.startswith("ERROR HOST_NOT_READY"), reply)

        # ---- B 开服 + 隧道，玩家在老位置上完成配对 ----
        b.mark()
        _br, bw, breply = await open_raw(f"HOST {room_token} cid-b B")
        check("B 开服", breply.startswith("OK HOST"), breply)

        hosting = await b.wait_state(
            lambda s: token_of(s, "host_id") == "cid-b"
            and "cid-b" in parse_started(s)
            and parse_conn(s).get("cid-b") == "connected",
            timeout=8, label="B hosting",
        )
        check("★ B 开服后 host_id=/started=/conn= 三者立刻自洽",
              hosting is not None, b.latest())

        p_reader, p_writer, t_reader, t_writer, start, connected = (
            await pair_player(room_token, "p3-cid", "P3")
        )
        check("★★ 玩家在**新** host_id 上真的完成配对（START -> READY -> "
              "CONNECTED，不是只看广播）",
              start.startswith("START") and connected == "CONNECTED",
              f"{start!r} / {connected!r}")

        close_writers(bw, p_writer, t_writer)
    finally:
        await a.close()
        await b.close()


# ============================================================
# 5. 热切换：was_live 必须是**切换前**的真实状态
# ============================================================

async def test_hot_switch_was_live(port, admin_port):
    print("")
    print("---- 5. 换模式广播带 was_live（切换前有没有连接）----")

    # ---- 5a. 原本空闲 -> was_live=0 ----
    idle_room = "waslive-idle"
    owner, reply = await open_member(
        f"CREATE {SENTINEL} cid-idle Owner - {idle_room}"
    )
    room_token = token_of(reply, "room")

    try:
        owner.mark()
        await owner.send("CONNMODE stun")
        await owner.expect("OK CONNMODE", 8)
        await asyncio.sleep(0.5)

        line = await owner.wait_line("CONN_MODE_CHANGED", timeout=6)
        check("★ 原本空闲：CONN_MODE_CHANGED 带 was_live=0",
              line is not None and "was_live=0" in line, str(line))

        state = await owner.wait_state(
            lambda s: "was_live=" in s, timeout=6, label="was_live broadcast"
        )
        check("★ 原本空闲：紧随的 HOST_STATE 带 was_live=0",
              state is not None and "was_live=0" in state, owner.latest())

        # 一次性：之后的普通广播不再带 was_live
        owner.mark()
        await owner.send("STARTED 1")
        await owner.expect("OK STARTED", 8)
        await asyncio.sleep(0.5)
        later = await owner.wait_state(
            lambda s: True, timeout=4, label="post-was_live broadcast"
        ) or owner.latest()
        check("★ was_live 是一次性事实：之后的广播不再带它",
              "was_live=" not in later, later)
    finally:
        await owner.close()

    # ---- 5b. 原本有连接 -> was_live=1 ----
    live_room = "waslive-live"
    owner2, reply2 = await open_member(
        f"CREATE {SENTINEL} cid-live Owner - {live_room}"
    )
    room_token2 = token_of(reply2, "room")
    host, _ = await open_member(f"JOIN {SENTINEL} cid-h H - {live_room}")

    try:
        owner2.mark()
        await owner2.send("CONNMODE stun")
        await owner2.expect("OK CONNMODE", 8)
        await host.send("CLAIM_HOST")
        await host.expect("OK CLAIM_HOST", 8)
        _hr, hw, hreply = await open_raw(f"HOST {room_token2} cid-h H")
        check("Host 开服", hreply.startswith("OK HOST"), hreply)

        # 一条"已配对但还没进世界"的会话 = conn= 里的 pairing = 有连接
        _tr, tw, first = await tunnel_wait(room_token2)
        p_reader, p_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", RELAY_PORT), 8
        )
        p_writer.write(
            (f"PLAYER {room_token2} pair-cid Pair\n").encode()
        )
        await p_writer.drain()
        start = await read_line(_tr, timeout=8)
        check("会话进入 pairing（还没 READY）", start.startswith("START"), start)

        seen = await owner2.wait_state(
            lambda s: parse_conn(s).get("pair-cid") == "pairing",
            timeout=8, label="pairing state",
        )
        check("中继广播里这条会话是 pairing", seen is not None,
              owner2.latest())

        owner2.mark()
        await owner2.send("CONNMODE relay")
        await owner2.expect("OK CONNMODE", 8)
        await asyncio.sleep(0.5)

        line = await owner2.wait_line("CONN_MODE_CHANGED", timeout=6)
        check("★ 原本有连接：CONN_MODE_CHANGED 带 was_live=1",
              line is not None and "was_live=1" in line, str(line))

        state = await owner2.wait_state(
            lambda s: "was_live=1" in s, timeout=6, label="was_live=1 broadcast"
        )
        check("★ 原本有连接：HOST_STATE 带 was_live=1（切换前的真实状态）",
              state is not None, owner2.latest())
        check("★ 同一条广播里的 conn 已经清掉那条会话（事实与原因分开）",
              state is not None and "pair-cid" not in parse_conn(state),
              owner2.latest())

        close_writers(hw, p_writer, tw)
    finally:
        await owner2.close()
        await host.close()


# ============================================================
# 6. 连接 != 身份/职位：三种释放 + 一种不释放
# ============================================================

async def test_identity_vs_connection(port, admin_port):
    print("")
    print("---- 6. 连接状态 vs 房间身份/职位（主机位只在身份结束时释放）----")

    room = "idconn"

    owner, reply = await open_member(f"CREATE {SENTINEL} cid-owner Owner - {room}")
    room_token = token_of(reply, "room")
    host, _ = await open_member(f"JOIN {SENTINEL} cid-host H - {room}")

    try:
        await host.send("CLAIM_HOST")
        await host.expect("OK CLAIM_HOST", 8)
        _hr, hw, hreply = await open_raw(f"HOST {room_token} cid-host H")
        check("Host 开服", hreply.startswith("OK HOST"), hreply)

        # ---- ① 停服（开服连接断掉）：主机位必须保留 ----
        owner.mark()
        hw.close()
        stopped = await owner.wait_state(
            lambda s: token_of(s, "host") == "1"
            and token_of(s, "host_name") == "H"
            and parse_conn(s).get("cid-host") == "idle",
            timeout=10, label="host stopped",
        )
        check("★ ① 停服 -> host=1 / host_name=H / conn=idle"
              "（UI 语义：xxx（未开服））",
              stopped is not None, owner.latest())

        snapshot = find_room(room_token) or {}
        check("★ ① 管理快照里 host_claimed=true、host_online=false"
              "（职位与连接分开）",
              snapshot.get("host_claimed") is True
              and snapshot.get("host_online") is False,
              str({k: snapshot.get(k)
                   for k in ("host_claimed", "host_online", "host_name")}))

        await owner.send("CLAIM_HOST")
        denied = await owner.expect("ERROR HOST_TAKEN", 6)
        check("★ ① 别人（Owner 也不行）不能认领：ERROR HOST_TAKEN",
              bool(denied), str(denied or owner.latest()))

        reply = await player_reply(room_token, "p-idconn", "P")
        check("★ ① 新 PLAYER -> ERROR HOST_NOT_READY（不是 NO_HOST）",
              reply.startswith("ERROR HOST_NOT_READY"), reply)
        check("★ ① started= 里没有他（没开服），但 host_id= 仍然是他"
              "（职位还在）",
              stopped is not None
              and "cid-host" not in parse_started(stopped)
              and token_of(stopped, "host_id") == "cid-host",
              owner.latest())

        # ---- ② 主动释放：释放 ----
        owner.mark()
        await host.send("RELEASE_HOST")
        check("Host RELEASE_HOST", bool(await host.expect("OK RELEASE_HOST", 8)))
        released = await owner.wait_state(
            lambda s: token_of(s, "host") == "0",
            timeout=8, label="released",
        )
        check("★ ② 主动释放 -> host=0 / host_name=- / host_id=-",
              released is not None
              and token_of(released, "host_name") == "-"
              and token_of(released, "host_id") == "-",
              owner.latest())

        # ---- ③ 被清退（token 失效）：释放 ----
        guest, guest_reply = await open_member(
            f"JOIN {SENTINEL} cid-guest - - {room}"
        )
        gtok = token_of(guest_reply, "guest")
        gname = token_of(guest_reply, "you")
        await guest.send("CLAIM_HOST")
        check("游客认领 Host", bool(await guest.expect("OK CLAIM_HOST", 8)))
        _gr, gw, greply = await open_raw(f"HOST {room_token} cid-guest {gname}")
        check("游客开服", greply.startswith("OK HOST"), greply)

        # 身份结束：LOGOUT 掉他的游客会话
        _r, w, logout = await open_raw(f"LOGOUT {SENTINEL} cid-guest - {gtok}")
        w.close()
        check("游客 token LOGOUT", logout.startswith("OK LOGOUT"), logout)

        # 连接断掉：token 已失效 -> 立刻按"离开房间"处理（释放主机位）
        owner.mark()
        gw.close()
        with contextlib.suppress(Exception):
            guest.writer.close()

        gone = await owner.wait_state(
            lambda s: token_of(s, "host") == "0"
            and parse_conn(s).get("cid-guest") is None,
            timeout=10, label="token invalid -> released",
        )
        check("★ ③ token 失效后掉线 -> 主机位释放 + 成员整条消失",
              gone is not None, owner.latest())

        # ---- ④ 主动退房：释放 ----
        other, _ = await open_member(f"JOIN {SENTINEL} cid-leave L - {room}")
        await other.send("CLAIM_HOST")
        check("L 认领 Host", bool(await other.expect("OK CLAIM_HOST", 8)))
        owner.mark()
        await other.send("LEAVE")
        await other.expect("OK LEAVE", 8)

        left = await owner.wait_state(
            lambda s: token_of(s, "host") == "0", timeout=8, label="left"
        )
        check("★ ④ 主动退房 -> 主机位释放", left is not None, owner.latest())
    finally:
        await owner.close()
        await host.close()


# ============================================================
# 7. Owner / Host / Player 只是角色（三种组合 + 参数冻结）
# ============================================================

async def test_role_combinations(port, admin_port):
    print("")
    print("---- 7. Owner 兼职 Host / Owner 只当 Player / 非 Owner 当 Host ----")

    room = "roles"

    # 建房时带上人数（第 7 个字段）-> 容量在建房那一刻**冻结**。
    owner, reply = await open_member(
        f"CREATE {SENTINEL} cid-owner Owner - {room} 7"
    )
    room_token = token_of(reply, "room")
    host, _ = await open_member(f"JOIN {SENTINEL} cid-host H - {room}")

    try:
        # ---- 非 Owner 当 Host；Owner 只当 Player ----
        owner.mark()
        await host.send("CLAIM_HOST")
        check("非 Owner 的 H 认领 Host", bool(await host.expect("OK CLAIM_HOST", 8)))
        _hr, hw, hreply = await open_raw(f"HOST {room_token} cid-host H")
        check("H 开服", hreply.startswith("OK HOST"), hreply)

        state = await owner.wait_state(
            lambda s: token_of(s, "host_id") == "cid-host"
            and token_of(s, "host_name") == "H", timeout=8, label="H is host"
        )
        check("★ Owner 没认领时 host_id=H（Owner 只是 Player 之一）",
              state is not None, owner.latest())

        # Owner 以玩家身份连进世界（走 H 的隧道）
        owner.mark()
        p_reader, p_writer, t_reader, t_writer, start, connected = (
            await pair_player(room_token, "cid-owner", "Owner")
        )
        check("★ Owner 作为普通 Player 在 H 的 Host 上配对成功",
              start.startswith("START") and connected == "CONNECTED",
              f"{start!r} / {connected!r}")

        conn = await owner.wait_state(
            lambda s: parse_conn(s).get("cid-owner") == "connected",
            timeout=8, label="owner connected as player",
        )
        check("★ Owner 的 conn=connected（连接状态），host_id 仍是 H"
              "（三套东西不混）",
              conn is not None and token_of(conn, "host_id") == "cid-host",
              owner.latest())

        # ---- 房间参数创建后冻结：非 Owner 不能改模式 ----
        owner.mark()
        await owner.send("CONNMODE stun")     # 先定一个基准（Owner 可以）
        await owner.expect("OK CONNMODE", 8)

        await host.send("CONNMODE relay")
        denied = await host.expect("ERROR NOT_HOST", 6)
        check("★ 非 Owner 的 Host 改 CONNMODE -> ERROR NOT_HOST（参数冻结）",
              bool(denied), str(denied))

        mode_line = await owner.wait_state(
            lambda s: True, timeout=4, label="mode unchanged"
        )
        check("★ 被拒之后房间模式没有变（仍是 stun）",
              mode_line is not None and token_of(mode_line, "mode") == "stun",
              owner.latest())

        # 人数在建房时冻结：连当前 Host 也改不了
        await host.send("MAXPLAYERS 3")
        denied = await host.expect("ERROR BAD_MAXPLAYERS", 6)
        check("★ 建房后人数冻结：Host 改 MAXPLAYERS 也被拒"
              "（ERROR BAD_MAXPLAYERS）",
              bool(denied), str(denied))

        # ---- Owner 兼职 Host：释放给 Owner 再认领 ----
        await host.send("RELEASE_HOST")
        await host.expect("OK RELEASE_HOST", 8)
        await owner.send("CLAIM_HOST")
        check("★ Owner 释放后可以自己认领 Host（Owner + Host 组合）",
              bool(await owner.expect("OK CLAIM_HOST", 8)))
        owner.mark()
        _or, ow, oreply = await open_raw(f"HOST {room_token} cid-owner Owner")
        check("Owner 开服", oreply.startswith("OK HOST"), oreply)

        both = await owner.wait_state(
            lambda s: token_of(s, "host_id") == "cid-owner"
            and "cid-owner" in parse_started(s),
            timeout=8, label="owner hosting",
        )
        check("★ Owner 同时是 Host：host=1 / host_id=cid-owner",
              both is not None, owner.latest())

        close_writers(hw, ow, p_writer, t_writer)
    finally:
        await owner.close()
        await host.close()


# ============================================================
# 7b. 满员 + 本人重连：不许把自己的位子判成 ROOM_FULL
# ============================================================

async def test_full_room_reconnect(port, admin_port):
    print("")
    print("---- 7b. 满员房间里本人重连（座位还在）不许 ROOM_FULL ----")

    room = "fullreconnect"

    owner, reply = await open_member(
        f"CREATE {SENTINEL} cid-owner Owner - {room} 2"
    )
    room_token = token_of(reply, "room")

    try:
        guest, guest_reply = await open_member(
            f"JOIN {SENTINEL} cid-g - - {room}"
        )
        tok = token_of(guest_reply, "guest")
        name = token_of(guest_reply, "you")

        check("房间已满（owner + guest = 2/2）",
              (find_room(room_token) or {}).get("members") == 2,
              str(find_room(room_token)))

        # 陌生人进满员房 -> ROOM_FULL（没被放宽）
        stranger_reply = ""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", RELAY_PORT), 8
        )
        writer.write(
            (f"JOIN {SENTINEL} cid-stranger Stranger - {room}\n").encode()
        )
        await writer.drain()
        stranger_reply = await read_line(reader, timeout=8)
        writer.close()

        check("★ 陌生人进满员房 -> ERROR ROOM_FULL（上限没放宽）",
              stranger_reply.startswith("ERROR ROOM_FULL"), stranger_reply)

        # 本人掉线（游客 token 仍有效 -> 座位保留），再重连
        guest.writer.close()
        await asyncio.sleep(1.0)

        probe = await probe_session("cid-g", name, tok)
        check("掉线后座位仍保留（in_room=1）",
              "in_room=1" in probe, probe)

        again, again_reply = await open_member(
            f"JOIN {SENTINEL} cid-g - {tok} {room}"
        )

        check("★★ 满员时本人重连 -> OK JOIN（不是 ROOM_FULL；位子是他的）",
              again_reply.startswith("OK JOIN"), again_reply)
        check("★ 重连后成员数还是 2（顶替自己的座位，没有变成 3）",
              (find_room(room_token) or {}).get("members") == 2,
              str(find_room(room_token)))

        await again.close()
    finally:
        await owner.close()


# ============================================================
# 8. 静态：模式的写入点只有一个，读取绝不凭空造 relay
# ============================================================

def test_mode_single_writer_static():
    print("")
    print("---- 8. 静态：`conn_mode` 只允许在唯一写入点被赋值 ----")

    allowed = {
        # 构造默认值（新房间 = relay，老客户端行为不变）
        'self.conn_mode = "relay"',
        # 房间销毁/重建时恢复 Owner 的选择
        "room.conn_mode = remembered",
        # 唯一写入 API：room_members.set_room_conn_mode
        "room.conn_mode = wanted",
    }

    offenders = []

    server_dir = os.path.join(ROOT, "server")

    for name in sorted(os.listdir(server_dir)):
        if not name.endswith(".py"):
            continue

        path = os.path.join(server_dir, name)

        with io.open(path, encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                stripped = line.strip()

                if not re.match(r"^(room|self)\.conn_mode\s*=", stripped):
                    continue

                if stripped in allowed:
                    continue

                offenders.append(f"{name}:{number}: {stripped}")

    check("★ 除了 Room.__init__ 的默认值 / 重建恢复 / 唯一写入函数，"
          "没有任何地方直接赋值 conn_mode",
          not offenders, "；".join(offenders[:6]))

    with io.open(
        os.path.join(server_dir, "room_members.py"), encoding="utf-8"
    ) as fh:
        room_members_text = fh.read()

    check("★ set_room_conn_mode 是唯一的写入 API（房主 CONNMODE 走它）",
          "def set_room_conn_mode(" in room_members_text
          and "set_room_conn_mode(room, wanted, member=member, cause=\"owner\")"
          in room_members_text,
          "room_members.set_room_conn_mode")

    code_line_offenders = [
        line.strip()
        for line in room_members_text.splitlines()
        if not line.strip().startswith("#")
        and (
            'getattr(room, "conn_mode", "relay")' in line
            or "getattr(room, 'conn_mode', 'relay')" in line
        )
    ]
    check("★ 广播里没有 `getattr(room, 'conn_mode', 'relay')` 这种凭空兜底"
          "（注释不算）",
          not code_line_offenders,
          "；".join(code_line_offenders[:3]) or "读口径必须走 room_conn_mode(room)")

    # 读口径：没有这个字段时返回 `-`，绝不返回 relay
    sys.path.insert(0, server_dir)

    try:
        import room_members  # noqa: E402

        class FakeRoom:
            pass

        fake = FakeRoom()
        check("★ room_conn_mode(没有字段的房间) == '-'（不是 relay）",
              room_members.room_conn_mode(fake) == "-",
              room_members.room_conn_mode(fake))

        fake.conn_mode = "stun"
        check("★ room_conn_mode 读到真实值时原样返回",
              room_members.room_conn_mode(fake) == "stun",
              room_members.room_conn_mode(fake))

        class FakeWritable:
            name = "static-room"

        room = FakeWritable()
        room.conn_mode = "relay"
        room.conn_mode_reason = "default"

        class FakeMember:
            client_id = "cid-x"

        room_members.set_room_conn_mode(
            room, "stun", member=FakeMember(), cause="owner"
        )
        check("★ set_room_conn_mode 写入 + 记来源=owner",
              room.conn_mode == "stun" and room.conn_mode_reason == "owner",
              f"{room.conn_mode} / {room.conn_mode_reason}")

        room_members.set_room_conn_mode(room, "bogus", cause="owner")
        check("★ set_room_conn_mode 拒绝非法值",
              room.conn_mode == "stun", room.conn_mode)
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(server_dir)


# ============================================================
# 驱动
# ============================================================

async def run_real_relay_tests(proc, log, log_path):
    try:
        await test_internal_paths_never_touch_mode(
            RELAY_PORT, ADMIN_PORT, log_path
        )
        await test_mode_across_destroy_rebuild(RELAY_PORT, ADMIN_PORT)
        await test_session_probe(RELAY_PORT, ADMIN_PORT)
        await test_host_drift(RELAY_PORT, ADMIN_PORT)
        await test_hot_switch_was_live(RELAY_PORT, ADMIN_PORT)
        await test_identity_vs_connection(RELAY_PORT, ADMIN_PORT)
        await test_role_combinations(RELAY_PORT, ADMIN_PORT)
        await test_full_room_reconnect(RELAY_PORT, ADMIN_PORT)
    except Exception:
        import traceback
        check("真中继 mode/identity 测试没有抛异常", False)
        traceback.print_exc()
    else:
        check("真中继 mode/identity 测试没有抛异常", True)

    return read_log(log_path)


def main():
    print("=" * 70)
    print("CraftLink：房间模式 / 房间身份 / 重连判定（真中继）")
    print("=" * 70)

    try:
        test_mode_single_writer_static()
    except Exception:
        import traceback
        check("静态检查没有抛异常", False)
        traceback.print_exc()
    else:
        check("静态检查没有抛异常", True)

    proc, log, log_path = start_relay()
    started = proc.poll() is None

    check("中继启动", started, "进程已退出，见 tests/mode_identity_relay.log")

    relay_text = ""

    try:
        if started:
            relay_text = asyncio.run(run_real_relay_tests(proc, log, log_path))
    finally:
        stop_relay(proc, log)
        relay_text = relay_text or read_log(log_path)

    check("★ 服务端日志里没有任何 [ROOM CREATE] 是由迟到握手造出来的"
          "（房间只由 CREATE / legacy HOST 建）",
          "-ghost" not in relay_text
          or "[ROOM CREATE] room=modekeep-ghost" not in relay_text,
          "见 tests/mode_identity_relay.log")

    check("★ 服务端日志里有唯一写入点留下的 [CONN MODE] 记录",
          "[CONN MODE]" in relay_text, "")

    print("")
    print("=" * 70)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 70)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
