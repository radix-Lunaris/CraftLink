<?php
/**
 * 面板设置 / 权限模型 / logo 上传 / 中继版本 —— **可运行的**自检脚本（不需要数据库）。
 *
 * 为什么放在 docs/ 而不是 tests/：
 *   * 仓库的 PHP 门禁（tests/panel_static_test.py）只扫 panel/**.php；
 *   * 测试网（tests/）归测试工作流；
 *   * 放在 docs/ 还有个好处：部署包不会把它当成面板代码收进去。
 *
 * 用法（任何有 php-cli 的机器，不需要 MySQL、不需要面板容器、不需要中继）：
 *
 *     php docs/panel-settings-selftest.php
 *
 * 退出码 0 = 全过；非 0 = 有失败项（每条 FAIL 都写清期望值和实际值）。
 *
 * 它**加载真的 lib/common.php + lib/panel_settings.php**，只把外部依赖指到
 * 连不上的地址上：
 *   * 数据库：MCLINK_DB_HOST 指向一个不存在的主机 → db() 抛异常 → 正好用来
 *     验证"数据库不可用时只读回退到配置文件"这条设计（D1）；
 *   * 中继管理接口：指向 127.0.0.1:1 → 连接被拒 → 正好用来验证中继版本的
 *     文件回退；
 *   * 设置 / logo / VERSION 全部落在系统临时目录里，**不碰仓库**。
 */

declare(strict_types=1);

// ============================================================
// 0. 环境：必须在第一次 config() 之前设好（config() 会把环境读进去并缓存）
// ============================================================

$root = sys_get_temp_dir() . '/mclink-panel-selftest';
$backupFile = $root . '/config/panel-settings.json';
$versionFile = $root . '/relay-VERSION';
$uploadDir = $root . '/uploads';

@mkdir($root . '/config', 0700, true);
@mkdir($uploadDir, 0700, true);

if (is_file($backupFile)) {
    @unlink($backupFile);
}

if (is_file($versionFile)) {
    @unlink($versionFile);
}

putenv('MCLINK_PANEL_SETTINGS_FILE=' . $backupFile);
putenv('MCLINK_ROOT=' . $root);
putenv('MCLINK_CONFIG_DIR=' . $root . '/config');
putenv('MCLINK_PANEL_UPLOAD_DIR=' . $uploadDir);
putenv('MCLINK_RELAY_VERSION_FILE=' . $versionFile);
putenv('MCLINK_PANEL_TITLE=配置文件里的标题');
putenv('MCLINK_PANEL_ICP=');
// 数据库指向一个连不上的主机；中继接口指向一个没人监听的端口 —— 都不需要
// 真的凭据（面板自己的默认密码是空的，连接本来就建立不起来）。
putenv('MCLINK_DB_HOST=selftest-no-such-db-host.invalid');
putenv('MCLINK_RELAY_API_URL=http://127.0.0.1:1');

require __DIR__ . '/../panel/lib/common.php';

// CLI 里也把会话起好：必须在任何输出之前，否则 session_start() 会警告。
start_session();

// ============================================================
// 迷你断言 / 小工具
// ============================================================

$GLOBALS['selftest_pass'] = 0;
$GLOBALS['selftest_fail'] = 0;

function selftest_check(string $name, bool $ok, string $detail = ''): void
{
    if ($ok) {
        $GLOBALS['selftest_pass']++;
        echo "  [PASS] {$name}\n";
        return;
    }

    $GLOBALS['selftest_fail']++;
    echo "  [FAIL] {$name}" . ($detail !== '' ? "  -- {$detail}" : '') . "\n";
}

function selftest_section(string $title): void
{
    echo "\n== {$title} ==\n";
}

/** 切换"当前登录角色"（走真正的 $_SESSION，和浏览器里同一个判定路径）。 */
function selftest_role(string $role): void
{
    $_SESSION['user'] = [
        'username' => 'selftest',
        'role'     => $role,
        'is_admin' => $role === 'player' ? 0 : 1,
    ];
    $_SESSION['last_seen_at'] = time();

    if (current_role() !== $role) {
        throw new RuntimeException('selftest: could not switch role to ' . $role);
    }
}

