"""Trajectory —— 事件流的只读视图。

**这是评测器与被测 agent 之间唯一的交互面。** 它不含任何 I/O，
因此可以被 `evaluators/` 安全依赖而不违反分层规则（见
`docs/superpowers/specs/2026-09-14-agent-eval-harness-design.md` §2.2）。

## 两个刻意的设计选择

1. **访问器返回 `tuple` 而非 `list`。**
   评测器改不了轨迹 —— 这消除了一整类"评测器之间通过修改轨迹互相影响"的隐蔽 bug。

2. **索引在构造时一次建好。**
   `GroundingChecker` 要按 `call_id` 频繁查 `ToolResultEvent`，O(1) 很值。
   注意 `slots=True` 下实例没有 `__dict__`，**不能事后挂属性** ——
   索引必须声明为 dataclass 字段并由 `from_events` 经构造参数传入。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from harness.events.types import (
    ContextCompactEvent,
    EventType,
    EventUnion,
    LLMResponseEvent,
    PolicyDenyEvent,
    RunEndEvent,
    RunStartEvent,
    ToolCallEvent,
    ToolResultEvent,
    parse_event,
    dump_event,
)


@dataclass(slots=True)
class Trajectory:
    """事件流只读视图。不含任何 I/O，可被 evaluators 安全依赖。"""

    run_id: str
    events: tuple[EventUnion, ...]
    _by_seq: dict[int, EventUnion] = field(default_factory=dict, repr=False)
    _by_call_id: dict[str, ToolResultEvent] = field(default_factory=dict, repr=False)

    # ---- 构造 ----
    @classmethod
    def from_events(cls, run_id: str, events: Iterable[EventUnion]) -> Trajectory:
        """从事件序列构造。索引在这里一次建好（slots 下无法事后赋值）。"""
        evs = tuple(events)
        return cls(
            run_id=run_id,
            events=evs,
            _by_seq={e.seq: e for e in evs},
            _by_call_id={e.call_id: e for e in evs if isinstance(e, ToolResultEvent)},
        )

    @classmethod
    def from_jsonl(cls, text: str) -> Trajectory:
        """从 JSONL 文本构造。空行被忽略。"""
        events = [
            parse_event(json.loads(line))
            for line in text.splitlines()
            if line.strip()
        ]
        run_id = events[0].run_id if events else ""
        return cls.from_events(run_id, events)

    def to_jsonl(self) -> str:
        return "".join(
            json.dumps(dump_event(e), ensure_ascii=False) + "\n" for e in self.events
        )

    # ---- 通用访问 ----
    def of(self, *types: EventType) -> tuple[EventUnion, ...]:
        wanted = set(types)
        return tuple(e for e in self.events if e.type in wanted)

    def at(self, seq: int) -> EventUnion | None:
        return self._by_seq.get(seq)

    # ---- 类型化访问（评测器的主力 API）----
    def start(self) -> RunStartEvent | None:
        found = self.of(EventType.RUN_START)
        return found[0] if found else None  # type: ignore[return-value]

    def end(self) -> RunEndEvent | None:
        found = self.of(EventType.RUN_END)
        return found[-1] if found else None  # type: ignore[return-value]

    def llm_responses(self) -> tuple[LLMResponseEvent, ...]:
        return self.of(EventType.LLM_RESPONSE)  # type: ignore[return-value]

    def tool_calls(self) -> tuple[ToolCallEvent, ...]:
        return self.of(EventType.TOOL_CALL)  # type: ignore[return-value]

    def tool_results(self) -> tuple[ToolResultEvent, ...]:
        return self.of(EventType.TOOL_RESULT)  # type: ignore[return-value]

    def result_for(self, call_id: str) -> ToolResultEvent | None:
        """按 call_id 查工具结果。O(1) —— 走构造时建好的索引。"""
        return self._by_call_id.get(call_id)

    def compactions(self) -> tuple[ContextCompactEvent, ...]:
        return self.of(EventType.CONTEXT_COMPACT)  # type: ignore[return-value]

    def denials(self) -> tuple[PolicyDenyEvent, ...]:
        return self.of(EventType.POLICY_DENY)  # type: ignore[return-value]

    def tool_sequence(self) -> tuple[str, ...]:
        """仅工具名序列。TrajectoryMatcher 的输入。"""
        return tuple(c.name for c in self.tool_calls())

    # ---- 派生量（避免评测器各写一份）----
    @property
    def status(self) -> str:
        end = self.end()
        return end.status if end else "unknown"

    @property
    def final_output(self) -> str | None:
        end = self.end()
        return end.final_output if end else None

    @property
    def input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.llm_responses())

    @property
    def output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.llm_responses())

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.llm_responses())
