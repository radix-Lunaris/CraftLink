# MC-Link 协议规范与冻结说明

本文件说明 MC-Link 的连接协议，以及**哪些代码可以改、哪些绝对不能改**。

如果有人（包括未来的我）想重构中继逻辑，请先读完这一页。

---

## 1. 为什么要有这份文件

项目历史上有过一次教训：

> 纯 Python TCP Relay 原型已经**真实 Minecraft 联机成功**（房主 + 最多 3 人实测稳定）。
> 之后为了加 GUI、加用户名、加房间管理做了一次大重构（MC-Link V1.1），
> 结果 UI 客户端**连不上了**。
> 最大嫌疑就是：重构时改变了原本已经验证成功的数据通路或握手协议。

因此本项目的第一原则是：

```
冻结已成功的 TCP Core  →  独立测试  →  修复生命周期  →  再真实测试
        →  加功能（CLI/引擎）  →  再测试
        →  GUI 只做外壳  →  再测试
        →  打包 exe
```

**绝不**：重新设计协议 + 重写服务端 + 重写客户端 + 加 GUI 一次性全改。

V1.0.0 的做法是：把归档里三份已验证的文件作为 **Known Good Core**，
逐行沿用其 TCP 机制，只在外围增加：配置读取、状态统计、生命周期清理、GUI 适配层。

---

## 2. 角色与连接类型

一个客户端进程启动后，只扮演**一个**角色。房间内一共有三类连接：

| 连接类型 | 谁发起 | 作用 | 生命周期 |
| --- | --- | --- | --- |
| `HOST` | 房主客户端 | 房主控制连接，**不传游戏数据** | 房主在线期间一直保持 |
| `HOST_TUNNEL` | 房主客户端 | 数据隧道，房主启动时预先建 10 条 | 每条隧道循环复用 |
| `PLAYER` | 玩家客户端 | 每个 Minecraft 连接对应一条 | Minecraft 断开即结束 |

```
        PLAYER ──┐
        PLAYER ──┤
        PLAYER ──┼──►  RELAY  ──►  HOST_TUNNEL
                 │      │              │
        HOST ────┘      │              ▼
      (控制，无数据)     │        房主本机 Minecraft
                         │        127.0.0.1:<局域网端口>
                    不解析任何字节
```

---

## 3. 完整协议

### 3.1 通用规则

* 传输：纯 TCP，**没有** TLS、没有 HTTP
* 行协议：以 `\n` 结尾，UTF-8 编码，字段用空格分隔
* **第一行是握手行**，服务器必须在 10 秒内收到，否则断开
* 在任何一条连接上，控制协议只在**数据转发开始之前**使用

### 3.2 角色 1：HOST（房主控制连接）

客户端第一行：

```
HOST <room> <client_id> <username>\n
```

* `<room>`：房间号，1～64 字符，**不能含空格或控制字符**
* `<client_id>`：本机随机 ID（16 位十六进制），V1 新增，**可省略**
* `<username>`：显示用昵称，最长 32 字符，V1 新增，**可省略**
* 兼容性：旧客户端只发 `HOST <room>`，服务器照样接受

服务器应答：

```
OK HOST room=<room>\n
```

失败时：

```
ERROR ROOM_ALREADY_HAS_HOST\n     # 该房间已有房主
ERROR ROOM_CLOSED\n               # 房间对象已销毁（极小概率竞态）
ERROR BAD_ROOM\n                  # 房间号非法
ERROR BAD_HANDSHAKE\n             # 字段数量不对
```

握手成功后，这条连接**只用于控制**，永远不承载 Minecraft 数据。

### 3.3 角色 2：HOST_TUNNEL（数据隧道）

客户端第一行：

```
HOST_TUNNEL <room>\n
```

服务器先应答：

```
WAIT\n                            # 已进入空闲隧道池
```

失败时：

```
ERROR NO_HOST\n                   # 该房间没有房主
ERROR BAD_ROOM\n
ERROR BAD_HANDSHAKE\n
```

当某个玩家被配对到这条隧道时，服务器发送：

```
START <session_id>\n              # session_id 是 8 位十六进制
```

房主客户端收到 `START` 后：

1. 连接本机 Minecraft：`127.0.0.1:<mc_port>`
2. 连接成功后，向隧道发送：

```
READY\n
```

3. **从这一刻起，这条连接上只允许出现 Minecraft 原始字节**

