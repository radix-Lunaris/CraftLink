"""
MC-Link -- run every regression suite and print one summary.

Tests are executed as child processes (stdio inherited) so their own server
subprocesses and ports stay isolated, and so a crash in one suite cannot hide
the results of the others.

Usage:
    python tests/run_all.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    ("config_contract_test.py", "配置契约测试", "cfg.* 引用 / store 接口一致"),
    ("units_test.py", "格式化 / 测速单元测试", "纯函数，最快"),
    ("e2e_relay_test.py", "中继数据通路", "冻结的 RAW TCP 转发回归"),
    ("engine_session_test.py", "引擎与会话层", "GUI 使用的代码路径"),
    ("latency_test.py", "延迟与实时速度", "PING/PONG + 测速"),
    ("gui_smoke_test.py", "界面冒烟测试", "tkinter 界面 + 账号卡片"),
    ("gui_appearance_test.py", "界面外观（语言/主题）", "只改观感：控件一个不少 + 房间入口回归"),
    ("gui_i18n_test.py", "语言覆盖硬测试", "切英/日文后界面不许残留中文 + 日志正文必须是英文"),
    ("room_name_host_test.py", "房间名 / 主机位置", "真中继：中文房间名、重名、host 位置不撒手"),
    ("gui_room_ghost_test.py", "幽灵房 / 按钮状态", "真中继+真界面：管理关房后自动收拾干净"),
    ("v2_control_test.py", "V2 服务端控制面", "登录、删除规则、管理 API"),
    ("session_lifecycle_test.py", "会话生命周期（幽灵会话）", "真中继：配对≠进世界 + 超时回收名额"),
    ("db_boot_timeout_test.py", "数据库卡死也不能拖住启动", "真中继+假 MySQL：连不上/查不动都必须照常监听"),
    ("room_recovery_test.py", "人数锁定 / 掉线复位 / 身份", "隧道跟房间容量走 + 选择器不决定身份"),
    ("pairing_stale_tunnel_test.py", "死隧道不许喂给玩家", "中继配对：僵尸隧道直接扔掉，不改变 FIFO"),
    ("server_logs_test.py", "服务端日志 / 导出", "真中继：写文件 + 面板可下载"),
    ("v2_client_test.py", "V2 客户端控制面", "客户端登录、房间列表、删房"),
    ("room_settings_test.py", "房间人数上限 / 关房", "面板改人数立刻生效 + 强制关房"),
    # 3.0.9：踢出一个成员（不是删房）。真中继 + 真成员连接：权限矩阵
    # （所有者 / 当前主机 / 普通人）、KICK_SELF / KICK_NO_SUCH_MEMBER、
    # 被踢者收到 KICKED 并断开而其余人不受影响、踢所有者 = 房间解散，
    # 以及面板管理 API `POST /rooms/<room>/kick`（token 保护 + 可踢所有者）。
    # host 会换人：权限按发 KICK 那一刻的当前 host 判（认领/释放/掉线/重连
    # 顶替/老式 HOST 直连都立刻生效，卸任的旧 host 立刻 FORBIDDEN）。
    ("kick_test.py", "踢人（踢出单个成员）", "真中继：权限矩阵 + host 换人即时生效 + KICKED + 管理 API"),
    # 3.0.9：踢人的**客户端半边** —— 成员列表数据（未知就留空）、踢人按钮
    # 可见性、KICK/KICKED 的客户端解析、真中继 + 真 RoomSession 的权限矩阵、
    # 被踢后的本地拆除，以及「一人一行」的直连行（来一个多一行/走一个少一行）。
    ("client_kick_roster_test.py", "房间成员列表 / 踢人（客户端）", "纯数据 + 真中继真客户端 + 真界面：未知处理/按钮矩阵/KICKED 拆除/多洞行"),
    # 3.0.9：成员【真名】与【两列延迟】。中继广播 names=（登录名 / 游客xxxxxx）
    # 和 rtts=（它自己量的每成员中转延迟），客户端 ADDITIVE 解析、老中继回退到
    # client_id；对端延迟（玩家↔主机）适用才显示真实 RTT，不适用写 N/C —— 那个
    # 数字只来自隧道本来就有的 T_PING/T_PONG keepalive 往返（协议一步没动）。
    ("member_name_rtt_test.py", "成员真名 / 每成员中转延迟 / 对端延迟", "纯解析 + 纯口径 + 真界面：真名/游客/老中继回退/per-member RTT/N-C/换 host 矩阵"),
    # 3.0.9：**服务端**的成员身份 + 中转 RTT + 版本门开发开关。
    #   身份：登录名原样；免登录/匿名连接由中继分配「游客xxxxxx」（只活在这条
    #         连接上）；空 username / client_id 一律兜底，绝不出现空 key。
    #   RTT：中继主动发 `PING <token>`、成员回 `PONG <token>`，中继量每个成员
    #        到自己的往返，放进 HOST_STATE 的 `rtts=`（整数毫秒）；不回/超时的
    #        成员整条不出现（绝不编数字）。
    #   开关：面板「允许不同版本客户端连接」是**开发调试**项（默认关闭）。
    #         默认拒 / 打开放行 / 再关又拒，中继运行时读取、不重启。
    ("member_identity_rtt_test.py", "成员身份 / RTT / 版本门开发开关（服务端）", "真中继：游客名 + names=/rtts=/you= + 开关默认拒/开/再拒"),
    # 2026-09-27 第二轮问题 4/5/7 的**中继侧**（与上面 room_identity_test.py 的
    # 客户端半边配套）：
    #   释放：成员离开/掉线/被踢/超时后立刻清主机位并广播 HOST_STATE（别人不
    #         用断开重连就能认领），每次释放记唯一一行
    #         `[HOST RELEASE] room=.. by=.. reason=leave|drop|kick|timeout`。
    #         房主离开：所有游戏连接收 HOST_LEFT 后被断开，房间保留可再认领。
    #   同源：host= / host_name= / host_now 全部出自 host_state() 一个来源 ——
    #         "有主机但没有名字"这种自相矛盾的一对物理上不可能出现；成员认领
    #         和老式 `HOST <room>` 控制连接两条路都覆盖。
    #   身份：游客身份是**登录会话**级的（不绑设备、不落库）——中继发一条
    #         临时 token（OK JOIN 的 `guest=`），退房/被踢/房间被关都不销毁，
    #         账号退出（LOGOUT / 换账号 / 重连丢登录态）才清；
    #         `IDENTITY <username> <token> ver=<版本>` 更新显示名并立刻广播，
    #         token 无效回 ERROR IDENTITY_*，版本不一致被版本门拒。
    ("host_release_identity_test.py", "主机位释放 / 同源 host 字段 / IDENTITY（服务端）", "真中继：掉线即释放+广播+四种 reason 日志 + host=与host_name=同源 + 游客身份一次性/不残留 + IDENTITY 成功/token无效/版本门"),
    # 2026-09-27 设计基准（docs/DESIGN-BASELINE-20260927.md，AGENTS.md 规则 6）的
    # **防回归网**：这些规则多数是项目早期就有、用户早就验收过的，后来被改坏了。
    #   §A 身份：Owner 不可转移/不可继承 —— 创建者的连接被顶替 = 本人已不在，
    #      房间走唯一销毁路径；一个房间最多一个 Host（搬运 host 位置时清残留）。
    #   §B 门禁：没人认领 Host -> ERROR NO_HOST；认领了但没开服 ->
    #      ERROR HOST_NOT_READY（两个码可区分）；Host 开服后 PLAYER 才进配对。
    #   §D/§E：HOST_STATE 新增 started=<cid>,<cid>（谁已开始连接）——
    #      player 的默认打洞目标只认当前 Host（不能打向创建房间的 Owner）。
    #   §A/§D：只有 Owner 能切连接模式；切换瞬间清掉旧配对/隧道并给
    #      CONN_MODE_CHANGED 原因码。
    #   §B/§12：Host 成员心跳超时 = 真正离开（房间保留可再认领）；开服控制
    #      连接静默掉线只拆连接，带 Owner 的房间不许被 host_watchdog 销毁。
    ("relay_baseline_test.py", "中继永久基准（Owner/Host/门禁/started/换模式）", "真中继+假成员：Owner不可转移 + NO_HOST/HOST_NOT_READY + started= + 只Owner能切模式并清旧通路 + Host超时释放 + host_watchdog不销毁Owner房间"),
    # 2026-09-28 第三轮：用户报的 P0「热切换不能用了，连不上，隧道好像没有重建」。
    #   上一轮已经做到"切换瞬间清 sessions + 等待池，先清理后广播
    #   CONN_MODE_CHANGED"；这一套钉的是**清完之后客户端还能不能重建**：
    #     A. 同一个人重连开服 -> 立刻顶替旧的开服控制连接（不再
    #        ROOM_ALREADY_HAS_HOST 让客户端干等 3s 退避）；不同的人仍然被拒。
    #     B. 真中继 relay -> stun -> relay，每一跳都按真实客户端方式重建
    #        （新 HOST / 新 HOST_TUNNEL / 新 PLAYER -> START -> READY ->
    #        CONNECTED），并断言清理后的 HOST_STATE 自洽。
    #     C. 真客户端核心（MCLinkEngine = GUI 用的代码路径）走完整一圈，
    #        最后真的用一条 Minecraft 连接把字节跑通。
    ("conn_mode_hotswitch_test.py", "连接模式热切换（清完必须还能重建）", "真中继+真客户端核心：同一个人重连开服立刻顶替 + relay→stun→relay 每跳重新配对 + 最后游戏字节真的跑通"),
    # 2026-09-28 第三轮：用户要求"把连接状态也包进管理广播 TCP"（所有状态迅速
    # 同步）。HOST_STATE 新增 `conn=<cid>:<state>,...`（PROTOCOL §8.5），
    # 每次变化立刻推送（不等心跳）：Host 开服 -> connected；PLAYER 接入 ->
    # waiting；配对 -> pairing；进世界 -> connected；PLAYER socket 断开
    # （含还在等待池里就掉线，由 0.2s 的 at_eof 看护发现）-> 立刻 idle；
    # 被踢 -> 整条消失。另含 `STARTED <1|0>`（§8.6）——「已开始连接」= 用户
    # 点过开始连接，与连接现算取并集，且不放宽 §B 门禁。
    ("member_conn_state_test.py", "每成员连接状态立刻同步（conn= / STARTED）", "真中继：conn= 全表 + 每种变化 2s 内推送 + 等待池掉线看护 + STARTED 显式上报 + §B 门禁不放宽 + member_list 带 conn"),
    ("panel_static_test.py", "PHP 面板静态检查", "括号/函数/action/CSS/require"),
    # V3: the STUN stack is now MCLanP2P's verified implementation, not
    # CraftLink's earlier half-finished one. This suite pins HIS behaviour
    # (the classifier's counter-intuitive rules), the async bridge that keeps
    # his blocking probes off our event loop, and the two-port server -- with
    # a real child process and real UDP, because "both ports answer" cannot be
    # faked.
    ("stun_stack_test.py", "STUN 栈（移植版）", "他的分类器 + 双端口服务端 + 异步桥不阻循环"),
    # V3 direct path: the punching engine ported from MCLanP2P. The handshakes
    # are exercised for real on loopback (two peers, one event loop), because
    # "which port does this side send from" is the whole point and a fake
    # socket cannot show it.
    ("punch_test.py", "打洞引擎（直连）", "策略矩阵 + 端口窗口 + 回环真打通 + learn-then-reply"),
    # V3 direct coordination: real relay subprocess, two real member
    # connections. Proves the relay only FORWARDS candidates (verbatim,
    # including fields it does not know) and that old keywords still work.
    ("direct_cand_test.py", "直连协调控制面", "真中继：候选转发逐字不变 + 入站探测 + 同步起跑 + 旧功能回归"),
    ("direct_e2e_test.py", "直连端到端（游戏走直连）", "真中继+真引擎：点开始连接->打洞->长连接先于游戏->字节真的走直连"),
    ("direct_reuse_test.py", "直连通道复用（连两次都走直连）", "真中继+真引擎：列表探测+真进世界 两条连接都必须走直连，中继零字节"),
    # 3.0.3: the user's multiplayer rules, pinned one by one -- host has ONE
    # long-lived tunnel per player; a late joiner gets their own punch without
    # disturbing existing links; a player dropping only kills their own link
    # (20s-silence watchdog, same as his LINK_DEAD_S); host stopping closes
    # every channel; and the relay never carries game bytes in STUN mode.
    ("direct_multi_test.py", "多人直连（host×每玩家一条长连接）", "真中继+三真引擎：晚到玩家单独打洞不扰已有 + 一人断只断自己 + host断全关"),
    # 2026-09-28（用户："洞打通马上就断"）：打通是**成功**终态，不许被下一轮
    # 的 `ConnSession.restart()` 当成"要重开一次连接"而抹掉。真中继 + 真引擎 +
    # 真 Minecraft 连接：打通后连续 8 秒（跨两个 3s 重试轮）断言阶段仍是
    # connected、generation 不变、没有新的 `new connection cycle`、字节持续双向流动；
    # 另有一份不依赖中继的离线复现。
    ("direct_persist_test.py", "打通后保持（不许下一轮 restart()）", "真中继+真引擎：打通后 8s 阶段/轮次/隧道都不许变 + 字节持续双向流动 + 离线复现"),
    # 2026-09-28（用户批准）：上一轮刻意留下的六个隐患，逐条修复 + 逐条回归。
    #   1. needs_new_cycle 把成功终态也当成"要重开"（语义修复 + cycle_action API）；
    #   2. start() 幂等闸门只看"引用在不在"（洞死了就再也重打不了）；
    #   3. _sweep_links 拿打洞目标表当花名册（started= 抖一下拆掉活着的洞）；
    #   4. _open_tunnel 两次 HELLO 失败就扔掉已打通的 socket（新映射 = 打通又断）；
    #   5. 隧道一直没起来时永远不判死（给它一条真正的截止时间）；
    #   6. flow_summary 在读路径上建状态（改成纯函数）。
    ("direct_hardening_test.py", "打洞加固（六个隐患）", "纯逻辑 + 离线真 _run()：成功/失败终态分离 + 洞死可重打 + 花名册判死 + 同 socket 重试 + 隧道截止时间 + summary 只读"),
    # V3.0.1: the carrier is now the reference project's udptunnel (731 lines,
    # verified on a real NAT3/NAT4 network) instead of a self-written reliable
    # stream. This suite pins HIS constants, then exercises the real thing on
    # loopback: handshake, 200 KB reassembly, a lossy/reordering proxy, and TWO
    # simultaneous Minecraft connections multiplexed over ONE tunnel by sid.
    #
    # Loopback proves the mechanism was ported intact. It does NOT prove the
    # user's network will connect -- see HANDOFF-TO-NEXT-SESSION.md §3.3.
    ("udptunnel_test.py", "直连承载（他的 udptunnel 移植版）", "常量逐字一致 + 回环真握手 + 分片重组 + 丢包乱序 + 一条洞两条流"),
    # 2026-09-27 日志审计（docs/LOG-AUDIT-20260927.md）的回归网。
    #   P0-1 host_core 的 tunnel_reader NameError（洞通了游戏秒掉线）
    #   P0-3 重建必须带 role_override（房主不许被重建成 Player）
    #   P1-5 打洞快路（复用房间 NAT 画像 / 暖场不等 STUN 答案）
    #   P1-6 relay 模式下不许出现"打洞中"的洞行
    #   P2-7 进程级流量账本（直连也计数 / 离开房间不清零 / 速度永远有）
    #   P2-8 亚毫秒 RTT 不许显示成 0；T_PONG 按序号核对
    ("punch_traffic_test.py", "直连承载 / 打洞快路 / 流量账本", "假承载记录 kwargs + 假 STUN 计数快路 + 进程级账本 + T_PONG 序号"),
    # 同一份审计里界面侧的三条 P0/P1：免登录游客点开始连接不许 AttributeError、
    # 重建不许丢主机身份、非房主侧房间模式变了要自动重建（带防抖）。
    # 真界面（真 tk 控件） + 假 Engine：测的是界面真正跑的代码路径。
    ("gui_room_host_session_test.py", "免登录 / 主机身份 / 会话模式自动重建（界面侧）", "真界面+假引擎：guest 不崩且落日志 + 重建 role=host + 每秒同步只重建一次"),
    # 2026-09-27 第二轮问题集（docs/ISSUES-20260927.md）：
    #   问题 5 —— 人在房间里登录 / 改名之后，中继名册里那颗名字必须跟着变
    #   （身份绑在 client_id 上），房间不重建、不断线。新增 ADDITIVE 控制消息
    #   `IDENTITY <username> <token> ver=<版本>`（ver= 在整行最后）。
    #   真中继子进程 + 真 TCP：证明两边格式一致、房间里其他人也看得见改名；
    #   另有纯逻辑（OK/ERROR/老中继超时）和真界面（只在房间里发、同一身份只发一次）。
    ("room_identity_test.py", "房间里改名 / 身份绑设备（IDENTITY）", "真中继：OK IDENTITY 改名全员可见且不断线 + 错码 + 客户端解析 + 界面防抖"),
    # 2026-09-27 第二轮（用户批准）：会话**自动重连**框架 + 房主离线（HOST_LEFT）
    # + 单人房间的「等待对端用户加入…」文案 BUG。
    #   退避 0.5→1→2→4→8→15s、总窗口 60s、终态永不重试、同一时刻只飞一个重建；
    #   房主离线 -> 停会话 + 弹窗 + 「等待房主上线」（不重试）；
    #   单人文案不显示秒数、有对端未公告时才给"第 N 秒"。
    #   真界面 + 假 Engine：测状态机与文案；打洞那一半由 punch_test / direct_* 负责。
    ("auto_reconnect_test.py", "自动重连 / 房主离线 / 单人文案", "真界面+假引擎：退避与终态 + 房主离线停会话弹窗 + 等待房主上线 + 文案不倒计时"),
    # V5.0 AUTO（Phase 1，docs/AUTO-SCHEDULING-PLAN.md）：
    #   中继侧：auto 是合法模式，但要人人报过 caps=auto —— auto 房间拒绝
    #           不懂 auto 的客户端，房里有跑不了的成员时拒绝切换，坏模式仍
    #           然 BAD_MODE；
    #   客户端侧：三态选路 relay->off / stun->only / auto->prefer，auto 的
    #           provider 有界等待（AUTO_DIRECT_WAIT_S），等不到返回 None 让
    #           这条连接走中继。真中继（版本门开着，握手带 ver= + caps=）。
    ("auto_schedule_test.py", "AUTO 调度（Phase 1）", "真中继：auto 模式 + caps 能力门（拒绝/切换/坏模式）+ 客户端三态选路与有界等待"),
    # 3.0.8：版本门。用户的规则是"版本不是最新不给用" —— 客户端和中继必须
    # 同版本，不一致时回 ERROR VERSION_MISMATCH（两边版本都摊开）并断开。
    # 真中继子进程，门开着/门关着两种环境都跑一遍。
    ("version_gate_test.py", "版本门（客户端与服务端同版本）", "真中继：不一致拒/一致过/只读不拦/关着照旧"),
    # 3.0.8：用户报的"断开/关房/换模式之后端口被占用、再也连不上"。根因是
    # `stop_session()` 把拆除超时**吞掉**了（旧监听 socket 还开着，引用却没了）。
    # 这一套把那条路径连同"拆除卡住"的极端情况一起钉死。
    ("reconnect_after_teardown_test.py", "断开之后必须还能重连（端口必须真的释放）", "真中继：停掉立刻重连×4/关房重建/换模式/拆除卡住也要放端口"),
    # 3.0.8：朋友报的"每刷新一次玩家数就涨"。房间里的「世界里的人数」数是
    # **会话**，而 Minecraft 刷新一次列表就是一次完整连接 —— 所以这里连做
    # 6 轮刷新，每轮断开后数字都必须回落到基线，不能一路上涨。
    ("refresh_count_test.py", "刷新计数（列表刷新不许把人数刷上去）", "真中继：6 轮刷新/会话每次都要回收/峰值确实动过"),
    ("reset_password_test.py", "账号找回工具", "离线：密码生成/角色/重置"),
    ("credentials_test.py", "记住密码", "离线：加密存储/版本失效/篡改检测"),
    # V3: the read-only diagnostics card. Real STUN responder in a child
    # process, so "we actually received a packet" is proven rather than
    # assumed -- and so the probe is exercised without the test's own event
    # loop being the thing that answers.
    ("nat_diag_test.py", "网络诊断（只读）", "真 STUN 子进程：字段/延迟/不阻塞引擎 loop + 房间级持续检测"),
    # Lives in scripts/ because it is a deployment gate, not a code test. It
    # is in the suite because the failure it guards against is silent and
    # expensive: uploading a compose file with placeholder credentials, or a
    # Dockerfile missing a COPY line, breaks production without any test
    # noticing.
    ("../scripts/check_deploy.py", "部署包预检", "凭据非占位符 / COPY 完整 / 与源码一致"),
    # 2026-09-28：设计基准 `docs/DESIGN-BASELINE-20260927.md`（用户逐条确认，
    # 已写进 AGENTS.md 规则 6）的**打洞流程回归网**。用户本轮 8 条 BUG 里有 5 条
    # 是同一个根因：打洞没有被建模成"每个 Host↔Player 一条独立、双端同步的
    # 状态机"。新的 `core/punch_flow.py` 就是它，这一套把每一条规则钉死：
    #   §E 严格顺序（确认 Host→确认目标→NAT→入站→上报→**等待同步起跑**→同步
    #      candidate→双方确认→同步起降→Punch）
    #   §F 每连接独立 Timer（终态立即停止/清零/释放，重连从 0）
    #   §F 双端 attempt/batch/序号同步（错位 -> 作废 attempt -> 重新同步）
    #   §D 目标只认 Host（BUG 1 的复现）/ Host 只打已开始的 Player
    #   §D 新人加入不扰动已打通 + 换模式严格重建全部通路
    #   §G UI 三层（通路 n/m / 打洞进度 n/m / 逐目标行）+ N/C 口径 + 三语言
    #   §C 被动掉线两类判定（被踢 = 退出 / 真闪断 = 自动重建）
    #   §C 真中继端到端：拔掉中继 -> 老会话清理干净、端口释放、角色不变
    # 纯逻辑部分不需要网络；真中继那几条自己起子进程。
    ("punch_flow_test.py", "打洞流程基准（§C/§D/§E/§F/§G）",
     "阶段顺序 / 同步起跑 / 每连接 Timer 与双端同步 / 目标只认 Host / 三层 UI 与 N/C / 被动掉线两类 / 真中继拔线清理"),
    # 2026-09-28 第二轮（用户实测 v3.4.0 的 8 条）的**客户端半边**回归网：
    #   1. 逐目标行 = 对端真名 + 「第 N 次」+ 打通的地址:端口（`【A】` 只是
    #      设计文档的占位示例，不许当计数/名字）；
    #   2. 状态迁移表（阶段 -> 下一阶段 -> 触发条件）与代码逐条一致；跳步/回退
    #      被拒；手动中断后重连 -> 每一格重新走一遍（restart(), generation+1）；
    #   3. 本机测量阶段两端都可见（测量 NAT 类型中… / 等待对端测量 NAT 类型…）；
    #   4. host 端「等待对端连接…」（房内有人但没开始连接）；
    #   5. `conn=` 广播驱动成员「连接状态」（老中继回退旧行为）+ STARTED 1/0；
    #   6. P0 热切换：**真中继 + 三方** 房主切 relay->stun，三方各自重建、
    #      再次打通、字节逐字节正确；另有界面侧"重建按调用那一刻的模式"
    #      （不受界面每秒刷新影响 —— 这是"隧道好像没有重建"的根因）。
    ("mode_switch_rebuild_test.py", "热切换重建 / 状态迁移 / 逐目标行文案",
     "迁移表==代码 + 手动重连逐格重走 + 真名/第N次/地址:端口 + 本机测量两端可见 + host等待对端连接 + conn广播状态 + STARTED + 真中继三方热切换再打通"),
    # 2026-09-29 第五轮（用户：两端状态机显示实时同步 + 自动重连 + 连接状态传播 +
    # 显示升级）：
    #   1. 两端共用一套阶段枚举（`punch_flow.Stage`），每一步进阶段即上报
    #      `DIRECT_HIT stage=<阶段>`，显示只推进到"两人都到过"的那一格，
    #      对端没到显示「等待对端…」（**只让显示等，不让真实打洞等**）；
    #   2. P0：一端已直连 -> 立刻上报 `stage=connected`，另一端在**同一个已打通
    #      socket** 上收敛（保活+隧道）并**停止重试**（判据硬：没链路不收敛）；
    #   3. 「连接状态 ≠ 身份/职位」：`conn=`（连接状态）与 `host=`/`host_claimed`
    #      （身份）分成两套读法；Host 只是停止连接 -> 主机位保留、别人不许认领；
    #   4. Owner/Host/Player 是**组合**角色（房主·主机 / 房主·玩家 / 玩家·主机）；
    #   5. 每对 Host↔Player 独立 + 每轮重新确认目标（host 会漂移）；
    #   6. 房间模式**未知**绝不等于 relay（不许自己拆直连/回退中转），
    #      模式只由 Owner 的显式操作改变；
    #   7. 热切换：空闲 = 只换模式；有连接 = 严格重建 + 自动重连。
    ("punch_display_sync_test.py", "显示同步 / 收敛 / 身份-连接分离",
     "共享阶段枚举 + 等对端显示 + PUNCH #NNNN + 三语 + 一端已直连->另一端收敛停重试 + conn≠身份 + 组合角色 + 每对独立与重新确认目标 + 模式未知≠relay + 热切换重建判定"),
    # 2026-09-29（服务端侧，用户 P0）：**房间模式不许被内部路径改写** +
    # 重连判定 + host 漂移 + 热切换语义。
    #   1. 「模式自己跳回 relay」的根因：迟到/走错房间的 PLAYER / HOST_TUNNEL
    #      握手用 get_or_create_room **造**出一个默认 relay 的新房间，以及
    #      `mode=` 的 `getattr(..., "relay")` 兜底。现在：握手只读查找、
    #      `conn_mode` 只有一个写入点（Owner 的 CONNMODE）、读口径绝不凭空造
    #      relay；房间销毁后由**同一个 Owner** 重建才恢复（mode_reason=
    #      restored），换了人不继承。
    #   2. `SESSION __mclink__ ...`（PROTOCOL §8.10）：闪断（token 有效、身份
    #      还在）-> 自动重连；被踢/退房/超时/房间没了/token 失效 -> 不自动
    #      重连、必要时重新登录。
    #   3. host 漂移：释放 -> 认领 -> 新 host_id 上真的 START/READY/CONNECTED。
    #   4. 换模式广播带一次性 `was_live=0|1`（切换前的真实连接状态）。
    #   5. 连接 != 身份/职位：Host 停服保留主机位（别人 CLAIM_HOST 被拒、
    #      PLAYER 得到 HOST_NOT_READY 而不是 NO_HOST）；只有离开房间（含
    #      token 失效/超时清退）或主动释放才释放；座位宽限期让"连接抖动"
    #      能拿回职位。Owner/Host/Player 三种组合都验证。
    ("mode_identity_reconnect_test.py", "房间模式不被内部改写 / 重连判定 / 身份-连接分离",
     "真中继：迟到握手不造房 + mode 只由 Owner CONNMODE 改（销毁重建由同 Owner restored）+ SESSION 探测矩阵 + host 漂移后新 host 真配对 + was_live + 停服保位/释放/踢/超时/token失效 + 组合角色 + 参数冻结"),
    # 2026-09-29（用户原话）：「重连后检查账号 token 是否还有效、是否被服务器
    # 踢掉：被踢掉了就按退出房间处理；如果没有（真就是闪了一小下的那种）那就
    # 自动重建」—— 上面那一套是**服务端**的 SESSION 探测矩阵，这一套是**客户端
    # 半边**（PROTOCOL §8.10 / §8.9 / §8.11）：
    #   1. `SESSION __mclink__ <cid> <user> <token>`：真 socket 探只读行 + 完整
    #      reason 判定表 + 老中继 UNKNOWN_ROLE / 超时 / 不可达一律退回旧行为；
    #      `RoomSession._explain_disconnect` 按探活结论发 room_lost（闪断）或
    #      session_ended（退出，不重连），Host 与 Player 同一条规则；界面侧
    #      session_ended 清房间 + 正确提示，relogin=1 时提示重新登录（只弹一次）。
    #   2. `mode_reason=owner|restored|default` 容错解析 + 归因日志 + 状态快照；
    #      `restored`（同一 Owner 重建恢复）不许被当成"有人在乱改模式"。
    #   3. `was_live=0|1`：成员控制连接那条之外的**等待池**路径
    #      （`ERROR CONN_MODE_CHANGED … was_live=`，PLAYER / HOST_TUNNEL）也解析
    #      并交给同一个消费者（真 PLAYER / 真 HOST_TUNNEL 握手对外）。
    #   4. `HOST_TAKEN`：认领被拒后 host 位仍是"有人"（is_host 清掉，身份字段
    #      一个不动），界面照搬服务器的中文原因、认领按钮不许因此亮起来。
    ("session_probe_reconnect_test.py", "重连探活 / 模式归因 / was_live / HOST_TAKEN",
     "纯解析+判定表 + 真 socket SESSION 探活（短超时/老中继/不可达）+ 核心 host/player 同规则 + 界面 session_ended 不重连与重登录 + mode_reason 归因与 restored 不误判 + 真 PLAYER/HOST_TUNNEL 等待池吃 was_live + HOST_TAKEN 身份仍在"),
    # 2026-09-29（用户原话）：「未来的 Rebuild 无需加那个 -v，可以直接打印详细
    # 的日志，每一步的日志状态，且是实时打印……它这个一条一条地给我实时打印」。
    # 这一套用真子进程量**实时性**（第一行必须在第二行产生之前就到屏幕上），
    # 并检查完整落盘 / 失败上下文 / -v 更啰嗦 / --strict 不变，以及
    # `deploy/HANDOVER.md` 由 sync-deploy 生成并进 BUILD.txt + README 清单。
    ("deploy_scripts_test.py", "mcr 实时日志 / 交接文档进交付包",
     "抠出真 run_step 跑真子进程：实时逐行 + 完整进 rebuild.log + 失败上下文 + -v 打印被过滤行 + --strict 不变 + deploy/HANDOVER.md 生成/一致/进指纹与清单"),
    # 2026-09-29（用户第二轮实测的 7 条问题）—— 离线回归网，不占端口：
    #   1  idle 的对端不许进打洞目标（`conn=idle` 一票否决 + 陈旧状态机清理）；
    #   2  `peer_ready` 必须认"已被隧道接手的洞"（否则打通 3 秒后又开新 cycle /
    #      界面退回「等待同步起跑」）+ 服务端 `OK JOIN` 回包之前不许广播；
    #   3  热切换"空闲态的保持"（`was_live=1` 不替空闲成员决定）+ 自动重连窗口内
    #      按钮不许被释放；
    #   4  语言 / 主题下拉框的滚轮不许改选项；
    #   5  人数 == 客户端能画出来的行数（client_id 去重 + 成员的游戏会话只算一次）；
    #   7  踢人立刻结束座位宽限（不许替被踢的人留座继续打洞）+ 打洞分母跟着降。
    ("hotswitch_roster_state_test.py", "热切换 / 花名册 / 陈旧状态机（7 条实测问题）",
     "替身 + 真模块：idle 不是目标 + 陈旧 session 清理 + 打洞分母 + peer_ready 认隧道 + 按钮忙碌态 + 下拉框滚轮 + 人数去重 + 游戏会话不重复计数 + ready 闸门 + 踢人取消宽限 + 凭证判同人"),
    # 2026-09-29（用户第二轮第 3 条的补充）：中继 -> STUN 热切换之后**必须真的
    # 开始打洞**，而且第几次切都得行。
    #   A  打洞循环的**活性**：循环没了（热切换拆旧通路 / `room_current()` 短暂
    #      为 None / 旧协程没被取消掉）-> `start()` 必须能重新拉起来；旧闸门
    #      `_established and transport_ready` 会把"重启循环"和"重打这条洞"混成
    #      一件事，于是洞还在时打洞永远起不来（用户：没有开启打洞流程）；
    #   A2 每一轮循环带 token：旧循环自己退出，不许两条 `_run()` 同时跑；
    #   B  界面：重建成功 = 会话又活了 -> `running` 必须补回 True（否则
    #      `_sync_room_nat` 会把刚起来的打洞每秒关一次）；
    #   C  文案：确认框 / 房主提示不许再说"所有人掉线、需要手动重连"（三语）。
    ("hotswitch_stun_resume_test.py", "中继↔STUN 热切换后打洞必须起来（活性/按钮/文案）",
     "真 RoomDirectLink：循环死+洞活 -> start() 重起循环且不重打活洞 + 旧循环 token 自退 + 界面连续 5 跳都能重建并按新模式拉起打洞 + 重建成功补回 running + 三语文案不再说手动重连"),
    # 2026-09-29（用户第三轮实测的 6 条）：
    #   1  本地代理端口 = 唯一可热切换的参数：空闲直接生效；连接中确认后
    #      **自动重建这条隧道并自动重连**（旧实现只改显示、端口还是旧的）；
    #   2  客户端踢人：修 `proto.room_token` 不幂等导致的二次编码（中继回
    #      KICK_NO_SUCH_MEMBER「目标不在这个房间里」）+ 成员列表表头/行共用
    #      一套 grid（出现「踢出」按钮时列错位）；
    #   3  未连接时打洞进度只说「未启用」（不再写「未启用（Relay 模式）」）；
    #   4  MC 多人列表反复刷新 -> 幽灵 PLAYER 连接占满名额（两人房也「房间已满」）：
    #      客户端等配对时盯住本机 MC socket + 服务端容量闸门按人去重 + 等待池过期回收；
    #   5  面板在线房间页：没有中继会话时不再显示「房间里暂时没有玩家。」；
    #   6  被踢的玩家重新进房（未点开始连接）不许被 Host 打起来（忘掉
    #      `_heard_started` + 到达时刻的新鲜度 + 花名册判死）。
    # 2026-09-30（用户第三轮收尾 3 条）：
    #   1  host 离线 -> 其他玩家必须断开/清状态/重算按钮（客户端认中继的
    #      `HOST_LEFT` 控制行 -> `host_left` 事件 -> `_on_host_left`，幂等）；
    #   2  一个人离开不许影响别人：Host 侧不再被旧信标反复拉回目标（日志
    #      ping-pong 修掉），中继侧"主机位换人后前任离开"不再断全房游戏；
    #   3  房主身份：`OK JOIN` 的 `owner=` 取权威值（本人重连不许丢房主），
    #      HOST_STATE 补上客户端早已解析的 `owner_id=`（成员列表标「房主」）。
    ("host_offline_owner_test.py", "host 离线 / 离开隔离 / 房主身份（第三轮收尾 3 条）",
     "客户端协议 + 真界面 + 真中继 + 服务端：HOST_LEFT 控制行被认出来（HOST_LEFT/NO_HOST，HOST_STATE 不误判）+ 界面停会话/清状态/等待房主且幂等 + 离开的人不被旧信标重新变成目标（日志不再 ping-pong）+ 只动离开那一个人 + 主机位换人后前任离开只断自己（真主机/老式房间仍然全房 HOST_LEFT）+ owner= 取权威值 + HOST_STATE 带 owner_id= + 成员列表标出「房主·主机」"),
    ("port_kick_refresh_test.py", "本地端口热切换 / 客户端踢人 / MC 刷新幽灵（第三轮 1-6 条）",
     "真界面 + 真中继 + 替身：端口热切换四种情形 + 中文房间名 KICK 端到端 + token 幂等 + 成员列表单一 grid + 「未启用」文案 + MC 关闭即收工（握手字节暂存）+ occupied_seats 按人去重 + 等待池过期回收 + idle 重进房不是目标 + 面板空态不再渲染"),
    # ★ M6：客户端去硬编码 + 品牌可定制（界面流程）。数据层那半在
    # `tests/m6_client_config_test.py`（跑在包内回归里）；这一套把界面**真的
    # 建出来**，验：地址空 -> 卡片引导 + 首次运行引导窗；「恢复默认」没有品牌
    # 默认值时清空 + 明说；关于页的品牌来源；数据目录卡片改目录后文件真的跟着走。
    # 没有 tkinter / 没有显示器的机器上它打印 SKIP 并返回 0（不拖住别的环境）。
    ("m6_client_gui_test.py", "M6 客户端界面流程（品牌 / 首次引导 / 数据目录）",
     "真 tkinter + 沙箱：空地址引导 + 首次运行引导窗（关掉后不再骚扰，地址仍空则再提示）+ 「恢复默认」清空并说明 + 关于页品牌来源 + 数据目录改目录迁移（文件真落在新目录、旧目录保留）"),
]


def main():
    results = []

    print("=" * 66)
    print(" MC-Link 全量回归测试")
    print("=" * 66)

    for index, (filename, title, note) in enumerate(SUITES, start=1):
        path = os.path.join(HERE, filename)

        print("")
        print("=" * 66)
        print(f" [{index}/{len(SUITES)}] {title}  ({filename})")
        print(f"          {note}")
        print("=" * 66)

        if not os.path.isfile(path):
            print(f"!! 文件不存在: {filename}")
            results.append((title, None))
            continue

        completed = subprocess.run(
            [sys.executable, "-u", path],
            cwd=os.path.dirname(HERE),
        )

        results.append((title, completed.returncode))

    print("")
    print("=" * 66)
    print(" 汇总")
    print("=" * 66)

    failed = 0

    for title, code in results:
        if code is None:
            status = "SKIP (文件不存在)"
            failed += 1
        elif code == 0:
            status = "PASS"
        else:
            status = f"FAIL (exit={code})"
            failed += 1

        print(f"  {status:<22} {title}")

    print("=" * 66)

    if failed:
        print(f"  {failed} 个测试套件失败，请看上面的输出")
    else:
        print("  全部通过 —— 核心链路可以正常使用")

    print("=" * 66)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
