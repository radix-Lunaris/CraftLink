<?php
/**
 * Login page.
 *
 * Lives at web/login.php so the container health check can probe it, and is
 * also reachable as index.php?action=login.
 */

declare(strict_types=1);

require_once __DIR__ . '/../lib/common.php';

start_session();

// 反代子路径 + 没有结尾斜杠 = 相对地址全错（没样式、登录表单 404）。
// 先纠正地址再处理任何请求。
enforce_trailing_slash();

// 登录页也绝不缓存。
//
// no_cache_headers() 的注释里早就写着"登录页也必须 no-store"，但那句话只在
// index.php 那条路径上成立（index.php 顶部调了它）。直接打开 /login.php
// （健康检查、以及用户手输的地址）时没人设这个头，浏览器可以合法地把整页
// 留在缓存里 —— 而"页面里的地址基准"（<base>）是随页面一起下发的：留住了
// 旧页面，就等于留住了旧的基准，用户改完代码刷新还是老样子。
no_cache_headers();

// 语言 / 主题：?lang= / ?theme= 写进 cookie。必须在任何输出之前 ——
// setcookie 要求响应头还没发出去。
panel_apply_preferences();

if (current_user() !== null) {
    redirect('index.php?action=dashboard');
}

// M4 域名绑定：绑了域名之后，只有该域名的 Host 能到登录页（急救入口跳过）。
// nginx 那一层已经判过一次，这里是第二层 —— 面板被别的反代挡在前面、
// 或者新配置还没生效时，仍然守得住。
panel_transport_guard();

$flash = flash();

// 站名：面板设置里的「左上角名称 / 网页标签名」优先，其次是 config.php /
// MCLINK_PANEL_TITLE，最后是内置默认值 —— 和顶栏共用同一个取值函数，
// 所以登录页和登录后的页头永远不会显示两个名字。
$panel_title = panel_brand_name();

// 兜底：panel_brand_name() 正常永远非空（默认值就是 CraftLink 管理面板，见
// lib/common.php 的 PANEL_DEFAULT_TITLE）；这里再兜一次，登录页的 <title>
// 绝不会是空的。
if (trim($panel_title) === '') {
    $panel_title = PANEL_DEFAULT_TITLE;
}

// 网页标签名：面板设置里的「网页标签名」，留空 = 跟左上角名称一样。
$panel_tab_title = panel_tab_title();

// logo（面板设置里上传的那张）：登录页也用它做 favicon，和左上角是同一张图。
$logo_url = panel_logo_url();

// 头像：assets/avatar.png。图片不存在（还没生成 / 上传漏了）时
// avatar_url() 回一个空串，这里就退回原来的 MC 方块 —— 登录页永远
// 不会出现一个破图。
$avatar = avatar_url();

// 切换条要跳回这一页（登录页没有 action 参数，得自己说明白）。
$prefs_action = 'login';
?>
<!DOCTYPE html>
<html lang="<?= h(panel_html_lang()) ?>" data-theme="<?= h(panel_theme()) ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title><?= h(t('登录')) ?> - <?= h(t($panel_tab_title)) ?></title>
<?php
// favicon：面板设置里上传的 logo（和左上角标记同一张图）。没上传就不发这个
// 标签，浏览器用默认图标 —— 和以前一样。
if ($logo_url !== ''):
    ?>
    <link rel="icon" href="<?= h($logo_url) ?>">
    <?php
endif;
?>
<?php
// 相对地址的基准：用 "./"（相对于当前文档）而不是服务端推导的路径 ——
// 只有浏览器同时知道端口和反代前缀，详见 lib/common.php 的 panel_base_href()。
?>
<base href="<?= h(panel_base_href()) ?>">
<link rel="stylesheet" href="<?= asset_url('assets/style.css') ?>">
</head>
<body class="login-body">

