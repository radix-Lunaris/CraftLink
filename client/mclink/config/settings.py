"""
CraftLink client -- settings / persistence.

存两份东西，位置由 `userdirs` 决定（M6）：

  * `mclink_config.json` —— 界面状态（身份、房间、上次用的数字、外观）；
  * `logs/`、`cache/` —— 运行期文件。

**默认目录**：Windows `%APPDATA%\\CraftLink`，其它平台
`~/.config/CraftLink`（`$XDG_CONFIG_HOME` 优先）。用户可以在界面上改，
改完记在默认目录的 `data_dir.txt` 里。**绝不再写程序安装目录** —— 老代码
的 `fallback_dir()` 是"EXE 旁边 / 当前目录"，那正是"往安装目录写运行期
文件"，已经删掉。

品牌（软件名 / logo / 默认中继地址）来自 `branding.json`（见 branding.py）。
"""

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field

from . import branding
from . import userdirs
from ..core import protocol as proto

#: 软件名。**默认 = 当前项目状态**；有 branding.json 时被它覆盖。
#:
#: 注意：它只在 import 时求值一次。运行时改 branding.json 需要重启（或者
#: 调 branding.reload() + 重建界面），这是刻意的 —— 软件名出现在几十处
#: 界面文字和日志里，中途改名只会让日志前后对不上。
APP_NAME = branding.app_name()
APP_NAME_ZH = "方块联机"
APP_VERSION = "3.9.22"
#: 允许的界面语言代码（和 gui/i18n.py 的 LANGUAGES 对应）。
UI_LANGUAGES = ("zh", "en", "ja")

#: 允许的主题名。**必须和 gui/theme.py 的 THEME_LABELS 一致**。
#:
#: 这里不能直接 import gui.theme：config 被 gui 依赖，反过来 import 会成环
#: （gui.theme 又要读 config）。所以两边各存一份，靠测试
#: （tests/gui_appearance_test.py 的"主题名单两处一致"）钉住不许漂移。
UI_THEMES = ("dark", "light", "system")

#: 数据目录名（默认目录用；改过目录之后以 userdirs 的指针文件为准）。
#:
#: 从 "MC-Link" 改成 "CraftLink" 是 M6 的一部分（计划书 §1）。老目录不丢：
#: `userdirs.migrate_legacy()` 会把配置/日志复制过来，并把旧凭据清掉
#: （D11：登录记忆默认留空，不允许旧文件把复选框自动勾回来）。
APP_DIR_NAME = userdirs.APP_DIR_NAME

CONFIG_FILENAME = "mclink_config.json"

#: 中继地址没填时给用户看的引导（**明确说清填什么、填完会怎样**）。
#:
#: 客户端不再有任何内置服务器（D10），所以"地址是空的"是一个正常状态，
#: 不是一个错误状态 —— 但必须有人告诉用户下一步做什么，不能静默失败。
SERVER_GUIDE = (
    "中转服务器地址还没有填写。\n\n"
    "请填写这两项（由搭建中继服务器的人提供）：\n"
    "  · 服务器地址：域名或 IP，例如 relay.example.com\n"
    "  · 端口：部署时定的数字（1-65535）\n\n"
    "填好后会在连接/测试成功时被记住（存在你的用户数据目录里），"
    "下次自动带出；随时可以修改，改完立即生效。\n\n"
    "你是自己搭建的中继？地址就是部署那台机器的域名或 IP。"
)

#: 端口单独缺失时的引导（地址有了、端口没填）。
SERVER_PORT_GUIDE = (
    "中转服务器端口还没有填写。\n\n"
    "端口是部署中继服务器时定的那个数字（1-65535）。\n"
    "如果不确定，请向搭建服务器的人确认 —— 客户端没有内置的默认端口。"
)

#: 服务器卡片上的常驻提示（没填地址时显示）。
SERVER_FIELD_HINT = (
    "首次使用：请填写中继服务器地址与端口（部署者提供）。"
    "填好后会被记住，下次自动带出。"
)

#: 服务器卡片上的常驻提示（填了地址时显示）。
SERVER_FIELD_HINT_SET = "地址与端口随时可改，改完立即生效。"


# ------------------------------------------------------------
# 品牌默认值（branding.json）
# ------------------------------------------------------------

def default_server_host():
    """branding.json 里的默认地址；没有就是空串（**没有内置服务器**）。"""
    return branding.default_server_host()


