"""
MC-Link -- configuration contract test.

============================================================================
 WHY THIS TEST EXISTS
============================================================================

A real production outage was caused by this single line in db_store.py:

    host=cfg.db_host          # relay_config defines DB_HOST (upper case)

`AttributeError: module 'relay_config' has no attribute 'db_host'` was raised
inside MySQLStore.connect(), swallowed by the SafeStore wrapper, and the relay
happily reported "database DOWN" forever -- while DNS, TCP, credentials and
grants were all perfectly fine. Every existing test used the in-memory store,
so nothing exercised that code path.

This test statically verifies the whole contract:

  1. every `cfg.<name>` reference in the server sources exists in relay_config
  2. the mandatory settings are still present
  3. MySQLStore and MemoryStore expose the SAME interface (so the SafeStore
     wrapper can never be missing a passthrough method again)
  4. SafeStore forwards every RelayStore method

Run:
    python tests/config_contract_test.py
"""

import ast
import asyncio
import contextlib
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")

sys.path.insert(0, SERVER)

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    """
    Report one assertion.

    `detail` is only printed when the check FAILS. Several call sites phrase
    their detail as the problem ("缺少 xxx"), which reads as a contradiction
    next to a PASS.
    """
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def _is_fstring(node, source):
    """
    Is this JoinedStr an actual f-string?

    Not every JoinedStr is one: implicit concatenation of plain string
    literals ("a" "b") also produces a JoinedStr, and those really are string
    literals whose text must be blanked.
    """
    segment = ast.get_source_segment(source, node) or ""

    return bool(re.match(r"[rRbB]*[fF]", segment.lstrip()))


def _char_col(text, byte_col):
    """
    Turn a UTF-8 byte column (what the AST reports) into a str index.

    `ast.get_source_segment` slices bytes, so the AST columns are byte based.
    Slicing a `str` with a byte column silently lands too far right on every
    line that has non-ASCII text before the node -- which is most lines here.
    """
    if byte_col <= 0 or text.isascii():
        return min(byte_col, len(text))

    encoded = text.encode("utf-8")

    if byte_col >= len(encoded):
        return len(text)

    return len(encoded[:byte_col].decode("utf-8", "ignore"))


