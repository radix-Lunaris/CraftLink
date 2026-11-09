<?php
/**
 * CraftLink 管理面板 —— 「面板（php-fpm）↔ 根代理（panel-agent.sh）」之间的文件队列。
 *
 * ============================================================================
 *  WHY THIS FILE EXISTS（为什么不直接在 PHP 里 exec）
 * ============================================================================
 *
 * 面板要在**运行中**改自己的 nginx 配置：渲染模板 → `nginx -t` 试一遍 →
 * 通过才原子替换 → `nginx -s reload` 热加载。这件事需要 root：
 *
 *   * 私钥是 0600 root，php-fpm 连读都读不到，更别说让 nginx 去读；
 *   * `nginx -s reload` 是给 root 的 master 进程发信号；
 *   * 写 /etc/nginx/... 本来就不该是网页进程的权限。
 *
 * 而 php-fpm 是以 nobody(65534) 跑的，**也不应该**为了这件事变成 root。
 * 所以两边用一个"请求文件 / 结果文件"的队列解耦：
 *
 *     php-fpm（nobody）                      panel-agent.sh（root，supervisord 常驻）
 *     ─────────────────                      ─────────────────────────────────────
 *     写 requests/<id>.json      ──────────▶  每 2 秒扫一次，认领这一条
 *                                             执行（渲染 / nginx -t / reload / 证书）
 *     读 results/<id>.json      ◀──────────  写 results/<id>.json（ok / 错误原文）
 *
 * 三条设计约束，都是为了"改配置改不死自己"：
 *
 *   1. **请求里不放秘密**：请求只写"做什么"（action）和参数（路径、域名），
 *      不带私钥内容；私钥只在 root 那一侧从磁盘读。
 *   2. **结果永远是文件，不是内存**：面板刷新页面还能看到上一次的结果，
 *      包括失败原因（"nginx -t 报了什么"）。
 *   3. **代理不在了也要能看出来**：心跳文件（agent-status.json）里有最后一次
 *      循环时间；面板上会写"根代理：X 秒前（正常 / 没在跑）"。代理没跑时，
 *      排队不会假装成功 —— 设置页会明确说"已排队但没人执行"。
 *
 * 这个文件和别的 lib 一样是"纯声明"：不连库、不读文件、不写文件，被调用时才动手。
 */

declare(strict_types=1);

// ============================================================
// 目录
// ============================================================

/**
 * 队列根目录：证书目录下面的 agent/（和证书同一个卷，重启不丢）。
 *
 * 之所以放在证书目录里：那个卷是**唯一**同时满足"root 可写 / 面板可写 /
 * 容器重建后还在"的挂载点（compose 里 ${MCLINK_PANEL_CERT_DIR}:/var/www/certs）。
 */
function panel_agent_spool_candidates(): array
{
    $candidates = [];

    $env = getenv('MCLINK_PANEL_AGENT_DIR');

    if ($env !== false && trim((string) $env) !== '') {
        $candidates[] = rtrim(trim((string) $env), '/');
    }

    $config = config('panel.agent_dir');

    if (is_string($config) && trim($config) !== '') {
        $candidates[] = rtrim(trim($config), '/');
    }

    $certDir = panel_cert_dir_candidates();

    foreach ($certDir as $dir) {
        $candidates[] = rtrim($dir, '/') . '/agent';
    }

    return array_values(array_unique($candidates));
}

/** 队列根目录（写入时挑第一个可写的，必要时建出来）。 */
function panel_agent_spool(bool $forWrite = false): string
{
    static $chosen = null;

    if ($chosen !== null) {
        return $chosen;
    }

    $candidates = panel_agent_spool_candidates();

    foreach ($candidates as $dir) {
        if (is_dir($dir) && is_writable($dir)) {
            $chosen = $dir;

            return $chosen;
        }
    }

    if ($forWrite) {
        foreach ($candidates as $dir) {
            $parent = dirname($dir);

            if (!is_dir($parent) && !@mkdir($parent, 0750, true)) {
                continue;
            }

            if (@mkdir($dir, 0755, true)) {
                $chosen = $dir;

                return $chosen;
            }
        }
    }

    $chosen = (string) ($candidates[0] ?? '');

    return $chosen;
}

function panel_agent_requests_dir(bool $forWrite = false): string
{
    return rtrim(panel_agent_spool($forWrite), '/') . '/requests';
}

function panel_agent_results_dir(bool $forWrite = false): string
{
    return rtrim(panel_agent_spool($forWrite), '/') . '/results';
}

