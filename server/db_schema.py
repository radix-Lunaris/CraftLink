"""
MC-Link relay -- MySQL schema bootstrap.

The schema is created with `CREATE TABLE IF NOT EXISTS` at startup so the
relay can be deployed before the web panel exists, and the panel can be
deployed before anyone has logged in.

Tables
------
users        账号（房主与玩家都必须登录）
rooms        当前房间的实时镜像（面板读这张表）
room_sessions 每一次玩家会话的历史记录（用于流量/在线时长统计）
relay_events  审计日志：面板强删房间、踢人、登录失败等
"""

SCHEMA_STATEMENTS = [
    # ------------------------------------------------------------
    # 账号
    # ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS users (
        id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        username      VARCHAR(32)     NOT NULL,
        password_hash VARCHAR(255)    NOT NULL,
        is_admin      TINYINT(1)      NOT NULL DEFAULT 0,
        is_disabled   TINYINT(1)      NOT NULL DEFAULT 0,
        -- 三级角色：root / admin / player
        --
        -- `is_admin` 是旧版的两级字段，保留是为了兼容旧面板查询；
        -- `role` 才是权威字段，写入时两者保持同步。
        --   root   根管理员，全服务器唯一，不可降级、不可转让、不可另设
        --   admin  管理员：看房间密码、踢人、改除 root 外所有账号
        --   player 玩家：只能只读看面板
        role          VARCHAR(16)     NOT NULL DEFAULT 'player',
        created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_login_at DATETIME        NULL,
        last_login_ip VARCHAR(64)     NULL,
        login_count   INT UNSIGNED    NOT NULL DEFAULT 0,
        -- 改名限流：三年内最多改几次、上次改名时间
        rename_count  INT UNSIGNED    NOT NULL DEFAULT 0,
        rename_window_start DATETIME  NULL,
        renamed_at    DATETIME        NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_users_username (username)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # ------------------------------------------------------------
    # 房间实时状态（面板通过这张表看房间列表）
    # ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS rooms (
        room_id        VARCHAR(64)  NOT NULL,
        host_username  VARCHAR(32)  NULL,
        host_client_id VARCHAR(64)  NULL,
        host_ip        VARCHAR(64)  NULL,
        players        INT UNSIGNED NOT NULL DEFAULT 0,
        max_players    INT UNSIGNED NOT NULL DEFAULT 8,
        idle_tunnels   INT UNSIGNED NOT NULL DEFAULT 0,
        waiting_players INT UNSIGNED NOT NULL DEFAULT 0,
        bytes_to_host  BIGINT UNSIGNED NOT NULL DEFAULT 0,
        bytes_to_player BIGINT UNSIGNED NOT NULL DEFAULT 0,
        -- 中继进程实例 ID：每次进程启动都不同。
        -- 启动时把所有 instance_id <> 当前值的行删掉，重启残留的房间
        -- 一条语句就清干净（见 db_store.purge_other_instances）。
        instance_id    CHAR(32)     NULL,
        -- 房主控制连接的所有权令牌。清理时用它作为条件，
        -- 保证"旧进程的延迟清理"永远不会删掉新进程重建的同名房间。
        host_token     VARCHAR(64)  NULL,
        created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (room_id),
        KEY idx_rooms_updated (updated_at),
        KEY idx_rooms_instance (instance_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # ------------------------------------------------------------
    # 玩家会话历史
    # ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS room_sessions (
        id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        session_id     VARCHAR(32)     NOT NULL,
        room_id        VARCHAR(64)     NOT NULL,
        player_username VARCHAR(32)    NULL,
        player_client_id VARCHAR(64)   NULL,
        player_ip      VARCHAR(64)     NULL,
        started_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at       DATETIME        NULL,
        duration_sec   INT UNSIGNED    NULL,
        bytes_to_host  BIGINT UNSIGNED NOT NULL DEFAULT 0,
        bytes_to_player BIGINT UNSIGNED NOT NULL DEFAULT 0,
        end_reason     VARCHAR(32)     NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_sessions_session (session_id),
        KEY idx_sessions_room (room_id),
        KEY idx_sessions_started (started_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # ------------------------------------------------------------
    # 审计日志（面板操作 / 安全事件）
    # ------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS relay_events (
        id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actor      VARCHAR(32)  NULL,
        action     VARCHAR(32)  NOT NULL,
        room_id    VARCHAR(64)  NULL,
        session_id VARCHAR(32)  NULL,
        detail     VARCHAR(255) NULL,
        source_ip  VARCHAR(64)  NULL,
        PRIMARY KEY (id),
        KEY idx_events_created (created_at),
        KEY idx_events_room (room_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # ------------------------------------------------------------
    # 运行时设置（面板可改的开关）
    # ------------------------------------------------------------
    #
    # 放在数据库里而不是配置文件里，这样面板上点一下就改完了，
    # 而且重启容器后设置仍然保留。
    """
    CREATE TABLE IF NOT EXISTS settings (
        name       VARCHAR(64)  NOT NULL,
        value      VARCHAR(255) NOT NULL,
        updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                   ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]

# Columns added to existing tables after the first release.
#
# CREATE TABLE IF NOT EXISTS never touches a table that already exists, so a
# new column has to be added explicitly. These statements are applied at
# startup and a duplicate-column error is ignored, which makes them safe to
# run on every boot.
# One-shot data fixups, run after the column migrations.
#
# The role column is the authority, but `is_admin` predates it, so an existing
# installation has every account at role='player' until this runs. Getting it
# wrong would silently demote every administrator, so the rule is:
#
#     the FIRST administrator (lowest id, the one the relay bootstrapped)
#         -> root    (there is exactly one, forever)
#     every other administrator
#         -> admin
#     everybody else
#         -> player
#
# Written to be safely repeatable: `role = 'player'` is only ever applied to
# rows that still hold the default, so a deliberate promotion is never undone
# by a later restart.
ROLE_BACKFILL = [
    # Promote the oldest administrator to root -- but only if no root exists.
    "UPDATE users SET role = 'root' WHERE is_admin = 1 "
    "AND NOT EXISTS (SELECT 1 FROM (SELECT id FROM users WHERE role = 'root') "
    "AS existing_root) "
    "ORDER BY id LIMIT 1",
    # Remaining administrators become plain admins.
    "UPDATE users SET role = 'admin' WHERE is_admin = 1 AND role = 'player'",
    # Non-administrators stay players.
    "UPDATE users SET is_admin = 0 WHERE role = 'player' AND is_admin = 1",
]

#: The single root account is enforced here as well as in the application.
#: MySQL has no partial unique index, so this is a generated column trick: the
#: column is 'root' for the root row and NULL for everything else, and a unique
#: index ignores NULLs. A second root therefore becomes impossible at the
#: storage layer, not merely discouraged by the code above it.
ROOT_UNIQUE_MIGRATION = (
    "users",
    "uq_users_single_root",
    "ALTER TABLE users ADD COLUMN root_marker VARCHAR(8) "
    "GENERATED ALWAYS AS (IF(role = 'root', 'root', NULL)) VIRTUAL",
)

ROOT_UNIQUE_INDEX = (
    "users",
    "uq_users_root_marker",
    "ALTER TABLE users ADD UNIQUE INDEX uq_users_root_marker (root_marker)",
)

MIGRATIONS = [
    (
        "users",
        "rename_count",
        "ALTER TABLE users ADD COLUMN rename_count "
        "INT UNSIGNED NOT NULL DEFAULT 0",
    ),
    (
        "users",
        "rename_window_start",
        "ALTER TABLE users ADD COLUMN rename_window_start DATETIME NULL",
    ),
    (
        "users",
        "renamed_at",
        "ALTER TABLE users ADD COLUMN renamed_at DATETIME NULL",
    ),
    (
        "rooms",
        "instance_id",
        "ALTER TABLE rooms ADD COLUMN instance_id CHAR(32) NULL",
    ),
    (
        "rooms",
        "host_token",
        "ALTER TABLE rooms ADD COLUMN host_token VARCHAR(64) NULL",
    ),
    (
        "rooms",
        "idx_rooms_instance",
        "ALTER TABLE rooms ADD INDEX idx_rooms_instance (instance_id)",
    ),
    (
        "users",
        "role",
        "ALTER TABLE users ADD COLUMN role VARCHAR(16) "
        "NOT NULL DEFAULT 'player'",
    ),
    ROOT_UNIQUE_MIGRATION,
    ROOT_UNIQUE_INDEX,
]

# Optional starter admin account.
#
# The relay does NOT create any account by default. Set
# MCLINK_BOOTSTRAP_ADMIN=<username>:<password> once to create the first admin,
# then remove the variable. The hash is generated at startup with the same
# PBKDF2 parameters as the PHP panel, so both sides validate identically.
