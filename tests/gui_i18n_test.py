"""
CraftLink -- 语言覆盖硬测试（"切到英文之后不许残留中文"）。

============================================================================
 这一套为什么存在
============================================================================

用户报的：「英语和其他语言并未完全覆盖所有按钮和面板，还是有很多未生效的
语言部分」。

我当时的 i18n 只覆盖了"我想到的地方"，而且**没有任何测试能发现漏翻** —— 漏掉的
按钮就是漏掉了，测试照样全绿。

所以这一套反过来做：把界面切到 English / 日本語，然后遍历**整棵控件树**，
把所有还是中文的文字收集出来。collection 非空就是失败，并且会把它打印出来 ——
于是"漏翻"从一个看不见的问题变成一个必须修的红灯。

同时验证：
  * tr() 的兜底（查不到就原文，不返回空、不抛异常）
  * 软件名 CraftLink **不跟随语言**（用户明确要求）
  * 语言/主题名单两处定义保持一致（config 与 gui.theme）
  * 每个界面语言下都能把界面完整建出来（没有因为某句翻译而报错）

Run:
    python tests/gui_i18n_test.py
"""

import ast
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

    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    (PASSED if ok else FAILED).append(name)


def collect_texts(widget, skip_classes=("Text", "Entry", "TEntry", "Listbox")):
    """
    收集一棵树上所有【静态文字】：控件的 text、标题、Treeview 列头、下拉框选项。

    带 textvariable 的控件跳过：那种文字是数据（房间名、用户名、状态值），
    不是界面文案 —— 把"我的房间"翻译掉才是真的错。
    """
    found = []

    def visit(node):
        with contextlib.suppress(Exception):
            klass = node.winfo_class()

            if klass not in skip_classes:
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

                for option in ("values",):
                    with contextlib.suppress(Exception):
                        values = node.cget(option)

                        if values:
                            for value in values:
                                if str(value).strip():
                                    found.append(str(value))

            if klass == "Treeview":
                for column in node["columns"]:
                    with contextlib.suppress(Exception):
                        heading = node.heading(column).get("text", "")

                        if str(heading).strip():
                            found.append(str(heading))

        for child in node.winfo_children():
            visit(child)

    visit(widget)

    return found


def _matches_replacement(replacement, text):
    """
    `text` 会不会是这条句式替换模板的产出？

    模板 `'ルーム「\\1」を作成しました'` 反着看就是一个正则：
    字面部分要一模一样，`\\1` 那种捕获组的位置可以是任何变量。
    这样"日文的动态日志"能被认出来，而中文原文不会。
    """
    import re

    parts = re.split(r"\\\d+", replacement)

    if len(parts) == 1:
        return replacement == text

    regex = "^" + "(.*?)".join(re.escape(part) for part in parts) + "$"

    return re.match(regex, text, re.DOTALL) is not None


def leftover_texts(texts, code):
    """
    这批文字里，当前语言还没翻到的那些。

    ============================================================================
      "还是中文" ≠ "含中日韩字符"
    ============================================================================

    最开始这里就是一句 `if i18n.has_cjk(t)`。它对英文是对的，对日文是错的：
    日文译文本来就是汉字 + 假名（「ルーム」「ホスト」），于是**翻对了的**日文
    也被当成漏翻报出来 —— 检查器自己制造假红灯。

    所以判定改成"这段文字是不是当前语言的翻译表产出的"：

      * 是静态表里某个键的值      -> 已经翻好了（日文、以及 identity 条目）
      * 是"图标 + 译文"           -> 按钮上是「⧉ 复制 / ⧉ Copy」，图标由
                                     gui/i18n.py 的控件遍历接回去（那里同样按
                                     第一个空格切开再查一次表），这里用同一条规则
      * 是句式表某条替换的产出    -> 动态日志/提示（f-string 拼出来的句子）本来
                                     就没有静态键，只能认模板的形状
      * 其余仍然含中日韩字符的    -> 真·漏翻，红灯

    这样英文依旧是最严格的：英文表里没有一个值是中文（只有语言选择器那三个
    identity 条目），英文句式表也不会产出中文，所以英文界面上只要还剩一个中文
    字，就一定会失败。
    """
    from mclink.gui import i18n  # 局部导入：和 main() 里一样，client 要先在 sys.path 上

    table = i18n.TRANSLATIONS.get(code) or {}
    patterns = i18n.PATTERNS.get(code) or ()
    produced = set(table.values())

    leftovers = set()

    for text in texts:
        if not i18n.has_cjk(text):
            continue

        if text in produced:
            continue

        head, sep, tail = str(text).partition(" ")

        if sep and tail and tail in produced:
            continue

        if any(_matches_replacement(replacement, text)
               for _pattern, replacement in patterns):
            continue

        leftovers.add(text)

    return sorted(leftovers)


