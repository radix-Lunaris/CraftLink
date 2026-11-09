# CraftLink 打洞流程树（代码现状，2026-09-28）

> 目的：把「点开始连接 → 打洞 → 打通 → 把 socket 交给承载层」这条流程**按代码
> 实际走法**整理成一棵树，每一片叶子标 `file:line`。写这份东西的触发是用户
> 2026-09-28 的实测：「打洞工作流有点混乱，洞打通马上就断」。
>
> 范围：**client 侧为主**（打洞状态机、socket、承载层全在客户端）。
> 中继只参与「配对 / 转发 / `HOST_STATE` 广播 / WAIT / START / CONNECTED」，
> 本文件中凡是 `server/**` 的引用一律标【只读】—— 本轮**不碰服务端**。
>
> 唯一代码来源：`client/mclink/core/punch_flow.py`（纯逻辑状态机）、
> `client/mclink/core/room_direct.py`（房间级协调器，socket 与循环都在这）、
> `client/mclink/core/direct_carry.py`（把洞变成能搬字节的隧道）。
> 基线：`docs/DESIGN-BASELINE-20260927.md` §D/§E/§F/§G（= `AGENTS.md` 规则 6）。

---

## 0. 角色与术语

| 名字 | 代码位置 | 说明 |
| --- | --- | --- |
| `RoomDirectLink` | `core/room_direct.py:104` | 一个房间的打洞协调器；**socket / 循环 / 状态机字典都归它** |
| `ConnSession` | `core/punch_flow.py:367` | 每个 Host↔Player **一条**的独立状态机（§F） |
| `ConnTimer` | `core/punch_flow.py:254` | 每条连接独立计时；终态 `finish()` = 停/清/释放（§F） |
| `DirectLink` | `core/direct.py:163` | 一次「交换候选 → 喷洒 → 命中」的打洞；**它拥有打洞 socket** |
| `PeerLink` | `core/direct_carry.py:105` | 把**命中的那个 socket** 变成 `UdpTunnel` + `PeerChannel` 的长连接 |
| `DirectCarrier` | `core/direct_carry.py:813` | 一条 Minecraft 连接挂到 `PeerLink` 上（一条 sid 流） |
| `HOST_STATE` | 【只读】`server/room_members.py:1666` | 中继广播：谁是 Host / 谁已开始连接（`started=`）/ 每成员连接状态（`conn=`） |

两个「终态」概念要分清（这份文件后面反复用）：

* **一轮**（attempt / batch）失败 → `ConnSession.note_round_failed()`（`punch_flow.py:762`）→ 表继续跑；
* **一次连接**（generation）到达终态 → `ConnSession.finish()`（`punch_flow.py:836`）→ Timer 停/清/释放。

---

## 1. 入口树（谁能让打洞跑起来 / 停下来）

