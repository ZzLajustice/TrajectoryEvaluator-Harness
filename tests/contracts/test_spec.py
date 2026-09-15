"""RunSpec 及其组成部件的测试。

核心约束：**RunSpec 必须能完整 JSON 序列化** —— 它会被整个塞进
`RunStartEvent.spec_json`，让评测器无需回查 suite 配置就能理解一次 run 的上下文。
这条约束传导到 `MiddlewareSpec`（只存名字+配置，不存实例）。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from harness.contracts.spec import (
    Budget,
    MiddlewareSpec,
    ModelRef,
    RunRole,
    RunSpec,
    RunStatus,
    TaskSpec,
    ToolPolicy,
    WorkspaceSpec,
)


def _spec(**kw: object) -> RunSpec:
    base: dict[str, object] = {
        "role": RunRole.SUT,
        "system_prompt": "you are a coder",
        "model": ModelRef(provider="openai_compat", model="deepseek-chat"),
    }
    return RunSpec(**{**base, **kw})  # type: ignore[arg-type]


def test_runspec_is_fully_json_serializable():
    spec = _spec(
        task=TaskSpec(case_id="c1", prompt="fix the bug"),
        budget=Budget(max_turns=10),
        workspace=WorkspaceSpec(kind="copy", source="examples/toyrepo"),
    )
    dumped = json.loads(spec.model_dump_json())
    assert dumped["role"] == "sut"
    assert dumped["budget"]["max_turns"] == 10
    assert dumped["middlewares"] == []
    assert dumped["workspace"]["source"] == "examples/toyrepo"


def test_middleware_spec_holds_name_and_config_not_instances():
    """RunSpec 可序列化的前提 —— MiddlewareSpec 只存名字+配置。"""
    mw = MiddlewareSpec(name="permission", config={"rules": []})
    assert json.loads(mw.model_dump_json()) == {
        "name": "permission",
        "enabled": True,
        "config": {"rules": []},
    }


def test_fingerprint_is_stable():
    assert _spec().fingerprint() == _spec().fingerprint()


def test_fingerprint_changes_on_behavioral_field():
    assert _spec(system_prompt="a").fingerprint() != _spec(system_prompt="b").fingerprint()
    a = _spec(model=ModelRef(provider="fake", model="m1"))
    b = _spec(model=ModelRef(provider="fake", model="m2"))
    assert a.fingerprint() != b.fingerprint()


def test_fingerprint_ignores_metadata():
    """元数据不影响行为，不该进指纹 —— 否则无关改动会让 run 判定为不可比。"""
    assert _spec(metadata={"x": 1}).fingerprint() == _spec(metadata={"x": 2}).fingerprint()


def test_fingerprint_ignores_workspace_keep_flags():
    """调试开关不改变 agent 行为，因此不进指纹。"""
    a = _spec(workspace=WorkspaceSpec(kind="copy", keep=False, keep_on_failure=False))
    b = _spec(workspace=WorkspaceSpec(kind="copy", keep=True, keep_on_failure=True))
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_changes_on_workspace_source():
    a = _spec(workspace=WorkspaceSpec(kind="copy", source="repo-a"))
    b = _spec(workspace=WorkspaceSpec(kind="copy", source="repo-b"))
    assert a.fingerprint() != b.fingerprint()


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        ModelRef(provider="fake", model="m", bogus=1)  # type: ignore[call-arg]


def test_budget_exceeded_is_distinct_from_llm_error():
    """预算耗尽与模型报错是完全不同的失败模式，FailureClassifier 依赖此区分。"""
    assert RunStatus.BUDGET_EXCEEDED != RunStatus.LLM_ERROR
    assert RunStatus.BUDGET_EXCEEDED.value == "budget_exceeded"


def test_max_turns_has_a_single_source_of_truth():
    """轮次上限只存在于 Budget —— RunSpec 刻意不设 max_turns。

    两个字段都能配、语义重叠、实现读哪个不明确，是真实踩过的坑。
    """
    assert not hasattr(_spec(), "max_turns")
    assert _spec(budget=Budget(max_turns=7)).budget.max_turns == 7


def test_tool_policy_allow_none_means_all_permitted():
    assert ToolPolicy().allow is None
    assert ToolPolicy(allow=["read_file"]).allow == ["read_file"]


def test_run_role_covers_the_four_roles():
    assert {r.value for r in RunRole} == {"sut", "judge", "classifier", "external"}
