<?php
/**
 * CraftLink Admin Panel -- front controller.
 *
 * All state changing operations go through this file, use POST, and are
 * protected by a CSRF token. Read-only pages are rendered by the views.
 *
 * The panel is a thin layer over:
 *   * the relay admin API  (live rooms, players, kick, force close)
 *   * MySQL                (accounts, history, audit log)
 */

declare(strict_types=1);

require __DIR__ . '/../lib/common.php';

// Never let a browser reuse an old panel page: the panel is updated by
// uploading files, and a cached copy is indistinguishable from a broken
// server. See no_cache_headers() for the bug this prevents.
no_cache_headers();

// 反代把面板挂在 /秘密路径/ 下时，浏览器如果不带结尾斜杠访问，
// 页面里所有相对地址都会以 / 为基准解析（样式表 404、按钮 404）。
// 这里先把它纠正过来，再渲染任何东西。
enforce_trailing_slash();

start_session();

// 语言 / 主题：把 ?lang= / ?theme= 记进 cookie，必须在任何输出之前
// （setcookie 要求响应头还没发出去）。不带这两个参数时，语言仍是中文、
// 主题仍是暗色 —— 也就是和以前一模一样。
panel_apply_preferences();

$action = (string) ($_GET['action'] ?? 'dashboard');

