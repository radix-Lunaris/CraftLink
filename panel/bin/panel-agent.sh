#!/bin/sh
# ============================================================
#  CraftLink 面板 —— 根代理（root，supervisord 常驻）
# ============================================================
#
#  它是"面板设置 → 真的生效"这条链路上**唯一有 root** 的那一段：
#
#      面板（php-fpm, nobody）
#        └─ 写 <证书目录>/agent/requests/<id>.json      ← panel/lib/panel_agent.php
#                        │
#                        ▼
#      本脚本（root）── 每 MCLINK_AGENT_INTERVAL 秒扫一次 ──┐
#        ├─ php mclink-panel.php render   （渲染配置，PHP 那一侧的逻辑）  │
#        ├─ nginx -t -c <测试主配置>       （候选配置先试跑）              │
#        ├─ 通过 → 备份 + 原子替换 → nginx -s reload                       │
#        ├─ 不通过 → 保留上一份能跑的配置（**绝不让面板打不开**）          │
#        ├─ mclink-cert.sh import/selfsigned/acme-*（证书，openssl/certbot）│
#        └─ 写 <证书目录>/agent/results/<id>.json ────────────────────────┘
#
#  另外两件定时的事：
#    * 启动时：从环境变量播种一次设置（老部署的 MCLINK_PANEL_SECRET_PATH /
#      MCLINK_PANEL_DOMAIN / MCLINK_PANEL_PORT），保证升级后行为不变；
#    * 每 MCLINK_ACME_CHECK_INTERVAL 秒（默认 6 小时）：检查 ACME 证书到期
#      时间，剩不到 MCLINK_ACME_RENEW_DAYS 天（默认 30）就自动续期 ——
#      **续期失败绝不动现有证书**。
#
#  为什么是 POSIX sh：容器是 alpine（busybox ash），没有 bash。
#
#  权限：面板（php-fpm）以 nobody(65534) 跑，所以"请求目录"和"待安装证书目录"
#  必须归它（prepare_dirs() 负责 chown）；证书、私钥、结果、日志是 root 写的，
#  面板只需要读（644）。
# ============================================================

set -u

# ------------------------------------------------------------------
# 路径与参数（全部可用环境变量覆盖；默认值就是镜像里的约定位置）
# ------------------------------------------------------------------
PHP_BIN="${MCLINK_PHP_BIN:-/usr/local/bin/php}"
PANEL_BIN_DIR="${MCLINK_PANEL_BIN_DIR:-/var/www/bin}"
PANEL_CLI="$PANEL_BIN_DIR/mclink-panel.php"
CERT_TOOL="$PANEL_BIN_DIR/mclink-cert.sh"
CERT_DIR="${MCLINK_PANEL_CERT_DIR:-/var/www/certs}"
AGENT_DIR="${MCLINK_PANEL_AGENT_DIR:-$CERT_DIR/agent}"
REQ_DIR="$AGENT_DIR/requests"
RES_DIR="$AGENT_DIR/results"
LOG_FILE="$AGENT_DIR/agent.log"
STATUS_FILE="$AGENT_DIR/agent-status.json"
SERVER_DIR="${MCLINK_NGINX_SERVER_DIR:-/etc/nginx/mclink-servers}"
NGINX_BIN="${MCLINK_NGINX_BIN:-/usr/sbin/nginx}"
NGINX_TEST_CONF="${MCLINK_NGINX_TEST_CONF:-/etc/nginx/mclink-nginx-test.conf}"
NGINX_TEST_TEMPLATE="${MCLINK_NGINX_TEST_TEMPLATE:-$SERVER_DIR/../mclink-nginx-test.conf.template}"
MIME_TYPES="${MCLINK_MIME_TYPES:-/etc/nginx/mime.types}"

INTERVAL="${MCLINK_AGENT_INTERVAL:-2}"
HEARTBEAT_EVERY="${MCLINK_AGENT_HEARTBEAT:-15}"
ACME_CHECK_INTERVAL="${MCLINK_ACME_CHECK_INTERVAL:-21600}"
ACME_RENEW_DAYS="${MCLINK_ACME_RENEW_DAYS:-30}"
START_DELAY="${MCLINK_AGENT_START_DELAY:-0}"

AGENT_VERSION="1.0"

