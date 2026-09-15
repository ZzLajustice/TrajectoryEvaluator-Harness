"""FailureClassifier 测试。

## 规则层与 LLM 层的分工必须被测住

`use_llm_fallback` 的语义是**规则全部无发现时才补一刀语义判断**。
两条测试分别盯住两个方向：规则命中时不能白花 judge 的钱，
规则无发现时不能静默跳过语义层。

`evaluate` 是 async 的 —— LLM 兜底要真的 await judge。规则路径虽然不 await，
但保持单一路径比"有时 async 有时 sync"省事得多。
"""

from __future__ import annotations

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalStatus
from harness.evaluators.failure_classify import FailureClassifier
from harness.events.types import EventType
from harness.testing import TrajectoryBuilder as TB


def _codes(result) -> set[str]:
    return {f.category for f in result.findings if f.category}


async def _classify(traj, **kw):
    return await FailureClassifier(**kw).evaluate(traj, EvalContext())


# ---- FM: step repetition ----


async def test_step_repetition_is_detected():
    b = TB(run_id="r1").turn()
    for _ in range(4):
        b = b.llm_response(tool_calls=[("read_file", {"path": "a.py"})]).tool_result(
            name="read_file", content="same", ok=True)
    assert "step_repetition" in _codes(await _classify(b.run_end().build()))


async def test_no_repetition_when_args_differ():
    b = TB(run_id="r1").turn()
    for p in ("a.py", "b.py", "c.py"):
        b = b.llm_response(tool_calls=[("read_file", {"path": p})]).tool_result(
            name="read_file", content=p, ok=True)
    assert "step_repetition" not in _codes(await _classify(b.run_end().build()))


async def test_repetition_threshold_is_configurable():
    b = TB(run_id="r1").turn()
    for _ in range(2):
        b = b.llm_response(tool_calls=[("read_file", {"path": "a.py"})]).tool_result(
            name="read_file", content="same", ok=True)
    traj = b.run_end().build()
    assert "step_repetition" not in _codes(await _classify(traj))
    assert "step_repetition" in _codes(await _classify(traj, repetition_threshold=2))


# ---- FM: unaware of termination ----


async def test_missing_finish_is_flagged():
    traj = TB(run_id="r1").turn().llm_response(text="done I think").run_end(
        status="no_finish").build()
    assert "unaware_of_termination" in _codes(await _classify(traj))


async def test_finish_present_is_not_flagged():
    traj = (TB(run_id="r1").turn().llm_response(tool_calls=[("finish", {"summary": "x"})])
            .tool_result(name="finish", content="x", ok=True).run_end(status="ok").build())
    assert "unaware_of_termination" not in _codes(await _classify(traj))


# ---- FM: 单 agent 补充 —— 幻觉工具 ----


async def test_hallucinated_tool_is_detected():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("nonexistent_tool", {})])
            .tool_result(name="nonexistent_tool", content="", ok=False,
                         error="unknown tool", error_type="unknown_tool")
            .run_end().build())
    assert "hallucinated_tool" in _codes(await _classify(traj))


async def test_bad_path_args_are_detected():
    """参数指向不存在的路径 —— 与"调了不存在的工具"是两种不同的幻觉。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("read_file", {"path": "ghost.py"})])
            .tool_result(name="read_file", content="", ok=False, error="not found",
                         error_type="not_found")
            .run_end().build())
    assert "hallucinated_tool_args" in _codes(await _classify(traj))


# ---- FM: 单 agent 补充 —— 忽略工具返回 ----


async def test_ignored_tool_result_is_detected():
    """看到报错但未修正就重试同一调用。"""
    b = TB(run_id="r1").turn()
    for _ in range(2):
        b = b.llm_response(tool_calls=[("run_command", {"argv": ["pytest"]})]).tool_result(
            name="run_command", content="FAILED", ok=False, error="exit 1",
            error_type="nonzero_exit")
    assert "ignored_tool_result" in _codes(await _classify(b.run_end().build()))


async def test_changed_args_after_failure_is_not_ignored():
    """失败后改了参数再试 —— 这是正确的行为，不该被判成"视而不见"。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("run_command", {"argv": ["pytest", "-x"]})])
            .tool_result(name="run_command", content="FAILED", ok=False,
                         error_type="nonzero_exit")
            .llm_response(tool_calls=[("run_command", {"argv": ["pytest", "-k", "one"]})])
            .tool_result(name="run_command", content="1 passed", ok=True)
            .run_end(status="ok").build())
    assert "ignored_tool_result" not in _codes(await _classify(traj))


