"""M9 端到端验收：双 Harness 对称与元评测。

## 本文件要证明的核心命题

**judge 是一个真正的 agent run，与被测 agent 走同一条代码路径。**

这不是"看起来像"—— 判据是：judge 的轨迹用的是同一个 `Trajectory` 视图、
同一套事件类型、同一套中间件与预算机制，且**能被 `harness trace` 原样读出来**。

计划里的验收方式是"跑一条用例，然后对比 sut 与 judge 的轨迹事件结构"——
下面 `test_sut_and_judge_trajectories_are_isomorphic` 把这件事自动化了。

## 为什么值得写成测试而不是人眼看一次

对称性是**架构主张**。主张如果只写在文档里，半年后有人加一个
`JudgeRun(Run)` 子类就悄悄破功了，而且不会有任何症状 ——
直到有人问"你凭什么相信 judge 判得对"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.events.trajectory import Trajectory
from harness.orchestration.deps import RunBuilder

REPO_ROOT = Path(__file__).resolve().parents[2]
JUDGED = REPO_ROOT / "examples" / "judged.yaml"

# 一条 run 的完整事件词汇表（与 events/types.py 的 EventType 一致）
_RUN_EVENT_TYPES = {
    "run.start", "turn.start", "llm.request", "llm.response",
    "tool.call", "tool.result", "run.end",
}


def _run(tmp_path, **kw):
    # **kw 而不是 *extra：`run_suite_sync(suite, evaluate=True, *extra)`
    # 是"关键字参数之后再解包位置参数"，Python 允许但极易读错，
    # 而且一旦 extra 里混进位置参数就会撞车。
    return RunBuilder(out_dir=tmp_path / "runs",
                      workdir=tmp_path / "wd").run_suite_sync(
        JUDGED, evaluate=True, **kw)


def _events(runs_dir: Path, run_id: str) -> list[dict]:
    path = runs_dir / f"{run_id}.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _all_trajectories(runs_dir: Path) -> dict[str, list[dict]]:
    return {p.stem: _events(runs_dir, p.stem) for p in runs_dir.glob("*.jsonl")}


# ---- 对称性 ----
def test_a_judge_run_appears_alongside_the_sut_run(tmp_path):
    """Judge 的轨迹落在**同一个 store** 里 —— 否则它无法被 trace、被审计。"""
    outcomes = _run(tmp_path)
    assert outcomes

    trajs = _all_trajectories(tmp_path / "runs")
    roles = {t[0]["role"] for t in trajs.values() if t}
    assert "sut" in roles
    assert "judge" in roles, f"没有 judge 轨迹；实际 roles={roles}"


def test_sut_and_judge_trajectories_are_isomorphic(tmp_path):
    """★ M9 的核心验收：两条轨迹的事件结构同构。

    判据是**同一个 `Run` 类产出的**：同样的事件词汇表、同样的字段形状、
    同样能被 `Trajectory` 读出来。计划里的手工对比在这一条里自动化了。
    """
    _run(tmp_path)
    trajs = _all_trajectories(tmp_path / "runs")
    sut = next(t for t in trajs.values() if t and t[0]["role"] == "sut")
    judge = next(t for t in trajs.values() if t and t[0]["role"] == "judge")

    sut_types = {e["type"] for e in sut}
    judge_types = {e["type"] for e in judge}

    # judge 也是完整的一轮 agent 生命周期，不是"一段文本"
    assert {"run.start", "turn.start", "llm.request", "llm.response",
            "run.end"} <= judge_types
    # 它调了工具（read_trajectory）—— 这正是 Agent-as-a-Judge 的意义
    assert {"tool.call", "tool.result"} <= judge_types
    # 二者用的是同一个事件词汇表，没有 judge 专属的事件类型
    assert judge_types <= _RUN_EVENT_TYPES
    assert sut_types <= _RUN_EVENT_TYPES


def test_both_trajectories_parse_through_the_same_view(tmp_path):
    """同构的可执行判据：同一个 `Trajectory` 类能吃下两条轨迹。"""
    _run(tmp_path)
    runs_dir = tmp_path / "runs"
    for path in runs_dir.glob("*.jsonl"):
        traj = Trajectory.from_jsonl(path.read_text(encoding="utf-8"))
        assert traj.start() is not None
        assert traj.end() is not None


def test_the_judge_actually_used_its_tool(tmp_path):
    """★ 如果 judge 没调工具，那它就只是"单次调用 + 长 prompt"。

    这条是 M9 里最容易被做假的验收项：judge 跑起来了、给了判定、
    指标都有了 —— 但它可能根本没去查。工具调用是"它真的是 agent"的证据。
    """
    _run(tmp_path)
    judge = next(t for t in _all_trajectories(tmp_path / "runs").values()
                 if t and t[0]["role"] == "judge")
    tool_names = [e["name"] for e in judge if e["type"] == "tool.call"]
    assert "read_trajectory" in tool_names, f"judge 没查过轨迹：{tool_names}"


def test_judge_runs_are_separate_from_sut_runs(tmp_path):
    """Judge 的 run_id 与 sut 不重叠 —— 两套轨迹不能互相覆盖。"""
    _run(tmp_path)
    trajs = _all_trajectories(tmp_path / "runs")
    sut_ids = {r for r, t in trajs.items() if t and t[0]["role"] == "sut"}
    judge_ids = {r for r, t in trajs.items() if t and t[0]["role"] == "judge"}
    assert not (sut_ids & judge_ids)


def test_judge_role_and_agent_name_are_recorded(tmp_path):
    """角色必须落在轨迹里 —— 事后审计要能一眼分清哪条是判官。

    而且 `spec_json` 里要能查到 `agent_name=judge:...`：
    轨迹是自解释的，不该需要回查 suite 才知道这条 run 是谁。
    """
    _run(tmp_path)
    judge = next(t for t in _all_trajectories(tmp_path / "runs").values()
                 if t and t[0]["role"] == "judge")
    assert judge[0]["role"] == "judge"
    spec = json.loads(judge[0]["spec_json"])
    assert spec["agent_name"].startswith("judge")
    assert spec["role"] == "judge"


# ---- 元评测 ----
def _meta_evals(outcome):
    return [ev for ev in outcome.evals if ev.evaluator == "MetaEvaluator"]


def test_meta_evaluation_runs_over_the_judge_trajectory(tmp_path):
    outcomes = _run(tmp_path)
    outcome = next(o for o in outcomes if o.case_id == "judged_clean")
    metas = _meta_evals(outcome)
    assert metas, "MetaEvaluator 没有产出结果"


def test_a_consistent_judge_reports_full_consistency(tmp_path):
    """三条 judge 响应完全相同 → 一致性必须是 100%。"""
    outcomes = _run(tmp_path)
    outcome = next(o for o in outcomes if o.case_id == "judged_clean")
    meta = _meta_evals(outcome)[0]

    assert meta.metrics["judge_verdict_count"] == 3.0
    assert meta.metrics["judge_consistency"] == 1.0
    assert not [f for f in meta.findings if f.code == "meta.judge_inconsistent"]


def test_meta_evaluation_reports_judge_cost(tmp_path):
    """Judge 的成本单列 —— 不与 sut 混。"""
    outcomes = _run(tmp_path)
    outcome = next(o for o in outcomes if o.case_id == "judged_clean")
    meta = _meta_evals(outcome)[0]
    assert "judge_cost_usd" in meta.metrics
    # 本评测器自己没产生 LLM 调用；judge 的花费记在 metrics 里而不是 usage
    assert meta.usage is None


def test_the_injection_probe_fires_on_a_real_run(tmp_path):
    """★ 抗注入探针在真实链路上真的能找到注射内容。

    用例先把含注射语的文件写进 workspace，再读回来 ——
    于是注射内容真的出现在被测轨迹的 `tool_result` 里。

    **这条不断言 judge 扛住了**：那需要带 ground-truth 标签的用例，
    当前用例集没有。它断言的是"这件事能被测量"，见 docs/known-gaps.md。
    """
    outcomes = _run(tmp_path)
    outcome = next(o for o in outcomes if o.case_id == "judged_injection")
    meta = _meta_evals(outcome)[0]
    assert meta.metrics["injection_probe_available"] == 1.0
    assert meta.metrics["injection_markers_found"] >= 1.0
    # 判了 3 次且都稳定 → 代理指标算得出来
    assert "injection_resistance" in meta.metrics


def test_meta_results_are_persisted_to_the_index(tmp_path):
    """元评测结果要落索引 —— 报告从那里读回，不重跑。"""
    _run(tmp_path)
    from harness.store.composite import CompositeStore

    async def read():
        store = CompositeStore(root=tmp_path / "runs")
        found = []
        try:
            for row in await store.query_runs():
                found.extend(ev for ev in await store.get_evals(row["run_id"])
                             if ev.evaluator == "MetaEvaluator")
        finally:
            await store.close()
        return found

    import asyncio

    assert asyncio.run(read())


# ---- 报告里的 judge 面板 ----
def test_report_includes_a_judge_reliability_panel(tmp_path):
    _run(tmp_path)
    from harness.report import data as report_data

    data = report_data.collect(tmp_path / "runs")
    assert data["judge"] is not None
    assert data["judge"]["judge_consistency"] == 1.0
    assert data["judge"]["judge_cost_usd"] >= 0.0


def test_report_omits_the_judge_panel_when_no_judge_ran(tmp_path):
    """没启用 judge 时不摆空面板 —— 全 0 会被读成"judge 很差"。"""
    RunBuilder(out_dir=tmp_path / "runs", workdir=tmp_path / "wd").run_suite_sync(
        REPO_ROOT / "examples" / "hello.yaml", evaluate=True)
    from harness.report import data as report_data

    assert report_data.collect(tmp_path / "runs")["judge"] is None


def test_html_report_renders_the_judge_radar_with_data(tmp_path):
    _run(tmp_path)
    from harness.report import data as report_data
    from harness.report.html import render_report

    data = report_data.collect(tmp_path / "runs")
    out = render_report(data, tmp_path / "r.html")
    text = out.read_text(encoding="utf-8")
    assert "judge" in text
    assert "chart-judge" in text


# ---- 递归防护 ----
def test_the_judge_never_launches_another_judge(tmp_path):
    """★ 递归失控防护的可执行验证。

    一次 suite 跑出的 judge 轨迹条数必须**恰好**等于重复次数之和。

    如果 judge 能触发新 judge，这个数字会爆炸 —— 而且是静默的，
    只表现为"账单变高了"。
    """
    outcomes = _run(tmp_path)
    trajs = _all_trajectories(tmp_path / "runs")
    judge_count = sum(1 for t in trajs.values() if t and t[0]["role"] == "judge")

    expected = 0
    for outcome in outcomes:
        meta = _meta_evals(outcome)
        if meta:
            expected += int(meta[0].metrics["judge_verdict_count"])
    assert judge_count == expected == 6, f"judge run 数不对：{judge_count} vs {expected}"


def test_max_depth_is_one():
    from harness.orchestration.judge import MAX_DEPTH

    assert MAX_DEPTH == 1


# ---- 预算隔离 ----
def test_judge_cost_is_not_folded_into_the_sut_result(tmp_path):
    """★ judge 花的是 judge 的钱。

    混进 sut 的 usage 会让"这个模型多贵"这个问题失去意义 ——
    而 judge 的成本恰恰是评测本身的成本，两者要分开看。
    """
    outcomes = _run(tmp_path)
    for outcome in outcomes:
        # fake provider 下 sut 与 judge 的 cost 都是 0，所以这里断言的是
        # **结构**：sut 的 usage 只反映 sut 自己的调用次数
        assert outcome.result.usage.calls <= 4


def test_missing_judge_block_means_no_judge_runs(tmp_path):
    """没配 `judge:` 就不该产生 judge 轨迹 —— 也不该报错。"""
    RunBuilder(out_dir=tmp_path / "runs", workdir=tmp_path / "wd").run_suite_sync(
        REPO_ROOT / "examples" / "hello.yaml", evaluate=True)
    trajs = _all_trajectories(tmp_path / "runs")
    assert all(t and t[0]["role"] == "sut" for t in trajs.values())


def test_meta_evaluator_is_a_registered_grader():
    """MetaEvaluator 必须在白名单里，否则 suite 里写它就是加载期报错。"""
    from harness.orchestration.evalrunner import EVALUATOR_REGISTRY
    from harness.orchestration.suite import load_suite

    assert "MetaEvaluator" in EVALUATOR_REGISTRY
    suite = load_suite(JUDGED)
    names = {g.name for c in suite.cases for g in c.graders}
    assert "MetaEvaluator" in names
    assert suite.defaults.judge is not None
    assert suite.defaults.judge.repeat == 3


@pytest.mark.parametrize("field", ["repeat", "injection_probe", "rubric"])
def test_judge_config_survives_the_yaml_round_trip(field):
    from harness.orchestration.suite import load_suite

    assert getattr(load_suite(JUDGED).defaults.judge, field) is not None