# ------------------------------------------------------------------
# 日志：stdout（docker logs）+ agent.log（面板能读最后几行）
# ------------------------------------------------------------------
say () {
    line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"

    printf '%s\n' "$line"
    mkdir -p "$AGENT_DIR" 2>/dev/null || true
    printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null || true

    # 日志别无限长：超过 2000 行就砍掉前 1000 行。
    if [ -f "$LOG_FILE" ]; then
        lines=$(wc -l < "$LOG_FILE" 2>/dev/null || printf '0')
        if [ "$lines" -gt 2000 ] 2>/dev/null; then
            tail -n 1000 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE" 2>/dev/null
        fi
    fi
}

# ------------------------------------------------------------------
# 迷你 JSON：写（转义）与读（我们自己的单行扁平格式）
# ------------------------------------------------------------------
json_escape () {
    printf '%s' "$1" | tr -d '\r\n' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

json_get () {   # $1=文件 $2=键 → 字符串值（没有就空）
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"\\\\]*\)\".*/\1/p" "$1" 2>/dev/null | head -n1
}

json_get_raw () {   # 数字 / true / false
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\([^,}[:space:]]*\).*/\1/p" "$1" 2>/dev/null | head -n1
}

write_result () {   # $1=id $2=action $3=ok(0/1) $4=message $5=detail-json
    mkdir -p "$RES_DIR" 2>/dev/null || true

    tmp="$RES_DIR/$1.json.tmp"
    {
        printf '{"schema": 1, "id": "%s", "action": "%s", "ok": %s, "at": "%s", "message": "%s", "error": "%s", "detail": %s}\n' \
            "$(json_escape "$1")" "$(json_escape "$2")" \
            "$([ "$3" = "0" ] && printf 'true' || printf 'false')" \
            "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
            "$(json_escape "$4")" \
            "$([ "$3" = "0" ] && printf '' || json_escape "$4")" \
            "${5:-{\}}"
    } > "$tmp" 2>/dev/null || return 1

    mv "$tmp" "$RES_DIR/$1.json" 2>/dev/null || return 1
    return 0
}

# 参数只允许"我们认识的东西"：路径必须是绝对路径 + 安全字符集
safe_path () {
    case "$1" in
        /*) : ;;
        *) return 1 ;;
    esac

    case "$1" in
        *..*) return 1 ;;
        *[\ \	\"\'\`\$\|\&\;\<\>]*) return 1 ;;
    esac

    return 0
}

safe_token () {   # 域名 / 邮箱 / 模式这类短值
    case "$1" in
        *[\ \"\'\`\$\|\&\;\<\>]*|"") return 1 ;;
    esac

    return 0
}

# ------------------------------------------------------------------
# PHP CLI 的封装（设置只有一个写入口：panel/lib/panel_settings.php）
# ------------------------------------------------------------------
php_cli () {
    [ -f "$PANEL_CLI" ] || return 127

    "$PHP_BIN" "$PANEL_CLI" "$@" 2>&1
}

state_json () {
    php_cli state --json | tail -n1
}

state_get () {   # $1=键
    state_json | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"\\\\]*\)\".*/\1/p" | head -n1
}

# ------------------------------------------------------------------
# nginx：试跑候选配置 / 安装 / reload
# ------------------------------------------------------------------
nginx_test_candidate () {   # $1=候选目录 → 0 = 通过
    candidate="$1"

    [ -x "$NGINX_BIN" ] || { say "nginx 不在（$NGINX_BIN）：跳过 -t，保留现有配置"; return 1; }
    [ -f "$NGINX_TEST_CONF" ] || { say "测试主配置不在（$NGINX_TEST_CONF）"; return 1; }

    test_prefix="$AGENT_DIR/nginx-test"
    mkdir -p "$test_prefix"

    conf="$test_prefix/nginx-test.conf"

    sed -e "s|@CANDIDATE_DIR@|$candidate|g" \
        -e "s|@MIME_TYPES@|$MIME_TYPES|g" \
        -e "s|@TEST_PREFIX@|$test_prefix|g" \
        "$NGINX_TEST_CONF" > "$conf" 2>/dev/null || {
            say "生成测试主配置失败：$conf"
            return 1
        }

    "$NGINX_BIN" -t -p "$test_prefix" -c "$conf" > "$test_prefix/nginx-t.log" 2>&1
    rc=$?

    if [ "$rc" != "0" ]; then
        say "nginx -t 不通过（候选配置）：$(tail -n 3 "$test_prefix/nginx-t.log" 2>/dev/null | tr '\n' ' ')"
        return 1
    fi

    return 0
}

