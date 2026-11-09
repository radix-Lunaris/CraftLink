#!/usr/bin/env bash
# ============================================================
#  CraftLink 面板设置 / 权限模型 / 未认证返回码 / logo —— **端到端**冒烟测试
# ============================================================
#
#  为什么需要它：docs/panel-settings-selftest.php 覆盖的是纯逻辑（校验、权限
#  判定、存储回退）。渲染与"服务端真的拦下了越权 POST"只有真发一个 HTTP
#  请求才算数 —— 这个脚本用 php 自带的 web server 起一个面板，用 curl 打它。
#
#  不需要 MySQL：数据库故意指向连不上的 127.0.0.1:3306，于是面板走"数据库
#  不可用 → 只读回退到配置文件备份"这条路（D1 的兜底路径），正好把它也验了。
#  中继也不需要：MCLINK_RELAY_API_URL 指向 127.0.0.1:1。
#
#  用法（要求 php-cli 8.2+ 与 curl）：
#
#      PHP_BIN=php bash docs/panel-settings-smoke.sh
#
#  退出码 0 = 全过；非 0 = 有失败项（会打印每一处 FAIL 的期望值和实际值）。
#
#  覆盖的验收点：
#    1. 登录页渲染：面板设置里的名字 / 网页标签名 / favicon（上传的 logo）
#    2. /index.php?action=logo 不需要登录也能取到图片
#    3. 未认证返回码：404 / 403 / 444 / 200 空页 / 登录页 五种配置各验一次
#    4. 设置页渲染：root 看到「放行」开关，普通管理员看不到、控件是禁用的
#    5. **服务端强制**：普通管理员 POST 一条没放行的设置 → 被拦下（不是改了）
#    6. 只有 root 能拨「放行」开关、能初始化存储
#    7. 校验规则在服务端生效（越界值在写库之前就被拒）
#
#  [!] 所有文件都在 mktemp 出来的临时目录里，不碰仓库、不碰真实配置。

set -u

PHP_BIN="${PHP_BIN:-php}"
PORT="${PORT:-18099}"
PANEL_DIR="$(cd "$(dirname "$0")/../panel" && pwd)"
ROOT="$(mktemp -d)"
SETTINGS_FILE="$ROOT/config/panel-settings.json"
UPLOAD_DIR="$ROOT/uploads"
RELAY_VERSION_FILE="$ROOT/relay-VERSION"

# [!] 证书目录也要指到临时目录：面板设置页上的 M4 动作（渲染 / 自签 / ACME…）
#     会往证书目录里写"队列请求文件"，不指的话会落到仓库的 data/panel-certs/。
CERT_DIR="$ROOT/certs"
AGENT_DIR="$CERT_DIR/agent"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  [PASS] $1"; }

fail() {
    FAIL=$((FAIL + 1))
    echo "  [FAIL] $1"
    [ -n "${2:-}" ] && echo "         -- $2"
}

check() { # check <名称> <期望> <实际>
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "期望 [$2] 实际 [$3]"; fi
}

check_contains() { # check_contains <名称> <文件> <子串>
    if grep -qF -- "$3" "$2" 2>/dev/null; then pass "$1"; else fail "$1" "在 $(basename "$2") 里找不到 [$3]"; fi
}

check_missing() {
    if grep -qF -- "$3" "$2" 2>/dev/null; then fail "$1" "不该出现却出现了 [$3]"; else pass "$1"; fi
}

write_settings() { # write_settings <json 里的 settings 对象>
    cat > "$SETTINGS_FILE" <<JSON
{
    "schema": 1,
    "panel_version": "2.11.45",
    "updated_at": "$(date -Is)",
    "settings": $1
}
JSON
}

get_code() { # get_code <url> <输出文件> [cookie]
    if [ -n "${3:-}" ]; then
        curl -s -o "$2" -w '%{http_code}' -b "$3" "$1"
    else
        curl -s -o "$2" -w '%{http_code}' "$1"
    fi
}

mkdir -p "$ROOT/config" "$UPLOAD_DIR" "$AGENT_DIR"

# 一张最小的合法 PNG（只要魔术字节对：面板按魔术字节判类型）。
printf '\x89PNG\r\n\x1a\n' > "$UPLOAD_DIR/logo-selftest.png"
head -c 128 /dev/zero >> "$UPLOAD_DIR/logo-selftest.png"

