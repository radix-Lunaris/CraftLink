CraftLink v5.1.0 部署与客户端源码包
================================================================
本目录结构 = 服务器上的目录结构，照着拖拽就不会放错位置。

v5.1.0（2026-10-26）：CustomTkinter 全新侧栏与工作台，深色/浅色/跟随系统；
联机、房间、连接状态、日志、设置与账号五个分区，账号弹窗同步替换。
保留现有网络引擎、手动两模式、AUTO、64 人容量和面板功能。
未实现新 API 或 Host 漂移；本版改动说明与验证见 docs/V5.1.0-CHANGES.md。
现有严格版本校验继续沿用：需同步更新服务端版本文件与两端客户端。
完整上传后仅重建中继即可；面板源码和预构建镜像与 v5.0.1 相同，无需重建。

!! 这个 README 是由 scripts/make_build.py 自动生成的 !!
   不要手工编辑 —— 人工维护的清单漏过 db_schema.py，
   直接把中继搞成了重启循环。

AUTO 使用方式
================================================================

  房主创建 AUTO 房间，Host 和玩家分别点击「开始连接」。
  无需先运行游戏或监听目标端口：先建立中继网络隧道，后台逐玩家尝试直连。
  网络通道验证后自动切换，备用中继持续保留；任意 TCP 应用稍后接入。
  直连断开会自动回退。切换房间策略（手动模式与 AUTO）会中断游戏连接。
  v5 客户端与 v5 中继须配套更新；旧客户端不能进入 AUTO 房间。

  详细设计和验收状态：docs/AUTO-PROTOCOL.md、docs/V5-VALIDATION.md。
  network-r1 修复、OpenClash 对照复测：docs/V5-NETWORK-R1.md。
  当前修订 network-r2：修复反复刷新／取消短 TCP 探测导致共享 P2P 被关闭。
  两端使用本目录新的 EXE，详见 docs/V5-NETWORK-R2.md。
  两端 EXE 与服务端须同步更新，旧服务端不支持独立 AUTO_NETWORK 隧道。
  本地验证不能代替真实公网 NAT 和多人 Minecraft 实测。

客户端构建与自检
================================================================

    python -m pip install -r requirements-build.txt
    python scripts/build_client.py
    python client/main.py              # 源码运行前 pip install -r requirements.txt
    CraftLink-console.exe --version
    CraftLink-console.exe --self-test
    python -B tests/run_all.py
    python -B scripts/smoke_ui.py
    python -B scripts/audit_ui.py

  CraftLink.exe 为图形版，CraftLink-console.exe 保留控制台便于排查。
  构建脚本会对两个新 EXE 各自执行版本、协议及模块自检后才替换交付文件。


部署凭据策略
================================================================

沿用 v3.9.20 已登记的 hardcoded 策略，参见 SECURITY-NOTICE.md。
不要把此目录当成已经完成凭据外置的版本；.env.example 是外置方案的模板。
部署时使用本目录配套的 server/、panel/、rebuild.sh 和 BUILD.txt。


上传统清单（共 142 个文件）
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
    client/main.py
    client/mclink/__init__.py
    client/mclink/assets/app.ico
    client/mclink/assets/app.png
    client/mclink/config/__init__.py
    client/mclink/config/credentials.py
    client/mclink/config/settings.py
    client/mclink/core/__init__.py
    client/mclink/core/auto_client.py
    client/mclink/core/auto_network.py
    client/mclink/core/auto_peer.py
    client/mclink/core/auto_room_direct.py
    client/mclink/core/auto_stream.py
    client/mclink/core/auto_udp.py
    client/mclink/core/channel.py
    client/mclink/core/common.py
    client/mclink/core/connection_status.py
    client/mclink/core/control_client.py
    client/mclink/core/direct.py
    client/mclink/core/direct_carry.py
    client/mclink/core/engine.py
    client/mclink/core/host_core.py
    client/mclink/core/network_diagnostics.py
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
    client/mclink/gui/controller.py
    client/mclink/gui/i18n.py
    client/mclink/gui/i18n_tables.py
    client/mclink/gui/presentation.py
    client/mclink/gui/theme.py
    client/mclink/gui/widgets.py
    client/mclink/log.py