nginx_reload () {
    [ -x "$NGINX_BIN" ] || return 1

    if "$NGINX_BIN" -s reload >/dev/null 2>&1; then
        say "nginx 已热加载新配置"
        return 0
    fi

    say "nginx reload 失败（进程没在跑？supervisord 会把它拉起来）"
    return 1
}

# ------------------------------------------------------------------
# 动作 1：渲染 + 试跑 + 安装 + reload
# ------------------------------------------------------------------
action_render () {
    candidate="$AGENT_DIR/nginx-candidate"
    rm -rf "$candidate"
    mkdir -p "$candidate"

    render_out=$(php_cli render --dir "$candidate")
    rc=$?

    if [ "$rc" != "0" ]; then
        say "渲染失败：$render_out"
        return 1
    fi

    nginx_test_candidate "$candidate" || return 1

    # 备份当前生效的那一份（失败要能原样放回去）
    backup="$AGENT_DIR/nginx-lastgood"
    rm -rf "$backup"
    mkdir -p "$backup"

    if [ -d "$SERVER_DIR" ]; then
        cp -a "$SERVER_DIR"/. "$backup"/ 2>/dev/null || true
    fi

    render_out=$(php_cli render --dir "$SERVER_DIR")
    rc=$?

    if [ "$rc" != "0" ]; then
        say "渲染到生效目录失败：$render_out"
        return 1
    fi

    # 整份正式配置再跑一次 -t：候选试跑用的是测试主配置，这一步用的是真主配置
    # （能抓到 include 缺失、端口冲突之类只在正式配置里出现的问题）。
    if [ -x "$NGINX_BIN" ]; then
        if ! "$NGINX_BIN" -t >"$AGENT_DIR/nginx-live-t.log" 2>&1; then
            say "生效目录 -t 不通过，回滚：$(tail -n 3 "$AGENT_DIR/nginx-live-t.log" 2>/dev/null | tr '\n' ' ')"

            if [ -d "$backup" ]; then
                rm -rf "$SERVER_DIR"
                mkdir -p "$SERVER_DIR"
                cp -a "$backup"/. "$SERVER_DIR"/ 2>/dev/null || true
            fi

            return 1
        fi
    fi

    nginx_reload
    return 0
}

# ------------------------------------------------------------------
# 动作 2..6：证书
# ------------------------------------------------------------------
cert_install_settings () {   # $1=crt $2=key $3=mode
    out=$(php_cli set-cert --cert "$1" --key "$2" --mode "$3" --no-apply)
    rc=$?

    if [ "$rc" != "0" ]; then
        say "写设置失败：$out"
        return 1
    fi

    case "$out" in
        *degraded=1*) say "设置写进了急救覆盖文件（数据库不可用）：$out" ;;
    esac

    return 0
}

action_ssl_import () {   # $1=cert $2=key
    [ -f "$CERT_TOOL" ] || { say "找不到 $CERT_TOOL"; return 1; }

    installed=$(sh "$CERT_TOOL" import --cert "$1" --key "$2" --label upload 2>>"$LOG_FILE")
    rc=$?

    if [ "$rc" != "0" ] || [ -z "$installed" ]; then
        say "证书导入失败（旧证书继续用）"
        return 1
    fi

    crt=$(printf '%s\n' "$installed" | sed -n '1p')
    key=$(printf '%s\n' "$installed" | sed -n '2p')

    cert_install_settings "$crt" "$key" "upload" || return 1
    action_render
}

action_ssl_selfsigned () {   # $1=domain $2=days
    [ -f "$CERT_TOOL" ] || { say "找不到 $CERT_TOOL"; return 1; }

    installed=$(sh "$CERT_TOOL" selfsigned --domain "$1" --days "${2:-825}" --label selfsigned 2>>"$LOG_FILE")
    rc=$?

    if [ "$rc" != "0" ] || [ -z "$installed" ]; then
        say "自签证书生成失败"
        return 1
    fi

    crt=$(printf '%s\n' "$installed" | sed -n '1p')
    key=$(printf '%s\n' "$installed" | sed -n '2p')

    cert_install_settings "$crt" "$key" "selfsigned" || return 1
    action_render
}

acme_preflight () {   # $1=domain → 只写日志（不阻断：真正的判定交给 certbot）
    if command -v nslookup >/dev/null 2>&1; then
        if nslookup "$1" >"$AGENT_DIR/acme-dns.log" 2>&1; then
            say "ACME 预检：$1 能解析（详见 $AGENT_DIR/acme-dns.log）"
        else
            say "ACME 预检：$1 解析失败（域名还没解析到这台机器？certbot 大概率会失败）"
        fi
    fi
}

