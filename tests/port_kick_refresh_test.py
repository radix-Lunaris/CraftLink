"""
CraftLink —— 用户 2026-09-29（第三轮）实测的 6 条（第 7 条待确认后再做）。

逐条对应：

  1. **本地代理端口热切换**（唯一允许热切换的参数）：空闲时直接改；连接中改 ->
     确认框（会掉线、需要重进游戏）-> 确认后**自动重建这条隧道并自动重连**，
     状态不变。旧实现只改显示、不重建、端口还是旧的。
  2. **客户端踢人**：中继那条路是好的，坏在客户端把**已经是 token 的房间 ID
     又编码了一次**（`quote` 不幂等）-> 中继 `ERROR KICK_NO_SUCH_MEMBER
     目标不在这个房间里`，界面还把它说成「该成员不在房间里」。同时修成员列表
     在出现「踢出」按钮时列错位（表头 + 每行各一套 grid）。
  3. **打洞进度文案**：未连接时只显示「未启用」，不再写「未启用（Relay 模式）」。
  4. **MC 多人列表反复刷新** -> 幽灵 PLAYER 连接占满房间名额 -> 两人房也
     「房间已满」：客户端在等中继配对时**同时盯住本机 MC socket**（MC 一关就
     立刻收工）；服务端容量闸门**按人去重** + 等待池有真正的过期回收。
  5. **面板在线房间页**：没有中继会话时不再显示「房间里暂时没有玩家。」。
  6. **被踢的玩家重新进房但没点开始连接** -> Host 状态机不该自己打起来
     （`_heard_started` 只增不减 + 非破坏性 peek 反复刷新新鲜度 = 自锁）。

Run:
    python tests/port_kick_refresh_test.py
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SANDBOX = tempfile.mkdtemp(prefix="mclink-round3-")
os.environ["APPDATA"] = SANDBOX
os.environ["XDG_CONFIG_HOME"] = SANDBOX

sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "server"))

from mclink.core import protocol as proto  # noqa: E402
from mclink.core import punch_flow as pf  # noqa: E402
from mclink.core import room_direct as rd  # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_R3_PORT", "19620"))
SENTINEL = "__mclink__"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================================
#  1. 本地代理端口热切换
# ============================================================================

class _PortEngine:
    """只记录 `watch_room_nat` / `session_mode` 的引擎替身。"""

    def __init__(self, mode="stun"):
        self.room = _PortRoom(mode)
        self.session_mode_value = mode
        self.nat_calls = []

    def room_state(self):
        return self.room

    def room_current(self):
        return object()

    def session_mode(self):
        return self.session_mode_value

    def start_session(self, config, auth=None, timeout=20):
        self.session_mode_value = str(config.mode)
        return object()

    def stop_session(self, timeout=15):
        self.session_mode_value = ""
        return True

    def watch_room_nat(self, enabled, log=None, standalone_host=None):
        self.nat_calls.append(bool(enabled))

    def watch_room_direct(self, enabled, log=None):
        pass

    def room_direct_state(self):
        return {}

    def room_nat_profile(self):
        return None

    def room_nat_age(self):
        return None

    def room_rtt_ms(self):
        return None

    def room_set_started(self, connecting, timeout=8):
        return True, "ok"

    def traffic_snapshot(self):
        return {
            "relay_up": 0, "relay_down": 0, "direct_up": 0, "direct_down": 0,
            "total_up": 0, "total_down": 0, "up_rate": 0.0, "down_rate": 0.0,
        }

    def snapshot(self):
        data = {"role": None, "state": "IDLE", "state_text": "未启动"}
        data.update(self.traffic_snapshot())
        return data

    def relay_requires_account(self, *_a, **_k):
        return False

    def _push(self, kind, **data):
        pass

    def drain_events(self):
        return []


class _PortRoom:
    def __init__(self, mode):
        self.conn_mode = mode
        self.is_host = False
        self.is_owner = False
        self.host_claimed = True
        self.host_id = "h-cid"
        self.host_name = "host"
        self.members = 2
        self.client_id = "p-cid"
        self.peer_ids = ["h-cid"]
        self.started_ids = ["h-cid"]
        self.member_conns = {"h-cid": "pairing", "p-cid": "idle"}
        self.member_names = {"h-cid": "host", "p-cid": "player"}
        self.member_rtts = {}
        self.self_name = "player"
        self.username = "player"
        self.own_name = "player"
        self.world_players = 0
        self.max_players = 8
        self.server_max_players = 8
        self.mode_reason = "owner"
        self.revoked = False
        self.revoked_reason = ""
        self.display = "78"
        self.joined_at = 0.0
        self.owner_id = ""


def test_local_port_hot_switch():
    print("")
    print("---- 第 1 条：本地代理端口热切换（空闲直接改 / 连接中确认后重建）----")

    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        check("本地端口热切换（需要 tkinter）", False, "no tkinter")
        return

    answers = {"ok": True}
    messagebox.askokcancel = lambda *a, **k: answers["ok"]
    messagebox.showwarning = lambda *a, **k: None
    messagebox.showinfo = lambda *a, **k: None
    messagebox.showerror = lambda *a, **k: None

    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        check("本地端口热切换（需要 X 显示）", False, repr(e))
        return

    root.withdraw()

    from mclink.gui import app as gui_app

    app = gui_app.MCLinkApp(root)
    app.engine = _PortEngine("stun")
    app.log = lambda *a, **k: None

    rebuilds = []
    app._rebuild_tunnels = lambda mode=None: rebuilds.append(
        str(mode or app._room_conn_mode() or "?")
    )

    try:
        # `_collect_config()` 需要 mc_port / max_players 已经提交过（正常界面
        # 一定填过）；这里补上，免得和这条测试无关的报错挡住断言。
        app.mc_port_var.set("25565")
        app.max_players_var.set("4")
        app._committed["mc_port"] = "25565"
        app._committed["max_players"] = "4"
        app.config.role = "player"
        app.config.server_host = "127.0.0.1"
        app.config.server_port = 19100
        app.config.room = "78"
        app.config.username = "player"
        app.room_var.set("78")
        app.username_var.set("player")
        app.host_var.set("127.0.0.1")
        app.port_var.set("19100")

        # ---- ① 空闲（running=False）：直接改，不弹窗、不重建 ----
        app.running = False
        app._committed["local_port"] = "25565"
        app.local_port_var.set("25566")
        app._commit_port("local_port", app.local_port_var)

        check(
            "★ 空闲：直接生效，不重建、不弹窗",
            app._committed.get("local_port") == "25566" and not rebuilds,
            f"committed={app._committed.get('local_port')} rebuilds={rebuilds}",
        )

        # ---- ② 连接中 + 确认：改完自动重建这条隧道（新模式取自房间） ----
        app.running = True
        app.config.role = "player"
        app._punch_mode = "stun"
        rebuilds.clear()
        answers["ok"] = True

        app.local_port_var.set("25599")
        app._commit_port("local_port", app.local_port_var)

        check(
            "★★ 连接中 + 确认：端口生效**并且**自动重建了这条隧道",
            app._committed.get("local_port") == "25599"
            and rebuilds == ["stun"],
            f"committed={app._committed.get('local_port')} rebuilds={rebuilds}",
        )

        config = app._collect_config(role_override="player")

        check(
            "★★ 重建用的配置带的是**新端口**（旧实现只有显示变、端口还是旧的）",
            int(getattr(config, "local_port", 0) or 0) == 25599,
            f"local_port={getattr(config, 'local_port', None)}",
        )

        # ---- ③ 连接中 + 取消：什么都不动（连显示都退回旧值） ----
        rebuilds.clear()
        answers["ok"] = False

        app.local_port_var.set("25601")
        app._commit_port("local_port", app.local_port_var)

        check(
            "★ 连接中 + 取消：不改、不重建，输入框退回已生效的值",
            app._committed.get("local_port") == "25599"
            and not rebuilds
            and app.local_port_var.get() == "25599",
            f"committed={app._committed.get('local_port')} "
            f"var={app.local_port_var.get()} rebuilds={rebuilds}",
        )

        # ---- ④ 房主侧改它不该重建（重建房主会话会把所有人踢下线） ----
        app.config.role = "host"
        rebuilds.clear()
        answers["ok"] = True

        app.local_port_var.set("25602")
        app._commit_port("local_port", app.local_port_var)

        check(
            "★ 房主侧改本地端口：不重建会话（那不是房主用的端口）",
            not rebuilds and app._committed.get("local_port") == "25602",
            f"rebuilds={rebuilds}",
        )
    finally:
        with contextlib.suppress(Exception):
            root.destroy()


# ============================================================================
#  2. 客户端踢人：房间 token 必须幂等（+ 真中继端到端）
# ============================================================================

def test_room_token_is_idempotent():
    print("")
    print("---- 第 2 条：房间 token 幂等（客户端不再二次编码）----")

    raw = "晚上好"
    tok = proto.room_token(raw)

    check(
        "★ 中文房间名 -> token 是 percent-encoded",
        tok and tok.startswith("%") and raw not in tok,
        repr(tok),
    )
    check(
        "★★ 对**已经是 token** 的值再来一次结果不变（旧代码会变成 %25E6...）",
        proto.room_token(tok) == tok,
        f"{tok} -> {proto.room_token(tok)}",
    )
    check(
        "★ ASCII 房间名不受影响，仍然幂等",
        proto.room_token("mc123456") == "mc123456"
        and proto.room_token(proto.room_token("mc123456")) == "mc123456",
        "",
    )
    check(
        "★ 和服务端同一套语义（unquote 再 quote）",
        proto.room_token("%E6%99%9A%E4%B8%8A%E5%A5%BD") == tok,
        f"{proto.room_token('%E6%99%9A%E4%B8%8A%E5%A5%BD')} != {tok}",
    )

    # 真中继：中文房间名 + 客户端那样发 KICK（旧代码在这里必红）
    process, log = _start_relay()

    try:
        if process.poll() is not None:
            check("（前提）中继启动", False, "见 tests/round3_relay.log")
            return

        check("（前提）中继启动", True)

        async def scenario():
            owner = await _Member("owner").connect()
            guest = await _Member("guest").connect()

            from mclink.config import settings as cfg

            ver = cfg.APP_VERSION

            await owner.send(
                f"CREATE {SENTINEL} cid-owner owner - "
                f"{proto.room_token('晚上好')} ver={ver}"
            )
            ack = await owner.expect("OK JOIN", timeout=8)
            check("（前提）中文房间名建房成功（OK JOIN）", bool(ack), str(ack))

            room_id = ""
            for token in (ack or "").split():
                if token.startswith("room="):
                    room_id = token.split("=", 1)[1]

            await guest.send(
                f"JOIN {SENTINEL} cid-guest guest - {room_id} ver={ver}"
            )
            joined = await guest.expect("OK JOIN", timeout=8)
            check("（前提）第二个人加入成功", bool(joined), str(joined))

            # ★ 客户端就是这么发的：state.room（**已经是 token**）再过一次
            #   `proto.room_token()`。旧代码在这里被中继回 KICK_NO_SUCH_MEMBER。
            kick_token = proto.room_token(room_id)
            await owner.send(f"KICK {kick_token} cid-guest")

            reply = await owner.expect("OK KICK", timeout=8)

            if reply is None:
                reply = await owner.expect("ERROR", timeout=2)

            check(
                "★★ 中文房间名：客户端那样发 KICK 必须被接受（旧代码："
                "KICK_NO_SUCH_MEMBER 目标不在这个房间里）",
                bool(reply) and reply.startswith("OK KICK"),
                str(reply),
            )

            await owner.close()
            await guest.close()

        asyncio.run(scenario())
    finally:
        with contextlib.suppress(Exception):
            process.terminate()
            process.wait(timeout=10)

        with contextlib.suppress(Exception):
            log.close()


def test_member_list_uses_one_grid():
    print("")
    print("---- 第 2 条：成员列表表头 + 所有行共用一套 grid（不再错位）----")

    source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()

    check(
        "★ 表头直接 grid 进 member_table（不再有每行/表头自己的 Frame）",
        "header = tk.Frame(self.member_table" not in source
        and "tk.Label(\n                self.member_table,\n                text=tr(title),"
        in source,
        "header frame",
    )
    check(
        "★ 数据格子也 grid 进 member_table（列宽只有一个来源）",
        "label = tk.Label(\n                self.member_table,\n                textvariable=var,"
        in source,
        "row cells",
    )
    check(
        "★ 重排行号时每一格都跟着走",
        'for cell in widgets["cells"].values():' in source
        and "cell.grid_configure(row=index + 1)" in source,
        "regrid",
    )
    check(
        "★ 成员走了以后每个格子都销毁（没有行 Frame 可销毁）",
        'for cell in widgets["cells"].values():\n                            cell.destroy()'
        in source,
        "destroy",
    )


# ============================================================================
#  3. 打洞进度：未连接时只说「未启用」
# ============================================================================

def test_punch_progress_text():
    print("")
    print("---- 第 3 条：未连接时打洞进度只显示「未启用」----")

    summary = pf.summarize([], punching=False)

    check(
        "★ `summarize(punching=False)` 的正文 = 未启用（不再提 Relay）",
        summary.get("progress") == "未启用"
        and "Relay" not in str(summary.get("progress")),
        str(summary.get("progress")),
    )

    from mclink.gui import app as gui_app

    text, _hint = gui_app.punch_step_text({"mode": "relay"})

    check(
        "★ 状态卡那一行同样是「未启用」",
        text == "未启用",
        repr(text),
    )

    i18n_source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "i18n_tables.py"),
        encoding="utf-8",
    ).read()

    check(
        "★ 三语表里「未启用（Relay 模式）」已经不存在（旧键清干净）",
        "未启用（Relay 模式）" not in i18n_source
        and "'未启用': 'Not in use'" in i18n_source,
        "",
    )


# ============================================================================
#  4. MC 刷新幽灵：客户端盯着本机 socket + 服务端按人去重 + 等待池过期
# ============================================================================

class _FakeReader:
    def __init__(self, chunks=(), eof_after=True):
        self._chunks = list(chunks)
        self._eof_after = eof_after

    async def read(self, _n=0):
        if self._chunks:
            return self._chunks.pop(0)

        if self._eof_after:
            return b""

        await asyncio.sleep(30)
        return b""

    async def readline(self):
        if self._chunks:
            return self._chunks.pop(0)

        if self._eof_after:
            return b""

        await asyncio.sleep(30)
        return b""


class _SlowReader:
    """永远不回（模拟"中继还没配对"）。"""

    async def read(self, _n=0):
        await asyncio.sleep(30)
        return b""

    async def readline(self):
        await asyncio.sleep(30)
        return b""


def test_client_drops_the_probe_when_minecraft_leaves():
    print("")
    print("---- 第 4 条：MC 关了这条探测 -> 客户端立刻收工（不再占名额）----")

    from mclink.core import player_core as pc

    core = pc.PlayerCore.__new__(pc.PlayerCore)
    core.PAIR_WAIT_S = 2.0

    async def scenario():
        # ① 中继还没回，MC 先关了（刷新/探测结束）-> mc_gone
        line, pending, gone = await core._await_relay_or_mc(
            _SlowReader(), _FakeReader(chunks=()), 1
        )
        check(
            "★★ MC 关掉 -> `mc_gone=True`（调用方立刻关掉中继那条 socket）",
            gone is True and line is None,
            f"line={line!r} gone={gone}",
        )

        # ② MC 先发来握手包，然后中继才回 CONNECTED -> 一个字节都不许丢
        mc = _FakeReader(chunks=[b"MC-HANDSHAKE"], eof_after=False)
        relay = _FakeReader(chunks=[b"CONNECTED\n"], eof_after=False)
        line, pending, gone = await core._await_relay_or_mc(relay, mc, 2)

        check(
            "★★ MC 的握手包被**暂存**下来（配对成功后原样补发）",
            line == b"CONNECTED\n"
            and pending == b"MC-HANDSHAKE"
            and gone is False,
            f"line={line!r} pending={pending!r} gone={gone}",
        )

        # ③ ★ P0 自检：前缀必须走"最终胜出的那条通路"，绝不能写进中继。
        #
        # STUN 模式下房主对中继隧道是"只读不转发"的（direct_carry._drain_relay
        # 会丢弃并记事故日志），所以把握手包丢给中继 = 直连流从半路开始
        # = Minecraft 直接连不上。
        wrapped = pc.PlayerCore._PrependedReader(pending, _FakeReader(
            chunks=[b"REST"], eof_after=False
        ))

        first = await wrapped.read(65536)
        second = await wrapped.read(65536)

        check(
            "★★★ 提前读走的字节被塞回读取流最前面（顺序不变：谁拿到通路谁发）",
            first == b"MC-HANDSHAKE" and second == b"REST",
            f"first={first!r} second={second!r}",
        )

        # 前缀没读完时不许报 EOF（否则上层会把这条连接当成读完了）
        eof_reader = pc.PlayerCore._PrependedReader(b"P", _FakeReader(chunks=[]))

        check(
            "★★★ 前缀没读完时 at_eof() 为假（底层关了也不算读完）",
            eof_reader.at_eof() is False,
            str(eof_reader.at_eof()),
        )

        source = open(
            os.path.join(ROOT, "client", "mclink", "core", "player_core.py"),
            encoding="utf-8",
        ).read()

        check(
            "★★★ 代码里**没有**把提前读到的字节直接写进中继 socket"
            "（那会在 STUN 模式下丢握手包）",
            "relay_writer.write(early_mc_bytes)" not in source
            and "self._PrependedReader(early_mc_bytes, mc_reader)" in source,
            "flush-to-relay",
        )

    asyncio.run(scenario())


def test_server_counts_people_not_probes():
    print("")
    print("---- 第 4 条：容量闸门按**人**去重 + 等待池有真正的过期回收 ----")

    import relay_server as rs
    import room_members as rm

    async def scenario():
        room = rs.Room("座位房")
        room.max_players_room = 8

        class _W:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def is_closing(self):
                return self.closed

        # 同一个人刷 7 次 MC 多人列表 = 7 条 PLAYER 握手
        for _ in range(7):
            waiter = rs.Waiter(None, _W(), client_id="cid-same", username="p")
            room.waiting_players.append(waiter)

        check(
            "★★ 同一个人的 7 条探测只占 1 个名额（旧闸门 = 7 -> 两人房也报满）",
            room.occupied_seats() == 1,
            f"occupied={room.occupied_seats()}",
        )

        # 另一个不同的人 -> 2 个名额（真的两个人还是要算两个）
        room.waiting_players.append(
            rs.Waiter(None, _W(), client_id="cid-other", username="q")
        )

        check(
            "★ 不同的人仍然各占一个名额",
            room.occupied_seats() == 2,
            f"occupied={room.occupied_seats()}",
        )

        # 过期回收：把 created_at 拨老 -> 看门狗必须收掉它
        stale = room.waiting_players[0]
        stale.created_at -= (rm.WAITER_MAX_WAIT_S + 5)

        reaped = await rm.reap_stale_waiters([room], max_age=rm.WAITER_MAX_WAIT_S)

        check(
            "★★ 等太久的未配对连接被等待池看门狗关掉（名额还回来）",
            reaped == [stale] and stale.writer.closed is True,
            f"reaped={len(reaped)} closed={stale.writer.closed}",
        )

        # 正常等待不许被误收（已经收过的那条也不该被重复记一次）
        fresh = room.waiting_players[-1]
        reaped = await rm.reap_stale_waiters([room], max_age=rm.WAITER_MAX_WAIT_S)

        check(
            "★ 刚进来的等待者不会被误收（reaper 幂等）",
            reaped == [] and fresh.writer.closed is False,
            f"reaped={len(reaped)}",
        )

    asyncio.run(scenario())


# ============================================================================
#  7. 起跑前的相对倒计时（0.1s 分辨率）必须出现在状态机的逐目标行上
# ============================================================================

def test_punch_countdown_is_visible():
    print("")
    print("---- 第 7 条：起跑前的相对倒计时（1.5s / 1.2s…）----")

    def walk_to(sess, target):
        """按 §E 的合法边一格一格走到 `target`（`advance()` 拒绝跳步）。"""
        order = list(pf.STAGE_ORDER)

        while sess.stage != target:
            nxt = order[order.index(sess.stage) + 1]
            assert sess.advance(nxt), (sess.stage, nxt)
            # 真实信标里是**字符串**（`stage=punch`），不是枚举：枚举的
            # `str()` 是 "Stage.PUNCH"，`note_peer_stage` 认不出来。
            sess.note_peer_stage(getattr(nxt, "value", nxt))

    async def scenario():
        sess = pf.ConnSession(peer="p1-cid", role="host", label="玩家一")
        walk_to(sess, pf.Stage.SYNC_START)

        # ① 「等待同步起跑」这一段：真实现场是"我在同步起跑等它测完"
        #    （对端最后一条信标还在 nat），所以显示同步会把这一格判成 NAT。
        #    倒计时取**本端真实阶段**的基线原文，对端在测什么作注脚。
        sess.set_countdown(time.monotonic() + 9.8)
        sess.note_peer_stage(pf.PEER_MEASURING_NAT)

        row = sess.row_text()

        check(
            "★★ 逐目标行：基线「等待同步起跑」+ 0.1s 倒计时都在"
            "（旧代码只显示对端小步，且秒数是正计时）",
            row.startswith("等待同步起跑 9.") and "【玩家一】N/C" in row,
            row,
        )

        check(
            "★ 对端在测什么作为注脚保留（用户 09-28 的要求没被吃掉）",
            "对端正在测 NAT" in row,
            row,
        )

        # ①-b 对端报"测完了" -> 注脚消失，只剩基线原文 + 倒计时
        sess.note_peer_stage(pf.PEER_MEASURED)

        row = sess.row_text()

        check(
            "★ 对端测完：只剩「等待同步起跑 9.xs」（注脚自动消失）",
            row.startswith("等待同步起跑 9.")
            and "对端正在测" not in row
            and "【玩家一】N/C" in row,
            row,
        )

        # ② 倒计时在走：0.1s 分辨率（同一条状态机读两次必须变小）
        first = sess.countdown_text()
        time.sleep(0.25)
        second = sess.countdown_text()

        check(
            "★★ 倒计时真的在走（0.1s 分辨率，界面 150ms 一拍就能画出 1.5->1.2）",
            first is not None and second is not None
            and float(first[:-1]) - float(second[:-1]) >= 0.1,
            f"{first} -> {second}",
        )

        # ③ 约定的喷包时刻（`DIRECT_PUNCH startIn=1.5`）：起跑前显示「等待发包起跑」
        walk_to(sess, pf.Stage.CANDIDATE)
        walk_to(sess, pf.Stage.CONFIRM_CAND)
        walk_to(sess, pf.Stage.PUNCH)
        sess.note_punch(1)
        sess.set_countdown(time.monotonic() + 1.4, label="等待发包起跑")

        row = sess.row_text()

        check(
            "★★ 喷包前 1.5s：显示「等待发包起跑 1.4s」而不是「打洞中 PUNCH #0001」",
            row.startswith("等待发包起跑 1.4s")
            and "打洞中" not in row
            and "PUNCH" not in row,
            row,
        )

        # ④ 到点（清掉倒计时）-> 回到「打洞中 PUNCH #NNNN」
        sess.note_peer_stage(pf.Stage.PUNCH.value)
        sess.clear_countdown()
        row = sess.row_text()

        check(
            "★ 起跑时刻一到：立刻回到「打洞中 PUNCH #0001」",
            row.startswith("打洞中 PUNCH #0001"),
            row,
        )

        # ⑤ 换阶段 = 倒计时作废（不许跨阶段残留）
        sess.set_countdown(time.monotonic() + 5.0, label="等待发包起跑")
        direct = rd.RoomDirectLink(
            _IdleEngine(_IdleState(), _IdleRoom()), log=lambda *a: None
        )
        direct.sessions = {"p1-cid": sess}
        direct._advance_session(sess, pf.Stage.CONNECTED)

        check(
            "★ 换阶段时倒计时自动清掉（不会跨阶段残留）",
            sess.countdown_text() is None and sess.countdown_left() is None,
            f"left={sess.countdown_left()}",
        )

        # ⑥ 成员列表那种窄格子（compact）不加倒计时（版式优先）
        sess2 = pf.ConnSession(peer="p2-cid", role="host", label="玩家二")
        walk_to(sess2, pf.Stage.SYNC_START)
        sess2.set_countdown(time.monotonic() + 3.0)

        compact_row = sess2.row_text(compact=True)

        check(
            "★ 成员列表（compact）不加倒计时也不加秒数，版式不炸",
            "等待同步起跑" in compact_row
            and compact_row.endswith("【玩家二】N/C")
            and "（" not in compact_row
            and "s【" not in compact_row,
            compact_row,
        )

        # ⑦ 房间状态里带 `countdown` 信号（界面据此提速重画）
        direct.sessions = {"p1-cid": sess2}
        state = direct.state()

        check(
            "★★ 房间状态带 `countdown=True`（界面把逐目标行提到 150ms 一拍）",
            state.get("countdown") is True,
            str(state.get("countdown")),
        )

        sess2.clear_countdown()

        check(
            "★ 倒计时结束 -> `countdown=False`（回到 1 秒一拍）",
            direct.state().get("countdown") is False,
            str(direct.state().get("countdown")),
        )

    asyncio.run(scenario())

    source = open(
        os.path.join(ROOT, "client", "mclink", "gui", "app.py"),
        encoding="utf-8",
    ).read()

    check(
        "★★ 界面每 tick 都会重画倒计时那一块（150ms 一拍，纯显示）",
        "self._refresh_countdown_rows()" in source
        and "if not room_punch.get(\"countdown\"):" in source,
        "fast repaint",
    )


def test_gui_countdown_fast_path():
    print("")
    print("---- 第 7 条：界面在倒计时期间每 tick 只重画那一块（150ms 一拍）----")

    try:
        import tkinter as tk
    except ImportError:
        check("倒计时快路径（需要 tkinter）", False, "no tkinter")
        return

    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        check("倒计时快路径（需要 X 显示）", False, repr(e))
        return

    root.withdraw()

    from mclink.gui import app as gui_app

    class _Engine:
        def __init__(self):
            self.calls = 0
            self.countdown = True

        def room_state(self):
            return None

        def room_direct_state(self):
            self.calls += 1

            return {
                "countdown": self.countdown,
                "punching": True,
                "stun_ready": False,
                "direct_step": "等待发包起跑 1.4s",
                "flow": {"total": 1, "up": 0, "all_up": False},
                "links": [{
                    "peer": "p1", "name": "甲", "status": "打洞中",
                    "nat": "-", "inbound": "-", "addr": "-",
                    "ping_ms": None, "peer_rtt_ms": None,
                    "peer_direct": False, "role_text": "玩家",
                }],
            }

        def snapshot(self):
            return {
                "mode": "stun", "role": "player", "state": "RUNNING",
                "direct_step": "等待发包起跑 1.4s", "direct_attempts": 1,
            }

    app_gui = gui_app.MCLinkApp(root)
    app_gui.engine = _Engine()
    app_gui.running = True

    try:
        root.update_idletasks()
        app_gui._refresh_countdown_rows()
        root.update_idletasks()

        check(
            "★ 倒计时中：这一拍真的查了房间打洞状态并写进「打洞进度」",
            app_gui.engine.calls >= 1
            and app_gui.status_vars["punch"].get() == "等待发包起跑 1.4s",
            f"calls={app_gui.engine.calls} "
            f"punch={app_gui.status_vars['punch'].get()!r}",
        )

        app_gui.engine.countdown = False
        before = app_gui.status_vars["punch"].get()
        app_gui.engine.calls = 0

        app_gui._refresh_countdown_rows()
        root.update_idletasks()

        check(
            "★ 没有倒计时：只查一次标志、一个字都不重画"
            "（回到 1 秒一拍，不做无用功）",
            app_gui.engine.calls == 1
            and app_gui.status_vars["punch"].get() == before,
            f"calls={app_gui.engine.calls} "
            f"punch={app_gui.status_vars['punch'].get()!r}",
        )
    finally:
        with contextlib.suppress(Exception):
            root.destroy()


# ============================================================================
#  6. 被踢的玩家重新进房（没点开始连接）不许被 Host 打起来
# ============================================================================

class _IdleState:
    def __init__(self, peer_ids=("p1-cid",), conns=None):
        self.client_id = "h-cid"
        self.peer_ids = list(peer_ids)
        self.members = len(self.peer_ids) + 1
        self.is_host = True
        self.started_ids = ["h-cid"]
        self.host_claimed = True
        self.host_id = "h-cid"
        self.host_name = "host"
        self.self_name = "host"
        self.member_names = {p: p for p in self.peer_ids}
        self.member_conns = conns if conns is not None else {
            "h-cid": "connected", "p1-cid": "idle"
        }


class _IdleRoom:
    def __init__(self):
        self.direct_rx_at = {}


class _IdleEngine:
    def __init__(self, state, room):
        self._state = state
        self._room = room

    def room_state(self):
        return self._state

    def room_current(self):
        return self._room

    def loop(self):
        return None

    def room_nat_profile(self):
        return None


def test_idle_rejoined_peer_is_not_a_target():
    print("")
    print("---- 第 6 条：被踢的玩家重新进房（未连接）不许被 Host 打起来 ----")

    async def scenario():
        state = _IdleState(peer_ids=("p1-cid",))
        room = _IdleRoom()
        direct = rd.RoomDirectLink(_IdleEngine(state, room), log=lambda *a: None)
        direct._member_ids.add("p1-cid")

        # 他"曾经开始过"（离开之前的证据还在），而且离开前发过信标
        direct._heard_started = {"p1-cid"}
        direct._heard_at = {"p1-cid": time.monotonic()}
        room.direct_rx_at = {"p1-cid": time.monotonic()}

        # ① 还在连接中（信标新鲜 + conn=idle）-> 仍然是目标（不许误伤）
        targets, _reason = direct._select_targets_now()

        check(
            "★ 信标新鲜 + conn=idle -> 仍然是目标（正在打洞的正常玩家）",
            targets == ["p1-cid"],
            f"targets={targets}",
        )

        # ② 他离开了房间（花名册里没有他了）-> 证据全部忘掉
        state.peer_ids = []
        state.members = 1
        direct._sweep_links()

        check(
            "★★ 离开房间 -> 忘掉【他曾经开始过】（否则回来就被无限打洞）",
            "p1-cid" not in direct._heard_started
            and "p1-cid" not in direct._heard_at
            and "p1-cid" not in direct._heard_seen
            and "p1-cid" not in room.direct_rx_at,
            f"started={direct._heard_started} at={direct._heard_at} "
            f"rx={room.direct_rx_at}",
        )

        # ③ 他重新进房（同一个 client_id），没点开始连接
        state.peer_ids = ["p1-cid"]
        state.members = 2
        state.member_conns = {"h-cid": "connected", "p1-cid": "idle"}

        targets, reason = direct._select_targets_now()

        check(
            "★★ 重新进房但没开始连接 -> **不是**目标（界面应该是"
            "「等待对端连接…」，而不是自己打起来）",
            targets == [],
            f"targets={targets} reason={reason}",
        )

        # ④ 反例：信标已经过期（> HEARD_FRESH_S）+ conn=idle -> 也不是目标
        direct._heard_started = {"p1-cid"}
        direct._heard_at = {
            "p1-cid": time.monotonic() - (rd.HEARD_FRESH_S + 5.0)
        }
        room.direct_rx_at = {
            "p1-cid": time.monotonic() - (rd.HEARD_FRESH_S + 5.0)
        }

        targets, _reason = direct._select_targets_now()

        check(
            "★ 旧信标（超过新鲜度窗口）+ conn=idle -> 不是目标（旧代码会被"
            "非破坏性 peek 无限刷新）",
            targets == [],
            f"targets={targets}",
        )

    asyncio.run(scenario())


# ============================================================================
#  5. 面板：没有中继会话就什么都不显示
# ============================================================================

def test_panel_hides_the_empty_session_line():
    print("")
    print("---- 第 5 条：面板在线房间页不再显示「房间里暂时没有玩家。」----")

    source = open(
        os.path.join(ROOT, "panel", "web", "views", "rooms.php"),
        encoding="utf-8",
    ).read()

    check(
        "★ 「房间里暂时没有玩家。」已经不渲染了",
        "h(t('房间里暂时没有玩家。'))" not in source,
        "still rendered",
    )
    check(
        "★ 会话表改由「有会话才显示」把守",
        "if ($sessions !== [])" in source,
        "guard",
    )
    check(
        "★ 会话表自己的踢人表单还在（有会话时照旧允许踢人）",
        "action=session_kick" in source or "session_kick" in source,
        "session kick",
    )


# ============================================================================
#  真中继小工具
# ============================================================================

def _start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        # 版本门在开发开关下放行：这一套测的是房间 token，不是版本。
        "MCLINK_ALLOW_VERSION_MISMATCH": "1",
        "MCLINK_STATUS_INTERVAL": "30",
        "MCLINK_STATUS_FILE": os.path.join(HERE, "round3_status.json"),
    })

    log = open(
        os.path.join(HERE, "round3_relay.log"), "w",
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
            socket.create_connection(("127.0.0.1", RELAY_PORT), 1).close()
            return process, log
        except OSError:
            if process.poll() is not None:
                return process, log

            time.sleep(0.3)

    return process, log


class _Member:
    """一条成员控制连接（裸协议）。"""

    def __init__(self, name):
        self.name = name
        self.reader = None
        self.writer = None
        self.lines = asyncio.Queue()

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", RELAY_PORT
        )
        asyncio.create_task(self._pump())
        return self

    async def _pump(self):
        with contextlib.suppress(Exception):
            while True:
                line = await self.reader.readline()

                if not line:
                    return

                await self.lines.put(line.decode(errors="replace").strip())

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

            # HOST_STATE 之类的推送不消费掉也不影响：这里只要那一条应答。
            if line.upper().startswith("ERROR") and prefix.upper() != "ERROR":
                self.lines.put_nowait(line)
                continue

        return None

    async def close(self):
        with contextlib.suppress(Exception):
            self.writer.close()


# ============================================================================

def main():
    print("=" * 70)
    print(" 用户 2026-09-29（第三轮）第 1-6 条回归网")
    print("=" * 70)

    test_local_port_hot_switch()
    test_room_token_is_idempotent()
    test_member_list_uses_one_grid()
    test_punch_progress_text()
    test_client_drops_the_probe_when_minecraft_leaves()
    test_server_counts_people_not_probes()
    test_idle_rejoined_peer_is_not_a_target()
    test_punch_countdown_is_visible()
    test_gui_countdown_fast_path()
    test_panel_hides_the_empty_session_line()

    print("")
    print(f"passed: {len(PASSED)}   failed: {len(FAILED)}")

    if FAILED:
        print("failed checks:")

        for name in FAILED:
            print(f"  - {name}")

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
