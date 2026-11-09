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
            "默认 8 —— Minecraft 的「对局域网开放」本身最多接受 8 人，"
            "填大于 8 没有意义。这是【单个房间】的上限，不是所有房间加起来。"
            "注意：这是【服务器天花板】，只管新房间 —— "
            "每个房间的人数在「建房那一刻」就定下了，之后不能改。"
        ),
        "live_effect": (
            "改小之后，已经在房间里的玩家不会被踢出，"
            "但新的玩家进不来，直到人数降到新上限以下。"
        ),
        "minimum": 2,
        "maximum": 8,
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

    def get(self, name, default=None):
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

            out[name] = entry

        return out