/** 把一条（或几条）设置写进"配置文件备份"，模拟 DB 不可用时的只读回退。 */
function selftest_write_backup(array $settings): void
{
    global $backupFile;

    file_put_contents($backupFile, json_encode([
        'schema'        => 1,
        'panel_version' => PANEL_VERSION,
        'updated_at'    => date('c'),
        'settings'      => $settings,
    ], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));

    // 让 panel_settings_* 的请求内缓存失效（真实环境里缓存只活一个请求）。
    panel_settings_file(true);
    panel_settings_all(true);
}

echo "CraftLink 面板设置自检（PHP " . PHP_VERSION . "）\n";
echo "临时目录：{$root}\n";

// ============================================================
// 1. 版本号与注册表
// ============================================================

selftest_section('1. 版本号 / 设置项注册表（M1-M3 要求逐条实现）');

selftest_check(
    '面板版本号 = 2.11.45（唯一一份 PANEL_VERSION）',
    PANEL_VERSION === '2.11.45',
    '实际 ' . PANEL_VERSION
);

$defs = panel_setting_defs();

$required = [
    'listen_port',           // 1) 面板监听端口
    'listen_ip',             // 2) 面板监听 IP
    'url_suffix',            // 3) URL 安全入口后缀
    'unauthenticated_code',  // 4) 未认证时的 HTTP 返回码
    'session_minutes',       // 7) 会话时长（默认 360 分钟）
    'panel_brand',           // 8) 左上角名称
    'panel_tab_title',       // 8) 网页标签名
    'panel_logo',            // 9) logo（左上角 + favicon 同一张图）
    'icp_registered',        // 10) 是否展示备案号
    'icp_number',            // 10) 备案号
    // ---- M4：域名绑定 + SSL ----
    'bound_domain',          // 5) 绑定域名（只有该域名能访问）
    'force_https',           // 5) HTTP → HTTPS 跳转
    'ssl_mode',              // 6) off / upload / selfsigned / acme
    'ssl_cert_file',         // 6) 证书文件（PEM）
    'ssl_key_file',          // 6) 私钥文件（PEM）
    'acme_email',            // 6) Let's Encrypt 账号邮箱
];

foreach ($required as $key) {
    selftest_check("注册表里有 {$key}", array_key_exists($key, $defs));
}

selftest_check(
    '会话时长默认 360 分钟',
    panel_setting_default('session_minutes') === '360',
    '实际 ' . panel_setting_default('session_minutes')
);

selftest_check(
    '备案号 / 备案开关默认空（空 → 页脚不渲染）',
    panel_setting_default('icp_number') === ''
    && panel_setting_default('icp_registered') === ''
);

selftest_check(
    '未认证返回码默认「显示登录页」（保持老行为）',
    panel_setting_default('unauthenticated_code') === 'login'
);

selftest_check(
    '未认证返回码至少支持 404 / 403 / 444 / 200 空页',
    array_key_exists('404', (array) $defs['unauthenticated_code']['options'])
    && array_key_exists('403', (array) $defs['unauthenticated_code']['options'])
    && array_key_exists('444', (array) $defs['unauthenticated_code']['options'])
    && array_key_exists('200', (array) $defs['unauthenticated_code']['options'])
);

selftest_check(
    'M4 已实现：占位钩子清空，六个键都进了注册表',
    panel_setting_placeholders() === []
    && array_key_exists('bound_domain', $defs)
    && array_key_exists('force_https', $defs)
    && array_key_exists('ssl_mode', $defs)
    && array_key_exists('ssl_cert_file', $defs)
    && array_key_exists('ssl_key_file', $defs)
    && array_key_exists('acme_email', $defs)
);

selftest_check(
    'SSL 模式默认 off、不绑域名、不强制跳转（升级后行为一个字都不变）',
    panel_setting_default('ssl_mode') === 'off'
    && panel_setting_default('bound_domain') === ''
    && panel_setting_default('force_https') === '0'
    && panel_setting_default('ssl_cert_file') === ''
    && panel_setting_default('ssl_key_file') === ''
    && panel_setting_default('acme_email') === ''
);

selftest_check(
    'SSL 模式四种选项齐全',
    array_keys((array) $defs['ssl_mode']['options']) === ['off', 'upload', 'selfsigned', 'acme']
);

