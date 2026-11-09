"""
MC-Link Relay Server -- configuration.

All values can be overridden with environment variables so the same image can
be reused without editing code.
"""

import os


def _env_str(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def env_flag(name, default=False):
    """
    `1/true/yes/on` -> True，`0/false/no/off` -> False，没设置/看不懂 -> default。

    为什么单独有它：`MCLINK_*` 里有一批"开关"型配置，以前散在各处用
    `os.environ.get(...) == "1"` 之类的写法，写错一个字符（比如 `True`）就
    静默变成关闭 —— 而开关被静默关掉正是最难查的那类事故。
    """
    raw = str(os.environ.get(name, "") or "").strip().lower()

    if raw in ("1", "true", "yes", "on"):
        return True

    if raw in ("0", "false", "no", "off"):
        return False

    return bool(default)


# ------------------------------------------------------------
# Listen
# ------------------------------------------------------------

HOST = _env_str("MCLINK_BIND", "0.0.0.0")
PORT = _env_int("MCLINK_PORT", 19100)

# ------------------------------------------------------------
# Limits
# ------------------------------------------------------------

# Maximum simultaneous Minecraft players per room (tunnel sessions).
MAX_PLAYERS = _env_int("MCLINK_MAX_PLAYERS", 8)

# Room name / username length limits (security requirement).
#
# 房间名（玩家看到的那个）可以是中文、英文、空格、emoji —— 任何东西。
# 但它在线上协议里必须是一个"没有空格、没有斜杠"的记号，所以客户端会先做
# percent-encode，中继拿到的那个编码后的字符串才叫 room token（房间 ID）。
# 因此限制分两个：
#   MAX_ROOM_LEN      —— 显示名的字符数上限（1 个汉字算 1 个）
#   MAX_ROOM_TOKEN_LEN—— 编码后允许的长度（1 个汉字 = 9 个字符，留够余量）
MAX_ROOM_LEN = _env_int("MCLINK_MAX_ROOM_LEN", 64)
MAX_ROOM_TOKEN_LEN = _env_int(
    "MCLINK_MAX_ROOM_TOKEN_LEN", MAX_ROOM_LEN * 9 + 16
)
MAX_USERNAME_LEN = _env_int("MCLINK_MAX_USERNAME_LEN", 32)

# First-line handshake timeout (seconds).
HANDSHAKE_TIMEOUT = _env_int("MCLINK_HANDSHAKE_TIMEOUT", 10)

# ------------------------------------------------------------
# 配对握手时限（这一组是「幽灵会话」的疫苗）
# ------------------------------------------------------------
#
# 一次配对（PLAYER <-> HOST_TUNNEL）要经历：
#     配对上 -> 房主客户端连本机 Minecraft -> 回 READY -> 玩家回 CONNECTED
# 才真的算"进入世界"。
#
# 这段过程以前**没有超时**，所以只要有一次卡住，那一对就永久占着房间名额：
#   1) 界面人数虚高（用户看到「房间 3 人却显示已加入世界 9 人」）；
#   2) 名额被幽灵占满 -> 后面的人配不上对 -> 游戏直接连不上。
#
# 所以给整段握手一个上限，到点还没成，就整对拆掉、名额立刻归还。
PAIR_HANDSHAKE_TIMEOUT = _env_int("MCLINK_PAIR_HANDSHAKE_TIMEOUT", 20)

# 隧道单方向等 READY 的上限（比总时限短，先把明显卡死的隧道放掉）
PAIR_READY_TIMEOUT = _env_int("MCLINK_PAIR_READY_TIMEOUT", 15)

# ------------------------------------------------------------
# 服务端日志（用户：「服务端日志在哪里啊？找不到一点」）
# ------------------------------------------------------------
#
# 以前日志只有 print()，只落在容器标准输出，翻不了历史、没法下载。
# 现在同时写文件 + 留内存环形缓冲，面板可以直接下载。
#
# LOG_DIR 默认 /app/logs（compose 里挂成数据卷，重建容器不丢）；
# 本地跑（没有这个目录的写权限）时自动退化成"只有内存缓冲"，绝不报错。
LOG_DIR = _env_str("MCLINK_LOG_DIR", "/app/logs")
LOG_FILE_NAME = _env_str("MCLINK_LOG_FILE", "relay.log")

#: 单个日志文件多大就轮转（默认 5MB）
LOG_FILE_MAX_BYTES = _env_int("MCLINK_LOG_MAX_BYTES", 5 * 1024 * 1024)

#: 保留几个历史文件（relay.log.1 … relay.log.N）
LOG_KEEP_FILES = _env_int("MCLINK_LOG_KEEP_FILES", 3)

#: 内存里保留多少行（面板"最近 N 行"从这里取）
LOG_RING_LINES = _env_int("MCLINK_LOG_RING_LINES", 4000)

#: 一次导出的正文上限（默认 3MB，防止把面板撑爆）
LOG_EXPORT_MAX_BYTES = _env_int("MCLINK_LOG_EXPORT_MAX_BYTES", 3 * 1024 * 1024)


# ------------------------------------------------------------
# Runtime
# ------------------------------------------------------------

# Socket read buffer. DO NOT lower this too much: Minecraft world
# transfers (chunk streaming) go through here.
BUFFER_SIZE = 64 * 1024

# asyncio StreamReader limit. Same value the verified build used.
STREAM_LIMIT = 1024 * 1024

# Periodic console status output.
STATUS_INTERVAL = _env_int("MCLINK_STATUS_INTERVAL", 10)

# Path of the machine-readable status file.
#
# The file is written atomically every STATUS_INTERVAL seconds. It is the
# data source for the future admin panel (V2) and for `docker healthcheck`.
STATUS_FILE = _env_str("MCLINK_STATUS_FILE", "/app/status/status.json")

# Ping answers on the host CONTROL connection.
ENABLE_CONTROL_PING = _env_str("MCLINK_CONTROL_PING", "1") not in ("0", "false", "False")

# Push unsolicited INFO lines on the host CONTROL connection.
ENABLE_CONTROL_INFO = _env_str("MCLINK_CONTROL_INFO", "1") not in ("0", "false", "False")

# Verbose per-connection logging (handshakes, DELETE decisions, host messages).
VERBOSE = _env_str("MCLINK_VERBOSE", "1") not in ("0", "false", "False")

# ------------------------------------------------------------
# Host heartbeat (V2.1) -- fixes the "stuck room" failure mode
# ------------------------------------------------------------
#
# PROBLEM: if the host's control connection dies UNCLEANLY (cable pulled,
# process killed, wifi switched), TCP never sends a FIN. The relay keeps
# room.host_writer pointing at a dead socket, so the room looks "hosted"
# forever:
#   * the room is refused to the host with ROOM_ALREADY_HAS_HOST
#   * its own tunnels see NO_HOST
#   * the panel shows a room nobody can use
#   * DELETE cannot clear it, because the stale writer still counts as alive
#
# The client already sends "PING <ts>" on the control channel every 3 seconds
# (for latency). The relay now treats those bytes as a heartbeat too: no
# traffic for HOST_IDLE_TIMEOUT seconds => declare the host dead and destroy
# the room exactly as if it had closed cleanly.
#
# Was 25 s, which meant a force-killed client (or a pulled network cable) kept
# showing up in the panel as a live room for up to half a minute. The client
# pings every 3 s, so 12 s tolerates THREE consecutive missed pings -- enough
# for a brief stall on a mobile link, while still clearing a dead host in
# about a quarter of the old time.
#
# Note this only affects an UNGRACEFUL exit. A clean exit (closing the window,
# or an explicit LOGOUT) closes the control socket, and the relay tears the
# room down immediately without waiting for any timeout.
#
# ADDITIVE: this only watches the existing control channel. The Minecraft
# data path is untouched.
#
# ----------------------------------------------------------------------------
#  ★ v3.9.16：12 -> 25，因为"4 次心跳判死"在弱网下会**误杀**
# ----------------------------------------------------------------------------
#
# 客户端心跳 3 秒一次，12 秒 = 4 次丢失就判定 host 死亡并**把整个房间的游戏
# 连接全部断开**。手机热点 / 电梯 / 跨运营商那几秒的抖动足够丢掉 4 个包，
# 于是"host 明明还在，全房被踢下线" —— 这和被报的那个 bug（host 走了没人
# 断开）是同一个场景的正反面，用户只会觉得"这软件时好时坏"。
#
# 25 秒 ≈ 丢 8 次心跳才判死：正常的一两秒抖动完全穿不过去，真掉线（进程没了
# / 拔网线）也就多等十几秒 —— 而真掉线时客户端自己会先报"连接已断开"，那
# 条路不走这个超时。
#
# 想调回去就设 `MCLINK_HOST_IDLE_TIMEOUT=12`，不需要改代码。
HOST_IDLE_TIMEOUT = _env_int("MCLINK_HOST_IDLE_TIMEOUT", 25)

# How often the watchdog checks (seconds). Proportional to the timeout above:
# checking every 5 s against a 12 s budget made the real detection time
# anywhere between 12 and 17 s.
HOST_WATCHDOG_INTERVAL = _env_int("MCLINK_HOST_WATCHDOG_INTERVAL", 3)

# A tunnel that has been idle (paired with a player but no data) for this long
# is considered dead. 0 disables the check.
TUNNEL_IDLE_TIMEOUT = _env_int("MCLINK_TUNNEL_IDLE_TIMEOUT", 0)

# ------------------------------------------------------------
# Database (V2)
# ------------------------------------------------------------
#
# The relay connects to MySQL by CONTAINER NAME over the shared Docker
# network, e.g. MCLINK_DB_HOST=web-mysql80. 127.0.0.1:3306 on the host is NOT
# reachable from inside a container unless the relay uses host networking.
#
# MCLINK_DB_ENABLED=0 keeps everything in memory (no accounts, no panel data).
# The Minecraft relay itself never depends on the database being up.

DB_ENABLED = _env_str("MCLINK_DB_ENABLED", "1") not in ("0", "false", "False")

DB_HOST = _env_str("MCLINK_DB_HOST", "web-mysql80")
DB_PORT = _env_int("MCLINK_DB_PORT", 3306)
DB_NAME = _env_str("MCLINK_DB_NAME", "mclink")
DB_USER = _env_str("MCLINK_DB_USER", "mclink")
DB_PASSWORD = _env_str("MCLINK_DB_PASSWORD", "")

DB_POOL_SIZE = _env_int("MCLINK_DB_POOL_SIZE", 4)
DB_CONNECT_TIMEOUT = _env_int("MCLINK_DB_CONNECT_TIMEOUT", 8)

# ------------------------------------------------------------
# 启动时限：数据库卡住也不能拖住中继（这一组是「中继起不来」的疫苗）
# ------------------------------------------------------------
#
# 现场故障（2026-09-15）：中继日志停在
#
#     [DB] connecting to mysql://mclink@web-mysql80:3306/mclink
#
# 之后什么都没有，容器一直 unhealthy —— 因为中继在
# `await setup_services()` 里等数据库，而这一步在监听 19100 **之前**。
# 也就是说：数据库慢一下，全世界都进不来（连"不依赖数据库"的原始 TCP
# 转发也一起被拖死），而这恰恰是最不该发生的。
#
# 现在给启动阶段的每一步一个硬上限：
#   DB_BOOT_TIMEOUT  —— connect / health / 建表 各自的等待上限
#   DB_QUERY_TIMEOUT —— 单条 SQL 在 socket 上的读写上限（0 = 不限制）
#
# 超时不是"启动失败"：超时只意味着降级（面板/账号不可用），
# 中继照样监听、照样转发原始字节。
DB_BOOT_TIMEOUT = _env_int("MCLINK_DB_BOOT_TIMEOUT", 20)
DB_QUERY_TIMEOUT = _env_int("MCLINK_DB_QUERY_TIMEOUT", 30)

# How often the live room mirror is pushed to the database (seconds).
DB_SYNC_INTERVAL = _env_int("MCLINK_DB_SYNC_INTERVAL", 5)

# Rooms not refreshed for this long are considered stale and deleted from the
# panel's view (safety net; the normal path deletes immediately).
DB_STALE_ROOM_SECONDS = _env_int("MCLINK_DB_STALE_ROOM_SECONDS", 60)

# ------------------------------------------------------------
# Authentication (V2)
# ------------------------------------------------------------
#
# When REQUIRE_ACCOUNT is on, HOST and PLAYER connections must carry a valid
# token obtained from the LOGIN command. A connection WITHOUT a token is still
# parsed the same way as before (additive protocol), it is simply rejected
# with ERROR AUTH_REQUIRED -- the frozen handshake itself is untouched.

REQUIRE_ACCOUNT = _env_str("MCLINK_REQUIRE_ACCOUNT", "1") not in (
    "0",
    "false",
    "False",
)

# Token lifetime in seconds.
TOKEN_TTL = _env_int("MCLINK_TOKEN_TTL", 12 * 3600)

# Failed login attempts allowed per IP per minute.
LOGIN_MAX_ATTEMPTS = _env_int("MCLINK_LOGIN_MAX_ATTEMPTS", 8)
LOGIN_WINDOW = _env_int("MCLINK_LOGIN_WINDOW", 60)

# Optional first admin account, "<username>:<password>".
#
# Creates the account if it does not exist, using the same PBKDF2 format as
# the PHP panel. Leave empty in production once your admin exists.
BOOTSTRAP_ADMIN = _env_str("MCLINK_BOOTSTRAP_ADMIN", "")

# ------------------------------------------------------------
# Admin API (V2) -- consumed by the PHP web panel
# ------------------------------------------------------------
#
# Bound to 127.0.0.1 inside the container and published ONLY on the internal
# Docker network (the compose file uses `expose`, never `ports`). The PHP
# panel reaches it as http://mc-relay:19101.

ADMIN_API_ENABLED = _env_str("MCLINK_ADMIN_API_ENABLED", "1") not in (
    "0",
    "false",
    "False",
)

ADMIN_API_HOST = _env_str("MCLINK_ADMIN_API_HOST", "0.0.0.0")
ADMIN_API_PORT = _env_int("MCLINK_ADMIN_API_PORT", 19101)

# Shared secret. The panel sends it as `Authorization: Bearer <token>`.
# MUST be changed for a real deployment.
ADMIN_API_TOKEN = _env_str("MCLINK_ADMIN_API_TOKEN", "")

# ------------------------------------------------------------
# Room lifecycle (V2)
# ------------------------------------------------------------
#
# A room with 0 players is deleted automatically this many seconds after the
# last player leaves. 0 = delete as soon as it becomes empty.
ROOM_EMPTY_GRACE = _env_int("MCLINK_ROOM_EMPTY_GRACE", 0)

# ------------------------------------------------------------
# Built-in STUN server
# ------------------------------------------------------------
#
# This is MCLanP2P's STUN responder (see server/stun_server.py, a verbatim
# port). CraftLink's own earlier implementation was removed on the project
# owner's instruction: his is the one verified working on real NAT3/NAT4
# networks, and keeping a second half-finished one alongside it only creates
# drift.
#
# TWO PORTS, AND WHY
#
#   Detecting a symmetric NAT needs two DIFFERENT destinations to compare
#   mappings against, and a single-IP VPS can only offer that by varying the
#   PORT. Hence a consecutive pair: 3478 and 3479 (STUN_PORT + 1), which is
#   the same layout his project deploys.
#
#   BOTH must be published in docker-compose AND opened in the cloud security
#   group. Missing one is silent: the client just sees one destination less
#   and cannot tell a cone NAT from a symmetric one.
#
# Note the ports are deliberately NOT the relay's TCP port number. Earlier
# CraftLink builds put STUN on UDP 19100 (same number as the TCP relay) and
# invented 19103 for the second port; the standard pair below is what the
# ported client and the ported server both expect, and what his firewall
# script opens.
#
# MCLINK_STUN_ENABLED=0 disables STUN entirely and the relay behaves exactly
# as before.
STUN_ENABLED = _env_str("MCLINK_STUN_ENABLED", "1") not in (
    "0",
    "false",
    "False",
)

STUN_HOST = _env_str("MCLINK_STUN_BIND", "0.0.0.0")

#: The primary STUN port.
STUN_PORT = _env_int("MCLINK_STUN_PORT", 3478)

#: The second destination. Defaults to STUN_PORT + 1, exactly as the ported
#: project does (`STUN_PORT2 = STUN_PORT + 1`), because the two must be
#: adjacent for the "same IP, different port" trick to be the documented one.
STUN_PORT2 = _env_int("MCLINK_STUN_PORT2", STUN_PORT + 1 if STUN_PORT else 0)

#: Every STUN port to bind. A single source of truth for the server, the
#: compose file and the deploy precheck.
STUN_PORTS = tuple(p for p in (STUN_PORT, STUN_PORT2) if p)

#: The pair as "3478:3479", for logs and the deploy checklist.
STUN_PORT_RANGE = (
    f"{STUN_PORTS[0]}:{STUN_PORTS[-1]}" if len(STUN_PORTS) > 1
    else str(STUN_PORTS[0]) if STUN_PORTS else ""
)

# ------------------------------------------------------------
# Protocol version
# ------------------------------------------------------------
#
# Reported in the LOGIN reply. The client uses it to decide which features are
# available; the frozen handshake is not affected.
PROTOCOL_VERSION = _env_int("MCLINK_PROTOCOL_VERSION", 2)



# ============================================================================
#  服务端版本号（版本门用）
# ============================================================================
#
# 客户端和服务端**必须同版本**：不同版本之间的握手/字段约定可能不一致，而
# 报错现场往往是一堆看不懂的怪现象。所以：
#
#   * `scripts/sync-deploy.py` 会把客户端的 APP_VERSION 写进 deploy/server/VERSION；
#   * 中继启动时读它，读不到就是 ""（**门关**，方便本地/测试环境）；
#   * 门开着时，客户端的握手行必须带 `ver=<APP_VERSION>`，不一致直接拒绝，
#     并明确回 `ERROR VERSION_MISMATCH server=... client=...`。
#
# 环境变量 MCLINK_VERSION 优先，便于测试与临时覆盖。
def _read_relay_version():
    env = _env_str("MCLINK_VERSION", "")

    if env:
        return env.strip()

    try:
        import os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        with open(_os.path.join(here, "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


RELAY_VERSION = _read_relay_version()
