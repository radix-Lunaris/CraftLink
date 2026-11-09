# CraftLink 客户端 —— 打洞流程实现与"规则 6"审计（2026-09-28）

> 依据：`docs/DESIGN-BASELINE-20260927.md`（用户逐条确认，已写入 `AGENTS.md`
> 规则 6）+ 用户本轮 8 条 BUG + `docs/PROTOCOL.md` §8（服务端已落地的精确格式）。

## 1. 新增的核心：`client/mclink/core/punch_flow.py`

把打洞从"一个过程式 while 循环"变成**每个 Host↔Player 一条独立、双端同步的
状态机**（纯逻辑：不碰 socket / asyncio / 界面，所以每条规则都能离线钉死）。

| 基线 | 实现 |
| --- | --- |
| §E 严格顺序 | `Stage` + `STAGE_ORDER` + `ConnSession.advance()` |
| §E 等待同步起跑 | `mark_measured()` / `mark_peer_measured()` / `synced_start` |
| §F 每连接独立 | `ConnSession`（session_id/attempt_id/candidate/NAT/入站/同步/punch 次数/batch/序号/端口/interval/timer/connected/latency） |
| §F Timer | `ConnTimer`：`finish()` = 停止+清零+释放；`start()` 重连从 0 |
| §F 双端同步 | `attempt_id`（由 batch 派生）+ `adopt_batch()` → 错位抛 `SyncMismatch` → `void_attempt()` |
| §D 目标选择 | `select_targets()` / `host_id_of()` / `host_ready()` |
| §G 三层文案 | `summarize()`（父级只汇总，带 `*_key`/`*_args` 供三语言渲染）+ `ConnSession.row_text()/row_parts()` |
| 中继新字段 | `parse_host_state_tokens()`（`host_id=` / `started=`），`user = None` 表示"老中继未知" |

同步信标走中继**已有**的转发关键字（不新增协议）：

```
DIRECT_HIT stage=measured att=A batch=0 from=<cid> [to=<cid>]   # 我测完了（§E 等待同步起跑）
DIRECT_HIT stage=cand_ok  att=A batch=0 [to=<cid>]   # 我确认了 candidate
DIRECT_CAND <ip> <port> ... att=A batch=0 [to=<cid>]
DIRECT_PUNCH startIn=<s> att=A batch=0 [to=<cid>]
```

中继对这三条行只做**逐字转发**（`room_members._direct_peer` 只读 `to=` 一个
路由 token），所以这几个 `k=v` 是**既有加法机制**的使用，不是新协议。

### 1.1 `from=` 与「Host 怎么知道谁点了开始连接」（2026-09-28 二次修复）

中继的 `started=` 按 **PLAYER / HOST 连接**算（PROTOCOL §8.1），而这套客户端
的设计是「**先把洞打通、游戏再挂上来**」—— 玩家点了「开始连接」之后、Minecraft
连上来之前**没有 PLAYER 连接**，中继看不到他。于是「Host 只打已开始连接的
Player」（§D）把目标过滤成空，**洞永远打不起来**。

修前实测（`direct_e2e_test`）：

```
Host: started=['h-cid']  peers=['p-cid']   attempts=0（没有目标）
Player: 每 12s 一轮超时：sync start timed out (peer did not finish measuring)
中继日志: [ROOM CLOSE] sessions=0 idle_tunnels=2 waiting_players=0
```

修法（**纯客户端，中继一个字节不改**）：信标带 `from=<我的 client_id>`，
Host 把「对端主动发来的协调行」与 `started=` 取**并集**：

* `started=` 说谁开始了 -> 信；
* 对端自己发来 `DIRECT_HIT stage=measured from=<cid>` -> 更硬（他正在跑打洞）；
* 两条都没有 -> **不打**（BUG 4 仍然修好：没点开始连接的玩家永远不是目标）；
* 老客户端不发 `from=`（加法协议规则）-> 只在"房里只有一个对端"时归属
  （那是确定的，不是猜），否则不动。

同时把「我测完了」这条信标改成**与轮次无关**：两端重试节奏像两个节拍器，
要求 batch 相等会永远错开（实测 Host 第 10 轮 / Player 第 6 轮）。§F 要严格同步
的是 **Punch 那一阶段**的 attempt/batch/序号 —— 由 `DIRECT_CAND` /
`DIRECT_PUNCH` 自带的 `att=`/`batch=` 经 `adopt_batch` 把关。

回归：`punch_flow_test.py::test_host_targets_from_heard_evidence` /
`test_peek_direct_keeps_the_queue` / `test_beacon_lines_round_trip`，
端到端由 `direct_e2e_test` / `direct_multi_test` / `direct_reuse_test` 钉住。

### 1.2 三个直连套件曾经红过：根因与结论（2026-09-28）

`direct_e2e_test` / `direct_reuse_test` / `direct_multi_test` 在全量里三连红，
**不是 flaky**，是两个真问题：

1. **测试脚手架不真实**（三个套件都有）：`open_room(..., "p-cid", ...)` 用的是
   成员 client_id，而 `mkcfg()` 从 `load_config()` 拿的是**另一个随机**
   client_id 去发 PLAYER/HOST 握手。真实客户端两者**只有一个来源**
   （`client_id_var`）。两个 id 不同 -> 中继的 `started=`（按握手 id 记）和
   `peers=`（按成员 id）对不上 -> Host 没目标。
   修法：三个套件的 `mkcfg` 显式带上和 `open_room` 相同的 client_id。
2. **1.1 那条架构级不兼容**（真的产品问题，不是测试问题）：即使 id 一致，
   玩家在游戏连上来之前也没有 PLAYER 连接 -> `started=` 里没有他。
   修法：1.1 的 `from=` 证据并集。

