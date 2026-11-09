"""
CraftLink -- M6 客户端界面流程（真 tkinter + 真界面，沙箱目录）。

============================================================================
 为什么还要一套界面测试
============================================================================

`tests/m6_client_config_test.py` 验的是数据层（配置、记忆、目录、迁移）。
但这一条里程碑里有一半是**用户看得到的流程**：

  * 地址空着的时候，界面上到底有没有告诉他下一步做什么？
  * 首次运行的引导窗口真的会弹出来吗？关掉之后会不会每次都烦人？
  * 「恢复默认」在没有品牌默认值时，是清空 + 说明，还是偷偷连一台服务器？
  * 「关于」页显示的品牌来源对不对？
  * 「数据目录」卡片改目录之后，文件真的跟着走了吗？

这些只有把界面**真的建出来**才能验。没有 tkinter / 没有显示器的机器上会
打印一行 skip 并返回 0（不是失败）—— 它不该拖住别的环境。

跑法：
    python3 tests/m6_client_gui_test.py
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

PASSED = []
FAILED = []
SKIPPED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def note(name):
    print(f"[SKIP] {name}", flush=True)
    SKIPPED.append(name)


def fresh_env(sandbox):
    """把用户目录整体指进沙箱（真目录一个字节都不碰）。"""
    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox
    os.environ.pop("MCLINK_DATA_DIR", None)
    os.environ.pop("MCLINK_BRANDING_FILE", None)


def top_levels(root):
    import tkinter as tk

    return [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]


def titles(root):
    found = []

    for win in top_levels(root):
        with contextlib.suppress(Exception):
            found.append(str(win.title()))

    return found


def destroy_extra(root, keep=()):
    for win in top_levels(root):
        if win in keep:
            continue

        with contextlib.suppress(Exception):
            win.destroy()


def all_widgets(node):
    """深度遍历一棵控件树（含自己）。"""
    found = [node]

    for child in node.winfo_children():
        found.extend(all_widgets(child))

    return found


def texts_of(node):
    """这棵树里所有控件上的静态文字（带 text 选项的那些）。"""
    found = []

    for widget in all_widgets(node):
        with contextlib.suppress(Exception):
            value = str(widget.cget("text") or "")

            if value.strip():
                found.append(value)

    return found


def main():
    print("=" * 70)
    print(" M6 客户端界面流程（真 tkinter · 沙箱）")
    print("=" * 70)

    try:
        import tkinter as tk
    except Exception as e:  # noqa: BLE001
        note(f"没有 tkinter（{e}）—— 跳过界面流程测试")
        return 0

    sandbox = tempfile.mkdtemp(prefix="craftlink-m6-gui-")
    fresh_env(sandbox)

    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        note(f"起不了 Tk 窗口（{e}）—— 跳过界面流程测试")
        shutil.rmtree(sandbox, ignore_errors=True)
        return 0

    original_env = {
        key: os.environ.get(key)
        for key in ("APPDATA", "XDG_CONFIG_HOME", "MCLINK_DATA_DIR",
                    "MCLINK_BRANDING_FILE")
    }

    try:
        root.withdraw()

        from mclink.config import settings
        from mclink.gui import app as gui_app

        # 弹窗一律替身：这套测试验的是"界面做了什么"，不是"用户点了什么"。
        shown = []

        gui_app.messagebox.showinfo = lambda *a, **k: shown.append(("info", a))
        gui_app.messagebox.showwarning = lambda *a, **k: shown.append(
            ("warn", a)
        )
        gui_app.messagebox.showerror = lambda *a, **k: shown.append(
            ("error", a)
        )
        gui_app.messagebox.askyesno = lambda *a, **k: False

        # ============================================================
        print("")
        print("---- 1. 地址为空：卡片上必须有引导 ----")

        application = gui_app.MCLinkApp(root)
        application._on_mode_change()
        application._on_role_change()
        root.update_idletasks()

        hint = application.server_hint_label.cget("text")

        check(
            "★ 服务器卡片上写着「请填写地址与端口」的引导（不是空白）",
            "地址" in hint and "端口" in hint,
            hint,
        )

        check(
            "空地址时提示是警告色（不是不起眼的灰）",
            str(application.server_hint_label.cget("fg")).lower()
            not in ("", str(gui_app.FG_MUTED).lower()),
            application.server_hint_label.cget("fg"),
        )

        check(
            "端口框里是空的（不是 0，也不是某个内置端口）",
            application.port_var.get() == "",
            repr(application.port_var.get()),
        )

        # ============================================================
        print("")
        print("---- 2. 首次运行引导 ----")

        application._maybe_show_first_run_guide()
        root.update_idletasks()

        guide_titles = titles(root)
        guide = None

        for win in top_levels(root):
            if "首次使用" in str(win.title()):
                guide = win
                break

        check(
            "★ 首次运行会弹出引导窗口",
            guide is not None,
            repr(guide_titles),
        )

        if guide is not None:
            joined = "\n".join(texts_of(guide))

            check(
                "引导里说清了「填地址 + 端口」",
                "地址" in joined and "端口" in joined,
                joined[:200],
            )

            check(
                "引导里说清了「填完会被记住」",
                "记住" in joined,
                joined[:200],
            )

            check(
                "引导里写了数据目录在哪（用户找得到自己的文件）",
                settings.config_dir() in joined,
                joined[:400],
            )

            # 关掉：不再当"第一次"。
            #
            # 注意：按钮是自绘的 RoundButton（Canvas），不是 tk.Button ——
            # 遍历时要用 cget("text") 找标签，而且控件树里混着 Frame 这类
            # 没有 -text 选项的控件，所以每一次都要兜住 TclError。
            for button in all_widgets(guide):
                label = ""

                with contextlib.suppress(Exception):
                    label = str(button.cget("text")).strip()

                if label in ("知道了", "Got it"):
                    with contextlib.suppress(Exception):
                        button.invoke()

            root.update_idletasks()

            check(
                "点掉引导窗口后它消失了",
                "首次使用：连接设置" not in titles(root),
                repr(titles(root)),
            )

            check(
                "看过之后 first_run 被清掉（但地址还空着 -> 下次仍会提示）",
                application.config.first_run is False,
            )

        # 关掉之后立刻再调一次：不重复弹（这一次已经"看过"了，且地址还空）
        application._first_run_guide_shown = False
        application._maybe_show_first_run_guide()
        root.update_idletasks()

        check(
            "★ 地址仍为空时会再次提示（空地址=连不上，不能静默）",
            "首次使用：连接设置" in titles(root),
            repr(titles(root)),
        )

        destroy_extra(root)

        # ============================================================
        print("")
        print("---- 3. 「恢复默认」= 品牌默认值（没有就清空 + 说明）----")

        shown.clear()
        application.on_reset_server()
        root.update_idletasks()

        check(
            "★ 没有品牌默认值时：清空地址与端口（不是偷偷连某台服务器）",
            application.host_var.get() == "" and application.port_var.get() == "",
            f"{application.host_var.get()!r}:{application.port_var.get()!r}",
        )

        check(
            "并且明确告诉用户「没有默认服务器」（不是静默失败）",
            any(kind == "info" for kind, _args in shown),
            repr(shown),
        )

        check(
            "卡片提示回到「请填写」状态",
            "地址" in application.server_hint_label.cget("text"),
            application.server_hint_label.cget("text"),
        )

        # ---- 填上地址：提示变成"随时可改" ----
        application.host_var.set("relay.example.net")
        application.port_var.set("23456")
        application._refresh_server_hint()

        check(
            "填好之后提示变成「随时可改、立即生效」",
            "随时可改" in application.server_hint_label.cget("text"),
            application.server_hint_label.cget("text"),
        )

        # ============================================================
        print("")
        print("---- 4. 关于页 ----")

        shown.clear()
        application.on_about()
        root.update_idletasks()

        about = None

        for win in top_levels(root):
            if str(win.title()) == "关于":
                about = win
                break

        check("★ 「关于」窗口能打开", about is not None, repr(titles(root)))

        if about is not None:
            joined = "\n".join(texts_of(about))

            check(
                "关于页显示软件名（默认 = 当前项目状态）",
                gui_app.cfgmod.APP_NAME in joined,
                joined[:200],
            )

            check(
                "关于页显示数据目录",
                settings.config_dir() in joined,
                joined[:400],
            )

            check(
                "关于页说明品牌来源（没有 branding.json 时写明用内置默认值）",
                "branding.json" in joined,
                joined[:400],
            )

            check(
                "关于页说明默认服务器未配置（客户端不含真实地址）",
                "未配置" in joined or "由用户填写" in joined,
                joined[:400],
            )

        destroy_extra(root)

        # ============================================================
        print("")
        print("---- 5. 数据目录卡片：改目录 -> 文件真的跟着走 ----")

        old_dir = settings.config_dir()
        new_dir = os.path.join(sandbox, "moved-data")

        # 先制造一个"旧目录里有东西"的状态。
        cfg = application.config
        cfg.server_host = "relay.example.net"
        cfg.server_port = 23456
        cfg.server_source = "user"
        settings.save_config(cfg)

        with open(os.path.join(old_dir, "marker.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("x")

        original_ask = gui_app.filedialog.askdirectory
        gui_app.filedialog.askdirectory = lambda *a, **k: new_dir

        try:
            shown.clear()
            application.on_change_data_dir()
            root.update_idletasks()
        finally:
            gui_app.filedialog.askdirectory = original_ask

        check(
            "★ 改完之后界面显示的就是新目录",
            application.data_dir_var.get() == os.path.abspath(new_dir),
            application.data_dir_var.get(),
        )

        check(
            "★ 配置文件真的出现在新目录",
            os.path.isfile(os.path.join(new_dir, "mclink_config.json")),
            repr(sorted(os.listdir(new_dir)) if os.path.isdir(new_dir) else None),
        )

        check(
            "日志目录跟着新建",
            os.path.isdir(os.path.join(new_dir, "logs")),
        )

        check(
            "★ 旧目录里的文件保留着（不替用户删数据）",
            os.path.isfile(os.path.join(old_dir, "marker.txt")),
        )

        check(
            "迁移结果有明确提示（不是悄悄换目录）",
            any(kind == "info" for kind, _args in shown),
            repr(shown[:2]),
        )

        check(
            "旧目录不再被当作当前目录",
            settings.config_dir() == os.path.abspath(new_dir),
            settings.config_dir(),
        )

        # ============================================================
        print("")
        print("---- 6. 声明式检查：客户端里没有内置服务器 ----")

        check(
            "界面常量里没有默认地址/端口",
            settings.default_server_host() == ""
            and settings.default_server_port() == 0,
            f"{settings.default_server_host()!r}:"
            f"{settings.default_server_port()!r}",
        )

        check(
            "「关于」页里的品牌路径来自 branding.py（单一来源）",
            gui_app.branding_mod.load().source == "",
        )

    finally:
        with contextlib.suppress(Exception):
            root.destroy()

        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        shutil.rmtree(sandbox, ignore_errors=True)

    print("")
    print("=" * 70)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed"
          + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))

    for name in FAILED:
        print(f"  FAILED: {name}")

    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