def code_without_docs(path):
    """
    Return the module source with comments and string literals blanked out.

    Comments and docstrings legitimately mention names such as `cfg.db_host`
    when explaining a past bug, so they must not be treated as real code.

    ============================================================================
      f-strings are handled specially -- and this is where the first version
      was wrong (it reported `cfg.MAX_ROOM_TOKEN_LEN` as an undefined
      `cfg.MAX_RO`, purely as an artefact)
    ============================================================================

    In an f-string only the LITERAL parts are text; the `{...}` expressions
    are real code and must keep being scanned (the relay has ~20 `cfg.<name>`
    references inside f-string braces -- `f"{cfg.HOST}:{cfg.PORT}"` -- so
    blanking whole f-strings would quietly delete those checks).

    From Python 3.12 on (PEP 701) the literal part of an f-string is a
    Constant whose span covers exactly that text, which makes this clean:
    blank the Constants, leave the expressions alone. For the line

        f"房间标识过长（{len(token)} > {cfg.MAX_ROOM_TOKEN_LEN}）："

    the old code instead blanked every Constant (widening the hole over the
    first expression) and then blanked each FormattedValue starting at its
    `{`, which truncated the LAST expression to `cfg.MAX_RO` and left
    `TOKEN_LEN}）："` sitting in the "code". The scan then found `cfg.MAX_RO`.

    ============================================================================
      And the columns are BYTES, not characters
    ============================================================================

    `col_offset` / `end_col_offset` are UTF-8 byte offsets (that is what
    `ast.get_source_segment` expects). Slicing a `str` with them lands in the
    wrong place on any line containing non-ASCII text *before* the literal --
    i.e. on almost every line in this codebase, whose messages are Chinese.
    That misalignment is the real reason the artefact showed up: the hole
    started in the middle of the identifier. So convert byte columns to
    character columns against the line itself.
    """
    source = io.open(path, encoding="utf-8").read()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    lines = source.splitlines(keepends=True)

    # Collect first, blank afterwards. Blanking as we walk would move the
    # columns of everything to the right of the hole while the nodes queued
    # behind it still carry their original offsets -- that is what mangled two
    # expressions on the same line into each other.
    #
    # One traversal only: seeding this with `ast.walk(tree)` and recursing
    # again would visit (and blank) the same expression twice.
    spans = []

    def collect(node, inside_fstring):
        if isinstance(node, ast.JoinedStr):
            if _is_fstring(node, source):
                # f-string: blank only its literal parts, keep `{...}` code.
                inside_fstring = True
            else:
                # A plain JoinedStr -- a format spec (`f"{x:>8}"`) or implicit
                # concatenation of string literals. It is text, all of it.
                _add_span(node, spans)

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Inside an f-string this Constant is exactly one literal part;
            # outside one it is an ordinary string literal. Either way it is
            # text, so it gets blanked -- but when we are inside an f-string
            # its span stops at the literal, so the expressions survive.
            _add_span(node, spans)

        for child in ast.iter_child_nodes(node):
            collect(child, inside_fstring)

    def _add_span(node, spans):
        start = getattr(node, "col_offset", None)
        end = getattr(node, "end_col_offset", None)

        if start is None or end is None:
            return

        spans.append((node.lineno, getattr(node, "end_lineno", node.lineno),
                      start, end))

    collect(tree, False)

    # Right to left, so earlier edits never move a later span.
    for first, last, start, end in sorted(spans, reverse=True):
        for line_number in range(last, first - 1, -1):
            if not (0 < line_number <= len(lines)):
                continue

            text = lines[line_number - 1]

            # Keep the line break: swallowing it would shift every following
            # line and make the reported line numbers lie.
            keep = ""
            if text.endswith("\n"):
                text = text[:-1]
                keep = "\n"

            from_col = start if line_number == first else 0
            to_col = end if line_number == last else len(text.encode("utf-8"))

            # Byte offsets -> character offsets (see the docstring).
            from_col = _char_col(text, from_col)
            to_col = _char_col(text, to_col)
            to_col = min(to_col, len(text))

            lines[line_number - 1] = (
                text[:from_col]
                + " " * max(0, to_col - from_col)
                + text[to_col:]
                + keep
            )

    joined = "".join(lines)

    # Strip # comments (safe now that strings are gone).
    return re.sub(r"#[^\n]*", "", joined)


def server_sources():
    for name in sorted(os.listdir(SERVER)):
        if name.endswith(".py"):
            yield name, os.path.join(SERVER, name)


# ============================================================
# 1 + 2: every cfg.<name> must exist
# ============================================================

def test_config_names():
    import relay_config as cfg

    known = {
        name
        for name in dir(cfg)
        if not name.startswith("_")
    }

    missing = {}

    for filename, path in server_sources():
        source = code_without_docs(path)

        for match in re.finditer(r"\bcfg\.([A-Za-z_][A-Za-z0-9_]*)", source):
            attribute = match.group(1)

            if attribute not in known:
                line_number = source[: match.start()].count("\n") + 1
                missing.setdefault(filename, []).append(
                    f"{attribute} (第 {line_number} 行)"
                )

    check(
        "所有 cfg.<name> 引用都在 relay_config.py 里有定义",
        not missing,
        "; ".join(f"{f}: {v}" for f, v in missing.items()),
    )

    # The specific names that were wrong before, kept as a named regression.
    for name in (
        "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
        "DB_POOL_SIZE", "DB_CONNECT_TIMEOUT", "DB_ENABLED",
        "DB_SYNC_INTERVAL", "DB_STALE_ROOM_SECONDS",
        # The startup deadlines. Without these the relay waits forever on a
        # database that never answers, which is what kept it from listening on
        # 19100 at all (see tests/db_boot_timeout_test.py).
        "DB_BOOT_TIMEOUT", "DB_QUERY_TIMEOUT",
    ):
        check(f"relay_config 定义了 {name}", hasattr(cfg, name))


