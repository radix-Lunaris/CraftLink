#!/usr/bin/env python3
"""
假 docker（安装器测试用）—— 够真的替身，不是打桩。

============================================================================
 为什么需要它
============================================================================

`install.sh` 要探测/复用/创建 MySQL 容器，还要 build/up 中继与面板。CI 和
开发沙箱里**没有 docker**（本仓库历史上也没有任何能在无 docker 环境里跑
安装器的办法），于是"安装器"这种东西只能靠人肉在真机上试 —— 而它恰恰是最
不能出错的一段（会改数据库账号、会写凭据）。

这个脚本把 `docker` 这个**命令**模拟出来，行为按真实 docker 的关键语义走：

  * `docker ps -a --format ...`、`docker inspect`（含 `.Config.Env`）、
    `docker exec`、`docker start`、`docker restart`、`docker network`、
    `docker compose up -d / ps / logs / down`；
  * `docker exec <mysql> mysql -uroot -pXXX -e "<SQL>"` 会**真的解析并执行**
    那几条 SQL（CREATE DATABASE / CREATE USER / ALTER USER / GRANT /
    FLUSH / SELECT CONCAT(User,'@',Host) FROM mysql.user），并维护一份
    用户-密码-DB 的状态；**root 密码不对就回 Access denied 并以 1 退出** ——
    安装器"读不到 root 凭据就不猜"的分支才有东西可测；
  * 状态存在 `$FAKE_DOCKER_STATE/`（JSON 文件），所以"重复跑一次"能验证
    幂等（凭据不变、容器不重建、数据还在）。

使用（测试自己装 shim，见 tests/install_sh_test.py）：

    export FAKE_DOCKER_STATE=/tmp/xxx
    ln -s fake_docker.py $BIN/docker      # 或者写一个 exec 它的 shim
    PATH=$BIN:$PATH bash install.sh ...

**它不是 docker 的完整实现**：遇到没实现的子命令会明确以非零退出并打印
"fake docker: unsupported ..."，而不是假装成功 —— 安装器写了新用法就该让
测试红，而不是静默通过。
"""

import json
import os
import re
import sys

STATE_DIR = os.environ.get("FAKE_DOCKER_STATE", "")

CONTAINERS = "containers.json"
MYSQL = "mysql.json"
NETWORKS = "networks.json"
CALLS = "calls.log"

DEFAULT_SESSION_DIR = "/var/lib/php/sessions"


# ============================================================
# 状态读写
# ============================================================

def _path(name):
    return os.path.join(STATE_DIR, name)


def load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)

    tmp = _path(name) + ".tmp"

    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)

    os.replace(tmp, _path(name))


def record_call(argv):
    os.makedirs(STATE_DIR, exist_ok=True)

    with open(_path(CALLS), "a", encoding="utf-8") as fh:
        fh.write(" ".join(argv) + "\n")


def containers():
    return load(CONTAINERS, {})


def mysql_state():
    return load(MYSQL, {"databases": [], "users": {}, "grants": [], "rows": 0})


def networks():
    return load(NETWORKS, [])


def out(text=""):
    sys.stdout.write(text + "\n")


def err(text=""):
    sys.stderr.write(text + "\n")


def unsupported(argv):
    err("fake docker: unsupported command: " + " ".join(argv))
    return 127


def env_file_values(path):
    values = {}

    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return values

    for line in lines:
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    return values


# ============================================================
# ps
# ============================================================

def cmd_ps(argv):
    fmt = ""
    names_filter = []

    index = 0

    while index < len(argv):
        arg = argv[index]

        if arg == "--format":
            fmt = argv[index + 1]
            index += 2
            continue

        if arg == "--filter":
            value = argv[index + 1]

            if value.startswith("name="):
                names_filter.append(value[5:])

            index += 2
            continue

        index += 1

    rows = []

    for name, data in sorted(containers().items()):
        if names_filter and not any(f in name for f in names_filter):
            continue

        rows.append((name, data))

    if "table" in fmt or fmt == "":
        header = "NAMES\tSTATUS\tPORTS"

        if "{{.Image}}" in fmt or not fmt:
            header = "NAMES\tIMAGE\tSTATUS"

        out(header)

        for name, data in rows:
            status = "Up 3 minutes" if data.get("running") else "Exited (0) 2 minutes ago"
            ports = data.get("ports") or ""

            if "{{.Image}}" in fmt or not fmt:
                out(f"{name}\t{data.get('image', '')}\t{status}")
            else:
                out(f"{name}\t{status}\t{ports}")

        return 0

    for name, data in rows:
        state = "running" if data.get("running") else "exited"

        line = fmt
        line = line.replace("{{.Names}}", name)
        line = line.replace("{{.Image}}", data.get("image", ""))
        line = line.replace("{{.State}}", state)
        line = line.replace("{{.Status}}", "Up 3 minutes" if data.get("running") else "Exited")
        line = line.replace("{{.Ports}}", data.get("ports") or "")
        out(line.replace("\\t", "\t"))

    return 0


