# 任务 8：`JsonlStore`

> **所属里程碑**：M1 · **前置任务**：任务 3 · **代码位置**：`src/harness/store/jsonl.py`、`tests/store/test_jsonl.py`

## 1. 总体目标

轨迹的**真相源**（设计文档 §3.7）：每 run 一个 `.jsonl` 文件，一行一个事件，追加写。

要解决的三个具体问题：

1. **并发 run 会互锁 store。** 评测时有多路 run 并行（设计文档 §7.2 的并发测试是 20 路）。若 `append()` 直接同步写盘并全程持锁，磁盘 I/O 的耗时会串行化所有 run。解法：`append()` 只做校验 + 入内存队列（不碰磁盘），由 `flush()` 批量落盘。
2. **序列号错乱会让轨迹不可重放。** `seq` 是 run 内的全序（因果性靠 `span_id`，顺序靠 `seq`）。store 是唯一写入路径，所以**不变量检查必须放在这里**：非单调 `seq` 直接 `ValueError`。
3. **Windows 上文件句柄不能被并发关闭。** `close()` 必须先 `await flush()`，否则 `PermissionError: [WinError 32]`（同一个坑在任务 12 的进程树处理里会以另一副面目出现）。

## 2. 实现流程

1. 写 5 个失败测试：append / get roundtrip / seq 非单调抛 `ValueError` / 文件懒创建且 flush 后存在 / 未知 run 抛 `KeyError` / **8 路并发 run 各 20 事件互不干扰**
2. 跑出 `ModuleNotFoundError`（红）
3. 实现 `JsonlStore`（内存 buffer + per-run 校验 + `to_thread` 落盘）
4. 跑 `tests/store/` 5 passed（绿）
5. commit

顺序理由：

- **并发测试必须在第一版就写。** 它是**形态约束**而非事后验证：先写一个"每次 `append` 都 `await` 写盘"的实现也能通过前四条测试，问题要等到真实并行评测时才暴露，那时改造要动 store 与所有调用点。**"先写会限制实现形态的测试"是 TDD 里最有价值的一类测试。**
- **roundtrip 测试覆盖"写 → flush → 读"三段**，因为"读到还没落盘的数据"是一个独立的语义问题（见下）。
- `seq` 校验测试用 `ev.model_copy(update={"seq": 0})` 造重复序号——借助任务 2 的 `frozen=True`，**篡改行为在代码里是显式可见的**，不会伪装成普通赋值。

## 3. 具体技术实现

**`append()` 的全部动作都在锁内**：读 `_last_seq` → 校验 → 更新 `_last_seq` → 入 buffer。若校验与写 buffer 分离（或校验在锁外），两个并发 task 可能都读到同一个 `last` 然后都通过。**"检查-然后-行动"必须在同一临界区里**——并发编程最常见的错误之一。

**`flush()` 先把 buffer 摘出来，再在锁外写盘**：

```python
async with self._lock:
    if not self._dirty: return
    pending, self._buffers, self._dirty = self._buffers, {}, False
for rid, rows in pending.items(): ...  # 锁外 await to_thread
```

**绝不在 `await` 期间持锁**：`asyncio.Lock` 是协作式的，持锁期间 await 会让其他 task 全部排队等磁盘。摘出 pending 后，flush 期间的 `append` 会写进新 buffer，两者互不干扰。

**写盘走 `asyncio.to_thread`**：`path.open("a").write(text)` 是阻塞 I/O，直接调用会阻塞事件循环（进而阻塞所有并发 run 的事件处理），`to_thread` 把它挪进线程池。**注意**：计划示例用的就是 stdlib `asyncio.Lock` / `asyncio.to_thread`（与设计文档 §3.7 一致），而 tech-stack §4.1 把 anyio 定为异步原语层；在默认 asyncio 后端下二者等价，`anyio.Lock` / `anyio.to_thread.run_sync` 是直接替换项——**若真要跑 trio 后端，这两处必须替换**。可移植性代价要显式记录，而不是默认它不存在。

**批量拼接成一次写入**：`"".join(json.dumps(r, ...) + "\n" for r in rows)` 再一次性 `write`，把 N 次系统调用压成 1 次——这就是"内存队列 + 批量 flush"的全部收益来源。

