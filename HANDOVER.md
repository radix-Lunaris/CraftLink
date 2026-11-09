# CraftLink-V3 交接文档（新会话从这里 + `AGENTS.md` 直接开工）

> 写于 **2026-09-29 00:15**（v3.5.1 已交付、6 条打洞隐患**正在由另一个 agent 修**的时点）。
> 本文只记录**仓库里读得到的事实**与文件位置；规则以 `AGENTS.md`（硬规则 1–6）和
> `V3-AGENTS.md`（完整版）为准，本文与它们冲突时以它们为准。
> ⚠️ 本仓库**没有 git**（`git status` → `fatal: not a git repository`）。历史与"改没改"只能靠
> 文件内容 + 文档 + 测试判断，**不要指望 `git diff`**。

---

## 1. 一句话定位 + 当前版本

**CraftLink（中文名「方块联机」）** 是 Minecraft 联机的中转/直连客户端 + 中继 + PHP 面板：
中继永远只做纯字节转发，v3 的主题是 **STUN 打洞直连、打不通无感回落中继**。
客户端 tkinter + asyncio（`client/mclink/`），中继 Python（`server/relay_server.py`，以 `__main__` 跑），
面板 PHP（`panel/`）。

| 项 | 值 / 规则 |
|---|---|
| `client/mclink/config/settings.py` 的 `APP_VERSION` | **`3.5.1`**（版本号唯一来源） |
| `deploy/server/VERSION` | **`3.5.1`**，**必须与 `APP_VERSION` 完全一致**（由 `scripts/sync-deploy.py` 生成，**禁止手改**） |
| 客户端 exe 标题栏 | `CraftLink v3.5.1 (构建 YYYY-MM-DD HH:MM)` —— 版本 + 构建时间是用户判断"我拿的是不是这一版"的唯一依据（`settings.version_line()` / `build_stamp()` 取 exe 自己的 mtime；`gui/app.py:_build_window` 设 `root.title(...)`） |
| 当前 exe | `deploy/CraftLink.exe` / `deploy/CraftLink-console.exe`，构建 **09-28 23:45** |
| 规则 1.5 | **默认"改了就升版本号"**；一次交付一个号；升版本只能走 `python3 scripts/bump-version.py <x.y.z>` |

> 版本门机制：中继启动读同目录 `VERSION`（env `MCLINK_VERSION`）→ 客户端五个受管握手角色
> `CREATE/JOIN/HOST/HOST_TUNNEL/PLAYER` **每一条都要在整行最后一个 token 带 `ver=`**；
> 不一致回 `ERROR VERSION_MISMATCH`，客户端弹「需要更新」并**拒绝运行**（不重连、不降级）。
> 详见 `AGENTS.md` 规则 1、`PROTOCOL.md`、`tests/version_gate_test.py`。

---

## 2. 交付流程（照做即可）

```
deploy/ 整包上传到服务器 /opt/mc-link/  →  跑 mcr（= deploy/rebuild.sh）
exe 不上传服务器；客户端 exe 由用户拷到 Windows 运行
服务端与客户端必须【同一次发版】；只发一边 = 客户端被自己的服务器拒掉
```

* **服务器侧**：把 `deploy/` 的**全部内容**（含 `server/VERSION`）覆盖到 `/opt/mc-link/`，然后
  `mcr`；`mcr` 就是 `bash /opt/mc-link/rebuild.sh`。`rebuild.sh` 可选参数：
  `panel`（只重建面板，不断玩家）/ `relay`（只重建中继，会断玩家）/ `--strict`（指纹不一致拒绝重建）/
  `-v`（打印全部构建日志）。等价的原始命令（START-HERE §3）：
  `sed -i 's/\r$//' /opt/mc-link/*.sh && bash /opt/mc-link/rebuild.sh`
  （`rebuild.sh` 自己也会先洗 CRLF；构建步骤默认安静，完整输出在 `/opt/mc-link/rebuild.log`）。
  `rebuild.sh` 会校验：凭据非占位符、`server/VERSION` 非空且与镜像同源、`Dockerfile` 有 `COPY VERSION`、
  UDP 19100 映射存在、容器内模块 `import` 全通过、容器内文件与上传文件 md5 一致。
* **客户端侧**：`deploy/CraftLink.exe` 拷到 Windows 运行即可；**exe 不进服务器**。

### 改完东西的标准发版顺序（规则 1.5）

```bash
python3 scripts/bump-version.py 3.6.0          # 1) 只改 settings.py + 生成 deploy/server/VERSION
cd /tmp && cmd.exe /c "C:\Windows\Temp\clb4.bat"   # 2) Windows 侧重打包 exe（不要在 WSL 里出 exe）
cp client/dist/CraftLink.exe client/dist/CraftLink-console.exe deploy/   # 3) 回仓库根执行
python3 scripts/sync-deploy.py                 # 4) 再 sync 一次（写 VERSION/BUILD.txt/README）
python3 scripts/check_deploy.py                # 5) 门禁一
XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/run_all.py   # 6) 门禁二
```

* 打包入口是 `cmd.exe /c "C:\Windows\Temp\clb4.bat"`（spec：`scripts/mclink-client.spec`；
  另有 `scripts/build-exe.bat` / `build-win-exe.bat`）。
* `deploy/README.txt`、`deploy/BUILD.txt` 由 `sync-deploy.py` **自动生成，禁止手改**。

---

## 3. 两道门禁（精确命令 + 期望）

### 门禁一：部署包预检

```bash
python3 scripts/check_deploy.py
# 期望末行：RESULT: 71 passed, 0 failed
```

* 当前 **71 项**（脚本末尾动态打印，以 `RESULT` 行为准）。它检查：compose 凭据非占位符、
  Dockerfile `COPY` 完整、`deploy/` 与源码逐字节一致/无漂移、`BUILD.txt` 覆盖并算对全部指纹、
  `rebuild.sh` 版本标记与 `run_step` 结构、**客户端版本 == `deploy/server/VERSION`**、
  五个受管握手角色所在文件都带 `ver=`、**exe 比最新客户端源码新**。
* **唯一允许在"改到一半"时出现的红**：`CraftLink.exe 比源码旧` 和 `客户端与 deploy 漂移`
  （刚改完源码、还没重打包/没 sync）。跑完 Windows 重打包 + `sync-deploy.py` 后必须**全绿**；
  除此之外任何红都是真问题，不许绕过。

### 门禁二：全量回归

```bash
# GUI 测试前（每个新 shell 都要做，/tmp 是每次调用重建的）：
mkdir -p /tmp/.X11-unix && ln -sf /mnt/wslg/.X11-unix/X0 /tmp/.X11-unix/X0

XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/run_all.py
# 期望：48 个套件全部 PASS（末尾 "全部通过 —— 核心链路可以正常使用"）
```

* 当前 `tests/run_all.py` 的 `SUITES` 有 **48 套**（见 §5.6 清单；`../scripts/check_deploy.py`
  本身是第 46 套）。**套件数会随新回归网增加**，以 `run_all.py` 的 `SUITES` 为准。
* **时序敏感套件**：`direct_e2e_test` / `direct_reuse_test` / `direct_multi_test` /
  `direct_persist_test` / `conn_mode_hotswitch_test` / `mode_switch_rebuild_test`。
  全量里偶尔红时，**单独复跑**核实；不要急着改代码。
* **跑全量前先确认没有别的任务在跑同一批固定端口**（另一个 agent、上一次没退干净的测试进程）。
  症状：`Errno 98 / address already in use`、`ROOM_FULL`、莫名其妙的假红。

---

## 4. 开发环境的坑（WSL2）

1. **必须设 `XDG_CONFIG_HOME=$PWD/.testcfg`**：不设客户端会往只读的 `~/.config/MC-Link/` 写日志
   （一堆 `Read-only file system` traceback，会遮住真错误）。
2. **GUI 测试需要 X socket**：`mkdir -p /tmp/.X11-unix && ln -sf /mnt/wslg/.X11-unix/X0 /tmp/.X11-unix/X0`
   （沙箱把 `/tmp` 换成 tmpfs；不修 → `TclError: couldn't connect to display ":0"`，4 套 GUI 测试全红，代码没问题）。
3. **exe 只能在 Windows 侧打包**（规则 3，`cmd.exe /c "C:\Windows\Temp\clb4.bat"`），
   **不要试图在 WSL 里出 Windows exe**。受限沙箱里单文件 exe 跑 `--selfcheck` 会失败
   （`Failed to extract VCRUNTIME140.dll`），那是环境限制不是 exe 坏了。
4. **不要碰**：`reference/` 与 v2 归档（只读）；用户的 VPS（无 SSH）。所有验证都在本机做。
   `.bat` 不要用 read 工具读（GBK），用 `Get-Content`。
5. **网络能力**：沙箱网络开放（能连 VPS TCP 19100 / UDP 3478-3479）、≥1024 端口随便绑、loopback 真 TCP/UDP。
   挡住的：<1024 端口、`SOCK_RAW`、`/home/ai_workspace` 之外的写。
   **本机只有一个 NAT、没有第二个网络** → "真实两端打洞"只能靠用户在真实网络实测。
6. **不要并发跑同一批测试**（固定端口）；失败先看是不是端口占用。

---

## 5. 项目结构导航

### 5.1 `client/mclink/core/*`（逐文件一句话；行数为 2026-09-29 00:16 快照，正在被改的文件会漂）

| 文件 | 行数 | 一句话 |
|---|---|---|
| `engine.py` | 1736 | 客户端网络核心 `MCLinkEngine`：一条 asyncio 事件循环跑在守护线程，GUI 通过它提交/取事件；`start_session/stop_session/watch_room_nat/watch_room_direct/room_direct_path` 是打洞相关入口。 |
| `session.py` | 1530 | 会话层：GUI 面向的对象，持有 `HostCore`/`PlayerCore`，把原始回调翻译成 UI 事件（`SessionBase`/`HostSession`/`PlayerSession` + `RoomNatMonitor`）。 |
| `direct.py` | 1813 | 直连协调器：一次「交换候选 → 喷洒 → 命中」的打洞 `DirectLink`（`measure/probe_inbound/advertise/attempt/start_keepalive/close`）；**打洞 socket 归它**（命中记录为 `punched_sock`）。 |
| `direct_carry.py` | 1255 | 把打通的洞变成能搬字节的通道：`PeerLink`（用 `punched_sock`，`carried=True` 后停房间保活）、`DirectCarrier`（把一条 MC 连接挂到 PeerLink）、`Spare`。 |
| `room_direct.py` | 2571 | **房间级打洞协调器** `RoomDirectLink`：socket / `_run()` 循环 / 每 peer 的 `ConnSession` 字典都在这；`_punch/_open_tunnel/_sweep_links/_drop_peer/flow_summary/link_rows/peer_ready`。 |
| `punch_flow.py` | 1841 | 打洞**纯逻辑状态机**（不碰 socket/asyncio/界面）：`Stage/TRANSITIONS/ConnSession/ConnTimer`、`select_targets/host_id_of/host_ready/parse_host_state_tokens/summarize/beacon_line`。规则 6 §D/§E/§F/§G 的落点。 |
| `punch.py` | 2345 | UDP 打洞引擎（移植 MCLanP2P）：策略矩阵、端口窗口、同步起跳、learn-then-reply。 |
| `udptunnel.py` | 794 | 可靠 UDP 隧道（**逐字移植**参考工程）：`UdpTunnel`/`UdpHub`（同 socket 唯一读者），`KEEPALIVE_S=5.0`。 |
| `channel.py` | 167 | `PeerChannel`/`Stream`：一条洞上按 `sid` 多路复用，并各自连本地 Minecraft。 |
| `stun.py` | 1665 | STUN 探测与 NAT 判定（移植版）：`stun_probe/stun_filtering/stun_order/refine_nat_subtype/NatProfile`；两条铁律见 `V3-AGENTS.md` §2.1。 |
| `protocol.py` | 637 | **冻结**的线上协议字符串：`ROLE_*`、错误码、解析/拼装。 |
| `host_core.py` | 1338 | **冻结**的房主 TCP 机制 `HostCore`：`HOST`/`HOST_TUNNEL` 握手 + `relay_mc` 字节转发 + 多隧道。 |
| `player_core.py` | 904 | **冻结**的玩家 TCP 机制 `PlayerCore`：`PLAYER` 握手 + `raw_relay`。 |
| `traffic.py` | 202 | 进程级流量账本 `TrafficLedger`（直连也计数；离房不清零；速度永远有）。 |
| `presence.py` | 169 | presence 频道 `PresenceKeeper`。 |
| `transport.py` | 376 | 传输路径抽象：`Path/UdpPath/RelayPath/MemoryPath`。 |
| `common.py` | 206 | 底层公共工具（`close_writer` 等，来自归档客户端）。 |
| `control_client.py` | 681 | V2 控制面客户端（登录/房间列表/删房/改名），每次一条短连接。 |
| `__init__.py` | 1 | 包声明；core **必须与 GUI 无关**。 |

### 5.2 `client/mclink/gui/*`

| 文件 | 行数 | 一句话 |
|---|---|---|
| `app.py` | 10421 | 主界面（tkinter **shell**）：读表单 → `ClientConfig` → `MCLinkEngine` → 渲染事件；`on_start/on_stop`、热切换重建、自动重连、三层 UI、`_report_started`、`_sync_room_nat` 全在这。 |
| `auth_ui.py` | 1402 | 登录对话框 + 实时房间列表（网络调用全走 `AsyncTask` 工作线程，不碰 GUI 线程）。 |
| `i18n.py` | 235 | 语言框架（中/英/日）+ `tr()`；翻不到显示原文。 |
| `i18n_tables.py` | 1856 | 翻译表（只放内容不放逻辑）。 |
| `theme.py` | 164 | 主题配色（暗 / 亮 / 跟随系统）；彩色主题已被用户否决。 |

