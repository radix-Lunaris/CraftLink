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


上传统清单（共 449 个文件）
================================================================

--- .env.example ---
    .env.example

--- .gitignore ---
    .gitignore

--- BUILD.txt ---
    BUILD.txt

--- HANDOVER.md ---
    HANDOVER.md

--- README.txt ---
    README.txt

--- SECURITY-NOTICE.md ---
    SECURITY-NOTICE.md

--- check-db.sh ---
    check-db.sh

--- client ---
    client/branding.json.example
    client/build/mclink-client/Analysis-00.toc
    client/build/mclink-client/CraftLink-console.pkg
    client/build/mclink-client/CraftLink.pkg
    client/build/mclink-client/EXE-00.toc
    client/build/mclink-client/EXE-01.toc
    client/build/mclink-client/PKG-00.toc
    client/build/mclink-client/PKG-01.toc
    client/build/mclink-client/PYZ-00.pyz
    client/build/mclink-client/PYZ-00.toc
    client/build/mclink-client/base_library.zip
    client/build/mclink-client/warn-mclink-client.txt
    client/build/mclink-client/xref-mclink-client.html
    client/dist/CraftLink-console.exe
    client/dist/CraftLink.exe
    client/main.py
    client/mclink/__init__.py
    client/mclink/assets/app.ico
    client/mclink/assets/app.png
    client/mclink/config/__init__.py
    client/mclink/config/branding.py
    client/mclink/config/credentials.py
    client/mclink/config/settings.py
    client/mclink/config/userdirs.py
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

--- deploy ---
    deploy/.env.example
    deploy/.gitignore
    deploy/BUILD.txt
    deploy/CraftLink-console.exe
    deploy/CraftLink.exe
    deploy/README.txt
    deploy/SECURITY-NOTICE.md
    deploy/check-db.sh
    deploy/client/mclink/__init__.py
    deploy/client/mclink/assets/app.ico
    deploy/client/mclink/assets/app.png
    deploy/client/mclink/config/__init__.py
    deploy/client/mclink/config/credentials.py
    deploy/client/mclink/config/settings.py
    deploy/client/mclink/core/__init__.py
    deploy/client/mclink/core/channel.py
    deploy/client/mclink/core/common.py
    deploy/client/mclink/core/control_client.py
    deploy/client/mclink/core/direct.py
    deploy/client/mclink/core/direct_carry.py
    deploy/client/mclink/core/engine.py
    deploy/client/mclink/core/host_core.py
    deploy/client/mclink/core/player_core.py
    deploy/client/mclink/core/presence.py
    deploy/client/mclink/core/protocol.py
    deploy/client/mclink/core/punch.py
    deploy/client/mclink/core/punch_flow.py
    deploy/client/mclink/core/reliable.py
    deploy/client/mclink/core/room.py
    deploy/client/mclink/core/room_direct.py
    deploy/client/mclink/core/session.py
    deploy/client/mclink/core/stun.py
    deploy/client/mclink/core/traffic.py
    deploy/client/mclink/core/transport.py
    deploy/client/mclink/core/udptunnel.py
    deploy/client/mclink/gui/__init__.py
    deploy/client/mclink/gui/app.py
    deploy/client/mclink/gui/auth_ui.py
    deploy/client/mclink/gui/i18n.py
    deploy/client/mclink/gui/i18n_tables.py
    deploy/client/mclink/gui/theme.py
    deploy/client/mclink/log.py
    deploy/diag-fleet.sh
    deploy/diag-panel.sh
    deploy/diag-relay.sh
    deploy/docker-compose.db.yml
    deploy/fix-panel.sh
    deploy/install.sh
    deploy/panel/Dockerfile
    deploy/panel/config.sample.php
    deploy/panel/docker-compose.yml
    deploy/panel/lib/common.php
    deploy/panel/lib/i18n.php
    deploy/panel/nginx.conf
    deploy/panel/php-fpm-pool.conf
    deploy/panel/supervisord.conf
    deploy/panel/web/assets/avatar.png
    deploy/panel/web/assets/style.css
    deploy/panel/web/index.php
    deploy/panel/web/live.php
    deploy/panel/web/login.php
    deploy/panel/web/logout.php
    deploy/panel/web/views/account.php
    deploy/panel/web/views/dashboard.php
    deploy/panel/web/views/events.php
    deploy/panel/web/views/layout_footer.php
    deploy/panel/web/views/layout_header.php
    deploy/panel/web/views/prefs.php
    deploy/panel/web/views/rooms.php
    deploy/panel/web/views/users.php
    deploy/rebuild.sh
    deploy/scripts/check_deploy.py
    deploy/scripts/e2e_host_gone.py
    deploy/scripts/ensure_secrets.py
    deploy/scripts/install_db.py
    deploy/scripts/install_patch.py
    deploy/scripts/make_build.py
    deploy/scripts/mclink-console.sh
    deploy/scripts/rotate_secrets.py
    deploy/server/Dockerfile
    deploy/server/VERSION
    deploy/server/admin_api.py
    deploy/server/auth.py
    deploy/server/db_schema.py
    deploy/server/db_store.py
    deploy/server/docker-compose.yml
    deploy/server/gatekeeper.py
    deploy/server/healthcheck.py
    deploy/server/relay_config.py
    deploy/server/relay_events.py
    deploy/server/relay_log.py
    deploy/server/relay_server.py
    deploy/server/requirements.txt
    deploy/server/reset_password.py
    deploy/server/room_members.py
    deploy/server/room_names.py
    deploy/server/room_registry.py
    deploy/server/settings_service.py
    deploy/server/stun_server.py

