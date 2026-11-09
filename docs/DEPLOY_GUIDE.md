# MC-Link V2 完整部署指南

> 你的服务器参数已经通过探测脚本确认，本指南里的命令**可以直接复制执行**。
> 版本：MC-Link v2.0.0

## 你的环境（探测结果）

| 项目 | 值 |
| --- | --- |
| Docker | 29.8.0 / Compose v5.5.1 |
| MySQL 容器 | `web-mysql80`（mysql:8.0.46） |
| MySQL 端口 | `127.0.0.1:3306`（只对宿主机可见） |
| Docker 网络 | **`1panel-network`** ← 中继和面板都要加入它 |
| PHP 容器 | `web-php82`（1panel-php-fpm:8.2.33） |
| nginx | `web-nginx`（**host 网络**） |
| 现役中继 | `mc-relay`（旧版 v1，需要重建） |
| 内存 | 1.6 GB 总量，可用约 585 MB（注意余量） |
| 磁盘 | 40 GB，已用 21% |

**为什么中继必须加入 `1panel-network`**：MySQL 只映射到 `127.0.0.1:3306`，
那是**宿主机**的回环地址。容器里的 `127.0.0.1` 指的是容器自己。
所以中继要通过 Docker 网络、用容器名 `web-mysql80` 去连数据库。

---

## 一、V2 新增了什么

| 功能 | 说明 |
| --- | --- |
| 账号密码登录 | 房主和玩家**都必须登录**才能建房 / 进房 |
| 房间列表 | 客户端可查看所有在线房间、人数、状态 |
| 房主删除自己的房间 | 只有房主本人能删 |
| 有人时禁止删除 | **服务端强制拒绝**（客户端只是把按钮变灰） |
| 管理员强删房间 | 后台面板可关闭任意状态的房间 |
| 踢人 | 单个玩家 / 整个账号 |
| 数据库记录 | 用户、房间、会话、审计日志 |
| 后台监看面板 | PHP + MySQL，独立子域名 + SSL |
| 延迟显示修复 | `0.0 ms` 不再显示成 `—` |
| 实时网速 | 房主 / 玩家端显示 ↑上行 ↓下行 |

> 版本历史：这一段写的是 v2 时期的状态（当时 STUN / AUTO 都锁着，没有假实现）。
> **V3 起 STUN 解锁，V5.0 起 AUTO 解锁** —— AUTO 的语义与实现见
> `docs/AUTO-SCHEDULING-PLAN.md`（房间级 `auto` 策略 + 连接建立前有界等待 +
> 失败回落中继，`caps=auto` 能力门拒绝旧客户端）。

**数据通路一个字节都没改**。新增的全部是「多一个字段」或「新的查询连接」，
有 5 项回归测试专门盯着这一点。

---

## 二、部署服务端（中继）

### 2.1 上传新代码

把本地 `mc-link-v1/server/` 里的**所有文件**覆盖上传到 `/opt/mc-link/server/`。

⚠️ 目录结构必须是（**不要多套一层**）：

```
/opt/mc-link/server/
├── docker-compose.yml
├── Dockerfile
├── relay_server.py
├── relay_config.py
├── healthcheck.py
├── requirements.txt
├── admin_api.py
├── auth.py
├── db_store.py
├── db_schema.py
├── gatekeeper.py
├── .dockerignore
└── sql/mclink-v2-schema.sql
```

上传后先确认：

```bash
ls -l /opt/mc-link/server/
ls -l /opt/mc-link/server/sql/
```

如果发现文件名变成 `xxx.py.txt` 这类（1Panel 上传有时会这样），先改回来：

```bash
cd /opt/mc-link/server
for f in *.txt; do [ -f "$f" ] && mv "$f" "${f%.txt}"; done
```

### 2.2 建数据库和账号

**先改密码**：打开 `sql/mclink-v2-schema.sql`，把这一行改掉：

```sql
CREATE USER IF NOT EXISTS 'mclink'@'%' IDENTIFIED BY '请改成中继数据库密码';
```

