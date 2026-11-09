<?php
/**
 * Audit log: logins, rooms, connections, panel actions.
 *
 * 行是从 index.php 传进来的（$events），筛选也是**在那边做好的**：
 *   $filter_category / $filter_outcome 只用来把筛选条上的下拉框恢复成
 *   用户选中的样子，这里不发查询。
 *
 * 「操作」那一列的文案（以及色调、分类）来自 panel_event_meta()：左边是
 * 中继写进数据库的 action 代号（数据，永远不翻译，认不出代号时就直接把代号
 * 显示出来），右边是面板自己给它配的说明，跟着面板语言走。
 */

declare(strict_types=1);

$page_title = '操作日志';

$event_categories = panel_event_categories();
$event_outcomes = panel_event_outcomes();
$filter_active = $filter_category !== '' || $filter_outcome !== '';

require __DIR__ . '/layout_header.php';
?>

<h2><?= h(sprintf(t('操作日志（最近 %s 条）'), count($events))) ?></h2>

<div class="toolbar-row">
    <p class="dim" style="margin:0;">
        <?= h(sprintf(
            t('记录登录 / 登出、房间、连接和面板操作。日志保留 %d 天，超期自动清理，也可以随时手动清空。'),
            PANEL_EVENT_RETENTION_DAYS
        )) ?>
        <br>
        <?= h(t('「连接」里的进入 / 离开世界来自中继的会话记录，清空日志不会删掉它们。')) ?>
    </p>

    <?php if (can_operate()): ?>
        <form method="post" action="index.php?action=events_clear" class="inline"
              onsubmit="return confirm('<?= h(t('确定清空全部操作日志吗？')) ?>\n\n<?= h(t('这会删掉所有历史记录，无法恢复。')) ?>');">
            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
            <button type="submit" class="danger small"><?= h(t('清空日志')) ?></button>
        </form>
    <?php endif; ?>
</div>

<?php
// ---- 服务端日志下载 ---------------------------------------------------------
//
// 上面那一大段是**面板**的操作日志（数据库里的 relay_events）；下面这个按钮
// 下载的是**中继自己**的日志文件（relay.log：连接、配对、报错都在里面），
// 反馈问题时附上它最有用。只给能操作的人看，接口那边还会再挡一次
// （index.php 的 logs_download 走 require_operator）。
//
// 行数 / 大小来自 GET /logs/tail?lines=1（index.php 里的 $log_info）。读不到
// 就整段不显示 —— 中继还是旧版时这里绝不能冒出一句报错，点下载才会看到
// 「中继暂不支持日志导出」。
$log_total = null;
$log_bytes = null;

if (is_array($log_info['data'] ?? null)) {
    $log_total = isset($log_info['data']['total'])
        ? (int) $log_info['data']['total']
        : null;
    $log_bytes = isset($log_info['data']['bytes'])
        ? (int) $log_info['data']['bytes']
        : null;
}
?>

<?php if (can_operate()): ?>
    <div class="log-export">
        <form method="post" action="index.php?action=logs_download" class="inline">
            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
            <button type="submit" class="small"><?= h(t('下载服务端日志')) ?></button>
        </form>

        <?php
        // 清空服务端日志（relay.log）：内存缓冲 + 文件 + 轮转备份一起清。
        //
        // WHY A SECOND BUTTON: 「清空日志」清的是数据库里的**审计事件**，而这一
        // 个清的是**中继自己的日志文件**。用户清完之后发现"列表里还是原来那些"，
        // 因为那个列表读的是中继日志（/logs/tail），不是审计事件。两个东西来自
        // 两个地方，所以给两个按钮，各自说清自己清的是哪个。
        ?>
        <form method="post" action="index.php?action=logs_clear" class="inline"
              onsubmit="return confirm('<?= h(t('确定清空服务端日志吗？')) ?>\n\n<?= h(t('这会把中继的日志文件和内存缓冲一起清空，无法恢复。')) ?>');">
            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
            <button type="submit" class="danger small"><?= h(t('清空服务端日志')) ?></button>
        </form>

        <?php if ($log_total !== null && $log_bytes !== null): ?>
            <span class="dim small"><?= h(sprintf(
                t('中继日志当前：%d 行，%s'),
                $log_total,
                human_bytes($log_bytes)
            )) ?></span>
        <?php endif; ?>
    </div>

    <p class="dim small" style="margin:0 0 14px;">
        <?= h(t('中继把服务端日志保存在内存和磁盘上；下载得到的是原始服务端日志（不是这个页面上的操作日志），反馈问题时附上它最有用。')) ?>
    </p>
