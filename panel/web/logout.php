<?php
/**
 * Logout shortcut.
 *
 * The header posts to index.php?action=logout (which is CSRF protected).
 * This file exists so a plain link/GET keeps working too -- it only destroys
 * the local session, it does not change any server state.
 */

declare(strict_types=1);

require_once __DIR__ . '/../lib/common.php';

start_session();

$_SESSION = [];

if (ini_get('session.use_cookies')) {
    $params = session_get_cookie_params();
    setcookie(
        session_name(),
        '',
        time() - 42000,
        $params['path'],
        $params['domain'],
        (bool) $params['secure'],
        (bool) $params['httponly']
    );
}

session_destroy();

redirect('index.php?action=login');