如果房主本地 Minecraft 连不上（世界关了、端口填错），房主客户端直接关闭隧道，
服务器会清理这个会话，玩家端会断开。

### 3.4 角色 3：PLAYER（玩家）

客户端第一行：

```
PLAYER <room> <client_id> <username>\n
```

服务器应答（**这是数据转发前的最后一条控制消息**）：

```
CONNECTED\n
```

失败时：

```
ERROR NO_HOST\n                   # 房间不存在或房主不在线
ERROR ROOM_FULL\n                 # 达到最大人数
ERROR BAD_ROOM\n
ERROR BAD_HANDSHAKE\n
```

收到 `CONNECTED` 后：**这条连接上只允许出现 Minecraft 原始字节**。

### 3.5 服务器 → 房主控制连接的附加消息（V1 新增，可选）

这些消息**只在 HOST 控制连接上**出现（因为那条连接本来就不传游戏数据）：

```
PONG <原样回显>\n                                  # 应答客户端的 PING
INFO players=2/10 idle_tunnels=8 waiting_players=0\n
```

客户端 → 服务器：

```
PING <任意字符串>\n                                # 建议填高精度时间戳
```

用途：测量「客户端 ↔ 中转服务器」的真实往返延迟，并防止 NAT 静默丢连接。

**为什么放在 HOST 控制连接而不是 PLAYER 连接？**
因为 PLAYER 连接在 `CONNECTED` 之后必须是纯数据流，
任何额外写入都可能被 Minecraft 当成协议内容。而 HOST 控制连接天然是空的。

**为什么 PING 不放进数据隧道？**
因为数据隧道里是 Minecraft 的原始字节，插入任何内容都会破坏游戏协议。

### 3.5b V2 控制面连接（新增，一律使用哨兵房间名 `__mclink__`）

每一种都**自己开一条短连接**，只发一行然后关闭。哨兵名 `__mclink__` 不可能
和真实房间重名，所以老服务端只会回 `ERROR UNKNOWN_ROLE`，不会误解。

```
LOGIN  __mclink__ <用户名> <密码> <client_id>
    ◄── OK LOGIN user=<名> admin=0/1 protocol=2 token=<token>
    ◄── ERROR AUTH_BAD_CREDENTIALS / AUTH_DISABLED / AUTH_RATE_LIMIT /
              AUTH_UNAVAILABLE / AUTH_BAD_REQUEST / AUTH_DISABLED_SERVER

ROOMS  __mclink__ <client_id> <用户名>
    ◄── ROOMS <n>
        ROOM <房间> <房主> <玩家>/<上限> <空闲隧道> <等待玩家> <empty|busy>
        DONE

DELETE __mclink__ <client_id> <用户名> <token> <房间>
    ◄── OK DELETE room=<房间>
    ◄── ERROR NOT_HOST / DELETE_ROOM_BUSY / ROOM_NOT_FOUND /
              AUTH_REQUIRED / AUTH_INVALID_TOKEN / AUTH_DISABLED

RENAME __mclink__ <client_id> <用户名> <token> <新用户名>
    ◄── OK RENAME user=<新用户名>
    ◄── ERROR BAD_USERNAME / USERNAME_TAKEN / RENAME_LIMIT /
              AUTH_REQUIRED / AUTH_INVALID_TOKEN / AUTH_DISABLED /
              AUTH_DISABLED_SERVER / RENAME_FAILED
```

`RENAME` 是**客户端自助改名**：谁被改名由 **token 决定**，请求里的用户名只用于
日志，所以客户端不可能改别人的号。限制是 30 天 3 次；被拒绝的改名不消耗次数。
改名**不会踢下线**：

```
改名成功
  → rename_live_identity()  把正在运行的房间/会话里的名字就地改掉（房间不重建）
  → TokenManager.rebind_user()  把该账号所有在线 token 指向新用户名（不登出）
```

面板改名走 HTTP（`POST /users/<用户名>/rename`），**无次数限制、同样不踢下线**。

### 3.6 错误码对照

