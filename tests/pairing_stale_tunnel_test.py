"""
CraftLink -- 中继配对：**死隧道不许配对给玩家**（现场故障的回归测试）。

============================================================================
 现场到底发生了什么（2026-09-15 20:21，日志为证）
============================================================================

中继日志：

    [20:21:45] room=年宝 host=ONLINE players=1/8 idle_tunnels=26
    [20:21:46] [PLAYER CONNECT] room=年宝 ... username=user2
    [20:21:46] [PAIR] ... session=b621cf75 active=1/8 player=user2
    [20:21:46] [TUNNEL ASSIGNED] room=年宝 session=b621cf75
    [20:21:46] [TUNNEL ERROR] session=b621cf75 error=host tunnel closed before READY
    [20:21:46] [ERROR] session=b621cf75 failed to send START: Connection lost

玩家看到的是 Minecraft 报「你的主机中的软件中止了一个已建立的连接」，
**连着 4 次都是这样**（20:21:46 / 20:21:58 / 20:22:03 / 20:22:26）。

关键数字：房间容量是 **8**，池子里却堆着 **26** 条隧道。多出来的那些是
host 之前几次 stop/restart 留下的、**socket 早就断开**的僵尸隧道。

而 `try_pair()` 以前只检查池子里"有没有"隧道，**不检查"还活着吗"**：
它按 FIFO 挑走第一条 —— 正好是那条死掉的 —— 配对上、告诉玩家 CONNECTED、
然后 `START` 写不出去，整对立刻拆掉。玩家一次都进不去。

============================================================================
 这一套怎么测
============================================================================
真起一个中继子进程（和别的套件一样），在里面直接构造 Room：

  1. 往池子里塞 N 条**已经断开**的隧道（真的建 TCP 再关掉，不是假对象）
  2. 再塞 1 条活着的好隧道
  3. 让一个玩家连进来
  4. 断言：玩家被配到**那条好隧道**上，而不是被死隧道吃掉

除了"修好了"，还断言"没有把它改坏"：池子里全是好隧道时，仍然按 FIFO
配对（最小改动，不改变原有行为）。

Run:
    python tests/pairing_stale_tunnel_test.py
"""

import asyncio
import contextlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")

sys.path.insert(0, SERVER)

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail:
        line += f"  -- {detail}"

    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    (PASSED if ok else FAILED).append(name)


# ============================================================
# 真 TCP 连接对（用来做"活的"和"死掉的"两种隧道）
# ============================================================

class Listener:
    """一个只用来产出真连接的本地监听器。"""

    def __init__(self):
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        # 收下连接就一直挂着（测试结束时统一关）。
        with contextlib.suppress(Exception):
            await reader.read()

    async def stop(self):
        if self.server is not None:
            self.server.close()

            with contextlib.suppress(Exception):
                await self.server.wait_closed()

    async def pair(self):
        """返回一条真连接 (reader, writer)。"""
        return await asyncio.open_connection("127.0.0.1", self.port)


async def dead_pair(listener):
    """
    造一条**真的已经断开**的连接。

    不是假对象：先把两边都关掉，并把事件循环转一下让 EOF 真正送达，
    这样 `writer.is_closing()` 才是真的 True —— 测的就是这个判断。
    """
    reader, writer = await listener.pair()

    writer.close()

    with contextlib.suppress(Exception):
        await writer.wait_closed()

    # 让对端也关掉，并把循环转几圈，确保任何一个 connect_ex 之类的探针
    # 都能看出这条连接没了。
    with contextlib.suppress(Exception):
        probe_reader, probe_writer = await listener.pair()
        probe_writer.close()
        await probe_writer.wait_closed()

    await asyncio.sleep(0.05)

    return reader, writer


# ============================================================
# 场景
# ============================================================

