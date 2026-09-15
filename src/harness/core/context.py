"""上下文管理：消息累积、token 估算、压缩事件。

## M1 只实现前半部分

任务 10 需要 `build_request` / `append_assistant` / `append_tool_result`；
压缩逻辑在**任务 22** 补齐。因此 `needs_compaction()` 与 `compact()` 现在
**接口存在但固定返回 False / None** —— 这样任务 22 只需替换实现，不用改 loop。

## Token 估算的双轨制

provider 上报的 usage 是**权威值**，落在 `LLMResponseEvent` 上用于记账；
这里的本地估算只用于**触发压缩决策**的一路参考。两者都不写进对方。

`tiktoken` 对 DeepSeek / 通义无效（编码不同，偏差可超 30%），
所以这里用字符数启发式 —— 它至少是**确定性**的，不会让金轨迹与重放漂移。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from harness.contracts.protocols import LLMResponse, Message, ToolResult
from harness.events.types import ContextCompactEvent

# 经验值：英文约 4 字符/token，中文约 1.5 字符/token
_CHARS_PER_TOKEN_ASCII = 4.0
_CHARS_PER_TOKEN_CJK = 1.5

_CJK_START = "一"
_CJK_END = "鿿"


def estimate_tokens(text: str) -> int:
    """字符数启发式。

    **必须确定性** —— 同样的输入给同样的输出。否则 record/replay 的
    字节级一致性断言会假失败。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _CJK_START <= ch <= _CJK_END)
    ascii_chars = len(text) - cjk
    return int(cjk / _CHARS_PER_TOKEN_CJK + ascii_chars / _CHARS_PER_TOKEN_ASCII) + 1


@dataclass(slots=True)
class BuiltRequest:
    """构造好的请求 + 一个摘要。

    `digest` 供 `LLMRequestEvent` 记录"两次请求是否相同"，
    刻意**不存完整 messages** —— 那是 provider 的 `raw` 与轨迹的职责。
    """

    messages: list[Message]
    digest: str


@dataclass(slots=True)
class ContextManager:
    system_prompt: str
    token_budget: int = 100_000
    _history: list[Message] = field(default_factory=list, repr=False)

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

    # ---- 以下两个方法在任务 22 实现 ----
    def needs_compaction(self) -> bool:
        """M1 固定返回 False —— 接口先立住，实现后补。"""
        return False

    def compact(self) -> ContextCompactEvent | None:
        """M1 固定返回 None。任务 22 会返回真实事件并丢弃最旧的消息。"""
        return None
