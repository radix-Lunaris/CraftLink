"""
MC-Link client -- desktop GUI (tkinter).

The GUI is a SHELL. It never implements networking itself: it reads a
ClientConfig from the form, hands it to MCLinkEngine, and renders the events
that come back. That is the rule from the handover document.
"""

import contextlib
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import asdict
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from ..config import settings as cfgmod
from .. import log as logmod
from ..core import protocol as proto
from ..core import punch
from ..core import punch_flow
from ..core import stun as stun_mod
from ..core.common import human_bytes_short, human_rate
from ..core.engine import MCLinkEngine
from . import i18n
from .i18n import tr
from ..core.session import (
    STATE_CONNECTING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_ONLINE,
    STATE_WAITING,
)

# ============================================================
# Theme
# ============================================================
#
# 颜色值来自 theme.py（暗色 / 亮色两套），这里只是把当前生效的那一套取出来
# 当成模块常量。切换主题时 apply_theme() 会就地改写这些全局变量，然后重建
# 窗口 —— 布局代码一个字都不用改，也就没有"改配色改坏功能"的可能。

from . import theme as theme_mod  # noqa: E402  (needs to sit with the palette)

BG = theme_mod.PALETTES["dark"]["BG"]
CARD = theme_mod.PALETTES["dark"]["CARD"]
CARD_ALT = theme_mod.PALETTES["dark"]["CARD_ALT"]
BORDER = theme_mod.PALETTES["dark"]["BORDER"]

FG = theme_mod.PALETTES["dark"]["FG"]
FG_DIM = theme_mod.PALETTES["dark"]["FG_DIM"]
FG_MUTED = theme_mod.PALETTES["dark"]["FG_MUTED"]

ACCENT = theme_mod.PALETTES["dark"]["ACCENT"]
ACCENT_DARK = theme_mod.PALETTES["dark"]["ACCENT_DARK"]
OK = theme_mod.PALETTES["dark"]["OK"]
WARN = theme_mod.PALETTES["dark"]["WARN"]
BAD = theme_mod.PALETTES["dark"]["BAD"]
#: ★ 用户 2026-09-30 第 4 条：NAT 四档的专用颜色（见 `nat_grade_color`）。
#: 名字必须和 `theme.PALETTES` 的键**同名**（GUI 外观测试会逐个核对）。
NAT1 = theme_mod.PALETTES["dark"]["NAT1"]
NAT2 = theme_mod.PALETTES["dark"]["NAT2"]
#: NAT4 两色（用户 2026-10-01）：可预测（递增/递减）与随机（难）。
NAT4_EASY = theme_mod.PALETTES["dark"]["NAT4_EASY"]
NAT4_HARD = theme_mod.PALETTES["dark"]["NAT4_HARD"]
LOCKED = theme_mod.PALETTES["dark"]["LOCKED"]
TILE_ON = theme_mod.PALETTES["dark"]["TILE_ON"]
TILE_OFF = theme_mod.PALETTES["dark"]["TILE_OFF"]
LOG_BG = theme_mod.PALETTES["dark"]["LOG_BG"]
LOG_FG = theme_mod.PALETTES["dark"]["LOG_FG"]
HEADER_BG = theme_mod.PALETTES["dark"]["HEADER"]
SHADOW = theme_mod.PALETTES["dark"]["SHADOW"]


def apply_theme(name):
    """
    切换配色（'dark' / 'light' / 'system'）并立刻套用到模块常量上。

    返回实际生效的配色名。调用方负责重建窗口 —— 只有重建才能让已经画好的
    控件换颜色，而重建不会碰到任何逻辑状态。
    """
    global BG, CARD, CARD_ALT, BORDER, FG, FG_DIM, FG_MUTED
    global ACCENT, ACCENT_DARK, OK, WARN, BAD, LOCKED
    global NAT1, NAT2, NAT4_EASY, NAT4_HARD
    global TILE_ON, TILE_OFF, LOG_BG, LOG_FG, HEADER_BG, SHADOW

    resolved = theme_mod.set_theme(name)
    values = theme_mod.palette()

    BG = values["BG"]
    CARD = values["CARD"]
    CARD_ALT = values["CARD_ALT"]
    BORDER = values["BORDER"]
    FG = values["FG"]
    FG_DIM = values["FG_DIM"]
    FG_MUTED = values["FG_MUTED"]
    ACCENT = values["ACCENT"]
    ACCENT_DARK = values["ACCENT_DARK"]
    OK = values["OK"]
    WARN = values["WARN"]
    BAD = values["BAD"]
    # ★ 用户 2026-09-30 第 4 条：NAT 四档颜色随主题重建（和别的常量同一条路）。
    NAT1 = values.get("NAT1", OK)
    NAT2 = values.get("NAT2", ACCENT)
    NAT4_EASY = values.get("NAT4_EASY", BAD)
    NAT4_HARD = values.get("NAT4_HARD", BAD)
    LOCKED = values["LOCKED"]
    TILE_ON = values["TILE_ON"]
    TILE_OFF = values["TILE_OFF"]
    LOG_BG = values["LOG_BG"]
    LOG_FG = values["LOG_FG"]
    HEADER_BG = values["HEADER"]
    SHADOW = values["SHADOW"]

    return resolved


#: Fixed colour per role, used everywhere a role is shown (client header,
#: client account card, panel header, panel account list) so the same person
#: is the same colour wherever you look at them.
ROLE_COLORS = {
    "root": "#ff9f43",     # 橙黄 -- 根管理员
    "admin": "#3ddc84",    # 绿色 -- 管理员
    "player": "#4c8dff",   # 蓝色 -- 玩家
}

ROLE_LABELS = {
    "root": "根管理员",
    "admin": "管理员",
    "player": "玩家",
}

#: 强调色（ACCENT）【上面】的前景：主按钮的文字、选中复选框里的那个勾。
#:
#: 两套配色里的强调色都是中深蓝，白色文字/白勾在上面都清楚，所以它跟着 ACCENT
#: 走 —— 这不是"写死的白色背景"，而是强调色配套的前景色（主按钮一直就是白字，
#: 这里只是给同一个决定起个名字，免得在新控件里又散落几个 "#ffffff"）。
ON_ACCENT = "#ffffff"

#: 所有输入框共用的 ttk 样式名（在 MCLinkApp._build_styles() 里定义）。
#:
#: 用名字而不是到处写裸字符串：改样式只改一处，而且 grep 这个常量就能查清
#: "到底哪些控件吃到了现代输入框样式"。
ENTRY_STYLE = "Modern.TEntry"

# Fonts went up one point across the board at one stage (12/11/10/22) and came
# back down one notch here, because the user wants the UI to fit the default
# 1040x800 window: "字体稍小一点".
#
# 这一轮（用户："字体稍小一点、颜色饱和度提高"）：正文 12 -> 11，小字 11 -> 10,
# 极小 10 -> 9，标题 22 -> 20，副标题 13 -> 12，等宽 11 -> 10。
# 窗口缩放到 1.3，所以实际观感比数字更大一点。重点内容一律用 FONT_BOLD。
FONT = ("Microsoft YaHei UI", 11)
FONT_BOLD = ("Microsoft YaHei UI", 11, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 10)
FONT_TINY = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 20, "bold")
FONT_SUBTITLE = ("Microsoft YaHei UI", 12, "bold")
FONT_MONO = ("Consolas", 10)

TICK_MS = 150          # GUI event pump interval
UI_REFRESH_MS = 1000   # status panel refresh interval

#: 房间列表的自动刷新周期。列表是"现在谁在哪个房间"的快照，人手点刷新永远
#: 慢一步：别人释放了主机、房间没了、有人刚建房，用户看到的都是旧的。
#: 2 秒一次 = 一个只发几十字节的 ROOMS 查询，肉眼看是实时的。
ROOM_LIST_REFRESH_MS = 2000

MAX_LOG_LINES = 500

MODE_RELAY = "relay"
MODE_STUN = "stun"
MODE_AUTO = "auto"

#: 卡片圆角半径（像素）。
#:
#: 这个数字不只控制"看起来圆不圆"：内容层必须内缩同样的距离，否则方角会把
#: 圆角整个盖住（见 Card 的注释）。
#:
#: 用户看了第一版说"可以再做大一点，面板里那些看板的圆角状态就不错"，所以从
#: 16 提到 22 —— 比系统卡片明显，但不至于圆成胶囊。
CARD_RADIUS = 22

#: 瓷砖（连接模式 / 身份）的圆角半径。比卡片小一点，看起来更"按钮"。
TILE_RADIUS = 14

#: 卡片"阴影"往右下偏多少像素（纯观感，见 Card._redraw）。
SHADOW_OFFSET = 3


# ============================================================
# Small helpers
# ============================================================

def human_bytes(value):
    """Kept as a thin alias so older call sites keep working."""
    return human_bytes_short(value)


