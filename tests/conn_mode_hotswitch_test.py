"""
CraftLink -- 连接模式热切换：切换之后**必须还能重新配对/重建**（真中继 + 真握手）

用户 2026-09-28 报的 P0（原话）：

    「房主切换后，通知所有人给我重建所有隧道并热重连啊，现在热切换不能用了，
      连不上，隧道好像没有重建」

上一轮已经做到"切换瞬间清 sessions + 等待池，先清理后广播 CONN_MODE_CHANGED"。
这一套回答的是下一半问题：**清完之后，客户端还能不能真的重建起来**。

A. **同一个人重连开服 -> 立刻顶替旧的开服控制连接**（服务端 P0）
   客户端换模式 / 停止→重开时是"先关旧的 `HOST <room>`，立刻再开一条新的"。
   旧连接的 FIN 还没被服务端读完时新握手就到了 —— 旧代码一律回
   `ERROR ROOM_ALREADY_HAS_HOST`，客户端只能退避 3s 重试；这 3 秒里
   `host_alive()` 为假、新隧道还会被旧 handler 的清理顺手打掉 =
   "连不上、隧道没有重建"。
   现在：同一个 `client_id`（= 主机位持有人本人）的新连接直接接管，旧的让位；
   **别人**（不同 client_id）仍然 `ROOM_ALREADY_HAS_HOST`，§A"一个房间最多
   一个 Host"不破。

B. **relay -> stun -> relay 每一跳都能重新配对**
   每一步都按真实客户端的方式重建（关掉旧的 HOST/HOST_TUNNEL/PLAYER，重新
   握手），并且断言：
     * 新 `HOST <room>` 立刻拿到 `OK HOST`（不是 ROOM_ALREADY_HAS_HOST）；
     * 新 `HOST_TUNNEL` 立刻进池（不被人清掉），并能与 PLAYER 配对拿到
       `START` -> `READY` -> `CONNECTED`；
     * 清理之后广播的 `HOST_STATE` 里 `host_id=` / `started=` / `mode=` 自洽
       （旧配对的 cid 不再挂在 started 里）；
     * 老 PLAYER socket 在切换瞬间被关掉（严格重建全部通路）。

C. **真客户端核心**（MCLinkEngine，就是 GUI 用的那条代码路径）走完整一圈
   relay -> stun -> relay，最后**真的用一条 Minecraft 连接把字节跑通**。

Run:
    python tests/conn_mode_hotswitch_test.py
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_HOTSWITCH_PORT", "19580"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_HOTSWITCH_ADMIN_PORT", "19581"))
MC_PORT = int(os.environ.get("MCLINK_TEST_HOTSWITCH_MC_PORT", "19582"))
LOCAL_PORT = int(os.environ.get("MCLINK_TEST_HOTSWITCH_LOCAL_PORT", "19583"))

ADMIN_TOKEN = "hotswitch-test-admin-token"
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


def tokens_of(text):
    out = {}

    for token in str(text or "").split()[1:]:
        key, _, value = token.partition("=")
        out[key] = value

    return out


def started_of(text):
    value = str(text or "")

    if value.startswith("HOST_STATE"):
        value = tokens_of(value).get("started", "")
    else:
        value = tokens_of(f"HOST_STATE {value}").get("started", "")

    return {cid for cid in value.split(",") if cid and cid != "-"}


def conn_of(text):
    if str(text or "").startswith("HOST_STATE"):
        return tokens_of(text).get("conn", "")

    return tokens_of(f"HOST_STATE {text}").get("conn", "")


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
            # 内建 STUN：真客户端核心在 stun 模式要用到（和 direct_e2e_test
            # 同一套默认端口 3478/3479）。
            "MCLINK_STUN_ENABLED": "1",
            "MCLINK_STUN_BIND": "127.0.0.1",
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


def read_log(path):
    with contextlib.suppress(OSError, TypeError):
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    return ""


# ============================================================
# 插座 / 成员连接小工具
# ============================================================

async def open_raw(line, timeout=6, read_reply=True):
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


async def read_line(reader, timeout=6):
    with contextlib.suppress(asyncio.TimeoutError):
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return raw.decode(errors="replace").strip()

    return ""


async def wait_eof(reader, timeout=5.0):
    """读到 EOF（忽略中间的心跳 / INFO 行）。"""
    deadline = time.time() + timeout

    while time.time() < deadline:
        line = await read_line(reader, timeout=max(0.2, deadline - time.time()))

        if line == "":
            return True

    return False


class Member:
    """成员控制连接：读推送（含 HOST_STATE）、回 PING。"""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.lines = []
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

    async def wait_state(self, predicate, since=0.0, timeout=8, label=""):
        """等一条 `since` **之后**到达、且满足 predicate 的 HOST_STATE。"""
        deadline = time.time() + timeout

        while time.time() < deadline:
            for stamp, text in reversed(self.states):
                if stamp >= since and predicate(tokens_of(text)):
                    return text

            await asyncio.sleep(0.05)

        print(f"  !! timeout waiting for {label}: "
              f"{[t for _s, t in self.states[-2:]]}", flush=True)
        return None

    def latest(self):
        return self.states[-1][1] if self.states else ""

    async def close(self):
        if self._task is not None:
            self._task.cancel()

            with contextlib.suppress(BaseException):
                await self._task

        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()


async def open_member(line, timeout=8):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=timeout
    )
    member = Member(reader, writer)
    reply = await member.join(line, timeout=timeout)
    return member, reply


# ============================================================
# 管理 API
# ============================================================

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
    for room in admin_rooms().get("data") or []:
        if room.get("room") == room_id:
            return room

    return None


# ============================================================
# A0. 纯逻辑：顶替判据（同人 + token）
# ============================================================

def test_supersede_predicate():
    print("")
    print("---- A0. 顶替判据：同 client_id + 账号体系下同 token ----")

    sys.path.insert(0, os.path.join(ROOT, "server"))

    try:
        import relay_server
    except Exception as e:  # noqa: BLE001
        check("能导入 relay_server（顶替判据单测）", False, repr(e))
        return

    same = relay_server._same_host_person

    check("同 client_id -> 是本人", same("cid", "cid") is True, "cid/cid")
    check("不同 client_id -> 不是本人", same("cid", "other") is False, "cid/other")
    check("任一边为空 -> 不是本人",
          same("", "cid") is False and same("cid", "") is False, "empty")
    check("★ 账号体系下：同 client_id 但 token 不同 -> 不是本人（不许顶替）",
          same("cid", "cid", "token-a", "token-b") is False, "a vs b")
    check("★ 账号体系下：同 client_id + 同 token -> 是本人",
          same("cid", "cid", "token-a", "token-a") is True, "a vs a")
    check("免登录（旧连接没有 token）：只看 client_id",
          same("cid", "cid", "", "") is True, "no token")


# ============================================================
# A. 同一个人重连开服 -> 顶替（P0 的服务端半边）
# ============================================================

async def test_host_supersede(room_id):
    print("")
    print("---- A. 真中继：同一个人重连开服 -> 立刻顶替（不是 3s 后重试）----")

    owner, reply = await open_member(
        f"CREATE {SENTINEL} cid-owner Owner - {room_id}"
    )
    token = tokens_of(reply).get("room", "")
    host, _ = await open_member(f"JOIN {SENTINEL} cid-host Host - {room_id}")

    w1 = w2 = w3 = None

    try:
        await host.send("CLAIM_HOST")
        await host.expect("OK CLAIM_HOST", timeout=6)

        r1, w1, a1 = await open_raw(f"HOST {room_id} cid-host Host")
        check("第一个开服连接建立（OK HOST）", a1.startswith("OK HOST"), a1)

        t1r, t1w, t1reply = await open_raw(
            f"HOST_TUNNEL {room_id} cid-host cid-host"
        )
        check("开服时隧道进池（WAIT）", t1reply == "WAIT", t1reply)

        # ---- 同一个 client_id：不关旧连接，直接再来一条（最坏时序）----
        t0 = time.monotonic()
        r2, w2, a2 = await open_raw(f"HOST {room_id} cid-host Host", timeout=6)
        elapsed = time.monotonic() - t0

        check("★★ 同一个人的新开服连接【立刻】拿到 OK HOST"
              "（不再 ROOM_ALREADY_HAS_HOST）",
              a2.startswith("OK HOST") and elapsed < 2.0,
              f"{a2!r} elapsed={elapsed:.3f}s")

        # 旧的必须被顶掉（socket 关掉 -> 客户端读到 EOF）
        old_closed = await wait_eof(r1, timeout=4)

        check("★ 旧的开服连接被顶掉（读到 EOF）", old_closed, "EOF")

        # 旧连接那条隧道不能被"顺手清理"打掉
        await asyncio.sleep(0.4)
        room = find_room(token)
        idle = int((room or {}).get("idle_tunnels") or 0)

        check("★★ 新开服后隧道还在池子里（旧 handler 的清理不许把它吃掉）",
              idle >= 1, f"idle_tunnels={idle}")

        # 玩家能立刻配上这条隧道（隧道真的活着）
        p_reader, p_writer, _ = await open_raw(
            f"PLAYER {room_id} cid-p Player", read_reply=False
        )
        start = await read_line(t1r, timeout=6)

        check("★ 隧道可用：PLAYER 接入 -> START", start.startswith("START"), start)

        t1w.write(b"READY\n")
        await t1w.drain()
        connected = await read_line(p_reader, timeout=6)

        check("★ 顶替之后整条链路照常进世界（CONNECTED）",
              connected == "CONNECTED", connected)

        with contextlib.suppress(Exception):
            p_writer.close()

        # ---- 换一个 client_id：必须仍然被拒（§A 一个房间最多一个 Host）----
        r3, w3, a3 = await open_raw(
            f"HOST {room_id} cid-someone-else Other", timeout=6
        )

        check("★ 不同的人抢开服连接 -> 仍然 ERROR ROOM_ALREADY_HAS_HOST",
              a3.startswith("ERROR ROOM_ALREADY_HAS_HOST"), a3)

        with contextlib.suppress(Exception):
            t1w.close()
    finally:
        for writer in (w1, w2, w3):
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()

        for member in (owner, host):
            await member.close()


# ============================================================
# B. relay -> stun -> relay：每一跳都必须能重新配对
# ============================================================

async def pair_once(room_id):
    """一套完整的开服 + 配对握手；返回 (是否成功, 细节, 句柄)。"""
    handle = [None, None, None, None]

    h_reader, h_writer, h_reply = await open_raw(
        f"HOST {room_id} cid-host Host"
    )
    handle[0] = h_writer

    if not h_reply.startswith("OK HOST"):
        return False, f"REBUILD: HOST -> {h_reply!r}", handle

    t_reader, t_writer, t_reply = await open_raw(
        f"HOST_TUNNEL {room_id} cid-host cid-host"
    )
    handle[1] = t_writer

    if t_reply != "WAIT":
        return False, f"REBUILD: HOST_TUNNEL -> {t_reply!r}", handle

    p_reader, p_writer, _ = await open_raw(
        f"PLAYER {room_id} cid-p Player", read_reply=False
    )
    handle[2] = p_writer
    handle[3] = p_reader

    start = await read_line(t_reader, timeout=6)

    if not start.startswith("START"):
        return False, f"REBUILD: tunnel got {start!r}", handle

    t_writer.write(b"READY\n")
    await t_writer.drain()

    connected = await read_line(p_reader, timeout=6)

    if connected != "CONNECTED":
        return False, f"REBUILD: player got {connected!r}", handle

    return True, "CONNECTED", handle


def close_pair(handle):
    if handle is None:
        return

    for item in handle:
        if item is None:
            continue

        with contextlib.suppress(Exception):
            item.close()


async def test_hot_switch_cycle(room_id):
    print("")
    print("---- B. 真中继：relay -> stun -> relay，每一跳都能重新配对 ----")

    owner, reply = await open_member(
        f"CREATE {SENTINEL} cid-owner Owner - {room_id}"
    )
    host, _ = await open_member(f"JOIN {SENTINEL} cid-host Host - {room_id}")
    player, _ = await open_member(f"JOIN {SENTINEL} cid-p Player - {room_id}")

    handle = None

    try:
        await host.send("CLAIM_HOST")
        await host.expect("OK CLAIM_HOST", timeout=6)

        await owner.send("CONNMODE relay")
        check("起始模式 = relay",
              bool(await owner.expect("OK CONNMODE mode=relay", timeout=6)),
              "OK CONNMODE mode=relay")

        ok, detail, handle = await pair_once(room_id)

        check("起始：relay 下配对成功", ok, detail)

        for mode in ("stun", "relay"):
            t0 = time.monotonic()

            # 换模式（真实 Owner 的 CONNMODE）
            await owner.send(f"CONNMODE {mode}")

            changed = await owner.expect("CONN_MODE_CHANGED", timeout=6)
            ack = await owner.expect(f"OK CONNMODE mode={mode}", timeout=6)

            check(f"切到 {mode}：成员收到 CONN_MODE_CHANGED",
                  bool(changed), str(changed))
            check(f"切到 {mode}：OK CONNMODE", bool(ack), str(ack))

            # 旧 PLAYER socket 必须被关掉（严格重建全部通路）
            if handle[3] is not None:
                check(f"切到 {mode}：旧的 PLAYER socket 被关掉（读到 EOF）",
                      await wait_eof(handle[3], timeout=5), "EOF")

            # 清理**之后**的那条完整 HOST_STATE：mode= 是新模式、带 conn=，
            # 而且旧配对的 cid-p 不再挂在 started= 里。
            state = await owner.wait_state(
                lambda t, m=mode: t.get("mode") == m
                and "conn=" in text_of_tokens(t)
                and "cid-host" in started_of(text_of_tokens(t))
                and "cid-p" not in started_of(text_of_tokens(t)),
                since=t0, timeout=6, label=f"{mode} post-cleanup HOST_STATE",
            )

            check(f"★ 切到 {mode}：清理后的 HOST_STATE 自洽"
                  f"（mode=new / 带 conn= / started 只剩开服的 Host）",
                  bool(state), str(state)[:200])

            # 旧句柄（隧道已被中继关掉）收尾，然后按真实客户端方式重建
            close_pair(handle)
            handle = None

            ok, detail, handle = await pair_once(room_id)

            check(f"★★ 切到 {mode} 后立刻重新配对成功"
                  f"（HOST/HOST_TUNNEL/PLAYER）",
                  ok, detail)

            state = await owner.wait_state(
                lambda t, m=mode: t.get("mode") == m
                and t.get("host_id") == "cid-host"
                and "cid-host" in started_of(text_of_tokens(t))
                and "cid-p" in started_of(text_of_tokens(t)),
                since=t0, timeout=8, label=f"{mode} re-paired HOST_STATE",
            )

            check(f"★★ 切到 {mode} 后 HOST_STATE 自洽"
                  f"（host_id=Host 且 started 含 host+player）",
                  bool(state), str(state)[:200])

        close_pair(handle)
    finally:
        close_pair(handle)

        for member in (owner, host, player):
            await member.close()


def text_of_tokens(tokens):
    return "HOST_STATE " + " ".join(f"{k}={v}" for k, v in tokens.items())


# ============================================================
# C. 真客户端核心：relay -> stun -> relay + 最后真的跑通一条游戏字节流
# ============================================================

def fake_world(stop):
    """假 Minecraft 局域网世界：ECHO。"""

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", MC_PORT))
    srv.listen(16)
    srv.settimeout(0.5)

    def serve(conn):
        try:
            while True:
                data = conn.recv(65536)

                if not data:
                    return

                conn.sendall(b"MCBANNER:" + data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    while not stop.is_set():
        try:
            conn, _addr = srv.accept()
        except OSError:
            continue

        threading.Thread(target=serve, args=(conn,), daemon=True).start()


def game_roundtrip(payload, timeout=25):
    try:
        sock = socket.create_connection(("127.0.0.1", LOCAL_PORT), 8)
    except OSError as e:
        return None, f"connect failed: {e}"

    sock.settimeout(timeout)

    try:
        sock.sendall(payload)
        got = bytearray()

        while len(got) < len(payload) + 9:
            chunk = sock.recv(65536)

            if not chunk:
                break

            got.extend(chunk)

        return bytes(got), ""
    except OSError as e:
        return None, str(e)
    finally:
        with contextlib.suppress(Exception):
            sock.close()


def test_real_client_hot_switch(room_id):
    print("")
    print("---- C. 真客户端核心：relay -> stun -> relay，最后真的跑通游戏字节 ----")

    stop = threading.Event()
    threading.Thread(target=fake_world, args=(stop,), daemon=True).start()
    time.sleep(0.3)

    sys.path.insert(0, os.path.join(ROOT, "client"))

    from mclink.config import settings as cfgmod
    from mclink.core import engine as eng_mod

    host = eng_mod.MCLinkEngine()
    player = eng_mod.MCLinkEngine()
    host.start_loop()
    player.start_loop()

    def mkcfg(role, mode):
        cfg = cfgmod.load_config()
        cfg.role = role
        cfg.client_id = "h-cid" if role == "host" else "p-cid"
        cfg.server_host = "127.0.0.1"
        cfg.server_port = RELAY_PORT
        cfg.room = room_id
        cfg.mc_host = "127.0.0.1"
        cfg.mc_port = MC_PORT
        cfg.local_port = LOCAL_PORT
        cfg.max_tunnels = 2
        cfg.mode = mode
        return cfg

    def wait_online(engine, label, timeout=30):
        deadline = time.time() + timeout

        while time.time() < deadline:
            if engine.snapshot().get("state") == "ONLINE":
                return True

            time.sleep(0.4)

        print(f"  !! {label} not online: {engine.snapshot().get('state')} "
              f"err={engine.snapshot().get('last_error')}", flush=True)
        return False

    try:
        host.open_room("127.0.0.1", RELAY_PORT, "h-cid", "host", "", room_id,
                       create=True, max_players=4, conn_mode="relay")
        time.sleep(0.4)
        player.open_room("127.0.0.1", RELAY_PORT, "p-cid", "player", "",
                         room_id)
        host.claim_host()
        time.sleep(0.4)

        host.start_session(mkcfg("host", "relay"))
        time.sleep(0.5)
        player.start_session(mkcfg("player", "relay"))

        check("真客户端：relay 基线两条会话都 ONLINE",
              wait_online(host, "host") and wait_online(player, "player"))
        time.sleep(0.6)

        got, err = game_roundtrip(b"R1")
        check("真客户端：relay 基线游戏字节跑通",
              got == b"MCBANNER:R1", f"{got!r} {err}")

        # ---- 换模式（和界面 _apply_room_mode / _sync_session_mode 同一条路径）----
        ok, message = host.set_conn_mode("stun")
        check("真客户端：Owner 切到 stun（set_conn_mode 成功）", ok, str(message))
        time.sleep(0.5)

        host.stop_session(timeout=15)
        host.start_session(mkcfg("host", "stun"))
        player.stop_session(timeout=15)
        player.start_session(mkcfg("player", "stun"))

        check("真客户端：切到 stun 之后两条会话都重建成功（ONLINE）",
              wait_online(host, "host") and wait_online(player, "player"))

        # ---- 切回 relay ----
        ok, message = host.set_conn_mode("relay")
        check("真客户端：Owner 切回 relay（set_conn_mode 成功）", ok, str(message))
        time.sleep(0.5)

        host.stop_session(timeout=15)
        host.start_session(mkcfg("host", "relay"))
        player.stop_session(timeout=15)
        player.start_session(mkcfg("player", "relay"))

        check("真客户端：切回 relay 之后两条会话都重建成功（ONLINE）",
              wait_online(host, "host") and wait_online(player, "player"))
        time.sleep(0.8)

        got, err = game_roundtrip(b"R3")
        check("★★ 真客户端：热切换一圈之后游戏字节仍然跑通（隧道真的重建了）",
              got == b"MCBANNER:R3", f"{got!r} {err}")
    finally:
        with contextlib.suppress(Exception):
            host.stop_session(timeout=5)
            player.stop_session(timeout=5)
            host.close_room(timeout=5)
            player.close_room(timeout=5)

        stop.set()


# ============================================================

async def run_raw_tests():
    await test_host_supersede("hotswitch-supersede")
    await test_hot_switch_cycle("hotswitch-cycle")


def main():
    print("=" * 70)
    print("CraftLink 连接模式热切换：切换后必须还能重建 / 重新配对")
    print("=" * 70)

    proc, log, log_path = start_relay("conn_mode_hotswitch_relay.log")
    started = proc.poll() is None

    check("中继启动", started, "进程已退出，见 tests/conn_mode_hotswitch_relay.log")

    try:
        test_supersede_predicate()
    except Exception:
        import traceback
        check("顶替判据单测没有抛异常", False)
        traceback.print_exc()
    else:
        check("顶替判据单测没有抛异常", True)

    if started:
        try:
            asyncio.run(run_raw_tests())
        except Exception:
            import traceback
            check("热切换（协议层）测试没有抛异常", False)
            traceback.print_exc()
        else:
            check("热切换（协议层）测试没有抛异常", True)

        try:
            test_real_client_hot_switch("hotswitch-room")
        except Exception:
            import traceback
            check("热切换（真客户端）测试没有抛异常", False)
            traceback.print_exc()
        else:
            check("热切换（真客户端）测试没有抛异常", True)

        stop_relay(proc, log)

    text = read_log(log_path)

    check("★ 只有「故意用别人 client_id 抢」那一次被拒（同一个人一次都没被拒）",
          text.count("[HOST REJECT]") == 1,
          f"rejects={text.count('[HOST REJECT]')} "
          "见 tests/conn_mode_hotswitch_relay.log")

    check("★ 服务端日志里有 [HOST SUPERSEDE]（顶替路径真的走到了）",
          "[HOST SUPERSEDE]" in text,
          "见 tests/conn_mode_hotswitch_relay.log")

    print("")
    print("=" * 70)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 70)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
