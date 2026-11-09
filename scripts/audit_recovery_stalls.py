"""Local-only TCP backpressure/abort audit; no Minecraft protocol is involved."""
import asyncio
import contextlib
import json
import socket
import struct
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('tests', 'client', 'server')]
import test_auto_integration as fixtures
import relay_server as relay


async def run(delays):
    test = fixtures.IntegrationTests()
    await test.asyncSetUp()
    results = []
    paused = None
    client = None
    sender = None
    try:
        host, player, _, _, port = await test.start_cores('relay')
        for seconds in delays:
            loop = asyncio.get_running_loop()
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.setblocking(False)
            await loop.sock_connect(client, ('127.0.0.1', port))
            await loop.sock_sendall(client, b'warmup')
            warmup = b''
            while len(warmup) < 6:
                warmup += await asyncio.wait_for(loop.sock_recv(client, 6 - len(warmup)), 8)
            assert warmup == b'warmup'
            for task in tuple(test.backend_tasks):
                # A deliberately reset socket is an expected backend outcome.
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            paused = next(w for w in reversed(test.writers)
                          if w.get_extra_info('sockname') and w.get_extra_info('sockname')[1] == test.mc_port)
            paused.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            paused.transport.pause_reading()
            sender = asyncio.create_task(loop.sock_sendall(client, b'x' * (8 * 1024 * 1024)))
            await asyncio.sleep(.5)
            # abort() alone may produce FIN after kernel-buffered bytes. Use
            # SO_LINGER=0 to inject an actual reset behind the backpressure.
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                struct.pack('HH' if os.name == 'nt' else 'ii', 1, 0))
            client.close(); client = None
            start = time.monotonic()
            # Windows may defer delivery of RST behind a paused receive. In
            # that case validate release once the backend resumes, without
            # turning healthy zero-window backpressure into an idle timeout.
            early_budget = .5 if os.name == 'nt' else 5
            while (host.active_sessions or relay.rooms['auto-test'].sessions) and time.monotonic() - start < early_budget:
                await asyncio.sleep(.02)
            cleanup = time.monotonic() - start
            cleaned_during_pause = not host.active_sessions and not relay.rooms['auto-test'].sessions
            if os.name != 'nt' and host.active_sessions:
                def chain(task):
                    coro = task.get_coro(); names = []
                    for _ in range(10):
                        names.append(getattr(getattr(coro, 'cr_code', None), 'co_name', type(coro).__name__))
                        coro = getattr(coro, 'cr_await', None)
                        if coro is None: break
                    return names
                diagnosis = {'player_tasks': [chain(t) for t in player._tasks],
                    'host_workers': [chain(t) for t in host.tunnel_tasks],
                    'pairs': [{'player_closing': p.player_writer.is_closing(),
                               'host_closing': p.tunnel_writer.is_closing(),
                               'player_bytes': p.player_bytes, 'host_bytes': p.tunnel_bytes}
                              for p in relay.rooms['auto-test'].sessions.values()],
                    'tail': test.output.getvalue()[-3500:]}
                print(json.dumps(diagnosis), file=sys.__stdout__, flush=True)
            if os.name != 'nt':
                assert cleaned_during_pause, 'Host/relay forwarding remains blocked after reset'
            await asyncio.sleep(max(0, seconds - cleanup))
            paused.transport.resume_reading(); paused = None
            resumed = time.monotonic()
            await test.ready(lambda: not host.active_sessions and not relay.rooms['auto-test'].sessions, 5)
            post_resume_cleanup = time.monotonic() - resumed
            r, w = await asyncio.open_connection('127.0.0.1', port)
            test.writers.append(w)
            payload = b'recovered-' + bytes([seconds]) * 128000
            w.write(payload); await w.drain()
            assert await asyncio.wait_for(r.readexactly(len(payload)), 8) == payload
            w.close()
            await test.ready(lambda: not host.active_sessions, 5)
            results.append({'paused_seconds': seconds, 'cleanup_seconds': round(cleanup, 3),
                'cleaned_during_pause': cleaned_during_pause,
                'post_resume_cleanup_seconds': round(post_resume_cleanup, 3), 'new_flow_hash_bytes_equal': True})
    finally:
        if client:
            client.close()
        if sender and not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        if paused:
            paused.transport.resume_reading()
        await test.asyncTearDown()
    return results


if __name__ == '__main__':
    delays = [int(n) for n in sys.argv[1:]] or [5, 30, 90]
    result = asyncio.run(run(delays))
    print(json.dumps(result, indent=2))
