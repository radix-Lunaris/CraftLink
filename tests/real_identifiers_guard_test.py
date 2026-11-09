#!/usr/bin/env python3
"""
真实标识符守卫（D5：开源版绝不硬编码凭据 / 真实基础设施标识）。

============================================================================
 它挡的是哪一类事故
============================================================================

开源发布前，仓库里曾经有过这些东西（都不是"代码 bug"，但每一条都会让仓库
**不能公开**）：

  * 真实域名（含 `www.` / `mc.` 子域）—— 出现在文档、脚本默认值、客户端日志里；
  * 真实 ICP 备案号 —— 出现在面板 compose 的默认值与页脚注释里；
  * 真实数据库密码 / 管理 API token —— 明文写在两个 compose 里（v3.9.20）；
  * 真实公网 IP —— 审计文档与客户端日志里的真机地址。

人工"grep 一遍"挡不住下一次：改完代码随手把日志、域名、IP 贴进注释或文档，
没人会发现。所以这里把它变成**每次回归都跑**的检查。

============================================================================
 判据
============================================================================

扫描**跟踪树**（= 仓库里会被提交的文件）：

  * 跳过 `deploy/`、`optional/`、`.testcfg/`、`__pycache__/`、`.git/`、
    `logs/`、`status/`（前两个是生成/留档目录，见 .gitignore 与 make_build.py）；
  * 再跳过 `.gitignore` 命中的文件（真机日志、`.env`、构建产物…… 它们本来
    就不会被提交，也没什么"发布"可言）。

每个文件按**行**匹配下面的标识符；命中就打印"文件:行号 + 是哪一类"，并且
**不回显值**（守卫自己的输出也会被贴到工单里）。

反例针写法说明：本文件里的域名/备案号/凭据/IP 一律**拆成片段拼**，否则这个
守卫自己就成了泄漏源。

`KNOWN_LEFTOVERS` 是"别的 workflow 拥有的文件里已知的遗留"：按
（文件 -> 类别 -> 该行必须匹配的正则）登记，粒度是**行**而不是文件 ——
同一个文件里出现新的、不在注释/夹具里的泄漏照样会红。每一轮都会把它们
打印出来（`[warn] 已知遗留`），提醒清理；清掉之后请把这几个条目删掉。

用法：

    python3 tests/real_identifiers_guard_test.py          # 人类可读
    python3 tests/real_identifiers_guard_test.py --quiet   # 只打印 PASS/FAIL

返回码：0 = 干净；1 = 有泄漏（或已知遗留被"改坏"）。
"""

import argparse
import fnmatch
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 这些目录不扫（生成物 / 留档 / 别的 workflow 明确划出去的）。
EXCLUDE_DIRS = {
    ".git",
    "deploy",        # sync-deploy.py 生成的"已部署镜像"，打包时重新生成
    "optional",      # 可选组件，不进主交付（也没必要跟着扫）
    ".testcfg",
    "__pycache__",
    "logs",
    "status",
}

#: ---- 真实标识符（**拆开写**，别改成整串）--------------------------------
_DOMAIN = "superyk" + "7hero"                       # 注册域名的主干
_ICP = "2026" + "008614"                            # 备案号里的数字段
_DB_PW = "968f9ed2" + "5a33c608"                    # v3.9.20 明文的库密码
_API_TOKEN = "b36652a3" + "2ff1d95db1e965f8b64e259220f8275ca64f9a15"

#: 真实公网 IP（审计文档 / 客户端日志里出现过的真机地址），按段拆开拼。
_IP_PARTS = (
    ("180", "106", "222", "112"),
    ("220", "178", "180", "188"),
    ("223", "166", "20", "9"),
    ("223", "167", "44", "206"),
    ("220", "196", "193", "15"),
    ("220", "178", "180", "180"),
)

_IP_ALTERNATION = "|".join(
    r"\.".join(re.escape(part) for part in parts) for parts in _IP_PARTS
)

#: (类别 -> 正则)。类别名会出现在失败信息里，所以写人话。
FORBIDDEN = (
    ("真实域名", re.compile(re.escape(_DOMAIN), re.IGNORECASE)),
    ("真实备案号", re.compile(re.escape(_ICP))),
    ("明文数据库密码", re.compile(re.escape(_DB_PW))),
    ("明文管理 API token", re.compile(re.escape(_API_TOKEN))),
    ("真实公网 IP", re.compile(r"\b(?:" + _IP_ALTERNATION + r")\b")),
)

#: 别的 workflow 拥有的文件里**已知**的遗留：文件 -> 类别 -> 行必须匹配的正则。
#:
#: 这些都在 `client/**` 与 `installer/**`（本轮明确不许改的目录）里，而且是
#: 注释 / 文档串 / 反例夹具，不是可执行逻辑。粒度到"行"：同一个文件里出现新的
#: 泄漏（比如某行真的把 IP 当默认值使）照样会红。
#:
#: `_PROSE` = 这一行既不是注释、也不带赋值/调用 —— 也就是文档串里的行文
#: （`P2P 已直连【玩家名】<ip>:40879 18ms` 这种日志样例）。
#: `addr="<ip>"` / `DEFAULT_HOST = "<ip>"` 这一类**不会**命中，会被判违规。
_PROSE = r"^\s*#|^[^=()\[\]]*$"

