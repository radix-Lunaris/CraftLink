#!/bin/sh
# ============================================================
#  CraftLink 面板 —— 证书工具（容器内，root 运行）
# ============================================================
#
#  为什么是 shell 而不是 PHP：
#    * 私钥不该经过网页进程（php-fpm 是 nobody）；
#    * 证书的深度校验（密钥配对、域名覆盖、到期时间）只有真 openssl 说了算；
#    * 仓库的 PHP 门禁有一份内置函数白名单，openssl_* / exec 不在里面
#      （见 tests/panel_static_test.py 与 panel/lib/panel_ssl.php 的说明）。
#
#  用法（这个脚本本身不读面板设置：谁调用它谁把参数传全）：
#
#      mclink-cert.sh status   --cert DIR|<path> --key <path> --mode <mode>
#      mclink-cert.sh inspect  --cert <crt> --key <key>          # 人类可读
#      mclink-cert.sh selfsigned --domain D [--days 825] [--label selfsigned]
#      mclink-cert.sh import   --cert <crt> --key <key> [--label upload]
#      mclink-cert.sh acme-issue --domain D --email E [--dry-run]
#      mclink-cert.sh acme-renew [--domain D] [--dry-run]
#      mclink-cert.sh prune    [--keep 5]
#
#  退出码：0 = 成功；非 0 = 失败（失败时**绝不覆盖已经装好的证书**）。
#
#  环境变量（都有默认值，绝不硬编码安装路径）：
#      MCLINK_PANEL_CERT_DIR   证书目录（默认 /var/www/certs）
#      MCLINK_ACME_WEBROOT     HTTP-01 校验目录（默认 /var/www/acme）
#      MCLINK_CERTBOT_BIN      certbot 可执行文件（默认 certbot）
#      MCLINK_OPENSSL_BIN      openssl（默认 openssl）
# ============================================================

set -u

OPENSSL="${MCLINK_OPENSSL_BIN:-openssl}"
CERTBOT="${MCLINK_CERTBOT_BIN:-certbot}"
CERT_DIR="${MCLINK_PANEL_CERT_DIR:-/var/www/certs}"
ACME_WEBROOT="${MCLINK_ACME_WEBROOT:-/var/www/acme}"
CERTBOT_DIR="${MCLINK_CERTBOT_DIR:-$CERT_DIR/certbot}"
STATUS_FILE="$CERT_DIR/status.json"
ACME_FILE="$CERT_DIR/acme.json"
INCOMING_DIR="$CERT_DIR/incoming"

log () { printf '[mclink-cert] %s\n' "$*" >&2; }
die () { log "ERROR: $*"; exit 1; }

need () { command -v "$1" >/dev/null 2>&1 || die "找不到可执行文件：$1"; }

# 文件名安全化：域名 / 标签里只留 [A-Za-z0-9._-]
slug () {
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-64
}

mkdirs () {
    mkdir -p "$CERT_DIR" "$ACME_WEBROOT/.well-known/acme-challenge" 2>/dev/null || true
}

