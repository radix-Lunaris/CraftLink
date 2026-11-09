#!/bin/bash
# ============================================================
#  MC-Link 面板 一键修复 + 自证
# ============================================================
#  用法（在服务器上执行）：
#      bash /opt/mc-link/fix-panel.sh
#
#  它会做四件事，每一步都自己判断对错，不用你去看：
#    1. 检查修复后的文件是否真的在服务器上
#    2. 修权限 + 强制重建镜像（先删旧镜像，杜绝构建缓存）
#    3. 检查文件是否真的进了新容器
#    4. 用真实请求验证页面不再报「无法从中继读取开关状态」
#
#  如果第 1 步就报「缺失」，说明文件没传上去 —— 那才是真正的卡点，
#  脚本会明确告诉你缺哪几个。
# ============================================================

set -u

PANEL_DIR=/opt/mc-link/panel
IMAGE=mc-link-panel:2.0.0

echo "=============================================="
echo " 1/4  检查文件是否在服务器上"
echo "=============================================="

MISSING=0

# 用 grep -F（纯字符串匹配），避免正则转义出错
check_file () {   # $1=文件  $2=只在新版里出现的字符串
    if [ ! -f "$1" ]; then
        echo "  [缺失] $1"
        MISSING=$((MISSING + 1))
        return
    fi

    if grep -qF "$2" "$1"; then
        echo "  [OK]   $1"
    else
        echo "  [旧版] $1"
        echo "         文件在，但还是旧内容（找不到：$2）"
        MISSING=$((MISSING + 1))
    fi
}

check_file "$PANEL_DIR/lib/common.php"              "relay_message"
check_file "$PANEL_DIR/web/index.php"               "relay_message(\$result)"
check_file "$PANEL_DIR/web/views/layout_header.php" "\$header_health['raw']['database']"
check_file "$PANEL_DIR/web/views/dashboard.php"     "排查顺序"
check_file "$PANEL_DIR/web/views/users.php"         "所有账号都能登录这个面板"

if [ "$MISSING" -gt 0 ]; then
    echo ""
    echo "!! 有 $MISSING 个文件缺失或还是旧版。"
    echo "!! 这就是页面一直报错的原因。"
    echo "!! 请先把本机 mc-link-v1/deploy/panel/ 下的对应文件传上来："
    echo "!!   deploy/panel/lib/common.php              -> $PANEL_DIR/lib/common.php"
    echo "!!   deploy/panel/web/index.php               -> $PANEL_DIR/web/index.php"
    echo "!!   deploy/panel/web/views/layout_header.php -> $PANEL_DIR/web/views/layout_header.php"
    echo "!!   deploy/panel/web/views/dashboard.php     -> $PANEL_DIR/web/views/dashboard.php"
    echo "!!   deploy/panel/web/views/users.php         -> $PANEL_DIR/web/views/users.php"
    exit 1
fi

echo ""
echo "五个文件都是新版，继续。"

echo ""
echo "=============================================="
echo " 2/4  修权限 + 强制重建镜像"
echo "=============================================="

# php-fpm 以 nobody(65534) 运行，读不到 root-only 的文件会直接 500
echo "  -- 修正文件权限（php-fpm 以 nobody 运行）"
chown -R 65534:65534 "$PANEL_DIR/web" "$PANEL_DIR/lib" 2>/dev/null || true
find "$PANEL_DIR/web" "$PANEL_DIR/lib" -type f -name '*.php' -exec chmod 640 {} \; 2>/dev/null || true
find "$PANEL_DIR/web" "$PANEL_DIR/lib" -type d -exec chmod 755 {} \; 2>/dev/null || true
if [ -f "$PANEL_DIR/config.php" ]; then
    chown 65534:65534 "$PANEL_DIR/config.php" 2>/dev/null || true
    chmod 640 "$PANEL_DIR/config.php" 2>/dev/null || true
    echo "     config.php 已设为 65534:65534 / 640"
fi

cd "$PANEL_DIR" || exit 1

echo "  -- 停止容器"
docker compose down 2>&1 | sed 's/^/     /'

# 关键：先删镜像。否则 `up -d --build` 可能直接吃构建缓存，
# COPY 那一步不重新执行，新文件永远进不去容器。
echo "  -- 删除旧镜像 $IMAGE（这一步是本次修复的关键）"
docker rmi -f "$IMAGE" 2>&1 | sed 's/^/     /' || true

echo "  -- 重新构建并启动"
docker compose up -d --build 2>&1 | sed 's/^/     /'

echo ""
echo "=============================================="
echo " 3/4  检查文件是否真的进了容器"
echo "=============================================="

INSIDE=$(docker exec mc-panel sh -c \
    'grep -c relay_message /var/www/lib/common.php 2>/dev/null || echo 0' \
    2>/dev/null | tr -d '\r')

echo "  容器内 common.php 里 relay_message 出现次数：${INSIDE:-取不到}"

if [ "${INSIDE:-0}" = "0" ]; then
    echo ""
    echo "!! 文件没有进容器。说明 Dockerfile 里 COPY 的路径和实际目录对不上。"
    echo "!! 请把下面这条的输出发给我："
    echo "     sed -n '1,60p' $PANEL_DIR/Dockerfile"
    echo "!! 以及："
    echo "     ls -la $PANEL_DIR/web/views/ $PANEL_DIR/lib/"
    exit 1
fi

echo "  文件已进容器，继续。"

echo ""
echo "=============================================="
echo " 4/4  真实请求验证"
echo "=============================================="

sleep 3

BODY=$(curl -s -m 10 "http://127.0.0.1:19102/index.php?action=dashboard")

if printf '%s' "$BODY" | grep -qF "无法从中继读取开关状态"; then
    echo "  [仍然报错] 页面还是说读不到开关状态"
    echo ""
    echo "  请把下面两条的输出发给我："
    echo "     TOKEN=$(docker compose -f server/docker-compose.yml config --format json | python3 -c 'import json,sys;print(json.load(sys.stdin)["services"]["mc-relay"]["environment"]["MCLINK_ADMIN_API_TOKEN"])')"
    echo "     curl -s -H \"Authorization: Bearer $TOKEN\" http://127.0.0.1:19101/settings"
    echo "     docker logs --tail 30 mc-panel"
    exit 1
fi

echo "  [OK] 页面不再报「无法从中继读取开关状态」"

if printf '%s' "$BODY" | grep -qF "已开启"; then
    echo "  [OK] 运行开关读出状态：已开启"
elif printf '%s' "$BODY" | grep -qF "已关闭"; then
    echo "  [OK] 运行开关读出状态：已关闭"
else
    echo "  [注意] 没找到开关状态文字，请手动打开页面确认"
fi

echo ""
echo "=============================================="
echo " 完成 —— 刷新浏览器即可"
echo "=============================================="
echo "  https://mc.example.com/index.php"
echo ""
echo "  顺手删掉两个诊断脚本（会泄露中继信息）："
echo "     rm -f $PANEL_DIR/web/_diag.php $PANEL_DIR/web/_diag2.php"
echo ""
