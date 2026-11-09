"""
CraftLink —— 打洞"六个隐患"的回归网（2026-09-28，用户逐条批准修复）。

============================================================================
 这一套为什么存在
============================================================================

上一轮（`docs/PUNCH-FLOW-TREE-20260928.md` §6 + `tests/direct_persist_test.py`）
只修了「洞打通马上就断」的**一个生产调用点**。用户随后批准把当时**刻意留着**
的六个隐患一起修掉，每一个都必须有回归测试钉住：

  1. `needs_new_cycle` 把两个终态混为一谈（`punch_flow.py`）：
     成功（CONNECTED）也是终态 -> 下一轮把它 `restart()` 回 §E 第一步。
     修法：分清"成功终态（保持连接）"与"失败终态（重开一轮）"，并给调用方
     一个无歧义的 API（`ConnSession.cycle_action(link_alive)`）。
  2. `RoomDirectLink.start()` 的幂等闸门（`room_direct.py`）：
     `transport_ready` 只看 `link/peer_addr` 引用在不在 -> 停止之后洞死了也
     永远挡住重打。修法：`transport_ready` 只认"链路真的活着"
     （keepalive / `carried` / 隧道）。
  3. `_sweep_links` 拿**打洞目标表**当花名册：`started=` 抖一下就把活着的洞
     当"对端走了"拆掉。修法：判据改成中继花名册 `RoomState.peer_ids`。
  4. `_open_tunnel` 两次 HELLO 失败就把**已经打通的 socket** 扔掉重打：
     新 socket = 新 NAT 映射 = 又一次"打通又断"。修法：同一个 socket 上有界
     重试 + 退避，只有 socket 真的没了才重打。
  5. `_sweep_links` 在 `carrier is None or not carrier.started` 时永远不判死：
     隧道一直没起来的洞只要保活线程活着就再也不会重打。修法：给一条真正的
     截止时间（`TUNNEL_SETUP_DEADLINE_S`）。
  6. `flow_summary` 在读路径上写状态（给不在 `sessions` 里的 link 现建
     `_session()`，界面每刷新一次就多一条 / 改 generation）。修法：summary
     改成纯函数（只读、只汇总）。

Run:
    python tests/direct_hardening_test.py
"""

import asyncio
import contextlib
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import punch_flow as pf  # noqa: E402
from mclink.core import room_direct as rd  # noqa: E402
import mclink.core.direct as direct_mod  # noqa: E402
import mclink.core.direct_carry as carry_mod  # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================================
#  装置：假房间 / 假引擎 / 假打洞链路 / 假承载
# ============================================================================

class FakeState:
    """`RoomState` 的最小替身（字段名与中继广播解析出来的完全一致）。"""

    def __init__(self, client_id="h-cid", peer_ids=("p1-cid",), members=2,
                 is_host=True, started_ids=None, host_id="h-cid",
                 host_name="host", self_name="host", member_names=None):
        self.client_id = client_id
        self.peer_ids = list(peer_ids) if peer_ids is not None else None
        self.members = members
        self.is_host = is_host
        self.started_ids = started_ids
        self.host_claimed = True
        self.host_id = host_id
        self.host_name = host_name
        self.self_name = self_name
        self.member_names = member_names or {
            p: p for p in (peer_ids or ())
        }


class FakeRoom:
    """`room_current()` 的对象；没有 `peek_direct` = 没有协调行可看。"""

    def __init__(self):
        self.state = None


class FakeEngine:
    def __init__(self, state, room=None):
        self._state = state
        self._room = room if room is not None else FakeRoom()

    def room_state(self):
        return self._state

    def room_current(self):
        return self._room

    def server_host(self):
        return "127.0.0.1"

    def server_port(self):
        return 19100

    def loop(self):
        # 测试都跑在真事件循环里；`start()` 需要它（`run_coroutine_threadsafe`）。
        return asyncio.get_running_loop()

    def room_nat_profile(self):
        return None


class FakeNat:
    per_ip_pool = False
    nat_type = "cone"
    filtering = "apdf"


