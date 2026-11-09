# v2.0 遗留问题调查：加入房间后「开始连接」概率性无响应

> 状态：**只调查、不修改**（用户明确要求交给 v3 处理）。
> 记录时间：2026-09-15 第四场会话
> 影响版本：客户端 v2.2.0（`deploy/CraftLink.exe`，SHA-256 `80eadbe6…`）

---

## 1. 用户描述的现象（原话整理）

* 加入房间之后点「开始连接」，**概率性**超时。
* 超时的时候**整个客户端进程直接无响应**（窗口卡住、点不动）。
* **重启软件后一般就好了**。
* 概率出现，不影响日常使用，所以暂不修。

关键词：**概率性** + **整个进程无响应**（不是"只有网络失败"）+ **重启可恢复**。

---

## 2. 已知的线程模型（这是分析的地基）

客户端是「tkinter 主线程（GUI） + 一个 asyncio 事件循环线程（网络）」的双线程结构：

```
GUI 线程 (tkinter)                   网络线程 (asyncio loop)
─────────────────────────────       ────────────────────────────────
_tick()  root.after(1000ms)  ──►     SessionBase / RoomSession 协程
  ├─ engine.drain_events()            ├─ control_loop()   读写控制连接
  ├─ _append_logs()                   ├─ tunnel_loop() × max_tunnels
  └─ _refresh_*()                     ├─ ping_loop() / sample_loop()
                                      └─ 房间成员控制连接
engine.submit(coro, timeout=N) ──►   asyncio.run_coroutine_threadsafe(...)
  future.result(timeout=N)  ← 阻塞 GUI 线程直到超时或完成
```

**关键点**：GUI 线程上有一批"**同步等待**"调用（`engine.submit(...)` → `future.result(timeout=N)`）。
它们本身有超时上限，所以**单次调用不应该永久卡死**。但如果超时值叠加、或者超时之后
协程仍占着事件循环，就会表现成"窗口卡住好几十秒"。

---

## 3. 代码里能找到的「可疑点」（按可疑程度排序）

### 3.1 `stop_session()` + `start_session()` 的超时是**串行叠加**的（最可疑）

`client/mclink/core/engine.py`：

```python
def start_session(self, config, auth=None, timeout=20):
    self.start_loop()
    self.stop_session(timeout=15)        # ← 先停旧的：最长 15s
    ...
    self.submit(session.start(), timeout=timeout)   # ← 再起新的：最长 20s
```

而 `stop_session()` 内部（同文件）：

```python
self.submit(session.stop(), timeout=timeout)        # HostSession.stop()
```

`HostSession.stop()`（`client/mclink/core/host_core.py`）做的是：

```python
for task in tasks:            # control + ping + sample + tunnel_loop × N
    task.cancel()
await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=6)
await close_writer(self.control_writer)     # ← 这一步在事件循环里做网络关闭
```

**链条**：玩家/房主在**刚加入房间**（房间成员连接正在跑）之后马上点「开始连接」，
就会走到 `stop_session()` → 取消并 `gather` 一堆隧道任务 → `close_writer()`。
如果此刻网络半死（弱网、路由切换、对端不回 FIN），`close_writer()` 里的
`await writer.wait_closed()` 可能长时间不返回 —— 它在**事件循环线程**上，
于是整个 asyncio loop 停住；GUI 线程等 `future.result()` 一直等到超时。

**表现**：点「开始连接」→ 窗口整片无响应 → 若干十秒后可能自己好，或者用户直接
重启软件（重启 = 换一个干净的事件循环，所以"重启就好"完全吻合）。

**为什么是"概率性"**：只有"房间刚加入（成员连接活跃）+ 网络抖动"这个组合才会命中。
平时网络干净，`wait_closed()` 秒回，谁也看不出来。

### 3.2 `close_writer()` 没有超时 —— 而它的 docstring 声称不会永久阻塞

`client/mclink/core/common.py:40`（实测代码，逐行确认）：

```python
async def close_writer(writer):
    """Close a StreamWriter safely. Never raises, never blocks forever."""   # ← 声称
    if writer is None:
        return

    try:
        if writer.is_closing():     # 已经关掉的直接跳过（这一点是好的）
            return
    except Exception:
        pass

    try:
        writer.close()
    except Exception:
        return

    try:
        await writer.wait_closed()  # ← 没有 timeout：docstring 的承诺在这里失效
    except Exception:
        pass
```

这是**全项目最热的清理函数**：隧道、控制连接、成员连接、presence 连接都走它。
`wait_closed()` 在"对端半死、不回 FIN"时可以拖很久，而它跑在**事件循环线程**上
—— 循环一停，GUI 线程的 `submit()` 就只能干等到超时。

另外 `safe_close_all(*writers)` 是**串行** await 每一个 `close_writer()`，
所以 N 条隧道一起清理时，最坏情况是 N × 单个等待时间。

