"""
MC-Link relay -- storage layer.

Two interchangeable implementations:

  MemoryStore  in-process dictionaries, used by the automated tests and by a
               relay started with MCLINK_DB_ENABLED=0
  MySQLStore   the real deployment: MySQL 8.0 reached by container name over
               the shared Docker network

The storage layer is a *side channel*. It never participates in the Minecraft
data path: the relay keeps serving raw bytes even if MySQL is down. When the
database is unreachable the relay logs a warning, falls back to the in-memory
mirror for the live state, and keeps relaying.

Design note: every database call happens off the event loop (aiomysql, or
pymysql inside a worker thread), so a slow query can never add latency to a
tunnel.
"""

import asyncio
import contextlib
import concurrent.futures
import os
import threading
import time

from db_schema import MIGRATIONS, ROLE_BACKFILL, SCHEMA_STATEMENTS

try:  # optional: only needed for the MySQL backend
    import pymysql
except Exception:  # pragma: no cover - depends on the environment
    pymysql = None

try:
    import aiomysql
except Exception:  # pragma: no cover - depends on the environment
    aiomysql = None


# ============================================================
# Database worker threads
# ============================================================
#
# Every blocking database call runs in a worker thread
# (`loop.run_in_executor`). Two things about that need to be contained, and
# both come from the same root cause: pymysql is synchronous, so a query can
# block in the kernel for a very long time (a `SELECT` behind a metadata lock,
# or a login whose greeting never arrives).
#
#   1. The asyncio side must not WAIT for it forever.
#      -> relay_server.db_step() puts a deadline on every startup/status step.
#
#   2. The process must not be held open by it at shutdown.
#      -> `DB_INFLIGHT` counts the calls currently inside a worker, and
#         `shutdown_guard()` (registered with atexit by relay_server) exits
#         hard when one is still stuck. Executor threads are non-daemon by
#         default and the interpreter JOINS them, so without this a single
#         wedged query would turn `docker stop` into a 10 s wait and a SIGKILL.
#
# Why not daemon threads: `ThreadPoolExecutor` starts each worker inside
# `_adjust_thread_count()` and records it afterwards, so an override that flips
# `thread.daemon` runs while the thread is still inside `Thread.__init__` --
# racy at best (measured: the flag silently stayed False) and able to raise
# "thread not initialized" anyway. A counter plus an explicit exit is
# deterministic and uses no private attributes.

#: Worker calls that have not returned yet.
DB_INFLIGHT = 0

#: Guards DB_INFLIGHT (worker threads and the atexit handler both touch it).
DB_INFLIGHT_LOCK = threading.Lock()

#: How long shutdown may wait for a stuck worker before exiting hard.
DB_SHUTDOWN_GRACE = 3.0


def inflight_calls():
    with DB_INFLIGHT_LOCK:
        return DB_INFLIGHT


def shutdown_guard():
    """
    atexit hook: never let a wedged database call keep the process alive.

    Called by relay_server at import time. Returns immediately in the normal
    case (no worker is inside a call). When one IS stuck -- which is exactly
    the failure this whole change is about -- the process has already stopped
    serving and everything worth flushing is flushed, so the only thing left
    is to not hang.
    """
    deadline = time.time() + DB_SHUTDOWN_GRACE

    while inflight_calls() and time.time() < deadline:
        time.sleep(0.1)

    if inflight_calls():
        print(
            "[DB] 还有数据库调用卡在工作线程里，直接退出（否则 docker stop 会被拖住）",
            flush=True,
        )
        os._exit(0)


#: One shared pool for every MySQL call in the process.
DB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="mclink-db"
)


async def run_db_call(function):
    """
    Run one blocking database function in a worker thread.

    Counted so `shutdown_guard()` can tell "idle" from "wedged". The counter
    is decremented in a `finally`, so an exception (or a timeout on the
    asyncio side) still releases it -- only a call that is genuinely stuck
    inside the kernel keeps the count up.
    """
    global DB_INFLIGHT

    loop = asyncio.get_event_loop()

    def _wrapped():
        global DB_INFLIGHT

        try:
            return function()
        finally:
            with DB_INFLIGHT_LOCK:
                DB_INFLIGHT -= 1

    with DB_INFLIGHT_LOCK:
        DB_INFLIGHT += 1

    try:
        return await loop.run_in_executor(DB_EXECUTOR, _wrapped)
    except BaseException:
        # The future was cancelled (or failed) before the worker started: the
        # worker never ran, so the `finally` above will not fire for it.
        with DB_INFLIGHT_LOCK:
            DB_INFLIGHT -= 1
        raise


# ============================================================
# Interface
# ============================================================

