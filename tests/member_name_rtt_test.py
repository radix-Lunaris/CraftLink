"""
CraftLink -- 成员真名 / 每成员中转延迟 / 对端延迟（客户端半边）

============================================================================
 这一套盯的四件事（用户 2026-09-26 的要求）
============================================================================

  1. **用真实用户名显示成员**：中继广播每人的显示名（登录名；免登录连接是它
     分配的「游客xxxxxx」）。客户端要显示它，而不是「未知」；老中继不发时
     回退到 **client_id**（绝不空、绝不「未知」）。自己也要看得见自己的名字。

  2. **每人的中转延迟始终有显示**：`rtts=` 是中继自己量出来的 per-member RTT；
     自己的用现有 PING/PONG 回声。拿不到 -> `-`，绝不编。

  3. **对端延迟（玩家↔主机）**：适用（STUN 直连且链路已打通）显示真实 RTT；
     不适用（中继模式 / 未连接 / 未打通）显示 **`N/C`**。这个数字来自隧道
     本来就有的 T_PING/T_PONG keepalive 往返（`udptunnel.py` 里只有只读插桩，
     协议与握手一步没动）。

  4. **显示口径统一 + 换 host 后按钮矩阵仍然正确**。

冻结的协议格式（协调者定，改中继的工程师同步）：
    names=<cid>:<name>,...    名字 percent-encoded，按第一个 `:` 切分
    rtts=<cid>:<ms>,...       ms 整数；没有数据的成员整条不出现
    OK JOIN/CREATE ... you=<name>   percent-encoded
中继还会反过来发 `PING <token>`，成员必须回 `PONG <token>`（冻结行为）。

Run:
    python tests/member_name_rtt_test.py
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "server"))

from mclink.core.room import (  # noqa: E402
    RoomSession, RoomState, parse_member_names, parse_member_rtts,
)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_MEMBERNAME_PORT", "19194"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    (PASSED if ok else FAILED).append(name)


GUEST = "游客ab12cd"          # 中继分配的游客名：游客 + 6 位 [a-z0-9]
GUEST_ENC = "%E6%B8%B8%E5%AE%A2ab12cd"


def _state(**kw):
    """一个和 RoomState 同形状的假房间状态。"""
    state = RoomState()
    state.room = kw.pop("room", "r1")
    state.room_name = kw.pop("room_name", "room-one")
    state.client_id = kw.pop("client_id", "cid-me")
    state.username = kw.pop("username", "")
    state.is_owner = kw.pop("is_owner", False)
    state.is_host = kw.pop("is_host", False)
    state.host_claimed = kw.pop("host_claimed", False)
    state.host_name = kw.pop("host_name", "")
    state.members = kw.pop("members", 1)
    state.peer_ids = list(kw.pop("peer_ids", []) or [])
    state.world_players = kw.pop("world_players", 0)
    state.member_names = dict(kw.pop("member_names", {}) or {})
    state.member_rtts = dict(kw.pop("member_rtts", {}) or {})
    state.self_name = kw.pop("self_name", "")

    for key, value in kw.items():
        setattr(state, key, value)

    return state


def _row(rows, key):
    for row in rows:
        if row.get("key") == key:
            return row

    return None


# ============================================================
# 1. 协议解析：names= / rtts= / you= / 老中继回退
# ============================================================

def test_member_table_parsers():
    print("")
    print("---- names= / rtts= 的容错解析（纯函数）----")

    names = parse_member_names(f"cid-a:alice,cid-b:{GUEST_ENC},cid-c:")

    check("★ names= 解析成 {cid: 名字}（按第一个 `:` 切分）",
          names.get("cid-a") == "alice" and names.get("cid-b") == GUEST,
          str(names))
    check("空名字的条目被跳过（回退 client_id，不显示空）",
          "cid-c" not in names, str(names))
    check("畸形的 names= 不会抛异常，也不产生垃圾条目",
          parse_member_names("nonsense") == {}
          and parse_member_names("") == {}
          and parse_member_names("-") == {})

    rtts = parse_member_rtts("cid-a:42,cid-b:0,cid-c:x,cid-a:43")

    check("★ rtts= 解析成 {cid: 毫秒}（0 也是有效值）",
          rtts.get("cid-a") == 43.0 and rtts.get("cid-b") == 0.0,
          str(rtts))
    check("看不懂的值被跳过（没有数据的成员整条不出现）",
          "cid-c" not in rtts, str(rtts))

    # ---- RoomState 的两个 helper：永远不为空 ----
    state = _state(client_id="cid-me", username="")
    state.member_names = {"cid-me": GUEST, "cid-a": "alice"}

    check("★ 自己的名字：中继分配的名字优先，游客也看得见自己",
          state.own_name == GUEST, state.own_name)
    check("★ 别人的名字来自广播表",
          state.name_of("cid-a") == "alice", state.name_of("cid-a"))
    check("★ 广播表里没有的成员 -> 回退 client_id（不是空、不是「未知」）",
          state.name_of("cid-z") == "cid-z", state.name_of("cid-z"))

    # ---- 老中继：一张表都没有 ----
    old = _state(client_id="cid-1", username="someone")
    check("老中继（没有 names=）-> 自己回退到登录用户名",
          old.own_name == "someone", old.own_name)

    guest_old = _state(client_id="cid-2", username="")
    check("老中继 + 免登录 -> 自己回退到 client_id（绝不空）",
          guest_old.own_name == "cid-2", guest_old.own_name)


def test_host_state_line_parsing():
    print("")
    print("---- HOST_STATE 行：names= / rtts= 进 RoomState ----")

    session = RoomSession("127.0.0.1", 1, "cid-me", "", "",
                          emit=lambda *a: None)
    session._on_line(
        "HOST_STATE host=1 host_name=alice members=3 world=1 "
        f"peers=cid-a,cid-b names=cid-a:alice,cid-b:{GUEST_ENC} "
        "rtts=cid-a:42,cid-b:88 mode=stun"
    )

    check("★ 广播里的显示名进了 state.member_names",
          session.state.member_names.get("cid-a") == "alice"
          and session.state.member_names.get("cid-b") == GUEST,
          str(session.state.member_names))
    check("★ 广播里的 per-member 中转延迟进了 state.member_rtts",
          session.state.member_rtts.get("cid-b") == 88.0,
          str(session.state.member_rtts))

    # ---- 老中继：同样的行没有那两个 token ----
    old = RoomSession("127.0.0.1", 1, "cid-me", "", "",
                      emit=lambda *a: None)
    old._on_line("HOST_STATE host=0 host_name=- members=2 world=0 "
                 "peers=cid-a")

    check("老中继不发 names=/rtts= -> 空表（界面走回退，不报错）",
          old.state.member_names == {} and old.state.member_rtts == {},
          f"{old.state.member_names} {old.state.member_rtts}")

    # 解析不碰别的字段（回归）
    check("解析 names=/rtts= 不影响原有的 peers=/host_name=/mode=",
          session.state.peer_ids == ["cid-a", "cid-b"]
          and session.state.host_name == "alice"
          and session.state.conn_mode == "stun",
          f"{session.state.peer_ids} {session.state.host_name} "
          f"{session.state.conn_mode}")


def test_ok_reply_you():
    print("")
    print("---- OK CREATE/JOIN 回包里的 you=（我自己的名字）----")

    class FakeWriter:
        def __init__(self):
            self.sent = b""

        def write(self, data):
            self.sent += data

        async def drain(self):
            return None

    class OnceReader:
        def __init__(self, line):
            self._line = line

        async def readline(self):
            if self._line is not None:
                line, self._line = self._line, None
                return line

            await asyncio.Event().wait()

    async def run(line):
        session = RoomSession("127.0.0.1", 1, "cid-me", "", "",
                              emit=lambda *a: None)
        session.writer = FakeWriter()
        session.reader = OnceReader(line)

        async def fake_connect():
            return None

        session._connect = fake_connect

        try:
            await session._request("JOIN", "room-one", "OK JOIN")
        finally:
            with contextlib.suppress(Exception):
                await session.stop()

        return session

    session = asyncio.run(run(
        f"OK JOIN room=r1 name=room-one owner=0 host=0 members=2 "
        f"you={GUEST_ENC}\n".encode()
    ))
    check("★ OK JOIN 的 you= 被解析成自己的显示名（percent-decoded）",
          session.state.self_name == GUEST, session.state.self_name)

    old = asyncio.run(run(
        b"OK JOIN room=r1 name=room-one owner=0 host=0 members=1\n"
    ))
    check("老中继的 OK 回包没有 you= -> self_name 保持空（回退到用户名/cid）",
          old.state.self_name == "", old.state.self_name)


def test_relay_ping_is_answered():
    print("")
    print("---- 中继反过来 PING 我们：必须回 PONG（冻结行为）----")

    class FakeWriter:
        def __init__(self):
            self.sent = b""

        def write(self, data):
            self.sent += data

        async def drain(self):
            return None

    session = RoomSession("127.0.0.1", 1, "cid-me", "", "",
                          emit=lambda *a: None)
    session.writer = FakeWriter()

    session._on_line("PING 1720000000.123")

    check("★ 收到中继 PING <token> -> 原样回 PONG <token>",
          session.writer.sent == b"PONG 1720000000.123\n",
          repr(session.writer.sent))

    session.writer.sent = b""
    session._on_line("PING")

    check("没有 token 的 PING 也要回（回裸 PONG，不带尾空格）",
          session.writer.sent == b"PONG\n", repr(session.writer.sent))

    # 自己的 PONG 处理没有被破坏
    session._on_line("PONG 999.0")
    check("中继回的 PONG 仍然用于本机中转延迟（回归）",
          session.relay_rtt_ms is not None and session.relay_rtt_ms >= 0,
          str(session.relay_rtt_ms))


# ============================================================
# 2. 名单数据：真名 / 游客 / 每成员中转延迟 / 对端延迟 N-C
# ============================================================

def test_roster_builder():
    print("")
    print("---- 名单数据：真名 / 游客 / 每成员中转延迟 / 对端延迟 ----")

    import importlib

    gui_app = importlib.import_module("mclink.gui.app")

    # ---- 中继广播了名字：每人显示真名 ----
    state = _state(
        client_id="cid-owner", username="", self_name="房主小明",
        is_owner=True, host_claimed=True, host_name="房主小明", members=3,
        peer_ids=["cid-a", "cid-b"],
        member_names={"cid-owner": "房主小明", "cid-a": "alice",
                      "cid-b": GUEST},
        member_rtts={"cid-owner": 12, "cid-a": 42, "cid-b": 88},
    )
    rows = gui_app.build_member_rows(state, self_ping_ms=12.0)

    me = _row(rows, "cid-owner")
    peer_a = _row(rows, "cid-a")
    peer_b = _row(rows, "cid-b")

    check("★ 自己的名字用中继分配的真名（免登录也有名字）",
          me and me["name"] == "房主小明", str(me))
    check("★ 别人的名字用中继广播的真名",
          peer_a and peer_a["name"] == "alice", str(peer_a))
    check("★ 游客显示中继分配的「游客xxxxxx」",
          peer_b and peer_b["name"] == GUEST, str(peer_b))

    check("★ 每人的中转延迟：自己的用本机真实 RTT，别人的用中继广播值",
          me and me["ping_ms"] == 12.0
          and peer_a and peer_a["ping_ms"] == 42.0
          and peer_b and peer_b["ping_ms"] == 88.0,
          str([(r["key"], r["ping_ms"]) for r in rows]))

    # ---- 老中继：没有名字表 / 没有 rtts ----
    old = _state(
        client_id="cid-owner", username="owner-name",
        is_owner=True, members=2, peer_ids=["cid-a"],
    )
    rows = gui_app.build_member_rows(old, self_ping_ms=7.0)
    old_me = _row(rows, "cid-owner")
    old_peer = _row(rows, "cid-a")

    check("★ 老中继：自己回退到登录用户名（不是「未知」）",
          old_me and old_me["name"] == "owner-name", str(old_me))
    check("★ 老中继：对端回退到 client_id（不是「未知」、不是空）",
          old_peer and old_peer["name"] == "cid-a", str(old_peer))
    check("老中继：对端的中转延迟拿不到 -> None（渲染层 `-`）",
          old_peer and old_peer["ping_ms"] is None, str(old_peer))

    # ---- 历史 host_name 回退：老中继只广播房主名字 ----
    hostish = _state(
        client_id="cid-owner2", username="owner2", is_owner=True,
        host_claimed=True, host_name="真主机名", members=2, peer_ids=["cid-h"],
    )
    direct = [{
        "peer": "cid-h", "addr": "", "ready": False, "attempts": 0,
        "peer_is_host": True, "nat": None, "filtering": None, "rtt_ms": None,
    }]
    rows = gui_app.build_member_rows(hostish, direct_rows=direct)
    host_row = _row(rows, "cid-h")

    check("★ 老中继 + 房主：回退用 host_name=（老中继唯一的真用户名）",
          host_row and host_row["name"] == "真主机名", str(host_row))

    # ---- 对端延迟：适用 / 不适用 / 适用但没测出来 ----
    state = _state(
        client_id="cid-owner", username="owner", is_owner=True, members=3,
        peer_ids=["cid-ready", "cid-punching", "cid-num"],
        member_names={"cid-owner": "owner"},
    )
    direct = [
        # 已打通但还没测到数字（第一次 keepalive 往返还没回来）
        {"peer": "cid-ready", "addr": "1.2.3.4:1", "ready": True,
         "attempts": 0, "peer_is_host": False, "nat": None,
         "filtering": None, "rtt_ms": None},
        # 洞还没打通 -> 不适用
        {"peer": "cid-punching", "addr": "", "ready": False, "attempts": 2,
         "peer_is_host": False, "nat": None, "filtering": None,
         "rtt_ms": None},
        # 已打通且量到了真实数字
        {"peer": "cid-num", "addr": "5.6.7.8:2", "ready": True,
         "attempts": 0, "peer_is_host": False, "nat": None,
         "filtering": None, "rtt_ms": 34.4},
    ]
    rows = gui_app.build_member_rows(state, direct_rows=direct)

    ready = _row(rows, "cid-ready")
    punching = _row(rows, "cid-punching")
    numbered = _row(rows, "cid-num")

    check("★ 已打通但没测到 -> 适用(True) 且 rtt None（渲染层 `-`）",
          ready and ready["peer_direct"] is True
          and ready["peer_rtt_ms"] is None, str(ready))
    check("★ 没打通 -> 不适用（渲染层 N/C）",
          punching and punching["peer_direct"] is False, str(punching))
    check("★ 已打通且有数字 -> 真实 RTT 进名单",
          numbered and numbered["peer_direct"] is True
          and numbered["peer_rtt_ms"] == 34.4, str(numbered))
    check("自己那一行的对端延迟不适用（没有对端）",
          _row(rows, "cid-owner")["peer_direct"] is False)

    # ---- 显示口径（纯函数）----
    check("★ 口径：不适用 -> N/C",
          gui_app.peer_latency_text(None, False) == "N/C"
          and gui_app.peer_latency_text(34.0, False) == "N/C")
    check("★ 口径：适用但没数字 -> `-`（不是 N/C，也不是编的数字）",
          gui_app.peer_latency_text(None, True) == "-")
    check("★ 口径：适用且有数字 -> `NN ms`",
          gui_app.peer_latency_text(34.4, True) == "34 ms",
          gui_app.peer_latency_text(34.4, True))


def test_host_transfer_matrix():
    print("")
    print("---- host 换人后按钮矩阵仍然正确（回归 + 新字段一起走）----")

    import importlib

    gui_app = importlib.import_module("mclink.gui.app")

    room = ("cid-owner", "cid-A", "cid-B")
    names = {"cid-owner": "房主", "cid-A": "A", "cid-B": "B"}

    def state_for(me, is_host, host_peer=None, host_claimed=True,
                  host_name=""):
        state = _state(
            client_id=me, username=me, is_owner=False, is_host=is_host,
            host_claimed=host_claimed, host_name=host_name, members=3,
            peer_ids=[p for p in room if p != me],
            member_names=names,
            # 2026-09-28（规则 6 §A）：Owner 只认中继的 owner_id=，
            # 不再"花名册第一个就是房主"。
            owner_id="cid-owner",
        )
        direct = []

        if host_peer:
            direct = [{
                "peer": host_peer, "addr": "9.9.9.9:9", "ready": True,
                "attempts": 0, "peer_is_host": True, "nat": None,
                "filtering": None, "rtt_ms": 21.0,
            }]

        return state, direct

    state, direct = state_for("cid-A", True, host_name="A")
    rows = gui_app.build_member_rows(state, direct_rows=direct)

    check("A 在任：能踢 B、不能踢房主",
          _row(rows, "cid-B")["can_kick"] is True
          and _row(rows, "cid-owner")["can_kick"] is False)
    check("A 在任：真名进名单（房主 / A / B 都不是 cid）",
          {r["key"]: r["name"] for r in rows}
          == {"cid-A": "A", "cid-owner": "房主", "cid-B": "B"},
          str({r["key"]: r["name"] for r in rows}))

    # host 转给 B：A 卸任
    state, direct = state_for("cid-A", False, host_peer="cid-B", host_name="B")
    rows = gui_app.build_member_rows(state, direct_rows=direct)

    check("★ A 卸任后：所有踢人按钮立刻消失（权限不缓存）",
          all(not r["can_kick"] for r in rows if not r["is_self"]),
          str([(r["key"], r["can_kick"]) for r in rows]))
    check("★ A 卸任后：B 那行身份 = 主机，且它的对端延迟数字照常显示",
          _row(rows, "cid-B")["role"] == gui_app.ROLE_HOST
          and _row(rows, "cid-B")["peer_rtt_ms"] == 21.0
          and _row(rows, "cid-B")["peer_direct"] is True,
          str(_row(rows, "cid-B")))

    state, direct = state_for("cid-B", True, host_name="B")
    rows = gui_app.build_member_rows(state, direct_rows=direct)

    check("★ B 接任：能踢 A，仍然不能踢房主",
          _row(rows, "cid-A")["can_kick"] is True
          and _row(rows, "cid-owner")["can_kick"] is False)


# ============================================================
# 3. 真界面（Tk）：两列延迟 / 状态面板 / 换人
# ============================================================

def _window_texts(win):
    found = []

    def visit(node):
        with contextlib.suppress(Exception):
            klass = node.winfo_class()

            if klass not in ("Text", "Entry", "TEntry", "Listbox"):
                try:
                    bound = str(node.cget("textvariable") or "")
                except Exception:
                    bound = ""

                if not bound:
                    try:
                        text = str(node.cget("text") or "")
                    except Exception:
                        text = ""

                    if text.strip():
                        found.append(text)

        for child in node.winfo_children():
            visit(child)

    visit(win)

    return found


def test_gui():
    print("")
    print("---- 真界面：成员列表两列延迟 + 状态面板「对端延迟」----")

    try:
        import tkinter as tk
    except ImportError as e:  # pragma: no cover
        check("tkinter available", False, str(e))
        return

    sandbox = tempfile.mkdtemp(prefix="mclink-name-rtt-")
    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox

    from mclink.gui import app as gui_app
    from mclink.gui import i18n

    gui_app.messagebox.showwarning = lambda *a, **k: None
    gui_app.messagebox.showerror = lambda *a, **k: None
    gui_app.messagebox.showinfo = lambda *a, **k: None
    gui_app.messagebox.askokcancel = lambda *a, **k: True
    gui_app.messagebox.askyesno = lambda *a, **k: True

    try:
        root = tk.Tk()
    except Exception as e:  # pragma: no cover
        check("Tk root can be created (display available)", False, repr(e))
        return

    try:
        root.withdraw()
        i18n.set_language("zh")

        application = gui_app.MCLinkApp(root)

        class FakeEngine:
            def __init__(self, engine):
                self._engine = engine
                self.state = None
                self.direct = {}
                self.profile = None
                self.ping = None
                self.snapshot_data = None

            def room_state(self):
                return self.state

            def room_direct_state(self):
                return {"links": list(self.direct.get("links") or [])}

            def room_nat_profile(self):
                return self.profile

            def room_rtt_ms(self):
                return self.ping

            def snapshot(self):
                return self.snapshot_data or {"state": "idle", "role": None}

            def __getattr__(self, name):
                return getattr(self._engine, name)

        fake = FakeEngine(application.engine)
        application.engine = fake

        state = _state(
            client_id="cid-me", username="", self_name=GUEST,
            is_owner=True, host_claimed=True, host_name="alice", members=3,
            peer_ids=["cid-a", "cid-b"], room="r-gui", room_name="gui-room",
            member_names={"cid-me": GUEST, "cid-a": "alice",
                          "cid-b": "bob"},
            member_rtts={"cid-me": 15, "cid-a": 42, "cid-b": 88},
        )
        fake.state = state
        fake.ping = 15.0
        fake.direct = {"links": [
            {"peer": "cid-a", "addr": "1.2.3.4:5000", "ready": True,
             "attempts": 0, "peer_is_host": False, "nat": None,
             "filtering": None, "rtt_ms": 34.2},
            {"peer": "cid-b", "addr": "", "ready": False, "attempts": 3,
             "peer_is_host": False, "nat": None, "filtering": None,
             "rtt_ms": None},
        ]}

        application._open_member_window()
        root.update_idletasks()
        application._refresh_member_list(force=True)
        root.update_idletasks()

        cells_a = application.member_rows["cid-a"]["vars"]
        cells_b = application.member_rows["cid-b"]["vars"]

        check("★ [GUI] 成员列表显示中继广播的真名",
              application.member_rows["cid-a"]["vars"]["name"].get() == "alice"
              and application.member_rows["cid-b"]["vars"]["name"].get() == "bob",
              str([application.member_rows[k]["vars"]["name"].get()
                   for k in application.member_rows]))
        check("★ [GUI] 自己那行显示中继分配的游客名",
              application.member_rows["cid-me"]["vars"]["name"].get() == GUEST,
              application.member_rows["cid-me"]["vars"]["name"].get())
        check("★ [GUI] 每人的中转延迟都显示（自己的 / 别人的）",
              application.member_rows["cid-me"]["vars"]["ping"].get() == "15 ms"
              and cells_a["ping"].get() == "42 ms"
              and cells_b["ping"].get() == "88 ms",
              str({k: application.member_rows[k]["vars"]["ping"].get()
                   for k in application.member_rows}))
        check("★ [GUI] 对端延迟：已打通有点对点数字",
              cells_a["peer_ping"].get() == "34 ms",
              cells_a["peer_ping"].get())
        check("★ [GUI] 对端延迟：没打通 -> N/C",
              cells_b["peer_ping"].get() == "N/C",
              cells_b["peer_ping"].get())
        check("★ [GUI] 自己那行的对端延迟 -> N/C（没有对端）",
              application.member_rows["cid-me"]["vars"]["peer_ping"].get()
              == "N/C",
              application.member_rows["cid-me"]["vars"]["peer_ping"].get())

        # ---- 成员窗口表头有「对端延迟」且 EN 下不漏中文 ----
        headers = [t for t in _window_texts(application.member_window)]

        check("成员窗口表头有「对端延迟」",
              "对端延迟" in headers, str(headers))

        application.language_var.set("English")
        application.on_language_change()
        root.update_idletasks()
        # 切语言会重建整棵界面树，成员窗口跟着被关掉 —— 重新打开再检查。
        application._open_member_window()
        root.update_idletasks()
        application._refresh_member_list(force=True)
        root.update_idletasks()

        leftovers = [t for t in _window_texts(application.member_window)
                     if i18n.has_cjk(t)]

        check("★ [en] 成员窗口（含新列）没有残留中文",
              not leftovers, "漏翻：" + " | ".join(leftovers[:8]))
        check("[en] N/C 三语一致（符号不翻译）",
              application.member_rows["cid-b"]["vars"]["peer_ping"].get()
              == "N/C",
              application.member_rows["cid-b"]["vars"]["peer_ping"].get())

        i18n.set_language("zh")
        application.language_var.set("中文")
        application.on_language_change()
        root.update_idletasks()
        application._open_member_window()
        root.update_idletasks()

        # ---- 运行状态卡片：中转延迟 + 对端延迟 ----
        print("")
        print("---- 运行状态卡片：中转延迟 + 对端延迟（适用/不适用）----")

        fake.snapshot_data = {"state": "idle", "role": None}
        application._refresh_status()
        root.update_idletasks()

        check("★ [卡片] 中转延迟始终有显示（房间 PING/PONG 兜底）",
              "15" in application.status_vars["ping"].get(),
              application.status_vars["ping"].get())
        check("★ [卡片] 对端延迟：有已打通的直连 -> 真实数字",
              application.status_vars["peer_ping"].get() == "34 ms",
              application.status_vars["peer_ping"].get())

        # 没有任何直连 -> N/C
        fake.direct = {"links": [
            {"peer": "cid-b", "addr": "", "ready": False, "attempts": 1,
             "peer_is_host": False, "nat": None, "filtering": None,
             "rtt_ms": None},
        ]}

        application._refresh_status()
        root.update_idletasks()

        check("★ [卡片] 对端延迟：没有直连 -> N/C（不拿中转数字冒充）",
              application.status_vars["peer_ping"].get() == "N/C",
              application.status_vars["peer_ping"].get())

        # 已打通但还没测到 -> `-`
        fake.direct = {"links": [
            {"peer": "cid-b", "addr": "1.2.3.4:9", "ready": True,
             "attempts": 0, "peer_is_host": False, "nat": None,
             "filtering": None, "rtt_ms": None},
        ]}

        application._refresh_status()
        root.update_idletasks()

        check("★ [卡片] 对端延迟：适用但还没测到 -> `-`（不是 N/C，也不编）",
              application.status_vars["peer_ping"].get() == "-",
              application.status_vars["peer_ping"].get())

        # ---- 一人一行：对端行带真名 + 对端延迟 ----
        print("")
        print("---- 运行状态卡片：一人一行带真名 + 对端延迟 ----")

        fake.direct = {"links": [
            {"peer": "cid-a", "addr": "1.2.3.4:5000", "ready": True,
             "attempts": 0, "peer_is_host": False, "nat": None,
             "filtering": None, "rtt_ms": 34.2},
            {"peer": "cid-b", "addr": "", "ready": False, "attempts": 2,
             "peer_is_host": False, "nat": None, "filtering": None,
             "rtt_ms": None},
        ]}

        application._render_hole_rows(fake.direct["links"])
        root.update_idletasks()

        texts = [label.cget("text")
                 for _row, label in application._hole_rows.values()]

        check("★ 一人一行用中继真名（不是 client_id）",
              any("alice" in t for t in texts)
              and any("bob" in t for t in texts),
              str(texts))
        check("★ 已打通行带对端延迟数字、未打通行带 N/C",
              any("34 ms" in t for t in texts)
              and any("N/C" in t for t in texts),
              str(texts))

        # ---- 换 host：同一个窗口，按钮矩阵实时跟着换 ----
        print("")
        print("---- 真界面：host 换人后按钮矩阵实时跟换 ----")

        fake.direct = {}
        fake.state = _state(
            client_id="cid-A", username="A", self_name="A", is_host=True,
            is_owner=False, host_claimed=True, host_name="A", members=3,
            peer_ids=["cid-owner", "cid-B"], room="r-gui", room_name="gui",
            member_names={"cid-A": "A", "cid-owner": "房主", "cid-B": "B"},
            owner_id="cid-owner",
        )
        application._refresh_member_list(force=True)
        root.update_idletasks()

        check("host=A：A 能踢 B、不能踢房主",
              application.member_rows["cid-B"]["kick_visible"] is True
              and application.member_rows["cid-owner"]["kick_visible"] is False)

        fake.state = _state(
            client_id="cid-A", username="A", self_name="A", is_host=False,
            is_owner=False, host_claimed=True, host_name="B", members=3,
            peer_ids=["cid-owner", "cid-B"], room="r-gui", room_name="gui",
            member_names={"cid-A": "A", "cid-owner": "房主", "cid-B": "B"},
        )
        fake.direct = {"links": [
            {"peer": "cid-B", "addr": "9.9.9.9:9", "ready": True,
             "attempts": 0, "peer_is_host": True, "nat": None,
             "filtering": None, "rtt_ms": 21.0},
        ]}
        application._refresh_member_list(force=True)
        root.update_idletasks()

        check("★ A 卸任后（同一窗口）：按钮全消失",
              all(not application.member_rows[k]["kick_visible"]
                  for k in application.member_rows),
              str({k: application.member_rows[k]["kick_visible"]
                   for k in application.member_rows}))
        check("★ A 卸任后：B 那行 = 主机 + 对端延迟 21 ms"
              "（用户 2026-09-29：身份是组合角色）",
              application.member_rows["cid-B"]["vars"]["role"].get()
              == "主机"
              and application.member_rows["cid-B"]["vars"]["peer_ping"].get()
              == "21 ms",
              str(application.member_rows["cid-B"]["vars"]["role"].get()))

        application._close_member_window()

    except Exception:
        check("成员真名 / 延迟界面测试完成且没有抛异常", False)
        traceback.print_exc()
    else:
        check("成员真名 / 延迟界面测试完成且没有抛异常", True)
    finally:
        with contextlib.suppress(Exception):
            root.destroy()

        i18n.set_language("zh")


# ============================================================
# 4. 真中继 + 真 RoomSession：名字 / 游客名 / 每成员中转延迟
# ============================================================

def _start_relay():
    env = dict(os.environ)
    env.update({
        "MCLINK_PORT": str(RELAY_PORT),
        "MCLINK_DB_ENABLED": "0",
        "MCLINK_REQUIRE_ACCOUNT": "0",
        "MCLINK_STUN_ENABLED": "0",
        "MCLINK_STATUS_FILE": os.path.join(HERE, "member_name_status.json"),
        "MCLINK_STATUS_INTERVAL": "5",
        "MCLINK_VERBOSE": "0",
    })

    log = open(
        os.path.join(HERE, "member_name_relay.log"), "w",
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


async def _open(cid, name, events, room, create=False):
    session = RoomSession(
        "127.0.0.1", RELAY_PORT, cid, name, "",
        emit=lambda kind, data, c=cid: events.append((c, kind, data)),
    )

    if create:
        await session.create(room)
    else:
        await session.join(room)

    return session


async def test_real_relay():
    print("")
    print("---- 真中继 + 真客户端：真名 / 游客名 / 每成员中转延迟 ----")

    import re

    guest_re = re.compile(r"^游客[0-9a-z]{6}$")

    relay, log = _start_relay()
    sessions = []
    events = []

    try:
        if relay.poll() is not None:
            check("中继启动", False, "进程已退出，见 tests/member_name_relay.log")
            return

        check("中继启动", True)

        room = "member-name-room"

        owner = await _open("cid-owner", "房主甲", events, room, create=True)
        guest = await _open("cid-guest", "", events, room)
        sessions = [owner, guest]

        check("★ OK CREATE/JOIN 带回了中继给我的显示名（you=）",
              owner.state.self_name == "房主甲",
              owner.state.self_name)

        # 等中继把 HOST_STATE / names= / rtts= 推齐。RTT 探测 0.3 秒后开始，
        # 每 3 秒一轮，所以给足几秒。
        deadline = time.monotonic() + 15

        while time.monotonic() < deadline:
            if (
                owner.state.member_names.get("cid-guest")
                and owner.state.member_names.get("cid-owner")
                and owner.state.member_rtts.get("cid-owner") is not None
                and owner.state.member_rtts.get("cid-guest") is not None
                and guest.state.member_rtts.get("cid-owner") is not None
            ):
                break

            await asyncio.sleep(0.3)

        names = owner.state.member_names
        rtts = owner.state.member_rtts

        check("★ 真中继广播每个成员的登录名（不再只有 client_id）",
              names.get("cid-owner") == "房主甲"
              and names.get("cid-guest") is not None,
              str(names))
        check("★ 免登录成员拿到中继分配的「游客xxxxxx」",
              bool(guest_re.match(names.get("cid-guest") or "")),
              str(names.get("cid-guest")))
        check("★ 游客自己也看得见自己被分配的名字（自己那行 / 房间卡片）",
              bool(guest_re.match(guest.state.own_name or ""))
              and guest.state.own_name == names.get("cid-guest"),
              f"{guest.state.own_name} / {names.get('cid-guest')}")

        check("★ 真中继广播 per-member 中转延迟（客户端回了 PONG 才有）",
              isinstance(rtts.get("cid-owner"), float)
              and isinstance(rtts.get("cid-guest"), float)
              and rtts.get("cid-owner") >= 0.0,
              str(rtts))
        check("★ 游客侧也看得到房主的每成员中转延迟（同一张表）",
              isinstance(guest.state.member_rtts.get("cid-owner"), float),
              str(guest.state.member_rtts))

        # ---- 名单数据端到端：真名 + 每成员延迟进成员列表 ----
        import importlib

        gui_app = importlib.import_module("mclink.gui.app")

        rows = gui_app.build_member_rows(
            owner.state,
            direct_rows=[],
            self_ping_ms=owner.relay_rtt_ms,
        )
        names_in_rows = {row["key"]: row["name"] for row in rows}

        check("★ 成员列表端到端用真名（自己 + 游客）",
              names_in_rows.get("cid-owner") == "房主甲"
              and guest_re.match(names_in_rows.get("cid-guest") or ""),
              str(names_in_rows))
        check("★ 成员列表每一行都有中转延迟（自己的 / 别人的）",
              all(row["ping_ms"] is not None for row in rows),
              str([(r["key"], r["ping_ms"]) for r in rows]))
        check("★ 没有直连时对端延迟不适用（渲染层 N/C）",
              all(row["peer_direct"] is False for row in rows),
              str([(r["key"], r["peer_direct"]) for r in rows]))
    finally:
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.stop()

        with contextlib.suppress(Exception):
            relay.terminate()
            relay.wait(timeout=8)

        with contextlib.suppress(Exception):
            log.close()


# ============================================================

def main():
    print("=" * 64)
    print("CraftLink 客户端：成员真名 / 每成员中转延迟 / 对端延迟")
    print("=" * 64)

    test_member_table_parsers()
    test_host_state_line_parsing()
    test_ok_reply_you()
    test_relay_ping_is_answered()
    test_roster_builder()
    test_host_transfer_matrix()

    try:
        asyncio.run(test_real_relay())
    except Exception:
        check("真中继名字/延迟测试没有抛异常", False)
        traceback.print_exc()
    else:
        check("真中继名字/延迟测试没有抛异常", True)

    test_gui()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
