"""
MC-Link relay -- 房间名 与 房间 ID（room token）。

============================================================================
 为什么要分成两个东西
============================================================================

玩家看到的是【房间名】：中文、英文、带空格、带 emoji 都行。它是人对房间的
识别标志，两个人取同一个名字就是同一个房间（所以名字天然唯一）。

但是线上的握手协议是**空格分隔的一行文本**：

    HOST <room> <client_id> <username> <token>

房间名里只要有一个空格，这一行就被切错了 —— 中继会把名字的后半截当成
client_id。斜杠也不行：面板的管理接口把房间标识放在 URL 路径里
（/rooms/<room>/revoke），斜杠会变成路径分隔符，于是"面板能显示但关不掉"。

所以协议里那个字段永远是一个 **percent-encode 过的 ASCII 记号**，
本文管它叫 room token（也就是对用户隐藏的"房间 ID"）：

    房间名  我的世界          <-- 玩家看到、输入、列表里显示
    token   %E6%88%91%E7%9A%84%E4%B8%96%E7%95%8C   <-- 协议/数据库/面板接口

两者是**一一对应**的（quote 是单射），所以：
  * 同名 = 同一个房间（重名自然被拒：CREATE 撞到已有 token → ROOM_EXISTS）
  * 房间名任意字符都能用，不再受"不能有空格"这种协议限制
  * token 永远是 ASCII，面板的管理接口不再需要担心 percent-decode

老客户端照旧工作：它们发的是自己那串 ASCII 名字，`room_token()` 对它做
unquote+quote 之后还是原样（ASCII 安全字符不变），所以命中的还是同一个房间。
"""

from urllib.parse import quote, unquote

SAFE = ""


def room_token(value):
    """
    任何输入 -> 规范的 room token（ASCII、无空格、无斜杠）。

    幂等：room_token(room_token(x)) == room_token(x)。
    老客户端发来的裸名字（含中文时是 UTF-8 原文）也能得到同一个 token。
    """
    if not value:
        return ""

    try:
        text = unquote(str(value))
    except Exception:
        text = str(value)

    # quote 默认不转义 "/"，这里必须转 —— 见文件开头关于面板接口的说明。
    return quote(text, safe=SAFE)


def display_name(value):
    """room token -> 玩家看到的房间名。"""
    if not value:
        return ""

    try:
        return unquote(str(value))
    except Exception:
        return str(value)


# ============================================================
# 任意字符串 <-> 一个协议 token（percent-encoding）
# ============================================================
#
# 和房间名同一套规则，但**不做 unquote**：这里编的是"显示名"本身
# （登录名 / 中继分配的游客名），它可能含中文、空格甚至 %，先 unquote 会把
# 真正的字符吃掉（`a%41` -> `aA`）。所以编码是无损的单向 quote，
# 解码是 unquote。
#
# 用途（协调者冻结的格式，见下）：
#   OK JOIN/OK CREATE 的 `you=<name>`
#   HOST_STATE 的 `names=<cid>:<name>,...`
#
# 保证结果里没有空格 / `,` / `:`，所以塞进一行空格分隔的协议里
# 不会把这一行切坏。

def encode_field(value):
    """任意字符串 -> 安全放进一行协议的 ASCII token（percent-encoding）。"""
    if value is None or value == "":
        return ""

    return quote(str(value), safe=SAFE)


def decode_field(value):
    """encode_field() 的逆运算。畸形输入按原文返回，绝不抛异常。"""
    if not value:
        return ""

    try:
        return unquote(str(value))
    except Exception:
        return str(value)


def name_problem(name, max_len):
    """
    房间名合法性。返回 None 表示可以用，否则返回给用户看的中文原因。

    规则只有三条，都很宽松（用户的房间名应当能随便取）：
      * 去掉两端空白后不能是空的
      * 不能有控制字符（换行/制表符会把协议行切断，也会污染日志）
      * 长度上限
    """
    if name is None:
        return "请填写房间名"

    text = str(name).strip()

    if not text:
        return "请填写房间名"

    if len(text) > max_len:
        return f"房间名最长 {max_len} 个字符（你填了 {len(text)} 个）"

    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return "房间名不能包含换行、制表符等控制字符"

    return None
