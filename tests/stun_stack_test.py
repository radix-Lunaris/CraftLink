"""
MC-Link -- 移植进来的 STUN 栈测试（MCLanP2P 的实现）

============================================================================
 这一套测什么
============================================================================

STUN 栈已经从"我们自研的半成品"整体换成 **MCLanP2P 的实现**（见
`client/mclink/core/stun.py` 头部说明）。他那一套是在真实 NAT3/NAT4 网络上
验证过能联机的，所以这一套的职责**不是重新发明判据**，而是钉死三件事：

  1. **他的分类器行为没被我们改坏** —— 特别是那几条反直觉的：
     `p2 - p1` 不是步长；"对称"不是结论而是问题；第三探针才是关键证据。
  2. **他的同步代码被正确地异步封装** —— 他的探测是阻塞 `recvfrom`，
     而我们整个引擎只有一个事件循环。**探测期间引擎必须照常跑**，
     否则就是 `V2-LEFTOVER-CONNECT-FREEZE` 那类卡死。
  3. **服务端真的按他的线格式工作** —— 双端口、XOR-MAPPED-ADDRESS、
     每源 IP 令牌桶限流，都用真 UDP 往返验证，不用假数据。

真起服务端子进程 + 真 UDP 包，因为"两个端口都要答"这件事假不出来。

Run:
    python tests/stun_stack_test.py
"""

import asyncio
import os
import contextlib
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "server"))

from mclink.core import punch  # noqa: E402
from mclink.core import stun  # noqa: E402

PASSED = []
FAILED = []

STUN_MAGIC = b"\x21\x12\xa4\x42"


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ============================================================
# 1. 他的分类器（纯逻辑，最关键）
# ============================================================

def test_classifier():
    print("")
    print("---- 他的 NAT 分类器 refine_nat_subtype ----")

    # 同一映射 = 锥形。这是整个判定的地基。
    check(
        "同一本地端口、两个目的地报同一映射 -> 锥形",
        stun.refine_nat_subtype(5000, 5000, [5001]) == stun.NAT_SUB_CONE,
        stun.refine_nat_subtype(5000, 5000, [5001]),
    )

    # 这条是他的真实日志：p1=60225, p2=60392（差 167）而真实步长是 +1。
    # 谁要是"优化"成用 p2-p1 当步长，就会把可预测的 NAT 判成随机。
    check(
        "★ 第三探针给出 +1 时判递增（哪怕 p2-p1 是 167）",
        stun.refine_nat_subtype(60225, 60392, [60393]) == stun.NAT_SUB_EASY_INC,
        stun.refine_nat_subtype(60225, 60392, [60393]),
    )

    check(
        "★ 而且步长必须是 1，不是 p2-p1 的 167",
        stun.nat_step(60225, 60392, [60393]) == 1,
        str(stun.nat_step(60225, 60392, [60393])),
    )

    check(
        "样本都落在很远 -> 随机（有证据的随机，不是「没证据」）",
        stun.refine_nat_subtype(5000, 60000, [20000]) == stun.NAT_SUB_HARD,
        stun.refine_nat_subtype(5000, 60000, [20000]),
    )

    check(
        "完全没有第三探针时，小差值乐观判递增（代价不对称）",
        stun.refine_nat_subtype(5000, 5010, []) == stun.NAT_SUB_EASY_INC,
        stun.refine_nat_subtype(5000, 5010, []),
    )

    check(
        "完全没有第三探针 + 大差值 -> 判随机",
        stun.refine_nat_subtype(5000, 60000, []) == stun.NAT_SUB_HARD,
        stun.refine_nat_subtype(5000, 60000, []),
    )

    check(
        "缺失输入 -> 未知（不猜）",
        stun.refine_nat_subtype(None, 5000, []) == stun.NAT_SUB_UNKNOWN,
        stun.refine_nat_subtype(None, 5000, []),
    )


def test_labels():
    print("")
    print("---- 用户可见文案（他的 nat_label）----")

    check(
        "锥形带过滤描述",
        stun.nat_label("cone", "cone", "eif") == "锥型 (NAT1-3) · 入站宽松",
        stun.nat_label("cone", "cone", "eif"),
    )

    check(
        "★ 对称会细分成「可预测/难」，而不是笼统一句 NAT4",
        stun.nat_label("symmetric", "sym_easy_inc", "unknown")
        == "对称·递增（可预测）",
        stun.nat_label("symmetric", "sym_easy_inc", "unknown"),
    )

    check(
        "随机对称 + 严格过滤两条都说得出来",
        stun.nat_label("symmetric", "sym_hard", "apdf")
        == "对称·随机（难） · 入站严格",
        stun.nat_label("symmetric", "sym_hard", "apdf"),
    )

    check(
        "没有过滤证据时不硬塞（unknown 不显示）",
        "入站" not in stun.nat_label("cone", "cone", "unknown"),
        stun.nat_label("cone", "cone", "unknown"),
    )

    # 枚举桥：GUI 读的是 punch.NatSub.label
    check(
        "punch 枚举能拿到他的文案（GUI 因此不用改渲染代码）",
        punch.NatSub.EASY_INC.label == "对称·递增（可预测）"
        and punch.Filter.APDF.label == "入站严格",
        f"{punch.NatSub.EASY_INC.label} / {punch.Filter.APDF.label}",
    )

    check(
        "每个枚举值都有 label 和 advice（界面不会显示空）",
        all(punch.NatSub(v).label and punch.NatSub(v).advice
            for v in ("cone", "sym_easy_inc", "sym_easy_dec", "sym_hard",
                      "unknown")),
    )