selftest_check(
    '绑定域名：规范化会去掉协议 / 端口 / 路径 / 结尾的点',
    panel_domain_normalize('HTTPS://Panel.Example.COM:19102/a?b=1') === 'panel.example.com'
    && panel_domain_normalize('*.example.com.') === '*.example.com'
);

selftest_check(
    '绑定域名：匹配规则（通配子域 / 带端口的 Host / 大小写）',
    panel_domain_match('a.example.com', '*.example.com') === true
    && panel_domain_match('example.com', '*.example.com') === true
    && panel_domain_match('a.example.org', '*.example.com') === false
    && panel_domain_match('Panel.Example.com:19102', 'panel.example.com') === true
    && panel_domain_request_ok('anything') === true   // 没绑域名 = 都放行
);

selftest_check(
    '未认证返回码 login 在 nginx 层按 404（否则安全入口白配）',
    panel_nginx_guard_code() === '404'
);

selftest_check(
    '证书路径：只收绝对路径，拒绝 .. 与空格',
    panel_ssl_path_problem('/var/www/certs/a.crt') === ''
    && panel_ssl_path_problem('certs/a.crt') !== ''
    && panel_ssl_path_problem('/var/www/../etc/passwd') !== ''
);

selftest_check(
    'PEM 结构校验：证书 / 私钥 / 加密私钥 / 互串都能认出来',
    panel_ssl_pem_looks_like_cert("-----BEGIN CERTIFICATE-----\nxx\n-----END CERTIFICATE-----\n") === true
    && panel_ssl_pem_looks_like_cert("-----BEGIN " . "PRIVATE KEY-----\nxx\n-----END PRIVATE KEY-----\n") === false
    && panel_ssl_pem_looks_like_key("-----BEGIN " . "PRIVATE KEY-----\nxx\n-----END PRIVATE KEY-----\n") === true
    && panel_ssl_pem_looks_like_key("-----BEGIN CERTIFICATE-----\nxx\n-----END CERTIFICATE-----\n") === false
    && panel_ssl_pem_looks_encrypted("-----BEGIN ENCRYPTED PRIVATE KEY-----\nxx\n") === true
);

selftest_check(
    '证书 / 私钥路径能看出"在不在面板证书目录里"（在外面要额外挂载）',
    panel_ssl_path_in_cert_dir(panel_cert_dir() . '/panel.crt') === true
    && panel_ssl_path_in_cert_dir('/etc/letsencrypt/live/x/fullchain.pem') === false
);

// ============================================================
// 2. 校验规则（服务端强制；页面上的 min / max 只是方便）
// ============================================================

selftest_section('2. 校验规则（合法值通过 / 越界与垃圾值被拒）');

$cases = [
    ['listen_ip', '127.0.0.1', true],
    ['listen_ip', '0.0.0.0', true],
    ['listen_ip', '::1', true],
    ['listen_ip', 'not-an-ip', false],
    ['listen_ip', '', false],
    ['listen_port', '19102', true],
    ['listen_port', '1', true],
    ['listen_port', '65535', true],
    ['listen_port', '0', false],
    ['listen_port', '65536', false],
    ['listen_port', 'abc', false],
    ['url_suffix', '', true],
    ['url_suffix', 'mclink-panel', true],
    ['url_suffix', '/mclink-panel/', true],
    ['url_suffix', 'a/b', false],
    ['url_suffix', 'has space', false],
    ['unauthenticated_code', 'login', true],
    ['unauthenticated_code', '404', true],
    ['unauthenticated_code', '444', true],
    ['unauthenticated_code', '500', false],
    ['session_minutes', '360', true],
    ['session_minutes', '5', true],
    ['session_minutes', '10080', true],
    ['session_minutes', '4', false],
    ['session_minutes', '10081', false],
    ['panel_brand', '我的面板', true],
    ['panel_tab_title', '', true],
    ['icp_registered', '', true],
    ['icp_registered', '1', true],
    ['icp_registered', '2', false],
    ['icp_number', '你的备案号', true],
    ['panel_logo', '', true],
    ['panel_logo', 'logo-abc123.png', true],
    ['panel_logo', '../../etc/passwd', false],
    // ---- M4：域名与 SSL ----
    ['bound_domain', '', true],
    ['bound_domain', 'panel.example.com', true],
    ['bound_domain', 'https://panel.example.com/', true],
    ['bound_domain', '*.example.com', true],
    ['bound_domain', 'bad_domain!!', false],
    ['bound_domain', 'a.*.b.com', false],
    ['force_https', '0', true],
    ['force_https', '1', true],
    ['force_https', 'yes', false],
    ['ssl_mode', 'off', true],
    ['ssl_mode', 'upload', true],
    ['ssl_mode', 'selfsigned', true],
    ['ssl_mode', 'acme', true],
    ['ssl_mode', 'letsencrypt', false],
    ['ssl_cert_file', '', true],
    ['ssl_cert_file', '/var/www/certs/panel.crt', true],
    ['ssl_cert_file', 'certs/panel.crt', false],
    ['ssl_cert_file', '/var/www/../etc/passwd', false],
    ['ssl_key_file', '/var/www/certs/panel.key', true],
    ['acme_email', '', true],
    ['acme_email', 'admin@example.com', true],
    ['acme_email', 'not-an-email', false],
];

