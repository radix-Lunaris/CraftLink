#!/usr/bin/env python3
"""
M1（D12 + A2）—— 单房间上限 2→64 与"中继版本对外可读"，跑**真中继**验证。

============================================================================
 这个文件挡的是哪两类事故
============================================================================

1. 「上限改了但没生效」（D12）
   2..8 这个范围散在三处：中继校验（settings_service 的 maximum）、
   compose 的 MCLINK_MAX_PLAYERS、客户端显示。只改文案不改校验的事发生过
   太多次 —— 面板上写着 64，第 9 个人还是被 ROOM_FULL 挡在门外，而且日志
   里一句话都没有。

   所以这里**不读代码常量**，而是起一个真中继、真的开 65 条控制连接：
   前 64 个人必须进去，第 65 个必须被拒。边界值是 64 本身，不是 8/10/16。

   环境里刻意**不设** MCLINK_MAX_PLAYERS：验的是"代码默认值就是 64"，
   不是"我在测试里把它配成了 64"。

2. 「面板上的版本号是假的」（A2）
   管理 API /health 与 status.json 以前写死 `version: "2.0.0"`（管理 API
   自己的字段约定版本），面板把它当中继版本显示 —— 于是中继升到哪一版，
   界面上都写着 2.0.0。

   这里断言 `/health` 的 `relay_version` **逐字等于 server/VERSION 的内容**
   （文件是唯一权威），并确认老键 `version` 也已经是真话、
   `api_version` 仍是字段约定版本。

Run:
    python3 tests/m1_capacity_version_test.py
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_M1_PORT", "19140"))
ADMIN_PORT = int(os.environ.get("MCLINK_TEST_M1_ADMIN_PORT", "19141"))

ADMIN_TOKEN = "test-m1-token"

#: D12 的数字：64 人进得去，第 65 个进不去。下限仍然是 2。
CAP = 64

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


# ============================================================
# helpers
# ============================================================

def relay_version_file():
    """server/VERSION 的内容 —— 唯一的权威版本号（A2 就是拿它比对）。"""
    with open(os.path.join(ROOT, "server", "VERSION"), encoding="utf-8") as fh:
        return fh.read().strip()


def http_request(method, path, token=None, body=None, timeout=10):
    url = f"http://127.0.0.1:{ADMIN_PORT}{path}"
    data = None
    headers = {}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")

        try:
            return e.code, json.loads(payload or "{}")
        except Exception:
            return e.code, {"raw": payload}


def port_open(port, host="127.0.0.1", timeout=0.4):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


async def wait_until(predicate, timeout=25, interval=0.25, label="condition"):
    deadline = time.time() + timeout

    while time.time() < deadline:
        if predicate():
            return True

        await asyncio.sleep(interval)

    print(f"  !! timeout waiting for {label}", flush=True)
    return False


async def send_line(line, timeout=10):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=timeout
    )
    writer.write((line + "\n").encode())
    await writer.drain()

    data = await asyncio.wait_for(reader.readline(), timeout=timeout)

    return reader, writer, data.decode(errors="replace").strip()


async def read_reply(reader, prefixes, timeout=10):
    """
    等一条以 prefixes 开头的回复，跳过中间的广播（HOST_STATE ...）。

    成员控制连接上同时跑着两类东西：请求的应答和主动推送。广播随时可能插在
    应答前面，所以"读下一行"不等于"读我的应答"（room_settings_test 踩过）。
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(
                reader.readline(), timeout=max(0.1, deadline - time.time())
            )
        except asyncio.TimeoutError:
            return ""

        if not raw:
            return ""

        text = raw.decode("utf-8", errors="replace").strip()

        if text and any(text.startswith(prefix) for prefix in prefixes):
            return text

    return ""


async def close_all(members):
    for _reader, writer in members:
        try:
            writer.close()
        except Exception:
            pass

    for _reader, writer in members:
        try:
            await writer.wait_closed()
        except Exception:
            pass


def find_room(room_id):
    status, payload = http_request("GET", "/rooms", token=ADMIN_TOKEN)

    if status != 200:
        return None

    for row in payload.get("data") or []:
        if str(row.get("room")) == room_id:
            return row

    return None


