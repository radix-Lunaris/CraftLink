#!/usr/bin/env python3
"""
生成 `BUILD.txt`（文件指纹）与 `README.txt`（上传统清单）。

============================================================================
 为什么它必须存在
============================================================================

v3.9.15 的 `BUILD.txt` 只有 47 个条目，**不含 `client/` 下的 50 个文件**；
`README.txt` 自我声明"共 52 个文件"，实际包内 85 个。

也就是说，这套"交付自证"机制对客户端源码完全不成立：
`sha256sum -c BUILD.txt` 通过，并不能证明客户端是新的。而它最想防的那种事故
（"web/ 传了、lib/ 没传"）恰恰就发生在客户端身上 —— 手工维护的清单漏一个
文件，是迟早的事（历史上漏过 `db_schema.py`，直接把中继搞成重启循环）。

所以清单与指纹**一律自动生成**：人不再有机会漏。

============================================================================
 用法
============================================================================

    python3 scripts/make_build.py                 # 生成 BUILD.txt + README.txt
    python3 scripts/make_build.py --check         # 只校验，不写（CI 用）

`--check` 在指纹对不上时以退出码 1 结束，可以挂进 CI / 发版门禁。
"""

import argparse
import contextlib
import hashlib
import os
import shutil
import sys

#: 包根的判定：脚本自己在 `scripts/` 下，包根是它的上一级。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 这些**不进**清单：本地产物 / 机密 / 版本控制元数据。
#:
#: 注意 `.env` 与 `.env.example` 的区别：`.env` 里有真实凭据，绝不能进包；
#: `.env.example` 只有占位符，是**要交付**的（服务器上 `cp` 一下就能用）。
EXCLUDED_DIRS = {
    ".git",
    # ------------------------------------------------------------------
    #  ★ v3.9.18：`__pycache__` **不进包**（复核报告 3.4）
    # ------------------------------------------------------------------
    # 本地跑测试 / 门禁会在源码树里留下 7 个 `__pycache__` 目录。它们本来被
    # 排除了 —— 但打包用的是 `tar`/`zip`（见交付流程），不走这个脚本，所以
    # 照样被带了进去。这里保留排除（脚本自身生成的清单正确），同时在脚本里
    # 提供 `--clean` 一键清掉，打包前跑一次即可。
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "logs",
    "status",
}

EXCLUDED_FILES = {
    ".env",
    ".DS_Store",
    "Thumbs.db",
    # ------------------------------------------------------------------
    #  ★ v3.9.17：`LAST_VERSION` 是**仓库里**的"上一版发版记录"，用来拦住
    #    "修复了但忘了 +1"。它不该进交付包 —— 否则用户拿到包一跑
    #    `check_deploy.py`，当前版本 == 包内记录的已发版本，会被自己的门禁
    #    拦住（"版本号没有变大"），看着像包有问题。
    #
    #    纪律在仓库侧执行，交付包不受影响。
    # ------------------------------------------------------------------
    "LAST_VERSION",
}

#: 这些**不进** BUILD.txt（它们本来就是每次生成的，指纹不可能对得上 ——
#: 把它们算进去等于让自查永远失败）。但仍然进 README 的清单。
#: ★ 2026-10-04（用户口径）：**交付物与指纹清单分开**。
#: 两个客户端 exe 是给玩家的交付物，**服务器上不需要**；顶层 .md（HANDOVER /
#: 修复说明 / SECURITY-NOTICE）是给人看的文档。它们留在包里照旧交付，但**不进
#: BUILD.txt** —— 否则 rebuild.sh 的 `sha256sum -c` 必然报"这些文件和服务器的对不上"。
UNFINGERPRINTED = {"BUILD.txt", "README.txt"}

UNFINGERPRINTED_SUFFIXES = (".exe", ".zip")


def _unfingerprinted(rel, name):
    """进包、进 README，但**不进 BUILD.txt**（= 不上传服务器的东西）。"""
    if rel in UNFINGERPRINTED or name in UNFINGERPRINTED:
        return True

    if name.lower().endswith(UNFINGERPRINTED_SUFFIXES):
        return True

    if "/" not in rel and name.lower().endswith(".md"):
        return True

    return False