action_ssl_acme_issue () {   # $1=domain $2=email $3=dry_run
    [ -f "$CERT_TOOL" ] || { say "找不到 $CERT_TOOL"; return 1; }

    acme_preflight "$1"

    dry_arg=''
    [ "$3" = "1" ] && dry_arg='--dry-run'

    # shellcheck disable=SC2086
    if ! sh "$CERT_TOOL" acme-issue --domain "$1" --email "$2" $dry_arg 2>>"$LOG_FILE"; then
        say "ACME 申请失败（没有改动任何证书）"
        return 1
    fi

    if [ "$3" = "1" ]; then
        say "ACME dry-run 通过"
        sh "$CERT_TOOL" status --cert "$(state_get ssl_cert_file)" --key "$(state_get ssl_key_file)" --mode acme >>"$LOG_FILE" 2>&1 || true
        return 0
    fi

    crt="$CERT_DIR/acme-$1.crt"

    # install_pair 用的是带时间戳的文件名；从证书目录里找最新的一对。
    latest=$(ls -1t "$CERT_DIR"/acme-"$1"-*.crt 2>/dev/null | head -n1)
    [ -n "$latest" ] && crt="$latest"
    key="${crt%.crt}.key"

    cert_install_settings "$crt" "$key" "acme" || return 1
    action_render
}

action_ssl_acme_renew () {   # $1=domain $2=email $3=dry_run
    [ -f "$CERT_TOOL" ] || { say "找不到 $CERT_TOOL"; return 1; }

    dry_arg=''
    [ "$3" = "1" ] && dry_arg='--dry-run'

    # shellcheck disable=SC2086
    if ! sh "$CERT_TOOL" acme-renew --domain "$1" --email "$2" $dry_arg 2>>"$LOG_FILE"; then
        say "ACME 续期失败（**旧证书继续用**，看 acme.json / agent.log）"
        sh "$CERT_TOOL" status --cert "$(state_get ssl_cert_file)" --key "$(state_get ssl_key_file)" --mode acme >>"$LOG_FILE" 2>&1 || true
        return 1
    fi

    latest=$(ls -1t "$CERT_DIR"/acme-"$1"-*.crt 2>/dev/null | head -n1)

    if [ -n "$latest" ] && [ "$3" != "1" ]; then
        cert_install_settings "$latest" "${latest%.crt}.key" "acme" || return 1
        action_render
    fi

    sh "$CERT_TOOL" status --cert "$(state_get ssl_cert_file)" --key "$(state_get ssl_key_file)" --mode acme >>"$LOG_FILE" 2>&1 || true
    return 0
}

action_cert_status () {
    [ -f "$CERT_TOOL" ] || { say "找不到 $CERT_TOOL"; return 1; }

    sh "$CERT_TOOL" status \
        --cert "$(state_get ssl_cert_file)" \
        --key "$(state_get ssl_key_file)" \
        --mode "$(state_get ssl_mode)" >>"$LOG_FILE" 2>&1
}

