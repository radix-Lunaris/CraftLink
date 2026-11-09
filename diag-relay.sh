#!/bin/bash
# ============================================================
#  中继为什么在重启循环 —— 抓真实崩溃原因
# ============================================================
#  用法（在服务器上执行）：
#      bash /opt/mc-link/diag-relay.sh
#
#  把完整输出发回来即可。
# ============================================================

echo "=============================================="
echo " 1/4  容器状态"
echo "=============================================="
docker ps -a --filter name=mc-relay --format '  状态: {{.Status}}'
echo ""
docker inspect mc-relay --format '  重启次数: {{.RestartCount}}' 2>/dev/null
docker inspect mc-relay --format '  退出码  : {{.State.ExitCode}}' 2>/dev/null

echo ""
echo "=============================================="
echo " 2/4  崩溃前的日志（最后 60 行）"
echo "=============================================="
docker logs --tail 60 mc-relay 2>&1 | sed 's/^/  /'

echo ""
echo "=============================================="
echo " 3/4  容器里到底有哪些 .py 文件"
echo "=============================================="
docker run --rm --entrypoint sh mc-link-relay:2.0.0 -c 'ls -1 /app/*.py 2>/dev/null' 2>&1 | sed 's/^/  /'

echo ""
echo "=============================================="
echo " 4/4  直接用容器镜像跑一次，看真实报错"
echo "=============================================="
echo "  （用 --entrypoint python，不经过 docker 的重启策略）"
echo ""

# 复用容器的环境变量，但不接数据库端口，纯粹为了看导入/启动错误。
docker run --rm --network 1panel-network \
    --env-file <(docker inspect mc-relay --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -v '^PATH=') \
    --entrypoint sh mc-link-relay:2.0.0 -c '
cd /app
echo "  --- python 语法/导入自检 ---"
python - <<PY
import sys
sys.path.insert(0, "/app")
for name in ("room_members", "room_registry", "db_store", "db_schema",
             "gatekeeper", "stun_server", "admin_api", "settings_service",
             "relay_server"):
    try:
        __import__(name)
        print(f"  OK   {name}")
    except Exception as e:
        print(f"  FAIL {name}: {type(e).__name__}: {e}")
PY
' 2>&1 | sed 's/^/  /'

echo ""
echo "=============================================="
echo " 完成 —— 把以上全部输出发回来"
echo "=============================================="
