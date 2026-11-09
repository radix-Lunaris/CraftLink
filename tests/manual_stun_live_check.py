"""
One-off validation: talk to real STUN servers over the network.

This is NOT part of the test suite (the suite must stay offline and
deterministic). Run it by hand:

    python tests/manual_stun_live_check.py

After the STUN stack was replaced with MCLanP2P's (see client/mclink/core/
stun.py), this answers one question the offline suites cannot: **does the
ported client actually get a public endpoint from the real internet?**

Everything offline is tested against hand-made responses, and a wrong
XOR-MAPPED-ADDRESS decoder would still pass those. Talking to the real thing
at least once is what rules that out.

It also prints the full NAT verdict, so it doubles as a "what is my network
doing right now" tool when a punch does not work on a real machine.
"""

import asyncio
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import stun  # noqa: E402


async def quick_reachability():
    """One plain binding request per public server, to see who answers."""
    print("")
    print("---- 公共 STUN 可达性（他的服务器列表）----")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))

    try:
        # `_stun_once` is his synchronous code: it needs a blocking socket
        # and it will restore nothing on its own.
        previous = sock.getblocking()
        sock.setblocking(True)

        try:
            for host, port in stun.STUN_SERVERS:
                info = {}

                try:
                    got = stun._stun_once(sock, host, port, 2.5, hub=None,
                                          info=info)
                except Exception as e:  # pragma: no cover - network
                    got = None
                    info["err"] = str(e)

                if got:
                    print(f"  {host:26} -> {got[0]}:{got[1]}")
                else:
                    print(f"  {host:26} -> 无响应 {info.get('err', '')}")
        finally:
            sock.setblocking(previous)
    finally:
        sock.close()


async def main():
    print("=" * 68)
    print("STUN live check -- 真实网络，真实服务器")
    print("=" * 68)
    print(f"  时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")

    await quick_reachability()

    print("")
    print("---- 完整 NAT 判定（他的探测栈）----")

    started = time.monotonic()
    profile = await stun.detect_nat_type(timeout=3.0)
    elapsed = time.monotonic() - started

    print("")
    print(f"  结论     : {profile.label}")
    print(f"  粗分类   : {profile.coarse}")
    print(f"  子类型   : {profile.nat_type.value}")
    print(f"  过滤     : {profile.filtering.value}")
    print("  公网映射 : "
          + (f"{profile.public[0]}:{profile.public[1]}" if profile.public
             else "未知"))
    print(f"  步长     : {profile.step} | 按目标 IP 分池: {profile.per_ip_pool}")
    print(f"  采样端口 : {profile.mapped_ports}")
    print(f"  耗时     : {elapsed:.1f}s")

    for error in profile.errors:
        print(f"  [!] {error}")

    print("")
    print("=" * 68)

    if profile.public is None:
        print(" 拿不到公网映射：UDP 出不去，或者所有 STUN 都不可达。")
        print(" 这不是打洞代码的问题 —— 先解决 UDP 连通性。")
        return 1

    print(" 公网映射已拿到，线上探测链路正常。")
    print("=" * 68)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
