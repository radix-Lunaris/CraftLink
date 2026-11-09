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
    ],

    // ------------------------------------------------------------
    // Panel
    // ------------------------------------------------------------
    'panel' => [
        // Legacy compatibility field; current login uses PHP server-side sessions.
        'session_secret' => 'CHANGE_ME_a_long_random_string',

        // 管理面板标题，显示在页头和浏览器标签上。
        // 产品名：MC-Link → CraftLink（方块联机）。
        'title' => 'CraftLink 管理面板',

        // ICP 备案号，显示在页面底部。留空则不显示。
        // 例：'icp' => '陇ICP备2026008614号',
        'icp' => '',

        // Optional: allow creating accounts without being logged in.
        // Keep false unless you really want strangers to register.
        'allow_self_register' => false,

        // Set to true only while debugging: shows raw API errors.
        'debug' => false,
    ],
];
