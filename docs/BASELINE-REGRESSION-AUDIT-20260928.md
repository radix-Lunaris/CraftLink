# CraftLink 中继侧：永久基准回归审计（2026-09-28）

> 触发：用户 2026-09-27 把《设计基准》写进 `AGENTS.md` 规则 6 并明确
> 「很久之前都是这样的了，但不知为啥你又改坏了，你先审计代码再看看自己
> 是不是给我改坏了」。
>
> 本文件逐条对照 `docs/DESIGN-BASELINE-20260927.md` / `AGENTS.md` 规则 6，
> 给出 `file:line`、判定（**本来就这样** / **我们改坏的**）、以及本轮的
> 修复与回归测试。判定依据是代码里原有的注释与提交时留下的说明（仓库没有
> git 历史，但被删掉的注释明确写了"这是后来为了……加的"）。

---

## ① Owner 不可转移 / 不可继承；Owner 离开 → 房间立即销毁

| 位置 | 审计结论 |
|---|---|
| `server/room_members.py:626`（旧 `add_member`：`if previous.is_owner: member.is_owner = True`） | **我们改坏的。** 原注释写着 "Ownership follows the person through a reconnect too: the room belongs to them, not to one TCP socket" —— 这是后来加的"同一个人重连保持所有权"便利，直接违反 §A「身份永久绑定房间、不可转移」。 |
| `server/room_members.py:700`（旧 `remove_member`：被顶替时 `return False`） | **我们改坏的（同一处）。** 创建者的旧连接被顶替后，它的清理路径返回 False → 房间不会销毁，所有权被留给了新连接。 |
| `server/relay_server.py:5298`（旧 `host_watchdog`：`room.close(reason="host heartbeat timeout")`） | **我们改坏的。** 开服控制连接一静默就把**整个房间**销毁，而 §A 说房间寿命绑在 Owner 的**成员连接**上。 |
| `server/relay_server.py:3355`（`handle_member_join` finally：`was_owner → room.close + retire_room`） | **本来就这样。** 正确，但被上面两条绕过。 |

**本轮修复**

* `room_members.py:640-655`：`add_member` 不再复制 `is_owner`；创建者的连接被
  顶替时置 `owner_superseded` 并**关掉旧 socket**，让它唯一的清理路径立刻跑。
* `room_members.py:700-706`：`remove_member()` 对"已被顶替的 owner 对象"返回
  `bool(member.is_owner)` → 房间走唯一的销毁路径。
* `relay_server.py:5270-5298`：`host_watchdog` 只 `room.host_writer.close()`，
  由 `handle_host` 的 finally 决定"销毁还是保留"（无 Owner 的老式房间照样销毁）。

**回归测试**

* `tests/relay_baseline_test.py::test_owner_never_transfers_unit`
* `tests/relay_baseline_test.py::test_owner_supersede_destroys_room`（真中继）
* `tests/room_settings_test.py`（既有：Owner 断开 → 房间销毁）
* `tests/v2_control_test.py` 第 23 项（既有：老式 zombie 房间被看门狗销毁）

---

## ② 一个房间最多一个 Host

| 位置 | 审计结论 |
|---|---|
| `server/room_members.py:615-621`（旧 `add_member`：只 `member.is_host = True`，不清其它） | **我们改坏的（半边）。** host 位置跟着同一个人重连是既有行为（`kick_test` 钉住），但只往新对象打标记、不清残留，会出现两个 `is_host`；`host_member` 取遍历到的第一个 → `HOST_STATE` 报错 `host_name`、player 打洞目标指向错的人（用户 P0 #1）。 |
| `server/room_members.py:423`（`take_host_from` 里已有"清其它 is_host"） | **本来就这样（补丁）。** 这段"防御性清理"的存在本身就说明不变量曾被破坏。 |
| `server/room_members.py:714`（`claim_host` 有 Host 时拒绝） | **本来就这样。** |
| `server/room_members.py:855`（`_direct_peer` 旧代码用 `member.is_host` 判"我是不是 host"） | **我们改坏的。** 被顶替的旧连接上 `is_host` 可能是过期值；一旦按过期值当成 host，就退回"最早进房的人" → 候选打给 Owner。 |

**本轮修复**

* `room_members.py:615-621`：搬运 `is_host` 时把其它任何 `is_host` 一起清掉。
* `room_members.py:748-764`：`release_host()` 同样做"位置空 = 没人持有"的收尾。
* `room_members.py:855`：`_direct_peer` 改成权威判定 `room.host_member is not member`。

**回归测试**

* `tests/relay_baseline_test.py::test_single_host_invariant_unit`
* `tests/relay_baseline_test.py::test_started_and_direct_target`（真中继：三人房
  player 的 `DIRECT_CAND` 只到当前 Host，不到 Owner）
* `tests/kick_test.py::test_superseded_connection_cannot_kick`（既有）

---

## ③ 房间参数创建后冻结（人数 + 连接模式）

| 位置 | 审计结论 |
|---|---|
| `server/relay_server.py:733-786`（`set_room_capacity` + `capacity_locked`） | **本来就这样。** 人数建房后锁死，`room_settings_test` 钉住。 |
| `server/room_members.py:1275`（旧 `CONNMODE` 权限：`is_host or is_owner`） | **我们改坏的。** 原注释写着这是为了让"建房者还没认领主机位时也能 seed 模式"而放开的；但顺手把**非 Owner 的 Host** 也放进来了，违反 §A「只有 Owner 能切模式；Host（非 Owner）无权」。 |