# ============================================================
# 网络诊断（V3）：把 stun.NatProfile 变成界面能显示的行
# ============================================================
#
# 这一段刻意放在模块级、而且**不碰任何 tkinter 控件**，原因有两个：
#
#   1. 它是纯数据变换，可以直接离线验证（tests/nat_diag_test.py 会调它）；
#      塞进按钮回调里就只能靠"点一下看看"来验证了。
#   2. 界面文案必须走 tr()。这里的每个字段名就是翻译表的键，加一行就自动
#      带翻译，不会出现"英文界面上诊断结果还是中文"。

def nat_diag_status_values(profile, snapshot, mode):
    """
    The two live rows that replaced the standalone diagnostics card.

    Returns {"nat": (text, colour), "direct": (text, colour)}.

    Why rows and not a card: the card had its own button, its own worker and
    its own frozen state, and it told the user nothing while a connection was
    actually being attempted. These read the SAME snapshot the rest of the
    status card reads, so "what my NAT is" and "what the punch is doing" sit
    next to "am I connected" instead of in a separate panel that could go
    stale on its own.
    """
    colours = {"ok": None, "warn": None, "bad": None, "dim": None}

    # ---- 网络类型 ----
    if profile is None:
        nat_text = tr("未检测")
    elif profile.nat_type is not None:
        # ⑰ 细分：锥型按过滤那一档分成 NAT1/NAT2/NAT3，对称按分配规律分 NAT4。
        grade = _filter_grade(
            snapshot.get("inbound_state"),
            getattr(profile, "filtering", None),
            bool(snapshot.get("eif_proof")),
        )
        detailed = nat_grade_text(profile.nat_type, grade)[0]
        nat_text = tr(detailed)

        # 明细附在后面：别人读到的是"你的网络到底属于哪一类、为什么"。
        #
        # ★ 用户 2026-10-01：`adf_or_eif` 那一档**不要**再挂"入站未定"的尾巴 ——
        #   它和主文案是同一件事，屏幕上会变成
        #   「NAT1/2 未定（全锥或地址受限）· 入站未定（全锥或地址受限）」，
        #   又长又重复（用户原话："这样…反而是不太好用了"）。
        #   换成一句"它会自己变"的提示。
        # ★ 用户 2026-10-01：**入站那一栏才是入站策略**。NAT 类型这一行只在
        #   "入站档位"**真的测出来了**（eif/adf/apdf）时才带那个后缀；否则
        #   （未定/未测）什么都不挂 —— 以前会出现"NAT 类型已经写了入站策略，
        #   可入站档位那一栏还写着入站未测"的自相矛盾。
        # ★ 用户 2026-10-03：只要入站档位**测出来了**（宽松/中等/严格）就挂后缀，
        #   与「入站档位」那一栏用同一张表 -> 两行永远一致（不许一边有、一边"未测"）。
        if grade in ("eif", "adf", "apdf") and "NAT" in detailed:
            nat_text = f"{nat_text} · {tr(stun_filter_label(grade))}"
    else:
        nat_text = tr("未检测")

    # ---- 直连尝试 ----
    #
    # ONE story, from ONE dict. This row and 打洞进度 (punch_step_text) used to
    # derive their text separately, which is how the owner ended up looking at
    # "已直连" next to "还未尝试" at the same moment: the two renderers read
    # different objects (the room's punch vs the session's snapshot) and each
    # applied its own default. Both now consume the same merged view, and the
    # unknown case says the same thing in both places.
    via = snapshot.get("direct_via")
    attempts = snapshot.get("direct_attempts") or 0
    peer = snapshot.get("direct_peer") or ""

    if mode not in (MODE_STUN, MODE_AUTO):
        direct_text = tr("未启用")
    elif via == "udp":
        direct_text = (
            tr("已直连 {0}").replace("{0}", peer) if peer else tr("已直连")
        )
    elif snapshot.get("stun_ready"):
        # The punch SUCCEEDED. The user's rule, verbatim: 打通了就是长连接，
        # 不等待任何游戏/进程。So the row says the link is UP -- it must NOT say
        # "waiting for the game", because the game is an afterthought that rides
        # an already-established tunnel (and may never come at all).
        direct_text = tr("已直连（长连接保持中）")
    elif attempts:
        # Includes the "failed" case: STUN mode never falls back to the relay,
        # so the honest answer is "still trying", with the count.
        direct_text = tr("打洞中，已尝试 {0} 次").replace("{0}", str(attempts))
    else:
        # Nothing has been attempted yet. Say exactly what punch_step_text says
        # for the same state, so the two rows cannot disagree.
        direct_text = tr("等待开始（点「开始连接」后打洞）")

    return {
        "nat": (nat_text, nat_grade_color(profile, snapshot)),
        "direct": (direct_text, FG),
    }


def nat_grade_color(profile, snapshot):
    """
    NAT 那一行的颜色（用户 2026-09-30 第 4 条，**纯显示**）：

        NAT1 全锥形        -> 青绿
        NAT2 地址受限锥形  -> 天蓝
        NAT3 端口受限锥形  -> 橙黄（WARN）
        NAT4 对称型        -> 红（BAD）

    判据和 `nat_grade_text()` 完全同源（映射 + 过滤两轴），所以颜色永远不会
    和文字打架。测不出来 / 没有画像时给默认前景色（不上色 = 不假装）。
    """
    if profile is None or getattr(profile, "nat_type", None) is None:
        return FG

    from ..core import punch as punch_mod

    sub = str(getattr(profile.nat_type, "value", profile.nat_type) or "")

    if sub in ("sym_easy_inc", "sym_easy_dec"):
        # 递增 / 递减 = 可预测：还能用生日悖论那套打 -> 用"还有救"的洋红。
        return NAT4_EASY

    if sub == "sym_hard":
        # 随机 = 难：最坏 -> 红。
        return NAT4_HARD

    if sub not in ("cone", "NAT_SUB_CONE"):
        return FG

    grade = _filter_grade(
        snapshot.get("inbound_state"),
        getattr(profile, "filtering", None),
        bool(snapshot.get("eif_proof")),
    )

    # ★ 用户 2026-10-02：NAT1/2 合并 -> 三档颜色（NAT1/2 天蓝、NAT3 橙黄、
    #   NAT4 红/洋红）。全锥与地址受限不再用两种颜色区分。
    return {
        "eif": NAT2,
        "adf": NAT2,
        "adf_or_eif": NAT2,
        "apdf": WARN,
    }.get(str(getattr(grade, "value", grade) or ""), FG)


def _filter_grade(inbound_state, measured_filter, eif_proof=False):
    """
    入站档位：**只由 STUN 的过滤测量决定**（用户 2026-10-03 最终口径）。

        换 IP 也答回来（eif）        -> 入站宽松
        换端口答了 / 地址受限（adf）  -> 入站中等
        adf_or_eif（单 IP 测不了换 IP，但已证明**不是**端口受限） -> **入站中等**
        换源全被丢（apdf）           -> 入站严格
        没测到                       -> 入站未测

    用户原话：「入站就是由 STUN 测的，入站就只有中等、严格、宽松三个判断挡位，
    不要搞什么中等或更好，两边不统一」。所以：
      * 展示层把 `adf_or_eif` **折成中等**（数据层保留它，日志可追溯）；
      * `inbound_state`（中继入站探测）与 `eif_proof`（对端首包）**都不再改档位** ——
        它们的源地址/依据不是 STUN 的换源应答，混进来就会出现"NAT 行写入站宽松、
        入站行写中等/严格"这种自相矛盾（分析实测 16/20 组不一致）。
    """
    measured = getattr(measured_filter, "value", measured_filter) or "unknown"

    if measured == "adf_or_eif":
        return "adf"

    if measured in ("eif", "adf", "apdf"):
        return measured

    return "unknown"

