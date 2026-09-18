"""结果聚合测试。

## 三个指标必须分列，且各自的语义要写死

    pass_rate    所有 repeat 都通过
    pass@k       k 次中至少一次通过
    flaky_rate   通过率严格落在 (0,1) 之间的 case 占比

`pass@k` 在本项目里是**「至少一次通过的 case 占全部 case 的比例」**，
不是经典 pass@k 那个无偏估计量。这是刻意选的：报告要回答的是
「有多少用例存在可行解」，而不是「采样 k 次的期望通过率」。
名字沿用业界叫法，语义在报告脚注里写明 —— 不写就会有歧义。

`flaky` 的判定边界是本文件最容易写错的地方：全过不算 flaky、全不过也不算。
"""

from __future__ import annotations

import json

import pytest

from harness.contracts.spec import RunStatus
from harness.orchestration.aggregator import (
    CASE_FLAKY,
    CASE_OK,
    CaseOutcome,
    aggregate,
    read_snapshot,
    to_case_outcomes,
    write_snapshot,
)


def _o(case_id: str, *statuses: RunStatus, golden: float | None = None) -> CaseOutcome:
    return CaseOutcome(case_id=case_id, statuses=list(statuses), golden_score=golden,
                       cost_usd=0.01, turns=3, tool_calls=4)


def test_pass_rate_requires_all_repeats_to_pass():
    o = aggregate([_o("a", RunStatus.OK, RunStatus.OK),
                   _o("b", RunStatus.OK, RunStatus.NO_FINISH)])
    assert o["pass_rate"] == 0.5


def test_pass_at_k_is_at_least_one_success():
    o = aggregate([_o("a", RunStatus.OK, RunStatus.NO_FINISH)])
    assert o["pass@k"] == 1.0


def test_flaky_rate_counts_partial_success_cases():
    o = aggregate([_o("a", RunStatus.OK, RunStatus.NO_FINISH),
                   _o("b", RunStatus.OK, RunStatus.OK),
                   _o("c", RunStatus.NO_FINISH, RunStatus.NO_FINISH)])
    # 3 个 case 里只有 a 是 flaky
    assert o["flaky_rate"] == pytest.approx(1 / 3)


def test_flaky_cases_are_listed_explicitly_not_averaged_away():
    o = aggregate([_o("a", RunStatus.OK, RunStatus.NO_FINISH)])
    assert o["flaky_cases"] == ["a"]


def test_all_pass_is_not_flaky():
    """全过不是 flaky —— 边界写成 `<= 0` 会把稳定的用例也报成 flaky。"""
    o = aggregate([_o("a", RunStatus.OK, RunStatus.OK)])
    assert o["flaky_rate"] == 0.0
    assert o["flaky_cases"] == []


def test_all_fail_is_not_flaky():
    """全不过也不是 flaky —— 它是**稳定的失败**，比 flaky 好办得多。

    把"稳定失败"混进 flaky 会掩盖真正的不确定性。
    """
    o = aggregate([_o("a", RunStatus.NO_FINISH, RunStatus.NO_FINISH)])
    assert o["flaky_rate"] == 0.0
    assert o["flaky_cases"] == []


def test_golden_score_is_reported_separately_from_outcome():
    o = aggregate([_o("a", RunStatus.OK, golden=0.5)])
    assert "golden_score_mean" in o
    assert o["golden_score_mean"] == 0.5
    assert "pass_rate" in o  # 两者不合并


def test_golden_score_mean_ignores_cases_without_one():
    """没跑轨迹匹配的 case 不该被当成 0 分拉低均值 —— 那是"不适用"，不是"零分"。"""
    o = aggregate([_o("a", RunStatus.OK, golden=1.0),
                   _o("b", RunStatus.OK, golden=None)])
    assert o["golden_score_mean"] == 1.0


def test_status_distribution_is_reported():
    o = aggregate([_o("a", RunStatus.OK), _o("b", RunStatus.BUDGET_EXCEEDED),
                   _o("c", RunStatus.LLM_ERROR)])
    assert o["status_distribution"]["ok"] == 1
    assert o["status_distribution"]["budget_exceeded"] == 1


def test_total_cost_and_tokens_are_summed():
    o = aggregate([_o("a", RunStatus.OK), _o("b", RunStatus.OK)])
    assert o["total_cost_usd"] == pytest.approx(0.02)


def test_empty_outcomes_do_not_divide_by_zero():
    o = aggregate([])
    assert o["pass_rate"] == 0.0
    assert o["flaky_rate"] == 0.0


def test_case_outcome_pass_rate_with_no_statuses():
    assert CaseOutcome(case_id="x", statuses=[]).pass_rate == 0.0


def test_flaky_cases_are_sorted_for_a_stable_report():
    o = aggregate([_o("z", RunStatus.OK, RunStatus.NO_FINISH),
                   _o("a", RunStatus.OK, RunStatus.NO_FINISH)])
    assert o["flaky_cases"] == ["a", "z"]


