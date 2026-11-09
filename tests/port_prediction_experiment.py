#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端口预测可行性实验：对称 NAT 的下一个端口能不能被猜中？

============================================================================
 它回答的问题
============================================================================

NAT3 ↔ NAT4（一端锥形、一端对称）能不能打通，**只取决于一件事**：

    锥形那边能不能猜中对称那边新分配的端口。

锥形一侧的映射对所有目的地都一样，所以它不需要被猜 —— 它只要主动往
"猜出来的端口"发包，就能在对称那侧打开一个洞。

所以这个实验只测一件事：

    在同一个 (本地socket, 目标) 上下一次分配，端口落在哪里？
    那个偏移是"可预测的"还是"随机的"？

============================================================================
 怎么测才可信（三个坑）
============================================================================

1. **必须有真实的外部观察者**。只有 NAT 外面的机器能告诉你它给你分配的
   公网端口，所以用 VPS 的 STUN 响应器（19100/udp）当观察者。

2. **必须把"分配事件"和"别的流量"分开**。每开一个新 socket 就把本地端口
   占掉一个，而 NAT 的端口分配和本地端口经常是同一个游标 —— 中间夹了
   别的 UDP 流量，观察到的"步长"就不再是步长。所以这里：
     * 先开好全部 socket，**不发**；
     * 然后按固定顺序、尽快地逐个发。
   把"开 socket"和"触发分配"在时间上分开。

3. **要对照两件事**：
     a. 同一批分配之间的步长（簇内）；
     b. **上一批的最后几个** 和 **下一批的最前几个** 之间的步长（跨批）。
   (b) 才是真正要预言的东西 —— peer 猜的是"你下一批会落在哪"。
   只测 (a) 会得出过于乐观的结论。

Run:
    python tests/port_prediction_experiment.py
    python tests/port_prediction_experiment.py --rounds 8 --per-round 8
