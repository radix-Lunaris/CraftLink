#!/usr/bin/env bash
# ============================================================
#  CraftLink 面板 M4（SSL + 域名绑定 + 自带 nginx）—— 端到端冒烟测试
# ============================================================
#
#  它验的是"设置 → 模板 → 生效"这条链路里**能在这台机器上验的全部**：
#
#     1. 六个 M4 设置项的校验规则（域名 / 模式 / 路径 / 邮箱）
#     2. 渲染出来的 nginx 配置内容（端口、安全入口、Host 守卫、ACME 通道、
#        HTTPS + 跳转、急救 server 块）
#     3. **真 nginx** 把这些配置跑起来之后的行为（用 curl 一条条打）：
#        安全入口内外 / 域名对不对 / 强制 HTTPS 跳转 / 自签证书握手 /
#        ACME 校验通道 / 急救入口
#     4. 根代理（panel-agent.sh）的完整链路：请求文件 → 渲染 → nginx -t →
#        安装 → reload → 结果文件；以及**失败时保留旧配置**
#     5. 证书工具（mclink-cert.sh）：自签、导入、状态、ACME（用桩 certbot）
#     6. 急救命令：reset-entry / unbind-domain / ssl-off / 急救覆盖清除
#
#  用法：
#
#      PHP_BIN=/path/to/php bash docs/panel-ssl-nginx-smoke.sh
#
#  可选环境变量：
#      NGINX_BIN       真 nginx 可执行文件（找不到就跳过第 3 组，会明确写 SKIP）
#      NGINX_CONF_DIR  mime.types / fastcgi_params 所在目录（默认自动找）
#      OPENSSL_BIN     openssl（默认 PATH 里的）
#      KEEP_TMP=1      保留临时目录（排查用）
#
#  这里**不需要** MySQL、不需要 docker、不需要域名、不需要 Let's Encrypt：
#  数据库指向连不上的地址（正好验"数据库不可用时的降级"），ACME 用桩 certbot。
#
#  退出码：0 = 全过（允许有 SKIP）；1 = 有 FAIL。
# ============================================================

set -u

PHP_BIN="${PHP_BIN:-php}"
OPENSSL_BIN="${OPENSSL_BIN:-openssl}"
NGINX_BIN="${NGINX_BIN:-$(command -v nginx 2>/dev/null || true)}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PANEL="$REPO/panel"
TPL_DIR="$PANEL/nginx/templates"

PASS=0
FAIL=0
SKIP=0

pass () { PASS=$((PASS + 1)); echo "  [PASS] $1"; }
skip () { SKIP=$((SKIP + 1)); echo "  [SKIP] $1"; }

fail () {
    FAIL=$((FAIL + 1))
    echo "  [FAIL] $1"
    [ -n "${2:-}" ] && echo "         -- $2"
}

check () { # check <名称> <期望> <实际>
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "期望 [$2] 实际 [$3]"; fi
}

check_contains () { # check_contains <名称> <文件> <子串>
    if grep -qF -- "$3" "$2" 2>/dev/null; then pass "$1"; else fail "$1" "在 $(basename "$2") 里找不到 [$3]"; fi
}

# 命令输出（字符串）里找子串 —— check_contains 要的是文件
check_has () { # check_has <名称> <文本> <子串>
    case "$2" in
        *"$3"*) pass "$1" ;;
        *) fail "$1" "在输出里找不到 [$3]，实际输出：$(printf %s "$2" | head -c 200)" ;;
    esac
}

check_missing () { # check_missing <名称> <文件> <子串>
    if grep -qF -- "$3" "$2" 2>/dev/null; then fail "$1" "不该出现却出现了 [$3]"; else pass "$1"; fi
}

check_file () { # check_file <名称> <文件>
    if [ -f "$2" ]; then pass "$1"; else fail "$1" "文件不存在：$2"; fi
}

if ! command -v "$PHP_BIN" >/dev/null 2>&1; then
    echo "找不到 PHP：$PHP_BIN（用 PHP_BIN=... 指定）" >&2
    exit 2
fi

if ! command -v "$OPENSSL_BIN" >/dev/null 2>&1; then
    echo "找不到 openssl：$OPENSSL_BIN" >&2
    exit 2
fi

# nginx 的 mime.types / fastcgi_params 在哪（容器里是 /etc/nginx）
NGINX_CONF_DIR="${NGINX_CONF_DIR:-}"

if [ -z "$NGINX_CONF_DIR" ] && [ -n "$NGINX_BIN" ]; then
    for candidate in \
        "$(dirname "$NGINX_BIN")/../../etc/nginx" \
        "$(dirname "$NGINX_BIN")/../etc/nginx" \
        "/etc/nginx"; do
        if [ -f "$candidate/mime.types" ]; then
            NGINX_CONF_DIR="$(cd "$candidate" && pwd)"
            break
        fi
    done
fi

TMP_ROOT="${TMP_ROOT:-${TMPDIR:-/tmp}}"
mkdir -p "$TMP_ROOT" 2>/dev/null || TMP_ROOT=/tmp
ROOT="$(mktemp -d "$TMP_ROOT/mclink-m4-smoke.XXXXXX")"
CONFIG_DIR="$ROOT/config"
CERT_DIR="$ROOT/certs"
AGENT_DIR="$CERT_DIR/agent"
NGINX_DIR="$ROOT/nginx-live"
ACME_DIR="$ROOT/acme"
WEBROOT="$PANEL/web"
S="$ROOT/settings/panel-settings.json"

