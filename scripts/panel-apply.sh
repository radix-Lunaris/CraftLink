#!/usr/bin/env bash
# ============================================================
#  CraftLink 面板 —— 把「面板设置」里的网络配置落到宿主机（M4）
# ============================================================
#
#  为什么需要它（一句话）：
#    容器里的 nginx 只听固定的 80 / 443 / 19103；**宿主机**这一侧绑定成
#    什么地址、什么端口是 compose 的 ports 决定的。改 listen_ip / listen_port
#    / 开 SSL 之后，要让宿主机跟着变，就得重新生成端口映射并重建容器。
#
#  它做四件事：
#    1. 读"当前生效的设置"：优先问容器里的面板 CLI（docker exec，最准），
#       容器没在跑就退回读配置文件（含急救覆盖）；
#    2. **按设置改写 panel/docker-compose.yml 里的两行 ports**（主入口 + 急救
#       入口）与对应的两个环境变量 —— 形状固定，所以 install.sh 的
#       `--panel-port` 和本脚本不会互相打架；
#    3. 需要 HTTPS 时生成 override 文件 docker-compose.panel-ssl.yml，
#       把 80（ACME + 跳转）和 443（HTTPS）映射加上；
#    4. docker compose up -d 重建面板容器。
#
#  用法：
#      bash scripts/panel-apply.sh              # 真正执行
#      bash scripts/panel-apply.sh --dry-run    # 只打印将要做什么
#      bash scripts/panel-apply.sh --no-restart # 只写 .env 与 override
#      bash scripts/panel-apply.sh --root /srv/mclink
#
#  [!] 不写死 /opt/mc-link：安装根目录从脚本自身位置推导（D8），
#      也可以用 MCLINK_ROOT 或 --root 覆盖。
#
#  [!] 不改任何凭据：只动网络相关的键，.env 里其余内容原样保留
#      （写值走 scripts/install_db.py env-set，那个已经有"保留其它行"的逻辑）。
# ============================================================

set -u

PROG="${0##*/}"
DRY_RUN=0
DO_RESTART=1

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)    DRY_RUN=1; shift ;;
        --no-restart) DO_RESTART=0; shift ;;
        --root)       MCLINK_ROOT="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "[x] 不认识的参数：$1（--help 看用法）" >&2
            exit 2
            ;;
    esac
done

