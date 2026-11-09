"""
CraftLink 中继日志模块 —— 让服务端日志**存得下来、找得到、能下载**。

============================================================================
 为什么要有这个文件（用户的原始抱怨）
============================================================================

用户原话：「服务端日志在哪里啊？找不到一点」。当时的实际情况是：

  * 中继的 log() 就是一句 print()，日志只落在**容器标准输出**里；
  * 想看只能 `docker logs mc-relay`，翻不了历史、没法按时间找、
    更没法把它发给别人排查；
  * 中继一重启，之前的日志就彻底没了（除非 docker 的 json 日志还在）。

所以这里做两件事：
  1. **写到文件**，带大小轮转（默认 5MB × 3 份），容器里挂在数据卷上，
     重建容器也不会丢；
  2. **留一份内存环形缓冲**（最近 N 行），面板要"下载最近日志"时不用
     去读几 MB 文件，也保证文件刚好在轮转时仍然有东西可给。

============================================================================
 为什么单独一个模块（不要塞进 relay_server.py）
============================================================================

`relay_server.py` 是以 `__main__` 跑的。`admin_api.py` 要读同一份日志缓冲，
如果在 relay_server 里定义，`import relay_server` 会再导入一份**副本**，
两边看到的缓冲不是同一个（AGENTS.md §5 那个很贵的坑就是这么踩的）。

所以共享状态放这里，两个模块都正常 `import relay_log`，拿到的是同一份。
"""

import collections
import contextlib
import os
import threading
import time

import relay_config as cfg

#: ★ 必须是 RLock（可重入锁），**绝不能**用普通 Lock。
#:
#: 踩过的致命事故（现场极具迷惑性）：write() 先 `with _LOCK:` 拿锁，写文件
#: 失败时又 `with _LOCK:` 去记一条警告 —— 同一线程二次获取普通 Lock 会
#: **永久死锁**。表现是：日志停在最后一行、进程永不退出、0 次重启、
#: 健康检查一直 unhealthy，看起来像"卡在连数据库"，其实是卡在自己的锁上。
#: 触发条件只有一个：日志目录不可写（compose 挂的 ./logs 属主是 root，
#: 而容器以 uid 10001 运行）。
_LOCK = threading.RLock()

#: 最近若干行（面板/接口要"给我最近 N 行"时从这里取，快且不受轮转影响）
_LINES = collections.deque(maxlen=max(200, int(getattr(cfg, "LOG_RING_LINES", 4000))))

#: 当前日志文件是否可写（目录建不出来时降级为"只有内存缓冲"，绝不能因此崩掉）
_FILE_READY = False
_FILE_PATH = ""
_FILE_BYTES = 0

#: 写入失败只报一次，避免刷屏
_WRITE_ERROR_REPORTED = False

#: 日志文件彻底不可写（例如挂载目录属主不对）→ 只用内存缓冲
_FILE_FAILED = False


def _resolve_path():
    directory = getattr(cfg, "LOG_DIR", "") or ""

    if not directory:
        return ""

    try:
        os.makedirs(directory, exist_ok=True)
    except Exception:
        return ""

    return os.path.join(directory, getattr(cfg, "LOG_FILE_NAME", "relay.log"))


def _open_ready():
    """第一次写入时再准备文件；准备不了也不抛异常。"""
    global _FILE_READY, _FILE_PATH, _FILE_BYTES

    if _FILE_READY:
        return True

    if not _FILE_PATH:
        _FILE_PATH = _resolve_path()

    if not _FILE_PATH:
        return False

    try:
        if os.path.exists(_FILE_PATH):
            _FILE_BYTES = os.path.getsize(_FILE_PATH)
        else:
            _FILE_BYTES = 0

        _FILE_READY = True

        return True
    except Exception:
        return False


def _rotate_if_needed(next_chunk_bytes):
    """按大小轮转：relay.log -> relay.log.1 -> relay.log.2 …"""
    global _FILE_BYTES

    limit = int(getattr(cfg, "LOG_FILE_MAX_BYTES", 5 * 1024 * 1024) or 0)
    keep = int(getattr(cfg, "LOG_KEEP_FILES", 3) or 0)

    if limit <= 0 or keep <= 0:
        return

    if _FILE_BYTES + next_chunk_bytes <= limit:
        return

    try:
        # 最老的那份直接丢掉，其余依次后移
        oldest = f"{_FILE_PATH}.{keep}"

        if os.path.exists(oldest):
            os.remove(oldest)

        for index in range(keep - 1, 0, -1):
            src = f"{_FILE_PATH}.{index}"
            dst = f"{_FILE_PATH}.{index + 1}"

            if os.path.exists(src):
                os.replace(src, dst)

        if os.path.exists(_FILE_PATH):
            os.replace(_FILE_PATH, f"{_FILE_PATH}.1")

        _FILE_BYTES = 0
    except Exception:
        # 轮转失败不影响继续写（宁可文件大一点）
        pass


def write(text):
    """
    记一行日志。**永远不抛异常** —— 日志坏了绝不能影响 Minecraft 连接。
    """
    global _FILE_BYTES, _WRITE_ERROR_REPORTED

    line = text.rstrip("\n")

    with _LOCK:
        _LINES.append(line)

        if not _open_ready():
            return

        try:
            chunk = (line + "\n").encode("utf-8", errors="replace")
            _rotate_if_needed(len(chunk))

            with open(_FILE_PATH, "ab") as handle:
                handle.write(chunk)

            _FILE_BYTES += len(chunk)
        except Exception:
            # 注意：这里**已经持有** _LOCK（write 开头拿的），绝不能再 with _LOCK
            # ——普通 Lock 二次获取就是死锁（线上事故的根因）。锁本身已改成
            # RLock，这里是第二道保险：直接 append，不再碰锁。
            if not _WRITE_ERROR_REPORTED:
                _WRITE_ERROR_REPORTED = True
                _LINES.append(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[LOG WARN] 日志文件写入失败，已退化为只保留内存缓冲"
                )
                _FILE_FAILED = True