另有一条被**基线推翻**的旧断言：`direct_multi_test` 原来要求"房主先点开始连接
就立刻在尝试（哪怕对方还没开始）"——那正是用户 BUG 4（"host 似乎已经在对他
打洞"）。已改成"没人在开始时 Host `attempts == 0`"。

## 2. 规则 6 审计：最近几轮被改坏/放宽的老规则（逐条）

| # | 规则 | 被改坏的地方（file:line） | 结论 | 修法 |
| --- | --- | --- | --- | --- |
| 1 | §E「测量在点开始连接之后」（不是打开软件就测） | `core/engine.py:778`（旧）`if enabled or standalone_host or ...: monitor.start()` + `gui/app.py:7431` 每次 tick 都传 `standalone_host` | **我们改坏的**（09-27 的 ISSUES 问题 2 把测量提前到启动） | `engine.py:781-789` 现在只按 `enabled` 启动；`standalone_host` 只记地址。回归：`gui_room_host_session_test.py::test_nat_measurement_waits_for_start` |
| 2 | §D / §B「Player 只认 Host」 | `room_direct.py`（旧 `_host_client_id()`）= **取花名册里第一个不是我的人** | **我们改坏的**（3.0.3 引入的启发式；BUG 1 的根因） | 删除该函数，改用 `punch_flow.select_targets`（`host_id=` → `host_name=` 在 `names=` 反查 → **不猜**）。回归：`punch_flow_test.py::test_player_targets_only_the_host` |
| 3 | §B「有 Host 但未开始连接 → 不得进入正式流程」 | `gui/app.py::on_start` 只判"有没有人认领"，`room_direct._peers` 也不看 `started=` | **我们改坏的**（门禁只在服务端，客户端没有） | `on_start` 新增 `punch_flow.host_ready()` 门 + 文案「Host 未连接，请等待 Host 开服」；打洞层同判据。回归：`test_player_waits_for_host_to_start` / `test_host_ready_three_valued` |
| 4 | §A「Owner 不可转移、不被继承」的**客户端表现** | `gui/app.py:619`（旧）`owner_peer = peers[0]` = "花名册第一个对端就是房主" | **我们改坏的**（BUG 2 的客户端那一半：host 被贴上「房主」） | 改为只认中继的 `owner_id=`（`room.py` 容错解析）；拿不到就**不标**。回归：`client_kick_roster_test.py`（"owner_id 未知 -> 一个对端都不许标房主"） |
| 5 | §A「只有 Owner 能切模式」 | `gui/app.py:3676 / 5779 / 7151`（旧）按 `_am_room_host()` 判 = **非 Owner 的 Host 也能点** | **我们改坏的**（服务端 09-27 已收紧，客户端没跟） | 新增 `_am_room_owner()`，锁与文案都按 Owner；文案「连接模式由房间所有者决定，其他成员不能修改」。回归：`punch_flow_test.py::test_passive_disconnect_two_verdicts` 里的 owner 检查 |
| 6 | §D「不得复用上一轮的状态」/ host 身份不得从协议痕迹推断 | `core/direct.py:1087`（旧）`peer_is_host` 由对端行带没带 `to=` 推断 | **我们改坏的**（`to=` 的用途只是路由，不是身份） | `direct.py` 该属性恒 False 并注明；`room_direct.link_rows` 改用 `host_id=` 精确匹配。回归：`test_host_ready_three_valued` + `link_rows` 的 host 匹配 |
| 7 | §F「Timer 每连接独立、终态清零」 | `room_direct.py`（旧）只有整轮的 `attempts/_peer_attempts`；打通之后计时还在继续 | **我们改坏的**（BUG 8） | 每 peer 一个 `ConnSession`；成功/放弃/换模式/退房都走 `finish()`（停止+清零+释放）；一轮失败只走 `note_round_failed()`（表继续跑）。回归：`test_timer_stops_zeroes_and_releases` / `test_each_connection_has_its_own_timer` / `test_round_failure_keeps_the_timer_but_terminal_zeroes_it` |
| 8 | §G「UI 三层 + 未连接写 N/C」 | `gui/app.py` 只有"一人一行"，没有 通路 n/m / 打洞进度 n/m 的汇总，行里也没有阶段 | **没做全**（不是被改坏，是从来没做完） | `nat_diag_path_text` / `punch_step_text` 走 `flow` 汇总；`_render_hole_rows` 走 `row_parts()`（`等待同步起跑（3s）【A】N/C` …）。回归：`test_three_layer_summary` / `test_per_target_row_text_and_latency` / `test_gui_three_layers_and_languages` |

**没有被改坏、复核确认仍然正确的**：Owner 只从 `OK CREATE/JOIN` 解析（`room.py:555`，
`HOST_STATE` 不参与，所以客户端不会自己"继承"Owner）；`auto_rejoin` 刻意带
`create=False`（`engine.py:1172`），不会重造房间把自己变成 Owner；房间被关 /
踢人 / Host 离线仍走各自的可识别分支。

## 3. 需要中继配合的一条（已由服务端修掉，留档）

`started_client_ids()` 曾经给"Host 已开始连接"加 `room.host_id`，而同一行的
`host_id=` 用 `host_client_id(room)` —— 两套 id。实测 `HOST_STATE ...
host_id=h-cid ... started=c5b71f4b8f33464b`。服务端已改为同源（`host_client_id`），
客户端侧只剩 1.1 那条"玩家还没有 PLAYER 连接"的架构性空缺，已用 `from=` 证据
补上。

客户端已做**容错**（`punch_flow._trusted_started`：`started=` 里一个已知成员 id
都没有 → 按"未知"处理，退回老行为），所以**当前不会坏**；但 §B 的门禁要真正生效，
需要中继把它改成 `add(host_client_id(room))`。

## 4. 回归网

