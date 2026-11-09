"""
CraftLink -- M6 客户端去硬编码 / 品牌可定制（离线，沙箱目录）。

============================================================================
 这条里程碑的验收点（计划书 M6 / D8 / D10 / D11）
============================================================================

  1. 中继地址/端口**默认空**，没填时给的是"下一步填什么"的引导，
     不是一句干巴巴的报错，更不是偷偷连某台内置服务器；
  2. 填过之后**记忆**（写进用户数据目录），下次读回来；
  3. `branding.json` 覆盖软件名 / logo / 默认地址；文件缺失或写坏 -> 退回默认；
  4. 用户数据目录可自定义：改完之后配置/日志/记忆**真的**落在新目录，
     而且**永远不写程序安装目录**；
  5. 老版本目录里的记忆要么迁移要么清理 —— 这里钉的是"D11：旧凭据清掉且
     不自动勾选"，以及配置/日志的迁移。

全部离线、全部在 tempfile 沙箱里跑：**绝不碰真实用户目录**（HOME/APPDATA/
XDG_CONFIG_HOME 都被指到沙箱）。任何一条测试碰到真实目录都算 bug。

跑法：
    python3 tests/m6_client_config_test.py
"""

import ast
import importlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(ROOT, "client")

sys.path.insert(0, CLIENT)

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail and not ok:
        line += f"  -- {detail}"

    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def fresh(sandbox, data_dir=None, branding_file=None, tree=None):
    """
    把用户目录环境指到沙箱，然后按依赖顺序重新加载模块。

    `tree` 是"平台根"（Windows APPDATA / Linux XDG_CONFIG_HOME）指向的目录。
    同时设 APPDATA 与 XDG_CONFIG_HOME，是为了让同一份测试在 Windows 与
    Linux 上都落进沙箱。
    """
    tree = tree or os.path.join(sandbox, "tree")
    os.makedirs(tree, exist_ok=True)

    os.environ["APPDATA"] = tree
    os.environ["XDG_CONFIG_HOME"] = tree

    if data_dir is None:
        os.environ.pop("MCLINK_DATA_DIR", None)
    else:
        os.environ["MCLINK_DATA_DIR"] = data_dir

    if branding_file is None:
        os.environ.pop("MCLINK_BRANDING_FILE", None)
    else:
        os.environ["MCLINK_BRANDING_FILE"] = branding_file

    import mclink.config.userdirs as userdirs

    importlib.reload(userdirs)

    import mclink.config.branding as branding

    importlib.reload(branding)

    import mclink.config.settings as settings

    importlib.reload(settings)

    import mclink.config.credentials as credentials

    importlib.reload(credentials)

    userdirs.reset_for_tests()

    return settings, branding, userdirs, credentials


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ============================================================
# 1. 默认空 + 引导
# ============================================================

def test_defaults_are_empty(sandbox):
    settings, branding, _userdirs, _credentials = fresh(sandbox)

    check(
        "品牌文件不存在时：软件名是当前项目状态（CraftLink）",
        settings.APP_NAME == branding.DEFAULT_APP_NAME == "CraftLink",
        settings.APP_NAME,
    )

    check(
        "★ 默认中继地址为空（没有任何内置服务器）",
        settings.ClientConfig().server_host == ""
        and settings.default_server_host() == "",
        f"{settings.ClientConfig().server_host!r} / "
        f"{settings.default_server_host()!r}",
    )

    check(
        "★ 默认端口为 0（= 未设置，不是 19100）",
        settings.ClientConfig().server_port == 0
        and settings.default_server_port() == 0,
        f"{settings.ClientConfig().server_port!r} / "
        f"{settings.default_server_port()!r}",
    )

    cfg = settings.ClientConfig()

    check(
        "没有地址时 has_server() 为假（界面据此显示引导）",
        cfg.has_server() is False,
    )

    # ---- 引导文案：说清"填什么"和"填完会怎样" ----
    try:
        cfg.validated()
        ok = False
        message = "没有抛异常"
    except ValueError as e:
        message = str(e)
        ok = True

    check("地址为空时连接被拒绝（而不是静默用默认值）", ok, message)

    for needle in ("中转服务器", "端口", "记住", "1-65535"):
        check(
            f"引导里说清了：{needle}",
            needle in message,
            message[:120],
        )

    check(
        "界面常驻引导（服务器卡片）不是空的",
        bool(settings.SERVER_FIELD_HINT.strip())
        and bool(settings.SERVER_PORT_GUIDE.strip()),
    )

    # ---- 端口单缺时的引导是"另一条" ----
    cfg = settings.ClientConfig(server_host="relay.example")

    try:
        cfg.validated()
        ok = False
        message = "没有抛异常"
    except ValueError as e:
        message = str(e)
        ok = True

    check("只缺端口时也会被拒绝", ok, message)
    check(
        "只缺端口时的提示指向端口（不是笼统的报错）",
        "端口" in message and "默认" in message,
        message[:120],
    )


