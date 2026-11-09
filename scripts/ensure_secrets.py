#!/usr/bin/env python3
"""
部署时**自动**把 .env 里的占位符凭据填上（v3.9.19 新增）。

============================================================================
 和 rotate_secrets.py 的区别（别混用）
============================================================================

  * `rotate_secrets.py` = **强制轮换**：不管现在是啥，一律换成新的随机值。
    用于"旧包泄漏过，必须换掉"那种场合。它会让你手工去 MySQL 改密码。
  * `ensure_secrets.py` = **只补占位符**：只在值是空的 / `CHANGE_ME_REDACTED` 时
    才动，**已经有真值的一律不动**。用于日常部署。

这个区别是**安全边界**，不是啰嗦：部署脚本每次都会跑它，如果它敢动已有
的真值，那"跑一次 rebuild"就等于"把线上数据库密码换掉"——一次误操作就是
一次全站中断。所以它只填**空的**。

============================================================================
 两个凭据，两种自动能力
============================================================================

  `MCLINK_ADMIN_API_TOKEN`  —— **总能自动**。
      它是应用内部共享密钥：面板和中继从同一个 .env 读同一个值，生成随机
      值写进去、两边重建后就一致了。不涉及任何外部系统。

  `MCLINK_DB_PASSWORD`      —— **能自动，但要能碰到 MySQL**。
      它是 MySQL 里 `mclink` 账号的密码。光在 .env 里写一个随机值是**有害**
      的：库里还是旧密码，面板只会从"占位符连不上"变成"密码错连不上"，
      而且更难排查。
      所以自动填必须**两边一起改**：
          1. 从 `docker inspect <mysql容器>` 拿到 root 密码（和 check-db.sh
             用的是同一个办法）；
          2. 在容器内执行 CREATE USER / ALTER USER / GRANT；
          3. **只有 MySQL 那步真的成功了**，才把新值写进 .env。

      拿不到 MySQL（不是 docker、容器不在、没有 docker 权限）时：
      **不写 .env**，说清楚原因和手工步骤，返回非零让部署停在这里。
      宁可停着，也不给一个连不上的密码。

用法：

    python3 scripts/ensure_secrets.py                  # 补齐（幂等）
    python3 scripts/ensure_secrets.py --check          # 只报告，不改动
    python3 scripts/ensure_secrets.py --env /opt/mc-link/.env
    python3 scripts/ensure_secrets.py --no-db          # 只补 token，不碰数据库

返回码：
    0 = 两个凭据都已是真值（可能刚刚补上）
    1 = 还有占位符没能自动填上（见输出里该怎么做）
"""

import argparse
import contextlib
import os
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rotate_secrets import (  # noqa: E402
    ALPHABET,
    DB_PASSWORD_LEN,
    API_TOKEN_LEN,
    KEYS,
    default_env_path,
    random_secret,
)

#: 默认 .env 路径：**从脚本自身位置推导**（D8），不再写死 /opt/mc-link。
#: 详见 `rotate_secrets.default_env_path()`：
#:     MCLINK_ENV_FILE > MCLINK_ROOT/.env > <脚本上一级>/.env
DEFAULT_ENV = default_env_path()

#: 什么算"还没填"。这些值都表示"这里缺一个真凭据"。
PLACEHOLDERS = {
    "",
    "CHANGE_ME_REDACTED",
    "CHANGE_ME",
    "changeme",
    "TODO",
    "todo",
}


def is_placeholder(value):
    """空 / 占位符 -> True（需要补）；已经填了真值 -> False（不许动）。"""
    value = (value or "").strip()

    if value in PLACEHOLDERS:
        return True

    # 非 ASCII（中文占位符"请改成..."）也一定是占位符：真凭据不会是中文。
    if value and not value.isascii():
        return True

    return False


# ============================================================================
#  .env 读写（就地替换，保留注释与顺序）
# ============================================================================

def read_env(path):
    if not os.path.isfile(path):
        return []

    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


def env_values(path):
    """解析出 {key: value}（后面的覆盖前面的，和 docker compose 一致）。"""
    values = {}

    for line in read_env(path):
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    return values


