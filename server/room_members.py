"""
MC-Link relay -- room membership.

============================================================================
 WHY THIS EXISTS
============================================================================

Being "in a room" is not the same as being connected to Minecraft. Somebody
can sit in a room watching the ping before they start playing, and the host
position has to survive exactly that state: claimed, but carrying no traffic
yet.

The Minecraft data tunnel cannot carry this information -- once a player is
paired, that socket is raw Minecraft bytes and inserting anything would corrupt
the game protocol. So membership gets its own small control connection.

============================================================================
 OWNER vs HOST
============================================================================

Two roles that are easy to conflate and are deliberately separate:

    OWNER   whoever created the room.
            The room's LIFETIME is bound to them. If the owner's control
            connection drops, the room is destroyed -- which is exactly the
            rule the relay always had, so nothing about that changes.

    HOST    a position inside the room.
            The room's CONNECTIONS are bound to it. Only a room with an
            occupied host position can be joined in Minecraft.

The owner is frequently NOT the host. They can sit in their own room as an
ordinary player, or hand the position to somebody else, and the room keeps
living as long as they stay.

The host position locks the moment it is claimed -- NOT when Minecraft starts.
It is released by a deliberate act: changing identity back to player, being
kicked, or the holder's control connection dropping.

============================================================================
 COST
============================================================================

One TCP connection and a few dozen bytes per person in a room. The Minecraft
data path is not touched at all: a paired tunnel behaves exactly as before.
"""

import asyncio
import auto_relay
from urllib.parse import quote as _auto_quote
import contextlib
import os
import time
import uuid

from relay_config import MAX_PLAYERS, MAX_USERNAME_LEN, RELAY_VERSION

import room_names
import stun_server

# ============================================================
#  ★ v3.9.16：每一次"往 socket 里写"都必须有上限
# ============================================================
#
# v3.9.15 里 `member.send()` / `disconnect_game_connections()` /
# `broadcast_member_state()` 全是**裸 `await writer.drain()`**，没有任何超时。
#
# 一条半开 TCP（对端进程还在、但不读；或者中间网络丢了 FIN，内核不报 EOF）
# 会让 `drain()` 一直等到内核发送缓冲区腾出空间为止 —— 也就是**永远**。而
# `disconnect_game_connections` 是在同一个协程里**串行**通知全房间的：第一条
# 卡住，后面所有人的"host 走了"都发不出去，会话也拆不掉，`host_writer` 还得
# 不到清理，房间从此开不了服。
#
# 这正是"有时候好使、有时候不好使"的那一类偶发：同一个 bug，网络好的时候完全
# 看不见。
#
# 所以：所有写操作统一走 `write_with_timeout()`，默认值 2 秒。2 秒足够把一行
# 几十字节推给任何一个健康的人；超过这个时间的人，本来也收不到"host 走了"。
SEND_DRAIN_TIMEOUT = float(
    os.environ.get("MCLINK_SEND_DRAIN_TIMEOUT", "2") or 2
)

#: ★ v3.9.17：给**成员控制连接**单独放宽的写超时。
#:
#: 为什么它比 `SEND_DRAIN_TIMEOUT` 大：那一个是给"游戏连接/等待池"用的
#: （那里卡住会拖住整个房间的拆除，必须短）；成员广播是"让人知道房间状态"
#: 这件事的**唯一渠道**，一行几十字节推给一个弱网的人可能真的要几秒。
#:
#: 用同一个 2 秒去判它，代价是**把一个活人误判成死人**（v3.9.16 的 P1：
#: 主机位凭空消失 + 成员变僵尸且没人回收）。
MEMBER_SEND_TIMEOUT = float(
    os.environ.get("MCLINK_MEMBER_SEND_TIMEOUT", "5") or 5
)

#: 一个成员连续写失败多少次才真的当作"这条连接死了"。
#:
#: 单次超时不置 `closed`（见 `RoomMember.send`），但**连续**失败说明对端
#: 确实不在了 —— 到这个次数才升级成真失败，并让看门狗能回收他。
MEMBER_SEND_FAIL_LIMIT = int(
    os.environ.get("MCLINK_MEMBER_SEND_FAIL_LIMIT", "3") or 3
)

#: `disconnect_game_connections()` 里"通知所有人"这一步的整体上限。
#:
#: 单条已经有 `SEND_DRAIN_TIMEOUT` 了，这一步是**并发**的（见那里），所以正常
#: 情况它不该比单条慢多少。留一个更大的上限只是防止"并发里有人把事件循环占住"
#  这种谁也没想到的情况 —— 到点也要往下走，房间必须被拆掉。
NOTIFY_BUDGET_S = float(os.environ.get("MCLINK_NOTIFY_BUDGET_S", "5") or 5)


async def write_with_timeout(writer, data, timeout=None):
    """
    写一段字节并 drain，**最多等 `timeout` 秒**。永不抛异常。

    返回 True/False（False = 没写出去）。

    为什么是"吞掉异常"而不是往上抛：这些调用点全在**通知与清理**路径上，
    一条连接写失败绝不能改变"房间里其他人该怎么处理"这件事。

    ============================================================================
      ★ v3.9.17：调用方**不要**把 False 一律当成"这条连接死了"
    ============================================================================
    False 也可能是"对端那一刻没在读"。要区分就用 `write_with_result()`。
    """
    ok, _hard = await write_with_result(writer, data, timeout=timeout)

    return ok


async def write_with_result(writer, data, timeout=None):
    """
    `write_with_timeout` 的**带原因**版本：`(ok, hard_failure)`。

    ============================================================================
      为什么要分开"超时"和"真失败"（v3.9.16 的 P1）
    ============================================================================
    两者都表现为"没写出去"，但含义完全相反：

      * **超时**（`asyncio.TimeoutError`）—— drain 一直没等到内核缓冲腾出空间。
        说明**对端那一刻没在读**：弱网抖一下、对端事件循环卡一下、GC、DB
        同步、一次大广播。连接本身完全可能是好的。**绝不能**据此判死。
      * **真失败**（`ConnectionResetError` / `BrokenPipeError` /
        `OSError`）—— 内核已经明确说这条连接没了。这才是可以置 `closed` 的。

    把它们混成一句 `return False`，代价就是用一个偶发的抖动换一个永久的
    僵尸成员（主机位凭空消失 + 占着名额没人回收）。
    """
    if writer is None:
        return False, False

    limit = SEND_DRAIN_TIMEOUT if timeout is None else timeout

    try:
        writer.write(data)

        await asyncio.wait_for(writer.drain(), timeout=limit)

        return True, False
    except asyncio.TimeoutError:
        # 只说明"那一刻没在读"，不是连接断了。
        return False, False
    except Exception:
        # ConnectionReset / BrokenPipe / 其它 OSError：内核说这条连接没了。
        return False, True

# ============================================================
# 注入点：token 校验 / 版本门放行开关（不能 import relay_server）
# ============================================================
#
# relay_server.py 是以 `__main__` 运行的，从这里 import 它会再加载一整份
# 服务器（空的 rooms 字典，见 room_registry.py 的说明）。所以账号校验和
# 「允许不同版本客户端连接」这两个只有 relay_server 拿得到的东西，用注入
# 的方式传进来 —— 和 room_registry.set_logger 同一条路子。
#
# 注：`_identity_verify` 是 async (token) -> (ok, identity_or_error_code)；
# `_version_bypass` 是 () -> bool。
_identity_verify = None
_version_bypass = None
_identity_release_guest = None
_identity_guest_name = None
_identity_guest_prefix = ""


def set_identity_hooks(verify_token=None, version_bypass=None, release_guest=None,
                       guest_token_name=None, guest_token_prefix=""):
    """relay_server 在模块加载时注册这几个回调。幂等，可重复调用。"""
    global _identity_verify, _version_bypass, _identity_release_guest
    global _identity_guest_name, _identity_guest_prefix

    if verify_token is not None:
        _identity_verify = verify_token

    if version_bypass is not None:
        _version_bypass = version_bypass

    if release_guest is not None:
        _identity_release_guest = release_guest

    if guest_token_name is not None:
        _identity_guest_name = guest_token_name

    if guest_token_prefix:
        _identity_guest_prefix = str(guest_token_prefix)

# The shared room registry. Imported from room_registry -- NOT from
# relay_server, which is started as `__main__`: importing it under its own name
# creates a second copy of the server with its own empty `rooms`, which is how
# "the room does not exist" was answered for a room that was live.
from room_registry import (
    drop_room_if_empty,
    log_line,
    remember_mode,
    rooms,
    rooms_lock,
)


def _log(*parts):
    """
    Log through the server's own logger when it is available.

    The server hands its logger to room_registry at import time; this is just
    the local name for it. Never raises: a log line must not be able to break
    the thing it is describing.
    """
    log_line(*parts)


def _peer_ip(writer):
    """
    The public IP a control connection arrived from ("" when unknown).

    `relay_server.py` has its own copy of this. It is duplicated rather than
    imported on purpose: `relay_server` runs as `__main__`, so importing it from
    here would create a SECOND copy of the whole server (see room_registry.py's
    note). Two lines of duplication beat that.
    """
    try:
        peer = writer.get_extra_info("peername")
    except Exception:
        return ""

    if isinstance(peer, (tuple, list)) and peer:
        return str(peer[0])

    return ""


def _event(name, **fields):
    """
    记一条结构化事件。

    `relay_events.py` 是纯函数（只拼字符串），所以这里直接 import 用是安全的
    —— 它没有共享可变状态，不存在"导入两份"的问题（那个坑只对持有状态的模块
    成立，见 room_registry.py 的说明）。

    ============================================================================
      NEVER RAISES —— 这一条不是口号，是被真实事故逼出来的
    ============================================================================

    第一版这里只包了 `log_line` 和 import，**没有包字段求值**：`previous=...`
    里的属性名写错（`Room` 上没有 `host_name`）时，异常从**调用点**抛出来，
    被房间成员连接的顶层 handler 抓到，于是整条连接被当成"成员控制连接出错"
    断开。表现是：认领主机位直接失败，日志里只有一句
    `[ROOM MEMBER ERROR] ... 'Room' object has no attribute 'host_name'`。

    日志是**旁路**：它坏了最多是少一行记录，绝不能把正在建立的连接搞死。
    所以调用点也要能抗住字段求值出错 —— 传进来的值用 `getattr(..., None)` 之类
    取，并且在**包住整个调用**的地方兜底。

    实现上做两件事：
      * import 与拼接都在 try 里（已经做了）；
      * 新增 `safe_event()`，字段用 lambda 延迟求值，任何一个字段炸了都只丢
        那一个字段，其余照记 —— 调用点不必为了"日志别炸"牺牲可读性。
    """
    try:
        import relay_events

        log_line(relay_events.event(name, **fields))
    except Exception:
        pass


def safe_event(name, **getters):
    """
    和 `_event` 一样，但字段是**延迟求值**的：`safe_event("x", room=lambda: room.name)`。

    给"取这个字段本身可能失败"的调用点用（属性可能不存在、对象可能已经拆了）。
    一个字段炸了只丢它自己，其余字段照常记录 —— 比起整条事件丢掉，
    少一个字段仍然能分析。
    """
    fields = {}

    for key, getter in getters.items():
        try:
            fields[key] = getter()
        except Exception:
            fields[key] = "<field_error>"

    _event(name, **fields)


class RoomMember:
    """One person in a room, with their own long lived control connection."""

    def __init__(self, room_name, reader, writer, client_id="", username="",
                 token="", is_owner=False):
        self.room_name = room_name
        self.reader = reader
        self.writer = writer
        self.client_id = client_id
        self.username = username
        self.token = token

        #: 中继发给这个游客的**临时会话 token**（登录用户是空串）。它绑定在
        #: "客户端与中继的登录会话"上：房间进出不动它，换账号登录（IDENTITY
        #: 用有效账号 token 改名）时才销毁。见 relay_server 的 guest session。
        self.guest_token = ""

        #: 这个成员在 room.members 里的键。由 add_member() 决定：正常情况下
        #: 就是 client_id；当同一个 client_id 被另一个账号占用时，会变成
        #: "client_id:username"，这样两个人可以并存而不会互相顶掉。
        #: 所有"按 key 查成员"的地方都必须用它，不能用裸 client_id。
        #: 两个都缺时给一个唯一兜底值 —— 空 key 会让两个匿名成员互相顶掉。
        self.member_key = client_id or username or f"anon-{uuid.uuid4().hex[:12]}"

        #: True for the member who created the room. At most one, ever.
        self.is_owner = bool(is_owner)

        #: True while this member holds the room's host position.
        self.is_host = False

        #: 「这个成员点过开始连接了吗」的**显式上报**（ADDITIVE，2026-09-28）。
        #:
        #: 语义（用户口径）：**用户在客户端点了「开始连接」**，不是"已经有
        #: PLAYER / HOST 连接"。老客户端永远不发这一行 -> 默认 False，此时
        #: `started=` 仍然按"连接本身"现算（见 started_client_ids），行为一个字
        #: 不变。新客户端发 `STARTED 1` / `STARTED 0` 之后，这个标记与"连接
        #: 现算"取**并集**：点了开始、但还没开游戏（stun 模式打洞先于游戏）
        #: 的成员也会出现在 `started=` 里，Host 才知道该往谁那里打。
        self.connecting = False

        #: 这条成员连接**收到自己的 OK 回包了吗**（见 broadcast_member_state）。
        #:
        #: ============================================================================
        #:  ★ 为什么必须有这道闸门（用户 2026-09-29 实测：自动重连整条失败）
        #: ============================================================================
        #: `handle_member_join` 的顺序是 `room.add_member(member)` -> 记事件（await）
        #: -> 写 `OK JOIN`。中间那些 await 让**别的成员**的
        #: `broadcast_member_state()` 可以先跑，而它遍历的是 `room.members` ——
        #: 于是一条 `HOST_STATE ...` 会被写进这个**还没收到 OK JOIN** 的新连接。
        #:
        #: 客户端 `RoomSession._request()` 只读第一行当握手应答，读到的却是
        #: `HOST_STATE` -> 抛「服务器应答异常：HOST_STATE ...」-> 自动重连整条
        #: 失败（用户日志：`[18:12:09] automatic rejoin failed: 服务器应答异常：
        #: HOST_STATE host=1 ...`）。服务端随后看到"成员控制连接已关闭" -> 走
        #: 座位宽限，玩家实际被踢出房间。
        #:
        #: 所以：**OK 回包写完之前，谁都不许往这条连接推广播**。
        #:
        #: 默认 True = 老行为（别处直接造出来的成员照常收广播）；只有
        #: `handle_member_join` 在 `add_member()` 之前显式置 False，写完 OK 回包
        #: 再置 True。
        self.ready = True

        #: Last time the control connection said anything (monotonic). Fed to
        #: the watchdog so a half-open TCP connection is still detected.
        self.last_seen = time.monotonic()

        #: Public IPs this member has announced as its OWN endpoint
        #: (`DIRECT_CAND <ip> ...`). Used to keep `DIRECT_INGRESS` from becoming
        #: an open UDP reflector: the relay will only probe an address the
        #: requester itself published (or the address it connected from).
        self.published_ips = set()
        self.capabilities = set()
        self.public_ip = _peer_ip(writer)

        self.joined_at = time.time()
        self.send_lock = asyncio.Lock()
        self.closed = False

        # ============================================================================
        #  ★ v3.9.17：写**超时**不等于连接断了
        # ============================================================================
        #
        # v3.9.16 里 `send()` 一旦超过 `SEND_DRAIN_TIMEOUT` 就把 `closed` 置成
        # True。而 `closed` 在这里的语义是"这条连接死了"，它会导致：
        #
        #   * `host_member` 过滤掉他（:523）-> **主机位凭空消失**，房主明明在线
        #     却显示"无人认领"，玩家拿 `NO_HOST`；
        #   * 读循环不检查 `closed`，socket 还活着、循环还在跑；
        #   * 看门狗对 `closed` 成员 `continue`（:3741）-> **没人来清他**，
        #     他永久占着房间名额（ROOM_FULL）却再也收不到任何广播。
        #
        # 而"写超时"只说明**对端那一刻没在读**（弱网抖一下、事件循环卡一下、
        # GC、DB 同步、大广播），根本不是连接断了。把这两件事当成一件事，
        # 就是用一个偶发的抖动换一个永久的僵尸。
        #
        # 所以分开记：超时只 `last_send_failed`，真正的失败（ConnectionReset /
        # BrokenPipe / EOF）才置 `closed`。
        self.last_send_failed = 0.0
        self.send_failures = 0

        #: 成员写超时单独的预算（比 `SEND_DRAIN_TIMEOUT` 宽）。
        #:
        #: 控制连接上的一行只有几十字节，推给一个健康的人用不了 1 秒；但成员
        #: 广播是"要让他知道房间状态"这件事的全部渠道，宁可多等一会儿也不要
        #: 误判。5 秒是"弱网抖动穿得过、真死的连接也不会拖太久"的折中。
        self.send_timeout = MEMBER_SEND_TIMEOUT

        #: 为什么这个人离开房间。四条移除路径各自在动手前写好，最终清理路径
        #: （handle_member_join 的 finally）据此记**唯一一行**
        #: `[HOST RELEASE] room=... by=... reason=leave|drop|kick|timeout`。
        #: 默认 "drop"：socket 断了、进程被杀了、连接出错都是它。
        self.removal_reason = "drop"

        #: ============================================================
        #:  「连接断了，但房间身份还在」——座位宽限期内的状态
        #: ============================================================
        #
        # 用户口径：连接和身份是两回事。控制连接断了、但 token 仍有效的成员
        # 不算离开房间：这个标记为 True 时，成员**仍在 `room.members` 里**
        # （所以 `host_claimed` / `host_name` / 成员表都不变，主机位保留），
        # 只是连接层面是 idle —— 直到宽限期满或本人重连顶替。
        #
        # 绝不要用 `detached` 推导"没有 Host"：那是两套状态（身份/职位 vs
        # 连接）。
        self.detached = False

        #: 什么时候进入 detached（monotonic），宽限期据此计时。
        self.detached_at = 0.0

        #: 这个成员到中继的往返时间（毫秒，实测值），由**中继主动**发起的
        #: PING/PONG 探测量出来（见 member_rtt_loop）。
        #:
        #: None = 还没测到（没回过/超时）—— 广播里这个成员就【整条不出现】，
        #: 界面上显示 `-`。绝不编一个数字。
        self.rtt_ms = None

        #: 在途探测：`(token, 发出时刻, asyncio.Event)`。同一时刻只有一条
        #: 在途探测（member_rtt_loop 串行），所以不存在"自己的探测排队"污染
        #: 测量口径。收到匹配的 PONG 时由控制循环 set()。
        self._rtt_wait = None
        self._rtt_seq = 0

        #: 上次把 RTT 广播出去的值/时刻，用来抑制抖动导致的刷屏。
        self._rtt_pushed = None
        self._rtt_pushed_at = 0.0

    # --------------------------------------------------------

    @property
    def label(self):
        return self.username or self.client_id or "?"

    @property
    def display_name(self):
        """
        给人看的名字（登录名 / 中继分配的游客名 / 最后兜底 client_id）。

        正常情况下 username 在握手时就已经被填好（登录名，或"游客xxxxxx"），
        所以这里只是最后一道保险：任何路径都不许让一个成员显示成空字符串 ——
        那正是"房间广播里只有 client_id，前端只能显示未知"的来源。
        """
        name = (self.username or "").strip()

        if name and name != "-":
            return name

        return self.client_id or "?"

    def snapshot(self):
        return {
            "client_id": self.client_id,
            "username": self.username,
            "display_name": self.display_name,
            "is_owner": self.is_owner,
            "is_host": self.is_host,
            "joined_at": self.joined_at,
            "idle": round(time.monotonic() - self.last_seen, 1),
            "rtt_ms": (
                round(self.rtt_ms) if self.rtt_ms is not None else None
            ),
        }

    async def send(self, line):
        """
        Write one control line. Never raises.

        One member with a dead socket must not be able to break a broadcast to
        everybody else, so a failure is recorded on the member and swallowed.

        座位宽限期内的成员（`detached`）**故意当只读**：他的控制连接已经断了，
        往里写只会失败，而失败会把 `self.closed` 置 True —— 那会让
        `host_member` 认不出他，主机位就"看起来没了"（用户明确要求连接断了
        不许动主机位）。所以 detached 直接返回 False，不碰任何状态。
        """
        if self.closed or self.detached or self.writer is None:
            return False

        async with self.send_lock:
            # ★ v3.9.16：裸 `await drain()` 没有上限，一条半开连接能把整个
            # 房间的状态推进卡死（见模块顶部 `SEND_DRAIN_TIMEOUT` 的说明）。
            #
            # ★ v3.9.17：但**超时不等于死了**。下面按"超时 / 真失败"分开处理，
            #   只有真失败（或连续失败到上限）才置 `closed`。
            ok, hard = await write_with_result(
                self.writer,
                line.encode(),
                timeout=getattr(self, "send_timeout", None)
                or MEMBER_SEND_TIMEOUT,
            )

            if ok:
                # 写通了就把失败计数清零：偶发抖动不该累积成"这个人坏了"。
                self.send_failures = 0

                return True

            self.last_send_failed = time.monotonic()
            self.send_failures += 1

            if hard:
                self.closed = True
            elif self.send_failures >= MEMBER_SEND_FAIL_LIMIT:
                # 连续几次都超时 —— 这时候才判死。单次超时不判。
                self.closed = True

            if self.closed:
                # ★ v3.9.18：写失败判死时必须把"这是房主"标出来。
                #
                # 否则排障时看不出是**网络**问题还是**人真的走了**——而这两者
                # 的处理完全不同（前者要等他重连、后者要释放主机位）。
                # 尤其是房主：主机位凭空消失这种事，日志里至少要能一眼看出
                # 是"写不通判死"还是"他自己断的"。
                _log(
                    "[MEMBER DEAD]",
                    f"room={self.room_name}",
                    f"user={self.label}",
                    f"send_failures={self.send_failures}",
                    f"hard_failure={hard}",
                    f"is_host={bool(self.is_host)}",
                    f"is_owner={bool(self.is_owner)}",
                    "reason=could not write to this member; marked closed",
                )

            return False

    # --------------------------------------------------------

    async def probe_rtt(self, token, event):
        """
        发一条 RTT 探测（`PING <token>`），返回发出时刻（monotonic）；发不出去
        返回 None。

        时间戳取在 `write()` **紧前**、并且是在 `send_lock` 里 —— 也就是说
        只有"这一行真的进了内核缓冲"之前的那一瞬。发出去之后才 await drain，
        所以发送侧排队不会算进 RTT；同一成员同一时刻只有一条在途探测，
        也不存在探测自己排队。收到 PONG 的一端（member_control_loop）在把
        时间戳取在**读完那一行之后、任何 await 之前**，所以接收侧排队同样
        不算。这是"稳"的口径：量的是这条控制消息的真实往返。

        老客户端/不认这条 PING 的进程只是不回 PONG —— 那就永远没有数字，
        广播里这个成员整条不出现（见 member_rtt_loop）。
        """
        if self.closed or self.writer is None:
            return None

        async with self.send_lock:
            sent = time.monotonic()

            try:
                self.writer.write(f"PING {token}\n".encode())
                # 必须在 await 之前把"在途探测"挂上：drain() 会让出事件循环，
                # 快到极点的 PONG 可能就在那一次让出里回来。
                self._rtt_wait = (token, sent, event)

                # ★ v3.9.16：同样不许无限等（RTT 探测卡住会一直占着
                # `send_lock`，那条连接的**所有**通知 —— 包括"host 走了"——
                # 都会被它挡在后面）。
                #
                # ★ v3.9.17：RTT 探测拿不到数字**不该**把成员判死 ——
                # 探测本来就是"测不到就没有数字"（老客户端永远不回 PONG）。
                # 超时只丢弃这一次探测，不碰 `closed`。
                ok, hard = await write_with_result(
                    self.writer, b"", timeout=SEND_DRAIN_TIMEOUT
                )

                if not ok:
                    self._rtt_wait = None

                    # 只有内核明确说"连接没了"才判死。
                    if hard:
                        self.closed = True

                    return None

                self.send_failures = 0

                return sent
            except Exception:
                self.closed = True
                self._rtt_wait = None
                return None


