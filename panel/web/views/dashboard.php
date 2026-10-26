<?php
/** Dashboard: live service state, room summary and recent sessions. */

declare(strict_types=1);

$page_title = '仪表盘';

$service = $stats['data'] ?? [];
$room_list = $rooms['data'] ?? [];

require __DIR__ . '/layout_header.php';
?>

<h2><?= h(t('运行概览')) ?></h2>

<div class="cards overview-cards">
    <div class="card">
        <span class="card-label"><?= h(t('在线房间')) ?></span>
        <span class="card-value" data-live="rooms"><?= (int) ($service['rooms'] ?? 0) ?></span>
    </div>
    <div class="card">
        <span class="card-label"><?= h(t('房间内在线')) ?></span>
        <span class="card-value accent" data-live="members"><?= (int) ($service['members'] ?? 0) ?></span>
    </div>
    <div class="card">
        <span class="card-label"><?= h(t('已加入世界')) ?></span>
        <span class="card-value accent" data-live="players"><?= (int) ($service['players'] ?? 0) ?></span>
    </div>
    <?php // v3.9.16：按成员连接状态现算（服务端下发的 conn=），和房间页每一行的
          // 「连接状态」列同源。「正在连接」= 点了开始连接但还没进世界；老客户端
          // 不上报 STARTED，所以它恒为 0，而不是编一个数字出来。 ?>
    <div class="card">
        <span class="card-label"><?= h(t('已连接（按成员状态）')) ?></span>
        <span class="card-value" data-live="connected_members">0</span>
    </div>
    <div class="card">
        <span class="card-label"><?= h(t('空闲隧道')) ?></span>
        <span class="card-value" data-live="idle_tunnels"><?= (int) ($service['idle_tunnels'] ?? 0) ?></span>
    </div>
    <div class="card">
        <span class="card-label"><?= h(t('在线账号')) ?></span>
        <span class="card-value" data-live="online_accounts"><?= (int) ($service['online_account_count'] ?? 0) ?></span>
    </div>
</div>

<div class="cards">
    <div class="card small">
        <span class="card-label"><?= h(t('中继运行时长')) ?></span>
        <span class="card-value-sm" data-live="uptime_text">
            <?= h(seconds_to_human((int) round($service['uptime'] ?? 0))) ?>
        </span>
    </div>
    <div class="card small">
        <span class="card-label"><?= h(t('单个房间人数上限')) ?></span>
        <span class="card-value-sm" data-live="max_players">
            <?= (int) ($service['max_players']
                ?? ($service['max_players_per_room'] ?? 0)) ?>
        </span>
    </div>
    <div class="card small">
        <span class="card-label"><?= h(t('数据库（面板实测）')) ?></span>
        <span class="card-value-sm">
            <span class="pill <?= $panel_db_ok ? 'ok' : 'bad' ?>"
                  data-live="panel_db_ok"><?= h($panel_db_ok ? t('正常') : t('不可用')) ?></span>
            <span class="dim"><?= h(t('面板 ↔ MySQL')) ?></span>
        </span>
    </div>
    <div class="card small">
        <span class="card-label"><?= h(t('数据库（中继上报）')) ?></span>
        <span class="card-value-sm">
            <?= $relay_db_ok
                ? '<span class="pill ok">' . h(t('正常')) . '</span>'
                : '<span class="pill warn">' . h(t('异常')) . '</span>' ?>
            <span class="dim"><?= h((string) (($service['database']['backend'] ?? '') ?: '-')) ?></span>
        </span>
    </div>
    <div class="card small">
        <span class="card-label"><?= h(t('账号验证')) ?></span>
        <span class="card-value-sm">
            <?php $auth_required = $service['auth_required'] ?? null; ?>
            <?php if ($auth_required === null): ?>
                <span class="pill warn"><?= h(t('未知')) ?></span>
                <span class="dim"><?= h(t('中继未上报')) ?></span>
            <?php elseif (!empty($auth_required)): ?>
                <span class="pill ok"><?= h(t('已开启')) ?></span>
                <span class="dim"><?= h(t('必须登录')) ?></span>
            <?php else: ?>
                <span class="pill warn"><?= h(t('已关闭')) ?></span>
                <span class="dim"><?= h(t('无需登录')) ?></span>
            <?php endif; ?>
        </span>
    </div>
    <div class="card small">
        <span class="card-label"><?= h(t('面板请求数')) ?></span>
        <span class="card-value-sm">
            <?= (int) (($service['admin_api']['requests'] ?? 0)) ?>
        </span>
    </div>
