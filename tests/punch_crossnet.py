#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CraftLink P2P 打洞实验工具（跨真实网络，两端各跑一份）

============================================================================
 它是干什么的
============================================================================

把"能不能打成洞"从讨论变成数据。两端各跑一次，它会：

  1. 用 **你自己的 VPS**（19100/udp 的 STUN）测出本机的公网 endpoint；
  2. 用 **你自己的 VPS**（19100/tcp 的免登录 HOST_TUNNEL 隧道）和对端
     交换候选地址 —— 不需要登录、不需要改服务端；
  3. 双方**同时**朝对方的公网 endpoint 发包，同时监听；
  4. 报告：谁先收到谁、收到多少、以及**打完洞之后本地映射有没有变**。

============================================================================
 为什么第 4 步是关键
============================================================================

对称 NAT 下，我向 STUN 服务器报告的公网端口，**只对那台服务器有效**。
换一个目的地（= 对端的公网地址），NAT 会给一个**新端口**。所以对端按
信令里那个端口打过来，打在的是没人监听的映射上。

这个工具会把这件事测出来，而不是靠推理：它在"刚问完 STUN"和"开始打洞
之后"都测一次本机映射，把前后两个端口都打出来。

============================================================================
 用法（两端各一条，room 要一样）
============================================================================

    # A 端
    python tests/punch_crossnet.py --room cltest01

    # B 端（另一台机器/另一个网络）
    python tests/punch_crossnet.py --room cltest01

两端顺序无所谓，先到的那端会等。

常用参数：
    --room NAME     配对用的房间名（两端必须一致）
    --seconds N     打洞持续多久（默认 20）
    --interval MS   发包间隔毫秒（默认 200）
    --relay HOST:PORT   中继/STUN 地址（默认就是项目 VPS）
    --quiet         只打印结论

只依赖标准库。
"""

import argparse
import asyncio
import contextlib
import os
import random
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import stun  # noqa: E402

#: 打洞包的魔数 + 结构。故意做小：一个包要能被对方一眼认出，
#: 同时带够信息区分"这是谁的包"。
MAGIC = b"CLP1"
T_PROBE = 1
T_ANSWER = 2
HEADER = struct.Struct("!4sB3s")      # magic, type, 3 字节乱码
PROBE_LEN = 24                        # 对齐 EasyTier 建议的最小打洞包长


def build_packet(kind, token):
    return HEADER.pack(MAGIC, kind, token) + os.urandom(PROBE_LEN - HEADER.size)


def parse_packet(data):
    if len(data) < HEADER.size:
        return None

    magic, kind, token = HEADER.unpack_from(data, 0)

    if magic != MAGIC:
        return None

    return kind, token


# ============================================================
# 信令：借用中继的免登录 HOST_TUNNEL 隧道
# ============================================================

class TunnelSignaller:
    """
    A byte pipe to the peer, multiplexed through the relay's tunnel pairing.

    WHY THIS AND NOT A NEW SERVER:

      * `PLAYER` requires a login (`ERROR AUTH_REQUIRED` on this deployment),
        so it is not usable for a test that must run unattended;
      * `HOST_TUNNEL <room>` is answered with `WAIT` immediately and needs no
        account, and two of them pair with each other in FIFO order;
      * the relay only ever relays bytes here, so nothing about this test can
        perturb the frozen data path.

    The pairing order decides who waits: the first connection sits in
    `WAIT`, the second one triggers `START <id>` on the first. Both sides
    discover their situation by what they read, so either side can be started
    first.
    """

    def __init__(self, host, port, room, log=print):
        self.host = host
        self.port = port
        self.room = room
        self.log = log
        self.reader = None
        self.writer = None
        #: True when we were the one that had to wait (i.e. we are "A").
        self.waited = False

    async def connect(self, timeout=30.0):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=timeout
        )

        self.writer.write(f"HOST_TUNNEL {self.room}\n".encode())
        await self.writer.drain()

        first = await asyncio.wait_for(self.reader.readline(), timeout=timeout)

        if not first:
            raise ConnectionError("中继没有回应，连接被关闭")

        if first.startswith(b"WAIT"):
            self.waited = True
            self.log("  [信令] 已连上中继，排在第一，等对端进来 ...")
            start = await asyncio.wait_for(
                self.reader.readline(), timeout=timeout
            )
            self.log(f"  [信令] {start.strip().decode(errors='replace')}")

            if not start.startswith(b"START"):
                raise ConnectionError(f"没有配对上：{start!r}")

        elif first.startswith(b"START"):
            # Very unlikely with HOST_TUNNEL, but harmless to handle.
            self.log(f"  [信令] {first.strip().decode(errors='replace')}")

        elif first.startswith(b"ERROR"):
            # A relay whose account requirement also covers tunnels.
            raise ConnectionError(
                f"中继拒绝握手：{first.strip().decode(errors='replace')}"
            )

        else:
            raise ConnectionError(f"无法识别的中继回应：{first!r}")

        return self

    async def send_line(self, text, timeout=20.0):
        self.writer.write((text + "\n").encode())
        await self.writer.drain()

    async def read_line(self, timeout=20.0):
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        return line.decode(errors="replace").strip()

    async def close(self):
        if self.writer is not None:
            with contextlib.suppress(Exception):
                self.writer.close()


# ============================================================
# UDP 探测
# ============================================================

async def resolve(host, port):
    """Resolve with one retry; mobile links wobble on DNS."""
    loop = asyncio.get_running_loop()

    for attempt in range(2):
        try:
            return await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        except (OSError, socket.gaierror):
            if attempt == 0:
                await asyncio.sleep(0.8)

    return None


async def discover_public(sock, host, port, timeout=5.0):
    """Ask the VPS's own STUN responder what the world sees for `sock`."""
    infos = await resolve(host, port)

    if not infos:
        return None, None

    picked = stun.pick_ipv4(infos)

    if picked is None:
        return None, None

    _family, sockaddr = picked

    mapped, source, rtt = await stun._stun_exchange(
        sock, sockaddr[0], sockaddr[1], False, False, timeout
    )

    return mapped, rtt