"""

import argparse
import asyncio
import contextlib
import os
import socket
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core import stun  # noqa: E402

DEFAULT_OBSERVER = ("www.example.com", 19100)


def classify_steps(steps):
    """
    Describe a list of port deltas: are they a usable prediction?

    Returns (verdict, detail). The verdicts are deliberately narrow:

      "递增可预测"  every positive step is small and the same size
      "递增不稳定"  positive but the size wobbles
      "递减可预测"  mirror image
      "随机/大跨度" the steps are all over the place -- prediction is hopeless
      "无规律"      mixed signs or too few samples
    """
    if len(steps) < 3:
        return "样本不足", f"只有 {len(steps)} 个步长"

    positives = [s for s in steps if s > 0]
    negatives = [s for s in steps if s < 0]

    if len(positives) == len(steps):
        spread = max(positives) - min(positives)
        median = statistics.median(positives)

        if spread <= 2:
            return "递增可预测", f"步长恒定 ≈ +{median:.0f}（最大波动 {spread}）"

        if max(positives) <= 100:
            return "递增不稳定", (
                f"步长全为正但大小在 {min(positives)}..{max(positives)} 之间"
                f"（中位 {median:.0f}）"
            )

        return "随机/大跨度", f"全为正，但最大步长 {max(positives)} 太大"

    if len(negatives) == len(steps):
        spread = max(negatives) - min(negatives)
        median = statistics.median(negatives)

        if spread <= 2:
            return "递减可预测", f"步长恒定 ≈ {median:.0f}"

        if abs(min(negatives)) <= 100:
            return "递减不稳定", (
                f"步长全为负但大小在 {min(negatives)}..{max(negatives)} 之间"
            )

        return "随机/大跨度", f"全为负，但最大步长 {min(negatives)} 太大"

    return "无规律", "步长正负混杂"


async def one_round(observer_host, observer_port, per_round, gap_ms=0):
    """
    One batch: open `per_round` sockets first, then trigger them all.

    Returns (list_of_public_ports, list_of_local_ports) in trigger order.
    """
    loop = asyncio.get_running_loop()

    infos = None
    for attempt in range(3):
        try:
            infos = await loop.getaddrinfo(
                observer_host, observer_port, type=socket.SOCK_DGRAM
            )
            break
        except (OSError, socket.gaierror):
            await asyncio.sleep(0.8)

    if not infos:
        raise RuntimeError("无法解析观察者地址")

    picked = stun.pick_ipv4(infos)

    if picked is None:
        raise RuntimeError("观察者没有 IPv4 地址")

    _family, sockaddr = picked

    # ---- 阶段 1：先把 socket 全开好（此时还没有任何分配） ----
    socks = []

    for _ in range(per_round):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        s.bind(("0.0.0.0", 0))
        socks.append(s)

    local_ports = [s.getsockname()[1] for s in socks]

    # ---- 阶段 2：按顺序逐个触发分配 ----
    public_ports = []

    for s in socks:
        mapped, _source, _rtt = await stun._stun_exchange(
            s, sockaddr[0], sockaddr[1], False, False, 3.0
        )
        public_ports.append(mapped[1] if mapped else None)

        if gap_ms:
            await asyncio.sleep(gap_ms / 1000.0)

    for s in socks:
        with contextlib.suppress(OSError):
            s.close()

    return public_ports, local_ports


async def main_async(args):
    observer = args.observer
    host, _, port = observer.partition(":")
    port = int(port or "19100")

    print("")
    print("=" * 78)
    print(" 端口预测可行性实验")
    print("=" * 78)
    print(f" 观察者   : {host}:{port}（它看到的就是 NAT 分配的公网端口）")
    print(f" 批次     : {args.rounds} 批 × 每批 {args.per_round} 个 socket")


# ============================================================
# 增量式实验：真实打洞是**顺序**发生的，不是整批
# ============================================================
#
# 上面那个整批实验把"开 socket"和"触发分配"分开，看起来更干净，但它测的
# 形态和真实打洞不一样：真实打洞是每开一个新 socket 就立刻朝对端发，
# 逐次、有间隔。
#
# 而且整批实验暴露了一件更重要的事：这台 NAT **不是一条游标，而是好几条
# 并行的递增序列**（观察到的 -2 / +4500 / -5960 那种大跳，其实是采样在
# 多条流之间交错）。分不清是哪条流，预测就会像上面那样 3/5 命中。
#
# 所以这个增量实验按真实时序来，并额外统计：
#   * 前向误差（预测值 - 实际值，正数 = 猜高了）
#   * 命中率
#   * **要覆盖实际值需要多大的窗口**（这才是打洞真正的成本）

async def incremental_async(args):
    host, _, port = args.observer.partition(":")
    port = int(port or "19100")

    print("")
    print("=" * 78)
    print(" 增量式端口预测（按真实打洞时序：逐次分配 + 间隔）")
    print("=" * 78)
    print(f" 观察者 : {host}:{port}")
    print(f" 次数   : {args.count}")
    print("")

    loop = asyncio.get_running_loop()

    infos = None
    for _ in range(3):
        try:
            infos = await loop.getaddrinfo(
                host, port, type=socket.SOCK_DGRAM
            )
            break
        except (OSError, socket.gaierror):
            await asyncio.sleep(0.8)

    if not infos:
        print("  无法解析观察者地址。")
        return 1

    picked = stun.pick_ipv4(infos)
    _family, sockaddr = picked

    observations = []

    for index in range(args.count):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        s.bind(("0.0.0.0", 0))
        local = s.getsockname()[1]

        mapped, _src, _rtt = await stun._stun_exchange(
            s, sockaddr[0], sockaddr[1], False, False, 3.0
        )
        s.close()

        with contextlib.suppress(OSError):
            pass

        if not mapped:
            print(f"  #{index + 1} 无响应")
            continue

        public = mapped[1]
        observations.append((local, public))
        print(f"  #{index + 1:2}  本地 {local:5}  →  公网 {public:5}")

        if args.gap:
            await asyncio.sleep(args.gap / 1000.0)

    if len(observations) < 4:
        print("\n  [!] 样本太少。")
        return 1

    # ---- 用"前两个的步长"去猜下一个 ----
    print("")
    print("-" * 78)
    print(" 检验：看到第 i-2、i-1 个，预测第 i 个")
    print("-" * 78)

    errors = []
    hits = 0
    needed_window = []

    for index in range(2, len(observations)):
        prev2 = observations[index - 2][1]
        prev1 = observations[index - 1][1]
        actual = observations[index][1]

        predicted = prev1 + (prev1 - prev2)
        error = actual - predicted

        errors.append(error)
        needed_window.append(abs(error))

        if abs(error) <= 1:
            hits += 1

        print(
            f"    有 {prev2},{prev1} → 猜 {predicted}，实际 {actual}"
            f"（误差 {error:+d}）{'  ← 命中' if abs(error) <= 1 else ''}"
        )

    total = len(errors)
    print(f"\n  命中（误差 ≤1）: {hits}/{total}")

    if errors:
        print(
            f"  误差范围        : {min(errors):+d} .. {max(errors):+d}"
            f"（中位 {statistics.median(errors):+.0f}）"
        )

    print("")
    print("-" * 78)
    print(" 要覆盖实际端口，需要多宽的窗口？")
    print("-" * 78)

    for width, label in (
        (0, "只猜 1 个端口"),
        (5, "窗口 ±5"),
        (20, "窗口 ±20"),
        (50, "窗口 ±50"),
        (100, "窗口 ±100"),
    ):
        covered = sum(1 for e in needed_window if abs(e) <= width)
        print(
            f"    {label:16} 覆盖 {covered}/{total} "
            f"（{100.0 * covered / total:.0f}%）"
        )

    print("")
    print("=" * 78)
    print(" 结论")
    print("=" * 78)

    # ---- 关键判断 ----
    if hits >= total * 0.6:
        print(f"  ⇒ 端口**可预测**（{hits}/{total} 命中）。")
        print("     值得实现「有界端口预测」：一端开若干 socket 形成一个簇，")
        print("     把窗口作为候选告诉 peer，peer 往整簇里喷包。")
    else:
        wide = sum(1 for e in needed_window if abs(e) <= 100)
        print(f"  ⇒ 精确预测不行（只 {hits}/{total} 命中）。")

        if wide >= total * 0.8:
            print("     但**窗口 ±100 能覆盖大多数**分配 —— 这正是 EasyTier")
            print("     「有界端口预测 + 端口簇」能救回来的那类网络。")
            print("     它不要求猜准，只要求窗口盖得住，靠多 socket 摊平。")
        else:
            print("     窗口也盖不住 —— 这台 NAT 的分配跨度过大，")
            print("     端口预测没有可行空间，应当快速判定并回落中继。")

    print("=" * 78)
    return 0


def main():
    parser = argparse.ArgumentParser(description="对称 NAT 端口预测可行性实验")
    parser.add_argument("--observer", default="www.example.com:19100",
                        help="外部观察者 host:port（默认项目 VPS 的 STUN）")
    parser.add_argument("--rounds", type=int, default=6, help="批次数")
    parser.add_argument("--per-round", type=int, default=8, help="每批 socket 数")
    parser.add_argument("--gap-between-rounds", type=int, default=800,
                        help="批与批之间间隔毫秒")
    parser.add_argument("--incremental", action="store_true",
                        help="用真实打洞时序（逐次分配）测端口预测")
    parser.add_argument("--count", type=int, default=16,
                        help="增量模式的分配次数")
    parser.add_argument("--gap", type=int, default=150,
                        help="增量模式两次分配之间的间隔毫秒")

    args = parser.parse_args()

    with contextlib.suppress(KeyboardInterrupt):
        if args.incremental:
            return asyncio.run(incremental_async(args))

        return asyncio.run(main_async(args))

    return 130


if __name__ == "__main__":
    sys.exit(main())
