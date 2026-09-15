"""事件模型：全系统的 L0 契约，schema 稳定性的第一道防线。

## 三条设计约束

1. **每种 EventType 一个扁平子类，不套 `payload` 一层。**
   评测器拿到就是 `ToolResultEvent`，直接 `.content` / `.ok`，
   无需 isinstance 收窄和二次解包。代价是子类字段多，但它们是 schema 的唯一真相源。

2. **`extra="forbid"` + `attrs` 逃生舱。**
   评测器想加临时派生字段走 `attrs`；想加正式字段必须改本文件并 bump 版本，
   由 golden JSONL 回归与字段快照测试卡住。这条规约防止 schema 被评测器倒逼改动。

3. **`frozen=True`。**
   事件不可变 → 并发读取无需加锁，且可直接作为 dict key。

## 为什么 CONTEXT_COMPACT 与 POLICY_DENY 是一等事件

它们在别处通常只写日志，但恰是过程级评测最有价值的信号 ——
上下文被压缩后丢失关键信息、越权被拦截后的降级行为，都是真实的失败模式。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

EVENT_SCHEMA_VERSION = 1


class EventType(StrEnum):
    RUN_START = "run.start"
    TURN_START = "turn.start"
    LLM_REQUEST = "llm.request"
    LLM_RESPONSE = "llm.response"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    CONTEXT_COMPACT = "context.compact"
    BUDGET_EVENT = "budget.event"
    POLICY_DENY = "policy.deny"
    ERROR = "error"
    RUN_END = "run.end"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Event(BaseModel):
    """所有事件的基类。字段刻意保持精简 —— 通用字段在此，类型特有字段在子类。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    seq: int
    ts: datetime = Field(default_factory=_utcnow)
    turn: int | None = None
    # 因果性靠 span，不靠 seq 相邻性：并发工具调用的 seq 会交错，
    # 但同一 turn 的所有 tool span 共享 parent_span_id。
    span_id: str | None = None
    parent_span_id: str | None = None
    # 逃生舱：评测器的临时派生信息放这里，不碰 schema
    attrs: dict[str, Any] = Field(default_factory=dict)


class RunStartEvent(Event):
    type: Literal[EventType.RUN_START] = EventType.RUN_START
    role: str
    task: str | None = None
    model: str
    provider: str
    tools: list[str] = Field(default_factory=list)
    # 完整 RunSpec 快照 —— 评测器无需回查 suite 配置即可理解本次 run 的上下文
    spec_json: str = "{}"
    schema_version: int = EVENT_SCHEMA_VERSION


class TurnStartEvent(Event):
    type: Literal[EventType.TURN_START] = EventType.TURN_START


class LLMRequestEvent(Event):
    type: Literal[EventType.LLM_REQUEST] = EventType.LLM_REQUEST
    model: str
    # 不存完整 messages —— 那是 LLMResponseEvent.raw 与 Profile 的职责。
    # 这里只存足以判断"两次请求是否相同"的摘要。
    messages_digest: str = ""
    message_count: int = 0
    context_tokens_est: int = 0
    tools_offered: list[str] = Field(default_factory=list)


class LLMResponseEvent(Event):
    type: Literal[EventType.LLM_RESPONSE] = EventType.LLM_RESPONSE
    model: str
    content: list[dict[str, Any]] = Field(default_factory=list)
    text: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    # ★ provider 原始响应 —— replay 无损性的唯一保证。
    # 回放 raw 而非归一化结果，否则跨厂商无法无损重放。
    raw: dict[str, Any] = Field(default_factory=dict)


class ToolCallEvent(Event):
    type: Literal[EventType.TOOL_CALL] = EventType.TOOL_CALL
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(Event):
    type: Literal[EventType.TOOL_RESULT] = EventType.TOOL_RESULT
    call_id: str
    name: str
    ok: bool
    # ★ 工具结果原文。GroundingChecker 靠它与后续 assistant 消息比对，
    # 检测"幻觉工具输出"。绝不要在这里做结构化解析 —— 忠实保留原文。
    content: str = ""
    error: str | None = None
    error_type: str | None = None
    duration_ms: int = 0
    truncated: bool = False
    # 被哪个中间件拦下（permission / sandbox / policy / budget）。
    # FailureClassifier 依赖它区分不同来源的失败。
    denied_by: str | None = None


class ContextCompactEvent(Event):
    type: Literal[EventType.CONTEXT_COMPACT] = EventType.CONTEXT_COMPACT
    reason: Literal["token_pressure", "turn_limit", "manual"] = "token_pressure"
    messages_before: int = 0
    messages_after: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    dropped_message_digests: list[str] = Field(default_factory=list)
    strategy: str = ""


class BudgetEvent(Event):
    type: Literal[EventType.BUDGET_EVENT] = EventType.BUDGET_EVENT
    dimension: Literal[
        "turns", "tool_calls", "input_tokens", "output_tokens", "usd", "wall_clock"
    ]
    limit: float = 0.0
    consumed: float = 0.0
    threshold: float | None = None
    action: Literal["warn", "deny", "terminate"] = "warn"


class PolicyDenyEvent(Event):
    type: Literal[EventType.POLICY_DENY] = EventType.POLICY_DENY
    policy: str
    subject: str
    reason: str = ""
    call_id: str | None = None
    severity: Literal["soft", "hard"] = "hard"


class ErrorEvent(Event):
    type: Literal[EventType.ERROR] = EventType.ERROR
    where: Literal["llm", "tool", "sandbox", "store", "provider", "loop"] = "loop"
    error_type: str
    message: str = ""
    retryable: bool = False
    traceback: str | None = None


class RunEndEvent(Event):
    type: Literal[EventType.RUN_END] = EventType.RUN_END
    status: str
    final_output: str | None = None
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0


EventUnion = Annotated[
    Union[
        RunStartEvent,
        TurnStartEvent,
        LLMRequestEvent,
        LLMResponseEvent,
        ToolCallEvent,
        ToolResultEvent,
        ContextCompactEvent,
        BudgetEvent,
        PolicyDenyEvent,
        ErrorEvent,
        RunEndEvent,
    ],
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(EventUnion)


def parse_event(data: dict[str, Any]) -> Any:
    """从 dict 解析事件。未知 type 会抛 ValidationError（判别联合的收益）。"""
    return _ADAPTER.validate_python(data)


def dump_event(ev: Any) -> dict[str, Any]:
    """序列化为可 JSON 化的 dict。"""
    return _ADAPTER.dump_python(ev, mode="json")