```
打洞工作流入口
├── ① 用户点「开始连接」（唯一"正向"入口）
│   └── gui/app.py:8593 on_start()
│       ├── 门禁：不在房间 / 房间被关 / Host 未开服（host_ready() is False）
│       │   └── gui/app.py:8644-8652（判据 = punch_flow.host_ready，punch_flow.py:1222）
│       ├── engine.start_session(config)            core/engine.py:352
│       │   ├── (旧会话先停) engine.stop_session()   core/engine.py:412
│       │   └── session.start() → 中继握手（CREATE/JOIN/HOST/HOST_TUNNEL/PLAYER）
│       │       └── 握手行都带 `ver=`（AGENTS.md 规则 1）
│       ├── self.running = True                      gui/app.py:8809
│       ├── _report_started(True)   → `STARTED 1`    gui/app.py:7816 / room.py:1084
│       └── _refresh_room_ui()                       gui/app.py:5801
│           └── _sync_room_nat()                     gui/app.py:7584
│               ├── engine.watch_room_nat(stun_room and running)  gui/app.py:7666 → engine.py:740
│               │   ├── RoomNatMonitor.start()（NAT 画像，独立 socket）
│               │   └── engine.watch_room_direct(enabled)          engine.py:795 → 891
│               │       ├── enabled=True  → RoomDirectLink.start() room_direct.py:830 / 914
│               │       └── enabled=False → RoomDirectLink.close() room_direct.py:900 / 943
│               └──（模式真的变了时，先 watch_room_direct(False) 再按新模式起）
│
├── ② 用户点「停止」
│   └── gui/app.py:8827 on_stop()
│       ├── engine.stop_session()                    core/engine.py:412
│       ├── self.running = False                     gui/app.py:8847
│       ├── _report_started(False) → `STARTED 0`     gui/app.py:7851
│       └── engine.watch_room_nat(False)             gui/app.py:8857
│           └── watch_room_direct(False) → RoomDirectLink.close()  room_direct.py:900
│               └── ⚠ 注意：`close()` 会关掉**所有**洞；`stop()`（room_direct.py:870）
│                   才是"停循环但留下 socket"的那一个。GUI 这条走的是 close()。
│
├── ③ 手动重连（停止 → 再开始）
│   └── 顺序 = ② 然后 ①；socket 生命周期由 room_direct.start()/stop()/close() 决定
│       ├── 洞**还活着**时：start() 里的 `_established and transport_ready` 直接
│       │   返回（room_direct.py:858），循环不重启，洞保留（长连接语义）
│       └── 洞**已不在**时：_run() 每轮给每条连接走 `restart()`（room_direct.py:1288）
│
├── ④ 自动重连（被动掉线 / 换模式）
│   └── gui/app.py:8232 _trigger_auto_reconnect() → 8299 _reconnect_session()
│       └── 重建完成 → `rebuild_done` 事件 → _sync_room_nat()  gui/app.py:9032
│           └── 等于重新走 ① 的 watch_room_nat/walk_room_direct 那一支
│
├── ⑤ 模式热切换重建（§D 末句：换模式 = 严格重建全部通路）
│   ├── 中继主动推 `CONN_MODE_CHANGED` → gui/app.py:9307-9350
│   │   ├── engine.watch_room_direct(False)（先把旧的一整套拆干净） gui/app.py:9331
│   │   └── self._mode_rebuild_pending = True（等新模式的 HOST_STATE 到了再建）
│   ├── 每秒对账：_sync_session_mode()               gui/app.py:7681
│   └── 新模式一到：_maybe_rebuild_for_mode_change() gui/app.py:7720 → _rebuild_tunnels(mode=…) 7946
│       └── 重建后 `rebuild_done` → _sync_room_nat() → watch_room_direct(True)
│
├── ⑥ 新人加入（不得影响已打通的连接；§D 末句 / §13）
│   ├── 中继 `HOST_STATE`（peers/names/started 变）→ `host_state` 事件 gui/app.py:9352
│   ├── _run() 每一轮重新拉目标 `_peers()`           room_direct.py:1272 / 960
│   └── _sleep_or_wake() 每 0.25s 看一次目标表，出现新 peer 立刻醒 room_direct.py:1448-1474
│
└── ⑦ 对端离开 / 掉线
    ├── 花名册变化 → 下一轮 `_peers()` 少一个人
    ├── `_sweep_links(roster)`：不在 roster 的**只拆那一条** room_direct.py:1558-1588
    └── 隧道 20s 没数据（LINK_DEAD_S）也走同一条：`_drop_peer()` room_direct.py:1590
```

---

## 2. `RoomDirectLink` 的生命周期与"谁拥有什么"

```
RoomDirectLink（room_direct.py:104）
├── start()                      room_direct.py:830
│   ├── 幂等闸门 1：`self.running`（_task 还活着）→ return        :848
│   ├── 幂等闸门 2：`self._established and self.transport_ready` → return  :858
│   ├── `self._stop = asyncio.Event()`                            :867
│   └── `self._task = run_coroutine_threadsafe(self._run(), loop)` :868
│       └──（loop 来自 engine.loop()，是引擎那条事件循环）
│
├── stop()                       room_direct.py:870
│   ├── `_task = None` + `task.cancel()`                          :880 / 897
│   ├── attempts / last_reason / _peer_attempts 归零              :885-887
│   ├── 每条 **正在跑** 的 timer `finish()`（停/清/释放，§F）      :892-895
│   └── **刻意不关 socket**（映射是资产，会话可能正要来取）        :876-879
│
└── close()                      room_direct.py:900
    ├── stop()                                                     :902
    ├── 取消 `_tasks`（"打通后建隧道"的后台任务）                   :906-909
    ├── 取消 `_ingress_task`（入站探测）                            :912-916
    ├── **先关 carriers**（PeerLink 握着 socket 的读线程）           :920-924
    ├── 再关复用的 `_punch_links`（各自一个 UDP socket）             :927-931
    ├── 再关 `self.link` 与 `links[*]["link"]`（DirectLink.close）   :933-943
    ├── 清 links / _claimed / `_established = False`                :944-946
    ├── 所有 sessions 的 timer.finish() + 清空 sessions             :950-954
    └── 清 target_reason / _heard_started                           :955-956
```

