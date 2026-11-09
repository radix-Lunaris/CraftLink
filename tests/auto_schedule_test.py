"""
CraftLink -- V5.0 AUTO 调度（Phase 1）测试

============================================================================
 这一套测什么
============================================================================

AUTO 的第一版就两条规矩，两条都必须被钉住：

  1. **房间级策略，能力门**（中继侧）：`CONNMODE auto` 是合法的，但只有报过
     `caps=auto` 的客户端才跑得了它。所以：
       * 房里人人有 auto 能力 -> 切 auto 成功，广播 `mode=auto`；
       * 房里有跑不了 auto 的成员 -> 拒绝切换并点名；
       * auto 房间 -> 不懂 auto 的客户端连门都进不去（`ERROR AUTO_UNSUPPORTED`）。
     测试用的客户端**都带 `ver=`**（和真实新客户端一样），所以它同时穿过了版本门
     —— 这样"没有 caps 的客户端"模拟的是"版本对得上、但能力位没报"的那一类，
     而不是被版本门顺手挡掉的旧版。

  2. **三态选路**（客户端侧）：`config.mode` -> `off / only / prefer`。
       * relay -> off，没有 provider（中继承载一切，v2 行为）；
       * stun  -> only，provider 无限等（等不到就拒绝会话，绝不回落）；
       * auto  -> prefer，provider 最多等 `AUTO_DIRECT_WAIT_S`（等不到就让
                  这条连接走中继）。
     回落**绝不改房间模式**：这一条由"中继侧的模式只能由显式 CONNMODE 改变"
     （既有套件）+ 这里的"provider 不写任何模式"共同保证。

真起中继子进程 + 真 TCP 连接跑第 1 部分；第 2 部分是纯客户端单元（无网络）。

Run:
    python tests/auto_schedule_test.py
"""

import asyncio
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "server"))

from mclink.core import protocol as proto  # noqa: E402

#: The version the test handshakes advertise. The relay is started with the SAME
#: value, so the version gate is ON (unlike the sibling suites that switch it
#: off) and this suite exercises a realistic new-client handshake.
TEST_VERSION = "5.0.0"

