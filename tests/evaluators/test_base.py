"""评测器框架测试。

## 声明式订阅必须是**真行为**，不只是文档

`subscribes` 声明"我关心哪些事件"。调度器据此**跳过**不需要的评测器 ——
不实例化、不调用。测试用计数器证明这一点：如果只是文档承诺，
计数器会显示它还是跑了。

## ERROR 与 FAIL 严格区分

评测器自身抛异常记为 `ERROR`，判定失败记为 `FAIL`。
混淆会让「评测器有 bug」被记成「被测 agent 有问题」——
这是最会污染整份报告的失败方式。
"""

from __future__ import annotations

import pytest

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalResult, EvalStatus, Finding, Severity
from harness.evaluators.base import BaseEvaluator, run_evaluators
from harness.events.trajectory import Trajectory
from harness.events.types import ContextCompactEvent, EventType, RunEndEvent, RunStartEvent


def _traj(*types: EventType) -> Trajectory:
    events = []
    for i, t in enumerate(types):
        if t is EventType.RUN_START:
            events.append(RunStartEvent(run_id="r1", seq=i, type=t,
                                        role="sut", model="m", provider="fake"))
        elif t is EventType.RUN_END:
            events.append(RunEndEvent(run_id="r1", seq=i, type=t, status="ok"))
        elif t is EventType.CONTEXT_COMPACT:
            events.append(ContextCompactEvent(
                run_id="r1", seq=i, type=t, reason="token_pressure",
                messages_before=10, messages_after=4, tokens_before=9000,
                tokens_after=3000, dropped_message_digests=["d1"],
                strategy="drop_oldest_groups"))
    return Trajectory.from_events("r1", events)


class _Spy(BaseEvaluator):
    name = "spy"
    subscribes = frozenset({EventType.CONTEXT_COMPACT})
    calls = 0

    def evaluate(self, traj: Trajectory, ctx: EvalContext) -> EvalResult:
        type(self).calls += 1
        return EvalResult(evaluator=self.name, run_id=traj.run_id, status=EvalStatus.PASS)


class _Boom(BaseEvaluator):
    name = "boom"
    subscribes = frozenset({EventType.RUN_END})

    def evaluate(self, traj: Trajectory, ctx: EvalContext) -> EvalResult:
        raise RuntimeError("evaluator bug")


# ---- 声明式订阅 ----
async def test_evaluator_is_skipped_when_its_event_is_absent():
    """★ 这是「声明式订阅」从文档变成行为的证据。"""
    _Spy.calls = 0
    results = await run_evaluators([_Spy], _traj(EventType.RUN_START, EventType.RUN_END),
                                   EvalContext())
    assert results == []
    assert _Spy.calls == 0, "订阅的事件不存在时不该调用评测器"


async def test_evaluator_runs_when_its_event_is_present():
    _Spy.calls = 0
    results = await run_evaluators(
        [_Spy], _traj(EventType.RUN_START, EventType.CONTEXT_COMPACT, EventType.RUN_END),
        EvalContext())
    assert len(results) == 1
    assert _Spy.calls == 1


async def test_evaluator_with_empty_subscribes_never_runs():
    """空订阅 = 对什么都不感兴趣 —— 不该跑。"""

    class _Never(BaseEvaluator):
        name = "never"
        subscribes = frozenset()

        def evaluate(self, traj, ctx):  # pragma: no cover
            raise AssertionError("should never be called")

    assert await run_evaluators([_Never], _traj(EventType.RUN_END), EvalContext()) == []


async def test_empty_trajectory_skips_everything():
    assert await run_evaluators([_Spy], Trajectory.from_events("r1", []),
                                EvalContext()) == []


# ---- 异常处理 ----
async def test_evaluator_exception_becomes_error_status_not_a_crash():
    """★ 评测器崩了不能拖垮整个评测 —— 但必须明确记成 ERROR。"""
    results = await run_evaluators([_Boom], _traj(EventType.RUN_END), EvalContext())
    assert len(results) == 1
    assert results[0].status is EvalStatus.ERROR
    assert results[0].status is not EvalStatus.FAIL
    assert "evaluator bug" in (results[0].error or "")


async def test_one_failing_evaluator_does_not_stop_the_others():
    results = await run_evaluators([_Boom, _Spy],
                                   _traj(EventType.CONTEXT_COMPACT, EventType.RUN_END),
                                   EvalContext())
    statuses = {r.evaluator: r.status for r in results}
    assert statuses == {"boom": EvalStatus.ERROR, "spy": EvalStatus.PASS}


# ---- 同步 / 异步 ----
async def test_sync_and_async_evaluators_both_work():
    class _Sync(BaseEvaluator):
        name = "sync"
        subscribes = frozenset({EventType.RUN_END})

        def evaluate(self, traj, ctx):
            return EvalResult(evaluator=self.name, run_id=traj.run_id,
                              status=EvalStatus.PASS)

    class _Async(BaseEvaluator):
        name = "async"
        subscribes = frozenset({EventType.RUN_END})

        async def evaluate(self, traj, ctx):
            return EvalResult(evaluator=self.name, run_id=traj.run_id,
                              status=EvalStatus.PASS)

    results = await run_evaluators([_Sync, _Async], _traj(EventType.RUN_END),
                                   EvalContext())
    assert {r.evaluator for r in results} == {"sync", "async"}


async def test_duration_is_recorded():
    results = await run_evaluators([_Spy],
                                   _traj(EventType.CONTEXT_COMPACT, EventType.RUN_END),
                                   EvalContext())
    assert results[0].duration_ms >= 0


# ---- 基类契约 ----
def test_skipped_helper_produces_a_skipped_result():
    cm = _Spy()
    r = cm.skipped(Trajectory.from_events("r1", []), "no data")
    assert r.status is EvalStatus.SKIPPED
    assert r.summary == "no data"


def test_finding_defaults_to_minor_severity():
    assert Finding(code="x", message="y").severity is Severity.MINOR


def test_base_evaluator_requires_evaluate():
    with pytest.raises(NotImplementedError):
        BaseEvaluator().evaluate(Trajectory.from_events("r1", []), EvalContext())


def test_base_evaluator_has_a_default_version():
    assert _Spy().version


# ---- 架构约束（评测器不得依赖 core）----
def test_evaluator_base_does_not_import_core():
    """这是 M5 要守住的架构底线。

    违反它，「评测器与 agent 零耦合」就只是文档承诺。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "harness" / "evaluators"
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for forbidden in ("harness.core", "harness.orchestration", "harness.store",
                          "harness.providers", "harness.cli"):
            assert forbidden not in text, f"{py.name} imports {forbidden}"
