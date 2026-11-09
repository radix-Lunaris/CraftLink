"""
CraftLink client -- 用户数据目录（配置 / 记忆 / 日志 / 缓存）。

============================================================================
 为什么要有这个模块（M6 / 计划书 §1）
============================================================================

计划书口径：

    客户端用户数据（日志、记忆的服务器/账号）存 **用户可选目录**
    （安装器询问，默认 `%APPDATA%\\CraftLink` / `~/.config/CraftLink`）。

在这之前，客户端把运行期文件写在了**两个都不对**的地方：

  * `settings.fallback_dir()` 是"EXE 所在目录 / 当前工作目录"—— 于是
    "装到 `C:\\Program Files` 之后配置写不进去"和"配置写进了安装目录"
    同时存在（后者在 Program Files 下会直接失败，在绿色版里则把用户数据
    混进了程序目录）；
  * `credentials.py` 自己拼 `%APPDATA%\\MC-Link`（Linux 上 `APPDATA` 没设时
    变成 `~/MC-Link`），和 `settings.config_dir()` 的 `~/.config/MC-Link`
    **不是同一个目录** —— 配置和记忆各住各的，迁移时两边都得找。

这个模块把"目录在哪"收成**一个**答案，并且回答了三个问题：

  1. 默认在哪？  Windows `%APPDATA%\\CraftLink`，其它平台
     `$XDG_CONFIG_HOME/CraftLink`（没设就是 `~/.config/CraftLink`）。
  2. 用户改了之后怎么记住？  在**默认目录**里留一个指针文件 `data_dir.txt`
     （它自身必须待在稳定的地方，不能放在被指向的目录里 —— 否则改一次目录
     就再也找不到"用户选的是哪"）。
  3. 老目录怎么办？  `migrate_legacy()`：配置/日志**复制**过去，凭据文件
     **搬走**（不留在旧目录，见下）。

============================================================================
 凭据为什么是"搬走"而不是"复制"
============================================================================

配置和日志是**没有秘密的历史**，复制一份最安全（用户后悔了还能回旧版本）。
凭据文件（`credentials.dat` / `.prefs`）不一样：

  * 新版登录记忆默认留空（D11），旧文件里的"记住"意愿**不允许**自动把
    复选框勾回来；
  * 旧文件是加密的，但它是一份**没人再会读**的密码副本 —— 留在磁盘上
    只增加暴露面，不增加任何功能。

所以迁移时凭据是**移动**（复制成功即删源），并且把这件事写进迁移报告，
由界面明确告诉用户"旧的登录记忆已转移/清除，请重新输入一次"。

============================================================================
 硬规则
============================================================================

  * **不写死任何绝对路径**：默认目录全部由平台/环境变量推导；
  * **不往程序安装目录写运行期文件**：`fallback_dir()` 指向系统临时目录，
    而且 `set_data_dir()` 直接拒绝把数据目录设在安装目录里面。
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile

#: 数据目录名。**只用于默认目录**：用户改过之后一切以指针文件为准。
APP_DIR_NAME = "CraftLink"

#: 老版本的目录名。老用户的数据可能躺在这里，迁移时按这个名字找。
#:
#: 两个名字都要找，因为老代码里配置和凭据用的目录**不一样**：
#:   * 配置   -> `%APPDATA%\\MC-Link` / `~/.config/MC-Link`（settings.config_dir）
#:   * 凭据   -> `%APPDATA%\\MC-Link` / `~/MC-Link`（credentials._storage_path，
#:                Linux 上没有 APPDATA 时退化成家目录下的 MC-Link）
LEGACY_APP_DIR_NAMES = ("MC-Link",)

#: 指针文件：记录"用户把数据目录改到哪了"。它自己待在**默认目录**里。
POINTER_FILENAME = "data_dir.txt"

#: 迁移完成标记：避免每次启动都重新扫一遍老目录。
MIGRATION_MARKER = ".legacy-migrated"

#: 环境变量覆盖（安装器 / 便携版 / 测试用）。设了它就不看指针文件。
ENV_DATA_DIR = "MCLINK_DATA_DIR"

#: 数据目录里各子目录的名字。
LOGS_SUBDIR = "logs"
CACHE_SUBDIR = "cache"

#: 属于"数据"、迁移时复制的文件（不含秘密）。
MIGRATED_FILES = ("mclink_config.json",)

#: 属于"秘密"的文件：登录记忆。**老目录迁移时直接清掉**（见 migrate_legacy），
#: 用户在界面里主动换目录时才跟着搬（见 migrate_user_data）。
SECRET_FILES = ("credentials.dat", "credentials.dat.prefs")

#: 界面里"更改数据目录"时要一起搬走的东西（相对数据目录）。
MOVABLE_ENTRIES = ("mclink_config.json", LOGS_SUBDIR, CACHE_SUBDIR)

#: 上次目录解析/迁移过程中值得告诉用户的话（界面读它弹一次提示）。
#: 结构：{"kind": str, "message": str} 或 None。
LAST_NOTICE = None


def _notice(kind, message):
    global LAST_NOTICE

    LAST_NOTICE = {"kind": kind, "message": message}


def pop_notice():
    """取走并清空待提示信息（只提示一次）。"""
    global LAST_NOTICE

    current = LAST_NOTICE
    LAST_NOTICE = None

    return current


# ============================================================
# 基本路径
# ============================================================

def platform_base():
    """
    平台给"每用户配置"用的根目录。

    Windows : `%APPDATA%`（拿不到就退回家目录）
    macOS   : `~/Library/Application Support`
    其它    : `$XDG_CONFIG_HOME` 或 `~/.config`
    """
    if sys.platform.startswith("win"):
        return os.environ.get("APPDATA") or os.path.expanduser("~")

    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support")

    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )


def default_dir():
    """平台默认数据目录。用户没改过目录时就是它。"""
    return os.path.join(platform_base(), APP_DIR_NAME)


def install_dir():
    """
    程序自己的目录（**绝不允许**写运行期文件的地方）。

    冻结的 EXE -> exe 所在目录；源码运行 -> `client/`（main.py 那一层）。
    只用来做"数据目录不许设在这里面"的判断，不用来写任何东西。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    # .../client/mclink/config/userdirs.py -> .../client
    here = os.path.dirname(os.path.abspath(__file__))

    return os.path.dirname(os.path.dirname(here))


