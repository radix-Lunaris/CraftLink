<?php
/**
 * CraftLink Admin Panel -- shared library.
 *
 * Deliberately dependency free (no composer): the panel is small, and a
 * zero-dependency container is easier to keep alive on a 2 GB VPS.
 *
 * Security notes:
 *   * db() uses prepared statements only.
 *   * relay_api() authenticates with a bearer token and never exposes it to
 *     the browser.
 *   * All output goes through h() (htmlspecialchars).
 *   * Every state changing form carries a CSRF token.
 */

declare(strict_types=1);

// 语言与主题（面板内的中英文切换、亮色/暗色切换）。
//
// 单独一个文件，因为 t() 和那张翻译表跟数据库、会话都没关系，放在这里只会
// 让这个本来就很长的共用库更难读。它是纯函数式的：不连库、不读文件，
// 只读 cookie 和一个静态数组 —— 所以任何地方调用都安全。
require_once __DIR__ . '/i18n.php';

// ============================================================
// Constants
// ============================================================

/**
 * How often the page polls web/live.php for fresh numbers, in seconds.
 *
 * 3 seconds because the poll is a small JSON reply, not a page render: it
 * replaces a handful of values in place, so there is no scroll jump and no
 * lost form input. The old full-page reload had to be slow to be tolerable.
 */
const AUTO_REFRESH_SECONDS = 3;

/**
 * Tell the browser never to cache a panel response.
 *
 * This is not a nicety, it is a correctness requirement: the panel is updated
 * by uploading PHP files, and PHP responses carry no useful cache validator,
 * so a browser is free to reuse an old page. That produced a genuinely
 * baffling bug report -- the server had stopped reloading the page, but the
 * user's browser was still running the OLD markup, so it kept reloading every
 * ten seconds and there was nothing in the source to explain it.
 *
 * no-store also covers the login page, which must never be served from cache
 * after a logout.
 */
function no_cache_headers(): void
{
    if (headers_sent()) {
        return;
    }

    header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
    header('Pragma: no-cache');
    header('Expires: 0');
}

// ============================================================
// Configuration
// ============================================================

function deployment_hint(string $text): string
{
    $home = getenv('MCLINK_HOME') ?: '/opt/mc-link';
    $relay = getenv('MCLINK_RELAY_CONTAINER') ?: 'mc-relay';
    $url = getenv('MCLINK_PANEL_LOCAL_URL') ?: 'http://127.0.0.1:19102';
    return strtr($text, ['/opt/mc-link' => escapeshellarg($home), 'mc-relay' => escapeshellarg($relay), 'http://127.0.0.1:19102' => $url]);
}

function config(?string $key = null)
{
    static $config = null;

    if ($config === null) {
        // config.php is bind-mounted from the host so the image never contains
        // a secret.
        //
        // [!] 不要用单一路径 + ".."：
        //    `__DIR__ . '/../config.php'` 在 php-cli 下能解析，在 php-fpm 下
        //    realpath() 返回 false（/var/www 是 overlayfs 挂载点），于是出现
        //    "命令行能读、网页 500"的诡异现象。
        //    这里改成候选路径列表，任何一个命中即可，彻底不依赖 ".." 解析。
        $here = __DIR__;

        $normalised = realpath($here);
        if ($normalised === false) {
            $normalised = $here;
        }

        $candidates = [
            $normalised . '/config.php',   // 正常：/var/www/lib/config.php
            $here . '/config.php',         // realpath 失败时的退路
        ];

        $parent = realpath($here . '/..');
        if ($parent !== false) {
            $candidates[] = $parent . '/config.php';
        }

        $path = null;

        foreach ($candidates as $candidate) {
            if (is_file($candidate)) {
                $path = $candidate;
                break;
            }
        }

        if ($path === null) {
            // config.php is OPTIONAL: docker-compose supplies every credential
            // through environment variables, so a fresh deployment works even
            // before the file exists. Only metadata (title, session secret,
            // ICP) falls back to the defaults below.
            $config = [
                'db' => [
                    'host' => 'web-mysql80',
                    'port' => 3306,
                    'database' => 'mclink',
                    'username' => 'mclink',
                    'password' => '',
                    'charset' => 'utf8mb4',
                ],
                'relay' => [
                    'base_url' => 'http://mc-relay:19101',
                    'token' => '',
                    'timeout' => 6,
                ],
                'panel' => [
                    'session_secret' => 'mclink-panel-default-secret',
                    'title' => 'CraftLink 管理面板',
                    'icp' => '',
                    'copyright' => '',
                    'allow_self_register' => false,
                    'debug' => false,
                ],
            ];

            $path = null;

            // fall through to the environment overrides below
            $skipFileLoad = true;
        } else {
            $skipFileLoad = false;
        }

        if (!$skipFileLoad) {
            $loaded = require $path;

            if (!is_array($loaded)) {
                http_response_code(500);
                echo t('config.php 格式错误：它必须 return 一个数组。');
                exit;
            }

            $config = $loaded;
        }

        $localPath = $normalised . '/config.local.php';

        if (is_file($localPath)) {
            $local = require $localPath;

            if (is_array($local)) {
                $config = array_replace_recursive($config, $local);
            }
        }

        // ---- 环境变量覆盖（docker-compose 提供） ----
        $envDbPassword = getenv('MCLINK_DB_PASSWORD');
        $envApiToken = getenv('MCLINK_ADMIN_API_TOKEN');
        $envDbHost = getenv('MCLINK_DB_HOST');
        $envDbPort = getenv('MCLINK_DB_PORT');
        $envDbName = getenv('MCLINK_DB_NAME');
        $envDbUser = getenv('MCLINK_DB_USER');
        $envRelayUrl = getenv('MCLINK_RELAY_API_URL');

        if ($envDbPassword !== false && $envDbPassword !== '') {
            $config['db']['password'] = $envDbPassword;
        }

        if ($envDbHost !== false && $envDbHost !== '') {
            $config['db']['host'] = $envDbHost;
        }

        if ($envDbPort !== false && $envDbPort !== '') {
            $port = filter_var($envDbPort, FILTER_VALIDATE_INT, ['options' => ['min_range' => 1, 'max_range' => 65535]]);
            if ($port === false) { throw new RuntimeException('MCLINK_DB_PORT invalid'); }
            $config['db']['port'] = $port;
        }

        if ($envDbName !== false && $envDbName !== '') {
            $config['db']['database'] = $envDbName;
        }

        if ($envDbUser !== false && $envDbUser !== '') {
            $config['db']['username'] = $envDbUser;
        }

        if ($envApiToken !== false && $envApiToken !== '') {
            $config['relay']['token'] = $envApiToken;
        }

        if ($envRelayUrl !== false && $envRelayUrl !== '') {
            $config['relay']['base_url'] = $envRelayUrl;
        }

        $envTitle = getenv('MCLINK_PANEL_TITLE');

        if ($envTitle !== false && $envTitle !== '') {
            $config['panel']['title'] = $envTitle;
        }

        $envCopyright = getenv('MCLINK_PANEL_COPYRIGHT');
        if ($envCopyright !== false && $envCopyright !== '') {
            $config['panel']['copyright'] = $envCopyright;
        }

        $envIcp = getenv('MCLINK_PANEL_ICP');

        if ($envIcp !== false && $envIcp !== '') {
            $config['panel']['icp'] = $envIcp;
        }
    }

    if ($key === null) {
        return $config;
    }

    $parts = explode('.', $key);
    $value = $config;

    foreach ($parts as $part) {
        if (!is_array($value) || !array_key_exists($part, $value)) {
            return null;
        }
        $value = $value[$part];
    }

    return $value;
}

