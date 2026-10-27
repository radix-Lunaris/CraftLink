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
    ".git", "dist", "build", ".private", "private", "mclan-private", ".ui-build-deps", ".githooks", ".github",
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
    ".env", "credentials.dat", ".device_id", "settings.json",
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

README_TEMPLATE = """CraftLink v5.1.4 候选部署包（未完成 VPS 验收）
================================================================
源码使用 Git 管理；本包由 scripts/make_build.py 生成。
当前版本加粗普通界面文字，连接状态页保持原样；私有配置及网络核心保持原逻辑。
公共客户端默认使用示例地址；老用户保留原设置，自用地址文件只在私有分发目录。

已有部署必须先备份并保值迁移，不能先覆盖旧 Compose：
  1. 单独上传 scripts/deploy_config.py 和 scripts/migrate_config.py 到临时目录。
  2. python3 /临时目录/migrate_config.py --from-dir /opt/mc-link --output /opt/mc-link/.env
  3. 上传新版，保留 .env、panel/config.php、panel/config.local.php。
  4. python3 /opt/mc-link/scripts/deploy_config.py
  5. bash /opt/mc-link/rebuild.sh all --prebuilt-panel

迁移不改密码、不执行 SQL；冲突则停止。预检失败不停止旧容器。
支持 MCLINK_HOME 和 MCLINK_ENV_FILE。两端读取同一个配置文件。
私有配置不在本包中，严禁将其补进公共压缩包。
客户端运行 CraftLink.exe；控制台排查使用 CraftLink-console.exe。
源码运行先 pip install -r requirements.txt，再 python client/main.py。
中继和两端客户端同步更新为 5.1.4；面板源码不变，复用原 5.1.1 面板镜像。
本版未部署 VPS；脱敏源码按授权发布至 dev，master 空占位，legacy 保留旧历史。
用户验收后另行授权发布源码和正式版本标签。

共 {count} 个文件：
{listing}
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
            rel_candidate = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            private = (name.startswith(".env") and name != ".env.example" or name.endswith((".env", ".bak", ".pem", ".key", ".pfx", ".p12", ".migration-tmp")) or name == "config.local.php" or name == "config.php")
            private = private or (name.startswith("mclink_config") and name.endswith(".json")) or name.startswith("credentials.dat")
            private = private or name.endswith((".local.json", ".bundle", ".gitbundle", ".crt")) or name.startswith(("publication-denylist", "id_rsa", "id_ed25519")) or ".private." in name
            if private or name in EXCLUDED_FILES:
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

    with open(os.path.join(ROOT, "BUILD.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    with open(os.path.join(ROOT, "README.txt"), "w", encoding="utf-8", newline="\n") as fh:
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