# ---- FM: 单 agent 补充 —— 预算内未收敛 ----


async def test_budget_exceeded_is_classified():
    traj = TB(run_id="r1").turn().llm_response(text="still working").run_end(
        status="budget_exceeded").build()
    assert "budget_not_converged" in _codes(await _classify(traj))


async def test_other_failure_statuses_are_not_budget_not_converged():
    """`llm_error` 是外部依赖出错，与"有资源但没收敛"是两回事。"""
    traj = TB(run_id="r1").turn().llm_response(text="x").run_end(
        status="llm_error").build()
    assert "budget_not_converged" not in _codes(await _classify(traj))


# ---- FM: FC3 —— 过早终止 ----


async def test_premature_termination_is_detected():
    """改了代码但没跑测试就 finish。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("write_file", {"path": "a.py", "content": "x"})])
            .tool_result(name="write_file", content="ok", ok=True)
            .llm_response(tool_calls=[("finish", {"summary": "fixed"})])
            .tool_result(name="finish", content="fixed", ok=True)
            .run_end(status="ok").build())
    assert "premature_termination" in _codes(await _classify(traj))


async def test_no_premature_termination_when_tests_were_run():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("write_file", {"path": "a.py", "content": "x"})])
            .tool_result(name="write_file", content="ok", ok=True)
            .llm_response(tool_calls=[("run_command", {"argv": ["pytest"]})])
            .tool_result(name="run_command", content="1 passed", ok=True)
            .llm_response(tool_calls=[("finish", {"summary": "fixed"})])
            .tool_result(name="finish", content="fixed", ok=True)
            .run_end(status="ok").build())
    assert "premature_termination" not in _codes(await _classify(traj))


# ---- FM: FC1 —— 上下文丢失归因 ----


async def test_context_loss_is_attributed_to_compaction_event():
    traj = TB(run_id="r1").turn().llm_response(text="a").raw_emit(
        EventType.CONTEXT_COMPACT, reason="token_pressure", messages_before=20,
        messages_after=6, tokens_before=9000, tokens_after=3000,
        dropped_message_digests=["d1", "d2"], strategy="drop_oldest_tool_results",
    ).run_end(status="ok").build()
    assert "loss_of_conversation_history" in _codes(await _classify(traj))


# ---- FM: FC3 —— 缺失验证 ----


async def test_no_verification_is_flagged():
    """只改不验 —— read_file 没有、测试也没跑。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("write_file", {"path": "a.py", "content": "x"})])
            .tool_result(name="write_file", content="ok", ok=True)
            .run_end(status="ok").build())
    assert "no_incomplete_verification" in _codes(await _classify(traj))


# ---- 契约 ----


async def test_matched_failure_modes_are_reported_as_metrics():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("ghost", {})])
            .tool_result(name="ghost", content="", ok=False, error_type="unknown_tool")
            .run_end().build())
    r = await _classify(traj)
    assert r.metrics["matched_modes"] >= 1


async def test_expected_modes_narrow_the_report():
    """用例声明了 expected_failure_modes 时，只报告关心的那几类。

    未声明的模式数量单独记进 `unexpected_modes` —— 它本身就是有价值的信号：
    说明这条用例的失败方式和设计时想考的不是一回事。
    """
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("ghost", {})])
            .tool_result(name="ghost", content="", ok=False, error_type="unknown_tool")
            .run_end(status="no_finish").build())
    r = await FailureClassifier(expected_modes=["unaware_of_termination"]).evaluate(
        traj, EvalContext())
    assert _codes(r) == {"unaware_of_termination"}
    assert r.metrics["unexpected_modes"] >= 1


