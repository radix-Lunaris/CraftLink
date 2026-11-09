"""
MC-Link -- "remember my password" tests.

Offline, and written to a temporary directory so a test run can never touch a
real stored credential.

The requirement being pinned here is the version rule: a stored password must
be DISCARDED when the app version changes, not migrated. That is easy to get
wrong in a way nothing notices -- the feature would simply keep working, with
a password that nobody re-confirmed after an update.

Run:
    python tests/credentials_test.py
"""

import importlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "client"))

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def fresh_module(sandbox):
    """
    Import the credential module with the **user data directory** in a sandbox.

    M6 起凭据不再是"自己拼 %APPDATA%\\MC-Link"，而是放在用户数据目录里
    （见 client/mclink/config/userdirs.py）。所以沙箱要用 `MCLINK_DATA_DIR`
    指定；同时把 APPDATA / XDG_CONFIG_HOME 也指到沙箱里 —— 这样连"默认
    目录"和指针文件都不可能落到真实的用户目录上。

    The module reads these at call time (not import time), so setting the
    environment and reimporting gives a clean slate per test.
    """
    os.environ["MCLINK_DATA_DIR"] = sandbox
    os.environ["APPDATA"] = sandbox
    os.environ["XDG_CONFIG_HOME"] = sandbox

    import mclink.config.credentials as credentials

    importlib.reload(credentials)

    return credentials