**socket 归属（关键）**

| 阶段 | 谁拥有 socket | 关闭者 |
| --- | --- | --- |
| 打洞中（measure / advertise / attempt） | `DirectLink`（`direct.py:163`），命中后记录 `punched_sock`（`direct.py:231 / 1584`） | `DirectLink.close()`（`direct.py:1754`，关 `punched_sock` + `sock`） |
| 打通、尚未交给承载层 | 同上 + 房间保活线程 `DirectLink.start_keepalive()`（`direct.py:1639`） | 同上 |
| 已交给承载层 | `PeerLink`（`direct_carry.py:119`，**用 `punched_sock`**，见 `direct_carry.py:126`）→ `UdpHub`（唯一读者，`udptunnel.py:582`） | `PeerLink.close()`（`direct_carry.py:707`）→ `hub.close()`（`udptunnel.py:775`）→ 关掉注册在同 socket 上的所有 tunnel |
| 交给承载层之后 | 房间保活**被刻意停掉**（`direct_carry.py:380-382`：`carried=True` + `stop_keepalive()`），改由隧道自己的保活（`udptunnel.py:544` `KEEPALIVE_S=5.0`）维持映射 | — |

> 「已交给承载层」之后，`DirectLink.keepalive_running`（`direct.py:1620`）里 **`carried` 为真就直接返回 True** —— 所以 `RoomDirectLink.peer_ready()`（`room_direct.py:1626`）和 `ready`（`:296`）仍然认为这条洞是活的。这是"一个 socket 两个 owner 阶段"的桥。

---

## 3. 主循环 `_run()` 的每一轮（树）

```
_run()                                  room_direct.py:1258  （while not self._stop）
├── room = engine.room_current()；None → return                    :1262-1265
├── _note_heard_started(room)                                      :1270 / 1194
│   └── 读 room.peek_direct()（room.py:847）里的 DIRECT_HIT/DIRECT_CAND/DIRECT_PUNCH，
│       用 `from=<cid>`（punch_flow.beacon_line，punch_flow.py:1638）记成
│       「他真的点了开始连接」→ `_heard_started`（room_direct.py:259）
│       * 没有 `from=`（老客户端）→ 只在"房里恰好一个对端"时归属，否则不动 :1236-1249
├── peers = self._peers()                                          :1272 / 960
│   ├── state 为 None / 没有 client_id / members<2 → [] + reason      :991-1007
│   └── punch_flow.select_targets(state, me)                        :1009 / punch_flow.py:1258
│       ├── Player：只认 host_id_of(state)（绝不猜，punch_flow.py:1120）
│       │   ├── Host 未知           → ([], "no_host")
│       │   ├── Host 在但没开始连接 → ([], "host_not_ready")
│       │   └── Host 已开始         → ([host], "host")
│       └── Host：started= 过滤 peers（punch_flow.py:1346-1352）
│           └── 老中继没有 started= → 全体 roster + "started_unknown"
│   └── Host 额外并入 `_heard_started`（本架构下玩家在游戏连上之前没有
│       PLAYER 连接，中继 `started=` 看不到他）                      :1020-1036
│
├── 【每条目标】建/刷新状态机（§F：一个 peer 一个，绝不复用）        :1282-1297
│   ├── sess = _session(peer)   room_direct.py:1046（new ConnSession，role/is_host）
│   ├── `sess.timer.running` → 跳过（这一轮还在跑）
│   └── `sess.needs_new_cycle`（punch_flow.py:492）
│       ├── True  → `sess.restart()`（generation+1、阶段回 §E 第一步、表从 0）:1290
│       └── False → `sess.start()`（幂等）                          :1297
│       ⚠ 见 §6：这就是「打通即断」的落点（CONNECTED 也是终态 → needs_new_cycle 为真）
│
├── `if not peers:`（没有目标的那条大分支）                          :1299-1336
│   ├── 阶段文案（按 target_reason 分流；**静止、不倒计时**）        :1310-1322
│   │   ├── no_host           → 「当前房间暂无 Host」
│   │   ├── host_not_ready    → 「Host 未连接，请等待 Host 开服」
│   │   ├── host_not_in_roster→ 「确认 Host 中」
│   │   ├── 房里还有人         → 「等待对端连接…」
│   │   └── 房里没人           → 「等待对端用户加入…」
│   ├── await asyncio.sleep(0.5)
│   └── **只在 reason ∈ {no_peer, no_room} 时**才 `_sweep_links(set())`  :1333-1334
│       （短暂状态不许把活着的洞拆掉 —— §D）
│
├── 【每条目标】真正打洞                                            :1350-1362
│   ├── `if self.peer_ready(peer): continue`（已打通的不重打）        :1351
│   └── ok = await self._punch(room, me, peer)                       :1355（见 §4）
│       └── 异常：attempts+1、记 last_reason、ok=False                :1358-1362
│
├── 【Host 仲裁轮次】这一轮没成 → `sess.next_batch()`（batch+1）      :1377-1385
│   └── 见 punch_flow.py:819；Player 侧靠 `adopt_batch` 跟随（punch_flow.py:727）
│
├── 【成功记账】ok=True                                              :1387-1426
│   ├── `self.attempts = 0` / last_reason = ""                        :1407-1408
│   ├── `self._open_tunnel(peer)`（后台建长连接隧道，见 §5）           :1426 / 1478
│   └── progressed = True
│
├── progressed → `self._established = True`                          :1428-1429
├── `_sweep_links(set(peers))`（判死 + 清理，只动该死的那条）         :1432 / 1558
│   ├── peer ∈ roster 且 carrier 活着 且 idle < LINK_DEAD_S(20s) → 留  :1570-1579
│   └── 否则 → `_drop_peer(peer)`（关 carrier + link + 会话 + timer）  :1588 / 1590
└── `await self._sleep_or_wake(peers)`（3s，切成 0.25s 片；新目标就醒）:1444 / 1448
```