| 错误码 | 含义 | 客户端提示 |
| --- | --- | --- |
| `NO_HOST` | 房间不存在或房主不在线 | 房间不存在或房主不在线 |
| `ROOM_FULL` | 房间已满 | 房间已满（达到最大人数） |
| `ROOM_ALREADY_HAS_HOST` | 房间已有房主 | 该房间已经有房主了 |
| `BAD_HANDSHAKE` | 握手字段不对 | 协议握手被服务器拒绝 |
| `BAD_ROOM` | 房间号非法 | 房间号非法（1-64 个字符，不能有空格） |
| `UNKNOWN_ROLE` | 角色不认识 | 客户端与服务端版本不匹配 |
| `ROOM_CLOSED` | 房间已销毁 | 房间已关闭 |
| `AUTH_*` | V2 认证错误 | 见 `protocol.py` 的 `ERROR_TEXT` |
| `BAD_USERNAME` | 用户名不合法 | 用户名不合法（2-32 个字符，不能有空格） |
| `USERNAME_TAKEN` | 用户名被占用 | 这个用户名已经被别人用了 |
| `RENAME_LIMIT` | 自助改名次数用完 | 改名次数已用完（30 天内最多 3 次） |

---

## 4. 一次完整的数据流转（房主 + 1 玩家）

```
时间     房主客户端              中继服务器                玩家客户端
────────────────────────────────────────────────────────────────────────
t0       HOST room       ──────►
                          ◄────── OK HOST room
t1       HOST_TUNNEL     ──────►
                          ◄────── WAIT                       （×10 条）
t2                                                  PLAYER room ──────►
                          （配对：隧道#1 ↔ 玩家）
         ◄────── START s1                （玩家继续等待）
t3       连接 127.0.0.1:25565
         READY            ──────►
                          INFO players=... ◄────►（推送给房主）
                          ──────► CONNECTED                  ◄──┐
t4       ◄════════════ 原始 Minecraft TCP 字节 ════════════►     │
                                                                  │
t5       玩家退出世界                     ROOM 会话清理 ◄─────────┘
         （隧道#1 转到下一位玩家，或重连服务器）
```

---

## 5. 冻结清单（改动前必读）

### 5.1 绝对不能改

| 位置 | 内容 |
| --- | --- |
| `server/relay_server.py` | `copy_stream()` 的整体结构（单向读取 → 写入 → drain） |
| `server/relay_server.py` | `bidirectional_relay()`（**一条数据隧道只能有一个 task 负责双向搬运**） |
| `server/relay_server.py` | `try_pair()` 里 `START <session_id>` 的发送时机 |
| `server/relay_server.py` | `handle_host` / `handle_host_tunnel` / `handle_player` 中的握手收发顺序 |
| `client/mclink/core/host_core.py` | `tunnel_loop()` 的握手顺序：`WAIT` → `START` → 连本机 MC → `READY` |
| `client/mclink/core/host_core.py` | `relay_mc()` |
| `client/mclink/core/player_core.py` | `connect_relay()` 的握手行、`raw_relay()` |
| `client/mclink/core/protocol.py` | 所有协议字符串常量 |
| 两端 | 缓冲区大小 `64 * 1024` |
| 两端 | 每个 socket 都要 `TCP_NODELAY` |

### 5.2 可以改（V1 就是在这些地方做的改进）

* 日志内容与格式
* GUI 界面
* 状态统计与 `status.json`
* 生命周期清理路径（`finally` / `room.close()` / `remove_session()`）
* 配置读取（环境变量、JSON 文件）
* 错误提示文案
* 控制连接上的 PING / INFO（不触碰数据隧道）

### 5.3 V1 相对归档版本的具体改动

| 改动 | 文件 | 原因 |
| --- | --- | --- |
| 等待配对的连接从 `asyncio.Event` 唤醒，替代 0.05s 轮询 | `relay_server.py` | 房间关闭时等待者能立刻退出，不再卡住 |
| `remove_session()` 改为幂等 | `relay_server.py` | 多条清理路径重复调用不会出错 |
| 房主断开 → `room.close()` + 从注册表移除 | `relay_server.py` | 修复「房主重开世界连不上」的 bug |
| `drop_room_if_empty()` 校验对象身份 | `relay_server.py` | 防止陈旧任务误删同名的新房间 |
| 新增 `PING` / `PONG` / `INFO`（仅 HOST 控制连接） | 两端 | 延迟显示与状态推送 |
| 第一行支持可选的 `client_id` / `username` | 两端 | 向后兼容（老客户端只发两个字段） |
| 命令行封装成 `MCLinkEngine` + Session | `client/` | GUI 只做外壳，不复制网络代码 |

> 这些改动**没有**触碰任何一条数据路径。

---

## 6. 安全约束（服务端必须遵守）