def main():
    print("=" * 64)
    print("MC-Link 记住密码测试（离线，沙箱目录）")
    print("=" * 64)

    sandbox = tempfile.mkdtemp(prefix="mclink-cred-")
    original_appdata = os.environ.get("APPDATA")
    original_xdg = os.environ.get("XDG_CONFIG_HOME")
    original_data_dir = os.environ.get("MCLINK_DATA_DIR")

    try:
        credentials = fresh_module(sandbox)

        # ---- 1. nothing stored yet ----
        print("")
        print("---- 空状态 ----")

        check("初始状态没有保存的凭据", credentials.exists() is False)

        user, password = credentials.load("2.0.0")

        check(
            "没有凭据时 load() 返回 (None, None) 而不是抛异常",
            user is None and password is None,
            f"{user!r}, {password!r}",
        )

        # ---- 2. save and load ----
        print("")
        print("---- 保存与读取 ----")

        saved = credentials.save("char", "MyPass123", "2.0.0")

        check("save() 报告成功", saved is True)

        check("保存后文件存在", credentials.exists() is True)

        user, password = credentials.load("2.0.0")

        check(
            "同版本能读回用户名和密码",
            user == "char" and password == "MyPass123",
            f"{user!r}, {password!r}",
        )

        # ---- 3. the file must not contain the plaintext ----
        print("")
        print("---- 落盘内容 ----")

        path = credentials._storage_path()
        raw = open(path, "r", encoding="utf-8").read()

        check(
            "文件里找不到明文密码",
            "MyPass123" not in raw,
            "明文密码出现在文件里",
        )

        check(
            "文件里记录了写入它的 app 版本",
            "2.0.0" in raw,
        )

        check(
            "用户名是明文（只有密码需要保护）",
            "char" in raw,
        )

        # ---- 4. version change discards the PASSWORD but keeps the CHOICE ----
        print("")
        print("---- 版本更新：密码要重输，但「记住」的勾选项要保留 ----")

        credentials.save("char", "MyPass123", "2.0.0")

        check(
            "前置条件：保存后勾选状态为「要记住」",
            credentials.remember_choice() is True,
        )

        user, password = credentials.load("2.0.1")

        check(
            "版本不同时读不回密码",
            user is None and password is None,
            f"{user!r}, {password!r}",
        )

        check(
            "版本不同时密码文件已删除",
            credentials.exists() is False,
            "文件仍然存在",
        )

        check(
            "版本不同时「记住密码」的勾选状态被保留（否则更新后功能会静默关闭）",
            credentials.remember_choice() is True,
            "勾选状态被一起清掉了",
        )

        # And the round trip that matters: type it once more, it is stored again.
        credentials.save("char", "MyPass123", "2.0.1")

        user, password = credentials.load("2.0.1")

        check(
            "更新后重新输入一次即可继续记住",
            user == "char" and password == "MyPass123",
            f"{user!r}, {password!r}",
        )

        # Same for an old version, in case somebody downgrades.
        user, password = credentials.load("1.9.9")

        check(
            "降级到旧版本同样只清密码、保留勾选",
            user is None
            and not credentials.exists()
            and credentials.remember_choice() is True,
            f"{user!r}, {password!r}, exists={credentials.exists()}, "
            f"remember={credentials.remember_choice()}",
        )

        # ---- 4b. unticking must forget BOTH ----
        print("")
        print("---- 取消勾选要连「记住」的意愿一起忘掉 ----")

        credentials.save("char", "MyPass123", "2.0.0")

        check("前置：密码存了", credentials.exists() is True)
        check("前置：勾选为真", credentials.remember_choice() is True)

        credentials.clear()

        check("clear() 之后密码没了", credentials.exists() is False)
        check(
            "clear() 之后勾选状态也没了（取消勾选必须真的取消）",
            credentials.remember_choice() is False,
            "勾选状态还留着",
        )

        # ---- 5. tampering is detected ----
        print("")
        print("---- 篡改检测 ----")

        credentials.save("char", "MyPass123", "2.0.0")

        import base64
        import json

        payload = json.loads(open(path, encoding="utf-8").read())

        # Flip the blob to a different password, leaving the HMAC alone. This
        # is what an attacker editing the file would produce, and handing the
        # user a silently wrong password would be worse than forgetting it.
        fake = base64.b64encode(b"WrongPass999").decode("ascii")
        payload["blob"] = fake

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

        user, password = credentials.load("2.0.0")

        check(
            "被篡改的文件读不回凭据（而不是返回错误密码）",
            user is None and password is None,
            f"{user!r}, {password!r}",
        )

        check(
            "被篡改的文件被删除",
            credentials.exists() is False,
        )

        # ---- 6. corrupt file must not crash ----
        print("")
        print("---- 损坏文件 ----")

        for label, content in (
            ("不是 JSON", "this is not json at all"),
            ("是 JSON 但不是对象", "[1, 2, 3]"),
            ("缺少字段", '{"format": 1}'),
            ("base64 乱码", '{"format":1,"app_version":"2.0.0",'
                            '"username":"x","salt":"!!","nonce":"!!",'
                            '"blob":"!!","auth":"!!"}'),
        ):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)

            try:
                user, password = credentials.load("2.0.0")
                ok = user is None and password is None
                detail = f"{user!r}, {password!r}"
            except Exception as e:  # noqa: BLE001
                ok = False
                detail = f"{type(e).__name__}: {e}"

            check(f"损坏的文件不会崩：{label}", ok, detail)

            # And the module must survive it for the NEXT call too.
            check(
                f"损坏被清理，后续调用正常：{label}",
                credentials.load("2.0.0") == (None, None),
            )

        # ---- 7. clear ----
        print("")
        print("---- 主动清除 ----")

        credentials.save("char", "MyPass123", "2.0.0")

        check("前置条件：文件已存在", credentials.exists() is True)

        cleared = credentials.clear()

        check("clear() 报告成功", cleared is True)
        check("clear() 之后文件没了", credentials.exists() is False)

        check(
            "重复 clear() 不会抛异常（幂等）",
            credentials.clear() is False,
        )

        # ---- 8. empty inputs are refused ----
        print("")
        print("---- 空输入 ----")

        check("空用户名不保存", credentials.save("", "pw", "2.0.0") is False)
        check("空密码不保存", credentials.save("char", "", "2.0.0") is False)
        check("两者都空不保存", credentials.save("", "", "2.0.0") is False)
        check("因此没有留下文件", credentials.exists() is False)

        # ---- 9. a round trip with awkward characters ----
        print("")
        print("---- 特殊字符 ----")

        for value in ("a" * 200, "中文密码", "with space", "sym!@#$%^&*()"):
            credentials.save("char", value, "2.0.0")
            _user, got = credentials.load("2.0.0")

            check(
                f"能完整往返：{value[:16]!r}{'...' if len(value) > 16 else ''}",
                got == value,
                f"got {got!r}",
            )

    finally:
        for _key, _value in (
            ("MCLINK_DATA_DIR", original_data_dir),
            ("APPDATA", original_appdata),
            ("XDG_CONFIG_HOME", original_xdg),
        ):
            if _value is None:
                os.environ.pop(_key, None)
            else:
                os.environ[_key] = _value

        shutil.rmtree(sandbox, ignore_errors=True)

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
