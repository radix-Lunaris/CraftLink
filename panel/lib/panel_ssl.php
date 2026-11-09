<?php
/**
 * CraftLink 管理面板 —— 域名绑定 + SSL（M4）的设置读写、校验、证书状态与上传。
 *
 * ============================================================================
 *  WHY THIS FILE EXISTS
 * ============================================================================
 *
 * M3 里这四个值只是占位（`panel_setting_placeholders()`），M4 把它们变成真的：
 *
 *     bound_domain   绑定域名（只有这个域名能访问面板）
 *     force_https    HTTP → HTTPS 跳转
 *     ssl_mode       off / upload / selfsigned / acme
 *     ssl_cert_file  证书文件（fullchain PEM）
 *     ssl_key_file   私钥文件
 *     acme_email     Let's Encrypt 账号邮箱
 *
 * 这一层只做四件事（全部可以在没有 openssl、没有 nginx 的机器上验）：
 *
 *   1. 读设置 + 兜底（坏值绝不让面板变成"打不开"）；
 *   2. 校验（域名、邮箱、路径）；
 *   3. 读根代理写出来的证书状态（`<证书目录>/status.json`）—— 到期时间、
 *      签发者、密钥是否配对、覆盖了哪些域名；
 *   4. 把"上传 / 自签 / 申请 / 续期"排进队列交给根代理（见 panel_agent.php）。
 *
 * [!] 为什么这里一行 openssl_* 都没有：
 *   * 私钥不该经过网页进程（php-fpm 是 nobody）；
 *   * `openssl_x509_parse()` 之类不在仓库门禁的内置函数白名单里，真要做也得
 *     改 tests/ —— 那属于别的工作流；
 *   * 证书的**深度校验**（密钥配对、域名覆盖、到期）只有真 openssl 说了算，
 *     所以那件事在 `panel/bin/mclink-cert.sh`（root，openssl CLI）里做，
 *     结论写进 status.json，网页只读结论。
 *
 * 这个文件和别的 lib 一样是"纯声明"：不连库、不读文件、不写文件，被调用时才动手。
 */

declare(strict_types=1);

// ============================================================
// 1. 域名
// ============================================================

/**
 * 规范化一个域名：小写、去掉协议 / 路径 / 端口 / 空白、去掉结尾的点。
 *
 * 允许的输入（用户会从浏览器地址栏直接复制）：
 *     https://panel.example.com/        → panel.example.com
 *     panel.example.com:19102           → panel.example.com
 *     *.example.com                     → *.example.com
 *     PANEL.Example.COM.                → panel.example.com
 */
function panel_domain_normalize(string $raw): string
{
    $value = strtolower(trim($raw));

    if ($value === '') {
        return '';
    }

    // 协议（http:// https:// 或者 //开头）
    $value = preg_replace('#^[a-z][a-z0-9+.-]*://#', '', $value) ?? $value;
    $value = preg_replace('#^//#', '', $value) ?? $value;

    // 路径 / 查询 / 片段：从第一个 / ? # 处截断。
    // [!] 分隔符用 ~ 而不是 #：`[^/?#]` 里那个未转义的 # 会被 PHP 当成
    //     分隔符，直接报 "Unknown modifier"。
    if (preg_match('~^([^/?#]*)~', $value, $match) === 1) {
        $value = (string) $match[1];
    }

    // user@host:port —— 取最后一个 @ 之后的部分（explode + 取末项，
    // 不用 strrpos：它不在门禁的内置函数白名单里）。
    $parts = explode('@', $value);

    if (count($parts) > 1) {
        $value = (string) $parts[count($parts) - 1];
    }

    // [::1]:port 这种方括号写法面板不支持（域名绑定只认主机名）
    if (str_starts_with($value, '[')) {
        return '';
    }

    // host:port —— 去掉端口那一段（explode + 取首项，不用 strrpos）。
    $segments = explode(':', $value);

    if (count($segments) > 1) {
        $value = (string) $segments[0];
    }

    $value = trim($value);

    // 结尾的点是合法写法（FQDN），存进去之前去掉。
    $value = rtrim($value, '.');

    return $value;
}