class FakeSock:
    """socket 替身：`_link_socket_dead` 只看"引用还在不在"，不会真的发包。"""

    def close(self):
        pass


class FakeLink:
    """`direct.DirectLink` 的替身：真打洞引擎不认识网络，这里只回答案。"""

    def __init__(self, *a, **k):
        self.closed = False
        self.step = None
        self.room = None
        self.nat = FakeNat()
        self.inbound_note = "stubbed"
        self.sock = FakeSock()
        self.punched_sock = None
        self.peer_addr = ("5.6.7.8", 6000)
        self.peer_nat = "cone"
        self.peer_filter = "eif"
        self.public = ("5.6.7.8", 6000)
        self.keepalive_alive = False
        self.stun_port = 0
        self.flow_attempt = ""
        self.flow_batch = 0
        self.punch_calls = 0

    async def probe_inbound(self, timeout=4.0):
        return "punchable"

    async def measure(self, force=False):
        return self.nat

    async def advertise(self):
        return ("5.6.7.8", 6000)

    async def attempt(self, timeout=8.0, per_ip_pool=False):
        self.punch_calls += 1

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
        self.sock = None
        self.punched_sock = None


class FakeCarrier:
    """
    `direct_carry.PeerLink` 的替身。

    `next_results` 是"下一次 new 出来的 carrier 的行为"：一个 True/False 列表，
    按次返回 `ensure_started()`。它让"握手失败两次再成功"这种时序变成确定的。
    """

    next_results = None
    instances = []

    def __init__(self, peer_id, punch_link, log=None, peer_addr=None):
        self.peer_id = peer_id
        self.punch_link = punch_link
        self.log = log or (lambda *_a: None)
        self.stop = threading.Event()
        self.started = False
        self.calls = 0
        self._backend = None
        self._results = list(FakeCarrier.next_results or ())
        FakeCarrier.next_results = None
        # 记下这一条 carrier 拿到的 socket（被"重打"等于换 socket）。
        self.sock = (
            getattr(punch_link, "punched_sock", None) or punch_link.sock
        )
        FakeCarrier.instances.append(self)

    def set_backend(self, host, port):
        self._backend = (host, int(port))

    async def ensure_started(self, timeout=None):
        self.calls += 1
        ok = bool(self._results.pop(0)) if self._results else True

        if ok:
            self.started = True

        return ok

    def idle_for(self):
        return 0.0

    def close(self):
        self.stop.set()
        self.started = False


@contextlib.contextmanager
def patched_fakes():
    """把真 DirectLink / PeerLink 换成替身（测试期间）。"""
    real_link = direct_mod.DirectLink
    real_peer = carry_mod.PeerLink
    direct_mod.DirectLink = FakeLink
    carry_mod.PeerLink = FakeCarrier
    try:
        yield
    finally:
        direct_mod.DirectLink = real_link
        carry_mod.PeerLink = real_peer


def build(peer_ids=("p1-cid",), started_ids=None, is_host=True, members=2,
          client_id="h-cid"):
    state = FakeState(
        client_id=client_id, peer_ids=peer_ids, members=members,
        is_host=is_host, started_ids=started_ids,
    )
    logs = []
    punch = rd.RoomDirectLink(FakeEngine(state), log=logs.append)

    return punch, state, logs


async def wait_until(pred, timeout=5.0, step=0.02):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if pred():
            return True

        await asyncio.sleep(step)

    return bool(pred())


def count(logs, needle):
    return sum(1 for line in logs if needle in line)


# ============================================================================
#  1. needs_new_cycle 不许把"成功终态"当成"要重开一次连接"
# ============================================================================