def inbound_status_text(snapshot, measured_filter=None):
    """
    入站这一档：**照抄朋友的方案，一个字都不自己造。**

    ============================================================================
      他只有四个挡位（他 protocol.py 的 NAT_FILTER_LABEL）
    ============================================================================

        入站宽松 (eif) / 入站中等 (adf) / 入站严格 (apdf) / 入站未测 (unknown)

    而且他不把"入站探测"当成单独一列，而是把它和 STUN 判出来的过滤行为
    **合成一个挡位**（他 natpunch.effective_filter —— 已经原样搬进我们的
    `core/punch.py`，这里只是调用它）：

        measured  STUN 的结论        eif / adf / apdf / unknown
        inbound   中继探测的结论      open / punchable / blocked / unknown

    我们界面上原来那句「可达（陌生主机能打进来）」「可打（出方向已放行…）」
    「不可达（入方向被挡）」是上一轮**自己编的** —— 他项目里没有这些词。
    现在直接显示他那一档，英文提示也不再往这一行上贴（他的界面从不贴）。

    顺带说清楚两个容易误会的结论：
      * 在"STUN 忽略 CHANGE-REQUEST（measured 只能是 unknown）+ 探测没到达"
        的组合下，**他的算法本身给的就是「入站未测」** —— 因为一次"没到达"
        只能排除 eif，不能证明 adf。这不是我们的 bug，是他的设计。
      * **「探测到达」不等于「入站宽松」**：中继的探测包源地址就是中继那个
        公网 IP，而我们早就为测 STUN 往它发过包了 —— 地址受限(adf)的 NAT 也
        会放行。到达只能证明"不是端口受限"。这一条 2026-09-25 按用户实测修正
        （参考工具报 NAT3=端口受限，即"严格"）。

    Returns (text, colour_hint).
    """
    from ..core import punch as punch_mod
    from ..core import stun as stun_mod

    # `measured_filter` 通常是 `punch.Filter` 枚举，而他的 effective_filter
    # 是按**字符串**比较的（"eif"/"adf"/…），所以先取 .value。
    measured = getattr(measured_filter, "value", measured_filter) or "unknown"
    inbound = snapshot.get("inbound_state") or "unknown"

    # ============================================================================
    #  挡位怎么合成：见 _filter_grade（2026-09-25 按用户实测修正）
    # ============================================================================
    #
    # 上一轮这里的规则是"探测到达 ⇒ 一律 eif（宽松）"，理由是"朋友的客户端
    # 显示宽松、我们显示中等，所以是我们错"。**这条规则本身是错的**，用户后来
    # 拿参考工具（mao.fan/mynat、比特彗星）核实：他的网络是 **NAT3 = 端口受限
    # = apdf = 严格**。也就是说宽松（朋友）和中等（当时的我们）都不对。
    #
    # 为什么"到达"证明不了 eif：中继的探测包源地址就是**中继那一个公网 IP**，
    # 而我们为了测 STUN 早就往那个 IP 发过包了。地址受限（adf）的 NAT 只要
    # "发过这个地址"就放行 → 一样会到达。所以"到达"只能证明**不是 apdf**。
    # eif 必须靠 RFC 5780 的**换 IP**那一项（需要一台有第二个 IP 的 STUN）。
    #
    # 现在的中等/严格是这样来的（都在 _filter_grade 里，这里是显示层）：
    #   * 换端口测得 apdf（服务器动了端口、包没进来）→ 严格，硬证据；
    #   * 换端口测得 adf 或探测到达（最多 adf）      → 中等；
    #   * eif 只有换 IP 的应答真从另一个 IP 回来时才会出现 → 宽松。
    grade = _filter_grade(inbound, measured)

    label = stun_mod.NAT_FILTER_LABEL.get(
        grade, stun_mod.NAT_FILTER_LABEL["unknown"]
    )

    hint = {
        "eif": "ok",
        "adf": "warn",
        # ★ 用户 2026-10-02：换端口已答（非端口受限）—— 比"未定"更靠前的一档，
        #   用"中等"的颜色（它至少是这一档或更好）。
        "adf_or_eif": "warn",
        "apdf": "bad",
    }.get(grade, "dim")

    return (label, hint)


# ============================================================
# 房间成员列表（3.0.9）：把 RoomState + 每人一条的直连状态
# 变成一个"能直接画成表格"的纯数据列表
# ============================================================
#
# 和 nat_diag_status_values 一样，这一段刻意是**模块级纯函数、不碰任何
# tkinter 控件**：
#
#   * 数据变换可以离线验证（tests/client_kick_roster_test.py 直接调它，
#     不需要图形环境）；
#   * 界面只负责把每一行画出来，不参与任何"谁是谁"的推断；
#   * **拿不到的一律留空**，由渲染层显示 未知 / `-`，绝不编造。

#: 成员在这个房间里的身份（渲染层再翻译成中文/英文/日文）。
ROLE_OWNER = "owner"
ROLE_HOST = "host"
ROLE_PLAYER = "player"
ROLE_UNKNOWN = "unknown"

#: 队伍成员列表的列（键, 中文表头, 宽度）。渲染与自检共用同一份定义。
MEMBER_COLUMNS = (
    ("name", "用户名", 14),
    ("role", "身份", 6),
    ("status", "连接状态", 8),
    ("nat", "NAT 类型", 16),
    ("inbound", "入站档位", 10),
    ("addr", "IP:端口", 20),
    ("ping", "延迟", 8),
    # 「对端延迟」：玩家 ↔ 主机 的直连往返时间。适用（STUN 直连且链路已打通）
    # 才显示数字，不适用显示 N/C —— 口径见 build_member_rows 的说明。
    ("peer_ping", "对端延迟", 9),
    ("kick", "踢出", 6),
)


def role_text(role):
    """ROLE_* -> 中文原文（渲染时再过 tr()）。未知 = 真未知，不猜。"""
    return {
        ROLE_OWNER: "房主",
        ROLE_HOST: "主机",
        ROLE_PLAYER: "玩家",
    }.get(role, "未知")


def mode_switch_should_reconnect(was_live, running):
    """
    热切换之后，这个成员要不要**自动重连**（用户第 3 条的精确语义）。

    ============================================================================
      ★ 判据是"**我自己**切换前有没有连接"，不是"房间里有没有人连接"
    ============================================================================
    用户原话：「不论什么模式切什么模式，第几次切，都所有客户端严格重建隧道
    并将切换时处于已连接态的玩家自动重连，**空闲态的保持**」。

    而中继的 `was_live=0|1`（PROTOCOL §8.9）是**房间级**的一次性事实
    （`server/room_members.py`：切换前 `conn=` 里出现过 connected/pairing 就是
    1）。所以：

      * `was_live == "0"` —— 比本地 `running` 更硬的**否决**依据：整个房间当时
        都没连接，那我肯定也没连接 -> 只换模式，**不重连**（保留原行为）；
      * `was_live == "1"` —— **只说明"房间里有人连过"，不能据此替我决定**：
        我可能正是那个空闲的人。此时以自己的 `running`（本机会话是不是在跑）
        为准 —— 这才是"空闲态的保持"；
      * 拿不到（老中继）-> 同样以本地 `running` 为准（旧行为，一字不差）。
    """
    value = str(was_live or "").strip()

    if value == "0":
        return False

    return bool(running)


#: `SESSION` 探活判定"房间身份已结束"（`auto_reconnect=0`）时，reason 码 ->
#: 界面文案的映射。用户的规则（PROTOCOL §8.10 / 设计基准 §C）：
#:   KICKED / LEFT / TIMEOUT / ROOM_CLOSED / AUTH_* -> 按退出房间处理。
#: 只用**已有**的翻译 key（不新增界面字符串，i18n 覆盖测试不用改）。
def session_end_notice(code):
    """
    reason 码 -> (标题, 正文)，两段都已 `tr()`。

    未知码给通用文案：宁可说"你已离开房间"，也不编一个具体原因。
    """
    code = str(code or "").strip().upper()

    if code == "KICKED":
        return (
            tr("你已被踢出房间"),
            tr("你已被踢出房间。") + "\n\n" + tr("你已被移出该房间，已自动退出。"),
        )

    if code == "ROOM_CLOSED":
        return (
            tr("房间已被管理关闭"),
            tr("你已经自动退出该房间，可以直接重新创建或加入房间。"),
        )

    return (
        tr("房间已被关闭"),
        tr("你已经自动退出该房间，可以直接重新创建或加入房间。"),
    )


def member_roles(is_owner, is_host):
    """

        房主·主机 = Owner + Host      房主·玩家 = Owner（不认领 Host）
        玩家·主机 = Host（非 Owner）   玩家      = 普通成员

    Owner 是"账号与房间的绑定"，Host 是"当前房间角色"，两者**可以同时成立**，
    所以这里返回的是列表而不是一个枚举；也**绝不**用 `conn=`（连接状态）来推。
    """
    if is_owner and is_host:
        return [ROLE_OWNER, ROLE_HOST]

    if is_owner:
        return [ROLE_OWNER, ROLE_PLAYER]

    if is_host:
        # ★ 用户 2026-10-03 的**冻结口径**（成员列表「身份」列）：
        #     房主            -> 房主
        #     房主 + 主机      -> 房主·主机
        #     房主 + 玩家      -> 房主·玩家
        #     普通成员当主机    -> **主机**（不是"玩家·主机"）
        #     普通玩家        -> 玩家
        #   所以非房主的 host 只显示「主机」—— 房主那一份身份他不具备。
        return [ROLE_HOST]

    return [ROLE_PLAYER]


