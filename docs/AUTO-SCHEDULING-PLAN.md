# AUTO 调度方案（V5.0 · Phase 1）

> 日期：2026-10-11。用户三轮拍板（见 §1），本文是 AUTO 的**唯一实现口径**。
> 冲突时以本文为准；有疑问先问，不要自己发明。
>
> 相关：`docs/AUTO-RECONNECT-PLAN.md`（自动重连，和本文的"回落"是两件事）、
> `docs/DESIGN-BASELINE-20260927.md`（§A/§B/§C 的身份与重建规则）、
> `docs/PROTOCOL.md`（§8.3/§8.9 的 `mode=` / `CONN_MODE_CHANGED`）、
> `docs/PUNCH-FLOW-TREE-20260928.md`（打洞现状）。
>
> 参考工程 `MCLanP2P` 的 AUTO（`client/net_transport.py` 的
> `AUTO_PUNCH_TIMEOUT_S` / `AUTO_DIRECT_ATTEMPTS` / `RELAY_UPGRADE_*`）是
> **策略来源**，不是逐行移植对象 —— 它的传输层结构和我们不同（见 §10）。

---

## 0. 一句话

**AUTO = 房间级"策略"（owner 选、中继广播），实际走哪条路由每条连接的
"有界等待 + 两端一致 + 失败即回落中继"决定。**

它不是第三条传输，也不是中途热切换。它是把已经存在的两条路（中继 / 直连）
接上一个调度器和一个**连接建立前**的选路判断。

---

## 1. 用户已确认的三条决策（2026-10-11）

| # | 问题 | 决策 |
|---|---|---|
| 1 | 第一版做到哪一步 | **Phase 1 先行：连接建立前选路，不做中途迁移。** 热升级 / 热回落留到 Phase 2。 |
| 2 | AUTO 的粒度 | **房间级模式**：Owner 选，中继广播 `mode=auto`，两端一致。 |
| 3 | 旧客户端混房 | **禁止旧客户端进入 AUTO 房间并提示升级**（不降级、不静默）。 |

---

## 2. 语义：策略 vs 事实

* **房间级策略**：`mode=auto` 表示"这个房间的选路策略是尽量直连、直连建不起来
  时用中继"。
* **连接级事实**：某一条 Minecraft 连接最终跑了中继还是直连，是**这一条的事实**，
  由两端各自执行同一条规则得出，**必须一致**（§5.4）。
* `mode` 仍然是**房间属性**，仍然**只由 Owner 的显式 `CONNMODE` 改变**。
* 实际路径**不写回任何房间属性**，面板也（Phase 1）看不到它。

---

## 3. 不许破的不变量

1. **回落绝不走 `CONNMODE`**。`room_members.py:2470-2476` 那条"房间模式只能由
   Owner 显式改变"的铁律一字不改。直连失败 → 本地改用中继，`mode=auto` 不变。
   否则就是 v3.9.19 那个 P0 的复活（`docs/HANDOVER.md:533`：
   "模式自己跳回 relay，直连玩一半被拆"）。
2. **STUN 的语义一个字不改**。"已打的通道和主流量不允许碰中继"是 **STUN 专属**；
   AUTO 是唯一允许中继当保底的模式。所有 `== "stun"` 的分支改成显式三态，
   绝不让 auto 顺带改变 relay/stun 的行为。
3. **权限不放宽**。只有 Owner 能选 auto（`room_members.py:1966` 不变）。
4. **一人断只断自己 / host 停止才全关**（`direct_multi_test` 钉住）。
5. **"模式未知 = 什么都不做"**（v3.9.19 修复）继续有效：广播里那一秒没有
   `mode=` 时，客户端保持上次已知模式，绝不退回 relay。
6. **版本/能力门是硬门**：旧客户端进不了 auto 房间（§6）。

---

## 4. 服务端行为规范