def test_success_terminal_is_not_a_new_cycle():
    print("")
    print("---- 隐患 1：成功（CONNECTED）不是「要重开」，失败/停止才是 ----")

    sess = pf.ConnSession("p1-cid", role="host", label="players1")

    check(
        "（前提）新连接停在 idle：要开一轮（restart），不是 start",
        sess.stage == pf.Stage.IDLE
        and sess.needs_new_cycle is True
        and sess.cycle_action(link_alive=False) == pf.CYCLE_RESTART,
        f"stage={sess.stage} needs={sess.needs_new_cycle}",
    )

    sess.start()

    for stage in (
        pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND,
        pf.Stage.REPORT, pf.Stage.SYNC_START, pf.Stage.CANDIDATE,
        pf.Stage.CONFIRM_CAND, pf.Stage.SYNC_GO, pf.Stage.PUNCH,
    ):
        sess.advance(stage)

    sess.finish(True, latency_ms=18.0, addr="1.2.3.4:6000")

    check(
        "★ ★ 打通（成功终态）**不是** needs_new_cycle（否则界面会被自己抹掉）",
        sess.success_terminal is True
        and sess.terminal is True
        and sess.needs_new_cycle is False,
        f"succ={sess.success_terminal} term={sess.terminal} "
        f"needs={sess.needs_new_cycle} timer_released={sess.timer.released}",
    )

    check(
        "★ ★ 洞还活着 -> cycle_action == keep（保持连接，绝不 restart）",
        sess.cycle_action(link_alive=True) == pf.CYCLE_KEEP,
        sess.cycle_action(link_alive=True),
    )

    check(
        "★ 洞真的死了 -> cycle_action == restart（§F 的作废→重新同步→再打）",
        sess.cycle_action(link_alive=False) == pf.CYCLE_RESTART,
        sess.cycle_action(link_alive=False),
    )

    # 失败终态：下一次必须重开。
    bad = pf.ConnSession("p2-cid", role="host")
    bad.start()
    bad.finish(False, reason="boom")

    check(
        "★ 失败终态 -> needs_new_cycle 为真、cycle_action == restart（与成功相反）",
        bad.failure_terminal is True
        and bad.success_terminal is False
        and bad.needs_new_cycle is True
        and bad.cycle_action(link_alive=True) == pf.CYCLE_RESTART,
        f"failed={bad.failure_terminal} needs={bad.needs_new_cycle}",
    )

    # 手动停止（表已释放、阶段在中间）：下一次也是全新一次连接。
    manual = pf.ConnSession("p3-cid", role="player")
    manual.start()
    manual.advance(pf.Stage.CONFIRM_TARGET)
    manual.advance(pf.Stage.NAT)
    manual.timer.finish()

    check(
        "★ 手动停止（表已释放）-> needs_new_cycle 仍为真（用户第 3 条不许回归）",
        manual.needs_new_cycle is True
        and manual.cycle_action(link_alive=False) == pf.CYCLE_RESTART,
        f"stage={manual.stage} released={manual.timer.released}",
    )

    check(
        "★ 两个终态集合是分开的，合起来才是 TERMINAL_STAGES",
        pf.SUCCESS_STAGES == frozenset({pf.Stage.CONNECTED})
        and pf.FAILURE_STAGES == frozenset({pf.Stage.FAILED})
        and pf.TERMINAL_STAGES == pf.SUCCESS_STAGES | pf.FAILURE_STAGES,
        f"{pf.SUCCESS_STAGES} / {pf.FAILURE_STAGES} / {pf.TERMINAL_STAGES}",
    )


def test_run_keeps_a_punched_link_beyond_the_retry_rounds():
    print("")
    print("---- 隐患 1（行为）：跑真 `_run()`，跨两个重试轮不许 restart ----")

    async def body():
        with patched_fakes():
            punch, state, logs = build()
            old_interval = rd.RETRY_INTERVAL_S
            rd.RETRY_INTERVAL_S = 0.3

            try:
                task = asyncio.get_running_loop().create_task(punch._run())
                punched = await wait_until(lambda: bool(punch.ready), 3.0)

                gen0 = {p: s.generation for p, s in punch.sessions.items()}
                stage0 = {p: s.stage.value for p, s in punch.sessions.items()}
                cycles0 = count(logs, "new connection cycle")

                await asyncio.sleep(1.0)  # 跨过两个 0.3s 重试轮

                gen1 = {p: s.generation for p, s in punch.sessions.items()}
                stage1 = {p: s.stage.value for p, s in punch.sessions.items()}

                task.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await task

                return (punched, gen0, stage0, gen1, stage1,
                        cycles0, count(logs, "new connection cycle"))
            finally:
                rd.RETRY_INTERVAL_S = old_interval

    (punched, gen0, stage0, gen1, stage1, cycles0, cycles1) = asyncio.run(body())

    check(
        "（前提）离线装置里这一条已经打通",
        punched and all(s == "connected" for s in stage0.values()),
        f"stages={stage0}",
    )

    check(
        "★ 跨过两个重试轮：阶段仍是 connected",
        stage1 == stage0,
        f"before={stage0} after={stage1}",
    )

    check(
        "★ 跨过两个重试轮：generation 不变（= 没有重开一次连接）",
        gen1 == gen0,
        f"before={gen0} after={gen1}",
    )

    check(
        "★ 跨过两个重试轮：没有新的 `new connection cycle`",
        cycles1 == cycles0,
        f"before={cycles0} after={cycles1}",
    )