# 端口让内核随便给一个空的（随机数撞上别的进程 = 一个假失败，不值当）
pick_port () {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

PHP_PORT="$(pick_port)"
HTTP_PORT="$(pick_port)"
HTTPS_PORT="$(pick_port)"
RESCUE_PORT="$(pick_port)"

mkdir -p "$CONFIG_DIR" "$CERT_DIR" "$AGENT_DIR" "$NGINX_DIR" "$ACME_DIR/.well-known/acme-challenge" "$(dirname "$S")"

echo "CraftLink 面板 M4 冒烟测试（SSL + 域名绑定 + 自带 nginx）"
echo "  PHP      : $("$PHP_BIN" -r 'echo PHP_VERSION;')"
echo "  面板     : $PANEL"
echo "  nginx    : ${NGINX_BIN:-（没有：第 3 组会 SKIP）}"
echo "  openssl  : $(command -v "$OPENSSL_BIN")"
echo "  临时目录 : $ROOT"
echo "  端口     : php=$PHP_PORT http=$HTTP_PORT https=$HTTPS_PORT rescue=$RESCUE_PORT"

# ------------------------------------------------------------------
# 环境：面板所有外部依赖都指向"连不上"的地址
# ------------------------------------------------------------------
export MCLINK_PANEL_SETTINGS_FILE="$S"
export MCLINK_PANEL_OVERRIDES_FILE="$CONFIG_DIR/panel-emergency-overrides.json"
export MCLINK_CONFIG_DIR="$CONFIG_DIR"
export MCLINK_ROOT="$ROOT"
export MCLINK_PANEL_CERT_DIR="$CERT_DIR"
export MCLINK_PANEL_AGENT_DIR="$AGENT_DIR"
export MCLINK_NGINX_SERVER_DIR="$NGINX_DIR"
export MCLINK_NGINX_TEMPLATE_DIR="$TPL_DIR"
export MCLINK_PANEL_WEBROOT="$WEBROOT"
export MCLINK_ACME_WEBROOT="$ACME_DIR"
export MCLINK_NGINX_HTTP_PORT="$HTTP_PORT"
export MCLINK_NGINX_HTTPS_PORT="$HTTPS_PORT"
export MCLINK_PANEL_HTTPS_PORT="$HTTPS_PORT"
export MCLINK_PANEL_RESCUE_PORT="$RESCUE_PORT"
export MCLINK_DB_HOST="127.0.0.1"
export MCLINK_DB_PORT="1"
export MCLINK_RELAY_API_URL="http://127.0.0.1:1"
export MCLINK_PANEL_TITLE="M4 冒烟面板"

# [!] 这台机器上没有 php-fpm（生产容器里才有），所以把渲染出来的 PHP 后端
#     切成 proxy 指向 php 自带的 web server。生产默认是 fastcgi，不受影响。
export MCLINK_NGINX_BACKEND="proxy"
export MCLINK_NGINX_BACKEND_ADDR="127.0.0.1:$PHP_PORT"

PHP_SERVER_PID=""
NGINX_PID=""

cleanup () {
    [ -n "$NGINX_PID" ] && kill "$NGINX_PID" 2>/dev/null
    [ -n "$PHP_SERVER_PID" ] && kill "$PHP_SERVER_PID" 2>/dev/null
    wait 2>/dev/null

    if [ "${KEEP_TMP:-}" = "1" ]; then
        echo
        echo "（KEEP_TMP=1：临时目录保留在 $ROOT）"
        return
    fi

    rm -rf "$ROOT"
}
trap cleanup EXIT

write_settings () { # write_settings <settings 对象>
    cat > "$S" <<JSON
{
    "schema": 1,
    "panel_version": "2.11.45",
    "settings": $1
}
JSON
}

render () { # render → 渲染到 $NGINX_DIR（额外参数会透传给 CLI）
    "$PHP_BIN" "$PANEL/bin/mclink-panel.php" render --dir "$NGINX_DIR" "$@" 2>&1
}

php_eval () { # php_eval <代码>
    "$PHP_BIN" -r "require '$PANEL/lib/common.php'; $1" 2>&1
}

# ============================================================
echo
echo "== 1. M4 设置项：注册表 / 默认值 / 校验 =="
# ============================================================

write_settings '{}'

check "六个 M4 设置项都进了注册表" "6" "$(php_eval 'echo count(array_intersect(panel_setting_keys(), ["bound_domain","force_https","ssl_mode","ssl_cert_file","ssl_key_file","acme_email"]));')"
check "占位钩子已经空了（M4 全部实现）" "0" "$(php_eval 'echo count(panel_setting_placeholders());')"
check "ssl_mode 默认 off（升级后行为不变）" "off" "$(php_eval 'echo panel_setting_default("ssl_mode");')"
check "bound_domain 默认空（不绑定域名）" "" "$(php_eval 'echo panel_setting_default("bound_domain");')"
check "force_https 默认关" "0" "$(php_eval 'echo panel_setting_default("force_https");')"
check "分组名不再写「未实现」" "0" "$(php_eval 'echo (int) (strpos(implode(",", panel_setting_groups()), "未实现") !== false);')"

# 校验矩阵：值 => 期望（ok / bad）
check "域名：panel.example.com 通过" "" "$(php_eval 'echo panel_setting_validate("bound_domain", "panel.example.com");')"
check "域名：从地址栏粘贴的 https://panel.example.com/ 也认" "" "$(php_eval 'echo panel_setting_validate("bound_domain", "https://panel.example.com/");')"
check "域名：*.example.com 通配通过" "" "$(php_eval 'echo panel_setting_validate("bound_domain", "*.example.com");')"
check "域名：带路径的输入被截成主机名（从地址栏粘贴不会报错）" "example.com" "$(php_eval 'echo panel_domain_normalize("example.com/panel");')"
check "域名：非法字符被拒" "1" "$(php_eval 'echo (int) (panel_setting_validate("bound_domain", "bad_domain!!") !== "");')"
check "域名：中间带通配符被拒" "1" "$(php_eval 'echo (int) (panel_setting_validate("bound_domain", "a.*.b.com") !== "");')"
check "ssl_mode：acme 通过" "" "$(php_eval 'echo panel_setting_validate("ssl_mode", "acme");')"
check "ssl_mode：垃圾值被拒" "1" "$(php_eval 'echo (int) (panel_setting_validate("ssl_mode", "letsencrypt") !== "");')"
check "force_https：1 通过" "" "$(php_eval 'echo panel_setting_validate("force_https", "1");')"
check "force_https：yes 被拒" "1" "$(php_eval 'echo (int) (panel_setting_validate("force_https", "yes") !== "");')"
check "证书路径：绝对路径通过" "" "$(php_eval 'echo panel_setting_validate("ssl_cert_file", "/var/www/certs/a.crt");')"
check "证书路径：相对路径被拒" "1" "$(php_eval 'echo (int) (panel_setting_validate("ssl_cert_file", "certs/a.crt") !== "");')"
check "证书路径：.. 被拒" "1" "$(php_eval 'echo (int) (panel_setting_validate("ssl_cert_file", "/var/www/../etc/passwd") !== "");')"
check "邮箱：空通过（不用 ACME 时不用填）" "" "$(php_eval 'echo panel_setting_validate("acme_email", "");')"
check "邮箱：合法通过" "" "$(php_eval 'echo panel_setting_validate("acme_email", "a@b.com");')"
check "邮箱：非法被拒" "1" "$(php_eval 'echo (int) (panel_setting_validate("acme_email", "not-an-email") !== "");')"
check "域名规范化：大小写 + 端口 + 协议都去掉" "panel.example.com" "$(php_eval 'echo panel_domain_normalize("HTTPS://Panel.Example.COM:19102/x?y=1");')"
check "域名匹配：通配匹配子域" "1" "$(php_eval 'echo (int) panel_domain_match("a.example.com", "*.example.com");')"
check "域名匹配：通配不匹配别的域" "0" "$(php_eval 'echo (int) panel_domain_match("a.example.org", "*.example.com");')"
check "域名匹配：带端口的 Host 也能匹配" "1" "$(php_eval 'echo (int) panel_domain_match("panel.example.com:19102", "panel.example.com");')"
check "未认证返回码 login 在 nginx 层按 404（否则安全入口白配）" "404" "$(php_eval 'echo panel_nginx_guard_code();')"

# ============================================================
echo
echo "== 2. 渲染：安全入口 + 域名绑定（无 TLS）=="
# ============================================================

write_settings '{"url_suffix":"mclink-panel","bound_domain":"panel.example.com","unauthenticated_code":"404","listen_port":"19102","listen_ip":"127.0.0.1"}'

render > /dev/null

check_file "渲染出了 panel-server.conf" "$NGINX_DIR/panel-server.conf"
check_file "渲染出了 panel-locations.inc" "$NGINX_DIR/panel-locations.inc"
check_file "渲染出了 panel-backend.inc" "$NGINX_DIR/panel-backend.inc"
check_file "渲染出了急救用的 panel-rescue-locations.inc" "$NGINX_DIR/panel-rescue-locations.inc"

check_contains "容器内监听固定 HTTP 端口（设置里的端口由 compose 映射）" "$NGINX_DIR/panel-server.conf" "listen 0.0.0.0:$HTTP_PORT;"
check_contains "server_name 用的是绑定域名" "$NGINX_DIR/panel-server.conf" "server_name panel.example.com;"
check_contains "Host 守卫：其他 Host 一律拒绝" "$NGINX_DIR/panel-server.conf" 'if ($host !~* "^panel\.example\.com$")'
check_contains "安全入口守卫：入口外一律未认证返回码" "$NGINX_DIR/panel-server.conf" 'if ($uri !~ ^(/mclink-panel(/|$)|/\.well-known/acme-challenge/))'
check_contains "安全入口：前缀在原样地址上被剥掉" "$NGINX_DIR/panel-locations.inc" 'rewrite ^/mclink-panel/(.*)$ /$1 last;'
check_contains "安全入口：不带斜杠时补一个 302" "$NGINX_DIR/panel-locations.inc" "location = /mclink-panel {"
check_contains "ACME 校验通道存在（明文 HTTP 直达）" "$NGINX_DIR/panel-locations.inc" "location ^~ /.well-known/acme-challenge/ {"
check_contains "急救 server 块监听容器内急救端口" "$NGINX_DIR/panel-server.conf" "listen 0.0.0.0:$RESCUE_PORT;"
check_contains "急救 server 块带 /healthz" "$NGINX_DIR/panel-server.conf" "location = /healthz {"
check_missing "无 TLS 时不生成 ssl_certificate" "$NGINX_DIR/panel-server.conf" "ssl_certificate"
check_missing "无 TLS 时不生成跳转" "$NGINX_DIR/panel-http-redirect.inc" "return 301"
awk '/^# ---- 急救入口/{flag=1} flag' "$NGINX_DIR/panel-server.conf" > "$ROOT/rescue-block.conf"
check_missing "急救 server 块里没有域名守卫（否则救不了命）" "$ROOT/rescue-block.conf" "域名绑定：只有"
check_missing "急救 server 块里没有安全入口守卫" "$ROOT/rescue-block.conf" "安全入口守卫"

# 渲染是"值不对就降级"，不是"写一份起不来的配置"
write_settings '{"url_suffix":"secret","force_https":"1","ssl_mode":"upload","ssl_cert_file":"/nonexistent/nope.crt","ssl_key_file":"/nonexistent/nope.key"}'
OUT="$(render)"
printf '%s
' "$OUT" > "$ROOT/render-warn.txt"
check_contains "证书文件不存在：明确警告" "$ROOT/render-warn.txt" "读不到"
check_missing "证书文件不存在：不生成引用它的 ssl_certificate" "$NGINX_DIR/panel-server.conf" "ssl_certificate"
check_contains "证书没就绪：跳转文件被渲染成一句「没启用」的注释" "$NGINX_DIR/panel-http-redirect.inc" "本次没有启用"
check_missing "证书没就绪：跳转文件里没有 301" "$NGINX_DIR/panel-http-redirect.inc" "return 301"

# ============================================================
echo
echo "== 3. 渲染：自签证书 + 强制 HTTPS =="
# ============================================================

CERT_OUT="$(MCLINK_PANEL_CERT_DIR="$CERT_DIR" MCLINK_ACME_WEBROOT="$ACME_DIR" \
    sh "$PANEL/bin/mclink-cert.sh" selfsigned --domain panel.example.com --days 30 --label smoke 2>&1)"
CERT_CRT="$(printf '%s\n' "$CERT_OUT" | grep -E '^/.*\.crt$' | head -n1)"
CERT_KEY="$(printf '%s\n' "$CERT_OUT" | grep -E '^/.*\.key$' | head -n1)"

check_file "自签证书生成成功" "$CERT_CRT"
check_file "自签私钥生成成功" "$CERT_KEY"
check "自签私钥权限是 600" "600" "$(stat -c '%a' "$CERT_KEY" 2>/dev/null || echo '?')"

write_settings "{\"url_suffix\":\"mclink-panel\",\"bound_domain\":\"panel.example.com\",\"force_https\":\"1\",\"ssl_mode\":\"upload\",\"ssl_cert_file\":\"$CERT_CRT\",\"ssl_key_file\":\"$CERT_KEY\"}"

render > /dev/null

check_contains "TLS 打开：生成 ssl_certificate" "$NGINX_DIR/panel-server.conf" "ssl_certificate     $CERT_CRT;"
check_contains "TLS 打开：生成 ssl_certificate_key" "$NGINX_DIR/panel-server.conf" "ssl_certificate_key $CERT_KEY;"
check_contains "TLS 打开：HTTPS server 块监听容器内 HTTPS 端口" "$NGINX_DIR/panel-server.conf" "listen 0.0.0.0:$HTTPS_PORT ssl;"
check_contains "强制 HTTPS：HTTP 侧 301 跳转" "$NGINX_DIR/panel-http-redirect.inc" "return 301 https://\$host:$HTTPS_PORT\$request_uri;"
check_contains "强制 HTTPS：HTTP 侧仍然放行 ACME 校验" "$NGINX_DIR/panel-http-redirect.inc" "acme-challenge"
check_contains "HTTPS 侧也有安全入口守卫" "$NGINX_DIR/panel-server.conf" 'if ($uri !~ ^(/mclink-panel(/|$)'

# ---- 默认端口：没有任何环境变量覆盖时，容器内必须是 80 / 443 ----
# （生产 compose 就是这么跑的；本地冒烟为了避让端口才用了环境变量覆盖）
(
    unset MCLINK_NGINX_HTTP_PORT MCLINK_NGINX_HTTPS_PORT MCLINK_PANEL_HTTPS_PORT
    "$PHP_BIN" "$PANEL/bin/mclink-panel.php" render --dir "$NGINX_DIR" > /dev/null 2>&1
)
check_contains "默认（无覆盖）：容器内 HTTP 监听 80" "$NGINX_DIR/panel-server.conf" "listen 0.0.0.0:80;"

(
    unset MCLINK_NGINX_HTTP_PORT MCLINK_NGINX_HTTPS_PORT MCLINK_PANEL_HTTPS_PORT
    write_settings "{\"ssl_mode\":\"upload\",\"ssl_cert_file\":\"$CERT_CRT\",\"ssl_key_file\":\"$CERT_KEY\",\"force_https\":\"1\"}"
    "$PHP_BIN" "$PANEL/bin/mclink-panel.php" render --dir "$NGINX_DIR" > /dev/null 2>&1
)
check_contains "默认（无覆盖）：容器内 HTTPS 监听 443" "$NGINX_DIR/panel-server.conf" "listen 0.0.0.0:443 ssl;"
check_missing "默认 443：跳转地址里不带端口（浏览器默认 443）" "$NGINX_DIR/panel-http-redirect.inc" "https://\$host:"

# 恢复成这一节要验的那份设置
write_settings '{"url_suffix":"mclink-panel","bound_domain":"panel.example.com","unauthenticated_code":"404","listen_port":"19102","listen_ip":"127.0.0.1"}'
render > /dev/null

# ---- 生产主配置 + 默认渲染结果：真 nginx 能不能吃下去 ----
if [ -n "$NGINX_BIN" ] && [ -x "$NGINX_BIN" ] && [ -n "$NGINX_CONF_DIR" ] && [ -f "$NGINX_CONF_DIR/mime.types" ]; then
    PROD_PREFIX="$ROOT/ngx-prod"
    mkdir -p "$PROD_PREFIX/logs" "$PROD_PREFIX/tmp"

    # 主配置照抄 panel/nginx/nginx-main.conf，只把路径指到临时环境，
    # 并去掉 user 行（这台机器上没有 nginx 用户；容器里是有的）
    cp "$NGINX_CONF_DIR/mime.types" "$NGINX_CONF_DIR/fastcgi_params" "$PROD_PREFIX/" 2>/dev/null

    # 这台机器上 nginx 的编译期默认临时目录（/var/lib/nginx/...）不存在，
    # 容器里是存在的 —— 这里显式指到临时目录再测。
    sed -e "s|/etc/nginx/mclink-servers|$NGINX_DIR|g" \
        -e "s|/etc/nginx/mime.types|$NGINX_CONF_DIR/mime.types|g" \
        -e "s|^user .*|# user removed for local test|" \
        -e "s|/var/log/nginx|$PROD_PREFIX/logs|g" \
        -e "s|/run/nginx.pid|$PROD_PREFIX/nginx.pid|g" \
        -e "/^http {/a\
    client_body_temp_path $PROD_PREFIX/body;\
    proxy_temp_path       $PROD_PREFIX/proxy;\
    fastcgi_temp_path     $PROD_PREFIX/fastcgi;\
    uwsgi_temp_path       $PROD_PREFIX/uwsgi;\
    scgi_temp_path        $PROD_PREFIX/scgi;" \
        "$PANEL/nginx/nginx-main.conf" > "$PROD_PREFIX/main.conf"

    if "$NGINX_BIN" -t -p "$PROD_PREFIX" -c "$PROD_PREFIX/main.conf" > "$ROOT/nginx-prod-t.log" 2>&1; then
        pass "生产主配置（nginx-main.conf）+ 渲染结果：真 nginx -t 通过"
    else
        fail "生产主配置 + 渲染结果：真 nginx -t 通过" "$(tail -n 3 "$ROOT/nginx-prod-t.log")"
    fi
fi


# ============================================================
echo
echo "== 4. 真 nginx：把渲染出来的配置跑起来，用 curl 一条条打 =="
# ============================================================

if [ -z "$NGINX_BIN" ] || [ ! -x "$NGINX_BIN" ]; then
    skip "没有 nginx（NGINX_BIN=... 指定一个就能验这一组）"
else
    if [ -z "$NGINX_CONF_DIR" ] || [ ! -f "$NGINX_CONF_DIR/mime.types" ]; then
        skip "找不到 mime.types（用 NGINX_CONF_DIR=... 指定）"
    else
        # 后端：php 自带 web server（这里没有 php-fpm；proxy 后端只在冒烟里用）
        MCLINK_DB_HOST=127.0.0.1 MCLINK_DB_PORT=1 MCLINK_RELAY_API_URL=http://127.0.0.1:1 \
        "$PHP_BIN" -S "127.0.0.1:$PHP_PORT" -t "$WEBROOT" > "$ROOT/php-server.log" 2>&1 &
        PHP_SERVER_PID=$!

        # 等后端真的在听（php -S 起来要一点点时间；不等的话下面全是假 502）
        BACKEND_READY=0
        for _try in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
            if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PHP_PORT/index.php?action=login"; then
                BACKEND_READY=1
                break
            fi
            sleep 0.3
        done

        if [ "$BACKEND_READY" = "1" ]; then
            pass "php 后端（冒烟用）已就绪：127.0.0.1:$PHP_PORT"
        else
            fail "php 后端（冒烟用）起不来" "$(tail -n 3 "$ROOT/php-server.log" 2>/dev/null)"
        fi

        # ACME 校验文件（证明明文通道真的能取到）
        printf 'acme-token-ok' > "$ACME_DIR/.well-known/acme-challenge/smoke-token"

        NGX_PREFIX="$ROOT/ngx"
        mkdir -p "$NGX_PREFIX/logs" "$NGX_PREFIX/tmp"
        cp "$NGINX_CONF_DIR/mime.types" "$NGINX_CONF_DIR/fastcgi_params" "$NGX_PREFIX/" 2>/dev/null

        sed -e "s|@CANDIDATE_DIR@|$NGINX_DIR|g" \
            -e "s|@MIME_TYPES@|$NGINX_CONF_DIR/mime.types|g" \
            -e "s|@TEST_PREFIX@|$NGX_PREFIX/tmp|g" \
            "$PANEL/nginx/nginx-test.conf.template" > "$NGX_PREFIX/main.conf"

        if ! "$NGINX_BIN" -t -p "$NGX_PREFIX" -c "$NGX_PREFIX/main.conf" > "$ROOT/nginx-t.log" 2>&1; then
            fail "真 nginx -t 通过（渲染出来的配置是合法 nginx 配置）" "$(tail -n 3 "$ROOT/nginx-t.log")"
        else
            pass "真 nginx -t 通过（渲染出来的配置是合法 nginx 配置）"

            "$NGINX_BIN" -p "$NGX_PREFIX" -c "$NGX_PREFIX/main.conf" > "$ROOT/nginx.log" 2>&1 &
            NGINX_PID=$!
            sleep 1.2

            BASE="http://127.0.0.1:$HTTP_PORT"
            BODY="$ROOT/body.out"

            code () { curl -s -o "$BODY" -w '%{http_code}' "$@"; }

            # ---- 阶段 A：域名绑定 + 安全入口 + TLS（不强制跳转）----
            # 这一阶段用 section 3 渲染的配置（force_https=1）之前，先把它改成
            # "有 TLS 但不强制"，这样明文 HTTP 上能验完入口与域名规则。
            write_settings "{\"url_suffix\":\"mclink-panel\",\"bound_domain\":\"panel.example.com\",\"force_https\":\"0\",\"ssl_mode\":\"upload\",\"ssl_cert_file\":\"$CERT_CRT\",\"ssl_key_file\":\"$CERT_KEY\"}"
            render > /dev/null
            "$NGINX_BIN" -p "$NGX_PREFIX" -c "$NGX_PREFIX/main.conf" -s reload >/dev/null 2>&1
            sleep 0.7

            check "对域名 + 入口内：200（登录页）" "200" \
                "$(code -H 'Host: panel.example.com' "$BASE/mclink-panel/index.php?action=login")"
            check "对域名 + 入口外（/index.php）：404" "404" \
                "$(code -H 'Host: panel.example.com' "$BASE/index.php?action=login")"
            check "对域名 + 入口根：302 到带斜杠地址" "302" \
                "$(code -H 'Host: panel.example.com' "$BASE/mclink-panel")"
            check "对域名 + 入口内静态资源：200" "200" \
                "$(code -H 'Host: panel.example.com' "$BASE/mclink-panel/assets/style.css")"
            check "错域名（Host: evil.test）：404（不是登录页）" "404" \
                "$(code -H 'Host: evil.test' "$BASE/mclink-panel/index.php?action=login")"
            check "错域名 + 入口外：也是 404" "404" \
                "$(code -H 'Host: evil.test' "$BASE/whatever")"
            check "ACME 校验通道不吃安全入口：200 + 正确内容" "acme-token-ok" \
                "$(curl -s -H 'Host: panel.example.com' "$BASE/.well-known/acme-challenge/smoke-token")"

            # ---- 急救入口（上面三样全配上了，它必须照样能用）----
            check "急救入口 /healthz：200（不受安全入口 / 域名 / 跳转影响）" "200" \
                "$(code "http://127.0.0.1:$RESCUE_PORT/healthz")"
            check "急救入口：登录页 200（Host 随便写）" "200" \
                "$(code -H 'Host: nonsense' "http://127.0.0.1:$RESCUE_PORT/index.php?action=login")"
            check "急救入口：不走 HTTPS 跳转" "200" \
                "$(code "http://127.0.0.1:$RESCUE_PORT/index.php?action=login")"

            # ---- TLS 握手（自签证书）----
            if command -v curl >/dev/null 2>&1; then
                TLSOUT="$(curl -sk -o "$BODY" -w '%{http_code}' \
                    --resolve "panel.example.com:$HTTPS_PORT:127.0.0.1" \
                    "https://panel.example.com:$HTTPS_PORT/mclink-panel/index.php?action=login" 2>&1)"

                check "HTTPS（自签）：入口内 200" "200" "$(printf '%s' "$TLSOUT" | tr -d '\n')"
                check "HTTPS：错域名依然 404" "404" \
                    "$(curl -sk -o /dev/null -w '%{http_code}' -H 'Host: evil.test' "https://127.0.0.1:$HTTPS_PORT/mclink-panel/index.php?action=login")"

                # 服务端拿出来的证书必须就是我们生成的那一张（CN=panel.example.com）
                SERVED_CN="$(printf '' | "$OPENSSL_BIN" s_client -connect "127.0.0.1:$HTTPS_PORT" \
                    -servername panel.example.com 2>/dev/null | "$OPENSSL_BIN" x509 -noout -subject 2>/dev/null)"
                check "HTTPS：nginx 端上来的就是我们的证书（CN=panel.example.com）" "1" \
                    "$(printf '%s' "$SERVED_CN" | grep -q 'CN *= *panel.example.com' && echo 1 || echo 0)"
            fi

            # ---- 阶段 B：强制 HTTPS（HTTP 只留 ACME + 301）----
            write_settings "{\"url_suffix\":\"mclink-panel\",\"bound_domain\":\"panel.example.com\",\"force_https\":\"1\",\"ssl_mode\":\"upload\",\"ssl_cert_file\":\"$CERT_CRT\",\"ssl_key_file\":\"$CERT_KEY\"}"
            render > /dev/null
            "$NGINX_BIN" -p "$NGX_PREFIX" -c "$NGX_PREFIX/main.conf" -s reload >/dev/null 2>&1
            sleep 0.7

            check "强制 HTTPS：入口内明文请求 301" "301" \
                "$(code -H 'Host: panel.example.com' "$BASE/mclink-panel/index.php?action=login")"
            curl -s -D "$ROOT/headers.txt" -o /dev/null -H 'Host: panel.example.com' "$BASE/mclink-panel/" >/dev/null 2>&1
            check "301 跳转目标 = https://<域名>:<HTTPS端口>/mclink-panel/" "1" \
                "$(grep -ci "location: https://panel.example.com:$HTTPS_PORT/mclink-panel/" "$ROOT/headers.txt" 2>/dev/null || echo 0)"
            check "强制 HTTPS：ACME 校验仍然走明文 200" "acme-token-ok" \
                "$(curl -s -H 'Host: panel.example.com' "$BASE/.well-known/acme-challenge/smoke-token")"
            check "强制 HTTPS：HTTPS 侧照常 200" "200" \
                "$(curl -sk -o /dev/null -w '%{http_code}' --resolve "panel.example.com:$HTTPS_PORT:127.0.0.1" \
                    "https://panel.example.com:$HTTPS_PORT/mclink-panel/index.php?action=login")"
            check "强制 HTTPS：急救入口仍然是明文 200（不跳转）" "200" \
                "$(code "http://127.0.0.1:$RESCUE_PORT/index.php?action=login")"

            # ---- 阶段 C：热加载成"没有安全入口 / 不绑域名 / 不用 HTTPS" ----
            write_settings '{"unauthenticated_code":"login","listen_port":"19102"}'
            render > /dev/null
            "$NGINX_BIN" -p "$NGX_PREFIX" -c "$NGX_PREFIX/main.conf" -s reload >/dev/null 2>&1
            sleep 0.7

            check "热加载后：没有安全入口时 /index.php?action=login 直接 200" "200" \
                "$(code -H 'Host: any.example.org' "$BASE/index.php?action=login")"
            check_missing "热加载后：渲染结果里没有 Host 守卫" "$NGINX_DIR/panel-server.conf" "域名绑定：只有"
            check_missing "热加载后：渲染结果里没有 HTTPS 跳转文件被引用" "$NGINX_DIR/panel-server.conf" "panel-http-redirect.inc"

            # ---- 阶段 D：未认证返回码 403 在 nginx 层生效（入口外）----
            write_settings '{"url_suffix":"mclink-panel","unauthenticated_code":"403"}'
            render > /dev/null
            "$NGINX_BIN" -p "$NGX_PREFIX" -c "$NGX_PREFIX/main.conf" -s reload >/dev/null 2>&1
            sleep 0.7

            check "未认证返回码 403：入口外返回 403（可配置）" "403" \
                "$(code "$BASE/index.php?action=login")"
            check "未认证返回码 403：入口内照常 200" "200" \
                "$(code "$BASE/mclink-panel/index.php?action=login")"

            # ---- 阶段 E：未认证返回码 444（直接断连接）----
            write_settings '{"url_suffix":"mclink-panel","unauthenticated_code":"444"}'
            render > /dev/null
            "$NGINX_BIN" -p "$NGX_PREFIX" -c "$NGX_PREFIX/main.conf" -s reload >/dev/null 2>&1
            sleep 0.7

            check "未认证返回码 444：入口外连接被直接关闭（curl 000/空回复）" "1" \
                "$(R="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/index.php" 2>/dev/null)"; [ -z "$R" ] || [ "$R" = "000" ] && echo 1 || echo 0)"

            kill "$NGINX_PID" 2>/dev/null
            wait "$NGINX_PID" 2>/dev/null
            NGINX_PID=""
        fi

        kill "$PHP_SERVER_PID" 2>/dev/null
        wait "$PHP_SERVER_PID" 2>/dev/null
        PHP_SERVER_PID=""
    fi
