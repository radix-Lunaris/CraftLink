"""
MC-Link -- 一条打通的通道必须能服务【多次】Minecraft 连接

============================================================================
 这一套为什么必须存在（真实事故）
============================================================================

项目负责人的日志，原文：

    19:56:11 [TUNNEL 01] Minecraft data over the DIRECT link session=468c9874
    19:56:23 [TUNNEL 01] session ended session=468c9874
    19:56:29 [TUNNEL 02] the room punch is not running; using the relay
    19:56:29 [TUNNEL 02] Minecraft data forwarding started session=1e32f7ff

第一条连接走了直连、12 秒后结束（那是 Minecraft 的**服务器列表探测**，游戏
问完就关）；真正的"进入世界"是第二条连接，它掉回了中继 —— 于是整个游戏过程
七兆多字节全部走中继，而界面上写着"已直连"。

根因有三个，都是"一个洞只服务一条流"这个错误假设：

  1. `take_path()` 只给一次，会话结束**不还**；
  2. `ready` 依赖 keepalive 任务，而那个任务是会话协程的子任务，会话一结束
     就被连带取消（连 `asyncio.shield` 都挡不住）→ 现在改成独立线程，和
     `udptunnel.py` 一样；
  3. 流的清理会关掉那个 socket，而 socket 是**房间的资产**，不属于任何一条流。

**v3.0.1 之后这一套的形态变了，盯的东西没变。** 现在一条打通的洞上跑的是
参考工程的 `udptunnel.UdpTunnel`，多条连接靠 **sid** 分用同一条隧道
（`channel.PeerChannel`）—— 这是参考工程自己的答案，也是唯一可行的答案：
他的 `UdpHub` 是"一个 socket、多个隧道"的单一读者，按**对端 IP** 路由，
同一个 IP 下注册两个隧道会被判 ambiguous 全部丢掉。

所以这一套现在盯的是："同一对端、连续两条连接都要走直连，且中继一个字节
都不搬"，而且第二条**不能**因为第一条结束而掉回中继。

Run:
    python tests/direct_reuse_test.py
"""
import os, socket, subprocess, sys, threading, time
sys.path.insert(0, "/home/ai_workspace/CraftLink-V3/client")
from mclink.core import engine as eng_mod
from mclink.config import settings as cfgmod

PORT, STUN, MC_PORT, LOCAL = 19231, 3478, 19232, 19233

def fake_mc():
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", MC_PORT)); srv.listen(8)
    while True:
        try: conn, _ = srv.accept()
        except OSError: return
        def serve(c):
            try:
                c.sendall(b"MC-BANNER\n")
                while True:
                    d = c.recv(4096)
                    if not d: return
                    c.sendall(b"ECHO:" + d)
            except OSError: pass
        threading.Thread(target=serve, args=(conn,), daemon=True).start()
threading.Thread(target=fake_mc, daemon=True).start(); time.sleep(0.3)

env = dict(os.environ)
env.update({"MCLINK_PORT": str(PORT), "MCLINK_DB_ENABLED": "0",
            "MCLINK_REQUIRE_ACCOUNT": "0", "MCLINK_STUN_ENABLED": "1",
            "MCLINK_STUN_BIND": "127.0.0.1", "MCLINK_STUN_PORT": str(STUN),
            "MCLINK_STATUS_FILE": "/tmp/two_status.json",
            "MCLINK_STATUS_INTERVAL": "60"})
log = open("/tmp/two_relay.log", "w")
relay = subprocess.Popen([sys.executable, "-u", "relay_server.py"],
                         cwd="/home/ai_workspace/CraftLink-V3/server", env=env,
                         stdout=log, stderr=subprocess.STDOUT)
for _ in range(80):
    try: socket.create_connection(("127.0.0.1", PORT), 1).close(); break
    except OSError: time.sleep(0.3)

host = eng_mod.MCLinkEngine(); host.start_loop()
player = eng_mod.MCLinkEngine(); player.start_loop()
texts = []
try:
    host.open_room("127.0.0.1", PORT, "h", "h", "", "two", create=True,
                   max_players=2, conn_mode="stun")
    time.sleep(0.4)
    player.open_room("127.0.0.1", PORT, "p", "p", "", "two")
    host.claim_host()
    host.watch_room_nat(True, log=texts.append)
    player.watch_room_nat(True, log=texts.append)

    t0 = time.time()
    while time.time() - t0 < 30:
        time.sleep(0.5)
        h, p = host.room_direct(), player.room_direct()
        if h and h.ready and p and p.ready: break

    def mkcfg(role):
        c = cfgmod.load_config()
        c.role = role
        # 会话握手里的 client_id 必须和进房用的**同一个**（真实客户端只有一个
        # 来源）。中继的 `started=` 按握手 client_id 记，`peers=` 按成员
        # client_id —— 不一致时 Host 侧一个目标都拉不到（§D）。
        c.client_id = "h" if role == "host" else "p"
        c.server_host="127.0.0.1"; c.server_port=PORT
        c.room="two"; c.mc_host="127.0.0.1"; c.mc_port=MC_PORT
        c.local_port=LOCAL; c.max_tunnels=2; c.mode="stun"
        return c

    host.start_session(mkcfg("host")); player.start_session(mkcfg("player"))
    time.sleep(1.5)

    def one_connection(tag):
        mc = socket.create_connection(("127.0.0.1", LOCAL), 8); mc.settimeout(8)
        try:
            b = mc.recv(100); mc.sendall(b"HI-" + tag.encode()); e = mc.recv(200)
        except Exception as exc:
            print("  conn", tag, "TIMEOUT/ERR", exc)
            b = e = b""
        mc.close()
        ok = b"MC-BANNER" in b and e == b"ECHO:HI-" + tag.encode()
        print(f"  conn {tag}: {'OK' if ok else 'FAIL'} banner={b} echo={e}")
        return ok

    print("connection 1 (like a server-list ping):")
    r1 = one_connection("1")
    time.sleep(2)   # let the link be released
    print("connection 2 (the REAL join):")
    r2 = one_connection("2")

    time.sleep(1)
    for e in host.drain_events(1500):
        if e.get("kind") == "log": texts.append("H " + e["text"])
    for e in player.drain_events(1500):
        if e.get("kind") == "log": texts.append("P " + e["text"])
    relay_sessions = [t for t in texts if "forwarding started" in t and "session=" in t]
    print(f"*** conn1={r1} conn2={r2} ***")
    print(f"RELAY SESSIONS CARRYING DATA: {len(relay_sessions)}  (want 0)")
    print("---- path decisions ----")
    for t in [t for t in texts if any(k in t for k in ("DIRECT link", "using the relay", "link established", "stream handshake", "Minecraft data", "PLAYER 1", "PLAYER 2", "wait_for_link"))][-14:]:
        print("   ", t[:140])

    # A suite that cannot fail is not a safety net: this one only PRINTED its
    # verdict, so a regression here would have shipped green. (Seen while
    # replacing the carrier in v3.0.1.)
    ok = bool(r1 and r2 and not relay_sessions)
    print("")
    print(f"RESULT: {'PASS' if ok else 'FAIL'} "
          f"(conn1={r1} conn2={r2} relay_sessions={len(relay_sessions)})")

    if not ok:
        sys.exit(1)
finally:
    host.shutdown(timeout=5); player.shutdown(timeout=5)
    relay.terminate(); log.close()