# ============================================================================
#  2. start() 的幂等闸门必须问"洞还活着吗"
# ============================================================================

def test_start_repunches_when_the_hole_is_dead():
    print("")
    print("---- 隐患 2：established -> 停止 -> 洞死 -> start() 必须能重打 ----")

    async def body():
        with patched_fakes():
            punch, state, logs = build()
            old_interval = rd.RETRY_INTERVAL_S
            rd.RETRY_INTERVAL_S = 0.3

            try:
                task = asyncio.get_running_loop().create_task(punch._run())
                await wait_until(lambda: bool(punch.ready), 3.0)
                task.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await task

                # ---- 洞活着 + 循环起来：`start()` 必须真的幂等 ----
                #
                # ★ 2026-09-29：外面那道 `_established and transport_ready` 闸门
                # 已经删掉（它把"重启循环"和"重打这条洞"混成一件事，于是循环
                # 死了而洞还在时打洞永远起不来 —— 用户第 3 条）。所以"幂等"
                # 现在只由 `running` 保证：循环在跑时再叫 `start()` 不许起第二条。
                # "不许重打活洞"由 `_run()` 的 `peer_ready()` 跳过负责。
                alive_ready = punch.transport_ready
                punch.start()
                running_task = punch._task
                first_link = (punch.links.get("p1-cid") or {}).get("link")
                up_before_idempotent = count(logs, "ROOM LINK UP")

                punch.start()
                await asyncio.sleep(0.4)

                alive_idempotent = (
                    punch.running
                    and punch._task is running_task
                    and (punch.links.get("p1-cid") or {}).get("link") is first_link
                    and count(logs, "ROOM LINK UP") == up_before_idempotent
                )

                # ---- 停止 + 洞死：引用还在，但 keepalive 不再维持映射 ----
                #
                # ★ 2026-09-29：**隧道也必须一起停**才算"洞死了"。判据是
                # `_link_alive(link) or _carried_alive(peer)`：一条已经被隧道
                # 接手的洞由隧道自己的保活维持映射，打洞层的 `keepalive_alive`
                # 变 False 并不代表洞不可用（那正是用户第 2 条「打通后界面显示
                # 断了、实际连接是通的」）。所以"洞死" = 两层都停。
                #
                # 先把循环停掉：`transport_ready` 是"可用 **或** 还在争取中"
                # （`running or ready`），循环在跑时它本来就该是 True。
                punch.stop()
                await asyncio.sleep(0.2)

                for peer, entry in punch.links.items():
                    entry["link"].keepalive_alive = False
                    carrier = punch._carriers.get(peer)

                    if carrier is not None:
                        carrier.stop.set()
                        carrier.started = False

                dead_ready = punch.transport_ready
                before_up = count(logs, "ROOM LINK UP")

                punch.start()
                restarted = punch.running
                repunched = await wait_until(lambda: bool(punch.ready), 3.0)

                punch.stop()

                return (alive_ready, alive_idempotent, dead_ready, restarted,
                        repunched, before_up, count(logs, "ROOM LINK UP"))

            finally:
                rd.RETRY_INTERVAL_S = old_interval

    (alive_ready, alive_idempotent, dead_ready, restarted, repunched,
     before_up, after_up) = asyncio.run(body())

    check(
        "（前提）洞活着时 transport_ready 为真",
        alive_ready is True,
        str(alive_ready),
    )

    check(
        "★ 洞活着 + 循环在跑 -> start() 幂等（不起第二条循环、不换洞、不重打）",
        alive_idempotent is True,
        "start() 又起了一个循环 / 换了洞 / 多了一次 ROOM LINK UP",
    )

    check(
        "★ ★ 洞死了（keepalive 不再维持映射）-> transport_ready 必须为假",
        dead_ready is False,
        "旧的 `link is not None and peer_addr is not None` 会误判成真",
    )

    check(
        "★ ★ 洞死之后 start() 必须重新起打洞循环（不再被闸门永远挡住）",
        restarted is True,
        "start() 提前返回了",
    )

    check(
        "★ ★ 重新起来之后真的又打通了（ROOM LINK UP 计数增加）",
        repunched and after_up > before_up,
        f"before={before_up} after={after_up} ready={repunched}",
    )