def fallback_dir():
    """
    最后兜底目录：系统临时目录下的 CraftLink-<用户>。

    为什么不是"EXE 旁边 / 当前目录"（老行为）：那正是"往安装目录写运行期
    文件"本身。临时目录会丢，但它至少是**对的地方**，而且写不进去也不会
    影响功能 —— 配置记忆是便利功能，不是数据通路。
    """
    who = (
        os.environ.get("USERNAME")
        or os.environ.get("USER")
        or str(os.getpid())
    )
    safe = "".join(c for c in who if c.isalnum() or c in "-_") or "user"

    return os.path.join(tempfile.gettempdir(), f"{APP_DIR_NAME}-{safe}")


def is_inside_install_dir(path):
    """`path` 是不是落在程序目录里面（含自己）？"""
    try:
        target = os.path.abspath(path)
        base = os.path.abspath(install_dir())

        return target == base or target.startswith(base + os.sep)
    except Exception:
        return False


# ============================================================
# 指针文件
# ============================================================

def pointer_path():
    """记录用户所选目录的文件（在**默认目录**里）。"""
    return os.path.join(default_dir(), POINTER_FILENAME)


def _read_pointer():
    try:
        with open(pointer_path(), "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except Exception:
        return ""

    if not value:
        return ""

    return os.path.abspath(os.path.expanduser(value))


def _write_pointer(path):
    """原子写指针文件。返回 True/False，绝不抛。"""
    target = pointer_path()

    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)

        tmp = target + ".tmp"

        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(os.path.abspath(path) + "\n")

        os.replace(tmp, target)

        return True
    except Exception:
        return False


def _remove_pointer():
    with contextlib.suppress(OSError):
        os.remove(pointer_path())


# ============================================================
# 解析
# ============================================================