--- diag-fleet.sh ---
    diag-fleet.sh

--- diag-panel.sh ---
    diag-panel.sh

--- diag-relay.sh ---
    diag-relay.sh

--- docs ---
    docs/AUTO-PROTOCOL.md
    docs/V5-AUTO-HANDOFF.md
    docs/V5-NETWORK-R1.md
    docs/V5-NETWORK-R2.md
    docs/V5-PANEL-BUILD.md
    docs/V5-VALIDATION.md
    docs/V5.0.1-CHANGES.md
    docs/V5.1.0-CHANGES.md
    docs/client-artifacts.json
    docs/manual-core-baseline.json
    docs/panel-image.json
    docs/panel-image.sha256
    docs/ui-controller-baseline.json
    docs/ui-preservation.json
    docs/ui-preview/desktop-dark.png
    docs/ui-preview/desktop-light-settings.png
    docs/ui-preview/desktop-network.png
    docs/ui-preview/dialog-login.png

--- fix-panel.sh ---
    fix-panel.sh

--- images ---
    images/mc-link-panel-5.0.1-amd64.tar.gz

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

--- requirements-build.txt ---
    requirements-build.txt

--- requirements.txt ---
    requirements.txt

--- scripts ---
    scripts/audit_ui.py
    scripts/build_client.py
    scripts/check_deploy.py
    scripts/e2e_host_gone.py
    scripts/ensure_secrets.py
    scripts/make_build.py
    scripts/rotate_secrets.py
    scripts/smoke_ui.py

--- server ---
    server/Dockerfile
    server/VERSION
    server/admin_api.py
    server/auth.py
    server/auto_relay.py
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

--- tests ---
    tests/run_all.py
    tests/test_auto_integration.py
    tests/test_auto_relay.py
    tests/test_auto_stream.py
    tests/test_auto_udp.py
    tests/test_capacity.py
    tests/test_connection_status.py
    tests/test_manual_baseline.py
    tests/test_network_startup.py
    tests/test_panel_rebuild.py
    tests/test_ui_controller.py

================================================================
【推荐】整包覆盖 —— 不要一个一个文件传
================================================================

  把本目录内的服务端内容（server/、panel/、client/、*.sh、BUILD.txt）
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
    /opt/mc-link/.env          不在包里（保留服务器原有的本地配置）

  唯一要注意：覆盖【不会】删除服务器上的多余文件
  （例如以前手工放的 _diag.php、_diag2.php），那些自己删掉即可；
  rebuild.sh 结束时会再提醒一次。


上传统清单（共 142 个文件）
================================================================

（想单独补某个文件时照这个清单来；路径和服务器上完全一致）


重建 / 自证
================================================================

    bash /opt/mc-link/rebuild.sh          # 两个容器都重建 + 自证
    bash /opt/mc-link/rebuild.sh panel    # 只重建面板（不断开玩家）
    bash /opt/mc-link/rebuild.sh panel --prebuilt-panel  # amd64 使用随包镜像，免下载编译
    bash /opt/mc-link/rebuild.sh relay    # 只重建中继（会断开玩家）

它会自己检查：文件齐不齐（一个一个点名）、凭据是不是占位符、
Dockerfile 有没有 COPY 全部模块、文件真的进容器没有、数据库连上没有、
users.role 列在不在、容器健康不健康、面板能不能打开。
文件指纹对不上时只警告不拦截（要硬性检查加 --strict）。

面板构建修正（2026-10-22）：只补编译 PDO MySQL，curl / mbstring 使用基础镜像内置版本。
面板先构建、成功后再替换容器；构建失败保留现有面板。首次仍需下载编译工具链。
中继已正常时只运行带 panel 参数的命令。详细说明：docs/V5-PANEL-BUILD.md。
默认构建源已改为清华，最终配置真实构建及容器登录页通过。
Linux amd64 可上传 images/ 与 docs/panel-image.* 等完整配套文件，使用 --prebuilt-panel。
此路径先校验镜像和配套面板源文件，再载入并验证启动，不进行现场编译。

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
