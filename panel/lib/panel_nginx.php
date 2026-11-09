<?php
/**
 * CraftLink 管理面板 —— 自带 nginx 的**配置渲染**（M3 接口 + M4 域名与 SSL）。
 *
 * ============================================================================
 *  WHY THIS FILE EXISTS
 * ============================================================================
 *
 * 「设置 → 模板 → 生效」这条链路的中间那一段就在这里：
 *
 *     panel_setting_defs() / 设置页
 *          │  （用户改监听端口 / 安全入口 / 未认证返回码 / 域名 / 证书）
 *          ▼
 *     panel_nginx_render($dir)            ← 本文件：纯函数式渲染，写文件
 *          │  渲染 5 个文件到目标目录
 *          ▼
 *     panel/bin/panel-agent.sh（root）    ← `nginx -t` 试跑 → 通过才替换 → reload
 *
 * 为什么渲染和生效要分开：
 *   * 渲染要能在**没有 nginx 的机器上**验（本仓库的冒烟测试就是这么验的：
 *     把配置渲染到临时目录，逐条断言里面该有什么、不该有什么）；
 *   * `nginx -t` + `nginx -s reload` 需要 root，而 php-fpm 是 nobody —— 那条
 *     路走 panel_agent.php 的文件队列。
 *
 * ============================================================================
 *  容器内端口是固定的，宿主机端口才是设置里的值
 * ============================================================================
 *
 * 这一点必须说清楚，否则"我改了端口怎么没生效"会一直出现：
 *
 *   * 容器内：HTTP 80 / HTTPS 443 / 急救 19103 —— **固定**。nginx 只听
 *     这三个（同一个容器里改监听端口没有任何好处，只会多一种"配置和端口
 *     不一致"的失败方式）。
 *   * 宿主机：`listen_ip:listen_port` → 容器 80 由 compose 发布
 *     （`scripts/panel-apply.sh` 按设置生成端口映射并重建容器）。
 *   * 所以改 listen_ip / listen_port **不会**把面板改到打不开：最坏情况是
 *     宿主机映射没更新，而急救入口（127.0.0.1:<急救端口>）一直是通的。
 *
 * 这个文件和别的 lib 一样是"纯声明"：不连库、不读文件、不写文件，被调用时才动手。
 */

declare(strict_types=1);

// ============================================================
// 1. 路径与端口
// ============================================================

/** 模板目录（仓库里是 panel/nginx/templates，容器里是 /var/www/nginx-templates）。 */
function panel_nginx_template_dir_candidates(): array
{
    $candidates = [];

    $env = getenv('MCLINK_NGINX_TEMPLATE_DIR');

    if ($env !== false && trim((string) $env) !== '') {
        $candidates[] = rtrim(trim((string) $env), '/');
    }

    $config = config('panel.nginx_template_dir');

    if (is_string($config) && trim($config) !== '') {
        $candidates[] = rtrim(trim($config), '/');
    }

    $candidates[] = '/var/www/nginx-templates';                 // 容器里推荐的挂载点
    $candidates[] = dirname(__DIR__) . '/nginx/templates';      // 仓库布局

    return array_values(array_unique($candidates));
}

function panel_nginx_template_dir(): string
{
    static $chosen = null;

    if ($chosen !== null) {
        return $chosen;
    }

    foreach (panel_nginx_template_dir_candidates() as $dir) {
        if (is_dir($dir) && is_readable($dir)) {
            $chosen = $dir;

            return $chosen;
        }
    }

    $chosen = (string) (panel_nginx_template_dir_candidates()[0] ?? '');

    return $chosen;
}

/** 生效中的配置目录（渲染目标）。 */
function panel_nginx_server_dir_candidates(): array
{
    $candidates = [];

    $env = getenv('MCLINK_NGINX_SERVER_DIR');

    if ($env !== false && trim((string) $env) !== '') {
        $candidates[] = rtrim(trim((string) $env), '/');
    }

    $config = config('panel.nginx_server_dir');

    if (is_string($config) && trim($config) !== '') {
        $candidates[] = rtrim(trim($config), '/');
    }

    $candidates[] = '/etc/nginx/mclink-servers';                 // 容器里的正式位置
    $candidates[] = dirname(__DIR__) . '/nginx/live';            // 仓库 / 开发布局

    return array_values(array_unique($candidates));
}

