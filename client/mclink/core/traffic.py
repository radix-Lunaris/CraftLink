"""
MC-Link -- process-level traffic ledger（审计问题 6）。

============================================================================
 为什么要有这个模块（而不是继续用 core 里的计数器）
============================================================================

用户的规则（原话大意）：累计流量要拆成【对端流量】和【中转流量】，按**当时
真正在用的那条通路**记账，而且要活满**整个进程** —— 离开房间不清零，只有重启
客户端才清零；实时速度任何时候都要有。

原来的实现做不到，因为它挂在会话上：

  * `host_core` 的 `total_bytes_*` 是 HostCore 的生命周期，会话一停就没了；
  * `player_core` 只有一对 `bytes_to_relay/bytes_to_minecraft`，没有 live/total
    之分；
  * `direct_carry.py` **一个字节都不数** —— 直连承载的游戏流量在界面上等于不存在；
  * `engine.snapshot()` 在没会话时只回 `{role: None, state: IDLE}`，累计数字
    直接消失。

所以这里放一个**进程级单例**：谁在搬字节谁就往里加，通路自己说了算
（relay 泵记 relay、direct 泵记 direct），**绝不读 `config.mode`** —— 审计的
问题 3/4 已经证明那个字段会和实际通路漂移。

`RateMeter` 喂的是**累计值**，所以同一个 ledger 直接喂出"实时速度"，而且它
永远在采样：没会话时累计值不动，速率自然衰减到 0，界面显示 `↑ 0 B/s`，
不会像以前那样写一个 `—`。空转时不会有任何线程被这个模块拉起来。
"""

import threading
import time

from .common import RateMeter

#: 通路标签。刻意用字符串（和协议/显示里的 "relay" / "direct" 一致）。
RELAY = "relay"
DIRECT = "direct"

#: 所有合法的通路。写错通路名只会被忽略，绝不抛异常打断数据面。
MODES = (RELAY, DIRECT)


class TrafficLedger:
    """
    进程级累计流量 + 实时速度。

    **两个方向都是"本机视角"**：
        up   = 本机往外发（发往中转服务器 / 发往对端）
        down = 本机往里收（从中转服务器收 / 从对端收）

    线程安全：计数只在 int 上做 `+=`（GIL 下原子），速率采样用一把小锁。
    数据面（direct_carry 的读线程、relay 泵）会直接调 `record()`，所以这里
    **绝不能**做任何可能阻塞的事情。
    """

    def __init__(self):
        # 累计（字节），按通路分开。
        self._totals = {
            RELAY: {"up": 0, "down": 0},
            DIRECT: {"up": 0, "down": 0},
        }

        # 实时速度：每个通路一组，另外一组合计（界面上的"实时速度"是合计）。
        self._meters = {
            RELAY: (RateMeter(), RateMeter()),
            DIRECT: (RateMeter(), RateMeter()),
            "total": (RateMeter(), RateMeter()),
        }

        self._lock = threading.Lock()

        self._rates = {
            RELAY: {"up": 0.0, "down": 0.0},
            DIRECT: {"up": 0.0, "down": 0.0},
            "total": {"up": 0.0, "down": 0.0},
        }

        self._peak_up = 0.0
        self._peak_down = 0.0

        #: 上一次采样是什么时候（只用于测试/诊断，不参与计算）。
        self.sampled_at = 0.0

    # ---------------------------------------------------------------- 写入

    def record(self, mode, up=0, down=0):
        """
        记一笔。`mode` 是**当时真正在用的通路**（`RELAY` / `DIRECT`）。

        未知通路直接忽略：宁可少记一个数字，也不能让数据面因为记账抛异常。
        """
        if mode not in MODES:
            return

        try:
            up = int(up)
            down = int(down)
        except (TypeError, ValueError):
            return

        if up <= 0 and down <= 0:
            return

        totals = self._totals[mode]

        if up > 0:
            totals["up"] += up

        if down > 0:
            totals["down"] += down

    # ---------------------------------------------------------------- 采样

    def sample(self):
        """
        重新算一次实时速度。由界面的每秒刷新（`engine.snapshot()`）驱动。

        喂的是**累计值**，所以这个方法可以在任何时刻被调用任意多次：间隔越短，
        数字越平滑；长时间不调用（界面卡住）也只是下一次采样覆盖更长的时间窗。
        """
        with self._lock:
            for mode in MODES:
                meter_up, meter_down = self._meters[mode]
                totals = self._totals[mode]

                meter_up.update(totals["up"])
                meter_down.update(totals["down"])

                self._rates[mode]["up"] = meter_up.sample()
                self._rates[mode]["down"] = meter_down.sample()

            total = self._totals
            meter_up, meter_down = self._meters["total"]

            meter_up.update(total[RELAY]["up"] + total[DIRECT]["up"])
            meter_down.update(total[RELAY]["down"] + total[DIRECT]["down"])

            self._rates["total"]["up"] = meter_up.sample()
            self._rates["total"]["down"] = meter_down.sample()

            if self._rates["total"]["up"] > self._peak_up:
                self._peak_up = self._rates["total"]["up"]

            if self._rates["total"]["down"] > self._peak_down:
                self._peak_down = self._rates["total"]["down"]

            self.sampled_at = time.monotonic()

        return self._rates["total"]["up"], self._rates["total"]["down"]

    # ---------------------------------------------------------------- 读取

    def snapshot(self):
        """
        给界面用的一份字典。**永远有速度**（没流量就是 0），所以界面不需要在
        "没有会话"时把这一行写成 `—`。

        键的命名固定，`tests/traffic_ledger_test.py` 钉住它。
        """
        with self._lock:
            relay = dict(self._totals[RELAY])
            direct = dict(self._totals[DIRECT])
            rates = {mode: dict(values) for mode, values in self._rates.items()}
            peak_up = self._peak_up
            peak_down = self._peak_down

        return {
            # 累计（进程级，离开房间不清零）。
            "relay_up": relay["up"],
            "relay_down": relay["down"],
            "direct_up": direct["up"],
            "direct_down": direct["down"],
            "total_up": relay["up"] + direct["up"],
            "total_down": relay["down"] + direct["down"],
            # 实时速度（全部通路合计 + 分通路）。
            "up_rate": rates["total"]["up"],
            "down_rate": rates["total"]["down"],
            "relay_up_rate": rates[RELAY]["up"],
            "relay_down_rate": rates[RELAY]["down"],
            "direct_up_rate": rates[DIRECT]["up"],
            "direct_down_rate": rates[DIRECT]["down"],
            "peak_up_rate": peak_up,
            "peak_down_rate": peak_down,
        }


# ============================================================================
#  进程级单例
# ============================================================================
#
# Engine 整个进程只创建一次，理论上放它身上就够。但 `direct_carry` 的读线程和
# relay 泵要能拿到同一个账本，而它们不都持有 Engine 引用 —— 一个模块级单例是
# 最简单、也最难写错的做法（规则 5：共享状态放独立模块，不塞进别的类里）。
_LEDGER = TrafficLedger()


def ledger():
    """进程级的那个账本。"""
    return _LEDGER


__all__ = ["TrafficLedger", "ledger", "RELAY", "DIRECT", "MODES"]
