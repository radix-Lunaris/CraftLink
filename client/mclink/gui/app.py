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
from ..config import branding as branding_mod
from ..config import userdirs as userdirs_mod
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

# ============================================================================
#  ★ M6：首次运行引导的三段正文
# ============================================================================
#
# 定义成常量、并且**每一段单独 tr()**：整段拼完再查表的话，句式表里就得放
# 一条跨三句、带换行的长正则 —— 实际上查不到，于是英文界面上会突然冒出一段
# 中文。分开之后每段都是一个能被翻译的静态键。
FIRST_RUN_LEGACY_CLEARED = (
    "检测到旧版本配置里保存的服务器地址（旧版内置的默认值）。\n"
    "按新版本的口径，客户端不再内置任何服务器，这个地址已经清空 —— "
    "请重新确认一次。"
)

FIRST_RUN_BODY = (
    "客户端没有内置服务器，需要填写你的中继服务器：\n\n"
    "  1. 服务器地址：部署者给你的域名或 IP\n"
    "  2. 端口：部署时定的数字（1-65535）\n\n"
    "填好后点「测试服务器连接」。测试成功即被记住，下次自动带出；"
    "随时可以修改，改完立即生效。"
)

FIRST_RUN_DATA_DIR_LEAD = "你的配置、日志和（如果你勾选了）记住的账号都存在："
FIRST_RUN_DATA_DIR_TAIL = "这个目录可以在「数据目录」卡片里随时更改。"

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

    # 直连在 STUN 和 AUTO 下都会打洞，所以"未启用"只对 relay 说。AUTO 下这一行
    # 报的是同一条打洞进展 —— 区别在于"洞没通"时 AUTO 会用中继顶着，界面照实
    # 显示"打洞中"，而不是假装已经直连。
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
            "连接模式由你决定（房主）：切换会重建所有隧道，"
            "客户端会自动重连（无需手动操作）"
        )

    current = {
        MODE_RELAY: "Relay 中转",
        MODE_STUN: "STUN 直连",
        MODE_AUTO: "AUTO 自动",
    }.get(room_mode, "Relay 中转")

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
         `P2P 已直连【玩家名】192.0.2.13:40879 18ms`。

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
    it showed "已直连 192.0.2.14:60831" -- the same fact, two answers, on
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


class Tooltip:
    """Minimal hover tooltip (no third party dependency)."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None

        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self.tip is not None:
            return

        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6

            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")

            tk.Label(
                self.tip,
                text=self.text,
                bg="#2c3444",
                fg=FG,
                font=FONT_SMALL,
                relief="solid",
                bd=1,
                padx=8,
                pady=5,
                justify="left",
            ).pack()
        except Exception:
            self.tip = None

    def _hide(self, _event=None):
        if self.tip is not None:
            with contextlib.suppress(Exception):
                self.tip.destroy()
            self.tip = None


class TextHandler:
    """Python logging handler that forwards records into the GUI log queue."""

    def __init__(self, emit):
        self.emit = emit

    def write(self, text):
        text = text.rstrip()
        if text:
            self.emit(text)

    def flush(self):
        pass


# ============================================================
# Mouse-wheel-only scrolling (no visible scrollbar)
# ============================================================

#: Widgets that scroll themselves. The global wheel handler must not steal
#: their wheel events, otherwise scrolling over the log feels broken.
OWN_SCROLL_WIDGETS = (tk.Text, tk.Listbox, ttk.Treeview)


def _widget_under_pointer(root):
    try:
        widget = root.winfo_containing(
            root.winfo_pointerx(), root.winfo_pointery()
        )
    except Exception:
        return None

    return widget


def _block_wheel_select(combobox, on_scroll):
    """
    Stop the wheel from ever changing a `readonly` ttk.Combobox's value.

    ============================================================================
      用户 2026-09-29：「鼠标浮动在语言和背景色选框上滚动滚轮可直接操作切换
      语言或背景，应当是只允许下拉菜单点击才行」
    ============================================================================

    Tk 的 `TCombobox` **类绑定**把 `<MouseWheel>` 当成"上一个/下一个选项"，
    readonly 的 Combobox 因此会被滚轮直接改值 —— 用户只是想把页面滚下去，
    语言/主题却被换掉了（而且换语言会重建整个界面）。

    控件级绑定排在类绑定**前面**（bindtags: widget -> class -> toplevel -> all），
    所以这里绑定并 `return "break"` 就能把类绑定整个挡掉。挡掉之后**自己把这一
    滚转交给页面滚动**（`install_wheel` 的全局滚动在这一步轮不到），于是
    "任何地方都能滚"的手感不变，唯独"滚轮不许改选项"。

    下拉列表（popdown 的 listbox）是另一个 toplevel，不在这里的绑定范围里，
    它照旧可以正常滚动 —— 那是用户主动打开菜单之后的操作。
    """
    def _wheel(event):
        num = getattr(event, "num", 0)

        if num in (4, 5):
            on_scroll(-1 if num == 4 else 1, 1)
            return "break"

        delta = getattr(event, "delta", 0) or 0

        if delta:
            steps = max(1, int(round(abs(delta) / 120.0)))
            on_scroll(-1 if delta > 0 else 1, steps)

        return "break"

    if combobox is None:
        return

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        with contextlib.suppress(Exception):
            combobox.bind(sequence, _wheel, add="+")


def _can_scroll_itself(widget, delta):
    """True when `widget` still has room to scroll in the wheel's direction."""
    for parent in [widget, *widget.master.winfo_children()]:
        if not isinstance(parent, OWN_SCROLL_WIDGETS):
            continue

        try:
            first, last = parent.yview()
        except Exception:
            continue

        if delta > 0 and first > 0.0:
            return True

        if delta < 0 and last < 1.0:
            return True

    return False


class ScrollFrame(tk.Frame):
    """
    A vertically scrollable container driven by the mouse wheel only.

    There is deliberately NO ttk.Scrollbar: the wheel is the only control, so
    the UI has no ugly grey strip on the right. (If you ever want one back,
    create a ttk.Scrollbar and set it as the canvas `yscrollcommand`.)

    ============================================================================
      纵向滚轮可以，横向滚动【永远不行】（用户明确要求）
    ============================================================================

    `_sync_scrollregion()` 把滚动区钉成 `(0, 0, 画布宽度, 内容高度)`：

      * 内容层宽度被 itemconfigure 钉死在画布宽度上，所以卡片永远和窗口一样宽，
        不会因为里面某个控件"需要的宽度"更大就把页面撑出横向滚动条；
      * 滚动区宽度 = 画布宽度，于是 xview 恒为 (0, 1)，横向没有任何可滚的余量
        （就算别处手滑调了 xview_scroll 也滚不动）。

    装不下的时候由布局自己解决（两列 -> 单列，见 MCLinkApp._reflow_columns），
    不是靠横向滚动。

    Usage:
        body = ScrollFrame(root, bg=BG)
        body.pack(fill="both", expand=True)
        ... put widgets into body.inner ...
        body.finalize()          # call AFTER the layout is built
    """

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)

        self.canvas = tk.Canvas(
            self,
            bg=kwargs.get("bg", BG),
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=kwargs.get("bg", BG))

        self._window = self.canvas.create_window(
            (0, 0), window=self.inner, anchor="nw"
        )

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    # --------------------------------------------------------

    def _sync_scrollregion(self):
        """
        滚动区 = 画布宽度 × 内容高度，并且把横向位置归零。

        见类注释：这是"永远不横向滚动"的实现处。内容比画布高的时候纵向照样
        能滚（滚轮 / scroll_by）。
        """
        with contextlib.suppress(Exception):
            width = max(1, self.canvas.winfo_width())
            box = self.canvas.bbox("all")
            height = max(1, box[3] if box else self.canvas.winfo_height())

            self.canvas.configure(scrollregion=(0, 0, width, height))
            self.canvas.xview_moveto(0.0)

    def _on_inner_configure(self, _event=None):
        self._sync_scrollregion()

    def _on_canvas_configure(self, event):
        # Keep the inner frame as wide as the canvas.
        self.canvas.itemconfigure(self._window, width=event.width)
        self._sync_scrollregion()

    def finalize(self):
        """Recompute the scroll region once every child exists."""
        try:
            self.update_idletasks()
        except Exception:
            pass

        self._on_inner_configure()

    # --------------------------------------------------------

    def refresh_content(self):
        """
        内容高度变了以后重新算滚动区。

        WHY THIS IS NEEDED (a real bug): the wheel stops working after
        something inside the page changes height without any <Configure> on
        the content frame. `_sync_scrollregion` otherwise only runs on a
        configure event, so the scrollregion keeps the OLD height: the page
        can then be scrolled past the real content (into blank space) and the
        wheel appears dead near the bottom. Reported symptom: run the network
        diagnosis, the page drops to the bottom and the wheel does nothing
        until you switch mode back and forth (which rebuilds everything).

        Call this from any code that grows or shrinks a widget in the page.
        """
        try:
            self.update_idletasks()
        except Exception:
            pass

        self._sync_scrollregion()

    def scroll_by(self, direction, steps=1):
        # 切语言/主题会重建界面，而 install_wheel 的滚轮是 bind_all 到整棵控件树上的
        # —— 旧滚动区已经销毁，它的处理函数还挂在 "all" 标签上。这里挡一下，
        # 否则每滚一下滚轮都会在控制台刷一串 invalid command name（观感问题，
        # 而且旧处理函数本来也没有任何东西可滚）。
        with contextlib.suppress(Exception):
            if not self.canvas.winfo_exists():
                return

        with contextlib.suppress(Exception):
            self.canvas.yview_scroll(int(direction * steps), "units")

    def install_wheel(self, root):
        """
        Bind the wheel GLOBALLY so the wheel works anywhere in the window.

        The "all" binding runs last, and the handler yields to any widget
        under the pointer that can still scroll itself (the log text box, a
        list, a treeview). That is what keeps the log scrollable while the
        page also scrolls.

        ============================================================================
          为什么装新绑定之前要先把旧的摘掉
        ============================================================================

        切语言 / 换主题会重建整个界面（新的 ScrollFrame、新的画布），而 `bind_all`
        是挂在 "all" 标签上的 —— 旧滚动区的处理函数**不会**随控件一起消失。

        于是滚轮事件会按注册顺序依次叫醒它们：旧的那个（画布早没了）先跑，而且
        它照样 `return "break"` —— Tcl 收到 break 就**不再执行后面的绑定**，新建的
        那个处理函数永远轮不到。表现就是"切一次语言之后，滚轮再也滚不动页面"。

        所以每次安装前先清掉 "all" 上的旧绑定（本项目里 "all" 上只有这三个滚轮
        绑定；控件自己类级别的绑定，比如 Treeview / Text 的滚轮，不受影响）。
        """
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            with contextlib.suppress(Exception):
                root.unbind_all(sequence)

        def _wheel(event):
            widget = _widget_under_pointer(root)

            if widget is not None:
                delta_sign = 1 if getattr(event, "delta", 0) > 0 else -1

                if _can_scroll_itself(widget, delta_sign):
                    return None

            delta = getattr(event, "delta", 0) or 0

            if delta == 0:
                return "break"

            direction = -1 if delta > 0 else 1
            steps = max(1, int(round(abs(delta) / 120.0)))

            self.scroll_by(direction, steps)

            return "break"

        def _wheel_linux(event):
            widget = _widget_under_pointer(root)

            if widget is not None:
                delta_sign = 1 if getattr(event, "num", 0) == 4 else -1

                if _can_scroll_itself(widget, delta_sign):
                    return None

            self.scroll_by(-1 if event.num == 4 else 1, 1)

            return "break"

        root.bind_all("<MouseWheel>", _wheel, add="+")
        root.bind_all("<Button-4>", _wheel_linux, add="+")
        root.bind_all("<Button-5>", _wheel_linux, add="+")


# ============================================================
# Card helper
# ============================================================

class Card(tk.Frame):
    """
    一张卡片：圆角面板 + 标题。

    ============================================================================
      圆角是怎么做出来的（tkinter 没有原生的圆角）
    ============================================================================

    tkinter 的 Frame 永远是直角矩形。所以这里在卡片底下放一块 Canvas，
    在上面画一个圆角矩形（填充 = 卡片色、描边 = 边框色），再把内容 Frame
    盖在上面。

    ============================================================================
      内容层的内缩必须 >= 圆角半径，否则圆角会被盖住（踩过一次）
    ============================================================================

    The content layer is an opaque CARD-coloured RECTANGLE. If it is inset by
    only 1px (which is what "let the border show" suggests), its square corners
    cover the whole corner area and the rounding is invisible -- the window
    looks exactly like it did before, which is precisely what the user reported
    ("完全没变化").

    So the inset equals the radius: the cut-away corner area stays outside the
    content layer, and the page background shows through the arc. That is the
    whole trick.

    The same applies to anything dropped into a card (a Treeview, a Text box,
    a nested panel): it must not reach into the corner region. Values that are
    tuned together are CARD_RADIUS / _inner inset / body padding.

    卡片尺寸变化时会重画（<Configure>），所以窗口拉大拉小都不会花。
    """

    def __init__(self, master, title=None, radius=None, **kwargs):
        super().__init__(master, bg=BG, highlightthickness=0, bd=0, **kwargs)

        self._radius = CARD_RADIUS if radius is None else radius
        self._last_size = (0, 0)

        self._canvas = tk.Canvas(
            self, bg=BG, highlightthickness=0, bd=0, takefocus=0
        )
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)

        # 内容层：比画布晚创建 = 盖在画布上面。
        # 四周留出 = 半径 的空白，圆角才有地方露出来。
        self._inner = tk.Frame(self, bg=CARD)
        self._inner.pack(
            fill="both", expand=True, padx=self._radius, pady=self._radius
        )

        self.body = tk.Frame(self._inner, bg=CARD)
        self.body.pack(fill="both", expand=True, padx=6, pady=6)

        if title:
            # tr() 在标题这一层做：卡片标题全项目只有这一处绘制，
            # 翻译它不用碰任何一张卡片的构建代码。
            tk.Label(
                self.body,
                text=tr(title),
                bg=CARD,
                fg=FG_DIM,
                font=FONT_BOLD,
                anchor="w",
            ).pack(fill="x", pady=(0, 8))

            # 标题下面一条细分隔线：卡片因此有了"区块感"，
            # 而不是一堆文字浮在同一个平面上。纯观感。
            tk.Frame(self.body, bg=BORDER, height=1).pack(fill="x", pady=(0, 8))

        self.bind("<Configure>", self._redraw)
        self.after_idle(self._redraw)

    def _redraw(self, _event=None):
        """
        重画卡片背景：一层"阴影" + 一层圆角卡片本体。

        阴影是纯观感：同一块画布上先画一个往右下偏 3px 的深色圆角矩形，
        再把卡片盖上去。tkinter 没有真阴影，但这一手在两种主题下都够用了 ——
        卡片像是浮在页面上，而不是和背景糊在一起（用户说"完全没变化"，
        一半原因就是圆角太细看不出来）。

        ============================================================================
          为什么要记 _last_size（用户问"做阴影会不会卡"）
        ============================================================================

        <Configure> 在拖动窗口大小的时候一秒能触发几十次。画两个多边形本身很
        便宜，但尺寸没变就没必要重画 —— 子控件 packing 引起的事件全都会被这一行
        挡掉。

        真正会持续跑的东西只有彩色主题的极光光带（见 _maybe_start_aurora），
        它是可选的、窗口不可见时自己停。
        """
        with contextlib.suppress(Exception):
            width = self.winfo_width()
            height = self.winfo_height()

            if width < 8 or height < 8:
                return

            if (width, height) == self._last_size:
                return

            self._last_size = (width, height)

            self._canvas.delete("all")

            # 阴影层
            rounded_rect(
                self._canvas,
                SHADOW_OFFSET,
                SHADOW_OFFSET + 1,
                width,
                height,
                self._radius,
                fill=SHADOW,
                outline=SHADOW,
            )

            # 卡片本体（比画布小一圈，阴影从右下露出来）
            rounded_rect(
                self._canvas,
                0,
                0,
                width - SHADOW_OFFSET,
                height - SHADOW_OFFSET - 1,
                self._radius,
                fill=CARD,
                outline=BORDER,
            )


class RoundButton(tk.Canvas):
    """
    圆角按钮。tk.Button 画不出圆角，所以这个按钮是自己画的。

    ============================================================================
      为什么要这么小心：它必须能和 tk.Button 互换
    ============================================================================

    The rest of the app (and the tests) treat buttons as tk widgets:

        button["state"]            -> "normal" / "disabled"
        button.cget("text")
        button.configure(text=..., state=...)
        button.configure(bg=...)   _set_enabled() recolours them
        button.invoke()            not used, but cheap to provide

    So this class implements exactly those, and the drawn colours follow
    `state` / `bg` / `text`. A caller that cannot tell the difference cannot
    break because of the swap -- which is the whole point: the feature is
    "buttons look rounded", not "buttons behave differently".

    Hover/press are visual only; the click still calls the same `command`.
    """

    #: 圆角半径（像素）
    RADIUS = 12

    def __init__(self, parent, text="", command=None, width=12,
                 primary=False, bg=None, fg=None, font=None,
                 padx=20, pady=12, secondary=False, **_ignored):
        self._primary = bool(primary)
        self._command = command
        self._text = text
        self._state = "normal"
        self._font = font if font is not None else FONT
        self._fg_normal = fg if fg is not None else FG
        self._bg_normal = bg if bg is not None else CARD_ALT
        self._padx = padx
        self._pady = pady
        self._hover = False

        #: `width=字符数`（tk.Button 的语义）。真正变成像素要等控件建好、
        #: 能问 Tk 的字体引擎之后（见 _measure）。
        self._width_chars = width
        self._min_width = 0
        #: 量文字用的字体对象，第一次用到时才建（控件建好之前没有 Tcl 解释器）。
        self._font_obj = None

        # 画布底色必须和父容器一模一样，否则圆角外面会露出一圈方方正正的
        # 底色（用户报的"选中项里有一圈很违和的白色"就是这个）。
        surface = surface_color(parent)

        super().__init__(
            parent,
            bg=surface,
            highlightthickness=0,
            bd=0,
            takefocus=1,
        )

        # 尺寸必须等控件建好之后再定：量文字要问 Tk 要真实字体度量（self.tk）。
        self._resize()

        self._shapes = []

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())
        self.bind("<Configure>", lambda _e: self._draw())

        self._draw()

    # ---- 兼容 tk.Button 的那几个口 ----

    def cget(self, key):
        key = str(key).lower()

        if key == "text":
            return self._text

        if key == "state":
            return self._state

        if key in ("bg", "background"):
            return self._bg_normal

        if key in ("fg", "foreground"):
            return self._fg_normal

        return super().cget(key)

    def __getitem__(self, key):
        """
        `button["state"]` 也必须走我们这一套。

        ----------------------------------------------------------------------
        Canvas 自己有一个同名的 Tcl 选项 `-state`（normal/disabled/hidden），
        而 `widget["state"]` 在 tkinter 里是直接问 Tcl 的，**不会**经过我们
        重写的 cget()。不覆盖它的话，程序里看到的永远是 Canvas 默认的
        "normal" —— 测试立刻就会红（"logout is disabled while logged out"）。
        ----------------------------------------------------------------------
        """
        return self.cget(key)

    def configure(self, **kwargs):  # noqa: D401 - tk 的接口名
        redraw = False

        for key, value in kwargs.items():
            key = key.lower()

            if key == "text":
                self._text = str(value)
                redraw = True
            elif key == "state":
                self._state = str(value)
                redraw = True
            elif key in ("bg", "background"):
                self._bg_normal = value
                redraw = True
            elif key in ("fg", "foreground"):
                self._fg_normal = value
                redraw = True
            elif key == "command":
                self._command = value
            elif key == "font":
                self._font = value
                # 量文字用的字体对象要跟着换掉，否则新字体还是按旧字体量。
                self._font_obj = None
                redraw = True

        if redraw:
            # _resize() 会重新 _measure()：所以 `configure(text=...)`（切语言
            # 时按钮文字整体换掉）之后按钮一定按【新文字 + 真实字体】重算尺寸。
            self._resize()
            self._draw()

    config = configure

    def invoke(self):
        """点击效果，和 tk.Button.invoke() 一样（禁用时不响应）。"""
        if self._state == "disabled":
            return

        if callable(self._command):
            with contextlib.suppress(Exception):
                self._command()

    # ---- 画 ----

    def _font_size(self):
        with contextlib.suppress(Exception):
            return int(self._font[1])

        return 10

    def _tk_font(self):
        """
        当前字体在 Tk 里的真实度量对象（量文字 / 量行高用）。

        中英日的字宽差别很大（中文一个字≈1em，日文半角假名更窄，英文平均
        0.5em 左右），所以按钮尺寸**不能靠估算**：直接问 Tk 用这个字体量出来
        多宽。取不到就返回 None，调用方退回旧的估算方式（宁可丑，不能崩）。
        """
        if self._font_obj is None:
            with contextlib.suppress(Exception):
                self._font_obj = tkfont.Font(root=self, font=self._font)

        return self._font_obj

    def _char_width(self):
        """
        `width=字符数` 里"一个字符"是多少像素。

        Tk 自己的定义就是**字体里数字 '0' 的宽度**（见 tk.Button 的 -width
        文档），所以这里直接问字体，而不是 `字号 × 0.62` 那种猜法。
        """
        font = self._tk_font()

        if font is not None:
            with contextlib.suppress(Exception):
                return max(1.0, float(font.measure("0")))

        return max(1.0, self._font_size() * 0.62)

    def _text_width(self, text):
        """这段文字用当前字体画出来有多宽（像素）。"""
        font = self._tk_font()

        if font is not None:
            with contextlib.suppress(Exception):
                return float(font.measure(text))

        return sum(
            1.0 if ord(c) > 0x2E80 else 0.62 for c in text
        ) * self._font_size()

    def _linespace(self):
        """一行字占的高度（像素）。"""
        font = self._tk_font()

        if font is not None:
            with contextlib.suppress(Exception):
                return float(font.metrics("linespace"))

        return self._font_size() * 1.35

    def _measure(self):
        """
        量一个刚好装得下当前文字的尺寸（语言无关）。

        ============================================================================
          为什么不再用 `0.62 × 字号 × 字符数`
        ============================================================================

        用户报「日语模式下按钮大小要做适配」：旧实现把每个非 CJK 字符算成
        0.62em、CJK 算成 1em，这在日文（汉字 + 假名混排）和英文下都对不上，
        于是同一排按钮有的被裁掉文字、有的虚胖。现在宽度取
        `tkinter.font.Font(字体).measure(文字)`，行高取同字体的
        `metrics("linespace")`：任何语言都是"文字多宽，按钮就多宽"。

        `width=字符数` 仍然生效（它是调用方给的**最小宽度**，用来让一排按钮
        看起来一样宽），单位按 Tk 的定义 = 字体里 '0' 的宽度。
        """
        pad = self._padx * 2

        self._min_width = (
            int(self._width_chars * self._char_width() + pad)
            if self._width_chars else 0
        )

        width = max(self._min_width, int(self._text_width(self._text) + pad))
        # 按钮做大一点（用户："这些按钮你稍微做大一点啊"）
        height = int(self._linespace() + self._pady * 1.3)

        return max(30, width), max(38, height)

    def _resize(self):
        with contextlib.suppress(Exception):
            width, height = self._measure()
            super().configure(width=width, height=height)

    def _colors(self):
        primary = self._primary

        if self._state == "disabled":
            return CARD, LOCKED

        if primary:
            bg = ACCENT_DARK if self._hover else ACCENT
            return bg, "#ffffff"

        if self._hover:
            return BORDER, FG

        return self._bg_normal, self._fg_normal

    def _draw(self):
        with contextlib.suppress(Exception):
            width = self.winfo_width()
            height = self.winfo_height()

            if width < 4 or height < 4:
                return

            fill, fg = self._colors()

            self.delete("all")

            rounded_rect(
                self, 0, 0, width, height, self.RADIUS, fill=fill, outline=fill
            )

            self.create_text(
                width / 2,
                height / 2,
                text=self._text,
                fill=fg,
                font=self._font,
            )

    def _on_enter(self, _event=None):
        self._hover = True
        self._draw()
        with contextlib.suppress(Exception):
            self.configure(cursor="hand2")

    def _on_leave(self, _event=None):
        self._hover = False
        self._draw()

    def _on_press(self, _event=None):
        if self._state == "disabled":
            return

        self._hover = True
        self._draw()

    def _pulse(self, step=0):
        """
        点击时的一个小动画：短暂"压一下"再回弹（只改观感，不动任何行为）。

        用 Canvas 缩放模拟按下：先把内容整体缩 96%，再分两帧回到 100%。
        动画期间按钮依然可用（不阻塞事件循环）。
        """
        if self._state == "disabled":
            return

        factors = (0.96, 0.98, 1.0)

        if step >= len(factors):
            return

        with contextlib.suppress(Exception):
            width, height = self._measure()
            factor = factors[step]

            super().configure(
                width=int(width * factor), height=int(height * factor)
            )
            self._draw()

        with contextlib.suppress(Exception):
            self.after(60, lambda: self._pulse(step + 1))

    def _on_release(self, _event=None):
        inside = True

        with contextlib.suppress(Exception):
            x, y = self.winfo_pointerxy()
            inside = (
                self.winfo_rootx() <= x <= self.winfo_rootx() + self.winfo_width()
                and self.winfo_rooty() <= y
                <= self.winfo_rooty() + self.winfo_height()
            )

        self._hover = inside
        self._draw()

        if inside:
            # 重点按钮（主色）点一下有个小回弹；次要按钮保持安静。
            if self._primary:
                self._pulse()

            self.invoke()


class EyeButton(RoundButton):
    """
    「小眼睛」：密码框右边那个显示 / 隐藏明文的按钮。

    ============================================================================
      为什么是 RoundButton 的子类，而且用"画"的而不是 👁 / 🙈
    ============================================================================

    It IS the same button as everywhere else -- the rounded shape, the hover
    fill, the press feedback, the `invoke()` API and the keyboard handling
    (Tab to it, space / Enter to trigger) are all inherited from RoundButton.
    Only `_draw()` is replaced, so there is exactly one button implementation
    in the app and this cannot drift away from it.

    The glyph is drawn with canvas primitives instead of the 👁 / 🙈 emoji:
    an emoji only appears when the font has the glyph (or Tk falls back to one
    that does), and when it does not, the user gets a tofu box in the password
    field -- the worst possible place for a cosmetic gamble. Two arcs, a dot
    and a slash always render.

    状态显示：明文可见时眼睛是强调色，隐藏时眼睛上有一道斜杠（"现在看不见"）。
    """

    def __init__(self, parent, revealed=False, **kwargs):
        self._revealed = bool(revealed)

        # width=0 是刻意的：RoundButton 的 width 是【字符数】，默认 12 会给
        # 这个小按钮算出 100 多像素。0 = 只要图标本身那么大。
        kwargs.setdefault("width", 0)
        kwargs.setdefault("padx", 9)
        # 高度对齐旁边的输入框（padding 10x8 的 Modern.TEntry 差不多 37 高）。
        kwargs.setdefault("pady", 14)
        kwargs.setdefault("bg", CARD_ALT)
        kwargs.setdefault("fg", FG_DIM)

        super().__init__(parent, text="", **kwargs)

    # ---- 状态 ----

    def revealed(self):
        return self._revealed

    def set_revealed(self, revealed):
        """设置"明文是否可见"。只重画，不碰任何变量（变量在调用方手里）。"""
        self._revealed = bool(revealed)
        self._draw()

    def toggle_revealed(self):
        self.set_revealed(not self._revealed)
        return self._revealed

    # ---- 画 ----

    def _colors(self):
        fill, fg = super()._colors()

        # 明文可见 = 这只眼睛"睁着"，用强调色点一下，一眼能看出当前状态。
        if self._state != "disabled" and self._revealed:
            fg = ACCENT

        return fill, fg

    def _draw(self):
        with contextlib.suppress(Exception):
            width = self.winfo_width()
            height = self.winfo_height()

            if width < 6 or height < 6:
                return

            fill, fg = self._colors()

            self.delete("all")

            rounded_rect(
                self, 0, 0, width, height, self.RADIUS, fill=fill, outline=fill
            )

            self._draw_eye(width, height, fg)

    def _draw_eye(self, width, height, colour):
        cx = width / 2.0
        cy = height / 2.0

        eye_w = float(min(max(width - 12, 12), 22))
        eye_h = max(8.0, eye_w * 0.6)

        x1, y1 = cx - eye_w / 2, cy - eye_h / 2
        x2, y2 = cx + eye_w / 2, cy + eye_h / 2

        # 眼眶：上下两条半圆弧拼成一个杏仁形（横着的椭圆）。
        self.create_arc(x1, y1, x2, y2, start=0, extent=180, style="arc",
                        outline=colour, width=2)
        self.create_arc(x1, y1, x2, y2, start=180, extent=180, style="arc",
                        outline=colour, width=2)

        # 瞳孔
        radius = max(2.0, eye_h * 0.22)
        self.create_oval(
            cx - radius, cy - radius, cx + radius, cy + radius,
            fill=colour, outline=colour,
        )

        # 斜杠 = "现在看不见"（点一下就把明文显示出来）
        if not self._revealed:
            self.create_line(
                x1 - 1, y2 + 1, x2 + 1, y1 - 1,
                fill=colour, width=2, capstyle="round",
            )


class RoundTile(tk.Frame):
    """
    圆角瓷砖（连接模式 / 身份那几块）。和 Card 一样：Canvas 画圆角垫底。

    对外只需要 `set_selected()`：选中态换填充色和描边色。子控件的 bg 也要
    跟着换（它们是不透明的矩形，不换就会在圆角上露出直角），所以内部保存了
    一份子控件列表，切换时一起刷。
    """

    def __init__(self, master, locked=False):
        super().__init__(master, bg=CARD, highlightthickness=0, bd=0)
        self._locked = bool(locked)
        self._fill = TILE_OFF
        self._border = BORDER
        self._last_size = (0, 0)

        self._surface = surface_color(master)

        self._canvas = tk.Canvas(
            self, bg=self._surface, highlightthickness=0, bd=0, takefocus=0
        )
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)

        # 内容层内缩 = 半径（否则方角盖住圆角，和卡片一个道理）
        self.body = tk.Frame(self, bg=TILE_OFF)
        self.body.pack(
            fill="both", expand=True,
            padx=TILE_RADIUS, pady=TILE_RADIUS,
        )

        self.bind("<Configure>", self._redraw)
        self.after_idle(self._redraw)

    def set_selected(self, selected, bg=None, border=None):
        """
        切换选中态：只换填充色和描边色（外加把内容层刷成同一个底色）。

        ============================================================================
          body 自己也要刷（用户："选框里为啥会有这么一圈白色的没有填色的玩意"）
        ============================================================================

        `self.body` 不是一个"透明的内边距"—— 它是一个**不透明的 Frame**，
        内缩 TILE_RADIUS 摆在圆角上面。只刷它的子控件、不刷它自己，选中时
        画布上的填充色（TILE_ON）和 body 的旧底色（TILE_OFF）之间就会露出一圈
        **没被填色的浅色环**：暗色下不明显，亮色下就是一块白边。

        所以这里三层一起刷：画布（_redraw）、body 本身、body 的子控件。
        """
        self._fill = bg or (TILE_ON if selected else TILE_OFF)
        self._border = border or (ACCENT if selected else BORDER)

        with contextlib.suppress(Exception):
            self.body.configure(bg=self._fill)

        for child in self.body.winfo_children():
            with contextlib.suppress(Exception):
                child.configure(bg=self._fill)

        self._redraw(force=True)

    def _redraw(self, _event=None, force=False):
        with contextlib.suppress(Exception):
            width = self.winfo_width()
            height = self.winfo_height()

            if width < 6 or height < 6:
                return

            if not force and (width, height) == self._last_size:
                return

            self._last_size = (width, height)

            self._canvas.delete("all")

            # 两层画法：外层用描边色铺满（圆角），内层用填充色缩进 2px 盖上去。
            # 一层"填充+描边"的写法在圆角处会露出画布底色，看起来像没填满
            # （用户报的"选中的那个里面不是完全填色"）。
            rounded_rect(
                self._canvas, 0, 0, width, height, TILE_RADIUS,
                fill=self._border, outline=self._border,
            )
            rounded_rect(
                self._canvas, 2, 2, width - 2, height - 2,
                max(0, TILE_RADIUS - 2),
                fill=self._fill, outline=self._fill,
            )


def surface_color(widget, fallback=None):
    """
    父容器的底色 —— 圆角控件（按钮 / 瓷砖 / 卡片）的画布必须用它当底色。

    不这么做的话，圆角外面会露出一圈"方方正正"的底色，看起来非常违和
    （用户报过两次：一次是瓷砖里一圈白色，一次是按钮周围一圈浅色）。
    """
    with contextlib.suppress(Exception):
        value = widget.cget("bg")

        if value:
            return value

    return fallback or CARD


def rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """
    在 Canvas 上画一个圆角矩形（tkinter 没有现成的）。

    用"平滑多边形"这个经典做法：把每个角拆成两个点，smooth=True 之后
    Tk 会把它插值成弧线。半径会自动收缩到短边的一半以内，避免小控件变形。
    """
    radius = max(0, min(int(radius), (x2 - x1) // 2, (y2 - y1) // 2))

    if radius <= 0:
        return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)

    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]

    return canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


# ============================================================
# 复选框（替代 tk.Checkbutton 那个系统方框）
# ============================================================

class CheckBox(tk.Frame):
    """
    圆角复选框：一个小方框（描边 + 选中时填充强调色 + 一个画出来的勾）+ 一行文字。

    ============================================================================
      为什么不用 tk.Checkbutton
    ============================================================================

    tk.Checkbutton 的方块是 Windows 自己画的（跟着"显示缩放 / 高对比度 /
    经典主题"走），在暗色界面上是一块突兀的浅色方框，而且没法调圆角、没法调
    勾的颜色 —— 用户看到的就是"好复古"。这里自己画：方框是圆角矩形，
    配色全部取自当前主题。

    ============================================================================
      为什么要"组合"而不是画一个 Canvas
    ============================================================================

    The label has to wrap (「记住密码（加密保存在本机；…）」 is a long sentence),
    so it stays a real tk.Label: the text is selectable by the widget walker for
    translation and it wraps with `wraplength` exactly like the old widget did.

    ============================================================================
      它和 tk.Checkbutton 的兼容面（够用就好，不是完整实现）
    ============================================================================

        checkbox.variable          -> 传进来的那一个 tk.BooleanVar（同一个对象）
        checkbox.cget("text")      -> 标签文字
        checkbox["state"]          -> "normal" / "disabled"
        checkbox.configure(...)    -> text / state / variable / bg / fg / font
        checkbox.invoke()          -> 切换并调用 command（和 tk.Checkbutton 一样）
        checkbox.select()/.deselect()/.toggle()
        Tab 能停到它上面，空格 / 回车切换

    变量是【外面传进来的那一个】：调用方继续用 .get() / .set() 读写老变量，
    一行都不用改 —— 这也是它敢替换 tk.Checkbutton 的前提。
    """

    #: 方框边长（像素）
    BOX_SIZE = 20
    #: 方框圆角半径
    BOX_RADIUS = 6

    def __init__(self, master, text="", variable=None, command=None,
                 bg=None, fg=None, font=None, wraplength=0, **ignored):
        # 圆角外面不能露出一圈异色，所以底色默认跟着父容器（同 RoundButton）。
        surface = bg or surface_color(master)

        super().__init__(master, bg=surface, highlightthickness=0, bd=0,
                         takefocus=1)

        self._command = command
        self._state = "normal"
        self._fg = fg if fg is not None else FG_DIM
        self._font = font if font is not None else FONT_SMALL
        self._hover = False
        self._focused = False
        self._trace = None

        self.variable = (
            variable if variable is not None else tk.BooleanVar(value=False)
        )

        self._canvas = tk.Canvas(
            self,
            width=self.BOX_SIZE,
            height=self.BOX_SIZE,
            bg=surface,
            highlightthickness=0,
            bd=0,
            takefocus=0,
            cursor="hand2",
        )
        self._canvas.pack(side="left", anchor="n", pady=(2, 0))

        self._label = tk.Label(
            self,
            text=text,
            bg=surface,
            fg=self._fg,
            font=self._font,
            anchor="w",
            justify="left",
            takefocus=0,
            cursor="hand2",
            wraplength=wraplength or 0,
        )
        self._label.pack(side="left", anchor="n", padx=(10, 0), pady=(2, 0))

        # 任何地方 .set() 这个变量，方框都跟着变（不只是"点它"才变）。
        with contextlib.suppress(Exception):
            self._trace = self.variable.trace_add("write", self._on_var_write)

        for widget in (self, self._canvas, self._label):
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

        # 键盘：空格切换（tk.Checkbutton 就是这个键），回车也顺手接受。
        self.bind("<space>", self._on_key)
        self.bind("<Return>", self._on_key)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)

        # 对话框销毁时把变量上的 trace 摘掉：留着会在变量活下去的时候去
        # configure 一个已经不存在的控件。
        self.bind("<Destroy>", self._on_destroy)

        self._draw()

    # ---- tk.Checkbutton 的那几个口 ----

    def cget(self, key):
        key = str(key).lower()

        if key == "text":
            return self._label.cget("text")

        if key == "state":
            return self._state

        if key == "variable":
            return str(self.variable)

        if key in ("bg", "background"):
            return self._bg_value()

        if key in ("fg", "foreground"):
            return self._fg

        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def configure(self, **kwargs):  # noqa: D401 - tk 的接口名
        redraw = False

        for key, value in kwargs.items():
            key = key.lower()

            if key == "text":
                self._label.configure(text=str(value))
            elif key == "state":
                self._state = str(value)
                redraw = True
            elif key == "variable":
                self._set_variable(value)
                redraw = True
            elif key in ("bg", "background"):
                self._paint_surface(value)
            elif key in ("fg", "foreground"):
                self._fg = value
                self._refresh_label_colour()
            elif key == "font":
                self._font = value
                self._label.configure(font=value)
            elif key == "command":
                self._command = value
            elif key == "wraplength":
                self._label.configure(wraplength=value)

        if redraw:
            self._draw()

    config = configure

    def select(self):
        self.variable.set(True)

    def deselect(self):
        self.variable.set(False)

    def toggle(self):
        self.variable.set(not bool(self.variable.get()))

    def invoke(self):
        """切换 + 调用 command（和 tk.Checkbutton.invoke() 同一个语义）。"""
        if self._state == "disabled":
            return

        self.toggle()

        if callable(self._command):
            with contextlib.suppress(Exception):
                self._command()

    # ---- 内部 ----

    def _bg_value(self):
        with contextlib.suppress(Exception):
            return super().cget("bg")

        return CARD

    def _set_variable(self, variable):
        with contextlib.suppress(Exception):
            if self._trace is not None:
                self.variable.trace_remove("write", self._trace)
                self._trace = None

        if variable is None:
            variable = tk.BooleanVar(value=False)

        self.variable = variable

        with contextlib.suppress(Exception):
            self._trace = self.variable.trace_add("write", self._on_var_write)

    def _paint_surface(self, colour):
        with contextlib.suppress(Exception):
            super().configure(bg=colour)
            self._canvas.configure(bg=colour)
            self._label.configure(bg=colour)

    def _on_var_write(self, *_args):
        self._draw()

    def _on_destroy(self, event=None):
        if event is not None and getattr(event, "widget", None) is not self:
            return

        with contextlib.suppress(Exception):
            if self._trace is not None:
                self.variable.trace_remove("write", self._trace)
                self._trace = None

    def _on_click(self, _event=None):
        if self._state == "disabled":
            return "break"

        with contextlib.suppress(Exception):
            self.focus_set()

        self.invoke()

        return "break"

    def _on_key(self, _event=None):
        self.invoke()

        return "break"

    def _on_enter(self, _event=None):
        self._hover = True
        self._draw()
        self._refresh_label_colour()

    def _on_leave(self, _event=None):
        self._hover = False
        self._draw()
        self._refresh_label_colour()

    def _on_focus_in(self, _event=None):
        self._focused = True
        self._draw()
        self._refresh_label_colour()

    def _on_focus_out(self, _event=None):
        self._focused = False
        self._draw()
        self._refresh_label_colour()

    def _refresh_label_colour(self):
        """指针在上面 / 有焦点时文字亮一档（纯观感）。"""
        colour = self._fg

        if self._state != "disabled" and (self._hover or self._focused):
            colour = FG

        with contextlib.suppress(Exception):
            self._label.configure(fg=colour)

    def _draw(self):
        with contextlib.suppress(Exception):
            checked = bool(self.variable.get())

            fill, outline, mark = self._box_colors(checked)

            self._canvas.delete("all")

            size = self.BOX_SIZE

            rounded_rect(
                self._canvas, 0.5, 0.5, size - 0.5, size - 0.5,
                self.BOX_RADIUS, fill=fill, outline=outline,
            )

            if checked:
                # 勾用折线画，不用字体里的 "✓"：不依赖字形，缩放也不会虚。
                self._canvas.create_line(
                    size * 0.26, size * 0.52,
                    size * 0.43, size * 0.70,
                    size * 0.76, size * 0.28,
                    fill=mark, width=2, capstyle="round", joinstyle="round",
                )

    def _box_colors(self, checked):
        """(填充, 描边, 勾) —— 全部来自当前主题的配色常量。"""
        if self._state == "disabled":
            if checked:
                return LOCKED, LOCKED, CARD
            return CARD, BORDER, LOCKED

        if checked:
            return ACCENT, ACCENT, ON_ACCENT

        # 没勾：深一档的表面色 + 边框色；指针在上面或者有焦点时描边变强调色
        # （等于自己画了一个焦点环）。
        outline = ACCENT if (self._hover or self._focused) else BORDER

        return CARD_ALT, outline, FG


# ============================================================
# 图标（logo）
# ============================================================

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

    ★ M6：logo 现在来自品牌配置（branding.json 的 `logo_file`）；没有品牌
    文件时用内置的 `app.png`（= 当前项目状态，见 scripts/make_assets.py
    生成的那个等距草方块）。
    """
    path = logo_path()

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


def branding_logo_image():
    """
    品牌配置里指定的 logo 图片路径（**不含内置回退**）。

    关于页用它来判断"这个 logo 是品牌给的还是内置的"，所以这里不兜底。
    """
    try:
        return branding_mod.load().logo_image()
    except Exception:
        return ""


def port_text(value):
    """
    端口在输入框里应该显示成什么。

    没有端口（0 / None / 空 / 乱码）-> 空串。**不能显示 "0"**：用户会以为
    "0 就是默认端口"，然后带着一个连不上的端口去点连接。D10 之后"没有默认
    端口"是一个正常状态，界面上就该是空的。
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return ""

    return str(port) if 1 <= port <= 65535 else ""


def logo_path():
    """界面显示用的 logo 路径：品牌 logo 优先，否则内置 app.png。"""
    return branding_logo_image() or asset_path("app.png")


def apply_window_icon(root):
    """
    给窗口装图标（任务栏 / 标题栏）。

    三种情况都要能跑：
      * 品牌给的是 .ico  -> iconbitmap（Windows 最稳）
      * 品牌给的是 .png  -> iconphoto
      * 没有品牌 / 读失败 -> 内置 app.ico

    图标只是外观：任何一步失败都不许影响启动，所以全部包在 suppress 里。
    """
    icon = ""

    try:
        icon = branding_mod.load().logo_icon()
    except Exception:
        icon = ""

    if icon:
        with contextlib.suppress(Exception):
            root.iconbitmap(icon)
            return

    image = branding_logo_image()

    if image:
        with contextlib.suppress(Exception):
            # 引用必须留住：PhotoImage 被 GC 掉图标就变空白。
            root._branding_icon_image = tk.PhotoImage(file=image)  # noqa: SLF001
            root.iconphoto(True, root._branding_icon_image)
            return

    builtin = asset_path("app.ico")

    if builtin:
        with contextlib.suppress(Exception):
            root.iconbitmap(builtin)


# ============================================================
# Main application
# ============================================================

#:
#: 静默窗口是"模式热切换期间界面保持住连接态"，它必须**永远**有尽头 —— 一旦因为
#: 任何原因没人来清（重建被提前挡回 / 事件丢了 / 线程挂了），界面就会被硬按在
#: "在连接"，表现为**按钮不释放、停止无效、新房间连不上**。所以给它一个上限，
#: 超时无条件恢复（并记一行日志）。
MODE_SWITCH_SILENT_MAX_S = 45.0

#: ★ 2026-10-03：`_mode_rebuild_pending`（"等房间广播里的新模式到了再重建"）
#: 的**最长期限**。中继先发 `CONN_MODE_CHANGED`、后发 `HOST_STATE mode=<新>`，
#: 万一那一行丢了/迟了，标记会一直挂着 —— 而它一旦挂着，就会：
#:   * 吞掉"真的 host 停服"（其他 player 不断开）；
#:   * 吞掉 tick 里"会话静默死亡"的复位。
#: 所以它必须有尽头：到点就当没这回事，并记一行日志。
MODE_REBUILD_PENDING_MAX_S = 20.0

#: ★ v3.9.16：被**推迟**的 host-gone 最多等多久。
#:
#: 模式重建窗口里的 host-gone 现在是"缓存 + 补跑"，不是丢弃。但"窗口什么时候
#: 结束"这件事本身也可能没人来通知（worker 挂了 / 事件丢了），所以给它一个
#: 硬上限：到点无条件补跑。
#:
#: 为什么是 8 秒：模式重建（stop + start）实测 1～3 秒；给到 8 秒既能盖住一次
#: 正常的重建，又不会让用户盯着"已连接"等太久 —— 而且断不断早由核心层决定了，
#: 这里等掉的只是提示与按钮的刷新时机。
HOST_GONE_DEFER_MAX_S = 8.0

class MCLinkApp:

    def __init__(self, root):
        self.root = root
        self.engine = MCLinkEngine()

        self.config = cfgmod.load_config()

        #: 「基准配置」那三个数字有没有从磁盘装载过。
        #:
        #: 它区分两件完全不同的事（第一版混在一起，切一次语言就把三个数字清空）：
        #:   False -> 程序刚起来：按 config.first_run 决定"预填上一次的值"还是"留空"
        #:   True  -> 只是重建界面（切语言/主题）：一切以内存里的 _committed 为准
        self._config_loaded_from_disk = False

        self.log_queue = queue.Queue()
        self.running = False
        self.last_snapshot = {}
        self._last_ui_refresh = 0.0
        self._last_info_players = None
        self._last_rates = {}
        self._last_emit_errors = []

        #: 「房间被关闭」的弹窗只弹一次（ROOM REVOKED 和 room_closed 会先后到）。
        #: 每次成功进入房间时复位。
        self._room_close_notified = False

        # 版本门：同一次运行里只弹一次「需要更新」，避免重连刷屏。
        self._version_popup_shown = False

        #: 身份同步（IDENTITY，ISSUES 问题 5）：最后一次**被中继确认**的身份、
        #: 最后一次**尝试过**的身份、以及尝试的时刻（5 秒冷却用）。
        self._identity_sent = None
        self._identity_attempted = None
        self._identity_sent_at = 0.0

        #: 游客身份（ISSUES 问题 5/6，用户 2026-09-27 口径）。
        #:
        #: **登录会话级**：中继第一次给我们的「游客xxxxxx」记在这里，
        #: 退房 / 被踢 / 房间被关**都不销毁**（下次建房或加房还用这个名字）；
        #: 只有登出 / 换账号 / 重启软件才清掉。
        #: **绝不写进配置文件**（一次性的临时身份），所以它只是一份内存状态。
        self._guest_name = ""
        self._guest_token = ""

        #: File logging lives in `mclink.log` now (one process-wide logger), so
        #: there is no per-window logger object to hold here any more.
        self._config_file_path = None

        # V2 login session (AuthSession) or None while logged out.
        self.auth = None

        #: 界面正在重建（切换语言/主题）时，定时刷新先让路，避免碰到
        #: 已经销毁的控件。
        self._rebuilding = False

        #: V3 直连状态：最近一次本机 NAT 画像（由直连协调器填），供运行状态
        #: 卡片显示。诊断不再是独立栏目，所以它只是显示用的一份缓存。
        self._nat_profile = None

        #: 3.0.9 房间成员列表：窗口、按成员复用的行控件、正在重入的守卫。
        #: 行控件按成员 key 复用（只改文字），所以实时刷新不会闪、不会抢焦点。
        self.member_window = None
        self.member_table = None
        self.member_hint = None
        self.member_rows = {}
        #: 每个成员当前显示的名字（踢人确认用它，而不是 client_id）。
        self.member_names_by_key = {}
        self._member_list_filling = False

        #: 3.0.9「一人一行」的直连行（运行状态卡片里）：peer -> (frame, label)。
        self.hole_rows_frame = None
        self._hole_rows = {}

        #: 会话模式同步（非房主的热切换自动重建）的防抖状态。
        #: 见 `_sync_session_mode` / `_session_mode_rebuild_delta`。
        self._rebuild_in_flight = False
        self._last_session_mode_rebuild_at = 0.0
        self._session_mode_rebuild_target = None

        #: ★ 用户 2026-10-02：**模式热切换的重建是静默的**。
        #:
        #: 重建内部会 `stop_session()`（重建的必要一步），而"停止"那条路会把
        #: 连接按钮放掉、把状态栏写成"未连接"。可这是**房主换模式**触发的自动
        #: 重建，玩家什么都不该感觉到（用户原话：「连接按钮不应该被释放，且状态
        #: 栏需要正常显示，仅静默自动重连即可」）。这个标志为 True 期间，事件泵
        #: 每轮都把界面按回"在连接"，直到 `rebuild_done`（按新模式正常显示）或
        #: `rebuild_failed`（那时该报错就报错、该放手就放手）。
        self._mode_switch_silent = False
        self._mode_switch_silent_until = 0.0

        #: ★ 2026-10-03（热切换分析 P2）：收到 `CONN_MODE_CHANGED` 那一刻的
        #: **切换前**房间模式。中继先发那一行、后发 `HOST_STATE mode=<新>`，
        #: 所以"房间模式仍是这个旧值"= 新模式还没到 —— 那一刻绝不能拿旧模式
        #: 重建，也绝不能清掉待重建标记（旧代码清了，host 于是永不重建）。
        self._mode_rebuild_from = None

        #: "待重建"标记的期限（秒）。到期无条件清掉并记日志 —— 见 P4 回归的教训。
        self._mode_rebuild_pending_until = 0.0

        #: "下一次真正开始的重建请静默"。模式驱动的调用方只设这一个标记，由
        #: `_rebuild_tunnels` 在**通过了全部提前返回之后**消费它 —— 调用点直接设
        #: `_mode_switch_silent` 就是 2026-10-03 那个致命回归（重建被挡回 ->
        #: 标志永远没人清 -> 界面被硬按在"在连接"，停止/新房间全废）。
        self._mode_rebuild_silent_next = False

        #: 中继推过 `CONN_MODE_CHANGED` 但房间广播里的新模式可能还没到：
        #: 那一天的处理放在 `_on_conn_mode_changed` / `_maybe_rebuild_for_mode_change`。
        self._mode_rebuild_pending = False

        #: 「我点了开始连接」的上报状态（`STARTED 1/0`，PROTOCOL §8.6）：
        #: 同一个会话里同一个状态只发一次（None = 还没发过）。
        self._started_reported = None

        #: ★ v3.9.16：被推迟的 host-gone（模式重建窗口里收到的那一条）。
        #:
        #: 以前窗口期内这类事件被**丢弃**，那就是"host 断开后其余人不断开"的
        #: 直接原因。现在只缓存、不丢弃，由 `_flush_pending_host_gone()` 在
        #: 窗口结束后补跑。见 `HOST_GONE_DEFER_MAX_S`。
        self._pending_host_gone = None


        # ---- 外观：语言 + 主题（在任何控件被创建之前定下来）----
        i18n.set_language(getattr(self.config, "ui_language", "zh"))
        apply_theme(getattr(self.config, "ui_theme", "dark"))

        self._setup_file_logging()

        # Must run before any widget reads username_var / client_id_var: the
        # visible "身份" card that used to create them is gone, so they are
        # plain state now.
        self._init_identity_vars()

        self._build_window()
        self._build_styles()
        self._build_layout()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._refresh_account_ui()

        # The startup line goes into the FILE through the structured logger, so
        # it is one parseable fact rather than a translated sentence: the log is
        # the audit record and does not follow the UI language.
        logmod.event(
            "client.started",
            version=cfgmod.version_line(),
            config=self._config_file_path or cfgmod.config_path(),
        )
        self.log(
            f"CraftLink started ({cfgmod.version_line()})",
            kind="info",
        )
        self.log(
            f"config file: {self._config_file_path or cfgmod.config_path()}",
            kind="dim",
        )

        # ★ M6：品牌 / 数据目录 / 首次运行引导。
        #
        # 顺序有讲究：先说品牌文件的警告（如果有），再说数据目录迁移的提示，
        # 最后才弹首次运行引导 —— 引导是给用户看的，日志是给排查用的。
        self._show_branding_warnings()
        self._show_data_dir_notice()

        # 引导用 after 排一拍：等窗口画完再弹，不然它会出现在窗口还没有位置
        # 的时候（位置算不出来，弹到屏幕角上）。
        with contextlib.suppress(Exception):
            self.root.after(400, self._maybe_show_first_run_guide)

        self.root.after(TICK_MS, self._tick)

    def _show_data_dir_notice(self):
        """
        数据目录相关的"必须让用户知道"的事（迁移结果、目录不可用）。

        只提示一次：`userdirs.pop_notice()` 取走即清空。用**非模态**窗口而
        不是 messagebox：它可能在启动流程里被触发，模态框会卡住启动时的
        其它刷新（而且日志已经记了一份）。
        """
        notice = userdirs_mod.pop_notice()

        if not notice:
            return

        message = notice.get("message") or ""
        kind = "warn" if notice.get("kind") == "warn" else "info"

        if not message:
            return

        self.log(
            "data directory notice: " + " ".join(message.split()),
            kind=kind,
        )

        self._info_window(tr("数据目录"), message)

    def _info_window(self, title, text, width=520):
        """
        非模态信息窗口（启动期提示用，绝不阻塞 UI 线程）。

        失败一律吞掉：提示只是提示，不能因为它让客户端起不来。
        """
        try:
            win = tk.Toplevel(self.root)
            win.title(tr(title))
            win.configure(bg=BG)
            win.transient(self.root)

            body = tk.Frame(win, bg=BG)
            body.pack(fill="both", expand=True, padx=18, pady=16)

            tk.Label(
                body,
                text=tr(text),
                bg=BG,
                fg=FG,
                font=FONT_SMALL,
                justify="left",
                anchor="w",
                wraplength=width,
            ).pack(fill="x")

            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=(14, 0))

            self._button(row, "关闭", win.destroy, width=8).pack(side="right")

            win.update_idletasks()
            x = self.root.winfo_rootx() + 80
            y = self.root.winfo_rooty() + 100
            win.geometry(f"+{int(x)}+{int(y)}")

            return win
        except Exception:
            return None

    # --------------------------------------------------------
    # 外观（语言 / 主题）：只改观感，不动任何状态
    # --------------------------------------------------------

    def on_language_change(self, _event=None):
        chosen = self.language_var.get()

        for code, label in i18n.LANGUAGES:
            if label == chosen:
                self.config.ui_language = code
                break

        self._apply_appearance()

    def on_theme_change(self, _event=None):
        # 下拉框里显示的是【翻译过】的名字（暗色 / Dark / ダーク），
        # 存进配置的必须是 dark / light / system 这三个代码。
        chosen = self.theme_var.get()

        for code, label in theme_mod.THEME_LABELS:
            if chosen == tr(label) or chosen == code:
                self.config.ui_theme = code
                break

        self._apply_appearance()

    def _apply_appearance(self):
        """
        应用新的语言/配色：存进配置 -> 重建界面。

        重建的是【控件】，状态全在 MCLinkApp 自己的属性里（账号、房间、
        正在跑的会话、日志内容），所以功能一个都不会丢。这也是为什么
        切换外观不需要重启，也不会影响联机。
        """
        i18n.set_language(getattr(self.config, "ui_language", "zh"))
        apply_theme(getattr(self.config, "ui_theme", "dark"))

        with contextlib.suppress(Exception):
            cfgmod.save_config(self.config)

        # 对话框（登录 / 改名）用的是快照式的颜色常量，这里让它们重新取一遍。
        with contextlib.suppress(Exception):
            from . import auth_ui

            auth_ui.refresh_palette()

        self.rebuild_ui()

        self.log(
            f"appearance switched: language {i18n.language_label()}, "
            f"theme {theme_mod.resolved()}",
            kind="dim",
        )

    #: 重建界面时必须原样保住的输入框内容。
    #:
    #: WHY THIS LIST EXISTS: rebuilding destroys the widgets, and every text
    #: field is a StringVar created inside those build methods. Without this,
    #: "切个语言" quietly wiped the room name, the ports, the capacity and the
    #: chosen identity -- the appearance switch looked like a reset button.
    FORM_VARS = (
        "mode_var",
        "room_var",
        "host_var",
        "port_var",
        "mc_port_var",
        "local_port_var",
        "max_players_var",
        "username_var",
        "client_id_var",
    )

    def rebuild_ui(self):
        """用当前语言/配色重画整个窗口（控件重建，状态保留）。"""
        self._rebuilding = True

        # 表单里已经填好的东西先记下来，重建之后再放回去。
        form = {}

        for name in self.FORM_VARS:
            var = getattr(self, name, None)

            if var is not None:
                with contextlib.suppress(Exception):
                    form[name] = var.get()

        try:
            log_text = ""

            with contextlib.suppress(Exception):
                # 【原样】取出来（end-1c = 不含 Tk 自己那个收尾换行），重建后
                # 原样放回去。以前是 strip() 之后再补一个 "\n"，每切一次外观
                # 日志就多一个空行 —— 切十次就多十行。
                log_text = self.log_text.get("1.0", "end-1c")

            for child in list(self.root.winfo_children()):
                with contextlib.suppress(Exception):
                    child.destroy()

            # 成员窗口是 root 的子窗口，上面那轮一并销毁了 —— 引用也要清掉，
            # 否则后面的刷新会拿着一个已经死掉的控件去找 winfo_exists()。
            self.member_window = None
            self.member_table = None
            self.member_hint = None
            self.member_rows = {}
            self.member_names_by_key = {}

            # ============================================================================
            #  重建控件前必须清掉"控件登记表"
            # ============================================================================
            #
            # _tile_groups 里存的是瓷砖控件对象。控件被销毁之后，这份名单还留着
            # 死引用，下一次 _refresh_tiles() 就会去配置一个已经不存在的窗口，
            # Tcl 直接报 "bad window path name"。
            #
            # 以前没人发现，是因为那时候用的是 `widget.configure(bg=...)` 并且外面
            # 套了 suppress(TclError) —— 异常被吞掉了。圆角瓷砖换成方法调用之后，
            # 异常就冒出来了（测试立刻变红，这是好事）。
            self._tile_groups = []

            # 窗口本身的底色不在任何卡片里，重建子控件不会碰到它 ——
            # 少了这一行，亮色主题下窗口四周仍是深色。
            with contextlib.suppress(Exception):
                self.root.configure(bg=BG)

            self._build_styles()
            self._build_layout()

            # ---- 还原表单 ----
            for name, value in form.items():
                var = getattr(self, name, None)

                if var is not None:
                    with contextlib.suppress(Exception):
                        var.set(value)

            # 模式提示文字 / 身份对应的端口输入框，跟 run() 里初次构建后
            # 做的事完全一样 —— 不重放这两步，重建后的提示文字和显示哪一组
            # 端口框就会跟启动时不一致。
            with contextlib.suppress(Exception):
                self._on_mode_change()

            with contextlib.suppress(Exception):
                self._on_role_change()

            if log_text.strip() and hasattr(self, "log_text"):
                with contextlib.suppress(Exception):
                    self.log_text.configure(state="normal")
                    self.log_text.delete("1.0", "end")
                    self.log_text.insert("1.0", log_text)
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")

            # 把动态内容重新填回去（账号行、房间状态、状态卡、顶部提示）。
            self._refresh_account_ui()
            self._refresh_room_ui()
            self._refresh_status()

        finally:
            self._rebuilding = False

    # --------------------------------------------------------
    # Logging
    # --------------------------------------------------------

    def _setup_file_logging(self):
        """
        Hand file logging to `mclink.log` (the one process-wide logger).

        WHY THIS IS NOW A ONE-LINER: the file handler used to be built here, in
        the GUI, which meant (a) anything logging before the window existed had
        nowhere to go, and (b) this class and the core could disagree about the
        format. See `client/mclink/log.py` for the whole argument.
        """
        try:
            logmod.event(
                "client.log.start",
                file=logmod.log_path(),
                pid=os.getpid(),
                version=cfgmod.version_line(),
            )
        except Exception:
            pass

    def log(self, text, kind="normal"):
        """
        Thread safe: only appends to a queue.

        THE SCREEN AND THE FILE GET THE SAME STRING, BY CONSTRUCTION.

        This used to run `tr(text)` on the way to the screen, while the file got
        the untranslated text -- so a user's screenshot and the log file they
        sent back could say different things, and the one time that matters is
        exactly when something is broken and being investigated.

        The owner's instruction is explicit: the log records the RAW behaviour,
        not a translation of it. The UI keeps its own language for its own
        labels; the log does not follow the UI language at all.
        """
        with contextlib.suppress(queue.Full):
            self.log_queue.put_nowait((time.strftime("%H:%M:%S"), text, kind))

        # `dim` is the self-test / decoration tier: screen only, never the file,
        # so the file stays a record of events rather than of the window dressing.
        if kind != "dim":
            logmod.note(text)

    # --------------------------------------------------------
    # Window / style
    # --------------------------------------------------------

    def _build_window(self):
        # The build stamp is IN the title on purpose: "which exe am I running"
        # has to be answerable at a glance, not by guessing whether the fix
        # arrived. 软件名不跟随语言，后面的说明文字才翻。
        self.root.title(
            tr(f"{cfgmod.version_line()} — Minecraft 联机")
        )

        # 用户的原话：「之前那个窗口大小刚好」—— 所以要的是原来的尺寸，
        # 而不是被我撑到 1600×950 的大窗口。窗口可以自由缩放，布局会跟着适配
        # （见 _build_layout 里的"窄了就改单列"）。
        self.root.geometry("1040x800")
        self.root.minsize(900, 680)
        self.root.configure(bg=BG)

        with contextlib.suppress(Exception):
            self.root.call("tk", "scaling", 1.3)

        # 窗口图标（任务栏/标题栏那个小图标）。品牌 logo 优先，文件不在就
        # 当作没有 —— 图标是外观，不是启动条件。
        apply_window_icon(self.root)

    def _build_styles(self):
        style = ttk.Style(self.root)

        with contextlib.suppress(Exception):
            style.theme_use("clam")

        style.configure(
            ".",
            background=BG,
            foreground=FG,
            font=FONT,
            bordercolor=BORDER,
            focuscolor=ACCENT,
        )

        style.configure(
            "TEntry",
            fieldbackground=CARD_ALT,
            background=CARD_ALT,
            foreground=FG,
            insertcolor=FG,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            # 更"苹果"一点的输入框：扁平、内边距大一些（原来 padding=6，
            # 现在上下 7 左右 10），点进去更容易看清光标位置。
            relief="flat",
            borderwidth=1,
            padding=(10, 7),
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", CARD)],
            foreground=[("disabled", FG_MUTED)],
        )

        # ---- 现代输入框（Modern.TEntry）----
        #
        # 每个输入框都显式引用这个样式（app.py 里 6 个 + 登录/改名对话框 5 个，
        # 一律通过 app.ENTRY_STYLE 这个名字）。目标是一个"干净扁平的输入区"：
        # 深一档的表面色、1px 边框、宽松的内边距（10x8，原来只有 6），
        # 拿到焦点时整圈边框变成强调色。
        #
        # 为什么不直接改 TEntry：TEntry 是所有 ttk 控件的兜底样式（下拉框、
        # 表格内编辑态都沾它），改它等于整个窗口一起动；单独起一个名字，
        # 出问题也只影响"确实是输入框"的那几个控件。
        #
        # 为什么 focus 要同时改三个颜色：clam 主题里输入框的边框是 Entry.field
        # 这个元素画的，它按 bordercolor / lightcolor / darkcolor 分别画边和
        # 两个"倒角"。只改 bordercolor 的话，另外两条还是灰的，焦点环看起来
        # 是断的。
        style.configure(
            ENTRY_STYLE,
            fieldbackground=CARD_ALT,
            background=CARD_ALT,
            foreground=FG,
            insertcolor=FG,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            relief="flat",
            borderwidth=1,
            padding=(10, 8),
        )
        style.map(
            ENTRY_STYLE,
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
            # 只读 / 禁用时退回卡片底色 + 灰字，和按钮的禁用态一致。
            fieldbackground=[("disabled", CARD), ("readonly", CARD)],
            foreground=[("disabled", FG_MUTED), ("readonly", FG_DIM)],
        )

        # ---- 下拉框（顶部的语言 / 主题选择）----
        #
        # ttk.Combobox 不继承父容器的配色，不显式配就是"白底黑字"，
        # 放在暗色顶栏上像贴了一块白纸。这里连同弹出列表一起配好。
        style.configure(
            "TCombobox",
            fieldbackground=CARD_ALT,
            background=CARD_ALT,
            foreground=FG,
            arrowcolor=FG_DIM,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            selectbackground=CARD_ALT,
            selectforeground=FG,
            padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", CARD_ALT)],
            foreground=[("readonly", FG)],
            background=[("readonly", CARD_ALT)],
        )

        # 下拉列表本身是 Tk 的 Listbox，只能用 option database 调色。
        with contextlib.suppress(Exception):
            self.root.option_add("*TCombobox*Listbox.background", CARD_ALT)
            self.root.option_add("*TCombobox*Listbox.foreground", FG)
            self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
            self.root.option_add("*TCombobox*Listbox.font", FONT_SMALL)

        style.configure(
            "Mode.TRadiobutton",
            background=CARD,
            foreground=FG,
            font=FONT_BOLD,
            indicatorcolor=CARD_ALT,
        )
        style.map(
            "Mode.TRadiobutton",
            background=[("active", CARD)],
            foreground=[("disabled", LOCKED), ("active", FG)],
        )

        # ---- dark tables ----
        #
        # A ttk.Treeview ignores its parent's background and always paints the
        # widget default, which on Windows is WHITE. Dropped into the dark
        # room list that looked like a light table pasted onto a dark dialog,
        # so the whole thing is themed explicitly here.
        style.configure(
            "Dark.Treeview",
            background=LOG_BG,
            fieldbackground=LOG_BG,
            foreground=LOG_FG,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            relief="flat",
            borderwidth=0,
            # 行更高 = 更透气（26 -> 30），接近手机/网页那种列表观感。
            rowheight=30,
            font=FONT,
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", ACCENT_DARK)],
            foreground=[("selected", "#ffffff")],
        )

        style.configure(
            "Dark.Treeview.Heading",
            background=CARD_ALT,
            foreground=FG_DIM,
            # 表头比表体小一档：列宽因此能压到 456px 还不裁字（见 _build_room_card）。
            font=FONT_SMALL,
            relief="flat",
            padding=(5, 6),
        )
        style.map(
            "Dark.Treeview.Heading",
            background=[("active", BORDER)],
            foreground=[("active", FG)],
        )

        style.configure(
            "Dark.Vertical.TScrollbar",
            background=CARD_ALT,
            troughcolor=BG,
            bordercolor=BG,
            arrowcolor=FG_DIM,
            relief="flat",
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", BORDER)],
        )

        style.configure(
            "TNotebook",
            background=BG,
            bordercolor=BORDER,
        )
        style.configure(
            "TNotebook.Tab",
            background=CARD,
            foreground=FG_DIM,
            padding=(16, 8),
            font=FONT_BOLD,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD_ALT)],
            foreground=[("selected", FG)],
        )

        style.configure(
            "Vertical.TScrollbar",
            background=CARD_ALT,
            troughcolor=BG,
            bordercolor=BG,
            arrowcolor=FG_DIM,
        )

    # --------------------------------------------------------
    # Layout
    # --------------------------------------------------------

    #: 两列布局的【下限】宽度（像素）。真正决定"两列还是单列"的是 _column_plan()
    #: 量出来的实际需要宽度，这个常数只是兜底：窗口比它还窄就直接单列，不用量。
    TWO_COLUMN_MIN_WIDTH = 960

    #: 两列时左右两列的宽度配比（左 : 右 = 3 : 2 = 60% : 40%）。
    #: 左列是"连接模式 / 账号"（主要操作），右列是"身份配置 / 操作"。
    COLUMN_WEIGHTS = (3, 2)

    #: 两列之间的水平间距（左列右侧 10px + 右列左侧 10px）。
    COLUMN_GAP = 20

    #: 额外的安全余量：两列"需要的宽度"加起来还得再留这么多，才认为放得下。
    #: 留余量是因为有些文字是运行期才填进去的（房间状态、连接提示），
    #: 刚好卡在临界值上迟早会被裁掉一条。
    COLUMN_SLACK = 16

    def _column_space(self):
        """
        两列一共能用多少水平像素（= 中间容器的宽度 - 列间距）。

        还没映射（宽度=1）时用窗口宽度估算：34 = outer 卡片区左右各 18。
        """
        _left, _right, middle = self._columns

        try:
            width = int(middle.winfo_width())
        except Exception:
            width = 0

        if width <= 1:
            with contextlib.suppress(Exception):
                width = int(self.root.winfo_width()) - 36

        return max(1, width - self.COLUMN_GAP)

    def _column_plan(self):
        """
        这个宽度下该怎么摆两列？返回 (mode, left_px, right_px)。

        ============================================================================
          为什么是"量出来的"，而不是写死一个阈值
        ============================================================================

        控件的"需要的宽度"取决于**语言**（中文/日文/英文差很多）和 **tk scaling**
        （本程序把缩放设成 1.3，所以 11pt 的字实际有 14px 高）。一个写死的
        980 在中文 + 1.3 缩放下够用，换到日文就差几十像素 —— 而差几十像素的结果
        就是右列被裁掉一条（用户报的正是"装不下 / 右边怎么空着"）。

        所以这里直接问控件：`左列需要的 + 右列需要的 + 余量 <= 可用宽度`吗？
        放不下就退成单列（单列时每张卡片都是整行宽，永远不会被裁）。

        ============================================================================
          宽度怎么分：左列 60%，右列 40%，但右列不会被压到装不下
        ============================================================================

        `left_px` 取"目标值（60%）"和"可用宽度 - 右列需要的"两者中较小的那个，
        所以：

          * 宽窗口下就是干净的 60 / 40（不是 pack 的 50/50）；
          * 紧张时左列先让路，右列永远拿到它需要的宽度（一个像素都不裁）；
          * 两列都写下限（minsize），等于把比例钉死，不会再被内容反向拉扯。
        """
        left, right, _middle = self._columns

        left_req = int(left.winfo_reqwidth())
        right_req = int(right.winfo_reqwidth())
        space = self._column_space()

        try:
            window = int(self.root.winfo_width())
        except Exception:
            window = 0

        fits = (
            window >= self.TWO_COLUMN_MIN_WIDTH
            and space >= left_req + right_req + self.COLUMN_SLACK
        )

        if not fits:
            return "one", 0, 0

        left_weight, right_weight = self.COLUMN_WEIGHTS
        target = int(space * left_weight / (left_weight + right_weight))

        left_px = max(left_req, min(target, space - right_req))
        right_px = max(right_req, space - left_px)

        return "two", left_px, right_px

    def _reflow_columns(self, first=False):
        """
        按当前窗口宽度摆放两个容器（两列 / 单列）。只重新摆放，不重建控件。

        为什么需要它：两列并排在宽窗口下好看，但窗口一窄（或者用户把窗口拉小），
        右列就会被挤到横向滚动区外面 —— 用户看到的就是"右边怎么空着"。
        横向滚动是被明确禁止的（ScrollFrame 把滚动区钉死在画布宽度上），
        所以这里必须【在裁到之前】就切成单列。
        """
        with contextlib.suppress(Exception):
            left, right, middle = self._columns

            try:
                if int(self.root.winfo_width()) <= 1:
                    return  # 还没映射，等下一次 Configure
            except Exception:
                return

            mode, left_px, right_px = self._column_plan()
            plan = (mode, left_px, right_px)

            # 防抖：只有"摆放方案"真的变了才动 grid（拖动窗口时这个回调
            # 一秒会跑几十次，而每次重排都会再触发一轮 <Configure>）。
            if plan == getattr(self, "_column_plan_cache", None) and not first:
                return

            self._column_plan_cache = plan
            self._column_mode = mode

            for frame in (left, right):
                with contextlib.suppress(Exception):
                    frame.grid_forget()

            if mode == "two":
                # 用 grid 而不是 pack：pack 的 expand 只能 50/50，
                # 而这里要的是 60/40 且两列都不许低于自己需要的宽度
                # （grid 的 minsize 正好是"下限"，weight 只分多出来的空间）。
                left_weight, right_weight = self.COLUMN_WEIGHTS

                middle.grid_columnconfigure(
                    0, weight=left_weight, minsize=left_px
                )
                middle.grid_columnconfigure(
                    1, weight=right_weight, minsize=right_px
                )

                left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
                right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
            else:
                middle.grid_columnconfigure(0, weight=1, minsize=0)
                middle.grid_columnconfigure(1, weight=0, minsize=0)

                left.grid(row=0, column=0, sticky="new")
                right.grid(row=1, column=0, sticky="new", pady=(14, 0))

    def _build_layout(self):
        # The header lives OUTSIDE the scroll area, so it stays visible no
        # matter how far down the page is scrolled. It carries the relay
        # reachability indicator, and having it scroll away meant the user had
        # to scroll down to the test button, click it, then scroll back up to
        # read the result.
        header_bar = tk.Frame(self.root, bg=BG, highlightbackground=BORDER,
                              highlightthickness=0)
        header_bar.pack(fill="x", side="top")

        header_inner = tk.Frame(header_bar, bg=BG)
        header_inner.pack(fill="x", padx=18, pady=(14, 10))

        self._build_header(header_inner)

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", side="top")

        # Everything below the header scrolls, by MOUSE WHEEL only (no
        # scrollbar). The log box keeps its own wheel, see install_wheel().
        self.page = ScrollFrame(self.root, bg=BG)
        self.page.pack(fill="both", expand=True, side="top")

        outer = tk.Frame(self.page.inner, bg=BG)
        outer.pack(fill="both", expand=True, padx=18, pady=16)

        # The server card spans the full width directly under the header
        # instead of sitting at the bottom of a column: "test the server" is
        # the first thing a user does, and it is the control they reach for
        # most often.
        self._build_server_card(outer)

        middle = tk.Frame(outer, bg=BG)
        middle.pack(fill="both", expand=True, pady=(14, 0))

        # ============================================================================
        #  两列布局 + 窄窗口自动变单列（用户要的"手动缩放要做适配"）
        # ============================================================================
        #
        # 左列：连接模式 / 账号
        # 右列：身份配置 / 操作
        #
        # 窗口拉窄到放不下两列时（_reflow_columns 量出来的实际需要宽度），右列
        # 自动挪到左列下面，避免出现"右列被挤出窗口、要横向滚动才看得到"的老问题。
        # 这里只决定【放哪一列】，控件本身一个都不动。
        #
        # ============================================================================
        #  「房间」「运行状态」不在列里，它们和「日志」一样是整行宽的
        # ============================================================================
        #
        # 用户：「房间和运行状态这里 我说的是这俩它右边是空的呀，为啥不和这个日志
        # 一样长度」。它们原来在左列里，所以只能长到左列那么宽，右边空一大片。
        # 现在直接挂在 outer 上（= 内容区整宽），纵向照样 expand 拉满。
        left = tk.Frame(middle, bg=BG)
        right = tk.Frame(middle, bg=BG)

        self._columns = (left, right, middle)
        self._column_mode = ""

        self._build_mode_card(left)
        self._build_account_card(left)

        # ★ M6：数据目录卡片（日志/配置/记忆在哪，以及随时改）
        self._build_data_card(left)

        self._build_role_card(right)
        self._build_action_card(right)

        self._reflow_columns(first=True)

        # 窗口尺寸一变就重新决定"两列还是单列"。防抖：只在模式真的改变时重排。
        middle.bind("<Configure>", lambda _e: self._reflow_columns())
        left.bind("<Configure>", lambda _e: self._reflow_columns())

        # 房间里最宽的东西是房间列表（Treeview），它的列宽是像素、不随语言和
        # scaling 变，所以列宽之和必须自己守住"远小于 520px"这条线（见
        # _build_room_card）。卡片挂在这里 = 整行宽，列表跟着窗口变宽。
        # ============================================================================
        #  v3：网络诊断不再是一张独立卡片（用户要求移除）
        # ============================================================================
        #
        # 它以前会卡住、而且和"连接状态"割裂成了两块。现在 NAT 判定和直连
        # 动态直接显示在「运行状态」卡片里（直连相关字段 + NAT 类型 + 尝试
        # 次数），一个地方说一件事。
        self._build_room_card(outer, expand=True)
        self._build_status_card(outer, expand=True)

        # ============================================================================
        #  亮色主题【不画任何渐变/晕色】（用户："去掉亮色的渐变/晕色背景"）
        # ============================================================================
        #
        # 这里原来注册了 `canvas.bind("<Configure>", lambda _e: self._paint_wash())`，
        # 两个后果：一是亮色底上铺了一层极光渐变（用户不要），二是 tkinter 的
        # bind() 默认是【替换】而不是追加 —— 它把 ScrollFrame 自己在
        # __init__ 里注册的 `_on_canvas_configure`（把内容层宽度钉死在画布宽度
        # 上）顶掉了，于是内容层按自己的"需要的宽度"长到 1248px，默认窗口
        # 1040px 装不下，只能横向滚动。删掉这条绑定，两个问题一起消失。
        #
        # 亮色留白就是留白：BG 是平的浅冷灰，卡片是纯白（见 theme.PALETTES）。

        self._build_log_card(outer)

        # ============================================================================
        #  自检：界面上的输入框和程序读的变量必须是【同一个】
        # ============================================================================
        #
        # 一个真实的、代价很大的 bug：`self.room_var` 被赋值了两次（房间卡一次、
        # 身份配置卡一次），而身份配置卡是后建的 —— 于是程序读的那个变量和输入框
        # 绑的那个变量不是同一个，用户输入什么程序都读成空。测试当时全是直接给
        # `app.room_var` 赋值，正好绕过了这条线，所以一路绿灯。
        #
        # 这一检查把"控件 ↔ 变量"的绑定关系钉死：不一致就在日志里吼一声，
        # 并且把不一致的字段名写清楚。
        self._verify_form_bindings()

        # ============================================================================
        #  自动翻译界面上所有静态文字（一处顶几百处）
        # ============================================================================
        #
        # 用户反馈"英语并未完全覆盖所有按钮和面板"。逐条在 call site 上包 tr()
        # 是几百处改动，漏掉一处根本看不出来；所以这里在建完之后遍历整棵树，
        # 把所有静态文字过一遍 tr()（详见 gui/i18n.py 的 translate_widget_tree）。
        #
        # 中文界面下这一步什么都不做（tr 直接返回原文）。
        translated, leftovers = i18n.translate_widget_tree(self.root)

        if leftovers:
            # 有漏翻的就说出来（控制台/日志），免得又变成"某个角落还是中文"。
            with contextlib.suppress(Exception):
                self.log(
                    "界面还有未翻译的文字：" + "；".join(sorted(set(leftovers))[:12]),
                    kind="dim",
                )

        # Must run after every child exists.
        self.page.finalize()
        self.page.install_wheel(self.root)

    #: 表单控件 ↔ 变量名。Entry 的 textvariable 必须就是这里的那个变量。
    FORM_WIDGETS = (
        ("room_var", "room_entry"),
        ("host_var", "host_entry"),
        ("port_var", "port_entry"),
        ("mc_port_var", "mc_port_entry"),
        ("local_port_var", "local_port_entry"),
        ("max_players_var", "max_players_entry"),
    )

    def _verify_form_bindings(self):
        """
        检查每个输入框绑的变量就是程序在读的那个变量。返回出问题的字段名列表。

        只在"名字对不上"时说话：控件可以不存在（某些卡片在不同版本里被挪过位置），
        但**只要控件在，它绑的变量就必须和 self.<name> 是同一个**。
        """
        broken = []

        for var_name, widget_name in self.FORM_WIDGETS:
            var = getattr(self, var_name, None)
            widget = getattr(self, widget_name, None)

            if var is None or widget is None:
                continue

            try:
                bound = str(widget.cget("textvariable") or "")
            except Exception:
                continue

            if bound != str(var):
                broken.append(
                    f"{widget_name} 绑的是 {bound or '(无)'}，"
                    f"程序读的是 {var}"
                )

        if broken:
            with contextlib.suppress(Exception):
                self.log(
                    "界面自检失败：输入框和程序读的不是同一个变量 —— "
                    + "；".join(broken),
                    kind="error",
                )

        return broken

    # NOTE: the light theme used to paint a soft "aurora wash" here
    # (_paint_wash + theme.WASH_LIGHT + _mix_hex). It is gone on purpose:
    # 用户原话「去掉亮色的渐变/晕色背景」. The light theme is now a flat
    # surface -- BG (#f2f4f8) with plain white cards -- and nothing repaints the
    # scroll canvas, which also restores ScrollFrame's own <Configure> binding
    # (see _build_layout).

    def _wheel_scroll_page(self, direction, steps=1):
        """
        把一次滚轮转交给页面（`ScrollFrame.scroll_by`）。

        单独包一层的原因：下拉框在**顶部工具栏**里，而页面（`self.page`）是
        `_build_layout` 才建的 —— 绑定必须能在任何时刻安全调用（拿不到就什么
        都不做，绝不抛异常）。
        """
        page = getattr(self, "page", None)

        if page is None:
            return

        with contextlib.suppress(Exception):
            page.scroll_by(direction, steps)

    def _build_header(self, parent):
        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x")

        # ---- 极光背景（只有彩色主题才画，见 _build_aurora）----
        self.aurora = None
        self._aurora_canvas = None

        left = tk.Frame(header, bg=BG)
        left.pack(side="left")

        # ---- logo ----
        #
        # 图标文件由 scripts/make_assets.py 生成，打包时随 mclink/assets 一起进
        # exe。读不到就当没有（老机器缺文件也不能让界面起不来）。
        self.logo_image = load_logo_image(32)

        if self.logo_image is not None:
            self.logo_label = tk.Label(left, image=self.logo_image, bg=BG)
            self.logo_label.pack(side="left", padx=(0, 8))

        # ---- 软件名 + 版本 ----
        #
        # 软件名【不翻译】：中文/英文/日文界面下都是 CraftLink。
        # 版本号跟着标题栏走，两边都是 cfgmod.version_line() 里的同一份数据。
        tk.Label(
            left,
            text=cfgmod.APP_NAME,
            bg=BG,
            fg=FG,
            font=FONT_TITLE,
        ).pack(side="left")

        tk.Label(
            left,
            text=f"  v{cfgmod.APP_VERSION}",
            bg=BG,
            fg=FG_MUTED,
            font=FONT_SMALL,
        ).pack(side="left", pady=(8, 0))

        # ---- 当前房间（醒目显示）----
        #
        # 用户要的：一进房间就能看到自己在哪个房，而不是去卡片里找那行小字。
        self.header_room_label = tk.Label(
            left,
            text="",
            bg=CARD_ALT,
            fg=FG_DIM,
            font=FONT_BOLD,
            padx=10,
            pady=3,
        )
        self.header_room_label.pack(side="left", padx=(14, 0), pady=(6, 0))

        self.server_status_label = tk.Label(
            header,
            text="● 未测试服务器",
            bg=BG,
            fg=WARN,
            font=FONT_BOLD,
        )
        self.server_status_label.pack(side="right", pady=(6, 0))

        # Role chip, shown right after login and coloured by role:
        # root = orange, admin = green, player = blue.
        # 未登录时显示灰色的「未登录」，而不是留一块空白。
        self.role_badge = tk.Label(
            header,
            text=" 未登录 ",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            padx=6,
            pady=2,
        )
        self.role_badge.pack(side="right", padx=(0, 12), pady=(6, 0))

        # ---- 外观：语言 + 主题 ----
        #
        # 只改观感：语言决定文字用哪种翻译，主题决定配色。两者都不参与任何
        # 逻辑判断（身份、房间、协议都看不懂这些东西），切换时是"重建界面 +
        # 保留全部状态"，功能一个都不少。
        look = tk.Frame(header, bg=BG)
        look.pack(side="right", padx=(0, 14))

        tk.Label(
            look, text=tr("语言"), bg=BG, fg=FG_MUTED, font=FONT_SMALL
        ).pack(side="left")

        self.language_var = tk.StringVar(value=i18n.language_label())

        language_box = ttk.Combobox(
            look,
            textvariable=self.language_var,
            values=[label for _code, label in i18n.LANGUAGES],
            state="readonly",
            width=7,
            font=FONT_SMALL,
        )
        language_box.pack(side="left", padx=(4, 10))
        language_box.bind("<<ComboboxSelected>>", self.on_language_change)
        # 滚轮不许改 readonly 的选项（只允许点下拉菜单），但页面照旧能滚。
        _block_wheel_select(language_box, self._wheel_scroll_page)

        tk.Label(
            look, text=tr("主题"), bg=BG, fg=FG_MUTED, font=FONT_SMALL
        ).pack(side="left")

        self.theme_var = tk.StringVar(value=self._theme_label())

        theme_box = ttk.Combobox(
            look,
            textvariable=self.theme_var,
            values=[tr(label) for _code, label in theme_mod.THEME_LABELS],
            state="readonly",
            width=9,
            font=FONT_SMALL,
        )
        theme_box.pack(side="left", padx=(4, 0))
        theme_box.bind("<<ComboboxSelected>>", self.on_theme_change)
        # 滚轮不许改 readonly 的选项（只允许点下拉菜单），但页面照旧能滚。
        _block_wheel_select(theme_box, self._wheel_scroll_page)

    def _theme_label(self):
        """当前主题在界面上的名字（跟随语言翻译）。"""
        current = theme_mod.current()

        for code, label in theme_mod.THEME_LABELS:
            if code == current:
                return tr(label)

        return tr("暗色")

    # --------------------------------------------------------
    # Left column
    # --------------------------------------------------------

    def _build_tile_selector(self, parent, options, var, command, columns=1,
                             wrap=200):
        """
        A row of large clickable tiles, used for mode and role selection.

        Replaces ttk.Radiobutton: the little radio dot was small, hard to hit
        and looked dated. Each tile is a real frame with a label and a hint,
        and the selected one is filled and outlined so the current choice is
        obvious at a glance.

        `options` is a list of (value, label, hint, locked).

        ============================================================================
          `wrap` 是"这块瓷砖最多能有多宽"的旋钮
        ============================================================================

        The hint label's `wraplength` decides how wide a tile *asks* to be
        (a wrapped label requests min(text, wraplength)). The mode tiles sit
        alone in the wide LEFT column, so they can ask for 200px; the two role
        tiles share the narrow RIGHT column (~400px at the default window), so
        they must ask for much less -- otherwise the whole right column gets
        clipped at 1040px, which is exactly the bug the user reported.

        同一排瓷砖在 grid 里是 uniform 的（等宽），所以这个值取"最宽的那块
        需要多少"，整排就是两倍。
        """
        container = tk.Frame(parent, bg=CARD)
        holder = {}

        for index, (value, label, hint, locked) in enumerate(options):
            column = index % columns
            row_index = index // columns

            # 圆角瓷砖：和卡片同一套画法（Canvas 画圆角垫在底下）。
            # 选中/未选中只是把填充色和描边色换掉，由 set_selected() 一起处理。
            tile = RoundTile(container, locked=locked)

            tile.grid(
                row=row_index,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 6, 0),
                pady=(0 if row_index == 0 else 6, 0),
            )

            title = tk.Label(
                tile.body,
                text=label,
                bg=TILE_OFF,
                fg=FG if not locked else LOCKED,
                font=FONT_BOLD,
                anchor="w",
                cursor="hand2" if not locked else "arrow",
            )
            title.pack(fill="x", padx=12, pady=(9, 0))

            detail = tk.Label(
                tile.body,
                text=hint,
                bg=TILE_OFF,
                fg=FG_MUTED,
                font=FONT_SMALL,
                anchor="w",
                justify="left",
                wraplength=wrap,
                cursor="hand2" if not locked else "arrow",
            )
            detail.pack(fill="x", padx=12, pady=(2, 10))

            widgets = (tile, title, detail)

            if locked:
                lock = tk.Label(
                    tile.body,
                    text="🔒 未开放",
                    bg=TILE_OFF,
                    fg=LOCKED,
                    font=FONT_TINY,
                )
                lock.place(relx=1.0, x=-10, y=8, anchor="ne")
                widgets = widgets + (lock,)

            if not locked:
                for widget in widgets:
                    widget.bind(
                        "<Button-1>",
                        lambda _e, v=value: self._on_tile_click(v, var, command),
                    )

            holder[value] = widgets

        for column in range(columns):
            container.grid_columnconfigure(column, weight=1, uniform="tile")

        self._tile_groups = getattr(self, "_tile_groups", [])
        self._tile_groups.append((holder, var, options))

        return container

    def _on_tile_click(self, value, var, command):
        if var.get() == value:
            return

        var.set(value)
        self._refresh_tiles()

        if command is not None:
            command()

    def _refresh_tiles(self):
        """Repaint every tile group so the selected one is unmistakable."""
        for holder, var, options in getattr(self, "_tile_groups", []):
            selected = var.get()

            for value, _label, _hint, locked in options:
                widgets = holder.get(value) or ()

                if locked:
                    # Locked tiles never look selected, even if something set
                    # the variable programmatically.
                    chosen = False
                else:
                    chosen = value == selected

                bg = TILE_ON if chosen else TILE_OFF
                border = ACCENT if chosen else BORDER

                for widget in widgets:
                    # 圆角瓷砖自己会重画（画布上的填充和描边），这里只通知它。
                    # 外面套 suppress 是因为重建界面时旧瓷砖已经被销毁 ——
                    # 死控件的 TclError 不该把刷新打断（下面 set_selected 里
                    # 也会碰到同样的事）。
                    if isinstance(widget, RoundTile):
                        with contextlib.suppress(tk.TclError, AttributeError):
                            widget.set_selected(chosen, bg=bg, border=border)

                        continue

                    with contextlib.suppress(tk.TclError):
                        widget.configure(bg=bg)

    def _build_mode_card(self, parent):
        card = Card(parent, title="连接模式")
        card.pack(fill="x", pady=(0, 10))

        self.mode_var = tk.StringVar(value=MODE_RELAY)

        # ============================================================================
        #  ★ 用户 2026-09-30 第 3 条：非房主**也要看得见当前模式**，只是点不动
        # ============================================================================
        #
        # 旧行为：进别人房间时把 Relay/STUN 两块**整体锁死**（🔒 未开放），而且
        # 这个锁是**建卡片那一刻算一次**的 —— 于是：
        #   * 房间当前用的是哪种模式，选择器上完全看不出来（两块都"未开放"）；
        #   * 退出别人的房间、自己建房之后锁**不重算**，房主自己也选不了模式。
        # 用户原话：「同步当前房主模式选择器给我展示出来呀，只是我换模式点击
        # 不允许那么操作而已，不要全部未开放」。
        #
        # 所以：Relay/STUN/AUTO 三块**永远可点**（显示跟随房间模式，见
        # `_refresh_room_ui` 的三处设置），点一个不是当前模式的 -> `_on_mode_change`
        # 里按身份拒绝并说清"由房间所有者决定"。权限判定**一个字没放宽**
        # （服务端仍然是最终裁判）。AUTO 在 V5.0 解锁，它只是多了一条"直连建不
        # 起来就用中继"的本地回落，房间模式仍由 Owner 独占决定。
        locked_by_host = False

        options = [
            (
                MODE_RELAY,
                "Relay 中转",
                "流量经中转服务器转发，兼容性最好，推荐",
                locked_by_host,
            ),
            (
                MODE_STUN,
                "STUN 直连",
                # v3：STUN 解锁。语义照抄 MCLanP2P 的 MODE_DIRECT：
                # 「只走 UDP 打洞，每 3 秒重试直到连上，永不走中转」。
                # "打不通自动换 relay" 是 AUTO 的语义，见下面那一块（V5.0）。
                "只走 UDP 打洞，一直重试直到连上；永不自动改用中转",
                locked_by_host,
            ),
            (
                MODE_AUTO,
                "AUTO 自动",
                # ★ V5.0：解锁。语义 = "优先直连，直连建不起来时自动改用中转"。
                # 回落是**客户端本地**的事，房间模式始终是 auto（绝不悄悄改回
                # relay）。见 docs/AUTO-SCHEDULING-PLAN.md。
                "优先直连；直连建不起来时自动改用中转",
                locked_by_host,
            ),
        ]

        self._build_tile_selector(
            card.body, options, self.mode_var, self._on_mode_change
        ).pack(fill="x")

        self.mode_hint = tk.Label(
            card.body,
            text="",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            # 左列（≈560px）里最长的一行提示：wrap 必须 ≤ 列的可用宽度，
            # 否则它会反过来决定这一列要多宽。下同（见 _reflow_columns）。
            wraplength=460,
            justify="left",
        )
        self.mode_hint.pack(fill="x", pady=(8, 0))

        # ---- 谁来决定连接模式（房主）----
        #
        # A room shares ONE transport: everyone in the same Minecraft world has
        # to use the same path, so this is a room property, not a local
        # preference. The relay enforces it; this label tells the user why the
        # selector is greyed out.
        self.mode_owner_label = tk.Label(
            card.body,
            text="",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            wraplength=460,
            justify="left",
        )
        self.mode_owner_label.pack(fill="x", pady=(6, 0))

        self._refresh_tiles()

    def _build_account_card(self, parent):
        card = Card(parent, title="账号")
        card.pack(fill="x", pady=(0, 10))

        self.account_label = tk.Label(
            card.body,
            text="未登录",
            bg=CARD,
            fg=WARN,
            font=FONT_BOLD,
            anchor="w",
        )
        self.account_label.pack(fill="x")

        self.account_hint = tk.Label(
            card.body,
            text="当前服务器要求登录后才能建房 / 进房",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            wraplength=330,
            justify="left",
        )
        self.account_hint.pack(fill="x", pady=(4, 8))

        row = tk.Frame(card.body, bg=CARD)
        row.pack(fill="x")

        self.login_button = self._button(
            row, "登录", self.on_login, width=8, primary=True,
        
    icon="🔑",
        )
        self.login_button.pack(side="left")

        self.logout_button = self._button(
            row, "退出登录", self.on_logout, width=9, secondary=True,
        
    icon="🚪",
        )
        self.logout_button.pack(side="left", padx=(6, 0))
        self.logout_button.configure(state="disabled")

        # NOTE: there used to be a second "房间列表" button here. It opened the
        # same dialog as the one on the action card, and two entries to one
        # feature is confusing, so this one is deliberately never packed. The
        # widget still exists (and is still enabled/disabled with the rest of
        # the account controls) because the account button groups reference
        # it; it just has no place on screen.
        self.rooms_button = self._button(
            row, "房间列表", self.on_rooms, width=10, secondary=True
        )
        self.rooms_button.configure(state="disabled")

        # Renaming is deliberately NOT blocked while a session is running: the
        # relay applies it without dropping the connection.
        action_row = tk.Frame(card.body, bg=CARD)
        action_row.pack(fill="x", pady=(6, 0))

        self.rename_button = self._button(
            action_row, "修改用户名", self.on_rename, width=10, secondary=True,
        
    icon="✎",
        )
        self.rename_button.pack(side="left")
        self.rename_button.configure(state="disabled")

    def _init_identity_vars(self):
        """
        Hidden identity state.

        There used to be a visible "身份" card holding a username box and a
        read-only client id. It was removed because:

          * the username box did NOT rename the account -- it only changed the
            display name inside a room, which is confusing sitting next to the
            real "修改用户名" action in the account card, and the user judged it
            useless;
          * the client id is machine generated and never needs editing.

        Both values are still read in several places, so they remain as plain
        variables that no widget is bound to.
        """
        self.username_var = tk.StringVar(value=self.config.username or "")
        self.client_id_var = tk.StringVar(value=self.config.client_id)

    def _build_server_card(self, parent):
        card = Card(parent, title="中转服务器")
        card.pack(fill="x")

        row = tk.Frame(card.body, bg=CARD)
        row.pack(fill="x")

        # ★ D10：地址与端口**没有内置默认值**（默认空）。空着的时候这张卡
        # 必须自己说清楚"下一步填什么"，而不是等用户点了连接再弹一个错误。
        self.host_var = tk.StringVar(value=self.config.server_host or "")
        self.host_entry = ttk.Entry(
            row, textvariable=self.host_var, style=ENTRY_STYLE
        )
        self.host_entry.pack(side="left", fill="x", expand=True)

        # 端口没设置时显示空串，而不是 "0" —— "0" 看起来像一个能用的端口。
        self.port_var = tk.StringVar(
            value=port_text(self.config.server_port)
        )
        self.port_entry = ttk.Entry(
            row, textvariable=self.port_var, width=8, style=ENTRY_STYLE
        )
        self.port_entry.pack(side="left", padx=(10, 0))

        # 改动立即反映到提示上（"允许随时修改并立即生效"）。
        for widget in (self.host_entry, self.port_entry):
            widget.bind("<KeyRelease>", self._on_server_field_edited, add="+")
            widget.bind("<<Paste>>", self._on_server_field_edited, add="+")

        #: 常驻提示：没填地址时是引导，填了之后是"随时可改"。
        self.server_hint_label = tk.Label(
            card.body,
            text="",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.server_hint_label.pack(fill="x", pady=(8, 0))

        buttons = tk.Frame(card.body, bg=CARD)
        buttons.pack(fill="x", pady=(10, 0))

        self.test_button = self._button(
            buttons,
            "测试服务器连接",
            self.on_test_server,
            width=18,
        
    icon="⚡",
        )
        self.test_button.pack(side="left")

        self._button(
            buttons,
            "恢复默认",
            self.on_reset_server,
            width=10,
            secondary=True,
        
    icon="↺",
        ).pack(side="left", padx=(8, 0))

        self._refresh_server_hint()

    def _on_server_field_edited(self, _event=None):
        self._refresh_server_hint()

    def _refresh_server_hint(self):
        """
        服务器卡片下面那行常驻提示（**状态驱动**，不靠一次性弹窗）。

        没填地址/端口 -> 引导（这是首次使用看到的唯一指引）；
        填好了        -> 告诉用户随时可改、改完立即生效。
        """
        label = getattr(self, "server_hint_label", None)

        if label is None:
            return

        host = ""
        port = 0

        with contextlib.suppress(Exception):
            host = self.host_var.get().strip()

        with contextlib.suppress(Exception):
            port = self._port_or_default()

        # 品牌配置给了默认地址时，说清楚它是"默认值"而不是"内置服务器"。
        branding_host = cfgmod.default_server_host()
        branding_port = cfgmod.default_server_port()

        if not host:
            text = tr(cfgmod.SERVER_FIELD_HINT)
            colour = WARN
        elif not port:
            text = tr(cfgmod.SERVER_PORT_GUIDE.split("\n\n")[0])
            colour = WARN
        else:
            text = tr(cfgmod.SERVER_FIELD_HINT_SET)

            if branding_host and branding_port:
                text = (
                    f"{text} {tr('（默认地址来自品牌配置）')} "
                    f"{branding_host}:{branding_port}"
                )

            colour = FG_MUTED

        with contextlib.suppress(Exception):
            label.configure(text=text, fg=colour)

    # --------------------------------------------------------
    # 数据目录（M6：日志 / 配置 / 记忆 / 缓存的落脚点）
    # --------------------------------------------------------

    def _build_data_card(self, parent):
        """
        「数据目录」卡片。

        为什么要有这张卡：M6 之前运行期文件的位置是"看运气"的（EXE 旁边 /
        当前目录 / %APPDATA%\\MC-Link 各写各的）。现在有一个**唯一**的用户
        数据目录，用户必须能看见它在哪、能改、能打开 —— 看不见的目录等于
        找不回来的日志。
        """
        card = Card(parent, title="数据目录")
        card.pack(fill="x", pady=(10, 0))

        tk.Label(
            card.body,
            text="配置、日志、记住的账号都存在这里（不写程序安装目录）。",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=340,
        ).pack(fill="x", pady=(0, 6))

        self.data_dir_var = tk.StringVar(value=cfgmod.config_dir())

        tk.Label(
            card.body,
            textvariable=self.data_dir_var,
            bg=CARD,
            fg=FG,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=340,
        ).pack(fill="x")

        info = cfgmod.config_dir_description()

        detail = "默认位置" if info.get("is_default") else "自定义位置"

        if info.get("override_env"):
            detail = "由环境变量 MCLINK_DATA_DIR 指定"

        self.data_dir_note = tk.Label(
            card.body,
            text=detail,
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
        )
        self.data_dir_note.pack(fill="x", pady=(4, 8))

        buttons = tk.Frame(card.body, bg=CARD)
        buttons.pack(fill="x")

        self._button(
            buttons,
            "更改…",
            self.on_change_data_dir,
            width=8,
            secondary=True,
            icon="📁",
        ).pack(side="left")

        self._button(
            buttons,
            "打开",
            self.on_open_data_dir,
            width=7,
            secondary=True,
        ).pack(side="left", padx=(6, 0))

        self._button(
            buttons,
            "恢复默认",
            self.on_reset_data_dir,
            width=10,
            secondary=True,
            icon="↺",
        ).pack(side="left", padx=(6, 0))

    def on_change_data_dir(self):
        """
        改数据目录：选目录 -> 写指针 -> 把旧目录的数据迁过去。

        迁移（复制配置/日志/缓存 + 搬走凭据）在这里立刻做，因为用户按下
        "更改"的那一刻，他心里想的就是"我的东西跟着走"。日志的落盘位置
        当场重绑（`logmod.reinit`），所以旧日志不会继续写到旧目录。
        """
        current = cfgmod.config_dir()

        chosen = filedialog.askdirectory(
            title=tr("选择数据目录"),
            initialdir=current if os.path.isdir(current) else None,
            mustexist=False,
        )

        if not chosen:
            return

        chosen = os.path.abspath(chosen)

        if os.path.abspath(chosen) == os.path.abspath(current):
            return

        ok, message = cfgmod.set_config_dir(chosen)

        if not ok:
            messagebox.showerror("数据目录", message, parent=self.root)
            return

        report = userdirs_mod.migrate_user_data(current, chosen)

        # 日志：立刻改到新目录（不重绑的话，运行期还会往旧目录写）。
        with contextlib.suppress(Exception):
            logmod.reinit()

        self.data_dir_var.set(chosen)
        self._refresh_data_dir_note()

        copied = len(report.get("copied") or [])
        moved = len(report.get("secrets_moved") or [])

        self.log(
            f"data directory changed to {chosen} "
            f"(copied {copied} file(s), moved {moved} credential file(s))",
            kind="info",
        )

        messagebox.showinfo(
            "数据目录已更改",
            "新的数据目录：\n"
            f"{chosen}\n\n"
            f"已迁移 {copied} 个文件"
            + ("（含记住的账号密码）" if moved else "")
            + "。\n"
            "日志已立刻写到新目录；配置与登录记忆也会从这里读写。\n\n"
            f"旧目录（{current}）里的文件**保留**着，确认没问题后可以自己删掉。",
            parent=self.root,
        )

        self._refresh_server_hint()

    def on_reset_data_dir(self):
        """恢复平台默认数据目录（同样迁移旧文件）。"""
        current = cfgmod.config_dir()
        target = cfgmod.reset_config_dir()

        if os.path.abspath(target) == os.path.abspath(current):
            self.log("data directory is already the platform default", kind="dim")
            return

        report = userdirs_mod.migrate_user_data(current, target)

        with contextlib.suppress(Exception):
            logmod.reinit()

        self.data_dir_var.set(target)
        self._refresh_data_dir_note()

        self.log(
            f"data directory restored to the platform default {target} "
            f"(copied {len(report.get('copied') or [])} file(s))",
            kind="info",
        )

        messagebox.showinfo(
            "已恢复默认数据目录",
            f"{target}\n\n"
            "旧目录里的文件保留着，确认没问题后可以自己删掉。",
            parent=self.root,
        )

    def _refresh_data_dir_note(self):
        info = cfgmod.config_dir_description()

        text = "默认位置" if info.get("is_default") else "自定义位置"

        if info.get("override_env"):
            text = "由环境变量 MCLINK_DATA_DIR 指定"

        with contextlib.suppress(Exception):
            self.data_dir_note.configure(text=tr(text))

    def _show_branding_warnings(self):
        """把 branding.json 的解析警告写进日志（不弹窗：不影响使用）。"""
        for warning in branding_mod.warnings():
            self.log(f"branding: {warning}", kind="warn")

    def _maybe_show_first_run_guide(self):
        """
        首次运行引导（M6 验收点之一）。

        触发条件（任一）：
          * 没有任何配置文件（first_run）；
          * 地址/端口是空的（包含"旧配置里的内置地址被清掉"这一种）。

        用**非模态**窗口而不是 messagebox：首次启动时用户还想同时填上面的
        地址框，模态框会把人挡住。窗口里说清三件事：填什么、填完会怎样、
        文件存在哪。
        """
        if getattr(self, "_first_run_guide_shown", False):
            return

        needs = False

        with contextlib.suppress(Exception):
            needs = not self.host_var.get().strip() or not self._port_or_default()

        if not getattr(self.config, "first_run", False) and not needs:
            return

        self._first_run_guide_shown = True

        win = tk.Toplevel(self.root)
        win.title(tr("首次使用：连接设置"))
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=16)

        tk.Label(
            body,
            text=tr(f"欢迎使用 {cfgmod.APP_NAME}"),
            bg=BG,
            fg=FG,
            font=FONT_TITLE,
            anchor="w",
        ).pack(fill="x")

        cleared = getattr(self.config, "legacy_address_cleared", False)

        # 每一段单独 tr()：整段拼完再翻译的话，句式表里就得放一条带换行的
        # 长正则（而且是三个句子粘在一起），基本查不到 —— 那样英文界面上
        # 会突然冒出一段中文。分段之后每段都是一个静态键。
        lines = []

        if cleared:
            lines.append(tr(FIRST_RUN_LEGACY_CLEARED))

        lines.append(tr(FIRST_RUN_BODY))

        lines.append(
            tr(FIRST_RUN_DATA_DIR_LEAD)
            + "\n"
            + cfgmod.config_dir()
            + "\n"
            + tr(FIRST_RUN_DATA_DIR_TAIL)
        )

        tk.Label(
            body,
            text="\n\n".join(lines),
            bg=BG,
            fg=FG_MUTED,
            font=FONT_SMALL,
            justify="left",
            anchor="w",
            wraplength=460,
        ).pack(fill="x", pady=(10, 0))

        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(14, 0))

        def close():
            # 引导看过就不再当"第一次"了：下次启动只按"地址是否为空"决定
            # 要不要再提示（地址还空着就还会提示 —— 不能静默地不告诉用户）。
            self.config.first_run = False
            self.config.legacy_address_cleared = False

            with contextlib.suppress(Exception):
                cfgmod.save_config(self.config)

            with contextlib.suppress(Exception):
                win.destroy()

        self._button(row, "知道了", close, width=10).pack(side="left")

        def focus_host():
            close()

            with contextlib.suppress(Exception):
                self.host_entry.focus_set()

        self._button(
            row,
            "去填写地址",
            focus_host,
            width=12,
            secondary=True,
        ).pack(side="left", padx=(8, 0))

        with contextlib.suppress(Exception):
            win.update_idletasks()
            x = self.root.winfo_rootx() + 60
            y = self.root.winfo_rooty() + 80
            win.geometry(f"+{int(x)}+{int(y)}")

        self.log("first-run guide shown (no relay address configured)", kind="dim")

    def _show_address_help(self):
        """地址为空时按下"连接/测试"给的引导（不只是一句报错）。"""
        messagebox.showinfo(
            "先填写中转服务器",
            cfgmod.SERVER_GUIDE,
            parent=self.root,
        )

        with contextlib.suppress(Exception):
            self.host_entry.focus_set()

    # --------------------------------------------------------
    # Right column
    # --------------------------------------------------------

    def _build_role_card(self, parent):
        """
        基准配置：本机连接用的三个数字（房主局域网端口 / 玩家本地端口 / 人数上限）。

        ============================================================================
          为什么这里【没有】主机 Host / 玩家 Player 选择器（用户要求，2026-09-15）
        ============================================================================

        用户原话：「把整个身份配置那个东西做成基准配置，把主机 host 和玩家 player
        这个选择器直接移除掉，免得它去影响到操作那一栏。」

        那个选择器从来就不该存在，它有四个真问题：

          1. **它和真实身份无关**。真实身份由「谁持有主机位置」决定
             （见 _decide_connect_role）。选择器却让人以为"选了主机就一定是主机"。
          2. **它影响开服**。切换它会把其中一个端口输入框藏起来，而"哪个端口"
             恰恰取决于真实身份 —— 于是选择器间接影响了能不能连上。
          3. **默认值是 host，第一次点没反应**。要来回点两次才有反应（用户报的）。
          4. 它下面那两行「主机模式：…」「玩家模式：…」也是歧义文案。

        所以现在：**三个数字全部平铺出来、随时可改**，不再有任何"身份"语义。
        """

        card = Card(parent, title="基准配置")
        card.pack(fill="x", pady=(0, 10))

        tk.Label(
            card.body,
            text="本机连接要用的数字。改完必须按右边的「确定」才生效。",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=340,
        ).pack(fill="x", pady=(0, 10))

        #: 三个数字的"已提交"值 —— 连接时读的就是它，不是输入框里的字。
        #:
        #: 为什么要有这一层：用户要求「输入完新的之后不会立即生效，按右边的提交」。
        #: 输入框里的字随时在变，只有按了确定这个字典才会更新。
        #:
        #: 只在第一次建这张卡时初始化；切语言/主题会重建整张卡，
        #: 那时**必须保住**已提交的值（否则界面会从"已生效"退回"未提交"，
        #: 而且用户已经提交过的端口会被当成没提交）。
        if not hasattr(self, "_committed"):
            self._committed = {}

        # ---- 1. 房主的 Minecraft 局域网端口 ----
        self.mc_port_var = tk.StringVar(value="")
        self._build_config_row(
            card.body,
            label="主机 Host：Minecraft 局域网端口",
            hint="游戏里「对局域网开放」时显示的那个端口。你开世界时用。",
            variable=self.mc_port_var,
            commit=lambda: self._commit_port("mc_port", self.mc_port_var),
            key="mc_port",
        )

        # ---- 2. 玩家的本地代理端口 ----
        self.local_port_var = tk.StringVar(value="")
        self._build_config_row(
            card.body,
            label="玩家 Player：本地代理端口",
            hint="你作为玩家时，在 Minecraft 里连接 127.0.0.1:该端口。",
            variable=self.local_port_var,
            commit=lambda: self._commit_port(
                "local_port", self.local_port_var
            ),
            key="local_port",
        )

        # ---- 3. 人数上限 ----
        self.max_players_var = tk.StringVar(value="")
        self._build_config_row(
            card.body,
            label="人数上限",
            hint=(
                f"单个房间最多几个人（含房主），"
                f"{proto.MIN_MAX_PLAYERS}-{proto.DEFAULT_MAX_PLAYERS} 人。"
                "只在你【建房】时生效。"
            ),
            variable=self.max_players_var,
            commit=self._commit_max_players,
            key="max_players",
        )

        # ---- 这一条是"改了要付出代价"的说明，必须显眼 ----
        tk.Label(
            card.body,
            text=(
                "⚠ 房间一旦建好，上面三个数字就锁定了 —— 隧道在建房那一刻"
                "就建好了。想改必须先删掉房间，改完再重新建房。"
            ),
            bg=CARD,
            fg=WARN,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=340,
        ).pack(fill="x", pady=(10, 0))

        # 建卡时就把输入框填成上次用的值（第一次打开是全空）。
        self._load_config_fields()

    def _build_config_row(self, parent, label, hint, variable, commit, key=""):
        """
        一行配置：标签 + 输入框 + 右边「确定」+ 下面一行**即时反馈**。

        ============================================================================
          为什么按钮旁边必须有自己的反馈（用户提的）
        ============================================================================

        用户原话：「点击确定改动端口啊那些配置能否加个反馈啊，是应该成功还是失败
        对吧」。

        第一版只在成功时写 `self.log(...)` —— 那是**窗口底部的日志面板**，而且
        要等界面 tick（约 1 秒）才出现。用户站在「基准配置」卡片上按确定，
        眼睛看不到那行字，只看到"按钮按了、什么都没发生"。

        修法：每一行自带一个状态标签，按确定的**那一瞬间**就变：
            已生效：25565      （绿色，说明这次提交成功了）
            未提交：改了请按「确定」（橙色，说明你改了但还没提交）
            尚未设置           （灰色，空的）
        失败仍然弹窗（见 _commit_port），而且已生效的值不会被污染。
        """
        tk.Label(
            parent,
            text=label,
            bg=CARD,
            fg=FG_DIM,
            font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x", pady=(8, 0))

        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(4, 0))

        entry = ttk.Entry(row, textvariable=variable, style=ENTRY_STYLE)
        entry.pack(side="left", fill="x", expand=True)

        # 名字必须挂到 self 上：_verify_form_bindings() 会检查
        # 「输入框绑的变量 == 程序读的变量」，找不到控件它就不查了 ——
        # 那等于把一道防"读错变量"的闸门关掉（这个项目因为这个踩过大坑）。
        if key == "mc_port":
            self.mc_port_entry = entry
        elif key == "local_port":
            self.local_port_entry = entry
        elif key == "max_players":
            self.max_players_entry = entry

        button = self._button(
            row, "确定", commit, width=6, primary=True
        )
        button.pack(side="left", padx=(6, 0))

        # 回车 = 按确定。用户原话就是"改完回车之后"发现问题，
        # 所以回车必须走同一条提交路径，不能是"改了就算数"。
        entry.bind("<Return>", lambda _event: commit())

        # ---- 这一行的即时反馈（静态提示就写在这里）----
        status = tk.Label(
            parent,
            text=hint or "",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_TINY,
            anchor="w",
            justify="left",
            wraplength=340,
        )
        status.pack(fill="x", pady=(2, 0))

        variable.trace_add("write", lambda *_a: self._on_config_edited())

        self._config_rows = getattr(self, "_config_rows", [])
        self._config_rows.append((variable, entry, button, status, hint or ""))

        return entry

    def _set_row_status(self, key, template, kind="ok", value=None):
        """
        把某一行的反馈文字**立刻**显示出来。

        反馈文字要走 tr()：三语言是这个软件已经验收的功能，动态生成的提示
        也不能例外（i18n 测试会检查整个界面不残留中文）。
        模板用 %s 占位，翻成什么语言都能正确代入数字。

        用 update_idletasks()：不刷的话，用户点完确定要等下一次事件循环才看到，
        视觉上仍然是"按了没反应"。
        """
        text = tr(template)

        if value is not None:
            with contextlib.suppress(Exception):
                text = text % value

        for variable, _entry, _button, status, _hint in getattr(
            self, "_config_rows", []
        ):
            if self._config_key_of(variable) != key:
                continue

            color = {"ok": OK, "warn": WARN, "error": BAD}.get(kind, FG_MUTED)

            with contextlib.suppress(Exception):
                status.configure(text=text, fg=color)
                status.update_idletasks()

            return

    def _restore_row_hint(self, key):
        """把某一行恢复成它的静态说明文字。"""
        for variable, _entry, _button, status, hint in getattr(
            self, "_config_rows", []
        ):
            if self._config_key_of(variable) != key:
                continue

            with contextlib.suppress(Exception):
                status.configure(text=hint, fg=FG_MUTED)

            return

    # --------------------------------------------------------
    # 基准配置：提交 / 装载 / 锁定
    # --------------------------------------------------------

    #: 建房后锁死的两个键（房间的命根子）。
    #:
    #: 用户定的原则（原话）：
    #     「房间强绑定房主的参数，人数和 host 代理端口全部按房主的来……
    #       房间人数和 host 代理端口，建房后锁定，就是房主本人和服务器
    #       管理员都不允许修改，这东西一改联机全掉了。」
    #:
    #: 为什么会全掉：**隧道是建房那一刻按这两个数字建好的**（人数决定建几条，
    #: host 端口决定每条隧道去连哪里）。改了它们，中继那边原来那批隧道仍然
    #: 活着（没人回收），新的一批又建出来 —— 两批同时存在，池子里的数字翻倍，
    #: 玩家会被配到"旧参数那批"上，连不上（用户实测的连接丢失）。
    #:
    #: 而且这条**与房主是谁无关**：换一个配置不同的人来当 host，房间也必须
    #: 按建房时定下的来。所以锁定看的是"房间建了没有"，不是"你是谁"。
    LOCKED_AFTER_CREATE = ("mc_port", "max_players")

    def _lock_key(self, key):
        """这个键在建房后是否被锁死？"""
        return key in self.LOCKED_AFTER_CREATE

    def _config_locked(self, key=None):
        """
        现在能不能改这个数字？

        - 房间已建（或已在房间里 / 连接在跑）→ mc_port 和 max_players 锁死。
        - local_port **永不锁定**：它和隧道没有任何关系（隧道数量只由人数推导），
          它只是本机监听端口，房主根本不用它。所以玩家随时可以改 ——
          但改了要重连（那是玩家自己的事，不影响房间，也不影响别人）。
        """
        if key is not None and not self._lock_key(key):
            return False

        with contextlib.suppress(Exception):
            if self.running:
                return True

        with contextlib.suppress(Exception):
            if self.engine.room_state() is not None:
                # 房间被服务端关掉（ROOM REVOKED）之后它已经不是"在建房时锁定
                # 参数"的那个房间了 —— 隧道也没了，参数必须能改，否则用户
                # 面对的是"房间没了、端口也动不了"（ISSUES 问题 4 的另一半）。
                if not getattr(
                    self.engine.room_state(), "revoked", False
                ):
                    return True

        return False

    def _config_lock_reason(self):
        with contextlib.suppress(Exception):
            if self.running:
                return "连接正在运行"

        return "你已经在这个房间里了"

    def _is_config_dirty(self, variable):
        """这个输入框里的字和"已提交"的值不一样吗？"""
        key = self._config_key_of(variable)

        if not key:
            return False

        return self._committed.get(key) != str(variable.get()).strip()

    def _on_config_edited(self):
        """
        输入框被改动 → 按钮变成"待提交"的样子。

        不做任何校验、不报错：打字过程中每敲一个字符都会走到这里，
        在这里弹窗是最烦人的做法。校验只在按「确定」时做。
        """
        self._sync_config_buttons()

    def _config_key_of(self, variable):
        for key, own in (
            ("mc_port", getattr(self, "mc_port_var", None)),
            ("local_port", getattr(self, "local_port_var", None)),
            ("max_players", getattr(self, "max_players_var", None)),
        ):
            if own is variable:
                return key

        return ""

    def _load_config_fields(self):
        """
        把上次用的值填进输入框。

        用户要求：**第一次打开是空的**（玩家自己填一次），之后记忆上一次的。

        "第一次"由 `config.first_run` 决定（没有任何配置文件 = 第一次跑）。
        注意：装载进来的值**同时**算"已提交" —— 上次能用，这次不该再让人
        按一遍确定；否则每次开软件都要重新提交三个数字，那是折磨。
        """
        if not getattr(self, "_config_loaded_from_disk", False):
            # ============================================================================
            #  第一次装载（程序刚起来）：按 first_run 决定要不要预填
            # ============================================================================
            #
            # 用户要求：第一次打开三个数字是**空的**（玩家自己填一次），
            # 之后每次打开记忆上一次提交的值。
            self._config_loaded_from_disk = True

            self._committed = {}

            if getattr(self.config, "first_run", False):
                for key, variable in (
                    ("mc_port", getattr(self, "mc_port_var", None)),
                    ("local_port", getattr(self, "local_port_var", None)),
                    ("max_players", getattr(self, "max_players_var", None)),
                ):
                    if variable is not None:
                        variable.set("")

                self._on_config_edited()
                return

            values = {
                "mc_port": getattr(self.config, "mc_port", None),
                "local_port": getattr(self.config, "local_port", None),
                "max_players": getattr(self.config, "max_players", None)
                or proto.DEFAULT_MAX_PLAYERS,
            }

            for key, variable in (
                ("mc_port", getattr(self, "mc_port_var", None)),
                ("local_port", getattr(self, "local_port_var", None)),
                ("max_players", getattr(self, "max_players_var", None)),
            ):
                if variable is not None and values.get(key) is not None:
                    variable.set(str(values[key]))

            # 装载进来的值就是"已提交"的值：上次能用，这次不用再按一次。
            self._committed = {
                "mc_port": str(values["mc_port"] or "").strip(),
                "local_port": str(values["local_port"] or "").strip(),
                "max_players": str(values["max_players"]).strip(),
            }

            self._on_config_edited()
            return

        # ============================================================================
        #  重建界面（切语言/切主题）：**只按内存里的事实恢复**
        # ============================================================================
        #
        # 这里**不能**再看 config.first_run，也不能从 config 反推"提交过没有" ——
        # 它是一个"这台机器第一次打开"的标记，在同一个 config 对象上永远是 True
        # （它只在 load_config() 里写一次）。第一版就是这样：切一次语言，
        # 三个数字全被清空、_committed 被重置成 {}，界面从「已生效」变成
        # 「未提交」—— 外观测试（切完语言/主题后控件指纹必须一致）直接把它抓红了。
        #
        # 内存里的 _committed 才是真相，重建只是把控件按它重画一遍。
        for key, variable in (
            ("mc_port", getattr(self, "mc_port_var", None)),
            ("local_port", getattr(self, "local_port_var", None)),
            ("max_players", getattr(self, "max_players_var", None)),
        ):
            value = self._committed.get(key, "")

            if variable is not None:
                variable.set(value)

        self._on_config_edited()

    def _commit_port(self, key, variable):
        """
        提交一个端口（按了确定或回车）。

        反馈分三层，用户按下去**立刻**能看到结果：
          1. 按钮旁边那一行文字变成「已生效：25565」（绿色）—— 成功
          2. 失败弹窗说清楚哪里不对，而且**已生效的值保持不变**
          3. 日志面板也留一条（事后可查）
        """
        if self._config_locked(key):
            self._config_locked_notice(key)
            self._mark_row_blocked(key)
            return

        name = (
            "主机 Host 局域网端口"
            if key == "mc_port"
            else "玩家 Player 本地端口"
        )

        raw = str(variable.get()).strip()

        if not raw:
            self._set_row_status(
                key, "✗ 还没填 —— 端口不能为空", kind="error"
            )
            messagebox.showwarning(
                "还没填",
                "这个端口不能为空。请先填一个 1-65535 之间的数字。",
                parent=self.root,
            )
            return

        try:
            value = int(raw)
        except (TypeError, ValueError):
            self._set_row_status(
                key, "✗ 「%s」不是数字", kind="error", value=raw
            )
            messagebox.showwarning(
                "不是数字",
                f"端口必须是数字，你填的是「{raw}」。",
                parent=self.root,
            )
            return

        if not (1 <= value <= 65535):
            self._set_row_status(
                key,
                "✗ %s 超出范围（只能是 1-65535）",
                kind="error",
                value=value,
            )
            messagebox.showwarning(
                "端口超出范围",
                f"端口只能是 1-65535，你填的是 {value}。",
                parent=self.root,
            )
            return

        # ============================================================================
        #  ★ 用户 2026-09-29（第三轮）第 1 条：本地代理端口的**热切换**
        # ============================================================================
        #
        # 规则（用户原话）：
        #   * 本地连接 Player 代理端口是**唯一允许热切换**的参数
        #     （房间人数 / host 代理端口建房即冻结 —— 见 LOCKED_AFTER_CREATE）；
        #   * 玩家**空闲**时改：直接改，什么都不用重建；
        #   * 玩家**连接中**时改：二级弹窗说清楚"换端口会导致游戏掉线、需要
        #     重新进游戏"，确认之后**自动重建这条隧道并自动重连** ——
        #     操作者原来是什么状态，确认后还是什么状态。
        #
        # 旧实现只在日志里说一句"下一次开始连接才会用新端口（要现在生效请先停止
        # 再开始）"：于是界面显示"已生效：新端口"，实际监听还在旧端口上，
        # 必须手动重连才生效 —— 用户看到的"热切换仅状态机显示生效、代理端口还是
        # 原来的"就是这里。
        # 只有**玩家**的会话才真的在监听这个端口（房主用的是 mc_port），
        # 所以只有玩家这一侧需要"确认 + 重建"。房主改了它不动任何东西
        # （重建房主会话会把所有玩家踢下线，那绝对不是用户要的）。
        port_hot_switch = (
            key == "local_port"
            and self.running
            and str(getattr(self.config, "role", "") or "") == "player"
        )

        if port_hot_switch:
            if not messagebox.askokcancel(
                tr("修改本地代理端口"),
                tr(
                    "把本地代理端口改成 {0} 需要重建这一条隧道：\n\n"
                    "· 当前游戏连接会断开，需要在 Minecraft 里重新进入一次；\n"
                    "· 隧道会自动重建、自动重连 —— 你不需要手动点任何东西；\n"
                    "· 改完之后你的连接状态和现在一样。\n\n"
                    "确定要现在修改吗？"
                ).replace("{0}", str(value)),
                parent=self.root,
            ):
                # 取消：界面退回"已生效"的旧值，什么都不动。
                previous = str(self._committed.get("local_port") or "")

                if previous:
                    variable.set(previous)

                self._set_row_status(
                    key, "已取消 —— 仍是 %s", kind="warn", value=previous
                )
                self._sync_config_buttons()
                return

        # 回写规范化后的数字（"025565" → "25565"），避免显示和实际不一致。
        variable.set(str(value))

        self._committed[key] = str(value)

        # 提交之后立刻存起来（下次打开就是"记忆上一次"）。
        with contextlib.suppress(Exception):
            self.config.mc_port = int(self._committed.get("mc_port") or 0)
            self.config.local_port = int(
                self._committed.get("local_port") or 0
            )
            cfgmod.save_config(self.config)

        self._on_config_edited()

        # ---- 成功：状态驱动的刷新，那一行立刻变成「✓ 已生效：<值>」----
        #
        # 反馈文字**不在这里写死**，而是由 _sync_config_buttons() 按当前状态
        # 统一算出来。理由见那个函数的说明：写死的文字在切语言/主题重建界面后
        # 会消失，于是"重建前后界面必须一致"的外观测试就会红 —— 那说明它
        # 本来就不是一个稳定的状态。
        self._sync_config_buttons()

        if port_hot_switch:
            # 已经弹窗确认过：现在真的把它用起来。
            #
            # 监听 socket 是在**会话创建那一刻**绑上去的，光改配置只是"下次用"。
            # 所以这里原地重建这一次会话（对玩家 = 他那一条隧道）：
            # `_rebuild_tunnels()` 会 stop -> start，用新的 local_port 重新监听，
            # 并按**房间当前模式**重新建立隧道、自动重连；`running` 不变，
            # 所以"原来是什么状态，改完还是什么状态"。
            self.log(
                f"{name} set to {value}; rebuilding this player's tunnel now "
                "(the new port takes effect immediately; the game reconnects "
                "automatically)",
                kind="warn",
            )
            self._rebuild_tunnels(mode=self._room_conn_mode() or None)
        elif key == "local_port":
            self.log(
                f"{name} set to {value} (idle: applied immediately, "
                "can be changed again at any time)",
                kind="ok",
            )
        else:
            self.log(
                f"{name} set to {value} (room not created; can be changed again at any time)", kind="ok"
            )

    def _mark_row_blocked(self, key):
        """被锁定/被拒绝时，把那一行的反馈也改掉（不能只弹窗）。"""
        self._set_row_status(
            key,
            "🔒 已锁定 —— 房间已建，要改请先删房重建",
            kind="warn",
        )

    def _commit_max_players(self):
        if self._config_locked("max_players"):
            self._config_locked_notice("max_players")
            self._mark_row_blocked("max_players")
            return

        raw = str(self.max_players_var.get()).strip()

        try:
            value = int(raw)
        except (TypeError, ValueError):
            self._set_row_status(
                "max_players",
                "✗ 「%s」不是数字",
                kind="error",
                value=raw,
            )
            messagebox.showwarning(
                "不是数字",
                f"人数上限必须是数字（{proto.MIN_MAX_PLAYERS}-"
                f"{proto.DEFAULT_MAX_PLAYERS}）。",
                parent=self.root,
            )
            return

        if not (
            proto.MIN_MAX_PLAYERS <= value <= proto.DEFAULT_MAX_PLAYERS
        ):
            self._set_row_status(
                "max_players",
                "✗ 超出范围（只能是 %d-%d 人）"
                % (proto.MIN_MAX_PLAYERS, proto.DEFAULT_MAX_PLAYERS),
                kind="error",
            )
            messagebox.showwarning(
                "人数超范围",
                f"人数上限只能是 {proto.MIN_MAX_PLAYERS}-"
                f"{proto.DEFAULT_MAX_PLAYERS} 人，你填的是 {value}。\n\n"
                "服务器允许的最大值是 "
                f"{proto.DEFAULT_MAX_PLAYERS} 人（单个房间）。",
                parent=self.root,
            )
            return

        self.max_players_var.set(str(value))
        self._committed["max_players"] = str(value)

        with contextlib.suppress(Exception):
            self.config.max_players = value
            cfgmod.save_config(self.config)

        self._on_config_edited()

        # 成功反馈同样由状态驱动（会显示「✓ 已生效：N 人（建房时用）」那类文字
        # 是在 _sync_config_buttons 里统一算的）。
        self._sync_config_buttons()

        self.log(
            f"max players set to {value} (applies when the room is created, locked after that)", kind="ok"
        )

    def _config_locked_notice(self, key="max_players"):
        """
        建房之后想改这两个"房间命根子"数字：明确告诉他要先删房。

        用户定的原则：「房间人数和 host 代理端口，建房后锁定，就是房主本人
        和服务器管理员都不允许修改，这东西一改联机全掉了。」
        """
        what = (
            "主机 Host 的局域网端口"
            if key == "mc_port"
            else "人数上限"
        )

        self.log(
            f"once the room is created, {what} is locked (the tunnels are built "
            "at that exact moment) -- delete the room first, change it, then create it again.",
            kind="error",
        )

        messagebox.showwarning(
            "这个数字已经锁定",
            f"房间一旦建好，就不能再改{what}了。\n\n"
            "原因：隧道是在建房那一刻按这个数字建好的。"
            "再改只会在新参数上又建一批，两批隧道同时活着，"
            "玩家就会连到旧的那一批上（连接丢失）。\n\n"
            "这一条对谁都一样 —— 房主、接管的 host、服务器管理员都不能改。\n\n"
            "要换参数：先「删除我的房间」，改完再重新建房。",
            parent=self.root,
        )

    def _committed_value(self, key, default=0):
        """
        连接时读这个，不直接读输入框（输入框里可能停着改了没提交的值）。

        回退顺序：按过确定的值 → 输入框当前的字 → `default`。

        为什么允许回退到输入框：这个函数也被测试和"程序化设置"的路径用到
        （`mc_port_var.set("25565")` 这种）。不给回退，那些路径就会莫名其妙
        拿到 0；给了回退，用户在主界面上永远拿不到"没提交的值"，因为他必须
        按确定才会走到连接这一步。
        """
        raw = str(self._committed.get(key) or "").strip()

        if not raw:
            variable = getattr(self, f"{key}_var", None)

            if variable is not None:
                with contextlib.suppress(Exception):
                    raw = str(variable.get()).strip()

        if not raw:
            return int(default or 0)

        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(default or 0)

    def _build_action_card(self, parent):
        card = Card(parent, title="操作")
        card.pack(fill="x", pady=(0, 10))

        # ---- 房间 ----
        #
        # Room membership and the Minecraft connection are separate steps on
        # purpose. Being IN a room (watching the ping, waiting for a host) is
        # a different state from being connected to a world, and the relay
        # tracks them separately so the host slot can be claimed before anybody
        # starts playing.
        # 房间号输入、创建/加入/退出，以及房间列表，全部搬到「房间」卡片里
        # （见 _build_room_card）。这里只留主机位置和连接按钮。

        # ---- host slot ----
        host_row = tk.Frame(card.body, bg=CARD)
        host_row.pack(fill="x", pady=(8, 0))

        self.claim_host_button = self._button(
            host_row, "认领主机身份 (host)", self.on_claim_host, width=14, secondary=True,
        
    icon="⚑",
        )
        self.claim_host_button.pack(side="left")
        self.claim_host_button.configure(state="disabled")

        self.release_host_button = self._button(
            host_row, "释放主机身份", self.on_release_host, width=14, secondary=True,
        
    icon="⚐",
        )
        self.release_host_button.pack(side="left", padx=(6, 0))
        self.release_host_button.configure(state="disabled")

        # 用户 2026-09-27：运行中不让释放是**有意**的（必须先停止），不是 bug；
        # 但要说清楚，别让用户以为按钮"丢了"。
        Tooltip(
            self.release_host_button,
            "释放主机身份：把主机位置交出去，让别人可以认领。\n"
            "连接运行中不能释放 —— 请先点「停止」，否则正在你世界里的玩家"
            "会突然断开。\n"
            "（「停止」只停止开服，不会释放主机位置。）",
        )

        # ---- connect ----
        row = tk.Frame(card.body, bg=CARD)
        row.pack(fill="x", pady=(12, 0))

        self.start_button = self._button(
            row,
            "开始连接",
            self.on_start,
            width=16,
            primary=True,
        
    icon="▶",
        )
        self.start_button.pack(side="left")

        self.stop_button = self._button(
            row,
            "停止",
            self.on_stop,
            width=10,
            secondary=True,
        
    icon="■",
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        self.stop_button.configure(state="disabled")

        # ---- Minecraft 连接地址（一键复制）----
        tk.Label(
            card.body,
            text="Minecraft 连接地址",
            bg=CARD,
            fg=FG_DIM,
            font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x", pady=(12, 0))

        addr_row = tk.Frame(card.body, bg=CARD)
        addr_row.pack(fill="x", pady=(4, 0))

        self.addr_var = tk.StringVar(value="—")
        self.addr_label = tk.Label(
            addr_row,
            textvariable=self.addr_var,
            bg=CARD_ALT,
            fg=ACCENT,
            font=FONT_MONO,
            anchor="w",
            padx=8,
            pady=5,
        )
        self.addr_label.pack(side="left", fill="x", expand=True)

        self.copy_button = self._button(
            addr_row, "复制", self.on_copy_address, width=6, secondary=True,
        
    icon="⧉",
        )
        self.copy_button.pack(side="left", padx=(6, 0))

        self.connect_hint = tk.Label(
            card.body,
            text="",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=330,
        )
        self.connect_hint.pack(fill="x", pady=(8, 0))

        # ---- 更多操作 ----
        more = tk.Frame(card.body, bg=CARD)
        more.pack(fill="x", pady=(10, 0))

        self.delete_room_button = self._button(
            more,
            "删除我的房间",
            self.on_delete_my_room,
            width=14,
            secondary=True,
        
    icon="🗑",
        )
        self.delete_room_button.pack(side="left")
        self.delete_room_button.configure(state="disabled")

    def _build_room_card(self, parent, expand=False):
        """
        「房间」卡片：房间名、创建/加入/退出，以及房间列表。

        WHY IT LIVES HERE, NOT IN A DIALOG

        Creating a room used to sit in 身份配置（和身份没关系），and the room
        list was behind its own button that opened a second window. Both were
        extra steps for the two things a person does constantly, and the main
        window had a空着的角落 anyway. Now: type a name, press 创建, and watch
        the list right below it.

        Everything keeps working exactly as before -- the same calls, the same
        validation, the same buttons (they merely moved).

        `expand=True`（用户要求）让这张卡纵向铺满窗口：房间列表跟着变长，
        而不是下面留一片空白。
        """
        card = Card(parent, title="房间")
        card.pack(fill="both" if expand else "x", expand=expand, pady=(0, 10))

        # 房间名默认留空（不预填、不随机）：预填会让两个人不约而同进同一个房间。
        self.room_var = tk.StringVar(value="")

        tk.Label(
            card.body,
            text="房间名（自己取，中文 / 英文都行；同名就是同一个房间）",
            bg=CARD,
            fg=FG_DIM,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            # 这张卡现在是整行宽的，所以 wrap 可以放宽；给个上限只是为了
            # 窗口被拉得很窄时它不会反过来把卡片撑宽。
            wraplength=520,
        ).pack(fill="x")

        room_row = tk.Frame(card.body, bg=CARD)
        room_row.pack(fill="x", pady=(4, 0))

        self.room_entry = ttk.Entry(
            room_row, textvariable=self.room_var, style=ENTRY_STYLE
        )
        self.room_entry.pack(side="left", fill="x", expand=True)

        # ---- 创建 / 加入 / 退出 ----
        actions = tk.Frame(card.body, bg=CARD)
        actions.pack(fill="x", pady=(8, 0))

        self.create_room_button = self._button(
            actions, "创建房间", self.on_create_room, width=11, primary=True,
        
    icon="✚",
        )
        self.create_room_button.pack(side="left")

        self.join_room_button = self._button(
            actions, "加入房间", self.on_join_room, width=11, secondary=True,
        
    icon="→",
        )
        self.join_room_button.pack(side="left", padx=(6, 0))

        self.leave_room_button = self._button(
            actions, "退出房间", self.on_leave_room, width=11, secondary=True,
        
    icon="←",
        )
        self.leave_room_button.pack(side="left", padx=(6, 0))
        self.leave_room_button.configure(state="disabled")

        # ---- 房间成员（3.0.9）----
        #
        # 列表放在独立窗口里（7 列 + 每行的「踢出」按钮，塞进卡片会把整页顶宽）。
        # 窗口里的每一行按成员 key **复用控件**、只改文字，所以实时刷新不会
        # 闪、不会重建、更不会抢焦点（参见房间列表的 _room_list_filling 守卫）。
        self.members_button = self._button(
            actions, "房间成员", self.on_show_members, width=11, secondary=True,

    icon="☰",
        )
        self.members_button.pack(side="left", padx=(6, 0))
        self.members_button.configure(state="disabled")

        # Membership state, updated from HOST_STATE pushes.
        self.room_state_label = tk.Label(
            card.body,
            text="未加入房间",
            bg=CARD,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=420,
        )
        self.room_state_label.pack(fill="x", pady=(6, 0))

        # ---- 房间列表（内嵌，不再是二级弹窗）----
        list_head = tk.Frame(card.body, bg=CARD)
        list_head.pack(fill="x", pady=(10, 4))

        tk.Label(
            list_head, text="在线房间", bg=CARD, fg=FG_DIM, font=FONT_SMALL
        ).pack(side="left")

        self.room_list_hint = tk.Label(
            list_head, text="", bg=CARD, fg=FG_MUTED, font=FONT_SMALL
        )
        self.room_list_hint.pack(side="right")

        self.rooms_button = self._button(
            list_head, "刷新", self.on_rooms_refresh, width=6, secondary=True,
        
    icon="⟳",
        )
        self.rooms_button.pack(side="right", padx=(0, 8))

        table_frame = tk.Frame(card.body, bg=CARD)
        table_frame.pack(fill="both", expand=True)

        columns = ("room", "owner", "host", "inside", "players", "state")

        self.rooms_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=5,
            style="Dark.Treeview",
        )

        # ============================================================================
        #  列宽之和必须远小于 520px（用户："这些栏太大了导致默认窗口装不下"）
        # ============================================================================
        #
        # 房间列表是整页最宽的东西：列宽是**像素**，不随语言/缩放变，所以只要
        # 列宽之和偏大，它就会把整张卡片（进而整个页面）顶宽，默认 1040 的窗口
        # 装不下就只能横向滚动 —— 而横向滚动是被禁止的。
        #
        # 这里的数字是"表头在 tk scaling 1.3、小号字下不被裁掉"的最小值
        # （表头字号见 _build_styles 的 Dark.Treeview.Heading）。合计 456px：
        # 卡片本身还有 56px 的内边距，加起来 512 < 520。
        #
        # 列的实际宽度会比这个值大得多：Treeview 的列默认 stretch=True，卡片
        # 是整行宽的（≈948px），多出来的空间会按比例分给六列。
        for key, title, width in (
            ("room", "房间名", 84),
            ("owner", "房主（建房的人）", 116),
            ("host", "当前主机", 72),
            ("inside", "房间内", 58),
            ("players", "已加入世界", 78),
            ("state", "状态", 48),
        ):
            self.rooms_tree.heading(key, text=title)
            self.rooms_tree.column(key, width=width, anchor="center")

        # ============================================================================
        #  没有滚动条（用户："房间列表滚轮那个边上那个滚轮条去掉 难看"）
        # ============================================================================
        #
        # 滚轮照旧能滚这个列表：ScrollFrame.install_wheel() 的全局滚轮处理
        # 先用 _can_scroll_itself() 问"指针下面这个控件自己还能不能滚"，
        # Treeview 在 OWN_SCROLL_WIDGETS 里，能滚就把事件让给它（Tk 自己给
        # Treeview 注册了 <MouseWheel> 类绑定）；只有它滚到头了，滚轮才转去滚
        # 整个页面。所以去掉滚动条不影响滚轮。
        self.rooms_tree.pack(fill="both", expand=True)

        self.rooms_tree.tag_configure("busy", foreground=WARN)
        self.rooms_tree.tag_configure("empty", foreground=OK)
        self.rooms_tree.tag_configure("mine", background=TILE_ON)
        self.rooms_tree.tag_configure("mine_busy", background=TILE_ON, foreground=WARN)

        # 选一行 = 把房间名填进输入框（这样「加入房间」按钮立刻可用，不会再
        # 出现"在列表里选了房间、点加入却提示要输入房间名"）；
        # 双击 = 直接进这个房间。
        self.rooms_tree.bind("<<TreeviewSelect>>", self.on_room_row_selected)
        self.rooms_tree.bind("<Double-1>", lambda _e: self.on_join_selected_room())

        self.room_rows = []

        #: 上一次自动刷新的时间（monotonic）与"正在刷新"标记，用于实时刷新节流。
        self._room_list_fetched_at = 0.0
        self._room_list_busy = False
        #: 自动刷新失败只记一条日志，不要每个周期都刷屏。
        self._room_list_failed_once = False

        #: 正在"程序化地恢复选中行"。`<<TreeviewSelect>>` 会在刷新时被触发，
        #: 那种触发不能去改写房间名输入框（见 on_room_row_selected）。
        self._room_list_filling = False

    def _build_status_card(self, parent, expand=False):
        card = Card(parent, title="运行状态")
        card.pack(fill="both" if expand else "x", expand=expand)

        grid = tk.Frame(card.body, bg=CARD)
        grid.pack(fill="x")

        self.status_vars = {}
        self.status_labels = {}

        rows = [
            ("state", "状态"),
            ("role", "身份"),
            # V3：这一行回答"数据到底走哪条路"。STUN 模式打通了就是直连，
            # 没打通就是中转 —— 用户不需要去猜，也不需要在日志里翻。
            ("path", "通路"),
            # V3 直连动态：这三行取代了原来那张独立的「网络诊断」卡片
            # （用户要求移除）。诊断的信息并入连接状态，一个地方说一件事。
            ("nat", "网络类型"),
            # 入站：能不能被"陌生主机"打进来。这是他整套判定里唯一描述
            # 【入方向】的量，也是"打不通到底怪谁"的唯一依据。
            ("inbound", "入站可达"),
            # 「直连尝试」那一行【已删除】（用户要求，2026-09-24）：
            # 它和「通路」「打洞进度」说的是同一件事，而且它的措辞来自旧模型
            # （"尝试"暗示直连要先试一下、成不成看游戏）。现在的模型是
            # "点开始连接就打洞、打通了就是长连接"，所以那一行是纯冗余。
            # 直连状态由「通路」显示（直连 P2P <peer>），进度由「打洞进度」显示。
            #
            # 打洞进行到哪一步：第几次、在等什么、还要等几秒。
            ("punch", "打洞进度"),
            ("room", "房间"),
            ("room_members", "房间内在线"),
            ("players", "已加入世界"),
            ("local_links", "本地连接"),
            ("ping", "中转延迟"),
            # 对端延迟（玩家↔主机）：STUN 直连且链路已打通才有数字，否则 N/C。
            # 和中转延迟是两件事，刻意分两行 —— 中转延迟永远有，对端延迟不适用
            # 时明确写 N/C，绝不拿中转的数字冒充直连。
            ("peer_ping", "对端延迟"),
            ("speed", "实时速度"),
            ("traffic", "累计流量"),
        ]

        # 3.0.9「一人一行」的直连行：控件按 peer 复用（见 _render_hole_rows），
        # 重建界面时清空登记表，避免握着已经销毁的控件。
        self.hole_rows_frame = None
        self._hole_rows = {}

        grid_row = 0

        for key, label in rows:
            label_widget = tk.Label(
                grid,
                text=tr(label),
                bg=CARD,
                fg=FG_DIM,
                font=FONT_SMALL,
                anchor="w",
                width=9,
            )
            label_widget.grid(row=grid_row, column=0, sticky="w", pady=2)

            var = tk.StringVar(value="—")
            self.status_vars[key] = var

            value_widget = tk.Label(
                grid,
                textvariable=var,
                bg=CARD,
                fg=FG,
                font=FONT_BOLD,
                anchor="w",
            )
            value_widget.grid(row=grid_row, column=1, sticky="w", pady=2)

            # Remembered so _refresh_status can recolour by meaning. Every
            # row used to be plain white, which made "运行中" and "错误" look
            # identical at a glance.
            self.status_labels[key] = value_widget

            if key == "ping":
                self.ping_label = value_widget
                Tooltip(
                    label_widget,
                    "中转延迟：本机 ↔ 中转服务器的往返时间（每 3 秒测一次）。\n"
                    "它不是游戏内延迟：你和主机之间的实际延迟\n"
                    "≈ 你的中转延迟 + 主机的中转延迟。",
                )

            elif key == "peer_ping":
                self.peer_ping_label = value_widget
                Tooltip(
                    label_widget,
                    "对端延迟：本机 ↔ 对端（玩家或主机）的直连往返时间，\n"
                    "由已打通的直连隧道自身量出。\n"
                    "N/C = 不适用：没有直连（走中转 / 未连接 / 洞没打通）。",
                )

            elif key == "speed":
                self.speed_label = value_widget
                Tooltip(
                    label_widget,
                    "实时速度：本机当前每秒收发的总数据量（中转 + 对端直连）。\n"
                    "↑ 上行 = 本机发出去的数据\n"
                    "↓ 下行 = 本机收到的数据\n"
                    "没有流量时显示 0，而不是「—」。",
                )

            elif key == "traffic":
                Tooltip(
                    label_widget,
                    "累计流量：从**启动客户端**至今收发的总数据量，按通路分开：\n"
                    "「中转」= 经中转服务器转发的字节；\n"
                    "「对端」= 直连隧道直接搬运的字节。\n"
                    "离开房间不清零，只有重启客户端才清零。",
                )

            grid_row += 1

            # ============================================================================
            #  3.0.9：一个对端一条洞 = 一行（用户要求）
            # ============================================================================
            #
            # 这一块**紧跟在「打洞进度」下面**：那一行说的是"整体在干什么"，
            # 这里说的是"每个玩家自己的洞怎么样了"。多一个人多一行，人走了
            # 那一行跟着消失，绝不留空行。
            if key == "punch":
                self.hole_rows_frame = tk.Frame(grid, bg=CARD)
                self.hole_rows_frame.grid(
                    row=grid_row, column=0, columnspan=2, sticky="ew"
                )
                grid_row += 1

    def _local_conn_state(self):
        """
        **我自己**现在的连接状态（`idle` / `pairing` / `connected`）。

        ============================================================================
          ★ 用户 2026-10-01 第 3 条：成员列表那一列改成"读本地状态 -> 上报"
        ============================================================================

        用户原话：「这里就只是一个显示问题，实际连接都是已经过验证的 OK 的，
        你别给我乱改实际连接的部分，只动状态展示部分」。所以这个函数**只读**
        已经存在的、界面本来就在读的那几个字段（会话状态 / 房间打洞状态），
        然后交给 `RoomSession.report_conn_state()` 上报给中继展示。

        判据（和状态卡同一套，绝不新发明）：
          * 不在房间里 -> `""`（什么都不报）；
          * 会话没跑 -> `idle`；
          * 会话状态不是 ONLINE（正在连接 / 正在自动重连）-> `pairing`；
          * ONLINE 且（非 STUN 模式，或 STUN 下洞已通/正在承载）-> `connected`。
        """
        state = None

        with contextlib.suppress(Exception):
            state = self.engine.room_state()

        if state is None or getattr(state, "revoked", False):
            return ""

        if not getattr(self, "running", False):
            return "idle"

        snapshot = {}

        with contextlib.suppress(Exception):
            snapshot = self.engine.snapshot() or {}

        if str(snapshot.get("state") or "") != "ONLINE":
            return "pairing"

        mode = str(
            snapshot.get("mode") or self._room_conn_mode() or MODE_RELAY
        )

        if mode != MODE_STUN:
            return "connected"

        punch = {}

        with contextlib.suppress(Exception):
            punch = self.engine.room_direct_state() or {}

        if (
            punch.get("stun_ready")
            or punch.get("direct_carrying")
            or snapshot.get("direct_carrying")
        ):
            return "connected"

        # STUN 模式还没打通：如实说"配对中"，不假装已连接。
        return "pairing"

    def _report_local_conn_state(self):
        """把本地连接状态上报给中继（**纯显示**；失败什么都不做）。"""
        with contextlib.suppress(Exception):
            session = self.engine.room_current()

            if session is None:
                return

            value = self._local_conn_state()

            if value:
                session.report_conn_state(value)

    def _refresh_countdown_rows(self):
        """
        倒计时期间（每 150ms 一拍）只重画「逐目标行」+「打洞进度」两处文字。

        **纯显示**：不建控件、不抢焦点、不调引擎的任何动作。没有倒计时就是空操作
        （所以正常情况下这一拍什么都不做）。

        用户 2026-09-30（第三轮第 7 条）：起跑前的相对倒计时要走成
        `1.5s -> 1.2s -> 开打`，1 秒一拍的界面画不出这个。
        """
        engine = getattr(self, "engine", None)

        if engine is None:
            return

        try:
            room_punch = engine.room_direct_state() or {}
        except Exception:
            return

        if not room_punch.get("countdown"):
            return

        with contextlib.suppress(Exception):
            # 和 `_refresh_status` 走**同一个**视图构造器：两处读两份对象，
            # 正是"通路说直连尝试中、打洞进度说已直连"那种自相矛盾的来源。
            snapshot = self.engine.snapshot() or {}
            step_text, step_hint = punch_step_text(
                self._direct_status_view(snapshot)
            )
            self.status_vars["punch"].set(tr(step_text))
            self._set_status_color("punch", step_hint)

        self._render_hole_rows(
            room_punch.get("links"), punching=bool(room_punch.get("punching"))
        )

    def _render_hole_rows(self, links, punching=True):
        """
        运行状态卡片里"一人一行"的直连状态。

        ============================================================================
          数据只有一处：RoomDirectLink.state()["links"]
        ============================================================================
          * 已经打通   -> `直连 <peer> <ip:port> 已打通`
          * 正在打洞   -> `打洞中 <peer> 第 N 次`（N = 这个 peer 自己的次数）
          * 没在打洞   -> `未打洞（中转模式）`（审计问题 2）

        ============================================================================
          ★ 为什么"打洞中"必须要有 `punching` 这一道门
        ============================================================================
        用户实测（审计问题 2）：切到中转之后，状态卡下面仍然一人一行
        「打洞中 X 第 N 次」。两个原因叠加：
          * `_render_hole_rows` 被无条件调用，没有模式门；
          * 一行只要 `ready` 为假就写「打洞中」，`attempts==0` 也一样 ——
            而"这个对端一次都还没打过"和"正在打"是两回事。
        所以：非 STUN / 没有在打洞时**不出行**；万一还有行落到这里，
        `attempts==0` 的写「未打洞（中转模式）」，绝不写「打洞中」。

        行控件按 peer 复用；人走了就销毁那一行 —— 所以不会出现空行，也不会
        因为每秒刷新而闪。

        对端延迟跟着这一行走：适用（已打通）时是直连隧道的真实往返时间，还没
        测出来 `-`；没打通（不适用）写 `N/C`（见 peer_latency_text）。
        """
        frame = getattr(self, "hole_rows_frame", None)

        if frame is None:
            return

        with contextlib.suppress(Exception):
            if not frame.winfo_exists():
                return

        # 没有在打洞（relay 模式 / 会话没跑）就不该有"打洞中"的行。
        #
        # 但**行本身是有证据的**：一条 `ready`（已打通）或 `attempts>0`（这个
        # 对端真的打过）的行，说明打洞确实发生过。老中继 / 老快照可能没有
        # `punching` 这个 key，所以那种情况下按行的证据行事，而不是把一条活着的
        # 直连从界面上抹掉。
        punching = bool(punching)

        if not punching and any(
            item.get("ready") or int(item.get("attempts") or 0)
            for item in (links or ())
            if item.get("peer")
        ):
            punching = True

        if not punching:
            links = []

        # 中继广播的每成员显示名（有就用真名；老中继 -> 空表，回退到标识）。
        names = {}

        with contextlib.suppress(Exception):
            state = self.engine.room_state()
            names = dict(getattr(state, "member_names", None) or {})

        links = [item for item in (links or ()) if item.get("peer")]
        wanted = [item["peer"] for item in links]

        for peer in list(self._hole_rows):
            if peer not in wanted:
                _row, label = self._hole_rows.pop(peer)

                with contextlib.suppress(Exception):
                    label.destroy()

                with contextlib.suppress(Exception):
                    _row.destroy()

        for index, item in enumerate(links):
            peer = item["peer"]

            # 对端显示名：中继广播的真名优先；没有（老中继）时，合成标识
            # （"peer"/"guest-N"）显示成「对端」，其余用它的标识。
            raw = str(peer)
            name = names.get(raw) or (
                tr("对端")
                if raw in ("peer", "-") or raw.startswith("guest-")
                else raw
            )

            # ============================================================
            #  §G 第 3 层（2026-09-28 设计基准）：逐目标一行，实时阶段
            # ============================================================
            #
            # 版式（用户 2026-09-28 第 1/2 条）：
            #     `等待同步起跑（3s）【玩家名】N/C`
            #     `打洞中【玩家名】第 3 次 · N/C`
            #     `P2P 已直连【玩家名】192.0.2.13:40879 18ms`
            #
            # 拼装放在模块级纯函数 `hole_row_text()` 里：界面和回归测试用的是
            # **同一份**代码（不会出现"测试拼的"和"界面拼的"两个版本）。
            #
            # ★ 这一整段**只读**：`row_parts()` 不碰任何状态。用户明确要求
            #   "不许为修 UI 改动 P2P 逻辑"。
            text, hint = hole_row_text(item, fallback_name=name)
            colour = {"ok": OK, "warn": WARN, "muted": FG_MUTED}.get(hint, FG)

            widgets = self._hole_rows.get(peer)

            if widgets is None:
                row = tk.Frame(frame, bg=CARD)
                label = tk.Label(
                    row,
                    text=text,
                    bg=CARD,
                    fg=colour,
                    font=FONT_SMALL,
                    anchor="w",
                )
                label.pack(side="left")
                widgets = (row, label)
                self._hole_rows[peer] = widgets
            else:
                row, label = widgets

                with contextlib.suppress(Exception):
                    label.configure(text=text, fg=colour)

            with contextlib.suppress(Exception):
                row.pack(fill="x", pady=1)

    def _set_status_color(self, key, color):
        """
        Recolour one value on the 运行状态 card.

        Never raises: if the card was not built (or Tk is shutting down) the
        lookup simply does nothing, because a colour is cosmetic and must not
        be able to break a status refresh.
        """
        widget = self.status_labels.get(key)

        if widget is None:
            return

        with contextlib.suppress(Exception):
            widget.configure(fg=color)

    # --------------------------------------------------------
    # Log panel
    # --------------------------------------------------------

    def _build_log_card(self, parent):
        card = Card(parent, title="日志")
        card.pack(fill="both", expand=True, pady=(14, 0))

        toolbar = tk.Frame(card.body, bg=CARD)
        toolbar.pack(fill="x", pady=(0, 8))

        self._button(
            toolbar,
            "复制日志",
            self.on_copy_log,
            width=10,
            secondary=True,
        ).pack(side="left")

        self._button(
            toolbar,
            "保存日志",
            self.on_save_log,
            width=10,
            secondary=True,
        ).pack(side="left", padx=(6, 0))

        self._button(
            toolbar,
            "清空",
            self.on_clear_log,
            width=8,
            secondary=True,
        ).pack(side="left", padx=(6, 0))

        self._button(
            toolbar,
            "关于",
            self.on_about,
            width=8,
            secondary=True,
        ).pack(side="right", padx=(0, 6))

        self._button(
            toolbar,
            "打开数据文件夹",
            self.on_open_data_dir,
            width=16,
            secondary=True,
        ).pack(side="right", padx=(0, 6))

        text_frame = tk.Frame(card.body, bg=LOG_BG)
        text_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            text_frame,
            bg=LOG_BG,
            fg=LOG_FG,
            font=FONT_MONO,
            relief="flat",
            wrap="word",
            height=12,
            insertbackground=FG,
            padx=10,
            pady=8,
            state="disabled",
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(
            text_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        scroll.pack(side="right", fill="y")

        self.log_text.configure(yscrollcommand=scroll.set)

        self.log_text.tag_configure("normal", foreground=LOG_FG)
        self.log_text.tag_configure("info", foreground=ACCENT)
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("warn", foreground=WARN)
        self.log_text.tag_configure("error", foreground=BAD)
        self.log_text.tag_configure("dim", foreground=FG_MUTED)

    # --------------------------------------------------------
    # Widget factory
    # --------------------------------------------------------

    def _button(
        self,
        parent,
        text,
        command,
        width=12,
        primary=False,
        secondary=False,
        icon="",
    ):
        bg = ACCENT if primary else (CARD_ALT if secondary else CARD_ALT)
        fg = "#ffffff" if primary else FG

        # 图标（纯观感）：用户要的"按钮加个小图标"。
        label = f"{icon} {tr(text)}" if icon else tr(text)

        button = RoundButton(
            parent,
            # 按钮文字统一在这里翻译：全项目的按钮都走 _button()，
            # 所以这一处就够，不必去改几十个调用点。
            text=label,
            command=command,
            width=width,
            primary=primary,
            bg=bg,
            fg=fg,
            font=FONT_BOLD if primary else FONT,
        )

        button._primary = primary  # noqa: SLF001 - used by _set_enabled

        return button

    def _set_enabled(self, button, enabled):
        if enabled:
            button.configure(
                state="normal",
                bg=ACCENT if getattr(button, "_primary", False) else CARD_ALT,
            )
        else:
            button.configure(state="disabled", bg=CARD)

    # --------------------------------------------------------
    # V3: room membership and the host slot
    # --------------------------------------------------------

    def _room_credentials(self):
        """The connection details every room call needs. May raise ValueError."""
        # ★ D10：地址/端口是用户填的，缺任何一项都抛带引导的 ValueError，
        # 而不是拿一个内置默认值去连。
        host, port = self._require_server(show_error=False)

        return (
            host,
            port,
            self.client_id_var.get().strip(),
        )

    def _current_room_name(self):
        """建房/加入的目标：输入框里填的那个【房间名】（中文/空格都行）。"""
        return self.room_var.get().strip()

    def _room_in_use(self):
        """我【现在真正在的】房间的显示名（不在房间里时返回空串）。"""
        session = self.engine.room_current()

        if session is None:
            return ""

        state = getattr(session, "state", None)

        if state is None:
            return ""

        return str(
            getattr(state, "room_name", "") or getattr(state, "room", "") or ""
        ).strip()

    def _active_room_name(self):
        """
        我【现在真正在的】房间名；不在房间里时退回输入框的内容。

        ============================================================================
         为什么必须和输入框分开
        ============================================================================

        The room input is cleared the moment a room is created or joined (that
        was a deliberate fix: a leftover name made people think they were still
        in the old room, and made "create" reuse a stale name).

        But 开始连接 and 删除房间 used to read THAT box. So after joining a room
        the box was empty, and pressing 开始连接 answered 「房间不能为空」 while
        the client was demonstrably inside a room. The session must always be
        built for the room we are actually in -- never for whatever happens to
        be typed in a text field.
        """
        return self._room_in_use() or self.room_var.get().strip()

    def _active_room_token(self):
        """
        我真正在的那个房间的 room token（房间 ID）。

        发给中继的每一行都得用它：房间名可能是中文或带空格，直接塞进
        空格分隔的协议行会被切断。不在房间里时返回空串。
        """
        session = self.engine.room_current()
        state = getattr(session, "state", None) if session is not None else None

        return str(getattr(state, "room", "") or "").strip()

    def _confirm_room_is_free(self, room, host, port, client_id):
        """
        建房前检查这个房间名有没有人在用。

        Returns True when it is safe to go ahead. When somebody is already in
        that room the user is asked what they meant, because both readings are
        plausible:

          * "我要加入他" -> switch to 加入房间
          * "我名字打错了" -> 改个名字再建

        Doing nothing here was the bug: 创建 on an existing name either collided
        with the live room or (worse, before this) took it over.
        """
        try:
            # ============================================================================
            #  这个查询跑在界面线程上，所以给它一个短上限
            # ============================================================================
            #
            # fetch_rooms() 是同步的（submit → 等网络答案），默认 20 秒。网络
            # 一弱，界面就真的会卡住好几秒 —— 不只是"慢一下"，是整窗不响应。
            #
            # 建房流程本身不改（它要的就是"现在能不能建"这一个同步答案），只把
            # 等待上限压到几秒：查不到就当"不清楚"，让中继去当裁判（重复建房它
            # 会回 ERROR ROOM_EXISTS），这条兜底本来就是下面 except 的语义。
            rooms = self.engine.fetch_rooms(host, port, client_id, "", timeout=4)
        except Exception:
            # Cannot check (relay hiccup): let the relay be the judge -- it
            # refuses a duplicate create anyway.
            return True

        # 比对【房间名】：中继列表里的 room 是房间 ID，name 才是人看到的那个。
        existing = next(
            (
                r
                for r in rooms
                if r.display == room or r.room == room
            ),
            None,
        )

        if existing is None:
            return True

        who = existing.owner or existing.host or "别人"

        if messagebox.askyesno(
            "这个房间名已经有人用了",
            f"房间「{existing.display}」已经由「{who}」建好了，"
            f"现在里面 {existing.members} 人。\n\n"
            "点「是」= 直接加入他的房间（不再新建）\n"
            "点「否」= 取消，我换一个房间名",
            parent=self.root,
        ):
            self.log(f'room "{existing.display}" already exists; joining instead', kind="warn")
            self.on_join_room(existing.display)
            return False

        self.log(f'room name "{room}" is already taken; choose another name', kind="warn")

        return False

    def _validate_room_name(self, room):
        """
        房间名：随便取（中文 / 英文 / 空格 / emoji 都行），只要非空、不太长。

        ============================================================================
         为什么以前会"输入了自定义名称却提示房间号不合法"
        ============================================================================

        The old rule rejected spaces and "/" — those are protocol and URL
        concerns, and they were being pushed onto the person typing. They are
        not the user's problem any more: the name is percent-encoded into an
        ASCII room ID before it goes anywhere near the wire or the panel's
        admin API (see protocol.room_token / server/room_names.py).

        So the only remaining rules are the ones a human would recognise:
        写得下、不是空的、没有换行/制表符这类会把协议行切断的控制字符。
        """
        problem = proto.room_name_problem(room)

        if problem:
            return False, problem

        return True, ""

    def _configured_max_players(self):
        """
        「基准配置」里那个已提交的人数上限，夹到本客户端接受的范围。

        Used as the room's capacity -- not the server's number.

        读的是**按过确定**的值（_committed）；输入框里可能停着改了没提交的
        数字，那种值绝不能拿去建房（隧道是按它建的，建完就锁定了）。
        """
        value = self._committed_value(
            "max_players",
            getattr(self.config, "max_players", None)
            or proto.DEFAULT_MAX_PLAYERS,
        )

        return max(proto.MIN_MAX_PLAYERS, min(value, proto.DEFAULT_MAX_PLAYERS))

    def _room_capacity(self):
        """
        这个房间的上限（中继给的权威数字），没有就返回 0。

        为什么必须有这个：房间人数在建房时就定下并锁死了（见 server 侧
        `set_room_capacity`），而**开几条隧道**必须等于这个数字。
        以前隧道数量只看本机那个输入框 —— 于是「房主建房时填 8、另一个人
        当 host 时本机填 3」，房间就只开 3 条隧道，第 4 个人永远配不上对，
        在 Minecraft 里就是"连不上"（用户报的正是这个）。
        """
        state = self.engine.room_state()

        if state is None:
            return 0

        try:
            return max(0, int(getattr(state, "max_players", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _capacity_within_server(self):
        """
        False (and a message) when our setting is above the server's ceiling.

        WHY THIS IS A LOCAL CHECK

        The server's number is a ceiling, and its room capacity code simply
        cannot give a room more slots than that. Sending 8 to a server limited
        to 6 used to produce the worst possible outcome: the room was created,
        the client opened 8 tunnels, and the connection hung until it timed out
        -- with nothing anywhere saying why.

        Refusing here, with both numbers named, turns that into one sentence.
        """
        server_cap = 0

        if self.auth is not None:
            server_cap = int(getattr(self.auth, "server_max_players", 0) or 0)

        if not server_cap:
            # Older relay: it did not tell us. Its own ceiling still applies,
            # but we cannot compare against a number we do not have.
            return True

        mine = self._configured_max_players()

        if mine <= server_cap:
            return True

        message = (
            f"你设置的人数上限是 {mine} 人，但中转服务器的上限是 {server_cap} 人。\n\n"
            f"房间人数不能超过服务器上限 —— 要么把这里改成 {server_cap} 或更小，"
            "要么请管理员在管理面板里把上限调高。"
        )

        self.log(
            f"max players {mine} exceeds the server limit {server_cap}; "
            "room creation blocked",
            kind="error",
        )

        messagebox.showwarning("人数超过服务器上限", message, parent=self.root)

        return False

    def on_create_room(self):
        """Create a room and become its OWNER."""
        self._open_room(create=True)

    def on_join_room(self, room=None):
        """
        加入一个房间。

        `room` 给了就用它（房间列表里选的那一行），否则用输入框里的房间名。
        这样"在列表里选一个 → 点加入"一定能进得去，不会反过来要求用户再
        手工把名字敲进输入框。
        """
        if room:
            self.room_var.set(room)

        if not self._current_room_name() and self.room_rows:
            # 输入框空着、但列表里选中了一行：就用那一行。
            selected = self._selected_room_row()

            if selected is not None:
                self.room_var.set(selected.display)

        self._open_room(create=False)

    def _open_room(self, create):
        room = self._current_room_name()

        ok, why = self._validate_room_name(room)

        if not ok:
            messagebox.showerror("房间名不合法", why, parent=self.root)
            return

        if not self.require_login():
            return

        try:
            host, port, client_id = self._room_credentials()
        except ValueError as e:
            messagebox.showerror(
                tr("参数错误"), tr(str(e)), parent=self.root
            )
            return

        if self.engine.room_current() is not None:
            messagebox.showinfo(
                "提示",
                "你已经在一个房间里了，请先点「退出房间」。",
                parent=self.root,
            )
            return

        action = "创建" if create else "加入"
        self.log(f'{action} room "{room}" ...', kind="info")

        # ---- 建房前先看一眼这个名字有没有人在用 ----
        #
        # 中继会拒绝重复建房（ERROR ROOM_EXISTS），但让用户在点下去之前就知道
        # 更好：这里查到有人在用，就直接问他是要加入还是换个名字。
        if create and not self._confirm_room_is_free(room, host, port, client_id):
            return

        # ---- 人数：客户端设置是实际值，服务器设置只是上限 ----
        #
        # 服务器上限 6 而本机设了 8 时，以前会"房间建得出来、连上去卡死超时"：
        # 中继按 6 个位置算，客户端却开了 8 条隧道。现在在建房之前就拦下来，
        # 并说清楚是谁限制了谁。
        if create and not self._capacity_within_server():
            return

        try:
            state = self.engine.open_room(
                host,
                port,
                client_id,
                self.auth.username if self.auth else "",
                self.auth.token if self.auth else "",
                room,
                create=create,
                # 只有建房的人把自己的容量带上去；加入的人沿用房间已有的。
                max_players=self._configured_max_players() if create else 0,
                # 建房时把当前选的连接模式带上去（房主之后仍可改）。
                conn_mode=self.mode_var.get() if create else "",
            )
        except RuntimeError as e:
            self.log(f"{action} room failed: {e}", kind="error")

            if self._refuse_outdated(str(e)):
                return

            messagebox.showerror(
                tr(f"{action}房间失败"), tr(str(e)), parent=self.root
            )
            return

        verb = "已创建" if state.is_owner else "已加入"
        shown = getattr(state, "room_name", "") or room

        # 新的房间 = 允许再弹一次"房间被关闭"的提示
        self._room_close_notified = False

        self.log(f'{verb} room "{shown}"', kind="ok")
        self._refresh_room_ui()
        self._refresh_room_list_soon()

        # 进去之后就把输入框清空：那块地方留着旧房间名，很容易让人以为
        # "已经在这个房间里了"，或者在下次建房时误用上一个名字。
        # （房间名本身在房间卡片的状态行里一直显示着，不会丢。）
        self.room_var.set("")

    def on_leave_room(self):
        session = self.engine.room_current()

        if session is None:
            self._refresh_room_ui()
            return

        # Walking out while a Minecraft session is running would leave the
        # tunnel pointing at a room we are no longer in.
        if self.running:
            if not messagebox.askokcancel(
                "退出房间",
                "当前连接仍在运行，退出房间会先停止连接。继续吗？",
                parent=self.root,
            ):
                return

            self.on_stop()

        if not messagebox.askokcancel(
            "退出房间",
            f"确定要离开房间 {self._current_room_name()} 吗？\n\n"
            + (
                "你是这个房间的所有者，离开后房间会被关闭。"
                if self._room_is_owner()
                else "离开后主机身份（如果你持有）会被释放。"
            ),
            parent=self.root,
        ):
            return

        state = self.engine.room_state()

        if state is not None and state.is_host:
            # Release first so the slot frees immediately rather than waiting
            # for the relay's watchdog.
            self.engine.release_host()

        self.engine.close_room()

        self.log("left the room", kind="info")
        self._refresh_room_ui()

    def on_claim_host(self):
        ok, message = self.engine.claim_host()

        if ok:
            self.log(f"host slot claimed: {message}", kind="ok")
        else:
            self.log(f"failed to claim the host slot: {message}", kind="warn")
            messagebox.showwarning("无法认领主机身份", message, parent=self.root)

        self._refresh_room_ui()

    def on_release_host(self):
        """
        释放主机身份。

        ============================================================================
          连接还在跑的时候必须拦住（用户明确要求）
        ============================================================================

        Releasing while Minecraft is being relayed would pull the position out
        from under a live world: the players who are in it are dropped, and the
        person clicking has not stopped anything. So the rule is 先停止、再释放
        — the button is disabled while a session runs, and this check makes it
        true even if the button state is stale.

        Stopping does NOT release the position any more either (see
        engine.stop_session): the position stays yours until you explicitly say
        otherwise, which is exactly what makes the two buttons meaningful.
        """
        if self.running:
            messagebox.showwarning(
                "请先停止连接",
                "当前连接还在运行。\n\n"
                "请先点「停止」，再释放主机身份 —— 否则正在你世界里的玩家"
                "会突然被断开。",
                parent=self.root,
            )
            return

        if not messagebox.askokcancel(
            "释放主机身份",
            "释放后房间里就没有主机了，其他人可以重新认领。\n\n"
            "（你仍然留在房间里，只是不再担任主机。）\n\n确定吗？",
            parent=self.root,
        ):
            return

        ok, message = self.engine.release_host()

        if ok:
            self.log(f"host slot released: {message}", kind="ok")
        else:
            self.log(f"failed to release the host slot: {message}", kind="warn")

        self._refresh_room_ui()
        self._refresh_room_list_soon()

    def _room_is_owner(self):
        state = self.engine.room_state()

        return bool(state is not None and state.is_owner)

    def _refresh_room_ui(self):
        """
        渲染房间状态，并【一次性】把房间相关的按钮状态全部对齐。

        ============================================================================
          为什么按钮状态必须都在这一个函数里
        ============================================================================

        Button states used to be set from four different places (build, start,
        stop, this). When a room was destroyed from the SERVER side, only some
        of them were updated — the窗口 kept looking like "you are still in a
        room", 开始连接 kept being clickable, and the only way out was to press
        退出房间 by hand. That is the "幽灵房" the user reported.

        One function, one truth: every button that depends on "am I in a room /
        am I hosting" is decided HERE, and every path that changes those facts
        calls it. Never raises.
        """
        state = self.engine.room_state()
        has_room = state is not None

        # Live NAT measurement follows the ROOM's mode (see _sync_room_nat):
        # it starts the moment we are in a STUN room and stops when we are not.
        # This is the one function every room-state change already goes
        # through, so hooking it here means no future room event can forget to
        # turn the measurement on or off.
        with contextlib.suppress(Exception):
            self._sync_room_nat()

        # ============================================================================
        #  ★ 房主热切换模式之后，**非房主**的会话也必须按新模式重建
        # ============================================================================
        #
        # 用户实测（审计问题 3/4）：房主把房间从 relay 切到 STUN，非房主这边
        # 什么都没发生，会话继续按旧模式搬数据（日志里 `[RELAY] connecting ...`
        # 一条接一条，而界面写着 STUN）。
        #
        # 原来这段逻辑写在 `_on_mode_change` 里，但它永远不会执行：
        #   * 非房主的模式磁贴被 `_refresh_tiles()` 锁死 -> 点击回调根本不进；
        #   * 就算进去了，`switched = self.config.mode != room_mode` 也是恒假 ——
        #     `_refresh_room_ui` 每秒（本函数上面那句 `self.config.mode =
        #     room_mode`）已经把 config 改成了房间的模式。
        #
        # 所以改到这条**每秒必跑**的同步路径上，判据换成"会话真正在用的模式"，
        # 并且带防抖（见 `_sync_session_mode`）。
        with contextlib.suppress(Exception):
            self._sync_session_mode()

        # 中继主动推过 `CONN_MODE_CHANGED`：等房间广播里的新模式一到就重建一次
        # （见 `_on_conn_mode_changed` 里的 P0 说明）。
        with contextlib.suppress(Exception):
            self._maybe_rebuild_for_mode_change()

        def enable(widget, on):
            # ★ 2026-10-03（分析结论 E2）：以前只改 `state`、不改 `bg` ——
            #   主按钮（开始连接）被释放后仍是"禁用灰底"，看着像没释放。
            #   `_set_enabled` 两样都改，这里统一走它。
            with contextlib.suppress(Exception):
                self._set_enabled(widget, on)

        # ---- 连接模式：跟着房间走，房主之外锁定 ----
        #
        # This is the ONE place that decides the selector's state, for the same
        # reason the room buttons are decided here: two places agreeing by
        # accident is how "displayed mode" and "actual mode" drift apart.
        room_mode = self._room_conn_mode()

        if room_mode is not None:
            if self.mode_var.get() != room_mode:
                self.mode_var.set(room_mode)

            # ================================================================
            #  ★ 2026-09-29（用户第 7 条）：模式变化必须**可归因**
            # ================================================================
            # 本地显示以中继的 `mode=` 为准，但**只跟着它显示**，绝不因为
            # 它去重建/回退。这里落一行日志，用户下次贴日志就能一眼看出
            # "是谁把模式改了"（中继广播 vs 自己的点击）。
            previous_mode = str(getattr(self.config, "mode", "") or "")

            if previous_mode and previous_mode != room_mode:
                self.log(
                    f"[mode] the relay now reports the room mode as "
                    f"{room_mode} (was {previous_mode}); the display follows the "
                    f"relay -- only the room owner can change the mode",
                    kind="warn",
                )

            self.config.mode = room_mode

            self.mode_owner_label.configure(
                text=mode_owner_label_text(
                    room_mode, self._am_room_owner(), True
                )
            )
        else:
            self.mode_owner_label.configure(
                text=mode_owner_label_text(None, False, False)
            )

        # Grey the tiles out for non-hosts by rebuilding them with the lock
        # flag, so a click cannot even start the flow.
        self._refresh_tiles()

        # ---- 房间入口 ----
        enable(self.create_room_button, not has_room)
        enable(self.join_room_button, not has_room)
        enable(self.leave_room_button, has_room)
        # 「房间成员」只在房间里可用：没房间时的列表没有意义，而且会让用户
        # 以为"这里能看到别处的房间"。
        enable(self.members_button, has_room)

        # 「删除我的房间」在房间里就能用（不必先开服）：中继仍然只允许所有者
        # 或当前主机执行，别人点会被它拒绝。
        # 删房 = 只有【房间所有者】能做（2026-09-24：host 不算 —— host 只是
        # 一个任何人都能认领的位置）。服务端也会拒（ERROR NOT_HOST），
        # 这里先灰掉，免得用户点了才发现。
        enable(
            self.delete_room_button,
            has_room and bool(getattr(state, "is_owner", False)),
        )

        # ---- 主机位置 ----
        # 释放必须先停止（用户要求）：连接还在跑时这个按钮是灰的，
        # on_release_host() 里还有第二道拦截。
        #
        # ============================================================================
        #  ★ 用户 2026-09-29：「连接状态 ≠ 身份/职位」—— 认领按钮只看**身份**
        # ============================================================================
        # 主机只是**停止连接**（大家断开），主机位**不释放**：所以这里必须看
        # `state.host_claimed`（中继广播的身份事实），**不能**因为它没连上就
        # 让别人的「认领主机身份」亮起来 —— 那正是用户点名不许出现的行为。
        enable(
            self.claim_host_button,
            bool(
                has_room and not state.is_host and not state.host_claimed
                and not self.running
            ),
        )
        enable(
            self.release_host_button,
            bool(has_room and state.is_host and not self.running),
        )

        # ---- 连接按钮 ----
        #
        # ★ 用户第 3 条：自动重连窗口里也算"连接还在"（见 `_session_busy`），
        # 否则掉线那一瞬间按钮就被释放了 —— 而重连其实正在进行。
        #
        # ★ 2026-10-04（回环回归网钉住的现场，`tests/host_offline_owner_test.py`）：
        #   有一个例外 —— **host 已经走了**（`_reconnect_phase == "host_down"`）
        #   或者这一轮已经收尾（`"stopped"`）。这时后台可能还挂着一次隧道重建
        #   （`_rebuild_in_flight` 要等那个 worker 自己的 `finally` 才复位），
        #   `_session_busy()` 于是仍然报"忙"，按钮就被按住了 —— 可那是上一轮的
        #   收尾，不是"还连着"。用户口径（反复重申）：**host 一走，房内所有人
        #   断开 + 连接按钮当场释放**；`_rebuild_in_flight` 这种长命标记绝不能
        #   把一次真 host-stop 的释放动作一起吞掉。只有模式热切换那条**有界**
        #   窗口（`_mode_switch_silent` / `_mode_rebuild_pending`）里才保持按下，
        #   而那时 phase 仍是 "running"/"reconnecting"，不走这个例外。
        _busy = self._session_busy() and (
            getattr(self, "_reconnect_phase", "") not in ("host_down", "stopped")
        )
        enable(self.start_button, not _busy)
        enable(self.stop_button, bool(_busy))

        # ---- 房间列表的「刷新」：永远可用（它是手动兜底，自动刷新才是常态）----
        enable(self.rooms_button, True)

        if not has_room:
            self.room_state_label.configure(
                text=tr("未加入房间"), fg=FG_MUTED
            )

            with contextlib.suppress(Exception):
                self.header_room_label.configure(
                    text=tr("◆ 未加入房间"), bg=CARD_ALT, fg=FG_MUTED
                )

            # ============================================================
            #  ★ 离开房间后，「基准配置」的确定按钮必须当场解锁
            # ============================================================
            #
            # 用户报告（ISSUES 问题 4）：在房间里 mc_port 的确定按钮是灰的
            # （房间参数锁死，设计如此），**退出房间之后它没有恢复**，于是
            # 即使离开了房间也改不了端口。
            #
            # 根因：按钮状态由 `_sync_config_buttons()` 决定，而它只在
            # 「界面建好 / 提交一次 / 改一个字 / 身份变更」时被调用过 ——
            # 房间状态变化这条最常见路径从来没调过它。房间开了它不会灰，
            # 房间关了它不会亮。这里补在 `_refresh_room_ui` 里：这个函数是
            # **每一次房间状态变化（含每秒自愈）**的必经之处，所以按钮状态
            # 和房间状态永远不会再分家。
            self._sync_config_buttons()

            return

        if state.revoked:
            self.room_state_label.configure(
                text=tr(f"房间已被关闭：{state.revoked_reason}"),
                fg=BAD,
            )

            # 房间被服务端关掉 = 等同于离开房间：同样要解锁。
            self._sync_config_buttons()

            return

        display = state.display

        # ---- 顶部那个醒目的"当前房间"----
        with contextlib.suppress(Exception):
            self.header_room_label.configure(
                text=f"◆ {display}",
                bg=TILE_ON,
                fg=FG,
            )

        if state.is_host:
            host_text = (
                "主机：你（正在开服）" if self.running
                else "主机：你（没在开服 —— 点「开始连接」或「释放主机身份」）"
            )
            host_color = OK
        elif state.host_claimed:
            # ★ 用户 2026-09-29：主机位在（身份）**不等于**它连上了（连接状态）。
            # 只是停止连接时，这里要说"已断开连接 / 未开服"，而不是含糊的
            # "正在开服"，更不是"暂无主机"。
            host_conn = host_conn_state(state)
            suffix = {
                "connected": "（正在开服，可以开始连接）",
                "idle": "（已断开连接 / 未开服）",
            }.get(host_conn, "")
            host_text = f"主机：{state.host_name or '别人'}{suffix}"
            host_color = FG_DIM
        else:
            host_text = "房间还没有主机，无法开始连接"
            host_color = WARN

        # 自己的显示名（中继分配的登录名 / 游客名）也要看得见 —— 否则免登录
        # 用户根本不知道别人在名单里看到的是谁。名字是数据，前缀走 tr()。
        own_name = getattr(state, "own_name", "") or state.username or state.client_id

        self.room_state_label.configure(
            text=(
                f"{display}　"
                f"房间内 {state.members} 人　"
                f"已加入世界 {state.world_players} 人\n"
                f"{host_text}"
                + ("　（你是房主）" if state.is_owner else "")
                + f"\n{tr('你的名字：')}{own_name}"
            ),
            fg=host_color,
        )

        # 在房间里：mc_port / max_players 按设计锁死（房间参数锁定是已验收的行为）。
        # 这里跟着房间状态同步，是为了**退出房间那一刻**同一个函数也能把它解开
        # —— 见上面 `not has_room` 分支的说明（ISSUES 问题 4）。
        self._sync_config_buttons()

    # --------------------------------------------------------
    # 3.0.9: 房间成员列表 + 踢人
    # --------------------------------------------------------

    def on_show_members(self):
        """「房间成员」按钮：打开（或前置）成员列表窗口。"""
        self._open_member_window()

    def _open_member_window(self):
        """
        打开成员列表窗口。已经开着就把它抬到前面，不重建。

        刷新是**就地改文字**（见 _render_member_rows）：不销毁、不重建控件，
        所以列表不会闪，也不会因为刷新而抢走键盘焦点。
        """
        win = getattr(self, "member_window", None)

        if win is not None:
            with contextlib.suppress(Exception):
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    self._refresh_member_list(force=True)
                    return

        win = tk.Toplevel(self.root)
        win.title(tr("房间成员"))
        win.configure(bg=BG)
        # 多了一列「对端延迟」（8 个数据列 + 一个踢出按钮），窗口跟着放宽一点。
        win.geometry("1030x380")
        win.minsize(860, 240)

        # 刻意 **不** 调用 grab_set()：那会把主窗口的输入抢走，用户就不能一边
        # 看成员一边操作主界面了。列表刷新也从不碰焦点。

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 6))

        tk.Label(
            head,
            text=tr("房间成员列表（房间里的人，实时更新）"),
            bg=BG,
            fg=FG_DIM,
            font=FONT_SMALL,
            anchor="w",
        ).pack(side="left")

        self.member_hint = tk.Label(
            head, text="", bg=BG, fg=FG_MUTED, font=FONT_SMALL, anchor="e"
        )
        self.member_hint.pack(side="right")

        self.member_table = tk.Frame(win, bg=CARD)
        self.member_table.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        # ============================================================================
        #  ★ 用户 2026-09-29（第三轮）第 2 条：有「踢出」按钮时每行的列是错位的
        # ============================================================================
        #
        # 根因：表头和**每一行**以前各是一个独立的 `tk.Frame`，各自带一套 9 列
        # 的 grid。tkinter 的列宽是**每个 grid 各算各的**：列宽 = 该列请求宽度 +
        # 按 weight 平分的剩余空间。于是只要某一行的请求宽度和别人不一样
        # （对端名字长一点、或者那一格是「踢出」按钮而别人是等宽占位 Label
        # —— Button 和 Label 的请求宽度本来就不一样），它那一行的 9 个格子
        # 就会整体平移，和上下行对不上。
        #
        # 修法：**所有行 + 表头共用 `member_table` 这一套 grid**（一行 = 一个
        # row 号，列号就是 MEMBER_COLUMNS 的下标）。列宽只有一个来源，
        # 物理上不可能错位。表头文字两边不再留 padx，底色就是连续的一条。
        for index, (key, title, width) in enumerate(MEMBER_COLUMNS):
            tk.Label(
                self.member_table,
                text=tr(title),
                bg=CARD_ALT,
                fg=FG_DIM,
                font=FONT_SMALL,
                width=width,
                anchor="w" if key in ("name", "addr", "nat") else "center",
            ).grid(row=0, column=index, sticky="ew", padx=0, pady=4)

        for index in range(len(MEMBER_COLUMNS)):
            self.member_table.grid_columnconfigure(index, weight=1)

        self.member_window = win
        self.member_rows = {}
        #: 每个成员当前显示的名字（踢人确认要用它，而不是 client_id）。
        self.member_names_by_key = {}
        self._member_list_filling = False

        win.protocol("WM_DELETE_WINDOW", self._close_member_window)

        self._refresh_member_list(force=True)

    def _close_member_window(self):
        """关掉成员列表窗口并把引用清干净（下次打开是全新的一扇窗）。"""
        win = getattr(self, "member_window", None)

        self.member_window = None
        self.member_table = None
        self.member_hint = None
        self.member_rows = {}
        self.member_names_by_key = {}

        if win is not None:
            with contextlib.suppress(Exception):
                win.destroy()

    def _member_row_widgets(self, key, index):
        """
        造一行（每个数据列一个格子 + 踢出按钮），并记在 self.member_rows[key] 上。

        ============================================================================
          ★ 一行就是 `member_table` 里的一行（不再有每行自己的 Frame）
        ============================================================================
        见 `_open_member_window` 里那段说明：每行一个独立 grid = 列宽各算各的 =
        有「踢出」按钮时上下行的列对不上（用户 2026-09-29 第三轮第 2 条）。
        现在所有格子直接 grid 进 `member_table`，列宽只有一个来源。

        之后每次刷新只调 `var.set(...)` / `grid_configure(row=…)`，控件一个都不
        重建 —— 这是"不闪"的全部秘密，也是不抢焦点的原因。
        """
        cells = {}
        vars_ = {}

        for column, (name, _title, width) in enumerate(MEMBER_COLUMNS):
            if name == "kick":
                continue

            var = tk.StringVar(value="—")
            vars_[name] = var

            label = tk.Label(
                self.member_table,
                textvariable=var,
                bg=CARD,
                fg=FG,
                font=FONT_SMALL,
                width=width,
                anchor="w" if name in ("name", "addr", "nat") else "center",
            )
            label.grid(row=index + 1, column=column, sticky="ew", padx=2, pady=3)
            cells[name] = label

        # 踢出按钮 + 同宽的占位（不允许踢时用占位保持列对齐，绝不显示空按钮）。
        kick = self._button(
            self.member_table, "踢出", lambda target=key: self.on_kick_member(target),
            width=6, secondary=True,
        )

        spacer = tk.Label(self.member_table, text="", bg=CARD, width=6)

        widgets = {
            # `frame` 这一格保留成"这一行的稳定对象"（测试用它判断刷新有没有
            # 重建控件）：现在没有行 Frame 了，用第一格标签代替。
            "frame": cells.get("name"),
            "cells": cells,
            "vars": vars_,
            "kick": kick,
            "spacer": spacer,
            "kick_column": len(MEMBER_COLUMNS) - 1,
            "kick_visible": None,
        }

        self.member_rows[key] = widgets

        return widgets

    def _render_member_rows(self, rows):
        """
        把 build_member_rows() 的结果画进表格。

        ============================================================================
          不闪 / 不抢焦点 / 没有空行
        ============================================================================
          * 控件按成员 key **复用**：见过的成员只改文字、只重排 grid 行号；
          * 走了的成员才销毁它那一行的控件（不会留下空白行）；
          * `_member_list_filling` 守卫：刷新期间被重入时直接返回，和房间列表
            的 `_room_list_filling` 是同一个教训（程序化重建触发了本该只属于
            用户操作的回调）。
        """
        table = getattr(self, "member_table", None)

        if table is None:
            return

        with contextlib.suppress(Exception):
            if not table.winfo_exists():
                return

        if getattr(self, "_member_list_filling", False):
            return

        self._member_list_filling = True

        try:
            wanted = [row.get("key") for row in rows]

            for key in list(self.member_rows):
                if key not in wanted:
                    widgets = self.member_rows.pop(key)
                    self.member_names_by_key.pop(key, None)

                    # 这一行的每一个格子（含踢出按钮 / 占位）都在
                    # `member_table` 里，必须逐个销毁 —— 没有行 Frame 可销毁。
                    with contextlib.suppress(Exception):
                        for cell in widgets["cells"].values():
                            cell.destroy()

                    for name in ("kick", "spacer"):
                        with contextlib.suppress(Exception):
                            widgets[name].destroy()

            for index, row in enumerate(rows):
                key = row.get("key")

                if not key:
                    continue

                widgets = self.member_rows.get(key)

                if widgets is None:
                    widgets = self._member_row_widgets(key, index)
                else:
                    # 行号重排：**每一格**都要跟着走（共用一套 grid）。
                    for cell in widgets["cells"].values():
                        with contextlib.suppress(Exception):
                            cell.grid_configure(row=index + 1)

                    for name in ("kick", "spacer"):
                        widget = widgets.get(name)

                        if widget is not None and widget.winfo_manager() == "grid":
                            with contextlib.suppress(Exception):
                                widget.grid_configure(row=index + 1)

                ping = row.get("ping_ms")
                # 用户名永远有值（中继真名 / 回退 client_id）；再兜一层 key，
                # 保证这一格**不可能是空的或「未知」**。
                name_text = (
                    row.get("name") or row.get("key") or tr("未知")
                )
                texts = {
                    "name": name_text,
                    # 身份 = 组合角色（`房主·主机` / `房主·玩家` / `玩家·主机` / `玩家`）；
                    # 老数据（没有 `roles`）退回单角色。
                    "role": (
                        roles_text(row.get("roles"))
                        if row.get("roles") else tr(role_text(row.get("role")))
                    ),
                    "status": _member_status_text(row),
                    # 拿不到就是 "-"：这里的每一格都必须是真实数据。
                    "nat": row.get("nat") or "-",
                    "inbound": row.get("inbound") or "-",
                    "addr": row.get("addr") or "-",
                    # 中转延迟：亚毫秒也标出来（审计问题 5），绝不写成 0。
                    "ping": p2p_rtt_text(ping),
                    # 对端延迟：适用才有数字，不适用 N/C（见 peer_latency_text）。
                    "peer_ping": peer_latency_text(
                        row.get("peer_rtt_ms"), row.get("peer_direct")
                    ),
                }

                # 踢人确认要用**这个成员显示的名字**（见 on_kick_member），
                # 所以在渲染时把当前这批名字记下来。
                self.member_names_by_key[key] = name_text

                for name, text in texts.items():
                    var = widgets["vars"].get(name)

                    if var is not None:
                        var.set(text)

                # 身份用颜色区分：房主橙、主机绿、其他灰（和别处一致）。
                role_colour = {
                    ROLE_OWNER: ROLE_COLORS.get("root", FG),
                    ROLE_HOST: OK,
                }.get(row.get("role"), FG)

                with contextlib.suppress(Exception):
                    widgets["cells"]["role"].configure(fg=role_colour)

                can_kick = bool(row.get("can_kick"))

                if widgets["kick_visible"] != can_kick:
                    with contextlib.suppress(Exception):
                        if can_kick:
                            widgets["spacer"].grid_forget()
                            widgets["kick"].grid(
                                row=0, column=widgets["kick_column"],
                                padx=2, pady=3,
                            )
                        else:
                            widgets["kick"].grid_forget()
                            widgets["spacer"].grid(
                                row=0, column=widgets["kick_column"],
                                padx=2, pady=3,
                            )

                    widgets["kick_visible"] = can_kick
        finally:
            self._member_list_filling = False

    def _refresh_member_list(self, force=False):
        """
        成员列表的一拍：数据来自引擎，渲染走 _render_member_rows。

        窗口没开时什么都不做（这是每秒钟都会被调到的函数）。
        """
        win = getattr(self, "member_window", None)

        if win is None:
            return

        try:
            if not win.winfo_exists():
                self._close_member_window()
                return
        except Exception:
            self._close_member_window()
            return

        state = self.engine.room_state()

        if state is None:
            with contextlib.suppress(Exception):
                self.member_hint.configure(text=tr("未加入房间"))

            self._render_member_rows([])
            return

        view = self._direct_status_view(self.last_snapshot or {})
        punch = self.engine.room_direct_state() or {}
        profile = (
            self.engine.room_nat_profile()
            or punch.get("nat_profile")
            or self._nat_profile
        )

        rows = build_member_rows(
            state=state,
            direct_rows=punch.get("links") or [],
            self_profile=profile,
            self_inbound_state=view.get("inbound_state") or "",
            self_ping_ms=self.engine.room_rtt_ms(),
            # ★ 每个对端自己广播的公网端点（DIRECT_ADDR，纯显示）：
            #   打洞实际用的地址优先，没有就显示这个。
            addr_rows=punch.get("addrs") or {},
            # ★ 全锥证据（对端首包证明）——纯展示，进「入站」那一档的判定。
            eif_proof=bool(punch.get("eif_proof")),
        )

        with contextlib.suppress(Exception):
            host = state.host_name if state.host_claimed else ""

            self.member_hint.configure(
                text=(
                    f"{state.display}  "
                    f"{tr('房间内')} {state.members}  "
                    f"{tr('主机')}: {host or '—'}"
                )
            )

        self._render_member_rows(rows)

    def on_kick_member(self, target):
        """
        踢出一个人。服务端是权威：这里只做"界面能点的"和"结果怎么说"。
        """
        target = (target or "").strip()

        if not target:
            return

        state = self.engine.room_state()

        # 界面第二道闸（第一道是按钮可见性）：即使状态在点之前变了也不乱发。
        if state is None or not (state.is_owner or state.is_host):
            with contextlib.suppress(Exception):
                messagebox.showwarning(
                    tr("踢人失败"),
                    tr("你没有踢人的权限（只有房主或当前主机可以）"),
                    parent=self.member_window or self.root,
                )
            return

        # 确认框里显示**这个成员的名字**（中继广播的真名 / 游客名），而不是
        # client_id：用户看不懂 cid。名字来自成员列表最近一次渲染。
        target_name = (
            (getattr(self, "member_names_by_key", {}) or {}).get(target)
            or target
        )

        if not messagebox.askokcancel(
            tr("踢出成员"),
            tr("确定要把「{0}」踢出房间吗？").replace("{0}", target_name),
            parent=self.member_window or self.root,
        ):
            return

        ok, message, code = self.engine.kick_member(target)

        if ok:
            self.log(
                f"member kicked: target={target} code={code}", kind="ok"
            )

            with contextlib.suppress(Exception):
                messagebox.showinfo(
                    tr("已踢出成员"),
                    tr("已踢出房主，房间已解散。")
                    if code == "OWNER_LEFT_ROOM"
                    else tr("该成员已被踢出房间。"),
                    parent=self.member_window or self.root,
                )
        else:
            self.log(
                f"kick failed: target={target} code={code} "
                f"message={message}",
                kind="warn",
            )

            # 服务端的原话就是中文的（describe_error 也兜了底），照原样显示。
            friendly = message

            if code == "KICK_FORBIDDEN":
                friendly = tr("你没有踢人的权限（只有房主或当前主机可以）")
            elif code == "KICK_NO_SUCH_MEMBER":
                friendly = tr("该成员不在房间里（可能已经离开）")

            with contextlib.suppress(Exception):
                messagebox.showwarning(
                    tr("踢人失败"), friendly,
                    parent=self.member_window or self.root,
                )

        self._refresh_room_ui()
        self._refresh_member_list(force=True)

    def _kick_reason_text(self, by, reason):
        """被踢的弹窗正文：由已翻译的固定句 + 中继给的数据拼成。"""
        # 中继的 reason 是给日志用的英文记号（kicked / owner_kicked）；界面上
        # 把它换成中文。认不出来的记号照原样显示（诚实：那是服务端给的）。
        known = {
            "kicked": tr("被踢出房间"),
            "owner_kicked": tr("房主被踢出（房间已解散）"),
        }

        lines = [tr("你已被踢出房间。")]
        lines.append(f"{tr('发起者：')}{by or tr('未知')}")

        if reason:
            lines.append(f"{tr('原因：')}{known.get(reason, reason)}")

        return "\n".join(lines)

    # --------------------------------------------------------
    # V2: account
    # --------------------------------------------------------

    def _account_buttons(self):
        return (
            (self.login_button, self.rooms_button),
            (self.logout_button, self.rename_button),
        )

    def _refresh_account_ui(self):
        """Render the login state. Called whenever self.auth changes."""
        session = self.auth

        if session is not None:
            # Colour the account line by ROLE, so "root" is unmistakable at a
            # glance. The same three colours are used in the panel.
            role = session.effective_role
            colour = ROLE_COLORS.get(role, OK)

            self.account_label.configure(
                text=tr(f"已登录：{session.summary()}"), fg=colour
            )
            self.account_hint.configure(
                text=(
                    f"服务器：{session.server}　"
                    f"协议版本：v{session.protocol}"
                    + (
                        f"　{session.self_ops_text()}"
                        if session.self_ops_text()
                        else ""
                    )
                ),
                fg=FG_MUTED,
            )
            self._set_role_badge(role)
            self.login_button.configure(
                text=tr("切换账号"), state="normal"
            )
            self.rooms_button.configure(state="normal")
            self.logout_button.configure(state="normal")
            self.rename_button.configure(state="normal")
        else:
            self._set_role_badge(None)
            self.account_label.configure(text=tr("未登录"), fg=WARN)
            self.account_hint.configure(
                text=tr(
                    "当前服务器要求登录后才能建房 / 进房。"
                    "点「登录」输入账号密码。"
                ),
                fg=FG_MUTED,
            )
            self.login_button.configure(text=tr("登录"), state="normal")
            self.rooms_button.configure(state="normal")
            self.logout_button.configure(state="disabled")
            self.rename_button.configure(state="disabled")

    def _set_role_badge(self, role):
        """
        Show the role as a coloured chip next to the account.

        Kept as its own widget rather than baked into the label text so the
        colour can change independently -- a role change (an admin promoting
        somebody) should be visible without the text moving.
        """
        badge = getattr(self, "role_badge", None)

        if badge is None:
            return

        if not role:
            # 未登录时也要占住这块地方：留空会在标题栏右侧出现一小块
            # 说不清是什么的空隙（用户的原话："空着一小块很难看"）。
            #
            # tr() 必须在这里加：这个函数是在 rebuild_ui() 里 **翻译整棵树之后**
            # 才被调用的（见 rebuild_ui 的顺序），不在这里翻的话，每次切语言
            # 这枚徽章都会被打回中文。
            with contextlib.suppress(Exception):
                badge.configure(
                    text=tr(" 未登录 "),
                    bg=CARD,
                    fg=FG_MUTED,
                )
            return

        with contextlib.suppress(Exception):
            badge.configure(
                text=f" {tr(ROLE_LABELS.get(role, role))} ",
                bg=ROLE_COLORS.get(role, ACCENT),
                fg="#101623",
            )

    def on_login(self):
        from .auth_ui import ask_login  # local import: app.py must not import
        # auth_ui at module level (auth_ui imports the theme constants below)

        reason = ""

        if self.auth is not None:
            reason = "切换账号会断开当前连接（如果正在运行请先停止）。"

        if self.running:
            messagebox.showinfo(
                "提示", "请先点「停止」，再切换账号。", parent=self.root
            )
            return

        # ★ M6/D10：登录要连中继。地址/端口没填时先给引导，不要把用户送进
        # 一个注定连不上的登录框（他会以为是账号问题）。
        try:
            host, port = self._require_server(show_error=True)
        except ValueError:
            self.log(
                "login refused: no relay address/port configured yet",
                kind="warn",
            )
            return

        session = ask_login(
            self.root,
            self.engine,
            host,
            port,
            username=self.config.username or self.username_var.get().strip(),
            client_id=self.client_id_var.get().strip(),
            reason=reason,
        )

        if session is None:
            return

        # 登录 = 游客那份临时身份就此作废（用户 2026-09-27 口径：换账号 /
        # 登录都销毁游客身份，下次连接由中继重新分配）。
        self._forget_guest_identity("an account signed in")

        self.auth = session

        # Keep the form in sync with the account that actually logged in.
        self.username_var.set(session.username)
        self.config.username = session.username

        if ":" in session.server:
            host, _, port = session.server.partition(":")
            self.host_var.set(host)
            self.port_var.set(port)

        self._refresh_account_ui()

        with contextlib.suppress(Exception):
            cfgmod.save_config(self._collect_config())

        # 登录时人**可能还在房间里**：名册里那颗名字必须跟着变，房间不重建
        # （用户要求，ISSUES 问题 5：身份绑设备）。失败只提示，不断连接。
        self._sync_identity()

        self.log(f"logged in: {session.summary()}", kind="ok")
        self.log(
            f"server {session.server}, protocol version v{session.protocol}",
            kind="dim",
        )

        # 剩余的自助修改次数（中继给的，客户端和面板共用同一个计数）。
        allowance = session.self_ops_text()

        if allowance:
            self.log(allowance, kind="dim")

        # ---- 上线状态上报 ----
        #
        # 面板的「在线账号」按真实连接统计，所以登录成功后要开一条常驻的
        # presence 连接：关软件（正常关或强杀）它就断，面板立刻不在线。
        # 旧版中继不认这条命令，会自动跳过，不影响任何联机功能。
        self.engine.start_presence(
            self.host_var.get().strip() or session.server.split(":")[0],
            self._port_or_default(),
            session,
            self.client_id_var.get().strip(),
            on_line=lambda text: self.log(text, kind="dim"),
        )

    def on_logout(self):
        if self.running:
            messagebox.showinfo(
                "提示", "请先点「停止」，再退出登录。", parent=self.root
            )
            return

        # ---- leaving the account means leaving its room ----
        #
        # A room's owner DOES NOT SURVIVE losing the account that owns it: the
        # relay records the owner from the token, so signing out while still
        # holding a room left an orphan behind -- the room stayed listed and
        # looked healthy, but it belonged to nobody and could not be deleted
        # from the client any more.
        #
        # So logging out now leaves the room first. That is also what the user
        # expects: "exit account" should not leave things running in the
        # background on a server they are no longer signed in to.
        room_state = self.engine.room_state()

        if room_state is not None:
            if not messagebox.askokcancel(
                "退出登录",
                f"你还在房间「{room_state.display}」里。\n\n"
                + (
                    "你是这个房间的所有者，退出登录会同时关闭该房间。\n"
                    if room_state.is_owner
                    else "退出登录会同时退出该房间。\n"
                )
                + "\n确定要退出登录吗？",
                parent=self.root,
            ):
                return
        elif not messagebox.askokcancel("退出登录", "确定要退出当前账号吗？"):
            return

        username = self.auth.username if self.auth else ""

        # 先把上线状态那条连接关掉：面板的「在线」就是它，主动登出之后
        # 不该等到 token 过期才消失。
        self.engine.stop_presence()

        if room_state is not None:
            # Release the host slot first so it frees immediately rather than
            # waiting for the relay's watchdog.
            if room_state.is_host:
                self.engine.release_host()

            self.log("leaving the room ...", kind="dim")
            self.engine.close_room()
            self._refresh_room_ui()

            self.log("left the room (logging out does not leave an unmanaged room behind)", kind="info")

        # Release the token on the relay too. Dropping it locally only would
        # leave the account showing as online in the panel until the token's
        # 12 hour TTL ran out.
        self.log("notifying the server of logout ...", kind="dim")
        released = self.engine.logout_session(
            self.host_var.get().strip(),
            self._port_or_default(),
            self.auth,
            self.client_id_var.get().strip(),
            timeout=8,
        )

        self.auth = None
        self._refresh_account_ui()

        # 登出 = 这次登录会话结束：游客身份随之销毁（用户 2026-09-27 口径）。
        # 放在 `self.auth = None` 之后，所以这次调用**一定**按"游客"算。
        self._forget_guest_identity("the account signed out")

        # 老版本可能把游客名写进了配置：现在清掉，免得下次连接"看起来还是同一个人"。
        if str(self.config.username or "").strip() not in ("", self.GUEST_NAME_PLACEHOLDER):
            self.config.username = self.GUEST_NAME_PLACEHOLDER
            self.username_var.set("")
            cfgmod.save_config(self.config)

        if released:
            self.log(f"logged out: {username} (the server is in sync)", kind="info")
        else:
            self.log(
                f"logged out: {username}"
                " (could not notify the server; it will expire automatically later)",
                kind="warn",
            )

    def on_rename(self):
        """
        Self-service rename.

        Deliberately allowed while a session is running: the relay patches the
        live room/session name and rebinds the token, so nobody is disconnected.
        """
        from .auth_ui import ask_rename  # local import, same reason as on_login

        if self.auth is None:
            messagebox.showinfo("提示", "请先登录账号再改名。", parent=self.root)
            return

        host = self.host_var.get().strip()

        if not host:
            messagebox.showerror("参数错误", "请先填写中转服务器地址", parent=self.root)
            return

        new_name = ask_rename(
            self.root,
            self.engine,
            host,
            self._port_or_default(),
            self.auth,
            self.client_id_var.get().strip(),
        )

        if not new_name:
            return

        # The engine patched the live session; the GUI only has to follow.
        self.username_var.set(new_name)
        self.config.username = new_name
        self._refresh_account_ui()

        with contextlib.suppress(Exception):
            cfgmod.save_config(self._collect_config())

        # 改名后房间里那颗名字也要立刻跟着变（同登录，见 _sync_identity）。
        self._sync_identity()

        self.log(f"renamed to: {new_name} (takes effect immediately; the connection is not dropped)", kind="ok")

    def require_login(self):
        """
        Return True when we may proceed; prompt for a login only if the
        RELAY says an account is required.

        This used to be a hardcoded local rule: no session -> always prompt.
        The consequence was that turning "必须登录" off in the admin panel had
        no visible effect at all, because the client never asked the server --
        it refused to start connecting and popped the login dialog on its own.
        The server side was working correctly the whole time.

        Now the client asks first (an unauthenticated ROOMS query carries the
        live switch), and only prompts when the answer is "yes" or "unknown".
        "unknown" means an unreachable or older relay, where prompting is the
        safe choice: a needless login prompt is a far smaller problem than
        letting the user connect and be refused.
        """
        if self.auth is not None:
            # Already signed in. Say so, and stop advertising a mode the
            # server may have left behind.
            self._set_account_mode_hint(True)
            return True

        host = self.host_var.get().strip()

        if not host:
            # Nothing to ask. Fall through to the prompt so the user gets a
            # usable error from the login dialog rather than silence.
            needs_account = True
            known = False
        else:
            needs_account = self.engine.relay_requires_account(
                host,
                self._port_or_default(),
                self.client_id_var.get().strip(),
            )

            known = needs_account is not None

            if needs_account is None:
                needs_account = True

        if not needs_account:
            self._set_account_mode_hint(False)
            return True

        # The server wants an account. Record that BEFORE prompting, because
        # the hint used to be written once and never corrected: after an admin
        # turned the switch back on, the account card kept claiming "服务器
        # 当前不要求登录" even while the login dialog was on screen demanding
        # one. Being told two opposite things at once is worse than being told
        # nothing.
        self._set_account_mode_hint(True, known=known)

        self.log("the server requires a login; showing the login window ...", kind="warn")
        self.on_login()

        return self.auth is not None

    def _set_account_mode_hint(self, requires_account, known=True):
        """
        Keep the account card's wording honest about the relay's live switch.

        Called on every attempt rather than once at startup, so a switch
        flipped in the panel is reflected the next time the user tries to
        connect -- instead of leaving a stale "不要求登录" on screen.
        """
        label = getattr(self, "account_hint", None)

        if label is None:
            return

        if self.auth is not None:
            # A signed-in account: the server's switch no longer matters for
            # this session, and saying "不要求登录" next to a logged-in user
            # was the confusing part.
            text = (
                f"服务器：{self.auth.server}　"
                f"协议版本：v{self.auth.protocol}"
            )
            colour = FG_MUTED
        elif requires_account:
            text = (
                "服务器要求登录后才能建房 / 进房。点「登录」输入账号密码。"
                + ("" if known else "（未能确认服务器状态，按需要登录处理）")
            )
            colour = FG_MUTED
        else:
            text = (
                "服务器当前不要求登录，可以直接建房 / 进房。\n"
                "需要账号时再点「登录」即可。"
            )
            colour = OK

        with contextlib.suppress(Exception):
            label.configure(text=text, fg=colour)

    # --------------------------------------------------------
    # 内嵌房间列表
    # --------------------------------------------------------

    def on_rooms(self):
        """刷新内嵌的房间列表（旧版这里是打开一个二级窗口）。"""
        self.on_rooms_refresh()

    def _refresh_room_list_soon(self):
        """
        立刻安排一次房间列表刷新（用于刚建房/刚进房/刚退出之后）。

        和 _tick 里的定时刷新是同一个入口；这里只是不等下一个周期，
        让用户点完按钮马上看到变化。
        """
        with contextlib.suppress(Exception):
            self.on_rooms_refresh()

    def on_rooms_refresh(self):
        """
        拉一次房间列表并填进主界面那张表。

        ============================================================================
          为什么是"自动刷新"而不是"点刷新"
        ============================================================================

        The list used to be a snapshot from whenever the user last pressed
        刷新: somebody releases the host position, the panel/room changes, and
        the table keeps showing the old row — which is how「释放了主機身份，
        列表还显示他在当主机」and「列表显示无人认领，其实位置还占着」both
        happened, in opposite directions.

        Now _tick refreshes it on a timer (ROOM_LIST_REFRESH_MS) whenever the
        window is visible and no request is already in flight. The 刷新 button
        still works; it just is not required any more.
        """
        # 本地导入：auth_ui 依赖本模块的主题常量，模块级导入会成环。
        from .auth_ui import AsyncTask

        host = self.host_var.get().strip()

        if not host:
            with contextlib.suppress(Exception):
                self.room_list_hint.configure(
                    text=tr("请先填写中转服务器地址")
                )
            return

        if self._room_list_busy:
            return

        self._room_list_busy = True
        self._room_list_fetched_at = time.monotonic()

        with contextlib.suppress(Exception):
            self.room_list_hint.configure(text=tr("正在获取 ..."))

        AsyncTask(
            self.root,
            self.engine.fetch_rooms,
            on_success=self._fill_room_list,
            on_error=self._room_list_failed,
        ).start(
            host,
            self._port_or_default(),
            self.client_id_var.get().strip(),
            self.auth.username if self.auth else "",
        )

    def _room_list_failed(self, message):
        self._room_list_busy = False

        with contextlib.suppress(Exception):
            self.room_list_hint.configure(
                text=tr(f"刷新失败：{message}")
            )

        # 自动刷新失败不要每次都刷屏：只在切换成失败时记一条。
        if not self._room_list_failed_once:
            self._room_list_failed_once = True
            self.log(f"failed to refresh the room list: {message}", kind="warn")

    def _fill_room_list(self, rooms):
        self._room_list_busy = False
        self._room_list_failed_once = False
        self.room_rows = list(rooms or [])

        # 刷新时保住用户已经选中的那一行（按房间 ID 找回），否则每次自动刷新
        # 都会把选择清掉，"选好了准备点加入"就白选了。
        keep = ""

        with contextlib.suppress(Exception):
            selected = self._selected_room_row()

            if selected is not None:
                keep = selected.room

        for item in self.rooms_tree.get_children():
            self.rooms_tree.delete(item)

        mine = (self.auth.username if self.auth else "") or ""

        for index, room in enumerate(self.room_rows):
            is_mine = bool(mine) and room.owner == mine

            if is_mine and room.players > 0:
                tag = "mine_busy"
            elif is_mine:
                tag = "mine"
            elif room.players > 0:
                tag = "busy"
            else:
                tag = "empty"

            self.rooms_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    room.display,
                    room.owner or "-",
                    room.host_text,
                    room.members,
                    room.players_text(),
                    room.state_text,
                ),
                tags=(tag,),
            )

        if keep:
            # 程序化重选只是为了让"已经选好的那一行"在刷新后还在，
            # **不能**让它触发 on_room_row_selected 去改写输入框 ——
            # 否则用户正在敲的房间名每几秒被覆盖一次（用户报的第 2 个问题）。
            self._room_list_filling = True

            try:
                for index, room in enumerate(self.room_rows):
                    if room.room == keep:
                        self.rooms_tree.selection_set(str(index))
                        break
            finally:
                self._room_list_filling = False

        count = len(self.room_rows)
        busy = sum(1 for r in self.room_rows if r.players > 0)

        with contextlib.suppress(Exception):
            self.room_list_hint.configure(
                text=(
                    f"共 {count} 个房间，{busy} 个有玩家（自动刷新，双击一行即可加入）"
                    if count
                    else "现在没有房间（先点「创建房间」）"
                )
            )

    def _selected_room_row(self):
        """列表里当前选中的那一行（RoomInfo），没选返回 None。"""
        with contextlib.suppress(Exception):
            selection = self.rooms_tree.selection()

            if selection:
                return self.room_rows[int(selection[0])]

        return None

    def on_room_row_selected(self, _event=None):
        """
        用户【真的】点中一行 = 把房间名填进输入框。

        ============================================================================
          为什么必须区分"用户点的"和"刷新替我们选的"（用户 2026-09-24 报的）
        ============================================================================

        用户的原话：单击选中一行之后**取消不掉**（Tk 的 Treeview 就是这样），
        而列表每几秒自动刷新一次；为了让"选好了准备点加入"不被刷掉，
        `_fill_room_list` 刷新后会**重新选中**原来那一行 —— 于是
        `<<TreeviewSelect>>` 又触发一次，房间名每几秒就被写回输入框一遍。
        结果：正在输入框里敲新房间名，刚打几个字就被整个覆盖掉。

        所以：
          * 刷新期间程序化的重选（`_room_list_filling`）**不写输入框**；
          * 用户正在输入框里打字时（焦点在输入框）**不写输入框**；
          * 只有"人在列表上真的点了一下"才写。
        """
        if getattr(self, "_room_list_filling", False):
            return

        room = self._selected_room_row()

        if room is None:
            return

        if self.engine.room_current() is not None:
            # 已经在房间里了：只提示，不动输入框（避免看起来像换房成功了）。
            return

        # 焦点在房间名输入框里 = 用户正在敲字，别覆盖他的输入。
        with contextlib.suppress(Exception):
            if self.root.focus_get() is self.room_entry:
                return

        with contextlib.suppress(Exception):
            self.room_var.set(room.display)

    def on_join_selected_room(self):
        """双击列表里的一行 = 加入那个房间。"""
        room = self._selected_room_row()

        if room is None:
            return

        self._on_room_chosen(room.display)

    def _on_room_chosen(self, room):
        """
        A room was picked in the room list.

        Selecting a room and ENTERING it are the same gesture on a double
        click: the whole point of picking one from the list is to go there.
        """
        self.room_var.set(room)
        self.log(f"selected from the room list: {room}", kind="info")

        if self.engine.room_current() is not None:
            messagebox.showinfo(
                "提示",
                "你已经在一个房间里了，请先点「退出房间」再换。",
                parent=self.root,
            )
            return

        self.on_join_room(room)

    # --------------------------------------------------------
    # 一键复制 Minecraft 连接地址
    # --------------------------------------------------------

    def on_copy_address(self):
        text = (self.addr_var.get() or "").strip()

        if not text or text == "—":
            self.log("no address to copy yet: click 'start connection' first.", kind="warn")
            messagebox.showinfo(
                "提示", "还没有连接地址。\n\n请先点「开始连接」。", parent=self.root
            )
            return

        if "127.0.0.1" not in text:
            self.log(
                f'the current address is "{text}", which is not a Minecraft '
                f"connection address (only player mode has a 127.0.0.1 address).",
                kind="warn",
            )
            messagebox.showinfo(
                "提示",
                "只有【玩家 Player】模式才有 Minecraft 连接地址。\n\n"
                "主机模式下这里显示的是本机世界端口，"
                "玩家连的是他们自己电脑上的 127.0.0.1 地址。",
                parent=self.root,
            )
            return

        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception as e:
            self.log(f"copy failed: {e}", kind="error")
            return

        # 绿色日志行就是"复制成功"的提示（不弹窗，不打断操作）
        self.log(f"copied the Minecraft connection address: {text}", kind="ok")

    def _update_address(self, snapshot):
        """
        刷新「Minecraft 连接地址」。

        玩家模式 -> 127.0.0.1:<本地代理端口>（真正要复制给 Minecraft 的地址）
        主机模式 -> 本机世界端口（仅提示，不是给玩家用的地址）
        未启动   -> —
        """
        role = snapshot.get("role")
        state = snapshot.get("state")

        if role == "player" and snapshot.get("local_endpoint"):
            self.addr_var.set(str(snapshot["local_endpoint"]))

        elif role == "host" and state == STATE_ONLINE:
            self.addr_var.set(
                f"本机世界 {snapshot.get('mc_endpoint', '')}"
            )

        elif not role:
            self.addr_var.set("—")

    def on_delete_my_room(self):
        """
        删除（关闭）自己的房间。

        Who may do this, and when -- the rules, in one place:

          * 房间的所有者 (owner) 或 持有主机身份的人 (host) —— the relay decides
            and refuses anybody else with ERROR NOT_HOST.
          * 房间里还有 Minecraft 玩家时，客户端拒绝。This is a safety rail for
            players, not a permission: the panel's 「强制关闭」 exists for when
            you really do need to end it with people inside.
          * 只有房间成员、还没有人进世界时，允许删除：成员会收到
            「ROOM REVOKED」而不是被当成掉线。

        It also has to work while the client is NOT connected to a world: being
        in a room without hosting is a normal state now, and leaving the button
        dead in that state is why 「客户端也没法删房间」.
        """
        # 删的必须是"我现在在的那个房间"，不是输入框里可能已经空掉的内容。
        room = self._active_room_name()

        if not room:
            messagebox.showerror(
                "还没有进入房间",
                "你现在不在任何房间里。\n\n"
                "要删除房间，请先创建或加入它。",
                parent=self.root,
            )
            return

        if not self.require_login():
            return

        # Players in the WORLD may come from the live session; when there is no
        # session, the room only holds members.
        snapshot = self.engine.snapshot()
        players = int(snapshot.get("players") or 0)

        if players > 0:
            messagebox.showwarning(
                "不能删除",
                f"房间 {room} 里还有 {players} 名玩家。\n\n"
                "请先让玩家退出 Minecraft，或者由服主在管理面板强制关闭。",
                parent=self.root,
            )
            return

        if not messagebox.askyesno(
            "确认删除",
            f"确定要删除房间 {room} 吗？\n\n"
            "删除后房间立即关闭，其他玩家将无法再加入。",
            parent=self.root,
        ):
            return

        self.delete_room_button.configure(state="disabled")
        self.root.update_idletasks()

        self.log(f"deleting room {room} ...", kind="warn")

        # Run off the GUI thread: the request may wait on the control socket.
        import threading

        def worker():
            try:
                if self.running:
                    result = self.engine.session_delete_room(room)
                else:
                    result = self.engine.delete_room(
                        self.host_var.get().strip(),
                        self._port_or_default(),
                        room,
                        self.auth,
                        self.client_id_var.get().strip(),
                    )
            except Exception as e:  # pragma: no cover - defensive
                result = (False, str(e))

            self.root.after(0, lambda: self._on_delete_room_result(result))

        threading.Thread(target=worker, name="delete-room", daemon=True).start()

    def _on_delete_room_result(self, result):
        ok, message = result

        self.delete_room_button.configure(
            state="normal" if self.running else "disabled"
        )

        if ok:
            self.log(message, kind="ok")
            messagebox.showinfo("删除成功", message, parent=self.root)
        else:
            self.log(message, kind="error")
            messagebox.showwarning("删除失败", message, parent=self.root)

    def _port_or_default(self):
        """
        输入框里的端口；没填/填错就是 **0**（= 没有端口）。

        ★ D10：这里**不再回退到一个写死的端口**。以前那个字面量就是"内置
        服务器端口"本身 —— 用户把端口清空之后，客户端会拿着它去连一个他
        根本没配的服务器。现在"没有端口"是一个明确状态，由
        `_require_server()` 在行动之前拦下来并给出引导。
        """
        try:
            return int(str(self.port_var.get()).strip())
        except (TypeError, ValueError):
            return 0

    def _require_server(self, show_error=True):
        """
        取"现在能用的 host/port"。缺任何一项 -> 抛 ValueError（带引导文字）。

        所有"要连服务器"的动作都从这里过：地址是用户填的，客户端不猜、也
        不内置任何服务器（D10）。`show_error=False` 时只抛不弹窗（给已经在
        错误处理里的调用方用）。
        """
        host = ""

        with contextlib.suppress(Exception):
            host = self.host_var.get().strip()

        port = self._port_or_default()

        if not host:
            if show_error:
                self._show_address_help()

            raise ValueError(cfgmod.SERVER_GUIDE)

        if not port:
            if show_error:
                messagebox.showwarning(
                    "还没填端口", cfgmod.SERVER_PORT_GUIDE, parent=self.root
                )

            raise ValueError(cfgmod.SERVER_PORT_GUIDE)

        return host, port

    # --------------------------------------------------------
    # Form events
    # --------------------------------------------------------

    def _on_mode_change(self):
        mode = self.mode_var.get()

        # ★ V5.0：AUTO 解锁。三个都是真实模式 —— Relay（中继）/ STUN（只走
        # 直连）/ AUTO（优先直连、不行回落中继）。没有"直接跳到某个模式"的
        # 暗箱行为：AUTO 的实际路径由数据面按同一条规则决定，模式本身始终是 auto。
        if mode not in (MODE_RELAY, MODE_STUN, MODE_AUTO):
            self.mode_var.set(MODE_RELAY)
            mode = MODE_RELAY

        # The ROOM's mode wins once we are in a room. Only the host may change
        # it, and changing it restarts every tunnel, so it goes through a
        # confirmation window (see _apply_room_mode).
        room_mode = self._room_conn_mode()

        if room_mode is not None and mode != room_mode:
            if not self._am_room_owner():
                # 非 Owner（**包括非 Owner 的 Host**）：把选择器弹回去并说清谁决定。
                #
                # NOTE: the "the host switched the mode, so rebuild OUR session"
                # work that used to live here was dead code (the tiles of a
                # non-host are disabled, AND `switched` was always False because
                # `_refresh_room_ui` writes `config.mode = room_mode` every
                # tick). It now lives on the per-tick sync path where it actually
                # runs: `_sync_session_mode()`.
                self.mode_var.set(room_mode)
                self.config.mode = room_mode
                self._refresh_tiles()
                mode_name = {
                    MODE_RELAY: "relay",
                    MODE_STUN: "STUN direct",
                    MODE_AUTO: "AUTO",
                }.get(room_mode, room_mode)
                self.log(
                    "the connection mode is decided by the host; currently "
                    f"{mode_name}",
                    kind="warn",
                )
            else:
                # Host: confirm, because this disconnects everyone.
                if not self._confirm_mode_switch(room_mode, mode):
                    self.mode_var.set(room_mode)
                    self.config.mode = room_mode
                    self._refresh_tiles()
                    return

                self._apply_room_mode(mode)

        if mode == MODE_STUN:
            self.mode_hint.configure(
                text=tr(
                    "直连模式：只尝试 P2P 打洞直连，游戏数据不经中转服务器。"
                    "打不通时不会自动改用中转 —— 需要联机请手动切回"
                    "「Relay 中转」，或改用「AUTO 自动」。"
                )
            )
        elif mode == MODE_AUTO:
            self.mode_hint.configure(
                text=tr(
                    "自动模式：优先 P2P 打洞直连；洞一时建不起来时，"
                    "这条连接自动改用中转服务器，不会卡住不让你进游戏。"
                    "直连一旦可用，之后的新连接会自动走直连。"
                )
            )
        else:
            self.mode_hint.configure(
                text=tr(
                    "中转模式：玩家 → 中转服务器 → 主机。"
                    "所有游戏数据经由中转服务器转发，兼容性最好。"
                )
            )

        # 选路只有一个事实来源：这里把决定写进配置，连接时读它。
        with contextlib.suppress(Exception):
            self.config.mode = mode

    def _on_role_change(self):
        """
        原来叫"身份切换"，现在**没有身份选择器了**（用户要求删除）。

        ============================================================================
          身份选择器为什么被整个删掉（用户原话）
        ============================================================================

        「把整个身份配置那个东西做成基准配置，把主机 host 和玩家 player 这个
          选择器直接移除掉，免得它去影响到操作那一栏。」

        它有三个真问题，全都指向"它本来就不该存在"：

          1. 真实身份由「谁持有主机位置」决定（见 _decide_connect_role），
             选择器却让人以为"选了主机就一定是主机"。
          2. 它会把另一个端口输入框藏起来，而"该用哪个端口"恰恰取决于真实身份
             —— 于是它间接影响了能不能连上（用户报的"依旧会影响开服"）。
          3. 默认值是 host，第一次点没反应，要来回点两次（用户报的）。

        现在这个函数只做一件事：**按"房间在不在"决定三个确定按钮能不能按**
        （用户要求：建房之后不许再改这些数字）。名字保持不变，因为
        `_refresh_room_ui()` 和界面重建流程都在调它。
        """
        self._sync_config_buttons()

    def _sync_config_buttons(self):
        """
        按"这个键的当前状态"刷新三个「确定」按钮 **和每行的反馈文字**。

        mc_port / max_players：房间建了就禁用（改了就掉线）。
        local_port：永远可用 —— 它只是一个本机监听端口，和隧道无关，
        房主也不用它，所以玩家随时能改。

        ============================================================================
          反馈为什么是"状态驱动"而不是"提交时写一句话"
        ============================================================================

        第一版在提交成功时把那一行改成「✓ 已生效：25565」—— 外观测试立刻抓到
        它是个**不稳定状态**：切语言/切主题会重建界面，重建后那行变回静态提示，
        于是"重建前后必须一模一样"这条断言就红了。

        状态驱动就没这个问题，而且信息更实在 —— 每一行随时都显示它此刻的真实
        状态，不管是刚启动、刚提交、还是改了一半：

            未提交：改了请按「确定」        （你改了但没提交）
            🔒 已锁定 —— …                  （房间已建，改不了）
            ✓ 已生效：25565                  （这个值就是现在会用的值）
            （静态说明）                      （还没填过）
        """
        with contextlib.suppress(Exception):
            for variable, _entry, button, _status, hint in getattr(
                self, "_config_rows", []
            ):
                key = self._config_key_of(variable)
                locked = self._config_locked(key)
                dirty = self._is_config_dirty(variable)

                self._set_enabled(button, not locked)
                button.configure(
                    bg=ACCENT if (dirty and not locked) else CARD_ALT
                )

                if dirty:
                    self._set_row_status(
                        key, "未提交：改了请按「确定」", kind="warn"
                    )
                elif locked:
                    self._set_row_status(
                        key,
                        "🔒 已锁定 —— 房间已建，要改请先删房重建",
                        kind="warn",
                    )
                elif self._committed.get(key):
                    self._set_row_status(
                        key,
                        "✓ 已生效：%s" % self._committed.get(key),
                        kind="ok",
                    )
                else:
                    self._restore_row_hint(key)

    def on_random_room(self):
        import random
        import string

        room = "mc" + "".join(
            random.choice(string.ascii_lowercase + string.digits)
            for _ in range(6)
        )
        self.room_var.set(room)
        self.log(f"generated a random room name: {room}", kind="dim")

    def on_reset_server(self):
        """
        「恢复默认」= 回到 **branding.json** 里的默认地址（D10）。

        ★ 这里原来写死的是作者的域名和端口 —— 那个按钮本身就是"客户端内置了
        一台真实服务器"的最后一块。现在默认值只有一个来源：安装器/开发者
        固化的 branding.json；它没给默认值时，按钮把两个框清空并显示引导
        （**空 = 没有默认服务器**，而不是"偷偷用某台机器"）。
        """
        host = cfgmod.default_server_host()
        port = cfgmod.default_server_port()

        self.host_var.set(host)
        self.port_var.set(port_text(port))
        self.config.server_host = host
        self.config.server_port = port
        self.config.server_source = "branding" if host else ""

        self._refresh_server_hint()

        if host and port:
            self.log(
                f"relay server restored to the branding default {host}:{port}",
                kind="dim",
            )
        else:
            self.log(
                "no branding default server is configured; the address field "
                "was cleared -- it has to be filled in by hand",
                kind="warn",
            )
            messagebox.showinfo(
                "没有默认服务器",
                "这个客户端没有内置默认的中继服务器"
                "（开源版不带任何真实服务器地址）。\n\n"
                "请填写部署者提供的地址与端口。",
                parent=self.root,
            )

    # --------------------------------------------------------
    # Actions
    # --------------------------------------------------------

    def _collect_config(self, role_override=None):
        """
        Build the config from the form.

        `role_override` is the CONNECTION role, decided by who holds the host
        position in the room (see _decide_connect_role). There is no longer any
        identity selector to disagree with it.

        ============================================================================
          端口只认「按过确定」的值（用户要求）
        ============================================================================

        用户原话：「每次修改的话都必须要按一下，就输入完新的之后不会立即生效，
        按右边的提交。」

        所以这里读的是 `_committed`（按过确定的那个数字），不是输入框里的字。
        输入框里可能停着一个还没提交的新端口 —— 那种值绝不能拿去建隧道，
        因为隧道是建房那一刻按这个数字建好的，用没提交的值建出来的隧道会
        和界面显示的对不上。
        """
        mc_port = self._committed_value(
            "mc_port", getattr(self.config, "mc_port", None)
        )
        local_port = self._committed_value(
            "local_port", getattr(self.config, "local_port", None)
        )

        if not mc_port:
            raise ValueError(
                "还没填「主机 Host：Minecraft 局域网端口」。\n\n"
                "请在上面填好，然后按右边的「确定」。"
            )

        if not local_port:
            raise ValueError(
                "还没填「玩家 Player：本地代理端口」。\n\n"
                "请在上面填好，然后按右边的「确定」。"
            )

        try:
            server_port = int(self.port_var.get())
        except (TypeError, ValueError):
            raise ValueError("中转服务器端口必须是数字")

        # 人数上限：超出就报错要求重填（而不是悄悄夹到极限值）
        #
        # Silently clamping was worse than an error: somebody typing 20 would
        # get a room for 8 and never be told why their friends could not join.
        max_players = self._committed_value(
            "max_players",
            getattr(self.config, "max_players", None)
            or proto.DEFAULT_MAX_PLAYERS,
        )

        if not (proto.MIN_MAX_PLAYERS <= max_players <= proto.DEFAULT_MAX_PLAYERS):
            raise ValueError(
                f"人数上限只能是 {proto.MIN_MAX_PLAYERS}-"
                f"{proto.DEFAULT_MAX_PLAYERS}，当前是 {max_players}。\n\n"
                "请在「基准配置」里改好，然后按右边的「确定」。"
            )

        # ---- 隧道数量跟随【房间】的配置，不跟随本机这个框 ----
        #
        # 这是用户报的那个 BUG：房间主人建房时填 8，但另一台机器当 host
        # 时本机填的是 3 —— 以前 max_tunnels 直接取本机这个框，于是只开
        # 3 条隧道；配对是"一条隧道配一个人"，第 4 个人永远配不上对，
        # 在 Minecraft 里就是连不上（房间却显示 8）。
        #
        # 现在：只要中继告诉了我们房间的容量，就以它为准。建房的人（owner）
        # 填的就是房间容量，两边天然一致；换谁当 host 都开一样多。
        # ============================================================================
        #  身份**必须**由调用方给出，这里不再有静默默认
        # ============================================================================
        #
        # 原来这里是 `role_override or "player"`。那个默认值制造了审计问题 3：
        # `_rebuild_tunnels()` 忘了传 role_override，于是房主切模式时被静默地
        # 重建成了 Player —— 房间瞬间没有 host，所有人都 NO_HOST。
        #
        # 现在：连接路径（开始连接 / 重建 / 自动重建）必须显式传一个已决定的
        # 身份；只有"纯粹为了保存表单值"的调用（登录成功、改名、退出时持久化）
        # 才允许省略，那种情况走同一个判定函数（拿不到房间就用上次用过的身份）。
        # 缺身份不再是静默降级，而是一行错误日志 + 响亮的报错。
        if role_override not in ("host", "player"):
            if role_override is None:
                role_override = self._safe_connect_role()
            else:
                message = (
                    "internal error: invalid connection role %r; refusing to "
                    "guess" % (role_override,)
                )
                self.log(message, kind="error")
                raise ValueError(message)

        connect_role = role_override
        room_capacity = self._room_capacity()

        if room_capacity and connect_role == "host":
            max_players = room_capacity

        self.config.server_host = self.host_var.get().strip()
        self.config.server_port = server_port

        # ★ M6：这个地址是**用户亲手填进框里**的（能走到这里说明他至少按过
        # 开始/确定），标记成 "user"。迁移逻辑靠这个标记区分"用户确认过的
        # 地址"和"老版本内置的默认值"——后者必须被清掉并要求重新确认。
        if self.config.server_host:
            self.config.server_source = "user"

        self.config.username = self.username_var.get().strip()
        self.config.client_id = self.client_id_var.get().strip()
        self.config.role = connect_role
        # 连接用的是"我现在真正在的房间"，不是输入框里残留的文字。
        # 输入框在进房成功后会被清空，读它就会得到「房间不能为空」。
        self.config.room = self._active_room_name()
        self.config.mc_port = mc_port
        self.config.local_port = local_port
        self.config.max_players = max_players

        # max_tunnels is DERIVED in validated(); assigning it here as well
        # would be a second source of truth, which is how the two drifted
        # apart in the first place.

        return self.config.validated()

    def on_test_server(self):
        # ★ D10：没有内置服务器。地址/端口缺一项就给引导（不是"参数错误"），
        # 因为"还没填"是正常的首次使用状态。
        try:
            host, port = self._require_server(show_error=True)
        except ValueError:
            self.log(
                "relay probe refused: no server address/port configured yet",
                kind="warn",
            )
            return

        # 用户主动填了地址并按下测试 = 这个地址是他确认过的，落盘时按 "user"
        # 记住（迁移逻辑据此区分"用户填的"和"老版本内置的"）。
        with contextlib.suppress(Exception):
            self.config.server_host = host
            self.config.server_port = port
            self.config.server_source = "user"

        self.server_status_label.configure(
            text=tr("● 测试中 ..."), fg=WARN
        )
        self.log(f"testing {host}:{port} ...", kind="info")

        self._set_enabled(self.test_button, False)

        # Never block the GUI thread on a socket.
        import threading

        def worker():
            try:
                ok, message, rtt = self.engine.test_relay(host, port)
            except Exception as e:  # pragma: no cover - defensive
                ok, message, rtt = False, f"测试失败：{e}", None

            self.root.after(0, lambda: self._on_test_result(ok, message, rtt))

        threading.Thread(target=worker, name="relay-probe", daemon=True).start()

    def _on_test_result(self, ok, message, rtt):
        self._set_enabled(self.test_button, True)

        if ok:
            self.server_status_label.configure(
                text=tr(f"● 服务器正常 {rtt:.0f} ms")
            if rtt
            else tr("● 服务器正常"),
                fg=OK,
            )
            self.log(message, kind="ok")
        else:
            self.server_status_label.configure(
                text=tr("● 服务器不可用"), fg=BAD
            )
            self.log(message, kind="error")

    # --------------------------------------------------------
    # 网络诊断（V3，只读）
    # --------------------------------------------------------

    # --------------------------------------------------------
    # 连接模式：房主决定 + 重建所有隧道
    # --------------------------------------------------------

    def _room_state_obj(self):
        """The live RoomState, or None when not in a room."""
        try:
            return self.engine.room_state()
        except Exception:
            return None

    def _room_conn_mode(self):
        """The room's mode as the RELAY reports it, or None when not in a room."""
        state = self._room_state_obj()

        if state is None:
            return None

        mode = getattr(state, "conn_mode", None)

        return mode if mode in (MODE_RELAY, MODE_STUN, MODE_AUTO) else None

    def _sync_room_nat(self):
        """
        Start / stop the live NAT measurement + punch to match what the user is
        DOING, not just where they are standing.

        ============================================================================
          3.0.3：触发点是【点「开始连接」】，不是进房
        ============================================================================

        The owner's rule, repeated many times and now followed literally:

            clicking 开始连接 = 开始打洞

        The direct link is a long-lived NETWORK channel between the host and
        each player -- it exists while the host is running and is completely
        independent of any Minecraft connection or any other process. It must
        NOT wait for a game session to start punching, and it must NOT die when
        a game session ends.

        So the switch is:

            active = (room mode is STUN) AND (a session is running)

        Clicking 停止 stops the session, and with it the punch and every channel
        this side holds: on the host that is every player's channel; on a player
        it is only that player's own -- which is exactly the required lifetime
        ("host 断开 → 通道全关；玩家断开 → 只断自己那条").

        The reason it is safe to key off the GUI's `running` here: `on_start`
        and `on_stop` both end in `_refresh_room_ui()`, and `_tick` re-runs this
        every second, so the switch follows the button within one UI tick.
        """
        # ★ V5.0：AUTO 也要打洞。房间级打洞/测量对 STUN 和 AUTO 是同一条路 ——
        # 区别只在数据面"等不到洞时怎么办"（STUN 拒绝会话，AUTO 回落中继）。
        punch_room = self._room_conn_mode() in (MODE_STUN, MODE_AUTO)

        # ONE entry point, and it starts BOTH room-level jobs:
        #
        #   * the live NAT measurement (RoomNatMonitor), and
        #   * the punch (RoomDirectLink) -- on 开始连接, not on game traffic.
        #
        # It used to be two calls into two different implementations
        # (`watch_room_punch` and `watch_room_direct`), which meant two rival
        # punch loops fought over the same peer: 41 punches and 13 "ROOM LINK
        # UP" in one of the owner's sessions, with the player disconnected by
        # the relay four times. One owner per room-level job, or they fight.
        # ==================================================================
        #  ⑤ 模式一变就把房间级任务**整个重来**一遍
        # ==================================================================
        #
        # 用户 2026-09-25 报的："中转换 STUN 再 STUN 换中转回去，切换连接会导致
        # 隧道出现问题，切回 STUN 时日志出现拒绝连接 —— 明显还是走了中继。"
        #
        # 换模式时那些 carrier / 打洞链路是按**旧模式**的本地后端和端口建起来的
        # （`set_room_direct_backend` 指的还是上一个会话的地址），复用它们必然
        # 出现"界面说 STUN、数据走旧路"的撕裂。所以模式一变就全部关掉重建：
        # 下面那句 watch_room_nat(True) 会把它们按新配置重新拉起来。
        #
        # 只在**模式真的变了**时做（`_punch_mode`），否则每秒一次的刷新会把刚
        # 打好的洞反复关掉。
        mode_now = self._room_conn_mode()

        if mode_now is None:
            # ============================================================================
            #  ★ 用户 2026-09-29 第 7 条：房间模式**未知**绝不等于 relay
            # ============================================================================
            # 旧代码是 `self._room_conn_mode() or MODE_RELAY`：只要那一秒广播里没有
            # `mode=`（换模式正在拆旧通路 / 老中继 / 房间状态抖动），本函数就会把
            # `_punch_mode` 从 stun 改成 relay，紧接着 `watch_room_direct(False)`
            # **把刚打通的直连整个拆掉** —— 用户看到的就是"直连玩一会儿游戏断开、
            # 模式自己跳到中转（没有任何人操作）"。
            #
            # 现在：模式未知 **什么都不做**，保持上一次已知的模式（宁可不动作）。
            # `_punch_mode` 只会因为两种事实而改变：中继明确广播的 `mode=`，或者
            # Owner 的显式操作（`_apply_room_mode`）。客户端**绝不会**自己回退到
            # relay。
            mode_now = getattr(self, "_punch_mode", None)

        if mode_now is not None and mode_now != getattr(self, "_punch_mode", None):
            self._punch_mode = mode_now
            self.log(
                f"connection mode is now {mode_now}; rebuilding the room-level "
                f"NAT/punch work from scratch",
                kind="warn",
            )

            with contextlib.suppress(Exception):
                self.engine.watch_room_direct(False, log=self.log)

        # EXCEPTION SAFETY: `_render_hole_rows` reads `punching`/`ready` from the
        # room punch, so an exception here must not abort `_refresh_room_ui`
        # before that line -- a hole row would then keep claiming 打洞中 in relay
        # mode (audit problem 2). Never let a mode-sync failure hide the truth.
        #
        # `standalone_host`：只把中继地址**记下来**给冷启动用，它**不再**是启动
        # 测量的理由 —— 设计基准 §E（2026-09-27 用户逐条确认）明确推翻了那次
        # "打开软件就测"：「测量是在点『开始连接』之后按这个顺序做的（不是打开
        # 软件就测）」。唯一的上游开关就是第一个参数
        # （`punch_room and self.running` = 用户真的点了开始连接）。
        with contextlib.suppress(Exception):
            self.engine.watch_room_nat(
                bool(punch_room and self.running),
                log=self.log,
                standalone_host=getattr(self.config, "server_host", "") or "",
            )

    # --------------------------------------------------------
    # 会话模式同步（非房主的热切换自动重建）
    # --------------------------------------------------------

    #: 两次"自动重建"之间至少隔这么久。重建 = 停会话 + 起会话，玩家会掉线
    #: 重连一次，绝不能每秒来一遍；同时它也不是"只做一次"就完了 —— 万一这次
    #: 重建失败了，几秒后还要允许再试。
    SESSION_MODE_REBUILD_COOLDOWN_S = 5.0

    def _sync_session_mode(self):
        """
        会话真正在用的模式 != 房间当前模式时，自动重建一次（带防抖）。

        ============================================================================
          为什么必须看 `session_mode()`，而不是 `config.mode`
        ============================================================================
        模式是在**会话创建那一刻**冻进 core 的（`session.py` 里的 config.mode）。
        房间热切换只改中继的广播和界面，正在跑的 core 不会自己变。而
        `_refresh_room_ui` 每秒都把 `self.config.mode` 同步成房间的模式，所以
        "config 和房间不一致"这个判据永远是假的 —— 只有"会话真正在用的模式"
        才是事实（审计问题 3/4：非房主侧那条自动重建是死代码）。

        防抖用 [冷却时间 + 目标模式] 而不是"只做一次"：
          * 冷却挡住每秒一次的刷新把重建打成死循环；
          * 重建失败（会话没起来 / 又断了）时，几秒后还允许再试一次，
            不会把用户永久卡在"旧模式"里。

        已经有一个重建在跑（`_rebuild_in_flight`）时绝不叠加第二次：审计的
        log2 里两次 teardown 交叠（33ms 内停旧起新，旧 host 还在收尾）。
        """
        delta = self._session_mode_rebuild_delta()

        if delta is None:
            return

        session_mode, room_mode = delta

        self._last_session_mode_rebuild_at = time.time()
        self._session_mode_rebuild_target = room_mode

        self.log(
            f"this session is still on the {session_mode} mode while the room "
            f"is now {room_mode}; rebuilding this connection to match "
            f"(automatic: every client rebuilds its own side; nobody has to reconnect by hand)",
            kind="warn",
        )
        # ★ 静默（用户 2026-10-02）：按钮不释放、状态栏正常，只悄悄重建。
        #   只设"下一次静默"标记 —— 真正武装在 `_rebuild_tunnels` 里（否则一次被
        #   挡回的重建就会把界面永久卡在"在连接"，见 2026-10-03 致命回归）。
        self._mode_rebuild_silent_next = True
        self._rebuild_tunnels(mode=room_mode)

    def _maybe_rebuild_for_mode_change(self):
        """
        中继推过 `CONN_MODE_CHANGED` 之后的**主动重建**（用户第 6 条 P0）。

        ============================================================================
          为什么要有这一条，而不是只靠 `_sync_session_mode()`
        ============================================================================
        `_sync_session_mode()` 的判据是"会话真正用的模式 != 房间当前模式"。这
        在"会话还活着"时是对的，但它漏掉一种：中继换模式时会把旧通路**拆掉**
        （`disconnect_game_connections`），会话可能已经停在 `session_mode()==''`。
        那时代码里 `if not session_mode: return None` 直接放弃 —— 界面还在
        "运行中"，但不会自己去按新模式重建，只能等自动重连的退避（慢，而且
        用户看到的是"热切换不能用了"）。

        这里补的正是那一条：只要 `conning_mode_changed` 打过标记、房间广播里的
        **新模式已经到了**，就立刻按新模式重建一次（`_rebuild_tunnels` 自己带
        防重入）。会话已经在新模式上 -> 清标记，不折腾。
        """
        if not getattr(self, "_mode_rebuild_pending", False):
            return False

        if not self.running or getattr(self, "_rebuild_in_flight", False):
            return False

        room_mode = self._room_conn_mode()

        if room_mode not in (MODE_RELAY, MODE_STUN, MODE_AUTO):
            # 广播还没到 / 不在房间：**等**，绝不拿旧模式去重建（那正是 bug）。
            return False

        # ★ P2（2026-10-03 分析）：中继先发 `CONN_MODE_CHANGED`、后发
        #   `HOST_STATE mode=<新>`。如果此刻房间模式**还是切换前那个**，说明
        #   新模式还没到 —— 继续等，**不要**清标记、更不要拿旧模式重建
        #   （旧代码就在这里把标记清了，host 于是永远不重建）。
        if room_mode == getattr(self, "_mode_rebuild_from", None):
            return False

        try:
            session_mode = str(self.engine.session_mode() or "")
        except Exception:
            session_mode = ""

        if session_mode == room_mode:
            # 会话已经是新模式了（比如 `_sync_session_mode` 刚重建完）：不重复。
            self._mode_rebuild_pending = False
            return False

        self._mode_rebuild_pending = False
        self._last_session_mode_rebuild_at = time.time()
        self._session_mode_rebuild_target = room_mode

        self.log(
            f"the relay announced a mode change and the room now reports "
            f"{room_mode} (this session is on {session_mode or 'none'}); "
            f"rebuilding every tunnel for the new mode",
            kind="warn",
        )
        self._mode_rebuild_silent_next = True
        self._rebuild_tunnels(mode=room_mode)

        return True

    def _session_mode_rebuild_delta(self):
        """
        (会话模式, 房间模式) 当且仅当"该自动重建一次"时；否则 None。

        拆成一个纯判定函数，回归测试才能直接钉住"只请求一次，不会每次都请求"
        （见 tests/room_host_session_test.py）。
        """
        if not self.running or getattr(self, "_rebuild_in_flight", False):
            return None

        room_mode = self._room_conn_mode()

        if room_mode not in (MODE_RELAY, MODE_STUN, MODE_AUTO):
            return None

        try:
            session_mode = str(self.engine.session_mode() or "")
        except Exception:
            return None

        # ★ 2026-10-03：P5（"session_mode 为空也重建"）**先不加** —— 它会让
        #   "会话一没就重建"，在用户主动停止 / 测试桩里都会立刻起一次重建，
        #   把 `_rebuild_in_flight` 卡住（回归网直接抓到）。模式切换那条路已经由
        #   P2（等新模式到）覆盖；需要 P5 时再连测试一起做。
        if not session_mode or session_mode == room_mode:
            return None


        now = time.time()

        # 同一个目标模式刚重建过 -> 让它先跑完；冷却过去之后允许重试一次。
        if (
            getattr(self, "_session_mode_rebuild_target", None) == room_mode
            and now - getattr(self, "_last_session_mode_rebuild_at", 0.0)
            < self.SESSION_MODE_REBUILD_COOLDOWN_S
        ):
            return None

        return session_mode, room_mode

    def _report_started(self, connecting):
        """
        把"我点了开始连接 / 我停了"报给中继（`STARTED <1|0>`，PROTOCOL §8.6）。

        ============================================================================
          ★ 用户 2026-09-28 第 5 条：房间里有人 ≠ 他已经开始连接
        ============================================================================

        中继的 `started=` 原来只按**连接**现算，而 STUN 模式是"先点开始打洞、
        游戏连接是之后的事" —— 只按连接算的话 Host 拉不到已经点了开始的玩家
        （见 `punch_flow.beacon_line` 的 `from=` 兜底）。显式上报把用户意图说清楚。

        **同一状态只发一次**（用户/协调者口径）：`_started_reported` 记住上次的
        值；进新房间时在 `room_joined` / `_teardown_room_locally` 里复位。

        放在后台线程：老中继会忽略这一行（等到超时），**绝不**让它冻住界面。
        """
        connecting = bool(connecting)

        if getattr(self, "_started_reported", None) == connecting:
            return False

        self._started_reported = connecting

        self.log(
            f"[room] reporting STARTED {1 if connecting else 0} to the relay "
            f"(I {'started' if connecting else 'stopped'} connecting)"
            + (
                ""
                if hasattr(self.engine, "room_set_started")
                else " (not supported by this build)"
            ),
            kind="dim",
        )

        engine = getattr(self, "engine", None)

        if engine is None or not hasattr(engine, "room_set_started"):
            return False

        def worker():
            with contextlib.suppress(Exception):
                engine.room_set_started(connecting)

        import threading

        threading.Thread(
            target=worker, name="started-report", daemon=True
        ).start()

        return True

    def _am_room_host(self):
        state = self._room_state_obj()

        return bool(state is not None and getattr(state, "is_host", False))

    def _am_room_owner(self):
        """
        我是不是房间的 **Owner**（建房的人）。

        设计基准 §A / AGENTS.md 规则 6：**只有 Owner 能切连接模式**；Host
        （非 Owner）无权。所以模式磁贴 / 提示的锁必须按 Owner 判，不能按 Host 判
        —— 按 Host 判会让一个非 Owner 的 Host 看到可点的磁贴，点下去被服务端
        `ERROR NOT_HOST` 拒掉（用户看到的是"能点但没反应"）。
        """
        state = self._room_state_obj()

        return bool(state is not None and getattr(state, "is_owner", False))

    def _confirm_mode_switch(self, old_mode, new_mode):
        """
        Second confirmation for the change that disconnects everyone.

        WHY IT IS NEEDED: switching transports restarts every tunnel, so every
        player in the world drops and has to reconnect. That is a small cost
        (they rejoin; nobody has to rebuild the room) but it is NOT something
        to do by mis-clicking a tile, so the host is asked explicitly and told
        what will happen.
        """
        label = {
            MODE_RELAY: "Relay 中转",
            MODE_STUN: "STUN 直连",
            MODE_AUTO: "AUTO 自动",
        }

        return messagebox.askokcancel(
            tr("切换连接模式"),
            tr(
                "将把房间的连接模式从「{0}」切换为「{1}」。\n\n"
                "切换会【重建全部隧道】：所有客户端会自动按新模式重连完成，"
                "正在游戏中的玩家不需要手动操作，当时空闲的成员保持空闲。\n\n"
                "确定要切换吗？"
            ).replace("{0}", tr(label.get(old_mode, old_mode)))
             .replace("{1}", tr(label.get(new_mode, new_mode))),
            parent=self.root,
        )

    def _apply_room_mode(self, mode):
        """
        Tell the relay, then rebuild every tunnel.

        WHY REBUILD: the transport is baked into each tunnel when it is
        established -- a relay tunnel is a TCP connection to the relay, a
        direct one is a punched UDP path. There is no in-place conversion, so
        the honest move is to drop them and let them come back on the new
        transport. The LOCAL side (proxy ports, Minecraft connection, room
        membership) is deliberately untouched: only the player-facing tunnels
        are rebuilt.
        """
        self.log(f"requested connection mode switch -> {mode}", kind="info")

        # ★ 用户 2026-10-02 现场：模式热切换后"客户端按钮状态被释放、但通道
        #   确实重建好了"，于是**下一次**切换因为 `self.running` 已经是 False
        #   而不再重建。根因是**同一个模式变化被重建了两次**：
        #     1. 本地这一次点击（`_apply_room_mode` -> `_rebuild_tunnels`）；
        #     2. 中继随后广播 `CONN_MODE_CHANGED` -> `_mode_rebuild_pending`
        #        -> 房间状态一到，`_maybe_rebuild_for_mode_change()` **又重建一次**。
        #   第二次重建的 stop 会把按钮放掉（running=False），而第一次的
        #   `rebuild_done` 已经把它补回来过 —— 净效果就是用户看到的"通了但按钮
        #   是释放的"，并且下一轮切换被 `if self.running` 挡住。
        #
        #   这里把"切换前我到底在不在连接"在**改动之前**就定死（local 侧没有
        #   中继的 `was_live=` 那么硬的事实，但此刻的 running 就是真相），并在
        #   本地重建开始时**清掉待重建标记** —— 一次模式变化只重建一次。
        was_live = bool(self.running)

        ok, message = self.engine.set_conn_mode(mode)

        if not ok:
            self.log(f"mode switch failed: {message}", kind="error")

            # Snap back to what the room actually has.
            actual = self._room_conn_mode() or MODE_RELAY
            self.mode_var.set(actual)
            self.config.mode = actual
            self._refresh_tiles()

            return

        self.config.mode = mode

        # Remember it, so anything rebuilt later uses the new mode.
        with contextlib.suppress(Exception):
            cfgmod.save_config(self.config)

        self._refresh_tiles()

        if was_live:
            self.log(
                "rebuilding the connection tunnels for the new mode; every "
                "client reconnects automatically on the new mode (nothing to do by hand)",
                kind="warn",
            )

            # 一次模式变化只重建一次：中继的广播随后到，不必再重建一遍
            # （它只负责把房间模式对齐）。
            self._mode_rebuild_pending = False
            # 模式**显式传入**（见 `_rebuild_tunnels` 的 P0 说明：不能到工作线程
            # 里再读一个每秒都会被改的共享字段）。
            #
            # 注意：模式切换**不**走 `_force_rebuild_links()`（用户 2026-09-30
            # 第 1 条只讲"触发自动重连后的行为"）：换模式本来就是"严格重建全部
            # 通路"，由 `_rebuild_tunnels` 自己的重建完成，这里保持原行为。
            #
            # ★ 静默（用户 2026-10-02）：房主点模式切换时，**所有人**（包括
            #   房主自己）的连接按钮都不该被释放、状态栏也不该掉到"未连接"。
            self._mode_rebuild_silent_next = True
            self._rebuild_tunnels(mode=mode)

    def _force_rebuild_links(self):
        """
        ★ 用户 2026-09-30 第 1 条：**自动重连一律重建通路**，不再判断原通路
        是否可复用。

        * 我是 host  -> 丢掉**所有** peer 的洞（重建到每个玩家的通路）；
        * 我是 player-> 只丢掉**到 host** 的那一条（其他玩家完全不受影响）；
        * 我不在房间里 / 没有打洞协调器 -> 什么都不做。

        **只影响调用它的那一次重连**：空闲的人不会走到这里（自动重连只由
        "我原本在连接"触发），所以"处于未连接态的玩家保持空闲"天然成立。
        """
        state = None

        with contextlib.suppress(Exception):
            state = self.engine.room_state()

        if state is None:
            return []

        if getattr(state, "is_host", False):
            peers = None
        else:
            host = str(
                getattr(state, "host_id", "")
                or getattr(state, "host_name", "")
                or ""
            ).strip()
            peers = [host] if host else []

        with contextlib.suppress(Exception):
            dropped = self.engine.rebuild_room_direct_links(peers)

            if dropped:
                self.log(
                    "[reconnect] forced path rebuild: "
                    + (
                        "every peer (I am the host)"
                        if peers is None
                        else f"only {', '.join(dropped)} (I am a player)"
                    ),
                    kind="warn",
                )

            return dropped or []

        return []

    def _rebuild_tunnels(self, mode=None, force_links=False):
        """
        Restart the connection so every tunnel is re-established.

        Runs on its own thread: stop+start is blocking (up to ~15s + ~20s) and
        must not freeze the window. The GUI is re-enabled when it finishes, on
        the GUI thread via the event queue.

        ============================================================================
          角色必须在【这里】重新决定，不能沿用表单默认
        ============================================================================
        用户实测（审计问题 3）：房主切模式时，重建把会话建成了 **Player** ——
        房间当场失去 host，所有人 `NO_HOST`。根因是这里调 `_collect_config()`
        没传 `role_override`，而它默认 `"player"`。

        所以身份和 `on_start` 走**同一个**规则：谁持有主机位置，谁就是 host。

        ============================================================================
          ★ 模式必须在【调用这一刻】定死，不能到工作线程里再读 `config.mode`
        ============================================================================
        用户实测（2026-09-28 P0）：房主热切换之后"连不上、隧道好像没有重建"。
        根因是**竞态**：`config.mode` 是界面线程每秒都会改的一个共享字段
        （`_refresh_room_ui` 把房间模式写进去），而重建的工作线程在几毫秒之后才
        读它。切模式那一瞬间中继的 `HOST_STATE mode=` 还没到，界面线程完全可能
        把 `config.mode` 又写回**旧模式**，于是新会话按旧传输协议建立：
        房间写着 STUN、会话还在走中继（或反过来），表现就是"隧道没有重建、
        怎么都连不上"。

        所以这里在**调用线程**上把目标模式快照下来（`mode` 参数，或房间当前
        模式），工作线程用 `config.mode = 这个快照` 显式覆盖 —— 重建结果只由
        "调用那一刻的事实"决定，不受之后任何一秒刷新影响。
        """
        import threading

        # 已经有一个重建在跑：绝不叠加第二次（审计 log2 里两次 teardown 交叠）。
        # 手动点模式、房主切模式、非房主自动重建，三条路都走这里。
        # ★ 静默标记在这里就**消费**掉，不论这次重建是否真的发生（2026-10-03
        #   致命回归）：一次被挡回的重建绝不能把标记留着 —— 那会让**之后某次
        #   完全无关的重建**也变成静默，界面被永远按在"在连接"（按钮不释放、
        #   停止无效、新房间连不上）。真正武装静默在下面通过全部 guard 之后。
        silent_next = bool(getattr(self, "_mode_rebuild_silent_next", False))
        self._mode_rebuild_silent_next = False

        if getattr(self, "_rebuild_in_flight", False):
            self.log(
                "a connection rebuild is already running; not starting another",
                kind="warn",
            )
            return

        # 角色在起线程之前就定好（同一个判定函数，和 on_start 共用一条规则）。
        role = self._safe_connect_role()

        # 目标模式也在起线程之前定死（见上面那段：这是 P0 的修复点）。
        target_mode = str(
            mode or self._room_conn_mode() or getattr(self.config, "mode", "")
            or MODE_RELAY
        )

        # 让本函数返回之后，每秒一次的自动重建判定**等这一次跑完**再重新评估。
        if silent_next:
            # 真正开始重建了才武装静默窗口（并且它有硬上限，见事件泵里的兜底）。
            self._mode_switch_silent = True
            self._mode_switch_silent_until = (
                time.monotonic() + MODE_SWITCH_SILENT_MAX_S
            )

        self._rebuild_in_flight = True

        def worker():
            # ★ 用户 2026-09-30 第 1 条：自动重连**一律重建通路**。
            #   放在这里（工作线程、stop 之前）而不是调用方那侧：
            #   * 离开 Tk 线程，和打洞循环的其它改写点同一条线程；
            #   * 在"停会话"之前把洞丢掉，紧接着的新会话就会重新打洞 ——
            #     绝不复用旧通路。
            if force_links:
                self._force_rebuild_links()

            clean = True

            try:
                clean = self.engine.stop_session(timeout=15)
            except Exception as e:
                self.engine._push("log", text=f"停止旧连接失败：{e}")
                clean = False

            if not clean:
                # 旧连接的收尾超时了。引擎已经走 abort() 把 socket 全部关掉
                # （否则新连接会在同一个本地端口上绑失败 —— 用户报的那一条），
                # 所以这里继续，但要说出来。
                self.engine._push(
                    "log",
                    text="旧连接收尾超时，已强制关闭它的 socket（本地端口已释放）",
                )

            try:
                # Reuse the real form-reading path, so the rebuilt session has
                # exactly the settings the user last committed (ports included)
                # -- not a hand-made subset that could drift.
                #
                # `role_override` is mandatory here: rebuilding as the wrong
                # role is the "host turns into a player and the room dies" bug.
                config = self._collect_config(role_override=role)

                # ★ P0（2026-09-28）：模式用**调用那一刻**的快照覆盖。这一行
                # 就是"房主切模式后新会话还走旧传输"的根治点 —— 界面线程随后
                # 任何一次把 config.mode 写回旧值都不会影响这一次重建。
                config.mode = target_mode

                self.engine.start_session(config, auth=self.auth, timeout=25)

                self.engine._push("log", text="已按新的连接模式重建隧道")
                # 让界面线程去重排房间级任务（tk 变量只能在界面线程碰）。
                self.engine._push("rebuild_done")
            except Exception as e:
                # 失败必须把界面恢复成"已停止"，否则用户看到的是"还在运行、
                # 但用什么都不通"的半死状态 —— 那正是他报的另一半问题。
                self.engine._push(
                    "log", text=f"按新模式重建失败：{e}（可手动重新连接）"
                )
                self.engine._push("rebuild_failed", text=str(e))
            finally:
                self._rebuild_in_flight = False

        threading.Thread(target=worker, name="mode-switch", daemon=True).start()

    # --------------------------------------------------------
    # 身份同步（房间里登录 / 改名之后，名册里的名字要跟着变）
    # --------------------------------------------------------

    #: ★ 用户 2026-10-03（致命回归的兜底）：静默重建窗口**最长**这么久。

    #: 两次 IDENTITY 之间至少隔这么久（防抖：登录 + 改名 + 事件回调可能叠着来）。
    IDENTITY_COOLDOWN_S = 5.0

    #: 配置文件里代表"还没有真正起过名字"的占位（`settings.validated()` 的兜底）。
    #: 重启之后 `config.username` 是它 = 上一轮那个游客名没有被持久化下来。
    GUEST_NAME_PLACEHOLDER = "玩家"

    # ================================================================
    #  会话自动重连（用户 2026-09-27 批准，见 docs/AUTO-RECONNECT-PLAN.md）
    # ================================================================
    #
    # 触发 = **不是用户意图**的断开：中继 TCP 断 / 心跳超时 / 会话核心自己退出 /
    # 换模式重建失败。终态（停止 / 退房 / 被踢 / 关房 / 版本门 / 认证失效）
    # **永不重连**。
    #
    #: 退避节奏：首次 0.5s，之后 1→2→4→8→15s 封顶。
    RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)

    #: 总窗口：从第一次触发算起，超过就不再来（停下 + 弹窗一次）。
    RECONNECT_WINDOW_S = 60.0

    #: 自动重连撞上"另一次重建还在跑"时，最多让路几次（每次 0.5s）。
    #: 见 `_reconnect_session()`：等不到就让位给终态，绝不静默卡住。
    RECONNECT_DEFER_LIMIT = 40

    def _reconnect_state_text(self):
        """界面四态：运行中 / 正在自动重连 / host 没开服 / 停止（终态原因）。"""
        phase = getattr(self, "_reconnect_phase", "")

        if phase == "reconnecting":
            left = max(
                0.0,
                getattr(self, "_reconnect_next_at", 0.0) - time.time(),
            )
            return f"正在自动重连（第 {self._reconnect_attempt} 次，{left:.0f}s 后）"

        if phase == "waiting_host":
            return "host 未开服（等待 host 开始连接）"

        # host 主动断开/释放之后：**不自动重连**，等 host 回来后用户手动点。
        if phase == "host_down":
            return "host 已断开连接（不自动重连，可手动「开始连接」）"

        if phase == "stopped":
            reason = getattr(self, "_reconnect_stop_reason", "") or ""
            return f"停止（{reason}）" if reason else "停止"

        return "运行中"

    @staticmethod
    def _is_no_host_reason(reason):
        """
        这个原因是不是"房间里暂时没有 host"。

        host 那边出事时，中继会给一个**可识别的原因**（ADDITIVE）：
        `HOST_LEFT`（闪断）/ `HOST_STOPPED`（主动停止开服）/ `HOST_RELEASED`
        （主动释放主机位）/ `NO_HOST`（房间里根本没有 host）。
        这个判据只用来回答"要不要走 host 那条路"；三条路各自怎么处理见
        `_on_host_gone()`（用户 2026-09-30 定稿）。
        """
        text = str(reason or "").upper()

        return (
            "HOST_LEFT" in text
            or "HOST_STOPPED" in text
            or "HOST_RELEASED" in text
            or "NO_HOST" in text
        )

    @staticmethod
    def _is_terminal_failure(reason):
        """
        这个原因是不是**终态**（绝不重连，只弹窗一次）。

        协调者口径的五类：用户点停止 / 主动退房、被踢、房间被销毁、
        版本不一致、登录失效（token 无效 / 被禁用）、免登录被要求登录。
        """
        text = str(reason or "").upper()

        for token in (
            "VERSION_MISMATCH",
            "KICKED",
            "ROOM REVOKED",
            "ROOM_REVOKED",
            "REVOKED",
            "AUTH_INVALID_TOKEN",
            "AUTH_FAILED",
            "AUTH_REQUIRED",
            "AUTH_INVALID",
            "AUTHENTICATION",          # "authentication required"（中继的英文说法）
            "NOT_LOGGED_IN",
            "ROOM_CLOSED",
            "OWNER_LEFT_ROOM",
            # V5.0 AUTO：能力门是终态 —— 重连一万次也还是进不去，只弹窗一次。
            # 顺序无所谓（两个都含 AUTO_UNSUPPORTED），但两个都列上更清楚。
            "AUTO_UNSUPPORTED_MEMBER",
            "AUTO_UNSUPPORTED",
        ):
            if token in text:
                return True

        return False

    def _reconnect_terminal(self, reason):
        """
        进入**终态**：不再重连，弹一次窗。

        终态一共五类（协调者口径）：用户点停止/主动退房、被踢、房间被销毁、
        版本不一致、登录失效（token 无效/被禁用）、免登录被要求登录。
        """
        self._reconnect_phase = "stopped"
        self._reconnect_stop_reason = str(reason or "")
        self._reconnect_attempt = 0
        self._reconnect_started_at = 0.0
        self._reconnect_next_at = 0.0

        if not str(reason or "").strip():
            return

        self.log(
            f"[reconnect] give up: terminal condition ({reason}) -- no retry",
            kind="warn",
        )

    def _reconnect_waiting_for_host(self, reason=""):
        """
        房间暂时**没有 host**：不重试，只等，按钮可点重试（用户口径）。

        这不是失败 —— 房主随时可能回来；玩家自己重试也没有意义
        （`ERROR NO_HOST` 已经由服务端说过一遍）。
        """
        self._reconnect_phase = "waiting_host"
        self._reconnect_attempt = 0
        self._reconnect_next_at = 0.0
        self.log(
            f"[reconnect] the host is not serving (reason={reason or 'no host'}); "
            f"not retrying by myself -- start again by hand once it is back",
            kind="warn",
        )

    def _on_core_host_gone(self, code="", reason=""):
        """
        【v3.9.16】核心层已经拆完会话之后发来的那一声（`kind="host_gone"`）。

        和房间事件（`host_stopped` 等）说的是同一件事，但**来源不同**：这条由
        `MCLinkEngine._on_host_gone_from_room` 直接 push，到达时会话与直连通路
        已经被停掉了。

        所以这里**无条件**处理，不看任何窗口 —— 它是"已经发生的事实"的通知，
        不是"要不要断开"的询问。
        """
        kind = {
            "HOST_LEFT": "left",
            "HOST_STOPPED": "stopped",
            "HOST_RELEASED": "released",
            "NO_HOST": "left",
        }.get(str(code or "").strip().upper(), "stopped")

        self._on_host_gone(kind, reason or code, forced=True)

    def _defer_host_gone(self, kind, reason=""):
        """
        【v3.9.16】模式重建窗口里到了一条 host-gone：**缓存**，等窗口结束补跑。

        ============================================================================
          和"丢弃"的区别就是这次修复的全部意义
        ============================================================================
        窗口期的语义本来是"这可能是我自己切模式时的 teardown，别把界面打乱"。
        但它被写成"什么都不做" —— 于是**真的** host 停服也被一起吞掉，中继
        已经把会话拆了，界面却还写着"已连接"（用户 10-03 实测）。

        现在只**推迟**界面的那一部分（提示 + 按钮重算）：
          * 断不断由核心层决定，早就断完了；
          * `_flush_pending_host_gone()` 在窗口一结束就补跑；
          * 还有 `HOST_GONE_DEFER_MAX_S` 兜底 —— 窗口万一没人清，也不会把
            这件事无限期压着（和 `MODE_SWITCH_SILENT_MAX_S` 同一条道理）。
        """
        self._pending_host_gone = {
            "kind": kind,
            "reason": reason,
            "at": time.monotonic(),
        }

        self.log(
            f"[host] host {kind} arrived during a mode-switch rebuild; the core "
            "has already torn the session down -- deferring only the UI part "
            f"(it runs as soon as the rebuild ends, {HOST_GONE_DEFER_MAX_S:g}s "
            "at the latest)",
            kind="warn",
        )

    def _flush_pending_host_gone(self):
        """
        【v3.9.16】窗口一结束（或兜底超时）就把缓存的那条 host-gone 补跑一次。

        每秒的 tick 里调，所以"窗口结束"最多晚一拍被发现 —— 而界面本来就
        是一秒一拍刷新的，这一拍用户看不出来。
        """
        pending = getattr(self, "_pending_host_gone", None)

        if not pending:
            return

        now = time.monotonic()

        # 还在窗口里？那就再等等（但绝不无限等，见下面的兜底）。
        #
        # 口径和 `_handle_core_event` 里那条**完全一致**：能压住补跑的只有
        # "有界的模式热切换窗口"。`_rebuild_in_flight` 不再是掩码 —— 它只说明
        # 我这边有一次重建在飞，与 host 走没走无关；留着它只会让补跑白等到
        # 兜底上限（`HOST_GONE_DEFER_MAX_S`）才执行。
        windows_open = (
            (
                getattr(self, "_mode_switch_silent", False)
                and now <= getattr(self, "_mode_switch_silent_until", 0.0)
            )
            or (
                getattr(self, "_mode_rebuild_pending", False)
                and now <= getattr(self, "_mode_rebuild_pending_until", 0.0)
            )
        )

        if windows_open and (
                now - float(pending.get("at", now)) < HOST_GONE_DEFER_MAX_S):
            return

        self._pending_host_gone = None

        self.log(
            "[host] the mode-switch window is over -- running the deferred "
            f"host-gone ({pending.get('kind')})",
            kind="warn",
        )

        self._on_host_gone(
            pending.get("kind", "stopped"),
            pending.get("reason", "") or "",
            forced=True,
        )

    def _on_host_gone(self, kind, reason="", forced=False):
        """
        host 那边出事的**唯一入口**（用户 2026-09-30 定稿，三种情况分开）。

        `forced=True` 表示"核心层已经拆完了，这里是补界面那一份"（v3.9.16）。
        和默认路径的区别只有一处：即使 `self.running` 已经是 False，也再调一次
        `engine.stop_session()` —— 它是幂等的，而"掉线处理先把 running 清成
        False、随后 host_stopped 才到"正是以前 `stop_session()` 被跳过的那条路。

        ============================================================================
          三种情况（`kind`）与两种身份（我是不是 host）
        ============================================================================
        * `"left"`     —— host **闪断**（中继 `HOST_LEFT`：他的成员连接断了，但
          身份/主机位还在宽限期里保留）。用户口径："迅速回来后可能只是丢了几个
          包…检测现行连接是否还可用，假如可用那就复用，假如不可用那就自动重连；
          原来房间里其他人处于连接态，那就直接重连，假如有人处于空闲态，那空闲态
          不重连。"
          -> 我们这边：**在连接态**（`running`）-> 走自动重连（`_rebuild_tunnels`
          会保住已经打通的洞、只重建死掉的，所以"能复用就复用"就是它）；
          **空闲态** -> 什么都不做，只重算按钮。
        * `"stopped"`  —— host **主动停止开服**（主机位保留、人还在房间）。
          -> 断开 + 重算按钮，**不自动重连、不弹窗**。
        * `"released"` —— host **主动释放主机位**。
          -> 断开 + 重算 + **弹窗「host 已断开连接」**，不自动重连。

        设计里**没有"房主已离线"这种说法**（用户 2026-09-30）：房主真离开
        房间 -> 房间直接销毁（`ROOM REVOKED`），根本走不到这里。所以这一条路
        上一句"房主…"都不会出现，"host 已断开连接"说的也**只是 host 这个角色**。

        我自己就是 host 时（身份在我手上），这几行说的是**我自己的**开服/主机位
        状态：只重算界面，不弹"host 已断开连接"（那是我自己刚做的动作）。
        """
        state = None

        with contextlib.suppress(Exception):
            state = self.engine.room_state()

        if state is not None and getattr(state, "is_host", False):
            # 我持有主机位：这是我的事，不是"别人家的 host 没了"。
            with contextlib.suppress(Exception):
                self._refresh_room_ui()

            return

        # ============================================================================
        #  ★ 用户 2026-10-03（重申；实测仍未生效的正是这一条）
        # ============================================================================
        #
        #  「host 断开连接时，房间内所有人一并一起断开连接且重算按钮状态」。
        #  旧行为：`left`（HOST_LEFT，host 闪断/掉线）在**连接态**走自动重连
        #  （`_trigger_auto_reconnect`）—— 界面于是继续显示"已连接"，可 host
        #  根本不在。用户现场："他断开连接，我这里应该也要断开连接并释放的，
        #  可是实际上并未，我这里还是已连接的状态"。
        #  现在：`left` 与 `stopped`/`released` 走**同一条断开路径**（下面那段），
        #  只是提示词不同；**不自动重连**，等 host 回来再手动点「开始连接」。
        host_left = kind == "left"

        # ---- 主动停止开服 / 主动释放主机位：断开 + 重算，**不自动重连** ----
        released = kind == "released"

        # 幂等：已经在这个状态里了（中继重复通知 / 停止与释放前后脚到），
        # 第二次只重算界面，不再停一遍、也不再弹一次窗。
        already_down = getattr(self, "_reconnect_phase", "") == "host_down"

        # ============================================================================
        #  ★ v3.9.16：`self.running` 不再**单独**决定"停不停"（2026-10-04 修正）
        # ============================================================================
        #
        # 旧代码是"只有 `running` 为真时才去停"，也就是下面这种形状被反过来
        # 用成了闸门：
        # 而"掉线处理会先把 `self.running` 清成 False"（`_session_busy` 上方那段
        # 注释自己写着）—— 于是 PLAYER socket 掉线先被处理、`host_stopped` 后到
        # 时，这一步被整个跳过：只写状态、不断会话。用户看到的就是"host 断了、
        # 我这里还是已连接"。
        #
        # 但**无条件**调也不行（2026-10-04）：空闲态（本来就断开、也没有重建/
        # 重连在跑）收到 host **闪断**（HOST_LEFT）时，用户口径是"保持空闲"——
        # 本来就没有连接可停，回归网直接钉住这个调用次数（空闲 -> 0 次）。
        #
        # 所以判据是"**还可能有东西要停**"：
        #   * `self.running`：界面这边还认为在连接；
        #   * `_rebuild_in_flight` / `_reconnect_phase == "reconnecting"`：
        #     重建或自动重连正在跑 —— 这一次"停"是必要的收尾（`stop_session()`
        #     本身幂等，没有会话时立刻返回 True）；
        #   * `kind in ("stopped", "released")`：host **明确结束**了开服/主机位。
        #     这时中继那边可能刚拆完、界面 `running` 只是还没跟上的陈旧值 ——
        #     v3.9.16 要修的正是这一条，必须再停一次；
        #   * `forced=True`：核心层已经拆完，这里补界面那一份
        #     （`_flush_pending_host_gone()`）。
        session_maybe_live = (
            bool(self.running)
            or bool(getattr(self, "_rebuild_in_flight", False))
            or getattr(self, "_reconnect_phase", "") == "reconnecting"
            or kind in ("stopped", "released")
            or bool(forced)
        )

        if not already_down and session_maybe_live:
            with contextlib.suppress(Exception):
                self.engine.stop_session()

        self.running = False

        # 闸门：`host_down` 期间**不许自动重连**（`_can_auto_reconnect` 读它）。
        # 用户口径：这情况不要自动重连了，免得又弄出 BUG；host 回来之后由用户
        # 自己手动点「开始连接」。
        self._reconnect_phase = "host_down"
        self._reconnect_attempt = 0
        self._reconnect_started_at = 0.0
        self._reconnect_next_at = 0.0
        self._reconnect_stop_reason = ""

        self._host_down_hint(
            "host 已断开连接，host 位置暂无人认领"
            if released
            else ("host 已断开连接" if host_left else "host 已断开连接（未开服）")
        )
        self.log(
            "[reconnect] the host released the seat (HOST_RELEASED); stopping "
            "this session, clearing the old state, NOT retrying"
            if released
            else (
                "[reconnect] the host is gone (HOST_LEFT); stopping this "
                "session and recomputing the buttons, NOT retrying"
                if host_left
                else "[reconnect] the host stopped serving (HOST_STOPPED); "
                     "stopping this session, clearing the old state, NOT retrying"
            ),
            kind="warn",
        )

        with contextlib.suppress(Exception):
            self._refresh_room_ui()

        if released and not already_down:
            messagebox.showinfo(
                tr("host 已断开连接"),
                tr(
                    "host 已断开连接，host 位置暂无人认领。\n\n"
                    "如果还需要联机，请等有人认领 host 并开服之后，"
                    "再手动点「开始连接」。"
                ),
                parent=self.root,
            )

    def _host_down_hint(self, text):
        """把"host 那边怎么样"写到「开始连接」下面那行提示里（**不弹窗**）。"""
        with contextlib.suppress(Exception):
            self.connect_hint.configure(text=tr(text))

    def _on_host_left(self, reason=""):
        """兼容旧调用点（`room_lost` 的 no-host 原因）：按"host 闪断"处理。"""
        self._on_host_gone("left", reason)

    def _session_busy(self):
        """
        界面意义上的"这次连接还在（或者正在被重建）"。

        ============================================================================
          ★ 用户 2026-09-29 第 3 条：按钮状态必须跟着重连走
        ============================================================================
        用户原话：「直连换中转，会触发自动重连，但是按钮状态被错误的释放了；
        中转换直连，按钮状态未被释放，但是连接未自动重连」。

        根因就是这里：掉线处理会先把 `self.running` 清成 False（"世界那侧停了"），
        而按钮只看 `self.running` —— 于是**自动重连还在跑的那几秒**里，界面已经
        把「开始连接」放开了，看起来像"没在连"。反过来，重连成功之后没有任何
        地方把 `running` 恢复成 True，按钮就一直停在"已停止"（用户看到的另一半）。

        所以："忙碌" ≠ "running"：自动重连窗口内也算忙碌。
        """
        if self.running:
            return True

        # ★ 2026-10-03（分析结论 E1）：真正的"正在重建"是 `_rebuild_in_flight`
        #   这个**事实** —— worker 的 finally 一定会复位它，不会像别的开关那样
        #   被留下。把它算进来之后，模式热切换重建期间按钮**保持按下**（用户口径：
        #   按钮不许被释放），而静默窗口就不必再硬写 `running` 了。
        if getattr(self, "_rebuild_in_flight", False):
            return True

        return getattr(self, "_reconnect_phase", "") == "reconnecting"

    def _can_auto_reconnect(self):
        """现在允许自动重连吗（不满足就什么都不做）。"""
        phase = getattr(self, "_reconnect_phase", "running")

        # 正在自动重连的这一次**必须能接着跑**：掉线那一刻 `running` 会被清成
        # False（世界那侧停了），但重连本身还没结束 —— 拿 `running` 当闸门会把
        # 重连的第一次退避直接掐死（用户第 3 条的另一半）。
        if not self.running and phase != "reconnecting":
            return False

        if getattr(self, "_stopping_on_purpose", False):
            return False

        # `stopped` = 用户自己停的；`host_down` = host 主动停止/释放（用户
        # 2026-09-30 口径：**这情况不要自动重连**，等他回来手动点开始连接）。
        if phase in ("stopped", "host_down"):
            return False

        if getattr(self, "_rebuild_in_flight", False):
            return False

        return True

    def _reconnect_succeeded(self):
        """
        自动重连成功（会话重新起来了）：把界面恢复成"在连接"。

        以前**没有任何地方**做这件事，所以一次自动重连之后 `_reconnect_phase`
        永远停在 `reconnecting`、`running` 永远是 False —— 界面显示"已停止、可以
        点开始连接"，而连接其实已经回来了（用户第 3 条前半句）。
        """
        self._reconnect_phase = "running"
        self._reconnect_attempt = 0
        self._reconnect_started_at = 0.0
        self._reconnect_next_at = 0.0
        self._reconnect_stop_reason = ""
        self.running = True

        self.log("[reconnect] the session is back; buttons are live again", kind="ok")

        with contextlib.suppress(Exception):
            self._refresh_room_ui()

        with contextlib.suppress(Exception):
            self.root.after(0, self._refresh_room_ui)

    def _trigger_auto_reconnect(self, reason):
        """
        非用户意图的断开 -> 起一次（带退避的）会话重建。

        只做**一次调度**，真正的重建在后台线程里跑；到点没成功就进下一次退避，
        总窗口（`RECONNECT_WINDOW_S`）用完就停下 + 弹窗（用户口径）：
        「失败到底 → 停止（终态原因）」。
        """
        if not self._can_auto_reconnect():
            return False

        now = time.time()
        started = getattr(self, "_reconnect_started_at", 0.0) or 0.0

        if not started:
            started = now
            self._reconnect_started_at = started
            self._reconnect_attempt = 0

        if now - started > self.RECONNECT_WINDOW_S:
            reason_text = f"自动重连超时（{self.RECONNECT_WINDOW_S:.0f}s）"
            self._reconnect_terminal(reason_text)
            self._refresh_room_ui()
            messagebox.showwarning(
                tr("自动重连失败"),
                tr(
                    "已经重试了 {0} 次都没能恢复连接，已停止重试。\n\n"
                    "原因：{1}\n\n你可以手动点「开始连接」再试。"
                ).replace("{0}", str(self._reconnect_attempt)).replace(
                    "{1}", str(reason or "")
                ),
                parent=self.root,
            )
            return False

        attempt = int(getattr(self, "_reconnect_attempt", 0) or 0) + 1
        self._reconnect_attempt = attempt

        index = min(attempt - 1, len(self.RECONNECT_BACKOFF_S) - 1)
        delay = self.RECONNECT_BACKOFF_S[index]

        self._reconnect_phase = "reconnecting"
        self._reconnect_next_at = now + delay

        self.log(
            f"[reconnect] attempt {attempt} in {delay:.1f}s (reason={reason})",
            kind="warn",
        )

        self._refresh_room_ui()

        def worker():
            time.sleep(delay)

            if not self._can_auto_reconnect():
                return

            # 回到 Tk 主线程再动界面状态（后台线程碰 tkinter 会抛
            # "main thread is not in main loop"）。
            self.root.after(0, lambda: self._reconnect_session())

        threading.Thread(
            target=worker, name="reconnect-delay", daemon=True
        ).start()

        return True

    def _reconnect_session(self):
        """
        真正重建一次会话（**Tk 主线程**上调用 —— `root.after` 调度回来）。

        固定顺序（协调者口径）：
          ① `stop_session()` 并**确认本地代理端口真的释放**（失败就报错不往下）；
          ② 角色按"我是否持有 host 位"定（**绝不退化成 player**）；
          ③ 按**房间当前模式**（不是本机磁贴）重建；
          ④ 立刻重排房间级任务（NAT 监测 + 打洞）；
          ⑤ **不释放 host 位**（现行规则：只有"先停止再释放"能交出去）。

        这五条 `_rebuild_tunnels` 本来就全做到了（角色判定 / `_collect_config` /
        rebuild_done 后 `_sync_room_nat`）。所以这里只补一句可观测日志，然后调它
        —— 绝不另写一份平行实现。
        """
        # ================================================================
        #  ★ 另一次重建还在收尾时**不许静默放弃**
        # ================================================================
        #
        # `_rebuild_tunnels()` 遇到 `_rebuild_in_flight` 会直接返回。如果在这里
        # 什么都不做，这次自动重连就**停在"正在自动重连"上**：`running` 是 False、
        # `_reconnect_phase` 是 `reconnecting`，谁也不再来叫醒它（按钮永远不恢复、
        # 退避也不再继续）。所以这里自己等一小会儿再试（有界，免得卡死）。
        if getattr(self, "_rebuild_in_flight", False):
            deferred = int(getattr(self, "_reconnect_deferred", 0) or 0) + 1
            self._reconnect_deferred = deferred

            if deferred <= self.RECONNECT_DEFER_LIMIT:
                self.log(
                    f"[reconnect] another rebuild is still finishing; "
                    f"retrying in 0.5s ({deferred}/"
                    f"{self.RECONNECT_DEFER_LIMIT})",
                    kind="warn",
                )
                with contextlib.suppress(Exception):
                    self.root.after(500, self._reconnect_session)

                return

            self.log(
                "[reconnect] another rebuild never finished; giving up "
                "(manual reconnect needed)",
                kind="error",
            )
            self._reconnect_terminal("上一次重建一直没有结束")
            self._refresh_room_ui()

            return

        self._reconnect_deferred = 0

        role = "?"

        with contextlib.suppress(Exception):
            role = self._safe_connect_role()

        self.log(
            f"[reconnect] attempt {self._reconnect_attempt} rebuilding session "
            f"role={role} mode={self._room_conn_mode() or '?'}",
            kind="warn",
        )

        # ★ 用户 2026-09-30 第 1 条：自动重连**一律重建通路**（不再判断原通路
        #   是否可复用）：host 重建全部，player 只重建到 host 的那一条。
        #   真正的"丢洞"在 `_rebuild_tunnels` 的工作线程里做（见那里的说明）。
        self._rebuild_tunnels(
            mode=self._room_conn_mode() or None, force_links=True
        )

    def _is_guest(self):
        """现在是不是游客（免登录）身份。"""
        return self.auth is None

    def _remember_guest_name(self, name):
        """
        记住中继这次分配给我们的游客名（**登录会话级**，见 `__init__` 的说明）。

        只在**本来就没有游客名**时记住；已经有就不覆盖 —— 名字属于这次的登录
        会话，退房 / 被踢 / 房间被关之后再进房必须还是它（用户 2026-09-27 口径），
        换新名字只能由中继在**下一次连接**里给（那时我们已经重启或登出过了）。

        **绝不 `save_config`**：游客身份是一次性的，不许落盘。
        """
        name = str(name or "").strip()

        if not name or self._guest_name or not self._is_guest():
            return self._guest_name

        self._guest_name = name
        self.log(
            f"guest identity for this login session: {name} "
            f"(kept until logout or restart; never written to the config file)",
            kind="dim",
        )

        return self._guest_name

    def _forget_guest_identity(self, reason=""):
        """
        **销毁**游客身份：登出 / 换账号 / 重启软件才走这里。

        用户 2026-09-27 口径：游客身份是**登录会话级**的 ——
        退房 / 被踢 / 房间被关**不**销毁，只有"账号退出"才销毁
        （掉线重连导致登录状态丢失、换账号登录、重启软件）。

        顺带把可能残留在配置里的那个游客名清掉：老版本曾经把游客名写进
        `config.username`，那会让下次连接"看起来还是同一个人"。现在起
        `on_start` 也不会再写进去了，这里再兜一次。
        """
        had = bool(self._guest_name)

        self._guest_name = ""
        self._guest_token = ""

        # 引擎那份也要清：它下一次进房会把它放进握手行的 token 字段。
        with contextlib.suppress(Exception):
            self.engine._guest_token = ""

        if had:
            self.log(
                f"cleared the guest identity ({reason or 'login session ended'}); "
                f"the next connection gets a brand-new relay-assigned name",
                kind="warn",
            )

    def _configured_username(self):
        """
        这次连接该报给中继的名字：登录名 > 本次登录会话的游客名 > 表单/配置里的名字。

        游客名的用途就是"同一个登录会话里换个房间也不用换名字"；重启之后
        `_guest_name` 是空的（内存状态），于是又回到表单里的名字，由中继重新分配。
        """
        if self.auth is not None:
            return str(self.auth.username or "").strip()

        if self._guest_name:
            return self._guest_name

        return str(self.config.username or self.username_var.get() or "").strip()

    def _sync_identity(self):
        """
        房间已加入、且**登录状态真的变了**时，告诉中继"我这个 client_id 现在的
        显示名是这个"（`IDENTITY <用户名> <token> ver=<版本>`）。

        ============================================================================
          用户要求（ISSUES 问题 5）：玩家身份绑定设备
        ============================================================================

        用户原话：「玩家身份绑定设备，无论处于游客还是已登录的状态，加入房间的
        都是这个人，仅信息发生改变。」

        中继的显示名原来只在 CREATE/JOIN 握手那一刻取一次（`room.py::_request`），
        所以人在房间里登录之后名册里还是游客名（日志证据：craftlink-client.log
        12:22:59 `self_name='游客45teiy'`，直到重新握手才变 `root`）。

        三条硬规矩（照协调者给的格式）：
          1. **只在"房间已加入 + 登录/改名状态发生变化"时发一次** ——
             同一个 (用户名, token) 不重复发；再加 5 秒冷却兜底；
          2. 没有房间**不发**（本来就没有名册可更新）；
          3. 失败**绝不影响连接**：记一行英文日志 + 弹一句中文提示，
             名字保持原样（界面照旧显示中继给的那个）。

        跑在后台线程上：`room_send_identity` 要等中继应答（最多 8 秒），
        不能卡住 Tk 主循环。返回 True 表示这次真的发出去了。
        """
        state = self._room_state_obj()

        if state is None:
            return False

        username = self._configured_username()

        token = str(getattr(self.auth, "token", "") or "").strip()

        # 免登录 / 没名字：中继只按 client_id 记得我们，这条消息没有意义。
        if not username:
            return False

        signature = (username, token)

        if signature == getattr(self, "_identity_sent", None):
            return False

        now = time.time()

        if (
            now - getattr(self, "_identity_sent_at", 0.0)
            < self.IDENTITY_COOLDOWN_S
            and signature == getattr(self, "_identity_attempted", None)
        ):
            return False

        self._identity_attempted = signature
        self._identity_sent_at = now

        def worker():
            try:
                ok, message, name = self.engine.room_send_identity(
                    username, token
                )
            except Exception as e:  # noqa: BLE001 - 改名绝不能让界面崩
                ok, message, name = False, f"{type(e).__name__}: {e}", ""

            def report():
                if ok:
                    self._identity_sent = signature
                    self.log(
                        f"the relay accepted our display name: {name or username}",
                        kind="ok",
                    )
                    with contextlib.suppress(Exception):
                        self._refresh_room_ui()
                    with contextlib.suppress(Exception):
                        self._refresh_member_list(force=True)
                else:
                    # 老中继直接忽略这条行（超时）—— 说清楚，别让用户以为网络坏了。
                    self.log(
                        f"the relay did not confirm the display-name update "
                        f"({message}); keeping the current name",
                        kind="warn",
                    )
                    messagebox.showwarning(
                        tr("改名未生效"),
                        tr(
                            "房间里显示的名字没能更新：{0}\n\n"
                            "连接不受影响，房间里的名字会保持服务器给的那个"
                            "（可能是游客名）。"
                        ).replace("{0}", str(message or "")),
                        parent=self.root,
                    )

            self.root.after(0, report)

        threading.Thread(target=worker, name="identity", daemon=True).start()

        return True

    def _safe_connect_role(self):
        """
        这次连接该用哪个身份。同一个判定规则给 `on_start` / `_rebuild_tunnels` /
        模式自动重建用。

        **绝不静默降级**：判定失败（拿不到房间状态之类）也要留下一行日志，
        然后才退回 `config.role`（上次真正用过的身份），最后才退到 player。
        """
        state = self._room_state_obj()

        if state is None:
            # ================================================================
            #  ★ 没有房间时**不**去问 `_decide_connect_role`
            # ================================================================
            #
            # 用户日志（ISSUES 问题 1/4/6，两份日志各一次，都是退出流程）：
            #
            #     cannot decide the connection role
            #     (AttributeError: 'NoneType' object has no attribute 'is_host');
            #     falling back to the last used role
            #
            # `_decide_connect_role(state)` 的实现是 `state.is_host`，传 None
            # 进去必然 AttributeError。以前靠 `except` 兜住它 —— 能跑，但
            # 每次退出都往用户日志里写一条 "AttributeError"，看起来像故障，
            # 也让"角色判定失败"和"本来就没有房间"混成一件事（前者值得报警）。
            # 这里把"没有房间"当成一条正常分支，日志说人话、结论仍是回落。
            role = str(getattr(self.config, "role", "") or "")

            if role in ("host", "player"):
                self.log(
                    f"not in a room; keeping the last used role ({role})",
                    kind="warn",
                )
                return role

            self.log(
                "not in a room and no previously used role; using player",
                kind="warn",
            )
            return "player"

        try:
            role, _explanation = self._decide_connect_role(state)

            if role in ("host", "player"):
                return role
        except Exception as e:
            self.log(
                f"cannot decide the connection role ({type(e).__name__}: {e}); "
                f"falling back to the last used role",
                kind="warn",
            )

        previous = str(getattr(self.config, "role", "") or "")

        if previous in ("host", "player"):
            return previous

        self.log(
            "no room state and no previously used role; rebuilding as player",
            kind="warn",
        )

        return "player"

    def _decide_connect_role(self, state):
        """
        决定**这次连接用哪个身份**，以及要不要给用户一句解释。

        ============================================================================
          铁律：不再有任何"身份选择器"参与这件事（用户要求把它删掉了）
        ============================================================================

        规则只有一条：

            持有主机位置 = 以【主机 Host】连接（你来开世界）
            否则         = 以【玩家 Player】连接（你进别人的世界）

        用户看得到的三个数字（房主局域网端口 / 玩家本地端口 / 人数上限）是
        **基准配置**，任何时候都能改、和身份无关；真正选哪个端口由这里决定。

        返回 (connect_role, 解释文案或 None)。
        """
        connect_role = "host" if state.is_host else "player"

        if connect_role == "host":
            return connect_role, None

        # 不是主机 —— 如果房间里已经有别人在当主机，顺口说一句为什么。
        if state.host_claimed:
            return connect_role, (
                "这个房间已经有主机了，你将按【玩家 Player】连接。"
                "想自己开世界请换一个房间号。"
            )

        return connect_role, None

    def on_start(self):
        if self.running:
            return

        # ============================================================================
        #  没有"身份选择器"了 —— 身份由「谁持有主机位置」决定
        # ============================================================================
        #
        # 用户要求把身份选择器整个删掉（它会干扰操作栏、默认值还会让人点两次
        # 才有反应）。所以这里不再读任何"身份"控件，只看一件事：
        # **我持有主机位置吗？** 持有 = 以主机身份开世界，否则 = 以玩家身份进房。
        #
        # 三个数字（房主局域网端口 / 玩家本地端口 / 人数上限）在「基准配置」里
        # 随时可改，但**必须是按过「确定」的值** —— 连接时只读 _committed。

        # ---- V3: you must be IN the room first ----
        state = self.engine.room_state()

        if state is None:
            if messagebox.askokcancel(
                "还没有加入房间",
                f"你还没有进入房间 {self._current_room_name()}。\n\n"
                "要以主机身份开世界，需要先创建房间；"
                "要进别人的房间，需要先加入。\n\n现在就去吗？",
                parent=self.root,
            ):
                self.on_join_room()

            return

        if state.revoked:
            messagebox.showwarning(
                "房间已被关闭",
                f"{state.revoked_reason}\n\n请重新创建或加入房间。",
                parent=self.root,
            )
            return

        # ============================================================
        #  §B（设计基准，AGENTS.md 规则 6）：有 Host，但 Host 还没开始连接
        # ============================================================
        #
        # 基线原文："有 Host 但 Host 未开始连接：其他玩家点开始连接**不得进入
        # 正式 P2P 流程**，提示「Host 未连接，请等待 Host 开服」。"
        #
        # 打洞那一层（`punch_flow.select_targets`）已经会返回空目标把它挡住，
        # 但那只是"没打洞"；用户看到的是**界面毫无反应**。所以这里先给提示，
        # 两个地方共用同一个判据（`punch_flow.host_ready`），不可能互相矛盾。
        #
        # `host_ready() is None` = 判断不了（老中继没有 `started=` / 我不在房间 /
        # 没有 Host）-> **不许当成"没开服"**，否则老中继上谁都连不了。
        if not state.is_host and state.host_claimed:
            from mclink.core import punch_flow

            if punch_flow.host_ready(state) is False:
                messagebox.showwarning(
                    tr("Host 未连接"),
                    tr("Host 未连接，请等待 Host 开服"),
                    parent=self.root,
                )
                self.connect_hint.configure(text=tr("Host 未连接，请等待 Host 开服"))
                self.log(
                    "the host has claimed the slot but has not started yet; "
                    "refusing to start a session",
                    kind="warn",
                )
                return

        # ---- 房间有主机位置，但不在我手上 → 先问要不要认领 ----
        #
        # 用户要求删掉身份选择器，但**没有**要求删掉"认领主机身份"这件事：
        # 「认领主机身份 (host)」按钮是显式操作，它决定的是谁负责开世界，
        # 和选择器是两回事（一个决定事实，一个只是界面偏好）。
        if not state.is_host and not state.host_claimed:
            if not self._capacity_within_server():
                return

            # ============================================================
            #  ★ host 那边没开服：玩家侧不自动重试，给清晰提示
            # ============================================================
            #
            # 用户口径（2026-09-30）：host 主动断开/释放之后**不要自动重连**，
            # 等 host 回来之后用户自己手动点「开始连接」。这里只管"还没开服"：
            # 房间还在（host 位可能有人但没在开服，或者暂时没人认领）。
            if getattr(self, "_reconnect_phase", "") in ("waiting_host", "host_down"):
                messagebox.showwarning(
                    tr("host 未开服"),
                    tr(
                        "host 那边现在没有开服，连上也没用。\n\n"
                        "房间还在 —— 等 host 开始连接之后，你再手动点"
                        "「开始连接」。"
                    ),
                    parent=self.root,
                )
                self.connect_hint.configure(text=tr("host 已断开连接（未开服）"))
                return

            if not messagebox.askokcancel(
                "认领主机身份 (host)",
                "这个房间还没有主机。要以主机身份开始，需要先认领主机身份。\n\n"
                "现在认领吗？",
                parent=self.root,
            ):
                return

            ok, message = self.engine.claim_host()

            if not ok:
                messagebox.showerror("认领失败", message, parent=self.root)
                self.log(f"failed to claim the host slot: {message}", kind="error")
                return

            self.log(f"host slot claimed: {message}", kind="ok")
            self._refresh_room_ui()
            state = self.engine.room_state() or state

        # ============================================================================
        #  连接身份：持有主机位置 = 开世界；否则 = 进别人的房间
        # ============================================================================
        connect_role, explanation = self._decide_connect_role(state)

        if explanation:
            self.log(explanation, kind="warn")

        try:
            config = self._collect_config(role_override=connect_role)
        except ValueError as e:
            messagebox.showerror(tr("参数错误"), tr(str(e)))
            self.log(f"invalid parameters: {e}", kind="error")
            return

        # ---- V2: an account may be required before hosting or joining ----
        if not self.require_login():
            self.log("not logged in; connection cancelled.", kind="warn")
            return

        # ============================================================================
        #  账号是**可选**的：免登录（游客）也必须能连
        # ============================================================================
        #
        # 用户实测（审计问题 1）：web 面板把「必须登录」关掉之后，未登录加入房间、
        # 点「开始连接」只会反复看到「这个房间已经有主机了…」，永远进不去，而且
        # **一条日志都没有**。根因就是这里原来无条件写
        #
        #     config.username = self.auth.username
        #
        # 而 `require_login()` 在免登录时返回 True（那时根本没有 auth 对象，
        # 见 6293 行），于是 `self.auth.username` 抛 AttributeError；这一行又不在
        # 任何 try 里，Tk 回调把异常吞掉 —— 不落日志、不弹窗、不连接。
        #
        # 现在：有账号就用账号名（它是权威的）；没有账号就保留配置里 / 中继分配
        # 的游客名。并且整段包进 try，未来任何异常都会变成一行日志，而不是消失。
        try:
            if self.auth is not None:
                config.username = self.auth.username
                self.username_var.set(config.username)
            elif self._guest_name:
                # 同一个登录会话里换个房间：报**中继已经分配给我的那个游客名**，
                # 所以中继认得出"还是这个人"，名字不变（用户 2026-09-27 口径：
                # 退房 / 被踢 / 房间被关都不销毁游客身份）。
                config.username = self._guest_name
                self.log(
                    f"starting as the same guest for this login session "
                    f"({self._guest_name}); it is not written to the config file",
                    kind="dim",
                )
            else:
                # 只有"这一次登录会话还没拿到游客名"（首次连接 / 重启之后）
                # 才用表单里的名字 —— 中继会分配一个全新的「游客xxxxxx」。
                config.username = config.username or self.username_var.get().strip()
                self.log(
                    "starting without a login (guest); the relay assigns a "
                    "brand-new guest name for this connection",
                    kind="warn",
                )
        except Exception as e:
            self.log(
                f"account handling failed ({type(e).__name__}: {e}); "
                f"connecting as a guest with the configured display name",
                kind="error",
            )

        # 游客名字**绝不写进配置文件**（一次性身份）。`config.username` 会被
        # `validated()` 兜成占位符「玩家」，落盘的是那个占位符，不是游客名。
        saved_username = config.username

        if self.auth is None and self._guest_name:
            config.username = self.GUEST_NAME_PLACEHOLDER

        self._config_file_path = cfgmod.save_config(config)

        if self.auth is None and self._guest_name:
            config.username = saved_username

        self.log("=" * 46, kind="dim")
        self.log(
            f"starting {config.role.upper()} mode | "
            f"room {config.room} | "
            f"account {config.username} | "
            f"server {config.server_host}:{config.server_port}",
            kind="info",
        )

        try:
            self.engine.start_session(config, auth=self.auth)
        except Exception as e:
            self.log(f"start failed: {e}", kind="error")

            if self._refuse_outdated(str(e)):
                return

            messagebox.showerror(tr("启动失败"), tr(str(e)))
            return

        # ★ 用户 2026-10-03（host 交接现场）：这里是一次**全新连接**的开始，
        #   上一次 host 走掉时留下的陈旧阶段（host_down / waiting_host）以及
        #   模式重建的残留标记都必须清掉 —— 否则用户重新认领 host、重新连上之后，
        #   那个陈旧阶段会让**后续每一次重建**都被判成"host 不在"（现场表现：
        #   热切换不自动重连 + 按钮被释放）。不管 host 传了几手，流程都必须一样。
        self._reconnect_phase = "running"
        self._mode_switch_silent = False
        self._mode_rebuild_pending = False
        self._mode_rebuild_from = None
        self.running = True
        self._set_enabled(self.start_button, False)
        self._set_enabled(self.stop_button, True)
        self.delete_room_button.configure(state="normal")

        # ★ 用户 2026-09-28 第 5 条：告诉中继"我点了开始连接"（STARTED 1）。
        # 这样 Host 的目标表（`started=`）当场就有我，不用等游戏连接上来。
        self._report_started(True)

        # 主机位置相关的按钮要跟着变（连接跑起来时"释放身份"必须变灰）。
        self._refresh_room_ui()

        if config.role == "player":
            self.log(
                f"connect in Minecraft to 127.0.0.1:{config.local_port}",
                kind="ok",
            )

    def on_stop(self):
        # ★ 2026-10-03（分析结论 E4）：以前 `if not self.running: return` ——
        #   自动重连期间 `running=False` 但按钮是**可用**的（`_session_busy()`），
        #   于是"点停止毫无反应"。现在只要真的忙（在跑或正在重建/重连）就处理。
        if not self._session_busy():
            return

        # 终止在飞的重连/静默窗口，否则下一次刷新又把按钮按回去。
        self._reconnect_phase = "stopped"
        self._reconnect_stop_reason = "用户停止"
        self._reconnect_attempt = 0
        self._reconnect_started_at = 0.0
        self._reconnect_next_at = 0.0
        self._mode_switch_silent = False

        self.log("stopping ...", kind="warn")
        self.stop_button.configure(state="disabled")

        self.root.update_idletasks()

        clean = True

        try:
            # 注意：停止【不再】释放主机位置（用户的要求）。位置是你主动
            # 认领的，也只有「释放主机身份」能交出去 —— 停止只是不开服了，
            # 房间列表里那一列仍然要显示你的名字。
            # ★ v3.9.17：这是**用户点的停止**，必须让中继知道（发 `STOPPING`），
            #   它才能和"掉线/崩溃/模式重建"分开。别的调用点一律不传。
            clean = self.engine.stop_session(graceful=True)
        except Exception as e:
            self.log(f"error while stopping: {e}", kind="warn")
            clean = False

        self.running = False

        # ★ 用户 2026-09-28 第 5 条：告诉中继"我停了"（STARTED 0）—— 房间里
        # 有这个人，但他没在连接；Host 那边必须能区分（「等待对端连接…」）。
        self._report_started(False)

        # 房间级任务（NAT 监测 + 打洞）**立刻**停，不等下一个 UI tick：
        # 它们各自持有 UDP socket，留着就会和下一次连接抢端口、甚至继续打洞
        # （用户报的"断开之后无法再建立新的连接"里就有这一份）。
        with contextlib.suppress(Exception):
            self.engine.watch_room_nat(False, log=self.log)

        if not clean:
            # 拆除没在预算内完成 —— 强制关掉了所有 socket。用户应该知道
            # 这件事发生过（日志里能看到），但界面照常可用：端口已经交出来了。
            self.log(
                "old connection did not stop in time; its sockets were "
                "force-closed (the local port is free again)",
                kind="warn",
            )

        self.connect_hint.configure(text="")
        self.log("stopped", kind="info")

        # 所有按钮状态由这一个函数统一恢复（含开始/停止、认领/释放）。
        self._refresh_room_ui()
        self._refresh_room_list_soon()

    # --------------------------------------------------------
    # Log panel actions
    # --------------------------------------------------------

    def on_copy_log(self):
        text = self.log_text.get("1.0", "end").strip()

        self.root.clipboard_clear()
        self.root.clipboard_append(text)

        self.log("log copied to the clipboard", kind="dim")

    def on_save_log(self):
        path = filedialog.asksaveasfilename(
            title=tr("保存日志"),
            defaultextension=".txt",
            initialfile=time.strftime("mclink-log-%Y%m%d-%H%M%S.txt"),
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )

        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.log_text.get("1.0", "end"))
            self.log(f"log saved: {path}", kind="ok")
        except Exception as e:
            messagebox.showerror(tr("保存失败"), tr(str(e)))

    def on_clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def on_open_data_dir(self):
        """打开**当前生效的**用户数据目录（配置 / 日志 / 记忆都在它下面）。"""
        path = cfgmod.config_dir()

        with contextlib.suppress(Exception):
            os.makedirs(path, exist_ok=True)

        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')  # noqa: S605
            else:
                webbrowser.open(f"file://{path}")
        except Exception as e:
            messagebox.showinfo("数据目录", f"{path}\n\n({e})")

    # 兼容老名字（老代码/文档里叫它"配置目录"）。
    on_open_config_dir = on_open_data_dir

    def on_about(self):
        """
        关于页（M6）：软件名 + logo + 版本 + 目录 + 品牌来源。

        为什么要有：软件名和 logo 现在可以被 branding.json 覆盖，用户必须
        有一个地方能看到"我这个客户端是谁做的、品牌文件从哪读的" —— 否则
        改了名字之后没人能解释它为什么叫这个名字。
        """
        info = cfgmod.config_dir_description()
        branding = branding_mod.load()

        win = tk.Toplevel(self.root)
        win.title(tr("关于"))
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=18)

        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")

        logo = load_logo_image(64)

        if logo is not None:
            holder = tk.Label(head, image=logo, bg=BG)
            holder.image = logo
            holder.pack(side="left", padx=(0, 12))

        names = tk.Frame(head, bg=BG)
        names.pack(side="left", anchor="w")

        tk.Label(
            names,
            text=cfgmod.APP_NAME,
            bg=BG,
            fg=FG,
            font=FONT_TITLE,
            anchor="w",
        ).pack(fill="x")

        tk.Label(
            names,
            text=f"v{cfgmod.APP_VERSION}   构建 {cfgmod.build_stamp()}",
            bg=BG,
            fg=FG_MUTED,
            font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x")

        if branding.app_name != branding_mod.DEFAULT_APP_NAME:
            tk.Label(
                names,
                text=tr("（软件名来自品牌配置 branding.json）"),
                bg=BG,
                fg=FG_MUTED,
                font=FONT_SMALL,
                anchor="w",
            ).pack(fill="x")

        details = [
            f"程序目录：{info.get('install_dir', '')}",
            f"数据目录：{info.get('data_dir', '')}",
            f"日志目录：{cfgmod.log_dir()}",
            f"配置文件：{self._config_file_path or cfgmod.config_path()}",
            "",
            f"品牌文件：{branding.source or '（没有 branding.json，使用内置默认值）'}",
        ]

        if branding.logo_file:
            details.append(f"品牌 logo：{branding.logo_file}")

        if branding.has_server_default():
            details.append(
                "品牌默认服务器："
                f"{branding.default_server_host}:{branding.default_server_port}"
            )
        else:
            details.append("品牌默认服务器：未配置（地址由用户填写）")

        if branding.update_url:
            details.append(f"更新地址：{branding.update_url}")

        for warning in branding.warnings:
            details.append(f"⚠ {warning}")

        tk.Label(
            body,
            text="\n".join(details),
            bg=BG,
            fg=FG_MUTED,
            font=FONT_SMALL,
            justify="left",
            anchor="w",
            wraplength=520,
        ).pack(fill="x", pady=(14, 0))

        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(16, 0))

        self._button(
            row,
            "打开数据目录",
            self.on_open_data_dir,
            width=14,
            secondary=True,
        ).pack(side="left")

        self._button(
            row,
            "关闭",
            win.destroy,
            width=8,
        ).pack(side="right")

        with contextlib.suppress(Exception):
            win.update_idletasks()
            x = self.root.winfo_rootx() + 120
            y = self.root.winfo_rooty() + 120
            win.geometry(f"+{int(x)}+{int(y)}")

    # --------------------------------------------------------
    # Event pump
    # --------------------------------------------------------

    def _report_room_emit_errors(self):
        """
        房间事件发不出去时，在日志里说清楚（只报一次）。

        这类错误以前是被静默吞掉的，代价是"房间已经没了，界面还蒙在鼓里"。
        宁可刷一条日志，也不要再出现那种情况。
        """
        session = self.engine.room_current()

        if session is None:
            return

        errors = list(getattr(session, "emit_errors", []) or [])

        if not errors or errors == self._last_emit_errors:
            return

        self._last_emit_errors = errors
        self.log(
            "房间事件没有送达界面（已按最新状态重画）：" + errors[-1],
            kind="warn",
        )

    def _maybe_auto_refresh_room_list(self):
        """
        房间列表的【自动刷新】（用户要求"实时刷新"，不是手动点）。

        每 ROOM_LIST_REFRESH_MS 拉一次。三条闸门，避免无谓的网络流量：
          * 窗口不可见（最小化/切到别的标签）时不拉
          * 上一次请求还没回来时不拉（_room_list_busy）
          * 没填服务器地址时不拉
        失败只记一次日志，不会每 1.5 秒刷屏。
        """
        if self._room_list_busy:
            return

        if time.monotonic() - self._room_list_fetched_at < ROOM_LIST_REFRESH_MS / 1000:
            return

        if not self.host_var.get().strip():
            return

        with contextlib.suppress(Exception):
            # 窗口最小化 / 被隐藏时没必要轮询。
            if self.root.state() == "iconic":
                return

        self.on_rooms_refresh()

    def _tick(self):
        try:
            # 界面正在重建（切换语言/主题）时先让路：这一拍里的控件可能
            # 已经被销毁，刷新它们只会抛异常。
            if getattr(self, "_rebuilding", False):
                return

            self._drain_events()
            self._append_logs()

            # ★ v3.9.16：窗口一结束就把推迟的 host-gone 补跑（被推迟 ≠ 被丢弃）。
            self._flush_pending_host_gone()

            # ============================================================================
            #  ★ 用户 2026-09-30（第三轮第 7 条）：起跑倒计时要有"毫秒感"
            # ============================================================================
            #
            # 状态卡是每 `UI_REFRESH_MS`（1 秒）重画一次的，而约定的喷包时刻只提前
            # 1.5 秒公告 —— 1 秒一拍就只剩一格可看，用户要的 `1.5s -> 1.2s -> ...`
            # 根本走不起来。
            #
            # 所以倒计时期间把**逐目标那一块**（纯文字、就地改 StringVar，不建控件
            # 也不抢焦点）按本 tick（150ms）重画一次；倒计时一结束就回到 1 秒一拍。
            # 只重画打洞那一块，不碰别的、也不触发任何引擎动作。
            self._refresh_countdown_rows()

            # ★ 用户 2026-10-01 第 3 条：和成员列表**同频**上报我自己的连接状态
            #   （纯显示：中继存下来、填进 HOST_STATE 的 `conn=`）。
            self._report_local_conn_state()

            now = time.time()
            if now - self._last_ui_refresh >= UI_REFRESH_MS / 1000:
                self._last_ui_refresh = now
                self._refresh_status()

                # ============================================================
                #  房间界面每秒自愈一次（事件丢了也不能卡在幽灵状态）
                # ============================================================
                #
                # 事件是"推送"：正常情况下一秒内就有，但推送是可以丢的
                # （emit 约定不符、事件队列满、界面正在重建……）。
                # 曾经就丢过一次：房间被服务器关掉，界面完全不知道，按钮一直
                # 停在"还在房间里"。所以这里每秒按引擎的**当前状态**重画一遍 ——
                # 丢事件最多让界面晚一秒，绝不会停在那儿。
                self._refresh_room_ui()
                self._report_room_emit_errors()

            self._maybe_auto_refresh_room_list()

        except Exception as e:  # never let the UI loop die
            with contextlib.suppress(Exception):
                self.log(f"UI refresh error: {e}", kind="warn")

        finally:
            self.root.after(TICK_MS, self._tick)

    def _drain_events(self):
        for event in self.engine.drain_events():
            kind = event.get("kind")

            if kind == "log":
                self.log(event.get("text", ""), kind="normal")

            elif kind == "event":
                self._handle_core_event(
                    event.get("name", ""),
                    event.get("data", {}) or {},
                )

            elif kind == "host_gone":
                # ============================================================
                #  ★ v3.9.16：核心层自己拆完之后的那一声"已经拆了"
                # ============================================================
                #
                # 它和房间事件（`host_stopped` 等）说的是同一件事，但来源不同：
                # 这条由 `MCLinkEngine._on_host_gone_from_room` 直接 push，
                # 此时会话与直连通路**已经被核心层停掉了**。
                #
                # 界面收到它只做两件事：对齐状态、放掉按钮。就算它和房间事件
                # 前后脚都到了也没关系 —— `_on_host_gone` 是幂等的。
                self._on_core_host_gone(
                    event.get("code", "") or "",
                    event.get("reason", "") or "",
                )

            # ★ 用户 2026-10-02：模式热切换的重建期间**界面不许掉到"未连接"**。
            #
            # `_rebuild_tunnels` 内部会 stop_session，停止那条路会把 `running`
            # 清掉、把阶段写成 stopped —— 玩家于是看到"按钮被释放 + 状态栏未连接"，
            # 可一两秒后隧道已经按新模式建好、游戏也能进了（用户现场）。这里在
            # 事件泵每轮之后把界面按回去，直到 `rebuild_done` / `rebuild_failed`。

            elif kind == "rebuild_done":
                # 重建完成：立刻按新模式重新对齐 NAT 监测 / 打洞，别让新会话
                # 在"洞还没开始打"的窗口里干等（用户报的换模式后连不上）。
                #
                # ★ 如果这一次重建属于**自动重连**，那它成功了：把界面从
                # "正在自动重连" 恢复成"在连接"（否则按钮永远停在已停止）。
                # ★ 用户 2026-10-03：`host_down` 可能是**交接之前**留下的陈旧状态。
                #   只有当"我此刻不是 host 且房间里确实没人认领 host"时，才保持断开；
                #   否则（我已经认领了 host / 别人已认领）必须先清掉它，再走正常点亮。
                _state = None

                with contextlib.suppress(Exception):
                    _state = self.engine.room_state()

                _host_present = bool(
                    getattr(_state, "is_host", False)
                    or getattr(_state, "host_claimed", False)
                )

                if (
                    getattr(self, "_reconnect_phase", "") == "host_down"
                    and _host_present
                ):
                    self._reconnect_phase = "running"

                if getattr(self, "_reconnect_phase", "") == "host_down":
                    # ★ 2026-10-03（分析实测的最后一条洞）：host 已经走了
                    #   （`_on_host_gone` 把阶段置成 host_down），这时**任何**
                    #   早先就在飞的重建完成，都不许把界面重新点亮成"已连接"。
                    #   否则用户看到的就是"host 断了、我这里还是已连接"。
                    self.running = False
                    self.log(
                        "[host] a rebuild finished while the host is gone "
                        "(host_down); keeping this side disconnected and the "
                        "buttons released",
                        kind="warn",
                    )
                elif getattr(self, "_reconnect_phase", "") == "reconnecting":
                    self._reconnect_succeeded()
                elif not self.running:
                    # ============================================================
                    #  ★ 用户 2026-09-29 第 3 条：重建成功 = 会话又活了，
                    #    界面必须回到"在连接"
                    # ============================================================
                    #
                    # `_rebuild_tunnels` 成功只说明 `start_session` 没抛异常；
                    # 它**不会**动 `self.running`。于是只要重建**触发那一刻**
                    # 之后 `running` 变成了 False（掉线处理 / 重连窗口 / 别的
                    # 路径），重建成功之后界面就停在"已停止"：
                    #
                    #   * 用户看到的：**状态机显示已连接，按钮却是释放的**；
                    #   * 更糟的是 `_sync_room_nat()` 的开关是
                    #     `stun_room and self.running` —— `running=False` 会把
                    #     刚重建起来的打洞**立刻关掉**，而且每秒都关，
                    #     于是「中转换 STUN 之后根本没有开始打洞」。
                    #
                    # 所以重建成功就是"在跑"这个事实的权威来源：把 running
                    # 补回来，再让 `_sync_room_nat()` 去按新模式拉起打洞。
                    self.running = True
                    self.log(
                        "[mode] the rebuild finished and the session is live "
                        "again; connection buttons re-armed and the punch is "
                        "started for the new mode",
                        kind="warn",
                    )

                # 静默期到此结束：新模式已经起来了，界面按正常状态显示。
                self._mode_switch_silent = False

                with contextlib.suppress(Exception):
                    self._sync_room_nat()

            elif kind == "rebuild_failed":
                # 失败也结束静默（否则界面卡在"在连接"）。
                self._mode_switch_silent = False
                # 换模式重建失败 / 自动重连重建失败。
                #
                # 用户口径（2026-09-27，docs/AUTO-RECONNECT-PLAN.md）：
                # "换模式重建失败→纳入同一套自动重试（不再只留一句
                # 可手动重新连接）"。但也**绝不**把界面卡在"看起来在跑、
                # 其实什么都没连上"：重试窗口内保持 running（按钮维持现状），
                # 到点没成功就进终态 + 弹窗 + 按钮恢复。
                reason = str(data.get("text", "") or "")

                if self._is_terminal_failure(reason):
                    self._reconnect_terminal(reason)
                    self.running = False
                    self._refresh_room_ui()
                    self.connect_hint.configure(text=tr("重建失败，可以重新连接"))
                else:
                    self._trigger_auto_reconnect(
                        f"rebuild failed: {reason}" if reason else "rebuild failed"
                    )
                    self.connect_hint.configure(
                        text=tr("重建失败，正在自动重试 ...")
                    )

            elif kind == "snapshot":
                self.last_snapshot = event.get("data", {}) or {}

            elif kind == "state":
                state = event.get("state")

                if state == STATE_ONLINE:
                    # 身份不再来自选择器（已删除）：由本次会话真正的角色决定。
                    session_role = getattr(self.config, "role", "") or ""

                    if session_role == "host":
                        self.log("host ready; waiting for players to join.", kind="ok")
                    else:
                        self.log("ready; you can enter Minecraft now.", kind="ok")

                elif state == STATE_ERROR:
                    reason = event.get("reason", "") or ""

                    # 版本门优先：这条错误要变成"需要更新"的弹窗 + 停会话，
                    # 而不是日志里一行普通错误。
                    if self._refuse_outdated(reason):
                        return

                    self.log(f"error: {reason}", kind="error")


            # ★ 2026-10-03（分析结论 E3）：静默窗口**只**负责"重建期间别把界面
            #   画成掉线"，绝不硬写 `running` / `_reconnect_phase` ——
            #   旧写法每 150ms 强写一次，会覆盖用户的**合法释放**（手动停止、
            #   被踢、host 掉了），而且到点只清标记、不复原事实，于是按钮
            #   永远按着、停止也无效。
            #   现在按钮的"按下"由 `_session_busy()` 按事实（running 或真的
            #   有重建在跑）重算；这里只负责到点/离房时收尾。
            if getattr(self, "_mode_rebuild_pending", False) and time.monotonic() > getattr(
                self, "_mode_rebuild_pending_until", 0.0
            ):
                self._mode_rebuild_pending = False
                self.log(
                    "[mode] the 'waiting for the new room mode' marker expired "
                    "(the broadcast never arrived); a real host stop is no "
                    "longer masked by it",
                    kind="warn",
                )

            if getattr(self, "_mode_switch_silent", False):
                expired = time.monotonic() > getattr(
                    self, "_mode_switch_silent_until", 0.0
                )
                no_room = self.engine.room_current() is None
                done = not getattr(self, "_rebuild_in_flight", False)

                if expired or no_room or done:
                    self._mode_switch_silent = False

                    if not getattr(self, "_rebuild_in_flight", False):
                        if not self.running and getattr(
                            self, "_reconnect_phase", ""
                        ) == "reconnecting":
                            # 重建结束了、会话也没起来 -> 如实回到停止态。
                            self._reconnect_phase = "stopped"

                        with contextlib.suppress(Exception):
                            self._refresh_room_ui()

                    self.log(
                        "[mode] silent-rebuild window closed "
                        f"({'expired' if expired else ('left the room' if no_room else 'rebuild finished')}); "
                        "buttons and status are recomputed from the real state",
                        kind="warn",
                    )
    # --------------------------------------------------------
    # 版本门（客户端与服务端必须同版本）
    # --------------------------------------------------------

    def _refuse_outdated(self, text):
        """
        服务端说两边版本不一致时：弹「需要更新」并且**拒绝运行**。

        这里不是"提示一下然后继续用旧版" —— 旧版会带着已删除/已改名的
        控制字段继续打，制造出比报错更难查的怪状态。所以：不重连、不
        降级，把已经起来的会话停掉，只留一个明确的更新提示。

        返回 True 表示"这条错误已经被版本门吃掉了"，调用方不要再当普通
        错误处理（否则用户会同时看到两个弹窗）。
        """
        if not proto.is_version_mismatch(text):
            return False

        self.log(f"version rejected: {text}", kind="error")

        if getattr(self, "_version_popup_shown", False):
            self._stop_after_version_reject()
            return True

        self._version_popup_shown = True

        server, client = proto.version_mismatch_peers(text)

        self._stop_after_version_reject()

        # ★ M6：下载地址**不再是代码里的常量**。它来自 branding.json
        # （`update_url`），没有品牌文件时就没有网址 —— 客户端不许把一个
        # 真实域名留成内置默认值（D10 的同一条口径）。
        url = cfgmod.update_url()
        site = url.replace("https://", "").replace("http://", "").rstrip("/")

        # 日志正文英文（日志是分析材料，不跟界面语言走）；中文提示在那个
        # 弹窗里。
        if url:
            self.log(f"version mismatch; update from {url}", kind="error")
        else:
            self.log(
                "version mismatch; no update_url configured (branding.json), "
                "asking the user to contact the server operator",
                kind="error",
            )

        body = (
            "客户端与服务器的版本不一致，无法连接。\n\n"
            f"服务器版本：{server or '未知'}\n"
            f"本机版本：{client or '未知'}\n"
            f"本机程序版本：{cfgmod.APP_VERSION}\n\n"
        )

        if url:
            body += (
                f"请下载最新版客户端：{site}\n"
                f"（{url}）\n\n"
                "现在打开下载页面吗？"
            )

            if messagebox.askyesno("需要更新", body, parent=self.root):
                with contextlib.suppress(Exception):
                    webbrowser.open(url)
        else:
            body += (
                "请向服务器管理员/部署者索取最新版客户端"
                "（这个客户端没有配置下载地址）。"
            )

            messagebox.showinfo("需要更新", body, parent=self.root)

        return True

    def _stop_after_version_reject(self):
        """版本门命中后把会话收干净（不释放主机身份、不重连）。"""
        if not self.running:
            return

        try:
            self.engine.stop_session()
        except Exception:
            pass

        self.running = False
        self._version_popup_shown = True

        try:
            self.connect_hint.configure(text="版本不一致，已停止")
            self._refresh_room_ui()
        except Exception:
            pass

    def _handle_core_event(self, name, data):
        # 版本门可以在任意一条 relay 回复里出现（HOST_TUNNEL / PLAYER 握手
        # 被拒时就是一条普通 core 错误），所以每个带 reason/message 的事件
        # 都先过一遍这道门。
        for _field in ("reason", "message", "text"):
            _val = str(data.get(_field, "") or "")

            if _val and self._refuse_outdated(_val):
                return

        if name == "control_online":
            self.log("control connection established (connected to the relay)", kind="ok")

        elif name == "control_offline":
            self.log(
                f"control connection closed: {data.get('reason', '')}; reconnecting ...",
                kind="warn",
            )

        elif name == "tunnel_ready":
            self.log(
                f"tunnel {data.get('tunnel')} ready (idle, waiting)", kind="dim"
            )

        elif name == "session_start":
            self.log(
                f"player connected: session={data.get('session')} "
                f"tunnel={data.get('tunnel')}",
                kind="ok",
            )

        elif name == "session_end":
            self.log(
                f"player disconnected: session={data.get('session')}", kind="info"
            )

        elif name == "info":
            players = data.get("players")
            if players and players != self._last_info_players:
                self._last_info_players = players
                self.log(f"room players: {players}", kind="info")

        elif name == "rates":
            # Live speed is rendered from the snapshot tick; this event is
            # kept for future charting / logging of speed peaks.
            self._last_rates = data

        # ---- V2: account events ----
        elif name == "auth_failed":
            self.log(f"login rejected by the server: {data.get('reason', '')}", kind="error")

        elif name == "auth_invalidated":
            reason = data.get("reason", "")
            self.log(f"login invalid: {reason}; please log in again", kind="error")
            self._handle_auth_lost(reason)

        elif name == "delete_result":
            if data.get("ok"):
                self.log(data.get("message", "房间已删除"), kind="ok")
            else:
                self.log(
                    f"failed to delete the room: {data.get('message', '')}", kind="error"
                )

        elif name == "account_renamed":
            # Emitted by the running session (the GUI path already updated the
            # form). A running room keeps working; only the display name moved.
            renamed = data.get("username", "")

            if renamed:
                self.username_var.set(renamed)
                self.config.username = renamed
                self._refresh_account_ui()
                self._sync_identity()
                self.log(
                    f"username changed to: {renamed} (the room is unaffected)", kind="ok"
                )

        elif name == "identity_updated":
            # 中继认了我们的新显示名（问题 5）。名字以**它回的**为准：
            # 刚刚登录 / 改名的这个 client_id 在房间名册里从此对得上号。
            confirmed = str(data.get("name", "") or "")

            if confirmed:
                self.username_var.set(confirmed)
                self.config.username = confirmed

            self._refresh_account_ui()
            self._refresh_room_ui()

            with contextlib.suppress(Exception):
                self._refresh_member_list(force=True)

            self.log(
                f"the relay updated our display name to {confirmed or '(unchanged)'}",
                kind="ok",
            )

        # ---- V3: room membership ----
        elif name == "room_joined":
            self.log("entered the room", kind="ok")
            # 新房间 = 新的"已通知过"周期
            self._room_close_notified = False

            # 新房间 = STARTED 上报也要重新来一次（中继那边的 connecting 标记是
            # 跟着成员对象走的，换房/重连之后必须重新说清楚）。
            self._started_reported = None

            if self.running:
                # 自动重连/断线重连之后会话可能还在跑：补一次 STARTED 1，
                # 否则 Host 的目标表里暂时没有我们（用户第 5 条的同一件事）。
                self._report_started(True)

            # ================================================================
            #  `_mode_rebuild_pending` **不在这里清**
            # ================================================================
            #
            # 旧代码在 `room_joined` 里无条件清掉它，理由是"进新房间时旧标记
            # 不该有意义"。但那正好把**同一次换模式**的重建吃掉：热切换会让
            # 玩家的成员连接抖一下（中继先拆旧通路再广播），紧接着的
            # `room_joined` 把"待重建"清掉 -> 按钮还显示在连接，会话却永远不
            # 按新模式重建（用户 2026-09-29 第 3/6 条："中转换直连……连接未自动
            # 重连，需要双端手动重连"）。
            #
            # 标记的清零点只剩 `_maybe_rebuild_for_mode_change()` 里那几个
            # （真的重建了 / 会话本来就在新模式上）；每秒一次的
            # `_refresh_room_ui()` 马上就会再判一次，这里什么都不用做。


            # 游客身份是**登录会话级**的：中继第一次给的「游客xxxxxx」记在内存里，
            # 退房 / 被踢 / 房间被关都**不销毁**（下次建房或加房还用这个名字），
            # 只有登出 / 换账号 / 重启才由中继重新分配。绝不写进配置文件。
            state = self._room_state_obj()

            if self._is_guest() and state is not None:
                own = str(getattr(state, "own_name", "") or "").strip()
                remembered = self._remember_guest_name(own)

                if remembered:
                    self.username_var.set(remembered)

            self._refresh_room_ui()
            self._refresh_room_list_soon()
            self._refresh_member_list()

            # 中继首次分配游客名时随 OK 回包给了 `guest=<token>`：存进**登录
            # 会话级**状态（内存），同一个会话里换房 / 重新进房时放回握手行，
            # 名字才不变（用户 2026-09-27 口径）。登出 / 换账号 / 重启清掉。
            with contextlib.suppress(Exception):
                if self._is_guest():
                    self.engine._guest_token = str(
                        getattr(self.engine.room_current(), "guest_token", "")
                        or self.engine._guest_token
                        or ""
                    )

        elif name == "conn_mode_changed":
            # ============================================================
            #  §D 末句（设计基准 / PROTOCOL §8.3）：换模式 = **严格重建全部通路**
            # ============================================================
            #
            # 中继在成员控制连接上推的不带 `ERROR` 前缀的那一行。它**不是**网络
            # 抖动：旧模式下的洞 / 隧道 / 每条连接的独立状态机全部作废（新模式的
            # NAT 映射和它们不是同一套），必须整体重建。
            #
            # 这里和"房间模式变了"那条路径共用同一个实现（`_sync_room_nat` /
            # 会话模式同步）：那一条是**每秒比对**出来的，可能晚一拍；这一条是
            # 中继**主动推**的，所以收到就立刻做一次，别等下一拍。
            #
            # "热切换后第一轮打洞白打"的根治也在这里：先把旧的一整套拆干净
            # （`watch_room_direct(False)` 会 close 掉 punch link + 状态机 +
            # 计时器），再按新模式从 §E 的第一步重新走，所以第一轮就是有效的一轮。
            self.log(
                f"[direct] the relay says the room mode changed "
                f"({data.get('reason', '') or 'CONN_MODE_CHANGED'}); "
                f"tearing down every path and rebuilding from scratch",
                kind="warn",
            )

            with contextlib.suppress(Exception):
                self.engine.watch_room_direct(False, log=self.log)

            self._punch_mode = None

            # ★ P0（2026-09-28）：这一行是"通知所有人重建"的**主动**触发点。
            #
            # 为什么不能在这里直接 `_rebuild_tunnels(mode=…)`：中继这一行是在
            # `HOST_STATE mode=<新模式>` **之前**发的（它先拆旧通路、再广播房间
            # 状态），所以此刻 `_room_conn_mode()` 还是旧模式 —— 拿它去重建等于
            # 按旧模式再建一遍（用户报的"隧道好像没有重建、连不上"）。
            #
            # 所以只打一个"待重建"标记；房间状态一到（下一次 `HOST_STATE` ->
            # `_refresh_room_ui`），`_maybe_rebuild_for_mode_change()` 用**已经
            # 确认的新模式**重建一次。每秒一次的 `_sync_session_mode` 是同一件
            # 事的兜底（会话还活着、只是模式对不上时它也会重建）。
            #
            # ★ 2026-09-29（服务端 §8.9 ADDITIVE）：中继现在会带 `was_live=0|1`
            # —— "切换前这一个成员**真的在连接吗**"。用户要的语义正是：
            #   原本空闲/未连接 -> **只换模式**，不自动重连；
            #   原本有连接（含正在打洞）-> 严格重建 + 自动重连。
            # `was_live=0` 是比"本地 running"更硬的事实，所以它优先；
            # 老中继没有这个 token -> 退回旧的 `self.running` 判据。
            was_live = str(data.get("was_live", "") or "")

            if mode_switch_should_reconnect(was_live, self.running):
                # ★ P2：记下**切换前**的房间模式（见 `_mode_rebuild_from`）。
                self._mode_rebuild_from = self._room_conn_mode()
                self._mode_rebuild_pending = True
                # ★ 期限（2026-10-03）：这个标记**必须会过期**。它曾经被留成永久真，
                #   结果把"真的 host 停服"和"掉线复位"全都吞掉了。
                self._mode_rebuild_pending_until = (
                    time.monotonic() + MODE_REBUILD_PENDING_MAX_S
                )
            else:
                self._mode_rebuild_pending = False

                if was_live == "0":
                    self.log(
                        "[mode] the relay says this member had NO connection "
                        "before the mode switch (was_live=0); only the mode "
                        "changes, no automatic reconnect",
                        kind="warn",
                    )

            self._refresh_room_ui()
            self._refresh_member_list()

        elif name == "host_state":
            # Pushed by the relay whenever the host slot or the roster changes,
            # so the UI is never more than one event behind reality.
            self._refresh_room_ui()
            # 名单变了（有人认领/释放主机、有人进出），房间列表也要跟着更新，
            # 否则"刚释放了身份，列表还显示我是主机"。
            self._refresh_room_list_soon()
            # ============================================================================
            #  host 可以换人：踢人权限必须跟着换，不许缓存
            # ============================================================================
            #
            # 用户要求：一个房间生命周期内 host 会转移，所以「踢出」按钮的可见性
            # 和「身份」列都必须在**每一次房间状态变化**时重新判定。成员列表的行
            # 控件是复用的（不重建、不闪），但每一格里写什么、按钮显不显示，
            # 都由这一刻的 RoomState 现算 —— 卸任的 host 下一次刷新就没按钮了。
            self._refresh_member_list()

        elif name == "host_taken":
            self.log(
                f"someone else claimed the host slot first: {data.get('reason', '')}",
                kind="warn",
            )
            self._refresh_room_ui()
            self._refresh_member_list()

        elif name == "room_revoked":
            # ============================================================================
            #  服务器把房间关了：本地必须【立刻】收拾干净（幽灵房的根治）
            # ============================================================================
            #
            # 真正替我们说 LEAVE 并关掉那条成员连接的是下面的
            # _teardown_room_locally() —— 它调用 engine.close_room()，
            # 也就是 room.py 的 leave()（LEAVE + 关 socket）。（room.py 里那个
            # _leave_after_revoke() 已经不在活动路径上了，别再指望它。）
            #
            # 界面不能等任何异步收尾：用户看到的是"房间已经被管理关闭了，可所有
            # 按钮还是房间内的状态，得手动点退出房间才恢复"。所以这里立刻把世界
            # 那侧停掉、把所有按钮恢复成"不在房间里"的样子。
            #
            # 另外留一个兜底定时器：万一连接没有自己退干净（网络半死、连接卡住），
            # 2.5 秒后强制把成员连接也关掉，绝不留幽灵状态。
            reason = data.get("reason", "管理员关闭了房间")

            self.log(f"room closed by the server: {reason}", kind="error")

            # 这里就弹窗：ROOM REVOKED 是**服务器关房的第一手通知**。
            # （以前只在后面的 room_closed 弹，但那条通知可能永远不来 ——
            #  界面这边会先把成员连接关掉，room.py 里发 room_closed 的那段
            #  代码跟着被取消。用户看到的就是"日志有、弹窗没有"。）
            self._teardown_room_locally(
                reason=reason,
                notify=True,
                title=tr("房间已被管理关闭"),
            )
            self._schedule_room_teardown_fallback()

        elif name == "kicked":
            # ============================================================================
            #  被踢（KICKED by=... reason=...）：只对【我这一个人】生效的关房
            # ============================================================================
            #
            # 和 ROOM REVOKED 走**同一条**拆除路径，只是措辞不同：房间还在，
            # 只是我进不去了。绝不能当普通断线处理 —— _explain_disconnect()
            # 会去问"房间还在不在"，答案是"在"（别人还在里面），于是它会判定
            # 成网络问题、把用户留在一个人家已经把他踢出去的房间里（幽灵态）。
            by = data.get("by", "") or ""
            reason = data.get("reason", "") or ""

            self.log(
                f"kicked from the room: by={by or '-'} reason={reason or '-'}",
                kind="error",
            )

            self._teardown_room_locally(
                reason="你已被踢出房间",
                notify=True,
                title=tr("你已被踢出房间"),
                message=(
                    self._kick_reason_text(by, reason)
                    + "\n\n"
                    + tr("你已被移出该房间，已自动退出。")
                ),
            )
            self._schedule_room_teardown_fallback()

        elif name == "room_closed":
            # 我们主动退出的那一步已经做完了：本地状态干净，按钮可用，
            # 可以立刻重新建房或加入别的房间 —— 不需要手动点「退出房间」。
            reason = data.get("reason", "房间已被关闭")

            self.log(f"automatically left the room: {reason}", kind="warn")

            self._teardown_room_locally(reason=reason, notify=True)

        elif name == "room_gone":
            # 中继明确说这个名字对应的房间不存在了（例如管理员刚删掉，
            # 或者中继重启过）。和 room_closed 一样清理，但用词不同。
            reason = data.get("reason", "房间不存在")

            self.log(f"room no longer exists: {reason}", kind="warn")

            self._teardown_room_locally(
                reason=reason, notify=True, title=tr("房间已不存在")
            )

        elif name in ("host_left", "host_stopped", "host_released"):
            # ============================================================================
            #  ★ 用户 2026-09-30 定稿：host 的三种情况**分开处理**
            # ============================================================================
            #
            # 中继在**成员控制连接**上发一行可识别的原因（核心转成这三个事件，
            # 见 `RoomSession._on_line`）：
            #
            #   `host_left`     = HOST_LEFT（host **闪断**，身份还在宽限期里）
            #                     -> 能复用就复用现有连接，不能用就自动重连；
            #                        原本空闲的人不重连。
            #   `host_stopped`  = HOST_STOPPED（host **主动停止开服**，位还在）
            #                     -> 断开 + 重算按钮，**不自动重连、不弹窗**。
            #   `host_released` = HOST_RELEASED（host **主动释放主机位**）
            #                     -> 断开 + 重算 + **弹窗「host 已断开连接」**，
            #                        不自动重连。
            #
            # 设计里**没有"房主已离线"这个概念**：房主真离开房间 = 房间直接销毁
            # （ROOM REVOKED 那条路），不会走到这里。
            kind = {
                "host_left": "left",
                "host_stopped": "stopped",
                "host_released": "released",
            }[name]

            # ★ P4（2026-10-03 热切换分析）：模式热切换时，**如果我是 host**，
            #   我自己的重建会先 `stop_session()` -> 关掉 `HOST <room>` 控制连接 ->
            #   中继于是给**所有人**广播 `HOST_STOPPED`（"host 主动停止，别重连"）。
            #   但这不是用户停服，是我在按新模式重建：谁要是把它当"host 停了"，
            #   就会停会话、把按钮放掉、进入 host_down（本房所有人一起被放掉）。
            #   所以模式重建窗口里/待重建标记还在时，这类事件只记日志、不改状态。
            # ★ 2026-10-03 修回归：以前这里还看 `_mode_rebuild_pending`，而那个
            #   标记**可能被留成永久真**（新模式广播没到就没人清它）—— 于是
            #   host 无论什么原因断开，这一支都被吞掉，**其他 player 永远不断开**。
            # ★ 2026-10-04 修回归（本轮）：`_rebuild_in_flight` 也**不能**单独当
            #   掩码。它只说明"我这边有一次隧道重建在跑"（自动重连、换模式都算），
            #   与"host 走没走"无关；拿它吞 host-gone，玩家正在重建时收到真 host
            #   停服就会被整条推迟（回归网直接钉住：**玩家正在重建时收到
            #   host_stopped / host_released / host_left，仍然必须当场断开 +
            #   释放按钮**，见 `tests/host_offline_owner_test.py`）。
            #   用户口径（2026-10-03 重申）：host 一走，房内所有人都要断开；
            #   **唯一的例外**是 host 自己的模式热切换 —— 那是一条**有界窗口**，
            #   由下面两个会自己结束的标记表达（窗口一关就在下一拍补跑，
            #   还有 `HOST_GONE_DEFER_MAX_S` 兜底，见 `_flush_pending_host_gone()`）：
            #     * `_mode_switch_silent`：静默重建窗口（`MODE_SWITCH_SILENT_MAX_S`）；
            #     * `_mode_rebuild_pending`：等房间广播里的新模式（有期限）。
            #   模式重建一开始就**同时**武装静默窗口（`_rebuild_tunnels` 里
            #   `silent_next` 那一段），所以"host 自己切模式"这条路一点没丢。
            # 有界的"待重建"：标记在 + **未过期**才算数（分析结论 c）。
            pending_armed = getattr(self, "_mode_rebuild_pending", False) and (
                time.monotonic()
                <= getattr(self, "_mode_rebuild_pending_until", 0.0)
            )

            # 静默窗口也要**按期限**算（分析建议）：闩锁住的标志不许吞掉真实停服。
            silent_armed = getattr(self, "_mode_switch_silent", False) and (
                time.monotonic()
                <= getattr(self, "_mode_switch_silent_until", 0.0)
            )

            # ============================================================
            #  ★ v3.9.16：**不再丢弃，只推迟**
            # ============================================================
            #
            # 以前窗口期内这类事件被降级成一行日志 —— 那正是"host 断开后房间内
            # 其余人不断开"的直接原因（用户 2026-09-30 定稿 → 10-02 → 10-03
            # 三次重申，实测仍未生效）。
            #
            # 现在拆成两层：
            #   * **断不断**由核心层决定 —— `RoomSession` 收到这几个码就调
            #     `MCLinkEngine._on_host_gone_from_room`，会话与直连通路当场停掉，
            #     界面这三道窗口管不着（见 core/room.py 的 `_host_gone_teardown`）；
            #   * 界面这层只剩"提示什么时候显示"：窗口期内**缓存**最后一次
            #     host-gone，等窗口结束（或兜底超时）`_flush_pending_host_gone()`
            #     立刻补跑。事件因此不可能被吃掉。
            if silent_armed or pending_armed:
                self._defer_host_gone(kind, data.get("reason") or "")
            else:
                self._on_host_gone(kind, data.get("reason") or "")

        elif name == "room_lost":
            # ============================================================================
            #  掉线了，但房间**还在**（中继那边查过了）
            # ============================================================================
            #
            # 用户报的问题：「掉线之后开始连接立刻变回可用，但房间那一片还是
            # 老状态，得手动点一次退出房间才复位」。
            #
            # 原因是这里只刷新了连接那一侧：引擎里那个房间对象还在，界面就继续
            # 认为"我在房间里"。两边用的根本不是同一个事实来源。
            #
            # 现在的做法是**把事实重新对上**，而不是就地改界面：
            #   1. 先停掉世界那侧（数据通道已经断了，界面不该继续显示在转发）；
            #   2. 延迟一小会儿自动重新进房（顺带把主机位置抢回来）；
            #   3. 成功 → 房间状态由 room.py 的广播重新填满，两边一致；
            #      失败 → 彻底收拾干净，所有按钮回到"不在房间"。
            #
            # 关键是第 3 步的兜底：无论重连成功还是失败，**都不会再留下一个
            # 看起来还在房间、实际已经掉线的界面**。
            reason = data.get("reason", "")

            if reason:
                self.log(f"connection to the room lost: {reason}", kind="warn")

            # ================================================================
            #  ★ 我的房间连接丢了，而且中继给的原因是"host 那边"
            # ================================================================
            #
            # 设计里**没有"房主离线"这个概念**（用户 2026-09-30）：房主真离开
            # 房间 -> 房间直接销毁，那是 `ROOM REVOKED` 那条路。所以这里只可能是
            # host 闪断 -> 按 `_on_host_gone("left")` 处理（能复用就复用现有
            # 连接、不能用就自动重连；空闲态不重连）。
            if self._is_no_host_reason(reason):
                # 我的房间连接丢了、中继给的原因是"host 那边"：按 host 闪断
                # 处理（能复用就复用、不能用就自动重连），**不**再进
                # "等待房主上线"那个状态（用户 2026-09-30：设计里没有这个说法）。
                self._on_host_gone("left", reason)
                self._refresh_room_ui()
                self._schedule_room_recovery()
                return

            # ---- 普通掉线：世界那侧要停，会话要自动重建（用户已批准的框架） ----
            session_was_live = bool(self.running)

            if session_was_live:
                # 只停世界那侧，**不**走 on_stop()：用户没有点停止，这是
                # "非用户意图的断开"，要走自动重连（见 AUTO-RECONNECT-PLAN.md）。
                # 标成"有意的"会让 `_can_auto_reconnect` 直接返回 False。
                self.log(
                    "the room connection dropped while the session was live; "
                    "the world side is stopped and the session will be rebuilt",
                    kind="warn",
                )

                # ★ 顺序是 P0：`_trigger_auto_reconnect()` 的闸门看的就是
                # `self.running`（"掉线前我是不是在连接"），所以**必须先起重连、
                # 再清 running**。以前的顺序反了 —— 先清 running 再调用，闸门
                # 直接返回 False，整条"被动意外掉线自动重连"是**死代码**
                # （日志里写着 "the session will be rebuilt"，实际一次都没重建）。
                self._trigger_auto_reconnect(
                    f"room connection lost: {reason or 'unknown'}"
                )

            self.running = False
            self._refresh_room_ui()

            self._schedule_room_recovery()

        elif name == "session_ended":
            # ============================================================
            #  SESSION 探活（PROTOCOL §8.10）：**不是闪断**
            # ============================================================
            #
            # 被动掉线之后，成员连接已经断了，`KICKED` / `AUTH_INVALID_TOKEN`
            # 这类被动推送再也收不到。核心因此先做一次只读探活；这一条就是
            # "探活说我的房间身份已经结束"（auto_reconnect=0）：
            #
            #   KICKED / LEFT / TIMEOUT / ROOM_CLOSED / AUTH_*
            #
            # 用户口径（设计基准 §C / AGENTS.md 规则 6 补充）：**按退出房间处理**
            # —— 不自动重连、收拾干净、把话说清楚；`relogin=1` 时提示需要重新
            # 登录。自动重连仍然只允许「模式热切换」+「真·被动闪断」两类，这条
            # 路径把第二类里"其实是被踢/失效"的那些挡在外面。
            code = str(data.get("code", "") or "")
            relogin = bool(data.get("relogin", False))
            fallback = str(data.get("reason", "") or "")

            self.log(
                f"[reconnect] the relay's SESSION probe says my room identity "
                f"is gone (reason={code or '-'}, "
                f"relogin={1 if relogin else 0}); leaving the room "
                f"(no automatic reconnect)",
                kind="warn",
            )

            title, message = session_end_notice(code)

            # 已经有人告诉过用户了（例如 KICKED 被动推送先到、kicked 分支已经
            # 弹过窗并收拾干净）-> 这一条只负责把房间/登录态对齐，**不再弹第二个
            # 对话框**（用户要的是"告知一下"，不是被连点两次）。
            already_notified = bool(
                getattr(self, "_room_close_notified", False)
            )

            # 先退干净房间（不弹这个通用窗），保证按钮/房间状态复位。
            self._teardown_room_locally(
                reason=fallback or title,
                notify=False,
            )

            if relogin and not already_notified:
                # token 已失效：走"需要重新登录"那条既有路径（它自己会弹一次
                # 窗并把会话停掉）—— 只弹一次，绝不两个对话框叠着。
                self._handle_auth_lost(
                    str(data.get("detail", "") or "") or code
                )
            elif relogin:
                # 已经告知过房间没了，但登录态确实失效了：清掉它并留一行日志。
                self.auth = None
                self._refresh_account_ui()
                self.log(
                    "the relay says the sign-in is no longer valid; "
                    "please sign in again",
                    kind="warn",
                )
            elif not already_notified:
                with contextlib.suppress(Exception):
                    messagebox.showwarning(
                        title, message, parent=self.root
                    )

                self._room_close_notified = True

            self._schedule_room_teardown_fallback()

        elif name == "listen_failed":
            # 本地代理端口绑不上 = 会话核心自己退出（用户列的非用户意图断开之一）。
            # 以前只有一行日志，界面还显示"运行中" —— 现在走自动重连（换一次
            # 绑定仍失败就会用尽窗口并进终态，不会无限重试）。
            reason = str(data.get("reason", "") or "")

            self.log(f"failed to listen on the local port: {reason}", kind="error")

            if self.running:
                self._trigger_auto_reconnect(f"listen failed: {reason}")

        elif name == "listening":
            self.log(
                f"local proxy listening on {data.get('endpoint')}", kind="ok"
            )

        elif name == "minecraft_connected":
            self.log("Minecraft client connected to the local proxy", kind="ok")

        elif name == "relay_connected":
            self.log("data channel established with the host; forwarding data", kind="ok")

        elif name == "relay_error":
            self.log(f"forwarding failed: {data.get('reason', '')}", kind="error")

        elif name == "minecraft_disconnected":
            self.log("Minecraft client disconnected", kind="dim")

    def _teardown_room_locally(self, reason="", notify=False, title=None,
                               message=None):
        """
        房间没了（被管理关闭 / 被踢 / 不存在了）→ 把本地恢复到"不在任何房间里"。

        ============================================================================
          这个函数存在的唯一理由：不再出现"幽灵房"
        ============================================================================

        What the user saw before this existed:

          * the room was closed by an admin, the client kept looking connected
          * the buttons stayed in the "I am in a room" state
          * 开始连接 was still clickable, and only THEN did it say the room was
            closed
          * and the user still had to press 退出房间 by hand to get a clean UI

        So the teardown is unconditional and complete, whatever the relay said:
        stop the Minecraft side, drop the room session, clear the local room
        state, and re-render every button from scratch.

        `notify` 控制要不要弹窗告知用户。**只弹一次**：中继会先推
        ROOM REVOKED、随后客户端自己退出时又有一个 room_closed，两次都弹就是
        连续两个对话框（用户要的是"告知一下"，不是被连点两次）。

        `message` 是给"被踢"这类需要带发起者/原因的通知用的完整正文；给了就
        原样显示（调用方已经逐句 tr() 过），不再套默认那句。
        """
        if self.running:
            with contextlib.suppress(Exception):
                self.on_stop()

        # 成员连接：正常路径上 room.py 已经自己 LEAVE + 关掉了；这里再关一次
        # 是幂等的，为的是兜住"通知来了但连接没退"的情况。
        with contextlib.suppress(Exception):
            self.engine.close_room()

        self.room_var.set("")

        with contextlib.suppress(Exception):
            self.room_state_label.configure(
                text=tr("未加入房间"), fg=FG_MUTED
            )

        # 房间没了：STARTED 的"已上报"记忆一起清掉（下次进房重新报）。
        self._started_reported = None
        self._mode_rebuild_pending = False

        # 按钮状态重新算一遍（这是关键：以前就是这里漏了）。
        self._refresh_room_ui()
        self._refresh_room_list_soon()

        # 人在房间里被踢掉：成员窗口里的行要跟着空掉（窗口本身留着，用户可能
        # 还想看"刚才都有谁"）。
        self._refresh_member_list(force=True)

        if notify and not self._room_close_notified:
            self._room_close_notified = True

            with contextlib.suppress(Exception):
                body = message or (
                    tr(reason or "房间已被关闭")
                    + "\n\n"
                    + tr("你已经自动退出该房间，可以直接重新创建或加入房间。")
                )

                messagebox.showwarning(
                    title or tr("房间已被关闭"),
                    body,
                    parent=self.root,
                )

    def _schedule_room_recovery(self, delay_ms=900):
        """
        掉线之后（房间还在）：等一下、自动重新进房，失败就把本地收拾干净。

        ============================================================================
          为什么不能"就地改界面"
        ============================================================================

        界面上"我在不在房间里"只有一个来源：引擎里的房间对象。掉线之后那个对象
        还在，所以界面会一直显示在房间里，而连接那侧已经复位 —— 这就是用户看到的
        「开始连接能用了，房间那边还得手动退出」。

        所以这里动的是**事实**：重新进房——成功，房间状态被广播重新填满；失败，
        调用 _teardown_room_locally() 把房间对象扔掉。两条路都不会留下
        "看起来还在房间、其实已经掉线"的界面。

        用后台线程做：open_room() 是阻塞的（最长十几秒），不能卡住界面。
        """
        session = self.engine.room_current()

        if session is None:
            # 引擎里已经没有房间了：直接复位就行，不用重连。
            self._refresh_room_ui()
            self._refresh_room_list_soon()
            return

        # 掉线那一刻我们是不是主机 —— 重连成功了要把位置抢回来。
        with contextlib.suppress(Exception):
            self.engine._room_was_host = bool(session.state.is_host)

        def start():
            # 记下要重连的房间名（重连用得到，界面上也要显示）。
            state = self.engine.room_state()
            name = ""

            with contextlib.suppress(Exception):
                name = (state.room_name or state.room or "") if state else ""

            self.log(
                f"与房间「{name}」的连接断了，正在自动重连…"
                if name
                else "与房间的连接断了，正在自动重连…",
                kind="warn",
            )

            def worker():
                ok, message = self.engine.auto_rejoin()

                def finish():
                    if ok:
                        self.log(message, kind="ok")
                        self._room_close_notified = False
                        self._refresh_room_ui()
                        self._refresh_room_list_soon()
                        return

                    # 重连不上：房间确实回不去了，把本地收拾干净 ——
                    # 这一步就是"按钮状态跟着复位"，不再需要手动退出房间。
                    self.log(f"automatic rejoin failed: {message}", kind="warn")

                    self._teardown_room_locally(
                        reason=f"与房间的连接已断开（{message}）",
                        notify=True,
                        title=tr("与房间的连接已断开"),
                    )

                with contextlib.suppress(Exception):
                    self.root.after(0, finish)

            threading.Thread(
                target=worker, name="mclink-room-rejoin", daemon=True
            ).start()

        with contextlib.suppress(Exception):
            self.root.after(delay_ms, start)

    def _schedule_room_teardown_fallback(self, delay_ms=2500):
        """
        兜底：ROOM REVOKED 之后如果成员连接没有自己退干净，也要收干净。

        中继那边是"通知 → 等客户端自己退 → 超时强断"，客户端这边就不该赌
        哪一步一定成功。等一小会儿（给对方一个体面 LEAVE 的机会），如果本地
        还觉得自己在房间里，就强行收拾掉。
        """

        def check():
            with contextlib.suppress(Exception):
                if getattr(self, "_rebuilding", False):
                    self.root.after(delay_ms, check)
                    return

                self.engine.drain_events()

                if self.engine.room_current() is not None:
                    self.log(
                        "the connection did not exit by itself after the room-closed notification; local state was force-cleaned.",
                        kind="warn",
                    )
                    self._teardown_room_locally(notify=False)

        with contextlib.suppress(Exception):
            self.root.after(delay_ms, check)

    def _handle_auth_lost(self, reason=""):
        """The relay rejected our token: stop and require a fresh login."""
        self.auth = None
        self._refresh_account_ui()

        if self.running:
            try:
                self.engine.stop_session()
            except Exception:
                pass

            self.running = False
            self._set_enabled(self.start_button, True)
            self._set_enabled(self.stop_button, False)
            self.delete_room_button.configure(state="disabled")

        # 会话在这里被停掉了，_refresh_status 再也看不到 ERROR 状态
        # （引擎里已经没有会话了），所以页眉要在这里亲手改掉 —— 否则它会
        # 一直显示「已连接中转服务器」，而我们已经把连接关掉了。
        with contextlib.suppress(Exception):
            self.server_status_label.configure(
                text=tr("● 服务器不可用"), fg=BAD
            )

        messagebox.showwarning(
            "需要重新登录",
            f"{reason or '登录状态已失效'}\n\n请重新登录后再连接。",
            parent=self.root,
        )

    def _append_logs(self):
        lines = []

        for _ in range(60):
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break

        if not lines:
            return

        self.log_text.configure(state="normal")

        for stamp, text, kind in lines:
            self.log_text.insert("end", f"[{stamp}] {text}\n", kind or "normal")

        # Keep the widget bounded.
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")

        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _direct_status_view(self, snapshot):
        """
        ONE dict for every row that describes the direct path.

        Merges three sources in order of how live they are:

          1. the session snapshot (a running connection re-measures constantly);
          2. the ROOM's punch, which starts the moment we join a STUN room and is
             therefore the only thing that can answer before a game exists;
          3. the room's mode, so a STUN room shows STUN rows even while the local
             selector still reads relay.

        Anything that renders a direct-path row goes through here. Two rows
        reading two objects is how the owner got a screenshot where 通路 said
        "直连尝试中" directly above 打洞进度 saying "已直连 <ip:port>".
        """
        room_punch = self.engine.room_direct_state() or {}
        view = dict(snapshot)

        for key in ("direct_step", "direct_via", "direct_peer", "direct_attempts",
                    "direct_carrying", "flow"):
            if not view.get(key):
                view[key] = room_punch.get(key)

        # ============================================================
        #  入站判定：以【房间】那次探测为准，不是会话的
        # ============================================================
        #
        # 3.0.x 起，入站探测是「点开始连接」时房间那次打洞里跑的
        # （`room_direct._punch` → measure → probe_inbound），会话自己**不再**
        # 探测。可是会话快照里那两个字段是初始值 "unknown" / 一句英文 note ——
        # 它们是**非空字符串**，所以上面那句 `if not view.get(key)` 拦不住它们，
        # 会话的"unknown"就把房间真实测出来的结论顶掉了。
        #
        # 结果就是用户在日志里明明看到
        #   [nat] the relay's probe did not arrive from a host we never wrote to
        #   → 可打
        # 屏幕上却写着「未测（relay did not confirm the probe）」。
        # 对这两个字段，房间的结论优先（它有值就用它的）。
        for key in ("inbound_state", "inbound_note"):
            if room_punch.get(key):
                view[key] = room_punch[key]

        # ★ 用户 2026-10-01：全锥证据也走这里（纯展示：NAT 那两行要用它把
        #   「NAT1/2 未定」升级成「NAT1 全锥形」）。
        if room_punch.get("eif_proof"):
            view["eif_proof"] = True

        # Not a "falsy wins" field: it is a fact about the punched link, and it
        # decides whether the card says "the punch has not started" or "the
        # link is up and carrying".
        view["stun_ready"] = bool(room_punch.get("stun_ready"))
        view["punching"] = bool(room_punch.get("punching"))

        # 房间模式是权威（STUN / AUTO 都要打洞）。会话快照里的 `mode` 可能是
        # 建会话那一刻的旧值，这里让展示跟随房间。
        room_mode_now = self._room_conn_mode()
        if room_mode_now in (MODE_STUN, MODE_AUTO) and view.get("mode") != room_mode_now:
            view["mode"] = room_mode_now

        return view

    def _status_peer_latency(self, view, room_punch):
        """
        运行状态卡片「对端延迟」那一格的 (rtt_ms, applicable)。

        适用 = STUN 直连且**至少一条对端链路已打通**。数据源和「通路」「打洞
        进度」是同一份 `room_direct` 状态，所以同一屏上的几行不会互相矛盾。

        选中哪一条：优先 `direct_peer`（「通路」正在显示的那个对端），否则第一
        条已打通的。一条都没有 -> (None, False) -> 界面显示 `N/C`。
        """
        links = [
            item for item in (room_punch.get("links") or [])
            if item.get("ready")
        ]

        if not links:
            return None, False

        wanted = view.get("direct_peer") or ""
        chosen = None

        for item in links:
            if wanted and item.get("peer") == wanted:
                chosen = item
                break

            if chosen is None:
                chosen = item

        if chosen is None:
            return None, False

        return chosen.get("rtt_ms"), True

    def _refresh_status(self):
        snapshot = self.engine.snapshot()

        if not snapshot:
            snapshot = self.last_snapshot

        self.last_snapshot = snapshot

        # 「Minecraft 连接地址」跟着状态实时更新（玩家模式显示 127.0.0.1:端口）
        self._update_address(snapshot)

        state = snapshot.get("state", STATE_IDLE)
        role = snapshot.get("role")

        state_text = snapshot.get("state_text") or snapshot.get("state") or "—"

        state_color = {
            STATE_IDLE: FG_DIM,
            STATE_CONNECTING: WARN,
            STATE_WAITING: WARN,
            STATE_ONLINE: OK,
            STATE_ERROR: BAD,
        }.get(state, FG)

        self.status_vars["state"].set(tr(state_text))

        # ---- 通路（V3）----
        #
        # 判定逻辑在 nat_diag_path_text（模块级纯函数，可离线测试）。
        # 这个显示错过两次，都是同一个方向：**断言了一条并不存在的通路**。
        connected = state in (STATE_WAITING, STATE_ONLINE)

        # ONE merged dict for every direct-path row, built BEFORE the first row
        # reads it. They used to read different objects and therefore disagreed
        # on the same screen: 通路 said "直连尝试中" while 打洞进度 said
        # "已直连 192.0.2.14:60831".
        view = self._direct_status_view(snapshot)

        # Read the ROOM's punch too, exactly like the two rows below do.
        #
        # This row used to look only at the session snapshot, so a host whose
        # punch had already succeeded but who had no game session yet saw:
        #
        #     通路       直连尝试中                    <- this row, session-only
        #     打洞进度   已直连 192.0.2.14:60831   <- the other rows, merged
        #
        # Two rows about the same fact, disagreeing on screen. The owner's
        # screenshot. One source of truth for both.
        path_text, hint = nat_diag_path_text(
            getattr(self.config, "mode", "relay"),
            view.get("direct_via"),
            connected,
            peer=view.get("direct_peer") or "",
            punch_ready=bool(view.get("stun_ready")),
            punching=bool(view.get("punching")),
            carrying=bool(view.get("direct_carrying")),
            # §G 第 1 层：有 flow 汇总时用「n/m」口径（用户点名要的版式）。
            # `view` 已经把会话与房间两份 `flow` 合过了（见 `_merge_direct_view`）。
            flow=view.get("flow") or None,
        )

        path_color = {
            "ok": OK, "warn": WARN, "bad": BAD,
            "dim": FG_DIM, "muted": FG_MUTED,
        }.get(hint, FG)

        self.status_vars["path"].set(tr(path_text))
        self._set_status_color("path", path_color)

        # ---- 网络类型 / 直连尝试（取代原来的独立诊断卡片）----
        #
        # Three sources, in order of how LIVE they are:
        #   1. the session's snapshot (a running connection re-measures every
        #      round and every NAT_RECHECK_S);
        #   2. the ROOM monitor, which measures from the moment we join a STUN
        #      room -- so the card is live before "start connection" is pressed;
        #   3. the last profile we ever saw, so leaving a room does not blank
        #      the row out.
        # The ROOM-level punch is the source of truth while we are in a STUN
        # room: it starts before any session exists, so it is the only thing
        # that can report what is happening during those first seconds.
        room_punch = self.engine.room_direct_state() or {}

        nat_profile = (
            snapshot.get("nat_profile")
            or self.engine.room_nat_profile()
            or room_punch.get("nat_profile")
            or self._nat_profile
        )
        # ★ 用户 2026-10-01：把"对端首包证明的全锥"塞进 snapshot（**只读**：
        #   它只是一个显示用的布尔值，进界面的 NAT 文案/颜色，不进任何决策）。
        with contextlib.suppress(Exception):
            snapshot["eif_proof"] = bool(room_punch.get("eif_proof"))

        # ★ 两行同源（分析结论 B）：NAT 行也必须吃**合并后**的 view，
        #   否则 raw snapshot 的 inbound_state 还是初始 "unknown"，
        #   会出现"NAT 行说端口受限、入站行说中等"这种自相矛盾。
        nat_rows = nat_diag_status_values(
            nat_profile, view, getattr(self.config, "mode", MODE_RELAY)
        )

        for key, (text, hint_color) in nat_rows.items():
            # 「直连尝试」那一行已经不存在了（用户要求删除），
            # 所以 nat_rows 里即使还带着 "direct" 也不再写入界面。
            if key == "direct":
                continue

            if key in self.status_vars:
                self.status_vars[key].set(text)
                self._set_status_color(key, hint_color)

        # ---- 入站可达 / 打洞进度 ----
        #
        # 入站那一档要用他那一套（NAT_FILTER_LABEL 的四个挡位），而合成规则
        # 需要两个输入：STUN 判出的过滤行为（就在上面那个 nat_profile 里）和
        # 中继探测的结论（在 view 里）。所以这里把 profile 的 filtering 传进去。
        inbound_text, inbound_hint = inbound_status_text(
            view, getattr(nat_profile, "filtering", None)
        )
        step_text, step_hint = punch_step_text(view)

        self.status_vars["inbound"].set(tr(inbound_text))
        self._set_status_color("inbound", inbound_hint)
        self.status_vars["punch"].set(tr(step_text))
        self._set_status_color("punch", step_hint)

        # 3.0.9：一人一行（一个对端一条洞）。数据来自房间打洞协调器，和上面
        # 「通路」「打洞进度」是同一份来源，所以几行不会互相打架。
        #
        # 审计问题 2：**只有真的在打洞时才出行**。`room_punch["links"]` 现在
        # 已经带了这个门（room_direct.state()），这里再显式传一次 `punching`，
        # 多一道保险 —— relay 模式下绝不许出现「打洞中」。
        self._render_hole_rows(
            room_punch.get("links"), punching=bool(room_punch.get("punching"))
        )

        # 3.0.9：房间成员列表窗口开着的话，跟着这一拍一起刷新（没开就是空操作）。
        self._refresh_member_list()

        # The state text is the single most important thing on this card, so
        # it gets the strongest colour treatment.
        self._set_status_color("state", state_color)

        active = bool(role)

        if role == "host":
            self.status_vars["role"].set("主机 Host")
            # 本机视角：中继实际分配给我的 Minecraft 会话数 / 房间容量。
            #
            # 这一行以前挂在「已加入世界」上（然后又被下面那句无条件覆盖），
            # 于是房主端和玩家端对同一个房间显示两个不同的数字。现在它是
            # 自己的一行，谁也不会覆盖谁；容量也还在。
            self.status_vars["local_links"].set(
                f"{snapshot.get('players', 0)} / "
                f"{snapshot.get('max_players') or proto.DEFAULT_MAX_PLAYERS}"
            )
            # ================================================================
            #  ★ 用户 2026-09-29 第 6 条：这个数字的**名字**必须说清它是什么
            # ================================================================
            #
            # `tunnels_ready` 数的是**空闲等待中的**隧道（中继答了 WAITING、
            # 还没分配玩家）。旧文案写「隧道 n/m 就绪」，用户读成"我有 n 条隧道
            # 可用"，于是玩家一挂上来（idle -> 分配）数字就往下跌，
            # 「6/7、7/8 一直跳动，突然变成 0，又变成 1」——看起来像隧道掉了，
            # 其实是"空闲的少了、在用的多了"。
            #
            # 用**已有的**词「空闲隧道」（房间列表那列就是这个 key），并且把
            # 正在服务的条数一起说出来 —— 一升一降互相解释，不会再被误读。
            _idle = int(snapshot.get("tunnels_ready", 0) or 0)
            _total = int(snapshot.get("tunnels_total", 0) or 0)
            _busy = int(snapshot.get("players", 0) or 0)

            self.status_vars["traffic"].set(
                traffic_ledger_text(snapshot)
                + f"  |  {tr('空闲隧道')} {_idle}/{_total}"
                + f"  |  {tr('已加入世界')} {_busy}"
            )
        elif role == "player":
            self.status_vars["role"].set("玩家 Player")
            # 本机视角：已经和中继配对成功的 Minecraft 连接数。
            self.status_vars["local_links"].set(
                str(snapshot.get("active_connections", 0))
            )
            self.status_vars["traffic"].set(traffic_ledger_text(snapshot))
        else:
            self.status_vars["role"].set("—")
            self.status_vars["players"].set("—")
            self.status_vars["local_links"].set("—")
            # 审计问题 6：累计流量是**进程级**的，离开房间/没会话时也要显示
            # （只是没有会话相关的隧道信息）。
            self.status_vars["traffic"].set(traffic_ledger_text(snapshot))

        # 实时速度：任何模式 / 有没有会话都显示真实数字（没流量就是 0），
        # 不再是 `—`（审计问题 6）。
        self.status_vars["speed"].set(traffic_speed_text(snapshot))

        # 状态卡里的「房间」显示的是【房间名】（人在房间卡片里输入的那个），
        # 不是协议里的房间 ID —— 会话里的 snapshot["room"] 是房间 ID。
        room_state = self.engine.room_state()

        if room_state is not None and room_state.display:
            self.status_vars["room"].set(room_state.display)
        else:
            self.status_vars["room"].set(snapshot.get("room", "—") or "—")

        # ---- V3 counts ----
        #
        # Three different questions, deliberately shown separately:
        #   房间内在线 = how many people are IN the room (owner included,
        #                regardless of whether they are playing)
        #   已加入世界 = how many are actually in somebody's world (the host
        #                counts as one, plus every paired player) -- the RELAY's
        #                number, so host and players read the same value
        #   本地连接   = the local view: how many Minecraft sessions this machine
        #                has with the relay / the room capacity
        #
        # The room counts come from the room membership channel, not from the
        # Minecraft session, so they are correct even before anybody starts
        # playing.
        if room_state is None:
            self.status_vars["room_members"].set("—")
        else:
            self.status_vars["room_members"].set(
                f"{room_state.members} 人"
                + ("　(你是房间所有者)" if room_state.is_owner else "")
            )

        # ---- 已加入世界：只有一个来源 ----
        #
        # 这个数字必须和别的客户端显示的一样，所以它以中继为准：HOST_STATE
        # 里的 world= 是中继自己数出来的"真的在世界里"的人数（房主算一个，
        # 每条完成握手的隧道再加一个）。房主本地那条 len(active_sessions)
        # 只是"本机有几个会话"，它属于「本地连接」那一行，不能拿来当
        # 「已加入世界」——同一个房间两个客户端显示两个数字就是这么来的。
        #
        # 万一拿不到房间成员通道（旧中继没有 V3、或者还没进房间）才退回
        # 本会话自己的数字；一个数字都没有时显示「—」，绝不显示 0。
        if room_state is not None:
            self.status_vars["players"].set(str(room_state.world_players))
        else:
            world = snapshot.get("players")
            self.status_vars["players"].set(
                str(world) if world is not None else "—"
            )

        # ---- 状态文字的颜色（用户要"正常绿 / 警告黄 / 错误红"更明显）----
        #
        # 只改颜色，不改文字：一眼能看出当前是正常还是出问题了。
        with contextlib.suppress(Exception):
            state_text = str(self.status_vars["state"].get())

            if any(word in state_text for word in ("错误", "失败", "异常")):
                self.status_labels["state"].configure(fg=BAD)
            elif any(word in state_text for word in ("连接中", "等待", "重连", "测试")):
                self.status_labels["state"].configure(fg=WARN)
            elif any(word in state_text for word in ("运行中", "在线", "已连接")):
                self.status_labels["state"].configure(fg=OK)
            else:
                self.status_labels["state"].configure(fg=FG)

            # 延迟：优秀绿 / 一般黄 / 差红
            ping_text = str(self.status_vars["ping"].get())

            if "ms" in ping_text:
                with contextlib.suppress(Exception):
                    value = float(ping_text.split()[0])

                    if value < 80:
                        self.status_labels["ping"].configure(fg=OK)
                    elif value < 200:
                        self.status_labels["ping"].configure(fg=WARN)
                    else:
                        self.status_labels["ping"].configure(fg=BAD)
            else:
                self.status_labels["ping"].configure(fg=FG_MUTED)

        # ---- latency -------------------------------------------------
        #
        # The player side DOES have a measurement: its local proxy talks to the
        # relay too, and the log prints the number. The card used to force "—"
        # for players anyway, which looked like "the feature is missing here"
        # while the log right below it showed a value.
        #
        # So: show whatever the session measured. Only when there is genuinely
        # no reading does it stay a dash.
        ping = snapshot.get("relay_ping_ms")

        # 玩家端那条数据连接上不能发 PING（CONNECTED 之后全是原始字节），
        # 所以退回用 presence 通道量出来的往返时间 —— 都是"本机 ↔ 中继"。
        if role == "player" and ping is None:
            ping = self.engine.presence_rtt_ms()

        # 房间成员连接自己的 PING/PONG 也是**真实**的「本机 ↔ 中继」往返（中继
        # 把 token 原样回给我们，见 room.py 的 PONG 处理）。会话还没开始 / 玩家
        # 那条连接不能发 PING 时用它 ——「中转延迟始终有显示」就是靠这一条兜底。
        if ping is None:
            ping = self.engine.room_rtt_ms()

        if role == "player" and ping is None:
            self.status_vars["ping"].set("—")
            with contextlib.suppress(Exception):
                self.ping_label.configure(fg=FG_MUTED)
        else:
            self.status_vars["ping"].set(fmt_ping(ping))

            quality_text, quality_color = ping_quality(ping)

            if active and ping is not None:
                self.status_vars["ping"].set(
                    f"{fmt_ping(ping)}  ({quality_text})"
                )

            with contextlib.suppress(Exception):
                self.ping_label.configure(
                    fg=quality_color if active else FG_MUTED
                )

        # ---- 对端延迟（玩家 ↔ 主机）----
        #
        # 和中转延迟分成两行是刻意的：中转延迟总是有（没房间/没会话才是「—」），
        # 对端延迟**不适用时写 N/C**，不拿中转的数字冒充直连。
        # `view`/`room_punch` 和上面「通路」「打洞进度」是同一份数据。
        peer_rtt, peer_applicable = self._status_peer_latency(view, room_punch)

        self.status_vars["peer_ping"].set(
            peer_latency_text(peer_rtt, peer_applicable)
        )
        self._set_status_color(
            "peer_ping",
            ping_quality(peer_rtt)[1]
            if (peer_applicable and peer_rtt is not None)
            else FG_MUTED,
        )

        if self.running and role is None:
            # Session is gone (stopped): do not leave stale speed numbers.
            self.status_vars["speed"].set("—")

        # ---- colour by meaning ----
        #
        # Everything used to be plain white, so "运行中" and "错误" looked the
        # same until you actually read them. Role and room get the accent
        # colour, the live numbers get green when they are actually moving,
        # and a dash means "nothing to show" and stays muted.
        self._set_status_color("role", ACCENT if active else FG_MUTED)

        room_text = self.status_vars["room"].get()
        self._set_status_color(
            "room",
            ACCENT if (active and room_text not in ("—", "")) else FG_MUTED,
        )

        moving = state == STATE_ONLINE

        self._set_status_color("room_members", ACCENT if room_state else FG_MUTED)
        self._set_status_color("players", OK if (active or room_state) else FG_MUTED)
        self._set_status_color("local_links", OK if active else FG_MUTED)
        self._set_status_color("speed", OK if moving else FG_MUTED)
        self._set_status_color("traffic", FG_DIM if active else FG_MUTED)

        # ---- 「删除 / 关闭房间」按钮什么时候能点 ----
        #
        # 以前只有"正在连接"时才能点，于是在房间里（还没开始连接、或者已经
        # 停止）就完全点不了 —— 看起来像客户端没有删房功能。
        # 有账号 + （正在连接 或 在某个房间里）就够了；真正能不能删由中继
        # 判定（所有者或主机），被拒绝时会把服务器的原话显示出来。
        can_delete = (self.auth is not None) and (
            self.running or room_state is not None
        )

        self.delete_room_button.configure(
            state="normal" if can_delete else "disabled"
        )

        # Ping colour is decided by ping_quality() above; when there is no
        # reading at all it must not stay green from a previous tick.
        if role == "player" or ping is None or not active:
            self._set_status_color("ping", FG_MUTED)

        # Acknowledge the state in the header.
        #
        # 错误状态必须【先】判断：会话自己停了的时候 self.running 还是 True，
        # 后面两个分支都不命中，页眉就会一直挂着「已连接中转服务器」——
        # 用户看到的是"连着呢"，实际中继已经把我们拒之门外。
        if state == STATE_ERROR:
            self.server_status_label.configure(
                text=tr("● 服务器不可用"), fg=BAD
            )
        elif self.running and state == STATE_ONLINE:
            self.server_status_label.configure(
                text=tr("● 已连接中转服务器"), fg=OK
            )
        elif self.running and state == STATE_CONNECTING and role:
            self.server_status_label.configure(
                text=tr("● 正在连接 ..."), fg=WARN
            )

        # Live hint with the exact address for players.
        if role == "player" and state == STATE_ONLINE:
            endpoint = snapshot.get("local_endpoint", "")
            if endpoint:
                self.connect_hint.configure(
                    text=tr(f"在 Minecraft 中连接：{endpoint}"),
                    fg=OK,
                )
        elif role == "host" and state == STATE_ONLINE:
            self.connect_hint.configure(
                text=(
                    f"等待玩家加入。房间号 {snapshot.get('room', '')}；"
                    f"本机 MC {snapshot.get('mc_endpoint', '')}"
                ),
                fg=FG_DIM,
            )

        # Reset the button state if the session silently died.
        #
        # ★ P3（2026-10-03 热切换分析）：模式切换会**短暂**把会话拆掉
        #   （role=None / STATE_IDLE），这不是掉线。在这里清 `running` 会让
        #   后面三条重建路径（`_sync_session_mode` / `_maybe_rebuild_for_mode_change`
        #   / 中继广播那条）全部拒绝执行 —— 现场表现就是"房主自己重连了、
        #   host 没重连、状态也不释放"。模式切换窗口里不许清。
        if (
            self.running
            and role is None
            and state == STATE_IDLE
            and not getattr(self, "_mode_switch_silent", False)
            and not getattr(self, "_mode_rebuild_pending", False)
        ):
            self.running = False
            self._set_enabled(self.start_button, True)
            self._set_enabled(self.stop_button, False)

    # --------------------------------------------------------
    # Close
    # --------------------------------------------------------

    def on_close(self):
        if self.running:
            if not messagebox.askokcancel(
                "退出",
                "当前连接仍在运行，确定要退出吗？",
            ):
                return

        # Persist the form values (best effort, must never block exit).
        with contextlib.suppress(Exception):
            self._config_file_path = cfgmod.save_config(self._collect_config())

        # ---- leave the room, so the host slot frees immediately ----
        #
        # Closing the window is how most people leave. The relay would notice
        # eventually via its watchdog, but "eventually" means the host slot
        # stays locked and nobody else can start playing. A polite LEAVE makes
        # it instant.
        with contextlib.suppress(Exception):
            if self.engine.room_current() is not None:
                self.log("leaving the room ...", kind="dim")
                self.engine.close_room(timeout=5)

        # ---- 上线状态：先关连接 ----
        #
        # 面板的「在线账号」按真实连接统计。这条连接一断，面板立刻显示离线；
        # 正常关窗走这条路，强杀进程则由对端读到 EOF 自动生效。
        with contextlib.suppress(Exception):
            self.engine.stop_presence(timeout=3)

        # ---- say goodbye to the account session ----
        #
        # Closing the window used to leave two traces behind on the server:
        #
        #   * the room: handled above and by the member watchdog.
        #   * the session token: NOT handled, so the panel counted the account
        #     as online for the token's full 12 hour life.
        with contextlib.suppress(Exception):
            if self.auth is not None:
                self.log("notifying the server of logout ...", kind="dim")
                self.engine.logout_session(
                    self.host_var.get().strip(),
                    self._port_or_default(),
                    self.auth,
                    self.client_id_var.get().strip(),
                    timeout=5,
                )

        # ============================================================================
        #  ★ 用户 2026-10-03：退出**不许"未响应一下"**
        # ============================================================================
        #
        # 上面这些收尾（离开房间 / 下线 / 登出 / 停会话）每一个都带秒级超时，
        # 在 UI 线程上串起来就是"点了叉，窗口卡住几秒才关"。用户口径：可以挪到
        # 后台、也可以给个"退出中"的状态，**但不能影响正常功能**。
        #
        # 做法：先把窗口画成"正在退出…"（立刻反馈），然后**在后台线程**跑收尾，
        # 完成后回 UI 线程销毁窗口；同时挂一个 8 秒硬兜底 —— 收尾再慢，窗口也
        # 一定会关，绝不会永久卡住。
        # ★ 2026-10-03（退出分析结论）：**先把要用的 tk 值快照下来**，
        #   再立刻销毁窗口 —— 销毁之后所有 tk 变量都失效了。
        host = ""
        port = 0
        client_id = ""
        with contextlib.suppress(Exception):
            host = self.host_var.get().strip()
        with contextlib.suppress(Exception):
            port = self._port_or_default()
        with contextlib.suppress(Exception):
            client_id = self.client_id_var.get().strip()

        auth = self.auth
        had_room = False
        with contextlib.suppress(Exception):
            had_room = self.engine.room_current() is not None
        running = bool(self.running)

        with contextlib.suppress(Exception):
            self.log("exiting ...", kind="dim")
            logmod.event(
                "client.exit",
                room=self._current_room_name() or "",
                running=running,
            )

        with contextlib.suppress(Exception):
            self.root.title(f"{self.root.title()} — {tr('正在退出…')}")
            self.root.update_idletasks()

        import threading

        # **窗口立刻关**（Tk 线程、同步）。以前 `close_room(timeout=5)` 和
        # `logout_session(timeout=5)` 都在 Tk 线程上跑 —— 中继不可达时正好各等满
        # 5 秒，就是用户看到的"未响应一下"。现在这两个也挪到后台。
        with contextlib.suppress(Exception):
            self.root.destroy()

        def _finish_exit():
            # 后台线程只碰引擎和日志文件，**绝不碰 tkinter**
            # （本文件 9237 那条教训：后台碰 tk 会抛 main thread is not in main loop）。
            if had_room:                      # 礼貌 LEAVE，主机位立刻空出来
                with contextlib.suppress(Exception):
                    self.engine.close_room(timeout=5)

            with contextlib.suppress(Exception):
                self.engine.stop_presence(timeout=3)

            if auth is not None:              # 登出（新开 TCP，慢，所以放这儿）
                with contextlib.suppress(Exception):
                    self.engine.logout_session(
                        host, port, auth, client_id, timeout=5
                    )

            with contextlib.suppress(Exception):
                self.engine.shutdown()

            # Windows：释放日志文件句柄（否则日志文件删不掉）。
            with contextlib.suppress(Exception):
                logmod.close()

        self._exit_thread = threading.Thread(
            target=_finish_exit, name="exit", daemon=True
        )
        self._exit_thread.start()


# ============================================================
# Entry
# ============================================================

#: 单实例锁的句柄/文件对象（**故意保存在模块级**：只要进程活着就不能被回收，
#: 回收 = 锁没了 = 第二个实例又能起来）。
_SINGLE_INSTANCE_HANDLE = None


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


def run():
    """
    Start the GUI. Returns the process exit code.

    IMPORTANT: the GUI is started with plain `tk.Tk()` (not `ttk`), so the
    "🌐 proxy" system setting can never interfere. `root.iconify()` flicker is
    avoided by building everything before `mainloop()`.
    """
    # ★ 用户 2026-09-30 第 2 条：不许双开 —— 只提示并退出（不动别的窗口）。
    ok, why = acquire_single_instance()

    if not ok:
        with contextlib.suppress(Exception):
            warn_root = tk.Tk()
            warn_root.withdraw()
            messagebox.showwarning("CraftLink", why, parent=warn_root)
            warn_root.destroy()

        with contextlib.suppress(Exception):
            print(why, file=sys.stderr)

        return 2

    root = tk.Tk()

    app = MCLinkApp(root)

    # Mode banner / role fields are already built; refresh once so the text
    # matches the loaded config.
    app._on_mode_change()  # noqa: SLF001 - internal init call
    app._on_role_change()  # noqa: SLF001 - internal init call

    root.mainloop()

    # 退出收尾在 daemon 线程里：不 join 的话解释器一退就把它掐了，
    # 礼貌 LEAVE / 登出就丢了（主机位只能等看门狗）。等一小会儿即可。
    with contextlib.suppress(Exception):
        _exit = getattr(app, "_exit_thread", None)
        if _exit is not None:
            _exit.join(timeout=4)

    return 0


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