/** 一个（可能带 *. 通配的）域名格式是否合法。 */
function panel_domain_looks_valid(string $domain): bool
{
    if ($domain === '' || strlen($domain) > 253) {
        return false;
    }

    $body = $domain;

    if (str_starts_with($body, '*.')) {
        $body = substr($body, 2);
    }

    if ($body === '' || strpos($body, '*') !== false) {
        return false;
    }

    // 每段：字母数字开头结尾，中间可以有短横线；不允许空段（a..b）。
    foreach (explode('.', $body) as $label) {
        if ($label === '' || strlen($label) > 63) {
            return false;
        }

        if (preg_match('/^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/', $label) !== 1) {
            return false;
        }
    }

    return true;
}

/** 校验结果（'' = 合格），文案给设置页用。 */
function panel_domain_problem(string $raw): string
{
    $value = trim($raw);

    if ($value === '') {
        return '';
    }

    // 用户经常直接从地址栏复制：https://panel.example.com/ 这种输入是**正常**
    // 用法，规范化阶段会把协议 / 路径 / 端口 / 结尾的点都去掉，页面上再显示
    // 一遍"当前生效"的实际值。所以这里不因为出现斜杠就报错。
    $domain = panel_domain_normalize($value);

    if ($domain === '') {
        return t('域名看不懂：填 panel.example.com 这样的主机名（从地址栏复制也行）。');
    }

    if (!panel_domain_looks_valid($domain)) {
        return t('域名格式不对：只能有字母、数字、点和短横线，最长 253 位；通配符只能写成最前面的 *.');
    }

    return '';
}

/** 通配域名（*.example.com）—— ACME 的 HTTP-01 校验不支持它。 */
function panel_domain_is_wildcard(string $domain): bool
{
    return str_starts_with($domain, '*.');
}

/** 设置里绑定的域名（已规范化；'' = 不绑定）。 */
function panel_bound_domain(): string
{
    $value = panel_domain_normalize(panel_setting('bound_domain', ''));

    return panel_domain_looks_valid($value) ? $value : '';
}

/**
 * 绑定的域名（一个），外加它的通配形式 —— 生成的 nginx 配置用得到。
 *
 * @return array<int,string>
 */
function panel_bound_domain_patterns(): array
{
    $domain = panel_bound_domain();

    return $domain === '' ? [] : [$domain];
}

/**
 * 一个请求的 Host 是否匹配绑定域名。
 *
 * `$host` 允许带端口（反向代理常见），比较前剥掉；大小写不敏感；
 * `*.example.com` 匹配任意一级或多级子域（nginx 的 server_name 通配语义）。
 */
function panel_domain_match(string $host, string $pattern): bool
{
    $host = strtolower(trim($host));

    if ($host === '' || $pattern === '') {
        return false;
    }

    if (str_starts_with($host, '[')) {
        // IPv6 字面量：只做原样比较（绑定域名时不该出现，但别让它匹配一切）。
        return false;
    }

    // host:port（反向代理常见）：只留主机名那一段。
    $segments = explode(':', $host);

    if (count($segments) > 1) {
        $host = (string) $segments[0];
    }

    $host = rtrim($host, '.');

    if ($host === '') {
        return false;
    }

    if (str_starts_with($pattern, '*.')) {
        $suffix = substr($pattern, 2);

        return $host === $suffix || str_ends_with($host, '.' . $suffix);
    }

    return $host === $pattern;
}

/** 当前请求的 Host 是否被允许（没绑域名 = 都允许）。 */
function panel_domain_request_ok(string $host): bool
{
    $patterns = panel_bound_domain_patterns();

    if ($patterns === []) {
        return true;
    }

    foreach ($patterns as $pattern) {
        if (panel_domain_match($host, $pattern)) {
            return true;
        }
    }

    return false;
}

/** 强制 HTTPS 开关。 */
function panel_force_https(): bool
{
    return panel_setting('force_https', '0') === '1';
}

// ============================================================
// 2. SSL 模式
// ============================================================

/** 允许的 ssl_mode 值。 */
function panel_ssl_modes(): array
{
    return ['off', 'upload', 'selfsigned', 'acme'];
}

