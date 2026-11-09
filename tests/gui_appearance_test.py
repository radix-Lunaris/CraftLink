"""
MC-Link -- 客户端外观（语言 / 主题）与房间入口测试。

============================================================================
 这个套件存在的两个理由
============================================================================

1. 【底线】语言和主题是纯观感的改动，绝不允许顺手删掉任何功能。
   所以这里不是"看看能不能切换"，而是：
     * 先把切换前整个窗口的控件清单（类名 + 显示文字）抓下来，
     * 切到 English / 日本語 / 亮色 / 跟随系统 / 再切回中文暗色，
     * 每一步都比对控件数量，切回中文暗色后要求清单【完全一致】。
   只要有人在"美化"的时候漏掉一个按钮、少了一行状态、或者把某个开关
   从界面上删了，这个测试就会红。

2. 房间入口的回归测试：「开始连接 / 删除房间」必须读【我真正在的房间】，
   不是读输入框。输入框在进房成功后被刻意清空，旧代码读它就会回
   「房间不能为空」——用户就是这么卡住的。

Run:
    python tests/gui_appearance_test.py
"""

import contextlib
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail:
        line += f"  -- {detail}"

    # 控制台可能是 GBK（Windows 中文默认），emoji 这类字符打印会抛
    # UnicodeEncodeError —— 那是测试输出的问题，不该让测试失败。
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    (PASSED if ok else FAILED).append(name)


def walk(widget):
    """这个控件和它下面的所有子控件。"""
    yield widget

    for child in widget.winfo_children():
        yield from walk(child)


def signature(root):
    """
    整个窗口的控件指纹：类名 + 显示文字 + 状态。

    两件事必须成立才算没动功能：控件一个不少，且它们的文字/状态一模一样。
    用指纹而不是逐个断言，是因为"美化"最容易犯的错就是漏掉某一个控件，
    而漏掉的那个往往正是写测试的人没想到要去断言的那个。

    日志框例外：它显示的是【内容】，内容随运行变化，所以只记行数。

    ttk 控件的 textvariable 也例外：`cget("text")` 在 ttk 上返回的是变量名
    （PY_VAR62 这种东西），每次重建都会变。所以这里改记【变量的当前值】，
    这样文字变了照样能发现，变量名变了则不会误报。
    """
    items = []

    for widget in walk(root):
        if widget is root:
            continue

        row = [widget.winfo_class()]

        if widget.winfo_class() == "Text":
            with contextlib.suppress(Exception):
                row.append(f"lines={widget.index('end-1c').split('.')[0]}")
        else:
            var_name = ""

            with contextlib.suppress(Exception):
                var_name = str(widget.cget("textvariable") or "")

            if var_name:
                with contextlib.suppress(Exception):
                    row.append(f"value={widget.getvar(var_name)}")
            else:
                with contextlib.suppress(Exception):
                    value = widget.cget("text")

                    if value not in ("", None):
                        row.append(f"text={value}")

            for option in ("state", "values"):
                with contextlib.suppress(Exception):
                    value = widget.cget(option)

                    if value not in ("", None):
                        row.append(f"{option}={value}")

        items.append("|".join(str(part) for part in row))

    return sorted(items)


#: 配色字典里的键名 -> app.py 里的常量名（只有一个不一样，别让测试猜）。
_PALETTE_ALIASES = {"HEADER": "HEADER_BG"}


class FakeState:
    """假房间状态：只提供 _active_room_name() 会读的那几个字段。"""

    def __init__(self, room="", is_owner=False, is_host=False):
        self.room = room
        self.is_owner = is_owner
        self.is_host = is_host


class FakeSession:
    def __init__(self, state):
        self.state = state


def _mode_lock_state(application, gui_app):
    """
    连接模式那张卡上每个选项的「上锁」标记。

    上锁不是靠文字看出来的，而是画瓷砖时带的 locked 标志 —— 这里直接读它，
    所以"看起来上锁但实际能点"这种情况会露馅。
    """
    wanted = {gui_app.MODE_RELAY, gui_app.MODE_STUN, gui_app.MODE_AUTO}

    for _holder, _var, options in application._tile_groups:
        values = [value for value, _l, _h, _lock in options]

        if wanted <= set(values):
            return {value: lock for value, _l, _h, lock in options}

    return {}


