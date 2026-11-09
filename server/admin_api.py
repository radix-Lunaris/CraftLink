"""
MC-Link relay -- admin HTTP API.

This is the bridge between the relay (which owns the live TCP state) and the
PHP web panel (which renders it).

Security model
--------------
* The port is NEVER published to the host. The compose file uses `expose`, so
  it is only reachable from other containers on the same Docker network. The
  PHP panel talks to it as http://mc-relay:19101 .
* Every request must carry `Authorization: Bearer <MCLINK_ADMIN_API_TOKEN>`.
* The API only exposes JSON. It never renders HTML and never proxies the
  Minecraft port.

The relay continues to work normally when this API is disabled, when the
token is wrong, or when the caller does not exist.
"""

import asyncio
import contextlib
import hmac
import json
import time
from urllib.parse import parse_qs, unquote, urlparse

from relay_config import ADMIN_API_TOKEN

STARTED_AT = time.time()

#: 关房请求在中继这边最多等多久。
#:
#: 关房是"通知客户端 → 等它们自己退 → 超时再强断"，本身就慢（客户端那边
#: 的协同窗口见 room_members.COOPERATIVE_CLOSE_GRACE）。中继必须先于面板
#: 放弃，否则面板只能看到一个没有答案的连接 —— 面板侧的等待页用的就是
#: 这个数字再加一点余量。
REVOKE_TIMEOUT = 25


# ============================================================
# Raw (non-JSON) responses
# ============================================================

class RawResponse:
    """
    处理器用它表示「这条路由直接回原始正文，不要套 JSON 外壳」。

    目前只有 `GET /logs/export`（下载服务端日志）用它：浏览器必须收到
    `text/plain` + `Content-Disposition` 才会弹出「另存为」。若套成 JSON，
    面板还得在 PHP 里再解码一次，白白多一层出错机会。
    """

    __slots__ = ("body", "content_type", "filename", "extra_headers", "status")

    def __init__(
        self,
        body="",
        content_type="text/plain; charset=utf-8",
        filename="",
        extra_headers=None,
        status=200,
    ):
        self.body = body
        self.content_type = content_type
        self.filename = filename
        self.extra_headers = extra_headers or {}
        self.status = status


# ============================================================
# Server
# ============================================================