**`get()` 先 `await self.flush()` 再读文件**：保证 **read-your-writes**。否则刚 `append` 的事件还在内存里，读回的轨迹是残缺的，而调用方从 API 上看不出来。**凡带缓冲的存储，都必须显式定义"读到的是什么"，并让它在 API 语义上说得通。**

**文件懒创建**：`__init__` 只 `mkdir` 根目录，`.jsonl` 在首次有内容 flush 时才出现（测试断言了这一点）。构造一个 store 应当廉价且无副作用，不应产生垃圾文件。

**校验放在 store 而非 producer**：`seq` 的分配发生在 loop 里（任务 10），但**验证必须发生在唯一写入路径上**。把不变量检查收口到一处，比要求每个生产者都自觉可靠得多。

**用 `dump_event` 而非 `event.model_dump()`**：必须走任务 2 的 `TypeAdapter` 路径，保证 `mode="json"` 的 datetime 转换与判别联合的 `type` 标签都在。

**为什么是 JSONL**：追加写 crash-safe（写完一行就是完整一行）、可流式读、可 grep、可对单行 diff，且天然适合 zstd 压缩（tech-stack §5：inspect_ai 核心依赖含 `zstandard`，lm-eval 的 archiver extra 同为 `["jsonlines", "zstandard"]`）。

## 4. 使用的技术栈简介

| 技术 | 说明 |
|---|---|
| `asyncio.Lock` | 保护 `_last_seq` / `_buffers` 的临界区 |
| `asyncio.to_thread` | 把阻塞文件 I/O 挪出事件循环。何时该用：**操作是阻塞的、又不持有跨 `await` 的状态**时 |
| `json` / `pathlib` | 标准库 |
| `zstandard` 0.24.x | 轨迹压缩，后续接入；轨迹文本压缩比很高 |

**为什么不用 `aiosqlite`**：tech-stack §5 给了实证依据——**inspect_ai 是重度异步项目却选了同步 stdlib `sqlite3`**（`_util/kvstore.py`）。小 KV 读写不值得引入异步驱动，SQLite 本身就是单写者模型，而 `aiosqlite` 最后发版是 2025-12，维护放缓。后续的索引层（任务 28）沿用同一结论：stdlib `sqlite3` + `asyncio.to_thread`。

**存储分层**（tech-stack §5）：JSONL 是 source layer，SQLite 只做索引，DuckDB 只做可选分析层（`duckdb.read_json_auto()` 直查 JSONL，不做主存储——并发写与 WAL 语义不适合流式追加）。

## 5. 工程化思想

**（1）先保证"不丢"，再保证"查得快"。** 真相源与索引分离：追加友好的格式存真相，索引随时可从真相重建。**推论是：索引永远是可丢弃的派生数据**——坏了删掉重建即可；反过来说，任何只存在于索引、真相源里没有的信息都是在冒险。

**（2）缓冲 + 批量的代价必须显式支付。** 非阻塞写入换来吞吐，代价是"落盘延迟"与"读到未落盘数据"这两个新问题，`get()` 里的 `flush()` 就是为后者付的账。**引入缓冲时一定要同时回答：崩溃时最多丢多少？谁保证读到自己的写？** 没有答案的缓冲就是数据丢失。

**（3）不变量检查放在唯一的写入路径上。** store 是轨迹的守卫，不是文件写入器。**"每个生产者都自觉"是系统里最贵的假设**；把校验收口到一处，成本是 O(1)，收益是脏数据根本进不了盘。

**（4）不可变 + 只追加 = 天然的审计日志。** 事件 `frozen=True`（任务 2）配合文件只追加，让轨迹不可能被"就地修改"——任何修史都会留下新记录。这个属性对评测系统格外重要：**报告的每个数字都应能回溯到一段不可否认的原始记录。**

**（5）平台差异要写进注释，而不是留在一个人的记忆里。** "Windows 上文件句柄必须先 flush 再关闭"如果只存在于某人脑中，下一个人半年后会重新踩。写进 docstring 的成本是一行字，收益是同类 bug 不复发第二次。