/** 心跳文件：代理每次循环都会更新它（面板上用它判断"代理在不在跑"）。 */
function panel_agent_status_file(): string
{
    return rtrim(panel_agent_spool(), '/') . '/agent-status.json';
}

/** 代理自己的日志文件（docker logs 之外再留一份，方便面板读最后几行）。 */
function panel_agent_log_file(): string
{
    return rtrim(panel_agent_spool(), '/') . '/agent.log';
}

/** 队列目录能不能写（不能写 = 排队一定失败，页面上要说清楚）。 */
function panel_agent_writable(): bool
{
    $dir = panel_agent_requests_dir(true);

    return $dir !== '' && is_dir($dir) && is_writable($dir);
}

// ============================================================
// 请求
// ============================================================

/** 一次请求的文件名是个安全的 id：时间戳 + 随机十六进制。 */
function panel_agent_new_id(): string
{
    return date('Ymd-His') . '-' . bin2hex(random_bytes(4));
}

function panel_agent_request_path(string $id): string
{
    return rtrim(panel_agent_requests_dir(true), '/') . '/' . $id . '.json';
}

/**
 * 排一条请求给根代理。
 *
 * 参数只允许标量（字符串 / 数字 / 布尔）：队列文件是给 shell 那一侧读的，
 * 塞数组进去只会让两边对不上。调用方负责把路径之类的东西限定在允许的集合里
 * —— 代理那一侧**不信任**队列里的任何东西（它自己会再校验一遍）。
 *
 * @param array<string,scalar> $payload
 * @return array{ok:bool,id:string,error:string,path:string}
 */
function panel_agent_request(string $action, array $payload = [], string $actor = ''): array
{
    $result = ['ok' => false, 'id' => '', 'error' => '', 'path' => ''];

    $action = trim($action);

    if ($action === '' || preg_match('/^[a-z][a-z0-9-]{0,31}$/', $action) !== 1) {
        $result['error'] = t('队列动作名不合法。');

        return $result;
    }

    $dir = panel_agent_requests_dir(true);

    if (!is_dir($dir) && !@mkdir($dir, 0755, true)) {
        $result['error'] = sprintf(t('建不出队列目录：%s'), $dir);

        return $result;
    }

    if (!is_writable($dir)) {
        $result['error'] = sprintf(t('队列目录不可写：%s'), $dir);

        return $result;
    }

    $clean = [];

    foreach ($payload as $name => $value) {
        if (!is_string($name) || preg_match('/^[a-z][a-z0-9_]{0,31}$/', $name) !== 1) {
            continue;
        }

        if (is_bool($value)) {
            $clean[$name] = $value ? '1' : '0';
            continue;
        }

        if (is_int($value)) {
            $clean[$name] = (string) $value;
            continue;
        }

        if (is_string($value)) {
            // 换行会让 shell 那一侧的分行解析错位，直接换掉。
            $clean[$name] = str_replace(["\r", "\n"], ' ', $value);
        }
    }

    $id = panel_agent_new_id();
    $path = panel_agent_request_path($id);

    $payload = [
        'schema'     => 1,
        'id'         => $id,
        'action'     => $action,
        'created_at' => date('c'),
        'actor'      => substr(str_replace(["\r", "\n"], ' ', $actor), 0, 64),
        'args'       => $clean,
    ];

    $json = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

    if (!is_string($json)) {
        $result['error'] = t('请求无法序列化成 JSON。');

        return $result;
    }

    // 直接写最终文件名（代理只认 *.json），但**先写 .tmp 再改名**：代理不会
    // 读到半截文件。代理那一侧对坏 JSON 也是"跳过并留着"，不会崩。
    $temp = $path . '.tmp';

    if (@file_put_contents($temp, $json . "\n") === false) {
        $result['error'] = sprintf(t('写不进队列文件：%s'), $temp);

        return $result;
    }

    if (!@rename($temp, $path)) {
        @unlink($temp);
        $result['error'] = sprintf(t('队列文件改名失败：%s'), $path);

        return $result;
    }

    $result['ok'] = true;
    $result['id'] = $id;
    $result['path'] = $path;

    return $result;
}

// ============================================================
// 结果
// ============================================================

/**
 * 读一条结果（没有就 found=false）。
 *
 * @return array{found:bool,ok:bool,id:string,action:string,message:string,error:string,
 *               at:string,detail:array<string,mixed>}
 */