README_TEMPLATE = """CraftLink v5.1.0 部署与客户端源码包
================================================================
本目录结构 = 服务器上的目录结构，照着拖拽就不会放错位置。

v5.1.0（2026-10-26）：CustomTkinter 全新侧栏与工作台，深色/浅色/跟随系统；
联机、房间、连接状态、日志、设置与账号五个分区，账号弹窗同步替换。
保留现有网络引擎、手动两模式、AUTO、64 人容量和面板功能。
未实现新 API 或 Host 漂移；本版改动说明与验证见 docs/V5.1.0-CHANGES.md。
现有严格版本校验继续沿用：需同步更新服务端版本文件与两端客户端。
完整上传后仅重建中继即可；面板源码和预构建镜像与 v5.0.1 相同，无需重建。

!! 这个 README 是由 scripts/make_build.py 自动生成的 !!
   不要手工编辑 —— 人工维护的清单漏过 db_schema.py，
   直接把中继搞成了重启循环。

AUTO 使用方式
================================================================

  房主创建 AUTO 房间，Host 和玩家分别点击「开始连接」。
  无需先运行游戏或监听目标端口：先建立中继网络隧道，后台逐玩家尝试直连。
  网络通道验证后自动切换，备用中继持续保留；任意 TCP 应用稍后接入。
  直连断开会自动回退。切换房间策略（手动模式与 AUTO）会中断游戏连接。
  v5 客户端与 v5 中继须配套更新；旧客户端不能进入 AUTO 房间。

  详细设计和验收状态：docs/AUTO-PROTOCOL.md、docs/V5-VALIDATION.md。
  network-r1 修复、OpenClash 对照复测：docs/V5-NETWORK-R1.md。
  当前修订 network-r2：修复反复刷新／取消短 TCP 探测导致共享 P2P 被关闭。
  两端使用本目录新的 EXE，详见 docs/V5-NETWORK-R2.md。
  两端 EXE 与服务端须同步更新，旧服务端不支持独立 AUTO_NETWORK 隧道。
  本地验证不能代替真实公网 NAT 和多人 Minecraft 实测。

客户端构建与自检
================================================================

    python -m pip install -r requirements-build.txt
    python scripts/build_client.py
    python client/main.py              # 源码运行前 pip install -r requirements.txt
    CraftLink-console.exe --version
    CraftLink-console.exe --self-test
    python -B tests/run_all.py
    python -B scripts/smoke_ui.py
    python -B scripts/audit_ui.py

  CraftLink.exe 为图形版，CraftLink-console.exe 保留控制台便于排查。
  构建脚本会对两个新 EXE 各自执行版本、协议及模块自检后才替换交付文件。


部署凭据策略
================================================================

沿用 v3.9.20 已登记的 hardcoded 策略，参见 SECURITY-NOTICE.md。
不要把此目录当成已经完成凭据外置的版本；.env.example 是外置方案的模板。
部署时使用本目录配套的 server/、panel/、rebuild.sh 和 BUILD.txt。


上传统清单（共 {count} 个文件）
================================================================

{listing}

================================================================
【推荐】整包覆盖 —— 不要一个一个文件传
================================================================

  把本目录内的服务端内容（server/、panel/、client/、*.sh、BUILD.txt）
  一次性拖到服务器的 /opt/mc-link/ 下，提示覆盖时选【全部覆盖】。
  目录结构和服务器上完全一样。

  覆盖完之后，服务器上【只需要一条命令】：

      bash /opt/mc-link/rebuild.sh

  不必再先手动修行尾了。rebuild.sh 现在自己会把 /opt/mc-link/*.sh
  的 CRLF 洗掉。

  为什么推荐：逐个文件上传失败过很多次，症状永远是「改了但没生效」
  （web/ 传了、lib/ 没传 → 面板整站 500；index.php 传了、
  lib/common.php 没传 → Call to undefined function）。
  整包覆盖没有这个失败模式：要么每个文件都是新的，要么覆盖时报错。

  覆盖不会出事的文件：
    panel/config.php           不在包里（服务器上唯一存着数据库密码的
                               文件，永远不要覆盖它）
    /opt/mc-link/.env          不在包里（保留服务器原有的本地配置）

  唯一要注意：覆盖【不会】删除服务器上的多余文件
  （例如以前手工放的 _diag.php、_diag2.php），那些自己删掉即可；
  rebuild.sh 结束时会再提醒一次。


上传统清单（共 {count} 个文件）
================================================================

（想单独补某个文件时照这个清单来；路径和服务器上完全一致）


重建 / 自证
================================================================

    bash /opt/mc-link/rebuild.sh          # 两个容器都重建 + 自证
    bash /opt/mc-link/rebuild.sh panel    # 只重建面板（不断开玩家）
    bash /opt/mc-link/rebuild.sh panel --prebuilt-panel  # amd64 使用随包镜像，免下载编译
    bash /opt/mc-link/rebuild.sh relay    # 只重建中继（会断开玩家）

它会自己检查：文件齐不齐（一个一个点名）、凭据是不是占位符、
Dockerfile 有没有 COPY 全部模块、文件真的进容器没有、数据库连上没有、
users.role 列在不在、容器健康不健康、面板能不能打开。
文件指纹对不上时只警告不拦截（要硬性检查加 --strict）。

面板构建修正（2026-10-22）：只补编译 PDO MySQL，curl / mbstring 使用基础镜像内置版本。
面板先构建、成功后再替换容器；构建失败保留现有面板。首次仍需下载编译工具链。
中继已正常时只运行带 panel 参数的命令。详细说明：docs/V5-PANEL-BUILD.md。
默认构建源已改为清华，最终配置真实构建及容器登录页通过。
Linux amd64 可上传 images/ 与 docs/panel-image.* 等完整配套文件，使用 --prebuilt-panel。
此路径先校验镜像和配套面板源文件，再载入并验证启动，不进行现场编译。

指纹自查（纯只读，随时可跑）：
    cd /opt/mc-link && sha256sum -c BUILD.txt

发版门禁（7 项，发版前必跑）：
    python3 scripts/check_deploy.py            # 只检查，不写任何东西
    #
    # 当前凭据是**硬编码**的（SECURITY-NOTICE.md），所以这条会打印一块
    # 风险告知并照常放行。改回外部化后用 --strict-secrets 验收。
    python3 scripts/check_deploy.py --strict-secrets   # 明文按失败处理
    python3 scripts/check_deploy.py --stamp    # **打包那一刻**才用：
                                               # 把当前版本记进 LAST_VERSION，
                                               # 下一次修复没 +1 就会被拦住。
    python3 scripts/e2e_host_gone.py           # 起真中继跑一遍 host-gone

指纹覆盖 `client/`（v3.9.16 修）：以前它只覆盖 47 个文件、客户端源码
完全不在保护内 —— `sha256sum -c` 通过并不能证明客户端是新的。


跑不起来的时候
================================================================

    bash /opt/mc-link/diag-relay.sh     # 中继崩了 / 重启循环
    bash /opt/mc-link/diag-panel.sh     # 面板 500 / 登录失败

这两个脚本只读，不重启容器、不动数据，会把真实报错打出来。

如果 bash 报这种错：
    rebuild.sh: line 18: $'\\r': command not found
    syntax error near unexpected token `$'do\\r''
那是文件被存成了 Windows 换行（CRLF），和代码无关。一条命令修好：

    sed -i 's/\\r$//' /opt/mc-link/*.sh


SSH 找回根管理密码
================================================================

    docker exec -it mc-relay python reset_password.py

不需要参数。回车 = 自动生成 16 位随机密码（排除 0oO1iIlLq9g）；
输入只允许字母数字，含 @ 这类符号会要求重输；输入 q 取消。
注意 -it 不能省。
"""