</div>

<div class="cards">
    <div class="card small">
        <span class="card-label"><?= h(t('注册账号')) ?></span>
        <span class="card-value-sm">
            <?= (int) ($totals['users'] ?? 0) ?>
            <span class="dim"><?= h(sprintf(t('（禁用 %s）'), (int) ($totals['disabled'] ?? 0))) ?></span>
        </span>
    </div>
    <div class="card small">
        <span class="card-label"><?= h(t('历史会话')) ?></span>
        <span class="card-value-sm"><?= (int) ($totals['sessions'] ?? 0) ?></span>
    </div>
    <div class="card small">
        <span class="card-label"><?= h(t('累计流量')) ?></span>
        <span class="card-value-sm"><?= h(human_bytes((int) ($totals['bytes'] ?? 0))) ?></span>
    </div>
</div>

<h2><?= h(t('运行开关')) ?></h2>

<?php
$switch_data = $settings['data'] ?? [];
$require_account = $switch_data['require_account'] ?? null;
$account_on = $require_account !== null && !empty($require_account['value']);

// 房间人数上限。它和开关一样是「运行开关」，所以渲染在同一个区块里。
// 上下限来自中继自己上报的元数据，面板不再把 2 和 8 写第二遍 ——
// 两边各写一份，就是两边开始不一致的原因。
//
// IMPORTANT: this must render even when the relay is OLDER than the panel.
// An old relay answers /settings without a max_players entry, and an earlier
// version of this page then hid the whole control -- which is exactly the
// complaint "面板只能看人数上限，没地方改". The value falls back to what
// /stats reports, so the box is always there; saving then tells the truth
// about whether this relay supports it.
$capacity = $switch_data['max_players'] ?? null;
$capacity_now = $capacity !== null
    ? (int) ($capacity['value'] ?? 0)
    : (int) ($service['max_players'] ?? ($service['max_players_per_room'] ?? 0));
$capacity_min = (int) ($capacity['minimum'] ?? 2);
$capacity_max = (int) ($capacity['maximum'] ?? 64);
$capacity_editable = $capacity !== null;

// ============================================================
// 「允许不同版本的客户端连接」—— **开发调试开关**（默认关闭）
// ============================================================
//
// 这是中继上报的运行开关里的一项（和 require_account / max_players 同一个
// 机制、同一个接口），文案（label / help / live_effect）也是**中继报上来的
// 数据**，面板不翻译、不自己写第二份 —— 两边各写一份文案，就是两边开始
// 不一致的原因。
//
// 旧中继不认识这一项：`/settings` 里没有它，这里就是 null，整块不渲染。
// 绝不能因为一个开关而让整个设置页报错或空掉。
$version_bypass = $switch_data['allow_version_mismatch'] ?? null;
$bypass_on = $version_bypass !== null && !empty($version_bypass['value']);
?>

<?php if ($require_account === null): ?>
    <div class="flash warn">
        <b><?= h(t('无法从中继读取开关状态')) ?></b>
        <br>
        <?php if (!empty($settings['error'])): ?>
            <?= h(sprintf(t('中继返回的错误：%s'), (string) $settings['error'])) ?>
        <?php else: ?>
            <?= h(sprintf(t('中继接口没有返回 settings 数据，HTTP 状态码：%s'), (int) ($settings['status'] ?? 0))) ?>
        <?php endif; ?>
        <br>
        <?= h(t('排查顺序：')) ?>
        <br><?= h(t('1、')) ?><?= h(t('中继容器在跑吗：')) ?><code><?= h(deployment_hint('docker compose -f /opt/mc-link/server/docker-compose.yml ps')) ?></code>
        <br><?= h(t('2、')) ?><?= h(t('中继是新版吗：')) ?><code><?= h(deployment_hint('docker exec mc-relay grep -c settings_service relay_server.py')) ?></code><?= h(t('（大于 0 即新版）')) ?>
        <br><?= h(t('3、')) ?><?= h(t('面板能否连上中继：')) ?><code><?= h(deployment_hint('curl -s http://127.0.0.1:19102/_diag2.php')) ?></code>
    </div>