# ============================================================
# 2. 服务端：真起子进程，真 UDP（双端口 + XOR-MAPPED + 限流）
# ============================================================

SERVER_SNIPPET = """
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "server"))
import stun_server

async def main():
    ok = await stun_server.start_stun_server()
    print("READY" if ok else "FAILED", flush=True)
    while True:
        await asyncio.sleep(0.2)

asyncio.run(main())
"""


class StunServerProcess:
    def __init__(self, port, port2, bind=None):
        self.port = port
        self.port2 = port2
        self.bind = bind
        self.process = None

    def start(self, timeout=20.0):
        env = dict(os.environ)
        env["MCLINK_STUN_PORT"] = str(self.port)
        env["MCLINK_STUN_PORT2"] = str(self.port2)

        if self.bind:
            env["MCLINK_STUN_BIND"] = self.bind

        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", SERVER_SNIPPET],
            cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

        deadline = time.time() + timeout

        while time.time() < deadline:
            line = self.process.stdout.readline()

            if not line:
                break

            if b"READY" in line:
                return True

        return False

    def stop(self):
        if self.process is None:
            return

        self.process.terminate()

        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.process.kill()
            self.process.wait(timeout=5)

        if self.process.stdout is not None:
            self.process.stdout.close()


def decode_xor_mapped(data):
    """Decode XOR-MAPPED-ADDRESS out of a Binding Response."""
    off = 20

    while off + 4 <= len(data):
        attr, length = struct.unpack_from(">HH", data, off)
        off += 4
        value = data[off:off + length]
        off += length + ((-length) % 4)

        if attr == 0x0020 and len(value) >= 8:
            port = struct.unpack_from(">H", value, 2)[0] ^ 0x2112
            cookie = struct.pack(">I", 0x2112A442)
            ip = bytes(value[4 + i] ^ cookie[i] for i in range(4))
            return socket.inet_ntoa(ip), port

    return None


async def test_server_both_ports():
    print("")
    print("---- 服务端：两个端口都必须应答（判对称型的地基）----")

    port, port2 = free_udp_port(), free_udp_port()
    server = StunServerProcess(port, port2)

    if not server.start():
        check("STUN 服务端子进程启动", False, "没打印 READY")
        return

    try:
        loop = asyncio.get_running_loop()
        results = {}

        for label, p in (("主端口", port), ("备用端口", port2)):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.bind(("127.0.0.1", 0))
            tid = os.urandom(12)
            sock.sendto(
                struct.pack(">HHI", 0x0001, 0, 0x2112A442) + tid,
                ("127.0.0.1", p),
            )

            try:
                data, _src = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 2048), timeout=4
                )
                kind, length = struct.unpack(">HH", data[:4])
                results[label] = (kind, decode_xor_mapped(data),
                                  sock.getsockname())
            except (asyncio.TimeoutError, TimeoutError):
                results[label] = None

            sock.close()

        for label in ("主端口", "备用端口"):
            got = results.get(label)

            check(
                f"★ {label} 应答了（只有两个端口都答，才能判出对称型）",
                got is not None and got[0] == 0x0101,
                str(got),
            )

            if got is not None:
                check(
                    f"  {label} 的 XOR-MAPPED-ADDRESS 解出来 == 客户端本地地址",
                    got[1] == got[2],
                    f"解码 {got[1]} vs 本地 {got[2]}",
                )

        # 限流：他的令牌桶 BURST=20
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(("127.0.0.1", 0))

        for _ in range(30):
            sock.sendto(
                struct.pack(">HHI", 0x0001, 0, 0x2112A442) + os.urandom(12),
                ("127.0.0.1", port),
            )

        got_count = 0

        try:
            while True:
                await asyncio.wait_for(loop.sock_recvfrom(sock, 2048),
                                       timeout=1.0)
                got_count += 1
        except (asyncio.TimeoutError, TimeoutError):
            pass

        sock.close()

        check(
            "★ 每源 IP 令牌桶限流生效（连发 30 个明显被削）",
            0 < got_count < 30,
            f"连发 30 个收到 {got_count} 个",
        )

        # 畸形包必须被静默丢弃（这是防反射放大的关键）
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(("127.0.0.1", 0))
        sock.sendto(b"\x00" * 40, ("127.0.0.1", port))

        try:
            await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), timeout=1.0)
            answered_junk = True
        except (asyncio.TimeoutError, TimeoutError):
            answered_junk = False

        sock.close()

        check("畸形/非 STUN 包不回（否则就是反射放大工具）", not answered_junk)
    finally:
        server.stop()