// ============================================================
// Database
// ============================================================

function db(): PDO
{
    static $pdo = null;

    if ($pdo instanceof PDO) {
        return $pdo;
    }

    $cfg = config('db');

    $dsn = sprintf(
        'mysql:host=%s;port=%d;dbname=%s;charset=%s',
        $cfg['host'],
        (int) $cfg['port'],
        $cfg['database'],
        $cfg['charset'] ?? 'utf8mb4'
    );

    $pdo = new PDO($dsn, $cfg['username'], $cfg['password'], [
        PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES   => false,
    ]);

    return $pdo;
}

// ============================================================
// Relay admin API
// ============================================================

/**
 * Call the relay admin API.
 *
 * `$timeout` exists for the ONE call that is slow on purpose: 强制关闭房间.
 * The relay tells the clients and waits for them to leave by themselves before
 * forcing anything, so it can legitimately take several seconds. With the 6 s
 * default the panel would give up first and report a failure for a close that
 * was actually working.
 *
 * @return array{ok:bool,status:int,data:mixed,error:string}
 */
function relay_api(string $method, string $path, ?array $body = null,
                   ?int $timeout = null): array
{
    $base  = rtrim((string) config('relay.base_url'), '/');
    $token = (string) config('relay.token');
    $url   = $base . '/' . ltrim($path, '/');

    $headers = [
        'Authorization: Bearer ' . $token,
        'Accept: application/json',
    ];

    $payload = null;

    if ($body !== null) {
        $payload = json_encode($body, JSON_UNESCAPED_UNICODE);
        $headers[] = 'Content-Type: application/json';
    }

    $ch = curl_init($url);

    curl_setopt_array($ch, [
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_TIMEOUT        => $timeout
            ?? (int) (config('relay.timeout') ?? 6),
        CURLOPT_CONNECTTIMEOUT => 4,
    ]);

    if ($payload !== null) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, $payload);
    }

    $raw    = curl_exec($ch);
    $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err    = curl_error($ch);

    curl_close($ch);

    if ($raw === false || $err !== '') {
        return [
            'ok'     => false,
            'status' => 0,
            'data'   => null,
            'error'  => sprintf(
                t('无法连接中继服务：%s'),
                $err !== '' ? $err : t('未知错误')
            ),
        ];
    }

    $decoded = json_decode((string) $raw, true);

    if (!is_array($decoded)) {
        return [
            'ok'     => false,
            'status' => $status,
            'data'   => null,
            'error'  => t('中继服务返回了无法解析的内容'),
        ];
    }

    // [!] 这里必须原样返回响应体，不能再包一层 'data' => $decoded。
    //
    // 中继的每个响应都是 {"ok": bool, "data": {...}, ...}，调用方读的是
    // $result['data']['xxx']。
    // 之前写成 'data' => $decoded 相当于包了两层，于是 $result['data'] 拿到的是
    // 整个响应体、里面根本没有业务字段：
    //   $settings['data']['require_account'] 永远是 null
    //   -> 仪表盘一直显示"无法从中继读取开关状态：中继未响应"
    // 而 CLI 直连中继是正常的，所以问题看起来像环境故障，实际是这个函数。
    return [
        'ok'     => $status >= 200 && $status < 300 && ($decoded['ok'] ?? false),
        'status' => $status,
        'data'   => $decoded['data'] ?? null,
        'error'  => (string) ($decoded['error'] ?? $decoded['message'] ?? ''),
        'raw'    => $decoded,
    ];
}

/**
 * Call the relay admin API and return the RAW response body.
 *
 * 为什么不能直接用 relay_api()：那个函数会把响应体 json_decode() 之后只留
 * `data` 字段，遇到非 JSON 的正文还会回一句「中继服务返回了无法解析的内容」。
 * 而「下载服务端日志」要的正好是**没被解析过的原文**（几 MB 的纯文本日志），
 * 解析一次不但白费内存，还会把一次成功的下载判成失败。所以这里另开一个兄弟
 * 函数，relay_api() 的契约一个字不动。
 *
 * 用法和 relay_api() 一样（方法 + 路径 + 超时），但它不发 JSON 正文。
 *
 * @return array{ok:bool,status:int,body:string,content_type:string,
 *               content_disposition:string,error:string}
 */
function relay_api_raw(string $method, string $path, ?int $timeout = null): array
{
    $base  = rtrim((string) config('relay.base_url'), '/');
    $token = (string) config('relay.token');
    $url   = $base . '/' . ltrim($path, '/');

    // 响应头单独收集：文件名在 Content-Disposition 里，curl_getinfo() 只认得
    // Content-Type。CURLOPT_HEADERFUNCTION 每收到一行调一次，返回读到的字节数
    // 是约定（返回别的值 curl 会当成写失败）。
    $headers = [];

    $ch = curl_init($url);

    curl_setopt_array($ch, [
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => [
            'Authorization: Bearer ' . $token,
            'Accept: text/plain, */*',
        ],
        CURLOPT_TIMEOUT        => $timeout
            ?? (int) (config('relay.timeout') ?? 6),
        CURLOPT_CONNECTTIMEOUT => 4,
        CURLOPT_HEADERFUNCTION => static function ($handle, string $line) use (&$headers): int {
            $parts = explode(':', $line, 2);

            if (count($parts) === 2) {
                $headers[strtolower(trim($parts[0]))] = trim($parts[1]);
            }

            return strlen($line);
        },
    ]);

    $body   = curl_exec($ch);
    $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $type   = (string) curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
    $err    = curl_error($ch);

    curl_close($ch);

    if ($body === false || $err !== '') {
        return [
            'ok'                  => false,
            'status'              => 0,
            'body'                => '',
            'content_type'        => '',
            'content_disposition' => '',
            'error'               => sprintf(
                t('无法连接中继服务：%s'),
                $err !== '' ? $err : t('未知错误')
            ),
        ];
    }

    $body = (string) $body;
    $message = '';

    if ($status < 200 || $status >= 300) {
        // 失败时中继回的是一小段 JSON（{"ok":false,"error":"..."}）。把小响应
        // 解析出来能让面板显示中继自己的说法；只有小响应才解析 —— 报错时没
        // 必要把几 MB 的正文再解析一遍。
        if (strlen($body) <= 65536) {
            $decoded = json_decode($body, true);

            if (is_array($decoded)) {
                $message = (string) ($decoded['error'] ?? $decoded['message'] ?? '');
            }
        }
    }

    return [
        'ok'                  => $status >= 200 && $status < 300,
        'status'              => $status,
        'body'                => $body,
        'content_type'        => $type,
        'content_disposition' => (string) ($headers['content-disposition'] ?? ''),
        'error'               => $message,
    ];
}

