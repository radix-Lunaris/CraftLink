"""
CraftLink —— 交付脚本的静态 + 行为检查（`mcr` 实时日志 / 交接文档进包）

用户 2026-09-29 的原话：

    「未来的 Rebuild 无需加那个 -v，可以直接打印详细的日志，每一步的日志状态，
      且是实时打印；尤其是重建那一块，它重建完了一次冒出来一坨，它这个一条
      一条地给我实时打印」

这一套钉住 `deploy/rebuild.sh` 的 `run_step` **行为**（不是措辞）：

  1. **流式**：默认（无参数）就是边跑边逐行输出 —— 用真子进程 + 到达时刻
     验证"第二行还没产生时，第一行已经打出来了"（攒到最后倒出来会失败）；
  2. **完整落盘**：同一份内容逐字进 `$REBUILD_LOG`；
  3. **失败上下文**：失败时打 `✘` + 最后 25 行，并以非 0 返回；
  4. **`-v` = 更啰嗦**：被默认过滤掉的空行在 `-v` 下会打出来；
  5. **`--strict` 语义不变**（STRICT=1）。

以及 `scripts/sync-deploy.py`：

  6. 交付包里必须有 `deploy/HANDOVER.md`（= `docs/HANDOVER.md` 的生成物），
     而且它进了 `BUILD.txt` 指纹与 `README.txt` 清单 —— 用户整包上传之后
     服务器上就有一份最新的交接文档；
  7. `check_deploy.py` 里"构建步骤必须实时逐行"的那几条断言存在（防回退）。

Run:
    python tests/deploy_scripts_test.py
"""

import io
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REBUILD = os.path.join(ROOT, "deploy", "rebuild.sh")
SYNC = os.path.join(ROOT, "scripts", "sync-deploy.py")
CHECK = os.path.join(ROOT, "scripts", "check_deploy.py")

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def read(path):
    return io.open(path, encoding="utf-8", errors="replace").read()


def extract_function(text, name):
    """从 shell 脚本里抠出一个 `name () { ... }` 函数（顶层，缩进 0）。"""
    lines = text.splitlines()
    start = None

    for index, line in enumerate(lines):
        if re.match(r"^" + re.escape(name) + r"\s*\(\)\s*\{", line):
            start = index
            break

    if start is None:
        return ""

    collected = []

    for line in lines[start:]:
        collected.append(line)

        if line == "}":
            break

    return "\n".join(collected)


