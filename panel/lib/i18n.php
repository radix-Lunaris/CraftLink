<?php
/**
 * CraftLink（方块联机，原 MC-Link）管理面板 -- language (中文 / English) and theme (暗色 / 亮色).
 *
 * WHY THIS FILE EXISTS
 *
 * The panel started out Chinese-only. Adding a second language touches every
 * view, so the two things that must never break live here, in one small file:
 *
 *   1. t('中文原文')  -- the lookup used by every view. It FALLS BACK to the
 *      Chinese original when a key is missing, so an unfinished translation
 *      can never render a blank string or a "???" placeholder: the worst case
 *      is one sentence still in Chinese. That property is the whole reason the
 *      table can be filled in gradually.
 *
 *   2. The preference (language + theme) is a cookie, resolved once per request
 *      BEFORE any output (see panel_apply_preferences()). Defaults are Chinese
 *      and dark, so with no cookie set the panel behaves exactly as before.
 *
 * Deliberately dependency free and side-effect free: no database, no session,
 * no file access. common.php requires this file; nothing else needs to know it
 * exists.
 */

declare(strict_types=1);

// ============================================================
// Constants
// ============================================================

const PANEL_LANG_ZH = 'zh';
const PANEL_LANG_EN = 'en';

const PANEL_THEME_DARK  = 'dark';
const PANEL_THEME_LIGHT = 'light';

/** Cookie names. Values are validated before they are ever written back. */
const PANEL_LANG_COOKIE  = 'panel_lang';
const PANEL_THEME_COOKIE = 'panel_theme';

/** How long a chosen language / theme is remembered: one year. */
const PANEL_PREF_SECONDS = 31536000;

// ============================================================
// Current language / theme
// ============================================================

/**
 * The language in force for this request.
 *
 * Chinese is the source language and the default: no cookie, an unknown cookie
 * or a corrupt one all mean 中文.
 *
 * Pass a value to force it (panel_apply_preferences() does that once, before
 * any output).
 */
function panel_lang(?string $set = null): string
{
    static $lang = null;

    if ($set !== null) {
        $lang = panel_known_language($set) ?? PANEL_LANG_ZH;

        return $lang;
    }

    if ($lang === null) {
        $lang = panel_known_language($_COOKIE[PANEL_LANG_COOKIE] ?? '')
            ?? PANEL_LANG_ZH;
    }

    return $lang;
}

/** The theme in force for this request. Dark is the default (the old look). */
function panel_theme(?string $set = null): string
{
    static $theme = null;

    if ($set !== null) {
        $theme = panel_known_theme($set) ?? PANEL_THEME_DARK;

        return $theme;
    }

    if ($theme === null) {
        $theme = panel_known_theme($_COOKIE[PANEL_THEME_COOKIE] ?? '')
            ?? PANEL_THEME_DARK;
    }

    return $theme;
}

/** "en" / "zh" as an HTML lang attribute value. */
function panel_html_lang(): string
{
    return panel_lang() === PANEL_LANG_EN ? 'en' : 'zh-CN';
}

/**
 * A language code we recognise, or null.
 *
 * Null means "not a choice we understand", which is what keeps ?lang= (empty)
 * or ?lang=xx from silently resetting somebody's preference.
 *
 * The is_string() guard is not decoration: ?lang[]=en makes $_GET['lang'] an
 * ARRAY, and trim(array) is a TypeError in PHP 8 -- a whole-panel 500 from a
 * query string. Anything that is not a plain string is simply not a choice.
 */
function panel_known_language($value): ?string
{
    if (!is_string($value)) {
        return null;
    }

    $text = strtolower(trim($value));

    return in_array($text, [PANEL_LANG_ZH, PANEL_LANG_EN], true) ? $text : null;
}

/** A theme name we recognise, or null. Same rules as panel_known_language(). */
function panel_known_theme($value): ?string
{
    if (!is_string($value)) {
        return null;
    }

    $text = strtolower(trim($value));

    return in_array($text, [PANEL_THEME_DARK, PANEL_THEME_LIGHT], true)
        ? $text
        : null;
}

// ============================================================
// Applying the choice (?lang= / ?theme=)
// ============================================================

/**
 * Read ?lang= / ?theme= from the current URL, remember them in cookies, and
 * make them take effect for THIS request.
 *
 * MUST be called before any output (setcookie needs unsent headers). Both
 * index.php and login.php call it right after start_session().
 *
 * Nothing is written when the request carries no choice, or carries the one
 * already in force -- so a normal page view sends no cookie at all, and the
 * default Chinese/dark behaviour is byte for byte what it was before.
 */
function panel_apply_preferences(): void
{
    $current = panel_lang();

    $requested = panel_known_language($_GET['lang'] ?? '');

    if ($requested !== null && $requested !== $current) {
        panel_remember(PANEL_LANG_COOKIE, $requested);
        $current = $requested;
    }

    panel_lang($current);

    $currentTheme = panel_theme();

    $requestedTheme = panel_known_theme($_GET['theme'] ?? '');

    if ($requestedTheme !== null && $requestedTheme !== $currentTheme) {
        panel_remember(PANEL_THEME_COOKIE, $requestedTheme);
        $currentTheme = $requestedTheme;
    }

    panel_theme($currentTheme);
}

/** Write a preference cookie and make it visible to the rest of this request. */
function panel_remember(string $name, string $value): void
{
    if (headers_sent()) {
        // Should be unreachable (the panel applies preferences before it
        // prints anything), but a warning here would corrupt the page.
        return;
    }

    setcookie($name, $value, [
        'expires'  => time() + PANEL_PREF_SECONDS,
        'path'     => panel_base_url(),
        'samesite' => 'Lax',
    ]);

    // t() reads $_COOKIE, and the browser only sends the new cookie with the
    // NEXT request: without this line the page would still render in the old
    // language until the user clicked a second time.
    $_COOKIE[$name] = $value;
}

/**
 * Link that switches one preference while staying on the current page.
 *
 * Every panel page is the same script with an "action" query parameter, so
 * rebuilding a clean URL from that action is enough -- and it also keeps
 * ?lang= / ?theme= from piling up in the address bar every time somebody
 * clicks.
 *
 * $fallback_action is used when the address carries no action of its own
 * (the standalone login.php, for instance).
 */
function panel_pref_url(string $key, string $value, string $fallback_action = 'dashboard'): string
{
    $action = (string) ($_GET['action'] ?? '');

    if (preg_match('/^[a-z_]+$/', $action) !== 1) {
        $action = (string) $fallback_action;
    }

    return 'index.php?action=' . h($action) . '&amp;' . h($key) . '=' . h($value);
}

// ============================================================
// t()
// ============================================================

/**
 * Translate a Chinese panel string.
 *
 * The argument IS the key: t('房间') looks up '房间' in the table below. When
 * the key is absent -- or the language is Chinese -- the ORIGINAL string comes
 * back untouched. That is a hard guarantee, not a nicety: a missing
 * translation shows Chinese instead of an empty page element, so a half
 * finished table can never break the panel.
 *
 * $context exists for the handful of Chinese words that mean different things
 * in different places. 「关闭」 is the Close button of the wait dialog, the OFF
 * state of the account switch, AND the verb in "turn off the switch". English
 * needs three different words for those, so those call sites pass a context
 * and the table stores the key as "context|原文":
 *
 *     t('关闭')           ->  Close      (the dialog button)
 *     t('关闭', 'state')  ->  OFF        (the switch is off)
 *     t('关闭', 'verb')   ->  Turn off   ("Turn off Require sign-in?")
 *
 * The visible CHINESE never changes -- only the lookup key does. An unknown
 * context falls back to the plain key, then to the original string.
 *
 * What must NOT go through t(): usernames, room names, IPs and session ids,
 * anything the relay sends (its labels, its messages, its log details), and
 * anything inside <code>. Those are data, not panel text.
 */
function t(?string $text, string $context = ''): string
{
    $original = (string) $text;

    // Chinese is the source language: the original IS the translation, and
    // skipping the lookup means a Chinese panel cannot be affected by the
    // table at all.
    if ($original === '' || panel_lang() === PANEL_LANG_ZH) {
        return $original;
    }

    $table = panel_english_table();

    if ($context !== '') {
        return $table[$context . '|' . $original]
            ?? ($table[$original] ?? $original);
    }

    return $table[$original] ?? $original;
}

// ============================================================
// The English table
// ============================================================
//
// Keys are the Chinese source strings, EXACTLY as they appear in t('...') --
// including leading/trailing spaces on sentence fragments (a few paragraphs
// are split so that bold words can stay bold). Values must not contain a
// single quote: they live in single-quoted PHP strings, and several of them are
// embedded in single-quoted JavaScript string literals (confirm() dialogs).
//
// %s / %d placeholders must match the ones in the key, in the same order.

