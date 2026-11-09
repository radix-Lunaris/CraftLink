"""
CraftLink -- 数据库卡住时，中继必须照样起来（真起中继 + 假 MySQL）。

============================================================================
 这一套测的是哪个故障
============================================================================

2026-09-15 的现场：房间 3 个人、客户端显示「已加入世界 9 人」，重打包上传后
中继重建失败 —— 日志停在

    [DB] connecting to mysql://mclink@web-mysql80:3306/mclink

既没有下一行，也没有报错。容器一直 unhealthy，19100 从来没被监听。

根因是中继的 V2 启动**全部跑在监听之前**，而其中任何一次数据库调用都
没有时限：

  * `connect()`   —— TCP 连上了，但 MySQL 不回握手包
  * `SELECT 1`    —— 握手过了，但那条 SQL 永远不回（元数据锁 / 半死连接）
  * `ALTER TABLE` —— 建表迁移排在别人的长事务后面

只要其中任何一个"慢"，全世界都进不来（连完全不依赖数据库的原始 TCP 转发
也一起被拖死）。这是最不该发生的耦合。

所以现在每一步都有硬上限（`MCLINK_DB_BOOT_TIMEOUT`），单条 SQL 还有
socket 读写上限（`MCLINK_DB_QUERY_TIMEOUT`），超时只降级
—— 面板/账号不可用，联机照旧。

这一套用**真的中继子进程** + **假的 MySQL 服务**覆盖两种卡法：

  1. blackhole-handshake：接受 TCP 连接，然后永远不发握手包
     （连 `connect()` 都过不去）
  2. blackhole-query：握手走完，登录能过，但任何查询都不回
     （`SELECT 1` 探活永远等不到 —— 生产环境最像的一种）

两种都必须满足：中继在期限内开始监听 19100、状态文件里的 database.healthy
是 false（诚实报告）、而联机端口是活的。

Run:
    python tests/db_boot_timeout_test.py
"""

import asyncio
import contextlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RELAY_PORT = int(os.environ.get("MCLINK_TEST_DBBOOT_PORT", "19172"))
FAKE_DB_PORT = int(os.environ.get("MCLINK_TEST_DBBOOT_DB_PORT", "13306"))

#: 中继每一步数据库操作的硬上限。故意设得很短，测试才跑得快；
#: 生产默认是 20 秒。
BOOT_TIMEOUT = 4
QUERY_TIMEOUT = 3
CONNECT_TIMEOUT = 3

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"

    if detail:
        line += f"  -- {detail}"

    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    (PASSED if ok else FAILED).append(name)


def port_open(port, host="127.0.0.1", timeout=0.4):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


# ============================================================
# 假 MySQL：只做够骗过驱动的那一点握手，然后按剧本卡住
# ============================================================

def _packet(payload, sequence):
    """MySQL 包 = 3 字节长度 + 1 字节序号 + 正文。"""
    return struct.pack("<I", len(payload))[:3] + bytes([sequence]) + payload


def _handshake_packet():
    """
    mysql_native_password 的握手包。密码认证走的是"客户端直接算好
    SHA1 摘要发过来"，服务端不需要知道明文 —— 所以这里完全不用实现
    密码校验，只要包格式对，PyMySQL 就会认为登录成功。
    """
    payload = bytearray()

    payload += b"\x0a"                  # 协议版本 10
    payload += b"8.0.46-fake\x00"       # 服务端版本
    payload += struct.pack("<I", 1)     # 连接 id
    payload += b"12345678"              # auth-plugin-data-part-1
    payload += b"\x00"                  # filler
    payload += struct.pack("<H", 0xFFFF)  # capability flags (low)
    payload += b"\x21"                  # 字符集 utf8_general_ci
    payload += struct.pack("<H", 0x0002)  # status flags
    payload += struct.pack("<H", 0xFFFF)  # capability flags (high)
    payload += b"\x15"                  # auth plugin data 长度
    payload += b"\x00" * 10             # reserved
    payload += b"abcdefghijklm\x00"     # auth-plugin-data-part-2
    payload += b"mysql_native_password\x00"

    return _packet(bytes(payload), 0)


def _ok_packet():
    """OK 包：header 0x00 + affected rows + last insert id + status + warnings。"""
    return _packet(b"\x00\x00\x00\x02\x00\x00\x00", 2)