class RelayStore:
    """Interface implemented by both stores."""

    name = "abstract"
    persistent = False

    async def connect(self):
        raise NotImplementedError

    async def close(self):
        raise NotImplementedError

    async def ensure_schema(self):
        raise NotImplementedError

    async def healthy(self):
        raise NotImplementedError

    # ---- users ----
    async def get_user(self, username):
        raise NotImplementedError

    async def create_user(self, username, password_hash, is_admin=False,
                          role="player"):
        """
        Insert an account.

        `role` is the authority ("root" | "admin" | "player"); `is_admin` is
        kept because older code and the panel's SQL still read it, and it is
        derived from the role by the caller. Writing the role column is what
        makes a newly created administrator actually BE an administrator --
        leaving it at the 'player' default made every created admin show up as
        普通玩家 in the panel.
        """
        raise NotImplementedError

    async def touch_login(self, username, ip=""):
        raise NotImplementedError

    async def list_users(self, limit=200):
        raise NotImplementedError

    async def set_user_disabled(self, username, disabled):
        raise NotImplementedError

    async def set_user_role(self, username, role, is_admin=None):
        raise NotImplementedError

    async def set_user_password(self, username, password_hash):
        raise NotImplementedError

    async def delete_user(self, username):
        raise NotImplementedError

    async def rename_user(self, old_username, new_username):
        """
        Rename an account.

        Returns (ok, message, rename_info) where rename_info carries the
        rate-limit bookkeeping: {"count": n, "window_start": dt, "renamed_at": dt,
        "remaining": n}.
        """
        raise NotImplementedError

    async def bump_rename(self, username, count, reset_window=False):
        """Record one rename against the account's rate-limit window."""
        raise NotImplementedError

    # ---- rooms ----
    async def upsert_room(self, room):
        raise NotImplementedError

    async def delete_room(self, room_id):
        raise NotImplementedError

    async def delete_room_owned(self, room_id, host_token):
        """Delete a room row only if it is still owned by that host session."""
        raise NotImplementedError

    async def get_room(self, room_id):
        raise NotImplementedError

    async def list_rooms(self):
        raise NotImplementedError

    async def purge_stale_rooms(self, max_age_seconds):
        raise NotImplementedError

    async def purge_other_instances(self, instance_id, host_token=None):
        """
        Delete room rows that belong to a PREVIOUS relay process.

        This is the structural fix for "ghost rooms": after a restart the
        in-memory registry is empty while the `rooms` table still holds the
        previous run's rows, which then show up in the panel as rooms with no
        host and no name. One statement at boot removes all of them.

        When `host_token` is given, rows that are already owned by the current
        instance are kept (belt and braces in case of a rapid double start).
        """
        raise NotImplementedError

    # ---- sessions ----
    async def open_session(self, session):
        raise NotImplementedError

    async def close_session(self, session_id, bytes_to_host, bytes_to_player,
                            end_reason=""):
        raise NotImplementedError

    async def list_sessions(self, room_id=None, limit=100):
        raise NotImplementedError

    # ---- audit ----
    async def add_event(self, action, actor="", room_id="", session_id="",
                        detail="", source_ip=""):
        raise NotImplementedError

    async def list_events(self, limit=100):
        raise NotImplementedError

    async def clear_events(self):
        """Delete every audit event. Returns how many rows went away."""
        raise NotImplementedError

    async def purge_events(self, max_age_days):
        """
        Delete audit events older than `max_age_days`. Returns how many went away.

        This is the retention half of "日志每 10 天自动清理一次": a single
        DELETE, never a schema change, and it only ever touches relay_events.
        """
        raise NotImplementedError

    # ---- runtime settings (panel toggles) ----
    async def get_setting(self, name, default=None):
        raise NotImplementedError

    async def set_setting(self, name, value):
        raise NotImplementedError

    async def all_settings(self):
        raise NotImplementedError


# ============================================================
# In-memory store
# ============================================================

