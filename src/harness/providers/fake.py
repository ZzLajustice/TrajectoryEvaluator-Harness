"""脚本化 provider：无网络、确定性。

**这是 M1 端到端路径能完全离线的关键。** 没有它，垂直切片就得依赖真实 API，
测试会变慢、变贵、不确定 —— 而 M1 的价值恰恰是"最早跑通一条可运行路径"。

两级用法：
  - 传序列：按顺序返回，用尽后抛 `IndexError`（让"多调了一次"立刻暴露）
  - 传 callable：按请求分支，用于测试失败重试路径

`requests` 记录所有收到的请求，供上层断言 payload。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

from harness.contracts.protocols import (
    LLMRequest,
    LLMResponse,
    ToolCall,
    Usage,
)

_FAKE_USAGE = Usage(input_tokens=10, output_tokens=5, calls=1)


def text_response(text: str, model: str = "fake") -> LLMResponse:
    """纯文本响应 —— `finish_reason="stop"`，无工具调用。

    注意：无 tool_calls 会让 agent loop 终止为 `NO_FINISH`（agent 停止行动了但没说完成）。
    """
    return LLMResponse(
        model=model,
        content=[{"type": "text", "text": text}],
        text=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_FAKE_USAGE,
        latency_ms=0,
    )


def tool_call_response(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "call_1",
    model: str = "fake",
    text: str = "",
) -> LLMResponse:
    """工具调用响应 —— `finish_reason="tool_calls"`，agent loop 会继续。"""
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    content.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
    return LLMResponse(
        model=model,
        content=content,
        text=text,
        tool_calls=[ToolCall(call_id=call_id, name=name, arguments=arguments)],
        finish_reason="tool_calls",
        usage=_FAKE_USAGE,
        latency_ms=0,
    )


class FakeProvider:
    """脚本化 provider。

    `script` 为序列时用尽后抛 `IndexError` —— 刻意不静默重复最后一条响应，
    因为"agent 多调了一次模型"是真实的 bug 信号，应当立刻暴露。
    """

    name = "fake"

    def __init__(
        self, script: Sequence[LLMResponse] | Callable[[LLMRequest], LLMResponse]
    ) -> None:
        # if/else 而非布尔标志：类型检查器需要 `callable()` 直接守卫分支才能收窄联合类型
        self._fn: Callable[[LLMRequest], LLMResponse] | None
        self._it: Iterator[LLMResponse] | None
        if callable(script):
            self._fn, self._it = script, None
        else:
            self._fn, self._it = None, iter(script)
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if self._fn is not None:
            return self._fn(req)
        assert self._it is not None
        try:
            return next(self._it)
        except StopIteration as exc:
            raise IndexError(
                f"fake provider script exhausted after {len(self.requests)} calls"
            ) from exc

    async def aclose(self) -> None:
        return None