<?php else: ?>
    <section class="panel-block">
        <div class="switch-row">
            <div class="switch-text">
                <?php // label / help / live_effect 都是**中继**报上来的文案，
                      // 属于数据，不翻译。 ?>
                <b><?= h((string) $require_account['label']) ?></b>
                <span class="dim"><?= h((string) $require_account['help']) ?></span>
                <span class="dim small">
                    <?= h(t('切换后')) ?><b><?= h(t('不会断开任何在线玩家')) ?></b><?= h(t('：已登录/已在游戏的连接照常运行，只影响之后的连接。')) ?>
                </span>
                <?php if (!empty($require_account['live_effect'])): ?>
                    <span class="dim small">
                        <?= h((string) $require_account['live_effect']) ?>
                    </span>
                <?php endif; ?>
            </div>

            <div class="switch-action">
                <span class="pill <?= $account_on ? 'ok' : 'warn' ?>">
                    <?= h($account_on ? t('已开启') : t('已关闭')) ?>
                </span>

                <?php if (is_admin()): ?>
                    <form method="post" action="index.php?action=setting_save"
                          class="inline"
                          onsubmit="return confirm('<?= h(sprintf(t('%s「必须登录」？'), $account_on ? t('关闭', 'verb') : t('开启', 'verb'))) ?>\n\n<?= h(t('在线玩家不会被断开，只影响之后的连接。')) ?>');">
                        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                        <input type="hidden" name="name" value="require_account">
                        <input type="hidden" name="value" value="<?= $account_on ? '0' : '1' ?>">
                        <button type="submit" class="<?= $account_on ? 'danger' : 'primary' ?>">
                            <?= h($account_on ? t('关闭（允许免登录）') : t('开启（必须登录）')) ?>
                        </button>
                    </form>
                <?php else: ?>
                    <span class="dim small"><?= h(t('只有管理员可以修改')) ?></span>
                <?php endif; ?>
            </div>
        </div>

        <?php if ($account_on): ?>
            <p class="dim small">
                <?= h(t('当前状态说明：')) ?><b><?= h(t('开启', 'state')) ?></b><?= h(t(' —— 房主和玩家都必须用账号登录才能建房 / 进房。面板上「账号」页可以给朋友创建账号。')) ?>
            </p>
        <?php else: ?>
            <p class="dim small">
                <?= h(t('当前状态说明：')) ?><b><?= h(t('关闭', 'state')) ?></b><?= h(t(' —— 任何人不需要账号就能建房 / 进房（相当于早期版本的行为）。账号系统仍然可用，只是不再强制。')) ?>
            </p>
        <?php endif; ?>
    </section>

    <?php // 无论中继新旧，这一块都要显示：能改才是重点。 ?>
    <section class="panel-block">
        <div class="switch-row">
            <div class="switch-text">
                <b><?= h((string) ($capacity['label'] ?? t('单个房间的人数上限'))) ?></b>
                <span class="dim"><?= h((string) ($capacity['help']
                    ?? t('一个房间里最多能有多少人（含房主）。默认 8，可设置为 2-64 人；实际游戏人数取决于游戏服务端设置。'))) ?></span>
                <?php if (!empty($capacity['live_effect'])): ?>
                    <span class="dim small">
                        <?= h((string) $capacity['live_effect']) ?>
                    </span>
                <?php endif; ?>
                <?php // 这一句是 2026-09-15 改的规则：房间人数在【建房那一刻】定下并
                      // 锁死（要改只能删房重建），而隧道数量跟着房间人数走。
                      // 面板这个数字是【服务器上限】—— 给新房间用的天花板，
                      // 已经在跑的房间里的人数不会因为它而变。 ?>
                <span class="dim small">
                    <?= h(t('这是')) ?><b><?= h(t('服务器上限')) ?></b><?= h(t('：新建的房间不能超过它。')) ?>
                    <?= h(t('每个房间的人数在“建房那一刻”就定下了，之后不能修改 —— 要换人数请删掉房间重建。')) ?>
                </span>
                <span class="dim small">
                    <?= h(t('房主客户端开几条隧道，跟着【房间】的人数走，不跟房主本机设置走：')) ?><b><?= h(t('一个玩家一条')) ?></b><?= h(t('。所以换个人当房主，房间容量也不会变。')) ?>
                </span>
                <?php if (!$capacity_editable): ?>
                    <span class="dim small">
                        <b><?= h(t('注意：中继是旧版')) ?></b><?= h(t('，还没上报这个设置项的详细信息。下面显示的是中继当前生效的值；保存时如果中继不认，请先重建中继（')) ?><code><?= h(deployment_hint('bash /opt/mc-link/rebuild.sh relay')) ?></code><?= h(t('）。')) ?>
                    </span>
                <?php endif; ?>
            </div>

            <div class="switch-action">
                <span class="pill <?= $capacity_now > 0 ? 'ok' : 'warn' ?>">
                    <?= h(sprintf(t('当前 %s 人'), (int) $capacity_now)) ?>
                </span>

                <?php if (is_admin()): ?>
                    <form method="post" action="index.php?action=setting_save"
                          class="inline"
                          onsubmit="return confirm('<?= h(t('把单个房间的人数上限改成 ')) ?>' + this.value.value + '<?= h(t(' 人？')) ?>\n\n<?= h(t('已经在房间里的玩家不会被踢出，只影响之后进入的玩家。')) ?>');">
                        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                        <input type="hidden" name="name" value="max_players">
                        <input type="number" name="value" class="tiny"
                               min="<?= $capacity_min ?>" max="<?= $capacity_max ?>"
                               step="1" required
                               value="<?= (int) $capacity_now ?>">
                        <button type="submit" class="primary"><?= h(t('保存')) ?></button>
                    </form>
                    <span class="dim small">
                        <?= h(sprintf(t('范围 %s - %s 人（含房主），保存后立即生效'), $capacity_min, $capacity_max)) ?>
                    </span>
                <?php else: ?>
                    <span class="dim small"><?= h(t('只有管理员可以修改')) ?></span>
                <?php endif; ?>
            </div>
        </div>
    </section>

    <?php // ============================================================
          //  【开发调试】允许不同版本的客户端连接（默认关闭）
          //
          //  中继不报这一项（旧中继）时整块不显示。它是**开发调试**功能：
          //  开启后版本不一致的客户端也能连，可能造成未知 BUG —— 文案由
          //  中继提供（$version_bypass['help']），面板不另写一份。
          // ============================================================ ?>
    <?php if ($version_bypass !== null): ?>
        <section class="panel-block">
            <div class="switch-row">
                <div class="switch-text">
                    <b><?= h((string) ($version_bypass['label'] ?? '')) ?></b>
                    <span class="dim"><?= h((string) ($version_bypass['help'] ?? '')) ?></span>
                    <?php if (!empty($version_bypass['live_effect'])): ?>
                        <span class="dim small">
                            <?= h((string) $version_bypass['live_effect']) ?>
                        </span>
                    <?php endif; ?>
                    <span class="dim small">
                        <?= h(t('这是开发调试开关，不是给玩家用的；只应由管理员临时打开、测完立刻关掉。')) ?>
                    </span>
                </div>

                <div class="switch-action">
                    <span class="pill <?= $bypass_on ? 'bad' : 'ok' ?>">
                        <?= h($bypass_on ? t('已开启') : t('已关闭')) ?>
                    </span>

                    <?php if (is_admin()): ?>
                        <form method="post" action="index.php?action=setting_save"
                              class="inline"
                              onsubmit="return confirm('<?= h($bypass_on ? t('关闭「允许不同版本客户端连接」？') : t('开启「允许不同版本客户端连接」？')) ?>\n\n<?= h(t('这是开发调试功能：版本不一致的客户端连上来可能造成未知 BUG。')) ?>\n<?= h(t('只影响之后的新连接，不会断开在线玩家。')) ?>');">
                            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                            <input type="hidden" name="name" value="allow_version_mismatch">
                            <input type="hidden" name="value" value="<?= $bypass_on ? '0' : '1' ?>">
                            <button type="submit" class="<?= $bypass_on ? 'danger' : 'primary' ?>">
                                <?= h($bypass_on ? t('关闭（要求版本一致）') : t('开启（允许不同版本）')) ?>
                            </button>
                        </form>
                    <?php else: ?>
                        <span class="dim small"><?= h(t('只有管理员可以修改')) ?></span>
                    <?php endif; ?>
                </div>
            </div>

            <?php // v3.9.16：这个开关会自动到期，把"还剩多久"显示出来。 ?>
            <?php $_vb = $version_bypass['version_bypass'] ?? null; ?>
            <?php if ($bypass_on): ?>
                <p class="dim small">
                    <?= h(t('当前状态说明：')) ?><b><?= h(t('开启', 'state')) ?></b><?= h(t(' —— 版本不同的客户端现在能连上，可能造成未知 BUG。这是开发调试状态，测完请关掉。')) ?>
                </p>
                <?php if (is_array($_vb) && !empty($_vb['ttl_s']) && ($_vb['expires_in_s'] ?? null) !== null): ?>
                    <p class="dim small">
                        <?php if (!empty($_vb['active'])): ?>
                            <b><?= h(t('自动到期：')) ?></b><?= h(sprintf(
                                t('约 %d 分钟后自动关闭，不需要手动关（改这个开关会重新计时）。'),
                                (int) max(1, round((int) $_vb['expires_in_s'] / 60))
                            )) ?>
                        <?php else: ?>
                            <b><?= h(t('已自动到期：')) ?></b><?= h(t('开关已失效，版本门恢复强制校验。这里的显示还写着「开启」是因为数据库里的值没被改写 —— 重新关一次即可。')) ?>
                        <?php endif; ?>
                    </p>
                <?php endif; ?>
            <?php else: ?>
                <p class="dim small">
                    <?= h(t('当前状态说明：')) ?><b><?= h(t('关闭', 'state')) ?></b><?= h(t(' —— 客户端与服务器版本必须完全一致，不一致会被拒绝（正常状态）。')) ?>
                </p>
            <?php endif; ?>
        </section>
    <?php endif; ?>
