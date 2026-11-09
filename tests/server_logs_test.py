"""
CraftLink -- 服务端日志测试（文件 + 轮转 + 面板导出接口）。

用户的原始诉求：
  「服务端日志在哪里啊？找不到一点。我建议你加上服务端日志这个东西，
    并且可以在 web 端做导出下载到本地。」

以前 log() 就是 print()，日志只在容器标准输出里，重启就没了、也没法下载。
现在：写文件（带轮转）+ 内存环形缓冲 + admin API 两个接口：
  * GET /logs/tail?lines=N   -> JSON，面板预览
  * GET /logs/export         -> text/plain + Content-Disposition，浏览器直接下载

这一套真起中继子进程，真的去请求这两个接口。

Run:
    python tests/server_logs_test.py
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_LOGS_PORT", "19162"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_LOGS_ADMIN_PORT", "19163"))
ADMIN_TOKEN = "test-server-logs-token"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail:
        line += f"  -- {detail}"

    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    (PASSED if ok else FAILED).append(name)


def http_raw(path, timeout=10):
    """返回 (status, headers, body_text)。"""
    request = urllib.request.Request(
        f"http://127.0.0.1:{ADMIN_PORT}{path}",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                dict(response.headers),
                response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")


def http_post(path, payload=None, timeout=10):
    """POST JSON, same auth. Used by the log-clear regression."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{ADMIN_PORT}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={
            "Authorization": f"Bearer {ADMIN_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(
                response.read().decode("utf-8", "replace") or "{}"
            )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")

        with contextlib.suppress(Exception):
            return e.code, json.loads(body or "{}")

        return e.code, {}


def http_json(path, timeout=10):
    status, _headers, body = http_raw(path, timeout)

    with contextlib.suppress(Exception):
        return status, json.loads(body or "{}")

    return status, {}


def port_open(port, timeout=0.4):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


async def wait_until(predicate, timeout=25, interval=0.25, label=""):
    deadline = time.time() + timeout

    while time.time() < deadline:
        if predicate():
            return True

        await asyncio.sleep(interval)

    print(f"  !! timeout waiting for {label}", flush=True)
    return False


def chinese_log_literals():
    """
    服务端日志调用里还残留的中文字面文字。

    ============================================================================
      WHY THIS IS A HARD TEST
    ============================================================================

    用户的要求：服务端日志「记录原始数据，不要加上翻译」，方便后续改进和审计。

    服务端日志是要**发给别人看**的东西（下载、贴聊天、发邮件），而且是事后唯一
    能回答"当时线上到底发生了什么"的材料。中英混排的日志里，中文那一半既不是
    协议原文、也不是可 grep 的字段，两边都够不着。

    所以规矩和客户端一样：日志正文是 `EVENT <name> k=v ...`，全英文、取值原样。
    这个测试把它钉死 —— 新加一行中文日志就是红灯，不会再悄悄漂回去。
    """
    import ast
    import re

    cjk = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
    offenders = []

    for rel in ("relay_server.py", "room_members.py", "relay_log.py",
                "relay_events.py"):
        path = os.path.join(ROOT, "server", rel)

        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), path)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            name = getattr(node.func, "attr", None) or getattr(
                node.func, "id", None
            )

            if name not in ("log", "_log", "event", "_event", "safe_event",
                            "raw", "write") or not node.args:
                continue

            first = node.args[0]
            text = ""

            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                text = first.value
            elif isinstance(first, ast.JoinedStr):
                text = "".join(
                    part.value
                    for part in first.values
                    if isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                )

            if cjk.search(text):
                offenders.append((rel, node.lineno, text.strip()[:70]))

    return offenders


