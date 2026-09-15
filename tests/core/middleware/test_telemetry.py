"""TelemetryMW 测试。

## 两条不可违反的规则（设计文档 R3）

1. **异常路径也必须落事件。**
   否则评测器看到「有 TOOL_CALL 无 TOOL_RESULT」的悬空配对 ——
   而这种损坏是**沉默的**：GroundingChecker 失去判据、EfficiencyAnalyzer
   漏计最严重的失败、TrajectoryMatcher 的长度断言产生误导。

2. **`CancelledError` 必须继续传播。**
   它继承自 `BaseException`，所以 `except Exception` 天然不会捕获它，
   但绝不能写成 `except BaseException` —— 并发场景下吞掉取消会让任务悬挂。

## 记录者 vs 决策者的分工

TelemetryMW 只负责**记录**：异常路径先发事件，然后**原样上抛**。
**决策**由 `RunContext.invoke_tool` 做（转成 `sandbox_error` 并让 loop 继续）。
中间件不知道上层想怎么处理异常，把策略固化在中间件层是错的。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from harness.contracts.protocols import ToolCall, ToolResult
from harness.contracts.spec import MiddlewareSpec
from harness.core.middleware.telemetry import TelemetryMiddleware


def _ctx(call: ToolCall, events: list, seq: list | None = None):
    counter = seq if seq is not None else [0]

    def _next_seq() -> int:
        v = counter[0]
        counter[0] += 1
        return v

    return SimpleNamespace(call=call, scratch={}, emit=events.append,
                           turn=0, run_id="r1", next_seq=_next_seq)


async def _ok(ctx):
    return ToolResult(call_id=ctx.call.call_id, name=ctx.call.name, ok=True, content="out")


async def _boom(ctx):
    raise RuntimeError("sandbox exploded")


def _results(events: list) -> list:
    return [e for e in events if e.type.value == "tool.result"]


async def test_successful_call_emits_one_result_event():
    events: list = []
    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    await mw.handle(_ctx(ToolCall("c1", "read_file", {}), events), _ok)
    results = _results(events)
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].content == "out"


async def test_exception_still_emits_a_result_event_before_propagating():
    """★ 异常路径的埋点 —— 漏掉它评测器就会看到悬空配对。"""
    events: list = []
    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    with pytest.raises(RuntimeError, match="exploded"):
        await mw.handle(_ctx(ToolCall("c1", "read_file", {}), events), _boom)
    results = _results(events)
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error_type == "middleware_error"
    assert "exploded" in (results[0].error or "")


async def test_exception_is_reraised_not_swallowed():
    """记录者不是决策者 —— 中间件不该替上层决定怎么处理异常。"""
    events: list = []
    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    with pytest.raises(RuntimeError):
        await mw.handle(_ctx(ToolCall("c1", "f", {}), events), _boom)


async def test_cancelled_error_is_not_swallowed():
    """CancelledError 是 BaseException —— 吞掉它会导致并发任务悬挂。"""
    events: list = []

    async def cancelled(ctx):
        raise asyncio.CancelledError

    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    with pytest.raises(asyncio.CancelledError):
        await mw.handle(_ctx(ToolCall("c1", "f", {}), events), cancelled)


async def test_cancelled_error_does_not_emit_a_fake_result_event():
    """取消不是"工具失败" —— 不该伪造成一条 ok=False 的结果。"""
    events: list = []

    async def cancelled(ctx):
        raise asyncio.CancelledError

    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    with pytest.raises(asyncio.CancelledError):
        await mw.handle(_ctx(ToolCall("c1", "f", {}), events), cancelled)
    assert _results(events) == []


async def test_denied_result_is_also_recorded():
    """被中间件拦下的调用也必须留下结果事件 —— 否则同样是悬空配对。"""
    events: list = []

    async def denied(ctx):
        return ToolResult(call_id=ctx.call.call_id, name=ctx.call.name, ok=False,
                          denied_by="permission", error_type="permission_denied")

    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    await mw.handle(_ctx(ToolCall("c1", "f", {}), events), denied)
    results = _results(events)
    assert len(results) == 1
    assert results[0].denied_by == "permission"


async def test_event_carries_seq_and_run_id():
    events: list = []
    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    await mw.handle(_ctx(ToolCall("c1", "f", {}), events), _ok)
    assert _results(events)[0].run_id == "r1"
    assert _results(events)[0].seq == 0


async def test_duration_is_recorded_when_tool_does_not_set_it():
    events: list = []
    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    await mw.handle(_ctx(ToolCall("c1", "f", {}), events), _ok)
    assert _results(events)[0].duration_ms >= 0


async def test_tool_supplied_duration_is_preserved():
    """工具自己测的耗时更准（它知道哪部分算执行），不该被覆盖。"""
    events: list = []

    async def timed(ctx):
        return ToolResult(call_id="c1", name="f", ok=True, duration_ms=1234)

    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    await mw.handle(_ctx(ToolCall("c1", "f", {}), events), timed)
    assert _results(events)[0].duration_ms == 1234


async def test_truncation_flag_is_propagated():
    events: list = []

    async def truncated(ctx):
        return ToolResult(call_id="c1", name="f", ok=True, truncated=True)

    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    await mw.handle(_ctx(ToolCall("c1", "f", {}), events), truncated)
    assert _results(events)[0].truncated is True
