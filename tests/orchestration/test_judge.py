"""Judge 装配层测试。

## 对称性的可执行证据

「judge 与被测 agent 复用同一个 Run 类」这句话如果只写在文档里，
半年后有人加一个 `JudgeRun` 子类就悄悄破功了。所以这里断言的是
**同一个类型**（`RunSpec` / `Run`），不是"看起来像"。

## 两条硬性约束各有对应测试

  - judge 不能触发新的 judge（递归失控）
  - judge 预算独立（judge_cost 指标靠这个分离才有意义）
"""

from __future__ import annotations

from typing import Any

import pytest

from harness.contracts.protocols import EvalContext, JudgeCase
from harness.contracts.spec import RunRole, RunSpec, RunStatus
from harness.core.run import RunResult
from harness.core.tools.introspect import ReadTrajectoryTool
from harness.events.trajectory import Trajectory
from harness.orchestration.judge import (
    JUDGE_TOOLS,
    JudgeConfig,
    RunBasedJudgeClient,
    build_judge_spec,
    build_judge_tools,
    meta_trajectory,
    parse_verdict,
)
from harness.testing import TrajectoryBuilder as TB


def _cfg(**kw: Any) -> JudgeConfig:
    # 值类型不一（str/float/int），所以 base 必须是 dict[str, Any] ——
    # 推成 dict[str, str] 的话每个数值参数都会被判类型错。
    base: dict[str, Any] = {"model": "strong-model", "rubric": "Rate 0-1."}
    base.update(kw)
    return JudgeConfig(**base)


# ---- spec ----
def test_judge_spec_uses_same_run_spec_type_as_sut():
    """★ 对称性的可执行证据：judge 的 spec 与 sut 是同一个类型，只是取值不同。"""
    spec = build_judge_spec(_cfg(), run_id="j1", trajectory_ref="r1")
    assert isinstance(spec, RunSpec)
    assert spec.role is RunRole.JUDGE
    assert spec.role is not RunRole.SUT


def test_judge_spec_has_no_max_turns_field_because_budget_owns_it():
    """`RunSpec` **刻意不设** `max_turns` —— Budget 是轮次上限的唯一真相源。

    计划里写的是 `RunSpec(..., max_turns=config.max_turns)`，
    那会直接 ValidationError（extra=forbid）。这条测试把"为什么不能加回去"钉住：
    两个都能配的时候，实现读哪个就不明确了。
    """
    spec = build_judge_spec(_cfg(max_turns=5), run_id="j1", trajectory_ref="r1")
    assert "max_turns" not in type(spec).model_fields
    assert spec.budget.max_turns == 5


def test_judge_spec_uses_a_fresh_task_by_default():
    spec = build_judge_spec(_cfg(), run_id="j1", trajectory_ref="r1")
    assert spec.task is not None
    assert "r1" in spec.task.case_id or "r1" in spec.task.prompt


def test_judge_tools_do_not_include_anything_that_could_recurse():
    """★ judge 绝不能触发新的 judge —— 递归失控防护。"""
    spec = build_judge_spec(_cfg(), run_id="j1", trajectory_ref="r1")
    assert spec.tools.allow is not None
    for forbidden in ("spawn_judge", "judge", "finish_judging_others"):
        assert forbidden not in spec.tools.allow


def test_every_whitelisted_judge_tool_actually_exists():
    """★ 白名单里写了却不存在，等于 judge 拿到一个点了就报错的工具。

    `PermissionMiddleware` 只校验"调用的工具在不在 allow 里"，
    **不校验 allow 里的名字有没有对应实现** —— 拼错的名字不会有任何症状，
    直到 judge 真的去调它。
    """
    registry = build_judge_tools(TB(run_id="s1").build())
    known = set(registry.names())
    assert set(JUDGE_TOOLS) <= known, f"白名单里有不存在的工具：{set(JUDGE_TOOLS) - known}"


def test_judge_has_its_own_budget_not_shared_with_sut():
    """★ judge 成本绝不能混进 sut 的 cost —— judge_cost 指标靠这个分离才有意义。"""
    spec = build_judge_spec(_cfg(max_usd=0.5), run_id="j1", trajectory_ref="r1")
    assert spec.budget.max_usd == 0.5


