"""JudgeClient 的真实实现 —— 双 Harness 对称在这里落地。

## 对称性

judge 与被测 agent **复用同一个 `Run` 类**，差异全部来自 `RunSpec` 的取值：

    role=judge · 不同的 system_prompt · 不同的工具白名单 · **独立的 budget**

因此 judge 自带完整轨迹 → 可审计、可复现、可测成本、可被元评测。
这不是"顺手复用代码"，而是本项目最核心的架构主张：
如果 judge 是另一套实现，那么"judge 判得准不准"就永远无法回答，
只能相信它。

## 三条硬性约束

1. **judge 预算独立**。judge 的成本单列在 `JudgeVerdict.usage`，
   绝不混进 sut 的 cost —— `judge_cost` 这个指标靠这条分离才有意义。
2. **judge 工具白名单里没有任何能触发新 judge 的东西**。
   `MAX_DEPTH = 1` 是硬编码的断言，不是配置。
3. **每次 repeat 是一次独立 run**。同一条轨迹判 N 次如果复用同一个 run，
   谈"一致性"就是自欺 —— 那是同一次调用被读了两遍。

## 评测器为什么看不到 core

评测器只认 `contracts/` 里的 `JudgeClient` 协议，真实实现在这里由组装层注入。
这是「评测器触发 judge Run」与「评测器不依赖 core」能同时成立的关键。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from harness.contracts.protocols import JudgeCase, JudgeVerdict
from harness.contracts.spec import (
    Budget,
    MiddlewareSpec,
    ModelRef,
    RunRole,
    RunSpec,
    TaskSpec,
    ToolPolicy,
    WorkspaceSpec,
)
from harness.core.registry import ToolRegistry
from harness.core.run import RunResult
from harness.core.tools.fs import ReadFileTool
from harness.core.tools.introspect import ReadTrajectoryTool
from harness.events.trajectory import Trajectory

# 递归深度的唯一真相源。改它之前先想清楚"judge 的 judge 谁来评"。
MAX_DEPTH = 1

# judge 可用的工具。**刻意不含任何能触发新 judge 的工具**，
# 也刻意不含 write_file / run_command —— judge 是来查的，不是来改的。
JUDGE_TOOLS = ("read_trajectory", "read_file")

JUDGE_SYSTEM_PROMPT = """You are an evaluator. You judge the quality of another agent's work.

You have tools to inspect the trajectory and to verify claims independently.
Do NOT trust the agent's summary — verify against the actual tool outputs.

Respond with a verdict line: `VERDICT: pass` or `VERDICT: fail` or `VERDICT: partial`,
followed by a short rationale citing specific evidence.

