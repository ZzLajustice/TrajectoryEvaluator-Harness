"""组合 JSONL（真相源）+ SQLite（查询层）。

## 为什么是两个而不是一个

两者的**可信度不同**：

  - `JsonlStore` 是真相源。一行一条事件，独立可解析，索引坏了它还在。
  - `SqliteIndex` 是派生数据。为查询而生（"哪条用例 flaky"），坏了可以从
    JSONL 重建 —— 所以它允许批量、异步、最终一致。

把两者合成一个类，是为了让调用方只有**一个** `TrajectoryStore` 实现可注入，
而不是要在 `RunDeps` 里同时塞两个。分工仍然清楚：轨迹读 JSONL，
查询与评测结果读索引。

## 评测结果为什么落索引而不是 JSONL

`EvalResult` 不是事件，不该混进事件流（事件 schema 是被字段快照测试锁住的）。
它天然是"按 run 查询"的数据，正好是索引的用途 —— M8 的聚合报告直接从这里读。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.contracts.results import EvalResult
from harness.events.trajectory import Trajectory
from harness.store.jsonl import JsonlStore
from harness.store.layout import INDEX_NAME
from harness.store.sqlite import SqliteIndex


class CompositeStore:
    """实现 `TrajectoryStore`，额外提供 run / eval 查询。"""

    def __init__(self, *, root: Path | str) -> None:
        self._root = Path(root)
        self._jsonl = JsonlStore(self._root)
        self._index = SqliteIndex(self._root / INDEX_NAME)

    # ---- TrajectoryStore ----
    async def append(self, event: Any) -> None:
        await self._jsonl.append(event)
        await self._index.append(event)

    async def flush(self) -> None:
        await self._jsonl.flush()
        await self._index.flush()

    async def get(self, run_id: str) -> Trajectory:
        return await self._jsonl.get(run_id)

    # ---- 评测结果 ----
    async def put_evals(self, run_id: str, results: list[EvalResult]) -> None:
        await self._index.put_evals(run_id, results)

    async def get_evals(self, run_id: str) -> list[EvalResult]:
        return await self._index.get_evals(run_id)

    # ---- 查询 ----
    async def query_runs(self) -> list[dict[str, Any]]:
        return await self._index.query_runs()

    async def query_index(self) -> int:
        return len(await self._index.query_runs())

    async def close(self) -> None:
        # 先 flush 再关 —— 顺序反了就是"数据无声地少了几个事件"
        await self.flush()
        await self._index.close()