foreach ($cases as [$key, $value, $expectOk]) {
    $problem = panel_setting_validate($key, $value);
    $ok = ($problem === '') === $expectOk;

    selftest_check(
        sprintf('%s = %s → %s', $key, $value === '' ? '(空)' : $value, $expectOk ? '通过' : '拒绝'),
        $ok,
        '实际：' . ($problem === '' ? '通过' : $problem)
    );
}

selftest_check(
    '逐条放行开关只认 0 / 1',
    panel_setting_validate('listen_port__admin_edit', '1') === ''
    && panel_setting_validate('listen_port__admin_edit', 'yes') !== ''
);

selftest_check('未知键一律拒绝', panel_setting_validate('drop_database', '1') !== '');

selftest_check(
    'url_suffix 存进去之前规范化（去掉前后斜杠）',
    panel_setting_normalize('url_suffix', '/secret/') === 'secret'
);

// ============================================================
// 3. 权限矩阵：每条设置 × 角色 × root 放行开关
// ============================================================

selftest_section('3. 权限矩阵（每条设置 × 角色 × 放行开关，逐个组合断言）');

foreach (panel_setting_keys() as $key) {
    selftest_role(ROLE_ROOT);
    selftest_check("root 能改 {$key}（放行开关默认关也照样能改）", panel_setting_can_edit($key) === true);

    selftest_role(ROLE_ADMIN);
    selftest_check("普通管理员默认不能改 {$key}", panel_setting_can_edit($key) === false);
    selftest_check("普通管理员能看到设置页（{$key} 在那一页上）", panel_setting_page_visible() === true);

    selftest_role(ROLE_PLAYER);
    selftest_check(
        "玩家不能改 {$key}，也看不到设置页",
        panel_setting_can_edit($key) === false && panel_setting_page_visible() === false
    );
}

selftest_role(ROLE_ADMIN);
selftest_check(
    '普通管理员不能拨「放行」开关（只有 root 能）',
    panel_setting_can_toggle_admin_edit('listen_port') === false
);

selftest_role(ROLE_ROOT);
selftest_check('root 能拨「放行」开关', panel_setting_can_toggle_admin_edit('listen_port') === true);
selftest_check(
    '放行开关的键名是 <键>__admin_edit',
    panel_setting_admin_edit_key('listen_port') === 'listen_port__admin_edit'
);

// 放行之后（root 打开开关）：普通管理员能改那一条，别的条还是不能改。
selftest_write_backup(['listen_port__admin_edit' => '1']);

selftest_role(ROLE_ADMIN);
selftest_check(
    '放行开关打开后：普通管理员能改被放行的那一条',
    panel_setting_can_edit('listen_port') === true
);
selftest_check(
    '放行开关打开后：普通管理员仍然不能改没放行的条目',
    panel_setting_can_edit('listen_ip') === false
);

selftest_role(ROLE_PLAYER);
selftest_check(
    '放行开关打开后：玩家依旧不能改（放行只放给普通管理员）',
    panel_setting_can_edit('listen_port') === false
);

selftest_write_backup([]);

// ============================================================
// 4. 存储：DB 为主 + 配置文件备份（DB 不可用时只读回退，D1）
// ============================================================

selftest_section('4. 存储：DB 为主 + config/panel-settings.json 备份（DB 不可用时回退读）');