/** 渲染目标目录（写的时候用 $forWrite=true，必要时建出来）。 */
function panel_nginx_server_dir(bool $forWrite = false): string
{
    static $chosen = null;

    if ($chosen !== null) {
        return $chosen;
    }

    $candidates = panel_nginx_server_dir_candidates();

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

            if (@mkdir($dir, 0755, true)) {
                $chosen = $dir;

                return $chosen;
            }
        }
    }

    $chosen = (string) ($candidates[0] ?? '');

    return $chosen;
}

/** 容器内的 HTTP 端口（固定 80；宿主机端口由 compose 映射）。 */
function panel_http_port(): int
{
    $value = (int) (getenv('MCLINK_NGINX_HTTP_PORT') ?: 80);

    return $value >= 1 && $value <= 65535 ? $value : 80;
}

/** 容器内的 HTTPS 端口（固定 443）。 */
function panel_https_port(): int
{
    $value = (int) (getenv('MCLINK_NGINX_HTTPS_PORT') ?: 443);

    return $value >= 1 && $value <= 65535 ? $value : 443;
}

/** 宿主机上的 HTTPS 端口（默认 443；跳转地址里要带上它）。 */
function panel_https_host_port(): int
{
    $value = (int) (getenv('MCLINK_PANEL_HTTPS_PORT') ?: 443);

    return $value >= 1 && $value <= 65535 ? $value : 443;
}

/** 宿主机上的 HTTP 端口（默认 80；ACME 校验和跳转都走它）。 */
function panel_http_host_port(): int
{
    $value = (int) (getenv('MCLINK_PANEL_HTTP_PORT') ?: 80);

    return $value >= 1 && $value <= 65535 ? $value : 80;
}

/**
 * 急救入口端口（默认 19103）。
 *
 * [!] 这是"配置改坏了还能进去"的那条路：compose 只把它发布到**宿主机回环**
 *     （127.0.0.1:19103），公网到不了；而且这一份 server 块**不做**安全入口、
 *     域名绑定和 HTTPS 跳转判断。所以无论上面三项怎么配错，本机都能进去改回来。
 */
function panel_rescue_port(): int
{
    $value = (int) (getenv('MCLINK_PANEL_RESCUE_PORT') ?: 19103);

    return $value >= 1 && $value <= 65535 ? $value : 19103;
}

/** 急救入口的地址（给页面和 mclink 管理台显示用）。 */
function panel_rescue_url(): string
{
    $port = panel_rescue_port();
    $suffix = $port === 80 ? '' : ':' . $port;

    return 'http://127.0.0.1' . $suffix . '/';
}

/** ACME HTTP-01 校验文件放哪（certbot 的 webroot）。 */
function panel_acme_webroot(): string
{
    $value = trim((string) (getenv('MCLINK_ACME_WEBROOT') ?: ''));

    return $value !== '' ? rtrim($value, '/') : '/var/www/acme';
}

/** 面板网页根目录（容器里是 /var/www/panel；本地冒烟测试会覆盖它）。 */
function panel_nginx_webroot(): string
{
    $value = trim((string) (getenv('MCLINK_PANEL_WEBROOT') ?: ''));

    return $value !== '' ? rtrim($value, '/') : '/var/www/panel';
}

/**
 * 这个请求是不是走急救入口进来的（php-fpm 那边要据此跳过域名判定）。
 *
 * 判据是 `SERVER_PORT`：急救 server 块监听的是它自己的端口，公网请求不可能
 * 伪造（Host 头能伪造，SERVER_PORT 不能）。`MCLINK_RESCUE` 是给本地测试用的
 * 显式开关。
 */
