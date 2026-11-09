<?php
/** Rooms: live room list with player sessions, kick and force close. */

declare(strict_types=1);

$page_title = '房间';

$room_list = $rooms['data'] ?? [];

// Runtime telemetry is display-only. Unknown/stale never means relay.
$route_label = static function (array $row): string {
    if (!empty($row['stale'])) return '状态未知／已过期';
    $labels = ['relay' => '中继', 'direct' => 'P2P 直连', 'mixed' => '部分直连',
               'transitioning' => '切换中', 'reconnecting' => '路径恢复中',
               'none' => '无游戏连接', 'unknown' => '未知'];
    $path = (string) ($row['active_path'] ?? 'unknown');
    $parts = [$labels[$path] ?? '未知'];
    if (($row['room_mode'] ?? '') === 'auto' && $path !== 'none') {
        $parts[] = !empty($row['relay_ready']) ? '中继备用就绪' : '中继备用未就绪';
        $reasons = ['relay_first' => '先由中继承载', 'direct_verified' => '直连验证通过',
                    'direct_unavailable' => '直连不可用，已回退', 'peer_fallback' => '对端请求回退',
                    'relay_unavailable' => '中继恢复中'];
        $reason = (string) ($row['last_reason'] ?? '');
        if ($reason !== '') $parts[] = $reasons[$reason] ?? '路径状态已更新';
    }
    if (!empty($row['direct_endpoint'])) $parts[] = '直连端点：' . $row['direct_endpoint'];
    $rtt = $row[$path === 'direct' ? 'direct_rtt_ms' : 'relay_rtt_ms'] ?? null;
    $parts[] = 'RTT：' . (is_numeric($rtt) ? round((float) $rtt) . ' ms' : '—');
    return implode(' · ', $parts);
};

// 区分「真正在线的房间」和「数据库里的残留记录」。
// 中继重启后内存里的房间会消失，但数据库里的行还在，
// 旧版面板会把它们混在一起显示成一堆没有房主的空房间。
$live_rooms = array_values(array_filter(
    $room_list, static fn($r) => ($r['live'] ?? true) !== false
));
$stale_rooms = array_values(array_filter(
    $room_list, static fn($r) => ($r['live'] ?? true) === false
));

require __DIR__ . '/layout_header.php';
?>

<h2><?= h(sprintf(t('在线房间（%s）'), count($live_rooms))) ?></h2>

<p class="dim">
    <?= h(t('这里可以对')) ?><b><?= h(t('任意状态')) ?></b><?= h(t('的房间执行强制关闭，也可以单独踢出一个玩家。客户端自己只能删除「自己的、且没有玩家」的房间，管理员不受此限制。')) ?>
</p>

<?php if ($live_rooms === []): ?>
    <p class="dim"><?= h(t('当前没有在线房间。房主启动客户端并点「开始连接」后会出现在这里。')) ?></p>
