"""并发调度器。

## 三个语义，每一个都有对应的失败场景

1. **结果按输入顺序返回**，不按完成顺序。
   否则同一份 suite 跑两次报告的行序不同，diff 出来全是噪声。
   实现上是先按 key 收集、最后按输入顺序重排 —— 不是靠"恰好按顺序完成"。

2. **异常隔离** —— 单条 case 失败不取消其他（除非 `fail_fast`）。
   5 条用例里第 2 条炸了，剩下 3 条的数据不该跟着消失。

3. **每 case 独立超时** —— 防止单条卡死拖垮整个 suite。
   卡住的 case 记成一个异常结果，与其他失败一视同仁。

## TaskGroup 的拆包是必须的

`asyncio.TaskGroup` 把子任务异常包成 `BaseExceptionGroup`。不拆包的话
`except BudgetExceeded` 这类精确捕获会**静默失效** —— 代码看起来对，
运行时永远走不到那个分支。`_reraise_single` 负责拆。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

Work = Callable[[], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class Skipped:
    """因 suite 级预算耗尽而**未启动**。

    刻意不是异常，也不是失败：这条 case 根本没跑，
    把它记成失败会污染 pass_rate。
    """

    reason: str


class Scheduler:
    """有界并发 + 顺序结果 + 异常隔离。不认识 Run，只认识"可等待的工作"。"""

    def __init__(
        self,
        *,
        concurrency: int = 4,
        case_timeout_s: float | None = 600.0,
        fail_fast: bool = False,
    ) -> None:
        # 0 或负数会让 Semaphore 永久挂起 —— 静默死锁比报错难查得多
        self.concurrency = max(1, concurrency)
        self.case_timeout_s = case_timeout_s
        self.fail_fast = fail_fast

    async def gather(
        self,
        items: Sequence[tuple[str, Work]],
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[tuple[str, Any]]:
        keys = [key for key, _ in items]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            # 重复 key 会让两条结果互相覆盖，且没有任何报错 —— 报告里少一条用例
            raise ValueError(f"duplicate keys: {dupes}")

        sem = asyncio.Semaphore(self.concurrency)
        results: dict[str, Any] = {}

        async def guarded(key: str, work: Work) -> None:
            async with sem:
                # 在**拿到信号量之后**判，而不是排队之前 ——
                # 排队时判的话，已经在等的那些照样会跑，等于没停。
                if should_stop is not None and should_stop():
                    results[key] = Skipped("suite budget exhausted before this case started")
                    return
                try:
                    if self.case_timeout_s is None:
                        results[key] = await work()
                    else:
                        async with asyncio.timeout(self.case_timeout_s):
                            results[key] = await work()
                except Exception as exc:  # noqa: BLE001
                    # 记成结果而不是抛出 —— 隔离在这里发生。
                    # fail_fast 时的 re-raise 是两条路径唯一的差异。
                    results[key] = exc
                    if self.fail_fast:
                        raise

        try:
            async with asyncio.TaskGroup() as tg:
                for key, work in items:
                    tg.create_task(guarded(key, work))
        except* Exception as eg:
            _reraise_single(eg)

        # ★ 按输入顺序重排，不是按完成顺序
        return [(key, results.get(key)) for key in keys]


def _reraise_single(eg: BaseExceptionGroup) -> None:
    """拆开 ExceptionGroup，让单个异常以原类型抛出。

    不这么做的话，调用方拿到的是 `ExceptionGroup` 而非原始异常类型 ——
    `pytest.raises(RuntimeError)` 会失败，上层 `except BudgetExceeded`
    这类精确捕获也会静默失效。
    """
    flat = [e for e in eg.exceptions if not isinstance(e, BaseExceptionGroup)]
    if len(flat) == 1:
        raise flat[0]
    raise eg
