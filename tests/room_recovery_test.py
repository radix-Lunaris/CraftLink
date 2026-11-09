"""
CraftLink -- 基准配置 / 房间参数锁定 的回归测试。

============================================================================
 用户定的规则（2026-09-15，原话）
============================================================================

1. 「身份配置选择器只展示页面，不干涉用户身份操作」→ 后来直接要求**删掉它**：
   「把整个身份配置那个东西做成基准配置，把主机 host 和玩家 player 这个
     选择器直接移除掉，免得它去影响到操作那一栏。」

2. 「房间强绑定房主的参数，人数和 host 代理端口全部按房主的来……房间人数和
   host 代理端口，建房后锁定，就是房主本人和服务器管理员都不允许修改，
   这东西一改联机全掉了。」

3. 「玩家本地联机端口……如果它不涉及隧道那些，只是一个本地配置那就允许。」
   （查证：本地端口和隧道无关 —— 隧道数量只由 max_players 推导，房主也不用它。）

4. 「每次修改的话都必须要按一下，就输入完新的之后不会立即生效，按右边的提交。」

============================================================================
 这套测试守什么
============================================================================

  * 身份选择器**真的没了**（没有 role_var / 不再有"身份决定端口"这种事）
  * 三个数字全部平铺可配，每个旁边有「确定」
  * 改输入框**不会**立即生效，必须提交
  * 建房/开服之后：mc_port 和 max_players 拒绝修改；local_port 允许
  * 隧道数量永远跟着"房间容量"，不是本机输入框

Run:
    python tests/room_recovery_test.py
"""

import contextlib
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

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
# 最小替身：借 MCLinkApp 的真方法，不复制逻辑
# ============================================================

class FakeRoomState:
    def __init__(self, **kwargs):
        self.room = kwargs.get("room", "testroom")
        self.room_name = kwargs.get("room_name", "测试房间")
        self.is_owner = kwargs.get("is_owner", False)
        self.is_host = kwargs.get("is_host", False)
        self.host_claimed = kwargs.get("host_claimed", False)
        self.host_name = kwargs.get("host_name", "")
        self.members = kwargs.get("members", 1)
        self.world_players = kwargs.get("world_players", 0)
        self.max_players = kwargs.get("max_players", 0)
        self.server_max_players = kwargs.get("server_max_players", 8)
        self.revoked = kwargs.get("revoked", False)
        self.revoked_reason = kwargs.get("revoked_reason", "")


class FakeEngine:
    def __init__(self, state=None):
        self._state = state

    def room_state(self):
        return self._state


class FakeVar:
    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class FakeApp:
    """只实现被测方法真正用到的东西。"""

    def __init__(self, state=None, mc_port="", local_port="", max_players=""):
        import types

        from mclink.config import settings as cfgmod
        from mclink.gui.app import MCLinkApp

        self.engine = FakeEngine(state)
        self.config = cfgmod.ClientConfig()
        self.running = False
        self._committed = {}

        self.mc_port_var = FakeVar(mc_port)
        self.local_port_var = FakeVar(local_port)
        self.max_players_var = FakeVar(max_players)

        # 借真方法（用 MethodType 真正绑定；别自己拼闭包 —— 第一版就是那样
        # 把 self 传重了，报 "missing 1 required positional argument"）。
        for name in (
            "_decide_connect_role",
            "_room_capacity",
            "_config_locked",
            "_lock_key",
            "_is_config_dirty",
            "_config_key_of",
            "_committed_value",
            "_configured_max_players",
            "_collect_config",
            "_commit_port",
            "_commit_max_players",
            "_config_locked_notice",
            "_on_config_edited",
            "_sync_config_buttons",
            "_set_row_status",
            "_mark_row_blocked",
            "_restore_row_hint",
        ):
            setattr(
                self, name, types.MethodType(getattr(MCLinkApp, name), self)
            )

        # 提交路径会读这几个界面变量 / 类属性
        self.LOCKED_AFTER_CREATE = MCLinkApp.LOCKED_AFTER_CREATE
        self.root = None  # messagebox(parent=self.root) 需要一个值
        self._config_rows = []
        self.log_messages = []
        self.log = lambda message, kind="info": self.log_messages.append(
            str(message)
        )
        self._set_enabled = lambda *a, **k: None
        # 弹窗一律"点确定"，测试环境没人点按钮。
        import mclink.gui.app as gui_app

        gui_app.messagebox.showwarning = lambda *a, **k: "ok"
        gui_app.messagebox.showerror = lambda *a, **k: "ok"
        gui_app.messagebox.askokcancel = lambda *a, **k: True