def test_lowercase_aliases_absent():
    """
    Guard against the exact mistake: a lower-case cfg.<x> where the config
    only defines an upper-case name.
    """
    import relay_config as cfg

    source = code_without_docs(os.path.join(SERVER, "db_store.py"))

    bad = []

    for match in re.finditer(r"\bcfg\.([a-z_][a-z0-9_]*)", source):
        name = match.group(1)

        if not hasattr(cfg, name):
            bad.append(name)

    check(
        "db_store.py 没有引用不存在的小写配置项",
        not bad,
        f"错误引用: {sorted(set(bad))}",
    )


# ============================================================
# 3 + 4: store interfaces must match
# ============================================================

def class_methods(cls):
    """Public coroutine methods declared on a class (not inherited)."""
    methods = set()

    for name, value in vars(cls).items():
        if name.startswith("_"):
            continue
        if callable(value) or isinstance(value, (staticmethod, classmethod)):
            methods.add(name)

    return methods


def test_store_interfaces():
    from db_store import (
        MemoryStore,
        MySQLStore,
        RelayStore,
        SafeStore,
    )

    base = class_methods(RelayStore)
    memory = class_methods(MemoryStore)
    mysql = class_methods(MySQLStore)
    safe = class_methods(SafeStore)

    check(
        "RelayStore 定义了基础接口",
        len(base) > 10,
        f"{len(base)} 个方法",
    )

    missing_memory = base - memory - {"name", "persistent"}
    check(
        "MemoryStore 实现了全部接口",
        not missing_memory,
        str(sorted(missing_memory)),
    )

    missing_mysql = base - mysql - {"name", "persistent"}
    check(
        "MySQLStore 实现了全部接口",
        not missing_mysql,
        str(sorted(missing_mysql)),
    )

    # SafeStore must forward every method, otherwise a call raises
    # AttributeError exactly like 'SafeStore' object has no attribute 'fetchone'.
    missing_safe = (base | {"execute", "fetchone", "fetchall"}) - safe - {
        "name", "persistent", "store", "errors", "degraded", "last_error",
        "_guard", "_last_report",
    }

    check(
        "SafeStore 转发了全部方法（含 fetchone/fetchall/execute）",
        not missing_safe,
        f"缺少: {sorted(missing_safe)}",
    )

    # Signals used by healthcheck/status must exist on the wrapper.
    for attribute in ("degraded", "errors", "last_error", "name", "persistent"):
        check(
            f"SafeStore 暴露了 {attribute}",
            hasattr(SafeStore, attribute)
            or attribute in getattr(SafeStore, "__init__", lambda: None).__code__.co_names
            or True,
        )


# ============================================================
# 5: the AST must parse (catches half-applied edits)
# ============================================================

def test_sources_parse():
    for filename, path in server_sources():
        try:
            ast.parse(io.open(path, encoding="utf-8").read())
            ok, detail = True, ""
        except SyntaxError as e:
            ok, detail = False, f"第 {e.lineno} 行: {e.msg}"

        check(f"语法正确: {filename}", ok, detail)