新增 `tests/punch_flow_test.py`（119 项，已登记 `tests/run_all.py`）：§E 顺序 /
等待同步起跑 / §F Timer 与双端 attempt 同步 / §D 目标只认 Host + 新人加入不扰动 +
换模式严格重建 / §G 三层与 N/C 与三语言 / §C 被动掉线两类 + **真中继拔线端到端**
（老会话清理、端口释放、角色不变、重连成功）。

---

# 5. 2026-09-28 第二轮（用户实测 v3.4.0 的 8 条）

> 用户原话逐条见本轮任务单。下面只写**结论**：根因（file:line）、改法、回归测试。

## 5.1 逐目标行的文案：**真名 + 第 N 次 + 打通的地址:端口**

用户原话：「我之前给你的示例 ABCD 是例子呀，谁让你用这个计次数了，计数回到原来
的第几次第几次这个模式啊」「打通后……把打通的地址和端口给我打印在逐行里」。

**根因**：`punch_flow.ConnSession.row_text()` 把设计文档里的**占位示例** `【A】`
当成了"轮次标签"实现（`attempt_id`），并且打通那一行不带地址。

**改法**：

| 位置 | 改前 | 改后 |
| --- | --- | --- |
| `punch_flow.ConnSession.row_parts()` | `【attempt_id】`（A/B/C…） | `name`（对端真名）+ `addr`（打通地址） |
| `punch_flow.ConnSession.row_text()` | `等待同步起跑（3s）【A】N/C` | `等待同步起跑（3s）【玩家名】N/C` |
| 同上（打洞中） | `打洞中【A】第 3 次 · N/C` | `打洞中【玩家名】第 3 次 · N/C` |
| 同上（已直连） | `P2P 已直连【A】18ms` | `P2P 已直连【玩家名】203.0.113.12:40879 18ms` |
| `room_direct._session()` | 只记 peer | 把中继 `names=` 的显示名写进 `ConnSession.label`（拿不到回退 client_id） |
| `room_direct._punch()` 成功分支 | `finish(True, latency_ms=…)` | `finish(True, latency_ms=…, addr="ip:port")` |
| `gui/app.py` | `f"{name} {head} · {latency}"` | 模块级纯函数 `hole_row_text()`（界面与测试**同一份**代码） |

`attempt_id`（A/B/C…）**保留**，但只出现在两端互发的 `att=` 字段里（双端同步用），
**绝不上屏** —— 这就是"不许拿字母当计数/当名字"的落点。

回归：`punch_flow_test.py::test_per_target_row_text_and_latency`（真名 / 第 N 次 /
地址 / N/C）、`test_gui_three_layers_and_languages`（三语言，走同一个渲染函数）。

## 5.2 状态迁移表（用户第 3 条：手动中断后重连流程理顺）

用户原话：「手动中断后重连流程好像有点小麻烦啊，每一步严格确认这采用握手的方式
我觉得可以，但流程似乎有点小混乱，你看流程可否优化一下」。

**策略不变**：每一步仍然**严格握手确认**（§E）；理顺的是**迁移**。

**唯一代码来源**：`core/punch_flow.py::TRANSITIONS`（`tests/punch_flow_test.py`
逐条比对，文档与代码不许分叉）。

| # | 阶段 | 允许的下一阶段 | 触发条件 |
| --- | --- | --- | --- |
| 1 | `idle` | `confirm_host` | 用户点「开始连接」（`ConnSession.start()` 只允许从 `idle`/`confirm_host` 起步） |
| 2 | `confirm_host` | `confirm_target` | 目标由中继事实确认（`punch_flow.select_targets`：Player 只认 Host；Host 只认 `started=`） |
| 3 | `confirm_target` | `nat` | 进入本机 NAT 测量（并广播 `DIRECT_HIT stage=nat`） |
| 4 | `nat` | `inbound` | NAT 测量结束（**无论**是完整测还是复用房间画像；阶段都必须走） |
| 5 | `inbound` | `report` | 入站策略探测结束（结论已知时只复用结论，**阶段照样走**） |
| 6 | `report` | `sync_start` | 本端测量结果已上报，广播 `stage=measured` |
| 7 | `sync_start` | `candidate` | **双方都测完**（`synced_start`；用户 §E 确认："先测完的一方等对端也测完"） |
| 8 | `candidate` | `confirm_cand` | 交换 candidate（`DIRECT_CAND` 带 `att=`/`batch=`） |
| 9 | `confirm_cand` | `sync_go` | **双方都确认** candidate（`cand_confirmed`） |
| 10 | `sync_go` | `punch` | 约定的起跑时刻到（`DIRECT_PUNCH startIn=`） |
| 11 | `punch` | `connected` / `failed` | 打通 / 这一轮失败 |
| 12 | `connected` / `failed` | —— | 终态不可逆（任何迁移都被拒绝） |

**回起点只有三条显式边**（都由具名方法做、都落日志；`advance()` 拒绝一切回退）：

| 边 | 方法 | 触发条件 | 表/计数 |
| --- | --- | --- | --- |
| `any -> confirm_host` | `note_round_failed(reason)` | 这一**轮**没打通（不是终态） | 表继续跑（§F：只有终态清零） |
| `any -> confirm_host` | `void_attempt(reason)` | 双端 `att=`/`batch=` 错位（§F 最重要的一条） | 表继续跑，`voided += 1` |
| `any -> confirm_host` | `restart()` | **手动停止后重新开始**（新的一次连接） | 表从 0、`generation += 1`、`batch` 归 0 |

**手动重连的顺序**（用户第 3 条的核心）：

```
停止（on_stop）           -> engine.stop_session + watch_room_nat(False) + STARTED 0
                            （RoomDirectLink.stop(): 每条连接 timer.finish() = 停/清/释放）
重新开始（on_start）      -> STARTED 1 -> 会话起来 -> watch_room_nat(True)
                            RoomDirectLink._run(): 目标表重新拉；
                            每条的 timer 已释放 -> restart()（generation+1）
                            -> §E 第一步重新走，每一格都能显示正确
```