def host_conn_state(state):
    """
    当前 Host 的**连接状态**（与"主机位在不在"完全分开的两套状态之一）。

    用户 2026-09-29：「连接和身份是两回事」——
    主机位在不在 = 身份事实（`host=` / `host_claimed`，中继广播）；
    主机连没连 = 连接事实（`conn=<cid>:<state>` PROTOCOL §8.5，或 `started=`）。

    返回 `"connected"` / `"idle"` / `"unknown"`：

      * `conn=connected` -> connected（§8.5：Host 正在开服 / 已进世界）；
      * `conn=idle`      -> idle（明确没连）；
      * `waiting|pairing`-> 中继还没说它"连上"，退回 `started=` 判；
      * `started=` 里有它 -> connected；明确说没有 -> idle；
      * 拿不到（老中继）  -> unknown（**绝不**因此显示成"未开服"）。
    """
    if state is None:
        return "unknown"

    host = punch_flow.host_id_of(state)

    if not host:
        return "unknown"

    conns = getattr(state, "member_conns", None) or {}
    value = str(conns.get(host) or "")

    if value == "connected":
        return "connected"

    if value == "idle":
        return "idle"

    started = punch_flow.started_ids_of(state)

    if started is None:
        return "unknown"

    return "connected" if host in set(started) else "idle"


def roles_text(roles):
    """
    **身份**这一列的正文：一个成员可以同时是多个角色（用户 2026-09-29 强调）。

    用户原话：「host 只是身份，房主可兼职 host 也可是普通玩家」——
    Owner / Host / Player 是"账号 + 房间角色"，**不是互斥的三种人**。
    所以这一列显示组合：

        房主·主机（Owner 且是 Host）  房主·玩家（Owner 不认领 Host）
        玩家·主机（非 Owner 的 Host） 玩家（普通成员）

    渲染是"逐段 tr() 再拼"，三语言各自成立（`房主/主机/玩家` 都有译）。
    """
    items = [r for r in (roles or ()) if r]

    if not items:
        return tr("未知")

    return "·".join(tr(role_text(r)) for r in items)


#: `conn=` 状态 -> 成员列表「连接状态」列的中文原文（渲染时再过 tr()）。
#:
#: 用户 2026-09-28：「这边玩家断开连接，那边 host 这个连接状态还是显示已连接，
#: 未及时更正状态」—— 这一列现在以中继广播的 `conn=` 为唯一真相（PROTOCOL §8.5）。
#: 老中继没有这个 token 时 `conn_state` 是空串 -> 回退旧行为（房间里 = 已连接）。
CONN_STATE_LABELS = {
    "idle": "未连接",
    "waiting": "等待配对",
    "pairing": "配对中",
    "connected": "已连接",
}


def mode_owner_label_text(room_mode, is_owner, in_room):
    """
    连接模式卡片下面那行说明（**纯函数**，用户 2026-09-30 第 3 条）。

    * 不在房间 -> 说明"还没加入房间"；
    * 我是房主 -> "由你决定"；
    * 别人（含非房主的 host）-> "由房间所有者决定" + **当前是哪种模式**。

    用户原话：「不能换模式时对的，但是做成那种就是同步当前房主模式选择器给我
    展示出来呀，只是我换模式点击不允许那么操作而已，不要全部未开放啊」——
    所以非房主**看得见当前模式**，只是点不动（点击的拒绝在 `_on_mode_change`）。
    """
    if not in_room or room_mode not in (MODE_RELAY, MODE_STUN, MODE_AUTO):
        return tr("尚未加入房间：模式在你建房或成为房主后生效")

    if is_owner:
        return tr(
            "连接模式由你决定：切换房间模式会中断当前游戏连接；"
            "AUTO 内部的路径切换由后台自动完成"
        )

    current = {MODE_RELAY: "Relay 中转", MODE_STUN: "STUN 直连", MODE_AUTO: "AUTO 自动"}[room_mode]

    return (
        tr("连接模式由房间所有者决定，其他成员不能修改")
        + tr("（当前：")
        + tr(current)
        + tr("）")
    )


def _member_status_text(row):
    """
    成员列表「连接状态」那一格的正文（已 tr()）。

    判据顺序：

      1. ★ 中继说 `idle`，但**我们和他之间已经打通的直连洞还活着**
         （`peer_direct`，STUN 模式下的 P2P 通道）-> 「已连接」。
         用户 2026-09-30 第 3 条实测："部分已经连接的玩家状态被错误地打印为
         未连接"。根因就是这一格只认中继的 `conn=` —— 而 STUN 模式下游戏走的是
         **P2P 通道**，中继那条会话结束之后 `conn=` 就变 `idle`，可他和我们之间
         的洞还在搬数据（"洞已打通且在保活" 是**硬事实**，不是猜）。
      2. `conn_state` 在 4 个白名单值里 -> 用它的原文（未连接 / 等待配对 /
         配对中 / 已连接）；
      3. 老中继（空）-> 旧行为：`connected` 为真写「已连接」，否则「未知」。

    **纯显示**：不改 `conn=` 的口径，也不碰任何连接逻辑（规则 6 §15）。
    """
    state = str((row or {}).get("conn_state") or "")

    if state == "idle" and bool((row or {}).get("peer_direct")):
        return tr(CONN_STATE_LABELS["connected"])

    if state in CONN_STATE_LABELS:
        return tr(CONN_STATE_LABELS[state])

    return tr("已连接") if (row or {}).get("connected") else tr("未知")


def hole_row_text(item, fallback_name=""):
    """
    逐目标一行的正文 + 颜色语义（**纯函数**，界面上和测试里用的是同一份）。

    ============================================================================
      用户 2026-09-28 的两条修正（第 1/2 条）
    ============================================================================

      1. `【…】` 那一格是**对端真实名字**（中继广播的 `names=`；拿不到才
         client_id）。设计文档里的 `【A】` 只是占位示例 —— **不是计数、不是名字**。
         尝试次数用「第 N 次」，不用 `n/m`、不用字母。
      2. 打通那一行必须带**打通的公网地址:端口**：
         `P2P 已直连【玩家名】223.166.20.9:40879 18ms`。

    数据来自 `RoomDirectLink.link_rows()` 的 `row_parts`（片段），每一段各自
    `tr()`，所以中/英/日都成立（英文界面不会残留全角括号）。

    Returns (text, colour_hint)，colour_hint ∈ {"ok", "warn", "muted"}。
    """
    item = item or {}
    parts = item.get("row_parts") or {}
    shown_name = str(item.get("name") or fallback_name or item.get("peer") or "")
    latency = peer_latency_text(
        item.get("rtt_ms"), bool(item.get("ready"))
    )

    if parts and parts.get("stage"):
        head = tr(parts.get("stage") or "")

        # ============================================================
        #  用户 2026-09-29（显示升级）：打洞那一行带 `PUNCH #0012`
        # ============================================================
        # 它和基线要求的「第 N 次」**并存**（两个都要，谁也不替谁）：
        #     `打洞中 PUNCH #0003（5s）【玩家甲】第 3 次 · N/C`
        # 计数由核心给（`punch_number`，这一轮的次数），界面只负责 `tr()` 模板 ——
        # 三语言各自成立。
        punch_number = int(parts.get("punch_number") or 0)

        if punch_number:
            head += " " + tr("PUNCH #{0}").replace(
                "{0}", "%04d" % punch_number
            )

        if parts.get("seconds") is not None:
            head += tr("（{0}s）").replace("{0}", str(int(parts["seconds"])))

        # 名字那一格（不是轮次字母）。
        head += tr("【{0}】").replace("{0}", str(parts.get("name") or shown_name))

        punch_count = int(parts.get("punch_count") or 0)

        if punch_count:
            return (
                head
                + tr("第 {0} 次").replace("{0}", str(punch_count))
                + " · "
                + latency,
                "ok" if parts.get("connected") else "warn",
            )

        addr = str(parts.get("addr") or "")
        text = head + (
            f"{addr} " if (parts.get("connected") and addr) else ""
        ) + latency

        return (text, "ok" if parts.get("connected") else "warn")

    if item.get("ready"):
        return (
            f"{tr('直连')}【{shown_name}】{item.get('addr') or '—'} "
            f"{tr('已打通')} · {latency}",
            "ok",
        )

    attempts = int(item.get("attempts") or 0)

    # 一次都没打过 = 还没开始，不是"正在打洞"。这一行只在 STUN + 正在打洞时才
    # 应该存在（见 `_render_hole_rows` 的门），所以这里的措辞是兜底。
    if attempts:
        return (
            f"{tr('打洞中')}【{shown_name}】"
            f"{tr('第 {0} 次').replace('{0}', str(attempts))}"
            f" · {latency}",
            "warn",
        )

    return (f"{tr('未打洞（中转模式）')}【{shown_name}】 · {latency}", "muted")


