"""OpenAI 兼容 provider。

覆盖 OpenAI / DeepSeek / 通义 / Moonshot / Groq / vLLM / Ollama / OpenRouter ——
凡是提供 OpenAI 兼容端点的厂商，改 `base_url` 即可。

## 三条设计决策

1. **走官方 `openai` SDK，不是裸 httpx2、更不是 litellm。**
   - SDK 覆盖所有 OpenAI 兼容端点，且 `http_client=` 是唯一 transport 注入口
     → 单测注入 `MockTransport` 就能完全离线地断言 payload
   - **不用 litellm**：它会抹平厂商差异，而评测 harness 需要精确复现 payload。
     inspect_ai 的 `deepseek.py` 注释说明了为什么必须逐家处理：
     每家都有 "不支持强制工具调用"、"结构化输出降级为 JSON mode" 这类 quirk。

2. **归一化方向是「富 → 简」。**
   内部用 Anthropic 风格 content blocks（`text` / `tool_use` / `tool_result`），
   因为它表达能力更强；映射到 OpenAI chat 是**可预测的有损**。
   反过来（简 → 富）会凭空发明结构。

3. **`raw` 不是字节级原文。**
   它是 `completion.model_dump()`，即 SDK 解析后的结构化副本，用于审计与调试。
   **重放基于 `LLMResponse` 的序列化**，不依赖 `raw` 的字节保真 ——
   这一点在设计文档里曾被过度声称，实现时按事实修正。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx2
import openai

from harness.contracts.protocols import LLMRequest, LLMResponse, Message, ToolCall
from harness.providers.base import usage_from_openai


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """工具参数解析。**坏 JSON 降级为空 dict 而非崩溃。**

    模型偶尔会返回不合法 JSON；崩掉会让一次可诊断的失败变成一次 run 崩溃。
    降级后 `arguments` 为空，上层（工具层 / 评测器）能看到"参数是空的"这个事实。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _messages_to_payload(messages: list[Message]) -> list[dict[str, Any]]:
    """归一化内部 content blocks → OpenAI chat 格式。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            block = m.content[0] if m.content else {}
            out.append({
                "role": "tool",
                "tool_call_id": block.get("tool_call_id", ""),
                "content": block.get("content", ""),
            })
            continue

        text = "".join(b.get("text", "") for b in m.content if b.get("type") == "text")
        tool_uses = [b for b in m.content if b.get("type") == "tool_use"]

        entry: dict[str, Any] = {"role": m.role, "content": text}
        if tool_uses:
            entry["tool_calls"] = [
                {
                    "id": b.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                    },
                }
                for b in tool_uses
            ]
        out.append(entry)
    return out


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        client: httpx2.AsyncClient | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self._client = client or httpx2.AsyncClient(timeout=timeout_s)
        self._sdk = openai.AsyncOpenAI(
            api_key=api_key, base_url=base_url, http_client=self._client
        )

    async def complete(self, req: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": _messages_to_payload(req.messages),
        }
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if req.max_output_tokens is not None:
            kwargs["max_tokens"] = req.max_output_tokens
        if req.tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in req.tools]
            kwargs["tool_choice"] = req.tool_choice

        completion = await self._sdk.chat.completions.create(**kwargs)
        return self._from_payload(
            completion.model_dump(), latency_ms=int((time.monotonic() - started) * 1000)
        )

    @staticmethod
    def _from_payload(raw: dict[str, Any], *, latency_ms: int) -> LLMResponse:
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""

        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})

        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            # 缺 id 时兜底造一个 —— 下游靠 call_id 做 TOOL_CALL/RESULT 配对，
            # 空 id 会让配对全部失效
            call_id = tc.get("id") or f"call_{i}"
            args = _parse_arguments(fn.get("arguments"))
            content.append({
                "type": "tool_use", "id": call_id,
                "name": fn.get("name", ""), "input": args,
            })
            tool_calls.append(ToolCall(call_id=call_id, name=fn.get("name", ""),
                                       arguments=args))

        return LLMResponse(
            model=raw.get("model") or "",
            content=content,
            text=text,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage=usage_from_openai(raw.get("usage")),
            latency_ms=latency_ms,
            raw=raw,
        )

    async def aclose(self) -> None:
        await self._sdk.close()