**本轮修复**

* `room_members.py:1275`：改成 `if room.owner is not member:`（权威 Owner）。
  建房者 house=0 也能 seed（既有测试需要），非 Owner 的 Host 一律
  `ERROR NOT_HOST`（保留这个码，不改已有错误码，保持 ADDITIVE 兼容）。

**回归测试**

* `tests/relay_baseline_test.py::test_connmode_owner_only_and_rebuild`
* `tests/direct_cand_test.py::test_conn_mode`（既有）
* `tests/room_settings_test.py`（既有：人数锁定）

---

## ④ Host 掉线 = 真正离开；房间保留（§B/§12）

| 位置 | 审计结论 |
|---|---|
| `room_members.py::reap_idle_members` + `handle_member_join` finally | **本来就这样（上一轮已实现）。** 成员心跳超时 → 释放主机位 + 唯一一行 `[HOST RELEASE]` + 断游戏连接 + 广播 + 房间保留。 |
| `relay_server.py:2189`（旧 `handle_host` finally：`drop_all_sessions`） | **漏了一半。** 只拆 `room.sessions`，`waiting_players` / `waiting_tunnels` 不动 → 还在等配对的玩家"没掉线"，两端表现不一致（用户 P0 #7）。 |
| `relay_server.py:2420-2437` / `2552-2569`（旧门禁只有 `NO_HOST`） | **缺门。** Host 认领了但没开服时，其他玩家照样进正式流程（违反 §B「不得进入正式流程」）。 |

**本轮修复**

* `relay_server.py:2189`：`handle_host` finally 改调
  `disconnect_game_connections`（覆盖 sessions + 等待池，带可识别 `HOST_LEFT`）。
* `relay_server.py:2412-2437` / `2552-2569`：两级门禁
  `host_claimed` → `host_alive`，分别回 `ERROR NO_HOST` / `ERROR HOST_NOT_READY`。

**回归测试**

* `tests/relay_baseline_test.py::test_host_member_timeout`（真中继快看门狗）
* `tests/relay_baseline_test.py::test_host_gate`
* `tests/host_release_identity_test.py`（既有，已更新 5a 的 NO_HOST 断言为
  HOST_NOT_READY，并新增真正的"没人认领 = NO_HOST"用例）

---

## ⑤ 每次打洞前重新确认目标：房间广播"当前 Host + 哪些成员已开始连接"

| 位置 | 审计结论 |
|---|---|
| `room_members.py::broadcast_member_state`（旧只有 `host=` / `host_name=`） | **缺字段。** `host_name=` 是显示名，可能重名；也没有"谁已开始连接"。 |

**本轮修复（全部 ADDITIVE）**

* `room_members.py:1765-1810`：新增 `started_client_ids()` / `started_token()` ——
  从**连接本身**现算（host 控制连接 → `host_id`；等待池 PLAYER →
  `waiter.client_id`；已配对会话 → `pair.player_id`），不引入新握手。
* `room_members.py::broadcast_member_state`：新增 `started=<cid>,<cid>` 与
  `host_id=<cid>`（没有时都是 `-`）。
* `relay_server.py:3238/3248`：`OK JOIN` 也带 `host_id=`。
* `relay_server.py:2056 / 2663 / 3282`：Host 开服、PLAYER 接入、房间状态变化
  都立刻 `broadcast_member_state`，`started=` 不会滞后。
* `relay_server.py:1262`：管理快照 `member_list` 新增 `started` 布尔。

**回归测试**

* `tests/relay_baseline_test.py::test_started_token_unit`
* `tests/relay_baseline_test.py::test_broadcast_carries_started`
* `tests/relay_baseline_test.py::test_started_and_direct_target`

---

## ⑥ 换模式必须严格重建全部通路

| 位置 | 审计结论 |
|---|---|
| `room_members.py:1318-1327`（旧 `CONNMODE` 只改字段 + 广播） | **缺清理。** 旧模式下的配对/隧道在模式变了以后仍是"已配对但模式已变"的脏会话（违反 §D「换模式必须严格重建全部通路」）。 |

**本轮修复**

* `room_members.py:1318-1327`：真正发生切换（值不同）时调用
  `disconnect_game_connections(..., code=CONN_MODE_CHANGED_CODE)`，清掉
  sessions + waiting players + waiting tunnels；先清理再广播，`started=` 不会
  留旧目标。

**回归测试**

* `tests/relay_baseline_test.py::test_connmode_owner_only_and_rebuild`
* `tests/direct_cand_test.py::test_conn_mode`（既有）

---

## 没改的（诚实清单）

* **打洞状态机 / 顺序 / Timer**（§D/§E/§F/§G 的客户端半边）：全部在
  `client/**`，本轮明确不动。
* **`host_watchdog` 是否应释放 Host 位置**：本轮只保证"不销毁带 Owner 的
  房间"。开服控制连接静默时主机位**保留**（UI 走既有的 `xxx（未开服）`），
  与"按停止保留位置"这条已验收规则一致；"Host 真正离开（成员心跳超时）"
  仍然释放位置。若用户要"开服连接静默也释放位置"，需单独确认（这条会改
  已验收的 UI 三态）。
* **玩家在等待池里 socket 断开但从未配对**：`started=` 会短暂偏大，直到
  下一次 `try_pair` 清掉死 waiter。属既有配对池行为，本轮未动。