def test_judge_spec_carries_a_workspace():
    """没有 workspace，judge 的 `read_file` 会全部失败（实测踩过 ctx.ws=None）。"""
    spec = build_judge_spec(_cfg(), run_id="j1", trajectory_ref="r1")
    assert spec.workspace is not None


def test_judge_rubric_reaches_the_system_prompt():
    spec = build_judge_spec(_cfg(rubric="Only correctness matters."),
                            run_id="j1", trajectory_ref="r1")
    assert "Only correctness matters." in spec.system_prompt
    assert "VERDICT" in spec.system_prompt, "judge 必须知道输出格式，否则解析不出判定"


def test_judge_metadata_points_back_at_the_judged_trajectory():
    """可审计性：从 judge 的 spec 能查到它在评谁。"""
    spec = build_judge_spec(_cfg(), run_id="j1", trajectory_ref="sut-run-42")
    assert spec.metadata["judged_trajectory"] == "sut-run-42"


def test_a_judge_spec_is_not_declared_as_a_sut_agent():
    spec = build_judge_spec(_cfg(), run_id="j1", trajectory_ref="r1")
    assert spec.agent_name.startswith("judge")


# ---- 工具集装配 ----
def test_judge_tools_exclude_the_sut_toolset():
    """Judge 不该拿到 write_file / run_command —— 它是来查的，不是来改的。"""
    registry = build_judge_tools(TB(run_id="s1").build())
    names = set(registry.names())
    assert "write_file" not in names
    assert "run_command" not in names
    assert "read_trajectory" in names


def test_read_trajectory_is_bound_to_the_subject():
    subject = TB(run_id="sut1").turn().llm_response(text="hi").run_end().build()
    registry = build_judge_tools(subject)
    tool = registry.get("read_trajectory")
    assert isinstance(tool, ReadTrajectoryTool)


# ---- 判定解析 ----
@pytest.mark.parametrize("text,expected", [
    ("VERDICT: pass", "pass"),
    ("blah\nVERDICT: FAIL\nblah", "fail"),
    ("verdict: partial", "partial"),
    ("I think it's fine", "uncertain"),
    ("", "uncertain"),
])
def test_verdict_parsing(text, expected):
    assert parse_verdict(text) == expected


def test_verdict_parsing_is_case_insensitive():
    assert parse_verdict("Verdict: PASS") == "pass"


def test_partial_wins_over_pass_when_both_appear():
    """模型偶尔会两种都写；按更保守的那个取，避免乐观误判。"""
    assert parse_verdict("VERDICT: pass or VERDICT: partial") == "partial"


# ---- JudgeClient 实现 ----
class _FakeRun:
    def __init__(self, spec, *, output: str, run_id: str = "j-run") -> None:
        self.spec = spec
        self._output = output
        self.run_id = run_id

    async def execute(self) -> RunResult:
        from harness.contracts.results import Usage

        traj = (TB(run_id=self.run_id, role="judge").turn()
                .llm_response(text=self._output)
                .run_end(status="ok").build())
        return RunResult(run_id=self.run_id, status=RunStatus.OK,
                         final_output=self._output, trajectory=traj,
                         usage=Usage(cost_usd=0.02, input_tokens=100,
                                     output_tokens=20, calls=1),
                         turns=1, tool_calls=0, duration_s=0.1)


def _client(output: str = "VERDICT: pass", **kw) -> RunBasedJudgeClient:
    specs: list[RunSpec] = []

    def factory(spec: RunSpec, subject: Trajectory | None) -> _FakeRun:
        # 签名必须与 RunBasedJudgeClient 的调用约定一致。
        # 少一个参数的话，装配错误会被**故意**留在 try 之外冒泡 —— 见 client 的实现。
        specs.append(spec)
        return _FakeRun(spec, output=output, run_id=f"j{len(specs)}")

    client = RunBasedJudgeClient(factory, _cfg(**kw))
    client._specs = specs  # type: ignore[attr-defined]
    return client


def _case() -> JudgeCase:
    traj = TB(run_id="sut1").turn().llm_response(text="done").run_end().build()
    return JudgeCase(case_id="sut1", task="fix it", traj=traj, rubric="r")