def p2p_rtt_text(rtt_ms):
    """
    对端直连 RTT 的显示口径（**只读**，审计问题 5）。

        * 没测到（还没完成第一次 keepalive 往返）  -> `-`
        * 亚毫秒（真实测量，只是很短）              -> `<1 ms`
        * 其余                                     -> `NN ms`

    ============================================================================
      为什么亚毫秒不能显示成 `0 ms`
    ============================================================================
    审计问题 5：用户看到"对端延迟一会儿 0ms、一会儿十几 ms"。日志里没有 0，
    但显示层有两条会把**真实存在的亚毫秒**写成 `0` 的路：对端延迟用
    `f"{x:.0f} ms"`、成员列表也用 `.0f`。`0` 和"没测到"混在一起就没人能判断
    这是"测量抖动"还是"根本没量"。

    所以亚毫秒单独标 `<1 ms`：它仍然说明"量到了，而且很近"，但不再是那个
    看起来像坏掉的 0。注意：0.0 本身是**合法**值（本机回环），这里也一并
    归到 `<1 ms` —— 不丢信息，只是不再误导。
    """
    if rtt_ms is None:
        return "-"

    with contextlib.suppress(TypeError, ValueError):
        value = float(rtt_ms)

        if value < 1.0:
            return tr("<1 ms")

        return f"{value:.0f} ms"

    return "-"


def peer_latency_text(rtt_ms, applicable):
    """
    「对端延迟」这一格的显示口径（成员列表和运行状态卡片共用同一个函数）。

        * 不适用（relay 模式 / 未连接 / 洞没打通）      -> `N/C`
        * 适用（STUN 直连且链路已打通）但还没测出数字    -> `-`
        * 适用且量到了真实往返时间                       -> `NN ms` / `<1 ms`

    `N/C` = not connected / not applicable：直连没成立时，**根本没有**"玩家↔
    主机"的延迟可量，硬塞一个中转的数字或一个猜测都是错的。适用时那个数字来自
    隧道自己的 keepalive 往返（见 core/udptunnel.py 的只读插桩）。
    """
    if not applicable:
        return tr("N/C")

    return p2p_rtt_text(rtt_ms)


def build_member_rows(state, direct_rows=(), self_profile=None,
                      self_inbound_state="", self_ping_ms=None,
                      addr_rows=None, eif_proof=False):
    """
    把房间状态 + 每人一条的直连行拼成成员列表的**数据**。

    ============================================================================
      Data sources (nothing is invented)
    ============================================================================

      * 自己            RoomState（username / is_owner / is_host / client_id）
      * 其他成员        HOST_STATE 的 `peers=`（中继发的 client_id 列表）
      * 每人的显示名     HOST_STATE 的 `names=`（中继广播：登录名 / 游客名），
                        自己的优先用 OK 回包的 `you=`。**老中继不发这两样时
                        回退到 client_id** —— 绝不显示「未知」或空白。
      * 房主是谁        HOST_STATE 的 `host_name=`（中继的权威显示名）
      * 每人的 NAT / 入站 / IP:端口
                        DIRECT_CAND 的 `sub=` / `filt=` / 公网地址，经
                        DirectLink.parse_candidate 解析、RoomDirectLink 按
                        peer 存好（见 room_direct.link_rows）。
      * 每个人的中转延迟 HOST_STATE 的 `rtts=`（中继自己量出来的）；自己的用
                        成员连接 PING/PONG 回声量出来的真实 RTT。
      * 对端延迟        DIRECT 链路隧道 keepalive 的往返时间（room_direct 的
                        `rtt_ms`）。**适用 = STUN 直连且该 peer 链路已打通**；
                        不适用（relay 模式 / 未连接 / 未打通）-> None，渲染层
                        显示 `N/C`；适用但还没测出来 -> 渲染层显示 `-`。

    对端身份：中继现在会广播每人的显示名（`names=`），所以对端用户名不再是
    「未知」。只有在**老中继**（没有 `names=`）时才回退：
      * 房主的显示名来自 `host_name=`（老中继唯一的真用户名）；
      * 其余对端回退到 client_id（我们确实知道的身份）。

    哪一行是房主：
      * 自己不是所有者时，`peers=` 是按 room.members 的插入顺序生成的，而房间
        的寿命绑在所有者那条成员连接上 —— 所以所有者永远是第一个加入的人，
        即 `peers[0]`。这是结构事实，不是猜测。
      * 自己在 DIRECT_CAND 里收到某对端带 `to=` 的行时，那个对端就是当前主机
        （`to=` 按协议只有主机才发，见 direct.py::_to_field）。

    返回 [{key, is_self, name, role, connected, status, nat, inbound, addr,
           ping_ms, peer_rtt_ms, peer_direct, can_kick, detail}]
    """
    state = state if state is not None else None

    if state is None:
        return []

    get = (lambda obj, key, default=None: getattr(obj, key, default))

    self_id = get(state, "client_id", "") or ""
    is_owner = bool(get(state, "is_owner", False))
    is_host = bool(get(state, "is_host", False))
    host_claimed = bool(get(state, "host_claimed", False))
    host_name = (get(state, "host_name", "") or "").strip()
    username = (get(state, "username", "") or "").strip()

    # 中继广播的两张表（ADDITIVE）。老中继不发 -> 空表，下面全部走回退。
    member_names = dict(get(state, "member_names", None) or {})
    member_rtts = dict(get(state, "member_rtts", None) or {})
    #: 每成员**此刻的游戏连接状态**（`conn=`，PROTOCOL §8.5）。用户 2026-09-28：
    #: 玩家断开后 host 那边必须立刻不再显示「已连接」——这一列以它为准。
    member_conns = dict(get(state, "member_conns", None) or {})

    # 我自己的名字：中继在 OK 回包里给的 you= -> names= 里按 client_id 查 ->
    # 登录用户名 -> client_id。**永远不为空**，游客也有中继分配的名字。
    own_name = (get(state, "own_name", "") or "").strip() or username or self_id

    direct = {}

    for row in (direct_rows or ()):
        peer = (row or {}).get("peer") or ""

        if peer:
            direct[peer] = row

    # ============================================================================
    #  ★ 用户 2026-09-30：每个对端**自己广播**的公网端点（`DIRECT_ADDR`）
    # ============================================================================
    #
    # 直连模式下以前只有 host 能看到玩家的候选，玩家之间互不可见 —— 成员列表
    # 的 `IP:端口` 那一列对大部分人都是 `-`。现在每个人都把自己测量到的公网
    # 端点广播给全房（纯显示，不进打洞状态机），这里用它兜底。
    #
    # `direct` 里那条（真正用于打洞的地址）优先；没有才用广播的端点。
    addrs = {
        str(k): str(v)
        for k, v in dict(addr_rows or {}).items()
        if k and v
    }

    # 自己的 NAT / 入站 / 公网地址：本地测出来的那一份。
    if self_profile is not None:
        nat_text = nat_grade_text(
            get(self_profile, "nat_type"), get(self_profile, "filtering")
        )[0]
        # ★ 用户 2026-10-01 修 P0：这里以前误写成 `self._direct_status_view(...)`，
        #   而 `build_member_rows` 是**模块级函数**、根本没有 `self` -> NameError
        #   -> 成员窗口的 except 直接把它关掉（"打开就空了"）。证据改由调用方
        #   通过 `eif_proof=` 传进来（和 `addr_rows` 同一条路）。
        grade = _filter_grade(
            self_inbound_state, get(self_profile, "filtering")
        )
        inbound_text = stun_filter_label(grade)
        public = get(self_profile, "public")
        self_addr = f"{public[0]}:{public[1]}" if public else ""
    else:
        nat_text = ""
        inbound_text = ""
        self_addr = ""

    # 我自己的中转延迟：优先用**本机** PING/PONG 回声量出来的那一个（最贴近
    # "我这条路"）；本机还没量到时用中继广播的 `rtts=` 里属于我的那个值（那
    # 也是中继真测出来的同一条路）。两个都没有才是 None -> 界面显示 `-`。
    self_ping = (
        self_ping_ms
        if self_ping_ms is not None
        else member_rtts.get(self_id)
    )

    self_conn = member_conns.get(self_id)
    # ============================================================
    #  ★ 身份 = 一组角色，不是一个枚举（用户 2026-09-29 的"三条原则"）
    # ============================================================
    #  `role` 保留成"主角色"（房主 > 主机 > 玩家）只为兼容旧调用方；
    #  显示走 `roles`（可以同时是「房主·主机」）。
    #  身份**绝不从 `conn=`（连接状态）推导** —— 那是两套状态。
    self_roles = member_roles(is_owner, is_host)

    rows = [{
        "key": self_id or "__self__",
        "is_self": True,
        "name": own_name,
        "role": (
            ROLE_OWNER if is_owner else (ROLE_HOST if is_host else ROLE_PLAYER)
        ),
        # 组合身份（显示用；见 roles_text 的说明）。
        "roles": list(self_roles),
        # 自己这一行也跟广播走（用户第 7 条：这一列以 `conn=` 为唯一真相）。
        # 老中继没有这个 token（`self_conn is None`）-> 旧行为：在房间里 = 已连接。
        "connected": True if self_conn is None else self_conn != "idle",
        "conn_state": str(self_conn or ""),
        "status": "online",
        "nat": nat_text,
        "inbound": inbound_text,
        "addr": self_addr,
        "ping_ms": self_ping,
        # 对端延迟是「玩家 ↔ 主机」：我自己这一行没有对端，不适用。
        "peer_rtt_ms": None,
        "peer_direct": False,
        "can_kick": False,
        "detail": self_id,
    }]

    peers = [
        p for p in (get(state, "peer_ids", None) or [])
        if p and p != self_id
    ]

    # ============================================================
    #  Owner 是谁：**只认中继说的事实，绝对不猜**（AGENTS.md 规则 6 §A）
    # ============================================================
    #
    # 这里以前是 `owner_peer = peers[0]` —— "花名册里第一个对端就是房主"。
    # 那和 BUG 1 是同一种错误（把**顺序**当成**身份**）：
    #   * host 那一行被贴上「房主」（用户报的"host 错误继承了房主的位置/身份"）；
    #   * 主机/房主的踢人按钮指向了错的人。
    #
    # 现在：
    #   * 我自己是 owner -> 对端里没有 owner；
    #   * 中继发了 `owner_id=`（ADDITIVE）-> 精确匹配那一行；
    #   * 中继没发（老中继）-> **谁都不标「房主」**，对端只显示「主机 / 玩家」。
    #     不标比标错便宜得多：标错会让用户以为身份被继承了。
    owner_peer = "" if is_owner else str(getattr(state, "owner_id", "") or "")

    if owner_peer and owner_peer not in peers:
        owner_peer = ""

    for peer in peers:
        info = direct.get(peer) or {}

        peer_is_host = bool(info.get("peer_is_host"))
        peer_is_owner = bool(peer == owner_peer)
        role = (
            ROLE_OWNER if peer_is_owner
            else (ROLE_HOST if peer_is_host else ROLE_PLAYER)
        )
        # ★ 用户 2026-09-29：「房主可兼职 host」—— Owner 与 Host 不互斥，
        # 身份是一个**组合**（房主·主机 / 房主·玩家 / 玩家·主机 / 玩家）。
        roles = member_roles(peer_is_owner, peer_is_host)

        # 保守：主机位有人占、但我拿不到它的 client_id（老中继），那这一行可能
        # 就是主机本人 —— **不标**成"玩家"（标错比"未知"贵），沿用旧行为。
        if (
            host_claimed and not is_host and not peer_is_owner and not peer_is_host
            and not punch_flow.host_id_of(state)
        ):
            role = ROLE_UNKNOWN
            roles = [ROLE_UNKNOWN]

        # 只有真正的房主/主机能踢人；主机不能踢房主，自己永远不在列表里出现
        # （被踢的是 owner 那条路由面板/管理 API 走）。服务端仍然是最终裁判。
        can_kick = bool((is_owner or is_host) and role != ROLE_OWNER)

        nat_sub = info.get("nat")
        filt = info.get("filtering")

        peer_nat = nat_grade_text(nat_sub, filt)[0] if nat_sub is not None else ""
        peer_inbound = stun_filter_label(
            getattr(filt, "value", filt) or "unknown"
        ) if filt is not None else ""

        # ============================================================
        #  显示名：中继广播的名字优先，老中继才回退
        # ============================================================
        #
        #  * `names=` 里有他 -> 用中继给的名字（登录名 / 游客名）；
        #  * 老中继（没有 names=）且他是房主 -> 用 host_name=（那是老中继唯一
        #    广播的真用户名）；
        #  * 其余 -> 回退到 client_id。**绝不返回空 / 「未知」**。
        name = (
            member_names.get(peer)
            or (host_name if (peer_is_host and host_name) else "")
            or peer
        )

        rows.append({
            "key": peer,
            "is_self": False,
            "name": name,
            "role": role,
            # 组合身份（显示用；Owner 与 Host 可以同时成立）。
            "roles": list(roles),
            # ============================================================
            #  「连接状态」列：以中继广播的 `conn=` 为**唯一真相**（用户第 7 条）
            # ============================================================
            #
            #   * `connected` = 不是 idle（waiting/pairing 都算"在连接中"）；
            #   * `conn_state` 原样交给渲染层，由它翻成
            #     已连接 / 等待配对 / 配对中 / 未连接；
            #   * **老中继没有这个 token**（表里没他）-> 回退旧行为：房间里的人
            #     一律算已连接（`connected=True`、`conn_state=""`）—— 绝不能因为
            #     拿不到新字段就把所有人都显示成"未连接"。
            "connected": (
                True if member_conns.get(peer) is None
                else member_conns.get(peer) != "idle"
            ),
            "conn_state": str(member_conns.get(peer) or ""),
            "status": "online",
            "nat": peer_nat,
            "inbound": peer_inbound,
            # 打洞实际用的地址优先；没有就显示他自己广播的公网端点。
            "addr": info.get("addr") or addrs.get(peer, ""),
            # 中转延迟：中继广播的 per-member rtts=（它自己量的）。没有 -> None
            # -> 渲染层显示 `-`（绝不编）。
            "ping_ms": member_rtts.get(peer),
            # 对端延迟：只有「STUN 直连且这条链路已打通」才适用；适用与否由
            # 渲染层决定显示数字 / `-` / `N/C`。数字来自隧道 keepalive 往返。
            "peer_rtt_ms": info.get("rtt_ms"),
            "peer_direct": bool(info.get("ready")),
            "can_kick": can_kick,
            "detail": peer,
        })

    return rows


