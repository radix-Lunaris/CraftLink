#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CraftLink-Builder -- 入口（GUI + CLI 二合一）。

一个可执行文件，两种用法：

    CraftLink-Builder.exe                       # 双击 = 图形界面
    CraftLink-Builder.exe --non-interactive --json 输入.json
    CraftLink-Builder.exe --selftest            # 离线自检（不写任何东西）
    CraftLink-Builder.exe --print-json-template # 打印 CI 用的输入模板

它做的事（详见 docs/INSTALLER.md）：

    输入：软件名 / 中继域名或地址 + 端口 / logo / 可选默认安装目录 / 可选默认
          用户数据目录 …
    输出：branding.json（字段与回退规则严格按 docs/CLIENT-BRANDING.md）、
          logo、Inno Setup 的编译参数（params.iss），以及"下一步怎么编译出
          CraftLink-Setup.exe"的明确指令（打印 + NEXT-STEPS.txt）。

离线：只用标准库，不发任何网络请求。

硬规则：不硬编码服务器地址、不写死安装路径（默认值都是可改的默认值）。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

if HERE not in sys.path:
    sys.path.insert(0, HERE)

import builder_core          # noqa: E402

#: 仓库根：installer/builder/ -> 上两级。
DEFAULT_REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

#: 字段化参数表：(flag, dest, metavar/类型, 帮助)。顺序 = --help 里的顺序。
FIELD_FLAGS = [
    ("--app-name", "app_name", "名字", "软件名（窗口标题/开始菜单/关于页）"),
    ("--server-host", "server_host", "域名或IP", "中继服务器域名或 IP；留空 = 没有内置服务器（D10）"),
    ("--server-port", "server_port", "端口", "中继服务器端口（1-65535）；留空/0 = 未设置"),
    ("--update-url", "update_url", "URL", "版本不一致时提示的下载页（可选）"),
    ("--logo", "logo", "文件", "品牌 logo（.png/.gif/.ppm/.pgm 关于页图片 或 .ico 窗口图标）"),
    ("--icon", "icon", "文件", "安装器/快捷方式图标 .ico（可选；给了 .png logo 时会自动生成）"),
    ("--default-install-dir", "default_install_dir", "目录", "安装目录**默认值**（可空 = 系统程序目录\\软件名）"),
    ("--default-data-dir", "default_data_dir", "目录",
     "用户数据目录**默认值**（可空 = %%APPDATA%%\\CraftLink）"),
    ("--out", "out_dir", "目录", "输出目录（默认 installer/build）"),
    ("--exe", "exe", "文件", "主程序 exe（默认 client/dist/CraftLink.exe）"),
    ("--console-exe", "console_exe", "文件", "诊断版 exe（默认自动找 CraftLink-console.exe）"),
    ("--exe-name", "exe_name", "名字", "安装后的 exe 文件名（默认沿用源文件名）"),
    ("--app-version", "app_version", "版本", "版本号（默认读客户端 settings.py）"),
    ("--app-publisher", "app_publisher", "发布者", "发布者（进卸载信息/注册表记录）"),
    ("--publisher-url", "publisher_url", "URL", "发布者网址（可选）"),
    ("--app-id", "app_id", "GUID", "安装身份 GUID（默认仓库固定值；换品牌独立发布时换掉）"),
    ("--output-basename", "output_basename", "名字", "安装包文件名（不含 .exe，默认 CraftLink-Setup）"),
    ("--license-file", "license_file", "文件", "许可文件（默认自动用仓库根 LICENSE）"),
    ("--language-file", "language_file", "isl", "Inno 语言文件（如 compiler:Languages\\ChineseSimplified.isl）"),
    ("--language-name", "language_name", "名字", "上面语言文件的 Name（默认 custom）"),
    ("--min-windows", "min_windows", "版本", "最低 Windows 版本（默认 10.0）"),
]

BOOL_FLAGS = [
    ("--data-dir-page", "data_dir_page", "安装时询问用户数据目录（默认开）"),
    ("--desktop-icon-task", "desktop_icon_task", "提供桌面快捷方式选项（默认开）"),
    ("--env-var-task", "env_var_task", "提供 MCLINK_DATA_DIR 环境变量选项（默认开，复选框默认不勾）"),
]

#: JSON 输入里被显式接受的键（多余键会被忽略，见 builder_core.inputs_from_mapping）。
def build_parser():
    parser = argparse.ArgumentParser(
        prog="CraftLink-Builder",
        description=(
            "CraftLink 安装器构建器：生成 branding.json / logo / Inno Setup 参数，"
            "并告诉你下一步怎么编译出 CraftLink-Setup.exe。"
            "不带参数双击运行 = 图形界面。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  CraftLink-Builder.exe\n"
            "  CraftLink-Builder.exe --non-interactive --json 输入.json\n"
            "  CraftLink-Builder.exe --non-interactive --app-name 方块特快 "
            "--server-host relay.example.com --server-port 19100\n"
            "  CraftLink-Builder.exe --print-json-template > 输入.json\n"
            "  CraftLink-Builder.exe --selftest\n"
        ),
    )

    parser.add_argument("--non-interactive", action="store_true",
                        help="不开图形界面，按参数/JSON 生成（CI 用）")
    parser.add_argument("--json", metavar="文件",
                        help="从 JSON 文件读全部输入（隐含 --non-interactive）")
    parser.add_argument("--texts", metavar="JSON|文件",
                        help="覆盖安装器界面文本（JSON 对象或 .json 文件路径）")
    parser.add_argument("--gui", action="store_true", help="强制打开图形界面")
    parser.add_argument("--cli", action="store_true", help="强制命令行模式")
    parser.add_argument("--repo-root", metavar="目录",
                        help="仓库根目录（默认按本文件位置推导）")
    parser.add_argument("--check", action="store_true",
                        help="只校验输入，不写任何文件")
    parser.add_argument("--force", action="store_true",
                        help="允许写进一个已有内容的、不像是 Builder 的输出目录")
    parser.add_argument("--quiet", action="store_true", help="只打印最少信息")
    parser.add_argument("--print-json-template", action="store_true",
                        help="打印 --json 输入模板后退出")
    parser.add_argument("--selftest", action="store_true",
                        help="跑离线自检（Builder + 生成物断言）后退出")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")

    for flag, dest, metavar, help_text in FIELD_FLAGS:
        parser.add_argument(flag, dest=dest, metavar=metavar, default=None, help=help_text)

    for flag, dest, help_text in BOOL_FLAGS:
        parser.add_argument(flag, dest=dest, action="store_true", default=None, help=help_text)
        parser.add_argument("--no-" + flag[2:], dest=dest, action="store_false",
                            default=None, help="关闭：" + help_text)

    return parser


