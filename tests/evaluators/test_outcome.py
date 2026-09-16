"""结果级评测器（跑隐藏验收测试）测试 —— 全部离线。

## 为什么这个评测器必须存在

设计文档写着「golden 是次要判据，**outcome 永远是主判据**」。
而 M1–M10 的 5 个评测器全是**轨迹级**的：它们只读事件流，
没有任何一个能回答"这份代码到底修对了没有"。

缺了它，17 条 codefix 用例只会产出一堆过程指标 ——
而一个"步数很漂亮但根本没修好"的 run 会看起来很正常。
那正是本项目声称要解决的问题本身。

## 为什么它必须能在工作目录销毁前跑

判据是"被测 agent 改出来的代码能不能通过隐藏测试"，
而那些代码只存在于工作目录里。目录一销毁，判据就没了。

## 这里测什么

用假 runner 覆盖判定逻辑（离线、零成本）；真实的
"在工作目录里跑 pytest" 由 `tests/orchestration/` 的接线测试覆盖。
"""

from __future__ import annotations

import pytest

from harness.contracts.protocols import EvalContext, ToolResult
from harness.contracts.results import EvalStatus, Severity
from harness.evaluators.outcome import OutcomeGrader
from harness.events.types import EventType
from harness.testing.builder import TrajectoryBuilder as TB


class _FakeRunner:
    """记录调用、返回预置结果。"""

    def __init__(self, result: ToolResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[list[str], float]] = []

    async def run(self, argv, *, timeout_s: float = 120.0) -> ToolResult:
        self.calls.append((list(argv), timeout_s))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _traj():
    return (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("finish", {"summary": "fixed"})])
            .tool_result(name="finish", content="done", ok=True)
            .run_end(status="ok").build())


def _ctx(runner=None) -> EvalContext:
    return EvalContext(runner=runner)


def _result(**kwargs) -> ToolResult:
    base = {"call_id": "outcome", "name": "run_command", "ok": True,
            "content": "3 passed in 0.05s"}
    base.update(kwargs)
    return ToolResult(**base)


def _grader(**config) -> OutcomeGrader:
    return OutcomeGrader(**{"argv": ["python", "-m", "pytest", "-q"], **config})


# ---- 订阅（这条最容易静默失效）----
def test_it_subscribes_to_run_end():
    """★ 空的 `subscribes` 意味着**永远不跑**。

    `run_evaluators` 的跳过判据是 `if not (cls.subscribes & present): continue`，
    所以订阅集合为空 = 每条轨迹都被跳过。而"被跳过"在报告里长得像"没问题"。
    """
    assert EventType.RUN_END in OutcomeGrader.subscribes


# ---- 判定 ----
async def test_passing_hidden_tests_are_a_pass():
    runner = _FakeRunner(_result(ok=True))
    r = await _grader().evaluate(_traj(), _ctx(runner))
    assert r.status is EvalStatus.PASS
    assert r.metrics["outcome_pass"] == 1.0
    assert r.findings == []


async def test_failing_hidden_tests_are_a_fail():
    runner = _FakeRunner(_result(ok=False, error="exit 1",
                                 content="1 failed, 2 passed"))
    r = await _grader().evaluate(_traj(), _ctx(runner))
    assert r.status is EvalStatus.FAIL
    assert r.metrics["outcome_pass"] == 0.0
    assert r.findings[0].code == "outcome.hidden_tests_failed"
    assert r.findings[0].severity is Severity.MAJOR


async def test_the_failure_finding_carries_the_test_output():
    """证据要留在 finding 里 —— 报告要能直接显示"哪条测试挂了"。"""
    runner = _FakeRunner(_result(ok=False, content="FAILED test_x - assert 1 == 2"))
    r = await _grader().evaluate(_traj(), _ctx(runner))
    assert "assert 1 == 2" in r.findings[0].data["output"]


async def test_the_summary_is_readable_without_opening_the_report():
    runner = _FakeRunner(_result(ok=False, content="1 failed"))
    r = await _grader().evaluate(_traj(), _ctx(runner))
    assert r.summary
    assert "fail" in r.summary.lower()