fi

# ============================================================
echo
echo "== 5. 根代理：请求 → 渲染 → nginx -t → 安装 → reload → 结果 =="
# ============================================================

# 桩 nginx：只认三种调用（-t 候选 / -t 正式 / -s reload），把动作记下来。
STUB="$ROOT/bin"
mkdir -p "$STUB"

cat > "$STUB/nginx" <<'STUBEOF'
#!/bin/sh
# 桩 nginx：MCLINK_STUB_FAIL=1 时让 -t 失败（模拟"配置有问题"）
LOG="${MCLINK_STUB_LOG:-/tmp/nginx-stub.log}"
case "$*" in
    *"-t"*)
        echo "test $*" >> "$LOG"
        if [ "${MCLINK_STUB_FAIL:-0}" = "1" ]; then
            echo "nginx: [emerg] stub failure" >&2
            exit 1
        fi
        ;;
    *"-s reload"*)
        echo "reload" >> "$LOG"
        ;;
esac
exit 0
STUBEOF
chmod +x "$STUB/nginx"

export MCLINK_STUB_LOG="$ROOT/nginx-stub.log"
AGENT_ENV="MCLINK_PHP_BIN=$PHP_BIN MCLINK_PANEL_BIN_DIR=$PANEL/bin MCLINK_NGINX_BIN=$STUB/nginx"
AGENT_ENV="$AGENT_ENV MCLINK_NGINX_TEST_CONF=$PANEL/nginx/nginx-test.conf.template"
AGENT_ENV="$AGENT_ENV MCLINK_AGENT_START_DELAY=0 MCLINK_AGENT_INTERVAL=1"

