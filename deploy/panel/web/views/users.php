<?php
/** Users: create accounts, reset passwords, enable/disable, kick. */

declare(strict_types=1);

$page_title = '账号';

$online = $stats['data']['online_accounts'] ?? [];

require __DIR__ . '/layout_header.php';
?>

<h2><?= h(t('账号管理')) ?></h2>

<p class="dim">
    <?= h(t('房主和玩家都必须有账号才能建房 / 进房。客户端')) ?><b><?= h(t('不提供注册功能')) ?></b><?= h(t('，所有账号都在这里创建和管理。密码使用 PBKDF2-SHA256（与中继完全相同的格式），这里创建的账号可以直接登录客户端。')) ?>
</p>

<div class="flash info">
    <b><?= h(t('几种操作的区别：')) ?></b>
    <br>· <b><?= h(t('禁用')) ?></b><?= h(t(' —— 对方无法再登录，并立即断开所有在线连接（账号保留，随时可恢复）')) ?>
    <br>· <b><?= h(t('删除')) ?></b><?= h(t(' —— 永久删除账号，立即断开连接，不可恢复（历史会话记录保留）')) ?>
    <br>· <b><?= h(t('踢下线')) ?></b><?= h(t(' —— 只是让 TA 重新登录，不影响账号状态')) ?>
    <br>· <b><?= h(t('重置密码')) ?></b><?= h(t(' —— 改密码并强制对方重新登录')) ?>
    <br>· <b><?= h(t('改名')) ?></b><?= h(t(' —— 用户名同时是')) ?><b><?= h(t('游戏内显示名和房间列表里的名字')) ?></b><?= h(t('。')) ?><b><?= h(t('立即生效，不会断开玩家连接')) ?></b><?= h(t('，管理员改名不限次数（玩家在客户端也能自助改名，但那边 30 天最多 3 次）')) ?>
    <br>· <b><?= h(t('设为管理员')) ?></b><?= h(t(' —— 可以登录这个面板（谨慎授予）')) ?>
</div>

<div class="flash info">
    <b><?= h(t('所有账号都能登录这个面板')) ?></b><?= h(t('，但只有')) ?><b><?= h(t('管理员')) ?></b><?= h(t('能改动任何东西。普通账号登录后是')) ?><b><?= h(t('只读')) ?></b><?= h(t('的：能看在线房间、在线玩家、账号列表和操作记录，但看不到任何按钮，直接构造请求也会被后端拒绝。')) ?>
</div>

<section class="panel-block">
    <h3><?= h(t('新建账号')) ?></h3>

    <?php if (!can_operate()): ?>
        <div class="flash info">
            <?= h(t('只读模式：你可以查看账号列表，但只有管理员能新建账号。')) ?>
        </div>
    <?php else: ?>
    <form method="post" action="index.php?action=user_create" class="form-row">
        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">

        <label>
            <?= h(t('用户名')) ?>
            <input type="text" name="username" required maxlength="32"
                   pattern="[^\s]+" title="<?= h(t('不能包含空格')) ?>">
        </label>

        <label>
            <?= h(t('密码（至少 6 位）')) ?>
            <input type="text" name="password" required minlength="6" maxlength="20"
                   pattern="[A-Za-z0-9]{6,20}"
                   title="<?= h(t('6-20 位，只能字母和数字')) ?>">
        </label>

        <?php
        // ---- 角色选择器：圆角方块的分段控件 -------------------------------
        //
        // 这里原来用的是 label.checkbox 包着单选框（radio）。CSS 里
        // `label.checkbox input { width: auto }`（那是给复选框写的）比
        // `input[type="radio"] { width: 17px }` 更具体，于是 appearance:none
        // 之后的单选框宽度被改成 auto —— 圆点被压成一条细缝，选中时那个 7px
        // 的圆点还会溢出到框外。用户截图里那个"坏掉的选择器"就是这个。
        //
        // 现在换成 .segmented：一排圆角方块，选中的那个用强调色填充（样式见
        // style.css）。原生 radio 仍然在 DOM 里，name / value / checked /
        // disabled 一个字没动 —— 点整块就能选，键盘 Tab 进来之后方向键在组内
        // 切换，表单提交的字段和值和以前完全一样。
        ?>
        <div class="segmented" role="radiogroup" aria-label="<?= h(t('账号角色')) ?>">
            <label class="segmented-option">
                <input type="radio" name="role" value="player" checked>
                <span class="segmented-label"><?= h(t('玩家（只读看板）')) ?></span>
            </label>

            <?php if (is_root()): ?>
                <label class="segmented-option">
                    <input type="radio" name="role" value="admin">
                    <span class="segmented-label"><?= h(t('管理员（可踢人 / 改账号 / 看房间密码）')) ?></span>
                </label>
            <?php else: ?>
                <?php
                // 普通管理员只能创建普通账号。选项是锁着的，不是隐藏 ——
                // 藏起来会让人以为面板坏了，锁着并说明原因才说得通。
                // index.php 里还会再挡一次（前端锁不是权限）。
                ?>
                <label class="segmented-option locked" title="<?= h(t('只有根管理员能创建管理员账号')) ?>">
                    <input type="radio" name="role" value="admin" disabled>
                    <span class="segmented-label"><?= h(t('管理员 🔒 只有根管理员能创建')) ?></span>
                </label>
            <?php endif; ?>
        </div>

        <button type="submit" class="primary"><?= h(t('创建账号')) ?></button>
    </form>

    <p class="dim small">
        <?= h(t('提示：密码框用明文显示，方便你把密码复制给朋友；创建后请让对方自行修改。')) ?>
        <br>
        <b><?= h(t('根管理员')) ?></b><?= h(t('不在这里创建 —— 一个服务器只有一个，它是中继首次启动时用')) ?> <code>MCLINK_BOOTSTRAP_ADMIN</code> <?= h(t('建立的那个账号，不能另设、不能转让。')) ?>
        <?php if (is_root()): ?>
            <br><?= h(t('你自己这个根管理员账号要改名字 / 改密码，去')) ?>
            <a href="index.php?action=account"><?= h(t('「我的账号」')) ?></a><?= h(t('改（改名和改密码对管理员不限制次数）。')) ?>
        <?php else: ?>
            <br><?= h(t('你（管理员）只能创建')) ?><b><?= h(t('玩家')) ?></b><?= h(t('账号；要开管理员账号请找根管理员。')) ?>
        <?php endif; ?>
    </p>
    <?php endif; ?>