<?php else: ?>
    <?php foreach ($live_rooms as $room): ?>
        <?php
        // 房间有两个身份，别再混着用：
        //   room  房间 ID（room token）：ASCII、percent-encode 过的记号，
        //         协议和接口用它 —— 表单提交、关房请求都回传它。
        //   name  房间名：玩家看到的那个（中文、空格、emoji 都行），
        //         面板上给人看的文字一律用它。
        // 中继没报 name（老版本中继）时退回显示 ID，绝不留空白。
        $room_id   = (string) ($room['room'] ?? '');
        $room_name = (string) (($room['name'] ?? '') !== ''
            ? $room['name']
            : $room_id);

        // 房间名是任意字符，直接塞进 onsubmit 里的 JS 字符串，一个引号就能
        // 把字符串截断（点了确认等于没点）。json_encode() 生成合法的 JS
        // 字面量，JSON_HEX_* 保证里面不留裸引号，h() 再负责 HTML 属性那一层。
        $room_js = (string) json_encode(
            $room_name,
            JSON_UNESCAPED_UNICODE | JSON_HEX_APOS | JSON_HEX_QUOT
        );
        ?>
        <section class="room-block">
            <header class="room-head">
                <div>
                    <h3>
                        <?= h($room_name) ?>
                        <span class="pill ok"><?= h(t('在线')) ?></span>
                    </h3>
                    <span class="dim">
                        <?php
                        // 两个概念分开写清楚，别再让人以为"房主没被算进去"：
                        //   房主 = 建房的人（所有者，房间生命绑在他身上）
                        //   主机 = 当前开世界的那个位置（可能还没人认领）
                        ?>
                        <?= h(t('房主（建房的人）')) ?>
                        <?= h((string) (($room['owner_username'] ?? '') ?: ($room['host_username'] ?? '') ?: t('(未知)'))) ?>
                        · <?= h(t('当前主机')) ?>
                        <?= h((string) (($room['host_now'] ?? '') ?: t('(还没人认领)'))) ?>
                        · <?= h(t('房间号')) ?> <span class="mono"><?= h($room_id) ?></span>
                        · <?= h(t('客户端 ID')) ?> <?= h((string) ($room['host_client_id'] ?: '-')) ?>
                        · IP <?= h((string) ($room['host_ip'] ?: '-')) ?>
                    </span>
                </div>
                <div class="room-actions">
                    <span class="pill <?= (int) $room['players'] > 0 ? 'warn' : 'ok' ?>">
                        <?= h(sprintf(t('已加入世界 %s / %s'), (int) $room['players'], (int) $room['max_players'])) ?>
                    </span>
                    <span class="pill">
                        <?= h(sprintf(t('房间内在线 %s'), (int) ($room['members'] ?? 0))) ?>
                    </span>
                    <?php if (can_operate()): ?>
                    <?php
                    // 强制关闭 = 吊销房间。
                    //
                    // 和客户端的「删除我的房间」不同：客户端那条会拒绝
                    // 有玩家的房间（防止误踢朋友），而这里是管理员通道，
                    // 故意不做任何检查 —— 房间里有一个还是十个人、
                    // 是否正在 relay，都直接销毁。管理员点「关闭房间」
                    // 就是要关掉它。
                    //
                    // 两步确认：这是不可撤销的，而且会踢掉正在玩游戏的人。
                    //
                    // 这个表单由 layout_footer.php 里的等待页接管（它按
                    // form.js-room-close 找表单）：中继关房最多要等 25 秒，
                    // 同步提交会让页面卡住 30 秒没有任何反馈。没有 JS 时表单
                    // 照旧同步提交，功能不变。
                    ?>
                    <form method="post" action="index.php?action=room_revoke"
                          class="inline js-room-close"
                          data-room-name="<?= h($room_name) ?>"
                          onsubmit="return confirm('<?= h(t('强制关闭房间「')) ?>' + <?= h($room_js) ?> + '<?= h(t('」？')) ?>\n\n<?= h(t('这会把房间内的所有人（')) ?><?= (int) $room['players'] ?><?= h(t(' 名玩家）立即断开，并销毁房间，无法撤销。')) ?>\n\n<?= h(t('确定继续吗？')) ?>');">
                        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                        <input type="hidden" name="room" value="<?= h($room_id) ?>">
                        <button type="submit" class="danger small"><?= h(t('强制关闭房间')) ?></button>
                    </form>
                    <?php endif; ?>
                </div>
            </header>

            <div class="room-meta">
                <span>模式 <b><?= h(strtoupper((string) ($room['room_mode'] ?? '未知'))) ?></b></span>
                <span><?= h(t('空闲隧道')) ?> <b><?= (int) $room['idle_tunnels'] ?></b></span>
                <span><?= h(t('等待配对玩家')) ?> <b><?= (int) $room['waiting_players'] ?></b></span>
                <span><?= h(t('中继上行')) ?> <b><?= h(human_bytes((int) ($room['bytes_player_to_host'] ?? 0))) ?></b></span>
                <span><?= h(t('中继下行')) ?> <b><?= h(human_bytes((int) ($room['bytes_host_to_player'] ?? 0))) ?></b></span>
            </div>

            <?php
            // ---- 房间成员（踢出单个成员） ----
            //
            // 和下面的「会话」表是两回事，别混：
            //   * 成员 = 人在房间里（成员控制连接，可能还没进世界）
            //   * 会话 = 人已经在世界里（PLAYER 隧道，踢了人还在房间里）
            // 这一张表踢的是成员身份：人被移出房间，游戏会话一并断开。
            //
            // `member_list` 是中继快照里新增的键；老中继没有它 -> 空数组，
            // 表格自动不显示，面板其余部分照常工作（向后兼容）。
            $members = $room['member_list'] ?? [];
            ?>

            <?php if ($members !== []): ?>
                <h4 class="room-sub"><?= h(t('房间成员')) ?></h4>
                <table class="table compact">
                    <thead>
                        <tr>
                            <th><?= h(t('成员')) ?></th>
                            <th><?= h(t('身份')) ?></th>
                            <?php // v3.9.16：服务端早就下发 conn= / started=，这里补上渲染。 ?>
                            <th><?= h(t('连接状态')) ?></th>
                            <th>公网 IP（服务端观察）</th>
                            <th>实际路径／备用／最近原因</th>
                            <th><?= h(t('客户端 ID')) ?></th>
                            <th><?= h(t('空闲')) ?></th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>
                    <?php foreach ($members as $member): ?>
                        <?php
                        $member_label = (string) (
                            $member['label']
                            ?? ($member['username'] ?? '')
                        );

                        if ($member_label === '') {
                            $member_label = (string) ($member['client_id'] ?? '');
                        }

                        if ($member_label === '') {
                            $member_label = t('(未知)');
                        }

                        $member_cid       = (string) ($member['client_id'] ?? '');
                        $member_is_owner  = !empty($member['is_owner']);
                        $member_is_host   = !empty($member['is_host']);

                        // v3.9.16：渲染服务端已下发的连接状态。
                        //
                        // `conn` 的取值是白名单（idle|waiting|pairing|connected），
                        // 和客户端界面上那一行**同一个来源**，所以面板不会和
                        // 客户端各说各话。不认识的值一律按 idle 显示 —— 绝不把
                        // 一个看不懂的英文单词直接贴给用户。
                        $_conn_label_map = [
                            'connected' => t('已连接'),
                            'pairing'   => t('配对中'),
                            'waiting'   => t('等待中'),
                            'idle'      => t('未连接'),
                        ];
                        $_conn_raw   = (string) ($member['conn'] ?? '');
                        $_conn_pill  = [
                            'connected' => 'ok',
                            'pairing'   => 'warn',
                            'waiting'   => 'warn',
                            'idle'      => '',
                        ];
                        $_conn_label = $_conn_label_map[$_conn_raw]
                            ?? $_conn_label_map['idle'];
                        $_conn_class = $_conn_pill[$_conn_raw] ?? '';
                        // 「已开始连接」由客户端显式上报（STARTED），老客户端
                        // 恒为假 —— 那时只显示 conn，不编一个状态出来。
                        $_member_started = !empty($member['started']);
                        $_member_connecting = !empty($member['connecting']);

                        // 二次确认文案按身份分开。房主被踢 = 解散；主机被踢 =
                        // 当前所有玩家的游戏连接都会断（房间本身还在）。
                        if ($member_is_owner) {
                            $confirm_text = t('当前命中房主，踢出后房间会解散');
                        } elseif ($member_is_host) {
                            $confirm_text = t('当前命中玩家为 host，踢出后会断开当前所有玩家的游戏连接');
                        } else {
                            $confirm_text = t('踢出这个玩家？');
                        }

                        // label 是任意字符，直接塞进 onsubmit 的 JS 字符串里一个
                        // 引号就能截断（点了确认等于没点）。json_encode() 生成
                        // 合法 JS 字面量，JSON_HEX_* 保证不留裸引号，h() 再负责
                        // HTML 属性那一层 —— 和上面房间名的处理完全一样。
                        $member_js = (string) json_encode(
                            $member_label,
                            JSON_UNESCAPED_UNICODE | JSON_HEX_APOS | JSON_HEX_QUOT
                        );
                        $confirm_js = (string) json_encode(
                            $confirm_text,
                            JSON_UNESCAPED_UNICODE | JSON_HEX_APOS | JSON_HEX_QUOT
                        );
                        ?>
                        <tr>
                            <td><?= h($member_label) ?></td>
                            <td>
                                <?php if ($member_is_owner): ?>
                                    <span class="pill warn"><?= h(t('房主')) ?></span>
                                <?php endif; ?>
                                <?php if ($member_is_host): ?>
                                    <span class="pill ok"><?= h(t('主机')) ?></span>
                                <?php endif; ?>
                                <?php if (!$member_is_owner && !$member_is_host): ?>
                                    <span class="pill"><?= h(t('玩家')) ?></span>
                                <?php endif; ?>
                            </td>
                            <td>
                                <span class="pill <?= h($_conn_class) ?>"><?= h($_conn_label) ?></span>
                                <?php if ($_member_connecting && $_conn_raw === 'idle'): ?>
                                    <span class="dim small"><?= h(t('（已点开始连接）')) ?></span>
                                <?php endif; ?>
                            </td>
                            <td class="mono"><?= h((string) ($member['public_ip'] ?? '未知')) ?></td>
                            <td><?= h($route_label($member)) ?></td>
                            <td class="mono"><?= h($member_cid !== '' ? $member_cid : '-') ?></td>
                            <td><?= h(sprintf(t('%s 秒'), (int) round((float) ($member['idle'] ?? 0)))) ?></td>
                            <td>
                                <?php if (can_operate()): ?>
                                <form method="post" action="index.php?action=room_member_kick"
                                      class="inline"
                                      onsubmit="return confirm(<?= h($member_js) ?> + '\n\n' + <?= h($confirm_js) ?>);">
                                    <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                                    <input type="hidden" name="room" value="<?= h($room_id) ?>">
                                    <input type="hidden" name="client_id" value="<?= h($member_cid) ?>">
                                    <input type="hidden" name="label" value="<?= h($member_label) ?>">
                                    <button type="submit" class="danger small"><?= h(t('踢出')) ?></button>
                                </form>
                                <?php else: ?>
                                    <span class="dim small">—</span>
                                <?php endif; ?>
                            </td>
                        </tr>
                    <?php endforeach; ?>
                    </tbody>
                </table>
            <?php endif; ?>

            <?php $sessions = $room['sessions'] ?? []; ?>

            <?php
            // ================================================================
            //  ★ 用户 2026-09-29（第三轮）第 5 条
            // ================================================================
            // 有中继会话 -> 展示会话表（同样允许踢人）。它和上面那张主房间
            // 成员表**允许重复**，那是刻意的：一张是"房间里的人"，一张是
            // "中继上正在搬数据的会话"。
            //
            // 没有会话 -> **什么都不显示**。以前这里会打一行
            // 「房间里暂时没有玩家。」，那是只有中继功能的时代留下来的：
            // 空闲 / 直连模式下会话表本来就是空的，那行字只会让人以为房间没人。
            ?>
            <?php if ($sessions !== []): ?>
                <table class="table compact">
                    <thead>
                        <tr>
                            <th><?= h(t('会话')) ?></th>
                            <th><?= h(t('玩家')) ?></th>
                            <th>公网 IP（服务端观察）</th>
                            <th>实际路径／备用／最近原因</th>
                            <th><?= h(t('已连接')) ?></th>
                            <th><?= h(t('中继上行')) ?></th>
                            <th><?= h(t('中继下行')) ?></th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>
                    <?php foreach ($sessions as $session): ?>
                        <tr>
                            <td class="mono"><?= h((string) $session['session_id']) ?></td>
                            <td><?= h((string) ($session['player_username'] ?: '-')) ?></td>
                            <td class="mono"><?= h((string) ($session['player_ip'] ?: '-')) ?></td>
                            <td><?= h($route_label($session)) ?></td>
                            <td><?= h(seconds_to_human((int) round((float) ($session['duration'] ?? 0)))) ?></td>
                            <td><?= h(human_bytes((int) ($session['bytes_player_to_host'] ?? 0))) ?></td>
                            <td><?= h(human_bytes((int) ($session['bytes_host_to_player'] ?? 0))) ?></td>
                            <td>
                                <?php if (can_operate()): ?>
                                <form method="post" action="index.php?action=session_kick"
                                      class="inline"
                                      onsubmit="return confirm('<?= h(t('踢出这个玩家？')) ?>');">
                                    <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                                    <input type="hidden" name="session"
                                           value="<?= h((string) $session['session_id']) ?>">
                                    <button type="submit" class="danger small"><?= h(t('踢出')) ?></button>
                                </form>
                                <?php else: ?>
                                    <span class="dim small">—</span>
                                <?php endif; ?>
                            </td>
                        </tr>
                    <?php endforeach; ?>
                    </tbody>
                </table>
            <?php endif; ?>
        </section>
    <?php endforeach; ?>
