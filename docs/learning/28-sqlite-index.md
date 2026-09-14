# 任务 28：`SqliteIndex` 与 `CompositeStore`

> **所属里程碑**：M6 · **前置任务**：8（`JsonlStore`，真相源） · **代码位置**：`src/harness/store/sqlite.py`、`src/harness/store/composite.py`

## 1. 总体目标

轨迹的写法与查法是两件事，得用两种存储。

`JsonlStore` 是**真相源**：每 run 一个文件，追加写，可重放、可人工打开看、可 diff。但"查"很难受——报告要列出所有 run、按状态过滤、取某个 run 的评测结论，在 JSONL 上做这些等于每次全量扫描。于是加一层 SQLite 索引（`runs` / `events` / `eval_results` 三张表），`CompositeStore` 把两者组合起来：**JSONL 存真相，SQLite 供查询**（设计文档 §3.7）。

真正棘手的是并发。评测 harness 天然并发跑（调度器默认 4 路，验收时到 8 路），每个工具事件都要落盘。若每个 run 各自持一条连接并发写，SQLite 会直接甩 `database is locked`；再加上 Windows 上 SQLite 文件句柄**不能被并发关闭**（`PermissionError`），一个不小心就是随机挂。本任务要解决的就是这两件事，而不是"把 SQL 写出来"。

## 2. 实现流程

1. 构造：建父目录 → `sqlite3.connect(path, check_same_thread=False)` → `PRAGMA journal_mode=WAL` + `synchronous=NORMAL` → `executescript` 建表 → 建内存队列（writer task 惰性启动）。
2. 写路径 `append()` / `put_evals()`：**只做一件事**——投进 `asyncio.Queue`，然后返回。
3. 后台 `_drain()` 单写者循环：阻塞取一项 → 在队列非空且未满 200 的范围内批量多取 → `await asyncio.to_thread(self._apply, batch)` 落盘。
4. `_apply()` 在**线程里**执行 SQL 并 commit 一次。
5. 读路径（`query_runs` / `get_evals` / `journal_mode`）：先 `await self.flush()`，再 `to_thread(execute().fetchall())`。
6. `close()`：`flush()` → 投停止哨兵 → `await self._writer` → `to_thread(self._conn.close)`，并用 `_closed` 保证幂等。

两处顺序是正确性而非风格：

- **`close()` 必须先 `flush()` 再关句柄**。反过来的话，后台 writer 的 `_apply` 会打到一条已关闭的连接上；而且 Windows 上句柄被并发关闭就是 `PermissionError`。测试 `test_close_flushes_before_closing_handle` 专门断言"不显式 flush 也能正确落盘"。
- **读之前先 flush**。队列是内存态、落盘是异步的，`query_runs()` 若不等队列排空，读到的是旧状态——这类测试失败是**随机**的，最难查。

## 3. 具体技术实现

### 为什么是 stdlib `sqlite3` + `asyncio.to_thread`，不是 `aiosqlite`

这是本任务最重要的一个决策（tech-stack §5）。证据链：

| 事实 | 来源 |
|---|---|
| **inspect_ai 是重度异步项目，却用同步 stdlib `sqlite3`**（`_util/kvstore.py`） | tech-stack §5 实测 |
| 理由是"小 KV 读写不值得引入异步驱动的复杂度，且 SQLite 本身是单写者模型" | tech-stack §5 原文 |
| `aiosqlite` 最后发版 2025-12，**维护放缓** | tech-stack §5 |
| lm-eval 用 `sqlitedict`，mlflow 用 SQLAlchemy → SQLite | tech-stack §5 同构表 |

关键是那个反问：**引入 `aiosqlite` 到底换来了什么？** 异步驱动解决的是"不该阻塞事件循环"，而 `asyncio.to_thread` 已经把这件事解决了。它换不来并发写——SQLite 的并发模型就是单写者 + WAL（读不阻塞写），换驱动换不掉。净收益是一个额外依赖、一套与 stdlib 不完全同形的 API，以及"看起来我们在做异步 IO"的错觉。

量级上也不成立：本项目的写入是"每次评测几百行索引"，`to_thread` 的线程调度开销被 SQL 执行本身淹没。**"每次几百行"这个量级，就是"不需要为它引入新依赖"的量级。**

### 单写者队列如何让 `database is locked` 不会发生

```python
async def append(self, event: Any) -> None:
    self._ensure_writer()
    await self._queue.put(("event", event))
```

所有写入先进内存队列，**只有一个后台 task** 在消费并串行落盘。于是同一时刻只有一个连接在执行写事务——锁竞争在源头上不存在，而不是靠重试和超时把它压下去。附带的好处是批量：`_drain` 一次最多攒 200 条，`_apply` 只 `commit()` 一次，把 N 次事务降成 1 次。

`check_same_thread=False` 是前提条件（连接会被 `to_thread` 放到别的线程用），但要清楚它**只关掉了 sqlite3 的线程亲和检查**，并发安全完全由"只有一个写者"来保证。这是一处隐式的契约：`to_thread` 的存在理由和单写者队列是同一个决策的两半，谁都不能单独删掉。

`PRAGMA journal_mode=WAL` 让读不阻塞写——这正是"报告在跑的同时查询另一个 run"这种场景需要的；`test_wal_mode_is_enabled` 断言的 `journal_mode() == "wal"` 就是在验这条 PRAGMA 真的生效了，而不是被静默忽略。

### 写语句的幂等性