---

## 4. 单目标阶段树（§E 严格顺序）—— 一次 `_punch()`

`_punch()` = `room_direct.py:1648`。**每个 peer 一次**，两端各自跑同一套阶段；
同步靠中继逐字转发的 `DIRECT_HIT / DIRECT_CAND / DIRECT_PUNCH`（**不新增协议**，
中继只读 `to=` 路由【只读】`server/room_members.py:807 _direct_peer`）。

```
_punch(room, me, peer)                                  room_direct.py:1648
├── attempts+1 / _peer_attempts[peer]+1                              :1657-1658
├── sess = _session(peer)
│   ├── `not timer.running and not terminal` → `sess.start()`         :1666-1667
│   └── advance(CONFIRM_HOST) → advance(CONFIRM_TARGET)               :1672-1673
│
├── 复用同一个 DirectLink（同一 socket、同一份测量；重试不再换映射）   :1686-1707
│   ├── 建：direct.DirectLink(room, relay_host, relay_port, my_id, peer_id,
│   │        is_host=…)                                              :1690-1700
│   ├── `link.stun_port = _stun_port()`                              :1706 / 1635
│   └── `_punch_links[peer] = link`                                  :1707
├── 挂本轮 att/batch 到出站直连行：`link.flow_attempt/flow_batch`      :1711-1713
├── 房间 NAT 画像（RoomNatMonitor，同一个 NAT 另一个 socket）当提示     :1718-1726
│
├── §E 阶段 3：NAT 测量
│   ├── `_set_phase("测量 NAT 类型中…")` + `advance(NAT)`              :1743-1744
│   ├── `_announce_stage(link, sess, BEACON_NAT)` → 线上 `DIRECT_HIT stage=nat att= batch= from=` :1749 / 2000 / beacon_line 1638
│   ├── `_attach_punch_steps(link)`（接 kick 倒计时文案）              :1750 / 2219
│   └── `await link.measure()`（direct.py:625；异常只记日志不中断）    :1753-1755
│
├── §E 阶段 4：入站策略测量
│   ├── `_set_phase("测量入站策略中…")` + `advance(INBOUND)`           :1785-1786
│   ├── `_announce_stage(..., BEACON_INBOUND)`                        :1787
│   └── 只在第一轮/结论未知时真的探：`link.probe_inbound()`（direct.py:326）:1789-1800
│       （**阶段每一轮都走**，探测本身不重复 —— 用户第 3 条）
│
├── §E 阶段 5：上报 + 「等待同步起跑」
│   ├── `sess.mark_measured(nat, filtering, inbound)`                 :1813-1817 / punch_flow.py:641
│   ├── `_set_phase("上报测量结果")` + `advance(REPORT)`               :1819-1820
│   ├── `_set_phase("等待同步起跑")` + `advance(SYNC_START)`           :1822-1823
│   └── `synced = await _wait_synced_start(link, sess)`               :1825 / 2056
│       ├── 每 BEACON_REANNOUNCE_S(1.5s) 重播 `DIRECT_HIT stage=measured` :2107-2116
│       ├── `room.wait_direct((DIRECT_HIT, DIRECT_CAND), timeout=…)`（room.py:889）
│       │   ├── 收到 `DIRECT_CAND` → 放回队列 + `mark_peer_measured()`  :2132-2145
│       │   │   （老客户端不发同步信标，候选本身就是"它测完了"）
│       │   ├── 收到 `stage=nat|inbound` → `note_peer_stage()` 后**继续等** :2156-2165
│       │   ├── 收到 `stage=measured` → `mark_peer_measured()` 返回 True :2186-2193
│       │   └── **不要求 batch 相等**（两端节拍器会永远错开；轮次同步交给
│       │       candidate / DIRECT_PUNCH 自带的 att=/batch=）           :2172-2185
│       ├── 超时 SYNC_START_WAIT_S(12s) → 记 last_reason、`note_round_failed`、
│       │   返回 False（**不硬打**）                                    :2195-2201 / 1827-1835
│       └── 没有房间控制面（单测 FakeRoom）→ 直接 True                  :2082-2093
│
├── §E 阶段 6：同步 candidate → 双方确认
│   ├── `_set_phase("交换候选地址")` + `advance(CANDIDATE)`            :1840-1841
│   ├── 再挂一次 att/batch（对端信标可能改过轮次）                      :1845-1847
│   ├── `addr = await link.advertise()`（direct.py:982；出站 `DIRECT_CAND ip port …`）
│   │   └── None → `note_round_failed("no candidate exchanged")` + False :1851-1857
│   ├── `mark_candidate()` / `mark_peer_candidate()` / `confirm_candidate()` :1859-1869
│   ├── `advance(CONFIRM_CAND)`                                        :1871
│   └── `not sess.cand_confirmed` → `note_round_failed` + False         :1874-1877
│
├── §E 阶段 7：同步起降 + Punch
│   ├── `_set_phase("等待对端起跑公告")` + `advance(SYNC_GO)`           :1886-1887
│   ├── `result = await link.attempt(timeout=PUNCH_BUDGET_S(6s), per_ip_pool=…)`
│   │     （direct.py:1359；线上 `DIRECT_PUNCH startIn=`；两端在本地单调钟上对齐起跳）
│   ├── `sess.note_punch(1)`（「第 N 次」；punch_flow.py:716）          :1894-1895
│   │
│   ├── result.ok == True  ★打通★                                     :1897-1944
│   │   ├── `links[peer] = {"link","addr": result.addr,"ready_at"}`     :1907-1911
│   │   ├── `attempts = 0` / last_reason = "" / `_peer_attempts[peer] = 0` :1407 / 1914
│   │   ├── `link.start_keepalive()`（**保活从这一刻起，直到退房**）      :1915 / direct.py:1639
│   │   ├── `sess.finish(True, latency_ms=…, addr="ip:port")`（Timer 停/清/释放）:1931-1938 / punch_flow.py:836
│   │   └── 日志 `[direct] ROOM LINK UP …` / `punched link … kept warm`  :1392-1396 / 1940-1943
│   │
│   └── result.ok == False                                             :1946-1952
│       ├── last_reason = result.reason or "no packet arrived"
│       └── `sess.note_round_failed(reason)`（表继续跑，Timer 不停）      :1950
│
├── except SyncMismatch（双端 att/batch 错位，§F 最重的一条）            :1953-1969
│   └── `adopt_batch` 已经 void 过；这里 `note_round_failed` + 日志 + False
│
└── finally                                                            :1970-1980
    └── **只关"这一轮没成为我们保留的那条"的 link**：
        `if not any(e.get("link") is link for e in self.links.values()): link.close()`
        （成功那条在 links 里 → 不关；失败那条 → 关 socket）
```

