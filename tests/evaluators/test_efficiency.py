"""EfficiencyAnalyzer 测试。

**纯规则、零 LLM 成本。** 效率指标最大的价值不是"打分"，
而是提供**不需要判断力的**客观量：走了多少步、重复调了几次、失败几次。
这些数字不会因为 judge 的心情而变。
"""

from __future__ import annotations

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalStatus
from harness.evaluators.efficiency import EfficiencyAnalyzer
from harness.testing import TrajectoryBuilder as TB


def _traj(steps: list[tuple[str, dict]]):
    b = TB(run_id="r1").turn()
    for i, (name, args) in enumerate(steps):
        b = b.llm_response(tool_calls=[(name, args, f"c{i}")]).tool_result(
            name=name, content="ok", ok=True)
    return b.run_end().build()


def _run(traj, **kw):
    return EfficiencyAnalyzer(**kw).evaluate(traj, EvalContext())


def test_step_ratio_is_one_for_the_optimal_path():
    traj = _traj([("read", {}), ("write", {}), ("test", {}), ("finish", {})])
    r = _run(traj, optimal_steps=4)
    assert r.metrics["step_ratio"] == 1.0
    assert r.status is EvalStatus.PASS


def test_step_ratio_grows_with_waste():
    traj = _traj([("read", {}) for _ in range(12)])
    r = _run(traj, optimal_steps=4)
    assert r.metrics["step_ratio"] == 3.0


def test_excessive_steps_emit_a_finding():
    traj = _traj([("read", {}) for _ in range(20)])
    r = _run(traj, optimal_steps=4)
    assert any(f.code == "efficiency.excessive_steps" for f in r.findings)
    assert r.status is EvalStatus.WARN


def test_zero_optimal_steps_does_not_divide_by_zero():
    r = _run(_traj([("a", {})]), optimal_steps=0)
    assert r.status is not EvalStatus.ERROR
    assert r.metrics["step_ratio"] == 0.0


# ---- 冗余检测 ----
def test_redundant_calls_are_counted():
    """同工具 + 同参数重复调用 —— 循环检测的基础。"""
    traj = _traj([("read_file", {"path": "a.py"})] * 4)
    r = _run(traj, optimal_steps=4)
    assert r.metrics["redundant_calls"] == 3


def test_repeated_calls_with_different_args_are_not_redundant():
    traj = _traj([("read_file", {"path": f"{c}.py"}) for c in "abc"])
    r = _run(traj, optimal_steps=3)
    assert r.metrics["redundant_calls"] == 0


def test_redundancy_tolerance_is_configurable():
    """阈值可配 —— 连续两次重试是合理的，十次不是。"""
    traj = _traj([("f", {})] * 3)
    assert not any(f.code == "efficiency.redundant_calls"
                   for f in _run(traj, redundancy_tolerance=5).findings)
    assert any(f.code == "efficiency.redundant_calls"
               for f in _run(traj, redundancy_tolerance=2).findings)


# ---- 失败计数 ----
def test_failed_tool_calls_are_counted():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {}, "c1")])
            .tool_result(name="f", content="", ok=False, error="boom",
                         error_type="nonzero_exit")
            .run_end().build())
    r = _run(traj, optimal_steps=1)
    assert r.metrics["failed_tool_calls"] == 1


def test_denied_calls_count_as_failed():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {}, "c1")])
            .tool_result(name="f", content="", ok=False, denied_by="permission")
            .run_end().build())
    assert _run(traj, optimal_steps=1).metrics["failed_tool_calls"] == 1


# ---- 退化输入 ----
def test_empty_trajectory_does_not_crash():
    r = _run(TB(run_id="r1").build(), optimal_steps=4)
    assert r.status is not EvalStatus.ERROR


def test_truncated_trajectory_does_not_crash():
    traj = TB(run_id="r1").turn().llm_response(tool_calls=[("a", {}, "c1")]).build()
    assert _run(traj, optimal_steps=2).status is not EvalStatus.ERROR


