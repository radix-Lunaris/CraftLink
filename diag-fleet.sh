#!/bin/bash
# ============================================================
#  CraftLink 现场体检（只读，不重启任何东西）
# ============================================================
#  用法（在服务器上执行）：
#      bash /opt/mc-link/diag-fleet.sh
#
#  把全部输出发回来即可。
#
#  它回答三个问题：
#    1) 中继/面板此刻跑的到底是哪一份代码（对比磁盘上的哈希）
#    2) 中继为什么起不来（最后几条 [DB] 日志点出卡在哪一步）
#    3) 数据库/19100/19101 此刻的真实状态
# ============================================================

echo "=============================================================="
echo " 0/5  时间与文件情况"
echo "=============================================================="
date
echo ""
echo "-- /opt/mc-link 下的关键文件时间戳 --"
ls -l --time-style=long-iso \
    /opt/mc-link/server/relay_server.py \
    /opt/mc-link/server/db_store.py \
    /opt/mc-link/server/relay_config.py \
    /opt/mc-link/rebuild.sh 2>&1

echo ""
echo "=============================================================="
echo " 1/5  容器状态"
echo "=============================================================="
docker ps -a --format '  {{.Names}}\t{{.Status}}\t{{.Image}}' | grep -E 'mc-relay|mc-panel|mysql' || true
echo ""
docker inspect mc-relay --format '  中继: status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}}' 2>&1

echo ""
echo "=============================================================="
echo " 2/5  19100 到底有没有在监听（这是唯一真正要紧的事）"
echo "=============================================================="
echo -n "  从容器内部连 19100: "
docker exec mc-relay python -c "import socket,sys; s=socket.socket(); s.settimeout(2); print('OK 在监听' if s.connect_ex(('127.0.0.1',19100))==0 else 'NO 没在监听')" 2>&1
echo -n "  从宿主机连 19100 : "
timeout 3 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/19100' 2>/dev/null && echo "OK 通" || echo "NO 不通"
echo -n "  容器里的健康自检 : "
docker exec mc-relay python healthcheck.py 2>&1; echo "    (exit=$?)"

echo ""
echo "=============================================================="
echo " 3/5  中继卡在哪一步（最后几条数据库日志）"
echo "=============================================================="
docker logs mc-relay 2>&1 | grep -E '\[DB\]|listening|\[SETTINGS\]' | tail -12 | sed 's/^/  /'
echo ""
echo "  -- 容器日志最后 25 行 --"
docker logs --tail 25 mc-relay 2>&1 | sed 's/^/  /'

echo ""
echo "=============================================================="
echo " 4/5  跑的到底是哪一份代码（容器内 vs 磁盘上）"
echo "=============================================================="
for f in relay_server.py db_store.py relay_config.py; do
    host=$(md5sum "/opt/mc-link/server/$f" 2>/dev/null | awk '{print $1}')
    inside=$(docker exec mc-relay md5sum "/app/$f" 2>/dev/null | awk '{print $1}' | tr -d '\r')
    if [ -z "$inside" ]; then
        echo "  [$f] 读不到容器内文件（容器没在跑？）"
    elif [ "$host" = "$inside" ]; then
        echo "  [OK]   $f: 容器内 = 磁盘上"
    else
        echo "  [旧!]  $f: 容器里跑的是旧代码  磁盘=$host 容器=$inside"
    fi
done
echo ""
echo -n "  中继是否带本轮修复（db_step）: "
docker exec mc-relay grep -c "def db_step(" /app/relay_server.py 2>/dev/null | tr -d '\r' || echo "读不到"
echo -n "  中继是否带本轮配置（DB_BOOT_TIMEOUT）: "
docker exec mc-relay grep -c "MCLINK_DB_BOOT_TIMEOUT" /app/relay_config.py 2>/dev/null | tr -d '\r' || echo "读不到"

echo ""
echo "=============================================================="
echo " 5/5  数据库本身有没有病"
echo "=============================================================="
echo "-- 中继能不能连上数据库（真实驱动报错）--"
docker exec mc-relay python relay_server.py --dbcheck 2>&1 | tail -20 | sed 's/^/  /'
echo ""
echo "-- 有没有还没提交的长事务（就是它让 ALTER/DELETE 排队）--"
docker exec -i web-mysql80 mysql -uroot -p'YOUR_ROOT_PASSWORD' -e \
    "SELECT trx_id, trx_state, trx_started, trx_mysql_thread_id, LEFT(trx_query,60) AS q FROM information_schema.innodb_trx;" 2>&1 | sed 's/^/  /'
echo ""
echo "-- 正在等锁的语句 --"
docker exec -i web-mysql80 mysql -uroot -p'YOUR_ROOT_PASSWORD' -e \
    "SELECT OBJECT_NAME, LOCK_TYPE, LOCK_STATUS, THREAD_ID FROM performance_schema.metadata_locks WHERE LOCK_STATUS='PENDING';" 2>&1 | sed 's/^/  /'

echo ""
echo "=============================================================="
echo " 完成 —— 把以上全部输出发回来"
echo "=============================================================="