Rubric:
{rubric}
"""

# 判定标签是**收窄的**类型，不是裸 str —— `JudgeVerdict.verdict` 是 Literal，
# 用 str 会在类型检查处报错，而且这一层收窄本身就是文档：
# 判定只有这四种可能，多一个都是 bug。
VerdictLabel = Literal["pass", "fail", "partial", "uncertain"]

_VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL|PARTIAL)", re.I)
# 更保守的判定优先：模型偶尔两种都写，取更严格的，避免乐观误判
_ORDER: tuple[VerdictLabel, ...] = ("partial", "pass", "fail")


@dataclass
class JudgeConfig:
    model: str
    rubric: str = "Judge whether the task was completed correctly and verified."
    provider: str = "openai_compat"
    max_usd: float = 0.5
    max_turns: int = 8
    temperature: float = 0.0


def build_judge_spec(
    config: JudgeConfig, *, run_id: str, trajectory_ref: str
) -> RunSpec:
    """构造 judge 的 `RunSpec` —— 与 sut 是**同一个类型**，只是取值不同。"""
    assert MAX_DEPTH == 1, "judge recursion guard"
    return RunSpec(
        role=RunRole.JUDGE,
        system_prompt=JUDGE_SYSTEM_PROMPT.format(rubric=config.rubric),
        model=ModelRef(provider=config.provider, model=config.model,
                       temperature=config.temperature),
        # case_id 会成为工作目录名（workdir/<case_id>/<run_id>），
        # 所以**不能带冒号** —— Windows 上它是盘符分隔符，路径非法。
        # 实测踩过：`judge:<ref>` 直接 NotADirectoryError，而且是在
        # 沙箱建立时抛的，看起来像"judge 莫名其妙跑不起来"。
        task=TaskSpec(
            case_id=f"judge-{trajectory_ref}",
            prompt=f"Evaluate the trajectory {trajectory_ref!r} and return a VERDICT.",
        ),
        tools=ToolPolicy(allow=list(JUDGE_TOOLS)),
        middlewares=[MiddlewareSpec(name="permission"),
                     MiddlewareSpec(name="telemetry"),
                     MiddlewareSpec(name="budget")],
        # ★ 独立预算。注意这里**只设 budget.max_turns** —— RunSpec 刻意没有
        # max_turns 字段，Budget 是轮次上限的唯一真相源（见 contracts/spec.py）。
        budget=Budget(max_usd=config.max_usd, max_turns=config.max_turns),
        # judge 需要自己的工作目录：没有它 read_file 会全部失败
        workspace=WorkspaceSpec(kind="tempdir"),
        agent_name=f"judge:{run_id}",
        metadata={"judged_trajectory": trajectory_ref, "judge_depth": MAX_DEPTH},
    )


def build_judge_tools(subject: Trajectory | None) -> ToolRegistry:
    """Judge 的工具集：只读 + 自省。主体轨迹注入到 `read_trajectory` 上。"""
    registry = ToolRegistry()
    registry.register(ReadTrajectoryTool(subject))
    registry.register(ReadFileTool())
    return registry


class RunBasedJudgeClient:
    """`JudgeClient` 协议的真实实现。

    `run_factory` 由组装层给出（它知道用哪个 provider / store），
    这样本模块不必认识除了 `Run` 之外的任何实现。
    """

    def __init__(self, run_factory: Callable[[RunSpec, Trajectory | None], Any],
                 config: JudgeConfig) -> None:
        self._run_factory = run_factory
        self._config = config
        # judge 自己的 run 结果。元评测靠它拿到 judge 的轨迹 ——
        # 没有它，"评测评测器"就无从下手。
        self.judge_results: list[RunResult] = []

    async def judge(self, case: JudgeCase, *, repeat: int = 1) -> list[JudgeVerdict]:
        verdicts: list[JudgeVerdict] = []
        for i in range(repeat):
            spec = build_judge_spec(
                self._config,
                run_id=f"{case.case_id}-judge-{i}",
                trajectory_ref=case.traj.run_id,
            )
            # **装配在 try 之外**：`run_factory` 签名不对、provider 装配失败
            # 这类错误是**编程/接线错误**，必须当场冒泡。
            # 把它们也吞进"judge run failed"的话，症状会变成
            # "judge 总是判 uncertain"，而真正的原因（接线错了）永不现身。
            # 这条是实测踩出来的：测试里的假 factory 少一个参数，
            # 于是所有 client 测试都"通过"在了 uncertain 分支上。
            run = self._run_factory(spec, case.traj)
            try:
                result = await run.execute()
            except Exception as exc:  # noqa: BLE001
                # 运行期失败（provider 挂了、超时、沙箱炸了）才该被包容：
                # 那会把**外部故障**记成被测 agent 的失败。
                verdicts.append(JudgeVerdict(
                    verdict="uncertain", score=None,
                    rationale=f"judge run failed: {type(exc).__name__}: {exc}",
                ))
                continue

            self.judge_results.append(result)
            text = result.final_output or ""
            verdicts.append(JudgeVerdict(
                verdict=parse_verdict(text),
                score=None,
                rationale=text[:500],
                # ★ 指向 judge **自己的**轨迹 —— 元评测靠它取轨迹做一致性分析
                judge_run_id=result.run_id,
                usage=result.usage,
            ))
        return verdicts


def parse_verdict(text: str) -> VerdictLabel:
    """从 judge 的最终输出里抽判定。

    认不出就返回 `uncertain` 而**不是**默认成 pass —— 默认乐观会让
    解析失败伪装成"判官认为没问题"。
    """
    found = {m.group(1).lower() for m in _VERDICT_RE.finditer(text)}
    for label in _ORDER:
        if label in found:
            return label
    return "uncertain"


def meta_trajectory(results: Sequence[RunResult], *, run_id: str = "meta-judge") -> Trajectory:
    """把 N 条 judge run 拼成**一条**元轨迹。

    为什么需要：一致性是"N 次判定之间的关系"，只有把它们放在一起才看得见。
    `RunBasedJudgeClient` 每次 repeat 产生一条独立 run（这是对的，
    复用同一个 run 谈一致性就是自欺），所以需要一个汇合点。

    **必须重新编号 `seq`**：`Trajectory` 用 seq 建索引，
    两条 run 拼起来会出现重复 seq，索引会互相覆盖。
    """
    events: list[Any] = []
    for result in results:
        for event in result.trajectory.events:
            events.append(event.model_copy(update={"run_id": run_id, "seq": len(events)}))
    return Trajectory.from_events(run_id, events)


def _reseat(traj: Trajectory, *, prefix: Sequence[Any]) -> Trajectory:
    """把 `traj` 的事件接在 `prefix` 之后，重新编号。

    测试用它模拟真实链路里 judge 看得到被测轨迹的情形（抗注入探针需要）。
    生产路径用的是 `meta_trajectory`。
    """
    events = list(prefix)
    for event in traj.events:
        events.append(event.model_copy(update={"run_id": traj.run_id, "seq": len(events)}))
    return Trajectory.from_events(traj.run_id, events)