class PunchLab:
    def __init__(self, args):
        self.args = args
        self.log = (lambda *a: None) if args.quiet else print
        self.port = args.relay_port
        self.host = args.relay_host
        self.token = struct.pack("!3s", os.urandom(3))
        self.stats = {
            "sent": 0,
            "got_probe": 0,
            "got_answer": 0,
            "peers": set(),
        }

    # --------------------------------------------------------

    def make_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(("0.0.0.0", 0))
        return sock

    def describe(self, sock):
        local = sock.getsockname()
        return f"{local[0]}:{local[1]}"

    # --------------------------------------------------------

    async def run(self):
        args = self.args

        self.log("")
        self.log("=" * 72)
        self.log(" CraftLink P2P 打洞实验")
        self.log("=" * 72)
        self.log(f"  本机标记 : {self.token.hex()}")
        self.log(f"  房间     : {args.room}")
        self.log(f"  中继/STUN: {self.host}:{self.port}")
        self.log(f"  打洞时长 : {args.seconds}s，间隔 {args.interval}ms")
        self.log("")

        # ---- 1. 一个 socket，全程复用（关键：映射绑在本地端口上）----
        sock = self.make_socket()
        self.log(f"  [UDP] 本地 socket {self.describe(sock)}（全程只有这一个）")

        # ---- 2. 问 STUN 拿自己的公网 endpoint ----
        mapped, rtt = await discover_public(sock, self.host, self.port)

        if mapped is None:
            self.log("  [UDP] 拿不到公网映射 —— 本机 UDP 出不去，实验无法继续。")
            sock.close()
            return 2

        self.log(
            f"  [UDP] 问 VPS-STUN 得到公网 endpoint : "
            f"{mapped[0]}:{mapped[1]}（{rtt:.0f} ms）"
        )

        # ---- 3. 用中继隧道交换候选地址 ----
        signal = TunnelSignaller(self.host, self.port, args.room, log=self.log)

        try:
            await signal.connect(timeout=args.wait)

            my_line = f"CAND {mapped[0]} {mapped[1]} {self.token.hex()}"

            if signal.waited:
                # We are "A": we are already paired, so exchange data first
                # and read the peer's afterwards. Writing first avoids both
                # ends blocking on a read.
                await signal.send_line(my_line)
                peer_line = await signal.read_line()
            else:
                peer_line = await signal.read_line()
                await signal.send_line(my_line)

            self.log(f"  [信令] 对端候选: {peer_line}")

            parts = peer_line.split()

            if len(parts) < 4 or parts[0] != "CAND":
                self.log("  [信令] 对端候选格式不对，实验中止。")
                return 2

            peer_addr = (parts[1], int(parts[2]))
            peer_token = parts[3]
        except Exception as e:
            self.log(f"  [信令] 失败：{e}")
            self.log("  [信令] 提示：两端必须用同一个 --room，并且先启动的一端要等。")
            return 2
        finally:
            with contextlib.suppress(Exception):
                await signal.close()

        # ---- 4. 同时打洞 ----
        self.log("")
        self.log(f"  [打洞] 目标 {peer_addr[0]}:{peer_addr[1]}，两端同时开火 ...")
        self.log("")

        started = time.time()
        deadline = started + args.seconds
        interval = max(args.interval, 10) / 1000.0

        first_in = None
        first_out = None

        while time.time() < deadline:
            # 对称 NAT 下的关键点：这个 socket 现在**换目的地了**。
            packet = build_packet(T_PROBE, self.token)

            try:
                await asyncio.get_running_loop().sock_sendto(
                    sock, packet, peer_addr
                )
                self.stats["sent"] += 1

                if first_out is None:
                    first_out = time.time() - started
            except OSError as e:
                self.log(f"  [打洞] 发送失败：{e}")

            # 收包窗口 = 一个发包间隔，尽量不漏
            try:
                data, addr = await asyncio.wait_for(
                    asyncio.get_running_loop().sock_recvfrom(sock, 2048),
                    timeout=interval,
                )
            except (asyncio.TimeoutError, TimeoutError):
                continue
            except OSError:
                continue

            parsed = parse_packet(data)

            if parsed is None:
                continue

            kind, token = parsed

            if token.hex() == peer_token:
                if kind == T_PROBE:
                    self.stats["got_probe"] += 1
                    self.stats["peers"].add(addr)

                    if first_in is None:
                        first_in = time.time() - started
                        self.log(
                            f"  [打洞] ★ 收到对端探测包！来自 {addr[0]}:{addr[1]}"
                            f"，用时 {first_in:.2f}s"
                        )

                    # 回一个 ANSWER，确认"双向都通"
                    with contextlib.suppress(OSError):
                        await asyncio.get_running_loop().sock_sendto(
                            sock, build_packet(T_ANSWER, self.token), addr
                        )
                elif kind == T_ANSWER:
                    self.stats["got_answer"] += 1
                    self.stats["peers"].add(addr)

                    self.log(
                        f"  [打洞] ★ 收到对端回执！来自 {addr[0]}:{addr[1]}"
                        " —— 双向都通了"
                    )

        # ---- 5. 打完之后再测一次映射：它变了吗？----
        self.log("")
        self.log("  [复测] 打洞结束后，再问一次 VPS-STUN ...")

        after, _rtt2 = await discover_public(sock, self.host, self.port)

        sock.close()

        # ---- 6. 结论 ----
        self.log("")
        self.log("=" * 72)
        self.log(" 结果")
        self.log("=" * 72)
        self.log(f"  发包        : {self.stats['sent']}")
        self.log(f"  收到探测包  : {self.stats['got_probe']}")
        self.log(f"  收到回执    : {self.stats['got_answer']}")

        if self.stats["peers"]:
            for peer in self.stats["peers"]:
                self.log(f"  对端实际来源: {peer[0]}:{peer[1]}")

        self.log(f"  打洞前映射  : {mapped[0]}:{mapped[1]}")
        self.log(f"  打洞后映射  : {after[0] + ':' + str(after[1]) if after else '未知'}")

        if after and after[1] != mapped[1]:
            self.log("  ⇒ 换目的地之后公网端口变了（对称 NAT 的直接证据）")
        elif after:
            self.log("  ⇒ 公网端口没变（这一步不能单独证明是锥形，见下方说明）")

        self.log("")

        if self.stats["got_answer"]:
            self.log("  ★★ 直连成功：双向都能收到对方的包。")
            self.log("     这条路可以承载 Minecraft 流量（还需要自研可靠流兜底丢包）。")
        elif self.stats["got_probe"]:
            self.log("  ★ 半通：收到了对端的包，但没收到回执（可能只单向通）。")
        else:
            self.log("  ✗ 没有打成：一个对端的包都没收到。")

        self.log("")
        self.log("  说明：两端都在对称 NAT（NAT4）后面时，这个结果**是预期内的**，")
        self.log("        原因是信令里交换的端口只对 STUN 服务器有效，对端打过来时")
        self.log("        落在另一个映射上。要区分「真的没救」和「端口可预测」需要")
        self.log("        再做端口预测实验，见报告。")
        self.log("=" * 72)

        return 0 if self.stats["got_answer"] else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="CraftLink P2P 打洞实验（两端各跑一份）"
    )
    parser.add_argument("--room", required=True, help="配对房间名，两端必须一致")
    parser.add_argument("--relay", default="www.example.com:19100",
                        help="中继/STUN 地址 host:port")
    parser.add_argument("--seconds", type=int, default=20, help="打洞持续秒数")
    parser.add_argument("--interval", type=int, default=200, help="发包间隔毫秒")
    parser.add_argument("--wait", type=float, default=60.0,
                        help="等对端出现的秒数")
    parser.add_argument("--quiet", action="store_true", help="只打印结论")

    args = parser.parse_args(argv)

    host, _, port = args.relay.partition(":")

    args.relay_host = host
    args.relay_port = int(port or "19100")

    return args


def main():
    args = parse_args()
    lab = PunchLab(args)

    try:
        return asyncio.run(lab.run())
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