function panel_is_rescue_request(): bool
{
    if (trim((string) (getenv('MCLINK_RESCUE') ?: '')) === '1') {
        return true;
    }

    // nginx 在急救 server 块里 `set $mclink_rescue 1;`，再通过 fastcgi_param /
    // proxy_set_header 传进来 —— 这两个都是 **nginx 覆盖写的**，客户端伪造不了
    // （proxy_set_header 会替换掉客户端带的同名头）。
    if ((string) ($_SERVER['MCLINK_RESCUE'] ?? '') === '1'
        || (string) ($_SERVER['HTTP_X_MCLINK_RESCUE'] ?? '') === '1') {
        return true;
    }

    $port = (int) ($_SERVER['SERVER_PORT'] ?? 0);

    return $port > 0 && $port === panel_rescue_port();
}

// ============================================================
// 2. 渲染用的值
// ============================================================

/**
 * nginx 层的"未认证返回码"。
 *
 * `login`（显示登录页）在 nginx 这一层没有对应物：nginx 不知道访客有没有登录。
 * 所以：
 *   * 没配安全入口时：nginx 不做路径限制，一切交给 PHP（PHP 按 login 跳登录页）；
 *   * 配了安全入口、或者 Host 不是绑定域名时：`login` 按 **404** 处理 ——
 *     否则"配了安全入口却把登录页摆给所有人"等于白配。
 */
function panel_nginx_guard_code(): string
{
    $code = panel_listen_config()['unauth_code'];

    if ($code === 'login') {
        return '404';
    }

    return in_array($code, ['404', '403', '444', '200'], true) ? $code : '404';
}

/** 绑定域名在 nginx 里的 server_name 值（没绑 = `_`）。 */
function panel_nginx_server_name(): string
{
    $domain = panel_bound_domain();

    return $domain === '' ? '_' : $domain;
}

/**
 * Host 守卫：绑了域名就只认这个域名（含 *. 通配）。
 *
 * [!] 为什么不用 `server_name` 就够了：
 *   同一个端口上只有一个 server 块时，nginx 会把它当作 default_server ——
 *   Host 不匹配也会落到它身上。所以必须再显式判一次 Host。
 * [!] 为什么**不**顺手放行 127.0.0.1 / localhost：
 *   Host 头是客户端随便写的，"允许 Host: 127.0.0.1" = 允许所有人。真的被
 *   域名锁在外面时，正确的入口是急救端口 + mclink 管理台（见第 5 节）。
 */
function panel_nginx_host_guard(): string
{
    $domain = panel_bound_domain();

    if ($domain === '') {
        return '';
    }

    $body = str_starts_with($domain, '*.') ? substr($domain, 2) : $domain;
    $quoted = str_replace('.', '\\.', $body);

    $pattern = str_starts_with($domain, '*.')
        ? '^(.+\\.)?' . $quoted . '$'
        : '^' . $quoted . '$';

    return "    # " . sprintf(t("域名绑定：只有 %s 能访问（其他 Host 按未认证返回码处理）"), $domain) . "\n"
        . "    if (\$host !~* \"{$pattern}\") {\n"
        . '        return ' . panel_nginx_guard_code() . ";\n"
        . "    }\n";
}

/**
 * 安全入口守卫（**server 块级别**的 if）。
 *
 * 为什么放在 server 级、而不是写成 `location / { return 404; }`：
 *   入口内的请求要先把前缀剥掉（`rewrite … last`），剥完之后 nginx 会**重新
 *   做一次 location 匹配**，那时 URI 已经变成 `/index.php`、`/assets/...`；
 *   如果"入口外一律 404"是靠 `location /` 表达的，这些被重写过的请求会一头
 *   撞上那个 404。server 级的 if 只在 server-rewrite 阶段跑一次（原始 URI），
 *   `rewrite … last` 触发的重匹配不会重跑它 —— 语义正好是我们想要的。
 *
 * ACME 校验路径写在白名单里：certbot 的 HTTP-01 校验必须能直达，
 * 不能因为配了安全入口就 404（否则续期一定会失败）。
 */