def _flow_format(key, args=()):
    """
    `tr(模板)` + 占位符替换。翻译表里的键带 `{0}` `{1}`（见 i18n_tables），
    值也是同形状的模板，所以先查表再替换，中/英/日各自成立。
    """
    text = tr(key or "")

    for index, value in enumerate(args or ()):
        text = text.replace("{%d}" % index, str(value))

    return text


def flow_progress_text(snapshot):
    """
    §G 第 2 层「打洞进度」的**汇总**正文（`2/3 已完成` / `正在建立 3 条连接` …）。

    数据来自 `RoomDirectLink.state()["flow"]`（`punch_flow.summarize()`），
    父级只做汇总 —— 逐目标的事实在第 3 层的行上。**没有 flow 字段**（老快照 /
    老协调器 / 单测里的假 state）时返回空串，调用方走老文案。
    """
    flow = snapshot.get("flow") or {}

    key = str(flow.get("progress_key") or "")

    if not key:
        return ""

    return _flow_format(key, flow.get("progress_args") or ())


def flow_path_text(snapshot):
    """
    §G 第 1 层「通路」的汇总正文（`P2P 已直连：3/3` / `直连尝试中... 0/3`）。

    没有 flow 字段时返回空串（调用方走 `nat_diag_path_text` 的老逻辑）。
    """
    flow = snapshot.get("flow") or {}

    key = str(flow.get("path_key") or "")

    if not key:
        return ""

    if int(flow.get("total") or 0) <= 0:
        return ""

    return _flow_format(key, flow.get("path_args") or ())


def punch_step_text(snapshot):
    """
    The 打洞进度 row: which round, what phase, and how long until the next try.

    Straight from his `status()` strings -- "direct punch #3 (public)",
    "等待同步起跑", "直连暂停中（12s）". The point is that a user staring at a
    stuck punch can read the screen instead of grepping a log.

    Returns (text, colour_hint).

    ============================================================================
      2026-09-28（设计基准 §G）：这一行现在是**父级汇总**，「n/m」
    ============================================================================
    基线的第 2 层是"打洞进度 n/m 汇总"（`正在建立 3 条连接` / `2/3 已完成` /
    `打洞完成：全部 3 个目标已打通`）。逐目标的事实在第 3 层（成员列表下面
    每人一行的 `等待同步起跑（3s）【A】N/C`）。

    所以：**有 `flow` 汇总时以它为准**；没有（老中继 / 老快照 / 单测假 state）
    时完全走原来的文案，行为一个字不变。
    """
    mode = snapshot.get("mode") or "relay"
    via = snapshot.get("direct_via")
    step = (snapshot.get("direct_step") or "").strip()
    attempts = snapshot.get("direct_attempts") or 0

    if mode == "auto":
        from ..core.connection_status import auto_summary
        progress = flow_progress_text(snapshot) or step or "等待对端开始连接"
        return (auto_summary(snapshot.get("connection_rows", [])) + " · " + progress, "ok")

    if mode != "stun":
        return ("未启用", "dim")

    summary = flow_progress_text(snapshot)

    if summary:
        flow = snapshot.get("flow") or {}
        return (summary, "ok" if flow.get("all_up") else "warn")

    if via == "udp":
        peer = snapshot.get("direct_peer") or ""
        return (f"已直连 {peer}".strip(), "ok")

    if snapshot.get("stun_ready"):
        # Ready beats both the step and the count: the link is up and kept warm.
        # Wording per the owner: the punch result IS the connection -- never
        # "waiting for the game".
        return ("已直连（长连接保持中）", "ok")

    if step:
        return (step, "warn" if via == "failed" else "ok")

    if attempts:
        return (f"第 {attempts} 次尝试", "warn")

    # Same string the 直连尝试 row uses for this state. Two rows describing the
    # same fact must not use two different sentences.
    return ("等待开始（点「开始连接」后打洞）", "dim")