def run_harness(steps, verbose="0"):
    """
    在一个干净 bash 里跑真实的 say/run_step 两个函数。

    返回 `(out_lines, returncode)`；`out_lines` 是 [(到达时刻, 行), ...]，
    用真子进程的行缓冲读取，所以"实时性"是可测的。
    """
    text = read(REBUILD)
    say_block = extract_function(text, "say")
    step_block = extract_function(text, "run_step")

    if not say_block or not step_block:
        return [], 127

    log_path = tempfile.mktemp(prefix="mclink-runstep-", suffix=".log")

    script = "\n".join(
        [
            "#!/bin/bash",
            "set -u",
            f'REBUILD_LOG="{log_path}"',
            f'VERBOSE="{verbose}"',
            say_block,
            step_block,
        ]
        + steps
        + [f'echo "LOG={log_path}"']
    )

    handle = subprocess.Popen(
        ["bash", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    arrivals = []

    if handle.stdout is not None:
        for line in handle.stdout:
            arrivals.append((time.monotonic(), line.rstrip("\n")))

    handle.wait(timeout=30)

    return arrivals, handle.returncode


def lines_of(arrivals):
    return [line for _stamp, line in arrivals]


def test_run_step_streams_live():
    print("")
    print("---- 1. run_step：默认就是**实时逐行**（不是攒完再倒）----")

    arrivals, code = run_harness(
        [
            'run_step "流式步骤" bash -c '
            '\'printf "line-1\\n"; sleep 0.7; printf "line-2\\n"\'',
        ]
    )

    text = "\n".join(lines_of(arrivals))

    check("步骤以 ▶ 开始、以 ✔ 结束（步骤名 + 成功）",
          "▶ 流式步骤" in text and "✔ 流式步骤" in text, text[:200])

    first = [stamp for stamp, line in arrivals if "line-1" in line]
    second = [stamp for stamp, line in arrivals if "line-2" in line]

    check("两行输出都打出来了", bool(first) and bool(second),
          f"first={first} second={second}")

    if first and second and len(arrivals) >= 1:
        end_stamp = arrivals[-1][0]

        check("★ 第一行在**第二行产生之前**就已经在屏幕上（真·实时，"
              "攒到最后倒出来会红）",
              first[0] < second[0] - 0.3,
              f"第一行 {first[0]:.2f} / 第二行 {second[0]:.2f}")

        check("★ 第一行远早于进程结束（不是收完一次性打印）",
              first[0] < end_stamp - 0.3,
              f"first={first[0]:.2f} end={end_stamp:.2f}")

    # 完整落盘
    log_paths = [
        line.split("=", 1)[1]
        for line in lines_of(arrivals)
        if line.startswith("LOG=")
    ]

    if log_paths:
        log_text = read(log_paths[0])

        check("★ 同一份内容逐字进 $REBUILD_LOG（▶ / 两行输出 / ✔ 都在）",
              "▶ 流式步骤" in log_text and "line-1" in log_text
              and "line-2" in log_text and "✔ 流式步骤" in log_text,
              log_text[:200])

        with_suppress = log_text

        check("★ 日志按步骤分节（===== 步骤名 =====）",
              "===== 流式步骤 =====" in with_suppress,
              with_suppress[:200])

        os.unlink(log_paths[0])
    else:
        check("★ 拿到 $REBUILD_LOG 路径", False, str(lines_of(arrivals)))

    check("成功的步骤返回 0", code == 0, str(code))


def test_run_step_failure_context():
    print("")
    print("---- 2. run_step：失败要打上下文并以非 0 返回 ----")

    arrivals, code = run_harness(
        [
            'run_step "会失败的步骤" bash -c '
            '\'printf "boom-1\\n"; printf "boom-2\\n"; exit 3\' || echo "rc=$?"',
        ]
    )

    text = "\n".join(lines_of(arrivals))

    check("失败步骤打 ✘ + 退出码", "✘ 会失败的步骤" in text and "exit=3" in text,
          text[:300])
    check("失败上下文单独打了一段（最后 25 行）",
          "失败上下文" in text and "boom-1" in text and "boom-2" in text,
          text[:300])
    check("return 非 0（调用方 `|| true` 之外能看出来）",
          "rc=3" in text, text[-200:])


def test_run_step_verbose_prints_filtered():
    print("")
    print("---- 3. -v：把默认过滤掉的空行也打出来（更啰嗦）----")

    command = (
        "run_step \"带空行\" bash -c "
        "'printf \"a\\n\\nprintf-b\\n\"'"
    )

    plain, _code = run_harness([command])
    verbose, _code2 = run_harness([command], verbose="1")

    plain_pipes = [line for line in lines_of(plain) if "│" in line]
    verbose_pipes = [line for line in lines_of(verbose) if "│" in line]

    check("默认：两行内容都打出来", len(plain_pipes) == 2,
          str(lines_of(plain)))
    check("★ -v：连空行也打出来（3 行 > 默认 2 行）",
          len(verbose_pipes) == 3, str(lines_of(verbose)))

    text = read(REBUILD)

    check("rebuild.sh 里有 -v/--verbose 开关", "--verbose" in text
          and "VERBOSE=1" in text)

    check("★ 默认模式**不再**要求加 -v 才看得到过程"
          "（旧提示语已删除）",
          "要看每一步的完整输出，加 -v" not in text)

    check("--strict 语义不变（仍然是 STRICT=1 + 拒绝重建）",
          "--strict) STRICT=1" in text
          and "去掉 --strict 可以忽略这些差异继续重建" in text)


def test_rebuild_source_shape():
    print("")
    print("---- 4. run_step 的实现形状（流式 + 落盘）----")

    text = read(REBUILD)

    check("★ 边跑边逐行：`while IFS= read -r line` 在读命令的输出",
          "while IFS= read -r line" in text)
    check("★ 用 PIPESTATUS 取真实退出码", "PIPESTATUS" in text)
    check("★ 不再 `out=$(\"$@\" 2>&1)` 攒批", 'out=$("$@" 2>&1)' not in text)
    check("★ 每一步用 tee 既上屏又落盘", 'tee "$out_file"' in text)
    check("★ 完整输出追加进 $REBUILD_LOG", '>> "$REBUILD_LOG"' in text)
    check("两个构建步骤都走 run_step（面板 + 中继）",
          text.count('run_step "重建并启动') == 2)


def test_handover_is_generated_into_deploy():
    print("")
    print("---- 5. sync-deploy：deploy/HANDOVER.md 必须生成并进清单 ----")

    source = os.path.join(ROOT, "docs", "HANDOVER.md")
    staged = os.path.join(ROOT, "deploy", "HANDOVER.md")

    check("docs/HANDOVER.md 存在（交接文档的唯一来源）",
          os.path.isfile(source), source)
    check("★ deploy/HANDOVER.md 已生成", os.path.isfile(staged), staged)

    if os.path.isfile(source) and os.path.isfile(staged):
        check("★ deploy/HANDOVER.md 与 docs/HANDOVER.md 逐字节一致",
              read(source) == read(staged), "内容不同 -> 跑 sync-deploy")
        check("★ 生成物比来源新（sync-deploy 真的跑过）",
              os.path.getmtime(staged) >= os.path.getmtime(source) - 1,
              f"src={os.path.getmtime(source)} staged="
              f"{os.path.getmtime(staged)}")

    sync_text = read(SYNC)

    check("★ sync-deploy 里有 HANDOVER 的复制步骤",
          "docs/HANDOVER.md" in sync_text
          and 'os.path.join(DEPLOY, "HANDOVER.md")' in sync_text,
          "sync-deploy 必须把 docs/HANDOVER.md 复制成 deploy/HANDOVER.md")

    check("★ sync-deploy 把它加进 copied（否则 README / BUILD.txt 会漏）",
          'copied.append("HANDOVER.md")' in sync_text)

    stamp = os.path.join(ROOT, "deploy", "BUILD.txt")

    if os.path.isfile(stamp):
        entries = [
            line.split("  ", 1)[1].strip()
            for line in read(stamp).splitlines()
            if "  " in line
        ]
        check("★ BUILD.txt 指纹里包含 HANDOVER.md",
              "HANDOVER.md" in entries,
              f"共 {len(entries)} 条")
    else:
        check("deploy/BUILD.txt 存在", False, stamp)

    readme = os.path.join(ROOT, "deploy", "README.txt")

    if os.path.isfile(readme):
        listed = {
            line.strip()
            for line in read(readme).splitlines()
            if line.strip() == "HANDOVER.md"
        }
        check("★ README.txt 上传统清单里有 HANDOVER.md",
              bool(listed), "清单里没有它，用户上传时就不会带")
    else:
        check("deploy/README.txt 存在", False, readme)

    check_check_deploy_mentions_streaming()


def check_check_deploy_mentions_streaming():
    text = read(CHECK)

    ok = (
        "run_step 是流式的" in text
        and "while IFS= read -r line" in text
        and "PIPESTATUS" in text
        and "每一步都实时落盘" in text
    )

    check("★ check_deploy.py 里有「实时逐行」的静态断言（防回退）", ok)
    return ok


def main():
    print("=" * 70)
    print("CraftLink 交付脚本检查（mcr 实时日志 / 交接文档进包）")
    print("=" * 70)

    print("")
    print("---- 0. 前置：deploy/rebuild.sh 语法 ----")

    result = subprocess.run(
        ["bash", "-n", REBUILD], capture_output=True, text=True
    )
    check("bash -n deploy/rebuild.sh 通过", result.returncode == 0,
          (result.stderr or "").strip()[:300])

    for fn in (
        test_rebuild_source_shape,
        test_run_step_streams_live,
        test_run_step_failure_context,
        test_run_step_verbose_prints_filtered,
        test_handover_is_generated_into_deploy,
    ):
        try:
            fn()
        except Exception:
            import traceback
            check(f"{fn.__name__} 没有抛异常", False)
            traceback.print_exc()
        else:
            check(f"{fn.__name__} 没有抛异常", True)

    print("")
    print("=" * 70)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 70)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
