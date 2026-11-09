#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Normalise line endings to LF across the project.

============================================================================
 WHY THIS EXISTS
============================================================================

A `rebuild.sh` with Windows line endings does not run at all on the server:

    /opt/mc-link/rebuild.sh: line 18: $'\\r': command not found
    /opt/mc-link/rebuild.sh: line 28: syntax error near unexpected token `$'do\\r''

bash sees the carriage return as part of every token. The file is fine on
Windows, refuses to start on Linux, and the error message says nothing about
line endings.

That is exactly what shipped, because some of the edits in this project were
made with tools that write CRLF on Windows (PowerShell's Set-Content, and
Python's io.open() without newline=""). One such rewrite converted a whole
file, and the damage is invisible in every diff view.

So: keep the tree LF-only, and let check_deploy.py FAIL the build if any file
that the server executes has a CRLF in it. This script is the fix.

Usage:
    python scripts/fix_line_endings.py            # report + fix
    python scripts/fix_line_endings.py --check    # report only, exit 1 if bad
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: Text files worth normalising. Binaries (the exes) are never touched.
SUFFIXES = (
    ".py", ".sh", ".php", ".css", ".js", ".txt", ".md", ".yml", ".yaml",
    ".conf", ".spec", ".bat", ".sql", ".json", ".html",
)

#: Files without a suffix that are still text.
NAMES = ("Dockerfile", ".gitattributes", ".gitignore")

SKIP_DIRS = {"__pycache__", ".git", "build", "dist", ".venv", "venv"}


def is_candidate(path):
    name = os.path.basename(path)

    if name in NAMES:
        return True

    return name.endswith(SUFFIXES)


def main():
    check_only = "--check" in sys.argv

    fixed = []
    checked = 0

    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for name in filenames:
            path = os.path.join(dirpath, name)

            if not is_candidate(path):
                continue

            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue

            checked += 1

            if b"\r\n" not in data and b"\r" not in data:
                continue

            fixed.append(os.path.relpath(path, ROOT).replace(os.sep, "/"))

            if check_only:
                continue

            normalised = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

            with open(path, "wb") as handle:
                handle.write(normalised)

    print(f"检查了 {checked} 个文本文件")

    if not fixed:
        print("全部都是 LF 换行，没有问题")
        return 0

    if check_only:
        print("")
        print(f"!! {len(fixed)} 个文件是 CRLF（在 Linux 上会让 bash 直接报错）：")

        for relative in fixed:
            print(f"   {relative}")

        print("")
        print("修法：python scripts/fix_line_endings.py")

        return 1

    print("")
    print(f"已把 {len(fixed)} 个文件改成 LF：")

    for relative in fixed:
        print(f"   {relative}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
