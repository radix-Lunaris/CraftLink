<?php
/**
 * CraftLink 管理面板 —— 面板设置（M2 / M3）。
 *
 * ============================================================================
 *  WHY THIS FILE EXISTS
 * ============================================================================
 *
 * 「面板设置」是一个新页面：root 改所有项，普通管理员默认**看得见但改不了**，
 * 每一项还有一个**只有 root 看得见**的开关 —— 打开之后普通管理员才能改那一条。
 *
 * 三件事必须写在同一个地方，否则一定会走偏：
 *
 *   1. 设置项注册表（键、类型、默认值、校验规则、分组、要不要重启）；
 *   2. 存储：**数据库为主 + 配置文件备份**（D1）。数据库不可用时只读回退到
 *      配置文件，所以"面板打不开设置页"永远不会发生；
 *   3. 权限：`panel_setting_can_edit()` 这一个判定函数，页面（隐藏/禁用控件）
 *      和控制器（真正拦下 POST）都调它 —— 前端隐藏是方便，服务端判定才是权限。
 *
 * 这个文件由 lib/common.php require，和其它共用库一样：不连库、不写文件，
 * 只有被调用时才动手。
 *
 * ============================================================================
 *  [!] 关于内置函数的使用范围（别改成会被门禁判红的写法）
 * ============================================================================
 *
 * 仓库的门禁 tests/panel_static_test.py 维护了一份 PHP 内置函数白名单
 * （"没有调用未定义的函数"）。下面这些**常用但不在白名单里**的函数一律不用：
 *
 *   move_uploaded_file / is_uploaded_file / getimagesize / finfo_* /
 *   filesize / chmod / touch / strtoupper / pathinfo / scandir / unlink 之外的
 *   文件系统调用……
 *
 * logo 上传因此走「读临时文件 → 自己验魔术字节 → file_put_contents 落盘」，
 * 不使用 move_uploaded_file。上传临时名是 PHP 自己生成的随机名，配合
 * `$_FILES[...]['error'] === UPLOAD_ERR_OK` 与大小上限，安全性不比
 * move_uploaded_file 差（类型判定反而更严：只认图片魔术字节）。
 */

declare(strict_types=1);

// ============================================================
// 1. 设置项注册表
// ============================================================

/**
 * 设置分组（页面按这个顺序渲染）。slug 是内部的，label 跟着语言走。
 *
 * 'domain' 是 M4 的域名绑定 + SSL（M3 时这里还只是占位；M4 已经实现，
 * 键名和 M3 文档里留给服务端的那份接口完全一致）。
 */
function panel_setting_groups(): array
{
    return [
        'network'    => t('网络与安全'),
        'session'    => t('会话'),
        'appearance' => t('外观与名称'),
        'legal'      => t('备案与版权'),
        'domain'     => t('域名与 SSL'),
    ];
}

/**
 * 每一条设置：键 => 定义。
 *
 * 字段说明：
 *   group    分组 slug（见 panel_setting_groups()）
 *   type     text / number / select / image —— 决定页面渲染成什么控件
 *   default  默认值（**空字符串也是默认值**，页脚备案号就是"默认空 → 不渲染"）
 *   options  type=select 时的 值 => 文案
 *   help     一句话说明这条设置是干什么的
 *   restart  true = 改完要重启面板容器才生效（现在只有监听地址/端口是）
 *   validate 交给 panel_setting_validate()，这里只存人话说明
 *
 * [!] 键名是**数据**：进了数据库和 config/panel-settings.json，永远不翻译。
 *     文案（label / help / options）才是给页面看的，全部走 t()。
 */
