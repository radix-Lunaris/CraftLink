"""
MC-Link -- unit tests for the small formatting / measurement helpers.

These lock down the bug that made the GUI show "--" for latency:
a measured 0.0 ms is a valid value, but the old code used a truthiness test
(`if ping:`) which treats 0.0 as "no data".

Run:
    python tests/units_test.py
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(ROOT, "client"))

from mclink.core.common import (                      # noqa: E402
    RateMeter,
    human_bytes_short,
    human_rate,
)

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def test_human_rate():
    check("human_rate 0", human_rate(0) == "0 B/s", human_rate(0))
    check(
        "human_rate bytes",
        human_rate(512) == "512 B/s",
        human_rate(512),
    )
    check(
        "human_rate KB",
        human_rate(2048) == "2.0 KB/s",
        human_rate(2048),
    )
    check(
        "human_rate MB",
        human_rate(3 * 1024 * 1024) == "3.0 MB/s",
        human_rate(3 * 1024 * 1024),
    )
    check(
        "human_rate GB",
        human_rate(2 * 1024 ** 3) == "2.00 GB/s",
        human_rate(2 * 1024 ** 3),
    )
    check(
        "human_rate tolerates None",
        human_rate(None) == "0 B/s",
        human_rate(None),
    )


def test_human_bytes():
    check("human_bytes 0", human_bytes_short(0) == "0 B", human_bytes_short(0))
    check(
        "human_bytes KB",
        human_bytes_short(1536) == "1.5 KB",
        human_bytes_short(1536),
    )
    check(
        "human_bytes MB",
        human_bytes_short(12 * 1024 * 1024) == "12.0 MB",
        human_bytes_short(12 * 1024 * 1024),
    )


def test_rate_meter():
    meter = RateMeter()

    # First sample with no data -> 0.
    meter.update(0)
    first = meter.sample()

    check("RateMeter starts at 0", first == 0.0, str(first))

    # Simulate 1 MB in one second.
    meter.update(1024 * 1024)
    time.sleep(1.0)
    rate = meter.sample()

    check(
        "RateMeter reports roughly the injected rate",
        300 * 1024 < rate < 2 * 1024 * 1024,
        f"{rate / 1024:.0f} KB/s",
    )

    # No new bytes for a while -> must decay towards 0.
    values = []
    for _ in range(6):
        time.sleep(0.2)
        values.append(meter.sample())

    check(
        "RateMeter decays to 0 when traffic stops",
        values[-1] < 1024,
        f"{[round(v / 1024, 1) for v in values]} KB/s",
    )

    # Counter reset (new session) must not produce a negative rate.
    meter.update(0)
    time.sleep(0.3)
    check("RateMeter handles a counter reset", meter.sample() >= 0, "")


def test_gui_formatters():
    from mclink.gui.app import fmt_ping, ping_quality, fmt_speed

    # ---- THE BUG: 0.0 ms used to render as "--" ------------------
    check(
        "fmt_ping(0.0) shows a real value, not a dash",
        fmt_ping(0.0) == "0.0 ms",
        fmt_ping(0.0),
    )
    check("fmt_ping(None) shows a dash", fmt_ping(None) == "—", fmt_ping(None))
    check(
        "fmt_ping(42.4) rounds nicely",
        fmt_ping(42.4) == "42 ms",
        fmt_ping(42.4),
    )
    check(
        "fmt_ping(7.25) keeps one decimal",
        fmt_ping(7.25) == "7.2 ms",
        fmt_ping(7.25),
    )
    check(
        "fmt_ping rejects negative values",
        fmt_ping(-5) == "—",
        fmt_ping(-5),
    )

    check("ping_quality(30) is 优秀", ping_quality(30)[0] == "优秀", ping_quality(30)[0])
    check("ping_quality(100) is 良好", ping_quality(100)[0] == "良好", ping_quality(100)[0])
    check("ping_quality(150) is 一般", ping_quality(150)[0] == "一般", ping_quality(150)[0])
    check("ping_quality(300) is 较差", ping_quality(300)[0] == "较差", ping_quality(300)[0])
    check(
        "ping_quality(None) is neutral",
        ping_quality(None)[0] == "未测到",
        ping_quality(None)[0],
    )

    text = fmt_speed(2048, 4096)
    check(
        "fmt_speed shows both directions",
        "↑ 2.0 KB/s" in text and "↓ 4.0 KB/s" in text,
        text,
    )
    check("fmt_speed hides when inactive", fmt_speed(1, 1, active=False) == "—")
    check("fmt_speed tolerates None", "0 B/s" in fmt_speed(None, None), fmt_speed(None, None))


def main():
    print("=" * 64)
    print("MC-Link unit tests (formatting + rate measurement)")
    print("=" * 64)

    test_human_rate()
    test_human_bytes()
    test_rate_meter()
    test_gui_formatters()

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
