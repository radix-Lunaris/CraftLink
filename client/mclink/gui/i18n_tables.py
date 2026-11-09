"""
CraftLink client -- 翻译表（中 → 英 / 日）。

============================================================================
 这个文件只放"内容"，不放逻辑
============================================================================

`gui/i18n.py` 负责查表、兜底、遍历控件树；这里只有数据。分开的好处是补翻译
（几百条）不用碰任何一行逻辑代码，也不会因为改错一个缩进把界面弄坏。

  EN / JA      静态文案：键 = 界面上的中文原文，值 = 译文
  PATTERNS_EN  动态文案：日志/提示里带变量的句子（f-string 拼完就对不上键了，
  PATTERNS_JA  所以用正则匹配"句式"，把变量原样搬过去）

填表规则（重要）：
  * 键必须和代码里的中文原文**一模一样**（含标点、空格、省略号）。这里的键是
    用 AST 从源码里抽出来的，所以是逐字节一致的 —— 手写很容易差一个空格。
  * 带 {} 的模板原样保留占位符，不要改写成别的形式。
  * 按钮上显示的是"图标 + 标签"（▶ Start / ⧉ Copy）：表里只放**标签本身**
    （"复制"），图标由 gui/i18n.py 的控件遍历自动接回去，别把图标写进键里。
  * 翻不出来就**不要加**：tr() 会原样显示中文，界面不会坏，只是那句没翻。
  * 软件名 CraftLink 不翻译（用户要求：不随语言变化）。
  * 语言下拉框里的「中文 / English / 日本語」各自用本语言书写（这是语言选择器
    的通行做法），所以表里是 identity 条目。

动态句式的写法（PATTERNS_*）：
  * 每条是 (compiled_regex, replacement)，i18n.py 用 pattern.sub 执行；
  * 正则一律用 ^...$ 锚定整条消息，变量位置放 (.+?) 捕获组，替换里用 \1 \2 引用；
  * 只匹配"整句"，绝不做"匹配一大片"的宽松规则 —— 那比不翻更糟。
"""

import re