class MemoryStore(RelayStore):
    name = "memory"
    persistent = False

    def __init__(self):
        self.users = {}
        self.rooms = {}
        self.sessions = {}
        self.events = []
        self.settings = {}
        self._connected = False

    async def connect(self):
        self._connected = True

    async def close(self):
        self._connected = False

    async def ensure_schema(self):
        return True

    async def healthy(self):
        return self._connected

    # ---- users ----

    async def get_user(self, username):
        user = self.users.get(username)
        return dict(user) if user else None

    async def create_user(self, username, password_hash, is_admin=False,
                          role="player"):
        if username in self.users:
            return False

        self.users[username] = {
            "username": username,
            "password_hash": password_hash,
            "is_admin": 1 if is_admin else 0,
            "role": role or ("admin" if is_admin else "player"),
            "is_disabled": 0,
            "created_at": time.time(),
            "last_login_at": None,
            "last_login_ip": None,
            "login_count": 0,
        }
        return True

    async def touch_login(self, username, ip=""):
        user = self.users.get(username)
        if user is None:
            return

        user["last_login_at"] = time.time()
        user["last_login_ip"] = ip
        user["login_count"] = int(user.get("login_count") or 0) + 1

    async def list_users(self, limit=200):
        return [dict(u) for u in list(self.users.values())[:limit]]

    async def set_user_disabled(self, username, disabled):
        user = self.users.get(username)
        if user is None:
            return False
        user["is_disabled"] = 1 if disabled else 0
        return True

    async def set_user_role(self, username, role, is_admin=None):
        user = self.users.get(username)
        if user is None:
            return False
        role = str(role or "").strip().lower()
        user["role"] = role
        user["is_admin"] = (
            int(bool(is_admin)) if is_admin is not None
            else (1 if role in ("root", "admin") else 0)
        )
        return True

    async def set_user_password(self, username, password_hash):
        user = self.users.get(username)
        if user is None:
            return False
        user["password_hash"] = password_hash
        return True

    async def delete_user(self, username):
        return self.users.pop(username, None) is not None

    async def rename_user(self, old_username, new_username):
        old = self.users.get(old_username)

        if old is None:
            return False, "账号不存在", None

        if new_username in self.users:
            return False, "该用户名已被占用", None

        old["username"] = new_username
        self.users[new_username] = self.users.pop(old_username)

        # NOTE: this method does NOT bump rename_count any more.
        #
        # It used to, and MySQLStore.rename_user never did -- so the two stores
        # disagreed about who owns the bookkeeping, and the allowance was spent
        # twice per rename on the memory store (once here, once by
        # AccountService._bump_self_service). The visible effect was "3 per
        # month" behaving like 2.
        #
        # The POLICY (how many changes, over what period, for which roles)
        # belongs to the account service; a store only stores.
        old["renamed_at"] = time.time()

        return True, "", {
            "count": int(old.get("rename_count") or 0),
            "window_start": old.get("rename_window_start"),
            "renamed_at": old["renamed_at"],
        }

    async def bump_rename(self, username, count, reset_window=False):
        user = self.users.get(username)

        if user is None:
            return False

        user["rename_count"] = int(count)

        if reset_window or not user.get("rename_window_start"):
            user["rename_window_start"] = time.time()

        user["renamed_at"] = time.time()

        return True

    # ---- rooms ----

    async def upsert_room(self, room):
        self.rooms[room["room_id"]] = dict(room)

    async def delete_room(self, room_id):
        existed = self.rooms.pop(room_id, None) is not None

        for sid in [
            s for s, v in self.sessions.items()
            if v.get("room_id") == room_id and not v.get("ended_at")
        ]:
            self.sessions[sid]["ended_at"] = time.time()

        return existed

    async def delete_room_owned(self, room_id, host_token):
        room = self.rooms.get(room_id)

        if room is None:
            return False

        if host_token and room.get("host_token") not in (None, host_token):
            return False

        return await self.delete_room(room_id)

    async def get_room(self, room_id):
        room = self.rooms.get(room_id)
        return dict(room) if room else None

    async def list_rooms(self):
        return [dict(r) for r in self.rooms.values()]

    async def purge_stale_rooms(self, max_age_seconds):
        now = time.time()
        stale = [
            rid
            for rid, room in self.rooms.items()
            if now - float(room.get("updated_at") or 0) > max_age_seconds
        ]

        for rid in stale:
            self.rooms.pop(rid, None)

        return stale

    async def purge_other_instances(self, instance_id, host_token=None):
        doomed = [
            rid for rid, room in self.rooms.items()
            if room.get("instance_id") not in (None, instance_id)
        ]

        for rid in doomed:
            self.rooms.pop(rid, None)

        return doomed

    # ---- sessions ----

    async def open_session(self, session):
        self.sessions[session["session_id"]] = dict(session)

    async def close_session(
        self, session_id, bytes_to_host, bytes_to_player, end_reason=""
    ):
        session = self.sessions.get(session_id)

        if session is None:
            return

        session["ended_at"] = time.time()
        session["duration_sec"] = int(
            time.time() - float(session.get("started_at") or time.time())
        )
        session["bytes_to_host"] = int(bytes_to_host)
        session["bytes_to_player"] = int(bytes_to_player)
        session["end_reason"] = end_reason

    async def list_sessions(self, room_id=None, limit=100):
        rows = [
            dict(s)
            for s in self.sessions.values()
            if room_id is None or s.get("room_id") == room_id
        ]
        rows.sort(key=lambda s: s.get("started_at") or 0, reverse=True)
        return rows[:limit]

    # ---- audit ----

    async def add_event(self, action, actor="", room_id="", session_id="",
                        detail="", source_ip=""):
        self.events.append(
            {
                "created_at": time.time(),
                "actor": actor,
                "action": action,
                "room_id": room_id,
                "session_id": session_id,
                "detail": detail,
                "source_ip": source_ip,
            }
        )

        if len(self.events) > 5000:
            self.events = self.events[-2500:]

    async def list_events(self, limit=100):
        return list(reversed(self.events))[:limit]

    async def clear_events(self):
        """
        清空审计日志，返回清掉了多少条。

        Counted first so the panel can say "已清空（N 条）" instead of a bare
        "已清空" that looks identical to a no-op.
        """
        count = len(self.events)
        self.events = []

        return count

    async def purge_events(self, max_age_days):
        """
        删掉超过保留期的审计日志，返回删掉的条数。

        内存版按 created_at（epoch 秒）过滤 —— 和中继定时清理用的是同一套
        语义：只删"旧到不该再留着"的行，不碰别的表。
        """
        cutoff = time.time() - max(1, int(max_age_days)) * 86400

        before = len(self.events)
        self.events = [
            event for event in self.events
            if float(event.get("created_at") or 0) >= cutoff
        ]

        return before - len(self.events)

    # ---- runtime settings ----

    async def get_setting(self, name, default=None):
        return self.settings.get(name, default)

    async def set_setting(self, name, value):
        self.settings[name] = str(value)
        return True

    async def all_settings(self):
        return dict(self.settings)


# ============================================================
# MySQL store
# ============================================================

