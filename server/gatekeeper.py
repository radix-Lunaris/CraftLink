"""
MC-Link relay -- accounts, tokens and login throttling.

============================================================================
 WHY THIS IS A SEPARATE MODULE
============================================================================

The relay's TCP data path is frozen: `HOST <room>` / `HOST_TUNNEL <room>` /
`PLAYER <room>` and the raw byte forwarding must keep working exactly as
verified. Authentication is therefore implemented as:

  * a NEW connection type used only for logging in:
        LOGIN __mclink__ <username> <password>
  * two OPTIONAL extra fields on the existing handshake line:
        HOST   <room> <client_id> <username> <token>
        PLAYER <room> <client_id> <username> <token>

An old client that sends only `HOST <room>` still parses identically, and the
data tunnels are never touched by any of this.
"""

import asyncio
import base64
import contextlib
import inspect
import secrets
import time

from auth import hash_password, verify_password

# Sent as the "room" field of the LOGIN connection so a legacy server would
# simply treat it as a (harmless) room name instead of crashing.
SENTINEL = "__mclink__"


def _as_timestamp(value):
    """Normalise a MySQL DATETIME / epoch number into a float timestamp."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    try:
        import datetime

        if isinstance(value, datetime.datetime):
            return value.timestamp()

        if isinstance(value, datetime.date):
            return time.mktime(value.timetuple())
    except Exception:
        pass

    return None


# ============================================================
# Token manager
# ============================================================

class TokenManager:
    """
    Issues and validates short opaque session tokens.

    Storage is in-process: the relay is a single container, and a token only
    has to survive as long as the client keeps the connection. Tokens are
    bound to the username and expire after TOKEN_TTL seconds; the last
    activity time is refreshed on each successful validation so an active
    session never expires mid-game.
    """

    def __init__(self, ttl_seconds=12 * 3600, max_tokens=5000):
        self.ttl = int(ttl_seconds)
        self.max_tokens = int(max_tokens)
        self._tokens = {}
        self.lock = asyncio.Lock()

    def _now(self):
        return time.time()

    async def issue(self, username, is_admin=False, client_id="", role=""):
        token = base64.urlsafe_b64encode(
            secrets.token_bytes(24)
        ).decode().rstrip("=")

        async with self.lock:
            self._cleanup_locked()

            self._tokens[token] = {
                "username": username,
                "is_admin": bool(is_admin),
                #: The three tier role, carried on the token so a HOST/PLAYER
                #: handshake and the client UI can show it without another
                #: database read.
                "role": role or ("admin" if is_admin else "player"),
                "client_id": client_id,
                "issued_at": self._now(),
                "last_seen": self._now(),
            }

            # Hard cap so a token flood cannot grow memory without bound.
            if len(self._tokens) > self.max_tokens:
                oldest = sorted(
                    self._tokens.items(),
                    key=lambda kv: kv[1]["last_seen"],
                )
                for key, _ in oldest[: len(self._tokens) - self.max_tokens]:
                    self._tokens.pop(key, None)

        return token

    async def validate(self, token, touch=True):
        """Return the token record or None."""
        if not token:
            return None

        async with self.lock:
            record = self._tokens.get(token)

            if record is None:
                return None

            if self._now() - record["last_seen"] > self.ttl:
                self._tokens.pop(token, None)
                return None

            if touch:
                record["last_seen"] = self._now()

            return dict(record)

    async def revoke(self, token):
        async with self.lock:
            return self._tokens.pop(token, None) is not None

    async def revoke_user(self, username):
        """Kick every token of one account (used by the panel)."""
        async with self.lock:
            doomed = [
                t for t, r in self._tokens.items()
                if r["username"] == username
            ]
            for token in doomed:
                self._tokens.pop(token, None)

        return doomed

    async def rebind_user(self, old_username, new_username):
        """
        Point every LIVE token of an account at its new username.

        A rename must NOT log the player out (explicit requirement: the new
        name takes effect immediately without a reconnect). Tokens are stored
        with the username inside them, so without this the token would keep
        naming an account row that no longer exists and the next HOST/PLAYER
        handshake would fail the "is this account disabled" lookup.

        Returns the number of tokens rebound.
        """
        if not old_username or not new_username:
            return 0

        changed = 0

        async with self.lock:
            for record in self._tokens.values():
                if record.get("username") == old_username:
                    record["username"] = new_username
                    changed += 1

        return changed

    async def active_users(self):
        async with self.lock:
            self._cleanup_locked()
            return sorted({r["username"] for r in self._tokens.values()})

    async def count(self):
        async with self.lock:
            self._cleanup_locked()
            return len(self._tokens)

    def _cleanup_locked(self):
        now = self._now()
        expired = [
            t for t, r in self._tokens.items()
            if now - r["last_seen"] > self.ttl
        ]
        for token in expired:
            self._tokens.pop(token, None)


#: 密码规则，三处（中继 / 面板 / SSH 重置工具）必须完全一致。
#:
#: 6-20 位，只能字母和数字。Why so narrow: this password gets typed by hand
#: into the Minecraft client, the panel login box and the SSH recovery tool,
#: and symbols are what people mistype. It also closes a real gap -- the rule
#: used to live in three separate places, and one of them only checked the
#: LENGTH, so a password containing "_" was accepted there while the others
#: would have refused it.
PASSWORD_MIN = 6
PASSWORD_MAX = 20


def password_problem(password):
    """
    返回 "" 表示密码合格，否则返回人话原因。

    注意 isalnum() 对中文也返回 True，所以必须同时要求 ASCII。
    """
    text = str(password or "")

    if len(text) < PASSWORD_MIN:
        return f"密码至少 {PASSWORD_MIN} 位"

    if len(text) > PASSWORD_MAX:
        return f"密码最长 {PASSWORD_MAX} 位"

    if not all(c.isascii() and c.isalnum() for c in text):
        return "密码只能使用字母和数字（不能有符号、空格或中文）"

    return ""


# ============================================================
# Login throttling
# ============================================================

class LoginThrottle:
    """
    Simple per-IP sliding window limiter.

    Deliberately dependency free: the threat model here is a friend group
    server exposed on one port, not a public SaaS.
    """

    def __init__(self, max_attempts=8, window_seconds=60):
        self.max_attempts = int(max_attempts)
        self.window = int(window_seconds)
        self._attempts = {}
        self.lock = asyncio.Lock()

    async def allow(self, ip):
        async with self.lock:
            now = time.time()
            bucket = [
                stamp for stamp in self._attempts.get(ip, [])
                if now - stamp < self.window
            ]
            self._attempts[ip] = bucket

            return len(bucket) < self.max_attempts

    async def record_failure(self, ip):
        async with self.lock:
            self._attempts.setdefault(ip, []).append(time.time())

            # Keep the table from growing forever.
            if len(self._attempts) > 2000:
                now = time.time()
                self._attempts = {
                    key: [s for s in value if now - s < self.window]
                    for key, value in self._attempts.items()
                }

    async def reset(self, ip):
        async with self.lock:
            self._attempts.pop(ip, None)


# ============================================================
# Account service
# ============================================================

class AccountService:
    """
    Ties the storage layer, password hashing, tokens and throttling together.
    """

    def __init__(self, store, tokens, throttle, require_account=True,
                 bootstrap_admin="", on_rename=None, settings=None,
                 on_rename_tokens=None):
        self.store = store
        self.tokens = tokens
        self.throttle = throttle
        self.require_account = bool(require_account)
        self.bootstrap_admin = bootstrap_admin or ""
        self.last_auth_error = ""

        # Called as on_rename(old_name, new_name) after a successful rename so
        # the live rooms/sessions can adopt the new display name immediately,
        # without dropping the player's connection.
        self.on_rename = on_rename

        # Called as on_rename_tokens(old_name, new_name): rebinds the caller's
        # live session tokens so a rename never invalidates a login.
        self.on_rename_tokens = on_rename_tokens

        # Runtime settings (panel switches). When present it wins over the
        # value baked into the environment, so the panel can toggle
        # "must log in" without a container restart.
        self.settings = settings

    # --------------------------------------------------------

    @property
    def accounts_required(self):
        """Live value of the 'must log in' switch."""
        if self.settings is not None:
            return self.settings.get_bool(
                "require_account", self.require_account
            )

        return self.require_account

    # --------------------------------------------------------
    # Startup
    # --------------------------------------------------------

    async def bootstrap(self):
        """
        Create the first admin account when MCLINK_BOOTSTRAP_ADMIN is set.

        Format: "<username>:<password>". Idempotent: an existing account is
        left completely untouched (never resets a password).
        """
        spec = self.bootstrap_admin.strip()

        if not spec or ":" not in spec:
            return None

        username, _, password = spec.partition(":")
        username = username.strip()
        password = password.strip()

        if not username or not password:
            return None

        if len(username) > 32:
            username = username[:32]

        existing = await self.store.get_user(username)

        if existing:
            return None

        created = await self.store.create_user(
            username,
            hash_password(password),
            is_admin=True,
            # The bootstrap account is the ROOT: the first account created on a
            # fresh database is the one that can never be demoted, and it must
            # not be born as a plain admin and then depend on the backfill
            # migration to be promoted.
            role="root",
        )

        if created:
            await self.store.add_event(
                "bootstrap_admin", actor=username,
                detail="初始根管理员账号已创建",
            )
            return username

        return None

    # --------------------------------------------------------
    # Login
    # --------------------------------------------------------

    async def login(self, username, password, ip="", client_id=""):
        """
        Returns (ok, payload_or_error_code, detail).

        payload on success:
            {"token":..., "username":..., "is_admin":0/1}
        error codes: AUTH_BAD_CREDENTIALS / AUTH_DISABLED / AUTH_RATE_LIMIT /
                     AUTH_UNAVAILABLE / AUTH_BAD_REQUEST
        """
        username = (username or "").strip()
        password = password or ""

        if not username or not password:
            return False, "AUTH_BAD_REQUEST", "用户名或密码为空"

        if len(username) > 32:
            return False, "AUTH_BAD_REQUEST", "用户名过长"

        if len(password) > 128:
            return False, "AUTH_BAD_REQUEST", "密码过长"

        if not await self.throttle.allow(ip):
            await self.store.add_event(
                "login_throttled", actor=username, source_ip=ip,
                detail="尝试过于频繁",
            )
            return False, "AUTH_RATE_LIMIT", "尝试过于频繁，请稍后再试"

        user = await self.store.get_user(username)

        # A database outage must be reported as such, not as a wrong password.
        if user is None and not await self.store.healthy():
            return False, "AUTH_UNAVAILABLE", "服务器数据库暂时不可用"

        if user is None:
            # Always do the hashing work so a missing account and a wrong
            # password take a similar amount of time.
            verify_password(password, hash_password("dummy-password"))
            await self.throttle.record_failure(ip)
            await self.store.add_event(
                "login_failed", actor=username, source_ip=ip,
                detail="账号不存在",
            )
            return False, "AUTH_BAD_CREDENTIALS", "用户名或密码错误"

        if int(user.get("is_disabled") or 0) == 1:
            await self.store.add_event(
                "login_disabled", actor=username, source_ip=ip,
                detail="账号已被禁用",
            )
            return False, "AUTH_DISABLED", "账号已被禁用"

        ok, needs_rehash = verify_password(password, user.get("password_hash"))

        if not ok:
            await self.throttle.record_failure(ip)
            await self.store.add_event(
                "login_failed", actor=username, source_ip=ip,
                detail="密码错误",
            )
            return False, "AUTH_BAD_CREDENTIALS", "用户名或密码错误"

        if needs_rehash:
            with contextlib.suppress(Exception):
                await self.store.set_user_password(
                    username, hash_password(password)
                )

        await self.throttle.reset(ip)
        await self.store.touch_login(username, ip)

        role = self.role_of(user)

        # The monthly self-service allowance travels with the login, so the
        # client can SHOW it instead of letting the player discover the limit
        # by being refused. None means "unlimited" (root/admin).
        budget = await self.self_service_budget(username)

        token = await self.tokens.issue(
            username,
            is_admin=self.is_staff(role),
            client_id=client_id,
            role=role,
        )

        await self.store.add_event(
            "login_ok", actor=username, source_ip=ip,
            detail="登录成功",
        )

        return (
            True,
            {
                "token": token,
                "username": username,
                "is_admin": 1 if self.is_staff(role) else 0,
                "role": role,
                "self_ops_left": budget["remaining"],
                "self_ops_limit": budget["limit"],
                "self_ops_unlimited": budget["unlimited"],
                "self_ops_month": budget["month"],
            },
            "",
        )

    # --------------------------------------------------------
    # Roles
    # --------------------------------------------------------

    #: The three levels, highest first.
    ROLE_ROOT = "root"
    ROLE_ADMIN = "admin"
    ROLE_PLAYER = "player"

    @staticmethod
    def role_of(user):
        """
        The role of a user row, tolerating a database that predates `role`.

        Falls back to is_admin so an un-migrated installation keeps working
        instead of silently demoting everybody to player.
        """
        stored = str((user or {}).get("role") or "").strip().lower()

        if stored in ("root", "admin", "player"):
            return stored

        return "admin" if int((user or {}).get("is_admin") or 0) == 1 else "player"

    @staticmethod
    def is_staff(role):
        """Root and admin are staff; player is not."""
        return role in ("root", "admin")

    #: What each role may do. Enforced in one place so the panel and the API
    #: can never disagree about who is allowed to do what.
    #:
    #:   view_rooms / view_password   read-only panel features
    #:   kick                         disconnect somebody
    #:   edit_users                   create / rename / re-enable / edit
    #:   reset_password               set somebody else's password
    #:   delete_user                  remove an account
    #:   manage_root                  touch the root account (nobody)
    def can(self, actor_role, action, target_role="player"):
        """
        Whether `actor_role` may perform `action` on `target_role`.

        The root account is untouchable through the panel by anybody,
        including root itself -- that is what keeps the hierarchy meaningful,
        and it is why the SSH recovery command exists.
        """
        if actor_role == self.ROLE_ROOT:
            if target_role == self.ROLE_ROOT:
                # Root may not modify itself through the panel either. Its
                # password is changed from the shell; see reset_password.py.
                return action in ("view_rooms", "view_password", "view_users")

            # Root outranks everybody.
            return action in (
                "view_rooms", "view_password", "view_users",
                "kick", "edit_users", "reset_password", "delete_user",
            )

        if actor_role == self.ROLE_ADMIN:
            if target_role == self.ROLE_ROOT:
                # Admins cannot touch root, at all.
                return action in ("view_rooms", "view_password", "view_users")

            return action in (
                "view_rooms", "view_password", "view_users",
                "kick", "edit_users", "reset_password", "delete_user",
            )

        # Players are read-only, and cannot see room passwords.
        return action in ("view_rooms", "view_users")

    # --------------------------------------------------------
    # Authorisation for HOST / PLAYER connections
    # --------------------------------------------------------

    async def authorize(self, token):
        """
        Returns (ok, identity_or_error_code).

        identity: {"username":..., "is_admin":...}

        Reads the LIVE switch, so turning "must log in" off in the panel takes
        effect for the next connection without touching the ones already
        running -- and turning it back on never kicks an authenticated player.
        """
        if not self.accounts_required:
            # Accounts are optional: a token is still honoured if present, so
            # the panel keeps showing real names.
            if token:
                record = await self.tokens.validate(token)

                if record is not None:
                    return True, {
                        "username": record["username"],
                        "is_admin": bool(record.get("is_admin")),
                    }

            return True, {"username": "", "is_admin": False}

        if not token:
            return False, "AUTH_REQUIRED"

        record = await self.tokens.validate(token)

        if record is None:
            return False, "AUTH_INVALID_TOKEN"

        # A disabled account must lose access immediately.
        user = await self.store.get_user(record["username"])

        if user is not None and int(user.get("is_disabled") or 0) == 1:
            await self.tokens.revoke(token)
            return False, "AUTH_DISABLED"

        return True, {
            "username": record["username"],
            "is_admin": bool(record.get("is_admin")),
            "role": record.get("role")
            or ("admin" if record.get("is_admin") else "player"),
        }

    # --------------------------------------------------------
    # Account management (used by the admin API)
    # --------------------------------------------------------

    async def create_user(self, username, password, role="player"):
        """
        Create an account with an EXPLICIT role.

        This used to take `is_admin` and insert only that flag, leaving the
        `role` column at its 'player' default. The panel then showed the new
        administrator as 普通玩家 -- because role_of() trusts the role column
        -- while is_admin said otherwise. Two sources of truth, one of them
        silently wrong. The role is now the only thing passed in, and
        `is_admin` is derived from it for the legacy column.
        """
        username = (username or "").strip()

        role = str(role or "").strip().lower()

        if role == self.ROLE_ROOT:
            # There is exactly one root account and it already exists.
            return False, "不能创建根管理员账号"

        if role not in (self.ROLE_ADMIN, self.ROLE_PLAYER):
            return False, "只能创建「管理员」或「玩家」账号"

        if not username or not password:
            return False, "用户名和密码不能为空"

        if len(username) > 32:
            return False, "用户名最长 32 个字符"

        if any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in username):
            return False, "用户名不能包含空格或特殊字符"

        problem = password_problem(password)

        if problem:
            return False, problem

        created = await self.store.create_user(
            username,
            hash_password(password),
            is_admin=(role == self.ROLE_ADMIN),
            role=role,
        )

        if not created:
            return False, "用户名已存在"

        await self.store.add_event(
            "user_created", actor=username,
            detail="管理员" if role == self.ROLE_ADMIN else "普通用户",
        )

        return True, ""

    async def set_role(self, username, role, actor_role=""):
        """
        Promote or demote an EXISTING account.

        Rules, same as everywhere else in this file:

          * root is untouchable -- not by admins, and not by root either. There
            is exactly one, it is unique in the database, and a stray click
            that demoted it would lock everybody out of the panel.
          * only root may change anybody's role (the panel enforces this, and
            this is the second lock).
          * root cannot be ASSIGNED: it is born with the bootstrap account.

        Demoting is a security event, so the account's tokens are revoked: the
        role is baked into the token when it is issued, and leaving the old
        token alive would let a demoted admin keep admin rights until it
        expired.
        """
        username = (username or "").strip()
        role = str(role or "").strip().lower()

        if role not in (self.ROLE_ADMIN, self.ROLE_PLAYER):
            return False, "角色只能是「管理员」或「玩家」"

        if actor_role and actor_role != self.ROLE_ROOT:
            return False, "只有根管理员能修改别人的权限"

        me = await self.store.get_user(username)

        if me is None:
            return False, "账号不存在"

        current = self.role_of(me)

        if current == self.ROLE_ROOT:
            return False, "根管理员账号不能改权限（一个服务器只有一个）"

        if current == role:
            return False, f"{username} 已经是{self.role_label(role)}了"

        ok = await self.store.set_user_role(
            username, role, is_admin=(role == self.ROLE_ADMIN)
        )

        if not ok:
            return False, "修改失败（数据库没有更新）"

        if role == self.ROLE_PLAYER:
            # A demotion must take effect NOW, not when the token expires.
            with contextlib.suppress(Exception):
                await self.tokens.revoke_user(username)

        await self.store.add_event(
            "role_changed",
            actor=username,
            detail=f"{self.role_label(current)} -> {self.role_label(role)}",
        )

        return True, (
            f"{username} 已{'提升为' if role == self.ROLE_ADMIN else '降级为'}"
            f"{self.role_label(role)}"
            + ("（该账号的登录已失效，需要重新登录）" if role == self.ROLE_PLAYER
               else "")
        )

    @classmethod
    def role_label(cls, role):
        return {
            cls.ROLE_ROOT: "根管理员",
            cls.ROLE_ADMIN: "管理员",
            cls.ROLE_PLAYER: "玩家",
        }.get(role, role)

    async def set_password(self, username, password, self_service=False):
        """
        Set an account's password.

        self_service=True is the "改我自己的密码" path: it is charged against
        the monthly allowance (players only) and, on success, invalidates every
        other device's login. The admin path is unlimited and also revokes the
        tokens (a password reset must always log the account out everywhere).
        """
        username = (username or "").strip()

        problem = password_problem(password)

        if problem:
            return False, problem

        if self_service:
            me = await self.store.get_user(username)

            if me is None:
                return False, "账号不存在"

            allowed, reason, _budget = await self._self_service_allowed(
                me, "改密码"
            )

            if not allowed:
                return False, reason

        ok = await self.store.set_user_password(
            username, hash_password(password)
        )

        if not ok:
            return False, "账号不存在"

        if self_service:
            await self._bump_self_service(username)

        await self.tokens.revoke_user(username)

        await self.store.add_event(
            "password_changed", actor=username, detail="密码已重置"
        )

        return True, ""

    async def set_disabled(self, username, disabled):
        ok = await self.store.set_user_disabled(username, disabled)

        if not ok:
            return False, "账号不存在"

        if disabled:
            await self.tokens.revoke_user(username)

        await self.store.add_event(
            "user_disabled" if disabled else "user_enabled",
            actor=username,
            detail="禁用" if disabled else "启用",
        )

        return True, ""

    async def delete_user(self, username):
        """
        Permanently remove an account.

        The caller (admin API) revokes the tokens first; this only touches the
        database row. Historical room_sessions rows are intentionally kept --
        they record what happened and are shown in the panel.
        """
        username = (username or "").strip()

        if not username:
            return False, "用户名不能为空"

        # Belt and braces: do not let the very last admin disappear, otherwise
        # nobody can manage the panel any more.
        users = await self.store.list_users(500)
        admins = [
            u for u in users
            if int(u.get("is_admin") or 0) == 1
            and int(u.get("is_disabled") or 0) == 0
        ]

        target = next(
            (u for u in users if u.get("username") == username), None
        )

        if target is None:
            return False, "账号不存在"

        if int(target.get("is_admin") or 0) == 1 and len(admins) <= 1:
            return False, "这是最后一个可用管理员，不能删除"

        deleted = await self.store.delete_user(username)

        if not deleted:
            return False, "删除失败（账号可能已不存在）"

        await self.store.add_event(
            "user_deleted", actor=username, detail="账号已删除"
        )

        return True, ""

    # --------------------------------------------------------
    # Rename (unified identity: login name == in-game display name)
    # --------------------------------------------------------

    #: How many self-service identity changes an ORDINARY account gets.
    #:
    #: 改名和改密码 SHARE this budget (a person who has run out of changes has
    #: run out, whichever form they use), and it resets on the first day of each
    #: calendar month.
    #:
    #: Staff (root/admin) are NOT limited: they are the ones who fix other
    #: people's accounts, and being blocked mid-incident would be absurd. The
    #: limit exists because a self-service loop is a cheap way to hammer the
    #: authentication database -- it is not there to ration a normal user.
    SELF_LIMIT = 3

    @staticmethod
    def _month_key(when=None):
        """(year, month) of `when` (a timestamp, default now) in local time."""
        parts = time.localtime(when if when is not None else time.time())

        return (parts.tm_year, parts.tm_mon)

    @classmethod
    def _month_label(cls, when=None):
        year, month = cls._month_key(when)

        return f"{year}-{month:02d}"

    async def self_service_budget(self, username):
        """
        Remaining self-service changes for this account this month.

        `unlimited` is True for staff, and then `remaining` is None -- "no
        limit" is not a number, and a caller that prints one would be lying.
        """
        me = await self.store.get_user((username or "").strip())
        month = self._month_label()

        if me is None:
            return {
                "unlimited": False,
                "limit": self.SELF_LIMIT,
                "used": self.SELF_LIMIT,
                "remaining": 0,
                "month": month,
                "role": "player",
            }

        role = self.role_of(me)

        if self.is_staff(role):
            return {
                "unlimited": True,
                "limit": None,
                "used": 0,
                "remaining": None,
                "month": month,
                "role": role,
            }

        used = 0
        started = _as_timestamp(me.get("rename_window_start"))

        # A counter from a previous month is simply ignored: the budget comes
        # back on the 1st, which is what "每月 3 次" means to a person.
        if started is not None and self._month_key(started) == self._month_key():
            used = int(me.get("rename_count") or 0)

        return {
            "unlimited": False,
            "limit": self.SELF_LIMIT,
            "used": used,
            "remaining": max(0, self.SELF_LIMIT - used),
            "month": month,
            "role": role,
        }

    async def _self_service_allowed(self, me, what):
        """
        May this account make one more self-service change? (ok, message, info)

        Does NOT consume the allowance -- the change has to succeed first.
        """
        budget = await self.self_service_budget(me.get("username"))

        if budget["unlimited"]:
            return True, "", budget

        if budget["remaining"] <= 0:
            return (
                False,
                f"本月的自助修改次数已用完（每月 {self.SELF_LIMIT} 次，"
                f"改名和改密码共用；{budget['month']} 已用 {budget['used']} 次）。"
                "要改可以等月初，或者请管理员在管理面板里帮你改（管理员不受次数限制）。",
                budget,
            )

        return True, "", budget

    async def _bump_self_service(self, username):
        """Charge one self-service change to this account."""
        me = await self.store.get_user((username or "").strip())

        if me is None or self.is_staff(self.role_of(me)):
            return

        used = int(me.get("rename_count") or 0)
        started = _as_timestamp(me.get("rename_window_start"))
        same_month = (
            started is not None and self._month_key(started) == self._month_key()
        )

        await self.store.bump_rename(
            username,
            (used + 1) if same_month else 1,
            reset_window=not same_month,
        )

    async def rename_user(self, username, new_username, actor="",
                          by_panel=False):
        """
        Change an account's username.

        The username IS the display name shown in game and in the room list,
        so this is the single place where identity changes.

        Two very different callers:

          by_panel=True   (管理员在面板改名)
              * NO rate limit -- an admin fixing a typo must not be blocked
              * NO forced logout -- the change takes effect immediately and the
                player keeps playing. The relay resolves the name from the
                account record, so the new name simply appears in the room list
                and in the dashboard on the next refresh.

          by_panel=False  (玩家在客户端自助改名)
              * rate limited: SELF_LIMIT per calendar month, shared with
                self-service password changes. Without a limit a looping client
                could rename itself on every frame, which is a cheap way to
                hammer the authentication database and to make the audit log
                useless.
              * still does NOT disconnect the player.

        Returns (ok, message, info).
        """
        username = (username or "").strip()
        new_username = (new_username or "").strip()

        if not username:
            return False, "账号不存在", None

        if new_username == username:
            return False, "新用户名和当前用户名相同", None

        # ---- validation (same rules as account creation) ----
        if len(new_username) < 2:
            return False, "用户名至少 2 个字符", None

        if len(new_username) > 32:
            return False, "用户名最长 32 个字符", None

        if any(
            c.isspace() or ord(c) < 33 or ord(c) == 127
            for c in new_username
        ):
            return False, "用户名不能包含空格或特殊字符", None

        me = await self.store.get_user(username)

        if me is None:
            return False, "账号不存在", None

        # ---- rate limit: only for the self-service path ----
        if not by_panel:
            allowed, reason, budget = await self._self_service_allowed(me, "改名")

            if not allowed:
                return False, reason, budget

        ok, message, _info = await self.store.rename_user(
            username, new_username
        )

        if not ok:
            return False, message, None

        if not by_panel:
            # Charge the monthly allowance. Done only now, after the rename
            # actually succeeded: a rejected name must not cost the player one
            # of their three changes.
            await self._bump_self_service(new_username)

        # ---- live state: no restart, no disconnect ----
        # Update the name held by every room / session this account is using
        # right now, so the panel and the room list show the new name on the
        # next refresh instead of the old one.
        if self.on_rename is not None:
            try:
                result = self.on_rename(username, new_username)

                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass

        # The caller's own session tokens still carry the old name; move them
        # across so the rename does not look like a logout to the client.
        # NOTE: this hook is async (TokenManager.rebind_user), so it MUST be
        # awaited -- calling it without `await` silently creates a coroutine
        # that never runs and leaves every token pointing at the old name.
        if self.on_rename_tokens is not None:
            try:
                result = self.on_rename_tokens(username, new_username)

                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass

        # Bookkeeping: charge the self-service allowance. Done AFTER the rename
        # succeeded, so a rejected name never costs the player one of their
        # three changes.
        #
        # NOTE: exactly one bump, and it is the one above -- an earlier version
        # of this patch had a second copy here, which spent TWO of the three
        # monthly changes per rename.
        await self.store.add_event(
            "panel_rename_user" if by_panel else "user_renamed",
            actor=actor or ("panel" if by_panel else new_username),
            detail=f"{username} -> {new_username}",
        )

        budget = await self.self_service_budget(new_username)

        return (
            True,
            f"已改名为 {new_username}，立即生效（无需重新登录）",
            {
                "username": new_username,
                "count": budget["used"],
                "remaining": budget["remaining"],
                "limit": budget["limit"],
                "unlimited": budget["unlimited"],
                "month": budget["month"],
            },
        )

    async def rename_budget(self, username):
        """Backwards-compatible name for self_service_budget()."""
        return await self.self_service_budget(username)

    async def rename_info(self, username):
        """Current self-service budget for the panel's account page."""
        budget = await self.self_service_budget(username)
        me = await self.store.get_user((username or "").strip())

        budget["count"] = budget["used"]
        budget["renamed_at"] = (me or {}).get("renamed_at")

        return budget


# ============================================================
# Password helpers re-exported for convenience
# ============================================================

__all__ = [
    "SENTINEL",
    "TokenManager",
    "LoginThrottle",
    "AccountService",
    "hash_password",
    "verify_password",
]