async def test_judge_client_runs_a_real_run_per_repeat():
    client = _client()
    verdicts = await client.judge(_case(), repeat=3)
    assert len(verdicts) == 3
    assert len(client._specs) == 3  # type: ignore[attr-defined]


async def test_every_verdict_points_at_its_own_judge_run():
    """★ 元评测靠这个指针找到 judge 自己的轨迹。"""
    verdicts = await _client().judge(_case(), repeat=3)
    ids = [v.judge_run_id for v in verdicts]
    assert len(set(ids)) == 3, "每次 judge 必须是独立 run，否则一致性无从谈起"
    assert all(i for i in ids)


async def test_verdict_carries_the_judge_usage_signature():
    v = (await _client().judge(_case()))[0]
    assert v.usage is not None
    assert v.usage.cost_usd == 0.02


async def test_judge_specs_are_built_from_the_case_not_shared():
    client = _client()
    await client.judge(_case(), repeat=2)
    specs = client._specs  # type: ignore[attr-defined]
    assert specs[0] is not specs[1]
    assert all(s.role is RunRole.JUDGE for s in specs)


async def test_the_client_records_its_judge_runs_for_meta_evaluation():
    client = _client()
    await client.judge(_case(), repeat=2)
    assert len(client.judge_results) == 2
    assert all(r.trajectory.run_id.startswith("j") for r in client.judge_results)


async def test_a_runtime_failure_becomes_uncertain_not_a_crash():
    """Judge **跑**挂了不能把评测器一起拖崩 —— 那会把外部故障记成 agent 的失败。"""
    class _Exploding:
        run_id = "j-x"

        async def execute(self):
            raise RuntimeError("judge provider exploded")

    client = RunBasedJudgeClient(lambda spec, subject: _Exploding(), _cfg())
    verdicts = await client.judge(_case())
    assert verdicts[0].verdict == "uncertain"
    assert "exploded" in verdicts[0].rationale


async def test_a_wiring_failure_bubbles_up_instead_of_being_swallowed():
    """★ 装配错误必须冒泡，不能被"judge 总是判 uncertain"掩盖。

    `run_factory` 签名不对是**编程错误**，不是运行期故障。
    两者混为一谈的话，症状是"judge 每次都拿不定主意"，
    而真正的原因（接线错了）永远不会现身。
    """
    def wrong_signature(spec):          # 少一个参数
        raise AssertionError("unreachable")

    client = RunBasedJudgeClient(wrong_signature, _cfg())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await client.judge(_case())


# ---- 元轨迹 ----
async def test_meta_trajectory_concatenates_judge_runs():
    """judge_repeat=N 会产生 N 条独立 run；一致性要在**一条**元轨迹上看。

    `Trajectory` 的 `seq` 是索引键，直接拼接会撞 —— 所以重新编号。
    """
    client = _client()
    await client.judge(_case(), repeat=3)
    meta = meta_trajectory(client.judge_results)

    assert isinstance(meta, Trajectory)
    assert len(meta.llm_responses()) == 3
    seqs = [e.seq for e in meta.events]
    assert seqs == list(range(len(seqs))), "拼接后必须重新编号，否则索引会撞"


def test_meta_trajectory_of_nothing_is_empty_not_an_error():
    assert meta_trajectory([]).events == ()


def test_meta_trajectory_keeps_run_start_and_end():
    """对称性的可见回报：元轨迹本身也是一条结构完整的轨迹。"""
    import asyncio

    client = _client()
    asyncio.run(client.judge(_case(), repeat=2))
    meta = meta_trajectory(client.judge_results)
    start, end = meta.start(), meta.end()
    assert start is not None and end is not None
    assert start.role == "judge"


# ---- 与 EvalContext 的接线 ----
def test_eval_context_accepts_a_judge_client():
    ctx = EvalContext(judge=_client())
    assert ctx.judge is not None


def test_judge_client_satisfies_the_protocol():
    """评测器只认 `JudgeClient` 协议 —— 实现必须真的符合它。"""
    from harness.contracts.protocols import JudgeClient

    assert isinstance(_client(), JudgeClient)
