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
        '一个房间里最多能有多少人（含房主）。默认 8，可设置为 2-64 人；实际游戏人数取决于游戏服务端设置。' => 'People per room, host included. Default 8; configurable from 2 to 64. Actual game capacity depends on the game server settings.',
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
    ];

    return $table;
}