# ============================================================
# inspect
# ============================================================

def cmd_inspect(argv):
    if not argv:
        return unsupported(["inspect"])

    name = argv[0]
    fmt = ""

    for index, arg in enumerate(argv):
        if arg == "--format":
            fmt = argv[index + 1]

    data = containers().get(name)

    if data is None:
        err(f"Error: No such object: {name}")
        return 1

    env_lines = [f"{k}={v}" for k, v in (data.get("env") or {}).items()]

    if fmt:
        if "Config.Env" in fmt:
            out("".join(line + "\n" for line in env_lines).rstrip("\n"))
            return 0

        if "State.Running" in fmt:
            out("true" if data.get("running") else "false")
            return 0

        if "NetworkSettings.Networks" in fmt:
            out(" ".join(data.get("networks") or []))
            return 0

        if "Mounts" in fmt:
            out("\n".join(data.get("volumes") or []))
            return 0

        out("")
        return 0

    payload = {
        "Name": "/" + name,
        "Config": {"Env": env_lines, "Image": data.get("image", "")},
        "State": {"Running": bool(data.get("running"))},
        "NetworkSettings": {
            "Networks": {net: {} for net in (data.get("networks") or [])}
        },
        "Mounts": [
            {"Name": vol, "Destination": "/var/lib/mysql"}
            for vol in (data.get("volumes") or [])
        ],
    }

    out(json.dumps(payload, ensure_ascii=False))
    return 0


# ============================================================
# MySQL（mock 的那几条语句）
# ============================================================

ROOT_PASSWORD_RE = re.compile(r"^-p(.*)$")


def _root_password(args):
    for index, arg in enumerate(args):
        match = ROOT_PASSWORD_RE.match(arg)

        if match:
            if match.group(1):
                return match.group(1)

            # `-p` 单独出现 = 交互输入；mock 里当空密码。
            return ""

        if arg == "--password" and index + 1 < len(args):
            return args[index + 1]

    return ""


def _mysql_error(container):
    env = (containers().get(container) or {}).get("env") or {}
    expected = env.get("MYSQL_ROOT_PASSWORD") or env.get("MARIADB_ROOT_PASSWORD") or ""

    return expected


def _check_root(container, password):
    expected = _mysql_error(container)

    if password != expected:
        err("ERROR 1045 (28000): Access denied for user 'root'@'localhost' "
            "(using password: YES)")
        return False

    return True


def _split_statements(sql):
    return [s.strip() for s in sql.split(";") if s.strip()]


#: 一个"名字"可以是反引号标识符、单引号字符串，或者裸词。
#: 真 MySQL 里三种都合法，`ensure_secrets.provision_mysql()` 也确实三种都用
#: （`CREATE USER \`u\`@'%'`、`ALTER USER \`u\`@\`h\``、`GRANT ... TO \`u\`@\`h\``）。
IDENT = r"(?:`([^`]+)`|'([^']*)'|([A-Za-z0-9_$%.@\-]+))"


def _pick(match, start):
    """从 IDENT 的三选一捕获组里取值（第 start/start+1/start+2 组）。"""
    for offset in range(3):
        value = match.group(start + offset)

        if value is not None:
            return value

    return ""


def _run_sql(container, sql):
    state = mysql_state()
    outputs = []

    for statement in _split_statements(sql):
        upper = statement.upper()

        match = re.match(
            rf"CREATE\s+DATABASE\s+IF\s+NOT\s+EXISTS\s+{IDENT}",
            statement, re.IGNORECASE,
        )

        if match:
            name = _pick(match, 1)

            if name not in state["databases"]:
                state["databases"].append(name)

            continue

        match = re.match(
            rf"SELECT\s+CONCAT\(User,\s*'@',\s*Host\)\s+FROM\s+mysql\.user"
            rf"\s+WHERE\s+User\s*=\s*'([^']*)'",
            statement, re.IGNORECASE,
        )

        if match:
            user = match.group(1)

            for account in sorted(state["users"]):
                if account.split("@", 1)[0] == user:
                    outputs.append(account)

            continue

        match = re.match(
            rf"CREATE\s+USER\s+{IDENT}\s*@\s*{IDENT}\s+IDENTIFIED\s+BY\s+{IDENT}",
            statement, re.IGNORECASE,
        )

        if match:
            user, host, password = _pick(match, 1), _pick(match, 4), _pick(match, 7)
            state["users"][f"{user}@{host}"] = password
            continue

        match = re.match(
            rf"ALTER\s+USER\s+{IDENT}\s*@\s*{IDENT}\s+IDENTIFIED\s+BY\s+{IDENT}",
            statement, re.IGNORECASE,
        )

        if match:
            user, host, password = _pick(match, 1), _pick(match, 4), _pick(match, 7)
            state["users"][f"{user}@{host}"] = password
            continue

        match = re.match(
            rf"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+{IDENT}\s*\.\s*\*\s+TO\s+"
            rf"{IDENT}\s*@\s*{IDENT}",
            statement, re.IGNORECASE,
        )

        if match:
            database = _pick(match, 1)
            user, host = _pick(match, 4), _pick(match, 7)
            state["grants"].append(f"{user}@{host}->{database}")
            continue

        if upper.startswith("FLUSH PRIVILEGES"):
            continue

        if re.match(r"^SELECT\s+1$", statement, re.IGNORECASE):
            outputs.append("1")
            continue

        if upper.startswith("SHOW DATABASES"):
            outputs.extend(state["databases"])
            continue

        if re.match(r"^(SET|USE|START TRANSACTION|COMMIT)", upper):
            continue

        # 没模拟到的语句：明确失败，别让安装器以为成功了。
        err(f"fake docker: unsupported SQL: {statement[:120]}")
        return False, outputs

    state["rows"] = int(state.get("rows") or 0) + 1
    save(MYSQL, state)

    return True, outputs


