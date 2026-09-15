"""工具注册表与 finish 工具测试。

`finish` 不只是一个工具 —— 它是 agent 声明"我完成了"的**唯一契约**。
FailureClassifier 检测「Unaware of termination」正是靠"有没有调用 finish"，
所以它的语义必须稳定。
"""

from __future__ import annotations

from typing import Any

import pytest

from harness.contracts.protocols import ToolCall, ToolResult
from harness.contracts.spec import ToolPolicy
from harness.core.registry import ToolRegistry
from harness.core.tools.finish import FinishTool


class _StubTool:
    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def description(self) -> str:
        return "stub"

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": {}}}

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:
        return ToolResult(call_id=call.call_id, name=self.name, ok=True, content="stub")


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FinishTool())
    reg.register(_StubTool("read_file"))
    reg.register(_StubTool("write_file"))
    return reg


def test_register_and_get():
    assert _registry().get("finish").name == "finish"


def test_get_unknown_tool_lists_available_names():
    with pytest.raises(KeyError, match="finish"):
        _registry().get("nope")


def test_names_are_sorted():
    assert _registry().names() == ["finish", "read_file", "write_file"]


def test_allow_list_filters_schemas():
    names = [s["name"] for s in _registry().schemas(ToolPolicy(allow=["read_file"]))]
    assert names == ["read_file"]


def test_deny_list_filters_schemas():
    names = [s["name"] for s in _registry().schemas(ToolPolicy(deny=["finish"]))]
    assert names == ["read_file", "write_file"]


def test_allow_none_exposes_everything():
    """allow=None 表示全部允许 —— 这是默认值，不能变成"一个都不给"。"""
    assert _registry().schemas(ToolPolicy()) != []


def test_deny_wins_over_allow_when_both_set():
    names = [s["name"] for s in _registry().schemas(
        ToolPolicy(allow=["read_file", "finish"], deny=["finish"]))]
    assert names == ["read_file"]


# ---- finish 工具 ----
def test_finish_schema_requires_summary():
    schema = FinishTool().schema()
    assert schema["name"] == "finish"
    assert "summary" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["summary"]


async def test_finish_returns_summary_as_content():
    r = await FinishTool().invoke(
        ToolCall("c1", "finish", {"summary": "fixed the bug"}), ws=None)  # type: ignore[arg-type]
    assert r.ok is True
    assert r.content == "fixed the bug"


async def test_finish_tolerates_missing_summary():
    """模型偶尔不给 summary —— 不能崩，那会把'agent 完成了'变成'run 出错'。"""
    r = await FinishTool().invoke(ToolCall("c1", "finish", {}), ws=None)  # type: ignore[arg-type]
    assert r.ok is True