def make_app(**kwargs):
    app = FakeApp(**kwargs)

    # _collect_config 还要读中转服务器地址/端口和房间名
    app.host_var = FakeVar("127.0.0.1")
    app.port_var = FakeVar("19100")
    app.username_var = FakeVar("tester")
    app.client_id_var = FakeVar("client-1")
    app._active_room_name = lambda: "testroom"

    return app


# ============================================================
# 1. 身份选择器真的没了
# ============================================================

def test_identity_selector_is_gone():
    print("")
    print("-" * 64)
    print("1. 身份选择器已删除（用户要求）")
    print("-" * 64)

    import inspect

    from mclink.gui import app as gui_app

    source = inspect.getsource(gui_app.MCLinkApp)

    check(
        "代码里已经没有 role_var（选择器被整个移除）",
        "role_var" not in source,
        "还在引用 role_var",
    )

    # 选择器曾经决定"显示哪个端口框"，那是它影响开服的根源；两个端口框
    # 都必须一直存在、一直可见。
    build_source = inspect.getsource(gui_app.MCLinkApp._build_role_card)

    check(
        "两个端口框都在「基准配置」里平铺（不再二选一）",
        "mc_port_var" in build_source and "local_port_var" in build_source,
        "少了某个端口输入框",
    )

    check(
        "卡片标题改成「基准配置」（不再叫「身份配置」）",
        '"基准配置"' in build_source,
        "标题没改",
    )

    # 每个数字旁边一个确定按钮
    check(
        "三个数字各有一个「确定」（走 _build_config_row）",
        build_source.count("_build_config_row(") >= 3,
        f"调用次数={build_source.count('_build_config_row(')}",
    )

    # 旧的两行歧义提示（主机模式：… / 玩家模式：…）必须彻底消失
    role_change_source = inspect.getsource(gui_app.MCLinkApp._on_role_change)
    code = re.sub(r"#[^\n]*", "", role_change_source)

    check(
        "不再有「主机模式：…」「玩家模式：…」那种歧义文案",
        "主机模式：" not in code and "玩家模式：" not in code,
        "歧义文案又回来了",
    )


# ============================================================
# 2. 改完必须按「确定」才生效
# ============================================================

def test_changes_need_explicit_commit():
    print("")
    print("-" * 64)
    print("2. 改输入框不会立即生效，必须按「确定」")
    print("-" * 64)

    # 注意 state=None：这个用例测的是"提交"本身，所以必须**不在房间里**
    # （在房间里 mc_port 是锁定的，提交会被拒 —— 那条规则由下一个用例验证）。
    app = make_app(
        state=None,
        mc_port="25565",
        local_port="25566",
        max_players="8",
    )

    # 模拟"上次提交过"的状态
    app._committed = {
        "mc_port": "25565",
        "local_port": "25566",
        "max_players": "8",
    }

    # 输入框里改成 30000，但**没有**按确定
    app.mc_port_var.set("30000")

    check(
        "改了没提交 → 连接时用的还是旧值（25565）",
        app._committed_value("mc_port") == 25565,
        f"committed_value={app._committed_value('mc_port')}",
    )

    check(
        "而且界面知道这个框是待提交状态",
        app._is_config_dirty(app.mc_port_var) is True,
        "没有识别出未提交的改动",
    )

    # 按确定（提交）
    app._commit_port("mc_port", app.mc_port_var)

    check(
        "按了确定之后新值才生效（30000）",
        app._committed_value("mc_port") == 30000,
        f"committed_value={app._committed_value('mc_port')}",
    )

    check(
        "提交之后不再是待提交状态",
        app._is_config_dirty(app.mc_port_var) is False,
        "",
    )

    # 非法值必须被拒绝，且不能污染已提交的值
    app.mc_port_var.set("abc")
    app._commit_port("mc_port", app.mc_port_var)

    check(
        "提交非数字端口被拒绝，已提交的值保持 30000",
        app._committed_value("mc_port") == 30000,
        f"committed_value={app._committed_value('mc_port')}",
    )

    app.mc_port_var.set("99999")
    app._commit_port("mc_port", app.mc_port_var)

    check(
        "提交超范围端口被拒绝，已提交的值保持 30000",
        app._committed_value("mc_port") == 30000,
        f"committed_value={app._committed_value('mc_port')}",
    )