def tail(count=2000):
    """最近 count 行（新的在后）。"""
    try:
        count = int(count)
    except Exception:
        count = 2000

    count = max(1, min(count, 20000))

    with _LOCK:
        lines = list(_LINES)

    return lines[-count:]


def _remove_rotated():
    """
    删掉所有轮转出来的旧文件（relay.log.1 / .2 / …）。

    WHY: 清日志如果只清 relay.log，旧的 .1/.2 还在，用户下一眼看面板
    「日志有多大」仍然是一堆历史 —— 那是"删了但没删干净"，比不删更糟：
    他会以为清空没生效。导出读的也是当前文件，但备份还占着磁盘。
    """
    removed = 0
    keep = int(getattr(cfg, "LOG_KEEP_FILES", 3) or 0)

    for index in range(1, keep + 1):
        path = f"{_FILE_PATH}.{index}"

        try:
            if os.path.exists(path):
                os.remove(path)
                removed += 1
        except Exception:
            continue

    return removed


def clear(reason=""):
    """
    清空服务端日志：**内存缓冲 + 当前文件 + 轮转备份**，一处不留。

    ============================================================================
      为什么要清三样，而不是只清缓冲（用户报的就是这个）
    ============================================================================

    用户原话：「服务端手动删除日志后，列表内容也要一并清理」。

    清日志以前只动了内存缓冲（如果动了的话），于是：

      * 面板上的行数来自 `info()["buffered_lines"]` 和文件大小 —— 文件没清，
        数字不变，看起来"点了清空但列表还是原来那些"；
      * `tail()` 读的是缓冲，缓冲清了、文件还在，重新拉取又按文件/缓冲
        的不一致显示；
      * 旧的 `.1/.2` 备份还躺在卷里占地方。

    所以这里一次做全：缓冲清空、当前文件按"截断"处理（保留下一个 inode，
    不制造"文件消失"的窗口 —— 导出线程可能正好在读）、备份逐个删掉。
    轮转计数归零。

    返回一份**清空前后的事实**（清了多少行、多少字节、删了几个备份），
    调用方把它写进审计日志：清空动作本身必须留痕，这是唯一一条事后能回答
    「谁把日志清了」的记录。
    """
    global _FILE_BYTES, _WRITE_ERROR_REPORTED, _FILE_FAILED

    with _LOCK:
        lines_before = len(_LINES)
        bytes_before = 0

        if _FILE_PATH:
            with contextlib.suppress(Exception):
                bytes_before = os.path.getsize(_FILE_PATH)

        _LINES.clear()

        removed_backups = 0

        if _open_ready():
            # 截断而不是 unlink：unlink 会让"文件不存在"，而导出/预览线程可能
            # 正拿着路径准备读，读到"日志没了"会显示成错误。截断只是变空。
            with contextlib.suppress(Exception):
                with open(_FILE_PATH, "wb"):
                    pass

            _FILE_BYTES = 0
            removed_backups = _remove_rotated()

        # 清空之后允许再报一次写入错误：那是对"新的这段"有用的信息。
        _WRITE_ERROR_REPORTED = False
        _FILE_FAILED = False

        if reason:
            _LINES.append(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"[LOG CLEAR] 服务端日志已清空 by={reason} "
                f"lines={lines_before} bytes={bytes_before} "
                f"backups_removed={removed_backups}"
            )

    return {
        "lines": lines_before,
        "bytes": bytes_before,
        "backups_removed": removed_backups,
    }


def info():
    """给面板看的元信息：文件在哪、多大、缓冲里有多少行。"""
    size = 0

    with _LOCK:
        path = _FILE_PATH
        buffered = len(_LINES)

    if path:
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0

    return {
        "file": os.path.basename(path) if path else "",
        "bytes": size,
        "buffered_lines": buffered,
        "max_bytes": int(getattr(cfg, "LOG_FILE_MAX_BYTES", 0) or 0),
        "keep_files": int(getattr(cfg, "LOG_KEEP_FILES", 0) or 0),
    }


def export(max_bytes=None):
    """
    导出用的正文。

    优先给**文件内容**（完整、带轮转前的历史）；文件读不到就退回内存缓冲。
    为了避免把几 MB 一次塞给面板，超过上限时只给尾部，并在开头注明被截断。
    """
    limit = int(
        max_bytes
        if max_bytes is not None
        else getattr(cfg, "LOG_EXPORT_MAX_BYTES", 3 * 1024 * 1024)
    )

    with _LOCK:
        path = _FILE_PATH

    text = ""

    if path:
        try:
            size = os.path.getsize(path)

            with open(path, "rb") as handle:
                if limit > 0 and size > limit:
                    handle.seek(size - limit)

                text = handle.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""

    if not text.strip():
        with _LOCK:
            text = "\n".join(_LINES) + ("\n" if _LINES else "")

    header = (
        "# CraftLink 中继服务端日志\n"
        f"# 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# 来源文件：{os.path.basename(path) if path else '(仅内存缓冲)'}\n"
    )

    if limit > 0 and len(text.encode("utf-8", errors="replace")) >= limit:
        header += f"# 注意：日志过大，这里只包含最后约 {limit // 1024} KB\n"

    return header + text
