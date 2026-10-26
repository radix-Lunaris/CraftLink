"""
MC-Link client -- 界面语言（中文 / English / 日本語）。

============================================================================
 设计原则：翻不到就显示原文，绝不显示空白或 key
============================================================================

`tr("创建房间")` 查表；查不到就原样返回。所以：

  * 只翻译一部分是安全的 —— 没译到的部分仍然是中文，功能完全不受影响；
  * 新增一句界面文字时忘了翻译，最坏结果只是那句还是中文；
  * 任何时候都不需要"翻译完整性"来保证程序能跑。

语言只改观感：所有逻辑判断（身份、房间、协议）都不看这里的字符串，
它们是分开的。
"""

import contextlib

#: 语言代码 -> 界面上显示的名字
LANGUAGES = (
    ("zh", "中文"),
    ("en", "English"),
    ("ja", "日本語"),
)

DEFAULT_LANGUAGE = "zh"

_current = DEFAULT_LANGUAGE


# 翻译表在 i18n_tables.py（只有数据，方便补翻译）；
# PATTERNS 是「带变量的句子」用的正则表（f-string 拼完就查不到表了）。
from .i18n_tables import EN, JA, PATTERNS  # noqa: E402

TRANSLATIONS = {"en": EN, "ja": JA}


# ============================================================
# API
# ============================================================

def set_language(code):
    """设置界面语言。未知代码退回中文。返回实际生效的代码。"""
    global _current

    code = (code or "").strip().lower()
    _current = code if code in TRANSLATIONS or code == DEFAULT_LANGUAGE else DEFAULT_LANGUAGE

    return _current


def current_language():
    return _current


def language_label(code=None):
    code = code or _current

    for known, label in LANGUAGES:
        if known == code:
            return label

    return code


def tr(text):
    """
    翻译一句界面文字。

    中文 / 查不到 -> 原样返回。永远不返回空串，永远不抛异常：
    翻译只是观感，不能因为它让界面缺字或报错。

    三层查找：
      1. 静态表（界面上的固定文案）
      2. 句式表（日志/提示里带变量的句子 —— f-string 拼完之后原文就没了，
         只能靠正则句式匹配，见 i18n_tables.PATTERNS）
      3. 都没有 -> 原文
    """
    if _current == DEFAULT_LANGUAGE:
        return text

    table = TRANSLATIONS.get(_current)

    if not table:
        return text

    try:
        found = table.get(text)

        if found is not None:
            return found
    except Exception:
        return text

    return _apply_patterns(text)


def _apply_patterns(text):
    """按句式表翻译一句带变量的文字；没有匹配就原样返回。"""
    for pattern, replacement in PATTERNS.get(_current, ()):
        try:
            if pattern.search(str(text)):
                return pattern.sub(replacement, str(text))
        except Exception:
            continue

    return text


def missing_keys(texts):
    """
    给出这批文字里"当前语言还没翻译"的那些。

    用途是自动检查：切到英文之后把界面上所有文字收集起来，剩下的就是漏翻的。
    中文（默认语言）下永远返回空 —— 中文本来就是原文。
    """
    if _current == DEFAULT_LANGUAGE:
        return []

    table = TRANSLATIONS.get(_current) or {}

    return sorted({t for t in texts if t and t not in table})


def has_cjk(text):
    """这句里还有中日韩文字吗？（自动检查漏翻用）"""
    if not text:
        return False

    for char in str(text):
        code = ord(char)

        if (
            0x4E00 <= code <= 0x9FFF      # 汉字
            or 0x3040 <= code <= 0x30FF   # 平假名 / 片假名
            or 0xAC00 <= code <= 0xD7AF   # 韩文
            or 0x3000 <= code <= 0x303F   # 中文标点
            or 0xFF00 <= code <= 0xFFEF   # 全角符号（，。！？：；（）等）
        ):
            return True

    return False


def translate_widget_tree(widget, skip=()):
    """
    把一棵控件树上所有【静态文字】翻译一遍。

    ============================================================================
      为什么用"遍历"而不是"在几百个 call site 上包 tr()"
    ============================================================================

    Wrapping every string where it is written works, but it is 300+ edits and
    the one you forget is invisible until a user switches language -- which is
    exactly what happened (「英语并未完全覆盖所有按钮和面板」).

    Walking the tree afterwards covers every static label, button, checkbox,
    treeview heading and notebook tab that exists, including ones added later
    by somebody who never reads this file. `tr()` returns the input unchanged
    when there is no translation, so the pass is safe by construction.

    Skipped on purpose:
      * widgets with a `textvariable` -- their text comes from a variable (data)
      * Text/Entry/Listbox content -- that is data, not a label
      * anything in `skip` (the caller's own exceptions)

    返回 (翻译过的条数, 仍然含中日韩文字的文字列表) 方便调用方做自检。
    """
    translated = 0
    leftovers = []

    def visit(node):
        nonlocal translated

        with contextlib.suppress(Exception):
            if node in skip:
                return

            klass = node.winfo_class()

            if klass not in ("Text", "Entry", "TEntry", "Listbox"):
                try:
                    bound = str(node.cget("textvariable") or "")
                except Exception:
                    bound = ""

                if not bound:
                    try:
                        current = node.cget("text")
                    except Exception:
                        current = ""

                    if current:
                        new = tr(str(current))

                        # 按钮文字是"图标 + 标签"（▶ 开始连接 / ⧉ 复制）：
                        # 整串查不到表时，把前面那个图标剥掉再查一次，
                        # 翻译完把图标原样接回去。
                        if new == current and _current != DEFAULT_LANGUAGE:
                            head, sep, tail = str(current).partition(" ")

                            if sep and tail:
                                translated_tail = tr(tail)

                                if translated_tail != tail:
                                    new = f"{head} {translated_tail}"

                        if new != current:
                            node.configure(text=new)
                            translated += 1
                        elif _current != DEFAULT_LANGUAGE and has_cjk(new):
                            leftovers.append(str(new))

            # Treeview 的列标题也要翻（它是 per-column 的，不是 text 选项）
            if klass == "Treeview":
                for column in node["columns"]:
                    with contextlib.suppress(Exception):
                        heading = node.heading(column).get("text", "")

                        if heading:
                            new = tr(str(heading))

                            if new != heading:
                                node.heading(column, text=new)
                                translated += 1
                            elif _current != DEFAULT_LANGUAGE and has_cjk(new):
                                leftovers.append(str(new))

        for child in node.winfo_children():
            visit(child)

    visit(widget)

    return translated, leftovers
