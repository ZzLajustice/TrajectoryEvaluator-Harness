"""M4 的集成测试：预算与压缩在**真实 run 里**的行为。

单测证明每个部件是对的；这里证明它们**接进 loop 之后**仍然对 ——
孤立通过的类不等于能用的功能（M2/M3 已经踩过这个坑：
`RecordingProvider` 实现了却没接 CLI）。
"""

from __future__ import annotations

from typing import Any

import pytest

from harness.contracts.protocols import ToolCall, ToolResult
from harness.contracts.spec import (
    Budget,
    MiddlewareSpec,
    ModelRef,
    RunRole,
    RunSpec,
    RunStatus,
    TaskSpec,
    ToolPolicy,
)
from harness.core.middleware.factory import CANONICAL_ORDER, build_middlewares
from harness.core.registry import ToolRegistry
from harness.core.run import Run, RunDeps
from harness.core.tools.finish import FinishTool
from harness.events.types import EventType
from harness.providers.fake import FakeProvider, tool_call_response
from harness.store.jsonl import JsonlStore


class _EchoTool:
    name = "echo"

    @property
    def description(self) -> str:
        return "echoes"

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}}}

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:
        return ToolResult(call_id=call.call_id, name=self.name, ok=True,
                          content="y" * int(call.arguments.get("size", 10)))


def _deps(provider: Any, tmp_path: Any, middlewares: Any = ()) -> RunDeps:
    reg = ToolRegistry()
    reg.register(FinishTool())
    reg.register(_EchoTool())
    return RunDeps(provider=provider, store=JsonlStore(tmp_path), tools=reg,
                   middlewares=middlewares)


def _spec(*, budget: Budget | None = None, allow: list[str] | None = None,
          task: str | None = None) -> RunSpec:
    return RunSpec(
        role=RunRole.SUT, system_prompt="sys",
        model=ModelRef(provider="fake", model="fake"),
        task=TaskSpec(case_id="c1", prompt=task) if task else None,
        tools=ToolPolicy(allow=allow),
        budget=budget or Budget(max_turns=5),
    )


# ---- 任务提示词真的进了请求 ----
async def test_task_prompt_reaches_the_model(tmp_path):
    """★ agent 必须能看到任务 —— 否则它根本不知道要做什么。"""
    p = FakeProvider([tool_call_response("finish", {"summary": "d"})])
    await Run(_spec(task="fix the widget bug"), _deps(p, tmp_path)).execute()

    sent = p.requests[0].messages
    assert sent[0].role == "system"
    assert sent[1].role == "user"
    assert sent[1].content[0]["text"] == "fix the widget bug"


# ---- 预算终止 ----
async def test_tool_call_budget_denies_calls_beyond_the_limit(tmp_path):
    """工具调用超限时每次超额调用被拦下。

    `max_turns` 与工具调用数**要对齐**：只给 3 条脚本响应却跑 5 轮，
    第 4 轮会因脚本用尽而变成 LLM_ERROR —— 那是测试自己的坑，
    不是被测行为。
    """
    mw = build_middlewares([MiddlewareSpec(name="budget")])
    p = FakeProvider([
        tool_call_response("echo", {}, call_id="c1"),
        tool_call_response("echo", {}, call_id="c2"),
        tool_call_response("finish", {"summary": "d"}, call_id="c3"),
    ])
    budget = Budget(max_turns=3, max_tool_calls=1)
    result = await Run(_spec(budget=budget), _deps(p, tmp_path, mw)).execute()

    # limit=1 允许第一次调用，其余两次（含 finish）都被拦
    denied = [r for r in result.trajectory.tool_results() if r.denied_by == "budget"]
    assert len(denied) == 2
    assert denied[0].error_type == "budget_exceeded"
    assert result.status is RunStatus.MAX_TURNS


async def test_budget_event_is_recorded(tmp_path):
    mw = build_middlewares([MiddlewareSpec(name="budget")])
    p = FakeProvider([
        tool_call_response("echo", {}, call_id="c1"),
        tool_call_response("echo", {}, call_id="c2"),
        tool_call_response("finish", {"summary": "d"}, call_id="c3"),
    ])
    budget = Budget(max_turns=3, max_tool_calls=1)
    result = await Run(_spec(budget=budget), _deps(p, tmp_path, mw)).execute()

    events = result.trajectory.of(EventType.BUDGET_EVENT)
    assert events, "超限必须留下 BUDGET_EVENT —— 否则事后无法解释终止原因"