### 5.3 `client/mclink/config/*`

* `settings.py`（413）：`APP_VERSION`、`UPDATE_URL`、`build_stamp()`、`version_line()`、`config_dir()`（目录名固定 `MC-Link`，**永远不要改**）。
* `credentials.py`（386）：记住密码的加密存储。

### 5.4 `server/*`（中继）

| 文件 | 一句话 |
|---|---|
| `relay_server.py` | 中继主程序（**以 `__main__` 跑**）：握手、配对、纯字节转发、版本门、门禁、Host 顶替、watchdog、管理快照。 |
| `room_members.py` | 房间成员与主机位：`HOST_STATE` 广播（含 `host_id=/started=/conn=`）、`claim_host/release_host`、[HOST RELEASE]。 |
| `room_registry.py` | **房间注册表（唯一持有 live rooms 的对象）**；共享状态必须放独立模块（见 `V3-AGENTS.md` §5）。 |
| `relay_config.py` | 配置 + `RELAY_VERSION`（env → `VERSION` 文件）。 |
| `stun_server.py` | 内置 STUN 应答器（双端口 3478/3479，支持 CHANGE-REQUEST）。 |
| `gatekeeper.py` | 账号 / token / 登录限流。 |
| `auth.py` / `db_store.py` / `db_schema.py` / `settings_service.py` | 密码哈希 / MySQL 存取 / 建表 / 运行时设置。 |
| `admin_api.py` | 管理 HTTP API（只在内网容器里）。 |
| `healthcheck.py` | 容器健康检查（stdlib）。 |
| `relay_log.py` / `relay_events.py` | 服务端日志落盘 + 事件格式。 |
| `room_names.py` | 房间名 ⇄ 房间 token。 |
| `reset_password.py` | root 账号找回。 |
| `Dockerfile` / `docker-compose.yml` / `requirements.txt` / `sql/mclink-v2-schema.sql` | 部署与库结构。 |

### 5.5 `panel/*`、`scripts/*`

* **panel（PHP 面板，已满意尽量别动）**：入口 `panel/web/index.php`；视图 `panel/web/views/{dashboard,rooms,users,events,account,prefs,layout_*}.php`；
  异步刷新 `panel/web/live.php`（只改 `data-live` 节点，**绝不整页 reload**）；公共库 `panel/lib/common.php`、`panel/lib/i18n.php`；部署 `Dockerfile/nginx.conf/php-fpm-pool.conf/supervisord.conf/docker-compose.yml/config.sample.php`。
  ⚠️ `deploy/` 里的 compose 带真实凭据，仓库里的那份是占位符。
