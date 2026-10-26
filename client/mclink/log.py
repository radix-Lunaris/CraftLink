"""
CraftLink -- 客户端日志系统（按 MCLanP2P 的那一套）。

============================================================================
 为什么要单独一个模块（而不是留在界面里）
============================================================================

原来的落盘日志是界面的一个私有细节：`MCLinkApp._setup_file_logger()` 建一个
handler，`MCLinkApp.log()` 往里写。后果有三个，都是真实踩到的：

  1. **引擎/内核想在界面起来之前记日志，没地方记**。房间成员连接、STUN 探测、
     打洞尝试都发生在"窗口还没建好"或者"没有窗口"（`main.py --selfcheck`、
     命令行用法）的时候，那时候日志只能丢。
  2. **同一条日志两个版本**：屏幕上的过 `tr()`（跟着界面语言），文件里的没过，
     于是"用户截图的日志"和"用户发来的日志"能对不上。排查最怕这个。
  3. **格式散在各处**：谁写日志谁拼字符串，于是同一件事在不同地方长得不一样，
     事后 grep 不出来。

所以这里做三件事，正是 MCLanP2P 的 `client/common.py::file_logger()` 做的事：

  * **一个进程级 logger**（`logging.getLogger("mclink")`），谁都能拿；
  * **按天轮转的文件**（`TimedRotatingFileHandler`，午夜切，留 5 份，
    UTF-8）——他原来的注释写得很清楚：日志以前只在窗口里，窗口一关，
    出错的所有线索都没了；
  * **写不进去也不能影响运行**：只读安装目录、磁盘满，都退化成 NullHandler。

============================================================================
 关于「记录原始数据，不要翻译」
============================================================================

用户的原话：不是把中文翻成英文，而是**记录原始行为数据**，方便后续改进和审计。

所以这里的每一条都写成 `EVENT code=... key=value ...` 的形状：

    [2026-09-21 13:04:11.482] EVENT connection.established role=host peer=1.2.3.4:51234 room=... 

规则：
  * **字段名是数据**（`room`、`peer`、`code`、`mode`），从协议/代码里直接取，
    永远不翻译、不改名——改了就对不上服务器日志和协议文档；
  * **值是原始值**：IP、端口、会话 ID、协议原文、错误码原样进来，
    `repr()` 保证空格/换行/中文房间名不会被日志格式吃掉；
  * **不写"人话解释"**：解释会随版本变化，数据不会。要可读性由字段名提供。

============================================================================
 界面语言和日志语言是两件事
============================================================================

界面上按钮写"创建房间"还是 "Create room" 跟着语言走；日志**不跟**。
日志是审计材料，界面是产品外观。`client/mclink/gui/app.py` 的 `log()` 因此
**不再对日志正文做 tr()**——以前那一步正是"文件里英文、屏幕上中文"的来源。
"""

import contextlib
import logging
import logging.handlers
import os
import sys
import time

from .config import settings as cfgmod

#: 进程级唯一。`logging.getLogger` 本身按名字去重，所以模块被导入几次都只有
#: 一个 logger、一组 handler。
_LOGGER = None

#: 日志文件名。和配置同目录下的 logs/。
LOG_FILENAME = "craftlink-client.log"

#: 轮转：每天一份，留 5 份（他的 backupCount=5）。
BACKUP_COUNT = 5


def log_path():
    """日志文件的完整路径（拿不到目录时返回空串）。"""
    try:
        return os.path.join(cfgmod.log_dir(), LOG_FILENAME)
    except Exception:
        return ""


