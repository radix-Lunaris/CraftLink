"""
MC-Link client -- settings / persistence.

Stores a small JSON file next to the user's profile (or next to the EXE when
running from source) so the Client ID and the last used values survive a
restart.
"""

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field

from ..core import protocol as proto

APP_NAME = "CraftLink"
APP_NAME_ZH = "方块联机"
APP_VERSION = "5.1.1"
APP_REVISION = "network-r2"
#: 版本不一致时让用户去下载新版的地址（HTTPS，443 端口）。
#:
#: 只出现在「需要更新」那个弹窗里。放在 settings 里而不是写死在界面代码里，
#: 是为了以后换域名时只改一处（弹窗、日志、以后的"检查更新"都读它）。
from .defaults import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT, UPDATE_URL, UPDATE_SITE_TEXT, update_url

#: 配置目录名。**永远不要跟着 APP_NAME 改**。
#:
#: 这个目录里存着 client_id（账号绑定的身份）和加密保存的密码。改目录名 =
#: 对老用户来说"换了台机器"：client_id 变了、记住的密码没了、服务器那边会认为
#: 是新客户端。软件改个显示名字是好事情，但配置文件必须待在原地。
APP_DIR_NAME = "MC-Link"

CONFIG_FILENAME = "mclink_config.json"

#: 允许的界面语言代码（和 gui/i18n.py 的 LANGUAGES 对应）。
UI_LANGUAGES = ("zh", "en", "ja")

#: 允许的主题名。**必须和 gui/theme.py 的 THEME_LABELS 一致**。
#:
#: 这里不能直接 import gui.theme：config 被 gui 依赖，反过来 import 会成环
#: （gui.theme 又要读 config）。所以两边各存一份，靠测试
#: （tests/gui_appearance_test.py 的"主题名单两处一致"）钉住不许漂移。
UI_THEMES = ("dark", "light", "system")


def build_stamp():
    """
    When THIS copy was built, as "YYYY-MM-DD HH:MM".

    Taken from the file's own timestamp rather than a hand-written constant:
    a date literal in the source goes stale the moment somebody forgets to bump
    it, and then it lies in exactly the situation it exists for. For the frozen
    EXE that file is the exe itself, so the answer is always true.

    This exists because "你跑的到底是哪个 exe" came up while a fix looked like
    it had not worked -- the fix was in a build the user did not have yet.
    """
    import datetime

    target = sys.executable if getattr(sys, "frozen", False) else __file__

    try:
        stamp = os.path.getmtime(target)
    except OSError:
        return "unknown"

    return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def version_line():
    """
    窗口标题 / 日志首行用的那一行。

    软件名（CraftLink）**不跟随语言**：中文界面、英文界面、日文界面下它都一样。
    后面那截说明文字才翻译（见 gui/i18n.py 的 "Minecraft 联机"）。
    """
    return f"{APP_NAME} v{APP_VERSION} [{APP_REVISION}] (构建 {build_stamp()})"


def config_dir():
    """
    Per-user writable directory.

    Frozen EXE    -> %APPDATA%\\MC-Link
    Docker/Linux  -> ~/.config/MC-Link

    目录名用的是 APP_DIR_NAME（= 老的 "MC-Link"），不是显示名 CraftLink —— 见
    APP_DIR_NAME 上面的说明：改它等于让所有老用户丢掉身份和记住的密码。
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_DIR_NAME)

    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, APP_DIR_NAME)


def fallback_dir():
    """
    Used when the per-user directory is not writable (locked down machine,
    read-only roaming profile, sandbox, ...). A JSON config is not worth
    failing over.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    return os.getcwd()


def config_path():
    return os.path.join(config_dir(), CONFIG_FILENAME)


def fallback_config_path():
    return os.path.join(fallback_dir(), CONFIG_FILENAME)


def _candidate_paths():
    """Preferred first, fallback second."""
    primary = config_path()
    secondary = fallback_config_path()

    return [primary] if os.path.abspath(primary) == os.path.abspath(secondary) else [
        primary,
        secondary,
    ]


def log_dir():
    path = os.path.join(config_dir(), "logs")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        path = os.path.join(fallback_dir(), "logs")
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            return fallback_dir()
    return path


def new_client_id():
    """A stable random identifier. Display only -- never a secret."""
    return uuid.uuid4().hex[:16]


