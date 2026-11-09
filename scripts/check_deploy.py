#!/usr/bin/env python3
"""
发版门禁（一）：交付包自检。

============================================================================
 它挡的是哪一类事故
============================================================================

本项目最大的工程债是"没有 git、没有测试"（HANDOVER.md:6）。改没改、改了什么
全靠读文件 —— 于是同一类 bug（host 断开后其他人不断开）回归了三次
（09-30 定稿 → 10-02 → 10-03 重申"实测仍未生效"）。

这个脚本把"发版前必须为真"的那几条**变成可执行的检查**，人不用再靠记性：

  1. 版本号一致（客户端 `APP_VERSION` == `server/VERSION`）；
  2. 包内**没有**真实凭据（v3.9.15 的 P0）；
  3. `BUILD.txt` 覆盖全部文件（含 `client/`，v3.9.15 的 P1）；
  4. Dockerfile COPY 了全部服务端模块（漏一个 = 容器里跑的是旧代码）；
  5. 服务端/客户端源码能编译（语法层面）；
  6. 回归测试全过（`tests/run_all.py`）。

任一不过 -> 退出码 1。**发版前必须跑它。**

用法：

    python3 scripts/check_deploy.py              # 全查
    python3 scripts/check_deploy.py --no-tests   # 跳过回归测试（只想快速自查）
"""

import argparse
import contextlib
import os
import py_compile
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

#: 凭据长什么样：这两个键在 v3.9.15 里是明文写死的。
#:
#: 判定"明文凭据"要看**值**，不能只搜键名 —— compose 里现在仍然有这两个键，
#: 但值是 `${MCLINK_DB_PASSWORD:?...}` 这种引用，那是**正确**的写法。
SECRET_KEYS = ("MCLINK_DB_PASSWORD", "MCLINK_ADMIN_API_TOKEN")

# ============================================================================
#  ★ v3.9.17：判据是"**字面值**"，不是"长得像 hex"
# ============================================================================
#
# v3.9.16 的判据是 `\b[0-9a-fA-F]{16,}\b`（16 位以上纯十六进制）。它漏报：
#
#     MCLINK_DB_PASSWORD: "Xy7kQm2nRp4wLb9d"   -> [ OK ]   ← 漏报
#
# 讽刺的是 `rotate_secrets.py` 生成的**正是**非 hex 串（字母+数字、去掉了
# 0oO1iIlLq9g 这类易混字符）—— 真被人贴回 compose 里，门禁抓不到（复核报告 P2）。
#
# 所以主判据改成形态无关的"键后面跟了一个不是 `${` 的字面值"：
# 只要是字面值就报，不看它长什么样。这也让"占位符"（`__CHANGE_ME__`）一样
# 逃不掉 —— 那就是它该被拦下的地方。
#
# 唯一的例外是**空值**（首次部署前允许留空）与明确的占位符标记。
LITERAL_SECRET_RE = re.compile(
    r"""(?ix)
        (?<!\$)(?<!\{)          # 不是 `${KEY ...}` 这种引用的一部分
        \b(?:MCLINK_DB_PASSWORD|MCLINK_ADMIN_API_TOKEN)\b
        \s*[:=]\s*
        (?:
            "(?P<dq>[^"]*)"
          | '(?P<sq>[^']*)'
          #  ★ v3.9.19：裸值遇到引号/反斜杠就停。
          #
          #  源码里写 `fh.write("...=__CHANGE_ME__\n")` 时，真正的"值"后面
          #  还挂着 `\n"` 这些噪声。不截断的话，一个**本来就是占位符**的值
          #  会因为多了尾缀而变得不在白名单里，被误报成明文凭据。
          | (?P<bare>[^\s#"'\\]+)
        )
    """
)

#: 扫描前先把**环境变量引用**整段抹掉：`${MCLINK_DB_PASSWORD:?...}`、
#: `$MCLINK_DB_PASSWORD`、`\${...}`（shell 里转义过的）。
#:
#: 不抹的话，文档里那句"compose 里应该写成 `${MCLINK_DB_PASSWORD:?...}`"
#: 会被判成明文凭据 —— 它恰恰是**正确写法**的说明（v3.9.17 修掉的误报）。
ENV_REF_RE = re.compile(r"\\?\$\{?[A-Za-z_][A-Za-z0-9_]*[^{}]*\}?")