def cmd_exec(argv):
    # 去掉 -i / -t / -e 之外的 docker exec 选项
    args = []

    for arg in argv:
        if arg in ("-i", "-t", "-it", "-ti", "--interactive", "--tty"):
            continue

        args.append(arg)

    if not args:
        return unsupported(["exec"])

    container = args[0]
    command = args[1:]

    if container not in containers():
        err(f"Error response from daemon: No such container: {container}")
        return 1

    if not command:
        return 0

    if command[0] == "mysql":
        password = _root_password(command[1:])

        if not _check_root(container, password):
            return 1

        sql = ""

        for index, arg in enumerate(command):
            if arg == "-e" and index + 1 < len(command):
                sql = command[index + 1]

        if not sql:
            return 0

        ok, outputs = _run_sql(container, sql)

        if outputs:
            out("\n".join(outputs))

        return 0 if ok else 1

    if command[0] == "mysqladmin":
        password = _root_password(command[1:])

        if not _check_root(container, password):
            return 1

        out("mysqld is alive")
        return 0

    if command[0] == "php" and any("session_save_path" in c for c in command):
        out(DEFAULT_SESSION_DIR)
        return 0

    if command[0] == "sh" and len(command) >= 3 and command[1] == "-c":
        # 会话清理：把命令记进 calls.log 就够了（真删文件在容器里）。
        record_call(["EXEC-SH", container, command[2]])
        return 0

    if command[0] == "python" and len(command) > 1 and "reset_password.py" in command[1]:
        out("用户名: root")
        out("密码  : FakeGeneratedPw123")
        return 0

    return unsupported(["exec", container] + command)


# ============================================================
# network / start / restart / rm / volume
# ============================================================

def cmd_network(argv):
    if not argv:
        return unsupported(["network"])

    action = argv[0]

    if action == "inspect":
        name = argv[1] if len(argv) > 1 else ""

        if name in networks():
            out(json.dumps([{"Name": name}], ensure_ascii=False))
            return 0

        err(f"Error: No such network: {name}")
        return 1

    if action == "create":
        name = argv[-1]
        state = networks()

        if name not in state:
            state.append(name)
            save(NETWORKS, state)

        out(name)
        return 0

    if action == "ls":
        out("NETWORK ID\tNAME")
        for name in networks():
            out(f"deadbeef\t{name}")
        return 0

    return unsupported(["network"] + argv)


def cmd_start(argv):
    state = containers()
    name = argv[-1]

    if name not in state:
        err(f"Error response from daemon: No such container: {name}")
        return 1

    state[name]["running"] = True
    save(CONTAINERS, state)
    out(name)
    return 0


def cmd_restart(argv):
    return cmd_start(argv)


def cmd_rm(argv):
    args = [a for a in argv if a != "-f"]
    state = containers()
    name = args[-1] if args else ""

    if name in state:
        del state[name]
        save(CONTAINERS, state)

    out(name)
    return 0


def cmd_volume(argv):
    if not argv:
        return unsupported(["volume"])

    if argv[0] == "ls":
        out("DRIVER\tVOLUME NAME")
        out("local\tmclink-db-data")
        return 0

    if argv[0] in ("create", "inspect"):
        out(argv[-1])
        return 0

    return unsupported(["volume"] + argv)


# ============================================================
# compose
# ============================================================

def _compose_file(argv):
    for index, arg in enumerate(argv):
        if arg in ("-f", "--file"):
            return argv[index + 1]

    return ""