function panel_setting_defs(): array
{
    static $defs = null;

    if ($defs !== null) {
        return $defs;
    }

    $defs = [
        // ---- 网络与安全（M3：自带 nginx 生效）------------------------------
        'listen_ip' => [
            'group'    => 'network',
            'type'     => 'text',
            'default'  => '127.0.0.1',
            'label'    => t('面板监听 IP'),
            'help'     => t('面板自带的 nginx 监听哪个地址：127.0.0.1 = 只让本机反代访问（推荐）；0.0.0.0 = 所有网卡都能直连。'),
            'restart'  => true,
            'validate' => t('合法的 IPv4 / IPv6 地址；不能为空。'),
        ],
        'listen_port' => [
            'group'    => 'network',
            'type'     => 'number',
            'default'  => '19102',
            'min'      => 1,
            'max'      => 65535,
            'label'    => t('面板监听端口'),
            'help'     => t('面板对外提供网页的端口。开放公网前先确认防火墙 / 安全组放行的是这一个。'),
            'restart'  => true,
            'validate' => t('1 - 65535 之间的整数。'),
        ],
        'url_suffix' => [
            'group'    => 'network',
            'type'     => 'text',
            'default'  => '',
            'label'    => t('URL 安全入口后缀'),
            'help'     => t('面板网址后面的秘密路径（例：mclink-panel）。留空 = 不使用安全入口。只允许字母、数字、点、下划线、短横线。'),
            'restart'  => true,
            'validate' => t('只能包含字母、数字、点、下划线、短横线（1 - 64 位），不能带斜杠。'),
        ],
        'unauthenticated_code' => [
            'group'    => 'network',
            'type'     => 'select',
            'default'  => 'login',
            'options'  => [
                'login' => t('显示登录页（当前行为，默认）'),
                '404'   => t('返回 404（找不到页面）'),
                '403'   => t('返回 403（拒绝访问）'),
                '444'   => t('返回 444（直接断开连接，没有响应体）'),
                '200'   => t('返回 200 空页（什么都没有的空白页）'),
            ],
            'label'    => t('未认证时的 HTTP 返回码'),
            'help'     => t('没登录的人访问面板时回什么 —— 用来隐藏面板特征：404 / 403 / 444 / 200 空页都看不出这里有一个管理面板。'),
            'restart'  => false,
            'validate' => t('只能是上面列出的几种之一。'),
        ],

        // ---- 会话 ----------------------------------------------------------
        'session_minutes' => [
            'group'    => 'session',
            'type'     => 'number',
            'default'  => '360',
            'min'      => 5,
            'max'      => 10080,
            'label'    => t('会话时长（分钟）'),
            'help'     => t('登录后多久不操作就自动退出（默认 360 分钟 = 6 小时）。到时间后需要重新登录，已经打开的页面会跳回登录页。'),
            'restart'  => false,
            'validate' => t('5 - 10080 分钟（7 天）之间的整数。'),
        ],

        // ---- 外观与名称 ----------------------------------------------------
        'panel_brand' => [
            'group'    => 'appearance',
            'type'     => 'text',
            'default'  => '',
            'label'    => t('面板左上角名称'),
            'help'     => t('页头左上角显示的名字。留空 = 用 config.php / MCLINK_PANEL_TITLE 里的标题（当前值）。'),
            'restart'  => false,
            'validate' => t('最长 96 字节（约 30 个汉字）；留空表示使用默认标题。'),
        ],
        'panel_tab_title' => [
            'group'    => 'appearance',
            'type'     => 'text',
            'default'  => '',
            'label'    => t('网页标签名'),
            'help'     => t('浏览器标签页 / 收藏夹里显示的名字。留空 = 跟左上角名称一样。'),
            'restart'  => false,
            'validate' => t('最长 96 字节（约 30 个汉字）；留空表示跟左上角名称一致。'),
        ],
        'panel_logo' => [
            'group'    => 'appearance',
            'type'     => 'image',
            'default'  => '',
            'label'    => t('面板 logo'),
            'help'     => t('左上角标记和浏览器标签图标（favicon）用的是同一张图。支持 PNG / JPEG / GIF / WebP / ICO，最大 1 MB；留空 = 内置的草方块标记。'),
            'restart'  => false,
            'validate' => t('只能通过上传更新（不接受 SVG，避免脚本注入）。'),
        ],

        // ---- 备案与版权 ----------------------------------------------------
        'icp_registered' => [
            'group'    => 'legal',
            'type'     => 'select',
            'default'  => '',
            'options'  => [
                ''  => t('自动（填了备案号就显示，默认）'),
                '1' => t('显示备案号'),
                '0' => t('不显示备案号'),
            ],
            'label'    => t('是否已 ICP 备案 / 是否展示备案号'),
            'help'     => t('选「不显示」时页脚连备案号那一块都不渲染 —— 没备案的部署不该出现别人的登记号。'),
            'restart'  => false,
            'validate' => t('只能是「自动 / 显示 / 不显示」之一。'),
        ],
        'icp_number' => [
            'group'    => 'legal',
            'type'     => 'text',
            'default'  => '',
            'label'    => t('ICP 备案号'),
            'help'     => t('填你备案下来的登记号。留空 = 页脚不渲染备案号（默认空）。备案号是纯文本，面板不会给它加任何外链。'),
            'restart'  => false,
            'validate' => t('最长 64 字节；留空表示不显示。'),
        ],

        // ---- 域名与 SSL（M4）-----------------------------------------------
        //
        // 这一组是"面板自己的 HTTPS"：域名绑定 + 上传证书 / 自签 / Let's Encrypt。
        // 改完**不用重启容器**：根代理会重新渲染 nginx 配置并热加载（见
        // panel/lib/panel_nginx.php 与 panel/bin/panel-agent.sh）。
        'bound_domain' => [
            'group'    => 'domain',
            'type'     => 'text',
            'default'  => '',
            'label'    => t('绑定域名'),
            'help'     => t('填了之后**只有这个域名**能打开面板，用 IP 或其他域名访问会按「未认证返回码」处理。可以写 *.example.com 通配子域。留空 = 不限制（任何 Host 都能访问）。'),
            'restart'  => false,
            'validate' => t('只能填主机名（可带最前面的 *.），不要带 http:// 、端口或斜杠；留空表示不绑定。'),
        ],
        'force_https' => [
            'group'    => 'domain',
            'type'     => 'select',
            'default'  => '0',
            'options'  => [
                '0' => t('不强制：HTTP 和 HTTPS 都能打开'),
                '1' => t('强制 HTTPS：HTTP 自动 301 跳转'),
            ],
            'label'    => t('强制 HTTPS'),
            'help'     => t('打开之后，明文 HTTP 的请求会被跳转到 HTTPS（Let’s Encrypt 的校验路径除外，否则续期会失败）。证书没就绪时不会跳转，避免把面板跳到打不开的地址。'),
            'restart'  => false,
            'validate' => t('只能是「不强制 / 强制」之一。'),
        ],
        'ssl_mode' => [
            'group'    => 'domain',
            'type'     => 'select',
            'default'  => 'off',
            'options'  => [
                'off'        => t('不用 HTTPS（默认，和以前一样）'),
                'upload'     => t('上传证书（离线可用，推荐）'),
                'selfsigned' => t('自签证书（只用于测试，浏览器会警告）'),
                'acme'       => t('Let’s Encrypt 自动申请 / 续期'),
            ],
            'label'    => t('SSL 模式'),
            'help'     => t('上传证书不需要域名解析、也不需要 80 端口，离线就能用；Let’s Encrypt 需要域名解析到这台机器、且 80 端口能从公网访问。'),
            'restart'  => false,
            'validate' => t('只能是列出的四种模式之一。'),
        ],
        'ssl_cert_file' => [
            'group'    => 'domain',
            'type'     => 'text',
            'default'  => '',
            'label'    => t('证书文件（PEM）'),
            'help'     => t('容器里的绝对路径。在下面直接上传证书时这一项会自动填上；也可以手填一个已经挂进容器里的路径（例如 certbot 的 fullchain.pem）。'),
            'restart'  => false,
            'validate' => t('必须是容器里的绝对路径（以 / 开头），不能有 .. 或空格。'),
        ],
        'ssl_key_file' => [
            'group'    => 'domain',
            'type'     => 'text',
            'default'  => '',
            'label'    => t('私钥文件（PEM）'),
            'help'     => t('和证书配对的私钥。上传时会自动填上；私钥不能带密码（nginx 不支持加密私钥）。'),
            'restart'  => false,
            'validate' => t('必须是容器里的绝对路径（以 / 开头），不能有 .. 或空格。'),
        ],
        'acme_email' => [
            'group'    => 'domain',
            'type'     => 'text',
            'default'  => '',
            'label'    => t('Let’s Encrypt 账号邮箱'),
            'help'     => t('申请 / 续期用的联系邮箱（到期提醒也发到这里）。只在上面的 SSL 模式选「Let’s Encrypt」时才用得到。'),
            'restart'  => false,
            'validate' => t('要填一个合法邮箱；不用 ACME 时留空即可。'),
        ],
    ];

    return $defs;
}

/**
 * M3 遗留的占位钩子 —— **M4 已经全部实现，这里不再有占位项**。
 *
 * 保留这个函数（返回空数组）只是为了让 M3 时期引用它的代码 / 文档不会
 * 因为函数不存在而报错；新代码请直接用 `panel_setting_defs()`。
 *
 * @return array<string,array<string,mixed>>
 */
function panel_setting_placeholders(): array
{
    return [];
}

/** 一条设置里能被"放行给普通管理员"的键（= 注册表里的全部键）。 */
function panel_setting_keys(): array
{
    return array_keys(panel_setting_defs());
}

/** 每一项设置都有一个「允许普通管理员修改」的开关，键名在这里统一生成。 */
function panel_setting_admin_edit_key(string $key): string
{
    return $key . '__admin_edit';
}

/** 内部键（逐条放行开关）不算"设置项"，页面不把它当普通设置渲染。 */
function panel_setting_is_internal(string $key): bool
{
    return str_ends_with($key, '__admin_edit');
}

/** 认不认识这个键：注册表里的，或者内部开关。 */
function panel_setting_is_known(string $key): bool
{
    return $key !== ''
        && (array_key_exists($key, panel_setting_defs())
            || panel_setting_is_internal($key));
}

/** 给人看的名字（日志 / 提示里用）。 */
function panel_setting_label(string $key): string
{
    if (panel_setting_is_internal($key)) {
        return sprintf(
            t('%s（允许普通管理员修改）'),
            panel_setting_label(substr($key, 0, -12))
        );
    }

    $defs = panel_setting_defs();

    return (string) ($defs[$key]['label'] ?? $key);
}

/** 默认值（注册表里的，不是数据库里的当前值）。 */
function panel_setting_default(string $key): string
{
    $defs = panel_setting_defs();

    return (string) ($defs[$key]['default'] ?? '');
}

// ============================================================
// 2. 校验
// ============================================================

/**
 * 校验一条设置的新值。返回 "" = 合格，否则是给人看的原因。
 *
 * 校验放在服务端：页面上的 min / max / pattern 只是方便，绕过表单直接 POST
 * 一样会被这里挡下来。
 */