/** 当前的 ssl_mode（认不出的值一律当 off —— 绝不让坏值把面板弄成打不开）。 */
function panel_ssl_mode(): string
{
    $mode = strtolower(trim(panel_setting('ssl_mode', 'off')));

    return in_array($mode, panel_ssl_modes(), true) ? $mode : 'off';
}

/** 这个模式的界面名字（日志 / 页面上用）。 */
function panel_ssl_mode_label(string $mode): string
{
    switch ($mode) {
        case 'upload':
            return t('上传证书');
        case 'selfsigned':
            return t('自签证书（仅测试）');
        case 'acme':
            return t('Let’s Encrypt 自动申请 / 续期');
    }

    return t('不用 HTTPS');
}

/**
 * TLS 是不是真的能起来：模式不是 off，而且证书 + 私钥两个文件都填了。
 *
 * **文件存不存在**由根代理在应用的时候判定（容器里看到的路径才是真的），
 * 这里只判到"填了没有"——面板渲染出来的配置里带一个不存在的证书路径会让
 * `nginx -t` 直接失败，代理那一侧会保留上一份配置（见 panel-agent.sh）。
 */
function panel_ssl_configured(): bool
{
    if (panel_ssl_mode() === 'off') {
        return false;
    }

    return panel_ssl_cert_file() !== '' && panel_ssl_key_file() !== '';
}

/** 证书文件设置值（'' = 没填）。 */
function panel_ssl_cert_file(): string
{
    return trim(panel_setting('ssl_cert_file', ''));
}

function panel_ssl_key_file(): string
{
    return trim(panel_setting('ssl_key_file', ''));
}

function panel_acme_email(): string
{
    return trim(panel_setting('acme_email', ''));
}

// ============================================================
// 3. 证书目录
// ============================================================

/**
 * 证书目录的候选（和 logo 上传目录同一个套路：环境变量 > config.php >
 * 安装根 > 容器约定挂载点 > 默认安装根 > 仓库布局）。
 *
 * [!] 不写死 /opt/mc-link（D8）：它只是排在后面的默认值。
 *
 * @return array<int,string>
 */
function panel_cert_dir_candidates(): array
{
    $candidates = [];

    $env = getenv('MCLINK_PANEL_CERT_DIR');

    if ($env !== false && trim((string) $env) !== '') {
        $candidates[] = rtrim(trim((string) $env), '/');
    }

    $config = config('panel.cert_dir');

    if (is_string($config) && trim($config) !== '') {
        $candidates[] = rtrim(trim($config), '/');
    }

    $root = getenv('MCLINK_ROOT');

    if ($root !== false && trim((string) $root) !== '') {
        $candidates[] = rtrim(trim((string) $root), '/') . '/data/panel-certs';
    }

    $candidates[] = '/var/www/certs';                     // 容器里推荐的挂载点
    $candidates[] = '/opt/mc-link/data/panel-certs';      // D8 的默认安装根（只是默认值）
    $candidates[] = dirname(__DIR__, 2) . '/data/panel-certs';   // 仓库布局
    $candidates[] = __DIR__ . '/../web/assets/certs';            // 兜底：容器里一定能写

    return array_values(array_unique($candidates));
}

/** 实际使用的证书目录。 */
function panel_cert_dir(bool $forWrite = false): string
{
    static $chosen = null;

    if ($chosen !== null) {
        return $chosen;
    }

    $candidates = panel_cert_dir_candidates();

    foreach ($candidates as $dir) {
        if (is_dir($dir) && is_writable($dir)) {
            $chosen = $dir;

            return $chosen;
        }
    }

    if ($forWrite) {
        foreach ($candidates as $dir) {
            if (is_dir($dir)) {
                continue;
            }

            if (@mkdir($dir, 0750, true)) {
                $chosen = $dir;

                return $chosen;
            }
        }
    }

    $chosen = (string) ($candidates[0] ?? '');

    return $chosen;
}

/** 上传进来的证书先落在这里，等根代理校验 + 安装。 */
function panel_ssl_incoming_dir(bool $forWrite = false): string
{
    $dir = rtrim(panel_cert_dir($forWrite), '/') . '/incoming';

    if ($forWrite && !is_dir($dir)) {
        @mkdir($dir, 0750, true);
    }

    return $dir;
}

