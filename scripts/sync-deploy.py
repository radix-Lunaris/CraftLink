#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sync the deploy package from the source tree.

============================================================================
 WHY THIS EXISTS
============================================================================

The deploy package shipped a README with a hand written upload list, and that
list went stale three separate times in one evening:

  * gatekeeper.py was never uploaded, so renames and LOGOUT silently used old
    code for hours
  * docker-compose.yml with placeholder credentials was uploaded, which
    overwrote the working database password
  * db_schema.py was MISSING FROM THE LIST, so the relay crash-looped on
    `ImportError: cannot import name 'ROLE_BACKFILL'`

A list maintained by hand will keep failing. This script replaces it: the
deploy tree is now generated, and the README's list is generated from it.

Run:
    python scripts/sync-deploy.py
"""

import io
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEPLOY = os.path.join(ROOT, "deploy")

#: (source path relative to ROOT, destination relative to DEPLOY)
#
# Every server .py is included by globbing rather than listing, because a
# hand written file list is exactly what caused the outage above.
SERVER_EXTRA = ("Dockerfile", "requirements.txt")

PANEL_TREES = (
    "lib",
    "web",
    # M4：自带 nginx 的所有权都在这两个目录里 ——
    #   bin/   根代理（渲染 / nginx -t / 热加载 / openssl / certbot）+ CLI
    #   nginx/ 渲染模板 + 静态主配置（nginx-main.conf、nginx-test.conf.template）
    # 漏掉它们的后果不是"少个文件"，而是容器起来之后渲染不出配置、
    # 面板只能停在构建期那份纯 HTTP 默认配置上。
    "bin",
    "nginx",
)

#: Credentials live in the deploy copies only.
SECRET_KEYS = (
    "MCLINK_DB_PASSWORD",
    "MCLINK_ADMIN_API_TOKEN",
    "MCLINK_BOOTSTRAP_ADMIN",
)


def log(message):
    print(message, flush=True)


def copy_tree(source, destination):
    """
    Copy a tree into the deploy package.

    Returns the list of files copied, as paths relative to `source`. The list
    matters: it is what the README's upload list is built from, and a tree that
    is copied but not listed is a tree the user will not upload. That is
    exactly how the panel views (dashboard.php and friends) were missing from
    the list while sitting in the package.
    """
    if not os.path.isdir(source):
        log(f"  [跳过] 没有 {os.path.relpath(source, ROOT)}")
        return []

    copied = []

    for dirpath, dirnames, filenames in os.walk(source):
        # Never ship caches.
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]

        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue

            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, source)

            dst = os.path.join(destination, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            shutil.copy2(src, dst)
            copied.append(rel)

    return sorted(copied)


def keep_credentials(source_compose, deploy_compose):
    """
    Regenerate the deploy compose from the source one, carrying the REAL
    credentials across.

    The source composes deliberately contain Chinese placeholders, because the
    repository must not hold secrets. Uploading one of those placeholders
    overwrites the working password on the server -- which is what took the
    database down once. So the credentials already present in the deploy copy
    are read first and written back over the placeholders.
    """
    def read(path):
        try:
            return io.open(path, encoding="utf-8").read()
        except OSError:
            return ""

    import re

    def value(text, key):
        match = re.search(r"^\s*" + key + r"\s*:\s*(.*?)\s*$", text, re.M)
        return match.group(1).strip().strip('"').strip("'") if match else ""

    deploy_text = read(deploy_compose)
    source_text = read(source_compose)

    if not source_text:
        return 0

    carried = 0

    for key in SECRET_KEYS:
        real = value(deploy_text, key)

        # BOOTSTRAP_ADMIN is special: it only matters on the very first boot,
        # and the account already exists by now. Leaving the repository's
        # Chinese placeholder in place would trip the pre-flight check for no
        # reason, so it is deliberately blanked. Setting it again is a one line
        # edit on the server if a fresh install ever needs it.
        if key == "MCLINK_BOOTSTRAP_ADMIN":
            source_text = re.sub(
                r"^(\s*" + key + r"\s*:).*$",
                lambda m: f'{m.group(1)} ""',
                source_text,
                flags=re.M,
            )
            continue

        # An empty value means there is nothing to carry across.
        if not real:
            continue

        # Refuse to carry a placeholder forward.
        if any(ord(c) > 126 for c in real):
            continue

        source_text = re.sub(
            r"^(\s*" + key + r"\s*:).*$",
            lambda m: f'{m.group(1)} "{real}"',
            source_text,
            flags=re.M,
        )
        carried += 1

    os.makedirs(os.path.dirname(deploy_compose), exist_ok=True)

    with io.open(deploy_compose, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(source_text)

    return carried


def write_version_file():
    """
    把客户端的 APP_VERSION 写成 deploy/server/VERSION。

    这是"客户端与服务端必须同版本"的**唯一数据来源**：中继启动时读它
    （relay_config.RELAY_VERSION），客户端握手行带 `ver=<APP_VERSION>`，
    不一致直接拒。

    必须在上传清单和指纹**之前**写，否则它进了包里却不在清单上 —— 服务器
    上那个文件就不会被传上去，中继读到空 = 版本门永远关着（比不加门更糟）。
    返回相对路径，写不出来时返回 None。
    """
    import re as _re

    source = os.path.join(ROOT, "client", "mclink", "config", "settings.py")

    try:
        with open(source, encoding="utf-8") as fh:
            match = _re.search(r'APP_VERSION\s*=\s*"([^"]+)"', fh.read())
    except Exception as e:
        print(f"  [!] 读不到客户端版本号：{e}")
        return None

    if not match:
        print("  [!] settings.py 里找不到 APP_VERSION")
        return None

    version = match.group(1).strip()

    try:
        with io.open(
            os.path.join(DEPLOY, "server", "VERSION"),
            "w",
            encoding="utf-8",
            newline="\n",
        ) as fh:
            fh.write(version + "\n")
    except Exception as e:
        print(f"  [!] 没能写入 server/VERSION：{e}")
        return None

    print(f"  server/VERSION = {version}")
    return "server/VERSION"


def write_build_stamp(files):
    """
    Write BUILD.txt: sha256 of every file that has to reach the server.

    Paths are relative to the UPLOAD ROOT (/opt/mc-link), so the server can
    verify them with a plain `sha256sum -c BUILD.txt` from that directory.

    Two things are deliberately NOT in here:

      * the exes -- they are for the player's own PC, never uploaded, and a
        missing-file report for them would be noise;
      * BUILD.txt itself, which cannot contain its own hash. An empty or
        missing BUILD.txt disables the check instead of failing it, and the
        per-file symbol checks still run.

    The file is machine-only (no comment lines): `sha256sum -c` is happiest
    with a plain list, and a header that some coreutils versions reject would
    turn into a false failure.
    """
    import hashlib

    skip = {"CraftLink.exe", "CraftLink-console.exe"}

    lines = []

    for relative in sorted(set(files)):
        normalised = relative.replace(os.sep, "/")

        if normalised in skip:
            continue

        path = os.path.join(DEPLOY, normalised)

        if not os.path.isfile(path):
            continue

        digest = hashlib.sha256()

        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)

        lines.append(f"{digest.hexdigest()}  {normalised}")

    stamp = os.path.join(DEPLOY, "BUILD.txt")

    with io.open(stamp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")

    return stamp


def write_readme(files):
    """Regenerate the upload list from what was actually copied."""
    lines = [
        "MC-Link 部署包",
        "=" * 64,
        "本目录结构 = 服务器上的目录结构，照着拖拽就不会放错位置。",
        "",
        "!! 这个 README 是由 scripts/sync-deploy.py 自动生成的 !!",
        "   不要手工编辑 —— 人工维护的清单漏过 db_schema.py，",
        "   直接把中继搞成了重启循环。",
        "",
        "!! compose 文件用本目录里的，不要用仓库里的 !!",
        "   仓库里的密码字段是中文占位符，传上去会覆盖掉真实密码，",
        "   中继会报 'latin-1' codec can't encode characters。",
        "   本目录里的已填好真实凭据。",
        "",
        "",
        "上传统清单（共 %d 个文件）" % len(files),
        "=" * 64,
        "",
    ]

    grouped = {}

    for relative in sorted(files):
        # Forward slashes and the top TWO components as the group, so
        # panel/web/views/*.php is listed under panel/web/views rather than
        # being mixed into one huge "panel" section. Paths arrive with the
        # platform's separator (and sometimes with forward slashes, because
        # the compose entries are written that way), so normalise first --
        # grouping on os.sep alone silently made each forward-slash path its
        # own one-file section.
        relative = relative.replace("\\", "/")
        parts = relative.split("/")
        top = "/".join(parts[:2]) if len(parts) > 2 else parts[0]

        grouped.setdefault(top, []).append(relative)

    for top in sorted(grouped):
        lines.append(f"--- {top} ---")

        for relative in grouped[top]:
            lines.append(f"    {relative}")

        lines.append("")

    lines += [
        "",
        "=" * 64,
        "【推荐】整包覆盖 —— 不要一个一个文件传",
        "=" * 64,
        "",
        "  把 deploy 文件夹里的全部内容（server/、panel/、*.sh、BUILD.txt）",
        "  一次性拖到服务器的 /opt/mc-link/ 下，提示覆盖时选【全部覆盖】。",
        "  目录结构和服务器上完全一样。",
        "",
        "  覆盖完之后，服务器上【只需要一条命令】：",
        "",
        "      bash /opt/mc-link/rebuild.sh",
        "",
        "  不必再先手动修行尾了。rebuild.sh 现在自己会把 /opt/mc-link/*.sh",
        "  的 CRLF 洗掉 —— 以前那两条命令：",
        "",
        "      sed -i 's/\\r$//' /opt/mc-link/*.sh && bash /opt/mc-link/rebuild.sh",
        "",
        "  仍然有效（做了也没坏处），但已经不需要了。CRLF 的坑在于 bash",
        "  只会报 $'\\r': command not found，完全不提真正的原因。",
        "",
        "  为什么推荐：逐个文件上传失败过很多次，症状永远是「改了但没生效」",
        "  （web/ 传了、lib/ 没传 → 面板整站 500；index.php 传了、",
        "  lib/common.php 没传 → Call to undefined function）。",
        "  整包覆盖没有这个失败模式：要么每个文件都是新的，要么覆盖时报错。",
        "",
        "  覆盖不会出事的文件：",
        "    panel/config.php           不在包里（服务器上唯一存着数据库密码的",
        "                               文件，永远不要覆盖它）",
        "    server/docker-compose.yml  包里的已填好真实凭据，覆盖是安全的",
        "    panel/docker-compose.yml   同上",
        "",
        "  唯一要注意：覆盖【不会】删除服务器上的多余文件",
        "  （例如以前手工放的 _diag.php、_diag2.php），那些自己删掉即可；",
        "  rebuild.sh 结束时会再提醒一次。",
        "",
        "",
        "上传统清单（共 %d 个文件）" % len(files),
        "=" * 64,
        "",
        "（想单独补某个文件时照这个清单来；路径和服务器上完全一致）",
        "",
        "",
        "重建 / 自证",
        "=" * 64,
        "",
        "    bash /opt/mc-link/rebuild.sh          # 两个容器都重建 + 自证",
        "    bash /opt/mc-link/rebuild.sh panel    # 只重建面板（不断开玩家）",
        "    bash /opt/mc-link/rebuild.sh relay    # 只重建中继（会断开玩家）",
        "",
        "它会自己检查：文件齐不齐（一个一个点名）、凭据是不是占位符、",
        "Dockerfile 有没有 COPY 全部模块、文件真的进容器没有、数据库连上没有、",
        "users.role 列在不在、容器健康不健康、面板能不能打开。",
        "文件指纹对不上时只警告不拦截（要硬性检查加 --strict）。",
        "",
        "指纹自查（纯只读，随时可跑）：",
        "    cd /opt/mc-link && sha256sum -c BUILD.txt",
        "",
        "",
        "全新机器安装（M5）",
        "=" * 64,
        "",
        "把整个包上传到**任意目录**，然后：",
        "",
        "    bash install.sh",
        "",
        "它会：探测本机 Docker 里已有的 MySQL/MariaDB（**有就复用，不新建**）→",
        "交互确认安装根目录（默认 /opt/mc-link，可自定义）/ 面板端口 / URL 安全入口 →",
        "生成 .env（600，凭据随机生成）→ 构建并启动容器 → 打印访问方式。",
        "",
        "  * 幂等：重复跑不换凭据、不重建数据库、不动已有数据；",
        "  * 失败即停：任何一步失败都立刻退出并说清原因，不留半装状态；",
        "  * 路径从脚本自身位置推导（不写死 /opt/mc-link，MCLINK_ROOT 可覆盖）。",
        "",
        "非交互（自动化）：bash install.sh --yes --root /opt/mc-link",
        "",
        "安装完会注册一个 root-only 的 SSH 管理台：",
        "",
        "    sudo mclink            # 菜单：重置安全入口 / 解绑域名 / 改 root 密码",
        "                           #       / 重置会话 / 看状态与日志 / 看凭据",
        "",
        "所有动作都写进 <安装根目录>/logs/mclink-console.log（审计）。",
        "",
        "",
        "跑不起来的时候",
        "=" * 64,
        "",
        "    bash /opt/mc-link/diag-relay.sh     # 中继崩了 / 重启循环",
        "    bash /opt/mc-link/diag-panel.sh     # 面板 500 / 登录失败",
        "",
        "这两个脚本只读，不重启容器、不动数据，会把真实报错打出来。",
        "",
        "如果 bash 报这种错：",
        "    rebuild.sh: line 18: $'\\r': command not found",
        "    syntax error near unexpected token `$'do\\r''",
        "那是文件被存成了 Windows 换行（CRLF），和代码无关。一条命令修好：",
        "",
        "    sed -i 's/\\r$//' /opt/mc-link/*.sh",
        "",
        "（deploy 包里的脚本已确认是 LF；这条只是以防上传工具又转了一次。）",
        "",
        "",
        "SSH 找回根管理密码",
        "=" * 64,
        "",
        "    docker exec -it mc-relay python reset_password.py",
        "",
        "不需要参数。回车 = 自动生成 16 位随机密码（排除 0oO1iIlLq9g）；",
        "输入只允许字母数字，含 @ 这类符号会要求重输；输入 q 取消。",
        "注意 -it 不能省。",
        "",
    ]

    path = os.path.join(DEPLOY, "README.txt")

    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))

    return path


def main():
    log("=" * 60)
    log("同步 deploy 包")
    log("=" * 60)

    os.makedirs(DEPLOY, exist_ok=True)

    copied = []

    # ---- server ----
    log("")
    log("[server]")

    server_src = os.path.join(ROOT, "server")
    server_dst = os.path.join(DEPLOY, "server")

    for name in sorted(os.listdir(server_src)):
        src = os.path.join(server_src, name)

        if not os.path.isfile(src):
            continue

        if name.endswith((".pyc", ".pyo")):
            continue

        if name.endswith(".py") or name in SERVER_EXTRA:
            shutil.copy2(src, os.path.join(server_dst, name))
            copied.append(os.path.join("server", name))
            log(f"  {name}")

    # The diagnostic scripts live in deploy/ already; keep them listed.
    # fix-panel.sh is on this list for the same reason as the rest: it sits in
    # the package, so if it is not on the list it never reaches the server, and
    # the one time the panel broke there was nothing to run.
    for extra in ("check-db.sh", "diag-panel.sh", "diag-relay.sh",
                  "diag-fleet.sh", "fix-panel.sh", "rebuild.sh"):
        candidate = os.path.join(ROOT, "deploy", extra)

        if os.path.isfile(candidate):
            copied.append(extra)

    # ---- M5：安装器 + SSH 管理台 ----
    #
    # 这几个文件必须进包，否则用户拿到 zip 跑 `bash install.sh` 会缺助手脚本。
    # 以前它们是"手工放进 deploy/"的，于是仓库里改了、包里还是旧的（这次
    # install.sh 就是新加的）—— 所以这里统一从**仓库根**复制，deploy/ 只当
    # 生成物（和 HANDOVER.md / BUILD.txt 一样）。
    log("")
    log("[M5 安装器]")

    for relative in (
        "install.sh",
        "docker-compose.db.yml",
        "rebuild.sh",
        "scripts/install_db.py",
        "scripts/install_patch.py",
        "scripts/mclink-console.sh",
        "scripts/ensure_secrets.py",
        "scripts/rotate_secrets.py",
    ):
        src = os.path.join(ROOT, relative)

        if not os.path.isfile(src):
            log(f"  [!] 缺少 {relative} —— 交付包里不会有它")
            continue

        dst = os.path.join(DEPLOY, relative)

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

        if relative not in copied:
            copied.append(relative)

        log(f"  {relative}")

    # ---- M8：开源仓库文件（进交付包）----
    #
    # D7：`LICENSE`（GPLv3）必须**同时**进仓库与交付包；`README.html` 是给
    # 新用户 / 未来开发者的图形化说明书（含 3.9.20→新版升级章），也随包走；
    # `CONTRIBUTING.md` / `SECURITY.md` / `CODE_OF_CONDUCT.md` 同理 ——
    # 用户在服务器上打开包就能看到这些规矩，不必回仓库翻。
    #
    # 它们必须进 `copied`，否则 deploy/README.txt 的上传清单与
    # deploy/BUILD.txt 的指纹都会漏掉它们（服务器上 `sha256sum -c` 会报
    # "没有这个文件"）。`optional/` **不在**这里：它不随包发布。
    log("")
    log("[M8 开源文件]")

    for relative in (
        "LICENSE",
        "README.html",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
    ):
        src = os.path.join(ROOT, relative)

        if not os.path.isfile(src):
            log(f"  [!] 缺少 {relative} —— 交付包里不会有它")
            continue

        shutil.copy2(src, os.path.join(DEPLOY, relative))
        copied.append(relative)
        log(f"  {relative}")

    # ---- composes, keeping credentials ----
    log("")
    log("[compose 凭据]")

    for relative in ("server/docker-compose.yml", "panel/docker-compose.yml"):
        carried = keep_credentials(
            os.path.join(ROOT, relative),
            os.path.join(DEPLOY, relative),
        )
        copied.append(relative)

        log(f"  {relative}: 保留 {carried} 项真实凭据")

    # ---- panel ----
    log("")
    log("[panel]")

    for tree in PANEL_TREES:
        names = copy_tree(
            os.path.join(ROOT, "panel", tree),
            os.path.join(DEPLOY, "panel", tree),
        )
        log(f"  {tree}/: {len(names)} 个文件")

        # EVERY copied file goes into the upload list. Listing only index.php
        # and common.php is how the panel's views/ (which hold the actual
        # pages) stayed out of the README while being present in the package.
        for name in names:
            copied.append(os.path.join("panel", tree, name))

    # The panel Dockerfile and its friends are not secrets and rarely change,
    # but shipping them keeps the package self contained.
    for name in ("Dockerfile", "php-fpm-pool.conf",
                 "supervisord.conf", "config.sample.php"):
        src = os.path.join(ROOT, "panel", name)

        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(DEPLOY, "panel", name))
            copied.append(os.path.join("panel", name))

    # ---- client ----
    log("")
    log("[client]")

    for tree in ("mclink",):
        names = copy_tree(
            os.path.join(ROOT, "client", tree),
            os.path.join(DEPLOY, "client", tree),
        )
        log(f"  {tree}/: {len(names)} 个文件")

        # The client source is shipped for reference/diagnostics. It is NOT
        # part of the upload list (the exe is what people run), so it is
        # deliberately not added to `copied`.
        del names

    for name in ("mclink_diag.py",):
        src = os.path.join(ROOT, "client", name)

        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(DEPLOY, "client", name))

    # The exes are built separately; only report whether they are there.
    # Both of them: the windowed one people run, and the console one used to
    # debug "the exe closes immediately", which is useless if it is not listed
    # and therefore never uploaded.
    for name in ("CraftLink.exe", "CraftLink-console.exe"):
        exe = os.path.join(DEPLOY, name)

        if os.path.isfile(exe):
            copied.append(name)
            log(f"  {name}: 已存在（{os.path.getsize(exe) // 1024} KB）")

    if not os.path.isfile(os.path.join(DEPLOY, "CraftLink.exe")):
        log("  [!] CraftLink.exe 还没有构建，先跑 pyinstaller")

    # ---- 交接文档：让服务器上永远有一份最新的 HANDOVER ----
    #
    # 用户 2026-09-29 的要求：交付包（用户整包上传的 deploy/）里要带一份
    # 最新的交接文档，这样新会话/新接手的人**在服务器上**就能读到
    # "这一版是怎么部署、怎么排查"的说明，不必再去翻仓库。
    #
    # 源文件是 docs/HANDOVER.md（唯一权威），这里只做复制：
    # deploy/HANDOVER.md 是**生成物，禁止手改**（和 README.txt / BUILD.txt 一样）。
    # 它必须进 `copied`，否则 README 的清单和 BUILD.txt 的指纹都会漏掉它，
    # 服务器上就不会有这个文件（check_deploy.py 会因此变红）。
    log("")
    log("[交接文档]")

    handover_src = os.path.join(ROOT, "docs", "HANDOVER.md")

    if os.path.isfile(handover_src):
        shutil.copy2(handover_src, os.path.join(DEPLOY, "HANDOVER.md"))
        copied.append("HANDOVER.md")
        log(
            "  docs/HANDOVER.md -> deploy/HANDOVER.md"
            f"（{os.path.getsize(handover_src) // 1024} KB）"
        )
    else:
        log("  [!] 找不到 docs/HANDOVER.md —— 交付包里不会有交接文档")

    # ---- 上传指纹：让服务器自己说"哪个文件没传上来" ----
    #
    # The failure this prevents, measured in hours: `web/` was uploaded and
    # `lib/` was not, so index.php called no_cache_headers() -- a function that
    # only exists in the new common.php -- and the whole panel answered 500
    # with "Call to undefined function". Every check said OK, because the old
    # common.php was a real file with real content.
    #
    # Symbol checks catch that particular case. A hash list catches ALL of
    # them: the server hashes what it received and names the files that differ,
    # before it rebuilds anything.
    # 版本门：先把 deploy/server/VERSION 落到磁盘，再生成指纹和清单 ——
    # 顺序反了它就会漏在清单外，服务器上就永远没有这个文件。
    version_file = write_version_file()

    if version_file:
        copied.append(version_file)

    stamp = write_build_stamp(copied)
    copied.append("BUILD.txt")

    # ---- the list itself ----
    readme = write_readme(copied)

    log("")
    log("=" * 60)
    log(f"指纹已生成：{os.path.relpath(stamp, ROOT)}（{len(copied) - 1} 个文件）")
    log(f"README 已重新生成：{os.path.relpath(readme, ROOT)}")
    log(f"清单里共 {len(copied)} 个文件")
    log("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