function panel_setting_validate(string $key, string $value): string
{
    if (panel_setting_is_internal($key)) {
        return in_array($value, ['0', '1'], true) ? '' : t('开关的值只能是 0 或 1。');
    }

    $defs = panel_setting_defs();

    if (!array_key_exists($key, $defs)) {
        return t('未知的设置项。');
    }

    switch ($key) {
        case 'listen_ip':
            $ip = trim($value);

            if ($ip === '') {
                return t('监听 IP 不能为空：只给本机用就填 127.0.0.1。');
            }

            if (filter_var($ip, FILTER_VALIDATE_IP) === false) {
                return t('监听 IP 不是合法的 IPv4 / IPv6 地址。');
            }

            return '';

        case 'listen_port':
            if (!ctype_digit(trim($value))) {
                return t('监听端口必须是数字。');
            }

            $port = (int) trim($value);

            if ($port < 1 || $port > 65535) {
                return t('监听端口必须在 1 - 65535 之间。');
            }

            return '';

        case 'url_suffix':
            $suffix = trim(trim($value), '/');

            if ($suffix === '') {
                return '';
            }

            if (strlen($suffix) > 64) {
                return t('安全入口后缀最长 64 位。');
            }

            if (preg_match('/^[A-Za-z0-9._-]+$/', $suffix) !== 1) {
                return t('安全入口后缀只能包含字母、数字、点、下划线、短横线，不能有斜杠或空格。');
            }

            return '';

        case 'unauthenticated_code':
            $code = trim($value);

            return array_key_exists($code, (array) $defs[$key]['options'])
                ? ''
                : t('未认证返回码只能是列出的几种之一。');

        case 'session_minutes':
            if (!ctype_digit(trim($value))) {
                return t('会话时长必须是分钟数（整数）。');
            }

            $minutes = (int) trim($value);

            if ($minutes < 5 || $minutes > 10080) {
                return t('会话时长必须在 5 - 10080 分钟之间。');
            }

            return '';

        case 'panel_brand':
        case 'panel_tab_title':
            return strlen(trim($value)) <= 96
                ? ''
                : t('名称最长 96 字节（约 30 个汉字）；留空表示使用默认值。');

        case 'icp_registered':
            return in_array(trim($value), ['', '0', '1'], true)
                ? ''
                : t('备案号显示开关只能是「自动 / 显示 / 不显示」之一。');

        case 'icp_number':
            return strlen(trim($value)) <= 64
                ? ''
                : t('备案号最长 64 字节。');

        case 'panel_logo':
            $name = trim($value);

            if ($name === '') {
                return '';
            }

            return preg_match('/^[A-Za-z0-9._-]{1,80}$/', $name) === 1
                && strpos($name, '..') === false
                ? ''
                : t('logo 只能通过页面上传更新。');

        // ---- 域名与 SSL（M4）----------------------------------------------
        case 'bound_domain':
            return panel_domain_problem($value);

        case 'force_https':
            return in_array(trim($value), ['0', '1'], true)
                ? ''
                : t('强制 HTTPS 只能是「不强制 / 强制」之一。');

        case 'ssl_mode':
            return in_array(trim($value), panel_ssl_modes(), true)
                ? ''
                : t('SSL 模式只能是列出的几种之一。');

        case 'ssl_cert_file':
        case 'ssl_key_file':
            return panel_ssl_path_problem($value);

        case 'acme_email':
            $email = trim($value);

            if ($email === '') {
                return '';
            }

            if (filter_var($email, FILTER_VALIDATE_EMAIL) === false) {
                return t('邮箱格式不对（例：admin@example.com）。');
            }

            return strlen($email) <= 128 ? '' : t('邮箱太长了（最多 128 字节）。');
    }

    return '';
}

/** 存进数据库 / 备份文件之前的规范化（只做无副作用的裁剪）。 */
function panel_setting_normalize(string $key, string $value): string
{
    switch ($key) {
        case 'listen_ip':
        case 'icp_number':
        case 'panel_brand':
        case 'panel_tab_title':
        case 'ssl_cert_file':
        case 'ssl_key_file':
        case 'acme_email':
            return trim($value);

        case 'url_suffix':
            return trim(trim($value), '/');

        // 域名统一小写、去掉协议 / 端口 / 路径（用户经常直接从地址栏复制）。
        case 'bound_domain':
            return panel_domain_normalize($value);

        case 'listen_port':
        case 'session_minutes':
            return trim($value);

        case 'unauthenticated_code':
        case 'icp_registered':
        case 'force_https':
        case 'ssl_mode':
            return trim($value);
    }

    return $value;
}

// ============================================================
// 3. 存储：数据库为主 + 配置文件备份
// ============================================================

/** 面板设置表名（建表语句见 panel_settings_ensure_schema()）。 */
function panel_settings_table(): string
{
    return 'panel_settings';
}

/**
 * 建表（幂等）。升级路径就是这一条：面板第一次读 / 写设置时自动执行
 * `CREATE TABLE IF NOT EXISTS`，不需要单独的迁移脚本，也不会破坏老库。
 *
 *   CREATE TABLE IF NOT EXISTS panel_settings (
 *       name       VARCHAR(64)  NOT NULL,
 *       value      TEXT         NULL,
 *       updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
 *                               ON UPDATE CURRENT_TIMESTAMP,
 *       updated_by VARCHAR(32)  NOT NULL DEFAULT '',
 *       PRIMARY KEY (name)
 *   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
 *
 * @return array{ok:bool,error:string}
 */
function panel_settings_ensure_schema(bool $refresh = false): array
{
    static $result = null;

    if ($refresh) {
        $result = null;
    }

    if ($result !== null) {
        return $result;
    }

    try {
        db()->exec(
            'CREATE TABLE IF NOT EXISTS panel_settings (
                name       VARCHAR(64)  NOT NULL,
                value      TEXT         NULL,
                updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                        ON UPDATE CURRENT_TIMESTAMP,
                updated_by VARCHAR(32)  NOT NULL DEFAULT \'\',
                PRIMARY KEY (name)
             ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci'
        );

        $result = ['ok' => true, 'error' => ''];
    } catch (Throwable $e) {
        // 数据库不可用不是致命错误：读走配置文件回退，写会明确报错。
        $result = ['ok' => false, 'error' => $e->getMessage()];
    }

    return $result;
}

/**
 * 从数据库读全部设置。数据库或表不可用时返回空数组（调用方回退到文件）。
 *
 * @return array<string,string>
 */
function panel_settings_db_read(): array
{
    if (!panel_settings_ensure_schema()['ok']) {
        return [];
    }

    try {
        $statement = db()->query(
            'SELECT name, value FROM ' . panel_settings_table()
        );
        $rows = $statement->fetchAll();
    } catch (Throwable $e) {
        return [];
    }

    $values = [];

    foreach ($rows as $row) {
        $name = (string) ($row['name'] ?? '');

        if ($name === '') {
            continue;
        }

        $values[$name] = (string) ($row['value'] ?? '');
    }

    return $values;
}

// ============================================================
// 3b. 急救覆盖（root 管理台在数据库写不进去时用）
// ============================================================

/**
 * 急救覆盖文件的候选路径（和配置备份同一个目录，文件名固定）。
 *
 * 它是什么、为什么需要：面板设置以数据库为主，但有一种很难受的情况 ——
 * **数据库能读、写不进去**（只读账号 / 磁盘满 / 表被锁），这时候
 * `mclink reset-entry` 会"执行成功但没生效"，人就被自己的安全入口关在外面了。
 * 所以 root 的 mclink 管理台在写库失败时会退一步，把"这一条"写进这个文件；
 * 它的优先级**高于数据库**，面板每次读设置都会先看它。
 *
 * 三条约束（都在代码里落实了）：
 *   1. 只有 root 的 CLI 会写它 —— 网页进程永远不写；
 *   2. 它的值**不会**被写回 panel-settings.json（否则一次急救就变永久了）；
 *   3. 面板设置页上会显眼地列出"当前有急救覆盖"，root 点一下就能清掉。
 *
 * @return array<int,string>
 */
function panel_settings_override_file_candidates(): array
{
    $candidates = [];

    $env = getenv('MCLINK_PANEL_OVERRIDES_FILE');

    if ($env !== false && trim((string) $env) !== '') {
        $candidates[] = trim((string) $env);
    }

    $config = config('panel.overrides_file');

    if (is_string($config) && trim($config) !== '') {
        $candidates[] = trim($config);
    }

    $dirs = [];

    $envDir = getenv('MCLINK_CONFIG_DIR');

    if ($envDir !== false && trim((string) $env) !== '') {
        $dirs[] = rtrim(trim((string) $envDir), '/');
    }

    $root = getenv('MCLINK_ROOT');

    if ($root !== false && trim((string) $root) !== '') {
        $dirs[] = rtrim(trim((string) $root), '/') . '/config';
    }

    $dirs[] = '/var/www/config';
    $dirs[] = '/opt/mc-link/config';
    $dirs[] = dirname(__DIR__) . '/config';
    $dirs[] = dirname(__DIR__, 2) . '/config';

    foreach (array_unique($dirs) as $dir) {
        $candidates[] = rtrim($dir, '/') . '/panel-emergency-overrides.json';
    }

    return array_values(array_unique($candidates));
}