# 中继版本文件（面板读不到 API 时用它）。
printf '3.9.20\n' > "$RELAY_VERSION_FILE"

# 初始设置：名字 / 标签名 / logo / 未认证返回码 = 404。
write_settings '{"panel_brand":"自检面板","panel_tab_title":"自检标签","panel_logo":"logo-selftest.png","unauthenticated_code":"404"}'

echo "CraftLink 面板设置冒烟测试"
echo "  PHP     : $("$PHP_BIN" -r 'echo PHP_VERSION;')"
echo "  面板    : $PANEL_DIR"
echo "  临时目录: $ROOT"
echo "  地址    : http://127.0.0.1:$PORT"

MCLINK_PANEL_SETTINGS_FILE="$SETTINGS_FILE" \
MCLINK_PANEL_UPLOAD_DIR="$UPLOAD_DIR" \
MCLINK_PANEL_CERT_DIR="$CERT_DIR" \
MCLINK_PANEL_AGENT_DIR="$AGENT_DIR" \
MCLINK_ROOT="$ROOT" \
MCLINK_RELAY_VERSION_FILE="$RELAY_VERSION_FILE" \
MCLINK_DB_HOST=127.0.0.1 \
MCLINK_RELAY_API_URL=http://127.0.0.1:1 \
MCLINK_PANEL_TITLE= \
"$PHP_BIN" -S "127.0.0.1:$PORT" -t "$PANEL_DIR/web" > "$ROOT/server.log" 2>&1 &
SERVER_PID=$!

cleanup() {
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null

    if [ "${KEEP_TMP:-}" = "1" ]; then
        echo
        echo "（KEEP_TMP=1：临时目录保留在 $ROOT，server.log 在里面）"
        return
    fi

    rm -rf "$ROOT"
}
trap cleanup EXIT

sleep 1.5

BASE="http://127.0.0.1:$PORT"
BODY="$ROOT/body.html"

# ============================================================
echo
echo "== 1. 登录页渲染（名字 / 标签名 / favicon） =="
# ============================================================

check "登录页返回 200" "200" "$(get_code "$BASE/login.php" "$BODY")"
check_contains "网页标签名取自面板设置（自检标签）" "$BODY" "<title>登录 - 自检标签</title>"
check_contains "左上角名称取自面板设置（自检面板）" "$BODY" "自检面板"
check_contains "上传的 logo 同时作为 favicon（同一张图）" "$BODY" 'rel="icon" href="index.php?action=logo&amp;v='

# ============================================================
echo
echo "== 2. /index.php?action=logo（不需要登录） =="
# ============================================================

check "logo 返回 200" "200" "$(get_code "$BASE/index.php?action=logo" "$BODY")"
if [ "$(head -c 8 "$BODY" | od -An -tx1 | tr -d ' \n')" = "89504e470d0a1a0a" ]; then
    pass "logo 正文是 PNG（按魔术字节原样输出）"
else
    fail "logo 正文是 PNG（按魔术字节原样输出）" "开头是 $(head -c 8 "$BODY" | od -An -tx1 | tr -d ' \n')"
fi

# ============================================================
echo
echo "== 3. 未认证返回码（隐藏面板特征） =="
# ============================================================

write_settings '{"unauthenticated_code":"404"}'
CODE="$(get_code "$BASE/index.php?action=settings" "$BODY")"
check "配 404：未登录访问设置页返回 404" "404" "$CODE"
check "配 404：正文是空的（看不出这是一个面板）" "0" "$(wc -c < "$BODY" | tr -d ' ')"

write_settings '{"unauthenticated_code":"403"}'
check "配 403：返回 403" "403" "$(get_code "$BASE/index.php?action=dashboard" "$BODY")"

write_settings '{"unauthenticated_code":"200"}'
CODE="$(get_code "$BASE/index.php?action=dashboard" "$BODY")"
check "配 200：返回 200 空页" "200" "$CODE"
check "配 200：正文是空的" "0" "$(wc -c < "$BODY" | tr -d ' ')"

write_settings '{"unauthenticated_code":"444"}'
check "配 444：返回 444（nginx 的直接断开码）" "444" "$(get_code "$BASE/index.php?action=dashboard" "$BODY")"