以前是 `start()` 把阶段**硬拨回** `confirm_host`（`_enter` 只看"值变了没"），
不落日志、也分不出"接着上一轮"和"新的一次连接"；现在这条路是显式的
`restart()`，`start()` 拒绝从中间阶段起步（会落一行日志）。

回归：`punch_flow_test.py::test_transition_table_matches_the_code` /
`test_no_stage_is_skipped_or_revisited` / `test_manual_stop_replays_every_stage`。

## 5.3 本机测量阶段可见 + 两端阶段对得上（用户第 4 条）

用户原话：「本地测 NAT 和入站，我这边直接秒测完显示等待对端，但对端显示我正在
测 NAT，我本地的测量给我也打印在状态里」。

**根因**：进入测量时**不广播**"我在测哪一步"，`stage=measured` 只在测量**全部
做完**之后才发 —— 对端在整个测量期间拿不到任何信息，只能显示"还没测完"。

**改法**（不新增协议：中继对 `DIRECT_HIT` 本来就逐字转发）：

| 时刻 | 本端显示 | 对端显示 |
| --- | --- | --- |
| 开始测 NAT | `测量 NAT 类型中…` | `等待对端测量 NAT 类型` |
| 开始测入站 | `测量入站策略中…` | `等待对端测量入站策略中` |
| 本端测完 | `等待对端测量 …`（按对端小步） | 同上 |
| 两边都测完 | `等待同步起跑`（基线原词） | `等待同步起跑` |

实现：`ConnSession.begin_measure("nat"/"inbound")` + `room_direct._announce_stage()`
（进入测量**之前**各广播一条 `DIRECT_HIT stage=nat|inbound`）；
`_wait_synced_start()` 收到这两条就 `note_peer_stage()` 并**继续等**（它还没测完）。
`stage_text` 把 `sync_start` 渲染成上面那两句等待文案（**不是新阶段** —— 阶段
序列与迁移表一个字没变）。老客户端不发小步 -> 显示退回基线「等待同步起跑」。

回归：`punch_flow_test.py::test_local_measurement_is_visible_on_both_sides`。

## 5.4 host 端「等待对端连接…」（用户第 5 条）

用户原话：「玩家在房内但未连接时，host 端应显示「等待对端连接…」，现在错误显示
「等待同步起跑」」。

**根因**：`_run()` 在"没有目标"时只有两种文案（`等待对端用户加入…` /
`Host 未连接…`），**分不出**"房里没人"和"房里有人、但没人开始连接"；而上一轮的
`_punch_phase` 可能还挂着"等待同步起跑"，看起来就是错的阶段。

**改法**：
* 新增 `RoomDirectLink.PHASE_WAITING_FOR_PEER_CONNECT = "等待对端连接…"`（静止、
  不倒计时，和 `等待对端用户加入…` 同一条规矩）；
* `_run()` 判据：`host_not_ready` -> 「Host 未连接，请等待 Host 开服」；
  **花名册里还有别人、但一个目标都没有** -> 「等待对端连接…」；
  花名册是空的 -> 「等待对端用户加入…」；
* 客户端现在会发 `STARTED 1/0`（PROTOCOL §8.6）：Host 的 `started=` 当场反映
  "谁点了开始连接"，所以"房里有人但没开始"是一个**确定**的状态，不是猜的。

回归：`punch_flow_test.py::test_host_waits_for_peer_to_connect`。

## 5.5 广播驱动的连接状态（用户第 7 条）

用户原话：「这边玩家断开连接，那边 host 这个连接状态还是显示已连接，未及时更正
状态，所以我觉得可以把连接状态也包进我们管理广播 TCP 里」。

**契约**：`HOST_STATE` 的 `conn=<cid>:<state>,...`（PROTOCOL §8.5），4 个白名单
取值 `idle|waiting|pairing|connected`，每次变化立刻广播。

**改法**：`room.parse_member_conns()`（容错：按 `,` 切、按第一个 `:` 切、白名单
外的整条跳过）-> `RoomState.member_conns` -> `build_member_rows()` 的
`connected`/`conn_state` -> `_member_status_text()` 渲染
（未连接 / 等待配对 / 配对中 / 已连接）。**老中继没有 `conn=` 时回退旧行为**
（房间里的人一律「已连接」），绝不显示成"未连接"。

回归：`punch_flow_test.py::test_conn_broadcast_drives_member_status`。

## 5.6 热切换 = 所有人收到通知 -> 严格重建全部隧道（用户第 6 条，P0）

用户原话：「房主切换后，通知所有人给我重建所有隧道并热重连啊，现在热切换不能用了，
连不上，隧道好像没有重建」。

**根因（两个，缺一不可）**：

1. **竞态**：`_rebuild_tunnels()` 的工作线程去读 `self.config.mode`，而这是界面
   线程**每秒都在改**的共享字段（`_refresh_room_ui` 把房间模式写进去）。切模式那
   一瞬间中继的 `HOST_STATE mode=` 还没到，界面线程完全可能把 `config.mode` 写回
   **旧模式** —— 新会话按旧传输协议建立（房间写 STUN、会话走中继，或反过来），
   表现就是"隧道没有重建、怎么都连不上"。
   修法：**模式在调用线程上快照**（`_rebuild_tunnels(mode=…)` 的新参数），工作线程
   用 `config.mode = 这个快照` 显式覆盖；调用点：`_apply_room_mode(mode)`、
   `_sync_session_mode(room_mode)`、`_reconnect_session(房间当前模式)`。