def ensure_dir(path):
    """尽力创建目录。返回 True/False，绝不抛。"""
    try:
        os.makedirs(path, exist_ok=True)

        return os.path.isdir(path)
    except Exception:
        return False


def _usable(path):
    """
    这个目录现在能不能用（不存在就试着建）。

    "能用"只判断可创建/可写：磁盘满、权限不对、U 盘拔了，都不该让客户端
    起不来 —— 拿不到就用默认目录，并把原因记下来给界面提示。
    """
    if not path:
        return False

    if os.path.isdir(path):
        return os.access(path, os.W_OK)

    return ensure_dir(path) and os.access(path, os.W_OK)


def data_dir():
    """
    当前生效的数据目录（**唯一入口**）。

    优先级：
      1. `MCLINK_DATA_DIR` 环境变量（安装器 / 便携版 / 测试）
      2. 指针文件里用户选的目录
      3. 平台默认目录

    第 2 步的目录不可用时退回第 3 步，并记一条给界面看的提示 —— 不静默
    失败：用户改了目录却看到"设置没生效"必须有解释。
    """
    override = os.environ.get(ENV_DATA_DIR, "").strip()

    if override:
        return os.path.abspath(os.path.expanduser(override))

    chosen = _read_pointer()

    if chosen:
        if _usable(chosen):
            return chosen

        _notice(
            "warn",
            f"上次选择的数据目录不可用：{chosen}\n"
            f"本次已临时改回默认目录：{default_dir()}\n"
            "请检查该目录是否存在/可写，或在设置里重新选择。",
        )

    return default_dir()


def logs_dir():
    return os.path.join(data_dir(), LOGS_SUBDIR)


def cache_dir():
    return os.path.join(data_dir(), CACHE_SUBDIR)


def config_path(filename="mclink_config.json"):
    return os.path.join(data_dir(), filename)


def fallback_path(filename="mclink_config.json"):
    return os.path.join(fallback_dir(), filename)


def config_candidates(filename="mclink_config.json"):
    """优先数据目录；写不进去时退到系统临时目录（**不是**安装目录）。"""
    primary = config_path(filename)
    secondary = fallback_path(filename)

    if os.path.abspath(primary) == os.path.abspath(secondary):
        return [primary]

    return [primary, secondary]


def set_data_dir(path):
    """
    把数据目录改到 `path`。返回 `(ok, message)`。

    拒绝的情况（都给出人话解释，绝不静默）：
      * 空路径；
      * 路径在程序安装目录里面（那正是"往安装目录写运行期文件"）；
      * 目录建不出来 / 不可写。
    """
    if not path or not str(path).strip():
        return False, "数据目录不能为空"

    target = os.path.abspath(os.path.expanduser(str(path).strip()))

    if is_inside_install_dir(target):
        return False, (
            "数据目录不能设在程序安装目录里面：\n"
            f"  程序目录：{install_dir()}\n"
            f"  你选的：  {target}\n\n"
            "程序升级会整目录替换，放在里面的日志和配置会丢。"
        )

    if not ensure_dir(target):
        return False, f"目录建不出来（权限或路径问题）：{target}"

    if not os.access(target, os.W_OK):
        return False, f"目录不可写：{target}"

    if not _write_pointer(target):
        return False, (
            "没能记住这个选择（写指针文件失败）：\n"
            f"  {pointer_path()}\n\n"
            "请检查默认目录是否可写，或者用 MCLINK_DATA_DIR 环境变量指定。"
        )

    return True, target


def clear_data_dir():
    """恢复默认目录（删掉指针文件）。返回默认目录。"""
    _remove_pointer()

    return default_dir()


def describe():
    """给界面/日志用的一份目录说明（不含任何秘密）。"""
    return {
        "data_dir": data_dir(),
        "default_dir": default_dir(),
        "fallback_dir": fallback_dir(),
        "pointer": pointer_path(),
        "is_default": os.path.abspath(data_dir())
        == os.path.abspath(default_dir()),
        "override_env": bool(os.environ.get(ENV_DATA_DIR, "").strip()),
        "install_dir": install_dir(),
    }