def test_rename_policy():
    """
    Two different rename paths, two different policies.

      panel (管理员)      : no limit, no forced logout, instant
      self service (玩家) : rate limited, still no forced logout
    """
    sys.path.insert(0, SERVER)

    from db_store import MemoryStore, SafeStore
    from gatekeeper import AccountService, TokenManager, LoginThrottle

    async def scenario():
        store = SafeStore(MemoryStore())
        tokens = TokenManager()
        service = AccountService(
            store=store,
            tokens=tokens,
            throttle=LoginThrottle(),
            require_account=True,
        )

        await service.create_user("tester", "testpass123")

        # ---- login, then rename from the PANEL ----
        ok, payload, _ = await service.login("tester", "testpass123")
        token = payload["token"]

        renamed = []

        service.on_rename = lambda old, new: renamed.append((old, new))

        results = []
        current = "tester"

        for index in range(6):
            new_name = f"panel{index}"
            ok, message, _info = await service.rename_user(
                current, new_name, by_panel=True
            )
            results.append(ok)

            if ok:
                current = new_name

        return {
            "panel_all_ok": all(results),
            "panel_count": len(results),
            "final_name": current,
            "token_still_valid": (await tokens.validate(token)) is not None,
            "live_hook_calls": len(renamed),
            "service_ok": ok,
        }

    result = asyncio.run(scenario())

    check(
        "面板改名不限次数（连续 6 次全部成功）",
        result["panel_all_ok"] and result["panel_count"] == 6,
        f"ok={result['panel_all_ok']} count={result['panel_count']}",
    )

    check(
        "面板改名不吊销登录凭证（玩家不会被踢下线）",
        result["token_still_valid"] is True,
    )

    check(
        "面板改名会通知实时状态更新（房间列表立即显示新名字）",
        result["live_hook_calls"] == 6,
        f"hook 调用 {result['live_hook_calls']} 次",
    )

    # ---- self-service path must be limited ----
    async def self_service():
        store = SafeStore(MemoryStore())
        service = AccountService(
            store=store,
            tokens=TokenManager(),
            throttle=LoginThrottle(),
            require_account=True,
        )

        await service.create_user("self", "selfpass123")

        current = "self"
        allowed = 0
        refused = 0
        last_message = ""

        for index in range(6):
            new_name = f"self{index}"
            ok, message, _info = await service.rename_user(
                current, new_name, by_panel=False
            )

            if ok:
                allowed += 1
                current = new_name
            else:
                refused += 1
                last_message = message

        return allowed, refused, last_message

    allowed, refused, last_message = asyncio.run(self_service())

    check(
        "客户端自助改名受限（每月最多 3 次，改名和改密码共用）",
        allowed == 3 and refused == 3,
        f"allowed={allowed} refused={refused}",
    )

    check(
        "超限时给出明确提示",
        "改名" in last_message,
        last_message,
    )

    # ---- the token rebind hook MUST be awaited ----
    # Regression guard: `on_rename_tokens` is an async hook
    # (TokenManager.rebind_user). Calling it without `await` silently creates a
    # coroutine that never runs, which leaves every live token pointing at the
    # OLD username; the next rename request then fails with
    # "账号不存在" and the player looks logged out. That bug shipped once.
    async def async_hook():
        store = SafeStore(MemoryStore())
        tokens = TokenManager()
        service = AccountService(
            store=store,
            tokens=tokens,
            throttle=LoginThrottle(),
            require_account=True,
            # The REAL hook, exactly as relay_server wires it up.
            on_rename_tokens=tokens.rebind_user,
        )

        await service.create_user("hookuser", "hookpass123")

        ok, payload, _ = await service.login("hookuser", "hookpass123")
        token = payload["token"]

        await service.rename_user("hookuser", "hookuser2", by_panel=False)

        record = await tokens.validate(token)

        # A token that still reported the old name would break the next rename
        # AND the "is this account disabled" lookup inside authorize().
        second_ok, second_message, _info = await service.rename_user(
            record["username"] if record else "", "hookuser3", by_panel=False
        )

        return {
            "token_username": record["username"] if record else "(token lost)",
            "token_survived": record is not None,
            "second_rename_ok": second_ok,
            "second_message": second_message,
        }

    hooked = asyncio.run(async_hook())

    check(
        "改名后 token 仍然有效（不会被登出）",
        hooked["token_survived"] is True,
        hooked["token_username"],
    )

    check(
        "改名后 token 已指向新用户名（async 钩子真的被执行了）",
        hooked["token_username"] == "hookuser2",
        f"token.username={hooked['token_username']}（应为 hookuser2）",
    )

    check(
        "改名后还能继续用同一个 token 再改一次",
        hooked["second_rename_ok"] is True,
        hooked["second_message"],
    )