#: 允许出现的**字面值**：明确的占位符/空值。除此之外一律视为明文凭据。
ALLOWED_LITERALS = {
    "",
    "__CHANGE_ME__",
    "CHANGE_ME",
    "CHANGE_ME_db_password",
    "changeme",
    "todo",
}

#: 允许出现例子的地方（只有**文档性质**的文件可以豁免，脚本自身不再豁免：
#: v3.9.16 因为它俩被豁免，里面的样例值反而扫不到 —— 复核报告 P2）。
EXEMPT_FILES = {
    ".env.example",
}

#: 上一次**发出去**的版本号。由 `stamp_version()` 在全绿时写，门禁据此校验
#: "版本号 +1 了吗"。
#:
#: **故意不进交付包**（见 make_build.py 的 EXCLUDED_FILES）：包内带上它，
#: 用户一跑就会被自己的门禁判"版本号没变大"。代价是拿到 zip 的人这一条校验
#: 会失效 —— v3.9.18 起会明确提示，不再静默跳过。
LAST_VERSION_FILE = "LAST_VERSION"

#: 上一次检查里"没有发版记录"的提示（由 check_version 设置，main 打印）。
LAST_VERSION_WARNING = ""

SKIP_SCAN_SUFFIX = (".ico", ".png", ".css", ".zip")


def failures():
    return []


def check_version():
    """客户端 `APP_VERSION` 必须等于 `server/VERSION`。"""
    problems = []

    settings_path = os.path.join(
        ROOT, "client", "mclink", "config", "settings.py"
    )
    version_path = os.path.join(ROOT, "server", "VERSION")

    client_version = ""

    if os.path.isfile(settings_path):
        with open(settings_path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("APP_VERSION"):
                    client_version = (
                        line.split("=", 1)[1].strip().strip('"').strip("'")
                    )
                    break

    server_version = ""

    if os.path.isfile(version_path):
        with open(version_path, encoding="utf-8") as fh:
            server_version = fh.read().strip()

    if not client_version:
        problems.append("读不到客户端 APP_VERSION（settings.py）")
    elif not server_version:
        problems.append("读不到 server/VERSION")
    elif client_version != server_version:
        problems.append(
            f"版本号不一致：客户端 {client_version} vs 服务端 {server_version}"
        )

    # ============================================================================
    #  ★ v3.9.17：版本号必须比**上一次发版**大
    # ============================================================================
    #
    # 修复说明写着"每次修复都会 +1，check_deploy 强制校验"，但 v3.9.16 只比
    # 两处相等 —— 发版时不改版本号也能全绿，"改了就升版本"这条纪律仍然只靠
    # 人（复核报告 P2）。
    #
    # 比对基准是 `LAST_VERSION`（包内文件，随版本一起走）。它是**发版那一刻**
    # 写下的当前版本号，所以下一次发版时它记的就是"上一版"。
    if client_version and server_version and client_version == server_version:
        prev = ""

        last_path = os.path.join(ROOT, LAST_VERSION_FILE)

        if os.path.isfile(last_path):
            with open(last_path, encoding="utf-8") as fh:
                prev = fh.read().strip()

        if prev and not _version_newer(client_version, prev):
            problems.append(
                f"版本号没有变大：当前 {client_version}，上一版 {prev}。"
                f"修复一次就要 +1（改 client/mclink/config/settings.py 的 "
                f"APP_VERSION 与 server/VERSION）"
            )
        elif not prev:
            # ============================================================================
            #  ★ v3.9.18：没有发版记录时必须**说出来**，不能静默跳过
            # ============================================================================
            #
            # 复核报告 3.3：`LAST_VERSION` 故意不进交付包（包内带上它，用户一跑
            # 就会被自己的门禁判"版本号没变大"）。于是拿到 zip 的人跑这个脚本：
            # `prev` 为空 -> 静默跳过 -> 7/7 全绿，但"版本号 +1"这条保护
            # **根本没执行**，而且看不出来。
            #
            # 所以这里记一条 warning 并返回给调用方打印（不是 problems ——
            # 交付包里缺这个文件是**设计如此**，不该判失败）。
            global LAST_VERSION_WARNING
            LAST_VERSION_WARNING = (
                f"未找到发版记录（{LAST_VERSION_FILE}），无法校验版本号是否递增。"
                f"当前 {client_version}；在仓库里发版时加 --stamp 才会写它。"
            )

    return client_version, server_version, problems


def _version_tuple(value):
    """`'3.9.17'` -> `(3, 9, 17)`；带非数字段就退回字符串比较能兜住的形式。"""
    parts = []

    for chunk in str(value or "").strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())

        parts.append(int(digits) if digits else 0)

    return tuple(parts) if parts else (0,)