`events` 表用 `INSERT OR REPLACE`，主键 `(run_id, seq)`：重放同一条事件不会炸，也不会产生重复行。`runs` 表分工更细——`RunStartEvent` 走 `INSERT OR IGNORE` 建行（`status="running"`），`RunEndEvent` 走 `UPDATE` 收尾（`status` / `turns` / `tool_calls` / `cost_usd`）。也就是说**摘要字段来自事件本身，不是另算的**，轨迹与索引永远对得上。

### 一处骨架里的语义漏洞：`flush()` 会挂起

计划给出的 `_drain` 消费了队列项，却没有调用 `task_done()`：

```python
item = await self._queue.get()
...
while not self._queue.empty() and len(batch) < 200:
    batch.append(self._queue.get_nowait())
await asyncio.to_thread(self._apply, batch)
```

而 `flush()` 是 `await self._queue.join()`。按 `asyncio.Queue` 的语义，`join()` 只在**每个 put 进来的项都被 `task_done()` 标记之后**才返回——所以 `flush()` 会永久挂起，`close()`（它第一步就是 flush）也一样。单测里这类问题不会给你报错，只会卡住（会被 `pytest-timeout` 打断，而不是指出原因）。

修法是给每个消费点补 `task_done()`——**包括批量 `get_nowait()` 取出的那些**，因为 `put()` 每调用一次就加一次未完成计数；或者改用显式的完成信号（例如记录"已落盘的最高 seq"）。这条要写进实现清单里，代码 review 时极难发现：它看起来完全合理。

### `CompositeStore` 的分工

`append` 两边都写（JSONL 是真相源、索引是查询层），`get(run_id)` 只从 JSONL 读，`put_evals` / `get_evals` / `query_runs` 走索引，`close` 先 flush 再关索引。

这个分工有个直接收益：**索引是派生数据，损坏或丢了的代价远低于轨迹本身**——真出问题时重建索引即可，轨迹不用重跑。这也是"不引入更重数据库"的底气：既然索引可重建，就没必要为它上 Postgres。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| **JSONL（`jsonlines>=4,<5`）+ `zstandard>=0.24,<1`**（tech-stack §5） | 真相源；轨迹文本压缩比很高，zstd 默认开启。inspect_ai、lm-eval 的 `archiver` extra 都是 `["jsonlines", "zstandard"]` |
| **stdlib `sqlite3` + `asyncio.to_thread`** | 索引层。零依赖、可预测、与 inspect_ai 同构；`aiosqlite` 在"每次几百行"的量级上收益接近于零 |
| **DuckDB 1.5.5**（`[analytics]` extra，tech-stack §5） | **只作分析层，绝不作主存储**——并发写与 WAL 语义不适合流式追加。正确用法是 `duckdb.read_json_auto()` 直接查 JSONL 做跨 run 聚合，JSONL 始终保留为 source layer |
| 同构参照 | inspect_ai（JSONL + zstd + stdlib sqlite3）、lm-eval（结果 JSON + `sqlitedict`）、mlflow（SQLAlchemy → SQLite/Postgres） |
| `pytest-timeout>=2.4`（dev deps） | 上面那个 `join()` 挂起问题会被它打断——**测试基础设施的存在让"卡死"比"挂到天荒地老"可接受得多** |

## 5. 工程化思想

**引入依赖的门槛要与它解决的问题量级匹配。** 问自己两个问题：没有这个依赖会怎样？有了会怎样？`aiosqlite` 的诚实答案是——没有它，`to_thread` 已经把事件循环保护好了；有了它，多一个包、多一套 API 差异，而 SQLite 仍是单写者。**区分"能力性依赖"（没有它就做不到）和"便利性依赖"（没有它只是写法不同）**，后者要有非常明确的理由才进核心依赖。

**把并发问题在架构上消除，而不是在运行时缓解。** 单写者队列让"锁"这件事不存在；重试 + 超时 + backoff 只是把"偶尔失败"压成"很少失败"，问题仍在，而且会在最不巧的时候冒出来。可迁移的思考顺序：先看拓扑能不能串行化（一个写者、一个队列、一个 owner），再看能不能分区（按 key 分片），最后才考虑在访问点加锁或重试。

**真相源与派生视图必须明确分工，判据是"丢了它还能不能恢复"。** JSONL 丢了就真丢了（要重跑）；索引丢了重建即可。所以索引可以随便丢、JSONL 必须谨慎对待——**索引可以放在临时目录，轨迹不行**。反过来讲，如果某个"索引"丢了之后无法从任何地方重建，那它其实是第二个真相源，两个真相源迟早会不一致。

**平台的坑要写进注释和测试，而不是留在某个人的记忆里。** "Windows 上句柄不能被并发关闭"和"close 必须先 flush"这两条，代码里写清楚了，测试也钉住了。这类知识如果只存在于"上次踩过的那个人"脑子里，换个人就会以 `PermissionError` 的形式重新踩一遍。

**骨架代码的结构正确，不等于语义正确。** `flush()` / `task_done()` 这个漏洞的来源很有意思：结构（队列 + 后台任务 + join）完全合理，坏的是"谁负责 signal 完成"这个语义细节。**实现计划给的是骨架，落地时必须逐条核对异步原语的契约**——`Queue.join()` 依赖 `task_done()`、`TaskGroup` 会包装异常（任务 29）、`asyncio.timeout` 抛 `TimeoutError`，这些都不是"看出来"的，是"查出来"的。