# ============================================================================
#  3. `started=` 抖一下不许把活着的洞当"对端走了"
# ============================================================================

def test_started_flap_keeps_a_live_link():
    print("")
    print("---- 隐患 3：目标表（started= 抖动）不是花名册，活着的洞不许拆 ----")

    async def body():
        with patched_fakes():
            punch, state, logs = build(
                peer_ids=("p1-cid", "p2-cid"), started_ids=None, members=3,
            )
            old_interval = rd.RETRY_INTERVAL_S
            rd.RETRY_INTERVAL_S = 0.3

            try:
                task = asyncio.get_running_loop().create_task(punch._run())
                await wait_until(lambda: punch.peer_ready("p1-cid"), 3.0)

                p1_link = punch.links.get("p1-cid", {}).get("link")
                # 打通那一刻已经记过一条 "new connection cycle"（新建状态机 =
                # 新的一次连接）。我们要断的是**之后**不再多出来。
                cycles0 = count(logs, "new connection cycle for p1-cid")

                # `started=` 抖一下：p1 从"已开始"掉出去，p2 还在。
                # 目标表 = [p2]，但花名册仍然是 [p1, p2]。
                state.peer_ids = ["p1-cid", "p2-cid"]
                state.started_ids = ["p2-cid"]

                await asyncio.sleep(1.0)  # 跨过若干轮（每轮都调 _sweep_links）

                task.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await task

                return (
                    p1_link,
                    punch.links.get("p1-cid", {}).get("link"),
                    punch.peer_ready("p1-cid"),
                    count(logs, "gone from the room roster"),
                    count(logs, "new connection cycle for p1-cid") - cycles0,
                    punch._peer_attempts.get("p1-cid", 0),
                )
            finally:
                rd.RETRY_INTERVAL_S = old_interval

    (p1_link, p1_now, still_ready, roster_drops, p1_cycles,
     p1_attempts) = asyncio.run(body())

    check(
        "（前提）装置里 p1 先打通了",
        p1_link is not None and still_ready,
        f"link={p1_link} ready={still_ready}",
    )

    check(
        "★ ★ started= 抖动后，p1 的洞还在（没有被当成「对端走了」拆掉）",
        p1_now is not None and p1_now is p1_link,
        f"before={p1_link} after={p1_now}",
    )

    check(
        "★ 没有走「不在花名册」那条拆洞路径",
        roster_drops == 0,
        f"roster_drops={roster_drops}",
    )

    check(
        "★ 也没有对 p1 重开一次连接（没有重打）",
        p1_cycles == 0 and p1_attempts == 0,
        f"cycles={p1_cycles} attempts={p1_attempts}",
    )


