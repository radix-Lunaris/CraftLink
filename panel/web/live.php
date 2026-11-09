<?php
/**
 * 实时数据接口 —— 给页面的自动刷新用。
 *
 * ============================================================================
 *  WHY THIS EXISTS
 * ============================================================================
 *
 * Auto-refresh used to reload the WHOLE page every 10 seconds. Two problems:
 *
 *   * 10 seconds is sluggish for a dashboard that only has to say "3 rooms".
 *   * A full reload throws the user back to the top of the page and discards
 *     whatever they were doing. Somebody halfway through typing a new account
 *     name would lose it, which is the kind of thing that makes a panel feel
 *     hostile.
 *
 * So the page now polls this endpoint with fetch() and patches the few numbers
 * that actually change. Everything else -- scroll position, open forms, text
 * being typed -- is untouched, because the document is never replaced.
 *
 * It returns a small fixed set of scalars, which is also why it is cheap: no
 * HTML rendering, no list queries, just the relay's in-memory counters.
 */

declare(strict_types=1);

require __DIR__ . '/../lib/common.php';

start_session();

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

// Same rule as the pages: you must be logged in to read them.
if (current_user() === null) {
    http_response_code(401);
    echo json_encode(['ok' => false, 'error' => 'not_logged_in']);
    exit;
}

$stats = relay_api('GET', '/stats');
$rooms = relay_api('GET', '/rooms');

$service = is_array($stats['data'] ?? null) ? $stats['data'] : [];
$roomList = is_array($rooms['data'] ?? null) ? $rooms['data'] : [];

$liveRooms = 0;
$livePlayers = 0;
$liveMembers = 0;
$staleRooms = 0;

foreach ($roomList as $room) {
    if (($room['live'] ?? true) === false) {
        $staleRooms++;
        continue;
    }

    $liveRooms++;
    $livePlayers += (int) ($room['players'] ?? 0);
    // 「房间内在线」：人在房间里但还没进世界也算。两个数字分开显示，
    // 否则"人明明在房间里，面板却是 0"。
    $liveMembers += (int) ($room['members'] ?? 0);
}

// Panel-side database check, same as the dashboard, so the two never disagree.
$panelDbOk = true;

try {
    db()->query('SELECT 1');
} catch (Throwable $e) {
    $panelDbOk = false;
}

echo json_encode([
    'ok' => true,
    'time' => date('H:i:s'),
    'stats' => [
        'rooms' => $liveRooms,
        'members' => $liveMembers,
        'players' => $livePlayers,
        'idle_tunnels' => (int) ($service['idle_tunnels'] ?? 0),
        'online_accounts' => (int) ($service['online_account_count'] ?? 0),
        'max_players' => (int) ($service['max_players']
            ?? $service['max_players_per_room']
            ?? 0),
        'uptime_text' => seconds_to_human((int) round($service['uptime'] ?? 0)),
        'stale_rooms' => $staleRooms,
        'relay_ok' => (bool) $stats['ok'],
        'panel_db_ok' => $panelDbOk,
        'auth_required' => $service['auth_required'] ?? null,
        'admin_requests' => (int) (($service['admin_api']['requests'] ?? 0)),
    ],
], JSON_UNESCAPED_UNICODE);
