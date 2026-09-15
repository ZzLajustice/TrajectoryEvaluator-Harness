"""Provider 一致性套件。

## 这个套件要解决什么

不同 provider 的实现细节千差万别，但**对外契约必须一致** ——
否则 `Run` / 评测器就得为每家写一套分支。

这里把契约写成可执行的断言，并参数化到 (provider × 场景) 两个维度：

    provider  新增一家只需在 FACTORIES 里加一项，不用改任何断言
    场景      纯文本 / 单工具 / 并行工具 / 空内容 / 缺 usage / 坏 JSON

**二维参数化很重要**：只参数化 provider 的话，"工具调用有 id"这类断言
在纯文本场景下是空断言，检查不出来。

## 全部离线

OpenAI 兼容 provider 走 `httpx2.MockTransport`，FakeProvider 本来就不联网。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest

from harness.contracts.protocols import LLMRequest, LLMResponse, Message, ToolCall, Usage
from harness.providers.fake import FakeProvider, text_response, tool_call_response
from harness.providers.openai_compat import OpenAICompatProvider

FIXTURES = Path(__file__).parent.parent / "fixtures" / "provider"


# --------------------------------------------------------------------------
# provider 侧：给定一个响应体，产出一个 provider
# --------------------------------------------------------------------------
def _openai_serving(fixture: str) -> Callable[[], OpenAICompatProvider]:
    body = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))

    def build() -> OpenAICompatProvider:
        async def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=body)

        return OpenAICompatProvider(
            api_key="k", base_url="https://example.invalid/v1",
            client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))

    return build


def _fake_serving(responses: list[LLMResponse]) -> Callable[[], FakeProvider]:
    return lambda: FakeProvider(responses)


def _fake_parallel() -> LLMResponse:
    """FakeProvider 侧的并行工具调用 —— 与 parallel_tool_calls.json 语义等价。"""
    calls = [
        ToolCall(call_id="c1", name="read_file", arguments={"path": "a.py"}),
        ToolCall(call_id="c2", name="list_dir", arguments={"path": "."}),
    ]
    return LLMResponse(
        model="fake",
        content=[{"type": "tool_use", "id": c.call_id, "name": c.name,
                  "input": c.arguments} for c in calls],
        text="", tool_calls=calls, finish_reason="tool_calls",
        usage=Usage(input_tokens=20, output_tokens=12, calls=1), latency_ms=0)


def _fake_no_usage() -> LLMResponse:
    """缺 usage 的场景 —— 对应 no_usage.json。"""
    return LLMResponse(
        model="fake", content=[{"type": "text", "text": "hello"}], text="hello",
        tool_calls=[], finish_reason="stop", usage=Usage(), latency_ms=0)


# --------------------------------------------------------------------------
# 场景定义：每个场景给出「同一语义」的两种 provider 表达
# --------------------------------------------------------------------------
_SCENARIOS: dict[str, dict[str, Callable[[], object]]] = {
    "text": {
        "openai_compat": _openai_serving("text_response.json"),
        "fake": _fake_serving([text_response("hello")]),
    },
    "tool_call": {
        "openai_compat": _openai_serving("tool_call_response.json"),
        "fake": _fake_serving([tool_call_response("read_file", {"path": "a.py"},
                                                  call_id="call_abc")]),
    },
    "parallel_tools": {
        "openai_compat": _openai_serving("parallel_tool_calls.json"),
        "fake": _fake_serving([_fake_parallel()]),
    },
    "empty_content": {
        "openai_compat": _openai_serving("empty_content.json"),
        "fake": _fake_serving([text_response("")]),
    },
    "no_usage": {
        "openai_compat": _openai_serving("no_usage.json"),
        "fake": _fake_serving([_fake_no_usage()]),
    },
    "malformed_arguments": {
        "openai_compat": _openai_serving("malformed_arguments.json"),
        "fake": _fake_serving([tool_call_response("f", {}, call_id="c1")]),
    },
}


_SCENARIO_NAMES = sorted(_SCENARIOS)
_PROVIDER_NAMES = ["fake", "openai_compat"]


@pytest.fixture(params=_SCENARIO_NAMES, ids=_SCENARIO_NAMES)
def scenario(request):
    return request.param


@pytest.fixture(params=_PROVIDER_NAMES, ids=_PROVIDER_NAMES)
def provider(request, scenario):
    return _SCENARIOS[scenario][request.param]()


def _req(**kw) -> LLMRequest:
    base = {"model": "m", "messages": [Message.user_text("hi")]}
    return LLMRequest(**{**base, **kw})


# --------------------------------------------------------------------------
# 契约断言（对全部 provider × 场景组合成立）
# --------------------------------------------------------------------------
async def test_c01_complete_returns_an_llm_response(provider):
    r = await provider.complete(_req())
    assert isinstance(r, LLMResponse)


async def test_c02_model_is_a_non_empty_string(provider):
    r = await provider.complete(_req())
    assert isinstance(r.model, str) and r.model


async def test_c03_finish_reason_is_one_of_the_known_values(provider):
    """finish_reason 决定上层如何解读结果，取值必须收敛。"""
    r = await provider.complete(_req())
    assert r.finish_reason in {"stop", "length", "tool_calls", "content_filter"}


async def test_c04_usage_is_never_none(provider):
    r = await provider.complete(_req())
    assert isinstance(r.usage, Usage)
    assert r.usage.input_tokens >= 0 and r.usage.output_tokens >= 0


async def test_c05_content_blocks_are_well_formed(provider):
    r = await provider.complete(_req())
    assert isinstance(r.content, list)
    for block in r.content:
        assert block["type"] in {"text", "tool_use"}


async def test_c06_every_tool_call_has_an_id_and_a_name(provider):
    """空 call_id 会让 TOOL_CALL / TOOL_RESULT 配对全部失效。"""
    r = await provider.complete(_req())
    for call in r.tool_calls:
        assert isinstance(call, ToolCall)
        assert call.call_id
        assert call.name


async def test_c07_tool_calls_agree_with_content_blocks(provider):
    """`tool_calls` 与 `content` 里的 `tool_use` 块必须一致。

    不一致会让按 content 走的下游与按 tool_calls 走的下游产生分歧。
    """
    r = await provider.complete(_req())
    from_content = [b for b in r.content if b["type"] == "tool_use"]
    assert len(from_content) == len(r.tool_calls)
    assert [b["id"] for b in from_content] == [c.call_id for c in r.tool_calls]


async def test_c08_text_matches_text_blocks(provider):
    r = await provider.complete(_req())
    joined = "".join(b["text"] for b in r.content if b["type"] == "text")
    assert r.text == joined


async def test_c09_raw_is_json_serializable(provider):
    """Raw 要能落进事件流。"""
    r = await provider.complete(_req())
    json.dumps(r.raw, default=str)


async def test_c10_latency_is_non_negative(provider):
    r = await provider.complete(_req())
    assert r.latency_ms >= 0


async def test_c11_aclose_is_idempotent(provider):
    await provider.aclose()
    await provider.aclose()


async def test_c12_provider_exposes_a_stable_name(provider):
    assert isinstance(provider.name, str) and provider.name


# --------------------------------------------------------------------------
# 场景特有的语义断言
#
# 这里用 provider_factory 而非共享的 (provider, scenario) 组合：
# 场景特有断言只该跑自己的场景，用 pytest.skip 排除其余组合会把
# "不适用"和"真的跳过了"混在同一个信号里，掩盖真正的问题。
# --------------------------------------------------------------------------
@pytest.fixture(params=_PROVIDER_NAMES, ids=_PROVIDER_NAMES)
def provider_factory(request):
    return lambda scenario: _SCENARIOS[scenario][request.param]()


async def test_s_text_scenario_yields_no_tool_calls(provider_factory):
    r = await provider_factory("text").complete(_req())
    assert r.tool_calls == []
    assert r.text


async def test_s_parallel_scenario_keeps_every_call(provider_factory):
    """只保留第一个工具调用会静默丢失 agent 的意图。"""
    r = await provider_factory("parallel_tools").complete(_req())
    assert [c.name for c in r.tool_calls] == ["read_file", "list_dir"]


async def test_s_parallel_scenario_keeps_call_ids_distinct(provider_factory):
    r = await provider_factory("parallel_tools").complete(_req())
    ids = [c.call_id for c in r.tool_calls]
    assert len(set(ids)) == len(ids), "重复的 call_id 会让结果配对错位"


async def test_s_malformed_arguments_degrade_to_empty_dict(provider_factory):
    """坏 JSON 必须降级而非崩溃 —— 一次可诊断的失败不该变成 run 崩溃。"""
    r = await provider_factory("malformed_arguments").complete(_req())
    assert r.tool_calls[0].arguments == {}
    assert r.tool_calls[0].name


async def test_s_empty_content_yields_empty_text(provider_factory):
    r = await provider_factory("empty_content").complete(_req())
    assert r.text == ""


async def test_s_missing_usage_degrades_to_zeros(provider_factory):
    """缺 usage 是常态（兼容层偏差）—— 记 0 比崩掉好。"""
    r = await provider_factory("no_usage").complete(_req())
    assert r.usage.input_tokens == 0
    assert r.usage.output_tokens == 0


async def test_s_truncated_response_keeps_the_length_finish_reason():
    """截断是重要信号 —— 抹平成 stop 会让上层误判为正常结束。"""
    provider = OpenAICompatProvider(
        api_key="k", base_url="https://example.invalid/v1",
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(
            _handler_for("length_truncated.json"))))
    r = await provider.complete(_req())
    assert r.finish_reason == "length"


def _handler_for(fixture: str):
    body = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=body)

    return handler


def test_adding_a_provider_requires_only_a_factory_entry():
    """可扩展性的可执行证据：断言没有对具体 provider 类型做任何分支。"""
    assert set(_PROVIDER_NAMES) == set(next(iter(_SCENARIOS.values())).keys())
    for scenario_map in _SCENARIOS.values():
        assert set(scenario_map) == set(_PROVIDER_NAMES), (
            "every scenario must provide the same set of providers"
        )
