# MC-Link V2 部署说明（当前进度）

> 这份文件是 V2 的**阶段性**部署说明。
> 完整的《部署与操作指南》会在客户端登录界面 + PHP 面板全部完成后重写。
> 现在你只需要完成 **服务端** 这一步，我才能继续往下做面板。

---

## 一、V2 现在做到哪了

### ✅ 已完成并测试通过（服务端全部 + 客户端显示优化）

| 功能 | 状态 | 验证方式 |
| --- | --- | --- |
| 延迟显示修好了（`0.0 ms` 不再显示成 `—`） | ✅ | 26 项单元测试 + 11 项延迟测试 |
| 房主端**实时网速**（↑上行 / ↓下行）+ 累计流量 | ✅ | 实测峰值 61 MB/s，闲时衰减到 0 |
| 账号密码登录（协议层） | ✅ | 39 项 V2 测试 |
| 房主和玩家都必须登录才能建房 / 进房 | ✅ | 无 token → `ERROR AUTH_REQUIRED` |
| 房间列表查看（协议层） | ✅ | `ROOMS` 查询返回房间 / 房主 / 人数 / 状态 |
| 房主删除自己的房间 | ✅ | 非房主 → `ERROR NOT_HOST` |
| **房间有人时禁止客户端删除** | ✅ | 服务端强制 → `ERROR DELETE_ROOM_BUSY` |
| 服务端可强删任意状态的房间 | ✅ | 管理 API `DELETE /rooms/<id>` |
| 房间无人自动删除 | ✅ | 房主断开立即销毁；空房按 `ROOM_EMPTY_GRACE` 清理 |
| 踢人（单个玩家 / 整个账号） | ✅ | 管理 API `DELETE /sessions/<id>`、`POST /users/<u>/kick` |
| 账号禁用后立即失效 | ✅ | 禁用后登录与旧 token 都被拒绝 |
| 审计日志（登录 / 踢人 / 强删） | ✅ | 管理 API `GET /events` |
| MySQL 持久化（用户/房间/会话/审计） | ✅ 代码完成 | 表结构自动创建；**需要你服务器上实测** |
| 管理 HTTP API（给面板用） | ✅ 39 项测试 | 只在 Docker 内网，不发布到宿主机 |

### 🚧 还没做（下一轮）

* 客户端的**登录界面**（现在核心已支持，界面还没接上）
* 客户端的**房间列表面板**（查看 / 删除按钮）
* PHP 后台监看面板
* 打包成 `MC-Link.exe`

### ⛔ 依然锁着

STUN / AUTO 模式保持 🔒 未开放，没有假实现。

---

## 二、你的下一步：三件事

### 第 1 步：跑环境探测脚本（最重要）

我在 `scripts/mclink-v2-check.sh` 写了一个**只读**探测脚本。
它会查出：MySQL 容器名、Docker 网络名、PHP 容器名、端口占用、是否有旧容器冲突。

1. 把 `scripts/mclink-v2-check.sh` 上传到服务器（1Panel 文件管理器直接拖进去）
2. 在服务器上执行：

```bash
cd /root          # 或者你上传到的目录
bash mclink-v2-check.sh > mclink-check.txt 2>&1
cat mclink-check.txt
```

3. **把输出发给我。**

我需要其中的两个值来给你准确的 `docker-compose.yml`：

* `MCLINK_DB_HOST` —— MySQL 的**容器名**（大概率是 `web-mysql80`）
* `networks.mclink.name` —— MySQL 和 PHP 所在的**网络名**（大概率是 `1panel-network`）

> 为什么必须知道网络名：你的 MySQL 只映射到 `127.0.0.1:3306`，那是**宿主机**的
> 回环地址。容器里的 `127.0.0.1` 指的是容器自己，所以中继必须通过 Docker 网络
> 用容器名去连数据库。

### 第 2 步：建库建账号

在 1Panel → 数据库 → MySQL → 终端（或 phpMyAdmin）里执行
`server/sql/mclink-v2-schema.sql`。

**执行前先把文件里这三处改掉：**