服务端**数据面一行不改**（配对、转发、`DIRECT_*` 透传本来就是模式无关的，
见 `relay_server.py:890`、`room_members.py:2220`）。只改校验、能力位、广播。

### 4.1 放开 `auto`（4 处 gate）

| 位置 | 现状 | 改成 |
|---|---|---|
| `server/room_members.py:1945` | `wanted not in ("relay","stun")` → `ERROR BAD_MODE` | 允许 `auto`；错误文案补上 auto |
| `server/room_members.py:2525` | `set_room_conn_mode` 的 gate | 允许 `auto` |
| `server/room_registry.py:139` | `remember_mode` 的 gate | 允许 `auto` |
| `server/relay_server.py:1563` | `get_or_create_room` 的恢复 gate | 允许 `auto` |

读取点 `room_members.py:2489 room_conn_mode()` **不用改**：它已经把任何非空
字符串原样返回并广播（`mode=auto` 会正常发出去）。

### 4.2 能力位 `caps`（决策 3 的落地）

握手行尾部新增**可选** token：`caps=auto`（逗号分隔，ADDITIVE）。

* 客户端：`CREATE/JOIN ... ver=<APP_VERSION> caps=auto`（`room.py:707-713`）。
* 服务端：在版本门的同一处 token 扫描里解析 `caps=`（`relay_server.py:4663`），
  存到 `RoomMember.caps`（新增字段，`room_members.py:301`）。
* **旧中继**：只按位置读 `parts[2..6]` 并扫 `ver=`，多出来的尾部 token 直接忽略
  → 不受影响。
* **旧客户端**：不发 `caps=` → `caps` 视为空集 → 没有 auto 能力。

拒绝规则（`handle_member_join`，`relay_server.py:3633`；在 `add_member()` 之前）：

1. `JOIN` 进一个 `mode=auto` 的房间、而该成员没有 `auto` 能力
   → `ERROR AUTO_UNSUPPORTED 该房间已启用 AUTO，请升级客户端后加入` 并关闭。
2. Owner `CONNMODE auto` 时，房里已有成员没有 `auto` 能力
   → `ERROR AUTO_UNSUPPORTED_MEMBER 房间内有客户端不支持 AUTO（<名字列表>），请其升级或离开`
   并拒绝切换（房间保持原模式）。
3. `CREATE` 出来的房间默认 relay，Owner 随后 seed auto —— 自己是新客户端、有
   能力位，规则 2 自然通过。

> 为什么需要它而不是只靠版本门：版本门是**精确字符串相等**
> （`relay_server.py:4670`），默认就已经拦住混版本；但面板有
> `allow_version_mismatch` 开发开关（`settings_service.py:38`），开关打开时会
> 放混版本进来。能力位是那道门关着时也成立的硬保证。

### 4.3 广播（不改协议）

`HOST_STATE ... mode=auto mode_reason=owner|restored|default` 已能承载。
客户端解析处要接受 `auto`（§5.5）。

### 4.4 可观测（Phase 1 可选，默认不做）

面板目前完全看不到路径。若要让面板看到 `relay|direct`，需要：中继侧记录
`DIRECT_HIT`、`Room.snapshot()`/status.json 加字段、`rooms`/`room_sessions`
加列 + `db_schema.MIGRATIONS`、PHP 面板改。**Phase 1 不做**，留给 Phase 2
和"瑕疵"批次一起评估。

---

## 5. 客户端行为规范

### 5.1 三态策略（新增）

在 `session.py` 里把 `config.mode` 映射成显式三态，供 cores 使用：

| `config.mode` | `direct_policy` | 语义 |
|---|---|---|
| `relay` | `off` | 中继承载一切（现状不变，`direct_path_provider = None`） |
| `stun` | `only` | 只走直连；等不到就**拒绝这条会话**（现状不变） |
| `auto` | `prefer` | **有界等待**直连；等到走直连，等不到**这条连接整条走中继** |