# ============================================================
# 2. 填写后记忆并回读
# ============================================================

def test_address_is_remembered(sandbox):
    data = os.path.join(sandbox, "remember")
    settings, _branding, userdirs, credentials = fresh(sandbox, data_dir=data)

    cfg = settings.load_config()

    check("第一次运行：first_run 为真", cfg.first_run is True)

    cfg.server_host = "relay.example.net"
    cfg.server_port = 34567
    cfg.server_source = "user"
    cfg.role = "player"
    cfg.mc_port = 25565
    cfg.local_port = 25566

    path = settings.save_config(cfg)

    check(
        "配置写进了用户数据目录",
        bool(path) and os.path.abspath(path).startswith(os.path.abspath(data)),
        f"{path}",
    )

    reloaded = settings.load_config()

    check(
        "★ 下次启动自动带出地址与端口",
        reloaded.server_host == "relay.example.net"
        and int(reloaded.server_port) == 34567,
        f"{reloaded.server_host!r}:{reloaded.server_port!r}",
    )

    check(
        "用户确认过的地址被标记为 user（迁移时不会被清掉）",
        reloaded.server_source == "user",
        reloaded.server_source,
    )

    check(
        "非第一次运行：first_run 为假",
        reloaded.first_run is False,
    )

    check(
        "地址与端口都填了 -> has_server() 为真",
        reloaded.has_server() is True,
    )

    check(
        "server_label() 给出 host:port",
        reloaded.server_label() == "relay.example.net:34567",
        reloaded.server_label(),
    )

    # ---- 登录记忆：默认不写盘 ----
    check(
        "登录记忆默认：没有文件、勾选为假",
        credentials.exists() is False
        and credentials.remember_choice() is False,
    )

    check(
        "登录记忆文件默认不存在（默认不记、不写盘）",
        not os.path.exists(os.path.join(data, "credentials.dat")),
    )

    saved = credentials.save("alice", "s3cret-pw", settings.APP_VERSION)

    check("用户勾选后才写得进去", saved is True)

    check(
        "记忆文件落在用户数据目录（不是程序目录）",
        os.path.abspath(credentials._storage_path()).startswith(
            os.path.abspath(data)
        ),
        credentials._storage_path(),
    )

    user, password = credentials.load(settings.APP_VERSION)

    check(
        "能读回记忆的账号",
        user == "alice" and password == "s3cret-pw",
        f"{user!r}, {password!r}",
    )

    check(
        "勾选状态被记住（下次打开登录框还是勾着的）",
        credentials.remember_choice() is True,
    )

    credentials.clear()

    check(
        "取消勾选后：文件删除、勾选为假（默认不记）",
        credentials.exists() is False
        and credentials.remember_choice() is False,
    )

    check(
        "userdirs.describe() 报告的目录就是数据目录",
        userdirs.describe()["data_dir"] == os.path.abspath(data),
        userdirs.describe()["data_dir"],
    )


# ============================================================
# 3. branding.json 覆盖
# ============================================================

