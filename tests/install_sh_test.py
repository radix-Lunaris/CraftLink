#!/usr/bin/env python3
"""
M5：服务端安装器 + SSH 管理台的行为测试（**用假 docker**，不需要真 docker）。

============================================================================
 这一套为什么存在
============================================================================

`install.sh` 是新用户接触到的**第一条命令**，而它要做的事每件都很危险：
探测/复用别人的 MySQL 容器、改数据库账号密码、写明文凭据、起容器。出错
方式也都很贵：连到别人的库、把线上密码换掉、留下半装状态。

历史上这类脚本"只能靠人在真机上试"，因为 CI / 沙箱里没有 docker。所以这里
用 `tests/fake_docker.py`（会真的解析并执行那几条 SQL、维护用户/密码/库的
状态）把三条路径都跑一遍：

    1. **全新机器**：没有现成 MySQL -> 用 compose 起一个 -> 建库建账号；
    2. **已有 MySQL**：探测到就**复用**（绝不新建容器），改掉 mclink 账号密码；
    3. **重复跑**：幂等 —— 凭据不变、库数据不动、容器不重建。

外加两条"必须失败"的路径（失败即停，不许留下半装状态），以及管理台的
root-only / 审计日志 / 每个菜单动作。

Run:
    python3 tests/install_sh_test.py
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAKE_DOCKER = os.path.join(HERE, "fake_docker.py")

#: 打桩的"包"里必须有的文件 —— 从**真包**复制，而不是在这里手写一份。
#: 这样安装器改了期望（比如多要一个脚本）而包没跟上时，测试会红。
PACKAGE_FILES = (
    "install.sh",
    "docker-compose.db.yml",
    "scripts/install_db.py",
    "scripts/install_patch.py",
    "scripts/ensure_secrets.py",
    "scripts/rotate_secrets.py",
    "scripts/mclink-console.sh",
    "server/docker-compose.yml",
    "panel/docker-compose.yml",
)

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_env(path):
    values = {}

    for line in read(path).splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    return values


def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# ============================================================
# 环境搭建
# ============================================================

def make_package(tmp):
    """把真包的必要文件复制成一个最小"上传目录"。"""
    package = os.path.join(tmp, "upload")
    os.makedirs(os.path.join(package, "scripts"), exist_ok=True)

    for rel in PACKAGE_FILES:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(package, rel)

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    return package


def make_env(tmp, fake_root=False):
    """装好 docker（+ 可选 id）shim，返回 (env, bin_dir, state_dir)。"""
    bin_dir = os.path.join(tmp, "bin")
    state_dir = os.path.join(tmp, "state")
    os.makedirs(bin_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)

    with open(os.path.join(bin_dir, "docker"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
        fh.write(f'exec python3 "{FAKE_DOCKER}" "$@"\n')

    os.chmod(os.path.join(bin_dir, "docker"), 0o755)

    if fake_root:
        real_id = shutil.which("id") or "/usr/bin/id"

        with open(os.path.join(bin_dir, "id"), "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
            fh.write('if [ "${1:-}" = "-u" ]; then echo 0; exit 0; fi\n')
            fh.write(f'exec "{real_id}" "$@"\n')

        os.chmod(os.path.join(bin_dir, "id"), 0o755)

    env = dict(os.environ)
    env["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    env["FAKE_DOCKER_STATE"] = state_dir

    return env, bin_dir, state_dir


def run(command, env, cwd=None, timeout=180, stdin=""):
    return subprocess.run(
        command, env=env, cwd=cwd, capture_output=True, text=True,
        timeout=timeout, input=stdin,
    )


def run_install(package, env, *args, **kwargs):
    return run(["bash", "install.sh", "--yes", *args], env, cwd=package, **kwargs)


def seed_containers(state, data):
    with open(os.path.join(state, "containers.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def seed_mysql(state, data):
    with open(os.path.join(state, "mysql.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def mysql_state(state):
    return load(os.path.join(state, "mysql.json"),
                {"databases": [], "users": {}, "grants": [], "rows": 0})


def container_state(state):
    return load(os.path.join(state, "containers.json"), {})


def docker_calls(state):
    try:
        with open(os.path.join(state, "calls.log"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


# ============================================================
# 1. 全新机器
# ============================================================

def scenario_fresh(tmp):
    print("")
    print("---- 1. 全新机器：没有现成 MySQL -> compose 起一个 ----")

    package = make_package(tmp)
    env, bin_dir, state = make_env(tmp)
    root = os.path.join(tmp, "opt")

    result = run_install(
        package, env,
        "--root", root,
        "--panel-port", "19103",
        "--secret-path", "entry123",
        "--network", "testnet",
        "--bin-dir", bin_dir,
    )

    check("install.sh 全新安装返回 0", result.returncode == 0,
          (result.stdout or "")[-800:] + (result.stderr or "")[-400:])

    env_file = os.path.join(root, ".env")
    check("生成了 .env", os.path.isfile(env_file), env_file)

    if not os.path.isfile(env_file):
        return None

    mode = stat.S_IMODE(os.stat(env_file).st_mode)
    check("★ .env 权限是 600（里面是明文凭据）", mode == 0o600, oct(mode))

    values = read_env(env_file)

    check(
        "★ 凭据是随机生成的（不是示例值/占位符）",
        len(values.get("MCLINK_DB_PASSWORD", "")) >= 12
        and values.get("MCLINK_DB_PASSWORD") not in ("CHANGE_ME_REDACTED", "mclink")
        and len(values.get("MCLINK_ADMIN_API_TOKEN", "")) >= 24,
        f"db_len={len(values.get('MCLINK_DB_PASSWORD', ''))} "
        f"token_len={len(values.get('MCLINK_ADMIN_API_TOKEN', ''))}",
    )

    check(
        "面板端口 / 安全入口进了 .env",
        values.get("MCLINK_PANEL_PORT") == "19103"
        and values.get("MCLINK_PANEL_SECRET_PATH") == "entry123",
        f"{values.get('MCLINK_PANEL_PORT')} / "
        f"{values.get('MCLINK_PANEL_SECRET_PATH')}",
    )

    containers = container_state(state)

    check(
        "★ 没有现成 MySQL 时用 compose 起了一个",
        "mclink-db" in containers
        and bool(containers["mclink-db"]["env"].get("MYSQL_ROOT_PASSWORD")),
        str(sorted(containers)),
    )

    ms = mysql_state(state)

    check(
        "★ 建了库、建了 mclink 账号，密码 == .env 里的值",
        "mclink" in ms["databases"]
        and ms["users"].get("mclink@%") == values.get("MCLINK_DB_PASSWORD"),
        f"users={ms['users']}",
    )

    panel = read(os.path.join(root, "panel", "docker-compose.yml"))

    check(
        "★ 面板 compose：凭据是 ${...} 引用，且**不含** .env 的真值（D5）",
        f'MCLINK_DB_PASSWORD: "${{MCLINK_DB_PASSWORD:?' in panel
        and f'MCLINK_ADMIN_API_TOKEN: "${{MCLINK_ADMIN_API_TOKEN:?' in panel
        and values["MCLINK_DB_PASSWORD"] not in panel
        and values["MCLINK_ADMIN_API_TOKEN"] not in panel,
        f"db_pw_len={len(values['MCLINK_DB_PASSWORD'])} "
        f"token_len={len(values['MCLINK_ADMIN_API_TOKEN'])}",
    )

    check(
        "面板 compose：数据库指向新建的容器",
        f'MCLINK_DB_HOST: "mclink-db"' in panel,
    )

    check(
        "★ 面板监听端口按参数改掉（127.0.0.1:19103:80）",
        '127.0.0.1:19103:80' in panel,
        [ln.strip() for ln in panel.splitlines() if ":80" in ln][:3],
    )

    check(
        "安全入口进了面板 compose（面板 M3 直接读这个环境变量）",
        'MCLINK_PANEL_SECRET_PATH: "entry123"' in panel,
    )

    relay = read(os.path.join(root, "server", "docker-compose.yml"))

    check(
        "中继 compose：凭据是 ${...} 引用，且不含真值（与面板同一个变量名）",
        f'MCLINK_DB_PASSWORD: "${{MCLINK_DB_PASSWORD:?' in relay
        and f'MCLINK_ADMIN_API_TOKEN: "${{MCLINK_ADMIN_API_TOKEN:?' in relay
        and values["MCLINK_DB_PASSWORD"] not in relay
        and values["MCLINK_ADMIN_API_TOKEN"] not in relay,
        "两个 compose 引用同名变量 -> 面板与中继永远一致",
    )

    check(
        "两个容器都起来了",
        "mc-relay" in containers and "mc-panel" in containers,
        str(sorted(containers)),
    )

    check(
        "Docker 网络被创建",
        "testnet" in load(os.path.join(state, "networks.json"), []),
    )

    link = os.path.join(bin_dir, "mclink")

    check(
        "★ 管理台快捷命令已注册（软链 -> 安装根目录里的脚本）",
        os.path.islink(link)
        and os.path.realpath(link)
        == os.path.realpath(os.path.join(root, "scripts", "mclink-console.sh")),
        f"{link} -> {os.path.realpath(link) if os.path.islink(link) else '?'}",
    )

    return {"root": root, "env_file": env_file, "env": env, "state": state,
            "bin_dir": bin_dir, "package": package, "values": values}


# ============================================================
# 2. 已有 MySQL：复用，不新建
# ============================================================

def scenario_existing_db(tmp):
    print("")
    print("---- 2. 已有 MySQL 容器：直接复用（绝不新建）----")

    package = make_package(tmp)
    env, bin_dir, state = make_env(tmp)
    root = os.path.join(tmp, "opt-existing")

    seed_containers(state, {
        "web-mysql80": {
            "image": "mysql:8.0.46",
            "env": {"MYSQL_ROOT_PASSWORD": "rootpw-keep", "MYSQL_DATABASE": "mclink"},
            "running": False,        # 故意是停着的：安装器应当 start 它，而不是新建
            "networks": ["1panel-network"],
            "volumes": ["mysql-data"],
            "ports": "",
        },
        "web-php82": {
            "image": "1panel/php:8.2",
            "env": {}, "running": True, "networks": ["1panel-network"],
            "volumes": [], "ports": "",
        },
    })

    # 库里已经有数据（marker：重复跑/复用都不许把它弄丢）。
    seed_mysql(state, {
        "databases": ["mclink"],
        "users": {"mclink@%": "old-password"},
        "grants": ["mclink@%->mclink"],
        "rows": 42,
        "marker": "keep-me",
    })

    result = run_install(
        package, env,
        "--root", root,
        "--db-container", "web-mysql80",
        "--network", "1panel-network",
        "--panel-port", "19102",
        "--secret-path", "reuseentry",
        "--bin-dir", bin_dir,
        "--no-start",
    )

    check("install.sh 复用已有 DB 返回 0", result.returncode == 0,
          (result.stdout or "")[-800:] + (result.stderr or "")[-400:])

    containers = container_state(state)

    check(
        "★ 没有新建 MySQL 容器（还是那两个）",
        sorted(containers) == ["web-mysql80", "web-php82"],
        str(sorted(containers)),
    )

    check(
        "★ 停着的已有容器被 start，而不是重建",
        containers.get("web-mysql80", {}).get("running") is True
        and "docker start web-mysql80" in docker_calls(state),
        "docker start web-mysql80 已执行",
    )

    values = read_env(os.path.join(root, ".env")) if os.path.isfile(
        os.path.join(root, ".env")) else {}

    check(
        ".env 指向被复用的容器（MCLINK_DB_HOST=web-mysql80）",
        values.get("MCLINK_DB_HOST") == "web-mysql80",
        str(values.get("MCLINK_DB_HOST")),
    )

    ms = mysql_state(state)

    check(
        "★ mclink 账号密码被改成 .env 里那个新随机值（ALTER 路径）",
        ms["users"].get("mclink@%") == values.get("MCLINK_DB_PASSWORD")
        and ms["users"].get("mclink@%") != "old-password",
        f"db={ms['users'].get('mclink@%')} env={values.get('MCLINK_DB_PASSWORD')}",
    )

    check(
        "★ 库里的数据没被动过（marker 还在、库还在）",
        ms.get("marker") == "keep-me" and "mclink" in ms["databases"],
        str(ms.get("marker")),
    )

    # 再跑一次：幂等
    before_env = read(os.path.join(root, ".env"))
    before_state = json.dumps(container_state(state), sort_keys=True)
    before_panel = read(os.path.join(root, "panel", "docker-compose.yml"))

    result2 = run_install(
        package, env,
        "--root", root,
        "--db-container", "web-mysql80",
        "--network", "1panel-network",
        "--panel-port", "19102",
        "--secret-path", "reuseentry",
        "--bin-dir", bin_dir,
        "--no-start",
    )

    check("★ 重复跑第二次仍然返回 0（幂等）", result2.returncode == 0,
          (result2.stdout or "")[-600:])

    check(
        "★★ 重复跑不动凭据（.env 逐字节不变）",
        read(os.path.join(root, ".env")) == before_env,
        "第二次跑完 .env 与第一次逐字节相同",
    )

    check(
        "★ 重复跑不动容器、不动库里的账号",
        json.dumps(container_state(state), sort_keys=True) == before_state
        and mysql_state(state)["users"].get("mclink@%")
        == values.get("MCLINK_DB_PASSWORD"),
    )

    check(
        "重复跑不改 compose（值都一样 -> 不该有变化）",
        read(os.path.join(root, "panel", "docker-compose.yml")) == before_panel,
    )

    # ---- --rotate-secrets：这是**唯一**允许换掉已有真值的路径 ----
    old_password = values.get("MCLINK_DB_PASSWORD")
    old_token = values.get("MCLINK_ADMIN_API_TOKEN")

    result3 = run_install(
        package, env,
        "--root", root,
        "--db-container", "web-mysql80",
        "--network", "1panel-network",
        "--panel-port", "19102",
        "--secret-path", "reuseentry",
        "--bin-dir", bin_dir,
        "--no-start",
        "--rotate-secrets",
    )

    rotated = read_env(os.path.join(root, ".env"))
    ms = mysql_state(state)

    check(
        "★ --rotate-secrets 真的换了密码，而且库与 .env 同步换",
        result3.returncode == 0
        and rotated.get("MCLINK_DB_PASSWORD") not in (old_password, "")
        and rotated.get("MCLINK_ADMIN_API_TOKEN") not in (old_token, "")
        and ms["users"].get("mclink@%") == rotated.get("MCLINK_DB_PASSWORD"),
        f"rc={result3.returncode} "
        f"db={ms['users'].get('mclink@%')} env={rotated.get('MCLINK_DB_PASSWORD')}",
    )

    check(
        "★ 轮换后两个 compose 仍是引用、且不含新值（值只在 .env 里）",
        'MCLINK_DB_PASSWORD: "${MCLINK_DB_PASSWORD:?'
        in read(os.path.join(root, "server", "docker-compose.yml"))
        and 'MCLINK_DB_PASSWORD: "${MCLINK_DB_PASSWORD:?'
        in read(os.path.join(root, "panel", "docker-compose.yml"))
        and rotated["MCLINK_DB_PASSWORD"]
        not in read(os.path.join(root, "server", "docker-compose.yml"))
        and rotated["MCLINK_DB_PASSWORD"]
        not in read(os.path.join(root, "panel", "docker-compose.yml")),
    )

    return {"state": state, "root": root}


# ============================================================
# 3. 必须失败的路径
# ============================================================

def scenario_failures(tmp):
    print("")
    print("---- 3. 失败即停：读不到 root 凭据就不猜、不写 .env ----")

    package = make_package(tmp)
    env, bin_dir, state = make_env(tmp)
    root = os.path.join(tmp, "opt-fail")

    # 一个没有 MYSQL_ROOT_PASSWORD 的 MySQL 容器（例如手工起的、密码在
    # 配置文件里）。安装器不许猜密码，更不许先把密码写进 .env。
    seed_containers(state, {
        "mystery-db": {
            "image": "mariadb:11",
            "env": {"MARIADB_DATABASE": "mclink"},
            "running": True, "networks": [], "volumes": [], "ports": "",
        },
    })

    result = run_install(
        package, env,
        "--root", root,
        "--db-container", "mystery-db",
        "--bin-dir", bin_dir,
        "--no-start",
    )

    check("读不到 root 凭据时安装器**失败**（不是硬着头皮往下走）",
          result.returncode != 0, f"returncode={result.returncode}")

    check(
        "说清了原因（root 密码），而不是一句 'failed'",
        "root" in (result.stdout + result.stderr)
        and "密码" in (result.stdout + result.stderr),
        (result.stdout or "")[-300:],
    )

    env_file = os.path.join(root, ".env")

    check(
        "★ 失败时**没有**写 .env（不留半装状态）",
        not os.path.isfile(env_file),
        f"{env_file} 不存在（符合预期）",
    )

    check(
        "失败时没有偷偷建库/建账号",
        not mysql_state(state)["users"],
        str(mysql_state(state)["users"]),
    )

    # 多个 MySQL 容器 + 没指定 -> 不猜，直接停下问用户
    package2 = make_package(os.path.join(tmp, "two"))
    env2, bin_dir2, state2 = make_env(os.path.join(tmp, "two"))
    root2 = os.path.join(tmp, "opt-two")

    seed_containers(state2, {
        "mysql-a": {"image": "mysql:8.0", "env": {"MYSQL_ROOT_PASSWORD": "a"},
                    "running": True, "networks": [], "volumes": [], "ports": ""},
        "mysql-b": {"image": "mariadb:11", "env": {"MARIADB_ROOT_PASSWORD": "b"},
                    "running": True, "networks": [], "volumes": [], "ports": ""},
    })

    result = run_install(package2, env2, "--root", root2,
                         "--bin-dir", bin_dir2, "--no-start")

    check(
        "★ 探测到多个 MySQL 又不指定 -> 不猜，要求 --db-container",
        result.returncode != 0
        and "--db-container" in (result.stdout + result.stderr),
        f"rc={result.returncode}",
    )


# ============================================================
# 4. 管理台（root-only + 审计 + 动作）
# ============================================================

def scenario_console(tmp, fresh):
    print("")
    print("---- 4. SSH 管理台：root-only / 审计 / 每个动作 ----")

    if fresh is None:
        check("管理台测试（需要先完成全新安装）", False, "场景 1 没跑起来")
        return

    root = fresh["root"]
    console = os.path.join(root, "scripts", "mclink-console.sh")
    env_file = os.path.join(root, ".env")
    values = read_env(env_file)
    audit_log = os.path.join(root, "logs", "mclink-console.log")

    # ---- 非 root 必须被挡住（当前进程不是 root 时才测）----
    if os.geteuid() != 0:
        plain_env = dict(os.environ)
        result = run(["bash", console, "status"], plain_env)

        check(
            "★ 非 root 运行管理台被拒绝（免密码的前提是 root-only）",
            result.returncode != 0 and "root" in (result.stdout + result.stderr),
            f"rc={result.returncode} out={(result.stdout or '')[:120]}",
        )
    else:
        print("  [SKIP] 当前就是 root，跳过'非 root 被拒'那一条")

    # ---- 伪装 root（id shim）后跑真正的动作 ----
    env, bin_dir, state = make_env(tmp, fake_root=True)
    link = os.path.join(fresh["bin_dir"], "mclink")

    # 管理台要能找到 docker（假的那个）与 python3
    console_env = dict(env)

    result = run([link, "status"], console_env)

    check("伪装 root 后 status 能跑（返回 0）", result.returncode == 0,
          (result.stdout or "")[-400:] + (result.stderr or "")[-200:])

    result = run([link, "show-secrets"], console_env)

    check(
        "★ show-secrets 打印 .env 里的凭据（root 专属）",
        result.returncode == 0
        and values["MCLINK_DB_PASSWORD"] in result.stdout,
        (result.stdout or "")[:200],
    )

    result = run([link, "reset-entry", "newentry42"], console_env)

    check("reset-entry 返回 0", result.returncode == 0,
          (result.stdout or "")[-300:])

    after = read_env(env_file)

    check(
        "★ 安全入口真的被换掉（.env + 面板 compose 两处一致）",
        after.get("MCLINK_PANEL_SECRET_PATH") == "newentry42"
        and 'MCLINK_PANEL_SECRET_PATH: "newentry42"'
        in read(os.path.join(root, "panel", "docker-compose.yml")),
        f"env={after.get('MCLINK_PANEL_SECRET_PATH')}",
    )

    result = run([link, "unbind-domain"], console_env)

    check(
        "unbind-domain 返回 0 且把域名清空",
        result.returncode == 0
        and read_env(env_file).get("MCLINK_PANEL_DOMAIN") == "",
        (result.stdout or "")[-200:],
    )

    result = run([link, "reset-sessions"], console_env)

    check(
        "reset-sessions 返回 0（清掉面板会话文件）",
        result.returncode == 0
        and "EXEC-SH" in docker_calls(state),
        (result.stdout or "")[-200:],
    )

    # ---- 审计 ----
    result = run([link, "audit", "50"], console_env)

    audit_text = ""
    if os.path.isfile(audit_log):
        audit_text = read(audit_log)

    check("★ 每个动作都写进了审计日志", os.path.isfile(audit_log)
          and "reset-entry" in audit_text
          and "unbind-domain" in audit_text
          and "reset-sessions" in audit_text
          and "show-secrets" in audit_text,
          audit_text[-300:])

    check(
        "审计日志带时间戳与 uid",
        "uid=0" in audit_text and "action=" in audit_text,
        audit_text[-160:],
    )

    check(
        "★ 审计日志**不泄露**凭据（不能把密码抄进去）",
        values["MCLINK_DB_PASSWORD"] not in audit_text
        and values["MCLINK_ADMIN_API_TOKEN"] not in audit_text,
        f"审计日志 {len(audit_text.splitlines())} 行，无凭据字样",
    )

    check(
        "audit 子命令能看日志",
        result.returncode == 0 and "reset-entry" in result.stdout,
        (result.stdout or "")[-200:],
    )

    # ---- 菜单（不带参数就是菜单式；选 6 看凭据，再选 0 退出）----
    result = run([link], console_env, stdin="6\n0\n")

    check(
        "★ 菜单式入口：选 6 打印凭据、选 0 正常退出",
        result.returncode == 0
        and "1) 重置面板安全入口" in result.stdout
        and values["MCLINK_DB_PASSWORD"] in result.stdout
        and "退出。" in result.stdout,
        (result.stdout or "")[-200:],
    )


# ============================================================
# main
# ============================================================

def main():
    print("=" * 70)
    print(" M5：安装器（全新 / 已有 DB / 重复跑）+ SSH 管理台")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="craftlink-m5-") as tmp:
        fresh = None

        for name, fn in (
            ("fresh", scenario_fresh),
            ("existing", scenario_existing_db),
            ("failure", scenario_failures),
        ):
            try:
                result = fn(os.path.join(tmp, name))

                if name == "fresh":
                    fresh = result

                print(f"  [i] {name} 场景跑完")
            except Exception as e:  # noqa: BLE001
                import traceback

                check(f"{name} 场景没有抛异常", False, f"{e.__class__.__name__}: {e}")
                traceback.print_exc()

        try:
            scenario_console(os.path.join(tmp, "console"), fresh)
        except Exception as e:  # noqa: BLE001
            import traceback

            check("管理台场景没有抛异常", False, f"{e.__class__.__name__}: {e}")
            traceback.print_exc()

    print("")
    print("=" * 70)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 70)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
