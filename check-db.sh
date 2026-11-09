# 直接查数据库，不依赖中继代码版本
#
# 这三条会明确回答：users 表结构对不对、有没有 role 列、账号是什么角色。
# 把完整输出发回来即可。

echo "=== 1. users 表结构 ==="
docker exec -i web-mysql80 mysql -uroot -p"$(docker inspect web-mysql80 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^MYSQL_ROOT_PASSWORD=' | cut -d= -f2-)" mclink -e "DESCRIBE users;" 2>&1 | grep -v "Using a password"

echo ""
echo "=== 2. 有没有 role 列 ==="
docker exec -i web-mysql80 mysql -uroot -p"$(docker inspect web-mysql80 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^MYSQL_ROOT_PASSWORD=' | cut -d= -f2-)" mclink -e "SELECT COUNT(*) AS role_column_exists FROM information_schema.columns WHERE table_schema='mclink' AND table_name='users' AND column_name='role';" 2>&1 | grep -v "Using a password"

echo ""
echo "=== 3. 账号与角色 ==="
docker exec -i web-mysql80 mysql -uroot -p"$(docker inspect web-mysql80 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^MYSQL_ROOT_PASSWORD=' | cut -d= -f2-)" mclink -e "SELECT id, username, is_admin, is_disabled, role FROM users ORDER BY id;" 2>&1 | grep -v "Using a password"