class MySQLStore(RelayStore):
    """
    MySQL 8.0 backend.

    Uses aiomysql when it is installed (the Docker image installs it) and
    transparently falls back to pymysql in a worker thread otherwise.
    """

    name = "mysql"
    persistent = True

    def __init__(self, config):
        self.config = config
        self.pool = None
        self._pymysql_conn = None
        self._lock = asyncio.Lock()

        # A single pymysql connection is NOT thread safe, and it is not safe
        # against interleaved async use either: two concurrent coroutines
        # inside run_in_executor() write to the same socket and MySQL answers
        # "Packet sequence number wrong - got 1 expected 5". Every raw query
        # therefore goes through this asyncio lock.
        self._raw_lock = asyncio.Lock()

        self.last_error = ""

    # --------------------------------------------------------
    # Connection
    # --------------------------------------------------------

    async def connect(self):
        """
        Open the connection.

        NOTE: the configuration module defines UPPER CASE names
        (DB_HOST, DB_PORT, ...). Reading `cfg.db_host` here raises
        AttributeError, which the SafeStore wrapper swallows -- so the relay
        silently reported "database DOWN" while TCP, DNS and credentials were
        all fine. Keep these names in sync with relay_config.py.
        """
        cfg = self.config

        if aiomysql is not None:
            # Not installed in the production image (requirements.txt ships
            # PyMySQL only) -- but if it ever is, it must not have a longer
            # leash than the pymysql path: aiomysql is asyncio based, so
            # `DB_QUERY_TIMEOUT` maps onto its socket read/write timeouts
            # instead of the manual settimeout() below.
            query_timeout = int(getattr(cfg, "DB_QUERY_TIMEOUT", 0) or 0)

            extra = (
                {"read_timeout": query_timeout, "write_timeout": query_timeout}
                if query_timeout > 0
                else {}
            )

            self.pool = await aiomysql.create_pool(
                host=cfg.DB_HOST,
                port=cfg.DB_PORT,
                user=cfg.DB_USER,
                password=cfg.DB_PASSWORD,
                db=cfg.DB_NAME,
                charset="utf8mb4",
                autocommit=True,
                minsize=1,
                maxsize=cfg.DB_POOL_SIZE,
                connect_timeout=cfg.DB_CONNECT_TIMEOUT,
                **extra,
            )
            return

        if pymysql is None:
            raise RuntimeError(
                "没有可用的 MySQL 驱动（缺少 aiomysql / PyMySQL）"
            )

        await self._reconnect_raw()

    async def close(self):
        if self.pool is not None:
            self.pool.close()
            with contextlib.suppress(Exception):
                await self.pool.wait_closed()
            self.pool = None

        if self._pymysql_conn is not None:
            with contextlib.suppress(Exception):
                self._pymysql_conn.close()
            self._pymysql_conn = None

    async def healthy(self):
        try:
            await self.fetchone("SELECT 1 AS ok")
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    # --------------------------------------------------------
    # Low level query helpers
    # --------------------------------------------------------

    async def _acquire(self):
        if self.pool is not None:
            return await self.pool.acquire()

        if self._pymysql_conn is not None:
            return self._pymysql_conn

        raise RuntimeError("数据库未连接")

    async def execute(self, sql, params=None):
        params = tuple(params or ())

        if self.pool is not None:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    return cur.rowcount

        return await self._raw_query(sql, params, rowcount_only=True)

    async def fetchall(self, sql, params=None):
        params = tuple(params or ())

        if self.pool is not None:
            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
                    return [dict(r) for r in rows]

        return await self._raw_query(sql, params, rowcount_only=False)

    async def fetchone(self, sql, params=None):
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    async def _raw_query(self, sql, params, rowcount_only):
        """
        Run one query on the shared pymysql connection.

        Serialised by `_raw_lock` and retried once after a reconnect, which
        covers both the interleaving bug and a connection dropped by MySQL's
        `wait_timeout` (8 hours by default).
        """
        def _run():
            conn = self._pymysql_conn

            if conn is None:
                raise RuntimeError("数据库未连接")

            conn.ping(reconnect=True)

            if rowcount_only:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.rowcount

            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]

        async with self._raw_lock:
            try:
                return await run_db_call(_run)
            except Exception as first_error:
                # One retry: the connection may have been closed server side.
                try:
                    self._pymysql_conn = None
                    await self._reconnect_raw()
                    return await run_db_call(_run)
                except Exception:
                    raise first_error

    async def _reconnect_raw(self):
        cfg = self.config

        # ---- 每条 SQL 的读写上限（「中继卡住」的第二道疫苗）----
        #
        # `connect_timeout` only covers establishing the TCP connection.
        # Everything after it -- reading the server greeting,
        # `set_character_set()` during login, every later query -- used to run
        # on a socket with NO timeout at all. Measured against a server that
        # accepts the connection and then stays silent, the worker thread sat
        # in `set_character_set()` forever; that is exactly how the relay ended
        # up stuck on
        #     [DB] connecting to mysql://mclink@web-mysql80:3306/mclink
        # with 19100 never opened.
        #
        # `read_timeout` / `write_timeout` are the driver's own knobs for this
        # and exist in the PINNED version (1.1.1 -- see requirements.txt, and
        # note that a newer pymysql on this dev machine accepts more). They are
        # enforced per read/write by pymysql itself, not by asyncio -- which
        # matters, because cancelling a synchronous call from the event loop
        # leaves the worker thread running anyway, with a half-read packet on a
        # connection that later queries would then reuse.
        #
        # DB_QUERY_TIMEOUT=0 restores the old unbounded behaviour, for
        # diagnosing a genuinely long-running migration by hand.
        timeout = int(getattr(cfg, "DB_QUERY_TIMEOUT", 0) or 0)
        connect_timeout = int(cfg.DB_CONNECT_TIMEOUT or 8)

        def _connect():
            return pymysql.connect(
                host=cfg.DB_HOST,
                port=cfg.DB_PORT,
                user=cfg.DB_USER,
                password=cfg.DB_PASSWORD,
                database=cfg.DB_NAME,
                charset="utf8mb4",
                autocommit=True,
                connect_timeout=connect_timeout,
                read_timeout=timeout or None,
                write_timeout=timeout or None,
            )

        self._pymysql_conn = await run_db_call(_connect)

    # --------------------------------------------------------
    # Schema
    # --------------------------------------------------------

    async def ensure_schema(self):
        for statement in SCHEMA_STATEMENTS:
            await self.execute(statement)

        # Additive migrations for tables that already exist. A duplicate
        # column error means the migration was already applied, which is the
        # normal case from the second boot onwards.
        #
        # IMPORTANT: a failure here does NOT abort the rest of the list.
        #
        # These used to `raise` on anything unexpected, which meant one bad
        # statement silently skipped every migration after it. The `role`
        # column is the last one, so a failure earlier in the list would leave
        # it missing -- and the panel selects it, so every login returned 500
        # with no explanation anywhere. Reporting and continuing is strictly
        # better: a missing index is survivable, a missing column is not.
        migration_errors = []

        for _table, _column, statement in MIGRATIONS:
            try:
                await self.execute(statement)
            except Exception as e:
                message = str(e).lower()

                # 1060 duplicate column, 1061 duplicate key name, 1091 can't
                # drop something that is not there.
                if (
                    "duplicate" in message
                    or "1060" in message
                    or "1061" in message
                    or "1091" in message
                ):
                    continue

                migration_errors.append(f"{_column}: {e}")

        # One-shot data fixups (the role backfill). Idempotent by
        # construction, and they must run AFTER the column migrations above
        # because they read the columns those statements add.
        for statement in ROLE_BACKFILL:
            try:
                await self.execute(statement)
            except Exception as e:
                migration_errors.append(f"role backfill: {e}")

        # ---- verify the result ----
        #
        # Do not trust that the ALTER worked. Probe for the column and, when it
        # is missing, say so LOUDLY with the real driver error -- this is
        # exactly the situation that produced an unexplained 500 on the login
        # page, and a bare exception string is the difference between a
        # two-minute fix and an hour of guessing.
        role_ok = await self._column_exists("users", "role")

        if not role_ok:
            self.role_column_missing = True

            print(
                "[DB] !! users.role 列不存在 —— 面板登录会失败。",
                flush=True,
            )

            for problem in migration_errors[:5]:
                print(f"[DB]    迁移失败原因：{problem}", flush=True)

            if not migration_errors:
                print(
                    "[DB]    迁移没有报错但列就是不在，检查 MySQL 版本是否 >= 5.7",
                    flush=True,
                )
        else:
            self.role_column_missing = False

            if migration_errors:
                # Harmless in itself, but worth knowing about.
                print(
                    "[DB] 部分迁移语句失败（role 列已就绪，不影响使用）：",
                    flush=True,
                )
                for problem in migration_errors[:3]:
                    print(f"[DB]    {problem}", flush=True)

        return True

    async def _column_exists(self, table, column):
        """Whether a column exists. Never raises."""
        try:
            row = await self.fetchone(
                "SELECT COUNT(*) AS n FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "AND table_name = %s AND column_name = %s",
                (table, column),
            )

            return bool(int((row or {}).get("n") or 0))
        except Exception:
            return False

    # --------------------------------------------------------
    # Users
    # --------------------------------------------------------

    async def get_user(self, username):
        """
        Read one account row.

        `role` MUST be in this SELECT. It was not, and the consequences were
        not subtle:

          * role_of() fell back to is_admin, so the ROOT account was reported
            as a plain admin. The panel showed root's row correctly (it reads
            the column straight from SQL) while the relay said "admin" -- so
            the person who owns the server was told 「只有根管理员才能提权」 by
            their own account, and every account they created came out as a
            player.
          * the monthly self-service counter read rename_count /
            rename_window_start from here too, so on MySQL the window always
            looked expired and the limit never applied at all.

        Anything role_of(), rename_budget() or the panel reads must be in this
        list; a missing column is invisible until it changes behaviour.
        """
        return await self.fetchone(
            "SELECT username, password_hash, is_admin, is_disabled, role, "
            "       login_count, last_login_at, last_login_ip, created_at, "
            "       rename_count, rename_window_start, renamed_at "
            "FROM users WHERE username = %s",
            (username,),
        )

    async def create_user(self, username, password_hash, is_admin=False,
                          role="player"):
        # The role column is written explicitly. Without it MySQL used the
        # column default ('player'), so an account created as an administrator
        # showed up as 普通玩家 everywhere that trusts the role.
        role = (role or "").strip().lower()

        if role not in ("root", "admin", "player"):
            role = "admin" if is_admin else "player"

        try:
            await self.execute(
                "INSERT INTO users (username, password_hash, is_admin, role) "
                "VALUES (%s, %s, %s, %s)",
                (username, password_hash, 1 if is_admin else 0, role),
            )
            return True
        except Exception as e:
            if "Duplicate" in str(e) or "1062" in str(e):
                return False
            raise

    async def touch_login(self, username, ip=""):
        await self.execute(
            "UPDATE users SET last_login_at = NOW(), last_login_ip = %s, "
            "login_count = login_count + 1 WHERE username = %s",
            (ip[:64], username),
        )

    async def list_users(self, limit=200):
        return await self.fetchall(
            "SELECT username, is_admin, is_disabled, login_count, "
            "       created_at, last_login_at, last_login_ip "
            "FROM users ORDER BY id DESC LIMIT %s",
            (int(limit),),
        )

    async def set_user_disabled(self, username, disabled):
        count = await self.execute(
            "UPDATE users SET is_disabled = %s WHERE username = %s",
            (1 if disabled else 0, username),
        )
        return count > 0

    async def set_user_role(self, username, role, is_admin=None):
        """
        Promote / demote an account.

        `is_admin` is derived from the role when not given, because the panel
        and the relay both read that legacy column in places -- letting the two
        disagree is exactly the bug that made every created administrator show
        up as a 普通玩家.
        """
        role = str(role or "").strip().lower()

        if role not in ("root", "admin", "player"):
            return False

        if is_admin is None:
            is_admin = role in ("root", "admin")

        count = await self.execute(
            "UPDATE users SET role = %s, is_admin = %s WHERE username = %s",
            (role, 1 if is_admin else 0, username),
        )

        return count > 0

    async def set_user_password(self, username, password_hash):
        count = await self.execute(
            "UPDATE users SET password_hash = %s WHERE username = %s",
            (password_hash, username),
        )
        return count > 0

    async def delete_user(self, username):
        # room_sessions rows are intentionally kept: they are the history the
        # panel shows, and they reference the username as plain text.
        count = await self.execute(
            "DELETE FROM users WHERE username = %s", (username,)
        )
        return count > 0

    async def rename_user(self, old_username, new_username):
        try:
            count = await self.execute(
                "UPDATE users SET username = %s WHERE username = %s",
                (new_username, old_username),
            )
        except Exception as e:
            message = str(e).lower()

            if "duplicate" in message or "1062" in message:
                return False, "该用户名已被占用", None

            raise

        if count <= 0:
            return False, "账号不存在", None

        row = await self.fetchone(
            "SELECT rename_count, rename_window_start, renamed_at "
            "FROM users WHERE username = %s",
            (new_username,),
        )

        info = {
            "count": int((row or {}).get("rename_count") or 0),
            "window_start": (row or {}).get("rename_window_start"),
            "renamed_at": (row or {}).get("renamed_at"),
        }

        return True, "", info

    async def bump_rename(self, username, count, reset_window=False):
        if reset_window:
            await self.execute(
                "UPDATE users SET rename_count = %s, "
                "rename_window_start = NOW(), renamed_at = NOW() "
                "WHERE username = %s",
                (int(count), username),
            )
        else:
            await self.execute(
                "UPDATE users SET rename_count = %s, renamed_at = NOW() "
                "WHERE username = %s",
                (int(count), username),
            )

        return True

    # --------------------------------------------------------
    # Rooms
    # --------------------------------------------------------

    async def upsert_room(self, room):
        await self.execute(
            """
            INSERT INTO rooms (
                room_id, host_username, host_client_id, host_ip,
                players, max_players, idle_tunnels, waiting_players,
                bytes_to_host, bytes_to_player, instance_id, host_token,
                created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
            ON DUPLICATE KEY UPDATE
                host_username = VALUES(host_username),
                host_client_id = VALUES(host_client_id),
                host_ip = VALUES(host_ip),
                players = VALUES(players),
                max_players = VALUES(max_players),
                idle_tunnels = VALUES(idle_tunnels),
                waiting_players = VALUES(waiting_players),
                bytes_to_host = VALUES(bytes_to_host),
                bytes_to_player = VALUES(bytes_to_player),
                -- 认领：新进程重启后会把自己实例的 ID 写进去，
                -- 这样"残留行"就被本次实例接管，不会一直留着。
                instance_id = VALUES(instance_id),
                host_token = VALUES(host_token),
                -- updated_at 必须显式写。只靠 ON UPDATE CURRENT_TIMESTAMP
                -- 是不够的：当其它列的值都没变时 MySQL 会跳过写入，
                -- 时间戳不会动，活跃房间就会被误判为陈旧而被清理。
                updated_at = NOW()
            """,
            (
                room.get("room_id", "")[:64],
                (room.get("host_username") or "")[:32],
                (room.get("host_client_id") or "")[:64],
                (room.get("host_ip") or "")[:64],
                int(room.get("players") or 0),
                int(room.get("max_players") or 0),
                int(room.get("idle_tunnels") or 0),
                int(room.get("waiting_players") or 0),
                int(room.get("bytes_to_host") or 0),
                int(room.get("bytes_to_player") or 0),
                (room.get("instance_id") or None),
                (room.get("host_token") or None),
            ),
        )

    async def delete_room(self, room_id):
        count = await self.execute(
            "DELETE FROM rooms WHERE room_id = %s", (room_id,)
        )
        return count > 0

    async def delete_room_owned(self, room_id, host_token):
        """
        Delete a room row only while it still belongs to that host session.

        This is the ownership token from the reference design: a late cleanup
        from a dead process can never remove a room that a reconnected host has
        just re-created under the same name.
        """
        if not host_token:
            return await self.delete_room(room_id)

        count = await self.execute(
            "DELETE FROM rooms WHERE room_id = %s AND host_token = %s",
            (room_id, host_token),
        )
        return count > 0

    async def get_room(self, room_id):
        return await self.fetchone(
            "SELECT * FROM rooms WHERE room_id = %s", (room_id,)
        )

    async def list_rooms(self):
        return await self.fetchall(
            "SELECT * FROM rooms ORDER BY created_at DESC"
        )

    async def purge_stale_rooms(self, max_age_seconds):
        rows = await self.fetchall(
            "SELECT room_id FROM rooms "
            "WHERE updated_at < (NOW() - INTERVAL %s SECOND)",
            (int(max_age_seconds),),
        )

        ids = [r["room_id"] for r in rows]

        for room_id in ids:
            await self.delete_room(room_id)

        return ids

    async def purge_other_instances(self, instance_id, host_token=None):
        rows = await self.fetchall(
            "SELECT room_id, host_token FROM rooms "
            "WHERE instance_id IS NULL OR instance_id <> %s",
            (instance_id,),
        )

        doomed = []

        for row in rows or []:
            room_id = row.get("room_id")

            if not room_id:
                continue

            # Keep rows that somehow already belong to this process.
            if host_token and row.get("host_token") == host_token:
                continue

            doomed.append(room_id)

        for room_id in doomed:
            await self.delete_room(room_id)

        return doomed

    # --------------------------------------------------------
    # Sessions
    # --------------------------------------------------------

    async def open_session(self, session):
        await self.execute(
            """
            INSERT INTO room_sessions (
                session_id, room_id, player_username, player_client_id,
                player_ip, started_at
            ) VALUES (%s,%s,%s,%s,%s,NOW())
            ON DUPLICATE KEY UPDATE
                room_id = VALUES(room_id),
                player_username = VALUES(player_username),
                player_client_id = VALUES(player_client_id),
                player_ip = VALUES(player_ip)
            """,
            (
                session.get("session_id", "")[:32],
                session.get("room_id", "")[:64],
                (session.get("player_username") or "")[:32],
                (session.get("player_client_id") or "")[:64],
                (session.get("player_ip") or "")[:64],
            ),
        )

    async def close_session(
        self, session_id, bytes_to_host, bytes_to_player, end_reason=""
    ):
        await self.execute(
            """
            UPDATE room_sessions
               SET ended_at = NOW(),
                   duration_sec = TIMESTAMPDIFF(SECOND, started_at, NOW()),
                   bytes_to_host = %s,
                   bytes_to_player = %s,
                   end_reason = %s
             WHERE session_id = %s
            """,
            (
                int(bytes_to_host),
                int(bytes_to_player),
                (end_reason or "")[:32],
                session_id,
            ),
        )

    async def list_sessions(self, room_id=None, limit=100):
        if room_id:
            return await self.fetchall(
                "SELECT * FROM room_sessions WHERE room_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (room_id, int(limit)),
            )

        return await self.fetchall(
            "SELECT * FROM room_sessions ORDER BY id DESC LIMIT %s",
            (int(limit),),
        )

    # --------------------------------------------------------
    # Audit
    # --------------------------------------------------------

    async def add_event(self, action, actor="", room_id="", session_id="",
                        detail="", source_ip=""):
        with contextlib.suppress(Exception):
            await self.execute(
                "INSERT INTO relay_events "
                "(actor, action, room_id, session_id, detail, source_ip) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    (actor or "")[:32],
                    (action or "")[:32],
                    (room_id or "")[:64],
                    (session_id or "")[:32],
                    (detail or "")[:255],
                    (source_ip or "")[:64],
                ),
            )

    async def list_events(self, limit=100):
        return await self.fetchall(
            "SELECT * FROM relay_events ORDER BY id DESC LIMIT %s",
            (int(limit),),
        )

    async def clear_events(self):
        """
        清空审计日志。

        Counted first so the caller can report how many rows were removed: a
        "已清空" that deleted nothing looks exactly like a failure otherwise.
        """
        try:
            row = await self.fetchone(
                "SELECT COUNT(*) AS n FROM relay_events"
            )
            count = int((row or {}).get("n") or 0)
        except Exception:
            count = 0

        await self.execute("DELETE FROM relay_events")

        return count

    async def purge_events(self, max_age_days):
        """
        删掉超过保留期的审计日志，返回删掉的条数。

        一条 DELETE 就够：relay_events 上有 idx_events_created(created_at)
        索引，这个范围删除走的就是它。INTERVAL 后面的天数是整数，不是拼出来
        的字符串；除了 relay_events 这张表，什么都不碰。
        """
        return await self.execute(
            "DELETE FROM relay_events "
            "WHERE created_at < (NOW() - INTERVAL %s DAY)",
            (max(1, int(max_age_days)),),
        )

    # --------------------------------------------------------
    # Runtime settings
    # --------------------------------------------------------

    async def get_setting(self, name, default=None):
        row = await self.fetchone(
            "SELECT value FROM settings WHERE name = %s", (name,)
        )

        return row["value"] if row else default

    async def set_setting(self, name, value):
        await self.execute(
            "INSERT INTO settings (name, value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE value = VALUES(value), updated_at = NOW()",
            (name[:64], str(value)[:255]),
        )
        return True

    async def all_settings(self):
        rows = await self.fetchall("SELECT name, value FROM settings")

        return {r["name"]: r["value"] for r in rows or []}


