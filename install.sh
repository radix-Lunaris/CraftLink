#!/usr/bin/env bash
# ============================================================
#  CraftLink 服务端安装器（M5）
# ============================================================
#
#  用法：把整个包上传到**任意目录**，然后
#
#      bash install.sh
#
#  它会：
#    1. 探测本机 Docker 里已有的 MySQL / MariaDB：**有就直接复用**
#       （读容器里的 root 凭据，建库建账号），没有才用 compose 起一个；
#    2. 交互确认：安装根目录（默认 /opt/mc-link，可自定义）、面板监听端口、
#       URL 安全入口；
#    3. 生成 .env（权限 600）—— 凭据**优先沿用线上已有的**（老部署升级：先看
#       .env，再看运行中的 mc-relay / mc-panel 容器环境，D5「一次性升级时保留」），
#       两处都没有才**本机随机生成**（不是示例值）；
#    4. 让部署副本里的两个 compose 用上这台机器的设置（凭据只写 `${VAR:?...}`
#       引用，真值永远不落进 compose）；
#    5. 构建并启动中继 + 面板容器，最后打印访问方式。
#
#  三条纪律：
#    * **幂等**：重复跑不会换掉已有凭据、不会重建数据库、不会动已有数据；
#    * **失败即停**：任何一步失败都立刻退出，并说清"哪一步、为什么、怎么继续"，
#      绝不带着半装状态往下走；
#    * **路径从脚本自身位置推导**（D8）：源码里没有任何写死的安装路径，
#      `/opt/mc-link` 只是**默认值**，`MCLINK_ROOT` / `--root` 都能覆盖。
#
#  非交互（自动化 / 测试）：
#
#      bash install.sh --yes --root /opt/mc-link --panel-port 19102 \
#           --secret-path my-entry --db-container web-mysql80
#
#  常用参数见 `bash install.sh --help`。
# ============================================================

set -u

APP_NAME="CraftLink 服务端安装器"

#: 默认安装根目录。**只是默认值** —— 交互里可以改，--root / MCLINK_ROOT 也能改。
#: 想换掉"默认的默认值"，设 MCLINK_DEFAULT_ROOT。
DEFAULT_ROOT="${MCLINK_DEFAULT_ROOT:-/opt/mc-link}"

#: 面板默认端口（= 现在线上那套；只绑 127.0.0.1，外面由面板自带 nginx 直听）。
DEFAULT_PANEL_PORT="${MCLINK_PANEL_PORT:-19102}"

#: 中继对外端口（协议端口，安装器不改它，只在汇总里打印）。
RELAY_PORT="${MCLINK_RELAY_PORT:-19100}"

# ------------------------------------------------------------------
# 脚本自身位置（D8：一切路径从它推导）
# ------------------------------------------------------------------
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"

