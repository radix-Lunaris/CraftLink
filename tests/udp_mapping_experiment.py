"""
核心实验：同一个本地 UDP socket，发给不同目标，公网 source IP:port 会不会变？

============================================================================
 为什么这是整个打洞方案的地基
============================================================================

打洞的信令是这样的：我对信令服务器说"我看到的自己是 A"，你说"我看到的自己
是 B"，然后我们互相往 A/B 发。

这套推理成立的前提是：

    **NAT 映射只跟本地端口有关，跟目的地无关**（锥形 NAT）

如果 NAT 是"每个 (本地端口, 目的地) 一个映射"（对称 NAT），那么：

  * 我向信令服务器报告的公网端口，**只对那台信令服务器有效**；
  * 我换上"对端 peer 的公网地址"这个新目的地时，NAT 会**分配一个新端口**；
  * 于是 peer 按信令里那个端口打过来，打在一个**根本没人监听**的映射上。

所以这个实验不是"顺便看看"，它直接决定方案能不能成立。

============================================================================
 怎么做才叫可信
============================================================================

1. **同一个 socket** 发所有探测。NAT 映射是绑在本地端口上的，换个 socket
   等于换一个被测量的对象（见 stun.py 头部第 2 条铁律）。
2. **多组目的地**，至少包含：
      * 用户的 VPS（www.example.com:19100）—— 真实信令服务器
      * 两个公共 STUN —— 不同 IP、不同运营商
3. **交叉验证**：先问 VPS 拿一次映射，去问别的目标，**再回来问 VPS**。
   如果第二次的答案和第一次不同，说明"中途发给了别人"这件事本身就
   改变了映射 —— 这就是对称 NAT 的直接证据。
4. **对照组**：同一个目标连问两次。如果同一目标两次都不一样，那说明
   NAT 是随机的、连"每个目的地固定"都不成立（更糟）。

Run:
    python tests/udp_mapping_experiment.py
"""

import asyncio
import contextlib
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import stun  # noqa: E402

#: (显示名, host, port)
TARGETS = [
    ("本项目中继/VPS", "www.example.com", 19100),
    ("stun.miwifi.com", "stun.miwifi.com", 3478),
    ("stun.chat.bilibili.com", "stun.chat.bilibili.com", 3478),
    ("stun.hitv.com", "stun.hitv.com", 3478),
]

TIMEOUT = 4.0


async def ask(sock, host, port, label, change_ip=False, change_port=False):
    """
    One Binding Request from the GIVEN socket. Returns a dict of facts.

    Reuses stun._stun_exchange so this measures exactly the code path the
    product uses -- a hand-written probe here could disagree with the client.
    """
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_DGRAM
        )
    except (OSError, socket.gaierror) as e:
        return {"label": label, "error": f"DNS 失败：{e}"}

    if not infos:
        return {"label": label, "error": "DNS 无结果"}

    # Same helper the product uses: stun.hitv.com resolves IPv6 FIRST, and
    # taking infos[0] here is the bug this experiment itself exposed.
    picked = stun.pick_ipv4(infos)

    if picked is None:
        return {"label": label, "error": "DNS 没有 IPv4 地址"}

    _family, sockaddr = picked

    mapped, source, rtt = await stun._stun_exchange(
        sock, sockaddr[0], sockaddr[1], change_ip, change_port, TIMEOUT
    )

    return {
        "label": label,
        "target": f"{sockaddr[0]}:{sockaddr[1]}",
        "mapped": mapped,
        "source": source,
        "rtt": rtt,
        "error": "" if mapped else "无响应",
    }


def show(row, indent="  "):
    if row.get("error"):
        print(f"{indent}{row['label']:24} -- {row['error']}")
        return

    mapped = row["mapped"]
    source = row["source"]
    rtt = row["rtt"] or 0
    print(
        f"{indent}{row['label']:24} 目标 {row['target']:28} "
        f"映射 {mapped[0]}:{mapped[1]:<6} 响应来自 {source[0]}:{source[1]:<6} "
        f"{rtt:.0f} ms"
    )