# ============================================================
# 老目录迁移
# ============================================================

def legacy_dirs():
    """
    老版本可能用过的目录（存在才返回，去重，绝对路径）。

    包括老配置目录（`<平台根>/MC-Link`）和老凭据目录（Windows 同左；
    Linux 上老代码用的是 `~/MC-Link`）。
    """
    found = []

    for name in LEGACY_APP_DIR_NAMES:
        for candidate in (
            os.path.join(platform_base(), name),
            os.path.join(os.path.expanduser("~"), name),
        ):
            path = os.path.abspath(candidate)

            if path in found:
                continue

            if os.path.isdir(path):
                found.append(path)

    return found


def _copy_missing(src, dst):
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        if os.path.exists(dst):
            return False

        shutil.copy2(src, dst)

        return True
    except Exception:
        return False


def _move_secret(src, dst):
    """
    搬走一份凭据文件：复制成功才删源。

    复制失败（磁盘满/权限）就**不删**源文件 —— 宁可留一份，也不能因为
    迁移把用户的记忆弄丢。返回 (moved, removed_source)。
    """
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        if not os.path.exists(dst):
            shutil.copy2(src, dst)

        os.remove(src)

        return True, True
    except Exception:
        return False, False


def migrate_legacy(target_dir=None, sources=None, force=False):
    """
    把**老版本**目录里的用户数据迁到 `target_dir`（默认当前数据目录）。

    规则（这就是 D11 在磁盘上的落地）：

      * 配置文件 / 日志：**复制**（缺什么补什么，绝不覆盖已有的）；
      * 凭据文件（`credentials.dat` / `.prefs`）：**清掉，不迁移**。

    凭据为什么清掉而不是搬过来：老版本那份"记住密码"的意愿是**在旧位置、
    按旧口径**勾的，而 D11 要求"用户名/密码记忆默认留空，用户主动勾选才记"。
    把它搬过来会导致新版本一启动就自动勾上复选框并回填密码 —— 那正是这条
    决定要禁掉的行为。所以这里删源文件，并在报告里标 `credentials_discarded`，
    由界面明确告诉用户"请重新勾选一次"（不静默、也不留下没人读的密码副本）。

    迁完写 `.legacy-migrated` 标记，默认只跑一次（`force=True` 可重跑）。

    返回一份报告字典（可 JSON 序列化），界面据此提示用户。
    """
    target = os.path.abspath(target_dir or data_dir())
    sources = list(sources if sources is not None else legacy_dirs())

    report = {
        "ran": False,
        "reason": "",
        "target": target,
        "sources": [],
        "copied": [],
        "secrets_removed": [],
        "secrets_failed": [],
        "config_migrated": False,
        "credentials_discarded": False,
    }

    marker = os.path.join(target, MIGRATION_MARKER)

    sources = [
        os.path.abspath(p)
        for p in sources
        if os.path.isdir(p) and os.path.abspath(p) != target
    ]

    if not sources:
        report["reason"] = "no-legacy-dir"

        return report

    if os.path.exists(marker) and not force:
        report["reason"] = "already-migrated"

        return report

    if not ensure_dir(target):
        report["reason"] = "target-unwritable"

        return report

    report["ran"] = True
    report["sources"] = sources

    for old in sources:
        for name in MIGRATED_FILES:
            src = os.path.join(old, name)

            if os.path.isfile(src) and _copy_missing(
                src, os.path.join(target, name)
            ):
                report["copied"].append(name)

                if name == "mclink_config.json":
                    report["config_migrated"] = True

        # 日志：一次迁移，不改名（同名就不覆盖）。
        old_logs = os.path.join(old, LOGS_SUBDIR)

        if os.path.isdir(old_logs):
            for name in sorted(os.listdir(old_logs)):
                src = os.path.join(old_logs, name)

                if not os.path.isfile(src):
                    continue

                if _copy_missing(src, os.path.join(target, LOGS_SUBDIR, name)):
                    report["copied"].append(f"{LOGS_SUBDIR}/{name}")

        # ---- 凭据：清掉，不给新版自动勾选的机会（D11）----
        for name in SECRET_FILES:
            src = os.path.join(old, name)

            if not os.path.isfile(src):
                continue

            report["credentials_discarded"] = True

            try:
                os.remove(src)
                report["secrets_removed"].append(src)
            except OSError:
                # 删不掉（权限/占用）也要说清楚：旧文件还在，但它不会被读取。
                report["secrets_failed"].append(src)
                report["credentials_discarded"] = False

    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(report, ensure_ascii=False, indent=2))
    except Exception:
        pass

    return report


