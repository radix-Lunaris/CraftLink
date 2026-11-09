# 会话自动重连方案（用户 2026-09-27 批准）

> 交付：客户端为主（`client/mclink/**`）。本轮**未动** `server/**`、`panel/**`、
> `deploy/**`，未升版本号（协调者负责）。
> 相关：`docs/ISSUES-20260927.md`（问题 1/4/6 的上下文）、
> `docs/LOG-AUDIT-20260927.md`（上一轮的会话模式重建）。

---

## 1. 触发条件

**只有"非用户意图的断开"才重连**：

| 触发 | 来源（客户端可观测点） |
|---|---|
| 中继 TCP 断 / 心跳超时 | `room_lost` 事件（`room.py::_ping_loop` 的 PONG 看门狗 → `_explain_disconnect` → 房间还在 = `room_lost`） |
| 隧道或配对在游戏中非终态地被关闭 | 同上（会话侧 `relay_error` / `minecraft_disconnected`） |
| 本机会话核心自行退出（例如监听端口重试后仍失败） | `listen_failed` 事件 |
| 换模式重建失败 | `rebuild_failed` 事件（不再只留一句"可手动重新连接"） |
| 长时间无进展的重建 | `RECONNECT_WINDOW_S = 60s` 用尽 |

## 2. 绝不重连（终态，只弹窗一次）

`gui/app.py::_is_terminal_failure()` 一张表认这些可识别原因：

* 用户点停止 / 主动退房 —— `on_stop()` / `on_leave_room()` 会把
  `self.running = False`，`_can_auto_reconnect()` 第一道门就返回 False；
* 被踢 —— `KICKED`；
* 房间被销毁 —— `ROOM REVOKED` / `ROOM_CLOSED` / `OWNER_LEFT_ROOM`；
* 版本不一致 —— `VERSION_MISMATCH`；
* 登录失效 —— `AUTH_INVALID_TOKEN` / `AUTH_FAILED` / `AUTH_REQUIRED` /
  `AUTHENTICATION` / `NOT_LOGGED_IN`。

命中即 `_reconnect_terminal(reason)`：界面进 `停止（原因）`、弹一次窗、
按钮恢复可用。

**房主离线的 `HOST_LEFT` / `NO_HOST` 是另一条路**：不算失败、也不算终态，
进 `等待房主上线`（见 §5）。

## 3. 状态机（界面四态）

```
                 非用户意图断开
   运行中 ─────────────────────────► 正在自动重连（第 N 次，Xs 后）
     ▲                                        │
     │  rebuild_done                          │ 退避到点
     └────────────────────────────────────────┘
                                              │ 60s 窗口用尽
                                              ▼
                                    停止（终态原因）  ← 弹窗一次

   房主离线（HOST_LEFT / NO_HOST） ──► 等待房主上线（不重试，按钮可点重试）
```

实现：`gui/app.py::_reconnect_state_text()` 直接产出这四句中文，界面显示在
`connect_hint` 与状态卡上；`_reconnect_phase` 是唯一事实
（`running` / `reconnecting` / `waiting_host` / `stopped`）。

## 4. 退避与窗口

```python
RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)   # 首次 0.5s，之后 15s 封顶
RECONNECT_WINDOW_S  = 60.0                                # 从第一次触发算起
```

* 每次触发先记 `[reconnect] attempt N in Xs (reason=…)`，再用一个**后台
  延时线程**睡到点，然后 `root.after(0, …)` 回到 Tk 主线程重建
  （后台线程碰 tkinter 会抛 "main thread is not in main loop"）；
* **同一时刻只允许一个重建在飞**：`_can_auto_reconnect()` 检查
  `_rebuild_in_flight`（既有闸门，见上一轮的会话模式重建）；
* 到点没成功 → 下一次退避；**总窗口用完** → `_reconnect_terminal` +
  `messagebox.showwarning`（中/英/日三语）+ 按钮恢复可用。

## 5. 每次重连的固定顺序

1. **停旧会话并确认本地代理端口释放**：`engine.stop_session(timeout=15)`
   （它内部超时走 `abort()`，端口必定释放；返回 False 也会照实落一行日志）；
2. **角色按"我是否持有 host 位"定**：`_safe_connect_role()` —— 和 `on_start`
   同一个判定函数，**绝不退化成 player**（上一轮的 P0：房主被重建成玩家 =
   房间失去 host）；
3. **按房间当前模式重建**：`_room_conn_mode()`（中继广播的），不是本机磁贴；
4. **立刻重排房间级任务**：`rebuild_done` → `_sync_room_nat()`（NAT 监测 +
   打洞），不留"洞还没开始打"的空窗；
5. **不释放 host 位**：重建路径里没有任何 `release_host()`；现行规则只有
   "先停止再释放"能交出去。

这五条 `_rebuild_tunnels()` 本来就都做到了，所以
`_reconnect_session()` **只补一行可观测日志再调它** —— 绝不另写一份平行实现。

## 6. 场景矩阵

