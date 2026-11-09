<?php
/**
 * 面板设置（M2 / M3）—— root 改所有项，普通管理员默认只读。
 *
 * WHY THIS PAGE EXISTS
 *
 * 面板以前的所有开关都住在中继那边（/settings：必须登录、人数上限……），
 * 而"面板自己"的参数（监听地址、安全入口、会话时长、名字、logo、备案号）
 * 只能改 config.php / 环境变量再重建容器 —— 用户改一个名字要动服务器。
 *
 * 这一页把面板自己的参数搬进面板，同时把权限说清楚：
 *
 *   * root：每一条都能改；每条还有一个**只有 root 看得见**的开关，
 *           打开之后普通管理员才能改那一条（默认全部关闭）。
 *   * 普通管理员：整页可见（知道服务器上是什么配置），但控件是禁用的。
 *   * 玩家：看不到这一页（控制器里 require_admin()）。
 *
 * [!] 页面上的禁用 / 隐藏只是方便。真正拦下越权修改的是
 *     require_panel_setting_edit()（见 lib/panel_settings.php），
 *     控制器里的每一次 POST 都要过它。
 *
 * 存储（D1）：数据库为主 + config/panel-settings.json 备份；数据库不可用时
 * 只读回退到配置文件。页面顶部那块状态就是把这两处摊开给管理员看。
 */

declare(strict_types=1);

$page_title = '面板设置';

$state = panel_settings_state();
$defs = panel_setting_defs();
$groups = panel_setting_groups();

// 按分组排好（注册表的顺序就是页面上的顺序）。
$by_group = [];

foreach ($defs as $key => $def) {
    $by_group[(string) ($def['group'] ?? 'other')][] = $key;
}

$logo_url = panel_logo_url();
$listen = panel_listen_config();

// M4：域名与 SSL 的"当前状态"——证书链、生效链路、根代理、急救入口。
$nginx_state = panel_nginx_state(true);
$ssl_status = panel_ssl_status(true);
$ssl_active = panel_ssl_active_cert();
$ssl_mode_now = panel_ssl_mode();
$ssl_days_left = panel_ssl_days_left();
$last_apply = $nginx_state['last_result'];

$can_edit_ssl = panel_setting_can_edit('ssl_cert_file');
$can_edit_domain = panel_setting_can_edit('bound_domain');
$can_edit_entry = panel_setting_can_edit('url_suffix');

require __DIR__ . '/layout_header.php';
?>

<h2><?= h(t('面板设置')) ?></h2>

<p class="dim">
    <?= h(t('这里改的是')) ?><b><?= h(t('面板自己')) ?></b><?= h(t('的参数（监听地址、安全入口、会话时长、名字、logo、备案号），不是中继的运行开关 —— 那些在仪表盘的「运行开关」里。')) ?>
</p>

<div class="flash info settings-storage">
    <b><?= h(t('存储状态')) ?></b>
    <br>
    <?= h(sprintf(t('面板版本：%s'), PANEL_VERSION)) ?>
    <br>
    <?php if ($state['db_ok']): ?>
        <?= h(sprintf(t('数据库：可用（%d 条设置）—— 这是主存储。'), (int) $state['db_rows'])) ?>
    <?php else: ?>
        <?= h(t('数据库：不可用 —— 现在只读回退到配置文件备份，保存会报错。')) ?>
        <?php if ($state['db_error'] !== ''): ?>
            <br><span class="mono small"><?= h($state['db_error']) ?></span>
        <?php endif; ?>
    <?php endif; ?>
    <br>
    <?= h(sprintf(
        t('配置文件备份：%s（%s，%s）'),
        $state['file_path'],
        $state['file_exists'] ? t('存在') : t('还没有'),
        $state['file_writable'] ? t('可写') : t('不可写')
    )) ?>
</div>

<?php if ($state['overrides'] !== []): ?>
    <?php // 急救覆盖：root 的 mclink 管理台在"数据库写不进去"时写的，优先级最高。 ?>
    <div class="flash warn">
        <b><?= h(t('当前有「急救覆盖」在生效（优先级高于数据库）')) ?></b>
        <br>
        <?= h(sprintf(t('文件：%s'), $state['overrides_path'])) ?>
        <br>
        <span class="mono small"><?= h(implode(t('、'), array_keys($state['overrides']))) ?></span>
        <br>
        <?= h(t('这些值是 root 的 mclink 管理台在数据库写不进去时写的（例如重置安全入口）。在设置页里把这一项保存一次就会自动清掉，也可以直接点下面的按钮清除。')) ?>
        <?php if (is_root()): ?>
            <form method="post" action="index.php?action=panel_emergency_clear" class="inline setting-form">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <button type="submit" class="danger small"><?= h(t('清除急救覆盖')) ?></button>
            </form>
        <?php endif; ?>
    </div>