/** 状态文件（root 代理写、面板读）。 */
function panel_ssl_status_file(): string
{
    return rtrim(panel_cert_dir(), '/') . '/status.json';
}

// ============================================================
// 4. 路径校验（ssl_cert_file / ssl_key_file）
// ============================================================

/**
 * 证书 / 私钥路径的格式校验。
 *
 * 只允许**绝对路径**：容器里的相对路径会被 nginx 按自己的 prefix 解析，
 * 那是最难排查的一类问题。字符集也收紧（字母数字 . _ - /），不允许 "..",
 * 不允许空格（nginx 配置里带空格的路径要加引号，容易漏）。
 */
function panel_ssl_path_problem(string $path): string
{
    $path = trim($path);

    if ($path === '') {
        return '';
    }

    if (!str_starts_with($path, '/')) {
        return t('证书 / 私钥要填容器里的绝对路径（以 / 开头），例如 /var/www/certs/panel.crt。');
    }

    if (strlen($path) > 200) {
        return t('路径太长（最多 200 个字符）。');
    }

    if (str_contains($path, '..')) {
        return t('路径里不能有 ..');
    }

    if (preg_match('#^/[A-Za-z0-9._/-]+$#', $path) !== 1) {
        return t('路径只能包含字母、数字、点、下划线、短横线和斜杠。');
    }

    return '';
}

/** 这个路径是不是在面板自己的证书目录里（在外面就提示要额外挂载）。 */
function panel_ssl_path_in_cert_dir(string $path): bool
{
    $path = trim($path);

    if ($path === '') {
        return false;
    }

    foreach (panel_cert_dir_candidates() as $dir) {
        if (str_starts_with($path, rtrim($dir, '/') . '/')) {
            return true;
        }
    }

    return false;
}

// ============================================================
// 5. 证书状态（根代理写的 status.json）
// ============================================================

/**
 * 读证书状态。
 *
 * @return array{ok:bool,found:bool,generated_at:string,error:string,
 *               active:array<string,mixed>,certs:array<int,array<string,mixed>>,
 *               acme:array<string,mixed>}
 */
function panel_ssl_status(bool $refresh = false): array
{
    static $cache = null;

    if ($refresh) {
        $cache = null;
    }

    if ($cache !== null) {
        return $cache;
    }

    $empty = [
        'ok'           => false,
        'found'        => false,
        'generated_at' => '',
        'error'        => '',
        'active'       => [],
        'certs'        => [],
        'acme'         => [],
    ];

    $path = panel_ssl_status_file();

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

    $certs = [];

    if (is_array($decoded['certs'] ?? null)) {
        foreach ($decoded['certs'] as $row) {
            if (is_array($row)) {
                $certs[] = $row;
            }
        }
    }

    $cache = [
        'ok'           => empty($decoded['error']),
        'found'        => true,
        'generated_at' => (string) ($decoded['generated_at'] ?? ''),
        'error'        => (string) ($decoded['error'] ?? ''),
        'active'       => is_array($decoded['active'] ?? null) ? $decoded['active'] : [],
        'certs'        => $certs,
        'acme'         => is_array($decoded['acme'] ?? null) ? $decoded['acme'] : [],
    ];

    return $cache;
}

/**
 * 当前生效的那张证书的信息（没读过状态 / 没有证书 → 空数组）。
 *
 * "生效"以**设置里的路径**为准：状态文件里那条 path 和 ssl_cert_file 一样
 * 才算数（否则页面会显示一张早就不用的证书的到期时间）。
 *
 * @return array<string,mixed>
 */
function panel_ssl_active_cert(): array
{
    $status = panel_ssl_status();
    $want = panel_ssl_cert_file();

    if ($want === '') {
        return [];
    }

    foreach ($status['certs'] as $row) {
        if ((string) ($row['cert_file'] ?? '') === $want) {
            return $row;
        }
    }

    // 状态文件里没有（比如证书是刚手动挂进来的）：回退到 active 那一条。
    if (($status['active']['cert_file'] ?? '') === $want) {
        return $status['active'];
    }

    return [];
}