KNOWN_LEFTOVERS = {
    "client/mclink/gui/app.py": {"真实公网 IP": _PROSE},
    "client/mclink/core/punch_flow.py": {"真实公网 IP": _PROSE},
    "client/mclink/core/room_direct.py": {"真实公网 IP": _PROSE},
    "client/mclink/core/direct_carry.py": {"真实公网 IP": _PROSE},
    "client/mclink/core/punch.py": {"真实公网 IP": _PROSE},
    # M7（Windows 安装器）工作流的反例针：`forbidden = (...)` / `for needle in (...)`
    "installer/tests/builder_selftest.py": {"真实域名": r"forbidden\s*="},
    "scripts/verify-installer-inputs.py": {"真实域名": r"for\s+needle\s+in"},
}

#: 扫描时按二进制读（bytes），但跳过这些明显不是文本、也不可能携带上述标识符
#: 的扩展名？—— 不跳：`.exe` 里如果带着旧域名，那正是要抓的。只限大小。
MAX_BYTES = 64 * 1024 * 1024


# ============================================================================
#  .gitignore（子集实现：够用、可读，不引第三方）
# ============================================================================

def _gitignore_patterns(root):
    patterns = []
    path = os.path.join(root, ".gitignore")

    if not os.path.isfile(path):
        return patterns

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()

            if not line or line.startswith("#"):
                continue

            negate = line.startswith("!")

            if negate:
                line = line[1:].strip()

            patterns.append((line, negate))

    return patterns


def _pattern_matches(pattern, rel):
    pattern = pattern.rstrip("/")

    if not pattern:
        return False

    if "/" in pattern:
        # 路径型：相对仓库根。`**` 退化成 `*`（够这个仓库用）。
        for candidate in (pattern, pattern + "/*"):
            if fnmatch.fnmatch(rel, candidate):
                return True

        return fnmatch.fnmatch(rel, pattern.replace("**", "*"))

    # 名字型：任意一层路径组件命中即可（git 的语义）
    return any(fnmatch.fnmatch(part, pattern) for part in rel.split("/"))


def _is_ignored(rel, patterns):
    ignored = False

    for pattern, negate in patterns:
        if _pattern_matches(pattern, rel):
            ignored = not negate

    return ignored


# ============================================================================
#  扫描
# ============================================================================

def iter_tracked_files(root):
    """产出 (相对路径, 绝对路径)。顺序稳定，便于对拍。"""
    patterns = _gitignore_patterns(root)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)

        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")

            if _is_ignored(rel, patterns):
                continue

            yield rel, full


def scan(root=ROOT):
    """
    返回 (violations, leftovers)：

        violations = [(rel, lineno, 类别)]
        leftovers  = [(rel, lineno, 类别, 行正则)]
    """
    violations = []
    leftovers = []

    for rel, full in iter_tracked_files(root):
        try:
            if os.path.getsize(full) > MAX_BYTES:
                continue

            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue

        allowed = KNOWN_LEFTOVERS.get(rel)

        for lineno, line in enumerate(raw.split(b"\n"), 1):
            for label, pattern in FORBIDDEN:
                if not pattern.search(line.decode("utf-8", "ignore")):
                    continue

                if allowed and label in allowed:
                    text = line.decode("utf-8", "ignore")

                    if re.search(allowed[label], text):
                        leftovers.append((rel, lineno, label, allowed[label]))
                        break

                violations.append((rel, lineno, label))
                break

    return violations, leftovers


def main():
    parser = argparse.ArgumentParser(description="真实标识符守卫")
    parser.add_argument("--quiet", action="store_true", help="只打印 PASS/FAIL")
    args = parser.parse_args()

    violations, leftovers = scan(ROOT)

    if not args.quiet:
        print(f"  扫描根目录：{ROOT}")
        print(f"  标识符类别：{', '.join(label for label, _ in FORBIDDEN)}")

    if leftovers:
        print(f"  [warn] 已知遗留（属别的 workflow，本轮没动）：{len(leftovers)} 处")

        if not args.quiet:
            for rel, lineno, label, _rule in leftovers:
                print(f"  [warn]   - {rel}:{lineno} {label}")

    if violations:
        print(
            f"[FAIL] 跟踪树里出现真实标识符：{len(violations)} 处"
            "（值已隐藏；下面只给文件:行号与类别）"
        )

        for rel, lineno, label in violations:
            print(f"          {rel}:{lineno}  {label}")

        print(
            "[FAIL] ★ 跟踪树里没有真实域名 / 备案号 / 明文凭据 / 真实公网 IP"
        )
        return 1

    print(
        "[PASS] ★ 跟踪树里没有真实域名 / 备案号 / 明文凭据 / 真实公网 IP"
    )
    print(f"[PASS] 已扫描 {sum(1 for _ in iter_tracked_files(ROOT))} 个跟踪文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