然后执行（root 密码**从容器里读**，不要抄进文档或聊天记录 ——
`check-db.sh` 用的就是这一招）：

```bash
ROOT_PW="$(docker inspect web-mysql80 --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | grep '^MYSQL_ROOT_PASSWORD=' | cut -d= -f2-)"
docker exec -i web-mysql80 mysql -uroot -p"$ROOT_PW" < /opt/mc-link/server/sql/mclink-v2-schema.sql
```

验证：

```bash
docker exec -i web-mysql80 mysql -uroot -p"$ROOT_PW" -e "SHOW TABLES FROM mclink;"
```

应该看到 4 张表：`users` `rooms` `room_sessions` `relay_events`。

### 2.3 凭据写进 `.env`（**不是** docker-compose.yml）

> ★ D5：`server/docker-compose.yml` 与 `panel/docker-compose.yml` 里**只有**
> `${MCLINK_DB_PASSWORD:?...}` 这样的引用，明文凭据永远不进 compose、不进仓库。
> 真值只放在 `<安装根目录>/.env`（权限 600）。手工改 compose 里的密码是**错**的：
> 下次升级/覆盖包就被打回引用，`rebuild.sh` 还会把明文判成失败。

老部署（v3.9.20 及以前）直接跑安装器就行 —— 它会把线上凭据读进 `.env`：

```bash
bash install.sh --root /opt/mc-link --yes
```

全新机器或手工部署：建 `.env` 再填这三个值：

```bash
cp /opt/mc-link/.env.example /opt/mc-link/.env
chmod 600 /opt/mc-link/.env
```

```ini
# /opt/mc-link/.env（三条凭据都写在这里；下面用「→」描述值，
#  真的落到文件里时写成 KEY=值 —— 注意别把真实值贴进任何文档/工单）
MCLINK_DB_PASSWORD     →  和 2.2 里设的一致
MCLINK_BOOTSTRAP_ADMIN →  admin:你的管理员密码
MCLINK_ADMIN_API_TOKEN →  48 位随机串
```

生成随机 token 的命令：

```bash
openssl rand -hex 24
```

> `MCLINK_BOOTSTRAP_ADMIN` 只用来创建**第一个**管理员。
> 如果账号已存在，它**完全不会改动**（不会重置密码），所以留着也不危险。
> 创建成功后建议把它删掉或清空。

**网络名已经填好了**（`1panel-network`，你的实测结果），不用改。

### 2.4 重建容器

```bash
cd /opt/mc-link/server
docker rm -f mc-relay                     # 旧容器占用了名字，必须先删
docker compose up -d --build
docker compose ps
```

第一次构建要装 PyMySQL 和 cryptography，约 1~2 分钟。

### 2.5 确认数据库连通

```bash
docker compose logs --tail=60
```

**成功的标志：**

```text
[DB] connecting to mysql://mclink@web-mysql80:3306/mclink
[DB] 数据库连接正常，表结构已就绪
[DB] 已创建初始管理员账号： admin
[ADMIN API] listening on 0.0.0.0:19101 (仅 Docker 内网可访问)
[xxxx] Relay listening on: ('0.0.0.0', 19100)
```

**失败的标志：**

```text
[DB] 数据库不可用： ... -> 中继继续工作，但账号与面板数据不可用
```

排查顺序：

1. 网络对不对：

   ```bash
   docker inspect mc-relay --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
   ```

   应该输出 `1panel-network`

2. 从容器里能不能解析并连上 MySQL：

   ```bash
   docker exec mc-relay python -c "
   import socket
   print(socket.gethostbyname('web-mysql80'))
   s = socket.create_connection(('web-mysql80', 3306), timeout=5)
   print('tcp ok')
   "
   ```

3. 账号密码对不对（在宿主机上试）：

   ```bash
   docker exec -i web-mysql80 mysql -umclink -p'中继数据库密码' mclink -e "SELECT 1;"
   ```

   > 如果报 `Authentication plugin 'caching_sha2_password' cannot be loaded`，
   > 说明镜像里缺少 cryptography —— 重新 `docker compose up -d --build` 即可
   > （requirements.txt 里已经装了它）。