```sql
CREATE USER IF NOT EXISTS 'mclink'@'%' IDENTIFIED BY '请改成中继数据库密码';
```

* 数据库名（默认 `mclink`）要和中继配置里的 `MCLINK_DB_NAME` 一致
* 用户名（默认 `mclink`）要和中继配置里的 `MCLINK_DB_USER` 一致
* 密码要和中继配置里的 `MCLINK_DB_PASSWORD` 一致

> 会给中继单独建一个只有 `mclink` 库权限的账号，**不使用 root**。

### 第 3 步：更新中继容器

等我按你的探测结果给出最终的 `docker-compose.yml` 之后：

```bash
cd /opt/mc-link/server

# 1) 备份旧的
cp docker-compose.yml docker-compose.yml.v1.bak

# 2) 上传新的 server 目录内容（覆盖）
#    注意：目录结构不要多套一层！正确结构是
#    /opt/mc-link/server/docker-compose.yml
#    /opt/mc-link/server/Dockerfile

# 3) 处理旧容器名冲突
docker rm -f mc-relay

# 4) 重建
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=60
```

**启动日志里看到这些就说明数据库通了：**

```text
[DB] connecting to mysql://mclink@web-mysql80:3306/mclink
[DB] 数据库连接正常，表结构已就绪
[DB] 已创建初始管理员账号： admin
[ADMIN API] listening on 0.0.0.0:19101 (仅 Docker 内网可访问)
```

如果看到：

```text
[DB] 数据库不可用： ... -> 中继继续工作，但账号与面板数据不可用
```

说明网络或账号密码不对，把日志发我。**注意：这种状态下中继不会崩，
但因为我开了 `MCLINK_REQUIRE_ACCOUNT=1`，所有玩家都会被拒绝登录。**
临时救急可以把 `MCLINK_REQUIRE_ACCOUNT` 改成 `"0"`。

---

## 三、验证清单（部署后）

在服务器上执行：

```bash
# 1) 容器健康
docker compose ps
#    期望：Up ... (healthy)

# 2) 数据库连通与状态
docker exec mc-relay cat /app/status/status.json | python3 -m json.tool | head -30
#    关注： "database": { "healthy": true }

# 3) 管理 API 只在内网（宿主机上应该连不上，这是对的）
curl -m 3 http://127.0.0.1:19101/health || echo "宿主机连不上 = 符合预期（没发布端口）"

# 4) 管理 API 从容器内部可用
docker exec mc-relay python -c "
import urllib.request, os
r = urllib.request.Request('http://127.0.0.1:19101/health',
      headers={'Authorization': 'Bearer ' + os.environ['MCLINK_ADMIN_API_TOKEN']})
print(urllib.request.urlopen(r, timeout=5).read().decode())
"

# 5) 账号是否建好
docker exec -i web-mysql80 mysql -umclink -p'你的中继数据库密码' mclink \
  -e "SELECT username, is_admin, is_disabled, created_at FROM users;"

# 6) 用真实登录测试（把密码换成你的）
python3 - <<'EOF'
import socket
s = socket.create_connection(("127.0.0.1", 19100), timeout=8)
s.sendall(b"LOGIN __mclink__ admin 你的管理员密码\n")
print(s.recv(300).decode())
EOF
#    期望： OK LOGIN user=admin admin=1 protocol=2 token=xxxxx
```

> ⚠️ 最后一条测试用的是你**真实的**管理员密码，注意别把输出发到公开地方。

---

## 四、这个版本的协议变更（兼容性说明）

数据通路**一个字节都没改**。新增的全部是「多一个字段」或「新的连接类型」：

