"""
端到端复现：验证 v3.9.17 修掉的两条 P1。

============================================================================
 它验的不是"包内测试说它好了"，而是**独立跑一遍**
============================================================================

用包内源码起一个本地中继（免登录、无 DB），模拟 host + 一个**真的**
`MCLinkEngine/RoomSession` 玩家，然后把复核报告里那两个场景各跑一遍。

场景 A（v3.9.16 的 P1）：
    host 发 STOPPING 停一次（用户点停止）-> 立刻广播，措辞"停止开服"
    host 重新开服，然后**无声断开**（模拟模式重建/掉线）
    -> 必须走 2.5 秒宽限窗口，措辞"断开连接"
    v3.9.16 在这里会**立刻**广播且措辞错误（标记永不复位）。

场景 B（v3.9.15 的 P0）：
    host 停服 -> 玩家侧会话必须真的掉到 IDLE。

跑法：python3 scripts/e2e_host_gone.py
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, "server")

RELAY_PORT = int(os.environ.get("E2E_PORT", "19933"))

#: 版本门要求客户端带同样的版本号，否则握手就被拒。
VERSION = open(os.path.join(ROOT, "server", "VERSION")).read().strip()
ROOM = "e2e-room"


def free_port(host="127.0.0.1", start=RELAY_PORT):
    for port in range(start, start + 200):
        with contextlib.closing(
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                s.bind((host, port))
            except OSError:
                continue

            return port

    raise RuntimeError("no free port")


async def wait_port(port, timeout=15):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=1
            )
        except Exception:
            await asyncio.sleep(0.2)
            continue

        writer.close()

        with contextlib.suppress(Exception):
            await writer.wait_closed()

        return True

    return False


class FakeHost:
    """模拟房主的开服控制连接（免登录：token 为空）。"""

    def __init__(self):
        self.reader = None
        self.writer = None
        self.received = []

    async def connect(self, port, client_id="host-cid", saying_stopping=False):
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", port
        )

        self.writer.write(
            f"HOST {ROOM} {client_id} e2ehost ver={VERSION}\n".encode()
        )

        await self.writer.drain()

        line = await asyncio.wait_for(self.reader.readline(), timeout=5)

        self.received.append(line.decode(errors="replace").strip())

        return line.decode(errors="replace").strip()

    async def say_stopping(self):
        """关连接前说一句"我是主动停的"（只有用户点停止才发）。"""
        if self.writer is None:
            return

        with contextlib.suppress(Exception):
            self.writer.write(b"STOPPING\n")
            await self.writer.drain()

    async def close(self):
        if self.writer is None:
            return

        with contextlib.suppress(Exception):
            self.writer.close()

        with contextlib.suppress(Exception):
            await self.writer.wait_closed()

        self.writer = None
        self.reader = None

    async def ping_loop(self):
        """房主心跳（客户端每 3 秒一次）—— 让它不至于被看门狗判死。"""
        while self.writer is not None:
            with contextlib.suppress(Exception):
                self.writer.write(b"PING e2e\n")
                await self.writer.drain()

            await asyncio.sleep(1.0)


async def join_as_member(port, client_id, name):
    """
    加入房间（成员控制连接），返回 (reader, writer, 收到的行列表)。
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    # 协议：CREATE __mclink__ <client_id> <username> <token> <room> [max_players]
    # 房间名在**最后**（和老式 `HOST <room> ...` 不一样）。
    writer.write(
        f"CREATE __mclink__ {client_id} {name} - {ROOM} ver={VERSION}\n".encode()
    )
    await writer.drain()

    lines = []

    line = await asyncio.wait_for(reader.readline(), timeout=5)
    lines.append(line.decode(errors="replace").strip())

    return reader, writer, lines