async def scenario():
    import relay_server as rs

    listener = Listener()
    await listener.start()

    try:
        room = rs.Room("staletunnel")

        # 中继里没有数据库（这一套只测配对逻辑本身）。
        rs.store = None

        # ---- 1. 先塞 5 条死隧道（复现现场的僵尸池）----
        dead_waiters = []

        for _ in range(5):
            reader, writer = await dead_pair(listener)
            waiter = rs.Waiter(reader, writer)
            room.waiting_tunnels.append(waiter)
            dead_waiters.append(waiter)

        check(
            "池子里有 5 条已经断开的隧道（现场是 26 条）",
            len(room.waiting_tunnels) == 5,
            f"waiting={len(room.waiting_tunnels)}",
        )

        check(
            "这些隧道确实处于「已关闭」状态（不是假死）",
            all(rs._waiter_closed(w) for w in dead_waiters),
            f"closed={[rs._waiter_closed(w) for w in dead_waiters]}",
        )

        # ---- 2. 再塞 1 条活的好隧道 ----
        live_reader, live_writer = await listener.pair()
        live_waiter = rs.Waiter(live_reader, live_writer)
        room.waiting_tunnels.append(live_waiter)

        check(
            "好隧道被判定为「活着」",
            not rs._waiter_closed(live_waiter),
            f"closed={rs._waiter_closed(live_waiter)}",
        )

        # ---- 3. 玩家连进来 ----
        player_reader, player_writer = await listener.pair()
        player = await room.add_player(player_reader, player_writer)

        check(
            "玩家没有被判成「房间已满」",
            player is not None and player != "full",
            f"player={player!r}",
        )

        await asyncio.sleep(0.3)

        # ---- 4. 玩家必须被配到**那条好隧道**上 ----
        session_ids = list(room.sessions)

        check(
            "玩家被配对成功（现场是永远配不上）",
            len(session_ids) == 1,
            f"sessions={len(session_ids)}",
        )

        if session_ids:
            pair = room.sessions[session_ids[0]]

            check(
                "配对用的是那条【活的】隧道，不是死隧道",
                pair.tunnel_writer is live_writer,
                "配到了死隧道 —— 玩家会立刻被断开",
            )

            check(
                "好隧道已被标记为 assigned（会话已经交给它）",
                live_waiter.session_id == session_ids[0],
                f"session_id={live_waiter.session_id!r}",
            )

        # ---- 5. 死隧道必须被清出池子，不再吃掉下一个玩家 ----
        check(
            "5 条死隧道都被清出池子（不再堆着）",
            len(room.waiting_tunnels) == 0,
            f"still_waiting={len(room.waiting_tunnels)}",
        )

        check(
            "死隧道一条都没被 assign（没被拿去喂玩家）",
            all(w.session_id is None for w in dead_waiters),
            f"assigned={[w.session_id for w in dead_waiters]}",
        )

        # ---- 6. 清理 ----
        with contextlib.suppress(Exception):
            await room.close()

        for _reader, writer in [(live_reader, live_writer)]:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

        with contextlib.suppress(Exception):
            player_writer.close()
            await player_writer.wait_closed()

        # ============================================================
        #  没有把老行为改坏：全是好隧道时，仍然按 FIFO 配对
        # ============================================================
        room2 = rs.Room("fiforoom")

        good = []

        for _ in range(3):
            reader, writer = await listener.pair()
            waiter = rs.Waiter(reader, writer)
            room2.waiting_tunnels.append(waiter)
            good.append((waiter, writer))

        player_reader2, player_writer2 = await listener.pair()
        await room2.add_player(player_reader2, player_writer2)

        await asyncio.sleep(0.3)

        check(
            "全是好隧道时：配的是第一条（FIFO 行为没变）",
            len(room2.sessions) == 1
            and list(room2.sessions.values())[0].tunnel_writer is good[0][1],
            f"sessions={len(room2.sessions)}",
        )

        check(
            "剩下的好隧道还在池子里等下一个玩家（没有被误扔）",
            len(room2.waiting_tunnels) == 2,
            f"waiting={len(room2.waiting_tunnels)}",
        )

        with contextlib.suppress(Exception):
            await room2.close()

        for _waiter, writer in good:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

        with contextlib.suppress(Exception):
            player_writer2.close()
            await player_writer2.wait_closed()

    finally:
        await listener.stop()


def main():
    print("=" * 64)
    print("CraftLink 中继配对：死隧道不许喂给玩家")
    print("=" * 64)

    try:
        asyncio.run(scenario())
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("测试本身不抛异常", False, f"{type(e).__name__}: {e}")

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
