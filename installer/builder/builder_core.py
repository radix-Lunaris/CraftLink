r"""
CraftLink-Builder -- 核心逻辑（不依赖 tkinter，可单测）。

这个模块回答一个问题：**"面向用户的安装器" 需要什么输入，谁来生成？**

    开发者（交互式 GUI / --non-interactive）
        │  软件名 / 中继域名或地址 + 端口 / logo / 默认安装目录 / 默认数据目录
        ▼
    CraftLink-Builder（本模块）
        │
        ├── staging/branding.json        客户端读的品牌文件（字段严格按
        │                                docs/CLIENT-BRANDING.md §2.2）
        ├── staging/branding-logo.<ext>  品牌 logo（可选）
        ├── staging/CraftLink.exe        主程序（从 --exe 复制过来）
        ├── staging/CraftLink-console.exe 诊断版（可选）
        ├── setup-icon.ico               安装器/快捷方式图标（可选，不进 {app}）
        ├── params.iss                   Inno Setup 编译参数（UTF-8 带 BOM）
        ├── NEXT-STEPS.txt               "下一步怎么编译 CraftLink-Setup.exe"
        └── builder-report.json          机器可读的输入/输出清单（给 CI 与校验用）
        ▼
    scripts/build-installer.sh  →  ISCC.exe installer/CraftLink.iss  →  CraftLink-Setup.exe

硬规则（与计划书 D8/D10/D11 一致）：

  * **不硬编码任何服务器地址/凭据** —— 中继地址只来自 Builder 的输入，留空就是留空
    （客户端会走"首次运行自己填"的引导）；
  * **不写死安装路径** —— 默认值可以给，但用户随时能改（见 installer/CraftLink.iss）；
  * **数据目录用客户端已有的机制**：指针文件 `<客户端默认目录>\data_dir.txt`
    （UTF-8 无 BOM）+ 可选环境变量 `MCLINK_DATA_DIR`，不发明第二套；
  * 客户端里的这些文件名/环境变量名**不在本文件里手抄一遍**：运行时从
    `client/mclink/config/userdirs.py`、`branding.py` 读出来（读不到就明确报错）。
"""

import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import re
import shutil
import struct
import sys

import imageconv

#: Builder 自己的版本（进 report，便于排查"哪一版 Builder 生成的"）。
BUILDER_NAME = "CraftLink-Builder"
BUILDER_VERSION = "1.0.0"

#: 生成物文件名（固定，脚本与校验器都按这些名字找）。
PARAMS_FILENAME = "params.iss"
REPORT_FILENAME = "builder-report.json"
NEXT_STEPS_FILENAME = "NEXT-STEPS.txt"
STAGING_DIRNAME = "staging"
SETUP_ICON_FILENAME = "setup-icon.ico"

#: 装进 {app} 的 logo 统一改名成 `branding-logo.<ext>`：
#: 升级时 {@link installer/CraftLink.iss} 的 [InstallDelete] 才能用
#: `branding-logo.*` 把上一版的旧 logo 清掉（否则安装目录会越攒越多图片）。
LOGO_STAGED_PREFIX = "branding-logo"

#: Inno Setup 的 AppId 默认值（安装身份，决定"升级替换"而不是"并存安装"）。
#: 换一个品牌独立发布时请用 `--app-id` 换掉（docs/INSTALLER.md 有说明）。
DEFAULT_APP_GUID = "3E7A9C41-5B62-4D8F-9A10-C7D24E6B81F3"

#: 安装器向导文本（可被 --texts JSON 覆盖；默认中文，因为仓库与用户都是中文）。
DEFAULT_TEXTS = {
    "data_dir_page_caption": "用户数据目录",
    "data_dir_page_description": (
        "请选择保存日志、配置和记住的账号的位置（随时可以在软件里改）。"
    ),
    "data_dir_page_sub_caption": (
        "客户端不会把日志和配置写进安装目录。留空或取消勾选都不影响使用："
        "不选时使用 Windows 默认位置（%APPDATA%\\CraftLink）。"
    ),
    "data_dir_page_prompt": "用户数据目录：",
    "data_dir_page_new_folder_name": "CraftLinkData",
    "task_desktop_icon_desc": "创建桌面快捷方式(&D)",
    "task_set_envvar_desc": (
        "同时设置用户环境变量 MCLINK_DATA_DIR（高级：设置后软件里的"
        "「更改数据目录」不会生效）"
    ),
    "additional_tasks_desc": "附加任务：",
    "launch_desc": "立即运行(&R)",
    "msg_data_dir_empty": "请填写用户数据目录。",
    "msg_data_dir_inside_app": (
        "用户数据目录不能放在程序安装目录里面：程序升级会整目录替换，"
        "放在里面的日志和配置会丢。请换一个目录。"
    ),
    "msg_data_dir_relative": "请填写完整的路径（例如 D:\\CraftLinkData），不要用相对路径。",
    "msg_data_dir_unwritable": "这个目录建不出来（权限或路径问题），请换一个：",
    "msg_pointer_failed_env_set": (
        "没能写入数据目录指针文件，已改用用户环境变量 MCLINK_DATA_DIR 记录。"
        "需要重新登录（或重启资源管理器）后生效："
    ),
    "msg_pointer_failed": (
        "没能记录数据目录（指针文件写不进去，环境变量也写不进去）。"
        "程序仍会正常安装，但客户端会使用默认数据目录。你选择的是："
    ),
    "msg_uninstall_keep_data": (
        "用户数据（日志、配置、记住的账号）已保留，没有删除。位置："
    ),
}

#: 静默安装时指定数据目录的自定义参数：`/DATA_DIR="D:\CraftLinkData"`。
#: 刻意不叫 DIR（那是 Inno 自己的安装目录参数），避免撞名。
DATA_DIR_PARAM_NAME = "DATA_DIR"

#: 必须由 `client/**` 提供、不允许在本文件里重新定义的常量名。
CLIENT_FACTS_REQUIRED = (
    "app_dir_name",
    "pointer_filename",
    "env_data_dir",
    "logs_subdir",
    "cache_subdir",
    "branding_filename",
)

