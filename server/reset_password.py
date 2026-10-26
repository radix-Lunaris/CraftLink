"""
MC-Link relay -- root account recovery.

============================================================================
 ONE COMMAND, ONE PURPOSE
============================================================================

This resets the ROOT account's password and nothing else. It exists because
root can neither be renamed, demoted, transferred, nor have its password
changed from the panel -- by anybody, including itself. Those rules are what
make the role hierarchy worth anything, but they would also mean that
forgetting the root password locks the owner out of their own server for good.

So the way back in is out of band. Anyone who can run a command inside the
relay container already has full control of the machine, so this grants no
privilege that was not already available.

============================================================================
 USAGE
============================================================================

    docker exec -it mc-relay python reset_password.py

That is the whole interface. No arguments, nothing to remember.

It finds the root account, asks for a new password, and prints it:

    用户名: char
    密码  : Kt7mRpXw2nBvHs4d

Pressing Enter with no input generates a random password instead, so the
command can also be run with no typing at all.

Nothing is printed about any other account, and no other account is touched.

============================================================================
 WHY IT IS INTERACTIVE
============================================================================

A password typed as a command line argument ends up in the shell history and
in `ps` output for every user on the box. An interactive prompt keeps it off
disk. (`docker exec -it` is required for the prompt to work: without the -t
there is no terminal for it to read from.)
"""

import asyncio
import os
import secrets
import sys

# The relay modules import each other by bare name, so this has to run with
# /app on the path, which is where the Dockerfile puts it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import hash_password          # noqa: E402
import relay_config as cfg              # noqa: E402
from db_store import MySQLStore         # noqa: E402

#: The only role this script will ever touch.
ROOT_ROLE = "root"

# ------------------------------------------------------------
# Password generation
# ------------------------------------------------------------

#: Characters left out of GENERATED passwords because they are hard to tell
#: apart when read off a terminal and retyped on a phone:
#:     0 o O      zero vs the letter o
#:     1 i I l L  one vs i vs l
#:     q 9 g      mirror pairs that get transposed
#:
#: This applies to generated passwords only. A password the operator types
#: deliberately is used exactly as typed -- they can see what they are doing,
#: and second-guessing them would be obnoxious.
AMBIGUOUS = set("0oO1iIlLq9g")

SAFE_ALPHABET = "".join(
    c for c in
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    if c not in AMBIGUOUS
)

GENERATED_LENGTH = 16

#: The relay's own login rule. Rejected here so the failure happens while
#: somebody is watching, rather than at the next login attempt.
MIN_LENGTH = 6
MAX_LENGTH = 128


def generate_password(length=GENERATED_LENGTH):
    """
    A random password from the unambiguous alphabet.

    `secrets.choice`, not `random`: this becomes a real password, and `random`
    can be predicted from a handful of observed outputs.
    """
    return "".join(secrets.choice(SAFE_ALPHABET) for _ in range(length))


def validate(password):
    """
    Check a password the operator typed. Returns (ok, message).

    Letters and digits only. This is the project's existing rule -- the relay
    and the PHP panel both refuse anything else at account creation -- so
    allowing symbols here would create a password that can be SET but then
    rejected at every login.
    """
    if len(password) < MIN_LENGTH:
        return False, f"密码至少 {MIN_LENGTH} 位"

    if len(password) > MAX_LENGTH:
        return False, f"密码最长 {MAX_LENGTH} 位"

    bad = sorted({c for c in password if not c.isascii() or not c.isalnum()})

    if bad:
        return False, (
            "只能使用字母和数字，不能包含 "
            + " ".join(bad)
            + " 这类符号"
        )

    return True, ""


# ------------------------------------------------------------
# Database
# ------------------------------------------------------------

async def open_store():
    if not cfg.DB_ENABLED:
        print("这个中继没有启用数据库（MCLINK_DB_ENABLED=0），没有账号可恢复。")
        return None

    store = MySQLStore(cfg)

    try:
        await store.connect()
    except Exception as e:  # noqa: BLE001 - reported below
        print(f"无法连接数据库：{type(e).__name__}: {e}")
        print("")
        print("排查：docker exec mc-relay python relay_server.py --dbcheck")
        return None

    return store


async def find_root(store):
    """
    Locate the root account.

    Prefers the `role` column. Falls back to the oldest administrator on a
    database that has not been migrated yet, because the first account the
    relay ever bootstrapped IS the root one by definition.
    """
    try:
        rows = await store.list_users(limit=1000)
    except Exception as e:  # noqa: BLE001
        print(f"读取账号失败：{type(e).__name__}: {e}")
        return None

    rows = rows or []

    for row in rows:
        if str(row.get("role") or "").strip().lower() == ROOT_ROLE:
            return row

    for row in rows:
        if int(row.get("is_admin") or 0) == 1:
            return row

    return None


# ------------------------------------------------------------
# Prompting
# ------------------------------------------------------------

def ask_password():
    """
    Ask for the new password. Returns the password, or None to abort.

    Empty input means "generate one for me", which makes the command runnable
    with no typing at all.
    """
    print("")
    print("请输入新的根管理密码（只允许字母和数字）")
    print("  直接回车 = 自动生成一个 16 位随机密码")
    print("  输入 q     = 取消")
    print("")

    for attempt in range(3):
        try:
            typed = input("新密码: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return None

        if typed.lower() == "q":
            return None

        if not typed:
            return generate_password()

        ok, message = validate(typed)

        if ok:
            return typed

        print(f"  !! {message}，请重新输入")
        print("")

    print("  连续 3 次输入不合法，已取消。")
    return None


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

async def run():
    store = await open_store()

    if store is None:
        return 1

    try:
        row = await find_root(store)

        if row is None:
            print("没有找到根管理账号。")
            print("")
            print("中继第一次启动时会用 MCLINK_BOOTSTRAP_ADMIN 创建它，")
            print("格式是 用户名:密码。在 docker-compose.yml 里设好后重启中继。")
            return 1

        username = str(row.get("username", ""))

        password = ask_password()

        if password is None:
            print("已取消，没有做任何修改。")
            return 1

        try:
            await store.set_user_password(username, hash_password(password))
        except Exception as e:  # noqa: BLE001
            print(f"写入失败：{type(e).__name__}: {e}")
            return 1

        try:
            await store.add_event(
                "password_reset_offline",
                actor=username,
                detail="通过 SSH 命令重置根管理密码",
            )
        except Exception:
            pass

        print("")
        print("=" * 52)
        print(" 根管理账号密码已重置")
        print("=" * 52)
        print(f" 用户名 : {username}")
        print(f" 密码   : {password}")
        print("=" * 52)
        print("")
        print(" 请立刻用这个密码登录面板，并妥善保存。")
        print("")
        print(" 说明：已经在线的旧登录状态不会立刻失效（登录凭证保存在")
        print(" 中继进程的内存里，本命令运行在另一个进程，碰不到它）。")
        print(" 它会自己过期。要立刻踢掉，在面板里操作，或重启中继：")
        print("     docker compose restart mc-relay")
        print("")

        return 0

    finally:
        try:
            await store.close()
        except Exception:
            pass


def main():
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
