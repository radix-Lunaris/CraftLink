"""
CraftLink -- 打洞流程的**状态机**（设计基准 §D / §E / §F / §G）。

============================================================================
 这个文件为什么存在
============================================================================

用户 2026-09-27 的 8 条 BUG 里有 5 条（1/3/4/6/8）是同一个根因的不同表现：

    **打洞没有被建模成"每个 Host↔Player 一条独立、双端同步的状态机"。**

它以前是散在 `room_direct.py::_run()` 一个 while 循环里的过程式代码：

  * 目标是谁靠猜（`_host_client_id()` 取花名册第一个不是我的条目）——BUG 1；
  * 测量/候选的节奏由各自的循环决定，两边没有共同的"起跑"概念 ——BUG 3/6；
  * 计时是"整轮"的（`attempts` 通杀），没有"每个连接一套、终态清零" ——BUG 8；
  * 界面只能读到几个散字段，凑不出"逐目标阶段" ——BUG 3/4 的观感。

所以这里把它抽出来，做成**纯逻辑**（不碰 socket、不碰 asyncio、不看界面）：

    Stage        §E 的严格阶段序列（逐目标一行显示的就是它）
    ConnTimer    §F 的"每连接独立 + 一次使用后清零 + 重连从 0"
    ConnSession  §F 的每连接独立状态机 + 双端 attempt/batch/序号同步
    select_targets  §D 的"每轮重新确认目标"（player 只认 Host / host 只打已开始）
    parse_host_state 对中继新字段的**容错**解析（见文件末尾的"假设"）
    summary_text / row_text  §G 的三层文案（父级只汇总，行是逐目标）

设计原则：**这个文件不认识 UDP**。`room_direct.py` 负责把它的决定变成真实的
打洞动作；`punch_flow` 只负责"现在该处于哪一步、这一步该显示什么、什么时候
必须停下来重新同步"。这样 8 条规则里最容易回归的那几条（顺序、同步、计时、
目标、文案）全部可以离线钉死，不需要网络也能跑。
"""

import contextlib
import enum
import time

# ============================================================================
#  §E 阶段 —— 点「开始连接」之后必须**按这个顺序**发生
# ============================================================================
#
# 用户基线原文（DESIGN-BASELINE-20260927 §E）：
#
#     确认当前 Host → 确认当前目标 → NAT 测量 → 入站策略测量 → 上报测量结果
#     → 等待双方都完成（= 等待同步起跑）→ 同步 candidate → 双方确认 candidate
#     → 同步起跑 → 同步起降 → Punch → Connected / Failed
#
# 两条硬约束，缺一条这个顺序就没意义：
#
#   1. **测量发生在点「开始连接」之后**（不是打开软件就测）—— BUG 4。
#   2. 任何一步双方状态不一致 -> **不允许继续**，必须等待同步 —— §E 末句。
#
# 所以这里的 STAGE_ORDER 是**唯一**的顺序来源；任何"跳步"的调用都由
# `ConnSession.advance()` 拒绝并落日志（`advance()` 只允许往前走，或者
# `void_attempt()` 明确地整条作废重来）。


class Stage(str, enum.Enum):
    """一条 Host↔Player 连接的打洞阶段。"""

    IDLE = "idle"
    CONFIRM_HOST = "confirm_host"
    CONFIRM_TARGET = "confirm_target"
    NAT = "nat"
    INBOUND = "inbound"
    REPORT = "report"
    #: 先测完的一方等对端也测完测量（用户对 §E 的确认回答）。
    SYNC_START = "sync_start"
    CANDIDATE = "candidate"
    CONFIRM_CAND = "confirm_cand"
    #: 双方 candidate 都确认了，等约定的那个起跑时刻。
    SYNC_GO = "sync_go"
    PUNCH = "punch"
    CONNECTED = "connected"
    FAILED = "failed"


#: §E 的严格顺序。`IDLE` 在它前面，两个终态在它后面。
STAGE_ORDER = (
    Stage.IDLE,
    Stage.CONFIRM_HOST,
    Stage.CONFIRM_TARGET,
    Stage.NAT,
    Stage.INBOUND,
    Stage.REPORT,
    Stage.SYNC_START,
    Stage.CANDIDATE,
    Stage.CONFIRM_CAND,
    Stage.SYNC_GO,
    Stage.PUNCH,
)

#: 终态：到了就必须 **立刻** 停表、清零、释放（§F）。
#:
#: ============================================================================
#:  ★ 两个终态**不是一回事**（用户 2026-09-28 批准的隐患 1）
#: ============================================================================
#
# 旧代码只有一个 `TERMINAL_STAGES`，于是 `needs_new_cycle`（= 终态 or 表已释放
# or 还在 idle）对 **CONNECTED** 也为真 —— 一条**成功**建立的连接在下一轮
# （3 秒后）被自己 `restart()` 回 §E 第一步，界面从「P2P 已直连：1/1」变成
# 「直连尝试中... 0/1」。这正是用户报的「洞打通马上就断」（socket 没关，断的是
# "这条连接已建立"的账）。
#
# 所以两个终态必须分开：
#   * `SUCCESS_STAGES` —— 连接已建立，**不是**"要重开一次连接"；
#   * `FAILURE_STAGES` —— 这一次没成，下一次必须重开一轮。
# `terminal` 仍然是两者之和（终态不可逆、Timer 必须清零 —— §F 一个字没变），
# 但"要不要重开一次连接"只认 `failure_terminal`（成功终态由
# `cycle_action(link_alive)` 结合"洞还活着吗"判断）。
SUCCESS_STAGES = frozenset({Stage.CONNECTED})
FAILURE_STAGES = frozenset({Stage.FAILED})
TERMINAL_STAGES = SUCCESS_STAGES | FAILURE_STAGES

#: `ConnSession.cycle_action()` 的三个取值 —— 调用方**不需要**自己拼判据。
#: "这条目标下一轮该做什么"只有一个来源，绕不过去（用户要的"无歧义 API"）。
CYCLE_KEEP = "keep"
CYCLE_RESTART = "restart"
CYCLE_START = "start"

# ============================================================================
#  ★ 状态迁移表（用户 2026-09-28：「手动中断后重连流程有点乱，把顺序理顺」）
# ============================================================================
#
# 用户原话：「手动中断后重连流程好像有点小麻烦啊，每一步严格确认这采用握手的
# 方式我觉得可以，但流程似乎有点小混乱，你看流程可否优化一下」。
#
# 保留的策略：**每一步都严格握手确认**（§E），不做任何简化。
# 理顺的是迁移：一次连接（一个 attempt）里阶段**只许 +1 往前走**，不许跳步；
# 作废/重试/手动重连是三条**显式**的回起点边，且只有它们能回起点。
#
# 完整的迁移表（阶段 → 允许的下一阶段 → 触发条件）见
# `docs/CLIENT-PUNCH-FLOW-20260928.md`「状态迁移表」一节；这里的 TRANSITIONS
# 就是那张表的**唯一代码来源**，`tests/punch_flow_test.py` 会逐条比对，
# 文档和代码不许分叉。
#
#   * `None` 在终态那一行 = 没有下一阶段（终态不可逆）；
#   * 回起点的边不在表里 —— 它们由 `void_attempt()` / `note_round_failed()` /
#     `restart()` 三个**具名方法**完成（每一次都落日志），`advance()` 拒绝一切
#     回退与跳步。
TRANSITIONS = {
    Stage.IDLE: (Stage.CONFIRM_HOST,),
    Stage.CONFIRM_HOST: (Stage.CONFIRM_TARGET,),
    Stage.CONFIRM_TARGET: (Stage.NAT,),
    Stage.NAT: (Stage.INBOUND,),
    Stage.INBOUND: (Stage.REPORT,),
    Stage.REPORT: (Stage.SYNC_START,),
    Stage.SYNC_START: (Stage.CANDIDATE,),
    Stage.CANDIDATE: (Stage.CONFIRM_CAND,),
    Stage.CONFIRM_CAND: (Stage.SYNC_GO,),
    Stage.SYNC_GO: (Stage.PUNCH,),
    Stage.PUNCH: (Stage.CONNECTED, Stage.FAILED),
    Stage.CONNECTED: (),
    Stage.FAILED: (),
}

#: 「回起点」的三条边（文档用；代码里由三个具名方法实现）。
RESET_EDGES = (
    ("any", Stage.CONFIRM_HOST, "note_round_failed()：这一轮没打通，重来一轮"),
    ("any", Stage.CONFIRM_HOST, "void_attempt()：双端错位，作废这一轮（§F）"),
    ("any", Stage.CONFIRM_HOST, "restart()：手动停止后重新连接 = 新的一次连接"),
)


def allowed_next(stages):
    """`allowed_next(Stage.PUNCH) == (Stage.CONNECTED, Stage.FAILED)`。"""
    try:
        return TRANSITIONS.get(Stage(stages), ())
    except ValueError:
        return ()


#: 逐目标行开头显示的中文阶段名（§G 第 3 层）。
#:
#: 只写**打洞流程**的阶段；`IDLE` 不出现在行上（那是"还没开始"）。
STAGE_LABELS = {
    Stage.CONFIRM_HOST: "确认 Host",
    Stage.CONFIRM_TARGET: "确认目标",
    Stage.NAT: "测量 NAT 类型中…",
    Stage.INBOUND: "测量入站策略中…",
    Stage.REPORT: "上报测量结果",
    Stage.SYNC_START: "等待同步起跑",
    Stage.CANDIDATE: "同步 candidate",
    Stage.CONFIRM_CAND: "确认 candidate",
    # 用户 2026-09-29（显示升级）：`等待同步起降` -> `等待发包起跑`（同一阶段，
    # 阶段枚举与 TRANSITIONS 一个字没变，只改这一格显示用词）。
    Stage.SYNC_GO: "等待发包起跑",
    Stage.PUNCH: "打洞中",
    Stage.CONNECTED: "P2P 已直连",
    Stage.FAILED: "打洞失败",
}

# ============================================================================
#  ★ 用户 2026-09-29：「每一步本端完成即上报，等到对端也上报同一阶段才把显示
#    推进到下一阶段；对端没到就显示「等待对端…」」
# ============================================================================
#
# 真值来源：`stage=` 信标（走中继本来就逐字转发的 `DIRECT_HIT`，见
# `beacon_line`）。两端共用的一套枚举就是上面的 `Stage`（**沿用现有阶段名**），
# 信标里的 `stage=` 值就是 `Stage.value`。
#
# 显示规则（**只影响显示，绝不影响真实打洞**）：
#
#   我此刻在阶段 i（`stage_index`），对端最后上报的阶段是 j：
#       display = min(i, j)      —— 两人中走得慢的那一格 + 「等待对端…」
#   对端一次都没报过 -> display = 我自己的阶段（还没形成"谁在等谁"）。
#
# 这保证**两端显示的顺序与时刻基本一致**：谁也不会在对方还没到的时候把显示
# 独自往前推一格。真实阶段推进（`advance()`）不受它约束 —— 基线 §E 要求的
# 那几处等待（等待同步起跑 / 双方确认 candidate / 同步起降）另有各自的真实
# 等待点，一个字没动。
#
#: 等对端时逐目标行的说法（键 = **显示阶段**）。只用于"我在等对端"的那一格。
WAIT_LABELS = {
    Stage.CONFIRM_HOST: "等待对端确认 Host",
    Stage.CONFIRM_TARGET: "等待对端确认目标",
    Stage.NAT: "等待对端测量 NAT 类型",
    Stage.INBOUND: "等待对端测量入站策略中",
    Stage.REPORT: "等待对端上报测量结果",
    Stage.SYNC_START: "等待对端同步起跑",
    Stage.CANDIDATE: "等待对端交换候选",
    Stage.CONFIRM_CAND: "等待对端确认 candidate",
    Stage.SYNC_GO: "等待对端起跑",
    Stage.PUNCH: "等待对端打洞",
    Stage.CONNECTED: "等待对端确认已直连",
}

