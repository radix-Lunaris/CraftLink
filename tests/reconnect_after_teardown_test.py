"""
MC-Link -- 断开/关房/换模式之后**必须还能重新连上**（用户报的"端口占用"）。

============================================================================
 用户报的症状
============================================================================

「房间断开，关闭/换模式/被销毁，再重新连接会导致端口占用还是怎样，总之就是
无法再建立新的连接。」

============================================================================
 审计结论：不是网络问题，是**拆除路径**的两个缺陷
============================================================================

1. `engine.stop_session()` 把拆除时的异常**吞掉了**：

       with contextlib.suppress(Exception):
           self.submit(session.stop(), timeout=timeout)

   一旦 `session.stop()` 超时（玩家侧最多等 6 秒收尾，加上房间级任务和慢中继，
   15 秒是会被撞到的），`submit` 抛"操作超时" → **被 suppress 吞掉** → 旧会话
   的监听 socket 还开着，而引擎已经把 `self._session` 清空、**再也没人持有它**。
   下一次连接就在同一个本地端口上再绑一次 → 端口占用 / WinError 10013。

2. `_rebuild_tunnels()`（换模式）紧接着就 start：

       stop_session(timeout=15)   # 失败被吞
       start_session(...)         # 立刻绑同一个本地端口 → 失败

   用户看到"按新模式重建失败（可手动重新连接）"，而手动再点也还是占用。

这一套就是用户那串操作的可重复版本：**同一个本地端口上反复"断开→立刻重连"、
"关房→重建"、"换模式→重建"**，每一次都必须真的连上，而且停止之后端口必须
真的空出来。
"""

import asyncio
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

RELAY_PORT = int(os.environ.get("MCLINK_TEST_RECON_PORT", "19160"))
MC_PORT = int(os.environ.get("MCLINK_TEST_RECON_MC", "19161"))
LOCAL_PORT = int(os.environ.get("MCLINK_TEST_RECON_LOCAL", "19162"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def port_free(port, host="127.0.0.1"):
    """这个本地端口现在能不能绑？能 = 真的放开了。"""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(Exception):
            sock.close()


def fake_world(stop):
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
        "MCLINK_STATUS_FILE": os.path.join(HERE, "recon_status.json"),
        "MCLINK_STATUS_INTERVAL": "30",
    })

    log = open(os.path.join(HERE, "recon_relay.log"), "w", encoding="utf-8",
               errors="replace")

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


def wait_online(engine, timeout=25):
    deadline = time.time() + timeout

    while time.time() < deadline:
        if engine.snapshot().get("state") == "ONLINE":
            return True
        time.sleep(0.4)

    return False