function panel_english_table(): array
{
    static $table = null;

    if ($table !== null) {
        return $table;
    }

    $table = [
        // ---- layout: nav, header, footer ----
        '仪表盘' => 'Dashboard',
        '房间' => 'Rooms',
        '账号' => 'Accounts',
        '操作日志' => 'Audit log',
        '我的账号' => 'My account',
        'CraftLink 管理面板' => 'CraftLink Admin Panel',
        '退出' => 'Sign out',
        '修改我的用户名和密码' => 'Change my username and password',
        '中继正常' => 'Relay OK',
        '中继不可用' => 'Relay unavailable',
        '数据库正常' => 'Database OK',
        '数据库不可用' => 'Database down',
        '只读模式' => 'Read-only mode',
        // 纯标点也算界面文案：中文的破折号和顿号编号在英文里要换掉。
        ' —— ' => ' -- ',
        '1、' => '1. ',
        '2、' => '2. ',
        '3、' => '3. ',
        '你的账号可以查看所有数据，但' => 'Your account can view everything, but ',
        '不能修改任何东西' => 'cannot change anything',
        '（房间、账号、开关都是只读的）。' => ' (rooms, accounts and switches are read-only). ',
        '需要操作权限请联系管理员。' => 'Ask an administrator for write access.',
        '中继容器是旧版' => 'The relay container is outdated',
        '面板上这几个功能现在不会生效：' => ' -- these panel features do nothing right now:',
        '在服务器上执行这一条命令重建中继就好了（会断开正在玩的玩家，挑没人的时候跑）：' => 'Rebuild the relay on the server with this one command (it disconnects players who are in game, so run it when nobody is playing):',
        '当前中继代码时间：%s' => 'Current relay build time: %s',
        '中继版本正常（代码时间 %s）' => 'Relay version is up to date (build time %s)',
        '无法连接中继服务的后台接口：%s' => 'Cannot reach the relay admin API: %s',
        '未知错误' => 'unknown error',
        '请检查 mc-relay 容器是否在运行，以及面板 config.php 里的 relay.base_url / relay.token 是否与中继的 MCLINK_ADMIN_API_TOKEN 一致。' => 'Check that the mc-relay container is running, and that relay.base_url / relay.token in the panel config.php match MCLINK_ADMIN_API_TOKEN on the relay.',
        '中文界面' => 'Chinese interface',
        '英文界面' => 'English interface',
        '亮色' => 'Light',
        '暗色' => 'Dark',
        '亮色主题' => 'Light theme',
        '暗色主题' => 'Dark theme',
        // 版权行（MC-Link → CraftLink 改名之后）。
        // 中文原文带中文括号「（方块联机）」，英文里只留 CraftLink —— 英文句子
        // 里不该出现中文括号。备案号是数据，不进这张表。
        // 旧的那条 '版权所有 © %s MC-Link' 已经没有任何调用点，随改名一起删掉。
        '© %s CraftLink（方块联机）' => '© %s CraftLink',

        // ---- login page ----
        '登录' => 'Sign in',
        '请使用你的 CraftLink 账号登录' => 'Sign in with your CraftLink account',
        '用户名' => 'Username',
        '密码' => 'Password',
        '账号由管理员在「账号」页面创建。忘记密码请联系服务器管理员重置。' => 'Accounts are created by an administrator on the Accounts page. If you forgot the password, ask the server administrator to reset it.',

        // ---- shared helpers (common.php) ----
        '根管理员' => 'Root admin',
        '管理员' => 'Administrator',
        '玩家' => 'Player',
        '密码至少 6 位' => 'Password must be at least 6 characters',
        '密码最长 20 位' => 'Password must be at most 20 characters',
        '密码只能使用字母和数字（不能有符号、空格或中文）' => 'Password may only contain letters and digits (no symbols, spaces or Chinese)',
        '需要管理员权限。' => 'Administrator permission required.',
        '你的账号是【只读】权限，不能执行修改操作。需要管理权限请联系管理员。' => 'Your account is read-only and cannot change anything. Ask an administrator for write access.',
        'CSRF 校验失败，请刷新页面重试。' => 'CSRF check failed. Refresh the page and try again.',
        'config.php 格式错误：它必须 return 一个数组。' => 'config.php is malformed: it must return an array.',
        '无法连接中继服务：%s' => 'Cannot connect to the relay: %s',
        '中继服务返回了无法解析的内容' => 'The relay returned a reply that could not be parsed',
        '%d 秒' => '%d s',
        '%d 分 %d 秒' => '%dm %ds',
        '%d 小时 %d 分' => '%dh %dm',
        '面板修改房间人数上限' => 'Changing the room capacity from the panel',
        '面板强制关闭房间 / 房间不存在误报' => 'Force-closing rooms from the panel / bogus room-not-found errors',
        '房间所有者删除自己的房间' => 'A room owner deleting their own room',
        '在线状态（关掉软件就离线）' => 'Online presence (closing the client means offline)',
        '创建管理员账号（而不是普通玩家）' => 'Creating administrator accounts (instead of plain players)',
        '提升 / 降级已有账号' => 'Promoting / demoting an existing account',
        '每月 3 次自助修改次数' => 'The 3 self-service changes per month',
        '中继版本信息（本提示本身）' => 'Relay version info (this very notice)',

        // ---- controller messages (index.php) ----
        '根管理员账号不能被其他人修改。如果忘记了它的密码，请在服务器上运行：' => 'The root account cannot be changed by anybody else. If its password is lost, run this on the server:',
        '没有指定房间号。' => 'No room id was given.',
        '用户名或密码错误，或账号已被禁用。' => 'Wrong username or password, or the account is disabled.',
        '登录处理失败（数据库错误）：%s' => 'Sign-in failed (database error): %s',
        '如果提示 users.role 不存在，说明数据库还没有迁移。把中继更新到最新版并重建即可（bash /opt/mc-link/rebuild.sh）。' => 'If it mentions that users.role does not exist, the database has not been migrated yet. Update the relay to the latest version and rebuild it (bash /opt/mc-link/rebuild.sh).',
        '登录成功，%s。你的账号是【玩家】权限：可以查看所有信息，但不能修改。' => 'Signed in as %s. Your account is a Player: you can view everything but change nothing.',
        '登录成功，欢迎回来 %s（%s）。' => 'Signed in. Welcome back, %s (%s).',
        '只能创建「管理员」或「玩家」账号。' => 'Only Administrator or Player accounts can be created.',
        '只有根管理员能创建管理员账号。你（管理员）可以创建玩家账号。' => 'Only the root admin can create administrator accounts. You (administrator) can create player accounts.',
        '用户名和密码不能为空。' => 'Username and password cannot be empty.',
        '账号 %s 创建成功（%s）。' => 'Account %s created (%s).',
        '创建失败：%s' => 'Create failed: %s',
        '%s（重置密码）' => '%s (password reset)',
        '已重置 %s 的密码（该账号的登录状态已被强制失效）。' => 'Password of %s has been reset (that account was signed out everywhere).',
        '重置失败：%s' => 'Reset failed: %s',
        '已清空操作日志。' => 'Audit log cleared.',
        '清空失败：%s' => 'Clear failed: %s',
        '只有根管理员能修改别人的权限。' => 'Only the root admin can change the role of another account.',
        '权限只能是「管理员」或「玩家」。' => 'The role must be Administrator or Player.',
        '已把 %s 设为 %s。' => '%s is now %s.',
        '修改失败：%s' => 'Change failed: %s',
        '已禁用 %s。' => '%s is now disabled.',
        '已启用 %s。' => '%s is now enabled.',
        '操作失败：%s' => 'Operation failed: %s',
        '已改名为 %s' => 'Renamed to %s',
        '改名失败：%s' => 'Rename failed: %s',
        '登录状态异常，请重新登录。' => 'Your session is invalid. Sign in again.',
        '新用户名不能为空，也不能和当前用户名相同。' => 'The new username cannot be empty, or the same as the current one.',
        '用户名已改为 %s，立即生效。' => 'Username changed to %s, effective immediately.',
        '两次输入的新密码不一致。' => 'The two new passwords do not match.',
        '%s（新密码）' => '%s (new password)',
        '当前密码不正确。' => 'The current password is not correct.',
        '密码已修改。当前浏览器保持登录；其它设备和客户端需要重新登录。' => 'Password changed. This browser stays signed in; other devices and clients must sign in again.',
        '没有指定要修改的设置项。' => 'No setting was specified.',
        '保存失败：%s' => 'Save failed: %s',
        '（中继还是旧版，它不认识这个设置项 —— 先在服务器上跑 bash /opt/mc-link/rebuild.sh relay）' => ' (the relay is outdated and does not know this setting -- run bash /opt/mc-link/rebuild.sh relay on the server first)',
        '设置已保存' => 'Settings saved',
        '不能删除自己的账号。' => 'You cannot delete your own account.',
        '这是最后一个可用管理员，删除后将无人能管理面板。' => 'This is the last usable administrator; deleting it would leave nobody able to manage the panel.',
        '账号 %s 已删除，其连接已断开。' => 'Account %s deleted and its connections closed.',
        '删除失败：%s' => 'Delete failed: %s',
        '已断开 %s 的所有连接。' => 'All connections of %s have been closed.',
        '管理员在面板关闭了房间' => 'An administrator closed the room from the panel',
        '房间 %s 已强制关闭。' => 'Room %s has been force-closed.',
        '关闭失败：' => 'Close failed: ',
        '房间 %s 已处理。' => 'Room %s has been handled.',
        '残留记录已清理' => 'Stale records cleaned up',
        '清理失败：%s' => 'Cleanup failed: %s',
        '已踢出会话 %s。' => 'Session %s has been kicked.',
        '操作失败：' => 'Operation failed: ',

        // ---- dashboard ----
        '运行概览' => 'Overview',
        '在线房间' => 'Live rooms',
        '房间内在线' => 'In rooms',
        '已加入世界' => 'In world',
        '空闲隧道' => 'Idle tunnels',
        '在线账号' => 'Online accounts',
        '中继运行时长' => 'Relay uptime',
        '单个房间人数上限' => 'Room capacity',
        '单个房间的人数上限' => 'Room capacity',
        '数据库（面板实测）' => 'Database (measured by the panel)',
        '面板 ↔ MySQL' => 'Panel ↔ MySQL',
        '数据库（中继上报）' => 'Database (reported by the relay)',
        '正常' => 'OK',
        '异常' => 'Fault',
        '账号验证' => 'Account auth',
        '未知' => 'Unknown',
        '中继未上报' => 'Not reported by the relay',
        '已开启' => 'On',
        '必须登录' => 'Sign-in required',
        '已关闭' => 'Off',
        '无需登录' => 'No sign-in needed',
        '面板请求数' => 'Panel API requests',
        '注册账号' => 'Registered accounts',
        '（禁用 %s）' => ' (%s disabled)',
        '历史会话' => 'Sessions',
        '累计流量' => 'Total traffic',
        '运行开关' => 'Runtime switches',
        '无法从中继读取开关状态' => 'Cannot read the switch state from the relay',
        '中继返回的错误：%s' => 'Error returned by the relay: %s',
        '中继接口没有返回 settings 数据，HTTP 状态码：%s' => 'The relay API returned no settings data, HTTP status: %s',
        '排查顺序：' => 'What to check, in order:',
        '中继容器在跑吗：' => 'Is the relay container running: ',
        '中继是新版吗：' => 'Is the relay up to date: ',
        '（大于 0 即新版）' => ' (greater than 0 means up to date)',
        '面板能否连上中继：' => 'Can the panel reach the relay: ',
        '切换后' => 'Switching this does ',
        '不会断开任何在线玩家' => 'not disconnect any online player',
        '：已登录/已在游戏的连接照常运行，只影响之后的连接。' => ': connections that are already signed in or in game keep running; only later connections are affected.',
        '关闭（允许免登录）' => 'Turn off (allow guests)',
        '开启（必须登录）' => 'Turn on (require sign-in)',
        // ---- 「允许不同版本的客户端连接」：开发调试开关（默认关闭）----
        '这是开发调试开关，不是给玩家用的；只应由管理员临时打开、测完立刻关掉。' => 'This is a development switch, not for players; only an administrator should turn it on temporarily and turn it off right after testing.',
        '开启「允许不同版本客户端连接」？' => 'Turn on "Allow clients with a different version"?',
        '关闭「允许不同版本客户端连接」？' => 'Turn off "Allow clients with a different version"?',
        '这是开发调试功能：版本不一致的客户端连上来可能造成未知 BUG。' => 'This is a development feature: a client with a different version may cause unknown bugs.',
        '只影响之后的新连接，不会断开在线玩家。' => 'Only later connections are affected; online players are not disconnected.',
        '开启（允许不同版本）' => 'Turn on (allow other versions)',
        '关闭（要求版本一致）' => 'Turn off (require the same version)',
        ' —— 版本不同的客户端现在能连上，可能造成未知 BUG。这是开发调试状态，测完请关掉。' => ' -- clients with a different version can connect now and may cause unknown bugs. This is a development state; turn it off after testing.',
        ' —— 客户端与服务器版本必须完全一致，不一致会被拒绝（正常状态）。' => ' -- the client and the server must be exactly the same version; a mismatch is rejected (the normal state).',
        '只有管理员可以修改' => 'Only an administrator can change this',
        '%s「必须登录」？' => '%s "Require sign-in"?',
        '在线玩家不会被断开，只影响之后的连接。' => 'Online players are not disconnected; only later connections are affected.',
        '当前状态说明：' => 'Current state: ',
        // 同一个「关闭 / 开启」在不同位置是不同词性，见 t() 的说明。
        // 不带 context 的那个「关闭」是等待对话框上的按钮（表末尾 'Close'）。
        '开启' => 'ON',
        'state|开启' => 'ON',
        'state|关闭' => 'OFF',
        'verb|开启' => 'Turn on',
        'verb|关闭' => 'Turn off',
        ' —— 房主和玩家都必须用账号登录才能建房 / 进房。面板上「账号」页可以给朋友创建账号。' => ' -- hosts and players must sign in to host or join. Create accounts for your friends on the Accounts page.',
        ' —— 任何人不需要账号就能建房 / 进房（相当于早期版本的行为）。账号系统仍然可用，只是不再强制。' => ' -- anybody can host or join without an account (the behaviour of the early versions). Accounts still work, they are just not required.',
        '一个房间里最多能有多少人（含房主）。默认 8 —— Minecraft 的「对局域网开放」本身最多接受 8 人。' => 'How many people one room can hold (host included). Default 8 -- the Minecraft Open to LAN screen itself accepts at most 8.',
        '这是' => 'This is the ',
        '服务器上限' => 'server ceiling',
        '：新建的房间不能超过它。' => ': a new room cannot exceed it.',
        '每个房间的人数在“建房那一刻”就定下了，之后不能修改 —— 要换人数请删掉房间重建。' => 'Room capacity is fixed the moment the room is created and cannot be changed afterwards -- delete the room and create it again to change it.',
        '房主客户端开几条隧道，跟着【房间】的人数走，不跟房主本机设置走：' => 'How many tunnels the host client opens follows the ROOM capacity, not the host machine setting: ',
        '一个玩家一条' => 'one per player',
        '。所以换个人当房主，房间容量也不会变。' => '. So the room capacity stays the same even if somebody else hosts.',
        '注意：中继是旧版' => 'Note: the relay is outdated',
        '，还没上报这个设置项的详细信息。下面显示的是中继当前生效的值；保存时如果中继不认，请先重建中继（' => ' and does not report the details of this setting yet. The value below is what the relay is using now; if saving it is rejected, rebuild the relay first (',
        '）。' => ').',
        '当前 %s 人' => '%s players now',
        '把单个房间的人数上限改成 ' => 'Change the room capacity to ',
        ' 人？' => ' players?',
        '已经在房间里的玩家不会被踢出，只影响之后进入的玩家。' => 'Players already in a room are not kicked; only players joining later are affected.',
        '保存' => 'Save',
        '范围 %s - %s 人（含房主），保存后立即生效' => 'Range %s - %s players (host included), effective immediately',
        '当前房间（真正在线的）' => 'Live rooms (actually online)',
        '当前没有在线房间。房主启动客户端并点「开始连接」后会出现在这里。' => 'No room is online right now. A room shows up here once its host starts the client and clicks Start connection.',
        '（数据库里还有 %s 条历史残留记录，去' => ' (%s stale records are still in the database; open the ',
        '房间页' => 'Rooms page',
        '可以清理）' => ' to clean them up)',
        '房主' => 'Host',
        '房主 IP' => 'Host IP',
        '上行流量' => 'Upload',
        '下行流量' => 'Download',
        '房间号 %s' => 'Room id %s',
        '强制关闭房间「' => 'Force-close room "',
        '」？' => '"?',
        '房间里的玩家会立即断开。' => 'Players in the room are disconnected immediately.',
        '管理员在仪表盘强制关闭' => 'An administrator force-closed it from the dashboard',
        '强制关闭' => 'Force close',
        '最近的玩家会话' => 'Recent player sessions',
        '还没有玩家会话记录。' => 'No player session recorded yet.',
        '时间' => 'Time',
        '时长' => 'Duration',
        '上行' => 'Up',
        '下行' => 'Down',
        '结束原因' => 'End reason',
        '已结束' => 'Finished',
        '进行中' => 'In progress',

        // ---- rooms ----
        '在线房间（%s）' => 'Live rooms (%s)',
        '这里可以对' => 'Any room can be force-closed here, in ',
        '任意状态' => 'any state',
        '的房间执行强制关闭，也可以单独踢出一个玩家。客户端自己只能删除「自己的、且没有玩家」的房间，管理员不受此限制。' => ', and a single player can be kicked. The client itself only deletes rooms that are its own and empty; an administrator is not limited by that.',
        '在线' => 'Online',
        '房主（建房的人）' => 'Owner (who created the room) ',
        '当前主机' => 'Current host ',
        '(未知)' => '(unknown)',
        '(还没人认领)' => '(not claimed yet)',
        '房间号' => 'Room id',
        '客户端 ID' => 'Client ID',
        '已加入世界 %s / %s' => 'In world %s / %s',
        '房间内在线 %s' => 'In room %s',
        '强制关闭房间' => 'Force-close room',
        '这会把房间内的所有人（' => 'This disconnects everybody in the room (',
        ' 名玩家）立即断开，并销毁房间，无法撤销。' => ' players) immediately and destroys the room. It cannot be undone.',
        '确定继续吗？' => 'Continue?',
        '等待配对玩家' => 'Waiting players',
        '会话' => 'Session',
        '房间里暂时没有玩家。' => 'No player in this room right now.',
        '已连接' => 'Connected',
        '踢出这个玩家？' => 'Kick this player?',
        '踢出' => 'Kick',
        // ---- room members (kick one member) ----
        '房间成员' => 'Room members',
        '成员' => 'Member',
        '身份' => 'Role',
        '主机' => 'Host',
        '空闲' => 'Idle',
        '%s 秒' => '%s s',
        '当前命中房主，踢出后房间会解散' => 'This is the room owner. Kicking them dissolves the room.',
        '当前命中玩家为 host，踢出后会断开当前所有玩家的游戏连接' => 'This is the host. Kicking them disconnects every player currently in the game.',
        '面板踢出成员' => 'Member kicked from the panel',
        '没有指定要踢出的成员。' => 'No member was specified.',
        '目标成员 %s' => 'Target member %s',
        '已踢出玩家 %s。' => 'Kicked player %s.',
        '残留记录（%s）' => 'Stale records (%s)',
        '这些房间里没有房主，是数据库里的历史残留记录。' => 'These rooms have no host: they are leftover rows in the database.',
        '出现原因：中继重启后内存里的房间没了，但数据库里的记录还在（正常情况下中继会在 1 分钟内自动清掉；如果它们一直存在，就点下面的按钮）。' => 'Why they appear: after a relay restart the in-memory rooms are gone while their database rows remain. Normally the relay clears them within a minute; if they never go away, use the button below.',
        '它们' => 'They do ',
        '不影响联机' => 'not affect playing',
        '，只是看着碍眼。' => '; they are only an eyesore.',
        '最后记录的房主' => 'Last recorded host',
        '最后更新' => 'Last update',
        '操作' => 'Actions',
        '(空房间号)' => '(empty room id)',
        '残留' => 'Stale',
        '正在清理数据库里的残留记录，中继会在几秒内回应' => 'Cleaning up the stale database record; the relay answers within seconds',
        '清理残留记录「' => 'Clean up stale record "',
        '清理' => 'Clean up',
        '清理全部 %s 条残留记录？' => 'Clean up all %s stale records?',
        '一键清理全部残留' => 'Clean up all stale records',
        '历史会话（最近 50 条）' => 'Session history (last 50)',
        '暂无记录。' => 'Nothing recorded yet.',
        '开始时间' => 'Started',

        // ---- audit log ----
        '登录成功' => 'Signed in',
        '登录失败' => 'Sign-in failed',
        '禁用账号尝试登录' => 'Disabled account tried to sign in',
        '登录过于频繁' => 'Too many sign-in attempts',
        '创建初始管理员' => 'Initial admin created',
        '禁用账号' => 'Account disabled',
        '启用账号' => 'Account enabled',
        '重置密码' => 'Password reset',
        '改名' => 'Rename',
        '修改权限' => 'Role changed',
        '房主删除房间' => 'Host deleted a room',
        '创建房间' => 'Room created',
        '加入房间' => 'Room joined',
        '退出房间' => 'Room left',
        '自动删除空房间' => 'Empty room auto-deleted',
        '面板强制关闭房间' => 'Room force-closed from the panel',
        '清理残留房间记录' => 'Stale room record cleaned up',
        '面板踢出玩家' => 'Player kicked from the panel',
        '面板踢出账号' => 'Account kicked from the panel',
        '面板创建账号' => 'Account created from the panel',
        '面板重置密码' => 'Password reset from the panel',
        '面板修改权限' => 'Role changed from the panel',
        '面板禁用账号' => 'Account disabled from the panel',
        '面板启用账号' => 'Account enabled from the panel',
        '面板改名' => 'Renamed from the panel',
        '自助改名' => 'Self-service rename',
        '清空操作日志' => 'Audit log cleared',
        '修改运行开关' => 'Runtime switch changed',
        '退出登录' => 'Signed out',
        '开始开服（房主上线）' => 'Hosting started (host online)',
        '停止开服（房主下线）' => 'Hosting stopped (host offline)',
        '操作日志（最近 %s 条）' => 'Audit log (last %s)',
        '记录登录、开服/停服、删除房间、踢人、账号变更等操作。中继会一直保留历史，可以随时清空。' => 'Records sign-ins, hosting start/stop, room deletion, kicks and account changes. The relay keeps the history until you clear it.',
        '确定清空全部操作日志吗？' => 'Clear the whole audit log?',
        '这会删掉所有历史记录，无法恢复。' => 'This deletes every record and cannot be undone.',
        '清空日志' => 'Clear log',
        '还没有日志。' => 'No log entry yet.',
        '操作者' => 'Actor',
        '来源 IP' => 'Source IP',
        '说明' => 'Detail',

        // ---- accounts (list) ----
        '账号管理' => 'Account management',
        '房主和玩家都必须有账号才能建房 / 进房。客户端' => 'Hosts and players both need an account to host or join. The client ',
        '不提供注册功能' => 'has no sign-up',
        '，所有账号都在这里创建和管理。密码使用 PBKDF2-SHA256（与中继完全相同的格式），这里创建的账号可以直接登录客户端。' => '; every account is created and managed here. Passwords use PBKDF2-SHA256 (exactly the same format as the relay), so an account created here can sign in to the client right away.',
        '几种操作的区别：' => 'What the actions do:',
        '禁用' => 'Disable',
        '启用' => 'Enable',
        ' —— 对方无法再登录，并立即断开所有在线连接（账号保留，随时可恢复）' => ' -- they can no longer sign in, and all connections are closed at once (the account is kept and can be restored at any time)',
        '删除' => 'Delete',
        ' —— 永久删除账号，立即断开连接，不可恢复（历史会话记录保留）' => ' -- removes the account for good and closes its connections; it cannot be restored (session history is kept)',
        '踢下线' => 'Kick',
        ' —— 只是让 TA 重新登录，不影响账号状态' => ' -- only forces them to sign in again, the account itself is untouched',
        ' —— 改密码并强制对方重新登录' => ' -- sets a new password and forces them to sign in again',
        ' —— 用户名同时是' => ' -- the username is both the ',
        '游戏内显示名和房间列表里的名字' => 'in-game display name and the name in the room list',
        '。' => '.',
        '立即生效，不会断开玩家连接' => 'It takes effect immediately without disconnecting players',
        '，管理员改名不限次数（玩家在客户端也能自助改名，但那边 30 天最多 3 次）' => '. Administrators can rename without limit (players can rename themselves in the client too, but only 3 times every 30 days there)',
        '设为管理员' => 'Make administrator',
        ' —— 可以登录这个面板（谨慎授予）' => ' -- can sign in to this panel (grant with care)',
        '所有账号都能登录这个面板' => 'Every account can sign in to this panel',
        '，但只有' => ', but only an ',
        '能改动任何东西。普通账号登录后是' => ' can change anything. A normal account signs in as ',
        '只读' => 'read-only',
        '的：能看在线房间、在线玩家、账号列表和操作记录，但看不到任何按钮，直接构造请求也会被后端拒绝。' => ': it can see live rooms, online players, the account list and the audit log, but no buttons are shown and a hand-made request is refused by the backend.',
        '新建账号' => 'New account',
        '只读模式：你可以查看账号列表，但只有管理员能新建账号。' => 'Read-only mode: you can view the account list, but only an administrator can create accounts.',
        '密码（至少 6 位）' => 'Password (at least 6 characters)',
        '不能包含空格' => 'Must not contain spaces',
        '6-20 位，只能字母和数字' => '6-20 characters, letters and digits only',
        '玩家（只读看板）' => 'Player (read-only view)',
        '管理员（可踢人 / 改账号 / 看房间密码）' => 'Administrator (kick, edit accounts, see room passwords)',
        '只有根管理员能创建管理员账号' => 'Only the root admin can create administrator accounts',
        '管理员 🔒 只有根管理员能创建' => 'Administrator 🔒 root admin only',
        '创建账号' => 'Create account',
        '提示：密码框用明文显示，方便你把密码复制给朋友；创建后请让对方自行修改。' => 'Tip: the password box is plain text so you can copy the password to your friend; ask them to change it after signing in.',
        '不在这里创建 —— 一个服务器只有一个，它是中继首次启动时用' => ' is not created here -- there is exactly one per server, the account the relay made on its first start from ',
        '建立的那个账号，不能另设、不能转让。' => '. It cannot be duplicated or transferred.',
        '你自己这个根管理员账号要改名字 / 改密码，去' => 'To rename your own root account or change its password, open ',
        '「我的账号」' => 'My account',
        '改（改名和改密码对管理员不限制次数）。' => ' (renames and password changes are unlimited for administrators).',
        '你（管理员）只能创建' => 'You (administrator) can only create ',
        '账号；要开管理员账号请找根管理员。' => ' accounts; ask the root admin for an administrator account.',
        '所有账号（%s）' => 'All accounts (%s)',
        '角色' => 'Role',
        '状态' => 'Status',
        '登录次数' => 'Sign-ins',
        '最后登录' => 'Last sign-in',
        '最后 IP' => 'Last IP',
        '创建时间' => 'Created',
        '（我）' => ' (me)',
        '已禁用' => 'Disabled',
        '只读模式，无可执行操作' => 'Read-only mode, no actions available',
        '去「我的账号」修改' => 'Change it on My account',
        '根管理员账号不能由别人修改' => 'The root account cannot be changed by anybody else',
        '（本人可以在「我的账号」里改；忘记密码时用服务器上的 SSH 命令重置）' => '(the owner can change it on My account; if the password is lost, reset it with the SSH command on the server)',
        '断开 %s 的所有连接？' => 'Close all connections of %s?',
        '%s %s ？' => '%s %s?',
        '会立即断开该账号的所有在线连接。' => 'Its connections are closed immediately.',
        '[!] 永久删除账号 ' => '[!] Permanently delete account ',
        ' ？' => '?',
        '会立即断开该账号的所有连接，且无法恢复。' => 'All its connections are closed and the account cannot be restored.',
        '（历史会话记录会保留）' => '(session history is kept)',
        '降级为玩家' => 'demote to player',
        '提升为管理员' => 'promote to administrator',
        '把 %s %s？' => '%s: %s?',
        '降级后该账号的所有登录会立即失效，需要重新登录。' => 'After a demotion all its sessions are invalidated and it must sign in again.',
        '降为玩家' => 'Demote to player',
        '重置 %s 的密码？该账号当前登录状态会立即失效。' => 'Reset the password of %s? Its current sessions are invalidated at once.',
        '新密码（6-20 位字母数字）' => 'New password (6-20 letters/digits)',
        '新用户名' => 'New username',
        '把 %s 改名？' => 'Rename %s?',
        '立即生效，不会断开该玩家的连接。' => 'Effective immediately, the player is not disconnected.',

        // ---- my account ----
        '这里改的是' => 'This page changes ',
        '你自己' => 'your own',
        '的账号。两件事的后果不一样，分开说：' => ' account. The two actions have different consequences:',
        '：' => ': ',
        '，但' => ', but ',
        '：立即生效，' => ': effective immediately, ',
        '不会把你踢下线' => 'you are not signed out',
        '。客户端和其它设备上的登录继续有效，游戏不用重连；新名字会出现在房间列表和面板里。' => '. Sign-ins on the client and other devices keep working and the game does not reconnect; the new name appears in the room list and the panel.',
        '改密码' => 'Password change',
        '当前这个浏览器保持登录' => 'this browser stays signed in',
        '其它设备（包括客户端）的登录会立即失效' => 'sign-ins on other devices (the client included) are invalidated at once',
        '，需要用新密码重新登录。这是应该的：改密码的目的就是把别处的登录踢掉。' => ', so they must sign in again with the new password. That is the point: changing the password is how you kick out sessions elsewhere.',
        '你的角色是' => 'Your role is ',
        '改名和改密码都不限次数' => 'renames and password changes are unlimited',
        '（次数限制只针对普通玩家）。' => ' (the limit only applies to plain players).',
        '普通账号每月可以自助修改 %s 次' => 'A normal account may change itself %s times a month',
        '（' => ' (',
        '改名和改密码共用' => 'renames and password changes share',
        '这个次数，月初重置）。本月还剩' => ' that quota, which resets at the start of each month). Left this month: ',
        '次。用完了可以等月初，或者请管理员在「账号」页帮你改（管理员不受限制）。' => '. Once it is used up, wait for the next month or ask an administrator to do it on the Accounts page (administrators are not limited).',
        '暂时读不到剩余次数（中继没回应）。普通账号每月可以自助修改 3 次（改名和改密码共用），管理员及以上不受限制。' => 'Cannot read the remaining quota right now (the relay did not answer). A normal account may change itself 3 times a month (renames and password changes share the quota); administrators and above are not limited.',
        '你是根管理员。' => 'You are the root admin.',
        '根管理员账号不能被其他任何人修改（包括其他管理员），但' => 'The root account cannot be changed by anybody else (other administrators included), but ',
        '你自己可以在这里改' => 'you can change it here',
        ' —— 账号列表里那行会直接跳到本页。' => ' -- the row in the account list links straight to this page.',
        '忘记密码、登不上面板时，用服务器上的这条命令重置：' => 'If the password is lost and you cannot sign in, reset it on the server with:',
        '修改用户名' => 'Change username',
        '用户名同时是' => 'The username is both your ',
        '，也是你登录用的账号名。改完请用新用户名登录。' => ' and the account name you sign in with. Use the new name from now on.',
        '已经在玩的人不会被断开。' => 'Nobody who is already playing is disconnected.',
        '当前用户名' => 'Current username',
        '新的用户名' => 'New username',
        '2-32 个字符，不能包含空格' => '2-32 characters, no spaces',
        '2-32 个字符，不能有空格' => '2-32 characters, no spaces',
        '修改密码' => 'Change password',
        '至少 6 位、最多 20 位，只能使用字母和数字（不能有符号或空格）。' => 'At least 6 and at most 20 characters, letters and digits only (no symbols or spaces). ',
        '当前浏览器不会被踢下线' => 'this browser is not signed out',
        '；其它设备和客户端上的登录会失效，需要用新密码重新登录。' => '; sign-ins on other devices and the client stop working and must be redone with the new password.',
        '当前密码' => 'Current password',
        '新密码（6-20 位，只能字母和数字）' => 'New password (6-20 characters, letters and digits only)',
        '再输一次新密码' => 'Repeat the new password',

        // ---- inline JavaScript strings (references in views/layout_footer.php) ----
        '不可用' => 'Unavailable',
        '正在关闭房间，请稍候' => 'Closing the room, please wait',
        '正在关闭房间「%s」，请稍候' => 'Closing room "%s", please wait',
        '已等待 %s 秒' => 'Waited %s s',
        '中继还没有确认，最多再等几秒（房间可能已经卡住）' => 'The relay has not confirmed yet; a few more seconds at most (the room may be stuck)',
        '房间已关闭' => 'Room closed',
        '关闭房间没有完成' => 'Closing the room did not finish',
        '正在通知房间里的玩家退出，最多等待 %s 秒' => 'Telling the players in the room to leave; waiting up to %s seconds',
        '返回房间列表' => 'Back to the room list',
        '刷新页面' => 'Refresh page',
        '关闭' => 'Close',
        '服务器返回了无法识别的回复（HTTP %s）' => 'The server returned a reply that could not be understood (HTTP %s)',
        '房间已关闭。' => 'Room closed.',
        '关闭失败，中继没有说明原因。' => 'Close failed and the relay gave no reason.',
        '等待超时：中继还没有确认。请刷新页面查看房间是否已经关闭。' => 'Timed out: the relay never confirmed. Refresh the page to see whether the room is closed.',
        '无法连接面板服务器（网络中断，或面板正在重启）。请刷新页面重试。' => 'Cannot reach the panel server (the network dropped, or the panel is restarting). Refresh the page and try again.',
        '请求失败，请刷新页面重试。' => 'The request failed. Refresh the page and try again.',

        // ---- 操作日志：筛选条 / 分类 / 结果（views/events.php）----
        '分类' => 'Category',
        '结果' => 'Outcome',
        '全部' => 'All',
        '成功' => 'OK',
        '警告' => 'Warning',
        '失败' => 'Failed',
        '筛选' => 'Filter',
        '重置' => 'Reset',
        '账号角色' => 'Account role',
        '登录 / 登出' => 'Sign-in / sign-out',
        '连接' => 'Connections',
        '面板操作' => 'Panel actions',
        '系统' => 'System',
        '记录登录 / 登出、房间、连接和面板操作。日志保留 %d 天，超期自动清理，也可以随时手动清空。' => 'Logs sign-ins, sign-outs, rooms, connections and panel actions. Entries are kept for %d days and cleaned up automatically; you can also clear them by hand at any time.',
        '「连接」里的进入 / 离开世界来自中继的会话记录，清空日志不会删掉它们。' => 'The enter/leave-the-world rows under Connections come from the relay session records; clearing the log does not delete them.',

        // ---- 操作日志：服务端日志下载（views/events.php + web/index.php）----
        //
        // 注意：这个功能下载的是**中继的**日志文件（relay.log），和这一页的
        // 操作日志（数据库里的 relay_events）是两回事 —— 英文也照这个区分写。
        '下载服务端日志' => 'Download server log',
        '中继日志当前：%d 行，%s' => 'Relay log now: %d lines, %s',
        '中继把服务端日志保存在内存和磁盘上；下载得到的是原始服务端日志（不是这个页面上的操作日志），反馈问题时附上它最有用。' => 'The relay keeps its server log in memory and on disk; the download is that raw server log (not the audit log on this page), and it is the most useful thing to attach to a bug report.',
        '中继暂不支持日志导出（需要更新服务端）' => 'The relay does not support log export yet (the server needs an update)',
        '下载服务端日志失败：%s' => 'Downloading the server log failed: %s',
        '中继返回 HTTP %d' => 'the relay answered HTTP %d',

        // ---- 操作日志：清空服务端日志（relay.log）----
        //
        // 和「清空日志」（清审计事件）分开写：英文里也是两件不同的事，
        // 不能都叫 "Clear log"，否则英语用户不知道清的是哪个。
        '清空服务端日志' => 'Clear server log',
        '确定清空服务端日志吗？' => 'Clear the server log?',
        '这会把中继的日志文件和内存缓冲一起清空，无法恢复。' => 'This empties the relay log file and its in-memory buffer. It cannot be undone.',
        '已清空服务端日志（%d 行，%d 个轮转备份）。' => 'Server log cleared (%d lines, %d rotated backups).',
        '清空服务端日志失败：%s' => 'Clearing the server log failed: %s',
        '中继暂不支持清空服务端日志（需要更新服务端）' => 'The relay does not support clearing its log yet (the server needs an update)',

        // ---- 操作日志：面板自己写的那几条（web/index.php + lib/common.php）----
        '面板登录成功' => 'Panel sign-in succeeded',
        '面板登录失败' => 'Panel sign-in failed',
        '面板登录成功（%s）' => 'Panel sign-in succeeded (%s)',
        '面板登录失败：%s' => 'Panel sign-in failed: %s',
        '退出面板登录' => 'Panel sign-out',
        '账号不存在' => 'no such account',
        '密码错误' => 'wrong password',
        '账号已被禁用' => 'the account is disabled',
        '面板删除账号' => 'Panel deleted an account',
        '账号已删除' => 'Account deleted',
        'SSH 重置根管理员密码' => 'Root password reset over SSH',
        '自动清理过期日志' => 'Automatic cleanup of old entries',
        '自动清理超过 %d 天的日志：%d 条' => 'Automatically removed entries older than %d days: %d rows',
        '创建账号 %s（%s）' => 'Create account %s (%s)',
        '目标账号 %s' => 'Target account %s',
        '目标账号 %s → %s' => 'Target account %s -> %s',
        '%s账号 %s' => '%s account %s',
        '改名 %s → %s' => 'Rename %s -> %s',
        '自助改名 %s → %s' => 'Self-service rename %s -> %s',
        '修改自己的密码：%s' => 'Change own password: %s',
        '设置 %s = %s' => 'Setting %s = %s',
        '目标房间 %s' => 'Target room %s',
        '目标会话 %s' => 'Target session %s',
        '%s：%s' => '%s: %s',

        // ---- 操作日志：连接（玩家进 / 出世界，来自 room_sessions）----
        '玩家进入世界' => 'Player entered the world',
        '玩家离开世界' => 'Player left the world',
        '进入世界：游戏会话开始' => 'Entered the world: the game session started',
        '离开世界（原因：%s，时长 %s，上行 %s / 下行 %s）' => 'Left the world (reason: %s, duration %s, up %s / down %s)',
        '房主心跳超时，房间被回收' => 'Host heartbeat timed out, the room was reclaimed',
        '关闭房间超时' => 'Closing the room timed out',
        // 2026-09-29：重连判定探测（SESSION，PROTOCOL §8.10）的中继日志行。
        '重连判定探测' => 'Reconnect decision probe',

        // ============================================================
        // M1 / M2 / M3（2026-10-05）
        // ------------------------------------------------------------
        // 面板设置页、左上角中继版本、去掉仪表盘「正在连接」，以及顺手补齐
        // 之前漏在英文表外面的旧文案（房间页的连接状态、仪表盘那两条到期提示）。
        //
        // 规矩和上面一样：值里不能有单引号 / 反斜杠 / 换行（好几条会嵌进
        // confirm() 的单引号 JS 字符串里），%s / %d 的数量必须和中文原文一致。
        // ============================================================

        // ---- 顶栏：中继版本 ----
        '中继 %s' => 'Relay %s',
        '中继版本来源：%s' => 'Relay version source: %s',
        '中继管理接口（/stats）' => 'Relay admin API (/stats)',
        '中继管理接口（/health）' => 'Relay admin API (/health)',
        '中继 VERSION 文件：%s' => 'Relay VERSION file: %s',
        '读不到：中继没回应，也没有可读的 VERSION 文件' => 'Unavailable: the relay did not answer and no readable VERSION file was found',
        '未知来源' => 'Unknown source',
        '注意：中继接口上报的版本（%s）和包内 VERSION 文件（%s）不一致。' => 'Note: the version reported by the relay admin API (%s) and the VERSION file in the package (%s) do not match.',

        // ---- 导航 / 页面名 ----
        '面板设置' => 'Panel settings',
        '只有根管理员能执行这个操作。' => 'Only the root admin can do this.',

        // ---- 面板设置：分组与设置项 ----
        '网络与安全' => 'Network and security',
        '会话' => 'Session',
        '外观与名称' => 'Look and name',
        '备案与版权' => 'ICP filing and copyright',
        '域名与 SSL（下一个里程碑，暂未实现）' => 'Domain and SSL (next milestone, not implemented yet)',
        '面板监听 IP' => 'Panel listen IP',
        '面板监听端口' => 'Panel listen port',
        'URL 安全入口后缀' => 'URL entry suffix',
        '未认证时的 HTTP 返回码' => 'HTTP status code when not signed in',
        '会话时长（分钟）' => 'Session length (minutes)',
        '面板左上角名称' => 'Panel name in the top left',
        '网页标签名' => 'Browser tab title',
        '面板 logo' => 'Panel logo',
        '是否已 ICP 备案 / 是否展示备案号' => 'ICP filed / show the filing number',
        'ICP 备案号' => 'ICP filing number',
        '域名绑定' => 'Domain binding',
        'SSL 证书' => 'SSL certificate',

        // ---- 面板设置：说明 ----
        '面板自带的 nginx 监听哪个地址：127.0.0.1 = 只让本机反代访问（推荐）；0.0.0.0 = 所有网卡都能直连。' => 'Which address the built-in nginx listens on: 127.0.0.1 = only a local reverse proxy can reach it (recommended); 0.0.0.0 = every network interface.',
        '面板对外提供网页的端口。开放公网前先确认防火墙 / 安全组放行的是这一个。' => 'The port the panel serves web pages on. Before exposing it, make sure the firewall / security group allows this port.',
        '面板网址后面的秘密路径（例：mclink-panel）。留空 = 不使用安全入口。只允许字母、数字、点、下划线、短横线。' => 'A secret path after the panel address (for example mclink-panel). Empty = no entry suffix. Letters, digits, dot, underscore and hyphen only.',
        '没登录的人访问面板时回什么 —— 用来隐藏面板特征：404 / 403 / 444 / 200 空页都看不出这里有一个管理面板。' => 'What a visitor who is not signed in gets back - used to hide the panel: 404 / 403 / 444 / 200 empty page all look like there is no admin panel here.',
        '显示登录页（当前行为，默认）' => 'Show the sign-in page (current behaviour, default)',
        '返回 404（找不到页面）' => 'Return 404 (page not found)',
        '返回 403（拒绝访问）' => 'Return 403 (access denied)',
        '返回 444（直接断开连接，没有响应体）' => 'Return 444 (drop the connection, no response body)',
        '返回 200 空页（什么都没有的空白页）' => 'Return 200 with an empty page (a blank page)',
        '登录后多久不操作就自动退出（默认 360 分钟 = 6 小时）。到时间后需要重新登录，已经打开的页面会跳回登录页。' => 'How long you stay signed in without activity (default 360 minutes = 6 hours). After that you must sign in again, and open pages go back to the sign-in page.',
        '页头左上角显示的名字。留空 = 用 config.php / MCLINK_PANEL_TITLE 里的标题（当前值）。' => 'The name shown in the top left. Empty = use the title from config.php / MCLINK_PANEL_TITLE (the current value).',
        '浏览器标签页 / 收藏夹里显示的名字。留空 = 跟左上角名称一样。' => 'The name shown on the browser tab / in bookmarks. Empty = same as the top-left name.',
        '左上角标记和浏览器标签图标（favicon）用的是同一张图。支持 PNG / JPEG / GIF / WebP / ICO，最大 1 MB；留空 = 内置的草方块标记。' => 'The top-left mark and the browser tab icon (favicon) use the same image. PNG / JPEG / GIF / WebP / ICO, up to 1 MB; empty = the built-in grass block mark.',
        '选「不显示」时页脚连备案号那一块都不渲染 —— 没备案的部署不该出现别人的登记号。' => 'With do not show, the footer renders no filing number at all - a deployment without a filing should not show somebody else number.',
        '填你备案下来的登记号。留空 = 页脚不渲染备案号（默认空）。备案号是纯文本，面板不会给它加任何外链。' => 'The registration number from your ICP filing. Empty = the footer renders no filing number (the default). The number is plain text; the panel never links it.',
        '自动（填了备案号就显示，默认）' => 'Automatic (show it when a number is set, default)',
        '显示备案号' => 'Show the filing number',
        '不显示备案号' => 'Do not show the filing number',
        '下个里程碑实现：绑定域名后只允许用该域名访问面板，直接 IP 访问按上面的返回码处理。' => 'Implemented in the next milestone: once a domain is bound, only that domain may reach the panel, and direct IP access gets the status code configured above.',
        '下个里程碑实现：上传证书（离线可用）+ ACME 自动申请 / 续期（Let’s Encrypt）。' => 'Implemented in the next milestone: upload a certificate (works offline) plus automatic ACME issuance / renewal (Let’s Encrypt).',

        // ---- 面板设置：校验原因 ----
        '合法的 IPv4 / IPv6 地址；不能为空。' => 'A valid IPv4 / IPv6 address; it cannot be empty.',
        '1 - 65535 之间的整数。' => 'A whole number between 1 and 65535.',
        '只能包含字母、数字、点、下划线、短横线（1 - 64 位），不能带斜杠。' => 'Letters, digits, dot, underscore and hyphen only (1 - 64 characters), with no slash.',
        '只能是上面列出的几种之一。' => 'Must be one of the options listed above.',
        '5 - 10080 分钟（7 天）之间的整数。' => 'A whole number between 5 and 10080 minutes (7 days).',
        '最长 96 字节（约 30 个汉字）；留空表示使用默认标题。' => 'Up to 96 bytes (about 30 Chinese characters); empty means use the default title.',
        '最长 96 字节（约 30 个汉字）；留空表示跟左上角名称一致。' => 'Up to 96 bytes (about 30 Chinese characters); empty means the same as the top-left name.',
        '只能通过上传更新（不接受 SVG，避免脚本注入）。' => 'Can only be changed by uploading (SVG is refused to avoid script injection).',
        'logo 只能通过页面上传更新。' => 'The logo can only be changed by uploading it on this page.',
        '只能是「自动 / 显示 / 不显示」之一。' => 'Must be one of automatic / show / do not show.',
        '最长 64 字节；留空表示不显示。' => 'Up to 64 bytes; empty means do not show it.',
        '未知的设置项。' => 'Unknown setting.',
        '开关的值只能是 0 或 1。' => 'A switch value can only be 0 or 1.',
        '监听 IP 不能为空：只给本机用就填 127.0.0.1。' => 'The listen IP cannot be empty: use 127.0.0.1 to keep it local.',
        '监听 IP 不是合法的 IPv4 / IPv6 地址。' => 'The listen IP is not a valid IPv4 / IPv6 address.',
        '监听端口必须是数字。' => 'The listen port must be a number.',
        '监听端口必须在 1 - 65535 之间。' => 'The listen port must be between 1 and 65535.',
        '安全入口后缀最长 64 位。' => 'The entry suffix can be at most 64 characters.',
        '安全入口后缀只能包含字母、数字、点、下划线、短横线，不能有斜杠或空格。' => 'The entry suffix may only contain letters, digits, dot, underscore and hyphen; no slashes or spaces.',
        '未认证返回码只能是列出的几种之一。' => 'The unauthenticated status code must be one of the listed options.',
        '会话时长必须是分钟数（整数）。' => 'The session length must be a whole number of minutes.',
        '会话时长必须在 5 - 10080 分钟之间。' => 'The session length must be between 5 and 10080 minutes.',
        '名称最长 96 字节（约 30 个汉字）；留空表示使用默认值。' => 'The name can be at most 96 bytes (about 30 Chinese characters); empty means use the default.',
        '备案号显示开关只能是「自动 / 显示 / 不显示」之一。' => 'The filing-number switch must be one of automatic / show / do not show.',
        '备案号最长 64 字节。' => 'The filing number can be at most 64 bytes.',

        // ---- 面板设置：存储 ----
        '无法写入数据库（面板设置以数据库为准）：%s' => 'Could not write to the database (panel settings treat the database as the primary store): %s',
        '没有可用的配置文件路径。' => 'No usable config file path.',
        '设置无法序列化成 JSON。' => 'The settings could not be serialised to JSON.',
        '目录不存在，而且创建失败：%s' => 'The directory does not exist and creating it failed: %s',
        '写不进临时文件：%s' => 'Could not write the temporary file: %s',
        '无法把临时文件改名成：%s' => 'Could not rename the temporary file to: %s',
        '设置已写入数据库，但配置文件备份写入失败：%s' => 'The setting was saved to the database, but writing the config-file backup failed: %s',
        '配置文件里没有可导入的设置（或者文件不存在）。' => 'The config file has no settings to import (or the file does not exist).',
        '面板设置 %s = %s' => 'Panel setting %s = %s',
        '清空 URL 安全入口后缀（值不写进日志）' => 'Cleared the URL entry suffix (the value is not written to the log)',
        '更新 URL 安全入口后缀（值不写进日志）' => 'Updated the URL entry suffix (the value is not written to the log)',
        '清除面板 logo（恢复默认草方块）' => 'Cleared the panel logo (back to the built-in grass block)',
        '更新面板 logo（图片内容不写进日志）' => 'Updated the panel logo (the image itself is not written to the log)',
        '%s（允许普通管理员修改）' => '%s (editable by normal admins)',

        // ---- 面板设置：权限与提示 ----
        '「%s」目前只允许根管理员修改。需要普通管理员也能改的话，请根管理员在这一项上打开放行开关。' => '"%s" can only be changed by the root admin right now. To let normal admins change it, ask the root admin to turn on the grant switch for this item.',
        '%s「%s」' => '%s "%s"',
        '允许普通管理员修改' => 'Allow normal admins to change it',
        '收回普通管理员的修改权限' => 'Revoke the change permission from normal admins',
        '放行普通管理员修改面板设置' => 'Granted normal admins permission to change a panel setting',
        '收回普通管理员修改面板设置的权限' => 'Revoked the panel-setting permission from normal admins',
        '修改面板设置' => 'Changed a panel setting',
        '初始化面板设置存储' => 'Initialised the panel settings store',
        '更换面板 logo' => 'Changed the panel logo',

        // ---- 面板设置：logo ----
        '没有选择要上传的图片。' => 'No image was selected for upload.',
        '上传没有完成（PHP 错误码 %d）。' => 'The upload did not finish (PHP error code %d).',
        '图片必须大于 0 字节、不超过 %s。' => 'The image must be larger than 0 bytes and no bigger than %s.',
        '上传的临时文件读不到，请重试。' => 'The uploaded temporary file could not be read; please try again.',
        '读取上传的图片失败，请重试。' => 'Reading the uploaded image failed; please try again.',
        '只支持 PNG / JPEG / GIF / WebP / ICO 图片（不接受 SVG，避免脚本注入）。' => 'Only PNG / JPEG / GIF / WebP / ICO images are supported (SVG is refused to avoid script injection).',
        '上传目录不可写：%s' => 'The upload directory is not writable: %s',
        '图片写不进上传目录：%s' => 'The image could not be written to the upload directory: %s',

        // ---- 面板设置页：页面文字 ----
        '这里改的是' => 'This page configures',
        '面板自己' => 'the panel itself',
        '的参数（监听地址、安全入口、会话时长、名字、logo、备案号），不是中继的运行开关 —— 那些在仪表盘的「运行开关」里。' => ' settings (listen address, entry path, session length, name, logo, ICP number) rather than the relay runtime switches - those are on the dashboard under Runtime switches.',
        '存储状态' => 'Storage status',
        '面板版本：%s' => 'Panel version: %s',
        '数据库：可用（%d 条设置）—— 这是主存储。' => 'Database: available (%d settings) - this is the primary store.',
        '数据库：不可用 —— 现在只读回退到配置文件备份，保存会报错。' => 'Database: unavailable - reads fall back to the config-file backup, and saving will report an error.',
        '配置文件备份：%s（%s，%s）' => 'Config-file backup: %s (%s, %s)',
        '存在' => 'present',
        '还没有' => 'not created yet',
        '可写' => 'writable',
        '不可写' => 'not writable',
        '你是根管理员。' => 'You are the root admin.',
        '每一条设置右边都有一个「允许普通管理员修改」开关，默认全部关闭；打开之后普通管理员才能改那一条，随时可以收回。' => 'Every setting has an allow normal admins to change it switch on the right, all off by default; turn one on to let normal admins change that single item, and revoke it at any time.',
        '你的账号是普通管理员：这一页可以看，但默认不能改。' => 'Your account is a normal admin: you can read this page, but by default you cannot change it.',
        '需要修改哪一条，请让根管理员在那一项右侧打开放行开关。' => 'To change an item, ask the root admin to turn on the grant switch on its right.',
        '未实现' => 'Not implemented',
        '默认值' => 'Default',
        '%s · 默认值：%s' => '%s · default: %s',
        '默认' => 'Default',
        '空' => 'empty',
        '现在用的是默认值；留空保存 = 回到默认。' => 'The default value is in use; saving an empty value returns to the default.',
        '改完需要重启面板容器' => 'needs a panel container restart',
        '普通管理员不能修改这一项（需要根管理员放行）。' => 'Normal admins cannot change this item (the root admin must grant it).',
        '只读：这一项现在只允许根管理员修改。' => 'Read-only: only the root admin can change this item right now.',
        '根管理员已放行这一项，普通管理员可以修改。' => 'The root admin granted this item, so normal admins can change it.',
        '当前生效：%s（设置留空时用 config.php / MCLINK_PANEL_TITLE）' => 'Currently in effect: %s (when the setting is empty, config.php / MCLINK_PANEL_TITLE is used)',
        '当前生效：%s' => 'Currently in effect: %s',
        '当前生效：空（页脚不渲染备案号）' => 'Currently in effect: empty (the footer renders no filing number)',
        '当前生效：页脚会显示备案号' => 'Currently in effect: the footer shows the filing number',
        '当前生效：页脚不显示备案号' => 'Currently in effect: the footer shows no filing number',
        '当前生效：%d 分钟' => 'Currently in effect: %d minutes',
        '普通管理员可改' => 'Normal admins can change it',
        '只有 root 可改' => 'Root admin only',
        '放行给普通管理员' => 'Grant to normal admins',
        '收回放行' => 'Revoke the grant',
        '上传' => 'Upload',
        '清除' => 'Clear',
        '保存' => 'Save',
        '网络与安全：面板自己要读的值' => 'Network and security: the values the panel itself reads',
        '下面这几个值面板已经能读出来并做了校验；真正让它们生效（监听新端口 / 安全入口 / 未认证返回码）由自带 nginx 完成，属于服务端工作流。' => 'The panel can already read and validate these values; making them take effect (listen on the new port / entry suffix / unauthenticated status code) is done by the built-in nginx and belongs to the server workstream.',
        '监听：%s；安全入口：%s；未认证返回码：%s' => 'Listen: %s; entry path: %s; unauthenticated status code: %s',
        '未设置' => 'not set',
        '存储维护' => 'Storage maintenance',
        '初始化设置存储' => 'Initialise the settings store',
        '重新建一遍设置表（幂等，不会删数据），并把配置文件备份里、数据库里还没有的键导入进来。数据库不可用时用这个把备份里的设置搬回去。' => 'Recreate the settings table (idempotent, nothing is deleted) and import keys from the config-file backup that the database does not have yet. Use this to move the backup settings back when the database was unavailable.',
        '初始化 / 从配置文件导入' => 'Initialise / import from the config file',

        // ---- 控制器里的提示 ----
        '没有指定要修改的设置项。' => 'No setting was selected to change.',
        '没有指定要放行的设置项。' => 'No setting was selected to grant.',
        '已保存「%s」。' => 'Saved "%s".',
        '保存失败：%s' => 'Save failed: %s',
        '已放行「%s」：普通管理员现在可以修改它。' => 'Granted "%s": normal admins can change it now.',
        '已收回「%s」：现在只有根管理员能修改它。' => 'Revoked "%s": now only the root admin can change it.',
        'logo 上传失败：%s' => 'Logo upload failed: %s',
        'logo 已更新：左上角标记和浏览器标签（favicon）用的是同一张图。' => 'The logo was updated: the top-left mark and the browser tab icon (favicon) use the same image.',
        '清除 logo 失败：%s' => 'Clearing the logo failed: %s',
        '已清除 logo，左上角和浏览器标签恢复成内置的草方块。' => 'The logo was cleared: the top-left mark and the browser tab are back to the built-in grass block.',
        '初始化面板设置存储，从配置文件导入 %d 条' => 'Initialised the panel settings store and imported %d settings from the config file',
        '初始化失败（数据库不可用）：%s' => 'Initialisation failed (database unavailable): %s',
        '设置表已就绪，从配置文件导入 %d 条设置。' => 'The settings table is ready; imported %d settings from the config file.',

        // ---- 仪表盘 / 房间页：之前漏在英文表外面的旧文案（顺手补齐）----
        '已自动到期：' => 'Auto-expired: ',
        '自动到期：' => 'Auto-expires: ',
        '约 %d 分钟后自动关闭，不需要手动关（改这个开关会重新计时）。' => 'It turns itself off in about %d minutes; no need to switch it off by hand (changing the switch restarts the timer).',
        '开关已失效，版本门恢复强制校验。这里的显示还写着「开启」是因为数据库里的值没被改写 —— 重新关一次即可。' => 'The switch has expired and the version gate is enforced again. This display still says on because the database value was not rewritten - switch it off once to clear it.',
        '已连接（按成员状态）' => 'Connected (by member state)',
        '正在连接' => 'Connecting',
        '连接状态' => 'Connection state',
        '未连接' => 'Not connected',
        '等待中' => 'Waiting',
        '配对中' => 'Pairing',
        '（已点开始连接）' => '(started connecting)',
        // ---- M4：域名绑定 / SSL / 自带 nginx（2026-10-05）----
        '域名与 SSL' => 'Domain and SSL',
        '绑定域名' => 'Bound domain',
        '填了之后**只有这个域名**能打开面板，用 IP 或其他域名访问会按「未认证返回码」处理。可以写 *.example.com 通配子域。留空 = 不限制（任何 Host 都能访问）。' => 'When set, **only this domain** can open the panel; IP addresses and other host names get the unauthenticated status code. *.example.com works for wildcard subdomains. Empty = no restriction (any Host is accepted).',
        '只能填主机名（可带最前面的 *.），不要带 http:// 、端口或斜杠；留空表示不绑定。' => 'A host name only (a leading *. is allowed); no scheme, port or path. Empty = not bound.',
        '强制 HTTPS' => 'Force HTTPS',
        '不强制：HTTP 和 HTTPS 都能打开' => 'Off: both HTTP and HTTPS work',
        '强制 HTTPS：HTTP 自动 301 跳转' => 'On: plain HTTP is redirected with a 301',
        '打开之后，明文 HTTP 的请求会被跳转到 HTTPS（Let’s Encrypt 的校验路径除外，否则续期会失败）。证书没就绪时不会跳转，避免把面板跳到打不开的地址。' => 'When on, plain HTTP requests are redirected to HTTPS (except the Let us Encrypt validation path, which must stay on plain HTTP or renewal breaks). While the certificate is not ready there is no redirect, so the panel can never point you at a dead address.',
        '只能是「不强制 / 强制」之一。' => 'Must be off or on.',
        '强制 HTTPS 只能是「不强制 / 强制」之一。' => 'Force HTTPS must be off or on.',
        'SSL 模式' => 'SSL mode',
        '不用 HTTPS（默认，和以前一样）' => 'No HTTPS (default, same as before)',
        '上传证书（离线可用，推荐）' => 'Upload a certificate (works offline, recommended)',
        '自签证书（只用于测试，浏览器会警告）' => 'Self-signed certificate (testing only, browsers will warn)',
        'Let’s Encrypt 自动申请 / 续期' => 'Let us Encrypt automatic issue and renewal',
        '上传证书不需要域名解析、也不需要 80 端口，离线就能用；Let’s Encrypt 需要域名解析到这台机器、且 80 端口能从公网访问。' => 'Uploading needs no DNS and no port 80 and works offline; Let us Encrypt needs the domain to resolve to this machine and port 80 to be reachable from the internet.',
        '只能是列出的四种模式之一。' => 'Must be one of the four listed modes.',
        'SSL 模式只能是列出的几种之一。' => 'SSL mode must be one of the listed values.',
        '证书文件（PEM）' => 'Certificate file (PEM)',
        '容器里的绝对路径。在下面直接上传证书时这一项会自动填上；也可以手填一个已经挂进容器里的路径（例如 certbot 的 fullchain.pem）。' => 'An absolute path inside the container. Uploading below fills this in automatically; you can also point it at a path you already mounted (for example a certbot fullchain.pem).',
        '必须是容器里的绝对路径（以 / 开头），不能有 .. 或空格。' => 'Must be an absolute path inside the container (starting with /), with no .. and no spaces.',
        '私钥文件（PEM）' => 'Private key file (PEM)',
        '和证书配对的私钥。上传时会自动填上；私钥不能带密码（nginx 不支持加密私钥）。' => 'The private key that matches the certificate. Uploading fills this in; the key must not have a passphrase (nginx cannot use encrypted keys).',
        'Let’s Encrypt 账号邮箱' => 'Let us Encrypt account email',
        '申请 / 续期用的联系邮箱（到期提醒也发到这里）。只在上面的 SSL 模式选「Let’s Encrypt」时才用得到。' => 'Contact address for issue and renewal (expiry notices go here too). Only used when the SSL mode above is Let us Encrypt.',
        '要填一个合法邮箱；不用 ACME 时留空即可。' => 'A valid email address; leave empty when ACME is not used.',
        '邮箱格式不对（例：admin@example.com）。' => 'That email address does not look valid (example: admin@example.com).',
        '邮箱太长了（最多 128 字节）。' => 'That email address is too long (128 bytes at most).',
        '邮箱格式不对。' => 'That email address does not look valid.',
        '域名格式不对：只能有字母、数字、点和短横线，最长 253 位；通配符只能写成最前面的 *.' => 'Invalid domain: letters, digits, dots and hyphens only, 253 characters at most, and a wildcard may only appear as the leading *.',
        '域名看不懂：填 panel.example.com 这样的主机名（从地址栏复制也行）。' => 'Could not read that domain: use a host name like panel.example.com (pasting from the address bar is fine).',
        '证书 / 私钥要填容器里的绝对路径（以 / 开头），例如 /var/www/certs/panel.crt。' => 'Certificate and key must be absolute paths inside the container (starting with /), for example /var/www/certs/panel.crt.',
        '路径太长（最多 200 个字符）。' => 'That path is too long (200 characters at most).',
        '路径里不能有 ..' => 'The path must not contain ..',
        '路径只能包含字母、数字、点、下划线、短横线和斜杠。' => 'The path may only contain letters, digits, dots, underscores, hyphens and slashes.',
        '当前生效：没有绑定域名（任何 Host 都能访问面板）。' => 'In effect: no bound domain (any Host can reach the panel).',
        '当前生效：只允许 %s 访问（其他 Host 按未认证返回码处理）。' => 'In effect: only %s may connect (other Host values get the unauthenticated status code).',
        '当前生效：HTTP 和 HTTPS 都能打开。' => 'In effect: both HTTP and HTTPS work.',
        '当前生效：明文 HTTP 会 301 跳到 HTTPS。' => 'In effect: plain HTTP is redirected to HTTPS with a 301.',
        '当前生效：开关是打开的，但证书还没就绪 —— 暂时不会跳转（避免面板打不开）。' => 'In effect: the switch is on but the certificate is not ready, so there is no redirect yet (that keeps the panel reachable).',
        '当前生效：不用 HTTPS（面板只提供 HTTP）。' => 'In effect: no HTTPS (the panel serves HTTP only).',
        '当前生效：模式已经选了，但证书 / 私钥路径还空着 —— 面板暂时仍按 HTTP 提供服务。' => 'In effect: a mode is selected but the certificate and key paths are still empty, so the panel keeps serving HTTP.',
        '当前生效：%s（证书还没被根代理检查过，刷新一下或看下面的应用结果）。' => 'In effect: %s (the root agent has not inspected this certificate yet; refresh or check the apply result below).',
        '当前生效：%s —— 这张证书已经过期了（%s）。' => 'In effect: %s -- this certificate has already expired (%s).',
        '当前生效：%s，%s 到期（还剩 %d 天）。' => 'In effect: %s, expires %s (%d days left).',
        '当前生效：还没设置路径。' => 'In effect: no path set yet.',
        '当前生效：%s（在面板证书目录里）' => 'In effect: %s (inside the panel certificate directory)',
        '当前生效：%s（在面板证书目录之外 —— 要确认这个路径已经挂进容器，否则 nginx 读不到）' => 'In effect: %s (outside the panel certificate directory -- make sure that path is mounted into the container or nginx cannot read it)',
        '当前生效：没填邮箱（只有用 Let’s Encrypt 时才需要）。' => 'In effect: no email (only needed for Let us Encrypt).',
        '当前生效：%s' => 'In effect: %s',
        '没有选择证书文件。' => 'No certificate file was selected.',
        '没有选择私钥文件。' => 'No private key file was selected.',
        '上传没有完成（PHP 错误码 %d）。' => 'The upload did not complete (PHP error code %d).',
        '文件大小必须在 0 字节和 %s 之间。' => 'The file must be larger than 0 bytes and at most %s.',
        '上传的临时文件读不到，请重试。' => 'The uploaded temporary file cannot be read, please try again.',
        '读取上传的文件失败，请重试。' => 'Reading the uploaded file failed, please try again.',
        '文件里有二进制内容 —— 证书和私钥都应该是文本（PEM）。' => 'The file contains binary data -- certificates and keys must be text (PEM).',
        '文件太大（上限 %s）。' => 'The file is too large (limit %s).',
        '证书文件里没有找到 -----BEGIN CERTIFICATE-----（要 PEM 格式的证书，通常是 fullchain.pem）。' => 'No -----BEGIN CERTIFICATE----- found in the certificate file (a PEM certificate is required, usually fullchain.pem).',
        '私钥文件里没有找到 -----BEGIN ' . 'PRIVATE KEY-----（要 PEM 格式的私钥，通常是 privkey.pem）。' => 'No -----BEGIN ' . 'PRIVATE KEY----- found in the key file (a PEM key is required, usually privkey.pem).',
        '这把私钥是带密码的，nginx 用不了：请先去掉密码（openssl rsa -in 原key -out 新key）再上传。' => 'That private key has a passphrase and nginx cannot use it: remove the passphrase first (openssl rsa -in old.key -out new.key) and upload again.',
        '证书暂存目录不可写：%s' => 'The certificate staging directory is not writable: %s',
        '证书写不进暂存目录：%s' => 'The certificate could not be written to the staging directory: %s',
        '私钥写不进暂存目录：%s' => 'The private key could not be written to the staging directory: %s',
        '证书已经暂存，但排不进处理队列：%s' => 'The certificate was staged but could not be queued: %s',
        '自签证书不支持通配符域名：请填一个具体域名（例如 panel.example.com）。' => 'Self-signed certificates do not support wildcard domains: use a concrete domain (for example panel.example.com).',
        '通配符域名不能用 HTTP-01 校验：请填具体域名（例如 panel.example.com），或者改用上传证书。' => 'A wildcard domain cannot pass HTTP-01 validation: use a concrete domain (for example panel.example.com) or switch to an uploaded certificate.',
        'ACME 要先绑定域名：Let’s Encrypt 只给域名签证书，不给 IP 签。' => 'ACME needs a bound domain first: Let us Encrypt only issues certificates for domain names, never for IP addresses.',
        'ACME 要填一个邮箱：Let’s Encrypt 用它发到期提醒，也是账号联系地址。' => 'ACME needs an email address: Let us Encrypt uses it for expiry notices and as the account contact.',
        'ACME 模式但没绑定域名：请先在「绑定域名」里填上要签的域名。' => 'ACME mode without a bound domain: fill in the domain to sign under Bound domain first.',
        '不认识的 ACME 动作。' => 'Unknown ACME action.',
        '生效链路' => 'Apply chain',
        '配置目录：%s（%s）' => 'Configuration directory: %s (%s)',
        '已有配置' => 'configuration present',
        '还没有生成' => 'not generated yet',
        '还没生成' => 'not generated yet',
        '上次生成：%s；容器内监听 HTTP %d / HTTPS %d' => 'Last generated: %s; inside the container listening on HTTP %d and HTTPS %d',
        '待处理请求：%d 条' => 'Queued requests: %d',
        '上次生效结果：%s（%s）' => 'Last apply result: %s (%s)',
        '成功' => 'success',
        '失败' => 'failure',
        '急救入口：%s（只在本机 / 宿主机回环上可访问；安全入口、域名、证书配错时从这里进去改）' => 'Rescue entry: %s (loopback only; when the secret entry, the domain or the certificate is misconfigured, get back in here)',
        '改完上面的设置不用重启容器：保存之后根代理会在几秒内重新渲染 nginx 配置、`nginx -t` 试跑、通过才热加载。这里显示的就是那条链路的当前状态。' => 'Changes above need no container restart: after saving, the root agent re-renders the nginx configuration within seconds, runs nginx -t, and only then hot-reloads. This block shows the current state of that chain.',
        '证书列表与到期时间' => 'Certificates and expiry',
        '证书目录里还没有证书。上传一张，或者先用「生成自签证书」把链路跑通。' => 'There are no certificates in the certificate directory yet. Upload one, or generate a self-signed certificate first to prove the chain works.',
        '还没读到证书状态（根代理还没跑过 cert-status）。' => 'No certificate status yet (the root agent has not run cert-status).',
        '证书' => 'Certificate',
        '签发者' => 'Issuer',
        '到期时间' => 'Expires',
        '剩余' => 'Remaining',
        '域名' => 'Domain',
        '%d 天' => '%d days',
        '已过期' => 'expired',
        '无法解析' => 'unreadable',
        '正在使用' => 'in use',
        '自签' => 'self-signed',
        '私钥不配对' => 'key mismatch',
        '读不出来' => 'cannot be read',
        '当前生效：%s / %s（指纹 %s）' => 'In effect: %s / %s (fingerprint %s)',
        '上传证书并启用' => 'Upload and enable',
        '生成自签证书（测试用）' => 'Generate a self-signed certificate (testing)',
        '刷新证书状态' => 'Refresh certificate status',
        '重新渲染并生效' => 'Re-render and apply',
        '先 dry-run 验证（不真的签证书）' => 'Dry run first (does not really issue a certificate)',
        '用 Let’s Encrypt 申请' => 'Issue with Let us Encrypt',
        '检查并续期（一键续期）' => 'Check and renew (one click)',
        '上传证书：离线可用，不需要域名解析，也不需要 80 端口。Let’s Encrypt：需要域名解析到这台机器、80 端口能从公网进来；续期失败不会动现有证书。' => 'Uploaded certificate: works offline, needs no DNS and no port 80. Let us Encrypt: needs the domain to resolve here and port 80 to be reachable; a failed renewal never touches the certificate that is in use.',
        '普通管理员不能修改这一项（需要根管理员放行）。' => 'A regular administrator cannot change this item (the root account has to grant it).',
        '急救（配置改坏了从这里回去）' => 'Rescue (get back in when the configuration locks you out)',
        '任何一项改动都有安全网：本机急救入口一直可用；下面这三个按钮也会真的改设置并让 nginx 立刻生效（走的是和 mclink 管理台同一条代码路径）。' => 'Every change has a safety net: the local rescue entry stays available, and the three buttons below really write the setting and apply it through nginx (the same code path the mclink console uses).',
        '重置 URL 安全入口' => 'Reset URL secret entry',
        '清空安全入口，回到「任何路径都能打开面板」。安全入口写错、自己被关在外面时用。' => 'Clear the secret entry and go back to any path opening the panel. Use it when the secret entry was mistyped and you are locked out.',
        '清空安全入口' => 'Clear secret entry',
        '解绑域名' => 'Unbind domain',
        '清空域名绑定，回到「任何 Host 都能访问」。换成别的域名访问不了时用。' => 'Clear the domain binding and let any Host connect again. Use it when you switched to another domain and cannot get in.',
        '关掉 HTTPS' => 'Turn HTTPS off',
        '把 SSL 模式改回「不用 HTTPS」，回到纯 HTTP。证书文件一个都不删；证书配错、浏览器打不开时用。' => 'Set the SSL mode back to no HTTPS and serve plain HTTP. No certificate file is deleted; use it when a certificate is wrong and browsers refuse to open the panel.',
        '当前有「急救覆盖」在生效（优先级高于数据库）' => 'An emergency override is in effect (it wins over the database)',
        '文件：%s' => 'File: %s',
        '这些值是 root 的 mclink 管理台在数据库写不进去时写的（例如重置安全入口）。在设置页里把这一项保存一次就会自动清掉，也可以直接点下面的按钮清除。' => 'These values were written by the root mclink console because the database could not be written (for example when resetting the secret entry). Saving that item in the settings page clears the override automatically, or use the button below.',
        '清除急救覆盖' => 'Clear emergency overrides',
        '[!] 数据库写不进去，这一条写进了急救覆盖文件：%s' => '[!] The database could not be written, so this item went into the emergency override file: %s',
        '清除急救覆盖：%s' => 'Cleared emergency overrides: %s',
        '清除急救覆盖失败：%s' => 'Clearing the emergency overrides failed: %s',
        '急救覆盖文件：%s' => 'Emergency override file: %s',
        '清除：overrides --clear' => 'Clear with: overrides --clear',
        '现在没有急救覆盖。' => 'There are no emergency overrides right now.',
        '已清除急救覆盖：%s' => 'Cleared emergency overrides: %s',
        '查看 / 清除急救覆盖' => 'show or clear emergency overrides',
        '急救覆盖无法序列化成 JSON。' => 'The emergency overrides could not be serialised to JSON.',
        '没有可用的急救覆盖文件路径。' => 'No usable emergency override file path.',
        '没有可写的设置项。' => 'There is no setting to write.',
        '目录不存在，而且创建失败：%s' => 'The directory does not exist and could not be created: %s',
        '写不进队列文件：%s' => 'Could not write the queue file: %s',
        '队列文件改名失败：%s' => 'Renaming the queue file failed: %s',
        '队列目录不可写：%s' => 'The queue directory is not writable: %s',
        '建不出队列目录：%s' => 'Could not create the queue directory: %s',
        '队列动作名不合法。' => 'That queue action name is not valid.',
        '请求无法序列化成 JSON。' => 'The request could not be serialised to JSON.',
        '根代理正常（%d 秒前还在跑）。' => 'Root agent is running (last seen %d seconds ago).',
        '根代理好像没在跑（最后一次心跳 %d 秒前）。' => 'The root agent does not seem to be running (last heartbeat %d seconds ago).',
        '根代理还没跑过（或者容器刚起来）。' => 'The root agent has not run yet (or the container just started).',
        '没有可用的 nginx 配置目录。' => 'No usable nginx configuration directory.',
        '建不出 nginx 配置目录：%s' => 'Could not create the nginx configuration directory: %s',
        'nginx 配置目录不可写：%s' => 'The nginx configuration directory is not writable: %s',
        '找不到 nginx 模板目录：%s' => 'The nginx template directory was not found: %s',
        '模板文件读不到：%s' => 'The template file cannot be read: %s',
        '模板文件读取失败：%s' => 'Reading the template file failed: %s',
        '模板 %s 里有渲染器不认识的占位符：%s' => 'Template %s contains placeholders the renderer does not know: %s',
        '写不进配置临时文件：%s' => 'Could not write the configuration temporary file: %s',
        '配置改名失败：%s' => 'Renaming the configuration failed: %s',
        '已渲染 %d 个文件到 %s' => 'Rendered %d files into %s',
        '没用 HTTPS' => 'no HTTPS',
        '没绑定（任何 Host 都能访问）' => 'not bound (any Host may connect)',
        '没配置（/ 就是面板）' => 'not configured (/ is the panel)',
        'HTTPS 服务：本次没有启用（ssl_mode=%s）' => 'HTTPS server: not enabled this time (ssl_mode=%s)',
        'HTTPS 服务（TLS）' => 'HTTPS server (TLS)',
        '域名绑定：只有 %s 能访问（其他 Host 按未认证返回码处理）' => 'Domain binding: only %s may connect (other Host values get the unauthenticated status code)',
        '没有配置 URL 安全入口：所有路径都交给面板（未认证返回码由 PHP 判定）' => 'No URL secret entry configured: every path goes to the panel (PHP decides the unauthenticated status code)',
        'URL 安全入口：%s/ 前缀在这里被剥掉，交给下面的 PHP / 静态文件规则' => 'URL secret entry: the %s/ prefix is stripped here and handed to the PHP and static file rules below',
        '把入口前缀剥掉再交给下面的 PHP / 静态文件规则（last = 重新匹配 location）' => 'Strip the entry prefix and re-match the PHP and static file rules below (last = match locations again)',
        '安全入口守卫：只有 %s/ 下面能到面板（ACME 校验除外），其余一律返回 %s' => 'Secret entry guard: only paths under %s/ reach the panel (the ACME validation path excepted); everything else returns %s',
        'ACME（Let’s Encrypt HTTP-01）校验文件：明文 HTTP 直达，不参与跳转 / 安全入口' => 'ACME (Let us Encrypt HTTP-01) validation files: served over plain HTTP, bypassing the redirect and the secret entry',
        '[!] proxy 后端只在本地冒烟测试（没有 php-fpm）时使用；生产是 fastcgi。' => '[!] The proxy backend is only for local smoke tests (no php-fpm available); production uses fastcgi.',
        '急救标记：proxy_set_header 会覆盖客户端传来的同名头，伪造不了' => 'Rescue marker: proxy_set_header overwrites any client supplied header of the same name, so it cannot be forged',
        '急救标记：只有急救 server 块里 set 了它，客户端伪造不了' => 'Rescue marker: only the rescue server block sets it, so a client cannot forge it',
        '本次没有启用「强制 HTTPS」：这个文件没有被 include，面板按上面 HTTP_MODE_INCLUDE 指定的那份走。' => 'Force HTTPS is off this time: this file is not included and the panel uses the file named by HTTP_MODE_INCLUDE above.',
        '容器内 nginx 固定监听 HTTP %d / HTTPS %d；宿主机上的 %s 由 compose 映射到这个端口（改端口要跑 mclink apply-network 重建容器）。' => 'Inside the container nginx always listens on HTTP %d and HTTPS %d; on the host %s is mapped here by compose (changing the port means running mclink apply-network to recreate the container).',
        '急救端口和面板端口撞了：请把 MCLINK_PANEL_RESCUE_PORT 改成别的端口。' => 'The rescue port collides with the panel port: set MCLINK_PANEL_RESCUE_PORT to another port.',
        '「强制 HTTPS」开着，但 SSL 模式是「不用 HTTPS」—— 本次不生成跳转（否则面板会打不开）。' => 'Force HTTPS is on but the SSL mode is no HTTPS, so no redirect is generated (otherwise the panel would become unreachable).',
        '选了 SSL 模式但证书 / 私钥路径是空的 —— 本次按 HTTP 渲染。' => 'An SSL mode is selected but the certificate and key paths are empty, so this render serves HTTP only.',
        '证书 / 私钥在容器里读不到（%s）—— 本次按 HTTP 渲染，旧配置保留。' => 'The certificate or key cannot be read inside the container (%s), so this render serves HTTP only and the previous configuration is kept.',
        '上传证书' => 'Uploaded certificate',
        '自签证书（仅测试）' => 'Self-signed certificate (testing only)',
        '不用 HTTPS' => 'No HTTPS',
        '证书已上传并排队：根代理会先校验（密钥是否配对 / 有没有过期 / 域名对不对），通过之后才安装并热加载 nginx。几秒后刷新这一页看结果 —— 校验失败时旧证书继续用。' => 'The certificate was uploaded and queued: the root agent validates it first (key match, expiry, domain coverage) and only then installs it and hot-reloads nginx. Refresh this page in a few seconds; if validation fails the previous certificate keeps working.',
        '上传了面板证书与私钥（内容不写进日志）' => 'Uploaded a panel certificate and private key (contents are not logged)',
        '上传面板证书失败：%s' => 'Uploading the panel certificate failed: %s',
        '上传面板证书' => 'Upload panel certificate',
        '证书上传失败：%s' => 'Certificate upload failed: %s',
        '已排队：根代理会生成一张自签证书（浏览器会提示不受信任，这是正常的），安装后自动热加载。几秒后刷新看结果。' => 'Queued: the root agent will generate a self-signed certificate (browsers warn about it, which is expected) and hot-reload after installing it. Refresh in a few seconds.',
        '为 %s 生成自签证书（测试用）' => 'Generate a self-signed certificate for %s (testing)',
        '生成自签证书失败：%s' => 'Generating the self-signed certificate failed: %s',
        '生成面板自签证书' => 'Generate panel self-signed certificate',
        '未绑定域名' => 'no bound domain',
        '已排队（dry-run）：只验证流程，不会真的签发、也不会安装任何证书。几秒后刷新看结果 —— 日志在「操作日志」和容器的 agent.log 里。' => 'Queued (dry run): this only validates the flow, it issues and installs nothing. Refresh in a few seconds; the log lives in the audit log and in the container agent.log.',
        '已排队：根代理会调用 certbot 申请 / 续期。失败时现有证书原样保留，不会因为续期失败掉 HTTPS；结果和失败原因几秒后刷新这一页可见。' => 'Queued: the root agent calls certbot to issue or renew. On failure the certificate in use is left untouched, so a failed renewal never drops HTTPS; refresh this page in a few seconds for the result and the reason.',
        '%s Let’s Encrypt：%s（域名 %s，dry_run=%s）' => '%s Let us Encrypt: %s (domain %s, dry_run=%s)',
        '申请' => 'issue',
        '续期' => 'renew',
        '已排队' => 'queued',
        '排队失败' => 'queueing failed',
        '未绑定' => 'not bound',
        '是' => 'yes',
        '否' => 'no',
        '申请 / 续期面板证书（Let’s Encrypt）' => 'Issue or renew the panel certificate (Let us Encrypt)',
        '刷新面板证书状态' => 'Refresh panel certificate status',
        '刷新证书状态（到期时间等）' => 'refresh certificate status (expiry and so on)',
        '刷新证书状态排队失败：%s' => 'Queueing the certificate status refresh failed: %s',
        '已排队：根代理会重新检查证书（到期时间 / 密钥配对 / 覆盖域名）。几秒后刷新这一页。' => 'Queued: the root agent re-inspects the certificate (expiry, key match, covered domains). Refresh this page in a few seconds.',
        '已排队：根代理会重新渲染配置 → nginx -t 试跑 → 通过才热加载。不通过会保留上一份能跑的配置，面板不会被打不开。几秒后刷新看结果。' => 'Queued: the root agent re-renders the configuration, runs nginx -t and only then hot-reloads. If the test fails the previous working configuration is kept, so the panel never becomes unreachable. Refresh in a few seconds.',
        '手动触发：重新渲染并热加载面板 nginx' => 'Manual trigger: re-render and hot-reload the panel nginx',
        '重新渲染并热加载面板 nginx' => 'Re-render and hot-reload the panel nginx',
        '重新渲染并热加载' => 're-render and hot-reload',
        '急救：重置 URL 安全入口' => 'Rescue: reset the URL secret entry',
        '急救：清空 URL 安全入口（值不写进日志）' => 'Rescue: cleared the URL secret entry (the value is not logged)',
        '安全入口已清空，并已排队让 nginx 生效：几秒后 http://<地址>/index.php 就能直接打开。' => 'The secret entry is cleared and applying it is queued: in a few seconds http://<address>/index.php opens the panel directly.',
        '安全入口已清空，但排队生效失败（用本机急救入口进去重试）：%s' => 'The secret entry is cleared but queueing the apply failed (retry from the local rescue entry): %s',
        '急救：解绑域名' => 'Rescue: unbind domain',
        '急救：解绑域名（现在任何 Host 都能访问面板）' => 'Rescue: unbound the domain (any Host can reach the panel now)',
        '域名绑定已清空，并已排队让 nginx 生效。' => 'The domain binding is cleared and applying it is queued.',
        '域名绑定已清空，但排队生效失败（用本机急救入口进去重试）：%s' => 'The domain binding is cleared but queueing the apply failed (retry from the local rescue entry): %s',
        '急救：关掉面板 HTTPS' => 'Rescue: turn the panel HTTPS off',
        '急救：关掉面板 HTTPS（证书文件保留）' => 'Rescue: turned the panel HTTPS off (certificate files are kept)',
        'SSL 模式已改回「不用 HTTPS」并排队生效：面板回到纯 HTTP，证书文件一个都没删。' => 'The SSL mode is back to no HTTPS and applying it is queued: the panel serves plain HTTP again and no certificate file was deleted.',
        'SSL 模式已改回纯 HTTP，但排队生效失败（用本机急救入口进去重试）：%s' => 'The SSL mode is back to plain HTTP but queueing the apply failed (retry from the local rescue entry): %s',
        '急救失败：%s' => 'Rescue action failed: %s',
        '重置失败：' => 'Reset failed: ',
        '解绑失败：' => 'Unbind failed: ',
        '解绑失败：%s' => 'Unbind failed: %s',
        '关闭 HTTPS 失败：' => 'Turning HTTPS off failed: ',
        '操作失败：%s' => 'The operation failed: %s',
        '排队失败：' => 'Queueing failed: ',
        '排队失败：%s' => 'Queueing failed: %s',
        '清除失败：' => 'Clearing failed: ',
        '清除失败：%s' => 'Clearing failed: %s',
        '写入失败：' => 'The write failed: ',
        '生效失败：%s' => 'Applying failed: %s',
        '渲染失败：' => 'Rendering failed: ',
        '保存失败：%s' => 'Saving failed: %s',
        'CraftLink 面板命令行（容器内 root）' => 'CraftLink panel command line (root, inside the container)',
        '当前设置 / 证书 / nginx 状态' => 'current settings, certificates and nginx state',
        '当前设置 / 证书 / nginx 状态（--json 给脚本用）' => 'current settings, certificates and nginx state (--json for scripts)',
        '重置（或设置）URL 安全入口' => 'reset (or set) the URL secret entry',
        '整份设置 / 证书 / nginx 状态' => 'full settings, certificate and nginx state',
        '改一条设置' => 'change one setting',
        '指向一对新证书' => 'point at a new certificate pair',
        '只渲染 nginx 配置（不生效）' => 'render the nginx configuration only (no apply)',
        '把老部署的环境变量播种进设置' => 'seed settings from an older deployment environment',
        '打印急救入口地址' => 'print the rescue entry URL',
        '用法：set <键> <值>（键见面板设置页）' => 'usage: set <key> <value> (keys are listed on the panel settings page)',
        '用法：set-cert --cert <证书路径> --key <私钥路径> [--mode upload|selfsigned|acme]' => 'usage: set-cert --cert <certificate path> --key <key path> [--mode upload|selfsigned|acme]',
        '这个键只能在面板里改。' => 'That key can only be changed from the panel.',
        '已写入 %s = %s' => 'Wrote %s = %s',
        '写入 %s 失败：%s' => 'Writing %s failed: %s',
        'degraded=1 %s 写进了急救覆盖文件' => 'degraded=1 %s was written to the emergency override file',
        '证书设置已写入（还没让 nginx 生效）。' => 'The certificate settings were written (nginx not applied yet).',
        '证书设置已写入，nginx 已重新加载。' => 'The certificate settings were written and nginx was reloaded.',
        '证书设置已写入，但 nginx 还没生效：%s' => 'The certificate settings were written but nginx is not applied yet: %s',
        'nginx 已按当前设置重新加载。' => 'nginx reloaded with the current settings.',
        'nginx 已按新入口重新加载。' => 'nginx reloaded with the new secret entry.',
        'nginx 已按新配置重新加载。' => 'nginx reloaded with the new configuration.',
        'nginx 已重新加载。' => 'nginx reloaded.',
        '已写入设置，但 nginx 还没生效（根代理没跑？）：%s' => 'The setting was written but nginx is not applied yet (is the root agent running?): %s',
        '已写入设置，但 nginx 还没生效：%s' => 'The setting was written but nginx is not applied yet: %s',
        '安全入口已改为：%s' => 'The secret entry is now: %s',
        '（不使用安全入口）' => '(no secret entry)',
        '新地址：http://<域名或IP>/%s' => 'New address: http://<domain or IP>/%s',
        '万一进不去，用急救入口：%s' => 'If that does not work, use the rescue entry: %s',
        '域名绑定已清空：现在任何域名 / IP 都能访问面板。' => 'The domain binding is cleared: any domain or IP can reach the panel now.',
        'SSL 模式已改成「不用 HTTPS」：面板回到纯 HTTP，证书文件一个都没删。' => 'The SSL mode is now no HTTPS: the panel serves plain HTTP and no certificate file was deleted.',
        '急救入口：%s' => 'Rescue entry: %s',
        '急救入口：%s（只在本机可访问；安全入口 / 域名 / 证书配错时从这里进）' => 'Rescue entry: %s (local access only; use it when the secret entry, domain or certificate is misconfigured)',
        '它只监听容器内，并且只发布到宿主机回环地址（公网到不了）。' => 'It only listens inside the container and is published on the host loopback address (the internet cannot reach it).',
        '不判安全入口、不判域名、不跳 HTTPS —— 面板被自己配错时的最后一条路。' => 'It ignores the secret entry, the domain and the HTTPS redirect -- the last way in when the panel locked itself out.',
        '在服务器上：curl -sS %shealthz' => 'On the server: curl -sS %shealthz',
        '没有需要播种的环境变量（设置里已经有值）。' => 'Nothing to seed from the environment (the settings already have values).',
        '已从环境变量播种：%s' => 'Seeded from the environment: %s',
        '（还没有装过证书）' => '(no certificate installed yet)',
        '还剩 %s 天' => '%s days left',
        '刷新证书状态（到期时间等）（--json 给脚本用）' => 'refresh certificate status (expiry and so on, --json for scripts)',
        '查看 / 清除急救覆盖（--clear [键,键]）' => 'show or clear emergency overrides (--clear [key,key])',
        '心跳 / 队列（面板判断代理在不在跑）' => 'heartbeat and queue (how the panel knows the agent is alive)',
        '%s（%s）' => '%s (%s)',
        'CraftLink 面板设置' => 'CraftLink panel settings',
        'SSL 证书与生效状态（M4）' => 'SSL certificates and apply status (M4)',
        '下面这几个值是自带 nginx 直接读的：改完保存，根代理会在几秒内重新渲染并热加载。监听 IP / 端口还要让 compose 的端口映射跟着变（mclink apply-network）。' => 'These values are read directly by the bundled nginx: save them and the root agent re-renders and hot-reloads within seconds. The listen IP and port also need the compose port mapping to follow (mclink apply-network).',
        '关掉 HTTPS（证书文件保留）' => 'Turn HTTPS off (certificate files are kept)',
        '已清除急救覆盖（%s）。之后以数据库 / 配置文件里的值为准。' => 'Cleared the emergency overrides (%s). The database and the configuration file are authoritative again.',
        '清除了急救覆盖：%s' => 'Cleared the emergency overrides: %s',
        '、' => ', ',
        '老地址 /login.php 兼容：面板入口始终是 index.php?action=login' => 'Legacy /login.php compatibility: the panel entry is always index.php?action=login',
        '由 mclink 管理台写入的急救覆盖：优先级高于数据库，面板设置页可以清除。' => 'Emergency override written by the mclink console: it wins over the database and can be cleared on the panel settings page.',

    ];

    return $table;
}