while [ -L "$SCRIPT_PATH" ]; do
    _target="$(readlink "$SCRIPT_PATH")"

    case "$_target" in
        /*) SCRIPT_PATH="$_target" ;;
        *)  SCRIPT_PATH="$(dirname "$SCRIPT_PATH")/$_target" ;;
    esac
done

SRC_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd)"

if [ -z "$SRC_DIR" ]; then
    echo "[x] 推导不出脚本所在目录（$SCRIPT_PATH）" >&2
    exit 1
fi

# ------------------------------------------------------------------
# 参数 / 环境变量（环境变量先取，命令行覆盖）
# ------------------------------------------------------------------
OPT_ROOT="${MCLINK_ROOT:-}"
OPT_PANEL_PORT="${MCLINK_PANEL_PORT:-}"
OPT_SECRET_PATH="${MCLINK_PANEL_SECRET_PATH:-}"
OPT_DB_CONTAINER="${MCLINK_DB_CONTAINER:-}"
OPT_DB_ROOT_PASSWORD="${MCLINK_DB_ROOT_PASSWORD:-}"
OPT_BIN_DIR="${MCLINK_BIN_DIR:-/usr/local/bin}"
OPT_NETWORK="${MCLINK_DOCKER_NETWORK:-}"
OPT_YES="${MCLINK_ASSUME_YES:-0}"
OPT_NO_START=0
OPT_NO_CONSOLE=0
OPT_FORCE_ENV=0
OPT_ROTATE=0

usage () {
    cat <<EOF
$APP_NAME

用法：bash install.sh [选项]

  --root DIR            安装根目录（默认 $DEFAULT_ROOT；MCLINK_ROOT）
  --panel-port N        面板监听端口（默认 $DEFAULT_PANEL_PORT；MCLINK_PANEL_PORT）
  --secret-path PATH    URL 安全入口（默认随机生成；MCLINK_PANEL_SECRET_PATH）
  --db-container NAME   指定复用的 MySQL/MariaDB 容器（默认自动探测）
  --db-root-password P  MySQL root 密码（默认从容器里读；读不到才问）
  --network NAME        Docker 网络名（默认 mclink-net；MCLINK_DOCKER_NETWORK）
  --bin-dir DIR         管理台快捷命令装到哪（默认 /usr/local/bin）
  -y, --yes             不交互，全部用默认值 / 参数（MCLINK_ASSUME_YES=1）
  --rotate-secrets      强制换掉 .env 里的凭据（默认：已有真值绝不动）
  --force-env           同 --rotate-secrets（写 .env 时覆盖已有值）
  --no-console          不安装 mclink 管理台
  --no-start            只做准备工作（探测数据库 / 写 .env / 打补丁 / 装管理台），
                        不 build/up 容器。排障与自动化测试用。
  -h, --help            显示这段帮助

凭据覆盖（默认随机生成，走环境变量则用你给的值）：
  MCLINK_DB_PASSWORD / MCLINK_ADMIN_API_TOKEN
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --root)             OPT_ROOT="${2:-}"; shift 2 ;;
        --root=*)           OPT_ROOT="${1#*=}"; shift ;;
        --panel-port)       OPT_PANEL_PORT="${2:-}"; shift 2 ;;
        --panel-port=*)     OPT_PANEL_PORT="${1#*=}"; shift ;;
        --secret-path)      OPT_SECRET_PATH="${2:-}"; shift 2 ;;
        --secret-path=*)    OPT_SECRET_PATH="${1#*=}"; shift ;;
        --db-container)     OPT_DB_CONTAINER="${2:-}"; shift 2 ;;
        --db-container=*)   OPT_DB_CONTAINER="${1#*=}"; shift ;;
        --db-root-password) OPT_DB_ROOT_PASSWORD="${2:-}"; shift 2 ;;
        --db-root-password=*) OPT_DB_ROOT_PASSWORD="${1#*=}"; shift ;;
        --network)          OPT_NETWORK="${2:-}"; shift 2 ;;
        --network=*)        OPT_NETWORK="${1#*=}"; shift ;;
        --bin-dir)          OPT_BIN_DIR="${2:-}"; shift 2 ;;
        --bin-dir=*)        OPT_BIN_DIR="${1#*=}"; shift ;;
        -y|--yes)           OPT_YES=1; shift ;;
        --rotate-secrets|--force-env) OPT_ROTATE=1; OPT_FORCE_ENV=1; shift ;;
        --no-console)       OPT_NO_CONSOLE=1; shift ;;
        --no-start)         OPT_NO_START=1; shift ;;
        -h|--help)          usage; exit 0 ;;
        *)
            echo "[x] 不认识的参数：$1（用 --help 看用法）" >&2
            exit 2
            ;;
    esac
done

# ------------------------------------------------------------------
# 输出 / 日志
# ------------------------------------------------------------------
LOG_FILE=""
STEP_NAME=""

log_to_file () {
    [ -n "$LOG_FILE" ] || return 0
    printf '%s\n' "$*" >> "$LOG_FILE" 2>/dev/null || true
}

say () {
    printf '%s\n' "$*"
    log_to_file "$*"
}

step () {
    STEP_NAME="$1"
    say ""
    say "▶ $1"
    log_to_file "===== $1 ====="
}

ok_step () {
    say "✔ ${1:-$STEP_NAME}"
}

warn () {
    say "  [!] $*"
}

die () {
    # $1 = 哪一步失败；$2.. = 原因（可多行）
    local where="$1"; shift
    local reason="$*"

    say ""
    say "✘ $where"
    [ -n "$reason" ] && say "  $reason"

    say ""
    say "安装**没有完成**，停在这里（不会继续往下做）。"
    say "已经做过的步骤都是幂等的：修好上面的原因后**重跑一次**即可，"
    say "已有的 .env 凭据与数据库数据不会被覆盖。"
    [ -n "$LOG_FILE" ] && say "完整日志：$LOG_FILE"

    exit 1
}

gen_secret () {   # $1 = 长度
    python3 - "$1" <<'PY'
import secrets
import sys

# 去掉容易看错/念错的字符（这些值要被人抄下来、念出来）。
alphabet = "".join(
    c for c in "abcdefghjkmnprstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ2345678"
    if c not in "0oO1iIlLq9g"
)

print("".join(secrets.choice(alphabet) for _ in range(int(sys.argv[1]))))
PY
}

json_field () {   # $1 = json 文件, $2 = 字段名（顶层）
    python3 - "$1" "$2" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    data = {}

value = data.get(sys.argv[2])

print("" if value is None else value)
PY
}

json_mysql_names () {   # $1 = json 文件 -> 一行一个容器名
    python3 - "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    data = {}

for item in data.get("mysql") or []:
    name = str(item.get("name") or "").strip()

    if name:
        print(name)
PY
}

compose_container_name () {   # $1 = compose 文件, $2 = 兜底名
    local found
    found=$(grep -m1 -E '^[[:space:]]*container_name:[[:space:]]*' "$1" 2>/dev/null \
        | sed -E 's/^[^:]*:[[:space:]]*//; s/^["'"'"']//; s/["'"'"']$//')

    printf '%s\n' "${found:-$2}"
}

