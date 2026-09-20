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

from harness.events.reasoning import Reasoning, reasoning_of
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
    dump_event,
    parse_event,
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

    def reasoning(self) -> tuple[Reasoning, ...]:
        """模型每一轮的推理，按 `seq` 升序；没有推理的轮次不在其中。

        ## 为什么它值得一个访问器

        推理是**唯一**记录"模型当时在想什么"的地方，而它默认谁都看不到：
        `LLMResponseEvent.text` 常常是空的（实测 17 条真轨迹里，
        有些轮次 `content` 为空字符串而推理有一千多字）——
        也就是说，只看 `text` 的话，那些轮次里模型的全部努力不可见。

        体量上它也不是边角料：实测推理 79,306 字符 vs 可见输出 7,336 字符
        （**10.8x**），provider 上报的 reasoning tokens 占 completion tokens 的 **34.5%**。

        ## 它是**证据**，不是一个分数

        刻意不在这里下任何判断（不评分、不判定"想得好不好"）——
        理由是实测的：拿这份数据测过三种"推理质量"的代理信号
        （关键词频率、与工具输出的 token 重合度、声称要读的文件 vs 实际读的文件），
        **三种与结果都没有相关性**（详见 `docs/known-gaps.md` §2.6）。
        没有信号就不该造指标 —— 那种指标看起来有意义，实际是噪音。

        返回 `tuple`：与其余访问器一致，评测器改不了轨迹。
        """
        found = []
        for event in self.llm_responses():
            got = reasoning_of(event)
            if got is not None:
                found.append(got)
        return tuple(found)

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