def _compose_env_file(argv):
    for index, arg in enumerate(argv):
        if arg == "--env-file":
            return argv[index + 1]

    return ""


def _compose_project(argv, compose_file):
    for index, arg in enumerate(argv):
        if arg in ("-p", "--project-name"):
            return argv[index + 1]

    return os.path.dirname(os.path.abspath(compose_file)) if compose_file else "."


def _create_container(name, image, env, networks_list, volumes=None, ports=""):
    state = containers()

    state[name] = {
        "image": image,
        "env": env,
        "running": True,
        "networks": networks_list,
        "volumes": volumes or [],
        "ports": ports,
    }

    save(CONTAINERS, state)


def _compose_up(argv, compose_file, env_file):
    values = dict(os.environ)

    if env_file:
        values.update(env_file_values(env_file))

    networks_list = [values.get("MCLINK_DOCKER_NETWORK", "mclink-net")]
    base = os.path.basename(compose_file)

    if base == "docker-compose.db.yml":
        name = values.get("MCLINK_DB_CONTAINER", "mclink-db")

        _create_container(
            name,
            values.get("MCLINK_DB_IMAGE", "mysql:8.0"),
            {
                "MYSQL_ROOT_PASSWORD": values.get("MCLINK_DB_ROOT_PASSWORD", ""),
                "MYSQL_DATABASE": values.get("MCLINK_DB_NAME", "mclink"),
                "MCLINK_DB_NAME": values.get("MCLINK_DB_NAME", "mclink"),
            },
            networks_list,
            volumes=[values.get("MCLINK_DB_VOLUME", "mclink-db-data")],
        )

        out(f"Container {name}  Started")
        return 0

    if os.path.basename(os.path.dirname(os.path.abspath(compose_file))) == "server":
        _create_container("mc-relay", "mc-link-relay:2.0.0",
                          {}, networks_list, ports="0.0.0.0:19100->19100/tcp")
        out("Container mc-relay  Started")
        return 0

    if os.path.basename(os.path.dirname(os.path.abspath(compose_file))) == "panel":
        name = "mc-panel"

        try:
            with open(compose_file, encoding="utf-8") as fh:
                match = re.search(
                    r"^\s*container_name:\s*[\"']?([\w.\-]+)",
                    fh.read(), re.MULTILINE,
                )

            if match:
                name = match.group(1)
        except OSError:
            pass

        _create_container(name, "mc-link-panel:2.0.0", {},
                          networks_list, ports="127.0.0.1:19102->80/tcp")
        out(f"Container {name}  Started")
        return 0

    out(f"Container for {compose_file} Started")
    return 0


def cmd_compose(argv):
    if argv and argv[0] == "version":
        out("Docker Compose version v2.24.0-fake")
        return 0

    compose_file = _compose_file(argv)
    env_file = _compose_env_file(argv)

    action = ""

    for arg in argv:
        if arg in ("up", "down", "ps", "logs", "build", "config", "restart", "stop"):
            action = arg
            break

    if action == "config":
        out("# fake compose config")
        return 0

    if action in ("up", "build", "restart", "stop"):
        return _compose_up(argv, compose_file, env_file)

    if action == "ps":
        return cmd_ps([])

    if action == "logs":
        out("fake-compose | service started")
        return 0

    if action == "down":
        state = containers()
        base = os.path.basename(compose_file)

        for name in list(state):
            if base == "docker-compose.db.yml" and "db" in name:
                del state[name]
            elif base == "docker-compose.yml":
                if name in ("mc-relay", "mc-panel"):
                    del state[name]

        save(CONTAINERS, state)
        out("Container stopped")
        return 0

    return unsupported(["compose"] + argv)


# ============================================================
# main
# ============================================================

def main(argv):
    if not STATE_DIR:
        err("fake docker: 需要 FAKE_DOCKER_STATE 环境变量（状态目录）")
        return 2

    os.makedirs(STATE_DIR, exist_ok=True)
    record_call(["docker"] + argv)

    if not argv:
        out("Usage: docker [OPTIONS] COMMAND")
        return 0

    if argv[0] in ("--version", "version"):
        out("Docker version 27.0.0-fake, build fake")
        return 0

    command = argv[0]
    rest = argv[1:]

    if command == "ps":
        return cmd_ps(rest)

    if command == "inspect":
        return cmd_inspect(rest)

    if command == "exec":
        return cmd_exec(rest)

    if command == "network":
        return cmd_network(rest)

    if command == "start":
        return cmd_start(rest)

    if command == "restart":
        return cmd_restart(rest)

    if command == "rm":
        return cmd_rm(rest)

    if command == "volume":
        return cmd_volume(rest)

    if command == "compose":
        return cmd_compose(rest)

    if command in ("logs", "stop", "pull", "info"):
        out(f"fake docker: {command} ok")
        return 0

    return unsupported(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