def package_files(root=ROOT):
    """
    包内要交付的全部文件，相对路径、按字典序。

    只排除"本地产物/机密"这两类 —— **不**排除 `client/`：v3.9.15 的教训就是
    客户端源码整个不在指纹保护内。
    """
    found = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in EXCLUDED_DIRS
        )

        # `scripts/` 与 `tests/` **都要进包**：分析报告 §5.3 的教训就是
        # "服务器上跑不了任何回归"—— 交付包里没有测试，就没有东西能证明
        # 改好了。`scripts/rotate_secrets.py` 更是轮换凭据的唯一入口。
        for name in sorted(filenames):
            if name in EXCLUDED_FILES:
                continue

            rel = os.path.relpath(os.path.join(dirpath, name), root)

            found.append(rel.replace(os.sep, "/"))

    return sorted(found)


def sha256_of(path):
    digest = hashlib.sha256()

    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)

    return digest.hexdigest()


def build_lines(files, root=ROOT):
    return [
        f"{sha256_of(os.path.join(root, rel))}  {rel}"
        for rel in files
        if not _unfingerprinted(rel, os.path.basename(rel))
    ]


def listing_text(files):
    """
    按目录分组的清单（和服务器上"一个目录一个目录传"的动作对上）。
    """
    groups = {}

    for rel in files:
        top = rel.split("/", 1)[0] if "/" in rel else rel

        groups.setdefault(top, []).append(rel)

    out = []

    for top in sorted(groups):
        out.append(f"--- {top} ---")

        for rel in groups[top]:
            out.append(f"    {rel}")

        out.append("")

    return "\n".join(out).rstrip("\n")