async def test_empty_trajectory_does_not_crash():
    assert (await _classify(TB(run_id="r1").build())).status is not EvalStatus.ERROR


async def test_critical_severity_maps_to_fail():
    """幻觉工具是 CRITICAL —— 必须判 FAIL 而不是 WARN。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("ghost", {})])
            .tool_result(name="ghost", content="", ok=False, error_type="unknown_tool")
            .run_end().build())
    assert (await _classify(traj)).status is EvalStatus.FAIL


async def test_findings_are_ordered_deterministically():
    """同一轨迹跑两次，findings 顺序必须一致 —— 否则报告 diff 全是噪声。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("ghost", {})])
            .tool_result(name="ghost", content="", ok=False, error_type="unknown_tool")
            .run_end(status="no_finish").build())
    a = [f.category for f in (await _classify(traj)).findings]
    b = [f.category for f in (await _classify(traj)).findings]
    assert a == b


# ---- LLM 兜底 ----


def _a_clean_trajectory():
    """一条**任何规则都不命中**的轨迹 —— LLM 兜底测试的前提。

    注意必须带 read_file：只有 finish 的轨迹会命中
    `no_incomplete_verification`（既没跑测试也没回读文件），
    于是兜底永远不会被触发，而症状是"这条测试莫名其妙地断言失败"。
    """
    return (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("read_file", {"path": "a.py"})])
            .tool_result(name="read_file", content="x", ok=True)
            .llm_response(tool_calls=[("finish", {"summary": "ok"})])
            .tool_result(name="finish", content="ok", ok=True)
            .run_end(status="ok").build())


class _SpyJudge:
    """记录调用次数的 judge 桩。"""

    calls = 0

    async def judge(self, case, *, repeat=1):  # noqa: ARG002
        type(self).calls += 1
        return []


async def test_llm_fallback_not_called_when_rules_match():
    _SpyJudge.calls = 0
    traj = TB(run_id="r1").turn().llm_response(text="x").run_end(status="no_finish").build()
    await FailureClassifier(use_llm_fallback=True).evaluate(
        traj, EvalContext(judge=_SpyJudge()))
    assert _SpyJudge.calls == 0, "规则命中时不该调 LLM"


async def test_llm_fallback_used_when_rules_find_nothing():
    _SpyJudge.calls = 0
    clean = (TB(run_id="r1").turn()
             .llm_response(tool_calls=[("read_file", {"path": "a.py"})])
             .tool_result(name="read_file", content="x", ok=True)
             .llm_response(tool_calls=[("finish", {"summary": "ok"})])
             .tool_result(name="finish", content="ok", ok=True)
             .run_end(status="ok").build())
    await FailureClassifier(use_llm_fallback=True).evaluate(
        clean, EvalContext(judge=_SpyJudge()))
    assert _SpyJudge.calls == 1, "规则无发现时才该调 LLM 兜底"


async def test_llm_fallback_is_off_by_default():
    _SpyJudge.calls = 0
    clean = (TB(run_id="r1").turn()
             .llm_response(tool_calls=[("read_file", {"path": "a.py"})])
             .tool_result(name="read_file", content="x", ok=True)
             .llm_response(tool_calls=[("finish", {"summary": "ok"})])
             .tool_result(name="finish", content="ok", ok=True)
             .run_end(status="ok").build())
    await FailureClassifier().evaluate(clean, EvalContext(judge=_SpyJudge()))
    assert _SpyJudge.calls == 0, "默认不开 LLM 兜底 —— 评测默认零成本"


async def test_llm_fallback_without_a_judge_does_not_crash():
    """没注入 judge 时不能崩 —— 记 ERROR 会让整份报告不可信。"""
    clean = _a_clean_trajectory()
    r = await FailureClassifier(use_llm_fallback=True).evaluate(clean, EvalContext())
    assert r.status is not EvalStatus.ERROR