<?php endif; ?>

<?php
// ---- 筛选条 ---------------------------------------------------------------
//
// 两个下拉都是普通表单控件，提交走 GET（?category=&outcome=），不需要 JS，
// 也不和上面那个清空按钮抢同一个表单（表单不能嵌套）。默认不选 = 全部，
// 也就是和以前完全一样的那一屏。
?>
<form class="filter-bar" method="get" action="index.php">
    <input type="hidden" name="action" value="events">

    <label>
        <?= h(t('分类')) ?>
        <select name="category">
            <option value=""<?= $filter_category === '' ? ' selected' : '' ?>><?= h(t('全部')) ?></option>
            <?php foreach ($event_categories as $slug => $label): ?>
                <option value="<?= h($slug) ?>"<?= $filter_category === $slug ? ' selected' : '' ?>><?= h($label) ?></option>
            <?php endforeach; ?>
        </select>
    </label>

    <label>
        <?= h(t('结果')) ?>
        <select name="outcome">
            <option value=""<?= $filter_outcome === '' ? ' selected' : '' ?>><?= h(t('全部')) ?></option>
            <?php foreach ($event_outcomes as $slug => $label): ?>
                <option value="<?= h($slug) ?>"<?= $filter_outcome === $slug ? ' selected' : '' ?>><?= h($label) ?></option>
            <?php endforeach; ?>
        </select>
    </label>

    <button type="submit" class="small"><?= h(t('筛选')) ?></button>

    <?php if ($filter_active): ?>
        <a class="button small" href="index.php?action=events"><?= h(t('重置')) ?></a>
    <?php endif; ?>
</form>

<?php if ($events === []): ?>
    <p class="dim"><?= h(t('还没有日志。')) ?></p>
<?php else: ?>
    <table class="table events-table">
        <thead>
            <tr>
                <th><?= h(t('时间')) ?></th>
                <th><?= h(t('分类')) ?></th>
                <th><?= h(t('操作')) ?></th>
                <th><?= h(t('操作者')) ?></th>
                <th><?= h(t('房间')) ?></th>
                <th><?= h(t('会话')) ?></th>
                <th><?= h(t('来源 IP')) ?></th>
                <th><?= h(t('说明')) ?></th>
            </tr>
        </thead>
        <tbody>
        <?php foreach ($events as $event): ?>
            <?php
            // 认不出的 action 直接显示代号本身（数据），不留空白。
            $action = (string) $event['action'];
            $meta = panel_event_meta($action);
            ?>
            <tr>
                <td class="mono"><?= h((string) $event['created_at']) ?></td>
                <td><span class="cat <?= h($meta['category_class']) ?>"><?= h($meta['category_label']) ?></span></td>
                <td><span class="pill <?= h($meta['tone']) ?>"><?= h($meta['label']) ?></span></td>
                <td><?= h((string) ($event['actor'] ?: '-')) ?></td>
                <td><?= h((string) ($event['room_id'] ?: '-')) ?></td>
                <td class="mono"><?= h((string) ($event['session_id'] ?: '-')) ?></td>
                <td class="mono"><?= h((string) ($event['source_ip'] ?: '-')) ?></td>
                <td class="cell-detail"><?= h((string) ($event['detail'] ?: '-')) ?></td>
            </tr>
        <?php endforeach; ?>
        </tbody>
    </table>
<?php endif; ?>

<?php require __DIR__ . '/layout_footer.php';