# ============================================================
# Room-level membership operations
# ============================================================
#
# These are mixed into Room via MembershipMixin so relay_server keeps owning
# the Room class itself (and the frozen data path stays where it is).

class MembershipMixin:
    """Membership state and operations for a Room."""

    #: 创建者本人的成员连接被新连接顶替过（见 add_member）。
    #: Owner 身份不可转移，所以这只是给日志/排查用的标记：那个被顶替的
    #: owner 对象最终会走 remove_member() -> True -> 房间销毁。
    owner_superseded = False

    #: ============================================================================
    #:  ★ 用户 2026-10-01 第 3 条：客户端**自己上报**的连接状态（纯显示）
    #: ============================================================================
    #: 成员控制连接上的一行 ADDITIVE 信令：
    #:
    #:     STATE <idle|waiting|pairing|connected>
    #:
    #: 客户端在**它自己的本地连接状态变化时**发一次（另有 1Hz 心跳，和成员列表
    #: 的刷新频率一致）；中继只把它存下来、并在 `conn=` 里**优先用它**。
    #:
    #: **绝不参与任何门禁/配对/打洞决策**：`conn=` 从头到尾只喂 HOST_STATE 和
    #: 面板快照（显示用）。没有上报过的成员（老客户端）走原来的推导，行为不变。
    reported_conn = ""

    # ---- queries ----

    @property
    def owner(self):
        """The member who created this room, or None."""
        for member in self.members.values():
            if member.is_owner:
                return member

        return None

    @property
    def host_member(self):
        """The member currently holding the host position, or None."""
        for member in self.members.values():
            if member.is_host and not member.closed:
                return member

        return None

    def host_state(self):
        """
        **主机状态的唯一来源**：`(claimed, display_name)`。

        WHY ONE FUNCTION: `host=` 和 `host_name=` 以前是两条独立算出来的路
        （一个读 `host_claimed` 属性、一个读 `host_member`），中间只要有一条
        路径只更新了一半，广播里就会出现 "host=1 但 host_name=-" 这种自相矛盾
        的一对 —— 客户端把 `-` 解成空串，界面上就是「有房主 / 主机：」（用户
        日志里那条 `host_claimed=True, host_name=''`）。现在两个字段都从这里
        出，物理上不可能不一致。

        两条持有方式都算"有主机"：
          * V3 成员认领（`host_member`）—— 显示名就是他在房间里的名字；
          * 老式 `HOST <room>` 控制连接（`host_writer`）—— 显示名是它握手时
            报的 username（没有就退回 client_id）。
        两个都没有 = 没有主机，此时 `claimed` 必为 False、名字必为空串。
        """
        member = self.host_member

        if member is not None:
            name = (member.display_name or "").strip()

            return True, name or (member.client_id or "")

        if getattr(self, "host_writer", None) is not None:
            # 老式 HOST 控制连接：display name 是握手带的 username，空则退回
            # client_id —— 和 RoomMember.display_name 同一套兜底，绝不给空串。
            name = (
                str(getattr(self, "host_username", "") or "").strip()
                or str(getattr(self, "host_id", "") or "").strip()
            )

            return True, name

        return False, ""

    @property
    def host_claimed(self):
        """Whether the room can be joined in Minecraft right now.

        和 `host_state()` 同源：两种持有方式（成员认领 / 老式 HOST 控制连接）
        都算有主机。
        """
        return self.host_state()[0]

    def take_host_from(self, member):
        """
        如果 `member` 此刻是**房间主机位的持有者**，就把位置空出来。

        权威判定，刻意不看两个"方便但会过期"的东西：

          * 不看 `member.is_host`：同一个 client_id + 同账号重连时，位置会被
            搬到**新**的成员对象上，旧对象上的标记还留着 True；
          * 不看 `self.host_member`：成员看门狗（member_watchdog）在调用清理
            路径**之前**就把 `member.closed` 置 True，而 `host_member` 会把
            closed 的成员过滤掉 —— 于是"他其实是主机"这件事会被漏掉，
            位置永远空不出来（正是用户报的"认领 host 的人走了，别人认领不了"）。

        唯一的真相是 `self.members` 里的键值对是不是这个对象。
        返回 True 表示这一次真的释放了主机位。
        """
        key = getattr(member, "member_key", None) or member.client_id

        if self.members.get(key) is not member:
            # 这条连接已经被重连顶替：位置（如果搬过去了）归新连接，不能清。
            return False

        if not member.is_host:
            return False

        member.is_host = False

        # 防御性收尾：任何残留的 is_host 一起清掉，保证"位置空 = 没人持有"。
        for other in self.members.values():
            if other is not member and other.is_host:
                other.is_host = False

        self.mark_dirty()

        return True

    async def release_legacy_host_for(self, member):
        """
        老式 `HOST <room>` 控制连接如果属于这个成员，一并释放。

        为什么必须做：v3 客户端的开服控制连接（`HOST <room>`）和房间成员连接
        用的是**同一个 client_id**。人离开房间后如果只清成员主机位，那条老式
        连接还挂着 `host_writer`，房间在面板/`world_players` 里仍然是"有人开服"，
        而且它的 socket 没人再看着（host_watchdog 只认 `host_writer`）——
        等于留下一条僵尸开服连接。归属按 client_id 优先、username 兜底判定。

        返回被释放的 writer（None = 不属于他 / 本来就没有）。
        """
        writer = None

        async with self.lock:
            if self.host_writer is None:
                return None

            cid = str(getattr(member, "client_id", "") or "").strip()
            uname = str(getattr(member, "username", "") or "").strip()
            host_cid = str(getattr(self, "host_id", "") or "").strip()
            host_uname = str(getattr(self, "host_username", "") or "").strip()

            belongs = bool(
                (cid and host_cid and cid == host_cid)
                or (uname and host_uname and uname == host_uname)
            )

            if not belongs:
                return None

            writer = self.host_writer
            self.host_reader = None
            self.host_writer = None
            self.host_id = ""
            self.host_username = ""
            self.host_ip = ""
            self.host_token = ""
            self.host_last_seen = None

        self.mark_dirty()

        # 关掉那条 socket：光把引用清掉会留下一条没人负责、看门狗也不管的
        # 控制连接（它会继续发 PING，而 host_watchdog 只认 host_writer）。
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass

        return writer

    def member_count(self):
        """How many people are in the room, owner included."""
        return len(self.members)

    def world_player_count(self):
        """
        How many players are actually in somebody's world.

        This is the number the client shows as "已加入世界玩家". The host
        counts as one as soon as they hold the position, whether or not
        Minecraft is running, and each real paired player adds one more.

        The owner is NOT counted separately: sitting in a room is not being in
        a world, and counting them would make an idle room look occupied.
        """
        # 口径必须和 relay_server.Room.world_players() 完全一致：
        # 只数**真的进入世界**的会话，而且按玩家名去重。
        #
        # 客户端界面上的「已加入世界」就是这条 HOST_STATE 的 world= 来的，
        # 数 len(sessions) 会把"配上对但没走完握手"的幽灵会话也算成人
        # （用户看到「房间 3 人，却显示已加入世界 9 人」）。
        counter = getattr(self, "world_players", None)

        if callable(counter):
            return counter()

        return (1 if self.host_claimed else 0) + len(self.sessions)

    def can_join_minecraft(self):
        """
        Whether a PLAYER connection should be accepted.

        Without a host there is nothing to connect to, so players are refused
        with a readable reason instead of being paired with a tunnel that will
        never exist.
        """
        return self.host_claimed and not self.closed

    def snapshot_members(self):
        return [m.snapshot() for m in self.members.values()]

    # ---- mutations ----

    def add_member(self, member):
        """
        Register a member.

        The key is the client id, and a second connection with the same client
        id is normally the same person reconnecting -- that is how the host
        position and the ownership survive a network blip.

        ============================================================================
         BUT IT IS ONLY A RECONNECT WHEN THE ACCOUNT MATCHES
        ============================================================================

        `client_id` is chosen by the CLIENT and travels in the handshake line,
        so treating "same client id" as "same person" regardless of account let
        anybody who guessed it (or reinstalled and reused it) evict the real
        owner, inherit the room, and destroy it by leaving. That is the reported
        「个别情况下可能会出现房主被篡夺」.

        Different account with the same client id is now simply a second member:
        both stay, and nobody inherits anything.
        """
        key = member.client_id or member.username

        # ============================================================================
        #  空的 client_id / username 不许变成空 key，也不许都用同一个 "?"
        # ============================================================================
        #
        # 中继会在握手时给匿名连接补一个唯一的 client_id（见 relay_server 的
        # [ANON CLIENT]），所以正常路径上这里不是空的。但 add_member 是所有
        # 成员进入房间的唯一入口，必须有最后一道保险：两个都空时用 "?" 会让
        # 第二个人把第一个人顶掉（两个人共享一个 key），而且日志里出现空 key。
        # 这里给每个这样的成员一个唯一值。
        if not key or key == "-":
            key = f"anon-{uuid.uuid4().hex[:12]}"

        previous = self.members.get(key)

        if previous is not None and previous is not member:
            same_person = bool(
                previous.username
                and member.username
                and previous.username == member.username
            )

            # ================================================================
            #  ★ 凭证也能证明"还是同一个人"（判据要和 relay_server 的
            #    `_same_seat` / ROOM_FULL 放行口径一致）
            # ================================================================
            #
            # 只比显示名会漏掉一种**真实重连**：同一个 client_id 回来了，但这次
            # 报的名字和上次不同（游客 token 换了 / 名字被中继重新分配）。漏掉
            # 的后果不是"顶替"，而是**多出一条记录**：
            #
            #   * `room.members` 出现两条同一个 client_id 的成员（key 变成
            #     `cid:name`），`members=` 比客户端能画出来的行数多 1 —— 用户
            #     2026-09-29 第 5 条「房间内仅有四人时却显示有五人，成员列表正常」；
            #   * 座位宽限期那条路径（`hold_member_seat` 等 `members.get(key)
            #     is not member`）永远等不到交接，只能干等满 45 秒。
            #
            # 所以把**凭证相同**也算同一个人（guest token / 账号 token）。
            # 这**不会放宽防篡夺**：拿不出同一个凭证的人仍然算"另一个人"，
            # `[MEMBER ID REUSED]` 那条分支一个字没改。
            if not same_person:
                with contextlib.suppress(Exception):
                    prev_guest = str(getattr(previous, "guest_token", "") or "")
                    new_guest = str(getattr(member, "guest_token", "") or "")
                    prev_token = str(getattr(previous, "token", "") or "")
                    new_token = str(getattr(member, "token", "") or "")

                    same_person = bool(
                        (prev_guest and prev_guest == new_guest)
                        or (prev_guest and prev_guest == new_token)
                        or (prev_token and prev_token == new_token)
                    )

            if same_person:
                # ============================================================
                #  「座位宽限期内本人回来」是**重连**，不是顶替
                #  （用户 2026-09-29：连接和身份是两回事）
                # ============================================================
                #
                # `previous.detached` = 他的控制连接断过、但 token 仍有效，
                # 中继把座位（含主机位/所有权）原地留着等他回来。回来的这条
                # 连接是**同一个人**，所以：
                #   * 座位、主机位、所有权全部搬给新连接；
                #   * 房间**不销毁**、已冻结的房间参数（含连接模式）**不变**；
                #   * 也不走下面那段"Owner 顶替 -> 房间随创建者销毁"。
                # 这不是"Owner 可转移"：凭证证明还是同一个人，且他离开房间
                # （宽限期满 / 主动离开 / 被踢）时房间照样销毁。
                detached_return = bool(getattr(previous, "detached", False))

                previous.closed = True

                if detached_return:
                    previous.detached = False

                    if previous.is_host:
                        member.is_host = True

                    if previous.is_owner:
                        member.is_owner = True
                        previous.is_owner = False

                    # "一个房间最多一个 Host"：搬完清掉任何残留。
                    for other in self.members.values():
                        if other is not member and other.is_host:
                            other.is_host = False

                    _log(
                        "[MEMBER RECONNECT]",
                        f"room={self.name}",
                        f"user={member.username or '-'}",
                        f"client_id={member.client_id or '-'}",
                        f"seat=kept host={bool(member.is_host)} "
                        f"owner={bool(member.is_owner)}",
                        "连接断了但身份还在 -> 座位/主机位/房间都保留",
                    )
                else:
                    # ============================================================
                    #  Host 位置可以跟着**同一个人**的新连接走，但必须保证
                    #  "一个房间最多一个 Host"
                    # ============================================================
                    #
                    # kick_test.py::test_superseded_connection_cannot_kick 钉住了
                    # "同 client_id + 同账号重连时 host 位置跟到新连接"这条既有
                    # 行为。但不能再像以前那样只往新对象上打一个 is_host：只要
                    # 旧对象上的标记因为任何一条路径没清掉，房间里就会同时存在
                    # 两个 is_host，`host_member` 按成员顺序取第一个 -> HOST_STATE
                    # 报出错的 host_name、player 的打洞目标指向错的人
                    # （用户 P0 #1「打向房主而不是 host」）。
                    #
                    # 所以这里在搬运的同时，把**其它任何** is_host 一起清掉。
                    if previous.is_host:
                        member.is_host = True

                        for other in self.members.values():
                            if other is not member and other.is_host:
                                other.is_host = False

                    # ============================================================
                    #  Owner 身份**不可转移**（设计基准 §A，用户 2026-09-27 确认）
                    # ============================================================
                    #
                    # 这里以前把 previous.is_owner 复制到新成员对象上，于是
                    # "同 client_id + 同账号"再来一条连接就继承了房间所有权：
                    # 创建者的旧连接还没清理完，新的连接已经是 owner，房间的寿命
                    # 跟着一条"别人以为是自己"的连接走 —— 正是用户报的
                    # 「host 错误继承了房主的位置/身份」。
                    #
                    # 基准：Owner = 创建房间的账号，身份永久绑定房间、不可转移；
                    # Owner 一离开房间立即销毁房间。所以创建者的连接被新连接顶替
                    # = 创建者本人已经不在这条连接上 -> 房间必须销毁，而不是把
                    # 所有权搬到新对象：
                    #   * 这里关掉创建者那条旧 socket，让它的清理路径
                    #     （handle_member_join 的 finally）立刻跑起来；
                    #   * remove_member() 对"已被顶替的 owner 对象"仍然返回 True，
                    #     于是房间走唯一的销毁路径（所有人断开、Host 释放）。
                    if previous.is_owner:
                        self.owner_superseded = True

                        _log(
                            "[OWNER SUPERSEDED]",
                            f"room={self.name}",
                            f"client_id={key}",
                            "创建者的连接被新连接顶替 -> 房间随创建者销毁（Owner 不可转移）",
                        )

                        with contextlib.suppress(Exception):
                            if previous.writer is not None:
                                previous.writer.close()
            else:
                # 同一个 client_id、不同的账号：不是重连，也不能踢人。
                # 换一个 key 让两个人并存，谁都不继承任何身份。
                #
                # 顺手记一笔：正常安装的客户端每台机器一个随机 id，出现这种
                # 情况通常意味着有人复制了整套配置目录（或两台机器共用一个），
                # 有日志才查得出来。
                _log(
                    "[MEMBER ID REUSED]",
                    f"room={self.name}",
                    f"client_id={key}",
                    f" 已有账号={previous.username or '-'}",
                    f"新账号={member.username or '-'}",
                    "-> 视为两个不同的人，不继承所有权",
                )

                key = f"{key}:{member.username or 'anon'}"

                while key in self.members:
                    key = f"{key}~"

        member.member_key = key
        self.members[key] = member
        self.mark_dirty()

        return member

    def remove_member(self, member):
        """
        Drop a member. Returns True when this member was the OWNER.

        The caller uses that to decide whether the whole room goes away: the
        room's lifetime is bound to the owner, not to the host.

        The lookup uses the member's OWN key (set in add_member). Using the raw
        client id again would remove the wrong entry whenever two accounts share
        one client id -- including the case that matters most, where it would
        delete somebody else's membership.
        """
        key = getattr(member, "member_key", None) or member.client_id

        if self.members.get(key) is not member:
            # Already replaced by a reconnect. Ownership never transfers, so if
            # THIS object was the room's creator the room must die with the
            # creator's own connection -- even though the dict entry now points
            # at a newer member object (see add_member). The caller uses this
            # return value to decide "the whole room goes away".
            return bool(member.is_owner)

        del self.members[key]
        member.closed = True

        if member.is_host:
            # The host position is free again. Any Minecraft sessions are the
            # caller's problem; the room itself survives.
            member.is_host = False

        self.mark_dirty()

        return member.is_owner

    def claim_host(self, member):
        """
        Take the host position. Returns (ok, message).

        First come, first served, one at a time. Two simultaneous claims cannot
        both win because this method contains no `await`: the check and the
        assignment happen in one uninterrupted step, so there is no window
        between them for the event loop to switch tasks.
        """
        if member.closed:
            return False, "连接已断开"

        if self.closed:
            return False, "房间已关闭"

        if self.members.get(
            getattr(member, "member_key", None) or member.client_id
        ) is not member:
            # This connection has already been superseded by a reconnect.
            return False, "连接已被新的登录取代"

        existing = self.host_member

        if existing is member:
            return True, "你已经拥有房主身份"

        if existing is not None:
            return False, f"房间已有房主（{existing.label}），无法重复认领"

        member.is_host = True
        self.mark_dirty()

        return True, "已获得房主身份"

    def release_host(self, member):
        """Give the host position up. Safe to call when not holding it."""
        if not member.is_host:
            return False

        member.is_host = False

        # 和 take_host_from() 同一个不变量：位置一旦空出来，就不许再有别的
        # 成员挂着 is_host（"位置空 = 没人持有"）。正常路径不会有第二个，
        # 但这里多一道防御，防止任何历史脏状态让 host_member 取到错的人。
        for other in self.members.values():
            if other is not member and other.is_host:
                other.is_host = False

        self.mark_dirty()

        return True


# ============================================================
# Control loop
# ============================================================

#: How long a cooperative close waits for the clients to leave by themselves
#: before it force-closes the stragglers. Long enough for a client on a slow
#: link to react to ROOM REVOKED, short enough that the admin's request still
#: answers quickly.
COOPERATIVE_CLOSE_GRACE = 6.0

#: How long a member may go silent before the watchdog reaps them.
#: The client pings every 3 s, so this tolerates three misses.
#:
#: 可用环境变量覆盖（只在测试里用；生产不设，行为与以前逐字一致）：把
#: "超时释放主机位"这条路径压到几秒内跑完，不必在回归里真等 15 秒。
def _env_seconds(name, default):
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)

    return value if value > 0 else float(default)


MEMBER_IDLE_TIMEOUT = _env_seconds("MCLINK_MEMBER_IDLE_TIMEOUT", 15)

#: How often the watchdog looks.
MEMBER_WATCHDOG_INTERVAL = _env_seconds("MCLINK_MEMBER_WATCHDOG_INTERVAL", 3)

#: 未配对的 PLAYER 等待者最多能在池子里待多久（秒），到点由看门狗收掉。
#:
#: 见 `reap_stale_waiters()`：池子以前完全没有过期机制，半开的探测连接能一直
#: 占着房间名额（用户第三轮第 4 条：两人房也会「房间已满」）。
#:
#: 取值比客户端自己的等待上限（30 秒）更宽，确保"真的在排队等隧道"的连接不会
#: 被误收；可用 `MCLINK_WAITER_MAX_WAIT` 覆盖（测试把压到几秒）。
WAITER_MAX_WAIT_S = _env_seconds("MCLINK_WAITER_MAX_WAIT", 45)