* 房间号 / 用户名 / Client ID 只允许作为**字典键、日志字段、回显内容**
* **绝不**作为路径、命令、SQL、模板使用
* 代码中**不使用** `eval` / `exec` / `os.system` / `subprocess` 处理客户端输入
* 所有控制消息按空格分字段解析，不做任何字符串求值
* 长度与字符校验：房间号 1～64、用户名 ≤32、Client ID ≤64，
  禁止空白与控制字符（`ord(c) < 33` 或 `== 127`）
* 首包握手超时 10 秒
* 单房间最多 `MAX_PLAYERS`（默认 10）个会话
* 一个房间只允许一个房主

客户端发送 `rm -rf /` 或 `del C:\` 只会被当成普通字符串（并因含空格而被房间号校验拒绝）。

---

## 7. 回归测试（每次改动后必须全绿）

```powershell
cd mc-link-v1
.\scripts\run-tests.bat
```

| 测试 | 覆盖 | 项目数 |
| --- | --- | --- |
| `tests/e2e_relay_test.py` | 握手、隧道池、256 KiB 二进制完整往返、并发 2 玩家、房主断开后房间清理、同房间号重建、status.json | 11 |
| `tests/engine_session_test.py` | `MCLinkEngine` 跨线程启动/停止、Host/Player 会话状态机、PING/PONG、双客户端互不干扰、重启无残留 | 11 |
| `tests/gui_smoke_test.py` | 界面构建、控件存在性、模式锁定、角色切换、参数校验、日志渲染、事件渲染、状态面板 | 23 |

合计 **45 项**。

测试全部在 `127.0.0.1` 上跑：临时启动一个中继服务端，
用一个 TCP echo 服务器冒充「房主的 Minecraft 局域网世界」，
然后完整走一遍房主 + 玩家的全流程。

**不需要 VPS，不需要 Minecraft，不需要 Docker。**

---

## 8. V3 新增：房间状态字段与错误码（ADDITIVE，2026-09-28）

> 这一节是给**客户端**看的精确契约。全部是**新增 token / 新增错误码**：
> 老客户端按空格分词，不认识的 `key=value` 直接忽略；老中继也不会发出这些
> 字段。任何一条都不改动上面冻结的握手与数据通路。

### 8.1 `HOST_STATE`（服务器 → 每个房间成员的控制连接）

```
HOST_STATE host=<0|1> host_name=<name|-> host_id=<cid|-> members=<n> world=<n> peers=<cid,cid|-> names=<cid:encname,...|-> rtts=<cid:ms,...|-> started=<cid,cid|-> conn=<cid>:<state>,... mode=<relay|stun>
```

| 字段 | 精确格式 | 含义 |
| --- | --- | --- |
| `host_id` | `host_id=<client_id>`，没有 Host 时 `host_id=-` | **当前 Host 的 client_id**。客户端用它和 `peers=` 做**精确匹配**，把打洞目标定死（不能靠 `host_name=` 猜，可能重名）。 |
| `started` | `started=<cid>,<cid>,...`，一个都没有时 `started=-` | **已经开始连接的成员** client_id 列表（逗号分隔，顺序无关，不重复）。"已开始连接"= 该成员此刻有 PLAYER 连接（等待配对或已配对），或他是当前 Host 且有 `HOST <room>` 开服控制连接，**或**他在成员控制连接上显式上报过 `STARTED 1`（见 §8.6）。 |
| `conn` | `conn=<cid>:<state>,<cid>:<state>,...`，没有成员时 `conn=-` | **每个成员此刻的游戏连接状态**（`state` ∈ `idle`/`waiting`/`pairing`/`connected`，精确口径见 §8.5）。 |

> **不变量**：`host_id=X` 时 `started=` 里**必须**包含 `X`（同一个 client_id
> 来源）。Host 侧做 `started ∩ peers` 时当前 Host 一定在结果里；否则 Host
> 一个目标都拉不到。

**客户端用法**（设计基准 §D/§E）：

* Player：每轮打洞前只向 `host_id=`（或 `host_name=` 对应的人）打，**与谁创建房间无关**；
  不得复用上一轮的 Host / candidate。
* Host：用 `started=` 过滤 `peers=`，只向**已经开始连接**的 Player 打；C 走了、
  E 来了，列表随下一次 `HOST_STATE` 自动更新。
* `-` 一律解成"没有"（`host_id=-` = 没有 Host；`started=-` = 还没有人开始）。

### 8.2 `OK JOIN` 回包新增 `host_id=`

```
OK JOIN room=<token> name=<encname> owner=<0|1> host=<0|1> host_id=<cid|-> members=<n> max_players=<n> server_max_players=<n> host_name=<name|-> guest=<token|-> you=<encname>
```

`host_id=` 插在 `host=` 之后，位置字段一个都没动；老客户端忽略它。

### 8.3 新增 / 变化的错误码

| 行 | 何时出现 | 客户端应做什么 |
| --- | --- | --- |
| `ERROR NO_HOST`（既有） | 房间里**没人认领**主机位时，PLAYER / HOST_TUNNEL 被拒 | 提示「当前房间暂无 Host，请等待 Host 认领」 |
| `ERROR HOST_NOT_READY Host 尚未开始连接，请等待 Host 开服`（**新增**） | 主机位**有人认领**、但 Host 还没开服（没有 `HOST <room>` 控制连接）时，PLAYER / HOST_TUNNEL 被拒 | 提示「Host 未连接，请等待 Host 开服」；**不得进入正式打洞流程**，等 `HOST_STATE` 的 `started=` 里出现 Host 后再手动点开始连接 |
| `ERROR CONN_MODE_CHANGED 连接模式已切换，请按新模式重新开始连接`（**新增**） | Owner 切模式时，还在等待配对的 PLAYER / HOST_TUNNEL socket 收到 | 按新模式**严格重建全部通路**；这是"模式热切换"触发的，不是网络抖动 |
| `CONN_MODE_CHANGED 连接模式已切换，请按新模式重新开始连接`（**新增**，成员控制连接上**不带 `ERROR` 前缀**） | 同上，但发给**每一个成员**的控制连接 | 同 `HOST_LEFT` 的收法：这是"模式变了"的可识别原因 |
| `ERROR NOT_HOST 连接模式由房间所有者决定，其他成员不能修改`（码不变，文案更新） | **非 Owner**（包括非 Owner 的 Host）发 `CONNMODE` | 连接模式只有 Owner 能改；非 Owner 的 UI 应锁定模式控件 |

### 8.4 Host 掉线时的既有原因码（不变）

* 成员心跳超时 / 被踢 / 主动退房导致主机位释放时，还在配对池里的
  PLAYER / HOST_TUNNEL 收到 `ERROR HOST_LEFT <原因>`；已进世界的会话只关
  socket，原因通过成员控制连接上的 `HOST_LEFT <原因>` 广播。
* **房间保留**（除非离开的是 Owner），任何人可以重新认领 Host。

### 8.5 `conn=` —— 每成员游戏连接状态（ADDITIVE，2026-09-28）

> 用户原话：「这边玩家断开连接，那边 host 这个连接状态还是显示已连接，未
> 及时更正状态，所以我觉得可以把连接状态也包进我们管理广播 TCP 里，这样会
> 比较好，所有状态迅速同步」。

`HOST_STATE` 新增字段（位置：`started=` 之后、`mode=` 之前）：

```
conn=<cid>:<state>,<cid>:<state>,...
```

精确规则（客户端照这个解析）：

* **一张全表**：房间里**每个成员各一条**，顺序与 `names=` / `peers=` 相同；
  某个人离开房间 -> 他**整条消失**（和 `names=` 一样，客户端据此知道
  "他不在房间里了"，而不是"他断线了"）。
* `<cid>` 是成员的 `client_id`：ASCII、不含 `:` 和 `,`。客户端按 `,` 切分，
  每条再按**第一个** `:` 切分。
* `<state>` **只有 4 个取值**（小写 ASCII，别的都不许出现）：

| `state` | 何时出现 | 含义 |
| --- | --- | --- |
| `idle` | 该成员此刻没有任何游戏连接 | 没连；**Host 停止开服也是 `idle`** |
| `waiting` | 他的 PLAYER socket 已接入中继、还在等待池里 | 点了开始连接、还没配上隧道 |
| `pairing` | 他的 PLAYER socket 已和某条 HOST_TUNNEL 配对 | 还没进世界（没收到 `CONNECTED`） |
| `connected` | 他**已经进入世界**（中继已发出 `CONNECTED`，字节在流）；**或者**他是当前 Host 且正在开服（`HOST <room>` 控制连接在） | 真正"连上了" |

* `conn=-` 只在"房间里一个成员都没有"时出现（防御性取值；正常情况下房间里
  至少有 Owner）。
* **`conn=` 是唯一权威**：客户端成员列表的「连接状态」列以它为准；老中继
  （没有这个 token）才回退到旧行为。
* 客户端映射建议（三态显示 / 布尔显示都说得通）：
  `connected` -> 「已连接」；`waiting` / `pairing` -> 「连接中」（也可细分
  「等待配对」/「打洞中」）；`idle` -> 「未连接」。
  **判定"是否已连接"用 `state == "connected"`**；判定"是否有连接在建立"
  用 `state != "idle"`。
* **立刻推送**：状态**每一次变化**都会触发一次新的 `HOST_STATE` 广播，不等
  心跳、不等轮询、不等下一次房间事件。触发点（服务端实现）：
  PLAYER 接入等待池、配对成功、发出 `CONNECTED`、PLAYER socket 在等待/配对
  阶段断开、会话拆除、被踢、掉线、Host 开服、Host 停服、成员进出房间。
  中继对"等待中的 PLAYER socket 断开"专门有一个 0.2s 的 EOF 看护
  （只 `at_eof()` 轮询，**不读** socket、不碰数据通路），所以它也是"立刻"的。
* 老客户端不认识这个 token -> 按空格分词直接忽略（additive 规则）。

### 8.6 `STARTED` —— 客户端显式上报「我点了开始连接」（ADDITIVE，2026-09-28）

> 用户口径：「已开始连接」= **用户在客户端点过「开始连接」**，而不是"已经
> 建立了 PLAYER / HOST 连接"。

成员控制连接上的新请求（老中继忽略未知行，新客户端照旧能用）：

```
STARTED <1|0>\n
    ◄── OK STARTED started=<1|0>\n
    ◄── ERROR BAD_STARTED 必须是 STARTED 0 或 STARTED 1\n