# ============================================================
# 静态检查：写死的中文文案必须有翻译
# ============================================================
#
# 为什么不靠"遍历控件树"就够了：那一套必须有 tkinter 才能跑。而"新加了一句
# 界面文字忘了翻译"这件事，在源码层面就能查出来 —— 而且**不需要图形环境**。
#
# 逐个控件造出来再遍历，代价是那一步只能在有显示器的机器上跑；这里这一层
# 让漏翻在没有 tkinter 的环境里也是红灯。

#: 参数里带界面文字的控件类。只查这些：`self.log(...)` 之类的动态句子走
#: PATTERNS 表，硬塞进这个检查只会制造噪音。
TEXT_WIDGETS = {
    "Label", "Button", "Checkbutton", "Radiobutton", "LabelFrame",
    "Toplevel", "Message", "RoundButton", "RoundTile", "Card",
}

TEXT_KWARGS = ("text=", "title=")


def _looks_translated(text):
    """A literal that already carries its own translations, or is not text."""
    return (
        " / " in text          # 三语并排的那种写法（语言选择器）
        or not any("\u4e00" <= ch <= "\u9fff" for ch in text)
    )


def static_untranslated_texts():
    """
    返回 [(文件, 行号, 文案)]：写死在控件参数里、却没有翻译的中文。

    Only checks literals that are ACTUALLY passed to a widget as its text:
      * a positional first argument (tk.Label(parent, "文字"))
      * an explicit text= / title= keyword (a plain string or an f-string with
        no placeholders)

    f-strings WITH placeholders are skipped: their interpolated value is data
    (a room name, a port), and translating the fixed part is exactly what the
    PATTERNS tables are for.
    """
    from mclink.gui import i18n

    translated = set(i18n.EN) | set(i18n.JA)
    produced = set(i18n.EN.values()) | set(i18n.JA.values())
    gui_dir = os.path.join(ROOT, "client", "mclink", "gui")

    found = []

    for name in sorted(os.listdir(gui_dir)):
        if not name.endswith(".py"):
            continue

        path = os.path.join(gui_dir, name)

        with open(path, encoding="utf-8") as handle:
            source = handle.read()

        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - defensive
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func

            # `tk.Label(...)` -> "Label"; `RoundButton(...)` -> "RoundButton"
            widget = getattr(func, "attr", None) or getattr(func, "id", None)

            if widget not in TEXT_WIDGETS:
                continue

            for keyword in node.keywords:
                if keyword.arg not in ("text", "title"):
                    continue

                _collect_static(keyword.value, name, found, translated,
                                produced, source)

            # tk.Label(parent, "文字") -- first positional after the parent.
            for argument in node.args[1:2]:
                _collect_static(argument, name, found, translated,
                                produced, source)

    return found


def _collect_static(node, filename, found, translated, produced, source):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        # An f-string with no `{...}` is just a string literal.
        if any(isinstance(v, ast.FormattedValue) for v in node.values):
            return

        text = "".join(
            v.value for v in node.values if isinstance(v, ast.Constant)
        )
    else:
        return

    text = text.strip()

    if not text or not any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return

    if text in translated or text in produced or _looks_translated(text):
        return

    line = getattr(node, "lineno", 0)
    found.append((filename, line, text))


FILES_THAT_MUST_LOG_IN_ENGLISH = (
    "core/host_core.py",
    "core/player_core.py",
    "core/session.py",
    "core/direct.py",
    "core/direct_carry.py",
    "core/punch.py",
    "core/stun.py",
    "core/room.py",
    "core/engine.py",
    "gui/app.py",
)

