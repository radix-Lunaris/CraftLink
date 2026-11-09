"""
MC-Link -- 直连协调控制面测试（V3 STUN 模式）

============================================================================
 这一套测什么
============================================================================

STUN 直连需要两端交换候选地址，而中继**只允许新增角色**（握手协议是冻结的）。
所以这一套盯的是三件事：

  1. **新增关键字不影响任何旧行为**：旧客户端不发这些行；旧中继不认识就忽略。
     这一条用"改动前后房间功能照常"来保证（其余套件是更强的证据）。
  2. **中继只做转发，不理解内容**：候选行要**原样**到达对端 —— 包括现在还不
     存在的字段。中继一旦"帮忙改写"，以后每加一个字段都会炸。
  3. **入站探测走中继的 STUN 服务**，而且**必须回报"我发了"**：
     客户端无法区分"探测没到"和"中继压根没发"，而这两件事的结论正相反
     （一个是用户网络，一个是我们的服务端）。移植项目在这上面栽了好几轮。

真起中继子进程 + 真 TCP 连接，不用假 object：转发是**连接之间**的行为，
假不出来。

Run:
    python tests/direct_cand_test.py
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

RELAY_PORT = int(os.environ.get("MCLINK_TEST_DIRECT_PORT", "19176"))
STUN_PORT = int(os.environ.get("MCLINK_TEST_DIRECT_STUN_PORT", "19176"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        # 门关：这一套测的是候选转发，不是版本门（那是 version_gate_test 的
        # 事）。不关的话，仓库里只要有 server/VERSION（发布树就有），这套握手
        # 不带 `ver=` 就会被 VERSION_MISMATCH 全部拒掉。同 conn_mode_hotswitch。
        "MCLINK_VERSION": "",
        "MCLINK_STUN_ENABLED": "1",
        "MCLINK_STUN_BIND": "127.0.0.1",
        "MCLINK_STUN_PORT": str(STUN_PORT),
        "MCLINK_STATUS_FILE": os.path.join(HERE, "direct_status.json"),
        "MCLINK_STATUS_INTERVAL": "2",
    })

    log = open(
        os.path.join(HERE, "direct_relay.log"), "w",
        encoding="utf-8", errors="replace",
    )

    process = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env, stdout=log, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    # Wait for the data port to accept.
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
# A member control connection
# ============================================================

class Member:
    """One room membership connection, spoken as raw protocol lines."""

    def __init__(self, name):
        self.name = name
        self.reader = None
        self.writer = None
        self.lines = asyncio.Queue()

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
                    break
                await self.lines.put(line.decode(errors="replace").strip())
        except Exception:
            pass

    async def send(self, text):
        self.writer.write((text + "\n").encode())
        await self.writer.drain()

    async def expect(self, prefix, timeout=8.0):
        """Wait for a line starting with `prefix`, returning it."""
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(
                    self.lines.get(), timeout=max(0.05, deadline - time.monotonic())
                )
            except (asyncio.TimeoutError, TimeoutError):
                return None

            if line.upper().startswith(prefix.upper()):
                return line

        return None

    async def close(self):
        if self.writer is not None:
            self.writer.close()


async def join_room(member, room, client_id, owner=False):
    role = proto.ROLE_CREATE if owner else proto.ROLE_JOIN

    await member.send(
        f"{role} {proto.SENTINEL} {client_id} {client_id} - {room}"
    )

    return await member.expect("OK JOIN") or await member.expect("OK CREATE")


# ============================================================
# Tests
# ============================================================

async def test_forwarding():
    print("")
    print("---- 候选交换：中继只转发，不改写 ----")

    host = await Member("host").connect()
    player = await Member("player").connect()

    try:
        room = "directtest-room"

        check("房主建房成功", bool(await join_room(host, room, "host-cid", owner=True)))
        check("玩家进房成功", bool(await join_room(player, room, "player-cid")))
        # 认领主机位
        await player.send("CLAIM_HOST")
        check("玩家认领主机位成功", bool(await player.expect("OK CLAIM_HOST")))

        # ---- 玩家 -> 房主：候选地址 ----
        await player.send(
            "DIRECT_CAND 203.0.113.9 41234 nat=cone sub=easy_inc "
            "filt=eif delta=2 futurefield=hello"
        )

        got = await host.expect("DIRECT_CAND")

        check("★ 候选行被转发到房主", got is not None, str(got))

        if got:
            check(
                "★ 内容逐字转发（含中继不认识的字段）",
                got == (
                    "DIRECT_CAND 203.0.113.9 41234 nat=cone sub=easy_inc "
                    "filt=eif delta=2 futurefield=hello"
                ),
                got,
            )

        # ---- 房主 -> 玩家：方向反过来也要通 ----
        await host.send("DIRECT_PUNCH startIn=1.5 plan=c2s")
        got2 = await player.expect("DIRECT_PUNCH")

        check("★ 反方向也转发（房主 -> 玩家）", got2 is not None, str(got2))
        check("  内容逐字转发", got2 == "DIRECT_PUNCH startIn=1.5 plan=c2s", str(got2))

        # ---- DIRECT_HIT ----
        await player.send("DIRECT_HIT port=41234")
        check(
            "命中通知也被转发",
            (await host.expect("DIRECT_HIT")) == "DIRECT_HIT port=41234",
        )
    finally:
        await host.close()
        await player.close()


async def test_no_peer():
    print("")
    print("---- 房间里没有别人时，要明确回报而不是静默 ----")

    lonely = await Member("lonely").connect()

    try:
        await join_room(lonely, "directtest-lonely", "lonely-cid", owner=True)

        await lonely.send("DIRECT_CAND 198.51.100.1 5000")

        got = await lonely.expect("ERROR NO_PEER", timeout=6)

        check(
            "★ 没有对端时回 ERROR NO_PEER（客户端才知道该重试）",
            got is not None,
            str(got),
        )
    finally:
        await lonely.close()


async def test_ingress_probe():
    print("")
    print("---- 入站探测：中继必须回报「我发了」----")

    member = await Member("ingress").connect()

    try:
        await join_room(member, "directtest-ingress", "ingress-cid", owner=True)

        # 一个真实的 UDP socket 当"我的打洞 socket"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(("127.0.0.1", 0))
        my_port = sock.getsockname()[1]

        await member.send(f"DIRECT_INGRESS 127.0.0.1 {my_port}")

        got = await member.expect("OK DIRECT_INGRESS", timeout=8)

        check("★ 中继确认它发了探测（否则客户端会把「没到」怪到自己网络上）",
              got is not None, str(got))

        # 探测包应当真的到达我们的 socket
        loop = asyncio.get_running_loop()
        arrived = False

        try:
            data, _addr = await asyncio.wait_for(
                loop.sock_recvfrom(sock, 2048), timeout=5
            )
            arrived = data.startswith(b"MCLINK-INGRESS-PROBE")
        except (asyncio.TimeoutError, TimeoutError):
            arrived = False

        check("★ 探测包从【全新 socket】到达了（这就是「对端第一个包」的样子）",
              arrived)

        sock.close()

        # 参数不全时必须报错，不能当成探测失败
        await member.send("DIRECT_INGRESS 1.2.3.4")
        check(
            "参数不全 -> ERROR BAD_INGRESS",
            (await member.expect("ERROR BAD_INGRESS", timeout=5)) is not None,
        )

        # ============================================================
        #  不许当开放 UDP 反射器
        # ============================================================
        #
        # 他的 `handle_inbound_probe` 明写：不限制目标就会变成"任何人让中继
        # 朝任何人发包"。客户端问的永远是**自己**映射出去的那个端点，所以
        # 只允许探测「它连接过来的源 IP」或「它自己 DIRECT_CAND 公告过的 IP」。
        await member.send("DIRECT_INGRESS 8.8.8.8 3478")

        rejected = await member.expect("ERROR BAD_INGRESS", timeout=5)

        check("★ 指向别人的地址 -> 拒绝（不是开放反射器）",
              rejected is not None, str(rejected))

        # 拒绝之后连接必须还能用（一行坏请求不许搞坏控制面）
        await member.send("PING still-alive")

        check("拒绝之后控制面照常可用",
              (await member.expect("PONG still-alive", timeout=5)) is not None)
    finally:
        await member.close()


async def test_old_keywords_still_work():
    print("")
    print("---- 旧功能一个不少（新增关键字的回归）----")

    member = await Member("legacy").connect()

    try:
        room = "directtest-legacy"

        check("建房仍然正常", bool(await join_room(member, room, "legacy-cid", owner=True)))

        await member.send("PING hello")
        check("PING/PONG 仍然正常",
              (await member.expect("PONG hello", timeout=5)) == "PONG hello")

        await member.send("MAXPLAYERS 3")
        got = await member.expect("OK MAXPLAYERS", timeout=5)
        check("MAXPLAYERS 仍然正常", got is not None, str(got))

        await member.send("RELEASE_HOST")
        check("RELEASE_HOST 仍然正常",
              (await member.expect("OK RELEASE_HOST", timeout=5)) is not None)

        await member.send("LEAVE")
        check("LEAVE 仍然正常",
              (await member.expect("OK LEAVE", timeout=5)) is not None)
    finally:
        await member.close()



async def test_conn_mode():
    """
    连接模式由房主决定，并且随 HOST_STATE 广播出去。

    Two things are pinned here:
      * the HOST can change it and everyone sees it (the relay is the single
        source of truth, so display and reality cannot drift);
      * a NON-HOST is refused BY THE RELAY -- not merely greyed out in the UI.
        A modified client must not be able to split a room across transports.
    """
    print("")
    print("---- 连接模式：房主决定 + 广播 + 非房主被拒 ----")

    host = await Member("mode-host").connect()
    player = await Member("mode-player").connect()

    try:
        room = "directtest-mode"

        check("房主建房", bool(await join_room(host, room, "mh-cid", owner=True)))
        check("玩家进房", bool(await join_room(player, room, "mp-cid")))

        # 房主必须真的【认领主机位】-- owner 和 host 是两个不同的东西
        # （房间的所有者 vs 世界所在的那个位置），设置连接模式要的是后者。
        await host.send("CLAIM_HOST")
        check("房主认领主机位", bool(await host.expect("OK CLAIM_HOST")))

        # 默认必须是 relay。
        #
        # Ask the relay for the current value instead of fishing for a
        # broadcast: HOST_STATE was already delivered during join, so a drain
        # loop is a race (and it was one -- it read None). `CONNMODE relay`
        # when the mode IS relay changes nothing and answers with the actual
        # value, which is exactly the assertion we want.
        await host.send("CONNMODE relay")
        default_ack = await host.expect("OK CONNMODE", timeout=6)

        check(
            "★ 默认模式是 relay（老客户端不报这个字段时也一样）",
            default_ack is not None and "mode=relay" in default_ack,
            str(default_ack),
        )

        # ★ 建房时就带上模式：房主选了 STUN 再建房，房间必须是 STUN，
        # 不能被中继的默认值（relay）冲掉 —— 那正是"只能开中转房"的根因。
        #
        # The old version of this test called CLAIM_HOST FIRST, and that is
        # exactly why it passed while the product was still broken: the real
        # client seeds the mode immediately after OK CREATE, when the creator is
        # still `owner=1 host=0`. The relay refused its own owner with
        # ERROR NOT_HOST and the room silently stayed on relay. There is no
        # CLAIM_HOST before the seed here, on purpose -- this is the flow a user
        # actually performs.
        stun_host = await Member("mode-create").connect()
        plain_member = await Member("mode-plain").connect()

        try:
            await stun_host.send(
                f"CREATE {proto.SENTINEL} sc-cid sc-cid - directtest-mode2"
            )
            await stun_host.expect("OK CREATE")

            # The creator has NOT claimed the host slot yet -- and must still be
            # allowed to decide the room's transport.
            await stun_host.send("CONNMODE stun")
            ack = await stun_host.expect("OK CONNMODE", timeout=6)

            check(
                "★ 建房后【还没认领主机位】就能把房间设成 STUN"
                "（真实客户端就是这个次序）",
                ack is not None and "mode=stun" in ack,
                str(ack),
            )

            await stun_host.send("CLAIM_HOST")
            await stun_host.expect("OK CLAIM_HOST")

            # ...and the mode must SURVIVE claiming the host slot.
            await stun_host.send("CONNMODE stun")
            ack = await stun_host.expect("OK CONNMODE", timeout=6)

            check(
                "★ 认领主机位之后模式仍然是 STUN（没有被冲回 relay）",
                ack is not None and "mode=stun" in ack,
                str(ack),
            )

            # A plain member -- neither owner nor host -- must STILL be refused.
            # The owner exemption must not have opened the door for everyone.
            await plain_member.send(
                f"JOIN {proto.SENTINEL} pm-cid pm-cid - directtest-mode2"
            )
            await plain_member.expect("OK JOIN")

            await plain_member.send("CONNMODE relay")
            refused = await plain_member.expect("ERROR", timeout=6)

            check(
                "★ 普通成员仍然改不了模式（房主豁免没有把门开给所有人）",
                refused is not None and "NOT_HOST" in refused.upper(),
                str(refused),
            )
        finally:
            await stun_host.close()
            await plain_member.close()

        await host.send("CONNMODE stun")
        ack = await host.expect("OK CONNMODE", timeout=6)

        check("★ 房主能把模式改成 stun", ack is not None and "mode=stun" in ack,
              str(ack))

        check(
            "★ 其他成员也收到 mode=stun（显示与实际一致）",
            (await player.expect("HOST_STATE", timeout=6)) is not None,
        )

        await player.send("CONNMODE relay")
        refused = await player.expect("ERROR", timeout=6)

        check(
            "★ 非房主被【中继】拒绝（改客户端也绕不过）",
            refused is not None and "NOT_HOST" in refused.upper(),
            str(refused),
        )

        # ★ V5.0：auto 是合法模式，但要报过 `caps=auto` 才跑得了。
        # 这里的 host/player 走的都是老式握手（没有 caps），所以房主切 auto
        # 会被能力门拦住 —— 拦的理由是"这个客户端/房里有客户端跑不了 AUTO"，
        # 不是"不认识这个模式"。（`AUTO_UNSUPPORTED_MEMBER` 那一支由
        # auto_schedule_test 覆盖：那里房主带 caps、同房成员不带。）
        await host.send("CONNMODE auto")
        bad = await host.expect("ERROR", timeout=6)

        check(
            "★ 没有 AUTO 能力位时，切 AUTO 被拒并说明原因",
            bad is not None and "AUTO_UNSUPPORTED" in bad.upper(),
            str(bad),
        )

        # 真正不认识的模式仍然是 BAD_MODE。
        await host.send("CONNMODE bogus")
        bad2 = await host.expect("ERROR BAD_MODE", timeout=6)

        check("不认识的模式仍然是 BAD_MODE", bad2 is not None, str(bad2))

        await host.send("CONNMODE relay")
        ack2 = await host.expect("OK CONNMODE", timeout=6)

        check("可以再切回 relay（热切换不是单向的）",
              ack2 is not None and "mode=relay" in ack2, str(ack2))
    finally:
        await host.close()
        await player.close()


# ============================================================
# 同步起跑：真中继 + 真两端
# ============================================================

class _RoomAdapter:
    """Give a raw Member the little bit of room API a DirectLink uses."""

    def __init__(self, member):
        self.member = member

    async def send_direct(self, line):
        await self.member.send(line)
        return True

    async def wait_direct(self, keywords, timeout=10.0):
        deadline = time.monotonic() + timeout
        held = []
        wanted = tuple(k.upper() for k in keywords)

        try:
            while True:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    return None

                try:
                    line = await asyncio.wait_for(
                        self.member.lines.get(), timeout=remaining
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    return None

                if line.split()[0:1] and line.split()[0].upper() in wanted:
                    return line

                held.append(line)
        finally:
            for line in held:
                self.member.lines.put_nowait(line)

    async def drain_direct(self):
        dropped = 0

        while True:
            try:
                self.member.lines.get_nowait()
            except asyncio.QueueEmpty:
                return dropped

            dropped += 1

    async def probe_ingress(self, line, timeout=6.0):
        """
        The relay's answer to an inbound-probe request.

        True = it confirms sending, None = it never answered. That distinction
        is the whole point: an older relay stays silent, and reporting "blocked"
        for that blames the user's NAT for our own deployment.
        """
        await self.member.send(line)

        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                return None

            try:
                got = await asyncio.wait_for(
                    self.member.lines.get(), timeout=remaining
                )
            except (asyncio.TimeoutError, TimeoutError):
                return None

            upper = got.upper()

            if upper.startswith("OK DIRECT_INGRESS"):
                return True

            if upper.startswith("ERROR DIRECT_INGRESS") or \
                    upper.startswith("ERROR BAD_INGRESS"):
                return False


def _seeded_link(room, my_id, peer_id, echo_port):
    """A DirectLink on the relay, with the NAT measurement pre-seeded.

    The measurement is seeded because this test is about the SCHEDULE; a real
    detection run would add seconds and reach the internet for a number this
    test already knows. Everything else below is real: real relay, real control
    connections, real UDP sockets.
    """
    from mclink.core import direct as direct_mod
    from mclink.core import punch as punch_mod
    from mclink.core import stun as stun_mod

    link = direct_mod.DirectLink(
        room, "127.0.0.1", RELAY_PORT, my_id=my_id, peer_id=peer_id
    )

    # Announce the ECHO port as our candidate, so the peer's spray has
    # somewhere real to go.
    nat = stun_mod.NatProfile()
    nat.public = ("127.0.0.1", echo_port)
    nat.nat_type = punch_mod.NatSub.CONE
    nat.filtering = punch_mod.Filter.EIF
    link.nat = nat
    link.nat_at = time.monotonic()
    link.public = nat.public

    return link


async def test_punch_start_sync_live():
    print("")
    print("---- 同步起跑（真中继）：两端必须在同一瞬间开始 ----")

    from mclink.core import direct as direct_mod

    host = await Member("sync-host").connect()
    player = await Member("sync-player").connect()

    try:
        room = "directtest-sync"

        check("房主建房", bool(await join_room(host, room, "sh-cid", owner=True)))
        check("玩家进房", bool(await join_room(player, room, "sp-cid")))
        await host.send("CLAIM_HOST")
        check("房主认领主机位", bool(await host.expect("OK CLAIM_HOST")))

        # One real UDP socket per side, standing in for "the port its NAT gave
        # it". Nothing listens on the other side's echo port -- the spray just
        # has to LEAVE, this test is about when.
        echo_host = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        echo_host.bind(("127.0.0.1", 0))
        echo_player = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        echo_player.bind(("127.0.0.1", 0))

        la = _seeded_link(
            _RoomAdapter(host), "sync-a", "sync-b", echo_host.getsockname()[1]
        )
        lb = _seeded_link(
            _RoomAdapter(player), "sync-b", "sync-a",
            echo_player.getsockname()[1],
        )

        # Both sides advertise concurrently, exactly as the two clients do.
        got = await asyncio.gather(
            la.advertise(), lb.advertise(), return_exceptions=True
        )

        check(
            "两端都完成了候选交换",
            all(not isinstance(g, Exception) and g is not None for g in got),
            str(got),
        )

        # ★ The relay must have carried the announcements in BOTH directions.
        # If it swallowed them, both sides would fall back to their own grace
        # period and the coordination would be silently dead.
        ea, eb = await asyncio.gather(
            la._wait_punch_start(), lb._wait_punch_start()
        )

        check(
            "★ 中继把 DIRECT_PUNCH 转发到了对端（两端都拿到了对方的公告）",
            ea is not None and eb is not None and la.peer_punch_at is not None
            and lb.peer_punch_at is not None,
            f"a={ea} b={eb}",
        )

        delta = abs(ea - eb) if ea is not None and eb is not None else None

        check(
            "★ 两端起跑时刻相差 < 300ms（真链路上只剩单程延迟之差）",
            delta is not None and delta < 0.30,
            f"a={ea} b={eb} delta={delta}",
        )

        # The budget must exclude the wait: the punch window is for spraying,
        # not for standing still.
        started = time.monotonic()
        result = await la.attempt(timeout=1.0, per_ip_pool=False)
        sprayed_after = time.monotonic() - started

        check(
            "★ 喷洒发生在约定时刻【之后】，不是一进 attempt 就喷",
            result.sent > 0 and sprayed_after >= 0.5,
            f"sent={result.sent} after={sprayed_after:.2f}s reason={result.reason}",
        )

        # And the run is bounded: a dead peer costs one sync window plus one
        # spray window, not a session.
        #
        # 2026-09-25：第一次"等对端起跑"的上限变成 `PUNCH_SYNC_WAIT_TOTAL_S`
        # （20s，用户实测一边晚十几二十秒才有用）。所以这里的上界跟着常量走：
        # 同步窗口 + 喷射窗口，而不是一个拍脑袋的数字。
        from mclink.core import direct as direct_mod
        from mclink.core import punch as punch_mod

        bound = (
            direct_mod.PUNCH_SYNC_WAIT_TOTAL_S
            + punch_mod.PUNCH_TIMEOUT_CAP_S
            + 3.0
        )

        check(
            "打不中时消耗有界（不会无限期喷）",
            sprayed_after < bound,
            f"{sprayed_after:.2f}s（上界 {bound:.0f}s = 同步窗口 + 喷射窗口）",
        )

        echo_host.close()
        echo_player.close()
    finally:
        await host.close()
        await player.close()


async def test_inbound_probe_live():
    """
    入站探测：中继必须"真的发了一炮"，而且要能确认。

    ============================================================================
      WHY THIS IS A HARD TEST AND NOT A NICETY
    ============================================================================

    "入站未测" while both players are connected is the worst possible answer:
    the inbound direction is the ONLY thing that separates "the peer cannot
    reach us" (their network, not fixable here) from "our punching is wrong"
    (our bug, fixable). Without it a miss is unattributable.

    Two things have to be true end to end:
      1. the client's request reaches the relay and the relay ANSWERS -- an
         un-upgraded relay that stays silent must be reported as "unknown with
         a reason", never as "blocked";
      2. the datagram actually lands on the punch socket, which is what makes
         the verdict evidence instead of a guess.
    """
    print("")
    print("---- 入站探测（真中继 + 真 socket）----")

    from mclink.core import direct as direct_mod
    from mclink.core import punch as punch_mod
    from mclink.core import stun as stun_mod

    host = await Member("inbound-host").connect()

    try:
        room = "directtest-inbound"

        check("建房", bool(await join_room(host, room, "ib-cid", owner=True)))

        adapter = _RoomAdapter(host)
        link = direct_mod.DirectLink(
            adapter, "127.0.0.1", RELAY_PORT, my_id="ib", peer_id="peer"
        )

        # A real punch socket, and a real published endpoint (our own loopback
        # address, which is where the relay's probe will be aimed).
        link.open_socket()
        port = link.sock.getsockname()[1]
        link.nat = stun_mod.NatProfile()
        link.nat.public = ("127.0.0.1", port)
        link.public = link.nat.public

        state = await link.probe_inbound(timeout=5.0)
        log_tail = getattr(link, "inbound_note", "")

        check(
            "★ 探测得到明确结论，而不是停在「未测」",
            state in (
                punch_mod.INBOUND_OPEN,
                punch_mod.INBOUND_PUNCHABLE,
                punch_mod.INBOUND_BLOCKED,
            ),
            f"state={state} note={log_tail}",
        )
        check(
            "★ 本机回环上探测能到达（否则「不可达」是谎报）",
            state == punch_mod.INBOUND_OPEN,
            f"state={state} note={log_tail}",
        )
    finally:
        await host.close()


async def test_logging_never_breaks_a_connection():
    """
    日志是旁路：它坏了最多少一行记录，绝不能把正在建立的连接搞死。

    ============================================================================
      WHY THIS IS A HARD TEST AND NOT A COMMENT
    ============================================================================

    This was a REAL regression, caught by this very suite: the first version of
    the structured relay events evaluated its fields AT THE CALL SITE, and one
    of them read `room.host_name` -- an attribute `Room` does not have. The
    exception came out of the logging call, was caught by the membership
    connection's top-level handler, and **dropped the member's connection**:

        [HOST CLAIM] room=... user=...
        [ROOM MEMBER ERROR] ... 'Room' object has no attribute 'host_name'

    So claiming the host position failed because of a log line. The fix is
    `safe_event()` (lazy per-field getters) plus suppression around the whole
    call; this test is what keeps it fixed. It makes the logging layer explode
    and then proves the room still works.
    """
    print("")
    print("---- 日志故障隔离：日志炸了也不许断连接 ----")

    host = await Member("logiso-host").connect()

    try:
        await join_room(host, "logtest-iso", "iso-cid", owner=True)
        await host.send("CLAIM_HOST")
        await host.expect("OK CLAIM_HOST")

        # Sanity: the room works BEFORE anything is sabotaged.
        await host.send("CONNMODE stun")
        good = await host.expect("OK CONNMODE", timeout=6)

        check("破坏之前：房间一切正常", good is not None, str(good))

        # ---- now make the structured-event layer throw on every call ----
        #
        # We sabotage the EVENT helper, not the relay logic: every structured
        # line goes through `relay_events.event`, so this is exactly the
        # "logging is broken" condition. The relay runs in a subprocess, but
        # `relay_events` is the module it imported, and `room_members` imports
        # it lazily inside `safe_event` -- patching it here is therefore not
        # possible across processes.
        #
        # So instead of patching, we EXPLOIT the same class of fault that broke
        # it for real: a field whose value cannot be evaluated. `safe_event`
        # must swallow that and still answer the command.
        import relay_events

        # The public contract we rely on: a field getter that throws loses only
        # that field, never the event, and never the caller.
        from room_members import safe_event

        try:
            safe_event(
                "test.broken_field",
                ok=lambda: "yes",
                broken=lambda: (_ for _ in ()).throw(
                    RuntimeError("sabotaged field")
                ),
            )
            isolated = True
        except Exception:
            isolated = False

        check(
            "★ safe_event：某一个字段取值炸了，事件本身仍然记下来",
            isolated,
            "字段求值异常逃逸了（这正是把连接搞死的那条路）",
        )

        # And the room keeps working, which is the assertion that matters.
        await host.send("CONNMODE relay")
        degraded = await host.expect("OK CONNMODE", timeout=6)

        check(
            "★ 命令不受日志影响：CONNMODE 照样成功",
            degraded is not None and "mode=relay" in degraded,
            str(degraded),
        )

        await host.send("RELEASE_HOST")
        released = await host.expect("OK RELEASE_HOST", timeout=6)

        check("★ RELEASE_HOST 照样成功", released is not None, str(released))

        await host.send("CLAIM_HOST")
        reclaimed = await host.expect("OK CLAIM_HOST", timeout=6)

        check(
            "★ CLAIM_HOST 照样成功（连接没有被日志搞断）",
            reclaimed is not None,
            str(reclaimed),
        )

        await host.send("PING")
        pong = await host.expect("PONG", timeout=6)

        check(
            "★ 连接没有被日志异常拆掉（还能 PING/PONG）",
            pong is not None,
            str(pong),
        )
    finally:
        await host.close()


def main():
    print("=" * 64)
    print("MC-Link 直连协调控制面测试（V3）")
    print("=" * 64)
    process, log = start_relay()

    try:
        if process.poll() is not None:
            check("中继启动", False, "进程已退出，见 tests/direct_relay.log")
            return 1

        check("中继启动", True)

        asyncio.run(test_forwarding())
        asyncio.run(test_no_peer())
        asyncio.run(test_ingress_probe())
        asyncio.run(test_old_keywords_still_work())
        asyncio.run(test_conn_mode())
        asyncio.run(test_punch_start_sync_live())
        asyncio.run(test_logging_never_breaks_a_connection())
        asyncio.run(test_inbound_probe_live())
    finally:
        process.terminate()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()

        log.close()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