# ---- 从真实 run 结果搭桥 ----
def _run_outcome(case_id: str, status: RunStatus, *, repeat: int = 0,
                 run_id: str = "r1", score: float | None = None,
                 cost: float = 0.0, evaluator: str = "TrajectoryMatcher"):
    from harness.contracts.results import EvalResult, EvalStatus, Usage
    from harness.core.run import RunResult
    from harness.events.trajectory import Trajectory

    traj = Trajectory.from_events(run_id, [])
    result = RunResult(run_id=run_id, status=status, final_output=None,
                       trajectory=traj, usage=Usage(cost_usd=cost), turns=2,
                       tool_calls=3, duration_s=0.1)
    evals = []
    if score is not None:
        evals = [EvalResult(evaluator=evaluator, run_id=run_id,
                            status=EvalStatus.PASS, score=score)]
    from harness.orchestration.deps import RunOutcome

    return RunOutcome(result=result, evals=evals, case_id=case_id,
                      repeat_index=repeat)


def test_repeats_of_a_case_are_grouped_together():
    """`--repeat 3` 的三条 run 必须合成**一个** case —— 否则 flaky 永远算不出来。"""
    outcomes = [_run_outcome("a", RunStatus.OK, repeat=0),
                _run_outcome("a", RunStatus.NO_FINISH, repeat=1),
                _run_outcome("b", RunStatus.OK, repeat=0)]
    cases = to_case_outcomes(outcomes)
    assert [c.case_id for c in cases] == ["a", "b"]
    assert cases[0].statuses == [RunStatus.OK, RunStatus.NO_FINISH]


def test_bridge_sums_cost_across_repeats():
    cases = to_case_outcomes([_run_outcome("a", RunStatus.OK, repeat=0, cost=0.1),
                              _run_outcome("a", RunStatus.OK, repeat=1, cost=0.2)])
    assert cases[0].cost_usd == pytest.approx(0.3)


def test_bridge_takes_the_golden_score_from_the_matcher():
    from harness.contracts.results import EvalResult, EvalStatus

    class _Fake:
        pass

    o = _run_outcome("a", RunStatus.OK, score=0.5)
    o.evals.append(EvalResult(evaluator="EfficiencyAnalyzer", run_id="r1",
                              status=EvalStatus.PASS, score=0.0))
    cases = to_case_outcomes([o])
    # 过程分只认轨迹匹配 —— 把效率分混进来会让 "golden_score" 语义失焦
    assert cases[0].golden_score == 0.5


def test_bridge_is_stable_under_input_reordering():
    """同一批 run 换个顺序，聚合结果必须一样 —— 否则报告 diff 全是噪声。"""
    a = to_case_outcomes([_run_outcome("b", RunStatus.OK, run_id="r2"),
                          _run_outcome("a", RunStatus.OK, run_id="r1")])
    b = to_case_outcomes([_run_outcome("a", RunStatus.OK, run_id="r1"),
                          _run_outcome("b", RunStatus.OK, run_id="r2")])
    assert [c.case_id for c in a] == [c.case_id for c in b] == ["a", "b"]

    agg_a = aggregate(a)
    agg_b = aggregate(b)
    assert agg_a["pass_rate"] == agg_b["pass_rate"]
    assert agg_a["flaky_cases"] == agg_b["flaky_cases"]


# ---- 快照（diff / ci 的输入）----
def test_snapshot_roundtrip(tmp_path):
    path = tmp_path / "latest.json"
    write_snapshot(aggregate([_o("a", RunStatus.OK, golden=1.0)]),
                   [_o("a", RunStatus.OK)], path, suite_name="demo",
                   fingerprints={"a": "fp1"})
    snap = read_snapshot(path)
    assert snap["suite_name"] == "demo"
    assert snap["runs"][0]["case_id"] == "a"
    assert snap["runs"][0]["spec_fingerprint"] == "fp1"


def test_snapshot_marks_flaky_cases_with_a_flaky_status():
    """Case 级状态是 diff 的输入 —— `ok`/`fail`/`flaky` 三态。

    **`flaky` 不是 RunStatus 的成员**：它是 case 级聚合出来的概念，
    单条 run 永远不会"flaky"。把它塞进 RunStatus 会让 run 的终态语义变浑。
    """
    cases = [_o("a", RunStatus.OK, RunStatus.NO_FINISH),
             _o("b", RunStatus.OK, RunStatus.OK),
             _o("c", RunStatus.NO_FINISH, RunStatus.NO_FINISH)]
    rows = write_snapshot(aggregate(cases), cases, None)["runs"]
    by_id = {r["case_id"]: r["status"] for r in rows}
    assert by_id == {"a": "flaky", "b": "ok", "c": "fail"}


def test_snapshot_has_a_fingerprint_per_case():
    """模型/prompt 变了的 case 不可比 —— 快照必须带上指纹。"""
    rows = write_snapshot(aggregate([_o("a", RunStatus.OK)]),
                          [_o("a", RunStatus.OK)], None,
                          fingerprints={"a": "abc123"})["runs"]
    assert rows[0]["spec_fingerprint"] == "abc123"