function panel_nginx_entry_guard(): string
{
    $entry = (string) panel_listen_config()['entry_path'];

    if ($entry === '') {
        return '';
    }

    $escaped = str_replace('.', '\\.', $entry);

    return "    # " . sprintf(t("安全入口守卫：只有 %s/ 下面能到面板（ACME 校验除外），其余一律返回 %s"), $entry, panel_nginx_guard_code()) . "\n"
        . "    if (\$uri !~ ^({$escaped}(/|\$)|/\\.well-known/acme-challenge/)) {\n"
        . '        return ' . panel_nginx_guard_code() . ";\n"
        . "    }\n";
}

/** 安全入口相关的 location 块（入口剥离 + 普通路径处理）。 */
function panel_nginx_entry_block(): string
{
    $listen = panel_listen_config();
    $entry = (string) $listen['entry_path'];

    $normal = "    location / {\n"
        . "        try_files \$uri \$uri/ /index.php?\$query_string;\n"
        . "    }\n";

    if ($entry === '') {
        return "    # " . t("没有配置 URL 安全入口：所有路径都交给面板（未认证返回码由 PHP 判定）") . "\n"
            . $normal;
    }

    return "    # " . sprintf(t("URL 安全入口：%s/ 前缀在这里被剥掉，交给下面的 PHP / 静态文件规则"), $entry) . "\n"
        . "    location = {$entry} {\n"
        . "        return 302 {$entry}/;\n"
        . "    }\n"
        . "\n"
        . "    location = {$entry}/login.php {\n"
        . "        return 302 {$entry}/index.php?action=login;\n"
        . "    }\n"
        . "\n"
        . "    location ^~ {$entry}/ {\n"
        . "        # " . t("把入口前缀剥掉再交给下面的 PHP / 静态文件规则（last = 重新匹配 location）") . "\n"
        . "        rewrite ^{$entry}/(.*)\$ /\$1 last;\n"
        . "    }\n"
        . "\n"
        . $normal;
}

/** PHP 后端：默认 fastcgi（容器里就是 php-fpm）；proxy 只在没有 php-fpm 的冒烟环境用。 */
function panel_nginx_php_backend(): string
{
    $mode = strtolower(trim((string) (getenv('MCLINK_NGINX_BACKEND') ?: 'fastcgi')));

    if ($mode === 'proxy') {
        $addr = trim((string) (getenv('MCLINK_NGINX_BACKEND_ADDR') ?: '127.0.0.1:9000'));

        return "        # " . t("[!] proxy 后端只在本地冒烟测试（没有 php-fpm）时使用；生产是 fastcgi。") . "\n"
            . "        proxy_pass http://{$addr};\n"
            . "        proxy_set_header Host \$host;\n"
            . "        proxy_set_header X-Real-IP \$remote_addr;\n"
            . "        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;\n"
            . "        proxy_set_header X-Forwarded-Proto \$scheme;\n"
            . "        # " . t("急救标记：proxy_set_header 会覆盖客户端传来的同名头，伪造不了") . "\n"
            . "        proxy_set_header X-Mclink-Rescue \$mclink_rescue;\n";
    }

    $addr = trim((string) (getenv('MCLINK_NGINX_FASTCGI_ADDR') ?: '127.0.0.1:9000'));

    return "        # " . t("急救标记：只有急救 server 块里 set 了它，客户端伪造不了") . "\n"
        . "        fastcgi_param MCLINK_RESCUE \$mclink_rescue;\n"
        . "        fastcgi_pass {$addr};\n"
        . "        fastcgi_index index.php;\n"
        . "        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;\n"
        . "        fastcgi_param HTTPS \$https if_not_empty;\n"
        . "        include fastcgi_params;\n";
}

/** ACME 校验目录（HTTP-01 必须是明文 HTTP，而且不能被跳转吃掉）。 */
function panel_nginx_acme_block(): string
{
    $root = panel_acme_webroot();

    return "    # " . t("ACME（Let's Encrypt HTTP-01）校验文件：明文 HTTP 直达，不参与跳转 / 安全入口") . "\n"
        . "    location ^~ /.well-known/acme-challenge/ {\n"
        . "        root {$root};\n"
        . "        default_type \"text/plain\";\n"
        . "        try_files \$uri =404;\n"
        . "        access_log off;\n"
        . "    }\n";
}