EN = {
    '网络类型': 'Network type',
    '入站可达': 'Inbound reachability',
    'NAT1 全锥形': 'NAT1 full cone',
    'NAT1/2（完全锥形或地址受限锥形·P2P 轻松）': 'NAT1/2 (full cone or address-restricted cone · easy P2P)',
    '正在退出…': 'exiting…',
    'NAT1/2 未定（全锥或地址受限）': 'NAT1/2 undecided (full cone or address-restricted)',
    '入站中等': 'inbound moderate',
    '等对端首包证实': 'waiting for the peer to prove it',
    'NAT2 地址受限锥形': 'NAT2 address-restricted cone',
    'NAT3 端口受限锥形': 'NAT3 port-restricted cone',
    'NAT4 对称型（递增·可预测）': 'NAT4 symmetric (incremental, predictable)',
    'NAT4 对称型（递减·可预测）': 'NAT4 symmetric (decremental, predictable)',
    'NAT4 对称型（随机·难）': 'NAT4 symmetric (random, hard)',
    '锥型（过滤未测，NAT1/2/3 待定）': 'Cone (filtering not measured; NAT1/2/3 undetermined)',
    '未知（映射未测出）': 'Unknown (mapping not measured)',
    '入站宽松': 'Inbound: permissive',
    '入站严格': 'Inbound: strict',
    '入站未测': 'Inbound: not measured',
    '房主已离线': 'The host went offline',
    '释放主机身份：把主机位置交出去，让别人可以认领。\n连接运行中不能释放 —— 请先点「停止」，否则正在你世界里的玩家会突然断开。\n（「停止」只停止开服，不会释放主机位置。）':
        'Release the host position so somebody else can claim it.\n'
        'It cannot be released while a connection is running -- press "Stop" first, '
        'or the players in your world are dropped without warning.\n'
        '("Stop" only stops hosting; it does not release the host position.)',
    '房主已离线，需要等待房主重新连接后再进。\n\n房间还在，主机位置空着；房主回来认领之后你就可以重新点「开始连接」。':
        'The host went offline, so you need to wait for the host to reconnect before playing.\n\n'
        'The room is still there and the host position is free; once the host claims it again '
        'you can press "Start connection".',
    '等待房主上线': 'Waiting for the host',
    # §B（设计基准）：有 Host 但 Host 还没开始连接时的提示（用户点名要这一句）。
    'Host 未连接': 'Host has not started',
    'Host 未连接，请等待 Host 开服': 'The host has not started yet; wait for the host to open the world',
    '自动重连失败': 'Automatic reconnect failed',
    '已经重试了 {0} 次都没能恢复连接，已停止重试。\n\n原因：{1}\n\n你可以手动点「开始连接」再试。':
        'Gave up after {0} attempts without restoring the connection.\n\n'
        'Reason: {1}\n\nYou can press "Start connection" to try again manually.',
    '重建失败，可以重新连接': 'Rebuild failed; you can connect again',
    '重建失败，正在自动重试 ...': 'Rebuild failed; retrying automatically ...',
    '房主已离线，需要等待房主重新连接后再进。\n\n房间还在、主机位置空着 —— 房主回来认领之后就可以重新点「开始连接」。':
        'The host went offline, so you need to wait for the host to reconnect before playing.\n\n'
        'The room is still there and the host position is free -- once the host claims it again '
        'you can press "Start connection".',
    # 2026-09-27（ISSUES 问题 5）：房间里登录/改名后，中继没确认 IDENTITY 时的提示。
    '改名未生效': 'Display name not updated',
    '房间里显示的名字没能更新：{0}\n\n连接不受影响，房间里的名字会保持服务器给的那个（可能是游客名）。':
        'The name shown in the room could not be updated: {0}\n\n'
        'Your connection is unaffected; the room keeps the name the server gave you '
        '(which may be a guest name).',
    '打洞进度': 'Punch progress',
    '可达（陌生主机能打进来）': 'Reachable (an unrelated host can get in)',
    '可打（出方向已放行，需同步起跑）': 'Punchable (outbound works; needs a synchronised start)',
    '不可达（入方向被挡）': 'Not reachable (inbound is filtered)',
    '未测（还没开始探测）': 'Not measured yet',
    '等待开始（还没有连接会话）': 'Idle (no connection session yet)',
    '打洞中（还没打通）': 'Punching (no direct link yet)',
    # 2026-09-27 用户报的 BUG：单人房间里不许显示"等待同步起降"并一直正计时。
    # 这三种阶段文案是动态值（`RoomDirectLink.direct_step()` 产出，走 tr() 渲染）。
    '等待对端用户加入…': 'Waiting for another player to join...',
    # 2026-09-28 用户第 5 条：房间里有人、但没人点开始连接（和上面那条分开）。
    '等待对端连接…': 'Waiting for the peer to connect...',
    '等待对端起跑公告': 'Waiting for the peer to announce its punch start',
    # 2026-09-28 用户第 4 条：本机测量的两种阶段 + 等对端测量的两种阶段。
    '测量 NAT 类型中…': 'Measuring the NAT type...',
    '测量入站策略中…': 'Measuring the inbound policy...',
    '等待对端测量 NAT 类型': 'Waiting for the peer to measure its NAT type',
    '等待对端测量入站策略中': 'Waiting for the peer to measure its inbound policy',
    '测量 NAT 类型中': 'Measuring the NAT type',
    '测量入站策略中': 'Measuring the inbound policy',
    '交换候选地址': 'Exchanging candidates',
    '等待同步起降': 'Waiting for the coordinated start',
    # 2026-09-29 用户（显示升级 + 两端阶段同步）：
    #   1. `SYNC_GO` 的逐目标行文案改为「等待发包起跑」；
    #   2. 「对端还没到」时逐目标行显示「等待对端…」（WAIT_LABELS）；
    #   3. 打洞那一行带 `PUNCH #0012`（与「第 N 次」并存）。
    '等待发包起跑': 'Waiting for the punch start',
    '等待对端确认 Host': 'Waiting for the peer to confirm the host',
    '等待对端确认目标': 'Waiting for the peer to confirm the target',
    '等待对端上报测量结果': 'Waiting for the peer to report its measurements',
    '等待对端同步起跑': 'Waiting for the peer to finish measuring',
    '等待对端交换候选': 'Waiting for the peer to exchange candidates',
    '等待对端确认 candidate': 'Waiting for the peer to confirm the candidate',
    '等待对端起跑': 'Waiting for the peer to start punching',
    '等待对端打洞': 'Waiting for the peer to punch',
    '等待对端确认已直连': 'Waiting for the peer to confirm the direct link',
    'PUNCH #{0}': 'PUNCH #{0}',
    # 2026-09-28 设计基准 §E/§G：逐目标一行的阶段名 + 括号模板。
    #
    # 这些是**动态**行（`punch_flow.ConnSession.row_parts()` 产出片段，界面自己
    # 拼），所以除阶段名外还要有"括号模板"：中文界面用全角 `（3s）【A】`
    # （用户点名的版式），英/日界面用半角 —— 否则 i18n 硬测试会把全角括号当成
    # "还没翻译"报红灯。
    '确认 Host': 'Confirming the host',
    '确认目标': 'Confirming the target',
    '测量 NAT 类型': 'Measuring the NAT type',
    '测量入站策略': 'Measuring the inbound policy',
    '上报测量结果': 'Reporting the measurements',
    '等待同步起跑': 'Waiting for both sides to finish measuring',
    '同步 candidate': 'Exchanging candidates',
    '确认 candidate': 'Confirming the candidate',
    '打洞失败': 'Punch failed',
    'P2P 已直连': 'Direct P2P',
    '（{0}s）': '({0}s)',
    '【{0}】': '[{0}]',
    # §G 第 2 层「打洞进度」的汇总句式（父级只汇总）。
    '正在建立 {0} 条连接': 'Establishing {0} connections',
    '{0}/{1} 已完成': '{0}/{1} done',
    '{0}/{1} 已完成，{2} 个失败': '{0}/{1} done, {2} failed',
    '{0}/{1} 成功，{2} 个目标失败': '{0}/{1} succeeded, {2} targets failed',
    '打洞完成：全部 {0} 个目标已打通': 'Punch complete: all {0} targets connected',
    # §G 第 1 层「通路」的汇总。
    'P2P 已直连：{0}/{1}': 'Direct P2P: {0}/{1}',
    'P2P 直连中... {0}/{1}': 'Direct P2P in progress... {0}/{1}',
    '直连尝试中... 0/{0}': 'Trying direct connection... 0/{0}',
    # §B 的两种"连不上"要分得开（用户明确要求两句不同的提示）。
    '当前房间暂无 Host': 'No host in this room yet',
    'Host 未连接，请等待 Host 开服': 'The host has not started yet; wait for the host to open the world',
    '确认 Host 中': 'Confirming the host',
    '未打洞（中转模式）': 'Not punching (relay mode)',
    '已打通（等待游戏）': 'Punched through (waiting for the game)',
    '未测（probe did not arrive）': 'not measured (the probe did not arrive)',
    '未测（relay did not confirm the probe）': 'not measured (the relay did not confirm the probe)',
    '未测（no public endpoint yet）': 'not measured (no public endpoint yet)',
    '未测（probe could not be sent: {e}）': 'not measured (the probe could not be sent: {e})',
    '未测（还没开始探测）': 'not measured yet',
    '已直连（长连接保持中）': 'Direct link up (kept alive, independent of any game)',
    '等待开始（点「开始连接」后打洞）': 'Idle (punching starts when you press Start connection)',
    'no public endpoint yet': 'no public endpoint yet',
    'relay did not confirm the probe': 'the relay did not confirm the probe',
    'probe did not arrive': 'the probe did not arrive',
    'warmed probe did not arrive': 'the probe did not arrive (even after warming the relay mapping)',
    'no public endpoint yet': 'no public endpoint yet',
    'an unrelated host reached us': 'an unrelated host reached us',
    '直连尝试': 'Direct attempt',
    '未检测': 'Not checked',
    '等待开始': 'Idle',
    '打洞中，已尝试 {0} 次': 'Punching, {0} attempt(s) so far',
    '已直连 {0}': 'Connected directly to {0}',
    '已直连': 'Connected directly',
    '未启用': 'Not in use',
    '只走 UDP 打洞，一直重试直到连上；永不自动改用中转':
        'UDP hole punching only; retries until it connects and never falls back to the relay',
    '连接模式由你决定（房主）：切换会重建所有隧道，客户端会自动重连（无需手动操作）':
        'You decide the connection mode (host). Switching rebuilds every tunnel and '
        'every client reconnects automatically (nothing to do by hand).',
    '连接模式由房主决定，当前已锁定；你无法修改':
        'The host decides the connection mode; it is locked for you.',
    # 2026-09-28 §A / PROTOCOL §8.3：**只有 Owner 能切模式**，Host（非 Owner）无权。
    '连接模式由房间所有者决定，其他成员不能修改':
        'The connection mode is decided by the room owner; other members cannot change it.',
    '尚未加入房间：模式在你建房或成为房主后生效':
        'Not in a room yet: the mode takes effect once you create a room or become host.',
    '切换连接模式': 'Switch connection mode',
    '将把房间的连接模式从「{0}」切换为「{1}」。\n\n切换会【重建全部隧道】：所有客户端会自动按新模式重连完成，正在游戏中的玩家不需要手动操作，当时空闲的成员保持空闲。\n\n确定要切换吗？':
        'This changes the room connection mode from "{0}" to "{1}".\n\n'
        'Switching REBUILDS every tunnel: all clients reconnect automatically on the new '
        'mode (players in the world do not have to do anything) and idle members stay idle.'
        '\n\nSwitch anyway?',
    '{0}/{1} 台响应（共问了 {2} 台）': '{0}/{1} answered ({2} asked)',
    '只尝试 P2P 打洞直连；打不通就报失败，不会自动改用中转':
        'Direct P2P only; if punching fails it reports the failure '
        'instead of switching to the relay',
    '直连模式：只尝试 P2P 打洞直连，游戏数据不经中转服务器。打不通时不会自动改用中转 —— 需要联机请手动切回「Relay 中转」。（自动选择模式是 AUTO，尚未开放）':
        'Direct mode: tries P2P hole punching only; game data never goes through '
        'the relay. If it cannot get through it does NOT switch to the relay by '
        'itself - switch back to "Relay" manually to play. (Automatic selection '
        'is AUTO, not available yet.)',
    '直连失败': 'Direct failed',
    '通路': 'Path',
    '直连 P2P': 'Direct P2P',
    '中转': 'Relay',
    '直连尝试中': 'Trying direct',
    # ---- V3 直连模式（STUN）----
    '直连模式：先尝试 P2P 打洞直连（不经中转服务器转发游戏数据）；打不通会自动改用中转，过程中你不需要做任何操作。':
        'Direct mode: tries P2P hole punching (game data does not go through the relay). '
        'If it cannot get through it switches to the relay automatically; you do not have to do anything.',

    # ---- V3 网络诊断卡片（只读） ----
    '网络诊断': 'Network diagnostics',
    '开始网络诊断': 'Run network diagnostics',
    'NAT 类型': 'NAT type',
    '公网映射': 'Public mapping',
    'STUN 服务器': 'STUN servers',
    '结论建议': 'What this means',
    '{0}/{1} 台响应': '{0}/{1} answered',
    '正在诊断 ...': 'Diagnosing ...',
    '正在探测 STUN 服务器 ...': 'Probing STUN servers ...',
    '诊断完成': 'Diagnosis complete',
    '诊断完成：判不出': 'Diagnosis complete: inconclusive',
    '诊断失败': 'Diagnosis failed',
    '还没有做过诊断。点左边的按钮开始。':
        'No diagnosis yet. Use the button on the left.',
    '开始网络诊断（只读，不影响当前连接）':
        'Running network diagnostics (read-only, does not affect the current connection)',

    # ---- V3 NAT 判定结果（来自 mclink/core/stun.py） ----
    '无法判定（STUN 无响应）': 'Inconclusive (no STUN response)',
    '公网直连（没有 NAT）': 'Public address (no NAT)',
    '有 NAT 但不改端口': 'Behind a NAT, but the port is not remapped',
    '完全锥形（FullCone）': 'Full-cone',
    '受限锥形（Restricted）': 'Restricted cone',
    '端口受限锥形（PortRestricted）': 'Port-restricted cone',
    '对称型（Symmetric，端口不可预测）':
        'Symmetric (port allocation not predictable)',
    '对称型但端口递增可预测': 'Symmetric, ports increase predictably',
    '对称型但端口递减可预测': 'Symmetric, ports decrease predictably',
    'UDP 被防火墙拦截': 'UDP is blocked by a firewall',
    '换个网络或换一组 STUN 服务器再试；这通常说明 UDP 被拦了。':
        'Try another network or another set of STUN servers; UDP is usually being blocked.',
    '最优。可以直接互连，不需要打洞。':
        'Ideal. Peers can connect directly, no hole punching needed.',
    '最优。NAT 不改端口，打洞几乎必成。':
        'Ideal. The NAT keeps the port, so punching almost always works.',
    '很好。任何一侧都能主动打通。':
        'Very good. Either side can open the path on its own.',
    '良好。双方同时发包即可打通。':
        'Good. Both sides sending at once is enough.',
    '端口按目的地分配且不可预测，打洞基本无解，走中转。':
        'Ports are assigned per destination and unpredictably; punching will not work, so the relay is used.',
    '端口递增有规律，可以预测着打，成功率取决于规律稳定性。':
        'Ports increase with a pattern that can be predicted; success depends on how stable it is.',
    '端口递减有规律，可以预测着打，成功率取决于规律稳定性。':
        'Ports decrease with a pattern that can be predicted; success depends on how stable it is.',
    'UDP 被完全拦截，只能走中转。':
        'UDP is fully blocked; the relay is the only option.',
    # ---- gui/app.py ----
    ' 未登录 ': ' Not signed in ',
    '(无)': '(none)',
    'AUTO 自动': 'AUTO',
    'Minecraft 客户端已断开': 'Minecraft client disconnected',
    'Minecraft 客户端已连上本地代理': 'Minecraft client connected to the local proxy',
    'Minecraft 局域网端口（游戏里“开放到局域网”的端口）':
        'Minecraft LAN port (the port from "Open to LAN")',
    'Minecraft 联机': 'Minecraft multiplayer',
    'Minecraft 连接地址': 'Minecraft address',
    'P2P 打洞直连 · 敬请期待': 'P2P direct · coming soon',
    'Relay 中转': 'Relay',
    'STUN 直连': 'STUN direct',
    '◆ 未加入房间': '◆ Not in a room',
    '● 已连接中转服务器': '● Relay server connected',
    '● 服务器不可用': '● Server unavailable',
    '● 服务器正常': '● Server OK',
    '● 未测试服务器': '● Server not tested',
    '● 正在连接 ...': '● Connecting ...',
    '● 测试中 ...': '● Testing ...',
    '\u3000(你是房间所有者)': '\u3000(you own this room)',
    '\u3000（你是房主）': '\u3000(you are the host)',
    '一个房间只能有一个主机。\n\n如果你想自己开世界，请换一个房间号；如果只是想玩，把身份切换成「玩家 Player」。':
        'A room can only have one host.\n\nIf you want to run your own world, pick another room name; if you just want to play, switch the role to Player.',
    '一般': 'Fair',
    '不能删除': 'Cannot delete',
    '中转延迟': 'Relay ping',
    '中转延迟：本机 ↔ 中转服务器的往返时间（每 3 秒测一次）。\n它不是游戏内延迟：你和主机之间的实际延迟\n≈ 你的中转延迟 + 主机的中转延迟。':
        'Relay ping: round trip between this machine and the relay server (measured every 3 s).\nIt is not in-game latency: the real latency between you and the host\n≈ your relay ping + the relay ping of the host.',
    '中转服务器': 'Relay server',
    '中转服务器地址已恢复默认': 'Relay server address restored to default',
    '中转模式：玩家 → 中转服务器 → 主机。所有游戏数据经由中转服务器转发，兼容性最好。':
        'Relay mode: player → relay server → host. All game traffic is forwarded through the relay server, best compatibility.',
    '主机 Host': 'Host',
    '主机已就绪，等待玩家加入。': 'Host ready, waiting for players to join.',
    '主机模式：先进入单人世界 → 对局域网开放 → 把“开放到局域网”的端口填到上面 → 点开始连接。':
        'Host mode: open your single-player world → Open to LAN → put the LAN port above → press Start.',
    '主机：你（正在开服）': 'Host: you (serving)',
    '主机：你（没在开服 —— 点「开始连接」或「释放主机身份」）':
        'Host: you (not serving — press "Start" or "Release host")',
    '主题': 'Theme',
    '人数上限': 'Max players',
    '人数超过服务器上限': 'Player count is above the server limit',
    '优先直连，失败回落中转 · 敬请期待': 'Direct first, relay fallback · coming soon',
    '优秀': 'Excellent',
    '你已经在一个房间里了，请先点「退出房间」。': 'You are already in a room. Press "Leave room" first.',
    '你已经在一个房间里了，请先点「退出房间」再换。':
        'You are already in a room. Press "Leave room" before switching.',
    '你已经自动退出该房间，可以直接重新创建或加入房间。':
        'You have already left that room automatically; you can create or join a room again.',
    '你是这个房间的所有者，离开后房间会被关闭。': 'You own this room; it will be closed when you leave.',
    '你是这个房间的所有者，退出登录会同时关闭该房间。\n':
        'You own this room; signing out will also close it.\n',
    '你现在不在任何房间里。\n\n要删除房间，请先创建或加入它。':
        'You are not in any room right now.\n\nTo delete a room, create or join it first.',
    '保存失败': 'Save failed',
    '保存日志': 'Save log',
    '修改用户名': 'Change username',
    '停止': 'Stop',
    '切换账号': 'Switch account',
    '切换账号会断开当前连接（如果正在运行请先停止）。':
        'Switching accounts drops the current connection (stop it first if it is running).',
    '创建': 'Create',
    '创建房间': 'Create room',
    '删除失败': 'Delete failed',
    '删除成功': 'Deleted',
    '删除我的房间': 'Delete my room',
    '别人': 'someone else',
    '刷新': 'Refresh',
    '加入': 'Join',
    '加入房间': 'Join room',
    '参数错误': 'Invalid argument',
    '只有【玩家 Player】模式才有 Minecraft 连接地址。\n\n主机模式下这里显示的是本机世界端口，玩家连的是他们自己电脑上的 127.0.0.1 地址。':
        'Only Player mode has a Minecraft address.\n\nIn host mode this shows the local world port; players connect to 127.0.0.1 on their own machine.',
    '启动失败': 'Start failed',
    '在线房间': 'Online rooms',
    '复制': 'Copy',
    '复制日志': 'Copy log',
    '实时速度': 'Speed',
    '实时速度：本次连接当前每秒收发的数据量。\n↑ 上行 = 本机发往中转服务器的数据\n↓ 下行 = 中转服务器发往本机的数据':
        'Speed: how much data this connection sends and receives per second.\n↑ upload = data from this machine to the relay server\n↓ download = data from the relay server to this machine',
    '已与主机建立数据通道，正在转发数据': 'Data channel to the host established, forwarding traffic',
    '已停止': 'Stopped',
    '已创建': 'Created',
    '已加入': 'Joined',
    '已加入世界': 'In world',
    '已就绪，可以进入 Minecraft 了。': 'Ready — you can enter Minecraft now.',
    '已进入房间': 'Entered the room',
    '已退出房间': 'Left the room',
    '已退出房间（退出登录不会留下无人管理的房间）':
        'Left the room (signing out never leaves an unmanaged room behind)',
    '开世界，别人进你的房间': 'I run the world; others join me',
    '基准配置': 'Baseline settings',
    '本机连接要用的数字。改完必须按右边的「确定」才生效。':
        'The numbers this machine uses to connect. Press "Apply" on the right to make a change take effect.',
    '主机 Host：Minecraft 局域网端口': 'Host: Minecraft LAN port',
    '游戏里「对局域网开放」时显示的那个端口。你开世界时用。':
        'The port Minecraft shows when you use "Open to LAN". Used when you run the world.',
    '玩家 Player：本地代理端口': 'Player: local relay port',
    '你作为玩家时，在 Minecraft 里连接 127.0.0.1:该端口。':
        'When you join as a player, connect Minecraft to 127.0.0.1:this port.',
    '确定': 'Apply',
    # ---- 基准配置每行的即时反馈（用户要求：成功还是失败要有反馈）----
    '✓ 已生效：%s':
        '✓ Applied: %s',
    '✓ 已生效：%d 人（建房时用）':
        '✓ Applied: %d players (used when the room is created)',
    '✓ 已生效：%s（下次「开始连接」用新端口）':
        '✓ Applied: %s (the next "Start" uses the new port)',
    '未提交：改了请按「确定」':
        'Not applied yet — press "Apply"',
    '🔒 已锁定 —— 房间已建，要改请先删房重建':
        '🔒 Locked — the room exists; delete it and create it again to change this',
    '修改本地代理端口': 'Change the local proxy port',
    'host 已断开连接':
        'host disconnected',
    'host 已断开连接，host 位置暂无人认领。\n\n如果还需要联机，请等有人认领 host 并开服之后，再手动点「开始连接」。':
        'The host disconnected and the host slot is now free.\n\nIf you still want to play, wait until somebody claims the host slot and starts serving, then press "Start connection" by hand.',
    'host 已断开连接，host 位置暂无人认领':
        'host disconnected; the host slot is free',
    'host 已断开连接（未开服）':
        'host disconnected (not serving)',
    'host 已断开连接（不自动重连，可手动「开始连接」）':
        'host disconnected (no automatic reconnect; use "Start connection")',
    'host 闪断（在连接态：优先复用现有连接，不能复用则自动重连）':
        'host blipped (you were connected: reuse the existing link if it still works, otherwise reconnect automatically)',
    'host 闪断（你原本空闲，不自动重连）':
        'host blipped (you were idle, so no automatic reconnect)',
    'host 未开服':
        'host is not serving',
    'host 未开服（等待 host 开始连接）':
        'host is not serving (waiting for it to start)',
    'host 那边现在没有开服，连上也没用。\n\n房间还在 —— 等 host 开始连接之后，你再手动点「开始连接」。':
        'The host is not serving right now, so connecting would be useless.\n\nThe room is still there -- once the host starts serving, press "Start connection" yourself.',
    '（当前：':
        ' (current: ',
    '把本地代理端口改成 {0} 需要重建这一条隧道：\n\n· 当前游戏连接会断开，需要在 Minecraft 里重新进入一次；\n· 隧道会自动重建、自动重连 —— 你不需要手动点任何东西；\n· 改完之后你的连接状态和现在一样。\n\n确定要现在修改吗？':
        'Changing the local proxy port to {0} rebuilds this tunnel:\n\n'
        '· The current game connection drops; you have to re-enter the world in Minecraft.\n'
        '· The tunnel is rebuilt and reconnected automatically — you do not have to click anything.\n'
        '· When it is done, your connection state is the same as it is now.\n\n'
        'Change it now?',
    '已取消 —— 仍是 %s': 'Cancelled — still %s',
    '✗ 还没填 —— 端口不能为空':
        '✗ Empty — a port is required',
    '✗ 「%s」不是数字':
        '✗ "%s" is not a number',
    '✗ %s 超出范围（只能是 1-65535）':
        '✗ %s is out of range (1-65535)',
    '✗ 超出范围（只能是 %d-%d 人）':
        '✗ Out of range (%d-%d players)',
    '单个房间最多几个人（含房主），2-64 人。只在你【建房】时生效。':
        'How many people fit in one room (owner included), 2-64. Only used when you create the room.',
    '⚠ 房间一旦建好，上面三个数字就锁定了 —— 隧道在建房那一刻就建好了。想改必须先删掉房间，改完再重新建房。':
        '⚠ Once the room exists these numbers are locked — the tunnels are built the moment the room is created. Delete the room, change them, then create it again.',
    '这个数字已经锁定': 'This number is locked',
    '房间一旦建好，就不能再改{what}了。\n\n原因：隧道是在建房那一刻按这个数字建好的。再改只会在新参数上又建一批，两批隧道同时活着，玩家就会连到旧的那一批上（连接丢失）。\n\n这一条对谁都一样 —— 房主、接管的 host、服务器管理员都不能改。\n\n要换参数：先「删除我的房间」，改完再重新建房。':
        'Once the room exists you cannot change {what} anymore.\n\nThe reason: the tunnels were built with this number the moment the room was created. Changing it just builds a second set with the new value; both sets stay alive and players get routed to the old one (the connection drops).\n\nThis applies to everybody — the owner, whoever takes over as host, and server administrators.\n\nTo change it: press "Delete my room", change the number, then create the room again.',
    '主机 Host 的局域网端口': 'the Host LAN port',
    '这个房间已经有主机了，你将按【玩家 Player】连接。想自己开世界请换一个房间号。':
        'This room already has a host, so you will connect as a Player. Use a different room name if you want to run your own world.',
    '房间一旦建好，{what}就锁定了（隧道在建房那一刻就建好了）—— 请先删掉房间，改完再重新建房。':
        'Once the room exists {what} is locked (the tunnels were built the moment the room was created) — delete the room, change it, then create the room again.',
    '还没填「主机 Host：Minecraft 局域网端口」。\n\n请在上面填好，然后按右边的「确定」。':
        '"Host: Minecraft LAN port" is empty.\n\nFill it in above and press "Apply".',
    '还没填「玩家 Player：本地代理端口」。\n\n请在上面填好，然后按右边的「确定」。':
        '"Player: local relay port" is empty.\n\nFill it in above and press "Apply".',
    '端口必须是数字，你填的是「%s」。': 'A port must be a number; you entered "%s".',
    '这个端口不能为空。请先填一个 1-65535 之间的数字。':
        'This port cannot be empty. Enter a number between 1 and 65535.',
    '端口超出范围': 'Port out of range',
    '端口只能是 1-65535，你填的是 %d。': 'A port must be between 1 and 65535; you entered %d.',
    '还没填': 'Not filled in yet',
    '人数超范围': 'Player limit out of range',
    '人数上限必须是数字（%d-%d）。': 'The player limit must be a number (%d-%d).',
    '开始连接': 'Start',
    '开始连接之前需要先选择身份：\n\n· 主机 Host —— 你来开世界，别人进你的房间\n· 玩家 Player —— 你进别人的房间\n\n身份在连接期间不能更改，要先断开才能换。':
        "Pick a role before connecting:\n\n· Host — you run the world, others join your room\n· Player — you join somebody else's room\n\nThe role cannot be changed while connected; disconnect first.",
    '当前主机': 'Host',
    '当前服务器要求登录后才能建房 / 进房':
        'This server requires signing in before you can create or join a room',
    '当前服务器要求登录后才能建房 / 进房。点「登录」输入账号密码。':
        'This server requires signing in before you can create or join a room. Press "Sign in" and enter your account and password.',
    '当前版本仅实现 Relay 模式；STUN 直连与 AUTO 调度敬请期待。':
        'This version only implements Relay mode; STUN direct and AUTO scheduling are coming soon.',
    '当前连接仍在运行，确定要退出吗？': 'The connection is still running. Sign out anyway?',
    '当前连接仍在运行，退出房间会先停止连接。继续吗？':
        'The connection is still running; leaving the room stops it first. Continue?',
    '当前连接还在运行。\n\n请先点「停止」，再释放主机身份 —— 否则正在你世界里的玩家会突然被断开。':
        'The connection is still running.\n\nPress "Stop" before releasing the host slot — otherwise the players in your world are dropped without warning.',
    '恢复默认': 'Restore defaults',
    '我开世界，别人进我的房间': 'I run the world; others join me',
    '我进别人的房间': "I join somebody's world",
    '房主（建房的人）': 'Owner (who created the room)',
    '房间': 'Room',
    '房间不存在': 'Room does not exist',
    '房间关闭通知之后连接没有自行退出，已强制清理本地状态。':
        'The connection did not exit after the room-closed notice; local state was force-cleared.',
    '房间内': 'Inside',
    '房间内在线': 'In room',
    '房间列表': 'Rooms',
    '房间名': 'Room name',
    '房间名不合法': 'Invalid room name',
    '房间名（自己取，中文 / 英文都行；同名就是同一个房间）':
        'Room name (anything you like, Chinese or English; the same name is the same room)',
    '房间已不存在': 'The room no longer exists',
    '房间已被管理关闭': 'The room was closed by an admin',
    '房间还没有主机': 'The room has no host yet',
    '房间还没有主机，无法开始连接': 'The room has no host yet, cannot start connecting',
    '房间里已经有主机了': 'The room already has a host',
    '所有文件': 'All files',
    '打开配置文件夹': 'Open config folder',
    '控制连接已建立（已连上中转服务器）': 'Control connection established (relay server connected)',
    '提示': 'Notice',
    '操作': 'Actions',
    '文本文件': 'Text file',
    '无法认领主机身份': 'Cannot claim the host slot',
    '日志': 'Log',
    '日志已复制到剪贴板': 'Log copied to the clipboard',
    '暗色': 'Dark',
    '服务器当前不要求登录，可以直接建房 / 进房。\n需要账号时再点「登录」即可。':
        'This server does not require signing in; you can create or join a room right away.\nPress "Sign in" when an account is needed.',
    '服务器要求登录，正在弹出登录窗口 ...':
        'The server requires signing in; opening the sign-in window ...',
    '未加入房间': 'Not in a room',
    '未测到': 'Not measured',
    '未登录': 'Not signed in',
    '未登录，已取消连接。': 'Not signed in, connection cancelled.',
    '本地代理端口（Minecraft 将连接 127.0.0.1:该端口）':
        'Local proxy port (Minecraft will connect to 127.0.0.1:this port)',
    '本机在房间里的角色，以及房间号': 'The role of this machine in the room, and the room name',
    '本地连接': 'Local connections',
    '正在停止 ...': 'Stopping ...',
    '正在获取 ...': 'Fetching ...',
    '正在退出 ...': 'Signing out ...',
    '正在退出房间 ...': 'Leaving the room ...',
    '正在通知服务器退出登录 ...': 'Notifying the server about the sign-out ...',
    '流量经中转服务器转发，兼容性最好，推荐': 'Traffic goes through the relay. Most compatible.',
    '测试服务器连接': 'Test server connection',
    '清空': 'Clear',
    '状态': 'State',
    '玩家 Player': 'Player',
    '现在没有房间（先点「创建房间」）': 'No rooms right now (press "Create room" first)',
    '登录': 'Sign in',
    '确定要退出当前账号吗？': 'Sign out of the current account?',
    '确认删除': 'Confirm delete',
    '离开后主机身份（如果你持有）会被释放。': 'Leaving releases the host slot (if you hold it).',
    '累计流量': 'Traffic',
    '累计流量：本次连接启动至今收发的总数据量。':
        'Traffic: total data sent and received since this connection started.',
    '良好': 'Good',
    '要以主机身份开世界，需要先创建房间。': 'To open a world as host, create a room first.',
    '认领主机身份 (host)': 'Claim host',
    '认领失败': 'Claim failed',
    '语言': 'Language',
    '请先停止连接': 'Stop the connection first',
    '请先填写中转服务器地址': 'Enter the relay server address first',
    '请先点「停止」，再切换账号。': 'Press "Stop" before switching accounts.',
    '请先点「停止」，再退出登录。': 'Press "Stop" before signing out.',
    '请先登录账号再改名。': 'Sign in to your account before renaming.',
    '请先选择身份': 'Choose a role first',
    '账号': 'Account',
    '身份': 'Role',
    '身份配置': 'Identity',
    '较差': 'Poor',
    '运行状态': 'Status',
    '还没有可复制的地址：请先点「开始连接」。': 'No address to copy yet: press "Start" first.',
    '还没有连接地址。\n\n请先点「开始连接」。': 'No connection address yet.\n\nPress "Start" first.',
    '这个房间名已经有人用了': 'That room name is already taken',
    '这个房间目前没有主机，进去也会连不上。\n\n等有人认领主机身份之后再开始连接。':
        'This room has no host right now, so joining would not connect anyway.\n\nWait until somebody claims the host slot.',
    '这个房间还没有主机。要以主机身份开始，需要先认领主机身份。\n\n现在认领吗？':
        'This room has no host yet. To start as host you must claim the host slot first.\n\nClaim it now?',
    '连接模式': 'Connection mode',
    '退出': 'Sign out',
    '退出房间': 'Leave room',
    '退出登录': 'Sign out',
    '退出登录会同时退出该房间。\n': 'Signing out also leaves that room.\n',
    '配置目录': 'Config folder',
    '释放主机身份': 'Release host',
    '释放后房间里就没有主机了，其他人可以重新认领。\n\n（你仍然留在房间里，只是不再担任主机。）\n\n确定吗？':
        'After releasing, the room has no host and others can claim it again.\n\n(You stay in the room, you just stop being the host.)\n\nContinue?',
    '需要先加入房间才能连接。': 'Join a room before connecting.',
    '需要重新登录': 'Sign-in required again',
    '（未能确认服务器状态，按需要登录处理）':
        '(could not confirm the server state; treating it as sign-in required)',
    '（正在开服，可以开始连接）': '(serving, you can start connecting)',
    '；': '; ',
    '🔒 未开放': '🔒 Not available yet',

    # ---- gui/auth_ui.py ----
    'CraftLink 修改用户名': 'CraftLink rename',
    'CraftLink 房间列表': 'CraftLink rooms',
    'CraftLink 登录': 'CraftLink sign in',
    '关闭': 'Close',
    '删除选中的房间': 'Delete selected room',
    '加入选中的房间': 'Join selected room',
    '取消': 'Cancel',
    '只有「你自己开的」并且「房间里没有玩家」的房间才能删除。房主也可以在主界面点「删除我的房间」。':
        'Only rooms you created yourself and that have no players in them can be deleted. The owner can also press "Delete my room" on the main screen.',
    '密码': 'Password',
    '已更新到新版本，请再输一次密码（之后会继续记住）':
        'Updated to a new version — enter the password once more (it will be remembered again afterwards)',
    '当前没有在线房间（让房主先点「开始连接」）':
        'No rooms online right now (ask the owner to press "Start")',
    '当前用户名': 'Current username',
    '房主和玩家都需要账号（服务器已开启账号验证）':
        'Hosts and players both need an account (verification is on)',
    '房间号': 'Room',
    '操作失败': 'Action failed',
    '改名失败': 'Rename failed',
    '改名成功': 'Renamed',
    '新的用户名': 'New username',
    '显示密码': 'Show password',
    '未知': 'Unknown',
    '正在加载 ...': 'Loading ...',
    '正在改名 ...': 'Renaming ...',
    '正在获取房间列表 ...': 'Fetching the room list ...',
    '用户名': 'Username',
    '用户名不能包含空格': 'The username cannot contain spaces',
    '用户名就是游戏里和房间列表里显示的名字。改名立即生效，不会掉线，也不用重新登录。':
        'The username is the name shown in game and in the room list. A rename takes effect immediately, without dropping the connection and without signing in again.',
    '用户名最长 32 个字符': 'The username can be at most 32 characters',
    '登录 MC-Link 账号': 'Sign in to CraftLink',
    '登录成功，但密码没能保存到本机（可能是目录权限问题）':
        'Signed in, but the password could not be saved locally (possibly a folder permission problem)',
    '确认改名': 'Confirm',
    '空闲隧道': 'Idle tunnels',
    '端口必须是数字': 'The port must be a number',
    '记住密码（加密保存在本机；每次更新版本后需重新输入一次）':
        'Remember password (encrypted locally; re-enter after an update)',
    '请先选中一个房间': 'Select a room first',
    '请填写密码': 'Enter the password',
    '请填写新的用户名': 'Enter the new username',
    '请填写用户名': 'Enter the username',
    '账号由管理员创建；忘记密码请联系服主重置。':
        'Accounts are created by an admin; ask the owner to reset a password.',
    '还没人认领': 'unclaimed',
    '还没有登录，确定要退出 CraftLink 吗？': 'You are not signed in. Quit CraftLink anyway?',

    # ---- core/protocol.py ----
    '你不是这个房间的房主，只有房主能删除':
        'You are not the host of this room; only the host can delete it',
    '协议握手被服务器拒绝': 'The server rejected the protocol handshake',
    '尝试过于频繁，请稍后再试': 'Too many attempts, please try again later',
    '房间不存在或房主不在线': 'The room does not exist or the owner is offline',
    '房间不存在（可能已经关闭）': 'Room does not exist (it may already be closed)',
    '房间名不能包含换行、制表符等控制字符':
        'A room name cannot contain control characters such as line breaks or tabs',
    '房间已关闭': 'The room is closed',
    '房间已满（达到最大人数）': 'The room is full (maximum players reached)',
    '房间已被管理员关闭': 'The room was closed by the administrator',
    '房间里还有玩家，不能删除（请先让玩家离开）':
        'There are still players in the room, it cannot be deleted (ask them to leave first)',
    '改名失败，请联系管理员': 'Rename failed, please contact an administrator',
    '服务器不认识该角色（客户端与服务端版本不匹配）':
        'The server does not know this role (client and server versions differ)',
    '服务器数据库暂时不可用，请稍后再试':
        'The server database is temporarily unavailable, please try again later',
    '服务器没有开启账号系统': 'The server has no account system enabled',
    '未知错误': 'Unknown error',
    '本月的自助修改次数已用完（改名和改密码共用 3 次）':
        'The monthly self-service changes are used up (3 shared between rename and password)',
    '用户名不合法（2-32 个字符，不能有空格）': 'Invalid username (2-32 characters, no spaces)',
    '用户名或密码格式不正确': 'Wrong username or password format',
    '用户名或密码错误': 'Wrong username or password',
    '登录状态已失效，请重新登录': 'The sign-in has expired, please sign in again',
    '该房间已经有房主了': 'That room already has an owner',
    '请先登录账号': 'Please sign in to your account first',
    '账号已被管理员禁用': 'The account has been disabled by an administrator',
    '这个房间名已经有人在用了。\n建房间请换一个名字，或者直接「加入房间」进他的房间。':
        'Somebody is already using this room name.\nTo create a room pick another name, or press "Join room" to enter theirs.',
    '这个用户名已经被别人用了': 'That username is already taken',

    # ---- core/control_client.py ----
    '密码最长 128 个字符': 'The password can be at most 128 characters',
    '房间名为空': 'The room name is empty',
    '改名和改密码不限次数（管理员及以上）': 'Unlimited renames and password changes (admin and above)',
    '新用户名和当前用户名相同': 'The new username is the same as the current one',
    '有玩家': 'Players',
    '服务器没有响应，请稍后再试': 'The server did not respond, please try again later',
    '服务器没有返回任何内容': 'The server returned nothing',
    '服务器没有返回登录凭证（可能是旧版服务端）':
        'The server returned no login credentials (it may be an older server build)',
    '本月的自助修改次数已用完（每月 3 次，改名和改密码共用）':
        'The monthly self-service changes are used up (3 per month, shared between rename and password)',
    '根管理员': 'Root admin',
    '用户名不能包含空格或特殊字符': 'The username cannot contain spaces or special characters',
    '用户名和密码不能包含空格': 'The username and password cannot contain spaces',
    '用户名至少 2 个字符': 'The username must be at least 2 characters',
    '空闲': 'Idle',
    '管理员': 'Admin',
    '请先登录': 'Please sign in first',
    '请先登录账号再改名': 'Sign in to your account before renaming',
    '请输入密码': 'Enter the password',
    '请输入新的用户名': 'Enter the new username',
    '请输入用户名': 'Enter the username',
    '（还没人认领）': '(unclaimed)',

    # ---- core/room.py ----
    '与服务器的连接已断开': 'The connection to the server was closed',
    '人数必须是数字': 'The player count must be a number',
    '已获得房主身份': 'Owner role acquired',
    '已释放房主身份': 'Owner role released',
    '成员': 'Members',
    '房主': 'Owner',
    '房间不存在（可能已被管理员关闭）': 'Room does not exist (it may have been closed by an admin)',
    '房间已被关闭': 'The room has been closed',
    '持有房主身份': 'Holds the owner role',
    '无房主': 'No owner',
    '有房主': 'Has owner',
    '服务器没有响应就关闭了连接': 'The server closed the connection without responding',
    '管理员关闭了房间': 'An admin closed the room',

    # ---- core/session.py ----
    '尚未启动，无法删除房间': 'Not started yet, cannot delete the room',
    '服务器没有确认删除请求（可能需要重试）':
        'The server did not confirm the delete request (a retry may be needed)',
    '未知结果': 'Unknown result',
    '登录状态已失效': 'The sign-in has expired',
    '等待中': 'Waiting',
    '运行中': 'Running',
    '连接中': 'Connecting',

    # ---- core/host_core.py ----
    '              MC-LINK  主机 (Host)': '              MC-LINK  Host (Host)',
    '[CONTROL] 已请求删除房间，等待服务器确认 ...':
        '[CONTROL] room deletion requested, waiting for the server ...',
    '中转控制连接被关闭': 'The relay closed the control connection',
    '中转服务器关闭了控制连接': 'The relay server closed the control connection',
    '中转服务器关闭了隧道': 'The relay server closed the tunnel',
    '主机已停止。': 'Host stopped.',
    '主机已启动，等待玩家连接。': 'Host started, waiting for players to connect.',
    '房间已删除': 'The room was deleted',
    '控制连接未建立，无法删除房间': 'No control connection, cannot delete the room',
    '正在停止主机 ...': 'Stopping the host ...',
    '隧道在分配前被关闭': 'The tunnel was closed before it was assigned',

    # ---- core/player_core.py ----
    '              MC-LINK  玩家 (Player)': '              MC-LINK  Player (Player)',
    '[LOCAL] 请在 Minecraft 中连接：': '[LOCAL] connect in Minecraft:',
    '[RELAY] PLAYER 握手已发送': '[RELAY] PLAYER handshake sent',
    '中转服务器关闭了连接': 'The relay server closed the connection',
    '正在停止玩家模式 ...': 'Stopping player mode ...',
    '玩家模式已停止。': 'Player mode stopped.',

    # ---- core/engine.py ----
    '当前没有正在运行的连接': 'No connection is running right now',
    '操作超时': 'The operation timed out',
    '服务器没有响应（连接被立即关闭）':
        'The server did not respond (the connection was closed immediately)',
    '未启动': 'Idle',
    '网络线程尚未启动': 'The network thread has not started yet',
    '还没有加入房间': 'Not in a room yet',
    '还没有进入房间': 'Not in a room yet',
    '连接超时，可能被防火墙/安全组拦截':
        'Connection timed out; a firewall or security group may be blocking it',
    '错误': 'Error',

    # ---- core/presence.py ----
    '在线状态连接被关闭': 'The presence connection was closed',
    '已上报在线状态': 'Presence reported',
    '服务器不支持在线状态上报（旧版中继），跳过':
        'The server does not support presence reporting (older relay); skipping',

    # ---- core/reliable.py ----
    '会话已关闭': 'The session is closed',
    '可靠流握手超时（对端没有响应）': 'Reliable-stream handshake timed out (the peer did not respond)',
    '对端重置了连接': 'The peer reset the connection',
    '对端长时间无响应': 'The peer stopped responding',
    '重传次数超限，路径可能已失效': 'Too many retransmissions; the path may be dead',

    # ---- config/settings.py ----
    'Minecraft 局域网端口必须在 1-65535 之间':
        'The Minecraft LAN port must be between 1 and 65535',
    'Minecraft 局域网端口必须是数字': 'The Minecraft LAN port must be a number',
    '中转服务器端口必须在 1-65535 之间': 'The relay server port must be between 1 and 65535',
    '中转服务器端口必须是数字': 'The relay server port must be a number',
    '本地代理端口必须在 1-65535 之间': 'The local proxy port must be between 1 and 65535',
    '本地代理端口必须是数字': 'The local proxy port must be a number',
    '玩家': 'Player',
    '请先选择身份（主机 Host 或玩家 Player）': 'Choose a role first (Host or Player)',
    '请填写中转服务器地址': 'Enter the relay server address',
    '请填写房间名': 'Enter a room name',

    # ---- gui/theme.py + language picker ----
    'English': 'English',
    'MC-Link 房间列表': 'CraftLink rooms',
    '中文': '中文',
    '亮色': 'Light',
    '亮色 · 晨曦': 'Light · Dawn',
    '亮色 · 薄荷': 'Light · Mint',
    '日本語': '日本語',
    '暗色 · 星云': 'Dark · Nebula',
    '暗色 · 极光': 'Dark · Aurora',
    '跟随系统': 'System',
    '还没有登录，确定要退出 MC-Link 吗？': 'You are not signed in. Quit CraftLink anyway?',

    # ---- 3.0.9: 房间成员列表 + 踢人（KICK）----
    '房间成员': 'Room members',
    '房间成员列表（房间里的人，实时更新）':
        'Room member list (everyone in the room, updated live)',
    '连接状态': 'Connection',
    # 2026-09-28（用户第 7 条）：这一列以中继广播的 `conn=` 为准（PROTOCOL §8.5）。
    '未连接': 'Not connected',
    '等待配对': 'Waiting to pair',
    '配对中': 'Pairing',
    '入站档位': 'Inbound grade',
    'IP:端口': 'IP:port',
    '延迟': 'Latency',
    # 对端延迟（玩家 ↔ 主机 的直连 RTT）。N/C = not connected / not applicable，
    # 是一个符号，三种语言保持一致，不翻译。
    '对端延迟': 'Peer latency',
    'N/C': 'N/C',
    # 亚毫秒的直连 RTT（审计问题 5）：绝不写成 0。
    '<1 ms': '<1 ms',
    '你的名字：': 'Your name: ',
    '踢出': 'Kick',
    '主机': 'Host',
    '已连接': 'Connected',
    '直连': 'Direct',
    '打洞中': 'Punching',
    '已打通': 'Punched through',
    '第 {0} 次': 'attempt {0}',
    '对端': 'Peer',
    '踢出成员': 'Kick member',
    '确定要把该成员踢出房间吗？': 'Remove this member from the room?',
    '确定要把「{0}」踢出房间吗？': 'Remove "{0}" from the room?',
    '已踢出成员': 'Member kicked',
    '该成员已被踢出房间。': 'The member has been removed from the room.',
    '已踢出房主，房间已解散。':
        'The owner was kicked, so the room has been dissolved.',
    '踢人失败': 'Kick failed',
    '你已被踢出房间': 'You were kicked out',
    '你已被踢出房间。': 'You have been removed from the room.',
    '你已被移出该房间，已自动退出。':
        'You were removed from the room and have left it automatically.',
    '发起者：': 'By: ',
    '原因：': 'Reason: ',
    '被踢出房间': 'removed from the room',
    '房主被踢出（房间已解散）': 'the owner was kicked (the room was dissolved)',
    '该成员不在房间里（可能已经离开）': 'That member is not in the room (they may have left)',
    '你没有踢人的权限（只有房主或当前主机可以）':
        'You do not have permission to kick (only the owner or the current host can)',

}


