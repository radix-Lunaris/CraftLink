"""
MC-Link relay -- runtime settings.

============================================================================
 WHY THIS EXISTS
============================================================================

Some switches are operational, not deployment:

    * "必须登录才能建房/进房" (require_account)

Changing them used to mean editing docker-compose.yml and recreating the
container, which is slow and -- worse -- drops every connected player.

These settings therefore live in the DATABASE, are editable from the panel via
the admin API, and are applied **live**:

  * turning the switch OFF : existing sessions keep playing untouched; only
    NEW connections are treated differently.
  * turning it ON          : already authenticated players keep playing; a
    client that never logged in is asked to log in on its next connection.

Nothing here ever closes a socket. A configuration change must never be a
reason to kick somebody out of their Minecraft world.

The environment variable still works as the DEFAULT for the very first boot
(when the database has no stored value yet), so a fresh deployment behaves
exactly as configured in compose.
"""

import contextlib
import os
import time

# Setting names
SETTING_REQUIRE_ACCOUNT = "require_account"
SETTING_MAX_PLAYERS = "max_players"
SETTING_ALLOW_VERSION_MISMATCH = "allow_version_mismatch"

#: Everything the panel is allowed to toggle, with metadata for rendering.
KNOWN_SETTINGS = {
    SETTING_REQUIRE_ACCOUNT: {
        "type": "bool",
        "label": "必须登录才能建房 / 进房",
        "help": (
            "开启：房主和玩家都必须用账号登录（推荐）。"
            "关闭：任何人都能直接建房/进房，不需要账号。"
        ),
        "live_effect": (
            "开启后，已登录的玩家不受影响，继续游戏；"
            "没有登录的客户端会在下次连接时被要求登录。"
        ),
    },
    SETTING_MAX_PLAYERS: {
        "type": "int",
        "label": "单个房间的人数上限",
        "help": (
            "一个房间里最多能有多少人（含房主）。"
            "默认 8，可设置为 2-64 人（含房主）。"
            "这是通用联机工具的房间上限，实际游戏可接入人数还取决于游戏服务端设置。"
            "这是【单个房间】的上限，不是所有房间加起来。"
            "注意：这是【服务器天花板】，房间实际人数同时受建房容量与此上限约束。"
            "每个房间的人数在「建房那一刻」就定下了，之后不能改。"
        ),
        "live_effect": (
            "改小之后，已经在房间里的玩家不会被踢出，"
            "但新的玩家进不来，直到人数降到新上限以下。"
        ),
        "minimum": 2,
        "maximum": 64,
        "default": 8,
    },
    # ============================================================
    #  【开发调试专用】允许版本不一致的客户端连接
    # ============================================================
    #
    # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #  这【不是】"兼容老版本"的路径。
    # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #
    # 中继和客户端**必须同版本**（AGENTS.md 规则 1）。这个开关存在的唯一
    # 理由，是开发/联调时手边恰好只有另一个版本的客户端（例如在改客户端、
    # exe 还没来得及重打），需要一个临时口子把连接放进来。
    #
    # 它默认关闭，而且只应由管理权限组在排查问题时临时打开、用完立刻关掉：
    # 放行不同版本的客户端**可能造成未知 BUG**（字段错位、新协议行不被识别、
    # 房间状态错乱），这些 BUG 在新版本里可能已经被修掉，也可能还没被我们
    # 发现。
    #
    # 未来的任何开发都按"版本必须一致"来做：
    #   * 不要为了这个开关保留任何老代码分支；
    #   * 不要新增"兼容旧版字段"的逻辑；
    #   * 需要临时连一下，就在面板上开这个开关，测完关掉。
    SETTING_ALLOW_VERSION_MISMATCH: {
        "type": "bool",
        "label": "允许不同版本的客户端连接（开发调试，默认关闭）",
        "help": (
            "【开发调试功能，不是给玩家用的】默认关闭。"
            "关闭时：客户端与服务器版本必须完全一致，不一致会被拒绝"
            "（ERROR VERSION_MISMATCH）—— 这是正常、推荐的配置。"
            "开启后：与当前服务器版本不同的客户端也能连接，"
            "【可能造成未知 BUG】（协议字段错位、房间状态错乱等）。"
            "这不是「兼容老版本」的路径：任何正式开发都要求客户端与"
            "服务器版本一致，不要为它保留老代码分支。"
            "只有管理员可以修改，用完请立刻关掉。"
        ),
        "live_effect": (
            "立即生效：改的是**之后的新连接**，已经连上的玩家不受影响。"
        ),
        "default": False,
    },
}


def parse_bool(value, default=False):
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")


def parse_int(value, minimum, maximum, default):
    """
    Coerce a setting to an int inside [minimum, maximum].

    Out of range is CLAMPED to the nearest bound rather than rejected. A
    setting that refuses to load leaves the relay without a value it needs on
    a path where there is nobody to report the problem to; a clamped value is
    always usable and the panel shows what it actually became.
    """
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return int(default)

    return max(int(minimum), min(int(maximum), number))