# ------------------------------------------------------------------
# 请求处理
# ------------------------------------------------------------------
process_one () {   # $1=请求文件
    req="$1"
    id=$(basename "$req" .json)
    action=$(json_get "$req" action)

    say "收到请求 $id：$action"

    arg_cert=$(json_get "$req" cert)
    arg_key=$(json_get "$req" key)
    arg_domain=$(json_get "$req" domain)
    arg_email=$(json_get "$req" email)
    arg_mode=$(json_get "$req" mode)
    arg_days=$(json_get "$req" days)
    arg_dry=$(json_get "$req" dry_run)

    case "$action" in
        render)
            if action_render; then
                write_result "$id" "$action" 0 "配置已重新渲染并生效" '{"applied": true}'
            else
                write_result "$id" "$action" 1 "渲染 / nginx -t 失败：保留了上一份能跑的配置（面板没被打断）" '{"applied": false}'
            fi
            ;;
        ssl-import)
            if ! safe_path "$arg_cert" || ! safe_path "$arg_key"; then
                write_result "$id" "$action" 1 "请求里的证书路径不合法" '{}'
            elif action_ssl_import "$arg_cert" "$arg_key"; then
                write_result "$id" "$action" 0 "证书已安装并生效" '{"applied": true}'
            else
                write_result "$id" "$action" 1 "证书安装失败：旧证书继续用（看 agent.log / acme.json）" '{"applied": false}'
            fi
            ;;
        ssl-selfsigned)
            if ! safe_token "$arg_domain"; then
                write_result "$id" "$action" 1 "请求里的域名不合法" '{}'
            elif action_ssl_selfsigned "$arg_domain" "${arg_days:-825}"; then
                write_result "$id" "$action" 0 "自签证书已生成并生效（浏览器会提示不受信任，测试用）" '{"applied": true}'
            else
                write_result "$id" "$action" 1 "自签证书生成失败" '{"applied": false}'
            fi
            ;;
        ssl-acme-issue)
            if ! safe_token "$arg_domain" || ! safe_token "$arg_email"; then
                write_result "$id" "$action" 1 "请求里的域名 / 邮箱不合法" '{}'
            elif action_ssl_acme_issue "$arg_domain" "$arg_email" "${arg_dry:-0}"; then
                write_result "$id" "$action" 0 "Let's Encrypt 申请完成" "{\"dry_run\": $([ "${arg_dry:-0}" = "1" ] && printf 'true' || printf 'false')}"
            else
                write_result "$id" "$action" 1 "Let's Encrypt 申请失败（没有改动现有证书）" '{}'
            fi
            ;;
        ssl-acme-renew)
            if ! safe_token "$arg_domain" || ! safe_token "$arg_email"; then
                write_result "$id" "$action" 1 "请求里的域名 / 邮箱不合法" '{}'
            elif action_ssl_acme_renew "$arg_domain" "$arg_email" "${arg_dry:-0}"; then
                write_result "$id" "$action" 0 "续期检查完成" "{\"dry_run\": $([ "${arg_dry:-0}" = "1" ] && printf 'true' || printf 'false')}"
            else
                write_result "$id" "$action" 1 "续期失败：旧证书继续用（不会因为续期失败而掉 HTTPS）" '{}'
            fi
            ;;
        cert-status)
            action_cert_status
            write_result "$id" "$action" 0 "证书状态已刷新" '{}'
            ;;
        *)
            write_result "$id" "$action" 1 "根代理不认识这个动作" '{}'
            ;;
    esac

    rm -f "$req"
}

process_requests () {
    [ -d "$REQ_DIR" ] || return 0

    for req in "$REQ_DIR"/*.json; do
        [ -f "$req" ] || continue

        # 只处理"写完"的文件（panel_agent_request 是先 .tmp 再改名，
        # 这里再挡一次：太新的文件可能还在写）。
        process_one "$req"
    done

    return 0
}

# ------------------------------------------------------------------
# 目录准备（含权限：php-fpm 是 nobody，它要能写"请求文件"和"待安装的证书"）
# ------------------------------------------------------------------
#
# [!] 为什么要 chown：请求目录与 incoming/ 是**面板（php-fpm, nobody）**要写的，
#     而本脚本是 root —— root 先 mkdir 出来的目录默认是 root:root 755，面板就
#     写不进去了（表现是设置页上那句"队列目录不可写 / 证书暂存目录不可写"）。
#     容器里 php-fpm 的用户是 nobody(65534)；要换就在 compose 里给
#     MCLINK_PANEL_WEB_UID。
prepare_dirs () {
    mkdir -p "$CERT_DIR" "$CERT_DIR/incoming" "$AGENT_DIR" "$REQ_DIR" "$RES_DIR" \
        "$SERVER_DIR" 2>/dev/null || true

    web_uid="${MCLINK_PANEL_WEB_UID:-65534}"

    chown "$web_uid:$web_uid" "$REQ_DIR" "$CERT_DIR/incoming" 2>/dev/null || true
    chmod 755 "$REQ_DIR" "$CERT_DIR/incoming" "$RES_DIR" 2>/dev/null || true

    # 面板要**读**这几个（结果 / 状态 / 心跳 / 日志）：root 写的文件默认 644，
    # 这里再兜一次，免得 umask 收紧之后面板读不到。
    chmod 644 "$STATUS_FILE" "$LOG_FILE" "$CERT_DIR/status.json" 2>/dev/null || true

    return 0
}

# ------------------------------------------------------------------
# 心跳
# ------------------------------------------------------------------
write_heartbeat () {
    mkdir -p "$AGENT_DIR" 2>/dev/null || true

    nginx_ok='false'
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -x nginx >/dev/null 2>&1 && nginx_ok='true'
    elif [ -f /run/nginx.pid ]; then
        nginx_ok='true'
    fi

    tmp="$STATUS_FILE.tmp"
    {
        printf '{"schema": 1, "version": "%s", "pid": "%s", "started_at": "%s", "last_loop": "%s", "last_loop_ts": %s, "nginx_ok": %s, "interval": %s}\n' \
            "$AGENT_VERSION" "$$" "$STARTED_AT" \
            "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(date -u '+%s')" \
            "$nginx_ok" "$INTERVAL"
    } > "$tmp" 2>/dev/null && mv "$tmp" "$STATUS_FILE" 2>/dev/null

    return 0
}

# ------------------------------------------------------------------
# ACME 自动续期检查
# ------------------------------------------------------------------
maybe_renew () {
    mode=$(state_get ssl_mode)
    [ "$mode" = "acme" ] || return 0

    domain=$(state_get bound_domain)
    email=$(state_get acme_email)
    [ -n "$domain" ] || return 0

    action_cert_status

    days=$(sed -n 's/.*"days_left"[[:space:]]*:[[:space:]]*\([0-9-]*\).*/\1/p' "$CERT_DIR/status.json" 2>/dev/null | head -n1)
    [ -n "$days" ] || return 0

    if [ "$days" -lt "$ACME_RENEW_DAYS" ] 2>/dev/null; then
        say "ACME 证书还剩 $days 天（阈值 $ACME_RENEW_DAYS）→ 自动续期"
        action_ssl_acme_renew "$domain" "$email" "0" || say "自动续期失败（旧证书继续用）"
    else
        say "ACME 证书还剩 $days 天，暂时不用续期"
    fi

    return 0
}

