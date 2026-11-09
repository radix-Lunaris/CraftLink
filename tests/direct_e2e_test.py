"""
MC-Link -- 直连端到端测试（点「开始连接」就打洞 → 长连接先于游戏 → 游戏真的走直连）

============================================================================
 这一套为什么必须存在
============================================================================

在此之前，"直连"只有**单点**测试：候选交换、同步起跑、喷洒时刻各自都验证过，
`direct_cand_test` 全绿。但那证明的是"零件是好的"，不是"车能开"。

代价是一次真实的失败：项目负责人的日志里，两边**一条 `[direct]` 都没有**，
隧道全程 idle。原因有两个，都是测试看不见的：

  1. **流程错了**：打洞挂在"隧道进 READY（游戏已连上）"之后，而他的用法是
     "进房就打洞、打通了再挂游戏"。没有游戏 → 打洞永远不启动。
  2. **打了洞没人用**：`reliable.py` 写好了却从来没接进核心，
     所以"直连"只是界面上的一个状态字。

所以这一套盯的是一条完整的链路，而不是任何一个零件：

    进房（STUN）→ 房间级打洞 → 打通 → 挂游戏 → **字节真的从直连过去了**

它用**真中继 + 两个真引擎 + 一个假 Minecraft 服务器**，最后断言游戏侧收到的
banner 和 echo 逐字节正确 —— 也就是"这次真的通了"，而不是"状态字显示 udp"。

Run:
    python tests/direct_e2e_test.py
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

RELAY_PORT = int(os.environ.get("MCLINK_TEST_E2E_PORT", "19197"))
STUN_PORT = int(os.environ.get("MCLINK_TEST_E2E_STUN", "3478"))
MC_PORT = int(os.environ.get("MCLINK_TEST_E2E_MC", "19198"))
LOCAL_PORT = int(os.environ.get("MCLINK_TEST_E2E_LOCAL", "19199"))

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
    srv.listen(4)

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
        "MCLINK_STATUS_FILE": os.path.join(HERE, "e2e_status.json"),
        "MCLINK_STATUS_INTERVAL": "30",
    })

    log = open(os.path.join(HERE, "e2e_relay.log"), "w", encoding="utf-8")

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


def main():
    print("=" * 64)
    print("MC-Link 直连端到端测试（进房打洞 -> 游戏走直连）")
    print("=" * 64)

    stop = threading.Event()
    threading.Thread(
        target=fake_minecraft_server, args=(stop,), daemon=True
    ).start()
    time.sleep(0.3)

    relay, relay_log = start_relay()

    host = eng_mod.MCLinkEngine()
    player = eng_mod.MCLinkEngine()
    host.start_loop()
    player.start_loop()

    texts = []

    try:
        if relay.poll() is not None:
            check("中继启动", False, "进程已退出，见 tests/e2e_relay.log")
            return 1

        check("中继启动", True)

        host.open_room("127.0.0.1", RELAY_PORT, "h-cid", "host", "", "e2e",
                       create=True, max_players=2, conn_mode="stun")
        time.sleep(0.4)
        player.open_room("127.0.0.1", RELAY_PORT, "p-cid", "player", "", "e2e")
        host.claim_host()

        check(
            "★ 建房时选 STUN → 房间就是 STUN",
            host.room_state().conn_mode == "stun",
            str(host.room_state().conn_mode),
        )
        check(
            "★ 玩家进房后也看到 STUN（中继广播）",
            player.room_state().conn_mode == "stun",
            str(player.room_state().conn_mode),
        )

        # ================================================================
        #  3.0.3：还没点「开始连接」→ 不打洞（触发点是按钮，不是进房）
        # ================================================================
        time.sleep(1.0)

        check(
            "★ 还没点开始连接：不打洞（触发点是按钮，不是进房）",
            host.room_direct() is None or not host.room_direct().running,
            str(host.room_direct() and host.room_direct().running),
        )

        # ---- 点「开始连接」（会话起），然后才开始打洞 ----
        # 2026-09-28：会话握手里的 client_id 必须和进房用的**同一个**
        # （真实客户端就是 `client_id_var` 一个来源）。中继的 `started=` 是
        # 按 PLAYER/HOST 握手里的 client_id 记的，而每条控制行的 `to=` 和
        # `peers=` 用的是成员 client_id —— 两个不同的话，Host 侧
        # `started ∩ peers = ∅`，按设计基准 §D 就一个目标都拉不到。
        def mkcfg(role):
            cfg = cfgmod.load_config()
            cfg.role = role
            cfg.client_id = "h-cid" if role == "host" else "p-cid"
            cfg.server_host = "127.0.0.1"
            cfg.server_port = RELAY_PORT
            cfg.room = "e2e"
            cfg.mc_host = "127.0.0.1"
            cfg.mc_port = MC_PORT
            cfg.local_port = LOCAL_PORT
            cfg.max_tunnels = 2
            cfg.mode = "stun"
            return cfg

        host.start_session(mkcfg("host"))
        player.start_session(mkcfg("player"))

        # The GUI does exactly this while a STUN session is running.
        host.watch_room_direct(True, log=texts.append)
        player.watch_room_direct(True, log=texts.append)

        # ---- 等：洞打通 + 长连接隧道建立（此刻没有任何 Minecraft 连接）----
        ready = False
        tunnels = False

        for _ in range(40):
            time.sleep(0.5)
            h, p = host.room_direct(), player.room_direct()

            if h and h.ready and p and p.ready:
                ready = True

            if h and p and h.tunnels_up >= 1 and p.tunnels_up >= 1:
                tunnels = True

            if ready and tunnels:
                break

        check(
            "★ 点开始连接 → 打洞成功（没有任何游戏参与）",
            ready,
            f"host={host.room_direct().describe() if host.room_direct() else None} "
            f"player={player.room_direct().describe() if player.room_direct() else None}",
        )
        check(
            "★ 打通后立刻建好长连接隧道（此刻没有任何游戏连接）",
            tunnels,
            f"host tunnels={host.room_direct().tunnels_up if host.room_direct() else 0} "
            f"player tunnels={player.room_direct().tunnels_up if player.room_direct() else 0}",
        )

        if not (ready and tunnels):
            return 1

        # ====================================================================
        #  对端延迟（玩家 ↔ 主机）：直连隧道自己的 keepalive 往返
        # ====================================================================
        #
        # 这一条**不是新协议**：`UdpTunnel` 本来就每 5 秒发一次 T_PING、对端回
        # T_PONG（那是保活，必须有）。`udptunnel.py` 里只加了"记下发出时刻、
        # 收到应答相减"的只读插桩 —— 包格式、握手步骤、收发路径一步没动。
        # 所以这里拿到的是一个**真实测量出来的**回环 RTT，而不是估算。
        peer_rtt = None
        d = host.room_direct()
        for _ in range(30):          # 最多 15 秒（keepalive 周期 5 秒）
            time.sleep(0.5)
            d = host.room_direct()
            values = [
                row.get("rtt_ms") for row in (d.link_rows() if d else [])
                if row.get("ready") and row.get("rtt_ms") is not None
            ]
            if values:
                peer_rtt = values[0]
                break

        check(
            "★ 已打通的直连链路量出真实的对端延迟（keepalive 往返，非估算）",
            peer_rtt is not None and peer_rtt >= 0.0,
            f"rtt={peer_rtt} rows={d.link_rows() if d else None}",
        )

        # ---- 现在才挂游戏：隧道早已在跑，游戏只是往上挂一条 sid 流 ----

        # ---- 一个假 Minecraft 客户端连到玩家本地代理 ----
        try:
            mc = socket.create_connection(("127.0.0.1", LOCAL_PORT), 8)
        except OSError as e:
            check("假 Minecraft 能连上玩家本地代理", False, str(e))
            return 1

        mc.settimeout(10)
        banner = mc.recv(100)
        mc.sendall(b"HELLO-FROM-MINECRAFT")
        echoed = mc.recv(200)
        mc.close()

        check(
            "★ 游戏数据经【直连】走通了（banner 从房主的世界回来）",
            b"MC-BANNER-FROM-HOST" in banner,
            repr(banner),
        )
        check(
            "★ 双向都通（echo 原样回来）",
            echoed == b"ECHO:HELLO-FROM-MINECRAFT",
            repr(echoed),
        )

        # ---- 游戏断开 ≠ 隧道断开（用户的原话："不是他妈的打通了等有游戏会话"）----
        time.sleep(1.5)

        check(
            "★ 游戏连接断开后，长连接隧道仍然在（寿命跟 host 运行绑定，不跟游戏绑定）",
            host.room_direct().tunnels_up >= 1 and player.room_direct().tunnels_up >= 1,
            f"host={host.room_direct().tunnels_up} player={player.room_direct().tunnels_up}",
        )

        # ====================================================================
        #  朋友的症状：每刷新一次列表就重新建一次隧道 / 人数往上涨
        # ====================================================================
        #
        # 直连这一侧的正确行为是：隧道在点「开始连接」时建好之后**一直是那一条**，
        # 刷新列表（= 又一条 Minecraft 连接）只是往它上面再挂一条 sid 流。
        # 所以这里再刷两次，断言**没有重新打洞**、隧道还是那一条。
        before_attempts = host.room_direct().attempts

        for extra in range(2):
            try:
                again = socket.create_connection(("127.0.0.1", LOCAL_PORT), 8)
                again.settimeout(8)
                again.recv(100)
                again.sendall(b"REFRESH-%d" % extra)
                again.recv(200)
                again.close()
            except OSError as e:
                check(f"第 {extra + 2} 次刷新也能连上本地代理", False, str(e))
                break

            time.sleep(1.5)

        check(
            "★ 刷新列表不会重新打洞（隧道还是原来那一条）",
            host.room_direct().attempts == before_attempts,
            f"before={before_attempts} after={host.room_direct().attempts}",
        )
        check(
            "★ 刷新之后长连接隧道仍在",
            host.room_direct().tunnels_up >= 1
            and player.room_direct().tunnels_up >= 1,
            f"host={host.room_direct().tunnels_up} "
            f"player={player.room_direct().tunnels_up}",
        )

        # ---- 直连确实是"多一条路"，不是"少一条路" ----
        for engine in (host, player):
            for event in engine.drain_events(800):
                if event.get("kind") == "log":
                    texts.append(event["text"])

        check(
            "★ 用的是直连而不是中继（日志明说）",
            any("over the DIRECT link" in t for t in texts),
            " / ".join(t for t in texts if "DIRECT" in t)[:150],
        )
        check(
            "中继隧道照常建立（兜底没有被破坏）",
            any("TUNNEL" in t or "RELAY" in t for t in texts),
            "",
        )
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            host.shutdown(timeout=6)
        with contextlib.suppress(Exception):
            player.shutdown(timeout=6)
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