/**
 * Read a message out of a relay API reply, wherever it happens to sit.
 *
 * The relay is deliberately inconsistent here and that is awkward to fix on
 * the wire:
 *
 *   GET  /settings   -> {"ok":true,"data":{...}}                  data only
 *   PUT  /settings   -> {"ok":true,"message":"...","data":{...}}  both
 *   POST /users/.../rename
 *                    -> {"ok":true,"message":"...","data":{...}}  both
 *   POST /rooms/purge-stale
 *                    -> {"ok":true,"message":"..."}               message only
 *
 * Reading the wrong one silently falls back to a generic string, which looks
 * like "the operation worked but the panel lost the detail". This helper
 * checks every place a message can live, so a caller never has to care.
 *
 * Returns "" when the relay sent no message at all.
 */
function relay_message(array $result): string
{
    foreach ([
        $result['raw']['message'] ?? null,
        $result['raw']['data']['message'] ?? null,
        $result['data']['message'] ?? null,
        $result['message'] ?? null,
    ] as $candidate) {
        if (is_string($candidate) && $candidate !== '') {
            return $candidate;
        }
    }

    return '';
}

// ============================================================
// Session / auth
// ============================================================

function start_session(): void
{
    if (session_status() === PHP_SESSION_ACTIVE) {
        return;
    }

    session_name('mclink_panel');
    session_set_cookie_params([
        'httponly' => true,
        'samesite' => 'Lax',
        'secure'   => !empty($_SERVER['HTTPS']),
    ]);

    session_start();
}

function current_user(): ?array
{
    start_session();

    return $_SESSION['user'] ?? null;
}

// ============================================================
// Roles (three tiers)
// ============================================================
//
//   root    根管理员   全服务器唯一。不可降级、不可转让、不可另设。
//                     面板里连它自己的密码都改不了 —— 那是刻意的，
//                     所以才有 SSH 找回命令（server/reset_password.py）。
//   admin   管理员     踢人、改账号、看房间密码，但完全碰不到 root
//   player  玩家       只读，且看不到房间密码
//
// The colours are the SAME three the client uses, so a person is the same
// colour wherever you look at them.

const ROLE_ROOT = 'root';
const ROLE_ADMIN = 'admin';
const ROLE_PLAYER = 'player';

function role_colors(): array
{
    return [
        ROLE_ROOT => '#ff9f43',    // 橙黄
        ROLE_ADMIN => '#3ddc84',   // 绿色
        ROLE_PLAYER => '#4c8dff',  // 蓝色
    ];
}

function role_labels(): array
{
    // 这三个词是面板自己写的，所以跟着面板语言走；角色代号（root/admin/player）
    // 是中继和数据库里的值，永远不翻译。
    return [
        ROLE_ROOT => t('根管理员'),
        ROLE_ADMIN => t('管理员'),
        ROLE_PLAYER => t('玩家'),
    ];
}

function normalize_role(?string $role, $isAdmin = 0): string
{
    $role = strtolower(trim((string) $role));

    if (in_array($role, [ROLE_ROOT, ROLE_ADMIN, ROLE_PLAYER], true)) {
        return $role;
    }

    // Older session or an un-migrated database: fall back to the 0/1 column.
    return ((int) $isAdmin) === 1 ? ROLE_ADMIN : ROLE_PLAYER;
}

/** The role of the signed-in account. */
function current_role(): string
{
    $user = current_user();

    if ($user === null) {
        return ROLE_PLAYER;
    }

    return normalize_role($user['role'] ?? null, $user['is_admin'] ?? 0);
}

function current_role_label(): string
{
    return role_labels()[current_role()] ?? t('玩家');
}

function current_role_color(): string
{
    return role_colors()[current_role()] ?? '#4c8dff';
}

function is_root(): bool
{
    return current_role() === ROLE_ROOT;
}

/** Whether a role is staff (sees the write buttons at all). */
function is_role_staff(string $role): bool
{
    return $role === ROLE_ROOT || $role === ROLE_ADMIN;
}

/**
 * Whether the signed-in account may act on `$targetRole`.
 *
 * The rules, in one place so the header, the buttons and the backend can never
 * disagree:
 *
 *   root   may act on admin and player. NOT on root (itself included).
 *   admin  may act on admin and player. NOT on root.
 *   player may not act on anybody.
 */
function can_act_on(string $targetRole): bool
{
    $mine = current_role();

    if ($targetRole === ROLE_ROOT) {
        // Nobody touches root through the panel, including root.
        return false;
    }

    return $mine === ROLE_ROOT || $mine === ROLE_ADMIN;
}

function is_admin(): bool
{
    $role = current_role();

    return $role === ROLE_ROOT || $role === ROLE_ADMIN;
}

/**
 * 密码规则（与中继 gatekeeper.password_problem 完全一致）：
 * 6-20 位，只能字母和数字。
 *
 * 返回 "" 表示合格，否则是给人看的原因。
 *
 * WHY THIS IS IN ONE PLACE: the rule used to be spelled out separately in the
 * create form, the reset form and the self-service page, and one of them only
 * checked the length -- so a password containing "_" was accepted there
 * (it succeeded for a real admin user) while the others would have refused it.
 */
function password_problem(?string $password): string
{
    $text = (string) $password;

    if (strlen($text) < 6) {
        return t('密码至少 6 位');
    }

    if (strlen($text) > 20) {
        return t('密码最长 20 位');
    }

    // 只允许 ASCII 字母和数字：ctype_* 依赖扩展，这里用正则更稳。
    if (preg_match('/^[A-Za-z0-9]+$/', $text) !== 1) {
        return t('密码只能使用字母和数字（不能有符号、空格或中文）');
    }

    return '';
}

/**
 * 面板被挂在哪个路径下（反代前缀），返回以 / 结尾的基址。
 *
 * 这是**服务端**用的路径，永远只有路径、没有主机和端口。现在只剩一个调用点：
 * cookie 的 path（见 i18n.php 的 panel_remember）。**跳转和页面里的地址都不
 * 再用它** —— 反代如果配成 `proxy_pass http://上游/;`（带结尾斜杠），nginx 会把
 * 前缀从 URI 上剥掉，这里算出来就是 "/"：
 *
 *   * 用它拼 Location -> 退出登录后跳到 https://域名/index.php?...，404；
 *   * 用它当 <base>  -> 样式表请求到 https://域名/assets/style.css，一页裸 HTML；
 *
 * 浏览器侧的地址请用 panel_base_href()（相对于当前文档）和 redirect() 的相对
 * Location —— 只有浏览器的地址栏同时知道端口和前缀。
 *
 * REQUEST_URI 保留反代交过来的前缀（/panel-example/index.php），而
 * SCRIPT_NAME 不保留，所以算的时候只能用它。
 */