# ------------------------------------------------------------------
# JSON 里用到的转义（值都是路径 / 指纹 / 日期，不会有换行）
# ------------------------------------------------------------------
json_escape () {
    printf '%s' "$1" | tr -d '\r\n' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# ------------------------------------------------------------------
# 一张证书的字段（都用 openssl 问出来）
# ------------------------------------------------------------------
cert_field () {   # $1=crt $2=-subject|-issuer|-enddate|-startdate
    "$OPENSSL" x509 -in "$1" -noout "$2" 2>/dev/null | sed -e "s/^[a-zA-Z]*=//" | tr -d '\r'
}

cert_san () {     # 证书里覆盖的域名（SAN，没有就退回 CN）
    crt="$1"
    san=$("$OPENSSL" x509 -in "$crt" -noout -ext subjectAltName 2>/dev/null \
        | grep -i 'DNS:' | tr ',' '\n' | sed -e 's/.*DNS://' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        | grep -v '^$' | tr '\n' ' ')
    if [ -z "$san" ]; then
        san=$(cert_field "$crt" -subject | sed -e 's/.*CN *= *//' -e 's/,.*//')
    fi
    printf '%s' "$san"
}

key_matches () {  # $1=crt $2=key → 0 = 公钥一致
    [ -f "$2" ] || return 1
    a=$("$OPENSSL" x509 -in "$1" -noout -pubkey 2>/dev/null | "$OPENSSL" dgst -sha256 2>/dev/null | awk '{print $NF}')
    b=$("$OPENSSL" pkey -in "$2" -pubout 2>/dev/null | "$OPENSSL" dgst -sha256 2>/dev/null | awk '{print $NF}')
    [ -n "$a" ] && [ "$a" = "$b" ]
}

is_self_signed () {  # 0 = 自签（subject == issuer）
    crt="$1"
    s=$(cert_field "$crt" -subject)
    i=$(cert_field "$crt" -issuer)
    [ -n "$s" ] && [ "$s" = "$i" ]
}

# 还剩多少整天（-1 = 已经过期；用 openssl -checkend 二分，busybox 的 date
# 解析不了 openssl 打印的 "Nov 5 12:00:00 2026 GMT"）。
cert_days_left () {
    crt="$1"

    if ! "$OPENSSL" x509 -in "$crt" -noout -checkend 0 >/dev/null 2>&1; then
        printf '%s' "-1"
        return 0
    fi

    lo=0
    hi=3650

    while [ "$lo" -lt "$hi" ]; do
        mid=$(( (lo + hi + 1) / 2 ))
        if "$OPENSSL" x509 -in "$crt" -noout -checkend $((mid * 86400)) >/dev/null 2>&1; then
            lo=$mid
        else
            hi=$((mid - 1))
        fi
    done

    printf '%s' "$lo"
}

cert_sha256 () {
    "$OPENSSL" x509 -in "$1" -noout -fingerprint -sha256 2>/dev/null \
        | sed -e 's/.*=//' | tr -d '\r'
}

# 深度校验：证书能解析 + 私钥能解析 + 两者配对 + 没过期
# 返回 0 时把结论写进全局变量（POSIX sh 没有返回值，用文件也行，这里用变量）
VALID_SUBJECT=''
VALID_ISSUER=''
VALID_NOT_BEFORE=''
VALID_NOT_AFTER=''
VALID_DAYS=''
VALID_DOMAINS=''
VALID_SELF=''
VALID_FP=''
VALID_KEY_MATCH=''

validate_pair () {   # $1=crt $2=key [--allow-expired]
    crt="$1"
    key="$2"
    allow_expired="${3:-}"

    [ -f "$crt" ] || { log "证书文件不存在：$crt"; return 1; }
    [ -f "$key" ] || { log "私钥文件不存在：$key"; return 1; }

    "$OPENSSL" x509 -in "$crt" -noout -subject >/dev/null 2>&1 \
        || { log "证书解析失败（不是 PEM 证书？）：$crt"; return 1; }

    "$OPENSSL" pkey -in "$key" -noout >/dev/null 2>&1 \
        || { log "私钥解析失败（是不是带密码 / 不是 PEM？）：$key"; return 1; }

    if ! key_matches "$crt" "$key"; then
        log "证书和私钥不是一对（公钥不一致）"
        return 1
    fi

    VALID_DAYS=$(cert_days_left "$crt")

    if [ "$VALID_DAYS" -lt 0 ] && [ "$allow_expired" != "--allow-expired" ]; then
        log "证书已经过期（$(cert_field "$crt" -enddate)）"
        return 1
    fi

    VALID_SUBJECT=$(cert_field "$crt" -subject)
    VALID_ISSUER=$(cert_field "$crt" -issuer)
    VALID_NOT_BEFORE=$(cert_field "$crt" -startdate)
    VALID_NOT_AFTER=$(cert_field "$crt" -enddate)
    VALID_DOMAINS=$(cert_san "$crt")
    VALID_FP=$(cert_sha256 "$crt")
    VALID_KEY_MATCH='true'

    if is_self_signed "$crt"; then
        VALID_SELF='true'
    else
        VALID_SELF='false'
    fi

    return 0
}

# ------------------------------------------------------------------
# 安装一对证书（原子：先写 .new，校验通过才改名；失败绝不碰旧文件）
# ------------------------------------------------------------------
install_pair () {   # $1=crt 源 $2=key 源 $3=label [--allow-expired]
    src_crt="$1"
    src_key="$2"
    label=$(slug "$3")

    validate_pair "$src_crt" "$src_key" "${4:-}" || return 1

    # 内容一样就不重复装：`certbot renew` 每 6 小时跑一次，绝大多数时候
    # "没到期 → 什么都没变"，重复安装只会在证书目录里堆一堆一模一样的副本。
    prev=$(ls -1t "$CERT_DIR/$label"-*.crt 2>/dev/null | head -n1)

    if [ -n "$prev" ] && [ -f "${prev%.crt}.key" ] \
        && cmp -s "$src_crt" "$prev" && cmp -s "$src_key" "${prev%.crt}.key"; then
        log "证书内容和现有的一样，跳过安装：$prev"
        printf '%s\n%s\n' "$prev" "${prev%.crt}.key"
        return 0
    fi

    stamp=$(date -u '+%Y%m%d-%H%M%S')
    base="$label-$stamp"
    dst_crt="$CERT_DIR/$base.crt"
    dst_key="$CERT_DIR/$base.key"

    mkdirs

    cp "$src_crt" "$dst_crt.new" || { log "写 $dst_crt.new 失败"; return 1; }
    cp "$src_key" "$dst_key.new" || { log "写 $dst_key.new 失败"; rm -f "$dst_crt.new"; return 1; }

    chmod 644 "$dst_crt.new" 2>/dev/null || true
    chmod 600 "$dst_key.new" 2>/dev/null || true

    mv "$dst_crt.new" "$dst_crt" || { log "改名失败：$dst_crt"; return 1; }
    mv "$dst_key.new" "$dst_key" || { log "改名失败：$dst_key"; return 1; }

    log "已安装：$dst_crt / $dst_key（$VALID_SUBJECT，$VALID_DAYS 天后到期）"

    # 给调用方（agent）用的两个路径
    printf '%s\n%s\n' "$dst_crt" "$dst_key"
    return 0
}

# ------------------------------------------------------------------
# status.json —— 面板只读这个文件（它不解析 PEM）
# ------------------------------------------------------------------
write_status () {   # $1=active cert path $2=active key path $3=mode
    active_cert="${1:-}"
    active_key="${2:-}"
    mode="${3:-off}"

    mkdirs

    tmp="$STATUS_FILE.tmp"
    {
        printf '{\n'
        printf '  "schema": 1,\n'
        printf '  "generated_at": "%s",\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf '  "error": "%s",\n' "$(json_escape "${STATUS_ERROR:-}")"
        printf '  "active": {'
        printf '"cert_file": "%s", "key_file": "%s", "mode": "%s"' \
            "$(json_escape "$active_cert")" "$(json_escape "$active_key")" "$(json_escape "$mode")"
        printf '},\n'
        printf '  "certs": [\n'

        first=1

        for crt in "$CERT_DIR"/*.crt; do
            [ -f "$crt" ] || continue
            key="${crt%.crt}.key"

            if ! validate_pair "$crt" "$key" "--allow-expired" >/dev/null 2>&1; then
                if [ "$first" -eq 0 ]; then printf ',\n'; fi
                first=0
                printf '    {"cert_file": "%s", "key_file": "%s", "name": "%s", "error": "invalid", "days_left": null}' \
                    "$(json_escape "$crt")" "$(json_escape "$key")" "$(json_escape "$(basename "$crt" .crt)")"
                continue
            fi

            active='false'
            [ "$crt" = "$active_cert" ] && active='true'

            if [ "$first" -eq 0 ]; then printf ',\n'; fi
            first=0

            printf '    {"name": "%s", "cert_file": "%s", "key_file": "%s", "subject": "%s", "issuer": "%s", "not_before": "%s", "not_after": "%s", "days_left": %s, "self_signed": %s, "domains": "%s", "sha256": "%s", "key_match": %s, "active": %s, "error": ""}' \
                "$(json_escape "$(basename "$crt" .crt)")" \
                "$(json_escape "$crt")" "$(json_escape "$key")" \
                "$(json_escape "$VALID_SUBJECT")" "$(json_escape "$VALID_ISSUER")" \
                "$(json_escape "$VALID_NOT_BEFORE")" "$(json_escape "$VALID_NOT_AFTER")" \
                "$VALID_DAYS" "$VALID_SELF" \
                "$(json_escape "$VALID_DOMAINS")" "$(json_escape "$VALID_FP")" \
                "$VALID_KEY_MATCH" "$active"
        done

        # 生效的那一对要是在证书目录之外（例如挂进来的 /etc/letsencrypt），
        # 也要单独列一条，否则页面上会显示"没有这张证书的信息"。
        case "$active_cert" in
            "$CERT_DIR"/*) : ;;
            "")
                : ;;
            *)
                if validate_pair "$active_cert" "$active_key" "--allow-expired" >/dev/null 2>&1; then
                    if [ "$first" -eq 0 ]; then printf ',\n'; fi
                    first=0
                    printf '    {"name": "active-external", "cert_file": "%s", "key_file": "%s", "subject": "%s", "issuer": "%s", "not_before": "%s", "not_after": "%s", "days_left": %s, "self_signed": %s, "domains": "%s", "sha256": "%s", "key_match": %s, "active": true, "error": "", "external": true}' \
                        "$(json_escape "$active_cert")" "$(json_escape "$active_key")" \
                        "$(json_escape "$VALID_SUBJECT")" "$(json_escape "$VALID_ISSUER")" \
                        "$(json_escape "$VALID_NOT_BEFORE")" "$(json_escape "$VALID_NOT_AFTER")" \
                        "$VALID_DAYS" "$VALID_SELF" \
                        "$(json_escape "$VALID_DOMAINS")" "$(json_escape "$VALID_FP")" \
                        "$VALID_KEY_MATCH"
                fi
                ;;
        esac

        printf '\n  ],\n'
        printf '  "acme": %s\n' "$(read_acme_json)"
        printf '}\n'
    } > "$tmp" || { log "写 $tmp 失败"; return 1; }

    mv "$tmp" "$STATUS_FILE" || { log "改名失败：$STATUS_FILE"; return 1; }
    chmod 644 "$STATUS_FILE" 2>/dev/null || true

    log "状态已写入：$STATUS_FILE"
    return 0
}

# acme.json 的内容（没有就是空对象），直接嵌进 status.json
read_acme_json () {
    if [ -f "$ACME_FILE" ]; then
        tr -d '\n' < "$ACME_FILE"
    else
        printf '{}'
    fi
}

write_acme_state () {   # $1=domain $2=email $3=ok(0/1) $4=message $5=dry_run
    mkdirs

    tmp="$ACME_FILE.tmp"
    printf '{"domain": "%s", "email": "%s", "last_attempt_at": "%s", "last_ok": %s, "last_error": "%s", "dry_run": %s}\n' \
        "$(json_escape "$1")" "$(json_escape "$2")" \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        "$([ "$3" = "0" ] && printf 'true' || printf 'false')" \
        "$(json_escape "$4")" \
        "$([ "${5:-0}" = "1" ] && printf 'true' || printf 'false')" \
        > "$tmp" || return 1

    mv "$tmp" "$ACME_FILE" || return 1
    chmod 644 "$ACME_FILE" 2>/dev/null || true
}

# ------------------------------------------------------------------
# 子命令
# ------------------------------------------------------------------
cmd_selfsigned () {
    domain=''; days='825'; label='selfsigned'

    while [ $# -gt 0 ]; do
        case "$1" in
            --domain) domain="$2"; shift 2 ;;
            --days) days="$2"; shift 2 ;;
            --label) label="$2"; shift 2 ;;
            *) die "selfsigned: 不认识的参数 $1" ;;
        esac
    done

    need "$OPENSSL"
    [ -n "$domain" ] || die "selfsigned: 需要 --domain"

    mkdirs

    tmp_crt="$CERT_DIR/.selfsigned.crt"
    tmp_key="$CERT_DIR/.selfsigned.key"

    # SAN 里同时写域名、localhost 和 127.0.0.1：本地测试时用哪个地址都不报
    # "证书不匹配主机名"。
    "$OPENSSL" req -x509 -newkey rsa:2048 -nodes \
        -keyout "$tmp_key" -out "$tmp_crt" \
        -days "$days" -subj "/CN=$domain" \
        -addext "subjectAltName=DNS:$domain,DNS:localhost,IP:127.0.0.1" \
        >/dev/null 2>&1 \
        || { rm -f "$tmp_crt" "$tmp_key"; die "openssl 自签失败"; }

    chmod 600 "$tmp_key" 2>/dev/null || true

    install_pair "$tmp_crt" "$tmp_key" "$label" || { rm -f "$tmp_crt" "$tmp_key"; return 1; }

    rm -f "$tmp_crt" "$tmp_key"
    return 0
}

cmd_import () {
    cert=''; key=''; label='upload'

    while [ $# -gt 0 ]; do
        case "$1" in
            --cert) cert="$2"; shift 2 ;;
            --key) key="$2"; shift 2 ;;
            --label) label="$2"; shift 2 ;;
            *) die "import: 不认识的参数 $1" ;;
        esac
    done

    need "$OPENSSL"
    [ -n "$cert" ] && [ -n "$key" ] || die "import: 需要 --cert 和 --key"

    install_pair "$cert" "$key" "$label"
}

cmd_status () {
    cert=''; key=''; mode=''

    while [ $# -gt 0 ]; do
        case "$1" in
            --cert) cert="$2"; shift 2 ;;
            --key) key="$2"; shift 2 ;;
            --mode) mode="$2"; shift 2 ;;
            *) die "status: 不认识的参数 $1" ;;
        esac
    done

    need "$OPENSSL"
    write_status "$cert" "$key" "${mode:-off}"
}

cmd_inspect () {
    cert=''; key=''

    while [ $# -gt 0 ]; do
        case "$1" in
            --cert) cert="$2"; shift 2 ;;
            --key) key="$2"; shift 2 ;;
            *) die "inspect: 不认识的参数 $1" ;;
        esac
    done

    need "$OPENSSL"
    [ -n "$cert" ] || die "inspect: 需要 --cert"

    echo "cert       : $cert"
    echo "key        : ${key:--}"
    echo "subject    : $(cert_field "$cert" -subject)"
    echo "issuer     : $(cert_field "$cert" -issuer)"
    echo "not_before : $(cert_field "$cert" -startdate)"
    echo "not_after  : $(cert_field "$cert" -enddate)"
    echo "days_left  : $(cert_days_left "$cert")"
    echo "domains    : $(cert_san "$cert")"
    echo "sha256     : $(cert_sha256 "$cert")"

    if is_self_signed "$cert"; then echo "self_signed: true"; else echo "self_signed: false"; fi

    if [ -n "$key" ]; then
        if key_matches "$cert" "$key"; then echo "key_match  : true"; else echo "key_match  : false"; fi
    fi
}

# certbot 的公共参数（状态目录放在证书卷里：容器重建之后 ACME 账号和续期
# 配置都还在，不然每次重建都要重新注册账号）
certbot_args () {
    printf '%s' "--config-dir $CERTBOT_DIR --work-dir $CERTBOT_DIR/work --logs-dir $CERTBOT_DIR/logs"
}

cmd_acme_issue () {
    domain=''; email=''; dry='0'

    while [ $# -gt 0 ]; do
        case "$1" in
            --domain) domain="$2"; shift 2 ;;
            --email) email="$2"; shift 2 ;;
            --dry-run) dry='1'; shift ;;
            *) die "acme-issue: 不认识的参数 $1" ;;
        esac
    done

    [ -n "$domain" ] || die "acme-issue: 需要 --domain"
    [ -n "$email" ] || die "acme-issue: 需要 --email（Let's Encrypt 账号邮箱）"

    # shellcheck disable=SC2086
    need "$CERTBOT"
    need "$OPENSSL"

    mkdir -p "$ACME_WEBROOT/.well-known/acme-challenge" "$CERTBOT_DIR" 2>/dev/null || true

    extra=''
    [ "$dry" = "1" ] && extra='--dry-run'

    log "certbot certonly --webroot -d $domain（dry_run=$dry）"

    # [!] 不要写成 `certbot ... | sed ...`：管道的退出码是 sed 的，certbot 失败
    #     会被吞掉（"续期失败了却显示成功"这种 bug 就是这么来的）。
    "$CERTBOT" certonly --webroot -w "$ACME_WEBROOT" -d "$domain" \
        --email "$email" --agree-tos --non-interactive --no-eff-email \
        $(certbot_args) $extra > "$CERT_DIR/.certbot.log" 2>&1
    rc=$?

    sed -e 's/^/[certbot] /' "$CERT_DIR/.certbot.log" >&2

    if [ "$rc" != "0" ]; then
        write_acme_state "$domain" "$email" 1 "certbot certonly 失败（看上面的输出）" "$dry"
        return 1
    fi

    if [ "$dry" = "1" ]; then
        write_acme_state "$domain" "$email" 0 "" "1"
        log "dry-run 通过：流程没问题，但**没有**安装任何证书"
        return 0
    fi

    cmd_acme_deploy "$domain" "$email" '0'
}

# 把 certbot 签下来的 lineage 装进证书目录（先校验、后原子替换）
cmd_acme_deploy () {
    domain="$1"
    email="${2:-}"
    dry="${3:-0}"

    live="$CERTBOT_DIR/live/$domain"

    [ -f "$live/fullchain.pem" ] || { log "找不到 $live/fullchain.pem"; write_acme_state "$domain" "$email" 1 "找不到签发的证书" "$dry"; return 1; }
    [ -f "$live/privkey.pem" ] || { log "找不到 $live/privkey.pem"; write_acme_state "$domain" "$email" 1 "找不到签发的私钥" "$dry"; return 1; }

    if ! install_pair "$live/fullchain.pem" "$live/privkey.pem" "acme-$domain"; then
        # [!] 这一步失败**不会**动已经装好的证书：install_pair 是先校验再改名。
        write_acme_state "$domain" "$email" 1 "新证书校验失败，已保留旧证书" "$dry"
        return 1
    fi

    write_acme_state "$domain" "$email" 0 "" "$dry"
    return 0
}

cmd_acme_renew () {
    domain=''; dry='0'; email=''

    while [ $# -gt 0 ]; do
        case "$1" in
            --domain) domain="$2"; shift 2 ;;
            --email) email="$2"; shift 2 ;;
            --dry-run) dry='1'; shift ;;
            *) die "acme-renew: 不认识的参数 $1" ;;
        esac
    done

    need "$CERTBOT"
    need "$OPENSSL"

    mkdir -p "$ACME_WEBROOT/.well-known/acme-challenge" "$CERTBOT_DIR" 2>/dev/null || true

    extra=''
    [ "$dry" = "1" ] && extra='--dry-run'

    log "certbot renew（dry_run=$dry）"

    # 同上：管道的退出码会骗人，这里分开取 certbot 的退出码。
    "$CERTBOT" renew --webroot -w "$ACME_WEBROOT" --non-interactive \
        $(certbot_args) $extra > "$CERT_DIR/.certbot.log" 2>&1
    rc=$?

    sed -e 's/^/[certbot] /' "$CERT_DIR/.certbot.log" >&2

    if [ "$rc" != "0" ]; then
        write_acme_state "$domain" "$email" 1 "certbot renew 失败（旧证书继续用，看上面的输出）" "$dry"
        return 1
    fi

    if [ "$dry" = "1" ]; then
        write_acme_state "$domain" "$email" 0 "" "1"
        log "dry-run 通过（没有改动任何证书）"
        return 0
    fi

    if [ -z "$domain" ]; then
        write_acme_state "" "$email" 0 "" "0"
        return 0
    fi

    cmd_acme_deploy "$domain" "$email" '0'
}

# 只保留最近 N 对（默认 5 对）自己装的证书，避免证书目录里堆一堆旧文件。
cmd_prune () {
    keep='5'

    while [ $# -gt 0 ]; do
        case "$1" in
            --keep) keep="$2"; shift 2 ;;
            *) die "prune: 不认识的参数 $1" ;;
        esac
    done

    [ "$keep" -ge 1 ] 2>/dev/null || keep=5

    list=$(ls -1t "$CERT_DIR"/*.crt 2>/dev/null | sed -n "$((keep + 1)),\$p")

    for crt in $list; do
        key="${crt%.crt}.key"
        log "清理旧证书：$crt"
        rm -f "$crt" "$key"
    done

    # incoming/ 里放的是还没被代理取走的上传文件：超过 1 天的删掉。
    if [ -d "$INCOMING_DIR" ]; then
        find "$INCOMING_DIR" -type f -mtime +1 -exec rm -f {} \; 2>/dev/null || true
    fi

    return 0
}

# ------------------------------------------------------------------
main () {
    cmd="${1:-}"
    [ $# -gt 0 ] && shift

    case "$cmd" in
        status)      cmd_status "$@" ;;
        inspect)     cmd_inspect "$@" ;;
        selfsigned)  cmd_selfsigned "$@" ;;
        import)      cmd_import "$@" ;;
        acme-issue)  cmd_acme_issue "$@" ;;
        acme-renew)  cmd_acme_renew "$@" ;;
        acme-deploy) cmd_acme_deploy "$@" ;;
        prune)       cmd_prune "$@" ;;
        ''|-h|--help|help)
            sed -n '2,40p' "$0" | sed -e 's/^# \{0,1\}//'
            ;;
        *)
            die "不认识的子命令：$cmd（status / inspect / selfsigned / import / acme-issue / acme-renew / acme-deploy / prune）"
            ;;
    esac
}

main "$@"