// ============================================================
// POST actions
// ============================================================

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    check_csrf();

    $post = $_POST;

    /**
     * 这次提交要不要用 JSON 回话（而不是 flash + 跳转）。
     *
     * 关房是有意慢的：中继先把「房间已关闭」通知给房间里的每个客户端，等他
     * 们自己退出，超时的才强制断开 —— 中继自己最多花 25 秒。同步提交的后果
     * 就是管理员对着一个完全没反应的页面等 30 秒，不知道是在干活还是已经死
     * 了。
     *
     * 所以房间页的关房表单改由浏览器端驱动（脚本在 views/layout_footer.php
     * 里）：它用 fetch 提交同一个表单，只是自己多带一个 ajax=1。带上就回
     * JSON、由页面显示秒表；不带（禁用 JS、或者有人直接 POST）还是走原来的
     * flash + 跳转，功能一点不少。
     *
     * 标记由脚本自己加，不是写死在表单里：万一本脚本没跑起来（浏览器太老），
     * 表单里就不会带 ajax=1，仍然走同步提交那条路。
     */
    $wants_json = trim((string) ($post['ajax'] ?? '')) !== '';

    /**
     * Refuse an action aimed at an account the signed-in role may not touch.
     *
     * The ONE exception is yourself. The root account cannot be edited by
     * anybody else -- not another admin, not root acting on root -- but its
     * owner must be able to change their own username and password from the
     * panel. Otherwise a typo in the root password would mean digging out SSH
     * for something as ordinary as a password change.
     *
     * SSH recovery stays as the LAST RESORT for the case it exists for:
     * having forgotten the password and therefore being unable to log in.
     *
     * The buttons are already hidden for the refused cases, but the panel is
     * a web app: hiding a button is a convenience, not a security control.
     * This is the control, and it covers every write action in one place so a
     * new action added later cannot forget to check.
     */
    $guard_target = static function (string $username): void {
        if ($username === '') {
            return;
        }

        $me = current_user()['username'] ?? '';

        if ($username === $me) {
            return;  // Editing yourself is always allowed.
        }

        $statement = db()->prepare(
            'SELECT role, is_admin FROM users WHERE username = ? LIMIT 1'
        );
        $statement->execute([$username]);
        $row = $statement->fetch();

        if ($row === false) {
            return;  // Nothing to protect; the action itself will report it.
        }

        $targetRole = normalize_role($row['role'] ?? null, $row['is_admin'] ?? 0);

        if (can_act_on($targetRole)) {
            return;
        }

        flash(
            t('根管理员账号不能被其他人修改。如果忘记了它的密码，请在服务器上运行：')
            . deployment_hint('docker exec -it mc-relay python reset_password.py'),
            'error'
        );
        redirect('index.php?action=users');
    };

    /**
     * 关房动作在还没碰到中继之前就放弃（例如表单里没有房间号）。
     *
     * 两种回话方式都在这里生成，JSON 那条不会和同步那条走偏。
     */
    $fail_room_action = static function (string $message) use ($wants_json): void {
        if ($wants_json) {
            json_out(['ok' => false, 'message' => '', 'error' => $message]);
        }

        flash($message, 'error');
        redirect('index.php?action=rooms');
    };

    /**
     * 关房动作的收尾：把中继的结果告诉管理员。
     *
     * ajax=1 -> JSON（房间页的等待页在读：ok=false + error 显示错误 + 刷新
     * 按钮，ok=true + message 显示中继的原话）；否则还是 flash + 跳转。
     *
     * 两边都用 relay_message()，所以管理员看到的是中继自己的措辞
     * （「断开 N 个游戏会话；M 名成员收到通知后自行退出」），不是面板编的。
     */
    $finish_room_action = static function (
        array $result, string $fallback, string $failure_prefix
    ) use ($wants_json): void {
        $message = $result['ok'] ? (relay_message($result) ?: $fallback) : '';

        if ($wants_json) {
            json_out([
                'ok'      => $result['ok'],
                'message' => $message,
                'error'   => $result['ok'] ? '' : (string) $result['error'],
            ]);
        }

        flash(
            $result['ok'] ? $message : $failure_prefix . $result['error'],
            $result['ok'] ? 'success' : 'error'
        );

        redirect('index.php?action=rooms');
    };

    /**
     * 记一笔「谁在面板上做了什么」。
     *
     * 中继自己也会为每一次面板调用记一行（actor 固定是 "panel"，因为它只看到
     * 面板那一个共享 token）。那行是"接口被调用了"的凭证，这一行是"是哪个人
     * 做的"：操作者、目标、浏览器 IP、时间、成没成功。两条一起看才完整。
     *
     * 只记「谁、对谁、做了什么」—— 密码之类的**值**永远不进日志。
     */
    $audit_op = static function (
        string $action, array $result, string $detail, string $room = '',
        string $session = ''
    ): void {
        audit_event($action, [
            'actor'      => (string) ((current_user() ?? [])['username'] ?? ''),
            'room_id'    => $room,
            'session_id' => $session,
            'detail'     => sprintf(
                t('%s：%s'),
                $detail,
                $result['ok'] ? t('成功') : t('失败')
            ),
        ]);
    };

    switch ($action) {
        // ---- login ----
        case 'login':
            $username = trim((string) ($post['username'] ?? ''));
            $password = (string) ($post['password'] ?? '');

            // Everything below touches the database, and a database problem
            // used to surface as a bare nginx 500 with NOTHING anywhere
            // explaining it -- which is unfixable from the outside.
            //
            // Reporting the real exception on the login page is deliberate.
            // The login page is reached before any authentication, so there is
            // no session to protect; what it must never do is leak a PASSWORD
            // or a hash. A driver error mentions table and column names only,
            // so it is safe, and it turns an unexplained 500 into a two minute
            // fix. h() escapes it on the way out.
            try {
                // Tolerant lookup: degrades gracefully when the database has
                // not been migrated to the three-tier roles yet.
                $user = fetch_login_user($username);

                if ($user === false || !login_user($user, $password)) {
                    // 审计：面板登录失败也记一笔 —— 账号名、原因、IP、时间。
                    // 中继只记客户端的 LOGIN，面板登录根本不经过它，所以这一
                    // 条只能由面板自己写。**绝不记密码本身。**
                    $reason = $user === false
                        ? t('账号不存在')
                        : ((int) ($user['is_disabled'] ?? 0) === 1
                            ? t('账号已被禁用')
                            : t('密码错误'));

                    audit_event('panel_login_failed', [
                        'actor'  => $username,
                        'detail' => sprintf(t('面板登录失败：%s'), $reason),
                    ]);

                    flash(t('用户名或密码错误，或账号已被禁用。'), 'error');
                    redirect('index.php?action=login');
                }

                $role = normalize_role(
                    $user['role'] ?? null, $user['is_admin'] ?? 0
                );

                $labels = role_labels();
            } catch (Throwable $e) {
                flash(
                    sprintf(t('登录处理失败（数据库错误）：%s'), $e->getMessage())
                    . "\n\n"
                    . deployment_hint(t('如果提示 users.role 不存在，说明数据库还没有迁移。把中继更新到最新版并重建即可（bash /opt/mc-link/rebuild.sh）。')),
                    'error'
                );
                redirect('index.php?action=login');
            }

            // 所有账号都能登录面板（方便玩家查看在线情况），
            // 但只有管理员能改动任何东西 —— 见 require_operator()。
            session_regenerate_id(true);

            // 审计：面板登录成功（含根管理员）。角色写进说明里，出问题时一眼
            // 看得出是谁、用什么身份进来的。
            audit_event('panel_login_ok', [
                'actor'  => (string) $user['username'],
                'detail' => sprintf(
                    t('面板登录成功（%s）'),
                    (string) ($labels[$role] ?? $role)
                ),
            ]);

            // The role goes into the session so every page can colour the
            // header and hide buttons without another query. normalize_role()
            // falls back to is_admin for a session created before the role
            // column existed.
            $_SESSION['user'] = [
                'username' => $user['username'],
                'is_admin' => is_role_staff($role) ? 1 : 0,
                'role' => $role,
            ];

            // A failure to record the login must not block the login itself:
            // this is bookkeeping, not authentication.
            try {
                db()->prepare(
                    'UPDATE users SET last_login_at = NOW(), last_login_ip = ?,
                                      login_count = login_count + 1
                     WHERE username = ?'
                )->execute([$_SERVER['REMOTE_ADDR'] ?? '', $user['username']]);
            } catch (Throwable $e) {
                // Ignored on purpose.
            }

            if ($role === ROLE_PLAYER) {
                flash(
                    sprintf(t('登录成功，%s。你的账号是【玩家】权限：可以查看所有信息，但不能修改。'), (string) $user['username']),
                    'success'
                );
            } else {
                flash(
                    sprintf(
                        t('登录成功，欢迎回来 %s（%s）。'),
                        (string) $user['username'],
                        (string) ($labels[$role] ?? $role)
                    ),
                    'success'
                );
            }

            redirect('index.php?action=dashboard');

        // ---- logout ----
        case 'logout':
            // 审计：谁退出了。要在清空 $_SESSION 之前拿到账号名 —— 清空之后
            // current_user() 就没人了，日志里只会剩一个空操作者。
            $logoutUser = (string) ((current_user() ?? [])['username'] ?? '');

            if ($logoutUser !== '') {
                audit_event('panel_logout', [
                    'actor'  => $logoutUser,
                    'detail' => t('退出面板登录'),
                ]);
            }

            $_SESSION = [];
            session_destroy();
            redirect('index.php?action=login');

        // ---- create a normal account ----
        case 'user_create':
            require_operator();

            $username = trim((string) ($post['username'] ?? ''));
            $password = (string) ($post['password'] ?? '');

            // The role comes from the form. Root cannot be created, because
            // there is exactly one and it already exists.
            $requested = strtolower(trim((string) ($post['role'] ?? ROLE_PLAYER)));

            if (!in_array($requested, [ROLE_ADMIN, ROLE_PLAYER], true)) {
                flash(t('只能创建「管理员」或「玩家」账号。'), 'error');
                redirect('index.php?action=users');
            }

            // Only ROOT may create administrators. The form locks the option
            // for everybody else, but a locked radio button is not a
            // permission: the check has to exist here too.
            if ($requested === ROLE_ADMIN && !is_root()) {
                flash(
                    t('只有根管理员能创建管理员账号。你（管理员）可以创建玩家账号。'),
                    'error'
                );
                redirect('index.php?action=users');
            }

            $asAdmin = $requested === ROLE_ADMIN;

            // 密码规则和中继一致：6-20 位、只能字母数字。
            $problem = password_problem($password);

            if ($problem !== '') {
                flash($problem, 'error');
                redirect('index.php?action=users');
            }

            if ($username === '' || $password === '') {
                flash(t('用户名和密码不能为空。'), 'error');
                redirect('index.php?action=users');
            }

            // `role` is what the relay writes into the users table; `is_admin`
            // is kept for older relays that only understand the flag. Sending
            // only is_admin is what made every created "管理员" turn up as a
            // 普通玩家: the role column kept its 'player' default.
            $result = relay_api('POST', '/users', [
                'username' => $username,
                'password' => $password,
                'role' => $requested,
                'is_admin' => $asAdmin,
                'actor' => (string) ((current_user() ?? [])['username'] ?? ''),
            ]);

            // 审计：谁创建了哪个账号、什么角色、成没成功（密码的值不记）。
            $audit_op('panel_create_user', $result, sprintf(
                t('创建账号 %s（%s）'),
                $username,
                (string) (role_labels()[$requested] ?? $requested)
            ));

            flash(
                $result['ok']
                    ? sprintf(
                        t('账号 %s 创建成功（%s）。'),
                        $username,
                        (string) (role_labels()[$requested] ?? $requested)
                    )
                    : sprintf(t('创建失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=users');

        // ---- reset a password ----
        case 'user_password':
            require_operator();

            $username = (string) ($post['username'] ?? '');
            $guard_target($username);
            $password = (string) ($post['password'] ?? '');

            // 和中继同一套规则（6-20 位、只能字母数字）。中继也会再挡一次，
            // 但在这里先挡能立刻给出中文原因，也避免一次没必要的往返。
            $problem = password_problem($password);

            if ($problem !== '') {
                flash(sprintf(t('%s（重置密码）'), $problem), 'error');
                redirect('index.php?action=users');
            }

            $result = relay_api(
                'PUT',
                '/users/' . rawurlencode($username),
                ['password' => $password]
            );

            // 审计：重置了谁的密码、成功与否（新密码本身不记）。
            $audit_op('panel_set_password', $result, sprintf(
                t('目标账号 %s'),
                $username
            ));

            flash(
                $result['ok']
                    ? sprintf(t('已重置 %s 的密码（该账号的登录状态已被强制失效）。'), $username)
                    : sprintf(t('重置失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=users');

        // ---- clear the audit log ----
        case 'events_clear':
            require_operator();

            $result = relay_api('POST', '/events/clear', null, 20);

            // 审计：清空日志这个动作本身要留下来 —— 而且必须是**在**中继清空
            // 之后再写，否则这一行会被自己删掉。这是唯一一条能在清空后还看得
            // 见"是谁清空的"的记录（中继那行的 actor 是 panel）。
            $audit_op('panel_clear_events', $result, t('清空操作日志'));

            flash(
                $result['ok']
                    ? (relay_message($result) ?: t('已清空操作日志。'))
                    : sprintf(t('清空失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=events');

        // ---- clear the relay's own server log ----
        //
        // 和 events_clear 是两件事：那个清数据库里的事件，这个清中继的日志
        // 文件与内存缓冲。用户报的「手动删除日志后列表内容也要一并清理」就是
        // 这一条 —— 页面上那个「中继日志当前：N 行」以及预览读的是
        // /logs/tail，所以必须让中继自己也清掉，光清数据库不动它。
        //
        // 审计写在**清空之后**：这一行要能回答"谁清的"，所以它不能属于被清掉的
        // 那一批。
        case 'logs_clear':
            require_operator();

            $result = relay_api('POST', '/logs/clear', null, 20);

            $audit_op('panel_clear_relay_log', $result, t('清空服务端日志'));

            if ($result['ok']) {
                $cleared = $result['data']['cleared'] ?? [];
                $flash = sprintf(
                    t('已清空服务端日志（%d 行，%d 个轮转备份）。'),
                    (int) ($cleared['lines'] ?? 0),
                    (int) ($cleared['backups_removed'] ?? 0)
                );
            } else {
                // 501 = 中继还是旧版，没有这个接口。说清楚要更新服务端，
                // 而不是显示成"清空了"却没有。
                $flash = ((int) ($result['status'] ?? 0) === 501)
                    ? t('中继暂不支持清空服务端日志（需要更新服务端）')
                    : sprintf(
                        t('清空服务端日志失败：%s'),
                        (string) ($result['error'] ?? '')
                    );
            }

            flash($flash, $result['ok'] ? 'success' : 'error');

            // 重新拉一遍：清空之后页面上的行数和列表必须是**新的**读数。
            redirect('index.php?action=events');

        // ---- download the relay's own server log ----
        //
        // 管理员点「下载服务端日志」时走这里：中继把日志正文原样发过来，面板
        // **原样转发**给浏览器 —— 不包 HTML、不 redirect、不 json_out。
        //
        // 为什么是 POST 而不是 <a href>：面板的写操作全部走 POST + 全局的
        // check_csrf()（见上面的 $_SERVER['REQUEST_METHOD'] === 'POST' 分支），
        // 下载跟着同一条路走，就不必为它单独开一条免 CSRF 的 GET 通道。浏览器
        // 收到带 Content-Disposition: attachment 的响应就直接存盘，页面停在原处。
        case 'logs_download':
            require_operator();

            // 30 秒：中继要把最多几 MB 的日志打包发过来，默认的 6 秒不够。
            $export = relay_api_raw('GET', '/logs/export', 30);

            if (!$export['ok']) {
                // 中继还没有这个接口（旧版）时给一句人话 —— 直接 500 或者一个
                // 空白页会让人以为面板坏了。
                flash(
                    $export['status'] === 404
                        ? t('中继暂不支持日志导出（需要更新服务端）')
                        : sprintf(
                            t('下载服务端日志失败：%s'),
                            $export['error'] !== ''
                                ? $export['error']
                                : sprintf(t('中继返回 HTTP %d'), $export['status'])
                        ),
                    'error'
                );

                redirect('index.php?action=events');
            }

            // 文件名优先用中继给的那个（它知道日志覆盖的时间段）；中继没给、
            // 或者给的名字里带引号 / 路径分隔符之类的东西时，面板按当前时间自己
            // 拼一个。白名单正则保证这个头里永远塞不进第二个字段。
            $filename = 'craftlink-relay-' . date('Ymd-His') . '.log';

            if (preg_match(
                '/filename="?([A-Za-z0-9._-]{1,80})"?/',
                (string) $export['content_disposition'],
                $name_match
            ) === 1) {
                $filename = $name_match[1];
            }

            // 下载头必须发在**任何正文之前**。走到这里响应体还是空的：index.php
            // 顶部只调过 no_cache_headers() 和 panel_apply_preferences()（两个都
            // 只发响应头、不产出正文），所以 headers_sent() 仍然是 false ——
            // 这里发自己的头不会触发"headers already sent"警告，也不会出现
            // "PHP 已经输出过，下载文件里混进一段 HTML"的经典事故。
            //
            // 这条路径上不许出现 no_cache_headers() 之外的 HTML、也不许 json_out：
            // 只要先 echo 了一个字符，下面的 header() 就全部失效，浏览器拿到的
            // 会是一个内容被污染的 .log。
            if (!headers_sent()) {
                header('Content-Type: text/plain; charset=utf-8');
                header('Content-Disposition: attachment; filename="' . $filename . '"');
                header('Content-Length: ' . strlen($export['body']));
                header('X-Content-Type-Options: nosniff');
            }

            // 原样吐出中继的日志正文，然后结束（不 redirect、不包 HTML）。
            echo $export['body'];
            exit;

        // ---- promote / demote an existing account (root only) ----
        case 'user_role':
            require_operator();

            $username = (string) ($post['username'] ?? '');
            $guard_target($username);

            // Only root, and the relay checks again by looking the actor up --
            // a hidden form field is not a permission.
            if (!is_root()) {
                flash(t('只有根管理员能修改别人的权限。'), 'error');
                redirect('index.php?action=users');
            }

            $role = strtolower(trim((string) ($post['role'] ?? '')));

            if (!in_array($role, [ROLE_ADMIN, ROLE_PLAYER], true)) {
                flash(t('权限只能是「管理员」或「玩家」。'), 'error');
                redirect('index.php?action=users');
            }

            $result = relay_api(
                'PATCH',
                '/users/' . rawurlencode($username),
                [
                    'role' => $role,
                    'actor' => (string) ((current_user() ?? [])['username'] ?? ''),
                ]
            );

            // 审计：谁被提权 / 降权，改成了什么。
            $audit_op('panel_set_role', $result, sprintf(
                t('目标账号 %s → %s'),
                $username,
                (string) (role_labels()[$role] ?? $role)
            ));

            flash(
                $result['ok']
                    ? (relay_message($result) ?: sprintf(
                        t('已把 %s 设为 %s。'),
                        $username,
                        (string) (role_labels()[$role] ?? $role)
                    ))
                    : sprintf(t('修改失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=users');

        // ---- enable / disable ----
        case 'user_toggle':
            require_operator();

            $username = (string) ($post['username'] ?? '');
            $guard_target($username);
            $disabled = !empty($post['disabled']);

            $result = relay_api(
                'PATCH',
                '/users/' . rawurlencode($username),
                ['disabled' => $disabled]
            );

            $audit_op(
                $disabled ? 'panel_disable_user' : 'panel_enable_user',
                $result,
                sprintf(
                    t('%s账号 %s'),
                    $disabled ? t('禁用') : t('启用'),
                    $username
                )
            );

            flash(
                $result['ok']
                    ? ($disabled
                        ? sprintf(t('已禁用 %s。'), $username)
                        : sprintf(t('已启用 %s。'), $username))
                    : sprintf(t('操作失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=users');

        // ---- rename an account ----
        case 'user_rename':
            require_operator();

            $username = (string) ($post['username'] ?? '');
            $guard_target($username);
            $newName  = trim((string) ($post['new_username'] ?? ''));

            $result = relay_api(
                'POST',
                '/users/' . rawurlencode($username) . '/rename',
                ['username' => $newName]
            );

            $audit_op('panel_rename_user', $result, sprintf(
                t('改名 %s → %s'),
                $username,
                $newName
            ));

            flash(
                $result['ok']
                    ? (relay_message($result) ?: sprintf(t('已改名为 %s'), $newName))
                    : sprintf(t('改名失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            // 管理员改名不踢下线，立刻生效；如果改的是自己，刷新一下顶栏即可。
            redirect('index.php?action=users');

        // ---- change MY OWN username ----
        case 'self_rename':
            require_login();

            $me = current_user() ?? [];
            $myName = (string) ($me['username'] ?? '');
            $newName = trim((string) ($post['new_username'] ?? ''));

            if ($myName === '') {
                flash(t('登录状态异常，请重新登录。'), 'error');
                redirect('index.php?action=account');
            }

            if ($newName === '' || $newName === $myName) {
                flash(t('新用户名不能为空，也不能和当前用户名相同。'), 'error');
                redirect('index.php?action=account');
            }

            $result = relay_api(
                'POST',
                '/users/' . rawurlencode($myName) . '/rename',
                [
                    'username' => $newName,
                    // 自助改名：计入每月 3 次（管理员不计）。
                    'self_service' => true,
                    'actor' => $myName,
                ]
            );

            if ($result['ok']) {
                // The session carries the old name, and every later request
                // uses it as the actor. Leaving it stale would make the next
                // action act on an account that no longer exists.
                $_SESSION['user']['username'] = $newName;

                flash(
                    relay_message($result)
                    ?: sprintf(t('用户名已改为 %s，立即生效。'), $newName),
                    'success'
                );
            } else {
                flash(sprintf(t('改名失败：%s'), (string) $result['error']), 'error');
            }

            // 审计：自助改名（改名不踢下线，只影响之后的新连接）。
            // 放在这里：actor 用的是改完之后的名字（还是同一个人），
            // 说明里保留了改名前后两个名字。
            $audit_op('panel_self_rename', $result, sprintf(
                t('自助改名 %s → %s'),
                $myName,
                $newName
            ));

            redirect('index.php?action=account');

        // ---- change MY OWN password ----
        case 'self_password':
            require_login();

            $me = current_user() ?? [];
            $myName = (string) ($me['username'] ?? '');

            $current = (string) ($post['current_password'] ?? '');
            $new = (string) ($post['new_password'] ?? '');
            $confirm = (string) ($post['confirm_password'] ?? '');

            if ($new !== $confirm) {
                flash(t('两次输入的新密码不一致。'), 'error');
                redirect('index.php?action=account');
            }

            $problem = password_problem($new);

            if ($problem !== '') {
                flash(sprintf(t('%s（新密码）'), $problem), 'error');
                redirect('index.php?action=account');
            }

            // Re-verify the CURRENT password. Being logged in is not proof
            // that the person at the keyboard is the account owner -- an
            // unattended session must not be enough to take the account over.
            $statement = db()->prepare(
                'SELECT password_hash FROM users WHERE username = ? LIMIT 1'
            );
            $statement->execute([$myName]);
            $row = $statement->fetch();

            if ($row === false || !verify_password($current, (string) $row['password_hash'])) {
                flash(t('当前密码不正确。'), 'error');
                redirect('index.php?action=account');
            }

            $result = relay_api(
                'PUT',
                '/users/' . rawurlencode($myName),
                [
                    'password' => $new,
                    // 自助改密码：和改名共用每月 3 次（管理员不计）。
                    'self_service' => true,
                ]
            );

            // 审计：谁改了自己的密码（新密码、旧密码都不记）。
            $audit_op('panel_set_password', $result, sprintf(
                t('修改自己的密码：%s'),
                $myName
            ));

            flash(
                $result['ok']
                    ? t('密码已修改。当前浏览器保持登录；其它设备和客户端需要重新登录。')
                    : sprintf(t('修改失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=account');

        // ---- runtime settings (panel switches) ----
        case 'setting_save':
            require_operator();

            $name  = trim((string) ($post['name'] ?? ''));
            $value = trim((string) ($post['value'] ?? ''));

            if ($name === '') {
                flash(t('没有指定要修改的设置项。'), 'error');
                redirect('index.php?action=dashboard');
            }

            // The value is forwarded AS TYPED, and the RELAY decides what it
            // means: it knows each setting's type, its bounds and its error
            // message.
            //
            // This used to be forced into a boolean here --
            //     $value === '1' || $value === 'true'
            // -- which was invisible while the only setting was a switch. The
            // moment the room capacity arrived, "6" became false, the relay
            // answered "单个房间的人数上限必须是数字", and the panel reported a
            // failure for a value the user had typed correctly.
            $result = relay_api('PUT', '/settings', [$name => $value]);

            // 审计：改了哪个开关、改成了什么（"从什么改成什么"里的旧值由中继
            // 自己记的那行 setting_changed 给出：它读得到旧值）。
            $audit_op('setting_changed', $result, sprintf(
                t('设置 %s = %s'),
                $name,
                $value
            ));

            $failure = sprintf(t('保存失败：%s'), (string) $result['error']);

            if (!$result['ok'] && strpos($result['error'], '未知的设置项') !== false) {
                // The relay predates this setting. Saying only "未知的设置项"
                // sends people looking for a typo in a form they filled in
                // correctly.
                //
                // NOTE: the needle above is the RELAY's own error text, so it
                // is data and must stay untranslated -- translating it would
                // break the match.
                $failure .= "\n" . deployment_hint(t('（中继还是旧版，它不认识这个设置项 —— 先在服务器上跑 bash /opt/mc-link/rebuild.sh relay）'));
            }

            flash(
                $result['ok']
                    ? (relay_message($result) ?: t('设置已保存'))
                    : $failure,
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=dashboard');

        // ---- delete an account permanently ----
        case 'user_delete':
            require_operator();

            $username = (string) ($post['username'] ?? '');
            $guard_target($username);
            $me = current_user()['username'] ?? '';

            // Safety rails: never let an admin lock themselves out.
            if ($username === $me) {
                flash(t('不能删除自己的账号。'), 'error');
                redirect('index.php?action=users');
            }

            $adminCount = (int) db()->query(
                'SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_disabled = 0'
            )->fetchColumn();

            $target = db()->prepare(
                'SELECT is_admin FROM users WHERE username = ? LIMIT 1'
            );
            $target->execute([$username]);
            $targetRow = $target->fetch();

            if ($targetRow !== false
                && (int) $targetRow['is_admin'] === 1
                && $adminCount <= 1) {
                flash(t('这是最后一个可用管理员，删除后将无人能管理面板。'), 'error');
                redirect('index.php?action=users');
            }

            // Kick first so no live session keeps running with a dead account.
            relay_api('POST', '/users/' . rawurlencode($username) . '/kick');
            $result = relay_api('DELETE', '/users/' . rawurlencode($username));

            // 审计：删除了哪个账号（历史会话记录保留）。
            $audit_op('panel_delete_user', $result, sprintf(
                t('目标账号 %s'),
                $username
            ));

            flash(
                $result['ok']
                    ? sprintf(t('账号 %s 已删除，其连接已断开。'), $username)
                    : sprintf(t('删除失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=users');

        // ---- kick every connection of an account ----
        case 'user_kick':
            require_operator();

            $username = (string) ($post['username'] ?? '');
            $guard_target($username);

            $result = relay_api(
                'POST',
                '/users/' . rawurlencode($username) . '/kick'
            );

            $audit_op('panel_kick_user', $result, sprintf(
                t('目标账号 %s'),
                $username
            ));

            flash(
                $result['ok']
                    ? sprintf(t('已断开 %s 的所有连接。'), $username)
                    : sprintf(t('操作失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=users');

        // ---- revoke a room outright (works in ANY state) ----
        case 'room_revoke':
            require_operator();

            $room = trim((string) ($post['room'] ?? ''));
            $reason = trim((string) ($post['reason'] ?? ''));

            if ($room === '') {
                $fail_room_action(t('没有指定房间号。'));
            }

            if ($reason === '') {
                $reason = t('管理员在面板关闭了房间');
            }

            // 房间标识放在 body 里（POST /rooms/revoke），不再拼进 URL 路径。
            //
            // 房间 ID 是一个 percent-encode 过的记号，里面会有 %2F、%20 这类
            // 转义；放进路径里会被反代 / nginx 归一化掉，于是面板显示得好好的
            // 房间，关的时候却回一句「房间不存在」。中继保留老路径写法只为兼容
            // 老面板，新面板一律走 body。
            //
            // 超时给 30 秒：要通知每个成员、等他们自己退出，然后才强制断开。
            // 中继内部把这套流程限制在 25 秒内，这里留足余量，保证先放手的不
            // 是面板。
            $result = relay_api(
                'POST',
                '/rooms/revoke',
                ['room' => $room, 'reason' => $reason],
                30
            );

            // A room can be a "ghost": a database row with no room in the
            // relay's memory (left over from a restart, or from a room that
            // went away without its row). Revoke cannot touch those -- it
            // answers 房间不存在 -- and a ghost is exactly the room an admin is
            // trying to get rid of. So the row gets cleaned up as the fallback,
            // the same way room_delete does it.
            if (!$result['ok']) {
                $fallback = relay_api(
                    'DELETE', '/rooms/' . rawurlencode($room), null, 10
                );

                if ($fallback['ok']) {
                    $result = $fallback;
                }
            }

            // 审计：谁关了哪个房间（房间名 / ID 进 room_id 列，关得成不成
            // 功进说明）。必须排在 $finish_room_action 之前 —— 它一定会
            // redirect()/json_out() 结束请求，写在后面就是死代码。
            $audit_op(
                'panel_revoke_room',
                $result,
                sprintf(t('目标房间 %s'), $room),
                $room
            );

            $finish_room_action(
                $result,
                sprintf(t('房间 %s 已强制关闭。'), $room),
                t('关闭失败：')
            );

        // ---- close / clean up a room, whichever state it is in ----
        //
        // One button, two cases, decided HERE instead of by the person
        // clicking:
        //
        //   * the room is live in the relay's memory -> revoke it (this is the
        //     unconditional destroy: sockets closed, members notified)
        //   * it is only a leftover database row    -> delete the row
        //
        // The panel used to send DELETE directly, and a live room answered
        // "房间不存在" -- which is the error that kept coming back, because
        // DELETE only ever meant "remove a leftover row".
        case 'room_delete':
            require_operator();

            $room = trim((string) ($post['room'] ?? ''));
            $reason = trim((string) ($post['reason'] ?? ''));

            if ($room === '') {
                // Without this, an empty room id would fall through to the
                // "list rooms" route and come back as a success.
                $fail_room_action(t('没有指定房间号。'));
            }

            if ($reason === '') {
                $reason = t('管理员在面板关闭了房间');
            }

            // 和 room_revoke 一样：先无条件吊销（房间标识放 body 里），
            // 吊销不成就退回数据库残留行的清理。
            $result = relay_api(
                'POST',
                '/rooms/revoke',
                ['room' => $room, 'reason' => $reason],
                30
            );

            if (!$result['ok']) {
                // Not live (or the revoke could not run). Fall through to the
                // database-row cleanup, which reports precisely what it found.
                $result = relay_api(
                    'DELETE', '/rooms/' . rawurlencode($room), null, 10
                );
            }

            $audit_op(
                'panel_delete_room',
                $result,
                sprintf(t('目标房间 %s'), $room),
                $room
            );

            $finish_room_action(
                $result,
                sprintf(t('房间 %s 已处理。'), $room),
                t('操作失败：')
            );

        // ---- purge every stale room row in one go ----
        case 'room_purge_stale':
            require_operator();

            $result = relay_api('POST', '/rooms/purge-stale');

            $audit_op('panel_purge_stale_rooms', $result, t('清理残留房间记录'));

            flash(
                $result['ok']
                    ? (relay_message($result) ?: t('残留记录已清理'))
                    : sprintf(t('清理失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=rooms');

        // ---- kick a single player ----
        case 'session_kick':
            require_operator();

            $session = (string) ($post['session'] ?? '');

            $result = relay_api(
                'DELETE',
                '/sessions/' . rawurlencode($session)
            );

            $audit_op(
                'panel_kick_player',
                $result,
                sprintf(t('目标会话 %s'), $session),
                '',
                $session
            );

            flash(
                $result['ok']
                    ? sprintf(t('已踢出会话 %s。'), $session)
                    : sprintf(t('操作失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=rooms');

        // ---- kick a single ROOM MEMBER (not just their game session) ----
        //
        // 和上面的 session_kick 区别：那个断的是 Minecraft 会话，人还在房间里；
        // 这一个把人移出房间（游戏会话一并断开），房间本身继续存在。
        //
        // 中继侧 `POST /rooms/<room>/kick`，body 是 {"client_id": "..."}。
        // 管理员可以踢任何人 —— 包括房主；踢房主 = 房间解散（中继走的是和
        // 「强制关闭房间」同一条路径，所以这里可能等上几秒，超时给 30 秒）。
        // 二次确认（普通玩家 / host / 房主三种文案）在 rooms.php 的表单上，
        // 这里只负责执行。
        case 'room_member_kick':
            require_operator();

            $room     = trim((string) ($post['room'] ?? ''));
            $client   = trim((string) ($post['client_id'] ?? ''));
            $label    = trim((string) ($post['label'] ?? ''));

            if ($room === '') {
                $fail_room_action(t('没有指定房间号。'));
            }

            if ($client === '') {
                $fail_room_action(t('没有指定要踢出的成员。'));
            }

            $shown = $label !== '' ? $label : $client;

            $result = relay_api(
                'POST',
                '/rooms/' . rawurlencode($room) . '/kick',
                ['client_id' => $client],
                30
            );

            $audit_op(
                'panel_kick_member',
                $result,
                sprintf(t('目标成员 %s'), $shown),
                $room
            );

            flash(
                $result['ok']
                    ? (relay_message($result) ?: sprintf(t('已踢出玩家 %s。'), $shown))
                    : sprintf(t('操作失败：%s'), (string) $result['error']),
                $result['ok'] ? 'success' : 'error'
            );

            redirect('index.php?action=rooms');

        default:
            redirect('index.php?action=dashboard');
    }
}

// ============================================================
// GET pages
// ============================================================

switch ($action) {
    case 'login':
        // 登录页是一个独立文件（容器健康检查也会访问它）。
        require __DIR__ . '/login.php';
        break;

    case 'rooms':
        require_login();
        $rooms    = relay_api('GET', '/rooms');
        $stats    = relay_api('GET', '/stats');
        $history  = db()->query(
            'SELECT * FROM room_sessions ORDER BY id DESC LIMIT 50'
        )->fetchAll();
        require __DIR__ . '/views/rooms.php';
        break;

    case 'users':
        require_login();

        // `role` MUST be selected. Without it normalize_role() falls back to
        // is_admin and every administrator renders as a green 管理员 -- the
        // root account included, which made the role colours look broken.
        $statement = db()->query(
            'SELECT id, username, is_admin, is_disabled, role, created_at,
                    last_login_at, last_login_ip, login_count
               FROM users ORDER BY id DESC LIMIT 200'
        );
        $users = $statement->fetchAll();

        $stats = relay_api('GET', '/stats');
        require __DIR__ . '/views/users.php';
        break;

    case 'account':
        // Root (and anybody else) editing their OWN account. Separate from
        // the account list, which deliberately refuses to touch root.
        require_login();
        require __DIR__ . '/views/account.php';
        break;

    case 'events':
        require_login();

        // ---- 保留期：超过 10 天的日志自动删掉 ----------------------------
        //
        // 定时清理的主力是中继（server/relay_server.py 的 events_retention_loop，
        // 一直在跑）。这里再清一次是"只更新了面板、中继还没重建"时的兜底：
        // 打开日志页顺手删掉超期的行。删到了才记一笔（平常这里返回 0，
        // 不会往日志里灌垃圾）。清理失败不会让页面打不开。
        $purged = panel_purge_old_events();

        if ($purged > 0) {
            audit_event('events_purged', [
                'actor'  => 'panel',
                'detail' => sprintf(
                    t('自动清理超过 %d 天的日志：%d 条'),
                    PANEL_EVENT_RETENTION_DAYS,
                    $purged
                ),
            ]);
        }

        // ---- 筛选（服务端）------------------------------------------------
        //
        // 参数只认白名单里的值（?category[]=x 这种数组会被 is_string 挡掉，
        // trim 之后认不出的值一律当"全部"）。用户不选就是全部，和以前一样。
        $filter_category = is_string($_GET['category'] ?? null)
            ? trim((string) $_GET['category'])
            : '';
        $filter_outcome = is_string($_GET['outcome'] ?? null)
            ? trim((string) $_GET['outcome'])
            : '';

        if (!array_key_exists($filter_category, panel_event_categories())) {
            $filter_category = '';
        }

        if (!array_key_exists($filter_outcome, panel_event_outcomes())) {
            $filter_outcome = '';
        }

        $events = panel_events_fetch($filter_category, $filter_outcome, 200);

        // 中继自己那份日志的概览（总行数 / 文件大小），给页面上那行
        // 「下载服务端日志」用：管理员一眼能看出中继日志是不是真的在长。
        //
        // 只有能操作的人才会看到下载按钮，所以也只给管理员查这一次。超时用
        // 面板的默认值（relay.timeout），因为这只是读一行 —— 真要说慢，是
        // 中继没回应，那种情况下顶栏的两个状态灯本来也在等它。
        //
        // 中继没回应、或者还是旧版（没有 /logs/tail）时 ok=false，页面上就
        // 什么都不显示 —— 这一行是锦上添花，绝不能让它变成页面上的一句报错。
        $log_info = can_operate()
            ? relay_api('GET', '/logs/tail?lines=1')
            : null;

        // No auto-refresh here on purpose: the audit log is for reading back,
        // and a page that jumps every 10 seconds makes that harder.
        require __DIR__ . '/views/events.php';
        break;

    case 'dashboard':
    default:
        require_login();
        $stats = relay_api('GET', '/stats');
        $rooms = relay_api('GET', '/rooms');
        $settings = relay_api('GET', '/settings');

        $totals = db()->query(
            'SELECT
                (SELECT COUNT(*) FROM users) AS users,
                (SELECT COUNT(*) FROM users WHERE is_disabled = 1) AS disabled,
                (SELECT COUNT(*) FROM room_sessions) AS sessions,
                (SELECT COALESCE(SUM(bytes_to_host + bytes_to_player), 0)
                   FROM room_sessions) AS bytes'
        )->fetch();

        $recent = db()->query(
            'SELECT * FROM room_sessions ORDER BY id DESC LIMIT 10'
        )->fetchAll();

        require __DIR__ . '/views/dashboard.php';
        break;
}