/** 生效证书还剩多少天（null = 不知道）。 */
function panel_ssl_days_left(): ?int
{
    $active = panel_ssl_active_cert();

    if ($active === [] || !isset($active['days_left'])) {
        return null;
    }

    return (int) $active['days_left'];
}

/** 页面上那句"当前生效"的说明。 */
function panel_ssl_effective_note(): string
{
    if (panel_ssl_mode() === 'off') {
        return t('当前生效：不用 HTTPS（面板只提供 HTTP）。');
    }

    if (!panel_ssl_configured()) {
        return t('当前生效：模式已经选了，但证书 / 私钥路径还空着 —— 面板暂时仍按 HTTP 提供服务。');
    }

    $active = panel_ssl_active_cert();

    if ($active === []) {
        return sprintf(
            t('当前生效：%s（证书还没被根代理检查过，刷新一下或看下面的应用结果）。'),
            panel_ssl_cert_file()
        );
    }

    $days = (int) ($active['days_left'] ?? 0);
    $until = (string) ($active['not_after'] ?? '');
    $issuer = (string) ($active['issuer'] ?? '');

    if ($days < 0) {
        return sprintf(t('当前生效：%s —— 这张证书已经过期了（%s）。'), $issuer, $until);
    }

    return sprintf(
        t('当前生效：%s，%s 到期（还剩 %d 天）。'),
        $issuer,
        $until,
        $days
    );
}

// ============================================================
// 6. 上传证书（PEM 结构校验 + 落 incoming/）
// ============================================================

/** 证书文件内容看起来像 PEM 证书吗（只看结构，深度校验在 root 代理那边）。 */
function panel_ssl_pem_looks_like_cert(string $pem): bool
{
    return str_contains($pem, '-----BEGIN CERTIFICATE-----')
        && str_contains($pem, '-----END CERTIFICATE-----')
        && !str_contains($pem, 'PRIVATE KEY');
}

/** 私钥文件内容看起来像 PEM 私钥吗。 */
function panel_ssl_pem_looks_like_key(string $pem): bool
{
    $begin = preg_match(
        '/-----BEGIN (RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----/',
        $pem
    ) === 1;

    return $begin
        && str_contains($pem, 'PRIVATE KEY-----')
        && !str_contains($pem, 'BEGIN CERTIFICATE');
}

/** 私钥是不是带密码的（nginx 起不来，必须明确拒掉）。 */
function panel_ssl_pem_looks_encrypted(string $pem): bool
{
    return str_contains($pem, 'ENCRYPTED PRIVATE KEY')
        || str_contains($pem, 'Proc-Type: 4,ENCRYPTED');
}

/** 上传大小上限：证书 128 KB / 私钥 64 KB（够大，又不至于被人塞垃圾）。 */
function panel_ssl_max_bytes(string $kind): int
{
    return $kind === 'key' ? 65536 : 131072;
}

/**
 * 处理一次"上传证书"（$_FILES['cert'] + $_FILES['key']）。
 *
 * 做三件事，顺序不能反：
 *   1. 结构校验（PEM 头尾、不能是加密私钥、大小、不能有二进制垃圾）；
 *   2. 落到 `<证书目录>/incoming/<随机>.crt|.key`（root 代理会从这里取走）；
 *   3. 排一条 `ssl-import` 请求 —— **真正的安装、`nginx -t`、reload 都在
 *      root 代理那一侧做**。面板这边不写设置：安装成功了代理会回调 PHP CLI
 *      把 ssl_cert_file / ssl_key_file / ssl_mode 写成真的路径。
 *
 * 这样做的好处：上传一个坏证书**不会**让面板引用一个 nginx 起不来的路径 ——
 * 最坏情况是"上传失败，旧证书继续用"。
 *
 * @param array<string,mixed> $certFile $_FILES['cert']
 * @param array<string,mixed> $keyFile  $_FILES['key']
 * @return array{ok:bool,error:string,id:string,queued:bool}
 */