#: `stage=` 信标值 -> `Stage`（两端共用的那一套枚举；认不出的值一律忽略）。
STAGE_BY_NAME = {stage.value: stage for stage in Stage}

#: 「已直连」信标值 —— 打通那一侧必须把它发给对端（用户报的 P0：一端已直连、
#: 另一端还在反复重试/测量，就是因为它一直在等**自己**的 socket 命中）。
BEACON_CONNECTED = Stage.CONNECTED.value

# ============================================================================
#  ★ 用户 2026-09-28：「本机测量也要显示出来，两端阶段必须对得上」
# ============================================================================
#
# 用户原话：「本地测 NAT 和入站，我这边直接秒测完显示等待对端，但对端显示我正在
# 测 NAT，我本地的测量给我也打印在状态里」。
#
# 所以测量那一段的**双方视角**各有两种写法，逐目标行（§G 第 3 层）按"我自己在
# 测"还是"对端在测"分别显示：
#
#     我在测 NAT      -> `测量 NAT 类型中…`        （STAGE_LABELS[Stage.NAT]）
#     我在测入站      -> `测量入站策略中…`          （STAGE_LABELS[Stage.INBOUND]）
#     我在等对端测 NAT -> `等待对端测量 NAT 类型`
#     我在等对端测入站 -> `等待对端测量入站策略中`
#     两边都测完       -> `等待同步起跑`（基线 §E 的原词，不许改名）
#
# 后两条**不是新的阶段**：阶段仍然是基线 §E 的 `SYNC_START`（"先测完的一方等
# 对端也测完"），只是它等待期间**对端走到了哪一小步**由对端自己周期性广播
# （`DIRECT_HIT stage=nat|inbound|measured`，走中继本来就逐字转发的关键字）。
# 拿不到对端的小步（老客户端）就退回基线原文「等待同步起跑」。
PEER_MEASURING_NAT = "nat"
PEER_MEASURING_INBOUND = "inbound"
PEER_MEASURED = "measured"

#: 对端当前小步 -> 我这一行显示的等待文案。键是中继转发过来的 `stage=` 值。
PEER_STAGE_LABELS = {
    PEER_MEASURING_NAT: "等待对端测量 NAT 类型",
    PEER_MEASURING_INBOUND: "等待对端测量入站策略中",
}

#: 对端测量小步的**短**说法，跟在「等待同步起跑」后面当注脚（用户 2026-09-30：
#: 他既要知道"现在停在同步起跑这一步"，也要知道对端在测什么）。
PEER_STAGE_SHORT = {
    PEER_MEASURING_NAT: "对端正在测 NAT",
    PEER_MEASURING_INBOUND: "对端正在测入站",
}

#: 测量小步信标（`nat` / `inbound` / `measured`）在阶段轴上的位置：
#: 正在测 NAT = 站在 `NAT` 这一格；`measured` = 已经站在「等待同步起跑」。
MEASURE_STEP_STAGES = {
    PEER_MEASURING_NAT: Stage.NAT,
    PEER_MEASURING_INBOUND: Stage.INBOUND,
    PEER_MEASURED: Stage.SYNC_START,
}

#: 逐目标行上"还没建立连接"时延迟的写法（用户明确要求：未连接前延迟 N/C）。
LATENCY_NOT_CONNECTED = "N/C"

#: 逐目标行上"已连接但这一秒还没测出数字"的写法（口径同 app.peer_latency_text）。
LATENCY_UNKNOWN = "-"


def stage_index(stage):
    """阶段在 §E 顺序里的下标；终态/未知返回 -1。"""
    try:
        if stage in STAGE_ORDER:
            return STAGE_ORDER.index(stage)
    except ValueError:
        pass

    if stage == Stage.CONNECTED or stage == Stage.FAILED:
        return len(STAGE_ORDER)

    return -1


def attempt_label(index):
    """
    第 n 轮 attempt 的短标签 —— 逐目标行里的 `【A】`（§G）。

    A..Z，然后 A2..Z2、A3..（**绝不**变成 `AA`：那两个字母比数字更难一眼区分，
    而这一列的全部意义就是"两端的第几轮是不是同一个"）。
    """
    try:
        n = int(index)
    except (TypeError, ValueError):
        n = 0

    if n < 0:
        n = 0

    letter = chr(ord("A") + (n % 26))
    cycle = n // 26

    return letter if cycle == 0 else f"{letter}{cycle + 1}"


# ============================================================================
#  §F Timer —— 每个 Host↔Player 独立一套；一次使用（成功或失败）立即
#          **停止 / 清零 / 释放**；下次重连**重新创建**、从 0 开始。
# ============================================================================
#
# 用户原话（基线 §F）："一次使用后直接清零，不保留任何'最终耗时'"。
#
# 这条规则以前被违反得很彻底：`RoomDirectLink.attempts` 是**整轮**计数，一个
# peer 成功就归零（把别人的次数一起抹掉），而且**打通之后计时还在继续**，
# 于是"真正重新连接时从几百秒的累计值开始"（BUG 8 的原话）。
#
# 所以这里把计时和计数一起收进一个对象，并且规定：
#   * `stop()`  = 停表（保留到这一刻的数字，给日志用），**不计入下一次**；
#   * `release()` = 清零并释放全部字段（`.elapsed()` 从此恒为 0.0）；
#   * 终态走 `finish()` = stop + release，**一行日志拿到数字、对象当场变干净**。


class ConnTimer:
    """一条连接的计时器。**用过一次就释放**，重连时 new 一个。"""

    __slots__ = ("_clock", "_started_at", "_released", "_peak", "_rounds")

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._started_at = None
        self._released = False
        #: 已经释放之前跑过的最长时间（**只在 `finish()` 的返回值里出现一次**，
        #: 之后永远为 0）。保留它是因为日志需要"这一轮花了多久"，
        #: 但它是**只读的一次性快照**，不是"最终耗时"字段。
        self._peak = 0.0
        self._rounds = 0

    # ---- 读 ----

    @property
    def running(self):
        return self._started_at is not None and not self._released

    @property
    def released(self):
        return self._released

    @property
    def rounds(self):
        """这条连接一共开过几轮表。"""
        return self._rounds

    def elapsed(self):
        """这一轮已经跑了多久。没在跑 / 已释放 -> **0.0**（绝不返回残留值）。"""
        if self._started_at is None or self._released:
            return 0.0

        return max(0.0, self._clock() - self._started_at)

    # ---- 写 ----

    def start(self):
        """开始（或按"下一次重连"重新开始）。从 0 计。"""
        self._released = False
        self._started_at = self._clock()
        self._rounds += 1

        return True

    def stop(self):
        """
        停表并把数字交出去（给日志）。**不**释放 —— 调用方可能还要看一眼。
        数字交出去之后计时器本身已经归零，下一次 `start()` 从 0 开始。
        """
        value = self.elapsed()

        self._started_at = None

        if value > self._peak:
            self._peak = value

        return value

    def release(self):
        """清零 + 释放。释放后 `elapsed()` 恒为 0.0，`running` 恒为 False。"""
        self._started_at = None
        self._released = True

        return True

    def finish(self):
        """
        §F 的终态动作：**停表 + 清零 + 释放**，并返回这一轮的耗时。

        返回值只用于**那一刻**落一行日志（"耗时 X.Xs"）；它不是对象的状态 ——
        对象立刻变成"未运行、已释放、elapsed()==0.0"。用户要的正是这个：
        不保留任何"最终耗时"。
        """
        value = self.elapsed()

        if value > self._peak:
            self._peak = value

        self._started_at = None
        self._released = True

        return value


# ============================================================================
#  §F 一条 Host↔Player 连接的独立状态机
# ============================================================================


class SyncMismatch(Exception):
    """
    双端状态对不上（"Host 第 10 次 / Player 第 6 次"这种情况）。

    基线 §F 的处置是**唯一**的，而且**比"尽快打洞"更重要**：

        作废当前 attempt -> 清理 -> 重新同步 -> 同步起跑 -> 同步起降 -> 重新 Punch

    所以它**必须**是个异常（能被看见、能被测试钉住），不许"忽略一下接着打"。
    """

    def __init__(self, field, mine, theirs):
        self.field = str(field)
        self.mine = mine
        self.theirs = theirs

        super().__init__(
            f"attempt mismatch on {self.field}: mine={mine!r} theirs={theirs!r}"
        )