LOG_METHODS = ("log", "status", "_status", "_emit")


def chinese_log_literals():
    """
    Every logging call whose LITERAL text still contains Chinese.

    ============================================================================
      WHY THIS IS A HARD TEST
    ============================================================================

    The project owner's words: the log system is going English because it has to
    record detailed, useful information for later analysis and repair, and
    Chinese in it is not wanted. The log is the artefact they send back when
    something breaks, so it is ENGLISH at the source -- engine, core and GUI
    alike -- and the file on disk and the line on screen say the same thing.

    A convention nobody can enforce is a convention that decays: one new
    Chinese log line is invisible in review and only surfaces in the user's
    next log dump. So this walks the AST and fails the build instead.

    NOT a scan of comments or docstrings: those are documentation, they may be
    Chinese, and nobody greps them at 2am.

    Interpolated VALUES may still be Chinese and that is correct -- a room name,
    a relay reason, a user's chosen name are DATA, not our wording.
    """
    import re

    cjk = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
    offenders = []

    for rel in FILES_THAT_MUST_LOG_IN_ENGLISH:
        path = os.path.join(ROOT, "client", "mclink", rel)

        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), path)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            name = getattr(node.func, "attr", None) or getattr(
                node.func, "id", None
            )

            if name not in LOG_METHODS or not node.args:
                continue

            first = node.args[0]
            text = ""

            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                text = first.value
            elif isinstance(first, ast.JoinedStr):
                text = "".join(
                    part.value
                    for part in first.values
                    if isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                )

            if cjk.search(text):
                offenders.append((rel, node.lineno, text.strip()[:70]))

    return offenders


def test_client_log_module():
    """
    客户端日志系统本身的硬测试（`client/mclink/log.py`）。

    规则来自用户的要求：日志记录**原始数据**，不翻译，用来事后改进和审计。
    """
    print("")
    print("---- 客户端日志系统：落盘 / 格式 / 故障隔离 ----")

    import tempfile

    # The log module resolves its directory from the config package at import
    # time, so point the whole app dir somewhere disposable for this test.
    sandbox = tempfile.mkdtemp(prefix="mclink-log-")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")

    sys.path.insert(0, os.path.join(ROOT, "client"))

    try:
        os.environ["XDG_CONFIG_HOME"] = sandbox

        for name in list(sys.modules):
            if name.startswith("mclink"):
                del sys.modules[name]

        from mclink import log as logmod
        from mclink.config import settings as cfgmod

        path = logmod.log_path()

        check(
            "日志文件在配置目录下（和 config 同处一个 AppData 目录）",
            path.endswith("craftlink-client.log") and "logs" in path,
            path,
        )

        logmod.event(
            "test.event",
            room="我的世界 一号",
            peer="1.2.3.4:51234",
            ok=True,
            count=7,
            nothing=None,
        )
        logmod.raw("HELLO", "HOST my-room cid user <token>")
        logmod.close()

        content = open(path, encoding="utf-8").read()

        check(
            "★ 真的落盘了（不是只在窗口里）",
            "EVENT test.event" in content,
            content.strip().splitlines()[-1][:100] if content.strip() else "(空)",
        )

        check(
            "行首带日期+毫秒时间戳（他的 Formatter 那一套）",
            content.splitlines()[0][:4].isdigit()
            and "." in content.splitlines()[0][:24],
            content.splitlines()[0][:26],
        )

        # Values are recorded RAW. A room name with a space must stay ONE token,
        # otherwise every downstream field split is off by one.
        check(
            "★ 带空格/中文的值仍然是【一个字段】（repr 保护，不能被空格撕开）",
            "room='我的世界 一号'" in content,
            [ln for ln in content.splitlines() if "room=" in ln][:1],
        )

        check(
            "布尔/数字不带引号（省得日志里全是引号）",
            "ok=true" in content and "count=7" in content,
        )

        check("None 字段直接省略，不写 key=null", "nothing=" not in content)

        check(
            "原样行带协议关键字前缀（事后才 grep 得到）",
            "HELLO HOST my-room cid user <token>" in content,
        )

        # The fields must be machine-parseable back into the ORIGINAL values,
        # even when a value contains a space. This is the whole reason the
        # values are Python-repr'd: a plain space-joined format would split a
        # Chinese room name across two fields and every later field would land
        # in the wrong column.
        line = [ln for ln in content.splitlines() if "room=" in ln][0]
        body = line.split("EVENT test.event", 1)[1].strip()

        fields = {}
        key = ""
        buf = ""
        depth_quote = False

        for char in body:
            if char == "'" and not depth_quote:
                depth_quote = True
                buf += char
                continue

            if char == "'" and depth_quote:
                depth_quote = False
                buf += char
                continue

            if char == " " and not depth_quote:
                if key:
                    fields[key] = buf
                key, buf = "", ""
                continue

            if char == "=" and not key and not depth_quote:
                key, buf = buf, ""
                continue

            buf += char

        if key:
            fields[key] = buf

        check(
            "★ 带空格的字段能被解析回原值（审计要的是「数据」）",
            fields.get("room") == "'我的世界 一号'"
            and fields.get("ok") == "true"
            and fields.get("count") == "7",
            str(fields),
        )

        # ---- failure isolation: an unwritable directory must not raise ----
        logmod.close()

        for name in list(sys.modules):
            if name.startswith("mclink"):
                del sys.modules[name]

        os.environ["XDG_CONFIG_HOME"] = "/proc/definitely-not-writable"
        from mclink import log as logmod2

        raised = False

        try:
            logmod2.event("test.unwritable", x=1)
            logmod2.close()
        except Exception:
            raised = True

        check(
            "★ 日志目录不可写时也不抛异常（只读安装必须能跑）",
            not raised,
        )
    finally:
        if old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old_xdg

        for name in list(sys.modules):
            if name.startswith("mclink"):
                del sys.modules[name]

        import shutil

        with contextlib.suppress(Exception):
            shutil.rmtree(sandbox)