<?php endif; ?>

<?php if ($stale_rooms !== []): ?>
    <h2><?= h(sprintf(t('残留记录（%s）'), count($stale_rooms))) ?></h2>

    <div class="flash warn">
        <b><?= h(t('这些房间里没有房主，是数据库里的历史残留记录。')) ?></b>
        <br><?= h(t('出现原因：中继重启后内存里的房间没了，但数据库里的记录还在（正常情况下中继会在 1 分钟内自动清掉；如果它们一直存在，就点下面的按钮）。')) ?>
        <br><?= h(t('它们')) ?><b><?= h(t('不影响联机')) ?></b><?= h(t('，只是看着碍眼。')) ?>
    </div>

    <table class="table">
        <thead>
            <tr>
                <th><?= h(t('房间号')) ?></th>
                <th><?= h(t('最后记录的房主')) ?></th>
                <th><?= h(t('最后更新')) ?></th>
                <th><?= h(t('操作')) ?></th>
            </tr>
        </thead>
        <tbody>
        <?php foreach ($stale_rooms as $room): ?>
            <?php
            // 残留行只有数据库里的 room_id（中继内存里已经没有这个房间，
            // 所以它报不出房间名）。同样按「有 name 用 name，没有就用 ID」
            // 的规矩显示，保证以后中继补上 name 时这里自动跟上。
            $stale_id   = (string) ($room['room'] ?? '');
            $stale_name = (string) (($room['name'] ?? '') !== ''
                ? $room['name']
                : $stale_id);

            $stale_js = (string) json_encode(
                $stale_name,
                JSON_UNESCAPED_UNICODE | JSON_HEX_APOS | JSON_HEX_QUOT
            );
            ?>
            <tr class="row-disabled">
                <td>
                    <strong><?= h($stale_name !== '' ? $stale_name : t('(空房间号)')) ?></strong>
                    <span class="pill"><?= h(t('残留')) ?></span>
                </td>
                <td><?= h((string) ($room['host_username'] ?: '-')) ?></td>
                <td class="mono"><?= h((string) ($room['updated_at'] ?: '-')) ?></td>
                <td>
                    <?php if (can_operate()): ?>
                    <?php
                    // 这个按钮也走 room_delete，而它第一步是中继的无条件吊销
                    // （残留行会让中继顺手清掉数据库里的那一行）—— 所以一样
                    // 交给等待页接管，只是提示文案换成"清理残留"。
                    //
                    // data-room-hint 是给脚本用的文案：PHP 按当前语言写好，
                    // 脚本不硬编码中文（英文界面下也是英文）。
                    ?>
                    <form method="post" action="index.php?action=room_delete"
                          class="inline js-room-close"
                          data-room-name="<?= h($stale_name) ?>"
                          data-room-hint="<?= h(t('正在清理数据库里的残留记录，中继会在几秒内回应')) ?>"
                          onsubmit="return confirm('<?= h(t('清理残留记录「')) ?>' + <?= h($stale_js) ?> + '<?= h(t('」？')) ?>');">
                        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                        <input type="hidden" name="room" value="<?= h($stale_id) ?>">
                        <button type="submit" class="small danger"><?= h(t('清理')) ?></button>
                    </form>
                    <?php else: ?>
                        <span class="dim small">—</span>
                    <?php endif; ?>
                </td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>

    <?php if (can_operate()): ?>
    <form method="post" action="index.php?action=room_purge_stale" class="inline"
          onsubmit="return confirm('<?= h(sprintf(t('清理全部 %s 条残留记录？'), count($stale_rooms))) ?>');">
        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
        <button type="submit" class="danger"><?= h(t('一键清理全部残留')) ?></button>
    </form>
    <?php endif; ?>