JA = {
    '网络类型': 'ネットワーク種別',
    '入站可达': '受信の到達性',
    'NAT1 全锥形': 'NAT1 フルコーン',
    'NAT1/2（完全锥形或地址受限锥形·P2P 轻松）': 'NAT1/2（フルコーンまたはアドレス制限コーン・P2P 容易）',
    '正在退出…': '終了しています…',
    'NAT1/2 未定（全锥或地址受限）': 'NAT1/2 未定（フルコーンまたはアドレス制限）',
    '等对端首包证实': '対端の証明待ち',
    'NAT2 地址受限锥形': 'NAT2 アドレス制限コーン',
    'NAT3 端口受限锥形': 'NAT3 ポート制限コーン',
    'NAT4 对称型（递增·可预测）': 'NAT4 対称型（増加・予測可）',
    'NAT4 对称型（递减·可预测）': 'NAT4 対称型（減少・予測可）',
    'NAT4 对称·随机（难）': 'NAT4 対称・ランダム（困難）',
    '锥型（过滤未测，NAT1/2/3 待定）': 'コーン（フィルタ未測定。NAT1/2/3 未確定）',
    '未知（映射未测出）': '不明（マッピング未測定）',
    '入站宽松': '受信：ゆるい',
    '入站中等': '受信：ふつう',
    '入站严格': '受信：きびしい',
    '入站未测': '受信：未測定',
    '房主已离线': 'ホストがオフラインになりました',
    '释放主机身份：把主机位置交出去，让别人可以认领。\n连接运行中不能释放 —— 请先点「停止」，否则正在你世界里的玩家会突然断开。\n（「停止」只停止开服，不会释放主机位置。）':
        'ホスト位置を解放して、他の人が取得できるようにします。\n'
        '接続の実行中は解放できません。先に「停止」を押してください。'
        'そうしないと、あなたのワールドにいるプレイヤーが突然切断されます。\n'
        '（「停止」はサーバーを止めるだけで、ホスト位置は解放しません。）',
    '房主已离线，需要等待房主重新连接后再进。\n\n房间还在，主机位置空着；房主回来认领之后你就可以重新点「开始连接」。':
        'ホストがオフラインになったため、ホストが再接続するまで参加できません。\n\n'
        'ルームは残っており、ホストの位置は空いています。ホストが再取得したら'
        '「接続開始」を押してください。',
    '等待房主上线': 'ホストの復帰を待っています',
    # §B（設計基準）
    'Host 未连接': 'ホストが未接続',
    'Host 未连接，请等待 Host 开服': 'ホストが未接続です。ホストの開始を待ってください',
    '自动重连失败': '自動再接続に失敗しました',
    '已经重试了 {0} 次都没能恢复连接，已停止重试。\n\n原因：{1}\n\n你可以手动点「开始连接」再试。':
        '{0} 回試しても接続を復元できなかったため、再試行を停止しました。\n\n'
        '理由：{1}\n\n手動で「接続開始」を押して再試行できます。',
    '重建失败，可以重新连接': '再構築に失敗しました。再接続できます',
    '重建失败，正在自动重试 ...': '再構築に失敗しました。自動で再試行中 ...',
    '房主已离线，需要等待房主重新连接后再进。\n\n房间还在、主机位置空着 —— 房主回来认领之后就可以重新点「开始连接」。':
        'ホストがオフラインになったため、ホストが再接続するまで参加できません。\n\n'
        'ルームは残っており、ホストの位置は空いています。ホストが再取得したら'
        '「接続開始」を押してください。',
    # 2026-09-27（ISSUES 問題 5）
    '改名未生效': '表示名を更新できませんでした',
    '房间里显示的名字没能更新：{0}\n\n连接不受影响，房间里的名字会保持服务器给的那个（可能是游客名）。':
        'ルーム内の表示名を更新できませんでした：{0}\n\n'
        '接続には影響ありません。ルーム内の名前はサーバーが割り当てたもの'
        '（ゲスト名の場合があります）のままになります。',
    '打洞进度': 'パンチの進捗',
    '可达（陌生主机能打进来）': '到達可（未知のホストから受信できる）',
    '可打（出方向已放行，需同步起跑）': 'パンチ可（送信は可、同時開始が必要）',
    '不可达（入方向被挡）': '到達不可（受信が遮断）',
    '未测（还没开始探测）': '未測定',
    '未打洞（中转模式）': '穴あけ未実施（中継モード）',
    # 2026-09-27（同じ BUG の日本語）
    '等待对端用户加入…': '対戦相手の参加を待っています…',
    '等待对端连接…': '対戦相手の接続を待っています…',
    '等待对端起跑公告': '相手の開始アナウンスを待っています',
    '测量 NAT 类型中…': 'NAT タイプを測定中…',
    '测量入站策略中…': '受信ポリシーを測定中…',
    '等待对端测量 NAT 类型': '対戦相手の NAT タイプ測定を待っています',
    '等待对端测量入站策略中': '対戦相手の受信ポリシー測定を待っています',
    '测量 NAT 类型中': 'NAT タイプを測定中',
    '测量入站策略中': '受信ポリシーを測定中',
    '交换候选地址': '候補アドレスを交換中',
    '等待同步起降': '同期開始を待っています',
    # 2026-09-29（日本語）
    '等待发包起跑': 'パンチ開始を待っています',
    '等待对端确认 Host': '対戦相手のホスト確認を待っています',
    '等待对端确认目标': '対戦相手のターゲット確認を待っています',
    '等待对端上报测量结果': '対戦相手の測定結果報告を待っています',
    '等待对端同步起跑': '対戦相手の測定完了を待っています',
    '等待对端交换候选': '対戦相手の候補交換を待っています',
    '等待对端确认 candidate': '対戦相手の候補確認を待っています',
    '等待对端起跑': '対戦相手のパンチ開始を待っています',
    '等待对端打洞': '対戦相手のパンチを待っています',
    '等待对端确认已直连': '対戦相手の直結確認を待っています',
    'PUNCH #{0}': 'PUNCH #{0}',
    # 2026-09-28 設計基準 §E/§G（日本語）
    '确认 Host': 'ホストを確認中',
    '确认目标': 'ターゲットを確認中',
    '测量 NAT 类型': 'NAT タイプを測定中',
    '测量入站策略': '受信ポリシーを測定中',
    '上报测量结果': '測定結果を報告中',
    '等待同步起跑': '両側の測定完了を待っています',
    '同步 candidate': '候補アドレスを交換中',
    '确认 candidate': '候補アドレスを確認中',
    '打洞失败': '穴あけに失敗',
    'P2P 已直连': 'P2P 直結',
    '（{0}s）': '({0}s)',
    '【{0}】': '[{0}]',
    '正在建立 {0} 条连接': '{0} 件の接続を確立中',
    '{0}/{1} 已完成': '{0}/{1} 完了',
    '{0}/{1} 已完成，{2} 个失败': '{0}/{1} 完了、{2} 件失敗',
    '{0}/{1} 成功，{2} 个目标失败': '{0}/{1} 成功、{2} 件失敗',
    '打洞完成：全部 {0} 个目标已打通': '穴あけ完了：{0} 件すべて接続済み',
    'P2P 已直连：{0}/{1}': 'P2P 直結：{0}/{1}',
    'P2P 直连中... {0}/{1}': 'P2P 直結中... {0}/{1}',
    '直连尝试中... 0/{0}': '直結を試行中... 0/{0}',
    '当前房间暂无 Host': 'このルームにはまだホストがいません',
    'Host 未连接，请等待 Host 开服': 'ホストが未接続です。ホストの開始を待ってください',
    '确认 Host 中': 'ホストを確認中',
    '等待开始（还没有连接会话）': '待機中（接続セッションなし）',
    '已直连（长连接保持中）': '直結中（長接続を維持。ゲームには依存しません）',
    '等待开始（点「开始连接」后打洞）': '待機中（「接続開始」を押すと穴あけを開始します）',
    '直连尝试': '直結の試行',
    '未检测': '未検出',
    '等待开始': '待機中',
    '打洞中，已尝试 {0} 次': '穴あけ中（{0} 回試行）',
    '已直连 {0}': '{0} と直結しました',
    '已直连': '直結しました',
    '未启用': '未使用',
    '只走 UDP 打洞，一直重试直到连上；永不自动改用中转':
        'UDP 穴あけのみ。接続できるまで再試行し、中継へは自動で切り替えません',
    '连接模式由你决定（房主）：切换会重建所有隧道，客户端会自动重连（无需手动操作）':
        '接続モードはあなた（ホスト）が決めます。切り替えると全トンネルを再構築し、'
        '各クライアントは自動的に再接続します（手動操作は不要です）。',
    '连接模式由房主决定，当前已锁定；你无法修改':
        '接続モードはホストが決定します。あなたは変更できません。',
    '连接模式由房间所有者决定，其他成员不能修改':
        '接続モードはルーム作成者が決定します。他のメンバーは変更できません。',
    '尚未加入房间：模式在你建房或成为房主后生效':
        'まだルームに参加していません。ルーム作成またはホスト就任後に有効になります。',
    '切换连接模式': '接続モードの切り替え',
    '将把房间的连接模式从「{0}」切换为「{1}」。\n\n切换会【重建全部隧道】：所有客户端会自动按新模式重连完成，正在游戏中的玩家不需要手动操作，当时空闲的成员保持空闲。\n\n确定要切换吗？':
        'ルームの接続モードを「{0}」から「{1}」に変更します。\n\n'
        '切り替えると【すべてのトンネルを再構築】します：各クライアントは新しいモードで'
        '自動的に再接続し（プレイ中のプレイヤーも操作は不要）、待機中のメンバーはそのままです。'
        '\n\n切り替えますか？',
    '{0}/{1} 台响应（共问了 {2} 台）': '{0}/{1} 台が応答（{2} 台に問い合わせ）',
    '只尝试 P2P 打洞直连；打不通就报失败，不会自动改用中转':
        'P2P 穴あけのみを試し、通らなければ失敗として報告します（中継へは自動切替しません）',
    '直连模式：只尝试 P2P 打洞直连，游戏数据不经中转服务器。打不通时不会自动改用中转 —— 需要联机请手动切回「Relay 中转」。（自动选择模式是 AUTO，尚未开放）':
        '直結モード：P2P 穴あけのみを試します。ゲームデータは中継サーバーを通りません。'
        '通らない場合でも自動で中継に切り替わりません。プレイするには手動で「Relay」に戻してください。'
        '（自動選択モードは AUTO で、まだ未開放です。）',
    '直连失败': '直結失敗',
    '通路': '経路',
    '直连 P2P': 'P2P 直結',
    '中转': '中継',
    '直连尝试中': '直結を試行中',
    # ---- V3 直結モード（STUN）----
    '直连模式：先尝试 P2P 打洞直连（不经中转服务器转发游戏数据）；打不通会自动改用中转，过程中你不需要做任何操作。':
        '直結モード：まず P2P 穴あけを試します（ゲームデータは中継サーバーを通りません）。'
        '通らない場合は自動的に中継へ切り替わります。操作は不要です。',

    # ---- V3 ネットワーク診断カード（読み取り専用） ----
    '网络诊断': 'ネットワーク診断',
    '开始网络诊断': 'ネットワーク診断を実行',
    'NAT 类型': 'NAT タイプ',
    '公网映射': 'グローバル側のマッピング',
    'STUN 服务器': 'STUN サーバー',
    '结论建议': 'この結果の意味',
    '{0}/{1} 台响应': '{0}/{1} 台が応答',
    '正在诊断 ...': '診断中 ...',
    '正在探测 STUN 服务器 ...': 'STUN サーバーを確認しています ...',
    '诊断完成': '診断完了',
    '诊断完成：判不出': '診断完了：判定できません',
    '诊断失败': '診断に失敗しました',
    '还没有做过诊断。点左边的按钮开始。':
        'まだ診断していません。左のボタンから実行してください。',
    '开始网络诊断（只读，不影响当前连接）':
        'ネットワーク診断を開始します（読み取り専用・現在の接続には影響しません）',

    # ---- V3 NAT 判定結果（mclink/core/stun.py 由来） ----
    '无法判定（STUN 无响应）': '判定できません（STUN の応答なし）',
    '公网直连（没有 NAT）': 'グローバルアドレス（NAT なし）',
    '有 NAT 但不改端口': 'NAT はあるがポートは変換されない',
    '完全锥形（FullCone）': 'フルコーン',
    '受限锥形（Restricted）': '制限コーン',
    '端口受限锥形（PortRestricted）': 'ポート制限コーン',
    '对称型（Symmetric，端口不可预测）':
        '対称型（ポート割り当ては予測不可）',
    '对称型但端口递增可预测': '対称型だがポートが規則的に増加',
    '对称型但端口递减可预测': '対称型だがポートが規則的に減少',
    'UDP 被防火墙拦截': 'UDP がファイアウォールで遮断されている',
    '换个网络或换一组 STUN 服务器再试；这通常说明 UDP 被拦了。':
        '別のネットワークか別の STUN サーバーで再試行してください。通常は UDP が遮断されています。',
    '最优。可以直接互连，不需要打洞。':
        '最良。直接接続でき、穴あけは不要です。',
    '最优。NAT 不改端口，打洞几乎必成。':
        '最良。NAT がポートを変えないため、穴あけはほぼ成功します。',
    '很好。任何一侧都能主动打通。':
        '良好。どちらからでも接続を開けられます。',
    '良好。双方同时发包即可打通。':
        '良好。双方が同時に送信すれば開通します。',
    '端口按目的地分配且不可预测，打洞基本无解，走中转。':
        'ポートは宛先ごとに予測不能に割り当てられるため穴あけは不可で、リレーを使用します。',
    '端口递增有规律，可以预测着打，成功率取决于规律稳定性。':
        'ポートが規則的に増加するため予測して試行できます。成功率は規則の安定性に依存します。',
    '端口递减有规律，可以预测着打，成功率取决于规律稳定性。':
        'ポートが規則的に減少するため予測して試行できます。成功率は規則の安定性に依存します。',
    'UDP 被完全拦截，只能走中转。':
        'UDP が完全に遮断されているため、リレーのみ利用できます。',
    # ---- gui/app.py ----
    ' 未登录 ': ' 未ログイン ',
    '(无)': '（なし）',
    'AUTO 自动': 'AUTO',
    'Minecraft 客户端已断开': 'Minecraft クライアントが切断されました',
    'Minecraft 客户端已连上本地代理': 'Minecraft クライアントがローカルプロキシに接続しました',
    'Minecraft 局域网端口（游戏里“开放到局域网”的端口）': 'Minecraft LANポート（「LANに公開」のポート）',
    'Minecraft 联机': 'Minecraft マルチプレイ',
    'Minecraft 连接地址': 'Minecraft 接続先',
    'P2P 打洞直连 · 敬请期待': 'P2P 直結 · 近日公開',
    'Relay 中转': 'リレー',
    'STUN 直连': 'STUN 直結',
    '◆ 未加入房间': '◆ ルーム未参加',
    '● 已连接中转服务器': '● リレーサーバー接続済み',
    '● 服务器不可用': '● サーバー利用不可',
    '● 服务器正常': '● サーバー正常',
    '● 未测试服务器': '● サーバー未確認',
    '● 正在连接 ...': '● 接続中 ...',
    '● 测试中 ...': '● 確認中 ...',
    '\u3000(你是房间所有者)': '\u3000（あなたが作成者）',
    '\u3000（你是房主）': '\u3000（あなたがホスト）',
    '一个房间只能有一个主机。\n\n如果你想自己开世界，请换一个房间号；如果只是想玩，把身份切换成「玩家 Player」。':
        'ルームにホストは1人だけです。\n\n自分でワールドを開くなら別のルーム名にしてください。参加だけなら役割を「プレイヤー」に切り替えてください。',
    '一般': '普通',
    '不能删除': '削除できません',
    '中转延迟': 'リレー遅延',
    '中转延迟：本机 ↔ 中转服务器的往返时间（每 3 秒测一次）。\n它不是游戏内延迟：你和主机之间的实际延迟\n≈ 你的中转延迟 + 主机的中转延迟。':
        'リレー遅延：本機 ↔ リレーサーバーの往復時間（3秒ごとに計測）。\nゲーム内の遅延ではありません。あなたとホストの実際の遅延\n≈ あなたのリレー遅延 + ホストのリレー遅延。',
    '中转服务器': 'リレーサーバー',
    '中转服务器地址已恢复默认': 'リレーサーバーのアドレスを既定値に戻しました',
    '中转模式：玩家 → 中转服务器 → 主机。所有游戏数据经由中转服务器转发，兼容性最好。':
        'リレーモード：プレイヤー → リレーサーバー → ホスト。すべての通信はリレーサーバー経由で、互換性が最も高い。',
    '主机 Host': 'ホスト',
    '主机已就绪，等待玩家加入。': 'ホストの準備ができました。プレイヤーの参加を待っています。',
    '主机模式：先进入单人世界 → 对局域网开放 → 把“开放到局域网”的端口填到上面 → 点开始连接。':
        'ホストモード：シングルプレイのワールドに入る → 「LANに公開」→ 表示されたポートを上に入力 → 接続開始を押す。',
    '主机：你（正在开服）': 'ホスト：あなた（公開中）',
    '主机：你（没在开服 —— 点「开始连接」或「释放主机身份」）': 'ホスト：あなた（未公開 — 「接続開始」か「ホストを放棄」を押す）',
    '主题': 'テーマ',
    '人数上限': '人数上限',
    '人数超过服务器上限': 'サーバー上限を超えています',
    '优先直连，失败回落中转 · 敬请期待': '直結優先、失敗時リレー · 近日公開',
    '优秀': '優秀',
    '你已经在一个房间里了，请先点「退出房间」。': 'すでにルームに参加しています。先に「ルーム退出」を押してください。',
    '你已经在一个房间里了，请先点「退出房间」再换。': 'すでにルームに参加しています。切り替える前に「ルーム退出」を押してください。',
    '你已经自动退出该房间，可以直接重新创建或加入房间。': 'そのルームからは自動的に退出しました。もう一度ルームを作成・参加できます。',
    '你是这个房间的所有者，离开后房间会被关闭。': 'あなたはこのルームの作成者です。退出するとルームは閉じられます。',
    '你是这个房间的所有者，退出登录会同时关闭该房间。\n': 'あなたはこのルームの作成者です。ログアウトするとルームも閉じられます。\n',
    '你现在不在任何房间里。\n\n要删除房间，请先创建或加入它。':
        '現在どのルームにも参加していません。\n\n削除するには、まずルームを作成するか参加してください。',
    '保存失败': '保存に失敗しました',
    '保存日志': 'ログを保存',
    '修改用户名': 'ユーザー名の変更',
    '停止': '停止',
    '切换账号': 'アカウント切替',
    '切换账号会断开当前连接（如果正在运行请先停止）。': 'アカウントを切り替えると現在の接続が切断されます（実行中なら先に停止してください）。',
    '创建': '作成',
    '创建房间': 'ルーム作成',
    '删除失败': '削除に失敗しました',
    '删除成功': '削除しました',
    '删除我的房间': '自分のルームを削除',
    '别人': '他の人',
    '刷新': '更新',
    '加入': '参加',
    '加入房间': 'ルーム参加',
    '参数错误': 'パラメータエラー',
    '只有【玩家 Player】模式才有 Minecraft 连接地址。\n\n主机模式下这里显示的是本机世界端口，玩家连的是他们自己电脑上的 127.0.0.1 地址。':
        'Minecraft 接続先があるのは【プレイヤー】モードだけです。\n\nホストモードでは本機のワールドポートを表示します。プレイヤーは自分の PC の 127.0.0.1 に接続します。',
    '启动失败': '起動に失敗しました',
    '在线房间': 'オンラインルーム',
    '复制': 'コピー',
    '复制日志': 'ログをコピー',
    '实时速度': '速度',
    '实时速度：本次连接当前每秒收发的数据量。\n↑ 上行 = 本机发往中转服务器的数据\n↓ 下行 = 中转服务器发往本机的数据':
        '速度：この接続が1秒ごとに送受信しているデータ量。\n↑ 上り = 本機からリレーサーバーへ\n↓ 下り = リレーサーバーから本機へ',
    '已与主机建立数据通道，正在转发数据': 'ホストとのデータ経路を確立しました。転送中です',
    '已停止': '停止しました',
    '已创建': '作成しました',
    '已加入': '参加しました',
    '已加入世界': 'ワールド内',
    '已就绪，可以进入 Minecraft 了。': '準備完了。Minecraft に入れます。',
    '已进入房间': 'ルームに入りました',
    '已退出房间': 'ルームから退出しました',
    '已退出房间（退出登录不会留下无人管理的房间）': 'ルームから退出しました（ログアウトしても管理されないルームは残りません）',
    '开始连接': '接続開始',
    '开始连接之前需要先选择身份：\n\n· 主机 Host —— 你来开世界，别人进你的房间\n· 玩家 Player —— 你进别人的房间\n\n身份在连接期间不能更改，要先断开才能换。':
        '接続の前に役割を選んでください：\n\n· ホスト — 自分がワールドを開き、他の人が入る\n· プレイヤー — 他人のルームに入る\n\n接続中は役割を変更できません。先に切断してください。',
    '当前主机': 'ホスト',
    '当前服务器要求登录后才能建房 / 进房': 'このサーバーではルームの作成・参加にログインが必要です',
    '当前服务器要求登录后才能建房 / 进房。点「登录」输入账号密码。':
        'このサーバーではルームの作成・参加にログインが必要です。「ログイン」を押してアカウントとパスワードを入力してください。',
    '当前版本仅实现 Relay 模式；STUN 直连与 AUTO 调度敬请期待。':
        'このバージョンはリレーモードのみ実装しています。STUN 直結と AUTO は近日公開です。',
    '当前连接仍在运行，确定要退出吗？': '接続がまだ実行中です。ログアウトしますか？',
    '当前连接仍在运行，退出房间会先停止连接。继续吗？': '接続がまだ実行中です。退出すると先に接続を停止します。続けますか？',
    '当前连接还在运行。\n\n请先点「停止」，再释放主机身份 —— 否则正在你世界里的玩家会突然被断开。':
        '接続がまだ実行中です。\n\nホストを放棄する前に「停止」を押してください。そうしないとワールド内のプレイヤーが突然切断されます。',
    '恢复默认': '既定値に戻す',
    '基准配置': '基本設定',
    '本机连接要用的数字。改完必须按右边的「确定」才生效。':
        'この PC が接続に使う数字です。変更は右の「確定」を押したときだけ反映されます。',
    '主机 Host：Minecraft 局域网端口': 'ホスト：Minecraft LAN ポート',
    '游戏里「对局域网开放」时显示的那个端口。你开世界时用。':
        'Minecraft の「LAN に公開」で表示されるポートです。自分がワールドを開くときに使います。',
    '玩家 Player：本地代理端口': 'プレイヤー：ローカル中継ポート',
    '你作为玩家时，在 Minecraft 里连接 127.0.0.1:该端口。':
        'プレイヤーとして参加するとき、Minecraft から 127.0.0.1:このポート に接続します。',
    '确定': '確定',
    # ---- 基本設定の各行の即時フィードバック ----
    '✓ 已生效：%s':
        '✓ 反映しました：%s',
    '✓ 已生效：%d 人（建房时用）':
        '✓ 反映しました：%d 人（ルーム作成時に使用）',
    '✓ 已生效：%s（下次「开始连接」用新端口）':
        '✓ 反映しました：%s（次回の「接続開始」で新しいポートを使用）',
    '未提交：改了请按「确定」':
        '未反映 — 「確定」を押してください',
    '🔒 已锁定 —— 房间已建，要改请先删房重建':
        '🔒 ロック中 — ルームが存在します。変更するには削除して作り直してください',
    '修改本地代理端口': 'ローカルプロキシポートの変更',
    'host 已断开连接':
        'host が切断しました',
    'host 已断开连接，host 位置暂无人认领。\n\n如果还需要联机，请等有人认领 host 并开服之后，再手动点「开始连接」。':
        'host が切断し、host の席は空きました。\n\nまだ一緒に遊ぶ場合は、誰かが host を引き受けて開服したあと、手動で「接続を開始」を押してください。',
    'host 已断开连接，host 位置暂无人认领':
        'host が切断（host の席は空き）',
    'host 已断开连接（未开服）':
        'host が切断（未開服）',
    'host 已断开连接（不自动重连，可手动「开始连接」）':
        'host が切断（自動再接続なし・「接続を開始」で手動再接続）',
    'host 闪断（在连接态：优先复用现有连接，不能复用则自动重连）':
        'host が瞬断（接続中：使える既存リンクを再利用、だめなら自動再接続）',
    'host 闪断（你原本空闲，不自动重连）':
        'host が瞬断（もともと待機中なので自動再接続しません）',
    'host 未开服':
        'host が未開服',
    'host 未开服（等待 host 开始连接）':
        'host が未開服（host の接続開始待ち）',
    'host 那边现在没有开服，连上也没用。\n\n房间还在 —— 等 host 开始连接之后，你再手动点「开始连接」。':
        'いま host は開服していないので、接続しても意味がありません。\n\nルームは残っています —— host が接続を開始したら、手動で「接続を開始」を押してください。',
    '（当前：':
        '（現在：',
    '把本地代理端口改成 {0} 需要重建这一条隧道：\n\n· 当前游戏连接会断开，需要在 Minecraft 里重新进入一次；\n· 隧道会自动重建、自动重连 —— 你不需要手动点任何东西；\n· 改完之后你的连接状态和现在一样。\n\n确定要现在修改吗？':
        'ローカルプロキシポートを {0} に変更すると、このトンネルを再構築します：\n\n'
        '· 現在のゲーム接続は切断され、Minecraft で入り直す必要があります。\n'
        '· トンネルは自動で再構築・再接続されます（手動操作は不要です）。\n'
        '· 完了後の接続状態は今と同じです。\n\n'
        '今すぐ変更しますか？',
    '已取消 —— 仍是 %s': 'キャンセルしました — %s のままです',
    '✗ 还没填 —— 端口不能为空':
        '✗ 未入力 — ポートは必須です',
    '✗ 「%s」不是数字':
        '✗ 「%s」は数字ではありません',
    '✗ %s 超出范围（只能是 1-65535）':
        '✗ %s は範囲外です（1〜65535）',
    '✗ 超出范围（只能是 %d-%d 人）':
        '✗ 範囲外です（%d〜%d 人）',
    '单个房间最多几个人（含房主），2-64 人。只在你【建房】时生效。':
        '1 ルームに入れる人数（ホスト含む）、2〜64 人。ルームを【作成】するときにだけ使われます。',
    '⚠ 房间一旦建好，上面三个数字就锁定了 —— 隧道在建房那一刻就建好了。想改必须先删掉房间，改完再重新建房。':
        '⚠ ルームを作成すると上の数字はロックされます（トンネルは作成した瞬間に作られるため）。変更するにはルームを削除し、変更後に作り直してください。',
    '这个数字已经锁定': 'この数字はロックされています',
    '房间一旦建好，就不能再改{what}了。\n\n原因：隧道是在建房那一刻按这个数字建好的。再改只会在新参数上又建一批，两批隧道同时活着，玩家就会连到旧的那一批上（连接丢失）。\n\n这一条对谁都一样 —— 房主、接管的 host、服务器管理员都不能改。\n\n要换参数：先「删除我的房间」，改完再重新建房。':
        'ルームを作成すると {what} は変更できません。\n\n理由：トンネルはルーム作成の瞬間にこの数字で作られます。変更すると新しい設定でもう一組作られ、両方が生き残り、プレイヤーは古い方に割り当てられて接続が切れます。\n\nこれは作成者・ホストを引き継いだ人・サーバー管理者の全員に適用されます。\n\n変更するには「自分のルームを削除」を押し、変更してから作り直してください。',
    '主机 Host 的局域网端口': 'ホストの LAN ポート',
    '这个房间已经有主机了，你将按【玩家 Player】连接。想自己开世界请换一个房间号。':
        'このルームにはすでにホストがいます。プレイヤーとして接続します。自分でワールドを開く場合は別のルーム名を使ってください。',
    '房间一旦建好，{what}就锁定了（隧道在建房那一刻就建好了）—— 请先删掉房间，改完再重新建房。':
        'ルームを作成すると {what} はロックされます（トンネルは作成の瞬間に作られます）—— ルームを削除し、変更してから作り直してください。',
    '还没填「主机 Host：Minecraft 局域网端口」。\n\n请在上面填好，然后按右边的「确定」。':
        '「ホスト：Minecraft LAN ポート」が空です。\n\n上に入力して右の「確定」を押してください。',
    '还没填「玩家 Player：本地代理端口」。\n\n请在上面填好，然后按右边的「确定」。':
        '「プレイヤー：ローカル中継ポート」が空です。\n\n上に入力して右の「確定」を押してください。',
    '端口必须是数字，你填的是「%s」。': 'ポートは数字で入力してください（入力値：「%s」）。',
    '这个端口不能为空。请先填一个 1-65535 之间的数字。':
        'このポートは空にできません。1〜65535 の数字を入力してください。',
    '端口超出范围': 'ポートが範囲外です',
    '端口只能是 1-65535，你填的是 %d。': 'ポートは 1〜65535 です（入力値：%d）。',
    '还没填': '未入力',
    '人数超范围': '人数が範囲外です',
    '人数上限必须是数字（%d-%d）。': '人数上限は数字で入力してください（%d〜%d）。',
    '我开世界，别人进我的房间': '自分がワールドを開く',
    '我进别人的房间': '他人のワールドに入る',
    '房主（建房的人）': '作成者（ルームを作った人）',
    '房间': 'ルーム',
    '房间不存在': 'ルームが存在しません',
    '房间关闭通知之后连接没有自行退出，已强制清理本地状态。': 'ルーム閉鎖の通知後も接続が終了しなかったため、ローカル状態を強制クリアしました。',
    '房间内': '室内',
    '房间内在线': 'ルーム内',
    '房间列表': 'ルーム一覧',
    '房间名': 'ルーム名',
    '房间名不合法': 'ルーム名が不正です',
    '房间名（自己取，中文 / 英文都行；同名就是同一个房间）': 'ルーム名（自由に決めてください。同じ名前は同じルームになります）',
    '房间已不存在': 'ルームはすでに存在しません',
    '房间已被管理关闭': 'ルームは管理者によって閉じられました',
    '房间还没有主机': 'ルームにはまだホストがいません',
    '房间还没有主机，无法开始连接': 'ルームにホストがいないため接続を開始できません',
    '房间里已经有主机了': 'ルームにはすでにホストがいます',
    '所有文件': 'すべてのファイル',
    '打开配置文件夹': '設定フォルダを開く',
    '控制连接已建立（已连上中转服务器）': '制御接続を確立しました（リレーサーバーに接続済み）',
    '提示': 'お知らせ',
    '操作': '操作',
    '文本文件': 'テキストファイル',
    '无法认领主机身份': 'ホストを引き受けられません',
    '日志': 'ログ',
    '日志已复制到剪贴板': 'ログをクリップボードにコピーしました',
    '暗色': 'ダーク',
    '服务器当前不要求登录，可以直接建房 / 进房。\n需要账号时再点「登录」即可。':
        'このサーバーはログイン不要です。そのままルームを作成・参加できます。\nアカウントが必要になったら「ログイン」を押してください。',
    '服务器要求登录，正在弹出登录窗口 ...': 'サーバーがログインを要求しています。ログイン画面を開きます ...',
    '未加入房间': 'ルーム未参加',
    '未测到': '未計測',
    '未登录': '未ログイン',
    '未登录，已取消连接。': '未ログインのため接続を中止しました。',
    '本地代理端口（Minecraft 将连接 127.0.0.1:该端口）':
        'ローカルプロキシのポート（Minecraft は 127.0.0.1:このポート に接続します）',
    '本机在房间里的角色，以及房间号': '本機のルーム内での役割とルーム名',
    '本地连接': 'ローカル接続',
    '正在停止 ...': '停止中 ...',
    '正在获取 ...': '取得中 ...',
    '正在退出 ...': 'ログアウト中 ...',
    '正在退出房间 ...': 'ルームから退出中 ...',
    '正在通知服务器退出登录 ...': 'サーバーにログアウトを通知中 ...',
    '流量经中转服务器转发，兼容性最好，推荐': '通信はリレー経由。互換性が最も高い',
    '测试服务器连接': 'サーバー接続を確認',
    '清空': 'クリア',
    '状态': '状態',
    '玩家 Player': 'プレイヤー',
    '现在没有房间（先点「创建房间」）': '現在ルームがありません（先に「ルーム作成」を押してください）',
    '登录': 'ログイン',
    '确定要退出当前账号吗？': '現在のアカウントからログアウトしますか？',
    '确认删除': '削除の確認',
    '离开后主机身份（如果你持有）会被释放。': '退出するとホストの位置（保持している場合）は解放されます。',
    '累计流量': '通信量',
    '累计流量：本次连接启动至今收发的总数据量。': '通信量：この接続の開始から現在までの送受信の合計。',
    '良好': '良好',
    '要以主机身份开世界，需要先创建房间。': 'ホストとしてワールドを開くには、先にルームを作成してください。',
    '认领主机身份 (host)': 'ホストを引き受ける',
    '认领失败': '引き受けに失敗しました',
    '语言': '言語',
    '请先停止连接': '先に接続を停止してください',
    '请先填写中转服务器地址': '先にリレーサーバーのアドレスを入力してください',
    '请先点「停止」，再切换账号。': 'アカウントを切り替える前に「停止」を押してください。',
    '请先点「停止」，再退出登录。': 'ログアウトする前に「停止」を押してください。',
    '请先登录账号再改名。': '名前を変更する前にアカウントへログインしてください。',
    '请先选择身份': '先に役割を選択してください',
    '账号': 'アカウント',
    '身份': '役割',
    '身份配置': '役割',
    '较差': 'やや悪い',
    '运行状态': '動作状況',
    '还没有可复制的地址：请先点「开始连接」。': 'コピーできる接続先がまだありません。先に「接続開始」を押してください。',
    '还没有连接地址。\n\n请先点「开始连接」。': '接続先がまだありません。\n\n先に「接続開始」を押してください。',
    '这个房间名已经有人用了': 'このルーム名はすでに使われています',
    '这个房间目前没有主机，进去也会连不上。\n\n等有人认领主机身份之后再开始连接。':
        'このルームには現在ホストがいないため、入っても接続できません。\n\n誰かがホストを引き受けてから接続してください。',
    '这个房间还没有主机。要以主机身份开始，需要先认领主机身份。\n\n现在认领吗？':
        'このルームにはまだホストがいません。ホストとして開始するには先に引き受ける必要があります。\n\n今すぐ引き受けますか？',
    '连接模式': '接続モード',
    '退出': 'ログアウト',
    '退出房间': 'ルーム退出',
    '退出登录': 'ログアウト',
    '退出登录会同时退出该房间。\n': 'ログアウトするとそのルームからも退出します。\n',
    '配置目录': '設定フォルダ',
    '释放主机身份': 'ホストを放棄',
    '释放后房间里就没有主机了，其他人可以重新认领。\n\n（你仍然留在房间里，只是不再担任主机。）\n\n确定吗？':
        '放棄するとルームにホストがいなくなり、他の人が引き受けられます。\n\n（ルームには残りますが、ホストではなくなります。）\n\nよろしいですか？',
    '需要先加入房间才能连接。': '接続するには先にルームへ参加してください。',
    '需要重新登录': '再ログインが必要です',
    '（未能确认服务器状态，按需要登录处理）': '（サーバーの状態を確認できませんでした。ログイン必須として処理します）',
    '（正在开服，可以开始连接）': '（公開中。接続を開始できます）',
    '；': '；',
    '🔒 未开放': '🔒 未開放',

    # ---- gui/auth_ui.py ----
    'CraftLink 修改用户名': 'CraftLink 名前変更',
    'CraftLink 房间列表': 'CraftLink ルーム一覧',
    'CraftLink 登录': 'CraftLink ログイン',
    '关闭': '閉じる',
    '删除选中的房间': '選択したルームを削除',
    '加入选中的房间': '選択したルームに参加',
    '取消': 'キャンセル',
    '只有「你自己开的」并且「房间里没有玩家」的房间才能删除。房主也可以在主界面点「删除我的房间」。':
        '削除できるのは、自分が作成したルームでプレイヤーがいないものだけです。作成者はメイン画面の「自分のルームを削除」も使えます。',
    '密码': 'パスワード',
    '已更新到新版本，请再输一次密码（之后会继续记住）': '新しいバージョンに更新されました。パスワードをもう一度入力してください（その後は再び記憶されます）',
    '当前没有在线房间（让房主先点「开始连接」）': '現在オンラインのルームはありません（作成者に「接続開始」を押してもらってください）',
    '当前用户名': '現在のユーザー名',
    '房主和玩家都需要账号（服务器已开启账号验证）': 'ホストもプレイヤーもアカウントが必要です',
    '房间号': 'ルーム',
    '操作失败': '操作に失敗しました',
    '改名失败': '名前の変更に失敗しました',
    '改名成功': '名前を変更しました',
    '新的用户名': '新しいユーザー名',
    '显示密码': 'パスワードを表示',
    '未知': '不明',
    '正在加载 ...': '読み込み中 ...',
    '正在改名 ...': '名前を変更中 ...',
    '正在获取房间列表 ...': 'ルーム一覧を取得中 ...',
    '用户名': 'ユーザー名',
    '用户名不能包含空格': 'ユーザー名に空白は使えません',
    '用户名就是游戏里和房间列表里显示的名字。改名立即生效，不会掉线，也不用重新登录。':
        'ユーザー名はゲーム内とルーム一覧に表示される名前です。変更はすぐに反映され、切断も再ログインも不要です。',
    '用户名最长 32 个字符': 'ユーザー名は最大32文字です',
    '登录 MC-Link 账号': 'CraftLink アカウントにログイン',
    '登录成功，但密码没能保存到本机（可能是目录权限问题）': 'ログインしましたが、パスワードを本機に保存できませんでした（フォルダの権限の問題かもしれません）',
    '确认改名': '変更する',
    '空闲隧道': '空きトンネル',
    '端口必须是数字': 'ポートは数字で入力してください',
    '记住密码（加密保存在本机；每次更新版本后需重新输入一次）': 'パスワードを記憶（本機に暗号化保存・更新後は再入力）',
    '请先选中一个房间': '先にルームを選択してください',
    '请填写密码': 'パスワードを入力してください',
    '请填写新的用户名': '新しいユーザー名を入力してください',
    '请填写用户名': 'ユーザー名を入力してください',
    '账号由管理员创建；忘记密码请联系服主重置。': 'アカウントは管理者が作成します。パスワードは鯖主に依頼してください。',
    '还没人认领': '未取得',
    '还没有登录，确定要退出 CraftLink 吗？': 'ログインしていません。CraftLink を終了しますか？',

    # ---- core/protocol.py ----
    '你不是这个房间的房主，只有房主能删除': 'あなたはこのルームのホストではありません。削除できるのはホストだけです',
    '协议握手被服务器拒绝': 'プロトコルハンドシェイクがサーバーに拒否されました',
    '尝试过于频繁，请稍后再试': '試行回数が多すぎます。しばらくしてからお試しください',
    '房间不存在或房主不在线': 'ルームが存在しないか、作成者がオフラインです',
    '房间不存在（可能已经关闭）': 'ルームが存在しません（すでに閉じられた可能性）',
    '房间名不能包含换行、制表符等控制字符': 'ルーム名に改行やタブなどの制御文字は使えません',
    '房间已关闭': 'ルームは閉じられました',
    '房间已满（达到最大人数）': 'ルームが満員です（上限人数に達しました）',
    '房间已被管理员关闭': 'ルームは管理者によって閉じられました',
    '房间里还有玩家，不能删除（请先让玩家离开）': 'ルームにプレイヤーが残っているため削除できません（先に退出してもらってください）',
    '改名失败，请联系管理员': '名前の変更に失敗しました。管理者に連絡してください',
    '服务器不认识该角色（客户端与服务端版本不匹配）': 'サーバーがこの役割を認識しません（クライアントとサーバーの版が不一致）',
    '服务器数据库暂时不可用，请稍后再试': 'サーバーのデータベースが一時的に利用できません。しばらくしてからお試しください',
    '服务器没有开启账号系统': 'サーバーはアカウント機能を有効にしていません',
    '未知错误': '不明なエラー',
    '本月的自助修改次数已用完（改名和改密码共用 3 次）': '今月の自己変更回数を使い切りました（名前変更とパスワード変更で合計3回）',
    '用户名不合法（2-32 个字符，不能有空格）': 'ユーザー名が不正です（2〜32文字、空白不可）',
    '用户名或密码格式不正确': 'ユーザー名またはパスワードの形式が正しくありません',
    '用户名或密码错误': 'ユーザー名またはパスワードが違います',
    '登录状态已失效，请重新登录': 'ログイン状態が失効しました。もう一度ログインしてください',
    '该房间已经有房主了': 'そのルームにはすでに作成者がいます',
    '请先登录账号': '先にアカウントにログインしてください',
    '账号已被管理员禁用': 'アカウントは管理者によって無効化されています',
    '这个房间名已经有人在用了。\n建房间请换一个名字，或者直接「加入房间」进他的房间。':
        'このルーム名はすでに使われています。\n作成するなら別の名前にするか、「ルーム参加」でそのルームに入ってください。',
    '这个用户名已经被别人用了': 'このユーザー名はすでに使われています',

    # ---- core/control_client.py ----
    '密码最长 128 个字符': 'パスワードは最大128文字です',
    '房间名为空': 'ルーム名が空です',
    '改名和改密码不限次数（管理员及以上）': '名前とパスワードの変更は回数制限なし（管理者以上）',
    '新用户名和当前用户名相同': '新しいユーザー名が現在と同じです',
    '有玩家': 'プレイ中',
    '服务器没有响应，请稍后再试': 'サーバーが応答しません。しばらくしてからお試しください',
    '服务器没有返回任何内容': 'サーバーから何も返されませんでした',
    '服务器没有返回登录凭证（可能是旧版服务端）': 'サーバーがログイン情報を返しませんでした（旧版サーバーの可能性）',
    '本月的自助修改次数已用完（每月 3 次，改名和改密码共用）': '今月の自己変更回数を使い切りました（月3回、名前変更とパスワード変更で共用）',
    '根管理员': 'ルート管理者',
    '用户名不能包含空格或特殊字符': 'ユーザー名に空白や特殊文字は使えません',
    '用户名和密码不能包含空格': 'ユーザー名とパスワードに空白は使えません',
    '用户名至少 2 个字符': 'ユーザー名は2文字以上必要です',
    '空闲': '空き',
    '管理员': '管理者',
    '请先登录': '先にログインしてください',
    '请先登录账号再改名': '名前を変更する前にアカウントへログインしてください',
    '请输入密码': 'パスワードを入力してください',
    '请输入新的用户名': '新しいユーザー名を入力してください',
    '请输入用户名': 'ユーザー名を入力してください',
    '（还没人认领）': '（未取得）',

    # ---- core/room.py ----
    '与服务器的连接已断开': 'サーバーとの接続が切断されました',
    '人数必须是数字': '人数は数字で入力してください',
    '已获得房主身份': '作成者権限を取得しました',
    '已释放房主身份': '作成者権限を放棄しました',
    '成员': 'メンバー',
    '房主': '作成者',
    '房间不存在（可能已被管理员关闭）': 'ルームが存在しません（管理者が閉じた可能性）',
    '房间已被关闭': 'ルームは閉じられました',
    '持有房主身份': '作成者権限を保持',
    '无房主': '作成者なし',
    '有房主': '作成者あり',
    '服务器没有响应就关闭了连接': 'サーバーは応答せずに接続を閉じました',
    '管理员关闭了房间': '管理者がルームを閉じました',

    # ---- core/session.py ----
    '尚未启动，无法删除房间': '起動していないためルームを削除できません',
    '服务器没有确认删除请求（可能需要重试）': 'サーバーが削除要求を確認しませんでした（再試行が必要かもしれません）',
    '未知结果': '不明な結果',
    '登录状态已失效': 'ログイン状態が失効しました',
    '等待中': '待機中',
    '运行中': '実行中',
    '连接中': '接続中',

    # ---- core/host_core.py ----
    '              MC-LINK  主机 (Host)': '              MC-LINK  ホスト (Host)',
    '[CONTROL] 已请求删除房间，等待服务器确认 ...': '[CONTROL] ルーム削除を要求、サーバーの確認待ち ...',
    '中转控制连接被关闭': 'リレーが制御接続を閉じました',
    '中转服务器关闭了控制连接': 'リレーサーバーが制御接続を閉じました',
    '中转服务器关闭了隧道': 'リレーサーバーがトンネルを閉じました',
    '主机已停止。': 'ホストを停止しました。',
    '主机已启动，等待玩家连接。': 'ホストを開始しました。プレイヤーの接続を待っています。',
    '房间已删除': 'ルームを削除しました',
    '控制连接未建立，无法删除房间': '制御接続がないためルームを削除できません',
    '正在停止主机 ...': 'ホストを停止中 ...',
    '隧道在分配前被关闭': 'トンネルは割り当ての前に閉じられました',

    # ---- core/player_core.py ----
    '              MC-LINK  玩家 (Player)': '              MC-LINK  プレイヤー (Player)',
    '[LOCAL] 请在 Minecraft 中连接：': '[LOCAL] Minecraft で接続してください：',
    '[RELAY] PLAYER 握手已发送': '[RELAY] PLAYER ハンドシェイク送信済み',
    '中转服务器关闭了连接': 'リレーサーバーが接続を閉じました',
    '正在停止玩家模式 ...': 'プレイヤーモードを停止中 ...',
    '玩家模式已停止。': 'プレイヤーモードを停止しました。',

    # ---- core/engine.py ----
    '当前没有正在运行的连接': '現在実行中の接続はありません',
    '操作超时': '操作がタイムアウトしました',
    '服务器没有响应（连接被立即关闭）': 'サーバーが応答しません（接続がすぐに閉じられました）',
    '未启动': '停止中',
    '网络线程尚未启动': 'ネットワークスレッドがまだ起動していません',
    '还没有加入房间': 'まだルームに参加していません',
    '还没有进入房间': 'まだルームに入っていません',
    '连接超时，可能被防火墙/安全组拦截': '接続がタイムアウトしました。ファイアウォールやセキュリティグループに遮断されている可能性があります',
    '错误': 'エラー',

    # ---- core/presence.py ----
    '在线状态连接被关闭': 'オンライン状態の接続が閉じられました',
    '已上报在线状态': 'オンライン状態を送信しました',
    '服务器不支持在线状态上报（旧版中继），跳过': 'サーバーがオンライン状態の送信に対応していません（旧版リレー）。スキップします',

    # ---- core/reliable.py ----
    '会话已关闭': 'セッションは閉じられました',
    '可靠流握手超时（对端没有响应）': '可靠ストリームのハンドシェイクがタイムアウトしました（相手が応答しません）',
    '对端重置了连接': '相手が接続をリセットしました',
    '对端长时间无响应': '相手が長時間応答しません',
    '重传次数超限，路径可能已失效': '再送回数が上限を超えました。経路が使えなくなった可能性があります',

    # ---- config/settings.py ----
    'Minecraft 局域网端口必须在 1-65535 之间': 'Minecraft LANポートは 1〜65535 の範囲で入力してください',
    'Minecraft 局域网端口必须是数字': 'Minecraft LANポートは数字で入力してください',
    '中转服务器端口必须在 1-65535 之间': 'リレーサーバーのポートは 1〜65535 の範囲で入力してください',
    '中转服务器端口必须是数字': 'リレーサーバーのポートは数字で入力してください',
    '本地代理端口必须在 1-65535 之间': 'ローカルプロキシのポートは 1〜65535 の範囲で入力してください',
    '本地代理端口必须是数字': 'ローカルプロキシのポートは数字で入力してください',
    '玩家': 'プレイヤー',
    '请先选择身份（主机 Host 或玩家 Player）': '先に役割を選択してください（ホスト または プレイヤー）',
    '请填写中转服务器地址': 'リレーサーバーのアドレスを入力してください',
    '请填写房间名': 'ルーム名を入力してください',

    # ---- gui/theme.py + language picker ----
    'English': 'English',
    'MC-Link 房间列表': 'CraftLink ルーム一覧',
    '中文': '中文',
    '亮色': 'ライト',
    '亮色 · 晨曦': 'ライト · ドーン',
    '亮色 · 薄荷': 'ライト · ミント',
    '日本語': '日本語',
    '暗色 · 星云': 'ダーク · ネビュラ',
    '暗色 · 极光': 'ダーク · オーロラ',
    '跟随系统': 'システム',
    '还没有登录，确定要退出 MC-Link 吗？': 'ログインしていません。CraftLink を終了しますか？',

    # ---- 3.0.9: ルームメンバー一覧 + キック（KICK）----
    '房间成员': 'ルームメンバー',
    '房间成员列表（房间里的人，实时更新）':
        'ルームメンバー一覧（ルーム内の全員・リアルタイム更新）',
    '连接状态': '接続状態',
    '未连接': '未接続',
    '等待配对': 'ペアリング待ち',
    '配对中': 'ペアリング中',
    '入站档位': '受信の段階',
    'IP:端口': 'IP:ポート',
    '延迟': '遅延',
    # 対向遅延（プレイヤー ↔ ホスト の直結 RTT）。N/C は記号なので訳さない。
    '对端延迟': 'ピア遅延',
    'N/C': 'N/C',
    '<1 ms': '<1 ms',
    '你的名字：': 'あなたの名前：',
    '踢出': 'キック',
    '主机': 'ホスト',
    '已连接': '接続済み',
    '直连': '直結',
    '打洞中': 'ホールパンチ中',
    '已打通': '開通済み',
    '第 {0} 次': '試行 {0} 回目',
    '对端': '相手',
    '踢出成员': 'メンバーをキック',
    '确定要把该成员踢出房间吗？': 'このメンバーをルームからキックしますか？',
    '确定要把「{0}」踢出房间吗？': '「{0}」をルームからキックしますか？',
    '已踢出成员': 'メンバーをキックしました',
    '该成员已被踢出房间。': 'メンバーをルームからキックしました。',
    '已踢出房主，房间已解散。':
        '作成者をキックしたため、ルームは解散しました。',
    '踢人失败': 'キックに失敗しました',
    '你已被踢出房间': 'キックされました',
    '你已被踢出房间。': 'ルームからキックされました。',
    '你已被移出该房间，已自动退出。':
        'ルームから削除され、自動的に退出しました。',
    '发起者：': '実行者：',
    '原因：': '理由：',
    '被踢出房间': 'ルームからキックされました',
    '房主被踢出（房间已解散）': '作成者がキックされました（ルームは解散）',
    '该成员不在房间里（可能已经离开）': 'そのメンバーはルームにいません（退出した可能性があります）',
    '你没有踢人的权限（只有房主或当前主机可以）':
        'キックの権限がありません（作成者または現在のホストのみ可能です）',

}



