#!/bin/bash
# ============================================================
#  面板 500 诊断
# ============================================================
#  用法（在服务器上执行）：
#      bash /opt/mc-link/diag-panel.sh
#
#  它会：
#    1. 把面板容器的 PHP 错误日志打开（临时），重现一次登录，然后抓出
#       真正的报错 —— 这是唯一能看到「500 到底是哪一行炸的」的办法
#    2. 顺便把 users 表结构和 role 列一起查出来
#
#  诊断完会把改动还原。不碰数据库，不碰正在玩的连接。
# ============================================================

set -u

PANEL_DIR=/opt/mc-link/panel

echo "=============================================="
echo " 1/3  users 表结构（不需要知道数据库密码）"
echo "=============================================="

docker exec -i mc-relay python - <<'PY' 2>&1
import sys
sys.path.insert(0, "/app")

import asyncio
import relay_config as cfg
from db_store import MySQLStore


async def main():
    store = MySQLStore(cfg)
    await store.connect()

    rows = await store.fetchall("DESCRIBE users")
    print("  字段名                 类型            允许空")
    print("  " + "-" * 52)
    for row in rows:
        print(f"  {row.get('Field',''):<22} {row.get('Type',''):<15} {row.get('Null','')}")

    names = [r.get("Field") for r in rows]
    print("")
    print(f"  role 列存在: {'是' if 'role' in names else '否  <<< 这就是登录 500 的原因'}")

    try:
        users = await store.fetchall(
            "SELECT id, username, is_admin, is_disabled, role FROM users ORDER BY id"
        )
        print("")
        print("  账号列表：")
        if not users:
            print("    （空的 —— users 表里一个账号都没有）")
        for u in users:
            print(f"    id={u.get('id')} {u.get('username')} "
                  f"is_admin={u.get('is_admin')} role={u.get('role')}")
    except Exception as e:
        print("")
        print(f"  查询账号失败：{e}")
        print("  -> users 表结构不对，面板登录必然 500")

    await store.close()


asyncio.run(main())
PY

echo ""
echo "=============================================="
echo " 2/3  重现登录并抓 PHP 真实报错"
echo "=============================================="

# 临时打开 php-fpm 的错误显示 + 日志，重现一次登录 POST，然后读日志。
docker exec mc-panel sh -c '
cat > /usr/local/etc/php/conf.d/zz-diag.ini <<EOF
display_errors = On
error_reporting = E_ALL
log_errors = On
error_log = /tmp/php-diag.log
EOF
kill -USR2 1 2>/dev/null || true
' 2>/dev/null

sleep 2

# 需要 CSRF：先取登录页，拿 token 和 cookie，再 POST。
JAR=$(mktemp)
TOKEN=$(curl -s -c "$JAR" "http://127.0.0.1:19102/index.php?action=login" \
    | grep -o 'name="csrf" value="[^"]*"' | head -1 | sed 's/.*value="//; s/"//')

if [ -z "$TOKEN" ]; then
    echo "  取不到 CSRF token —— 登录页本身可能就坏了。"
else
    echo "  用假账号重现一次登录 POST ..."
    curl -s -b "$JAR" -o /tmp/login-reply.html -w "  HTTP 状态码：%{http_code}\n" \
        -X POST "http://127.0.0.1:19102/index.php?action=login" \
        -d "csrf=$TOKEN" -d "username=__diag__" -d "password=__diag__"

    echo "  返回内容前几行："
    head -c 600 /tmp/login-reply.html | sed 's/^/    /'
    echo ""
fi

rm -f "$JAR"

echo ""
echo "  容器内 PHP 错误日志："
docker exec mc-panel sh -c 'cat /tmp/php-diag.log 2>/dev/null | tail -40' 2>/dev/null \
    | sed 's/^/    /' || echo "    （没有日志 —— 说明不是 PHP 致命错误）"

echo ""
echo "  nginx 错误日志："
docker exec mc-panel sh -c 'tail -20 /var/log/nginx/error.log 2>/dev/null' 2>/dev/null \
    | sed 's/^/    /' || echo "    （读不到）"

echo ""
echo "=============================================="
echo " 3/3  还原诊断设置"
echo "=============================================="

docker exec mc-panel sh -c 'rm -f /usr/local/etc/php/conf.d/zz-diag.ini' 2>/dev/null

cd "$PANEL_DIR" && docker compose restart >/dev/null 2>&1

echo "  已还原（面板已重启）。"
echo ""
echo "请把上面【1/3】和【2/3】的完整输出发回来。"