--- diag-fleet.sh ---
    diag-fleet.sh

--- diag-panel.sh ---
    diag-panel.sh

--- diag-relay.sh ---
    diag-relay.sh

--- docker-compose.db.yml ---
    docker-compose.db.yml

--- docs ---
    docs/AUTO-RECONNECT-PLAN.md
    docs/BASELINE-REGRESSION-AUDIT-20260928.md
    docs/BUGFIX-20260915.md
    docs/CLIENT-BRANDING.md
    docs/CLIENT-PUNCH-FLOW-20260928.md
    docs/DEPLOY_GUIDE.md
    docs/DESIGN-BASELINE-20260927.md
    docs/HANDOVER.md
    docs/HIDE_PANEL_ENTRY.md
    docs/ISSUES-20260927.md
    docs/LOG-AUDIT-20260927.md
    docs/M4-SSL-DOMAIN.md
    docs/OPENSOURCE-PLAN.md
    docs/PANEL-SETTINGS.md
    docs/PENDING.md
    docs/PROTOCOL.md
    docs/PUNCH-FLOW-TREE-20260928.md
    docs/V2-LEFTOVER-CONNECT-FREEZE.md
    docs/V2_DEPLOY_NOTES.md
    docs/client-logs/host.txt
    docs/client-logs/mclink-log-20260920-2214432.txt
    docs/client-logs/mclink-log-20260920-224432.txt
    docs/client-logs/mclink-log-20260921-171924.txt
    docs/client-logs/mclink-log-20260921-183830.txt
    docs/client-logs/mclink-log-20260921-184023.txt
    docs/client-logs/mclink-log-20260921-host.txt
    docs/client-logs/mclink-log-20260921-player.txt
    docs/client-logs/player.txt
    docs/panel-settings-selftest.php
    docs/panel-settings-smoke.sh
    docs/panel-ssl-nginx-smoke.sh
    docs/shots/client-corner-zoom.png
    docs/shots/client-dark-bottom.png
    docs/shots/client-dark-full-width.png
    docs/shots/client-dark-tile-zoom.png
    docs/shots/client-dark-top.png
    docs/shots/client-dark.png
    docs/shots/client-ja-light-bottom.png
    docs/shots/client-ja-light-buttons.png
    docs/shots/client-ja-light-top.png
    docs/shots/client-light-bottom.png
    docs/shots/client-light-full-width.png
    docs/shots/client-light-narrow-900-single-column.png
    docs/shots/client-light-narrow-900.png
    docs/shots/client-light-tile-zoom.png
    docs/shots/client-light.png
    docs/shots/client-login-dialog.png
    docs/shots/logo-preview.png
    docs/shots/tile-zoom.png

--- fix-panel.sh ---
    fix-panel.sh

--- install.sh ---
    install.sh

