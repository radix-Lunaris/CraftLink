"""
CraftLink client -- 品牌配置（`branding.json`）。

============================================================================
 这一层解决什么
============================================================================

开源版不能把"某个人的软件名 / 某个人的服务器地址"写死在代码里。品牌信息
（名字、logo、默认中继地址）由**构建/安装时固化的一份文件**决定：

    程序目录/branding.json      <- 安装器（或开发者）放这里

    程序启动时读它：
      * 文件不存在   -> 用代码里的默认值（= 当前项目状态：CraftLink + 内置图标）
      * 文件存在     -> 覆盖默认值
      * 文件写坏了   -> 坏的那一项退回默认值，并记一条警告（绝不因此起不来）

`default_server_host` / `default_server_port` 默认**为空**（D10）：客户端
不含任何"默认连到某个真实服务器"的行为。安装器可以在 branding.json 里填上
部署者自己的中继地址，用户界面里仍然是可改的（改完记忆在用户数据目录）。

============================================================================
 查找顺序（先找到先用）
============================================================================

  1. `MCLINK_BRANDING_FILE` 环境变量指定的路径（安装器/测试用；设了就只看它）
  2. `程序目录/branding.json`（冻结 EXE -> exe 所在目录；源码 -> `client/`）
  3. 打包进 EXE 的 `branding.json`（PyInstaller 的 `sys._MEIPASS`）

第 2 条是主路径：**改品牌不用重新打包**，安装器覆盖这个文件即可。

============================================================================
 字段（全部可选；缺失/类型不对 -> 该项用默认值）
============================================================================

    app_name             字符串  窗口标题 / 关于页 / 日志里的软件名。
                                  空串或缺失 -> "CraftLink"
    logo_file            字符串  相对 branding.json 所在目录，或绝对路径。
                                  找不到 / 不是图片 -> 用内置图标
    default_server_host  字符串  界面里"恢复默认"的目标地址；空 = 没有默认
    default_server_port  整数    同上，1-65535；其它值 = 没有默认
    update_url           字符串  「需要更新」弹窗里的下载页；空 = 不显示网址

未知字段直接忽略（向后/向前兼容：老客户端读到新字段不该报错）。
"""

import json
import os
import sys
from dataclasses import dataclass, field

try:  # pragma: no cover - 极端情况下 sys 被裁剪
    from . import userdirs
except Exception:  # pragma: no cover
    userdirs = None

#: 默认值 = **当前项目状态**（D10：中继地址默认为空）。
DEFAULT_APP_NAME = "CraftLink"
DEFAULT_LOGO_FILE = ""
DEFAULT_SERVER_HOST = ""
DEFAULT_SERVER_PORT = 0
DEFAULT_UPDATE_URL = ""

#: 文件名（固定，不跟软件名走）。
BRANDING_FILENAME = "branding.json"

#: 环境变量：显式指定品牌文件（设了就只看它）。
ENV_BRANDING_FILE = "MCLINK_BRANDING_FILE"

#: tkinter 的 PhotoImage 能直接读的图片后缀（.ico 只能当窗口图标）。
_IMAGE_SUFFIXES = (".png", ".gif", ".ppm", ".pgm")
_ICON_SUFFIXES = (".ico",)

_CACHE = None


@dataclass
class Branding:
    """一份解析好的品牌配置。`source` 为空串表示"没有品牌文件，全用默认值"。"""

    app_name: str = DEFAULT_APP_NAME
    logo_file: str = DEFAULT_LOGO_FILE
    default_server_host: str = DEFAULT_SERVER_HOST
    default_server_port: int = DEFAULT_SERVER_PORT
    update_url: str = DEFAULT_UPDATE_URL
    source: str = ""
    warnings: list = field(default_factory=list)

    # ---- 语义化小工具（界面用，避免各处重复判断）----

    def logo_image(self):
        """关于页/标题旁边能直接显示的 logo 路径（不是图片就返回空串）。"""
        if self.logo_file and self.logo_file.lower().endswith(_IMAGE_SUFFIXES):
            return self.logo_file

        return ""

    def logo_icon(self):
        """窗口图标（.ico）路径（不是 .ico 就返回空串）。"""
        if self.logo_file and self.logo_file.lower().endswith(_ICON_SUFFIXES):
            return self.logo_file

        return ""

    def has_server_default(self):
        return bool(self.default_server_host) and bool(self.default_server_port)

    def to_dict(self):
        return {
            "app_name": self.app_name,
            "logo_file": self.logo_file,
            "default_server_host": self.default_server_host,
            "default_server_port": self.default_server_port,
            "update_url": self.update_url,
            "source": self.source,
            "warnings": list(self.warnings),
        }


# ============================================================
# 找文件
# ============================================================