#: ============================================================================
#:  座位宽限（`MCLINK_MEMBER_SEAT_GRACE`）
#: ============================================================================
#
# 用户 2026-09-29 再次强调：「**连接和身份是两回事**」：
#
#   * 仅仅是**连接层面**的抖动/断开，而房间身份仍在（**token 仍有效**）
#     -> 不算离开房间：座位保留、**主机位保留**、客户端应当自动重连；
#   * 只有房间**身份结束**（主动 LEAVE / 被踢 / 超时清退 / token 失效 /
#     被服务端清退）才算离开房间，这时才释放主机位。
#
# 但一条 TCP 控制连接断掉的那一刻，中继无法区分"这个人掉线了"和"他马上
# 就会重连回来" —— 能区分它们的是**身份凭证**：token 还认得出这个人，
# 就给这段连接一个宽限期，位子（以及 is_host / is_owner）原地留着：
#
#   * 断线瞬间：只断**游戏连接**（可识别原因），广播 `conn=` 变 idle、
#     `host=1` + `host_name=<他>` 保持不变（UI：`xxx（未开服）`）；
#   * 同一个身份在宽限期内重新 JOIN/CREATE -> `add_member` 把座位（含
#     主机位/所有权）交给新连接，**房间不销毁、模式不变**；
#   * 宽限期走完还没回来 -> 按"真正离开"走原来那条唯一清理路径
#     （`reason=timeout`），释放主机位、必要时销毁房间。
#
# 没有 token 的连接（老客户端 / 免登录且没带游客 token）不享受宽限：没有
# 任何东西能证明"还是同一个人"，断线即离开 —— 与改动前逐字一致。
MEMBER_SEAT_GRACE = _env_seconds("MCLINK_MEMBER_SEAT_GRACE", 45.0)


async def member_identity_alive(member):
    """
    「这个成员的身份凭证现在还认得出他吗？」

    这是"连接断了 != 离开房间"的**唯一判据**（用户 2026-09-29 口径）：
    身份还在 -> 座位/主机位保留 + 等他自己重连；身份没了 -> 按离开处理。

    认得出 = 下列之一：
      * 客户端回传的 token 是一条还活着的**游客会话**（免登录客户端从
        `OK JOIN` 的 `guest=` 拿到、之后每条握手都回传）；
      * **中继自己发给这个成员的游客 token 还在**（`member.guest_token`）——
        客户端一定会在重连时把它带回来，这就是"身份没结束"的证据；
      * token 能被账号体系**校验通过**（登录用户）；
      * 账号体系关着（`_identity_verify` 返回 ok）——免登录服务器没有别的
        身份来源，token 本身就是登录态。

    没有 token、也没有发过游客 token -> False：没有任何东西证明"还是同一个人"，
    断线即离开（与改动前逐字一致）。
    """
    candidates = []

    for value in (
        getattr(member, "guest_token", "") or "",
        getattr(member, "token", "") or "",
    ):
        value = str(value).strip()

        if value and value != "-" and value not in candidates:
            candidates.append(value)

    if not candidates:
        return False

    for candidate in candidates:
        # 游客 token：前缀是**中继自己发的**那一套。它只可能是游客会话，
        # 绝不能拿去当账号 token 校验 —— 会话被 LOGOUT 之后，账号校验在
        # "免登录服务器"上会把它当成合法 token，位子就永远不放（那是错的：
        # 用户明确说"token 失效 = 房间身份结束 = 要释放主机位"）。
        is_guest = bool(_identity_guest_prefix) and candidate.startswith(
            _identity_guest_prefix
        )

        if is_guest:
            if _identity_guest_name is not None:
                try:
                    if _identity_guest_name(candidate):
                        return True
                except Exception:
                    pass

            continue

        if _identity_verify is not None:
            try:
                ok, _identity = await _identity_verify(candidate)

                if ok:
                    return True
            except Exception:
                pass

    return False


async def hold_member_seat(room, member, grace=None, reason="member connection lost"):
    """
    「连接断了，但房间身份还在」：把座位（含主机位/所有权）原地留住，等他回来。

    返回 `True` = 同一个身份在宽限期内重连了，座位**已经交给新连接**，调用方
    必须立刻收工：**不得**释放主机位、**不得**移除成员、**不得**销毁房间
    （用户 2026-09-29：「连接和身份是两回事」）。
    返回 `False` = 宽限期满还没回来 -> 调用方按原来那条唯一清理路径处理
    （reason=timeout，释放主机位、必要时销毁房间）。

    这段时间里中继只做一件事：**断游戏连接**（可识别原因），并把开服控制
    连接（同一个人的另一条 socket）一起拆掉 —— 一个成员身份已经掉线的
    "Host" 不能再替别人服务。主机位 / `host=` / `host_name=` / 成员表**一个
    都不动**，所以别人的「认领 Host」仍然是不可用的（他还在房间里）。

    绝不用 `conn=idle`（连接状态）推导"没有 Host"（身份/职位）：这是两套状态。
    """
    grace = MEMBER_SEAT_GRACE if grace is None else max(0.0, float(grace))
    key = getattr(member, "member_key", None) or member.client_id

    if room.members.get(key) is not member:
        # 已经被新连接顶替（极窄竞态）：什么都不用做。
        return True

    member.detached = True
    member.detached_at = time.monotonic()

    # ============================================================================
    #  ★ v3.9.18：进入座位宽限期 -> 把"写失败"的旧账清零
    # ============================================================================
    #
    # `send_failures` 只在写**成功**时清零，而进入 detached 之后他本来就不再
    # 被写（`send()` 对 detached 直接返回 False，根本不计数）—— 于是"掉线前
    # 抖了三下"累积的那 3 次会被**永久**带着。
    #
    # 后果（复核报告 3.1，真实复现）：房主掉线前累积 3 次写超时 -> 掉线 ->
    # 进入 detached -> 看门狗的收尸分支（当时排在 detached 检查之前）看到
    # `send_failures >= 3`，把**身份仍然有效**的房主收走 —— 主机位凭空消失、
    # 房间被销毁。
    #
    # 清零的理由很简单：进到这里意味着"连接已经断了"，而失败计数描述的是
    # "**这条连接**写不通"。连接都没了，那笔账也就没有意义了。
    member.send_failures = 0

    _log(
        "[MEMBER DETACHED]",
        f"room={room.name}",
        f"user={member.label}",
        f"client_id={member.client_id or '-'}",
        f"grace={int(grace)}s",
        f"is_host={bool(member.is_host)}",
        f"is_owner={bool(member.is_owner)}",
        "reason=connection lost, identity/token still valid -> seat kept",
    )

    safe_event(
        "room.member.detached",
        room=lambda: room.name,
        user=lambda: member.label,
        client_id=lambda: member.client_id,
        is_host=lambda: bool(member.is_host),
        grace=lambda: int(grace),
    )

    # ---- 只断"连接"，不动"身份/职位" ----
    legacy = await room.release_legacy_host_for(member)

    # ============================================================================
    #  ★ 用户 2026-09-30 第 2 条：**一个人离开不许影响其他已连接的人**
    # ============================================================================
    #
    # 旧判据只看"他名下还有没有老式 `HOST <room>` 开服控制连接"：有就把
    # **全房**的游戏连接按 HOST_LEFT 断掉。可是主机位是**会换人**的 ——
    # 换过之后，前任名下那条开服连接即便还挂着（僵尸），他也已经不是当前
    # 主机；这时把全房的游戏断掉，就等于"一个玩家离开，其他人全部掉线、
    # 重新打洞"（用户实测：lubenwey 一重启，root 的隧道被 HOST_LEFT 拆掉、
    # muran 的游戏当场掉线，两个人被迫重连）。
    #
    # 现在的判据是**权威事实**：只有"他**就是**当前主机位持有者"
    # （`room.host_member is member`），或者"这个房间压根没有成员级主机位、
    # 游戏就是靠他这条老式开服连接在跑"（`host_member is None`）时，
    # 才走"全房 HOST_LEFT"那条路。其余情况只断**他自己**的会话。
    is_current_host = getattr(room, "host_member", None) is member
    nobody_serving = getattr(room, "host_member", None) is None

    if legacy is not None and (is_current_host or nobody_serving):
        # 他正在开服（或没有别人在开服）：所有游戏连接走 HOST_LEFT 那条可识别路径。
        _log(
            "[HOST SEAT LEFT]",
            f"room={room.name}",
            f"user={member.label}",
            f"is_host={bool(getattr(member, 'is_host', False))}",
            "-> 断开全房游戏连接（HOST_LEFT）",
        )

        with contextlib.suppress(Exception):
            await disconnect_game_connections(room)
    else:
        if legacy is not None:
            _log(
                "[STALE HOST CONNECTION]",
                f"room={room.name}",
                f"user={member.label}",
                "他名下还有一条老式开服连接，但主机位已经换人 "
                "-> 只断他自己的会话，绝不动别人",
            )

        with contextlib.suppress(Exception):
            await drop_member_sessions(
                room, member, reason="member connection lost"
            )

    with contextlib.suppress(Exception):
        await broadcast_member_state(room)

    # ============================================================================
    #  ★ 用户 2026-09-30 第 2 条：**房主不留单独的宽限期**
    # ============================================================================
    #
    # 房间生命强绑房主。房主掉线时：
    #   * 座位（身份 + 所有权）**原地留住**（不搬给别人、也不显示成 `-`）；
    #   * **不**起"座位宽限期"这条独立的计时器；谁来决定他什么时候算"离开"，
    #     交给**成员心跳看门狗**（`reap_idle_members`：`MEMBER_IDLE_TIMEOUT`
    #     没有心跳就清退）——这正是用户说的"等房主无心跳包 / token 过期"；
    #   * 看门狗一收走他，清理路径立刻识别 `is_owner` -> **房间当场销毁**
    #     （不存在"房主走了房间还活着、房主显示成 `-`"的窗口）。
    #
    # 闪断（在看门狗收走之前回来）：`add_member` 的"本人重连"路径会把座位
    # （含所有权）交回新连接，房间保留 —— 窗口就是心跳/token 周期本身，
    # 不是我们另加的 45 秒。
    #
    # 非房主保持既有行为（座位宽限期）。
    # ★ 用户 2026-09-30 第 2 条：**房主的等待窗口就是心跳周期本身**，不是我们
    #   另加的那条 45 秒宽限期。语义上仍然"房间生命强绑房主"：
    #     * 心跳周期内他回来 -> 座位（含所有权）交回新连接，房间保留；
    #     * 心跳周期过完还没回来 -> 这里返回 False，调用方继续走**唯一**那条
    #       清理路径 -> `remove_member` 认出 `is_owner` -> **房间当场销毁**。
    #   所以不会出现"房主走了房间还活着、房主显示成 `-`"的窗口。
    if getattr(member, "is_owner", False):
        _log(
            "[OWNER DETACHED]",
            f"room={room.name}",
            f"user={member.label}",
            f"房主掉线：座位原地留住，窗口=心跳周期 {int(MEMBER_IDLE_TIMEOUT)}s"
            "（不另设宽限期）——超时即销毁房间",
        )

        grace = float(MEMBER_IDLE_TIMEOUT)

    # ---- 等本人回来（或窗口到点）----
    deadline = time.monotonic() + grace

    while time.monotonic() < deadline:
        if room.members.get(key) is not member:
            # 座位已经交给新连接 —— 本地对象是历史了，收工。
            _log(
                "[MEMBER RECONNECT]",
                f"room={room.name}",
                f"user={member.label}",
                "seat_handed_over=1",
            )
            return True

        if getattr(room, "closed", False):
            break

        # ====================================================================
        #  ★ 宽限期内被**显式移除**（踢人 / 管理员操作）：宽限立刻作废
        # ====================================================================
        #
        # 用户 2026-09-29 实测：一个成员 18:12:23 被管理员踢掉，中继却还在
        # 45 秒宽限里替他留座、继续把 DIRECT_HIT / DIRECT_CAND / DIRECT_PUNCH
        # 转发给他（"被踢了还在打洞"），直到 18:12:55 座位超时才真的离房。
        #
        # "被踢"属于**房间身份结束**（设计基准 §C：被踢按退出处理），不是
        # "连接抖了一下" —— 宽限是给闪断用的，绝不能把踢人也吞掉。
        #
        # `removal_reason` 由 kick_member() 置成 "kick"；`closed` 由它关闭
        # socket 时一起置真。两个判据任一成立就立刻收工，把真正的移除交给
        # 调用方（handle_member_join 的 finally）。
        if getattr(member, "closed", False):
            break

        if str(getattr(member, "removal_reason", "") or "") not in ("", "drop"):
            break

        await asyncio.sleep(0.25)

    member.detached = False

    if room.members.get(key) is not member:
        return True

    reason = str(getattr(member, "removal_reason", "") or "")

    if reason not in ("", "drop"):
        # 宽限期内被显式移除（踢）：**不是**超时，别把日志写歪。
        _log(
            "[MEMBER SEAT REVOKED]",
            f"room={room.name}",
            f"user={member.label}",
            f"client_id={member.client_id or '-'}",
            f"reason={reason}",
            "removed while the seat was being held -> grace cancelled",
        )

        return False

    _log(
        "[MEMBER SEAT EXPIRED]",
        f"room={room.name}",
        f"user={member.label}",
        f"client_id={member.client_id or '-'}",
        f"grace={int(grace)}s",
        "identity did not come back -> treated as leaving the room",
    )

    return False


def _direct_peer(room, member, text=""):
    """
    Who a direct-connect line from `member` should be forwarded to.

    ============================================================================
      3.0.3: 行上的 `to=<client_id>` 优先（第三人永远打不上洞的根因）
    ============================================================================

    The old routing below is a star "by accident": a non-host always talks to
    the host, but the HOST talks to the earliest joiner -- so with three people
    in the room the host's candidates always reached player 1 and player 2
    never received one, meaning the punch could never sync for player 2. That
    is exactly the owner's complaint: "后面进来的直接冷落在一边".

    His server addresses every message (`targetId` in `start_punch` /
    `peer_signal`). Ours is a line forwarder, so the address rides the line as
    an additive `to=` token. The relay still parses nothing else about the
    candidates -- it only reads this ONE routing token.

    An addressed line whose target is gone is DROPPED (the sender retries),
    never re-routed to the earliest joiner: delivering the host's line for
    player 2 to player 1 would be worse than not delivering it.

    Old clients never send `to=`, and the fallback below is exactly their
    behaviour, so nothing changes for them.
    """
    wanted = ""

    if text:
        for token in text.split()[2:]:
            key, _, value = token.partition("=")

            if key == "to" and value:
                wanted = value
                break

    if wanted:
        for other in room.members.values():
            if other is member:
                continue

            if (other.client_id == wanted
                    or getattr(other, "member_key", "") == wanted):
                return other

        # Addressed to someone who is no longer here. Dropping is the honest
        # answer; the client re-announces every few seconds anyway.
        return None

    others = [m for m in room.members.values() if m is not member]

    if not others:
        return None

    # 权威判定：只看房间【此刻】的 host_member，不看 member.is_host ——
    # 旧连接 / 被顶替的对象上那个标记可能是过期值。一旦按过期值当成"我是
    # host"，下面就会退回"最早进房的人"，于是 player 把候选打给了 owner
    # （用户 P0 #1「打向房主而不是 host」）。
    if room.host_member is not member:
        current = room.host_member

        if current is not None and current is not member:
            return current

    # The host (or a room with no host yet): the earliest joiner that is not us.
    return min(others, key=lambda m: m.joined_at)


# ============================================================
# 每成员到中继的 RTT —— 中继**主动**探测（ADDITIVE）
# ============================================================
#
# 用户的规则：房间里每个人都应该看得到**别人到中继的延迟**。客户端自己能
# 量"我到中继"的延迟（它发 PING、中继回 PONG），但它量不到别人。所以中继
# 必须自己量每一个成员，再广播出去。
#
# 口径（协调者冻结，客户端已按这个实现）：
#   * 中继发 `PING <token>`；成员把 token 原样回成 `PONG <token>`。
#     **不新增握手步骤**：这就是同一条控制连接上的一次回声，和客户端自己
#     的 PING/PONG 完全对称，老中继从不发这条行。
#   * 中继量"发出这一行"到"读完回声那一行"的耗时 = 这一成员到中继的 RTT。
#     时间戳取在 write() 紧前 / readline() 返回后紧接，且每个成员同一时刻
#     只有**一条**在途探测（串行），所以排队、GC、批量写入都不会被算进去。
#   * 没回 / 超时的探测**不写数字**：成员保留上一次真正测到的值；一次都没
#     测到就保持 None，广播的 `rtts=` 里这个成员整条不出现。
#
# 这组常量是探测节奏。3 秒一次足够"实时"，也不给 2 核小机器添负担。

RTT_PROBE_FIRST_DELAY = 0.3
RTT_PROBE_INTERVAL = 3.0
RTT_PROBE_TIMEOUT = 4.0

#: 广播抑制：RTT 抖动小于这个值就不推，避免每 3 秒刷一次全房界面。
RTT_PUSH_MIN_DELTA_MS = 3.0
#: 但再稳也要定期推一次，否则新进房的人要等到数字变化才看得到别人的值。
RTT_PUSH_HEARTBEAT_S = 30.0

#: 探测 token 前缀。故意不是纯数字：客户端把**数字** token 的 PONG 当成它
#: 自己 PING 的回声用来算 relay_rtt_ms（见客户端 room.py），前缀让两类 PONG
#: 永远分得开，各量各的，互不污染。
RTT_TOKEN_PREFIX = "mclink-rtt-"


def _handle_rtt_pong(member, text, now):
    """
    收到一条 PONG：如果它对应在途的 RTT 探测，就把这一趟往返记下来。

    `now` 由调用方（读循环）在解码之后、任何 await 之前取好传进来 ——
    这样接收侧排队不计入测量。
    """
    wait = getattr(member, "_rtt_wait", None)

    if wait is None:
        # 成员自己在回中继的 PONG（它发 PING、我们回 PONG 之后它又发别的），
        # 不对应任何探测：忽略，不是错误。
        return

    token, sent, event = wait

    if text[4:].strip() != token:
        return

    rtt = max(0.0, (now - sent) * 1000.0)
    member.rtt_ms = rtt
    member._rtt_wait = None

    with contextlib.suppress(Exception):
        event.set()


async def _maybe_push_rtt(room, member):
    """把刚测到的 RTT 推给全房（抖动小的时候少推）。"""
    value = getattr(member, "rtt_ms", None)

    if value is None:
        return

    now = time.monotonic()
    previous = getattr(member, "_rtt_pushed", None)
    last_at = getattr(member, "_rtt_pushed_at", 0.0)

    if (
        previous is None
        or abs(value - previous) >= RTT_PUSH_MIN_DELTA_MS
        or now - last_at >= RTT_PUSH_HEARTBEAT_S
    ):
        member._rtt_pushed = value
        member._rtt_pushed_at = now
        await broadcast_member_state(room)


async def member_rtt_loop(room, member):
    """
    周期性地量这一个成员到中继的 RTT，并（值变了才）广播给全房。

    随成员的控制连接一起生灭：member_control_loop 负责 create/cancel。
    发送失败（连接没了）直接退出，绝不抛出到调用方。
    """
    await asyncio.sleep(RTT_PROBE_FIRST_DELAY)

    while not member.closed:
        member._rtt_seq = getattr(member, "_rtt_seq", 0) + 1
        token = f"{RTT_TOKEN_PREFIX}{member._rtt_seq}"
        event = asyncio.Event()

        sent = await member.probe_rtt(token, event)

        if sent is None:
            return

        answered = False

        try:
            await asyncio.wait_for(event.wait(), timeout=RTT_PROBE_TIMEOUT)
            answered = True
        except asyncio.TimeoutError:
            # 没回 / 超时：不写数字（保留上一次真正测到的值）。
            pass
        finally:
            wait = getattr(member, "_rtt_wait", None)

            if wait is not None and wait[0] == token:
                member._rtt_wait = None

        if answered:
            await _maybe_push_rtt(room, member)

        await asyncio.sleep(RTT_PROBE_INTERVAL)


async def member_control_loop(room, member):
    """
    Keep a room member's control connection alive and answer its requests.

    这一层只做一件事：把**中继主动的 RTT 探测**和成员自己的控制循环绑在
    同一条命上 —— 连接建立时启动探测，连接（无论怎么结束）时取消它。
    真正的读循环在 `_member_control_body` 里，一行没动。
    """
    rtt_task = asyncio.create_task(member_rtt_loop(room, member))

    try:
        return await _member_control_body(room, member)

    finally:
        rtt_task.cancel()

        try:
            await rtt_task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - 探测坏了不能影响成员
            _log(
                "[RTT PROBE ERROR]",
                f"room={room.name}",
                f"user={member.label}",
                f"error={e}",
            )


