"""OpenAICompatProvider 测试。

## 全部离线

用 `httpx2.MockTransport` 替换传输层，因此这些测试**不产生任何网络流量**，
却能断言"实际发出的 payload 长什么样"。这是选择官方 SDK + 注入
`http_client` 而非裸 httpx 的直接收益。

## 一个诚实的说明

`raw` 是 **SDK 解析后的结构化副本**（`completion.model_dump()`），
不是字节级原文。它用于审计与调试；**重放基于 `LLMResponse` 的序列化**，
不依赖 `raw` 的字节保真。测试按这个事实断言。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from harness.contracts.protocols import LLMRequest, Message
from harness.providers.openai_compat import OpenAICompatProvider, _parse_arguments

FIXTURES = Path(__file__).parent.parent / "fixtures" / "provider"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _provider(name: str, captured: list | None = None, **kw) -> OpenAICompatProvider:
    body = _fixture(name)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if captured is not None:
            captured.append(json.loads(request.content))
        return httpx2.Response(200, json=body)

    return OpenAICompatProvider(
        api_key="k", base_url="https://example.invalid/v1",
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), **kw)


def _req(**kw) -> LLMRequest:
    base = {"model": "deepseek-chat", "messages": [Message.user_text("hi")]}
    return LLMRequest(**{**base, **kw})


# ---- 归一化 ----
async def test_text_response_is_normalized():
    r = await _provider("text_response.json").complete(_req())
    assert r.text == "hello"
    assert r.finish_reason == "stop"
    assert r.content[0] == {"type": "text", "text": "hello"}


async def test_tool_calls_are_normalized_to_call_id_and_arguments():
    r = await _provider("tool_call_response.json").complete(_req())
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].call_id == "call_abc"
    assert r.tool_calls[0].name == "read_file"
    assert r.tool_calls[0].arguments == {"path": "a.py"}


async def test_parallel_tool_calls_are_all_preserved():
    """并行工具调用不能被只取第一个 —— 那会静默丢失 agent 的意图。"""
    r = await _provider("parallel_tool_calls.json").complete(_req())
    assert [c.name for c in r.tool_calls] == ["read_file", "list_dir"]
    assert [c.call_id for c in r.tool_calls] == ["c1", "c2"]


async def test_tool_use_blocks_are_appended_to_content():
    r = await _provider("tool_call_response.json").complete(_req())
    assert any(b["type"] == "tool_use" for b in r.content)


async def test_malformed_arguments_degrade_to_empty_dict_not_crash():
    """模型偶尔返回坏 JSON —— 降级而非崩溃，把问题留给上层判定。"""
    r = await _provider("malformed_arguments.json").complete(_req())
    assert r.tool_calls[0].arguments == {}
    assert r.tool_calls[0].name == "f"


def test_parse_arguments_handles_all_the_shapes():
    assert _parse_arguments(None) == {}
    assert _parse_arguments("") == {}
    assert _parse_arguments("{not json") == {}
    assert _parse_arguments("[1, 2]") == {}          # 合法 JSON 但不是对象
    assert _parse_arguments('{"a": 1}') == {"a": 1}


# ---- usage 与 finish_reason ----
async def test_usage_is_mapped_from_openai_field_names():
    r = await _provider("text_response.json").complete(_req())
    assert r.usage.input_tokens == 12
    assert r.usage.output_tokens == 3
    assert r.usage.calls == 1


async def test_missing_usage_degrades_to_zeros():
    """厂商兼容层各有偏差，缺 usage 是常态 —— 不能崩。"""
    r = await _provider("no_usage.json").complete(_req())
    assert r.usage.input_tokens == 0
    assert r.usage.output_tokens == 0


async def test_finish_reason_length_is_preserved():
    """截断是重要信号 —— 抹平成 stop 会让上层误判为正常结束。"""
    r = await _provider("length_truncated.json").complete(_req())
    assert r.finish_reason == "length"


async def test_empty_content_yields_empty_text_without_crashing():
    r = await _provider("empty_content.json").complete(_req())
    assert r.text == ""
    assert r.tool_calls == []


# ---- raw ----
async def test_raw_is_populated_and_json_serializable():
    """Raw 用于审计 —— 必须能落进事件流。"""
    r = await _provider("text_response.json").complete(_req())
    assert r.raw.get("id") == "chatcmpl-1"
    assert r.raw.get("model") == "deepseek-chat"
    json.dumps(r.raw, default=str)


async def test_latency_is_recorded():
    r = await _provider("text_response.json").complete(_req())
    assert r.latency_ms >= 0


# ---- 请求侧 ----
async def test_tools_are_sent_in_openai_function_format():
    captured: list = []
    p = _provider("text_response.json", captured)
    tools = [{"name": "read_file", "description": "d",
              "parameters": {"type": "object", "properties": {}}}]
    await p.complete(_req(tools=tools))
    sent = captured[0]
    assert sent["tools"][0]["type"] == "function"
    assert sent["tools"][0]["function"]["name"] == "read_file"


async def test_no_tools_key_is_sent_when_none_offered():
    captured: list = []
    p = _provider("text_response.json", captured)
    await p.complete(_req())
    assert "tools" not in captured[0]


async def test_sampling_params_are_forwarded():
    captured: list = []
    p = _provider("text_response.json", captured)
    await p.complete(_req(temperature=0.3, max_output_tokens=128))
    assert captured[0]["temperature"] == pytest.approx(0.3)
    assert captured[0]["max_tokens"] == 128


async def test_model_name_is_forwarded():
    captured: list = []
    p = _provider("text_response.json", captured)
    await p.complete(_req(model="qwen-plus"))
    assert captured[0]["model"] == "qwen-plus"


async def test_system_message_is_preserved_in_payload():
    captured: list = []
    p = _provider("text_response.json", captured)
    await p.complete(LLMRequest(model="m", messages=[
        Message.system_text("be terse"), Message.user_text("hi")]))
    assert captured[0]["messages"][0] == {"role": "system", "content": "be terse"}


async def test_tool_result_message_is_mapped_to_tool_role():
    captured: list = []
    p = _provider("text_response.json", captured)
    await p.complete(LLMRequest(model="m", messages=[
        Message.tool_result("c1", "output", ok=True)]))
    sent = captured[0]["messages"][0]
    assert sent["role"] == "tool"
    assert sent["tool_call_id"] == "c1"
    assert sent["content"] == "output"


async def test_aclose_is_idempotent():
    p = _provider("text_response.json")
    await p.aclose()
    await p.aclose()


def test_provider_name_is_stable():
    assert _provider("text_response.json").name == "openai_compat"
