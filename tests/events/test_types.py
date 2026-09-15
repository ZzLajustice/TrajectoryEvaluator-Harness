"""事件模型测试。

重点验证三条 schema 稳定性约束：
  1. 往返序列化无损
  2. 不可变（frozen）
  3. 拒绝未知字段（extra="forbid"）
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from harness.events.types import (
    BudgetEvent,
    ContextCompactEvent,
    ErrorEvent,
    EventType,
    LLMRequestEvent,
    LLMResponseEvent,
    PolicyDenyEvent,
    RunEndEvent,
    RunStartEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnStartEvent,
    dump_event,
    parse_event,
)


def test_event_roundtrip_preserves_all_fields():
    ev = ToolResultEvent(
        run_id="r1",
        seq=3,
        type=EventType.TOOL_RESULT,
        call_id="c1",
        name="read_file",
        ok=True,
        content="def last(x): return x[len(x)]",
    )
    raw = dump_event(ev)
    assert parse_event(json.loads(json.dumps(raw))) == ev


def test_parse_rejects_unknown_event_type():
    with pytest.raises(ValidationError):
        parse_event({"type": "no.such.event", "run_id": "r", "seq": 0})


def test_event_is_frozen():
    ev = ToolCallEvent(
        run_id="r1", seq=0, type=EventType.TOOL_CALL,
        call_id="c1", name="read_file", arguments={"path": "a.py"},
    )
    with pytest.raises(ValidationError):
        ev.seq = 99  # type: ignore[misc]


def test_extra_field_is_rejected():
    """schema 漂移必须立刻报错 —— 临时字段走 attrs 逃生舱。"""
    with pytest.raises(ValidationError):
        ToolCallEvent(
            run_id="r1", seq=0, type=EventType.TOOL_CALL,
            call_id="c1", name="f", arguments={}, bogus_field=1,  # type: ignore[call-arg]
        )


def test_attrs_escape_hatch_accepts_arbitrary_keys():
    """逃生舱：评测器想存临时派生信息时走这里，不碰 schema。"""
    ev = ToolCallEvent(
        run_id="r1", seq=0, type=EventType.TOOL_CALL,
        call_id="c1", name="f", arguments={}, attrs={"custom": 1},
    )
    assert ev.attrs["custom"] == 1


def test_ts_defaults_to_utc_aware():
    """DTZ 规则要求时区安全 —— 时间戳必须带 tz。"""
    ev = TurnStartEvent(run_id="r1", seq=0, type=EventType.TURN_START)
    assert ev.ts.tzinfo is not None


def test_every_event_type_has_a_model():
    """EventType 枚举与判别联合必须完全对应，不能有遗漏。"""
    from harness.events import types as T

    models = {
        obj.model_fields["type"].default
        for obj in vars(T).values()
        if isinstance(obj, type) and hasattr(obj, "model_fields") and "type" in obj.model_fields
    }
    assert models == set(EventType)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RunStartEvent(run_id="r", seq=0, type=EventType.RUN_START,
                              role="sut", model="m", provider="fake"),
        lambda: TurnStartEvent(run_id="r", seq=0, type=EventType.TURN_START),
        lambda: LLMRequestEvent(run_id="r", seq=0, type=EventType.LLM_REQUEST, model="m"),
        lambda: LLMResponseEvent(run_id="r", seq=0, type=EventType.LLM_RESPONSE, model="m"),
        lambda: ToolCallEvent(run_id="r", seq=0, type=EventType.TOOL_CALL,
                              call_id="c", name="f"),
        lambda: ToolResultEvent(run_id="r", seq=0, type=EventType.TOOL_RESULT,
                                call_id="c", name="f", ok=True),
        lambda: ContextCompactEvent(run_id="r", seq=0, type=EventType.CONTEXT_COMPACT,
                                    reason="token_pressure", messages_before=10,
                                    messages_after=4, tokens_before=9000,
                                    tokens_after=3000, dropped_message_digests=[],
                                    strategy="drop_oldest_tool_results"),
        lambda: BudgetEvent(run_id="r", seq=0, type=EventType.BUDGET_EVENT,
                            dimension="turns", limit=10, consumed=8, action="warn"),
        lambda: PolicyDenyEvent(run_id="r", seq=0, type=EventType.POLICY_DENY,
                                policy="permission", subject="run_command", reason="denied"),
        lambda: ErrorEvent(run_id="r", seq=0, type=EventType.ERROR,
                           where="llm", error_type="TimeoutError", message="boom"),
        lambda: RunEndEvent(run_id="r", seq=0, type=EventType.RUN_END, status="ok"),
    ],
    ids=lambda f: f().type.value,
)
def test_all_events_survive_roundtrip(factory):
    ev = factory()
    assert parse_event(dump_event(ev)) == ev