`_wait_synced_start` 的"谁等谁"一句话：**先测完的一方等对端也测完测量**；
两端都进 `SYNC_START` 之后才允许 `CANDIDATE`（`advance()` 只许 +1，跳步/回退被拒，
`punch_flow.py:547`）。

---

## 5. 打通之后：socket 交给承载层（谁在什么时候动它）

```
O(1) 打通（room_direct._punch 成功分支，:1897）
├── links[peer] 记录 + link.start_keepalive()（房间保活，5s 一次）
└── _open_tunnel(peer)（后台任务，幂等）                room_direct.py:1426 / 1478
    ├── entry = links[peer]；punch_link = entry["link"]                  :1487-1495
    ├── 已有 carrier 且没停 → 复用                                     :1497-1519
    ├── 新建 `direct_carry.PeerLink(peer, punch_link, peer_addr=entry["addr"])` :1504-1507
    │   └── PeerLink.__init__：`self.sock = punch_link.punched_sock or punch_link.sock` :126
    │       `self.addr = peer_addr or punch_link.peer_addr`（**必须是命中地址**）  :146
    ├── 房主的本机 MC 地址应用给**每一条**链路（迟到玩家那条也要）        :1523-1524 / set_backend 350
    ├── `await carrier.ensure_started(CONNECT_TIMEOUT_S=8s)`             :1527 / direct_carry.py:332
    │   ├── `punch_link.carried = True` + `punch_link.stop_keepalive()`  :380-382
    │   │   → 房间保活停；隧道的 KEEPALIVE_S=5s 接手（udptunnel.py:544）
    │   ├── `_connect_blocking()`（线程里）：先 `_drain_socket()` 丢掉旧包、
    │   │   把 socket 设回阻塞、建 `UdpHub`（唯一读者）+ `UdpTunnel`、
    │   │   `tunnel.connect(8s)` 盲发 HELLO/HELLO_ACK                :418-462
    │   ├── 成功 → `_started = True`、起读线程 + demux 循环            :403-416
    │   └── 失败 → `_teardown_start()`、`carried=False`、**把保活还给房间** :391-401
    ├── 失败重试一次（`await sleep(RETRY_INTERVAL_S=3s)` 再 ensure_started）:1532-1534
    └── 两次都失败 → 日志 + `self._drop_peer(peer)`（**把这条洞扔掉重打**）:1551-1552
```