agent () { # agent <init|once|renew-now>
    env $AGENT_ENV sh "$PANEL/bin/panel-agent.sh" "$1" 2>&1
}

write_settings '{"url_suffix":"mclink-panel","bound_domain":"panel.example.com","unauthenticated_code":"404"}'
render > /dev/null

: > "$MCLINK_STUB_LOG"

# ---- 5.1 正常渲染请求 ----
REQ_ID="smoke-render-1"
mkdir -p "$AGENT_DIR/requests"
cat > "$AGENT_DIR/requests/$REQ_ID.json" <<JSON
{"schema":1,"id":"$REQ_ID","action":"render","created_at":"2026-10-05T00:00:00Z","actor":"smoke","args":{"reason":"smoke"}}
JSON

agent once > "$ROOT/agent-once.log" 2>&1
check_file "代理写了结果文件" "$AGENT_DIR/results/$REQ_ID.json"
check_contains "结果里 ok=true" "$AGENT_DIR/results/$REQ_ID.json" '"ok": true'
check_contains "代理跑过 nginx -t" "$MCLINK_STUB_LOG" "test"
check_contains "代理 reload 了 nginx" "$MCLINK_STUB_LOG" "reload"
check "请求文件被消费掉（队列不会重复执行）" "0" "$(ls "$AGENT_DIR/requests" | wc -l | tr -d ' ')"
check_file "候选配置与生效配置分离（ngnix-lastgood 备份目录存在）" "$AGENT_DIR/nginx-candidate/panel-server.conf"