class ConnSession:
    """
    一条 Host↔Player 连接的全部独立状态（§F 逐字）。

    基线列出的独立字段在这里**一个不少**：session_id / attempt_id / candidate /
    NAT 状态 / 入站策略 / 同步状态 / punch 状态 / punch 次数 / batch /
    packet sequence / 当前端口 / punch interval / timer / connected / latency。

    `role` 是 "host" 或 "player"，决定谁是**这一轮的仲裁者**（见 `adopt_batch`）：
    星形拓扑里 Host 是每个 Player 的对端，两端各自算"用哪一轮"必然分叉，
    所以规定 **Host 定 batch、Player 跟随**；跟随方发现对不上就作废重来。
    """

    def __init__(self, peer, role="player", session_id="", log=None, clock=None,
                 label=""):
        self.peer = str(peer or "")
        self.role = "host" if str(role) == "host" else "player"
        self.session_id = str(session_id or "")
        self.log = log or (lambda *_a: None)

        #: 逐目标行里 `【…】` 那一格要显示的**对端真实名字**。
        #:
        #: ================================================================
        #:  用户 2026-09-28：「ABCD 是例子呀，谁让你用这个计次数了」
        #: ================================================================
        #: 设计文档里的 `【A】` 只是**占位示例**（那一格是"哪个目标"），不是
        #: 计数、更不是名字。所以那一格现在放中继广播的**显示名**
        #: （`names=`）；拿不到（老中继）才退回 client_id（`peer`）。
        #: 字符集来源唯一：`room_direct._session()` 从房间状态里取。
        self.label = str(label or "")

        #: 打通后的公网 `ip:port`（用户要逐行里带上"打通的地址:端口"）。
        #: 只在真的打通（`finish(ok=True, addr=...)`）之后才有值。
        self.addr = ""

        #: 这一条连接的**轮次**（第几次 attempt，0 起）。`attempt_label()` 把它
        #: 变成两端同步用的 `att=`（**只用于比对，不再上屏** —— 用户明确不要
        #: 字母计数）。
        self.batch = 0
        #: 双端必须一致的那几个数（§F 逐字）：attempt_id / batch / 序号 / 次数 /
        #: 端口 / 阶段。`attempt_id` 由 batch 派生，天然同步。
        self.packet_seq = 0
        self.punch_count = 0
        self.port = None
        self.punch_interval = 0.0

        #: 测量结果（每连接一份，**不许把一条目标的状态复用到另一条**）。
        self.my_nat = None
        self.my_filter = None
        self.peer_nat = None
        self.peer_filter = None
        self.inbound = ""

        #: 对端**此刻在测量的哪一小步**（`nat` / `inbound` / `measured` / 空）。
        #: 由对端周期性广播（`DIRECT_HIT stage=…`），只用于"等待对端测量 X"的
        #: 显示 —— 它不参与任何门禁（拿不到就是空，显示退回基线原文）。
        self.peer_stage = ""
        #: 本端此刻在测量的哪一小步（同样只用于显示；本端推进阶段时写入）。
        self.my_stage = ""

        # ================================================================
        #  ★ 用户 2026-09-29：两端共用一套阶段枚举 + 上报 + 等对端匹配
        # ================================================================
        #: 本端最后上报给对端的阶段（`stage=` 信标的值；"" = 还没报过）。
        self.reported_stage = ""
        #: 对端上报过的阶段（`stage=` 值 -> 首次看到的时刻）。**只增不减**，
        #: 直到新的一次连接（`restart()`）才清 —— 它是"谁走到哪一格"的事实。
        self.peer_seen = {}
        #: 对端明确报了"我已直连"（`stage=connected`）。这是 P0 的收敛依据：
        #: 一端打通后必须把这件事告诉对端，对端据此停止重试（见 room_direct）。
        self.peer_connected = False
        self.peer_connected_at = None
        self.peer_connected_addr = ""

        #: candidate
        self.my_candidate = None
        self.peer_candidate = None
        self.cand_confirmed = False

        #: 同步状态
        self.my_measured = False
        self.peer_measured = False
        self.peer_ready_at = None
        self.go_at = None

        #: ============================================================================
        #:  ★ 用户 2026-09-30（第三轮第 7 条）：起跑前的**相对倒计时**要能看见
        #: ============================================================================
        #: 真实流程一直有这一步（两边互发 `DIRECT_PUNCH startIn=1.5`、等到同一个
        #: 本地单调时刻再一起喷包 —— 见 `punch.await_punch_start`），但它只进日志
        #: 和房间级那一行；逐目标行（状态机）以前只有"进入本阶段后的**正计时**"，
        #: 所以用户看不到"1.5s -> 1.2s -> 开打"。
        #:
        #: `countdown_until` = 约定的那个时刻（本地单调钟）；`countdown_label` =
        #: 这一格显示用的话术（起跑前是「等待发包起跑」，等对端测量时是
        #: 「等待同步起跑」）。**纯显示**：不参与任何阶段推进或打洞决策。
        self.countdown_until = None
        self.countdown_label = ""

        #: punch 状态
        self.connected = False
        self.failed = False
        self.latency_ms = None
        self.fail_reason = ""
        #: 换模式等事件必须**整条重建**（§D）：这个标记让上层知道"别再复用"。
        self.generation = 0

        self.timer = ConnTimer(clock=clock)

        self.stage = Stage.IDLE
        self.stage_entered_at = 0.0
        self.voided = 0

    # ---- 标识 ----

    @property
    def name(self):
        """
        逐目标行 `【…】` 里的名字：中继广播的显示名优先，拿不到用 client_id。

        **绝不返回空**（空会让那一格变成 `【】`）：最后一个兜底是 `peer`，它
        本来就是"我们确实知道的身份"。
        """
        return str(self.label or self.peer or "")

    @property
    def attempt_id(self):
        """
        双端同步的 attempt 标识。

        由 **batch 派生**（`A`/`B`/`C`…）而不是各自随机生成 —— 两端各自
        random 一个 id 再比对，第一轮就必然不等（那正是"错位"的来源）。
        派生则只要 batch 一致，id 就一致。

        **它只出现在两端互发的 `att=` 字段里，绝不上屏**：用户 2026-09-28 明确
        说过 `【A】` 是设计文档的占位示例，"不许拿字母当计数/当名字"。
        """
        return attempt_label(self.batch)

    @property
    def attempt(self):
        """
        「第 N 次」——逐目标行 `打洞中【玩家名】第 3 次 · N/C` 里的那个 3。

        它是**这一轮内部**的发包次数（`punch_count`），不是历史总和。
        """
        return self.punch_count

    @property
    def terminal(self):
        """两个终态的**并集**：终态不可逆、Timer 必须清零（§F）。"""
        return self.stage in TERMINAL_STAGES

    @property
    def success_terminal(self):
        """**成功**终态：这条连接已经建立了（`CONNECTED`）。"""
        return self.stage in SUCCESS_STAGES

    @property
    def failure_terminal(self):
        """**失败**终态：这一次连接没成（`FAILED`），下一次必须重开。"""
        return self.stage in FAILURE_STAGES

    @property
    def needs_new_cycle(self):
        """
        下一条目标行要不要**重开一次连接**（而不是接着上一轮）。

        ============================================================================
          ★ "成功终态"不算"要重开"（用户 2026-09-28 批准的隐患 1 的语义修复）
        ============================================================================
        用户 2026-09-28：「手动中断后每个阶段都能重新走一遍并显示正确」。
        手动停止会让 `stop()` 走 `timer.finish()`（= 已释放），失败终态也已经
        结束 —— 这两种情况下一次「开始连接」必须是全新的一次连接：从 §E 第一
        步重新走，`generation` 递增、表从 0。

        但**打通**（`CONNECTED`）不是"要重开"：它意味着这条连接已经建立，
        抹掉它就是用户报的「洞打通马上就断」。所以这里先让成功终态短路返回
        False（`finish(True)` 也会释放 Timer，光看 `timer.released` 会误判）；
        洞真的死了要重打时，由调用方用 `cycle_action(link_alive=False)` 表达。
        """
        if self.success_terminal:
            return False

        return bool(
            self.failure_terminal or self.timer.released or self.stage == Stage.IDLE
        )

    def cycle_action(self, link_alive):
        """
        下一轮对这个目标**该做什么** —— 调用方照做即可，不必自己拼判据。

        返回值（见 `CYCLE_KEEP` / `CYCLE_RESTART` / `CYCLE_START`）：

          * `"keep"`    —— 连接已建立**而且洞还活着**：什么都别做（这是"打通
            之后不许自己把账抹掉"的落点）；
          * `"restart"` —— 必须开**新的一次连接**：失败终态 / 手动停止已释放 /
            阶段还停在起点 / 成功终态但洞已经死了；
          * `"start"`   —— 表还没跑过、也没到要重开的地步：`start()` 接着来。

        `link_alive` 由调用方给（`punch_flow` 不认识 socket）：读的是
        `DirectLink.keepalive_running`（隧道接手后由 `carried` 兜住）—— 也就是
        `RoomDirectLink.peer_ready(peer)`。判据只有这一个，所以"从任何调用方
        回来"都不可能再把一条活着的连接当成"要重开"。
        """
        if self.success_terminal:
            return CYCLE_KEEP if link_alive else CYCLE_RESTART

        if self.needs_new_cycle:
            return CYCLE_RESTART

        return CYCLE_START

    @property
    def stage_text(self):
        """
        逐目标行开头那句话（**显示用**）。

        三层取值（从上到下）：

          1. **在等对端**（`waiting_for_peer`）-> `WAIT_LABELS[显示阶段]`
             （用户 2026-09-29：「对端没到就显示「等待对端…」」）；
          2. `SYNC_START` 还没等到对端测量时，按**对端此刻在测哪一小步**显示
             「等待对端测量 NAT 类型 / 等待对端测量入站策略中」
             （见 `PEER_STAGE_LABELS`，2026-09-28 第 4 条）；
          3. 其余就是这个阶段的正式文案（`STAGE_LABELS`）。

        这些都只是同一个阶段的说法，**不是新阶段**：阶段序列（§E）和
        TRANSITIONS 一个字都没变。拿不到对端小步（老客户端）就显示基线原文。
        """
        stage = self.display_stage

        if stage == Stage.IDLE:
            return ""

        # `SYNC_START` 有它自己的两句等待文案（「等待同步起跑」/「等待对端测量 X」），
        # **不加** `等待对端` 前缀 —— 那两句本身就是基线 §G 的原文。
        if self.waiting_for_peer and stage != Stage.SYNC_START:
            wait = WAIT_LABELS.get(stage)

            if wait:
                return wait

        if stage == Stage.SYNC_START and not self.synced_start:
            # ============================================================================
            #  ★ 用户 2026-09-30（第三轮第 7 条）：基线那一步必须**看得见**
            # ============================================================================
            #
            # 旧行为：只要对端在测 nat/inbound，这一格就**只**显示
            # 「等待对端测量 NAT 类型 / 入站策略中」；基线的「等待同步起跑」只在
            # 对端报 `measured` 之后的几百毫秒里出现 —— 界面每秒才重画一次，
            # 于是用户"从来没看到过等待同步起跑"。
            #
            # 现在：基线原文（+ 0.1s 倒计时）**永远在**，对端在测什么作为注脚：
            #   `等待同步起跑 9.8s · 对端正在测 NAT【名】N/C`
            # 两句话是同一个阶段（§E 的阶段序列一个字没改）。
            base = STAGE_LABELS.get(stage, "")

            short = PEER_STAGE_SHORT.get(self.peer_stage or "")

            return f"{base} · {short}" if short else base

        return STAGE_LABELS.get(stage, "")

    def note_peer_stage(self, stage):
        """
        记下对端**此刻在测量的哪一小步**（`DIRECT_HIT stage=nat|inbound`）。

        **只影响显示**：它不参与 `synced_start` / 任何门禁（对端"测完了"这件事
        仍然只认 `stage=measured` 或它已经公布 candidate）。认不出的值一律忽略。

        ============================================================================
          2026-09-29：同一个入口现在也吃**整条阶段轴**的信标
        ============================================================================
        用户要求「每一步本端完成即上报，等到对端也上报同一阶段才把显示推进到下一
        阶段」。所以 `stage=` 里除了测量小步（`nat|inbound|measured`），还会有
        `confirm_host` / `confirm_target` / `report` / `candidate` / `confirm_cand`
        / `sync_go` / `punch` / `connected`。它们全部记进 `peer_seen`，由
        `display_stage` 统一裁决"显示停在哪一格、是不是在等对端"。

        **仍然只影响显示**：没有任何真实迁移读 `peer_seen`。
        """
        value = str(stage or "").strip().lower()

        if not value:
            return False

        known = value in PEER_STAGE_LABELS or value in MEASURE_STEP_STAGES \
            or value in STAGE_BY_NAME

        if not known:
            return False

        if value not in self.peer_seen:
            self.peer_seen[value] = time.monotonic()

        if value == BEACON_CONNECTED:
            self.peer_connected = True

            if self.peer_connected_at is None:
                self.peer_connected_at = self.peer_seen[value]

        if value in (PEER_MEASURING_NAT, PEER_MEASURING_INBOUND,
                     PEER_MEASURED) and value != self.peer_stage:
            # 测量小步还额外驱动「等待对端测量 X」那两句显示变体。
            self.peer_stage = value

        return True

    def report_stage(self, stage):
        """
        本端到达（或正在做）某一阶段 —— 记下来，等上层把 `stage=` 信标发出去。

        返回真正该写进 `stage=` 的值（`""` = 认不出的值，不报）。
        """
        value = stage.value if isinstance(stage, Stage) else str(stage or "").strip().lower()

        if value not in STAGE_BY_NAME:
            return ""

        self.reported_stage = value

        return value

    def _index_of(self, stage):
        """阶段在 §E 顺序轴上的下标（`IDLE`=0；终态 = 轴长）。"""
        try:
            stage = Stage(stage)
        except ValueError:
            return -1

        if stage in STAGE_ORDER:
            return STAGE_ORDER.index(stage)

        if stage in TERMINAL_STAGES:
            return len(STAGE_ORDER)

        return -1

    @property
    def peer_index(self):
        """
        对端走到 / 报到的**最远**那一格（没报过 -> None）。

        「对端没到」和「对端报了起点」是两件事：前者无法判断谁在等谁，后者
        说明它确实在跑（于是"我比它快"是一个确定的事实）。
        """
        best = None

        for value in self.peer_seen:
            stage = MEASURE_STEP_STAGES.get(value) or STAGE_BY_NAME.get(value)

            if stage is None:
                continue

            index = self._index_of(stage)

            if index >= 0 and (best is None or index > best):
                best = index

        return best

    @property
    def peer_proven_index(self):
        """
        对端**被证实**走到的最远一格（下标），没有证据 -> None。

        证据有两类，都是硬事实，不是猜：

          * 对端自己上报的阶段（`peer_seen`，走 `stage=` 信标）；
          * **双方共同完成的里程碑** —— `synced_start`（两边都测完了）证明对端
            至少到 `SYNC_START`；`cand_confirmed`（两边 candidate 都在）证明至少到
            `CONFIRM_CAND`。这些由本端状态机自己算出来，比"等一条信标"更硬：
            拿不到信标（老客户端）时也绝不会把显示卡在"等待对端"。

        显示（`display_stage` / `waiting_for_peer`）只读它。
        """
        best = self.peer_index

        floors = []

        if self.synced_start:
            floors.append(self._index_of(Stage.SYNC_START))

        if self.cand_confirmed:
            floors.append(self._index_of(Stage.CONFIRM_CAND))

        for value in floors:
            if value >= 0 and (best is None or value > best):
                best = value

        return best

    @property
    def display_stage(self):
        """
        **显示用**阶段：本端真实阶段与对端已上报阶段的较小者。

        规则（用户 2026-09-29）：「本端完成即上报；必须等到对端也上报同一阶段，
        才把本端显示推进到下一阶段；对端没到就显示「等待对端…」」。

          * 成功/失败终态 -> 直接显示终态（打通了就是打通了）；
          * **对端一次都没报过** -> 显示本端真实阶段。这不是"假装对端到了"，而是
            "没有证据说它没到"：老客户端根本不发 `stage=`，按"它落后了"渲染会把
            两边都卡死在起点（也会破坏基线 §G 的原文）。新客户端开工就会上报，
            所以真正"谁在等谁"几乎立刻就有依据。
          * 否则 -> `min(我的下标, 对端的下标)` 那一格（对端下标见
            `peer_proven_index`：信标 + 双方共同完成的里程碑）。

        **纯读**：不写任何字段、不影响任何真实迁移。
        """
        if self.stage == Stage.IDLE:
            return Stage.IDLE

        if self.success_terminal:
            return Stage.CONNECTED

        if self.failure_terminal:
            return Stage.FAILED

        mine = self._index_of(self.stage)

        if mine < 0:
            return self.stage

        theirs = self.peer_proven_index

        if theirs is None:
            return self.stage

        index = min(mine, theirs)

        return STAGE_ORDER[index] if 0 <= index < len(STAGE_ORDER) else self.stage

    @property
    def waiting_for_peer(self):
        """
        这一格是不是"我在等对端"（显示给 `等待对端…` / `WAIT_LABELS` 用）。

        只有在**有证据**（对端报过阶段 / 双方共同完成的里程碑）而且它确实落后时
        才算等待：

          * 本端已经走过 §E 的第一步（还在 `confirm_host` 时没有"谁等谁"）；
          * 对端被证实走到的最远阶段 < 本端阶段。

        对端一次都没报过 -> False（见 `display_stage` 的说明）。终态不算等待。
        """
        if self.stage in TERMINAL_STAGES or self.stage == Stage.IDLE:
            return False

        mine = self._index_of(self.stage)

        if mine <= self._index_of(Stage.CONFIRM_HOST):
            return False

        theirs = self.peer_proven_index

        return theirs is not None and theirs < mine

    def elapsed(self):
        return self.timer.elapsed()

    # ---- 阶段推进 ----

    def advance(self, stage):
        """
        前进**恰好一步**（§E 的顺序；用户 2026-09-28 要求"不许跳过阶段"）。

        允许的迁移只有 `TRANSITIONS` 里列的那些，也就是**当前阶段的下一阶段**
        （外加从 `PUNCH` 到两个终态）。以下一律拒绝并返回 False：

          * **跳步**（`NAT` 直接到 `REPORT`）—— 那正是"两端阶段对不上"的来源；
          * **回退 / 重复已完成阶段**（`REPORT` 回到 `NAT`）—— 用户点名不许；
          * **终态之后再有迁移** —— 一个结束了的连接不许被推活。

        回起点是**三条显式边**（`void_attempt()` / `note_round_failed()` /
        `restart()`），它们各自落日志、各自清哪一轮写得很清楚；`advance()`
        不承担这个职责，所以也绝不会悄悄把状态往回拨。

        返回 True 表示这一次真的前进了（已经在该阶段 -> False，幂等）。
        """
        stage = Stage(stage)

        if self.terminal:
            # 终态是不可逆的：一个已经结束的连接不许被"重新推进"成活的。
            return False

        if stage == self.stage:
            # 幂等：同一个阶段重复调用不是错误（每秒重画不会把状态打乱）。
            return False

        if stage not in TRANSITIONS.get(self.stage, ()):
            self.log(
                f"[direct] refusing stage {self.stage.value} -> {stage.value} "
                f"for {self.peer}: only "
                f"{[s.value for s in TRANSITIONS.get(self.stage, ())]} allowed "
                f"(no skipping, no going back)"
            )
            return False

        self._enter(stage)

        return True

    def _enter(self, stage):
        if stage != self.stage:
            self.stage = stage
            self.stage_entered_at = time.monotonic()

    def stage_for(self, stage):
        """把阶段直接设成 `stage`（**只给初始化/测试用**，正常走 advance）。"""
        self._enter(Stage(stage))

        return self.stage

    def start(self):
        """
        点「开始连接」这一刻：这条连接从 0 开始（§E 的第一阶段 + §F 的计时）。

        **幂等**：已经在跑就不重开表（重开表 = 把"这一轮花了多久"抹掉，
        而用户恰恰要看这个数）。

        只允许从 `IDLE` / `CONFIRM_HOST` 起步；从其他阶段（上一轮走到一半就
        被停掉）起步必须走 `restart()` —— 那是**新的一次连接**，不是"接着上一轮
        往后跳"。这条区分正是用户 2026-09-28 要的"手动中断后每个阶段都能重新
        走一遍"。
        """
        if self.timer.running and not self.terminal:
            return False

        if self.stage not in (Stage.IDLE, Stage.CONFIRM_HOST):
            self.log(
                f"[direct] {self.peer}: start() ignored from stage "
                f"{self.stage.value}; a new connection cycle must use restart()"
            )
            return False

        self.timer.start()
        self._enter(Stage.CONFIRM_HOST)

        return True

    def begin_measure(self, step):
        """
        本端进入测量的某一小步（`nat` / `inbound`）—— **只记显示用的值**。

        它会被 `room_direct` 拿去做周期性广播（`DIRECT_HIT stage=…`），于是
        对端在"等待同步起跑"里能显示「等待对端测量 NAT 类型」而不是干等。
        """
        value = str(step or "").strip().lower()

        if value in (PEER_MEASURING_NAT, PEER_MEASURING_INBOUND):
            self.my_stage = value

        return self.my_stage

    # ---- 测量（§E 前半段）----

    def mark_measured(self, nat=None, filtering=None, inbound=""):
        """
        本端测量完成（NAT → 入站 → 上报）。**本端**这一半的"起跑线就绪"。
        """
        if nat is not None:
            self.my_nat = nat

        if filtering is not None:
            self.my_filter = filtering

        if inbound:
            self.inbound = inbound

        self.my_measured = True
        self.my_stage = PEER_MEASURED

        return self.my_measured

    def mark_peer_measured(self):
        """对端报"我测完了"（`DIRECT_HIT stage=measured`）。"""
        self.peer_measured = True
        self.peer_stage = PEER_MEASURED
        self.peer_ready_at = time.monotonic()

        return self.peer_measured

    @property
    def synced_start(self):
        """
        「等待同步起跑」完成了吗 —— **两边都测完**（用户对 §E 的确认）。

        只有一边测完就往下走 = 端口错位 + 长时间打不通（BUG 3 的原话）。
        """
        return bool(self.my_measured and self.peer_measured)

    # ---- candidate（§E 中段）----

    def mark_candidate(self, addr, port=None, seq=None):
        self.my_candidate = addr

        if port is not None:
            self.port = int(port)

        if seq is not None:
            self.packet_seq = int(seq)

        return self.my_candidate

    def mark_peer_candidate(self, addr, nat=None, filtering=None):
        self.peer_candidate = addr

        if nat is not None:
            self.peer_nat = nat

        if filtering is not None:
            self.peer_filter = filtering

        return self.peer_candidate

    def confirm_candidate(self):
        """
        「双方确认 candidate」——两边的 candidate 都到了才算确认。
        """
        self.cand_confirmed = bool(self.my_candidate and self.peer_candidate)

        return self.cand_confirmed

    # ---- 同步起降 ----

    def arm(self, go_at):
        """记下约定的起跑时刻（`startIn` 语义：这是**本地单调钟**上的一刻）。"""
        self.go_at = go_at

        return self.go_at

    def set_countdown(self, until, label=""):
        """
        记下"还要等到的那个时刻"（本地单调钟），用于逐目标行的 0.1s 倒计时。

        **纯显示**：`row_text()` 读它，阶段推进/打洞决策一个字都不读。
        """
        with contextlib.suppress(Exception):
            self.countdown_until = float(until)
            self.countdown_label = str(label or "")

        return self.countdown_until

    def clear_countdown(self):
        """倒计时结束/作废（起跑时刻到了、换轮、作废这一轮）—— 立刻清掉。"""
        self.countdown_until = None
        self.countdown_label = ""

    def countdown_left(self):
        """还剩几秒（float）；没在倒计时就是 None。"""
        if self.countdown_until is None:
            return None

        with contextlib.suppress(Exception):
            return max(0.0, float(self.countdown_until) - time.monotonic())

        return None

    def countdown_text(self):
        """
        逐目标行上那一小格：`1.4s`（0.1s 分辨率，用户要的"毫秒感"倒计时）。

        没有倒计时 / 已经到点 -> None（调用方退回原来的正计时文案）。
        """
        left = self.countdown_left()

        if left is None or left <= 0.0:
            return None

        return f"{left:.1f}s"

    def note_punch(self, count=1, batch=None):
        """记一次发包。`count` 是这一批的数量（batch 之外的"第几次"）。"""
        self.punch_count += int(count or 0)

        if batch is not None:
            self.batch = int(batch)

        return self.punch_count

    # ---- 双端同步（§F 最重要的一条）----

    def adopt_batch(self, peer_batch, peer_attempt_id=""):
        """
        对端告诉我们它现在是第几轮。**仲裁权在 Host**（星形拓扑的锚）。

        * 我是 Host：我定 batch。对端报的和我一样 -> 正常继续；
          不一样 -> 抛 `SyncMismatch`，由上层作废这一轮、重发我的 batch。
        * 我是 Player：Host 说了算。不一样 -> 采纳 Host 的 batch（**作废我
          自己那一轮**），并抛 `SyncMismatch` 让上层从头重新同步。

        为什么要抛异常而不是"悄悄改个数继续打"：基线 §F 写得很清楚 ——
        "出现错位 → 作废当前 attempt → 清理 → 重新同步"，而且
        "**这条比'尽快打洞'更重要**"。
        """
        try:
            theirs = int(peer_batch)
        except (TypeError, ValueError):
            raise SyncMismatch("batch", self.batch, peer_batch)

        if peer_attempt_id and str(peer_attempt_id) != attempt_label(theirs):
            # 对端自己报的 id 和它报的 batch 都对不上：它的状态是坏的。
            raise SyncMismatch("attempt_id", self.attempt_id, peer_attempt_id)

        if theirs == self.batch:
            return False

        mine = self.batch

        self.void_attempt(reason=f"peer batch {theirs} != ours {mine}")

        if self.role != "host":
            # Player 跟随 Host 的轮次。
            self.batch = theirs

        raise SyncMismatch("batch", mine, theirs)

    def note_round_failed(self, reason=""):
        """
        这一**轮**没打通 —— 但**不是这次连接的终态**（打洞本来就要重试）。

        §F 的 Timer 规则说的是"一次**连接**到达终态"才停表；一轮失败之后循环
        还会再打，所以这里：

          * **不动 Timer**（用户看的"这一次连接花了多久"还要接着算 —— 清零只
            发生在终态，见 `finish()`）；
          * 只清这一轮的东西（测量/候选/发包数/起跑时刻），回到确认 Host；
          * 把原因留下（`fail_reason`），界面/日志说得出为什么。
        """
        self.fail_reason = str(reason or "")
        self.punch_count = 0
        self.packet_seq = 0
        self.my_measured = False
        self.peer_measured = False
        self.my_stage = ""
        self.peer_stage = ""
        self.my_candidate = None
        self.peer_candidate = None
        self.cand_confirmed = False
        self.go_at = None
        self.peer_ready_at = None
        self.clear_countdown()
        self._enter(Stage.CONFIRM_HOST)

        return self.batch

    def void_attempt(self, reason=""):
        """
        作废当前 attempt：清理这一轮的一切，回到"确认 Host"，**但不停表**。

        为什么不停表：用户看的是"这一次连接一共花了多久"，作废一轮不代表
        这一次连接结束了 —— 只有终态才停表（§F）。
        """
        self.voided += 1
        self.my_measured = False
        self.peer_measured = False
        self.my_stage = ""
        self.peer_stage = ""
        self.my_candidate = None
        self.peer_candidate = None
        self.cand_confirmed = False
        self.go_at = None
        self.peer_ready_at = None
        self.punch_count = 0
        self.packet_seq = 0
        self.port = None
        self.clear_countdown()
        self._enter(Stage.CONFIRM_HOST)

        self.log(
            f"[direct] voiding attempt for {self.peer}: {reason or 'state mismatch'}"
            f" (void #{self.voided}); resyncing from the start"
        )

        return self.batch

    def next_batch(self):
        """
        Host 开新的一轮：batch +1 并回到 §E 起点。

        只有 Host 可以调（Player 调了就是自己发明轮次，两端必然分叉）。
        """
        if self.role != "host":
            return self.batch

        self.batch += 1
        self.void_attempt(reason=f"host starts batch {self.batch}")
        self.voided -= 1  # 这是正常开轮，不是"作废"

        return self.batch

    # ---- 终态（§F 的 Timer 规则落点）----

    def finish(self, ok, reason="", latency_ms=None, addr=""):
        """
        到达终态：**立即停表、清零、释放**；不保留任何"最终耗时"。

        `addr` 是**打通的公网地址**（`"ip:port"`）—— 用户 2026-09-28 点名要求
        逐目标行里必须带上它（"给我按之前的模式把打通的地址和端口打印在逐行
        里"）。失败时留空，那一行按基线显示 `N/C`。

        返回这一轮的耗时（只配一行日志用）。下一次重连由上层 `restart()`，
        从 0 重新算。
        """
        self.connected = bool(ok)
        self.failed = not bool(ok)
        self.fail_reason = "" if ok else str(reason or "")
        self.latency_ms = latency_ms
        self.addr = str(addr or "") if ok else ""

        self._enter(Stage.CONNECTED if ok else Stage.FAILED)

        spent = self.timer.finish()

        self.log(
            f"[direct] connection {'up' if ok else 'failed'} for {self.peer} "
            f"after {spent:.1f}s (attempt {self.attempt_id})"
            + (f" at {self.addr}" if self.addr else "")
            + f"; timer stopped, zeroed and released"
            + (f" reason={reason}" if reason and not ok else "")
        )

        return spent

    def restart(self):
        """
        下一次重连：**重新创建计时**，从 0 开始，不继承任何旧值（§F 末句）。

        这里刻意不 new 对象 —— 上层持有引用；但所有"一次连接"的字段全部回到
        出厂状态，和 new 一个完全等价（`tests/punch_flow_test.py` 逐字段钉住）。

        **只有它能被用来"手动中断后重新开始"**：阶段回到 §E 第一步，`generation`
        递增（上层据此知道"这是新的一次连接，不是接着上一轮"）。
        """
        self.connected = False
        self.failed = False
        self.latency_ms = None
        self.fail_reason = ""
        self.addr = ""
        self.my_measured = False
        self.peer_measured = False
        self.my_stage = ""
        self.peer_stage = ""
        # §D：新的一次连接**不复用上一轮的事实**（对端报到哪儿 / 它是否直连，
        # 都是"上一次连接"的账）。它会在新一轮里重新上报。
        self.peer_seen = {}
        self.peer_connected = False
        self.peer_connected_at = None
        self.peer_connected_addr = ""
        self.reported_stage = ""
        self.my_candidate = None
        self.peer_candidate = None
        self.cand_confirmed = False
        self.go_at = None
        self.peer_ready_at = None
        self.punch_count = 0
        self.packet_seq = 0
        self.port = None
        self.inbound = ""
        self.batch = 0
        self.generation += 1
        self.timer = ConnTimer(clock=self.timer._clock)

        self._enter(Stage.CONFIRM_HOST)
        self.timer.start()

        return self

    # ---- 逐目标一行的文案（§G 第 3 层）----

    def row_parts(self, latency_ms=None):
        """
        逐目标一行的**结构化**版本（界面用；`row_text()` 是它的中文拼装）。

        为什么给界面结构而不是成品字符串：界面有三语言（`gui_i18n_test` 会遍历
        控件树，英文界面残留一个全角括号都算漏翻）。界面拿这些片段各自 `tr()`，
        再用翻译表里的模板拼出来 —— 核心这边永远只说中文（日志正文英文，
        界面中文），两边互不污染。

        字段（用户 2026-09-28 点名要的那几样一个不少）：

          `stage`       阶段名（内含"等待对端测量 X"的显示变体）
          `seconds`     阶段已经过了几秒（没有上限的阶段是 None）
          `name`        **对端真实名字**（中继广播的显示名；拿不到才 client_id）
          `addr`        打通的公网 `ip:port`（没打通是空串 -> 界面写 `N/C`）
          `punch_count` 「第 N 次」（只在打洞阶段有意义）
          `latency`     `N/C` / `-` / `18ms`
          `connected`   是否已直连
          `attempt`     双端同步用的 `att=`（**不上屏**，仅供排障/测试）
        """
        if self.stage == Stage.IDLE:
            return {}

        secs = self._stage_seconds()
        stage = self.display_stage

        return {
            "stage": self.stage_text,
            "seconds": secs,
            "name": self.name,
            "addr": self.addr,
            # 「第 N 次」只在打洞阶段有意义（别的阶段还没有包可数）。
            "punch_count": (
                self.punch_count if stage == Stage.PUNCH else 0
            ),
            # 用户 2026-09-29 的显示升级：打洞那一行带 `PUNCH #0012`
            # （零填充的尝试计数），和基线要求的「第 N 次」并存。
            "punch_number": (
                int(self.punch_count)
                if stage == Stage.PUNCH and self.punch_count else 0
            ),
            "punch_label": (
                f"PUNCH #{int(self.punch_count):04d}"
                if stage == Stage.PUNCH and self.punch_count else ""
            ),
            # 「显示停在这一格是因为在对端之后」——界面/测试可以据此加提示。
            "waiting": bool(self.waiting_for_peer),
            "display_stage": stage.value,
            "attempt": self.attempt_id,
            "latency": self.latency_text(latency_ms),
            "connected": self.connected,
        }

    def row_text(self, latency_ms=None, compact=False):
        """
        `等待同步起跑（3s）【玩家名】N/C` 这一行的正文。

        规则（基线 §G + 用户 2026-09-28 的两条修正）：

          * 阶段名 + （阶段已经过了几秒）+ `【对端真实名字】` + 延迟；
            **`【…】` 里放的是名字，不是字母计数**（`【A】` 只是设计文档的占位
            示例；用户原话："谁让你用这个计次数了"）；
          * 打通那一行必须带**公网地址:端口**：
            `P2P 已直连【玩家名】223.166.20.9:40879 18ms`；
          * **未建立连接前延迟显示 `N/C`**；已连接后显示真实 P2P 延迟；
          * 已连接但这一秒还没测出数字 -> `-`（口径同 app.peer_latency_text）。

        `compact=True` 时不显示秒数（给成员列表那种一行很窄的地方用）。
        """
        if self.stage == Stage.IDLE:
            return ""

        label = self.stage_text

        # ============================================================================
        #  ★ 用户 2026-09-30（第三轮第 7 条）：起跑前的**相对倒计时**
        # ============================================================================
        #
        # 「等待同步起跑 / 等待发包起跑」这两段都是"等一个已知时刻"：前者等两边
        # 都测完（上限 12s），后者等**约定的喷包时刻**（`startIn=1.5`，两边同时
        # 开喷）。真实流程一直有这两步，但逐目标行以前只有"进入本阶段的累计秒"
        # （正计时），所以看不到用户要的 `1.5s -> 1.2s -> 开打`。
        #
        # 有倒计时就显示倒计时（0.1s 分辨率，界面每 150ms 重画一次）；到点/没有
        # 倒计时就退回原来的文案。**纯显示**，不参与任何决策。
        countdown = None if compact else self.countdown_text()

        if countdown is not None:
            # 倒计时期间**不**再叠 `PUNCH #NNNN`：那 1.5 秒里真正发生的事是
            # "还在等起跑"，不是"已经在喷包"。
            #
            # 文案取**本端真实阶段**的基线原文（`STAGE_LABELS[self.stage]`），
            # 不是 `stage_text`：`stage_text` 在"显示同步"规则下可能是
            # 「等待对端测量 NAT 类型」那种等待文案，而这一格现在要回答的是
            # "我卡在同步起跑这一步，还剩 9.8 秒"。对端在测什么作为注脚跟在
            # 后面（用户 09-28 要的信息一个字都没丢）。
            base = self.countdown_label or STAGE_LABELS.get(self.stage, "") or label
            note = ""

            if not self.countdown_label and self.stage == Stage.SYNC_START:
                short = PEER_STAGE_SHORT.get(self.peer_stage or "")

                if short:
                    note = f" · {short}"

            head = f"{base} {countdown}{note}"
        elif compact:
            head = label
        else:
            secs = self._stage_seconds()

            head = label if secs is None else f"{label}（{secs}s）"

        # 显示升级（用户 2026-09-29）：打洞那一行带上 `PUNCH #0012`
        # （零填充计数）—— 它放在阶段名后面、名字前面，基线的 `第 N 次` 保留。
        if countdown is None and self.display_stage == Stage.PUNCH and self.punch_count:
            head = f"{label} PUNCH #{int(self.punch_count):04d}"

            if not compact:
                secs = self._stage_seconds()
                head = head if secs is None else f"{head}（{secs}s）"

        # 名字那一格：绝不为空（`name` 有 client_id 兜底）。
        head += f"【{self.name}】"

        # 版式：`打洞中 PUNCH #0003【名】第 3 次 · N/C` /
        #        `等待发包起跑 1.4s【名】N/C` /
        #        `P2P 已直连【名】ip:port 18ms`。
        if countdown is None and self.display_stage == Stage.PUNCH and self.punch_count:
            return head + f"第 {self.punch_count} 次 · " + self.latency_text(
                latency_ms
            )

        addr = f"{self.addr} " if (self.connected and self.addr) else ""

        return head + addr + self.latency_text(latency_ms)

    def latency_text(self, latency_ms=None):
        """
        延迟那一格。**没连上就 `N/C`** —— 这是用户点名要的口径。

        `N/C` = not connected / 不适用；`-` = 已连接但还没测出数字。
        """
        if not self.connected:
            return LATENCY_NOT_CONNECTED

        value = self.latency_ms if latency_ms is None else latency_ms

        if value is None:
            return LATENCY_UNKNOWN

        try:
            ms = float(value)
        except (TypeError, ValueError):
            return LATENCY_UNKNOWN

        # 亚毫秒不许显示成 0（审计 P2-8 已经钉过的同一条口径）。
        return f"{ms:.0f}ms" if ms >= 1 else f"{ms:.1f}ms"

    def _stage_seconds(self):
        """
        阶段秒数。**只有"有上限、会过去"的阶段才加秒数**。

        用户 2026-09-27：单人房里显示"等待同步起降"并一直正计时是 BUG。
        已连接（终态）也不显示秒数 —— 那一刻表已经清零释放了。
        """
        if self.stage in TERMINAL_STAGES or not self.stage_entered_at:
            return None

        value = max(0.0, time.monotonic() - self.stage_entered_at)

        return int(value)

    def snapshot(self):
        """给界面/测试读的一份只读视图（字段名就是基线 §F 列的那些）。"""
        return {
            "peer": self.peer,
            "name": self.name,
            "label": self.label,
            "addr": self.addr,
            "role": self.role,
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
            "attempt": self.attempt,
            "batch": self.batch,
            "packet_seq": self.packet_seq,
            "port": self.port,
            "stage": self.stage.value,
            "stage_text": self.stage_text,
            "display_stage": self.display_stage.value,
            "waiting_for_peer": bool(self.waiting_for_peer),
            "peer_seen": sorted(self.peer_seen),
            "peer_connected": bool(self.peer_connected),
            "reported_stage": str(self.reported_stage or ""),
            "peer_stage": self.peer_stage,
            "stage_seconds": self._stage_seconds(),
            "row_text": self.row_text(),
            "row_parts": self.row_parts(),
            "connected": self.connected,
            "failed": self.failed,
            "latency_ms": self.latency_ms,
            "my_measured": self.my_measured,
            "peer_measured": self.peer_measured,
            "synced_start": self.synced_start,
            "cand_confirmed": self.cand_confirmed,
            "elapsed": self.elapsed(),
            "timer_running": self.timer.running,
            "timer_released": self.timer.released,
            "voided": self.voided,
            "generation": self.generation,
        }