Minecraft 连接到来时（游戏侧，**不是**打洞侧）：
`host_core.relay_mc`（`host_core.py:1122`）/ `player_core.raw_relay`（`player_core.py:514`）
在 STUN 模式下走 `direct_path_provider`（`session.py:473` → `engine.wait_room_direct_path`，`engine.py:974`）
→ `engine.room_direct_path()`（`engine.py:1043`）→ `RoomDirectLink.take_path()`（`room_direct.py:439`）
→ `DirectCarrier.open()`（`direct_carry.py:840`）→ `pump()`（`:869`）：
* 玩家侧：`channel.next_stream()` 分一条 sid → `_pump_stream`（`:1086`）；
* 房主侧：`_hold_host`（`:991`）：把会话那条本机 MC 连接 park 成 `Spare`，然后
  **守住会话直到中继配对结束**（`_drain_relay`，`:1045`）。

---

## 6. 「打通即断」的落点（本轮修复对象，先在这里点名）

`_run()` 每轮开头那段（`room_direct.py:1282-1297`）用的是
`ConnSession.needs_new_cycle`（`punch_flow.py:491-503`）：

```python
@property
def needs_new_cycle(self):
    return bool(self.terminal or self.timer.released or self.stage == Stage.IDLE)
```

而 `terminal`（`punch_flow.py:487`）= `CONNECTED` **或** `FAILED`
（`TERMINAL_STAGES`，`punch_flow.py:93`）。于是：

