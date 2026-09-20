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
    # `LLM_RESPONSE` 是推理体量的来源（见 `_reasoning_volume`）。
    # `subscribes` 是**行为**不是文档：没订阅的事件不在时这个评测器会被直接跳过。
    subscribes = frozenset({EventType.TOOL_CALL, EventType.LLM_RESPONSE,
                            EventType.RUN_END})

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
        reasoning_tokens, reasoning_ratio = self._reasoning_volume(traj)

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
                "reasoning_tokens": float(reasoning_tokens),
                "reasoning_ratio": reasoning_ratio,
            },
        )

    @staticmethod
    def _reasoning_volume(traj: Trajectory) -> tuple[int, float]:
        """推理 token 数，以及它占 completion tokens 的比例。

        ## 为什么这算效率指标

        它量的是"模型把多少产出花在了评测看不见的地方"。实测
        （2026-09-16 全量真跑，17 条 / 194 个响应）是 **34.5%** ——
        也就是说三分之一的产出对现有全部评测器不可见。

        ## 为什么不做成一个"推理质量分"

        **实测挡住的**：拿那份真实数据测过三种"推理质量"的代理信号
        （关键词频率、推理与工具输出的 token 重合度、推理里声称要读的文件
        vs 实际读的文件），三种与结果**都没有相关性**。

        没有信号就不该造指标 —— 那种指标看起来有意义，实际是噪音，
        而它最坏的后果是让人**去调模型**。所以这里只报体量：
        一个不需要判断力、provider 自己上报的客观量
        （与"走了多少步""重复调了几次"同一类）。

        `reasoning_tokens` 缺省 0 是**事实**而不是"不适用"，所以键一定在
        （§2.1：`metrics` 只能靠键缺席表达"不适用"，这里刻意不用那个逃生口 ——
        "这个模型不推理"与"我们没去看"必须能区分）。
        """
        tokens = sum(r.tokens for r in traj.reasoning())
        output = traj.output_tokens
        if output <= 0:
            # 没有 usage 的 provider 会让它全是 0 —— 而那正是"接一个新网关"
            # 时的默认状态，不是异常。
            return tokens, 0.0
        # `output_tokens` 是**含**推理的 completion tokens。某些 provider
        # 把两者算成不同源，会让比率大于 1 —— 那在报告里读起来像
        # "推理比产出还多"，而实际是"这两个数不可比"。按 1 封顶，
        # 原始 token 数照常保留。
        return tokens, min(tokens / output, 1.0)