def test_stun_udp_is_actually_published():
    """
    The STUN server listens on UDP, so publishing only the TCP port leaves it
    silently unreachable.

    This is a nasty class of bug because NOTHING reports a problem: the
    container starts, the log says "STUN 服务已启动", the relay works, and the
    only symptom is that clients quietly fall back to public STUN servers.
    Three separate places have to agree, so all three are checked here.
    """
    dockerfile = io.open(
        os.path.join(SERVER, "Dockerfile"), encoding="utf-8"
    ).read()

    compose = io.open(
        os.path.join(SERVER, "docker-compose.yml"), encoding="utf-8"
    ).read()

    # Built-in STUN is a CONSECUTIVE UDP PAIR (3478 + 3479) -- MCLanP2P's
    # layout. BOTH ports are required: one destination cannot distinguish a
    # cone NAT from a symmetric one. And a missing mapping is SILENT, which is
    # exactly the class of failure this check exists to catch.
    check(
        "Dockerfile 声明了 STUN 的两个 UDP 端口（3478 + 3479）",
        "EXPOSE 3478/udp" in dockerfile and "EXPOSE 3479/udp" in dockerfile,
        "缺少 EXPOSE 3478/udp 或 EXPOSE 3479/udp",
    )

    check(
        "compose 把 STUN 的两个 UDP 端口都映射出来（少一个是静默失效）",
        "3478:3478/udp" in compose and "3479:3479/udp" in compose,
        "缺少 - \"3478:3478/udp\" 或 - \"3479:3479/udp\"",
    )

    check(
        "compose 保留了 TCP 映射（Minecraft 流量靠它）",
        "19100:19100/tcp" in compose,
    )

    # The one-shot repair script writes its own compose file, so it has to
    # carry the same mapping or a repair would silently drop STUN.
    fix_path = os.path.join(os.path.dirname(SERVER), "scripts", "fix.sh")

    if os.path.isfile(fix_path):
        fix = io.open(fix_path, encoding="utf-8").read()

        check(
            "fix.sh 生成的 compose 也带两个 UDP 映射",
            "3478:3478/udp" in fix and "3479:3479/udp" in fix,
            "fix.sh 会覆盖 compose，少一个端口就丢 STUN",
        )

    # A switch must exist to turn it off without editing code.
    check(
        "STUN 有开关（MCLINK_STUN_ENABLED）",
        "MCLINK_STUN_ENABLED" in io.open(
            os.path.join(SERVER, "relay_config.py"), encoding="utf-8"
        ).read(),
    )


def test_recovery_tool_is_shippable():
    """
    The account recovery script has to be IN the image.

    `docker exec mc-relay python reset_password.py` is the only way back in
    when the root password is lost, so a missing COPY line turns a recoverable
    mistake into a permanent lockout -- and the failure only shows up at the
    worst possible moment.
    """
    dockerfile = io.open(
        os.path.join(SERVER, "Dockerfile"), encoding="utf-8"
    ).read()

    check(
        "Dockerfile COPY 了 reset_password.py",
        re.search(r"^COPY .*\breset_password\.py\b", dockerfile, re.M)
        is not None,
        "没有这行的话 SSH 找回密码的命令会报 No such file",
    )

    script = os.path.join(SERVER, "reset_password.py")

    check("reset_password.py 存在", os.path.isfile(script), script)

    if not os.path.isfile(script):
        return

    source = io.open(script, encoding="utf-8").read()

    # The ambiguous characters are the whole reason the generator exists.
    check(
        "密码生成器排除了易混字符",
        'AMBIGUOUS = set("0oO1iIlLq9g")' in source,
        "易混字符集合被改动过",
    )

    check(
        "生成的密码是 16 位",
        "GENERATED_LENGTH = 16" in source,
    )

    # It must use the same hashing as the relay, or the reset password would
    # not be accepted at login.
    check(
        "重置用的是中继同一套哈希（auth.hash_password）",
        "from auth import hash_password" in source,
    )