```
打通（finish(True) → Stage.CONNECTED、timer 已释放）
        │
        │  ← 同一轮里 _punch 返回 True，_run 继续；这一轮不会再进 sessions 段
        ▼
+ RETRY_INTERVAL_S(3s) 之后的下一轮
        │
        ├── peers 仍然包含这个 peer（它还在目标表里）
        ├── sessions 段：`sess.timer.running` False → 不跳过
        │   `sess.needs_new_cycle` True（terminal！）→ `sess.restart()`  :1288-1295
        │       → connected=False、stage=idle→confirm_host、batch=0、
        │         addr=""、generation+1、新建 ConnTimer 并 start()
        └── 直通分支 `peer_ready(peer)` 仍然 True（`carried` → keepalive_running）
            → 跳过重打（**socket 不会被关，字节仍然能走**）
```

后果（两端同时发生，间隔正好 3 秒）：

* `flow_summary()`（`room_direct.py:1168` → `punch_flow.summarize`，`punch_flow.py:1498`）
  从 `P2P 已直连：1/1` 变成 `直连尝试中... 0/1`；
* 逐目标行（`link_rows`，`room_direct.py:576`）从
  `P2P 已直连【玩家名】ip:port 18ms` 变成 `确认 Host（0s）【玩家名】N/C`；
* 日志新增一行 `[direct] new connection cycle for <peer> (generation N -> N+1):
  walking §E from the first stage again`。

即：**打通后 3 秒，"这条连接已建立"的账被自己抹掉**。这就是用户看到的
「洞打通马上就断」（实测见 `tests/direct_persist_test.py` / 报告）。

> 注意区分：**socket/承载层没有被关**（`peer_ready` 仍为真 → 不重打），
> 断掉的是 `ConnSession`/`flow`/UI 这条"连接已建立"的账。
> 本轮只修这一处；其它可疑路径（见 §9、报告 §⑥）一律不动。

---

## 7. Timer 生命周期（§F）与 `restart()/advance()/finish()` 全部调用点

```
ConnTimer（punch_flow.py:254）
├── start()    punch_flow.py:293  ← 只在 ConnSession.start()（:620）与 restart()（:900）
├── stop()     punch_flow.py:301  （保留数字给日志）
├── release()  punch_flow.py:315  （清零；elapsed() 从此 0.0）
└── finish()   punch_flow.py:322  = stop + release

一次连接的 Timer 规则：
  终态（成功/放弃）→ finish()（停/清/释放），下一次重连由 restart() new 一个
  一轮失败        → note_round_failed()（**不动 Timer**）
  作废一轮        → void_attempt()（**不动 Timer**）
```

| 方法 | 语义 | 调用点 |
| --- | --- | --- |
| `advance(stage)` `punch_flow.py:547` | 只许 +1 前进；跳步/回退/终态后一律拒 | `room_direct._advance_session`（`room_direct.py:1990`）在 `_punch` 各阶段调用：`:1672 :1673 :1744 :1786 :1820 :1823 :1841 :1871 :1887` |
| `restart()` `punch_flow.py:867` | 新的一次连接：generation+1、回 §E 第一步、表从 0 | `room_direct._run` 每轮目标段：`:1290`（**唯一生产调用点**）；测试 `punch_flow_test.py:251` / `mode_switch_rebuild_test.py:199` |
| `finish(ok)` `punch_flow.py:836` | 终态：Timer 停/清/释放 | `room_direct._punch` 成功分支：`:1931`；失败终态目前**没有**生产调用点（失败按"一轮"处理，见 `note_round_failed`） |
| `note_round_failed()` `punch_flow.py:762` | 一轮没成：清本轮、回 confirm_host、**表继续跑** | `_punch`：`:1833 :1855 :1876 :1950 :1962` |
| `void_attempt()` `punch_flow.py:790` | 双端错位：作废本轮 | `adopt_batch`（`punch_flow.py:754`）与 `next_batch`（`:829`）内部 |
| `next_batch()` `punch_flow.py:819` | Host 开新轮（batch+1） | `_run`：`:1381`（只在 `self._am_host()` 且这一轮没成时） |
| `ConnTimer.finish()` 直调 | 手动停止/退房/切模式：停/清/释放但**不是终态** | `RoomDirectLink.stop()` `:894`、`close()` `:952`、`_drop_peer()` `:1618`；测试 `mode_switch_rebuild_test.py:175` |

---

## 8. 关闭路径总表（谁 → 关了什么 → 触发条件）

