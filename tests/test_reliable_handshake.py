"""
可靠流握手编号的离线回归（`core/reliable.py`）。

钉死三件事：

  1. 响应端收到 SYN 之后，`recv_next` 必须是**数据起点**，不能被 SYN 的
     seq(=0) 覆盖。被覆盖的后果是整条流零交付（详见 test 里的推理）。
  2. 发起端重传的 SYN 不能把已经正确的 `recv_next` 再按回 0。
  3. `accept()` 拿到与自身 session id 不一致的 SYN 必须拒绝 —— 那个参数以前
     是死参数，文档承诺"用它核对 session id"却一行都没兑现。

这套测试不需要真实网络：`path` 只用到 `send()`，用一个记录的替身即可。

⚠️ 整个模块共用**一个**事件循环。`_on_segment()` 里起的收包/重传后台任务属于
创建它们的那个循环，用例自己新开一个循环去 `close()` 只会得到
`RuntimeError: Event loop is closed`，而且任务永远收不掉。

跑法：  python3 -m pytest tests/test_reliable_handshake.py -q
        （或者直接 python3 tests/test_reliable_handshake.py）
"""

import asyncio
import sys
import unittest

_ROOT = __file__.rsplit("/", 2)[0]
sys.path.insert(0, _ROOT + "/client")

from mclink.core import reliable as R  # noqa: E402

_LOOP = None


def get_loop():
    global _LOOP

    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()

    return _LOOP


def run(coro):
    return get_loop().run_until_complete(coro)


def tearDownModule():
    """关掉共用的循环 —— 不关就是一串 "Task was destroyed but it is pending"。"""
    global _LOOP

    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()

    _LOOP = None


class RecordingPath:
    """只实现 `send()`：把发出去的段留下来给断言用。"""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(R.Segment.decode(data))

    async def recv(self):
        await asyncio.sleep(3600)

    async def close(self):
        pass


class HandshakeNumbering(unittest.TestCase):
    def make_responder(self):
        path = RecordingPath()
        stream = R.ReliableStream.accept(path, session_id=7)

        self.addCleanup(lambda: run(stream.close(graceful=False)))

        return path, stream

    def test_syn_does_not_reset_recv_next_to_zero(self):
        """
        核心回归：SYN 的 seq 恒为 0，绝不能成为响应端的 recv_next。

        recv_next 一旦变 0，seq=2 的真实数据段既不满足 `seq_lt(2, 0)`
        （判不出"已交付"）、也不等于 recv_next(0)，于是落进乱序缓冲永不交付；
        我们回的 ACK 又是 ack=0，发起端判 `seq_lt(0, 1)` 成立、当陈旧 ACK 丢掉。
        表现为"握手成功、连接秒断、零字节"。
        """
        _path, stream = self.make_responder()

        self.assertEqual(stream.recv_next, R.DATA_SEQ_START)

        syn = R.Segment(session=7, seq=R.SYN_SEQ, ack=0, flags=R.Flag.SYN)
        run(stream._on_segment(syn))

        self.assertEqual(
            stream.recv_next,
            R.DATA_SEQ_START,
            "响应端的 recv_next 被 SYN 的 seq(0) 覆盖了 —— 整条流会零交付",
        )
        self.assertTrue(stream.established)

    def test_retransmitted_syn_is_idempotent(self):
        """发起端 0.2 秒一次的重传 SYN 不能把 recv_next 反复按回 0。"""
        _path, stream = self.make_responder()

        syn = R.Segment(session=7, seq=R.SYN_SEQ, ack=0, flags=R.Flag.SYN)
        run(stream._on_segment(syn))
        run(stream._on_segment(syn))
        run(stream._on_segment(syn))

        self.assertEqual(stream.recv_next, R.DATA_SEQ_START)

    def test_first_data_segment_is_in_order(self):
        """握手之后 seq=DATA_SEQ_START 的数据段必须是"正好下一个"，不是乱序。"""
        _path, stream = self.make_responder()

        run(
            stream._on_segment(
                R.Segment(session=7, seq=R.SYN_SEQ, ack=0, flags=R.Flag.SYN)
            )
        )

        data = R.Segment(
            session=7,
            seq=R.DATA_SEQ_START,
            ack=R.DATA_SEQ_START,
            flags=R.Flag.NONE,
            payload=b"hello",
        )
        run(stream._on_segment(data))

        # 交付了才会进 recv_buffer（`bytearray`）；落进 recv_out_of_order 就
        # 说明判成了乱序。
        self.assertEqual(bytes(stream.recv_buffer), b"hello")
        self.assertEqual(stream.recv_out_of_order, {})
        self.assertEqual(stream.recv_next, R.DATA_SEQ_START + 1)

    def test_accept_rejects_mismatched_session_id(self):
        """`first_segment` 不再是死参数：对不上就拒绝。"""
        path = RecordingPath()

        with self.assertRaises(ValueError):
            R.ReliableStream.accept(
                path, session_id=7, first_segment=R.Segment(session=8)
            )

    def test_accept_allows_matching_or_absent_syn(self):
        """session id 一致、或压根没传 SYN 时，行为不变。"""
        path = RecordingPath()

        stream = R.ReliableStream.accept(
            path, session_id=7, first_segment=R.Segment(session=7)
        )
        self.assertEqual(stream.session_id, 7)

        stream2 = R.ReliableStream.accept(path, session_id=7)
        self.assertEqual(stream2.session_id, 7)


if __name__ == "__main__":
    unittest.main()