# ============================================================================
#  §D 目标选择 —— 每轮开打前**重新拉**
# ============================================================================
#
# 基线原文：
#
#   * Player：每轮开打前重新拉当前 Host，确认自己的打洞目标；**只认 Host**。
#     不得复用上一轮的 Host ID/endpoint/candidate。
#   * Host：每轮开打前重新拉当前所有 Player 生成目标列表。
#   * 已打通的连接，在"游戏中新人加入"时不得被影响；换模式必须严格重建全部通路。
#
# BUG 1（房主≠host 的三人房）的根因就在这里：目标以前是
# `room_direct._host_client_id()` ——**取花名册里第一个不是我的人**。
# 房主（owner）排在 host 前面时，那个函数把 owner 当成 host，于是
# player 的热切换打洞打向了 owner。中继那边 `_direct_peer()` 是按**真正的
# host 位置**转发的，所以 host 侧显示"和该 player 已打通"、player 侧却连不上。


def _ids(value):
    """把 `a,b,c` / `["a","b"]` / `None` 统一成去重后的列表。"""
    if not value:
        return []

    if isinstance(value, str):
        parts = value.split(",")
    else:
        try:
            parts = list(value)
        except TypeError:
            return []

    out = []

    for item in parts:
        item = str(item or "").strip()

        if item and item != "-" and item not in out:
            out.append(item)

    return out