# ---- 配置与依赖缺失 → SKIPPED，不是 FAIL ----
async def test_missing_runner_is_skipped_not_failed():
    """★ "我没法判"与"判定为失败"是两回事。

    没有 runner 时判 FAIL 会把**配置问题**记成 agent 的失败 ——
    失败率虚高，而虚高的失败率会让真失败被淹没。
    """
    r = await _grader().evaluate(_traj(), _ctx(None))
    assert r.status is EvalStatus.SKIPPED
    assert "runner" in r.summary.lower()


async def test_missing_argv_is_skipped_not_failed():
    r = await OutcomeGrader().evaluate(_traj(), _ctx(_FakeRunner(_result())))
    assert r.status is EvalStatus.SKIPPED


# ---- 截断输出 ----
async def test_truncated_output_does_not_turn_a_pass_into_a_fail():
    """命令退出码是 0，截断不改变判定 —— 但它**必须留下痕迹**。"""
    runner = _FakeRunner(_result(ok=True, truncated=True,
                                 content="... [truncated] ..."))
    r = await _grader().evaluate(_traj(), _ctx(runner))
    assert r.status is EvalStatus.PASS
    assert any(f.code == "outcome.output_truncated" for f in r.findings)


async def test_a_broken_case_is_an_error_not_a_failure():
    """★ 隐藏测试一条都没收集到 → **用例本身坏了**，不是 agent 失败。

    pytest 对"没收集到用例"返回退出码 5，与"测试失败"（1）不同，
    但只看 `ok` 的话两者都是 False。把它们混起来，
    "任务无解但被记成模型失败"那类脏数据就会悄悄混进指标里 ——
    这正是 `tests/suites/test_cases_are_solvable.py` 在数据层挡的东西，
    这里在运行层再挡一次。
    """
    runner = _FakeRunner(_result(ok=False, content="no tests ran in 0.01s"))
    r = await _grader().evaluate(_traj(), _ctx(runner))
    assert r.status is EvalStatus.ERROR
    assert r.error


async def test_a_runner_crash_is_an_error():
    runner = _FakeRunner(RuntimeError("workspace is gone"))
    r = await _grader().evaluate(_traj(), _ctx(runner))
    assert r.status is EvalStatus.ERROR
    assert "workspace is gone" in (r.error or "")


# ---- 调用形状 ----
async def test_the_configured_argv_is_passed_through_verbatim():
    runner = _FakeRunner(_result())
    await _grader(argv=["python", "-m", "pytest", "_hidden/test_hidden.py", "-q"]
                  ).evaluate(_traj(), _ctx(runner))
    assert runner.calls[0][0] == ["python", "-m", "pytest",
                                  "_hidden/test_hidden.py", "-q"]


async def test_the_configured_timeout_is_used():
    """隐藏测试可能比 agent 的单条命令慢，所以超时是可配的。"""
    runner = _FakeRunner(_result())
    await _grader(timeout_s=300.0).evaluate(_traj(), _ctx(runner))
    assert runner.calls[0][1] == 300.0


# ---- 与指标口径的关系 ----
async def test_score_is_one_or_zero_never_a_fraction():
    """★ outcome 是二值的，不做加权。

    给"部分通过"一个 0.7 分会让它和 pass_rate / pass@k 混在一起，
    而那三个指标的语义本来就必须分列（设计文档 §4.6）。
    """
    for ok in (True, False):
        r = await _grader().evaluate(
            _traj(), _ctx(_FakeRunner(_result(ok=ok))))
        assert r.score in (0.0, 1.0)


async def test_an_empty_trajectory_is_still_graded():
    """★ 结果级评测**不需要**轨迹。

    轨迹缺失（进程被杀、只保留了部分事件）时，过程指标全都无从谈起，
    但"代码到底修好没有"仍然是个有答案的问题。
    这里刻意不返回 SKIPPED —— 那会把最需要 outcome 判据的情形排除掉。
    """
    from harness.events.trajectory import Trajectory

    r = await _grader().evaluate(Trajectory.from_events("r1", []),
                                 _ctx(_FakeRunner(_result(ok=True))))
    assert r.status is EvalStatus.PASS


@pytest.mark.parametrize("marker", ["no tests ran", "collected 0 items"])
async def test_both_pytest_no_test_markers_are_recognised(marker):
    runner = _FakeRunner(_result(ok=False, content=f"{marker} in 0.01s"))
    r = await _grader().evaluate(_traj(), _ctx(runner))
    assert r.status is EvalStatus.ERROR