```

* `STARTED 1` = 我点了「开始连接」（`CONNECTING`）；`STARTED 0` = 我停了。
* 中继把它记在**成员**上，并**并进** `HOST_STATE` 的 `started=`：
  发过 `STARTED 1` 的成员一定在 `started=` 里，**即使他此刻没有任何 PLAYER
  连接**（STUN 模式就是"点了开始先打洞、游戏连接是之后的事"）。
* 并集，不是替换：连接现算（PLAYER / 开服控制连接）仍然有效。老客户端
  从不发这一行 -> 结果与以前**逐字相同**。
* 上报同一个值不会重复广播（只有真的变了才广播；`OK` 每次都回）。
* **不放宽任何门禁**：能不能连由"有没有 Host / Host 有没有开服"决定
  （`ERROR NO_HOST` / `ERROR HOST_NOT_READY`），与 `started=` 无关；
  `started=` 只决定"Host 该往谁那里打"（设计基准 §D/§E）。
* `member_list` 快照里对应键名是 `connecting`（布尔）。

### 8.7 管理快照 `member_list` 新增字段（ADDITIVE）

`GET /rooms` 的每个成员对象新增：

| 键 | 类型 | 含义 |
| --- | --- | --- |
| `conn` | `"idle"\|"waiting"\|"pairing"\|"connected"` | 与 `HOST_STATE` 的 `conn=` **同一个来源**（`room_members.member_conn_states`），面板和客户端界面不会各说各话。 |
| `connecting` | `true`/`false` | 该成员是否显式上报过 `STARTED 1`（§8.6）。 |

老面板不认这两个键，忽略即可。

### 8.8 开服控制连接：**同一个人重连 -> 立刻顶替**（行为微调，2026-09-28）

`ERROR ROOM_ALREADY_HAS_HOST` 的判定从"房间已经有开服连接"收紧为"房间已经有**别人**的开服连接"：

* 新来的 `HOST <room> <client_id> <username> [<token>]` 的 `client_id` 等于
  **当前主机位持有人**（`CLAIM_HOST` 那位成员）的 `client_id`，且
  （账号体系开着时）`token` 也相同 -> 旧的 `HOST` 控制连接被关闭，新连接直接
  `OK HOST room=<room>`，并在服务端日志记一行
  `[HOST SUPERSEDE] ... reason=same host position holder reconnecting`。
  用途：客户端"换模式 / 停止→重开"时先关旧 `HOST` 再立刻开新 `HOST`，服务端
  还没读完旧连接的 FIN 就会把新握手拒掉，客户端只能退避重试（默认 3s），这
  段时间里 `host_alive()` 为假、玩家全是 `HOST_NOT_READY` —— 就是"热切换后
  连不上、隧道没有重建"。
* **别人**（不同 `client_id`，或同 `client_id` 但 token 不符）**仍然**
  `ERROR ROOM_ALREADY_HAS_HOST`；一个房间仍然**最多一个**开服连接（§A 不变量
  不变）。主机位本身仍由 `CLAIM_HOST` / `RELEASE_HOST` 控制。
* 顶替时旧 handler 的收尾**不会**清状态、也**不会**断开新连接建起来的隧道
  （它先比较 `room.host_writer is writer`，不是自己的就直接收工）。

### 8.9 `mode_reason=` / `was_live=` —— 模式为什么是这个 + 切换前的真实状态（ADDITIVE，2026-09-29）

> 背景（用户 P0）：「STUN(直连) → relay → 切回 STUN 打通后，直连玩一会儿游戏
> 断开，并且**没有任何人操作，模式自己跳回了 relay**，连接也断了。」
> 结论：**房间模式只能由 Owner 的显式 `CONNMODE` 改变**；任何内部路径
> （清理 / 重试 / 看门狗 / 兜底默认值 / 迟到握手）都不得改写它。

`HOST_STATE` 在 `mode=` 之后新增两个 token（位置在所有已有字段之后，老客户端
按空格分词直接忽略）：

```
HOST_STATE ... mode=<relay|stun> mode_reason=<owner|restored|default> [was_live=<0|1>]
```

| `mode_reason` | 含义 |
| --- | --- |
| `owner` | **Owner 刚才显式 `CONNMODE`** —— 正常切模式 |
| `restored` | 这个房间是被销毁后由**同一个 Owner** 重建的；`mode=` 沿用他上一次显式选过的值（房间参数属于 Owner，一次闪断/重连不该把它换掉）。换了人建房是 `default`，**不继承**别人的选择 |
| `default` | 全新房间的构造默认值（老客户端不发模式时也是它） |

`mode_reason=restored` 是**唯一**允许在没有 `CONNMODE` 的情况下出现非默认
`mode=` 的场景，而且它只发生在"房间销毁 -> 重建"，并且如实广播了原因。

`was_live=<0|1>` 是**一次性**字段，**只在换模式那一轮广播里出现**（之后的
`HOST_STATE` 不再带它，不会拿过期值骗人）：

```
was_live=1  <=>  CONNMODE 生效**之前**，conn= 里存在 connected / pairing 的成员
was_live=0  <=>  切换前房间是空闲的
```

客户端口径（设计基准 §C）：`was_live=0` -> 原本空闲，**不自动重连**；
`was_live=1` -> 原本有连接，**自动重连**。注意同一轮广播里的 `conn=` 已经是
清理之后的 `idle`（换模式必须严格重建全部通路）——**"切换前有没有连接"看
`was_live=`，不要去看清理后的 `conn=`**。

同一时刻发给成员控制连接 / 等待池 socket 的原因行也带这个值：

```
CONN_MODE_CHANGED <原因> was_live=<0|1>\n              # 成员控制连接（无 ERROR 前缀）
ERROR CONN_MODE_CHANGED <原因> was_live=<0|1>\n        # 还没进世界的 PLAYER / HOST_TUNNEL
```

`was_live=` 一律是整行**最后一个** `key=value` token；老客户端把原因文本
整段显示（`ERROR` 只取错误码）也不会被它破坏。

### 8.10 `SESSION` —— 重连前先问一句「我的登录态 / 房间身份还在吗」（ADDITIVE，2026-09-29）

> 用户口径：「**连接和身份是两回事**」。仅仅连接抖动/断开、但房间身份仍在
> （token 仍有效）-> 不算离开房间，要**自动重连**；被清退 / token 失效 /
> 房间没了 -> 才算离开，需要重新登录 / 重新加入。
>
> `KICKED` / `ERROR AUTH_INVALID_TOKEN` / `ROOM REVOKED` 都是**被动推送**，
> socket 已经断了的时候客户端什么都收不到。所以补一条**主动、只读**的探测。

请求（哨兵房间 `__mclink__`，短连接，一行一答）：

```
SESSION __mclink__ <client_id> <username> <token>
```

应答：

```
OK SESSION valid=<0|1> in_room=<0|1> token=<account|guest|none>
           room=<room-token|-> name=<percent-encoded|->
           reason=<CODE> auto_reconnect=<0|1> relogin=<0|1>
