#!/usr/bin/env python3
"""
CraftLink —— 版本号升级工具（**每次发新版都必须用它，不要手改 settings.py**）。

============================================================================
 为什么要有这个脚本
============================================================================

用户 2026-09-25 的要求（原话）：

    以后每次更新你都会保证服务器版本号握手校验更新吗？
    不要再出现拒绝最新版本的情况了。

那次事故：给握手行加版本号时漏了房主的**控制连接**（`HOST <room>`），
于是版本完全一致的客户端被服务端判成"版本不对"，用户连不上、还没有弹窗。

版本号在四个地方必须一致，手改必然漏一个：

    1. client/mclink/config/settings.py 的 APP_VERSION   ← 唯一来源
    2. deploy/server/VERSION                             ← 中继启动时读它
    3. 服务端镜像里的 VERSION（Dockerfile 的 COPY VERSION）
    4. 客户端的 exe（重建才有，标题栏能看到）

这个脚本做 1 + 2，并把剩下的**必做步骤**打印出来。它不接受"我只改了
settings.py"这种状态：写完之后 `deploy/server/VERSION` 立刻是新的，而
`check_deploy.py` 会发现 exe 比源码旧 —— 逼着你重建。

用法：

    python3 scripts/bump-version.py 3.0.9

跑完必须依次做完它打印的三件事（重建 exe / 同步 deploy / 跑测试），
`python3 scripts/check_deploy.py` 全绿才算发版完成。
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SETTINGS = os.path.join(ROOT, "client", "mclink", "config", "settings.py")
VERSION_FILE = os.path.join(ROOT, "deploy", "server", "VERSION")

VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"([^"]+)"\s*$', re.M)


def read_current():
    with open(SETTINGS, encoding="utf-8") as fh:
        text = fh.read()

    match = VERSION_RE.search(text)

    if not match:
        print("[FAIL] settings.py 里找不到 APP_VERSION")
        sys.exit(2)

    return text, match.group(1)


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        print("用法：python3 scripts/bump-version.py <新版本号，例如 3.0.9>")
        return 2

    new = argv[1].strip()

    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        print(f"[FAIL] 版本号必须是 x.y.z 三段数字，收到 {new!r}")
        return 2

    text, old = read_current()

    if new == old:
        print(f"[FAIL] 新版本号和当前一样（{old}）—— 没改就没必要发版")
        return 2

    def key(v):
        return tuple(int(p) for p in v.split("."))

    if key(new) < key(old):
        print(f"[FAIL] 新版本号 {new} 比当前 {old} 还小；版本只能往上走")
        return 2

    print("=" * 64)
    print(f"版本号：{old}  ->  {new}")
    print("=" * 64)

    # ---- 1. settings.py（唯一来源）----
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        fh.write(VERSION_RE.sub(f'APP_VERSION = "{new}"', text, count=1))

    print(f"  [OK] {os.path.relpath(SETTINGS, ROOT)}  APP_VERSION = {new}")

    # ---- 2. 同步 deploy（写 server/VERSION + BUILD.txt + README）----
    result = subprocess.run(
        [sys.executable, os.path.join(HERE, "sync-deploy.py")],
        cwd=ROOT,
    )

    if result.returncode != 0:
        print("[FAIL] sync-deploy.py 失败，版本只改了一半，去查上面的输出")
        return result.returncode

    if not os.path.isfile(VERSION_FILE):
        print(f"[FAIL] {os.path.relpath(VERSION_FILE, ROOT)} 没生成出来")
        return 1

    with open(VERSION_FILE, encoding="utf-8") as fh:
        written = fh.read().strip()

    if written != new:
        print(f"[FAIL] server/VERSION 里是 {written!r}，应该是 {new!r}")
        return 1

    print(f"  [OK] {os.path.relpath(VERSION_FILE, ROOT)} = {written}")

    # ---- 3. 还差什么，说清楚 ----
    print("")
    print("=" * 64)
    print("还差三步（不做完就是「改了源码、发的是旧版」）：")
    print("=" * 64)
    print("""
  1) 重建客户端 exe（版本号写进 exe，标题栏能看到）：
       cd /tmp && cmd.exe /c "C:\\Windows\\Temp\\clb4.bat"
       cp client/dist/CraftLink.exe client/dist/CraftLink-console.exe deploy/

  2) 再同步一次（把新 exe 和清单对齐）：
       python3 scripts/sync-deploy.py

  3) 跑门禁（**这一条最重要**：它会用真客户端连一台同版本的中继，
     并检查服务端日志里没有任何 VERSION REJECT）：
       python3 scripts/check_deploy.py
       XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/run_all.py

  全绿之后：把 deploy/ 整包传到 /opt/mc-link/，跑 mcr（= rebuild.sh）。
  **服务端和客户端必须同一次发版**；只发一边 = 客户端被自己的服务器拒掉。
""")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
