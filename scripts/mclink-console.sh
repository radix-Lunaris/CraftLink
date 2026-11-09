#!/usr/bin/env bash
# ============================================================
#  CraftLink SSH 终极管理台（M5）
# ============================================================
#
#  安装：`bash install.sh` 会把它软链成 /usr/local/bin/mclink（可用 --bin-dir 改）
#        手工安装：ln -sfn <安装根目录>/scripts/mclink-console.sh /usr/local/bin/mclink
#
#  用法：
#      mclink                  菜单式（交互）
#      mclink status           服务状态 + 最近日志
#      mclink show-secrets     显示 .env 里的凭据（root）
#      mclink reset-entry      清空面板 URL 安全入口（被关在外面时用）
#      mclink set-entry [PATH] 设置（不带 PATH 则随机生成）新的安全入口
#      mclink unbind-domain    解绑域名绑定
#      mclink ssl-status       面板设置 / 证书到期时间 / nginx 生效状态
#      mclink ssl-off          关掉面板 HTTPS（证书坏了的第一反应；证书不删）
#      mclink apply-network    把监听地址 / 端口 / SSL 落到宿主机（重建面板容器）
#      mclink rescue           打印本机急救入口地址（127.0.0.1:<急救端口>）
#      mclink reset-root-password   改 root 密码（走中继里的 reset_password.py）
#      mclink reset-sessions   重置面板会话（强制所有人重新登录）
#      mclink audit [N]        看审计日志（默认最后 40 行）
#
#  ============================================================
#   为什么是 root-only + 免密码
#  ============================================================
#
#  这些操作（改安全入口、解绑域名、重置会话、看凭据）本来就是"能登进
#  SSH 的人"才做得了的事 —— 再要一次面板密码只会让人把密码写在便签上。
#  所以：能拿到 root shell 就是授权本身；命令本身不再问密码。
#
#  也正因为免密码，**每一步都要留痕**：所有动作（谁、什么时候、做了什么、
#  结果）都追加进 <安装根目录>/logs/mclink-console.log（0600）。
#  这个文件只增不改，是事后唯一能回答"昨晚谁把安全入口改了"的东西。
#
#  ============================================================
#   路径
#  ============================================================
#
#  脚本在 <安装根目录>/scripts/ 下，所以安装根目录 = 它的上一级。
#  覆盖顺序：MCLINK_ROOT > 脚本位置。不出现任何写死的安装路径。
# ============================================================

set -u

PROG="${0##*/}"

# ------------------------------------------------------------------
# 只允许 root
# ------------------------------------------------------------------
if [ "$(id -u)" != "0" ]; then
    echo "[x] $PROG 只允许 root 运行（当前 uid=$(id -u)）。" >&2
    echo "    需要提权：sudo $PROG" >&2
    exit 1
fi

# ------------------------------------------------------------------
# 安装根目录：从脚本自身位置推导
# ------------------------------------------------------------------
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"

while [ -L "$SCRIPT_PATH" ]; do
    _target="$(readlink "$SCRIPT_PATH")"

    case "$_target" in
        /*) SCRIPT_PATH="$_target" ;;
        *)  SCRIPT_PATH="$(dirname "$SCRIPT_PATH")/$_target" ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" 2>/dev/null && pwd)"
MC_LINK_ROOT="${MCLINK_ROOT:-$(dirname "$SCRIPT_DIR")}"

if [ ! -d "$MC_LINK_ROOT" ]; then
    echo "[x] 安装根目录不存在：$MC_LINK_ROOT" >&2
    exit 1
fi

MC_LINK_ROOT="$(cd "$MC_LINK_ROOT" && pwd)"

ENV_FILE="${MCLINK_ENV_FILE:-$MC_LINK_ROOT/.env}"
RELAY_DIR="$MC_LINK_ROOT/server"
PANEL_DIR="$MC_LINK_ROOT/panel"

AUDIT_LOG="${MCLINK_AUDIT_LOG:-$MC_LINK_ROOT/logs/mclink-console.log}"
mkdir -p "$(dirname "$AUDIT_LOG")" 2>/dev/null || true
: >> "$AUDIT_LOG" 2>/dev/null || AUDIT_LOG=""
[ -n "$AUDIT_LOG" ] && chmod 600 "$AUDIT_LOG" 2>/dev/null || true

# ------------------------------------------------------------------
# 审计
# ------------------------------------------------------------------
audit () {   # $1 = action, $2 = detail(可空)
    [ -n "$AUDIT_LOG" ] || return 0

    printf '%s uid=%s action=%s%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$(id -u)" "$1" \
        "${2:+ detail=$2}" >> "$AUDIT_LOG" 2>/dev/null || true
}

say () {
    printf '%s\n' "$*"
}

warn () {
    printf '  [!] %s\n' "$*"
}

die () {
    say "✘ $*"
    audit "error" "$*"
    exit 1
}

# ------------------------------------------------------------------
# 小工具
# ------------------------------------------------------------------
compose_container_name () {   # $1 = compose 文件, $2 = 兜底
    local found

    found=$(grep -m1 -E '^[[:space:]]*container_name:[[:space:]]*' "$1" 2>/dev/null \
        | sed -E 's/^[^:]*:[[:space:]]*//; s/^["'"'"']//; s/["'"'"']$//')

    printf '%s\n' "${found:-$2}"
}

