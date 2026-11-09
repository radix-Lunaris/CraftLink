<?php
/**
 * 语言 / 主题切换条（中英文 + 亮色/暗色）。
 *
 * 两个切换都是**普通链接**，不依赖 JavaScript：点一下带上 ?lang= / ?theme=，
 * PHP 把选择写进 cookie，然后按新语言 / 新主题把当前这一页重新渲染一遍。
 * 浏览器禁用 JS 时照样能用，也不会自己刷新（只有人点它才会变）。
 *
 * 顶栏（views/layout_header.php）和登录页（web/login.php）都用这一份，
 * 免得两处的切换条慢慢长得不一样。
 *
 * 调用方可以预设 $prefs_action：切换后要跳回哪个页面。不给就沿用当前
 * 地址里的 action 参数（面板所有页面都是同一个脚本 + action 参数）。
 * 登录页没有 action 参数，所以自己传 'login'。
 */

declare(strict_types=1);

$prefs_action = $prefs_action ?? 'dashboard';
$prefs_lang   = panel_lang();
$prefs_theme  = panel_theme();
?>
<div class="prefs">
    <a class="pref<?= $prefs_lang === PANEL_LANG_ZH ? ' active' : '' ?>"
       href="<?= panel_pref_url('lang', PANEL_LANG_ZH, $prefs_action) ?>"
       title="<?= h(t('中文界面')) ?>">中文</a>
    <a class="pref<?= $prefs_lang === PANEL_LANG_EN ? ' active' : '' ?>"
       href="<?= panel_pref_url('lang', PANEL_LANG_EN, $prefs_action) ?>"
       title="<?= h(t('英文界面')) ?>">English</a>

    <span class="pref-sep"></span>

    <a class="pref<?= $prefs_theme === PANEL_THEME_LIGHT ? ' active' : '' ?>"
       href="<?= panel_pref_url('theme', PANEL_THEME_LIGHT, $prefs_action) ?>"
       title="<?= h(t('亮色主题')) ?>"><?= h(t('亮色')) ?></a>
    <a class="pref<?= $prefs_theme === PANEL_THEME_DARK ? ' active' : '' ?>"
       href="<?= panel_pref_url('theme', PANEL_THEME_DARK, $prefs_action) ?>"
       title="<?= h(t('暗色主题')) ?>"><?= h(t('暗色')) ?></a>
</div>

<?php
// ---- 密码框的「显示密码」小眼睛 -------------------------------------------
//
// 这一段放在这里，是因为 prefs.php 是**每一页都会引**的唯一片段：顶栏
// （views/layout_header.php）和登录页（web/login.php）都 require 它，而登录页
// 根本没有 footer。放在 layout_footer.php 里的话登录页就漏掉了。
//
// 为什么内联而不是单独的 .js：部署包（scripts/sync-deploy.py）只按扩展名收
// panel/web/assets/*.css，新增的 .js 不进包 —— 服务器上 404，按钮就哑了。
// 内联在 .php 里跟着视图一起被复制，不会漏。
//
// 为什么用**事件委托**（监听 document，而不是给每个按钮挂 click）：
//
//   这段脚本在 <head> 之后就执行了（prefs.php 在顶栏里），而页面正文里的
//   密码框要到后面才被浏览器解析出来。「我的账号」那三个密码框就是这么漏掉
//   的 —— 脚本跑的时候 document.querySelectorAll('.password-field') 还是空
//   的，按钮上一根监听都没挂上，点它当然一动不动。登录页恰好相反：密码框在
//   prefs.php **之前**，所以那边一直是好的。这就是"登录页能点、我的账号页
//   点不动"的全部原因。
//
//   委托之后，谁先谁后都无所谓：点击事件冒泡到 document 时再去最近的
//   .password-field 里找输入框，页面上任何一个密码框（现在有的、以后加的、
//   PHP 在表格里循环渲染出来的）都自动生效，只有这一份实现。
//
// 没有 JS 时（或者浏览器太老）按钮什么都不做，密码框本身的功能一点不变。
?>
<script>
(function () {
    'use strict';

    // 只挂一次：这一页万一引了两遍 prefs.php，两个监听会让 type 连续切两次，
    // 看起来还是"点了没反应"。
    if (window.__mclinkPasswordToggle) { return; }

    window.__mclinkPasswordToggle = true;

    function hasClass(node, name) {
        return !!(node && node.classList && node.classList.contains(name));
    }

    // 从点击处往上找最近的、带某个 class 的元素（不用 Element.closest：
    // 老浏览器没有它，而这里也就五行）。
    function upTo(node, name) {
        while (node && node.nodeType === 1) {
            if (hasClass(node, name)) { return node; }

            node = node.parentNode;
        }

        return null;
    }

    document.addEventListener('click', function (event) {
        // 点在 svg / path 上时 target 是图形本身，upTo 会一路提到按钮上。
        var toggle = upTo(event.target, 'password-toggle');

        if (!toggle) { return; }

        var field = upTo(toggle, 'password-field');
        var input = field ? field.querySelector('input') : null;

        if (!input) { return; }

        var hidden = input.type === 'password';

        // 只切 type，value 一个字都不动：表单提交的还是同一个字段。
        input.type = hidden ? 'text' : 'password';

        if (hidden) {
            toggle.classList.add('revealed');
        } else {
            toggle.classList.remove('revealed');
        }
    });
})();
</script>