<?php endif; ?>

<?php if (is_root()): ?>
    <div class="flash info">
        <b><?= h(t('你是根管理员。')) ?></b>
        <?= h(t('每一条设置右边都有一个「允许普通管理员修改」开关，默认全部关闭；打开之后普通管理员才能改那一条，随时可以收回。')) ?>
    </div>
<?php else: ?>
    <div class="flash warn">
        <b><?= h(t('你的账号是普通管理员：这一页可以看，但默认不能改。')) ?></b>
        <?= h(t('需要修改哪一条，请让根管理员在那一项右侧打开放行开关。')) ?>
    </div>
<?php endif; ?>

<?php foreach ($groups as $slug => $group_label): ?>
    <?php $keys = $by_group[$slug] ?? []; ?>

    <?php if ($keys === []) { continue; } ?>

    <h3><?= h($group_label) ?></h3>

    <?php foreach ($keys as $key): ?>
        <?php
        $def = $defs[$key];
        $type = (string) ($def['type'] ?? 'text');
        $value = panel_setting($key, '');
        $can_edit = panel_setting_can_edit($key);
        $granted = is_admin() && !is_root()
            ? panel_setting_admin_edit_enabled($key)
            : false;
        $allow_admin_edit = panel_setting_admin_edit_enabled($key);
        ?>
        <section class="panel-block setting-item">
            <div class="setting-head">
                <b><?= h((string) $def['label']) ?></b>
                <span class="dim"><?= h((string) ($def['help'] ?? '')) ?></span>

                <?php if ($value === '' && $type !== 'image'): ?>
                    <span class="dim small"><?= h(t('现在用的是默认值；留空保存 = 回到默认。')) ?></span>
                <?php endif; ?>

                <span class="dim small mono"><?= h(sprintf(
                    t('%s · 默认值：%s'),
                    $key,
                    panel_setting_default($key) === '' ? t('空') : panel_setting_default($key)
                )) ?><?php if (!empty($def['restart'])): ?> · <?= h(t('改完需要重启面板容器')) ?><?php endif; ?></span>
            </div>

            <div class="setting-actions">
                <?php if ($type === 'image'): ?>
                    <?php if ($logo_url !== ''): ?>
                        <img class="logo-preview" src="<?= h($logo_url) ?>" alt="" width="40" height="40">
                    <?php else: ?>
                        <span class="logo-preview logo-fallback"><?= h(t('默认')) ?></span>
                    <?php endif; ?>
                <?php endif; ?>

                <form method="post" action="index.php?action=panel_settings_save"
                      class="inline setting-form"<?= $type === 'image' ? ' enctype="multipart/form-data"' : '' ?>>
                    <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                    <input type="hidden" name="name" value="<?= h($key) ?>">

                    <?php if ($type === 'image'): ?>
                        <?php if ($can_edit): ?>
                            <input type="file" name="logo"
                                   accept="image/png,image/jpeg,image/gif,image/webp,image/x-icon">
                            <button type="submit" class="primary"><?= h(t('上传')) ?></button>
                            <?php if ($logo_url !== ''): ?>
                                <button type="submit" class="danger" name="logo_clear" value="1"><?= h(t('清除')) ?></button>
                            <?php endif; ?>
                        <?php else: ?>
                            <span class="dim small"><?= h(t('普通管理员不能修改这一项（需要根管理员放行）。')) ?></span>
                        <?php endif; ?>
                    <?php elseif ($type === 'select'): ?>
                        <select name="value" class="setting-value"<?= $can_edit ? '' : ' disabled' ?>>
                            <?php foreach ((array) $def['options'] as $option_value => $option_label): ?>
                                <option value="<?= h((string) $option_value) ?>"<?= (string) $option_value === $value ? ' selected' : '' ?>><?= h((string) $option_label) ?></option>
                            <?php endforeach; ?>
                        </select>
                        <?php if ($can_edit): ?>
                            <button type="submit" class="primary"><?= h(t('保存')) ?></button>
                        <?php endif; ?>
                    <?php elseif ($type === 'number'): ?>
                        <input type="number" name="value" class="setting-value"
                               min="<?= (int) ($def['min'] ?? 0) ?>"
                               max="<?= (int) ($def['max'] ?? 0) ?>"
                               step="1" value="<?= h($value) ?>"
                               <?= $can_edit ? '' : 'disabled' ?>>
                        <?php if ($can_edit): ?>
                            <button type="submit" class="primary"><?= h(t('保存')) ?></button>
                        <?php endif; ?>
                    <?php else: ?>
                        <input type="text" name="value" class="setting-value"
                               value="<?= h($value) ?>" <?= $can_edit ? '' : 'disabled' ?>>
                        <?php if ($can_edit): ?>
                            <button type="submit" class="primary"><?= h(t('保存')) ?></button>
                        <?php endif; ?>
                    <?php endif; ?>
                </form>

                <?php if (is_root()): ?>
                    <?php // 这个开关只有 root 渲染（panel_setting_can_toggle_admin_edit）
                          // 并且服务端只有 root 能改（require_root()）。 ?>
                    <form method="post" action="index.php?action=panel_setting_admin_edit"
                          class="inline setting-form">
                        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                        <input type="hidden" name="name" value="<?= h($key) ?>">
                        <input type="hidden" name="allow" value="<?= $allow_admin_edit ? '0' : '1' ?>">
                        <span class="pill <?= $allow_admin_edit ? 'ok' : 'warn' ?>">
                            <?= h($allow_admin_edit ? t('普通管理员可改') : t('只有 root 可改')) ?>
                        </span>
                        <button type="submit" class="small">
                            <?= h($allow_admin_edit ? t('收回放行') : t('放行给普通管理员')) ?>
                        </button>
                    </form>
                <?php endif; ?>
            </div>

            <?php if (!$can_edit): ?>
                <div class="setting-note dim small">
                    <?= h(t('只读：这一项现在只允许根管理员修改。')) ?>
                </div>
            <?php elseif ($granted): ?>
                <div class="setting-note dim small">
                    <?= h(t('根管理员已放行这一项，普通管理员可以修改。')) ?>
                </div>
            <?php endif; ?>

            <?php $effective_note = panel_setting_effective_note($key); ?>
            <?php if ($effective_note !== ''): ?>
                <div class="setting-note dim small"><?= h($effective_note) ?></div>
            <?php endif; ?>
        </section>
    <?php endforeach; ?>