write_settings '{"unauthenticated_code":"login"}'
CODE="$(curl -s -o "$BODY" -D "$ROOT/headers.txt" -w '%{http_code}' "$BASE/index.php?action=dashboard")"
check "配 login（默认）：未登录访问受保护页 → 302 到登录页" "302" "$CODE"
check_contains "跳转目标是登录页" "$ROOT/headers.txt" "index.php?action=login"

# ============================================================
echo
echo "== 4. 播种会话（root / 普通管理员） =="
# ============================================================

seed_session() { # seed_session <sid> <role>
    "$PHP_BIN" -r '
        session_name("mclink_panel");
        session_id($argv[1]);
        session_start();
        $_SESSION["user"] = [
            "username" => $argv[1],
            "role" => $argv[2],
            "is_admin" => $argv[2] === "player" ? 0 : 1,
        ];
        $_SESSION["last_seen_at"] = time();
        $_SESSION["csrf"] = "selftest-csrf-" . $argv[1];
        session_write_close();
    ' "$1" "$2"
}

seed_session root-selftest root
seed_session admin-selftest admin

ROOT_COOKIE="mclink_panel=root-selftest"
ADMIN_COOKIE="mclink_panel=admin-selftest"

# 放回"有人上传过 logo + 有名字"的设置（第 3 节把它们改掉了）。
write_settings '{"panel_brand":"自检面板","panel_tab_title":"自检标签","panel_logo":"logo-selftest.png","unauthenticated_code":"login"}'

# ============================================================
echo
echo "== 5. 设置页渲染：root 有放行开关，普通管理员只有只读视图 =="
# ============================================================

check "root 打开设置页返回 200（数据库不可用也照样渲染）" "200" \
    "$(get_code "$BASE/index.php?action=settings" "$BODY" "$ROOT_COOKIE")"
check_contains "root 看得到存储状态那块（数据库不可用 / 配置文件回退）" "$BODY" "数据库：不可用"
check_contains "root 看得到「放行给普通管理员」开关" "$BODY" "放行给普通管理员"
check_contains "页面显示当前生效的监听配置（自带 nginx 的接口数据）" "$BODY" "监听：127.0.0.1:19102"
check_contains "面板版本号显示的是 2.11.45" "$BODY" "面板版本：2.11.45"
check_contains "域名与 SSL 一节真的实现了（有绑定域名 / SSL 模式）" "$BODY" "绑定域名"
check_contains "SSL 一节列出四种模式（上传证书 / 自签 / Let’s Encrypt）" "$BODY" "上传证书（离线可用，推荐）"
check_contains "证书上传入口在页面上（离线可用）" "$BODY" "上传证书并启用"
check_contains "急救区块在页面上（重置安全入口 / 解绑域名 / 关掉 HTTPS）" "$BODY" "急救（配置改坏了从这里回去）"
check_contains "页面上显示本机急救入口地址" "$BODY" "127.0.0.1:19103"
check_missing "不再有「未实现」的占位" "$BODY" "未实现"

# ---- 左上角的中继版本（需求 3）：接口连不上 → 回退到 VERSION 文件 ----
printf '3.9.20\n' > "$RELAY_VERSION_FILE"
get_code "$BASE/index.php?action=settings" "$BODY" "$ROOT_COOKIE" > /dev/null
check_contains "中继版本：接口连不上时回退读 VERSION 文件（左上角显示 3.9.20）" "$BODY" ">中继 3.9.20<"

printf 'relay started at 12:00\n' > "$RELAY_VERSION_FILE"
get_code "$BASE/index.php?action=settings" "$BODY" "$ROOT_COOKIE" > /dev/null
check_missing "中继版本：坏文件里的日志行绝不会被当成版本号显示" "$BODY" ">中继 relay started at 12:00<"
check_contains "中继版本：跳过坏文件、顺延到下一个候选" "$BODY" ">中继 "

printf '3.9.20\n' > "$RELAY_VERSION_FILE"

check "普通管理员打开设置页也是 200（可见）" "200" \
    "$(get_code "$BASE/index.php?action=settings" "$BODY" "$ADMIN_COOKIE")"
check_missing "普通管理员看不到 root 的放行开关" "$BODY" "放行给普通管理员"
check_contains "普通管理员看到的是只读说明" "$BODY" "只读：这一项现在只允许根管理员修改。"