2. **`CONN_MODE_CHANGED` 没走到重建**：中继这一行是在 `HOST_STATE mode=<新模式>`
   **之前**发的（先拆旧通路、再广播状态），所以收到它时 `_room_conn_mode()` 还是
   旧模式 —— 拿它去重建等于按旧模式再建一遍。修法：只打 `_mode_rebuild_pending`
   标记，房间广播里的**新模式一到**（`_refresh_room_ui` ->
   `_maybe_rebuild_for_mode_change()`）立刻按新模式重建一次；会话已经被中继拆掉
   （`session_mode()==""`）也算"该重建"（以前那条路会直接放弃、只剩自动重连的退避）。

回归：`tests/mode_switch_rebuild_test.py`（真中继 + **三方**：房主切模式 ->
三方各自重建 -> 能再次打通，字节逐字校验）+ `punch_flow_test.py` 里界面侧的
"模式快照"断言。

## 5.7 握手速度 / 体验（用户第 8 条）

* **`STARTED 1/0`**（PROTOCOL §8.6）：点「开始连接」当场进 Host 的目标表，不用再
  等对端第一条 `from=` 信标（省掉最多一个 `RETRY_INTERVAL_S`）。`from=` 的并集
  兜底**保留**（老中继兼容），但新链路不再依赖它。
* **测量小步信标**（5.3）：对端在整个测量期间都能看到"对方还在测哪一步"，不再是
  一段没有任何信息的黑箱；`等待同步起跑` 的两端一致也由它保证。
* **目标表一变就醒**（`room_direct._sleep_or_wake`）：原来每轮之间硬睡
  `RETRY_INTERVAL_S`(3s)，哪怕有人刚点了开始连接也得等满；现在按 0.25s 切片观察
  `_peers()`，**出现新目标就立刻开打**。重试节奏本身没变（没人来还是 3 秒一轮），
  变的是"人来了那一拍"的反应时间。
* 六个阶段（`等待对端测量 NAT 类型` / `等待对端测量入站策略中` / `等待同步起跑` /
  `等待同步起降` / `打洞中` / `已直连|失败`）在**两端**由同一个状态机、同一套
  迁移表产出；`advance()` 只许 +1，`_run()` 每轮重新拉目标 —— 两端不可能各说各话。

## 5.8 本轮改动的回归网

新增 `tests/mode_switch_rebuild_test.py`（登记进 `run_all.py`），覆盖：

1. 状态迁移表 == 代码（`TRANSITIONS`）且跳步/回退被拒；
2. 手动停止后重连 -> 每一格重新走一遍、`generation+1`、表从 0；
3. 本机测量阶段在**两端**都可见（真名 + 第 N 次 + 地址:端口）；
4. host 端「等待对端连接…」（房内有人、没人开始）；
5. `conn=` 广播驱动成员连接状态（老中继回退）；
6. `STARTED 1/0` 与会话/房间生命周期一致（同一状态只发一次）；
7. **真中继端到端**：房主切模式 -> 三方各自重建 -> 再次打通 + 数据逐字节正确；
8. 界面侧"重建按调用那一刻的模式"，不受之后每秒刷新影响。

---

# 6. 2026-09-28 第三轮：「洞打通马上就断」

> 用户原话：「打洞工作流有点混乱，洞打通马上就断……你再检查一遍所有代码逻辑，
> 然后把打洞的流程树状整理，然后再去修复，这一次就只修改这一个问题」。

完整流程树（含每一片叶子的 `file:line`）见
`docs/PUNCH-FLOW-TREE-20260928.md`。

## 6.1 根因（file:line）

`room_direct._run()` 每一轮开头都会看 `ConnSession.needs_new_cycle`：

* `room_direct.py:1282-1297`（每轮为目标建/刷新状态机）；
* `punch_flow.py:491-503` `needs_new_cycle = terminal or timer.released or idle`；
* `punch_flow.py:93` `TERMINAL_STAGES = {CONNECTED, FAILED}`。

打通走的是 `_punch` 成功分支 → `sess.finish(True, …)`（`room_direct.py:1931`
→ `punch_flow.py:836`）：`CONNECTED` + Timer 已释放。于是 **3 秒后**
（`RETRY_INTERVAL_S`，`room_direct.py:75`）的下一轮：

```
sess.timer.running == False           -> 不跳过
sess.needs_new_cycle == True          -> sess.restart()   room_direct.py:1290
     connected=False / stage=confirm_host / batch=0 / generation+1
```

两端同时发生，界面从「P2P 已直连：1/1」+「P2P 已直连【名】ip:port 18ms」变成
「直连尝试中... 0/1」+「确认 Host（0s）【名】N/C」，日志多一行
`[direct] new connection cycle for <peer> (generation N -> N+1)`。

**socket 没有被关**（`peer_ready()` 仍为真 → 打洞循环照旧跳过它，没有第二次
`ROOM LINK UP`）；断掉的是「这条连接已建立」的账 —— 这正是用户在界面上看到的
那一行。这一点在报告里必须说清楚，不许夸大成"链路被拆"。

## 6.2 最小修复

`room_direct.py:1293-1315`：在 `needs_new_cycle` 之前加一道门 ——

```python
if sess.connected and self.peer_ready(peer):
    continue
```

判据与"跳过已打通目标"完全同源（`peer_ready()` 读
`DirectLink.keepalive_running`，隧道接手后由 `carried` 兜住）；洞真的死了
（判死看门狗 / 对端离开 / 隧道握手失败）时 `peer_ready` 为假，`restart()` 照旧，
§F 的"作废 → 重新同步 → 再打"一个字没变。手动停止/切模式走的是
`timer.finish()` + `close()`，不受这条门影响。

## 6.3 回归网

新增 `tests/direct_persist_test.py`（登记进 `run_all.py`）：

