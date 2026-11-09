"""
MC-Link -- root account recovery tests.

Offline. The database paths run against MemoryStore, so no MySQL is needed.

Two things are tested hardest, because both fail silently in production:

  * the password GENERATOR, whose output becomes a real password and whose
    whole point is that a human can retype it. A regression there would not
    crash anything -- it would just produce passwords people mistype and then
    blame the login.

  * the password VALIDATOR. It has to match the relay's own rule exactly. A
    validator that accepts a symbol would let somebody set a password that
    every subsequent login refuses, which looks exactly like the reset having
    failed.

Run:
    python tests/reset_password_test.py
"""

import asyncio
import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")

sys.path.insert(0, SERVER)

# This suite deliberately pushes exotic strings (中文, emoji) through the
# validator and prints them back. On a GBK console a single un-encodable
# character raises UnicodeEncodeError and kills the whole run -- which looks
# exactly like the code under test being broken. Degrade the character, never
# the test.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(errors="replace")

import reset_password as rp            # noqa: E402
from auth import hash_password, verify_password   # noqa: E402
from db_store import MemoryStore       # noqa: E402

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================
# Generator
# ============================================================

def test_generator():
    print("")
    print("---- 自动生成密码 ----")

    password = rp.generate_password()

    check("长度是 16 位", len(password) == 16, f"len={len(password)}")
    check("只由字母数字组成", password.isalnum(), password)

    forbidden = set("0oO1iIlLq9g")

    check(
        "生成结果不含易混字符 0 o O 1 i I l L q 9 g",
        not (set(password) & forbidden),
        f"出现了 {sorted(set(password) & forbidden)}",
    )

    check(
        "字母表本身就不含易混字符",
        not (set(rp.SAFE_ALPHABET) & forbidden),
        f"字母表里有 {sorted(set(rp.SAFE_ALPHABET) & forbidden)}",
    )

    check(
        "字母表大小 = 62 - 11",
        len(rp.SAFE_ALPHABET) == 51,
        f"len={len(rp.SAFE_ALPHABET)}",
    )

    samples = [rp.generate_password() for _ in range(20)]

    check(
        "连续 20 次都合规",
        all(
            len(s) == 16 and s.isalnum() and not (set(s) & forbidden)
            for s in samples
        ),
    )

    check("20 次互不相同", len(set(samples)) == 20, f"{len(set(samples))} 种")

    seen = set("".join(samples))

    check(
        "覆盖字母表大部分（不是小集合里打转）",
        len(seen) > len(rp.SAFE_ALPHABET) * 0.6,
        f"用到 {len(seen)}/{len(rp.SAFE_ALPHABET)}",
    )


# ============================================================
# Validator
# ============================================================

def test_validator():
    print("")
    print("---- 手动输入校验 ----")

    # Symbols are REJECTED, and this is the important one: the relay and the
    # PHP panel both refuse them at login, so accepting one here would set a
    # password that can never be used.
    #
    # The emoji is written as an escape on purpose: printing it raw killed this
    # suite on a GBK console with UnicodeEncodeError, which looks like a
    # failure of the code under test when it is only a printing problem.
    for symbol in ("@", "#", "!", ".", "-", "_", " ", "中文", "emoji\U0001f642"):
        ok, message = rp.validate(f"abc{symbol}def")
        check(
            f"拒绝含符号的密码：{symbol!r}",
            ok is False,
            f"ok={ok} 没有被拒绝",
        )

    check(
        "拒绝 @ 时给出可读的原因",
        "符号" in rp.validate("abc@def")[1],
        rp.validate("abc@def")[1],
    )

    check("接受纯字母", rp.validate("abcdef")[0] is True)
    check("接受纯数字", rp.validate("123456")[0] is True)
    check("接受字母数字混合", rp.validate("abc123XYZ")[0] is True)

    # Ambiguous characters are FINE when typed by hand: the operator can see
    # what they are typing, and the exclusion only exists for generated ones.
    ok, message = rp.validate("aO0o1iIlLq9gB")
    check(
        "手动输入允许易混字符（只对随机生成做排除）",
        ok is True,
        message,
    )

    check("太短被拒绝", rp.validate("abc")[0] is False, "3 位")
    check("刚好 6 位被接受", rp.validate("abc123")[0] is True)
    check("超长被拒绝", rp.validate("a" * 200)[0] is False)

    # The validator must agree with what the relay would accept.
    check(
        "最短长度和中继一致（6 位）",
        rp.MIN_LENGTH == 6,
        str(rp.MIN_LENGTH),
    )


# ============================================================
# Prompting
# ============================================================