read_env_value () {   # $1 = KEY
    [ -f "$ENV_FILE" ] || return 0

    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2-
}

set_env_value () {   # $1 = KEY, $2 = VALUE
    [ -f "$ENV_FILE" ] || die "找不到 $ENV_FILE（还没安装？）"

    printf '%s=%s\n' "$1" "$2" \
        | python3 "$MC_LINK_ROOT/scripts/install_db.py" env-set \
            --env "$ENV_FILE" --force >/dev/null \
        || die "写 $ENV_FILE 失败（$1）"
}

patch_compose_env () {   # $1 = compose 文件, $2 = KEY, $3 = VALUE
    [ -f "$1" ] || return 0

    # compose 里没有这个键就跳过（老版本的 compose 可能还没有它）。
    grep -qE "^[[:space:]]*$2[[:space:]]*:" "$1" || return 0

    printf '%s=%s\n' "$2" "$3" \
        | python3 "$MC_LINK_ROOT/scripts/install_patch.py" \
            --file "$1" patch-env >/dev/null 2>&1 \
        || warn "$1 里的 $2 没写进去（面板可能读不到新值）"
}

restart_panel () {
    local panel
    panel="$(compose_container_name "$PANEL_DIR/docker-compose.yml" mc-panel)"

    command -v docker >/dev/null 2>&1 || { warn "没有 docker，跳过重启"; return 1; }

    # `docker restart` 不会重读环境变量；要让新配置生效必须 up -d（配置变了
    # 它会重建容器）。compose 不可用时退回 restart，至少让进程重新起来。
    local env_args=()

    if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
        env_args=(--env-file "$ENV_FILE")
    fi

    if [ -f "$PANEL_DIR/docker-compose.yml" ] \
        && docker compose version >/dev/null 2>&1; then
        if ( cd "$PANEL_DIR" && docker compose \
                ${env_args[@]+"${env_args[@]}"} -f docker-compose.yml \
                up -d ) >/dev/null 2>&1; then
            say "  面板已按新配置重启（$panel）"
            return 0
        fi
    fi

    if docker restart "$panel" >/dev/null 2>&1; then
        warn "面板容器已重启，但环境变量要在下次重建后才会更新"
        return 0
    fi

    warn "面板容器重启失败（容器名 $panel）—— 手工看一眼：docker ps -a"
    return 1
}

gen_secret () {
    python3 - "$1" <<'PY'
import secrets
import sys

alphabet = "".join(
    c for c in "abcdefghjkmnprstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ2345678"
    if c not in "0oO1lL"
)

print("".join(secrets.choice(alphabet) for _ in range(int(sys.argv[1]))))
PY
}