/** 模板占位符 → 值。渲染前会检查"模板里的占位符这里都给了"。 */
function panel_nginx_render_values(): array
{
    $listen = panel_listen_config();
    $tls = panel_nginx_tls_state();
    $serverDir = panel_nginx_server_dir();

    $serverCommon = "    root " . panel_nginx_webroot() . ";\n"
        . "    index index.php;\n"
        . "    charset utf-8;\n"
        . "    client_max_body_size 4m;\n"
        . "    server_tokens off;\n"
        . "    add_header X-Robots-Tag \"noindex, nofollow\" always;\n"
        . "    add_header X-Content-Type-Options \"nosniff\" always;\n"
        . "    add_header X-Frame-Options \"SAMEORIGIN\" always;\n";

    $httpMode = $tls['force_https']
        ? "    include {$serverDir}/panel-http-redirect.inc;"
        : "    include {$serverDir}/panel-locations.inc;";

    $httpsBlock = '';

    if ($tls['enabled']) {
        $httpsBlock = "# ---- " . t("HTTPS 服务（TLS）") . " --------------------------------------------------\n"
            . "server {\n"
            . '    listen 0.0.0.0:' . panel_https_port() . " ssl;\n"
            . '    server_name ' . panel_nginx_server_name() . ";\n"
            . "\n"
            . '    ssl_certificate     ' . $tls['cert'] . ";\n"
            . '    ssl_certificate_key ' . $tls['key'] . ";\n"
            . "    ssl_protocols TLSv1.2 TLSv1.3;\n"
            . "    ssl_session_cache shared:MCLinkPanelTLS:2m;\n"
            . "    ssl_session_timeout 1h;\n"
            . "\n"
            . $serverCommon
            . "\n"
            . panel_nginx_host_guard()
            . panel_nginx_entry_guard()
            . "    include {$serverDir}/panel-locations.inc;\n"
            . "}\n";
    } else {
        $httpsBlock = "# ---- "
            . sprintf(t('HTTPS 服务：本次没有启用（ssl_mode=%s）'), $tls['mode'])
            . " ----\n";
    }

    $entry = (string) $listen['entry_path'];
    $domain = panel_bound_domain();

    return [
        'GENERATED_AT'      => date('c'),
        'PANEL_VERSION'     => PANEL_VERSION,
        'HTTP_PORT'         => (string) panel_http_port(),
        'HTTPS_PORT'        => (string) panel_https_port(),
        'RESCUE_PORT'       => (string) panel_rescue_port(),
        'SERVER_DIR'        => $serverDir,
        'ACME_ROOT'         => panel_acme_webroot(),
        'SERVER_NAME'       => panel_nginx_server_name(),
        'SERVER_COMMON'     => $serverCommon,
        'HOST_GUARD'        => panel_nginx_host_guard(),
        'ENTRY_GUARD'       => panel_nginx_entry_guard(),
        'HTTP_MODE_INCLUDE' => $httpMode,
        'HTTPS_SERVER_BLOCK' => $httpsBlock,
        'ENTRY_BLOCK'       => panel_nginx_entry_block(),
        'PHP_BACKEND'       => panel_nginx_php_backend(),
        'ACME_BLOCK'        => panel_nginx_acme_block(),
        'LOGIN_COMPAT'      => $entry === ''
            ? "    # " . t("老地址 /login.php 兼容：面板入口始终是 index.php?action=login") . "\n"
                . "    location = /login.php {\n"
                . "        return 302 /index.php?action=login;\n"
                . "    }\n"
            : '',
        'BOUND_DOMAIN_NOTE' => $domain === '' ? t('没绑定（任何 Host 都能访问）') : $domain,
        'ENTRY_NOTE'        => $entry === '' ? t('没配置（/ 就是面板）') : $entry . '/',
        'UNAUTH_CODE'       => panel_nginx_guard_code(),
        'TLS_NOTE'          => $tls['enabled']
            ? sprintf(t('%s（%s）'), panel_ssl_mode_label($tls['mode']), $tls['cert'])
            : t('没用 HTTPS'),
        'HTTPS_PORT_SUFFIX' => panel_https_host_port() === 443 ? '' : ':' . panel_https_host_port(),
        'HTTP_REDIRECT_ACTIVE' => $tls['force_https'] ? '1' : '',
    ];
}