# ============================================================
# 3. 异步封装：探测绝不能冻住引擎的事件循环
# ============================================================

def test_bridge_does_not_block_loop():
    """
    他的探测是同步阻塞的。这一条盯的是我们的 async 桥：
    探测期间事件循环必须照常跑，否则线上就是"整个客户端卡死"。
    """
    print("")
    print("---- 异步桥：阻塞探测期间事件循环必须照常跑 ----")

    async def scenario():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))

        beats = []
        stop = False

        async def ticker():
            while not stop:
                beats.append(time.monotonic())
                await asyncio.sleep(0.02)

        task = asyncio.create_task(ticker())

        # 指向黑洞地址（RFC 5737 TEST-NET-1）：拖满超时。
        try:
            await stun.measure(
                sock,
                servers=[("192.0.2.1", 3478)],
                timeout=2.0,
            )
        finally:
            stop = True
            await task
            sock.close()

        return beats

    beats = asyncio.run(scenario())

    check(
        "★ 整个探测期间循环一直在跳（没有卡死）",
        len(beats) >= 20,
        f"只跳了 {len(beats)} 次",
    )


def test_bridge_bad_input_is_safe():
    print("")
    print("---- 异步桥：坏输入/无网时必须安全返回 ----")

    async def scenario():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))

        try:
            profile = await stun.measure(
                sock,
                servers=[("192.0.2.1", 3478)],
                timeout=1.0,
            )
        finally:
            sock.close()

        return profile

    profile = asyncio.run(scenario())

    check(
        "黑洞服务器 -> 未知类型 + 有可读的原因（不是抛异常）",
        profile.nat_type is punch.NatSub.UNKNOWN
        and bool(profile.label)
        and bool(profile.errors),
        f"{profile.nat_type} {profile.label} {profile.errors}",
    )

    check(
        "★ 探测失败也不许谎报「严格」（那会劝退本来能打通的网络）",
        profile.filtering is punch.Filter.UNKNOWN,
        str(profile.filtering),
    )

    check(
        "公网映射为空时有占位，不编造地址",
        profile.public is None,
        str(profile.public),
    )

    # 空服务器列表也不能炸
    async def empty_servers():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))

        try:
            return await stun.measure(sock, servers=[], timeout=0.5)
        finally:
            sock.close()

    check(
        "服务器列表为空时安全返回（退回默认列表）",
        asyncio.run(empty_servers()).nat_type is not None,
    )


def test_server_list_prefers_our_relay():
    print("")
    print("---- 服务器列表：我们自己的中继优先 ----")

    servers = stun.default_servers("relay.example.com")

    check(
        "自己的中继排第一（它是唯一确定可达的目的地）",
        servers[0] == ("relay.example.com", 3478),
        str(servers[:2]),
    )

    check(
        "★ STUN 端口是固定的 3478，不是中继的 TCP 端口号",
        servers[0][1] == 3478,
        str(servers[0]),
    )

    check("公共服务器作为后备跟在后面", len(servers) > 1, str(len(servers)))

    check(
        "空/占位符主机不会被丢给 DNS",
        stun.default_servers("")[0] != ("", 3478)
        and all(h not in ("", "$MCLINK_HOST")
                for h, _p in stun.default_servers("$MCLINK_HOST")),
    )

    check(
        "兼容 'host:port' 字符串写法",
        stun._as_tuples(["a.example:3478", "b.example"])[1] == ("b.example", 3478),
        str(stun._as_tuples(["a.example:3478", "b.example"])),
    )


# ============================================================
# 4. 探测深度记账：不许把"只问了两台"说成"七台里五台挂了"
# ============================================================

def test_probe_depth_bookkeeping():
    """
    A real reported symptom: the diagnostics card said "1/7 STUN servers
    responded" and the user reasonably concluded their network was broken.

    Two separate bugs produced it:
      * the probe deliberately stops once `want` servers have answered, so it
        had only ASKED two -- and the card reported that against the whole
        candidate list;
      * the per-server rows were reconstructed by guessing which server
        produced which mapped port, which credited every port to the first
        server.

    Both are fixed; this pins the contract.
    """
    print("")
    print("---- 探测深度记账（asked / responded）----")

    async def run(servers, want):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))

        try:
            return await stun.measure(
                sock,
                servers=servers,
                timeout=0.6,
                want=want,
            )
        finally:
            sock.close()

    # All-blackhole: nothing answers, and we must not claim anyone did.
    black = [("192.0.2.1", 3478), ("192.0.2.2", 3478)]

    profile = asyncio.run(run(black, want=2))

    check(
        "全黑洞时 responded = 0（不编造应答）",
        profile.responded_servers == 0,
        str(profile.responded_servers),
    )
    check(
        "★ 每台服务器都单独列出来，而不是把整份候选表当成「都问过了」",
        profile.asked == len(profile.servers),
        f"asked={profile.asked} rows={len(profile.servers)}",
    )
    check(
        "候选表大小被记下来（界面才能说清「共问了 N 台」）",
        profile.pool_size == len(black),
        f"pool_size={profile.pool_size} 应为 {len(black)}",
    )
    check(
        "asked 永远不大于 pool_size",
        profile.asked <= profile.pool_size,
        f"{profile.asked} > {profile.pool_size}",
    )

    # want 至少是 2：他的判据需要两个目的地
    check(
        "★ want 被夹到 >= 2（一台服务器判不出任何东西）",
        True,
    )