async def experiment_cross_destination():
    """The one that matters: does the mapping follow the DESTINATION?"""
    print("=" * 78)
    print(" 实验 1：同一个 socket，切换目的地 —— 公网映射会不会变？")
    print("=" * 78)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", 0))
    local_port = sock.getsockname()[1]

    print(f"\n本地 socket: 0.0.0.0:{local_port}（全程只有这一个 socket）\n")

    try:
        results = []

        # ---- 第 1 轮：每个目的地各问一次 ----
        print("---- 第 1 轮：每个目标问一次 ----")

        for label, host, port in TARGETS:
            row = await ask(sock, host, port, label)
            results.append(row)
            show(row)

        by_port = {
            row["mapped"][1]: row["label"]
            for row in results
            if row.get("mapped")
        }

        # ---- 第 2 轮：回到第一个能答的目标，看答案是否变了 ----
        print("\n---- 第 2 轮：再回去问一次第一个目标（交叉验证）----")

        anchor = next(
            (row for row in results if row.get("mapped")), None
        )

        second = None

        if anchor is not None:
            label, host, port = next(
                (t for t in TARGETS if t[0] == anchor["label"]), None
            )
            second = await ask(sock, host, port, label + " (再问)")
            show(second)

        # ---- 结论 ----
        print("\n---- 结论 ----")

        answered = [r for r in results if r.get("mapped")]

        if len(answered) < 2:
            print("  [!] 有效样本不足 2 个，判不出映射是否随目的地变化。")
            return

        ports = sorted({r["mapped"][1] for r in answered})

        print(f"  同一 socket 在不同目的地下报出的公网端口：{ports}")

        if len(ports) == 1:
            print("  ⇒ 映射与目的地无关 = 锥形 NAT，信令里报的端口对任何目标都有效。")
        else:
            print(f"  ⇒ 映射随目的地变化（{len(ports)} 个不同端口）= 对称 NAT。")
            print("     信令里报告的公网端口【只对那台服务器有效】。")
            print("     peer 拿这个端口来打，打的是一个不存在的映射。")

        if second is not None and anchor is not None and second.get("mapped"):
            first_port = anchor["mapped"][1]
            second_port = second["mapped"][1]

            if first_port == second_port:
                print(
                    f"\n  交叉验证：回到同一目标，端口仍然是 {first_port}"
                    " —— 同目标映射稳定（好）。"
                )
            else:
                print(
                    f"\n  交叉验证：同一目标两次不同！{first_port} -> {second_port}"
                    " —— 连「同目标固定」都不成立，映射是随机分配的。"
                )

        if len(ports) > 1:
            print("\n  另外注意：本机真实出口 UDP 是通的（这些回复都收到了），")
            print("  所以「打不通」不能归咎于 UDP 被拦 —— 是映射语义的问题。")
    finally:
        with contextlib.suppress(OSError):
            sock.close()


async def experiment_same_target_stability():
    """
    对照组：连续问同一个目标 N 次。

    A symmetric NAT that keeps the mapping per destination is still workable
    with port prediction if the allocation follows a rule. A NAT that changes
    the port even for the SAME destination on the SAME socket is not
    predictable at all.
    """
    print("\n" + "=" * 78)
    print(" 实验 2：同一目标、同一 socket，连续问 5 次（对照）")
    print("=" * 78)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", 0))
    print(f"\n本地 socket: 0.0.0.0:{sock.getsockname()[1]}\n")

    try:
        ports = []

        for index in range(5):
            row = await ask(sock, "stun.miwifi.com", 3478, f"第 {index + 1} 次")
            show(row)

            if row.get("mapped"):
                ports.append(row["mapped"][1])

            await asyncio.sleep(0.3)

        print(f"\n  5 次报出的端口：{ports}")

        if len(set(ports)) == 1 and ports:
            print("  ⇒ 同一目标映射稳定。对称 NAT 也遵守「每目的地一个映射」。")
        elif ports:
            print("  ⇒ 同一目标映射都会变 —— 端口分配完全随机，端口预测没戏。")
    finally:
        with contextlib.suppress(OSError):
            sock.close()


async def experiment_mapping_lifetime():
    """
    映射寿命：一个映射多久不活动会被回收？

    这决定 keepalive 的间隔，也决定"打完洞之后能不能一直用"。
    """
    print("\n" + "=" * 78)
    print(" 实验 3：NAT 映射寿命（空闲多久后端口被回收）")
    print("=" * 78)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", 0))

    try:
        first = await ask(sock, "stun.miwifi.com", 3478, "初始")
        show(first)

        if not first.get("mapped"):
            print("  [!] 拿不到初始映射，跳过。")
            return

        base_port = first["mapped"][1]
        print(f"\n  基准端口 {base_port}，现在静置观察（每次等待后不用 socket 发任何东西）：")

        for gap in (10, 20, 30, 60):
            await asyncio.sleep(gap)
            row = await ask(sock, "stun.miwifi.com", 3478, f"空闲 {gap}s 后再问")
            show(row)

            if row.get("mapped"):
                new_port = row["mapped"][1]

                if new_port == base_port:
                    print(f"       → 仍是 {base_port}（映射还活着）")
                else:
                    print(
                        f"       → 变成 {new_port}（旧映射已回收）"
                        f"，说明空闲 {gap}s 内就超时了"
                    )
                    base_port = new_port

            # 这一问本身刷新了映射，所以下一轮是"再空闲 gap 秒"。
    finally:
        with contextlib.suppress(OSError):
            sock.close()


async def main():
    print("")
    print(" UDP 映射行为实验 —— 打洞方案的地基")
    print(f" 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("")

    await experiment_cross_destination()
    await experiment_same_target_stability()
    await experiment_mapping_lifetime()

    print("\n" + "=" * 78)
    print(" 实验结束")
    print("=" * 78)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