def _version_newer(candidate, previous):
    """`candidate` 是否严格大于 `previous`（逐段比，长度不齐补 0）。"""
    a = _version_tuple(candidate)
    b = _version_tuple(previous)

    width = max(len(a), len(b))

    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))

    return a > b


def stamp_version(version):
    """
    发版时把当前版本号写进 `LAST_VERSION`（门禁下一次据此比"变大了没"）。

    只在**全部检查通过**之后调（见 `main`），所以它记的是"这份包真的发出去了"。
    """
    with open(os.path.join(ROOT, LAST_VERSION_FILE), "w", encoding="utf-8") as fh:
        fh.write(str(version or "").strip() + "\n")


def _literal_is_secret(value):
    """
    这个字面值算不算"明文凭据"。

    判据是**形态无关**的：只要不是环境变量引用（`${...}`）、也不是明确的
    占位符/空值，就算。见模块顶部那段（v3.9.16 用 hex 判据会漏报）。
    """
    value = (value or "").strip()

    if not value:
        return False

    # `${VAR}` / `${VAR:?...}` / `$VAR` —— 正确的写法，放行。
    if value.startswith("${") or value.startswith("$"):
        return False

    if value.lower() in {v.lower() for v in ALLOWED_LITERALS}:
        return False

    # ★ v3.9.19：模板插值（`{var}` / `{values['x']}`）不是字面值。
    #
    # 测试里要构造"某个键等于某个变量"这种字符串，那是 f-string 的占位符，
    # 不是被硬编码进包的凭据。
    if value.startswith("{") or value.startswith("$("):
        return False

    return True


#: ============================================================================
#:  ★ v3.9.20：凭据策略 —— **当前是硬编码（风险已记录）**
#: ============================================================================
#:
#:  用户 2026-10-05 的决定：为了能部署，把凭据**写回** compose 明文，
#:  "这一风险仅记录，此后再修改"。
#:
#:  所以这条检查从"判失败"改成"记录 + 提醒"：
#:    * 扫到明文凭据 -> 醒目的 [warn]，列出位置与风险，**不拦部署**；
#:    * 用 `--strict-secrets` 可以临时打开旧的严格模式（改回外部化时用它）。
#:
#:  检测能力**完整保留** —— 以后要改回来，把 compose 换成 `${...}` 引用，
#:  这条就会重新变成 [ OK ]。现在它每天提醒一次，改回来那天零成本。
SECRETS_POLICY = "hardcoded"          # hardcoded | external
SECRET_NOTICES = []                   # 记录式提醒由这里带出去


def check_no_secrets(strict=True):
    # Public package allowlist, independent from Git or local private runtime files.
    from check_secrets import findings
    from make_build import package_files
    problems=[]
    for name in package_files():
        path=os.path.join(ROOT,name)
        with open(path,"rb") as stream:
            content=stream.read()
        for rule in findings(name,content):
            problems.append(name+": "+rule+"（值已隐藏）")
    return problems


def check_no_pycache():
    """
    包内不许有 `__pycache__` / `*.pyc`（v3.9.18，复核报告 3.4）。

    它们是本地跑测试留下的产物，随包发出既脏又可能让人误以为"这是要部署的"。
    打包前跑 `python3 scripts/make_build.py --clean` 即可清掉。
    """
    problems = []

    for dirpath, dirnames, filenames in os.walk(ROOT):
        if "__pycache__" in dirnames:
            rel = os.path.relpath(
                os.path.join(dirpath, "__pycache__"), ROOT
            ).replace(os.sep, "/")

            problems.append(f"{rel}/（跑 make_build.py --clean 可清掉）")

        for name in filenames:
            if name.endswith(".pyc"):
                rel = os.path.relpath(
                    os.path.join(dirpath, name), ROOT
                ).replace(os.sep, "/")

                problems.append(f"{rel}")

    return problems