/**
 * TLS 这一层到底能不能起来（渲染时的判定）。
 *
 * [!] 这里**故意宽容**：设置里写了 ssl_mode=upload 但证书文件读不到时，
 *     不生成引用那个路径的 HTTPS server 块，而是退回"只有 HTTP"并给出一条
 *     警告。理由：生成一份 nginx 起不来的配置 = 面板可能整体打不开；
 *     宁可"HTTPS 暂时没生效 + 页面上一句为什么"，也不要"整个面板 500"。
 *
 * @return array{enabled:bool,mode:string,cert:string,key:string,force_https:bool,warnings:array<int,string>}
 */
function panel_nginx_tls_state(): array
{
    $mode = panel_ssl_mode();
    $cert = panel_ssl_cert_file();
    $key = panel_ssl_key_file();
    $warnings = [];

    $enabled = false;

    if ($mode === 'off') {
        if (panel_force_https()) {
            $warnings[] = t('「强制 HTTPS」开着，但 SSL 模式是「不用 HTTPS」—— 本次不生成跳转（否则面板会打不开）。');
        }
    } elseif ($cert === '' || $key === '') {
        $warnings[] = t('选了 SSL 模式但证书 / 私钥路径是空的 —— 本次按 HTTP 渲染。');
    } elseif (!is_file($cert) || !is_file($key)) {
        $warnings[] = sprintf(
            t('证书 / 私钥在容器里读不到（%s）—— 本次按 HTTP 渲染，旧配置保留。'),
            is_file($cert) ? $key : $cert
        );
    } else {
        $enabled = true;
    }

    if ($mode === 'acme' && $enabled && panel_bound_domain() === '') {
        $warnings[] = t('ACME 模式但没绑定域名：请先在「绑定域名」里填上要签的域名。');
    }

    return [
        'enabled'      => $enabled,
        'mode'         => $mode,
        'cert'         => $cert,
        'key'          => $key,
        'force_https'  => $enabled && panel_force_https(),
        'warnings'     => $warnings,
    ];
}

/** 渲染时会写在页面上 / 日志里的提示（含 listen 与容器端口的关系）。 */
function panel_nginx_render_notes(): array
{
    $listen = panel_listen_config();
    $tls = panel_nginx_tls_state();
    $notes = $tls['warnings'];

    $notes[] = sprintf(
        t('容器内 nginx 固定监听 HTTP %d / HTTPS %d；宿主机上的 %s 由 compose 映射到这个端口（改端口要跑 mclink apply-network 重建容器）。'),
        panel_http_port(),
        panel_https_port(),
        $listen['nginx_listen']
    );

    if (panel_rescue_port() === panel_http_port() || panel_rescue_port() === panel_https_port()) {
        $notes[] = t('急救端口和面板端口撞了：请把 MCLINK_PANEL_RESCUE_PORT 改成别的端口。');
    }

    return $notes;
}

// ============================================================
// 3. 渲染
// ============================================================

/** 要渲染的模板 → 输出文件名。 */
function panel_nginx_render_map(): array
{
    return [
        'panel-server.conf.template'             => 'panel-server.conf',
        'panel-locations.inc.template'           => 'panel-locations.inc',
        'panel-rescue-locations.inc.template'    => 'panel-rescue-locations.inc',
        'panel-backend.inc.template'             => 'panel-backend.inc',
        'panel-http-redirect.inc.template'       => 'panel-http-redirect.inc',
        'panel-acme.inc.template'                => 'panel-acme.inc',
    ];
}