# ============================================================
# 3. 建房之后锁定：端口 + 人数不许改；本地端口允许
# ============================================================

def test_room_params_locked_after_create():
    print("")
    print("-" * 64)
    print("3. 建房后：host 端口 + 人数锁定；玩家本地端口允许改")
    print("-" * 64)

    in_room = FakeRoomState(is_host=True, host_claimed=True, max_players=8)

    app = make_app(
        state=in_room,
        mc_port="25565",
        local_port="25566",
        max_players="8",
    )
    app._committed = {
        "mc_port": "25565",
        "local_port": "25566",
        "max_players": "8",
    }

    check(
        "房间里 → host 局域网端口被锁定",
        app._config_locked("mc_port") is True,
        "没锁住 host 端口（改了两批隧道同时活着）",
    )

    check(
        "房间里 → 人数上限被锁定",
        app._config_locked("max_players") is True,
        "没锁住人数",
    )

    check(
        "房间里 → 玩家本地端口**不锁**（它和隧道无关，房主也不用它）",
        app._config_locked("local_port") is False,
        "本地端口被误锁了",
    )

    # 真的去提交被锁的那个：值不能变
    app.mc_port_var.set("30000")
    app._commit_port("mc_port", app.mc_port_var)

    check(
        "被锁时提交 host 端口：值不变（还是 25565）",
        app._committed_value("mc_port") == 25565,
        f"committed_value={app._committed_value('mc_port')}",
    )

    # The hint must SAY both things: that it is locked, and that the way out is
    # to delete the room and build it again. The wording is English because the
    # LOG is English by design (it is the artefact the owner analyses); the
    # Chinese wording lives in the dialog, which is the UI's business.
    check(
        "而且给了明确的提示（要删房重建，提到锁定）",
        any(
            ("locked" in m and "delete" in m) or ("锁定" in m or "删" in m)
            for m in app.log_messages
        ),
        "；".join(app.log_messages[-2:]),
    )

    # 本地端口允许改
    app.local_port_var.set("26000")
    app._commit_port("local_port", app.local_port_var)

    check(
        "本地端口改了能生效（26000）",
        app._committed_value("local_port") == 26000,
        f"committed_value={app._committed_value('local_port')}",
    )

    # ---- 房主本人也不许改（用户明确说了）----
    check(
        "锁定看的是房间建了没有，不是你是谁 —— 房主也锁",
        app._config_locked("mc_port") is True,
        "房主绕过了锁定",
    )


# ============================================================
# 4. 隧道数量跟房间容量走
# ============================================================

def test_tunnels_follow_room_capacity():
    print("")
    print("-" * 64)
    print("4. 隧道数量跟着【房间】容量走，不跟本机设置走")
    print("-" * 64)

    # 现场复现：房间是 8，本机那个框（已提交）填的是 3
    app = make_app(
        state=FakeRoomState(is_host=True, host_claimed=True, max_players=8),
        mc_port="25565",
        local_port="25566",
        max_players="3",
    )
    app._committed = {
        "mc_port": "25565",
        "local_port": "25566",
        "max_players": "3",
    }

    check(
        "_room_capacity() 读到的是房间的 8（不是本机的 3）",
        app._room_capacity() == 8,
        f"capacity={app._room_capacity()}",
    )

    config = app._collect_config(role_override="host")

    check(
        "本机填 3、房间是 8 → 真正开 8 条隧道",
        int(config.max_tunnels) == 8,
        f"max_tunnels={config.max_tunnels} max_players={config.max_players}",
    )

    # 反向：房间 3、本机 8 → 只开 3
    app2 = make_app(
        state=FakeRoomState(is_host=True, host_claimed=True, max_players=3),
        mc_port="25565",
        local_port="25566",
        max_players="8",
    )
    app2._committed = {
        "mc_port": "25565",
        "local_port": "25566",
        "max_players": "8",
    }

    config2 = app2._collect_config(role_override="host")

    check(
        "房间是 3、本机填 8 → 只开 3 条（房间容量是天花板）",
        int(config2.max_tunnels) == 3,
        f"max_tunnels={config2.max_tunnels}",
    )

    # 玩家身份不影响
    app3 = make_app(
        state=FakeRoomState(is_host=False, host_claimed=True, max_players=8),
        mc_port="25565",
        local_port="25566",
        max_players="4",
    )
    app3._committed = {
        "mc_port": "25565",
        "local_port": "25566",
        "max_players": "4",
    }

    check(
        "玩家身份不受房间容量影响（玩家不开隧道）",
        int(app3._collect_config(role_override="player").max_players) == 4,
        "",
    )