# ============================================================
# Facade with graceful degradation
# ============================================================

class SafeStore(RelayStore):
    """
    Wraps a store and swallows database errors.

    Rationale: the Minecraft relay must keep working even when MySQL is down
    or being upgraded. Every failed call is counted and reported once, and the
    caller never sees an exception. Live room state stays authoritative in the
    relay's own memory; the database is only a mirror for the panel.
    """

    def __init__(self, store):
        self.store = store
        self.name = store.name
        self.persistent = store.persistent
        self.errors = 0
        self.degraded = False
        self.last_error = ""
        self._last_report = 0.0

    # ---- pass through, guarded ----

    async def _guard(self, method, *args, **kwargs):
        try:
            result = await getattr(self.store, method)(*args, **kwargs)

            if self.degraded:
                self.degraded = False

            return result

        except Exception as e:
            self.errors += 1
            self.last_error = str(e)
            self.degraded = True
            return None

    async def connect(self):
        return await self._guard("connect")

    async def close(self):
        return await self._guard("close")

    async def ensure_schema(self):
        return await self._guard("ensure_schema")

    # ---- raw query passthrough (used by --dbcheck and diagnostics) ----

    async def execute(self, sql, params=None):
        return await self._guard("execute", sql, params)

    async def fetchall(self, sql, params=None):
        return await self._guard("fetchall", sql, params) or []

    async def fetchone(self, sql, params=None):
        return await self._guard("fetchone", sql, params)

    async def healthy(self):
        try:
            return await self.store.healthy()
        except Exception:
            return False

    async def get_user(self, username):
        return await self._guard("get_user", username)

    async def create_user(self, username, password_hash, is_admin=False,
                          role="player"):
        return await self._guard(
            "create_user", username, password_hash, is_admin, role
        )

    async def touch_login(self, username, ip=""):
        return await self._guard("touch_login", username, ip)

    async def list_users(self, limit=200):
        return await self._guard("list_users", limit) or []

    async def set_user_disabled(self, username, disabled):
        return await self._guard("set_user_disabled", username, disabled)

    async def set_user_role(self, username, role, is_admin=None):
        return await self._guard("set_user_role", username, role, is_admin)

    async def set_user_password(self, username, password_hash):
        return await self._guard("set_user_password", username, password_hash)

    async def delete_user(self, username):
        return await self._guard("delete_user", username)

    async def rename_user(self, old_username, new_username):
        result = await self._guard("rename_user", old_username, new_username)

        if result is None:
            return False, "数据库不可用，请稍后再试", None

        return result

    async def bump_rename(self, username, count, reset_window=False):
        return await self._guard("bump_rename", username, count, reset_window)

    async def upsert_room(self, room):
        return await self._guard("upsert_room", room)

    async def delete_room(self, room_id):
        return await self._guard("delete_room", room_id)

    async def delete_room_owned(self, room_id, host_token):
        return await self._guard("delete_room_owned", room_id, host_token)

    async def get_room(self, room_id):
        return await self._guard("get_room", room_id)

    async def list_rooms(self):
        return await self._guard("list_rooms") or []

    async def purge_stale_rooms(self, max_age_seconds):
        return await self._guard("purge_stale_rooms", max_age_seconds) or []

    async def purge_other_instances(self, instance_id, host_token=None):
        return await self._guard(
            "purge_other_instances", instance_id, host_token
        ) or []

    async def open_session(self, session):
        return await self._guard("open_session", session)

    async def close_session(self, session_id, bytes_to_host, bytes_to_player,
                            end_reason=""):
        return await self._guard(
            "close_session",
            session_id,
            bytes_to_host,
            bytes_to_player,
            end_reason,
        )

    async def list_sessions(self, room_id=None, limit=100):
        return await self._guard("list_sessions", room_id, limit) or []

    async def add_event(self, action, actor="", room_id="", session_id="",
                        detail="", source_ip=""):
        return await self._guard(
            "add_event", action, actor, room_id, session_id, detail, source_ip
        )

    async def list_events(self, limit=100):
        return await self._guard("list_events", limit) or []

    async def clear_events(self):
        return await self._guard("clear_events") or 0

    async def purge_events(self, max_age_days):
        return await self._guard("purge_events", max_age_days) or 0

    async def get_setting(self, name, default=None):
        value = await self._guard("get_setting", name, default)

        return default if value is None else value

    async def set_setting(self, name, value):
        return await self._guard("set_setting", name, value)

    async def all_settings(self):
        return await self._guard("all_settings") or {}


def build_store(config):
    """Create the store described by the configuration."""
    if not config.DB_ENABLED:
        return SafeStore(MemoryStore())

    return SafeStore(MySQLStore(config))
