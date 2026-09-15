"""并发调度器测试。

## 三个语义各对应一条测试

1. **结果按输入顺序返回**，不按完成顺序 —— 报告可复现的前提。
   慢的 case 先完成也不能打乱顺序，否则同一份 suite 跑两次报告不同。
2. **异常隔离** —— 单条 case 失败不取消其他（除非 fail_fast）。
   5 条用例里第 2 条炸了，剩下 3 条的数据不该一起消失。
3. **每 case 独立超时** —— 防止单条卡死拖垮整个 suite。
"""

from __future__ import annotations

import asyncio

import pytest

from harness.orchestration.scheduler import Scheduler


async def test_results_are_returned_in_case_order_not_completion_order():
    """报告可复现的前提 —— 慢的 case 先完成也不能打乱顺序。"""
    async def work(name: str, delay: float) -> str:
        await asyncio.sleep(delay)
        return name

    s = Scheduler(concurrency=4)
    out = await s.gather([
        ("a", lambda: work("a", 0.05)),
        ("b", lambda: work("b", 0.01)),
        ("c", lambda: work("c", 0.02)),
    ])
    assert [r for _, r in out] == ["a", "b", "c"]


async def test_concurrency_limit_is_respected():
    active = {"now": 0, "peak": 0}

    async def work():
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.02)
        active["now"] -= 1

    s = Scheduler(concurrency=3)
    await s.gather([(str(i), work) for i in range(12)])
    assert active["peak"] <= 3


async def test_all_items_actually_run():
    """并发限流不能变成"少跑几条"—— 这是最危险的静默错误。"""
    seen: list[str] = []

    async def work(name: str):
        seen.append(name)

    s = Scheduler(concurrency=2)
    out = await s.gather([(str(i), lambda i=i: work(str(i))) for i in range(9)])
    assert sorted(seen) == sorted(str(i) for i in range(9))
    assert len(out) == 9


async def test_one_failure_does_not_cancel_others():
    async def ok():
        return "ok"

    async def boom():
        raise RuntimeError("case failed")

    s = Scheduler(concurrency=2, fail_fast=False)
    out = await s.gather([("a", ok), ("b", boom), ("c", ok)])
    statuses = {k: ("error" if isinstance(v, Exception) else "ok") for k, v in out}
    assert statuses == {"a": "ok", "b": "error", "c": "ok"}


async def test_failure_keeps_its_original_type():
    """异常必须原样返回 —— 被包成别的类型会让上层的精确捕获静默失效。"""
    async def boom():
        raise ValueError("specific")

    s = Scheduler(concurrency=2)
    out = await s.gather([("a", boom)])
    assert isinstance(out[0][1], ValueError)


async def test_fail_fast_cancels_remaining():
    started: list[str] = []

    async def slow(name: str):
        started.append(name)
        await asyncio.sleep(0.5)

    async def boom():
        started.append("b")
        await asyncio.sleep(0.01)
        raise RuntimeError("boom")

    s = Scheduler(concurrency=1, fail_fast=True)
    with pytest.raises(RuntimeError):
        await s.gather([("a", boom), ("b", lambda: slow("b")), ("c", lambda: slow("c"))])
    assert "c" not in started


async def test_fail_fast_raises_the_original_exception_type():
    """TaskGroup 会把异常包成 ExceptionGroup —— 必须拆包。

    不拆的话 `pytest.raises(RuntimeError)` 失败（拿到的是 ExceptionGroup），
    上层 `except BudgetExceeded` 这类精确捕获同样静默失效。
    """
    async def boom():
        raise RuntimeError("boom")

    s = Scheduler(concurrency=1, fail_fast=True)
    with pytest.raises(RuntimeError, match="boom"):
        await s.gather([("a", boom)])


async def test_case_timeout_is_enforced():
    async def slow():
        await asyncio.sleep(10)

    s = Scheduler(concurrency=2, case_timeout_s=0.05)
    out = await s.gather([("a", slow)])
    assert isinstance(out[0][1], Exception)


async def test_timeout_of_one_case_does_not_kill_the_others():
    async def slow():
        await asyncio.sleep(10)

    async def quick():
        return "done"

    s = Scheduler(concurrency=2, case_timeout_s=0.05)
    out = await s.gather([("a", slow), ("b", quick)])
    assert isinstance(out[0][1], Exception)
    assert out[1][1] == "done"


async def test_no_timeout_when_disabled():
    async def work():
        await asyncio.sleep(0.02)
        return "ok"

    s = Scheduler(concurrency=1, case_timeout_s=None)
    assert (await s.gather([("a", work)]))[0][1] == "ok"


async def test_duplicate_keys_are_rejected():
    """重复 key 会让两条结果互相覆盖，且**没有任何报错**。

    suite 的 case_id 唯一性已经在加载期保证了，这里再挡一次是因为
    `Scheduler` 是公开类型 —— 换个调用方就可能绕过 suite 的校验。
    """
    async def work():
        return "x"

    with pytest.raises(ValueError, match="duplicate"):
        await Scheduler().gather([("a", work), ("a", work)])


async def test_empty_input_is_fine():
    assert await Scheduler().gather([]) == []


def test_concurrency_is_at_least_one():
    """0 或负数会让 Semaphore 永久挂起。"""
    assert Scheduler(concurrency=0).concurrency == 1
    assert Scheduler(concurrency=-3).concurrency == 1