def nat_diag_path_text(mode, direct_via, connected, peer="", punch_ready=False,
                       punching=False, carrying=False, flow=None):
    """
    What the 运行状态 card shows for 通路. Returns (text, colour_hint).

    ============================================================================
      §G 第 1 层（2026-09-28 设计基准）：父级只做「n/m」汇总
    ============================================================================

    基线原文：
        `直连尝试中... 0/3` → `P2P 直连中... 2/3` → `P2P 已直连：3/3`

    所以有 `flow` 汇总（`punch_flow.summarize()`）时，这一行就以它为准 ——
    它数的正是"这一轮有几个目标、其中几个通了"。没有 flow（老中继 / 老快照 /
    单测里的假 state）时，完全走下面的老逻辑，一个字不变。

    ============================================================================
      WHY THE PUNCH OUTRANKS "connected"
    ============================================================================

    The rule that was wrong for the longest time:

        "only claim a path when a SESSION is connected"

    That is right about the RELAY (a tunnel is a path, and without a session
    there is no tunnel), but it is exactly backwards about a PUNCHED link. The
    punch happens when you JOIN the room, on purpose, and its whole point is
    that the link exists BEFORE any game does. So a host with a live punched
    link and no game yet was shown "直连尝试中" while the row directly beneath
    it showed "已直连 223.167.44.206:60831" -- the same fact, two answers, on
    the same screen.

    The order now, and it is the order of how real each thing is:

      punched link is UP (kept warm) -> 直连 P2P [peer]   (even before a game)
      session connected + "udp"      -> 直连 P2P [peer]
      session connected + "failed"   -> 直连失败           (STUN never falls back)
      session connected + relay mode -> 中转
      session connected + STUN+other -> 直连尝试中
      nothing at all (STUN)          -> 打洞中
      nothing at all (relay mode)    -> 中转
    """
    # §G 第 1 层：有汇总就用「n/m」。
    if flow and int(flow.get("total") or 0) > 0:
        return (
            _flow_format(flow.get("path_key") or "", flow.get("path_args") or ()),
            "ok" if flow.get("all_up") else "warn",
        )

    # A session is ACTUALLY pushing game bytes over the punched link.
    if carrying:
        return (f"直连 P2P {peer}".strip() if peer else "直连 P2P"), "ok"

    # Punched and kept warm. The owner's rule, verbatim: 通道是长连接一直在的，
    # 不要等也不要依靠任何游戏或者进程的会话. So a punched link IS the direct
    # connection and the row says so -- it must NOT say "waiting for the game".
    #
    # (It used to read "已打通（等待游戏）". That word "等待游戏" was the last
    # survivor of the old model in which the punch was only a preparation step
    # for a game session; the tunnel does not wait for anything.)
    if punch_ready:
        return (f"直连 P2P {peer}".strip() if peer else "直连 P2P"), "ok"

    if connected:
        if direct_via == "failed":
            return "直连失败", "bad"

        if direct_via == "relay" or mode != "stun":
            return "中转", "warn"

        return "直连尝试中", "muted"

    # No session yet. Only claim "打洞中" when a punch is ACTUALLY RUNNING --
    # saying it from the mode alone would be the same class of lie this function
    # has been fixed for twice (claiming a path that is not there).
    if punching:
        return "打洞中（还没打通）", "muted"

    return "未连接", "dim"


#: 诊断结果显示哪些字段： (翻译表键, 取值方式)
#: 取值方式见 nat_diag_value()。顺序 = 屏幕上的顺序。
NAT_DIAG_ROWS = (
    ("NAT 类型", "type"),
    ("公网映射", "mapping"),
    ("STUN 服务器", "servers"),
    ("结论建议", "advice"),
)

#: 明细区最多显示几行（见 nat_diag_detail_lines 末尾的说明）。
NAT_DIAG_DETAIL_MAX_LINES = 12


def nat_diag_value(kind, profile):
    """
    Render one diagnostics field from a NatProfile.

    Returns (text, colour_hint), where `colour_hint` is one of
    "ok" / "warn" / "bad" / "dim" / None -- a MEANING, not a colour, so the
    theme stays the only place that decides what those look like.
    """
    if profile is None:
        return "—", "dim"

    if kind == "type":
        # The `[Symmetric]` half is deliberately NOT translated: it is the
        # name the algorithm, the logs and the docs all use, and a translated
        # identifier is useless when comparing against a log line.
        return f"{tr(profile.nat_type.label)} [{profile.nat_type.value}]", None

    if kind == "mapping":
        if not profile.public:
            return "—", "dim"
        return f"{profile.public[0]}:{profile.public[1]}", None

    if kind == "servers":
        asked = getattr(profile, "asked", 0) or len(profile.probes)
        pool_size = getattr(profile, "pool_size", 0) or asked
        answered = sum(1 for probe in profile.probes if probe.usable)

        if not asked:
            return "—", "dim"

        # SAY HOW MANY WERE ASKED. The probe deliberately stops as soon as it
        # has enough answers (his `want`), so a bare "2/7" reads as "five
        # servers are blocked" when the truth is "we only asked two". Show the
        # asked count, and mention the candidate list only when it is bigger.
        if pool_size > asked:
            text = tr("{0}/{1} 台响应（共问了 {2} 台）").replace(
                "{0}", str(answered)
            ).replace("{1}", str(asked)).replace("{2}", str(asked))
        else:
            text = tr("{0}/{1} 台响应").replace("{0}", str(answered)).replace(
                "{1}", str(asked)
            )

        if answered == 0:
            return text, "bad"

        # All the servers we asked answered -- the network is fine. Anything
        # less rests on fewer samples, so it is worth a warning colour.
        return text, ("ok" if answered == asked else "warn")

    if kind == "advice":
        if profile.nat_type.advice:
            return tr(profile.nat_type.advice), None
        return "—", "dim"

    return "—", "dim"


def nat_diag_detail_lines(profile):
    """
    The plain-text detail block: per-server latency, port sampling, errors.

    Not translated on purpose -- it is a diagnostic dump (the V3-7 "诊断与日志"
    work package) and the numbers are what matter. Everything a normal user
    reads lives on the labelled rows above, and those DO go through tr().
    """
    lines = []

    for probe in profile.probes:
        if probe.usable:
            rtt = f" · {probe.rtt_ms:.0f} ms" if probe.rtt_ms else ""
            lines.append(f"{probe.server}  OK{rtt}")
        else:
            lines.append(f"{probe.server}  {probe.error or '无响应'}")

    if profile.observed_ports:
        ports = ", ".join(str(p) for p in profile.observed_ports[:12])
        lines.append(
            f"mapped ports: {len(profile.observed_ports)} "
            f"(跨度 {profile.port_spread}): {ports}"
        )

    if profile.allocation is not None:
        lines.extend(profile.allocation.summary_lines())

    for error in profile.errors:
        lines.append(f"[!] {error}")

    # The card's detail box is a FIXED height and the page scrolls by wheel --
    # and the wheel deliberately skips Text widgets, so anything that did not
    # fit would be unreachable. Cap it and say so instead of hiding lines.
    if len(lines) > NAT_DIAG_DETAIL_MAX_LINES:
        extra = len(lines) - NAT_DIAG_DETAIL_MAX_LINES
        lines = lines[:NAT_DIAG_DETAIL_MAX_LINES]
        lines.append(f"... 还有 {extra} 行，见客户端日志")

    return lines


def fmt_ping(ping_ms):
    """
    Format the relay latency.

    NOTE: a measured 0.0 ms is a VALID value (client and relay on the same
    machine). The old code used `if ping:` which treats 0.0 as "no data" and
    was exactly why the GUI showed "--" while everything was fine.
    """
    if ping_ms is None:
        return "—"

    try:
        value = float(ping_ms)
    except (TypeError, ValueError):
        return "—"

    if value < 0:
        return "—"

    if value < 10:
        return f"{value:.1f} ms"

    return f"{value:.0f} ms"


def ping_quality(ping_ms):
    """Return (text, colour) describing the relay latency."""
    if ping_ms is None:
        return "未测到", FG_MUTED

    try:
        value = float(ping_ms)
    except (TypeError, ValueError):
        return "未测到", FG_MUTED

    if value < 60:
        return "优秀", OK
    if value < 120:
        return "良好", OK
    if value < 200:
        return "一般", WARN
    return "较差", BAD