| 场景 | 行为 |
|---|---|
| host 自己断了 | 自动重连成 **host**（角色不变）；玩家侧看到 `等待房主上线`，不刷屏；重连成功后自动恢复 |
| 玩家断了 | 只重连**自己这一条**（`HostCore` 的多隧道结构保证别人的不受影响） |
| 房间暂时无 host | 玩家侧**不重试**，转 `等待房主上线`；点「开始连接」被拒 + 提示 |
| host 离开 | 中继断开所有玩家并给 `HOST_LEFT`/`NO_HOST` → 全员停会话 + 弹窗 + host 位空出 |
| 房间关闭 / 被踢 / 版本门 / 认证失败 | 终态，**不重连**，弹窗一次 |
| 换模式重建失败 | 纳入同一套自动重试（退避 + 窗口），不再是"可手动重新连接"就完了 |

## 7. 状态文案（同时修当前 BUG）

`core/room_direct.py::RoomDirectLink.direct_step()`：

| 条件 | 文案 | 计时 |
|---|---|---|
| 房间里只有自己（花名册无对端） | `等待对端用户加入…` | **不倒计时、也不正计时** |
| 有对端，但还没收到它的 `DIRECT_PUNCH` | `等待对端起跑公告（第 N 秒）` | 正计时，**有上限**（`PUNCH_SYNC_WAIT_TOTAL_S` / `PUNCH_SYNC_WAIT_S`） |
| 已收到公告，等约定时刻 | `等待同步起降：…`（`punch.await_punch_start` 报的倒计时） | **倒计时** |
| 正在喷端口 | `打洞中（同步起跳）` + 第 N 轮 | —— |

实现：`PHASE_WAITING_FOR_PEER` / `PHASE_WAITING_FOR_ANNOUNCE` 两个常量；
`_run()` 在"没有对端"那一支显式 `_set_phase(PHASE_WAITING_FOR_PEER)`。
三语条目已进 `gui/i18n_tables.py`。

## 8. 可观测（每次重连固定三行英文日志）

```
[reconnect] attempt N in Xs (reason=…)
[reconnect] attempt N rebuilding session role=host mode=stun
[reconnect] ok                      ← 成功后由 rebuild_done 收尾（回到「运行中」）
[reconnect] give up: terminal condition (…) -- no retry
[reconnect] waiting for the host to come online (reason=…) -- not retrying
[reconnect] the host went offline (HOST_LEFT); stopping this session and waiting, no retry
```

这些行同时进引擎事件快照（`log_event` / `_push`），所以导出日志里能看到完整
的一次重连。

## 9. 一致性自检（不许破）

* 一人断只断自己（`direct_multi_test` 钉住）；
* host 停止才全关（同上）；
* **停止不释放主机位**（`engine.stop_session` 的既有规则，本轮没碰）；
* 游客身份**会话级**：退房 / 被踢 / 房间被关不销毁（见 §10）；
* 终态不重试（`auto_reconnect_test` 钉住）。

## 10. 游客身份（用户 2026-09-27 口径，与自动重连的关系）

* 游客身份 = **登录会话级**，**不绑设备**；
* 中继首次分配游客名时随 `OK JOIN`/`OK CREATE` 回包给 `guest=<token>`
  （ADDITIVE）。客户端把它存进**内存**（`engine._guest_token` /
  `RoomSession.guest_token`），同一次登录会话里换房 / 重新进房时放回握手行的
  `token` 字段 → 名字不变；
* **退房 / 被踢 / 房间被关都不清**（`leave` / `kicked` / `room_closed` 的清理
  路径**不碰**它）；**只有** LOGOUT / 登录态丢失 / 换账号 / 重启才清
  （`gui/app.py::_forget_guest_identity`）；
* **绝不写进配置文件**（`on_start` 落盘时把游客名换成占位符）。

## 11. 本轮明确不做

* 自动继承 host；
* 允许"无 host 也能待着"（第 4 条那套热认领/热释放**保持冻结**）；
* 为提速改已验证的打洞策略 / 预算（`ISSUES` 问题 6 的 `⏸ 待评估`）；
* 第 4 条"释放主机身份"的行为改动（现行"先停止再释放"保持不变，只补提示）。

## 12. 测试

`tests/auto_reconnect_test.py`（已登记进 `tests/run_all.py`，真界面 + 假 Engine）：

* 退避序列 `0.5/1/2/4/8/15/15`；
* 60s 窗口用尽 → 停 + 弹窗 + `[reconnect] give up`；
* 终态表：`VERSION_MISMATCH` / `KICKED` / `ROOM REVOKED` /
  `AUTH_INVALID_TOKEN` / `authentication required` 全部不重试；
* 用户点停止后绝不重连；
* `HOST_LEFT` / `NO_HOST` 识别；
* 房主离线 → `stop_session` 被调、`running=False`、弹「房主已离线」、
  进「等待房主上线」且**没有任何重连尝试**；
* 无 host 时玩家身份点「开始连接」被拒并提示（没有 `start_session`）；
* 单人房间文案 `等待对端用户加入…` 且不含秒数；
* 有对端未公告 → `等待对端起跑公告（第 7 秒）`；已公告 → 倒计时原样。

`punch_traffic_test.py::test_player_stream_survives_a_link_teardown`、
`gui_room_host_session_test.py`、`room_identity_test.py` 覆盖其余的
一致性自检（一人断只断自己 / 角色不变 / 游客名字不变）。
