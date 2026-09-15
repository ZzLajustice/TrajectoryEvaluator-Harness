"""洋葱管道测试。

**顺序语义错了，评测埋点、越权检测、预算控制会全部静默失效。**
因此本文件的核心是那条顺序断言 —— 它是整个中间件设计的回归保护。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.contracts.protocols import ToolCall, ToolResult
from harness.core.middleware.base import PrePostMiddleware
from harness.core.pipeline import build_pipeline


class Recording(PrePostMiddleware):
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    async def before(self, ctx: object) -> ToolResult | None:
        self._log.append(f"{self.name}.before")
        return None

    async def after(self, ctx: object, result: ToolResult) -> ToolResult:
        self._log.append(f"{self.name}.after")
        return result


class ShortCircuit(PrePostMiddleware):
    name = "short"

    async def before(self, ctx: object) -> ToolResult | None:
        call = ctx.call  # type: ignore[attr-defined]
        return ToolResult(call_id=call.call_id, name=call.name, ok=False,
                          denied_by="permission", error_type="permission_denied")


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(call=ToolCall("c1", "t", {}), scratch={})


def _terminal(log: list[str]):
    async def terminal(ctx: object) -> ToolResult:
        log.append("executor")
        call = ctx.call  # type: ignore[attr-defined]
        return ToolResult(call_id=call.call_id, name=call.name, ok=True, content="ran")
    return terminal


async def test_execution_order_is_outside_in_then_inside_out():
    log: list[str] = []
    chain = build_pipeline([Recording("a", log), Recording("b", log)], _terminal(log))
    await chain(_ctx())
    assert log == ["a.before", "b.before", "executor", "b.after", "a.after"]


async def test_first_middleware_is_outermost():
    """middlewares[0] 必须是最外层。

    规范顺序是 TOOL_CALL → Permission → Sandbox → Budget → Telemetry → Policy → Executor。
    """
    log: list[str] = []
    chain = build_pipeline([Recording("outer", log), Recording("inner", log)], _terminal(log))
    await chain(_ctx())
    assert log.index("outer.before") < log.index("inner.before")
    assert log.index("outer.after") > log.index("inner.after")


async def test_short_circuit_skips_terminal():
    log: list[str] = []
    chain = build_pipeline([ShortCircuit()], _terminal(log))
    result = await chain(_ctx())
    assert "executor" not in log
    assert result.denied_by == "permission"


async def test_short_circuit_still_returns_complete_result():
    """短路必须返回完整的 ToolResult（call_id / name 都不能缺）。

    否则评测器会看到悬空的 TOOL_CALL——TOOL_RESULT 配对。
    """
    chain = build_pipeline([ShortCircuit()], _terminal([]))
    r = await chain(_ctx())
    assert r.call_id == "c1"
    assert r.name == "t"
    assert r.ok is False


async def test_empty_middleware_list_calls_terminal_directly():
    chain = build_pipeline([], _terminal([]))
    assert (await chain(_ctx())).content == "ran"


async def test_exception_propagates_through_chain():
    """中间件抛异常时不得吞掉 —— 决策权在 RunContext，不在中间件。"""

    class Boom(PrePostMiddleware):
        name = "boom"

        async def before(self, ctx: object) -> ToolResult | None:
            raise RuntimeError("kaboom")

    chain = build_pipeline([Boom()], _terminal([]))
    with pytest.raises(RuntimeError, match="kaboom"):
        await chain(_ctx())


async def test_outer_after_runs_even_when_inner_completes():
    log: list[str] = []
    chain = build_pipeline([Recording("a", log), Recording("b", log)], _terminal(log))
    await chain(_ctx())
    assert log[-1] == "a.after"


async def test_pipeline_is_reusable_across_calls():
    """管道在 Run.__init__ 只构建一次，复用整个 run。"""
    log: list[str] = []
    chain = build_pipeline([Recording("a", log)], _terminal(log))
    await chain(_ctx())
    await chain(_ctx())
    assert log.count("a.before") == 2
    assert log.count("executor") == 2