def default_server_port():
    """branding.json 里的默认端口；没有就是 0（= 未设置）。"""
    return branding.default_server_port()


def update_url():
    """「需要更新」弹窗里的下载页；空串 = 不显示网址。"""
    return branding.update_url()


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

    软件名**不跟随语言**（中文界面、英文界面、日文界面下它都一样），
    也不写死在代码里：有 branding.json 时用品牌名（见 branding.py）。
    后面那截说明文字才翻译（见 gui/i18n.py 的 "Minecraft 联机"）。
    """
    return f"{APP_NAME} v{APP_VERSION} (构建 {build_stamp()})"


# ------------------------------------------------------------
# 目录（全部委托给 userdirs —— 单一事实来源）
# ------------------------------------------------------------

def config_dir():
    """当前生效的用户数据目录（配置 / 记忆 / 日志 / 缓存都在它下面）。"""
    return userdirs.data_dir()


def default_config_dir():
    """平台默认数据目录（用户没改过目录时就是它）。"""
    return userdirs.default_dir()


def config_dir_description():
    """给界面/日志用的一份目录说明（不含秘密）。"""
    return userdirs.describe()


def set_config_dir(path):
    """改数据目录（含校验）。返回 `(ok, message)`；迁移由调用方负责。"""
    return userdirs.set_data_dir(path)


def reset_config_dir():
    """恢复默认数据目录，返回默认目录。"""
    return userdirs.clear_data_dir()


def fallback_dir():
    """
    最后兜底目录：系统临时目录下的 CraftLink-<用户>。

    **不是**程序目录，也不是当前工作目录（老行为）：那等于往安装目录写
    运行期文件。写不进去也不影响功能 —— 配置记忆是便利功能，不是数据通路。
    """
    return userdirs.fallback_dir()


def config_path():
    return userdirs.config_path(CONFIG_FILENAME)


def fallback_config_path():
    return userdirs.fallback_path(CONFIG_FILENAME)


def _candidate_paths():
    """优先数据目录，其次系统临时目录（**永远不是**安装目录）。"""
    return userdirs.config_candidates(CONFIG_FILENAME)


def log_dir():
    """日志目录（优先数据目录，失败退到系统临时目录）。"""
    path = userdirs.logs_dir()

    if userdirs.ensure_dir(path) and os.access(path, os.W_OK):
        return path

    fallback = os.path.join(userdirs.fallback_dir(), "logs")

    if userdirs.ensure_dir(fallback):
        return fallback

    return userdirs.fallback_dir()


def cache_dir():
    """缓存目录（和数据目录一起走；当前只有测试与将来的功能用它）。"""
    path = userdirs.cache_dir()

    if userdirs.ensure_dir(path) and os.access(path, os.W_OK):
        return path

    fallback = os.path.join(userdirs.fallback_dir(), "cache")

    if userdirs.ensure_dir(fallback):
        return fallback

    return userdirs.fallback_dir()


def new_client_id():
    """A stable random identifier. Display only -- never a secret."""
    return uuid.uuid4().hex[:16]


@dataclass
class ClientConfig:
    # ---- connection ----
    #:
    #: ★ D10：**默认空**。客户端不再内置任何真实服务器地址；安装器可以在
    #: branding.json 里给一个默认值，用户界面里随时可改。
    server_host: str = field(default_factory=branding.default_server_host)
    server_port: int = field(default_factory=branding.default_server_port)

    #:
    #: 这个地址是谁给的：
    #:   ""        没确认过（老配置 / 从没填过）—— 迁移时会被清掉并提示
    #:   "user"    用户自己填过或确认过
    #:   "branding" 来自 branding.json 的默认值（用户按了「恢复默认」）
    server_source: str = ""

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
    #: "relay" | "stun" | "auto"（= `proto.CONN_MODES`）。
    #:
    #:   relay —— 游戏数据只走中继；兼容性最好。
    #:   stun  —— 只走 P2P 打洞直连，**永不回落中继**；打不通就连不上。
    #:   auto  —— 优先直连；某条连接等不到洞时**这条连接**改用中继，不影响
    #:            其他连接，也绝不把房间模式改回 relay（V5.0，见
    #:            docs/AUTO-SCHEDULING-PLAN.md）。
    #:
    #: 三个都是"房间级"的：真正生效的值由中继广播、Owner 独占决定，本机这份
    #: 只是建房/加入时 seed 上去的意图。
    mode: str = "relay"
    reconnect_delay: float = 3.0

    #: 这台机器是不是**第一次**运行（没有任何配置文件）。
    #:
    #: 只影响界面显示：第一次打开时「基准配置」那三个数字留空，让玩家自己填；
    #: 之后每次打开都记忆上一次提交过的值（用户要求）。
    #: 它不参与任何连接判断 —— 连接读的是 app._committed。
    first_run: bool = False

    # --------------------------------------------------------

    def has_server(self):
        """地址和端口都填了才算"能连"。"""
        return bool((self.server_host or "").strip()) and bool(
            self._port_int()
        )

    def _port_int(self):
        try:
            port = int(self.server_port)
        except (TypeError, ValueError):
            return 0

        return port if 1 <= port <= 65535 else 0

    def server_label(self):
        """"host:port"（缺哪个就空着），给日志和界面用。"""
        if not self.has_server():
            return ""

        return f"{self.server_host.strip()}:{self._port_int()}"

    def validated(self):
        """
        Return a cleaned copy. Raises ValueError with a Chinese message when
        something is unusable, so the GUI can show it directly.
        """
        cfg = ClientConfig(**asdict(self))

        cfg.server_host = (cfg.server_host or "").strip()
        if not cfg.server_host:
            # ★ D10：没有内置服务器 = "地址为空"是**预期状态**。所以这里给的是
            # 一段"下一步做什么"的引导，而不是一句干巴巴的报错。
            raise ValueError(SERVER_GUIDE)

        if cfg.server_port in (None, "", 0, "0"):
            raise ValueError(SERVER_PORT_GUIDE)

        try:
            cfg.server_port = int(cfg.server_port)
        except (TypeError, ValueError):
            raise ValueError("中转服务器端口必须是数字（1-65535）")

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
                f"{proto.DEFAULT_MAX_PLAYERS}）"
            )

        if not (
            proto.MIN_MAX_PLAYERS <= cfg.max_players <= proto.DEFAULT_MAX_PLAYERS
        ):
            raise ValueError(
                f"人数上限只能是 {proto.MIN_MAX_PLAYERS}-"
                f"{proto.DEFAULT_MAX_PLAYERS} 人。"
                f"服务器允许的最大值是 {proto.DEFAULT_MAX_PLAYERS} 人"
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
        #
        # 上限取协议里的同一个常量（M1/D12：64），不再写死一个字面量 ——
        # 两个数字各写一遍正是"房间 64 人只开 8 条隧道"那类 bug 的来源。
        cfg.max_tunnels = max(1, min(cfg.max_players, proto.DEFAULT_MAX_PLAYERS))

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

#: 老配置里"没有 server_source 字段"时的判定依据 —— 那时候的地址一律当作
#: **未经用户确认**（可能是老版本内置的默认值），迁移时清掉并要求重新填写。
def _address_was_user_confirmed(data):
    return str(data.get("server_source") or "") == "user"


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

    data = None

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

    # ---- 服务器地址：老配置里的不算数（M6 / D10）----
    #
    # 老版本的客户端把 `www.<某个真实域名>` 写死在代码里当默认值，用户配置
    # 里那份地址很可能**从来没有被用户看过**。所以：
    #   * 有地址但没有 server_source="user" 标记 -> 清掉，回到 branding 默认值
    #     （通常是空），并在配置对象上挂一个"清过"的标记，界面据此提示
    #     "请重新确认一次地址"，而不是静默地把一个旧默认值继续用下去；
    #   * 用户自己确认过的地址（server_source="user"）原样保留。
    legacy_address = bool(str(data.get("server_host") or "").strip()) and (
        not _address_was_user_confirmed(data)
    )

    if legacy_address:
        cfg.server_host = branding.default_server_host()
        cfg.server_port = branding.default_server_port()
        cfg.server_source = "branding" if cfg.server_host else ""

        # 非 dataclass 字段：不落盘，只给本次启动的界面提示用。
        cfg.legacy_address_cleared = True
    else:
        if not (cfg.server_host or "").strip():
            cfg.server_host = branding.default_server_host()
            cfg.server_port = branding.default_server_port()
            cfg.server_source = "branding" if cfg.server_host else ""

        if cfg.server_port in (None, "", 0, "0"):
            cfg.server_port = branding.default_server_port()

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