selftest_check(
    '候选路径第一优先是环境变量 MCLINK_PANEL_SETTINGS_FILE',
    panel_settings_file_candidates()[0] === $backupFile,
    implode(' | ', panel_settings_file_candidates())
);

selftest_check(
    '数据库不可用时不抛异常，只报「不可用」',
    panel_settings_ensure_schema(true)['ok'] === false && panel_settings_db_read() === []
);

selftest_check(
    '没有配置文件时读到的是注册表默认值',
    panel_setting('listen_port') === '19102' && panel_setting('session_minutes') === '360'
);

$write = panel_settings_write_backup('selftest');

selftest_check(
    '能写出配置文件备份（原子写：.tmp + rename）',
    $write['ok'] === true && is_file($backupFile),
    $write['error']
);

$decoded = is_file($backupFile)
    ? json_decode((string) file_get_contents($backupFile), true)
    : null;

selftest_check(
    '备份是 JSON，含 schema / panel_version / settings',
    is_array($decoded)
    && ($decoded['schema'] ?? null) === 1
    && ($decoded['panel_version'] ?? '') === PANEL_VERSION
    && is_array($decoded['settings'] ?? null)
);

selftest_write_backup(['listen_port' => '12345', 'panel_brand' => '测试面板']);

selftest_check(
    'DB 不可用时读得到配置文件里的值（只读回退）',
    panel_setting('listen_port') === '12345' && panel_setting('panel_brand') === '测试面板',
    'listen_port=' . panel_setting('listen_port')
);

selftest_check(
    '未知键不会被当成设置（drop_database 之类进不来）',
    panel_setting('drop_database', '') === ''
);

$save = panel_setting_save('listen_port', '23456', 'selftest');

selftest_check(
    'DB 不可用时保存**明确失败**（不会假装成功）',
    $save['ok'] === false && $save['error'] !== '',
    $save['error']
);

$after = json_decode((string) file_get_contents($backupFile), true);
selftest_check(
    '保存失败时不写配置文件（避免库里旧值 / 文件里新值两份真相）',
    ($after['settings']['listen_port'] ?? '') === '12345',
    json_encode($after['settings'] ?? [], JSON_UNESCAPED_UNICODE)
);

$import = panel_settings_import_file('selftest');
selftest_check(
    'DB 不可用时「从配置文件导入」明确报导入 0 条（不假装成功）',
    $import['ok'] === true && $import['imported'] === 0
);

// ============================================================
// 5. 生效值：面板设置 > config.php / 环境变量 > 内置默认
// ============================================================

selftest_section('5. 生效值（设置 > config.php > 默认）');

selftest_write_backup(['panel_brand' => '设置里的名字']);
selftest_check('左上角名称：设置里有就用设置', panel_brand_name() === '设置里的名字');

selftest_write_backup([]);
selftest_check(
    '左上角名称：设置留空就用 config.php / MCLINK_PANEL_TITLE',
    panel_brand_name() === '配置文件里的标题',
    panel_brand_name()
);

selftest_check('网页标签名：留空 = 跟左上角名称一样', panel_tab_title() === panel_brand_name());

selftest_write_backup(['panel_tab_title' => '标签名']);
selftest_check('网页标签名：设置里有就用设置', panel_tab_title() === '标签名');

selftest_write_backup([]);
selftest_check(
    '备案号默认空 → 页脚不渲染',
    panel_icp_number() === '' && panel_icp_enabled() === false
);

selftest_write_backup(['icp_number' => '你的备案号']);
selftest_check(
    '填了备案号 → 页脚渲染（自动模式）',
    panel_icp_number() === '你的备案号' && panel_icp_enabled() === true
);

selftest_write_backup(['icp_number' => '你的备案号', 'icp_registered' => '0']);
selftest_check('显式选「不显示」→ 页脚不渲染', panel_icp_enabled() === false);

selftest_write_backup([]);

// ============================================================
// 6. 网络与安全：面板能读到自己的监听配置（自带 nginx 的接口数据）
// ============================================================

selftest_section('6. 监听配置读取（M3：自带 nginx 的接口数据）');

$listen = panel_listen_config();

selftest_check(
    '默认监听 127.0.0.1:19102（老部署的实际值）',
    $listen['ip'] === '127.0.0.1' && $listen['port'] === 19102
    && $listen['nginx_listen'] === '127.0.0.1:19102'
);