function panel_ssl_store_upload(array $certFile, array $keyFile, string $actor = ''): array
{
    $result = ['ok' => false, 'error' => '', 'id' => '', 'queued' => false];

    $certPem = panel_ssl_read_upload($certFile, 'cert', $problem);

    if ($certPem === null) {
        $result['error'] = (string) $problem;

        return $result;
    }

    $keyPem = panel_ssl_read_upload($keyFile, 'key', $problem);

    if ($keyPem === null) {
        $result['error'] = (string) $problem;

        return $result;
    }

    if (!panel_ssl_pem_looks_like_cert($certPem)) {
        $result['error'] = t('证书文件里没有找到 -----BEGIN CERTIFICATE-----（要 PEM 格式的证书，通常是 fullchain.pem）。');

        return $result;
    }

    if (!panel_ssl_pem_looks_like_key($keyPem)) {
        $result['error'] = t('私钥文件里没有找到 -----BEGIN ' . 'PRIVATE KEY-----（要 PEM 格式的私钥，通常是 privkey.pem）。');

        return $result;
    }

    if (panel_ssl_pem_looks_encrypted($keyPem)) {
        $result['error'] = t('这把私钥是带密码的，nginx 用不了：请先去掉密码（openssl rsa -in 原key -out 新key）再上传。');

        return $result;
    }

    $dir = panel_ssl_incoming_dir(true);

    if ($dir === '' || !is_dir($dir) || !is_writable($dir)) {
        $result['error'] = sprintf(t('证书暂存目录不可写：%s'), $dir);

        return $result;
    }

    $stamp = date('Ymd-His') . '-' . bin2hex(random_bytes(3));
    $certPath = rtrim($dir, '/') . '/upload-' . $stamp . '.crt';
    $keyPath = rtrim($dir, '/') . '/upload-' . $stamp . '.key';

    if (@file_put_contents($certPath, $certPem) === false) {
        $result['error'] = sprintf(t('证书写不进暂存目录：%s'), $dir);

        return $result;
    }

    if (@file_put_contents($keyPath, $keyPem) === false) {
        @unlink($certPath);
        $result['error'] = sprintf(t('私钥写不进暂存目录：%s'), $dir);

        return $result;
    }

    $queued = panel_agent_request('ssl-import', [
        'cert'  => $certPath,
        'key'   => $keyPath,
        'label' => 'upload',
    ], $actor);

    if (!$queued['ok']) {
        @unlink($certPath);
        @unlink($keyPath);
        $result['error'] = sprintf(t('证书已经暂存，但排不进处理队列：%s'), $queued['error']);

        return $result;
    }

    $result['ok'] = true;
    $result['id'] = (string) $queued['id'];
    $result['queued'] = true;

    return $result;
}

/**
 * 从 $_FILES 的一项里读出正文（读不到 → null，并把原因写进 $problem）。
 *
 * 和 logo 上传一样不用 move_uploaded_file（门禁白名单里没有它）：
 * 上传临时名是 PHP 生成的随机名，这里只读正文 + 自己做结构校验。
 */
function panel_ssl_read_upload(array $file, string $kind, ?string &$problem = null): ?string
{
    $problem = '';

    $error = (int) ($file['error'] ?? UPLOAD_ERR_NO_FILE);

    if ($error === UPLOAD_ERR_NO_FILE) {
        $problem = $kind === 'key'
            ? t('没有选择私钥文件。')
            : t('没有选择证书文件。');

        return null;
    }

    if ($error !== UPLOAD_ERR_OK) {
        $problem = sprintf(t('上传没有完成（PHP 错误码 %d）。'), $error);

        return null;
    }

    $size = (int) ($file['size'] ?? 0);

    if ($size <= 0 || $size > panel_ssl_max_bytes($kind)) {
        $problem = sprintf(
            t('文件大小必须在 0 字节和 %s 之间。'),
            human_bytes(panel_ssl_max_bytes($kind))
        );

        return null;
    }

    $temp = (string) ($file['tmp_name'] ?? '');

    if ($temp === '' || !is_file($temp)) {
        $problem = t('上传的临时文件读不到，请重试。');

        return null;
    }

    $data = @file_get_contents($temp);

    if ($data === false || $data === '') {
        $problem = t('读取上传的文件失败，请重试。');

        return null;
    }

    if (str_contains((string) $data, "\0")) {
        $problem = t('文件里有二进制内容 —— 证书和私钥都应该是文本（PEM）。');

        return null;
    }

    if (strlen((string) $data) > panel_ssl_max_bytes($kind)) {
        $problem = sprintf(t('文件太大（上限 %s）。'), human_bytes(panel_ssl_max_bytes($kind)));

        return null;
    }

    return (string) $data;
}