# ------------------------------------------------------------------
# 面板 CLI（容器里的唯一写入口）
# ------------------------------------------------------------------
#
# M4 之后，安全入口 / 域名绑定 / SSL 这些设置**只有一份真相**：面板设置
# （数据库为主 + 配置文件备份）。所以管理台不再"自己改 .env 再重启容器"，
# 而是调用容器里的 panel/bin/mclink-panel.php —— 它和面板设置页走的是同一套
# 代码（panel_setting_save()），改完还会让根代理重新渲染 nginx 并热加载。
#
# 返回 0 = 面板 CLI 执行成功；非 0 = 老镜像 / 容器没跑 → 调用方走兜底。
panel_cli () {   # $@ = mclink-panel.php 的子命令与参数
    local panel
    panel="$(compose_container_name "$PANEL_DIR/docker-compose.yml" mc-panel)"

    command -v docker >/dev/null 2>&1 || return 1
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$panel" || {
        warn "面板容器（$panel）没在跑 —— 这一条走不了面板 CLI"
        return 1
    }

    docker exec "$panel" php /var/www/bin/mclink-panel.php "$@"
}

# ------------------------------------------------------------------
# 功能 1：重置面板安全入口
# ------------------------------------------------------------------
#
#  不带参数 = **清空**安全入口（"我被关在外面了"的正解：清空之后任何路径
#  都能打开面板，进去再设一个新的）。
#  带参数   = 把安全入口设成指定的值。
action_reset_entry () {
    local new_path="${1:-}"

    say "▶ 重置面板 URL 安全入口"
    audit "reset-entry" "path=${new_path:-<clear>}"

    if [ -z "$new_path" ]; then
        say "  不带参数 = 清空安全入口（进不去时的急救动作）"

        if panel_cli reset-entry; then
            say "  [√] 已通过面板 CLI 清空安全入口，并让自带 nginx 重新加载"
            say "      现在 http://<服务器IP>:<面板端口>/ 就能直接打开面板。"
            say "      想再设一个：mclink set-entry"
            return 0
        fi

        warn "面板 CLI 走不通（老镜像？）—— 退回改 .env + 重启容器（旧行为）"

        set_env_value MCLINK_PANEL_SECRET_PATH ""
        patch_compose_env "$PANEL_DIR/docker-compose.yml" MCLINK_PANEL_SECRET_PATH ""
        restart_panel

        say "  [√] MCLINK_PANEL_SECRET_PATH 已清空并重启面板"
        say "      [!] 老镜像不读这个值，真正生效的是面板设置里的安全入口 ——"
        say "          进不去就用本机急救入口：http://127.0.0.1:<急救端口>/"
        return 0
    fi

    new_path="${new_path#/}"

    case "$new_path" in
        *[!A-Za-z0-9._-]*) die "安全入口只能用字母/数字/._- ：$new_path" ;;
    esac

    [ "${#new_path}" -ge 3 ] || die "安全入口太短（至少 3 位）"

    if panel_cli set-entry --path "$new_path"; then
        say "  [√] 已通过面板 CLI 把安全入口改成 /$new_path 并热加载 nginx"
        say "      新地址：http://<服务器IP>:<面板端口>/$new_path/"
        say "      进不去就用急救入口：mclink rescue"
        return 0
    fi

    warn "面板 CLI 走不通（老镜像？）—— 退回改 .env + 重启容器（旧行为）"

    set_env_value MCLINK_PANEL_SECRET_PATH "$new_path"
    patch_compose_env "$PANEL_DIR/docker-compose.yml" MCLINK_PANEL_SECRET_PATH "$new_path"
    restart_panel

    local port
    port="$(read_env_value MCLINK_PANEL_PORT)"
    port="${port:-19102}"

    say "  新安全入口：/$new_path"
    say "  面板地址  ：http://<服务器IP>:$port/$new_path/"
    say "  [√] 已写入 $ENV_FILE、同步进 panel/docker-compose.yml，并重启了面板"
    say "      [!] 老镜像只是把这几个值备好；真正生效要等面板读设置（M4）"
}

# ------------------------------------------------------------------
# 功能 2：解绑域名绑定
# ------------------------------------------------------------------
action_unbind_domain () {
    say "▶ 解绑域名绑定"
    audit "unbind-domain" "via=panel-cli"

    if panel_cli unbind-domain; then
        say "  [√] 已通过面板 CLI 解绑域名并热加载 nginx（现在任何 Host 都能访问）"
        return 0
    fi

    warn "面板 CLI 走不通（老镜像？）—— 退回改 .env + 重启容器（旧行为）"

    local old
    old="$(read_env_value MCLINK_PANEL_DOMAIN)"
    set_env_value MCLINK_PANEL_DOMAIN ""
    patch_compose_env "$PANEL_DIR/docker-compose.yml" MCLINK_PANEL_DOMAIN ""
    restart_panel

    say "  解绑前：${old:-（本来就没绑）}"
    say "  [√] MCLINK_PANEL_DOMAIN 已清空、同步进 compose，并重启了面板"
    say "      [!] 老镜像不读这个值；真正生效的是面板设置里的「绑定域名」"
}

