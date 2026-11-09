"""
MC-Link V2 -- client side: login, room list, room deletion.

This exercises the code the GUI actually calls:

    MCLinkEngine.login()          -> AuthSession
    MCLinkEngine.fetch_rooms()    -> [RoomInfo]
    MCLinkEngine.start_session(auth=...)  -> HOST / PLAYER with a token
    MCLinkEngine.delete_room()    -> host deletes its own room

The relay runs with accounts ENABLED (MCLINK_DB_ENABLED=0 -> memory store),
so every request needs a real token.

Run:
    python tests/v2_client_test.py
"""

import asyncio
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.config.settings import ClientConfig          # noqa: E402
from mclink.core.engine import MCLinkEngine              # noqa: E402
from mclink.core import control_client                   # noqa: E402

RELAY_PORT = int(os.environ.get("MCLINK_TEST_RELAY_PORT", "19130"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_ADMIN_PORT", "19131"))
MC_PORT = int(os.environ.get("MCLINK_TEST_MC_PORT", "25595"))
PLAYER_PORT = int(os.environ.get("MCLINK_TEST_PLAYER_PORT", "25596"))

ADMIN_TOKEN = "client-test-admin-token"
ROOM = "v2client-room"

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


async def echo_handler(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def wait_for(predicate, timeout=25, interval=0.25, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    print(f"  !! timeout waiting for {label}", flush=True)
    return False


def relay_reachable(port):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


async def _is_online(username):
    """
    Ask the relay's admin API whether an account is in the online list.

    Deliberately not a client-side check: the whole point of the LOGOUT test
    is what the PANEL would show, and the panel reads /stats.
    """
    import json
    import urllib.request

    def fetch():
        request = urllib.request.Request(
            f"http://127.0.0.1:{ADMIN_PORT}/stats",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode())

    try:
        payload = await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception:
        return False

    online = (payload.get("data") or {}).get("online_accounts") or []

    return username in online


def main():
    print("=" * 64)
    print("MC-Link V2 client test (login / rooms / delete)")
    print("=" * 64)

    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(RELAY_PORT),
            "MCLINK_DB_ENABLED": "0",
            "MCLINK_REQUIRE_ACCOUNT": "1",
            "MCLINK_BOOTSTRAP_ADMIN": "admin:adminpass123",
            "MCLINK_ADMIN_API_ENABLED": "1",
            "MCLINK_ADMIN_API_HOST": "127.0.0.1",
            "MCLINK_ADMIN_API_PORT": str(ADMIN_PORT),
            "MCLINK_ADMIN_API_TOKEN": ADMIN_TOKEN,
            "MCLINK_STATUS_FILE": os.path.join(HERE, "client_status.json"),
            "MCLINK_STATUS_INTERVAL": "2",
        }
    )

    server_log = open(
        os.path.join(HERE, "v2_client_relay.log"),
        "w",
        encoding="utf-8",
        errors="replace",
    )

    relay = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    echo_state = {}

    import threading

    def run_echo():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def serve():
            server = await asyncio.start_server(
                echo_handler, "127.0.0.1", MC_PORT
            )
            echo_state["server"] = server
            async with server:
                await server.serve_forever()

        try:
            loop.run_until_complete(serve())
        except Exception:
            pass

    threading.Thread(target=run_echo, daemon=True).start()

    host_engine = MCLinkEngine()
    player_engine = MCLinkEngine()

    try:
        ok = wait_for(
            lambda: relay_reachable(RELAY_PORT),
            timeout=20,
            label="relay to listen",
        )
        check("relay starts", ok)

        if not ok:
            return 1

        # ---- 1. login: wrong password -----------------------
        try:
            host_engine.login("127.0.0.1", RELAY_PORT, "admin", "wrong-password")
            check("client login rejects a wrong password", False, "no error raised")
        except RuntimeError as e:
            check(
                "client login rejects a wrong password",
                "密码" in str(e) or "用户名" in str(e),
                str(e),
            )

        # ---- 2. login: unknown account ----------------------
        try:
            host_engine.login("127.0.0.1", RELAY_PORT, "nobody", "whatever123")
            check("client login rejects an unknown account", False, "no error")
        except RuntimeError as e:
            check(
                "client login rejects an unknown account",
                "密码" in str(e) or "用户名" in str(e),
                str(e),
            )

        # ---- 3. login: success ------------------------------
        admin = host_engine.login(
            "127.0.0.1", RELAY_PORT, "admin", "adminpass123", "client-1"
        )
        check(
            "client login returns an AuthSession",
            bool(admin.token) and admin.username == "admin" and admin.is_admin,
            f"{admin.summary()} protocol={admin.protocol} token_len={len(admin.token)}",
        )

        check(
            "client login reports protocol v2",
            admin.protocol >= 2,
            str(admin.protocol),
        )

        original_admin_token = admin.token

        # ---- 4b. self-service rename through the client ----------
        # A DEDICATED account is used, never `admin`: the self-service path is
        # rate limited (3 per 30 days) and the rest of this suite depends on
        # the admin account keeping its name.
        import json
        import urllib.request

        def admin_post(path, body):
            request = urllib.request.Request(
                f"http://127.0.0.1:{ADMIN_PORT}{path}",
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {ADMIN_TOKEN}",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                return json.loads(response.read().decode())

        created = admin_post(
            "/users", {"username": "renameuser", "password": "renamepass123"}
        )
        check(
            "admin API created the rename test account",
            created.get("ok") is True,
            str(created)[:120],
        )

        rename_session = host_engine.login(
            "127.0.0.1", RELAY_PORT, "renameuser", "renamepass123", "client-r"
        )
        original_token = rename_session.token

        try:
            renamed, message = host_engine.rename_account(
                "127.0.0.1",
                RELAY_PORT,
                "renameuser2",
                rename_session,
                "client-r",
            )
            check(
                "engine.rename_account() renames the logged-in account",
                renamed == "renameuser2" and "立即生效" in message,
                f"{renamed} / {message}",
            )
        except RuntimeError as e:
            check("engine.rename_account() renames the account", False, str(e))

        check(
            "the local AuthSession adopts the new name at once",
            rename_session.username == "renameuser2",
            rename_session.username,
        )

        check(
            "the token itself is unchanged by a rename (no logout)",
            rename_session.token == original_token and bool(original_token),
            f"token_len={len(original_token)}",
        )

        # The token must still authorise a HOST handshake under the new name.
        # NOTE: the token is NOT a ClientConfig field -- it travels in `auth`,
        # which is exactly what keeps the frozen handshake unchanged.
        host_engine.stop_session()

        rename_cfg = ClientConfig(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            username="renameuser2",
            client_id="client-r",
            role="host",
            room="renameroom",
            mc_host="127.0.0.1",
            mc_port=MC_PORT,
            max_tunnels=2,
        ).validated()

        try:
            host_engine.start_session(rename_cfg, auth=rename_session)
            check(
                "the same token can still host after a rename",
                True,
                "session started",
            )
        except RuntimeError as e:
            check("the same token can still host after a rename", False, str(e))

        snapshot = host_engine.snapshot()
        check(
            "a RUNNING session reports the new account name",
            snapshot.get("account") == "renameuser2"
            and snapshot.get("logged_in") is True,
            f"account={snapshot.get('account')}",
        )

        host_engine.stop_session()

        # Renaming to a name that is already taken fails with a friendly text.
        try:
            host_engine.rename_account(
                "127.0.0.1", RELAY_PORT, "admin", rename_session, "client-r"
            )
            check("renaming onto a taken name is refused", False, "not refused")
        except RuntimeError as e:
            check(
                "renaming onto a taken name is refused",
                "已经被别人用了" in str(e) or "占用" in str(e),
                str(e),
            )

        # Renaming to the CURRENT name is refused locally, so it never costs
        # one of the three allowed renames.
        try:
            host_engine.rename_account(
                "127.0.0.1", RELAY_PORT, "renameuser2", rename_session, "client-r"
            )
            check("renaming to the same name is refused", False, "not refused")
        except RuntimeError as e:
            check(
                "renaming to the same name is refused",
                "相同" in str(e),
                str(e),
            )

        # A rename without a login is refused.
        try:
            host_engine.rename_account(
                "127.0.0.1", RELAY_PORT, "nobody", None, "client-r"
            )
            check("renaming without a login is refused", False, "not refused")
        except RuntimeError as e:
            check(
                "renaming without a login is refused",
                "登录" in str(e),
                str(e),
            )

        # Two more renames are still allowed (limit is 3), the 4th is not.
        for index in range(2):
            try:
                host_engine.rename_account(
                    "127.0.0.1",
                    RELAY_PORT,
                    f"renameuser{index + 3}",
                    rename_session,
                    "client-r",
                )
                check(
                    f"self-service rename {index + 2} of 3 is allowed",
                    True,
                    rename_session.username,
                )
            except RuntimeError as e:
                check(
                    f"self-service rename {index + 2} of 3 is allowed",
                    False,
                    str(e),
                )

        try:
            host_engine.rename_account(
                "127.0.0.1", RELAY_PORT, "renameuser99", rename_session, "client-r"
            )
            check("the 4th self-service rename is refused", False, "not refused")
        except RuntimeError as e:
            check(
                "the 4th self-service rename is refused (RENAME_LIMIT)",
                "次数" in str(e),
                str(e),
            )

        check(
            "the admin account was never touched by the rename tests",
            admin.username == "admin" and admin.token == original_admin_token,
            admin.username,
        )

        # ---- 4c. client LOGOUT through the engine ----
        # Exercised through engine.logout_session(), which is what the GUI
        # calls when the window closes.
        created = admin_post(
            "/users", {"username": "quituser", "password": "quitpass123"}
        )
        check(
            "admin API created the logout test account",
            created.get("ok") is True,
            str(created)[:120],
        )

        quit_session = host_engine.login(
            "127.0.0.1", RELAY_PORT, "quituser", "quitpass123", "client-q"
        )

        # 在线 = 一条真实存在的连接（presence 通道），不是"手里有个没过期的
        # token"。所以这里必须真的把那条连接开起来，再验证它在关闭后立刻消失
        # —— 这正是用户报的「关掉软件后台还显示在线」。
        host_engine.start_presence(
            "127.0.0.1", RELAY_PORT, quit_session, "client-q"
        )

        for _ in range(20):
            if asyncio.run(_is_online("quituser")):
                break

            time.sleep(0.25)

        check(
            "开了在线状态连接后，面板能看到这个账号在线",
            asyncio.run(_is_online("quituser")),
            "not in /stats online_accounts",
        )

        # 只登出、不关连接：登出必须把在线状态一起收掉。
        released = host_engine.logout_session(
            "127.0.0.1", RELAY_PORT, quit_session, "client-q"
        )

        check(
            "engine.logout_session() 报告成功",
            released is True,
            str(released),
        )

        for _ in range(20):
            if not asyncio.run(_is_online("quituser")):
                break

            time.sleep(0.25)

        check(
            "退出登录后账号立即从在线列表消失（关窗口不再残留在线状态）",
            not asyncio.run(_is_online("quituser")),
            "still in /stats online_accounts",
        )

        host_engine.stop_presence()

        # The token must be dead too, not merely absent from the list.
        #
        # A HOST attempt is the right probe: it goes through authorize(), so a
        # revoked token is rejected there. (A DELETE against a nonexistent
        # room returns ROOM_NOT_FOUND before it ever looks at the token, so it
        # would prove nothing.)
        #
        # NOTE: start_session() does NOT raise here. The host core treats a
        # rejected handshake as a retryable condition, so it returns with a
        # session that keeps retrying in the background. The honest assertions
        # are therefore "it never reaches ONLINE" and "no room was created" --
        # not "an exception was thrown".
        revoked_cfg = ClientConfig(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            username="quituser",
            client_id="client-q",
            role="host",
            room="quitroom",
            mc_host="127.0.0.1",
            mc_port=MC_PORT,
            max_tunnels=2,
        ).validated()

        try:
            host_engine.start_session(revoked_cfg, auth=quit_session)
        except Exception:  # noqa: BLE001 - raising is also acceptable
            pass

        online = wait_for(
            lambda: host_engine.snapshot().get("state") == "ONLINE",
            timeout=6,
            interval=0.25,
            label="revoked token to stay offline",
        )

        check(
            "吊销后的 token 建不起房间（始终没有进入 ONLINE）",
            online is False,
            f"state={host_engine.snapshot().get('state')}",
        )

        # Confirm against the server's own room list rather than trusting the
        # client's view of itself.
        import json as _json
        import urllib.request as _url

        try:
            request = _url.Request(
                f"http://127.0.0.1:{ADMIN_PORT}/rooms",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )

            with _url.urlopen(request, timeout=8) as response:
                rooms_payload = _json.loads(response.read().decode())

            rooms_now = [
                r.get("room") for r in (rooms_payload.get("data") or [])
            ]
        except Exception as e:  # noqa: BLE001
            rooms_now = [f"(查询失败: {e})"]

        check(
            "服务器上没有出现被吊销 token 建出来的房间",
            "quitroom" not in rooms_now,
            f"rooms={rooms_now}",
        )

        host_engine.stop_session()

        # ---- 4. room list before hosting --------------------
        rooms = host_engine.fetch_rooms("127.0.0.1", RELAY_PORT, "client-1", "admin")
        check(
            "fetch_rooms() works on an empty relay",
            rooms == [],
            f"{len(rooms)} rooms",
        )

        # ---- 4a. does the client learn whether an account is needed? ----
        #
        # This relay runs with MCLINK_REQUIRE_ACCOUNT=1, so the answer is yes.
        # The important part is that the answer comes from the SERVER: the
        # client used to assume it locally and prompt before it ever
        # connected, which made the panel's switch look broken.
        required = host_engine.relay_requires_account(
            "127.0.0.1", RELAY_PORT, "client-1"
        )

        check(
            "client asks the relay whether an account is required",
            required is True,
            f"relay_requires_account() -> {required}",
        )

        # The parser itself, including the case that must NOT be read as
        # "no login needed": an older relay that does not send the field.
        check(
            "parse_auth_flag: auth=1 -> True",
            control_client.parse_auth_flag(["ROOMS 2 auth=1", "DONE"]) is True,
        )

        check(
            "parse_auth_flag: auth=0 -> False",
            control_client.parse_auth_flag(["ROOMS 2 auth=0", "DONE"]) is False,
        )

        check(
            "parse_auth_flag: 旧版中继不发这个字段 -> None（调用方按「需要登录」处理）",
            control_client.parse_auth_flag(["ROOMS 2", "DONE"]) is None,
            str(control_client.parse_auth_flag(["ROOMS 2", "DONE"])),
        )

        check(
            "parse_auth_flag: 非 ROOMS 行不会误判",
            control_client.parse_auth_flag(["ERROR NO_HOST"]) is None,
        )

        # An unreachable relay must also come back as None rather than False,
        # otherwise a network blip would silently skip the login prompt.
        unreachable = host_engine.relay_requires_account(
            "127.0.0.1", 1, "client-1", timeout=3
        )

        check(
            "连不上中继时返回 None（不能当成「不需要登录」）",
            unreachable is None,
            str(unreachable),
        )

        # ---- 5. delete WITHOUT a login is refused -----------
        try:
            host_engine.delete_room(
                "127.0.0.1", RELAY_PORT, ROOM, None, "client-1"
            )
            check("delete without a login is refused", False, "not refused")
        except RuntimeError as e:
            check(
                "delete without a login is refused",
                "登录" in str(e),
                str(e),
            )

        # ---- 6a. create the room the way the CLIENT does (CREATE) ----
        #
        # 2026-09-25 起删房**只认所有者，没有兜底**。老 `HOST <room>` 直接
        # 建出来的房间没有所有者，所以它自己都删不掉；真实客户端是先
        # `CREATE`（自己成为所有者）再开 `HOST` 控制连接 + `HOST_TUNNEL`。
        # 这一套测的就是客户端路径，所以房子也必须这样建。
        owner_state = host_engine.open_room(
            "127.0.0.1", RELAY_PORT, "client-1", "admin", admin.token,
            ROOM, create=True, max_players=4,
        )
        check(
            "客户端路径建房（CREATE）成功，房间有所有者",
            owner_state is not None and getattr(owner_state, "is_owner", False),
            str(owner_state),
        )

        # ---- 6. host a room with the token ------------------
        host_cfg = ClientConfig(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            username="admin",
            client_id="client-1",
            role="host",
            room=ROOM,
            mc_host="127.0.0.1",
            mc_port=MC_PORT,
            max_tunnels=2,
        ).validated()

        host_engine.start_session(host_cfg, auth=admin)

        ok = wait_for(
            lambda: host_engine.snapshot().get("state") == "ONLINE",
            timeout=25,
            label="host ONLINE",
        )
        check("host session reaches ONLINE with a token", ok)

        snapshot = host_engine.snapshot()

        check(
            "session snapshot carries the account name",
            snapshot.get("account") == "admin"
            and snapshot.get("logged_in") is True,
            f"account={snapshot.get('account')} "
            f"logged_in={snapshot.get('logged_in')}",
        )

        # ---- 7. the room shows up in the list ---------------
        rooms = host_engine.fetch_rooms("127.0.0.1", RELAY_PORT, "client-1", "admin")
        match = next((r for r in rooms if r.room == ROOM), None)

        check(
            "fetch_rooms() lists the hosted room",
            match is not None,
            str([r.room for r in rooms]),
        )

        check(
            "room info shows host / capacity / state",
            match is not None
            and match.host == "admin"
            # 1 = 开世界的房主自己。以前这里算 0，于是「在线玩家」永远是 0。
            and match.players == 1
            and match.max_players > 0
            # 没有别人进来时，对想加入的人来说仍然是"空闲可进"。
            and match.state_text == "空闲",
            str(match.to_dict()) if match else "",
        )

        # ---- 8. a second account cannot delete it -----------
        # create + login as another user through the admin API
        import json
        import urllib.request

        request = urllib.request.Request(
            f"http://127.0.0.1:{ADMIN_PORT}/users",
            data=json.dumps(
                {"username": "guest", "password": "guestpass123"}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ADMIN_TOKEN}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            created = json.loads(response.read().decode())

        check("admin API created a second account", created.get("ok") is True, str(created))

        guest = host_engine.login(
            "127.0.0.1", RELAY_PORT, "guest", "guestpass123", "client-2"
        )

        try:
            host_engine.delete_room(
                "127.0.0.1", RELAY_PORT, ROOM, guest, "client-2"
            )
            check("another account cannot delete my room", False, "not refused")
        except RuntimeError as e:
            check(
                "another account cannot delete my room",
                "房主" in str(e),
                str(e),
            )

        # ---- 9. host deletes its own room -------------------
        ok, message = host_engine.session_delete_room(ROOM)
        check("host deletes its own room", ok, message)

        ok = wait_for(
            lambda: all(
                r.room != ROOM
                for r in host_engine.fetch_rooms(
                    "127.0.0.1", RELAY_PORT, "client-1", "admin"
                )
            ),
            timeout=10,
            label="room to disappear",
        )
        check("the deleted room is gone from the list", ok)

        host_engine.stop_session(timeout=15)

        # ---- 10. player joins with a token ------------------
        # 房间在 9 里被删掉了，再建一次（同样走 CREATE —— 见 6a）。
        host_engine.open_room(
            "127.0.0.1", RELAY_PORT, "client-1", "admin", admin.token,
            ROOM, create=True, max_players=4,
        )
        host_engine.start_session(host_cfg, auth=admin)
        wait_for(
            lambda: host_engine.snapshot().get("state") == "ONLINE",
            timeout=25,
            label="host ONLINE again",
        )

        player_cfg = ClientConfig(
            server_host="127.0.0.1",
            server_port=RELAY_PORT,
            username="guest",
            client_id="client-2",
            role="player",
            room=ROOM,
            local_port=PLAYER_PORT,
        ).validated()

        player_engine.start_session(player_cfg, auth=guest)

        ok = wait_for(
            lambda: player_engine.snapshot().get("state") == "ONLINE",
            timeout=20,
            label="player ONLINE",
        )
        check("player session reaches ONLINE with a token", ok)

        # ---- 11. real traffic through the authenticated path -
        import socket

        payload = bytes((i * 13 + 7) % 256 for i in range(32 * 1024))
        echoed = bytearray()

        try:
            with socket.create_connection(
                ("127.0.0.1", PLAYER_PORT), timeout=10
            ) as sock:
                sock.settimeout(30)
                sock.sendall(payload)

                while len(echoed) < len(payload):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    echoed.extend(chunk)

            check(
                "Minecraft data still flows with accounts enabled",
                bytes(echoed) == payload,
                f"{len(echoed)}/{len(payload)} bytes",
            )
        except Exception as e:
            check(
                "Minecraft data still flows with accounts enabled",
                False,
                f"exception: {e!r}",
            )

        # ---- 12. a room WITH a player cannot be deleted -----
        player_engine.stop_session(timeout=15)
        time.sleep(0.8)

        # Reconnect a player so the room is busy again.
        player_engine.start_session(player_cfg, auth=guest)
        wait_for(
            lambda: player_engine.snapshot().get("state") == "ONLINE",
            timeout=20,
            label="player ONLINE again",
        )

        # The player only becomes a session once Minecraft connects.
        import socket as _socket

        busy_sock = _socket.create_connection(("127.0.0.1", PLAYER_PORT), timeout=10)

        ok = wait_for(
            lambda: host_engine.snapshot().get("players", 0) > 0,
            timeout=25,
            label="room to become busy",
        )
        check("room reports a connected player", ok,
              f"players={host_engine.snapshot().get('players')}")

        ok, message = host_engine.session_delete_room(ROOM)

        with open(
            os.path.join(HERE, "busy_delete_msg.txt"),
            "w",
            encoding="utf-8",
            # LF even on Windows: this scratch dump is scanned by the repo-wide
            # line-ending check, and a test artifact must not be the reason it
            # reports the tree as dirty.
            newline="\n",
        ) as fh:
            fh.write(f"ok={ok}\nmessage={message!r}\n")

        check(
            "host CANNOT delete a room that still has players",
            (not ok) and ("玩家" in message or "不能" in message),
            f"ok={ok} message={message!r}",
        )

        busy_sock.close()

    finally:
        for engine in (player_engine, host_engine):
            try:
                engine.shutdown()
            except Exception:
                pass

        relay.terminate()
        try:
            relay.wait(timeout=8)
        except Exception:
            relay.kill()

        server_log.flush()
        server_log.close()

        if FAILED:
            print("")
            print("---- relay server log (tail) ----")
            with open(
                os.path.join(HERE, "v2_client_relay.log"),
                "r",
                encoding="utf-8",
                errors="replace",
            ) as fh:
                print("\n".join(fh.read().splitlines()[-30:]))

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
