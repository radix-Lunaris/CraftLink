#!/usr/bin/env python3
"""
CraftLink M5 安装器：**就地把值写进 compose 文件**（被 `install.sh` 调用）。

============================================================================
 为什么需要它
============================================================================

v3.9.20 的凭据策略是**硬编码**（用户 2026-10-05 的决定，见 SECURITY-NOTICE.md），
所以 `panel/docker-compose.yml` 与 `server/docker-compose.yml` 里写的是明文。
新机器安装时，安装器要生成一套**本机专属**的随机凭据并把它们落到这两个文件里
（以及面板端口、URL 安全入口这些因人而异的设置）。

用 sed 干这件事是错的：值里可能有 `/`、`&`、`"`、反斜杠，还会在 `ps` 里
暴露（`sed -i "s/.../$PASSWORD/"`）。所以统一走这个脚本：

  * 值从 **stdin** 一行一个 `KEY=VALUE` 读进来（不进 argv、不进 ps）；
  * 只改 `KEY:` 那一行的值，注释/顺序/其它键一个字不动；
  * 改完**回读校验**：没写进去就非零退出（安装器据此停住，不给半装状态）。

用法：

    printf '%s\\n' 'MCLINK_DB_PASSWORD=CHANGE_ME_REDACTED' | \\
        python3 scripts/install_patch.py --file panel/docker-compose.yml patch-env

    python3 scripts/install_patch.py --file panel/docker-compose.yml patch-port --port 19102

返回码：0 = 全部应用成功；1 = 有键没应用上（stderr 说明是哪个）。
"""

import argparse
import contextlib
import os
import re
import shutil
import sys


def read_lines(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")


def backup_once(path):
    backup = path + ".bak"

    if not os.path.exists(backup):
        with contextlib.suppress(Exception):
            shutil.copy2(path, backup)


def yaml_quote(value):
    """把值写成 YAML 双引号字符串（compose 里一直是这个风格）。"""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def unquote(text):
    text = text.strip()

    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]

    return text


def current_env_value(lines, key):
    """读取 `KEY: value` 的当前值（只认 environment 段那种写法）。"""
    pattern = re.compile(r"^(\s*)" + re.escape(key) + r"\s*:\s*(.*?)\s*$")

    for line in lines:
        match = pattern.match(line)

        if match:
            return unquote(match.group(2))

    return None


def patch_env(lines, pairs):
    """
    把 pairs（KEY -> VALUE）写进 compose 的 environment 段。

    返回 (改了几行, 没找到的键列表)。
    """
    changed = 0
    missing = []

    for key, value in pairs.items():
        pattern = re.compile(r"^(\s*)" + re.escape(key) + r"\s*:\s*(.*?)\s*$")
        replaced = False

        for index, line in enumerate(lines):
            match = pattern.match(line)

            if not match:
                continue

            lines[index] = f"{match.group(1)}{key}: {yaml_quote(value)}"
            replaced = True
            changed += 1
            break

        if replaced:
            continue

        # 这个键还不存在：插到第一个 `environment:` 段里（缩进比它多一级）。
        inserted = False

        for index, line in enumerate(lines):
            if not re.match(r"^\s*environment\s*:\s*$", line):
                continue

            indent = re.match(r"^(\s*)", line).group(1) + "  "
            lines.insert(index + 1, f"{indent}{key}: {yaml_quote(value)}")
            inserted = True
            changed += 1
            break

        if not inserted:
            missing.append(key)

    return changed, missing


#: 面板 compose 里那一行发布端口：`- "127.0.0.1:19102:80"`。
#: 只认"回环地址 + 80 容器端口"这一种形状 —— 面板的 nginx 听 80，
#: 宿主机上换成别的前缀端口就是安装器要做的事。
PORT_LINE_RE = re.compile(
    r'^(\s*-\s*")127\.0\.0\.1:(\d+):80("\s*)$'
)


def patch_port(lines, port):
    """
    换掉面板的发布端口。返回 (改了几行, 旧端口列表)。

    找不到那一行就返回 (0, []) —— 调用方据此决定是警告还是失败（面板 compose
    将来可能自带 nginx 变量，不该因为形状变了就拦死安装）。
    """
    changed = 0
    olds = []

    for index, line in enumerate(lines):
        match = PORT_LINE_RE.match(line)

        if not match:
            continue

        old = match.group(2)

        if old == str(port):
            continue

        olds.append(old)
        lines[index] = f'{match.group(1)}127.0.0.1:{port}:80{match.group(3)}'
        changed += 1

    return changed, olds


def cmd_patch_env(args):
    pairs = {}

    for line in sys.stdin.read().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip()

    if not pairs:
        print("[x] patch-env：stdin 里没有 KEY=VALUE", file=sys.stderr)
        return 1

    lines = read_lines(args.file)
    backup_once(args.file)

    changed, missing = patch_env(lines, pairs)

    if missing:
        for key in missing:
            print(
                f"[x] {args.file} 里找不到 {key}，也没有可插入的 environment 段",
                file=sys.stderr,
            )

        return 1

    write_lines(args.file, lines)

    # ---- 回读校验：写进去的必须**逐字**能读回来 ----
    verify_failed = []

    for key, value in pairs.items():
        if current_env_value(read_lines(args.file), key) != str(value):
            verify_failed.append(key)

    if verify_failed:
        for key in verify_failed:
            print(f"[x] {args.file}：{key} 写入后回读不一致", file=sys.stderr)

        return 1

    print(f"  [√] {args.file}：{changed} 个值已写入（回读一致）")
    return 0


def cmd_patch_port(args):
    lines = read_lines(args.file)
    backup_once(args.file)

    changed, olds = patch_port(lines, args.port)

    if not changed:
        # 已经是目标端口，或者找不到那一行（调用方自己判断要不要拦）。
        if PORT_LINE_RE.search("\n".join(lines)) is None:
            print(
                f"  [!] {args.file}：没有找到 `- \"127.0.0.1:<旧端口>:80\"` 那一行，"
                "端口没有改动",
                file=sys.stderr,
            )
            return 2 if args.strict else 0

        print(f"  [=] {args.file}：端口已经是 {args.port}")
        return 0

    write_lines(args.file, lines)

    if PORT_LINE_RE.search("\n".join(read_lines(args.file))) is None:
        print(f"[x] {args.file}：端口改写后回读失败", file=sys.stderr)
        return 1

    print(f"  [√] {args.file}：面板发布端口 {', '.join(olds)} -> {args.port}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="把安装时的值就地写进 compose（值从 stdin 读，不进 argv）"
    )
    parser.add_argument("--file", required=True, help="要改的 compose 文件")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("patch-env", help="从 stdin 读 KEY=VALUE 并写入 environment")
    p.set_defaults(func=cmd_patch_env)

    p = sub.add_parser("patch-port", help="换掉面板的发布端口（127.0.0.1:X:80）")
    p.add_argument("--port", type=int, required=True)
    p.add_argument(
        "--strict", action="store_true",
        help="找不到端口那一行时以非零退出（默认只警告）",
    )
    p.set_defaults(func=cmd_patch_port)

    args = parser.parse_args()

    if not os.path.isfile(args.file):
        print(f"[x] 找不到文件：{args.file}", file=sys.stderr)
        return 1

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