def test_roster_leave_still_drops_but_aliases_survive():
    print("")
    print("---- 隐患 3（另一面）：真成员走了照拆；本地别名不许被花名册误杀 ----")

    with patched_fakes():
        # ---- A. 真成员 id 离开花名册 -> 必须拆 ----
        punch, state, logs = build(
            peer_ids=("p1-cid",), started_ids=None, members=2,
        )
        live = FakeLink()
        live.start_keepalive()
        punch.links["p1-cid"] = {
            "link": live, "addr": ("5.6.7.8", 6000),
            "ready_at": time.monotonic(),
        }
        punch._member_ids.update({"p1-cid"})

        state.peer_ids = []          # 中继说：现在房间里没有别人了
        state.members = 1
        punch._sweep_links()
        left_dropped = "p1-cid" not in punch.links

        # ---- B. 本地别名（host_name）不在花名册里 -> 不许拆 ----
        punch2, state2, logs2 = build(
            peer_ids=("other-cid",), started_ids=None, members=3,
            client_id="my-cid",
        )
        alias = FakeLink()
        alias.start_keepalive()
        punch2.links["peer-name"] = {   # `host_by_name` 路径的 key
            "link": alias, "addr": ("5.6.7.8", 6000),
            "ready_at": time.monotonic(),
        }
        punch2._member_ids.update({"other-cid"})   # 别名从不在花名册里
        punch2._sweep_links()
        alias_kept = "peer-name" in punch2.links

        # ---- C. 显式 `set()`（no_peer / no_room）仍然全拆 ----
        punch2._sweep_links(set())
        explicit_dropped = "peer-name" not in punch2.links

        punch.close()
        punch2.close()

    check(
        "★ 真成员离开花名册 -> 照旧只拆那一条（genuine leave 不许放宽）",
        left_dropped is True,
        f"dropped={left_dropped}",
    )

    check(
        "★ ★ 本地别名（host_name）不在 peers= 里 -> 花名册不许误杀（否则活洞被拆）",
        alias_kept is True,
        f"kept={alias_kept}",
    )

    check(
        "★ 显式 set()（确定房间里没人）-> 仍然全拆",
        explicit_dropped is True,
        f"dropped={explicit_dropped}",
    )


# ============================================================================
#  4. 隧道握手失败不许扔掉"已经打通的 socket"
# ============================================================================

def test_tunnel_handshake_reuses_the_punched_socket():
    print("")
    print("---- 隐患 4：两次 HELLO 失败 -> 同一个 socket 重试，不许重打 ----")

    async def body_live():
        with patched_fakes():
            punch, state, logs = build()
            old_backoff = rd.TUNNEL_RETRY_BACKOFF_S
            rd.TUNNEL_RETRY_BACKOFF_S = 0.01

            try:
                link = FakeLink()
                punch.links["p1-cid"] = {
                    "link": link, "addr": ("5.6.7.8", 6000),
                    "ready_at": time.monotonic(),
                }
                FakeCarrier.instances = []
                FakeCarrier.next_results = [False, False, True]

                punch._open_tunnel("p1-cid")

                await asyncio.gather(*list(punch._tasks))

                carrier = FakeCarrier.instances[-1]

                return (
                    punch.links.get("p1-cid", {}).get("link"),
                    link,
                    carrier.punch_link,
                    carrier.sock,
                    carrier.calls,
                    link.closed,
                    count(logs, "re-punching"),
                )
            finally:
                rd.TUNNEL_RETRY_BACKOFF_S = old_backoff

    async def body_dead():
        with patched_fakes():
            punch, state, logs = build()
            old_backoff = rd.TUNNEL_RETRY_BACKOFF_S
            rd.TUNNEL_RETRY_BACKOFF_S = 0.01

            try:
                link = FakeLink()
                link.closed = True  # socket 真的没了
                punch.links["p1-cid"] = {
                    "link": link, "addr": ("5.6.7.8", 6000),
                    "ready_at": time.monotonic(),
                }
                FakeCarrier.instances = []
                FakeCarrier.next_results = [False, False, False]

                punch._open_tunnel("p1-cid")

                await asyncio.gather(*list(punch._tasks))

                return (
                    punch.links.get("p1-cid"),
                    count(logs, "punched link is closed; re-punching"),
                )
            finally:
                rd.TUNNEL_RETRY_BACKOFF_S = old_backoff

    (kept, link, carrier_link, carrier_sock, calls, closed,
     repunch_logs) = asyncio.run(body_live())

    check(
        "★ ★ 两次 HELLO 失败 + 最后成功：洞还在、socket 没被扔掉（同一条链路）",
        kept is link and carrier_link is link and closed is False,
        f"kept={kept} link={link} carrier_link={carrier_link} closed={closed}",
    )

    check(
        "★ ★ carrier 拿到的就是命中的那个 socket（没有换映射）",
        carrier_sock is link.sock and link.punched_sock is None,
        f"sock={carrier_sock} link.sock={link.sock}",
    )

    check(
        "★ 同一个 socket 上有界重试（这里 3 次调用），没有走重打路径",
        calls == 3 and repunch_logs == 0,
        f"calls={calls} re-punching={repunch_logs}",
    )

    (dropped, dead_logs) = asyncio.run(body_dead())

    check(
        "★ ★ 链路真的被关掉（closed）-> 仍然重打（`_drop_peer`，下一轮重新打洞）",
        dropped is None and dead_logs >= 1,
        f"dropped={dropped} logs={dead_logs}",
    )