function panel_base_url(): string
{
    $uri  = (string) ($_SERVER['REQUEST_URI'] ?? '');
    $path = (string) (parse_url($uri, PHP_URL_PATH) ?: '');

    if ($path === '') {
        $path = (string) ($_SERVER['SCRIPT_NAME'] ?? '/');
    }

    $dir = rtrim(str_replace('\\', '/', dirname($path)), '/');

    return $dir === '' ? '/' : $dir . '/';
}

/**
 * <base href> 的值：**相对于当前文档**的 "./"。
 *
 * WHY "./" AND NOT panel_base_url()
 *
 * 页面里的地址（assets/style.css、index.php?action=...）都是相对地址，浏览器
 * 需要一个基准来解析它们。这个基准必须是"浏览器地址栏里那个地址"，而不是
 * 服务端从 REQUEST_URI 猜出来的路径：
 *
 *   * 端口：地址栏是 https://域名:54865/panel-example/，那么样式表就该从
 *     54865 取。任何"域名根 + 前缀"的绝对写法都会把端口丢掉，请求跑到 443
 *     上另一个站点（比如刚建的静态站）去，结果是 404 + 一页没有 CSS 的裸 HTML。
 *   * 前缀：1Panel 这类面板的 proxy_pass 如果写成 `http://127.0.0.1:19102/`
 *     （带结尾斜杠），nginx 会把 /panel-example/ 从 URI 上剥掉，面板根本
 *     看不到前缀 —— 服务端推导必然推错，而浏览器的地址栏里它一直都在。
 *
 * "./" 让浏览器用**当前文档地址所在目录**当基准，于是：
 *   直连 http://主机:19102/index.php        -> /assets/style.css
 *   反代 https://域名:54865/秘密路径/        -> /秘密路径/assets/style.css
 *   以后换端口 / 换前缀 / 换成子目录         -> 自动跟着走
 *
 * 前提是文档地址本身以 / 结尾（或者以 index.php 这类文件结尾），
 * enforce_trailing_slash() 保证了这一点。
 */
function panel_base_href(): string
{
    return './';
}

/**
 * 把"没有结尾斜杠"的访问跳到带斜杠的形式。
 *
 * 这一步不能指望 nginx：用户可能用 1Panel 的界面配的反代，location 留空、
 * 前缀写成 /xxx，此时 /xxx（不带斜杠）也会命中并把请求交给面板。
 * 面板在这里自己纠正一次，页面里所有相对地址就都对了。
 *
 * 只跳"看起来像目录"的请求：带扩展名的一律不动（否则会跳坏 index.php）。
 */
function enforce_trailing_slash(): void
{
    $uri = (string) ($_SERVER['REQUEST_URI'] ?? '');

    if ($uri === '' || $uri === '/') {
        return;
    }

    $path = (string) (parse_url($uri, PHP_URL_PATH) ?: '');

    if ($path === '' || substr($path, -1) === '/') {
        return;
    }

    $last = basename($path);

    // 有扩展名 = 是一个文件（index.php / style.css / live.php ...），别动。
    if (strpos($last, '.') !== false) {
        return;
    }

    $query = (string) (parse_url($uri, PHP_URL_QUERY) ?: '');
    $target = $path . '/' . ($query !== '' ? '?' . $query : '');

    header('Location: ' . $target, true, 301);
    exit;
}

/**
 * What the PANEL needs the relay to support, with a human explanation.
 *
 * The panel and the relay are rebuilt separately, so a panel can be newer than
 * the relay it talks to -- and then features silently do nothing: 房间人数上限
 * answers 未知的设置项, 强制关闭 answers 房间不存在, a created administrator
 * turns up as 玩家. Every one of those was diagnosed by hand, slowly, more than
 * once.
 *
 * The relay reports which features it has (relay.features in /stats); the
 * panel diffs that against this list and SHOWS the missing ones with the
 * command that fixes them.
 */
function relay_required_features(): array
{
    return [
        'max_players_setting' => t('面板修改房间人数上限'),
        'room_registry'       => t('面板强制关闭房间 / 房间不存在误报'),
        'owner_delete'        => t('房间所有者删除自己的房间'),
        'presence'            => t('在线状态（关掉软件就离线）'),
        'role_create'         => t('创建管理员账号（而不是普通玩家）'),
        'role_manage'         => t('提升 / 降级已有账号'),
        'self_service_limit'  => t('每月 3 次自助修改次数'),
        'relay_build_info'    => t('中继版本信息（本提示本身）'),
    ];
}

/**
 * Check the relay's reported features against what the panel needs.
 *
 * Returns ['ok' => bool, 'missing' => [feature => 说明], 'build' => string].
 * Never throws: a panel that cannot reach the relay must still render.
 */
function relay_feature_check(): array
{
    static $cached = null;

    if ($cached !== null) {
        return $cached;
    }

    $stats = relay_api('GET', '/stats');
    $data = is_array($stats['data'] ?? null) ? $stats['data'] : [];
    $relay = is_array($data['relay'] ?? null) ? $data['relay'] : [];
    $features = is_array($relay['features'] ?? null) ? $relay['features'] : [];

    $missing = [];

    foreach (relay_required_features() as $name => $label) {
        if (!in_array($name, $features, true)) {
            $missing[$name] = $label;
        }
    }

    $cached = [
        'ok' => $missing === [],
        'missing' => $missing,
        'build' => (string) ($relay['build'] ?? ''),
        'reachable' => $stats['ok'] || $features !== [],
    ];

    return $cached;
}

/**
 * A cache-busting version for a static asset: "assets/style.css?v=1757830000".
 * WHY THIS IS NOT OPTIONAL
 *
 * The stylesheet was the one file the browser was allowed to keep, and it did:
 * the panel HTML arrived fresh while the OLD style.css stayed in the cache, so
 * the account chip and the role badges rendered with no background at all --
 * transparent boxes on a dark bar, which looked exactly like a broken feature.
 * The PHP had been fixed and uploaded; the browser simply never asked for it.
 *
 * The timestamp changes whenever the file does, so the URL changes with it and
 * the cache can never serve yesterday's CSS.
 */
function asset_url(string $relative): string
{
    // 一律当相对地址：调用方即使写成 "/assets/style.css"，这里也把它改成
    // "assets/style.css"。前导斜杠在反代子路径下是**错的** —— 浏览器会以
    // 站点根目录为基准（丢掉 /秘密路径/，也丢掉端口），这正是"面板没有 CSS"
    // 那类事故的写法。配合 <base href="./">（panel_base_href()），相对地址
    // 永远落在浏览器地址栏那个目录下。
    $relative = ltrim($relative, '/');

    $path = __DIR__ . '/../web/' . $relative;

    $version = @filemtime($path) ?: time();

    return h($relative) . '?v=' . $version;
}