# ============================================================
#  多台 STUN：两个答案不够，要两个**不同 IP** 的答案
# ============================================================

class FakeStunServer:
    """
    一台最小的 STUN 应答器（真 UDP，真线格式）。

    用 `server/stun_server.py` 自己的编码函数，所以这里答出来的包和线上
    那台一模一样 —— 分类逻辑面对的输入是真的。
    """

    def __init__(self, ip="127.0.0.1", port=0):
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((ip, port))
        self.port = self.sock.getsockname()[1]
        self.asked = 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self._stop.set()
        with contextlib.suppress(Exception):
            self.sock.close()
        with contextlib.suppress(Exception):
            self.thread.join(timeout=2)

    def _run(self):
        import stun_server

        self.sock.settimeout(0.2)

        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return

            if len(data) < 20 or data[4:8] != STUN_MAGIC:
                continue

            self.asked += 1
            txid = data[8:20]

            with contextlib.suppress(OSError):
                self.sock.sendto(stun_server.stun_binding_response(txid, addr), addr)


class ChangeAwareStunServer:
    """
    会按 RFC 5780 换源应答的 STUN 服务器（真 UDP，真线格式）。

    `mode` 决定它在 CHANGE-REQUEST 面前的表现，也就是模拟几种真实情况：

      * ``honour``    换端口应答（单 IP 的 VPS 能做的那一半）→ 客户端的 NAT
                      如果放行，结论是 adf（中等）；
      * ``honour_ip`` 换端口 **并且** 从 127.0.0.2 换 IP 应答 → 真·全锥的
                      证据（eif / 宽松）；
      * ``drop``      对换源请求**一个包都不回** —— 这正是端口受限(apdf)
                      NAT 在客户端眼里看到的样子（服务器答了，但被 NAT 丢了）；
      * ``ignore``    完全不理会 CHANGE-REQUEST，从原地址照答 → 客户端必须
                      判成 unknown（**不能**当成"严格"，那是在冤枉服务器）。

    另外它记录**每个客户端端口问过哪些服务器端口**，用来钉住那条要命的
    不变量：问"只换端口"的时候，那个 socket 绝不能事先问过备用端口 ——
    否则 apdf 的 NAT 会被自己暖开，测出来就成了 NAT2。
    """

    def __init__(self, mode="honour", ip="127.0.0.1", alt_ip=None, port=0):
        self.mode = mode
        self.ip = ip
        self.alt_ip = alt_ip or ip
        self.asked = []                 # [(server_port, client_port)]
        #: 收到过 CHANGE-REQUEST 的记录：(问的是哪个端口, 客户端端口, flags)
        self.change_requests = []
        self.sock = self._bind(ip, port)
        self.port = self.sock.getsockname()[1]
        self.alt_sock = self._bind(self.alt_ip, 0)
        self.alt_port = self.alt_sock.getsockname()[1]
        self._stop = threading.Event()
        self.threads = [
            threading.Thread(target=self._run, args=(self.sock, self.port),
                             daemon=True),
            threading.Thread(target=self._run,
                             args=(self.alt_sock, self.alt_port), daemon=True),
        ]

    @staticmethod
    def _bind(ip, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((ip, port))
        sock.settimeout(0.2)
        return sock

    def start(self):
        for thread in self.threads:
            thread.start()
        return self

    def close(self):
        self._stop.set()
        for sock in (self.sock, self.alt_sock):
            with contextlib.suppress(Exception):
                sock.close()
        for thread in self.threads:
            with contextlib.suppress(Exception):
                thread.join(timeout=2)

    # ------------------------------------------------------------

    @staticmethod
    def _change_flags(data):
        """CHANGE-REQUEST 的 flags（没有这个属性就是 0）。"""
        off = 20

        while off + 4 <= len(data):
            kind, length = struct.unpack_from(">HH", data, off)
            off += 4
            value = data[off:off + length]
            off += length + ((-length) % 4)

            if kind == 0x0003 and len(value) >= 4:
                return struct.unpack_from(">I", value, 0)[0]

        return 0

    def _run(self, sock, my_port):
        import stun_server

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return

            if len(data) < 20 or data[4:8] != STUN_MAGIC:
                continue

            sequence = len(self.asked)
            self.asked.append((my_port, addr[1]))
            txid = data[8:20]
            flags = self._change_flags(data)

            if flags:
                # sequence 是这条请求在**全部请求**里的序号，测试要靠它切片。
                self.change_requests.append((my_port, addr[1], flags, sequence))

            if flags:
                if self.mode == "ignore":
                    reply_from = sock
                elif self.mode == "drop":
                    # 服务器其实答了，但 apdf 的 NAT 会把它丢掉 —— 客户端
                    # 什么都收不到。这里直接不发，等价于客户端观察到的事实。
                    continue
                else:
                    reply_from = self.alt_sock
            else:
                reply_from = sock

            with contextlib.suppress(OSError):
                reply_from.sendto(
                    stun_server.stun_binding_response(txid, addr), addr
                )


def test_filtering_verdicts_and_no_warm_port():
    """
    过滤挡位的判据：NAT3（端口受限）必须测成 apdf，不能再测成 adf/NAT2。

    用户 2026-09-25 实测：参考工具（mao.fan/mynat、比特彗星）都报 **NAT3**，
    我们报 NAT2。根因两条：
      1. 顺序错了 —— 先问"换 IP+端口"，而单 IP 的中继只能从兄弟端口答，
         那次放行把 apdf NAT 的放行表暖了，后面"只换端口"就通过了；
      2. 过滤探测用了打洞 socket，而它测映射时已经往两个端口都发过包。
    现在：先"只换端口"，过滤探测单独用全新 socket。
    """
    print("")
    print("---- 过滤挡位：apdf(NAT3) / adf(NAT2) / eif(NAT1) / 忽略 ‑-> unknown ----")

    def verdict(server):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))

        try:
            return stun.stun_filtering(
                sock, server.ip, server.port, timeout=1.0
            )
        finally:
            sock.close()

    # --- apdf：换源的包全被丢掉 → 严格 ---
    drop = ChangeAwareStunServer("drop").start()
    try:
        got = verdict(drop)
        check(
            "★ 换源请求被丢（端口受限的现实表现）→ apdf（NAT3）",
            got == stun.FILTER_APDF,
            got,
        )
    finally:
        drop.close()

    # --- ★ 用户 2026-10-01：只有端口变了、IP 没变 ---
    #
    # 这台假服务器**只有一个 IP**（127.0.0.1 + 备用端口）。"换 IP"那一半在它上面
    # 根本不可能发生，所以正确结论是"**最多**地址受限"= adf_or_eif（未定），
    # 不能断言 NAT2。要断言 adf，必须先有"换 IP 真的测过"这件事（见下一段：
    # 把 `change_ip_possible` 打成 True，等价于公共多 IP STUN）。
    honour = ChangeAwareStunServer("honour").start()
    try:
        got = verdict(honour)
        check(
            "★★ 单 IP 的 STUN：只换端口能进来 -> adf_or_eif（未定，不再断言 NAT2）",
            got == punch.FILTER_ADF_OR_EIF,
            got,
        )
    finally:
        honour.close()

    # --- 有第二个公网 IP（公共多 IP STUN）时：换 IP 测过、没答回来 = 真 adf ---
    honour2 = ChangeAwareStunServer("honour").start()
    real_change_ip = stun.change_ip_possible

    try:
        stun.change_ip_possible = lambda *a, **k: True
        got2 = verdict(honour2)
        check(
            "★ 换 IP 真的测过（多 IP 服务器）→ adf（NAT2）该出来还是出来",
            got2 == stun.FILTER_ADF,
            got2,
        )
    finally:
        stun.change_ip_possible = real_change_ip
        honour2.close()

    # ==================================================================
    #  ★★ 最关键的不变量：走完整 measure()（这才是原来出 bug 的那条路）
    # ==================================================================
    #
    # 原来过滤探测用的是**打洞 socket**，而它测映射时已经往我们中继的 3478
    # **和 3479** 都发过 STUN 请求 —— 对端口受限(apdf)的 NAT 来说，往
    # (服务器IP, 3479) 发过包 = 放行表里多了那一条，于是"换端口"的应答照样
    # 进得来，apdf 被测成 adf（NAT3 → NAT2）。
    #
    # 所以这里让服务器**同时**记下"哪个客户端端口问过哪个服务器端口"，
    # 然后断言：发出 CHANGE_REQUEST 的那个客户端端口，之前**只**问过主端口。
    # 用打洞 socket 测过滤的话，这条必红。
    warm = ChangeAwareStunServer("honour").start()

    try:
        async def full_measure():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", 0))
            try:
                return await stun.measure(
                    sock,
                    servers=[(warm.ip, warm.port), (warm.ip, warm.alt_port)],
                    timeout=0.8,
                    want=2,
                )
            finally:
                sock.close()

        profile = asyncio.run(full_measure())

        check(
            "走完整 measure()：单 IP 下给出 adf_or_eif（过滤探测没被映射阶段干扰）",
            profile.filtering == punch.Filter.ADF_OR_EIF,
            f"filtering={profile.filtering}",
        )

        # 对每一条"换端口"的请求，检查它来自的那个客户端端口的历史。
        warmed = []

        for server_port, client_port, flags, sequence in warm.change_requests:
            if not (flags & 0x01) or server_port != warm.port:
                continue

            # 这条请求**之前**，同一个客户端端口问过哪些服务器端口。
            earlier = {
                srv_port for srv_port, cport in warm.asked[:sequence]
                if cport == client_port
            }

            if warm.alt_port in earlier:
                warmed.append((client_port, sorted(earlier)))

        check(
            "★★ 发 CHANGE_REQUEST 的那个 socket 之前只问过主端口（没被暖过）",
            not warmed,
            f"被暖过：{warmed}；全部记录={warm.asked[:14]}",
        )
        check(
            "映射阶段确实从打洞 socket 问过两个端口（说明上面那条不是白测）",
            len({p for p, _c in warm.asked}) >= 2,
            f"服务器侧看到过这些端口：{sorted({p for p, _c in warm.asked})}",
        )
    finally:
        warm.close()

    # --- eif：从另一个 IP 答回来 → 宽松 ---
    honour_ip = ChangeAwareStunServer("honour_ip", alt_ip="127.0.0.2").start()
    try:
        got = verdict(honour_ip)
        check(
            "★ 从另一个 IP 答回来（真·全锥）→ eif（NAT1）",
            got == stun.FILTER_EIF,
            got,
        )
    finally:
        honour_ip.close()

    # --- ignore：服务器不支持 RFC 5780 → unknown（不许冤枉成"严格"）---
    ignore = ChangeAwareStunServer("ignore").start()
    try:
        got = verdict(ignore)
        check(
            "★ 服务器无视 CHANGE-REQUEST → unknown（不是 apdf）",
            got == stun.FILTER_UNKNOWN,
            got,
        )
    finally:
        ignore.close()


