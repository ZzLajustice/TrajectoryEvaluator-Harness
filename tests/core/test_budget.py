"""预算治理测试。

## 核心语义：`BUDGET_EXCEEDED` 是**独立终态**，不是错误

    budget_exceeded   agent 有资源但没收敛（单 agent 专属的失败模式）
    llm_error         外部依赖出错，与 agent 能力无关

混为一谈会让两个指标同时失真：稳定性统计把"烧完了预算"算成"服务不稳"，
能力统计把"没找到路"算成"运气不好"。

## 预算必须在三处强制

    loop 每轮开头    turns / wall_clock —— 终止性保证，防死循环
    LLM 调用前后     input 估算 + 实际 usage 记账
    每次工具调用前   tool_calls 上限
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.contracts.protocols import ToolCall, ToolResult
from harness.contracts.results import Usage
from harness.contracts.spec import Budget, MiddlewareSpec, RunStatus
from harness.core.budget import BudgetExceeded, BudgetGovernor
from harness.core.middleware.budget import BudgetMiddleware


def _gov(budget: Budget | None = None, events: list | None = None) -> BudgetGovernor:
    ev = events if events is not None else []
    counter = [0]

    def _next_seq() -> int:
        v = counter[0]
        counter[0] += 1
        return v

    return BudgetGovernor(budget or Budget(), run_id="r1", emit=ev.append,
                          next_seq=_next_seq)


# ---- check_turn ----
def test_turn_limit_returns_max_turns_not_an_exception():
    """轮次用尽报 MAX_TURNS，不是 BUDGET_EXCEEDED、也不是异常。

    语义是「一直在行动但没收敛」，与其他资源用尽区分开。
    """
    gov = _gov(Budget(max_turns=2))
    assert gov.check_turn(0) is None
    assert gov.check_turn(1) is None
    assert gov.check_turn(2) is RunStatus.MAX_TURNS


def test_wall_clock_limit_returns_budget_exceeded():
    """时间用尽不是"没收敛"，而是资源耗尽 —— 与轮次用尽语义不同。"""
    gov = _gov(Budget(max_wall_clock_s=0.0))
    assert gov.check_turn(0) is RunStatus.BUDGET_EXCEEDED


def test_warn_threshold_emits_a_warn_event():
    events: list = []
    gov = _gov(Budget(max_turns=10, warn_at=0.8), events)
    gov.check_turn(8)
    warns = [e for e in events if e.action == "warn"]
    assert len(warns) == 1
    assert warns[0].dimension == "turns"
    assert warns[0].threshold == pytest.approx(0.8)


def test_warn_is_emitted_only_once_per_dimension():
    """重复告警会淹没事件流 —— 同一维度同一动作只发一次。"""
    events: list = []
    gov = _gov(Budget(max_turns=10, warn_at=0.8), events)
    for turn in (8, 9):
        gov.check_turn(turn)
    assert len([e for e in events if e.action == "warn"]) == 1


def test_no_warn_below_the_threshold():
    events: list = []
    gov = _gov(Budget(max_turns=10, warn_at=0.8), events)
    gov.check_turn(3)
    assert events == []


# ---- check_tool_call ----
def test_tool_call_limit_raises_budget_exceeded():
    gov = _gov(Budget(max_tool_calls=2))
    gov.check_tool_call()
    gov.check_tool_call()
    with pytest.raises(BudgetExceeded) as ei:
        gov.check_tool_call()
    assert ei.value.dimension == "tool_calls"


# ---- charge_usage ----
def test_usage_accumulates_monotonically():
    gov = _gov()
    gov.charge_usage(Usage(input_tokens=10, output_tokens=5, cost_usd=0.01))
    gov.charge_usage(Usage(input_tokens=5, output_tokens=2, cost_usd=0.01))
    snap = gov.snapshot()
    assert snap["input_tokens"] == 15
    assert snap["output_tokens"] == 7
    assert snap["usd"] == pytest.approx(0.02)


def test_usd_limit_raises_with_the_right_dimension():
    gov = _gov(Budget(max_usd=0.01))
    with pytest.raises(BudgetExceeded) as ei:
        gov.charge_usage(Usage(cost_usd=0.02))
    assert ei.value.dimension == "usd"


def test_input_token_limit_raises():
    gov = _gov(Budget(max_input_tokens=100))
    with pytest.raises(BudgetExceeded) as ei:
        gov.charge_usage(Usage(input_tokens=200))
    assert ei.value.dimension == "input_tokens"


def test_precheck_llm_rejects_before_spending():
    """调用前估算能避免"为了发现超预算而先花掉预算"。"""
    gov = _gov(Budget(max_input_tokens=100))
    gov.charge_usage(Usage(input_tokens=90))
    with pytest.raises(BudgetExceeded):
        gov.precheck_llm(est_input_tokens=50)


# ---- BudgetView ----
def test_remaining_never_goes_negative():
    gov = _gov(Budget(max_turns=5))
    gov.check_turn(99)
    assert gov.view.remaining("turns") >= 0


def test_snapshot_has_every_dimension():
    snap = _gov().snapshot()
    assert set(snap) == {"turns", "tool_calls", "input_tokens",
                         "output_tokens", "usd", "wall_clock"}


# ---- BudgetMiddleware ----
def _ctx(call: ToolCall, governor: BudgetGovernor, events: list | None = None):
    ev = events if events is not None else []
    counter = [0]

    def _next_seq() -> int:
        v = counter[0]
        counter[0] += 1
        return v

    return SimpleNamespace(call=call, scratch={}, spec=None,
                           ws=SimpleNamespace(root="."), turn=0, run_id="r1",
                           budget=governor, emit=ev.append, next_seq=_next_seq)


async def test_budget_middleware_short_circuits_when_exhausted():
    called = False

    async def downstream(ctx):
        nonlocal called
        called = True
        return ToolResult(call_id="c1", name="f", ok=True)

    gov = _gov(Budget(max_tool_calls=0))
    mw = BudgetMiddleware(MiddlewareSpec(name="budget"))
    r = await mw.handle(_ctx(ToolCall("c1", "f", {}), gov), downstream)

    assert called is False
    assert r.ok is False
    assert r.denied_by == "budget"
    assert r.error_type == "budget_exceeded"


async def test_budget_middleware_passes_through_within_budget():
    async def downstream(ctx):
        return ToolResult(call_id="c1", name="f", ok=True, content="ran")

    gov = _gov(Budget(max_tool_calls=5))
    mw = BudgetMiddleware(MiddlewareSpec(name="budget"))
    r = await mw.handle(_ctx(ToolCall("c1", "f", {}), gov), downstream)
    assert r.ok is True


async def test_budget_middleware_counts_every_call():
    async def downstream(ctx):
        return ToolResult(call_id="c1", name="f", ok=True)

    gov = _gov(Budget(max_tool_calls=3))
    mw = BudgetMiddleware(MiddlewareSpec(name="budget"))
    for i in range(3):
        await mw.handle(_ctx(ToolCall(f"c{i}", "f", {}), gov), downstream)
    assert gov.snapshot()["tool_calls"] == 3