function panel_agent_result(string $id): array
{
    $empty = [
        'found'   => false,
        'ok'      => false,
        'id'      => $id,
        'action'  => '',
        'message' => '',
        'error'   => '',
        'at'      => '',
        'detail'  => [],
    ];

    if ($id === '' || preg_match('/^[A-Za-z0-9._-]{1,64}$/', $id) !== 1) {
        return $empty;
    }

    $path = rtrim(panel_agent_results_dir(), '/') . '/' . $id . '.json';

    if (!is_file($path) || !is_readable($path)) {
        return $empty;
    }

    $raw = @file_get_contents($path);

    if ($raw === false) {
        return $empty;
    }

    $decoded = json_decode((string) $raw, true);

    if (!is_array($decoded)) {
        return $empty;
    }

    return [
        'found'   => true,
        'ok'      => !empty($decoded['ok']),
        'id'      => (string) ($decoded['id'] ?? $id),
        'action'  => (string) ($decoded['action'] ?? ''),
        'message' => (string) ($decoded['message'] ?? ''),
        'error'   => (string) ($decoded['error'] ?? ''),
        'at'      => (string) ($decoded['at'] ?? ''),
        'detail'  => is_array($decoded['detail'] ?? null) ? $decoded['detail'] : [],
    ];
}

/** 队列里还排着多少条（面板上显示"排队中 N 条"）。 */
function panel_agent_pending_count(): int
{
    $dir = panel_agent_requests_dir();

    if ($dir === '' || !is_dir($dir)) {
        return 0;
    }

    $files = glob(rtrim($dir, '/') . '/*.json');

    return is_array($files) ? count($files) : 0;
}

/** 最近一条结果（面板上"上次生效结果"）。 */
function panel_agent_last_result(): array
{
    $dir = panel_agent_results_dir();

    if ($dir === '' || !is_dir($dir)) {
        return panel_agent_result('');
    }

    $files = glob(rtrim($dir, '/') . '/*.json');

    if (!is_array($files) || $files === []) {
        return panel_agent_result('');
    }

    sort($files);

    $last = (string) $files[count($files) - 1];

    return panel_agent_result(basename($last, '.json'));
}

/**
 * 代理心跳。
 *
 * @return array{running:bool,age:int,pid:string,version:string,started_at:string,
 *               last_loop:string,nginx_ok:bool,message:string}
 */
function panel_agent_heartbeat(bool $refresh = false): array
{
    static $cache = null;

    if ($refresh) {
        $cache = null;
    }

    if ($cache !== null) {
        return $cache;
    }

    $empty = [
        'running'    => false,
        'age'        => -1,
        'pid'        => '',
        'version'    => '',
        'started_at' => '',
        'last_loop'  => '',
        'nginx_ok'   => false,
        'message'    => t('根代理还没跑过（或者容器刚起来）。'),
    ];

    $path = panel_agent_status_file();

    if (!is_file($path) || !is_readable($path)) {
        $cache = $empty;

        return $cache;
    }

    $raw = @file_get_contents($path);

    if ($raw === false) {
        $cache = $empty;

        return $cache;
    }

    $decoded = json_decode((string) $raw, true);

    if (!is_array($decoded)) {
        $cache = $empty;

        return $cache;
    }

    $stamp = (int) ($decoded['last_loop_ts'] ?? 0);
    $age = $stamp > 0 ? max(0, time() - $stamp) : -1;

    // 15 秒没有心跳就认为代理没在跑（循环间隔默认 2 秒，心跳每 15 秒写一次）。
    $running = $age >= 0 && $age <= 15;

    $cache = [
        'running'    => $running,
        'age'        => $age,
        'pid'        => (string) ($decoded['pid'] ?? ''),
        'version'    => (string) ($decoded['version'] ?? ''),
        'started_at' => (string) ($decoded['started_at'] ?? ''),
        'last_loop'  => (string) ($decoded['last_loop'] ?? ''),
        'nginx_ok'   => !empty($decoded['nginx_ok']),
        'message'    => $running
            ? sprintf(t('根代理正常（%d 秒前还在跑）。'), $age)
            : sprintf(t('根代理好像没在跑（最后一次心跳 %d 秒前）。'), $age),
    ];

    return $cache;
}

/** 代理日志的最后几行（页面上"看日志"用；读不到就回空数组）。 */
function panel_agent_log_tail(int $lines = 20): array
{
    $path = panel_agent_log_file();

    if (!is_file($path) || !is_readable($path)) {
        return [];
    }

    $raw = @file_get_contents($path);

    if ($raw === false || trim((string) $raw) === '') {
        return [];
    }

    $all = explode("\n", trim((string) $raw));

    return array_slice($all, -1 * max(1, $lines));
}
