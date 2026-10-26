<?php
/**
 * Shared layout: header, navigation and footer.
 *
 * A page sets $page_title (and optionally $page_subtitle) then includes this
 * file. The header emits everything up to and including the opening <main>.
 */

declare(strict_types=1);

$user  = current_user();
$flash = flash();
$title = $page_title ?? '仪表盘';

// 站名：MC-Link → CraftLink（方块联机）。config('panel.title') 仍然优先
//（服务器上的 config.php / MCLINK_PANEL_TITLE 可以覆盖），只是默认值换成了新
// 名字 —— 否则「改名」只改了源码，页面上的字样还是旧的。
$panel_title = (string) (config('panel.title') ?? 'CraftLink 管理面板');

// 头像（顶栏那一小个）。图片不在时 avatar_url() 回空串，退回原来的首字母圆点。
$avatar = avatar_url();

// ---- 顶部状态灯 ----
//
// 两个灯都取「面板自己实测」的结果，保证与仪表盘一致：
//   * 中继：面板调用 mc-relay 的 /health
//   * 数据库：面板用 config.php 里的账号连一次 MySQL
//
// 之前仪表盘的「数据库」读的是中继上报的 status.json（那是中继进程启动时
// 写入的快照），于是出现过"右上角正常、仪表盘显示不可用"的矛盾。
$header_health = relay_api('GET', '/health');
$relay_ok = $header_health['ok'];

// /health 的响应没有 data 层：
//   {"ok":true,"service":...,"uptime":...,"database":"up"|"down"}
// 所以从 'raw' 里取（relay_api 会把原始响应体塞在 raw）。
// 中继上报的数据库状态优先看 /stats 里的详细字段；这里只是顶部那个小圆点。
$relay_database = $header_health['raw']['database'] ?? 'down';
$relay_db_ok = ($relay_database === 'up' || $relay_database === true);

$panel_db_ok = true;
$panel_db_error = '';

try {
    db()->query('SELECT 1');
} catch (Throwable $e) {
    $panel_db_ok = false;
    $panel_db_error = $e->getMessage();
}
?>
<!DOCTYPE html>
<html lang="<?= h(panel_html_lang()) ?>" data-theme="<?= h(panel_theme()) ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title><?= h(t($title)) ?> - <?= h(t($panel_title)) ?></title>
<?php
// <base> 必须排在第一个相对地址之前，浏览器才会拿它当基准。
//
// 值是 "./"（相对于当前文档），不是服务端从 REQUEST_URI 推导的路径：
// 只有浏览器的地址栏同时知道**端口**和**反代前缀**。用户报的"面板完全没有
// CSS"就是推导错的后果 —— 反代把前缀剥掉时算出来是 "/"，样式表被取到
// https://域名/assets/style.css（丢了 :54865、也丢了 /秘密路径/），落到站点
// 根目录上就是 404。详见 common.php 的 panel_base_href()。
?>
<base href="<?= h(panel_base_href()) ?>">
<link rel="stylesheet" href="<?= asset_url('assets/style.css') ?>">
</head>
<body>