# ============================================================================
#  5. 隧道一直起不来也必须有截止时间
# ============================================================================

def test_tunnel_setup_deadline_repunches():
    print("")
    print("---- 隐患 5：carrier 一直没 started -> 到截止时间判死并重打 ----")

    async def body():
        with patched_fakes():
            punch, state, logs = build()

            # ---- 单位：新鲜的不判死，过期的判死 ----
            fresh = FakeLink()
            punch.links["p1-cid"] = {
                "link": fresh, "addr": ("5.6.7.8", 6000),
                "ready_at": time.monotonic(),
            }
            overdue_before = punch._link_overdue("p1-cid")
            punch._sweep_links()
            fresh_kept = "p1-cid" in punch.links

            punch.links["p1-cid"] = {
                "link": fresh, "addr": ("5.6.7.8", 6000),
                "ready_at": time.monotonic() - (rd.TUNNEL_SETUP_DEADLINE_S + 1.0),
            }
            overdue_after = punch._link_overdue("p1-cid")
            punch._sweep_links()
            overdue_kept = "p1-cid" in punch.links

            # ---- 行为：真的下一轮重新打通 ----
            old_interval = rd.RETRY_INTERVAL_S
            rd.RETRY_INTERVAL_S = 0.3

            try:
                punch.close()  # 清干净，从零跑一次循环
                task = asyncio.get_running_loop().create_task(punch._run())
                await wait_until(lambda: punch.peer_ready("p1-cid"), 3.0)

                # 隧道"一直没起来"：carrier 不见了，但保活还活着（洞没死），
                # 而且 ready_at 已经过期。
                punch._carriers.pop("p1-cid", None)
                punch.links["p1-cid"]["ready_at"] = (
                    time.monotonic() - (rd.TUNNEL_SETUP_DEADLINE_S + 1.0)
                )
                up_before = count(logs, "ROOM LINK UP")

                repunched = await wait_until(
                    lambda: count(logs, "ROOM LINK UP") > up_before
                    and punch.peer_ready("p1-cid"),
                    3.0,
                )

                task.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await task

                return (overdue_before, fresh_kept, overdue_after, overdue_kept,
                        repunched, count(logs, "no tunnel came up"),
                        count(logs, "ROOM LINK UP") - up_before)
            finally:
                rd.RETRY_INTERVAL_S = old_interval

    (overdue_before, fresh_kept, overdue_after, overdue_kept, repunched,
     deadline_logs, new_ups) = asyncio.run(body())

    check(
        "（前提）刚打通的洞没到截止时间（不误杀）",
        overdue_before is False and fresh_kept is True,
        f"overdue={overdue_before} kept={fresh_kept}",
    )

    check(
        "★ ★ carrier 一直没 started + 过了截止时间 -> 判死、拆掉这一条",
        overdue_after is True and overdue_kept is False,
        f"overdue={overdue_after} kept={overdue_kept}",
    )

    check(
        "★ ★ 判死之后下一轮真的重新打通（ROOM LINK UP 增加）",
        repunched and new_ups >= 1,
        f"repunched={repunched} ups={new_ups}",
    )

    check(
        "★ 判死走的是「隧道没起来」那条日志（隐患 5 的新路径）",
        deadline_logs >= 1,
        f"deadline_logs={deadline_logs}",
    )


