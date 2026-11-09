"""
CraftLink -- 每成员「游戏连接状态」立刻同步（真中继 + 真握手）

用户 2026-09-28 原话：

    「这边玩家断开连接，那边 host 这个连接状态还是显示已连接，未及时更正状态，
      所以我觉得可以把连接状态也包进我们管理广播 TCP 里，这样会比较好，
      所有状态迅速同步」

在此之前，中继的 HOST_STATE 广播里**没有**"每个成员此刻的游戏连接状态"：
客户端成员列表的「连接状态」列只能对房间里的人一律写「已连接」（它只知道
"这个人在房间里"）。所以玩家掉线之后 Host 那边那一行还挂着「已连接」。

这一套钉住三件事（全部 ADDITIVE，老客户端忽略新 token）：

  1. `HOST_STATE ... conn=<cid>:<state>,...`（PROTOCOL §8.5）
     * 一张**全表**：房间里每个成员各一条；
     * `state` 只有 4 个取值：idle | waiting | pairing | connected；
     * 人离开房间 -> 他整条消失（不是 idle）。
  2. **每一次变化都立刻广播**，不等心跳、不等轮询：
     Host 开服 -> connected；PLAYER 接入 -> waiting；配对 -> pairing；
     进世界 -> connected；PLAYER socket 断开（含"还在等待池里"）-> 立刻
     不再 connected；Host 停服 -> idle；被踢 -> 移出。
     "立刻"的判据是**小于 2 秒**：成员心跳 / 看门狗周期都是 15 秒量级，
     2 秒内看到只可能是推送。
  3. `STARTED <1|0>`（PROTOCOL §8.6）：「已开始连接」的**显式上报** =
     用户点过「开始连接」。它与"连接现算"取并集进 `started=`，而且
     **不放宽** §B 门禁（无 Host -> NO_HOST；Host 未开服 -> HOST_NOT_READY）。

以及管理快照 `member_list` 同步带 `conn` / `connecting`（PROTOCOL §8.7）。

Run:
    python tests/member_conn_state_test.py
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_CONN_PORT", "19560"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_CONN_ADMIN_PORT", "19561"))
ADMIN_TOKEN = "conn-state-test-admin-token"
ROOM = "conn-state-room"
ROOM2 = "conn-state-gate"
SENTINEL = "__mclink__"

#: "立刻"的判据：成员心跳 15s / 看门狗 15s 量级，2 秒内看到只可能是推送。
IMMEDIATE_S = 2.0

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
# 解析（和客户端要照做的解析一模一样：按 `,` 切、再按第一个 `:` 切）
# ============================================================

def tokens_of(text):
    out = {}

    for token in str(text or "").split()[1:]:
        key, _, value = token.partition("=")
        out[key] = value

    return out


def parse_conn(text):
    """`conn=<cid>:<state>,...` -> {cid: state}（`-` / 缺字段 -> {}）。"""
    value = tokens_of(text).get("conn", "")

    if not value or value == "-":
        return {}

    table = {}

    for item in value.split(","):
        if not item:
            continue

        cid, _, state = item.partition(":")

        if cid:
            table[cid] = state

    return table


def parse_started(text):
    value = tokens_of(text).get("started", "")

    return {cid for cid in value.split(",") if cid and cid != "-"}


# ============================================================
# 真中继子进程
# ============================================================

def start_relay(log_name):
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
            # 看门狗放长：这一套量的是"推送有多快"，不许被超时清理干扰。
            "MCLINK_MEMBER_IDLE_TIMEOUT": "30",
            "MCLINK_MEMBER_WATCHDOG_INTERVAL": "3",
            "MCLINK_HOST_IDLE_TIMEOUT": "30",
            "MCLINK_HOST_WATCHDOG_INTERVAL": "3",
            "MCLINK_VERBOSE": "1",
        }
    )
    env["MCLINK_VERSION"] = ""

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


# ============================================================
# 成员控制连接：读推送、回 PING、按时刻记录每一次 HOST_STATE
# ============================================================

class Watcher:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.lines = []
        #: [(时刻, conn 表, started 集合, 原始行)]
        self.states = []
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

                if text.upper().startswith("PING"):
                    self.writer.write(
                        (f"PONG {text[4:].strip()}\n").encode()
                    )
                    await self.writer.drain()
                    continue

                if text.upper().startswith("HOST_STATE"):
                    self.states.append(
                        (
                            time.monotonic(),
                            parse_conn(text),
                            parse_started(text),
                            text,
                        )
                    )
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
        return self.states[-1] if self.states else (0.0, {}, set(), "")

    async def wait_conn(self, cid, state, since, timeout=IMMEDIATE_S):
        """等到同一行里 `conn[cid] == state`；返回 (是否, 耗时, 那一行)。

        `since` 是**动作发生之前**的时刻：只有这个时刻之后到达的广播才算数，
        否则"上一条旧广播里正好也是这个值"会造出假阳性（也会把"是不是立刻"
        这件事量错）。
        """
        started_at = time.monotonic()
        deadline = started_at + timeout

        while time.monotonic() < deadline:
            for stamp, table, _started, raw in reversed(self.states):
                if stamp >= since and table.get(cid) == state:
                    return True, stamp - since, raw

            await asyncio.sleep(0.05)

        _stamp, table, _started, raw = self.latest()
        return False, time.monotonic() - started_at, (
            f"conn={table} latest={raw[:160]}"
        )

    async def wait_started(self, cid, present, since, timeout=IMMEDIATE_S):
        started_at = time.monotonic()
        deadline = started_at + timeout

        while time.monotonic() < deadline:
            for stamp, _table, started, raw in reversed(self.states):
                if stamp >= since and ((cid in started) is present):
                    return True, stamp - since, raw

            await asyncio.sleep(0.05)

        _stamp, _table, started, raw = self.latest()
        return False, time.monotonic() - started_at, (
            f"started={sorted(started)} latest={raw[:160]}"
        )

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
            await self.writer.wait_closed()


async def open_watcher(line, timeout=8):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=timeout
    )
    watcher = Watcher(reader, writer)
    reply = await watcher.join(line, timeout=timeout)
    return watcher, reply


async def open_raw(line, timeout=8, read_reply=True):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    reply = ""

    if read_reply:
        with contextlib.suppress(asyncio.TimeoutError):
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
            reply = raw.decode(errors="replace").strip()

    return reader, writer, reply


async def read_line(reader, timeout=8):
    with contextlib.suppress(asyncio.TimeoutError):
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return raw.decode(errors="replace").strip()

    return ""


def admin_rooms():
    request = urllib.request.Request(
        f"http://127.0.0.1:{ADMIN_PORT}/rooms",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )

    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def find_room(room_id):
    payload = admin_rooms()

    for room in payload.get("data") or []:
        if room.get("room") == room_id:
            return room

    return None


def member_row(room, client_id):
    for row in (room or {}).get("member_list") or []:
        if row.get("client_id") == client_id:
            return row

    return None


# ============================================================
# 1. conn= 全表 + 每次变化立刻广播
# ============================================================

async def test_conn_states(room_id):
    print("")
    print("---- 1. 真中继：conn= 全表 + waiting/pairing/connected 立刻推送 ----")

    owner, owner_reply = await open_watcher(
        f"CREATE {SENTINEL} cid-owner Owner - {room_id}"
    )
    room_token = tokens_of(owner_reply).get("room", "")

    player, _ = await open_watcher(f"JOIN {SENTINEL} cid-p Player - {room_id}")
    host, _ = await open_watcher(f"JOIN {SENTINEL} cid-host Host - {room_id}")

    tunnel = None
    player_socket = None

    try:
        check("房间建好（owner=1）",
              tokens_of(owner_reply).get("owner") == "1", owner_reply)

        # ---- ① 初始：全表、人人 idle ----
        await asyncio.sleep(0.5)
        _stamp, table, _started, raw = owner.latest()

        check("★ HOST_STATE 带 conn= 字段",
              "conn=" in raw, raw[:200])
        check("★ conn= 是**全表**：房间里每个成员各一条",
              set(table) == {"cid-owner", "cid-p", "cid-host"}, str(table))
        check("★ 刚开始谁都没连：全员 idle",
              set(table.values()) == {"idle"}, str(table))

        # ---- ② Host 开服 -> Host 那一行立刻 connected ----
        t0 = time.monotonic()
        h_reader, h_writer, h_reply = await open_raw(
            f"HOST {room_id} cid-host Host"
        )
        check("Host 开服（OK HOST）", h_reply.startswith("OK HOST"), h_reply)

        ok, elapsed, detail = await owner.wait_conn(
            "cid-host", "connected", t0
        )

        check("★ Host 开服 -> 全体【立刻】看到 cid-host connected（不等心跳）",
              ok and elapsed < IMMEDIATE_S,
              f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        # ---- ③ PLAYER 接入 -> waiting ----
        t0 = time.monotonic()
        p_reader, p_writer, _ = await open_raw(
            f"PLAYER {room_id} cid-p Player", read_reply=False
        )
        player_socket = (p_reader, p_writer)

        ok, elapsed, detail = await owner.wait_conn("cid-p", "waiting", t0)

        check("★ PLAYER 接入（还在等待池）-> 全体【立刻】看到 waiting",
              ok and elapsed < IMMEDIATE_S,
              f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        # ---- ④ 隧道到位 -> pairing ----
        t0 = time.monotonic()
        t_reader, t_writer, _ = await open_raw(
            f"HOST_TUNNEL {room_id} cid-host cid-host"
        )
        tunnel = (t_reader, t_writer)

        start = await read_line(t_reader, timeout=6)

        check("隧道拿到 START（配对成功）", start.startswith("START"), start)

        ok, elapsed, detail = await owner.wait_conn("cid-p", "pairing", t0)

        check("★ 配对成功 -> 全体【立刻】看到 pairing（不是停在 waiting）",
              ok and elapsed < IMMEDIATE_S,
              f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        # ---- ⑤ READY / CONNECTED -> connected ----
        t0 = time.monotonic()
        t_writer.write(b"READY\n")
        await t_writer.drain()

        connected = await read_line(p_reader, timeout=6)

        check("玩家收到 CONNECTED（进世界）", connected == "CONNECTED", connected)

        ok, elapsed, detail = await owner.wait_conn(
            "cid-p", "connected", t0
        )

        check("★ 进世界 -> 全体【立刻】看到 connected",
              ok and elapsed < IMMEDIATE_S,
              f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        # ---- ⑥ 玩家断开 PLAYER socket -> 立刻不再 connected ----
        t0 = time.monotonic()
        p_writer.close()

        ok, elapsed, detail = await owner.wait_conn("cid-p", "idle", t0)

        check("★★ 玩家 PLAYER socket 断开 -> 全体【立刻】看到它不再是 connected"
              "（用户报的那一条）",
              ok and elapsed < IMMEDIATE_S,
              f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        player_socket = None

        # ---- ⑦ 等待池里的 PLAYER 掉线（从来没配上隧道）也要立刻回落 ----
        #
        # 这一条以前没有看护：等待期间中继不读那条 socket，只有下一次
        # try_pair / 20s 握手看门狗才会发现，界面就挂着「已连接」。
        t0 = time.monotonic()
        p2_reader, p2_writer, _ = await open_raw(
            f"PLAYER {room_id} cid-p Player", read_reply=False
        )

        ok, _elapsed, detail = await owner.wait_conn("cid-p", "waiting", t0)

        check("等待池里的 PLAYER 记为 waiting",
              ok, detail if not ok else "")

        t0 = time.monotonic()
        p2_writer.close()

        ok, elapsed, detail = await owner.wait_conn("cid-p", "idle", t0)

        check("★★ PLAYER 在等待池里掉线 -> 立刻回落 idle（等待期看护）",
              ok and elapsed < IMMEDIATE_S,
              f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        # ---- ⑧ 隧道被上一个会话消耗掉了（会话结束 = 拆两条 socket）----
        #
        # 这正是"每个 Host<->Player 是独立会话"的另一面：一条隧道只服务一次
        # 配对，会话终态时中继把它和 PLAYER 一起关掉（客户端会重新开一条）。
        # 所以这里重新开一条隧道，验证**新隧道**能立刻配上、立刻进世界。
        with contextlib.suppress(Exception):
            t_writer.close()

        t0 = time.monotonic()
        t2_reader, t2_writer, _ = await open_raw(
            f"HOST_TUNNEL {room_id} cid-host cid-host"
        )
        tunnel = (t2_reader, t2_writer)

        p3_reader, p3_writer, _ = await open_raw(
            f"PLAYER {room_id} cid-p Player", read_reply=False
        )

        ok, elapsed, detail = await owner.wait_conn("cid-p", "pairing", t0)

        check("★ 新隧道 + 新 PLAYER 立刻配对（清理没有把新连接吃掉）",
              ok, f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        start2 = await read_line(t2_reader, timeout=6)
        check("隧道收到 START", start2.startswith("START"), start2)

        t2_writer.write(b"READY\n")
        await t2_writer.drain()
        connected2 = await read_line(p3_reader, timeout=6)

        check("第二个会话进世界（CONNECTED）", connected2 == "CONNECTED",
              connected2)

        # ---- ⑨ Host 停服 -> idle ----
        t0 = time.monotonic()
        h_writer.close()

        ok, elapsed, detail = await owner.wait_conn("cid-host", "idle", t0)

        check("★ Host 停服（开服控制连接结束）-> 全体【立刻】看到 idle",
              ok and elapsed < IMMEDIATE_S,
              f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        with contextlib.suppress(Exception):
            p3_writer.close()

        # ---- ⑩ 管理快照 member_list 带 conn ----
        snap = find_room(room_token)
        owner_row = member_row(snap, "cid-owner")

        check("★ 管理快照 member_list 带 conn / connecting（面板同一来源）",
              owner_row is not None
              and owner_row.get("conn") == "idle"
              and "connecting" in owner_row,
              str(owner_row))

        with contextlib.suppress(Exception):
            t_writer.close()
    finally:
        for item in (tunnel, player_socket):
            if item:
                with contextlib.suppress(Exception):
                    item[1].close()

        with contextlib.suppress(Exception):
            h_writer.close()

        for watcher in (owner, player, host):
            await watcher.close()


# ============================================================
# 2. 被踢 / 离开 -> 从 conn= 里消失
# ============================================================

async def test_kick_removes_member(room_id):
    print("")
    print("---- 2. 真中继：被踢 / 离开房间 -> 从 conn= 整条消失 ----")

    owner, owner_reply = await open_watcher(
        f"CREATE {SENTINEL} cid-owner Owner - {room_id}"
    )
    room_token = tokens_of(owner_reply).get("room", "")
    player, _ = await open_watcher(f"JOIN {SENTINEL} cid-p Player - {room_id}")
    host, _ = await open_watcher(f"JOIN {SENTINEL} cid-host Host - {room_id}")

    try:
        await host.send("CLAIM_HOST")
        check("Host 认领", bool(await host.expect("OK CLAIM_HOST", timeout=6)))

        h_reader, h_writer, _ = await open_raw(
            f"HOST {room_id} cid-host Host"
        )

        p_reader, p_writer, _ = await open_raw(
            f"PLAYER {room_id} cid-p Player", read_reply=False
        )

        t_reader, t_writer, _ = await open_raw(
            f"HOST_TUNNEL {room_id} cid-host cid-host"
        )
        await read_line(t_reader, timeout=6)
        t_writer.write(b"READY\n")
        await t_writer.drain()

        check("玩家进世界", await read_line(p_reader, timeout=6) == "CONNECTED")

        ok, _elapsed, detail = await owner.wait_conn(
            "cid-p", "connected", time.monotonic() - 5.0
        )

        check("踢之前：cid-p = connected", ok, detail if not ok else "")

        t0 = time.monotonic()
        await owner.send(f"KICK {room_token} cid-p")
        ack = await owner.expect("OK KICK", timeout=6)

        check("Owner 踢掉 cid-p（OK KICK）", bool(ack), str(ack))

        gone = False

        while time.monotonic() - t0 < IMMEDIATE_S:
            _stamp, table, _started, _raw = owner.latest()

            if "cid-p" not in table:
                gone = True
                break

            await asyncio.sleep(0.05)

        check("★★ 被踢的人【立刻】从 conn= 整条消失（不是还挂着 connected）",
              gone, str(owner.latest()[1]))

        _stamp, table, _started, _raw = owner.latest()
        check("剩下的人仍在表里（owner + host）",
              set(table) >= {"cid-owner", "cid-host"}, str(table))

        with contextlib.suppress(Exception):
            t_writer.close()
    finally:
        with contextlib.suppress(Exception):
            h_writer.close()

        for watcher in (owner, player, host):
            await watcher.close()


# ============================================================
# 3. STARTED 显式上报（"点了开始连接"）+ §B 门禁不许放宽
# ============================================================

async def test_explicit_started(room_id):
    print("")
    print("---- 3. 真中继：STARTED 显式上报（点了开始连接）----")

    owner, owner_reply = await open_watcher(
        f"CREATE {SENTINEL} cid-owner Owner - {room_id}"
    )
    player, _ = await open_watcher(f"JOIN {SENTINEL} cid-p Player - {room_id}")

    try:
        # 等玩家那条 JOIN 引起的广播真正到达（否则读到的是更早的一行）
        arrived = False

        for _ in range(60):
            _stamp, table, _started, _raw = owner.latest()

            if "cid-p" in table:
                arrived = True
                break

            await asyncio.sleep(0.05)

        check("玩家进房后 owner 立刻收到包含 cid-p 的 conn= 全表",
              arrived, str(owner.latest()[1]))

        _stamp, table, started, _raw = owner.latest()

        check("还没开始：cid-p 不在 started= 里",
              "cid-p" not in started, f"started={sorted(started)}")
        check("还没开始：cid-p 的 conn= 是 idle",
              table.get("cid-p") == "idle", str(table))

        t0 = time.monotonic()
        await player.send("STARTED 1")
        ack = await player.expect("OK STARTED", timeout=6)

        check("★ STARTED 1 -> OK STARTED started=1",
              bool(ack) and "started=1" in ack, str(ack))

        ok, elapsed, detail = await owner.wait_started("cid-p", True, t0)

        check("★★ 点了开始连接 -> 全体【立刻】在 started= 里看到 cid-p"
              "（即使他还没有任何 PLAYER 连接）",
              ok and elapsed < IMMEDIATE_S,
              f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        _stamp, table, _started, _raw = owner.latest()

        check("★ 意图与事实分开：started= 里有他，但 conn= 仍然是 idle",
              table.get("cid-p") == "idle", str(table))

        t0 = time.monotonic()
        await player.send("STARTED 0")
        ack = await player.expect("OK STARTED started=0", timeout=6)

        check("STARTED 0 -> OK STARTED started=0", bool(ack), str(ack))

        ok, elapsed, detail = await owner.wait_started("cid-p", False, t0)

        check("★ 停止后【立刻】从 started= 里移除",
              ok and elapsed < IMMEDIATE_S,
              f"elapsed={elapsed:.3f}s {detail if not ok else ''}")

        await player.send("STARTED 2")
        bad = await player.expect("ERROR BAD_STARTED", timeout=6)

        check("非法取值 -> ERROR BAD_STARTED（不静默）", bool(bad), str(bad))
    finally:
        for watcher in (owner, player):
            await watcher.close()


async def test_gate_not_weakened():
    print("")
    print("---- 4. §B 门禁不因显式上报放宽（无 Host / Host 未开服）----")

    owner, owner_reply = await open_watcher(
        f"CREATE {SENTINEL} cid-owner2 Owner - {ROOM2}"
    )
    player, _ = await open_watcher(f"JOIN {SENTINEL} cid-p2 Player - {ROOM2}")

    try:
        await player.send("STARTED 1")
        await player.expect("OK STARTED", timeout=6)

        # 没人认领 Host：PLAYER 仍然 NO_HOST
        _r, w, reply = await open_raw(f"PLAYER {ROOM2} cid-p2 Player")

        check("★ 没人认领 Host + 点了开始 -> 仍然 ERROR NO_HOST",
              reply.startswith("ERROR NO_HOST"), reply)

        with contextlib.suppress(Exception):
            w.close()

        # 有人认领但没开服：PLAYER 仍然 HOST_NOT_READY
        host, _ = await open_watcher(f"JOIN {SENTINEL} cid-h2 Host - {ROOM2}")
        await host.send("CLAIM_HOST")
        await host.expect("OK CLAIM_HOST", timeout=6)

        _r2, w2, reply2 = await open_raw(f"PLAYER {ROOM2} cid-p2 Player")

        check("★ Host 未开服 + 点了开始 -> 仍然 ERROR HOST_NOT_READY",
              reply2.startswith("ERROR HOST_NOT_READY"), reply2)

        with contextlib.suppress(Exception):
            w2.close()
        await host.close()
    finally:
        for watcher in (owner, player):
            await watcher.close()


# ============================================================

async def run_all():
    proc, log, log_path = start_relay("member_conn_state_relay.log")
    ok = proc.poll() is None

    check("中继启动", ok, "进程已退出，见 tests/member_conn_state_relay.log")

    if not ok:
        return

    try:
        await test_conn_states(ROOM)
        await test_kick_removes_member("conn-state-kick")
        await test_explicit_started("conn-state-started")
        await test_gate_not_weakened()
    finally:
        stop_relay(proc, log)


def main():
    print("=" * 70)
    print("CraftLink 每成员连接状态：conn= 全表 + 变化立刻广播 + STARTED 显式上报")
    print("=" * 70)

    try:
        asyncio.run(run_all())
    except Exception:
        import traceback
        check("conn 状态测试没有抛异常", False)
        traceback.print_exc()
    else:
        check("conn 状态测试没有抛异常", True)

    print("")
    print("=" * 70)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 70)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