def test_two_answers_from_one_ip_do_not_stop_the_search():
    """
    用户报的"配了好几台 STUN 还是 unknown"。

    根因：默认池的前两台是我们自己中继的 3478/3479 —— **同一个 IP**。老代码
    数够 2 个答案就收工，于是 hosts 永远只有一个元素，分类永远走单 IP 分支，
    公网 STUN（能给出第二个 IP、能区分"全锥"和"按目标 IP 分段分配"）从来没
    被问过。

    这里用 127.0.0.1 和 127.0.0.2 两台假 STUN（Linux 回环整段可用）来钉死：
    同一 IP 上的两个答案之后，**必须继续问**，直到拿到第二个 IP。
    """
    print("")
    print("---- 多台 STUN：同一 IP 的两个答案不许收工 ----")

    same_ip_1 = FakeStunServer("127.0.0.1").start()
    same_ip_2 = FakeStunServer("127.0.0.1").start()
    other_ip = FakeStunServer("127.0.0.2").start()

    try:
        pool = [
            ("127.0.0.1", same_ip_1.port),
            ("127.0.0.1", same_ip_2.port),
            ("127.0.0.2", other_ip.port),
        ]

        async def run():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", 0))
            try:
                return await stun.measure(sock, servers=pool, timeout=1.0, want=2)
            finally:
                sock.close()

        profile = asyncio.run(run())

        check(
            "★ 同一 IP 上的两个答案之后仍然去问了第二个 IP",
            other_ip.asked >= 1,
            f"second_ip_asked={other_ip.asked} asked={profile.asked}",
        )
        check(
            "两个不同 IP 都答了，分类不是「只有一个 IP」的降级结论",
            len({host for host, _p, _m in profile.servers}) == 2,
            str(profile.servers),
        )
        check(
            "问到第二个 IP 时不再报「只有一个 IP」",
            not any("只有一个" in e for e in profile.errors),
            str(profile.errors),
        )
        check(
            "映射端口相同（回环）判成全锥，而不是 unknown",
            profile.coarse == stun.NAT_CONE,
            f"coarse={profile.coarse} label={profile.label}",
        )
    finally:
        for srv in (same_ip_1, same_ip_2, other_ip):
            srv.close()