# ------------------------------------------------------------------
# 功能 2b：关掉面板 HTTPS（证书坏了的第一反应）
# ------------------------------------------------------------------
action_ssl_off () {
    say "▶ 关掉面板 HTTPS（回到纯 HTTP，证书文件保留）"
    audit "ssl-off" "via=panel-cli"

    if panel_cli ssl-off; then
        say "  [√] 已通过面板 CLI 把 SSL 模式改成「不用 HTTPS」并热加载 nginx"
        say "      证书一个都没删；修好之后在面板设置页重新启用即可。"
        return 0
    fi

    warn "面板 CLI 走不通：请用急救入口进去，在「面板设置」里把 SSL 模式改回「不用 HTTPS」"
    return 1
}

# ------------------------------------------------------------------
# 功能 2c：证书与生效状态
# ------------------------------------------------------------------
action_ssl_status () {
    say "▶ 面板证书与生效状态"
    audit "ssl-status" "via=panel-cli"

    if panel_cli status; then
        return 0
    fi

    warn "面板 CLI 走不通（容器没跑？）—— 只能看本机文件"
    say "  设置备份：$MCLINK_ROOT/config/panel-settings.json"
    say "  证书目录：$MCLINK_ROOT/data/panel-certs"
    return 1
}

# ------------------------------------------------------------------
# 功能 2d：把网络设置落到宿主机（端口映射 / 80 / 443）
# ------------------------------------------------------------------
action_apply_network () {
    say "▶ 把面板设置里的监听地址 / 端口 / SSL 落到宿主机"
    audit "apply-network" "script=scripts/panel-apply.sh"

    [ -f "$MC_LINK_ROOT/scripts/panel-apply.sh" ] \
        || die "找不到 $MC_LINK_ROOT/scripts/panel-apply.sh"

    if bash "$MC_LINK_ROOT/scripts/panel-apply.sh" "$@"; then
        say "  [√] 完成（面板容器已按新的端口映射重建；中继与玩家不受影响）"
        return 0
    fi

    die "apply-network 失败（端口被占？看上面的输出；急救入口一直是通的：mclink rescue）"
}

# ------------------------------------------------------------------
# 功能 2e：急救入口地址
# ------------------------------------------------------------------
action_rescue () {
    local port
    port="$(read_env_value MCLINK_PANEL_RESCUE_PORT)"
    port="${port:-19103}"

    say "▶ 本机急救入口"
    audit "rescue" "port=$port"

    say "  在服务器上打开（或 SSH 端口转发到自己机器）："
    say "      http://127.0.0.1:$port/"
    say "  在服务器上直接验："
    say "      curl -sS http://127.0.0.1:$port/healthz"
    say "      curl -sS -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:$port/index.php?action=login"
    say ""
    say "  它只绑定宿主机回环地址，公网到不了；而且不判安全入口 / 不判域名 / 不跳 HTTPS ——"
    say "  安全入口写错、域名绑错、证书坏掉这三种「自己被自己关在外面」的情况都从这里进。"
}


# ------------------------------------------------------------------
# 功能 3：修改 root 密码
# ------------------------------------------------------------------
action_reset_root_password () {
    say "▶ 修改 root（中继）账号密码"
    say "  这一步走中继容器里的 reset_password.py：它只会动 root 一个账号，"
    say "  可以回车让它生成随机密码。"

    local relay
    relay="$(compose_container_name "$RELAY_DIR/docker-compose.yml" mc-relay)"

    command -v docker >/dev/null 2>&1 || die "没有 docker"

    audit "reset-root-password" "container=$relay"

    if docker exec -it "$relay" python reset_password.py; then
        say "  [√] root 密码已更新（上面的用户名/密码请立刻记下来）"
        audit "reset-root-password" "ok"
    else
        die "reset_password.py 失败（容器在跑吗：docker ps | grep $relay）"
    fi
}