/** 把模板里的 {{NAME}} 换成值；没给值的占位符原样留着（渲染会因此失败并报出来）。 */
function panel_nginx_apply_placeholders(string $template, array $values): string
{
    $names = [];

    if (preg_match_all('/\{\{([A-Z0-9_]+)\}\}/', $template, $matches) > 0) {
        $names = array_values(array_unique($matches[1]));
    }

    $out = $template;

    foreach ($names as $name) {
        if (!array_key_exists($name, $values)) {
            continue;
        }

        $out = str_replace('{{' . $name . '}}', (string) $values[$name], $out);
    }

    return $out;
}

/** 模板里还有没被替换的占位符？返回名字列表（空 = 都替换了）。 */
function panel_nginx_leftover_placeholders(string $rendered): array
{
    if (preg_match_all('/\{\{([A-Z0-9_]+)\}\}/', $rendered, $matches) < 1) {
        return [];
    }

    return array_values(array_unique($matches[1]));
}

/**
 * 渲染全部配置文件到 $dir（默认：生效目录）。
 *
 * 只写文件，**不碰正在运行的 nginx**（`nginx -t` 与 reload 是 root 代理的事）。
 *
 * @return array{ok:bool,dir:string,error:string,files:array<int,string>,notes:array<int,string>}
 */
function panel_nginx_render(string $dir = ''): array
{
    $result = [
        'ok'    => false,
        'dir'   => $dir,
        'error' => '',
        'files' => [],
        'notes' => [],
    ];

    if ($dir === '') {
        $dir = panel_nginx_server_dir(true);
    }

    $result['dir'] = $dir;

    if ($dir === '') {
        $result['error'] = t('没有可用的 nginx 配置目录。');

        return $result;
    }

    if (!is_dir($dir) && !@mkdir($dir, 0755, true)) {
        $result['error'] = sprintf(t('建不出 nginx 配置目录：%s'), $dir);

        return $result;
    }

    if (!is_writable($dir)) {
        $result['error'] = sprintf(t('nginx 配置目录不可写：%s'), $dir);

        return $result;
    }

    $templateDir = panel_nginx_template_dir();

    if ($templateDir === '' || !is_dir($templateDir)) {
        $result['error'] = sprintf(t('找不到 nginx 模板目录：%s'), $templateDir);

        return $result;
    }

    $values = panel_nginx_render_values();
    $result['notes'] = panel_nginx_render_notes();

    foreach (panel_nginx_render_map() as $template => $target) {
        $source = rtrim($templateDir, '/') . '/' . $template;

        if (!is_file($source) || !is_readable($source)) {
            $result['error'] = sprintf(t('模板文件读不到：%s'), $source);

            return $result;
        }

        $raw = @file_get_contents($source);

        if ($raw === false) {
            $result['error'] = sprintf(t('模板文件读取失败：%s'), $source);

            return $result;
        }

        $rendered = panel_nginx_apply_placeholders((string) $raw, $values);

        // 没被用到的跳转文件渲染成一句注释：运维 `cat` 到它的时候不该看到一份
        // "看起来在生效"的 301 规则（它会以为 HTTP 正在跳转，其实没有）。
        if ($target === 'panel-http-redirect.inc' && empty($values['HTTP_REDIRECT_ACTIVE'])) {
            $rendered = "# " . t('本次没有启用「强制 HTTPS」：这个文件没有被 include，面板按上面 HTTP_MODE_INCLUDE 指定的那份走。') . "\n";
        }

        $leftover = panel_nginx_leftover_placeholders($rendered);

        if ($leftover !== []) {
            $result['error'] = sprintf(
                t('模板 %s 里有渲染器不认识的占位符：%s'),
                $template,
                implode(', ', $leftover)
            );

            return $result;
        }

        $targetPath = rtrim($dir, '/') . '/' . $target;

        // 先写 .tmp 再改名：正在跑的 nginx（或者刚好重启的 nginx）永远读不到
        // 半截文件。
        $temp = $targetPath . '.tmp';

        if (@file_put_contents($temp, $rendered) === false) {
            $result['error'] = sprintf(t('写不进配置临时文件：%s'), $temp);

            return $result;
        }

        if (!@rename($temp, $targetPath)) {
            @unlink($temp);
            $result['error'] = sprintf(t('配置改名失败：%s'), $targetPath);

            return $result;
        }

        $result['files'][] = $targetPath;
    }

    $result['ok'] = true;

    return $result;
}

