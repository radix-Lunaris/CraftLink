MC-Link 部署包
================================================================
本目录结构 = 服务器上的目录结构，照着拖拽就不会放错位置。

!! 这个 README 是由 scripts/make_build.py 自动生成的 !!
   不要手工编辑 —— 人工维护的清单漏过 db_schema.py，
   直接把中继搞成了重启循环。

!! compose 文件用本目录里的，不要用仓库里的 !!
   本目录里的 compose 只保留 `${MCLINK_DB_PASSWORD:?...}` 这样的引用，
   真实凭据在服务器的 /opt/mc-link/.env 里（见 .env.example）。
   仓库里如果还有写死的值，传上去会覆盖掉服务器上正在用的。


凭据（v3.9.16 起）
================================================================

    cp .env.example /opt/mc-link/.env
    python3 scripts/rotate_secrets.py        # 生成随机值并写回
    # 然后按它打印的那条 SQL 去 MySQL 里改密码

两个值都不再有明文写在任何交付文件里。变量没设置时 docker compose
**拒绝启动**，rebuild.sh 会先一步拦住并说人话。


上传统清单（共 94 个文件）
================================================================

--- .env.example ---
    .env.example

--- .gitignore ---
    .gitignore

--- BUILD.txt ---
    BUILD.txt

--- CraftLink-console.exe ---
    CraftLink-console.exe

--- CraftLink.exe ---
    CraftLink.exe

--- README.txt ---
    README.txt

--- SECURITY-NOTICE.md ---
    SECURITY-NOTICE.md

--- check-db.sh ---
    check-db.sh

--- client ---
    client/mclink/__init__.py
    client/mclink/assets/app.ico
    client/mclink/assets/app.png
    client/mclink/config/__init__.py
    client/mclink/config/credentials.py
    client/mclink/config/settings.py
    client/mclink/core/__init__.py
    client/mclink/core/channel.py
    client/mclink/core/common.py
    client/mclink/core/control_client.py
    client/mclink/core/direct.py
    client/mclink/core/direct_carry.py
    client/mclink/core/engine.py
    client/mclink/core/host_core.py
    client/mclink/core/player_core.py
    client/mclink/core/presence.py
    client/mclink/core/protocol.py
    client/mclink/core/punch.py
    client/mclink/core/punch_flow.py
    client/mclink/core/reliable.py
    client/mclink/core/room.py
    client/mclink/core/room_direct.py
    client/mclink/core/session.py
    client/mclink/core/stun.py
    client/mclink/core/traffic.py
    client/mclink/core/transport.py
    client/mclink/core/udptunnel.py
    client/mclink/gui/__init__.py
    client/mclink/gui/app.py
    client/mclink/gui/auth_ui.py
    client/mclink/gui/i18n.py
    client/mclink/gui/i18n_tables.py
    client/mclink/gui/theme.py
    client/mclink/log.py

--- diag-fleet.sh ---
    diag-fleet.sh

--- diag-panel.sh ---
    diag-panel.sh

--- diag-relay.sh ---
    diag-relay.sh

--- fix-panel.sh ---
    fix-panel.sh

--- panel ---
    panel/Dockerfile
    panel/config.sample.php
    panel/docker-compose.yml
    panel/lib/common.php
    panel/lib/i18n.php
    panel/nginx.conf
    panel/php-fpm-pool.conf
    panel/supervisord.conf
    panel/web/assets/avatar.png
    panel/web/assets/style.css
    panel/web/index.php
    panel/web/live.php
    panel/web/login.php
    panel/web/logout.php
    panel/web/views/account.php
    panel/web/views/dashboard.php
    panel/web/views/events.php
    panel/web/views/layout_footer.php
    panel/web/views/layout_header.php
    panel/web/views/prefs.php
    panel/web/views/rooms.php
    panel/web/views/users.php

--- rebuild.sh ---
    rebuild.sh

--- scripts ---
    scripts/check_deploy.py
    scripts/e2e_host_gone.py
    scripts/ensure_secrets.py
    scripts/make_build.py
    scripts/rotate_secrets.py