`direct_policy` 必须**随会话创建冻结**（和 `config.mode` 一样），热切换由
现成的重建路径（`_sync_session_mode` / `_rebuild_tunnels`）处理。

### 5.2 何时起打洞

和 STUN 相同：**点「开始连接」**起（不是进房、不是开游戏）。改动点：

* `app.py:8971` `stun_room = self._room_conn_mode() == MODE_STUN`
  → `mode in (MODE_STUN, MODE_AUTO)`；
* `app.py:9015` 的 `_punch_mode` 单写入点接受 `auto`；
* `direct_carry` 房间级打洞 / keepalive / 重打逻辑**一行不改**。

### 5.3 每条新连接的选路（Phase 1 核心）

```
一条新的 Minecraft 连接到达
        │
        ├─ 该 peer 的直连洞此刻"两端都确认可用"？ ──是──► 直连承载
        │
        └─否──► 有界等待 AUTO_DIRECT_WAIT_S
                     ├─ 等到 ──► 直连承载
                     └─ 超时 ──► 这条连接整条走中继
```

* 判据是 `RoomDirectLink.peer_ready(peer)` 这一级的"**两端确认**"，复用现成的
  `DIRECT_HIT stage=connected` + `_adopt_peer_connected`（`room_direct.py:1906`）。
  绝不"本端单边打通了就拿去用"。
* 实现落点：
  * `session.py:473 make_direct_path_provider()` 的 gate 从 `!= "stun"` 改为三态；
    `stun` 保持无限等待；`auto` 用 `AUTO_DIRECT_WAIT_S` 为上限，超时返回 `None`。
  * `session.py:533 make_direct_hook()` 同样三态（auto 不装旧 hook，因为房间级
    打洞在时它本来就让路，见 `session.py:552`）。
  * `host_core.py:1185 relay_mc` / `player_core.py:721 raw_relay`：`_try_direct_carry`
    返回 False 时，**`direct_policy == "prefer"` 就继续往下走中继泵**；
    `"only"` 维持现状（拒绝并说明原因）。
  * `_try_direct_carry` 的直连握手本身有超时（`direct_carry.CONNECT_TIMEOUT_S`），
    所以"对端其实在走中继"时这端不会挂死，会超时回落。

### 5.4 两端一致（为什么不会一边直连一边中继）

选路规则是**共享打洞状态的纯函数**（`peer_ready`）+ **共享截止时刻**：

* 截止时刻锚定在**双方都知道的打洞起跑时刻**（`DIRECT_PUNCH` 同步握手 /
  中继广播的 `started=`），不是"各自游戏连接到达的时刻" —— 否则两端会各算各的。
* `peer_ready` 本身是两端会收敛的事实（`_adopt_peer_connected` 兜底）。
* **自愈**：万一仍出现分歧（截止瞬间的竞态），走直连那端的
  `ensure_started()` 会因为对端没接而超时，`prefer` 策略随即**回落中继** ——
  两个方向都收敛到中继，不会出现"一半直连一半中继"的坏会话。

### 5.5 洞死（Phase 1 的已知代价）

Phase 1 **不做中途迁移**。一条正在直连承载的连接，如果洞被判死
（`room_direct.LINK_DEAD_S = 20s`）：

* 这条连接会断（直连泵结束）；
* 现成的**自动重连 + 重建通路**接管（`AUTO-RECONNECT-PLAN.md`）；
* 重连后 `peer_ready` 已是假 → 新连接走中继，游戏恢复。

代价：用户会看到一次"掉线重连"（数秒）。**这是 Phase 1 明确接受的取舍**，
由 Phase 2 的热回落到无感。

### 5.6 打洞一直失败

* 房间级打洞照旧在跑（forever retry，`RETRY_INTERVAL_S=3s`），只是**新连接不再
  等它**（超时就中继）。