# ============================================================
echo
echo "== 6. 服务端强制：普通管理员 POST 没放行的设置 → 被拦下 =="
# ============================================================

post_settings() { # post_settings <cookie> <额外参数...>
    local cookie="$1"; shift
    local args=()
    for pair in "$@"; do args+=(--data "$pair"); done
    curl -s -o "$BODY" -b "$cookie" -L -w '%{http_code}' \
        "${args[@]}" "$BASE/index.php?action=panel_settings_save"
}

CODE="$(post_settings "$ADMIN_COOKIE" "csrf=selftest-csrf-admin-selftest" "name=listen_port" "value=9999")"
check "普通管理员改没放行的项：请求被处理成跳转 + 提示（不是 500）" "200" "$CODE"
check_contains "被拦下的原话（要求 root 放行）" "$BODY" "目前只允许根管理员修改"
if grep -q '"listen_port"' "$SETTINGS_FILE"; then
    fail "被拦下时设置一个字都没改" "配置文件里出现了 listen_port"
else
    pass "被拦下时设置一个字都没改"
fi

# 普通管理员不能拨放行开关（只有 root 能）。
curl -s -o "$BODY" -b "$ADMIN_COOKIE" -L \
    --data "csrf=selftest-csrf-admin-selftest" \
    --data "name=listen_port" \
    --data "allow=1" \
    "$BASE/index.php?action=panel_setting_admin_edit" > /dev/null
check_contains "普通管理员拨放行开关 → 只有根管理员能执行这个操作" "$BODY" "只有根管理员能执行这个操作"
check_missing "普通管理员的越权没有写进设置" "$BODY" "已放行「面板监听端口」"

# ============================================================
echo
echo "== 7. root：放行 → 普通管理员就能改了（服务端判定跟着开关走） =="
# ============================================================

curl -s -o "$BODY" -b "$ROOT_COOKIE" -L \
    --data "csrf=selftest-csrf-root-selftest" \
    --data "name=listen_port" \
    --data "allow=1" \
    "$BASE/index.php?action=panel_setting_admin_edit" > /dev/null
check_contains "root 拨开关：数据库不可用 → 明确报保存失败（不假装成功）" "$BODY" "保存失败"

# 放行是"数据库里的一行"，数据库不可用时写不进去；这里直接把放行开关写进
# 配置文件备份，验证"开关打开之后普通管理员能改那一条"。
write_settings '{"listen_port__admin_edit":"1","unauthenticated_code":"login"}'

curl -s -o "$BODY" -b "$ADMIN_COOKIE" -L \
    --data "csrf=selftest-csrf-admin-selftest" \
    --data "name=listen_port" \
    --data "value=9999" \
    "$BASE/index.php?action=panel_settings_save" > /dev/null
check_missing "放行之后：不再报「只允许根管理员修改」" "$BODY" "目前只允许根管理员修改"
check_contains "放行之后：改到数据库那一步才失败（说明权限已经过了）" "$BODY" "数据库"

# ============================================================
echo
echo "== 8. 校验在服务端生效（越界值写库之前就被拒） =="
# ============================================================

write_settings '{"unauthenticated_code":"login"}'

curl -s -o "$BODY" -b "$ROOT_COOKIE" -L \
    --data "csrf=selftest-csrf-root-selftest" \
    --data "name=listen_port" \
    --data "value=70000" \
    "$BASE/index.php?action=panel_settings_save" > /dev/null
check_contains "监听端口 70000 → 服务端校验拒绝" "$BODY" "监听端口必须在 1 - 65535 之间"

curl -s -o "$BODY" -b "$ROOT_COOKIE" -L \
    --data "csrf=selftest-csrf-root-selftest" \
    --data "name=unauthenticated_code" \
    --data "value=500" \
    "$BASE/index.php?action=panel_settings_save" > /dev/null
check_contains "未认证返回码 500 → 服务端校验拒绝" "$BODY" "未认证返回码只能是列出的几种之一"

# ============================================================
echo
echo "== 9. CSRF 仍然守着所有写操作 =="
# ============================================================

CODE="$(curl -s -o "$BODY" -w '%{http_code}' -b "$ROOT_COOKIE" \
    --data "name=listen_port" --data "value=19102" \
    "$BASE/index.php?action=panel_settings_save")"