| 连接 | V1 | V2 |
| --- | --- | --- |
| 房主控制 | `HOST <房间>` | `HOST <房间> <client_id> <用户名> <token>` |
| 数据隧道 | `HOST_TUNNEL <房间>` | **完全没变** |
| 玩家 | `PLAYER <房间>` | `PLAYER <房间> <client_id> <用户名> <token>` |
| 隧道分配 | `WAIT` → `START <id>` → `READY` | **完全没变** |
| 玩家就绪 | `CONNECTED` | **完全没变** |
| 登录（新增） | 无 | `LOGIN __mclink__ <用户名> <密码>` |
| 房间列表（新增） | 无 | `ROOMS __mclink__ <client_id> <用户名>` |
| 删除房间（新增） | 无 | `DELETE __mclink__ <client_id> <用户名> <token> <房间>` |
| 自助改名（新增） | 无 | `RENAME __mclink__ <client_id> <用户名> <token> <新用户名>` |
| 房主控制（新增） | `PING` | 多了 `DELETE <token>` |

不带 token 的老客户端仍然能被正确解析，只是会被
`ERROR AUTH_REQUIRED` 拒绝 —— 这就是我们想要的行为。

新增的错误码：

```text
AUTH_REQUIRED          需要先登录
AUTH_INVALID_TOKEN     token 无效或已过期
AUTH_DISABLED          账号被禁用
AUTH_BAD_CREDENTIALS   用户名或密码错误
AUTH_RATE_LIMIT        尝试过于频繁
AUTH_UNAVAILABLE       服务器数据库暂时不可用
ROOM_NOT_FOUND         房间不存在
NOT_HOST               你不是这个房间的房主
DELETE_ROOM_BUSY       房间还有玩家，不能删除
BAD_USERNAME           用户名不合法（2-32 字符，不能有空格）
USERNAME_TAKEN         这个用户名已经被别人用了
RENAME_LIMIT           自助改名次数用完（30 天 3 次）
```

### 改名的两条路径（重要）

| | 面板（管理员） | 客户端（玩家自助） |
| --- | --- | --- |
| 次数限制 | **无限制** | 30 天 3 次 |
| 生效速度 | 立即 | 立即 |
| 是否踢下线 | **不踢** | **不踢** |
| 接口 | HTTP `POST /users/<用户名>/rename` | TCP `RENAME __mclink__ ...` |

两条路径都调用同一个 `AccountService.rename_user()`，改名后：

1. `rename_live_identity()` 把**正在运行**的房间 / 会话里的房主名、玩家名就地改掉，
   房间不用重建，玩家不用重连；
2. `TokenManager.rebind_user()` 把该账号**所有在线 token** 指向新用户名，
   所以客户端不会被登出，token 也不会失效。

被拒绝的改名（重名、非法用户名）**不消耗**改名次数。

---

## 五、测试情况

```powershell
cd mc-link-v1
.\scripts\run-tests.bat
```

| 测试套件 | 项目数 | 覆盖 |
| --- | --- | --- |
| `units_test.py` | 26 | 延迟格式化、测速算法 |
| `e2e_relay_test.py` | 11 | 冻结的 RAW TCP 转发、房间生命周期 |
| `engine_session_test.py` | 11 | GUI 使用的代码路径 |
| `latency_test.py` | 11 | PING/PONG、实时网速 |
| `gui_smoke_test.py` | 28 | 界面 |
| `v2_control_test.py` | 39 | 登录、权限、删除规则、管理 API |
| **合计** | **126** | 全部通过 |

其中专门验证「没有为了新功能破坏老链路」的几项：

* `HOST_TUNNEL still replies with the frozen WAIT`
* `player receives the frozen CONNECTED reply`
* `raw Minecraft bytes still flow player -> tunnel`（7680 字节原样往返）
* `raw Minecraft bytes still flow tunnel -> player`
* `host can re-create the SAME room without restarting relay`

---

## 六、这一轮修掉的两个真实 bug

除了你要的功能，测试还抓出两个服务端问题：

1. **被拒绝的 HOST 会留下幽灵房间**
   以前先建房再校验权限，导致一次失败的连接会在房间列表和数据库里留下
   一个永远没人管的空房间。现在改成「先检查、后创建」。

2. **删除权限可以被绕过**
   原来的所有权判断写成 `if room.host_username and ...`，
   当房间没有记录房主名时会**放行任何人删除**。现在改成缺房主名就拒绝。

这两个都是我自己写出来又被自己的测试抓到的，已经补上了对应的回归项。