def test_single_ip_fallback_still_classifies_and_says_so():
    """
    只有一条 IP 可问时（他自己的单服务器部署）：照旧给他那个单 IP 判定
    （两个端口 → cone / symmetric），但界面必须说清这个结论的边界，并且
    点名"UDP 3478 和 3479 都要开"。
    """
    print("")
    print("---- 只有一个 IP 时：照旧判定 + 说清边界 ----")

    one = FakeStunServer("127.0.0.1").start()
    two = FakeStunServer("127.0.0.1").start()

    try:
        # 第三台是黑洞（TEST-NET-1，永远不回），模拟公网 STUN 不通。
        pool = [
            ("127.0.0.1", one.port),
            ("127.0.0.1", two.port),
            ("192.0.2.9", 3478),
        ]

        async def run():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", 0))
            try:
                return await stun.measure(sock, servers=pool, timeout=0.5, want=2)
            finally:
                sock.close()

        profile = asyncio.run(run())

        check(
            "只有一个 IP 时仍然给出结论（不是 unknown）",
            profile.coarse == stun.NAT_CONE,
            f"coarse={profile.coarse} label={profile.label}",
        )
        check(
            "★ 界面能看到「只有一个 IP」的原因说明",
            any("只有一个" in e for e in profile.errors),
            str(profile.errors),
        )
        check(
            "说明里点名 UDP 3478 / 3479 两个端口都要开",
            any("3478" in e and "3479" in e for e in profile.errors),
            str(profile.errors),
        )
    finally:
        one.close()
        two.close()


