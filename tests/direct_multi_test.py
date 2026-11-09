"""
MC-Link -- 多人直连测试（星形拓扑：host 与【每个】玩家各一条长连接）

============================================================================
 这一套盯的是什么（用户的原文，逐条对应）
============================================================================

  1. "房间内出现多人的时候，每一个玩家都要和 host 建立连接，即使 host 已经
     和其他人建立好了连接" —— 第二个玩家【晚到】也必须和 host 单独打洞；
  2. "不要影响已有连接" —— 新玩家打洞时，老玩家的链路一个包都不该受影响；
  3. "不要只就是两个人连接，后面进来的直接冷落在一边" —— 第三个进来的人
     必须能打洞、能走数据；
  4. "玩家如果断开停止，那么就只断开玩家这个连接，不要影响其他玩家的" ——
     p1 断开，host 只拆 p1 那条（判死看门狗），p2 照常；
  5. "通道长连接的生命是和整合 host 运行绑定，host 断开的时候，通道关闭" ——
     host 停止，所有通道关闭。

和 `direct_e2e_test` 一样：真中继 + 真引擎 + 假 Minecraft 服务端，字节逐字
校验。回环只能证明"机制对"，真实网络仍然只能靠真机 —— 见交接文件。

Run:
    python tests/direct_multi_test.py
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
from mclink.core import room_direct as rd  # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_MULTI_PORT", "19241"))
STUN_PORT = int(os.environ.get("MCLINK_TEST_MULTI_STUN", "3478"))
MC_PORT = int(os.environ.get("MCLINK_TEST_MULTI_MC", "19242"))
LOCAL1 = int(os.environ.get("MCLINK_TEST_MULTI_LOCAL1", "19243"))
LOCAL2 = int(os.environ.get("MCLINK_TEST_MULTI_LOCAL2", "19244"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def fake_minecraft_server(stop):
    """A tiny TCP server standing in for the host's LAN world."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", MC_PORT))
    srv.listen(16)

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
        "MCLINK_STUN_ENABLED": "1",
        "MCLINK_STUN_BIND": "127.0.0.1",
        "MCLINK_STUN_PORT": str(STUN_PORT),
        "MCLINK_STATUS_FILE": os.path.join(HERE, "multi_status.json"),
        "MCLINK_STATUS_INTERVAL": "30",
    })

    log = open(os.path.join(HERE, "multi_relay.log"), "w", encoding="utf-8")

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


def mkcfg(role, local_port, client_id=""):
    cfg = cfgmod.load_config()
    cfg.role = role
    # 会话握手里的 client_id 必须和进房（open_room）用的**同一个** —— 真实
    # 客户端只有一个来源（`client_id_var`）。中继的 `started=` 按 PLAYER/HOST
    # 握手里的 client_id 记，而 `peers=` / `to=` 用成员 client_id；两者不一致
    # 时 Host 侧 `started ∩ peers = ∅`，按 §D 一个目标都拉不到。
    if client_id:
        cfg.client_id = client_id
    cfg.server_host = "127.0.0.1"
    cfg.server_port = RELAY_PORT
    cfg.room = "multi"
    cfg.mc_host = "127.0.0.1"
    cfg.mc_port = MC_PORT
    cfg.local_port = local_port
    cfg.max_tunnels = 3
    cfg.mode = "stun"
    return cfg