#: 文本键 -> .iss 里的 #define 名字（一一对应，多余/拼错的键会被拒绝）。
TEXT_DEFINES = {
    "data_dir_page_caption": "DataDirPageCaption",
    "data_dir_page_description": "DataDirPageDescription",
    "data_dir_page_sub_caption": "DataDirPageSubCaption",
    "data_dir_page_prompt": "DataDirPagePrompt",
    "data_dir_page_new_folder_name": "DataDirPageNewFolderName",
    "task_desktop_icon_desc": "TaskDesktopIconDesc",
    "task_set_envvar_desc": "TaskSetEnvVarDesc",
    "additional_tasks_desc": "AdditionalTasksDesc",
    "launch_desc": "LaunchDesc",
    "msg_data_dir_empty": "MsgDataDirEmpty",
    "msg_data_dir_inside_app": "MsgDataDirInsideApp",
    "msg_data_dir_relative": "MsgDataDirRelative",
    "msg_data_dir_unwritable": "MsgDataDirUnwritable",
    "msg_pointer_failed_env_set": "MsgPointerFailedEnvSet",
    "msg_pointer_failed": "MsgPointerFailed",
    "msg_uninstall_keep_data": "MsgUninstallKeepData",
}

DEFAULT_MIN_WINDOWS = "10.0"


# ============================================================================
#  小工具
# ============================================================================

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def looks_like_repo_root(path):
    """仓库根的特征：有 installer/CraftLink.iss 和 client/。"""
    return (
        os.path.isfile(os.path.join(path, "installer", "CraftLink.iss"))
        and os.path.isdir(os.path.join(path, "client"))
    )


def find_repo_root(extra=None, start=None):
    """
    找仓库根目录。

    顺序：`CRAFTLINK_REPO` 环境变量 > 显式给的候选 > 从当前工作目录/本文件位置
    往上找（最多 8 层）。

    为什么需要它：Builder 会被冻结成 exe 放到任意位置（甚至放进 installer\\builder\\
    dist\\），那时"本文件在哪"不再等于"仓库在哪"；`--repo-root` 也随时可以覆盖。
    """
    candidates = []

    env = os.environ.get("CRAFTLINK_REPO", "").strip()

    if env:
        candidates.append(env)

    if extra:
        candidates.append(extra)

    if start:
        candidates.append(start)

    candidates.append(os.getcwd())
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.dirname(os.path.abspath(sys.executable)))

    for candidate in candidates:
        current = os.path.abspath(candidate)

        for _ in range(8):
            if looks_like_repo_root(current):
                return current

            parent = os.path.dirname(current)

            if parent == current:
                break

            current = parent

    return ""


def sha256_file(path):
    h = hashlib.sha256()

    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def sanitize_dir_name(name, fallback="CraftLink"):
    """
    把软件名变成一个合法的目录名（只用于**默认值**，用户可改）。

    去掉 Windows 非法字符 `\\ / : * ? " < > |` 与控制字符，去掉首尾空格和点。
    """
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(name or ""))
    cleaned = cleaned.strip().strip(".")

    return cleaned or fallback


def sanitize_registry_key(name, fallback="CraftLink"):
    """注册表键名：只允许字母数字和 `-_ .`，其余换成 `-`。"""
    cleaned = re.sub(r"[^0-9A-Za-z._\-]", "-", str(name or "")).strip("-.")

    return cleaned or fallback


def iss_text(value):
    """
    把一段**用户文本**安全地放进 .iss（同时满足两种转义规则）：

      * ISPP 字符串字面量（`#define X "..."`）：`"` 写成 `""`；
      * Pascal 字符串字面量（[Code] 里的 `'...'`）：`'` 写成 `''`；
      * `{` / `}` 换掉：品牌名里的花括号会被 Inno 当成常量或让 ISPP 报错。

    换行/制表符一律换成空格（.iss 是逐行的）。

    注意：**不要**拿它处理带 Inno 常量的值（`{autopf}` / `{userappdata}`）——
    那正会把常量换掉。路径请用 `iss_path()` / `iss_path_pascal()`。
    """
    text = str(value if value is not None else "")
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = text.replace('"', '""').replace("'", "''")
    text = text.replace("{", "(").replace("}", ")")

    return text.strip()


def iss_path(value):
    """
    路径（用在 `[Setup]` / `[Files]` 这类"节里的值"，不经过 Pascal 字面量）。

    只做两件事：换行变空格、`"` 按 ISPP 规则写成 `""`。
    Inno 常量（`{app}`、`{autopf}`…）原样保留 —— 它们本来就是要在编译/安装时展开的。
    """
    text = str(value if value is not None else "")
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = text.replace('"', '""')

    return text.strip()


def iss_path_pascal(value):
    """
    路径，但会出现在 `[Code]` 的 Pascal 字符串字面量里
    （例如 `ExpandConstant('{#ClientDefaultDataDir}')`）：追加 `'` 的转义。
    """
    return iss_path(value).replace("'", "''")


def _path_for_iss(path, installer_dir):
    """
    生成 .iss 里的路径：能相对就相对（相对 `installer/`，也就是 .iss 所在目录），
    跨盘符才用绝对路径。

    为什么优先相对路径：`.iss` 里的字符串是字面量，路径越短越不容易踩到
    "反斜杠 + 字母" 的组合（见 `check_iss_path`），而且默认布局下所有路径都是
    `build\\staging\\...`，可读、可校验。
    """
    absolute = os.path.abspath(path)

    try:
        rel = os.path.relpath(absolute, installer_dir)
    except ValueError:          # Windows 上跨盘符
        rel = None

    if rel is None:
        return absolute.replace("/", "\\"), True

    if rel == ".":
        rel = ""

    return rel.replace("/", "\\"), False


#: ISPP 字符串里的反斜杠组合（保守表）。真出现时给出提示 —— 见 docs/INSTALLER.md。
_ESCAPE_HAZARD = re.compile(r"\\[tnrbfvaxu0-9\"\\]")


def check_iss_path(value):
    """路径里有没有可能被当成转义序列的反斜杠组合？返回提示文本或空串。"""
    match = _ESCAPE_HAZARD.search(value)

    if not match:
        return ""

    return (
        f"路径里有可疑的反斜杠组合 {match.group(0)!r}：{value} —— "
        "如果 Inno Setup 预处理器把它当成转义序列，编译会找不到文件；"
        "请把输出目录放在 installer/ 里面（默认布局不受影响）。"
    )


# ============================================================================
#  从 client/** 读事实（不抄常量）
# ============================================================================

@dataclasses.dataclass
class ClientFacts:
    """安装器必须与客户端保持一致的常量（来源：客户端源码）。"""

    app_dir_name: str = ""
    pointer_filename: str = ""
    env_data_dir: str = ""
    logs_subdir: str = ""
    cache_subdir: str = ""
    branding_filename: str = ""
    default_app_name: str = "CraftLink"
    logo_suffixes: tuple = ()
    app_version: str = ""
    source: str = ""

    def missing(self):
        return [name for name in CLIENT_FACTS_REQUIRED if not getattr(self, name)]