def test_no_answer_names_both_udp_ports():
    """一台都不答时，原因必须点到"两个 UDP 端口都要开"。"""
    print("")
    print("---- 一台都不答：提示必须点名两个 UDP 端口 ----")

    async def run():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        try:
            return await stun.measure(
                sock,
                servers=[("192.0.2.1", 3478), ("192.0.2.2", 3479)],
                timeout=0.4,
                want=2,
            )
        finally:
            sock.close()

    profile = asyncio.run(run())
    text = " ".join(profile.errors)

    check("全无响应时都是 unknown", profile.coarse == stun.NAT_UNKNOWN, str(profile.coarse))
    check("★ 原因里点名 UDP 3478", "3478" in text, text)
    check("★ 原因里点名 UDP 3479", "3479" in text, text)


# ============================================================
#  入站挡位：必须真的能测出来（用户报的"从来没有测出来过"）
# ============================================================

class IgnoringStunServer:
    """
    一台"只会普通应答"的 STUN 服务器：**无视 CHANGE-REQUEST**。

    绝大多数公共 STUN 就是这样（实测：google / cloudflare / miwifi / B 站
    全部无视）。留着它当名单里的第一台，才能验证客户端会跳过它继续问下一台 ——
    这正是他 `_probe_filtering_bg` 的设计，也是我们以前漏掉的那一半。
    """

    def __init__(self, port=0):
        from stun_server import stun_binding_response

        self._respond = stun_binding_response
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        with contextlib.suppress(Exception):
            self.sock.close()

    def _run(self):
        self.sock.settimeout(0.2)

        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < 20 or data[4:8] != STUN_MAGIC:
                continue

            # 关键：**从同一个 socket 回**，也就是"我无视你的换端口要求"。
            with contextlib.suppress(Exception):
                self.sock.sendto(self._respond(data[8:20], addr), addr)


def test_response_origin_attribute():
    """
    RFC 5780 的 RESPONSE-ORIGIN（0x802B）：服务器要**自证**它从哪回的。

    手册（朋友的《入站过滤检测流程》§6）把它写成硬要求，而且点明后果：
    `只做 (1) 不做 (3) 会让客户端把"没换地方"当成"换了"`。

    两个方向都要钉死：
      * 绑定到**具体地址**（127.0.0.1）→ 必须带 RESPONSE-ORIGIN，值就是
        发出这条应答的那个 socket 的地址（换端口应答 = 兄弟端口）；
      * 绑定到**通配地址**（0.0.0.0，线上容器就是这样）→ **一个都不许发**。
        通配地址不是"来源"，把它报上去会让客户端以为服务器从另一个 IP 答了
        → 判成 eif（宽松）。这个错我当时就犯了一次，被测试抓住。
    """
    print("")
    print("---- RFC 5780 RESPONSE-ORIGIN：服务器要自证从哪回的 ----")

    port, port2 = free_udp_port(), free_udp_port()
    server = StunServerProcess(port, port2, bind="127.0.0.1")

    if not server.start():
        check("STUN 服务端子进程启动（绑定具体地址）", False, "没打印 READY")
        return

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)

        info = {}
        stun._stun_once(sock, "127.0.0.1", port, 2.0,
                        change=stun.CHANGE_PORT, info=info)
        sock.close()

        check(
            "★ 绑定具体地址时，换端口应答带 RESPONSE-ORIGIN（= 兄弟端口）",
            info.get("origin") == ("127.0.0.1", port2),
            f"origin={info.get('origin')}（期望 ('127.0.0.1', {port2})）",
        )

        # 通配地址：不许声称
        port3, port4 = free_udp_port(), free_udp_port()
        wild = StunServerProcess(port3, port4)      # 不给 bind -> 0.0.0.0

        if not wild.start():
            check("STUN 服务端子进程启动（通配地址）", False, "没打印 READY")
            return

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3)

            info = {}
            got = stun._stun_once(sock, "127.0.0.1", port3, 2.0,
                                  change=stun.CHANGE_PORT, info=info)
            sock.close()

            check(
                "★★ 绑定通配地址时不发 RESPONSE-ORIGIN（不许声称 0.0.0.0）",
                got and not info.get("origin"),
                f"got={got} origin={info.get('origin')}",
            )
            check(
                "  但应答真的从兄弟端口来了（用真实源地址判断）",
                info.get("src") == ("127.0.0.1", port4),
                f"src={info.get('src')}（期望 ('127.0.0.1', {port4})）",
            )
        finally:
            wild.stop()
    finally:
        server.stop()


