"""录制/回放装饰器。

## 为什么用装饰器而不是在 provider 内部加开关

装饰器模式让录制与厂商**解耦** —— `RecordingProvider(FakeProvider(...))`、
`RecordingProvider(OpenAICompatProvider(...))` 都成立，provider 实现不用知道
录制功能存在。这比在每个 provider 里塞 `if recording: ...` 干净得多。

## 回放的分级行为

`ReplayProvider` 默认在**未录制时抛 KeyError**，而不是返回空响应。
这是刻意的保守选择：静默的降级会让评测结果无声地错掉，而抛错至少能定位。

需要"回放优先、缺失时打真实 API"时，显式传 `fallback=`。
"""
from __future__ import annotations

from typing import Any

from harness.contracts.protocols import LLMRequest, LLMResponse, ToolCall
from harness.contracts.results import Usage
from harness.providers.response_pool import ResponsePool


def _serialize(resp: LLMResponse) -> dict[str, Any]:
    return {
        "model": resp.model,
        "content": resp.content,
        "text": resp.text,
        "tool_calls": [
            {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
            for c in resp.tool_calls
        ],
        "finish_reason": resp.finish_reason,
        # Usage 是 pydantic 模型而非 dataclass —— 用 model_dump 而非 dataclasses.asdict
        "usage": resp.usage.model_dump(),
        "latency_ms": resp.latency_ms,
        "raw": resp.raw,
    }


def _deserialize(data: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        model=data["model"],
        content=data["content"],
        text=data["text"],
        tool_calls=[ToolCall(**c) for c in data["tool_calls"]],
        finish_reason=data["finish_reason"],
        usage=Usage(**data["usage"]),
        latency_ms=data.get("latency_ms", 0),
        raw=data.get("raw", {}),
    )


class RecordingProvider:
    name = "recording"

    def __init__(self, inner: Any, pool: ResponsePool) -> None:
        self._inner = inner
        self._pool = pool

    async def complete(self, req: LLMRequest) -> LLMResponse:
        resp = await self._inner.complete(req)
        self._pool.record(req, _serialize(resp))
        return resp

    def save(self) -> None:
        self._pool.save()

    async def aclose(self) -> None:
        await self._inner.aclose()


class ReplayProvider:
    name = "replay"

    def __init__(self, pool: ResponsePool, *, fallback: Any | None = None) -> None:
        self._pool = pool
        self._fallback = fallback
        # 游标按 key 分别推进 —— 不同请求的样本序列互不干扰
        self._occurrence: dict[str, int] = {}

    async def complete(self, req: LLMRequest) -> LLMResponse:
        key = ResponsePool.key_of(req, req.model)
        try:
            data = self._pool.replay(req, occurrence=self._occurrence.get(key, 0))
        except KeyError:
            if self._fallback is None:
                raise
            return await self._fallback.complete(req)  # type: ignore[no-any-return]
        self._occurrence[key] = self._occurrence.get(key, 0) + 1
        return _deserialize(data)

    async def aclose(self) -> None:
        return None