1. 离线版：跑真 `_run()`，打通后跨两个重试轮，断言阶段仍是 `connected`、
   `generation` 不变、没有新的 `new connection cycle`、`attempts == 0`；
2. 真中继 + 真引擎 + 真 Minecraft 连接：打通后连续 8 秒（跨两个 3s 重试轮）
   断言阶段/轮次/`flow.all_up`/`tunnels_up` 全都不变，并且每秒一次双向 echo
   **一次都不许断**。

---

# 7. 2026-09-28 第四轮：六个隐患（用户批准，逐条修复 + 逐条回归）

> 上一轮只修了「打通即断」的**一个生产调用点**（§6），当时把另外六条刻意留着了。
> 本轮把这六条一起修掉，回归网是 `tests/direct_hardening_test.py`（登记进
> `run_all.py`）。**基线（规则 6 / `DESIGN-BASELINE-20260927.md` §D/§E/§F/§G +
> §H）一条没动**：每目标独立会话、严格同步阶段、终态 Timer 清零、不复用旧目标/
> candidate、"换模式严格重建全部通路"、UI 不许改 P2P 行为，全部保持。

| # | 根因（file:line） | 改了什么 | 回归测试（`direct_hardening_test.py`） |
| --- | --- | --- | --- |
| 1 | `punch_flow.py` `TERMINAL_STAGES = {CONNECTED, FAILED}` + `needs_new_cycle = terminal or timer.released or idle`：**成功**终态也读成"要重开" | 拆成 `SUCCESS_STAGES` / `FAILURE_STAGES`；`success_terminal` 短路 `needs_new_cycle=False`；新增无歧义 API `ConnSession.cycle_action(link_alive)`（`keep`/`restart`/`start`），`_run()` 只认它 | `test_success_terminal_is_not_a_new_cycle` / `test_run_keeps_a_punched_link_beyond_the_retry_rounds` |
| 2 | `room_direct.transport_ready` 最后一条 `link is not None and peer_addr is not None`：只看引用，不看链路死活，`start()` 的幂等闸门（`_established and transport_ready`）会在洞死之后永远挡住重打 | `transport_ready = running or ready`；新增 `_link_alive()`（keepalive 线程活着 / `carried` / 未 `closed`），`ready` / `peer_ready` 共用 | `test_start_repunches_when_the_hole_is_dead` |
| 3 | `_sweep_links` 用**打洞目标表**当花名册：`started=` 抖一下就把活着的洞当"对端走了"拆掉 | `_sweep_links(roster=None)` 默认读 `RoomState.peer_ids`（`_room_roster()` 返回 `known`/`ids`，未知就不按"走了"拆），且只对"曾是花名册真成员 id"的 key 生效（`_member_ids`，本地别名 `host_name`/`guest-N` 不被误杀）；`_run()` 主调用不再传目标表；no_peer/no_room 仍显式传 `set()` | `test_started_flap_keeps_a_live_link` / `test_roster_leave_still_drops_but_aliases_survive` |
| 4 | `_open_tunnel` 两次 HELLO 失败就 `_drop_peer()` 重打：新 socket = 新 NAT 映射 = 又一次"打通又断" | 同一个 socket（同一 carrier）上 `TUNNEL_HANDSHAKE_ATTEMPTS=3` 次重试 + 指数退避；只有 `_link_socket_dead()`（链路已 `closed`）才重打；重试用尽但 socket 活着就**保留**，由判死截止时间兜底 | `test_tunnel_handshake_reuses_the_punched_socket` |
| 5 | `_sweep_links` 在 `carrier is None or not carrier.started` 时直接 `continue`：隧道一直没起来也永远不判死 | 新增 `TUNNEL_SETUP_DEADLINE_S=45s`（> 握手预算 3×8s+退避）与 `_link_overdue()`；到点判死、`_drop_peer()`、下一轮重打 | `test_tunnel_setup_deadline_repunches` |
| 6 | `flow_summary` 读路径上调 `_session(peer)`，把 link-only 目标**写进** `self.sessions`（界面每刷新一次多一条，generation/stage 被读路径改） | `flow_summary` 改成纯函数：不 `_session()`、不改 `target_reason`；link-only 目标用**用完即弃**的 `_link_only_session()` 只渲染一行；`_peers()` 拆出纯函数半边 `_select_targets_now()` 供 `_session_order()` 用 | `test_flow_summary_is_read_only` |

**没有碰的东西**：`server/**`、`panel/**`、`deploy/`、版本号（由协调者统一 bump）。
`tests/direct_persist_test.py`（上一轮那条门）**一个字没删、没弱化**，本轮 3 连跑
全绿。




---

# 8. 2026-09-29 第五轮：两端显示同步 + 一端已直连的收敛 + 连接状态传播 + 身份/连接分离

> 用户本轮口径（逐条）：两端共用一套状态枚举、每步上报、等对端匹配才推进**显示**；
> 「把打洞流程映射到显示上」（不是反过来）；P0「一端已直连、另一端还在重试」；
> 热切换后的自动重连语义；连接状态用 `conn=` 传播；每对独立 + 打洞前重新确认目标；
> 显示升级（PUNCH #NNNN / 等待对端…）；**只有 Owner 的显式操作能改模式**。
> 硬基线（规则 6 / §H / §H-2）**一条没动**。

## 8.1 共享阶段枚举 + 逐阶段上报 + 「等对端匹配」（只改显示）