--- panel ---
    panel/Dockerfile
    panel/bin/mclink-cert.sh
    panel/bin/mclink-panel.php
    panel/bin/panel-agent.sh
    panel/config.sample.php
    panel/docker-compose.yml
    panel/lib/common.php
    panel/lib/i18n.php
    panel/lib/panel_agent.php
    panel/lib/panel_cli.php
    panel/lib/panel_nginx.php
    panel/lib/panel_settings.php
    panel/lib/panel_ssl.php
    panel/nginx/nginx-main.conf
    panel/nginx/nginx-test.conf.template
    panel/nginx/templates/panel-acme.inc.template
    panel/nginx/templates/panel-backend.inc.template
    panel/nginx/templates/panel-http-redirect.inc.template
    panel/nginx/templates/panel-locations.inc.template
    panel/nginx/templates/panel-rescue-locations.inc.template
    panel/nginx/templates/panel-server.conf.template
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
    panel/web/views/settings.php
    panel/web/views/users.php

--- rebuild.sh ---
    rebuild.sh

--- scripts ---
    scripts/_drop_identity_card.py
    scripts/_host_name.py
    scripts/_measure_width.py
    scripts/build-exe.bat
    scripts/build-win-exe.bat
    scripts/bump-version.py
    scripts/check_deploy.py
    scripts/deploy-now.py
    scripts/e2e_host_gone.py
    scripts/ensure_secrets.py
    scripts/exe_window_probe.py
    scripts/fix_line_endings.py
    scripts/frozen_selfcheck.py
    scripts/install_db.py
    scripts/install_patch.py
    scripts/make_assets.py
    scripts/make_build.py
    scripts/mclink-client.spec
    scripts/mclink-console.sh
    scripts/mclink-v2-check.sh
    scripts/panel-apply.sh
    scripts/panel_probe.ps1
    scripts/rotate_secrets.py
    scripts/run-tests.bat
    scripts/sync-deploy.py
    scripts/take_screenshots.py
    scripts/tls_probe.ps1
    scripts/ui_probe.py
    scripts/ui_probe.txt
    scripts/verify-stun-ports.py
    scripts/启动客户端.bat

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