selftest_check(
    '默认没有安全入口（保持老行为），未认证返回码 login',
    $listen['entry_suffix'] === '' && $listen['entry_path'] === ''
    && $listen['unauth_code'] === 'login'
);

selftest_write_backup([
    'listen_ip'            => '0.0.0.0',
    'listen_port'          => '8443',
    'url_suffix'           => '/secret-path/',
    'unauthenticated_code' => '404',
]);

$listen = panel_listen_config();

selftest_check(
    '读到设置后：0.0.0.0:8443 / /secret-path / 404',
    $listen['ip'] === '0.0.0.0' && $listen['port'] === 8443
    && $listen['entry_path'] === '/secret-path' && $listen['unauth_code'] === '404',
    json_encode($listen, JSON_UNESCAPED_UNICODE)
);

selftest_check('未认证返回码就是页面用的那个值', panel_unauthenticated_code() === '404');

selftest_write_backup([]);

// ============================================================
// 7. logo：魔术字节识别 + 上传失败时不留垃圾
// ============================================================

selftest_section('7. logo（魔术字节识别 / 上传失败要干净）');

$images = [
    'png'  => "\x89PNG\r\n\x1a\n" . str_repeat("\x00", 16),
    'jpg'  => "\xff\xd8\xff\xe0" . str_repeat("\x00", 16),
    'gif'  => "GIF89a" . str_repeat("\x00", 16),
    'webp' => "RIFF\x00\x00\x00\x00WEBPVP8 ",
    'ico'  => "\x00\x00\x01\x00" . str_repeat("\x00", 16),
];

foreach ($images as $ext => $blob) {
    $type = panel_image_type($blob);
    selftest_check("识别 {$ext}", ($type['ext'] ?? '') === $ext, json_encode($type));
}

selftest_check(
    'SVG 被拒绝（内嵌脚本风险）',
    panel_image_type('<svg xmlns="http://www.w3.org/2000/svg"></svg>') === []
);

selftest_check('随便一段文本被拒绝', panel_image_type('hello world') === []);

selftest_check(
    'logo 文件名白名单挡住路径穿越',
    panel_setting_validate('panel_logo', '../evil.png') !== ''
    && panel_setting_validate('panel_logo', 'logo-1a2b3c.png') === ''
);

selftest_check(
    '上传目录落在配置的 MCLINK_PANEL_UPLOAD_DIR 上',
    panel_logo_dir(true) === $uploadDir,
    panel_logo_dir(true)
);

selftest_check('没上传 logo 时 logo 地址是空串（页面退回内置草方块）', panel_logo_url() === '');

$tempPng = $root . '/fake-upload.png';
file_put_contents($tempPng, $images['png']);

$before = glob($uploadDir . '/*') ?: [];
$upload = panel_logo_store_upload([
    'name'     => 'fake-upload.png',
    'type'     => 'image/png',
    'tmp_name' => $tempPng,
    'error'    => UPLOAD_ERR_OK,
    'size'     => (int) filesize($tempPng),
], 'selftest');
$after = glob($uploadDir . '/*') ?: [];

selftest_check(
    'DB 不可用时上传明确失败（设置写不进去就不会留下半个 logo）',
    $upload['ok'] === false && $upload['error'] !== '',
    $upload['error']
);
selftest_check(
    '上传失败后上传目录没有被塞进垃圾文件',
    count($after) === count($before),
    implode(',', $after)
);

// ============================================================
// 8. 中继版本：API 优先 / 文件回退 / 不一致要说出来（D8：路径不写死）
// ============================================================

selftest_section('8. 中继版本（API 优先 → VERSION 文件回退 → 读不到就是 -）');

selftest_check(
    '环境变量 MCLINK_RELAY_VERSION_FILE 排在候选第一位',
    relay_version_file_candidates()[0] === $versionFile,
    implode(' | ', relay_version_file_candidates())
);

selftest_check(
    'MCLINK_ROOT 会派生 current/server/VERSION 与 server/VERSION 两个候选',
    in_array($root . '/current/server/VERSION', relay_version_file_candidates(), true)
    && in_array($root . '/server/VERSION', relay_version_file_candidates(), true)
);

selftest_check(
    '仓库布局（<仓库根>/server/VERSION）也在候选里',
    in_array(dirname(__DIR__) . '/server/VERSION', relay_version_file_candidates(), true)
);