def parse_texts(raw):
    """--texts 既接受 JSON 字符串，也接受 .json 文件路径。"""
    if not raw:
        return None

    text = raw.strip()

    if os.path.isfile(text):
        with open(text, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = json.loads(text)

    if not isinstance(data, dict):
        raise ValueError("--texts 需要是一个 JSON 对象")

    return data


def inputs_from_args(args):
    """CLI 参数 -> Inputs（先铺 JSON，再用显式命令行参数覆盖）。"""
    data = {}

    if args.json:
        with open(args.json, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        if not isinstance(data, dict):
            raise ValueError(f"--json 文件里必须是一个 JSON 对象：{args.json}")

    for _flag, dest, _metavar, _help in FIELD_FLAGS:
        value = getattr(args, dest, None)

        if value is not None:
            data[dest] = value

    for _flag, dest, _help in BOOL_FLAGS:
        value = getattr(args, dest, None)

        if value is not None:
            data[dest] = value

    texts = parse_texts(args.texts)

    if texts:
        merged = dict(data.get("texts") or {})
        merged.update(texts)
        data["texts"] = merged

    return builder_core.inputs_from_mapping(data)


def hide_own_console():
    """
    冻结成单文件 exe 时，`--console` 构建在 GUI 模式下会带出一个黑框。

    只在"这个控制台是本进程独占"时才隐藏（GetConsoleProcessList 只列出自己），
    这样从已有终端里跑 CLI 时绝不会把别人的终端弄没。
    """
    if not sys.platform.startswith("win") or not getattr(sys, "frozen", False):
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        processes = (ctypes.c_uint * 4)()
        count = kernel32.GetConsoleProcessList(processes, 4)

        if count == 1:
            user32 = ctypes.windll.user32
            user32.ShowWindow(kernel32.GetConsoleWindow(), 0)   # SW_HIDE
    except Exception:
        pass


def wants_gui(args, argv):
    if args.gui:
        return True

    if args.cli or args.non_interactive or args.json or args.check or args.quiet:
        return False

    if args.print_json_template or args.selftest or args.version:
        return False

    # 有任何业务参数 = 命令行用法；一个参数都没有 = 双击，开 GUI。
    for flag, dest, _metavar, _help in FIELD_FLAGS:
        if getattr(args, dest, None) is not None:
            return False

    for flag, dest, _help in BOOL_FLAGS:
        if getattr(args, dest, None) is not None:
            return False

    return True


def print_report(report, quiet=False):
    if quiet:
        for label, path in (report.get("outputs") or {}).items():
            if path:
                print(path)

        print(report.get("next_steps", ""))

        return

    for warning in report.get("warnings") or []:
        print(f"[提醒] {warning}", file=sys.stderr)

    print(report.get("next_steps", ""))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"{builder_core.BUILDER_NAME} {builder_core.BUILDER_VERSION}")

        return 0

    if args.print_json_template:
        print(json.dumps(builder_core.json_template(), ensure_ascii=False, indent=2))
        print(
            "\n# 用法： CraftLink-Builder --non-interactive --json 这个文件.json",
            file=sys.stderr,
        )

        return 0

    repo_root = os.path.abspath(
        args.repo_root or builder_core.find_repo_root() or DEFAULT_REPO_ROOT
    )

    if args.selftest:
        sys.path.insert(0, os.path.join(repo_root, "installer", "tests"))

        import builder_selftest            # noqa: PLC0415

        return builder_selftest.main(["--repo-root", repo_root])

    if wants_gui(args, argv):
        hide_own_console()

        import builder_gui                 # noqa: PLC0415

        return builder_gui.run(repo_root=repo_root)

    try:
        inputs = inputs_from_args(args)
    except (OSError, ValueError) as exc:
        print(f"✗ 输入有问题：{exc}", file=sys.stderr)

        return 2

    if args.check:
        facts = builder_core.read_client_facts(repo_root)
        result = builder_core.validate(inputs, repo_root, facts)

        for warning in result.warnings:
            print(f"[提醒] {warning}", file=sys.stderr)

        if result.errors:
            for message in result.errors:
                print(f"✗ {message}", file=sys.stderr)

            return 2

        print("✓ 输入校验通过（没有写任何文件）")

        return 0

    try:
        report = builder_core.build(
            inputs, repo_root, out_dir=inputs.out_dir or None, force=args.force
        )
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"✗ 生成失败：{exc.__class__.__name__}: {exc}", file=sys.stderr)

        return 1

    if not report.get("ok"):
        for message in report.get("errors") or []:
            print(f"✗ {message}", file=sys.stderr)

        return 2

    print_report(report, quiet=args.quiet)

    return 0


if __name__ == "__main__":
    sys.exit(main())