> 这一条是本轮调查里**唯一有明确代码证据**的问题：
> 一个自称"never blocks forever"的函数，实际上有唯一的无超时等待。

### 3.3 `session.py` 里一个 8 秒的 `wait_for` 在主路径上

`client/mclink/core/session.py:273` 有 `await asyncio.wait_for(event.wait(), timeout=8)`。
如果它在"启动检查"路径上被触发，会往上面 3.1 的串行总时长里再叠 8 秒。

### 3.4 GUI 每秒的 `_tick()` 是"自愈"也是"放大器"

`client/mclink/gui/app.py` 的 `_tick()` 每 1000ms 做：drain 事件、刷日志、
`_refresh_room_ui()`、`_refresh_status()`。

* 好处：事件丢了最多晚 1 秒恢复（这是已验收的"幽灵房"修复）。
* 坏处：如果网络线程把 `engine` 的锁持住，`_tick()` 里的 `_refresh_room_ui()`
  → `engine.room_state()` 会去拿 `_room_lock`；一旦拿不到，**GUI 线程也一起卡住**。

`engine.py` 里 `self._room_lock` / `self._session_lock` 是普通 `threading.Lock()`，
没有超时、没有 `try_lock`。跨线程共享 + 无超时 = 潜在死锁面。

---

## 4. 综合推断（不是结论，是"最可能的那一条"）

```
弱网/半死连接
   │
   ├─► 事件循环线程：close_writer() 的 await wait_closed() 长时间不返回
   │        （common.py: 没有 timeout）
   │
   ├─► 同一个循环上：stop_session() 的 gather/取消也排在后面
   │
   ├─► GUI 线程：future.result(timeout=15/20) 一直等 → 窗口无响应
   │
   └─► 用户重启 → 新的循环 → 立刻正常 ✅ 与"重启就好"完全吻合
```

**为什么概率性**：需要"刚进房间（成员连接活跃）+ 网络抖动 + 这一次清理正好撞上"
三者同时成立。这也是为什么它难以稳定复现 —— **它不是逻辑错误，是时序 + 网络**。

---

## 5. v3 建议的修法（按优先级，先取证再改）

> ⚠️ 铁律：**先写一个能复现的测试，再改**。这个问题的难点全在"复现"，不在"修"。

### 第一步：取证（不改功能，只加观测）

1. **事件循环心跳看门狗**
   在网络线程里起一个 `asyncio` 任务，每 0.5s 往一个时间戳里写一次。
   GUI 的 `_tick()` 读它：
   * 超过 3s 没跳 → 在界面上标「网络线程响应慢」，日志写警告。
   * 超过 10s → 用 `faulthandler.dump_traceback()`（或 `asyncio` 的
     `loop.call_soon` 探针）把**事件循环当前卡在哪个 await** 打出来。
2. **把所有 `close_writer()` 的调用点统一包一层**
   `asyncio.wait_for(writer.wait_closed(), timeout=2)`，并且**记录**超时事件
   （超时不再静默 pass）。这样"是它卡的"会立刻在日志里显形。
3. **给 `submit()` 的每一次调用打点**：调用点名字 + 实际耗时 + 是否超时。
   GUI 线程上一次超过 2s 的等待都值得记一条。

### 第二步：定位（拿到上面数据之后）

看日志里那一条 `wait_closed` 超时 / 循环停跳点，就能确认是不是 3.1+3.2 那条链。

### 第三步：修（候选方案，按侵入性从小到大）

| 方案 | 做法 | 侵入性 |
|---|---|---|
| A | `close_writer()` 加 `wait_for(..., timeout=2)` 并吸收超时 | 极小，安全，**建议先做这个** |
| B | `stop_session()` 改成"发取消信号就走"，不再 `gather` 等结果 | 小，但要确认没有资源泄漏 |
| C | `submit()` 的 GUI 阻塞改成"乐观放行"：只等 3s，超时就把界面标成"正在后台收尾"，不冻结 | 中，涉及交互语义 |
| D | `_room_lock` / `_session_lock` 改成带超时的取锁（`acquire(timeout=…)`），拿不到就走"无状态快照"路径 | 中 |
| E | 把网络层搬到独立进程（彻底隔离） | 大，留给 v5+ 或 UI 革新时一起考虑 |

**推荐顺序**：A（立刻降低危害）→ 心跳看门狗（下次复现立刻定位）→ B/C（按定位结果）。

---

## 6. 给 v3 的一句话交接

> 「加入房间后概率性无响应」已定位到**候选链**：
> `stop_session/start_session` 串行超时叠加 + `close_writer()` 无超时的
> `wait_closed()` 跑在事件循环上，再叠加 GUI 线程 `submit()` 的同步等待。
> **先加心跳看门狗与 close_writer 超时取证，再动结构**；
> 修复方案 A→E 已列好，但**不要在没拿到复现数据前重构网络层**。