/**
 * 头像图片的地址；图片不在时返回空串。
 *
 * 登录页和顶栏都用 assets/avatar.png（256×256、透明底的 Minecraft 风格头像）。
 * 它是一张静态图片，可能还没生成、或者上传时漏了 —— 那种时候 <img> 会显示
 * 一个"破图"图标，比直接不放头像更难看。所以调用方先问这里：拿到空串就退回
 * 原来的字母/方块头像，页面永远不会出现破图。
 *
 * 为什么是两个候选路径：本地仓库里面板网页在 panel/web/，容器里在
 * /var/www/panel（Dockerfile 的 "web" -> /var/www/panel），而这个文件本身
 * 在 panel/lib/ -> /var/www/lib。两个布局的上一级目录名不同，所以都试一遍，
 * 命中哪个都算。
 */
function avatar_url(): string
{
    $candidates = [
        __DIR__ . '/../web/assets/avatar.png',    // 本仓库布局
        __DIR__ . '/../panel/assets/avatar.png',  // 容器布局
    ];

    foreach ($candidates as $candidate) {
        if (file_exists($candidate)) {
            return asset_url('assets/avatar.png');
        }
    }

    return '';
}

/**
 * Turn "#3ddc84" into "rgba(61,220,132,0.16)".
 *
 * The role colours are used as translucent tints (pill background, chip
 * background). `color-mix()` does that in one line but needs a recent browser,
 * and the fallback path was a flat grey that looked like a rendering bug --
 * which is exactly what the top-right account chip looked like on a browser
 * without it. Computing the rgba() here works everywhere.
 */
function rgba_from_hex(string $hex, float $alpha): string
{
    $hex = ltrim(trim($hex), '#');

    if (strlen($hex) === 3) {
        $hex = $hex[0] . $hex[0] . $hex[1] . $hex[1] . $hex[2] . $hex[2];
    }

    if (strlen($hex) !== 6 || preg_match('/^[0-9a-fA-F]{6}$/', $hex) !== 1) {
        return 'rgba(76,141,255,' . $alpha . ')';
    }

    $r = hexdec(substr($hex, 0, 2));
    $g = hexdec(substr($hex, 2, 2));
    $b = hexdec(substr($hex, 4, 2));

    return sprintf('rgba(%d,%d,%d,%s)', $r, $g, $b, rtrim(rtrim(sprintf('%.2f', $alpha), '0'), '.'));
}

/**
 * Inline style for a role-coloured element.
 *
 * Everything the element needs is computed here, so the stylesheet does not
 * have to know about colour functions at all.
 */
function role_style(string $role, string $extra = ''): string
{
    $colour = role_colors()[$role] ?? '#4c8dff';

    return sprintf(
        '--role:%s;--role-bg:%s;--role-border:%s;%s',
        h($colour),
        h(rgba_from_hex($colour, 0.16)),
        h(rgba_from_hex($colour, 0.45)),
        $extra
    );
}

/**
 * Render a coloured role chip.
 *
 * `h()` on the label so a corrupted role string cannot inject markup.
 */
function role_badge(?string $role, $isAdmin = 0): string
{
    $normalized = normalize_role($role, $isAdmin);
    $label = role_labels()[$normalized] ?? t('玩家');

    return sprintf(
        '<span class="role-badge" style="%s">%s</span>',
        role_style($normalized),
        h($label)
    );
}

function require_login(): void
{
    if (current_user() === null) {
        redirect('index.php?action=login');
    }
}

function require_admin(): void
{
    require_login();

    if (!is_admin()) {
        http_response_code(403);
        echo t('需要管理员权限。');
        exit;
    }
}

/**
 * Gate every state-changing action.
 *
 * Viewing is open to any logged-in account (players like to see who is
 * online), but anything that WRITES requires an admin. Read-only users get a
 * friendly explanation and a link back instead of a bare 403, because the
 * button they pressed is hidden for them anyway.
 */
function require_operator(): void
{
    require_login();

    if (is_admin()) {
        return;
    }

    http_response_code(403);

    flash(
        t('你的账号是【只读】权限，不能执行修改操作。需要管理权限请联系管理员。'),
        'error'
    );

    redirect('index.php?action=dashboard');
}

/**
 * True when the current account may perform changes.
 * Templates use this to hide/disable buttons for read-only viewers.
 */
function can_operate(): bool
{
    return is_admin();
}

/**
 * Fetch one account row for login, tolerating a database that has not been
 * migrated to the three-tier roles yet.
 *
 * WHY THIS EXISTS: the login query selects `role`. On a database where the
 * relay's migration has not added that column, MySQL throws, PHP turns it into
 * a 500, and the login page shows "服务器无法处理请求" with nothing anywhere
 * explaining why. A missing OPTIONAL column must not be able to lock everybody
 * out of the panel, so the query degrades to the columns that have always
 * existed and the role falls back to is_admin.
 *
 * Returns the row, or false when the account does not exist.
 */
function fetch_login_user(string $username)
{
    $withRole = 'SELECT username, password_hash, is_admin, is_disabled, role
                   FROM users WHERE username = ? LIMIT 1';

    $withoutRole = 'SELECT username, password_hash, is_admin, is_disabled
                      FROM users WHERE username = ? LIMIT 1';

    $statement = db()->prepare($withRole);
    $statement->execute([$username]);
    $row = $statement->fetch();

    if ($row !== false) {
        return $row;
    }

    // false means "no such account" OR "the query itself failed"; only the
    // second case is worth retrying, and the cheapest way to tell them apart
    // is to look at whether the column exists at all.
    if (db_has_column('users', 'role')) {
        return false;
    }

    $statement = db()->prepare($withoutRole);
    $statement->execute([$username]);

    return $statement->fetch();
}

/**
 * Whether a table has a given column. Cached, and never throws.
 *
 * A failure here is treated as "does not have it", which sends the caller down
 * the compatible path rather than into a 500.
 */
function db_has_column(string $table, string $column): bool
{
    static $cache = [];

    $key = $table . '.' . $column;

    if (array_key_exists($key, $cache)) {
        return $cache[$key];
    }

    try {
        $statement = db()->prepare(
            'SELECT COUNT(*) AS n FROM information_schema.columns
              WHERE table_schema = DATABASE()
                AND table_name = ? AND column_name = ?'
        );
        $statement->execute([$table, $column]);

        $row = $statement->fetch();

        $cache[$key] = ((int) ($row['n'] ?? 0)) > 0;
    } catch (Throwable $e) {
        $cache[$key] = false;
    }

    return $cache[$key];
}

/**
 * Verify an account against the users table.
 *
 * The hash format is shared with the relay (auth.py):
 *     pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
 */