<?php endif; ?>

<h2><?= h(t('当前房间（真正在线的）')) ?></h2>

<?php
$live_now = array_values(array_filter(
    $room_list, static fn($r) => ($r['live'] ?? true) !== false
));
$stale_now = array_values(array_filter(
    $room_list, static fn($r) => ($r['live'] ?? true) === false
));
?>

<?php if ($live_now === []): ?>
    <p class="dim">
        <?= h(t('当前没有在线房间。房主启动客户端并点「开始连接」后会出现在这里。')) ?>
        <?php if ($stale_now !== []): ?>
            <br><?= h(sprintf(t('（数据库里还有 %s 条历史残留记录，去'), count($stale_now))) ?>
            <a href="index.php?action=rooms"><?= h(t('房间页')) ?></a> <?= h(t('可以清理）')) ?>
        <?php endif; ?>
    </p>
<?php else: ?>
    <table class="table">
        <thead>
            <tr>
                <th><?= h(t('房间')) ?></th>
                <th><?= h(t('房主')) ?></th>
                <th><?= h(t('玩家')) ?></th>
                <th><?= h(t('空闲隧道')) ?></th>
                <th><?= h(t('房主 IP')) ?></th>
                <th><?= h(t('上行流量')) ?></th>
                <th><?= h(t('下行流量')) ?></th>
                <th></th>
            </tr>
        </thead>
        <tbody>
        <?php foreach ($live_now as $room): ?>
            <?php
            // 给人看的文字用房间名（中继的 name），操作回传房间 ID（room）。
            // 中继没报 name 时退回显示 ID，绝不留空白。
            $room_id   = (string) ($room['room'] ?? '');
            $room_name = (string) (($room['name'] ?? '') !== ''
                ? $room['name']
                : $room_id);

            // 房间名可以是任意字符（带引号也行），要塞进 onsubmit 里的 JS
            // 字符串就得先变成合法的 JS 字面量，否则一个引号就能把字符串
            // 截断，点了确认等于没点。
            $room_js = (string) json_encode(
                $room_name,
                JSON_UNESCAPED_UNICODE | JSON_HEX_APOS | JSON_HEX_QUOT
            );
            ?>
            <tr>
                <td title="<?= h(sprintf(t('房间号 %s'), $room_id)) ?>">
                    <strong><?= h($room_name) ?></strong>
                </td>
                <td><?= h((string) ($room['host_username'] ?: '-')) ?></td>
                <td>
                    <span class="pill <?= (int) $room['players'] > 0 ? 'warn' : 'ok' ?>">
                        <?= (int) $room['players'] ?> / <?= (int) $room['max_players'] ?>
                    </span>
                </td>
                <td><?= (int) $room['idle_tunnels'] ?></td>
                <td class="mono"><?= h((string) ($room['host_ip'] ?: '-')) ?></td>
                <td><?= h(human_bytes((int) ($room['bytes_player_to_host'] ?? 0))) ?></td>
                <td><?= h(human_bytes((int) ($room['bytes_host_to_player'] ?? 0))) ?></td>
                <td>
                    <?php if (can_operate()): ?>
                    <?php // 强制关闭 = room_revoke（无条件销毁）。
                          // 这里原先发的是 room_delete，也就是「只删数据库残留行」，
                          // 对活着的房间只会回一句「房间不存在」—— 面板上点了没反应，
                          // 就是这个原因。
                          //
                          // 表单带 js-room-close：由 layout_footer.php 里的等待页
                          // 接管（中继关房最多 25 秒，同步提交会让页面卡住没反馈）。 ?>
                    <form method="post" action="index.php?action=room_revoke"
                          class="inline js-room-close"
                          data-room-name="<?= h($room_name) ?>"
                          onsubmit="return confirm('<?= h(t('强制关闭房间「')) ?>' + <?= h($room_js) ?> + '<?= h(t('」？')) ?>\n\n<?= h(t('房间里的玩家会立即断开。')) ?>');">
                        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                        <input type="hidden" name="room" value="<?= h($room_id) ?>">
                        <input type="hidden" name="reason" value="<?= h(t('管理员在仪表盘强制关闭')) ?>">
                        <button type="submit" class="danger small"><?= h(t('强制关闭')) ?></button>
                    </form>
                    <?php endif; ?>
                </td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>