# ------------------------------------------------------------------
# 功能 4：重置面板会话
# ------------------------------------------------------------------
action_reset_sessions () {
    say "▶ 重置面板会话（所有人被强制退出登录）"

    local panel
    panel="$(compose_container_name "$PANEL_DIR/docker-compose.yml" mc-panel)"

    command -v docker >/dev/null 2>&1 || die "没有 docker"

    # 会话目录从容器里的 php 配置问出来（写死 /var/lib/php/sessions 迟早会错）。
    local dir
    dir="$(docker exec "$panel" php -r 'echo session_save_path();' 2>/dev/null)"
    dir="${dir:-/var/lib/php/sessions}"

    if docker exec "$panel" sh -c "rm -f '$dir'/sess_*" >/dev/null 2>&1; then
        say "  已清空 $panel:$dir"
        audit "reset-sessions" "ok dir=$dir"
        say "  [√] 所有面板会话已失效，下次访问需要重新登录"
    else
        die "清空会话失败（容器 $panel 在跑吗？目录 $dir 存在吗？）"
    fi
}

# ------------------------------------------------------------------
# 功能 5：服务状态与日志
# ------------------------------------------------------------------
action_status () {
    local relay panel

    relay="$(compose_container_name "$RELAY_DIR/docker-compose.yml" mc-relay)"
    panel="$(compose_container_name "$PANEL_DIR/docker-compose.yml" mc-panel)"

    say "▶ 服务状态"
    audit "status" "relay=$relay panel=$panel"

    if ! command -v docker >/dev/null 2>&1; then
        warn "没有 docker —— 只能打印本机文件状态"
    else
        say ""
        say "-- 容器 --"
        docker ps -a --filter "name=$relay" --filter "name=$panel" \
            --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null \
            || warn "docker ps 失败"

        say ""
        say "-- 中继最近 15 行 --"
        docker logs --tail 15 "$relay" 2>&1 || warn "读不到 $relay 的日志"

        say ""
        say "-- 面板最近 15 行 --"
        docker logs --tail 15 "$panel" 2>&1 || warn "读不到 $panel 的日志"
    fi

    say ""
    say "-- 本机文件 --"
    say "  安装根目录 : $MC_LINK_ROOT"
    say "  .env       : $([ -f "$ENV_FILE" ] && echo "存在（$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo '?')）" || echo '不存在')"
    say "  审计日志   : ${AUDIT_LOG:-（不可写）}"
}

# ------------------------------------------------------------------
# 功能 6：显示 .env 里的凭据（只有 root 能到这里）
# ------------------------------------------------------------------
action_show_secrets () {
    say "▶ .env 里的凭据（仅 root）"
    audit "show-secrets" "file=$ENV_FILE"

    [ -f "$ENV_FILE" ] || die "没有 $ENV_FILE（还没安装？）"

    say ""
    say "  文件：$ENV_FILE（权限 $(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo '?')）"
    say ""

    local printed=0 key value

    for key in MCLINK_DB_HOST MCLINK_DB_PORT MCLINK_DB_NAME MCLINK_DB_USER \
               MCLINK_DB_PASSWORD MCLINK_DB_ROOT_PASSWORD \
               MCLINK_ADMIN_API_TOKEN MCLINK_PANEL_PORT \
               MCLINK_PANEL_SECRET_PATH MCLINK_DOCKER_NETWORK; do
        value="$(read_env_value "$key")"

        if [ -n "$value" ]; then
            printf '  %-28s = %s\n' "$key" "$value"
            printed=1
        fi
    done

    if [ "$printed" != "1" ]; then
        warn ".env 里没有安装器认识的键（是不是手写的？）"
    fi

    say ""
    say "  [!] 这些值等同于服务器控制权，别贴进聊天/工单。"
    say "      要换掉数据库密码：bash $MC_LINK_ROOT/rebuild.sh（或 rotate_secrets.py）"
}