def main():
    print("=" * 64)
    print("MC-Link 断线/关房/换模式之后还能不能重新连上")
    print("=" * 64)

    stop = threading.Event()
    threading.Thread(target=fake_world, args=(stop,), daemon=True).start()
    time.sleep(0.3)

    relay, relay_log = start_relay()

    host = eng_mod.MCLinkEngine()
    player = eng_mod.MCLinkEngine()
    host.start_loop()
    player.start_loop()

    def mkcfg(role):
        cfg = cfgmod.load_config()
        cfg.role = role
        cfg.server_host = "127.0.0.1"
        cfg.server_port = RELAY_PORT
        cfg.room = "recon"
        cfg.mc_host = "127.0.0.1"
        cfg.mc_port = MC_PORT
        cfg.local_port = LOCAL_PORT
        cfg.max_tunnels = 2
        cfg.mode = "relay"
        return cfg

    def open_room():
        host.open_room("127.0.0.1", RELAY_PORT, "rh-cid", "host", "", "recon",
                       create=True, max_players=4)
        time.sleep(0.3)
        player.open_room("127.0.0.1", RELAY_PORT, "rp-cid", "player", "", "recon")
        host.claim_host()
        time.sleep(0.5)

    try:
        open_room()

        # ---- 0. 基线：连上 ----
        host.start_session(mkcfg("host"))
        time.sleep(1.0)
        player.start_session(mkcfg("player"))
        check("基线：玩家连上（ONLINE）", wait_online(player))

        # ==================================================================
        #  1. 「停掉 → 立刻重连」反复来（就是用户说的"断开再连"）
        # ==================================================================
        rounds = []
        ok_all = True

        for index in range(4):
            player.stop_session(timeout=8)

            # 关键：**立刻**再连，不等、不点第二次
            player.start_session(mkcfg("player"))
            online = wait_online(player, timeout=25)
            rounds.append(online)

            if not online:
                ok_all = False
                break

        check(
            "★ 停掉之后【立刻】重连，连续 4 次都连上",
            ok_all,
            f"每一轮结果={rounds}（最后一次 snapshot="
            f"{player.snapshot().get('state')}）",
        )

        # ==================================================================
        #  2. 停止之后，本地端口必须**真的**空出来
        # ==================================================================
        player.stop_session(timeout=10)
        time.sleep(0.3)

        check(
            "★ 停止之后本地代理端口真的释放了（能自己绑上）",
            port_free(LOCAL_PORT),
            f"{LOCAL_PORT} 仍然绑不上",
        )

        # ==================================================================
        #  3. 房间被【销毁】→ 重新建房 → 再连
        # ==================================================================
        player.start_session(mkcfg("player"))
        check("销毁前玩家在线", wait_online(player))

        for engine in (player, host):
            with contextlib.suppress(Exception):
                engine.stop_session(timeout=10)

        # 房间被销毁 = 成员连接断开、房间从中继消失（模拟管理关房/过期）
        for engine in (player, host):
            with contextlib.suppress(Exception):
                engine.close_room()

        time.sleep(1.5)

        check(
            "房间销毁、双方都停掉之后，本地端口也空了",
            port_free(LOCAL_PORT),
            f"{LOCAL_PORT} 仍被占",
        )

        # 重新建房 + 重连（同一个本地端口）
        host.open_room("127.0.0.1", RELAY_PORT, "rh-cid", "host", "", "recon2",
                       create=True, max_players=4)
        time.sleep(0.3)
        player.open_room("127.0.0.1", RELAY_PORT, "rp-cid", "player", "", "recon2")
        host.claim_host()
        time.sleep(0.5)

        hcfg, pcfg = mkcfg("host"), mkcfg("player")
        hcfg.room = pcfg.room = "recon2"

        host.start_session(hcfg)
        time.sleep(1.0)
        player.start_session(pcfg)

        check(
            "★ 房间销毁后重新建房，玩家还能连上（同一个本地端口）",
            wait_online(player),
            f"state={player.snapshot().get('state')}",
        )

        # ==================================================================
        #  3b. ★ 拆除**超时/卡住**时也必须把端口交出来（吞异常的老 bug）
        # ==================================================================
        #
        # 用户报的现场就是这一条：`stop_session()` 里
        #     with contextlib.suppress(Exception): submit(session.stop(), ...)
        # 超时被吞掉 → 旧监听 socket 还开着、引用却没了 → 下一次连接绑同一个
        # 本地端口直接失败。这里把 stop() 挂住来复现。
        # 先把玩家侧收干净（上一节结束时它还连着），房主留着 —— 房间要靠他活着。
        player.stop_session(timeout=10)

        freed_before = False

        for _ in range(20):
            time.sleep(0.25)

            if port_free(LOCAL_PORT):
                freed_before = True
                break

        check(
            "卡住测试开始前，本地端口是空的（上一节收干净了）",
            freed_before,
            f"{LOCAL_PORT} 已被占",
        )

        if not host.snapshot().get("state") == "ONLINE":
            host.start_session(hcfg)
            time.sleep(1.0)

        player.start_session(pcfg)
        check("卡住测试前玩家在线", wait_online(player))

        from mclink.core import session as session_mod

        real_stop = session_mod.PlayerSession.stop
        hung = threading.Event()

        async def hanging_stop(self):
            # 模拟"收尾卡住"：超过 stop_session 的 timeout，而且不动 socket。
            hung.set()
            await asyncio.sleep(60)

        session_mod.PlayerSession.stop = hanging_stop

        try:
            clean = player.stop_session(timeout=2)
        finally:
            session_mod.PlayerSession.stop = real_stop

        check(
            "★ 拆除卡住时 stop_session 诚实返回 False（不再静默吞掉）",
            clean is False,
            f"返回值={clean}（True 说明超时被吞了）",
        )

        # abort() 是同步关 socket 的兜底：等一小会儿就应该能绑上了
        freed = False

        for _ in range(20):
            time.sleep(0.25)

            if port_free(LOCAL_PORT):
                freed = True
                break

        check(
            "★★ 拆除卡住之后，abort 兜底仍然把本地端口交出来了",
            freed,
            f"{LOCAL_PORT} 仍然绑不上（旧监听 socket 没关）",
        )

        # 而且立刻就能重新连上（用户要的"新连接顺利"）
        player.start_session(pcfg)

        check(
            "★★ 卡住过一次之后，立刻重连仍然成功",
            wait_online(player),
            f"state={player.snapshot().get('state')}",
        )

        # ==================================================================
        #  4. 换模式（direct <-> relay）之后重建
        # ==================================================================
        switched = []

        for mode in ("stun", "relay"):
            with contextlib.suppress(Exception):
                host.set_conn_mode(mode)
            time.sleep(0.8)

            # 换模式在界面上就是"停掉旧连接 → 用新配置重建"
            player.stop_session(timeout=10)
            cfg = mkcfg("player")
            cfg.room = "recon2"
            cfg.mode = mode
            player.start_session(cfg)
            switched.append((mode, wait_online(player)))

        check(
            "★ 换模式之后重建：relay / stun 两种都能连上",
            all(ok for _mode, ok in switched),
            f"{switched}",
        )
    finally:
        for engine in (player, host):
            with contextlib.suppress(Exception):
                engine.stop_session(timeout=8)

        time.sleep(0.5)

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