def main():
    print("=" * 64)
    print("CraftLink i18n coverage test (英文/日文不得残留中文)")
    print("=" * 64)

    print("")
    print("---- 静态：控件里写死的中文必须有翻译（不需要 tkinter）----")

    missing = static_untranslated_texts()

    check(
        "所有控件的静态中文文案都在翻译表里（含 V3 网络诊断卡片）",
        not missing,
        " / ".join(f"{f}:{line} {text!r}" for f, line, text in missing[:12]),
    )

    print("")
    print("---- 静态：日志正文必须是英文（用户要拿日志做分析）----")

    chinese_logs = chinese_log_literals()

    check(
        "★ 所有日志调用的字面文字都是英文（日志是分析材料，不跟界面语言走）",
        not chinese_logs,
        " / ".join(f"{f}:{line} {text!r}" for f, line, text in chinese_logs[:12]),
    )

    test_client_log_module()

    try:
        import tkinter as tk
    except ImportError as e:
        check("tkinter available", False, str(e))
        return 1

    sandbox = tempfile.mkdtemp(prefix="mclink-i18n-")
    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox

    from mclink.config import settings
    from mclink.gui import app as gui_app
    from mclink.gui import i18n
    from mclink.gui import theme as theme_mod

    # 关掉弹窗，测试里没人点确定
    import tkinter.messagebox as mb

    mb.showwarning = lambda *a, **k: None
    mb.showerror = lambda *a, **k: None
    mb.showinfo = lambda *a, **k: None
    mb.askokcancel = lambda *a, **k: True
    mb.askyesno = lambda *a, **k: True

    try:
        root = tk.Tk()
    except Exception as e:
        check("Tk root can be created (display available)", False, repr(e))
        return 1

    try:
        root.withdraw()

        application = gui_app.MCLinkApp(root)
        application._on_mode_change()
        application._on_role_change()
        application._refresh_account_ui()
        application._refresh_room_ui()
        application._refresh_status()
        root.update_idletasks()

        # ---- 兜底行为 ----
        i18n.set_language("en")

        check(
            "没翻译过的句子原样返回（不是空白，也不是 key）",
            i18n.tr("这句故意没有翻译") == "这句故意没有翻译",
            i18n.tr("这句故意没有翻译"),
        )

        check(
            "tr() 对空串、None 这类输入不炸",
            i18n.tr("") == "" and i18n.tr(None) is None,
        )

        check(
            "未知语言代码退回中文",
            i18n.set_language("klingon") == "zh",
        )

        # ---- 名单一致性 ----
        check(
            "语言名单：config 与 i18n 一致",
            set(settings.UI_LANGUAGES)
            == {code for code, _label in i18n.LANGUAGES},
            f"{settings.UI_LANGUAGES} vs "
            f"{[c for c, _l in i18n.LANGUAGES]}",
        )

        check(
            "主题名单：config 与 gui.theme 一致",
            set(settings.UI_THEMES)
            == {code for code, _label in theme_mod.THEME_LABELS},
            f"{settings.UI_THEMES} vs "
            f"{[c for c, _l in theme_mod.THEME_LABELS]}",
        )

        # ---- 逐个语言：整棵树不许残留中文 ----
        for code in ("en", "ja"):
            i18n.set_language(code)

            application.language_var.set(i18n.language_label(code))
            application.on_language_change()

            root.update_idletasks()

            texts = collect_texts(root)
            leftovers = leftover_texts(texts, code)

            check(
                f"[{code}] 主界面没有残留中文（共 {len(texts)} 处文字）",
                not leftovers,
                "漏翻：" + " | ".join(leftovers[:30]),
            )

            # 弹窗也要覆盖：登录框 / 改名框
            from mclink.gui import auth_ui

            auth_ui.refresh_palette()

            dialog = None

            try:
                dialog = auth_ui.LoginDialog(
                    root, application.engine, "127.0.0.1", 19100,
                    username="", client_id="i18n-test",
                )
                dialog.update_idletasks()

                dialog_texts = collect_texts(dialog)
                dialog_leftovers = leftover_texts(dialog_texts, code)

                check(
                    f"[{code}] 登录窗口没有残留中文",
                    not dialog_leftovers,
                    "漏翻：" + " | ".join(dialog_leftovers[:30]),
                )
            finally:
                if dialog is not None:
                    with contextlib.suppress(Exception):
                        dialog.grab_release()
                        dialog.destroy()

        # ---- 软件名不跟随语言 ----
        i18n.set_language("en")
        application.language_var.set("English")
        application.on_language_change()
        root.update_idletasks()

        title = application.root.title()

        check(
            "★ 窗口标题里的软件名不随语言变化（CraftLink）",
            "CraftLink" in title,
            title,
        )

        header_texts = collect_texts(root)
        wordmark = [t for t in header_texts if t.strip() == settings.APP_NAME]

        check(
            "★ 页眉里的软件名是固定英文名（不翻译）",
            bool(wordmark),
            f"APP_NAME={settings.APP_NAME} 找到={wordmark}",
        )

        # ---- 日志正文是英文的（日志系统本身不跟随界面语言）----
        #
        # The log is the artefact the owner sends back for analysis, so it is
        # ENGLISH at the source -- engine, core and GUI alike -- and the file
        # and screen agree. The UI keeps its own language; only the LOG is
        # pinned. A Chinese log line must therefore stay Chinese (it is the
        # caller's literal text, not a translation key), which is what proves
        # tr() is no longer rewriting log bodies.
        application.log("testing the relay connection ...", kind="info")
        application.log("界面语言是中文但日志是英文", kind="info")
        application._append_logs()
        root.update_idletasks()

        log_content = application.log_text.get("1.0", "end")
        body = log_content.strip().splitlines()[-2:] if log_content.strip() else []

        check(
            "[en] 日志正文是英文原样（日志系统不跟随界面语言）",
            "testing the relay connection ..." in log_content,
            str(body),
        )
        check(
            "★ 日志正文不再被 tr() 改写（中文原文照旧，说明日志与界面语言解耦）",
            "界面语言是中文但日志是英文" in log_content,
            str(body),
        )

        # ---- 收尾：回中文 ----
        i18n.set_language("zh")
        theme_mod.set_theme("dark")

    except Exception:
        check("i18n test completed without exception", False)
        traceback.print_exc()
    else:
        check("i18n test completed without exception", True)
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