* `peer_ready` 一直是假 → 所有连接走中继 → 用户始终能玩。
* 一旦后台某轮打通、两端确认，**之后的新连接**自动走直连（无需改模式、无需重连
  整个房间）。

---

## 6. 旧客户端兼容（决策 3）

* 中继：`JOIN` 到 auto 房间的旧客户端 → `ERROR AUTO_UNSUPPORTED ...` 拒绝 +
  提示升级（§4.2）。
* 中继：Owner 想把 relay 房间切 auto，但房里有旧客户端 → 拒绝切换并点名（§4.2）。
* 客户端：收到 `ERROR AUTO_UNSUPPORTED*` 时按可读原因弹窗/日志，**不重连**、
  不静默吞。
* 版本门：客户端与中继**必须同批发布**（精确匹配，`relay_server.py:4670`）。
  `deploy/` 当前是 v3.9.20 的旧镜像，发布前必须用 `rebuild.sh` 重新生成
  （`deploy/server/VERSION` 与 `server/VERSION` 已不一致）。

---

## 7. 客户端改动清单

### 7.1 界面 / i18n

| 位置 | 改动 |
|---|---|
| `app.py:180` | `MODE_AUTO` 已存在，不动 |
| `app.py:4437` | AUTO 磁贴 `locked=True` → `False`；提示文案改成"优先直连；直连建不起来时自动改用中转" |
| `app.py:4417,4432` | 注释里"v6/AUTO 未开放"的说法更新 |
| `app.py:8541` `_on_mode_change` | 接受集合加 `MODE_AUTO`（去掉弹回 relay） |
| `app.py:8578` `mode_hint` | 新增 auto 分支文案 |
| `app.py:8928` `_room_conn_mode` | 接受 `MODE_AUTO` |
| `app.py:9119` `_maybe_rebuild_for_mode_change` | room_mode 白名单加 `MODE_AUTO` |
| `app.py:9052` `_sync_session_mode` | 依赖 `session_mode()`，随 §5.1 自动生效 |
| `i18n_tables.py` | AUTO 提示、`AUTO_UNSUPPORTED*` 三语条目 |

### 7.2 配置 / 房间层

| 位置 | 改动 |
|---|---|
| `config/settings.py:276` | `mode` 注释与取值说明加 `auto`；若 `validated()` 有白名单则加 |
| `core/room.py:832` `create()` | seed 白名单加 `"auto"` |
| `core/room.py:1836` | 解析 `mode=` 时接受 `auto` |
| `core/room.py:1200` `set_conn_mode` | 原样透传即可；错误码 `AUTO_UNSUPPORTED*` 要能被读出 |

### 7.3 引擎 / 数据面

| 位置 | 改动 |
|---|---|
| `core/session.py:473` `make_direct_path_provider` | 三态；auto 有界等待 |
| `core/session.py:533` `make_direct_hook` | 三态 |
| `core/session.py:701` 注释 | 更新"那是 AUTO 的语义，AUTO 是 v6" |
| `core/session.py:1279,1456` | 把 `direct_policy` 传给 cores |
| `core/host_core.py:1185` | `prefer` 失败后继续走中继泵 |
| `core/player_core.py:721` | 同上 |
| `core/host_core.py:78`/`player_core.py:50` | 新增 `direct_policy` 形参（默认 `"only"` 以兼容现有调用） |
| `core/room_direct.py` | auto 与 stun **共用**打洞；仅新增"截止时刻"信息供 §5.3 使用，打洞算法不改 |
| `core/direct_carry.py` | **不改**（Phase 1 无迁移；`relay_reader` 排空口留到 Phase 2） |

### 7.4 常量

```python
#: AUTO：一条新连接最多等直连多久，然后整条改走中继。
#: 取小值是因为房间级打洞在「点开始连接」时就跑了，进游戏时洞通常早就好了。
AUTO_DIRECT_WAIT_S = 4.0        # 待实测标定（3~5s）
```

