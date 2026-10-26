"""
CraftLink -- 进房就打洞（照搬 MCLanP2P 的流程）。

============================================================================
 为什么需要这个文件（这一版最大的设计错误）
============================================================================

我第一版把打洞挂在「中继隧道进了 READY」之后，也就是**游戏已经连上之后**。
那是错的，而且错得很隐蔽：算法是对的，流程是错的。

MCLanP2P 的流程（`server/server.py::maybe_coordinate`，在两个地方被调用：
有人进房时、有人发布候选时）：

    进房 → 发布候选地址 → 中继把两端配起来 → 立刻打洞
    （打通之后，游戏才挂到这个已经建立的通道上）

我的流程：

    进房 → 交换候选 → ……等游戏连上、隧道进 READY …… → 才打洞

后果在项目负责人的日志里看得一清二楚：**两边的日志里一条 `[direct]` 都没有**，
HOST 的隧道全程 `idle`。没有游戏，就永远走不到打洞那一步；而用户要的恰恰是
"先把洞打通，游戏再挂上来"。

顺带还暴露了两个问题，都在这里一起修掉：

  * 连本机 Minecraft 失败时（`mc_writer` 未赋值 + `finally` 清理）抛
    `NameError`，把真实原因吃掉了 —— 用户看到的是安静重试，不是
    "世界没开"。那个在 `host_core.py` 里修了，修完立刻能报出真实错误。
  * `reliable.py`（可以换路径的可靠流）写好了却**从来没接进核心**，
    于是"直连"只是界面上的一个状态字。

============================================================================
 这个文件做什么
============================================================================

**房间级**的打洞协调器，和 `RoomNatMonitor` 并列：它不依赖任何会话、不依赖
游戏、不依赖隧道。只要房间里是「STUN 模式 + 一个房主 + 一个玩家」，就立刻
交换候选地址并打洞；打通之后**保住那个 socket 和 peer 地址**等着会话来取。

**保住 socket 是重点**：NAT 映射属于本地端口（`stun.py` 的"一个 socket"规则），
关掉再开一个新 socket 就是一个新映射，洞就没了。所以打通之后不重建、不重绑。

中继一个字节都不改：候选交换走的是**已有的**成员控制面消息转发
（`DIRECT_CAND` / `DIRECT_PUNCH`，中继只转发不理解），所以这里全是客户端逻辑。
"""

import asyncio
import contextlib
import time

from . import direct_carry
from . import protocol as proto
from . import punch_flow
from .direct_carry import CONNECT_TIMEOUT_S, PeerLink
from .punch_flow import ConnSession, Stage, SyncMismatch

#: There is NO expiry window, and that is a deliberate correction.
#:
#: My first version threw a punched link away after 120 seconds on the theory
#: that an idle mapping gets reclaimed. That is backwards: the mapping is
#: reclaimed BECAUSE it was left idle, and the cure is the keepalive his
#: `udptunnel.py` has always had ("keepalive every few seconds, otherwise the
#: mapping expires and the hole closes") -- not a timer that discards a working
#: hole.
#:
#: It also broke the stated requirement. This tool is not tied to any game
#: process; the user joins a room and may open a world (or an entirely different
#: game) a minute or an hour later. A link that survives that gap is the whole
#: point, so `ready` now means "punched AND being kept alive", with no clock.
#:
#: Kept as a name for the log/tests, and deliberately not used as a deadline.
DIRECT_KEEPALIVE_S = 5.0

#: 两次打洞之间的间隔（他的"每 3 秒重试直到连上"）。
RETRY_INTERVAL_S = 3.0

#: 单次打洞的预算。短一点：这发生在用户刚点「开始连接」、还没开始玩的时候，
#: 而且失败也不影响任何东西（STUN 模式打不通就会话拒绝，中继不搬游戏数据）。
PUNCH_BUDGET_S = 6.0

#: 一条直连隧道多少秒没有任何数据就算死（他的 `LINK_DEAD_S`）。
#:
#: 一个 UDP 洞不是一条连接：NAT 映射过期、对端重启，都不会产生任何"失败"，
#: 对端只是不再出声。没有这个判死，界面会一直显示直连，而游戏字节在往一条
#: 死隧道里灌。他那边是 20 秒；保活每 5 秒一次，所以 20 秒没消息 = 至少错过
#: 4 个保活 = 洞真的没了。
LINK_DEAD_S = 20.0

#: 洞打通之后，隧道必须在这么久之内起来，否则这个洞判死、让下一轮重打。
#:
#: ============================================================================
#:  ★ 为什么需要这条截止时间（用户 2026-09-28 批准的隐患 5）
#: ============================================================================
#: `DirectLink` 的保活线程只证明**映射**还在（NAT 洞没被回收），不证明
#: **隧道**能用。隧道握手一直没成时（`carrier is None` / `not carrier.started`），
#: 旧代码直接 `continue`，于是**永远不会判死** —— 只要保活线程活着，这条洞就
#: 再也不会被重打，玩家永远连不上（"打通又断"的一种）。这里给它一个真正的
#: 截止时间：到点就 `_drop_peer()`，下一轮重打（§F 的作废 → 重新同步 → 再打）。
#:
#: 取值必须 **大于** `_open_tunnel()` 的握手预算
#: （`TUNNEL_HANDSHAKE_ATTEMPTS * CONNECT_TIMEOUT_S + 退避`），否则会把一条
#: 还在合法握手中的隧道误杀（3 * 8s + 2 * 3s ≈ 30s < 45s）。
TUNNEL_SETUP_DEADLINE_S = 45.0

#: `_open_tunnel()` 在**同一个 socket** 上最多握手几次（有界重试）。
#:
#: 重打的代价是"新 socket = 新 NAT 映射"：会把一条还活着的洞扔掉。所以握手
#: 失败时先在**同一个洞**上重试，只有 socket 真的没了才允许重打。
TUNNEL_HANDSHAKE_ATTEMPTS = 3

#: `_open_tunnel()` 两次握手之间的退避（秒）。
TUNNEL_RETRY_BACKOFF_S = 3.0

#: 「等待同步起跑」等对端测量结果的上限（§E）。
#:
#: 先测完的一方必须等对端也测完测量 —— 但"等"不能是无限等，否则对端掉线时
#: 这一条连接就永远卡在同步阶段。超时后**不继续往下走**（那正是端口错位的原因），
#: 而是把这一轮标记为失败、下一轮从头再来（§F 的作废/重同步）。
SYNC_START_WAIT_S = 12.0

#: 「这个对端还在连接」的信标新鲜度窗口（秒）。
#:
#: `_heard_started` 只增不减，所以 Host 需要一个"他现在还在连吗"的判据 ——
#: 见 `_beacon_fresh()`：窗口内还收到过他的 `DIRECT_HIT / DIRECT_CAND /
#: DIRECT_PUNCH` 就算还在连。
#:
#: 取 15s 的理由：一段等待里最长的静默是 `SYNC_START_WAIT_S`（12s，期间对端
#: 每 1s 重播一次，所以实际远小于 12s）；15s 给中继转发抖动留了余量，又足够短，
#: 让"停止连接"的玩家在被判 idle 之后不久就退出目标表（用户第 1 条）。
HEARD_FRESH_S = 15.0

#: 两次"我测完了"信标之间的重播间隔。
#:
#: 用户报的 BUG 3/6（断线重连打不通/很慢、发包频率不一致）的共因是"两端的
#: 窗口几乎不重叠"。重播让晚进来的那一边也能拿到对端的起跑线 —— 和
#: `PUNCH_REANNOUNCE_S` 对 `startIn` 做的事完全一样。
BEACON_REANNOUNCE_S = 1.5


