"""失败模式分类器。规则优先，LLM 兜底。

## 分类法：MAST 单 agent 适用子集 + 单 agent 专属补充

MAST（Cemri et al., NeurIPS 2025, κ=0.88）是面向**多智能体**的分类法。
直接搬过来会被问「你的 agent 之间怎么通信的」。所以这里明确划分：

**采用（FC1 规范类 + FC3 验证类，共 8 个）**

    disobey_task_specification / disobey_role_specification        [LLM]
    step_repetition / loss_of_conversation_history /
    unaware_of_termination                                         [规则]
    premature_termination / no_incomplete_verification              [规则]
    incorrect_verification                                          [LLM]

**不采用（FC2「智能体间失调」6 个）**

    conversation_reset / fail_to_ask_clarification / task_derailment /
    information_withholding / ignored_other_agent_input / reasoning_action_mismatch

单 agent 架构下这些**结构上不存在**，不是"罕见"而是"不可能发生"。
把它们放进报告的分类维度，等于给每一类都留一个恒为 0 的格子。

**单 agent 专属补充（MAST 未覆盖，4 个）** —— 都走规则

    hallucinated_tool       调了不存在的工具
    hallucinated_tool_args  参数指向不存在的路径
    ignored_tool_result     看到报错未修正就重试
    budget_not_converged    预算耗尽仍未完成

合计 12 个模式：9 个规则、3 个 LLM。

## 为什么规则优先

**评测结果的可信度不该被 judge 的不确定性污染。** 9 个模式走确定性规则，
意味着这 9 个维度的数字任何时候重跑都一样。只有真正需要语义判断的 3 个
才交给 LLM，且它们的 finding 单独标 `severity=MINOR` —— 语义判断的证据强度
本来就低于规则命中。

## `evaluate` 为什么是 async

LLM 兜底要真的 await judge。规则路径虽然从不 await，但保持单一路径
比"有时 async 有时 sync"省事得多（`run_evaluators` 两种都支持）。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable

from harness.contracts.protocols import EvalContext, JudgeCase, JudgeVerdict
from harness.contracts.results import EvalResult, EvalStatus, Finding, Severity
from harness.evaluators.base import BaseEvaluator
from harness.events.trajectory import Trajectory
from harness.events.types import EventType

# 同一个调用重复多少次算"卡住了"。3 是保守值 —— 2 会把正常的
# "读文件→改→再读确认"误判成重复。
_REPETITION_THRESHOLD = 3

# 命令里出现这些子串就算"跑了测试"。用子串而非 argv 精确匹配，
# 因为 agent 可能写 `python -m pytest`、`pytest -x`、`make test`。
_TEST_COMMANDS = ("pytest", "unittest", "nose", "tox", "make test", "npm test")

# 规则无法判断、必须交给 LLM 的三个模式
_LLM_MODES = (
    "disobey_task_specification",
    "disobey_role_specification",
    "incorrect_verification",
)


class FailureClassifier(BaseEvaluator):
    name = "FailureClassifier"
    subscribes = frozenset({EventType.TOOL_CALL, EventType.RUN_END})

    # 分类法的公开定义 —— 报告的分类维度与测试都引用它，不另抄一份
    RULE_MODES: tuple[str, ...] = (
        "step_repetition",
        "loss_of_conversation_history",
        "unaware_of_termination",
        "premature_termination",
        "no_incomplete_verification",
        "hallucinated_tool",
        "hallucinated_tool_args",
        "ignored_tool_result",
        "budget_not_converged",
    )
    LLM_MODES: tuple[str, ...] = _LLM_MODES
    ALL_MODES: tuple[str, ...] = RULE_MODES + LLM_MODES

    def __init__(
        self,
        *,
        expected_modes: list[str] | None = None,
        use_llm_fallback: bool = False,
        repetition_threshold: int = _REPETITION_THRESHOLD,
    ) -> None:
        self.expected_modes = set(expected_modes) if expected_modes else None
        self.use_llm_fallback = use_llm_fallback
        self.repetition_threshold = repetition_threshold

    # ---- 规则检测器：每个返回 Finding | None ----
    def _step_repetition(self: FailureClassifier, traj: Trajectory) -> Finding | None:
        """同工具 + 同参数的重复调用。

        用 canonical JSON 做指纹 —— 参数顺序不同不算不同调用，
        否则 `{"a":1,"b":2}` 和 `{"b":2,"a":1}` 会被当成两次不同探索。
        """
        prints = Counter(
            f"{c.name}:{json.dumps(c.arguments, sort_keys=True, default=str)}"
            for c in traj.tool_calls()
        )
        top = max(prints.values(), default=0)
        if top < self.repetition_threshold:
            return None
        worst = max(prints, key=lambda k: prints[k])
        return Finding(code="failure.step_repetition", category="step_repetition",
                       severity=Severity.MAJOR,
                       message=f"same call repeated {top}x: {worst[:60]}",
                       data={"repeat_count": top})

    def _unaware_of_termination(self: FailureClassifier, traj: Trajectory) -> Finding | None:
        """没调 finish 就结束了 —— agent 不知道自己已经完事。

        MAST 里这是发生率最高的模式之一（12.4%）。
        """
        if any(c.name == "finish" for c in traj.tool_calls()):
            return None
        end = traj.end()
        status = end.status if end else "unknown"
        return Finding(code="failure.unaware_of_termination",
                       category="unaware_of_termination", severity=Severity.MAJOR,
                       message=f"run ended as {status!r} without calling finish",
                       data={"status": status})

    def _hallucinated_tool(self: FailureClassifier, traj: Trajectory) -> Finding | None:
        bad = [r for r in traj.tool_results() if r.error_type == "unknown_tool"]
        if not bad:
            return None
        # CRITICAL：调一个根本不存在的工具，说明模型对环境的认知整体失真
        return Finding(code="failure.hallucinated_tool", category="hallucinated_tool",
                       severity=Severity.CRITICAL,
                       message=f"called non-existent tools: {[r.name for r in bad]}",
                       data={"names": [r.name for r in bad]})

    def _hallucinated_tool_args(self: FailureClassifier, traj: Trajectory) -> Finding | None:
        """参数指向不存在的路径 —— 与"调了不存在的工具"是两种不同的幻觉。"""
        bad = [r for r in traj.tool_results() if r.error_type in {"not_found", "path_escape"}]
        if not bad:
            return None
        return Finding(code="failure.hallucinated_tool_args",
                       category="hallucinated_tool_args", severity=Severity.MAJOR,
                       message=f"bad path arguments: {[(r.name, r.error) for r in bad][:3]}",
                       data={"count": len(bad)})

    def _ignored_tool_result(self: FailureClassifier, traj: Trajectory) -> Finding | None:
        """失败调用之后，下一个同类调用的**参数完全没变** —— 看了错但没改。

        注意判据是"参数未变"而不是"又调了一次"：失败后改了参数重试是
        正确行为，不该被判成视而不见。
        """
        calls = {c.call_id: c for c in traj.tool_calls()}
        results = traj.tool_results()
        ignored = 0
        for i, r in enumerate(results[:-1]):
            if r.ok:
                continue
            cur = calls.get(r.call_id)
            nxt = calls.get(results[i + 1].call_id)
            if cur and nxt and cur.name == nxt.name and cur.arguments == nxt.arguments:
                ignored += 1
        if not ignored:
            return None
        return Finding(code="failure.ignored_tool_result", category="ignored_tool_result",
                       severity=Severity.MAJOR,
                       message=f"{ignored} failed call(s) retried unchanged",
                       data={"ignored": ignored})

    def _budget_not_converged(self: FailureClassifier, traj: Trajectory) -> Finding | None:
        """预算耗尽仍未完成 —— 单 agent 专属模式。

        `llm_error` 不算：那是外部依赖出错，与 agent 有没有收敛无关。
        """
        end = traj.end()
        if end is None or end.status != "budget_exceeded":
            return None
        return Finding(code="failure.budget_not_converged", category="budget_not_converged",
                       severity=Severity.MAJOR,
                       message="ran out of budget without converging",
                       data={"turns": end.turns})

    def _premature_termination(self: FailureClassifier, traj: Trajectory) -> Finding | None:
        """改了文件却没跑测试就 finish。"""
        names = [c.name for c in traj.tool_calls()]
        if "finish" not in names:
            return None
        if "write_file" not in names:
            return None
        flat = self._flatten_args(traj)
        if any(t in flat for t in _TEST_COMMANDS):
            return None
        return Finding(code="failure.premature_termination",
                       category="premature_termination", severity=Severity.MAJOR,
                       message="wrote files then finished without running tests")

    def _loss_of_conversation_history(self: FailureClassifier, traj: Trajectory) -> Finding | None:
        """上下文被压缩过 —— 归因到具体的 CONTEXT_COMPACT 事件。

        这正是把压缩做成一等事件（而非日志）的回报：
        没有这个事件，"agent 忘了之前的结论"就只是个猜测。
        """
        compactions = traj.compactions()
        if not compactions:
            return None
        total_dropped = sum(len(c.dropped_message_digests) for c in compactions)
        return Finding(code="failure.loss_of_conversation_history",
                       category="loss_of_conversation_history", severity=Severity.MINOR,
                       message=f"{len(compactions)} compaction(s), {total_dropped} messages dropped",
                       data={"compactions": len(compactions), "dropped": total_dropped})

    def _no_incomplete_verification(self: FailureClassifier, traj: Trajectory) -> Finding | None:
        """改了东西却没观察任何验证信号 —— 没跑测试，也没回头读文件确认。

        **前提是"改过东西"。** 这条限制是实测补上的：不加它的话，
        任何不读文件、不跑测试的 run 都会被判"缺失验证"——
        包括 `hello.yaml` 这种"打个招呼就结束"的轨迹。
        "验证"只有在**有东西需要验证**时才谈得上；只读不改的 run
        没有该验证的对象，报它属于误报，而误报会让人直接关掉这个维度。
        """
        names = [c.name for c in traj.tool_calls()]
        if "write_file" not in names:
            return None
        flat = self._flatten_args(traj)
        if any(t in flat for t in _TEST_COMMANDS):
            return None
        if "read_file" in names:
            return None
        return Finding(code="failure.no_incomplete_verification",
                       category="no_incomplete_verification", severity=Severity.MINOR,
                       message="wrote files but never ran tests or re-read them")

    @staticmethod
    def _flatten_args(traj: Trajectory) -> str:
        """把全部调用参数拍平成一个字符串，供子串匹配。

        不能直接 `str(arguments)` —— 那样 `["pytest"]` 会变成 `"['pytest']"`，
        而 `make test` 这种带空格的命令又会被引号切开。
        这条是实测踩过的：策略中间件曾因为同样的原因永不命中。
        """
        return " ".join(json.dumps(c.arguments, default=str) for c in traj.tool_calls())

    # 规则注册表。顺序即 findings 顺序 —— 固定下来报告 diff 才不会抖。
    _RULES: tuple[Callable[[FailureClassifier, Trajectory], Finding | None], ...] = (
        _step_repetition,
        _hallucinated_tool,
        _hallucinated_tool_args,
        _unaware_of_termination,
        _ignored_tool_result,
        _budget_not_converged,
        _premature_termination,
        _loss_of_conversation_history,
        _no_incomplete_verification,
    )

    # ---- 主入口 ----
    async def evaluate(self, traj: Trajectory, ctx: EvalContext) -> EvalResult:
        findings: list[Finding] = []
        for rule in self._RULES:
            found = rule(self, traj)
            if found is not None:
                findings.append(found)

        metrics: dict[str, float] = {}

        if self.expected_modes is not None:
            matched = {f.category for f in findings} & self.expected_modes
            unexpected = {f.category for f in findings} - self.expected_modes
            filtered = [f for f in findings if f.category in matched]
            metrics["matched_modes"] = float(len(matched))
            # 未声明的模式数量本身有价值：说明这条用例的失败方式
            # 和设计时想考的不是一回事
            metrics["unexpected_modes"] = float(len(unexpected))
        else:
            filtered = findings
            metrics["matched_modes"] = float(len(findings))
            metrics["unexpected_modes"] = 0.0

        # 规则层颗粒无收时才补语义判断 —— 规则能判的绝不问 LLM
        if not filtered and self.use_llm_fallback:
            llm_findings, llm_metrics = await self._llm_fallback(traj, ctx)
            filtered.extend(llm_findings)
            metrics.update(llm_metrics)

        critical = any(f.severity is Severity.CRITICAL for f in filtered)
        status = (EvalStatus.FAIL if critical
                  else EvalStatus.WARN if filtered
                  else EvalStatus.PASS)

        return EvalResult(
            evaluator=self.name,
            evaluator_version=self.version,
            run_id=traj.run_id,
            status=status,
            summary=f"{len(filtered)} failure mode(s) matched",
            findings=filtered,
            metrics=metrics,
        )

    async def _llm_fallback(
        self, traj: Trajectory, ctx: EvalContext
    ) -> tuple[list[Finding], dict[str, float]]:
        """语义层的三个模式。没注入 judge 时静默跳过而非报错。"""
        if ctx.judge is None:
            # 没 judge 是配置问题，不是评测器 bug —— 记 ERROR 会污染报告
            return [], {"llm_fallback_skipped": 1.0}

        start = traj.start()
        case = JudgeCase(
            case_id=_case_id_of(traj),
            task=(start.task if start is not None else None) or "",
            traj=traj,
            rubric=(
                "Classify whether this trajectory exhibits any of: "
                + ", ".join(_LLM_MODES)
                + ". If it does, return a failing verdict with "
                "raw.failure_mode set to exactly one of those names. "
                "If none apply, return a passing verdict."
            ),
        )
        try:
            verdicts = await ctx.judge.judge(case, repeat=1)
        except Exception:  # noqa: BLE001
            # judge 崩了不能把分类器一起拖崩 —— 那会把评测器 bug 记成 agent 的失败
            return [], {"judge_failed": 1.0}

        findings = [f for f in (_verdict_finding(v) for v in verdicts) if f is not None]
        return findings, {"judge_calls": 1.0, "llm_verdicts": float(len(verdicts))}


def _verdict_finding(verdict: JudgeVerdict) -> Finding | None:
    """把一条 judge 判定转成 finding。

    分类名只在**属于本分类法**时才采用。judge 报了个分类法外的名字
    （LLM 幻觉分类是常见现象），就落到 `llm_flagged` —— 否则报告的分类维度
    会被 judge 随机发明的新名字撑爆。
    """
    if verdict.verdict == "pass":
        return None
    mode = str(verdict.raw.get("failure_mode", ""))
    category = mode if mode in _LLM_MODES else "llm_flagged"
    return Finding(
        code=f"failure.{category}",
        category=category,
        # 语义判断的证据强度低于规则命中 —— 同样的分类，规则版本是 MAJOR
        severity=Severity.MINOR,
        message=f"judge verdict {verdict.verdict!r}: {verdict.rationale[:200]}",
        data={"verdict": verdict.verdict, "score": verdict.score,
              "judge_run_id": verdict.judge_run_id},
    )


def _case_id_of(traj: Trajectory) -> str:
    """从 RUN_START 的 spec_json 里取 case_id。

    刻意**不给 RunStartEvent 加 case_id 字段** —— 事件 schema 的第一规约是
    「加字段前先自问能否从已有事件派生」。`spec_json` 里已经有了，
    加字段就要动 L0 并 bump 版本，代价显式且不必要。
    """
    start = traj.start()
    if start is not None and start.spec_json:
        try:
            spec = json.loads(start.spec_json)
            case_id = (spec.get("task") or {}).get("case_id")
            if isinstance(case_id, str) and case_id:
                return case_id
        except (ValueError, AttributeError):
            pass
    return traj.run_id