def logger():
    """
    进程级 logger。第一次调用时建 handler。

    绝不在日志初始化失败时抛异常：磁盘满、只读目录、权限不对都必须让客户端
    照常跑——日志是旁路，不是数据通路的一部分（这条是全项目的硬要求）。
    """
    global _LOGGER

    if _LOGGER is not None:
        return _LOGGER

    log = logging.getLogger("mclink")
    log.setLevel(logging.INFO)
    # 不加这一句，root logger 会把我们的行再打一遍到 stderr。
    log.propagate = False

    if not log.handlers:
        added = False

        with contextlib.suppress(Exception):
            path = log_path()

            if path:
                handler = logging.handlers.TimedRotatingFileHandler(
                    path,
                    when="midnight",
                    backupCount=BACKUP_COUNT,
                    encoding="utf-8",
                    delay=True,
                )
                handler.setFormatter(
                    logging.Formatter("%(asctime)s.%(msecs)03d %(message)s",
                                      "%Y-%m-%d %H:%M:%S")
                )
                log.addHandler(handler)
                added = True

        if not added:
            # 只读安装也必须能跑：丢日志，不丢功能。
            log.addHandler(logging.NullHandler())

    _LOGGER = log

    return log


def close():
    """
    关掉 handler 并释放文件句柄。

    退出时调一次：Windows 上不关句柄，随后想删/移动日志文件会失败
    （"另一个程序正在使用此文件"），用户手动清日志时就会撞上。
    """
    global _LOGGER

    if _LOGGER is None:
        return

    for handler in list(_LOGGER.handlers):
        with contextlib.suppress(Exception):
            handler.close()

        with contextlib.suppress(Exception):
            _LOGGER.removeHandler(handler)

    _LOGGER = None


# ============================================================
# 事件记录
# ============================================================

def _fmt_value(value):
    """
    把一个值写成日志里安全的一段。

    字符串一律 `repr()`：房间名可能带空格/引号/中文，真值里也可能带空格
    （`ERROR NOT_HOST 连接模式由房主决定` 这种整行原因）。不 repr 就会把一行
    撕成好几段，事后按空格切字段全错位——这正是"日志看着有、其实没法分析"。
    数字/布尔/None 直接写，省得日志里全是引号。
    """
    if value is None:
        return "null"

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, (int, float)):
        return str(value)

    return repr(value if isinstance(value, str) else str(value))


def event(name, **fields):
    """
    记一条结构化事件：`EVENT <name> k=v k=v ...`

    `name` 是稳定的事件代号（`connection.established`、`room.created`），
    点号分层，方便 `grep -E 'EVENT room[.]'`。

    参数叫 `name` 而不是 `code` 是有意的：`code` 是**很多事件自己的字段**
    （中继的错误码、房间代号），如果它是形参名，调用方就没法再传 `code=`
    了（`TypeError: got multiple values`）——踩过一次，所以这里让开。

    字段顺序按传进来的顺序保留（Python 3.7+ dict 有序），肉眼看也稳定。
    """
    parts = ["EVENT", str(name)]

    for key, value in fields.items():
        if value is None:
            continue

        parts.append(f"{key}={_fmt_value(value)}")

    write(" ".join(parts))


def raw(keyword, line):
    """
    原样记一条**协议原文**：`<keyword> <原文>`。

    这是审计最需要的东西：线上到底传了什么字。绝不加工、绝不截断
    （要截断由调用方决定，因为只有调用方知道截多少还有意义）。

    `keyword` 前缀不是装饰：一整屏裸字段事后根本认不出是哪条协议，加了才能
    grep。签名和服务端 `relay_events.raw()` **完全一致**，两边的日志可以并起
    来看 —— 客户端发的那条和中继转发的那条，应当逐字相同。
    """
    write(f"{keyword} {line}")


def write(text):
    """
    落一行。带毫秒时间戳，永远不抛异常。
    """
    try:
        logger().info("%s", text)
    except Exception:
        pass


def note(text):
    """
    记一条"非结构化"的说明行（启动横幅、自检结果这类）。

    能用 `event()` 表达的一律用 `event()`；这个只留给真正的自由文本。
    """
    write(text)


__all__ = [
    "LOG_FILENAME",
    "BACKUP_COUNT",
    "log_path",
    "logger",
    "close",
    "event",
    "raw",
    "write",
    "note",
]
