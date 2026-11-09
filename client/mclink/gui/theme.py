"""
MC-Link client -- 配色方案（暗色 / 亮色 / 跟随系统）。

============================================================================
 为什么单独一个模块
============================================================================

The window is built from a dozen colour constants that are referenced directly
in the layout code (bg=BG, fg=FG_DIM ...). Turning those into a theme means the
VALUES have to be swappable at runtime, while the names stay exactly as they
are -- otherwise every line of layout code would have to change, and this is a
purely visual feature that must not be able to break anything.

So: the palettes live here as plain dictionaries, and `apply()` writes them into
app.py's module globals. Rebuilding the window then produces the other theme.

跟随系统 = 读 Windows 的 AppsUseLightTheme（注册表，标准库 winreg 就够）。
读不到就退回暗色 —— 猜错配色只是不好看，绝不能影响功能。
"""

import contextlib

#: 两套配色。键名就是 app.py 里的常量名。
PALETTES = {
    "dark": {        "BG": "#11151c",
        "CARD": "#1b2230",
        "CARD_ALT": "#232c3d",
        "BORDER": "#2f3a4f",
        "FG": "#e8edf7",
        "FG_DIM": "#9aa7bd",
        "FG_MUTED": "#6d7a94",
        "ACCENT": "#4c8dff",
        "ACCENT_DARK": "#3a6fd8",
        "OK": "#3ddc84",
        # ★ 用户 2026-09-30 第 4 条：NAT 四档的颜色
        #   NAT1 青绿 / NAT2 天蓝 / NAT3 橙黄 / NAT4 红。
        #   青绿和"已连接"的绿刻意分开（青绿偏 cyan），天蓝和 ACCENT 的蓝也分开
        #   （天蓝更亮更浅），这样四档并排看是四色而不是两种颜色深浅不一。
        "NAT1": "#2ee6c6",
        "NAT2": "#57c8ff",
        # NAT4 拆两色（用户 2026-10-01）：递增/递减 = **可预测**（洋红，还能用
        # 生日悖论那套打）；随机 = **难**（红，最坏）。和 NAT3 的橙黄刻意分开。
        "NAT4_EASY": "#e0559b",
        "NAT4_HARD": "#ff5f56",
        "WARN": "#ffb020",
        "BAD": "#ff5f56",
        "LOCKED": "#7a879e",
        "TILE_ON": "#1e3a68",
        "TILE_OFF": "#1b2231",
        "LOG_BG": "#0c1016",
        "LOG_FG": "#c7d3e8",
        "HEADER": "#0e1219",
        # 卡片阴影：比页面底色更暗一点，卡片才像"浮"在上面。
        "SHADOW": "#070a0f",
    },
    # 亮色：不是把暗色反过来那么简单 —— 纯白底配纯黑字太刺眼，
    # 所以用带一点蓝的浅灰底 + 深灰蓝文字（B 站 / Apple 的浅色界面都是这个路子）。
    #
    # 用户报「颜色发虚」：原来的三级文字（#1f2733/#55617a/#8994ab）对比度偏低，
    # 尤其是 FG_MUTED 在浅灰底上只有 ~3.1:1，看着像没上色的灰。这一轮整体压深，
    # 并且保证三级文字在【卡片白底】和【页面浅灰底】上都 ≥ 4.5:1（WCAG AA 正文）。
    "light": {
        "BG": "#f2f4f8",
        "CARD": "#ffffff",
        "CARD_ALT": "#eef1f7",
        "BORDER": "#d6dbe6",
        "FG": "#16202c",
        "FG_DIM": "#46536b",
        "FG_MUTED": "#64707f",
        "ACCENT": "#2f6fed",
        "ACCENT_DARK": "#1f57c9",
        "OK": "#12a150",
        # 亮色下同样四色，但压深保证在浅灰底上可读（WCAG AA）。
        "NAT1": "#0e8f7d",
        "NAT2": "#1d6fb8",
        "NAT4_EASY": "#b8367a",
        "NAT4_HARD": "#d93636",
        "WARN": "#b8730a",
        "BAD": "#d93636",
        "LOCKED": "#9aa4b8",
        "TILE_ON": "#dbe6ff",
        "TILE_OFF": "#f4f6fb",
        "LOG_BG": "#f7f9fc",
        "LOG_FG": "#2a3446",
        "HEADER": "#ffffff",
        # 亮色下的阴影：比页面底色深一档的灰蓝，太重会显脏。
        "SHADOW": "#dde2ec",
    },
}

#: 单纯的两套配色。**不做彩色主题**（用户看过之后说不好看，去掉了）。
#:
#: 亮色那一套是**平的**：接近白的冷灰底 + 纯白卡片，不画任何渐变/晕色
#: （用户：「去掉亮色的渐变/晕色背景」）。

#: 供界面显示的名字（基础三项 + 彩色四项，顺序就是下拉框顺序）
THEME_LABELS = (
    ("dark", "暗色"),
    ("light", "亮色"),
    ("system", "跟随系统"),
)

_current = "dark"
_resolved = "dark"


def system_theme():
    """
    Windows 现在是浅色还是深色？读不到就按暗色。

    AppsUseLightTheme: 1 = 浅色, 0 = 深色（这是 Windows 10/11 的标准位置）。
    """
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )

        with contextlib.closing(key):
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")

        return "light" if int(value) == 1 else "dark"
    except Exception:
        return "dark"


def resolve(name):
    """
    把用户选的主题名解析成真正生效的基础配色（'dark' / 'light'）。

    彩色主题（aurora / violet / mint / sunrise）是"基础配色 + 强调色覆盖"，
    所以这里返回的是它们各自的基础配色。
    """
    name = (name or "dark").strip().lower()

    if name == "system":
        return system_theme()

    return name if name in PALETTES else "dark"


def current():
    """用户选的那一项（可能是 'system' 或某个彩色主题）。"""
    return _current


def resolved():
    """实际生效的那一套基础配色名（'dark' / 'light'）。"""
    return _resolved


def set_theme(name):
    """
    记住选择并解析成实际配色。返回这一次真正生效的配色名。

    注意：这里只更新"应该用什么"，真正套用到界面上由 app.py 的
    apply_appearance() 调用 palette() 完成（它需要重建窗口）。
    """
    global _current, _resolved

    _current = (name or "dark").strip().lower()

    if _current not in ("dark", "light", "system"):
        _current = "dark"

    _resolved = resolve(_current)

    return _resolved


def palette():
    """
    当前生效的完整配色字典。

    返回的一定是齐全的字典，调用方（app.apply_theme）不需要任何判断。
    """
    return dict(PALETTES.get(_resolved, PALETTES["dark"]))
