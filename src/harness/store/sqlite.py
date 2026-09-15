"""SQLite 索引层。

## 为什么用 stdlib `sqlite3` 而不是 `aiosqlite`

inspect_ai 是重度异步项目却选了同步 stdlib —— 小 KV 读写不值得引入异步驱动的
复杂度，且 SQLite 本身就是单写者模型，异步驱动变不出并发度来。

## 并发策略：单写者队列 + 一把 IO 锁

`append()` 只 `put_nowait` 进内存队列就返回；后台**一个** writer task 批量取出、
串行落盘。8 路 run 因此不会在 SQLite 上互相锁（`database is locked` 的根源是
多写者，不是 SQLite 本身）。

**读写共用一条连接，所以读也必须进这把锁。** `check_same_thread=False` 只解除了
threading 检查，不解决语义竞争 —— 一个线程在 `SELECT` 时另一个线程 `COMMIT`，
轻则读到半截，重则驱动直接抛错。真实场景里这一定会发生：报告在跑的同时还有 run 在落盘。

## Windows：`close()` 必须先 flush

文件句柄不能带着未完成的写入关闭。顺序反了的话，症状是**临时目录删不掉**
（`PermissionError: [WinError 32]`），而不是"数据丢了"—— 排查方向会被带偏。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from harness.contracts.results import EvalResult
from harness.events.types import RunEndEvent, RunStartEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, role TEXT, model TEXT, provider TEXT,
  status TEXT, started_at TEXT, ended_at TEXT,
  turns INTEGER DEFAULT 0, tool_calls INTEGER DEFAULT 0,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0.0, spec_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT, seq INTEGER, type TEXT, ts TEXT, turn INTEGER, payload_json TEXT,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS eval_results (
  run_id TEXT, evaluator TEXT, status TEXT, score REAL, summary TEXT, payload_json TEXT,
  PRIMARY KEY (run_id, evaluator)
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""

# 单批上限。再大没有意义（单写者带宽就是瓶颈），反而推迟了别的批次的落盘。
_MAX_BATCH = 200

# 队列里的停机哨兵。用一个不可能与真实条目冲突的 kind。
_STOP = "__stop__"


class SqliteIndex:
    """run / event / eval_result 三张表的查询索引。派生数据，坏了可重建。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._io_lock = asyncio.Lock()
        self._writer: asyncio.Task[None] | None = None
        self._closed = False

    # ---- 写 ----
    def _ensure_writer(self) -> None:
        self._require_open()
        # 无 await 间隔 —— 检查与赋值在同一次事件循环 tick 内完成，不会起两个 writer
        if self._writer is None:
            self._writer = asyncio.create_task(self._drain())

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError(
                "SqliteIndex is closed; appending after close would write to a "
                "closed handle. Open a new index instead."
            )

    async def append(self, event: Any) -> None:
        self._ensure_writer()
        await self._queue.put(("event", event))

    async def put_evals(self, run_id: str, results: list[EvalResult]) -> None:
        self._ensure_writer()
        await self._queue.put(("evals", (run_id, results)))

    async def _drain(self) -> None:
        """单写者：批量取队列、串行落盘。

        ⚠️ **每个取出的项都必须 `task_done()`**，含哨兵。
        `flush()` 是 `await queue.join()`，而 `join()` 等 `unfinished_tasks` 归零 ——
        `put()` 加一、`task_done()` 减一。漏掉一个会让 `flush()` 与 `close()`
        **永久挂起**：测试表现为被超时打断而非报错，极难定位。
        """
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty() and len(batch) < _MAX_BATCH:
                batch.append(self._queue.get_nowait())

            stop = any(kind == _STOP for kind, _ in batch)
            payload = [item for item in batch if item[0] != _STOP]
            if payload:
                async with self._io_lock:
                    await asyncio.to_thread(self._apply, payload)
            for _ in batch:  # ★ 含哨兵，一个都不能漏
                self._queue.task_done()
            if stop:
                return

    def _apply(self, batch: list[tuple[str, Any]]) -> None:
        """在 worker 线程里执行 —— 期间持有 `_io_lock`。"""
        for kind, payload in batch:
            if kind == "event":
                self._apply_event(payload)
            else:
                run_id, results = payload
                for r in results:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO eval_results VALUES (?,?,?,?,?,?)",
                        (run_id, r.evaluator, r.status.value, r.score, r.summary,
                         r.model_dump_json()),
                    )
        self._conn.commit()

    def _apply_event(self, event: Any) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
            (event.run_id, event.seq, event.type.value, event.ts.isoformat(),
             event.turn, event.model_dump_json()),
        )
        if isinstance(event, RunStartEvent):
            # OR IGNORE：同一 run 的后续 RunStart（重放/重试）不该覆盖首条
            self._conn.execute(
                "INSERT OR IGNORE INTO runs "
                "(run_id, role, model, provider, status, started_at, spec_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (event.run_id, event.role, event.model, event.provider, "running",
                 event.ts.isoformat(), event.spec_json),
            )
        elif isinstance(event, RunEndEvent):
            self._conn.execute(
                "UPDATE runs SET status=?, ended_at=?, turns=?, tool_calls=?, "
                "input_tokens=?, output_tokens=?, cost_usd=? WHERE run_id=?",
                (event.status, event.ts.isoformat(), event.turns, event.tool_calls,
                 event.input_tokens, event.output_tokens, event.cost_usd, event.run_id),
            )

    # ---- 读 ----
    async def flush(self) -> None:
        """等队列清空。注意只保证「已入队」的内容落盘。"""
        if self._writer is not None:
            await self._queue.join()

    async def _fetch(self, sql: str, args: tuple[Any, ...] = ()) -> list[Any]:
        """先 flush 再持锁读。

        `flush()` 单独不够：它只保证**已入队**的写落盘。它返回之后、这里的
        SELECT 执行之前，完全可能有新的 `append` 入队并被 writer 线程开始 apply ——
        那个窗口里两个线程同时碰一条连接。锁关的正是这个 TOCTOU 窗口。
        """
        await self.flush()
        async with self._io_lock:
            return await asyncio.to_thread(self._read, sql, args)

    def _read(self, sql: str, args: tuple[Any, ...]) -> list[Any]:
        """在 worker 线程里执行 —— 由 `_fetch` 持锁调用。

        单独成方法是为了让"读写不重叠"这条不变式可被测试探针观测。
        """
        return self._conn.execute(sql, args).fetchall()

    async def query_runs(self) -> list[dict[str, Any]]:
        cols = ["run_id", "role", "status", "turns", "tool_calls", "cost_usd"]
        rows = await self._fetch(
            "SELECT run_id, role, status, turns, tool_calls, cost_usd FROM runs"
        )
        return [dict(zip(cols, row)) for row in rows]

    async def get_evals(self, run_id: str) -> list[EvalResult]:
        rows = await self._fetch(
            "SELECT payload_json FROM eval_results WHERE run_id=?", (run_id,)
        )
        return [EvalResult.model_validate_json(r[0]) for r in rows]

    async def journal_mode(self) -> str:
        async with self._io_lock:
            cur = await asyncio.to_thread(
                lambda: self._conn.execute("PRAGMA journal_mode").fetchone()
            )
        return str(cur[0]).lower()

    # ---- 生命周期 ----
    async def close(self) -> None:
        if self._closed:
            return
        await self.flush()  # ★ 必须先 flush 再关句柄
        if self._writer is not None:
            await self._queue.put((_STOP, None))
            await self._writer
            self._writer = None
        async with self._io_lock:
            await asyncio.to_thread(self._conn.close)
        self._closed = True
