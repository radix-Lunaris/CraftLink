"""SSH-managed accounts for the database-free experiment relay."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import tempfile

from auth import hash_password


def load_accounts(path):
    path = Path(path)
    try:
        accounts = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(accounts, dict):
        raise ValueError("Invalid account file")
    for name, password_hash in accounts.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", name) or not isinstance(password_hash, str):
            raise ValueError("Invalid account entry")
    return accounts


def modify_account(path, command, name, password=None):
    # Serialize CLI writers only. The relay reads a complete snapshot at startup.
    import fcntl

    path = Path(path)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        accounts = load_accounts(path)
        if command == "add":
            if name in accounts:
                raise ValueError("Account already exists")
            accounts[name] = hash_password(password)
        else:
            if name not in accounts:
                raise ValueError("Account does not exist")
            del accounts[name]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(accounts, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("add", "delete", "list"))
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    path = os.environ.get("MCLINK_ACCOUNT_FILE")
    if not path:
        parser.error("MCLINK_ACCOUNT_FILE is required")
    try:
        if args.command == "list":
            print("\n".join(sorted(load_accounts(path))))
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", args.name or ""):
            parser.error("Username must contain 1–32 letters, digits, '.', '_' or '-'")
        password = None
        if args.command == "add":
            password = getpass.getpass("Password: ")
            if not 8 <= len(password) <= 128 or password != getpass.getpass("Confirm password: "):
                parser.error("Passwords must match and contain 8–128 characters")
        modify_account(path, args.command, args.name, password)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print("Account file updated. Restart the experiment relay to apply.")


if __name__ == "__main__":
    main()