def wait_tunnels(engine, want, timeout=25.0):
    """Poll until this engine's long-lived tunnel count reaches `want`."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        direct = engine.room_direct()

        if direct is not None and direct.tunnels_up >= want:
            return True

        time.sleep(0.3)

    return False


def exchange(engine, local_port, tag, timeout=10.0):
    """
    A fake Minecraft client connects to this player's local proxy and expects
    the host world's banner, then an echo. Returns True when byte-exact.
    """
    try:
        mc = socket.create_connection(("127.0.0.1", local_port), 8)
    except OSError as e:
        print(f"  exchange({tag}): connect failed: {e}", flush=True)
        return False

    mc.settimeout(timeout)

    try:
        banner = mc.recv(100)
        mc.sendall(b"HI-" + tag.encode())
        echoed = mc.recv(200)
    except Exception as e:
        print(f"  exchange({tag}): read failed: {e}", flush=True)
        return False
    finally:
        mc.close()

    ok = (b"MC-BANNER-FROM-HOST" in banner
          and echoed == b"ECHO:HI-" + tag.encode())

    if not ok:
        print(f"  exchange({tag}): banner={banner!r} echo={echoed!r}", flush=True)

    return ok


def main():
    print("=" * 64)
    print("MC-Link 多人直连测试（host + 两个玩家，星形）")
    print("=" * 64)

    stop = threading.Event()
    threading.Thread(
        target=fake_minecraft_server, args=(stop,), daemon=True
    ).start()
    time.sleep(0.3)

    relay, relay_log = start_relay()

    host = eng_mod.MCLinkEngine()
    p1 = eng_mod.MCLinkEngine()
    p2 = eng_mod.MCLinkEngine()

    host.start_loop()
    p1.start_loop()
    p2.start_loop()

    try:
        if relay.poll() is not None:
            check("中继启动", False, "进程已退出，见 tests/multi_relay.log")
            return 1

        check("中继启动", True)

        # ---- 房主建房、认领主机、点开始连接 ----
        host.open_room("127.0.0.1", RELAY_PORT, "h-cid", "host", "", "multi",
                       create=True, max_players=3, conn_mode="stun")
        time.sleep(0.4)
        p1.open_room("127.0.0.1", RELAY_PORT, "p1-cid", "p1", "", "multi")
        host.claim_host()

        check("三人上限的房间、STUN 模式",
              host.room_state().conn_mode == "stun",
              str(host.room_state().conn_mode))

        host.start_session(mkcfg("host", LOCAL1, "h-cid"))
        host.watch_room_direct(True, log=lambda *_a: None)

        # ================================================================
        #  房主【先】点开始连接，等了 4 秒对方才点 —— 用户原话：
        #  "点击开始连接的那一瞬间，就开始尝试等待另一个人进来以后，
        # ================================================================
        #  房主【先】点开始连接，等了 4 秒对方才点
        # ================================================================
        #
        # ★ 2026-09-28（设计基准 §D / 用户 BUG 4，**这条断言被推翻了**）
        #
        # 旧断言是"房主一点开始连接就立刻在尝试（哪怕对方还没开始）"——那正是
        # 用户报的 BUG 4："玩家未点开始连接，host 似乎已经在对他打洞"。
        # 基线现在的规则是：Player 每轮只向当前 Host 打；Host 每轮只向
        # **已经开始连接**的 Player 打（中继 `started=`，PROTOCOL §8.1）。
        # 所以房主先点、房里没人开始时：**一个目标都没有 -> attempts == 0**。
        time.sleep(4.0)

        check("★ 房主先点开始连接、没人开始时：不许打洞（BUG 4 的修复点）",
              host.room_direct() is not None and host.room_direct().attempts == 0,
              f"attempts={host.room_direct().attempts} "
              f"reason={host.room_direct().target_reason}")

        p1.start_session(mkcfg("player", LOCAL1, "p1-cid"))
        p1.watch_room_direct(True, log=lambda *_a: None)

        check("★ host 与 p1 各自的长连接隧道建立（点开始连接后，游戏还没连）",
              wait_tunnels(host, 1) and wait_tunnels(p1, 1),
              f"host={host.room_direct().tunnels_up} p1={p1.room_direct().tunnels_up} "
              f"host_reason={host.room_direct().target_reason} "
              f"host_peers={host.room_state().peer_ids} "
              f"started={host.room_state().started_ids}")

        # 入站检测：点开始连接后就测，且必须测出结论（不再是"未测"）。
        ib = host.room_direct().inbound_state if host.room_direct() else ""
        check("★ 入站检测在点开始连接后就有结论（不是未测）",
              ib in ("open", "punchable", "blocked"),
              f"inbound_state={ib!r}")

        check("★ p1 的数据走通（banner + echo）",
              exchange(p1, LOCAL1, "P1"),
              "")

        # ---- 第二个玩家【晚到】：单独和 host 打洞，不影响 p1 ----
        p2.open_room("127.0.0.1", RELAY_PORT, "p2-cid", "p2", "", "multi")
        p2.start_session(mkcfg("player", LOCAL2, "p2-cid"))
        p2.watch_room_direct(True, log=lambda *_a: None)

        check("★ 晚到的 p2 也拿到自己的一条长连接（不被冷落）",
              wait_tunnels(host, 2) and wait_tunnels(p2, 1),
              f"host={host.room_direct().tunnels_up} p2={p2.room_direct().tunnels_up}")

        check("★ p2 打洞期间，p1 的链路仍然能用（不影响已有连接）",
              exchange(p1, LOCAL1, "P1B"),
              "")

        check("★ p2 的数据也走通",
              exchange(p2, LOCAL2, "P2"),
              "")

        # ---- p1 断开：只断 p1 那一条，p2 照常 ----
        p1.stop_session()
        p1.watch_room_direct(False)

        # 判死看门狗（LINK_DEAD_S=20s）对测试太长：压到 6 秒，让真实的那段
        # "静默判死"逻辑自己跑完。注意必须【大于】隧道保活间隔 5 秒 —— 否则
        # 两次保活之间活着的链路也会被判死（实测踩过：压到 1 秒，全杀）。
        old_dead = rd.LINK_DEAD_S
        rd.LINK_DEAD_S = 6.0

        try:
            deadline = time.time() + 14

            while time.time() < deadline:
                direct = host.room_direct()

                if direct is not None and direct.tunnels_up == 1:
                    break

                time.sleep(0.3)

            check("★ p1 断开后 host 只拆了 p1 那一条（判死看门狗），p2 的还在",
                  host.room_direct() is not None
                  and host.room_direct().tunnels_up == 1,
                  f"host tunnels={host.room_direct().tunnels_up}")
        finally:
            rd.LINK_DEAD_S = old_dead

        check("★ p1 断开后 p2 照常走数据（不受影响）",
              exchange(p2, LOCAL2, "P2B"),
              "")

        # ---- host 断开：通道全关 ----
        host.stop_session()
        host.watch_room_direct(False)

        check("★ host 断开 → 通道全关",
              host.room_direct() is not None
              and host.room_direct().tunnels_up == 0
              and not host.room_direct().links,
              f"tunnels={host.room_direct().tunnels_up} links={list(host.room_direct().links)}")

        p2.stop_session()
        p2.watch_room_direct(False)

        check("★ p2 自己也关了（玩家侧停止只关自己）",
              p2.room_direct() is not None and p2.room_direct().tunnels_up == 0,
              f"tunnels={p2.room_direct().tunnels_up}")

        # ---- 全程没有一条中继会话搬过游戏数据（STUN 模式铁律）----
        relay_bytes = 0

        for engine in (host, p1, p2):
            for event in engine.drain_events(800):
                if event.get("kind") != "log":
                    continue

                text = event["text"]

                if "forwarding started" in text:
                    relay_bytes += 1

        check("★ STUN 模式下中继一个字节的游戏数据都没搬",
              relay_bytes == 0,
              f"{relay_bytes} sessions")
    finally:
        stop.set()

        for engine in (host, p1, p2):
            with contextlib.suppress(Exception):
                engine.shutdown(timeout=6)

        with contextlib.suppress(Exception):
            relay.terminate()
            relay.wait(timeout=10)

        relay_log.close()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
