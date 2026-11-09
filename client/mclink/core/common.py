"""
Shared low level helpers for the MC-Link client core.

Everything in here comes from the verified archived clients
(`归档/host_client.py`, `归档/player_client.py`) and behaves identically:

  * TCP_NODELAY is enabled on every socket (Minecraft is latency sensitive).
  * close_writer() never raises and always tries wait_closed().
  * The read buffer is 64 KiB in both directions.
"""

import asyncio
import socket
import time

# Keep this identical to the archived clients: 64 KiB.
BUFFER_SIZE = 64 * 1024


def now():
    return time.strftime("%H:%M:%S")


def enable_tcp_nodelay(writer):
    """Disable Nagle's algorithm where possible."""
    try:
        sock = writer.get_extra_info("socket")

        if sock is not None:
            sock.setsockopt(
                socket.IPPROTO_TCP,
                socket.TCP_NODELAY,
                1,
            )

    except Exception:
        pass


async def close_writer(writer):
    """Close a StreamWriter safely. Never raises, never blocks forever."""
    if writer is None:
        return

    try:
        if writer.is_closing():
            return
    except Exception:
        pass

    try:
        writer.close()
    except Exception:
        return

    try:
        await writer.wait_closed()
    except Exception:
        pass


async def safe_close_all(*writers):
    for writer in writers:
        await close_writer(writer)


def local_ipv4_addresses():
    """
    Best effort list of this machine's LAN IPv4 addresses.

    Used only for a friendly hint in the GUI ("your friends on the same LAN
    can also use http://<ip>:port"), never for the relay itself.
    """
    result = []

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if addr not in result and not addr.startswith("127."):
                result.append(addr)
    except Exception:
        pass

    return result


class RateMeter:
    """
    Turns an ever growing byte counter into a live bytes/second figure.

    Used by the GUI to show real time upload / download speed instead of only
    a cumulative total. `sample()` must be called regularly (the GUI calls it
    about once per second); the reported rate is a smoothed average over the
    window since the previous call, which is exactly what a "speed" display
    should show.

    When no bytes move for a while the rate decays to 0 instead of holding the
    last value, so an idle connection reads 0 B/s rather than a stale number.
    """

    __slots__ = ("_count", "_last_count", "_last_time", "_rate", "_history")

    def __init__(self):
        self._count = 0
        self._last_count = 0
        self._last_time = time.monotonic()
        self._rate = 0.0
        self._history = []

    def update(self, total_bytes):
        """Feed the absolute counter (bytes since the session started)."""
        self._count = int(total_bytes or 0)

    def sample(self):
        """Recompute and return the current rate in bytes/second."""
        now = time.monotonic()

        elapsed = now - self._last_time

        if elapsed <= 0:
            return self._rate

        delta = self._count - self._last_count

        if delta < 0:
            # Counter was reset (new session): start over.
            delta = 0

        instant = delta / elapsed

        if delta == 0:
            # Nothing moved: decay quickly so the display drops to 0.
            self._rate = self._rate * 0.25
            if self._rate < 1.0:
                self._rate = 0.0
        else:
            # Exponential moving average keeps the number readable while
            # still reacting within about a second.
            self._rate = (self._rate * 0.35) + (instant * 0.65)

        self._last_count = self._count
        self._last_time = now

        self._history.append(self._rate)
        if len(self._history) > 5:
            self._history.pop(0)

        return self._rate

    @property
    def rate(self):
        return self._rate


def human_rate(bytes_per_second):
    """Format a bytes/second value for the GUI."""
    value = float(bytes_per_second or 0)

    if value < 1024:
        return f"{value:.0f} B/s"

    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB/s"

    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB/s"

    return f"{value / (1024 * 1024 * 1024):.2f} GB/s"


def human_bytes_short(value):
    """Format a byte total for the GUI (traffic totals)."""
    value = float(value or 0)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{value:.1f} TB"


async def wait_for_stopped(tasks, timeout=5.0):
    """
    Cancel a list of tasks and wait for them, with a hard timeout so a stuck
    socket can never freeze the GUI shutdown path.
    """
    tasks = [t for t in tasks if t is not None]

    for task in tasks:
        if not task.done():
            task.cancel()

    if not tasks:
        return

    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        pass