def test_snapshot_omits_fingerprint_when_unknown():
    """没有指纹时留空，而不是编一个 —— 编的指纹会让两个不同实验被判定为可比。"""
    rows = write_snapshot(aggregate([_o("a", RunStatus.OK)]),
                          [_o("a", RunStatus.OK)], None)["runs"]
    assert rows[0]["spec_fingerprint"] is None


def test_missing_snapshot_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_snapshot(tmp_path / "nope.json")


def test_snapshot_is_valid_json_with_utf8_names(tmp_path):
    import pathlib

    path = pathlib.Path(tmp_path / "s.json")
    write_snapshot(aggregate([_o("中文用例", RunStatus.OK)]),
                   [_o("中文用例", RunStatus.OK)], path, suite_name="中文套件")
    raw = path.read_text(encoding="utf-8")
    assert "中文用例" in raw, "中文不该被转义成 \\uXXXX"
    assert json.loads(raw)["suite_name"] == "中文套件"


# ---- 通过率的基准：结果级优先于 run 终态 ----
def _with_outcome(case_id: str, status: RunStatus, outcome: str,
                  *, repeat: int = 0):
    """一条带 `OutcomeGrader` 结果的 run。`outcome` 取 pass/fail/skipped/error。"""
    from harness.contracts.results import EvalResult, EvalStatus, Usage
    from harness.core.run import RunResult
    from harness.events.trajectory import Trajectory
    from harness.orchestration.deps import RunOutcome

    traj = Trajectory.from_events(f"r{repeat}", [])
    result = RunResult(run_id=f"r{repeat}", status=status, final_output=None,
                       trajectory=traj, usage=Usage(), turns=2, tool_calls=3,
                       duration_s=0.1)
    evals = [EvalResult(evaluator="OutcomeGrader", run_id=f"r{repeat}",
                        status=EvalStatus(outcome), score=None)]
    return RunOutcome(result=result, evals=evals, case_id=case_id,
                      repeat_index=repeat)


def test_outcome_wins_over_the_run_terminal_state():
    """★ 这条盯的是一个真实误报过的数字。

    真模型跑 17 条用例：`OutcomeGrader` 说 12 条修好了，而按 run 终态只有 4 条
    （模型修完 bug 就继续干活直到轮次耗尽，从不调 finish）。
    按终态算出来的 `pass_rate` 是 0.235，按结果算才是 0.706。

    只看终态等于把"agent 没说收工"记成"没修好" —— 而设计文档写着
    outcome 永远是主判据。
    """
    cases = to_case_outcomes([
        _with_outcome("a", RunStatus.MAX_TURNS, "pass"),   # 修好了但没调 finish
    ])
    assert cases[0].pass_rate == 1.0
    assert cases[0].pass_basis == "outcome"
    assert cases[0].case_status == CASE_OK


def test_run_status_is_still_the_fallback_without_an_outcome_grader():
    """没有结果级评测器的 suite（如 `examples/traps.yaml`）照旧按终态算。"""
    cases = to_case_outcomes([_run_outcome("a", RunStatus.MAX_TURNS)])
    assert cases[0].pass_rate == 0.0
    assert cases[0].pass_basis == "run_status"


@pytest.mark.parametrize("state", ["skipped", "error"])
def test_unjudgeable_outcomes_are_excluded_not_counted_as_failures(state):
    """★ "判不了"与"没通过"是两回事。

    SKIPPED（没配结果级评测器）与 ERROR（用例本身坏了）都算判不了。
    把它们计成失败会让 pass_rate 虚低 —— 而虚低的通过率会把真失败淹没。
    """
    cases = to_case_outcomes([_with_outcome("a", RunStatus.OK, state)])
    assert cases[0].outcome_flags == [None]
    # 判不了 → 退回 run 终态（OK → 通过），而不是记成失败
    assert cases[0].pass_basis == "run_status"
    assert cases[0].pass_rate == 1.0


def test_status_distribution_still_reports_the_terminal_states():
    """★ 终态没有被丢掉 —— 它只是不再冒充通过率。

    "有没有正常收尾"是有价值的过程信号（12/17 的 run 都是 max_turns），
    报告要看得见它。
    """
    agg = aggregate(to_case_outcomes([
        _with_outcome("a", RunStatus.MAX_TURNS, "pass"),
        _with_outcome("b", RunStatus.OK, "fail"),
    ]))
    assert agg["status_distribution"] == {"max_turns": 1, "ok": 1}
    assert agg["pass_basis"] == "outcome"
    assert agg["pass_rate"] == 0.5      # a 通过、b 没通过
    assert agg["pass@k"] == 0.5


def test_flaky_is_judged_on_the_same_basis_as_pass_rate():
    """同一条 case 的多次 repeat 判定不一致 = flaky，无论基准是哪个。"""
    cases = to_case_outcomes([
        _with_outcome("a", RunStatus.OK, "pass", repeat=0),
        _with_outcome("a", RunStatus.OK, "fail", repeat=1),
    ])
    assert cases[0].case_status == CASE_FLAKY
    assert aggregate(cases)["flaky_rate"] == 1.0