```

| 字段 | 精确含义 |
| --- | --- |
| `valid` | 登录态/token 是否仍有效。`0` = 必须重新登录（`relogin=1`） |
| `in_room` | **房间身份**是否仍在（服务端此刻还有这个 `client_id` 的成员连接，或他的座位宽限期内） |
| `token` | `account`（登录 token）/ `guest`（中继发的游客会话 token）/ `none`（没带 token） |
| `room` / `name` | 认出来的房间 token / 房间名（`-` = 没有） |
| `reason` | 见下表 |
| `auto_reconnect` | `1` = 这是闪断，可以**自动重连**；`0` = 不许自动重连 |
| `relogin` | `1` = 必须先重新登录 |

`reason` 的完整取值表（客户端按这个分支）：

| `reason` | 何时 | `auto_reconnect` |
| --- | --- | --- |
| `STILL_IN_ROOM` | 服务端还认得这条成员连接（或座位宽限期内）——真·闪断 | `1` |
| `DROPPED` | 连接掉了，但身份/token 没问题（例：没有 token 的老连接） | `1` |
| `NOT_IN_ROOM` | 服务端没有任何记录（例：中继刚重启过） | `1`（回到重新 JOIN 那条路） |
| `KICKED` | 被踢出房间 | `0` |
| `LEFT` | 本人主动退房 | `0` |
| `TIMEOUT` | 服务端超时清退（真正离开） | `0` |
| `ROOM_CLOSED` | 房间已销毁（Owner 离开 / 管理员关闭） | `0` |
| `AUTH_*`（例 `AUTH_INVALID_TOKEN` / `AUTH_REQUIRED`） | token 已失效 / 需要登录 | `0`，且 `relogin=1` |

* **只读**：不建房间、不改任何状态、不碰数据通路，每次一条短连接然后关闭。
* 老中继不认识 `SESSION` -> 回 `ERROR UNKNOWN_ROLE`，客户端按旧行为处理
  （additive 规则）。
* 这条探测**不放宽任何门禁**，也不改变 `HOST_STATE` 的任何既有字段。

### 8.11 座位宽限期 —— 连接断了但身份还在（服务端行为，ADDITIVE，2026-09-29）

用户 2026-09-29：「连接和身份是两回事」；「被服务端真正清退 = 房间身份结束
（token 失效 / 被移除 / 超时清退）才算离开房间」。

服务端因此对**成员控制连接**的断开分两种情况处理：

| 情况 | 服务端行为 |
| --- | --- |
| 连接断了（TCP 掉线/被杀），但身份凭证仍有效：回传的 token 是一条**活着的游客会话**，或中继发给他的游客 token 还在，或账号 token 校验通过 | **座位保留**（含主机位 / Owner 身份）：他仍然在成员表里、仍然算 `host=`；只断**游戏连接**（`HOST_LEFT` / `HOST_NOT_READY` 那套可识别原因）。`conn=` 变 `idle`、`started=` 不再含他（连接状态），但 `host_id=` / `host_name=` 不变（身份/职位）。本人宽限期内重连 -> 座位**原样交回**，房间不销毁、连接模式不变 |
| 身份结束：主动 `LEAVE` / 被踢 / 看门狗超时清退 / token 失效（例：游客会话被 `LOGOUT`）/ 房间被销毁 / 根本没有 token | 走原来那条唯一清理路径：释放主机位、`[HOST RELEASE] reason=leave|drop|kick|timeout`、必要时销毁房间 |

* 宽限期由服务端环境变量 `MCLINK_MEMBER_SEAT_GRACE`（默认 45 秒）控制，
  到点还没回来就按 `TIMEOUT` 处理（等于"真正离开"）。
* **没有 token 的连接不享受宽限**：没有任何东西能证明"还是同一个人"，
  断线即离开 —— 与加这条之前**逐字一致**（老客户端行为不变）。
* 关键区分（也是客户端必须照做的）：
  `conn=` 是**连接状态**；`host_id=` / `host_name=` / 成员表是**身份/职位**。
  **绝不能用 `conn=idle` 推导"没有 Host"**；`ERROR HOST_NOT_READY`（已认领、
  未开服）与 `ERROR NO_HOST`（没人认领）必须严格区分，这一条现在尤其重要。