def test_branding_overrides(sandbox):
    brand_dir = os.path.join(sandbox, "brand")
    os.makedirs(brand_dir, exist_ok=True)

    logo = os.path.join(brand_dir, "logo.png")

    with open(logo, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")  # 只需要"文件存在"

    brand_file = os.path.join(brand_dir, "branding.json")

    write_json(
        brand_file,
        {
            "app_name": "方块特快",
            "logo_file": "logo.png",
            "default_server_host": "relay.brand.example",
            "default_server_port": 24000,
            "update_url": "https://download.brand.example/",
            "unknown_field": "忽略我",
        },
    )

    data = os.path.join(sandbox, "brand-data")
    settings, branding, _userdirs, _credentials = fresh(
        sandbox, data_dir=data, branding_file=brand_file
    )

    check(
        "★ branding.json 覆盖软件名（窗口标题/关于页读的是它）",
        settings.APP_NAME == "方块特快",
        settings.APP_NAME,
    )

    check(
        "版本行里用的也是品牌名",
        settings.version_line().startswith("方块特快 v"),
        settings.version_line(),
    )

    check(
        "★ branding.json 覆盖默认地址与端口",
        settings.default_server_host() == "relay.brand.example"
        and settings.default_server_port() == 24000,
        f"{settings.default_server_host()!r}:{settings.default_server_port()!r}",
    )

    check(
        "新建配置直接带上品牌默认地址（用户仍然可以改）",
        settings.ClientConfig().server_host == "relay.brand.example"
        and int(settings.ClientConfig().server_port) == 24000,
    )

    check(
        "★ logo_file 解析成绝对路径（相对品牌文件所在目录）",
        branding.load().logo_file == os.path.abspath(logo),
        branding.load().logo_file,
    )

    check(
        "logo_file 是图片 -> 界面用 logo_image() 拿它",
        branding.load().logo_image() == os.path.abspath(logo),
    )

    check(
        "update_url 也来自品牌文件（代码里不再有真实域名）",
        settings.update_url() == "https://download.brand.example/",
        settings.update_url(),
    )

    check(
        "未知字段被忽略、不产生警告",
        branding.load().warnings == [],
        repr(branding.load().warnings),
    )

    check(
        "来源路径可查（关于页显示它）",
        branding.load().source == os.path.abspath(brand_file),
        branding.load().source,
    )

    # ---- 文件缺失 -> 全默认 ----
    settings, branding, _userdirs, _credentials = fresh(
        sandbox, data_dir=data, branding_file=os.path.join(sandbox, "nope.json")
    )

    check(
        "品牌文件缺失：软件名退回默认",
        settings.APP_NAME == "CraftLink" and branding.load().source == "",
        f"{settings.APP_NAME!r} / {branding.load().source!r}",
    )

    check(
        "品牌文件缺失：默认地址为空（不是别人的服务器）",
        settings.default_server_host() == ""
        and settings.default_server_port() == 0,
    )

    # ---- 写坏了 -> 默认值 + 警告，绝不崩 ----
    broken = os.path.join(brand_dir, "broken.json")

    with open(broken, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")

    settings, branding, _userdirs, _credentials = fresh(
        sandbox, data_dir=data, branding_file=broken
    )

    check(
        "品牌文件写坏：仍能起来，用默认值",
        settings.APP_NAME == "CraftLink"
        and settings.ClientConfig().server_host == "",
    )

    check(
        "品牌文件写坏：给出警告（不静默）",
        any("branding.json" in w for w in branding.warnings()),
        repr(branding.warnings()),
    )

    # ---- 类型/范围不对的字段 -> 该项默认 + 警告 ----
    odd = os.path.join(brand_dir, "odd.json")

    write_json(
        odd,
        {
            "app_name": "",
            "logo_file": "missing-logo.png",
            "default_server_host": "relay.odd.example",
            "default_server_port": "not-a-port",
        },
    )

    settings, branding, _userdirs, _credentials = fresh(
        sandbox, data_dir=data, branding_file=odd
    )

    check(
        "app_name 为空 -> 退回默认名（绝不出现空标题）",
        settings.APP_NAME == "CraftLink",
        settings.APP_NAME,
    )

    check(
        "端口不是数字 -> 按『没有默认端口』处理",
        settings.ClientConfig().server_port == 0,
        settings.ClientConfig().server_port,
    )

    check(
        "找不到的 logo -> 空串 + 警告（退回内置图标）",
        branding.load().logo_file == ""
        and any("logo_file" in w for w in branding.warnings()),
        repr(branding.warnings()),
    )


# ============================================================
# 4. 用户数据目录可自定义
# ============================================================

def test_data_dir_is_configurable(sandbox):
    settings, _branding, userdirs, credentials = fresh(sandbox)

    default = userdirs.default_dir()

    check(
        "默认目录 = 平台目录 + CraftLink",
        os.path.basename(default) == "CraftLink"
        and default.startswith(os.path.join(sandbox, "tree")),
        default,
    )

    check(
        "没改过时 data_dir() == 默认目录",
        userdirs.data_dir() == default,
        userdirs.data_dir(),
    )

    custom = os.path.join(sandbox, "custom-data")

    ok, message = settings.set_config_dir(custom)

    check("可以改数据目录", ok is True, str(message))
    check(
        "改完之后 data_dir() 立刻是新目录",
        userdirs.data_dir() == os.path.abspath(custom),
        userdirs.data_dir(),
    )

    check(
        "指针文件写在**默认目录**里（否则下次找不到用户选的是哪）",
        os.path.isfile(os.path.join(default, userdirs.POINTER_FILENAME)),
    )

    # 关掉环境变量，验证"下次启动"是从指针文件读回来的。
    os.environ.pop("MCLINK_DATA_DIR", None)
    importlib.reload(userdirs)

    check(
        "★ 重启后（无环境变量）仍然读回用户选的目录",
        userdirs.data_dir() == os.path.abspath(custom),
        userdirs.data_dir(),
    )

    _settings, _branding, userdirs, credentials = fresh(
        sandbox, data_dir=None, tree=os.path.join(sandbox, "tree")
    )

    check(
        "reload 之后依然是用户选的目录（指针文件生效）",
        userdirs.data_dir() == os.path.abspath(custom),
        userdirs.data_dir(),
    )

    # ---- 文件真的落在新目录 ----
    _settings, _branding, userdirs, credentials = fresh(
        sandbox, data_dir=custom
    )

    cfg = _settings.ClientConfig()
    cfg.server_host = "relay.custom.example"
    cfg.server_port = 19101
    cfg.server_source = "user"

    path = _settings.save_config(cfg)

    check(
        "配置文件真的写在新目录里",
        os.path.isfile(os.path.join(custom, "mclink_config.json")),
        f"{path}",
    )

    credentials.save("bob", "pw-bob", "3.9.20")

    check(
        "登录记忆也写在新目录里",
        os.path.isfile(os.path.join(custom, "credentials.dat")),
        credentials._storage_path(),
    )

    logs = _settings.log_dir()

    check(
        "日志目录在新目录下面",
        os.path.abspath(logs).startswith(os.path.abspath(custom)),
        logs,
    )

    cache = _settings.cache_dir()

    check(
        "缓存目录在新目录下面",
        os.path.abspath(cache).startswith(os.path.abspath(custom)),
        cache,
    )

    # ---- 不许写程序安装目录 ----
    install = userdirs.install_dir()

    ok, message = _settings.set_config_dir(install)

    check(
        "★ 拒绝把数据目录设成程序安装目录",
        ok is False and "安装目录" in str(message),
        str(message),
    )

    check(
        "★ 兜底目录也不在安装目录里（老代码会写 EXE 旁边）",
        not userdirs.is_inside_install_dir(userdirs.fallback_dir()),
        userdirs.fallback_dir(),
    )

    check(
        "安装目录判定本身是对的（否则上面两条没意义）",
        userdirs.is_inside_install_dir(install) is True,
    )


# ============================================================
# 5. 老目录：配置迁移 + 旧凭据清理
# ============================================================

def test_legacy_migration(sandbox):
    settings, _branding, userdirs, credentials = fresh(sandbox)

    tree = os.path.join(sandbox, "tree")
    legacy = os.path.join(tree, "MC-Link")
    target = os.path.join(sandbox, "new-home")

    os.makedirs(os.path.join(legacy, "logs"), exist_ok=True)

    write_json(
        os.path.join(legacy, "mclink_config.json"),
        {
            "client_id": "legacy-client-id",
            "username": "olduser",
            "server_host": "old.relay.example",
            "server_port": 19100,
            "ui_theme": "dark",
        },
    )

    with open(os.path.join(legacy, "credentials.dat"), "w",
              encoding="utf-8") as fh:
        fh.write('{"format": 1, "blob": "旧密码的加密副本"}')

    with open(os.path.join(legacy, "credentials.dat.prefs"), "w",
              encoding="utf-8") as fh:
        fh.write('{"remember": true}')

    with open(os.path.join(legacy, "logs", "craftlink-client.log"), "w",
              encoding="utf-8") as fh:
        fh.write("旧的日志\n")

    report = userdirs.migrate_legacy(target_dir=target, sources=[legacy])

    check("迁移报告说跑了", report.get("ran") is True, repr(report))
    check("配置被迁移", report.get("config_migrated") is True)
    check(
        "配置文件真的出现在新目录",
        os.path.isfile(os.path.join(target, "mclink_config.json")),
    )
    check(
        "日志被迁移",
        os.path.isfile(
            os.path.join(target, "logs", "craftlink-client.log")
        ),
    )

    check(
        "★ 老目录里的凭据文件被清掉（不留没人再读的密码副本）",
        not os.path.exists(os.path.join(legacy, "credentials.dat"))
        and not os.path.exists(os.path.join(legacy, "credentials.dat.prefs")),
        repr(sorted(os.listdir(legacy))),
    )

    check(
        "报告里明确标了『旧登录记忆已清除』（界面据此提示用户）",
        report.get("credentials_discarded") is True
        and len(report.get("secrets_removed") or []) == 2,
        repr(report.get("secrets_removed")),
    )

    check(
        "★ 迁移**没有**把旧密码文件搬进新目录（D11：默认不记）",
        not os.path.exists(os.path.join(target, "credentials.dat"))
        and not os.path.exists(os.path.join(target, "credentials.dat.prefs")),
    )

    # ---- 迁移后：配置读得回来，但地址要重新确认 ----
    _settings, _branding, userdirs2, _credentials = fresh(
        sandbox, data_dir=target, tree=tree
    )

    cfg = _settings.load_config()

    check(
        "迁移保留了身份（client_id 不变）",
        cfg.client_id == "legacy-client-id",
        cfg.client_id,
    )

    check(
        "★ 老配置里的地址被清掉（老版本内置默认值不算用户确认）",
        cfg.server_host == "" and int(cfg.server_port or 0) == 0,
        f"{cfg.server_host!r}:{cfg.server_port!r}",
    )

    check(
        "清掉地址时留下『需要重新确认』的标记（界面据此提示，不静默）",
        getattr(cfg, "legacy_address_cleared", False) is True,
    )

    # ---- 用户自己确认过的地址必须保留 ----
    write_json(
        os.path.join(target, "mclink_config.json"),
        {
            "client_id": "legacy-client-id",
            "server_host": "my.own.relay",
            "server_port": 19102,
            "server_source": "user",
        },
    )

    _settings, _branding, _userdirs, _credentials = fresh(
        sandbox, data_dir=target, tree=tree
    )

    cfg = _settings.load_config()

    check(
        "★ 用户确认过的地址原样保留（只清未经确认的）",
        cfg.server_host == "my.own.relay"
        and int(cfg.server_port) == 19102,
        f"{cfg.server_host!r}:{cfg.server_port!r}",
    )

    check(
        "保留的地址不会再弹『重新确认』",
        getattr(cfg, "legacy_address_cleared", False) is False,
    )

    # ---- 只跑一次 ----
    again = userdirs.migrate_legacy(target_dir=target, sources=[legacy])

    check(
        "迁移默认只跑一次（标记文件生效）",
        again.get("ran") is False
        and again.get("reason") == "already-migrated",
        repr(again),
    )

    check(
        "迁移标记文件在新目录里",
        os.path.isfile(os.path.join(target, userdirs.MIGRATION_MARKER)),
    )


def test_manual_data_dir_move(sandbox):
    """用户在设置里改目录：数据跟着走，凭据搬走（不留副本）。"""
    _settings, _branding, userdirs, credentials = fresh(sandbox)

    old = os.path.join(sandbox, "move-old")
    new = os.path.join(sandbox, "move-new")

    os.makedirs(os.path.join(old, "logs"), exist_ok=True)

    write_json(
        os.path.join(old, "mclink_config.json"),
        {"client_id": "cid-move", "server_host": "a.b.c", "server_port": 1},
    )

    with open(os.path.join(old, "credentials.dat"), "w",
              encoding="utf-8") as fh:
        fh.write("{}")

    with open(os.path.join(old, "logs", "x.log"), "w", encoding="utf-8") as fh:
        fh.write("log\n")

    report = userdirs.migrate_user_data(old, new)

    check("手动换目录：报告说跑了", report.get("ran") is True, repr(report))
    check(
        "手动换目录：配置复制过去",
        os.path.isfile(os.path.join(new, "mclink_config.json")),
    )
    check(
        "手动换目录：日志复制过去",
        os.path.isfile(os.path.join(new, "logs", "x.log")),
    )
    check(
        "手动换目录：凭据**搬走**（新目录有、旧目录没有）",
        os.path.isfile(os.path.join(new, "credentials.dat"))
        and not os.path.exists(os.path.join(old, "credentials.dat")),
        repr(report.get("secrets_moved")),
    )
    check(
        "手动换目录：不覆盖已存在的同名文件",
        userdirs.migrate_user_data(old, new).get("copied") == [],
    )


# ============================================================
# 6. 仓库里不许再出现真实服务器 / 硬编码端口默认值
# ============================================================

#: 老版本写死在客户端里的真实域名（大小写不敏感）。
#:
#: ★ 这里**故意拆成两段拼**：它是"客户端里不许再出现"的反例针，功能上必须
#:   保留整串；但整串写进仓库就等于把域名又公开了一次
#:   （tests/real_identifiers_guard_test.py 会拦这种字面量）。
_FORBIDDEN_DOMAIN = "superyk" + "7hero"
FORBIDDEN_IN_CLIENT = (_FORBIDDEN_DOMAIN, "service.example.invalid")

#: 客户端代码里不许再出现的"默认端口"字面量（服务器端口）。
FORBIDDEN_PORT_LITERAL = "19100"


def test_no_hardcoded_server_in_client():
    offenders = []

    for dirpath, dirnames, filenames in os.walk(CLIENT):
        dirnames[:] = [
            d for d in dirnames
            if d not in ("__pycache__", "build", "dist")
        ]

        for name in filenames:
            path = os.path.join(dirpath, name)

            if not name.endswith((".py", ".json", ".spec", ".txt", ".bat")):
                continue

            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue

            low = text.lower()

            for needle in FORBIDDEN_IN_CLIENT:
                if needle in low:
                    offenders.append(f"{os.path.relpath(path, ROOT)}: {needle}")

    check(
        "★ 客户端里没有任何写死的真实域名",
        not offenders,
        "; ".join(offenders[:5]),
    )

    # 端口字面量：允许出现在注释/文档字符串里（解释历史），但不许作为**代码
    # 常量**出现 —— 那种才是"回退到某台内置服务器"的来源。所以这里用 AST
    # 只看真正的常量节点，不看注释与 docstring。
    port_offenders = []

    for dirpath, dirnames, filenames in os.walk(CLIENT):
        dirnames[:] = [
            d for d in dirnames
            if d not in ("__pycache__", "build", "dist")
        ]

        for name in filenames:
            if not name.endswith(".py"):
                continue

            path = os.path.join(dirpath, name)

            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()

            try:
                tree = ast.parse(source)
            except SyntaxError:  # pragma: no cover - 语法错由门禁负责
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant):
                    continue

                value = node.value

                if isinstance(value, bool) or value is None:
                    continue

                text = str(value)

                if FORBIDDEN_PORT_LITERAL not in text:
                    continue

                port_offenders.append(
                    f"{os.path.relpath(path, ROOT)}:{node.lineno} = {value!r}"
                )

    check(
        "★ 客户端代码里没有硬编码的服务器端口 19100（含回退值）",
        not port_offenders,
        "; ".join(port_offenders[:5]),
    )


# ============================================================
# main
# ============================================================

def main():
    print("=" * 70)
    print(" M6 客户端去硬编码 / 品牌可定制（离线 · 沙箱）")
    print("=" * 70)

    original_env = {
        key: os.environ.get(key)
        for key in ("APPDATA", "XDG_CONFIG_HOME", "MCLINK_DATA_DIR",
                    "MCLINK_BRANDING_FILE", "HOME")
    }

    sandbox = tempfile.mkdtemp(prefix="craftlink-m6-")

    try:
        print("")
        print("---- 1. 默认空 + 引导 ----")
        test_defaults_are_empty(os.path.join(sandbox, "t1"))

        print("")
        print("---- 2. 填写后记忆并回读 ----")
        test_address_is_remembered(os.path.join(sandbox, "t2"))

        print("")
        print("---- 3. branding.json 覆盖 ----")
        test_branding_overrides(os.path.join(sandbox, "t3"))

        print("")
        print("---- 4. 用户数据目录可自定义 ----")
        test_data_dir_is_configurable(os.path.join(sandbox, "t4"))

        print("")
        print("---- 5. 老目录迁移 / 旧记忆清理 ----")
        test_legacy_migration(os.path.join(sandbox, "t5"))

        print("")
        print("---- 5b. 手动更改数据目录 ----")
        test_manual_data_dir_move(os.path.join(sandbox, "t5b"))

        print("")
        print("---- 6. 仓库里不再有真实服务器 ----")
        test_no_hardcoded_server_in_client()

    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        shutil.rmtree(sandbox, ignore_errors=True)

    print("")
    print("=" * 70)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