async def handle_member_identity(room, member, text):
    """
    `IDENTITY <username> <token> ver=<版本>` —— 人已经在房间里，现在登录了
    （或者改了名），把房间里的显示名换成登录用户名。

    回复：
        OK IDENTITY <name>
        ERROR IDENTITY_BAD_REQUEST / IDENTITY_NOT_IN_ROOM /
              IDENTITY_INVALID_TOKEN <auth_code> / IDENTITY_BAD_USERNAME
        ERROR VERSION_MISMATCH server=<> client=<>（版本门）

    ============================================================================
      ADDITIVE，而且只改"名字"这一件事
    ============================================================================

    老客户端不发这一条；老中继不认识它（成员控制循环对未知行直接忽略）——
    两个方向都不受影响。中继这边只动 `member.username`，然后立刻广播一次
    `HOST_STATE`（`names=` 里那颗名字跟着变）：房间、主机位、Minecraft 会话
    一个都不碰。这就是用户要的"身份绑设备，只更新信息，不打断游戏"。

    `ver=` 和五条握手行同一规则：挂在**整行最后一个 token**，按位置解析前先
    摘掉。版本门同样覆盖它 —— 不一致时按既有规则拒绝（回
    `ERROR VERSION_MISMATCH`，版本号两边都摊开）。
    """
    tokens = text.split()

    # ---- 先把 `ver=` 摘掉（它不许污染按位置解析的字段）----
    ver = ""
    positional = []

    for token in tokens:
        if token.startswith("ver="):
            if not ver:
                ver = token[4:]
            continue

        positional.append(token)

    # ---- 版本门（和握手同一个门、同一套日志/放行开关）----
    if RELAY_VERSION and ver != RELAY_VERSION:
        bypass = bool(_version_bypass()) if _version_bypass else False

        if bypass:
            _log(
                "[VERSION BYPASS]",
                f"room={room.name}",
                "role=IDENTITY",
                f"client={ver or '-'}",
                f"server={RELAY_VERSION}",
                "开发开关=开，已放行（可能产生未知 BUG）",
            )
        else:
            _log(
                "[VERSION REJECT]",
                f"room={room.name}",
                "role=IDENTITY",
                f"client={ver or '-'}",
                f"server={RELAY_VERSION}",
            )
            await member.send(
                f"ERROR VERSION_MISMATCH server={RELAY_VERSION} "
                f"client={ver or '-'}\n"
            )
            return

    # ---- 位置参数：IDENTITY <username> [token] ----
    if len(positional) < 2:
        await member.send("ERROR IDENTITY_BAD_REQUEST\n")
        return

    wanted = positional[1]
    token = positional[2] if len(positional) > 2 else "-"

    # ---- 必须还是这个房间的成员（旧连接可能已被同账号重连顶替）----
    key = getattr(member, "member_key", None) or member.client_id

    if room.members.get(key) is not member:
        await member.send("ERROR IDENTITY_NOT_IN_ROOM\n")
        return

    # ---- token：有账号体系时以它为准（登录名是权威，请求里的名字只是提示）----
    resolved = ""

    if _identity_verify is not None:
        ok, identity = await _identity_verify(token)

        if not ok:
            code = identity if isinstance(identity, str) else "AUTH_INVALID_TOKEN"

            _log(
                "[IDENTITY DENIED]",
                f"room={room.name}",
                f"user={member.label}",
                f"code={code}",
            )

            await member.send(f"ERROR IDENTITY_INVALID_TOKEN {code}\n")
            return

        if isinstance(identity, dict):
            resolved = str(identity.get("username") or "").strip()

    # 没有账号体系（或者 token 只是占位符）时用请求里带的登录名 —— 免登录
    # 房间下"游客 → 自己起的名字"走的就是这条路。
    name = resolved or str(wanted or "").strip()

    # 显示名不许带空格 / 控制字符（这一行是按空格分词的），长度按协议上限。
    name = "".join(c for c in name if ord(c) >= 33 and ord(c) != 127)

    if not name or name == "-" or len(name) > MAX_USERNAME_LEN:
        await member.send("ERROR IDENTITY_BAD_USERNAME\n")
        return

    # ---- 只改显示名，其它状态一个都不动 ----
    member.username = name
    room.mark_dirty()

    # 换账号登录 = 游客会话结束：把他身上那条临时 token 销毁（房间进出不会
    # 走到这里，只有真的用有效账号 token 改了名才算登录态切换）。
    if resolved and _identity_release_guest is not None:
        with contextlib.suppress(Exception):
            _identity_release_guest(getattr(member, "guest_token", "") or "")

    if resolved and auto_relay.supports(member):
        # The same room member can sign in without replacing game sockets.
        # Subsequent AUTO attachments use the newly verified credential.
        member.token = token
        member.guest_token = ""

    _log(
        "[IDENTITY]",
        f"room={room.name}",
        f"user={name}",
        f"client={member.client_id or '-'}",
    )
    safe_event(
        "room.identity.update",
        room=lambda: room.name,
        user=lambda: name,
        client_id=lambda: member.client_id,
        token_used=lambda: bool(token and token != "-"),
    )

    await member.send(f"OK IDENTITY {name}\n")

    # 立刻广播新的 HOST_STATE（含 names=）：房间里所有人的成员列表当场刷新。
    await broadcast_member_state(room)


async def _member_control_body(room, member):
    """
    Keep a room member's control connection alive and answer its requests.

        PING <anything>   -> PONG <anything>
        CLAIM_HOST        -> OK CLAIM_HOST / ERROR HOST_TAKEN <原因>
        RELEASE_HOST      -> OK RELEASE_HOST
        MAXPLAYERS <n>    -> OK MAXPLAYERS n=<最终值> / ERROR ...
        CONNMODE <mode>   -> OK CONNMODE mode=... / ERROR NOT_HOST（房主专属）
        STARTED <1|0>     -> OK STARTED started=<1|0>（ADDITIVE：点过开始连接）
        KICK <room> <id>  -> OK KICK <id> [OWNER_LEFT_ROOM] / ERROR KICK_*
        IDENTITY <name> <token> [ver=<版本>]
                          -> OK IDENTITY <name> / ERROR IDENTITY_*
        LEAVE             -> OK LEAVE, then the relay closes the connection

    Unknown lines are ignored rather than rejected, so a newer client can add
    requests without an older relay breaking, and vice versa.
    """
    reader = member.reader

    while True:
        line = await reader.readline()

        if not line:
            raise ConnectionError("成员控制连接已关闭")

        text = line.decode("utf-8", errors="replace").strip()

        if not text:
            continue

        now = time.monotonic()
        member.last_seen = now

        upper = text.upper()

        if upper.startswith("PING"):
            await member.send(f"PONG {text[4:].strip()}\n")
            continue

        # ============================================================
        #  PONG —— 中继 RTT 探测的回声（ADDITIVE）
        # ============================================================
        #
        # 中继每 RTT_PROBE_INTERVAL 秒发一条 `PING <token>`，成员把 token
        # 原样回成 `PONG <token>`。成员自己的心跳也发 PING（中继回 PONG），
        # 那条回包走的是下面的分支：它不对应任何在途探测，直接忽略。
        #
        # 时间戳 `now` 取在 decode 之后、任何 await 之前（就在上面），所以
        # 接收侧排队不计入 RTT。
        if upper.startswith("PONG"):
            _handle_rtt_pong(member, text, now)
            continue

        # ============================================================
        #  IDENTITY —— 人在房间里登录 / 改名（ADDITIVE，只读控制消息）
        # ============================================================
        #
        # 老中继忽略未知行，所以老客户端不发、新客户端发了也不会有兼容问题。
        # 见 handle_member_identity 的说明。
        # ============================================================================
        #  STATE <idle|waiting|pairing|connected>  —— 客户端上报的连接状态（ADDITIVE）
        # ============================================================================
        #
        # ★ 用户 2026-10-01 第 3 条：成员列表的「连接状态」列改成"客户端读自己的
        #   本地连接状态 -> 主动信令上传 -> 中继定期/变化时广播"。
        #
        # **纯显示**：存下来之后只影响 HOST_STATE 的 `conn=`（和面板快照），
        # 一条门禁/配对/打洞逻辑都不读它。老客户端不发这一行 -> 行为与以前一致。
        #
        # 频率：客户端在变化时发一次 + 1Hz 心跳（和成员列表刷新同频），所以这里
        # 只在**值变化**时广播，避免每秒给全房推一遍。
        if upper.startswith("STATE"):
            parts = text.split()
            wanted = parts[1].lower() if len(parts) > 1 else ""

            if wanted not in CONN_STATES:
                # 不认识的值：忽略（不是错误 —— 以后可能有新值）。
                continue

            if getattr(member, "reported_conn", "") != wanted:
                member.reported_conn = wanted
                await broadcast_member_state(room)

            continue

        if upper.startswith("AUTO_STATE "):
            await auto_relay.receive_report(room, member, text)
            continue

        if upper.startswith("IDENTITY"):
            await handle_member_identity(room, member, text)
            continue

        if upper.startswith("MAXPLAYERS"):
            # 房主改本房间的人数。服务器设置是上限，超过就按上限执行，
            # 并如实回报最终值 —— 界面显示的数字必须等于中继真正执行的数字。
            parts = text.split()
            wanted = parts[1] if len(parts) > 1 else ""

            ok, message = room.set_room_capacity(member, wanted)

            if ok:
                await member.send(
                    f"OK MAXPLAYERS n={room.room_capacity()}\n"
                )
                await broadcast_member_state(room)
            else:
                await member.send(f"ERROR BAD_MAXPLAYERS {message}\n")

            continue

        if upper == "LEAVE":
            # 明确退出：清理路径据此把 [HOST RELEASE] 的原因记成 leave。
            member.removal_reason = "leave"
            await member.send("OK LEAVE\n")
            return

        if upper.startswith("CONNMODE"):
            # CONNMODE <relay|stun>
            #
            # The HOST decides the transport for the whole room. Other members
            # are told what it is and cannot change it -- the relay enforces
            # that here, so a modified client cannot split the room.
            wanted = text.split()[1].lower() if len(text.split()) > 1 else ""

            if wanted not in ("relay", "stun", "auto"):
                await member.send("ERROR BAD_MODE 模式必须是 relay、stun 或 auto\n")
                continue

            # ============================================================
            #  只有房间【所有者】能切模式（设计基准 §A，用户 2026-09-27 确认）
            # ============================================================
            #
            # 以前这里是 `is_host or is_owner`。基准写得很清楚：「只有 Owner
            # 能切模式；Host（非 Owner）无权」。Host 只是房间里一个任何人
            # 都能认领的**位置**，让非 Owner 的 Host 改房间参数等于把
            # "房间参数创建后冻结"这条老规则开了个后门。
            #
            # 注意判的是 `room.owner is member`（房间此刻的权威所有者），
            # 不是 member.is_owner：旧连接 / 被顶替的对象上那个标记可能是
            # 过期值（见 kick 权限判定里的同一说明）。
            #
            # 为什么建房者还没认领主机位也要允许：真实客户端在建房后立刻
            # 就把模式 seed 上来，那一刻创建者是 owner=1 host=0。如果只认
            # host，房主自己的 seed 会被 ERROR NOT_HOST 拒掉，房间就锁死在
            # relay —— 这才是"建了房只能开中转"的根因。
            if room.owner is not member:
                safe_event(
                    "room.conn_mode.denied",
                    room=lambda: room.name,
                    by=lambda: member.label,
                    client_id=lambda: member.client_id,
                    wanted=lambda: wanted,
                    code=lambda: "NOT_HOST",
                    is_host=lambda: room.host_member is member,
                    is_owner=lambda: room.owner is member,
                )
                await member.send(
                    "ERROR NOT_HOST 连接模式由房间所有者决定，其他成员不能修改\n"
                )
                continue

            if wanted == "auto":
                incompatible = [m.client_id for m in room.members.values() if not auto_relay.supports(m)]
                if incompatible:
                    await member.send("ERROR AUTO_UNSUPPORTED members=" + ",".join(incompatible) + "\n")
                    continue

            if room_conn_mode(room, default="") != wanted:
                # ========================================================
                #  唯一的写入点（见 set_room_conn_mode 的说明）
                # ========================================================
                #
                # 先算"切换前房间里到底有没有连接"（PROTOCOL §8.5 的 conn=：
                # connected / pairing 都算有），再写模式、再清理。这个事实要
                # 跟着切换瞬间的广播一起发出去 —— 客户端据此决定"原本空闲
                # 就不自动重连 / 原本有连接就自动重连"（设计基准 §C）。
                #
                # 顺序不能反：清理之后再看就永远是"没有连接"。
                before = member_conn_states(room)
                was_live = 1 if any(
                    state in (CONN_CONNECTED, CONN_PAIRING)
                    for state in before.values()
                ) else 0

                set_room_conn_mode(room, wanted, member=member, cause="owner")

                # 一次性事实：由紧随其后的 `broadcast_member_state` 带上
                # `was_live=` 发出去，发完自动清掉（不会污染之后的广播）。
                room.conn_mode_was_live = was_live

                _log(
                    "[CONN MODE]",
                    f"room={room.name}",
                    f"by={member.label}",
                    f"mode={wanted}",
                    f"was_live={was_live}",
                )
                safe_event(
                    "room.conn_mode",
                    room=lambda: room.name,
                    by=lambda: member.label,
                    client_id=lambda: member.client_id,
                    mode=lambda: wanted,
                    is_host=lambda: room.host_member is member,
                    is_owner=lambda: room.owner is member,
                    was_live=lambda: was_live,
                )

                # ========================================================
                #  换模式 = 严格重建全部通路（设计基准 §D/§E）
                # ========================================================
                #
                # 旧模式下的配对 / 隧道在模式变了以后就是脏会话（"已配对但
                # 模式已变"）：relay 的配对和 stun 的直连用的是两套客户端
                # 状态机，留着它们只会让两边状态不一致。所以在切换的这一瞬
                # 间把它们全部作废，并给一个可识别的原因码 —— 客户端据此按
                # 新模式重建，而不是把它当网络抖动。
                #
                # `extra` 把 `was_live=` 一起挂到那条原因行上：等待池里的
                # PLAYER / HOST_TUNNEL 与每个成员的控制连接都收得到。
                dropped = await disconnect_game_connections(
                    room,
                    reason="连接模式已切换，请按新模式重新开始连接",
                    code=CONN_MODE_CHANGED_CODE,
                    extra=f" was_live={was_live}",
                )

                _log(
                    "[CONN MODE REBUILD]",
                    f"room={room.name}",
                    f"mode={wanted}",
                    f"stale_connections={dropped}",
                )

            await member.send(f"OK CONNMODE mode={room_conn_mode(room)}\n")

            # 广播必须在清理之后：`started=` / `world=` 要反映"旧通路已经
            # 全没了"，客户端才不会再对旧目标打洞。
            await broadcast_member_state(room)

            continue

        # ============================================================
        #  STARTED —— 客户端显式上报「我点了开始连接 / 我停了」（ADDITIVE）
        # ============================================================
        #
        # 精确协议（PROTOCOL §8.6）：
        #
        #     STARTED <1|0>\n
        #         ◄── OK STARTED started=<1|0>\n
        #
        # 用户口径（2026-09-28）：「已开始连接」= **用户在客户端点过「开始
        # 连接」**，而不是"已经建立了 PLAYER / HOST 连接"。老客户端永远不发
        # 这一行 -> 中继继续用连接现算（行为一个字不变）；新客户端发了之后，
        # 这个标记和连接现算**取并集**进 `started=`，于是 STUN 模式下"已经点
        # 了开始、正在打洞、还没开游戏"的玩家也会出现在 Host 的目标表里。
        #
        # 这条上报**不会**放宽 §B 的门禁：能不能连由 `host_claimed` /
        # `host_alive`（host_writer）决定，和 started= 无关；started= 只决定
        # "Host 往谁那里打"。
        if upper.startswith("STARTED"):
            parts = text.split()
            value = parts[1].strip().lower() if len(parts) > 1 else ""

            if value not in ("0", "1"):
                await member.send(
                    "ERROR BAD_STARTED 必须是 STARTED 0 或 STARTED 1\n"
                )
                continue

            wanted_connecting = value == "1"
            changed = member.connecting != wanted_connecting

            if changed:
                member.connecting = wanted_connecting

                _log(
                    "[CONNECTING]",
                    f"room={room.name}",
                    f"user={member.label}",
                    f"client_id={member.client_id or '-'}",
                    f"started={value}",
                )

            await member.send(f"OK STARTED started={value}\n")

            # 立刻广播：Host 的目标列表（`started=`）必须当场跟着变，不能
            # 等到下一次心跳/认领。真的变了才广播（客户端每秒重报同一个值
            # 时不要变成广播风暴）。
            if changed:
                await broadcast_member_state(room)

            continue

        if upper == "CLAIM_HOST":
            ok, message = room.claim_host(member)

            if ok:
                _log("[HOST CLAIM]", f"room={room.name}", f"user={member.label}")
                safe_event(
                    "room.host.claim",
                    room=lambda: room.name,
                    room_name=lambda: room.display_name,
                    user=lambda: member.label,
                    client_id=lambda: member.client_id,
                    previous=lambda: getattr(
                        getattr(room, "host_member", None), "label", ""
                    ),
                )
                await member.send("OK CLAIM_HOST\n")
                await broadcast_member_state(room)
            else:
                _log(
                    "[HOST CLAIM DENIED]",
                    f"room={room.name}",
                    f"user={member.label}",
                    f"reason={message}",
                )
                await member.send(f"ERROR HOST_TAKEN {message}\n")

            continue

        if upper == "RELEASE_HOST":
            released = room.release_host(member)

            _log(
                "[HOST RELEASE]",
                f"room={room.name}",
                f"user={member.label}",
                f"released={released}",
            )
            safe_event(
                "room.host.release",
                room=lambda: room.name,
                user=lambda: member.label,
                client_id=lambda: member.client_id,
                released=lambda: bool(released),
            )

            await member.send("OK RELEASE_HOST\n")

            if released:
                # ============================================================
                #  释放主机位 = 连他手上那条**开服控制连接**一起放掉
                # ============================================================
                #
                # 主机位和"谁在开服"是两件事，但同一个人身上它们必须一起走：
                # 只清 `member.is_host` 而不关他的 `HOST <room>`，中继就会
                # 一边广播 `host_id=<新人>`、一边继续用**旧人**的
                # `host_writer` 收 PLAYER / 发 START —— `host_id=` /
                # `started=` / `conn=` 三个字段看起来自洽，实际数据却进了
                # 一个已经卸任的人。
                #
                # 释放之后 `host_alive()` 为假 -> 新的 PLAYER 收到
                # `ERROR HOST_NOT_READY`，直到新 Host 自己开服（基准 §B）。
                legacy = await room.release_legacy_host_for(member)

                if legacy is not None:
                    _log(
                        "[HOST RELEASE]",
                        f"room={room.name}",
                        f"by={member.label}",
                        "reason=release",
                        "legacy_host_connection=closed",
                    )

                # ============================================================
                #  ★ 用户 2026-09-30 第 1 条：主动释放主机位也要给**可识别的原因**
                # ============================================================
                #
                # 没人能在没有主机的情况下继续玩，所以所有人的游戏连接都要断 ——
                # 但断法必须是"带原因的"，不能只是静默关 socket：
                #
                #   * `drop_all_sessions` 只关 socket，玩家侧看不出这是"房主停了"
                #     还是网络抖动 -> 会走普通掉线那条路（自动重连、继续打洞），
                #     界面停在"运行中"；
                #   * `disconnect_game_connections` 会给**每个成员的控制连接**发
                #     一行 `HOST_LEFT <原因>`（已进世界的玩家那条 socket 是纯字节，
                #     这一行是他们唯一的原因），玩家侧据此停会话、清状态与隧道、
                #     转「等待房主上线」，**不自动重连**。
                #
                # 房间本身**保留**（成员控制连接不动），主机位空着等下一次认领。
                await disconnect_game_connections(
                    room,
                    reason="host 已断开连接，host 位置暂无人认领",
                    code=HOST_RELEASED_CODE,
                )
                await broadcast_member_state(room)

            continue

        # ============================================================
        #  KICK —— 踢出房间里的一个成员（新增关键字，旧中继忽略）
        # ============================================================
        #
        # 房间 token 放在第一个参数，和客户端其它控制行的写法一致；第二个
        # 参数是要踢的人的 client_id。具体权限与拆除见 handle_member_kick。
        if upper.split()[0] == "KICK":
            await handle_member_kick(room, member, text)
            continue

        # ============================================================
        #  V3 direct path (STUN mode) -- ADDITIVE
        # ============================================================
        #
        # The relay's only job here is to carry one line to the OTHER member of
        # the room. It does not parse candidates, does not choose a plan and
        # does not touch the data path -- the same rule as the Minecraft bytes.
        #
        # Everything below is new keywords only: an older relay ignores them
        # (unknown lines fall through), and a newer relay answers an older
        # client exactly as before.
        # ====================================================================
        #  ★ DIRECT_ADDR：**自我介绍** -> 广播给房间里所有其他成员
        # ====================================================================
        #
        # 用户 2026-09-30：直连模式下，每个对端都要把自己的地址和端口广播给
        # 所有人，成员列表才看得到（以前只有 host 能看到玩家的候选）。
        #
        # 它和 DIRECT_CAND 是两件事：DIRECT_CAND 是打洞候选，必须按 `to=`
        # 精确投递（喂错人会污染打洞状态机）；DIRECT_ADDR 只是"我在这个
        # 公网端点"，收到的人只记进显示用的表。所以这里**只转发、不解析、
        # 不改路由**：原文逐个发给其他成员。
        if upper.startswith("DIRECT_ADDR"):
            keyword = "DIRECT_ADDR"
            recipients = 0

            for other in list(room.members.values()):
                if other is member:
                    continue

                with contextlib.suppress(Exception):
                    await other.send(text + "\n")
                    recipients += 1

            if not recipients:
                # 房间里只有自己：不是失败（客户端下次测量还会再报一遍）。
                await member.send(f"ERROR NO_PEER {keyword}\n")

            _log(
                "[DIRECT FWD]",
                f"room={room.name}",
                f"from={member.label}",
                f"keyword={keyword}",
                f"to=all({recipients})",
            )

            safe_event(
                "direct.forward",
                room=lambda: room.name,
                sender=lambda: member.label,
                receiver=lambda: "*",
                keyword=lambda: keyword,
                line=lambda: text,
            )

            continue

        if upper.startswith("DIRECT_CAND") or upper.startswith("DIRECT_PUNCH") \
                or upper.startswith("DIRECT_HIT"):
            keyword = upper.split()[0]

            # Remember which public IPs this member says are its own. Only used
            # to bound DIRECT_INGRESS (see there); the line itself is still
            # forwarded verbatim and never rewritten.
            parts = text.split()

            if keyword == "DIRECT_CAND" and len(parts) > 1:
                member.published_ips.add(parts[1])

            peer = _direct_peer(room, member, text)

            if peer is None:
                # Not a failure worth alarming about: the other side simply has
                # not joined yet. The client retries.
                await member.send(f"ERROR NO_PEER {keyword}\n")
                continue

            # Forward the line VERBATIM. The relay must not "helpfully" rewrite
            # it: the two clients agree on this format, and a middlebox that
            # edits it would break every future field addition.
            await peer.send(text + "\n")

            _log(
                "[DIRECT FWD]",
                f"room={room.name}",
                f"from={member.label}",
                f"to={peer.label}",
                f"keyword={keyword}",
            )

            # 转发的**原文**。这几条线上跑的是 NAT 候选和同步起跑时刻 ——
            # 打洞失败时要查的正是"它到底报了哪个端口、约在什么时候起跑"，
            # 而"forwarded DIRECT_CAND"这种摘要回答不了这个问题。
            safe_event(
                "direct.forward",
                room=lambda: room.name,
                sender=lambda: member.label,
                receiver=lambda: peer.label,
                keyword=lambda: keyword,
                line=lambda: text,
            )

            continue

        if upper.startswith("DIRECT_INGRESS"):
            # DIRECT_INGRESS <public_ip> <public_port>
            #
            # Ask our STUN responder to fire ONE datagram at that endpoint from
            # a socket that has never spoken to it. That is what the peer's
            # first punch packet looks like to the client's NAT, so the client
            # can tell "the peer's packets were dropped" apart from "I am
            # unreachable" -- two opposite problems with opposite responses.
            parts = text.split()
            host = parts[1] if len(parts) > 1 else ""
            port = 0

            with contextlib.suppress(ValueError):
                port = int(parts[2]) if len(parts) > 2 else 0

            if not host or not port:
                await member.send("ERROR BAD_INGRESS 参数不完整\n")
                continue

            # ============================================================
            #  只允许探测【请求者自己的】公网地址
            # ============================================================
            #
            # 他的 `handle_inbound_probe` 有这个检查，而且写明了为什么：
            # 没有它，这个 handler 就是一个**开放的 UDP 反射器** —— 任何人
            # 都可以让中继的 STUN 端口朝任何人发包。这不是能上线的东西。
            #
            # 客户端问的是"我朝这台服务器映射出去的那个端点通不通"（地址是
            # 从我们自己的 STUN 学来的），所以这个限制**零成本**：
            #   * 它连接过来的源 IP（TCP 看到的公网地址），或
            #   * 它自己用 DIRECT_CAND 公告过的 IP。
            # 两者取其一，是为了容忍"UDP 和 TCP 走了不同公网出口"的 NAT
            # （少见但真实存在），同时又堵住反射器。
            allowed = set(getattr(member, "published_ips", ()) or ())

            own_ip = _peer_ip(member.writer)

            if own_ip:
                allowed.add(own_ip)

            if allowed and host not in allowed:
                _log(
                    "[INGRESS REJECT]",
                    f"room={room.name}",
                    f"user={member.label}",
                    f"target={host}",
                    f"own={','.join(sorted(allowed))}",
                )
                await member.send("ERROR BAD_INGRESS 目标地址不是你的公网地址\n")
                continue

            sent = stun_server.probe_client((host, port), (member.label, 0))

            if sent:
                # Confirm that we SENT it. Without this the client cannot tell
                # "the probe did not arrive" from "this relay never fired one",
                # and the ported project spent rounds reporting the second as
                # the first.
                await member.send("OK DIRECT_INGRESS\n")
            else:
                await member.send("ERROR NO_STUN 中继的 STUN 服务未启用\n")

            continue


