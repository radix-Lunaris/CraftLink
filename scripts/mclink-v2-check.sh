#!/usr/bin/env bash
# ============================================================
#  MC-Link V2 -- 服务器环境探测脚本
# ============================================================
#
#  作用：在你部署 V2 之前，自动查出下面这些必须知道的信息：
#    1. MySQL 容器的名称
#    2. MySQL 所在的 Docker 网络名
#    3. PHP 容器的名称和网络
#    4. 19100 / 19101 端口占用情况
#    5. mc-relay 旧容器是否存在
#
#  用法（复制整段到服务器终端执行）：
#      bash mclink-v2-check.sh
#
#  或者：
#      bash mclink-v2-check.sh > mclink-check.txt 2>&1
#      然后把 mclink-check.txt 发给我
#
#  这个脚本只读取信息，不会修改、不会删除、不会重启任何东西。
# ============================================================

set -u

line() { printf '%s\n' "------------------------------------------------------------"; }

line
echo "MC-Link V2 环境探测   $(date '+%Y-%m-%d %H:%M:%S')"
line

# ------------------------------------------------------------
echo
echo "[1] Docker 版本"
docker --version 2>/dev/null || echo "  !! 找不到 docker 命令"
docker compose version 2>/dev/null || echo "  !! 找不到 docker compose"

# ------------------------------------------------------------
echo
echo "[2] 所有运行中的容器 (名称 / 镜像 / 端口)"
line
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null || true

# ------------------------------------------------------------
echo
echo "[3] 所有容器（包括已停止的）—— 关注是否有旧的 mc-relay"
line
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null || true

# ------------------------------------------------------------
echo
echo "[4] 数据库容器定位"
line
MYSQL_CONTAINERS=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'mysql|mariadb' || true)

if [ -z "$MYSQL_CONTAINERS" ]; then
  echo "  !! 没有找到名字里带 mysql/mariadb 的运行中容器"
  echo "     如果你的数据库不是容器，而是宿主机直装的，请告诉我。"
else
  for c in $MYSQL_CONTAINERS; do
    echo "  容器: $c"
    echo "    镜像   : $(docker inspect "$c" --format '{{.Config.Image}}' 2>/dev/null)"
    echo "    网络   : $(docker inspect "$c" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)"
    echo "    端口   : $(docker port "$c" 2>/dev/null | tr '\n' ' ')"
    echo "    环境   : $(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -iE 'MYSQL_DATABASE|MYSQL_USER|MYSQL_ROOT' | tr '\n' ' ')"
  done
fi

# ------------------------------------------------------------
echo
echo "[5] PHP 容器定位"
line
PHP_CONTAINERS=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'php' || true)

if [ -z "$PHP_CONTAINERS" ]; then
  echo "  !! 没有找到名字里带 php 的运行中容器"
else
  for c in $PHP_CONTAINERS; do
    echo "  容器: $c"
    echo "    镜像   : $(docker inspect "$c" --format '{{.Config.Image}}' 2>/dev/null)"
    echo "    网络   : $(docker inspect "$c" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)"
    echo "    端口   : $(docker port "$c" 2>/dev/null | tr '\n' ' ')"
  done
fi

# ------------------------------------------------------------
echo
echo "[6] 所有 Docker 网络（找出共用的那个）"
line
docker network ls 2>/dev/null || true

echo
echo "  各网络下的容器："
for net in $(docker network ls --format '{{.Name}}' 2>/dev/null); do
  members=$(docker network inspect "$net" --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null)
  if [ -n "$members" ]; then
    echo "    $net : $members"
  fi
done

# ------------------------------------------------------------
echo
echo "[7] 端口占用"
line
echo "  19100 (中继，公网):"
ss -lntp 2>/dev/null | grep -E ':19100\b' || echo "    未监听（正常，还没部署）"
echo "  19101 (管理 API，内网):"
ss -lntp 2>/dev/null | grep -E ':19101\b' || echo "    未监听（正常）"
echo "  3306 (MySQL):"
ss -lntp 2>/dev/null | grep -E ':3306\b' || echo "    未监听"
echo "  9000 (PHP):"
ss -lntp 2>/dev/null | grep -E ':9000\b' || echo "    未监听"

# ------------------------------------------------------------
echo
echo "[8] 现有的 mc-relay 容器（部署前必须确认）"
line
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'mc-relay'; then
  echo "  !! 已存在名为 mc-relay 的容器："
  docker ps -a --filter 'name=mc-relay' --format '    {{.Names}}  {{.Status}}  {{.Image}}'
  echo
  echo "  部署新版前需要执行：  docker rm -f mc-relay"
else
  echo "  没有冲突，可以直接部署。"
fi

# ------------------------------------------------------------
echo
echo "[9] 已部署的 mc-link 目录"
line
if [ -d /opt/mc-link ]; then
  find /opt/mc-link -maxdepth 3 2>/dev/null | sort
else
  echo "  /opt/mc-link 不存在（还没部署过）"
fi

# ------------------------------------------------------------
echo
echo "[10] 磁盘与内存余量"
line
df -h / 2>/dev/null | tail -2
free -h 2>/dev/null | head -3

line
echo "探测完成。把上面的输出发给我，我按你的实际环境给部署参数。"
line
