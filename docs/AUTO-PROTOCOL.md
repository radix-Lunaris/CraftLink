# CraftLink 5.0.0 AUTO 协议与实现

## network-r2：逻辑流与共享承载的关闭权限

新流收到中继上的有效端到端 ACK 后才能接入直连承载；ACK 前本地读取受零窗口限制，应用流若被取消则直接清理。DirectPath.close 只移除当前逻辑流的路由，不关闭共享 AutoPeer。应用流的 PONG／ACK 超时回落仅影响该流；已验证的独立网络会话健康超时、实际 UDP 写失败／外层帧解析失败才能废弃共享承载。合法外层帧内的单流序号／窗口错误 RESET 该流。既有身份、会话代次、帧格式及手动传输分支保持不变。见 V5-NETWORK-R2.md。

## network-r1：网络隧道独立于应用

这是通用 TCP 端口联机服务。Minecraft 或其它应用不需要先启动、监听或接入代理。手动模式核心遵循 v3.9.20Full；AUTO 的额外行为放在独立适配模块。

点开始连接后，AUTO 为每个 Host–Player 创建独立网络会话：

```text
AUTO_NETWORK <room> <cid> <name> <identity-token> peer=<other-cid> side=<host|player> caps=auto_stream_v1 ver=5.0.0
CONNECTED_AUTO_NETWORK <sid> proto=auto_stream_v1 host=<cid> player=<cid> generation=<host-generation> resume=<side-secret> player_ip=<observed> host_ip=<observed> channel=network
```

网络会话复用 AUTO 探测、迁移、备用恢复和生命周期；没有应用 socket，不产生 DATA。恢复仍使用 AUTO_RESUME。服务端单独保存 network_tunnels，不占应用等待隧道和应用流名额，不产生游戏进入／退出记录，目标端口无人监听也不妨碍网络会话建立。

应用稍后连接本地代理时，按以下原有数据流握手逐流附着。状态中的 channel=network/data 区分网络隧道与应用流；无应用时显示实测网络路径，有应用时显示应用实际路径。游戏进程退出不终止已开始的网络服务。

## 房间和游戏流

房间策略为 `relay | stun | auto`，只有 Owner 可以修改。开始连接操作仍独立于建房、入房。AUTO 内迁移不触发 CONNMODE、stop/start 或全房重建；每条 Minecraft TCP 对应持续存在的 AutoStream，精确绑定 Host、Player 和服务端签发的 128 位 session ID。

每条新流先准备中继承载，随后才允许后台直连升级。一个 Host–Player 直连承载可复用给该玩家的多个流，按 session ID 分流；不会认领其他玩家的链路，也不会为切换重建 Minecraft 后端。

手动 Relay 仍搬运原始游戏 TCP 字节；手动 STUN 仍使用已交付的 PeerLink/DirectCarrier，仅直连搬游戏数据。AUTO 独立使用 AutoPeer 和 AutoStream。

## 能力和握手

CREATE/JOIN/HOST/PLAYER 的已有位置参数后新增 `caps=auto_stream_v1`；AUTO HOST_TUNNEL 也必须携带成员身份与 token。`ver=5.0.0` 保持版本门禁。

房间已有无 AUTO 能力成员时，Owner 切换请求返回 `ERROR AUTO_UNSUPPORTED members=<cid,...>`；旧客户端加入 AUTO 房间直接拒绝。版本旁路不能绕过能力检查。

匿名 v5 成员在成员握手中取得服务端签发的 `guest=` 凭证；有昵称不等于已经具有凭证。登录账号并通过 IDENTITY 验证后，同一成员的后续 AUTO 附件改用新凭证，游戏流和本地 socket 保留。

AUTO 数据配对返回：

```text
START_AUTO <sid> proto=auto_stream_v1 host=<cid> player=<cid> generation=<host-generation> resume=<host-secret> player_ip=<observed> host_ip=<observed>
CONNECTED_AUTO <sid> proto=auto_stream_v1 host=<cid> player=<cid> generation=<host-generation> resume=<player-secret> player_ip=<observed> host_ip=<observed>
```

Host 仍在连接本地 Minecraft 后发送 READY。START_AUTO/CONNECTED_AUTO 是数据流上最后的文本握手；此后只允许完整 AUTO 帧。

备用重接：

```text
AUTO_RESUME <room> <cid> <name> <identity-token> sid=<sid> side=<host|player> generation=<generation> resume=<side-secret> caps=auto_stream_v1 ver=5.0.0
OK AUTO_RESUME
```

恢复必须同时匹配成员身份、对应端点的秘密、会话 ID 和当前 Host 开服代次。凭证不写入状态快照或日志。恢复拒绝是终止信号；新 Host 不能接收旧流数据。

## 帧格式