class AdminAPI:
    """
    `handlers` maps a name to a coroutine:

        stats()                       -> dict
        rooms()                       -> list[dict]
        delete_room(room_id)          -> (ok, message)
        kick_player(session_id)       -> (ok, message)
        kick_member(room_id, client_id) -> (ok, message)
        kick_user(username)           -> (ok, message)
        shutdown_room(room_id)        -> (ok, message)
    """

    def __init__(self, host, port, handlers, gatekeeper, store, tokens,
                 enabled=True, token=None, settings=None):
        self.host = host
        self.port = int(port)
        self.handlers = handlers or {}
        self.gatekeeper = gatekeeper
        self.store = store
        self.tokens = tokens
        self.settings = settings
        self.enabled = bool(enabled)
        self.token = token if token is not None else ADMIN_API_TOKEN
        self.server = None
        self.request_count = 0

    # --------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------

    async def start(self):
        if not self.enabled:
            return None

        if not self.token:
            # Refuse to expose an unauthenticated admin API.
            print(
                "[admin-api] MCLINK_ADMIN_API_TOKEN 未设置，管理 API 已禁用",
                flush=True,
            )
            return None

        self.server = await asyncio.start_server(
            self._on_connection, self.host, self.port
        )

        return self.server

    async def stop(self):
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None

    # --------------------------------------------------------
    # Connection handling
    # --------------------------------------------------------

    async def _on_connection(self, reader, writer):
        peer = "unknown"

        try:
            peer = writer.get_extra_info("peername")
            source_ip = peer[0] if isinstance(peer, (tuple, list)) else str(peer)

            request = await asyncio.wait_for(
                self._read_request(reader), timeout=10
            )

            if request is None:
                await self._respond(writer, 400, {"ok": False, "error": "bad_request"})
                return

            method, path, query, headers, body = request

            self.request_count += 1

            if not self._authorized(headers):
                await self._respond(
                    writer,
                    401,
                    {"ok": False, "error": "unauthorized"},
                    extra_headers={"WWW-Authenticate": "Bearer"},
                )
                return

            status, payload = await self._dispatch(
                method, path, query, body, source_ip
            )

            # 处理器可以要求"直接回正文"（日志下载），其余一律走 JSON。
            if isinstance(payload, RawResponse):
                await self._respond_raw(writer, payload)
            else:
                await self._respond(writer, status, payload)

        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                await self._respond(writer, 408, {"ok": False, "error": "timeout"})

        except Exception as e:
            with contextlib.suppress(Exception):
                await self._respond(
                    writer, 500, {"ok": False, "error": str(e)[:200]}
                )

        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _read_request(self, reader):
        """Minimal HTTP/1.1 request parser (headers + optional body)."""
        header_blob = b""

        while b"\r\n\r\n" not in header_blob:
            chunk = await reader.read(4096)

            if not chunk:
                return None

            header_blob += chunk

            if len(header_blob) > 64 * 1024:
                return None

        head, _, rest = header_blob.partition(b"\r\n\r\n")

        try:
            lines = head.decode("utf-8", errors="replace").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
        except (ValueError, IndexError):
            return None

        headers = {}

        for line in lines[1:]:
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()

        try:
            length = int(headers.get("content-length", "0") or 0)
        except ValueError:
            length = 0

        length = max(0, min(length, 256 * 1024))

        body = rest

        while len(body) < length:
            chunk = await reader.read(length - len(body))

            if not chunk:
                break

            body += chunk

        parsed = urlparse(target)

        # ============================================================================
        #  THE PERCENT-DECODING BUG THAT MADE "关闭房间" IMPOSSIBLE
        # ============================================================================
        #
        # `urlparse` does NOT decode anything: for
        #
        #     POST /rooms/%E6%88%91%E7%9A%84%E4%B8%96%E7%95%8C/revoke
        #
        # it returns the path with the escapes still in it. The panel sends
        # `rawurlencode($room)` -- correct HTTP -- so every room name with a
        # non-ASCII character (or a space, or anything else that needs escaping)
        # was looked up under its ESCAPED form:
        #
        #     rooms.get("%E6%88%91%E7%9A%84%E4%B8%96%E7%95%8C")  ->  None
        #
        # which produced 「房间不存在（可能已经关闭）」 for a room the panel was
        # displaying perfectly on the same page. The same applied to every
        # username in a URL: rename, kick, password, role changes.
        #
        # ASCII names worked (nothing to escape), which is why this survived so
        # long: every test used names like "caproom".
        path = unquote(parsed.path)

        return (
            method.upper(),
            path,
            parse_qs(parsed.query),
            headers,
            body[:length],
        )

    def _authorized(self, headers):
        header = headers.get("authorization", "")

        if not header.lower().startswith("bearer "):
            return False

        presented = header[7:].strip()

        return hmac.compare_digest(presented, self.token)

    async def _respond_raw(self, writer, raw):
        """
        直接写一段非 JSON 正文（目前只给「下载服务端日志」用）。

        和 _respond 分开是刻意的：JSON 那条路要给面板用的接口保持
        Content-Type/缓存头一致，而下载必须带
        Content-Disposition 且正文是原始文本，不能套 JSON 外壳。
        """
        reasons = {200: "OK", 400: "Bad Request", 404: "Not Found"}

        body = raw.body

        if isinstance(body, str):
            body = body.encode("utf-8", errors="replace")

        headers = [
            f"HTTP/1.1 {raw.status} {reasons.get(raw.status, 'OK')}",
            f"Content-Type: {raw.content_type}",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            "Connection: close",
        ]

        if raw.filename:
            # 浏览器据此弹出"另存为"，文件名给出去（不带路径）
            safe = raw.filename.replace('"', "").replace("\\", "").replace("/", "")
            headers.append(
                f'Content-Disposition: attachment; filename="{safe}"'
            )

        for key, value in (raw.extra_headers or {}).items():
            headers.append(f"{key}: {value}")

        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("utf-8"))
        writer.write(body)

        await writer.drain()

    async def _respond(self, writer, status, payload, extra_headers=None):
        reasons = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            405: "Method Not Allowed",
            408: "Request Timeout",
            409: "Conflict",
            500: "Internal Server Error",
        }

        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

        headers = [
            f"HTTP/1.1 {status} {reasons.get(status, 'OK')}",
            "Content-Type: application/json; charset=utf-8",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            "Connection: close",
        ]

        for key, value in (extra_headers or {}).items():
            headers.append(f"{key}: {value}")

        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("utf-8"))
        writer.write(body)

        await writer.drain()

    # --------------------------------------------------------
    # Routing
    # --------------------------------------------------------

    async def _dispatch(self, method, path, query, body, source_ip):
        path = path.rstrip("/") or "/"

        # ---- health ----
        if path == "/health":
            return 200, {
                "ok": True,
                "service": "mc-link-relay",
                "version": "2.0.0",
                "uptime": round(time.time() - STARTED_AT, 1),
                "database": "up" if await self.store.healthy() else "down",
            }

        # ---- runtime settings (panel switches) ----
        if path == "/settings":
            if method == "GET":
                return 200, {
                    "ok": True,
                    "data": self.settings.snapshot() if self.settings else {},
                }

            if method == "PUT":
                payload = _json_body(body)

                if self.settings is None:
                    return 400, {"ok": False, "error": "设置服务未启用"}

                changed = []
                failed = []

                for name, value in payload.items():
                    ok, message = await self.settings.set(
                        name, value, actor="panel"
                    )

                    if ok:
                        changed.append(name)
                    else:
                        failed.append(f"{name}: {message}")

                if failed:
                    return 400, {
                        "ok": False,
                        "error": "; ".join(failed),
                        "changed": changed,
                    }

                # Live effect: only the NEXT connection is treated
                # differently; connected players are never dropped.
                return 200, {
                    "ok": True,
                    "message": "设置已保存并立即生效（在线玩家不受影响）",
                    "changed": changed,
                    "data": self.settings.snapshot(),
                }

            return 405, {"ok": False, "error": "method_not_allowed"}

        # ---- live state ----
        if path == "/stats":
            return 200, {"ok": True, "data": await self._call("stats")}

        if path == "/rooms":
            return 200, {"ok": True, "data": await self._call("rooms")}

        if path == "/rooms/purge-stale":
            if method == "POST":
                ok, message = await self._call("purge_stale_rooms")
                await self._audit("panel_purge_stale_rooms", "", source_ip, message)
                return (200 if ok else 400), {"ok": ok, "message": message}

            return 405, {"ok": False, "error": "method_not_allowed"}

        if path == "/rooms/revoke":
            # 房间标识放在 body 里。新面板走这一条：房间 ID 可能含 %2F 之类的
            # 转义，放在 URL 路径里会被反代/nginx 归一化，放在 body 里不会。
            if method == "POST":
                payload = _json_body(body)

                room_id = str(
                    payload.get("room")
                    or payload.get("room_id")
                    or payload.get("name")
                    or ""
                ).strip()

                if not room_id:
                    return 400, {
                        "ok": False,
                        "error": "没有指定房间（body 里的 room 字段是空的）",
                    }

                reason = str(
                    payload.get("reason") or "管理员关闭了房间"
                )[:120]

                return await self._revoke_room(room_id, reason, source_ip)

            return 405, {"ok": False, "error": "method_not_allowed"}

        if path.startswith("/rooms/") and path.endswith("/revoke"):
            # 老路径写法（房间标识在路径里）。注意这一条必须排在
            # "/rooms/revoke" 之后：那个字符串同样以 "/rooms/" 开头、
            # 以 "/revoke" 结尾，先匹配它的话房间标识会解析成空串。
            room_id = path[len("/rooms/"):-len("/revoke")]

            if not room_id.strip():
                return 400, {
                    "ok": False,
                    "error": "没有指定房间（路径里的房间标识是空的）",
                }

            if method == "POST":
                payload = _json_body(body)
                reason = str(
                    payload.get("reason") or "管理员关闭了房间"
                )[:120]

                return await self._revoke_room(room_id, reason, source_ip)

            return 405, {"ok": False, "error": "method_not_allowed"}


        if path.startswith("/rooms/") and path.endswith("/kick"):
            # 踢出房间里的一个成员（房间本身继续存在，除非被踢的是所有者）。
            #
            # 路由必须排在下面的通用 "/rooms/" 之前：那个前缀同样匹配
            # "/rooms/<room>/kick"，会把 "<room>/kick" 整个当成房间标识。
            #
            # body 是 JSON `{"client_id": "<id>"}`。房间标识留在路径里，和
            # 面板那边 rawurlencode() 之后的写法对齐；kick 是即时动作，
            # 不需要 revoke 那条"房间标识放 body 里"的特殊照顾。
            room_id = path[len("/rooms/"):-len("/kick")]

            if not room_id.strip():
                return 400, {
                    "ok": False,
                    "error": "没有指定房间（路径里的房间标识是空的）",
                }

            if method == "POST":
                payload = _json_body(body)
                client_id = str(payload.get("client_id") or "").strip()

                if not client_id:
                    return 400, {
                        "ok": False,
                        "error": "没有指定要踢出的成员（body 里的 client_id 是空的）",
                    }

                ok, message = await self._call(
                    "kick_member", room_id, client_id
                )

                await self._audit(
                    "panel_kick_member",
                    room_id,
                    source_ip,
                    f"{client_id}：{message}",
                )

                return (200 if ok else 404), {
                    "ok": ok,
                    "message": message,
                }

            return 405, {"ok": False, "error": "method_not_allowed"}

        if path.startswith("/rooms/"):
            room_id = path[len("/rooms/"):]

            if method == "DELETE":
                ok, message = await self._call("delete_room", room_id)
                await self._audit("panel_delete_room", room_id, source_ip, message)
                return (200 if ok else 404), {
                    "ok": ok,
                    "message": message,
                }

            return 405, {"ok": False, "error": "method_not_allowed"}

        if path.startswith("/sessions/"):
            session_id = path[len("/sessions/"):]

            if method == "DELETE":
                ok, message = await self._call("kick_player", session_id)
                await self._audit("panel_kick_player", "", source_ip, session_id)
                return (200 if ok else 404), {"ok": ok, "message": message}

            return 405, {"ok": False, "error": "method_not_allowed"}

        if path.startswith("/users/") and path.endswith("/self-service"):
            username = path[len("/users/"):-len("/self-service")]

            if method == "GET":
                budget = await self.gatekeeper.self_service_budget(username)

                return 200, {"ok": True, "data": budget, "message": (
                    "该账号不受次数限制（管理员及以上）"
                    if budget.get("unlimited")
                    else f"本月还可以自助修改 {budget.get('remaining')} 次"
                )}

            return 405, {"ok": False, "error": "method_not_allowed"}

        if path.startswith("/users/") and path.endswith("/rename"):
            username = path[len("/users/"):-len("/rename")]

            if method == "POST":
                payload = _json_body(body)

                # self_service=True means "I am changing MY OWN account from
                # the panel", which is charged against the monthly allowance
                # (players only, 3/month, shared with password changes). An
                # admin rename is by_panel=True: unlimited, no forced logout.
                self_service = bool(payload.get("self_service"))

                ok, message, info = await self.gatekeeper.rename_user(
                    username,
                    payload.get("username", ""),
                    actor=str(payload.get("actor") or "panel"),
                    by_panel=not self_service,
                )

                await self._audit(
                    "panel_self_rename" if self_service else "panel_rename_user",
                    "", source_ip,
                    f"{username} -> {payload.get('username', '')}",
                )

                return (200 if ok else 400), {
                    "ok": ok,
                    "message": message,
                    "data": info,
                }

            return 405, {"ok": False, "error": "method_not_allowed"}

        if path.startswith("/users/") and path.endswith("/kick"):
            username = path[len("/users/"):-len("/kick")]

            if method == "POST":
                ok, message = await self._call("kick_user", username)
                await self._audit("panel_kick_user", "", source_ip, username)
                return (200 if ok else 404), {"ok": ok, "message": message}

            return 405, {"ok": False, "error": "method_not_allowed"}

        # ---- history ----
        if path == "/sessions":
            room_id = (query.get("room") or [""])[0]
            limit = _int_arg(query, "limit", 100, 1, 500)
            rows = await self.store.list_sessions(room_id or None, limit)
            return 200, {"ok": True, "data": rows}

        if path == "/logs/tail":
            if method != "GET":
                return 405, {"ok": False, "error": "method_not_allowed"}

            return 200, {"ok": True, "data": await self._call("logs_tail", query)}

        # 下载服务端日志：回的是**原始文本**，不套 JSON 外壳。
        #
        # 这条必须放在最后那串 "/users/" 之类的通用前缀路由之前（这里都是
        # 精确路径，不受影响），而且返回 RawResponse —— 面板那边收到
        # Content-Disposition 才会让浏览器弹「另存为」。
        if path == "/logs/export":
            if method != "GET":
                return 405, {"ok": False, "error": "method_not_allowed"}

            result = await self._call("logs_export", query)

            if isinstance(result, RawResponse):
                return 200, result

            # 处理器没登记（旧版中继）或抛异常：给一条能读的 JSON 错误，
            # 面板会把它显示成"中继暂不支持日志导出（需要更新服务端）"。
            if isinstance(result, tuple) and len(result) == 2:
                return 500, {"ok": False, "error": str(result[1])[:200]}

            return 500, {"ok": False, "error": "日志导出不可用"}

        if path == "/logs/clear":
            # 清空**服务端日志**（relay.log：内存缓冲 + 文件 + 轮转备份）。
            #
            # 和 /events/clear 是两件事，别混：那个清的是数据库里的审计事件，
            # 这个清的是中继自己的日志文件。用户在面板上点「清空日志」时看到
            # 的那个列表来自 /logs/tail，所以只清数据库是不够的——那正是
            # "删了但列表还在"的来源。
            if method != "POST":
                return 405, {"ok": False, "error": "method_not_allowed"}

            result = await self._call("logs_clear", {})

            if not isinstance(result, dict) or not result.get("ok"):
                # 旧版中继没有这个 handler：明确回报，面板才能说"需要更新服务端"，
                # 而不是显示成"已清空"却没有。
                return 501, {
                    "ok": False,
                    "error": "relay_has_no_logs_clear",
                    "detail": str(result)[:200],
                }

            return 200, result

        if path == "/events":
            limit = _int_arg(query, "limit", 100, 1, 500)
            rows = await self.store.list_events(limit)
            return 200, {"ok": True, "data": rows}

        if path == "/events/clear":
            if method == "POST":
                try:
                    removed = await self.store.clear_events()
                except Exception as e:  # noqa: BLE001
                    return 500, {"ok": False, "error": f"清空日志失败：{e}"}

                await self._audit(
                    "panel_clear_events", "", source_ip,
                    f"清空审计日志（{removed} 条）",
                )

                return 200, {
                    "ok": True,
                    "message": f"已清空审计日志（{removed} 条）",
                }

            return 405, {"ok": False, "error": "method_not_allowed"}

        # ---- account management ----
        if path == "/users":
            if method == "GET":
                rows = await self.store.list_users(
                    _int_arg(query, "limit", 200, 1, 500)
                )
                return 200, {"ok": True, "data": rows}

            if method == "POST":
                payload = _json_body(body)

                # The ROLE is the authority; `is_admin` only decides the legacy
                # flag. Passing just is_admin is what produced administrators
                # that the panel displayed as 普通玩家.
                requested_role = str(payload.get("role") or "").strip().lower()

                if not requested_role:
                    requested_role = (
                        "admin" if payload.get("is_admin") else "player"
                    )

                if requested_role == "root":
                    return 400, {
                        "ok": False,
                        "message": "不能创建根管理员账号（一个服务器只有一个）",
                    }

                # Only the ROOT account may create administrators. The panel
                # enforces this too; this is the second lock, because the panel
                # is not the only thing that can reach this API.
                actor = str(payload.get("actor") or "")
                actor_role = await self._actor_role(actor)

                if requested_role == "admin" and actor_role != "root":
                    return 403, {
                        "ok": False,
                        "message": (
                            "只有根管理员能创建管理员账号"
                            if actor_role
                            else "需要根管理员身份才能创建管理员账号"
                        ),
                    }

                ok, message = await self.gatekeeper.create_user(
                    payload.get("username", ""),
                    payload.get("password", ""),
                    role=requested_role,
                )

                await self._audit(
                    "panel_create_user", "", source_ip,
                    f"{payload.get('username', '')} ({requested_role})"
                    + (f" by {actor}" if actor else ""),
                )

                return (200 if ok else 400), {"ok": ok, "message": message}

            return 405, {"ok": False, "error": "method_not_allowed"}

        if path.startswith("/users/"):
            username = path[len("/users/"):]
            payload = _json_body(body) if body else {}

            if method == "PUT":
                payload = _json_body(body)

                ok, message = await self.gatekeeper.set_password(
                    username,
                    payload.get("password", ""),
                    self_service=bool(payload.get("self_service")),
                )
                await self._audit("panel_set_password", "", source_ip, username)
                return (200 if ok else 400), {"ok": ok, "message": message}

            if method == "PATCH":
                # Two independent things can be patched: the disabled flag and
                # the role. The role is a root-only action (see set_role).
                if "role" in payload:
                    actor = str(payload.get("actor") or "")
                    actor_role = await self._actor_role(actor)

                    ok, message = await self.gatekeeper.set_role(
                        username,
                        payload.get("role"),
                        actor_role=actor_role,
                    )

                    await self._audit(
                        "panel_set_role", "", source_ip,
                        f"{username} -> {payload.get('role')}"
                        + (f" by {actor}" if actor else ""),
                    )

                    return (200 if ok else 400), {"ok": ok, "message": message}

                disabled = bool(payload.get("disabled"))
                ok, message = await self.gatekeeper.set_disabled(username, disabled)
                await self._audit(
                    "panel_disable_user" if disabled else "panel_enable_user",
                    "", source_ip, username,
                )
                return (200 if ok else 404), {"ok": ok, "message": message}

            if method == "DELETE":
                # Revoke the tokens first so a live session cannot keep
                # running with an account that no longer exists.
                with contextlib.suppress(Exception):
                    await self.tokens.revoke_user(username)

                ok, message = await self.gatekeeper.delete_user(username)

                await self._audit("panel_delete_user", "", source_ip, username)

                return (200 if ok else 404), {"ok": ok, "message": message}

            return 405, {"ok": False, "error": "method_not_allowed"}

        return 404, {"ok": False, "error": "not_found"}

    async def _actor_role(self, username):
        """
        The role of the panel user who made this request.

        The panel authenticates its own users and then talks to the relay with
        ONE shared token, so the relay cannot tell root from admin by the
        request alone -- the panel says who is acting. That claim is not taken
        on faith for anything privileged: the account is looked up here, and an
        unknown or empty actor gets no special rights.
        """
        username = (username or "").strip()

        if not username:
            return ""

        try:
            user = await self.store.get_user(username)
        except Exception:
            return ""

        if user is None:
            return ""

        return self.gatekeeper.role_of(user)

    async def _call(self, name, *args):
        handler = self.handlers.get(name)

        if handler is None:
            return (False, f"此功能未启用：{name}") if name != "stats" else {}

        try:
            result = handler(*args)

            if asyncio.iscoroutine(result):
                result = await result

            return result

        except Exception as e:
            return (False, f"服务端错误：{e}")

    async def _audit(self, action, room_id, source_ip, detail=""):
        with contextlib.suppress(Exception):
            await self.store.add_event(
                action,
                actor="panel",
                room_id=room_id,
                detail=detail,
                source_ip=source_ip,
            )

    async def _revoke_room(self, room_id, reason, source_ip):
        """
        面板关房（无条件销毁）。两条路由（路径式 / body 式）共用这一份实现。

        Unconditional destroy. See room_members.revoke_room for why this
        deliberately has no "is it empty?" check: this is the admin path, and it
        exists precisely for the cases where the room will not go away on its
        own.

        `room_id` 可以是房间 ID（room token）或裸房间名 —— 中继两头都认。
        """
        try:
            from room_members import revoke_room_by_name

            report = await asyncio.wait_for(
                # The relay tells the clients and waits for them to leave on
                # their own before forcing anything, so this call is slower than
                # the others by design. The panel's HTTP timeout is raised to
                # match.
                revoke_room_by_name(room_id, reason),
                timeout=REVOKE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            # Never leave the panel spinning: the admin gets a clear answer and
            # can look at the logs.
            await self._audit(
                "panel_revoke_room_timeout", room_id, source_ip, "超时",
            )
            return 504, {
                "ok": False,
                "error": (
                    f"关闭房间超时（{REVOKE_TIMEOUT} 秒内中继没有确认；"
                    "房间可能已卡住，请查看中继日志）"
                ),
            }
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "error": f"关闭房间失败：{e}"}

        shown = str(report.get("name") or room_id)

        await self._audit(
            "panel_revoke_room",
            room_id,
            source_ip,
            f"{report.get('members', 0)} 名成员 / "
            f"{report.get('sessions', 0)} 个会话，原因：{reason}",
        )

        if not report.get("ok"):
            # Not in the relay's memory. It may still be a leftover ROW
            # ("ghost room"): the panel lists it, the admin clicks 强制关闭, and
            # a plain revoke would answer 房间不存在 and change nothing. Purging
            # the row IS the fix for that room, so do it here rather than hoping
            # the panel falls back.
            purged = False

            with contextlib.suppress(Exception):
                purged = bool(await self.store.delete_room(room_id))

            if purged:
                await self._audit(
                    "panel_revoke_stale_room", room_id, source_ip, reason,
                )

                return 200, {
                    "ok": True,
                    "message": (
                        f"房间「{shown}」只是数据库里的残留记录，"
                        "已清理（中继内存里本来就没有这个房间）"
                    ),
                }

            return 404, {
                "ok": False,
                "error": report.get("reason", "房间不存在"),
                "data": report,
            }

        return 200, {
            "ok": True,
            "message": (
                f"房间「{shown}」已关闭"
                f"（断开 {report.get('sessions', 0)} 个游戏会话；"
                f"{report.get('left_voluntarily', 0)} 名成员收到通知后自行退出"
                + (
                    f"，{report.get('forced', 0)} 名超时被强制断开"
                    if report.get("forced")
                    else ""
                )
                + "）"
            ),
            "data": report,
        }


def _int_arg(query, name, default, minimum, maximum):
    try:
        value = int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default

    return max(minimum, min(maximum, value))


def _json_body(body):
    if not body:
        return {}

    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return {}

    return data if isinstance(data, dict) else {}