class FakeHangingMySQL:
    """
    假的 MySQL：握手怎么走由 `mode` 决定，之后一律"不回"。

    mode="handshake" 连握手都不发（connect() 自己就会超时）
    mode="query"     握手 + 登录成功，但任何 COM_QUERY 都不回
                     —— 这就是生产现场的样子：TCP 通、登录过、SQL 永远不回
    """

    def __init__(self, port, mode="query"):
        self.port = port
        self.mode = mode
        self.connections = 0
        self.queries = 0
        self._server = None
        self._thread = None
        self._stop = threading.Event()

    # ---- 生命周期 ----

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", self.port))
        self._server.listen(8)
        self._server.settimeout(0.3)

        self._thread = threading.Thread(
            target=self._serve, name="fake-hanging-mysql", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

        with contextlib.suppress(Exception):
            self._server.close()

        with contextlib.suppress(Exception):
            self._thread.join(timeout=3)

    # ---- 主循环 ----

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            self.connections += 1

            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()

    def _handle(self, conn):
        try:
            conn.settimeout(0.5)

            if self.mode == "handshake":
                # 接受连接，然后什么都不发 —— 客户端会卡在等握手包。
                self._idle_forever(conn)
                return

            conn.sendall(_handshake_packet())

            if not self._read_login(conn):
                return

            conn.sendall(_ok_packet())

            # 登录成功之后再"咽气"：读掉 COM_QUERY，然后永远不回。
            self._idle_forever(conn, count_queries=True)

        except OSError:
            pass

        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def _idle_forever(self, conn, count_queries=False):
        """保持连接打开、什么都不回，直到测试结束。"""
        while not self._stop.is_set():
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return

            if not data:
                return

            if count_queries and data[:1] == b"\x03":
                self.queries += 1

    def _read_login(self, conn):
        """
        读完整的登录响应包（按 MySQL 包的 3 字节长度读全）。

        为什么必须"读全"：客户端发完登录响应就等着服务端回 OK，
        `set_character_set()` 也在等。要是这里只读了一半就回 OK，
        客户端会拿着半个包继续等别的，测试永远走不到"查询不回"那一步。
        """
        header = self._read_exactly(conn, 4)

        if header is None:
            return False

        length = header[0] | (header[1] << 8) | (header[2] << 16)

        if length <= 0 or length > (1 << 20):
            return False

        return self._read_exactly(conn, length) is not None

    def _read_exactly(self, conn, count):
        """读满 count 个字节；连接断开或超时返回 None。"""
        chunks = bytearray()
        deadline = time.time() + 5

        while len(chunks) < count and time.time() < deadline:
            if self._stop.is_set():
                return None

            try:
                data = conn.recv(count - len(chunks))
            except socket.timeout:
                continue
            except OSError:
                return None

            if not data:
                return None

            chunks += data

        return bytes(chunks) if len(chunks) == count else None


# ============================================================
# 一个场景：真中继 + 假 MySQL
# ============================================================

async def run_scenario(mode, label):
    """起一次真中继，看它有没有被卡死的数据库拖住。"""
    print("")
    print("-" * 64)
    print(f"场景：{label}（假 MySQL 模式 = {mode}）")
    print("-" * 64)

    fake = FakeHangingMySQL(FAKE_DB_PORT, mode=mode)
    fake.start()

    status_file = os.path.join(
        tempfile.mkdtemp(prefix=f"craftlink-dbboot-{mode}-"), "status.json"
    )

    env = dict(os.environ)
    env.update(
        {
            "MCLINK_PORT": str(RELAY_PORT),
            "MCLINK_STATUS_FILE": status_file,
            "MCLINK_STATUS_INTERVAL": "2",
            "MCLINK_REQUIRE_ACCOUNT": "0",
            "MCLINK_ADMIN_API_ENABLED": "0",
            "MCLINK_STUN_ENABLED": "0",
            "MCLINK_VERBOSE": "0",
            "MCLINK_ROOM_EMPTY_GRACE": "0",
            "MCLINK_LOG_DIR": os.path.dirname(status_file),
            # ★ 被测对象：数据库开着，但对端不回话
            "MCLINK_DB_ENABLED": "1",
            "MCLINK_DB_HOST": "127.0.0.1",
            "MCLINK_DB_PORT": str(FAKE_DB_PORT),
            "MCLINK_DB_NAME": "mclink",
            "MCLINK_DB_USER": "mclink",
            "MCLINK_DB_PASSWORD": "CHANGE_ME_TEST_ONLY",
            "MCLINK_DB_CONNECT_TIMEOUT": str(CONNECT_TIMEOUT),
            "MCLINK_DB_BOOT_TIMEOUT": str(BOOT_TIMEOUT),
            "MCLINK_DB_QUERY_TIMEOUT": str(QUERY_TIMEOUT),
        }
    )

    log_path = os.path.join(os.path.dirname(status_file), "relay.out")
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")

    relay = subprocess.Popen(
        [sys.executable, "-u", "relay_server.py"],
        cwd=os.path.join(ROOT, "server"),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        # ---- 1. 必须在期限内开始监听 ----
        #
        # 这是本测试的全部意义：数据库永远不回话，19100 也必须开。
        # 期限 = 启动时限 + 连接时限 + 留一点余量。
        deadline = BOOT_TIMEOUT + CONNECT_TIMEOUT + 8
        started = time.time()

        listening = False

        while time.time() - started < deadline:
            if relay.poll() is not None:
                break

            if port_open(RELAY_PORT):
                listening = True
                break

            await asyncio.sleep(0.25)

        elapsed = time.time() - started

        check(
            f"数据库卡死时，中继在 {deadline}s 内开始监听 19100",
            listening,
            f"用了 {elapsed:.1f}s"
            + ("" if listening else f"；进程 exit={relay.poll()}"),
        )

        if not listening:
            with contextlib.suppress(Exception):
                log_file.flush()

            with open(log_path, encoding="utf-8", errors="replace") as fh:
                print("  ---- 中继输出 ----")
                for line in fh.read().splitlines()[-30:]:
                    print(f"    {line}")

            return

        # ---- 2. 日志必须说清楚它为什么没连上（可诊断，不是静默）----
        #
        # 两道防线都会在这里体现，任何一种都算合格：
        #   * 驱动自己的 socket 超时（首选，线程自己带异常退出）：
        #         (2013, 'Lost connection to MySQL server during query (timed out)')
        #   * 万一驱动没超时，asyncio 这一步的硬上限兜底：
        #         [DB] 连接数据库 超过 N 秒没有响应
        # 真正不合格的是"既没有下一行、也没有报错" —— 也就是现场那个样子。
        with contextlib.suppress(Exception):
            log_file.flush()

        with open(log_path, encoding="utf-8", errors="replace") as fh:
            output = fh.read()

        diagnosable = (
            "timed out" in output
            or "没有响应" in output
            or "数据库不可用" in output
        )

        check(
            "日志里说清楚了为什么没连上（不是静默卡住）",
            diagnosable,
            "" if diagnosable else "日志里既没有超时说明，也没有真实报错",
        )

        # ---- 3. 状态文件要诚实报告数据库不健康 ----
        healthy = None

        for _ in range(24):
            try:
                with open(status_file, encoding="utf-8") as fh:
                    data = json.load(fh)

                healthy = bool((data.get("database") or {}).get("healthy"))
                break
            except Exception:
                await asyncio.sleep(0.5)

        check(
            "状态文件里 database.healthy = false（诚实报告，不假装连上）",
            healthy is False,
            f"healthy={healthy!r}",
        )

        # ---- 4. 假数据库确实被中继连过（证明确实是在等它）----
        check(
            "中继确实连到了数据库（所以刚才是在等它，而不是根本没连）",
            fake.connections >= 1,
            f"connections={fake.connections}",
        )

        # ---- 5. 原始 TCP 通路仍然是通的（冻结的那条路）----
        #
        # 注意：第一个连上来的 HOST 连接被当作"建房"，所以回的是 OK JOIN；
        # 这里只要确认握手协议有正常回复即可 —— 数据通路本身由
        # e2e_relay_test / session_lifecycle_test 负责。
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", RELAY_PORT), timeout=5
            )

            writer.write(b"CREATE __mclink__ cid-dbboot owner - dbboot-room 4\n")
            await writer.drain()

            line = await asyncio.wait_for(
                reader.readline(), timeout=8
            )

            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

            check(
                "数据库卡死时，建房握手照常可用（原始 TCP 通路没被拖累）",
                line.startswith(b"OK "),
                f"回复：{line[:80]!r}",
            )
        except Exception as e:  # noqa: BLE001
            check("数据库卡死时，建房握手照常可用（原始 TCP 通路没被拖累）",
                  False, f"{type(e).__name__}: {e}")

        # ---- 6. 能干净退出（没有非守护线程被卡住的 SQL 拖住）----
        stop_started = time.time()

        relay.terminate()

        with contextlib.suppress(Exception):
            relay.wait(timeout=10)

        exited = relay.poll() is not None

        if not exited:
            relay.kill()
            with contextlib.suppress(Exception):
                relay.wait(timeout=5)

        check(
            "中继能正常退出（卡住的 SQL 不会拖住进程关闭）",
            exited,
            f"{time.time() - stop_started:.1f}s",
        )

    finally:
        if relay.poll() is None:
            relay.kill()

        with contextlib.suppress(Exception):
            relay.wait(timeout=5)

        log_file.close()
        fake.stop()


async def main():
    print("=" * 64)
    print("CraftLink 数据库卡死时的启动测试")
    print("=" * 64)

    if port_open(RELAY_PORT) or port_open(FAKE_DB_PORT):
        print(f"  !! 端口被占用（relay={RELAY_PORT} db={FAKE_DB_PORT}），先关掉占用者")
        return 1

    # 场景一：连握手都不回 —— connect() 本身就该超时
    await run_scenario("handshake", "MySQL 接受了 TCP 但从不回握手包")

    # 场景二：握手/登录都成功，但 SQL 永远不回
    # （生产现场最像的一种：TCP 通、`db ok`、日志停在 connecting to 之后）
    await run_scenario("query", "MySQL 登录成功但任何查询都不回")

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")

    for name in FAILED:
        print(f"  FAILED: {name}")

    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