<?php endforeach; ?>

<h3><?= h(t('SSL 证书与生效状态（M4）')) ?></h3>

<p class="dim small">
    <?= h(t('改完上面的设置不用重启容器：保存之后根代理会在几秒内重新渲染 nginx 配置、`nginx -t` 试跑、通过才热加载。这里显示的就是那条链路的当前状态。')) ?>
</p>

<div class="flash info settings-storage">
    <b><?= h(t('生效链路')) ?></b>
    <br>
    <?= h(sprintf(
        t('配置目录：%s（%s）'),
        $nginx_state['dir'],
        $nginx_state['main_exists'] ? t('已有配置') : t('还没有生成'))
    ) ?>
    <br>
    <?= h(sprintf(
        t('上次生成：%s；容器内监听 HTTP %d / HTTPS %d'),
        $nginx_state['generated_at'] === '' ? t('还没生成') : $nginx_state['generated_at'],
        (int) $nginx_state['http_port'],
        (int) $nginx_state['https_port']
    )) ?>
    <br>
    <?= h($nginx_state['agent']['message']) ?>
    <br>
    <?= h(sprintf(t('待处理请求：%d 条'), (int) $nginx_state['pending'])) ?>
    <br>
    <?= h(sprintf(t('急救入口：%s（只在本机 / 宿主机回环上可访问；安全入口、域名、证书配错时从这里进去改）'), $nginx_state['rescue_url'])) ?>

    <?php if ($last_apply['found']): ?>
        <br>
        <?= h(sprintf(
            t('上次生效结果：%s（%s）'),
            $last_apply['ok'] ? t('成功') : t('失败'),
            $last_apply['at']
        )) ?>
        <?php if (!$last_apply['ok']): ?>
            <br><span class="mono small"><?= h($last_apply['message']) ?></span>
        <?php endif; ?>
    <?php endif; ?>
</div>

<?php foreach ($nginx_state['notes'] as $note): ?>
    <div class="flash warn"><?= h($note) ?></div>
<?php endforeach; ?>