def check_build_txt():
    """`BUILD.txt` 必须覆盖全部文件（含 client/）。"""
    script = os.path.join(ROOT, "scripts", "make_build.py")

    if not os.path.isfile(script):
        return ["找不到 scripts/make_build.py"]

    result = subprocess.run(
        [sys.executable, script, "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )

    if result.returncode != 0:
        return [ln for ln in result.stdout.splitlines() if ln.strip()]

    return []


def check_dockerfile():
    """服务端 Dockerfile 必须 COPY 了全部 .py 模块。"""
    problems = []

    dockerfile = os.path.join(ROOT, "server", "Dockerfile")

    if not os.path.isfile(dockerfile):
        return ["找不到 server/Dockerfile"]

    with open(dockerfile, encoding="utf-8") as fh:
        text = fh.read()

    for name in sorted(
        n for n in os.listdir(os.path.join(ROOT, "server"))
        if n.endswith(".py")
    ):
        if name not in text:
            problems.append(f"server/Dockerfile 没有 COPY {name}")

    return problems


def check_compiles():
    """源码必须能编译（语法层面的最低门槛）。"""
    problems = []

    targets = []

    for base in ("server", "client", "scripts"):
        path = os.path.join(ROOT, base)

        if not os.path.isdir(path):
            continue

        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]

            for name in filenames:
                if name.endswith(".py"):
                    targets.append(os.path.join(dirpath, name))

    # 编译到一个临时目录：写 /dev/null 会被 py_compile 当成"要把它变成普通
    # 文件"而报 FileExistsError，而且会污染源码树（散落 __pycache__）。
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        for path in targets:
            try:
                py_compile.compile(
                    path,
                    cfile=os.path.join(tmpdir, "out.pyc"),
                    doraise=True,
                )
            except Exception as e:
                problems.append(
                    f"{os.path.relpath(path, ROOT)}：{e.__class__.__name__}: {e}"
                )

    return problems


def check_e2e():
    """
    端到端复现：起一个真的本地中继，跑一遍 host-gone 那条链路。

    为什么门禁要跑它：`tests/run_all.py` 验的是单元逻辑，而这个脚本验的是
    **整条链路**（真中继 + 真客户端核心层）。v3.9.16 的 P1 就是单元逻辑看不
    出来、端到端一跑就露馅的那种（房主停过一次之后宽限窗口永久失效）。

    起子进程要几秒，所以超时给得宽一点；失败也不算致命 —— 它依赖本机端口
    和 `python3`，CI 上跑不起来时不该拖住整个门禁。
    """
    script = os.path.join(ROOT, "scripts", "e2e_host_gone.py")

    if not os.path.isfile(script):
        return ["找不到 scripts/e2e_host_gone.py"]

    env = dict(os.environ)
    env["E2E_PORT"] = str(int(os.environ.get("E2E_PORT", "19933")) + 7)

    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, cwd=ROOT, env=env, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return ["端到端复现超时（120s）—— 本机端口/环境可能不允许起中继"]

    if result.returncode != 0:
        lines = [
            ln for ln in (result.stdout + result.stderr).splitlines()
            if ln.strip()
        ]

        return ["端到端复现未通过："] + [f"    {ln}" for ln in lines[-15:]]

    return []


def check_tests():
    """回归测试必须全过。"""
    # ★ 2026-10-04（用户口径）：测试留在**工作区**，不塞进服务器包。
    candidates = [
        os.path.join(ROOT, "tests", "run_all.py"),
        os.path.join(os.path.dirname(ROOT), "tests", "run_all.py"),
        os.path.join(os.path.dirname(ROOT), "tests", "run_all_full.py"),
    ]

    runner = next((c for c in candidates if os.path.isfile(c)), "")

    if not runner:
        return ["找不到回归测试（包内 tests/ 或工作区 tests/run_all*.py）"]

    workdir = os.path.dirname(os.path.dirname(os.path.abspath(runner)))

    result = subprocess.run(
        [sys.executable, runner],
        capture_output=True, text=True, cwd=workdir,
    )
    log_dir = os.path.join(ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "test-suite.log"), "w", encoding="utf-8") as record:
        record.write(result.stdout + result.stderr)

    if result.returncode != 0:
        lines = [
            ln for ln in (result.stdout + result.stderr).splitlines()
            if ln.strip()
        ]

        return ["回归测试未通过："] + [f"    {ln}" for ln in lines[-15:]]

    return []