def status_file_has(text, path):
    """status.json 是原子写的；读的时候可能正好被替换，失败就下次再看。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return text in fh.read()
    except OSError:
        return False


# ============================================================
# main
# ============================================================

async def run():
    print("=" * 70)
    print(" M1：单房间上限 2->64（真中继 64 进 / 65 拒）+ 中继版本字段")
    print("=" * 70)

    expected_version = relay_version_file()
    print(f"server/VERSION = {expected_version!r}", flush=True)

    # 版本门（AGENTS.md 规则 1）：握手行尾要挂 `ver=<版本>`，否则中继回
    # ERROR VERSION_MISMATCH。真客户端也是这么发的，测试照抄同一套。
    ver = f" ver={expected_version}" if expected_version else ""

    env = dict(os.environ)

    # 刻意清掉：这一套要证明"不加任何配置时默认就是 64"，
    # 以及"版本来自 server/VERSION"（不是环境变量抄来的）。
    env.pop("MCLINK_MAX_PLAYERS", None)
    env.pop("MCLINK_VERSION", None)

    env.update(
        {
            "MCLINK_PORT": str(RELAY_PORT),
            "MCLINK_DB_ENABLED": "0",
            # 免登录：64 个成员用游客身份进房，测试不必先建 64 个账号。
            "MCLINK_REQUIRE_ACCOUNT": "0",
            "MCLINK_ADMIN_API_ENABLED": "1",
            "MCLINK_ADMIN_API_HOST": "127.0.0.1",
            "MCLINK_ADMIN_API_PORT": str(ADMIN_PORT),
            "MCLINK_ADMIN_API_TOKEN": ADMIN_TOKEN,
            "MCLINK_STATUS_INTERVAL": "2",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            # 64 条连接要活着撑完整个测试：心跳看门狗默认 15s 就会把"安静的"
            # 成员当成掉线收走，那会让 65 号看起来是被回收腾出的位子。
            "MCLINK_MEMBER_IDLE_TIMEOUT": "120",
            "MCLINK_HOST_IDLE_TIMEOUT": "120",
            "MCLINK_STUN_ENABLED": "0",
            "MCLINK_VERBOSE": "0",
        }
    )

    # 日志与 status.json 写进**临时目录**，不落进 tests/：
    # 落进源码树的话每次跑测试都会改文件指纹（BUILD.txt 永远对不上），
    # 而且这些是运行产物，本来就不该进包。
    work_dir = tempfile.mkdtemp(prefix="craftlink-m1-")
    status_path = os.path.join(work_dir, "m1-status.json")

    env["MCLINK_STATUS_FILE"] = status_path

    server_log = open(
        os.path.join(work_dir, "m1-relay.log"),
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

    members = []

    try:
        ok = await wait_until(
            lambda: port_open(RELAY_PORT) and port_open(ADMIN_PORT),
            timeout=30,
            label="relay + admin api",
        )
        check("relay 与 admin API 起来了", ok)

        if not ok:
            return 1

        # ---- 1. A2：版本字段 ----------------------------------------
        status, health = http_request("GET", "/health", token=ADMIN_TOKEN)

        check(
            "/health 可读",
            status == 200 and health.get("ok") is True,
            f"status={status}",
        )
        check(
            "★ A2：/health 的 relay_version == server/VERSION",
            health.get("relay_version") == expected_version,
            f"api={health.get('relay_version')!r} file={expected_version!r}",
        )
        check(
            "★ A2：老键 version 也是真版本（不再是写死的 2.0.0）",
            health.get("version") == expected_version,
            f"version={health.get('version')!r}",
        )
        check(
            "A2：api_version 保留字段约定版本（向后兼容）",
            health.get("api_version") == "2.0.0",
            f"api_version={health.get('api_version')!r}",
        )

        status, stats = http_request("GET", "/stats", token=ADMIN_TOKEN)
        data = stats.get("data") or {}

        check(
            "★ A2：/stats 同样带真实版本（status.json 也写它）",
            data.get("relay_version") == expected_version
            and data.get("version") == expected_version,
            f"relay_version={data.get('relay_version')!r} "
            f"version={data.get('version')!r}",
        )

        wrote = await wait_until(
            lambda: status_file_has(expected_version, status_path),
            timeout=15,
            label="status.json with the real version",
        )
        check("★ A2：status.json 里的版本也是真的", wrote, status_path)

        # ---- 2. D12：边界从 2-8 变成 2-64 ---------------------------
        status, payload = http_request("GET", "/settings", token=ADMIN_TOKEN)
        cap = (payload.get("data") or {}).get("max_players") or {}

        check(
            "★ /settings 的 max_players 边界是 2-64（不再是 2-8）",
            cap.get("minimum") == 2 and cap.get("maximum") == CAP,
            f"min={cap.get('minimum')} max={cap.get('maximum')}",
        )
        check(
            "★ 没设 MCLINK_MAX_PLAYERS 时，默认值就是 64",
            cap.get("value") == CAP,
            f"value={cap.get('value')}",
        )

        status, payload = http_request(
            "PUT", "/settings", token=ADMIN_TOKEN, body={"max_players": CAP}
        )
        check(
            "边界值 64 本身可以被接受",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

        status, payload = http_request(
            "PUT", "/settings", token=ADMIN_TOKEN, body={"max_players": CAP + 1}
        )
        check(
            "★ 65 在设置层就被拒（错误里写明 2-64）",
            status == 400 and "2-64" in str(payload.get("error", "")),
            f"status={status} error={payload.get('error')}",
        )

        status, payload = http_request("GET", "/stats", token=ADMIN_TOKEN)
        check(
            "/stats 报告的实时上限是 64",
            (payload.get("data") or {}).get("max_players") == CAP,
            f"max_players={(payload.get('data') or {}).get('max_players')}",
        )

        # ---- 3. 真开房：64 个人进去 --------------------------------
        reader, writer, reply = await send_line(
            f"CREATE __mclink__ m1-owner owner - m1cap64 {CAP}{ver}"
        )
        check(
            "CREATE 建房（容量 64）",
            reply.startswith("OK JOIN") and "owner=1" in reply,
            reply,
        )
        members.append((reader, writer))

        # 其余 63 个人（含房主共 64）。
        joined = 0
        for index in range(2, CAP + 1):
            m_reader, m_writer, m_reply = await send_line(
                f"JOIN __mclink__ m1-user-{index} p{index} - m1cap64{ver}"
            )
            members.append((m_reader, m_writer))

            if m_reply.startswith("OK JOIN"):
                joined += 1
            else:
                check(f"第 {index} 个人进房", False, m_reply)
                break

        check(
            f"★ 64 个人全部进得去（房主 + 63）",
            joined == CAP - 1,
            f"joined={joined + 1}/{CAP}",
        )

        room = find_room("m1cap64")
        check(
            "★ 房间里真的坐着 64 个人（不是只放行不记账）",
            room is not None
            and int(room.get("members") or 0) == CAP
            and int(room.get("max_players") or 0) == CAP,
            f"room={room if room is None else {k: room.get(k) for k in ('members', 'max_players')}}",
        )

        # ---- 4. 第 65 个人必须被拒 ---------------------------------
        f_reader, f_writer, f_reply = await send_line(
            f"JOIN __mclink__ m1-user-65 p65 - m1cap64{ver}"
        )
        members.append((f_reader, f_writer))

        check(
            f"★ 第 {CAP + 1} 个人被 ROOM_FULL 拒绝",
            f_reply.startswith("ERROR ROOM_FULL"),
            f_reply,
        )

        room = find_room("m1cap64")
        check(
            "拒绝之后房间里还是 64 个人（没有被顶掉/没有多出来）",
            room is not None and int(room.get("members") or 0) == CAP,
            f"members={None if room is None else room.get('members')}",
        )

        # ---- 5. 收尾：强制关房，成员必须收到通知 --------------------
        status, payload = http_request(
            "POST",
            "/rooms/m1cap64/revoke",
            token=ADMIN_TOKEN,
            body={"reason": "M1 测试收尾"},
        )
        check(
            "收尾：管理 API 能关掉这个 64 人房",
            status == 200 and payload.get("ok") is True,
            f"status={status} body={payload}",
        )

    finally:
        await close_all(members)

        with contextlib.suppress(Exception):
            relay.terminate()
            relay.wait(timeout=10)

        with contextlib.suppress(Exception):
            if relay.poll() is None:
                relay.kill()

        server_log.close()

        with contextlib.suppress(Exception):
            shutil.rmtree(work_dir)

    print("")
    print("=" * 70)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 70)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