<section class="panel-block setting-item">
    <div class="setting-head">
        <b><?= h(t('证书列表与到期时间')) ?></b>
        <span class="dim"><?= h(panel_ssl_effective_note()) ?></span>
    </div>

    <?php if ($ssl_status['certs'] === []): ?>
        <div class="setting-note dim small">
            <?= h($ssl_status['found']
                ? t('证书目录里还没有证书。上传一张，或者先用「生成自签证书」把链路跑通。')
                : t('还没读到证书状态（根代理还没跑过 cert-status）。')) ?>
        </div>
    <?php else: ?>
        <table class="table compact">
            <thead>
                <tr>
                    <th><?= h(t('证书')) ?></th>
                    <th><?= h(t('签发者')) ?></th>
                    <th><?= h(t('到期时间')) ?></th>
                    <th><?= h(t('剩余')) ?></th>
                    <th><?= h(t('域名')) ?></th>
                    <th><?= h(t('状态')) ?></th>
                </tr>
            </thead>
            <tbody>
                <?php foreach ($ssl_status['certs'] as $row): ?>
                    <?php
                    $days = isset($row['days_left']) ? (int) $row['days_left'] : null;
                    $is_active = !empty($row['active']);
                    ?>
                    <tr>
                        <td class="mono small"><?= h((string) ($row['name'] ?? '')) ?></td>
                        <td class="small"><?= h((string) ($row['subject'] ?? ($row['error'] ?? ''))) ?><br>
                            <span class="dim"><?= h((string) ($row['issuer'] ?? '')) ?></span></td>
                        <td class="small mono"><?= h((string) ($row['not_after'] ?? '')) ?></td>
                        <td class="small">
                            <?php if ($days === null): ?>
                                <span class="pill bad"><?= h(t('无法解析')) ?></span>
                            <?php elseif ($days < 0): ?>
                                <span class="pill bad"><?= h(t('已过期')) ?></span>
                            <?php elseif ($days < 14): ?>
                                <span class="pill warn"><?= h(sprintf(t('%d 天'), $days)) ?></span>
                            <?php else: ?>
                                <span class="pill ok"><?= h(sprintf(t('%d 天'), $days)) ?></span>
                            <?php endif; ?>
                        </td>
                        <td class="small mono"><?= h((string) ($row['domains'] ?? '')) ?></td>
                        <td class="small">
                            <?php if ($is_active): ?>
                                <span class="pill ok"><?= h(t('正在使用')) ?></span>
                            <?php endif; ?>
                            <?php if (!empty($row['self_signed'])): ?>
                                <span class="pill warn"><?= h(t('自签')) ?></span>
                            <?php endif; ?>
                            <?php if (isset($row['key_match']) && empty($row['key_match'])): ?>
                                <span class="pill bad"><?= h(t('私钥不配对')) ?></span>
                            <?php endif; ?>
                            <?php if (($row['error'] ?? '') === 'invalid'): ?>
                                <span class="pill bad"><?= h(t('读不出来')) ?></span>
                            <?php endif; ?>
                        </td>
                    </tr>
                <?php endforeach; ?>
            </tbody>
        </table>
    <?php endif; ?>

    <?php if ($ssl_active !== []): ?>
        <div class="setting-note dim small mono"><?= h(sprintf(
            t('当前生效：%s / %s（指纹 %s）'),
            (string) ($ssl_active['cert_file'] ?? ''),
            (string) ($ssl_active['key_file'] ?? ''),
            (string) ($ssl_active['sha256'] ?? '')
        )) ?></div>
    <?php endif; ?>

    <?php // ---- 操作：上传 / 自签 / ACME / 刷新 / 应用 ---- ?>

    <div class="setting-actions">
        <?php if ($can_edit_ssl): ?>
            <form method="post" action="index.php?action=panel_ssl_upload"
                  class="inline setting-form" enctype="multipart/form-data">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <input type="file" name="cert" accept=".pem,.crt,.cer,.txt">
                <input type="file" name="key" accept=".pem,.key,.txt">
                <button type="submit" class="primary"><?= h(t('上传证书并启用')) ?></button>
            </form>
        <?php else: ?>
            <span class="dim small"><?= h(t('普通管理员不能修改这一项（需要根管理员放行）。')) ?></span>
        <?php endif; ?>
    </div>

    <div class="setting-actions">
        <?php if ($can_edit_ssl): ?>
            <form method="post" action="index.php?action=panel_ssl_selfsigned" class="inline setting-form">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <button type="submit"><?= h(t('生成自签证书（测试用）')) ?></button>
            </form>
        <?php endif; ?>

        <form method="post" action="index.php?action=panel_ssl_status_refresh" class="inline setting-form">
            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
            <button type="submit" class="small"><?= h(t('刷新证书状态')) ?></button>
        </form>

        <form method="post" action="index.php?action=panel_nginx_apply" class="inline setting-form">
            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
            <button type="submit" class="small"><?= h(t('重新渲染并生效')) ?></button>
        </form>
    </div>

    <div class="setting-actions">
        <?php if ($can_edit_ssl): ?>
            <form method="post" action="index.php?action=panel_ssl_acme" class="inline setting-form">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <input type="hidden" name="mode" value="issue">
                <label class="dim small">
                    <input type="checkbox" name="dry_run" value="1" checked>
                    <?= h(t('先 dry-run 验证（不真的签证书）')) ?>
                </label>
                <button type="submit"><?= h(t('用 Let’s Encrypt 申请')) ?></button>
            </form>

            <form method="post" action="index.php?action=panel_ssl_acme" class="inline setting-form">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <input type="hidden" name="mode" value="renew">
                <button type="submit" class="small"><?= h(t('检查并续期（一键续期）')) ?></button>
            </form>
        <?php endif; ?>
    </div>

    <div class="setting-note dim small">
        <?= h(t('上传证书：离线可用，不需要域名解析，也不需要 80 端口。Let’s Encrypt：需要域名解析到这台机器、80 端口能从公网进来；续期失败不会动现有证书。')) ?>
    </div>
