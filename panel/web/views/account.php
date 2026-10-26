<?php
/**
 * 我的账号 —— 修改自己的用户名和密码。
 *
 * WHY THIS PAGE EXISTS
 *
 * The account list deliberately refuses to touch the root account: nobody may
 * edit root, including root acting on root's row. That rule is what makes the
 * role hierarchy mean anything.
 *
 * But it left the root owner unable to change their OWN password from the
 * panel, which is absurd -- SSH would be needed for something as ordinary as
 * a password change. The rule should be "nobody edits root EXCEPT root", and
 * this page is that exception, expressed as a separate action so the account
 * list keeps its strict behaviour.
 *
 * SSH recovery (server/reset_password.py) stays as the last resort for the
 * case it exists for: you forgot the password and cannot log in at all.
 */

declare(strict_types=1);

$page_title = '我的账号';

$me = current_user() ?? [];
$myName = (string) ($me['username'] ?? '');
$myRole = normalize_role($me['role'] ?? null, $me['is_admin'] ?? 0);

// 本月还能自助修改几次。中继是唯一的计数方（客户端和面板共用同一个计数），
// 所以这里问中继，而不是在面板里自己算一份 —— 两份计数一定会对不上。
$budget = relay_api('GET', '/users/' . rawurlencode($myName) . '/self-service');
$budgetData = is_array($budget['data'] ?? null) ? $budget['data'] : [];
$unlimited = !empty($budgetData['unlimited']);
$remaining = $budgetData['remaining'] ?? null;
$limit = $budgetData['limit'] ?? null;
$budgetKnown = $budget['ok'] && $budgetData !== [];

require __DIR__ . '/layout_header.php';
?>

<h2><?= h(t('我的账号')) ?></h2>

<p class="dim">
    <?= h(t('这里改的是')) ?><b><?= h(t('你自己')) ?></b><?= h(t('的账号。两件事的后果不一样，分开说：')) ?>
</p>

<div class="flash info">
    <b><?= h(t('改名')) ?></b><?= h(t('：立即生效，')) ?><b><?= h(t('不会把你踢下线')) ?></b><?= h(t('。客户端和其它设备上的登录继续有效，游戏不用重连；新名字会出现在房间列表和面板里。')) ?>
    <br><br>
    <b><?= h(t('改密码')) ?></b><?= h(t('：')) ?><b><?= h(t('当前这个浏览器保持登录')) ?></b><?= h(t('，但')) ?>
    <b><?= h(t('其它设备（包括客户端）的登录会立即失效')) ?></b><?= h(t('，需要用新密码重新登录。这是应该的：改密码的目的就是把别处的登录踢掉。')) ?>
</div>

<?php if ($unlimited): ?>
    <p class="dim small">
        <?= h(t('你的角色是')) ?> <b><?= h(role_labels()[$myRole] ?? $myRole) ?></b><?= h(t('：')) ?>
        <b><?= h(t('改名和改密码都不限次数')) ?></b><?= h(t('（次数限制只针对普通玩家）。')) ?>
    </p>
<?php elseif ($budgetKnown): ?>
    <p class="dim small">
        <?= h(sprintf(t('普通账号每月可以自助修改 %s 次'), (int) $limit)) ?>
        <?= h(t('（')) ?><b><?= h(t('改名和改密码共用')) ?></b><?= h(t('这个次数，月初重置）。本月还剩')) ?>
        <b><?= (int) $remaining ?></b> <?= h(t('次。用完了可以等月初，或者请管理员在「账号」页帮你改（管理员不受限制）。')) ?>
    </p>
<?php else: ?>
    <p class="dim small">
        <?= h(t('暂时读不到剩余次数（中继没回应）。普通账号每月可以自助修改 3 次（改名和改密码共用），管理员及以上不受限制。')) ?>
    </p>
<?php endif; ?>

<?php if ($myRole === ROLE_ROOT): ?>
    <div class="flash info">
        <b><?= h(t('你是根管理员。')) ?></b>
        <?= h(t('根管理员账号不能被其他任何人修改（包括其他管理员），但')) ?><b><?= h(t('你自己可以在这里改')) ?></b><?= h(t(' —— 账号列表里那行会直接跳到本页。')) ?><br>
        <?= h(t('忘记密码、登不上面板时，用服务器上的这条命令重置：')) ?><br>
        <code>docker exec -it mc-relay python reset_password.py</code>
    </div>
<?php endif; ?>

<section class="panel-block">
    <h3><?= h(t('修改用户名')) ?></h3>

    <p class="dim small">
        <?= h(t('用户名同时是')) ?><b><?= h(t('游戏内显示名和房间列表里的名字')) ?></b><?= h(t('，也是你登录用的账号名。改完请用新用户名登录。')) ?>
        <b><?= h(t('已经在玩的人不会被断开。')) ?></b>
    </p>

    <form method="post" action="index.php?action=self_rename" class="form-row">
        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">

        <label>
            <?= h(t('当前用户名')) ?>
            <input type="text" value="<?= h($myName) ?>" disabled>
        </label>

        <label>
            <?= h(t('新的用户名')) ?>
            <input type="text" name="new_username" required maxlength="32"
                   minlength="2" pattern="[^\s]+"
                   title="<?= h(t('2-32 个字符，不能包含空格')) ?>"
                   placeholder="<?= h(t('2-32 个字符，不能有空格')) ?>">
        </label>

        <button type="submit" class="primary"><?= h(t('修改用户名')) ?></button>
    </form>
</section>

<section class="panel-block">
    <h3><?= h(t('修改密码')) ?></h3>

    <p class="dim small">
        <?= h(t('至少 6 位、最多 20 位，只能使用字母和数字（不能有符号或空格）。')) ?>
        <b><?= h(t('当前浏览器不会被踢下线')) ?></b><?= h(t('；其它设备和客户端上的登录会失效，需要用新密码重新登录。')) ?>
    </p>

    <form method="post" action="index.php?action=self_password" class="form-row">
        <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">

        <label>
            <?= h(t('当前密码')) ?>
            <span class="password-field">
                <input type="password" name="current_password" required
                       autocomplete="current-password">
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

        <label>
            <?= h(t('新密码（6-20 位，只能字母和数字）')) ?>
            <span class="password-field">
                <input type="password" name="new_password" required minlength="6"
                       maxlength="20" pattern="[A-Za-z0-9]{6,20}"
                       title="<?= h(t('6-20 位，只能字母和数字')) ?>"
                       autocomplete="new-password">
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

        <label>
            <?= h(t('再输一次新密码')) ?>
            <span class="password-field">
                <input type="password" name="confirm_password" required minlength="6"
                       maxlength="20" pattern="[A-Za-z0-9]{6,20}"
                       title="<?= h(t('6-20 位，只能字母和数字')) ?>"
                       autocomplete="new-password">
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

        <button type="submit" class="primary"><?= h(t('修改密码')) ?></button>
    </form>
</section>

<?php require __DIR__ . '/layout_footer.php';
