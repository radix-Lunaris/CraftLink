<?php
/**
 * CraftLink 面板 —— 命令行工具（容器内，root 运行）。
 *
 * ============================================================================
 *  WHY THIS FILE EXISTS
 * ============================================================================
 *
 * 三件事需要一个"不开浏览器也能改面板设置"的入口：
 *
 *   1. **SSH 终极管理台**（`mclink reset-entry` / `unbind-domain` / `ssl-off`）：
 *      安全入口 / 域名 / 证书把自己锁在外面时，这是唯一还能进去的路。
 *      它必须改到**同一份设置**（数据库 + 配置文件备份），否则"命令说改了、
 *      面板还是老样子"。
 *   2. **根代理**（panel-agent.sh）：装完证书要把 ssl_cert_file / ssl_key_file
 *      写成新路径 —— 设置只有一个写入口（panel_setting_save()），代理也走这里。
 *   3. **升级播种**：老部署的 MCLINK_PANEL_SECRET_PATH / MCLINK_PANEL_DOMAIN /
 *      MCLINK_PANEL_PORT 一次性写进设置，行为不变。
 *
 *  调用方式：
 *      docker exec mc-panel php /var/www/bin/mclink-panel.php status
 *      docker exec mc-panel php /var/www/bin/mclink-panel.php reset-entry
 *   宿主机上更方便的是 `mclink`（scripts/mclink-console.sh）——它替你 exec。
 *
 * [!] 权限：这个脚本**按 root 设计**（它跑在容器里，只有 root 能 docker exec）。
 *     非 CLI 环境（网页请求）直接拒绝执行，免得有人通过 web 调它。
 *
 * [!] 数据库写不进去时（只读账号 / 磁盘满），`set` 类命令会退一步把值写进
 *     "急救覆盖文件"（优先级高于数据库），并在输出里写明 degraded=1 ——
 *     这是"重置安全入口必须真的生效"的兜底。清除用 `overrides --clear`。
 */

declare(strict_types=1);

// ============================================================
// 0. 只允许命令行
// ============================================================

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    echo '';
    exit(1);
}

// ============================================================
// 1. 加载面板的共用库（容器布局 / 仓库布局都认）
// ============================================================

$mclinkLibCandidates = [
    '/var/www/lib/common.php',
    dirname(__DIR__) . '/lib/common.php',
];

$mclinkLib = '';

foreach ($mclinkLibCandidates as $mclinkCandidate) {
    if (is_file($mclinkCandidate)) {
        $mclinkLib = $mclinkCandidate;
        break;
    }
}

if ($mclinkLib === '') {
    fwrite(STDERR, t("找不到 lib/common.php（试过：") . implode(', ', $mclinkLibCandidates) . t("）") . "\n");
    exit(1);
}

require $mclinkLib;

// CLI 专用的几个纯函数（输出 / 解析参数）住在 lib 里：仓库门禁的
// "没有调用未定义的函数"只认 panel/lib/*.php 里声明的函数。
$mclinkCliLib = is_file("/var/www/lib/panel_cli.php")
    ? "/var/www/lib/panel_cli.php"
    : dirname($mclinkLib) . "/panel_cli.php";

require_once $mclinkCliLib;

// ============================================================
// 2. 小工具
// ============================================================

// ============================================================
// 3. 子命令
// ============================================================

$argv = $argv ?? [];
$command = (string) ($argv[1] ?? 'status');
$options = cli_options($argv);
$args = cli_arguments($argv);
$asJson = isset($options['json']);

$state = panel_nginx_state(true);