check "没有 CSRF token 的 POST 被拒（400）" "400" "$CODE"
check_contains "CSRF 拒绝的理由" "$BODY" "CSRF"

# ============================================================
echo
echo "== 10. 仪表盘不再渲染「正在连接」（数据仍在 live.php 里） =="
# ============================================================

# 说明：仪表盘本身要查 users / room_sessions，没有数据库就打不开，所以这一条
# 在源码层面验：卡片（data-live=starting_members）必须从视图里消失，而
# web/live.php 仍然在回包里带这个数字（需求原话：数据留着，只是不渲染）。
if grep -q 'data-live="starting_members"' "$PANEL_DIR/web/views/dashboard.php"; then
    fail "仪表盘视图里不再有「正在连接」卡片" "dashboard.php 里还有 data-live=starting_members"
else
    pass "仪表盘视图里不再有「正在连接」卡片"
fi

if grep -q "'starting_members' => \$liveStarting" "$PANEL_DIR/web/live.php"; then
    pass "实时接口仍然提供 starting_members（数据没删，只是不渲染）"
else
    fail "实时接口仍然提供 starting_members（数据没删，只是不渲染）" "live.php 里找不到了"
fi

if grep -q 'data-live="connected_members"' "$PANEL_DIR/web/views/dashboard.php"; then
    pass "「已连接（按成员状态）」那张卡片保留着"
else
    fail "「已连接（按成员状态）」那张卡片保留着" "dashboard.php 里找不到了"
fi

# ============================================================
echo
echo "== 11. logo 在没有上传时的回退 =="
# ============================================================

write_settings '{"panel_brand":"自检面板","unauthenticated_code":"login"}'
get_code "$BASE/login.php" "$BODY" > /dev/null
check_missing "没有上传 logo 时不发 favicon 标签（用浏览器默认图标）" "$BODY" 'rel="icon"'
check_missing "没有上传 logo 时不渲染上传的图片（退回内置标记）" "$BODY" 'class="logo-img"'
check "没有 logo 时 action=logo 返回 404（空正文）" "404" "$(get_code "$BASE/index.php?action=logo" "$ROOT/logo-empty.bin")"
check "没有 logo 时正文是空的" "0" "$(wc -c < "$ROOT/logo-empty.bin" | tr -d ' ')"

# ============================================================
echo
echo "== 12. M4：SSL / 域名 / 生效 / 急救的 POST 端点 =="
# ============================================================

# 说明：这个冒烟环境里**数据库是故意连不上的**（走 D1 的只读回退路径），
# 所以"要改设置"的动作在这里应该**明确报错**（绝不假装成功）；
# "只排队"的动作（渲染 / 自签 / ACME / 刷状态）不写数据库，应该排队成功。
# 数据库可写时的行为由 docs/panel-ssl-nginx-smoke.sh 覆盖（那边用 CLI + 代理）。

write_settings '{"url_suffix":"mclink-panel","bound_domain":"panel.example.com","acme_email":"admin@example.com","unauthenticated_code":"login"}'

# 这一节把「绑定域名」也设上了：所以请求必须带对 Host —— 这正好同时验了
# PHP 那一层的域名兜底（nginx 之外的第二道门）。
post_m4 () { # post_m4 <cookie> <action> [--data ...]
    local cookie="$1"; shift
    local action="$1"; shift
    local args=()
    for pair in "$@"; do args+=(--data "$pair"); done
    curl -s -o "$BODY" -b "$cookie" -L -w '%{http_code}' \
        -H 'Host: panel.example.com' \
        "${args[@]}" "$BASE/index.php?action=$action"
}

# 域名兜底：Host 不对 → 按未认证返回码处理（这里是 login → 404）
check "PHP 层域名兜底：Host 不对 → 404（不是登录页）" "404" \
    "$(curl -s -o "$BODY" -w '%{http_code}' -b "$ROOT_COOKIE" -H 'Host: evil.test' \
        "$BASE/index.php?action=settings")"
check "PHP 层域名兜底：Host 对了 → 200" "200" \
    "$(curl -s -o "$BODY" -w '%{http_code}' -b "$ROOT_COOKIE" -H 'Host: panel.example.com' \
        "$BASE/index.php?action=settings")"