# ---- 5.1b 启动初始化：**每次都按当前设置重渲染**（容器重建后不能停在被镜像里
#      那份"构建期默认配置"上 —— 那会让安全入口失效）----
# 先把生效配置改成一份明显不一样的（模拟"镜像里的默认配置"），再跑 init，
# 它必须被当前设置重新渲染回来。
printf '# 这是脏配置（模拟镜像里的构建期默认）\n' > "$NGINX_DIR/panel-server.conf"
printf '# 脏的\n' > "$NGINX_DIR/panel-locations.inc"

agent init > "$ROOT/agent-init.log" 2>&1

check_contains "启动 init：日志里说明是按当前设置渲染" "$ROOT/agent-init.log" "按当前设置渲染"
check "启动 init：请求目录存在（面板要能往里写）" "1" "$([ -d "$AGENT_DIR/requests" ] && echo 1 || echo 0)"
check "启动 init：待安装证书目录存在（incoming/）" "1" "$([ -d "$CERT_DIR/incoming" ] && echo 1 || echo 0)"
check_contains "启动 init：脏配置被重新渲染回安全入口版本" "$NGINX_DIR/panel-server.conf" "mclink-panel"
check_missing "启动 init：脏注释已经不在生效配置里" "$NGINX_DIR/panel-server.conf" "这是脏配置"