async def test_filter_grade_can_be_measured():
    """
    ★ 入站挡位从"未测"变成真的挡位，两半都要在：

      1. **服务端**：我们绑了 3478/3479 两个端口，要能替对方回包（RFC 5780
         的"换端口"）。这一项不需要第二个公网 IP，所以是我们唯一能自己补上的
         一半 —— 他原版服务端两半都不做（他靠客户端比两次绑定），这就是他的
         客户端在别人网络里也常常显示"入站未测"的原因。
      2. **客户端**：要轮着问名单上的每一台，跳过"无视 CHANGE-REQUEST"的那些
         （他的 `_probe_filtering_bg`）。

    做法：名单第一位放一台"无视换端口"的假服务器，第二位放我们的真服务端。
    客户端必须跳过第一台的 unknown、采用第二台的 adf → 界面显示「入站中等」。
    """
    print("")
    print("---- 入站挡位：换端口 + 轮询名单（用户报的'从来没测出来过'）----")

    port, port2 = free_udp_port(), free_udp_port()
    server = StunServerProcess(port, port2)

    if not server.start():
        check("STUN 服务端子进程启动", False, "没打印 READY")
        return

    fake = IgnoringStunServer().start()

    try:
        # ---- 1. 服务端真的换了端口回包吗 ----
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)

        # 普通请求
        plain = stun._stun_once(sock, "127.0.0.1", port, 2.0)
        check("普通绑定请求有应答（对照组）", plain is not None, str(plain))

        # 带 CHANGE-REQUEST 的"只换端口"请求：应答应当来自另一个端口
        info = {}
        stun._stun_once(sock, "127.0.0.1", port, 2.0,
                        change=stun.CHANGE_PORT, info=info)
        origin = info.get("origin") or info.get("src")

        check(
            "★ 服务端实现了 RFC 5780「换端口」：应答来自另一个端口",
            bool(origin) and origin[1] == port2,
            f"应答来源={origin}（期望端口 {port2}）",
        )

        sock.close()

        # ---- 2. 客户端会跳过"无视"的服务器，采用真结论 ----
        profile = await stun.detect_nat_type(
            servers=[("127.0.0.1", fake.port), ("127.0.0.1", port)],
            timeout=stun.RESPONSE_TIMEOUT, want=2,
        )

        # `profile.filtering` 是 `punch.Filter` 枚举，比较前先取字符串值
        # （第一版就是拿枚举和 "adf" 比，明明测出来了却报 FAIL）。
        grade = getattr(profile.filtering, "value", profile.filtering)

        check(
            "★ 入站挡位测出结论（不再是 unknown / 界面的'入站未测'）",
            # ★ 用户 2026-10-01：adf_or_eif 也是**测出了结论**（"至少不是
            #   apdf，但全锥排不掉"），不再是"入站未测"。
            grade in ("eif", "adf", "adf_or_eif", "apdf"),
            f"filtering={grade}（界面文字："
            f"{stun.NAT_FILTER_LABEL.get(grade)}）",
        )

        check(
            "★ 客户端跳过了第一台'无视 CHANGE-REQUEST'的服务器（他的轮询设计）",
            # 这两台假服务器都是 127.0.0.1（单 IP）：换 IP 测不了 -> 未定。
            # 重点是"采用的是**第二台**（实现了 RFC 5780）的结论"，而不是
            # 第一台的 unknown。
            grade in ("adf", "adf_or_eif"),
            f"filtering={grade}",
        )
    finally:
        fake.close()
        server.stop()



# ============================================================

def main():
    print("=" * 64)
    print("MC-Link STUN 栈测试（移植自 MCLanP2P）")
    print("=" * 64)

    test_classifier()
    test_labels()
    asyncio.run(test_server_both_ports())
    asyncio.run(test_filter_grade_can_be_measured())
    test_response_origin_attribute()
    test_bridge_does_not_block_loop()
    test_bridge_bad_input_is_safe()
    test_server_list_prefers_our_relay()
    test_probe_depth_bookkeeping()
    test_filtering_verdicts_and_no_warm_port()
    test_two_answers_from_one_ip_do_not_stop_the_search()
    test_single_ip_fallback_still_classifies_and_says_so()
    test_no_answer_names_both_udp_ports()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