# ============================================================================
#  6. flow_summary 必须是纯函数（读路径不许建状态）
# ============================================================================

def test_flow_summary_is_read_only():
    print("")
    print("---- 隐患 6：连续调 flow_summary 不许建/改 sessions ----")

    with patched_fakes():
        punch, state, logs = build(
            peer_ids=("p1-cid", "p2-cid"), started_ids=None, members=3,
        )

        # p1：正常的状态机，已经打通（成功终态）。
        sess = punch._session("p1-cid")
        sess.start()
        for stage in (
            pf.Stage.CONFIRM_TARGET, pf.Stage.NAT, pf.Stage.INBOUND,
            pf.Stage.REPORT, pf.Stage.SYNC_START, pf.Stage.CANDIDATE,
            pf.Stage.CONFIRM_CAND, pf.Stage.SYNC_GO, pf.Stage.PUNCH,
        ):
            sess.advance(stage)
        sess.finish(True, latency_ms=18.0, addr="1.2.3.4:6000")

        # p2：**只有洞、没有状态机**（旧代码的 `_session()` 就是在这里写状态）。
        p2_link = FakeLink()
        p2_link.start_keepalive()
        punch.links["p2-cid"] = {
            "link": p2_link, "addr": ("5.6.7.8", 7000),
            "ready_at": time.monotonic(),
        }

        punch.target_reason = "sentinel"

        before_keys = list(punch.sessions.keys())
        before_ids = {p: id(s) for p, s in punch.sessions.items()}
        before_gen = {p: s.generation for p, s in punch.sessions.items()}
        before_stage = {p: s.stage.value for p, s in punch.sessions.items()}

        flows = [
            (punch.flow_summary() or {},
             punch.flow_summary(punching=True) or {})
            for _ in range(3)
        ]

        after_keys = list(punch.sessions.keys())
        after_ids = {p: id(s) for p, s in punch.sessions.items()}
        after_gen = {p: s.generation for p, s in punch.sessions.items()}
        after_stage = {p: s.stage.value for p, s in punch.sessions.items()}

        flow = flows[-1][0]

        check(
            "★ ★ 连续调 3 次：sessions 一个键都没多（不再现建状态机）",
            after_keys == before_keys == ["p1-cid"],
            f"before={before_keys} after={after_keys}",
        )

        check(
            "★ ★ 同一个状态机对象、同一个 generation/stage（读路径没改状态）",
            after_ids == before_ids
            and after_gen == before_gen
            and after_stage == before_stage,
            f"ids={after_ids == before_ids} gen={after_gen} stage={after_stage}",
        )

        check(
            "★ 显示仍然对称：p1（有状态机）+ p2（只有洞）都算已直连 2/2",
            flow.get("total") == 2 and flow.get("up") == 2
            and flow.get("all_up") is True and len(flow.get("rows") or []) == 2,
            f"total={flow.get('total')} up={flow.get('up')} "
            f"all_up={flow.get('all_up')} rows={flow.get('rows')}",
        )

        check(
            "★ 汇总行里 p2 也是「P2P 已直连」",
            all("P2P 已直连" in row for row in (flow.get("rows") or [])),
            str(flow.get("rows")),
        )

        check(
            "★ target_reason 不被读路径改写（纯函数的另一半）",
            punch.target_reason == "sentinel",
            punch.target_reason,
        )

        punch.close()


# ============================================================================

def main():
    print("=" * 66)
    print("CraftLink 打洞加固测试（六个隐患，逐条回归）")
    print("=" * 66)

    test_success_terminal_is_not_a_new_cycle()
    test_run_keeps_a_punched_link_beyond_the_retry_rounds()
    test_start_repunches_when_the_hole_is_dead()
    test_started_flap_keeps_a_live_link()
    test_roster_leave_still_drops_but_aliases_survive()
    test_tunnel_handshake_reuses_the_punched_socket()
    test_tunnel_setup_deadline_repunches()
    test_flow_summary_is_read_only()

    print("")
    print("=" * 66)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 66)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