def _clean_name(value):
    """
    和中继 `room_members.clean_host_name()` **同一条**压缩规则。

    中继的 `host_name=` 是把显示名里所有 `ord(c) < 33` 的字符**去掉**（空格、
    制表符、换行），而 `names=` 表里的名字是 **percent-encoded**（`%20`）。
    两者不是同一个字符串，直接 `==` 比不出来 —— 这就是为什么这里要各压一遍。
    """
    return "".join(c for c in str(value or "") if ord(c) >= 33 and ord(c) != 127)


def _decode_field(value):
    """中继 `room_names.encode_field()` 的逆（percent-decoding，绝不抛）。"""
    try:
        from urllib.parse import unquote

        return unquote(str(value or ""))
    except Exception:
        return str(value or "")


def host_id_of(state):
    """
    房间**当前 Host 的 client_id**，只认中继给的事实。

    来源优先级（**全部容错**，老中继只发得到第 3 档）：

      1. `host_id=` —— 中继新增字段（见文件末尾"假设 A"），最权威；
      2. `hosts=`（复数）里的第一个 —— 兼容另一种可能拼法；
      3. `host_name=` 在 `names=` 表里反查到的 client_id
         （两边都压成 `clean_host_name` 的形式再比，解决 `%20` 对不上的问题）；
      4. 空串 —— **绝不猜**。

    第 4 档很重要：BUG 1 的根因就是"猜"（花名册第一个）。猜错比"不知道"贵得多：
    不知道时 `select_targets` 会返回空、界面显示「确认 Host 中」，而猜错时
    一整轮打洞都打在错的人身上，两端状态还会不一致。
    """
    if state is None:
        return ""

    direct = str(getattr(state, "host_id", "") or "").strip()

    if direct and direct != "-":
        return direct

    # 我的身份从别处也知道了：如果中继说 host 是我，直接给答案。
    if getattr(state, "is_host", False) and getattr(state, "client_id", ""):
        return str(state.client_id)

    name = str(getattr(state, "host_name", "") or "").strip()

    if not name or name == "-":
        return ""

    wanted = _clean_name(name)

    for cid, display in (getattr(state, "member_names", None) or {}).items():
        if _clean_name(_decode_field(display)) == wanted:
            return str(cid)

    if wanted == _clean_name(getattr(state, "self_name", "") or ""):
        return str(getattr(state, "client_id", "") or "")

    return ""


