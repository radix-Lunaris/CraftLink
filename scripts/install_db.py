#!/usr/bin/env python3
"""
CraftLink M5 安装器的**服务端助手**（被包根的 `install.sh` 调用）。

它只做三件事，每件都有明确的失败语义：

    detect     探测本机 Docker 里已有的 MySQL / MariaDB 容器
    provision  复用那个容器：建库 + 建账号 / 改密码 + 授权（**不新建容器**）
    env-set    把 KEY=VALUE 写进 .env（600，有改动先备份）

============================================================================
 为什么单独抽出来（而不是全写在 install.sh 里）
============================================================================

1. **"先改库、后写 .env"这条顺序是安全边界。**
   `.env` 里写一个新密码、库里还是旧密码，是最难查的一种状态（面板从
   "占位符连不上"变成"密码错连不上"）。所以写密码这件事必须发生在
   `provision_mysql()` 返回成功**之后** —— 放在 Python 里，调用方就没机会
   搞反顺序（`scripts/ensure_secrets.py` 是同一套函数，同一条纪律）。

2. **`docker exec` 的引号/转义在 bash 里极易写错。**
   SQL 里带引号、密码里带特殊字符，用 shell 拼字符串迟早出事。这里统一
   走 `ensure_secrets.mysql_exec()`（参数数组，不经过 shell）。

3. 探测/解析 `docker ps` 的输出格式（`--format` 模板）也集中在一处。

用法（install.sh 内部用，也可以手工排障）：

    python3 scripts/install_db.py detect
    python3 scripts/install_db.py provision --container web-mysql80 --env /opt/mc-link/.env
    printf '%s\\n' 'MCLINK_DB_HOST=web-mysql80' | python3 scripts/install_db.py env-set --env /opt/mc-link/.env

返回码：0 = 成功；1 = 失败（原因打在 stderr，调用方据此**立刻停住**）。
"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ensure_secrets import (  # noqa: E402
    docker_available,
    env_values,
    is_placeholder,
    mysql_root_password,
    provision_mysql,
    write_env,
)
from rotate_secrets import API_TOKEN_LEN, DB_PASSWORD_LEN, random_secret  # noqa: E402

#: 认得出哪些镜像是"能复用的 MySQL/MariaDB"。
#:
#: 只看镜像名（`mysql:8.0`、`mariadb:11`、`bitnami/mysql:8`、`1panel/mysql` …），
#: 不看容器名 —— 容器名是用户随便起的（现场那台叫 `web-mysql80`，名字里
#: 既没有 mysql 也没有 db 的含义可依赖）。
MYSQL_IMAGE_RE = re.compile(r"(^|/)(mysql|mariadb)(:[\w.\-]+)?$", re.IGNORECASE)

#: 没指定容器时，按这个顺序找。
DEFAULT_DB_NAME = "mclink"
DEFAULT_DB_USER = "mclink"


def fail(message):
    print(message, file=sys.stderr)
    return 1


def docker_run(args, timeout=60):
    """跑一条 docker 命令，返回 (ok, stdout, stderr)。不经过 shell。"""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, "", f"{e.__class__.__name__}: {e}"

    return result.returncode == 0, result.stdout or "", (result.stderr or "").strip()


def list_containers():
    """
    所有容器（含已停止的）：[{name, image, running}]。

    `docker ps -a` 用 `--format '{{.Names}}\t{{.Image}}\t{{.State}}'`。老版本
    docker 不支持 `{{.State}}` 时退化成两列（running 未知 -> None）。
    """
    ok, out, err = docker_run(
        ["ps", "-a", "--format", "{{.Names}}\t{{.Image}}\t{{.State}}"]
    )

    if not ok:
        ok, out, err = docker_run(
            ["ps", "-a", "--format", "{{.Names}}\t{{.Image}}"]
        )

    if not ok:
        return None, err or "docker ps 失败"

    containers = []

    for line in out.splitlines():
        line = line.strip()

        if not line:
            continue

        parts = line.split("\t")
        name = parts[0].strip()
        image = parts[1].strip() if len(parts) > 1 else ""
        state = parts[2].strip().lower() if len(parts) > 2 else ""

        if not name:
            continue

        containers.append(
            {
                "name": name,
                "image": image,
                "running": (state == "running") if state else None,
            }
        )

    return containers, ""


def find_mysql_containers(containers):
    """从容器列表里挑出 MySQL / MariaDB。"""
    found = []

    for item in containers or []:
        image = item.get("image") or ""
        name = item.get("name") or ""

        # 镜像名去掉 registry 前缀/tag 之后判断；容器名只在镜像看不出来时兜底
        # （有人用 `docker run ... --name mysql-custom myimage`）。
        if MYSQL_IMAGE_RE.search(image.strip()):
            found.append(item)
            continue

        if not image and re.search(r"mysql|mariadb", name, re.IGNORECASE):
            found.append(item)

    return found


def container_running(name):
    ok, out, _err = docker_run(
        ["inspect", name, "--format", "{{.State.Running}}"]
    )

    if not ok:
        return None

    return out.strip().lower() == "true"


def cmd_detect(args):
    """
    探测并输出 JSON（给 install.sh 读）。

    JSON 形状（稳定）：
        {
          "docker": true,
          "containers": [{"name","image","running"}],
          "mysql": [{"name","image","running"}],
          "selected": "web-mysql80" | null
        }
    """
    if not docker_available():
        payload = {
            "docker": False,
            "containers": [],
            "mysql": [],
            "selected": None,
            "error": "docker 命令不存在",
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    containers, err = list_containers()

    if containers is None:
        print(json.dumps(
            {
                "docker": True,
                "containers": [],
                "mysql": [],
                "selected": None,
                "error": err,
            },
            ensure_ascii=False,
        ))
        return 0

    mysql = find_mysql_containers(containers)

    selected = None

    if args.container:
        selected = args.container
    elif len(mysql) == 1:
        selected = mysql[0]["name"]
    elif len(mysql) > 1:
        # 多个时**不猜**（D4）：让 install.sh 去问用户 / 要求 --db-container。
        selected = None

    print(json.dumps(
        {
            "docker": True,
            "containers": containers,
            "mysql": mysql,
            "selected": selected,
        },
        ensure_ascii=False,
    ))

    return 0


def cmd_root_password(args):
    """读容器里的 root 密码（读不到时返回空、退出码 0 —— 让调用方决定怎么办）。"""
    password = mysql_root_password(args.container) or ""
    print(password)
    return 0


def _read_stdin_pairs():
    """从 stdin 读 `KEY=VALUE` 行（install.sh 这样传值：不进 argv / 不进 ps）。"""
    pairs = {}

    for line in sys.stdin.read().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip()

    return pairs


def cmd_env_set(args):
    """把 stdin 的 KEY=VALUE 写进 .env（0600；已有真值默认不动，--force 才覆盖）。"""
    pairs = _read_stdin_pairs()

    if not pairs:
        return fail("[x] env-set：stdin 里没有 KEY=VALUE")

    current = env_values(args.env)
    to_write = {}

    for key, value in pairs.items():
        old = current.get(key)

        if (
            not args.force
            and key in current
            and old
            and not is_placeholder(old)
            and old != value
        ):
            # 已经有一个**真值**：绝不悄悄替换（那等于一次重装换掉线上密码）。
            print(f"  [=] {key}：已有真值，保持不变（--force 才覆盖）")
            continue

        to_write[key] = value

    if not to_write:
        print("  [=] .env 无需改动")
        return 0

    write_env(args.env, to_write, backup=not args.no_backup)

    print("  [√] .env 已更新：" + ", ".join(sorted(to_write)))
    return 0


def cmd_provision(args):
    """
    复用已有 MySQL 容器：建库 + 建账号/改密码 + 授权。

    **只有数据库那边真的成功了，才把密码写进 .env** —— 顺序在这里固定死，
    调用方没有机会搞反。
    """
    if not docker_available():
        return fail("[x] 找不到 docker：不猜、也不写 .env")

    if not args.container:
        return fail("[x] provision 需要 --container（先跑 detect）")

    root_password = args.root_password or mysql_root_password(args.container)

    if not args.root_password and not root_password:
        return fail(
            f"[x] 拿不到 {args.container} 的 root 密码（容器环境变量里没有 "
            "MYSQL_ROOT_PASSWORD/MARIADB_ROOT_PASSWORD）。\n"
            "     请用 --root-password 手工提供（或设 MCLINK_DB_ROOT_PASSWORD），"
            "不要靠猜。"
        )

    values = env_values(args.env) if args.env else {}
    password = args.password or values.get("MCLINK_DB_PASSWORD") or ""

    # `--rotate`：连已有的真值也换掉（安装器的 --rotate-secrets）。默认**绝不**
    # 动已有真值 —— 那等于跑一次安装就换掉线上密码。
    if args.rotate or is_placeholder(password):
        password = random_secret(DB_PASSWORD_LEN)

    token = values.get("MCLINK_ADMIN_API_TOKEN") or ""

    if args.rotate or is_placeholder(token):
        token = random_secret(API_TOKEN_LEN)

    ok, steps = provision_mysql(
        args.container, root_password, args.user, args.database, password
    )

    if not ok:
        for line in steps:
            print(f"  [x] {line}", file=sys.stderr)

        return fail(
            "[x] 数据库准备失败 —— **没有**写 .env（避免出现"
            "『.env 是新密码、库里是旧密码』这种最难查的状态）"
        )

    for line in steps:
        print(f"  [√] {line}")

    if args.env:
        # 走到这里说明库里已经是这个密码了，写 .env 才是安全的。
        write_env(
            args.env,
            {
                "MCLINK_DB_HOST": args.container,
                "MCLINK_DB_PORT": str(args.port),
                "MCLINK_DB_NAME": args.database,
                "MCLINK_DB_USER": args.user,
                "MCLINK_DB_PASSWORD": password,
                "MCLINK_ADMIN_API_TOKEN": token,
            },
            backup=not args.no_backup,
        )

        print(f"  [√] .env 已写入（{args.env}，权限 600）")

    print("PROVISIONED=1")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="CraftLink M5 安装器助手（MySQL 探测/复用 + .env 写入）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("detect", help="探测 Docker 里已有的 MySQL/MariaDB")
    p.add_argument("--container", default="", help="用户指定的容器名（优先）")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("root-password", help="读容器里的 root 密码（读不到回空）")
    p.add_argument("--container", required=True)
    p.set_defaults(func=cmd_root_password)

    p = sub.add_parser("env-set", help="把 stdin 的 KEY=VALUE 写进 .env")
    p.add_argument("--env", required=True)
    p.add_argument("--force", action="store_true", help="覆盖已有的真值")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_env_set)

    p = sub.add_parser("provision", help="建库 + 建账号/改密码 + 授权")
    p.add_argument("--container", default="")
    p.add_argument("--root-password", default="")
    p.add_argument("--env", default="", help="成功后才写这个 .env")
    p.add_argument("--database", default=DEFAULT_DB_NAME)
    p.add_argument("--user", default=DEFAULT_DB_USER)
    p.add_argument("--port", type=int, default=3306)
    p.add_argument("--password", default="", help="留空=用 .env 里的，或新生成")
    p.add_argument(
        "--rotate", action="store_true",
        help="连 .env 里已有的真值也换掉（默认：已有真值绝不动）",
    )
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_provision)

    args = parser.parse_args()

    with contextlib.suppress(BrokenPipeError):
        return args.func(args)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