网络字节序，固定头 44 字节；Python struct 格式 `!4sBBH16sQQI`：

| 字段 | 字节 | 含义 |
|---|---:|---|
| magic | 4 | MCA5 |
| version | 1 | 1 |
| type | 1 | 下表类型编号 |
| flags | 2 | ACK 的 bit 0 表示 FIN 已交付 |
| session | 16 | 128 位会话 ID |
| offset | 8 | 数据偏移、累计确认或 FIN 的最终偏移 |
| value | 8 | 窗口右边界、探测 nonce 或切换代号 |
| length | 4 | payload 长度，最多 65536 字节 |

类型编号：DATA=1、ACK/WINDOW=2、PING=3、PONG=4、PREPARE=5、READY=6、COMMIT=7、FALLBACK=8、FIN=9、RESET=10。常规 DATA 最大 16384 字节。

两个方向各自维护从零开始的字节偏移。发送缓存保留所有未确认字节；重放保持原有帧边界。接收端按 offset 去重和重排，可靠保留连续数据后才能推进 ACK。窗口值是接收端已向本地 TCP 交付的位置加预留容量形成的绝对右边界。传输层 ACK 不能代替此 ACK。

DATA 只向原 Minecraft writer 按序写入一次。FIN 带最终偏移，先交付所有前序数据才半关闭；两方向完成后结束流。RESET 立即终止逻辑流。接收解析器保留半帧，超时不能从下一字节重新猜头。

## 路径迁移和故障

直连通过真实帧承载完成双向有效负载回声验证；默认至少 3 次探测且稳定观察 1 秒。Host 发起 PREPARE，Player 验证自身条件后 READY，Host COMMIT。递增代号防止迟到控制重新激活已取消的升级。

两路始终可接收。直连失效时，任意一端立即切回中继并重放未确认范围，不等待故障路径排空。缓存、序号和 Minecraft socket 都保留。直连路径销毁不会销毁其逻辑游戏流。

AUTO UDP 使用独立 AutoUdpTunnel 适配：队列满不推进确认；发送窗口超时明确失败；完整帧单 writer 串行发送。控制帧优先于 DATA，同一字节承载中发生部分帧失败时废弃该承载。原版 udptunnel.py 保持字节一致，手动 STUN 不受该适配影响。

备用中继 TCP 单独断开时，健康直连继续。服务端保留逻辑配对，两端重新附着；旧附件的迟到清理只能处理旧对象。逻辑流结束、被踢、关房、Host 更换和身份被撤销优先于恢复。成员控制连接仍沿用原有断线和超时语义。

## 默认资源与计时

| 配置 | 默认 |
|---|---|
| 每方向发送缓存上限 | 4 MiB |
| 初始接收预留窗口 | 256 KiB，不超过方案的 4 MiB 上限 |
| 进程 AUTO 数据缓存预算 | 128 MiB，满时背压 |
| 直连探测 / 中继备用探测 | 1 秒 / 2 秒 |
| 直连黑洞 | max(2 秒, 4×实测 RTT)，上限 5 秒；同时看 ACK 进展 |
| 中继无响应 | 6 秒 |
| 直连重试 | 5、15、30、60 秒，带抖动 |
| 60 秒内回退 3 次 | 暂停升级 120 秒 |
| 备用重接退避 | 1、2、4、8、10 秒 |
| 双路均不可用保留期 | 15 秒，Minecraft 可能先超时 |
| 服务端分离附件保留期 | 45 秒；有效成员状态上报刷新存活证据 |
| AUTO 游戏流准入 | 每成员最多 16 流、每房间最多 256 流；人数仍按成员计算 |

参数集中在 auto_stream.Config 和 server/auto_relay.py。收发满队列使用背压，不能丢已经从 TCP 读取的字节。

## 状态和统计

客户端经成员控制连接发送 `AUTO_STATE <json>`，变化时和每 2 秒上报。只供展示，不能授权成员、改变策略或创建会话。服务端校验上报归属与 Host 代次。

状态包括 room_mode、active_path、relay_ready、direct_ready、switch_phase、last_reason、updated_at、IP、实际直连端点和两类实测 RTT。live active_path 为 relay/direct/reconnecting；逐人多流聚合可为 mixed；两端不一致为 transitioning；过期为 unknown；结束为 none。

Panel 只在双方上报均不超过 10 秒时认定状态新鲜。没有状态、过期状态和候选地址不能冒充实际业务直连。服务端观察到的成员 IP 与选中的直连端点分开标明，IPv6 端点使用方括号。

服务端统计实际经过中继的字节，包含 AUTO 协议开销和备用探测；直连账本独立记录。保留备用连接不表示游戏业务正在中继。迁移保持原 session，不增加虚假的进入／退出或在线人数。