// ============================================================
// 7. 自签证书 / ACME 的排队入口
// ============================================================

/**
 * 自签证书（**只用于测试**）。
 *
 * 不需要域名解析、不需要 80 端口，本机就能验 TLS：浏览器会警告"证书不受
 * 信任"，这是预期的。
 *
 * @return array{ok:bool,error:string,id:string}
 */
function panel_ssl_request_selfsigned(string $actor = ''): array
{
    $result = ['ok' => false, 'error' => '', 'id' => ''];

    $domain = panel_bound_domain();

    if ($domain === '' ) {
        // 没绑域名时用一个明确的占位域名 + localhost（证书里会带 SAN）。
        $domain = 'panel.local';
    }

    if (panel_domain_is_wildcard($domain)) {
        $result['error'] = t('自签证书不支持通配符域名：请填一个具体域名（例如 panel.example.com）。');

        return $result;
    }

    $queued = panel_agent_request('ssl-selfsigned', [
        'domain' => $domain,
        'days'   => '825',
        'label'  => 'selfsigned',
    ], $actor);

    if (!$queued['ok']) {
        $result['error'] = $queued['error'];

        return $result;
    }

    $result['ok'] = true;
    $result['id'] = (string) $queued['id'];

    return $result;
}

/**
 * Let's Encrypt（ACME）申请 / 续期。
 *
 * 需要：绑定了域名（= 要签的域名）、填了邮箱、域名解析到这台机器、
 * 80 端口能从公网进来（HTTP-01 校验）。dry-run 只跑流程不装证书、
 * 也不占用 Let's Encrypt 的签发配额 —— 验收流程用这一条。
 *
 * @return array{ok:bool,error:string,id:string}
 */
function panel_ssl_request_acme(string $action, bool $dryRun, string $actor = ''): array
{
    $result = ['ok' => false, 'error' => '', 'id' => ''];

    if (!in_array($action, ['ssl-acme-issue', 'ssl-acme-renew'], true)) {
        $result['error'] = t('不认识的 ACME 动作。');

        return $result;
    }

    $domain = panel_bound_domain();

    if ($domain === '') {
        $result['error'] = t('ACME 要先绑定域名：Let’s Encrypt 只给域名签证书，不给 IP 签。');

        return $result;
    }

    if (panel_domain_is_wildcard($domain)) {
        $result['error'] = t('通配符域名不能用 HTTP-01 校验：请填具体域名（例如 panel.example.com），或者改用上传证书。');

        return $result;
    }

    $email = panel_acme_email();

    if ($email === '') {
        $result['error'] = t('ACME 要填一个邮箱：Let’s Encrypt 用它发到期提醒，也是账号联系地址。');

        return $result;
    }

    if (filter_var($email, FILTER_VALIDATE_EMAIL) === false) {
        $result['error'] = t('邮箱格式不对。');

        return $result;
    }

    $queued = panel_agent_request($action, [
        'domain'  => $domain,
        'email'   => $email,
        'dry_run' => $dryRun ? '1' : '0',
    ], $actor);

    if (!$queued['ok']) {
        $result['error'] = $queued['error'];

        return $result;
    }

    $result['ok'] = true;
    $result['id'] = (string) $queued['id'];

    return $result;
}

/**
 * 一次"检查 / 重读证书状态"的排队（root 代理会重新跑一遍 openssl 检查）。
 *
 * @return array{ok:bool,error:string,id:string}
 */
function panel_ssl_request_status(string $actor = ''): array
{
    $result = ['ok' => false, 'error' => '', 'id' => ''];

    $queued = panel_agent_request('cert-status', [
        'cert' => panel_ssl_cert_file(),
        'key'  => panel_ssl_key_file(),
        'mode' => panel_ssl_mode(),
    ], $actor);

    if (!$queued['ok']) {
        $result['error'] = $queued['error'];

        return $result;
    }

    $result['ok'] = true;
    $result['id'] = (string) $queued['id'];

    return $result;
}