# ---- 5.2 nginx -t 失败：必须保留旧配置 ----
cp "$NGINX_DIR/panel-server.conf" "$ROOT/before-fail.conf"
: > "$MCLINK_STUB_LOG"

REQ_ID="smoke-render-fail"
cat > "$AGENT_DIR/requests/$REQ_ID.json" <<JSON
{"schema":1,"id":"$REQ_ID","action":"render","created_at":"2026-10-05T00:00:00Z","actor":"smoke","args":{}}
JSON

MCLINK_STUB_FAIL=1 agent once > "$ROOT/agent-fail.log" 2>&1 || true

check_contains "nginx -t 失败：结果是 ok=false" "$AGENT_DIR/results/$REQ_ID.json" '"ok": false'
if diff -q "$ROOT/before-fail.conf" "$NGINX_DIR/panel-server.conf" >/dev/null 2>&1; then
    pass "nginx -t 失败：生效配置一个字节都没动（面板不会被打不开）"
else
    fail "nginx -t 失败：生效配置一个字节都没动" "配置被改写了"
fi

# ---- 5.3 上传证书 → 代理安装 → 写设置 → 重新渲染 ----
CERT2="$ROOT/upload.crt"
KEY2="$ROOT/upload.key"
"$OPENSSL_BIN" req -x509 -newkey rsa:2048 -nodes -keyout "$KEY2" -out "$CERT2" -days 30 \
    -subj "/CN=upload.example.com" -addext "subjectAltName=DNS:upload.example.com" >/dev/null 2>&1

mkdir -p "$CERT_DIR/incoming"
cp "$CERT2" "$CERT_DIR/incoming/up.crt"
cp "$KEY2" "$CERT_DIR/incoming/up.key"

REQ_ID="smoke-ssl-import"
cat > "$AGENT_DIR/requests/$REQ_ID.json" <<JSON
{"schema":1,"id":"$REQ_ID","action":"ssl-import","created_at":"2026-10-05T00:00:00Z","actor":"smoke","args":{"cert":"$CERT_DIR/incoming/up.crt","key":"$CERT_DIR/incoming/up.key","label":"upload"}}
JSON

agent once > "$ROOT/agent-import.log" 2>&1

check_contains "证书导入：结果 ok=true" "$AGENT_DIR/results/$REQ_ID.json" '"ok": true'
check "证书目录里出现了安装好的证书" "1" "$(ls "$CERT_DIR"/upload-*.crt 2>/dev/null | wc -l | tr -d ' ')"
check "证书目录里出现了安装好的私钥" "1" "$(ls "$CERT_DIR"/upload-*.key 2>/dev/null | wc -l | tr -d ' ')"
check "装好的私钥权限是 600" "1" "$(ls -l "$CERT_DIR"/upload-*.key 2>/dev/null | grep -c '^-rw-------')"

INSTALLED_CRT="$(ls -1t "$CERT_DIR"/upload-*.crt 2>/dev/null | head -n1)"

# 数据库连不上 → 设置会写进"急救覆盖"（这就是"重置必须真的生效"的兜底）
check_contains "数据库不可用：设置写进了急救覆盖并明确标注 degraded" "$ROOT/agent-import.log" "degraded=1"
check "急救覆盖文件里有 ssl_cert_file" "1" "$(grep -c 'ssl_cert_file' "$CONFIG_DIR/panel-emergency-overrides.json" 2>/dev/null || echo 0)"
check "急救覆盖里的证书路径 = 刚装好的那一张" "$INSTALLED_CRT" \
    "$(php_eval 'echo panel_ssl_cert_file();')"
check "ssl_mode 变成 upload" "upload" "$(php_eval 'echo panel_ssl_mode();')"
check_contains "证书装好之后重新渲染（HTTPS 块出现）" "$NGINX_DIR/panel-server.conf" "ssl_certificate     $INSTALLED_CRT;"

# ---- 5.4 自签（走代理）----
REQ_ID="smoke-selfsigned"
cat > "$AGENT_DIR/requests/$REQ_ID.json" <<JSON
{"schema":1,"id":"$REQ_ID","action":"ssl-selfsigned","created_at":"2026-10-05T00:00:00Z","actor":"smoke","args":{"domain":"panel.example.com","days":"30","label":"selfsigned"}}
JSON

agent once > "$ROOT/agent-selfsigned.log" 2>&1
check_contains "自签：结果 ok=true" "$AGENT_DIR/results/$REQ_ID.json" '"ok": true'
check "ssl_mode 变成 selfsigned" "selfsigned" "$(php_eval 'echo panel_ssl_mode();')"