</section>

<h2><?= h(sprintf(t('所有账号（%s）'), count($users))) ?></h2>

<table class="table users-table">
    <thead>
        <tr>
            <th><?= h(t('用户名')) ?></th>
            <th><?= h(t('角色')) ?></th>
            <th><?= h(t('状态')) ?></th>
            <th><?= h(t('在线')) ?></th>
            <th><?= h(t('登录次数')) ?></th>
            <th><?= h(t('最后登录')) ?></th>
            <th><?= h(t('最后 IP')) ?></th>
            <th><?= h(t('创建时间')) ?></th>
            <th><?= h(t('操作')) ?></th>
        </tr>
    </thead>
    <tbody>
    <?php foreach ($users as $user): ?>
        <?php
        $name = (string) $user['username'];
        $isOnline = in_array($name, $online, true);
        $isDisabled = (int) $user['is_disabled'] === 1;

        // Three tiers. The row's role decides BOTH its colour and whether the
        // signed-in account is allowed to touch it at all: nobody acts on
        // root, not even root.
        $rowRole = normalize_role($user['role'] ?? null, $user['is_admin'] ?? 0);
        $mayEdit = can_act_on($rowRole);
        $isMe = $name === (current_user()['username'] ?? '');
        ?>
        <tr class="<?= $isDisabled ? 'row-disabled' : '' ?>">
            <td>
                <strong><?= h($name) ?></strong>
                <?php if ($isMe): ?>
                    <span class="dim small"><?= h(t('（我）')) ?></span>
                <?php endif; ?>
            </td>
            <td><?= role_badge($rowRole) ?></td>
            <td>
                <?= $isDisabled
                    ? '<span class="pill bad">' . h(t('已禁用')) . '</span>'
                    : '<span class="pill ok">' . h(t('正常')) . '</span>' ?>
            </td>
            <td>
                <?= $isOnline
                    ? '<span class="pill warn">' . h(t('在线')) . '</span>'
                    : '<span class="dim">—</span>' ?>
            </td>
            <td><?= (int) $user['login_count'] ?></td>
            <td class="mono"><?= h((string) ($user['last_login_at'] ?: '-')) ?></td>
            <td class="mono"><?= h((string) ($user['last_login_ip'] ?: '-')) ?></td>
            <td class="mono"><?= h((string) $user['created_at']) ?></td>
            <td class="actions">
                <?php if (!can_operate()): ?>
                    <span class="dim small"><?= h(t('只读模式，无可执行操作')) ?></span>
                <?php elseif (!$mayEdit): ?>
                    <?php
                    // Root is untouchable from OTHER people's rows -- but it is
                    // not untouchable from itself. Saying "不可通过面板修改"
                    // next to a root row contradicted the 「我的账号」 page,
                    // which does exactly that. So the row points there.
                    ?>
                    <?php if ($isMe): ?>
                        <a class="button small" href="index.php?action=account">
                            <?= h(t('去「我的账号」修改')) ?>
                        </a>
                    <?php else: ?>
                        <span class="dim small">
                            <?= h(t('根管理员账号不能由别人修改')) ?><br>
                            <?= h(t('（本人可以在「我的账号」里改；忘记密码时用服务器上的 SSH 命令重置）')) ?>
                        </span>
                    <?php endif; ?>
                <?php else: ?>
                <?php /* 第一行：常用按钮（不需要输入） */ ?>
                <div class="action-row">
                    <?php if ($isOnline): ?>
                        <form method="post" action="index.php?action=user_kick" class="inline"
                              onsubmit="return confirm('<?= h(sprintf(t('断开 %s 的所有连接？'), $name)) ?>');">
                            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                            <input type="hidden" name="username" value="<?= h($name) ?>">
                            <button type="submit" class="small"><?= h(t('踢下线')) ?></button>
                        </form>
                    <?php endif; ?>

                    <form method="post" action="index.php?action=user_toggle" class="inline"
                          onsubmit="return confirm('<?= h(sprintf(t('%s %s ？'), $isDisabled ? t('启用') : t('禁用'), $name)) ?><?= $isDisabled ? '' : '\n\n' . h(t('会立即断开该账号的所有在线连接。')) ?>');">
                        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                        <input type="hidden" name="username" value="<?= h($name) ?>">
                        <input type="hidden" name="disabled" value="<?= $isDisabled ? '0' : '1' ?>">
                        <button type="submit" class="small <?= $isDisabled ? '' : 'danger' ?>">
                            <?= h($isDisabled ? t('启用') : t('禁用')) ?>
                        </button>
                    </form>

                    <?php if (!$isMe): ?>
                        <form method="post" action="index.php?action=user_delete" class="inline"
                              onsubmit="return confirm('<?= h(t('[!] 永久删除账号 ')) ?><?= h($name) ?><?= h(t(' ？')) ?>\n\n<?= h(t('会立即断开该账号的所有连接，且无法恢复。')) ?>\n<?= h(t('（历史会话记录会保留）')) ?>');">
                            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                            <input type="hidden" name="username" value="<?= h($name) ?>">
                            <button type="submit" class="small danger"><?= h(t('删除')) ?></button>
                        </form>
                    <?php endif; ?>

                    <?php
                    // ---- 提升 / 降级（只有根管理员）----
                    //
                    // Root itself never appears here: the row above already
                    // handled it (nobody may touch root, and that includes
                    // demoting it by accident).
                    ?>
                    <?php if (is_root() && !$isMe): ?>
                        <form method="post" action="index.php?action=user_role" class="inline"
                              onsubmit="return confirm('<?= h(sprintf(t('把 %s %s？'), $name, $rowRole === ROLE_ADMIN ? t('降级为玩家') : t('提升为管理员'))) ?><?= $rowRole === ROLE_ADMIN ? '\n\n' . h(t('降级后该账号的所有登录会立即失效，需要重新登录。')) : '' ?>');">
                            <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                            <input type="hidden" name="username" value="<?= h($name) ?>">
                            <input type="hidden" name="role"
                                   value="<?= $rowRole === ROLE_ADMIN ? ROLE_PLAYER : ROLE_ADMIN ?>">
                            <button type="submit" class="small">
                                <?= h($rowRole === ROLE_ADMIN ? t('降为玩家') : t('提升为管理员')) ?>
                            </button>
                        </form>
                    <?php endif; ?>
                </div>

                <?php /* 第二行：需要输入的两个操作 */ ?>
                <div class="action-row">
                    <form method="post" action="index.php?action=user_password" class="inline">
                        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                        <input type="hidden" name="username" value="<?= h($name) ?>">
                        <?php
                        // 这里仍然是明文输入框（type="text"）：这是管理员**给别人**
                        // 设的新密码，得看得见才能念给对方 —— 密码框原有的行为，
                        // 一个字没改。
                        //
                        // 刻意**不**包 .password-field、**不**放那个「显示密码」小
                        // 眼睛：明文框本来就是显示着的，再挂一个"显示"按钮只会多
                        // 一个点了没反应的小图标（用户报的就是这个）。
                        // 小眼睛只留在真正的密码框上：登录页和「我的账号」。
                        ?>
                        <input type="text" name="password" placeholder="<?= h(t('新密码（6-20 位字母数字）')) ?>" required
                               minlength="6" maxlength="20" class="field"
                               pattern="[A-Za-z0-9]{6,20}"
                               title="<?= h(t('6-20 位，只能字母和数字')) ?>">
                        <button type="submit" class="small"
                                onclick="return confirm('<?= h(sprintf(t('重置 %s 的密码？该账号当前登录状态会立即失效。'), $name)) ?>');">
                            <?= h(t('重置密码')) ?>
                        </button>
                    </form>

                    <form method="post" action="index.php?action=user_rename" class="inline">
                        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
                        <input type="hidden" name="username" value="<?= h($name) ?>">
                        <input type="text" name="new_username" placeholder="<?= h(t('新用户名')) ?>"
                               required minlength="2" maxlength="32" class="field"
                               pattern="[^\s]+" title="<?= h(t('不能包含空格')) ?>">
                        <button type="submit" class="small"
                                onclick="return confirm('<?= h(sprintf(t('把 %s 改名？'), $name)) ?>\n\n<?= h(t('立即生效，不会断开该玩家的连接。')) ?>');">
                            <?= h(t('改名')) ?>
                        </button>
                    </form>
                </div>
                <?php endif; ?>
            </td>
        </tr>
    <?php endforeach; ?>
    </tbody>
</table>

<?php require __DIR__ . '/layout_footer.php';