class RoomDirectLink:
    """
    一个房间的直连协调器：点「开始连接」就打洞，打通立刻建长连接。

    生命周期跟着【会话】走（3.0.3 起）：点开始连接启动、点停止/退房/切回
    relay 销毁。和 `RoomNatMonitor` 一样由引擎持有，跑在引擎的事件循环上。

    和游戏的关系只有一句：**没有关系**。洞、隧道、保活都先于任何 Minecraft
    连接存在；游戏数据要过的时候只是把字节挂到已经建好的隧道上（按 sid 复用）。
    """

    # ================================================================
    #  阶段文案（`direct_step()` 读；界面「打洞进度」那一行）
    # ================================================================
    #
    # 2026-09-27 用户报的 BUG：单人房间里显示"等待同步起降"并且**一直正计时**。
    # 所以"没对端"必须是一个**独立**的阶段 —— 它不该带秒数（用户明确要求
    # 显示「等待对端用户加入…」且不倒计时）。
    PHASE_WAITING_FOR_PEER = "等待对端用户加入…"

    #: §B / 用户 2026-09-28 第 5 条：**房间里有这个人、但他还没点开始连接**。
    #:
    #: 用户原话：「玩家在房内但未连接时，host 端应显示「等待对端连接…」，现在
    #: 错误显示「等待同步起跑」」。判据必须是"花名册里有他"与"他已经开始连接/
    #: 已经进入测量"分开：
    #:
    #:   * 房里**一个人都没有**          -> `等待对端用户加入…`（上面那条）
    #:   * 房里有人、但没人开始连接      -> 这一条（`等待对端连接…`）
    #:   * 有人开始连接（进了目标表）    -> 正常走 §E 的逐目标阶段
    #:
    #: 它**静止、不带秒数**（和 `等待对端用户加入…` 同一条规矩）：这不是一个"有
    #: 上限的等待"，倒计时只会让人以为卡住了。
    PHASE_WAITING_FOR_PEER_CONNECT = "等待对端连接…"

    #: 有对端、但还没收到它的 `DIRECT_PUNCH`。这一段**有上限**（见
    #: `PUNCH_SYNC_WAIT_TOTAL_S` / `PUNCH_SYNC_WAIT_S`），所以给"第 N 秒"。
    PHASE_WAITING_FOR_ANNOUNCE = "等待对端起跑公告"

    #: §B / §E：房间**没有 Host**（没人认领）。基线原文的提示是
    #: 「当前房间暂无 Host，请等待 Host 认领」；这里只是**主行**的短文案，
    #: 完整提示由界面用它自己的弹窗/提示条出。
    PHASE_NO_HOST = "当前房间暂无 Host"

    #: §E 第一阶段：还在确认"谁是当前 Host"（`host_id=` 还没到 / 花名册还没刷新）。
    #: **不动手打洞** —— 猜错目标比晚一拍贵得多（BUG 1）。
    PHASE_CONFIRM_HOST = "确认 Host 中"

    #: §B：有 Host、但 Host **还没开始连接**（中继 `started=` 里没有它）。
    #: 基线原文的提示是「Host 未连接，请等待 Host 开服」。
    PHASE_HOST_NOT_READY = "Host 未连接，请等待 Host 开服"

    def __init__(self, engine, log=None):
        self.engine = engine
        self.log = log or (lambda *_a: None)

        #: 每个对端一条洞：peer_id -> {"link","addr","ready_at"}。
        #:
        #: ====================================================================
        #:   WHY THIS IS A DICT AND NOT ONE SLOT (the third player's bug)
        #: ====================================================================
        #:
        #: With one slot, the host punched to whoever happened to be the first
        #: other member and then treated the job as DONE (`_established`).
        #: A second guest's DIRECT_CAND was answered by the relay (it always
        #: forwards to "the host"), but the host had already stopped punching and
        #: had no slot to put a second link in -- so the third person sat at
        #: "正在打洞" forever with nobody on the other end.
        #:
        #: A star topology has ONE link per guest, so the state is per guest. The
        #: same `link`/`peer_addr` names are kept as properties so the status card
        #: and the single-peer tests keep reading what they always did.
        self.links = {}

        self.attempts = 0
        self.last_reason = ""

        #: 每个 peer 各自的打洞次数 —— 「一人一行」的状态卡要显示"第 N 次"，
        #: 而 attempts 是**整轮**的汇总（一个 peer 成功就归零，会把别人的
        #: 次数一起抹掉）。成功一个清一个，别的 peer 不受影响。
        self._peer_attempts = {}

        #: The inbound verdict from the punch round (`probe_inbound`), and the
        #: human note that goes with it. Kept because it is the only
        #: measurement that separates "the peer cannot reach us" (a network
        #: fact) from "our punching is wrong" (a bug) -- and because leaving
        #: the row at "未测" while the punch is clearly running is exactly the
        #: contradictory screen the owner complained about.
        self.inbound_state = ""
        self.inbound_note = ""
        self._task = None
        self._stop = asyncio.Event()

        #: 后台的"打通后立刻建隧道"任务（见 `_open_tunnel`）。close 时一起取消。
        self._tasks = set()

        #: 本机 Minecraft 的地址（房主侧）。`set_backend` 写入，之后新建的每条
        #: 链路都自动带上。
        self._backend = None

        #: Kept only so older callers/tests that mention claiming still find a
        #: name. Nothing reads it any more: links are SHARED now (one tunnel,
        #: one `sid` per Minecraft connection -- see `take_path`), so there is
        #: no "this peer is taken" state to track.
        self._claimed = set()

        #: peer -> `direct_carry.PeerLink`: the LIVE tunnel over that peer's
        #: punched socket (his `UdpTunnel` + `PeerChannel`).
        #:
        #: Built lazily by `take_path` and kept for the life of the room, NOT
        #: per session. A Minecraft client opens several connections -- a
        #: server-list probe, then the real join -- and they overlap; one tunnel
        #: per peer with one `sid` per connection is what makes both of them
        #: direct (and what keeps the peer's `UdpHub` from seeing two tunnels
        #: with one IP and dropping everything as "ambiguous").
        self._carriers = {}

        #: 每条 peer 打洞用的 DirectLink（跨重试复用：同一个 socket / 同一份测量）。
        self._punch_links = {}

        #: §F：**每个 Host↔Player 一条独立的 ConnSession**。
        #:
        #: 基线原文："独立拥有：session_id / attempt_id / candidate / NAT 状态 /
        #: 入站策略 / 同步状态 / punch 状态 / punch 次数 / batch / packet
        #: sequence / 当前端口 / punch interval / timer / connected / latency。
        #: 不得把一条目标的状态复用到另一条。"
        #:
        #: 所以它是 per-peer 的字典，**不是**上面那些整轮的计数器。那些老计数器
        #: 保留只是为了兼容老界面/老测试读快照；新逻辑一律读这里。
        self.sessions = {}

        #: 最近一次目标选择的原因（`punch_flow.select_targets` 的第二个返回值）。
        #: 界面用它区分「没有 Host」和「Host 没开服」两种提示（基线 §B）。
        self.target_reason = ""

        #: **别人主动告诉我们的"我已经开始连接了"**（peer client_id 集合）。
        #:
        #: ============================================================================
        #:  ★ 为什么 Host 需要这份证据（BUG 4 的另一半，2026-09-28）
        #: ============================================================================
        #:
        #: 中继的 `started=` 是按 **PLAYER / HOST 连接**算出来的。可这套客户端的
        #: 设计是"**先把洞打通、游戏再挂上来**"（本文件头部）：玩家点了
        #: 「开始连接」之后、Minecraft 连上来之前**没有 PLAYER 连接**，
        #: 中继自然看不到他 —— `started=` 里没有他。
        #:
        #: 于是 Host 侧 `started ∩ peers = ∅`，按 §D 一个目标都拉不到，洞永远
        #: 打不起来（实测：player 每轮 12 秒"等待同步起跑"超时，host 全程
        #: `attempts=0`）。
        #:
        #: 而 Host 手上有一份**更硬**的证据：对端主动发来的打洞协调行。正在发
        #: `DIRECT_HIT stage=measured ... from=<cid>` 的 peer，就是已经点了开始
        #: 连接的 peer。所以它和 `started=` 取**并集**：
        #:   * `started=` 说"谁开始了" -> 信；
        #:   * 对端自己发来的信标 -> 更信；
        #:   * 两条都没有 -> **不打**（BUG 4 要的正是这一条）。
        self._heard_started = set()

        #: 每个 peer **最后一次**发来打洞协调行的时刻（monotonic）。
        #:
        #: ============================================================================
        #:  ★ 用户 2026-09-29 第 1 条：`_heard_started` 是"只增不减"的，所以
        #:    "曾经开始过"和"现在正在连接"必须分开
        #: ============================================================================
        #: 一个玩家停止连接（中继广播 `conn=idle`）之后，`_heard_started` 里还留着
        #: 他 —— Host 于是照样为他走 §E，走到 `SYNC_START` 后永远等不到回应，
        #: 那一行就长期显示「等待同步起跑」。
        #:
        #: 判据不能只看 `conn=idle`：那会误伤"正在打洞、但还没开游戏连接"的正常
        #: 玩家（stun 模式下打洞**先于**游戏连接，此刻 `conn` 就是 idle）。所以再
        #: 加一条**新鲜度** —— 他还在发信标 = 他还在连；信标停了 = 他停了。
        self._heard_at = {}

        #: 每个 peer 在"非破坏性 peek"里**见过多少条**协调行。
        #:
        #: `peek_direct()` 取出来又放回去，所以同一条旧信标会被反复读到；
        #: 只有这个计数**增长**（= 真的来了新行）才刷新 `_heard_at`，否则一个
        #: 早就不在连接的 peer 会靠旧信标永远保持"新鲜"（用户第三轮第 6 条：
        #: 被踢的人一回来就被无限打洞）。
        self._heard_seen = {}

        #: 每个对端**自己广播**的公网端点（`"ip:port"`，来自 `DIRECT_ADDR`，
        #: ADDITIVE）。用户 2026-09-30：直连模式下每个对端都要把自己的地址和
        #: 端口广播给房间里所有人，成员列表才看得到（以前玩家之间互不可见，
        #: 那一列是 `-`）。
        #:
        #: **只用于显示**：不进打洞状态机、不改目标/候选、不参与任何决策。
        self.peer_addrs = {}

        #: 我们**自己**最后广播出去的端点（去重：别每轮都刷一行）。
        self.my_addr_sent = ""

        #: ★ 用户 2026-10-01：**全锥（eif）证据**（只读结论，纯展示）。
        #:
        #: 判据：**对端的包在我们本会话往它发过任何包之前就到了** —— 我们的 NAT
        #: 放行了一个"从未联系过的端点" = 全锥。桶交给 `punch.set_evidence()`，
        #: 由 `PunchArray` 在收发时各记一笔（**只观察，不改任何行为**）。
        #: 一旦成立就冻结（后面把端点了也不会翻案），因为它证明的是那一刻的事实。
        self.eif_proof = False
        self._evidence = None

        #: 后台的入站探测任务（审计问题 7：它不再挡在候选公布前面）。
        #: `close()` 会取消它；同一时刻只允许一个。
        self._ingress_task = None

        #: 打洞的**实时阶段文案**（审计问题 7：用户盯着十几秒的静止界面）。
        #: 由 `_set_phase()` 写、`direct_step()` 读，界面走同一个 key
        #: （`direct_step`），所以主行「打洞进度」会跟着变：
        #:   测量 NAT 类型中 -> 测量入站策略中 -> 等待同步起降 -> 打洞中（第 N 轮）
        self._punch_phase = ""
        self._phase_entered = 0.0

        #: Set the instant a punch succeeds, and NEVER cleared while the room
        #: lives. This is the fix for the punch storm.
        #:
        #: The owner's log showed 41 punches and 13 "ROOM LINK UP" in one
        #: session, with the player being disconnected by the relay four times.
        #: A punch that keeps restarting is not persistence, it is a loop that
        #: destroys its own result: every new attempt opens a NEW socket, which
        #: is a NEW NAT mapping, so the hole the game was about to use is gone
        #: before it can be used.
        #:
        #: His design is the opposite and states it plainly ("make room for a
        #: new link WITHOUT breaking the old one ... only once the new link is
        #: actually carrying traffic do we drop it"). A link is established
        #: ONCE. Replacing it is a deliberate act, never a side effect of a
        #: timer.
        self._established = False

        #: 当前这一轮 `_run()` 的记号（见 `start()` / `_run()` 的说明）。
        #: 只有 `start()` 会换一个新的；`stop()` 刻意不动它 —— 于是被
        #: `stop()` 之后还在收尾的旧循环会带着旧 token 抬头、自行退出。
        self._run_token = None

        #: 我们**见过它在中继花名册（`peers=`）里**的 peer 集合。
        #:
        #: 隐患 3 的花名册判死只对它生效：链接的 key 可能是**本地别名**
        #: （老中继/拿不到 `host_id=` 时用 `host_name`，更老时用 `guest-N`），
        #: 别名永远不在 `peer_ids` 里 —— 拿花名册去判"它走了"会把一条活着的洞
        #: 当场拆掉。真实成员的 id 至少有一轮出现在 `peers=` 里（不然它不会成为
        #: 目标），所以"曾经见过"就是"这是真 id、可以用花名册判死"。
        self._member_ids = set()

    # ---- 对外状态 ----

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    @property
    def ready(self):
        """
        现在能不能把这条直连交给会话用？

        "打通了"就够 —— 前提是 keepalive 一直在跑（见 DirectLink.start_keepalive）。

        这里曾经有一个 120 秒的过期窗口，理由是"闲置的映射会被回收"。方向是
        反的：映射正因为**闲置**才被回收，解法是让他那套 keepalive 一直发，
        而不是到点把一条好端端的洞扔掉。实测后果就是项目负责人遇到的那句
        「已打通但通道已经过期」—— 他进房打完洞，过了一会儿才开世界，
        客户端就把直连扔了、把游戏丢回中继。
        """
        if not self.links:
            return False

        # Being kept warm IS the readiness condition; a stopped keepalive means
        # the mapping is on its way out, so do not hand it to a game.
        #
        # Read it through the LINK (the object that owns the task), not through
        # this wrapper: `getattr(self, "keepalive_task", None)` on the wrapper
        # silently returned None, so a perfectly good link reported itself as
        # not ready -- which is how this test caught it.
        #
        # ★ 判据和 `peer_ready()` 共用 `_carried_alive()`：**已经被隧道接手的
        # 洞**当然算活着（那条隧道正在搬字节）。
        return any(
            self._link_alive(entry.get("link"))
            or self._carried_alive(peer)
            for peer, entry in self.links.items()
        )

    def _carried_alive(self, peer):
        """
        这个 peer 的洞**已经被一条起着的隧道接手**了吗？

        ============================================================================
          ★ 用户 2026-09-29 第 2 条：打通了、隧道也 ready 了，界面 3 秒后又
            「断了 / 等待同步起跑」，可是游戏其实进得去
        ============================================================================

        `DirectLink.keepalive_running` 只描述**打洞层**的账：
          * 房间保活线程还活着；或者
          * `carried=True`（隧道接手了）。

        隧道接手的那一刻 `PeerLink.ensure_started()` 先置 `carried=True` 再
        `stop_keepalive()`（`direct_carry.py`），**交接窗口**里 `carried` 可能被
        回落成 False（握手失败重试 / 会话结束），而 `start_keepalive()` 在
        `link.sock is None` 时是**静默返回**的（打洞命中在阵列 socket 上时就是
        这种情况）。于是打洞层的账变成"没保活"，`peer_ready()` 判 False，
        `ConnSession.cycle_action()` 拿到 `link_alive=False` -> `CYCLE_RESTART`
        -> 日志里 `new connection cycle ... (generation N -> N+1)`，界面退回
        「等待同步起跑」—— 而那条 UDP 隧道还好好地在搬数据。

        判据必须问**真正在搬字节的那一层**：`carrier.started and not stop`。
        """
        carrier = self._carriers.get(peer)

        if carrier is None:
            return False

        try:
            if carrier.stop.is_set():
                return False
        except Exception:
            return False

        return bool(getattr(carrier, "started", False))

    @staticmethod
    def _link_alive(link):
        """
        这条打洞链路**现在**还能用吗？

        ============================================================================
          ★ "引用还在"不等于"洞还活着"（用户 2026-09-28 批准的隐患 2）
        ============================================================================
        `links` 里的条目可能在洞死掉之后仍然留着：停止打洞**刻意不关 socket**
        （映射是资产），但保活线程后来死掉 / 被 `stop_keepalive()` 之后，
        `link is not None and peer_addr is not None` 仍然为真 —— 旧代码拿这句当
        `transport_ready` 的最后一条，于是 `start()` 的幂等闸门会**永远**挡住
        重打，洞死了也再没人来打。

        判据必须是链路**活着**：
          * keepalive 线程在跑（还没交给承载层的阶段）；
          * 隧道已经接手（`carried`）—— `DirectLink.keepalive_running` 自己会
            为 `carried` 返回 True（`direct.py:1619`）。
        """
        if link is None or getattr(link, "closed", False):
            return False

        return bool(getattr(link, "keepalive_running", False))

    @property
    def keepalive_running(self):
        """Whether the punched link is currently being kept warm."""
        return bool(getattr(self.link, "keepalive_running", False))

    #: The link the STATUS CARD shows: the first one that is up and warm.
    @property
    def link(self):
        for entry in self.links.values():
            if entry.get("link") is not None:
                return entry["link"]

        return None

    @property
    def peer_addr(self):
        for entry in self.links.values():
            if entry.get("addr"):
                return entry["addr"]

        return None

    def link_for(self, peer):
        """The entry for one peer, if we have a punched link to it."""
        return self.links.get(peer)

    def set_backend(self, host, port):
        """
        房主会话开始时：给【所有】链路配本机 Minecraft 的地址，包括以后新建的。

        ============================================================================
          为什么这里是"所有"，而不是 take_path 拿到的"那一条"
        ============================================================================

        Host 的每个玩家各有一条链路；会话按中继配对而来，和玩家没有一一对应，
        `take_path()` 拿到的可能是别的玩家的链路。如果只给那一条配后端，
        其余玩家的 sid 数据到了自己的链路上却没人去连本机世界 —— 表现就是
        "第三个玩家洞打上了、游戏却连不上"（多人测试实测抓到过，一模一样）。
        """
        self._backend = (host, int(port))

        for carrier in list(self._carriers.values()):
            with contextlib.suppress(Exception):
                carrier.set_backend(host, int(port))

    @property
    def tunnels_up(self):
        """
        How many LONG-LIVED tunnels are actually established right now.

        A punched hole is not a tunnel: the tunnel exists once the HELLO
        handshake completed (`PeerLink.started`). The user's rule is that the
        tunnel comes up the moment the punch lands -- before any game -- so
        this counter is what proves it (and what the tests pin).
        """
        return sum(
            1 for carrier in self._carriers.values()
            if carrier.started and not carrier.stop.is_set()
        )

    def available(self):
        """
        Is there a punched link right now, usable by a session?

        NOT the same as `self._established`. This is the question the cores ask,
        and it stays true for the whole life of the room, including between two
        consecutive Minecraft connections.
        """
        return self.ready

    @property
    def transport_ready(self):
        """
        Is the direct transport available OR still being made available?

        ========================================================================
          3.0.3 的变化：打洞从「点开始连接」才开始
        ========================================================================

        The punch now starts when the user presses 开始连接 -- the SAME moment
        the session (relay pairing) starts. So the very first game connection
        can arrive while the first punch is still spraying. The provider must
        WAIT in that window, not conclude "no direct link" and refuse the
        session. `running` is True for the whole time the punch loop is alive
        (it only stops on 停止 / leaving the room), so including it here means:

            waiting while punching, ready once punched,
            None only when the punch has given up for good.

        ============================================================================
          隐患 2：最后那条 `link is not None and peer_addr is not None` 已经删掉
        ============================================================================
        它读的是"引用还在不在"，不是"洞还活着吗"：停止打洞不关 socket，保活线
        程死掉之后引用仍然在，于是 `start()` 的幂等闸门（`_established and
        transport_ready`）会永远挡住重打。现在只剩两个**活**的判据：
        `running`（正在打）与 `ready`（打通而且保活/隧道真的在维持映射）。
        """
        return self.running or self.ready

    def on_host_gone(self, code="", reason=""):
        """
        【v3.9.16】host 没了 —— **直连模式自己的判死依据**。

        ============================================================================
          为什么必须有它（分析报告 §2.2 第 5 点）
        ============================================================================
        P2P（STUN 直连）模式下，玩家的游戏字节**根本不走中继**，中继唯一能做
        的只是在成员控制连接上发那一行文本。于是"host 走了"这件事在直连侧
        **没有任何独立的物理表现**：

          * UDP 洞不会关（NAT 映射还在，保活线程照样每 5 秒发一次包）；
          * 隧道不会报错（对端只是不再出声）；
          * `LINK_DEAD_S`（20 秒）判的是"有没有数据"，而**保活本身就在制造
            数据** —— 于是它可能永远判不死。

        结果就是：host 已经停服了，直连侧那条隧道还挂在"已打通"，玩家停在一个
        对端根本不存在世界里的链路上。

        所以这里由**控制连接上的那一行**来判死：把全部对端的隧道与洞都拆掉、
        把"谁曾经开始过连接"的证据忘掉。host 回来时，这一整套会按正常流程
        重新打一遍（几秒），而不是复用一条通向虚无的旧路。

        它**不关** `RoomDirectLink` 本身（那是房间级的、host 回来还要用），
        只作废这一轮的结果。
        """
        dropped = []

        for peer in list(self.links.keys()):
            with contextlib.suppress(Exception):
                # 和"对端离开房间"同一条口径：他走了，"他曾经开始过"这条
                # 证据也要一起忘掉，否则他一回来（同 client_id）就立刻又被
                # 当成打洞目标，而那条洞已经不存在了。
                self._forget_heard(peer)
                self._drop_peer(peer)
                dropped.append(peer)

        self.log(
            f"[direct] host is gone ({code or 'HOST_GONE'}): dropped "
            f"{len(dropped)} punched link(s)"
            + (f" [{', '.join(sorted(dropped))}]" if dropped else "")
            + " -- they will be re-punched once the host is serving again"
        )

        return dropped

    def release_all_paths(self):
        """
        Deprecated no-op. Kept because the engine and the session layer call it.

        It used to clear the per-peer "claimed" set so the next Minecraft
        connection could use the hole again. Links are now SHARED (one tunnel,
        many `sid` streams -- see `take_path`), so there is nothing to give
        back: the hole stays with the room for as long as the room lives.
        """
        return None

    def release_path(self):
        """
        Deprecated no-op. See `release_all_paths`.

        The problem it used to solve was real -- the first Minecraft connection
        (a server-list probe) must not consume the hole the real join needs --
        but the cure is now structural: both connections ride the SAME tunnel on
        different `sid`s, so neither one can use the other's link up.
        """
        return None

    def take_path(self):
        """
        把一条**已经打通**的直连链路交给会话用。

        ============================================================================
          这一版和上一版最大的区别：链路是【共享】的，不再是"取走一次"
        ============================================================================

        上一版把链路当成"一条可靠流用完、还回来再给下一条"（`_claimed`）。
        那个模型的前提是"一个 socket 只能有一条可靠流"，而参考工程的答案是
        **一条隧道承载多条流（sid）**：玩家的服务器列表探测和真正进世界是两条
        Minecraft 连接，共用同一条隧道，各走各的 sid。

        所以这里不再"认领/归还"：同一个对端永远拿到同一个 `PeerLink`，由它按
        sid 分用隧道。

        为什么这件事必须做对：Minecraft 的两条连接**可能重叠**（探测还没关，
        进世界已经开了）。如果每条连接各建一个隧道，对端的 `UdpHub` 会在同一个
        IP 下看到两个隧道，判定 `ambiguous packet ... dropped` 并把包全丢掉 ——
        表现就是"洞打通了，游戏连不上"。
        """
        from . import direct_carry

        for peer, entry in self.links.items():
            punch_link = entry.get("link")

            if punch_link is None:
                continue

            # `keepalive_running` is the right question, and it is still
            # answered correctly once a tunnel has adopted the socket: the
            # punch link's own property returns True for "kept warm" OR
            # "carried by a live tunnel" (see direct.py).
            if not getattr(punch_link, "keepalive_running", False):
                continue

            existing = self._carriers.get(peer)

            if existing is not None and not existing.stop.is_set():
                return existing

            try:
                link = direct_carry.PeerLink(
                    peer, punch_link, log=self.log,
                    peer_addr=entry.get("addr"),
                )
            except Exception as e:
                self.log(f"[direct] cannot build a direct link for {peer}: {e}")
                continue

            self._carriers[peer] = link

            return link

        return None

    # --------------------------------------------------------
    # 实时阶段文案（审计问题 7）
    # --------------------------------------------------------

    def _set_phase(self, text):
        """记下打洞进行到哪一步。**只写文本**，绝不影响打洞本身。"""
        text = str(text or "")

        if text != self._punch_phase:
            self._punch_phase = text
            self._phase_entered = time.monotonic()

    def _phase_elapsed(self):
        if not self._phase_entered:
            return None

        return max(0.0, time.monotonic() - self._phase_entered)

    def direct_step(self):
        """
        「打洞进度」这一行现在该显示什么。

        阶段 + 已在这一步花了多久 + （打洞阶段）第几轮/这个对端第几次。
        拿不到阶段就返回空串，界面走它原来的兜底文案 —— 阶段文案是**装饰**，
        绝不允许它影响打洞或让界面崩掉。

        ============================================================================
          2026-09-27（用户报的 BUG）：什么时候该计时、什么时候不该
        ============================================================================

        用户原话：房间里只有自己（没有对端）时，不能显示"等待同步起降"并且
        **一直正计时**；应该显示「等待对端用户加入…」且**不倒计时**。

        所以三种文案分得很清楚：

          * `等待对端用户加入…`  —— **没有对端**（花名册里除我之外没人）。
            静止文案，**不**加秒数：秒表在这里没有信息量，只会让人以为卡住了。
          * `等待对端起跑公告`  —— **有对端**，但还没收到它的 `DIRECT_PUNCH`。
            这是我们真正在等、而且**有上限**的那一段（`PUNCH_SYNC_WAIT_TOTAL_S`
            / `PUNCH_SYNC_WAIT_S`），所以给"第 N 秒"是有意义的。
          * `等待同步起降：…`  —— 已经收到对端公告、正在等那个约定时刻。
            `punch.await_punch_start` 报的是**倒计时**（`waiting X.Xs`），
            照原样显示（`_attach_punch_steps` 接的那条）。

        运行时长仍然只加在**有上限**的阶段上（NAT / 入站测量、打洞本身）——
        那些阶段的"已经花了 N 秒"能告诉用户还要不要等。
        """
        phase = self._punch_phase
        elapsed = self._phase_elapsed()

        if not phase:
            return ""

        # 静止文案：等待一个"还不知道要等多久、也没有进度可显示"的阶段。
        # `PHASE_NO_HOST`（基线 §B：房间暂无 Host）与 `PHASE_CONFIRM_HOST`
        # （§E 第一步还没确认完）同样**不**加秒数 —— 它们没有上限，正计时只
        # 会让用户以为卡住了（2026-09-27 用户报的同一条）。
        if phase in (
            self.PHASE_WAITING_FOR_PEER,
            self.PHASE_WAITING_FOR_PEER_CONNECT,
            self.PHASE_NO_HOST,
            self.PHASE_CONFIRM_HOST,
        ):
            if self.attempts:
                # 已经打过几轮（对端进进出出）：把轮次说出来，但**不加秒数**。
                return phase + f" · 第 {self.attempts} 次尝试"

            return phase

        if elapsed is not None and elapsed >= 1.0:
            if phase == self.PHASE_WAITING_FOR_ANNOUNCE:
                # 有对端、有上限：告诉用户已经等了第几秒。
                phase += f"（第 {elapsed:.0f} 秒）"
            else:
                phase += f"（{elapsed:.0f}s）"

        if self.attempts:
            phase += f" · 第 {self.attempts} 次尝试"

        return phase

    def link_rows(self, punching=None):
        """
        每个 peer 一行：状态卡的「一人一行」和房间成员列表都读这一份。

        ============================================================================
          为什么按【花名册】出人，而不是按已打通的 links
        ============================================================================
        用户要的是"来一个人多一行、走一个人少一行，没有空行"。links 字典只在
        **打通之后**才有条目，所以一个刚进房、还在打洞的玩家不会出现在上面 ——
        那正是用户抱怨的"后面进来的直接冷落在一边"。
        所以行来自 `_peers()`（当前该打洞的人），已经打通的 links 用来补地址。

        ============================================================================
          ★ `punching` 门（审计问题 2：relay 模式下还一人一行「打洞中」）
        ============================================================================
        调用方（`state()`）把"现在到底有没有在打洞"传进来。**没在打洞时**只保留
        `links` 里已经打通的条目，花名册上那些"从没打过"的行全部丢掉：
        `close()` 只清 links 不清花名册，而 `_room_direct` 对象一旦创建就永不置
        None，所以只要进过一次 STUN 房间，relay 模式下就会永远留着"打洞中"的
        行 —— 那是纯误导。

        `punching=None`（老调用方 / 单测直接读 rtt）时不设门，行为与以前一致。

        每行的字段（拿不到就是空，界面显示 未知 / `-`，绝不编）：
          peer        对端标识（中继的 client_id；老中继下是合成名）
          addr        "ip:port"（打通的地址优先，其次是对方公告的候选地址）
          ready       洞是否已打通且在保活
          attempts    这个 peer 打了几次（0 = 还没开始/已成功）
          peer_is_host 对方是不是**中继广播的当前 Host**（拿 `host_id=` 精确匹配）
          nat         对方公告的映射类型（punch.NatSub）
          filtering   对方公告的过滤档位（punch.Filter）
          rtt_ms      这条直连【到对端】的真实 RTT（毫秒），或 None
        """
        if punching is not None and not punching:
            # 已经打通的洞（`links` 里有条目）永远要显示 —— 那是已经发生的事实，
            # 和"现在有没有在打洞"无关。只有**从没打过**的花名册行才被这道门
            # 挡住（审计问题 2 的真正症状：relay 模式下仍一人一行「打洞中」）。
            if not self.links:
                return []

        try:
            roster = [p for p in self._peers() if p]
        except Exception:
            roster = []

        # 当前 Host 的 client_id：**只认中继的 `host_id=`**（PROTOCOL §8.1）。
        # 不再从 `to=` 推断（见 direct.py 里 `peer_is_host` 的说明）。
        try:
            current_host = punch_flow.host_id_of(self.engine.room_state())
        except Exception:
            current_host = ""

        keys = list(roster)

        # 已经打通、但（老中继 / 花名册还没刷新时）不在 roster 里的，也要显示，
        # 否则一条活着的直连会从界面上消失。
        for peer in self.links:
            if peer not in keys:
                keys.append(peer)

        rows = []

        for peer in keys:
            entry = self.links.get(peer) or {}
            link = entry.get("link")
            # ★ 与 `peer_ready()` 同一个判据：隧道已经接手的洞也算 ready
            # （用户第 2 条：打通之后那一行必须在，不许因为打洞层的保活账
            # 掉了就把「P2P 已直连」抹掉）。
            ready = self._link_alive(link) or self._carried_alive(peer)

            addr = entry.get("addr")
            addr_text = f"{addr[0]}:{addr[1]}" if addr else ""

            punch_link = self._punch_links.get(peer)

            if not addr_text and punch_link is not None:
                # 还没打通：至少把对端公告的公网候选显示出来（真实数据）。
                peer_addr = getattr(punch_link, "peer_addr", None)

                if peer_addr:
                    addr_text = f"{peer_addr[0]}:{peer_addr[1]}"

            # 对端 RTT：只有长连接隧道（PeerLink → UdpTunnel 的 keepalive 往返）
            # 真的建起来之后才有数字。**这不是新协议**：T_PING/T_PONG 本来就是
            # 隧道的保活，udptunnel.py 那边只加了"记时刻、相减"的只读插桩。
            # 洞打好了但隧道没起来 / 第一次往返没回来 -> None（界面显示 `-`）。
            carrier = self._carriers.get(peer)
            rtt_ms = None

            if carrier is not None and ready:
                with contextlib.suppress(Exception):
                    rtt_ms = carrier.rtt_ms

            # §G 第 3 层：每一行自己的阶段 + 【对端真名】 + （打通的话）地址:端口
            # + 延迟。
            #
            # 这一份是**只读**的：`sess.row_text()` 绝不改任何状态（基线 §G
            # "不许为修 UI 改动 P2P 逻辑"）。没有状态机（老路径 / 还没开打）
            # 就退回老字段，界面照旧显示。
            sess = self.sessions.get(peer)
            stage_text = ""
            attempt_id = ""
            latency_text = None
            name = self._display_name(peer)

            if sess is not None:
                stage_text = sess.stage_text
                attempt_id = sess.attempt_id
                latency_text = sess.latency_text(rtt_ms)

                # 状态机里那一份名字以房间状态为准（中继可能刚广播了改名）。
                if name:
                    sess.label = name
            elif ready:
                latency_text = punch_flow.LATENCY_NOT_CONNECTED

            parts = sess.row_parts(rtt_ms) if sess is not None else {}

            if parts:
                # 逐目标行 `【…】` 那一格 = 对端真名；打通那一行还要带地址。
                parts["name"] = name or parts.get("name") or peer
                parts["addr"] = sess.addr if sess is not None else ""

            rows.append({
                "peer": peer,
                "name": name,
                "addr": addr_text,
                "ready": ready,
                "attempts": int(self._peer_attempts.get(peer, 0) or 0),
                "peer_is_host": bool(
                    current_host and peer == current_host
                ),
                "nat": getattr(punch_link, "peer_nat", None),
                "filtering": getattr(punch_link, "peer_filter", None),
                "rtt_ms": rtt_ms,
                # §G：逐目标一行的三件新东西（都是**显示**用的）。
                "stage": sess.stage.value if sess is not None else "",
                "stage_text": stage_text,
                "attempt_id": attempt_id,
                "row_text": sess.row_text(rtt_ms) if sess is not None else "",
                "row_parts": parts,
                "latency_text": latency_text,
            })

        return rows

    def state(self):
        """
        The room punch in the shape the status card reads.

        ONE source of truth for the three rows that used to disagree:

          * 直连尝试 / 打洞进度  <- whether we are punching, retrying or DONE;
          * 入站可达             <- the inbound verdict from the last round;
          * 网络类型             <- the measurement (kept elsewhere).

        The owner's report -- "进度显示已直连，直连还未尝试，入站可达未测，通路还
        提示正在尝试" -- was four rows reading four different objects: the room's
        punch, the session's punch, the session's inbound probe and the room's
        NAT monitor. Two of those only exist after a session starts, so before
        that they read as "not tried"/"not measured" while the room punch was
        already reporting success.

        These keys are the SAME names the session snapshot uses on purpose, so
        the renderers need no special case.
        """
        peers_ready = [
            f"{entry['addr'][0]}:{entry['addr'][1]}"
            for entry in self.links.values()
            if entry.get("addr") and getattr(
                entry.get("link"), "keepalive_running", False
            )
        ]

        if peers_ready:
            return {
                "direct_via": "udp",
                "direct_peer": peers_ready[0],
                "direct_peers": peers_ready,
                "direct_attempts": self.attempts,
                # 已打通：主行由渲染层写成「已直连 <peer>」，这里不给阶段文案。
                "direct_step": "",
                "inbound_state": self.inbound_state,
                "inbound_note": self.inbound_note,
                # The link is up and kept warm, so anything still missing is
                # the GAME, not the punch. The card needs to say which.
                "stun_ready": True,
                # 3.0.3: how many LONG-LIVED tunnels are actually established.
                "tunnels_up": self.tunnels_up,
                # A linked peer is, by definition, still being served: the link
                # is kept warm for as long as the room lives.
                "punching": True,
                # 3.0.9: one row per peer hole (status card + member list).
                "links": self.link_rows(punching=True),
                # §G 三层：父级只汇总（通路 n/m + 打洞进度 n/m）。
                "flow": self.flow_summary(punching=True),
                "target_reason": self.target_reason,
            }

        punching = bool(self.running)

        if self._established:
            # Punched earlier and the keepalive lapsed (only possible after an
            # explicit close, e.g. leaving the room). There is no timed expiry.
            step = "直连已断开，正在重打" if punching else "直连已断开"
        else:
            # 实时阶段文案优先（审计问题 7）：测量 NAT 类型中 / 测量入站策略中 /
            # 等待同步起降 / 打洞中（第 N 轮）。它在整轮里一直有值，所以那十几秒
            # 的等待不再是静止的一行。
            step = self.direct_step()

            if not step:
                if self.attempts:
                    step = f"第 {self.attempts} 次尝试" + (
                        f"（{self.last_reason}）" if self.last_reason else ""
                    )
                elif punching:
                    step = "正在准备打洞（交换候选地址）"
                else:
                    step = ""

        return {
            "direct_via": None,
            "direct_peer": "",
            "direct_attempts": self.attempts,
            "direct_step": step,
            "inbound_state": self.inbound_state,
            "inbound_note": self.inbound_note,
            # Punched earlier and still held, but the keepalive is not running
            # -- i.e. the link exists but is not usable right now.
            "stun_ready": False,
            # Is anything actually TRYING right now? The card must not say
            # "打洞中" on the strength of a mode, only on the strength of a
            # running punch.
            "punching": punching,
            "tunnels_up": self.tunnels_up,
            # 3.0.9: one row per peer hole (status card + member list).
            # `punching=False` -> 空列表（审计问题 2：relay 模式不许再出行）。
            "links": self.link_rows(punching=punching),
            # §G 三层：父级只汇总（通路 n/m + 打洞进度 n/m）。
            "flow": self.flow_summary(punching=punching),
            "target_reason": self.target_reason,
            # ★ 用户 2026-09-30：每个对端**自己广播**的公网端点（成员列表显示）。
            "addrs": dict(getattr(self, "peer_addrs", {}) or {}),
            # ★ 用户 2026-10-01：对端首包证明的**全锥**证据（纯展示：界面据此
            #   把「NAT1/2 未定」升级成「NAT1 全锥形」）。
            "eif_proof": bool(getattr(self, "eif_proof", False)),
            # ★ 用户 2026-09-30（第 7 条）：有目标在走"起跑前倒计时"时为 True。
            # 界面据此把逐目标那一块的重画频率提到每 150ms（1 秒一拍画不出
            # `1.5s -> 1.2s -> 开打`）。**纯显示信号**。
            "countdown": self.countdown_active(),
        }

    def describe(self):
        if self.ready:
            return f"ready peer={self.peer_addr[0]}:{self.peer_addr[1]}"

        if self.attempts:
            return f"punching attempts={self.attempts} last={self.last_reason}"

        return "not started"

    # ---- 生命周期 ----

    def start(self):
        """
        开始打洞。幂等；可以安全地从界面线程调用。

        ============================================================================
          ★ 用户 2026-09-29 第 3 条：中继 -> STUN 热切换之后打洞根本没起来
        ============================================================================

        界面每秒（`_sync_room_nat` -> `watch_room_nat(True)` -> 这里）都会调
        `start()`，所以它同时是"开始打洞"和**唯一的活性兜底**：
        `_run()` 循环只要不在了，下一次调用就必须把它拉起来。

        旧实现在这里还有第二道闸门：

            if self._established and self.transport_ready:   # ← 已删除
                return

        它的本意是"已经打通了就别再来一轮"（每打一次都是新 socket/NAT 映射）。
        但**闸门问错了问题**：它把"要不要重启循环"和"要不要重打这个洞"混成了
        一件事。于是出现两个方向都能复现的故障：

          * 循环因为任何原因退出（`_run()` 里 `room_current()` 短暂为 None、
            热切换拆旧通路、`_adopt_peer_connected()` 在循环外把
            `_established` 置真并挂上一条 links 条目……），而**洞/引用还留着**
            -> `start()` 永远提前 return -> 打洞再也不会恢复。用户看到的就是
            「状态机显示已连接、按钮是释放的、实际没有开始打洞」；
          * 反过来，洞还活着时**新进房的 peer 永远等不到打洞**（旧代码里记着的
            那条已知问题）。

        现在职责分清楚了：

          * **这里**只负责"循环在不在"（`running` 幂等）；
          * "不许重打活着的洞"由 `_run()` 里的 `self.peer_ready(peer)` 跳过 +
            `ConnSession.cycle_action(link_alive=True) -> CYCLE_KEEP` 负责
            —— 它们才是"这条洞现在能不能用"的判据。

        另外给每一轮循环发一个 token（`_run_token`）：`stop()` 只能 cancel
        还没跑起来的 future，已经在事件循环里跑的旧协程**不会**被取消，
        于是"重开一次"可能在短时间内留下两条 `_run()`。旧循环下一次抬头发现自己
        的 token 已经不是当前那个，就会自己退出（见 `_run()` 开头）。
        """
        if self.running:
            return

        loop = self.engine.loop()

        if loop is None:
            self.log("[direct] room punch not started: the network loop is not up")
            return

        self._stop = asyncio.Event()

        # 新一轮的 token：任何还在收尾的旧循环会在下一次抬头时自行退出。
        self._run_token = object()
        token = self._run_token

        if self.links:
            # 可观测：这是"循环不在了但洞还留着"的那条活性兜底，不是正常路径。
            self.log(
                "[direct] punch loop was not running; restarting it "
                "(live links are kept as-is and will not be re-punched)"
            )

        self._task = asyncio.run_coroutine_threadsafe(self._run(token), loop)

    def stop(self):
        """
        停止打洞（用户点「停止」/ 退房 / 切模式）。计数一并归零。

        用户 2026-09-24："包括未连上的时候手动停止的，也重新算" —— 下一次
        开始连接必须从第 1 次尝试重新数，不能继承上一次的残留。

        **刻意不关 socket**：如果已经打通了，那个映射就是资产，会话可能正要
        来取。真正需要关的场景（退房、切回 relay）由 `close()` 负责。
        """
        task = self._task

        self._task = None

        # ★ 让"已经在事件循环里跑起来"的旧协程自己退出。
        #
        # `task.cancel()` 只能取消**还没开始跑**的 future；协程一旦开始执行，
        # `run_coroutine_threadsafe` 返回的那个 future 取消不掉，它会继续在
        # 事件循环里跑完自己的 while。把 token 置空 = 旧循环下一次抬头发现
        # 自己已经不是当前那一轮，就 return（见 `_run()`）。
        #
        # 只动 Python 侧的一个属性：**不碰** `asyncio.Event`（那是事件循环线程
        # 的东西，从界面线程 `set()` 不是线程安全的）。
        self._run_token = None

        # 手动停止 = 这一轮结束了：尝试次数与最后一次失败原因都清零。
        self.attempts = 0
        self.last_reason = ""
        self._peer_attempts = {}

        # §F：手动停止也是终态 —— 每条连接的 Timer 立刻停止、清零、释放。
        # **不清 sessions 字典**：逐目标行还要显示"这一条现在是什么状态"，
        # 而"没在打洞"由 `state()["punching"]` 那道门拦住（审计问题 2）。
        for sess in self.sessions.values():
            with contextlib.suppress(Exception):
                if sess.timer.running:
                    sess.timer.finish()

        if task is not None and not task.done():
            task.cancel()

    def close(self):
        """关掉底层 socket。点停止 / 退房 / 切回 relay / 客户端退出时调用。"""
        self.stop()

        # 还在半路的"打通后建隧道"任务先取消：它们可能正握着 to_thread 里的
        # 握手，不取消会在 close 之后把隧道建到已经关掉的 socket 上。
        for task in list(self._tasks):
            task.cancel()

        self._tasks.clear()

        # 后台的入站探测也要停：它读的是即将被关掉的打洞 socket。
        task = self._ingress_task
        self._ingress_task = None

        if task is not None and not task.done():
            task.cancel()

        # The live tunnels first: they hold the punched socket, and their own
        # keepalive/reader threads must be told to stop before the socket goes.
        for link in list(self._carriers.values()):
            with contextlib.suppress(Exception):
                link.close()

        self._carriers = {}

        # 复用的打洞链路也要关（它们各自握着一个 UDP socket）。
        for link in list(self._punch_links.values()):
            with contextlib.suppress(Exception):
                link.close()

        self._punch_links = {}

        if self.link is not None:
            with contextlib.suppress(Exception):
                self.link.close()

        for entry in self.links.values():
            link = entry.get("link")

            if link is not None:
                with contextlib.suppress(Exception):
                    link.close()

        self.links = {}
        self._claimed = set()
        self._established = False

        # §F：整机（退房 / 切模式 / 客户端退出）时，所有连接的 Timer 立即
        # 停止、清零、释放。状态机本身也丢掉 —— 下一次开始连接是全新的一套。
        for sess in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                sess.timer.finish()

        self.sessions = {}
        self.target_reason = ""
        self._heard_started = set()
        self._heard_at = {}
        self._heard_seen = {}
        self.peer_addrs = {}
        self.my_addr_sent = ""
        self._member_ids = set()
        #: 正在 `_punch()` 里（attempt 还没回来）的 peer —— 期间**不接管**它的洞
        #: （见 `_adopt_peer_connected`：不能在喷洒还在跑的时候起隧道读这个
        #: socket）。attempt 一回来，下一轮 `_run()` 立刻接管。
        self._punching_peers = set()

    # ---- 打洞循环 ----

    def _peers(self):
        """
        Every peer this side should try to punch to —— **§D：每轮重新拉**。

        ============================================================================
          ★ BUG 1 的根因就在这里（房主≠host 的三人房，热切换后目标错位）
        ============================================================================

        老实现是 `_host_client_id()`：**取花名册里第一个不是我的人**，然后
        "让打洞自己的身份校验去兜底"。房主（owner）在花名册里排在 host 前面时，
        player 侧算出的目标就是 **owner 而不是 host**；而中继的 `_direct_peer()`
        是按**真正的 host 位置**转发的，于是：

          * player 把候选发给了 owner（中继按 `to=` 路由），host 什么都没收到；
          * host 侧看到的是"和这个 player 已经打通"（它收到的是同一批候选的另一
            半），两端状态对不上；
          * 房主能进 MC（它是 owner，走的是中继隧道），那个 player 连不上。

        现在的规则**只认事实**（`punch_flow.select_targets`）：

          * **Player**：只打 **Host**（`host_id=` 优先；老中继时用 `host_name=`
            在 `names=` 表里反查；再没有就**返回空**，界面显示「确认 Host」）——
            **绝不猜**；
          * **Host**：只打「**已开始连接**」的玩家（`started=` token）；老中继
            没有这个 token 时退回花名册里的所有人（`started_unknown`），
            行为与今天一致 —— 这正是"加法、不破坏"的边界（BUG 4 的 host 半边
            要等中继的 `started=` 才能真正生效）。

        每调用一次就是一次新的目标列表 —— `_run()` 在**每一轮**开头调它，
        所以"C 走了、E 来了"、"Host 换人了"两会立刻反映出来（§D）。

        ============================================================================
          ★ 隐患 6：显示路径只许读 —— 所以这里拆成两半
        ============================================================================
        `_select_targets_now()` 是**纯函数**（只算、不写任何字段），
        `_peers()` 只是"算 + 提交 `target_reason`"。`_session_order()` 走纯的
        那一半，于是界面每刷新一次都不会偷偷改 `target_reason`。
        """
        targets, reason = self._select_targets_now()
        self.target_reason = reason

        # 记住"这些 peer 是花名册里的真成员 id"（见 `_member_ids` 的说明）。
        with contextlib.suppress(Exception):
            self._member_ids.update(self._room_roster()[1])

        return targets

    def _select_targets_now(self):
        """
        `_peers()` 的计算半边：返回 `(targets, reason)`，**绝不写任何字段**。

        显示路径（`flow_summary` / `session_rows` / `_session_order`）必须用
        这个 —— 读一行状态不该改任何东西（隐患 6）。
        """
        state = self.engine.room_state()

        if state is None:
            return [], "no_room"

        me = getattr(state, "client_id", "") or ""

        if not me:
            return [], "no_room"

        members = int(getattr(state, "members", 0) or 0)

        if members < 2:
            return [], "no_peer"

        targets, reason = punch_flow.select_targets(state, me)

        # ================================================================
        #  Host：把"对端主动发来打洞协调行"也算作"已开始连接"
        # ================================================================
        #
        # 这是 §D 在**本架构**下的正确退路（见 `_heard_started` 的说明）：
        # `started=` 由中继按 PLAYER/HOST 连接算，而玩家在游戏连上来之前没有
        # PLAYER 连接 —— 只认它，Host 就一个目标都拉不到。对端自己发来的信标
        # 是"他已经点了开始连接"的硬证据，和 `started=` 取并集，**仍然不会**
        # 对没开始的 peer 打洞（BUG 4 保持修好）。
        #
        # ================================================================
        #  ★ 用户 2026-09-29 第 1 条：`conn=idle` 的对端**永远不是目标**
        # ================================================================
        #
        # `_heard_started` 是"只增不减"的（只有 `close()` 清），所以一个**曾经**
        # 发过 DIRECT_HIT 的玩家，在他停止连接（`conn=idle`）之后仍然是 Host 的
        # 目标：Host 照样为他建状态机、走 §E 到 `SYNC_START`，而对端根本不回应
        # ——那一行就长期显示「等待同步起跑」（用户第 1 条）。
        #
        # `conn=idle` 是中继从连接现算的**硬事实**（"这个人此刻没有任何游戏连接"），
        # 比"他曾经发过信标"更硬，所以它一票否决。未知（老中继没有 `conn=`）
        # 时退回原行为，一个字不变。
        if getattr(state, "is_host", False) and targets is not None:
            extra = [
                p for p in (getattr(state, "peer_ids", None) or [])
                if p and p != me
                and p in self._heard_started
                # ★ 第 1 条：**曾经**开始过不算，必须"此刻还在连"：
                #   * 中继说他不是 idle（waiting/pairing/connected），或者
                #   * 他最近还在发打洞信标（`_heard_at` 新鲜）。
                # `conn=idle` + 信标已经停了 = 他已经不收发协调行了 -> 不打。
                and (
                    not punch_flow.peer_is_idle(state, p)
                    or self._beacon_fresh(p)
                )
            ]

            if extra:
                merged = list(targets)

                for peer in extra:
                    if peer not in merged:
                        merged.append(peer)

                targets = merged

                if reason in ("started", "started_unknown"):
                    reason = "started+heard"

        return targets, reason

    def _beacon_fresh(self, peer):
        """
        这个 peer 最近（`HEARD_FRESH_S` 之内）还发过打洞协调行吗？

        与 `_heard_started` 配对使用：那个集合回答"他曾经开始过吗"，这个回答
        "他现在还在连吗"。`conn=idle` + 信标已停 = 他已经不在连接
        （用户第 1 条 / 第 6 条的症状来源）。

        ============================================================================
        #  判据优先级：房间在**收到那一刻**记的时间戳 > 本地的 peek 记账
        ============================================================================
        `RoomSession.direct_rx_at`（按 `from=` 记到达时刻）是权威的：它不受
        "peek 是非破坏性的、同一条旧行被反复读到"影响。老客户端不发 `from=`、
        或者单测里的假房间没有这个字段时，退回 `_heard_at`（由 `_heard_seen`
        的计数增长维护 —— 也不会被旧行无限刷新）。
        """
        peer = str(peer or "")
        stamp = None

        with contextlib.suppress(Exception):
            room = self.engine.room_current()
            rx = getattr(room, "direct_rx_at", None)

            if isinstance(rx, dict):
                stamp = rx.get(peer)

        if stamp is None:
            stamp = self._heard_at.get(peer)

        if stamp is None:
            return False

        return (time.monotonic() - float(stamp)) <= HEARD_FRESH_S

    def _forget_heard(self, peer):
        """
        这个 peer **真的离开了房间**：把他"开始过连接"的全部证据一起忘掉。

        ============================================================================
          ★ 用户 2026-09-29（第三轮）第 6 条：被踢的玩家一回来就"无限打洞"
        ============================================================================

        `_heard_started`（"他曾经开始过"）以前**只有 `close()` 会清**。被踢
        （或自己退房）的玩家因此留着一条记录；等他重新进房（`peers=` 里又出现
        他），Host 立刻把他当成"已开始连接"的目标，§E 一轮一轮打下去 —— 而他
        根本没点「开始连接」。用户看到的就是「对端未连接时状态机在
        正在打洞 / 确认 Host / 等待同步起跑 之间反复循环」。

        花名册判死（`_sweep_links` 那条）是**唯一**能证明"他真的走了"的地方，
        所以清理就挂在那里：忘掉"开始过"的证据 + 新鲜度 + 已读计数。
        """
        peer = str(peer or "")

        self._heard_started.discard(peer)
        self._heard_at.pop(peer, None)

        # ★ `_heard_seen`（"这个 sender 的协调行我见过几条"）**故意不抹**：
        # 抹掉就等于把他临走前那几条旧行重新算成"新证据"，于是"忘记 -> 又看见
        # -> 又忘记"每 0.5 秒循环一次（用户 2026-09-30 第 2 条的日志原样）。
        # 计数只增不减没关系：他真的回来并重新发信标时计数会继续增长。
        #
        # `direct_rx_at` 才是"到达时刻"，那一份必须清 —— 否则他离开前的
        # 时间戳如果还在新鲜度窗口里，他一回来就被当成"还在连"。

        # ★ 房间那份"到达时刻"也要一起忘：他走了之后，那条**离开之前**的
        # 时间戳如果还在 15 秒窗口里，他一重新进房就会被当成"还在连"。
        with contextlib.suppress(Exception):
            room = self.engine.room_current()
            rx = getattr(room, "direct_rx_at", None)

            if isinstance(rx, dict):
                rx.pop(peer, None)

    def _note_peer_addrs(self, room):
        """
        收下别人广播的公网端点（`DIRECT_ADDR <ip> <port> from=<cid>`，ADDITIVE）。

        **只用于显示**（成员列表的 `IP:端口` 一列）：不进打洞状态机、不改
        目标/候选。`peek_direct()` 是非破坏性的，所以同一行会被反复读到；
        这里只做"最后写入者赢"，没有副作用（也不刷新任何新鲜度）。

        老客户端不发这一行 -> 表里没有他们的端点 -> 那一列还是 `-`（行为不变）。
        """
        peek = getattr(room, "peek_direct", None)

        if not callable(peek):
            return

        try:
            lines = peek(keywords=("DIRECT_ADDR",))
        except TypeError:
            # 假 room / 老接口：peek 不收 keywords 参数。
            with contextlib.suppress(Exception):
                lines = peek()
        except Exception:
            return

        for line in lines or ():
            parts = str(line or "").split()

            if len(parts) < 3 or parts[0].upper() != proto.ROLE_DIRECT_ADDR:
                continue

            ip = parts[1]
            port = parts[2]

            if not ip or not port.isdigit():
                continue

            # `from=` 是本轮加的加法字段（和打洞信标同一个来源，见 beacon_line）。
            sender = ""

            for token in parts[3:]:
                key, _, value = token.partition("=")

                if key == "from":
                    sender = value.strip()
                    break

            if not sender:
                continue

            endpoint = f"{ip}:{port}"

            if self.peer_addrs.get(sender) != endpoint:
                self.peer_addrs[sender] = endpoint

                self.log(
                    f"[direct] peer {sender} advertises {endpoint} "
                    f"(display only; the punch does not use this)"
                )

    def _announce_my_addr(self, room):
        """
        把我们**自己**测量到的公网端点广播给房间里所有人（`DIRECT_ADDR`）。

        来源按可靠性排序：
          1. 任何一条已打通/已测量的打洞链路自己的 `public`（最准，就是实际
             用于打洞的那个映射）；
          2. 房间 NAT 画像的 `public`（进房就测的那一份）。

        只在**值变化**时发一次（去重），失败也绝不抛（纯显示）。
        """
        if room is None or not callable(getattr(room, "send_direct", None)):
            return

        endpoint = ""

        for entry in (getattr(self, "links", None) or {}).values():
            public = getattr((entry or {}).get("link"), "public", None)

            if public and public[0] and public[1]:
                endpoint = f"{public[0]}:{int(public[1])}"
                break

        if not endpoint:
            with contextlib.suppress(Exception):
                profile = self.engine.room_nat_profile()
                public = getattr(profile, "public", None)

                if public and public[0] and public[1]:
                    endpoint = f"{public[0]}:{int(public[1])}"

        if not endpoint or endpoint == self.my_addr_sent:
            return

        me = ""

        with contextlib.suppress(Exception):
            me = str(
                getattr(self.engine.room_state(), "client_id", "") or ""
            )

        line = f"{proto.ROLE_DIRECT_ADDR} {endpoint}"

        if me:
            line += f" from={me}"

        try:
            sent = room.send_direct(line)
        except Exception:
            return

        # `send_direct` 是协程（真实现）/ 可能是同步的假对象（单测）。
        if asyncio.iscoroutine(sent):
            async def _fire(coro=sent, value=endpoint):
                with contextlib.suppress(Exception):
                    if await coro:
                        self.my_addr_sent = value

            # 和 `_task` 其它调用点同一套：挂进 `self._tasks`，`close()` 会收掉。
            self._tasks.add(asyncio.create_task(_fire()))
        else:
            self.my_addr_sent = endpoint

    # --------------------------------------------------------
    # §F 每目标一条独立连接状态机
    # --------------------------------------------------------

    def _session(self, peer):
        """
        取（或新建）这个 peer 的连接状态机。**一个 peer 一个，绝不复用**。

        新建时把**对端真实名字**（中继广播的 `names=`；老中继回退 `host_name=`
        再回退 client_id）一并写进状态机 —— 逐目标行 `【…】` 那一格要显示的
        就是它（用户 2026-09-28："ABCD 是例子，显示对端的真实名字"）。
        """
        peer = str(peer or "")

        if not peer:
            return None

        sess = self.sessions.get(peer)

        if sess is None:
            sess = ConnSession(
                peer,
                role="host" if self._am_host() else "player",
                log=self.log,
                label=self._display_name(peer),
            )
            self.sessions[peer] = sess

        return sess

    def _display_name(self, peer):
        """
        对端在界面上的**真实名字**：中继广播的 `names=` -> `host_name=`（对当前
        Host）-> client_id。**绝不返回空**（空会让那一格变成 `【】`）。

        名字只从"中继说的事实"里取，绝不靠花名册顺序猜（规则 6 §A 的同一条）。
        """
        peer = str(peer or "")

        if not peer:
            return ""

        with contextlib.suppress(Exception):
            state = self.engine.room_state()

            if state is not None:
                name = (getattr(state, "member_names", None) or {}).get(peer)

                if name:
                    return str(name)

                if peer == punch_flow.host_id_of(state):
                    host_name = str(getattr(state, "host_name", "") or "").strip()

                    if host_name and host_name != "-":
                        return host_name

        return peer

    def _has_other_members(self):
        """
        花名册里除我之外**还有没有别人**（不问他有没有开始连接）。

        「等待对端用户加入…」与「等待对端连接…」的分界就在这里（用户第 5 条）。
        """
        with contextlib.suppress(Exception):
            state = self.engine.room_state()

            if state is None:
                return False

            me = str(getattr(state, "client_id", "") or "")

            return any(
                p for p in (getattr(state, "peer_ids", None) or []) if p and p != me
            )

        return False

    def _am_host(self):
        with contextlib.suppress(Exception):
            state = self.engine.room_state()
            return bool(getattr(state, "is_host", False))

        return False

    def session_for(self, peer):
        """这个 peer 的状态机（只读用；不存在时返回 None）。"""
        return self.sessions.get(str(peer or ""))

    def session_rows(self):
        """
        逐目标明细（§G 第 3 层）的唯一数据源：**每个目标一行**。

        行来自"当前该打的目标"∪"已经有状态机的目标"：
          * 来一个目标加一行 —— `_session()` 在 `_run()` 每轮开头就建好了；
          * 走一个目标删一行 —— `_drop_peer()` 会把状态机一起移走；
        所以**不会留空行**（用户对这一条的要求是逐字的）。
        """
        rows = []

        for peer in self._session_order():
            sess = self.sessions.get(peer)

            if sess is None:
                continue

            rows.append(sess.row_text())

        return rows

    def _session_order(self):
        """
        行的顺序：**只按当前目标列表**（用户能预期的顺序）。

        **只读**：走 `_select_targets_now()`（纯函数），绝不写 `target_reason`
        —— 这是一条显示路径（`flow_summary` 都调它）。

        已经打通、但（因为 `started=` 抖动等）暂时不在目标表里的洞由
        `flow_summary()` 用 `_link_only_session()` 补回来，所以一条活着的直连
        不会从"通路 n/m"里消失；而**早就不是目标的陈旧状态机**不再出现在
        显示里（第 1 / 第 7 条，见下面的说明）。
        """
        order = []

        with contextlib.suppress(Exception):
            targets, _reason = self._select_targets_now()
            order.extend(p for p in targets if p in self.sessions)

        # ============================================================================
        #  ★ 陈旧的 session **不算进显示**（用户 2026-09-29 第 1 / 第 7 条）
        # ============================================================================
        #
        # 旧代码把 `self.sessions` 里**所有** peer 都补进行列表。于是：
        #   * 第 1 条：一个"曾经开始过、现在只是 idle"的对端还挂着一台走到了
        #     `SYNC_START` 的状态机 -> 逐目标行长期显示「等待同步起跑」；
        #   * 第 7 条：被踢 / 已离开房间的玩家没有洞，`_sweep_links` 看不到他，
        #     状态机永远留着 -> 打洞进度分母仍是 3（「打洞中 1/3」）。
        #
        # 显示口径必须和"现在到底在跟谁打"一致：本轮真实目标（§D 每轮重拉）
        # ∪ 已经打通的洞。`links` 那一半由 `flow_summary()` 用只读替身补上，
        # 所以这里不再无条件追加 `self.sessions`。
        return order

    def _link_only_session(self, peer):
        """
        只读替身：给"洞还活着、但状态机已经不在 `sessions` 里"的目标拼一行。

        ============================================================================
          ★ 隐患 6：读路径绝不许建状态
        ============================================================================
        旧 `flow_summary()` 这里调的是 `_session(peer)` —— 那会**写**
        `self.sessions`（界面每刷新一次就多一条状态机，`generation`/`stage` 被
        读路径改掉，显示也因此不对称）。这里 new 一个**用完即弃**的替身，
        **绝不放进 `self.sessions`**；对象本身只是行的渲染数据。
        """
        sess = ConnSession(
            peer,
            role="host" if self._am_host() else "player",
            log=self.log,
            label=self._display_name(peer),
        )
        sess.stage_for(Stage.CONNECTED)
        sess.connected = True

        return sess

    def flow_summary(self, punching=None):
        """
        §G 三层里的前两层（父级**只汇总**，判断全在 ConnSession 里）。

        `punching` 为 None 时按 `self.running` 自己判 —— 调用方（界面）也可以
        显式传，老调用方不传就是老行为。

        **纯函数**（隐患 6）：只读 `self.sessions` / `self.links`，
        绝不 `_session()`（那会写状态）、绝不改 `target_reason`。所以界面每秒
        刷十次也不会把 generation/stage 刷坏，显示也就自然对称。
        """
        if punching is None:
            punching = bool(self.running)

        sessions = [
            self.sessions[peer] for peer in self._session_order()
            if peer in self.sessions
        ]

        # 已经打通的洞也是一条"连接"，即使这一轮的表已经停了（§F 终态清零）。
        # 界面要看到的是「P2P 已直连：n/m」，所以把它们算进去 —— 用**只读替身**，
        # 不写 `self.sessions`。
        for peer in self.links:
            if peer not in self.sessions:
                sessions.append(self._link_only_session(peer))

        return punch_flow.summarize(sessions, punching=punching)

    def _note_heard_started(self, room):
        """
        把"对端主动发来打洞协调行"记成"他已经点了开始连接"的证据。

        行的 `from=<cid>` 是本轮加的**加法字段**（见 `punch_flow.beacon_line`）；
        老客户端不发 `from=`，那时这条证据就没有 —— 于是行为退回"只认中继的
        `started=`"，**不会**因此误伤（也不会因此放宽）。

        peer **真的离开房间**时由 `_sweep_links()`（花名册判死那一条）把它从这里
        清掉 —— 见 `_forget_heard()`；退房时 `close()` 清空全部。

        ============================================================================
          ★ 用户 2026-09-29（第三轮）第 6 条：被踢的玩家一回来就"无限打洞"
        ============================================================================

        `peek_direct()` 是**非破坏性**的（取出来再放回去），所以同一条旧信标会在
        每一轮（以及每次打洞的 0.3 秒 peek）被**重复读到**。旧代码在里面无条件
        `self._heard_at[sender] = now`，于是"很久以前那条 DIRECT_HIT"每次被重新
        读到时都把新鲜度刷新一遍 —— 一个**早就不在连接**的 peer 永远保持"新鲜"，
        永远留在 Host 的目标表里，§E 于是对他一轮一轮打下去（日志里
        `sync start: ... did not report its measurement within 12s` 无限循环）。

        现在只对**新出现的行**盖时间戳：按 senter 记录"我在队列里见过多少条"，
        只有这个计数**增长**时才更新新鲜度。队列被 `wait_direct` 消费掉之后计数
        回落，下一条真信标又是"新的"（计数增长）—— 正常连接照旧刷新。
        """
        peek = getattr(room, "peek_direct", None)

        if not callable(peek):
            return

        try:
            lines = peek()
        except Exception:
            return

        # ============================================================================
        #  ★ 只有**还在房间里**的人**才配当"他已经开始连接"的证据
        # ============================================================================
        #
        # 用户 2026-09-30 第 2 条：某个玩家退房之后，Host 的日志里
        #   `peer X is gone from the room roster; forgetting that it ever started
        #    connecting` 和 `peer X has started connecting` 每 0.5 秒来回刷 ——
        # 因为 `peek_direct()` 是**非破坏性**的，他临走前那几条 `DIRECT_HIT`
        # 还压在队列里；忘记一次就被重新读成"新证据"，于是他又变回目标，
        # §E 一轮一轮空转，并且**连带把别人也拖进重算**。
        #
        # 花名册（中继的 `peers=`）是**唯一**能证明"他还在房间里"的事实。
        # 花名册未知（老中继 / 假 state）时不设这道门 —— 行为与以前一致。
        roster = None

        with contextlib.suppress(Exception):
            state = self.engine.room_state()
            ids = getattr(state, "peer_ids", None)

            if ids is not None:
                roster = {str(p) for p in ids if p}

        # 先数一遍：每个 sender 在这批（非破坏性）peek 里有几条。
        counts = {}

        for line in lines or ():
            head = line.split()[0:1]

            if not head or head[0].upper() not in (
                punch_flow.BEACON_KEYWORD, "DIRECT_CAND", "DIRECT_PUNCH",
            ):
                continue

            info = punch_flow.parse_beacon(line)
            sender = str(info.get("from") or "").strip()

            if not sender:
                # ============================================================
                #  没有 `from=`（老客户端）：只在**唯一可能**时才归属
                # ============================================================
                #
                # 加法协议规则：老客户端不发 `from=`，但它照样会发
                # `DIRECT_CAND`。房里只有**一个**对端时，"这条行是谁发的"
                # 没有第二种可能，所以归属是确定的、不是猜的。
                # 两个以上对端时无从归属 —— 那时宁可不动（Host 会等中继的
                # `started=`），**绝不**把整份花名册都当成"已开始"。
                try:
                    state = self.engine.room_state()
                    me = str(getattr(state, "client_id", "") or "")
                    others = [
                        p for p in (getattr(state, "peer_ids", None) or [])
                        if p and p != me
                    ]
                except Exception:
                    others = []

                if len(others) == 1:
                    sender = others[0]
                else:
                    continue

            if roster is not None and sender not in roster:
                # 他不在花名册里 = 他已经不在这个房间。旧信标不算证据。
                continue

            counts[sender] = counts.get(sender, 0) + 1

        for sender, count in counts.items():
            # ★ 新鲜度：**只有新行**才盖时间戳（见上面的说明）。
            previous = getattr(self, "_heard_seen", {}).get(sender, 0)

            if count > previous:
                self._heard_at[sender] = time.monotonic()

            self._heard_seen[sender] = count

            if sender not in self._heard_started:
                self._heard_started.add(sender)
                self.log(
                    f"[direct] peer {sender} has started connecting "
                    f"(it sent a direct coordination line, evidence stronger "
                    f"than started=)"
                )

            # ================================================================
            #  ★ 2026-09-29：对端上报的**阶段**（用户要的"两端共用一套枚举"）
            # ================================================================
            #
            # 每一条 `DIRECT_HIT stage=<阶段>` 都是对端"我到了这一格"的事实：
            # 记进它自己的状态机（只影响显示 —— 见 ConnSession.note_peer_stage），
            # 于是两端的逐目标行由同一个轴裁决，谁也不会独自往前跳。
            #
            # `stage=connected` 还要**落到连接状态上**（P0：一端显示已直连、另一
            # 端还在反复重试/测量）。那条洞在两端是同一个洞：对端说它通了，我们
            # 手里这个 socket 的映射就是通的，所以这里把它当成一次真正的打通来
            # 收尾（保活 + 建隧道 + 停止重试），真实打洞算法一个字没动。
            for line in lines or ():
                info = punch_flow.parse_beacon(line)
                line_sender = str(info.get("from") or "").strip()

                if line_sender and line_sender != sender:
                    continue

                self._absorb_peer_beacon(line_sender or sender, info)

    def _absorb_peer_beacon(self, sender, info):
        """
        把一条对端信标里的**阶段**信息吃进去（只影响显示 / 直连收敛，不改打洞）。

        从 `_note_heard_started` 里拆出来，职责单一：那里负责"谁开始过 / 新鲜度"，
        这里负责"他走到了哪一格"。整段行为与原实现逐字一致。
        """
        try:
            self._absorb_peer_beacon_inner(sender, info)
        except Exception:
            return

    def _absorb_peer_beacon_inner(self, sender, info):
        stage_value = str(info.get("stage") or "")

        if not stage_value or not sender:
            return

        sess = self.sessions.get(sender)

        if sess is None:
            return

        sess.note_peer_stage(stage_value)

        if stage_value == punch_flow.BEACON_CONNECTED:
            self._adopt_peer_connected(sender)

    def _adopt_peer_connected(self, peer):
        """
        对端明确报了"我已直连"（`DIRECT_HIT stage=connected`）—— 把这条洞在
        本端也**记成已建立**，并停止对本端的重试。

        ============================================================================
          ★ 用户 2026-09-29 的 P0：两端对"这条洞已建立"的认知必须收敛
        ============================================================================
        以前没有任何"我已直连"的上报：打通只写在本端状态机里（`_punch` 成功
        分支的 `sess.finish(True)`）。对端唯一的希望是**它自己的** `link.attempt()`
        也命中 —— 在非对称 NAT / 两端重试节奏错开时它可能一直不命中，于是：

            一端 `P2P 已直连【…】ip:port`（游戏能进），
            另一端每 3 秒一轮 `确认 Host（0s）… N/C`（状态机被自己抹掉）。

        现在打通的那一侧**立刻上报**，这一侧据此收尾。判据仍然只有事实：

          * 我们手上必须有这条 peer 的打洞链路（`_punch_links`），而且它没被关；
          * 链路必须已经知道对端的地址（`peer_addr`，来自 candidate 交换）——
            没有它就无法保活、也无法建隧道。

        真实打洞算法（测量 / 候选 / 喷洒 / 起跑同步）**一个字没改**：这里只做
        "把对端已经证实的结果记到本端，并让本端的保活/隧道接管同一个 socket"。
        两个兜底一条不少：隧道 45s 起不来（`TUNNEL_SETUP_DEADLINE_S`）或 20s
        没有数据（`LINK_DEAD_S`）-> `_sweep_links` 照旧判死重打。
        """
        peer = str(peer or "")

        sess = self.sessions.get(peer)

        if sess is None or sess.connected:
            return False

        if peer in getattr(self, "_punching_peers", ()):
            # 我们自己的喷洒还在跑（`attempt()` 还没回来）：现在接管同一个 socket
            # 会和 spraying 抢读者。等这一轮结束，`_run()` 下一拍就接管。
            return False

        link = self._punch_links.get(peer)

        if link is None or getattr(link, "closed", False):
            return False

        peer_addr = getattr(link, "peer_addr", None)

        if peer_addr is None:
            return False

        addr_text = f"{peer_addr[0]}:{peer_addr[1]}"

        sess.note_peer_stage(punch_flow.BEACON_CONNECTED)
        sess.peer_connected_addr = addr_text

        if self.links.get(peer) is None:
            self.links[peer] = {
                "link": link,
                "addr": peer_addr,
                "ready_at": time.monotonic(),
            }

        # 保活 + 建隧道都建立在**同一个已打通的 socket** 上（新 socket = 新映射）。
        with contextlib.suppress(Exception):
            link.start_keepalive()

        sess.finish(
            True,
            latency_ms=self._link_latency_ms(peer),
            addr=addr_text,
        )

        self._peer_attempts[peer] = 0
        self.last_reason = ""
        self._established = True

        self.log(
            f"[direct] peer {peer} reported stage=connected at {addr_text}; "
            f"adopting the SAME punched socket (keepalive + tunnel) and stopping "
            f"the retry loop for this peer"
        )

        # 建隧道是后台任务；生产路径永远在事件循环里（`_run` / 打洞期间的
        # 周期 peek）。单测里可能会同步调到这里 —— 没有 loop 就只保留
        # "已打通 + 保活"，不抛。
        with contextlib.suppress(RuntimeError):
            self._open_tunnel(peer)

        return True

    def _start_evidence(self):
        """
        开始一轮房间打洞前装一个**本会话**的证据桶（用户 2026-10-01）。

        `sent_to` 是会话级的：**本会话**里我们联系过的对端端点都记在里面，
        所以"对端首包早于我们发包"只会在**第一次接触**那个端点时成立 ——
        这正是用户问的"暖端口会不会误判"的答案：暖过的端点不算证据。
        """
        if self._evidence is None:
            # 防暖端口的作用域 = **本端这一次映射**（`PunchArray` 每次构造都会
            # 把它重置，见 punch.py 的 EVIDENCE_ATTEMPTS 说明）：NAT 放行表是按
            # 映射记的，一次打洞一套新 socket -> 放行表本来是空的。
            self._evidence = {"sent_to": set(), "eif": False, "peer": None}

        with contextlib.suppress(Exception):
            from . import punch as punch_mod

            punch_mod.set_evidence(self._evidence)

    def _collect_evidence(self):
        """把 `PunchArray` 记下来的证据读出来（只读，纯展示）。"""
        evidence = self._evidence

        if not evidence or self.eif_proof:
            return

        if evidence.get("eif"):
            self.eif_proof = True

            self.log(
                "[nat] FULL CONE evidence: the peer's first packet arrived at "
                f"{evidence.get('peer')} BEFORE we ever sent to that endpoint "
                "-> our NAT let an uncontacted endpoint in (eif)"
            )

    async def _run(self, token=None):
        """
        房间打洞主循环。

        `token` 是 `start()` 发的"我是哪一轮"的记号：`stop()` 只能 cancel
        **还没跑起来**的 future，已经在事件循环里的旧协程取消不掉。旧循环
        下一次抬头发现 token 已经不是当前那个，就自己退出 —— 于是"重开一次"
        绝不会留下两条同时在跑的打洞循环（那是 41 次打洞的成因之一）。

        `token=None`（单测直接 `create_task(punch._run())`）表示不属于任何一轮，
        永远不因 token 退出：老调用方行为一字不变。
        """
        try:
            while not self._stop.is_set():
                if token is not None and getattr(
                    self, "_run_token", token
                ) is not token:
                    self.log(
                        "[direct] this punch loop was superseded by a newer one; "
                        "exiting (the new loop owns the room)"
                    )
                    return

                room = self.engine.room_current()

                if room is None:
                    return

                # 每一轮开头先"看一眼"对端发来的协调行：Host 靠它知道谁已经点了
                # 开始连接（见 `_heard_started`）。这不是新协议 —— 中继对
                # DIRECT_HIT / DIRECT_CAND / DIRECT_PUNCH 本来就是逐字转发。
                self._note_heard_started(room)

                # ★ 用户 2026-09-30：先收下别人广播的端点，再把自己的广播出去
                #   （纯显示：成员列表那一列要能看到每个人的地址和端口）。
                self._note_peer_addrs(room)
                self._announce_my_addr(room)

                # ★ 只读：把打洞时记下的"全锥证据"取出来（纯展示用）。
                self._start_evidence()
                self._collect_evidence()

                peers = self._peers()

                # §F：每轮开头**为目标建/刷新状态机**（来一个目标加一行）。
                # `_session()` 幂等 —— 已经打通的目标保持它自己的状态机，
                # 新人只是多一个，**绝不碰**别人那条（§D：新人加入不影响已打通）。
                #
                # ★ 用户 2026-09-28 第 3 条（手动中断后重连）：如果这条状态机
                # 上一轮已经走到**失败**终态 / 被手动停止（表已释放），这一次是
                # **新的一次连接** —— 用 `restart()`（generation+1、从 §E 第一步
                # 重走），而不是 `start()` 把阶段硬拨回起点（那样既不清晰也不落
                # 日志）。
                #
                # ★ 隐患 1：判据**只有** `ConnSession.cycle_action(link_alive)`
                # 一个来源（它内部：成功终态 + 洞活着 = 什么都不做；失败终态 /
                # 表已释放 / 还在 idle = restart；其余 = start）。
                #
                # 这样"成功终态"再也不可能被任何调用方当成"要重开一次连接"：
                # 上一轮的 `if sess.connected and self.peer_ready(peer)` 只是这道
                # 门的一种叫法，现在把它收进状态机自己的 API，绕不过去。
                for peer in peers:
                    sess = self._session(peer)

                    if sess is None or sess.timer.running:
                        continue

                    # ============================================================
                    #  ★★ 已经打通、而且那条洞还活着 → **这一条绝不许重开** ★★
                    # ============================================================
                    #
                    # 打通是**成功**终态（`SUCCESS_STAGES`），不再是"要重开一次
                    # 连接"；只有洞真的死了（`peer_ready` 为假：判死看门狗 /
                    # 对端走了 / 隧道握手失败）才 `restart()` 重来。
                    # `peer_ready()` 与下面打洞循环跳过已打通目标用的是**同一个**
                    # 判据（`DirectLink.keepalive_running`，隧道接手后由 `carried`
                    # 兜住），所以"界面把已直连抹掉"和"重打洞"不可能再各说各话。
                    action = sess.cycle_action(self.peer_ready(peer))

                    if action == punch_flow.CYCLE_KEEP:
                        continue

                    if action == punch_flow.CYCLE_RESTART:
                        before = sess.generation
                        sess.restart()
                        self.log(
                            f"[direct] new connection cycle for {peer} "
                            f"(generation {before} -> {sess.generation}): "
                            f"walking §E from the first stage again"
                        )
                    else:
                        sess.start()

                if not peers:
                    # 还没有对端：这是"点了开始连接、等另一个人进来"的那段。
                    # 每 0.5 秒看一次花名册；对方一点开始连接，这一轮就开打。
                    #
                    # 用户要求（2026-09-27）：这一段在界面上必须是
                    # 「等待对端用户加入…」——**静止文案、不倒数、不正计时**。
                    # 以前这里什么阶段都不写，于是界面一直挂着上一轮的
                    # "等待同步起降（123s）"，看起来像卡死在同步上。
                    #
                    # 基线 §B 的两种"没有目标"要分得开（用户明确要求两句不同的
                    # 提示）：没人认领 Host vs 有 Host 但还没开服。
                    if self.target_reason == "no_host":
                        self._set_phase(self.PHASE_NO_HOST)
                    elif self.target_reason == "host_not_ready":
                        self._set_phase(self.PHASE_HOST_NOT_READY)
                    elif self.target_reason == "host_not_in_roster":
                        self._set_phase(self.PHASE_CONFIRM_HOST)
                    elif self._has_other_members():
                        # ★ 用户 2026-09-28 第 5 条：房间里**有人**，只是他还没点
                        # 开始连接 —— 这必须是「等待对端连接…」，不是"等人加入"
                        # 更不是上一轮残留的"等待同步起跑"。
                        self._set_phase(self.PHASE_WAITING_FOR_PEER_CONNECT)
                    else:
                        self._set_phase(self.PHASE_WAITING_FOR_PEER)

                    await asyncio.sleep(0.5)

                    # 顺便把已经不在花名册里的对端清掉（对方退了房间/断了），
                    # 只断那一条，不影响还在的人。
                    #
                    # ★ 但**只在"房间里真的没有别人"时**才允许清空：换主机位、
                    # 主机还没开服、花名册还没刷新这些都是**短暂**状态，不是
                    # "对端走了"。在这里清空会把一条活着的直连拆掉 ——
                    # 那就违反了基线 §D"新人加入/短暂抖动不得影响已打通"。
                    #
                    # 显式传 `set()` = "确定房间里没有别人了"（no_peer / no_room
                    # 两个 reason 本身就是这个意思）。默认的 `_sweep_links()`
                    # 走中继花名册，不会拿目标表当花名册（隐患 3）。
                    if self.target_reason in ("no_peer", "no_room"):
                        self._sweep_links(set())
                    else:
                        # 还有别人、只是没人开始连接（例如 `started=` 空了 / 对端
                        # 全员 idle）：**也要**跑一次清理，否则那些"打了一半就
                        # 不动了"的状态机会永远留着（用户第 1 条 / 第 7 条的
                        # 分母问题）。默认路径只读中继花名册 + `conn=idle`，
                        # 绝不拿目标表当花名册，所以不会误拆。
                        self._sweep_links()

                    continue

                state = self.engine.room_state()
                me = getattr(state, "client_id", "") or ""

                progressed = False

                # ONE PUNCH PER PEER. The host serves every guest, and each one
                # needs its own hole; an already-punched peer is skipped (a new
                # punch would open a NEW socket and lose the live one).
                #
                # A newly joined guest is simply a peer that is not ready yet:
                # this loop punches with them WITHOUT touching the links that
                # are already carrying other players.
                for peer in peers:
                    if self.peer_ready(peer):
                        continue

                    try:
                        ok = await self._punch(room, me, peer)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.attempts += 1
                        self.last_reason = f"{type(e).__name__}: {e}"
                        self.log(f"[direct] room punch failed ({peer}): {self.last_reason}")
                        ok = False

                    # ================================================================
                    #  §F：这一轮没成 -> **Host 开新的一轮**（Player 跟随）
                    # ================================================================
                    #
                    # 用户 BUG 6 的原话是"多次打洞的发包/重试频率不一致"。
                    # 根因不是间隔数字，而是**两端各自决定"现在是第几轮"**：
                    # 两边按自己的循环计数，越走越岔，窗口就再也没对齐过。
                    #
                    # 所以轮次由 **Host 单方面仲裁**（星形拓扑里它是锚）：
                    # Host 每失败一轮就把 batch+1，下一次信标带着新 att/batch
                    # 出去；Player 收到对不上的轮次就作废自己那一轮、采纳 Host 的
                    # （`adopt_batch` → `SyncMismatch` → `note_round_failed`）。
                    # 两端因此**永远在同一个轮次上**，频率也就一致了。
                    if not ok:
                        sess = self.sessions.get(peer)

                        if sess is not None and self._am_host():
                            sess.next_batch()
                            self.log(
                                f"[direct] host starts attempt "
                                f"{sess.attempt_id} for {peer}"
                            )

                    if ok:
                        entry = self.links.get(peer) or {}
                        addr = entry.get("addr") or ("?", 0)
                        used = self.attempts + 1

                        self.log(
                            f"[direct] ROOM LINK UP: peer={addr[0]}:{addr[1]} "
                            f"(id={peer}) after {used} attempts "
                            f"(kept; a new punch would open a NEW socket and lose it)"
                        )

                        # ============================================================
                        #  打通了就把尝试计数归零（用户 2026-09-24 报的）
                        # ============================================================
                        #
                        # 以前这个计数从会话开始就一直往上加，**通了也继续加**，
                        # 下一次连接看到的是"第 5 次尝试""第 6 次尝试" —— 用户
                        # 的原话："不是从第一次开始，而是从上一次成功的那一次
                        # 继续往上累计，这不对"。数字要描述**这一次**用了多久，
                        # 不是历史总和。
                        self.attempts = 0
                        self.last_reason = ""
                        progressed = True

                        # ============================================================
                        #  3.0.3: 打通了就【立刻】建长连接隧道，不等任何游戏会话
                        # ============================================================
                        #
                        # His flow, ported: after the punch hit, the UdpTunnel is
                        # built and its HELLO handshake completes RIGHT THEN --
                        # the direct link is a long-lived channel between host and
                        # this player, and Minecraft connections (whenever they
                        # come, from whichever process) only attach as `sid`
                        # streams on top of it.
                        #
                        # It is a background task on purpose: the loop must stay
                        # free to punch the NEXT guest without waiting for this
                        # handshake (host + 3 players = 3 handshakes, all in
                        # parallel, none of them touching the others' sockets).
                        self._open_tunnel(peer)

                if progressed:
                    self._established = True

                # 判死 + 清理：只处理"死了/走了"的那一条，绝不动活着的。
                #
                # ★ 隐患 3：这里**不传目标表**（`set(peers)`）。目标表会因为
                # `started=` 抖一下而短暂少一个人，而它不是"谁还在房间"的事实；
                # `_sweep_links()` 默认去读中继花名册（`RoomState.peer_ids`）。
                self._sweep_links()

                # Keep looping: a new guest may join at any time, and a link
                # that died must be rebuilt. `start()` refuses to run a second
                # loop, and an already-punched peer is skipped above, so this is
                # not the punch storm that used to happen.
                #
                # ★ 用户 2026-09-28 第 8 条（握手速度）：睡这 3 秒的时候，
                # 只要"目标表变了"（有人进房 + 点了开始连接 / 中继的 started=
                # 冒出新的人 / 对端发来第一条信标）就**立刻醒**去开打，而不是
                # 硬等满 3 秒。重试节奏本身一个字没变（没人来还是 3 秒一轮），
                # 变的只是"人来了那一拍"的反应时间。
                await self._sleep_or_wake(peers)
        except asyncio.CancelledError:
            pass

    async def _sleep_or_wake(self, peers, interval=None, slice_s=0.25):
        """
        等价于 `sleep(RETRY_INTERVAL_S)`，但目标表一变就提前返回。

        判据只有一条：**当前目标表出现了新的 peer**（`set(新) - set(旧)` 非空）。
        那正是"有人刚点了开始连接"的时刻 —— 早 2 秒开打，用户就能早 2 秒看到
        「测量 NAT 类型中…」，而不是对着「等待对端连接…」发愣。
        """
        interval = RETRY_INTERVAL_S if interval is None else float(interval)
        before = set(peers or ())
        deadline = time.monotonic() + interval

        while time.monotonic() < deadline:
            if self._stop.is_set():
                return

            await asyncio.sleep(min(slice_s, max(0.01, deadline - time.monotonic())))

            with contextlib.suppress(Exception):
                fresh = set(self._peers())

                if fresh - before:
                    self.log(
                        "[direct] a new target appeared while waiting; "
                        "starting the next punch round immediately"
                    )
                    return

    # ---- 隧道与寿命 ----

    def _open_tunnel(self, peer):
        """
        为一个已经打通的 peer 建立长连接隧道（后台任务，幂等）。

        就是他那句 "direct UDP tunnel ready"：HELLO/HELLO_ACK 握手 + 读循环 +
        保活 + sid 分用，全部在**这一刻**起来 —— 游戏会话还没有、也不会有的
        时候，隧道已经在跑了。游戏来了只是往它上面挂一条 sid 流。
        """
        async def run():
            entry = self.links.get(peer)

            if entry is None:
                return

            punch_link = entry.get("link")

            if punch_link is None:
                return

            carrier = self._carriers.get(peer)

            if carrier is None or carrier.stop.is_set():
                try:
                    # `entry["addr"]` = the address THIS punch hit (may be the
                    # peer's LAN address); `punch_link.peer_addr` is only what
                    # the peer advertised. See PeerLink.__init__.
                    carrier = direct_carry.PeerLink(
                        peer, punch_link, log=self.log,
                        peer_addr=entry.get("addr"),
                    )
                except Exception as e:
                    # A punch link in a shape we cannot build a tunnel on is NOT
                    # a reason to throw the hole away -- the hole itself may be
                    # perfectly good (and in unit tests the link is a stub).
                    # Log it and leave the link as "punched, kept warm".
                    self.log(
                        f"[direct] cannot build a tunnel for {peer}: "
                        f"{type(e).__name__}: {e}"
                    )
                    return

                self._carriers[peer] = carrier

            # The host's world address, applied to EVERY link -- a late joiner's
            # link must serve its sid streams too (see `set_backend`).
            if self._backend is not None and self._backend != carrier._backend:
                carrier.set_backend(self._backend[0], self._backend[1])

            # ================================================================
            #  ★ 隐患 4：握手失败**不等于**洞死了 —— 同一个 socket 上有界重试
            # ================================================================
            #
            # 旧代码只重试一次，两次失败就 `_drop_peer()` 重打 —— 重打 = 新
            # socket = 新 NAT 映射，把一条**已经打通**的洞扔掉；在弱网/高延迟
            # 链路上这就是用户看到的又一次"打通又断"。
            #
            # 现在的规矩：
            #   * 在**同一个 socket**（同一个 `carrier`）上有界重试 + 退避；
            #   * 只有 socket 真的没了（`_link_socket_dead`：已关 / 引用清空）
            #     才允许重打；
            #   * 重试用尽但 socket 还活着 -> **不重打**，把映射留着；到点由
            #     `_sweep_links()` 的 `TUNNEL_SETUP_DEADLINE_S` 兜底重打。
            attempts = max(1, int(TUNNEL_HANDSHAKE_ATTEMPTS))
            backoff = float(TUNNEL_RETRY_BACKOFF_S)
            ok = False

            for attempt in range(1, attempts + 1):
                if carrier.stop.is_set():
                    # 隧道已经被别的路径关掉了（close/drop 半路）：什么都别做。
                    return

                try:
                    ok = await carrier.ensure_started(CONNECT_TIMEOUT_S)
                except Exception as e:
                    ok = False
                    self.log(
                        f"[direct] tunnel start failed for {peer} "
                        f"(attempt {attempt}/{attempts}): "
                        f"{type(e).__name__}: {e}"
                    )

                if ok:
                    break

                if self._link_socket_dead(punch_link):
                    self.log(
                        f"[direct] tunnel handshake failed for {peer} and its "
                        f"punched link is closed; re-punching"
                    )
                    self._drop_peer(peer)
                    return

                if attempt < attempts:
                    self.log(
                        f"[direct] tunnel handshake attempt {attempt}/{attempts} "
                        f"failed for {peer}; retrying on the SAME punched socket "
                        f"(a new socket would be a new NAT mapping)"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2.0

            if ok:
                self.log(
                    f"[direct] direct UDP tunnel ready, peer {peer} -- "
                    f"the link is UP and stays up until this host stops"
                )
                return

            # 握手一直没成，但洞还活着：**留着映射**，不重打。判死由
            # `_sweep_links()` 的 `TUNNEL_SETUP_DEADLINE_S` 兜底（到点才重打）。
            self.log(
                f"[direct] tunnel handshake still failing for {peer} after "
                f"{attempts} attempts, but the punched mapping is alive; keeping "
                f"the SAME socket (setup deadline {TUNNEL_SETUP_DEADLINE_S:.0f}s)"
            )

        # `ensure_started` 在别的线程里阻塞握手（to_thread），所以这里是任务，
        # 不是 await：打洞循环绝不能为一条握手而停下打下一个人的洞。
        self._tasks.add(asyncio.create_task(run()))

    @staticmethod
    def _link_socket_dead(link):
        """
        这条洞的打洞 socket 是不是**真的**没了（链路已经被关掉）？

        ============================================================================
          ★ 隐患 4 的判据：只有它为真才允许"重打"
        ============================================================================
        重打的代价是"新 socket = 新 NAT 映射"：一条**打通了**的洞会被扔掉，在
        弱网/高延迟上表现就是"打通又断"。所以握手失败时先问这一句。

        判据是 `DirectLink.closed`（`close()` 里唯一会把 `punched_sock`/`sock`
        置空、并把 `_closed` 置真的地方）—— **不是**"`sock` 是不是 None"：
        真实链路在打通那一刻一定握着命中 socket，而测试替身 / 还没建 socket
        的链路 `sock is None` 并不代表它已经被关掉。
        """
        if link is None:
            return True

        return bool(getattr(link, "closed", False))

    def _room_roster(self):
        """
        中继花名册（"房间里还有谁"，`RoomState.peer_ids`）—— 隐患 3 的判据。

        返回 `(known, ids)`：

          * `known=True`  —— `ids` 就是此刻还在房间里的其他成员（中继事实）；
          * `known=False` —— 花名册未知（不在房间 / 老中继没有 `peers=`），
            **不许**据此判定"对端走了"（当成"走了"会把好洞拆掉）。
        """
        with contextlib.suppress(Exception):
            state = self.engine.room_state()

            if state is None:
                return False, set()

            raw = getattr(state, "peer_ids", None)

            if raw is None:
                # 老中继只给 `members=<n>`，没有花名册 —— 未知。
                return False, set()

            me = str(getattr(state, "client_id", "") or "")

            return True, {str(p) for p in raw if p and str(p) != me}

        return False, set()

    def _link_overdue(self, peer):
        """
        这条洞打通之后，隧道在 `TUNNEL_SETUP_DEADLINE_S` 之内都没起来 —— 判死。

        隐患 5：保活线程只证明映射还在，不证明隧道能用；没有这条截止时间，
        "隧道一直没起来"的洞永远不会被重打。
        """
        entry = self.links.get(peer) or {}
        ready_at = entry.get("ready_at")

        if ready_at is None:
            return False

        return (time.monotonic() - float(ready_at)) >= TUNNEL_SETUP_DEADLINE_S

    def _sweep_links(self, roster=None):
        """
        判死与清理：只动该动的那一条。

        ============================================================================
          ★ 隐患 3：判据是【中继花名册】，不是【这一轮的打洞目标表】
        ============================================================================
        `started=` 的一次抖动（中继重启一行广播 / 玩家断一下 PLAYER 连接）会让
        目标表短暂少一个人，而目标表**不是**"谁还在房间里"的事实 —— 拿它当
        花名册，就会把一条还活着的洞当成"对端走了"拆掉。所以：

          * `roster=None`（默认）-> 自己去读 `RoomState.peer_ids`（中继事实）；
          * `roster=set()`（显式）-> "确定房间里没有别人了"（no_peer / no_room
            路径），照旧全拆；
          * 花名册未知（不在房间 / 老中继）-> **不按"走了"拆**，只走判死。

        **只看真成员 id**：链接的 key 可能是本地别名（`host_name` / `guest-N`），
        别名不在 `peers=` 里 —— 只有"曾经出现在花名册里"的 peer 才允许用花名册
        判死（`_member_ids`），否则一条活着的洞会被别名判成"走了"。

        判死两条（都只拆那一条，别的玩家一条不动）：
          1. 隧道活着 -> `LINK_DEAD_S`（20s）没有数据 = 洞真的没了；
          2. 隧道一直没起来 -> `TUNNEL_SETUP_DEADLINE_S` 到点 = 放弃重打
             （隐患 5：旧代码在 `carrier is None or not carrier.started` 时
             直接 `continue`，永远不会判死）。
        """
        explicit = roster is not None

        if not explicit:
            known, roster = self._room_roster()

            if not known:
                roster = None

        for peer in list(self.links.keys()):
            if roster is not None and peer not in roster and (
                explicit or peer in self._member_ids
            ):
                # 真正的"对端离开房间"：显式说明"没人了"，或者它曾经是中继花名册
                # 里的成员 id 而现在已经不在。别名（host_name / guest-N）不走这条。
                self.log(
                    f"[direct] peer {peer} is gone from the room roster; "
                    f"dropping THIS link and forgetting that it ever started "
                    f"connecting -- other peers are untouched"
                )
                # ★ 用户第 6 条：他走了，"他曾经开始过"这条证据也要一起忘掉，
                # 否则他一回来（同 client_id）就立刻又被当成打洞目标。
                self._forget_heard(peer)
                self._drop_peer(peer)
                continue

            carrier = self._carriers.get(peer)

            if carrier is not None and carrier.stop.is_set():
                # 隧道已经被关掉了（close / drop 的半路）：这条洞不再可用。
                self._drop_peer(peer)
                continue

            if carrier is not None and carrier.started:
                idle = carrier.idle_for()

                if idle is not None and idle >= LINK_DEAD_S:
                    addr = (self.links.get(peer) or {}).get("addr") or ("?", 0)
                    self.log(
                        f"[direct] link to {peer} died (%.0fs silent, "
                        f"{addr[0]}:{addr[1]}); dropping THIS link and re-punching "
                        f"-- other peers are untouched" % idle
                    )
                    self._drop_peer(peer)

                continue

            # 隧道没起来（carrier 没有，或 `started` 一直是 False）：给它一个
            # 真正的截止时间，否则只要 DirectLink 的保活线程活着，这条洞就
            # 永远不会被重打（隐患 5）。
            if self._link_overdue(peer):
                self.log(
                    f"[direct] no tunnel came up for {peer} within "
                    f"{TUNNEL_SETUP_DEADLINE_S:.0f}s; dropping THIS link and "
                    f"re-punching -- other peers are untouched"
                )
                self._drop_peer(peer)

        self._prune_sessions(roster, explicit)

    def _prune_sessions(self, roster, explicit):
        """
        清掉**已经不在房间里的**对端留下的状态机（没有任何洞的那种）。

        ============================================================================
          ★ 用户 2026-09-29 第 7 条：「两个玩家被踢出房间后……房内仅两人打洞时
            仍显示打洞中 1/3」
        ============================================================================

        `_sweep_links()` 以前只遍历 `self.links`（**打通过**的洞）。一个"打了一半
        就离开房间"的玩家从来没有 links 条目，于是他的 `ConnSession` 永远留在
        `self.sessions` 里 —— 打洞进度（`flow_summary` 的 total）就永远多算他
        一个人。

        判据和 `_sweep_links` 的洞判死**完全一样**（中继花名册 + 只对真成员 id
        生效），所以 `started=` 抖一下绝不会误清；别名（`host_name` / `guest-N`）
        也不受影响。`explicit=True`（"确定房间里没有别人了"）时清空全部本地
        状态机，和洞的处理保持一致。
        """
        if roster is None:
            # 花名册未知（不在房间 / 老中继）：仍然按 `conn=idle` 清理一次
            # ——那是中继的硬事实，和花名册无关。
            self._prune_idle_sessions()
            return

        # ★ 先把"没有洞、也没有状态机"的对端也忘掉。
        #
        # 用户第 6 条的形状就是这一种：被踢的玩家**连接都没建起来**（没有 link、
        # 没有 session），唯一留下的痕迹就是 `_heard_started` / `_heard_at` /
        # 房间那份 `direct_rx_at`。只遍历 `links`/`sessions` 的清理看不到他，
        # 于是他一重新进房就立刻又被当成"已开始连接"的目标，§E 一轮一轮空转。
        for peer in set(self._heard_started) | set(self._heard_at):
            if peer in roster:
                continue

            if not (explicit or peer in self._member_ids):
                continue

            self.log(
                f"[direct] peer {peer} is gone from the room roster; "
                f"forgetting that it ever started connecting"
            )
            self._forget_heard(peer)

        for peer in list(self.sessions.keys()):
            if peer in self.links:
                # 有洞的走 `_drop_peer()` 那条路（它会把 session 一起收掉）。
                continue

            if peer in roster:
                continue

            if not (explicit or peer in self._member_ids):
                continue

            # ★ 不管有没有状态机，"曾经开始过"的证据都要忘掉（用户第 6 条：
            # 被踢的玩家重新进房后，Host 不该把他当目标自己打起来）。
            self._forget_heard(peer)

            sess = self.sessions.pop(peer, None)

            self.log(
                f"[direct] peer {peer} is gone from the room roster; "
                f"dropping its stale connection state"
            )

            if sess is None:
                continue

            with contextlib.suppress(Exception):
                sess.timer.finish()

        self._prune_idle_sessions()

    def _prune_idle_sessions(self):
        """
        中继**明确**说 `conn=idle` 的对端，其状态机也收掉（用户第 1 条的清理半边）。

        `conn=idle` = 这个人此刻没有任何游戏连接。对这种对端打洞永远不会有结果
        （他不回应任何信标），留着状态机只会让那一行挂在旧阶段上。**只清没有洞的**：
        已经打通的洞由判死 / 花名册负责，绝不因为"这一刻 conn 说 idle"就拆 —— 用户
        在游戏里时 `conn=connected`，但中途 `conn=` 抖动不该拆掉正在搬数据的洞。
        """
        state = None

        with contextlib.suppress(Exception):
            state = self.engine.room_state()

        if state is None:
            return

        # ★ 当前**真实目标**绝不清理：客户端刚点「开始连接」时会先上报
        # `STARTED 1`（于是 `started=` 里已经有他），PLAYER 连接要几十~几百毫秒
        # 之后才到中继 —— 那一瞬间 `conn=idle` 但目标是**有效的**，清了就把
        # 正在走的 §E 打断（两端阶段对不上）。判据只清"既不在目标表、中继又说
        # 他 idle"的那一条。
        targets = set()

        with contextlib.suppress(Exception):
            fresh, _reason = self._select_targets_now()
            targets = set(fresh or ())

        for peer in list(self.sessions.keys()):
            if peer in self.links or peer in targets:
                continue

            if not punch_flow.peer_is_idle(state, peer):
                continue

            sess = self.sessions.pop(peer, None)

            if sess is None:
                continue

            self.log(
                f"[direct] peer {peer} is idle (it is not connecting); "
                f"dropping its stale connection state"
            )

            with contextlib.suppress(Exception):
                sess.timer.finish()

    def rebuild_links(self, peers=None):
        """
        强制丢掉已经打通的洞，让打洞循环**重打**（用户 2026-09-30 第 1 条）。

        「闪断重连不再判断原通路是否可复用，而是所有重新连接的行为一律重建
        通道」：
          * `peers=None` -> **全部**（host 重连：重建到所有玩家的通路）；
          * `peers=[host_cid]` -> **只这一条**（player 重连：只重建到 host 的
            通路，其他玩家完全不受影响；原本空闲的玩家保持空闲 —— 这条方法
            只被"已经在重连"的那一方调用，空闲的人根本不会走到这里）。

        和 `_drop_peer()` 同一条拆除路径（一条一条拆，绝不波及没点名的 peer）。
        返回被丢掉的 peer 列表，方便日志/测试。
        """
        if peers is None:
            wanted = None
        else:
            wanted = {str(p) for p in peers if p}

        dropped = []

        for peer in list(getattr(self, "links", {})):
            if wanted is not None and peer not in wanted:
                continue

            self._drop_peer(peer)
            dropped.append(peer)

        if dropped:
            self.log(
                f"[direct] forced rebuild: dropped the punched link(s) for "
                f"{', '.join(sorted(dropped))} -- they will be re-punched "
                f"(a reconnect never reuses the old path)"
            )

        return dropped

    def _drop_peer(self, peer):
        """
        拆掉【一个】peer 的隧道与洞。绝不波及别的 peer。

        §F：这条连接的 Timer 也要一起**停止/清零/释放** —— 用户 BUG 8 的原话
        "真正重新连接时从几百秒累计值开始"就是因为旧的计时没人收。
        下一个目标走人 -> 逐目标那一行也删掉（不留空行，§G）。
        """
        carrier = self._carriers.pop(peer, None)

        if carrier is not None:
            with contextlib.suppress(Exception):
                carrier.close()

        entry = self.links.pop(peer, None)

        if entry is not None:
            link = entry.get("link")

            if link is not None:
                with contextlib.suppress(Exception):
                    link.close()

        # 这个 peer 不在了：它的状态机/timeout 一并释放。
        sess = self.sessions.pop(peer, None)

        if sess is not None:
            with contextlib.suppress(Exception):
                sess.timer.finish()

        self._punch_links.pop(peer, None)
        self._peer_attempts.pop(peer, None)

        if not self.links:
            self._established = False

    def peer_ready(self, peer):
        """Is there a live, kept-warm link to this peer?（判据同 `_link_alive`）

        ★ 2026-09-29：除了打洞层的保活，**已经被隧道接手的洞**同样算"可用"
        ——见 `_carried_alive()` 的说明（用户第 2 条：打通后 3 秒界面显示断了、
        实际连接是通的）。
        """
        entry = self.links.get(peer) or {}

        return self._link_alive(entry.get("link")) or self._carried_alive(peer)

    def _stun_port(self):
        """
        Which port to probe for STUN.

        Production puts the responder on 3478/3479 while the relay listens on
        19100. But the relay ALSO answers STUN on its own port (both are bound),
        so when the built-in pair is unreachable -- a test relay, or an operator
        who moved it -- the relay port is the fallback that still works.
        """
        from . import stun as stun_mod

        return stun_mod.DEFAULT_STUN_PORT

    async def _punch(self, room, me, peer):
        """
        一次完整的「交换候选 → 打洞」。

        用的是**成员控制面**（房间那条已存在的连接），所以不需要隧道、
        不需要游戏。中继只做转发，不参与任何判断。
        """
        from . import direct as direct_mod

        self.attempts += 1
        self._peer_attempts[peer] = self._peer_attempts.get(peer, 0) + 1
        #: 这一轮正在打这个 peer（`_adopt_peer_connected` 据此避免和喷洒抢 socket）。
        #: `getattr` 兜底：单测里有用 `__new__` 造出来的替身（不走 `__init__`）。
        if not hasattr(self, "_punching_peers"):
            self._punching_peers = set()

        self._punching_peers.add(peer)

        # ==================================================================
        #  §F：这条连接的独立状态机（**每轮开打前重新拉目标**由 `_run()` 保证）
        # ==================================================================
        sess = self._session(peer)

        if sess is not None:
            if not sess.timer.running and not sess.terminal:
                sess.start()

            # §E 的前两步：确认 Host → 确认目标。目标由 `select_targets` 从
            # 中继的**事实**里选出来（BUG 1 的修复点），这里只是把它记进状态机，
            # 于是逐目标行上能看到这两步真的走过了。
            sess.advance(Stage.CONFIRM_HOST)
            sess.advance(Stage.CONFIRM_TARGET)

        # ==================================================================
        #  ★ 重试要**复用同一个 DirectLink**（= 同一个 socket、同一份测量）
        # ==================================================================
        #
        # 用户实测的日志：每次重试都重新测一遍 NAT + 入站 + 交换候选，一轮要
        # 七秒多 —— 而对端那时可能已经又开始打了。7 秒一轮、每轮只同步 1.5 秒，
        # 两边的窗口几乎撞不上，所以"要七八次才连上"。
        #
        # 复用之后：NAT 有 30 秒缓存（`direct.NAT_CACHE_S`）、socket 映射不变
        # （对端瞄的端口也不会变）、入站探测只跑一次；一轮重试回到"广播 + 打"
        # 的一两秒量级。
        link = self._punch_links.get(peer)
        first_round = link is None

        if link is None:
            link = direct_mod.DirectLink(
                room,
                self.engine.server_host(),
                self.engine.server_port(),
                log=self.log,
                my_id=me,
                peer_id=peer,
                # 只有 host 给直连线加 `to=`（见 DirectLink._to_field 的说明）。
                # 第三人打不上洞的根因就是中继把 host 的候选转发给了最早加入者。
                is_host=bool(getattr(self.engine.room_state(), "is_host", False)),
            )

            # Same STUN port the session path uses (see DirectLink.stun_port):
            # the built-in STUN service and the TCP relay are not always the
            # same number, and probing the wrong one looks like "no public
            # endpoint".
            link.stun_port = self._stun_port()
            self._punch_links[peer] = link

        # §F：把"这一轮"（batch / att）挂到所有出站直连行上。两端因此能**量出**
        # 错位，而不是靠猜 —— 见 `punch_flow` 文件里"同步信标"那段说明。
        if sess is not None:
            link.flow_attempt = sess.attempt_id
            link.flow_batch = sess.batch

            # ★ 2026-09-29（用户："每一步本端完成即上报"）：§E 前两步也上报，
            # 对端据此把逐目标行对齐到同一格（只影响显示，见 `_broadcast_stage`）。
            await self._broadcast_stage(link, sess, Stage.CONFIRM_HOST)
            await self._broadcast_stage(link, sess, Stage.CONFIRM_TARGET)

        # 房间的 NAT 画像（RoomNatMonitor 在 STUN 房间里一直测）当"分类提示"用：
        # 够新 + 分类确定时，`measure()` 只在本 socket 上探一次端点，不再完整
        # 测第二遍（审计问题 7，省 4~6 秒）。拿不到 / 过期 -> 完整测量。
        with contextlib.suppress(Exception):
            profile = self.engine.room_nat_profile()

            if profile is not None:
                monitor = getattr(self.engine, "_nat_monitor", None)
                link.nat_hint = profile
                link.nat_hint_at = float(
                    getattr(monitor, "profile_at", 0.0) or 0.0
                )

        try:
            # ==============================================================
            #  ★ 阶段 3：测量 NAT 类型（§E；点「开始连接」之后才做）
            # ==============================================================
            #
            # `probe_inbound` asks the relay to fire a packet at OUR PUBLISHED
            # endpoint, so it needs that endpoint to exist. Without the
            # measurement first, `link.public` is still None and the probe
            # answers "no published endpoint to probe yet" -- which is exactly
            # why the owner's screen said 入站可达「未检测」 for the whole
            # session while the punch was demonstrably working.
            #
            # 审计问题 7：房间画像（RoomNatMonitor，同一个 NAT、另一个 socket）
            # 够新时，`measure()` 会复用它做分类、只在本 socket 上探一次端点，
            # 不再完整测第二遍（省 4~6 秒）。
            self._set_phase("测量 NAT 类型中…")
            self._advance_session(sess, Stage.NAT)

            # ★ 用户 2026-09-28 第 4 条：**先告诉对端"我在测 NAT"**，再开始测。
            # 对方在「等待同步起跑」里就能显示「等待对端测量 NAT 类型」，而不是
            # 因为拿不到信息干等；我这一侧 meanwhile 显示「测量 NAT 类型中…」。
            await self._announce_stage(link, sess, punch_flow.BEACON_NAT)
            self._attach_punch_steps(link, sess)

            try:
                await link.measure()
            except Exception as e:
                self.log(f"[direct] NAT measurement failed: {e}")

            # ==============================================================
            #  ★ 阶段 4：入站策略探测（§E；诊断，不改打洞走向）
            # ==============================================================
            #
            # 审计问题 7 想做的事是"别让入站探测挡在候选公布前面"。这里**仍然**
            # 是先后顺序，原因不是懒得改，而是实测代价：
            #
            #   * 让它和 `advertise()` 并发（或者和打洞并发），在本机
            #     `direct_multi_test` 的三人场景里，**房主的控制连接会被拖断**
            #     （中继日志 `[HOST ERROR] host control connection closed`），
            #     房间失去所有者，后加入的玩家直接进不来；把探测挪到打洞之后
            #     并发更糟：`UdpHub` 已经接管了打洞 socket，两个读者抢同一批
            #     数据报，`direct_reuse_test` 两条直连全部超时。
            #   * 也就是说：这是"拿成功率换时间"，审计里明说了不许这么做。
            #
            # 保留下来的加速是**不碰并发结构**的那几条：房间 NAT 画像复用
            # （省一次完整测量）、`_warm_relay_mapping` 不再等 STUN 答案
            # （省 2 秒）。候选公布仍然在测量之后第一时间发出去。
            #
            # 只跑第一轮：这个结论在几分钟内不会变，而它要花好几秒。
            #
            # ★ 用户 2026-09-28 第 3 条：**阶段不许跳**。以前这里把
            # `_advance_session(INBOUND)` 也放在 `if` 里面，于是第二轮的逐目标
            # 行会从 `测量 NAT 类型中…` 直接跳到 `上报测量结果`（少一格），
            # 两端看起来就是"阶段对不上"。现在：
            #   * **阶段**每一轮都按 §E 走一遍（NAT -> 入站 -> 上报）；
            #   * **探测本身**仍然只在结论未知 / 第一轮时真的发一次（它是
            #     诊断，重测结论不会变，纯浪费几秒）。
            self._set_phase("测量入站策略中…")
            self._advance_session(sess, Stage.INBOUND)
            await self._announce_stage(link, sess, punch_flow.BEACON_INBOUND)

            if first_round or self.inbound_state in ("", "unknown", None):
                try:
                    self.inbound_state = await link.probe_inbound()
                    self.inbound_note = link.inbound_note
                except Exception as e:
                    self.inbound_state = self.inbound_state or "unknown"
                    self.inbound_note = f"inbound probe failed: {e}"
            else:
                self.log(
                    "[direct] inbound policy already measured; keeping the "
                    "previous verdict (the stage is still walked)"
                )

            # ==============================================================
            #  ★ 阶段 5：上报测量结果 + 「等待同步起跑」（§E）
            # ==============================================================
            #
            # 用户对 §E 的确认回答：**先测完的一方等对端也测完测量**。
            # 只有一边测完就往下走 = 端口错位 + 长时间打不通（BUG 3 的原话）。
            #
            # 所以这里先广播 `DIRECT_HIT stage=measured`（带 att/batch），
            # 再等到对端也报了同一条为止；等不到（对端是老客户端 / 掉线）
            # 就走**有上限**的退路，超时即这一轮失败（作废重来，**不硬打**）。
            if sess is not None:
                sess.mark_measured(
                    nat=getattr(link.nat, "nat_type", None),
                    filtering=getattr(link.nat, "filtering", None),
                    inbound=self.inbound_state,
                )

            self._set_phase("上报测量结果")
            self._advance_session(sess, Stage.REPORT)
            await self._broadcast_stage(link, sess, Stage.REPORT)

            self._set_phase(punch_flow.STAGE_LABELS[Stage.SYNC_START])
            self._advance_session(sess, Stage.SYNC_START)

            # ★ 用户 2026-09-30（第 7 条）：这一段是"等两边都测完"，**有上限**
            # （`SYNC_START_WAIT_S`），所以逐目标行显示 0.1s 倒计时：
            # `等待同步起跑 9.8s · 对端正在测 NAT【名】N/C`。
            self._arm_countdown(sess, SYNC_START_WAIT_S)

            await self._broadcast_stage(link, sess, Stage.SYNC_START)

            synced = await self._wait_synced_start(link, sess)

            if not synced:
                self.last_reason = "sync start timed out (peer did not finish measuring)"

                # 一轮失败**不是**这次连接的终态：只清这一轮，表继续跑
                # （§F：清零只发生在终态，见 note_round_failed 的说明）。
                if sess is not None:
                    sess.note_round_failed(self.last_reason)

                return False

            # ==============================================================
            #  ★ 阶段 6：同步 candidate → 双方确认 candidate（§E）
            # ==============================================================
            self._set_phase("交换候选地址")
            self._advance_session(sess, Stage.CANDIDATE)
            await self._broadcast_stage(link, sess, Stage.CANDIDATE)

            # 出站行上的 att/batch 也可能因为对端的同步信标而变（Player 跟随
            # Host 的轮次），所以在真正发候选之前再挂一次。
            if sess is not None:
                link.flow_attempt = sess.attempt_id
                link.flow_batch = sess.batch

            addr = await link.advertise()

            if addr is None:
                self.last_reason = "no candidate exchanged"

                if sess is not None:
                    sess.note_round_failed(self.last_reason)

                return False

            if sess is not None:
                sess.mark_candidate(addr, port=getattr(link, "public", (None, None))[1])
                # `advertise()` 返回的就是**对端的**候选；`link.peer_addr` 是真实
                # DirectLink 顺手记下的同一份。两个都认（单测里的假链路只实现
                # 返回值那一半）。
                sess.mark_peer_candidate(
                    getattr(link, "peer_addr", None) or addr,
                    nat=getattr(link, "peer_nat", None),
                    filtering=getattr(link, "peer_filter", None),
                )
                sess.confirm_candidate()

            self._advance_session(sess, Stage.CONFIRM_CAND)

            # 双方确认：只有两边的 candidate 都在，才允许起降。
            if sess is not None and not sess.cand_confirmed:
                self.last_reason = "candidate not confirmed by both sides"
                sess.note_round_failed(self.last_reason)
                return False

            await self._broadcast_stage(link, sess, Stage.CONFIRM_CAND)

            # ==============================================================
            #  ★ 阶段 7：同步起降 + 打洞（§E 末段）
            # ==============================================================
            # 这里先写"等待对端起跑公告"（有对端、有上限的那一段）；
            # `punch.await_punch_start` 一旦收到对端公告就会把它换成带**倒计时**
            # 的"等待同步起降：X.Xs"，再之后是"打洞中（同步起跳）"。
            # 玩家原话（2026-09-27）：不能一上来就"等待同步起降"然后一直正计时。
            self._set_phase(self.PHASE_WAITING_FOR_ANNOUNCE)
            self._advance_session(sess, Stage.SYNC_GO)
            await self._broadcast_stage(link, sess, Stage.SYNC_GO)

            # §E 的最后一步：真的进入 `PUNCH`（以前这里**没有**推进 —— 阶段一直
            # 停在 `sync_go`，于是逐目标行在真正打洞时显示的是「等待发包起跑」，
            # 与事实不符。2026-09-29 的显示同步把它补上：阶段轴与真实动作对齐）。
            self._advance_session(sess, Stage.PUNCH)

            if sess is not None:
                # 「第 N 次」在**开打之前**就记，这一轮的行上才看得到当前轮次。
                sess.note_punch(1)

            await self._broadcast_stage(link, sess, Stage.PUNCH)

            # ================================================================
            #  ★ 2026-09-29：打洞（可能好几秒）期间也要**持续**读对端的阶段上报
            # ================================================================
            #
            # 两端的 `stage=` 信标是在同一段时间里互相到达的，而消费它们的地方
            # （`_note_heard_started` 的 peek）在 `_run()` 每轮开头才跑一次 ——
            # 如果整段 `attempt()` 都在 await 里，这一轮的行会一直停在"等对端
            # 起跑"，而对端其实已经在打洞了。那就是用户报的"两端显示不一致"。
            #
            # 所以这里把 attempt 包成任务，每 0.3s 探一次队列（`peek_direct` 是
            # 同步的"取出再放回"，和 `wait_direct` 在同一条事件循环线程上）。
            # **打洞本身一个字没改**：加的只是显示用的观察点。
            result = await self._attempt_with_stage_polls(
                link,
                timeout=PUNCH_BUDGET_S,
                per_ip_pool=bool(getattr(link.nat, "per_ip_pool", False)),
            )

            if result.ok:
                # Keep the link (it owns the socket; closing it closes the hole)
                # AND keep the hole warm from this instant until the room ends.
                #
                # This is the difference between "punched at 19:02" and "usable
                # at 19:05 when the user finally opened their world" -- which is
                # exactly the gap the owner hit.
                #
                # Stored PER PEER: a star topology has one hole per guest, and
                # keeping only the latest threw the earlier ones away.
                self.links[peer] = {
                    "link": link,
                    "addr": result.addr,
                    "ready_at": time.monotonic(),
                }
                self.last_reason = ""
                # 这个 peer 打通了：它的"第 N 次"归零（别的 peer 不受影响）。
                self._peer_attempts[peer] = 0
                link.start_keepalive()

                # ==========================================================
                #  §F 的 Timer 规则（BUG 8 的修复点）
                # ==========================================================
                #
                # 用户原话："一次连接（成功或失败都算终态）立即停止、清零、
                # 释放；下次重连从 0 重新计算、不保留'最终耗时'。"
                #
                # 所以**成功也走 finish()**：以前打通之后整轮的计时还在继续，
                # 下一次连接看到的是从几百秒累计值开始（BUG 8）。
                if sess is not None:
                    # ★ 用户 2026-09-28 第 2 条：打通那一行必须带上**打通的公网
                    # 地址:端口**（`P2P 已直连【玩家名】223.166.20.9:40879 18ms`）。
                    # 地址就是这一次真正打中的那一个（`result.addr`），不是对方
                    # 公告的候选地址 —— 两者在回环/局域网场景下不一样。
                    sess.finish(
                        True,
                        latency_ms=self._link_latency_ms(peer),
                        addr=(
                            f"{result.addr[0]}:{result.addr[1]}"
                            if result.addr else ""
                        ),
                    )

                self.log(
                    f"[direct] punched link to {peer} kept warm every "
                    f"{DIRECT_KEEPALIVE_S:.0f}s (it does not expire)"
                )

                # ==========================================================
                #  ★ 2026-09-29（P0 收敛）：**立刻把"我已直连"告诉对端**
                # ==========================================================
                #
                # 以前打通只写在本端状态机里，对端唯一的希望是它自己的
                # `link.attempt()` 也命中。两端重试节奏错开 / 非对称 NAT 时，
                # 它可能一直不命中 —— 用户在界面上看到的就是"一端已直连、
                # 另一端还在反复重试/测量/确认主机"。
                #
                # 对端收到 `stage=connected` 会走 `_adopt_peer_connected()`：
                # 在**同一个已打通的 socket** 上补保活 + 建隧道，并把那条连接
                # 记成已建立。真实打洞算法一个字没动。
                await self._broadcast_stage(link, sess, Stage.CONNECTED)

                return True

            self.last_reason = result.reason or "no packet arrived"

            # 一轮没打通：清这一轮、表继续（下一次重试还属于**同一次连接**）。
            if sess is not None:
                sess.note_round_failed(self.last_reason)

            return False
        except SyncMismatch as e:
            # §F：错位 -> 作废这一轮 -> 清理 -> 重新同步（**不许各自硬打**）。
            # `adopt_batch` 已经作废了状态机；这里只负责记账 + 落日志。
            self.last_reason = f"attempt mismatch: {e.field}"

            # `adopt_batch()` 已经作废了这一轮；这里只把这一轮清干净、表继续跑。
            # **不用 `restart()`**：错位不等于"这次连接结束了"，计时器不该清零
            # （§F 的 Timer 清零只发生在终态）。
            if sess is not None:
                sess.note_round_failed(self.last_reason)

            self.log(
                f"[direct] attempt mismatch with {peer} on {e.field} "
                f"(mine={e.mine!r} theirs={e.theirs!r}); voided and resyncing"
            )

            return False
        finally:
            # Clean up ONLY the link that failed AND is not the one we hold.
            #
            # The order matters: assigning `self.link = link` happens on success,
            # so `self.link is not link` is the precise test for "this attempt
            # did not become the link we keep". The previous version tested
            # `not self.ready`, which is also true for a *held but expired* link
            # -- so a re-punch could close the socket the game was using.
            if not any(e.get("link") is link for e in self.links.values()):
                with contextlib.suppress(Exception):
                    link.close()

            getattr(self, "_punching_peers", set()).discard(peer)

            # The link we KEEP must outlive this coroutine, and so must its
            # keepalive task: readiness is "punched AND kept warm", so a task
            # cancelled here would leave a live socket that nothing maintains.

    # --------------------------------------------------------
    # §E 中段的两个同步点（走中继已有转发关键字，不新增协议）
    # --------------------------------------------------------

    def _advance_session(self, sess, stage):
        """阶段推进（None 安全的薄包装，**绝不影响打洞本身**）。"""
        if sess is None:
            return False

        with contextlib.suppress(Exception):
            # 换阶段 = 上一段的倒计时作废（新的那一段会自己重新挂，见
            # `_arm_countdown`）。**纯显示**。
            if getattr(sess, "stage", None) != stage:
                sess.clear_countdown()

            return sess.advance(stage)

        return False

    def _arm_countdown(self, sess, seconds, label=""):
        """
        给这条目标状态机挂一个 0.1s 倒计时（**纯显示**，用户第 7 条）。

        `seconds` 是"还要等多久"；`label` 缺省就用它当前阶段的话术。
        """
        if sess is None:
            return None

        with contextlib.suppress(Exception):
            until = time.monotonic() + float(seconds)

            return sess.set_countdown(until, label=label)

        return None

    def countdown_active(self):
        """
        现在有没有任何一条目标在倒计时？（界面据此把重画频率提到每 150ms）
        """
        for sess in list(getattr(self, "sessions", {}).values()):
            with contextlib.suppress(Exception):
                if sess.countdown_text() is not None:
                    return True

        return False

    async def _attempt_with_stage_polls(self, link, timeout=None, per_ip_pool=False):
        """
        `link.attempt(...)` 的薄包装：**执行完全相同**，只是在等待期间每 0.3s
        同步地"看一眼"对端的阶段上报（`_note_heard_started` 里的 peek）。

        为什么需要它：`attempt()` 可能要跑好几秒（PUNCH_BUDGET_S），而这正是两端
        互发 `stage=` 信标的时候。消费点只在 `_run()` 每轮开头，于是整段 attempt
        里当前这一轮的行会停在上一格（"等待对端起跑"），而对端早就在打洞了 ——
        用户看到的"两端显示不一致"。

        它**绝不影响打洞**：`attempt()` 的入参、超时、返回的 `PunchResult` 全部
        原样透传；多出来的只有事件循环上的 peek。
        """
        attempt_task = asyncio.ensure_future(
            link.attempt(timeout=timeout, per_ip_pool=per_ip_pool)
        )

        try:
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(attempt_task), timeout=0.3
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    with contextlib.suppress(Exception):
                        room = self.engine.room_current()

                        if room is not None:
                            self._note_heard_started(room)
        finally:
            if not attempt_task.done():
                with contextlib.suppress(Exception):
                    attempt_task.cancel()

    async def _broadcast_stage(self, link, sess, stage):
        """
        把"我到了这一阶段"发给对端（`DIRECT_HIT stage=<阶段> att=… batch=… from=…`）。

        ============================================================================
          ★ 用户 2026-09-29：两端共用一套阶段枚举 + 每一步完成即上报
        ============================================================================
        用户原话：「两端共用一套状态枚举，信令上报当前状态，等待对端状态匹配之后
        才允许进入下一阶段」—— 那一套枚举就是 `punch_flow.Stage`（沿用现有阶段
        名），承载它的关键字是中继**本来就逐字转发**的 `DIRECT_HIT`（见
        `punch_flow.beacon_line`：这不是新协议，是既有加法字段的又一种用法）。

        它**只上报**，不改任何真实迁移：`sess.report_stage()` 只记一个字段，
        对端那边由 `_note_heard_started()` 记进 `peer_seen` 供**显示**裁决
        （`ConnSession.display_stage`）。真实打洞该走哪一步，仍然只由 §E 的
        `advance()` / 各自的真实等待点决定（"等对端"绝不拖慢打洞）。
        """
        if sess is None or link is None:
            return False

        value = sess.report_stage(stage)

        if not value:
            return False

        room = getattr(link, "room", None)

        if room is None or not callable(getattr(room, "send_direct", None)):
            return False

        line = punch_flow.beacon_line(
            value,
            sess.attempt_id,
            sess.batch,
            getattr(link, "_to_field", lambda: "")(),
            from_id=getattr(link, "my_id", ""),
        )

        with contextlib.suppress(Exception):
            return bool(await room.send_direct(line))

        return False

    async def _announce_stage(self, link, sess, stage):
        """
        把"我现在在测量的哪一小步"发给对端（`DIRECT_HIT stage=nat|inbound`）。

        ============================================================================
          ★ 用户 2026-09-28 第 4 条：两端阶段必须能对得上
        ============================================================================

        用户原话：「本地测 NAT 和入站，我这边直接秒测完显示等待对端，但对端显示
        我正在测 NAT，我本地的测量给我也打印在状态里」。

        根因是**信息不对称**：这一步以前只在测量**全部做完**之后才发一条
        `stage=measured`，所以对端在整个测量期间只知道"对方还没测完"，无法显示
        "他在测哪一步"；而先测完的一方也只能干等。

        现在在进入 NAT / 入站测量**之前**各广播一条小步信标（走中继本来就逐字
        转发的 `DIRECT_HIT` 关键字，不新增协议），于是：

            我在测 NAT   -> 我显示「测量 NAT 类型中…」；对端显示「等待对端测量 NAT 类型」
            我在测入站   -> 我显示「测量入站策略中…」；对端显示「等待对端测量入站策略中」
            我测完了     -> 两端在「等待同步起跑」汇合（§E）

        任何失败都**只当没有这条信息**（老中继 / 没连上控制面）：显示退回基线
        原文「等待同步起跑」，绝不影响打洞走向。
        """
        if sess is None:
            return False

        sess.begin_measure(stage)
        # 2026-09-29：这一条也是"我到了哪一格"的上报（阶段轴的同一套枚举）。
        sess.report_stage(punch_flow.MEASURE_STEP_STAGES.get(stage, ""))

        room = getattr(link, "room", None)

        if room is None or not callable(getattr(room, "send_direct", None)):
            return False

        line = punch_flow.beacon_line(
            stage,
            sess.attempt_id,
            sess.batch,
            getattr(link, "_to_field", lambda: "")(),
            from_id=getattr(link, "my_id", ""),
        )

        with contextlib.suppress(Exception):
            sent = await room.send_direct(line)

            if sent:
                self.log(
                    f"[direct] told {sess.peer} we are measuring {stage} "
                    f"(attempt {sess.attempt_id})"
                )

            return bool(sent)

        return False

    async def _wait_synced_start(self, link, sess):
        """
        §E「等待同步起跑」：广播"我测完了"并**等到对端也测完**。

        返回 True = 两边都测完（可以继续）；False = 有上限的等待用完了。

        ============================================================================
         ★ 这里为什么必须"两边都测完才走"
        ============================================================================

        用户 BUG 3 的原话："点开始连接后才测量；测完先进入「等待同步起跑」
        （先测完的一方等对端也测完）；两边都同步后再「等待同步起降」，然后才
        Punch。流程不严格就会出现端口错位、长时间打不通。"

        端口错位的物理原因：打洞用的 socket 是在测量之后才定下来的（NAT 映射
        属于那个本地端口）。一方先进入打洞，它瞄的是对端**测量前**的旧端口；
        等对端测完换了端口，先走的那一方已经打偏了。

        ============================================================================
         超时怎么办：**不硬打**，把这一轮判失败（§F 的作废/重同步）
        ============================================================================

        对端可能是老客户端（根本不发这条信标）。那种情况下继续等就是永远卡住，
        所以给上限；超时后这一轮失败、下一轮重来 —— 基线 §E 末句"任何一步双方
        状态不一致 → 不允许继续"，宁可重来也不带着错位的状态往下打。
        """
        room = getattr(link, "room", None)

        if room is None or sess is None:
            # 没有房间控制面（单测里的假链路）：没有可同步的对象，直接过。
            return True

        # 老 / 简化版房间对象（单测的 FakeRoom）根本没有这套协调 API：
        # 没有可同步的对象，直接过 —— **不能**把它当成"对端不说话"而空等。
        if not callable(getattr(room, "wait_direct", None)) or not callable(
            getattr(room, "send_direct", None)
        ):
            return True

        to_field = link._to_field()
        attempt_id = sess.attempt_id
        batch = sess.batch

        deadline = time.monotonic() + SYNC_START_WAIT_S
        announced_at = 0.0

        while time.monotonic() < deadline:
            now = time.monotonic()

            # 周期重播：晚点进来的那一边也能立刻拿到我们的起跑线（BUG 3/6 的
            # "两边窗口几乎不重叠"就是靠这个消掉的）。
            if now - announced_at >= BEACON_REANNOUNCE_S:
                announced_at = now
                sess.report_stage(Stage.SYNC_START)

                with contextlib.suppress(Exception):
                    await room.send_direct(
                        punch_flow.beacon_line(
                            punch_flow.BEACON_MEASURED, attempt_id, batch,
                            to_field, from_id=link.my_id,
                        )
                    )

            raw = None

            with contextlib.suppress(Exception):
                # 同时盯着候选行：**老客户端不发同步信标**，但它测完就会发
                # `DIRECT_CAND` —— 收到候选本身就是"它测完了"的证据（候选是在
                # 测量之后才公布的）。不认这条，老客户端上每一轮都要白等 12 秒。
                raw = await room.wait_direct(
                    (punch_flow.BEACON_KEYWORD, "DIRECT_CAND"),
                    timeout=min(BEACON_REANNOUNCE_S, max(0.1, deadline - now)),
                )

            if raw is None:
                continue

            if raw.split()[0:1] and raw.split()[0].upper() == "DIRECT_CAND":
                # 放回队列给 `advertise()` 用（`wait_direct` 对不匹配的关键字
                # 会自己放回，这里匹配到了要手动放回）。
                with contextlib.suppress(Exception):
                    room._direct_lines.put_nowait(raw)

                sess.mark_peer_measured()

                self.log(
                    f"[direct] sync start: {sess.peer} already published its "
                    f"candidate, so it has finished measuring (no beacon needed)"
                )

                return True

            info = punch_flow.parse_beacon(raw)
            stage_value = info.get("stage") or ""

            # ================================================================
            #  ★ 2026-09-29：**任何**认得出的阶段上报都记进状态机
            # ================================================================
            #
            # 用户要求"每一步本端完成即上报、等对端也上报同一阶段"。对端上报的
            # 阶段全部由 `note_peer_stage` 收下（只影响**显示**），这里额外只对
            # 测量小步和 `measured` 做流程处理 —— 认不出的值等同于没有这条行。
            if stage_value:
                sess.note_peer_stage(stage_value)

            # ================================================================
            #  ★ 用户 2026-09-28 第 4 条：对端在测 NAT / 入站时，这里要显示出来
            # ================================================================
            #
            # 对端进入测量的每一小步都会广播一条（见 `_announce_stage`）。
            # 收到就记进状态机，于是逐目标行显示「等待对端测量 NAT 类型 /
            # 等待对端测量入站策略中」，**继续等**（它还没测完）。
            if stage_value in (
                punch_flow.BEACON_NAT, punch_flow.BEACON_INBOUND
            ):
                self.log(
                    f"[direct] sync start: {sess.peer} is measuring "
                    f"{stage_value}; waiting for it to finish"
                )

                continue

            if stage_value != punch_flow.BEACON_MEASURED:
                # 不是同步信标（旧语义的 DIRECT_HIT"我打穿了"）：忽略，
                # **不能**因为收到它就以为对端测完了。
                continue

            # ================================================================
            #  ★ "我测完了"这件事**不属于某一轮**（2026-09-28 修死锁）
            # ================================================================
            #
            # 一开始这里还要求"对端报的批次必须和我一致"，否则作废重来。那是错的，
            # 而且会死锁：两端的重试节奏是各自 12 秒一轮，像两个节拍器，很容易
            # **永远错开** —— 每次都因为"对方报的是上一轮"而作废，谁都进不到
            # 候选交换（实测：Host 第 10 轮 / Player 第 6 轮，洞永远打不起来）。
            #
            # §F 要求严格同步的是 **attempt/轮次/发包**（Punch 那一阶段），而
            # "我已经测完了"是一个**与轮次无关的事实**：对端 20 秒前测出的 NAT
            # 结果，今天依然成立。所以这里只记"他测完了"，**轮次同步交给
            # candidate / DIRECT_PUNCH 那两条自带 `att=`/`batch=` 的行**
            # （`adopt_batch` 仍旧在那里把关）。
            sess.mark_peer_measured()

            self.log(
                f"[direct] sync start: both sides finished measuring "
                f"(peer={sess.peer} attempt={sess.attempt_id})"
            )

            return True

        self.log(
            f"[direct] sync start: peer {sess.peer} did not report its "
            f"measurement within {SYNC_START_WAIT_S:.0f}s; voiding this round "
            f"instead of punching out of sync"
        )

        return False

    def _link_latency_ms(self, peer):
        """这条直连的真实 RTT（毫秒），还没有就 None（界面显示 `-`）。"""
        carrier = self._carriers.get(peer)

        if carrier is None:
            return None

        with contextlib.suppress(Exception):
            return carrier.rtt_ms

        return None

    # --------------------------------------------------------
    # 实时阶段文案 + 入站探测的后台任务（审计问题 7）
    # --------------------------------------------------------

    def _attach_punch_steps(self, link, sess=None):
        """
        把 `link.step` 接到本协调器的阶段文案上。

        `direct.py::punch` 本来就会通过 `step=` 报"还有几秒开始"，但房间里
        从来没给它接过线（`link.step` 一直是 None）。现在接上，主行
        「打洞进度」就能显示协调起跑的倒计时。
        """
        if link is None:
            return

        # ★ 用户 2026-09-30（第 7 条）：这段倒计时挂到**逐目标行**上，这样
        # 状态机自己就能显示 `等待发包起跑 1.5s -> 1.2s -> 开打`。
        # `sess` 由 `_punch()` 直接传进来（同一个函数作用域里的那条状态机）。
        target = sess

        def report(text):
            text = str(text or "")

            # punch.py 报的是英文（日志口径，见 gui_i18n_test）。界面只拿它当
            # 一句英文短语；有中文阶段文案时以中文阶段为主。
            if text.startswith("waiting "):
                # `waiting 1.4s for the coordinated start` -> `等待发包起跑 1.4s`
                # （以前是 `等待同步起降：1.4s for the coordinated start`，
                # 中英混在一句里）。
                left = text.split(" ", 1)[1].split("s", 1)[0].strip()

                self._set_phase(f"等待发包起跑 {left}s")

                with contextlib.suppress(Exception):
                    if target is not None:
                        target.set_countdown(
                            time.monotonic() + float(left),
                            label="等待发包起跑",
                        )
            elif text.startswith("coordinated"):
                self._set_phase("打洞中（同步起跳）")

                with contextlib.suppress(Exception):
                    if target is not None:
                        target.clear_countdown()

        link.step = report

    def _start_ingress_probe(self, link):
        """
        把入站探测挂成一个**并发**任务，返回那个 task（调用方负责 await 它）。

        ============================================================================
          现状：`_punch` **刻意没有**调它（审计问题 7 的取舍，见那里的长注释）
        ============================================================================
        试过让入站探测和 `advertise()`（或打洞）并发，本机三人场景下房主控制连接
        被拖断、直连握手被抢包 —— 那是拿成功率换时间。所以它保留成一个可用的
        工具（给以后真正能安全并发的通道用），当前关键路径仍走顺序调用。

        它是**诊断**：绝不挡候选公布，也绝不改打洞走向。调用方在打洞开始之前
        await 它，是为了"一个 socket 只能有一个读者"（见 `_punch` 里的说明）。

        `direct.py::probe_inbound` 自己保证"永不抛异常"，这里再兜一层：
        任务里的异常不能安静地消失，至少要落一行日志。
        状态**不写占位值**：探测期间保持上一次的结论 / 空串（界面走它原来的
        「未测」文案），绝不发明一个"measuring"让人以为测过。
        """
        if link is None:
            return None

        task = self._ingress_task

        if task is not None and not task.done():
            return task

        self._set_phase("测量入站策略中…")

        async def run():
            try:
                state = await link.probe_inbound()
                self.inbound_state = state
                self.inbound_note = link.inbound_note
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.inbound_state = self.inbound_state or "unknown"
                self.inbound_note = f"inbound probe failed: {e}"
                self.log(f"[direct] inbound probe failed: {e}")

        self._ingress_task = asyncio.ensure_future(run())

        return self._ingress_task


__all__ = [
    "RoomDirectLink",
    "DIRECT_KEEPALIVE_S",
    "RETRY_INTERVAL_S",
    "LINK_DEAD_S",
    "TUNNEL_SETUP_DEADLINE_S",
    "TUNNEL_HANDSHAKE_ATTEMPTS",
    "TUNNEL_RETRY_BACKOFF_S",
]