check "root 排队「重新渲染并生效」→ 200（不是 500）" "200" \
    "$(post_m4 "$ROOT_COOKIE" panel_nginx_apply "csrf=selftest-csrf-root-selftest")"
check_contains "渲染排队成功（面板明确说"已排队"）" "$BODY" "已排队"

check "root 排队「刷新证书状态」→ 200" "200" \
    "$(post_m4 "$ROOT_COOKIE" panel_ssl_status_refresh "csrf=selftest-csrf-root-selftest")"
check_contains "状态刷新排队成功" "$BODY" "已排队"

check "root 排队「生成自签证书」→ 200" "200" \
    "$(post_m4 "$ROOT_COOKIE" panel_ssl_selfsigned "csrf=selftest-csrf-root-selftest")"
check_contains "自签排队成功（说明会提示浏览器不受信任）" "$BODY" "自签证书"

check "root 排队「ACME dry-run」→ 200" "200" \
    "$(post_m4 "$ROOT_COOKIE" panel_ssl_acme "csrf=selftest-csrf-root-selftest" "mode=issue" "dry_run=1")"
check_contains "ACME dry-run 排队成功（明确说不会真的签发）" "$BODY" "不会真的签发"

check "root 上传空表单 → 200 + 明确报错（不是 500）" "200" \
    "$(post_m4 "$ROOT_COOKIE" panel_ssl_upload "csrf=selftest-csrf-root-selftest")"
check_contains "上传空表单的报错是人话" "$BODY" "证书上传失败"

# ---- 急救动作：数据库不可用时必须明确失败，不能假装成功 ----
check "root 急救「清空安全入口」→ 200（不是 500）" "200" \
    "$(post_m4 "$ROOT_COOKIE" panel_emergency_reset_entry "csrf=selftest-csrf-root-selftest")"
check_contains "数据库不可用时明确报「重置失败」" "$BODY" "重置失败"

check "root 急救「解绑域名」→ 200" "200" \
    "$(post_m4 "$ROOT_COOKIE" panel_emergency_unbind_domain "csrf=selftest-csrf-root-selftest")"
check_contains "数据库不可用时明确报「解绑失败」" "$BODY" "解绑失败"

check "root 急救「关掉 HTTPS」→ 200" "200" \
    "$(post_m4 "$ROOT_COOKIE" panel_emergency_ssl_off "csrf=selftest-csrf-root-selftest")"
check_contains "数据库不可用时明确报「操作失败」" "$BODY" "操作失败"

# ---- 权限：普通管理员（没放行）不能碰 SSL 相关的任何写操作 ----
check "普通管理员上传证书 → 被拦下（要求 root 放行）" "200" \
    "$(post_m4 "$ADMIN_COOKIE" panel_ssl_upload "csrf=selftest-csrf-admin-selftest")"
check_contains "被拦下的理由" "$BODY" "目前只允许根管理员修改"

check "普通管理员点「关掉 HTTPS」→ 被拦下" "200" \
    "$(post_m4 "$ADMIN_COOKIE" panel_emergency_ssl_off "csrf=selftest-csrf-admin-selftest")"
check_contains "被拦下的理由（急救动作也走同一套权限）" "$BODY" "目前只允许根管理员修改"

# ---- CSRF 仍然守着这些新端点 ----
CODE="$(curl -s -o "$BODY" -w '%{http_code}' -b "$ROOT_COOKIE" -H 'Host: panel.example.com' \
    --data "mode=issue" "$BASE/index.php?action=panel_ssl_acme")"
check "M4 端点没有 CSRF token 的 POST 被拒（400）" "400" "$CODE"

check "root 清除急救覆盖（没有覆盖时是空操作）→ 200" "200" \
    "$(post_m4 "$ROOT_COOKIE" panel_emergency_clear "csrf=selftest-csrf-root-selftest")"

# ============================================================
echo
echo "================================================================================"
echo "面板设置冒烟测试：$PASS 项通过，$FAIL 项失败"
echo "================================================================================"

if [ "$FAIL" -ne 0 ]; then
    echo
    echo "（php 自带 web server 的日志留在 $ROOT/server.log —— 但临时目录在退出时会删掉；"
    echo "  要留着排查就跑：KEEP_TMP=1 bash docs/panel-settings-smoke.sh）"
    exit 1
fi

exit 0