function verify_password(string $password, string $stored): bool
{
    $parts = explode('$', $stored);

    if (count($parts) !== 4 || $parts[0] !== 'pbkdf2_sha256') {
        return false;
    }

    $iterations = (int) $parts[1];
    $salt       = hex2bin($parts[2]);
    $expected   = $parts[3];

    if ($salt === false || $iterations < 1) {
        return false;
    }

    $derived = hash_pbkdf2('sha256', $password, $salt, $iterations, 64, false);

    return hash_equals($expected, $derived);
}

function login_user(array $user, string $password): bool
{
    if ((int) ($user['is_disabled'] ?? 0) === 1) {
        return false;
    }

    return verify_password($password, (string) $user['password_hash']);
}

function csrf_token(): string
{
    start_session();

    if (empty($_SESSION['csrf'])) {
        $_SESSION['csrf'] = bin2hex(random_bytes(16));
    }

    return $_SESSION['csrf'];
}

function check_csrf(): void
{
    $sent = (string) ($_POST['csrf'] ?? '');

    if ($sent === '' || !hash_equals(csrf_token(), $sent)) {
        http_response_code(400);
        echo t('CSRF 校验失败，请刷新页面重试。');
        exit;
    }
}

// ============================================================
// Helpers
// ============================================================

function h(?string $value): string
{
    return htmlspecialchars((string) $value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

/**
 * 跳到面板里的另一个页面。
 *
 * [!] Location 用**相对地址**发出去，不再拼服务端推导的基址。
 *
 * 真实事故：面板挂在 1Panel 反代后面（https://域名:54865/秘密路径/ → 上游
 * http://127.0.0.1:19102/，proxy_pass 结尾带斜杠 ⇒ nginx 把前缀从 URI 上剥掉）。
 * 面板看到的 REQUEST_URI 是 /index.php，panel_base_url() 于是算出 "/"，
 * 点「退出登录」发出去的就是 `Location: /index.php?action=login` —— 浏览器跳到
 * https://域名:54865/index.php?action=login，那是域名根目录（同一个域名上还有
 * 一个静态站），直接 404。之前的"面板完全没有 CSS"是同一个根因
 * （见 panel_base_href()）。
 *
 * 相对地址（`index.php?action=login`）交给**浏览器**用当前文档地址解析：
 * scheme / 主机 / **端口** / 反代前缀全都自动继承，服务端知不知道前缀都无所谓。
 * RFC 9110 §10.2.2 允许 Location 是相对引用，现代浏览器都按这个解析；
 * 反过来，拼绝对路径在"前缀被剥掉"或"换了端口"时必然指错。
 *
 * 唯一的前提是请求地址的目录部分就是面板目录 —— 面板自己的表单全部提交到
 * `index.php?action=...`（配合 <base href="./">），而且
 * enforce_trailing_slash() 会先把不带结尾斜杠的目录型地址 301 掉，
 * 所以这一点始终成立。
 *
 * 绝对目标（http(s):// 或 / 开头）原样发出：调用方明确要求绝对地址时不该被改。
 * 跳转目标本身一个字没改，全站仍然只有"index.php + action 参数"这一种形式。
 */
function redirect(string $target): void
{
    no_cache_headers();
    header('Location: ' . $target);
    exit;
}

function flash(?string $message = null, string $type = 'info')
{
    start_session();

    if ($message !== null) {
        $_SESSION['flash'] = ['message' => $message, 'type' => $type];
        return null;
    }

    $flash = $_SESSION['flash'] ?? null;
    unset($_SESSION['flash']);

    return $flash;
}

function human_bytes(int $bytes): string
{
    $units = ['B', 'KB', 'MB', 'GB', 'TB'];
    $value = (float) $bytes;
    $index = 0;

    while ($value >= 1024 && $index < count($units) - 1) {
        $value /= 1024;
        $index++;
    }

    return $index === 0
        ? sprintf('%d B', (int) $value)
        : sprintf('%.1f %s', $value, $units[$index]);
}

function seconds_to_human(?int $seconds): string
{
    if ($seconds === null) {
        return '—';
    }

    if ($seconds < 60) {
        return sprintf(t('%d 秒'), $seconds);
    }

    if ($seconds < 3600) {
        return sprintf(t('%d 分 %d 秒'), intdiv($seconds, 60), $seconds % 60);
    }

    return sprintf(
        t('%d 小时 %d 分'),
        intdiv($seconds, 3600),
        intdiv($seconds % 3600, 60)
    );
}

function json_out(array $payload): void
{
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE);
    exit;
}

// ============================================================
// 审计日志（面板侧）
// ============================================================
//
// WHY THIS EXISTS
//
// 面板的写操作全都走中继的管理接口，而中继给每条操作都会记一行 relay_events
// —— 但那一行的 actor 永远是 "panel"，source_ip 永远是面板容器的内网地址：
// 中继和面板之间只有一个共享 token，它分不出是哪个管理员点的。于是日志里能
// 看到「有人重置了 alice 的密码」，看不到「是谁重置的、从哪个 IP 来的」。
//
// 面板自己知道这两件事，所以面板也往同一张表（relay_events）写一行：
// 操作者 = 当前登录账号，IP = 浏览器 IP，说明 = 做了什么、目标是谁、成功与否。
// 中继那行是"接口被调用了"的凭证，这行是"是哪个人做的"，两条一起看就齐了。
//
// 三条铁律：
//   1. 审计写入**永远不能让操作失败** —— 全部包在 try/catch 里，写不进去就
//      当没这回事（中继自己还有一份）。
//   2. **绝不写密码 / token 的值**，只写"谁、对谁、做了什么、从哪来"。
//   3. 面板登录 / 登出也记（含根管理员），失败带原因 —— 中继只会记客户端
//      的 LOGIN，而面板登录根本不经过中继。

/**
 * 面板看到的访问者 IP。
 *
 * 面板挂在外层 nginx / 1Panel 反代后面，所以 REMOTE_ADDR 永远写着那台反代
 * （内网地址）。日志里每一行都写同一个 172.x 等于没写，所以优先取反代自己
 * 填的 X-Real-IP / X-Forwarded-For 的第一段，都没有才回退到 REMOTE_ADDR。
 * 面板容器不对公网暴露（只 expose 给反代），所以这两个头可以信。
 */
function panel_client_ip(): string
{
    foreach (['HTTP_X_REAL_IP', 'HTTP_X_FORWARDED_FOR', 'REMOTE_ADDR'] as $key) {
        $value = (string) ($_SERVER[$key] ?? '');

        if ($value === '') {
            continue;
        }

        $first = trim(explode(',', $value)[0]);

        if ($first !== '') {
            return $first;
        }
    }

    return '';
}

/**
 * 写一条审计事件。失败时静默（见上面的第 1 条铁律）。
 *
 * $fields: actor / room_id / session_id / detail / source_ip，缺省时 actor 取
 * 当前登录账号、IP 取浏览器 IP。
 */
function audit_event(string $action, array $fields = []): void
{
    if ($action === '') {
        return;
    }

    $actor = $fields['actor'] ?? ((current_user() ?? [])['username'] ?? '');

    try {
        $statement = db()->prepare(
            'INSERT INTO relay_events
                (actor, action, room_id, session_id, detail, source_ip)
             VALUES (?, ?, ?, ?, ?, ?)'
        );

        $statement->execute([
            substr((string) $actor, 0, 32),
            substr($action, 0, 32),
            substr((string) ($fields['room_id'] ?? ''), 0, 64),
            substr((string) ($fields['session_id'] ?? ''), 0, 32),
            substr((string) ($fields['detail'] ?? ''), 0, 255),
            substr((string) ($fields['source_ip'] ?? panel_client_ip()), 0, 64),
        ]);
    } catch (Throwable $e) {
        // 记账失败不影响任何操作，也不写进页面（用户看不到数据库错误才有意义）。
    }
}

/**
 * 审计日志里每一个 action 代号 → [说明, 色调, 分类]。
 *
 * 左边是**中继写进数据库的代号**（数据，永远不翻译）；中间是药丸的色调
 * （ok / warn / bad / 空 = 中性）；右边是分类（见 panel_event_categories()）。
 *
 * 认不出的代号走 panel_event_meta() 的兜底：直接显示代号本身，归到「系统」，
 * 不留空白 —— 以后中继加了新代号，日志页也不会变成一片空白。
 */
function panel_event_types(): array
{
    return [
        // ---- 登录 / 登出（客户端和面板都算一类）----
        'login_ok'                  => [t('登录成功'), 'ok', 'login'],
        'login_failed'              => [t('登录失败'), 'bad', 'login'],
        'login_disabled'            => [t('禁用账号尝试登录'), 'bad', 'login'],
        'login_throttled'           => [t('登录过于频繁'), 'warn', 'login'],
        'logout'                    => [t('退出登录'), '', 'login'],
        'panel_login_ok'            => [t('面板登录成功'), 'ok', 'login'],
        'panel_login_failed'        => [t('面板登录失败'), 'bad', 'login'],
        'panel_logout'              => [t('退出面板登录'), '', 'login'],

        // ---- 房间 ----
        'room_created'              => [t('创建房间'), 'ok', 'room'],
        'room_joined'               => [t('加入房间'), 'ok', 'room'],
        'room_left'                 => [t('退出房间'), '', 'room'],
        'client_delete_room'        => [t('房主删除房间'), 'warn', 'room'],
        'auto_delete_empty_room'    => [t('自动删除空房间'), 'warn', 'room'],
        'host_timeout'              => [t('房主心跳超时，房间被回收'), 'warn', 'room'],
        'panel_delete_room'         => [t('面板强制关闭房间'), 'bad', 'room'],
        'panel_revoke_room'         => [t('面板强制关闭房间'), 'bad', 'room'],
        'panel_revoke_room_timeout' => [t('关闭房间超时'), 'bad', 'room'],
        'panel_revoke_stale_room'   => [t('清理残留房间记录'), 'warn', 'room'],
        'panel_delete_stale_room'   => [t('清理残留房间记录'), 'warn', 'room'],
        'panel_purge_stale_rooms'   => [t('清理残留房间记录'), 'warn', 'room'],

        // ---- 连接：谁进了世界、谁断开了 ----
        //
        // host_online / host_offline 就是房主的上下线（中继在 HOST 握手成功 /
        // 控制连接结束时记的），session_open / session_close 是玩家每一次游戏
        // 会话的开始与结束。
        'host_online'               => [t('开始开服（房主上线）'), 'ok', 'connection'],
        'host_offline'              => [t('停止开服（房主下线）'), 'warn', 'connection'],
        'session_open'              => [t('玩家进入世界'), 'ok', 'connection'],
        'session_close'             => [t('玩家离开世界'), '', 'connection'],
        // 2026-09-29：客户端重连前的**只读**判定探测（`SESSION __mclink__ ...`，
        // PROTOCOL §8.10）。排查「他为什么自动重连 / 为什么不自动」时，
        // 这一行给出的就是中继当时的口径（valid / in_room / reason）。
        'session_probe'             => [t('重连判定探测'), '', 'connection'],

        // ---- 面板操作 ----
        'setting_changed'           => [t('修改运行开关'), 'warn', 'panel'],
        'panel_create_user'         => [t('面板创建账号'), 'ok', 'panel'],
        'panel_delete_user'         => [t('面板删除账号'), 'bad', 'panel'],
        'panel_set_role'            => [t('面板修改权限'), 'warn', 'panel'],
        'panel_set_password'        => [t('面板重置密码'), 'warn', 'panel'],
        'panel_disable_user'        => [t('面板禁用账号'), 'warn', 'panel'],
        'panel_enable_user'         => [t('面板启用账号'), 'ok', 'panel'],
        'panel_rename_user'         => [t('面板改名'), 'warn', 'panel'],
        'panel_self_rename'         => [t('自助改名'), 'warn', 'panel'],
        'panel_kick_user'           => [t('面板踢出账号'), 'bad', 'panel'],
        'panel_kick_player'         => [t('面板踢出玩家'), 'bad', 'panel'],
        'panel_kick_member'         => [t('面板踢出成员'), 'bad', 'panel'],
        'panel_clear_events'        => [t('清空操作日志'), 'bad', 'panel'],
        // 中继的账号服务（gatekeeper）为同一个面板动作记的那一行。
        // 归到「面板操作」：同一个动作的两条记录应该落在同一个筛选里。
        'user_created'              => [t('创建账号'), 'ok', 'panel'],
        'user_deleted'              => [t('账号已删除'), 'bad', 'panel'],
        'password_changed'          => [t('重置密码'), 'warn', 'panel'],
        'user_renamed'              => [t('改名'), 'warn', 'panel'],
        'role_changed'              => [t('修改权限'), 'warn', 'panel'],
        'user_disabled'             => [t('禁用账号'), 'warn', 'panel'],
        'user_enabled'              => [t('启用账号'), 'ok', 'panel'],

        // ---- 系统 / 其它 ----
        'bootstrap_admin'           => [t('创建初始管理员'), 'ok', 'system'],
        'password_reset_offline'    => [t('SSH 重置根管理员密码'), 'warn', 'system'],
        'events_purged'             => [t('自动清理过期日志'), 'warn', 'system'],
    ];
}

/** 分类（顺序就是筛选条里的顺序）。slug 是 URL 参数值，永远是 ASCII。 */
function panel_event_categories(): array
{
    return [
        'login'      => t('登录 / 登出'),
        'room'       => t('房间'),
        'connection' => t('连接'),
        'panel'      => t('面板操作'),
        'system'     => t('系统'),
    ];
}

/** 结果（= 药丸的色调）。'' 永远表示"全部"，不在这里。 */
function panel_event_outcomes(): array
{
    return [
        'ok'   => t('成功'),
        'warn' => t('警告'),
        'bad'  => t('失败'),
    ];
}

/** 一条日志行的显示信息：说明 / 色调 / 分类（slug、标签、CSS class）。 */
function panel_event_meta(?string $action): array
{
    $code = (string) $action;
    $types = panel_event_types();

    [$label, $tone, $category] = $types[$code] ?? [$code, '', 'system'];

    $categories = panel_event_categories();

    return [
        'label'          => $label,
        'tone'           => $tone,
        'category'       => $category,
        'category_label' => $categories[$category] ?? $categories['system'],
        'category_class' => 'cat-' . $category,
    ];
}

/**
 * 某个分类 / 色调对应的 action 代号清单。
 *
 * 返回 null = 不筛（两个条件都是"全部"）。返回空数组 = 筛出来的集合是空的，
 * 调用方直接给空结果（否则会退化成"不筛"，把不该显示的行使劲显示出来）。
 */
function panel_event_codes(string $category, string $tone): ?array
{
    if ($category === '' && $tone === '') {
        return null;
    }

    $codes = [];

    foreach (panel_event_types() as $code => $meta) {
        if ($category !== '' && $meta[2] !== $category) {
            continue;
        }

        if ($tone !== '' && $meta[1] !== $tone) {
            continue;
        }

        $codes[] = $code;
    }

    return $codes;
}

/**
 * 「连接」这一类里的玩家进 / 出世界。
 *
 * 中继每次配对出一条隧道就写一行 room_sessions（谁、哪个房间、哪个 IP、
 * 什么时候开始、什么时候结束、传了多少字节），面板把它读出来合成两行 ——
 * 中继的内存日志里有，但 relay_events 里没有，所以这是把已有的真实记录
 * 摊开给管理员看，不是面板自己造数据。
 */
function panel_session_events(int $limit = 200): array
{
    $limit = max(1, min(1000, $limit));

    try {
        $statement = db()->prepare(
            'SELECT session_id, room_id, player_username, player_ip, started_at,
                    ended_at, duration_sec, bytes_to_host, bytes_to_player,
                    end_reason
               FROM room_sessions ORDER BY id DESC LIMIT ' . $limit
        );
        $statement->execute();
        $sessions = $statement->fetchAll();
    } catch (Throwable $e) {
        return [];
    }

    $rows = [];

    foreach ($sessions as $session) {
        $sessionId = (string) ($session['session_id'] ?? '');
        $roomId    = (string) ($session['room_id'] ?? '');
        $actor     = (string) ($session['player_username'] ?? '');
        $ip        = (string) ($session['player_ip'] ?? '');

        $rows[] = [
            'created_at' => (string) ($session['started_at'] ?? ''),
            'action'     => 'session_open',
            'actor'      => $actor,
            'room_id'    => $roomId,
            'session_id' => $sessionId,
            'source_ip'  => $ip,
            'detail'     => t('进入世界：游戏会话开始'),
        ];

        $endedAt = (string) ($session['ended_at'] ?? '');

        if ($endedAt === '') {
            // 还在玩（或者中继上次是被杀掉的，没来得及写结束时间）。
            continue;
        }

        $rows[] = [
            'created_at' => $endedAt,
            'action'     => 'session_close',
            'actor'      => $actor,
            'room_id'    => $roomId,
            'session_id' => $sessionId,
            'source_ip'  => $ip,
            'detail'     => sprintf(
                t('离开世界（原因：%s，时长 %s，上行 %s / 下行 %s）'),
                (string) ($session['end_reason'] ?: '-'),
                seconds_to_human((int) ($session['duration_sec'] ?? 0)),
                human_bytes((int) ($session['bytes_to_host'] ?? 0)),
                human_bytes((int) ($session['bytes_to_player'] ?? 0))
            ),
        ];
    }

    return $rows;
}

/**
 * 日志页要显示的行（**服务端**筛选）。
 *
 * $category  '' = 全部分类，否则是 panel_event_categories() 里的 slug
 * $tone      '' = 全部结果，否则是 ok / warn / bad
 *
 * 筛选写在 SQL 的 WHERE 里，所以 LIMIT 是"符合条件的最新 N 条"，而不是
 * "最新 N 条里再挑" —— 页面上的条数限制才有意义。
 */
function panel_events_fetch(string $category, string $tone, int $limit = 200): array
{
    $limit = max(1, min(1000, $limit));
    $wanted = panel_event_codes($category, $tone);

    if ($wanted === []) {
        return [];
    }

    $rows = [];

    try {
        $sql = 'SELECT * FROM relay_events';
        $params = [];

        if ($wanted !== null) {
            $marks = [];

            foreach ($wanted as $code) {
                $marks[] = '?';
            }

            $sql .= ' WHERE action IN (' . implode(',', $marks) . ')';
            $params = $wanted;
        }

        // $limit 已经被强制成 1..1000 的整数，拼进 SQL 是安全的。
        $sql .= ' ORDER BY id DESC LIMIT ' . $limit;

        $statement = db()->prepare($sql);
        $statement->execute($params);
        $rows = $statement->fetchAll();
    } catch (Throwable $e) {
        $rows = [];
    }

    // 「连接」这一类还要算上玩家的进 / 出世界（来自 room_sessions）。
    // 色调筛选下不加：那两行是中性的（既不算成功也不算失败）。
    if (($category === '' || $category === 'connection') && $tone === '') {
        foreach (panel_session_events($limit) as $row) {
            $rows[] = $row;
        }
    }

    usort($rows, static function (array $a, array $b): int {
        return (string) ($b['created_at'] ?? '') <=> (string) ($a['created_at'] ?? '');
    });

    return array_slice($rows, 0, $limit);
}

/**
 * 审计日志保留几天。「每 10 天自动清理一次」里的那个 10。
 *
 * 中继侧也有一份同样的数字（server/relay_server.py 的 EVENTS_RETENTION_DAYS）：
 * 中继是定时清理的主力（它一直在跑），面板这里的清理是"只更新了面板、中继
 * 还没重建"时的兜底。改的话两边一起改。
 */
const PANEL_EVENT_RETENTION_DAYS = 10;

/**
 * 删掉超过保留期的日志行，返回删掉的条数。
 *
 * 由日志页在渲染前调用（见 web/index.php 的 case 'events'）。永远不抛异常：
 * 清理失败不该让日志页打不开。
 */
function panel_purge_old_events(int $days = PANEL_EVENT_RETENTION_DAYS): int
{
    // $days 是常量整数（不是用户输入），直接拼进 SQL 没有注入面。
    $days = max(1, $days);

    try {
        return (int) db()->exec(
            sprintf(
                'DELETE FROM relay_events WHERE created_at < (NOW() - INTERVAL %d DAY)',
                $days
            )
        );
    } catch (Throwable $e) {
        return 0;
    }
}
