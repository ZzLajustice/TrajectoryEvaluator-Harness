"""效率评测器 —— **纯规则，零 LLM 成本**。

## 它的价值不在"打分"，而在提供不需要判断力的客观量

走了多少步、重复调了几次、失败几次 —— 这些数字**不会因为 judge 的心情而变**。
在一个 LLM judge 满天飞的领域，确定性指标本身就是稀缺品。

## 步数比的语义

`step_ratio = 实际工具调用数 / 用例声明的 optimal_steps`

`optimal_steps` 写在用例里（人工标注的合理路径长度），不是运行时推断的 ——
推断"最优"本身就需要判断力，那就失去了确定性。
未声明时（0）比率报 0.0 而非除零。
"""

from __future__ import annotations

import json
from collections import Counter

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalResult, EvalStatus, Finding, Severity
from harness.evaluators.base import BaseEvaluator
from harness.events.trajectory import Trajectory
from harness.events.types import EventType

# 步数比超过这个倍数就算"明显绕路"
_STEP_RATIO_WARN = 2.0
# 失败调用占比超过一半时值得单独提示
_FAILURE_RATIO_WARN = 0.5


class EfficiencyAnalyzer(BaseEvaluator):
    name = "EfficiencyAnalyzer"
    subscribes = frozenset({EventType.TOOL_CALL, EventType.RUN_END})

    def __init__(self, *, optimal_steps: int = 0, redundancy_tolerance: int = 3) -> None:
        self.optimal_steps = optimal_steps
        self.redundancy_tolerance = redundancy_tolerance

    def evaluate(self, traj: Trajectory, ctx: EvalContext) -> EvalResult:
        calls = traj.tool_calls()
        results = traj.tool_results()

        # 冗余：同工具 + 同参数的重复调用。
        # 用 canonical JSON 做指纹 —— 参数顺序不同不算不同调用。
        fingerprints = Counter(
            f"{c.name}:{json.dumps(c.arguments, sort_keys=True, default=str)}"
            for c in calls
        )
        redundant = sum(n - 1 for n in fingerprints.values() if n > 1)

        failed = sum(1 for r in results if not r.ok)
        total_calls = len(calls)
        step_ratio = (total_calls / self.optimal_steps) if self.optimal_steps else 0.0

        findings: list[Finding] = []

        if redundant >= self.redundancy_tolerance:
            findings.append(Finding(
                code="efficiency.redundant_calls",
                severity=Severity.MINOR,
                message=f"{redundant} redundant call(s): same tool with identical arguments",
                data={"redundant": redundant, "total": total_calls},
            ))

        if self.optimal_steps and step_ratio > _STEP_RATIO_WARN:
            findings.append(Finding(
                code="efficiency.excessive_steps",
                severity=Severity.MAJOR,
                message=f"used {total_calls} steps against {self.optimal_steps} optimal "
                        f"({step_ratio:.1f}x)",
                data={"step_ratio": step_ratio},
            ))

        if results and failed / len(results) > _FAILURE_RATIO_WARN:
            findings.append(Finding(
                code="efficiency.high_failure_rate",
                severity=Severity.MINOR,
                message=f"{failed}/{len(results)} tool calls failed",
                data={"failed": failed, "total": len(results)},
            ))

        # 有 MAJOR 级发现才 WARN —— MINOR 是"值得看一眼"，不该拉低整体判定
        has_major = any(f.severity is Severity.MAJOR for f in findings)
        status = EvalStatus.WARN if has_major else EvalStatus.PASS

        return EvalResult(
            evaluator=self.name,
            evaluator_version=self.version,
            run_id=traj.run_id,
            status=status,
            # 步数比越接近 1 越好；未声明最优步数时不适用
            score=(1.0 / step_ratio) if step_ratio else None,
            summary=f"{total_calls} calls, ratio {step_ratio:.2f}, {redundant} redundant",
            findings=findings,
            metrics={
                "step_ratio": step_ratio,
                "tool_calls": float(total_calls),
                "redundant_calls": float(redundant),
                "failed_tool_calls": float(failed),
            },
        )