def clean_host_name(value):
    """
    把显示名压成能安全放进控制行一个 token 的名字；没有名字时是 `-`。

    username 是显示名，法律上可以带空格 / 控制字符；`host_name=` 是按空格分词
    解析的，混进去会把这一行拆坏。宁可少几个字也不能拆行。
    """
    text = "".join(
        c for c in str(value or "") if ord(c) >= 33 and ord(c) != 127
    )

    return text or "-"


def host_state_fields(room):
    """
    `(host_flag, host_name_token)` —— 广播与 OK 回包**共用**的同一份来源。

    两个字段一起从这里出，`host=1` 却 `host_name=-` 这种自相矛盾的一对就
    不可能再出现（客户端会把 `-` 解成空串，于是界面上成了「有房主 / 主机：」）。
    """
    claimed, name = room.host_state()

    return (1 if claimed else 0), clean_host_name(name)


def host_client_id(room):
    """
    当前 Host 的 **client_id**（没有 Host 时是空串）。

    为什么需要它：`host_name=` 是**显示名**，可能重名；客户端要在 `peers=`
    里挑出"哪个 peer 是当前 Host"才能把打洞目标定死（设计基准 §D：只认
    Host）。给一个 client_id 让这个匹配变成精确匹配，而不是靠名字猜。

    两种持有方式都覆盖：V3 成员认领（host_member.client_id）和老式
    `HOST <room>` 控制连接（host_id）。返回值永远能安全放进一个 token
    （client_id 是 ASCII），没有时是 `-`（见 clean_host_name 的同一口径）。
    """
    member = room.host_member

    if member is not None:
        return str(getattr(member, "client_id", "") or "").strip() or "-"

    if getattr(room, "host_writer", None) is not None:
        return str(getattr(room, "host_id", "") or "").strip() or "-"

    return "-"


def log_host_release(room, member, reason):
    """
    成员离开 / 掉线 / 被踢 / 超时导致主机位释放时，记**唯一一行**日志：

        [HOST RELEASE] room=<room> by=<name> reason=leave|drop|kick|timeout

    下一份"host 莫名丢了"的服务端日志靠这一行一眼定位。reason 只允许这四个
    值（其余一律按 drop 记），保证可以 grep。
    """
    reason = reason if reason in ("leave", "drop", "kick", "timeout") else "drop"

    _log(
        "[HOST RELEASE]",
        f"room={room.name}",
        f"by={member.label}",
        f"reason={reason}",
    )

    safe_event(
        "room.host.release",
        room=lambda: room.name,
        by=lambda: member.label,
        client_id=lambda: member.client_id,
        reason=lambda: reason,
    )


# ============================================================================
#  房间连接模式：唯一读写口径
# ============================================================================
#
# 用户 2026-09-29 的 P0：
#
#     「STUN(直连) → relay → 切回 STUN 打通后，直连玩一会儿游戏断开，并且
#       没有任何人操作，模式自己跳回了 relay，连接也断了。」
#
# 基准（设计基准 §A + PROTOCOL §8）：**连接模式只能由 Owner 的显式
# `CONNMODE` 请求改变**。任何内部路径（清理 / 重试 / 看门狗 / 兜底默认值）
# 都不许改写它。
#
# 所以写点只有一个（`set_room_conn_mode`），读点也只有一个（`room_conn_mode`，
# 绝不用 `getattr(room, "conn_mode", "relay")` 凭空造一个 "relay" 出来 ——
# 那正是"Owner 没切、广播里却写着 relay"的兜底来源）。
#
# `cause` 只有三个合法取值，落进 `mode_reason=` 广播给客户端：
#
#     owner    = Owner 显式 CONNMODE（唯一的"切模式"）
#     restored = 房间销毁后由**同一个 Owner** 重建，沿用他上一次的选择
#     default   = 全新房间的构造默认值（老客户端不发模式时也是这样）


#: `mode_reason=` 允许出现的全部取值。
MODE_CAUSES = ("owner", "restored", "default")


def room_conn_mode(room, default="-"):
    """
    房间此刻**真实**的连接模式；房间对象上没有这个字段时返回 `-`。

    绝不返回 "relay" 作为兜底：`mode=` 写着一个 Owner 从没要求过的值时，
    客户端会据此拆掉直连通路（"模式自己跳回 relay"就是这样发生的）。
    `-` 让客户端保持现状，而不是替它做一个它没做过的决定。
    """
    mode = getattr(room, "conn_mode", "")

    if mode in ("relay", "stun", "auto"):
        return mode

    if mode:
        return str(mode)

    return default


def room_mode_reason(room):
    """`mode=` 是怎么来的（`owner` / `restored` / `default`）。"""
    reason = str(getattr(room, "conn_mode_reason", "") or "")

    return reason if reason in MODE_CAUSES else "default"


def set_room_conn_mode(room, wanted, member=None, cause="default"):
    """
    **唯一**允许写 `room.conn_mode` 的地方（`Room.__init__` 的构造默认值除外）。

    返回 `True` 表示这次调用真的改了值（调用方据此记日志 / 广播）。权限判定
    不在这里 —— 唯一的线上调用点（CONNMODE 分支）已经先断言 `room.owner is
    member`；这里只负责"写 + 记来源 + 记住 Owner 的选择"。
    """
    wanted = str(wanted or "").strip().lower()

    if wanted not in ("relay", "stun", "auto"):
        return False

    if cause not in MODE_CAUSES:
        cause = "default"

    room.conn_mode = wanted
    room.conn_mode_reason = cause

    if member is not None:
        room.conn_mode_by = str(getattr(member, "client_id", "") or "")

    # Owner 的显式选择要跨"房间销毁 -> 重建"活下来（同一个 Owner 才认）。
    if cause == "owner" and member is not None:
        owner_key = str(getattr(member, "client_id", "") or "")
        remember_mode(room.name, wanted, owner_key)

    return True


#: ★ v3.9.18：**广播**这一路单独用的写超时（秒）。
#:
#: 比 `MEMBER_SEND_TIMEOUT`（5s）短。两个数字的含义不同：
#:
#:   * 5 秒是给"这个人的连接还好吗"这种**判死**用的 —— 宁可多等，别误判。
#:   * 广播是**通知** —— 同一条状态任何人晚一点收到都有下一轮补（任何状态
#:     变化都会再广播一次）。它最该避免的是"一个人慢，整条调用链陪着等"。
#:
#: 2 秒里一个健康的人早写完了一行几十字节；超过 2 秒没写完的，这一轮就先
#: 不等他（记一笔 `send_failures`，连续几次才判死，不会误杀）。
BROADCAST_SEND_TIMEOUT = float(
    os.environ.get("MCLINK_BROADCAST_SEND_TIMEOUT", "2") or 0
)

#: ★ v3.9.18：一次成员广播的整体预算（秒）。
#:
#: 和 `NOTIFY_BUDGET_S` 同一个量级：广播是"让每个人知道房间状态"这件事的
#: 全部渠道，**不能**因为一个人慢就把整批拖死，但也不该无限等。
#:
#: 设 0 = 不设预算（退回 v3.9.17 的串行为，仅用于对照）。
BROADCAST_BUDGET_S = float(
    os.environ.get("MCLINK_BROADCAST_BUDGET_S", "5") or 0
)


async def _send_lines_concurrently(pending, budget=None, timeout=None):
    """
    把 `(member, line)` 一批**并发**发出去，整体不超过 `budget` 秒。

    ============================================================================
      为什么要并发（复核报告 3.2）
    ============================================================================
    串行广播 + 5 秒写超时 = 一个慢成员拖住整个房间：多数调用点是裸 await，
    这 5 秒里那条控制循环什么都做不了。

    并发之后每个人最多拖到自己；整体再加一道预算，保证调用链**一定**会返回。

    超时的人不是"被放弃"—— `member.send()` 自己有 `send_timeout`，超时的会被
    记一笔 `send_failures`，连续几次才判死（见 `RoomMember.send`）。所以
    "这一轮没送到" 和 "这个人死了" 仍然是两件事。
    """
    if not pending:
        return 0

    async def _one(member, line):
        # 广播用**更短**的写超时（见 BROADCAST_SEND_TIMEOUT）：通知不该陪着
        # 慢的人一起等。临时换、发完还原，不改动这个成员自己的判死参数。
        saved = getattr(member, "send_timeout", None)

        if timeout and timeout > 0:
            member.send_timeout = timeout

        try:
            with contextlib.suppress(Exception):
                return await member.send(line)

            return False
        finally:
            member.send_timeout = saved

    tasks = [
        asyncio.ensure_future(_one(member, line))
        for member, line in pending
    ]

    try:
        if budget and budget > 0:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=budget,
            )
        else:
            await asyncio.gather(*tasks, return_exceptions=True)
    except (asyncio.TimeoutError, Exception):
        # 到点就走：没送完的那一小部分由各自的 send() 记账，
        # 下一轮广播（任何状态变化都会再触发一次）自然会补上。
        for task in tasks:
            if not task.done():
                task.cancel()

        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)

    sent = 0

    for task in tasks:
        # 被 cancel 的 task：`.result()` 会抛 CancelledError，
        # `.exception()` 也会 —— 三种"没送到"的形态都要能安全地问一遍。
        with contextlib.suppress(
            asyncio.CancelledError, asyncio.InvalidStateError, Exception
        ):
            if task.cancelled():
                continue

            if task.done() and task.exception() is None and task.result():
                sent += 1

    return sent


async def broadcast_member_state(room):
    """
    Tell every member who holds the host position and how many people are here.

    Pushed rather than polled, so a client can show "房间还没有房主，无法开始
    连接" the instant it becomes true instead of up to a heartbeat later.
    """
    # host_name= 是必须的，不是锦上添花：
    #
    # host=0/1 只说"有没有人当主机"，说不出"是谁"。于是某个人释放主机之后，
    # 别人客户端上那一行还挂着他上一次收到的名字 —— 看起来像"释放没生效"，
    # 也看不出自己现在能不能认领。名字跟着状态一起推，界面才是实时的。
    #
    # `host=` 与 `host_name=` 从**同一个** host_state_fields() 出（见那里的
    # 说明）：要么"有主机 + 名字"，要么"没主机 + `-`"，不允许第三种组合。
    host_flag, host_name = host_state_fields(room)

    # ============================================================================
    #  一次性事实：这次广播要不要带 `was_live=`（换模式切换瞬间用）
    # ============================================================================
    #
    # 设计基准 §C + 用户 2026-09-29 的要求：客户端要能判断"房间原本有没有
    # 连接"，据此决定「原本空闲 -> 不自动重连 / 原本有连接 -> 自动重连」。
    #
    # 换模式会**先清理后广播**，所以清理之后的 `conn=` 全是 idle —— 那个
    # 事实在广播里已经看不到了。CONNMODE 分支在清理前把它算出来，放在
    # `room.conn_mode_was_live` 上；这里**读一次就清掉**，于是只有切换瞬间
    # 的那一轮广播带 `was_live=`，之后的广播不会拿一个过期值骗人。
    was_live = getattr(room, "conn_mode_was_live", None)

    if was_live is not None:
        room.conn_mode_was_live = None
        was_live = "1" if was_live else "0"

    # 当前 Host 的 client_id（ADDITIVE，`-` = 没有 Host）。客户端把它和
    # `peers=` 精确对上，才知道"哪个 peer 是当前 Host"—— 只靠显示名可能重名。
    host_cid = host_client_id(room)

    # ============================================================================
    #  `owner_id=` —— 当前房主的 client_id（ADDITIVE）
    # ============================================================================
    #
    # ★ 用户 2026-09-30 第 3 条：客户端**早就在解析**这个 token
    #   （`RoomSession._on_line` 的 HOST_STATE 分支读 `owner_id=` / `owner_cid=`，
    #   `build_member_rows` 用它精确标出「房主」那一行），但中继从来没在
    #   HOST_STATE 里发过它 —— 于是除了房主自己，谁都看不到「房主」这个身份：
    #   成员列表里房主只显示成「主机 / 玩家」（用户报的"身份显示异常"）。
    #
    # 取值是**现算的权威值**（`room.owner` 是从成员对象上算出来的属性）；
    # 没有房主时是 `-`。老客户端不认这个 token，按空格分词直接忽略。
    try:
        _owner_member = room.owner
    except Exception:
        _owner_member = None

    owner_cid = str(getattr(_owner_member, "client_id", "") or "") or "-"

    # `peers=` was ADDED for the star topology's direct links.
    #
    # A host must punch to EVERY guest, and a punch needs the peer's identity
    # (both ends derive "who leads" from it, and the status card shows it). The
    # relay knew the roster all along but only ever broadcast the HOST's name,
    # so from the host's side the guests were anonymous -- which is why a third
    # person could sit at "正在打洞" forever while the host had nobody to punch
    # to and considered the job done.
    #
    # ONE TOKEN PER LINE, and client ids only: they are ASCII hex, they cannot
    # contain a space, and an older client that does not know the token simply
    # ignores it (the additive-protocol rule this project runs on). Usernames
    # are NOT used here: they are display names and may contain anything.

    # ---- 每成员显示名 / 每成员到中继的 RTT（ADDITIVE，协调者冻结的格式）----
    #
    #     names=<cid>:<name>,<cid>:<name>   name 是 percent-encoded
    #     rtts=<cid>:<ms>,<cid>:<ms>        ms 整数；没数据的成员整条不出现
    #
    # 两张表对每个成员都一样，所以在循环外算一次。
    #
    # `names=` 是"房间里显示的成员名必须是登录用户名"的落点：以前广播里只有
    # client_id，前端只能显示「未知」。免登录/游客连接的 username 是中继在
    # 握手时分配的「游客xxxxxx」，所以这张表里永远有名字，不会有空值。
    #
    # `rtts=` 是中继自己量的（见 member_rtt_loop）：None 的成员不进表 ——
    # 界面显示 `-`，绝不编一个数字。
    names_token = _member_names_token(room)
    rtts_token = _member_rtts_token(room)

    # `started=` —— 哪些成员**已经开始连接**（ADDITIVE，协调者/客户端按它选目标）
    #
    # 设计基准 §D/§E：Player 每轮只向**当前 Host** 打；Host 只向**已经开始
    # 连接的** Player 打。中继知道两件事：
    #   * 当前 Host = `host=` / `host_name=`（已有）；
    #   * 谁开始了 = 谁真的发起了 PLAYER / HOST_TUNNEL 连接。
    # 所以这个集合从**连接本身**现算（不引入新的客户端握手）：见
    # started_client_ids()。2026-09-28 起并上客户端**显式上报**的
    # `STARTED 1`（ADDITIVE）——"点了开始连接"本身也是一个来源。
    started = started_token(room)

    # `conn=` —— 每个成员**此刻的游戏连接状态**（ADDITIVE，2026-09-28）
    #
    # 用户原话：「这边玩家断开连接，那边 host 这个连接状态还是显示已连接，
    # 未及时更正状态，所以我觉得可以把连接状态也包进我们管理广播 TCP 里」。
    # 取值只有 4 个（idle|waiting|pairing|connected），全部从连接现算，并且
    # 每一次变化都会立刻再广播一次（见 watch_player_socket / remove_session /
    # handle_host），绝不等到下一次心跳。
    conn = conn_token(room)

    # ★ 花名册按 client_id 去重（见 _roster_members 的说明）：
    # `peers=` / `names=` 都是**列表**，同一个 client_id 两条记录会让客户端
    # 画出 4 行却报 5 人（用户 2026-09-29 第 5 条）。
    roster = _roster_members(room)

    # ============================================================================
    #  ★ v3.9.18：先把每人的那一行**全部拼好**，再并发发送
    # ============================================================================
    #
    # 以前是 `for member in roster: ... await member.send(line)` —— **串行**。
    # 而 v3.9.17 把成员写超时放宽到了 `MEMBER_SEND_TIMEOUT`=5 秒（为了不把弱网
    # 成员误判死，那个放宽是对的），两者叠在一起就变了味（复核报告 3.2，实测）：
    #
    #     全员正常            -> 广播耗时 0.00s
    #     1 个成员弱网(5s)    -> 广播耗时 5.00s     ← 整条调用链被他一个人拖住
    #
    # 后果有两层：
    #   * 大多数调用点是裸 `await broadcast_member_state(room)`：这 5 秒里那条
    #     控制循环 / handler 什么都做不了 —— 房间状态推进、成员加入、房主心跳
    #     全部延迟。8 人房间里有 3 个慢的就是 15 秒。
    #   * 少数包了预算的调用点（timeout=5 / NOTIFY_BUDGET_S=5）会在 5 秒整批
    #     截断 —— **慢成员之后的那批人这一轮广播直接收不到**（成员表不更新、
    #     `host=` 不更新）。
    #
    # 讽刺的是 `disconnect_game_connections()` 对 members 早就是 gather 并发 +
    # 预算了（v3.9.16 改的），只有这里还串行 —— 两处不一致。这里照搬那套。
    #
    # 顺序仍然是**确定的**（先拼完所有行，再一起发），所以"谁先收到"不影响
    # 内容；慢的人只会拖到自己，不会拖到别人。
    pending = []

    for member in roster:
        # NEW: 还没收到自己的 OK 回包的连接一条都不许推（见 RoomMember.ready）。
        # 竞态窗口里推过去的 `HOST_STATE` 会被客户端当成握手应答，直接毁掉
        # 这一次进房（用户 2026-09-29 的自动重连失败）。
        if not getattr(member, "ready", True):
            continue

        # The roster is built PER MEMBER because "peers" means "everyone except
        # you" -- the same line cannot be sent to all of them.
        #
        # 排除按 **client_id**（不是 `is not member`）：同一个 client_id 在
        # 房间里只该出现一次（花名册已经去过重），拿对象身份排除会把
        # "顶着我 cid 的另一个人"当成对端列出来。
        my_cid = str(getattr(member, "client_id", "") or "")

        peers = [
            other.client_id
            for other in roster
            if other.client_id and other.client_id != my_cid
        ]

        # ★ `members=` 必须**收信人能画出来**的那个数：客户端成员列表 / 逐目标行
        # 都以 client_id 为键，`peers=` 少一个人它就会"少一行、人数对不上"。
        # 正常房间（cid 互不相同）下 `len(roster)` 就是原来的 `member_count()`，
        # 一字不差；只有"同一个 cid 两条记录"那种畸形房间才会小于字典长度 ——
        # 而那正是用户报的「4 个人显示 5 人」（成员列表正常）。
        members_token = roster_count(room)

        line = (
            f"HOST_STATE host={host_flag} "
            f"host_name={host_name} "
            # ------------------------------------------------------------
            #  `host_id=` —— 当前 Host 的 client_id（ADDITIVE）
            # ------------------------------------------------------------
            #
            # 精确格式：`host_id=<cid>`；没有 Host 时是 `host_id=-`。
            # 客户端用它在 `peers=` 里精确挑出当前 Host，把打洞目标定死
            # （设计基准 §D：Player 只认 Host，与谁创建房间无关）。
            f"host_id={host_cid} "
            f"owner_id={owner_cid} "
            f"members={members_token} "
            f"world={room.world_player_count()} "
            f"peers={','.join(peers) if peers else '-'} "
            # 每成员显示名（登录名 / 游客名）与每成员到中继的 RTT。
            # 老客户端不认识这两个 token —— 直接忽略（additive 协议规则）。
            f"names={names_token} "
            f"rtts={rtts_token} "
            # ============================================================
            #  `started=` —— 已开始连接的成员 client_id 列表（ADDITIVE）
            # ============================================================
            #
            # 精确格式（客户端按这个解析）：
            #     started=<cid>,<cid>,...      （一个都没有时是 `-`）
            #
            #   * cid 就是成员的 client_id：ASCII 十六进制/短串，不可能含
            #     `,`，所以按 `,` 切分即可；
            #   * 该成员"已开始连接"= 他此刻有 PLAYER 连接（等待配对或已配对），
            #     或者他是当前 Host 且有 `HOST <room>` 开服控制连接；
            #   * 顺序无关；同一个 cid 不重复出现；
            #   * 老客户端不认识这个 token -> 按空格分词直接忽略。
            #
            # 客户端用法：`peers=` 给出"其他所有成员"，用 `started=` 过一遍
            # 就知道"哪些 peer 真的开始了"，Host 只对它们打洞；Player 用
            # `host=` / `host_name=` 认当前 Host（与谁创建房间无关）。
            f"started={started} "
            # ============================================================
            #  `conn=` —— 每个成员此刻的游戏连接状态（ADDITIVE）
            # ============================================================
            #
            # 精确格式（客户端按这个解析，见 PROTOCOL §8.5）：
            #
            #     conn=<cid>:<state>,<cid>:<state>,...   （没有成员时是 `-`）
            #
            #   * 表里**每个房间成员各一条**，顺序和 `names=` 相同；
            #   * `<cid>` 是成员的 client_id（ASCII，不含 `:` / `,`），
            #     所以按 `,` 切分、再按**第一个** `:` 切分即可；
            #   * `<state>` 只有 4 个取值：
            #       idle      = 此刻没有任何游戏连接（Host 未开服也是它）
            #       waiting   = PLAYER 连接在等待池里，还没配上隧道
            #       pairing   = 已配上隧道，还没进世界（未发 CONNECTED）
            #       connected = 已进世界；对**当前 Host** 表示"正在开服"
            #   * 某个人离开房间 -> 他整条从 `conn=` 消失（和 `names=` 一样）；
            #   * 老客户端不认识这个 token -> 按空格分词直接忽略。
            #
            # 为什么每次都带全表：客户端成员列表的「连接状态」列要显示的是
            # **每个人此刻**的状态，不是"变化过的人"；全表的增量代价是每行
            # 几十字节，换来的是"任何时刻收到的一行都自洽"。
            f"conn={conn} "
            "ips=" + (",".join(m.client_id + ":" + _auto_quote(getattr(m, "public_ip", ""), safe="") for m in room.members.values() if getattr(m, "public_ip", "")) or "-") + " "
            # ============================================================
            #  `mode=` —— 房间此刻的连接模式（ADDITIVE）
            # ============================================================
            #
            # **只由 Owner 的 CONNMODE 改变**（设计基准 §A）。读的是房间真实
            # 的值，`getattr(..., "relay")` 那种兜底已经删掉 —— 它会在房间
            # 没有这个字段时凭空报一个 "relay"，客户端据此就会拆掉直连通路
            # （用户报的"没人操作，模式自己跳回中转"）。
            f"mode={room_conn_mode(room)} "
            # `mode_reason=` —— 这个模式是怎么来的（ADDITIVE，PROTOCOL §8.9）：
            #   owner    = Owner 刚才显式 CONNMODE
            #   restored = 房间销毁后由同一个 Owner 重建，沿用他上次的选择
            #   default  = 全新房间的构造默认值
            # 老客户端不认识它，按空格分词直接忽略。
            f"mode_reason={room_mode_reason(room)}"
            # `was_live=` —— **只在换模式那一轮广播里出现**（ADDITIVE）：
            # 切换前房间里有没有连接（PROTOCOL §8.5 的 conn= 里出现过
            # connected / pairing 就算有）。客户端据此决定要不要自动重连
            # （设计基准 §C）。不是切换瞬间的广播不带这个 token。
            + (f" was_live={was_live}" if was_live is not None else "")
            + "\n"
        )

        pending.append((member, line))

    if not pending:
        return

    await _send_lines_concurrently(
        pending,
        budget=BROADCAST_BUDGET_S,
        timeout=BROADCAST_SEND_TIMEOUT,
    )