# ============================================================
# 动态文案（正则句式）
# ============================================================
#
# 日志和提示里大量是 f-string：`f"正在{action}房间「{room}」..."`。拼完之后
# 原文已经不在了，查表永远查不到。所以这类句子用正则匹配"句式"，把括号里的
# 变量原样搬到译文里。
#
# 每条是 (编译好的正则, 替换模板)，用 re.sub 执行；模板里 \1 \2 引用捕获组。
# 正则一律 ^...$ 锚定整条消息：只匹配整句，绝不吞掉半行。


def _en():
    return [

        (re.compile('^主机：(.*?)（正在开服，可以开始连接）$'), 'Host: \\1 (serving, you can start connecting)'),
        (re.compile('^(.*?)\u3000房间内 (.*?) 人\u3000已加入世界 (.*?) 人\\\n(.*?)\u3000（你是房主）$'), '\\1\u3000in room \\2\u3000in world \\3\n\\4\u3000(you are the host)'),
        (re.compile('^2\\-32 个字符，不能有空格。(.*?)$'), '2-32 characters, no spaces. \\1'),
        (re.compile('^\\[CONTROL\\] 控制连接已建立 room=(.*?)$'), '[CONTROL] control connection established room=\\1'),
        (re.compile('^\\[CONTROL\\] 控制连接断开: (.*?)$'), '[CONTROL] control connection closed: \\1'),
        (re.compile('^\\[CONTROL\\] 正在连接中转服务器 (.*?):(.*?) \\.\\.\\.$'), '[CONTROL] connecting to the relay server \\1:\\2 ...'),
        (re.compile('^\\[CONTROL\\] 登录状态失效：(.*?)$'), '[CONTROL] sign-in expired: \\1'),
        (re.compile('^\\[CONTROL\\] 连接失败: (.*?)$'), '[CONTROL] connection failed: \\1'),
        (re.compile('^\\[LOCAL\\] 启动失败：(.*?)$'), '[LOCAL] start failed: \\1'),
        (re.compile('^\\[PLAYER (.*?)\\] Minecraft 已连接 peer=(.*?)$'), '[PLAYER \\1] Minecraft connected peer=\\2'),
        (re.compile('^\\[PLAYER (.*?)\\] 原始数据转发开始$'), '[PLAYER \\1] raw data forwarding started'),
        (re.compile('^\\[PLAYER (.*?)\\] 原始数据转发结束 上行=(.*?)B 下行=(.*?)B$'), '[PLAYER \\1] raw data forwarding finished upload=\\2B download=\\3B'),
        (re.compile('^\\[PLAYER (.*?)\\] 已接入房主，开始转发 Minecraft 数据$'), '[PLAYER \\1] paired with the host, forwarding Minecraft data'),
        (re.compile('^\\[PLAYER (.*?)\\] 已断开$'), '[PLAYER \\1] disconnected'),
        (re.compile('^\\[PLAYER (.*?)\\] 等待中转服务器配对 \\.\\.\\.$'), '[PLAYER \\1] waiting for the relay server to pair ...'),
        (re.compile('^\\[PLAYER (.*?)\\] 错误: (.*?)$'), '[PLAYER \\1] error: \\2'),
        (re.compile('^\\[RELAY\\] 正在连接中转服务器 (.*?):(.*?) \\.\\.\\.$'), '[RELAY] connecting to the relay server \\1:\\2 ...'),
        (re.compile('^\\[TUNNEL (.*?)\\] Minecraft 数据转发开始 session=(.*?)$'), '[TUNNEL \\1] Minecraft data forwarding started session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] Minecraft 数据转发结束 session=(.*?) 上行=(.*?)B 下行=(.*?)B$'), '[TUNNEL \\1] Minecraft data forwarding finished session=\\2 upload=\\3B download=\\4B'),
        (re.compile('^\\[TUNNEL (.*?)\\] READY 已发送 session=(.*?)$'), '[TUNNEL \\1] READY sent session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] (.*?)s 后重连 \\.\\.\\.$'), '[TUNNEL \\1] reconnecting in \\2s ...'),
        (re.compile('^\\[TUNNEL (.*?)\\] 会话结束 session=(.*?)$'), '[TUNNEL \\1] session finished session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] 已分配玩家 session=(.*?)$'), '[TUNNEL \\1] player assigned session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] 本机 Minecraft 已连接 session=(.*?)$'), '[TUNNEL \\1] local Minecraft connected session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] 正在建立隧道 \\.\\.\\.$'), '[TUNNEL \\1] establishing the tunnel ...'),
        (re.compile('^\\[TUNNEL (.*?)\\] 正在连接本机 Minecraft (.*?):(.*?) \\.\\.\\.$'), '[TUNNEL \\1] connecting to local Minecraft \\2:\\3 ...'),
        (re.compile('^\\[TUNNEL (.*?)\\] 空闲等待中 \\(READY / WAITING\\)$'), '[TUNNEL \\1] idle, waiting (READY / WAITING)'),
        (re.compile('^\\[TUNNEL (.*?)\\] 等待房主就绪…（(.*?)）$'), '[TUNNEL \\1] waiting for the host to be ready… (\\2)'),
        (re.compile('^\\[TUNNEL (.*?)\\] 错误: (.*?)$'), '[TUNNEL \\1] error: \\2'),
        (re.compile('^(.*?)\\\n\\\n如果房间正在运行，房间列表里的房主名字会在下一次刷新时更新。$'), '\\1\n\nIf the room is running, the owner name in the room list will update on the next refresh.'),
        (re.compile('^(.*?)\\\n\\\n请重新创建或加入房间。$'), '\\1\n\nPlease create or join a room again.'),
        (re.compile('^(.*?)\\\n\\\n请重新登录后再连接。$'), '\\1\n\nPlease sign in again before connecting.'),
        (re.compile('^(.*?) v(.*?) \\(构建 (.*?)\\)$'), '\\1 v\\2 (build \\3)'),
        (re.compile('^(\\d+) 人$'), '\\1 players'),
        (re.compile('^(.*?) 人\u3000\\(你是房间所有者\\)$'), '\\1 players\u3000(you own this room)'),
        (re.compile('^(.*?) 已启动$'), '\\1 started'),
        (re.compile('^(.*?) 绑的是 (.*?)，程序读的是 (.*?)$'), '\\1 is bound to \\2, the program reads \\3'),
        (re.compile('^(.*?)\u3000房间内 (.*?) 人\u3000已加入世界 (.*?) 人\\\n(.*?)$'), '\\1\u3000in room \\2\u3000in world \\3\n\\4'),
        (re.compile('^已创建房间「(.+?)」$'), 'Created room "\\1"'),
        (re.compile('^已加入房间「(.+?)」$'), 'Joined room "\\1"'),
        (re.compile('^创建房间失败$'), 'Failed to create the room'),
        (re.compile('^加入房间失败$'), 'Failed to join the room'),
        (re.compile('^创建房间失败：(.+)$'), 'Failed to create the room: \\1'),
        (re.compile('^加入房间失败：(.+)$'), 'Failed to join the room: \\1'),
        (re.compile('^(.+?)（根管理员）$'), '\\1 (root admin)'),
        (re.compile('^(.+?)（管理员）$'), '\\1 (admin)'),
        (re.compile('^(.+?)（玩家）$'), '\\1 (player)'),
        (re.compile('^(.+?)（空闲）$'), '\\1 (idle)'),
        (re.compile('^(.+?)（有玩家）$'), '\\1 (players)'),
        (re.compile('^(.+?)（根管理员，(.+?)，房间内 (.+?) 人）$'), '\\1 (root admin, \\2, \\3 in room)'),
        (re.compile('^(.+?)（管理员，(.+?)，房间内 (.+?) 人）$'), '\\1 (admin, \\2, \\3 in room)'),
        (re.compile('^(.+?)（玩家，(.+?)，房间内 (.+?) 人）$'), '\\1 (player, \\2, \\3 in room)'),
        (re.compile('^(.+?)（未开服）$'), '\\1 (not serving)'),
        (re.compile('^↑(.*?)  ↓(.*?)  \\| 隧道 (.*?)/(.*?) 就绪$'), '↑\\1  ↓\\2  | tunnel \\3/\\4 ready'),
        (re.compile('^● 服务器正常 (.*?) ms$'), '● Server OK \\1 ms'),
        (re.compile('^与房间的连接已断开：(.*?)$'), 'The connection to the room was closed: \\1'),
        (re.compile('^中转服务器 : (.*?):(.*?)$'), 'Relay server : \\1:\\2'),
        (re.compile('^中转服务器应答异常: (.*?)$'), 'The relay server answered unexpectedly: \\1'),
        (re.compile('^中转服务器正常，往返延迟约 (.*?) ms（当前 (.*?) 个房间在线）$'), 'Relay server OK, round trip about \\1 ms (\\2 rooms online)'),
        (re.compile('^中转服务器正常，往返延迟约 (.*?) ms（当前没有房间在线）$'), 'Relay server OK, round trip about \\1 ms (no rooms online)'),
        (re.compile('^主机身份被别人抢先了：(.*?)$'), 'Somebody else claimed the host slot first: \\1'),
        (re.compile('^主机：(.*?)$'), 'Host: \\1'),
        (re.compile('^人数上限 (.*?) 超过服务器上限 (.*?)，已阻止建房$'), 'Max players \\1 is above the server limit \\2, room creation blocked'),
        (re.compile('^人数上限只能是 (.*?)\\-(.*?) 人。服务器允许的最大值是 (.*?) 人（这是单个房间的上限，不是所有房间加起来）。$'), 'Max players can only be \\1-\\2. The server allows at most \\3 (per room, not in total).'),
        (re.compile('^人数上限只能是 (.*?)\\-(.*?)，你填的是 (.*?)。\\\n\\\n服务器允许的最大值是 (.*?) 人（单房间，不是所有房间加起来）。$'), 'Max players can only be \\1-\\2, you entered \\3.\n\nThe server allows at most \\4 players (per room, not in total).'),
        (re.compile('^人数上限必须是数字（(.*?)\\-(.*?)）$'), 'Max players must be a number (\\1-\\2)'),
        (re.compile('^你设置的人数上限是 (.*?) 人，但中转服务器的上限是 (.*?) 人。\\\n\\\n房间人数不能超过服务器上限 —— 要么把这里改成 (.*?) 或更小，要么请管理员在管理面板里把上限调高。$'), 'You set the player limit to \\1, but the relay server allows \\2.\n\nThe room size cannot exceed the server limit — either change this to \\3 or less, or ask an admin to raise the limit in the panel.'),
        (re.compile('^你还在房间「(.*?)」里。\\\n\\\n$'), 'You are still in room "\\1".\n\n'),
        (re.compile('^你还在房间「(.*?)」里。\\\n\\\n你是这个房间的所有者，退出登录会同时关闭该房间。\\\n退出登录会同时退出该房间。\\\n$'), 'You are still in room "\\1".\n\nYou own this room; signing out will also close it.\nSigning out also leaves that room.\n'),
        (re.compile('^你还在房间「(.*?)」里。\\\n\\\n你是这个房间的所有者，退出登录会同时关闭该房间。\\\n退出登录会同时退出该房间。\\\n\\\n确定要退出登录吗？$'), 'You are still in room "\\1".\n\nYou own this room; signing out will also close it.\nSigning out also leaves that room.\n\nSign out anyway?'),
        (re.compile('^你还没有进入房间 (.*?)。\\\n\\\n$'), 'You have not entered room \\1 yet.\n\n'),
        (re.compile('^你还没有进入房间 (.*?)。\\\n\\\n要以主机身份开世界，需要先创建房间。需要先加入房间才能连接。$'), 'You have not entered room \\1 yet.\n\nTo open a world as host, create a room first.Join a room before connecting.'),
        (re.compile('^你还没有进入房间 (.*?)。\\\n\\\n要以主机身份开世界，需要先创建房间。需要先加入房间才能连接。\\\n\\\n现在就去吗？$'), 'You have not entered room \\1 yet.\n\nTo open a world as host, create a room first.Join a room before connecting.\n\nGo there now?'),
        (re.compile('^停止时出错：(.*?)$'), 'Error while stopping: \\1'),
        (re.compile('^共 (.*?) 个房间，(.*?) 个有玩家（自动刷新，双击一行即可加入）$'), '\\1 rooms in total, \\2 with players (auto refresh, double-click a row to join)'),
        (re.compile('^共 (.*?) 个房间，其中 (.*?) 个有玩家（(.*?) 刷新）$'), '\\1 rooms in total, \\2 of them with players (\\3 refresh)'),
        (re.compile('^删除失败：(.*?)$'), 'Delete failed: \\1'),
        (re.compile('^删除房间失败：(.*?)$'), 'Failed to delete the room: \\1'),
        (re.compile('^刷新失败：(.*?)$'), 'Refresh failed: \\1'),
        (re.compile('^刷新房间列表失败：(.*?)$'), 'Failed to refresh the room list: \\1'),
        (re.compile('^单个房间的人数上限，默认 (.*?) 人（含主机）。上限由服务器统一设置，超出会提示重填。$'), 'Player limit for a single room, \\1 by default (host included). The limit is set by the server; going over it asks you to re-enter.'),
        (re.compile('^参数错误：(.*?)$'), 'Invalid argument: \\1'),
        (re.compile('^发生错误：(.*?)$'), 'An error occurred: \\1'),
        (re.compile('^发送删除请求失败：(.*?)$'), 'Failed to send the delete request: \\1'),
        (re.compile('^发送失败：(.*?)$'), 'Send failed: \\1'),
        (re.compile('^启动 (.*?) 模式 \\| 房间 (.*?) \\| 账号 (.*?) \\| 服务器 (.*?):(.*?)$'), 'Starting \\1 mode | room \\2 | account \\3 | server \\4:\\5'),
        (re.compile('^启动失败：(.*?)$'), 'Start failed: \\1'),
        (re.compile('^在 Minecraft 中连接：(.*?)$'), 'Connect in Minecraft: \\1'),
        (re.compile('^在线状态上报被拒绝：(.*?)$'), 'Presence report rejected: \\1'),
        (re.compile('^在线状态连接断开（(.*?)），(.*?) 秒后重连$'), 'Presence connection lost (\\1), reconnecting in \\2 seconds'),
        (re.compile('^处理数据包失败：(.*?)$'), 'Failed to handle a packet: \\1'),
        (re.compile('^复制失败：(.*?)$'), 'Copy failed: \\1'),
        (re.compile('^已从房间列表选择：(.*?)$'), 'Selected from the room list: \\1'),
        (re.compile('^已填入保存的账号 (.*?)$'), 'Filled in the saved account \\1'),
        (re.compile('^已复制 Minecraft 连接地址：(.*?)$'), 'Copied the Minecraft address: \\1'),
        (re.compile('^已改名为 (.*?)，立即生效（无需重新登录）(.*?)$'), 'Renamed to \\1, effective immediately (no need to sign in again)\\2'),
        (re.compile('^已改名为：(.*?)（立即生效，未断开连接）$'), 'Renamed to: \\1 (effective immediately, connection kept)'),
        (re.compile('^已生成随机房间号：(.*?)$'), 'Generated a random room name: \\1'),
        (re.compile('^已登录：(.*?)$'), 'Signed in: \\1'),
        (re.compile('^已自动退出房间：(.*?)$'), 'Automatically left the room: \\1'),
        (re.compile('^已获得主机身份：(.*?)$'), 'Claimed the host slot: \\1'),
        (re.compile('^已退出登录：(.*?)（服务器已同步）$'), 'Signed out: \\1 (server synchronised)'),
        (re.compile('^已退出登录：(.*?)（未能通知服务器，稍后会自动失效）$'), 'Signed out: \\1 (could not notify the server, it will expire later)'),
        (re.compile('^已选择房间 (.*?)，回到主界面点「开始连接」$'), 'Selected room \\1, go back to the main window and press "Start"'),
        (re.compile('^已释放主机身份：(.*?)$'), 'Released the host slot: \\1'),
        (re.compile('^当前地址是「(.*?)」，不是 Minecraft 连接地址（只有玩家模式才有 127\\.0\\.0\\.1 地址）。$'), 'The current address is "\\1", which is not the Minecraft address (only player mode has a 127.0.0.1 address).'),
        (re.compile('^房间 (.*?) 已删除$'), 'Room \\1 deleted'),
        (re.compile('^房间 (.*?) 是「(.*?)」建的，当前主机是「(.*?)」，都不是你。\\\n只有建房的人或当前主机能删除房间。$'), 'Room \\1 was created by "\\2" and the current host is "\\3", neither is you.\nOnly the creator or the current host can delete a room.'),
        (re.compile('^房间 (.*?) 里还有 (.*?) 名玩家。\\\n\\\n请先让玩家退出 Minecraft，或者由服主在管理面板强制关闭。$'), 'Room \\1 still has \\2 players.\n\nAsk the players to leave Minecraft first, or have the owner force-close it in the admin panel.'),
        (re.compile('^房间 (.*?) 里还有 (.*?) 名玩家。\\\n\\\n请先让玩家退出，或者让服主在管理面板强制关闭。$'), 'Room \\1 still has \\2 players.\n\nAsk the players to leave first, or have the owner force-close it in the admin panel.'),
        (re.compile('^房间「(.*?)」已存在，改为加入$'), 'Room "\\1" already exists, joining it instead'),
        (re.compile('^房间「(.*?)」已经由「(.*?)」建好了，现在里面 (.*?) 人。\\\n\\\n点「是」= 直接加入他的房间（不再新建）\\\n点「否」= 取消，我换一个房间名$'), 'Room "\\1" was already created by "\\2", it currently has \\3 people.\n\nPress "Yes" = join his room directly (do not create a new one)\nPress "No" = cancel, I will pick another room name'),
        (re.compile('^房间事件没有送达界面（已按最新状态重画）：(.*?)$'), 'A room event did not reach the UI (redrawn from the latest state): \\1'),
        (re.compile('^房间人数已设为 (.*?)$'), 'Room size set to \\1'),
        (re.compile('^房间人数：(.*?)$'), 'Players in the room: \\1'),
        (re.compile('^房间号     : (.*?)$'), 'Room name  : \\1'),
        (re.compile('^房间名「(.*?)」已被占用，请换一个名字$'), 'The room name "\\1" is taken, please pick another one'),
        (re.compile('^房间名不能为空。可以写中文、英文、数字和空格，最长 (.*?) 个字符。$'), 'The room name cannot be empty. Chinese, English, digits and spaces are allowed, up to \\1 characters.'),
        (re.compile('^房间名最长 (.*?) 个字符（你填了 (.*?) 个）$'), 'The room name can be at most \\1 characters (you entered \\2)'),
        (re.compile('^房间已不存在：(.*?)$'), 'The room no longer exists: \\1'),
        (re.compile('^房间已被关闭：(.*?)$'), 'The room has been closed: \\1'),
        (re.compile('^房间已被服务器关闭：(.*?)$'), 'The room was closed by the server: \\1'),
        (re.compile('^探测失败：(.*?)$'), 'Probe failed: \\1'),
        (re.compile('^控制连接应答异常: (.*?)$'), 'The control connection answered unexpectedly: \\1'),
        (re.compile('^控制连接断开：(.*?)，正在重连 \\.\\.\\.$'), 'Control connection lost: \\1, reconnecting ...'),
        (re.compile('^操作失败：(.*?)$'), 'Action failed: \\1'),
        (re.compile('^改名失败：(.*?)$'), 'Rename failed: \\1'),
        (re.compile('^无法在本机监听 (.*?):(.*?)：(.*?)$'), 'Cannot listen on \\1:\\2: \\3'),
        (re.compile('^无法连接 (.*?):(.*?)（(.*?)）$'), 'Cannot connect to \\1:\\2 (\\3)'),
        (re.compile('^无法连接中转服务器（(.*?):(.*?)）：(.*?)$'), 'Cannot connect to the relay server (\\1:\\2): \\3'),
        (re.compile('^日志已保存：(.*?)$'), 'Log saved: \\1'),
        (re.compile('^普通账号每月最多 (.*?) 次自助修改（改名和改密码共用）；管理员不受限制。$'), 'A normal account can make at most \\1 self-service changes per month (shared between rename and password); admins are not limited.'),
        (re.compile('^有玩家接入：session=(.*?) 隧道=(.*?)$'), 'Player joined: session=\\1 tunnel=\\2'),
        (re.compile('^服务器 (.*?)，协议版本 v(.*?)$'), 'Server \\1, protocol version v\\2'),
        (re.compile('^服务器在线，但版本较旧（(.*?) ms）。请把服务端也更新到 v2 才能使用账号登录。$'), 'The server is online but runs an older version (\\1 ms). Update the server to v2 to use account sign-in.'),
        (re.compile('^服务器应答异常：(.*?)$'), 'The server answered unexpectedly: \\1'),
        (re.compile('^服务器返回错误：(.*?)$'), 'The server returned an error: \\1'),
        (re.compile('^服务器：(.*?)\u3000协议版本：v(.*?)$'), 'Server: \\1\u3000protocol version: v\\2'),
        (re.compile('^服务器：(.*?)\u3000协议版本：v(.*?)\u3000(.*?)$'), 'Server: \\1\u3000protocol version: v\\2\u3000\\3'),
        (re.compile('^本地代理已监听 (.*?)$'), 'The local proxy is listening on \\1'),
        (re.compile('^本地监听   : (.*?)$'), 'Local listen : \\1'),
        (re.compile('^本地端口监听失败：(.*?)$'), 'Failed to listen on the local port: \\1'),
        (re.compile('^本地连接 (.*?)$'), 'Local connections \\1'),
        (re.compile('^本月还可以自助修改 (.*?) 次（改名和改密码共用）$'), '\\1 self-service changes left this month (shared between rename and password)'),
        (re.compile('^本机 MC    : (.*?):(.*?)$'), 'Local MC   : \\1:\\2'),
        (re.compile('^本机世界 (.*?)$'), 'Local world \\1'),
        (re.compile('^本机端口 (.*?):(.*?) 已被占用，请换一个本地端口$'), 'The local port \\1:\\2 is already in use, please pick another one'),
        (re.compile('^正在创建房间「(.+?)」\\.\\.\\.$'), 'Creating room "\\1" ...'),
        (re.compile('^正在加入房间「(.+?)」\\.\\.\\.$'), 'Joining room "\\1" ...'),
        (re.compile('^正在删除房间 (.*?) \\.\\.\\.$'), 'Deleting room \\1 ...'),
        (re.compile('^正在测试 (.*?):(.*?) \\.\\.\\.$'), 'Testing \\1:\\2 ...'),
        (re.compile('^正在登录 (.*?):(.*?) \\.\\.\\.$'), 'Signing in to \\1:\\2 ...'),
        (re.compile('^没有权限监听 (.*?):(.*?)，请换一个端口或使用管理员身份运行$'), 'No permission to listen on \\1:\\2, please use another port or run as administrator'),
        (re.compile('^测试失败：(.*?)$'), 'Test failed: \\1'),
        (re.compile('^玩家断开：session=(.*?)$'), 'Player disconnected: session=\\1'),
        (re.compile('^玩家模式：点开始连接后，在 Minecraft 中连接 127\\.0\\.0\\.1:(.*?)$'), 'Player mode: after pressing Start, connect in Minecraft to 127.0.0.1:\\1'),
        (re.compile('^用户名已改为：(.*?)（房间未受影响）$'), 'Username changed to: \\1 (the room is unaffected)'),
        (re.compile('^用户名最长 (.*?) 个字符$'), 'The username can be at most \\1 characters'),
        (re.compile('^界面刷新异常：(.*?)$'), 'UI refresh error: \\1'),
        (re.compile('^界面外观已切换：语言 (.*?)，主题 (.*?)$'), 'Appearance switched: language \\1, theme \\2'),
        (re.compile('^界面自检失败：输入框和程序读的不是同一个变量 —— (.*?)$'), 'UI self-check failed: the entry box and the program do not read the same variable — \\1'),
        (re.compile('^界面还有未翻译的文字：(.*?)$'), 'The UI still has untranslated text: \\1'),
        (re.compile('^登录失败：(.*?)$'), 'Sign-in failed: \\1'),
        (re.compile('^登录已失效：(.*?)，请重新登录$'), 'The sign-in expired: \\1, please sign in again'),
        (re.compile('^登录成功：(.*?)$'), 'Signed in: \\1'),
        (re.compile('^登录状态被服务器拒绝：(.*?)$'), 'The server rejected the sign-in state: \\1'),
        (re.compile('^登录账号：(.*?)$'), 'Signed-in account: \\1'),
        (re.compile('^确定要删除房间 (.*?) 吗？\\\n\\\n删除后房间会立即关闭，其他玩家将无法加入。$'), 'Delete room \\1?\n\nThe room closes immediately and other players cannot join any more.'),
        (re.compile('^确定要删除房间 (.*?) 吗？\\\n\\\n删除后房间立即关闭，其他玩家将无法再加入。$'), 'Delete room \\1?\n\nThe room closes immediately and other players cannot join any more.'),
        (re.compile('^确定要把用户名从「(.*?)」改成「(.*?)」吗？\\\n\\\n改名立即生效，不会掉线。\\\n自助改名每 (.*?) 天最多 (.*?) 次。$'), 'Change the username from "\\1" to "\\2"?\n\nThe rename takes effect immediately, without dropping the connection.\nSelf-service renames: at most \\3 per \\4 days.'),
        (re.compile('^确定要离开房间 (.*?) 吗？\\\n\\\n$'), 'Leave room \\1?\n\n'),
        (re.compile('^确定要离开房间 (.*?) 吗？\\\n\\\n你是这个房间的所有者，离开后房间会被关闭。离开后主机身份（如果你持有）会被释放。$'), 'Leave room \\1?\n\nYou own this room; it will be closed when you leave.Leaving releases the host slot (if you hold it).'),
        (re.compile('^端口响应异常：(.*?)$'), 'Unexpected port response: \\1'),
        (re.compile('^端口有响应但协议不匹配：(.*?)$'), 'The port answered but the protocol does not match: \\1'),
        (re.compile('^等待玩家加入。房间号 (.*?)；本机 MC (.*?)$'), 'Waiting for players to join. Room name \\1; local MC \\2'),
        (re.compile('^获取失败：(.*?)$'), 'Fetch failed: \\1'),
        (re.compile('^获取房间列表失败：(.*?)$'), 'Failed to fetch the room list: \\1'),
        (re.compile('^认领主机身份失败：(.*?)$'), 'Failed to claim the host slot: \\1'),
        (re.compile('^请在 Minecraft 中连接 127\\.0\\.0\\.1:(.*?)$'), 'Connect in Minecraft to 127.0.0.1:\\1'),
        (re.compile('^转发失败：(.*?)$'), 'Forwarding failed: \\1'),
        (re.compile('^进入房间失败：(.*?)$'), 'Failed to enter the room: \\1'),
        (re.compile('^连接 (.*?):(.*?) 超时，可能被防火墙/安全组拦截$'), 'Timed out connecting to \\1:\\2; a firewall or security group may be blocking it'),
        (re.compile('^连接中转服务器超时（(.*?):(.*?)）$'), 'Timed out connecting to the relay server (\\1:\\2)'),
        (re.compile('^连接中转服务器超时（(.*?):(.*?)），请检查网络和服务器状态$'), 'Timed out connecting to the relay server (\\1:\\2), please check the network and the server status'),
        (re.compile('^配置文件：(.*?)$'), 'Config file: \\1'),
        (re.compile('^释放主机身份失败：(.*?)$'), 'Failed to release the host slot: \\1'),
        (re.compile('^隧道 (.*?) 已就绪（空闲等待）$'), 'Tunnel \\1 ready (idle, waiting)'),
        (re.compile('^隧道分配消息异常: (.*?)$'), 'Unexpected tunnel assignment message: \\1'),
        (re.compile('^隧道应答异常: (.*?)$'), 'Unexpected tunnel answer: \\1'),
        (re.compile('^隧道数量   : (.*?)$'), 'Tunnels    : \\1'),
        (re.compile('^（本月还剩 (.*?) 次自助修改）$'), '(\\1 self-service changes left this month)'),
        (re.compile('^正在测试服务器连接 \\.\\.\\.$'), 'Testing the server connection ...'),
        (re.compile('^正在测试 (.+?) \\.\\.\\.$'), 'Testing \\1 ...'),
        # 2026-09-28 设计基准 §G：打洞三层里的**汇总句式**（父级只汇总）。
        # 它们是 f-string 拼出来的，静态表查不到，只能在这里按句式认。
        (re.compile('^正在建立 (.*?) 条连接$'), 'Establishing \\1 connections'),
        (re.compile('^(.*?)/(.*?) 已完成$'), '\\1/\\2 done'),
        (re.compile('^(.*?)/(.*?) 已完成，(.*?) 个失败$'), '\\1/\\2 done, \\3 failed'),
        (re.compile('^(.*?)/(.*?) 成功，(.*?) 个目标失败$'), '\\1/\\2 succeeded, \\3 targets failed'),
        (re.compile('^打洞完成：全部 (.*?) 个目标已打通$'), 'Punch complete: all \\1 targets connected'),
        (re.compile('^P2P 已直连：(.*?)/(.*?)$'), 'Direct P2P: \\1/\\2'),
        (re.compile('^P2P 直连中\\.\\.\\. (.*?)/(.*?)$'), 'Direct P2P in progress... \\1/\\2'),
        (re.compile('^直连尝试中\\.\\.\\. 0/(.*?)$'), 'Trying direct connection... 0/\\1'),
        # 逐目标行的括号模板（中文全角 -> 英文半角）。
        (re.compile('^（(.*?)s）$'), '(\\1s)'),
        (re.compile('^【(.*?)】$'), '[\\1]'),
    ]