# ------------------------------------------------------------------
# 审计日志
# ------------------------------------------------------------------
action_audit () {
    local lines="${1:-40}"

    say "▶ 审计日志（最后 $lines 行）：${AUDIT_LOG:-（不可写）}"
    [ -n "$AUDIT_LOG" ] && [ -f "$AUDIT_LOG" ] || die "没有审计日志文件"

    tail -n "$lines" "$AUDIT_LOG"
}

# ------------------------------------------------------------------
# 菜单
# ------------------------------------------------------------------
menu () {
    local choice

    while true; do
        say "============================================================"
        say " CraftLink 管理台（root）  $MC_LINK_ROOT"
        say "============================================================"
        say "  1) 重置面板安全入口（清空 —— 自己被关在外面时用）"
        say "  2) 解绑域名绑定"
        say "  3) 修改 root 密码"
        say "  4) 重置面板会话（强制重新登录）"
        say "  5) 查看服务状态与日志"
        say "  6) 显示 .env 里的凭据"
        say "  7) 查看审计日志"
        say "  8) 面板设置 / 证书状态（监听、安全入口、域名、SSL）"
        say "  9) 关掉面板 HTTPS（证书坏了时用）"
        say " 10) 把端口 / SSL 设置落到宿主机（重建面板容器）"
        say " 11) 打印本机急救入口地址"
        say "  0) 退出"
        say ""

        # read 失败（EOF / Ctrl-D）也当成退出，绝不留下一个转不出去的循环。
        # 注意别写成 `if ! printf ...; read ...` —— bash 的 `!` 会否定**整条
        # 命令列表**，那样每次输入都会被误判成 EOF。
        printf '选择 [0-11]: '

        if ! read -r choice; then
            say ""
            say "退出。"
            return 0
        fi

        case "$choice" in
            1) action_reset_entry ;;
            2) action_unbind_domain ;;
            3) action_reset_root_password ;;
            4) action_reset_sessions ;;
            5) action_status ;;
            6) action_show_secrets ;;
            7) action_audit ;;
            8) action_ssl_status ;;
            9) action_ssl_off ;;
            10) action_apply_network ;;
            11) action_rescue ;;
            0|"") say "退出。"; return 0 ;;
            *) warn "没有这个选项：$choice" ;;
        esac

        say ""
    done
}

# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
COMMAND="${1:-menu}"
shift 2>/dev/null || true

audit "invoke" "cmd=$COMMAND"

case "$COMMAND" in
    menu)               menu ;;
    1|reset-entry|reset-security-entry)
        _reset_path="${1:-}"

        case "$_reset_path" in
            --path)   _reset_path="${2:-}" ;;
            --path=*) _reset_path="${_reset_path#--path=}" ;;
        esac

        action_reset_entry "$_reset_path"
        ;;
    set-entry|set-security-entry)
        # 两种写法都认：set-entry my-secret / set-entry --path my-secret
        # 不带参数 = 随机生成一个新的安全入口（老 reset-entry 的行为）
        _entry_path="${1:-}"

        case "$_entry_path" in
            --path)   _entry_path="${2:-}" ;;
            --path=*) _entry_path="${_entry_path#--path=}" ;;
        esac

        if [ -z "$_entry_path" ]; then
            _entry_path="$(gen_secret 16)"
        fi

        action_reset_entry "$_entry_path"
        ;;
    2|unbind-domain|unbind)             action_unbind_domain ;;
    3|reset-root-password|passwd)       action_reset_root_password ;;
    4|reset-sessions)                   action_reset_sessions ;;
    5|status|logs)                      action_status ;;
    6|show-secrets|secrets)             action_show_secrets ;;
    7|audit)                            action_audit "${1:-40}" ;;
    8|ssl-status|panel-status|certs)    action_ssl_status ;;
    9|ssl-off|no-ssl|http-only)         action_ssl_off ;;
    10|apply-network|apply)             action_apply_network "$@" ;;
    11|rescue|rescue-url)               action_rescue ;;
    help|-h|--help)
        sed -n '2,30p' "$SCRIPT_PATH" | sed 's/^# \{0,1\}//'
        ;;
    *)
        warn "不认识的命令：$COMMAND"
        say "可用：menu status show-secrets reset-entry set-entry unbind-domain"
        say "      reset-root-password reset-sessions audit"
        say "      ssl-status ssl-off apply-network rescue"
        exit 2
        ;;
esac

exit 0