// 注意：候选表里还有"仓库 / 发布包布局"的 VERSION 文件（这一台开发机上就
// 存在），所以"环境变量指向的文件不存在"时**不该**读到它、而是顺延到下一个
// 候选 —— 下面两条断言的就是这个"顺延 + 只认合法版本号"的行为。
selftest_check(
    '环境变量指向的 VERSION 文件不存在时不会被当成"读到了"',
    relay_version_from_file()['path'] !== $versionFile
    && (relay_version_from_file()['version'] === ''
        || relay_version_looks_valid(relay_version_from_file()['version']))
);

file_put_contents($versionFile, "3.9.20\n");

selftest_check(
    'VERSION 文件里的 3.9.20 被读出来（去掉换行，环境变量指定的那份优先）',
    relay_version_from_file()['version'] === '3.9.20'
    && relay_version_from_file()['path'] === $versionFile,
    json_encode(relay_version_from_file(), JSON_UNESCAPED_UNICODE)
);

file_put_contents($versionFile, "relay started at 12:00\n");

$garbage = relay_version_from_file();

selftest_check(
    'VERSION 文件里是垃圾内容时跳过它（绝不把日志行当版本号显示）',
    $garbage['path'] !== $versionFile
    && $garbage['version'] !== 'relay started at 12:00'
    && ($garbage['version'] === '' || relay_version_looks_valid($garbage['version'])),
    json_encode($garbage, JSON_UNESCAPED_UNICODE)
);

file_put_contents($versionFile, "3.9.20\n");

// API 优先：传一个"接口回包"进去（不再发 HTTP 请求），接口版本应当赢。
$version = relay_version_info([
    'ok'   => true,
    'raw'  => ['ok' => true, 'version' => '9.9.9', 'database' => 'up'],
    'data' => null,
]);

selftest_check(
    '接口报的版本优先于 VERSION 文件（9.9.9 而不是 3.9.20）',
    $version['version'] === '9.9.9' && $version['source'] === 'api-health',
    json_encode($version, JSON_UNESCAPED_UNICODE)
);

selftest_check(
    '接口与 VERSION 文件不一致时标出来（页面上写进 tooltip）',
    $version['mismatch'] === true && $version['file'] === '3.9.20'
);

selftest_check('左上角显示的版本文字就是它', relay_version_text() === '9.9.9');

selftest_check(
    '读不到版本时页面上显示 -（绝不编一个版本号）',
    relay_version_label('') === '-' && relay_version_label('   ') === '-'
    && relay_version_label('3.9.20') === '3.9.20'
);

selftest_check(
    '版本号格式校验：3.9.20 / v2.11.45-rc1 合法，空 / 日志行 / 超长不合法',
    relay_version_looks_valid('3.9.20') === true
    && relay_version_looks_valid('v2.11.45-rc1') === true
    && relay_version_looks_valid('') === false
    && relay_version_looks_valid('relay started at 12:00') === false
    && relay_version_looks_valid(str_repeat('9', 64)) === false
);

// ============================================================
// 9. 审计：安全入口后缀的值不写进日志
// ============================================================

selftest_section('9. 审计（值不进日志）');

selftest_check(
    '安全入口后缀只记「改了 / 清空了」，不记值',
    strpos(panel_setting_audit_detail('url_suffix', '/secret-path/'), 'secret-path') === false
    && strpos(panel_setting_audit_detail('url_suffix', ''), 'secret-path') === false
);

selftest_check(
    'logo 审计只记「更新了」，不记文件名内容',
    strpos(panel_setting_audit_detail('panel_logo', 'logo-abc.png'), 'logo-abc.png') === false
);

selftest_check(
    '普通设置照实记（键 = 值）',
    panel_setting_audit_detail('listen_port', '19102') === '面板设置 listen_port = 19102'
);

// ============================================================
// 汇总
// ============================================================

$pass = (int) $GLOBALS['selftest_pass'];
$fail = (int) $GLOBALS['selftest_fail'];

echo "\n" . str_repeat('=', 64) . "\n";
echo "面板设置自检：{$pass} 项通过，{$fail} 项失败\n";
echo str_repeat('=', 64) . "\n";

exit($fail === 0 ? 0 : 1);