switch ($command) {
    // ---- 状态（mclink status / ssl-status 用它）------------------------
    case 'status':
    case 'show':
        $rows = [
            'panel_version' => PANEL_VERSION,
            'settings_file' => panel_settings_file(true)['path'],
            'db_ok'         => panel_settings_ensure_schema(true)['ok'] ? '1' : '0',
            'db_rows'       => (string) panel_settings_all(true)['db_rows'],
            'overrides'     => implode(',', array_keys(panel_settings_overrides(true)['values'])),
            'listen'        => panel_listen_config()['nginx_listen'],
            'http_port'     => (string) panel_http_port(),
            'https_port'    => (string) panel_https_port(),
            'rescue_port'   => (string) panel_rescue_port(),
            'rescue_url'    => panel_rescue_url(),
            'entry_path'    => panel_listen_config()['entry_path'],
            'unauth_code'   => panel_listen_config()['unauth_code'],
            'bound_domain'  => panel_bound_domain(),
            'force_https'   => panel_force_https() ? '1' : '0',
            'ssl_mode'      => panel_ssl_mode(),
            'ssl_cert_file' => panel_ssl_cert_file(),
            'ssl_key_file'  => panel_ssl_key_file(),
            'acme_email'    => panel_acme_email(),
            'nginx_dir'     => $state['dir'],
            'nginx_main'    => $state['main_exists'] ? $state['main_file'] : '',
            'nginx_applied' => (string) $state['generated_at'],
            'agent'         => $state['agent']['running'] ? '1' : '0',
            'agent_age'     => (string) $state['agent']['age'],
            'pending'       => (string) $state['pending'],
        ];

        if ($asJson) {
            cli_json($rows);
            break;
        }

        cli_line(t('CraftLink 面板设置'));
        cli_line('  ' . sprintf(t('面板版本：%s'), $rows['panel_version']));

        foreach ($rows as $name => $value) {
            cli_line(sprintf('  %-14s = %s', $name, $value));
        }

        cli_line('');
        cli_line('  ' . panel_ssl_effective_note());
        cli_line('  ' . sprintf(
            t('急救入口：%s（只在本机可访问；安全入口 / 域名 / 证书配错时从这里进）'),
            $rows['rescue_url']
        ));
        break;

    // ---- 急救：重置安全入口 -------------------------------------------
    case 'reset-entry':
    case 'set-entry':
        $path = (string) ($options['path'] ?? ($args[0] ?? ''));

        if ($command === 'reset-entry' && $path === '') {
            $path = '';
        }

        if ($path !== '' && $path !== '/') {
            $path = trim(trim($path), '/');
        } else {
            $path = '';
        }

        $saved = cli_set('url_suffix', $path);

        if (!$saved['ok']) {
            cli_line(t('重置失败：') . $saved['error']);
            exit(1);
        }

        $applied = cli_apply('cli:' . $command);
        $result = $applied['ok'] ? cli_wait_result($applied['id']) : ['ok' => false, 'message' => $applied['error']];

        cli_line(sprintf(
            t('安全入口已改为：%s'),
            $saved['value'] === '' ? t('（不使用安全入口）') : '/' . $saved['value'] . '/'
        ));

        if ($saved['degraded']) {
            cli_line(sprintf(t('[!] 数据库写不进去，这一条写进了急救覆盖文件：%s'), $saved['path']));
        }

        cli_line($result['ok']
            ? t('nginx 已按新入口重新加载。')
            : sprintf(t('已写入设置，但 nginx 还没生效（根代理没跑？）：%s'), (string) ($result['message'] ?? '')));

        cli_line(sprintf(t('新地址：http://<域名或IP>/%s'), $saved['value']));
        cli_line(sprintf(t('万一进不去，用急救入口：%s'), panel_rescue_url()));
        break;

    // ---- 急救：解绑域名 -------------------------------------------------
    case 'unbind-domain':
        $saved = cli_set('bound_domain', '');

        if (!$saved['ok']) {
            cli_line(t('解绑失败：') . $saved['error']);
            exit(1);
        }

        $applied = cli_apply('cli:unbind-domain');
        $result = $applied['ok'] ? cli_wait_result($applied['id']) : ['ok' => false, 'message' => $applied['error']];

        cli_line(t('域名绑定已清空：现在任何域名 / IP 都能访问面板。'));

        if ($saved['degraded']) {
            cli_line(sprintf(t('[!] 数据库写不进去，这一条写进了急救覆盖文件：%s'), $saved['path']));
        }

        cli_line($result['ok']
            ? t('nginx 已按新配置重新加载。')
            : sprintf(t('已写入设置，但 nginx 还没生效：%s'), (string) ($result['message'] ?? '')));
        break;

    // ---- 急救：关掉 HTTPS（证书坏了的第一反应）--------------------------
    case 'ssl-off':
        $saved = cli_set('ssl_mode', 'off');

        if (!$saved['ok']) {
            cli_line(t('关闭 HTTPS 失败：') . $saved['error']);
            exit(1);
        }

        $applied = cli_apply('cli:ssl-off');
        $result = $applied['ok'] ? cli_wait_result($applied['id']) : ['ok' => false, 'message' => $applied['error']];

        cli_line(t('SSL 模式已改成「不用 HTTPS」：面板回到纯 HTTP，证书文件一个都没删。'));

        if ($saved['degraded']) {
            cli_line(sprintf(t('[!] 数据库写不进去，这一条写进了急救覆盖文件：%s'), $saved['path']));
        }

        cli_line($result['ok']
            ? t('nginx 已重新加载。')
            : sprintf(t('已写入设置，但 nginx 还没生效：%s'), (string) ($result['message'] ?? '')));
        break;

    // ---- 通用设置写入 ---------------------------------------------------
    case 'set':
        $key = (string) ($args[0] ?? '');
        $value = (string) ($args[1] ?? '');

        if ($key === '' || !panel_setting_is_known($key)) {
            cli_line(t('用法：set <键> <值>（键见面板设置页）'));
            exit(2);
        }

        if (panel_setting_is_internal($key)) {
            cli_line(t('这个键只能在面板里改。'));
            exit(2);
        }

        $saved = cli_set($key, $value, isset($options['force-file']));

        if (!$saved['ok']) {
            cli_line(t('写入失败：') . $saved['error']);
            exit(1);
        }

        cli_line(sprintf(t('已写入 %s = %s'), $key, $saved['value']));

        if ($saved['degraded']) {
            cli_line(sprintf(t('[!] 数据库写不进去，这一条写进了急救覆盖文件：%s'), $saved['path']));
        }

        if (!isset($options['no-apply'])) {
            $applied = cli_apply('cli:set:' . $key);
            $result = $applied['ok'] ? cli_wait_result($applied['id']) : ['ok' => false, 'message' => $applied['error']];

            cli_line($result['ok']
                ? t('nginx 已重新加载。')
                : sprintf(t('已写入设置，但 nginx 还没生效：%s'), (string) ($result['message'] ?? '')));
        }
        break;

    // ---- 指向一对新证书（根代理装完证书以后调它）------------------------
    case 'set-cert':
        $cert = trim((string) ($options['cert'] ?? ''));
        $key = trim((string) ($options['key'] ?? ''));
        $mode = trim((string) ($options['mode'] ?? 'upload'));

        if ($cert === '' || $key === '') {
            cli_line(t('用法：set-cert --cert <证书路径> --key <私钥路径> [--mode upload|selfsigned|acme]'));
            exit(2);
        }

        foreach ([['ssl_cert_file', $cert], ['ssl_key_file', $key], ['ssl_mode', $mode]] as $pair) {
            $saved = cli_set($pair[0], $pair[1]);

            if (!$saved['ok']) {
                cli_line(sprintf(t('写入 %s 失败：%s'), $pair[0], $saved['error']));
                exit(1);
            }

            if ($saved['degraded']) {
                cli_line(sprintf(t('degraded=1 %s 写进了急救覆盖文件'), $pair[0]));
            }
        }

        if (!isset($options['no-apply'])) {
            $applied = cli_apply('cli:set-cert');
            $result = $applied['ok'] ? cli_wait_result($applied['id']) : ['ok' => false, 'message' => $applied['error']];

            cli_line($result['ok']
                ? t('证书设置已写入，nginx 已重新加载。')
                : sprintf(t('证书设置已写入，但 nginx 还没生效：%s'), (string) ($result['message'] ?? '')));
        } else {
            cli_line(t('证书设置已写入（还没让 nginx 生效）。'));
        }

        cli_line(cli_ssl_line());
        break;

    // ---- 渲染（不开 nginx）---------------------------------------------
    case 'render':
        $dir = trim((string) ($options['dir'] ?? ''));

        $rendered = panel_nginx_render($dir);

        if ($asJson) {
            cli_json($rendered);
            break;
        }

        if (!$rendered['ok']) {
            cli_line(t('渲染失败：') . $rendered['error']);
            exit(1);
        }

        cli_line(sprintf(t('已渲染 %d 个文件到 %s'), count($rendered['files']), $rendered['dir']));

        foreach ($rendered['notes'] as $note) {
            cli_line('  [!] ' . $note);
        }
        break;

    // ---- 让当前配置生效（排队给根代理）---------------------------------
    case 'apply':
        $applied = cli_apply('cli:apply');
        $result = $applied['ok'] ? cli_wait_result($applied['id']) : ['ok' => false, 'message' => $applied['error']];

        if ($asJson) {
            cli_json([
                'ok'      => (bool) $result['ok'],
                'id'      => $applied['id'],
                'message' => (string) ($result['message'] ?? ''),
            ]);
            break;
        }

        cli_line($result['ok']
            ? t('nginx 已按当前设置重新加载。')
            : sprintf(t('生效失败：%s'), (string) ($result['message'] ?? $applied['error'])));

        if (!$result['ok']) {
            exit(1);
        }
        break;

    // ---- 证书状态（让根代理重新检查一遍并写 status.json）----------------
    case 'cert-status':
        $queued = panel_ssl_request_status(cli_actor());

        if (!$queued['ok']) {
            cli_line(t('排队失败：') . $queued['error']);
            exit(1);
        }

        $result = cli_wait_result($queued['id']);
        $status = panel_ssl_status(true);
        $active = panel_ssl_active_cert();

        if ($asJson) {
            cli_json([
                'ok'      => (bool) $result['ok'],
                'message' => (string) ($result['message'] ?? ''),
                'active'  => $active,
                'certs'   => $status['certs'],
                'acme'    => $status['acme'],
            ]);
            break;
        }

        cli_line(cli_ssl_line());

        foreach ($status['certs'] as $row) {
            cli_line(sprintf(
                '  - %s  %s  %s  %s',
                (string) ($row['name'] ?? ''),
                (string) ($row['subject'] ?? ''),
                (string) ($row['not_after'] ?? ''),
                isset($row['days_left']) ? sprintf(t('还剩 %s 天'), (string) $row['days_left']) : ''
            ));
        }

        if ($status['certs'] === []) {
            cli_line('  ' . t('（还没有装过证书）'));
        }
        break;

    // ---- 急救覆盖：查看 / 清除 -----------------------------------------
    case 'overrides':
        if (isset($options['clear'])) {
            $keys = trim((string) $options['clear']);

            $removed = panel_settings_overrides_clear(
                $keys === '' || $keys === '1' ? [] : explode(',', $keys),
                cli_actor()
            );

            if ($removed['ok']) {
                cli_line(sprintf(t('已清除急救覆盖：%s'), implode(', ', $removed['removed'])));
                $applied = cli_apply('cli:overrides-clear');
                cli_wait_result($applied['id']);
            } else {
                cli_line(t('清除失败：') . $removed['error']);
                exit(1);
            }

            break;
        }

        $overrides = panel_settings_overrides(true);

        if ($asJson) {
            cli_json($overrides);
            break;
        }

        if ($overrides['values'] === []) {
            cli_line(t('现在没有急救覆盖。'));
            break;
        }

        cli_line(sprintf(t('急救覆盖文件：%s'), $overrides['path']));

        foreach ($overrides['values'] as $name => $value) {
            cli_line(sprintf('  %s = %s', $name, $value));
        }

        cli_line('  ' . t('清除：overrides --clear'));
        break;

    // ---- 从环境变量播种（升级路径，只在设置里没有那个键时写）------------
    case 'seed-from-env':
        $seeded = [];

        $pairs = [
            'url_suffix'   => trim((string) (getenv('MCLINK_PANEL_SECRET_PATH') ?: '')),
            'bound_domain' => trim((string) (getenv('MCLINK_PANEL_DOMAIN') ?: '')),
            'listen_port'  => trim((string) (getenv('MCLINK_PANEL_PORT') ?: '')),
        ];

        foreach ($pairs as $key => $value) {
            if ($value === '') {
                continue;
            }

            // 只在"设置里还没有这一条"时播种：一旦播种过，用户清空它就该
            // 真的清空（不能被环境变量又顶回来 —— 那是自锁的经典成因）。
            if (panel_setting_is_custom($key)) {
                continue;
            }

            $saved = cli_set($key, $value);

            if ($saved['ok']) {
                $seeded[] = $key;
            }
        }

        if ($asJson) {
            cli_json(['ok' => true, 'seeded' => $seeded]);
            break;
        }

        cli_line($seeded === []
            ? t('没有需要播种的环境变量（设置里已经有值）。')
            : sprintf(t('已从环境变量播种：%s'), implode(', ', $seeded)));
        break;

    // ---- 急救入口信息 ---------------------------------------------------
    case 'rescue':
        cli_line(sprintf(t('急救入口：%s'), panel_rescue_url()));
        cli_line('  ' . t('它只监听容器内，并且只发布到宿主机回环地址（公网到不了）。'));
        cli_line('  ' . t('不判安全入口、不判域名、不跳 HTTPS —— 面板被自己配错时的最后一条路。'));
        cli_line('  ' . sprintf(t('在服务器上：curl -sS %shealthz'), panel_rescue_url()));
        break;

    // ---- 帮助 -----------------------------------------------------------
    case 'help':
    case '--help':
    case '-h':
    default:
        cli_line(t('CraftLink 面板命令行（容器内 root）'));
        cli_line('');
        cli_line('  status [--json]                      ' . t('当前设置 / 证书 / nginx 状态'));
        cli_line('  reset-entry [--path <suffix>]           ' . t('重置（或设置）URL 安全入口'));
        cli_line('  unbind-domain                        ' . t('解绑域名'));
        cli_line('  ssl-off                              ' . t('关掉 HTTPS（证书文件保留）'));
        cli_line('  set <key> <value> [--no-apply]           ' . t('改一条设置'));
        cli_line('  set-cert --cert <path> --key <path> --mode <mode>' . t('指向一对新证书'));
        cli_line('  render [--dir <dir>] [--json]        ' . t('只渲染 nginx 配置（不生效）'));
        cli_line('  apply [--json]                       ' . t('重新渲染并热加载'));
        cli_line('  cert-status [--json]                 ' . t('刷新证书状态（到期时间等）'));
        cli_line('  overrides [--clear [key,key]]           ' . t('查看 / 清除急救覆盖'));
        cli_line('  seed-from-env                        ' . t('把老部署的环境变量播种进设置'));
        cli_line('  rescue                               ' . t('打印急救入口地址'));
        break;
}

exit(0);