def program_dir():
    """程序目录（冻结 EXE -> exe 旁边；源码 -> `client/`）。"""
    if userdirs is not None:
        return userdirs.install_dir()

    if getattr(sys, "frozen", False):  # pragma: no cover
        return os.path.dirname(os.path.abspath(sys.executable))

    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def candidate_paths():
    """按优先级列出品牌文件候选位置（不判断存在性）。"""
    override = os.environ.get(ENV_BRANDING_FILE, "").strip()

    if override:
        # 显式指定 = 只看它（测试/安装器要能确定性地"没有品牌文件"）。
        return [os.path.abspath(os.path.expanduser(override))]

    found = [os.path.join(program_dir(), BRANDING_FILENAME)]

    bundled = getattr(sys, "_MEIPASS", "")

    if bundled:
        found.append(os.path.join(bundled, BRANDING_FILENAME))

    return found


def branding_file_path():
    """第一个真实存在的品牌文件路径；一个都没有就返回空串。"""
    for path in candidate_paths():
        if os.path.isfile(path):
            return path

    return ""


# ============================================================
# 解析
# ============================================================

def _clean_str(value):
    if value is None:
        return ""

    if isinstance(value, (dict, list, tuple, set)):
        return ""

    return str(value).strip()


def _clean_port(value):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return 0

    if 1 <= port <= 65535:
        return port

    return 0


def _resolve_logo(value, base_dir, warnings):
    """
    logo_file 解析：相对路径按品牌文件所在目录找，绝对路径原样。

    找不到时**不报错**，只记一条警告并退回内置图标 —— 一个写错的 logo
    路径不该让客户端起不来。
    """
    raw = _clean_str(value)

    if not raw:
        return ""

    candidate = raw if os.path.isabs(raw) else os.path.join(base_dir, raw)
    candidate = os.path.abspath(os.path.expanduser(candidate))

    if os.path.isfile(candidate):
        return candidate

    warnings.append(f"branding.json 里的 logo_file 找不到：{raw}（已用内置图标）")

    return ""


def parse(data, source="", base_dir=""):
    """把一份 dict 解析成 `Branding`。永远返回可用对象，永不抛。"""
    result = Branding(source=source)

    if not isinstance(data, dict):
        if data is not None:
            result.warnings.append("branding.json 不是一个 JSON 对象（已全部用默认值）")

        return result

    result.app_name = _clean_str(data.get("app_name")) or DEFAULT_APP_NAME

    if not _clean_str(data.get("app_name")):
        if "app_name" in data:
            result.warnings.append(
                "branding.json 的 app_name 为空，已用默认名 "
                f"{DEFAULT_APP_NAME}"
            )

    result.logo_file = _resolve_logo(
        data.get("logo_file"), base_dir or program_dir(), result.warnings
    )

    result.default_server_host = _clean_str(data.get("default_server_host"))

    raw_port = data.get("default_server_port")
    result.default_server_port = _clean_port(raw_port)

    if raw_port not in (None, "", 0, "0") and not result.default_server_port:
        result.warnings.append(
            f"branding.json 的 default_server_port 不是 1-65535 的整数："
            f"{raw_port!r}（已按「没有默认端口」处理）"
        )

    result.update_url = _clean_str(data.get("update_url"))

    return result


def load(force=False):
    """
    读取并缓存品牌配置。

    文件不存在 -> 全默认值（`source=""`）。读坏了 -> 默认值 + 警告。
    任何异常都不许冒出去：品牌只是外观与默认值，不是数据通路。
    """
    global _CACHE

    if _CACHE is not None and not force:
        return _CACHE

    path = branding_file_path()

    if not path:
        _CACHE = Branding()

        return _CACHE

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:  # noqa: BLE001
        branding = Branding(source=path)
        branding.warnings.append(
            f"branding.json 读取失败（已用默认值）：{e.__class__.__name__}: {e}"
        )

        _CACHE = branding

        return _CACHE

    branding = parse(data, source=path, base_dir=os.path.dirname(path))

    _CACHE = branding

    return branding


def reload():  # pragma: no cover - 测试与"开发者改完文件重读"用
    """丢掉缓存，下次 `load()` 重新读盘。"""
    global _CACHE

    _CACHE = None

    return load()


# ============================================================
# 便捷读取（settings / gui 用）
# ============================================================

def app_name():
    return load().app_name


def default_server_host():
    return load().default_server_host


def default_server_port():
    return load().default_server_port


def update_url():
    return load().update_url


def warnings():
    return list(load().warnings)


__all__ = [
    "BRANDING_FILENAME",
    "ENV_BRANDING_FILE",
    "DEFAULT_APP_NAME",
    "DEFAULT_SERVER_HOST",
    "DEFAULT_SERVER_PORT",
    "Branding",
    "branding_file_path",
    "candidate_paths",
    "program_dir",
    "parse",
    "load",
    "reload",
    "app_name",
    "default_server_host",
    "default_server_port",
    "update_url",
    "warnings",
]