* 复用现有：`room_direct.RETRY_INTERVAL_S=3.0`、`PUNCH_BUDGET_S=6.0`、
  `LINK_DEAD_S=20.0`、`SYNC_START_WAIT_S=12.0`、`CONNECT_TIMEOUT_S`。
* Phase 2 才会引入参考工程的 `RELAY_UPGRADE_EVERY_S` / `RELAY_UPGRADE_BUDGET_S`。

---

## 8. 测试计划

### 8.1 必须改的现有断言（当前都在断言"auto 被拒/上锁"）

| 测试 | 现状 | 改成 |
|---|---|---|
| `tests/direct_cand_test.py:477` | `CONNMODE auto` → `ERROR BAD_MODE` | 有能力的客户端 → `OK CONNMODE mode=auto` |
| `tests/gui_appearance_test.py:141,258,274,353` | AUTO 上锁 + 弹回 relay | AUTO 解锁 + 保持选中 |
| `tests/conn_mode_hotswitch_test.py:545,732` | 模式白名单 | 加 auto 路径 |
| `tests/relay_baseline_test.py:942` | 模式白名单 | 同上 |

### 8.2 新增（`tests/auto_schedule_test.py` 起步）

* AUTO 房间能建成、两端都收到 `mode=auto`；
* 洞已就绪 → 新连接走直连；洞未就绪 → 等待后走中继（假 Engine/假时钟）；
* 等待超时 → 该连接走中继，且**没有**任何 `CONNMODE` 被发出（回落不碰模式）；
* STUN 行为不变（等不到仍然拒绝，绝不落中继）；
* relay 行为不变（`direct_path_provider is None`，逐字节如旧）；
* 旧客户端（无 caps）`JOIN` auto 房间 → `ERROR AUTO_UNSUPPORTED`；
* Owner 在有旧成员的房间 `CONNMODE auto` → `ERROR AUTO_UNSUPPORTED_MEMBER`；
* 两端 `peer_ready` 分歧时，直连侧超时回落中继（自愈）；
* `mode=` 抖动一秒时，`_punch_mode` 保持不变（v3.9.19 不变量）。

### 8.3 回归网（必须全绿）

`direct_e2e / direct_reuse / direct_multi / direct_persist / direct_hardening /
conn_mode_hotswitch / mode_switch_rebuild / punch_traffic / punch_flow /
relay_baseline / auto_reconnect`，以及 `tests/run_all.py` 全量。

---

## 9. 发布注意

1. **客户端 + 中继必须同批**（版本精确匹配）。先发中继、再发客户端，中间窗口
   老客户端会 `VERSION_MISMATCH`（除非面板开了开发开关）—— 维护窗口内可接受。
2. `deploy/` 树当前停留在 v3.9.20，**发布前必须 `rebuild.sh` 重新生成**，否则
   上传的是旧代码。
3. 版本号：V5.0 大版本，客户端与 `server/VERSION` 同步。
4. 新增文档登记进 `docs/HANDOVER.md` 的文档索引。

---

## 10. Phase 2 预告（本次不做）

参考工程的中途热切换（`net_transport.py:1621-1740`）思路可用，但我们的传输层
和它不同，不能照抄：

* 它每 peer 一条 `PeerChannel`，`send_fn` 可原地换；中继走信令 websocket，
  数据是**分帧**的。
* 我们的中继是**配对出来的 TCP 隧道**（纯字节流），直连是 `udptunnel` 的
  **自带序号/ACK/重排的可靠有序流**；两条独立序号空间之间**没有对应关系**，
  原地换发送端会产生跨传输重排。

所以 Phase 2 的正确做法是**带字节偏移握手的协调切换**（或先只做
"直连 → 中继"单向、用 `udptunnel` 的 ACK 状态定位续传点），并单独出设计与
专项测试。`direct_carry.pump(..., relay_reader)` 与 `_drain_relay`
（`direct_carry.py:869,1045`）就是为它预留的接缝。