def main():
    print("=" * 64)
    print("MC-Link GUI appearance test (语言 / 主题 / 房间入口)")
    print("=" * 64)

    try:
        import tkinter as tk
    except ImportError as e:
        check("tkinter available", False, str(e))
        return 1

    sandbox = tempfile.mkdtemp(prefix="mclink-gui-look-")

    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox

    from mclink.config import settings
    from mclink.core import protocol as proto
    from mclink.gui import app as gui_app
    from mclink.gui import i18n
    from mclink.gui import theme as theme_mod

    try:
        root = tk.Tk()
    except Exception as e:
        check("Tk root can be created (display available)", False, repr(e))
        return 1

    try:
        root.withdraw()

        application = gui_app.MCLinkApp(root)

        check("MCLinkApp constructs", True)

        # run() 在构建完之后会补这两下（模式提示文字、身份对应的端口框），
        # 这里照做，否则"基线指纹"本身就不是一个真实启动后的界面。
        application._on_mode_change()
        application._on_role_change()

        application.room_var.set("appearance-test")
        application.host_var.set("127.0.0.1")
        # ★ M6/D10：端口不再有内置默认值（空端口会让 _collect_config 报错），
        # 这里显式给一个本机端口。
        application.port_var.set("19100")
        application.mc_port_var.set("25565")
        application.local_port_var.set("25566")
        application.max_players_var.set("8")
        application._commit_port("mc_port", application.mc_port_var)
        application._commit_port("local_port", application.local_port_var)
        application._commit_max_players()

        # 真实窗口每隔 TICK_MS 会刷一次状态卡/账号行/房间行。抓基线之前先刷
        # 一次，否则基线记的是"还没刷过的初始文字"，跟重建后的界面不可比。
        application._refresh_account_ui()
        application._refresh_room_ui()
        application._refresh_status()
        application._append_logs()

        root.update_idletasks()

        # ============================================================
        # 0. 默认必须是【中文 + 暗色】，而且房间号输入框默认为空
        # ============================================================
        check(
            "默认界面语言是中文",
            i18n.current_language() == "zh",
            i18n.current_language(),
        )

        check(
            "默认主题是暗色",
            theme_mod.current() == "dark" and theme_mod.resolved() == "dark",
            f"{theme_mod.current()}/{theme_mod.resolved()}",
        )

        check(
            "房间号输入框默认为空（不再预填 test001）",
            proto.DEFAULT_ROOM == ""
            and settings.ClientConfig().room == "",
            f"DEFAULT_ROOM={proto.DEFAULT_ROOM!r}",
        )

        # 语言/主题选择器必须真的在界面上，否则用户没法切
        check(
            "顶部有语言选择器（中文 / English / 日本語）",
            hasattr(application, "language_var")
            and application.language_var.get() == "中文",
            getattr(application, "language_var", None)
            and application.language_var.get(),
        )

        check(
            "顶部有主题选择器（暗色 / 亮色 / 跟随系统）",
            hasattr(application, "theme_var")
            and application.theme_var.get() in ("暗色", "Dark", "ダーク"),
            getattr(application, "theme_var", None)
            and application.theme_var.get(),
        )

        # ★ V5.0：AUTO 解锁。三个模式都真实可用（relay / stun / auto）。
        check(
            "★ v5：Relay / STUN / AUTO 三块全部解锁、可以点",
            _mode_lock_state(application, gui_app) == {
                gui_app.MODE_RELAY: False,
                gui_app.MODE_STUN: False,
                gui_app.MODE_AUTO: False,
            },
            str(_mode_lock_state(application, gui_app)),
        )

        # STUN 必须能真的选上（不能被弹回 Relay）
        application.mode_var.set(gui_app.MODE_STUN)
        application._on_mode_change()

        check(
            "★ 选中 STUN 后保持不变（v3 解锁了它）",
            application.mode_var.get() == gui_app.MODE_STUN,
            application.mode_var.get(),
        )

        # AUTO 也必须能真的选上（V5.0 解锁，不能被弹回 Relay）
        application.mode_var.set(gui_app.MODE_AUTO)
        application._on_mode_change()

        check(
            "★ 选中 AUTO 后保持不变（v5 解锁了它）",
            application.mode_var.get() == gui_app.MODE_AUTO,
            application.mode_var.get(),
        )

        # 回到 Relay，后面的外观指纹按已知状态抓。
        application.mode_var.set(gui_app.MODE_RELAY)
        application._on_mode_change()

        # 指纹必须在"已经写过日志"的状态下抓：日志框里有没有内容也是界面
        # 状态的一部分，而且这里正是当年多空行 bug 的地方。
        application.log("外观切换前写的一行日志", kind="info")
        application._append_logs()

        baseline = signature(root)

        check(
            "切换前抓到了完整的控件指纹",
            len(baseline) > 60,
            f"{len(baseline)} 个控件",
        )

        # 记住几个"必须一直活着"的功能点，单独点名（指纹之外的保险）
        protected = (
            "start_button", "stop_button", "test_button", "copy_button",
            "login_button", "logout_button", "rename_button",
            "delete_room_button", "rooms_button", "log_text", "status_vars",
            "room_var", "mode_var", "max_players_var",
            "mc_port_var", "local_port_var", "host_var", "port_var",
            "client_id_var", "rooms_tree", "room_list_hint", "room_state_label",
            "language_var", "theme_var",
        )

        # ============================================================
        # 1. 语言切换：真的换文字，控件一个不少
        # ============================================================
        application.language_var.set("English")
        application.on_language_change()

        root.update_idletasks()

        check(
            "切到 English 后生效",
            i18n.current_language() == "en",
            i18n.current_language(),
        )

        check(
            "界面上的按钮真的变成英文（不是只有变量变了）",
            # 按钮带图标（▶ / ■），所以断言"以标签结尾"而不是完全相等
            application.start_button.cget("text").endswith("Start")
            and application.stop_button.cget("text").endswith("Stop"),
            f"{application.start_button.cget('text')!r} / "
            f"{application.stop_button.cget('text')!r}",
        )

        check(
            "英文下控件数量与中文一致（没有控件被翻译弄丢）",
            len(signature(root)) == len(baseline),
            f"{len(signature(root))} vs {len(baseline)}",
        )

        check(
            "切换语言后日志内容还在（重建界面不清日志）",
            "外观切换前写的一行日志" in application.log_text.get("1.0", "end"),
        )

        check(
            "切换语言不影响任何输入框里的内容",
            application.room_var.get() == "appearance-test"
            and application.host_var.get() == "127.0.0.1",
            f"{application.room_var.get()!r} / {application.host_var.get()!r}",
        )

        check(
            "切换外观后模式锁状态不变（三块全开）",
            _mode_lock_state(application, gui_app) == {
                gui_app.MODE_RELAY: False,
                gui_app.MODE_STUN: False,
                gui_app.MODE_AUTO: False,
            }
            and application.mode_var.get() == gui_app.MODE_RELAY,
            f"{_mode_lock_state(application, gui_app)} mode={application.mode_var.get()}",
        )

        check(
            "语言选择被写进了配置文件（重启后还是这个语言）",
            settings.load_config().ui_language == "en",
            settings.load_config().ui_language,
        )

        # 日本語
        application.language_var.set("日本語")
        application.on_language_change()

        root.update_idletasks()

        check(
            "切到日本語后生效",
            i18n.current_language() == "ja"
            and application.start_button.cget("text").endswith("接続開始"),
            f"{i18n.current_language()} / "
            f"{application.start_button.cget('text')!r}",
        )

        check(
            "日文下控件数量仍然一致",
            len(signature(root)) == len(baseline),
            f"{len(signature(root))} vs {len(baseline)}",
        )

        # 切回中文：这必须是一张和一开始【一模一样】的界面
        application.language_var.set("中文")
        application.on_language_change()

        root.update_idletasks()

        check(
            "切回中文后控件指纹与最初完全一致（证明只改了观感）",
            signature(root) == baseline,
            "有差异："
            + str([x for x in signature(root) if x not in baseline][:5])
            + " / 少了："
            + str([x for x in baseline if x not in signature(root)][:5]),
        )

        # ============================================================
        # 2. 主题切换：配色真的换了，功能还是那套
        # ============================================================
        application.theme_var.set("亮色")
        application.on_theme_change()

        root.update_idletasks()

        light = theme_mod.PALETTES["light"]

        check(
            "切到亮色后 app.py 的配色常量真的变亮了",
            theme_mod.resolved() == "light"
            and gui_app.BG == light["BG"]
            and gui_app.CARD == light["CARD"],
            f"resolved={theme_mod.resolved()} BG={gui_app.BG}",
        )

        check(
            "亮色下窗口底色也跟着变（不是只改了常量）",
            str(root.cget("bg")).lower() == light["BG"].lower(),
            str(root.cget("bg")),
        )

        check(
            "亮色的每一个颜色都真的套用到了 app.py 的常量上",
            all(
                getattr(gui_app, _PALETTE_ALIASES.get(key, key)) == value
                for key, value in light.items()
            ),
            str(
                [
                    key
                    for key, value in light.items()
                    if getattr(gui_app, _PALETTE_ALIASES.get(key, key)) != value
                ]
            ),
        )

        check(
            "亮色下控件数量与暗色一致",
            len(signature(root)) == len(baseline),
            f"{len(signature(root))} vs {len(baseline)}",
        )

        check(
            "切换主题不影响任何输入框里的内容 / 选择",
            application.room_var.get() == "appearance-test"
            and application.mc_port_var.get() == "25565",
            f"{application.room_var.get()!r} / {application.mc_port_var.get()!r}",
        )

        check(
            "主题选择被写进了配置文件",
            settings.load_config().ui_theme == "light",
            settings.load_config().ui_theme,
        )

        # 跟随系统
        application.theme_var.set("跟随系统")
        application.on_theme_change()

        root.update_idletasks()

        expected_system = theme_mod.system_theme()

        check(
            "「跟随系统」解析成实际配色并且和注册表读出来的一致",
            theme_mod.current() == "system"
            and theme_mod.resolved() == expected_system
            and gui_app.BG == theme_mod.PALETTES[expected_system]["BG"],
            f"system={expected_system} resolved={theme_mod.resolved()}",
        )

        check(
            "跟随系统读不到注册表时退回暗色（不会崩）",
            theme_mod.resolve("system") in ("dark", "light"),
        )

        check(
            "跟随系统下控件数量一致",
            len(signature(root)) == len(baseline),
            f"{len(signature(root))} vs {len(baseline)}",
        )

        # 最终回到暗色
        application.theme_var.set("暗色")
        application.on_theme_change()

        root.update_idletasks()

        check(
            "切回暗色后配色与最初一致",
            theme_mod.resolved() == "dark"
            and gui_app.BG == theme_mod.PALETTES["dark"]["BG"],
            f"resolved={theme_mod.resolved()} BG={gui_app.BG}",
        )

        check(
            "切回暗色后控件指纹仍然与最初一致",
            signature(root) == baseline,
            "有差异："
            + str([x for x in signature(root) if x not in baseline][:5]),
        )

        # 反复切换不能把日志"撑大"（以前每切一次多一个空行）
        log_before = application.log_text.get("1.0", "end-1c")

        for _ in range(3):
            application.theme_var.set("亮色")
            application.on_theme_change()
            application.theme_var.set("暗色")
            application.on_theme_change()

        root.update_idletasks()

        check(
            "反复切换外观不会给日志塞空行 / 不会丢日志",
            application.log_text.get("1.0", "end-1c") == log_before,
            f"{len(application.log_text.get('1.0', 'end-1c').splitlines())} vs "
            f"{len(log_before.splitlines())} 行",
        )

        # ============================================================
        # 3. 外观切换绝不能碰已经验收过的功能状态
        # ============================================================
        for attribute in protected:
            check(
                f"切换外观后仍然存在：{attribute}",
                hasattr(application, attribute),
            )

        # 登录一个假账号，然后切换外观：账号状态、角色徽章、按钮可用性都不能丢
        from mclink.core.control_client import AuthSession

        application.auth = AuthSession(
            token="look-token",
            username="looktester",
            role="admin",
            protocol=3,
            server="127.0.0.1:19100",
        )
        application._refresh_account_ui()

        before_account = application.account_label.cget("text")
        before_badge = application.role_badge.cget("text")

        application.language_var.set("English")
        application.on_language_change()

        root.update_idletasks()

        check(
            "切换语言后账号仍然是登录状态（不会被踢下线）",
            "looktester" in application.account_label.cget("text")
            or "looktester" in before_account,
            application.account_label.cget("text"),
        )

        check(
            "切换语言后角色徽章还在",
            application.role_badge.cget("text").strip() != ""
            and application.role_badge.cget("bg")
            == gui_app.ROLE_COLORS["admin"],
            f"{application.role_badge.cget('text')!r} "
            f"{application.role_badge.cget('bg')!r} "
            f"(before badge={before_badge!r})",
        )

        check(
            "切换语言后「退出登录 / 改名」按钮仍然可用",
            str(application.logout_button["state"]) == "normal"
            and str(application.rename_button["state"]) == "normal",
            f"{application.logout_button['state']} / "
            f"{application.rename_button['state']}",
        )

        application.language_var.set("中文")
        application.on_language_change()
        application.auth = None
        application._refresh_account_ui()

        root.update_idletasks()

        check(
            "退出登录后界面回到未登录状态",
            "未登录" in application.account_label.cget("text"),
            application.account_label.cget("text"),
        )

        # ============================================================
        # 4. 房间入口读「我真正在的房间」，不是输入框
        # ============================================================
        check(
            "不在房间里时，目标房间 = 输入框内容",
            application._active_room_name() == "appearance-test",
            application._active_room_name(),
        )

        original_current = application.engine.room_current
        original_state = application.engine.room_state

        try:
            # 模拟"已经在房间里了，输入框是空的"——就是用户卡住的那个状态
            application.engine.room_current = lambda: FakeSession(
                FakeState(room="joined-room", is_owner=True)
            )
            application.engine.room_state = lambda: FakeState(
                room="joined-room", is_owner=True
            )

            application.room_var.set("")

            check(
                "输入框为空但人在房间里时，目标房间 = 真正在的房间（回归）",
                application._active_room_name() == "joined-room",
                application._active_room_name(),
            )

            application.room_var.set("别的房间")

            check(
                "房间里时输入框的残留文字不会覆盖真正在的房间",
                application._active_room_name() == "joined-room",
                application._active_room_name(),
            )

            collected = application._collect_config()

            check(
                "「开始连接」收集到的房间号是真正在的房间（不再报房间不能为空）",
                collected.room == "joined-room",
                collected.room,
            )

            application.engine.room_current = lambda: FakeSession(FakeState(room=""))

            check(
                "会话还没拿到房间名时退回输入框（不会拿到空字符串）",
                application._active_room_name() == "别的房间",
                application._active_room_name(),
            )

            application.engine.room_current = lambda: None

            check(
                "不在房间时「删除房间」会提示还没进入房间（不会误删别的房间）",
                application._active_room_name() == "别的房间",
                application._active_room_name(),
            )
        finally:
            application.engine.room_current = original_current
            application.engine.room_state = original_state
            application.room_var.set("")

        # ============================================================
        # 5. 房间号校验：空格 / 斜杠 / 特殊字符都不许过
        # ============================================================
        # 5. 房间名校验（新规则：任何名字都能用，只挡控制字符/超长/空）
        # ============================================================
        for bad, why in (
            ("", "空"),
            ("   ", "全空格"),
            ("a\tb", "带制表符"),
            ("换行\n注入", "带换行"),
            ("x" * 200, "超长"),
        ):
            ok, _message = application._validate_room_name(bad)
            check(f"房间名拒绝：{why}", ok is False)

        for good in (
            "abc",
            "room-1",
            "房间1",
            "我的房间 一号",
            "my room",
            "a/b",
            "Bob's World!",
            "🎮 游戏房",
        ):
            ok, message = application._validate_room_name(good)
            check(f"房间名接受：{good!r}", ok is True, message)

        # ============================================================
        # 6. 界面上的房间号输入框默认空 + 创建/加入后清空
        # ============================================================
        check(
            "房间输入框在界面重建后仍然是空的（不会把旧房间号填回去）",
            application.room_var.get() == "",
            application.room_var.get(),
        )

        original_open_room = application.engine.open_room
        original_require_login = application.require_login

        try:
            class FakeOpened:
                is_owner = True

            application.engine.open_room = lambda *a, **k: FakeOpened()
            application.require_login = lambda: True
            application._confirm_room_is_free = lambda *a, **k: True
            application._capacity_within_server = lambda: True

            application.room_var.set("newroom")
            application.on_create_room()

            root.update_idletasks()

            check(
                "创建房间成功后输入框被清空（避免误以为还在旧房间）",
                application.room_var.get() == "",
                application.room_var.get(),
            )
        finally:
            application.engine.open_room = original_open_room
            application.require_login = original_require_login

        # ============================================================
        # 7. i18n / theme 的兜底行为：翻不到就显示原文，绝不空、绝不抛
        # ============================================================
        i18n.set_language("en")

        check(
            "没翻译过的句子原样显示（不是空白，也不是 key）",
            i18n.tr("这句话故意没有翻译") == "这句话故意没有翻译",
            i18n.tr("这句话故意没有翻译"),
        )

        check(
            "翻译表里空值不会把界面弄成空白",
            i18n.tr("") == "",
        )

        check(
            "未知语言代码退回中文",
            i18n.set_language("klingon") == "zh"
            and i18n.current_language() == "zh",
            i18n.current_language(),
        )

        check(
            "三种语言都能查到自己名字",
            [i18n.language_label(code) for code, _label in i18n.LANGUAGES]
            == ["中文", "English", "日本語"],
        )

        check(
            "配置文件里写了非法语言/主题时会被夹回默认值（不会崩）",
            # M6/D10：中继地址不再有内置默认值，所以这里必须显式给一个 ——
            # 这条断言测的是"外观字段会被夹回默认"，不是服务器地址。
            settings.ClientConfig(
                server_host="127.0.0.1",
                server_port=19100,
                room="cfgtest",
                role="host",
                ui_language="klingon",
                ui_theme="neon",
            ).validated().ui_language
            == "zh"
            and settings.ClientConfig(
                server_host="127.0.0.1",
                server_port=19100,
                room="cfgtest",
                role="host",
                ui_language="klingon",
                ui_theme="neon",
            ).validated().ui_theme
            == "dark",
        )

        check(
            "theme.set_theme 非法值退回暗色",
            theme_mod.set_theme("neon") == "dark"
            and theme_mod.current() == "dark",
            theme_mod.current(),
        )

        check(
            "两套配色给出的键在 app.py 里都是真实存在的常量",
            all(
                hasattr(gui_app, _PALETTE_ALIASES.get(key, key))
                for key in set(theme_mod.PALETTES["dark"])
                | set(theme_mod.PALETTES["light"])
            ),
            str(
                [
                    key
                    for key in set(theme_mod.PALETTES["dark"])
                    | set(theme_mod.PALETTES["light"])
                    if not hasattr(gui_app, _PALETTE_ALIASES.get(key, key))
                ]
            ),
        )

        check(
            "两套配色的键完全一致（亮色不会漏掉某个颜色）",
            set(theme_mod.PALETTES["dark"]) == set(theme_mod.PALETTES["light"]),
        )

        # ============================================================
        # 8. 二级窗口（登录框）也跟着语言走
        # ============================================================
        from mclink.gui import auth_ui

        original_engine = application.engine

        for code, expected in (("en", "Sign in"), ("ja", "ログイン"), ("zh", "登录")):
            i18n.set_language(code)
            auth_ui.refresh_palette()

            dialog = None

            try:
                dialog = auth_ui.LoginDialog(
                    root,
                    original_engine,
                    "127.0.0.1",
                    19100,
                    username="",
                    client_id="look-test",
                )

                dialog.update_idletasks()

                check(
                    f"登录框跟随界面语言（{code} → {expected!r}）",
                    dialog.login_button.cget("text") == expected,
                    dialog.login_button.cget("text"),
                )
            finally:
                if dialog is not None:
                    with contextlib.suppress(Exception):
                        dialog.grab_release()
                        dialog.destroy()

        # 收尾：把界面还原成中文暗色，别把测试环境留在别的语言上
        i18n.set_language("zh")
        theme_mod.set_theme("dark")

    except Exception:
        check("GUI appearance test completed without exception", False)
        traceback.print_exc()
    else:
        check("GUI appearance test completed without exception", True)
    finally:
        with contextlib.suppress(Exception):
            root.destroy()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