def clean_tree(root=ROOT):
    """
    删掉源码树里的 `__pycache__` / `.pyc`（**打包前跑一次**）。

    v3.9.17 的包里带着 7 个 `__pycache__` 目录（本地跑测试留下的）。`.gitignore`
    覆盖了它们，但打包用的是 tar/zip，不看 .gitignore —— 于是照样进了包。
    """
    removed = 0
    clean_root = os.path.realpath(root)

    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames):
            if name == "__pycache__":
                target = os.path.join(dirpath, name)
                if os.path.commonpath([clean_root, os.path.realpath(target)]) != clean_root:
                    raise ValueError("cache cleanup escaped the delivery directory")

                with contextlib.suppress(Exception):
                    shutil.rmtree(target)
                    removed += 1

                dirnames.remove(name)

        for name in filenames:
            if name.endswith(".pyc"):
                with contextlib.suppress(Exception):
                    os.remove(os.path.join(dirpath, name))
                    removed += 1

    return removed


def main():
    parser = argparse.ArgumentParser(description="生成 BUILD.txt / README.txt")
    parser.add_argument(
        "--check", action="store_true",
        help="只校验现有 BUILD.txt 是否与当前文件一致（CI 用）",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="先清掉 __pycache__ / *.pyc 再生成清单（**打包前用**）",
    )
    args = parser.parse_args()

    if args.clean:
        removed = clean_tree()

        print(f"[OK] 已清理 {removed} 个 __pycache__ / .pyc")

    files = package_files()
    lines = build_lines(files)

    if args.check:
        path = os.path.join(ROOT, "BUILD.txt")

        if not os.path.isfile(path):
            print("[FAIL] 找不到 BUILD.txt —— 先跑一次 make_build.py")
            return 1

        with open(path, encoding="utf-8") as fh:
            current = [ln.strip() for ln in fh if ln.strip()]

        missing = [ln for ln in lines if ln not in current]
        extra = [
            ln for ln in current
            if ln not in lines
            and not _unfingerprinted(
                ln.split("  ", 1)[-1], os.path.basename(ln.split("  ", 1)[-1])
            )
        ]

        if missing or extra:
            print(f"[FAIL] BUILD.txt 与包内文件不一致（{len(missing)} 缺 / "
                  f"{len(extra)} 多）")

            for ln in missing[:20]:
                print(f"   缺：{ln}")

            for ln in extra[:20]:
                print(f"   多：{ln}")

            return 1

        print(f"[OK] BUILD.txt 覆盖全部 {len(lines)} 个文件，指纹一致")

        return 0

    with open(os.path.join(ROOT, "BUILD.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    with open(os.path.join(ROOT, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            README_TEMPLATE.format(
                count=len(files), listing=listing_text(files)
            )
        )

    print(f"[OK] BUILD.txt：{len(lines)} 条指纹")
    print(f"[OK] README.txt：{len(files)} 个文件（含 client/）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