</section>

<h3><?= h(t('急救（配置改坏了从这里回去）')) ?></h3>

<p class="dim small">
    <?= h(t('任何一项改动都有安全网：本机急救入口一直可用；下面这三个按钮也会真的改设置并让 nginx 立刻生效（走的是和 mclink 管理台同一条代码路径）。')) ?>
</p>

<section class="panel-block setting-item">
    <div class="setting-head">
        <b><?= h(t('重置 URL 安全入口')) ?></b>
        <span class="dim"><?= h(t('清空安全入口，回到「任何路径都能打开面板」。安全入口写错、自己被关在外面时用。')) ?></span>
    </div>
    <div class="setting-actions">
        <?php if ($can_edit_entry): ?>
            <form method="post" action="index.php?action=panel_emergency_reset_entry" class="inline setting-form">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <button type="submit" class="danger"><?= h(t('清空安全入口')) ?></button>
            </form>
        <?php else: ?>
            <span class="dim small"><?= h(t('只读：这一项现在只允许根管理员修改。')) ?></span>
        <?php endif; ?>
    </div>

    <div class="setting-actions">
        <b><?= h(t('解绑域名')) ?></b>
        <span class="dim small"><?= h(t('清空域名绑定，回到「任何 Host 都能访问」。换成别的域名访问不了时用。')) ?></span>
        <?php if ($can_edit_domain): ?>
            <form method="post" action="index.php?action=panel_emergency_unbind_domain" class="inline setting-form">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <button type="submit" class="danger"><?= h(t('解绑域名')) ?></button>
            </form>
        <?php endif; ?>
    </div>

    <div class="setting-actions">
        <b><?= h(t('关掉 HTTPS')) ?></b>
        <span class="dim small"><?= h(t('把 SSL 模式改回「不用 HTTPS」，回到纯 HTTP。证书文件一个都不删；证书配错、浏览器打不开时用。')) ?></span>
        <?php if ($can_edit_ssl): ?>
            <form method="post" action="index.php?action=panel_emergency_ssl_off" class="inline setting-form">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <button type="submit" class="danger"><?= h(t('关掉 HTTPS')) ?></button>
            </form>
        <?php endif; ?>
    </div>
</section>

<h3><?= h(t('网络与安全：面板自己要读的值')) ?></h3>

<p class="dim small">
    <?= h(t('下面这几个值是自带 nginx 直接读的：改完保存，根代理会在几秒内重新渲染并热加载。监听 IP / 端口还要让 compose 的端口映射跟着变（mclink apply-network）。')) ?>
</p>

<div class="flash info">
    <span class="mono"><?= h(sprintf(
        t('监听：%s；安全入口：%s；未认证返回码：%s'),
        $listen['nginx_listen'],
        $listen['entry_path'] === '' ? t('未设置') : $listen['entry_path'],
        $listen['unauth_code']
    )) ?></span>
</div>

<?php if (is_root()): ?>
    <h3><?= h(t('存储维护')) ?></h3>

    <section class="panel-block setting-item">
        <div class="setting-head">
            <b><?= h(t('初始化设置存储')) ?></b>
            <span class="dim"><?= h(t('重新建一遍设置表（幂等，不会删数据），并把配置文件备份里、数据库里还没有的键导入进来。数据库不可用时用这个把备份里的设置搬回去。')) ?></span>
        </div>
        <div class="setting-actions">
            <form method="post" action="index.php?action=panel_settings_init" class="inline setting-form">
                <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                <button type="submit" class="primary"><?= h(t('初始化 / 从配置文件导入')) ?></button>
            </form>
        </div>
    </section>
<?php endif; ?>

<?php require __DIR__ . '/layout_footer.php';