# ------------------------------------------------------------------
# 启动初始化（一次性）
# ------------------------------------------------------------------
startup_init () {
    prepare_dirs

    STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

    if [ "$START_DELAY" -gt 0 ] 2>/dev/null; then
        say "启动延迟 ${START_DELAY}s（等 nginx / 数据库起来）"
        sleep "$START_DELAY"
    fi

    # 1) 老部署的环境变量播种一次（只在设置里还没有那个键时写）
    seed=$(php_cli seed-from-env)
    say "环境变量播种：$seed"

    # 2) 证书状态
    action_cert_status || true

    # 3) 启动渲染：**每次都渲染**，不要"已有配置就跳过"。
    #
    #    为什么：/etc/nginx/mclink-servers 在**镜像里**（不是卷），所以每次
    #    `docker compose up -d --force-recreate` 之后容器里那份配置都会回到
    #    构建期的默认（纯 HTTP、没有安全入口）。而真正的设置在数据库 / JSON 里
    #    —— 如果这里跳过渲染，面板就会带着"默认的、没有安全入口的"配置一直跑下去
    #    （安全入口失效，直到有人手动点一次保存）。那是必须避免的。
    #
    #    安全性：渲染仍然走"候选 → nginx -t → 通过才替换"；失败时镜像里那份
    #    默认配置原样保留，容器照样起得来。
    say "按当前设置渲染 nginx 配置（镜像里那份只是构建期默认）"
    action_render || say "启动渲染失败：保留现有配置（面板仍然能打开，看 agent.log）"

    # 4) 续期检查（启动也查一次）
    maybe_renew || true

    write_heartbeat
    say "根代理启动完成（v$AGENT_VERSION，循环间隔 ${INTERVAL}s）"
}

# ------------------------------------------------------------------
# 主循环
# ------------------------------------------------------------------
loop () {
    STARTED_AT="${STARTED_AT:-$(date -u '+%Y-%m-%dT%H:%M:%SZ')}"
    prepare_dirs

    last_heartbeat=0
    last_acme=0
    now=$(date -u '+%s')

    # 启动时的续期检查已经在 init 里做过：这里把计时起点挪到"现在 + 间隔"。
    last_acme=$((now + ACME_CHECK_INTERVAL - 60))

    while :; do
        process_requests

        now=$(date -u '+%s')

        if [ $((now - last_heartbeat)) -ge "$HEARTBEAT_EVERY" ]; then
            write_heartbeat
            last_heartbeat=$now
        fi

        if [ $((now - last_acme)) -ge "$ACME_CHECK_INTERVAL" ]; then
            last_acme=$now
            maybe_renew || true
            action_cert_status || true
        fi

        sleep "$INTERVAL"
    done
}

case "${1:-loop}" in
    init) startup_init ;;
    loop) loop ;;
    once)  # 手工排一次队列（排障用）：mclink-agent once
        STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        process_requests
        ;;
    renew-now)
        STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        maybe_renew
        ;;
    *)
        printf 'usage: %s [init|loop|once|renew-now]\n' "$0" >&2
        exit 2
        ;;
esac

exit 0