--- tests ---
    tests/.status-44t4v3uo
    tests/.status-556dp9n2
    tests/.status-7647ydno
    tests/.status-pc9vbuuu
    tests/.status-zjqswpgc
    tests/auto_reconnect_test.py
    tests/busy_delete_msg.txt
    tests/client_kick_relay.log
    tests/client_kick_roster_test.py
    tests/client_kick_status.json
    tests/client_status.json
    tests/config_contract_test.py
    tests/conn_mode_hotswitch_relay.log
    tests/conn_mode_hotswitch_relay_status.json
    tests/conn_mode_hotswitch_test.py
    tests/credentials_test.py
    tests/db_boot_timeout_test.py
    tests/deploy_scripts_test.py
    tests/direct_cand_test.py
    tests/direct_e2e_test.py
    tests/direct_hardening_test.py
    tests/direct_multi_test.py
    tests/direct_persist_test.py
    tests/direct_relay.log
    tests/direct_reuse_test.py
    tests/direct_status.json
    tests/e2e_relay.log
    tests/e2e_relay_test.py
    tests/e2e_status.json
    tests/engine_session_test.py
    tests/fake_docker.py
    tests/ghost_relay.log
    tests/ghost_status.json
    tests/gui_appearance_test.py
    tests/gui_harness.py
    tests/gui_i18n_test.py
    tests/gui_room_ghost_test.py
    tests/gui_room_host_session_test.py
    tests/gui_smoke_test.py
    tests/host_offline_owner_test.py
    tests/host_release_identity_gate_status.json
    tests/host_release_identity_relay.log
    tests/host_release_identity_relay_status.json
    tests/host_release_identity_test.py
    tests/host_release_identity_timeout_status.json
    tests/hotswitch_roster_state_test.py
    tests/hotswitch_stun_resume_test.py
    tests/identity_relay.log
    tests/identity_rtt_gate_relay.log
    tests/identity_rtt_relay.log
    tests/identity_rtt_relay_status.json
    tests/identity_status.json
    tests/install_sh_test.py
    tests/kick_relay.log
    tests/kick_status.json
    tests/kick_test.py
    tests/latency_relay.log
    tests/latency_status.json
    tests/latency_test.py
    tests/m1_capacity_version_test.py
    tests/m6_client_config_test.py
    tests/m6_client_gui_test.py
    tests/manual_stun_live_check.py
    tests/member_conn_state_relay.log
    tests/member_conn_state_relay_status.json
    tests/member_conn_state_test.py
    tests/member_identity_rtt_test.py
    tests/member_name_relay.log
    tests/member_name_rtt_test.py
    tests/mode_identity_reconnect_test.py
    tests/mode_identity_relay.log
    tests/mode_identity_relay_status.json
    tests/mode_switch_rebuild_test.py
    tests/mode_switch_relay.log
    tests/mode_switch_status.json
    tests/multi_relay.log
    tests/multi_status.json
    tests/nat_diag_test.py
    tests/pairing_stale_tunnel_test.py
    tests/panel_static_test.py
    tests/persist_relay.log
    tests/port_kick_refresh_test.py
    tests/port_prediction_experiment.py
    tests/punch_crossnet.py
    tests/punch_display_sync_test.py
    tests/punch_flow_relay.log
    tests/punch_flow_test.py
    tests/punch_test.py
    tests/punch_traffic_test.py
    tests/r4_relay_19640.log
    tests/r4_relay_19641.log
    tests/r4_relay_19642.log
    tests/r4_relay_19643.log
    tests/r4_relay_19644.log
    tests/recon_relay.log
    tests/reconnect_after_teardown_test.py
    tests/refresh_count_test.py
    tests/refresh_relay.log
    tests/relay_baseline_relay.log
    tests/relay_baseline_relay_hostwd_status.json
    tests/relay_baseline_relay_status.json
    tests/relay_baseline_relay_timeout_status.json
    tests/relay_baseline_test.py
    tests/relay_engine_test.log
    tests/relay_test_output.log
    tests/reset_password_test.py
    tests/room_identity_test.py
    tests/room_name_host_test.py
    tests/room_name_relay.log
    tests/room_name_status.json
    tests/room_recovery_test.py
    tests/room_settings_relay.log
    tests/room_settings_test.py
    tests/room_status.json
    tests/round3_relay.log
    tests/run_all.py
    tests/run_all_full.py
    tests/run_all_hostrel.out
    tests/server_logs_dead.out
    tests/server_logs_relay.out
    tests/server_logs_test.py
    tests/session_lifecycle_relay.log
    tests/session_lifecycle_status.json
    tests/session_lifecycle_test.py
    tests/session_probe_reconnect_test.py
    tests/status.json
    tests/status_engine.json
    tests/stun_stack_test.py
    tests/udp_mapping_experiment.py
    tests/udptunnel_test.py
    tests/units_test.py
    tests/v2_client_relay.log
    tests/v2_client_test.py
    tests/v2_control_test.py
    tests/v2_relay.log
    tests/v2_status.json
    tests/version_gate_match.log
    tests/version_gate_match_status.json
    tests/version_gate_off.log
    tests/version_gate_relay.log
    tests/version_gate_relay_status.json
    tests/version_gate_test.py

--- 修复说明_v3.9.18.md ---
    修复说明_v3.9.18.md

--- 修复说明_v3.9.19.md ---
    修复说明_v3.9.19.md

--- 修复说明_v3.9.20.md ---
    修复说明_v3.9.20.md

--- 淇璇存槑_v3.9.20.md ---
    淇璇存槑_v3.9.20.md

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


上传统清单（共 449 个文件）
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


全新机器安装（M5）
================================================================

把整个包上传到**任意目录**，然后：

    bash install.sh

它会：探测本机 Docker 里已有的 MySQL/MariaDB（**有就复用，不新建**）→
交互确认安装根目录（默认 /opt/mc-link，可自定义）/ 面板端口 / URL 安全入口 →
生成 .env（600，凭据随机生成）→ 构建并启动容器 → 打印访问方式。

  * 幂等：重复跑不换凭据、不重建数据库、不动已有数据；
  * 失败即停：任何一步失败都立刻退出并说清原因，不留半装状态；
  * 路径从脚本自身位置推导（不写死 /opt/mc-link，MCLINK_ROOT 可覆盖）。

非交互（自动化）：bash install.sh --yes --root /opt/mc-link

安装完会注册一个 root-only 的 SSH 管理台：

    sudo mclink            # 菜单：重置安全入口 / 解绑域名 / 改 root 密码
                           #       / 重置会话 / 看状态与日志 / 看凭据

所有动作都写进 <安装根目录>/logs/mclink-console.log（审计）。


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