def migrate_user_data(old_dir, new_dir, move_secrets=True):
    """
    用户**主动**在设置界面里换了数据目录：把旧目录的数据搬过去。

    和 `migrate_legacy()` 的区别：这里的"记住密码"是本版本、本位置勾的，
    属于用户当下的意愿，跟着搬走是正确的（搬 = 复制成功后删源，不留副本）。

    非秘密文件（配置/日志/缓存）复制；已存在的同名文件**不覆盖**。
    返回报告字典。
    """
    old = os.path.abspath(old_dir or "")
    new = os.path.abspath(new_dir or "")

    report = {
        "ran": False,
        "reason": "",
        "from": old,
        "to": new,
        "copied": [],
        "secrets_moved": [],
        "secrets_failed": [],
    }

    if not old or old == new or not os.path.isdir(old):
        report["reason"] = "no-source"

        return report

    if is_inside_install_dir(new) or not ensure_dir(new):
        report["reason"] = "target-unwritable"

        return report

    report["ran"] = True

    for name in MOVABLE_ENTRIES:
        src = os.path.join(old, name)

        if os.path.isfile(src):
            if _copy_missing(src, os.path.join(new, name)):
                report["copied"].append(name)

            continue

        if not os.path.isdir(src):
            continue

        for dirpath, _dirnames, filenames in os.walk(src):
            rel = os.path.relpath(dirpath, old)

            for filename in filenames:
                src_file = os.path.join(dirpath, filename)
                dst_file = os.path.normpath(os.path.join(new, rel, filename))

                if _copy_missing(src_file, dst_file):
                    report["copied"].append(
                        os.path.relpath(dst_file, new).replace(os.sep, "/")
                    )

    if move_secrets:
        for name in SECRET_FILES:
            src = os.path.join(old, name)

            if not os.path.isfile(src):
                continue

            moved, removed = _move_secret(src, os.path.join(new, name))

            if moved and removed:
                report["secrets_moved"].append(name)
            else:
                report["secrets_failed"].append(src)

    return report



def ensure_ready():
    """
    进程启动时调一次：解析目录 + （必要时）迁移老数据。

    返回迁移报告（没迁移就是 `{"ran": False, ...}`）。失败绝不抛：
    客户端必须能起来。
    """
    try:
        report = migrate_legacy()

        if report.get("credentials_discarded"):
            _notice(
                "info",
                "检测到旧版本保存的登录信息，已从旧目录清除。\n"
                "按本版口径「记住密码」默认不记 —— 需要记住请在登录窗口"
                "重新勾选一次。",
            )

        return report
    except Exception:
        return {"ran": False, "reason": "error"}


def reset_for_tests():
    """测试用：清掉进程内状态（不碰磁盘上的用户数据）。"""
    global LAST_NOTICE

    LAST_NOTICE = None


__all__ = [
    "APP_DIR_NAME",
    "LEGACY_APP_DIR_NAMES",
    "ENV_DATA_DIR",
    "POINTER_FILENAME",
    "platform_base",
    "default_dir",
    "install_dir",
    "fallback_dir",
    "is_inside_install_dir",
    "pointer_path",
    "data_dir",
    "logs_dir",
    "cache_dir",
    "config_path",
    "config_candidates",
    "set_data_dir",
    "clear_data_dir",
    "describe",
    "legacy_dirs",
    "migrate_legacy",
    "migrate_user_data",
    "ensure_ready",
    "pop_notice",
    "ensure_dir",
    "reset_for_tests",
]