def _roster_members(room):
    """
    广播用的花名册：**按 client_id 去重**（同一个 client_id 只留一条，先到的赢）。

    ============================================================================
      ★ 用户 2026-09-29 第 5 条：「房间内仅有四人时，却显示有五人」
    ============================================================================

    客户端的花名册/逐目标行/连接状态列**全部以 client_id 为键**：
    `RoomState.member_names` / `member_conns` 是 dict，`peers=` 被渲染成一行一人。
    而 `room.members` 在"同一个 client_id + 不同账号"时故意保留两条（防篡夺，
    `add_member` 的 `[MEMBER ID REUSED]` 分支）。

    于是广播里出现：

        members=5                (len(room.members))
        peers=cidA,cidB,cidB,cidC (列表，cidB 出现两次)
        conn=cidA:..,cidB:..,cidC:..,cidD:..  (dict，本来就去重)

    客户端渲染出来的行数 = 1(自己) + 去重后的 peers = 4 —— **人数 5、列表 4 行**。

    修法就是这里：广播出去的整张表用**同一个键空间**（client_id），
    `peers=` / `names=` / `rtts=` / `members=` 全部从这个去重花名册派生，
    收信人能画出来的行数就正好等于 `members=`。正常房间（cid 互不相同）
    结果与以前逐字相同。
    """
    seen = set()
    out = []

    for member in (getattr(room, "members", None) or {}).values():
        key = (
            str(getattr(member, "client_id", "") or "").strip()
            or str(getattr(member, "member_key", "") or "").strip()
        )

        if key in seen:
            continue

        seen.add(key)
        out.append(member)

    return out


def roster_count(room):
    """
    「房间里有多少人」的**广播口径**：按 client_id 去重（见 `_roster_members`）。

    `member_count()`（= `len(room.members)`）是字典长度，同一个 client_id 出现
    两条记录时会比客户端能画出来的行数多。凡是发给人看的人数都用这一条。
    """
    roster = _roster_members(room)

    return len(roster) if roster else room.member_count()


def _member_names_token(room):
    """
    `names=<cid>:<percent-encoded-name>,...`；一个都没有时是 `-`。

    client_id 是 ASCII、不可能含 `:`，所以客户端按**第一个** `:` 切分；
    percent-encoding 保证名字里不会出现空格 / `,` / `:`，这一行切不坏。
    没有 client_id 的成员（理论上不存在）跳过：没有定位键就无法进表。
    """
    entries = []

    for member in _roster_members(room):
        client_id = (getattr(member, "client_id", "") or "").strip()

        if not client_id:
            continue

        name = member.display_name

        if not name:
            continue

        entries.append(f"{client_id}:{room_names.encode_field(name)}")

    return ",".join(entries) or "-"


def _member_rtts_token(room):
    """
    `rtts=<cid>:<ms>,...`；没有**任何**已测数据的成员时是 `-`。

    只写真正测到的数字（整数毫秒）。没有数据的成员整条不出现 —— 客户端
    （和面板）就不显示延迟，而不是显示一个编出来的 0。
    """
    entries = []

    for member in _roster_members(room):
        client_id = (getattr(member, "client_id", "") or "").strip()
        rtt = getattr(member, "rtt_ms", None)

        if not client_id or rtt is None:
            continue

        entries.append(f"{client_id}:{int(round(rtt))}")

    return ",".join(entries) or "-"


def started_client_ids(room):
    """
    `[...]` —— 房间里**已经开始连接**的成员 client_id（顺序稳定、去重）。

    设计基准 §D/§E：Player 只向**当前 Host** 打、Host 只向**已开始连接的**
    Player 打。中继不需要新协议就能知道这件事 —— "开始连接"本来就等于
    客户端真的发起了连接：

      * Player 点「开始连接」-> 打开 PLAYER 连接（先进等待池、再配对）；
      * Host 点「开始连接」-> 打开 `HOST <room>` 开服控制连接（然后才开
        HOST_TUNNEL）；`host_writer` 就是它的存在证明。

    所以这里从**连接本身**现算：host 控制连接 + 等待池里的 PLAYER
    （waiter.client_id）+ 已配对的会话（pair.player_id）。

    ============================================================================
      Host 那个 cid 必须和 `host_id=` **同一个来源**（host_client_id）
    ============================================================================

    这里以前加的是 `room.host_id`（老式 `HOST <room>` 控制连接握手时报的
    client_id），而 `host_id=` 用的是 `host_client_id(room)`（V3 成员认领时，
    是**成员**的 client_id）。两套 id 在真实客户端上会不一样（开服控制连接
    经常带的是中继补的匿名 id），于是广播里出现
    `host_id=h-cid` 但 `started=c5b71f4b8f33464b` —— Host 侧
    `started ∩ peers = ∅`（一个目标都拉不到），Player 侧永远判 `host_not_ready`。

    不变量（回归测试 relay_baseline_test 钉死）：**`host_id=X` 时
    `started=` 里必须包含 `X`**。所以这里只认 host_client_id()。

    不引入任何新的客户端握手，也就没有"客户端忘了上报 started"这种状态；
    连接断了这个集合自然就小了，下一次广播自动反映。

    ============================================================================
      2026-09-28：新增**显式上报**（ADDITIVE，取并集，不取代现算）
    ============================================================================

    用户口径：「已开始连接」= **用户在客户端点过「开始连接」**，而不是"已经
    建立了 PLAYER / HOST 连接"。两者在 relay 模式下恰好重合（点开始连接
    之后游戏才会开 PLAYER 连接），但在 STUN 模式下**不重合**：点「开始连接」
    先开始打洞（房间级），游戏连接是之后的事；只按连接现算，Host 会看不到
    "已经点了开始、正在打洞"的玩家。

    所以新客户端会在成员控制连接上发一行

        STARTED <1|0>

    （见 _member_control_body 的 STARTED 分支 / PROTOCOL §8.6）。这里把
    `member.connecting` 为 True 的成员**并进** `started=`：

      * 老客户端从不发 -> 一个都没有 -> 结果与以前**逐字相同**；
      * 新客户端发 -> 点了开始就一定在 `started=` 里，不管有没有 PLAYER 连接。

    并集而不是替换：连接现算仍然有效（它是"事实"），显式上报是"意图"，两者
    都对 Host 选目标有用；而且这样**删除上报也不会让已经连上的玩家消失**。

    用 getattr + 默认值取字段：`broadcast_member_state` 会被单元测试里的
    假 Room 调用（那里没有 waiting_players），不能因此抛异常。
    """
    seen = []

    def add(value):
        cid = str(value or "").strip()

        if cid and cid != "-" and cid not in seen:
            seen.append(cid)

    # Host 已开始连接 = 开服控制连接在（HOST_TUNNEL 在 relay 侧没有 client_id，
    # 但它一定伴随 host_writer 存在，见 handle_host_tunnel 的门禁）。
    #
    # cid 用 host_client_id()：和广播里的 `host_id=` 完全同源（成员认领时是
    # 成员的 client_id，老式 HOST 直连时才退回 host_id）。
    if getattr(room, "host_writer", None) is not None:
        add(host_client_id(room))

    for waiter in getattr(room, "waiting_players", None) or ():
        add(getattr(waiter, "client_id", ""))

    for pair in (getattr(room, "sessions", None) or {}).values():
        add(getattr(pair, "player_id", ""))

    # 显式上报（ADDITIVE）：`STARTED 1` 的成员。顺序放在最后，`started=` 的
    # 前几项（host / 等待 / 已配对）保持不变，老断言（逐字比较）不受影响。
    for member in (getattr(room, "members", None) or {}).values():
        if getattr(member, "connecting", False):
            add(getattr(member, "client_id", ""))

    return seen


def started_token(room):
    """`started=` 的取值：逗号分隔的 client_id，空时是 `-`（见广播说明）。"""
    return ",".join(started_client_ids(room)) or "-"


# ============================================================================
#  每成员游戏连接状态（HOST_STATE 的 `conn=`，ADDITIVE —— PROTOCOL §8.5）
# ============================================================================
#
#  用户的现场（2026-09-28 原话）：「这边玩家断开连接，那边 host 这个连接状态
#  还是显示已连接，未及时更正状态，所以我觉得可以把连接状态也包进我们管理广播
#  TCP 里，这样会比较好，所有状态迅速同步」。
#
#  在此之前，中继的广播里**没有**"每个成员现在的游戏连接状态"这一项：客户端
#  成员列表的「连接状态」列只能对房间里的人一律写「已连接」（它只从 `peers=`
#  知道"这个人在房间里"）。所以玩家掉线之后，Host 那边那一行还挂着「已连接」，
#  一直到下一次 `HOST_STATE` 里那个人消失为止 —— 而"人离开房间"和"游戏断开"
#  本来就不是一回事（停止游戏不等于退出房间）。
#
#  这个字段就是那个事实的唯一来源，取值刻意只有 4 个，且每个都能从**连接本身**
#  直接读出来，绝不猜测：

CONN_IDLE = "idle"
#: 有一条活的 PLAYER 连接，还在等待 HOST_TUNNEL 配对（进了等待池）。
CONN_WAITING = "waiting"
#: PLAYER 已经和某条 HOST_TUNNEL 配对，但还没进世界（没发 CONNECTED）。
CONN_PAIRING = "pairing"
#: 已经在世界里（CONNECTED 已发出，字节已经在流）；对**当前 Host** 来说，
#: `connected` = 正在开服（`HOST <room>` 控制连接在，玩家可以连他的世界）。
CONN_CONNECTED = "connected"

#: `conn=` 允许出现的全部取值（客户端/面板按这个白名单解析）。
CONN_STATES = (CONN_IDLE, CONN_WAITING, CONN_PAIRING, CONN_CONNECTED)

#: 掉线看护的轮询间隔（秒）。见 watch_player_socket。
CONN_STATE_POLL_S = 0.2


def member_conn_states(room):
    """
    `{client_id: state}` —— 房间里**每个成员**此刻的游戏连接状态。

    顺序 = `room.members` 的插入顺序（和 `names=` / `peers=` 同一套顺序），
    这样同一行的两张表能对上人。

    口径（全部从连接现算，不猜）：

      1. 每个成员默认 `idle`；
      2. 等待池里的 PLAYER（`waiting_players[].client_id`）-> `waiting`；
      3. 已配对的会话（`sessions[].player_id`）-> `pairing`；如果
         `pair.established`（CONNECTED 已发出）-> `connected`；
      4. 当前 Host（`host_client_id(room)`）且 `host_writer` 在（正在开服）
         -> `connected`（覆盖上面的 1/2/3）。

    注意第 4 条的方向：只有**正在开服**才算 connected；Host 停止开服
    （`HOST <room>` 控制连接结束）之后立刻回到 `idle` —— 这正是用户要的
    「Host 开服/停服也要反映」。

    用 getattr + 默认值取字段：会被单元测试里的假 Room 调用。
    """
    states = {}

    def put(cid, state):
        cid = str(cid or "").strip()

        if cid and cid != "-":
            states[cid] = state

    # 1. 每个成员先给 idle —— 名单里有谁，`conn=` 里就有谁（成员走人时整条
    #    消失，和 `names=` / `peers=` 一样，客户端据此判断"他不在房间了"）。
    for member in (getattr(room, "members", None) or {}).values():
        put(getattr(member, "client_id", ""), CONN_IDLE)

    # 2. 等待配对
    for waiter in getattr(room, "waiting_players", None) or ():
        put(getattr(waiter, "client_id", ""), CONN_WAITING)

    # 3. 已配对 / 已进世界
    for pair in (getattr(room, "sessions", None) or {}).values():
        put(
            getattr(pair, "player_id", ""),
            CONN_CONNECTED if getattr(pair, "established", False)
            else CONN_PAIRING,
        )

    # 4. 当前 Host 正在开服 = connected。
    #
    #    只在**这个 cid 已经是表里的一员**时覆盖：`conn=` 与 `names=` 的键集合
    #    必须一致（客户端拿它当"房间里每个人的状态"）。老式房间（只有
    #    `HOST <room>` 控制连接、没有成员）里 host_client_id() 会退回
    #    `room.host_id`，那不是成员 —— 那种房间的 Host 状态继续由
    #    `host=` / `host_name=` / `started=` 表达，不往 conn= 里塞陌生人。
    if getattr(room, "host_writer", None) is not None:
        cid = str(host_client_id(room) or "").strip()

        if cid in states:
            states[cid] = CONN_CONNECTED

    # ============================================================================
    #  ★ 5. 客户端**自己上报**的状态优先（用户 2026-10-01 第 3 条）
    # ============================================================================
    #
    # 上面 1~4 全是中继**猜**出来的（从它看得见的 socket/会话推），而连接到底
    # 通没通，只有客户端自己最清楚（STUN 模式下数据甚至不走中继）。所以：
    #
    #   * 成员用 `STATE <...>` 上报过 -> **以它为准**（`reported_conn`）；
    #   * 没上报过（老客户端 / 刚进房还没发）-> 保持上面的推导，行为不变。
    #
    # **纯显示**：这个值只进 HOST_STATE 的 `conn=` 和面板快照，任何门禁/配对/
    # 打洞决策都不读它（见本文件 2747 起的常量说明与 relay 的 `conn=` 用法）。
    for member in (getattr(room, "members", None) or {}).values():
        reported = str(getattr(member, "reported_conn", "") or "").strip()

        if reported in CONN_STATES:
            put(getattr(member, "client_id", ""), reported)

    return states


def conn_token(room):
    """
    `conn=` 的取值：`<cid>:<state>,<cid>:<state>,...`；一个成员都没有时是 `-`。

    cid 是 ASCII（十六进制/短串），不可能含 `:` 或 `,`；state 是固定 4 个
    ASCII 单词之一。客户端按**第一个** `:` 切分即可（PROTOCOL §8.5）。
    """
    states = member_conn_states(room)

    return ",".join(f"{cid}:{state}" for cid, state in states.items()) or "-"


async def broadcast_room_closed(room, reason=""):
    """
    房间被销毁前，逐一通知房间里的人。

    Sent BEFORE the sockets are closed, because a silent disconnect is
    indistinguishable from a network blip: the client would treat it as
    "connection lost", keep the user sitting in a room that no longer exists,
    and never explain why. With this line the client knows the room is gone,
    cleans up, and frees its buttons.
    """
    message = f"ROOM REVOKED {reason or '房间已关闭'}\n"

    for member in list(room.members.values()):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(member.send(message), timeout=1.0)


#: 房主离开时，游戏连接被断开的原因码（ADDITIVE）。
#:
#: 老客户端不认这一行 -> 当成普通错误/断线，行为不变；新客户端据此弹
#: 「房主已离线，需等待房主重新连接」，而不是把断线当成网络抖动。
HOST_LEFT_CODE = "HOST_LEFT"

#: Host **主动停止开服**（host 位还在，人还在房间）时，其他玩家游戏连接被断开的原因码。
#:
#: ★ 用户 2026-09-30 定稿：host 手动点断开 -> 其他人断开 + 重算按钮状态，
#:   **不自动重连**、**不弹窗**（只写一行原因）。和"闪断"（`HOST_LEFT`）分开，
#:   因为两者的客户端处理不同：闪断要"能复用就复用，不然自动重连"。
HOST_STOPPED_CODE = "HOST_STOPPED"

#: Host **主动释放主机位**时，其他玩家游戏连接被断开的原因码。
#:
#: ★ 用户 2026-09-30 定稿：host 主动释放 -> 其他人断开 + **弹窗「host 已断开连接」**
#:   + 房间列表显示 host 暂无人认领 + 相关状态重算，**不自动重连**。
HOST_RELEASED_CODE = "HOST_RELEASED"

#: Host 已认领「主机位」但还没开始连接（没有 HOST 控制连接 / 隧道）时，
#: 其他玩家点开始连接会被中继拒绝的原因码（ADDITIVE）。
#:
#: 格式（精确，客户端按这个解析）：
#:     ERROR HOST_NOT_READY Host 尚未开始连接，请等待 Host 开服
#:
#: 与 `ERROR NO_HOST`（房间里根本没人认领主机位）分开：客户端对这两者的
#: 提示不同（「当前房间暂无 Host，请等待 Host 认领」 vs
#: 「Host 未连接，请等待 Host 开服」），所以不能都塞进 NO_HOST。
HOST_NOT_READY_CODE = "HOST_NOT_READY"