def test_prompting():
    print("")
    print("---- 交互输入 ----")

    # `input` is a builtin, not an attribute of the module, so the patch has
    # to go through builtins. Patching it there also proves the script calls
    # the plain builtin rather than some private wrapper.
    import builtins

    original_input = builtins.input

    def feed(values):
        """Make input() return each value in turn, then raise EOFError."""
        iterator = iter(values)

        def fake(_prompt=""):
            try:
                return next(iterator)
            except StopIteration:
                raise EOFError

        return fake

    try:
        # ---- empty input generates ----
        builtins.input = feed([""])

        with contextlib.redirect_stdout(io.StringIO()):
            generated = rp.ask_password()

        check(
            "直接回车 -> 自动生成 16 位密码",
            generated is not None and len(generated) == 16,
            str(generated),
        )

        # ---- typed input is used as typed ----
        builtins.input = feed(["MyPass123"])

        with contextlib.redirect_stdout(io.StringIO()):
            typed = rp.ask_password()

        check("输入什么就用什么", typed == "MyPass123", str(typed))

        # ---- invalid then valid: retries ----
        builtins.input = feed(["has@symbol", "ok12345"])

        with contextlib.redirect_stdout(io.StringIO()):
            retried = rp.ask_password()

        check(
            "输入含符号后重试，接受第二次的合法输入",
            retried == "ok12345",
            str(retried),
        )

        # ---- the rejection is explained ----
        builtins.input = feed(["bad@pass", "good123"])

        buffer = io.StringIO()

        with contextlib.redirect_stdout(buffer):
            rp.ask_password()

        check(
            "拒绝时打印了原因并要求重新输入",
            "符号" in buffer.getvalue() and "重新输入" in buffer.getvalue(),
            buffer.getvalue()[:200],
        )

        # ---- q cancels ----
        builtins.input = feed(["q"])

        with contextlib.redirect_stdout(io.StringIO()):
            cancelled = rp.ask_password()

        check("输入 q 取消（返回 None）", cancelled is None, str(cancelled))

        # ---- three bad attempts give up rather than looping forever ----
        builtins.input = feed(["a@1", "b@2", "c@3", "d@4"])

        with contextlib.redirect_stdout(io.StringIO()):
            gave_up = rp.ask_password()

        check(
            "连续 3 次不合法就放弃（不会无限循环）",
            gave_up is None,
            str(gave_up),
        )

        # ---- EOF (run without -it) must not crash ----
        builtins.input = feed([])

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                eof = rp.ask_password()
            ok = eof is None
            detail = str(eof)
        except Exception as e:  # noqa: BLE001
            ok = False
            detail = f"{type(e).__name__}: {e}"

        check(
            "没有终端时（未加 -it）优雅退出而不是抛异常",
            ok,
            detail,
        )
    finally:
        builtins.input = original_input


# ============================================================
# Finding the root account
# ============================================================

def test_find_root():
    print("")
    print("---- 定位根管理账号 ----")

    async def scenario_roles():
        store = MemoryStore()
        await store.connect()

        await store.create_user("player1", hash_password("x"))
        await store.create_user("boss", hash_password("x"), is_admin=True)
        await store.create_user("helper", hash_password("x"), is_admin=True)

        # MemoryStore has no role column, so this exercises the fallback that
        # an un-migrated database would take: the FIRST administrator wins.
        row = await rp.find_root(store)

        return row.get("username") if row else None

    found = asyncio.run(scenario_roles())

    check(
        "没有 role 列时回退到「最早的第一个管理员」",
        found == "boss",
        str(found),
    )

    async def scenario_none():
        store = MemoryStore()
        await store.connect()

        await store.create_user("player1", hash_password("x"))
        await store.create_user("player2", hash_password("x"))

        return await rp.find_root(store)

    check(
        "没有管理员时返回 None（脚本会给出提示而不是崩溃）",
        asyncio.run(scenario_none()) is None,
    )


# ============================================================
# The reset itself
# ============================================================

def test_reset():
    print("")
    print("---- 重置 ----")

    async def scenario():
        store = MemoryStore()
        await store.connect()

        await store.create_user("boss", hash_password("oldpass"), is_admin=True)

        before = (await store.get_user("boss"))["password_hash"]

        new_password = rp.generate_password()
        await store.set_user_password("boss", hash_password(new_password))

        after_row = await store.get_user("boss")
        after = after_row["password_hash"]

        new_ok, _ = verify_password(new_password, after)
        old_ok, _ = verify_password("oldpass", after)

        return {
            "changed": before != after,
            "new_works": new_ok,
            "old_dead": not old_ok,
            "format": after.startswith("pbkdf2_sha256$"),
        }

    result = asyncio.run(scenario())

    check("密码哈希变了", result["changed"] is True)
    check(
        "新密码能通过中继的登录校验",
        result["new_works"] is True and result["format"] is True,
        str(result),
    )
    check("旧密码立即失效", result["old_dead"] is True, str(result))


def test_no_arguments_needed():
    print("")
    print("---- 命令形态 ----")

    source = io.open(
        os.path.join(SERVER, "reset_password.py"), encoding="utf-8"
    ).read()

    # The whole point of this round was "one command, nothing to remember".
    check(
        "不接受任何命令行参数（就是一条裸命令）",
        "sys.argv" not in source,
        "脚本仍然读取命令行参数",
    )

    check(
        "不再打印其他账号（只针对根管理）",
        "print_users" not in source and "现有账号" not in source,
        "仍然有列出全部账号的代码",
    )

    check(
        "只操作 root 角色",
        'ROOT_ROLE = "root"' in source,
    )

    # Symbols must be REJECTED: the relay refuses them at login, so a password
    # containing one could be set and then never used.
    check(
        "校验函数会拒绝非字母数字",
        "isalnum()" in source,
    )


def main():
    print("=" * 64)
    print("MC-Link 根管理密码找回测试（离线）")
    print("=" * 64)

    test_generator()
    test_validator()
    test_prompting()
    test_find_root()
    test_reset()
    test_no_arguments_needed()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