### 2.6 验证服务端

```bash
# 1) 容器健康
docker compose ps
#    期望 STATUS 里出现 (healthy)

# 2) 状态文件
docker exec mc-relay cat /app/status/status.json | python3 -m json.tool | head -40
#    关注 "database": { "healthy": true }

# 3) 管理 API 不应该能从公网访问（这是对的）
curl -m 3 http://127.0.0.1:19101/health || echo "宿主机连不上 = 符合预期"

# 4) 从容器内部调管理 API（应该成功）
docker exec mc-relay python -c "
import json, os, urllib.request
r = urllib.request.Request('http://127.0.0.1:19101/health',
      headers={'Authorization': 'Bearer ' + os.environ['MCLINK_ADMIN_API_TOKEN']})
print(json.dumps(json.loads(urllib.request.urlopen(r, timeout=5).read()), indent=1))
"

# 5) 测试登录（把密码换成你的管理员密码）
python3 - <<'EOF'
import socket
s = socket.create_connection(("127.0.0.1", 19100), timeout=8)
s.sendall(b"LOGIN __mclink__ admin 你的管理员密码\n")
print(s.recv(300).decode())
EOF
#    期望：OK LOGIN user=admin admin=1 protocol=2 token=xxxxx
```

---

## 三、部署后台面板

### 3.1 上传面板代码

把本地 `mc-link-v1/panel/` 整个目录上传到 `/opt/mc-link/panel/`。

```bash
ls -l /opt/mc-link/panel/
```

应该看到：`Dockerfile` `docker-compose.yml` `supervisord.conf` `config.sample.php`
以及 `lib/` `web/` `bin/` `nginx/` 四个目录。

> **M4 更新（2026-10-05）**：面板容器现在**自带 nginx**，站点配置由「面板设置」
> 渲染（`nginx/` 是模板，`bin/` 是渲染 + 热加载的根代理），原来那个
> `panel/nginx.conf` 已经不存在了。完整说明见 `docs/M4-SSL-DOMAIN.md`。

### 3.2 创建配置文件

```bash
cd /opt/mc-link/panel
cp config.sample.php config.php
nano config.php       # 或者用 1Panel 的文件编辑器
```

要改**四项**：

```php
'db' => [
    'host'     => 'web-mysql80',          // 容器名，不要写 127.0.0.1
    'database' => 'mclink',
    'username' => 'mclink',
    'password' => '中继数据库密码',        // 和 2.2 一致
],

'relay' => [
    'base_url' => 'http://mc-relay:19101',
    'token'    => '和中继 MCLINK_ADMIN_API_TOKEN 完全一致',
],

'panel' => [
    'session_secret' => '另一段随机字符串',   // openssl rand -hex 24
    'title' => 'MC-Link 管理面板',
],
```

### 3.3 启动面板

```bash
cd /opt/mc-link/panel
docker compose up -d --build
docker compose ps
docker compose logs --tail=30
```

宿主机上测试（应该返回 HTML 或 302）：

```bash
curl -I http://127.0.0.1:19102/login.php
```

期望看到 `HTTP/1.1 200 OK`。

### 3.4 域名与 HTTPS（M4 之后：面板自己就能做，**不需要** 1Panel 反代）

两条路，选一条：

**方式 A（推荐，M4 起）：面板自己终结 HTTPS**

1. 面板 →「面板设置」→「域名与 SSL」：
   * 绑定域名 = `mc.example.com`（只有这个域名能访问）
   * SSL 模式 = 「上传证书」（离线可用）或「Let's Encrypt 自动申请 / 续期」
2. 保存；上传证书的话在下面直接传 `fullchain.pem` + `privkey.pem`
3. 让宿主机把 80 / 443 映射加上（只在开 SSL 时需要）：
   ```bash
   mclink apply-network        # 或 bash <安装根>/scripts/panel-apply.sh
   ```
4. 访问 `https://mc.example.com`，应该看到登录页