| 触发 | 代码 | 关掉什么 | 不关什么 |
| --- | --- | --- | --- |
| 用户点停止 / 退房 / 切回 relay | `watch_room_direct(False)`→`close()` `room_direct.py:900` | 所有 carriers、所有 punch link、所有 DirectLink、sessions | — |
| 用户点停止但打洞循环仍在 | `RoomDirectLink.stop()` `:870` | 只停循环 + Timer | **socket / 洞**（刻意保留） |
| 对端离开房间（不在 roster） | `_sweep_links` → `_drop_peer` `:1558/:1590` | **只那一条** carrier + link + session | 别的 peer 一条不动 |
| 隧道 20s 没数据（LINK_DEAD_S） | 同上 `:1576-1588` | 同上 | 同上 |
| 建隧道握手连失败两次 | `_open_tunnel` `:1551-1552` → `_drop_peer` | 那条洞（**这是"打通后又把洞扔掉"的第二个入口**） | 别的 peer |
| 打洞某一轮没成功 | `_punch` 的 `finally` `:1978-1980` | 这一轮**没被保留**的那个 DirectLink | `links` 里保留的那条 |
| 退房 / 客户端退出 | `engine.close_room`→`session.stop`→…；`engine.shutdown` `:203` | 会话、房间、打洞（`watch_room_direct(False)`） | — |
| 打洞成功后 **下一轮** | `_run` `:1288-1295` `sess.restart()` | ❌ **不关 socket**，但**抹掉"已连接"状态**（= §6 的缺陷） | socket 留着 |

---

## 9. 只读引用：中继在这条流程里做什么

| 中继行为 | 位置【只读】 | 客户端对应 |
| --- | --- | --- |
| 广播 `HOST_STATE … host_id= started= conn= peers= names= mode=` | `server/room_members.py:1666` | `room.py` 解析 → `punch_flow.parse_host_state_tokens`（`punch_flow.py:1415`）→ `select_targets` |
| `started=` 的来源（host 控制连接 + 等待池 + 已配对 + `STARTED 1`） | `server/room_members.py:1861` | Host 的 `started ∩ peers`、Player 的 `host_ready()` |
| `host_id=` 与 `started=` 必须同源 | `server/room_members.py:1932-1933`（`host_client_id`，:1616） | `punch_flow._trusted_started`（`:1185`）做 id 空间一致性检查 |
| 直连行转发（只读 `to=` 一个路由 token） | `server/room_members.py:807` `_direct_peer` | Player 出站行**不带** `to=`；Host 带（`direct.py:953 _to_field`） |
| 开服控制连接 `HOST <room>` / 门禁 `NO_HOST` / `HOST_NOT_READY` | `server/relay_server.py:2050 / 2571 / 2709` | `gui/app.py:8644`；`direct.py` 错误码映射 |
| 配对 + `WAIT` / `START` / `CONNECTED` | `server/relay_server.py:808` `try_pair` | `player_core.py:357`（`LINE_CONNECTED`）、`host_core.relay_mc` |
| 换模式：清旧配对/隧道 + 广播 `CONN_MODE_CHANGED` | `server/room_members.py:1332` / `2106` | `gui/app.py:9307` → 严格重建 |

---

## 10. 一句话总结（这棵树说明了什么）

* **谁在打洞**：`RoomDirectLink._run()` 这条房间级循环；**每个 Host↔Player 一条**
  `ConnSession`，由 `punch_flow` 的迁移表把 §E 的十个阶段钉死。
* **socket 归谁**：打洞阶段归 `DirectLink`（命中 socket = `punched_sock`）；
  打通后由 `_open_tunnel()` 交给 `PeerLink`/`UdpHub`，房间保活让位给隧道保活。
* **谁在等谁**：测量阶段两端互等（`_wait_synced_start`，超时 12s）；
  candidate 两端互确认；起跳靠 `DIRECT_PUNCH startIn=` 对齐。
* **谁会让它停下**：手动停止/退房/换模式走 `close()`；对端离开/判死/握手失败走
  `_drop_peer()`（只断一条）。
* **本轮修的缺陷**：打通（`CONNECTED`）之后**下一轮** `_run()` 把这条连接
  `restart()` 回 §E 第一步 —— 状态机自己把"已直连"抹掉了（§6）。