say ()  { printf '%s\n' "$*"; }
warn () { printf '  [!] %s\n' "$*"; }
die ()  { printf '✘ %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------
# 安装根目录：从脚本自身位置推导（不写死 /opt/mc-link）
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

[ -d "$MC_LINK_ROOT" ] || die "安装根目录不存在：$MC_LINK_ROOT"
MC_LINK_ROOT="$(cd "$MC_LINK_ROOT" && pwd)"

PANEL_DIR="$MC_LINK_ROOT/panel"
ENV_FILE="${MCLINK_ENV_FILE:-$MC_LINK_ROOT/.env}"
CONFIG_DIR="${MCLINK_CONFIG_DIR:-$MC_LINK_ROOT/config}"
SETTINGS_FILE="$CONFIG_DIR/panel-settings.json"
OVERRIDES_FILE="$CONFIG_DIR/panel-emergency-overrides.json"
OVERRIDE_COMPOSE="$PANEL_DIR/docker-compose.panel-ssl.yml"

[ -d "$PANEL_DIR" ] || die "找不到面板目录：$PANEL_DIR"
[ -f "$PANEL_DIR/docker-compose.yml" ] || die "找不到 $PANEL_DIR/docker-compose.yml"

say "▶ 把面板设置里的网络配置落到宿主机"
say "  安装根目录 : $MC_LINK_ROOT"
say "  .env       : $ENV_FILE"
say "  设置备份   : $SETTINGS_FILE"
say "  容器       : $(grep -m1 -E '^[[:space:]]*container_name:' "$PANEL_DIR/docker-compose.yml" 2>/dev/null | sed -E 's/^[^:]*:[[:space:]]*//' || echo mc-panel)"

# ------------------------------------------------------------------
# 1. 读当前生效的设置
# ------------------------------------------------------------------
PANEL_CONTAINER="$(grep -m1 -E '^[[:space:]]*container_name:' "$PANEL_DIR/docker-compose.yml" 2>/dev/null \
    | sed -E 's/^[^:]*:[[:space:]]*//; s/^["'"'"']//; s/["'"'"']$//')"
PANEL_CONTAINER="${PANEL_CONTAINER:-mc-panel}"

STATUS_JSON=""

if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$PANEL_CONTAINER"; then
    STATUS_JSON="$(docker exec "$PANEL_CONTAINER" php /var/www/bin/mclink-panel.php status --json 2>/dev/null | tail -n1)"

    if [ -n "$STATUS_JSON" ]; then
        say "  设置来源   : 容器里的面板 CLI（$PANEL_CONTAINER）"
    fi
fi

if [ -z "$STATUS_JSON" ]; then
    say "  设置来源   : 配置文件（容器没在跑，或 CLI 读不到）"
fi

# 用 python3 解析（和 install_db.py 一致：宿主机上有 python3）
read_settings () {
    python3 - "$SETTINGS_FILE" "$OVERRIDES_FILE" "$STATUS_JSON" <<'PY'
import json
import os
import sys

settings_file, overrides_file, status_json = sys.argv[1], sys.argv[2], sys.argv[3]

values = {
    "listen_ip": "127.0.0.1",
    "listen_port": "19102",
    "ssl_mode": "off",
    "bound_domain": "",
    "force_https": "0",
}

if status_json.strip():
    try:
        data = json.loads(status_json)
        for key in values:
            if isinstance(data.get(key), str) and data[key] != "":
                values[key] = data[key]
        if data.get("listen_port"):
            values["listen_port"] = str(data["listen_port"])
        for key in ("ssl_mode", "force_https"):
            if isinstance(data.get(key), str):
                values[key] = data[key]
        values["rescue_port"] = str(data.get("rescue_port") or "19103")
    except Exception:
        pass
else:
    for path in (settings_file, overrides_file):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            for key, value in (data.get("settings") or {}).items():
                if key in values and isinstance(value, str) and value != "":
                    values[key] = value
        except Exception:
            pass

values.setdefault("rescue_port", os.environ.get("MCLINK_PANEL_RESCUE_PORT", "19103"))

for key in ("listen_ip", "listen_port", "ssl_mode", "bound_domain", "force_https", "rescue_port"):
    print("%s=%s" % (key.upper(), values.get(key, "")))
PY
}

SETTINGS="$(read_settings)" || die "读设置失败（python3 / JSON 有问题？）"

get () { printf '%s\n' "$SETTINGS" | grep -E "^$1=" | head -n1 | cut -d= -f2-; }

LISTEN_IP="$(get LISTEN_IP)";   LISTEN_IP="${LISTEN_IP:-127.0.0.1}"
LISTEN_PORT="$(get LISTEN_PORT)"; LISTEN_PORT="${LISTEN_PORT:-19102}"
RESCUE_PORT="$(get RESCUE_PORT)"; RESCUE_PORT="${RESCUE_PORT:-19103}"
SSL_MODE="$(get SSL_MODE)";     SSL_MODE="${SSL_MODE:-off}"

HTTP_PORT="${MCLINK_PANEL_HTTP_PORT:-80}"
HTTPS_PORT="${MCLINK_PANEL_HTTPS_PORT:-443}"

say ""
say "  当前设置：listen=${LISTEN_IP}:${LISTEN_PORT} rescue=127.0.0.1:${RESCUE_PORT} ssl_mode=${SSL_MODE}"

ENV_UPDATES=(
    "MCLINK_PANEL_LISTEN_IP=$LISTEN_IP"
    "MCLINK_PANEL_PORT=$LISTEN_PORT"
    "MCLINK_PANEL_RESCUE_PORT=$RESCUE_PORT"
    "MCLINK_PANEL_HTTP_PORT=$HTTP_PORT"
    "MCLINK_PANEL_HTTPS_PORT=$HTTPS_PORT"
)

# ------------------------------------------------------------------
# 1b. 改写 panel/docker-compose.yml 里的两行端口
# ------------------------------------------------------------------
#
# 形状是固定的（见 compose 里的注释）：
#     - "127.0.0.1:19102:80"        ← 主入口：<listen_ip>:<listen_port>:80
#     - "127.0.0.1:19103:19103"     ← 急救：  127.0.0.1:<rescue>:<rescue>
#
# [!] 为什么不用 ${MCLINK_PANEL_PORT} 这种变量插值：install.sh 的
#     `--panel-port` 是**按这个字面形状**改写这一行的（scripts/install_patch.py），
#     仓库的回归测试也这么断言。保持同一种形状，两条改法就不会互相覆盖。
patch_panel_ports () {
    python3 - "$PANEL_DIR/docker-compose.yml" "$LISTEN_IP" "$LISTEN_PORT" "$RESCUE_PORT" <<'PATCHPY'
import re
import sys

path, listen_ip, listen_port, rescue_port = sys.argv[1:5]

if not re.match(r'^[0-9.]+$', listen_ip):
    print("SKIP-IPV6")
    sys.exit(3)

with open(path, encoding="utf-8") as handle:
    lines = handle.readlines()

# [!] 每一组的最后一个捕获组必须是"行尾空白（含换行）"：re.sub 会因为
#     \s*$ 把换行一起吃掉，结果两行粘成一行（YAML 直接坏掉）。
main_re = re.compile(r'^(\s*-\s*")[0-9.]+:\d+:80(")(\s*)$')
rescue_re = re.compile(r'^(\s*-\s*")[0-9.]+:\d+:19103(")(\s*)$')
rescue_env_re = re.compile(r'^(\s*MCLINK_PANEL_RESCUE_PORT:\s*)"[^"]*"(\s*)$')
port_env_re = re.compile(r'^(\s*MCLINK_PANEL_PORT:\s*)"[^"]*"(\s*)$')

changed = 0

for index, line in enumerate(lines):
    if main_re.match(line):
        lines[index] = main_re.sub(r'\g<1>%s:%s:80\g<2>\g<3>' % (listen_ip, listen_port), line)
        changed += 1
        continue

    if rescue_re.match(line):
        lines[index] = rescue_re.sub(r'\g<1>127.0.0.1:%s:%s\g<2>\g<3>' % (rescue_port, rescue_port), line)
        changed += 1
        continue

    if rescue_env_re.match(line):
        lines[index] = rescue_env_re.sub(r'\g<1>"%s"\g<2>' % rescue_port, line)
        changed += 1
        continue

    if port_env_re.match(line):
        lines[index] = port_env_re.sub(r'\g<1>"%s"\g<2>' % listen_port, line)
        changed += 1
        continue

with open(path, "w", encoding="utf-8") as handle:
    handle.writelines(lines)

print(changed)
PATCHPY
}

# ------------------------------------------------------------------
# 2. override 文件：要不要 80 / 443
# ------------------------------------------------------------------
build_override () {
    if [ "$SSL_MODE" = "off" ]; then
        cat > "$OVERRIDE_COMPOSE" <<YAML
# ============================================================
#  由 scripts/panel-apply.sh 生成（**不要手改**，下次 apply 会覆盖）
#
#  当前面板设置：ssl_mode=off（不用 HTTPS），所以不需要 80 / 443 映射。
#  这个文件故意只有注释：留着它是为了让 compose 的命令行始终带上它
#  （docker compose -f docker-compose.yml -f docker-compose.panel-ssl.yml），
#  免得"这次有、下次没有"造成困惑。
# ============================================================
services:
  mc-panel:
    ports: []
YAML
        return 0
    fi

    # 开了 HTTPS：把 80（ACME 校验 + HTTP→HTTPS 跳转）和 443（HTTPS）
    # 映射到容器里的固定端口。这两个端口经常被 1Panel 的 nginx 占着 ——
    # 冲突时 docker compose up 会明确报错（不会静默把你的网站顶掉）。
    cat > "$OVERRIDE_COMPOSE" <<YAML
# ============================================================
#  由 scripts/panel-apply.sh 生成（**不要手改**，下次 apply 会覆盖）
#
#  面板设置：ssl_mode=$SSL_MODE，所以加上 80 / 443 映射：
#    * $HTTP_PORT  -> 80：Let's Encrypt 的 HTTP-01 校验（必须明文）
#                          以及 HTTP → HTTPS 跳转
#    * $HTTPS_PORT -> 443：HTTPS 本体
#
#  [!] 这两个端口如果已经被 1Panel 的 nginx 占着，docker compose up -d
#      会报 "port is already allocated"：先把占用它的服务停掉（或者改
#      MCLINK_PANEL_HTTP_PORT / MCLINK_PANEL_HTTPS_PORT 用别的端口，
#      但 Let's Encrypt 的 HTTP-01 只认 80）。
#  [!] 证书/域名绑定本身是面板容器里的 nginx 在做，这里只是把端口放进来。
# ============================================================
services:
  mc-panel:
    ports:
      - "0.0.0.0:$HTTP_PORT:80"
      - "$LISTEN_IP:$HTTPS_PORT:443"
YAML

    return 0
}

# ------------------------------------------------------------------
# 3. dry-run / 真正执行
# ------------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
    say ""
    say "-- dry-run：将要改写的端口行（$PANEL_DIR/docker-compose.yml）--"
    say "   - \"$LISTEN_IP:$LISTEN_PORT:80\"      （主入口；容器里 nginx 听 80）"
    say "   - \"127.0.0.1:$RESCUE_PORT:$RESCUE_PORT\" （急救入口，只绑回环）"

    say ""
    say "-- dry-run：将要写入 $ENV_FILE --"

    for pair in "${ENV_UPDATES[@]}"; do
        say "   $pair"
    done

    say ""
    say "-- dry-run：将要生成 $OVERRIDE_COMPOSE --"

    if [ "$SSL_MODE" = "off" ]; then
        say "   （ssl_mode=off：不加 80 / 443 映射）"
    else
        say "   0.0.0.0:$HTTP_PORT:80（ACME + 跳转）"
        say "   $LISTEN_IP:$HTTPS_PORT:443（HTTPS）"
    fi

    say ""
    say "-- dry-run：将要执行的命令 --"
    say "   docker compose -f docker-compose.yml -f docker-compose.panel-ssl.yml up -d"
    exit 0
fi

PATCHED="$(patch_panel_ports)" || die "改写 $PANEL_DIR/docker-compose.yml 的端口行失败（IPv6 监听地址暂不支持，请用 IPv4 或 0.0.0.0）"
say "  [√] 已改写 $PANEL_DIR/docker-compose.yml 的端口行（$PATCHED 处）"

if [ -f "$ENV_FILE" ]; then
    for pair in "${ENV_UPDATES[@]}"; do
        printf '%s\n' "$pair" \
            | python3 "$MC_LINK_ROOT/scripts/install_db.py" env-set \
                --env "$ENV_FILE" --force >/dev/null \
            || die "写 $ENV_FILE 失败（${pair%%=*}）"
    done

    say "  [√] 已写入 $ENV_FILE（$(printf '%s ' "${ENV_UPDATES[@]}" | wc -w) 个键）"
else
    warn "没有 $ENV_FILE（还没安装？）—— 跳过 .env"
fi

build_override
say "  [√] 已生成 $OVERRIDE_COMPOSE（ssl_mode=$SSL_MODE）"

if [ "$DO_RESTART" != "1" ]; then
    say ""
    say "  （--no-restart：没有重建容器；生效请跑："
    say "     cd $PANEL_DIR && docker compose -f docker-compose.yml -f docker-compose.panel-ssl.yml up -d ）"
    exit 0
fi

say ""
say "▶ 重建面板容器（只重建面板，不动中继与数据库）"

if ! command -v docker >/dev/null 2>&1; then
    die "找不到 docker"
fi

if ! docker compose version >/dev/null 2>&1; then
    die "docker compose 不可用（需要 compose v2）"
fi

COMPOSE_ARGS=(-f docker-compose.yml -f docker-compose.panel-ssl.yml)

if [ -f "$ENV_FILE" ]; then
    COMPOSE_ARGS=(--env-file "$ENV_FILE" "${COMPOSE_ARGS[@]}")
fi

if ( cd "$PANEL_DIR" && docker compose "${COMPOSE_ARGS[@]}" up -d ); then
    say "  [√] 面板容器已按新的端口映射重建（玩家不受影响）"
    say ""
    say "  现在可以这样访问："
    say "    HTTP : http://${LISTEN_IP}:${LISTEN_PORT}/   （listen_ip 是 127.0.0.1 时只有本机能访问）"

    if [ "$SSL_MODE" != "off" ]; then
        say "    HTTPS: https://<绑定域名>:${HTTPS_PORT}/"
    fi

    say "    急救 : http://127.0.0.1:${RESCUE_PORT}/     （配置改坏了从这里进）"
else
    die "compose up 失败（端口被占？看上面的输出；急救入口 127.0.0.1:${RESCUE_PORT} 一直是通的）"
fi

exit 0