def fmt_speed(up, down, active=True):
    """Format the live upload / download speed line."""
    if not active:
        return "—"

    try:
        up = float(up or 0)
        down = float(down or 0)
    except (TypeError, ValueError):
        return "—"

    return f"↑ {human_rate(up)}   ↓ {human_rate(down)}"


def traffic_ledger_text(snapshot):
    """
    「累计流量」这一行（审计问题 6）。

    进程级账本（`engine.traffic_snapshot()`）按**当时真正在用的通路**分成两笔：
    中转 / 对端。离开房间不清零，只有重启客户端才清零，所以这一行在没有会话时
    也照样显示（灰色），而不是一个 `—`。
    """
    snapshot = snapshot or {}

    def amount(value):
        try:
            return human_bytes_short(int(value or 0))
        except (TypeError, ValueError):
            return human_bytes_short(0)

    relay_up = amount(snapshot.get("relay_up"))
    relay_down = amount(snapshot.get("relay_down"))
    direct_up = amount(snapshot.get("direct_up"))
    direct_down = amount(snapshot.get("direct_down"))

    return (
        f"↑{amount(snapshot.get('total_up'))} "
        f"（中转 {relay_up}／对端 {direct_up}）  "
        f"↓{amount(snapshot.get('total_down'))} "
        f"（中转 {relay_down}／对端 {direct_down}）"
    )


def traffic_speed_text(snapshot):
    """
    「实时速度」这一行：永远有数字（没流量就是 0），不随"有没有会话"变成 `—`。

    优先用进程账本的速度（它跨会话、跨通路合计，见 traffic.py）。账本是 0 而
    快照又确实带着会话自己的速率时退回后者 —— 两个来源都是每秒采样一次的真实
    数字，只是"只有一份数据"的快照（单测 / 老快照）也要能显示。
    """
    snapshot = snapshot or {}

    up = snapshot.get("up_rate", 0.0)
    down = snapshot.get("down_rate", 0.0)

    try:
        if not float(up or 0.0) and not float(down or 0.0):
            up = snapshot.get("upload_rate", 0.0)
            down = snapshot.get("download_rate", 0.0)
    except (TypeError, ValueError):
        up = snapshot.get("upload_rate", 0.0)
        down = snapshot.get("download_rate", 0.0)

    return fmt_speed(up, down)




def asset_path(name):
    """
    mclink/assets 里某个文件的完整路径（源码运行 / 打包成 exe 都能用）。

    PyInstaller 单文件模式把数据解到 sys._MEIPASS；源码运行就是包目录本身。
    """
    candidates = []

    base = getattr(sys, "_MEIPASS", None)

    if base:
        candidates.append(os.path.join(base, "mclink", "assets", name))

    candidates.append(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "assets", name)
    )

    for path in candidates:
        if os.path.isfile(path):
            return path

    return ""


def load_logo_image(size=32):
    """
    读 logo 并缩放到指定高度。读不到就返回 None（界面照常起来）。

    图标本身由 scripts/make_assets.py 生成：一个等距草方块，打包进 exe。
    """
    path = asset_path("app.png")

    if not path:
        return None

    try:
        image = tk.PhotoImage(file=path)

        # PhotoImage 只能整数倍缩放，这里按比例缩到目标高度附近。
        factor = max(1, round(image.height() / size))

        if factor > 1:
            image = image.subsample(factor, factor)

        return image
    except Exception:
        return None


def acquire_single_instance():
    """
    只允许**一个** CraftLink 实例（用户 2026-09-30：双开造成很多"灵魂 BUG"，
    直接 ban 掉多开）。返回 `(ok, message)`。

    ============================================================================
      双开为什么必须禁（用户原话）
    ============================================================================
    同一个 client_id / 同一份配置再开一个实例，就会向中继发出**第二条**同身份
    连接：中继把它当"本人重连"顶替掉前一条（座位/主机位/所有权跟着搬），而两个
    客户端界面各说各话 —— 用户看到的就是"灵魂 BUG"（谁在房间、谁是 host 全乱）。
    所以启动最早期就拦掉，**不连中继、不建房间**。

    实现：
      * Windows —— 命名互斥体（`CreateMutexW` + `GetLastError()==183`）。
        进程退出（含崩溃）由内核自动释放，最稳；
      * 其它平台 —— 配置目录里的锁文件 + `fcntl.flock(LOCK_EX|LOCK_NB)`，
        同样进程退出即释放。

    拿不到锁句柄/文件、或者机制本身出错时**放行**：锁是为了防双开，
    绝不能因为锁机制自己出问题就把用户挡在门外。
    """
    global _SINGLE_INSTANCE_HANDLE

    message = (
        "CraftLink 已经在运行了。\n\n"
        "同一个房间身份不允许开两个客户端（会造成身份错乱），"
        "请先关掉已经打开的那个窗口再启动。"
    )

    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, False, "Local\\CraftLink-Single")

            if not handle:
                return True, ""

            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                return False, message

            _SINGLE_INSTANCE_HANDLE = handle
            return True, ""

        return True, ""

    with contextlib.suppress(Exception):
        import fcntl

        from ..config import settings as settings_mod

        path = os.path.join(settings_mod.config_dir(), "craftlink.lock")
        handle = open(path, "a+", encoding="utf-8")

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False, message

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()

        _SINGLE_INSTANCE_HANDLE = handle
        return True, ""

    return True, ""


def stun_filter_label(grade):
    """eif/adf/apdf -> 他的中文挡位（NAT_FILTER_LABEL，一字不改）。"""
    from ..core import stun as stun_mod

    return stun_mod.NAT_FILTER_LABEL.get(
        grade, stun_mod.NAT_FILTER_LABEL["unknown"]
    )


def nat_grade_text(nat_subtype, filtering):
    """
    ⑰ NAT 细分：把"锥型 (NAT1-3)"细化成 NAT1/NAT2/NAT3/NAT4。

    ============================================================================
      四个挡位怎么来的（经典定义，两个轴组合）
    ============================================================================

      NAT1 全锥        映射与目的地无关（锥型） + 过滤与来源无关（eif）
      NAT2 地址受限    映射与目的地无关（锥型） + 只放行"我们发过包的地址"（adf）
      NAT3 端口受限    映射与目的地无关（锥型） + 只放行"我们发过包的那个端口"（apdf）
      NAT4 对称        映射按目的地分配（sym_*），细分为递增/递减/随机

    两个轴我们都在测：
      * **映射**来自他的分类器（`punch.NatSub`：cone / sym_easy_inc /
        sym_easy_dec / sym_hard）；
      * **过滤**来自"入站探测 + 换端口应答"合成出来的那一档（见
        `inbound_status_text`）。

    过滤那一档还没测出来（unknown）时**不猜**：给出"锥型（过滤未测）"，
    并说明缺什么 —— 这比硬塞一个 NAT1-3 里的某一个要诚实。

    Returns (text, detail) —— detail 是给"怎么才能测出来"用的提示。
    """
    from ..core import punch as punch_mod

    sub = getattr(nat_subtype, "value", nat_subtype) or "unknown"
    filt = getattr(filtering, "value", filtering) or "unknown"

    sym_detail = {
        "sym_easy_inc": "递增·可预测",
        "sym_easy_dec": "递减·可预测",
        "sym_hard": "随机·难",
    }

    if sub in sym_detail:
        # 对称型的映射本身就不固定，过滤那一档再多也救不了打洞 ——
        # 这里直接给 NAT4 的细分。
        # ★ 用户 2026-09-30 第 4 条：文案统一补全"对称型"，细分放括号里。
        return f"NAT4 对称型（{sym_detail[sub]}）", ""

    if sub not in ("cone", "NAT_SUB_CONE"):
        return "未知（映射未测出）", "先让至少两台 STUN 服务器应答"

    # 映射是锥型，接下来靠过滤那一档分 NAT1/2/3
    # ★ 用户 2026-09-30 第 4 条：所有锥形结果补全"锥形"两个字。
    # ★ 用户 2026-10-02（最终口径）：**NAT1 与 NAT2 不再细分**。
    #
    # 理由（用户的判断，也是测量的现实）：全锥与地址受限的差别只在"陌生 IP 能不能
    # 打进来"，而单 IP 的 STUN 给不出这个证据 —— 对 P2P 打洞来说两者都是"轻松"
    # （都能打，只是先打谁的问题），所以合并成**一档**、用天蓝色：
    #
    #     NAT1/2（完全锥形或地址受限锥形·P2P 轻松）
    #
    # NAT3（端口受限）与 NAT4（对称）**保持原样**，颜色也不变。
    if filt in ("eif", "adf", "adf_or_eif"):
        return "NAT1/2（完全锥形或地址受限锥形·P2P 轻松）", ""


    if filt == "apdf":
        return "NAT3 端口受限锥形", ""

    return "锥形（过滤未测，NAT1/2/3 待定）", "需要入站探测到达，或服务端支持换端口应答"

_SINGLE_INSTANCE_HANDLE = None