/**
 * 读急救覆盖。
 *
 * @return array{path:string,values:array<string,string>,exists:bool,updated_at:string,updated_by:string}
 */
function panel_settings_overrides(bool $refresh = false): array
{
    static $cache = null;

    if ($refresh) {
        $cache = null;
    }

    if ($cache !== null) {
        return $cache;
    }

    $empty = [
        'path'       => panel_settings_override_file_candidates()[0] ?? '',
        'values'     => [],
        'exists'     => false,
        'updated_at' => '',
        'updated_by' => '',
    ];

    foreach (panel_settings_override_file_candidates() as $path) {
        if (!is_file($path) || !is_readable($path)) {
            continue;
        }

        $raw = @file_get_contents($path);

        if ($raw === false || trim((string) $raw) === '') {
            continue;
        }

        $decoded = json_decode((string) $raw, true);

        if (!is_array($decoded)) {
            continue;
        }

        $values = [];

        if (is_array($decoded['settings'] ?? null)) {
            foreach ($decoded['settings'] as $name => $value) {
                if (is_string($name) && $name !== ''
                    && (is_string($value) || is_numeric($value))) {
                    $values[$name] = (string) $value;
                }
            }
        }

        $cache = [
            'path'       => $path,
            'values'     => $values,
            'exists'     => $values !== [],
            'updated_at' => (string) ($decoded['updated_at'] ?? ''),
            'updated_by' => (string) ($decoded['updated_by'] ?? ''),
        ];

        return $cache;
    }

    $cache = $empty;

    return $cache;
}

/** 急救覆盖要写到哪个文件（优先已经存在的那个，否则第一个目录可写的）。 */
function panel_settings_override_write_path(): string
{
    $candidates = panel_settings_override_file_candidates();

    foreach ($candidates as $path) {
        if (is_file($path)) {
            return $path;
        }
    }

    foreach ($candidates as $path) {
        $dir = dirname($path);

        if (is_dir($dir) && is_writable($dir)) {
            return $path;
        }
    }

    foreach ($candidates as $path) {
        $dir = dirname($path);

        if (is_dir($dir)) {
            continue;
        }

        if (@mkdir($dir, 0750, true)) {
            return $path;
        }
    }

    return (string) ($candidates[0] ?? '');
}

/**
 * 写急救覆盖（**只给 root 的 CLI 用**，网页进程不调用它）。
 *
 * @param array<string,string> $changes 键 => 值（'' 也是有效值）
 * @return array{ok:bool,error:string,path:string,keys:array<int,string>}
 */