def _ja():
    return [
        (re.compile('^主机：(.*?)（正在开服，可以开始连接）$'), 'ホスト：\\1（公開中。接続を開始できます）'),
        (re.compile('^(.*?)\u3000房间内 (.*?) 人\u3000已加入世界 (.*?) 人\\\n(.*?)\u3000（你是房主）$'), '\\1\u3000ルーム内 \\2 人\u3000ワールド内 \\3 人\n\\4\u3000（あなたがホスト）'),
        (re.compile('^2\\-32 个字符，不能有空格。(.*?)$'), '2〜32文字、空白は使えません。\\1'),
        (re.compile('^\\[CONTROL\\] 控制连接已建立 room=(.*?)$'), '[CONTROL] 制御接続を確立 room=\\1'),
        (re.compile('^\\[CONTROL\\] 控制连接断开: (.*?)$'), '[CONTROL] 制御接続が切断: \\1'),
        (re.compile('^\\[CONTROL\\] 正在连接中转服务器 (.*?):(.*?) \\.\\.\\.$'), '[CONTROL] リレーサーバー \\1:\\2 に接続中 ...'),
        (re.compile('^\\[CONTROL\\] 登录状态失效：(.*?)$'), '[CONTROL] ログイン状態が失効：\\1'),
        (re.compile('^\\[CONTROL\\] 连接失败: (.*?)$'), '[CONTROL] 接続に失敗: \\1'),
        (re.compile('^\\[LOCAL\\] 启动失败：(.*?)$'), '[LOCAL] 起動に失敗：\\1'),
        (re.compile('^\\[PLAYER (.*?)\\] Minecraft 已连接 peer=(.*?)$'), '[PLAYER \\1] Minecraft 接続 peer=\\2'),
        (re.compile('^\\[PLAYER (.*?)\\] 原始数据转发开始$'), '[PLAYER \\1] 生データの転送を開始'),
        (re.compile('^\\[PLAYER (.*?)\\] 原始数据转发结束 上行=(.*?)B 下行=(.*?)B$'), '[PLAYER \\1] 生データの転送を終了 上り=\\2B 下り=\\3B'),
        (re.compile('^\\[PLAYER (.*?)\\] 已接入房主，开始转发 Minecraft 数据$'), '[PLAYER \\1] ホストと接続、Minecraft データの転送を開始'),
        (re.compile('^\\[PLAYER (.*?)\\] 已断开$'), '[PLAYER \\1] 切断されました'),
        (re.compile('^\\[PLAYER (.*?)\\] 等待中转服务器配对 \\.\\.\\.$'), '[PLAYER \\1] リレーサーバーのペアリング待ち ...'),
        (re.compile('^\\[PLAYER (.*?)\\] 错误: (.*?)$'), '[PLAYER \\1] エラー: \\2'),
        (re.compile('^\\[RELAY\\] 正在连接中转服务器 (.*?):(.*?) \\.\\.\\.$'), '[RELAY] リレーサーバー \\1:\\2 に接続中 ...'),
        (re.compile('^\\[TUNNEL (.*?)\\] Minecraft 数据转发开始 session=(.*?)$'), '[TUNNEL \\1] Minecraft データの転送を開始 session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] Minecraft 数据转发结束 session=(.*?) 上行=(.*?)B 下行=(.*?)B$'), '[TUNNEL \\1] Minecraft データの転送を終了 session=\\2 上り=\\3B 下り=\\4B'),
        (re.compile('^\\[TUNNEL (.*?)\\] READY 已发送 session=(.*?)$'), '[TUNNEL \\1] READY を送信 session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] (.*?)s 后重连 \\.\\.\\.$'), '[TUNNEL \\1] \\2s 後に再接続 ...'),
        (re.compile('^\\[TUNNEL (.*?)\\] 会话结束 session=(.*?)$'), '[TUNNEL \\1] セッション終了 session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] 已分配玩家 session=(.*?)$'), '[TUNNEL \\1] プレイヤーを割り当て session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] 本机 Minecraft 已连接 session=(.*?)$'), '[TUNNEL \\1] 本機 Minecraft が接続 session=\\2'),
        (re.compile('^\\[TUNNEL (.*?)\\] 正在建立隧道 \\.\\.\\.$'), '[TUNNEL \\1] トンネルを確立中 ...'),
        (re.compile('^\\[TUNNEL (.*?)\\] 正在连接本机 Minecraft (.*?):(.*?) \\.\\.\\.$'), '[TUNNEL \\1] 本機 Minecraft \\2:\\3 に接続中 ...'),
        (re.compile('^\\[TUNNEL (.*?)\\] 空闲等待中 \\(READY / WAITING\\)$'), '[TUNNEL \\1] 空き待機中 (READY / WAITING)'),
        (re.compile('^\\[TUNNEL (.*?)\\] 等待房主就绪…（(.*?)）$'), '[TUNNEL \\1] ホストの準備待ち…（\\2）'),
        (re.compile('^\\[TUNNEL (.*?)\\] 错误: (.*?)$'), '[TUNNEL \\1] エラー: \\2'),
        (re.compile('^(.*?)\\\n\\\n如果房间正在运行，房间列表里的房主名字会在下一次刷新时更新。$'), '\\1\n\nルームが実行中の場合、ルーム一覧の作成者名は次回の更新で反映されます。'),
        (re.compile('^(.*?)\\\n\\\n请重新创建或加入房间。$'), '\\1\n\nもう一度ルームを作成するか、参加してください。'),
        (re.compile('^(.*?)\\\n\\\n请重新登录后再连接。$'), '\\1\n\nもう一度ログインしてから接続してください。'),
        (re.compile('^(.*?) v(.*?) \\(构建 (.*?)\\)$'), '\\1 v\\2 (ビルド \\3)'),
        (re.compile('^(\\d+) 人$'), '\\1 人'),
        (re.compile('^(.*?) 人\u3000\\(你是房间所有者\\)$'), '\\1 人\u3000（あなたが作成者）'),
        (re.compile('^(.*?) 已启动$'), '\\1 を起動しました'),
        (re.compile('^(.*?) 绑的是 (.*?)，程序读的是 (.*?)$'), '\\1 のバインド先は \\2、プログラムが読むのは \\3'),
        (re.compile('^(.*?)\u3000房间内 (.*?) 人\u3000已加入世界 (.*?) 人\\\n(.*?)$'), '\\1\u3000ルーム内 \\2 人\u3000ワールド内 \\3 人\n\\4'),
        (re.compile('^已创建房间「(.+?)」$'), 'ルーム「\\1」を作成しました'),
        (re.compile('^已加入房间「(.+?)」$'), 'ルーム「\\1」に参加しました'),
        (re.compile('^创建房间失败$'), 'ルームの作成に失敗しました'),
        (re.compile('^加入房间失败$'), 'ルームへの参加に失敗しました'),
        (re.compile('^创建房间失败：(.+)$'), 'ルームの作成に失敗：\\1'),
        (re.compile('^加入房间失败：(.+)$'), 'ルームへの参加に失敗：\\1'),
        (re.compile('^(.+?)（根管理员）$'), '\\1（ルート管理者）'),
        (re.compile('^(.+?)（管理员）$'), '\\1（管理者）'),
        (re.compile('^(.+?)（玩家）$'), '\\1（プレイヤー）'),
        (re.compile('^(.+?)（空闲）$'), '\\1（空き）'),
        (re.compile('^(.+?)（有玩家）$'), '\\1（プレイ中）'),
        (re.compile('^(.+?)（根管理员，(.+?)，房间内 (.+?) 人）$'), '\\1（ルート管理者，\\2，ルーム内 \\3 人）'),
        (re.compile('^(.+?)（管理员，(.+?)，房间内 (.+?) 人）$'), '\\1（管理者，\\2，ルーム内 \\3 人）'),
        (re.compile('^(.+?)（玩家，(.+?)，房间内 (.+?) 人）$'), '\\1（プレイヤー，\\2，ルーム内 \\3 人）'),
        (re.compile('^(.+?)（未开服）$'), '\\1（未公開）'),
        (re.compile('^↑(.*?)  ↓(.*?)  \\| 隧道 (.*?)/(.*?) 就绪$'), '↑\\1  ↓\\2  | トンネル \\3/\\4 準備完了'),
        (re.compile('^● 服务器正常 (.*?) ms$'), '● サーバー正常 \\1 ms'),
        (re.compile('^与房间的连接已断开：(.*?)$'), 'ルームとの接続が切断されました：\\1'),
        (re.compile('^中转服务器 : (.*?):(.*?)$'), 'リレーサーバー : \\1:\\2'),
        (re.compile('^中转服务器应答异常: (.*?)$'), 'リレーサーバーの応答が異常です: \\1'),
        (re.compile('^中转服务器正常，往返延迟约 (.*?) ms（当前 (.*?) 个房间在线）$'), 'リレーサーバー正常、往復遅延 約 \\1 ms（オンライン \\2 ルーム）'),
        (re.compile('^中转服务器正常，往返延迟约 (.*?) ms（当前没有房间在线）$'), 'リレーサーバー正常、往復遅延 約 \\1 ms（オンラインのルームなし）'),
        (re.compile('^主机身份被别人抢先了：(.*?)$'), 'ホストを先に取られました：\\1'),
        (re.compile('^主机：(.*?)$'), 'ホスト：\\1'),
        (re.compile('^人数上限 (.*?) 超过服务器上限 (.*?)，已阻止建房$'), '人数上限 \\1 がサーバー上限 \\2 を超えているため、作成を中止しました'),
        (re.compile('^人数上限只能是 (.*?)\\-(.*?) 人。服务器允许的最大值是 (.*?) 人（这是单个房间的上限，不是所有房间加起来）。$'), '人数上限は \\1-\\2 人です。サーバーが許可する最大は \\3 人です（合計ではなく1ルームあたり）。'),
        (re.compile('^人数上限只能是 (.*?)\\-(.*?)，你填的是 (.*?)。\\\n\\\n服务器允许的最大值是 (.*?) 人（单房间，不是所有房间加起来）。$'), '人数上限は \\1-\\2 の範囲です。入力値は \\3。\n\nサーバーが許可する最大は \\4 人です（合計ではなく1ルームあたり）。'),
        (re.compile('^人数上限必须是数字（(.*?)\\-(.*?)）$'), '人数上限は数字で入力してください（\\1-\\2）'),
        (re.compile('^你设置的人数上限是 (.*?) 人，但中转服务器的上限是 (.*?) 人。\\\n\\\n房间人数不能超过服务器上限 —— 要么把这里改成 (.*?) 或更小，要么请管理员在管理面板里把上限调高。$'), '設定した人数上限は \\1 人ですが、リレーサーバーの上限は \\2 人です。\n\nルームの人数はサーバー上限を超えられません — ここを \\3 以下に変更するか、管理者に上限の引き上げを依頼してください。'),
        (re.compile('^你还在房间「(.*?)」里。\\\n\\\n$'), 'まだルーム「\\1」にいます。\n\n'),
        (re.compile('^你还在房间「(.*?)」里。\\\n\\\n你是这个房间的所有者，退出登录会同时关闭该房间。\\\n退出登录会同时退出该房间。\\\n$'), 'まだルーム「\\1」にいます。\n\nあなたはこのルームの作成者です。ログアウトするとルームも閉じられます。\nログアウトするとそのルームからも退出します。\n'),
        (re.compile('^你还在房间「(.*?)」里。\\\n\\\n你是这个房间的所有者，退出登录会同时关闭该房间。\\\n退出登录会同时退出该房间。\\\n\\\n确定要退出登录吗？$'), 'まだルーム「\\1」にいます。\n\nあなたはこのルームの作成者です。ログアウトするとルームも閉じられます。\nログアウトするとそのルームからも退出します。\n\nログアウトしますか？'),
        (re.compile('^你还没有进入房间 (.*?)。\\\n\\\n$'), 'まだルーム \\1 に入っていません。\n\n'),
        (re.compile('^你还没有进入房间 (.*?)。\\\n\\\n要以主机身份开世界，需要先创建房间。需要先加入房间才能连接。$'), 'まだルーム \\1 に入っていません。\n\nホストとしてワールドを開くには、先にルームを作成してください。接続するには先にルームへ参加してください。'),
        (re.compile('^你还没有进入房间 (.*?)。\\\n\\\n要以主机身份开世界，需要先创建房间。需要先加入房间才能连接。\\\n\\\n现在就去吗？$'), 'まだルーム \\1 に入っていません。\n\nホストとしてワールドを開くには、先にルームを作成してください。接続するには先にルームへ参加してください。\n\n今すぐ移動しますか？'),
        (re.compile('^停止时出错：(.*?)$'), '停止中にエラーが発生しました：\\1'),
        (re.compile('^共 (.*?) 个房间，(.*?) 个有玩家（自动刷新，双击一行即可加入）$'), '全 \\1 ルーム、うち \\2 にプレイヤーあり（自動更新・行をダブルクリックで参加）'),
        (re.compile('^共 (.*?) 个房间，其中 (.*?) 个有玩家（(.*?) 刷新）$'), '全 \\1 ルーム、うち \\2 にプレイヤーあり（\\3 更新）'),
        (re.compile('^删除失败：(.*?)$'), '削除に失敗しました：\\1'),
        (re.compile('^删除房间失败：(.*?)$'), 'ルームの削除に失敗しました：\\1'),
        (re.compile('^刷新失败：(.*?)$'), '更新に失敗しました：\\1'),
        (re.compile('^刷新房间列表失败：(.*?)$'), 'ルーム一覧の更新に失敗しました：\\1'),
        (re.compile('^单个房间的人数上限，默认 (.*?) 人（含主机）。上限由服务器统一设置，超出会提示重填。$'), '1ルームあたりの人数上限。既定は \\1 人（ホストを含む）。上限はサーバーが決めており、超えると再入力を求められます。'),
        (re.compile('^参数错误：(.*?)$'), 'パラメータエラー：\\1'),
        (re.compile('^发生错误：(.*?)$'), 'エラーが発生しました：\\1'),
        (re.compile('^发送删除请求失败：(.*?)$'), '削除要求の送信に失敗しました：\\1'),
        (re.compile('^发送失败：(.*?)$'), '送信に失敗しました：\\1'),
        (re.compile('^启动 (.*?) 模式 \\| 房间 (.*?) \\| 账号 (.*?) \\| 服务器 (.*?):(.*?)$'), '\\1 モードを開始 | ルーム \\2 | アカウント \\3 | サーバー \\4:\\5'),
        (re.compile('^启动失败：(.*?)$'), '起動に失敗しました：\\1'),
        (re.compile('^在 Minecraft 中连接：(.*?)$'), 'Minecraft で接続：\\1'),
        (re.compile('^在线状态上报被拒绝：(.*?)$'), 'オンライン状態の送信が拒否されました：\\1'),
        (re.compile('^在线状态连接断开（(.*?)），(.*?) 秒后重连$'), 'オンライン状態の接続が切断（\\1）。\\2 秒後に再接続します'),
        (re.compile('^处理数据包失败：(.*?)$'), 'パケットの処理に失敗しました：\\1'),
        (re.compile('^复制失败：(.*?)$'), 'コピーに失敗しました：\\1'),
        (re.compile('^已从房间列表选择：(.*?)$'), 'ルーム一覧から選択：\\1'),
        (re.compile('^已填入保存的账号 (.*?)$'), '保存済みのアカウント \\1 を入力しました'),
        (re.compile('^已复制 Minecraft 连接地址：(.*?)$'), 'Minecraft の接続先をコピーしました：\\1'),
        (re.compile('^已改名为 (.*?)，立即生效（无需重新登录）(.*?)$'), '\\1 に名前を変更しました。すぐに反映されます（再ログイン不要）\\2'),
        (re.compile('^已改名为：(.*?)（立即生效，未断开连接）$'), '名前を変更しました：\\1（すぐ反映・接続は維持）'),
        (re.compile('^已生成随机房间号：(.*?)$'), 'ランダムなルーム名を生成しました：\\1'),
        (re.compile('^已登录：(.*?)$'), 'ログインしました：\\1'),
        (re.compile('^已自动退出房间：(.*?)$'), '自動的にルームから退出しました：\\1'),
        (re.compile('^已获得主机身份：(.*?)$'), 'ホストを引き受けました：\\1'),
        (re.compile('^已退出登录：(.*?)（服务器已同步）$'), 'ログアウトしました：\\1（サーバーと同期済み）'),
        (re.compile('^已退出登录：(.*?)（未能通知服务器，稍后会自动失效）$'), 'ログアウトしました：\\1（サーバーに通知できませんでした。後で自動的に失効します）'),
        (re.compile('^已选择房间 (.*?)，回到主界面点「开始连接」$'), 'ルーム \\1 を選択しました。メイン画面に戻って「接続開始」を押してください'),
        (re.compile('^已释放主机身份：(.*?)$'), 'ホストを放棄しました：\\1'),
        (re.compile('^当前地址是「(.*?)」，不是 Minecraft 连接地址（只有玩家模式才有 127\\.0\\.0\\.1 地址）。$'), '現在のアドレスは「\\1」で、Minecraft の接続先ではありません（127.0.0.1 になるのはプレイヤーモードだけです）。'),
        (re.compile('^房间 (.*?) 已删除$'), 'ルーム \\1 を削除しました'),
        (re.compile('^房间 (.*?) 是「(.*?)」建的，当前主机是「(.*?)」，都不是你。\\\n只有建房的人或当前主机能删除房间。$'), 'ルーム \\1 を作成したのは「\\2」、現在のホストは「\\3」で、どちらもあなたではありません。\n削除できるのは作成者か現在のホストだけです。'),
        (re.compile('^房间 (.*?) 里还有 (.*?) 名玩家。\\\n\\\n请先让玩家退出 Minecraft，或者由服主在管理面板强制关闭。$'), 'ルーム \\1 にはまだ \\2 人のプレイヤーがいます。\n\n先にプレイヤーに Minecraft から退出してもらうか、鯖主が管理パネルから強制クローズしてください。'),
        (re.compile('^房间 (.*?) 里还有 (.*?) 名玩家。\\\n\\\n请先让玩家退出，或者让服主在管理面板强制关闭。$'), 'ルーム \\1 にはまだ \\2 人のプレイヤーがいます。\n\n先にプレイヤーに退出してもらうか、鯖主が管理パネルから強制クローズしてください。'),
        (re.compile('^房间「(.*?)」已存在，改为加入$'), 'ルーム「\\1」はすでに存在するため、参加に切り替えました'),
        (re.compile('^房间「(.*?)」已经由「(.*?)」建好了，现在里面 (.*?) 人。\\\n\\\n点「是」= 直接加入他的房间（不再新建）\\\n点「否」= 取消，我换一个房间名$'), 'ルーム「\\1」は「\\2」がすでに作成済みで、現在 \\3 人います。\n\n「はい」= そのルームに直接参加する（新規作成しない）\n「いいえ」= キャンセルして別のルーム名にする'),
        (re.compile('^房间事件没有送达界面（已按最新状态重画）：(.*?)$'), 'ルームのイベントが画面に届きませんでした（最新状態で再描画）：\\1'),
        (re.compile('^房间人数已设为 (.*?)$'), 'ルームの人数を \\1 に設定しました'),
        (re.compile('^房间人数：(.*?)$'), 'ルーム内の人数：\\1'),
        (re.compile('^房间号     : (.*?)$'), 'ルーム名   : \\1'),
        (re.compile('^房间名「(.*?)」已被占用，请换一个名字$'), 'ルーム名「\\1」は使用済みです。別の名前を付けてください'),
        (re.compile('^房间名不能为空。可以写中文、英文、数字和空格，最长 (.*?) 个字符。$'), 'ルーム名は空にできません。中国語・英語・数字・空白が使え、最大 \\1 文字です。'),
        (re.compile('^房间名最长 (.*?) 个字符（你填了 (.*?) 个）$'), 'ルーム名は最大 \\1 文字です（入力は \\2 文字）'),
        (re.compile('^房间已不存在：(.*?)$'), 'ルームはすでに存在しません：\\1'),
        (re.compile('^房间已被关闭：(.*?)$'), 'ルームは閉じられました：\\1'),
        (re.compile('^房间已被服务器关闭：(.*?)$'), 'ルームはサーバーによって閉じられました：\\1'),
        (re.compile('^探测失败：(.*?)$'), '検出に失敗しました：\\1'),
        (re.compile('^控制连接应答异常: (.*?)$'), '制御接続の応答が異常です: \\1'),
        (re.compile('^控制连接断开：(.*?)，正在重连 \\.\\.\\.$'), '制御接続が切断：\\1、再接続中 ...'),
        (re.compile('^操作失败：(.*?)$'), '操作に失敗しました：\\1'),
        (re.compile('^改名失败：(.*?)$'), '名前の変更に失敗しました：\\1'),
        (re.compile('^无法在本机监听 (.*?):(.*?)：(.*?)$'), '本機 \\1:\\2 をリッスンできません：\\3'),
        (re.compile('^无法连接 (.*?):(.*?)（(.*?)）$'), '\\1:\\2 に接続できません（\\3）'),
        (re.compile('^无法连接中转服务器（(.*?):(.*?)）：(.*?)$'), 'リレーサーバー（\\1:\\2）に接続できません：\\3'),
        (re.compile('^日志已保存：(.*?)$'), 'ログを保存しました：\\1'),
        (re.compile('^普通账号每月最多 (.*?) 次自助修改（改名和改密码共用）；管理员不受限制。$'), '一般アカウントの自己変更は月に最大 \\1 回まで（名前変更とパスワード変更で共用）。管理者は制限なし。'),
        (re.compile('^有玩家接入：session=(.*?) 隧道=(.*?)$'), 'プレイヤーが接続：session=\\1 トンネル=\\2'),
        (re.compile('^服务器 (.*?)，协议版本 v(.*?)$'), 'サーバー \\1、プロトコル版 v\\2'),
        (re.compile('^服务器在线，但版本较旧（(.*?) ms）。请把服务端也更新到 v2 才能使用账号登录。$'), 'サーバーはオンラインですが古い版です（\\1 ms）。アカウントログインを使うにはサーバーも v2 に更新してください。'),
        (re.compile('^服务器应答异常：(.*?)$'), 'サーバーの応答が異常です：\\1'),
        (re.compile('^服务器返回错误：(.*?)$'), 'サーバーがエラーを返しました：\\1'),
        (re.compile('^服务器：(.*?)\u3000协议版本：v(.*?)$'), 'サーバー：\\1\u3000プロトコル版：v\\2'),
        (re.compile('^服务器：(.*?)\u3000协议版本：v(.*?)\u3000(.*?)$'), 'サーバー：\\1\u3000プロトコル版：v\\2\u3000\\3'),
        (re.compile('^本地代理已监听 (.*?)$'), 'ローカルプロキシが \\1 で待ち受けています'),
        (re.compile('^本地监听   : (.*?)$'), 'ローカル待受 : \\1'),
        (re.compile('^本地端口监听失败：(.*?)$'), 'ローカルポートの待ち受けに失敗しました：\\1'),
        (re.compile('^本地连接 (.*?)$'), 'ローカル接続 \\1'),
        (re.compile('^本月还可以自助修改 (.*?) 次（改名和改密码共用）$'), '今月はあと \\1 回自己変更できます（名前変更とパスワード変更で共用）'),
        (re.compile('^本机 MC    : (.*?):(.*?)$'), '本機 MC    : \\1:\\2'),
        (re.compile('^本机世界 (.*?)$'), '本機ワールド \\1'),
        (re.compile('^本机端口 (.*?):(.*?) 已被占用，请换一个本地端口$'), '本機ポート \\1:\\2 は使用中です。別のポートを指定してください'),
        (re.compile('^正在创建房间「(.+?)」\\.\\.\\.$'), 'ルーム「\\1」を作成中 ...'),
        (re.compile('^正在加入房间「(.+?)」\\.\\.\\.$'), 'ルーム「\\1」に参加中 ...'),
        (re.compile('^正在删除房间 (.*?) \\.\\.\\.$'), 'ルーム \\1 を削除中 ...'),
        (re.compile('^正在测试 (.*?):(.*?) \\.\\.\\.$'), '\\1:\\2 を確認中 ...'),
        (re.compile('^正在登录 (.*?):(.*?) \\.\\.\\.$'), '\\1:\\2 にログイン中 ...'),
        (re.compile('^没有权限监听 (.*?):(.*?)，请换一个端口或使用管理员身份运行$'), '\\1:\\2 をリッスンする権限がありません。別のポートを使うか、管理者として実行してください'),
        (re.compile('^测试失败：(.*?)$'), '確認に失敗しました：\\1'),
        (re.compile('^玩家断开：session=(.*?)$'), 'プレイヤーが切断：session=\\1'),
        (re.compile('^玩家模式：点开始连接后，在 Minecraft 中连接 127\\.0\\.0\\.1:(.*?)$'), 'プレイヤーモード：接続開始を押した後、Minecraft で 127.0.0.1:\\1 に接続します'),
        (re.compile('^用户名已改为：(.*?)（房间未受影响）$'), 'ユーザー名を変更しました：\\1（ルームには影響ありません）'),
        (re.compile('^用户名最长 (.*?) 个字符$'), 'ユーザー名は最大 \\1 文字です'),
        (re.compile('^界面刷新异常：(.*?)$'), '画面の更新エラー：\\1'),
        (re.compile('^界面外观已切换：语言 (.*?)，主题 (.*?)$'), '外観を切り替えました：言語 \\1、テーマ \\2'),
        (re.compile('^界面自检失败：输入框和程序读的不是同一个变量 —— (.*?)$'), '画面の自己チェックに失敗：入力欄とプログラムが読む変数が違います — \\1'),
        (re.compile('^界面还有未翻译的文字：(.*?)$'), '画面に未翻訳の文字があります：\\1'),
        (re.compile('^登录失败：(.*?)$'), 'ログインに失敗しました：\\1'),
        (re.compile('^登录已失效：(.*?)，请重新登录$'), 'ログインが失効しました：\\1。もう一度ログインしてください'),
        (re.compile('^登录成功：(.*?)$'), 'ログインしました：\\1'),
        (re.compile('^登录状态被服务器拒绝：(.*?)$'), 'ログイン状態がサーバーに拒否されました：\\1'),
        (re.compile('^登录账号：(.*?)$'), 'ログイン中のアカウント：\\1'),
        (re.compile('^确定要删除房间 (.*?) 吗？\\\n\\\n删除后房间会立即关闭，其他玩家将无法加入。$'), 'ルーム \\1 を削除しますか？\n\n削除するとすぐに閉じられ、他のプレイヤーは参加できなくなります。'),
        (re.compile('^确定要删除房间 (.*?) 吗？\\\n\\\n删除后房间立即关闭，其他玩家将无法再加入。$'), 'ルーム \\1 を削除しますか？\n\n削除するとすぐに閉じられ、他のプレイヤーは参加できなくなります。'),
        (re.compile('^确定要把用户名从「(.*?)」改成「(.*?)」吗？\\\n\\\n改名立即生效，不会掉线。\\\n自助改名每 (.*?) 天最多 (.*?) 次。$'), 'ユーザー名を「\\1」から「\\2」に変更しますか？\n\n変更はすぐに反映され、切断はされません。\n自己変更は \\4 日ごとに最大 \\3 回までです。'),
        (re.compile('^确定要离开房间 (.*?) 吗？\\\n\\\n$'), 'ルーム \\1 から退出しますか？\n\n'),
        (re.compile('^确定要离开房间 (.*?) 吗？\\\n\\\n你是这个房间的所有者，离开后房间会被关闭。离开后主机身份（如果你持有）会被释放。$'), 'ルーム \\1 から退出しますか？\n\nあなたはこのルームの作成者です。退出するとルームは閉じられます。退出するとホストの位置（保持している場合）は解放されます。'),
        (re.compile('^端口响应异常：(.*?)$'), 'ポートの応答が異常です：\\1'),
        (re.compile('^端口有响应但协议不匹配：(.*?)$'), 'ポートは応答しましたがプロトコルが一致しません：\\1'),
        (re.compile('^等待玩家加入。房间号 (.*?)；本机 MC (.*?)$'), 'プレイヤーの参加を待っています。ルーム名 \\1、本機 MC \\2'),
        (re.compile('^获取失败：(.*?)$'), '取得に失敗しました：\\1'),
        (re.compile('^获取房间列表失败：(.*?)$'), 'ルーム一覧の取得に失敗しました：\\1'),
        (re.compile('^认领主机身份失败：(.*?)$'), 'ホストの引き受けに失敗しました：\\1'),
        (re.compile('^请在 Minecraft 中连接 127\\.0\\.0\\.1:(.*?)$'), 'Minecraft で 127.0.0.1:\\1 に接続してください'),
        (re.compile('^转发失败：(.*?)$'), '転送に失敗しました：\\1'),
        (re.compile('^进入房间失败：(.*?)$'), 'ルームへの入室に失敗しました：\\1'),
        (re.compile('^连接 (.*?):(.*?) 超时，可能被防火墙/安全组拦截$'), '\\1:\\2 への接続がタイムアウトしました。ファイアウォールやセキュリティグループに遮断されている可能性があります'),
        (re.compile('^连接中转服务器超时（(.*?):(.*?)）$'), 'リレーサーバー（\\1:\\2）への接続がタイムアウトしました'),
        (re.compile('^连接中转服务器超时（(.*?):(.*?)），请检查网络和服务器状态$'), 'リレーサーバー（\\1:\\2）への接続がタイムアウトしました。ネットワークとサーバーの状態を確認してください'),
        (re.compile('^配置文件：(.*?)$'), '設定ファイル：\\1'),
        (re.compile('^释放主机身份失败：(.*?)$'), 'ホストの放棄に失敗しました：\\1'),
        (re.compile('^隧道 (.*?) 已就绪（空闲等待）$'), 'トンネル \\1 準備完了（空き待機）'),
        (re.compile('^隧道分配消息异常: (.*?)$'), 'トンネル割り当てメッセージが異常です: \\1'),
        (re.compile('^隧道应答异常: (.*?)$'), 'トンネルの応答が異常です: \\1'),
        (re.compile('^隧道数量   : (.*?)$'), 'トンネル数 : \\1'),
        (re.compile('^（本月还剩 (.*?) 次自助修改）$'), '（今月はあと \\1 回自己変更できます）'),
        (re.compile('^正在测试服务器连接 \\.\\.\\.$'), 'サーバー接続を確認中 ...'),
        (re.compile('^正在测试 (.+?) \\.\\.\\.$'), '\\1 を確認中 ...'),
        # 2026-09-28 設計基準 §G（日本語）
        (re.compile('^正在建立 (.*?) 条连接$'), '\\1 件の接続を確立中'),
        (re.compile('^(.*?)/(.*?) 已完成$'), '\\1/\\2 完了'),
        (re.compile('^(.*?)/(.*?) 已完成，(.*?) 个失败$'), '\\1/\\2 完了、\\3 件失敗'),
        (re.compile('^(.*?)/(.*?) 成功，(.*?) 个目标失败$'), '\\1/\\2 成功、\\3 件失敗'),
        (re.compile('^打洞完成：全部 (.*?) 个目标已打通$'), '穴あけ完了：\\1 件すべて接続済み'),
        (re.compile('^P2P 已直连：(.*?)/(.*?)$'), 'P2P 直結：\\1/\\2'),
        (re.compile('^P2P 直连中\\.\\.\\. (.*?)/(.*?)$'), 'P2P 直結中... \\1/\\2'),
        (re.compile('^直连尝试中\\.\\.\\. 0/(.*?)$'), '直結を試行中... 0/\\1'),
        (re.compile('^（(.*?)s）$'), '(\\1s)'),
        (re.compile('^【(.*?)】$'), '[\\1]'),
    ]


