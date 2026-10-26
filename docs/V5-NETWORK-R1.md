# v5.0.0 network-r1 修复与复测说明

## 固定产品边界

这是通用 TCP 端口联机工具。Minecraft 是应用之一，不能成为网络连接的启动条件。

- 开始连接后建立网络服务与隧道，不要求 Host 的目标端口已有应用监听，也不要求 Player 已打开游戏或接入本地代理。
- NAT、入站探测、候选交换、打洞、直连隧道握手不依赖应用 TCP 流。
- AUTO 的网络级中继验证、直连升级、备用保持及回退，同样不依赖应用连接。
- 应用稍后接入时才按流连接目标端口；目标应用没开只使那条应用流失败，不能否定或销毁网络隧道。
- 每条新应用流仍先准备中继，再使用独立逻辑流完成切换和重放。

## 本轮修复

1. 移除手动网络核心中的 AUTO 改动：`stun.py`、`punch.py`、`direct.py`、`room_direct.py`、`udptunnel.py`、`direct_carry.py`、`transport.py`、`channel.py`、`reliable.py`、`punch_flow.py` 均与 v3.9.20Full 字节一致。固定指纹见 `manual-core-baseline.json`，回归中检查。
2. AUTO 调度由 `auto_room_direct.py` 单独适配；严格 UDP 行为由 `auto_udp.py` 单独实现，手动模式不注入 strict 标志，也不替换其 UDP 实现。
3. 新增独立 `AUTO_NETWORK` 会话。开始连接后按 Host–Player 配对建立；复用原 AutoStream 路径探测、迁移、回退与认证恢复，但没有本地应用 reader/writer，也不会连接应用后端。
4. 网络会话使用单独服务端注册表，不占 Host 的应用等待隧道，不占应用流数量，不制造进入世界记录。Panel 的成员路径在没有应用流时读取网络隧道状态。
5. 修复逐玩家状态：保留手动直连的实际阶段、倒计时、候选和就绪信息；不再一律盖成“连接准备中”。网络隧道就绪明确标注；存在应用流时，以应用流的实际路径为准。
6. 窗口／日志显示固定修订号 `network-r1`，避免复制 EXE 后文件时间变化被误当作代码版本变化。
7. NAT 失败后增加只读 DNS／目的端口诊断日志。另提供逐目标网络报告命令；不改 DNS、代理、路由或原版 STUN 的探测顺序与分类算法。

## 日志结论与尚未证实的部分

用户提供的两份日志中：Host 取得 NAT 画像并发布候选；Player 持续没有公网端点，因此不能发布候选，Host 一直等待。两端日志表明后台在尝试，原版逐阶段显示被新增状态行遮盖。日志没有足够数据确认 Player 的 UDP 请求为什么没有应答。

本机另外复现了一个环境问题：中继域名解析为 `198.18.0.0/15` 虚拟地址，两个自建 STUN 请求消耗默认探测预算；多个公共 STUN 单独测试有应答。v3.9.20 的原始源码在本机当前环境同样返回 unknown。此结果**只说明本机当前环境**，不能替代用户两处公网的诊断，不能断言用户当时一定开启了代理。

随后用户确认其路由器启用 OpenClash Fake-IP，并搭配 UA3F。该信息使虚拟地址／UDP 转发成为需要优先核实的条件，仍不证明 Fake-IP 必然阻断 UDP，也没有证据把问题直接归因于 UA3F。

### OpenClash 对照复测

先将 `relay.example.invalid` 加入 Fake-IP 排除，并为它添加高优先级 DIRECT 路由，然后刷新相关 DNS 缓存、重启工具。保留其余配置。Fake-IP 排除和 DIRECT 是两个不同配置项；前者让客户端取得真实目的地址，后者决定转发出口。[Mihomo DNS 文档](https://wiki.metacubex.one/config/dns/)、[路由规则文档](https://wiki.metacubex.one/config/rules/)说明了这两个设置。

默认 blacklist 模式下，将以下条目合并到已有配置，不覆盖已有列表：

```yaml
dns:
  fake-ip-filter:
    - 'relay.example.invalid'
rules:
  - DOMAIN,relay.example.invalid,DIRECT
```

如果现有 fake-ip-filter-mode 是 rule，过滤列表应改加 `DOMAIN,relay.example.invalid,real-ip`，不能照搬 blacklist 条目。whitelist 模式的匹配含义与 blacklist 相反，应按现有模式配置。先以实际 DNS 结果和网络报告确认生效，而不是仅看规则已保存。

复测依次看：域名是否还返回 198.18/15、UDP 3478/3479 是否收到应答、默认 NAT 测量是否得到公网端点、随后候选交换／打洞能否完成。如果 NAT 已正常但 P2P 仍失败，再检查两台测试设备对彼此公网端点的 UDP 路由；仅给 STUN 域名配置 DIRECT 并不自动覆盖任意端口的玩家 P2P 流量。

原始 EXE 与修复前 v5 EXE 内嵌的 STUN／NAT／DirectLink 字节码也做过对照：三个模块的执行代码相同。没有据此认定用户公网故障已解决。

可在测不出 NAT 的电脑执行：

```powershell
.\CraftLink-console.exe --network-report network-report.json
```

报告包含实际 DNS 结果、每台 STUN 的独立 UDP 结果，以及原版默认流程的真实分类结果。独立探测有应答不冒充默认流程已经测出 NAT。

## 复测部署范围

**这次需要同步更新服务端和两端客户端。** 客户端、服务端版本仍为 5.0.0；客户端修订号为 network-r1。旧服务端不实现 AUTO_NETWORK，不能仅替换 EXE 后验收独立网络隧道。

从本目录当前服务端源码重新构建并按原部署流程更新；没有修改既有账号、数据库、端口映射配置或远程部署状态。

先不运行任何游戏，在两端开始连接，观察网络隧道和逐阶段状态。确认网络就绪后，再接入任意 TCP 测试服务或游戏；确认一个应用未监听时不会使其它隧道断开。跨公网重复升级／回退实测仍为外部验收项。

## 验证记录

新增完整启动测试使用真实 RoomSession、Engine、Relay、STUN UDP 应答器、NAT 测量、入站探针、候选交换、打洞与 UDP 隧道握手。只把测试 STUN 目的地址配置为本地应答器，没有伪造打洞结果或注入已连接的 PeerLink。

覆盖：无应用监听下建立 AUTO 网络隧道、升级与回退；直连健康时备用中继恢复；全部 STUN 黑洞下中继先就绪；手动直连完整启动；AUTO → 手动直连 → 中继热切换；身份和方向拒绝；应用后启动后再转发任意二进制 TCP 数据。最终统一结果见 V5-VALIDATION.md。