@dataclass
class ClientConfig:
    # ---- connection ----
    server_host: str = DEFAULT_SERVER_HOST
    server_port: int = DEFAULT_SERVER_PORT

    update_url: str = UPDATE_URL

    # ---- identity ----
    username: str = ""
    client_id: str = field(default_factory=new_client_id)

    # ---- room ----
    #: "host" | "player", or "" for "not chosen yet".
    #:
    #: Empty is a real state now: the GUI starts with no identity selected so
    #: that opening the app does not silently make somebody a host candidate.
    #: validated() refuses to connect until it is set.
    role: str = ""
    room: str = proto.DEFAULT_ROOM

    # ---- host ----
    mc_host: str = "127.0.0.1"
    mc_port: int = proto.DEFAULT_MC_PORT
    max_tunnels: int = proto.DEFAULT_MAX_TUNNELS

    #: Room capacity, host included. Validated against the server's limit.
    max_players: int = proto.DEFAULT_MAX_PLAYERS

    # ---- 外观（只影响观感，永远不参与逻辑判断）----
    #: 界面语言：zh / en / ja。翻不到的句子会显示中文原文。
    ui_language: str = "zh"
    #: 配色：dark / light / system 三套（彩色主题已按用户意见去掉）。
    ui_theme: str = "dark"

    # ---- player ----
    local_host: str = "127.0.0.1"
    local_port: int = proto.DEFAULT_LOCAL_PORT

    # ---- behaviour ----
    #: 房间策略：relay 仅中继；stun 仅直连；auto 先中继、后台直连并保留备用。
    mode: str = "relay"
    reconnect_delay: float = 3.0

    #: 这台机器是不是**第一次**运行（没有任何配置文件）。
    #:
    #: 只影响界面显示：第一次打开时「基准配置」那三个数字留空，让玩家自己填；
    #: 之后每次打开都记忆上一次提交过的值（用户要求）。
    #: 它不参与任何连接判断 —— 连接读的是 app._committed。
    first_run: bool = False

    # --------------------------------------------------------

    def validated(self):
        """
        Return a cleaned copy. Raises ValueError with a Chinese message when
        something is unusable, so the GUI can show it directly.
        """
        cfg = ClientConfig(**asdict(self))

        cfg.server_host = (cfg.server_host or "").strip()
        if not cfg.server_host:
            raise ValueError("请填写中转服务器地址")

        try:
            cfg.server_port = int(cfg.server_port)
        except (TypeError, ValueError):
            raise ValueError("中转服务器端口必须是数字")

        if not (1 <= cfg.server_port <= 65535):
            raise ValueError("中转服务器端口必须在 1-65535 之间")

        cfg.room = (cfg.room or "").strip()
        if not cfg.room:
            raise ValueError("请填写房间名")

        # 房间名是给人看的：中文、英文、空格、emoji 都行。协议里发出去的是
        # percent-encode 之后的 room token（见 protocol.room_token），所以这里
        # 只剩下"非空 / 没有控制字符 / 不太长"三条。
        problem = proto.room_name_problem(cfg.room)

        if problem:
            raise ValueError(problem)

        cfg.username = (cfg.username or "").strip() or "玩家"
        if len(cfg.username) > proto.MAX_USERNAME_LEN:
            raise ValueError(f"用户名最长 {proto.MAX_USERNAME_LEN} 个字符")

        cfg.username = "".join(
            c for c in cfg.username if ord(c) >= 33 and ord(c) != 127
        ) or "玩家"

        if not cfg.client_id:
            cfg.client_id = new_client_id()

        cfg.client_id = "".join(
            c for c in cfg.client_id if ord(c) >= 33 and ord(c) != 127
        )[: proto.MAX_CLIENT_ID_LEN] or new_client_id()

        try:
            cfg.mc_port = int(cfg.mc_port)
        except (TypeError, ValueError):
            raise ValueError("Minecraft 局域网端口必须是数字")

        if not (1 <= cfg.mc_port <= 65535):
            raise ValueError("Minecraft 局域网端口必须在 1-65535 之间")

        try:
            cfg.local_port = int(cfg.local_port)
        except (TypeError, ValueError):
            raise ValueError("本地代理端口必须是数字")

        if not (1 <= cfg.local_port <= 65535):
            raise ValueError("本地代理端口必须在 1-65535 之间")

        # Room capacity. Refused rather than clamped: somebody who typed 20
        # and silently got 8 would never learn why their friends could not
        # join. The server's own limit is configurable; this is the ceiling the
        # client will accept.
        try:
            cfg.max_players = int(cfg.max_players)
        except (TypeError, ValueError):
            raise ValueError(
                f"人数上限必须是数字（{proto.MIN_MAX_PLAYERS}-"
                f"{proto.MAX_MAX_PLAYERS}）"
            )

        if not (
            proto.MIN_MAX_PLAYERS <= cfg.max_players <= proto.MAX_MAX_PLAYERS
        ):
            raise ValueError(
                f"人数上限只能是 {proto.MIN_MAX_PLAYERS}-"
                f"{proto.MAX_MAX_PLAYERS} 人。"
                f"客户端支持的最大值是 {proto.MAX_MAX_PLAYERS} 人"
                "（这是单个房间的上限，不是所有房间加起来）。"
            )

        # ---- 外观：不认识的值一律退回默认，绝不因此挡住连接 ----
        #
        # 这两个字段只影响观感，所以这里不像上面那些"填错就报错"——
        # 配色写错了不该让人连不上游戏。
        cfg.ui_language = str(cfg.ui_language or "zh").strip().lower()

        if cfg.ui_language not in UI_LANGUAGES:
            cfg.ui_language = "zh"

        cfg.ui_theme = str(cfg.ui_theme or "dark").strip().lower()

        if cfg.ui_theme not in UI_THEMES:
            cfg.ui_theme = "dark"

        # ---- max_tunnels is DERIVED, never trusted from the file ----
        #
        # The room capacity DECIDES how many tunnels are opened: one per
        # player. These two used to be independent values and they drifted --
        # a config file written before the capacity field existed still held
        # max_tunnels=10, so a room set to 8 people opened ten channels and
        # displayed "0 / 10" no matter what the user chose.
        #
        # One per player is exactly right: the host's own client reaches the
        # LAN world directly, and every remote player needs one tunnel of
        # their own. Not derived from the file, because anything a user can
        # set independently is something that can contradict what they set.
        cfg.max_tunnels = max(1, min(cfg.max_players, 64))

        # An unset role is NOT silently turned into "host".
        #
        # It used to be, and that is how somebody who never chose an identity
        # ended up claiming the host slot: the GUI defaulted to host, and this
        # normalisation would have forced it back to host even if the GUI had
        # not. A missing identity is a question for the user, not a default.
        if cfg.role not in ("host", "player"):
            raise ValueError("请先选择身份（主机 Host 或玩家 Player）")

        return cfg


