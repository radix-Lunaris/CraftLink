<?php
/**
 * CraftLink Admin Panel -- configuration.
 *
 * Copy this file to config.php and fill in your values:
 *     cp config.sample.php config.php
 *
 * config.php is git-ignored and never baked into the image.
 */

return [
    // ------------------------------------------------------------
    // MySQL (the same database the relay writes to)
    // ------------------------------------------------------------
    // Inside a container the host must be the MySQL CONTAINER NAME, not
    // 127.0.0.1 (that would be the panel container itself).
    'db' => [
        'host'     => 'web-mysql80',
        'port'     => 3306,
        'database' => 'mclink',
        'username' => 'mclink',
        'password' => 'CHANGE_ME_db_password',
        'charset'  => 'utf8mb4',
    ],

    // ------------------------------------------------------------
    // Relay admin API (Docker internal, never published to the host)
    // ------------------------------------------------------------
    // The token MUST be identical to MCLINK_ADMIN_API_TOKEN in the relay's
    // docker-compose.yml.
    'relay' => [
        'base_url' => 'http://mc-relay:19101',
        'token'    => 'CHANGE_ME_same_token_as_the_relay',
        'timeout'  => 6,

        // 可选：中继的 VERSION 文件。面板左上角显示中继版本时，先问中继的
        // 管理接口（/stats、/health）；接口没报版本才回退读这个文件。
        // 留空 = 用内置候选路径（MCLINK_ROOT/current/server/VERSION、
        //        /var/www/relay/VERSION …），绝不写死安装目录。
        // 例：'version_file' => '/opt/mc-link/current/server/VERSION',
        'version_file' => '',
    ],

    // ------------------------------------------------------------
    // Panel
    // ------------------------------------------------------------
    'panel' => [
        // Change this to a long random string. It signs the PHP session
        // cookie; leaking it lets an attacker forge a login.
        'session_secret' => 'CHANGE_ME_a_long_random_string',

        // 管理面板标题，显示在页头和浏览器标签上。
        // 产品名：MC-Link → CraftLink（方块联机）。
        'title' => 'CraftLink 管理面板',

        // ICP 备案号，显示在页面底部。留空则不显示。
        // 例：'icp' => '你的备案号',
        //
        // [!] 现在也可以在面板「设置」页里改（设置 > 这里 > 内置默认值）。
        //     两边都留空 = 页脚不渲染备案号那一块（M1 的要求）。
        'icp' => '',

        // ---- 可选的进阶配置：面板设置的存储位置（D1）----
        // 面板「设置」页把同一份设置写两处：数据库（主）+ 配置文件（备份）。
        // 下面两项留空 = 用内置候选路径（容器里是 /var/www/config 与
        // /var/www/data/panel-uploads，见 docker-compose.yml 的两条卷）。
        // 例：
        //   'settings_file' => '/opt/mc-link/config/panel-settings.json',
        //   'upload_dir'    => '/opt/mc-link/data/panel-uploads',
        'settings_file' => '',
        'upload_dir'    => '',

        // ---- M4：证书 / 根代理队列 / nginx 配置目录（都是可选的）----
        // 留空 = 用内置候选路径。容器里的约定位置由 panel/docker-compose.yml
        // 的三条卷决定（/var/www/certs、/var/www/config、/var/www/data）。
        'cert_dir'            => '',   // 证书 + certbot 状态 + 根代理队列
        'agent_dir'           => '',   // 覆盖根代理队列目录（默认 <cert_dir>/agent）
        'overrides_file'      => '',   // 急救覆盖文件（mclink 管理台在数据库写不进去时写）
        'nginx_server_dir'    => '',   // 渲染出来的 server 块放哪（容器默认 /etc/nginx/mclink-servers）
        'nginx_template_dir'  => '',   // 渲染模板（容器默认 /var/www/nginx-templates）

        // Optional: allow creating accounts without being logged in.
        // Keep false unless you really want strangers to register.
        'allow_self_register' => false,

        // Set to true only while debugging: shows raw API errors.
        'debug' => false,
    ],
];
