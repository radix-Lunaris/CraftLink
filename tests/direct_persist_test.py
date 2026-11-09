"""
CraftLink -- 「洞打通马上就断」的复现与回归（2026-09-28）

============================================================================
 这个缺陷是什么（用户原话）
============================================================================

    用户：「打洞工作流有点混乱，洞打通马上就断」

实测到的现象（真中继 + 两个真引擎，两端都一样）：

    t=0   打通。两端逐目标行 = `P2P 已直连【player】ip:port -`，
          通路 = `P2P 已直连：1/1`
    t=3   同一套状态被自己抹掉：逐目标行 = `确认 Host（0s）【player】N/C`，
          通路 = `直连尝试中... 0/1`，
          日志多出一行 `[direct] new connection cycle for <cid>
          (generation 1 -> 2): walking §E from the first stage again`

根因（file:line 见 docs/PUNCH-FLOW-TREE-20260928.md §6）：

    `room_direct._run()` 每一轮开头都会看 `ConnSession.needs_new_cycle`，
    而它把**终态**（`CONNECTED` **或** `FAILED`）一律当成"要重开一次连接"
    （`punch_flow.py:491-503`）。打通也是终态（`finish(True)` ->
    `Stage.CONNECTED` + Timer 已释放），于是**打通后的下一轮（3 秒后）**
    就对一条活着的连接执行 `restart()`：connected=False、阶段回
    `confirm_host`、batch 归 0、generation+1。

    注意：**socket 没有被关**（`peer_ready` 仍为真，所以不会重打），
    断掉的是"这条连接已建立"的账 —— 也就是用户在界面上看到的那一行。

============================================================================
 这一套怎么钉住它
============================================================================

真中继 + 真 STUN + 两个真引擎 + 一个真 Minecraft TCP 连接：

  1. 静默阶段（没有游戏）：打通后连续观察 N 秒，断言
     stage 一直是 `connected`、generation 不变、flow 一直 `all_up`、
     tunnels_up 一直 >= 1、并且**没有**新的 `new connection cycle` 日志；
  2. 有游戏阶段：真 Minecraft 连接挂上直连后，连续 N 秒每秒做一次
     双向字节往返（echo），断言连接**一直在**、字节**一直在流动**，
     同时上面的状态断言依然成立。

Run:
    python tests/direct_persist_test.py
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

RELAY_PORT = int(os.environ.get("MCLINK_TEST_PERSIST_PORT", "19630"))
STUN_PORT = int(os.environ.get("MCLINK_TEST_PERSIST_STUN", "3478"))
MC_PORT = int(os.environ.get("MCLINK_TEST_PERSIST_MC", "19631"))
LOCAL_PORT = int(os.environ.get("MCLINK_TEST_PERSIST_LOCAL", "19632"))

#: 打通之后连续观察多久（必须 > 2 个 RETRY_INTERVAL_S=3s 的重试轮，
#: 才能覆盖"下一轮就把连接重启掉"这个窗口）。
HOLD_S = 8.0
SAMPLE_S = 0.5

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def fake_minecraft_server(stop):
    """房主本机世界的最小替身：先说一句 banner，然后逐字 echo。"""
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

    while not stop.is_set():
        try:
            conn, _addr = srv.accept()
        except OSError:
            return

        threading.Thread(target=serve, args=(conn,), daemon=True).start()

    with contextlib.suppress(Exception):
        srv.close()


def start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        "MCLINK_STUN_ENABLED": "1",
        "MCLINK_STUN_BIND": "127.0.0.1",
        "MCLINK_STUN_PORT": str(STUN_PORT),
        "MCLINK_STATUS_FILE": os.path.join(HERE, "persist_status.json"),
        "MCLINK_STATUS_INTERVAL": "30",
    })

    log = open(os.path.join(HERE, "persist_relay.log"), "w", encoding="utf-8")

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


def sessions_snapshot(engine):
    """`{peer: (stage, generation, connected, all_up)}` —— 只读，不改状态。"""
    direct = engine.room_direct()

    if direct is None:
        return {}

    flow = direct.flow_summary() or {}

    return {
        peer: (sess.stage.value, sess.generation, bool(sess.connected),
               bool(flow.get("all_up")))
        for peer, sess in direct.sessions.items()
    }


def test_punched_link_survives_the_next_rounds():
    print("")
    print("---- §F/§G：打通之后的每一轮，不许把这条连接 restart() 掉 ----")

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
            check("中继启动", False, "进程已退出，见 tests/persist_relay.log")
            return
        check("中继启动", True)

        host.open_room("127.0.0.1", RELAY_PORT, "h-cid", "host", "", "persist",
                       create=True, max_players=2, conn_mode="stun")
        time.sleep(0.4)
        player.open_room("127.0.0.1", RELAY_PORT, "p-cid", "player", "", "persist")
        host.claim_host()

        def mkcfg(role):
            cfg = cfgmod.load_config()
            cfg.role = role
            cfg.client_id = "h-cid" if role == "host" else "p-cid"
            cfg.server_host = "127.0.0.1"
            cfg.server_port = RELAY_PORT
            cfg.room = "persist"
            cfg.mc_host = "127.0.0.1"
            cfg.mc_port = MC_PORT
            cfg.local_port = LOCAL_PORT
            cfg.max_tunnels = 2
            cfg.mode = "stun"
            return cfg

        host.start_session(mkcfg("host"))
        player.start_session(mkcfg("player"))
        host.watch_room_direct(True, log=texts.append)
        player.watch_room_direct(True, log=texts.append)

        ready = False

        for _ in range(40):
            time.sleep(0.5)
            h, p = host.room_direct(), player.room_direct()

            if h and p and h.ready and p.ready and h.tunnels_up >= 1 and p.tunnels_up >= 1:
                ready = True
                break

        check(
            "★ 点开始连接 -> 打通（两端都 ready，长连接已建好）",
            ready,
            f"host={host.room_direct().describe() if host.room_direct() else None} "
            f"player={player.room_direct().describe() if player.room_direct() else None}",
        )

        if not ready:
            return

        # ============================================================
        #  0) 打通那一刻的"账"：必须记下来，之后一个字都不许变
        # ============================================================
        h0, p0 = sessions_snapshot(host), sessions_snapshot(player)
        cycles0 = sum(1 for t in texts if "new connection cycle" in t)
        linkup0 = sum(1 for t in texts if "ROOM LINK UP" in t)

        check(
            "★ 打通那一刻两端都记成 connected（§G 逐目标行/通路的前提）",
            all(v[0] == "connected" and v[2] and v[3]
                for snap in (h0, p0) for v in snap.values())
            and bool(h0) and bool(p0),
            f"host={h0} player={p0}",
        )

        # ============================================================
        #  0b) 2026-09-29：两端互相上报 `stage=connected`
        # ============================================================
        #
        # 用户报的 P0 是"一端已直连、另一端还在重试"。根治办法是打通的那一侧
        # **立刻**把 `DIRECT_HIT stage=connected`（复用中继本来就逐字转发的关键字）
        # 发给对端，对端在同一个已打通 socket 上收敛。
        #
        # 这一条跑的是真中继 + 真引擎，所以它证明的是**上报通道真的通了**
        # （不是替身）。两侧都打通时，双方的状态机都该收到对端那颗信标。
        seen_connected = False

        for _ in range(20):
            h, p = host.room_direct(), player.room_direct()
            host_seen = [
                s.peer_seen for s in (h.sessions.values() if h else [])
            ]
            player_seen = [
                s.peer_seen for s in (p.sessions.values() if p else [])
            ]

            if all("connected" in seen for seen in host_seen + player_seen) \
                    and bool(host_seen) and bool(player_seen):
                seen_connected = True
                break

            time.sleep(0.3)

        check(
            "★ 两端都收到了对端的 `stage=connected`（本轮新增的收敛信标，真中继）",
            seen_connected,
            f"host={host_seen} player={player_seen}",
        )

        # ============================================================
        #  1) 静默保持：没有游戏，连续 HOLD_S 秒；跨过 2 个重试轮
        # ============================================================
        samples = []

        deadline = time.monotonic() + HOLD_S

        while time.monotonic() < deadline:
            time.sleep(SAMPLE_S)
            h, p = host.room_direct(), player.room_direct()

            samples.append({
                "host": sessions_snapshot(host),
                "player": sessions_snapshot(player),
                "h_tunnels": h.tunnels_up if h else 0,
                "p_tunnels": p.tunnels_up if p else 0,
            })

        def every(pred):
            return all(pred(one) for one in samples)

        def stages_connected(one):
            return all(
                v[0] == "connected" and v[2] and v[3]
                for side in ("host", "player")
                for v in one[side].values()
            ) and bool(one["host"]) and bool(one["player"])

        check(
            f"★ ★ 打通后静默保持 {HOLD_S:.0f}s：两端阶段一直是 connected、"
            f"通路一直 all_up（**不许 restart()**）",
            every(stages_connected),
            next((one for one in samples if not stages_connected(one)), samples[-1] if samples else None),
        )

        def generations_stable(one):
            return (
                all(v[1] == h0[k][1] for k, v in one["host"].items() if k in h0)
                and all(v[1] == p0[k][1] for k, v in one["player"].items() if k in p0)
            )

        check(
            "★ ★ 同一条连接的 generation 不许变（变了 = 一次连接被重开）",
            every(generations_stable),
            f"at_punch host={h0} player={p0} now={samples[-1] if samples else None}",
        )

        check(
            "★ ★ 隧道一直是同一条（tunnels_up>=1，不重打、不重建）",
            every(lambda one: one["h_tunnels"] >= 1 and one["p_tunnels"] >= 1),
            str(samples[-1] if samples else None),
        )

        # ============================================================
        #  2) 有游戏：真 Minecraft 连接挂上直连，连续 HOLD_S 秒逐秒往返
        # ============================================================
        try:
            mc = socket.create_connection(("127.0.0.1", LOCAL_PORT), 10)
        except OSError as e:
            check("假 Minecraft 能连上玩家本地代理", False, str(e))
            return

        mc.settimeout(6)
        banner = mc.recv(200)

        check(
            "★ 游戏 banner 从房主的世界回来（走的就是那条直连）",
            b"MC-BANNER-FROM-HOST" in banner,
            repr(banner),
        )

        flowed = 0
        broke_at = None

        deadline = time.monotonic() + HOLD_S

        while time.monotonic() < deadline:
            time.sleep(SAMPLE_S)
            payload = b"PERSIST-%d" % flowed

            try:
                mc.sendall(payload)
                echoed = mc.recv(400)
            except OSError as e:
                broke_at = f"{e!r}"
                break

            if echoed != b"ECHO:" + payload:
                broke_at = f"echo mismatch {echoed!r}"
                break

            flowed += 1

        with contextlib.suppress(Exception):
            mc.close()

        expected = int(HOLD_S / SAMPLE_S)

        check(
            f"★ ★ 打通后字节持续双向流动 {HOLD_S:.0f}s（{expected} 次往返，一次不许断）",
            broke_at is None and flowed >= expected - 1,
            f"flowed={flowed}/{expected} broke_at={broke_at}",
        )

        h1, p1 = sessions_snapshot(host), sessions_snapshot(player)
        cycles1 = sum(1 for t in texts if "new connection cycle" in t)
        linkup1 = sum(1 for t in texts if "ROOM LINK UP" in t)

        check(
            "★ ★ 游戏连着的时候，两端仍然是 connected / all_up（状态没有被自己抹掉）",
            all(v[0] == "connected" and v[2] and v[3]
                for snap in (h1, p1) for v in snap.values()),
            f"host={h1} player={p1}",
        )

        check(
            "★ ★ 打通之后**没有**任何新的 `new connection cycle`（不重启、不重建）",
            cycles1 == cycles0,
            f"before={cycles0} after={cycles1}: "
            + " | ".join(t for t in texts if "new connection cycle" in t),
        )

        check(
            "★ ★ 打通之后**没有**第二次 ROOM LINK UP（没有重打洞）",
            linkup1 == linkup0,
            f"before={linkup0} after={linkup1}",
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


# ============================================================================
#  离线版：同一个缺陷的确定性复现（不需要中继/端口，几秒钟跑完）
# ============================================================================

def test_run_does_not_restart_a_live_connection():
    print("")
    print("---- 离线复现：_run() 的下一轮不许对一条活着的连接 restart() ----")

    import asyncio

    import mclink.core.direct as direct_mod
    import mclink.core.direct_carry as carry_mod
    from mclink.core import room_direct

    logs = []

    class FakeState:
        members = 2
        client_id = "me-cid"
        is_host = True
        host_claimed = True
        host_id = "me-cid"
        host_name = "me"
        self_name = "me"
        peer_ids = ["p1-cid"]
        started_ids = {"p1-cid"}
        member_names = {"p1-cid": "player"}

    class FakeRoom:
        state = FakeState()

    class FakeEngine:
        def __init__(self):
            self._room = FakeRoom()

        def room_current(self):
            return self._room

        def room_state(self):
            return self._room.state

        def server_host(self):
            return "127.0.0.1"

        def server_port(self):
            return 19100

        def loop(self):
            return None

    class FakeNat:
        per_ip_pool = False
        nat_type = "cone"
        filtering = "apdf"

    class FakeLink:
        def __init__(self, *a, **k):
            self.closed = False
            self.step = None
            self.nat = FakeNat()
            self.inbound_note = "stubbed"
            self.sock = None
            self.punched_sock = None
            self.peer_addr = None
            self.keepalive_alive = False

        async def probe_inbound(self, timeout=4.0):
            return "punchable"

        async def measure(self, force=False):
            return self.nat

        async def advertise(self):
            return ("5.6.7.8", 6000)

        async def attempt(self, timeout=8.0, per_ip_pool=False):
            return type("R", (), {
                "ok": True, "addr": ("5.6.7.8", 6000), "sent": 10,
                "elapsed": 0.1, "plan": "c2c", "reason": "",
            })()

        def start_keepalive(self, interval=5.0):
            self.keepalive_alive = True

        def stop_keepalive(self):
            self.keepalive_alive = False

        @property
        def keepalive_running(self):
            return bool(self.keepalive_alive)

        def close(self):
            self.closed = True
            self.stop_keepalive()

    class FakeCarrier:
        """`_open_tunnel()` 的替身：只回答"隧道已经好了"，绝不拆洞。"""

        def __init__(self, *a, **k):
            self.started = True
            self.stop = threading.Event()
            self._backend = None

        def set_backend(self, host, port):
            self._backend = (host, port)

        async def ensure_started(self, timeout=None):
            return True

        def idle_for(self):
            return 0.0

        def close(self):
            self.stop.set()

    real_link = direct_mod.DirectLink
    real_peer = carry_mod.PeerLink
    real_peer_ref = getattr(room_direct.direct_carry, "PeerLink", None)
    direct_mod.DirectLink = FakeLink
    carry_mod.PeerLink = FakeCarrier
    room_direct.direct_carry.PeerLink = FakeCarrier

    try:
        punch = room_direct.RoomDirectLink(FakeEngine(), log=logs.append)

        async def drive():
            task = asyncio.get_running_loop().create_task(punch._run())

            # 打通
            for _ in range(150):
                await asyncio.sleep(0.02)

                if punch.ready:
                    break

            punched = dict(punch.sessions)
            gen = {k: v.generation for k, v in punched.items()}
            stages = {k: v.stage.value for k, v in punched.items()}
            cycles = [t for t in logs if "new connection cycle" in t]

            # 跨过至少两个重试轮（RETRY_INTERVAL_S = 3s）
            await asyncio.sleep(room_direct.RETRY_INTERVAL_S * 2 + 1.0)

            after_gen = {k: v.generation for k, v in punch.sessions.items()}
            after_stage = {k: v.stage.value for k, v in punch.sessions.items()}
            after_cycles = [t for t in logs if "new connection cycle" in t]

            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

            return (punched, gen, stages, cycles, after_gen, after_stage,
                    after_cycles)

        (punched, gen, stages, cycles, after_gen, after_stage,
         after_cycles) = asyncio.run(drive())

        check(
            "（前提）离线装置里这一条已经打通",
            bool(punched) and all(s == "connected" for s in stages.values()),
            f"stages={stages}",
        )

        check(
            "★ 打通后跨过两个重试轮，阶段仍然是 connected",
            after_stage == stages and all(
                s == "connected" for s in after_stage.values()
            ),
            f"before={stages} after={after_stage}",
        )

        check(
            "★ 打通后跨过两个重试轮，generation 不变（= 没有重开连接）",
            after_gen == gen,
            f"before={gen} after={after_gen}",
        )

        check(
            "★ 打通后不再出现新的 `new connection cycle`",
            len(after_cycles) == len(cycles),
            f"before={len(cycles)} after={len(after_cycles)}: {after_cycles}",
        )

        check(
            "★ 打通后的那一轮没有重打（attempts 仍是 0）",
            punch.attempts == 0,
            str(punch.attempts),
        )
    finally:
        direct_mod.DirectLink = real_link
        carry_mod.PeerLink = real_peer
        room_direct.direct_carry.PeerLink = real_peer_ref


def main():
    print("=" * 64)
    print("CraftLink 打洞保持测试（打通之后不许自己把连接抹掉）")
    print("=" * 64)

    test_run_does_not_restart_a_live_connection()
    test_punched_link_survives_the_next_rounds()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
