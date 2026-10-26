"""
CraftLink 中继 —— 结构化日志事件的记录格式。

============================================================================
 这个文件和 relay_log.py 的分工
============================================================================

  * `relay_log.py`  负责**落地**：内存环形缓冲 + 文件 + 轮转 + 清空；
  * `relay_events.py`（本文件）负责**形状**：一行日志长什么样。

分开是因为 `relay_server.py` 以 `__main__` 跑，而任何 `import relay_server`
都会再导入一份副本（AGENTS.md §5 那个很贵的坑）。格式必须是两边都能安全
`import` 的纯函数，不能住在 relay_server 里。

============================================================================
 为什么要有「形状」这件事（用户的原始要求）
============================================================================

用户原话：**「一切按照服务器和客户端行为语言记录 —— 不是单纯把原本的中文
翻译成英文，而是记录原始数据，不要加上翻译，方便后续的改进和审计」**。

所以日志行不是"一句话"，而是**一条可解析的事件**：

    EVENT connection.opened role=host peer='1.2.3.4:51234' room='...' 

规则，和客户端 `client/mclink/log.py` **完全一致**（两边格式一样，事后一条
命令就能把客户端和中继的日志并起来看）：

  1. `EVENT <name>` 打头，`name` 是稳定代号（点号分层，方便 grep）；
  2. 后面全是 `key=value`，**字段名是数据**：直接取协议/代码里的名字，
     永远不翻译、不改名——改了就对不上协议文档和历史日志；
  3. **值是原始值**：IP、端口、会话 ID、协议原文、错误码、耗时，原样进；
     字符串一律 `repr()`，因为原因里常带空格（`ERROR NOT_HOST 连接模式由房主决定`），
     不 repr 会把一行撕成好几段，事后按空格切字段全错位；
  4. **不写"人话解释"**：解释会随版本变化，数据不会。可读性由字段名提供。

不用 logging 模块的 Formatter 是因为：中继的日志必须同时进内存缓冲（面板
预览）、进文件（下载）和 stdout（docker logs），而这三条路现在都由
`relay_log.write()` 统一收口，所以格式在**进 write 之前**就定好——这比让每个
handler 各自 format 一遍更可控，也不会出现"文件里一个样、面板里另一个样"。
"""

#: 值里可能出现换行 / 引号 / 中文房间名。repr() 让它们始终是**一个 token**，
#: 事后 `shlex.split()` 或 `ast.literal_eval()` 都能还原出真值。
def value(item):
    if item is None:
        return "null"

    if isinstance(item, bool):
        return "true" if item else "false"

    if isinstance(item, (int, float)):
        return str(item)

    return repr(item if isinstance(item, str) else str(item))


def event(name, **fields):
    """
    组一条结构化事件行（**不含时间戳**）。

    时间戳由 `relay_server.log()` 统一加（它知道现在是哪个时区/格式），
    这里只负责 `EVENT ... key=value ...` 这一段。

    返回字符串；调用方负责交给 log()。
    """
    parts = ["EVENT", str(name)]

    for key, item in fields.items():
        # None 表示"这个字段这次没有值"，直接省略，不写 `key=null`。
        # 理由：日志里 `peer=null` 和 `peer` 缺失是两件事，而我们要的是后者
        # （"没这个信息"），留一个 null 只会让人以为"有个叫 null 的值"。
        if item is None:
            continue

        parts.append(f"{key}={value(item)}")

    return " ".join(parts)


def raw(keyword, line):
    """
    原样记一条**协议原文**：`<keyword> <line>`。

    审计最需要的就是"线上到底传了什么字"，所以这里绝不加工、绝不截断。
    截断由调用方决定，因为只有它知道截多少还有意义（`pairing.token` 可以
    全留，一条 Minecraft 数据不行）。

    加 `keyword` 前缀是为了让原样行**仍然能被 grep 到**（否则一屏纯数字和
    二进制，事后根本认不出这是哪条协议）。
    """
    return f"{keyword} {line}"


__all__ = ["event", "raw", "value"]
