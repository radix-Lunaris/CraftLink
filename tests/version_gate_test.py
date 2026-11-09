"""
MC-Link -- 版本门（客户端与服务端必须同版本）。

用户的规则很简单：**版本不是最新不给用** —— 客户端和中继必须是同一个版本，
不一致时弹「需要更新」并且拒绝运行，而不是带着旧字段继续跑出一堆怪状态。

这个套件跑真的中继子进程（内存库），覆盖：

  1. 门开着时，版本不一致的 CREATE / JOIN / HOST_TUNNEL / PLAYER 被拒，
     并且回的是 `ERROR VERSION_MISMATCH server=… client=…`（两边版本都摊开）。
  2. 握手行里**完全没有** ver= 的老客户端同样被拒。
  3. 版本一致时上面四个角色都能过（HOST_TUNNEL 拿到 WAIT 而不是 BAD）。
  4. 只读/账号类（LOGIN / ROOMS）**不拦** —— 否则用户连"去更新"都看不到。
  5. 门关着时（没有 VERSION 文件、没有 MCLINK_VERSION）一切照旧。
  6. 客户端三条握手行真的带了 ver=，而且是**挂在行尾**（位置字段没挪位）。

Run:
    python tests/version_gate_test.py
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_GATE_PORT", "19130"))
MC_PORT = int(os.environ.get("MCLINK_TEST_GATE_MC_PORT", "25594"))

SERVER_VERSION = "9.9.9"
#: 不写死！跟着客户端源码走 —— 写死的话每次升版本号这套都会红，
#: 而它要测的是"握手行带版本、版本不一致才拒"，不是某个具体数字。
CLIENT_VERSION = None   # 由 line_tests() 从 settings.APP_VERSION 读入

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def wait_until(predicate, timeout=20, interval=0.25, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    print(f"  !! timeout waiting for {label}", flush=True)
    return False


async def send_line(port, line, timeout=8):
    """开一条连接、发一行、读一行（读不到就当空串）。"""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    try:
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
        reply = data.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        reply = ""

    return reader, writer, reply


async def close_pair(reader, writer):
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


def start_relay(version, log_name, port):
    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(port),
            "MCLINK_DB_ENABLED": "0",
            "MCLINK_REQUIRE_ACCOUNT": "0",
            "MCLINK_BOOTSTRAP_ADMIN": "admin:adminpass123",
            "MCLINK_STATUS_FILE": os.path.join(HERE, log_name.replace(".log", "_status.json")),
            "MCLINK_STATUS_INTERVAL": "5",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            "MCLINK_VERBOSE": "1",
        }
    )

    if version is None:
        env.pop("MCLINK_VERSION", None)
        env["MCLINK_VERSION"] = ""      # 明确"门关"
    else:
        env["MCLINK_VERSION"] = version

    log = open(os.path.join(HERE, log_name), "w", encoding="utf-8", errors="replace")

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

    return proc, log


def stop_relay_log_flush(log):
    """把中继日志刷到磁盘（读之前必须调用，否则最后几行还在缓冲里）。"""
    with contextlib.suppress(Exception):
        log.flush()
        os.fsync(log.fileno())


def stop_relay(proc, log):
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        log.flush()
        log.close()
    except Exception:
        pass


async def echo_handler(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ============================================================
# 6. 客户端的握手行（静态 + 真解析）
# ============================================================

def line_tests():
    sys.path.insert(0, os.path.join(ROOT, "client"))
    sys.path.insert(0, os.path.join(ROOT, "server"))

    from mclink.config import settings as cfg
    from mclink.core import protocol as proto

    global CLIENT_VERSION

    # 直接采用客户端源码里的版本号：升版本号不该让这套测试变红。
    CLIENT_VERSION = cfg.APP_VERSION

    check(
        "客户端版本号能读到（后面所有握手行都用它）",
        bool(CLIENT_VERSION),
        f"settings={cfg.APP_VERSION}",
    )

    # --- 真解析：把 parse_hello 摘出来单独跑（它不依赖任何运行态） ---
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_gate_relay", os.path.join(ROOT, "server", "relay_server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    room_token = proto.room_token("gatehouse")

    player_line = (
        f"{proto.ROLE_PLAYER} {room_token} cid-1 player-name tok-1 "
        f"ver={cfg.APP_VERSION}"
    )
    role, room, client_id, username, token, err = mod.parse_hello(player_line)

    check(
        "PLAYER 行尾的 ver= 不污染 client_id / username / token",
        (role, client_id, username, token, err) == ("PLAYER", "cid-1", "player-name", "tok-1", None),
        f"{role} {client_id} {username} {token} {err}",
    )

    # HOST_TUNNEL 只有一个必须字段：ver= 是第 3 个，位置解析必须忽略它。
    tunnel_line = f"{proto.ROLE_HOST_TUNNEL} {room_token} ver={cfg.APP_VERSION}"
    role, room, client_id, username, token, err = mod.parse_hello(tunnel_line)

    check(
        "HOST_TUNNEL 行尾的 ver= 不被当成 client_id",
        (role, client_id, err) == ("HOST_TUNNEL", "", None),
        f"{role} client_id={client_id!r} err={err!r}",
    )

    # --- 错误文案：两边版本都要给用户看到 ---
    raw = f"ERROR {proto.ERR_VERSION_MISMATCH} server=9.9.9 client=3.0.7"

    check("识别版本门拒绝", proto.is_version_mismatch(raw), raw)
    check(
        "版本门的两边版本可解析",
        proto.version_mismatch_peers(raw) == ("9.9.9", "3.0.7"),
        str(proto.version_mismatch_peers(raw)),
    )

    text = proto.describe_error(raw)

    check(
        "describe_error 摊开两边版本",
        "9.9.9" in text and "3.0.7" in text and "更新" in text,
        text,
    )
    # 界面拿到的是这段中文（原始 token 已经丢在 describe_error 里了），
    # 所以从文案里也要能把两边版本捞回来 —— 否则弹窗写"服务器版本：未知"。
    check(
        "★ 从中文文案里也能捞回两边版本（弹窗要显示）",
        proto.version_mismatch_peers(text) == ("9.9.9", "3.0.7"),
        str(proto.version_mismatch_peers(text)),
    )
    check(
        "普通错误不会被误判成版本门",
        not proto.is_version_mismatch("ERROR NO_HOST")
        and not proto.is_version_mismatch("服务器没有响应，请稍后再试"),
    )


# ============================================================
# 主流程
# ============================================================

async def gate_on_tests(port):
    """门开着：错的拒、对的过、只读不拦。"""

    # 1. 版本不一致 —— 四个角色全部被拒，而且两边版本都在回复里。
    cases = [
        ("CREATE", f"CREATE __mclink__ cid-1 user-name tok-1 {proto_room()} 20 ver=3.0.7"),
        ("JOIN", f"JOIN __mclink__ cid-1 user-name tok-1 {proto_room()} ver=3.0.7"),
        ("HOST_TUNNEL", f"HOST_TUNNEL {proto_room()} ver=3.0.7"),
        ("PLAYER", f"PLAYER {proto_room()} cid-1 user-name tok-1 ver=3.0.7"),
    ]

    for role, line in cases:
        reader, writer, reply = await send_line(port, line)
        ok = (
            reply.startswith(f"ERROR {'VERSION_MISMATCH'}")
            and f"server={SERVER_VERSION}" in reply
            and "client=3.0.7" in reply
        )
        check(f"门开着：{role} 版本不一致被拒且报出两边版本", ok, reply)

        # 被拒之后连接必须关掉，不能留一个"半活"的 socket。
        try:
            extra = await asyncio.wait_for(reader.readline(), timeout=4)
        except asyncio.TimeoutError:
            extra = b"timeout"

        check(f"门开着：{role} 被拒后连接关闭", extra == b"", repr(extra))
        await close_pair(reader, writer)

    # 2. 老客户端（完全没有 ver=）同样拒。
    reader, writer, reply = await send_line(
        port, f"CREATE __mclink__ cid-1 user-name tok-1 {proto_room()} 20"
    )
    check(
        "门开着：不带 ver= 的老客户端被拒",
        reply.startswith("ERROR VERSION_MISMATCH") and "client=-" in reply,
        reply,
    )
    await close_pair(reader, writer)

    # 3. 版本一致 —— CREATE 真的能建出房间。
    reader, writer, reply = await send_line(
        port,
        f"CREATE __mclink__ cid-1 user-name tok-1 {proto_room()} 20 ver={SERVER_VERSION}",
    )
    # 注意：中继对 CREATE 的回复就是 OK JOIN（建房 = 房主自己进自己的房间）。
    check(
        "门开着：版本一致时 CREATE 通过",
        reply.startswith("OK JOIN") or reply.startswith("OK CREATE"),
        reply,
    )
    await close_pair(reader, writer)

    # HOST_TUNNEL 的正确回复是 WAIT：如果 parse_hello 把 ver= 当成了 client_id
    # 或者版本检查漏了它，这里会变成 BAD_HANDSHAKE / ERROR。
    reader, writer, reply = await send_line(
        port, f"HOST_TUNNEL {proto_room()} ver={SERVER_VERSION}"
    )
    check(
        "门开着：版本一致的 HOST_TUNNEL 拿到 WAIT",
        reply.strip() == "WAIT",
        reply,
    )
    await close_pair(reader, writer)

    # 4. 账号/只读操作不拦（否则"先登录再被告知要更新"这条路也被堵死）。
    reader, writer, reply = await send_line(port, "LOGIN __mclink__ admin adminpass123")
    check("门开着：LOGIN 不受版本门影响", reply.startswith("OK LOGIN"), reply)
    await close_pair(reader, writer)

    reader, writer, reply = await send_line(port, "ROOMS __mclink__")
    check(
        "门开着：ROOMS 不受版本门影响",
        reply.startswith("ROOM") or reply.strip() == "DONE",
        reply,
    )
    await close_pair(reader, writer)


async def real_client_tests():
    """
    ★ 用**真客户端**跑一遍门开着的场景（这一条是补的，因为漏了它）。

    用户原话："我他妈为什么连不上？明明是最新版，怎么还提示版本不对，
    还是自己在日志里看到的，连个弹窗都没有，一直在那边连接。"

    根因：房主的**控制连接**（`HOST <room>`）和 `HOST_TUNNEL` / `PLAYER`
    是分开写的三处，加版本时只改了后两处。版本完全一致的客户端也被判成
    "版本不对"（回复里 `client=-`），而客户端把它当普通连接错误一直重连。

    手写握手行的测试**永远抓不到这个**：它自己拼的行当然是对的。所以这一
    段必须走真客户端（engine → HostCore/PlayerCore），而且服务端版本要和
    客户端**完全一致** —— 也就是说：门开着的时候，真实客户端必须能一路
    ONLINE，中继日志里一条 VERSION REJECT 都不许有。
    """
    print("")
    print("---- 真客户端 + 门开着（版本一致必须一路通）----")

    sys.path.insert(0, os.path.join(ROOT, "client"))

    from mclink.config import settings as cfgmod
    from mclink.core import engine as eng_mod

    # 这一段必须用**和客户端完全相同**的版本起一台中继：上面那些手写行的
    # 测试用的是故意不同的 9.9.9（测"拒"），这里测的是"过"。
    port = RELAY_PORT + 1
    proc, log = start_relay(
        cfgmod.APP_VERSION, "version_gate_match.log", port
    )

    listening = await wait_until(
        lambda: port_open(port), timeout=20, label="matching-version relay"
    )
    check("版本一致的中继起来了", listening)

    if not listening:
        stop_relay(proc, log)
        return

    mc_port = MC_PORT
    local_port = MC_PORT + 1

    # 假世界：发一次横幅就够（这一套不测游戏数据，只测握手能不能过门）
    # 本机世界的角色由 main() 里已有的 echo server 扮演（同一个 MC_PORT）：
    # 这一套只关心"握手会不会被版本门拒"，不测游戏数据。

    host = eng_mod.MCLinkEngine()
    player = eng_mod.MCLinkEngine()
    host.start_loop()
    player.start_loop()

    try:
        state = host.open_room(
            "127.0.0.1", port, "gate-h-cid", "host", "", "gate-real",
            create=True, max_players=4,
        )
        check(
            "真客户端：CREATE 过门（门开着、版本一致）",
            bool(state and state.room),
            str(state),
        )

        cfg = cfgmod.load_config()
        cfg.role = "host"
        cfg.server_host = "127.0.0.1"
        cfg.server_port = port
        cfg.room = "gate-real"
        cfg.mc_host = "127.0.0.1"
        cfg.mc_port = mc_port
        cfg.max_tunnels = 2
        cfg.mode = "relay"

        host.start_session(cfg)

        ok = False
        for _ in range(40):
            await asyncio.sleep(0.5)
            if host.snapshot().get("state") == "ONLINE":
                ok = True
                break

        check(
            "★ 真客户端：房主一路 ONLINE（控制连接 + HOST_TUNNEL 都过了门）",
            ok,
            f"state={host.snapshot().get('state')} "
            f"error={host.snapshot().get('last_error') or host.snapshot().get('reason')}",
        )
        check(
            "★ 真客户端：没有被版本门拒过（version_rejected 是空的）",
            not _core_of(host).version_rejected,
            str(_core_of(host).version_rejected),
        )

        # 玩家侧：PLAYER 握手
        pstate = player.open_room(
            "127.0.0.1", port, "gate-p-cid", "player", "", "gate-real",
        )
        check("真客户端：JOIN 过门", bool(pstate and pstate.room), str(pstate))

        pcfg = cfgmod.load_config()
        pcfg.role = "player"
        pcfg.server_host = "127.0.0.1"
        pcfg.server_port = port
        pcfg.room = "gate-real"
        pcfg.mc_host = "127.0.0.1"
        pcfg.mc_port = mc_port
        pcfg.local_port = local_port
        pcfg.mode = "relay"

        player.start_session(pcfg)
        await asyncio.sleep(2.0)

        check(
            "★ 真客户端：玩家侧没有被版本门拒（PLAYER 握手过门）",
            not _core_of(player).version_rejected,
            str(_core_of(player).version_rejected),
        )

        # 玩家真的连一次本机代理，走一遍 PLAYER 握手
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
            writer.write(b"hi")
            await writer.drain()
            await asyncio.sleep(2.0)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        except OSError as e:
            check("真客户端：玩家本机代理能连上", False, str(e))

        check(
            "★ 真客户端：PLAYER 握手之后仍然没有被版本门拒",
            not _core_of(player).version_rejected,
            str(_core_of(player).version_rejected),
        )

        # ====================================================================
        #  ★★★ 最硬的一条：服务端日志里一条 VERSION REJECT 都不许有 ★★★
        # ====================================================================
        #
        # 为什么非要查日志：客户端只能证明"我自己没被拒"。真正要保证的是
        # **每一条握手行都带着版本**（CREATE / JOIN / HOST 控制连接 /
        # HOST_TUNNEL / PLAYER）。以后谁再加一条握手行（或者再拆一个文件）
        # 忘了带 `ver=`，只会在服务端留下这样一行：
        #
        #   [VERSION REJECT] room=… role=HOST client=- server=3.0.9
        #
        # 客户端侧可能只是"某个连接连不上"，很容易被当成普通网络问题。
        # 这就是 2026-09-25 用户连不上的原因。所以这里直接读服务端日志。
        stop_relay_log_flush(log)

        with open(os.path.join(HERE, "version_gate_match.log"),
                  encoding="utf-8", errors="replace") as fh:
            relay_log = fh.read()

        rejected = [
            line for line in relay_log.splitlines() if "VERSION REJECT" in line
        ]

        check(
            "★★★ 真客户端跑完，服务端日志里没有任何 VERSION REJECT",
            not rejected,
            "；".join(rejected[:5]),
        )
        check(
            "★★★ 服务端没有回过任何 VERSION_MISMATCH",
            "VERSION_MISMATCH" not in relay_log,
            relay_log[-400:] if "VERSION_MISMATCH" in relay_log else "",
        )

        # 上面那条"没有 REJECT"只有在日志真的记了握手时才说明问题，
        # 所以再正面钉一遍：每一种握手行都被服务端看到过，**而且都带 ver=**。
        #
        # 这就是"以后每次更新都不许再拒绝最新版"的机械保证：
        #   * 新加一条握手行忘了带版本  → 这里少一行 HELLO / 少一个 ver=
        #   * 改动把某条握手行拆到别的文件 → 同上
        # 不管哪种，这一套会红，而不是等用户连不上才发现。
        hello_lines = [
            line for line in relay_log.splitlines()
            if " HELLO " in line or line.split("] ", 1)[-1].startswith("HELLO ")
        ]

        for role in ("CREATE", "JOIN", "HOST", "HOST_TUNNEL", "PLAYER"):
            mine = [ln for ln in hello_lines if f"HELLO {role} " in ln]

            check(
                f"★ 服务端真的收到了 HELLO {role}（真客户端的每一条握手行）",
                bool(mine),
                "; ".join(hello_lines)[:200],
            )
            check(
                f"★ HELLO {role} 带着 ver=（版本门要靠它）",
                bool(mine) and all("ver=" in ln for ln in mine),
                "; ".join(mine)[:200],
            )
    finally:
        for engine in (player, host):
            with contextlib.suppress(Exception):
                engine.stop_session()
        await asyncio.sleep(0.5)
        stop_relay(proc, log)   # 里面会 flush + close，日志这时才是完整的


def _core_of(engine):
    """拿到引擎里真正跑着的那个 core（host 或 player），没有就返回空壳。"""
    session = engine.current_session() if hasattr(engine, "current_session") else None
    core = getattr(session, "core", None)

    if core is None:
        class _Empty:
            version_rejected = ""
        return _Empty()

    return core


async def gate_off_tests(port):
    """门关着（本地/测试环境）：一切照旧，不能被版本门拦住。"""
    reader, writer, reply = await send_line(
        port, f"CREATE __mclink__ cid-1 user-name tok-1 {proto_room()} 20"
    )
    check(
        "门关着：没有 ver= 的 CREATE 照常通过",
        reply.startswith("OK JOIN") or reply.startswith("OK CREATE"),
        reply,
    )
    await close_pair(reader, writer)

    reader, writer, reply = await send_line(port, f"HOST_TUNNEL {proto_room()}")
    check("门关着：HOST_TUNNEL 照常 WAIT", reply.strip() == "WAIT", reply)
    await close_pair(reader, writer)


def proto_room():
    """中继只认 room token；用一个固定的 ASCII 记号，别依赖客户端模块。"""
    return "vt-gate-room"


async def main():
    print("=" * 64)
    print("MC-Link 版本门测试（客户端与服务端必须同版本）")
    print("=" * 64)

    line_tests()

    echo_server = await asyncio.start_server(echo_handler, "127.0.0.1", MC_PORT)

    # ---- 门开着 ----
    proc, log = start_relay(SERVER_VERSION, "version_gate_relay.log", RELAY_PORT)

    try:
        ok = await wait_until(
            lambda: port_open(RELAY_PORT), timeout=20, label="relay to listen"
        )
        check("门开着的中继起来了", ok)

        if ok:
            await gate_on_tests(RELAY_PORT)

            # 用户报的那个 bug 就在这一段里：门开着、版本一致、真客户端。
            await real_client_tests()
    finally:
        stop_relay(proc, log)

    await wait_until(
        lambda: not port_open(RELAY_PORT), timeout=10, label="relay to stop"
    )

    # ---- 门关着 ----
    proc, log = start_relay(None, "version_gate_off.log", RELAY_PORT)

    try:
        ok = await wait_until(
            lambda: port_open(RELAY_PORT), timeout=20, label="relay to listen"
        )
        check("门关着的中继起来了", ok)

        if ok:
            await gate_off_tests(RELAY_PORT)
    finally:
        stop_relay(proc, log)

    echo_server.close()

    try:
        await echo_server.wait_closed()
    except Exception:
        pass

    print("-" * 64)
    print(f"passed: {len(PASSED)}  failed: {len(FAILED)}")

    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