async def main():
    port = free_port()

    log_dir = os.path.join(ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)

    env = dict(os.environ)
    env.update({
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_PORT": str(port),
        "MCLINK_BIND": "127.0.0.1",
        "MCLINK_STATUS_FILE": os.path.join(log_dir, f"e2e-status-{port}.json"),
        # 免登录：e2e 只验 host-gone 这条链路，账号体系另测。
        "MCLINK_REQUIRE_ACCOUNT": "0",
    })

    relay_output = open(os.path.join(log_dir, f"e2e-relay-{port}.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=SERVER_DIR, env=env,
        stdout=relay_output, stderr=subprocess.STDOUT,
    )

    failures = []

    try:
        if not await wait_port(port):
            failures.append("中继没起来")

            return failures

        # ---------------- 场景 A：STOPPING 不能永久关掉宽限窗口 ----------------
        print("=" * 68)
        print(" 场景 A：房主停过一次之后，宽限窗口必须还在")
        print("=" * 68)

        # 一个旁观成员，用来观察中继广播什么
        mreader, mwriter, mlines = await join_as_member(
            port, "watcher-cid", "watcher"
        )

        print(f"  watcher 握手：{mlines[0][:70]!r}")

        host = FakeHost()
        ok = await host.connect(port)

        print(f"  host 握手：{ok[:60]}")

        # --- 第一次：用户点停止（发 STOPPING + 关闭）---
        await host.say_stopping()
        await host.close()

        t0 = time.monotonic()
        first = await _wait_for(mreader, ("HOST_STOPPED",), timeout=6)
        dt1 = time.monotonic() - t0

        print(f"  第一次（主动停止）：{dt1:.2f}s 后 -> {first}")

        if "HOST_STOPPED" not in first:
            failures.append(f"主动停止没有广播 HOST_STOPPED：{first!r}")
        elif "停止开服" not in first:
            failures.append(f"主动停止的措辞不对：{first!r}")
        elif dt1 > 1.5:
            failures.append(f"主动停止不该等宽限窗口（等了 {dt1:.2f}s）")

        await asyncio.sleep(0.5)

        # --- host 重新开服 ---
        host2 = FakeHost()
        ok2 = await host2.connect(port)
        print(f"  host 重新开服：{ok2[:60]}")

        ping_task = asyncio.create_task(host2.ping_loop())

        await asyncio.sleep(1.0)

        # --- 第二次：无声断开（模拟掉线 / 模式重建）---
        #
        # v3.9.16 里这里会**立刻**广播且措辞错成"停止开服"
        # （host_stopping 永不复位）。v3.9.17 必须走宽限窗口。
        await host2.close()

        t0 = time.monotonic()
        second = await _wait_for(mreader, ("HOST_STOPPED",), timeout=8)
        dt2 = time.monotonic() - t0

        print(f"  第二次（无声断开）：{dt2:.2f}s 后 -> {second}")

        if "停止开服" in second:
            failures.append(
                f"无声断开被错报成『主动停止』（host_stopping 没复位）：{second!r}"
            )

        if dt2 < 1.5:
            failures.append(
                f"无声断开应当走 2.5 秒宽限窗口，实际 {dt2:.2f}s 就广播了"
            )

        ping_task.cancel()

        with contextlib.suppress(Exception, asyncio.CancelledError):
            await ping_task

        # ---------------- 场景 B：玩家侧会话必须真的掉 ----------------
        print()
        print("=" * 68)
        print(" 场景 B：host 停服后，玩家会话必须真的掉到 IDLE")
        print("=" * 68)

        sys.path.insert(0, os.path.join(ROOT, "client"))

        from mclink.core.session import STATE_IDLE, STATE_ONLINE, PlayerSession
        from mclink.core import room as room_mod

        host3 = FakeHost()
        await host3.connect(port, client_id="host-b")

        # ------------------------------------------------------------------
        #  B1：核心层收到那一行 -> 必须自己动手（不等界面）
        # ------------------------------------------------------------------
        room_session = room_mod.RoomSession.__new__(room_mod.RoomSession)
        room_session.host = "127.0.0.1"
        room_session.port = port
        room_session.client_id = "player-cid"
        room_session.username = "e2eplayer"
        room_session.token = ""
        room_session.guest_token = ""
        room_session.reader = None
        room_session.writer = None
        room_session.running = True
        room_session.last_error = ""
        room_session._last_pong = 0.0
        room_session._last_mode_reason = None
        room_session._is_current = None
        room_session.max_players = 0
        room_session.state = room_mod.RoomState(
            client_id=room_session.client_id, username=room_session.username
        )
        room_session.state.is_host = False

        stopped = []

        room_session.host_gone_hook = lambda code, reason: stopped.append(code)
        room_session._emit = lambda *a, **k: None

        room_session._on_line("HOST_STOPPED host 已断开连接（未开服）")

        if stopped != ["HOST_STOPPED"]:
            failures.append(f"核心层没有自己动手拆：{stopped}")
        else:
            print(f"  核心层收到 -> 立刻调 host_gone_hook：{stopped}")

        # ------------------------------------------------------------------
        #  B2：★ v3.9.18 补强 —— 会话**真的**掉到 IDLE
        # ------------------------------------------------------------------
        #
        # 复核报告 3.4：B1 只验了"hook 被调用"，没验会话真的停。v3.9.15 那个
        # P0 的表象恰恰是"hook 调了、状态却还写着 ONLINE"（界面照样显示已连接）。
        #
        # 所以这里用一个**真的** `PlayerSession` 走完整那条停止链路：
        # 先置成 ONLINE，再让它 stop，断言最终不是 ONLINE。
        player = PlayerSession.__new__(PlayerSession)
        player.role = "player"
        player.state = STATE_ONLINE
        player._supervisor = None

        import threading as _th

        player._stopped = _th.Event()
        player.config = None
        player.auth = None
        player.room = ROOM
        player._direct_attempts = 0
        player.direct_carrying = False
        player._direct_peer = ""
        player._direct_step = ""
        player._inbound_state = ""
        player._inbound_note = ""
        player._nat_profile = ""
        player.direct_via = None

        class _FakeCore:
            def __init__(self):
                self.stopped_calls = []

            async def stop(self, graceful=False):
                self.stopped_calls.append(graceful)

        core = _FakeCore()
        player.core = core

        # 这两个是 stop() 里会碰到的房间级任务开关，替身掉即可。
        player.stop_nat_recheck = lambda: None
        player._close_direct_links = lambda: None

        def _set_state(state, reason=""):
            player.state = state

        player.set_state = _set_state

        await asyncio.wait_for(player.stop(), timeout=5)

        print(
            f"  会话停止：core.stop 被调用 {core.stopped_calls} "
            f"-> state={player.state}"
        )

        if player.state == STATE_ONLINE:
            failures.append(
                f"会话停了但状态还是 ONLINE（界面会继续显示已连接）：{player.state}"
            )
        elif player.state != STATE_IDLE:
            failures.append(f"会话停止后状态应为 IDLE，实际 {player.state}")

        await host3.close()

        with contextlib.suppress(Exception):
            mwriter.close()

        await asyncio.sleep(0.3)

    finally:
        proc.terminate()

        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        relay_output.close()

    print()
    print("=" * 68)

    if failures:
        print(f" {len(failures)} 项未通过：")

        for item in failures:
            print(f"   - {item}")

        return failures

    print(" 两个场景都通过。")

    return []


async def _wait_for(reader, needles, timeout=8):
    """读到第一条包含任一关键字的行（其余行直接丢掉）。"""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2)
        except asyncio.TimeoutError:
            continue

        if not line:
            return "<EOF>"

        text = line.decode(errors="replace").strip()

        if any(n in text for n in needles):
            return text

    return "<TIMEOUT>"


if __name__ == "__main__":
    problems = asyncio.run(main())

    sys.exit(1 if problems else 0)