def _read_client_module(client_dir, dotted, names):
    """把 `client/mclink/config/userdirs.py` 当模块导入，取一批常量。"""
    import importlib

    added = False

    if client_dir not in sys.path:
        sys.path.insert(0, client_dir)
        added = True

    try:
        module = importlib.import_module(dotted)

        return {name: getattr(module, name) for name in names}
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(client_dir)


def _read_client_constants_by_regex(path, names):
    """兜底：直接正则读源码（导入失败时用，比如环境里缺依赖）。"""
    found = {}

    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return found

    for name in names:
        match = re.search(
            rf"^{re.escape(name)}\s*=\s*(['\"])(.*?)\1", text, re.MULTILINE
        )

        if match:
            found[name] = match.group(2)

    return found


def read_client_facts(repo_root):
    """
    读客户端事实。**优先导入**客户端模块（等于让客户端的解析器自己作证），
    失败再退回正则读源码；两条路都读不到就报错（绝不用手抄的值兜底）。
    """
    client_dir = os.path.join(repo_root, "client")
    config_dir = os.path.join(client_dir, "mclink", "config")
    userdirs_py = os.path.join(config_dir, "userdirs.py")
    branding_py = os.path.join(config_dir, "branding.py")
    settings_py = os.path.join(config_dir, "settings.py")

    facts = ClientFacts()

    if not os.path.isfile(userdirs_py):
        return facts

    names = [
        "APP_DIR_NAME",
        "POINTER_FILENAME",
        "ENV_DATA_DIR",
        "LOGS_SUBDIR",
        "CACHE_SUBDIR",
    ]

    got = {}
    source = ""

    try:
        got = _read_client_module(client_dir, "mclink.config.userdirs", names)
        source = "import:mclink.config.userdirs"
    except Exception:
        got = _read_client_constants_by_regex(userdirs_py, names)

        if got:
            source = "regex:userdirs.py"

    if not got:
        return facts

    facts.app_dir_name = str(got.get("APP_DIR_NAME") or "")
    facts.pointer_filename = str(got.get("POINTER_FILENAME") or "")
    facts.env_data_dir = str(got.get("ENV_DATA_DIR") or "")
    facts.logs_subdir = str(got.get("LOGS_SUBDIR") or "")
    facts.cache_subdir = str(got.get("CACHE_SUBDIR") or "")

    brand_names = ["BRANDING_FILENAME", "DEFAULT_APP_NAME"]
    brand_got = {}

    try:
        brand_got = _read_client_module(client_dir, "mclink.config.branding", brand_names)
        source += " + import:mclink.config.branding"
    except Exception:
        brand_got = _read_client_constants_by_regex(branding_py, brand_names)

        if brand_got:
            source += " + regex:branding.py"

    facts.branding_filename = str(brand_got.get("BRANDING_FILENAME") or "")
    facts.default_app_name = str(brand_got.get("DEFAULT_APP_NAME") or "CraftLink")

    # logo 后缀白名单：客户端只认这几种（branding.py 的 _IMAGE_SUFFIXES /
    # _ICON_SUFFIXES）。读不到就用文档里写死的那几种。
    suffixes = ()

    try:
        import importlib

        added = client_dir not in sys.path

        if added:
            sys.path.insert(0, client_dir)

        try:
            branding_mod = importlib.import_module("mclink.config.branding")
            suffixes = tuple(branding_mod._IMAGE_SUFFIXES) + tuple(
                branding_mod._ICON_SUFFIXES
            )
        finally:
            if added:
                with contextlib.suppress(ValueError):
                    sys.path.remove(client_dir)
    except Exception:
        suffixes = ()

    if not suffixes:
        suffixes = (".png", ".gif", ".ppm", ".pgm", ".ico")

    facts.logo_suffixes = tuple(suffixes)

    # 版本号：只读源码里的 APP_VERSION（settings.py 导入有副作用，不导入）。
    if os.path.isfile(settings_py):
        version = _read_client_constants_by_regex(settings_py, ["APP_VERSION"])

        facts.app_version = str(version.get("APP_VERSION") or "")

    facts.source = source

    return facts