# ---- 5.5 证书状态（status.json 面板能读）----
REQ_ID="smoke-cert-status"
cat > "$AGENT_DIR/requests/$REQ_ID.json" <<JSON
{"schema":1,"id":"$REQ_ID","action":"cert-status","created_at":"2026-10-05T00:00:00Z","actor":"smoke","args":{}}
JSON

agent once > "$ROOT/agent-status.log" 2>&1
check_file "status.json 写出来了" "$CERT_DIR/status.json"
check "面板能从 status.json 读出剩余天数（>0）" "1" \
    "$(php_eval '$d = panel_ssl_days_left(); echo (int) ($d !== null && $d > 0);')"
check "面板能读出当前生效证书的签发者" "1" \
    "$(php_eval '$c = panel_ssl_active_cert(); echo (int) (($c["issuer"] ?? "") !== "");')"

# ---- 5.6 ACME（桩 certbot）----
CB="$ROOT/certbot-stub"
cat > "$CB" <<CBEOF
#!/bin/sh
# 桩 certbot：把"签发"出来的证书放到 --config-dir/live/<domain>/
CFG=""
DOMAIN=""
MODE="\$1"
while [ \$# -gt 0 ]; do
    case "\$1" in
        --config-dir) CFG="\$2"; shift 2 ;;
        -d) DOMAIN="\$2"; shift 2 ;;
        *) shift ;;
    esac
done
[ -n "\$CFG" ] || exit 9
if [ "\${MCLINK_STUB_CERTBOT_FAIL:-0}" = "1" ]; then
    echo "stub certbot: simulated failure" >&2
    exit 1
fi
mkdir -p "\$CFG/live/\$DOMAIN" "\$CFG/work" "\$CFG/logs"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "\$CFG/live/\$DOMAIN/privkey.pem" \
    -out "\$CFG/live/\$DOMAIN/fullchain.pem" -days 90 -subj "/CN=\$DOMAIN" \
    -addext "subjectAltName=DNS:\$DOMAIN" >/dev/null 2>&1
echo "stub certbot: issued \$DOMAIN (\$MODE)"
exit 0
CBEOF
chmod +x "$CB"

export MCLINK_CERTBOT_BIN="$CB"

REQ_ID="smoke-acme-dry"
cat > "$AGENT_DIR/requests/$REQ_ID.json" <<JSON
{"schema":1,"id":"$REQ_ID","action":"ssl-acme-issue","created_at":"2026-10-05T00:00:00Z","actor":"smoke","args":{"domain":"panel.example.com","email":"admin@example.com","dry_run":"1"}}
JSON

agent once > "$ROOT/agent-acme-dry.log" 2>&1
check_contains "ACME dry-run：结果 ok=true" "$AGENT_DIR/results/$REQ_ID.json" '"ok": true'
check_contains "ACME dry-run：结果里标了 dry_run" "$AGENT_DIR/results/$REQ_ID.json" '"dry_run": true'

REQ_ID="smoke-acme-issue"
cat > "$AGENT_DIR/requests/$REQ_ID.json" <<JSON
{"schema":1,"id":"$REQ_ID","action":"ssl-acme-issue","created_at":"2026-10-05T00:00:00Z","actor":"smoke","args":{"domain":"panel.example.com","email":"admin@example.com","dry_run":"0"}}
JSON

agent once > "$ROOT/agent-acme.log" 2>&1
check_contains "ACME 正式签发：结果 ok=true" "$AGENT_DIR/results/$REQ_ID.json" '"ok": true'
check "ssl_mode 变成 acme" "acme" "$(php_eval 'echo panel_ssl_mode();')"
check_contains "ACME 证书被装上并生效" "$NGINX_DIR/panel-server.conf" "acme-panel.example.com-"
check_contains "签发结果记进了 acme.json" "$CERT_DIR/acme.json" 'panel.example.com'

# ---- 5.7 续期失败：旧证书必须原样在 ----
ACTIVE_BEFORE="$(php_eval 'echo panel_ssl_cert_file();')"
SUM_BEFORE="$(sha256sum "$ACTIVE_BEFORE" | awk '{print $1}')"

REQ_ID="smoke-acme-renew-fail"
cat > "$AGENT_DIR/requests/$REQ_ID.json" <<JSON
{"schema":1,"id":"$REQ_ID","action":"ssl-acme-renew","created_at":"2026-10-05T00:00:00Z","actor":"smoke","args":{"domain":"panel.example.com","email":"admin@example.com","dry_run":"0"}}
JSON

MCLINK_STUB_CERTBOT_FAIL=1 agent once > "$ROOT/agent-acme-fail.log" 2>&1 || true

check_contains "续期失败：结果是 ok=false" "$AGENT_DIR/results/$REQ_ID.json" '"ok": false'
SUM_AFTER="$(sha256sum "$ACTIVE_BEFORE" | awk '{print $1}')"
check "续期失败：现有证书文件一个字节都没动" "$SUM_BEFORE" "$SUM_AFTER"
check "续期失败：ssl_mode 仍然是 acme（没有掉回 HTTP）" "acme" "$(php_eval 'echo panel_ssl_mode();')"
check_contains "续期失败：失败原因写进了 acme.json" "$CERT_DIR/acme.json" '"last_ok": false'

unset MCLINK_CERTBOT_BIN

# ============================================================
echo
echo "== 6. 急救：reset-entry / unbind-domain / ssl-off / 清除覆盖 =="
# ============================================================

CLI () { "$PHP_BIN" "$PANEL/bin/mclink-panel.php" "$@" 2>&1; }

# 面板的写操作只是**排队**（真正干活的是 root 代理）：CLI 跑完再让代理处理一次，
# 就是生产里"2 秒内自动生效"的等价物。
CLI_AND_APPLY () { CLI "$@"; agent once > /dev/null 2>&1; }

# 先把"三个都配错"的状态摆好：安全入口 + 域名 + HTTPS
write_settings '{"url_suffix":"mclink-panel","bound_domain":"panel.example.com","unauthenticated_code":"404"}'
render > /dev/null

OUT="$(CLI_AND_APPLY reset-entry)"
check_has "reset-entry：明确说安全入口已清空" "$OUT" "安全入口已改为"
check "reset-entry：设置里 url_suffix 真的空了" "" "$(php_eval 'echo panel_listen_config()["entry_suffix"];')"
check_missing "reset-entry：渲染结果里不再有入口守卫" "$NGINX_DIR/panel-server.conf" "安全入口守卫"

OUT="$(CLI_AND_APPLY unbind-domain)"
check_has "unbind-domain：明确说域名绑定已清空" "$OUT" "域名绑定已清空"
check "unbind-domain：设置里 bound_domain 真的空了" "" "$(php_eval 'echo panel_bound_domain();')"
check_missing "unbind-domain：渲染结果里不再有 Host 守卫" "$NGINX_DIR/panel-server.conf" "域名绑定：只有"

OUT="$(CLI_AND_APPLY ssl-off)"
check_has "ssl-off：明确说回到纯 HTTP" "$OUT" "不用 HTTPS"
check "ssl-off：设置里 ssl_mode=off" "off" "$(php_eval 'echo panel_ssl_mode();')"
check "ssl-off：证书文件没被删（只是不引用）" "1" "$(ls "$CERT_DIR"/*.crt 2>/dev/null | wc -l | tr -d ' ' | awk '{print ($1 > 0) ? 1 : 0}')"

