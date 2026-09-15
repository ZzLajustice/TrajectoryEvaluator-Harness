"""上下文管理：消息累积、token 估算、压缩。

## 我们不追求压缩质量，只要求压缩**安全且可观测**

`CONTEXT_COMPACT` 是一等事件 —— 压缩后 agent 丢失关键信息是真实失败模式
（MAST FM-1.4）。所以压缩本身可以很简单，但**必须留下记录**。

## 两条正确性底线

1. **任务提示词绝不丢弃。**
   丢了它 agent 就忘了自己在干什么。症状是"模型变笨了"而非报错 —— 极难归因。

2. **压缩后不得出现悬空的 tool_use / tool_result 配对。**
   OpenAI 兼容端点会直接**报 400** 让整个 run 崩掉；而如果只丢结果保留调用，
   错误信息完全指不到真正的元凶。

   这条决定了压缩必须**按整组丢**：一个 assistant 消息 + 它引发的所有 tool 结果，
   要么全留要么全丢。

## Token 估算的双轨制

provider 上报的 usage 是**权威值**，落在 `LLMResponseEvent` 上用于记账；
这里的本地估算只用于**触发压缩决策**。两者都不写进对方。

`tiktoken` 对 DeepSeek / 通义无效（编码不同，偏差可超 30%），
所以这里用字符数启发式 —— 它至少**确定性**，不会让金轨迹与重放漂移。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from harness.contracts.protocols import LLMResponse, Message, ToolResult
from harness.events.types import ContextCompactEvent

_CHARS_PER_TOKEN_ASCII = 4.0
_CHARS_PER_TOKEN_CJK = 1.5
_CJK_START = "一"
_CJK_END = "鿿"

# 压缩时保留下来的尾部消息数下限 —— 当前正在做的事不能被丢掉
_KEEP_TAIL = 4

_STRATEGY = "drop_oldest_groups"


def estimate_tokens(text: str) -> int:
    """字符数启发式。**必须确定性** —— 同样输入给同样输出。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _CJK_START <= ch <= _CJK_END)
    ascii_chars = len(text) - cjk
    return int(cjk / _CHARS_PER_TOKEN_CJK + ascii_chars / _CHARS_PER_TOKEN_ASCII) + 1


def _msg_text(m: Message) -> str:
    return "".join(
        str(b.get("text") or b.get("content") or "") for b in m.content
    )


def _digest(m: Message) -> str:
    return hashlib.sha256(_msg_text(m).encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class BuiltRequest:
    """构造好的请求 + 摘要。

    `digest` 供 `LLMRequestEvent` 记录"两次请求是否相同"，
    刻意**不存完整 messages** —— 那是 provider `raw` 与轨迹的职责。
    """

    messages: list[Message]
    digest: str


@dataclass(slots=True)
class ContextManager:
    system_prompt: str
    token_budget: int = 100_000
    task: str | None = None
    _history: list[Message] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        # 任务作为第一条 user 消息进入历史，并在整个生命周期内不被丢弃
        if self.task:
            self._history.append(Message.user_text(self.task))

    # ---- 累积 ----
    def append_assistant(self, resp: LLMResponse) -> None:
        self._history.append(Message(role="assistant", content=list(resp.content)))

    def append_tool_result(self, result: ToolResult) -> None:
        self._history.append(
            Message.tool_result(
                result.call_id,
                result.content if result.ok else (result.error or ""),
                ok=result.ok,
            )
        )

    # ---- 构造请求 ----
    def build_request(self, turn: int) -> BuiltRequest:
        messages = [Message.system_text(self.system_prompt), *self._history]
        blob = f"{turn}|{len(messages)}|{self.estimated_tokens()}"
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
        return BuiltRequest(messages=messages, digest=digest)

    def estimated_tokens(self) -> int:
        total = estimate_tokens(self.system_prompt)
        for m in self._history:
            for block in m.content:
                total += estimate_tokens(
                    str(block.get("text") or block.get("content") or "")
                )
        return total

    # ---- 压缩 ----
    def needs_compaction(self) -> bool:
        return self.estimated_tokens() > self.token_budget

    def compact(self) -> ContextCompactEvent | None:
        """丢弃最旧的**整组**消息，直到回到预算内。

        返回的事件 `run_id` / `seq` 是占位值 —— 由 `RunContext` 在发出前填充。
        这样 ContextManager 不需要知道自己在哪个 run 里。
        """
        if not self.needs_compaction():
            return None

        tokens_before = self.estimated_tokens()
        messages_before = len(self._history)

        # 第一条是任务描述，永不丢弃
        protected = 1 if self.task else 0
        groups = self._group_from(self._history, protected)

        dropped: list[str] = []
        keep = list(self._history[:protected])
        kept_groups = list(groups)

        # 第一阶段：丢最旧的组，但保 `_KEEP_TAIL` 个 —— 近期上下文优先保留
        while len(kept_groups) > _KEEP_TAIL and self._would_exceed(keep, kept_groups):
            dropped.extend(_digest(m) for m in kept_groups.pop(0))

        # 第二阶段：尾部下限也保不住时继续丢，直到只剩 1 组。
        # 取舍：**超出预算是比丢失近期上下文更糟的情况** ——
        # 前者会让下一次调用直接撞上下文上限而失败，后者只是模型少看到一点历史。
        # 而且若在此停止，`needs_compaction()` 会一直为真，loop 每轮空转。
        while len(kept_groups) > 1 and self._would_exceed(keep, kept_groups):
            dropped.extend(_digest(m) for m in kept_groups.pop(0))

        for group in kept_groups:
            keep.extend(group)

        self._history = keep
        return ContextCompactEvent(
            run_id="", seq=0, type="context.compact",  # type: ignore[arg-type]
            reason="token_pressure",
            messages_before=messages_before,
            messages_after=len(self._history),
            tokens_before=tokens_before,
            tokens_after=self.estimated_tokens(),
            dropped_message_digests=dropped,
            strategy=_STRATEGY,
        )

    def _would_exceed(self, kept: list[Message], groups: list[list[Message]]) -> bool:
        total = estimate_tokens(self.system_prompt)
        for m in kept:
            total += sum(estimate_tokens(str(b.get("text") or b.get("content") or ""))
                         for b in m.content)
        for g in groups:
            for m in g:
                total += sum(estimate_tokens(str(b.get("text") or b.get("content") or ""))
                             for b in m.content)
        return total > self.token_budget

    @staticmethod
    def _group_from(history: list[Message], start: int) -> list[list[Message]]:
        """把历史切成「一个非 tool 消息 + 紧随其后的所有 tool 结果」的组。

        **按整组丢是硬要求** —— 拆开会产生悬空的 tool_use / tool_result 配对，
        下一次 API 调用直接 400，且错误信息指不到元凶。
        """
        groups: list[list[Message]] = []
        for m in history[start:]:
            if m.role == "tool" and groups:
                groups[-1].append(m)
            else:
                groups.append([m])
        return groups