def pe_architecture(path):
    """
    读 PE 头里的机器类型：0x8664 = x64，0x014C = x86，0xAA64 = arm64。

    返回 "x64" / "x86" / "arm64" / ""。读不出来（不是 PE / 文件不存在）返回空。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x400)
    except OSError:
        return ""

    if len(head) < 0x40 or head[:2] != b"MZ":
        return ""

    offset = struct.unpack_from("<I", head, 0x3C)[0]

    if offset + 6 > len(head) or head[offset:offset + 4] != b"PE\0\0":
        return ""

    machine = struct.unpack_from("<H", head, offset + 4)[0]

    return {0x8664: "x64", 0x014C: "x86", 0xAA64: "arm64"}.get(machine, "")


# ============================================================================
#  输入
# ============================================================================

@dataclasses.dataclass
class Inputs:
    """Builder 的全部输入（GUI、CLI、JSON 三条路都收敛到这里）。"""

    app_name: str = "CraftLink"
    server_host: str = ""
    server_port: int = 0
    update_url: str = ""
    logo: str = ""
    icon: str = ""
    default_install_dir: str = ""
    default_data_dir: str = ""
    out_dir: str = ""
    exe: str = ""
    console_exe: str = ""
    exe_name: str = ""
    app_version: str = ""
    app_publisher: str = "CraftLink"
    publisher_url: str = ""
    app_id: str = ""
    output_basename: str = "CraftLink-Setup"
    license_file: str = ""
    language_name: str = ""
    language_file: str = ""
    min_windows: str = DEFAULT_MIN_WINDOWS
    data_dir_page: bool = True
    desktop_icon_task: bool = True
    env_var_task: bool = True
    texts: dict = dataclasses.field(default_factory=dict)

    def merged_texts(self):
        merged = dict(DEFAULT_TEXTS)
        merged.update({k: v for k, v in (self.texts or {}).items() if v})
        # "立即运行" 后面带上软件名更友好。
        merged["launch_desc"] = merged.get("launch_desc") or "立即运行(&R)"

        return merged


FIELD_ALIASES = {
    "app_name": "app_name",
    "app-name": "app_name",
    "server_host": "server_host",
    "server-host": "server_host",
    "server_port": "server_port",
    "server-port": "server_port",
    "update_url": "update_url",
    "update-url": "update_url",
    "logo": "logo",
    "logo_file": "logo",
    "icon": "icon",
    "default_install_dir": "default_install_dir",
    "default-install-dir": "default_install_dir",
    "default_data_dir": "default_data_dir",
    "default-data-dir": "default_data_dir",
    "out": "out_dir",
    "out_dir": "out_dir",
    "exe": "exe",
    "console_exe": "console_exe",
    "console-exe": "console_exe",
    "exe_name": "exe_name",
    "app_version": "app_version",
    "app-version": "app_version",
    "app_publisher": "app_publisher",
    "publisher": "app_publisher",
    "publisher_url": "publisher_url",
    "app_id": "app_id",
    "app-id": "app_id",
    "output_basename": "output_basename",
    "output-basename": "output_basename",
    "license_file": "license_file",
    "license": "license_file",
    "language_name": "language_name",
    "language_file": "language_file",
    "language": "language_file",
    "min_windows": "min_windows",
    "data_dir_page": "data_dir_page",
    "desktop_icon_task": "desktop_icon_task",
    "env_var_task": "env_var_task",
    "texts": "texts",
}

_INT_FIELDS = ("server_port",)
_BOOL_FIELDS = ("data_dir_page", "desktop_icon_task", "env_var_task")


def inputs_from_mapping(data, base=None):
    """
    从 dict 造 `Inputs`（JSON 输入、GUI 表单、CLI 参数都用这条）。

    未知字段直接忽略（向前/向后兼容），字段别名见 `FIELD_ALIASES`。
    类型不对的布尔/整数给出 ValueError，由调用方变成人话错误。
    """
    result = dataclasses.replace(base) if base is not None else Inputs()

    if not isinstance(data, dict):
        raise ValueError("输入必须是一个 JSON 对象")

    for key, value in data.items():
        field = FIELD_ALIASES.get(str(key))

        if field is None:
            continue

        if value is None:
            continue

        if field in _BOOL_FIELDS:
            if isinstance(value, bool):
                setattr(result, field, value)
            elif isinstance(value, (int, float)):
                setattr(result, field, bool(value))
            elif isinstance(value, str):
                setattr(result, field, value.strip().lower() in ("1", "true", "yes", "on", "y"))
            else:
                raise ValueError(f"字段 {key} 需要 true/false，收到 {type(value).__name__}")

            continue

        if field in _INT_FIELDS:
            if isinstance(value, bool):
                raise ValueError(f"字段 {key} 需要数字")

            try:
                setattr(result, field, int(str(value).strip() or 0))
            except (TypeError, ValueError):
                raise ValueError(f"字段 {key} 需要数字（1-65535），收到 {value!r}")

            continue

        if field == "texts":
            if not isinstance(value, dict):
                raise ValueError("字段 texts 需要是一个对象")

            merged = dict(result.texts or {})
            merged.update(value)
            result.texts = merged

            continue

        if isinstance(value, (dict, list, tuple)):
            raise ValueError(f"字段 {key} 需要字符串，收到 {type(value).__name__}")

        setattr(result, field, str(value).strip())

    return result


def json_template():
    """CI 用的输入模板（`--print-json-template`）。"""
    return {
        "app_name": "CraftLink",
        "server_host": "",
        "server_port": 0,
        "update_url": "",
        "logo": "client/mclink/assets/app.png",
        "icon": "",
        "default_install_dir": "",
        "default_data_dir": "",
        "out": "installer/build",
        "exe": "client/dist/CraftLink.exe",
        "console_exe": "client/dist/CraftLink-console.exe",
        "app_version": "",
        "app_publisher": "CraftLink",
        "publisher_url": "",
        "app_id": "",
        "output_basename": "CraftLink-Setup",
        "license_file": "LICENSE",
        "language_file": "",
        "data_dir_page": True,
        "desktop_icon_task": True,
        "env_var_task": True,
        "texts": {},
    }


# ============================================================================
#  校验
# ============================================================================

@dataclasses.dataclass
class CheckResult:
    errors: list = dataclasses.field(default_factory=list)
    warnings: list = dataclasses.field(default_factory=list)

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)

    @property
    def ok(self):
        return not self.errors


_HOST_RE = re.compile(r"^[A-Za-z0-9._\-\[\]:]+$")


def _win_path_contains(parent, child):
    """
    Windows 路径的"包含"判断（大小写不敏感、斜杠不敏感）。

    必须在 Linux 上也能得到正确结论（校验脚本会在 Linux 跑），所以不走
    `os.path.abspath` —— 那会把 `D:\\CraftLink` 变成一个相对路径。
    """
    def norm(value):
        return str(value or "").replace("/", "\\").rstrip("\\").lower()

    parent_n = norm(parent)
    child_n = norm(child)

    if not parent_n or not child_n:
        return False

    return child_n == parent_n or child_n.startswith(parent_n + "\\")


def validate(inputs, repo_root, facts, check_files=True):
    """
    校验输入。返回 `CheckResult`。

    规则尽量与客户端一致（`settings.validated()`：
    端口 1-65535、地址空 = 没填），并且把"能提前发现的坑"都在这里说清楚。
    """
    result = CheckResult()

    # ---- 软件名 ----
    name = (inputs.app_name or "").strip()

    if not name:
        result.warn(
            f"没有填软件名，使用默认值 {facts.default_app_name or 'CraftLink'}"
        )

    if _ESCAPE_HAZARD.search(name or ""):
        result.warn("软件名里有反斜杠组合，可能被预处理器当成转义序列")

    if name and sanitize_dir_name(name) != name:
        result.warn(
            f"软件名里的 \\ / : * ? \" < > | 等字符不能出现在目录名里，"
            f"默认安装目录会用 {sanitize_dir_name(name)!r}（用户仍可改）"
        )

    # ---- 中继地址 / 端口（D10：留空 = 没有内置服务器）----
    host = (inputs.server_host or "").strip()

    if host:
        if "://" in host:
            result.error(
                "中继地址只填域名或 IP（不要带 http:// 或 https://）——"
                "客户端连接的是中继端口，不是网页地址"
            )
        elif not _HOST_RE.match(host):
            result.error(
                f"中继地址里有非法字符：{host!r}（只允许域名、IPv4、[IPv6]）"
            )

        if not (1 <= int(inputs.server_port or 0) <= 65535):
            result.error(
                "填了中继地址就必须一起填端口（1-65535）；"
                "端口留 0 等于没有默认服务器，客户端会引导用户自己填"
            )
    else:
        if inputs.server_port:
            result.warn("只填了端口没填地址：端口会被忽略（默认服务器需要两者都有）")

        result.warn(
            "中继地址留空：安装出来的客户端没有内置服务器，"
            "用户首次运行会看到填写引导（这是 D10 的预期行为）"
        )

    url = (inputs.update_url or "").strip()

    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        result.error(f"更新地址必须以 http:// 或 https:// 开头：{url!r}")

    # ---- logo ----
    if inputs.logo:
        if check_files and not os.path.isfile(inputs.logo):
            result.error(f"logo 文件不存在：{inputs.logo}")
        else:
            suffix = os.path.splitext(inputs.logo)[1].lower()

            if suffix not in tuple(facts.logo_suffixes):
                result.error(
                    f"logo 只支持 {'/'.join(facts.logo_suffixes)}（客户端 branding.py "
                    f"的白名单），收到 {suffix or '(没有后缀)'}"
                )

    if inputs.icon:
        if check_files and not os.path.isfile(inputs.icon):
            result.error(f"图标文件不存在：{inputs.icon}")
        elif os.path.splitext(inputs.icon)[1].lower() != ".ico":
            result.error("安装器图标（--icon）必须是 .ico 文件")

    # ---- 默认目录（都是默认值，用户可以改）----
    for label, value in (
        ("默认安装目录", inputs.default_install_dir),
        ("默认用户数据目录", inputs.default_data_dir),
    ):
        if not value:
            continue

        if not re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value):
            result.error(
                f"{label} 必须是绝对路径（例如 D:\\CraftLink）：{value!r}"
            )

        hint = check_iss_path(value)

        if hint:
            result.warn(hint)

    if inputs.default_install_dir and inputs.default_data_dir:
        if _win_path_contains(inputs.default_install_dir, inputs.default_data_dir):
            result.error(
                "默认用户数据目录不能放在默认安装目录里面"
                "（客户端也拒绝这种设置：升级会整目录替换）"
            )

    # ---- 版本 / 发布者 ----
    if inputs.app_version and not re.match(r"^[0-9]+(\.[0-9]+)*", inputs.app_version):
        result.warn(
            f"版本号 {inputs.app_version!r} 不是数字开头的常规版本号，"
            "安装器元数据会按 1.0.0.0 处理"
        )

    if inputs.app_id and not re.match(
        r"^\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}?$",
        inputs.app_id,
    ):
        result.error(f"--app-id 必须是 GUID（例如 {DEFAULT_APP_GUID}）：{inputs.app_id!r}")

    if not re.match(r"^[A-Za-z0-9._\-]+$", inputs.output_basename or ""):
        result.error(
            f"输出文件名只允许字母数字和 . _ -：{inputs.output_basename!r}"
        )

    if inputs.language_file and not inputs.language_name:
        result.warn("给了语言文件但没给语言名，安装器里会用 'custom' 作为语言名")

    unknown_texts = sorted(set(inputs.texts or {}) - set(TEXT_DEFINES))

    if unknown_texts:
        result.warn(f"这些文本键不认识（会被忽略）：{', '.join(unknown_texts)}")

    # ---- 主程序 ----
    if inputs.exe:
        if check_files and not os.path.isfile(inputs.exe):
            result.warn(
                f"主程序还不存在：{inputs.exe} —— 先构建客户端（PyInstaller），"
                "再跑 scripts/build-installer.sh（它会拦截这个问题）"
            )
        elif check_files and pe_architecture(inputs.exe) != "x64":
            arch = pe_architecture(inputs.exe) or "未知"
            result.warn(
                f"主程序架构是 {arch}（不是 x64）：安装器会按这个架构生成，"
                "x64 才启用 64 位安装模式"
            )

    if inputs.console_exe and check_files and not os.path.isfile(inputs.console_exe):
        result.warn(f"诊断版 exe 不存在，将不安装：{inputs.console_exe}")

    if inputs.license_file and check_files and not os.path.isfile(inputs.license_file):
        result.warn(f"许可证文件不存在，安装器不会显示许可页：{inputs.license_file}")

    if facts.missing():
        result.error(
            "读不到客户端常量（"
            + ", ".join(facts.missing())
            + "）：请确认 client/mclink/config/userdirs.py 存在；"
            "安装器不允许手抄这些名字（docs/INSTALLER.md §与客户端的接口）"
        )

    return result


def version_info_version(app_version):
    """把 `3.9.20` 变成 `3.9.20.0`（Inno 的 VersionInfoVersion 要四段数字）。"""
    numbers = re.findall(r"\d+", str(app_version or ""))
    parts = []

    for value in numbers[:4]:
        try:
            parts.append(str(min(int(value), 65535)))
        except ValueError:
            parts.append("0")

    while len(parts) < 4:
        parts.append("0")

    if parts == ["0", "0", "0", "0"]:
        parts = ["1", "0", "0", "0"]

    return ".".join(parts)


# ============================================================================
#  生成
# ============================================================================

def _params_lines(inputs, facts, paths, flags, texts, warnings):
    """把一切翻译成 `build/params.iss` 的 `#define` 行（UTF-8 带 BOM 写盘）。"""
    lines = [
        "; ==========================================================================",
        ";  由 CraftLink-Builder 生成 —— 不要手改",
        f";  Builder {BUILDER_VERSION} / {utc_now()}",
        ";",
        ";  这个文件被 installer\\CraftLink.iss 用 #include 读走：",
        ";  品牌文本（可能是中文）在这里，模板本身保持纯 ASCII（见模板顶部注释）。",
        ";  重新生成：CraftLink-Builder（GUI）或",
        ";      craftlink_builder.py --non-interactive --json <输入.json>",
        "; ==========================================================================",
        "",
        f'#define BuilderVersion "{iss_text(BUILDER_VERSION)}"',
        f'#define GeneratedAt "{iss_text(utc_now())}"',
        "",
        "; ---- 安装身份与外观 ----",
        # AppId 必须是 `{{GUID}` 形式（`{{` 是 Inno 里"字面量 {"的写法），
        # 所以这里**不能**过 iss_text（那会把花括号换成圆括号）。
        f'#define AppIdLine "{{{{{paths["app_guid"]}}}"',
        f'#define AppName "{iss_text(inputs.app_name)}"',
        f'#define AppVersion "{iss_text(inputs.app_version)}"',
        f'#define AppVersionInfo "{iss_text(version_info_version(inputs.app_version))}"',
        f'#define AppPublisher "{iss_text(inputs.app_publisher)}"',
        f'#define RegistryKeyName "{iss_text(paths["registry_key"])}"',
        f'#define OutputBaseFilename "{iss_text(inputs.output_basename)}"',
        f'#define MinVersionLine "{iss_text(inputs.min_windows)}"',
    ]

    if inputs.publisher_url:
        lines.append(f'#define AppPublisherURL "{iss_text(inputs.publisher_url)}"')

    lines += [
        "",
        "; ---- 目录（全是默认值，用户安装时可以改；脚本里没有写死盘符）----",
        f'#define DefaultDirNameLine "{iss_path(paths["default_dir_name"])}"',
        f'#define OutputDirLine "{iss_path(paths["output_dir"])}"',
        f'#define StagingDirLine "{iss_path(paths["staging_dir"])}"',
        f'#define MainExeName "{iss_text(paths["main_exe_name"])}"',
    ]

    if flags.get("has_console_exe"):
        lines.append(f'#define ConsoleExeName "{iss_text(paths["console_exe_name"])}"')
        lines.append("#define HasConsoleExe")

    if flags.get("has_branding"):
        lines.append(f'#define BrandingFileName "{iss_text(facts.branding_filename)}"')
        lines.append("#define HasBranding")

    if flags.get("has_logo"):
        lines.append(f'#define LogoStagedName "{iss_text(paths["logo_staged_name"])}"')
        lines.append("#define HasLogo")

    lines.append(f'#define LogoStagedPrefix "{iss_text(LOGO_STAGED_PREFIX)}"')

    if paths.get("setup_icon"):
        lines.append(f'#define SetupIconLine "{iss_path(paths["setup_icon"])}"')

    if paths.get("license_file"):
        lines.append(f'#define LicenseFileLine "{iss_path(paths["license_file"])}"')

    if paths.get("language_file"):
        lines.append(f'#define LanguageLine "{iss_path(paths["language_file"])}"')
        lines.append(
            f'#define LanguageName "{iss_text(paths["language_name"])}"'
        )

    if flags.get("arch_x64"):
        lines += ["", "; ---- x64 主程序：允许 x64 兼容架构，并按 64 位安装 ----", "#define ArchX64"]

    if inputs.language_file:
        lines += [
            "",
            "; 语言文件必须是本机 Inno Setup 里真实存在的那个（scripts/build-installer.sh",
            "; 会先检查；仓库默认不附带语言文件，避免编译时缺文件）。",
        ]

    lines += [
        "",
        "; ---- 与客户端一致的名字（全部来自 client/**，不允许手抄）----",
        f'#define ClientAppDirName "{iss_text(facts.app_dir_name)}"',
        # 这两个值会出现在 [Code] 的 ExpandConstant('...') 里 -> 走 Pascal 转义，
        # 但 {userappdata} 这个 Inno 常量必须原样保留。
        f'#define ClientDefaultDataDir "{iss_path_pascal(paths["client_default_data_dir"])}"',
        f'#define PointerFileName "{iss_text(facts.pointer_filename)}"',
        f'#define LogsSubdirName "{iss_text(facts.logs_subdir)}"',
        f'#define CacheSubdirName "{iss_text(facts.cache_subdir)}"',
        f'#define EnvVarName "{iss_text(facts.env_data_dir)}"',
        f'#define DataDirParamName "{iss_text(DATA_DIR_PARAM_NAME)}"',
        f'#define DataDirDefault "{iss_path_pascal(paths["data_dir_default"])}"',
    ]

    lines += [
        "",
        "; ---- 安装器文本（可被 --texts 覆盖）----",
    ]

    unknown = sorted(set(texts) - set(TEXT_DEFINES))

    if unknown:
        warnings.append(f"这些文本键不认识，已忽略：{', '.join(unknown)}")

    for key in sorted(TEXT_DEFINES):
        if key not in texts:
            continue

        lines.append(f'#define {TEXT_DEFINES[key]} "{iss_text(texts[key])}"')

    lines += ["", "; ---- 开关（有定义 = 打开）----"]

    if inputs.data_dir_page:
        lines.append("#define DataDirPageEnabled")

    if inputs.desktop_icon_task:
        lines.append("#define DesktopIconTaskEnabled")

    if inputs.env_var_task:
        lines.append("#define EnvVarTaskEnabled")

    if warnings:
        lines += ["", "; ---- 生成时的提醒（不阻塞编译）----"]

        for warning in warnings:
            lines.append(f"; ! {iss_text(warning)}")

    lines.append("")

    return "\n".join(lines)


def display_path(path):
    """
    给人看的路径。

    Windows 上把 `/` 换成 `\\`（原生写法）；在 Linux 上跑 Builder（做校验/离线
    测试时）就保持原样，不要输出 `\\home\\user\\...` 这种四不像。
    """
    text = str(path or "")

    if os.name == "nt" or re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
        return text.replace("/", "\\")

    return text


def _next_steps_text(inputs, paths, flags, report, repo_root):
    """"下一步怎么编译" —— CLI 打印、NEXT-STEPS.txt、GUI 文本框共用一份。"""
    out_dir = display_path(report["out_dir"])
    params_abs = display_path(paths["params_iss"])
    setup_exe = display_path(
        os.path.join(paths.get("output_dir_abs") or report["out_dir"],
                     inputs.output_basename + ".exe")
    )
    setup_name = os.path.basename(setup_exe)

    lines = [
        "=" * 78,
        f"{BUILDER_NAME} {BUILDER_VERSION} —— 下一步：编译面向用户的安装器",
        "=" * 78,
        "",
        f"这份 Builder 已经生成（都在 {out_dir}）：",
    ]

    for label, value in report["outputs"].items():
        if value:
            lines.append(f"  · {label}：{display_path(value)}")

    lines += [
        "",
        "接下来（在 Windows 上，装了 Inno Setup 6.3+）：",
        "",
        "  1) 构建客户端主程序（如果还没有）：",
        "       cd client",
        "       pyinstaller ..\\scripts\\mclink-client.spec --noconfirm --clean",
        "     产物：client\\dist\\CraftLink.exe（+ CraftLink-console.exe）",
        "",
        "  2) 编译安装器（一条命令，自动找 ISCC.exe / 校验 / 注入 / 编译）：",
        f'       bash scripts/build-installer.sh --params "{params_abs}"',
        "     或者手动（工作目录 = 仓库根）：",
        f'       "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe" installer\\CraftLink.iss',
        "",
        f"  3) 产物：{setup_exe}",
        "",
        "静默安装（给批量部署用）：",
        f'  {setup_name} /VERYSILENT /NORESTART /DIR="D:\\Apps\\CraftLink" '
        f'/DATA_DIR="D:\\CraftLinkData" /LOG="%TEMP%\\craftlink-install.log"',
        f'  {setup_name} /SILENT /TASKS="desktopicon"',
        f'  {setup_name} /VERYSILENT /TASKS="setenvvar"   ; 连环境变量一起设',
        "",
        "卸载（默认保留用户数据）：",
        "  控制面板 → 应用 → 卸载，或： \"%ProgramFiles%\\CraftLink\\unins000.exe\" /SILENT",
        "",
        "先自检再发出去：",
        f'  python3 scripts/verify-installer-inputs.py --out "{out_dir}"',
        "  （校验 branding.json / 参数 / 图标 / 与客户端常量是否一致）",
        "",
        "详见 docs/INSTALLER.md。",
    ]

    branding = report.get("branding") or {}

    if branding:
        host = branding.get("default_server_host") or "(空)"

        lines += [
            "",
            "-" * 78,
            "这一版固化进去的品牌信息：",
            f"  软件名        {branding.get('app_name')}",
            f"  中继默认地址  {host}",
            f"  中继默认端口  {branding.get('default_server_port')}",
            f"  logo          {branding.get('logo_file') or '(内置图标)'}",
            f"  更新地址      {branding.get('update_url') or '(空)'}",
            "-" * 78,
        ]

    if report.get("warnings"):
        lines += ["", "提醒："]

        for warning in report["warnings"]:
            lines.append(f"  ! {warning}")

    lines.append("")

    return "\n".join(lines)


def build(inputs, repo_root, out_dir=None, force=False, check_files=True):
    """
    生成安装器输入。返回 report dict（可 JSON 序列化）。

    会做：读客户端常量 → 校验 → 建 out 目录 → 复制 exe/logo → 写 branding.json
    →（可选）生成 setup-icon.ico → 写 params.iss → 写 NEXT-STEPS.txt
    → 写 builder-report.json。
    """
    repo_root = os.path.abspath(repo_root)
    installer_dir = os.path.join(repo_root, "installer")
    facts = read_client_facts(repo_root)

    merged = Inputs(**dataclasses.asdict(inputs))
    merged.out_dir = os.path.abspath(out_dir or inputs.out_dir or os.path.join(installer_dir, "build"))

    # ---- 默认值补全（都来自仓库现状，不是凭空写死）----
    if not merged.app_name:
        merged.app_name = facts.default_app_name or "CraftLink"

    if not merged.exe:
        merged.exe = os.path.join(repo_root, "client", "dist", "CraftLink.exe")

    if not merged.console_exe:
        candidate = os.path.join(os.path.dirname(merged.exe), "CraftLink-console.exe")

        if os.path.isfile(candidate):
            merged.console_exe = candidate

    if not merged.app_version:
        merged.app_version = facts.app_version or "0.0.0"

    if not merged.license_file:
        candidate = os.path.join(repo_root, "LICENSE")

        if os.path.isfile(candidate):
            merged.license_file = candidate

    check = validate(merged, repo_root, facts, check_files=check_files)
    warnings = list(check.warnings)

    if not check.ok:
        return {
            "ok": False,
            "errors": list(check.errors),
            "warnings": warnings,
            "out_dir": merged.out_dir,
            "outputs": {},
            "next_steps": "",
        }

    # ---- 目录准备 ----
    staging = os.path.join(merged.out_dir, STAGING_DIRNAME)

    if os.path.isdir(merged.out_dir) and not force:
        known = (
            os.path.isfile(os.path.join(merged.out_dir, REPORT_FILENAME))
            or os.path.isfile(os.path.join(merged.out_dir, PARAMS_FILENAME))
            or os.path.basename(merged.out_dir) == "build"
        )
        has_content = bool(os.listdir(merged.out_dir))

        if has_content and not known:
            return {
                "ok": False,
                "errors": [
                    f"输出目录不是空的，而且不像是 Builder 的目录：{merged.out_dir}；"
                    "换一个 --out，或加 --force 表示确实要写进去"
                ],
                "warnings": warnings,
                "out_dir": merged.out_dir,
                "outputs": {},
                "next_steps": "",
            }

    os.makedirs(staging, exist_ok=True)

    # 清掉上一轮的同名产物（只删我们自己管理的名字）。
    for stale in (
        os.path.join(staging, facts.branding_filename),
        os.path.join(merged.out_dir, PARAMS_FILENAME),
    ):
        if os.path.isfile(stale):
            os.remove(stale)

    for name in os.listdir(staging):
        if name.startswith(LOGO_STAGED_PREFIX + "."):
            os.remove(os.path.join(staging, name))

    outputs = {}

    # ---- 1. 主程序（复制进 staging：安装器只依赖这个目录）----
    main_exe_name = merged.exe_name or os.path.basename(merged.exe)
    arch = pe_architecture(merged.exe)
    staged_exe = os.path.join(staging, main_exe_name)
    exe_ready = os.path.isfile(merged.exe)

    if exe_ready:
        shutil.copy2(merged.exe, staged_exe)
        outputs["主程序"] = staged_exe

    console_exe_name = ""
    has_console = False

    if merged.console_exe and os.path.isfile(merged.console_exe):
        console_exe_name = os.path.basename(merged.console_exe)
        shutil.copy2(merged.console_exe, os.path.join(staging, console_exe_name))
        outputs["诊断版 exe"] = os.path.join(staging, console_exe_name)
        has_console = True

    # ---- 2. logo ----
    logo_staged_name = ""
    has_logo = False

    if merged.logo and os.path.isfile(merged.logo):
        suffix = os.path.splitext(merged.logo)[1].lower()
        logo_staged_name = LOGO_STAGED_PREFIX + suffix
        shutil.copy2(merged.logo, os.path.join(staging, logo_staged_name))
        outputs["logo"] = os.path.join(staging, logo_staged_name)
        has_logo = True

    # ---- 3. 安装器图标（不进 {app}）----
    setup_icon_path = ""
    icon_source = merged.icon

    if not icon_source and merged.logo and os.path.isfile(merged.logo):
        if os.path.splitext(merged.logo)[1].lower() == ".ico":
            icon_source = merged.logo
        else:
            generated = os.path.join(merged.out_dir, SETUP_ICON_FILENAME)

            try:
                imageconv.write_ico_from_image(merged.logo, generated)
                setup_icon_path = generated
                outputs["安装器图标"] = generated
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"没能从 {os.path.basename(merged.logo)} 生成安装器图标"
                    f"（{exc}）；安装器会用默认图标。要自定义请用 --icon 直接给 .ico"
                )

    if icon_source and os.path.isfile(icon_source):
        if icon_source.lower().endswith(".ico"):
            target = os.path.join(merged.out_dir, SETUP_ICON_FILENAME)

            if os.path.abspath(icon_source) != os.path.abspath(target):
                shutil.copy2(icon_source, target)

            setup_icon_path = target
            outputs["安装器图标"] = target
        else:
            warnings.append(f"图标必须是 .ico，已忽略：{icon_source}")
    elif not setup_icon_path:
        bundled = os.path.join(repo_root, "client", "mclink", "assets", "app.ico")

        if os.path.isfile(bundled):
            target = os.path.join(merged.out_dir, SETUP_ICON_FILENAME)
            shutil.copy2(bundled, target)
            setup_icon_path = target
            outputs["安装器图标"] = target
            warnings.append(
                "没有指定 logo/图标，安装器用了客户端内置图标（client/mclink/assets/app.ico）"
            )

    # ---- 4. branding.json（字段严格按 docs/CLIENT-BRANDING.md §2.2）----
    branding = {
        "app_name": merged.app_name,
        "logo_file": logo_staged_name,
        "default_server_host": merged.server_host or "",
        "default_server_port": int(merged.server_port or 0),
        "update_url": merged.update_url or "",
    }

    needs_branding = bool(
        logo_staged_name
        or branding["default_server_host"]
        or branding["default_server_port"]
        or branding["update_url"]
        or branding["app_name"] != (facts.default_app_name or "CraftLink")
    )

    branding_path = ""
    has_branding = False

    if needs_branding:
        branding_path = os.path.join(staging, facts.branding_filename)

        # UTF-8 **无 BOM**：客户端 branding.py 用 encoding="utf-8" 读，
        # 带 BOM 会被 json 解析器拒绝（docs/CLIENT-BRANDING.md §7 明确要求）。
        with open(branding_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(branding, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

        outputs["branding.json"] = branding_path
        has_branding = True
    else:
        warnings.append(
            "品牌信息全是默认值：不生成 branding.json（客户端会用默认名 + 内置图标 + "
            "空服务器地址）。上一版安装留下的 branding.json 会在升级时被删掉。"
        )

    # ---- 5. 路径与开关 ----
    default_dir_name = "{autopf}\\" + sanitize_dir_name(merged.app_name)

    if merged.default_install_dir:
        default_dir_name = merged.default_install_dir.replace("/", "\\")

    data_dir_default = merged.default_data_dir.replace("/", "\\") if merged.default_data_dir else (
        "{userappdata}\\" + facts.app_dir_name
    )
    client_default_data_dir = "{userappdata}\\" + facts.app_dir_name

    # 安装包放 <out>/out/：和 staging/params 分开，一眼能看出"哪个是发货文件"。
    output_dir_abs = os.path.join(merged.out_dir, "out")
    os.makedirs(output_dir_abs, exist_ok=True)
    output_dir_iss, output_abs = _path_for_iss(output_dir_abs, installer_dir)
    staging_iss, staging_abs = _path_for_iss(staging, installer_dir)
    setup_icon_iss = ""
    license_iss = ""
    language_iss = merged.language_file

    if setup_icon_path:
        setup_icon_iss, _ = _path_for_iss(setup_icon_path, installer_dir)

    if merged.license_file and os.path.isfile(merged.license_file):
        license_iss, _ = _path_for_iss(merged.license_file, installer_dir)

    for label, value in (
        ("输出目录", output_dir_iss),
        ("staging", staging_iss),
        ("安装器图标", setup_icon_iss),
        ("许可证", license_iss),
    ):
        if value:
            hint = check_iss_path(value)

            if hint:
                warnings.append(f"{label}：{hint}")

    paths = {
        "app_guid": (merged.app_id or DEFAULT_APP_GUID).strip("{}"),
        "registry_key": sanitize_registry_key(merged.app_publisher) + "\\" + sanitize_registry_key(merged.app_name, fallback="CraftLink"),
        "default_dir_name": default_dir_name,
        "client_default_data_dir": client_default_data_dir,
        "data_dir_default": data_dir_default,
        "output_dir": output_dir_iss,
        "output_dir_abs": output_dir_abs,
        "staging_dir": staging_iss,
        "main_exe_name": main_exe_name,
        "console_exe_name": console_exe_name,
        "logo_staged_name": logo_staged_name,
        "setup_icon": setup_icon_iss,
        "license_file": license_iss,
        "language_file": language_iss,
        "language_name": merged.language_name or "custom",
        "params_iss": os.path.join(merged.out_dir, PARAMS_FILENAME),
        "params_iss_for_iss": PARAMS_FILENAME,
    }

    flags = {
        "has_console_exe": has_console,
        "has_branding": has_branding,
        "has_logo": has_logo,
        "arch_x64": arch == "x64",
        "exe_ready": exe_ready,
        "staging_relative": not staging_abs,
        "output_relative": not output_abs,
    }

    if arch and arch != "x64":
        warnings.append(
            f"主程序架构 {arch}：不启用 64 位安装模式（{arch} 主程序装在 32 位程序目录）"
        )

    texts = merged.merged_texts()

    # ---- 6. params.iss（UTF-8 **带 BOM**：模板是纯 ASCII，中文都在这）----
    params_text = _params_lines(merged, facts, paths, flags, texts, warnings)

    with open(paths["params_iss"], "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write(params_text)

    outputs["Inno 参数"] = paths["params_iss"]

    # ---- 7. report + next steps ----
    staged = {}

    for name in sorted(os.listdir(staging)):
        full = os.path.join(staging, name)

        if os.path.isfile(full):
            staged[name] = {
                "path": full,
                "size": os.path.getsize(full),
                "sha256": sha256_file(full),
            }

    report = {
        "ok": True,
        "builder": {"name": BUILDER_NAME, "version": BUILDER_VERSION},
        "generated_at": utc_now(),
        "repo_root": repo_root,
        "installer_dir": installer_dir,
        "out_dir": merged.out_dir,
        "staging_dir": staging,
        "inputs": dataclasses.asdict(merged),
        "client": dataclasses.asdict(facts),
        "client_facts_source": facts.source,
        "branding": branding,
        "has_branding_json": has_branding,
        "flags": flags,
        "staged": staged,
        "outputs": outputs,
        "warnings": warnings,
        "errors": [],
        "paths": paths,
        "arch": arch,
    }

    report["next_steps"] = _next_steps_text(merged, paths, flags, report, repo_root)

    next_steps_path = os.path.join(merged.out_dir, NEXT_STEPS_FILENAME)

    with open(next_steps_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report["next_steps"])

    report["outputs"]["下一步说明"] = next_steps_path

    with open(os.path.join(merged.out_dir, REPORT_FILENAME), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    report["outputs"]["报告"] = os.path.join(merged.out_dir, REPORT_FILENAME)
    report["next_steps_path"] = next_steps_path

    return report