#: ★ v3.9.16：「允许不同版本客户端连接」的**自动到期**时间（秒）。
#:
#: 分析报告 P1：这个开关一旦被留在开启状态，版本门就整体失效，而唯一的痕迹
#: 只是日志里一行 `[VERSION BYPASS]`。它是调试用的临时口子，却没有任何东西
#: 提醒"你昨天开的那个还开着"。
#:
#: 所以开启时记一个到期时刻，到点**自动回到关闭**：
#:   * 内存里（`_bypass_until`）管生效 —— 到期那一刻起放行停止；
#:   * 到期不会去改数据库（避免"没人操作时数据库被写"），但面板会显示
#:     "已过期"，下一次有人动这个开关时顺手落盘。
#:
#: 设成 0 = 不过期（v3.9.15 的行为）。
VERSION_BYPASS_TTL_S = int(
    os.environ.get("MCLINK_VERSION_BYPASS_TTL_S", "1800") or 0
)


class SettingsService:
    """
    Reads/writes the runtime settings and caches them in memory.

    The cache means the hot path (authorising a connection) never touches the
    database: it reads one boolean out of a dict.
    """

    def __init__(self, store, defaults=None):
        self.store = store
        self.defaults = dict(defaults or {})
        self._cache = {}
        self.loaded = False
        self.on_change = None

        #: 「允许版本不一致」的到期时刻（monotonic）。None = 没开着。
        self._bypass_until = None
        #: 到期时补记的那一行日志只打一次。
        self._bypass_expiry_logged = False

    # --------------------------------------------------------

    async def load(self):
        """
        Load stored values, falling back to the configured defaults.

        Called once at startup. A database problem is not fatal: the defaults
        are used and the relay keeps serving.
        """
        try:
            stored = await self.store.all_settings()
        except Exception:
            stored = {}

        self._apply(stored)

        return dict(self._cache)

    # --------------------------------------------------------

    def load_defaults(self):
        """
        Populate the cache from the configured defaults, without the database.

        Used when the startup read is ABANDONED because it took too long: the
        relay must still know `max_players` and `require_account`, and those
        have perfectly good environment-variable defaults. Leaving the cache
        empty would work by luck (get() falls back), but the panel and the log
        line both read the cache, and "the value that was actually applied"
        should never be a guess.
        """
        self._apply({})

        return dict(self._cache)

    # --------------------------------------------------------

    def _apply(self, stored):
        """Fill the cache from `stored`, using the defaults for anything absent."""
        self._cache = {}

        for name, meta in KNOWN_SETTINGS.items():
            if name in stored:
                value = stored[name]
            else:
                value = self.defaults.get(name)

            if meta["type"] == "bool":
                self._cache[name] = parse_bool(
                    value, parse_bool(self.defaults.get(name), False)
                )
            elif meta["type"] == "int":
                self._cache[name] = parse_int(
                    value,
                    meta.get("minimum", 0),
                    meta.get("maximum", 1000000),
                    int(meta.get("default") or 0),
                )
            else:
                self._cache[name] = value

        self.loaded = True

    # --------------------------------------------------------

    # --------------------------------------------------------
    # v3.9.16：版本门的自动到期
    # --------------------------------------------------------

    def version_bypass_active(self):
        """
        「允许不同版本客户端连接」**此刻**是否真的生效（含到期判定）。

        ============================================================================
          为什么单列一个函数
        ============================================================================
        这个开关的危险性在于"被忘了关"：它一旦开着，版本门整体失效，而
        痕迹只有日志里的一行 `[VERSION BYPASS]`（分析报告 P1）。

        所以"开着"不再等于"缓存里是 True"——还要看那个到期时刻。到期之后
        这里立刻返回 False，**不需要任何人去点一下**。
        """
        if not self._cache.get(SETTING_ALLOW_VERSION_MISMATCH, False):
            return False

        if VERSION_BYPASS_TTL_S <= 0:
            return True

        until = self._bypass_until

        if until is None:
            # 开关是开着的，但没有到期时刻：说明它是**启动前**就在数据库里
            # 存着的（老配置 / 直接改库）。给一次宽限，从现在起算。
            self._bypass_until = time.monotonic() + VERSION_BYPASS_TTL_S

            return True

        if time.monotonic() >= until:
            # 到期了。只改内存，不写数据库 —— "没有人操作时偷偷落盘"比
            # "数据库里还写着开"更让人看不懂。面板会显示"已过期"。
            self._cache[SETTING_ALLOW_VERSION_MISMATCH] = False

            if not self._bypass_expiry_logged:
                self._bypass_expiry_logged = True

                with contextlib.suppress(Exception):
                    print(
                        "[VERSION BYPASS] expired after "
                        f"{VERSION_BYPASS_TTL_S}s -- the version gate is "
                        "enforced again (no action needed)",
                        flush=True,
                    )

            return False

        return True

    def version_bypass_state(self):
        """
        这个开关的完整状态，给面板用：`{"active", "expires_in_s", "ttl_s"}`。

        `expires_in_s` 为 None = 没开着 / 不过期。
        """
        ttl = VERSION_BYPASS_TTL_S

        if not self._cache.get(SETTING_ALLOW_VERSION_MISMATCH, False):
            return {"active": False, "expires_in_s": None, "ttl_s": ttl}

        if ttl <= 0:
            return {"active": True, "expires_in_s": None, "ttl_s": 0}

        until = self._bypass_until

        if until is None:
            return {"active": True, "expires_in_s": ttl, "ttl_s": ttl}

        left = until - time.monotonic()

        return {
            "active": left > 0,
            "expires_in_s": max(0, int(left)),
            "ttl_s": ttl,
        }

    def get(self, name, default=None):
        # ★ v3.9.16：版本门走带到期判定的那一条，其余照旧。
        if name == SETTING_ALLOW_VERSION_MISMATCH:
            return self.version_bypass_active()

        if name in self._cache:
            return self._cache[name]

        return self.defaults.get(name, default)

    def get_bool(self, name, default=False):
        return parse_bool(self.get(name), default)

    def get_int(self, name, default=0, minimum=0, maximum=1000000):
        return parse_int(self.get(name), minimum, maximum, default)

    # --------------------------------------------------------

    async def set(self, name, value, actor="panel"):
        """
        Change a setting and persist it. Applies to new connections instantly.

        Returns (ok, message).
        """
        if name not in KNOWN_SETTINGS:
            return False, f"未知的设置项：{name}"

        meta = KNOWN_SETTINGS[name]

        if meta["type"] == "bool":
            parsed = parse_bool(value)
        elif meta["type"] == "int":
            minimum = int(meta.get("minimum", 0))
            maximum = int(meta.get("maximum", 1000000))

            try:
                requested = int(str(value).strip())
            except (TypeError, ValueError):
                return False, f"{meta['label']}必须是数字"

            if not (minimum <= requested <= maximum):
                return False, (
                    f"{meta['label']}只能是 {minimum}-{maximum}，"
                    f"你填的是 {requested}"
                )

            parsed = requested
        else:
            parsed = str(value)

        # Nothing to do. Saying so is better than reporting a change that did
        # not happen.
        if parsed == self._cache.get(name):
            return True, "没有变化"

        old = self._cache.get(name)

        self._cache[name] = parsed

        # ★ v3.9.16：版本门被打开 -> 记一个到期时刻；被关掉 -> 清掉。
        if name == SETTING_ALLOW_VERSION_MISMATCH:
            if parsed is True:
                self._bypass_until = (
                    time.monotonic() + VERSION_BYPASS_TTL_S
                    if VERSION_BYPASS_TTL_S > 0 else None
                )
                self._bypass_expiry_logged = False
            else:
                self._bypass_until = None
                self._bypass_expiry_logged = False

        try:
            await self.store.set_setting(
                name, "1" if parsed is True else (
                    "0" if parsed is False else str(parsed)
                )
            )
        except Exception as e:
            self._cache[name] = old
            return False, f"保存失败（数据库不可用）：{e}"

        try:
            await self.store.add_event(
                "setting_changed",
                actor=actor,
                detail=f"{name}: {old} -> {parsed}",
            )
        except Exception:
            pass

        if self.on_change is not None:
            try:
                self.on_change(name, parsed, old)
            except Exception:
                pass

        return True, "设置已保存并立即生效（不会断开在线玩家）"

    # --------------------------------------------------------

    def snapshot(self):
        """Everything the panel needs to render the switches."""
        # ★ v3.9.16：先把版本门的到期判定跑一遍，这样下面读到的 `value` 与
        # `version_bypass` 是**同一时刻**的结论，不会出现"写着已开启、
        # 但其实已经过期"这种自相矛盾的面板。
        with contextlib.suppress(Exception):
            self.version_bypass_active()

        out = {}

        for name, meta in KNOWN_SETTINGS.items():
            entry = {
                "value": self._cache.get(name, self.defaults.get(name)),
                "type": meta["type"],
                "label": meta["label"],
                "help": meta["help"],
                "live_effect": meta["live_effect"],
            }

            # Bounds and default travel with the value: the panel renders a
            # number box from them instead of hardcoding 2 and 8 a second
            # time, which is how the two ends start disagreeing.
            for key in ("minimum", "maximum", "default"):
                if key in meta:
                    entry[key] = meta[key]

            # ★ v3.9.16：版本门多给一份"还剩多久到期"，面板据此显示倒计时，
            # 而不是让管理员记住自己昨天开过。
            if name == SETTING_ALLOW_VERSION_MISMATCH:
                entry["version_bypass"] = self.version_bypass_state()

            out[name] = entry

        return out