# ------------------------------------------------------------------
# 0. 前置检查
# ------------------------------------------------------------------
require_basics () {
    command -v docker >/dev/null 2>&1 \
        || die "前置检查" "找不到 docker。请先装 Docker（含 compose 插件）再跑安装器。"

    docker compose version >/dev/null 2>&1 \
        || die "前置检查" "docker compose 不可用（需要 Docker Compose v2：docker compose ...）。"

    command -v python3 >/dev/null 2>&1 \
        || die "前置检查" "找不到 python3（安装器用它生成随机凭据、读写 .env）。"
}

# ------------------------------------------------------------------
# 1. 安装根目录
# ------------------------------------------------------------------
copy_package () {   # $1 = 源目录, $2 = 目标目录
    mkdir -p "$2" || return 1

    # .env / logs / .git 不复制：凭据与运行数据属于"这台机器"，不属于包。
    if command -v tar >/dev/null 2>&1; then
        ( cd "$1" && tar --exclude=./.env --exclude=./logs \
              --exclude=./.git --exclude='./*/__pycache__' -cf - . ) \
            | ( cd "$2" && tar -xf - )
    else
        cp -a "$1/." "$2/"
    fi
}

prepare_root () {
    step "安装根目录"

    local root="$OPT_ROOT"
    local default_hint="$DEFAULT_ROOT"

    if [ -z "$root" ]; then
        if [ "$OPT_YES" = "1" ] || [ ! -t 0 ]; then
            root="$default_hint"
        else
            printf '安装根目录 [%s]: ' "$default_hint"
            read -r root || true
            root="${root:-$default_hint}"
        fi
    fi

    case "$root" in
        /*) ;;
        *) die "安装根目录" "必须是绝对路径：$root" ;;
    esac

    if [ "$root" != "$SRC_DIR" ]; then
        say "  把包复制到 $root ..."
        copy_package "$SRC_DIR" "$root" \
            || die "安装根目录" "复制失败：$SRC_DIR -> $root（权限？磁盘？）"
    else
        say "  包已经在安装根目录里：$root"
    fi

    MC_LINK_ROOT="$(cd "$root" 2>/dev/null && pwd)"

    [ -n "$MC_LINK_ROOT" ] || die "安装根目录" "进不去：$root"

    LOG_FILE="$MC_LINK_ROOT/logs/install.log"
    mkdir -p "$MC_LINK_ROOT/logs" 2>/dev/null || LOG_FILE=""
    [ -n "$LOG_FILE" ] && : >> "$LOG_FILE" 2>/dev/null || LOG_FILE=""

    # 包完整性：缺文件就别往下走（否则会在很后面才炸，且看不出原因）。
    local missing=""

    for rel in server/docker-compose.yml panel/docker-compose.yml \
               docker-compose.db.yml scripts/install_db.py \
               scripts/install_patch.py scripts/mclink-console.sh; do
        [ -f "$MC_LINK_ROOT/$rel" ] || missing="$missing $rel"
    done

    [ -z "$missing" ] || die "安装根目录" "包里缺少文件：$missing"

    ENV_FILE="${MCLINK_ENV_FILE:-$MC_LINK_ROOT/.env}"
    RELAY_DIR="$MC_LINK_ROOT/server"
    PANEL_DIR="$MC_LINK_ROOT/panel"

    ok_step
    say "  根目录 : $MC_LINK_ROOT"
    say "  .env   : $ENV_FILE"
}

# ------------------------------------------------------------------
# 2. 交互确认
# ------------------------------------------------------------------
ask_settings () {
    step "确认安装参数"

    local port="${OPT_PANEL_PORT:-}"
    local secret="${OPT_SECRET_PATH:-}"

    if [ "$OPT_YES" != "1" ] && [ -t 0 ]; then
        printf '面板监听端口 [%s]: ' "$DEFAULT_PANEL_PORT"
        read -r port_in || true
        port="${port_in:-${port:-$DEFAULT_PANEL_PORT}}"

        printf 'URL 安全入口（面板路径，留空=随机生成）[随机]: '
        read -r secret_in || true
        secret="${secret_in:-$secret}"
    fi

    PANEL_PORT="${port:-$DEFAULT_PANEL_PORT}"

    case "$PANEL_PORT" in
        ''|*[!0-9]*) die "确认安装参数" "面板端口必须是数字：$PANEL_PORT" ;;
    esac

    if [ "$PANEL_PORT" -lt 1 ] || [ "$PANEL_PORT" -gt 65535 ]; then
        die "确认安装参数" "面板端口超出范围 1-65535：$PANEL_PORT"
    fi

    # URL 安全入口：只允许 URL 里安全的字符（它会被当成路径前缀）。
    PANEL_SECRET_PATH="${secret:-$(gen_secret 16)}"
    PANEL_SECRET_PATH="${PANEL_SECRET_PATH#/}"

    case "$PANEL_SECRET_PATH" in
        *[!A-Za-z0-9._-]*)
            die "确认安装参数" \
                "URL 安全入口只能用字母/数字/._- ：$PANEL_SECRET_PATH" ;;
    esac

    if [ "${#PANEL_SECRET_PATH}" -lt 3 ]; then
        die "确认安装参数" "URL 安全入口太短（至少 3 位，建议 12 位以上）"
    fi

    # 网络名：默认 mclink-net（全新机器）。已经跑着 1panel-network 的机器由
    # --network / .env 提供，安装器不猜。
    DOCKER_NETWORK="${OPT_NETWORK:-${MCLINK_DOCKER_NETWORK:-mclink-net}}"

    ok_step
    say "  面板端口     : $PANEL_PORT"
    say "  URL 安全入口 : /$PANEL_SECRET_PATH"
    say "  Docker 网络  : $DOCKER_NETWORK"

    if [ "$OPT_YES" != "1" ] && [ -t 0 ]; then
        printf '按回车开始安装（Ctrl-C 取消）... '
        read -r _confirm || true
    fi
}

# ------------------------------------------------------------------
# 3. 数据库：探测 -> 复用 / 新建 -> 建库建账号
# ------------------------------------------------------------------
detect_database () {
    step "探测本机已有的 MySQL / MariaDB 容器"

    DETECT_JSON="$MC_LINK_ROOT/logs/detect.json"
    chmod 700 "$MC_LINK_ROOT/logs" 2>/dev/null || true

    local args=()

    if [ -n "$OPT_DB_CONTAINER" ]; then
        args=(--container "$OPT_DB_CONTAINER")
    fi

    python3 "$MC_LINK_ROOT/scripts/install_db.py" detect "${args[@]}" \
        > "$DETECT_JSON" 2>/dev/null

    if [ ! -s "$DETECT_JSON" ]; then
        die "探测数据库" "install_db.py detect 没有输出（脚本被改坏了？）"
    fi

    if [ "$(json_field "$DETECT_JSON" docker)" != "True" ]; then
        die "探测数据库" "docker 不可用：$(json_field "$DETECT_JSON" error)"
    fi

    DB_CONTAINER="$(json_field "$DETECT_JSON" selected)"
    DB_FOUND=0

    if [ -n "$DB_CONTAINER" ]; then
        DB_FOUND=1
        say "  复用已有容器：$DB_CONTAINER"
    else
        local names
        names="$(json_mysql_names "$DETECT_JSON")"

        if [ -n "$names" ]; then
            # 找到多个但没指定 -> **不猜**（D4：猜错就是连到别人的库）
            say "  发现多个 MySQL/MariaDB 容器，需要你指定一个："
            printf '    %s\n' $names
            die "探测数据库" \
                "用 --db-container <名字> 指定要复用哪一个（安装器不替你猜）。"
        fi

        say "  没有找到现成的 MySQL/MariaDB 容器 —— 稍后用 compose 起一个。"
    fi

    ok_step
}

# ------------------------------------------------------------------
# 3b. D5：老部署升级时**保留**线上凭据（读进 .env）
# ------------------------------------------------------------------
#
# 用户线上那套是 v3.9.20 的**硬编码**部署：两个 compose 里写着明文，同一份值
# 也活在运行中的容器环境里（`mc-relay` / `mc-panel` 的 Config.Env）。D5 要求
# "一次性升级时保留、便于平滑迁移"，所以这里按下面的优先级把值收进 .env：
#
#     已有的 .env 真值  >  运行中的 mc-relay / mc-panel 容器环境  >  （后面）随机生成
#
#  不这么做的话：老机器上没有 .env（硬编码模式不需要它），install_db.py 会
#  生成一串新密码并 `ALTER USER` 改掉库里的账号 —— 能跑通，但那是一次没必要的
#  轮换，而且任何**没跟着重建**的东西（另一台机器上的面板、备份脚本）会突然
#  连不上。读出来写进 .env 之后，数据库密码一个字符都不用改。
#
#  三条纪律：
#    * 已经有真值的 .env **一个字都不动**（幂等，绝不覆盖线上值）；
#    * 读不到（全新机器 / 容器没跑 / 没权限）**不是错误** —— 交给后面生成随机值；
#    * **不打印值**，只说"从哪读到了哪个键"。
adopt_credentials_from_running () {
    step "读取线上凭据（D5：老部署升级时保留）"

    local key value missing=0
    local -a pairs=()

    for key in MCLINK_DB_PASSWORD MCLINK_ADMIN_API_TOKEN; do
        value=""

        if [ -f "$ENV_FILE" ]; then
            value=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null \
                | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
        fi

        # 空的 / 明确占位符 / 中文占位符 都算"还没有真值"
        case "$value" in
            ""|CHANGE_ME_REDACTED|CHANGE_ME|changeme|TODO|todo) value="" ;;
        esac

        if [ -n "$value" ] \
            && printf '%s' "$value" | LC_ALL=C grep -q '[^ -~]'; then
            value=""
        fi

        if [ -n "$value" ]; then
            say "  $key：沿用 $ENV_FILE 里已有的值"
            continue
        fi

        missing=$((missing + 1))
    done

    if [ "$missing" = "0" ]; then
        ok_step
        say "  .env 里两个凭据都已是真值 —— 保持不动（幂等）"
        return 0
    fi

    # ---- 从运行中的容器读（老部署的 compose 把值直接写进 environment）----
    local container env_dump found_keys=""

    for container in \
        "$(compose_container_name "$RELAY_DIR/docker-compose.yml" mc-relay)" \
        "$(compose_container_name "$PANEL_DIR/docker-compose.yml" mc-panel)"; do
        [ -n "$container" ] || continue

        env_dump=$(docker inspect "$container" \
            --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null) || true
        [ -n "$env_dump" ] || continue

        for key in MCLINK_DB_PASSWORD MCLINK_ADMIN_API_TOKEN; do
            # 这个键已经有值了（中继读到了、面板也有）就不重复
            case "$found_keys" in *"|${key}|"*) continue ;; esac

            value=$(printf '%s\n' "$env_dump" \
                | grep -E "^${key}=" | head -1 | cut -d= -f2-)

            case "$value" in
                ""|CHANGE_ME_REDACTED|CHANGE_ME|changeme|TODO|todo) value="" ;;
            esac

            if [ -n "$value" ] \
                && printf '%s' "$value" | LC_ALL=C grep -q '[^ -~]'; then
                value=""
            fi

            [ -n "$value" ] || continue

            pairs+=("${key}=${value}")
            found_keys="${found_keys}|${key}|"
            say "  $key：从运行中的容器 $container 读到（写进 $ENV_FILE，库里的值不动）"
        done
    done

    if [ "${#pairs[@]}" -eq 0 ]; then
        ok_step
        say "  没有可沿用的线上凭据（全新机器）—— 稍后随机生成"
        return 0
    fi

    # env-set 默认**不覆盖**已有真值；这里写入的正是"还缺的那些键"。
    if printf '%s\n' "${pairs[@]}" | python3 \
            "$MC_LINK_ROOT/scripts/install_db.py" env-set \
            --env "$ENV_FILE" >/dev/null; then
        chmod 600 "$ENV_FILE" 2>/dev/null || true
        ok_step
        say "  已写入 $ENV_FILE（权限 600）—— 老部署的数据库密码**没有被改**"
    else
        warn "线上凭据没能写进 $ENV_FILE —— 后面按『没有真值』处理（会重新生成）"
    fi

    return 0
}

ensure_database_container () {
    if [ "$DB_FOUND" = "1" ]; then
        # 已停止的：起起来再用（不新建容器）。
        local running
        running="$(docker inspect "$DB_CONTAINER" --format '{{.State.Running}}' 2>/dev/null)"

        if [ "$running" != "true" ]; then
            say "  容器是停止状态，先启动它（不新建）：$DB_CONTAINER"
            docker start "$DB_CONTAINER" >/dev/null 2>&1 \
                || die "启动已有 MySQL 容器" "docker start $DB_CONTAINER 失败"
        fi

        return 0
    fi

    step "起一个 MySQL 容器（没有现成的可复用）"

    DB_CONTAINER="${MCLINK_DB_CONTAINER:-mclink-db}"

    if [ -z "$OPT_DB_ROOT_PASSWORD" ]; then
        # 只在本机 .env / 环境里没有时生成；生成的值马上写进 .env（600）。
        OPT_DB_ROOT_PASSWORD="$(gen_secret 24)"
        DB_ROOT_PASSWORD_NEW=1
    fi

    MCLINK_DB_ROOT_PASSWORD="$OPT_DB_ROOT_PASSWORD" \
    MCLINK_DB_CONTAINER="$DB_CONTAINER" \
    MCLINK_DB_NAME="${MCLINK_DB_NAME:-mclink}" \
    MCLINK_DOCKER_NETWORK="$DOCKER_NETWORK" \
        docker compose -f "$MC_LINK_ROOT/docker-compose.db.yml" up -d \
        >>"$LOG_FILE" 2>&1 \
        || die "起 MySQL 容器" \
            "docker compose -f docker-compose.db.yml up -d 失败（镜像拉不下来？\
没有网络？看 $LOG_FILE）"

    ok_step
    say "  容器：$DB_CONTAINER（数据在具名卷里，重建容器不丢）"

    # 等它就绪：首次启动要初始化数据目录，通常 10-40 秒。
    local waited=0

    while [ "$waited" -lt 90 ]; do
        if docker exec "$DB_CONTAINER" mysqladmin ping -uroot \
                -p"$OPT_DB_ROOT_PASSWORD" >/dev/null 2>&1; then
            say "  数据库已就绪（${waited}s）"
            return 0
        fi

        sleep 3
        waited=$((waited + 3))
    done

    die "等待 MySQL 就绪" \
        "90 秒还没起来。看 docker logs $DB_CONTAINER 找原因；数据卷没坏的话重跑安装器即可。"
}

provision_database () {
    step "建库 / 建账号（复用 $DB_CONTAINER，不新建容器）"

    local args=(--container "$DB_CONTAINER" --env "$ENV_FILE"
                --database "${MCLINK_DB_NAME:-mclink}"
                --user "${MCLINK_DB_USER:-mclink}")

    if [ -n "$OPT_DB_ROOT_PASSWORD" ]; then
        args+=(--root-password "$OPT_DB_ROOT_PASSWORD")
    fi

    if [ "$OPT_ROTATE" = "1" ]; then
        # --rotate：连 .env 里已有的真值（密码与 token）一起换掉。
        args+=(--rotate --no-backup)
    fi

    # MCLINK_DB_PASSWORD / MCLINK_ADMIN_API_TOKEN 走环境变量传（不进 argv）：
    # 没有就由 install_db.py 生成随机值。provision 成功之后它才会写 .env。
    if ! MCLINK_DB_PASSWORD="${MCLINK_DB_PASSWORD:-}" MCLINK_ADMIN_API_TOKEN="${MCLINK_ADMIN_API_TOKEN:-}" \
         python3 "$MC_LINK_ROOT/scripts/install_db.py" provision "${args[@]}"; then
        die "建库 / 建账号" \
            "数据库那一步失败了（原因见上面几行）。.env **没有**被写坏：\
要么它还不存在，要么里面还是原来的值。"
    fi

    ok_step
}

# ------------------------------------------------------------------
# 4. Docker 网络
# ------------------------------------------------------------------
ensure_network () {
    step "Docker 网络"

    if docker network inspect "$DOCKER_NETWORK" >/dev/null 2>&1; then
        say "  已存在：$DOCKER_NETWORK"
    else
        docker network create "$DOCKER_NETWORK" >/dev/null 2>&1 \
            || die "Docker 网络" "创建失败：$DOCKER_NETWORK"

        say "  已创建：$DOCKER_NETWORK"
    fi

    ok_step
}

# ------------------------------------------------------------------
# 5. .env + compose 补丁
# ------------------------------------------------------------------
write_env_file () {
    step "写 .env（权限 600）"

    local pairs
    pairs=$(cat <<EOF
MCLINK_ROOT=$MC_LINK_ROOT
MCLINK_DB_HOST=$DB_CONTAINER
MCLINK_DB_PORT=${MCLINK_DB_PORT:-3306}
MCLINK_DB_NAME=${MCLINK_DB_NAME:-mclink}
MCLINK_DB_USER=${MCLINK_DB_USER:-mclink}
MCLINK_PANEL_PORT=$PANEL_PORT
MCLINK_PANEL_SECRET_PATH=$PANEL_SECRET_PATH
MCLINK_PANEL_DOMAIN=
MCLINK_DOCKER_NETWORK=$DOCKER_NETWORK
MCLINK_MAX_PLAYERS=${MCLINK_MAX_PLAYERS:-64}
EOF
)

    if [ "${DB_ROOT_PASSWORD_NEW:-0}" = "1" ]; then
        pairs="$pairs
MCLINK_DB_ROOT_PASSWORD=$OPT_DB_ROOT_PASSWORD"
    fi

    printf '%s\n' "$pairs" | python3 "$MC_LINK_ROOT/scripts/install_db.py" \
        env-set --env "$ENV_FILE" --force >/dev/null \
        || die "写 .env" "install_db.py env-set 失败：$ENV_FILE"

    chmod 600 "$ENV_FILE" 2>/dev/null || true

    # 回读校验：600 + 关键键都在（否则后面 compose 会拿到空值）。
    local mode
    mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo "?")

    [ "$mode" = "600" ] || warn ".env 权限是 $mode（期望 600）—— 里面是明文凭据"

    for key in MCLINK_DB_HOST MCLINK_DB_PASSWORD MCLINK_ADMIN_API_TOKEN; do
        grep -q "^${key}=" "$ENV_FILE" || die "写 .env" ".env 里缺 $key"
    done

    ok_step
    say "  $ENV_FILE（权限 $mode）"
}

patch_composes () {
    step "让两个 compose 用上这台机器的设置"

    # ---- 凭据：**只写引用**（D5），真值永远留在 .env ----
    #
    #  v3.9.20 的硬编码模式是把 .env 里的明文值写进这两个 compose；D5 之后不行：
    #  compose 里只允许 `${VAR:?...}`，rebuild.sh 的 check_credentials 会把明文
    #  判成失败（那些值随旧交付包外传过，必须轮换）。
    #
    #  所以这一步的作用变成"**保证**部署副本里是引用、而且变量名与 .env 一致"：
    #    * 新包的 compose 本来就是引用 -> 写进去逐字相同，等于空操作；
    #    * 老部署的 compose 是明文 -> 就地改成引用（这是升级路径的关键一步）。
    #
    #  值不经过 argv、不落进 compose，只以 ${VAR:?...} 的形式出现。
    #  用 --env-file 的路径写提示，运维一眼知道去哪改。
    local cred_refs
    cred_refs=$(cat <<EOF
$(printf %s MCLINK_DB_PASSWORD)=\${MCLINK_DB_PASSWORD:?MISSING: 在 $ENV_FILE 里设置 MCLINK_DB_PASSWORD}
$(printf %s MCLINK_ADMIN_API_TOKEN)=\${MCLINK_ADMIN_API_TOKEN:?MISSING: 在 $ENV_FILE 里设置 MCLINK_ADMIN_API_TOKEN}
EOF
)

    printf '%s\n' "$cred_refs" | python3 \
        "$MC_LINK_ROOT/scripts/install_patch.py" \
        --file "$RELAY_DIR/docker-compose.yml" patch-env >/dev/null \
        || die "写 compose" "server/docker-compose.yml 的凭据引用没写进去"

    # ---- 面板：连接信息 + 凭据引用 + 安全入口 ----
    #
    # MCLINK_DB_HOST/NAME/USER 不是凭据，写成本机实际值（升级老部署时尤其重要：
    # 容器名不一定是 web-mysql80）。
    local panel_env
    panel_env=$(cat <<EOF
MCLINK_DB_HOST=$DB_CONTAINER
MCLINK_DB_NAME=${MCLINK_DB_NAME:-mclink}
MCLINK_DB_USER=${MCLINK_DB_USER:-mclink}
EOF
)

    panel_env="$panel_env
$cred_refs"

    printf '%s\n' "$panel_env" | python3 \
        "$MC_LINK_ROOT/scripts/install_patch.py" \
        --file "$PANEL_DIR/docker-compose.yml" patch-env >/dev/null \
        || die "写 compose" "panel/docker-compose.yml 的连接信息没写进去"

    # 面板 compose 里 **没有** 的键（安全入口/域名）由这一步补进 environment；
    # 面板将来实现 M3/M4 时直接读这两个环境变量即可。
    printf '%s\n' \
        "MCLINK_PANEL_SECRET_PATH=$PANEL_SECRET_PATH" \
        "MCLINK_PANEL_DOMAIN=" \
        | python3 "$MC_LINK_ROOT/scripts/install_patch.py" \
            --file "$PANEL_DIR/docker-compose.yml" patch-env >/dev/null \
        || warn "panel/docker-compose.yml 里没有可插入的 environment 段（安全入口只写进了 .env）"

    # 面板端口：只绑回环，外面是面板自带 nginx。
    python3 "$MC_LINK_ROOT/scripts/install_patch.py" \
        --file "$PANEL_DIR/docker-compose.yml" patch-port --port "$PANEL_PORT" \
        >/dev/null 2>&1 \
        || warn "面板端口没有改动（compose 里的端口行形状变了？见 install_patch.py）"

    ok_step
    say "  server/docker-compose.yml + panel/docker-compose.yml 已同步"
}

# ------------------------------------------------------------------
# 6. 起容器
# ------------------------------------------------------------------
start_services () {
    step "构建并启动中继 + 面板"

    local compose_args=(--env-file "$ENV_FILE")

    ( cd "$RELAY_DIR" && docker compose "${compose_args[@]}" \
        -f docker-compose.yml up -d --build ) >>"$LOG_FILE" 2>&1 \
        || die "启动中继容器" \
            "docker compose up -d --build（server/）失败，详见 $LOG_FILE"

    ( cd "$PANEL_DIR" && docker compose "${compose_args[@]}" \
        -f docker-compose.yml up -d --build ) >>"$LOG_FILE" 2>&1 \
        || die "启动面板容器" \
            "docker compose up -d --build（panel/）失败，详见 $LOG_FILE"

    ok_step
    say "  中继：$(compose_container_name "$RELAY_DIR/docker-compose.yml" mc-relay)"
    say "  面板：$(compose_container_name "$PANEL_DIR/docker-compose.yml" mc-panel)"
}

# ------------------------------------------------------------------
# 7. SSH 管理台（root-only 快捷命令）
# ------------------------------------------------------------------
install_console () {
    if [ "$OPT_NO_CONSOLE" = "1" ]; then
        return 0
    fi

    step "安装 SSH 管理台（$OPT_BIN_DIR/mclink）"

    local console="$MC_LINK_ROOT/scripts/mclink-console.sh"

    chmod +x "$console" 2>/dev/null || true

    if [ ! -w "$OPT_BIN_DIR" ] && [ ! -w "$(dirname "$OPT_BIN_DIR")" ]; then
        warn "没有写权限：$OPT_BIN_DIR —— 跳过（用 root 跑，或 --bin-dir 指到可写目录）"
        return 0
    fi

    mkdir -p "$OPT_BIN_DIR" 2>/dev/null || true

    # 软链而不是拷一份：管理台永远跟着安装根目录里的那份走，升级不需要重装。
    if ln -sfn "$console" "$OPT_BIN_DIR/mclink" 2>/dev/null; then
        ok_step
        say "  $OPT_BIN_DIR/mclink -> $console"
        say "  用法：sudo mclink（只允许 root；菜单式）"
    else
        warn "创建软链失败：$OPT_BIN_DIR/mclink"
    fi
}

# ------------------------------------------------------------------
# 8. 汇总
# ------------------------------------------------------------------
summary () {
    local host_ip
    host_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    host_ip="${host_ip:-<服务器IP>}"

    local relay_name panel_name
    relay_name="$(compose_container_name "$RELAY_DIR/docker-compose.yml" mc-relay)"
    panel_name="$(compose_container_name "$PANEL_DIR/docker-compose.yml" mc-panel)"

    say ""
    say "============================================================"
    say " 安装完成"
    say "============================================================"
    say ""
    say "  安装根目录 : $MC_LINK_ROOT"
    say "  凭据文件   : $ENV_FILE（权限 600，只有 root 能看）"
    say "  数据库     : 容器 $DB_CONTAINER${DB_ROOT_PASSWORD_NEW:+（本次新建，root 密码存在 .env）}"
    say ""
    say "  面板       : http://$host_ip:$PANEL_PORT/$PANEL_SECRET_PATH/"
    say "  中继端口   : $host_ip:$RELAY_PORT（Minecraft 客户端填这个）"
    say "  STUN       : UDP 3478 / 3479（打洞用，安全组要放行）"
    say ""
    say "  容器       : $relay_name / $panel_name"

    if [ "$OPT_NO_CONSOLE" != "1" ]; then
        say "  管理台     : mclink（root-only，免密码；改安全入口/改 root 密码/看日志）"
    fi

    say ""
    say "  下一步："
    say "    * 云安全组放行 TCP $RELAY_PORT 与 UDP 3478/3479"
    say "    * 面板反代/证书：见 docs/DEPLOY_GUIDE.md"
    say "    * 重建/自证    ：bash $MC_LINK_ROOT/rebuild.sh"
    say ""

    log_to_file "INSTALL OK root=$MC_LINK_ROOT db=$DB_CONTAINER panel_port=$PANEL_PORT"
}

# ==================================================================
# main
# ==================================================================
main () {
    say "============================================================"
    say " $APP_NAME"
    say "============================================================"
    say "  包目录   : $SRC_DIR"
    say "  交互模式 : $([ "$OPT_YES" = "1" ] && echo '关（--yes）' || echo '开')"

    require_basics
    prepare_root
    ask_settings
    detect_database
    adopt_credentials_from_running
    ensure_database_container
    provision_database
    ensure_network
    write_env_file
    patch_composes

    if [ "$OPT_NO_START" = "1" ]; then
        warn "--no-start：跳过 docker compose up（配置已经写好，随时可以重跑安装器起容器）"
    else
        start_services
    fi

    install_console
    summary

    return 0
}

main "$@"
