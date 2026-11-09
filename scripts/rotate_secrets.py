#!/usr/bin/env python3
"""
轮换 MC-Link 的真实凭据（v3.9.16 新增）。

============================================================================
 为什么需要它
============================================================================

v3.9.15 及以前，`MCLINK_DB_PASSWORD` 与 `MCLINK_ADMIN_API_TOKEN` 是明文写进
交付包的（`panel/docker-compose.yml:67,69`、`server/docker-compose.yml:110,135`），
README 甚至专门写着"本目录里的已填好真实凭据"。包只要外传一次（例如拿去给
别人排查）就等于把数据库和管理 API 一起交出去。

所以**任何人拿到过 v3.9.15 及更早的包，都必须把这两个值换掉**——换值是唯一
有效的补救，把包里的字符串删掉并不算（它可能已经被复制走了）。

============================================================================
 它做什么
============================================================================

  1. 生成两个随机值（DB 密码 20 位、token 48 位，排除 0oO1iIlLq9g 这类
     容易看错/念错的字符 —— 它们是要被人在电话里念出来的）；
  2. 写进 /opt/mc-link/.env（没有就从 .env.example 建，权限 600）；
  3. 打印**必须在 MySQL 里执行**的那条 SQL（脚本不碰数据库：改库密码需要
     DBA 权限，而且一旦写错会当场把面板搞挂，这件事必须人工确认）；
  4. 提示重启顺序。

用法：

    python3 scripts/rotate_secrets.py              # 生成并写 .env
    python3 scripts/rotate_secrets.py --print-only # 只看会生成什么，不写文件
    python3 scripts/rotate_secrets.py --env /opt/mc-link/.env
"""

import argparse
import contextlib
import os
import secrets
import stat
import sys

#: 容易看错/念错的字符一律不用：这些值要被人抄下来、念出来、贴进工单。
AMBIGUOUS = "0oO1iIlLq9g"

ALPHABET = "".join(
    c for c in "abcdefghjkmnprstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ2345678"
    if c not in AMBIGUOUS
)

DB_PASSWORD_LEN = 20
API_TOKEN_LEN = 48

KEYS = ("MCLINK_DB_PASSWORD", "MCLINK_ADMIN_API_TOKEN")


def random_secret(length):
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def read_env(path):
    lines = []

    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    return lines


def write_env(path, values):
    """就地替换（没有就追加），保留注释与顺序，写 0600。"""
    lines = read_env(path)
    out = []
    seen = set()

    for line in lines:
        key = line.split("=", 1)[0].strip()

        if key in values and not key.startswith("#"):
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)

    for key, value in values.items():
        if key not in seen:
            out.append(f"{key}={value}")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")

    with contextlib.suppress(Exception):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)



def main():
    parser = argparse.ArgumentParser(
        description="轮换 MC-Link 的数据库密码与管理 API token"
    )
    parser.add_argument("--env", default=os.environ.get("MCLINK_ENV_FILE", os.path.join(os.environ.get("MCLINK_HOME", str(__import__("pathlib").Path(__file__).resolve().parents[1])), ".env")), help=".env 路径")
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="只打印将要写入的值，不落盘",
    )
    args = parser.parse_args()

    values = {
        "MCLINK_DB_PASSWORD": random_secret(DB_PASSWORD_LEN),
        "MCLINK_ADMIN_API_TOKEN": random_secret(API_TOKEN_LEN),
    }

    if args.print_only:
        for key, value in values.items():
            print(f"{key}={value}")
        return 0

    if not os.path.isdir(os.path.dirname(os.path.abspath(args.env))):
        print(f"[!] 目录不存在：{os.path.dirname(args.env)}", file=sys.stderr)
        return 1

    write_env(args.env, values)

    print("=" * 70)
    print(" 新凭据已写入 " + args.env + "（权限 600）")
    print("=" * 70)
    print()
    print(" 接下来的两步**必须**手工做，脚本故意不替你做：")
    print()
    print("  1) 在 MySQL 里把 mclink 账号的密码改成同一个值：")
    print()
    print(f"     ALTER USER 'mclink'@'%' IDENTIFIED BY '{values['MCLINK_DB_PASSWORD']}';")
    print("     FLUSH PRIVILEGES;")
    print()
    print("     （账号名/主机名以线上为准；面板连不上库时先看 diag-panel.sh。）")
    print()
    print("  2) 重建两个容器让新值生效：")
    print()
    print("     不要 source 配置文件；通过 rebuild.sh 统一读取指定 .env。")
    print("     bash /opt/mc-link/rebuild.sh")
    print()
    print(" 面板与中继的 token 必须一致；不一致时面板会显示")
    print("「无法从中继读取开关状态」—— 那就是 token 没对上，不是面板坏了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