# ============================================================
# 5. 身份由「谁持有主机位置」决定
# ============================================================

def test_connect_role_from_host_slot():
    print("")
    print("-" * 64)
    print("5. 连接身份由「谁持有主机位置」决定")
    print("-" * 64)

    app = make_app(state=FakeRoomState(is_host=True, host_claimed=True))

    role, explanation = app._decide_connect_role(app.engine.room_state())

    check(
        "持有主机位置 → 以【主机 Host】连接",
        role == "host",
        f"role={role!r}",
    )

    app2 = make_app(
        state=FakeRoomState(is_host=False, host_claimed=True, host_name="别人")
    )

    role2, explanation2 = app2._decide_connect_role(app2.engine.room_state())

    check(
        "没有主机位置 → 以【玩家 Player】连接",
        role2 == "player",
        f"role={role2!r}",
    )

    check(
        "并且会说明房间里已经有主机了（不用用户自己猜）",
        bool(explanation2) and "已经有主机" in explanation2,
        f"explanation={explanation2!r}",
    )


# ============================================================
# 6. 掉线复位 / 自动重连（结构检查，真网络在别的套件里跑）
# ============================================================

def test_room_lost_no_longer_leaves_a_ghost():
    print("")
    print("-" * 64)
    print("6. 掉线之后不再留幽灵界面")
    print("-" * 64)

    import inspect

    from mclink.core import engine as engine_mod
    from mclink.gui import app as gui_app

    source = inspect.getsource(gui_app.MCLinkApp._handle_core_event)

    start = source.find('name == "room_lost"')
    end = source.find('name == "listen_failed"')
    block = source[start:end] if start >= 0 and end > start else ""

    check("room_lost 分支存在", bool(block), "找不到 room_lost 分支")

    check(
        "room_lost 会发起恢复流程（_schedule_room_recovery）",
        "_schedule_room_recovery" in block,
        "",
    )

    recovery = inspect.getsource(gui_app.MCLinkApp._schedule_room_recovery)

    check("恢复流程会尝试自动重连", "auto_rejoin" in recovery, "")

    check(
        "重连失败时会彻底复位（_teardown_room_locally）",
        "_teardown_room_locally" in recovery,
        "失败路径必须复位，否则又是幽灵房",
    )

    engine_source = inspect.getsource(engine_mod.MCLinkEngine.auto_rejoin)

    check("engine.auto_rejoin 会重新进房", "open_room" in engine_source, "")

    check(
        "engine.auto_rejoin 会尝试取回主机位置",
        "claim_host" in engine_source,
        "",
    )