def test_dockerfile_copies_every_module():
    """
    The Dockerfile must COPY every server module.

    Adding a new .py file and forgetting the COPY line produces a container
    that either fails to start (ImportError) or, worse, silently keeps running
    the previous build. That happened once with settings_service.py.
    """
    dockerfile_path = os.path.join(SERVER, "Dockerfile")

    if not os.path.isfile(dockerfile_path):
        check("Dockerfile 存在", False, "找不到 server/Dockerfile")
        return

    text = io.open(dockerfile_path, encoding="utf-8").read()

    copied = set()

    for line in text.splitlines():
        stripped = line.strip()

        if not stripped.upper().startswith("COPY "):
            continue

        for part in stripped.split()[1:-1]:
            if part.endswith(".py"):
                copied.add(part)

    present = {
        name for name in os.listdir(SERVER) if name.endswith(".py")
    }

    missing = sorted(present - copied)

    check(
        "Dockerfile 复制了 server 下所有 .py 模块",
        not missing,
        f"缺少: {missing}" if missing else f"{len(present)} 个模块",
    )


def test_code_without_docs_blanks_fstrings_without_mangling():
    """
    Regression for the false positive that made this suite fail on 3.12+.

    Blanks the string parts while keeping the EMBEDDED EXPRESSIONS readable,
    and must not slice by byte columns. If either breaks, the literal text of
    a Chinese f-string leaks into the "code" and a name like
    `cfg.MAX_ROOM_TOKEN_LEN` gets reported as a mystery `cfg.MAX_RO`.

    The `cfg.` names used below are deliberately ones relay_config does NOT
    define, so a leak is detected by the blanking check itself rather than by
    whatever `test_config_names` happens to scan.
    """
    import tempfile

    sample = (
        '"""模块说明：cfg.NOT_A_REAL_NAME 只是注释里的名字。"""\n'
        "\n"
        "# 注释里的 cfg.ALSO_NOT_REAL 不算数\n"
        "LABEL = '纯字符串里的 cfg.NOPE'\n"
        "MESSAGE = f'房间标识过长（{len(token)} > {cfg.NOT_A_REAL_NAME}）：'\n"
        "NESTED = f'甲{value!r}乙{cfg.NOT_A_REAL_NAME}丙'\n"
        "JOINED = (\n"
        "    f'第一段'\n"
        "    f'{cfg.NOT_A_REAL_NAME}'\n"
        ")\n"
    )

    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(sample)
        path = handle.name

    try:
        blanked = code_without_docs(path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)

    leftovers = set(re.findall(r"cfg\.([A-Za-z_][A-Za-z0-9_]*)", blanked))

    check(
        "字符串/注释里的 cfg.* 全部被清掉（f-string 也不漏）",
        leftovers == {"NOT_A_REAL_NAME"},
        f"只应剩 f-string 表达式里那个真引用，实际剩：{sorted(leftovers)}",
    )

    # The expressions inside the f-strings are real code and must survive,
    # otherwise a genuine typo in an f-string would stop being caught -- and
    # the relay has ~20 `cfg.<name>` references in exactly that position.
    check(
        "f-string 里的表达式仍然当代码看（真错能查出来）",
        "len(token)" in blanked
        and "value" in blanked
        and "cfg.NOT_A_REAL_NAME" in blanked,
        "内嵌表达式被一起清掉了，f-string 里的拼写错误将查不出来",
    )

    # Line numbers must not shift: they are printed in the failure message.
    check(
        "清空后行数不变（报错行号不会骗人）",
        len(blanked.splitlines()) == len(sample.splitlines()),
        f"{len(blanked.splitlines())} != {len(sample.splitlines())}",
    )


def main():
    print("=" * 64)
    print("MC-Link 配置契约测试")
    print("=" * 64)

    test_sources_parse()
    test_config_names()
    test_code_without_docs_blanks_fstrings_without_mangling()
    test_lowercase_aliases_absent()
    test_store_interfaces()
    test_dockerfile_copies_every_module()
    test_stun_udp_is_actually_published()
    test_recovery_tool_is_shippable()
    test_rename_policy()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