# ---- 元信息 ----
def test_name_and_subscriptions():
    from harness.events.types import EventType

    e = EfficiencyAnalyzer(optimal_steps=1)
    assert e.name == "EfficiencyAnalyzer"
    assert EventType.TOOL_CALL in e.subscribes


def test_tool_call_count_is_reported():
    r = _run(_traj([("a", {}), ("b", {})]), optimal_steps=2)
    assert r.metrics["tool_calls"] == 2


# ---- 推理体量（本次新增）----
def test_it_reports_reasoning_volume():
    """★ 推理体量是**第一等的过程事实**，而且 provider 自己报。

    实测（2026-09-16 全量真跑，17 条轨迹 / 194 个响应）：
    reasoning tokens 20,089 / completion tokens 58,149 = **34.5%**，
    194/194 个响应都带这个字段。

    它是效率指标而不是"推理质量分"：说的是"模型把多少产出花在了
    你（和所有现有评测器）看不到的地方"，这件事不需要判断力就能量。
    """
    traj = (TB(run_id="r1").turn()
            .llm_response(text="done", reasoning="thinking hard",
                          reasoning_tokens=900, output_tokens=1000)
            .run_end().build())
    r = _run(traj)
    assert r.metrics["reasoning_tokens"] == 900
    assert r.metrics["reasoning_ratio"] == 0.9


def test_reasoning_volume_accumulates_across_turns():
    traj = (TB(run_id="r1").turn()
            .llm_response(reasoning="a", reasoning_tokens=100, output_tokens=200)
            .turn()
            .llm_response(reasoning="b", reasoning_tokens=300, output_tokens=400)
            .run_end().build())
    r = _run(traj)
    assert r.metrics["reasoning_tokens"] == 400
    assert r.metrics["reasoning_ratio"] == 400 / 600


def test_no_reasoning_reports_zero_rather_than_omitting_the_key():
    """★ 0 是**事实**，不是"不适用" —— 所以键必须在。

    §2.1 记着 `metrics` 装不下"不适用"，只能靠键缺席表达。这里刻意不用那个
    逃生口：一个不产生推理的模型确实产生了 0 个推理 token，
    而"键缺席"会让报告区分不了"这个模型不推理"与"我们没去看"。
    """
    traj = (TB(run_id="r1").turn().llm_response(text="hi", output_tokens=10)
            .run_end().build())
    r = _run(traj)
    assert r.metrics["reasoning_tokens"] == 0
    assert r.metrics["reasoning_ratio"] == 0.0


def test_zero_output_tokens_does_not_divide_by_zero():
    """退化输入绝不抛 —— 所有评测器的共同规约。

    没有 usage 的 provider 会让 `output_tokens` 全是 0，
    而那正是"接一个新网关"时的默认状态。
    """
    traj = (TB(run_id="r1").turn().llm_response(reasoning="x").run_end().build())
    r = _run(traj)
    assert r.status is not EvalStatus.ERROR
    assert r.metrics["reasoning_ratio"] == 0.0


def test_ratio_never_exceeds_one():
    """守卫：`output_tokens` 是**含**推理的 completion tokens。

    某些 provider 只报推理 token 而不把它算进 completion
    （或两条数据来自不同的调用），那会让比率大于 1 ——
    一个大于 1 的比率在报告里读起来像"推理比产出还多"，
    而实际是"这两个数不同源"。宁可按 1 封顶并保留原始 token 数。
    """
    traj = (TB(run_id="r1").turn()
            .llm_response(reasoning="x", reasoning_tokens=500, output_tokens=100)
            .run_end().build())
    r = _run(traj)
    assert r.metrics["reasoning_ratio"] == 1.0
    assert r.metrics["reasoning_tokens"] == 500


def test_it_subscribes_to_llm_responses():
    """推理住在 `llm.response` 里 —— 不订阅它就永远取不到。

    `subscribes` 是**行为**不是文档：没订阅的事件不在时评测器直接被跳过。
    """
    from harness.events.types import EventType

    assert EventType.LLM_RESPONSE in EfficiencyAnalyzer(optimal_steps=1).subscribes