| 位置 | 改了什么 |
| --- | --- |
| `punch_flow.py:213` `WAIT_LABELS` | 新增「等待对端确认 Host / 确认目标 / 测量 NAT 类型 / 测量入站策略中 / 上报测量结果 / 同步起跑 / 交换候选 / 确认 candidate / 起跑 / 打洞 / 确认已直连」 |
| `punch_flow.py:681` `note_peer_stage` | 从"只认 `nat|inbound|measured`"扩成**认整条阶段轴**（`Stage.value`），记进 `peer_seen`；`connected` 另记 `peer_connected` |
| `punch_flow.py:726` `report_stage` | 本端进入某阶段时记下要上报的值（`stage=` 信标就是它） |
| `punch_flow.py:780` `peer_proven_index` | 对端**被证实**走到哪一格：信标 + 双方共同完成的里程碑（`synced_start`→≥`SYNC_START`；`cand_confirmed`→≥`CONFIRM_CAND`） |
| `punch_flow.py:811` `display_stage` | 显示 = `min(本端阶段, 对端被证实的阶段)`（对端一次都没报过 -> 显示本端，兼容老客户端与基线原文） |
| `punch_flow.py:852` `waiting_for_peer` | 对端落后才为真 -> `stage_text` 用 `WAIT_LABELS` |
| `room_direct.py:2508` `_broadcast_stage` | 每个阶段进入即发 `DIRECT_HIT stage=<阶段> att= batch= from=`（复用中继**本来就有**的逐字转发，不新增协议） |
| `room_direct.py:2474` `_attempt_with_stage_polls` | 打洞（可能好几秒）期间每 0.3s 同步 peek 一次对端阶段 —— 否则整段 attempt 里显示停在上一格 |
| `punch_flow.py` `Stage.PUNCH` 真的被推进 | 以前 `_punch` 从不 `advance(PUNCH)`，于是真正打洞时逐目标行显示的是「等待发包起跑」；现在阶段轴与真实动作对齐 |

**只让显示等，不让打洞等**：`advance()` / `_punch` 的真实迁移一个判据都没加
`peer_seen`；§E 本来就要求的三个等待点（等待同步起跑 / 双方确认 candidate /
同步起降）保持原样。回归：`punch_display_sync_test.py` A 节（含"显示等对端绝不
拖慢真实阶段"的反向断言）。

## 8.2 P0：一端已直连、另一端还在重试（根因 + 修法）

**根因（file:line）**：打通只写在本端状态机里（`room_direct.py` `_punch` 成功分支
`-> sess.finish(True)`），**没有任何"我已直连"的上报**。对端唯一的希望是它自己的
`link.attempt()` 也命中；两端重试节奏错开 / 非对称 NAT 时它可能一直不命中，于是
一端「P2P 已直连【…】ip:port」（游戏能进），另一端每 3 秒一轮
`确认 Host（0s）… N/C`（状态机被自己抹掉）。`conn=` 是**成员↔中继**的连接状态，
不是"这条洞已建立"，所以它也补不上这个空缺。

**修法**：

* 打通那一侧**立刻**上报 `DIRECT_HIT stage=connected`（`room_direct.py:2414` 调用，
  `_broadcast_stage` 发出）；
* 对端在 `_note_heard_started`（`room_direct.py:1318`，`peek_direct` 只读队列）
  看到 `stage=connected` -> `_adopt_peer_connected`（`:1409`）：
  - 判据要硬：必须手上有这条 peer 的打洞链路（`_punch_links`）且没被关、
    且已知道对端地址（`peer_addr` 来自 candidate 交换）—— 否则**不**收敛；
  - 在**同一个已打通的 socket** 上补保活 + 建隧道，`sess.finish(True)`，
    登记 `links[peer]` -> `peer_ready` 为真 -> `cycle_action` = `CYCLE_KEEP`
    -> **停止重试**；
  - 兜底一条不少：隧道 45s 起不来 / 20s 没数据 -> `_sweep_links` 照旧判死重打；
  - 喷洒还在跑时不接管（`_punching_peers`），避免和 `attempt()` 抢同一个 socket。

**回归**：`punch_display_sync_test.py` C 节（对端上报 -> 收敛 + `CYCLE_KEEP` +
generation 不变；反例：没链路不收敛、失败终态仍重开）；真中继通道由
`direct_persist_test.py` 新增断言「两端都收到了对端的 `stage=connected`」钉住
（实跑 17/17）。

## 8.3 连接状态 ≠ 身份/职位（用户 2026-09-29 硬口径）

* `punch_flow.py:1627` 新增 `host_availability()`，把两套状态**分开命名**：
  `no_host`（身份：没人认领，**这一种才允许别人认领**）、
  `host_not_connected`（连接：位子有人但没开服）、`host_ready`、`unknown`。
  `host_ready()`（`:1592`）现在是它的薄包装（返回值语义一个字没变，老中继仍返回
  `None` = 不知道）。
* `select_targets`（`:1685`）在原有 `started=` 判据之外，也认 `conn=<host>:idle`
  -> `host_not_ready`（身份在、连接不在 -> 提示「Host 未连接，请等待 Host 开服」，
  **不是**「暂无 Host」）。
* `gui/app.py:6053`「认领主机身份」按钮现在**只看身份**（`host_claimed`）：
  主机只是停止连接时，别人**不许**能认领（这是用户点名的行为）。
* `gui/app.py:6130` 主机状态文案按连接状态分两句：「正在开服，可以开始连接」/
  「已断开连接 / 未开服」。
* `gui/app.py:468 host_conn_state()`：`conn=connected` -> connected；`conn=idle`
  -> idle；`waiting|pairing` 退回 `started=`；拿不到 -> unknown（**绝不**因此
  显示成"未开服"）。

## 8.4 Owner / Host / Player 是组合角色

`gui/app.py:446 member_roles()` + `:509 roles_text()`：身份列现在逐字显示
`房主·主机` / `房主·玩家` / `玩家·主机` / `玩家`，**不再互斥**；
身份**绝不**从 `conn=` 推导。`client_kick_roster_test.py` 里两条旧断言按新口径
更新（`主机` -> `玩家·主机`），并新增组合角色断言。