PATTERNS_EN = _en()
PATTERNS_JA = _ja()

#: 语言代码 -> 正则表
PATTERNS = {"en": PATTERNS_EN, "ja": PATTERNS_JA}


# v5.1 desktop navigation and workspace copy.
EN.update({
    '联机工作台':'Connection workspace','房间大厅':'Room browser','连接状态':'Connection status',
    '运行日志':'Activity log','设置与账号':'Settings & account','当前连接':'Current connection',
    '网络通道独立于游戏':'Network tunnels work independently of games',
    '房间与身份':'Room & role','代理与房间配置':'Proxy & room settings',
    'Host 目标端口':'Host target port','Host 本机服务监听端口':'Local service port on the Host',
    'Player 本地代理端口':'Player local proxy port','应用连接本机代理端口':'Connect applications to this local proxy port',
    '2–64 人（含 Host），默认 8；建房后锁定':'2–64 members including Host; default 8; locked after room creation',
    '刷新房间':'Refresh rooms','加入选中房间':'Join selected room','点击刷新获取房间列表':'Refresh to fetch available rooms',
    '测试服务器':'Test server','P2P 直连':'P2P direct','● 未测试服务器':'● Server not tested',
    '程序已经在运行，请检查任务栏。':'CraftLink is already running. Check your taskbar.'
})
JA.update({
    '联机工作台':'接続ワークスペース','房间大厅':'ルーム一覧','连接状态':'接続状況',
    '运行日志':'実行ログ','设置与账号':'設定とアカウント','当前连接':'現在の接続',
    '网络通道独立于游戏':'トンネルはゲームと独立して動作します',
    '房间与身份':'ルームと役割','代理与房间配置':'プロキシとルーム設定',
    'Host 目标端口':'Host の接続先ポート','Host 本机服务监听端口':'Host のローカルサービスポート',
    'Player 本地代理端口':'Player のローカルプロキシポート','应用连接本机代理端口':'このローカルポートにアプリを接続',
    '2–64 人（含 Host），默认 8；建房后锁定':'Host 込み 2～64 人、初期値 8。ルーム作成後は固定',
    '刷新房间':'ルームを更新','加入选中房间':'選択したルームに参加','点击刷新获取房间列表':'更新してルーム一覧を取得',
    '测试服务器':'サーバーをテスト','P2P 直连':'P2P 直接接続','● 未测试服务器':'● サーバー未テスト',
    '程序已经在运行，请检查任务栏。':'起動済みです。タスクバーをご確認ください。'
})