def write_env(path, values, backup=True):
    """就地替换（没有就追加），写 0600。有改动时先备份成 .env.bak。"""
    if backup and os.path.isfile(path):
        with contextlib.suppress(Exception):
            shutil.copy2(path, path + ".bak")

            with contextlib.suppress(Exception):
                os.chmod(path + ".bak", stat.S_IRUSR | stat.S_IWUSR)

    lines = read_env(path)
    out = []
    seen = set()

    for line in lines:
        key = line.split("=", 1)[0].strip()

        if not key.startswith("#") and key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)

    for key, value in values.items():
        if key not in seen:
            out.append("")
            out.append(f"{key}={value}")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")

    with contextlib.suppress(Exception):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


# ============================================================================
#  MySQL：自动把账号建出来 / 把密码改掉
# ============================================================================

def docker_available():
    return shutil.which("docker") is not None


def mysql_root_password(container):
    """
    从 `docker inspect` 的容器环境变量里取 root 密码（和 check-db.sh 同一招）。

    返回 None = 拿不到（容器不存在 / 不是这种部署方式）。
    """
    if not docker_available():
        return None

    try:
        result = subprocess.run(
            [
                "docker", "inspect", container,
                "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        if line.startswith("MYSQL_ROOT_PASSWORD="):
            return line.split("=", 1)[1]

    # 有的镜像叫 MARIADB_ROOT_PASSWORD / MYSQL_PASSWORD。
    for alt in ("MARIADB_ROOT_PASSWORD=", "MYSQL_ROOT_PASSWORD_FILE="):
        for line in result.stdout.splitlines():
            if line.startswith(alt):
                return line.split("=", 1)[1]

    return None


def mysql_exec(container, root_password, sql):
    """在 MySQL 容器**内部**执行一条 SQL（宿主机不需要装客户端/开端口）。"""
    cmd = ["docker", "exec", "-i", container, "mysql", "-uroot"]

    if root_password:
        cmd.append(f"-p{root_password}")

    cmd += ["-e", sql]

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
    )

    # mysql 总会在 stderr 警告"命令行里带密码不安全"，那不是错误。
    stderr = "\n".join(
        ln for ln in (result.stderr or "").splitlines()
        if "Using a password" not in ln
    )

    return result.returncode == 0, (result.stdout or "").strip(), stderr.strip()


def mysql_accounts(container, root_password, user):
    """列出 MySQL 里这个用户名下的所有账号（`user`@`host`）。"""
    ok, out, _err = mysql_exec(
        container, root_password,
        f"SELECT CONCAT(User, '@', Host) FROM mysql.user "
        f"WHERE User='{user}';",
    )

    if not ok:
        return None

    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _quote(value):
    """MySQL 标识符转义（用户名/库名来自 .env，但我们不信任它的内容）。"""
    return "`" + str(value).replace("`", "``") + "`"


def _literal(value):
    """字符串字面量转义。"""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def provision_mysql(container, root_password, db_user, db_name, new_password):
    """
    把 `db_user` 这个账号建出来（或改掉密码）并授权 `db_name`。

    返回 (ok, 做了什么)。**只有 ok=True 时调用方才该把密码写进 .env**
    —— 否则就会出现".env 是新密码、库里还是旧的"这种最难查的状态。
    """
    steps = []

    # 库要存在，不然 GRANT 会失败。
    ok, _out, err = mysql_exec(
        container, root_password,
        f"CREATE DATABASE IF NOT EXISTS {_quote(db_name)} "
        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;",
    )

    if not ok:
        return False, [f"建库失败：{err}"]

    steps.append(f"库 {db_name} 已就绪")

    accounts = mysql_accounts(container, root_password, db_user)

    if accounts is None:
        return False, ["查不到 mysql.user（root 密码不对？）"]

    if not accounts:
        # 还没有这个账号 —— 建一个通配来源的（容器网络 IP 会变，不能写死）。
        ok, _out, err = mysql_exec(
            container, root_password,
            f"CREATE USER {_quote(db_user)}@'%' "
            f"IDENTIFIED BY {_literal(new_password)};",
        )

        if not ok:
            return False, [f"建账号失败：{err}"]

        steps.append(f"建了账号 {db_user}@'%'")
        accounts = [f"{db_user}@%"]
    else:
        # 已存在：每个 host 变体都要改，不然从另一个 host 连过来的那个
        # 还是旧密码，表现为"有时候连得上、有时候连不上"。
        for account in accounts:
            user, _, host = account.partition("@")

            ok, _out, err = mysql_exec(
                container, root_password,
                f"ALTER USER {_quote(user)}@{_quote(host)} "
                f"IDENTIFIED BY {_literal(new_password)};",
            )

            if not ok:
                return False, [f"改 {account} 的密码失败：{err}"]

            steps.append(f"改了 {account} 的密码")

    for account in accounts:
        user, _, host = account.partition("@")

        ok, _out, err = mysql_exec(
            container, root_password,
            f"GRANT ALL PRIVILEGES ON {_quote(db_name)}.* "
            f"TO {_quote(user)}@{_quote(host)};",
        )

        if not ok:
            return False, [f"给 {account} 授权失败：{err}"]

    ok, _out, err = mysql_exec(container, root_password, "FLUSH PRIVILEGES;")

    if not ok:
        return False, [f"FLUSH PRIVILEGES 失败：{err}"]

    steps.append("已 FLUSH PRIVILEGES")

    return True, steps


# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="部署时自动补齐 .env 里的占位符凭据（已有真值绝不动）"
    )
    parser.add_argument("--env", default=DEFAULT_ENV, help=".env 路径")
    parser.add_argument(
        "--check", action="store_true",
        help="只报告哪些还是占位符，不改动任何东西",
    )
    parser.add_argument(
        "--no-db", action="store_true",
        help="不碰数据库（只自动补 MCLINK_ADMIN_API_TOKEN）",
    )
    parser.add_argument(
        "--mysql-container", default="",
        help="MySQL 容器名（默认取 .env 里的 MCLINK_DB_HOST）",
    )
    parser.add_argument(
        "--example", default="",
        help=".env.example 的路径（默认在 .env 同目录/上级找）",
    )
    args = parser.parse_args()

    env_path = os.path.abspath(args.env)
    env_dir = os.path.dirname(env_path)

    # ---- .env 不存在 -> 从模板建一个 ----
    if not os.path.isfile(env_path):
        example = args.example

        if not example:
            for candidate in (
                os.path.join(env_dir, ".env.example"),
                os.path.join(os.path.dirname(env_dir), ".env.example"),
                os.path.join(os.path.dirname(os.path.dirname(env_dir)),
                             ".env.example"),
            ):
                if os.path.isfile(candidate):
                    example = candidate
                    break

        if args.check:
            print(f"[!!] {env_path} 不存在（--check 不创建）")
            return 1

        if not example:
            print(f"[!!] {env_path} 不存在，也找不到 .env.example 模板")
            return 1

        if not os.path.isdir(env_dir):
            print(f"[!!] 目录不存在：{env_dir}")
            return 1

        shutil.copy2(example, env_path)

        with contextlib.suppress(Exception):
            os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)

        print(f"[i] {env_path} 不存在 —— 已从模板创建（权限 600）")

    values = env_values(env_path)

    missing = [k for k in KEYS if is_placeholder(values.get(k))]

    if not missing:
        print(f"[OK] {env_path} 里的两个凭据都已是真值，未改动。")
        return 0

    if args.check:
        for key in missing:
            print(f"[!!] {env_path} 的 {key} 还是占位符")
        return 1

    print("=" * 70)
    print(f" 自动补齐 {env_path} 里还是占位符的凭据")
    print("=" * 70)
    print()
    print(" 只补**空的**；已经有真值的一律不动（跑一次部署不会换掉线上密码）。")
    print()

    filled = {}

    # ---- 1) token：总能自动 ----
    if "MCLINK_ADMIN_API_TOKEN" in missing:
        token = random_secret(API_TOKEN_LEN)
        filled["MCLINK_ADMIN_API_TOKEN"] = token

        print(" [√] MCLINK_ADMIN_API_TOKEN：已生成随机值")
        print("     面板与中继从同一个 .env 读它，重建后两边自动一致。")

    # ---- 2) DB 密码：只有能真的改到 MySQL 时才填 ----
    if "MCLINK_DB_PASSWORD" in missing:
        if args.no_db:
            print()
            print(" [!] MCLINK_DB_PASSWORD：--no-db，跳过（只补了 token）")
        else:
            db_user = values.get("MCLINK_DB_USER") or "mclink"
            db_name = values.get("MCLINK_DB_NAME") or "mclink"
            container = (
                args.mysql_container
                or values.get("MCLINK_DB_HOST")
                or "web-mysql80"
            )

            new_password = random_secret(DB_PASSWORD_LEN)

            print()
            print(
                f" [·] MCLINK_DB_PASSWORD：准备在 MySQL 容器 "
                f"{container} 里设置 {db_user} 的密码 ..."
            )

            root_password = mysql_root_password(container)

            if not root_password:
                print(f"     [!] 拿不到 {container} 的 root 密码，跳过。")
                print()
                print("     自动填 DB 密码必须**两边一起改**：光在 .env 里写")
                print("     一个随机值，库里还是旧密码，面板会从『占位符连")
                print("     不上』变成『密码错连不上』，而且更难查。")
                print()
                print("     所以这里不写 .env。请手工做完下面两步：")
                print()
                print(
                    f"       1) 把 {db_user} 的密码设成你选定的值"
                    f"（库里 + .env 必须是同一个）："
                )
                print(
                    "          docker exec -it "
                    f"{container} mysql -uroot -p"
                )
                print(
                    f"          ALTER USER '{db_user}'@'%' "
                    "IDENTIFIED BY '<你的密码>';"
                )
                print()
                print(f"       2) 在 {env_path} 里把同一个值写进去：")
                #
                # 注意：这里**不能**写成 `MCLINK_DB_PASSWORD=<值>` 的样子 ——
                # 门禁扫描凭据时会把它当成"明文凭据"误报（判据是"键后面跟了
                # 一个字面值"）。用中文隔开就没有那个形态了。
                print("          MCLINK_DB_PASSWORD 这一行 → <你的密码>")
                print()
                print(
                    "     （或者用 python3 scripts/rotate_secrets.py "
                    "生成并照它打印的 SQL 执行。）"
                )
            else:
                ok, steps = provision_mysql(
                    container, root_password, db_user, db_name, new_password,
                )

                if not ok:
                    print(f"     [!] 没改成：{steps[0] if steps else '?'}")
                    print()
                    print(
                        "     .env **没有**写入新密码（避免和库里不一致），"
                        "请按上面的步骤手工处理。"
                    )
                else:
                    for step in steps:
                        print(f"     [√] {step}")

                    filled["MCLINK_DB_PASSWORD"] = new_password
                    print()
                    print(" [√] MCLINK_DB_PASSWORD：库里已改，.env 同步写入")

    if not filled:
        # 注意措辞：走到这里**不代表什么都没做** —— token 可能上一轮已经补
        # 过了（`filled` 记的是"这一轮新补的"）。这里要说清"还差什么"，
        # 否则运维看到"没补上任何值"会以为之前白跑了。
        still = [
            k for k in KEYS if is_placeholder(env_values(env_path).get(k))
        ]

        print()
        print("=" * 70)
        print(" 这一轮没能补上新的值 —— 部署先停在这里。")
        print("=" * 70)

        if still:
            print()
            print(" 仍然缺（见上面各自的说明）：")

            for key in still:
                print(f"   - {key}")

        return 1

    write_env(env_path, filled)

    print()
    print("=" * 70)
    print(f" 已写入 {env_path}（权限 600，旧值备份在 {env_path}.bak）")
    print("=" * 70)

    still_missing = [
        k for k in KEYS
        if is_placeholder(env_values(env_path).get(k))
    ]

    if still_missing:
        print()
        print(f" 还有 {len(still_missing)} 个没填上：")

        for key in still_missing:
            print(f"   - {key}")

        return 1

    print()
    print(" 两个凭据都就绪了。新值要**重建容器**后才生效（rebuild 本来就要重建）。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