> 域名要先解析到这台机器（A 记录）；Let's Encrypt 还需要 80 端口能从公网进来。
> **进不去的时候**：`mclink rescue` 会打印 `http://127.0.0.1:19103/` —— 那是面板的
> 本机急救入口，不判安全入口 / 不判域名 / 不跳 HTTPS。

**方式 B（旧部署仍然可用）：1Panel 反代**

1. 1Panel → **网站** → **创建网站** → 「反向代理」
2. 填写：
   * **主域名**：`mc.example.com`（或你想用的子域名）
     * 如果子域名还没解析，先去域名服务商加一条 A 记录指向服务器公网 IP
   * **代理地址**：`http://127.0.0.1:19102`
3. 创建后进入该网站 → **HTTPS** → 申请证书（Let's Encrypt，勾选自动续期）
   → 打开「强制 HTTPS」
4. 访问 `https://mc.example.com`，应该看到登录页

> 用 `127.0.0.1:19102` 是因为 `web-nginx` 跑在 **host 网络**，
> 它看到的就是宿主机的回环地址。
> [!] 走这条路时**不要**在面板里同时打开「绑定域名」+「强制 HTTPS」——
> 面板会按它自己看到的 Host 判定，反代后面的 Host 可能对不上。要么把域名绑定
> 留在面板这一侧（方式 A），要么让 1Panel 管域名（方式 B），别两边都管。

### 3.5 首次登录

用管理员账号登录（第 2.3 步里 `MCLINK_BOOTSTRAP_ADMIN` 设置的那个）。

登录后建议：

1. 进「账号」页，给朋友们创建账号
2. 进「仪表盘」确认中继状态是绿色

---

## 四、客户端使用

### 4.1 两种运行方式

**方式 A：直接用 Python（自己用）**

双击 `mc-link-v1\scripts\启动客户端.bat`

**方式 B：用打包好的 exe（发给朋友）**

`mc-link-v1\client\dist\MC-Link.exe`（12 MB，单文件，朋友不需要装 Python）

> 如果 exe 双击后没有反应，用同目录的 `MC-Link-console.exe`（带控制台）
> 看错误信息，或者运行 `MC-Link-console.exe --selfcheck` 自检。

### 4.2 房主流程

1. 进 Minecraft → 打开单人世界 → 对局域网开放（记住端口，例如 25565）
2. 打开 MC-Link 客户端
3. 点 **「登录」**（或直接点「开始连接」，会弹出登录窗口）
   * 用户名 / 密码 = 管理员给你创建的账号
4. 填写：
   * 连接模式：**Relay 中转模式**
   * 我的身份：**主机 Host**
   * 房间号：例如 `test001`（或点「随机」）
   * Minecraft 局域网端口：`25565`
5. 点 **「测试服务器连接」**，确认变绿
6. 点 **「开始连接」** → 日志出现「主机已就绪，等待玩家加入」
7. 把**房间号**发给朋友

### 4.3 玩家流程

1. 打开客户端 → **登录**（用管理员创建的账号）
2. 填写：
   * 我的身份：**玩家 Player**
   * 房间号：房主给的那个（完全一致）
   * 本地代理端口：默认 `25566`
3. 点「开始连接」→ 界面会显示 **在 Minecraft 中连接：127.0.0.1:25566**
4. Minecraft → 多人游戏 → 直接连接 → 输入 `127.0.0.1:25566`

> 想看看有哪些房间？点 **「房间列表」**，选中后点「加入选中的房间」
> 会自动把房间号填进主界面。

### 4.4 删除房间（新功能）

* **房主**：主界面点 **「删除我的房间」**
* 或者：房间列表 → 选中自己的房间 → 点「删除选中的房间」
* **房间里还有玩家时不允许删除**（按钮会提示，服务端也会拒绝）
* 想强删就找管理员：面板 → 房间 → 「强制关闭房间」

### 4.5 换世界 / 重开世界

点「停止」→ 重新开放局域网 → 再点「开始连接」。
**不需要重启 Docker，也不需要重启服务器。**