async def test_llm_verdict_becomes_a_finding():
    """Judge 判 fail 时必须落成 finding，而不是只记个 metric。"""
    class _FailJudge:
        async def judge(self, case, *, repeat=1):  # noqa: ARG002
            from harness.contracts.protocols import JudgeVerdict
            return [JudgeVerdict(verdict="fail", score=0.1,
                                 rationale="agent ignored the stated requirement",
                                 raw={"failure_mode": "disobey_task_specification"})]

    clean = _a_clean_trajectory()
    r = await FailureClassifier(use_llm_fallback=True).evaluate(
        clean, EvalContext(judge=_FailJudge()))
    assert "disobey_task_specification" in _codes(r)
    assert r.metrics["judge_calls"] == 1.0


async def test_unknown_llm_failure_mode_falls_back_to_a_generic_category():
    """Judge 报了个分类法外的名字时不能原样写进报告 —— 报告的分类维度会失控。"""
    class _WeirdJudge:
        async def judge(self, case, *, repeat=1):  # noqa: ARG002
            from harness.contracts.protocols import JudgeVerdict
            return [JudgeVerdict(verdict="fail", score=0.1, rationale="hmm",
                                 raw={"failure_mode": "totally_made_up"})]

    clean = _a_clean_trajectory()
    r = await FailureClassifier(use_llm_fallback=True).evaluate(
        clean, EvalContext(judge=_WeirdJudge()))
    assert _codes(r) == {"llm_flagged"}


async def test_passing_judge_verdict_produces_no_finding():
    class _PassJudge:
        async def judge(self, case, *, repeat=1):  # noqa: ARG002
            from harness.contracts.protocols import JudgeVerdict
            return [JudgeVerdict(verdict="pass", score=0.9, rationale="fine")]

    clean = _a_clean_trajectory()
    r = await FailureClassifier(use_llm_fallback=True).evaluate(
        clean, EvalContext(judge=_PassJudge()))
    assert r.status is EvalStatus.PASS


async def test_judge_exception_is_contained():
    """Judge 崩了不能让分类器跟着崩 —— 那会把评测器 bug 记成 agent 的失败。"""
    class _BrokenJudge:
        async def judge(self, case, *, repeat=1):  # noqa: ARG002
            raise RuntimeError("judge exploded")

    clean = _a_clean_trajectory()
    r = await FailureClassifier(use_llm_fallback=True).evaluate(
        clean, EvalContext(judge=_BrokenJudge()))
    assert r.status is not EvalStatus.ERROR
    assert r.metrics.get("judge_failed") == 1.0


# ---- 分类法本身 ----
def test_the_taxonomy_is_documented_and_complete():
    """12 个模式必须齐 —— 报告的分类维度是这套分类法，缺一个就是缺一个维度。"""
    assert len(FailureClassifier.ALL_MODES) == 12
    assert len(FailureClassifier.RULE_MODES) == 9
    assert len(FailureClassifier.LLM_MODES) == 3


def test_multi_agent_mast_modes_are_excluded():
    """FC2「智能体间失调」6 个模式在单 agent 架构下结构上不存在。

    不写明这层适配，「生搬多智能体分类法」会被直接质疑 ——
    把它固化成断言，比写在注释里更难被误改。
    """
    fc2 = {"conversation_reset", "fail_to_ask_clarification", "task_derailment",
           "information_withholding", "ignored_other_agent_input",
           "reasoning_action_mismatch"}
    assert not (fc2 & set(FailureClassifier.ALL_MODES))
    assert len(fc2) == 6


def test_subscribes_declares_the_events_it_needs():
    assert EventType.TOOL_CALL in FailureClassifier.subscribes
    assert EventType.RUN_END in FailureClassifier.subscribes


async def test_unsupported_idle_trajectory_is_not_an_error():
    """退化输入绝不抛异常 —— 抛了会被调度器记成 ERROR 并污染报告。"""
    for status in ("max_turns", "timeout", "cancelled", "llm_error", "sandbox_error"):
        traj = TB(run_id="r1").run_end(status=status).build()
        assert (await _classify(traj)).status is not EvalStatus.ERROR