--- server ---
    server/Dockerfile
    server/VERSION
    server/admin_api.py
    server/auth.py
    server/db_schema.py
    server/db_store.py
    server/docker-compose.yml
    server/gatekeeper.py
    server/healthcheck.py
    server/relay_config.py
    server/relay_events.py
    server/relay_log.py
    server/relay_server.py
    server/requirements.txt
    server/reset_password.py
    server/room_members.py
    server/room_names.py
    server/room_registry.py
    server/settings_service.py
    server/stun_server.py

================================================================
【推荐】整包覆盖 —— 不要一个一个文件传
================================================================

  把 deploy 文件夹里的全部内容（server/、panel/、client/、*.sh、BUILD.txt）
  一次性拖到服务器的 /opt/mc-link/ 下，提示覆盖时选【全部覆盖】。
  目录结构和服务器上完全一样。

  覆盖完之后，服务器上【只需要一条命令】：

      bash /opt/mc-link/rebuild.sh

  不必再先手动修行尾了。rebuild.sh 现在自己会把 /opt/mc-link/*.sh
  的 CRLF 洗掉。

  为什么推荐：逐个文件上传失败过很多次，症状永远是「改了但没生效」
  （web/ 传了、lib/ 没传 → 面板整站 500；index.php 传了、
  lib/common.php 没传 → Call to undefined function）。
  整包覆盖没有这个失败模式：要么每个文件都是新的，要么覆盖时报错。

  覆盖不会出事的文件：
    panel/config.php           不在包里（服务器上唯一存着数据库密码的
                               文件，永远不要覆盖它）
    /opt/mc-link/.env          不在包里（真实凭据只在这里）

  唯一要注意：覆盖【不会】删除服务器上的多余文件
  （例如以前手工放的 _diag.php、_diag2.php），那些自己删掉即可；
  rebuild.sh 结束时会再提醒一次。


上传统清单（共 94 个文件）
================================================================

（想单独补某个文件时照这个清单来；路径和服务器上完全一致）


重建 / 自证
================================================================

    bash /opt/mc-link/rebuild.sh          # 两个容器都重建 + 自证
    bash /opt/mc-link/rebuild.sh panel    # 只重建面板（不断开玩家）
    bash /opt/mc-link/rebuild.sh relay    # 只重建中继（会断开玩家）

它会自己检查：文件齐不齐（一个一个点名）、凭据是不是占位符、
Dockerfile 有没有 COPY 全部模块、文件真的进容器没有、数据库连上没有、
users.role 列在不在、容器健康不健康、面板能不能打开。
文件指纹对不上时只警告不拦截（要硬性检查加 --strict）。

指纹自查（纯只读，随时可跑）：
    cd /opt/mc-link && sha256sum -c BUILD.txt

发版门禁（7 项，发版前必跑）：
    python3 scripts/check_deploy.py            # 只检查，不写任何东西
    #
    # 当前凭据是**硬编码**的（SECURITY-NOTICE.md），所以这条会打印一块
    # 风险告知并照常放行。改回外部化后用 --strict-secrets 验收。
    python3 scripts/check_deploy.py --strict-secrets   # 明文按失败处理
    python3 scripts/check_deploy.py --stamp    # **打包那一刻**才用：
                                               # 把当前版本记进 LAST_VERSION，
                                               # 下一次修复没 +1 就会被拦住。
    python3 scripts/e2e_host_gone.py           # 起真中继跑一遍 host-gone

指纹覆盖 `client/`（v3.9.16 修）：以前它只覆盖 47 个文件、客户端源码
完全不在保护内 —— `sha256sum -c` 通过并不能证明客户端是新的。


跑不起来的时候
================================================================

    bash /opt/mc-link/diag-relay.sh     # 中继崩了 / 重启循环
    bash /opt/mc-link/diag-panel.sh     # 面板 500 / 登录失败

这两个脚本只读，不重启容器、不动数据，会把真实报错打出来。

如果 bash 报这种错：
    rebuild.sh: line 18: $'\r': command not found
    syntax error near unexpected token `$'do\r''
那是文件被存成了 Windows 换行（CRLF），和代码无关。一条命令修好：

    sed -i 's/\r$//' /opt/mc-link/*.sh


SSH 找回根管理密码
================================================================

    docker exec -it mc-relay python reset_password.py

不需要参数。回车 = 自动生成 16 位随机密码（排除 0oO1iIlLq9g）；
输入只允许字母数字，含 @ 这类符号会要求重输；输入 q 取消。
注意 -it 不能省。