check "急救覆盖还在（数据库不可用时的兜底）" "1" "$(grep -qE 'ssl_mode|url_suffix|bound_domain' "$CONFIG_DIR/panel-emergency-overrides.json" 2>/dev/null && echo 1 || echo 0)"

OUT="$(CLI overrides --clear)"
check_has "清除急救覆盖：明确说清了哪几条" "$OUT" "已清除急救覆盖"
check "清除之后覆盖文件不在了（值回到数据库 / 文件）" "0" "$(ls "$CONFIG_DIR"/panel-emergency-overrides.json 2>/dev/null | wc -l | tr -d ' ')"

OUT="$(CLI rescue)"
check_has "rescue：打印出急救入口地址" "$OUT" "127.0.0.1:$RESCUE_PORT"

OUT="$(CLI status --json)"
check_has "status --json：能输出机器可读的状态" "$OUT" '"rescue_port"'

OUT="$(CLI help)"
check_has "help：列出 reset-entry" "$OUT" "reset-entry"
check_has "help：列出 ssl-off" "$OUT" "ssl-off"

# ---- 队列 / 心跳：面板页面上"根代理在不在跑"靠的就是这两个文件 ----
OUT="$(php_eval 'echo panel_agent_heartbeat()["running"] ? "1" : "0";')"
check "心跳文件能让面板判断出根代理在跑（这里没跑 → 0，但读得到）" "0" "$OUT"

OUT="$(php_eval 'echo panel_agent_writable() ? "1" : "0";')"
check "队列目录可写（面板能排队）" "1" "$OUT"

# ============================================================
echo
echo "== 7. mclink 管理台 → 面板 CLI（重置安全入口 / 解绑域名 / 关 HTTPS 真的生效）=="
# ============================================================

# 这一节把"SSH 管理台"整条链路也跑一遍：用两个桩把 docker / id 换掉，于是
# scripts/mclink-console.sh 里的 `docker exec mc-panel php
# /var/www/bin/mclink-panel.php ...` 会真的落到这台机器上的 PHP CLI 上。
CONSOLE_BIN="$ROOT/console-bin"
mkdir -p "$CONSOLE_BIN"

cat > "$CONSOLE_BIN/id" <<'IDEOF'
#!/bin/sh
# 桩：让 mclink 管理台以为自己是以 root 跑的（它第一件事就是查这个）
if [ "$1" = "-u" ]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
IDEOF
chmod +x "$CONSOLE_BIN/id"

cat > "$CONSOLE_BIN/docker" <<'DOCKEREOF'
#!/bin/sh
# 桩 docker：只实现 mclink 管理台用到的那两种调用。
#   docker ps --format ...            → 假装面板容器在跑
#   docker exec mc-panel php <cli> …  → 直接跑本机的 PHP CLI
case "$1" in
    ps)
        echo "mc-panel"
        exit 0
        ;;
    exec)
        shift 4   # exec / 容器名 / php / 容器内的 CLI 路径
        exec "$MCLINK_SMOKE_PHP" "$MCLINK_SMOKE_PANEL_CLI" "$@"
        ;;
    *)
        exit 1
        ;;
esac
DOCKEREOF
chmod +x "$CONSOLE_BIN/docker"

export MCLINK_SMOKE_PHP="$PHP_BIN"
export MCLINK_SMOKE_PANEL_CLI="$PANEL/bin/mclink-panel.php"

# 管理台脚本与它的辅助脚本整份复制到临时根目录（apply-network 要按安装根找），
# 再放一份真的 panel/docker-compose.yml —— panel-apply.sh 要从它读容器名与端口。
mkdir -p "$ROOT/scripts" "$ROOT/panel"
cp "$REPO/scripts/"*.py "$ROOT/scripts/" 2>/dev/null || true
cp "$REPO/scripts/mclink-console.sh" "$REPO/scripts/panel-apply.sh" "$ROOT/scripts/" 2>/dev/null || true
cp "$REPO/panel/docker-compose.yml" "$ROOT/panel/" 2>/dev/null || true
printf 'MCLINK_DB_HOST=127.0.0.1\nMCLINK_PANEL_PORT=19102\n' > "$ROOT/.env"

mclink () { # mclink <子命令...>（走真的管理台脚本 + 桩 docker / id）
    PATH="$CONSOLE_BIN:$PATH" MCLINK_ROOT="$ROOT" MCLINK_ENV_FILE="$ROOT/.env" \
        bash "$ROOT/scripts/mclink-console.sh" "$@"
}

write_settings '{"url_suffix":"mclink-panel","bound_domain":"panel.example.com","unauthenticated_code":"404"}'
render > /dev/null

OUT="$(mclink reset-entry)"
check_has "管理台 reset-entry：走的是面板 CLI（不是老的回退路径）" "$OUT" "已通过面板 CLI"
check "管理台 reset-entry：设置里 url_suffix 被清空" "" "$(php_eval 'echo panel_listen_config()["entry_suffix"];')"
check "管理台 reset-entry：审计日志留痕" "1" "$(grep -q 'reset-entry' "$ROOT/logs/mclink-console.log" 2>/dev/null && echo 1 || echo 0)"

OUT="$(mclink set-entry --path my-secret)"
check "管理台 set-entry：安全入口被设成指定值" "my-secret" "$(php_eval 'echo panel_listen_config()["entry_suffix"];')"

OUT="$(mclink unbind-domain)"
check_has "管理台 unbind-domain：走的是面板 CLI" "$OUT" "已通过面板 CLI"
check "管理台 unbind-domain：设置里 bound_domain 被清空" "" "$(php_eval 'echo panel_bound_domain();')"

write_settings "{\"ssl_mode\":\"upload\",\"ssl_cert_file\":\"$CERT_CRT\",\"ssl_key_file\":\"$CERT_KEY\"}"
OUT="$(mclink ssl-off)"
check_has "管理台 ssl-off：走的是面板 CLI" "$OUT" "已通过面板 CLI"
check "管理台 ssl-off：设置里 ssl_mode=off" "off" "$(php_eval 'echo panel_ssl_mode();')"
check "管理台 ssl-off：证书文件还在（只是不引用）" "1" "$(ls "$CERT_DIR"/*.crt >/dev/null 2>&1 && echo 1 || echo 0)"

OUT="$(mclink ssl-status)"
check_has "管理台 ssl-status：打出面板设置（含 ssl_mode）" "$OUT" "ssl_mode"
check_has "管理台 ssl-status：打出急救入口" "$OUT" "急救入口"
check_has "管理台 ssl-status：ssl_mode 真的是 off" "$OUT" "= off"

OUT="$(mclink apply-network --dry-run)"
check_has "管理台 apply-network --dry-run：打印将要写的 .env 键" "$OUT" "MCLINK_PANEL_LISTEN_IP"
check "管理台 apply-network --dry-run：不真的改 .env" "19102" "$(grep -E '^MCLINK_PANEL_PORT=' "$ROOT/.env" | cut -d= -f2-)"

OUT="$(mclink help)"
check_has "管理台 help：列出 rescue" "$OUT" "rescue"

# ============================================================
echo
echo "================================================================================"
echo "M4 冒烟测试：$PASS 项通过，$FAIL 项失败，$SKIP 项跳过"
echo "================================================================================"

if [ "$FAIL" -ne 0 ]; then
    echo
    echo "（排查：KEEP_TMP=1 bash docs/panel-ssl-nginx-smoke.sh，临时目录会保留）"
    exit 1
fi

exit 0