# ------------------------------------------------------------
# Persistence
# ------------------------------------------------------------

def load_config():
    cfg = ClientConfig()

    path = None

    for candidate in _candidate_paths():
        if os.path.isfile(candidate):
            path = candidate
            break

    if path is None:
        # ---- 第一次打开 ----
        #
        # 没有任何配置文件 = 这台机器第一次跑。用户要求：第一次打开时
        # 「基准配置」里那三个数字是**空的**，让玩家自己填一次，不要给
        # 预填的值（预填会让人以为"默认就能用"，而端口填错是最难查的一类问题）。
        #
        # 这个标记只影响界面显示，不参与连接判断：真正提交过什么值，
        # 看的是 app._committed（见 gui/app.py 的 _load_config_fields）。
        cfg.first_run = True

        return cfg

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        if not isinstance(data, dict):
            cfg.first_run = True
            return cfg

        known = {f for f in ClientConfig().__dataclass_fields__}

        for key, value in data.items():
            if key not in known:
                continue
            if key == "client_id" and not value:
                continue
            setattr(cfg, key, value)

    except Exception:
        # A corrupt config must never block startup.
        cfg.first_run = True

        return cfg

    if not cfg.client_id:
        cfg.client_id = new_client_id()

    cfg.first_run = False

    return cfg


def save_config(cfg):
    """
    Persist the config.

    Returns the path it was written to, or None when every candidate location
    failed. Never raises: losing the config file must not break the client.
    """
    data = asdict(cfg)

    for path in _candidate_paths():
        try:
            directory = os.path.dirname(path)

            if directory:
                os.makedirs(directory, exist_ok=True)

            tmp = path + ".tmp"

            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)

            os.replace(tmp, path)

            return path

        except Exception:
            continue

    return None