async def main():
    print("=" * 64)
    print("CraftLink server log test (文件日志 + 下载接口)")
    print("=" * 64)

    print("")
    print("---- 静态：服务端日志正文必须是英文结构化事件 ----")

    chinese = chinese_log_literals()

    check(
        "★ 中继日志调用没有中文字面文字（记录原始数据，不做翻译）",
        not chinese,
        " / ".join(f"{f}:{line} {text!r}" for f, line, text in chinese[:10]),
    )

    log_dir = tempfile.mkdtemp(prefix="craftlink-logs-")

    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(RELAY_PORT),
            "MCLINK_DB_ENABLED": "0",
            "MCLINK_REQUIRE_ACCOUNT": "0",
            "MCLINK_ADMIN_API_ENABLED": "1",
            "MCLINK_ADMIN_API_HOST": "127.0.0.1",
            "MCLINK_ADMIN_API_PORT": str(ADMIN_PORT),
            "MCLINK_ADMIN_API_TOKEN": ADMIN_TOKEN,
            "MCLINK_STATUS_FILE": os.path.join(HERE, "server_logs_status.json"),
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            # ★ 被测对象：日志写到这个临时目录
            "MCLINK_LOG_DIR": log_dir,
            "MCLINK_LOG_FILE": "relay.log",
            "MCLINK_LOG_RING_LINES": "300",
            "MCLINK_VERBOSE": "0",
        }
    )

    relay_log_file = os.path.join(log_dir, "relay.log")

    server_out = open(
        os.path.join(HERE, "server_logs_relay.out"),
        "w",
        encoding="utf-8",
        errors="replace",
    )

    relay = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env,
        stdout=server_out,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        ok = await wait_until(
            lambda: port_open(RELAY_PORT) and port_open(ADMIN_PORT),
            timeout=30,
            label="relay + admin api",
        )
        check("中继带着 admin API 起来了", ok)

        if not ok:
            return 1

        # 造一点真实日志：建个房，让中继打几行
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=8
        )
        writer.write(b"CREATE __mclink__ cid-logs owner - logs-room 4\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=8)

        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()

        await asyncio.sleep(1.5)

        # ---- 1. 日志确实落到文件里了 ----
        exists = await wait_until(
            lambda: os.path.exists(relay_log_file)
            and os.path.getsize(relay_log_file) > 0,
            timeout=10,
            label="log file written",
        )

        size = os.path.getsize(relay_log_file) if os.path.exists(relay_log_file) else 0

        check("★ 服务端日志写进文件了（不再是只能 docker logs）", exists, f"{size} 字节")

        content = ""

        if exists:
            content = open(
                relay_log_file, encoding="utf-8", errors="replace"
            ).read()

        check(
            "文件里是真实运行日志（含时间戳与事件）",
            "[" in content and ("CREATE" in content or "JOIN" in content.upper()),
            content.splitlines()[-1][:90] if content.strip() else "(空)",
        )

        # ---- 2. /logs/tail ----
        status, payload = http_json("/logs/tail?lines=50")
        data = payload.get("data") or {}

        check("GET /logs/tail 正常返回 JSON", status == 200 and payload.get("ok") is True, str(status))
        check(
            "tail 给了日志行",
            isinstance(data.get("lines"), list) and len(data["lines"]) > 0,
            f"returned={data.get('returned')} total={data.get('total')}",
        )
        check(
            "tail 报告了文件大小（面板要显示）",
            int(data.get("bytes") or 0) > 0,
            f"file={data.get('file')} bytes={data.get('bytes')}",
        )

        # ---- 3. /logs/export（用户要的"web 端导出下载到本地"）----
        status, headers, body = http_raw("/logs/export")

        check("GET /logs/export 返回 200", status == 200, str(status))

        ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
        check("导出是 text/plain（不是 JSON）", "text/plain" in ctype, ctype)

        disp = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
        check(
            "带 Content-Disposition: attachment（浏览器会弹另存为）",
            "attachment" in disp.lower() and ".log" in disp,
            disp,
        )

        check(
            "导出正文里有真实日志内容",
            "CREATE" in body or "JOIN" in body.upper() or "room=logs-room" in body,
            body.strip().splitlines()[-1][:90] if body.strip() else "(空)",
        )

        check(
            "导出正文带说明头（导出时间/来源文件）",
            "CraftLink" in body and "导出时间" in body,
        )

        # ---- 4. 轮转配置生效（配置能读到）----
        status, payload = http_json("/logs/tail?lines=1")
        data = payload.get("data") or {}

        check(
            "日志文件名为 relay.log（可在服务器上直接找）",
            (data.get("file") or "") == "relay.log",
            str(data.get("file")),
        )

        # ============================================================
        # 4.5 ★ 清空服务端日志：列表也必须一起空掉
        # ============================================================
        #
        # 用户报的现象：「服务端手动删除日志后，列表内容也要一并清理」。
        #
        # 面板上那个列表（「中继日志当前：N 行」和预览）读的是 /logs/tail，
        # 而 tail 读的是**内存环形缓冲**；导出读的是**文件**。所以清空必须
        # 两样都清 —— 只清一样，用户看到的就还是原来那些行。
        before = http_json("/logs/tail?lines=1")[1].get("data") or {}

        status, payload = http_post("/logs/clear")
        # The clear handler answers directly (ok/cleared/message), unlike the
        # /logs/tail handlers which wrap their payload in `data`.
        cleared = payload.get("cleared") or (payload.get("data") or {}).get("cleared") or {}

        check(
            "POST /logs/clear 成功",
            status == 200 and payload.get("ok") is True,
            f"{status} {payload}",
        )

        check(
            "★ 清空回报了它清掉多少（审计要能说出清掉了什么）",
            int(cleared.get("lines") or 0) > 0,
            str(cleared),
        )

        after = http_json("/logs/tail?lines=50")[1].get("data") or {}

        # The list the panel renders. It must be empty apart from the single
        # deliberate "[LOG CLEAR]" line, which is kept so the page can still
        # say who cleared it.
        lines_after = [
            line for line in (after.get("lines") or [])
            if "[LOG CLEAR]" not in line
        ]

        check(
            "★ 清空后 tail 列表为空（用户报的正是「删了但列表还在」）",
            lines_after == [],
            f"还剩下 {len(lines_after)} 行：{lines_after[:2]}",
        )

        check(
            "★ 清空后文件也空了（导出读的是文件，只清缓冲没用）",
            int(after.get("bytes") or 0) < 400,
            f"bytes {before.get('bytes')} -> {after.get('bytes')}",
        )

        check(
            "清空动作本身留了一行痕（否则事后无法回答「谁清的」）",
            any("[LOG CLEAR]" in line for line in (after.get("lines") or [])),
            str((after.get("lines") or [])[-1:])[:120],
        )

        # POST-only: a GET must not be able to wipe the log (CSRF surface).
        get_status, _unused = http_json("/logs/clear")

        check(
            "GET /logs/clear 被拒（清空不能是一个 GET）",
            get_status == 405,
            str(get_status),
        )

        # ============================================================
        # 5. ★ 回归：日志目录不可写时，中继必须照常启动（不能死锁！）
        # ============================================================
        #
        # 线上事故：容器里挂的 ./logs 属主是 root、而中继以 uid 10001 运行，
        # 写日志必然失败。当时 write() 在失败分支里二次获取同一把非重入锁
        # → 永久死锁 → 中继整个卡死（日志停在最后一行、0 次重启、永远
        # unhealthy），看起来却像"卡在连数据库"。
        #
        # 这里用一个**不可能创建**的目录当 LOG_DIR 复现那个条件。
        relay.terminate()

        with contextlib.suppress(Exception):
            relay.wait(timeout=10)

        if relay.poll() is None:
            relay.kill()

        dead_env = dict(env)
        dead_env["MCLINK_LOG_DIR"] = "/proc/craftlink-impossible-logs"
        dead_env["MCLINK_PORT"] = str(RELAY_PORT + 2)
        dead_env["MCLINK_ADMIN_API_PORT"] = str(ADMIN_PORT + 2)

        dead_out = open(
            os.path.join(HERE, "server_logs_dead.out"),
            "w",
            encoding="utf-8",
            errors="replace",
        )

        relay = subprocess.Popen(
            [sys.executable, "-u", "relay_server.py"],
            cwd=os.path.join(ROOT, "server"),
            env=dead_env,
            stdout=dead_out,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        server_out = dead_out

        ok = await asyncio.wait_for(
            wait_until(
                lambda: port_open(RELAY_PORT + 2),
                timeout=25,
                label="relay with unwritable log dir",
            ),
            timeout=30,
        )

        check(
            "★ 日志目录不可写时中继仍然能启动（修复前会死锁卡死）",
            ok,
            "这是线上中继卡死的那个条件",
        )

        if ok:
            # 还能正常服务：建个房看看
            reader2, writer2 = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", RELAY_PORT + 2), timeout=8
            )
            writer2.write(b"CREATE __mclink__ cid-dead dead - deadlog 4\n")
            await writer2.drain()

            reply = (
                await asyncio.wait_for(reader2.readline(), timeout=8)
            ).decode(errors="replace").strip()

            check(
                "★ 而且协议照常可用（建房成功）",
                reply.startswith("OK JOIN"),
                reply,
            )

            with contextlib.suppress(Exception):
                writer2.close()
                await writer2.wait_closed()

    finally:
        relay.terminate()

        with contextlib.suppress(Exception):
            relay.wait(timeout=10)

        if relay.poll() is None:
            relay.kill()

        server_out.close()

        with contextlib.suppress(Exception):
            shutil.rmtree(log_dir, ignore_errors=True)

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
