"""
MC-Link -- 「玩家数每刷新一次就涨」的复现与回归（朋友报的问题）。

============================================================================
 现象
============================================================================

朋友报的原话（转述）：**Minecraft 里每刷新一次服务器列表，房间里的玩家数就
涨一次**，而且每刷一次都重新建一条隧道。

机制上完全说得通，值得钉死：

  * Minecraft 的"刷新局域网列表"不是空操作 —— 它对本地代理发一次**完整
    的 TCP 连接 + 握手 + 状态查询**，然后就断开；
  * 我们的玩家侧是"一条 Minecraft 连接 = 一条中继会话"（冻结协议就是这样），
    所以每次刷新都会新建一条会话、和房主配一次对；
  * 而房间里的「玩家数」数的是**会话数**（`players=N/M` = 配对数），
    不是"具体几个人"。

所以只要哪次刷新的会话**没有被回收**，那个数字就只会往上涨，永远不回落。

这一套就是干这件事：连一次 → 断开 → 等一会儿 → 数一次，连做 6 轮，
**每次断开后数字都必须回落到同一个值**。涨了就是没回收。
"""

import contextlib
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.config import settings as cfgmod  # noqa: E402
from mclink.core import engine as eng_mod  # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_REFRESH_PORT", "19150"))
MC_PORT = int(os.environ.get("MCLINK_TEST_REFRESH_MC", "19151"))
LOCAL_PORT = int(os.environ.get("MCLINK_TEST_REFRESH_LOCAL", "19152"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def fake_world(stop):
    """房主本机的局域网世界（每条连接发一次横幅，然后回答状态查询）。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", MC_PORT))
    srv.listen(8)

    def serve(conn):
        try:
            conn.sendall(b"MC-BANNER-FROM-HOST\n")

            while True:
                data = conn.recv(4096)

                if not data:
                    return

                conn.sendall(b"ECHO:" + data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    while not stop.is_set():
        try:
            conn, _addr = srv.accept()
        except OSError:
            return

        threading.Thread(target=serve, args=(conn,), daemon=True).start()


def start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        "MCLINK_STUN_ENABLED": "0",
        "MCLINK_STATUS_FILE": os.path.join(HERE, "refresh_status.json"),
        "MCLINK_STATUS_INTERVAL": "30",
    })

    log = open(
        os.path.join(HERE, "refresh_relay.log"), "w", encoding="utf-8",
        errors="replace",
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
            socket.create_connection(("127.0.0.1", RELAY_PORT), 1).close()
            return process, log
        except OSError:
            if process.poll() is not None:
                return process, log
            time.sleep(0.3)

    return process, log


@contextlib.contextmanager
def one_refresh():
    """
    一次"服务器列表刷新"：连上本地代理、读横幅、问一句……**保持连着**，
    由调用方决定什么时候断开。

    这正是 Minecraft 刷新列表时做的事（区别只是它读的是状态 JSON）。
    保持连着是必须的：房间里的人数只在"这条会话活着"的时候才是 2，
    连完立刻断开的话根本量不到峰值。
    """
    mc = socket.create_connection(("127.0.0.1", LOCAL_PORT), 8)

    try:
        mc.settimeout(8)
        banner = mc.recv(100)
        ok = b"MC-BANNER" in banner

        if ok:
            mc.sendall(b"STATUS?")
            ok = mc.recv(200).startswith(b"ECHO:")

        yield ok
    finally:
        with contextlib.suppress(Exception):
            mc.close()


def main():
    print("=" * 64)
    print("MC-Link 刷新计数回归（朋友报的：每刷新一次玩家数就涨）")
    print("=" * 64)

    stop = threading.Event()
    threading.Thread(target=fake_world, args=(stop,), daemon=True).start()
    time.sleep(0.3)

    relay, relay_log = start_relay()

    host = eng_mod.MCLinkEngine()
    player = eng_mod.MCLinkEngine()
    host.start_loop()
    player.start_loop()

    try:
        # ---- 建房 / 进房（和中继模式下的真实流程一致）----
        host.open_room(
            "127.0.0.1", RELAY_PORT, "h-cid", "host", "", "refreshroom",
            create=True, max_players=2, conn_mode="relay",
        )
        time.sleep(0.5)
        player.open_room(
            "127.0.0.1", RELAY_PORT, "p-cid", "player", "", "refreshroom",
        )

        ready = False

        for _ in range(40):
            time.sleep(0.5)
            state = player.room_state()

            if state is not None and state.client_id:
                ready = True
                break

        check("两边都进了房间", ready)

        if not ready:
            return 1

        # 让房主认领主机位置（房间需要一条"世界"）
        host.claim_host()
        time.sleep(1.5)

        def mkcfg(role):
            cfg = cfgmod.load_config()
            cfg.role = role
            cfg.server_host = "127.0.0.1"
            cfg.server_port = RELAY_PORT
            cfg.room = "refreshroom"
            cfg.mc_host = "127.0.0.1"
            cfg.mc_port = MC_PORT
            cfg.local_port = LOCAL_PORT
            cfg.max_tunnels = 2
            cfg.mode = "relay"
            return cfg

        host.start_session(mkcfg("host"))
        time.sleep(1.5)
        player.start_session(mkcfg("player"))
        time.sleep(2.0)

        # ---- 基线：一次刷新都没做的时候，房间里的数字 ----
        baseline = None

        for _ in range(20):
            time.sleep(0.5)
            state = player.room_state()

            if state is not None and state.world_players:
                baseline = state.world_players
                break

        check(
            "能读到房间里的「世界里的人数」",
            baseline is not None,
            str(baseline),
        )

        if baseline is None:
            return 1

        # ---- 6 轮"刷新列表" ----
        peaks = []
        settles = []

        for index in range(6):
            peak = 0

            with one_refresh() as ok:
                if not ok:
                    check(
                        f"第 {index + 1} 次刷新：Minecraft 真的连上并拿到横幅",
                        False,
                    )
                    break

                # 峰值：**连接活着的时候**应该能看到 +1
                for _ in range(10):
                    time.sleep(0.3)
                    state = player.room_state()

                    if state is not None:
                        peak = max(peak, state.world_players)

            peaks.append(peak)

            # 回落：断开之后必须回到基线
            settled = peak

            for _ in range(24):
                time.sleep(0.5)
                state = player.room_state()

                if state is not None:
                    settled = state.world_players

                    if settled <= baseline:
                        break

            settles.append(settled)
            print(
                f"    第 {index + 1} 次刷新：峰值 {peak}，断开后 {settled}"
                f"（基线 {baseline}）",
                flush=True,
            )

        check(
            "★ 6 次刷新之后，房间人数回落到基线（不是一路上涨）",
            bool(settles) and all(s <= baseline for s in settles),
            f"基线={baseline} 每轮断开后={settles}",
        )
        check(
            "★ 没有出现单调上涨（朋友的症状）",
            settles == sorted(settles, reverse=True) or len(set(settles)) <= 1,
            f"{settles}",
        )
        check(
            "峰值确实动过（说明这几轮刷新真的建了会话，不是没连上）",
            any(p > baseline for p in peaks),
            f"基线={baseline} 峰值={peaks}",
        )
    finally:
        for engine in (player, host):
            with contextlib.suppress(Exception):
                engine.stop_session()

        time.sleep(1.0)

        with contextlib.suppress(Exception):
            relay.terminate()
            relay.wait(timeout=8)

        with contextlib.suppress(Exception):
            relay_log.close()

        stop.set()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