<?php endif; ?>

<h2><?= h(t('最近的玩家会话')) ?></h2>

<?php if ($recent === []): ?>
    <p class="dim"><?= h(t('还没有玩家会话记录。')) ?></p>
<?php else: ?>
    <table class="table">
        <thead>
            <tr>
                <th><?= h(t('时间')) ?></th>
                <th><?= h(t('房间')) ?></th>
                <th><?= h(t('玩家')) ?></th>
                <th>IP</th>
                <th><?= h(t('时长')) ?></th>
                <th><?= h(t('上行')) ?></th>
                <th><?= h(t('下行')) ?></th>
                <th><?= h(t('结束原因')) ?></th>
            </tr>
        </thead>
        <tbody>
        <?php foreach ($recent as $row): ?>
            <tr>
                <td class="mono"><?= h((string) $row['started_at']) ?></td>
                <td><?= h((string) $row['room_id']) ?></td>
                <td><?= h((string) ($row['player_username'] ?: '-')) ?></td>
                <td class="mono"><?= h((string) ($row['player_ip'] ?: '-')) ?></td>
                <td><?= h(seconds_to_human($row['duration_sec'] !== null ? (int) $row['duration_sec'] : null)) ?></td>
                <td><?= h(human_bytes((int) $row['bytes_to_host'])) ?></td>
                <td><?= h(human_bytes((int) $row['bytes_to_player'])) ?></td>
                <?php // end_reason 是数据库里的值（数据，不翻译）；
                      // 只有它为空时面板才自己写「已结束 / 进行中」。 ?>
                <td><?= h((string) ($row['end_reason'] ?: ($row['ended_at'] ? t('已结束') : t('进行中')))) ?></td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>
<?php endif; ?>

<?php require __DIR__ . '/layout_footer.php';