def started_ids_of(state):
    """
    房间里「已开始连接」的玩家 client_id 集合。

    来源：中继新增的 `started=`（§假设 B）。**老中继不发这个字段** —— 那时
    返回 `None`（= 未知），调用方必须按"未知"处理，**不许当成空集合**：
    当成空集合会让 Host 一个目标都拉不到，从而"谁都不打"；
    未知时退回老行为（花名册所有人）才是不破坏现状的加法。
    """
    if state is None:
        return None

    raw = getattr(state, "started_ids", None)

    if raw is None:
        return None

    return set(_ids(raw))


def _trusted_started(state, known_ids):
    """
    `started=` 的一份**可用**读法：对不上 id 空间时按"未知"处理。

    ============================================================================
      ★ 为什么需要这一层（2026-09-28 实测到的中继字段不一致，已上报）
    ============================================================================

    中继 `started_client_ids()` 目前给"Host 已开始连接"加的是
    `room.host_id` —— 那是**老式 `HOST <room>` 开服控制连接**的 id，
    和房间成员的 `client_id` **不是同一套**（实测：成员 `h-cid`，
    `started=` 里却是 `c1da8e4f28d64b1c`）。而同一行上的 `host_id=` 给的是
    成员的 client_id（`host_client_id(room)`，对的）。

    后果（不处理就是 P0）：
      * Player 侧：`host` 永远不在 `started=` 里 -> 一直被判「Host 未连接」；
      * Host 侧：`started ∩ peers = ∅` -> **一个目标都拉不到**，谁都不打。

    所以这里做一次**一致性检查**：`started=` 里如果**一个已知成员 id 都没有**，
    那它用的就是另一套 id 空间，对客户端而言等于"没这个信息"（返回 None，
    走老行为）。中继把它改成 `host_client_id(room)` 之后，这个检查自动通过，
    客户端不用改。

    真正"一个都没开始连接"（`started=-`）仍然是**空集合**，不是未知 ——
    那时 Host 确实没有目标。
    """
    started = started_ids_of(state)

    if started is None or not started:
        return started

    if not (started & set(known_ids)):
        return None

    return started


def host_ready(state, me=""):
    """
    §B 的门：**当前 Host 已经"开始连接"了吗**？返回 True / False / None。

      * `True`  —— Host 在 `started=` 里（可以进正式 P2P 流程）；
      * `False` —— 有 Host、但中继明确说它没开始（界面提示
        「Host 未连接，请等待 Host 开服」）；
      * `None`  —— 判断不了（我不在房间 / 没有 Host / 老中继没有 `started=` /
        id 空间对不上）。**None 必须按"不知道"处理**，不能当成 False：
        当成 False 会让老中继上谁都连不了。

    界面（`on_start` 的提示）和打洞（`select_targets` 的目标）共用这一个来源，
    所以"提示"和"实际行为"不可能互相矛盾。

    实现委托给 `host_availability`（两套状态分开命名，见那里的说明）。
    """
    value = host_availability(state, me)

    if value == HOST_READY:
        return True

    if value == HOST_NOT_CONNECTED:
        return False

    return None


#: `host_availability()` 的四个取值 —— **身份**与**连接**分开命名。
#: 用户 2026-09-29：「连接和身份是两回事」。
HOST_NONE = "no_host"                    # 身份：房间当前**没有** host 位（可认领）
HOST_NOT_CONNECTED = "host_not_connected"  # 连接：有 host 位，但它没在开服
HOST_READY = "host_ready"                # 连接：有 host 位，而且它在开服
HOST_UNKNOWN = "unknown"                 # 判断不了（老中继 / 我就是 host / 不在房）


def peer_conn_state(state, peer):
    """
    `conn=<cid>:<state>` 里这个 peer 的连接状态；拿不到就是 `""`（未知）。

    取值只有 `idle` / `waiting` / `pairing` / `connected`（PROTOCOL §8.5）。
    老中继没有 `conn=` -> 空表 -> **一律返回 ""（未知）**，绝不当成 idle：
    "不知道"和"明确空闲"必须分开（同一个教训见 `host_availability`）。
    """
    conns = getattr(state, "member_conns", None) or {}

    try:
        return str(conns.get(str(peer or "")) or "").strip().lower()
    except Exception:
        return ""


def peer_is_idle(state, peer):
    """
    中继**明确**说这个 peer 此刻没有任何游戏连接吗？

    ============================================================================
      ★ 用户 2026-09-29 第 1 条：对端在房里但**没点开始连接**，Host 却把 §E 走到
        「等待同步起跑」
    ============================================================================

    `conn=idle` 是中继从**连接本身**现算出来的硬事实（PROTOCOL §8.5）：这个人
    没有 PLAYER / 开服连接，也没上报 `STARTED 1`。对这种对端打洞是纯浪费，而且
    会让 Host 那一行永远停在 `SYNC_START`（对端从不回应 -> 每 12s 超时重来）。

    未知（老中继 / 广播里没有这条）返回 False —— 未知绝不等于空闲。
    """
    return peer_conn_state(state, peer) == "idle"