<header class="topbar">
    <div class="brand">
        <span class="logo">
            <?php
            // 品牌标记：等距草方块（和客户端图标是同一个想法）。
            //
            // 为什么画成内联 SVG，而不是引用一张图片：panel/web/assets/ 下
            // 只有 avatar.png（MC 风格的头像），没有方形 logo。原来那个
            // 「MC」字母方块随改名一起作废了，而指向一个不存在的文件只会
            // 变成一个破图图标。内联 SVG 不依赖任何文件，也不会 404。
            //
            // 三个面是草方块本身的颜色（草绿 + 土棕），刻意不用 --accent：
            // 亮色主题下那个蓝是给按钮用的，草方块就得是草色。
            // 外圈那笔描边用 currentColor（跟着文字颜色走），所以亮色 / 暗色
            // 下都有轮廓，不会糊在背景里。aria-hidden：旁边的文字已经说了
            // 品牌，读屏不必再念一遍。
            ?>
            <svg class="logo-mark" viewBox="0 0 32 32" width="100%" height="100%"
                 aria-hidden="true" focusable="false">
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
        <span class="brand-text"><?= h(t($panel_title)) ?></span>
    </div>

    <?php
    // 导航的 active 判断用的是**原始中文页名**（$page_title），不是翻译后的
    // 文字：翻译只负责显示，判断逻辑不能被语言影响。
    ?>
    <nav class="nav">
        <a href="index.php?action=dashboard"
           class="<?= $title === '仪表盘' ? 'active' : '' ?>"><?= h(t('仪表盘')) ?></a>
        <a href="index.php?action=rooms"
           class="<?= $title === '房间' ? 'active' : '' ?>"><?= h(t('房间')) ?></a>
        <?php if (is_admin()): ?>
            <a href="index.php?action=users"
               class="<?= $title === '账号' ? 'active' : '' ?>"><?= h(t('账号')) ?></a>
        <?php endif; ?>
        <a href="index.php?action=events"
           class="<?= $title === '操作日志' ? 'active' : '' ?>"><?= h(t('操作日志')) ?></a>
        <a href="index.php?action=account"
           class="<?= $title === '我的账号' ? 'active' : '' ?>"><?= h(t('我的账号')) ?></a>
    </nav>

    <div class="status">
        <span class="dot <?= $relay_ok ? 'ok' : 'bad' ?>"></span>
        <span class="status-text"><?= h($relay_ok ? t('中继正常') : t('中继不可用')) ?></span>
        <span class="dot <?= $panel_db_ok ? 'ok' : 'bad' ?>"></span>
        <span class="status-text"><?= h($panel_db_ok ? t('数据库正常') : t('数据库不可用')) ?></span>

        <?php
        // 语言（中文 / English）和主题（亮色 / 暗色）切换。
        // 两个都是普通链接，不依赖 JavaScript；切换后回到当前这一页
        //（$prefs_action 只是没有 action 参数时的兜底）。
        require __DIR__ . '/prefs.php';
        ?>

        <?php if ($user !== null): ?>
            <?php
            // The role chip, as a proper pill: coloured dot, then the name,
            // then the role. A bare coloured word next to the username read
            // like a rendering accident rather than a status.
            $badge_role = normalize_role(
                $user['role'] ?? null, $user['is_admin'] ?? 0
            );
            $badge_colour = role_colors()[$badge_role] ?? '#4c8dff';
            $badge_label = role_labels()[$badge_role] ?? t('玩家');
            ?>
            <a class="who" href="index.php?action=account"
               title="<?= h(t('修改我的用户名和密码')) ?>"
               style="<?= role_style($badge_role) ?>">
                <?php // 有头像图片就用图片，没有就还是首字母（class 里的
                      // has-image 只是给图片版加一圈角色颜色，见 style.css）。 ?>
                <span class="avatar<?= $avatar !== '' ? ' has-image' : '' ?>">
                    <?php if ($avatar !== ''): ?>
                        <img src="<?= $avatar ?>" alt="" width="26" height="26">
                    <?php else: ?>
                        <?php
                        // The first character, without mb_substr: the image does
                        // not guarantee mbstring, and a missing extension would
                        // take the whole page down over a decorative avatar.
                        // A username is ASCII in practice (the relay rejects
                        // spaces and control characters), so substr is enough;
                        // anything multi-byte just yields one replacement glyph.
                        $initial = substr((string) $user['username'], 0, 1);
                        echo h($initial !== '' ? $initial : '?');
                        ?>
                    <?php endif; ?>
                </span>
                <span class="who-name"><?= h((string) $user['username']) ?></span>
                <span class="role-pill"><?= h($badge_label) ?></span>
            </a>
            <form method="post" action="index.php?action=logout" class="inline">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <button type="submit" class="link-button"><?= h(t('退出')) ?></button>
            </form>
        <?php endif; ?>
    </div>
</header>

<main class="wrap">

<?php if ($flash !== null): ?>
    <div class="flash <?= h((string) $flash['type']) ?>">
        <?= h((string) $flash['message']) ?>
    </div>
<?php endif; ?>

<?php if ($user !== null && !can_operate()): ?>
    <div class="flash info readonly-banner">
        <b><?= h(t('只读模式')) ?></b><?= h(t(' —— ')) ?><?= h(t('你的账号可以查看所有数据，但')) ?><b><?= h(t('不能修改任何东西')) ?></b><?= h(t('（房间、账号、开关都是只读的）。')) ?><?= h(t('需要操作权限请联系管理员。')) ?>
    </div>
<?php endif; ?>

<?php
// ---- 中继是不是旧版？直接说出来 ----
//
// This is the answer to "我改了参数/关了房间怎么没反应": the panel can be
// newer than the relay container, and then the feature quietly does nothing.
// Saying WHICH features are missing, and the exact command, turns a night of
// guessing into one command.
$feature_check = relay_feature_check();
?>

<?php if ($user !== null && !$feature_check['ok']): ?>
    <div class="flash error">
        <b><?= h(t('中继容器是旧版')) ?></b><?= h(t(' —— ')) ?><?= h(t('面板上这几个功能现在不会生效：')) ?>

        <ul class="tight">
            <?php foreach ($feature_check['missing'] as $label): ?>
                <li><?= h((string) $label) ?></li>
            <?php endforeach; ?>
        </ul>

        <?= h(t('在服务器上执行这一条命令重建中继就好了（会断开正在玩的玩家，挑没人的时候跑）：')) ?>

        <br>
        <code><?= h(deployment_hint('bash /opt/mc-link/rebuild.sh relay')) ?></code>

        <?php if ($feature_check['build'] !== ''): ?>
            <br><span class="dim small">
                <?= h(sprintf(t('当前中继代码时间：%s'), (string) $feature_check['build'])) ?>
            </span>
        <?php endif; ?>
    </div>
<?php elseif ($user !== null && $feature_check['build'] !== ''): ?>
    <div class="dim small" style="margin: 0 0 10px;">
        <?= h(sprintf(t('中继版本正常（代码时间 %s）'), (string) $feature_check['build'])) ?>
    </div>
<?php endif; ?>

<?php if (!$relay_ok): ?>
    <div class="flash error">
        <?= h(sprintf(
            t('无法连接中继服务的后台接口：%s'),
            (string) ($header_health['error'] ?: t('未知错误'))
        )) ?>
        <br><small><?= h(t('请检查 mc-relay 容器是否在运行，以及面板 config.php 里的 relay.base_url / relay.token 是否与中继的 MCLINK_ADMIN_API_TOKEN 一致。')) ?></small>
    </div>
<?php endif;