<?php endif; ?>

<h2><?= h(t('历史会话（最近 50 条）')) ?></h2>

<?php if ($history === []): ?>
    <p class="dim"><?= h(t('暂无记录。')) ?></p>
<?php else: ?>
    <table class="table">
        <thead>
            <tr>
                <th><?= h(t('开始时间')) ?></th>
                <th><?= h(t('房间')) ?></th>
                <th><?= h(t('玩家')) ?></th>
                <th>IP</th>
                <th><?= h(t('时长')) ?></th>
                <th><?= h(t('中继上行')) ?></th>
                <th><?= h(t('中继下行')) ?></th>
                <th><?= h(t('结束原因')) ?></th>
            </tr>
        </thead>
        <tbody>
        <?php foreach ($history as $row): ?>
            <tr>
                <td class="mono"><?= h((string) $row['started_at']) ?></td>
                <td><?= h((string) $row['room_id']) ?></td>
                <td><?= h((string) ($row['player_username'] ?: '-')) ?></td>
                <td class="mono"><?= h((string) ($row['player_ip'] ?: '-')) ?></td>
                <td><?= h(seconds_to_human($row['duration_sec'] !== null ? (int) $row['duration_sec'] : null)) ?></td>
                <td><?= h(human_bytes((int) $row['bytes_to_host'])) ?></td>
                <td><?= h(human_bytes((int) $row['bytes_to_player'])) ?></td>
                <td><?= h((string) ($row['end_reason'] ?: '-')) ?></td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>
<?php endif; ?>

<?php require __DIR__ . '/layout_footer.php';