function panel_settings_overrides_write(array $changes, string $actor = ''): array
{
    $path = panel_settings_override_write_path();

    if ($path === '') {
        return ['ok' => false, 'error' => t('没有可用的急救覆盖文件路径。'), 'path' => '', 'keys' => []];
    }

    $current = panel_settings_overrides(true);
    $values = $current['values'];
    $written = [];

    foreach ($changes as $key => $value) {
        if (!is_string($key) || $key === '' || !panel_setting_is_known($key)) {
            continue;
        }

        $values[$key] = (string) $value;
        $written[] = $key;
    }

    if ($written === []) {
        return ['ok' => false, 'error' => t('没有可写的设置项。'), 'path' => $path, 'keys' => []];
    }

    $json = json_encode([
        'schema'     => 1,
        'note'       => t('由 mclink 管理台写入的急救覆盖：优先级高于数据库，面板设置页可以清除。'),
        'updated_at' => date('c'),
        'updated_by' => $actor,
        'settings'   => $values,
    ], JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

    if (!is_string($json)) {
        return ['ok' => false, 'error' => t('急救覆盖无法序列化成 JSON。'), 'path' => $path, 'keys' => []];
    }

    $dir = dirname($path);

    if (!is_dir($dir) && !@mkdir($dir, 0750, true)) {
        return [
            'ok'    => false,
            'error' => sprintf(t('目录不存在，而且创建失败：%s'), $dir),
            'path'  => $path,
            'keys'  => [],
        ];
    }

    $temp = $path . '.tmp';

    if (@file_put_contents($temp, $json . "\n") === false) {
        return [
            'ok'    => false,
            'error' => sprintf(t('写不进临时文件：%s'), $temp),
            'path'  => $path,
            'keys'  => [],
        ];
    }

    if (!@rename($temp, $path)) {
        @unlink($temp);

        return [
            'ok'    => false,
            'error' => sprintf(t('无法把临时文件改名成：%s'), $path),
            'path'  => $path,
            'keys'  => [],
        ];
    }

    panel_settings_overrides(true);
    panel_settings_all(true);

    return ['ok' => true, 'error' => '', 'path' => $path, 'keys' => $written];
}

/**
 * 清掉急救覆盖（全部，或者指定的几个键）。文件里没有键了就删掉这个文件。
 *
 * @param array<int,string> $keys
 * @return array{ok:bool,error:string,removed:array<int,string>,path:string}
 */
function panel_settings_overrides_clear(array $keys = [], string $actor = ''): array
{
    $current = panel_settings_overrides(true);
    $path = $current['path'];

    if ($path === '' || !is_file($path)) {
        return ['ok' => true, 'error' => '', 'removed' => [], 'path' => $path];
    }

    $values = $current['values'];
    $removed = [];

    if ($keys === []) {
        $removed = array_keys($values);
        $values = [];
    } else {
        foreach ($keys as $key) {
            if (array_key_exists($key, $values)) {
                $removed[] = $key;
                unset($values[$key]);
            }
        }
    }

    if ($values === []) {
        @unlink($path);
        panel_settings_overrides(true);
        panel_settings_all(true);

        return ['ok' => true, 'error' => '', 'removed' => $removed, 'path' => $path];
    }

    $result = panel_settings_overrides_write($values, $actor);

    return [
        'ok'      => $result['ok'],
        'error'   => $result['error'],
        'removed' => $removed,
        'path'    => $path,
    ];
}

/**
 * 配置文件（D1 的"备份"那半边）的候选路径。
 *
 * [!] 绝不写死 /opt/mc-link：环境变量 / config.php 优先，`/opt/mc-link/config`
 *     只是**默认值**（D8），而且排在容器约定的挂载点之后。
 */
function panel_settings_file_candidates(): array
{
    $candidates = [];

    $envFile = getenv('MCLINK_PANEL_SETTINGS_FILE');

    if ($envFile !== false && trim((string) $envFile) !== '') {
        $candidates[] = trim((string) $envFile);
    }

    $configFile = config('panel.settings_file');

    if (is_string($configFile) && trim($configFile) !== '') {
        $candidates[] = trim($configFile);
    }

    $dirs = [];

    $envDir = getenv('MCLINK_CONFIG_DIR');

    if ($envDir !== false && trim((string) $envDir) !== '') {
        $dirs[] = rtrim(trim((string) $envDir), '/');
    }

    $root = getenv('MCLINK_ROOT');

    if ($root !== false && trim((string) $root) !== '') {
        $dirs[] = rtrim(trim((string) $root), '/') . '/config';
    }

    // 容器里推荐的挂载点（见 panel/docker-compose.yml 的说明）。
    $dirs[] = '/var/www/config';
    // D8：安装根目录的默认值，只是默认值。
    $dirs[] = '/opt/mc-link/config';
    // 仓库 / 发布包布局：panel/config 与 <安装根>/config。
    $dirs[] = dirname(__DIR__) . '/config';
    $dirs[] = dirname(__DIR__, 2) . '/config';

    foreach ($dirs as $dir) {
        $candidates[] = rtrim($dir, '/') . '/panel-settings.json';
    }

    return array_values(array_unique($candidates));
}

/**
 * 读配置文件备份。
 *
 * @return array{path:string,values:array<string,string>,readable:bool}
 */
function panel_settings_file(bool $refresh = false): array
{
    static $cache = null;

    if ($refresh) {
        $cache = null;
    }

    if ($cache !== null) {
        return $cache;
    }

    foreach (panel_settings_file_candidates() as $path) {
        if (!is_file($path) || !is_readable($path)) {
            continue;
        }

        $raw = @file_get_contents($path);

        if ($raw === false || trim((string) $raw) === '') {
            continue;
        }

        $decoded = json_decode((string) $raw, true);

        if (!is_array($decoded)) {
            continue;
        }

        $values = [];

        if (is_array($decoded['settings'] ?? null)) {
            foreach ($decoded['settings'] as $name => $value) {
                if (is_string($name) && $name !== ''
                    && (is_string($value) || is_numeric($value))) {
                    $values[$name] = (string) $value;
                }
            }
        }

        $cache = ['path' => $path, 'values' => $values, 'readable' => true];

        return $cache;
    }

    $cache = [
        'path'     => panel_settings_write_path(),
        'values'   => [],
        'readable' => false,
    ];

    return $cache;
}

/**
 * 要写进哪个文件：优先"已经存在的那一个"（保持部署时选定的位置），
 * 其次是第一个目录存在且可写的候选，最后才尝试建目录。
 */
function panel_settings_write_path(): string
{
    static $chosen = null;

    if ($chosen !== null) {
        return $chosen;
    }

    $candidates = panel_settings_file_candidates();

    foreach ($candidates as $path) {
        if (is_file($path)) {
            $chosen = $path;
            return $chosen;
        }
    }

    foreach ($candidates as $path) {
        $dir = dirname($path);

        if (is_dir($dir) && is_writable($dir)) {
            $chosen = $path;
            return $chosen;
        }
    }

    foreach ($candidates as $path) {
        $dir = dirname($path);

        if (is_dir($dir)) {
            continue;
        }

        if (@mkdir($dir, 0750, true)) {
            $chosen = $path;
            return $chosen;
        }
    }

    $chosen = (string) ($candidates[0] ?? '');

    return $chosen;
}

/**
 * 当前生效的全部设置：**文件备份打底，数据库覆盖**（D1：数据库为主）。
 *
 * 这样一份从没有数据库的老部署把 config/panel-settings.json 挂进来时也能
 * 直接读出设置；数据库恢复之后，库里的值优先。
 *
 * [!] 急救覆盖（panel-emergency-overrides.json）**不算**在 values 里：
 *     它由 root 的 mclink 管理台在"数据库写不进去"时写，优先级高于数据库
 *     （见 panel_setting()），但不会被写回配置备份文件 —— 否则一次急救就
 *     把临时值变成永久值了。
 *
 * @return array{values:array<string,string>,db_rows:int,db_ok:bool,file_path:string,
 *               file_readable:bool,overrides:array<string,string>,overrides_path:string}
 */
function panel_settings_all(bool $refresh = false): array
{
    static $cache = null;

    if ($refresh) {
        $cache = null;
    }

    if ($cache !== null) {
        return $cache;
    }

    $file = panel_settings_file($refresh);
    $values = $file['values'];
    $db = panel_settings_db_read();

    foreach ($db as $name => $value) {
        $values[$name] = $value;
    }

    $overrides = panel_settings_overrides($refresh);

    $cache = [
        'values'         => $values,
        'db_rows'        => count($db),
        'db_ok'          => panel_settings_ensure_schema()['ok'],
        'file_path'      => $file['path'],
        'file_readable'  => $file['readable'],
        'overrides'      => $overrides['values'],
        'overrides_path' => $overrides['path'],
    ];

    return $cache;
}

/**
 * 一条设置的当前值（**急救覆盖 > 数据库 / 文件 > 注册表默认值**）。
 *
 * 注意：显式存进去的空字符串是有效值（"把备案号清空"就是空），所以判空不能
 * 用 empty()，只能看键在不在。
 */
function panel_setting(string $key, ?string $fallback = null): string
{
    $all = panel_settings_all();

    if (array_key_exists($key, $all['overrides'])) {
        return (string) $all['overrides'][$key];
    }

    if (array_key_exists($key, $all['values'])) {
        return (string) $all['values'][$key];
    }

    if (array_key_exists($key, panel_setting_defs())) {
        return panel_setting_default($key);
    }

    return $fallback === null ? '' : (string) $fallback;
}

function panel_setting_int(string $key, int $fallback): int
{
    $value = trim(panel_setting($key, (string) $fallback));

    return is_numeric($value) ? (int) $value : $fallback;
}

function panel_setting_bool(string $key, bool $fallback = false): bool
{
    $value = strtolower(trim(panel_setting($key, $fallback ? '1' : '0')));

    if ($value === '1' || $value === 'true' || $value === 'on') {
        return true;
    }

    if ($value === '0' || $value === 'false' || $value === 'off' || $value === '') {
        return false;
    }

    return $fallback;
}

/** 「这条设置」的当前值 + 是否被显式设置过（页面要显示"默认 / 已改"）。 */
function panel_setting_is_custom(string $key): bool
{
    $all = panel_settings_all();

    return array_key_exists($key, $all['values']);
}

/**
 * 写入一条设置：数据库为主，成功之后**再写一份配置文件备份**（D1）。
 *
 * 数据库写不进去时**不写文件**、原样报错：否则会出现"库里的旧值"和
 * "文件里的新值"两份真相，下次读的时候库又把文件盖回去。
 *
 * @return array{ok:bool,error:string,warning:string,value:string}
 */
function panel_setting_save(string $key, string $value, string $actor = ''): array
{
    $result = ['ok' => false, 'error' => '', 'warning' => '', 'value' => ''];

    if (!panel_setting_is_known($key)) {
        $result['error'] = t('未知的设置项。');

        return $result;
    }

    $value = panel_setting_normalize($key, $value);
    $problem = panel_setting_validate($key, $value);

    if ($problem !== '') {
        $result['error'] = $problem;

        return $result;
    }

    $schema = panel_settings_ensure_schema(true);

    if (!$schema['ok']) {
        $result['error'] = sprintf(
            t('无法写入数据库（面板设置以数据库为准）：%s'),
            $schema['error']
        );

        return $result;
    }

    try {
        $statement = db()->prepare(
            'INSERT INTO ' . panel_settings_table() . '
                (name, value, updated_by)
             VALUES (?, ?, ?)
             ON DUPLICATE KEY UPDATE
                value = VALUES(value), updated_by = VALUES(updated_by)'
        );

        $statement->execute([$key, $value, substr($actor, 0, 32)]);
    } catch (Throwable $e) {
        $result['error'] = sprintf(
            t('无法写入数据库（面板设置以数据库为准）：%s'),
            $e->getMessage()
        );

        return $result;
    }

    // 缓存里那一份已经过期了：下一次 panel_setting() 必须读到新值。
    panel_settings_all(true);

    // 这一条如果之前被"急救覆盖"钉住过，现在数据库里已经有正式值了 → 把覆盖
    // 撤掉，否则设置页上改了不生效（覆盖优先级高于数据库）。
    $overrides = panel_settings_overrides(true);

    if (array_key_exists($key, $overrides['values'])) {
        panel_settings_overrides_clear([$key], $actor);
        panel_settings_all(true);
    }

    $backup = panel_settings_write_backup($actor);

    if (!$backup['ok']) {
        $result['warning'] = sprintf(
            t('设置已写入数据库，但配置文件备份写入失败：%s'),
            $backup['error']
        );
    }

    $result['ok'] = true;
    $result['value'] = $value;

    return $result;
}

/**
 * 把当前全部设置写成配置文件备份（原子写：先写 .tmp 再 rename）。
 *
 * @return array{ok:bool,error:string,path:string}
 */
function panel_settings_write_backup(string $actor = ''): array
{
    $path = panel_settings_write_path();

    if ($path === '') {
        return ['ok' => false, 'error' => t('没有可用的配置文件路径。'), 'path' => ''];
    }

    $all = panel_settings_all(true);
    $stored = [];

    foreach ($all['values'] as $name => $value) {
        if (panel_setting_is_known((string) $name)) {
            $stored[(string) $name] = (string) $value;
        }
    }

    $payload = [
        'schema'        => 1,
        'panel_version' => PANEL_VERSION,
        'updated_at'    => date('c'),
        'updated_by'    => $actor !== '' ? $actor : (string) ((current_user() ?? [])['username'] ?? ''),
        'settings'      => $stored,
    ];

    $json = json_encode(
        $payload,
        JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    );

    if (!is_string($json)) {
        return ['ok' => false, 'error' => t('设置无法序列化成 JSON。'), 'path' => $path];
    }

    $dir = dirname($path);

    if (!is_dir($dir) && !@mkdir($dir, 0750, true)) {
        return [
            'ok'    => false,
            'error' => sprintf(t('目录不存在，而且创建失败：%s'), $dir),
            'path'  => $path,
        ];
    }

    $temp = $path . '.tmp';

    if (@file_put_contents($temp, $json . "\n") === false) {
        return [
            'ok'    => false,
            'error' => sprintf(t('写不进临时文件：%s'), $temp),
            'path'  => $path,
        ];
    }

    if (!@rename($temp, $path)) {
        @unlink($temp);

        return [
            'ok'    => false,
            'error' => sprintf(t('无法把临时文件改名成：%s'), $path),
            'path'  => $path,
        ];
    }

    panel_settings_file(true);

    return ['ok' => true, 'error' => '', 'path' => $path];
}

/**
 * 从配置文件把这些设置导入数据库（DB 是主，文件是备份 / 迁移来源）。
 *
 * @return array{ok:bool,imported:int,error:string}
 */
function panel_settings_import_file(string $actor = ''): array
{
    $file = panel_settings_file(true);

    if ($file['values'] === []) {
        return [
            'ok'       => false,
            'imported' => 0,
            'error'    => t('配置文件里没有可导入的设置（或者文件不存在）。'),
        ];
    }

    $imported = 0;

    foreach ($file['values'] as $name => $value) {
        if (!panel_setting_is_known((string) $name)) {
            continue;
        }

        $saved = panel_setting_save((string) $name, (string) $value, $actor);

        if ($saved['ok']) {
            $imported++;
        }
    }

    return ['ok' => true, 'imported' => $imported, 'error' => ''];
}

/**
 * 设置页要显示的存储状态（数据库能不能用、备份文件在哪、读过没有）。
 *
 * @return array{values:array<string,string>,db_ok:bool,db_error:string,db_rows:int,
 *               file_path:string,file_exists:bool,file_writable:bool,
 *               overrides:array<string,string>,overrides_path:string,overrides_at:string}
 */
function panel_settings_state(): array
{
    $schema = panel_settings_ensure_schema(true);
    $all = panel_settings_all(true);
    $file = panel_settings_file(true);
    $dir = dirname($file['path']);
    $overrides = panel_settings_overrides(true);

    return [
        'values'         => $all['values'],
        'db_ok'          => $schema['ok'],
        'db_error'       => $schema['error'],
        'db_rows'        => $all['db_rows'],
        'file_path'      => $file['path'],
        'file_exists'    => $file['readable'],
        'file_writable'  => is_dir($dir) && is_writable($dir),
        'overrides'      => $overrides['values'],
        'overrides_path' => $overrides['path'],
        'overrides_at'   => $overrides['updated_at'],
    ];
}

// ============================================================
// 4. 权限（服务端强制）
// ============================================================
//
// 判定矩阵（每一条设置 × 角色 × 那条设置的 root 开关）：
//
//   角色     root 开关   能不能改   能不能看到那条设置   能不能拨 root 开关
//   root     开/关       能         能                   能
//   admin    开          能         能                   不能
//   admin    关（默认）  不能       能（只读，控件禁用） 不能
//   player   —           不能       不能（整页 403）     不能
//
// 页面上把控件禁用 + 不渲染 root 开关，是"方便"；真正拦下 POST 的是
// require_panel_setting_edit()（见 web/index.php 的 case 'panel_settings_save'）。

/** 那条「允许普通管理员修改」的开关是不是开着（默认关）。 */
function panel_setting_admin_edit_enabled(string $key): bool
{
    return panel_setting(panel_setting_admin_edit_key($key), '0') === '1';
}

/**
 * 这条设置的唯一判定函数：当前角色能不能改它。
 *
 * $role 传 null 时取当前登录角色（和页面上的按钮、控制器里的判断完全同源）。
 */
function panel_setting_can_edit(string $key, ?string $role = null): bool
{
    $role = $role ?? current_role();

    if ($role === ROLE_ROOT) {
        return true;   // root 永远能改（包括改那个放行开关）
    }

    if ($role === ROLE_ADMIN) {
        return panel_setting_admin_edit_enabled($key);
    }

    return false;      // player：只读，且根本进不了设置页
}

/** 谁能拨「允许普通管理员修改」的开关：只有 root。 */
function panel_setting_can_toggle_admin_edit(string $key, ?string $role = null): bool
{
    return ($role ?? current_role()) === ROLE_ROOT;
}

/** 谁能看到设置页：root + 普通管理员（玩家看不到）。 */
function panel_setting_page_visible(?string $role = null): bool
{
    return is_role_staff($role ?? current_role());
}

/**
 * 服务端闸门：改这条设置之前必须过这里。
 *
 * 没过 = 403 + 一句人话 + 跳回设置页（按钮本来是禁用的，直接 POST 才会走到）。
 */
function require_panel_setting_edit(string $key): void
{
    if (panel_setting_can_edit($key)) {
        return;
    }

    http_response_code(403);

    flash(
        sprintf(
            t('「%s」目前只允许根管理员修改。需要普通管理员也能改的话，请根管理员在这一项上打开放行开关。'),
            panel_setting_label($key)
        ),
        'error'
    );

    redirect('index.php?action=settings');
}

// ============================================================
// 5. 给页面用的"生效值"
// ============================================================

/**
 * 左上角名称：设置 > config.php / MCLINK_PANEL_TITLE > 内置默认值。
 *
 * 留空**不是**"显示空标题"：留空 = 用部署里原来的标题，这样默认值和以前
 * 一模一样（向后兼容），改名只需要填一下设置项。
 */
function panel_brand_name(): string
{
    $value = trim(panel_setting('panel_brand', ''));

    if ($value !== '') {
        return $value;
    }

    $config = trim((string) (config('panel.title') ?? ''));

    return $config !== '' ? $config : PANEL_DEFAULT_TITLE;
}

/** 网页标签名：设置 > 左上角名称。 */
function panel_tab_title(): string
{
    $value = trim(panel_setting('panel_tab_title', ''));

    return $value !== '' ? $value : panel_brand_name();
}

/** 页脚要显示的备案号（设置 > config.php / MCLINK_PANEL_ICP > 空）。 */
function panel_icp_number(): string
{
    $value = trim(panel_setting('icp_number', ''));

    if ($value !== '') {
        return $value;
    }

    return trim((string) (config('panel.icp') ?? ''));
}

/**
 * 页脚要不要渲染备案号。
 *
 * 「自动（默认）」的语义：**填了备案号才显示**（老部署由 config / 环境变量
 * 提供号码，行为不变）；没填 = 空 → 页脚那一块整个不渲染（M1 的要求）。
 */
function panel_icp_enabled(): bool
{
    $flag = trim(panel_setting('icp_registered', ''));

    if ($flag === '1') {
        return true;
    }

    if ($flag === '0') {
        return false;
    }

    return panel_icp_number() !== '';
}

/**
 * 页面上「当前生效：…」那一行字。
 *
 * 设置留空 = 回退到 config.php / 环境变量 / 内置默认值，而回退之后**到底
 * 是什么值**在页面上看不见的话，验收就只能靠猜（"我明明清空了备案号，
 * 页脚为什么还有字？"）。所以这里把生效值直接写出来。
 */
function panel_setting_effective_note(string $key): string
{
    switch ($key) {
        case 'panel_brand':
            return sprintf(
                t('当前生效：%s（设置留空时用 config.php / MCLINK_PANEL_TITLE）'),
                panel_brand_name()
            );

        case 'panel_tab_title':
            return sprintf(t('当前生效：%s'), panel_tab_title());

        case 'icp_number':
            return panel_icp_number() === ''
                ? t('当前生效：空（页脚不渲染备案号）')
                : sprintf(t('当前生效：%s'), panel_icp_number());

        case 'icp_registered':
            return panel_icp_enabled()
                ? t('当前生效：页脚会显示备案号')
                : t('当前生效：页脚不显示备案号');

        case 'session_minutes':
            return sprintf(
                t('当前生效：%d 分钟'),
                intdiv(panel_session_lifetime_seconds(), 60)
            );

        case 'bound_domain':
            $domain = panel_bound_domain();

            return $domain === ''
                ? t('当前生效：没有绑定域名（任何 Host 都能访问面板）。')
                : sprintf(t('当前生效：只允许 %s 访问（其他 Host 按未认证返回码处理）。'), $domain);

        case 'force_https':
            $tls = panel_nginx_tls_state();

            if (!panel_force_https()) {
                return t('当前生效：HTTP 和 HTTPS 都能打开。');
            }

            return $tls['force_https']
                ? t('当前生效：明文 HTTP 会 301 跳到 HTTPS。')
                : t('当前生效：开关是打开的，但证书还没就绪 —— 暂时不会跳转（避免面板打不开）。');

        case 'ssl_mode':
            return panel_ssl_effective_note();

        case 'ssl_cert_file':
        case 'ssl_key_file':
            $path = $key === 'ssl_cert_file' ? panel_ssl_cert_file() : panel_ssl_key_file();

            if ($path === '') {
                return t('当前生效：还没设置路径。');
            }

            return panel_ssl_path_in_cert_dir($path)
                ? sprintf(t('当前生效：%s（在面板证书目录里）'), $path)
                : sprintf(t('当前生效：%s（在面板证书目录之外 —— 要确认这个路径已经挂进容器，否则 nginx 读不到）'), $path);

        case 'acme_email':
            return panel_acme_email() === ''
                ? t('当前生效：没填邮箱（只有用 Let’s Encrypt 时才需要）。')
                : sprintf(t('当前生效：%s'), panel_acme_email());
    }

    return '';
}

/**
 * 面板要监听什么 —— 自带 nginx（M3）和"去 1Panel 依赖"的配置读取层。
 *
 * 这里**只读 + 校验**，不生成 nginx 配置、不改容器：真正的模板改动在服务端
 * 工作流（接口见 docs/PANEL-SETTINGS.md）。返回值就是那个接口的数据形状：
 *
 *   ip / port       监听地址
 *   entry_suffix    安全入口后缀（不带斜杠；'' = 不用安全入口）
 *   entry_path      带前导斜杠的形式（'' 或 '/xxx'）
 *   unauth_code     未认证时的返回码：login / 404 / 403 / 444 / 200
 *   nginx_listen    nginx 模板里 `listen` 那一行的值
 *
 * @return array{ip:string,port:int,entry_suffix:string,entry_path:string,
 *               unauth_code:string,nginx_listen:string}
 */
function panel_listen_config(): array
{
    $ip = trim(panel_setting('listen_ip', '127.0.0.1'));

    if ($ip === '' || filter_var($ip, FILTER_VALIDATE_IP) === false) {
        $ip = '127.0.0.1';
    }

    $port = panel_setting_int('listen_port', 19102);

    if ($port < 1 || $port > 65535) {
        $port = 19102;
    }

    $suffix = trim(trim(panel_setting('url_suffix', '')), '/');

    if ($suffix !== '' && preg_match('/^[A-Za-z0-9._-]{1,64}$/', $suffix) !== 1) {
        $suffix = '';
    }

    $code = trim(panel_setting('unauthenticated_code', 'login'));

    if (!in_array($code, ['login', '404', '403', '444', '200'], true)) {
        $code = 'login';
    }

    return [
        'ip'           => $ip,
        'port'         => $port,
        'entry_suffix' => $suffix,
        'entry_path'   => $suffix === '' ? '' : '/' . $suffix,
        'unauth_code'  => $code,
        'nginx_listen' => $ip . ':' . $port,
    ];
}

// ============================================================
// 6. 未认证时的响应（隐藏面板特征）
// ============================================================

/** 配置里的未认证返回码（认不出的值一律回退到"显示登录页"）。 */
function panel_unauthenticated_code(): string
{
    return panel_listen_config()['unauth_code'];
}

/**
 * 未登录的人访问受保护页面时怎么回话。
 *
 *   login（默认）  302 到登录页 —— 和以前一模一样
 *   404 / 403      只发状态码 + 空正文（看不出这是一个管理面板）
 *   444            发 444；真正"直接断连接"由自带 nginx（M3）完成，
 *                  PHP 这边把状态码原样发出去、正文为空
 *   200            200 空页
 *
 * 登录页本身（index.php?action=login / login.php）**不走这里** —— 否则配了
 * 隐藏码之后谁都登不进来了。安全入口后缀（url_suffix）是隐藏入口的正解，
 * 由自带 nginx 按上面的配置执行。
 */
function panel_unauthenticated_reply(): void
{
    $code = panel_unauthenticated_code();

    if ($code === 'login') {
        redirect('index.php?action=login');
    }

    no_cache_headers();

    if ($code === '444' && !headers_sent()) {
        header('Connection: close');
    }

    http_response_code((int) $code);
    echo '';
    exit;
}

// ============================================================
// 7. logo：上传 / 保存 / 输出
// ============================================================
//
// 一张图同时做左上角标记和 favicon，所以必须有一个"永远取得到"的地址：
// 面板用 index.php?action=logo 输出（不需要 nginx 额外配置，也不要求上传
// 目录能被 HTTP 直接访问）。
//
// 存放位置按"用户数据不随发布包替换"（D9）优先：环境变量 / config.php 指定的
// 目录 → <安装根>/data/panel-uploads → 容器里的 /var/www/data/panel-uploads
// → 网页根目录下的 assets/uploads（兜底，一定能写）。

/** logo 上传目录的候选（写入时取第一个可写的）。 */
function panel_logo_dir_candidates(): array
{
    $candidates = [];

    $envDir = getenv('MCLINK_PANEL_UPLOAD_DIR');

    if ($envDir !== false && trim((string) $envDir) !== '') {
        $candidates[] = rtrim(trim((string) $envDir), '/');
    }

    $configDir = config('panel.upload_dir');

    if (is_string($configDir) && trim($configDir) !== '') {
        $candidates[] = rtrim(trim($configDir), '/');
    }

    $root = getenv('MCLINK_ROOT');

    if ($root !== false && trim((string) $root) !== '') {
        $candidates[] = rtrim(trim((string) $root), '/') . '/data/panel-uploads';
    }

    $candidates[] = '/var/www/data/panel-uploads';   // 容器里推荐的挂载点
    $candidates[] = '/opt/mc-link/data/panel-uploads'; // D8 的默认安装根（只是默认值）
    $candidates[] = dirname(__DIR__, 2) . '/data/panel-uploads';  // 仓库布局
    $candidates[] = __DIR__ . '/../web/assets/uploads';           // 兜底：容器里一定能写

    return array_values(array_unique($candidates));
}

/** 实际使用的上传目录（$forWrite=true 时挑第一个可写的，必要时建目录）。 */
function panel_logo_dir(bool $forWrite = false): string
{
    static $chosen = null;

    if ($chosen !== null) {
        return $chosen;
    }

    $candidates = panel_logo_dir_candidates();

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

/** 设置里记着的 logo 文件名（过一遍白名单，防止路径穿越）。 */
function panel_logo_filename(): string
{
    $name = trim(panel_setting('panel_logo', ''));

    if ($name === ''
        || preg_match('/^[A-Za-z0-9._-]{1,80}$/', $name) !== 1
        || strpos($name, '..') !== false) {
        return '';
    }

    return $name;
}

/** logo 文件在磁盘上的完整路径；没设置 / 文件不在时返回 ''。 */
function panel_logo_path(): string
{
    static $cache = null;

    if ($cache !== null) {
        return $cache;
    }

    $name = panel_logo_filename();

    if ($name === '') {
        $cache = '';
        return $cache;
    }

    foreach (panel_logo_dir_candidates() as $dir) {
        $path = rtrim($dir, '/') . '/' . $name;

        if (is_file($path)) {
            $cache = $path;
            return $cache;
        }
    }

    $cache = '';

    return $cache;
}

/** 页面上用的 logo 地址；没上传时返回 ''（调用方退回内置草方块）。 */
function panel_logo_url(): string
{
    $path = panel_logo_path();

    if ($path === '') {
        return '';
    }

    $stamp = @filemtime($path);

    return 'index.php?action=logo&v=' . ($stamp === false ? '1' : (string) $stamp);
}

/**
 * 图片类型识别：只看魔术字节，不依赖 getimagesize / finfo（门禁白名单里没有，
 * 而且魔术字节比扩展名可信）。SVG 刻意不支持：它内嵌脚本。
 *
 * @return array{ext:string,mime:string}|array{}
 */
function panel_image_type(string $data): array
{
    $hex = bin2hex(substr($data, 0, 16));

    if (str_starts_with($hex, '89504e470d0a1a0a')) {
        return ['ext' => 'png', 'mime' => 'image/png'];
    }

    if (str_starts_with($hex, 'ffd8ff')) {
        return ['ext' => 'jpg', 'mime' => 'image/jpeg'];
    }

    if (str_starts_with($hex, '474946383761') || str_starts_with($hex, '474946383961')) {
        return ['ext' => 'gif', 'mime' => 'image/gif'];
    }

    if (str_starts_with($hex, '52494646') && substr($hex, 16, 8) === '57454250') {
        return ['ext' => 'webp', 'mime' => 'image/webp'];
    }

    if (str_starts_with($hex, '00000100')) {
        return ['ext' => 'ico', 'mime' => 'image/x-icon'];
    }

    return [];
}

/** 允许的 logo 大小上限（1 MB）：logo 是一张小图，超过就不是 logo 了。 */
function panel_logo_max_bytes(): int
{
    return 1024 * 1024;
}

/**
 * 处理一次 logo 上传（$_FILES['logo']）。
 *
 * @return array{ok:bool,error:string,warning:string,name:string}
 */
function panel_logo_store_upload(array $file, string $actor = ''): array
{
    $result = ['ok' => false, 'error' => '', 'warning' => '', 'name' => ''];

    $error = (int) ($file['error'] ?? UPLOAD_ERR_NO_FILE);

    if ($error === UPLOAD_ERR_NO_FILE) {
        $result['error'] = t('没有选择要上传的图片。');

        return $result;
    }

    if ($error !== UPLOAD_ERR_OK) {
        $result['error'] = sprintf(t('上传没有完成（PHP 错误码 %d）。'), $error);

        return $result;
    }

    $size = (int) ($file['size'] ?? 0);
    $max = panel_logo_max_bytes();

    if ($size <= 0 || $size > $max) {
        $result['error'] = sprintf(
            t('图片必须大于 0 字节、不超过 %s。'),
            human_bytes($max)
        );

        return $result;
    }

    $temp = (string) ($file['tmp_name'] ?? '');

    if ($temp === '' || !is_file($temp)) {
        $result['error'] = t('上传的临时文件读不到，请重试。');

        return $result;
    }

    $data = @file_get_contents($temp);

    if ($data === false || $data === '') {
        $result['error'] = t('读取上传的图片失败，请重试。');

        return $result;
    }

    $type = panel_image_type((string) $data);

    if ($type === []) {
        $result['error'] = t('只支持 PNG / JPEG / GIF / WebP / ICO 图片（不接受 SVG，避免脚本注入）。');

        return $result;
    }

    $dir = panel_logo_dir(true);

    if ($dir === '' || !is_dir($dir) || !is_writable($dir)) {
        $result['error'] = sprintf(t('上传目录不可写：%s'), $dir);

        return $result;
    }

    // 旧图先记住（panel_logo_path() 是带缓存的）：换图成功之后要把它删掉，
    // 否则每换一次就多留一张没人引用的文件。
    $oldPath = panel_logo_path();

    $name = 'logo-' . bin2hex(random_bytes(8)) . '.' . $type['ext'];
    $target = rtrim($dir, '/') . '/' . $name;

    if (@file_put_contents($target, (string) $data) === false) {
        $result['error'] = sprintf(t('图片写不进上传目录：%s'), $dir);

        return $result;
    }

    $saved = panel_setting_save('panel_logo', $name, $actor);

    if (!$saved['ok']) {
        @unlink($target);
        $result['error'] = $saved['error'];

        return $result;
    }

    // 旧图删掉（只删我们自己命名的那种；panel_logo_path() 已经保证是白名单里的名字）。
    if ($oldPath !== '' && $oldPath !== $target) {
        @unlink($oldPath);
    }

    $result['ok'] = true;
    $result['name'] = $name;
    $result['warning'] = (string) ($saved['warning'] ?? '');

    return $result;
}

/** 清掉 logo，回到内置草方块（文件删掉 + 设置清空）。 */
function panel_logo_remove(string $actor = ''): array
{
    $path = panel_logo_path();

    if ($path !== '') {
        @unlink($path);
    }

    $saved = panel_setting_save('panel_logo', '', $actor);

    if (!$saved['ok']) {
        return ['ok' => false, 'error' => $saved['error']];
    }

    return ['ok' => true, 'error' => ''];
}

/**
 * 输出 logo（index.php?action=logo）。**不需要登录**：登录页的 favicon 也要用它。
 *
 * 缓存 5 分钟：地址里带 filemtime 做版本戳，换图之后地址就变了，不会拿到旧图。
 */
function panel_serve_logo(): void
{
    $path = panel_logo_path();

    if ($path === '') {
        http_response_code(404);
        echo '';
        exit;
    }

    $data = @file_get_contents($path);

    if ($data === false || $data === '') {
        http_response_code(404);
        echo '';
        exit;
    }

    $type = panel_image_type((string) $data);

    if ($type === []) {
        http_response_code(404);
        echo '';
        exit;
    }

    if (!headers_sent()) {
        header('Content-Type: ' . $type['mime']);
        header('Content-Length: ' . strlen((string) $data));
        header('Cache-Control: public, max-age=300');
        header('X-Content-Type-Options: nosniff');
    }

    echo (string) $data;
    exit;
}

// ============================================================
// 8. 审计说明（不进日志的值在这里被挡掉）
// ============================================================

/**
 * 一条设置改动的审计说明。
 *
 * 安全入口后缀是**路径级别的秘密**，不写进 relay_events（那张表是给人看的，
 * 也会被导出）；只记"改了 / 清空了"。
 */
function panel_setting_audit_detail(string $key, string $value): string
{
    if ($key === 'url_suffix') {
        return $value === ''
            ? t('清空 URL 安全入口后缀（值不写进日志）')
            : t('更新 URL 安全入口后缀（值不写进日志）');
    }

    if ($key === 'panel_logo') {
        return $value === ''
            ? t('清除面板 logo（恢复默认草方块）')
            : t('更新面板 logo（图片内容不写进日志）');
    }

    return sprintf(t('面板设置 %s = %s'), $key, $value);
}