* **scripts/**：
  `bump-version.py`（升版本，唯一入口）、`sync-deploy.py`（生成 deploy 包 + `server/VERSION` + `BUILD.txt` + `README.txt`，保留真实凭据）、
  `check_deploy.py`（门禁一）、`mclink-client.spec`（PyInstaller）、`build-exe.bat`/`build-win-exe.bat`（Windows 打包）、
  `take_screenshots.py`/`ui_probe.py`/`_measure_width.py`/`make_assets.py`（界面取证/资源）、
  `verify-stun-ports.py`/`tls_probe.ps1`/`panel_probe.ps1`/`exe_window_probe.py`/`frozen_selfcheck.py`、`fix_line_endings.py`、`deploy-now.py`、`fix.sh`、`_drop_identity_card.py`、`_host_name.py`、`run-tests.bat`、`启动客户端.bat`。
  `deploy/` 顶层还有服务器运维脚本：`rebuild.sh`（=mcr）、`check-db.sh`、`diag-fleet.sh`、`diag-panel.sh`、`diag-relay.sh`、`fix-panel.sh`。

### 5.6 `tests/*` —— `run_all.py` 现有 48 套（file — 名称）

| # | 文件 | 名称 | # | 文件 | 名称 |
|---|---|---|---|---|---|
| 1 | config_contract_test.py | 配置契约测试 | 25 | conn_mode_hotswitch_test.py | 连接模式热切换（清完必须还能重建） |
| 2 | units_test.py | 格式化 / 测速单元测试 | 26 | member_conn_state_test.py | 每成员连接状态立刻同步（conn= / STARTED） |
| 3 | e2e_relay_test.py | 中继数据通路（冻结的 RAW TCP 转发） | 27 | panel_static_test.py | PHP 面板静态检查 |
| 4 | engine_session_test.py | 引擎与会话层 | 28 | stun_stack_test.py | STUN 栈（移植版） |
| 5 | latency_test.py | 延迟与实时速度 | 29 | punch_test.py | 打洞引擎（直连） |
| 6 | gui_smoke_test.py | 界面冒烟测试 | 30 | direct_cand_test.py | 直连协调控制面 |
| 7 | gui_appearance_test.py | 界面外观（语言/主题） | 31 | direct_e2e_test.py | 直连端到端（游戏走直连） |
| 8 | gui_i18n_test.py | 语言覆盖硬测试 | 32 | direct_reuse_test.py | 直连通道复用（连两次都走直连） |
| 9 | room_name_host_test.py | 房间名 / 主机位置 | 33 | direct_multi_test.py | 多人直连（host×每玩家一条长连接） |
| 10 | gui_room_ghost_test.py | 幽灵房 / 按钮状态 | 34 | direct_persist_test.py | 打通后保持（不许下一轮 restart()） |
| 11 | v2_control_test.py | V2 服务端控制面 | 35 | udptunnel_test.py | 直连承载（udptunnel 移植版） |
| 12 | session_lifecycle_test.py | 会话生命周期（幽灵会话） | 36 | punch_traffic_test.py | 直连承载 / 打洞快路 / 流量账本 |
| 13 | db_boot_timeout_test.py | 数据库卡死也不能拖住启动 | 37 | gui_room_host_session_test.py | 免登录 / 主机身份 / 会话模式自动重建（界面侧） |
| 14 | room_recovery_test.py | 人数锁定 / 掉线复位 / 身份 | 38 | room_identity_test.py | 房间里改名 / 身份绑设备（IDENTITY） |
| 15 | pairing_stale_tunnel_test.py | 死隧道不许喂给玩家 | 39 | auto_reconnect_test.py | 自动重连 / 房主离线 / 单人文案 |
| 16 | server_logs_test.py | 服务端日志 / 导出 | 40 | version_gate_test.py | 版本门（客户端与服务端同版本） |
| 17 | v2_client_test.py | V2 客户端控制面 | 41 | reconnect_after_teardown_test.py | 断开之后必须还能重连（端口必须真的释放） |
| 18 | room_settings_test.py | 房间人数上限 / 关房 | 42 | refresh_count_test.py | 刷新计数（列表刷新不许把人数刷上去） |
| 19 | kick_test.py | 踢人（踢出单个成员） | 43 | reset_password_test.py | 账号找回工具 |
| 20 | client_kick_roster_test.py | 房间成员列表 / 踢人（客户端） | 44 | credentials_test.py | 记住密码 |
| 21 | member_name_rtt_test.py | 成员真名 / 每成员中转延迟 / 对端延迟 | 45 | nat_diag_test.py | 网络诊断（只读） |
| 22 | member_identity_rtt_test.py | 成员身份 / RTT / 版本门开发开关（服务端） | 46 | ../scripts/check_deploy.py | 部署包预检 |
| 23 | host_release_identity_test.py | 主机位释放 / 同源 host 字段 / IDENTITY（服务端） | 47 | punch_flow_test.py | 打洞流程基准（§C/§D/§E/§F/§G） |
| 24 | relay_baseline_test.py | 中继永久基准（Owner/Host/门禁/started/换模式） | 48 | mode_switch_rebuild_test.py | 热切换重建 / 状态迁移 / 逐目标行文案 |

> `tests/` 目录里还有**不在 run_all 里**的取证/实验脚本：`manual_stun_live_check.py`、
> `port_prediction_experiment.py`、`punch_crossnet.py`、`udp_mapping_experiment.py`
> （手动/实验用，不是门禁）。

### 5.7 `docs/*` 索引见 §11。

---

## 6. 核心设计（动代码前必须读）—— 规则 6 浓缩成可执行清单

> 完整版 `docs/DESIGN-BASELINE-20260927.md`；硬规则原文 `AGENTS.md` 规则 6。
> **这些多数是早期就有、用户已验收的**：任何"顺手重构/新功能"改掉它们 = **回归 bug**，改前必须问用户。

**身份与房间**
1. **Owner = 创建房间的账号，永久绑定、不可转移 / 不可继承**。Owner 离开（主动，或被判定为真离开）→ **房间立即销毁**：所有人断开、Host 释放、连接与状态清理。绝不允许"别人继承 Owner"或"Owner 走了房间还在"。
2. **一个房间最多一个 Host**（可以暂时没有 Host）；Host 只是**当前房间角色**，不改变账号本身。
3. **房间参数（人数上限、连接模式、Host 参数）创建后冻结**；加入房间**以 Server 的房间状态为准**，本地旧参数不得覆盖。
4. **只有 Owner 能切连接模式**；Host（非 Owner）无权（错误码 `ERROR NOT_HOST`）。

**Host 位的保留 / 清除（§H）**
5. Host 可**自由主动停止连接再重新开始**（停 = 未开服）；**"静默/停止"不清除主机位**，房间列表显示 `xxx（未开服）`。
6. **只有两种情况清除主机位**：① Host 离开房间（主动离开 / 被踢 / 掉线判定为真离开）；② Host 主动释放。

**连接门禁（不许放宽）**
7. **无 Host ⇒ 任何玩家不能开始连接**（`ERROR NO_HOST`）；**Host 已认领但未开服 ⇒ 只能看到「Host 未连接，请等待 Host 开服」**（`ERROR HOST_NOT_READY`），**不得进正式流程**；Host 已开服后 Player 才允许连接。

**打洞**
8. **每次真正 Punch 前重拉事实**：Player 每轮**重新拉当前 Host** 作为唯一目标（只认 `host_id=`，与谁建房无关）；Host 每轮**重新拉当前全部已开始连接的 Player**（`started ∩ peers`）。**不得复用上一轮的 Host / 目标 / candidate**。
9. **每个 Host↔Player 是一条完全独立的 Connection Session**：session_id / attempt_id / candidate / NAT / 入站 / 同步 / punch 次数 / batch / 序号 / 端口 / 频率 / timer / connected / latency **全独立，禁止互相复用**。
10. **双端严格同步** attempt / 轮次 / batch / 序号 / 阶段；发现错位（如 Host 第 10 次 / Player 第 6 次）→ **作废当前 attempt → 清理 → 重新同步 → 再打**；这条优先于"尽快打洞"。
11. **§E 严格顺序**：确认 Host → 确认目标 → NAT 测量 → 入站测量 → 上报 → **等待双方都完成（等待同步起跑）** → 同步 candidate → 双方确认 → 同步起跑 → 同步起降 → Punch。**测量在用户点「开始连接」之后做**（不是开软件就测），任何一步不同步都不许继续。
12. **Timer 每 Host↔Player 独立一套**：一次连接（成功或失败都算终态）**到达终态立即停止、清零、释放**；下次重连从 0 开始，**不保留"最终耗时"**。

**断线与重连**
13. **Host 掉线（成员心跳超时被服务器清理）= 真正离开房间**：断所有 Player、释放 tunnel/状态/Timer、释放 Host 位、广播、重算按钮；**UI 不新增"离线"字样**（沿用 没人认领 / `xxx（未开服）` / `xxx` 三态）。
14. **被动掉线两类判定**：重连先查 token 是否有效 / 是否已被踢 → 被踢按退出处理；**真·闪断才自动重建**（host/player 同规则）。自动重连**只允许**「模式热切换」+「被动意外掉线」；其他情况必须手动点开始连接。**Host 恢复后玩家必须手动重新开始**。
15. **游戏中新人加入不得影响已打通的连接**；但**换模式必须严格重建全部通路**。

**UI 与交付**
16. **UI 三层只做展示**：通路（n/m）→ 打洞进度（n/m）→ 逐目标行（`等待同步起跑（3s）【玩家名】N/C` / `打洞中【玩家名】第 3 次 · N/C` / `P2P 已直连【玩家名】ip:port 18ms`）；未建立连接前显示 **N/C**。**底层连接状态绝对不能汇总成一个状态；不许为修 UI 去改 P2P 逻辑**。
17. **交付纪律：一轮做完，不许"做一半"或把条目留到下一轮**；没做完必须当场说清并给理由。

> 相关不变量：直连/打洞**只许新增**，不许改变 Relay 通路行为；中继永远只转发（见 `V3-AGENTS.md` §1）。

---

## 7. 打洞流程树摘要

> **全文（每片叶子带 `file:line`）：`docs/PUNCH-FLOW-TREE-20260928.md`**。以下是骨感摘要。

**入口（谁能让它跑 / 停）**
* ① 正向唯一入口：用户点「开始连接」`gui/app.py:on_start` → 门禁（不在房 / 房间被关 / `host_ready()` 为假）→ `engine.start_session` → 会话握手（五行都带 `ver=`）→ `STARTED 1` → `_refresh_room_ui` → `_sync_room_nat` → `engine.watch_room_nat` / `watch_room_direct(True)` → `RoomDirectLink.start()/_run()`。
* ② 停止：`on_stop` → `engine.stop_session` + `STARTED 0` + `watch_room_nat(False)` → `RoomDirectLink.close()`（**提醒：`close()` 关掉所有洞；`stop()` 只停循环、刻意留 socket**）。
* ③ 手动重连 = ② 再 ①；④ 自动重连（被动掉线 / 换模式）→ `_rebuild_tunnels` → `rebuild_done` → 再走 ① 那一支。
* ⑤ 模式热切换：中继推 `CONN_MODE_CHANGED` → 先 `watch_room_direct(False)` 拆干净 + 打 `_mode_rebuild_pending` → 新模式 `HOST_STATE` 一到 → `_maybe_rebuild_for_mode_change()` 按新模式快照重建。
* ⑥ 新人加入：`HOST_STATE` 变 → `_run()` 每轮重拉 `_peers()`；`_sleep_or_wake` 每 0.25s 观察，来新目标立刻醒 —— **不扰动已打通的洞**。
* ⑦ 对端离开 / 掉线：roster 变化 → `_sweep_links` 只拆那一条；隧道 20s 无数据（`LINK_DEAD_S`）同理。

**阶段（§E 十步，`punch_flow.ConnSession.advance()` 只许 +1）**
`confirm_host → confirm_target → nat → inbound → report → sync_start → candidate → confirm_cand → sync_go → punch → connected / failed（终态不可逆）`。
同步靠中继**逐字转发**既有 `DIRECT_HIT / DIRECT_CAND / DIRECT_PUNCH`（带 `att=/batch=/from=`，**没有新协议**）；"先测完的一方等对端也测完"由 `_wait_synced_start` 保证（超时 12s，**不硬打**）。

**socket 归属（关键）**

| 阶段 | 谁拥有 socket | 谁关 |
|---|---|---|
| 打洞中（measure / advertise / attempt） | `DirectLink`（命中后 `punched_sock`） | `DirectLink.close()` |
| 打通、还没交给承载层 | 同上 + 房间保活 `start_keepalive()` | 同上 |
| 已交给承载层 | `PeerLink`（用 `punched_sock`）→ `UdpHub`（唯一读者） | `PeerLink.close()` → `hub.close()`（关同 socket 上所有 tunnel） |
| 交给承载层之后 | 房间保活**刻意停掉**（`carried=True`），改由隧道 `KEEPALIVE_S=5s` 维持映射 | —— |

**谁关谁**
* `close()`（`watch_room_direct(False)` / 退房 / 切回 relay）：关**全部** carriers / punch link / DirectLink / sessions。
* `stop()`：只停循环 + 每条 timer `finish()`，**socket 保留**（映射是资产）。
* `_sweep_links → _drop_peer`：对端真离开 / 20s 判死 / 隧道握手两次失败 → **只关那一条**。
* `_punch()` 的 `finally`：只关"这一轮没被保留的那条" DirectLink。

**两个终态别混**：一轮（attempt/batch）失败 → `note_round_failed()`（表继续跑）；一次连接（generation）到终态 → `finish()`（Timer 停/清/释放）。

---

## 8. 本轮（v3.5.1）状态

### 8.1 已修并交付：「洞打通马上就断」

* **现象**：打通后约 3 秒，界面从「P2P 已直连：1/1」+「P2P 已直连【名】ip:port 18ms」退回「直连尝试中... 0/1」+「确认 Host（0s）【名】N/C」，日志多一行 `new connection cycle ... generation N -> N+1`。
* **根因**：`punch_flow.py` 的 `TERMINAL_STAGES = {CONNECTED, FAILED}`（`:93`）与
  `needs_new_cycle = terminal or timer.released or idle`（`:492`）把**成功终态**和**失败终态**混为一谈：
  `room_direct._run()` 每轮开头看到"终态"就 `sess.restart()` 回 §E 第一步（`generation+1`、`connected=False`、`batch=0`），
  每 `RETRY_INTERVAL_S=3s` 发生一次。**socket 没被关**（`peer_ready` 仍真 → 不重打），
  断的是"这条连接已建立"的账。
* **修法（v3.5.1）**：`room_direct._run()` 在 `needs_new_cycle` 之前加门
  `if sess.connected and self.peer_ready(peer): continue`（当时的行号约 `room_direct.py:1313`；
  隐患 1 修完后这条门已演进为 `punch_flow.ConnSession.cycle_action()` 的 `CYCLE_KEEP` 分支，见 §8.2）。
* **回归网**：新增 `tests/direct_persist_test.py`（先红后绿；离线版 + 真中继/真引擎/真 MC 连接，打通后连续 8s 跨两个 3s 重试轮，阶段/轮次/隧道不许变、字节双向不许断）。已登记进 `run_all.py`（第 34 套）。

### 8.2 正在修：上一轮明确记下的 6 个隐患（**本轮另一 agent 在改 `client/**`、`tests/**`**）

> 来源 = 上一轮交付报告 §⑥ + 本轮协调者派单（不是我从代码里猜的）。**每条都要"先红后绿"+ 防回归测试，不许顺手改别的、不许放宽规则 6。**

1. **`needs_new_cycle` 把成功终态当失败终态**（`punch_flow.py`：`TERMINAL_STAGES` :93、`needs_new_cycle` :492）。
   上一轮只在 `room_direct._run()` 那一个生产调用点加了门；本轮要修**语义**：区分"成功终态（保持连接）"与"失败终态（开新一轮）"，给调用方一个**无歧义 API**，让它从任何调用点都不可能复发；且 `direct_persist_test` 必须保持绿。
2. **`room_direct.start()` 幂等闸门不真实**（约 `:857` `_established and transport_ready`）：`transport_ready` 只检查 `link/peer_addr` 非空，停止后若洞已死会**永远挡住重打**。判据要改成"洞是不是真的还活着"（keepalive / `carried` / tunnel 状态）。测试：established → stop → 洞判死 → 再 start 必须真的重新打。
3. **`_sweep_links` 拿"打洞目标表"当"花名册"**（约 `:1585` 起）：`started=` 抖一下就可能把**活着的洞**当"对端离开"拆掉。要用**房间成员花名册**（`RoomState` members / peer_ids）判"真的离开"，真离开/`NO_HOST` 路径照旧。测试：只有 `started=` 抖动时活洞必须存活（不 `_drop_peer`、不重打）。
4. **`_open_tunnel` 两次隧道 HELLO 失败就扔掉已打通的 socket**（约 `:1551`）：新 socket = **新 NAT 映射** = 又一次"打通又断"。要改成**同一 socket 有界重试 + 退避**，只有 socket 真死才重打。测试：活 socket 上两次 HELLO 失败不许重打（复用同映射）；真死 socket 仍要重打。
5. **`carrier` 没起来就永不判死**（`_sweep_links:1573` 附近：`carrier is None or not carrier.started` 时不走 20s 判死）：隧道没起来的洞，只要 `DirectLink` 保活线程还活着就永不重打。要给真实 deadline。测试：carrier 始终起不来 → 到点判死并重打。
6. **`flow_summary` 在读路径里写状态**（约 `:1168-1190`：对不在 `sessions` 里的 link 现场 `_session()` 造一条），导致显示不对称。汇总必须**纯只读**（从现有 sessions/links 派生，不创建/不改状态）。测试：反复调用汇总不许新建/改动 session，不许改 generation/stage。

> **新会话请先确认它们是否已落地**：`git` 不可用 → 直接读 `client/mclink/core/room_direct.py` /
> `punch_flow.py` 与 `tests/` 里相关套件，并跑两道门禁。上面的行号是**修复前**的快照，代码一变就漂。
>
> **我在 2026-09-29 00:15 前后读到的最新状态（另一个 agent 正在同一批文件上收尾）**：
>
> | 隐患 | 我在代码里读到的落地痕迹（00:15） |
> |---|---|
> | 1 | `punch_flow.py` 新增 `SUCCESS_STAGES`（:110）/ `FAILURE_STAGES`（:111）/ `TERMINAL_STAGES`（:112）、`success_terminal` / `failure_terminal` / `cycle_action(link_alive)`（:528 起）；`needs_new_cycle` 对成功终态短路返回 False。**已落地**。 |
> | 2 | `room_direct.py` 新增 `_link_alive(link)`（:349 静态方法）；`start()` 的闸门注释明确写了"隐患 2"（约 :905-925）。**已落地**。 |
> | 3 | `_sweep_links(roster=None)`（:1799）改为默认读 `RoomState.peer_ids`（`_room_roster()`），目标表不再是花名册。**已落地**。 |
> | 4 | `_open_tunnel`（:1616 起）改成同一 punched socket 上有界重试 + `TUNNEL_RETRY_BACKOFF_S`，不再两次失败就 `_drop_peer()`。**已落地**。 |
> | 5 | `TUNNEL_SETUP_DEADLINE_S = 45.0`（:103）+ `_link_overdue()`（:1797），carrier 没起来到点判死重打。**已落地**。 |
> | 6 | `flow_summary`（:1272）注明"纯函数（隐患 6）"，用只读替身 `_link_only_session()`，不再现场 `_session()` 写状态。**已落地**。 |
>
> ⚠️ **但截至 00:16**：`tests/` 里**还没有**任何新测试引用这批 API（`grep cycle_action/SUCCESS_STAGES/_link_alive/TUNNEL_SETUP_DEADLINE_S` 在 `tests/*.py` 里零命中），`tests/run_all.py` 仍是 23:27 的 48 套。也就是说**代码改动在飞、回归测试与门禁结论还没落盘**。新会话务必：**跑 `tests/run_all.py` 看真实结果**，并按 §8.2 每条去 `tests/` 找对应回归网；找不到就说明"先红后绿"那一步还没做完。
> 协调者计划：6 条修完后**升 3.6.0 → 重打包 exe → `sync-deploy` → 两道门禁**；
> 所以你看到的 `APP_VERSION` 可能是 `3.6.0`（那就以 `settings.py` + `deploy/server/VERSION` 为准）。

---

## 9. 仍然悬而未决的问题（逐条：现状 + 需要谁决定）

| # | 问题 | 现状（代码/文档证据） | 需要谁决定 |
|---|---|---|---|
| ① | **嵌套 NAT 判得准不准**：校园 CGNAT + 全锥路由器，我们测出 NAT2 是否对 | 只有一个公网 IP 时"换 IP"类探测无法满足，分类退回单 IP 判定（`stun.py` 的 `SECOND_HOST_GRACE_S` 宽限、`refine_nat_subtype`、界面会说明"只有一个 IP 有响应"；`V3-AGENTS.md` §16） | 需要**第二台不同公网 IP 的 STUN**做对照实验；用户/环境侧决定是否提供 |
| ② | **Host 的 `conn` 值要不要单独用 `hosting`** | `PROTOCOL.md` §8.5：`connected` = 已进世界 **或** 当前 Host 正在开服，两者合用一个值 | 用户（是否要区分"开服中"与"玩家已进世界"） |
| ③ | **免登录模式下"顶替开服连接"只校验 `client_id`，是否收紧** | `relay_server.py::_same_host_person`：账号体系开着时还比 token；**免登录/老客户端 token 本来就空 → 只看 client_id**（代码注释说这是既有语义） | 用户/安全取舍（`client_id` 会随 `peers=` 广播） |
| ④ | **真 VPS 网络从未验证（回环只证明机制）** | `udptunnel_test.py` 注释明说 loopback proves mechanism, does NOT prove the user's network；本机只有一个 NAT | 用户实测（真实两端） |
| ⑤ | **"连接状态进管理广播"已做，但面板 PHP 尚未渲染成员状态列** | `HOST_STATE conn=` / `STARTED` 已上线（`PROTOCOL.md` §8.5/§8.6），管理快照 `member_list` 已有 `conn` 与 `connecting`（`relay_server.py:1353/1357`）；`panel/web/views/rooms.php`、`live.php` 里**没有**引用这两个字段 | 协调者/面板侧（要不要加列） |
| ⑥ | **`started=` 的服务端语义与客户端 `STARTED` 上报已存在，面板/文档需同步** | `PROTOCOL.md` §8.6 = 客户端显式 `STARTED 1/0`，并集进 `started=`；快照键名 `connecting` | 面板/文档侧同步 |

---

## 10. 给新会话的第一条行动建议

1. **先跑两道门禁确认基线是绿的**（§3；GUI 前先做 X socket 软链）；
   红了先按 §3/§4 判断是真红还是环境/并发导致的假红。
2. **读 `AGENTS.md` 规则 1–6 + `docs/DESIGN-BASELINE-20260927.md`**（§6 是本会话必须背下来的清单）。
3. **确认 §8.2 那 6 条隐患是否已落地**（读 `room_direct.py`/`punch_flow.py` + 跑相关套件）；
   没落地就接着修，改每条都先写能复现的红测试。
4. **如果是继续修打洞 / 排打洞 BUG**：先读 `docs/PUNCH-FLOW-TREE-20260928.md`（带 `file:line` 的全树）
   和 `docs/CLIENT-PUNCH-FLOW-20260928.md`（实现与规则 6 审计），用它们定位，再动手。
5. 动手前 `todo_write` 建单（`V3-AGENTS.md` §0）；改完按 §2 发版、§3 跑门禁；
   收尾说清"改了什么 / 怎么验证 / 用户下一步做什么"。

---

## 11. 历史决策索引（每个 `docs/*.md`）

| 文件 | 写于 | 解决什么问题 | 现在还有效吗 |
|---|---|---|---|
| `docs/DESIGN-BASELINE-20260927.md` | 2026-09-27 | 用户逐条确认的核心设计基准（§A 身份 / §B Host / §C 掉线 / §D 目标 / §E 顺序 / §F 独立 Session / §G UI / §H Host 位保留）；= `AGENTS.md` 规则 6 全文来源 | **有效，最高权威** |
| `docs/PUNCH-FLOW-TREE-20260928.md` | 2026-09-28 | 按代码现状把「点开始连接→打洞→交 socket」整理成流程树，每片叶子带 `file:line`；定位"打通即断"的起点 | **有效，最新最准的打洞地图** |
| `docs/CLIENT-PUNCH-FLOW-20260928.md` | 2026-09-28（23:28 更新） | 客户端打洞实现说明 + 规则 6 逐条审计（哪条被我们改坏了）+ 第二轮 8 条 BUG + 第三轮"打通即断"根因与修法 | **有效**（含最详细的问题—根因—测试对照） |
| `docs/PROTOCOL.md` | 持续（§8 更新到 2026-09-28） | 冻结协议 + V3 ADDITIVE 契约：`HOST_STATE host_id=/started=/conn=`、`STARTED`、错误码、Host 顶替 | **有效，协议权威** |
| `docs/BASELINE-REGRESSION-AUDIT-20260928.md` | 2026-09-28 | **中继侧**对规则 6 的回归审计：逐条 `file:line` + "本来就这样 / 我们改坏的" + 修复 + 回归测试 | **有效**（中继侧基线的对照表） |
| `docs/ISSUES-20260927.md` | 2026-09-27 | 第二轮问题集逐字转写 + 审计（问题 1 热切换、2 测量时机、3 端口按钮、**4 热认领 host = 已冻结**、5 游客改名、6 无限重试/host 丢失） | **部分有效**：问题 4 按用户确认**冻结不做**；其余已修/已归因，作历史参照 |
| `docs/LOG-AUDIT-20260927.md` | 2026-09-27 | 四份客户端日志的 7 个问题审计（P0-1 tunnel_reader NameError、P0-3 重建带 role、P1-5 打洞快路、P1-6 relay 模式不出洞行、P2-7 流量账本、P2-8 亚毫秒/序号…），客户端侧已修 | **有效**（对应 `punch_traffic_test.py` 等回归网） |
| `docs/AUTO-RECONNECT-PLAN.md` | 2026-09-27（用户批准） | 会话自动重连方案：触发/终态表、退避 `0.5→15s`、窗口 60s、四态 UI、每次重连固定顺序、游客身份会话级 | **有效且已实现**（`auto_reconnect_test.py`） |
| `docs/BUGFIX-20260915.md` | 2026-09-15 | v2 致命 BUG（3 人显示 9 人 + 第二人连不上）根因/修法 + 服务端日志模块诞生 | **历史有效**（`session_lifecycle_test.py` 钉住） |
| `docs/V2-LEFTOVER-CONNECT-FREEZE.md` | v2 末期 | "加入房间后开始连接概率性无响应"的取证与修法建议（怀疑 `close_writer` 无超时） | **参考**：同类问题已在 V3 用 `stop_session→abort` 处理（`V3-AGENTS.md` §17）；本文件留作"先取证再改"的方法论 |
| `docs/DEPLOY_GUIDE.md` | v2 时期 | 首次部署：中继/面板/数据库/1Panel 反代/SSL 的完整步骤 | **部分被取代**：日常发版看 §2 + `deploy/rebuild.sh`；首次搭建/排错仍可用 |
| `docs/V2_DEPLOY_NOTES.md` | v2 时期 | 更早的 v2 部署进度与协议变更说明 | **已被取代**（看 `START-HERE.md` / `DEPLOY_GUIDE.md`） |
| `docs/PENDING.md` | v2.2.0（2026-09-14 前后） | v2.2.0 客户端改名 CraftLink + UI 美化/i18n 的接力备忘 | **已被取代**（`STATUS-V2-ARCHIVE.md` / `START-HERE.md`） |
| `docs/HIDE_PANEL_ENTRY.md` | v2 时期 | 面板入口隐藏 + 反代 404 排查（含公网实测地址与状态码） | **有效**（运维排查手册） |

> 仓库根目录还有（不在 `docs/`）：`AGENTS.md`（**硬规则 1–6，每次先读**）、`V3-AGENTS.md`（完整规则 + v3 专属注意事项 1–18）、
> `START-HERE.md`（开发入口/目录地图）、`STATUS-V2-ARCHIVE.md`（v2 已验收清单，改动前先问）、
> `ROADMAP-V3-V6.md`（v3→v6 路线，AUTO 在 v3–v5 保持上锁）、`HANDOFF-TO-NEXT-SESSION.md`（v3 早期交接）、
> `DELIVERY-V3-STUN.md`（逐版本交付史）、`NEXT-SESSION-v2-history.md` / `PROJECT_STATUS.md`（v2 历史，查"当初为什么"用）。

---

## 附：一句话记住这台机器上的规矩

> **改了东西就升版本（`bump-version.py`）→ Windows 重打包 exe → 拷进 `deploy/` → `sync-deploy.py` →**
> **`check_deploy.py` 71/71 绿 + `XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/run_all.py` 48 套绿 →**
> **`deploy/` 整包上传 `/opt/mc-link/` → `mcr`。**
> 动 `client/**` 打洞相关之前先读 `AGENTS.md` 规则 6 + `docs/PUNCH-FLOW-TREE-20260928.md`。

---

# 附：定稿补记（v3.6.0，2026-09-29，协调者收尾时写入）

> 上面 §8 里"在飞的 6 条隐患"**已全部落地**，本节就是它们的最终结论。若两者冲突，**以本节为准**。

## 版本与交付物
* `APP_VERSION = 3.6.0` == `deploy/server/VERSION = 3.6.0`；客户端 exe 构建 `2026-09-29 00:55`，13,333,404 字节，sha256 `8353ddcdbdfd9c64efc6839133b6a9bd7d9e02778d9f2e52354815c273f10e7d`。
* 两道门禁：`check_deploy.py` **71/71 全绿**；`run_all.py` **49 套全绿**（本附写完后由协调者实跑确认）。

## 六条隐患的最终结论（全部"先红后绿"）
新增回归套件 **`tests/direct_hardening_test.py`（36 passed / 0 failed）**，已登记 `run_all.py`。

| # | 根因 | 修法（文件:行，v3.6.0 时点） | 测试 |
|---|---|---|---|
| 1 | `TERMINAL_STAGES={CONNECTED,FAILED}` 把成功终态当失败 → 每 3s `restart()` 回 §E 第一步 | `punch_flow.py:110-112` 拆 `SUCCESS_STAGES`/`FAILURE_STAGES`；`:528` 成功终态短路 `False`；`:552` 新 API `ConnSession.cycle_action(link_alive)→ keep/restart/start`；`room_direct.py:1413` `_run()` 只认它 | `test_success_terminal_is_not_a_new_cycle`、`test_run_keeps_a_punched_link_beyond_the_retry_rounds` |
| 2 | `transport_ready` 只看引用非 None → 洞死后再 `start()` 被幂等闸门永久挡住 | `room_direct.py:358 _link_alive()`；`:452 transport_ready = running or ready` | `test_start_repunches_when_the_hole_is_dead` |
| 3 | `_sweep_links` 拿**目标表**当花名册 → `started=` 抖动误拆活洞 | `:1814 _sweep_links(roster=None)` 默认读 `RoomState.peer_ids`（`:1771 _room_roster`）；只对曾是花名册成员的 key 生效，本地别名不误杀 | `test_started_flap_keeps_a_live_link`、`test_roster_leave_still_drops_but_aliases_survive` |
| 4 | `_open_tunnel` 两次 HELLO 失败就扔掉已打通的 socket（新 socket=新映射） | `:1630` 同一 socket 有界重试 + 指数退避（`TUNNEL_HANDSHAKE_ATTEMPTS=3`）；只有 `_link_socket_dead()`（`DirectLink.closed`）才重打。**注意：判据不能用 `sock is None`**，`nat_diag_test` 的替身合法地 `sock=None` | `test_tunnel_handshake_reuses_the_punched_socket` |
| 5 | `carrier` 未 started 的洞永不判死 | `:103 TUNNEL_SETUP_DEADLINE_S=45`；`:1799 _link_overdue()` 到点判死 → `_drop_peer` → 下一轮重打 | `test_tunnel_setup_deadline_repunches` |
| 6 | `flow_summary` 在读路径里 `_session()` 写状态 | `:1286 flow_summary()` 纯只读；`:1263 _link_only_session()` 用完即弃；`:1080 _select_targets_now()` | `test_flow_summary_is_read_only` |

## 需要新会话注意的已知事项（本轮**故意没动**）
1. **`docs/PUNCH-FLOW-TREE-20260928.md` 是 v3.5.1 之前的快照** —— 其中 `needs_new_cycle` 的描述与行号已过时；最新、准确的行为说明看 `docs/CLIENT-PUNCH-FLOW-20260928.md §7`。要看树状结构仍可读它，但**行号必须重新核对**。
2. **停止→开始且洞还活着**时 `start()` 仍提前返回、`_run` 循环不重启（既有的"长连接语义"）：那一刻之后**新进房的 peer 要等这条洞死/被清掉才会被打洞**。若要改需用户拍板。
3. `no_host`/`host_not_ready` 路径**不主动拆洞**，靠 20s/45s 判死兜底。
4. `_start_ingress_probe` 等既有旁路、以及 UI 层本轮未碰。
5. §9 的 6 个悬而未决问题**仍未解决**（嵌套 NAT 需第二公网 IP、`hosting` 命名、免登录顶替判据、真 VPS 未验证、面板未渲染 `conn` 列、`started=` 文档同步）。

---

# 附：服务端侧补记（2026-09-29 中午，server/ 那一轮）

> 本节只记录**服务端（`server/`）与交付脚本**这一轮的落地事实；行号会漂，
> 以 `AGENTS.md` 规则 6 + `docs/DESIGN-BASELINE-20260927.md` §H-2 为准。

## 1. P0「没人操作，模式自己跳回 relay」——根因与修法

* **根因 A（真凶）**：`relay_server.handle_player` / `handle_host_tunnel` 用
  `get_or_create_room` 建房间 —— 一个**迟到 / 走错房间名**的 PLAYER /
  HOST_TUNNEL 握手会把一个全新的 `Room` 塞进注册表，而新房间的连接模式是
  构造默认值 `relay`。Owner 随后重连就落进这个"新房间"，广播开始报
  `mode=relay`，客户端据此拆掉直连通路。
  修法：这两个握手改成**只读查找**（`resolve_room`），没有房间就照旧回
  `ERROR NO_HOST`（回复逐字相同，行为零差异）。房间只由 `CREATE` /
  legacy `HOST <room>` 建。
* **根因 B（兜底）**：`broadcast_member_state` 用
  `getattr(room, "conn_mode", "relay")`，没有字段时凭空报 relay。
  修法：读口径只有 `room_members.room_conn_mode(room)`（没有字段时是 `-`）。
* **唯一写入点**：`room_members.set_room_conn_mode(room, wanted, member, cause)`；
  线上唯一调用者是 `CONNMODE` 分支（先断言 `room.owner is member`）。
  新增 `mode_reason=`（`owner|restored|default`）随 `HOST_STATE` 广播（PROTOCOL §8.9）。
* **允许的唯一例外**：房间销毁后由**同一个 Owner**（同 client_id）重建，
  `room_registry` 里 300 秒内的模式记忆会被恢复（`mode_reason=restored`）；
  换了人建房是 `default`，绝不继承。
* 回归网：`tests/mode_identity_reconnect_test.py`（真中继：迟到握手不造房 +
  玩家掉线 / Host 停服 / 重开服 / 显式切模式 之后 `mode=` 必须还是 stun）。

## 2. 连接 ≠ 身份/职位（用户口径的服务端落地）

* **Host 停服/开服连接断掉**：只拆连接、断游戏连接（可识别原因），
  `host_claimed` / `host_name` / `host_id` **保留**；别人 `CLAIM_HOST` 被拒
  （`ERROR HOST_TAKEN`），新 PLAYER 得到 `ERROR HOST_NOT_READY`（不是 `NO_HOST`）。
* **`RELEASE_HOST`** 现在同时释放该成员手上的 legacy `HOST <room>` 控制连接
  （以前只清 `is_host`，中继会一边报 `host_id=<新人>` 一边继续用旧人的隧道配对）。
* **座位宽限期**（`MCLINK_MEMBER_SEAT_GRACE`，默认 45 秒）：成员控制连接断掉、
  但**身份 token 仍有效**（活着的游客会话 / 中继发的游客 token 还在 / 账号
  token 校验通过）-> 座位（含主机位、Owner 身份）原地保留，等到本人重连
  （`add_member` 把座位交给新连接，房间不销毁、模式不变）；宽限期满才按
  `reason=timeout` 走原来那条唯一清理路径。**没有 token 的连接不享受宽限**
  （老客户端行为逐字不变）。
  `reap_idle_members` 跳过宽限期内的成员，避免看门狗把在位的 Host 收走。
* **管理员关房（`revoke_room`）不算"闪断"**：`room.closing=True` 期间不进宽限，
  并且关房时把成员 `removal_reason` 标成 `room_closed`。少了这一条，带有效
  token 的成员会各挂 45 秒，面板 `POST /rooms/<room>/revoke` 直接 8s 超时
  （实测复现过）。`tests/room_settings_test.py` 因此把宽限压到 3 秒，并把
  "Owner 掉线后房间自动销毁"改成"宽限期走完才销毁"（旧期望与新口径冲突）。
* 回归网：同上那一套（停服保位 / 释放 / 踢 / 超时 / token 失效 / Owner 组合角色）。

## 3. 重连判定 `SESSION`（PROTOCOL §8.10）

`SESSION __mclink__ <cid> <user> <token>` —— 只读探测，一条短连接：
`valid=` / `in_room=` / `token=account|guest|none` / `reason=` /
`auto_reconnect=` / `relogin=`。客户端据此区分「闪断 -> 自动重连」与
「被踢/退房/超时/房间没了/token 失效 -> 不自动重连，必要时重新登录」。

## 4. 热切换 `was_live=`

换模式那一轮（且只有那一轮）的 `HOST_STATE` 与 `CONN_MODE_CHANGED` 原因行带
`was_live=0|1` = **切换前** conn= 里有没有 `connected`/`pairing`。
空转就只换模式；有连接就严格重建 + 自动重连（PROTOCOL §8.9）。

## 5. `mcr`（`deploy/rebuild.sh`）默认实时逐行

`run_step` 从"攒完再倒"改成 `"$@" 2>&1 | tr '\r' '\n' | tee | while read`：
默认（不加参数）就 `▶ 步骤名` -> 每一行 `│ ...` 实时出现 -> `✔ 步骤名（Ns）`；
同一份内容完整进 `/opt/mc-link/rebuild.log`；失败打 `✘` + 最后 25 行上下文。
`-v` 只是"更啰嗦"（把默认过滤掉的空行也打出来）；`--strict` 语义不变。

## 6. 交付包带交接文档

`scripts/sync-deploy.py` 把 `docs/HANDOVER.md` 复制成 `deploy/HANDOVER.md`
（生成物，禁止手改），并进 `README.txt` 清单与 `BUILD.txt` 指纹；
`check_deploy.py` 新增"deploy/HANDOVER.md == docs/HANDOVER.md"的漂移检查，
以及"run_step 必须实时逐行"的静态断言。

## 7. 本轮新增/登记

* 新套件（已登记 `tests/run_all.py`，共 52 套）：
  `mode_identity_reconnect_test.py`、`deploy_scripts_test.py`。
* `check_deploy.py` 现在 **75 项**（"构建步骤实时逐行"那组从 3 条扩到 6 条）。
* 版本号仍由协调者统一升；本节不涉及 `bump-version.py`。

---

# 附二：v3.7.0 定稿（2026-09-29，本轮：显示同步 / 自动重连 / 状态传播 / 模式不自己跳）

> 本节覆盖上面所有"在飞/待确认"项。**新会话只需读：`AGENTS.md`（规则 1~6，含两次补充）+ 本文件。**

## 本轮修了什么
1. **「模式自己跳回中转、连接断开」的根因（两条，都已修）**
   * 服务端：`handle_player`/`handle_host_tunnel` 用 `get_or_create_room`，**迟到/走错房间名的握手会凭空造出一个默认 `mode=relay` 的新房间**，Owner 随后重连就落进它 → 广播 relay、连接断开。已改只读 `resolve_room`；删掉 `getattr(room,"conn_mode","relay")` 兜底；唯一读 `room_members.room_conn_mode()`、唯一写 `set_room_conn_mode()`（只有 Owner 的 CONNMODE）。
   * 客户端：`mode_now = self._room_conn_mode() or MODE_RELAY` —— 广播里只要有一秒没有 `mode=` 就被当成 relay，**把刚打通的直连整个拆掉**。已改成"模式未知 → 什么都不做"，`_punch_mode` 单写入点。
2. **两端显示同步（用户核心要求）**：两端共用阶段轴；每个阶段进入即上报 `DIRECT_HIT stage=… att= batch= from=`（复用中继逐字转发，不新增协议）；`display_stage = min(本端, 对端)`，对端没到就显示「等待对端…」；**只让显示等、不让打洞等**；打洞那几秒每 0.3s peek 对端阶段。
3. **P0「一端已直连、另一端还在反复重试」**：打通一侧立刻上报 `stage=connected`；对端 `_adopt_peer_connected` 在**同一个已打通 socket** 上补保活+建隧道+`finish(True)` → `cycle_action=KEEP` → 停止重试。判据要硬（必须有链路且已知地址）；45s/20s 判死兜底不变；**打洞算法一行未改**。
4. **连接状态 ≠ 身份/职位（用户两次强调，本次落地）**：新增 `host_availability()`，把**身份**（`host=`/`host_claimed` → `no_host`，唯一允许认领的情形）与**连接**（`conn=` → `host_not_connected`/`host_ready`）显式分开；「认领主机身份」按钮**只在身份为空时可用**；主机不在线 ≠ 主机位空出。服务端相应实现"座位宽限期"（token 仍有效时控制连接断掉保留座位，默认 `MCLINK_MEMBER_SEAT_GRACE=45s`）；只有离开房间/被清退/超时/token 失效/`RELEASE_HOST` 才释放。
5. **热切换自动重连（用户语义）**：新增一次性 `was_live=0|1`（PROTOCOL §8.9）：`=0`（原本空闲）**只换模式不重建**；`=1`（原本有连接）**重建全部隧道 + 原本已连接的成员自动重连**，原本未连接的保持空闲；玩家不需要在软件里点任何东西。
6. **每对独立 + host 漂移**：每轮重新拉目标（`host_id=`），A/B 已连通时 C 加入只多一行、不影响已打通链路；host 换人后下一轮目标跟着换。
7. **显示升级**：`等待发包起跑`、`PUNCH #NNNN`（与基线"第 N 次"并存）、逐目标 `等待对端…` 全阶段、身份组合显示 `房主·主机/房主·玩家/玩家·主机/玩家`；三语齐全；未连接延迟 `N/C`。
8. **服务端新增协议**：`SESSION __mclink__ <cid> <user> <token>`（§8.10）探活；`was_live`（§8.9）；`ERROR HOST_TAKEN`（主机位仍在但未开服时认领被拒）；`mode_reason=owner|restored|default`。
9. **`mcr` 默认实时逐行日志**：`▶ 步骤名` / 每行 `│` 实时 / `✔ 步骤名（Ns）`；完整仍进 `/opt/mc-link/rebuild.log`；`-v` 更啰嗦，`--strict` 不变。
10. **交付包含交接文档**：`scripts/sync-deploy.py` 会把 `docs/HANDOVER.md` 生成到 `deploy/HANDOVER.md`，并进 `BUILD.txt`/README 清单；`check_deploy.py` 增加"两者一致"的漂移检查。

## 实测与门禁（本轮）
* 客户端 `run_all`：52 套 51 绿，唯一红是 exe 比源码旧（允许，收尾重建后消）。
* 新增套件：`punch_display_sync_test.py`(57/0)、服务端 `mode_identity_reconnect_test.py`(96/0)、`deploy_scripts_test.py`(36/0)。
* 时序敏感套件各 3 次全绿：direct_e2e/direct_reuse/direct_multi/direct_persist/direct_hardening/conn_mode_hotswitch/mode_switch_rebuild。
* `check_deploy.py`：74 项，唯一红为 exe 漂移。

## 本轮**没做完 / 只能靠真机验证**的（诚实清单）
1. **真·非对称（只有一端命中）在本机复现不了**（只有一个 NAT）：P0 用离线真装置钉死（含反例），真中继那条只证明信标通道真的通；**真实网络效果需用户实测**。
2. 三方「2 已连接 + 1 只加入未连接」的真中继热切换没有单独套件（三方"全已连接"有；"未连接成员保持空闲"在引擎层是平凡的）。
3. 第 4 条的真中继四情形（断开/被踢/掉线/host 离开 → 另一端状态与按钮）用纯断言 + 既有 `member_conn_state_test` 覆盖，**没有真界面+真中继的端到端断言**。
4. 第 5 条真中继场景（A 释放 host、D 认领 → B/C 重新确认目标）用纯回归 + `direct_multi_test` 覆盖。
5. 会话期间的**瞬时红**（服务端并发编辑造成 `IndentationError` / `conn_mode_reason` AttributeError / revoke 8s 超时）在最终实跑中已消失；新会话若再现，先核对是不是并发编辑。

## 附二补：`SESSION` 探活客户端侧已接上（v3.7.1）

服务端那份补记里写的"client/** 一行未动：`SESSION`/`mode_reason=`/`was_live=` 需客户端实装" **已不成立**，客户端本轮已实现：

* **`SESSION __mclink__ <cid> <user> <token>` 主动探活**（`client/mclink/core/control_client.py`：`SessionProbe`/`probe_session`/`session_probe_verdict`，2s 短超时、永不抛异常）→ 被动掉线后**先探活再决定**：`auto_reconnect=1`（STILL_IN_ROOM/DROPPED/NOT_IN_ROOM）→ 自动重建；`auto_reconnect=0`（KICKED/LEFT/TIMEOUT/ROOM_CLOSED/AUTH_*）→ 按退出房间处理（新事件 `session_ended`，不重连；`relogin=1` 弹"需要重新登录"）；老中继 `ERROR UNKNOWN_ROLE`/探不通 → 退回原 ROOMS 判定。**host 与 player 走同一条判定，不按职位分叉。**
* **`mode_reason=owner|restored|default`** 解析（`room.py`）+ 归因日志（`restored` 明确写 "restored, not a manual change"，且**不**触发 `conn_mode_changed`）。
* **`was_live`** 补齐等待池那条：`ERROR CONN_MODE_CHANGED … was_live=` 现在也被 `player_core`/`host_core` 解析并 emit 给同一个消费者（原 ERROR 语义保留）。
* **`HOST_TAKEN`** 经核对**已实现且正确**（认领被拒只清 `is_host`，不动 `host_claimed`/`host_name`/`host_id`；认领按钮只看身份），本轮只补回归测试。
* 新套件 `tests/session_probe_reconnect_test.py`（59/0），run_all 第 53 套。
* 已知代价：探活是串行的，老中继/探测不可达时掉线处理最多晚 2.0 秒（常量 `SESSION_PROBE_TIMEOUT` 可调）。

---

# 附三：v3.7.2 定稿（2026-09-29 晚 —— 用户第二轮实测的 7 条问题）

> 本节是这一轮的唯一结论；上面任何"待确认/在飞"以本节为准。
> 逐条对应用户报的现象 —— **每条都有回归测试**（新套件
> `tests/hotswitch_roster_state_test.py`，46/0，run_all 第 54 套）。

## 版本与交付物
* `APP_VERSION = 3.7.2` == `deploy/server/VERSION = 3.7.2`（`bump-version.py 3.7.2`）。
* 两道门禁：`check_deploy.py` **75/75 全绿**；`run_all.py` **54 套全绿**。
* 用户提供的上一轮日志已从交付包里清掉（`deploy/logs/`），
  原样留档在仓库根 `logs/2026-09-29-user-test/`（**不属于交付包**）。

## 逐条：现象 -> 根因 -> 修法 -> 测试

### 第 1 条：Host 已连接，对端只是"在房里没连接"，却显示「等待同步起跑」
* **根因**：Host 的目标表 = `started=` ∪ `_heard_started`，而 `_heard_started`
  **只增不减**（只有 `close()` 清）。于是"曾经开始过、现在 `conn=idle`"的对端
  仍是目标：Host 为他建状态机、走完 §E 到 `SYNC_START`，对端从不回应 ->
  那一行永远显示 `STAGE_LABELS[SYNC_START] = 等待同步起跑`（每 12s 超时重来）。
* **修法**（`room_direct.py` + `punch_flow.py`；**只动目标选择与显示，打洞算法未动**）：
  1. 新增 `punch_flow.peer_conn_state/peer_is_idle`（读 `conn=`）；
  2. `_heard_started` 的合并加两条新判据：`conn` 不是 idle，**或者**信标还新鲜
     （新常量 `HEARD_FRESH_S = 15.0` + `_heard_at` 每收到一条信标盖一次时间戳，
     `_beacon_fresh()`）。**只看 `conn=idle` 会误伤"正在打洞但还没开游戏连接"的
     玩家（stun 模式打洞先于游戏连接）—— 这一条被 `direct_persist_test` 抓到过；
     只信标新鲜则能区分"曾经"与"现在"**；
  3. 陈旧状态机清理：`_prune_sessions()` / `_prune_idle_sessions()`（只在
     "不是当前目标、也没有活洞、中继又明确说 idle/已离房"时清）。
* **测试**：`test_idle_peer_is_not_a_target`（含反例：信标新鲜仍必须是目标）。

### 第 2 条：直连打通后玩家被踢再重连 —— 玩家侧通、Host 侧"通了又断了等待同步起跑"
两个独立根因，都在这一轮修掉：
* **(a) 服务端把 `HOST_STATE` 推给了还没收到 `OK JOIN` 的连接。**
  `handle_member_join` 是 `add_member()` -> 若干 `await`（事件/审计）-> 写
  `OK JOIN`；中间别的成员触发 `broadcast_member_state()` 时，会往这条新连接写
  `HOST_STATE`。客户端 `_request()` 只读第一行当握手应答，读到 `HOST_STATE`
  就抛「服务器应答异常」并关连接 -> 自动重连整条失败（用户日志
  `[18:12:09] automatic rejoin failed: 服务器应答异常：HOST_STATE ...`），
  服务端随后看到"成员控制连接已关闭"走座位宽限。
  **修法**：`RoomMember.ready`（默认 True）+ `handle_member_join` 在
  `add_member()` 前置 False、写完 `OK JOIN` 再置 True；
  `broadcast_member_state()` 跳过 `ready=False` 的成员（它随后那条广播正好给
  新人送上第一份快照）。测试：`test_broadcast_skips_members_without_their_ok`。
* **(b) 客户端 `peer_ready()` 不认"已经被隧道接手的洞"。**
  判据只有 `DirectLink.keepalive_running`（打洞层的账）。隧道接手时会置
  `carried=True` 并 `stop_keepalive()`；一旦 `carried` 回落而
  `start_keepalive()` 在 `link.sock is None` 时**静默返回**（命中在阵列 socket
  上就是这样），打洞层的账变成"没保活" -> `peer_ready` 假 -> `cycle_action`
  返回 `CYCLE_RESTART` -> 日志 `new connection cycle (generation N -> N+1)`，
  界面退回「等待同步起跑」，而 UDP 隧道还在搬数据。
  **修法**：新增 `_carried_alive(peer)`（`carrier.started and not stop`），
  `peer_ready()` / `transport_ready` / `link_rows()` 的 ready 全部改用它 ——
  判据问**真正在搬字节的那一层**。测试：
  `test_peer_ready_accepts_a_carried_link`。

### 第 3 条：热切换（双向、多次）必须"严格重建 + 原已连接者自动重连、空闲者保持"
* **根因（三处）**：
  1. `mode_switch_should_reconnect(was_live, running)` 把 `was_live=1` 当成
     "我连过" —— 但中继的 `was_live` 是**房间级**事实（`conn=` 里任一人
     connected/pairing 就是 1），于是**空闲成员也被自动重连**（"空闲态的保持"破）；
  2. `room_joined` 里无条件 `self._mode_rebuild_pending = False`：热切换会让成员
     连接抖一下，紧接着的 `room_joined` 把"待重建"吃掉 -> 按钮还显示在连接，
     会话却永远不按新模式重建（"中转换直连……需要双端手动重连"）；
  3. `room_lost` 里 `self.running = False` **先于** `_trigger_auto_reconnect()`
     —— 闸门看的正是 `running`，于是"被动意外掉线自动重连"整条是**死代码**
     （日志写着 "the session will be rebuilt"，一次都没重建）；而且重连成功
     之后**没有任何地方**把 `running` 恢复 / 清 `_reconnect_phase`，界面永远停在
     "已停止"（"按钮状态被错误的释放了"）。
* **修法**（全在 `gui/app.py`）：
  1. `was_live == "0"` 一票否决；`=1` 只表示"房间里有人连过"，**以本地
     `running` 为准**（自己空闲就保持空闲）；
  2. `was_live` 改成"只否决"，`room_joined` 不再清 `_mode_rebuild_pending`；
  3. 掉线处理**先起重连、再清 running**；新增 `_session_busy()`
     （`running or _reconnect_phase == "reconnecting"`）给按钮用；
     新增 `_reconnect_succeeded()`（`rebuild_done` 时把界面恢复成"在连接"）；
     `_can_auto_reconnect()` 允许 `reconnecting` 阶段在 `running=False` 时继续；
     新增 `RECONNECT_DEFER_LIMIT`（撞上"另一次重建还在跑"时让路重试，不静默卡死）。
* **测试**：`test_hot_switch_keeps_idle_members_idle` +
  `test_reconnect_wiring_is_not_dead_code`，以及既有
  `punch_display_sync_test` / `mode_switch_rebuild_test` / `conn_mode_hotswitch_test`
  （真中继双向热切换、数据逐字节跑通）。
* 已知：Host 对重连玩家的"确认"仍走 §E（3s 轮询 + 最长 12s 同步等待），
  但第 1 条的修复消除了"给没在连接的人空打 12s"的噪声 —— 那是之前"确认很慢"
  的主要来源。

### 第 4 条：鼠标停在语言 / 主题下拉框上滚滚轮会直接切换
* **根因**：Tk 的 `TCombobox` **类绑定**把 `<MouseWheel>` 当"上一个/下一个选项"，
  `readonly` 下拉框因此被滚轮直接改值（`install_wheel` 的全局处理在 "all" 标签上，
  排在类绑定**之后**，拦不住）。
* **修法**：`_block_wheel_select(combobox, on_scroll)` —— 控件级绑定
  （bindtags: widget -> class -> ...）`return "break"` 挡掉类绑定，并把这一滚
  转交给页面 `_wheel_scroll_page()`，所以"任何地方都能滚"的手感不变，唯独
  滚轮不许改选项。语言框和主题框都装上了。
* **测试**：`test_combobox_wheel_is_blocked`。
* **顺带**：状态卡的「隧道 n/m 就绪」改成「空闲隧道 n/m | 已加入世界 n」——
  那个数一直是"空闲等待中的隧道"，旧文案让它看起来像"隧道数在乱跳"
  （用户第 6 条：6/7、7/8 跳、突然 0 又变 1）。用的是**已有的** i18n key。

### 第 5 条：房间内 4 人却显示 5 人（成员列表正常，热切换后恢复正常）
有两个独立的"房间内"数字，**真正的根因是房间列表那个**：
* **房间列表 / 面板**：`Room.snapshot()["members"] = len(member_names | world_names)`
  —— 把成员**显示名**和已配对游戏会话的 **PLAYER 握手名**按名字并集去重。
  游客的成员名是「游客2c12wl」，而它的 PLAYER 握手报的是客户端表单名
  `lubenwey`（游客没有账号 token）—— 同一个人贡献两个名字，4 变 5；
  换模式会 `disconnect_game_connections()` 拆掉所有会话 -> `world_names` 清空
  -> 当场回到 4。**这与用户"热切换后恢复正常"一字不差**。
  **修法**：按 `player_id` 判定"这条游戏连接是不是房间里某个成员"，是则不重复
  计；老式房间（完全没有成员记录）保持原来的并集口径。
* **房间内卡片 / 状态卡 / 成员列表头**：`HOST_STATE members=` 用的是
  `room.member_count()`（字典长度）。而 `add_member` 在"同一个 client_id +
  不同账号"时刻意保留两条记录（防篡夺），`members=` 就比客户端按 client_id
  能画出来的行数多 1。**修法**：新增 `_roster_members()` / `roster_count()`
  （按 client_id 去重），`HOST_STATE members=` / `peers=` / `names=` / `rtts=`
  与 `OK JOIN members=` 全部走它 —— 广播出去的整张表**自洽**（收到的人能画出
  来的行数 == `members=`）。正常房间（cid 互不相同）结果与以前逐字相同。
* **顺带**：`add_member` 判"同一个人"只比显示名，漏掉"同一个 client_id + 同一个
  凭证（guest token / 账号 token）但名字变了"的真实重连 -> 多出一条记录、
  而且座位宽限永远等不到交接。现在凭证相同也算同一个人（**防篡夺一个字没改**：
  拿不出凭证的仍然算另一个人）。
* **测试**：`test_roster_count_matches_rows`、`test_room_snapshot_counts_a_member_once`、
  `test_guest_reconnect_by_token_is_the_same_person`；既有 `room_settings_test`
  的"同 client_id 不同账号 = 2 人"仍然绿。

### 第 7 条：踢人之后打洞进度分母不降（房内 2 人还显示「打洞中 1/3」）
* **根因**：分母 = `len(items)`，items 来自 `_session_order()`；而
  `_session_order()` 在目标表之后**把 `self.sessions` 里所有残留的 peer 都补进
  行列表**。清理只发生在 `_sweep_links()`，它**只遍历 `self.links`**（打通过
  的洞）—— 一个"打了一半就离开房间"的玩家没有洞，状态机永远留着，分母永远多 1。
  （逐目标行是对的：它走 `link_rows()`，本来就是目标表 + 活洞。）
* **修法**：`_session_order()` **只认当前目标**（活洞由 `flow_summary()` 用
  `_link_only_session()` 只读替身补回，不丢已直连那一行）；
  新增 `_prune_sessions()`（花名册 + 只对真成员 id 生效）与
  `_prune_idle_sessions()`（`conn=idle` 且**不是当前目标**），由 `_sweep_links()`
  调用；`_run()` 的"没有目标"分支也会跑一次花名册清理（以前只在
  `no_peer`/`no_room` 时清 links）。
* **第二条（被踢了还在打洞）**：`hold_member_seat()` 的宽限循环只认
  "座位被交接 / 房间关闭"，**不认踢人** —— 用户日志里成员 18:12:23 被踢，
  中继还替他留座、继续转发 `DIRECT_HIT/CAND/PUNCH`，直到 18:12:55 座位超时才
  真的离房。**修法**：宽限循环加两条判据（`member.closed` /
  `removal_reason` 不再是 `drop`）立刻收工并记
  `[MEMBER SEAT REVOKED]`；`handle_member_join` 的 finally 用**最新**的
  `removal_reason`（kick 不再被改写成 timeout —— SESSION 探活靠它区分
  "被踢不重连"与"闪断要重连"）。
* **测试**：`test_stale_sessions_are_pruned`、`test_kick_cancels_the_seat_grace`、
  `test_kick_reason_is_not_rewritten_as_timeout`；既有 `kick_test`(105/0) 绿。

### 第 6 条：多人 中转→直连 热切换后自动重连不完成 / 计数乱跳
* 与第 3 条同一批修法（`was_live` 语义、`room_joined` 不清标记、掉线重连的
  死代码与按钮复位、`peer_ready` 认隧道）。
* 计数乱跳：那个数字是「**空闲**隧道」，现在文案说清了（见第 4 条末）。
* "打通两个然后又断了、游戏一直进不去"：第 2 条(b) 的 `peer_ready` 修复
  （打通后 3 秒被自己 `restart()` 掉）就是它。
* "房间列表里除 host 外全部未连接"：那是中继**当时**的真实事实（那些玩家的
  成员连接确实断了、会话没了），而自动重连没能把他们拉回来 —— 就是上面这批
  缺陷的后果；修完之后不再出现"界面在跑、其实一个都没连"的组合。

## 本轮**没做 / 已知**（诚实清单）
1. `sessions` 里的**本地别名**（老中继的 `host_name` / `guest-N`）不走
   "花名册判死"，所以老中继上它们不会被清理（与隐患 3 的口径一致，不能放宽）。
2. Host 对每个重连玩家的确认仍受 §E 的 3s/12s 节奏约束（协议要求严格同步）；
   本轮只保证"不再给没在连接的人空打"、"不再把自己刚打通的洞 restart 掉"。
3. 真机（真实两端 NAT）验证仍需用户实测：本机只有一个 NAT，只能证明机制。
4. `docs/PUNCH-FLOW-TREE-20260928.md` 仍是 v3.5.1 之前的地图，行号会漂；
   行为以本文件 + `docs/CLIENT-PUNCH-FLOW-20260928.md §7` 为准。

## 发版顺序（照做）
```
python3 scripts/bump-version.py 3.7.2        # 已完成
cd /tmp && cmd.exe /c "C:\Windows\Temp\clb4.bat"
cp client/dist/CraftLink.exe client/dist/CraftLink-console.exe deploy/
python3 scripts/sync-deploy.py
python3 scripts/check_deploy.py              # 期望 75/75
XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/run_all.py   # 期望 54 套全绿
```

---

# 附四：v3.7.3 定稿（2026-09-29 深夜 —— 热切换 relay→STUN 之后打洞必须真的起来）

> 承接附三。用户第二轮实测的**唯一遗留项**：
> 「STUN 换中转可以触发自动重连，但是**中转换 STUN 未触发正确自动重连** ——
> 自动重建隧道并开始自动对齐流程开始打洞，这个并未触发，**状态机显示已连接，
> 但是按钮状态是释放掉了的**，实际也并没有开启打洞流程。而且一个房间周期可能会
> 多次热切换模式，所以这东西你做好**活性**，避免只第一次可以，多操作几次就不行了。
> 还有热切换的**文案**提示……‘会断开连接当前所有人需要手动连接’，这个提示肯定是
> 过时了，要重新适配的。」

## 版本与交付物
* `APP_VERSION = 3.7.3` == `deploy/server/VERSION = 3.7.3`。
* 两道门禁：`check_deploy.py` **75/75**；`run_all.py` **55 套全绿**。
* 新套件 `tests/hotswitch_stun_resume_test.py`（27/0，run_all 第 55 套）。

## 1. 活性：`start()` 不再被"已经打通了"挡住（真正的根因）
`RoomDirectLink.start()` 旧实现有两道闸门：

```python
if self.running: return
if self._established and self.transport_ready: return   # ← 已删除
```

第二道的本意是"打通了就别再来一轮"（每打一次都是新 socket / 新 NAT 映射），
但它把**两件事混成了一件**：

* "要不要**重启循环**"（活性）；
* "要不要**重打这条洞**"（由 `_run()` 里的 `peer_ready()` 跳过 +
  `ConnSession.cycle_action(link_alive=True) -> CYCLE_KEEP` 负责）。

于是只要出现"**循环不在了、但洞/引用还留着**"，`start()` 就永远提前 return，
打洞**再也不会恢复** —— 用户看到的正是「没有开启打洞流程」；而
界面上的逐目标行还挂着上一条洞的"已连接"（`flow_summary` 用只读替身补的那一行），
所以是「状态机显示已连接」。

会造出这个状态的真实路径（每一条都够触发一次）：
* `_run()` 在 `room_current()` 短暂为 `None` 时 `return`（热切换拆旧通路、
  会话重建的空窗）；
* `_adopt_peer_connected()` 在循环之外把 `_established` 置真并挂上 `links` 条目
  （对端广播 `stage=connected`）；
* `stop()` 只能 cancel **还没跑起来**的 future，已经在事件循环里的旧协程
  取消不掉，`start()` 与它交叠。

**修法**：
1. 删掉第二道闸门 —— `start()` 只回答"循环在不在"；活洞由 `_run()` 保护；
   循环不在时，下一次 `watch_room_nat(True)`（每秒一次）就会把它拉起来
   （并落一行 `punch loop was not running; restarting it ...` 便于核对）。
2. 每一轮循环发一个 token（`_run_token`）：旧循环下一次抬头发现 token 不是
   当前的，就自己退出 —— **绝不会留下两条 `_run()` 同时跑**（那是历史 41 次
   打洞的成因之一）。单测直接 `_run()`（token=None）行为一字不变。

## 2. 重建成功 = 界面回到"在连接"（按钮状态那一半）
`_rebuild_tunnels()` 成功只说明 `start_session` 没抛异常，它**不动**
`self.running`。而 `_sync_room_nat()` 的开关是 `stun_room and self.running`：

* `running=False` 时，重建起来的会话是 ONLINE（状态机"已连接"），
  但按钮是"已停止"，而且**刚起来的打洞会被每秒关一次**
  —— 这就是「按钮状态是释放掉了的，实际也并没有开启打洞流程」。

`rebuild_done` 的界面处理现在会：自动重连分支走 `_reconnect_succeeded()`；
其它情况下 `running=False` 就补回 True 并落一行
`[mode] the rebuild finished and the session is live again ...`，
然后再 `_sync_room_nat()` 按新模式拉起打洞。

## 3. 文案（用户点名的过时提示）
| 位置 | 旧 | 新 |
|---|---|---|
| 切换确认框 | 「这会让【所有正在游戏中的玩家掉线】，需要他们重新连接一次（房间不用重建…）」 | 「切换会【重建全部隧道】：所有客户端会自动按新模式重连完成，正在游戏中的玩家不需要手动操作，当时空闲的成员保持空闲。」 |
| 房主模式提示 | 「切换会重建所有隧道，玩家会掉线重连一次」 | 「切换会重建所有隧道，客户端会自动重连（无需手动操作）」 |
| 重建日志 | `players will drop and need to reconnect once` | `every client reconnects automatically on the new mode (nothing to do by hand)` |

中/英/日三套 `i18n_tables.py` 同步更新（`gui_i18n_test` 25/0 绿）。

## 4. 回归网
`tests/hotswitch_stun_resume_test.py`（27/0）：
* **A** 真 `RoomDirectLink`：循环死 + 洞活 -> `start()` 必须重起循环，
  而且**不许重打那条活洞**（洞对象不变、`ROOM LINK UP` 不增加）；
* **A2** 旧循环的 token 会被换掉（旧循环自行退出，不许两条同时在跑）；
* **B** 真 `MCLinkApp` + 替身引擎：连续 5 跳（stun→relay→stun→relay→stun）
  每一跳都真的重建、并按新模式开/关打洞；以及"重建成功那一刻 `running`
  曾经是 False"的关键回归（必须被补回来 + 打洞被拉起）；
* **C** 文案：确认框/提示/译文都不许再出现"需要手动重连"的字样。
另外把 `tests/direct_hardening_test.py` 里那条**钉住旧闸门**的断言改成真幂等
（循环在跑时 `start()` 不起第二条循环、不换洞、不重打）。

## 5. 已知（诚实清单）
1. `transport_ready` 的语义是"可用 **或** 还在争取中"（`running or ready`），
   所以循环在跑时它必然为真 —— 判"洞死了没有"要用 `ready` / `_link_alive()` /
   `_carried_alive()`，不要用 `transport_ready`。
2. 真机（真实两端 NAT）仍需用户实测；本机只有一个 NAT，只能证明机制。

---

# 附五：v3.7.5 定稿（2026-09-30 —— 用户第三轮 7 条全部完成）

> 用户第三轮实测："好消息是基本没有大问题了，仅出现一个致命项，其他全部是优化项"。
> 第 1-6 条直接改；**第 7 条（起跑前的相对倒计时）按用户要求先确认再动**：
> 用户答复是"倒计时以毫秒走，例如 1.5s、1.2s 这样"，并要求"假如流程里没有
> 这一步就一定要加上"（NAT3打4 / 4打4 对相对延迟同步喷包要求很高）。
> 排查结论：**这一步真实存在**（见第 7 条），所以本轮做的是"让它看得见"。

## 版本与交付物
* `APP_VERSION = 3.7.5` == `deploy/server/VERSION = 3.7.5`。
* 两道门禁：`check_deploy.py` **75/75**；`run_all.py` **56 套全绿**。
* 新套件 `tests/port_kick_refresh_test.py`（44/0，run_all 第 56 套）。

## 逐条

### 第 1 条（致命项）：本地代理端口热切换以前只是"显示生效"
* 规则确认：**本地代理端口是唯一允许热切换的参数**；房间人数 / host 代理端口
  建房即冻结（`LOCKED_AFTER_CREATE = ("mc_port", "max_players")`，
  `_config_locked()`），谁认领 host 都按房间参数走 —— 这几条本轮**没动**。
* 旧行为：连接中改本地端口只在日志里说一句"下次开始连接才用新端口"，
  监听 socket 还绑在旧端口上 —— 用户看到的"热切换仅状态机显示生效、代理端口
  还是原来的、只有手动重连才生效"。
* 新行为（`app.py::_commit_port`）：
  * **空闲**（未连接）→ 直接生效，什么都不重建；
  * **连接中**（且本次会话角色是 **player**）→ 二级弹窗说清"会掉线、需要在
    Minecraft 里重进一次、隧道会自动重建自动重连、改完状态不变"，
    确认后**原地 `_rebuild_tunnels()`**：用新的 local_port 重新监听 +
    按房间当前模式重建这一条隧道并自动重连；取消则连输入框都退回旧值。
  * 房主侧改它**不重建**（重建房主会话会把所有人踢下线，那不是用户要的）。
* 三语新增 `修改本地代理端口` / 确认正文 / `已取消 —— 仍是 %s`。

### 第 2 条：客户端不能踢人（`KICK_NO_SUCH_MEMBER`）+ 成员列表列错位
* **真根因**：客户端把**已经是 token** 的房间 ID 又 `quote()` 了一次
  （`proto.room_token()` 不幂等；中继的 `room_names.room_token()` 是
  unquote+quote）。中文房间名于是变成 `%25E6%99%9A...`，中继那条
  "房间 token 必须是这个成员自己的房间"的闸门回
  `ERROR KICK_NO_SUCH_MEMBER 目标不在这个房间里`，而界面把它翻译成
  「该成员不在房间里（可能已经离开）」，看起来像很久以前那个服务端踢人故障。
  **修法**：客户端 `room_token()` 改成幂等（和服务端同一套语义）——
  `KICK` / `LEAVE` / `DELETE` 一起治好（它们都拿 `RoomState.room` / 房间列表里的
  token 再过一次）。权限矩阵一个字没动（owner + 当前 host；非 owner 的 host
  不能踢 owner）。
* **列错位**：表头和**每一行**以前各是一个独立 Frame、各带一套 9 列 grid，
  列宽各算各的；「踢出」是 `RoundButton`（Canvas，带 20px 内边距）而占位是
  `Label(width=6)`，两者请求宽度差 ~40px，于是有按钮的行整体平移。
  **修法**：表头 + 所有行**共用 `member_table` 这一套 grid**（行=row 号，
  列=下标），列宽只有一个来源，物理上不可能错位。

### 第 3 条：未连接时打洞进度只显示「未启用」
`punch_flow.summarize()` 的 `progress`、`app.punch_step_text()` 的 relay 分支
都从「未启用（Relay 模式）」改成「未启用」；i18n 里那对重复的旧键删掉，
换成 `'未启用': 'Not in use' / '未使用'`。

### 第 4 条：MC 多人列表反复刷新 -> 幽灵连接占满名额 -> 两人房也「房间已满」
三条根因、三处修法（服务端一条、客户端一条，另有放大器被客户端修法一并压掉）：
* **容量闸门按"人"去重**（`Room.occupied_seats()`）：旧闸门
  `len(sessions) + len(waiting_players)` 让**每一条 TCP 探测各占一个名额**，
  同一个人刷新 8 次就把 8 人房刷满。现在按 `client_id` / `player_id` 去重 ——
  一个人只占一个位子；真正不同的人照旧各占一个。
* **等待池有了真正的过期回收**（`reap_stale_waiters()`，由 `member_watchdog`
  调用）：`Waiter.created_at` 以前**全项目没人读**、`assigned.wait()` 也没有
  超时，半开的探测连接能一直占名额。现在超过 `WAITER_MAX_WAIT_S`（默认 45s，
  比客户端自己 30s 的等待上限更宽）就只关 socket，后续清理复用既有唯一路径
  （`watch_player_socket` 0.2 秒内看到 at_eof -> `handle_player_socket_lost`）。
* **客户端等配对时盯住本机 MC**（`PlayerCore._await_relay_or_mc`）：旧代码只等
  中继第一行（最多 30s），完全不看本机 socket —— MC 的探测早就关了，
  中继那条 PLAYER 连接还挂在池子里 5~27 秒（日志实测）。现在两条 socket
  一起等：MC 先关 -> 立刻收工（名额 0.2 秒内还回去）；MC 先发来握手包 ->
  **暂存**并在配对成功后原样先发给房主（一个字节不丢、不解析协议）。

> **★ 本轮自检发现并修掉的一个 P0（就在第 4 条的客户端改动里）**
>
> 第一版把"等配对期间从本机 MC 提前读到的握手字节"直接 `write()` 进中继 socket。
> 这在 **STUN 模式是致命的**：房主侧对中继隧道是"只读不转发"的
> （`direct_carry._drain_relay` 会把中继送来的游戏字节丢弃并记一行事故日志），
> 于是握手包人间蒸发、直连流从半路开始 —— Minecraft 直接连不上，
> 而且正好会触发用户三令五申禁止的"中继搬游戏数据"事故日志。
>
> 修法：加 `PlayerCore._PrependedReader`，把这段前缀**塞回读取流最前面**，
> 交给 `raw_relay()` —— 谁最后拿到通路（中继 or 直连）前缀就跟谁走，
> 顺序与字节数都不变。回归里钉了三项：前缀顺序、前缀未读完时 `at_eof()` 为假、
> 以及源码里**不许**再出现 `relay_writer.write(early_mc_bytes)`。
>
> 排查方式：先读房主侧 `_drain_relay` 的语义 -> 再验证"提前读走"是否必然发生
> （MC 连上即发握手，而我们在配对期间必须读才能识别对端关闭）-> 结论是必然，
> 所以这一版必须修。

### 第 5 条：面板在线房间页去掉「房间里暂时没有玩家。」
`panel/web/views/rooms.php`：会话表改成"有会话才渲染"（`$sessions !== []`），
没有会话时**什么都不显示**；有会话时照旧展示（含会话级踢人），和上面的主房间
成员表**允许重复**。旧的那行空态文案连同它的 `if/else` 一起删掉。

### 第 6 条：被踢的玩家重新进房（未点开始连接）不许被 Host 打起来
* 中继侧**不是**原因：`STARTED 1` / `member.connecting` 跟着被踢的
  `RoomMember` 一起消失，重进房是全新成员、`connecting=False`
  （玩家自己的广播日志可以证明 `started_ids=[host]`、`conn=idle`）。
* 真根因在 **Host 客户端**：`_heard_started`（"他曾经开始过"）以前只有
  `close()` 会清；而 `_beacon_fresh()` 的新鲜度是在
  `peek_direct()`（**非破坏性**，取出来再放回去）里盖时间戳的 —— 队列里那条
  旧 `DIRECT_HIT` 被每一轮、每次打洞的 0.3s peek 反复读到，于是**永远"新鲜"**，
  被踢的人一回来就立刻重新成为目标，§E 一轮一轮空转（日志里
  `did not report its measurement within 12s` × 13 轮）。
  **修法三件**：
  1. `RoomSession.direct_rx_at`：在**收到那一刻**按 `from=` 记时间戳
     （收到才记，和 peek 无关），`_beacon_fresh()` 优先用它；
  2. `_heard_seen`（按 sender 数队列里见过几条）：只有计数**增长**才刷新
     本地兜底时间戳，旧行不再被无限刷新；
  3. `_forget_heard(peer)`：花名册判死（他真的离开房间）时把
     `_heard_started` / `_heard_at` / `_heard_seen` / `direct_rx_at` 一起忘掉 ——
     **并且对"既没有洞也没有状态机"的 peer 也生效**（旧清理只遍历
     `links`/`sessions`，恰好漏掉这一种，也就是本条的现场）。
  * 结果：他重新进房但没点「开始连接」时，Host 没有目标 -> 房间级文案
    「等待对端连接…」（`PHASE_WAITING_FOR_PEER_CONNECT`），不再自己打起来。
  * 反例都保留：**信标还新鲜**（正在打洞但 `conn=idle`，stun 模式打洞先于
    游戏连接）时他**仍然是**目标 —— `direct_persist_test` 钉的正是这条。

## 第 7 条：起跑前的相对倒计时（0.1s 分辨率，状态机可见）

用户原话：「我说等待同步起降，假如实际步骤中存在这一步，即相对倒计时后开始
喷包，那我希望看到状态机有这个的显示，倒计时以毫秒走，例如 1.5s，1.2s 这样，
我觉得这样看起来很有科技感；假如不存在，那一定要检查好，因为例如 NAT3 打 4
甚至 4 打 4 对相对延迟同步喷包要求很高」。

### 排查结论：这一步**真实存在**，而且有两段
1. **等待同步起跑**（§E 的 `REPORT -> SYNC_START`）：先测完的一方等对端也测完，
   上限 `SYNC_START_WAIT_S = 12s`，**两边都测完才**交换 candidate、才允许开打
   （`room_direct._wait_synced_start`）。这是"端口错位/长时间打不通"的防线。
2. **等待发包起跑**（§E 的 `SYNC_GO`，也就是用户说的「同步起降」）：
   两边互发 `DIRECT_PUNCH startIn=1.5`，各自把**同一个相对提前量**换算成本地单调
   时刻（`ConnSession.arm(go_at)`），然后 `punch.await_punch_start()` 等到那一刻
   **才一起喷包** —— 这正是 NAT3打4 / 4打4 需要的"相对延迟同步"。
   `punch.py` 一直在按 0.25s 步长上报倒计时（`waiting X.Xs for the coordinated
   start`），只是**只进了日志和房间级那一行**。

### 为什么用户看不到
* 逐目标行（状态机）以前只有"进入本阶段的**累计秒**"（正计时）；
* `SYNC_GO -> PUNCH` 在同一个瞬间连着推进，所以那 1.5 秒里状态机已经写着
  「打洞中」，而不是「等待发包起跑」；
* 状态卡每 `UI_REFRESH_MS = 1000ms` 才重画一次，1.5 秒的倒计时最多只能看到一格。

### 本轮改法（**纯显示**，打洞算法一个字没动）
1. `ConnSession` 新增 `countdown_until` / `countdown_label` + `set_countdown()`
   / `clear_countdown()` / `countdown_left()` / `countdown_text()`；
   `row_text()` 有倒计时时显示 **0.1s 分辨率** 的倒计时，到点自动回到原文案。
   * 等待同步起跑 -> `等待同步起跑 9.8s · 对端正在测 NAT【名】N/C`
     （基线原文**永远在**了；对端在测什么作注脚，用户 09-28 要的信息没丢）；
   * 喷包前 -> `等待发包起跑 1.4s【名】N/C` -> 到点 `打洞中 PUNCH #0001…`；
   * 成员列表那种窄格子（`compact=True`）**不加**倒计时也不加秒数，版式不变。
2. `room_direct`：进入 `SYNC_START` 时挂 12s 倒计时；把
   `punch.py` 的 `step` 上报接到那条状态机上（`link.step` 回调里
   `waiting X.Xs` -> `等待发包起跑 X.Xs`，到点清掉）。换阶段自动作废上一段倒计时
   （`_advance_session`）。
3. `room_direct.state()` 新增 `countdown` 标志；界面 `_tick`（150ms 一拍）在它为真时
   **只重画**「逐目标行 + 打洞进度」两处文字（就地改 StringVar，不建控件、不抢焦点、
   不触发引擎动作），倒计时结束立刻回到 1 秒一拍。
4. 顺手修掉一处中英混排的文案：`等待同步起降：1.4s for the coordinated start`
   -> `等待发包起跑 1.4s`。

### 回归
`tests/port_kick_refresh_test.py` 第 7 段（11 项）钉住：基线原文 + 0.1s 倒计时都在、
倒计时真的在走（两次读差 ≥0.1s）、注脚保留、对端测完注脚消失、喷包前显示
「等待发包起跑 1.4s」而不是「打洞中」、到点回到 `PUNCH #0001`、换阶段自动清、
`compact` 版式不变、`state()["countdown"]` 开关、界面每 tick 的重画钩子。

## 本轮**没做**（诚实清单）
1. 子代理发现的两个小问题**未动**（不在用户这 7 条里，避免节外生枝）：
   `RoomSession.log()` 的协议审计行会被界面静默丢掉
   （`engine._room_emit` 发的是 `name="log"` 事件，`_handle_agent_event` 没有
   这个分支）；`tests/kick_test.py` 只用 ASCII 房间名。
2. **STUN 模式下"配好对但玩家没进世界"的会话回收**比中继模式慢
   （`PlayerCore._try_direct_carry` 里的 `wait_room_direct_path(timeout=None)`，
   要等房主侧隧道断开才回收）。第 4 条修的是**等待池**那半边（ROOM_FULL 的真正
   来源）；这一半是"慢"、不是"永久"，而且改动会碰到数据通路，按"只动列出的
   问题"原则没有动。
3. 真机（真实两端 NAT）仍需用户实测。

---

# 附六：v3.7.6 定稿（2026-09-30 —— 用户第三轮收尾 3 条）

> 用户原话：「基本我都满意了，就是最后测出三个问题，只改这三个，不要节外生枝
> 弄出 BUG，改完我们争取收尾这个大版本的开发工作」。
> 本轮只动这三条；下面每条都写清**根因 / 改动 / 证据**。

## 版本与交付物
* `APP_VERSION = 3.7.6` == `deploy/server/VERSION = 3.7.6`。
* 两道门禁：`check_deploy.py` **75/75**；`run_all.py` **57 套全绿**。
* 新套件 `tests/host_offline_owner_test.py`（33/0，run_all 第 57 套）。

## 第 1 条：host 离线（主动 / 被动）后其他玩家要断开、清状态、重算按钮

### 证据（用户 2026-09-30 的实测日志）
* host 在 01:50:30 前后死掉，另一个玩家（root）的界面 `ui.state` 停在
  **`ONLINE` 6 分 46 秒**（01:44:30 → 01:51:16 是他自己手动停的）；
* 玩家侧**从来没有**出现 `[reconnect] the host went offline …` /
  `waiting for the host to come online`（`_on_host_left()` 一次都没跑）；
* 玩家侧唯一收到的 `HOST_LEFT` 是**开服隧道 socket** 上的
  `ERROR HOST_LEFT`（`host_core.tunnel_loop` 当"暂时失败"重试掉了），
  **控制面（房间成员连接）那一行没人认领**。

### 根因
中继在"主机位持有者离开 / 掉线 / 被踢 / 超时"时会把全房游戏连接断开，
并在**每个成员的控制连接**上发一行 `HOST_LEFT <原因>`
（`room_members.disconnect_game_connections` 第 3 步）——已进世界的玩家那条
PLAYER socket 是纯字节，读不到文本，所以这一行是他们**唯一**的"原因"。
**客户端 `RoomSession._on_line` 里没有这个分支**：那一行被静默丢掉，
于是 `_on_host_left()`（停会话 / 清状态与隧道 / 「等待房主上线」/ 不自动重连）
从来没被调用过。

另外**房主主动"释放主机位"**那条路（`RELEASE_HOST`）走的是
`drop_all_sessions()` —— 只静默关 socket、不给可识别原因，玩家侧只能当成
普通掉线（自动重连、继续打洞），界面同样停在"运行中"。

### 改动
1. `client/mclink/core/room.py`：`_on_line` 新增分支，认
   `HOST_LEFT …` / `NO_HOST …`（**不带 ERROR 前缀**，和 CONN_MODE_CHANGED
   同一类）-> `_notify("host_left", code=…, reason=…)`。
2. `client/mclink/gui/app.py`：`_handle_core_event` 新增 `host_left` 分支 ->
   `_on_host_left()`（和 `room_lost(reason=HOST_LEFT)` 完全同一条路）；
   并且 `_on_host_left()` 做成**幂等**（中继可能两条路都通知：第二次只刷新
   「等待房主上线」提示，不再停一遍会话、不再弹窗、不再打日志）。
3. `server/room_members.py`：`RELEASE_HOST` 从 `drop_all_sessions()`
   改成 `disconnect_game_connections(room, reason="房主已停止开服…")` ——
   一样断全房游戏，但**带上可识别原因**，玩家侧才会转「等待房主上线」
   而不是无脑自动重连。房间与成员控制连接照旧保留，主机位空着等下一位。

### 回归
`tests/host_offline_owner_test.py`：控制行被认出来（HOST_LEFT/NO_HOST，
HOST_STATE 不误判）+ 界面停会话/清状态/等待房主/幂等 + **真中继端到端**：
房主认领后 `RELEASE_HOST`，另一个成员必须在成员控制连接上收到 `HOST_LEFT`。

## 第 2 条：一个人离开不许影响别人（这条是**我上一轮改坏的**）

### 证据
* Host 日志里每次有人离开都会出现
  `peer <cid> is gone from the room roster; forgetting that it ever started
  connecting` 与 `peer <cid> has started connecting`（每 0.5 秒一对）：
  01:35:45→01:35:53（8.6 秒）、01:38:16→01:38:23、02:01:37→02:01:56（19 秒）；
  整份日志 **68 条 "gone" / 78 条 "has started connecting"**。
* 离开的人（lubenwey）一走，root 的开服隧道收到 `HOST_LEFT`、房间连接断、
  会话重建，**与离开者无关的另一个玩家（muran）在 T+2.5~4.4 秒被
  `new connection cycle` 重打洞**，它的游戏在 ~24 秒后掉线。

### 根因（两半）
* **客户端（我上一轮引入的）**：`peek_direct()` 是**非破坏性**的，离开者临走前的
  `DIRECT_HIT` 还压在队列里；我上一轮给 `_forget_heard()` 加了
  "清掉 `_heard_seen` 已读计数"，于是"忘记"之后那几条旧行又被算成**新证据** ——
  "忘记 → 又看见 → 又变成目标 → 又忘记"每 0.5 秒循环一次，§E 空转，
  逐目标行也因此迟迟不消失（用户说的"状态机更新有点慢"）。
* **服务端（既有判据不严）**：`hold_member_seat()` 里"他名下还有老式
  `HOST <room>` 开服连接（`legacy is not None`）→ 断**全房**游戏（HOST_LEFT）"。
  主机位是**会换人**的：换过之后前任名下那条连接即便还挂着（僵尸），他也已经
  不是当前主机 —— 这时断全房，就是"一个玩家离开，其他人全部掉线、重新打洞"。

### 改动
1. `client/mclink/core/room_direct.py`：
   * `_note_heard_started()` **只认花名册（中继 `peers=`）里还在的人**发来的
     协调行；不在花名册 = 他已经不在这个房间，旧信标不算证据
     （花名册未知的老中继/假 state 不设这道门，行为不变）；
   * `_forget_heard()` **不再抹掉 `_heard_seen`**（"这个 sender 我见过几条"），
     否则旧行又会变成"新行"；`direct_rx_at`（到达时刻）照旧清掉。
2. `server/room_members.py`：`hold_member_seat()` 里改成
   `legacy is not None and (room.host_member is member or room.host_member is None)`
   才走"全房 HOST_LEFT"，否则只 `drop_member_sessions()` 断**他自己**。
   * 他**确实**是当前主机 -> 全房（既有行为，一字未改）；
   * 老式房间没有成员级主机位、游戏就是靠他这条开服连接在跑 -> 全房（一字未改）。

### 回归
同一个套件里：离开的人不再被旧信标重新变成目标、日志不再 ping-pong
（"gone" 只记一次、"has started connecting" 0 次）、**只动离开那一个人**
（另一个人的目标/证据一个都不动）、主机位换人后前任离开只断自己
（真主机 / 老式房间仍然全房 HOST_LEFT）。

## 第 3 条：房主身份显示

### 根因
1. `OK JOIN` 回包里的 `owner=` 取的是 **`add_member()` 之前**算出来的局部变量
   （"CREATE 且房间没有 owner"）。可是 `add_member()` 里有一条**本人重连**的
   合法路径：座位宽限期内本人回来 -> 所有权**交回新连接**
   （`member.is_owner = True`，规则 6 的"连接断了 ≠ 离开房间"）。
   回包若还用旧值，房主重连后拿到 `owner=0` -> 界面显示 **「玩家·主机」**
   （用户实测："在房主首次释放 host 时身份从房主变成玩家了"），
   而中继其实一直认他是房主。
2. `HOST_STATE` **从来没有** `owner_id=` 这个 token —— 可客户端
   `RoomSession` 早就在解析它、`build_member_rows()` 也早就用它精确标出
   「房主」那一行。结果：除了房主自己，**谁都看不到「房主」这个身份**
   （成员列表里房主只显示成「主机 / 玩家」）。
3. 成员列表「连接状态」那一格以前**只认中继的 `conn=`**：STUN 模式下游戏走
   **P2P 通道**，中继那条会话结束之后 `conn=` 就变 `idle` —— 可我们和他之间的
   直连洞还在搬数据，用户看到的就是"已经连接的玩家被打印为未连接"。

### 改动
1. `server/relay_server.py`：`OK JOIN` 回包的 `owner=` 改读
   **权威值** `member.is_owner`（`add_member()` 跑完之后它就是事实）；
   审计事件 `room.member.join` 的 `owner=` 同源。
   **所有权规则一个字没改**：仍然是"CREATE 且没有 owner 才算 owner"，
   仍然不可继承、不可转移（规则 6 §1 + `relay_baseline_test` 钉着的
   "创建者连接被顶替 -> 房间销毁"照旧全绿）。
2. `server/room_members.py`：`HOST_STATE` 补上 `owner_id=<cid>`
   （没有房主时 `-`），ADDITIVE —— 老客户端按空格分词直接忽略。
3. `client/mclink/gui/app.py`：`_member_status_text()` 加一条判据 ——
   中继说 `idle` **但** `peer_direct`（我们和他之间已打通的直连洞还在保活）
   为真 -> 显示「已连接」。**纯显示**：`conn=` 的口径、连接逻辑一个字没动。

### 回归
`owner=` 的取值来源（源码级钉死）+ `HOST_STATE` 带 `owner_id=`（真中继）+
成员列表标出「房主」/「房主·主机」+ idle 但有直连洞 -> 「已连接」、
没有直连洞 -> 仍然「未连接」（口径没放宽）。

## 本轮**没做**（诚实清单）
1. 中继的 `conn=` 口径**没改**（仍然只认中继会话 + 开服连接）：目前
   "有洞但没进世界"在**广播里**仍然是 `idle`，只是成员列表那一格按"洞还活着"
   显示「已连接」。要让广播本身也反映 P2P 状态，需要一个**客户端上报隧道状态**
   的新协议字段 —— 那超出"只改这三个"的范围，等用户确认。
2. 1.log 里"root 自己的开服隧道收到 HOST_LEFT"这一条，是 root **自己**
   停止再开始开服（01:52:54 那次）导致的，属于既有语义（换开服控制连接 =
   旧隧道作废），没有改。中继侧日志本轮没有拿到，无法进一步交叉验证。
3. 真机（真实两端 NAT）仍需用户实测。