如果新世界的端口变了，把「Minecraft 局域网端口」改一下。

---

## 五、日常运维

```bash
# 中继
cd /opt/mc-link/server
docker compose ps
docker compose logs -f --tail=100      # Ctrl+C 退出，不会停容器
docker compose restart

# 面板
cd /opt/mc-link/panel
docker compose logs -f --tail=100

# 看实时状态
docker exec mc-relay cat /app/status/status.json | python3 -m json.tool

# 看房间
docker exec mc-relay python -c "
import json, os, urllib.request
r = urllib.request.Request('http://127.0.0.1:19101/rooms',
      headers={'Authorization': 'Bearer ' + os.environ['MCLINK_ADMIN_API_TOKEN']})
print(json.dumps(json.loads(urllib.request.urlopen(r, timeout=5).read()), ensure_ascii=False, indent=1))
"
```

### 内存注意事项

你的服务器只有 1.6 GB 内存、可用约 585 MB。两个新容器的上限是：

* `mc-relay`：256 MB
* `mc-panel`：192 MB

如果之后出现 OOM，可以在各自的 compose 里调小 `mem_limit`。
实测 3 人联机时中继 CPU 4%~10%、内存占用很小。

---

## 六、排错

| 现象 | 原因 / 解决 |
| --- | --- |
| `Conflict. The container name "/mc-relay" is already in use` | `docker rm -f mc-relay` 后重新 up |
| 中继日志 `数据库不可用` | 见 2.5 的三步排查 |
| 日志 `Authentication plugin 'caching_sha2_password' cannot be loaded` | 重新 build（缺 cryptography） |
| 面板提示「无法连接中继服务的后台接口」 | `config.php` 的 `relay.token` 与中继的 `MCLINK_ADMIN_API_TOKEN` 不一致 |
| 面板登录报「用户名或密码错误」 | 账号是在中继侧创建的；确认用 `MCLINK_BOOTSTRAP_ADMIN` 建的那个，或在面板「账号」页新建 |
| 客户端提示「请先登录账号」 | 正常：V2 要求登录。点「登录」 |
| 客户端提示「登录状态已失效」 | token 过期（默认 12 小时）或被管理员禁用/重置密码，重新登录 |
| 客户端提示「用户名或密码错误」 | 检查账号；密码区分大小写 |
| 提示「房间里还有玩家，不能删除」 | 正常限制。让玩家先退出，或用面板强制关闭 |
| 玩家进不去，提示房间不存在 | 房主还没点开始连接，或房间号写错 |
| exe 双击没反应 | 用 `MC-Link-console.exe` 看错误；或运行 `--selfcheck` |

### 收集排查信息

```bash
# 中继
docker logs --tail=200 mc-relay > /tmp/relay.log
docker exec mc-relay cat /app/status/status.json > /tmp/status.json

# 面板
docker logs --tail=100 mc-panel > /tmp/panel.log
```

客户端：界面点「保存日志」，或取 `%APPDATA%\MC-Link\logs\mclink-client.log`

---

## 七、回归测试

改代码后一定要跑：

```powershell
cd mc-link-v1
.\scripts\run-tests.bat
```

8 个测试套件，共 **192 项**：

| 套件 | 项目数 | 覆盖 |
| --- | --- | --- |
| `units_test.py` | 26 | 延迟格式化、测速算法 |
| `e2e_relay_test.py` | 11 | **冻结的 RAW TCP 转发**、房间生命周期 |
| `engine_session_test.py` | 11 | GUI 使用的代码路径 |
| `latency_test.py` | 11 | PING/PONG、实时网速 |
| `gui_smoke_test.py` | 41 | 界面 + 账号卡片 |
| `v2_control_test.py` | 39 | 服务端登录、删除规则、管理 API |
| `v2_client_test.py` | 19 | 客户端登录、房间列表、删房 |
| `panel_static_test.py` | 47 | 面板括号/函数/action/CSS/require |

全部不需要 VPS、不需要 Docker、不需要 Minecraft。