def test_commit_gives_visible_feedback():
    print("")
    print("-" * 64)
    print("2b. 按「确定」必须有立即可见的反馈（成功/失败都要）")
    print("-" * 64)

    import inspect

    from mclink.gui import app as gui_app

    # 用户原话：「点击确定改动端口啊那些配置能否加个反馈啊，是应该成功还是
    # 失败对吧」。第一版只写了 self.log()，那是**窗口底部的日志面板**，
    # 而且要等界面 tick 才出现 —— 站在「基准配置」卡片上按确定根本看不见。
    commit_source = inspect.getsource(gui_app.MCLinkApp._commit_port)

    check(
        "提交成功会在**按钮旁边**那一行显示反馈（_set_row_status）",
        "_set_row_status" in commit_source,
        "成功路径没有就地反馈",
    )

    check(
        "提交失败也会就地显示（不是只弹窗）",
        commit_source.count("_set_row_status") >= 3,
        f"调用次数={commit_source.count('_set_row_status')}",
    )

    check(
        "反馈文字走 tr()（三语言是已验收功能，动态提示也要翻译）",
        "tr(template)" in inspect.getsource(
            gui_app.MCLinkApp._set_row_status
        ),
        "反馈没翻译，英/日文界面会残留中文",
    )

    # 反馈是**状态驱动**的（这样切语言/主题重建界面后仍然一致 ——
    # 外观测试会因为"一次性文字"把重建前后的指纹比出来）。
    app = make_app(state=None, mc_port="25565", local_port="25566", max_players="8")
    app._committed = {"mc_port": "25565", "local_port": "25566", "max_players": "8"}

    shown = _fake_config_rows(app)

    # 干净状态：显示"已生效：25565"
    app._sync_config_buttons()

    check(
        "干净状态下那一行显示「✓ 已生效：25565」（随时能看到现在用的是哪个值）",
        "已生效" in shown["mc_port"]["status"].get("text", "")
        and "25565" in shown["mc_port"]["status"].get("text", ""),
        f"那一行现在是：{shown['mc_port']['status'].get('text')!r}",
    )

    check(
        "成功态反馈是绿色（OK 色）",
        shown["mc_port"]["status"].get("fg") == gui_app.OK,
        f"fg={shown['mc_port']['status'].get('fg')!r}",
    )

    # 改了没提交 → 那一行立刻变成"未提交"提示
    app.mc_port_var.set("25570")
    app._sync_config_buttons()

    check(
        "改了没提交时那一行变成「未提交：改了请按确定」",
        "未提交" in shown["mc_port"]["status"].get("text", ""),
        f"那一行现在是：{shown['mc_port']['status'].get('text')!r}",
    )

    # 提交 → 立刻变回"已生效：25570"
    app._commit_port("mc_port", app.mc_port_var)

    check(
        "按确定之后那一行立刻显示「✓ 已生效：25570」",
        "已生效" in shown["mc_port"]["status"].get("text", "")
        and "25570" in shown["mc_port"]["status"].get("text", ""),
        f"那一行现在是：{shown['mc_port']['status'].get('text')!r}",
    )

    # 失败：值不变，而且那一行显示错误
    app.mc_port_var.set("abc")
    app._commit_port("mc_port", app.mc_port_var)

    check(
        "提交失败时那一行显示错误原因",
        "不是数字" in shown["mc_port"]["status"].get("text", ""),
        f"那一行现在是：{shown['mc_port']['status'].get('text')!r}",
    )

    check(
        "失败反馈是红色（BAD 色）",
        shown["mc_port"]["status"].get("fg") == gui_app.BAD,
        f"fg={shown['mc_port']['status'].get('fg')!r}",
    )

    check(
        "失败之后已生效的值没被污染（还是 25570）",
        app._committed_value("mc_port") == 25570,
        f"committed_value={app._committed_value('mc_port')}",
    )

    # 被锁定时也要有就地反馈
    locked_app = make_app(
        state=FakeRoomState(is_host=True, host_claimed=True, max_players=8),
        mc_port="25565",
        local_port="25566",
        max_players="8",
    )
    locked_app._committed = {
        "mc_port": "25565",
        "local_port": "25566",
        "max_players": "8",
    }

    locked_rows = _fake_config_rows(locked_app)

    locked_app.mc_port_var.set("25570")
    locked_app._commit_port("mc_port", locked_app.mc_port_var)

    check(
        "被锁定时那一行显示「已锁定」，不是静默什么都不做",
        "锁定" in locked_rows["mc_port"]["status"].get("text", ""),
        f"那一行现在是：{locked_rows['mc_port']['status'].get('text')!r}",
    )


class FakeStatus:
    """假的 Label：只要 .configure / .get('text') 够用就行。"""

    def __init__(self):
        self._opts = {}

    def configure(self, **kwargs):
        self._opts.update(kwargs)

    def get(self, key, default=None):
        return self._opts.get(key, default)

    def update_idletasks(self):
        return None


def _fake_config_rows(app):
    """
    给三行配置装上假的状态标签，好断言"那一行显示了什么"。

    直接**替换** app._config_rows（不 append）：FakeApp 在 __init__ 里已经
    建过一个空列表，append 会变成 6 行、而 _set_row_status 只认第一个匹配。
    """
    rows = {}
    entries = []

    for key, variable in (
        ("mc_port", app.mc_port_var),
        ("local_port", app.local_port_var),
        ("max_players", app.max_players_var),
    ):
        status = FakeStatus()
        rows[key] = {"status": status}
        entries.append((variable, None, FakeStatus(), status, "（提示）"))

    app._config_rows = entries

    return rows


def main():
    print("=" * 64)
    print("CraftLink 基准配置 / 房间参数锁定 回归测试")
    print("=" * 64)

    sandbox = tempfile.mkdtemp(prefix="mclink-room-recovery-")
    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox

    for test in (
        test_identity_selector_is_gone,
        test_changes_need_explicit_commit,
        test_commit_gives_visible_feedback,
        test_room_params_locked_after_create,
        test_tunnels_follow_room_capacity,
        test_connect_role_from_host_slot,
        test_room_lost_no_longer_leaves_a_ghost,
    ):
        try:
            test()
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            check(f"{test.__name__} 不抛异常", False, f"{type(e).__name__}: {e}")

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