def host_availability(state, me=""):
    """
    房间的「Host 可用性」——**身份状态与连接状态分成两套读法**。

    用户 2026-09-29 的硬口径（AGENTS.md 规则 6 补充）：

      * **身份**（主机位在不在）= 中继广播的 `host=` / `host_claimed`；
        主机只是**停止连接**时它**不变**，别人也**不许**因此可以认领。
      * **连接**（主机连没连）= `conn=<cid>:<state>`（PROTOCOL §8.5）优先，
        其次是"客户端显式上报的 `started=`"。

    返回：

      * `"no_host"`           —— 身份事实：没有 host 位（UI：暂无 Host，可认领）；
      * `"host_not_connected"`—— 连接事实：host 位在，但它没开始（UI：
        「Host 已断开连接 / 未开服」，**不是**"暂无 Host"，**不许**放开认领）；
      * `"host_ready"`        —— host 位在，而且它在开服；
      * `"unknown"`           —— 判断不了（老中继 / host 就是我 / 不在房间）。
        **unknown 绝不等于 not_connected**，调用方按"不知道"处理。
    """
    if state is None:
        return HOST_UNKNOWN

    me = str(me or getattr(state, "client_id", "") or "").strip()

    if not getattr(state, "host_claimed", False):
        return HOST_NONE

    host = host_id_of(state)

    if not host or host == me:
        # 认不出 host 是谁（老中继没有 host_id）或者 host 就是我自己：
        # 我不需要等自己 —— 按"不知道"处理（旧行为）。
        return HOST_UNKNOWN

    peer_ids = _ids(getattr(state, "peer_ids", None))

    # ---- 连接事实（1）：`conn=`（每成员连接状态，PROTOCOL §8.5）----
    conns = getattr(state, "member_conns", None) or {}
    conn = str(conns.get(host) or "").strip().lower()

    if conn == "connected":
        return HOST_READY

    if conn == "idle":
        return HOST_NOT_CONNECTED

    # `waiting` / `pairing` 说明它的连接在动，但还没进世界 —— 继续看 `started=`。

    # ---- 连接事实（2）：`started=`（客户端显式 STARTED 上报的并集）----
    started = _trusted_started(state, set(peer_ids) | {me})

    if started is None:
        return HOST_UNKNOWN

    return HOST_READY if host in started else HOST_NOT_CONNECTED


def select_targets(state, me=""):
    """
    §D：这一轮该对谁打洞。返回 `(targets, reason)`。

    * **Player**：只认 Host。Host 未知 -> `([], "no_host")`（界面提示
      「当前房间暂无 Host，请等待 Host 认领」——基线 §B）；
      Host 已认领但**还没开始连接** -> `([], "host_not_ready")`
      （提示「Host 未连接，请等待 Host 开服」）。
    * **Host**：所有「已开始连接」的玩家；老中继没有 `started=` 时退回花名册
      里的所有人（`reason="started_unknown"`，行为与今天一致）。
    * 已经打通的 peer 由调用方跳过（不在这个函数的职责里），所以
      "新人加入不影响已打通"是天然满足的。

    ============================================================================
      没有 `peers=` 的老中继：退回"按人数合成槽位"（老行为，一个字不变）
    ============================================================================
    中继的 `peers=` 是 3.0.3 加的。更老的中继只给 `members=<n>`，那时：
      * Host 端按人数合成 `guest-0..guest-N`（粗，但**严格好过**"只服务第一个
        玩家、其余永远等"—— 那正是它当初被加进来的原因）；
      * Player 端的键用 `host_name`（见下面的说明：Player 的直连行不带 `to=`，
        中继按**真正的 host 位置**转发，所以这个键只是本地记账，不是路由）。
    """
    if state is None:
        return [], "no_room"

    me = str(me or getattr(state, "client_id", "") or "").strip()
    peer_ids = _ids(getattr(state, "peer_ids", None))
    roster = [p for p in peer_ids if p != me]

    try:
        members = int(getattr(state, "members", 0) or 0)
    except (TypeError, ValueError):
        members = 0

    if members < 2:
        # 单人房 / 还没别人：没有目标。
        return [], "no_peer"

    known_ids = set(roster) | {me}

    if not getattr(state, "is_host", False):
        host = host_id_of(state)

        if host and host != me:
            # ================================================================
            #  §B：Host 已认领但**还没开始连接** -> 不允许进入正式 P2P 流程
            # ================================================================
            #
            # 基线原文："有 Host 但 Host 未开始连接：其他玩家点开始连接**不得
            # 进入正式 P2P 流程**，提示「Host 未连接，请等待 Host 开服」。"
            #
            # 中继的 `started=` 就是这个事实（Host "已开始连接" = 它的开服控制
            # 连接在）。`started` 为 None = 老中继没这个 token / id 空间对不上
            # -> 不能拿它当依据（否则谁都连不了），保持老行为。
            started = _trusted_started(state, known_ids)

            if started is not None and host not in started:
                return [], "host_not_ready"

            # ★ 2026-09-29：`conn=`（连接状态）也参与同一个门 —— 主机位在、
            # 但它的连接状态明确是 idle 时，同样不许进正式流程（身份 ≠ 连接）。
            if host_availability(state, me) == HOST_NOT_CONNECTED:
                return [], "host_not_ready"

            if not roster or host in roster:
                return [host], "host"

            # 中继的 peers= 还没刷新到（host 刚认领）：宁可不打，也别打错人。
            return [], "host_not_in_roster"

        # ================================================================
        #  老中继 / 拿不到 client_id 时的**安全**退路
        # ================================================================
        #
        # 关键事实：**Player 侧发出去的直连行不带 `to=`**（`DirectLink._to_field`
        # 只给 Host 加），所以中继的 `_direct_peer()` 会把它转给**真正的
        # host 位置** —— 路由不由这个本地标识决定。
        #
        # 也就是说：这里的 `host` 只是**本地记账用的键**（平局裁决 + 界面显示），
        # 不是路由目标。用中继广播的 `host_name` 当这个键是安全的（它**就是**
        # 当前 Host 的名字，不是"花名册里第一个"那种猜法 —— BUG 1 的根因）。
        # 拒绝打洞反而会让老中继上功能整个失效，那是更大的回归。
        name = str(getattr(state, "host_name", "") or "").strip()
        my_name = str(getattr(state, "self_name", "") or "").strip()

        if name and name != "-" and name != my_name:
            return [name], "host_by_name"

        if getattr(state, "host_claimed", False):
            return ["peer"], "host_synthetic"

        return [], "no_host"

    if roster:
        started = _trusted_started(state, known_ids)

        if started is None:
            return roster, "started_unknown"

        return [p for p in roster if p in started], "started"

    # ---- Host 端，老中继没有 `peers=` ----
    return [f"guest-{i}" for i in range(max(1, members - 1))], "synthetic_guests"


# ============================================================================
#  ★ 中继字段：**已经落地的** 与 **仍在假设的**（以中继实际发出的格式为准）
# ============================================================================
#
# 2026-09-28 复核 `server/room_members.py::broadcast_member_state`，中继现在
# 实际广播的是：
#
#     HOST_STATE host=0/1 host_name=<name> members=<n> world=<n>
#                peers=<cid,cid|-> names=<cid>:<name>,... rtts=<cid>:<ms>,...
#                started=<cid,cid|-> mode=relay|stun
#
#   A. **`started=` —— 已落地**（服务端工程师本轮加的，见
#      `started_client_ids()` / `started_token()`）。格式逐字：
#          started=<cid>,<cid>,...   一个都没有时是 `-`
#      "已开始连接" = 该成员此刻有 PLAYER 连接（等待配对或已配对），或者它是
#      当前 Host 且有 `HOST <room>` 开服控制连接。顺序无关、不重复。
#      客户端按 `,` 切分即可（cid 是 ASCII 短串，不可能含 `,`）。
#      -> `punch_flow.STARTED_TOKENS[0] == "started"` 就是它。
#
#   B. **当前 Host 的 client_id —— 仍然没有这个 token**（截至本轮，
#      `host_id` 只出现在服务端日志 `relay_server.py:3461`，没有上线）。
#      所以 `host_id_of()` 的实际主路径是**第 3 档**：
#      `host_name=` 在 `names=` 表里反查 —— 两边都用
#      `clean_host_name()` 的同一套压缩规则（去掉 ord<33 的字符）后再比，
#      解决 `host_name` 是裸名、`names` 值是 percent-encoded 的对不上问题。
#      仍然兼容 `host_id=` / `hosts=` / `host_cid=` 三种拼法：中继哪天加了，
#      客户端**不用再发版**就能用。
#
#   C. **错误码** —— 已落地的是 `ERROR NOT_HOST`（换模式越权，服务端仍用这个
#      码 + 中文说明）与 `ERROR CONN_MODE_CHANGED`（换模式后旧通路被作废，
#      客户端据此按新模式重建，而不是当网络抖动）。`NO_HOST` 仍然是
#      "没有 Host 的隧道被拒"。`HOST_NOT_READY` / `HOST_NOT_STARTED` /
#      `HOST_OFFLINE` **还没落地**，这里先做可识别（见 `host_error_reason`），
#      落地后不用改客户端。
#
# 这三条都**不是**客户端发明的协议：A/C 是中继实际发出的格式，B 是"将来可能
# 发的字段"的容错读法。具体 token 以中继实际发出的为准 —— 落地后只需要改
# `HOST_ID_TOKENS` 这一组别名。


#: 「当前 Host 的 client_id」可能出现的字段名，按优先级。
HOST_ID_TOKENS = ("host_id", "hosts", "host_cid")

#: 「已开始连接的成员」可能出现的字段名，按优先级。
STARTED_TOKENS = ("started", "joined", "players", "ready")

#: 「Host 存在但还没开始连接」这一类**可识别原因**。
#:
#: 键 = 中继错误码；值 = 界面用的原因键（`protocol.py::ERROR_TEXT` 负责文案）。
HOST_REASON_CODES = frozenset({
    "NO_HOST",
    "HOST_NOT_READY",
    "HOST_NOT_STARTED",
    "HOST_OFFLINE",
})


def parse_host_state_tokens(tokens):
    """
    解析一条 `HOST_STATE ...` 的 k=v 片段，返回**加法式**字典。

    返回值只包含**这条行上真的出现过**的键（`{}` = 老中继，什么都没多给）。
    调用方按需取，取不到就当"未知" —— 这是"新客户端连老中继还能用"的落点。

    识别的新键：`host_id` / `hosts` / `host_cid`（当前 Host）、
    `started` / `joined` / `players` / `ready`（已开始连接）。
    **其余键一律忽略**（additive 协议规则：不认识就不认识，不许报错）。
    """
    out = {}

    if isinstance(tokens, str):
        tokens = tokens.split()

    host_id = ""
    started = None

    for token in tokens or ():
        key, _, value = str(token).partition("=")

        if not key:
            continue

        if key in HOST_ID_TOKENS and not host_id:
            values = _ids(value)

            if values:
                host_id = values[0]

        elif key in STARTED_TOKENS and started is None:
            # `-` 在这里是**有意义的**："明确说了谁都没开始"。
            if value == "-":
                started = []
            else:
                started = _ids(value)

    if host_id:
        out["host_id"] = host_id

    if started is not None:
        out["started_ids"] = started

    return out


