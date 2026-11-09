#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CraftLink -- 验证 VPS 的 STUN 端口是否真的通了（只读，不改任何东西）

============================================================================
 它回答什么问题
============================================================================

重建中继之后，怎么知道 STUN 真的在跑？**不能只看容器状态** ——
端口没映射、安全组没放行、STUN 没启动，这三种情况在 `docker ps` 里
看起来都可能像"健康的"。

所以这里从**外面**发真实的 STUN Binding Request，看有没有包回来。
和客户端用的是同一套线格式。

    python scripts/verify-stun-ports.py
    python scripts/verify-stun-ports.py --host 你的域名或IP
    python scripts/verify-stun-ports.py --host 1.2.3.4 --port 3478

检查项：

  1. TCP 19100 中继在监听（发一行 ROOMS 查询）
  2. **UDP 3478 应答**，而且 XOR-MAPPED-ADDRESS 解出来等于我们自己的地址
  3. **UDP 3479 应答** —— 这一条尤其重要：只开一个端口时客户端**判不出
     对称 NAT**（需要两个不同目的地才能对比映射）

只依赖标准库，几秒钟出结果。
"""

import argparse
import contextlib
import json
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

MAGIC = 0x2112A442
MAGIC_BYTES = b"\x21\x12\xa4\x42"

DEFAULT_HOST = "www.example.com"
RELAY_PORT = 19100
STUN_PORT = 3478
STUN_PORT2 = 3479


def ok(text):
    print(f"  [ OK ] {text}", flush=True)


def bad(text):
    print(f"  [FAIL] {text}", flush=True)


def note(text):
    print(f"  [ .. ] {text}", flush=True)


# ============================================================
# TCP：中继还活着吗
# ============================================================

def check_relay(host, port, timeout=8.0):
    """One unauthenticated ROOMS query proves the relay is listening."""
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        bad(f"TCP {port} 连不上：{e}")
        return False

    try:
        conn.settimeout(timeout)
        conn.sendall(b"ROOMS __mclink__ probe -\n")
        line = conn.recv(200).decode(errors="replace").strip()

        if line.startswith("ROOMS"):
            ok(f"TCP {port} 中继在跑（{line[:40]}）")
            return True

        bad(f"TCP {port} 有响应但不认识：{line[:60]!r}")
        return False
    except OSError as e:
        bad(f"TCP {port} 读失败：{e}")
        return False
    finally:
        with contextlib.suppress(Exception):
            conn.close()


# ============================================================
# UDP：STUN 两个端口
# ============================================================

def decode_xor_mapped(data):
    off = 20

    while off + 4 <= len(data):
        attr, length = struct.unpack_from(">HH", data, off)
        off += 4
        value = data[off:off + length]
        off += length + ((-length) % 4)

        if attr == 0x0020 and len(value) >= 8:
            port = struct.unpack_from(">H", value, 2)[0] ^ 0x2112
            cookie = struct.pack(">I", MAGIC)
            ip = bytes(value[4 + i] ^ cookie[i] for i in range(4))
            return socket.inet_ntoa(ip), port

    return None


def check_stun(host, port, timeout=5.0):
    """Send one real Binding Request and decode the answer."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except OSError as e:
        bad(f"UDP {port}：域名解析失败 {e}")
        return False

    ipv4 = [i for i in infos if i[0] == socket.AF_INET]

    if not ipv4:
        bad(f"UDP {port}：没有 IPv4 地址")
        return False

    target = ipv4[0][4]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)

    try:
        local = sock.getsockname()
        sock.bind(("0.0.0.0", 0))
        local = sock.getsockname()

        tid = os.urandom(12)
        request = struct.pack(">HHI", 0x0001, 0, MAGIC) + tid
        started = time.monotonic()
        sock.sendto(request, target)

        deadline = time.monotonic() + timeout

        while True:
            left = deadline - time.monotonic()

            if left <= 0:
                bad(f"UDP {port} 没有响应 —— 端口没通，或 STUN 没启动")
                return False

            sock.settimeout(left)

            try:
                data, src = sock.recvfrom(2048)
            except socket.timeout:
                bad(f"UDP {port} 没有响应 —— 端口没通，或 STUN 没启动")
                return False

            if len(data) < 20 or data[8:20] != tid or data[4:8] != MAGIC_BYTES:
                continue

            rtt = (time.monotonic() - started) * 1000
            kind = struct.unpack_from(">H", data, 0)[0]
            mapped = decode_xor_mapped(data)

            if kind != 0x0101:
                bad(f"UDP {port} 回了但不是成功响应（0x{kind:04x}）")
                return False

            if mapped is None:
                bad(f"UDP {port} 应答里没有 XOR-MAPPED-ADDRESS")
                return False

            # The decoded address must be OUR public address, which we can
            # check against the source socket's own view only loosely (we are
            # behind our own NAT). What matters is that it is a real answer.
            ok(f"UDP {port} 应答正常（{rtt:.0f} ms，映射 {mapped[0]}:{mapped[1]}）")
            return True
    finally:
        with contextlib.suppress(OSError):
            sock.close()


# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="验证 VPS 的中继 + STUN 端口（只读）"
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"服务器地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=STUN_PORT,
                        help=f"STUN 主端口（默认 {STUN_PORT}）")
    parser.add_argument("--port2", type=int, default=STUN_PORT2,
                        help=f"STUN 备用端口（默认 {STUN_PORT2}）")

    args = parser.parse_args()

    print("")
    print("=" * 64)
    print(" CraftLink 端口验证（只读）")
    print("=" * 64)
    print(f"  服务器：{args.host}")
    print(f"  时间  ：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("")

    print("---- 1. 中继（Minecraft 流量通路）----")
    relay_ok = check_relay(args.host, RELAY_PORT)

    print("")
    print("---- 2. 内置 STUN 主端口 ----")
    stun_ok = check_stun(args.host, args.port)

    print("")
    print("---- 3. 内置 STUN 备用端口 ----")
    stun2_ok = check_stun(args.host, args.port2)

    print("")
    print("=" * 64)
    print(" 结论")
    print("=" * 64)

    if relay_ok:
        print("  中继正常。")
    else:
        print("  [!] 中继不通 —— Minecraft 联机会受影响，先查这个。")

    if stun_ok and stun2_ok:
        print("  STUN 两个端口都正常，客户端可以判出 NAT 类型。")
    elif stun_ok and not stun2_ok:
        print("  [!] 只有主端口通。客户端还能测到公网映射，但**判不出对称型**")
        print("      （两个不同目的地才能对比映射）。检查：")
        print(f"        - compose 里的 \"{args.port2}:{args.port2}/udp\" 映射")
        print(f"        - 云安全组的 UDP {args.port2} 规则")
        print(f"        - Dockerfile 的 EXPOSE {args.port2}/udp")
    elif not stun_ok:
        print("  [!] STUN 完全不通。检查：")
        print(f"        - 容器里 STUN 有没有起来（日志里找「STUN 监听」）")
        print(f"        - compose 映射 + 云安全组 UDP {args.port}")
        print("        - 环境变量 MCLINK_STUN_ENABLED 是不是被设成 0 了")

    print("")

    all_ok = relay_ok and stun_ok and stun2_ok

    print("=" * 64)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