// ============================================================
// 4. 生效（排队给 root 代理）
// ============================================================

/**
 * 排一条"重新渲染 + nginx -t + reload"的请求。
 *
 * @return array{ok:bool,error:string,id:string}
 */
function panel_nginx_apply_request(string $actor = '', string $reason = ''): array
{
    $result = ['ok' => false, 'error' => '', 'id' => ''];

    $queued = panel_agent_request('render', [
        'reason' => $reason,
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
 * 面板上"现在到底是什么状态"：生效目录里的文件、上一次应用结果、代理心跳。
 *
 * @return array<string,mixed>
 */
function panel_nginx_state(bool $refresh = false): array
{
    $dir = panel_nginx_server_dir();
    $main = rtrim($dir, '/') . '/panel-server.conf';
    $locations = rtrim($dir, '/') . '/panel-locations.inc';

    // "上次生成是什么时候"直接看文件的修改时间：配置是原子改名写进来的，
    // mtime 就是那一次渲染的时间（不去解析文件内容，也就不会因为注释格式
    // 变了而失效）。
    $generatedAt = '';
    $stamp = is_file($main) ? @filemtime($main) : false;

    if ($stamp !== false) {
        $generatedAt = date('c', (int) $stamp);
    }

    $listen = panel_listen_config();
    $tls = panel_nginx_tls_state();

    return [
        'dir'          => $dir,
        'template_dir' => panel_nginx_template_dir(),
        'main_file'    => $main,
        'main_exists'  => is_file($main),
        'generated_at' => $generatedAt,
        'writable'     => is_dir($dir) && is_writable($dir),
        'agent'        => panel_agent_heartbeat($refresh),
        'pending'      => panel_agent_pending_count(),
        'last_result'  => panel_agent_last_result(),
        'tls'          => $tls,
        'notes'        => panel_nginx_render_notes(),
        'rescue_url'   => panel_rescue_url(),
        'http_port'    => panel_http_port(),
        'https_port'   => panel_https_port(),
        'listen'       => $listen,
        'locations_exists' => is_file($locations),
    ];
}

// ============================================================
// 5. 域名绑定的 PHP 兜底 + 强制 HTTPS
// ============================================================

/**
 * 每个请求进来时先过这里：Host 不是绑定域名 → 按未认证返回码回话。
 *
 * 这是**第二层**（第一层是 nginx 的 `if ($host !~ ...)`）。留着它的理由：
 *   * 面板可能被别的反代（1Panel / 宝塔 / Cloudflare）挡在前面，那一层不一定
 *     按我们的配置判 Host；
 *   * nginx 配置还没生效（代理没跑 / 刚改完）的那段时间，PHP 这一层仍然守着。
 *
 * 不判的两种情况：
 *   * 急救入口（SERVER_PORT == 急救端口）—— 它就是用来救命的；
 *   * 命令行（PHP_SAPI === 'cli'）—— 没有 Host。
 */
function panel_transport_guard(): void
{
    if (PHP_SAPI === 'cli' || panel_is_rescue_request()) {
        return;
    }

    $domain = panel_bound_domain();

    if ($domain === '') {
        return;
    }

    $host = (string) ($_SERVER['HTTP_HOST'] ?? '');

    if (panel_domain_request_ok($host)) {
        return;
    }

    // 和 nginx 那一层同一个口径：login 在"域名不对"时按 404 处理
    // （把登录页摆给错误域名的访客等于没绑）。
    $code = panel_nginx_guard_code();

    no_cache_headers();
    http_response_code((int) $code);
    echo '';
    exit;
}