#: 连接模式热切换时，旧配对 / 隧道被清理的原因码（ADDITIVE）。
#:
#: 模式变了就必须严格重建全部通路（设计基准 §D/§E），所以中继在切换瞬间
#: 把旧的 PLAYER / HOST_TUNNEL / 配对会话全部作废。客户端拿到
#: `ERROR CONN_MODE_CHANGED ...` 就知道"这是我要求的换模式造成的，按新模式
#: 重建"，而不是把它当成一次网络抖动。
CONN_MODE_CHANGED_CODE = "CONN_MODE_CHANGED"


async def disconnect_game_connections(
    room,
    reason="房主已离线，请等待房主重新连接",
    code=HOST_LEFT_CODE,
    extra="",
):
    """
    房主（主机位持有者）离开 / 掉线 / 被踢 / 超时：把房间里**所有游戏连接**
    断开，并给每个被断开的连接一个**可识别的原因**。

    房间本身**保留**（成员控制连接不动，房主位由调用方先清空）——用户 2026-09-27
    的最终口径：所有人断开的是"进世界的连接"，不是"房间成员关系"；房间留着，
    房主若回来可以重新认领，别人也可以认领。

    `code` 是给客户端的可识别原因码，默认 `HOST_LEFT`（房主离开）。换模式
    清理旧通路时传 `CONN_MODE_CHANGED`（见那里的说明）。

    `extra` 是**追加在原因行尾部**的 additive token（当前只有换模式会传
    ` was_live=<0|1>`）。原因码与位置字段一个都没动：客户端按空格分词，
    不认识的尾部 token 直接忽略。

    为什么分两种通知方式：

      * **还没进世界**的 PLAYER（还在等配对 / 没收到 CONNECTED）：socket 上
        还是控制行，直接发 `ERROR <code> <原因>` 是安全的，客户端读得到；
        还在等配对池里的 HOST_TUNNEL 同理。
      * **已经进世界**的 PLAYER：那条 socket 从 CONNECTED 起就是**纯原始
        Minecraft 字节**（协议冻结，谁都不许往里插东西）。所以只关 socket，
        原因放在**成员控制连接**上广播 `<code> <原因>` —— 客户端两边是
        同一个人的两个 socket，认得出这是"房主离线"。
    """
    extra = str(extra or "")
    message = f"ERROR {code} {reason}{extra}\n"

    async with room.lock:
        pairs = list(room.sessions.values())
        waiting = list(room.waiting_tunnels) + list(room.waiting_players)

    notified = 0

    # ============================================================================
    #  ★ v3.9.16：通知**并发** + 每条有上限 + 整体有预算
    # ============================================================================
    #
    # 以前这三步是**串行**的裸 `await drain()`：任何一条半开 TCP 都能把整个
    # 函数挂死。挂死的后果不是"某个人没收到通知"，而是：
    #
    #   * 后面所有人的通知都发不出去；
    #   * **第 4 步的"真的拆"根本执行不到** —— 会话留着、`host_writer` 不清，
    #     房间从此开不了服；
    #   * 用户看到的就是"host 断了，可谁都没断开"（而且是偶发的）。
    #
    # 现在：先把所有要写的目标收进一个列表，一次性 `gather` 出去，每条最多
    # `SEND_DRAIN_TIMEOUT` 秒，整批最多 `NOTIFY_BUDGET_S` 秒。无论发生什么，
    # 函数都会往下走到"真的拆"那一步。
    #
    # 先写、后拆的顺序保持不变：waiting 里的 PLAYER 要靠这一行才知道原因
    # （拆了 socket 就发不出去了）。
    notify_targets = []

    # ---- 1. 等待池里的玩家 / 隧道：直接告诉原因 ----
    for waiter in waiting:
        writer = getattr(waiter, "writer", None)

        if writer is not None:
            notify_targets.append((writer, message.encode()))

    # ---- 2. 还没进世界的会话：两边（玩家 / 隧道）都能收控制行 ----
    for pair in pairs:
        if getattr(pair, "established", False):
            continue

        for writer in (
            getattr(pair, "player_writer", None),
            getattr(pair, "tunnel_writer", None),
        ):
            if writer is not None:
                notify_targets.append((writer, message.encode()))

    if notify_targets:
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        write_with_timeout(writer, data)
                        for writer, data in notify_targets
                    ),
                    return_exceptions=True,
                ),
                timeout=NOTIFY_BUDGET_S,
            )

            notified += sum(
                1 for ok in results if ok is True
            )
        except Exception:
            # 整批超时：一条都没指望了，直接往下拆。
            notified = 0

    # ---- 3. 房主离开这件事，也让房间里每个成员的控制连接知道 ----
    #
    # 已进世界的玩家在 PLAYER socket 上收不到文本，这一行是他们唯一的"原因"。
    # 同样并发、同样有上限（`Member.send` 内部已经走 `write_with_timeout`）。
    members = list(getattr(room, "members", {}).values())

    if members:
        member_line = f"{code} {reason}{extra}\n"

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(member.send(member_line) for member in members),
                    return_exceptions=True,
                ),
                timeout=NOTIFY_BUDGET_S,
            )
        except Exception:
            pass

    # ---- 4. 真的拆 ----
    #
    # ★ v3.9.16：这一步**无论如何都会执行到**（上面的通知全部有上限）。
    # "通知发完了才拆"是以前的顺序，一条卡死的连接就能让这一步永远不被执行
    # —— 那才是"全房谁都不断开"的真凶。
    for pair in pairs:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                room.remove_session(pair.session_id, reason=code),
                timeout=NOTIFY_BUDGET_S,
            )

    for waiter in waiting:
        with contextlib.suppress(Exception):
            waiter.release()

    # ============================================================================
    #  只摘掉**这一次真正作废的**那几条，绝不 `clear()` 整个池子
    # ============================================================================
    #
    # 这里以前是无条件 `room.waiting_tunnels.clear()` /
    # `room.waiting_players.clear()`。上面几步全是 await（drain / remove_session
    # / member.send），而 host_watchdog 或一次"停止→立刻重开"的连接重试完全
    # 可能在**这段窗口里**把新隧道 / 新 PLAYER 加进池子 —— 快照里没有它们，
    # 那两句 clear() 却会把它们一起抹掉：
    #
    #   * 客户端已经收到 `WAIT`（它认为自己有一条空闲隧道），
    #   * 中继的池子里却什么都没有，`try_pair` 永远看不到它，
    #   * 玩家进来时无隧道可配 -> 一直等 -> 界面就是"连不上、隧道没有重建"。
    #
    # 所以改成按**对象身份**摘除：新加进池子的连接一条都不受影响。
    async with room.lock:
        for waiter in waiting:
            if waiter in room.waiting_tunnels:
                room.waiting_tunnels.remove(waiter)

            if waiter in room.waiting_players:
                room.waiting_players.remove(waiter)

    for waiter in waiting:
        writer = getattr(waiter, "writer", None)

        if writer is None:
            continue

        with contextlib.suppress(Exception):
            writer.close()

    if pairs or waiting:
        _log(
            "[GAME DROPPED]",
            f"room={room.name}",
            f"sessions={len(pairs)}",
            f"waiting={len(waiting)}",
            f"notified={notified}",
            f"reason={code}",
        )

        # 清理本身就是一次**连接状态变化**：`conn=` / `started=` / `world=`
        # 都要当场反映出来。调用方（换模式 / 房主离开 / 停服）通常紧接着还会
        # 广播一次，重复广播无害；但"清理完必须已经广播过"这件事由这里自己
        # 保证，不依赖调用方的顺序。
        #
        # ★ v3.9.16：这一步也要有上限 —— 它是本函数的最后一步，卡在这里
        # 等于"拆了但没人知道"，界面上的 `conn=` 还是老样子。
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                broadcast_member_state(room), timeout=NOTIFY_BUDGET_S
            )

    return len(pairs) + len(waiting)


# ============================================================================
#  PLAYER socket 掉线看护 —— "状态立刻同步"的另一半
# ============================================================================
#
#  `HOST_STATE` 的 `conn=` 只有在**每次都立刻广播**的前提下才有意义。大部分
#  路径本来就立刻广播（PLAYER 接入 / CONNECTED / remove_session / 被踢 /
#  Host 开服停服），但有一条一直没人管：
#
#      PLAYER socket 已经进了等待池、还没配上隧道就断了
#      （玩家点了开始连接，游戏连了一下又关了 / 网络闪断）。
#
#  等待期间 relay 不读那条 socket（协议规定 CONNECTED 之前不传数据），所以
#  这个断开只有两种时候才被发现：
#
#    * 下一次 `try_pair()` 顺手用 `_waiter_closed()` 扔掉（可能永远不来，
#      因为池子里还有别的隧道时它根本不会被挑到）；
#    * `PAIR_HANDSHAKE_TIMEOUT`（默认 20s）之后由会话看门狗拆掉（但那只覆盖
#      "已配对"的情况）。
#
#  结果就是用户报的那条：Host 那边「连接状态」还挂着「已连接」，实际上人早就
#  掉了。这个看护任务用 `reader.at_eof()` **轮询**（不读 socket、不碰任何字节，
#  所以对冻结的数据通路零影响），一发现 EOF 就：
#
#    1. 还在等待池里 -> 按对象身份摘出来 + `waiter.release()` 唤醒 handler
#       + 立刻广播（`conn=` 当场变 idle，`started=` 当场缩小）；
#    2. 已经配上隧道但还没进世界 -> `remove_session()`（它自己会广播）；
#    3. 已经进世界 -> **什么都不做**（那条 socket 归 `bidirectional_relay`
#       管，它读到 EOF 会走同一条 remove_session）；看护任务在此之前就退出。
#
#  轮询间隔 CONN_STATE_POLL_S = 0.2s：对"迅速同步"足够，而且每条 PLAYER 连接
#  只有一个 0.2s 的 timer，代价可以忽略。

async def watch_player_socket(room, waiter, reader):
    """
    看护一条 PLAYER socket：等待 / 配对阶段掉线 -> 立刻清理并广播。

    由 `relay_server.handle_player()` 起，和那条连接的 handler 一起收尾
    （handler 的 finally 会 cancel 这个任务）。永不抛异常：它只是"状态同步"，
    绝不能影响数据通路。
    """
    try:
        while True:
            await asyncio.sleep(CONN_STATE_POLL_S)

            # 已进世界：后面归 bidirectional_relay 管，看护到此为止。
            session_id = getattr(waiter, "session_id", None)

            if session_id:
                pair = (getattr(room, "sessions", None) or {}).get(session_id)

                if pair is not None and getattr(pair, "established", False):
                    return

            if reader is not None and reader.at_eof():
                break
    except asyncio.CancelledError:
        raise

    except Exception:
        return

    await handle_player_socket_lost(room, waiter)


async def handle_player_socket_lost(room, waiter):
    """
    PLAYER socket 在等待 / 配对阶段断开：把它从池子里收拾干净并**立刻广播**。

    幂等：会话已经没了、waiter 已经不在池子里，都只是什么都不做。
    """
    # ---- ① 还在等待池里（还没配对）：按身份摘掉，唤醒 handler ----
    removed = False

    async with room.lock:
        if waiter in room.waiting_players:
            room.waiting_players.remove(waiter)
            removed = True

    if removed:
        _log(
            "[PLAYER DROPPED]",
            f"room={room.name}",
            f"client_id={getattr(waiter, 'client_id', '') or '-'}",
            "reason=socket closed while waiting for a tunnel",
        )

        with contextlib.suppress(Exception):
            waiter.release()

        with contextlib.suppress(Exception):
            await broadcast_member_state(room)

        return

    # ---- ② 已经配上隧道（还没进世界）：拆掉这一对（remove_session 会广播）----
    session_id = getattr(waiter, "session_id", None)

    if session_id and session_id in (getattr(room, "sessions", None) or {}):
        _log(
            "[PLAYER DROPPED]",
            f"room={room.name}",
            f"session={session_id}",
            "reason=socket closed before the world was entered",
        )

        with contextlib.suppress(Exception):
            await room.remove_session(
                session_id, reason="player socket closed"
            )


async def drop_all_sessions(room, reason=""):
    """
    Disconnect every Minecraft session in a room, keeping the room itself.
    Used when the host position is released or its holder leaves: nobody can
    play without a host, but the room is still valid and the other members stay
    in it.

    ============================================================================
      FIX: 这里原来调的是 `await pair.close(...)` —— Pair 上没有这个方法
    ============================================================================

    `Pair`（relay_server.py）没有 `close()`，所以每一轮都会抛
    AttributeError，而下面那句 `except Exception: pass` 把它**静默吞掉**：
    这个函数名叫 drop_all_sessions，实际上一个会话都没拆。RELEASE_HOST /
    主机离开 / 关房这三条路都受影响（它们最后靠 Room.close() 兜底，所以
    症状被掩盖成了"偶发的通道不通、拒绝连接"）。

    现在改用真正有效的 Room.remove_session() —— 和 admin_kick_player /
    玩家自己的清理路径同一条。例外仍然逐个吞：一个坏会话不能挡住其余的拆除。
    """
    async with room.lock:
        pairs = list(room.sessions.values())

    for pair in pairs:
        try:
            await room.remove_session(
                pair.session_id, reason=reason or "host gone"
            )
        except Exception:
            pass

    if pairs:
        _log(
            "[SESSIONS DROPPED]",
            f"room={room.name}",
            f"count={len(pairs)}",
            f"reason={reason or '-'}",
        )

    return len(pairs)


# ============================================================
# 踢人（KICK）—— 只踢一个人，房间照旧
# ============================================================
#
# 和 revoke_room() 的区别就是"一个"和"所有人"：
#
#   KICK       把某一个成员的控制连接关掉，房间继续存在（除非被踢的是
#              所有者 —— 房间的生命绑在所有者身上，见下面的特殊处理）。
#   revoke_room 管理员关房，通知所有人、断开所有游戏会话、销毁房间。
#
# 两个动作共用同一套"把游戏会话拆干净"的代码（Room.remove_session），
# 所以被踢的人不会在中继里留下一条没有主人的隧道。

def _clean_label(value):
    """
       把 label 压成能放进控制行的一个 token。

    `KICKED by=<label>` 是按空格分词的，而 username 是显示名，法律上可以
    带空格/控制字符（见 broadcast_member_state 里 host_name 的同一处理）。
    这里把空格和不可见字符去掉，宁可少几个字也不能把这一行拆坏。
    """
    text = "".join(
        c for c in str(value or "") if ord(c) >= 33 and ord(c) != 127
    )

    return text or "-"


async def send_kicked(target, requester_label="", reason="kicked"):
    """告诉被踢的人"你被踢了"，然后由调用方负责关掉他的连接。"""
    with contextlib.suppress(Exception):
        await target.send(
            f"KICKED by={_clean_label(requester_label)} "
            f"reason={_clean_label(reason)}\n"
        )


async def drop_member_sessions(room, member, reason="member kicked"):
    """
    把**某一个成员**的游戏会话拆掉（别人的不动）。

    一个成员有两个 socket：房间成员的控制连接，和进世界时的 PLAYER 连接。
    只关控制连接的话，PLAYER 那条隧道还配着、还占着名额 —— 就是幽灵会话。
    拆会话走的是 Room.remove_session()（房间关闭路径用的也是它），这里只是
    把范围收窄到"这个成员自己的会话"。

    匹配优先用 client_id（PLAYER 握手带的就是它）；老客户端没带 id 时退回
    账号名。宁可漏拆也不误拆：拆错了会把别人踢下线。
    """
    client_id = getattr(member, "client_id", "") or ""
    username = getattr(member, "username", "") or ""

    async with room.lock:
        pairs = list(room.sessions.values())

    victims = []

    for pair in pairs:
        player_id = getattr(pair, "player_id", "") or ""
        player_name = getattr(pair, "player_username", "") or ""

        if client_id and player_id and player_id == client_id:
            victims.append(pair)
        elif not player_id and username and player_name == username:
            # 老客户端不带 client_id：只能按账号名认人。
            victims.append(pair)

    for pair in victims:
        with contextlib.suppress(Exception):
            await room.remove_session(pair.session_id, reason=reason)

    return len(victims)


async def kick_member(room, target, requester_label="", reason="kicked"):
    """
    执行一次"踢普通成员"：通知 -> 拆会话 -> 关控制连接。

    `is_host` 的清理不在这里做：关掉连接之后，成员自己的清理路径
    （handle_member_join 的 finally -> remove_member）会把主机位释放掉，
    主机位随即变成可再次认领的状态。这正是"host 被踢"要求的行为，而且
    复用已有的唯一清理路径，不另开一条。

    返回被拆掉的会话数。
    """
    # 必须在他被 remove_member 清掉之前、从房间此刻的权威状态记住：下面要用
    # 它决定"要不要把整个房间的游戏会话都拆掉"（不用 target.is_host 那个可能
    # 过期的对象标记）。
    was_host = room.host_member is target

    # 清理路径（handle_member_join 的 finally）据此把主机位释放记成
    # `[HOST RELEASE] ... reason=kick`。
    target.removal_reason = "kick"

    await send_kicked(target, requester_label=requester_label, reason=reason)

    dropped = await drop_member_sessions(
        room, target, reason=f"kicked ({reason})"
    )

    # ============================================================
    #  被踢的是当前主机：主机位一空，谁都不能玩了
    # ============================================================
    #
    # "主机离开 -> 房间里的游戏会话全部断开"是这个中继的既定语义（见
    # RELEASE_HOST 和 handle_member_join 的 finally）。主机被踢属于同一件事，
    # 只是触发方式不同，所以走同一条 drop_all_sessions()。
    #
    # （这个函数以前内部调的是不存在的 Pair.close()，异常被吞、实际一个会话都
    #   不拆；本轮已改成 Room.remove_session()，见 drop_all_sessions 的说明。）
    if was_host:
        # ====================================================================
        #  ★ 用户 2026-09-30：host（非房主）被房主踢出房间 -> **身份必须释放**
        # ====================================================================
        #
        # 以前这里只 `drop_all_sessions()`（静默关 socket），然后把"释放主机位"
        # 完全甩给被踢成员自己的异步清理路径（handle_member_join 的 finally
        # -> take_host_from）。那条路**通常**会跑，但它依赖
        # "关掉 writer -> 控制循环从 await 里醒来 -> 跑 finally"：
        #   * 循环正卡在某个 await 上时，finally 会晚很久（甚至要等看门狗），
        #     这段时间里 `host=` 仍然是 1 —— 别人看就是"踢了人，host 身份还在"；
        #   * 那条路还会把"踢掉 host"的断开原因说成普通原因。
        #
        # 现在改成**当场、显式**释放，和 KICK 同步：
        #   1. `take_host_from(target)` —— 主机位立刻空出来（幂等：那条异步
        #      清理路径稍后再跑一次也是无害的）；
        #   2. `release_legacy_host_for(target)` —— 他名下那条老式 `HOST <room>`
        #      开服连接一起放掉（否则 `host_alive()` 还是真，新的 PLAYER 会
        #      被喂给一个已经被踢掉的人）；
        #   3. `disconnect_game_connections(..., code=HOST_RELEASED)` —— 全房
        #      游戏连接断开，并且每个成员拿到**可识别的原因**（客户端据此断开
        #      + 重算 + 「host 位置暂无人认领」，**不自动重连**）。
        dropped += await drop_all_sessions(room, reason="host kicked")

        with contextlib.suppress(Exception):
            if room.take_host_from(target):
                # 和成员清理路径同一条审计行（那条路现在不会再记了：主机位
                # 已经被这里当场释放）。工具/回归依赖这行 `reason=kick`。
                log_host_release(room, target, "kick")

        with contextlib.suppress(Exception):
            await room.release_legacy_host_for(target)

        with contextlib.suppress(Exception):
            await disconnect_game_connections(
                room,
                reason="host 已被移出房间，host 位置暂无人认领",
                code=HOST_RELEASED_CODE,
            )

    # 让 KICKED 真的先离开 socket，再发 FIN —— 和 revoke_room 里同一个理由：
    # 客户端要能读到"为什么被断开"，而不是只看到一次普通断线。
    await asyncio.sleep(0.05)

    target.closed = True

    if target.writer is not None:
        with contextlib.suppress(Exception):
            target.writer.close()

    return dropped