def host_error_reason(text):
    """
    从一条中继回包/推送里认出"Host 不可用"的**可识别原因**。

    返回 `"no_host"` / `"host_not_ready"` / `""`（认不出）。

    基线 §B 要求三种提示分开：
      * 没人认领        -> 「当前房间暂无 Host，请等待 Host 认领」（`NO_HOST`）
      * 有 Host 但没开服 -> 「Host 未连接，请等待 Host 开服」（`HOST_NOT_READY`）
    """
    upper = str(text or "").upper()

    if "HOST_NOT_READY" in upper or "HOST_NOT_STARTED" in upper:
        return "host_not_ready"

    if "HOST_OFFLINE" in upper:
        return "no_host"

    if "NO_HOST" in upper:
        return "no_host"

    return ""


# ============================================================================
#  §G 三层文案（父级只做汇总）
# ============================================================================
#
# 1. **通路**：`直连尝试中... 0/3` → `P2P 直连中... 2/3` → `P2P 已直连：3/3`
# 2. **打洞进度**：`正在建立 3 条连接` → `2/3 已完成` /
#    `打洞完成：全部 3 个目标已打通` / `2/3 成功，1 个目标失败`
# 3. **逐目标明细**：每目标一行（`ConnSession.row_text()`）
#
# 父级（第 1/2 层）**只汇总**，不判断任何东西；判断全在 `ConnSession` 里。


def summarize(sessions, punching=True):
    """
    把一组 `ConnSession` 汇总成三层文案。

    返回 dict：
        {
          "total": n, "up": k, "failed": m, "pending": p,
          "path":     第 1 层「通路」的正文（不含数字以外的颜色提示）,
          "progress": 第 2 层「打洞进度」的正文,
          "rows":     第 3 层：每目标一行（`ConnSession.row_text()`）,
          "all_up":   bool,
        }

    `punching=False`（relay 模式 / 没在打洞）时三层都退化成"未连接"口径，
    **不显示"打洞中"**（审计问题 2 的同一道门）。
    """
    items = [s for s in (sessions or ()) if s is not None]

    total = len(items)
    up = sum(1 for s in items if s.connected)
    failed = sum(1 for s in items if s.failed)
    pending = total - up - failed
    all_up = bool(total) and up == total

    if not total:
        return {
            "total": 0,
            "up": 0,
            "failed": 0,
            "pending": 0,
            "path": "未连接",
            "path_key": "未连接",
            "path_args": (),
            "progress": "未启用" if not punching else "等待对端用户加入…",
            "progress_key": (
                "未启用" if not punching else "等待对端用户加入…"
            ),
            "progress_args": (),
            "rows": [],
            "all_up": False,
        }

    if not punching and not (up or failed):
        path = path_key = "未连接"
        path_args = ()
        progress = progress_key = "未连接"
        progress_args = ()
    elif all_up:
        path_key = "P2P 已直连：{0}/{1}"
        path_args = (up, total)
        path = path_key.replace("{0}", str(up)).replace("{1}", str(total))
        progress_key = "打洞完成：全部 {0} 个目标已打通"
        progress_args = (total,)
        progress = progress_key.replace("{0}", str(total))
    elif up:
        path_key = "P2P 直连中... {0}/{1}"
        path_args = (up, total)
        path = path_key.replace("{0}", str(up)).replace("{1}", str(total))
        progress_key = "{0}/{1} 已完成"
        progress_args = (up, total)
        progress = progress_key.replace("{0}", str(up)).replace("{1}", str(total))

        if failed:
            progress_key = "{0}/{1} 已完成，{2} 个失败"
            progress_args = (up, total, failed)
            progress = (
                progress_key.replace("{0}", str(up))
                .replace("{1}", str(total))
                .replace("{2}", str(failed))
            )
    else:
        path_key = "直连尝试中... 0/{0}"
        path_args = (total,)
        path = path_key.replace("{0}", str(total))

        if failed and not pending:
            progress_key = "{0}/{1} 成功，{2} 个目标失败"
            progress_args = (up, total, failed)
        else:
            progress_key = "正在建立 {0} 条连接"
            progress_args = (total,)

        progress = progress_key

        for index, value in enumerate(progress_args):
            progress = progress.replace("{%d}" % index, str(value))

    return {
        "total": total,
        "up": up,
        "failed": failed,
        "pending": pending,
        "path": path,
        "path_key": path_key,
        "path_args": path_args,
        "progress": progress,
        "progress_key": progress_key,
        "progress_args": progress_args,
        "rows": [s.row_text() for s in items],
        "all_up": all_up,
    }


# ============================================================================
#  客户端↔客户端 的同步信标（走中继**已有的**转发关键字，不新增协议）
# ============================================================================
#
# ============================================================================
#  ★ 为什么这不是"自己发明协议"
# ============================================================================
#
# 中继（`server/room_members.py`）对 `DIRECT_CAND` / `DIRECT_PUNCH` / `DIRECT_HIT`
# 三个关键字只做一件事：**逐字转发**给对端（`_direct_peer()` 只读 `to=` 这一个
# 路由 token）。所以"两端之间再加几个 `k=v` 字段"是**已经在用**的加法机制
# （`sub=` / `filt=` / `startIn=` / `local=` 都是这么加的），中继一行都不用改。
#
# 这里用 `DIRECT_HIT` 承载 §E 中段的两个同步点（`startIn=` 那条 `DIRECT_PUNCH`
# 专管"什么时候起跑"，语义不动）：
#
#     DIRECT_HIT stage=measured att=A batch=0          <- 我测完了（§E 等待同步起跑）
#     DIRECT_HIT stage=cand_ok  att=A batch=0          <- 我确认了 candidate（§E 双方确认）
#
# 两条都带 `att=` / `batch=`，于是"错位"不再是一句猜测 —— 它是**量出来的**：
# 对端报的 batch 和我这边不一样 = 立刻作废这一轮重来（§F）。
#
# 老客户端不会发这两条，新客户端等不到就按"对方是老的"处理（见 room_direct
# 的 BOUNDED 等待），双方都不受影响。

#: 同步信标的关键字（中继已经会转发的三个之一）。
BEACON_KEYWORD = "DIRECT_HIT"

#: 同步信标的阶段名。
BEACON_MEASURED = "measured"
BEACON_CAND_OK = "cand_ok"
#: **测量中的小步** —— 让对端能显示「等待对端测量 NAT 类型 / 入站策略中」，
#: 而不是在没有信息时干等（用户 2026-09-28 第 4 条：两端阶段必须对得上）。
BEACON_NAT = PEER_MEASURING_NAT
BEACON_INBOUND = PEER_MEASURING_INBOUND


def beacon_line(stage, attempt_id, batch, to_field="", from_id=""):
    """
    构造一条同步信标。`to_field` / `from_id` 由调用方给（Host 加 `to=`，
    两端都加 `from=`）。

    **字段顺序固定**：`stage=` `att=` `batch=` `from=` `to=` 在最后 —— 和客户端
    其它握手行"版本号挂最后一个 token"是同一条纪律，位置解析的老代码不会被打乱。

    ----------------------------------------------------------------------------
      `from=` 为什么必须有（BUG 4 的另一半：Host 怎么知道"谁点了开始连接"）
    ----------------------------------------------------------------------------
    中继的 `started=` 是按 **PLAYER / HOST 连接**算的（PROTOCOL §8.1）。可是
    这套客户端的设计是"**先打通洞、游戏再挂上来**"（见 `room_direct.py` 头部），
    所以玩家点了「开始连接」之后、Minecraft 连上来之前，**根本没有 PLAYER
    连接** —— 中继自然看不到他"已开始连接"。

    结果：Host 侧 `started ∩ peers = ∅`，按 §D 就一个目标都拉不到，洞永远打不起来。

    可是 Host 手上有另一份**同样可靠**的证据：对端自己发过来的打洞协调行。
    一个正在发 `DIRECT_HIT stage=measured` 的 peer，**就是**已经点了开始连接的
    peer —— 这比任何间接推断都硬。所以信标带上 `from=<我的 client_id>`，
    Host 据此把"谁开始了"补全（见 `room_direct._heard_started`）。
    中继对这条行只做逐字转发，一个字节都不用改。
    """
    from_field = f" from={from_id}" if from_id else ""

    return (
        f"{BEACON_KEYWORD} stage={stage} "
        f"att={attempt_id} batch={int(batch)}{from_field}{to_field or ''}"
    )


def parse_beacon(text):
    """
    解析一条信标；认不出返回 `{}`（**永远不抛**）。

    返回值：`{"stage": str, "att": str, "batch": int|None, "keyword": str}`；
    只有在 `stage=` 出现过时才带 `stage` 键 —— 没有 stage 的 `DIRECT_HIT`
    是**旧语义**（"我打穿了"），不能当成同步信标。
    """
    parts = str(text or "").split()

    if not parts or parts[0].upper() != BEACON_KEYWORD:
        return {}

    out = {"keyword": parts[0].upper()}
    seen_stage = False

    for token in parts[1:]:
        key, _, value = token.partition("=")

        if key == "stage" and value:
            out["stage"] = value
            seen_stage = True
        elif key == "att" and value:
            out["att"] = value
        elif key == "batch" and value:
            try:
                out["batch"] = int(value)
            except ValueError:
                pass
        elif key == "from" and value:
            out["from"] = value

    return out if seen_stage else {}


def parse_sync_fields(text):
    """
    从一条 `DIRECT_CAND` / `DIRECT_PUNCH` 上读 `att=` / `batch=`（加法字段）。

    老客户端不发 -> `{}` -> 调用方跳过同步校验（兼容老端），**不报错**。
    """
    out = {}

    for token in str(text or "").split()[1:]:
        key, _, value = token.partition("=")

        if key == "att" and value:
            out["att"] = value
        elif key == "batch" and value:
            try:
                out["batch"] = int(value)
            except ValueError:
                pass

    return out


__all__ = [
    "Stage",
    "STAGE_ORDER",
    "STAGE_LABELS",
    "PEER_STAGE_LABELS",
    "WAIT_LABELS",
    "STAGE_BY_NAME",
    "MEASURE_STEP_STAGES",
    "BEACON_CONNECTED",
    "PEER_MEASURING_NAT",
    "PEER_MEASURING_INBOUND",
    "PEER_MEASURED",
    "TRANSITIONS",
    "RESET_EDGES",
    "allowed_next",
    "TERMINAL_STAGES",
    "SUCCESS_STAGES",
    "FAILURE_STAGES",
    "CYCLE_KEEP",
    "CYCLE_RESTART",
    "CYCLE_START",
    "LATENCY_NOT_CONNECTED",
    "LATENCY_UNKNOWN",
    "stage_index",
    "attempt_label",
    "ConnTimer",
    "ConnSession",
    "SyncMismatch",
    "select_targets",
    "host_ready",
    "host_availability",
    "HOST_NONE",
    "HOST_NOT_CONNECTED",
    "HOST_READY",
    "HOST_UNKNOWN",
    "host_id_of",
    "started_ids_of",
    "parse_host_state_tokens",
    "host_error_reason",
    "summarize",
    "BEACON_KEYWORD",
    "BEACON_MEASURED",
    "BEACON_CAND_OK",
    "BEACON_NAT",
    "BEACON_INBOUND",
    "beacon_line",
    "parse_beacon",
    "parse_sync_fields",
    "HOST_ID_TOKENS",
    "STARTED_TOKENS",
    "HOST_REASON_CODES",
]