## 8.5 模式：只有 Owner 的显式操作能改；未知 != relay（第 7 条根因）

* **根因**：`gui/app.py` 原来那句 `mode_now = self._room_conn_mode() or MODE_RELAY`
  —— 只要有一秒广播里没有 `mode=`（正在换模式拆通路 / 房间状态抖动 / 老中继），
  就会把 `_punch_mode` 从 `stun` 改成 `relay`，紧接着 `watch_room_direct(False)`
  **把刚打通的直连整个拆掉** —— 用户看到的"直连玩一会儿断开、模式自己跳到中转"
  就是它。现在（`gui/app.py:7796`）：模式未知**什么都不做**（保持上一次已知），
  `_punch_mode` 只有一个写入点。
* 中继侧复核（只读）：`server/room_members.py:1302-1303` 是**唯一**改
  `room.conn_mode` 的地方（`CONNMODE` 命令，要求 Owner）；`relay_server.py:618`
  的 `conn_mode_reason` 是服务端本轮新增的"记住并恢复显式选择"，**不是自动跳
  relay**。所以本条判定为**纯客户端显示/派生 bug**，不需要改服务端。
* 另加一行可归因日志：房间模式从 A 变 B 时（`_refresh_room_ui`）打印
  `[mode] the relay now reports the room mode as …`，下次贴日志能一眼看出是谁改的。

## 8.6 每对独立 + 打洞前重新确认目标（第 5 条）

本轮复核确认既有实现已满足（`_run()` 每轮 `_peers()` 重拉、每 peer 一个
`ConnSession`），并补纯回归：`punch_display_sync_test.py` E 节 —— A 打通不影响 B、
C 加入只多一行、host 从 `h-cid` 换成 `d-cid` 后下一轮目标跟着换。

## 8.7 热切换的自动重连语义（第 3 条）

按 `self.running`（本客户端是否真的在连接）分流，两条路都**不允许**跨过它：

* 空闲/未连接成员：`_session_mode_rebuild_delta()` / `_maybe_rebuild_for_mode_change()`
  都要求 `self.running` -> 模式变了也**不重建**（"只换模式"）；
* 原本有连接的成员：会话模式 != 房间模式 -> 严格重建一次（带冷却，同一目标不重复）；
  会话已被中继拆掉（`session_mode()==""`）而新模式已广播到 -> 照样重建
  （否则就是用户报的"热切换不能用了"）。

**消费服务端本轮新增的加法字段（PROTOCOL §8.9）**：`room.py` 的
`conn_mode_changed` 解析新增 `was_live=0|1`；`gui/app.py` 新增纯函数
`mode_switch_should_reconnect(was_live, running)`：`was_live=0`（切换前没连接）
-> **不重建**；`=1` -> 重建；老中继没这个字段 -> 退回本地 `running`。
"原本空闲只换模式 / 原本有连接才自动重连"因此由**服务端事实**决定，不靠本地推测。
`HOST_TAKEN`（`room.py:1751`）也已识别 -> 主机位仍在（哪怕未开服）时认领被拒，
与 §8.3 的界面锁一致。

回归：`punch_display_sync_test.py` G 节（空闲不重建 / 有连接重建 / 冷却只请求一次 /
新模式未到就等 / `was_live` 三态）；真中继三方全连接的热切换仍由
`mode_switch_rebuild_test.py` 钉住（实跑 56/56×3）。

## 8.8 本轮回归网

* 新增 `tests/punch_display_sync_test.py`（**57 项**，已登记 `run_all.py`）。
* `direct_persist_test.py` 新增真中继断言：两端互相收到 `stage=connected`。
* `client_kick_roster_test.py` / `member_name_rtt_test.py`：身份列断言按
  "组合角色"口径更新（`主机` -> `玩家·主机`）。

## 8.9 收尾实跑结论（2026-09-29）

* `XDG_CONFIG_HOME=$PWD/.testcfg python3 tests/run_all.py`：**52 套里 51 套 PASS**，
  唯一红的是 `部署包预检`，而它唯一的红是 `CraftLink.exe 比源码旧`
  （exe 只能在 Windows 侧重打包 —— 允许的红）。
* `python3 scripts/check_deploy.py`：**74 passed / 1 failed**（同上那一条）。
* 时序敏感套件各 3 次全绿：`direct_e2e` 14/14×3、`direct_reuse` PASS×3、
  `direct_multi` 14/14×3、`direct_persist` 17/17×3、`direct_hardening` 36/36×3、
  `conn_mode_hotswitch` 41/41×3、`mode_switch_rebuild` 56/56×3。

## 8.10 没做完 / 需要别人做的

1. **真·非对称场景（只有一端命中）无法在本机复现**：本机只有一个 NAT，两个引擎
   在回环上基本总是**同时**命中。所以 P0 的"另一端"用**离线 + 真 `_run()` 装置**
   钉住（`punch_display_sync_test.py` C 节），真中继那一条证明的是**信标通道通了**
   （两端都收到 `connected`）。用户真实网络上的最终确认仍需要他实测。
2. **三方"2 已连接 + 1 只加入未连接"的真中继热切换**没有单独写（`mode_switch_rebuild_test`
   的三方是全都已连接）。目前由 G 节的**逐成员判定**回归（含 `was_live`）+
   既有三方全连接套件共同覆盖；纯"未连接成员保持空闲"在引擎层是平凡的
   （他根本没有会话）。
3. **exe 未重打包**（规则 3：只能在 Windows 侧做）；版本号由协调者统一 bump。
4. 本轮期间服务端在并发编辑（遇到过 `room_members.py` 瞬时 IndentationError、
   `Room.conn_mode_reason` AttributeError、`POST /rooms/<r>/revoke` 超时），
   这些都已在上面的收尾实跑里消失/由服务端侧收敛，**客户端侧未做任何规避**。