async def handle_member_kick(room, member, text):
    """
    KICK <room_token> <target_client_id>

    请求 → 回复（都在请求者自己的成员连接上）：
        OK KICK <target_client_id>
        OK KICK <target_client_id> OWNER_LEFT_ROOM
        ERROR KICK_FORBIDDEN
        ERROR KICK_NO_SUCH_MEMBER
        ERROR KICK_SELF

    权限矩阵（服务端强制，客户端改不了）：

        * 请求者 == 目标                  -> ERROR KICK_SELF
        * 请求者是房间所有者              -> 可以踢除自己以外的任何人
        * 请求者是当前主机（非所有者）    -> 只能踢非所有者、且不是自己的成员
                                             （踢所有者 -> ERROR KICK_FORBIDDEN）
        * 其他人                          -> ERROR KICK_FORBIDDEN

    ============================================================================
      "主机能不能踢所有者" —— 按用户原话
    ============================================================================

    用户原话：「当前 host 是房主时就除了自己他都能踢，host 非房主时就是除了
    当前房主和 host 他都能踢出」。所以非 owner 的 host 踢 owner 回
    `ERROR KICK_FORBIDDEN`，房间不受影响。

    `OK KICK <target> OWNER_LEFT_ROOM` 与"踢 owner = 解散房间"仍然保留：那是
    **管理 API / 面板**那条路（管理员踢房主，见 relay_server.admin_kick_member），
    它复用同一条 revoke_room()。成员连接这条路上，唯一能"踢" owner 的人只有
    owner 自己，而那已经被 KICK_SELF 拦下 —— 下面那个 owner 分支因而不可达，
    但它是冻结协议的一部分，保留下来（以后若放宽权限，去掉 FORBIDDEN 即复用）。
    """
    parts = text.split()

    room_token = parts[1] if len(parts) > 1 else ""
    target_key = parts[2] if len(parts) > 2 else ""

    # ---- 不能踢自己（放最前面，任何身份都一样） ----
    own_keys = {member.client_id, getattr(member, "member_key", "")}
    own_keys.discard("")

    if target_key and target_key in own_keys:
        await member.send("ERROR KICK_SELF 不能踢出自己\n")
        return

    if not target_key:
        await member.send("ERROR KICK_NO_SUCH_MEMBER 没有指定要踢出的成员\n")
        return

    # ---- 权限（所有者 / 当前主机，二选一） ----
    #
    # ============================================================================
    #  只读房间【此刻】的权威状态，绝不读 member 对象上的 is_owner / is_host
    # ============================================================================
    #
    # host 在一个房间的生命周期里是会换人的（CLAIM_HOST / RELEASE_HOST /
    # 掉线 / 被踢 / 重连顶替），所以权限必须在**发 KICK 的这一瞬间**从
    # `room.owner` / `room.host_member` 现算。这两个都是实时属性：每次访问
    # 重新遍历 room.members，不做缓存。
    #
    # 为什么不能顺手 `or member.is_host`：同一个 client_id + 账号重连时，
    # 身份（is_owner / is_host）会被搬到**新**的成员对象上，而**旧**连接对象
    # 上的标记不会清（socket 也还开着，它的控制循环可能继续收到 KICK）。
    # 拿旧对象上的 is_host 判权限，等于让"已经卸任 / 已经被顶替的旧连接"
    # 继续能踢人 —— 正是本次审计要堵的过期状态。
    is_requester_owner = room.owner is member
    is_requester_host = room.host_member is member

    if not (is_requester_owner or is_requester_host):
        safe_event(
            "room.kick.denied",
            room=lambda: room.name,
            by=lambda: member.label,
            client_id=lambda: member.client_id,
            target=lambda: target_key,
            is_host=lambda: room.host_member is member,
            is_owner=lambda: room.owner is member,
            code=lambda: "KICK_FORBIDDEN",
        )
        await member.send("ERROR KICK_FORBIDDEN 只有房主或当前主机可以踢人\n")
        return

    # ---- 房间 token 必须是这个成员自己的房间 ----
    #
    # 成员连接本身就绑定了房间，所以这里只是把"发错房间"当成"目标不在这个
    # 房间"处理，和下面的目标查找同一个错误码。
    if room_token:
        try:
            wanted = room_names.room_token(room_token)
        except Exception:
            wanted = room_token

        if wanted != room.name:
            await member.send("ERROR KICK_NO_SUCH_MEMBER 目标不在这个房间里\n")
            return

    # ---- 找目标（按 client_id / member_key，和 add_member 的键一致） ----
    target = None

    for other in list(room.members.values()):
        if other is member:
            continue

        if other.client_id == target_key \
                or getattr(other, "member_key", "") == target_key:
            target = other
            break

    if target is None:
        await member.send("ERROR KICK_NO_SUCH_MEMBER 该成员不在房间里\n")
        return

    target_id = target.client_id or getattr(target, "member_key", "") or target_key

    # ============================================================
    #  非 owner 的 host 不能踢 owner（用户原话，房主位高一档）
    # ============================================================
    # 同样是实时判定：target 是从 room.members 里现找的活成员。
    target_is_owner = room.owner is target

    if target_is_owner and not is_requester_owner:
        safe_event(
            "room.kick.denied",
            room=lambda: room.name,
            by=lambda: member.label,
            client_id=lambda: member.client_id,
            target=lambda: target.label,
            is_host=lambda: room.host_member is member,
            is_owner=lambda: room.owner is member,
            code=lambda: "KICK_FORBIDDEN_OWNER",
        )
        await member.send("ERROR KICK_FORBIDDEN 当前主机不能踢出房主\n")
        return

    # ============================================================
    #  特殊：被踢的是房间所有者 —— 房间随之解散
    # ============================================================
    #
    # 成员连接这条路上只有 owner 能踢 owner，而那是 KICK_SELF（见上），所以
    # 走到这里的分支在当前矩阵下不可达；保留它是冻结协议的一部分 —— 踢 owner
    # 的解散行为由管理 API 走同一条 revoke_room()。若以后放宽权限，去掉上面的
    # FORBIDDEN 即可直接复用。
    if target_is_owner:
        _log(
            "[KICK OWNER]",
            f"room={room.name}",
            f"by={member.label}",
            f"target={target.label}",
            f"is_host={room.host_member is member}",
            f"is_owner={room.owner is member}",
        )
        safe_event(
            "room.kick",
            room=lambda: room.name,
            by=lambda: member.label,
            client_id=lambda: member.client_id,
            target=lambda: target.label,
            target_client_id=lambda: target.client_id,
            owner=lambda: True,
            is_host=lambda: room.host_member is member,
            is_owner=lambda: room.owner is member,
        )

        await send_kicked(
            target, requester_label=member.label, reason="owner_kicked"
        )

        # 回复必须在 revoke_room 之前发出：关房会把请求者自己的连接也一起
        # 收掉，晚一步这条 OK 就永远发不出去。
        await member.send(f"OK KICK {target_id} OWNER_LEFT_ROOM\n")

        await revoke_room(room, reason="房主已被踢出（owner_kicked）")
        return

    # ---- 普通成员：踢掉他一个人，房间继续 ----
    _log(
        "[KICK]",
        f"room={room.name}",
        f"by={member.label}",
        f"target={target.label}",
        f"is_host={is_requester_host}",
        f"is_owner={is_requester_owner}",
    )
    safe_event(
        "room.kick",
        room=lambda: room.name,
        by=lambda: member.label,
        client_id=lambda: member.client_id,
        target=lambda: target.label,
        target_client_id=lambda: target.client_id,
        owner=lambda: room.owner is target,
        was_host=lambda: room.host_member is target,
        is_host=lambda: is_requester_host,
        is_owner=lambda: is_requester_owner,
    )

    await kick_member(
        room, target, requester_label=member.label, reason="kicked"
    )

    # 名单变了，剩下的人要立刻看到（目标自己的清理路径还会再广播一次，
    # 重复广播是无害的，这里发一次保证顺序确定）。
    await broadcast_member_state(room)

    await member.send(f"OK KICK {target_id}\n")


async def reap_idle_members(room_list, now=None):
    """
    把静默超过 `MEMBER_IDLE_TIMEOUT` 的成员按"掉线"处理。

    抽成独立函数只是为了**可测**：`member_watchdog` 每
    `MEMBER_WATCHDOG_INTERVAL` 秒调一次，而测试要验"超时释放主机位"这条
    路径不能真等 15 秒。行为一字没改。

    返回被处理的 `(room, member)` 列表。只关 socket 和记原因，真正的清理
    （释放主机位 / 广播）仍走成员控制循环那条唯一路径。
    """
    now = time.monotonic() if now is None else now
    reaped = []

    for room in room_list:
        for member in list(room.members.values()):
            # ============================================================================
            #  ★ v3.9.18：座位宽限期（detached）内的成员**一律不许**被收走
            # ============================================================================
            #
            # 这一条必须排在**所有**收尸/超时判断之前（v3.9.17 把它排在了收尸
            # 分支**后面**，于是 detached 成员照样被收 —— 复核报告 3.1）。
            #
            # 后果是上轮那条 P1 换路复发：房主"掉线前抖了三下"（累积 3 次写超时）
            # + "掉线后进入座位宽限期"——这两个完全正常的事件连起来，房主就在
            # 身份**仍然有效**的时候被看门狗收走：主机位凭空消失、房间被销毁。
            # 而 `send_failures` 只在写**成功**时清零，进入 detached 时不清，
            # 所以旧账会被一直算下去。
            #
            # 见下面 detached 分支那段更长的说明（两条注释说的是同一条规则，
            # 这里是它的**执行点**，放最前是为了不被别的分支抢先）。
            if getattr(member, "detached", False):
                continue

            # ============================================================================
            #  ★ v3.9.17：写**长期**失败的成员必须能被回收
            # ============================================================================
            #
            # v3.9.16 里 `send()` 超时就置 `closed`，而这里对 `closed` 成员
            # `continue` —— 于是他**永久**占着房间名额（容量 / ROOM_FULL），
            # 却再也收不到任何广播，也没人来清他（僵尸成员）。
            #
            # 现在的顺序是：单次超时不判死（`send()` 只累计 `send_failures`），
            # 连续失败到 `MEMBER_SEND_FAIL_LIMIT` 才置 `closed`。置了之后由
            # **这里**接手收尾：关 socket -> 控制循环抛异常 -> 走唯一那条
            # 清理路径（释放主机位 + 广播）。这样"判死"和"收尸"是同一条链，
            # 不会再留下一具没人管的尸体。
            if getattr(member, "send_failures", 0) >= MEMBER_SEND_FAIL_LIMIT:
                _log(
                    "[MEMBER DEAD]",
                    f"room={room.name}",
                    f"user={member.label}",
                    f"send_failures={member.send_failures}",
                    "reason=kept failing to write; closing so the control "
                    "loop runs the normal cleanup",
                )

                member.removal_reason = "drop"
                member.closed = True

                if member.writer is not None:
                    try:
                        member.writer.close()
                    except Exception:
                        pass

                reaped.append((room, member))

                continue

            # 已经死了、且上面没接手的（例如控制循环自己置的 closed）：
            # 交给那条唯一的清理路径，这里不重复处理。
            if member.closed:
                continue

            # ------------------------------------------------------------------
            #  ★ 为什么 detached（座位宽限期）成员一律跳过 —— 上面已执行
            # ------------------------------------------------------------------
            #
            # 他的身份**仍然有效**（token 还在），只是控制连接断了。收走会把
            # `closed` 置 True，`host_member` 就认不出他，主机位会在身份还
            # 有效的时候凭空消失 —— 正是用户禁止的那件事。
            #
            # 他的"窗口"由 `hold_member_seat` **自己**按心跳周期等待（不是另
            # 设的 45 秒），等不到他自己会返回，由那条**还在跑**的清理路径销毁
            # 房间。光把 socket 关掉是没有用的 —— 那个 handler 早就退出了，
            # 没人会来收尾（会变成幽灵房）。
            if now - member.last_seen <= MEMBER_IDLE_TIMEOUT:
                continue

            _log(
                "[MEMBER TIMEOUT]",
                f"room={room.name}",
                f"user={member.label}",
                f"silent_for={round(now - member.last_seen, 1)}s",
                f"was_host={member.is_host}",
            )

            # Closing the socket makes the control loop raise, which runs the
            # same cleanup path as a clean exit -- so there is exactly one place
            # that knows how to remove a member.
            #
            # 原因先写好：清理路径据此记
            # `[HOST RELEASE] ... reason=timeout`。
            member.removal_reason = "timeout"
            member.closed = True

            if member.writer is not None:
                try:
                    member.writer.close()
                except Exception:
                    pass

            reaped.append((room, member))

    return reaped


async def reap_stale_waiters(room_list, now=None, max_age=None):
    """
    把"还没配对、又已经等了太久"的 PLAYER 等待者收掉（等待池的唯一看门狗）。

    ============================================================================
      ★ 用户 2026-09-29（第三轮）第 4 条：MC 多点几次刷新 -> 房间已满
    ============================================================================

    `waiting_players` 以前**没有任何过期机制**：`Waiter.created_at` 只在构造时
    赋值，全项目没有一个读点；`handle_player` 的 `await waiter.assigned.wait()`
    也没有超时。唯一能回收未配对 waiter 的路径是 `watch_player_socket` 的
    `reader.at_eof()` —— 那要求**客户端主动关 socket**。老客户端（或者被强杀的
    客户端）留下的半开 socket 于是能一直占着名额。

    这里按 `created_at` 给它一个真正的截止时间：到点就**只关 socket**，
    后续清理全部复用既有那条唯一路径（`watch_player_socket` 0.2 秒内看到
    at_eof -> `handle_player_socket_lost` 把它从池子里摘掉、唤醒 handler、
    广播 `conn=`），不另开一条清理实现。

    正常等待不会被误伤：客户端自己等待中继应答的上限是 30 秒，而这里的默认
    上限比它宽（`WAITER_MAX_WAIT_S`），所以"真的在排队等隧道"的连接不会被收走。

    返回被关掉的 waiter 列表（测试用）。
    """
    # 注意时钟：`Waiter.created_at` 是 `time.time()`（墙钟），不是 monotonic
    # —— 和成员看门狗用的那套不一样，别混。
    now = time.time() if now is None else now
    max_age = WAITER_MAX_WAIT_S if max_age is None else float(max_age)
    reaped = []

    for room in room_list:
        for waiter in list(getattr(room, "waiting_players", None) or ()):
            waiter_id = getattr(waiter, "session_id", None)

            if waiter_id:
                # 已经配上隧道了（在等房主进世界）：那一半由配对握手看门狗管。
                continue

            created = getattr(waiter, "created_at", None)

            if created is None:
                continue

            waited = now - float(created)

            if waited <= max_age:
                continue

            _log(
                "[WAITER TIMEOUT]",
                f"room={room.name}",
                f"client_id={getattr(waiter, 'client_id', '') or '-'}",
                f"waited={round(waited, 1)}s",
                "no tunnel came; closing the socket so the seat is returned",
            )

            writer = getattr(waiter, "writer", None)

            if writer is not None:
                # 幂等：已经关过的不要再记一次（这个看门狗每几秒就转一圈）。
                closing = False

                with contextlib.suppress(Exception):
                    closing = bool(writer.is_closing())

                if closing:
                    continue

                with contextlib.suppress(Exception):
                    writer.close()

            reaped.append(waiter)

    return reaped


async def member_watchdog():
    """
    Reap member control connections that went silent.

    A force-killed client (or a pulled cable) leaves a half-open TCP connection
    that looks alive forever, and a stranded host position is worse than a
    stranded room: nobody else can claim it, so the room becomes unusable while
    still looking fine in the panel.
    """
    while True:
        await asyncio.sleep(MEMBER_WATCHDOG_INTERVAL)

        # One bad room must not kill the watchdog: it is the only thing that
        # reaps a stranded host position, so it has to survive whatever it
        # finds. (It used to have no protection at all, on top of reading a
        # registry that was always empty -- see room_registry.py.)
        try:
            async with rooms_lock:
                room_list = list(rooms.values())

            await reap_idle_members(room_list)

        except asyncio.CancelledError:
            raise

        except Exception as e:  # noqa: BLE001
            _log("[MEMBER WATCHDOG ERROR]", f"{type(e).__name__}: {e}")


async def revoke_room(room, reason="管理员关闭了房间", grace=None):
    """
    Close a room: tell the clients, let THEM leave, then clean up after them.

    ============================================================================
     为什么不是"服务端一脚把连接踹断"
    ============================================================================

    The old version pushed ROOM REVOKED, then closed every socket itself. From
    the admin's side that looks fine (the room is gone). From the player's side
    it is a mess: their client is left believing it is still in a room that no
    longer exists, the UI keeps showing "已连接/在房间中", and the only way out
    is to press 退出房间 by hand before they can create or join anything again.
    Whatever the relay cuts abruptly can also leave rows behind, which the panel
    then lists as rooms nobody can join.

    So the order is now the OPPOSITE, and the clients do the closing:

      1. mark the room as closing (nobody new can join, and a JOIN that is
         already in flight gets a clear "房间已被管理员关闭" instead of a
         mysterious "房间不存在")
      2. push ROOM REVOKED <原因> to every member
      3. drop the Minecraft sessions -- THESE the relay must close itself, it
         owns those sockets and the players' tunnels are paired with the host's
      4. WAIT (bounded) for the members to close their own control connections,
         which is a normal, clean "I am leaving" from their side
      5. only then force-close whatever is left, and say how many that was
      6. delete the row and drop the room

    The wait is bounded by `grace` so an admin's request can never hang: the
    panel gets an answer either way, and the report says how many clients left
    by themselves and how many had to be cut off.

    Returns a small report so the caller can tell the admin what happened.
    """
    if room is None:
        return {
            "ok": False,
            "reason": "房间不存在",
            "members": 0,
            "sessions": 0,
        }

    grace = COOPERATIVE_CLOSE_GRACE if grace is None else max(0.0, float(grace))

    members = list(room.members.values())
    sessions = len(room.sessions)

    # Nobody may join while this is going on, and late joiners get a reason.
    room.closing = True
    room.closing_reason = reason

    _log(
        "[ROOM REVOKE]",
        f"room={room.name}",
        f"members={len(members)}",
        f"sessions={sessions}",
        f"grace={grace}s",
        f"reason={reason}",
    )

    # ---- 1. tell them ----
    # Best effort and bounded: a member whose socket is wedged must not be able
    # to make the admin's request hang.
    message = f"ROOM REVOKED {reason}\n"

    for member in members:
        try:
            await asyncio.wait_for(member.send(message), timeout=2.0)
        except Exception:
            pass

    # A short pause so the notification actually leaves the socket before any
    # FIN does. The relay runs on the same event loop as the sends, so this is
    # a real gap rather than a hopeful one.
    await asyncio.sleep(0.05)

    # ---- 2. drop the Minecraft sessions ----
    dropped = await drop_all_sessions(room, reason=reason)

    # ---- 3. wait for the clients to leave on their own ----
    #
    # Their control loop is blocked on readline(); a client that reacts to
    # ROOM REVOKED closes the socket (or sends LEAVE), and either way
    # remove_member() runs in the normal cleanup path. Polling member_count is
    # enough -- the exact mechanism does not matter, only that they are gone.
    deadline = time.monotonic() + grace
    waited = 0.0

    while room.member_count() > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        waited = grace - max(0.0, deadline - time.monotonic())

    left_voluntarily = len(members) - room.member_count()

    # ---- 4. force-close whoever is still here ----
    #
    # Same reasoning as before: closing the socket makes the control loop
    # return, which runs the ONE cleanup path that knows how to remove a member.
    remaining = list(room.members.values())
    forced = 0

    for member in remaining:
        member.closed = True
        # 关房 = 房间身份结束（不是"连接抖动"）：清理路径据此记
        # `room_closed`，重连探测（SESSION）也会回 `ROOM_CLOSED`。
        # 少了这一句，一个还带着有效账号/游客 token 的成员会被判成
        # "闪断"、进入座位宽限期，管理员的关房请求就会一直等到宽限结束。
        if getattr(member, "removal_reason", "") in ("", "drop"):
            member.removal_reason = "room_closed"

        if member.writer is not None:
            try:
                member.writer.close()
                forced += 1
            except Exception:
                pass

    # Give those closes a moment to run their cleanup, so the room is really
    # empty before it is destroyed.
    if forced:
        force_deadline = time.monotonic() + 2.0

        while room.member_count() > 0 and time.monotonic() < force_deadline:
            await asyncio.sleep(0.05)

    # ---- 5. drop the room ----
    await room.close(reason=reason)

    # Leave a short-lived note so a client that was a second too late (or a
    # panel page that still had the room on screen) is told "管理员关闭了房间"
    # rather than "房间不存在", which reads like a typo in the room name.
    with contextlib.suppress(Exception):
        from room_registry import note_closed

        note_closed(room.name, reason)

    _log(
        "[ROOM CLOSED]",
        f"room={room.name}",
        f"left_by_themselves={left_voluntarily}",
        f"forced={forced}",
        f"waited={round(waited, 1)}s",
    )

    return {
        "ok": True,
        "reason": reason,
        "members": len(members),
        "left_voluntarily": left_voluntarily,
        "forced": forced,
        "waited": round(waited, 1),
        "sessions": dropped if isinstance(dropped, int) else sessions,
    }


async def revoke_room_by_name(room_name, reason="管理员关闭了房间", grace=None):
    """
    Look a room up and destroy it. Safe to call for a room that is not there.

    接受两种写法：room token（房间 ID，面板用的就是它）或裸房间名。
    见 room_names.room_token —— 归一化是幂等的，所以两种写法命中同一个房间。

    Returns the same report as revoke_room(), plus whether the room existed.
    """
    token = room_names.room_token(room_name)
    room = rooms.get(token)

    if room is None:
        return {
            "ok": False,
            "reason": f"房间不存在（可能已经关闭）：{room_names.display_name(token)!r}",
            "members": 0,
            "sessions": 0,
            "existed": False,
        }

    report = await revoke_room(room, reason=reason, grace=grace)
    report["existed"] = True
    report["room"] = token
    report["name"] = room.display_name

    with contextlib.suppress(Exception):
        await drop_room_if_empty(token, room)

    return report


__all__ = [
    "RoomMember",
    "MembershipMixin",
    "member_control_loop",
    "broadcast_member_state",
    "broadcast_room_closed",
    "drop_all_sessions",
    "disconnect_game_connections",
    "HOST_LEFT_CODE",
    "HOST_NOT_READY_CODE",
    "CONN_MODE_CHANGED_CODE",
    # 连接模式的唯一读写口径 + `mode_reason=` 的取值表
    "MODE_CAUSES",
    "room_conn_mode",
    "room_mode_reason",
    "set_room_conn_mode",
    "started_client_ids",
    "started_token",
    "drop_member_sessions",
    "kick_member",
    "handle_member_kick",
    "handle_member_identity",
    "set_identity_hooks",
    # 座位宽限：连接断了 != 离开房间（身份/职位 vs 连接两套状态）
    "MEMBER_SEAT_GRACE",
    "member_identity_alive",
    "hold_member_seat",
    "host_state_fields",
    "host_client_id",
    "clean_host_name",
    "log_host_release",
    "send_kicked",
    "member_watchdog",
    "reap_idle_members",
    "revoke_room",
    "revoke_room_by_name",
    "COOPERATIVE_CLOSE_GRACE",
    "MEMBER_IDLE_TIMEOUT",
    "MEMBER_WATCHDOG_INTERVAL",
    "MAX_PLAYERS",
]