def main():
    parser = argparse.ArgumentParser(description="交付包发版门禁")
    parser.add_argument("--no-tests", action="store_true", help="跳过回归测试")
    parser.add_argument(
        "--strict-secrets", action="store_true",
        help="明文凭据按**失败**处理（v3.9.16~19 的行为）。"
             "当前默认是记录式提醒，改回外部化时用它验收",
    )
    parser.add_argument(
        "--stamp", action="store_true",
        help="全绿时把当前版本号写进 LAST_VERSION（**打包那一刻才用**）",
    )
    args = parser.parse_args()

    client_version, server_version, version_problems = check_version()

    print("=" * 70)
    print(" 交付包自检" + (f"（v{server_version}）" if server_version else ""))
    print("=" * 70)

    # 注意顺序：`包内无 __pycache__` 必须**先**跑 —— 它下面那些检查（尤其
    # 回归测试）自己会 import 模块、从而在源码树里生成 __pycache__，放到后面
    # 就等于"永远失败"。
    groups = [
        ("版本号一致", version_problems),
        ("包内无明文凭据", check_no_secrets(strict=args.strict_secrets)),
        ("包内无 __pycache__", check_no_pycache()),
        ("BUILD.txt 覆盖全部文件", check_build_txt()),
        ("Dockerfile 覆盖全部模块", check_dockerfile()),
        ("源码可编译", check_compiles()),
    ]

    ok = True

    for title, problems in groups:
        if problems:
            ok = False
            print(f"  [FAIL] {title}")

            for item in problems[:15]:
                print(f"         {item}")
        else:
            print(f"  [ OK ] {title}")

    if not args.no_tests:
        test_problems = check_tests()

        if test_problems:
            ok = False
            print("  [FAIL] 回归测试")

            for item in test_problems:
                print(f"         {item}")
        else:
            print("  [ OK ] 回归测试")
    if not args.no_tests:
        e2e_problems = check_e2e()

        if e2e_problems:
            ok = False
            print("  [FAIL] 端到端复现")

            for item in e2e_problems:
                print(f"         {item}")
        else:
            print("  [ OK ] 端到端复现（真中继 + 真核心层）")
    else:
        print("  [ -- ] 回归测试（--no-tests，已跳过）")

    # ============================================================================
    #  ★ v3.9.18：跑完之后**自己**把 __pycache__ 清掉
    # ============================================================================
    # 回归测试 / e2e 会 import 模块，从而在源码树里留下 __pycache__。不清的话
    # 下一次跑这条检查必然失败 —— 而那个失败是**本脚本自己**造成的，看着像
    # 代码坏了。所以跑完顺手清掉（失败也清，否则下次一进门就红）。
    with contextlib.suppress(Exception):
        import make_build

        make_build.clean_tree(ROOT)

    if LAST_VERSION_WARNING:
        print(f"  [warn] {LAST_VERSION_WARNING}")

    if SECRET_NOTICES:
        # ------------------------------------------------------------------
        #  ★ v3.9.20：明文凭据 —— **记录**，不拦
        # ------------------------------------------------------------------
        print("  [warn] ┌─ 凭据风险（已记录，**不拦部署**）" + "─" * 20)
        print("  [warn] │ 包里带的是**明文**凭据。这个 zip 只要外传一次")
        print("  [warn] │ （例如拿去给别人排查），数据库与管理 API 就一起交出去了。")
        print("  [warn] │")
        print("  [warn] │ 这是 2026-10-05 为了能部署做的取舍，详情见")
        print("  [warn] │ SECURITY-NOTICE.md。要改回来时：")
        print("  [warn] │   1) 把下面几处换成 ${...} 引用；")
        print("  [warn] │   2) python3 scripts/rotate_secrets.py 换掉值；")
        print("  [warn] │   3) python3 scripts/check_deploy.py --strict-secrets 验收。")
        print("  [warn] │")
        print("  [warn] │ 检出位置：")

        for item in SECRET_NOTICES:
            print(f"  [warn] │   - {item}")

        print("  [warn] └" + "─" * 48)

    print("=" * 70)

    if ok:
        # 盖章要**显式**要求（`--stamp`）：日常自查会跑很多次，每次都写
        # LAST_VERSION 的话，"上一版"就被污染成当前版，下一次真发版反而被
        # 自己的门禁拦住。只有打包那一刻才 `--stamp`。
        if args.stamp and client_version:
            try:
                stamp_version(client_version)
                print(f"  [i] 已记录发版版本 LAST_VERSION={client_version}")
            except OSError as e:
                print(f"  [warn] 写 LAST_VERSION 失败：{e}")

        print(" 本地检查通过；候选仍需用户 VPS 验收，勿推送或标记正式发行。")

        return 0

    print(" 有检查未通过 —— 修好再发版。")

    return 1


if __name__ == "__main__":
    sys.exit(main())