async def test_turn_budget_terminates_with_max_turns(tmp_path):
    """轮次用尽报 `MAX_TURNS`，与 `BUDGET_EXCEEDED` 语义不同。

    前者是「一直在行动但没收敛」，后者是其他资源（token / 金额 / 时间）用尽。
    两者都不是 `llm_error`。
    """
    p = FakeProvider([tool_call_response("echo", {}, call_id=f"c{i}") for i in range(10)])
    result = await Run(_spec(budget=Budget(max_turns=3)),
                       _deps(p, tmp_path)).execute()
    assert result.status is RunStatus.MAX_TURNS
    assert result.turns == 3


# ---- 压缩 ----
async def test_compaction_event_carries_run_id_and_seq(tmp_path):
    """★ 事件必须带正确的 run_id / seq。

    ContextManager 只给模板，填充由 loop 负责 ——
    忘了填充会产生 run_id 为空的事件，破坏轨迹完整性。
    """
    budget = Budget(max_turns=30, max_input_tokens=300)
    p = FakeProvider(
        [tool_call_response("echo", {"size": 400}, call_id=f"c{i}") for i in range(29)]
        + [tool_call_response("finish", {"summary": "d"}, call_id="cf")]
    )
    result = await Run(_spec(budget=budget), _deps(p, tmp_path)).execute()

    compactions = result.trajectory.compactions()
    assert compactions, "超预算时应触发压缩"
    for c in compactions:
        assert c.run_id == result.run_id
        assert c.seq > 0
        assert c.tokens_after < c.tokens_before


async def test_compaction_does_not_orphan_tool_pairings(tmp_path):
    """★ 悬空配对会让下一次 API 调用 400，且错误指不到元凶。"""
    budget = Budget(max_turns=30, max_input_tokens=300)
    p = FakeProvider(
        [tool_call_response("echo", {"size": 400}, call_id=f"c{i}") for i in range(29)]
        + [tool_call_response("finish", {"summary": "d"}, call_id="cf")]
    )
    result = await Run(_spec(budget=budget), _deps(p, tmp_path)).execute()
    assert result.trajectory.compactions()

    # 最后一次请求的 messages 里，每个 tool 结果都要有对应的调用
    last = p.requests[-1].messages
    seen_calls: set[str] = set()
    for m in last:
        for block in m.content:
            if block.get("type") == "tool_use":
                seen_calls.add(block["id"])
            elif block.get("type") == "tool_result":
                assert block["tool_call_id"] in seen_calls, (
                    f"orphaned tool_result {block['tool_call_id']} in the final request")


# ---- 中间件工厂 ----
def test_factory_enforces_canonical_order_regardless_of_config_order():
    """顺序是安全语义 —— 配置写反了也不能真的反。"""
    specs = [MiddlewareSpec(name=n) for n in reversed(CANONICAL_ORDER)]
    names = [mw.name for mw in build_middlewares(specs)]
    assert names == list(CANONICAL_ORDER)


def test_factory_skips_disabled_middlewares():
    specs = [MiddlewareSpec(name="permission", enabled=False),
             MiddlewareSpec(name="telemetry")]
    assert [mw.name for mw in build_middlewares(specs)] == ["telemetry"]


def test_factory_rejects_unknown_middleware_names():
    """拼错的名字必须报错 —— 静默忽略会让人以为策略生效了。"""
    with pytest.raises(ValueError, match="unknown middleware"):
        build_middlewares([MiddlewareSpec(name="permision")])


def test_factory_returns_empty_for_empty_config():
    assert build_middlewares([]) == []


async def test_full_middleware_stack_does_not_break_a_normal_run(tmp_path):
    """六个中间件全开时，一次正常 run 仍应成功 —— 别把正常工作流拦死。"""
    mw = build_middlewares([MiddlewareSpec(name=n) for n in CANONICAL_ORDER])
    p = FakeProvider([tool_call_response("finish", {"summary": "done"})])
    result = await Run(_spec(allow=None), _deps(p, tmp_path, mw)).execute()
    assert result.status is RunStatus.OK