RELAY_PORT = int(os.environ.get("MCLINK_TEST_AUTO_PORT", "19186"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        # ★ 版本门**开着**，且和测试握手用的 ver= 一致。
        "MCLINK_VERSION": TEST_VERSION,
        "MCLINK_STUN_ENABLED": "0",
        "MCLINK_STATUS_FILE": os.path.join(HERE, "auto_schedule_status.json"),
        "MCLINK_STATUS_INTERVAL": "999",
    })

    log = open(
        os.path.join(HERE, "auto_schedule_relay.log"), "w",
        encoding="utf-8", errors="replace",
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
            probe = socket.create_connection(("127.0.0.1", RELAY_PORT), 1)
            probe.close()
            return process, log
        except OSError:
            if process.poll() is not None:
                return process, log
            time.sleep(0.3)

    return process, log


# ============================================================
# A member control connection (raw protocol lines)
# ============================================================

class Member:
    def __init__(self, name):
        self.name = name
        self.reader = None
        self.writer = None
        self.lines = asyncio.Queue()
        self.closed_by_server = False

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", RELAY_PORT
        )
        asyncio.create_task(self._reader())
        return self

    async def _reader(self):
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    self.closed_by_server = True
                    break
                await self.lines.put(line.decode(errors="replace").strip())
        except Exception:
            self.closed_by_server = True

    async def send(self, text):
        self.writer.write((text + "\n").encode())
        await self.writer.drain()

    async def expect(self, prefix, timeout=8.0):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(
                    self.lines.get(),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
            except (asyncio.TimeoutError, TimeoutError):
                return None

            if line.upper().startswith(prefix.upper()):
                return line

        return None

    async def expect_state(self, needle, timeout=8.0):
        """
        Wait for a `HOST_STATE` line that CONTAINS `needle`.

        Necessary because HOST_STATE is broadcast on every membership change, so
        the queue already holds the pre-change state (e.g. `mode=relay`) by the
        time the mode switch lands. `expect("HOST_STATE")` would return that
        stale line and "disagree" with a switch that actually worked.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            line = await self.expect(
                "HOST_STATE", timeout=max(0.05, deadline - time.monotonic())
            )

            if line is None:
                return None

            if needle in line:
                return line

        return None

    async def close(self):
        if self.writer is not None:
            self.writer.close()

    async def wait_closed(self, timeout=3.0):
        """The relay closed this connection (FIN observed by our reader)."""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline and not self.closed_by_server:
            await asyncio.sleep(0.05)

        return self.closed_by_server


async def hello(member, role, room, client_id, caps=False):
    """
    Send a modern CREATE/JOIN handshake.

    Always carries `ver=` (so the version gate passes); `caps=auto` only when
    asked for. That is exactly the client this suite cares about: same version,
    with or without the AUTO capability bit.
    """
    line = (
        f"{role} {proto.SENTINEL} {client_id} {client_id} - {room}"
        f" ver={TEST_VERSION}"
    )

    if caps:
        line += f" {proto.caps_token()}"

    await member.send(line)

    return await member.expect("OK JOIN")


def create(member, room, client_id, caps=False):
    return hello(member, proto.ROLE_CREATE, room, client_id, caps=caps)


def join(member, room, client_id, caps=False):
    return hello(member, proto.ROLE_JOIN, room, client_id, caps=caps)


# ============================================================
# 1. Relay: mode gate + capability gate
# ============================================================

async def test_relay_auto():
    print("")
    print("---- 1. 中继：auto 是合法模式，但要人人跑得了 ----")

    # ------------------------------------------------------------
    # 1a. 人人有 caps -> 切 auto 成功，广播 mode=auto
    # ------------------------------------------------------------
    host = await Member("host").connect()
    player = await Member("player").connect()

    try:
        room = "autotest-capable"

        check("房主建房成功（带 caps=auto）",
              bool(await create(host, room, "auto-owner", caps=True)))
        check("玩家进房成功（带 caps=auto）",
              bool(await join(player, room, "auto-player", caps=True)))

        await host.send("CONNMODE auto")
        ack = await host.expect("OK CONNMODE", timeout=6)

        check("★ 房主能把模式切成 auto", ack is not None and "mode=auto" in ack,
              str(ack))

        state = await player.expect_state("mode=auto", timeout=6)

        check("★ 其他成员也收到 mode=auto（显示与实际一致）",
              state is not None and "mode=auto" in state, str(state))

        check("★ 广播带 mode_reason=owner（是 Owner 显式切的）",
              state is not None and "mode_reason=owner" in state, str(state))

        # 可以切回去（热切换不是单向的）
        await host.send("CONNMODE relay")
        back = await host.expect("OK CONNMODE", timeout=6)

        check("★ auto 可以切回 relay（热切换不是单向的）",
              back is not None and "mode=relay" in back, str(back))

        await host.send("CONNMODE auto")
        again = await host.expect("OK CONNMODE", timeout=6)

        check("★ 再切回 auto 也成功（可反复）",
              again is not None and "mode=auto" in again, str(again))
    finally:
        await host.close()
        await player.close()

    # ------------------------------------------------------------
    # 1b. auto 房间：不懂 auto 的客户端进不来
    # ------------------------------------------------------------
    host = await Member("host").connect()
    old = await Member("old-client").connect()

    try:
        room = "autotest-join-guard"

        check("房主建房成功", bool(await create(host, room, "g-owner", caps=True)))

        await host.send("CONNMODE auto")
        check("房主先切成 auto",
              bool(await host.expect("OK CONNMODE", timeout=6)))

        await old.send(
            f"{proto.ROLE_JOIN} {proto.SENTINEL} old-cid old-cid - {room}"
            f" ver={TEST_VERSION}"
        )
        refused = await old.expect("ERROR", timeout=6)

        check("★ 没有 caps 的客户端进 auto 房间被拒（AUTO_UNSUPPORTED）",
              refused is not None and "AUTO_UNSUPPORTED" in refused.upper(),
              str(refused))

        check("★ 被拒之后中继关掉了这条连接（不是放着不管）",
              await old.wait_closed(timeout=3.0),
              f"closed={old.closed_by_server}")
    finally:
        await host.close()
        await old.close()

    # ------------------------------------------------------------
    # 1c. 房里有跑不了 auto 的成员 -> 拒绝切换（房间保持 relay）
    # ------------------------------------------------------------
    host = await Member("host").connect()
    old = await Member("old-client").connect()

    try:
        room = "autotest-switch-guard"

        check("房主建房成功", bool(await create(host, room, "s-owner", caps=True)))
        check("不懂 auto 的成员进 relay 房间是允许的",
              bool(await join(old, room, "s-old", caps=False)))

        await host.send("CONNMODE auto")
        refused = await host.expect("ERROR", timeout=6)

        check("★ 房里有不支持 AUTO 的成员时，切 auto 被拒并点名",
              refused is not None
              and "AUTO_UNSUPPORTED_MEMBER" in refused.upper(),
              str(refused))

        # 房间必须还是 relay：再发一次 relay 应当是 no-op 成功（不是 BAD_MODE），
        # 且广播里的 mode 仍然是 relay。
        await host.send("CONNMODE relay")
        ack = await host.expect("OK CONNMODE", timeout=6)

        check("★ 被拒后房间模式仍是 relay（没有被写坏）",
              ack is not None and "mode=relay" in ack, str(ack))
    finally:
        await host.close()
        await old.close()

    # ------------------------------------------------------------
    # 1d. 不认识的模式仍然是 BAD_MODE（门没有被放宽到"什么都收"）
    # ------------------------------------------------------------
    host = await Member("host").connect()

    try:
        room = "autotest-bad-mode"

        check("建房成功", bool(await create(host, room, "b-owner", caps=True)))

        await host.send("CONNMODE definitely-not-a-mode")
        bad = await host.expect("ERROR BAD_MODE", timeout=6)

        check("★ 不认识的模式仍然是 BAD_MODE",
              bad is not None and "BAD_MODE" in bad.upper(), str(bad))
    finally:
        await host.close()


# ============================================================
# 2. Client: three-state policy (no network)
# ============================================================

class _FakeEngine:
    """Records the timeout the provider asked for and returns None at once."""

    def __init__(self):
        self.timeouts = []

    async def wait_room_direct_path(self, timeout=None):
        self.timeouts.append(timeout)
        return None


def test_client_policy():
    print("")
    print("---- 2. 客户端：三态选路 + 能力位 ----")

    from mclink.config.settings import ClientConfig
    from mclink.core import session as session_mod

    check("★ 协议层的模式集合 = relay/stun/auto",
          proto.CONN_MODES == ("relay", "stun", "auto"), str(proto.CONN_MODES))

    check("★ 客户端广告的能力位是 caps=auto",
          proto.caps_token() == "caps=auto", proto.caps_token())

    mapping = {
        "relay": session_mod.DIRECT_OFF,
        "stun": session_mod.DIRECT_ONLY,
        "auto": session_mod.DIRECT_PREFER,
        "": session_mod.DIRECT_OFF,
        "weird": session_mod.DIRECT_OFF,
    }

    for mode, wanted in mapping.items():
        check(f"策略映射：{mode or '(空)'} -> {wanted}",
              session_mod.direct_policy_for(mode) == wanted,
              session_mod.direct_policy_for(mode))

    def make_session(mode):
        cfg = ClientConfig(mode=mode)

        sess = session_mod.SessionBase(cfg, lambda *a, **k: None)
        engine = _FakeEngine()
        sess.engine_provider = engine

        return sess, engine

    # relay -> 没有 provider（v2 行为，中继承载一切）
    sess, _ = make_session("relay")
    check("★ relay：不装 direct provider（中继照旧承载一切）",
          sess.make_direct_path_provider() is None)

    # stun -> 有 provider，且是**无限等**（timeout=None）
    sess, engine = make_session("stun")
    provider = sess.make_direct_path_provider()

    check("★ stun：装了 direct provider", provider is not None)

    async def run(prov):
        return await prov()

    loop = asyncio.new_event_loop()

    try:
        result = loop.run_until_complete(run(provider))
    finally:
        loop.close()

    check("★ stun：provider 无限等（timeout=None），等不到返回 None",
          engine.timeouts == [None] and result is None,
          f"timeouts={engine.timeouts} result={result!r}")

    # auto -> 有 provider，且**有界等待**（timeout=AUTO_DIRECT_WAIT_S）
    sess, engine = make_session("auto")
    provider = sess.make_direct_path_provider()

    check("★ auto：装了 direct provider", provider is not None)

    loop = asyncio.new_event_loop()

    try:
        result = loop.run_until_complete(run(provider))
    finally:
        loop.close()

    check("★ auto：provider 有界等待（= AUTO_DIRECT_WAIT_S），超时返回 None",
          engine.timeouts == [session_mod.AUTO_DIRECT_WAIT_S] and result is None,
          f"timeouts={engine.timeouts} result={result!r}")

    check("★ auto 的等待上限是个小值（不会把游戏拖到超时）",
          0.0 < session_mod.AUTO_DIRECT_WAIT_S <= 8.0,
          str(session_mod.AUTO_DIRECT_WAIT_S))

    # 两个能力门错误码都要有可读文案（弹窗/日志用）
    for code in (proto.ERR_AUTO_UNSUPPORTED, proto.ERR_AUTO_UNSUPPORTED_MEMBER):
        check(f"错误码 {code} 有中文文案",
              bool(proto.ERROR_TEXT.get(code)), str(proto.ERROR_TEXT.get(code)))


# ============================================================
# 3. Cores: prefer falls through to the relay, only does not
# ============================================================

class _FakeReader:
    def __init__(self, chunks=None):
        self.chunks = list(chunks or [])

    async def read(self, _n):
        if self.chunks:
            return self.chunks.pop(0)

        return b""          # EOF


class _FakeWriter:
    def __init__(self):
        self.buf = b""
        self.closed = False

    def write(self, data):
        self.buf += bytes(data)

    async def drain(self):
        return None

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def _bare_host_core(policy, provider):
    """A HostCore with ONLY the attributes relay_mc touches (no network)."""
    from mclink.core.host_core import HostCore

    core = HostCore.__new__(HostCore)

    core.direct_path_provider = provider
    core.direct_policy = policy
    core.logs = []
    core.log = lambda msg, *a, **k: core.logs.append(str(msg))
    core.live_bytes_to_relay = 0
    core.live_bytes_to_mc = 0
    core.total_bytes_to_relay = 0
    core.total_bytes_to_mc = 0
    core.on_traffic = None

    calls = {"n": 0}

    async def _fake_try(*_a, **_k):
        calls["n"] += 1
        return False        # no direct path available

    core._try_direct_carry = _fake_try

    return core, calls


def _run(coro):
    loop = asyncio.new_event_loop()

    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_core_fallthrough():
    print("")
    print("---- 3. 数据面：prefer 回落中继 / only 拒绝会话 ----")

    payload = b"MCBANNER:AUTO"

    def run_case(policy, provider):
        core, calls = _bare_host_core(policy, provider)

        tunnel_w = _FakeWriter()
        mc_w = _FakeWriter()

        _run(
            core.relay_mc(
                1, "sid-1",
                _FakeReader([payload]), tunnel_w,   # relay -> mc
                _FakeReader([]), mc_w,              # mc -> relay (EOF at once)
            )
        )

        return core, calls, tunnel_w, mc_w

    # (a) prefer + no direct link -> the relay pump carries the bytes.
    core, calls, tunnel_w, mc_w = run_case("prefer", lambda: None)

    check("★ prefer：provider 返回 None 时仍然走中继（字节真的搬过去了）",
          mc_w.buf == payload,
          f"mc={mc_w.buf!r}")
    check("★ prefer：直连确实被尝试过一次（不是直接跳过）",
          calls["n"] == 1, str(calls["n"]))
    check("★ prefer：日志说的是 AUTO 回落，并且真的进了中继泵",
          any("AUTO" in line for line in core.logs)
          and any("forwarding started" in line for line in core.logs),
          repr(core.logs))

    # (b) only (STUN) + no direct link -> the session is refused, NO relay bytes.
    core, calls, tunnel_w, mc_w = run_case("only", lambda: None)

    check("★ only（STUN）：provider 返回 None 时**不搬任何字节**（拒绝会话）",
          mc_w.buf == b"", f"mc={mc_w.buf!r}")
    check("★ only（STUN）：直连被尝试过一次",
          calls["n"] == 1, str(calls["n"]))
    check("★ only（STUN）：**没有**进入中继泵（会话被拒，不是悄悄走中继）",
          not any("forwarding started" in line for line in core.logs),
          repr(core.logs))

    # (c) relay (no provider) -> unchanged v2 behaviour: the relay carries it.
    core, calls, tunnel_w, mc_w = run_case("off", None)

    check("★ relay：没有 provider 时中继照旧承载（v2 行为不变）",
          mc_w.buf == payload and calls["n"] == 0,
          f"mc={mc_w.buf!r} tries={calls['n']}")


# ============================================================
# main
# ============================================================

def main():
    process, log = start_relay()

    check("中继启动（版本门开着，MCLINK_VERSION=%s）" % TEST_VERSION,
          process.poll() is None)

    try:
        asyncio.run(test_relay_auto())
    except Exception as exc:  # noqa: BLE001
        check("AUTO 中继测试没有抛异常", False, f"{type(exc).__name__}: {exc}")

    try:
        test_client_policy()
    except Exception as exc:  # noqa: BLE001
        check("AUTO 客户端单元没有抛异常", False, f"{type(exc).__name__}: {exc}")

    try:
        test_core_fallthrough()
    except Exception as exc:  # noqa: BLE001
        check("AUTO 数据面回落单元没有抛异常", False, f"{type(exc).__name__}: {exc}")

    if process.poll() is None:
        process.terminate()

        try:
            process.wait(timeout=8)
        except Exception:  # noqa: BLE001
            process.kill()

    log.close()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