<div class="login-stack">
    <div class="login-card">
        <div class="login-head">
            <?php if ($avatar !== ''): ?>
                <img class="login-avatar" src="<?= $avatar ?>"
                     alt="" width="84" height="84">
            <?php else: ?>
                <?php
                // 头像图片不在时的退路。原来是「MC」字母方块 —— 改名的同时
                // 它也作废了，所以换成和顶栏同一枚等距草方块（内联 SVG，
                // 不引用任何文件，永远不会是破图）。
                ?>
                <span class="logo big">
                    <svg class="logo-mark" viewBox="0 0 32 32" width="100%"
                         height="100%" aria-hidden="true" focusable="false">
                        <polygon points="16,1 31,9 16,17 1,9"
                                 fill="#6cbf3f" stroke="currentColor"
                                 stroke-width="1" stroke-linejoin="round"/>
                        <polygon points="1,9 16,17 16,31 1,23"
                                 fill="#8a5a33" stroke="currentColor"
                                 stroke-width="1" stroke-linejoin="round"/>
                        <polygon points="31,9 31,23 16,31 16,17"
                                 fill="#6b4425" stroke="currentColor"
                                 stroke-width="1" stroke-linejoin="round"/>
                    </svg>
                </span>
            <?php endif; ?>
            <h1><?= h(t($panel_title)) ?></h1>
            <p class="dim"><?= h(t('请使用你的 CraftLink 账号登录')) ?></p>
        </div>

        <?php if ($flash !== null): ?>
            <div class="flash <?= h((string) $flash['type']) ?>">
                <?= h((string) $flash['message']) ?>
            </div>
        <?php endif; ?>

        <form method="post" action="index.php?action=login" class="login-form">
            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">

            <label>
                <?= h(t('用户名')) ?>
                <input type="text" name="username" autocomplete="username"
                       autofocus required maxlength="32">
            </label>

            <label>
                <?= h(t('密码')) ?>
                <?php
                // 密码框带「显示密码」（小眼睛）。图标是内联 SVG（不引用文件），
                // 按钮不带文字，所以不需要翻译。事件由 prefs.php 里那段小脚本
                // 挂到 .password-field 上 —— 登录页也 require 了 prefs.php，
                // 所以这里不用再单独引一次脚本。
                ?>
                <span class="password-field">
                    <input type="password" name="password"
                           autocomplete="current-password" required maxlength="128">
                    <button type="button" class="password-toggle" aria-hidden="true" tabindex="-1">
                        <svg class="eye" viewBox="0 0 20 20" width="17" height="17" aria-hidden="true">
                            <path d="M10 4C5.8 4 2.4 6.9 1 10c1.4 3.1 4.8 6 9 6s7.6-2.9 9-6c-1.4-3.1-4.8-6-9-6z"
                                  fill="none" stroke="currentColor" stroke-width="1.6"/>
                            <circle cx="10" cy="10" r="2.6" fill="currentColor"/>
                        </svg>
                        <svg class="eye-off" viewBox="0 0 20 20" width="17" height="17" aria-hidden="true">
                            <path d="M10 4C5.8 4 2.4 6.9 1 10c1.4 3.1 4.8 6 9 6s7.6-2.9 9-6c-1.4-3.1-4.8-6-9-6z"
                                  fill="none" stroke="currentColor" stroke-width="1.6"/>
                            <circle cx="10" cy="10" r="2.6" fill="currentColor"/>
                            <path d="M3.5 3.5 L16.5 16.5" stroke="currentColor" stroke-width="1.6"
                                  stroke-linecap="round"/>
                        </svg>
                    </button>
                </span>
            </label>

            <button type="submit" class="primary"><?= h(t('登录')) ?></button>
        </form>

        <p class="dim small">
            <?= h(t('账号由管理员在「账号」页面创建。忘记密码请联系服务器管理员重置。')) ?>
        </p>
    </div>

    <?php require __DIR__ . '/views/prefs.php'; ?>
</div>

</body>
</html>
