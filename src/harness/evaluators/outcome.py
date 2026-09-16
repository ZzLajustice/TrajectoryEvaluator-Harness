"""结果级评测器 —— 在工作目录里跑隐藏验收测试。

## 它填的是哪个洞

M1–M10 的 5 个评测器**全是轨迹级**的：`TrajectoryMatcher`、`EfficiencyAnalyzer`、
`FailureClassifier`、`GroundingChecker`、`MetaEvaluator` 都只读事件流。
它们能回答"过程好不好"，**没有一个能回答"代码到底修对了没有"**。

设计文档写着「golden 是次要判据，**outcome 永远是主判据**」——
所以这个评测器不是可选项。少了它，一个"步数很漂亮、全程无重复调用、
grounding 干净"的 run 会拿满分，而它的修复补丁可能是空的。

## 与 judge 的关系：**分工，不是替代**

  - outcome（本模块）：确定性、零成本、**二值**。判"最终产物对不对"
  - judge（`JudgeClient`）：概率性、有成本、能给出解释。判"过程合不合理"

两者**必须分列**（设计文档 §4.6）：把 outcome 折进一个总分里，
过程级评测的意义就会被结果淹没 —— 而把过程指标折进 outcome，
这个评测器就退化成了 `pytest` 的包装。

## 为什么必须"在现场"跑

判据的输入是被测 agent 改出来的代码，而它只存在于工作目录里。
目录一销毁，判据就没了。所以组装层让这条路径上的工作目录**活到评测结束**
（见 `orchestration/deps.py::_run_case`）。

## 判定的三个层次，别混

    PASS   命令退出码 0                      —— 修好了
    FAIL   退出码非 0，且**确实跑了测试**     —— 没修好
    ERROR  没跑成（用例坏了 / runner 崩了）   —— **判不了**

第三类是最容易写错的地方。pytest 对"一条用例都没收集到"返回退出码 5，
只看 `ok` 的话它和"测试失败"（1）长得一模一样，
于是"任务无解但被记成模型失败"的脏数据就混进指标里了 ——
而那种脏数据会让失败率虚高，把真失败淹没。
"""

from __future__ import annotations

from collections.abc import Sequence

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalResult, EvalStatus, Finding, Severity
from harness.evaluators.base import BaseEvaluator
from harness.events.trajectory import Trajectory
from harness.events.types import EventType

#: 证据里保留多少字符的测试输出。够显示几条 FAILED 行，不至于把报告撑爆。
_OUTPUT_EXCERPT = 2000

#: pytest 在"没收集到用例"时打的字。命中它说明**用例本身坏了**，不是 agent 失败。
_NO_TESTS_MARKERS = ("no tests ran", "collected 0 items")


class OutcomeGrader(BaseEvaluator):
    """跑隐藏验收测试，把退出码变成 PASS / FAIL / ERROR。"""

    name = "OutcomeGrader"
    version = "1.0.0"
    #: 订阅 RUN_END 而不是留空。
    #:
    #: ★ 空的 `subscribes` 意味着**永远不跑** —— `run_evaluators` 的跳过判据是
    #: `if not (cls.subscribes & present): continue`。而"被跳过"在报告里
    #: 与"没问题"长得一样，所以这个坑只会以"结果不对"的形式暴露。
    subscribes = frozenset({EventType.RUN_END})

    def __init__(
        self,
        argv: Sequence[str] | None = None,
        *,
        timeout_s: float = 120.0,
    ) -> None:
        self.argv = list(argv) if argv else []
        # 隐藏测试比 agent 的单条命令更容易超时（要装环境、跑全量），
        # 所以超时是独立的配置项而不是沿用 run_command 的默认值。
        self.timeout_s = timeout_s

    async def evaluate(self, traj: Trajectory, ctx: EvalContext) -> EvalResult:
        if not self.argv:
            return self.skipped(traj, "no hidden-test command configured")
        if ctx.runner is None:
            return self.skipped(
                traj,
                "no workspace runner available — case was not graded on outcome")

        try:
            result = await ctx.runner.run(self.argv, timeout_s=self.timeout_s)
        except Exception as exc:  # noqa: BLE001
            # runner 崩了是**我们的**问题，不是 agent 的。
            return EvalResult(
                evaluator=self.name, evaluator_version=self.version,
                run_id=traj.run_id, status=EvalStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                summary="could not run the hidden tests",
            )

        output = result.content or ""
        if not result.ok and _looks_like_no_tests_ran(output):
            # 用例坏了 ≠ agent 失败。这条分支挡的正是数据集污染。
            return EvalResult(
                evaluator=self.name, evaluator_version=self.version,
                run_id=traj.run_id, status=EvalStatus.ERROR,
                error="the hidden tests collected no test cases — the case itself "
                      "is broken, this is not an agent failure",
                summary="hidden tests collected nothing",
                metrics={"outcome_pass": 0.0},
                findings=[Finding(
                    code="outcome.no_tests_collected",
                    message="隐藏测试没有收集到任何用例；先修用例再谈 agent 的对错",
                    severity=Severity.MAJOR,
                    data={"output": _excerpt(output)},
                )],
            )

        findings: list[Finding] = []
        if result.truncated:
            # 退出码仍然可信（它不受输出截断影响），但证据不完整。
            # 留一条 MINOR 而不是改判 —— 改判会把"输出太长"记成"没修好"。
            findings.append(Finding(
                code="outcome.output_truncated",
                message=f"隐藏测试的输出超过上限被截断，证据不完整"
                        f"（退出码 {_describe_exit(result)} 仍然可信）",
                severity=Severity.MINOR,
                data={"output": _excerpt(output)},
            ))

        if result.ok:
            return EvalResult(
                evaluator=self.name, evaluator_version=self.version,
                run_id=traj.run_id, status=EvalStatus.PASS,
                score=1.0, summary="hidden tests passed",
                metrics={"outcome_pass": 1.0},
                findings=findings,
            )

        findings.append(Finding(
            code="outcome.hidden_tests_failed",
            message="隐藏验收测试未通过 —— 修复没有达到验收标准",
            severity=Severity.MAJOR,
            category="no_incomplete_verification",
            data={"output": _excerpt(output), "error": result.error},
        ))
        return EvalResult(
            evaluator=self.name, evaluator_version=self.version,
            run_id=traj.run_id, status=EvalStatus.FAIL,
            score=0.0, summary="hidden tests failed",
            metrics={"outcome_pass": 0.0},
            findings=findings,
        )


def _looks_like_no_tests_ran(output: str) -> bool:
    return any(marker in output for marker in _NO_TESTS_MARKERS)


def _describe_exit(result: object) -> str:
    error = getattr(result, "error", None)
    return str(error) if error else "非 0"


def _excerpt(text: str) -> str:
    """留头部 —— pytest 把失败摘要打在最前面那几行。"""
    return text[:_OUTPUT_EXCERPT]
