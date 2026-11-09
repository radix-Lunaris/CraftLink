#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CraftLink -- 交付助手（本地运行）

============================================================================
 它解决什么
============================================================================

每次交付都要做同一串动作：找 deploy 目录、复制到服务器、在服务器上敲
一行重建命令。其中"找目录"和"记命令"是纯浪费 —— 这个脚本把它们压成
一条命令。

    python scripts/deploy-now.py              # 自检 + 打印上传/重建步骤
    python scripts/deploy-now.py --check      # 只做交付前自检
    python scripts/deploy-now.py --open       # 顺便打开 deploy 目录

它做两件事：

  1. **交付前自检**：跑 `check_deploy.py`（凭据不是占位符、COPY 完整、
     exe 比源码新）。**不通过就明确说不要上传** —— 上传一个坏的包比不传更糟。
  2. **打印交付物位置和哈希**，方便你核对传上去的是不是这一份。
  3. **打印服务器上要敲的命令**，并尽量复制到剪贴板。

============================================================================
 关于服务器上的命令（重要）
============================================================================

上传完 deploy 的全部内容之后，服务器上**只需要一条**：

    bash /opt/mc-link/rebuild.sh

以前要先洗 CRLF：

    sed -i 's/\\r$//' /opt/mc-link/*.sh && bash /opt/mc-link/rebuild.sh

现在不需要了：`rebuild.sh` 自己会把 /opt/mc-link/*.sh 的 CRLF 洗掉
（CRLF 的坑是 bash 只报 `$'\\r': command not found`，完全不提原因）。
旧命令仍然有效，做了也没坏处。

Run:
    python scripts/deploy-now.py
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEPLOY = os.path.join(ROOT, "deploy")

#: 服务器上的重建命令。**唯一的一条。**
SERVER_COMMAND = "bash /opt/mc-link/rebuild.sh"

#: 想看它一步步说什么，或者想强制核对文件指纹时用这条。
SERVER_COMMAND_STRICT = "bash /opt/mc-link/rebuild.sh --strict"


def run_precheck():
    """Run the deployment precheck. Returns True when it is safe to upload."""
    print("=" * 64)
    print(" 交付前自检")
    print("=" * 64)

    check = os.path.join(HERE, "check_deploy.py")

    if not os.path.isfile(check):
        print("  [!] 找不到 scripts/check_deploy.py，跳过自检")
        return True

    completed = subprocess.run([sys.executable, check], cwd=ROOT)

    print("")

    if completed.returncode != 0:
        print("=" * 64)
        print(" 自检没通过 —— 先别上传，见上面的 [FAIL]")
        print("=" * 64)
        return False

    print("=" * 64)
    print(" 自检通过，可以上传")
    print("=" * 64)

    return True


def open_deploy_folder():
    """Open the deploy directory in the OS file manager, best effort."""
    if not os.path.isdir(DEPLOY):
        print(f"  [!] 没有 {DEPLOY}（先跑 scripts/sync-deploy.py）")
        return False

    if sys.platform.startswith("win"):
        os.startfile(DEPLOY)  # noqa: S606 - intended
        return True

    # WSL/Linux: prefer explorer.exe so it opens on the WINDOWS side, which
    # is where the user is dragging from.
    explorer = shutil.which("explorer.exe")

    if explorer:
        # explorer.exe returns a non-zero exit code even on success, so the
        # return value is deliberately not checked.
        subprocess.Popen([explorer, _wsl_path(DEPLOY)])  # noqa: S603
        return True

    for opener in ("xdg-open", "open"):
        tool = shutil.which(opener)

        if tool:
            subprocess.Popen([tool, DEPLOY])  # noqa: S603
            return True

    print(f"  [!] 没找到文件管理器，手动打开：{DEPLOY}")
    return False


def _wsl_path(path):
    """Translate a WSL path to its Windows UNC form for explorer.exe."""
    try:
        completed = subprocess.run(
            ["wslpath", "-w", path], capture_output=True, text=True, timeout=5,
        )

        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    except Exception:
        pass

    return path


def copy_to_clipboard(text):
    """Best effort. Never fatal: the command is printed anyway."""
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["clip"], input=text.encode("utf-16le"), check=True,
                timeout=5,
            )
            return True
        except Exception:
            return False

    for tool, args in (("clip.exe", []), ("xclip", ["-selection", "clipboard"]),
                       ("wl-copy", [])):
        binary = shutil.which(tool)

        if not binary:
            continue

        try:
            subprocess.run(
                [binary] + args, input=text.encode("utf-8"), check=True,
                timeout=5,
            )
            return True
        except Exception:
            continue

    return False


def print_instructions():
    print("")
    print("=" * 64)
    print(" 交付步骤")
    print("=" * 64)
    print("")
    print("  1) 把 deploy 目录里的【全部内容】拖到服务器的 /opt/mc-link/")
    print("     —— 整包覆盖，不要一个一个文件传（漏传的失败模式很难查）。")
    print("       提示覆盖时选【全部覆盖】。")
    print("")
    print("  2) 在服务器上执行【一条】命令：")
    print("")
    print(f"        {SERVER_COMMAND}")
    print("")
    print("     （它会自己洗掉脚本的 CRLF，然后核对指纹、重建、自证。")
    print("       想强制核对指纹就用：")
    print(f"        {SERVER_COMMAND_STRICT}）")
    print("")
    print("  3) 只重建一部分时：")
    print("        bash /opt/mc-link/rebuild.sh relay   # 只重建中继（会断开玩家）")
    print("        bash /opt/mc-link/rebuild.sh panel   # 只重建面板（不影响玩家）")
    print("")
    print("  注意：覆盖【不会】删掉服务器上的多余文件。")
    print("")

    if copy_to_clipboard(SERVER_COMMAND):
        print("  （重建命令已复制到剪贴板）")
        print("")


def summarize():
    """Where the deliverable is, and the hashes, for the record."""
    exe = os.path.join(DEPLOY, "CraftLink.exe")

    print("")
    print("=" * 64)
    print(" 本次交付物")
    print("=" * 64)
    print(f"  目录：{DEPLOY}")

    if os.path.isfile(exe):
        import hashlib

        size = os.path.getsize(exe)
        digest = hashlib.sha256(open(exe, "rb").read()).hexdigest()
        print(f"  CraftLink.exe：{size:,} 字节")
        print(f"  SHA-256      ：{digest}")

    print("")


def main():
    parser = argparse.ArgumentParser(description="CraftLink 交付助手")
    parser.add_argument("--check", action="store_true",
                        help="只做交付前自检，不打印步骤")
    parser.add_argument("--open", action="store_true",
                        help="顺手打开 deploy 目录（默认不打开）")

    args = parser.parse_args()

    ok = run_precheck()

    if args.check:
        return 0 if ok else 1

    if not ok:
        print("")
        print(" 自检不过就不给步骤了 —— 修完再跑一次本脚本。")
        return 1

    summarize()

    if args.open:
        open_deploy_folder()

    print_instructions()

    return 0


if __name__ == "__main__":
    sys.exit(main())
