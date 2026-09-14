# Agent 过程级评测 Harness — 实现计划 Part 2：真实模型与评测核心

> **面向 AI 代理的工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**前提：** Part 1（T01–T15）已完成，`harness run --suite examples/hello.yaml --script '[{"tool":"finish","summary":"hello"}]'` 可跑通。

**本部分范围：** M3–M5（T16–T26）。产出**真实模型可跑 + 前两个评测器可用**。

**配套文档：**
- 设计文档：`docs/superpowers/specs/2026-09-14-agent-eval-harness-design.md`
- 技术选型：`docs/tech-stack.md`
- Part 1：`docs/superpowers/plans/2026-09-14-agent-eval-harness-part1-foundation.md`
- Part 3：`docs/superpowers/plans/2026-09-14-agent-eval-harness-part3-orchestration.md`

---

## M3：真实 Provider 与录制回放

### 任务 16：`OpenAICompatProvider`

**文件：**
- 创建：`src/harness/providers/base.py`
- 创建：`src/harness/providers/openai_compat.py`
- 创建：`tests/providers/test_openai_compat.py`
- 创建：`tests/fixtures/provider/`（JSON 快照）

> **选型依据见 [tech-stack.md §1](../..//tech-stack.md)**：用官方 `openai` 3.x SDK（而非裸 httpx2、更非 litellm），因为它覆盖所有 OpenAI 兼容厂商，且 `http_client=` 是唯一的 transport 注入口——这正是我们做离线测试的地方。

- [ ] **步骤 1：编写失败的测试**

```python
# tests/providers/test_openai_compat.py
import json
from pathlib import Path

import httpx2
import pytest

from harness.contracts.protocols import LLMRequest, Message
from harness.providers.openai_compat import OpenAICompatProvider

FIXTURES = Path(__file__).parent.parent / "fixtures" / "provider"


def _mock_client(response_body: dict, captured: list) -> httpx2.AsyncClient:
    """用 MockTransport 完全离线地断言发出的 payload。"""
    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        return httpx2.Response(200, json=response_body)
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

async def test_text_response_is_normalized():
    body = json.loads((FIXTURES / "text_response.json").read_text(encoding="utf-8"))
    p = OpenAICompatProvider(api_key="k", base_url="https://x/v1",
                             client=_mock_client(body, []))
    r = await p.complete(LLMRequest(model="deepseek-chat",
                                    messages=[Message.user_text("hi")]))
    assert r.text == "hello"
    assert r.finish_reason == "stop"
    assert r.usage.input_tokens == 12

async def test_tool_calls_are_normalized_to_call_id_and_arguments():
    body = json.loads((FIXTURES / "tool_call_response.json").read_text(encoding="utf-8"))
    p = OpenAICompatProvider(api_key="k", base_url="https://x/v1",
                             client=_mock_client(body, []))
    r = await p.complete(LLMRequest(model="m", messages=[Message.user_text("hi")]))
    assert r.tool_calls[0].call_id == "call_abc"
    assert r.tool_calls[0].name == "read_file"
    assert r.tool_calls[0].arguments == {"path": "a.py"}

async def test_raw_provider_payload_is_preserved_for_lossless_replay():
    body = json.loads((FIXTURES / "text_response.json").read_text(encoding="utf-8"))
    p = OpenAICompatProvider(api_key="k", base_url="https://x/v1",
                             client=_mock_client(body, []))
    r = await p.complete(LLMRequest(model="m", messages=[Message.user_text("hi")]))
    assert r.raw == body

async def test_finish_reason_length_is_preserved():
    body = json.loads((FIXTURES / "length_truncated.json").read_text(encoding="utf-8"))
    p = OpenAICompatProvider(api_key="k", base_url="https://x/v1",
                             client=_mock_client(body, []))
    r = await p.complete(LLMRequest(model="m", messages=[Message.user_text("hi")]))
    assert r.finish_reason == "length"

async def test_missing_usage_does_not_crash():
    body = json.loads((FIXTURES / "no_usage.json").read_text(encoding="utf-8"))
    p = OpenAICompatProvider(api_key="k", base_url="https://x/v1",
                             client=_mock_client(body, []))
    r = await p.complete(LLMRequest(model="m", messages=[Message.user_text("hi")]))
    assert r.usage.input_tokens == 0

async def test_tools_are_sent_in_openai_format():
    body = json.loads((FIXTURES / "text_response.json").read_text(encoding="utf-8"))
    captured: list = []
    p = OpenAICompatProvider(api_key="k", base_url="https://x/v1",
                             client=_mock_client(body, captured))
    tools = [{"name": "read_file", "description": "d",
              "parameters": {"type": "object", "properties": {}}}]
    await p.complete(LLMRequest(model="m", messages=[Message.user_text("hi")], tools=tools))
    assert captured[0]["tools"][0]["type"] == "function"
    assert captured[0]["tools"][0]["function"]["name"] == "read_file"

async def test_malformed_arguments_json_does_not_crash():
    """模型偶尔会返回坏 JSON —— 必须降级而非崩溃。"""
    from harness.providers.openai_compat import _parse_arguments
    assert _parse_arguments("{not json") == {}
    assert _parse_arguments('{"a": 1}') == {"a": 1}
```

`tests/fixtures/provider/text_response.json`：

```json
{
  "id": "chatcmpl-1", "object": "chat.completion", "model": "deepseek-chat",
  "choices": [{"index": 0, "finish_reason": "stop",
    "message": {"role": "assistant", "content": "hello"}}],
  "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
}
```

`tool_call_response.json`：`choices[0].message.tool_calls = [{"id": "call_abc", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]`，`finish_reason = "tool_calls"`。

其余三个 fixture 按名字构造（`length_truncated.json` 的 `finish_reason="length"`；`no_usage.json` 无 `usage` 键）。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/providers/test_openai_compat.py -v`
预期：FAIL，`ModuleNotFoundError` 或 fixture 缺失

- [ ] **步骤 3：编写实现**

```python
# src/harness/providers/base.py
"""Provider 共用的归一化辅助。"""
from __future__ import annotations

from typing import Any

from harness.contracts.results import Usage


def usage_from_openai(raw: dict[str, Any] | None) -> Usage:
    """缺失 usage 时降级为全 0，不抛异常 —— provider 差异是常态。"""
    if not raw:
        return Usage()
    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cache_read_tokens=int(
            (raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
        calls=1,
    )
```

```python
# src/harness/providers/openai_compat.py
"""OpenAI 兼容 provider。

覆盖 OpenAI / DeepSeek / 通义 / Moonshot / Groq / vLLM / Ollama / OpenRouter。

设计决策：走官方 openai SDK（3.x），不是裸 httpx2。
  - SDK 覆盖所有 OpenAI 兼容端点（base_url 可配）
  - http_client= 是唯一 transport 注入口 → 单测注入 MockTransport，完全不联网
  - 不走 litellm：它会抹平厂商差异，而评测 harness 需要精确复现 payload

注意：厂商兼容层各有偏差（response_format / thinking 字段名）。
真遇到时写子类覆盖 _to_payload / _from_payload，不要指望改 base_url 就完事。
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx2
import openai

from harness.contracts.protocols import LLMRequest, LLMResponse, Message, ToolCall
from harness.contracts.results import Usage
from harness.providers.base import usage_from_openai


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """模型偶尔返回坏 JSON —— 降级为空 dict 而非崩溃。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _messages_to_payload(messages: list[Message]) -> list[dict[str, Any]]:
    """归一化内部 content blocks → OpenAI chat 格式（富 → 简，可预测的有损）。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            block = m.content[0]
            out.append({"role": "tool", "tool_call_id": block.get("tool_call_id"),
                        "content": block.get("content", "")})
            continue
        text = "".join(b.get("text", "") for b in m.content if b.get("type") == "text")
        tool_uses = [b for b in m.content if b.get("type") == "tool_use"]
        entry: dict[str, Any] = {"role": m.role, "content": text}
        if tool_uses:
            entry["tool_calls"] = [
                {"id": b["id"], "type": "function",
                 "function": {"name": b["name"],
                              "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}}
                for b in tool_uses
            ]
        out.append(entry)
    return out


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, *, api_key: str, base_url: str, client: httpx2.AsyncClient | None = None,
                 timeout_s: float = 120.0) -> None:
        self._client = client or httpx2.AsyncClient(timeout=timeout_s)
        self._sdk = openai.AsyncOpenAI(api_key=api_key, base_url=base_url,
                                       http_client=self._client)

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
        raw = completion.model_dump()
        return self._from_payload(raw, latency_ms=int((time.monotonic() - started) * 1000))

    @staticmethod
    def _from_payload(raw: dict[str, Any], *, latency_ms: int) -> LLMResponse:
        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""

        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})

        tool_calls: list[ToolCall] = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = _parse_arguments(fn.get("arguments"))
            call_id = tc.get("id") or "call_0"
            content.append({"type": "tool_use", "id": call_id,
                            "name": fn.get("name", ""), "input": args})
            tool_calls.append(ToolCall(call_id=call_id, name=fn.get("name", ""),
                                       arguments=args))

        return LLMResponse(
            model=raw.get("model") or "",
            content=content, text=text, tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage=usage_from_openai(raw.get("usage")),
            latency_ms=latency_ms,
            raw=raw,                       # ★ replay 无损性的唯一保证
        )

    async def aclose(self) -> None:
        await self._sdk.close()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/providers/ -v`
预期：全部 passed，**且不产生任何网络流量**（MockTransport 拦截）

- [ ] **步骤 5：手工对真实 API 打样一次**

```bash
export DEEPSEEK_API_KEY=sk-xxx
uv run python -c "
import anyio
from harness.contracts.protocols import LLMRequest, Message
from harness.providers.openai_compat import OpenAICompatProvider

async def main():
    p = OpenAICompatProvider(api_key='$DEEPSEEK_API_KEY', base_url='https://api.deepseek.com/v1')
    r = await p.complete(LLMRequest(model='deepseek-chat', messages=[Message.user_text('say hi in 3 words')]))
    print(r.text, r.usage)
anyio.run(main)
"
```

预期：打印模型回复与真实 token 用量。

- [ ] **步骤 6：Commit**

```bash
git add src/harness/providers/base.py src/harness/providers/openai_compat.py \
        tests/providers/ tests/fixtures/provider/
git commit -m "feat(providers): OpenAI-compatible provider via official SDK with httpx2 injection"
```

---

### 任务 17：`ResponsePool` 与录制回放

**文件：**
- 创建：`src/harness/providers/response_pool.py`
- 创建：`src/harness/providers/recording.py`
- 创建：`tests/providers/test_response_pool.py`

> **概念澄清（见设计文档 §3.6）**：这不是 HTTP cassette。它是 **LLM 响应采样池**，为 `MetaEvaluator` 的 judge consistency 提供「同一请求返回 N 个不同样本」的能力。HTTP 层录制交给 `vcrpy`（放 `tests/`）。两者是独立关注点。

- [ ] **步骤 1：编写失败的测试**

```python
# tests/providers/test_response_pool.py
import pytest

from harness.contracts.protocols import LLMRequest, Message
from harness.providers.recording import RecordingProvider, ReplayProvider
from harness.providers.response_pool import ResponsePool
from harness.providers.fake import FakeProvider, text_response


def _req(text: str = "hi") -> LLMRequest:
    return LLMRequest(model="m", messages=[Message.user_text(text)])


def test_same_request_yields_distinct_samples(tmp_path):
    """judge consistency 的核心需求：同请求返回不同样本。"""
    pool = ResponsePool(tmp_path)
    for t in ("a", "b", "c"):
        pool.record(_req(), {"text": t})
    assert [pool.replay(_req(), occurrence=i)["text"] for i in range(3)] == ["a", "b", "c"]


def test_occurrence_wraps_around_when_exhausted(tmp_path):
    pool = ResponsePool(tmp_path)
    pool.record(_req(), {"text": "only"})
    assert pool.replay(_req(), occurrence=5)["text"] == "only"


def test_different_requests_get_separate_slots(tmp_path):
    pool = ResponsePool(tmp_path)
    pool.record(_req("a"), {"text": "for-a"})
    pool.record(_req("b"), {"text": "for-b"})
    assert pool.replay(_req("a"), occurrence=0)["text"] == "for-a"


def test_key_is_stable_across_processes(tmp_path):
    """key 必须可复现，否则录制与回放对不上。"""
    from harness.providers.response_pool import ResponsePool as RP
    assert RP.key_of(_req(), "m") == RP.key_of(_req(), "m")


def test_missing_key_raises_with_actionable_message(tmp_path):
    pool = ResponsePool(tmp_path)
    with pytest.raises(KeyError, match="not recorded"):
        pool.replay(_req(), occurrence=0)

async def test_recording_provider_passes_through_and_records(tmp_path):
    inner = FakeProvider([text_response("real")])
    pool = ResponsePool(tmp_path)
    p = RecordingProvider(inner, pool)
    assert (await p.complete(_req())).text == "real"
    assert pool.replay(_req(), occurrence=0)["text"] == "real"

async def test_replay_provider_serves_from_pool_without_touching_inner(tmp_path):
    pool = ResponsePool(tmp_path)
    pool.record(_req(), {"model": "m", "content": [{"type": "text", "text": "canned"}],
                         "text": "canned", "tool_calls": [], "finish_reason": "stop",
                         "usage": {"input_tokens": 1, "output_tokens": 1}, "latency_ms": 0,
                         "raw": {}})
    p = ReplayProvider(pool)
    assert (await p.complete(_req())).text == "canned"

async def test_replay_provider_raises_on_cache_miss_by_default(tmp_path):
    p = ReplayProvider(ResponsePool(tmp_path))
    with pytest.raises(KeyError, match="not recorded"):
        await p.complete(_req())
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/providers/test_response_pool.py -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现**

```python
# src/harness/providers/response_pool.py
"""LLM 响应采样池 —— 不是 HTTP cassette。

用途：MetaEvaluator 的 judge consistency 需要「同一请求返回 N 个不同样本」。
vcrpy 的 allow_playback_repeats 只能重复同一响应，给不出 N 个不同样本，
所以这件事必须自己做。

存储形态：{key: [resp, resp, ...]} —— list + 游标，不是单个响应。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness.contracts.protocols import LLMRequest


class ResponsePool:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._data: dict[str, list[dict[str, Any]]] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    @staticmethod
    def key_of(req: LLMRequest, model: str) -> str:
        """sha256(model + canonical_json(req))。

        只对影响模型输出的字段取哈希 —— temperature 等采样参数必须进 key，
        因为 judge consistency 正是要观测同一 prompt 在不同采样下的判定差异。
        """
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "tools": req.tools,
            "temperature": req.temperature,
            "tool_choice": req.tool_choice,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def record(self, req: LLMRequest, response: dict[str, Any]) -> None:
        key = self.key_of(req, req.model)
        self._data.setdefault(key, []).append(response)

    def replay(self, req: LLMRequest, *, occurrence: int) -> dict[str, Any]:
        key = self.key_of(req, req.model)
        samples = self._data.get(key)
        if not samples:
            raise KeyError(
                f"response not recorded for key {key[:12]}... "
                f"(model={req.model}, {len(req.messages)} messages). "
                "Record first with --record, or check that the prompt is unchanged.")
        return samples[occurrence % len(samples)]

    def occurrence_count(self, req: LLMRequest) -> int:
        return len(self._data.get(self.key_of(req, req.model), []))

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
```

```python
# src/harness/providers/recording.py
"""录制/回放装饰器 —— 与厂商解耦（装饰器模式包住任意 provider）。"""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from harness.contracts.protocols import LLMRequest, LLMResponse, ToolCall, Usage
from harness.contracts.results import Usage as _Usage  # noqa: F401
from harness.providers.response_pool import ResponsePool


def _serialize(resp: LLMResponse) -> dict[str, Any]:
    return {
        "model": resp.model, "content": resp.content, "text": resp.text,
        "tool_calls": [{"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                       for c in resp.tool_calls],
        "finish_reason": resp.finish_reason, "usage": asdict(resp.usage),
        "latency_ms": resp.latency_ms, "raw": resp.raw,
    }


def _deserialize(data: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        model=data["model"], content=data["content"], text=data["text"],
        tool_calls=[ToolCall(**c) for c in data["tool_calls"]],
        finish_reason=data["finish_reason"], usage=Usage(**data["usage"]),
        latency_ms=data.get("latency_ms", 0), raw=data.get("raw", {}),
    )


class RecordingProvider:
    name = "recording"

    def __init__(self, inner: Any, pool: ResponsePool) -> None:
        self._inner, self._pool = inner, pool
        self._occurrence: dict[str, int] = {}

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
        self._pool, self._fallback = pool, fallback
        self._occurrence: dict[str, int] = {}

    async def complete(self, req: LLMRequest) -> LLMResponse:
        key = ResponsePool.key_of(req, req.model)
        try:
            data = self._pool.replay(req, occurrence=self._occurrence.get(key, 0))
        except KeyError:
            if self._fallback is None:
                raise
            return await self._fallback.complete(req)
        self._occurrence[key] = self._occurrence.get(key, 0) + 1
        return _deserialize(data)

    async def aclose(self) -> None:
        return None
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/providers/ -v`
预期：全部 passed

- [ ] **步骤 5：Commit**

```bash
git add src/harness/providers/response_pool.py src/harness/providers/recording.py \
        tests/providers/test_response_pool.py
git commit -m "feat(providers): ResponsePool sampler and record/replay decorators"
```

---

### 任务 18：Provider 一致性测试套件

**文件：**
- 创建：`tests/providers/test_conformance.py`
- 创建：`tests/fixtures/provider/`（补充边界 fixture）

> **价值**：新增一家 provider **只需加一个 factory**，不需改测试。这是架构可扩展性的可执行证据。

- [ ] **步骤 1：编写失败的测试**

```python
# tests/providers/test_conformance.py
"""Provider 一致性套件。

同 8 个场景跑遍所有 provider 实现。新增 provider 只需在 FACTORIES 里加一项。
"""
import json
from pathlib import Path

import httpx2
import pytest

from harness.contracts.protocols import LLMRequest, Message
from harness.providers.fake import FakeProvider, text_response
from harness.providers.openai_compat import OpenAICompatProvider

FIXTURES = Path(__file__).parent.parent / "fixtures" / "provider"


def _openai_with(fixture: str, captured: list | None = None):
    body = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if captured is not None:
            captured.append(json.loads(request.content))
        return httpx2.Response(200, json=body)

    return lambda: OpenAICompatProvider(
        api_key="k", base_url="https://x/v1",
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))

FACTORIES = {
    "openai_compat:text": _openai_with("text_response.json"),
    "fake:text": lambda: FakeProvider([text_response("hello")]),
}


@pytest.fixture(params=sorted(FACTORIES), ids=sorted(FACTORIES))
def provider(request):
    return FACTORIES[request.param]()


def _req(**kw) -> LLMRequest:
    base = {"model": "m", "messages": [Message.user_text("hi")]}
    return LLMRequest(**{**base, **kw})

async def test_scenario_1_plain_text(provider):
    r = await provider.complete(_req())
    assert isinstance(r.text, str) and r.text

async def test_scenario_2_finish_reason_is_set(provider):
    r = await provider.complete(_req())
    assert r.finish_reason in {"stop", "length", "tool_calls"}

async def test_scenario_3_usage_is_never_none(provider):
    r = await provider.complete(_req())
    assert r.usage is not None
    assert r.usage.input_tokens >= 0

async def test_scenario_4_content_blocks_are_well_formed(provider):
    r = await provider.complete(_req())
    for b in r.content:
        assert b["type"] in {"text", "tool_use"}

async def test_scenario_5_tool_calls_have_ids_and_names(provider):
    r = await provider.complete(_req())
    for tc in r.tool_calls:
        assert tc.call_id and tc.name

async def test_scenario_6_raw_is_json_serializable(provider):
    """raw 要能落盘进事件流。"""
    r = await provider.complete(_req())
    json.dumps(r.raw, default=str)

async def test_scenario_7_latency_is_recorded(provider):
    r = await provider.complete(_req())
    assert r.latency_ms >= 0

async def test_scenario_8_aclose_is_idempotent(provider):
    await provider.aclose()
    await provider.aclose()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/providers/test_conformance.py -v`
预期：部分 FAIL（边界 fixture 缺失）

- [ ] **步骤 3：补齐 fixture 并修正实现中暴露的问题**

补充 `tests/fixtures/provider/` 下的 `empty_content.json`（`content` 为 `""`）、`parallel_tool_calls.json`（两个 tool_calls）。若测试暴露 provider 实现的缺陷（例如 `content` 为空时 `text` 属性崩溃），在此步修复。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/providers/ -v`
预期：全部 passed（2 provider × 8 场景 = 16 项）

- [ ] **步骤 5：Commit**

```bash
git add tests/providers/test_conformance.py tests/fixtures/provider/
git commit -m "test(providers): conformance suite parametrized over all providers"
```

---

## M4：中间件全量与预算治理

### 任务 19：Permission / Policy / Sandbox 中间件

**文件：**
- 创建：`src/harness/core/middleware/permission.py`
- 创建：`src/harness/core/middleware/policy.py`
- 创建：`src/harness/core/middleware/sandbox.py`
- 创建：`tests/core/middleware/test_guards.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/core/middleware/test_guards.py
import pytest

from harness.contracts.protocols import ToolCall, ToolResult
from harness.core.middleware.permission import PermissionMiddleware
from harness.core.middleware.policy import PolicyMiddleware
from harness.core.middleware.sandbox import SandboxMiddleware
from harness.contracts.spec import MiddlewareSpec, RunSpec, RunRole, ModelRef, ToolPolicy


def _spec(allow=None, deny=None) -> RunSpec:
    return RunSpec(role=RunRole.SUT, system_prompt="s",
                   model=ModelRef(provider="fake", model="m"),
                   tools=ToolPolicy(allow=allow, deny=deny))


def _ctx(call: ToolCall, spec: RunSpec | None = None, scratch: dict | None = None):
    from types import SimpleNamespace
    return SimpleNamespace(call=call, spec=spec or _spec(), scratch=scratch or {},
                           ws=SimpleNamespace(root="/tmp/ws"))

async def _ok(ctx):
    return ToolResult(call_id=ctx.call.call_id, name=ctx.call.name, ok=True, content="ran")

async def test_permission_denies_tool_outside_allow_list():
    mw = PermissionMiddleware(MiddlewareSpec(name="permission"))
    r = await mw.handle(_ctx(ToolCall("c1", "write_file", {}), _spec(allow=["read_file"])), _ok)
    assert r.ok is False and r.denied_by == "permission"

async def test_permission_allows_tool_in_allow_list():
    mw = PermissionMiddleware(MiddlewareSpec(name="permission"))
    r = await mw.handle(_ctx(ToolCall("c1", "read_file", {}), _spec(allow=["read_file"])), _ok)
    assert r.ok is True

async def test_permission_denies_explicit_deny_list():
    mw = PermissionMiddleware(MiddlewareSpec(name="permission"))
    r = await mw.handle(_ctx(ToolCall("c1", "run_command", {}), _spec(deny=["run_command"])), _ok)
    assert r.ok is False and r.denied_by == "permission"

async def test_policy_denies_when_predicate_false():
    mw = PolicyMiddleware(MiddlewareSpec(
        name="policy", config={"rules": [{"tool": "run_command", "deny_if_arg_contains": "pip install"}]}))
    r = await mw.handle(
        _ctx(ToolCall("c1", "run_command", {"argv": ["pip", "install", "x"]})), _ok)
    assert r.ok is False and r.denied_by == "policy"

async def test_policy_allows_when_no_rule_matches():
    mw = PolicyMiddleware(MiddlewareSpec(name="policy", config={"rules": []}))
    assert (await mw.handle(_ctx(ToolCall("c1", "read_file", {})), _ok)).ok is True

async def test_sandbox_blocks_path_escape_before_reaching_tool():
    mw = SandboxMiddleware(MiddlewareSpec(name="sandbox"))
    r = await mw.handle(_ctx(ToolCall("c1", "read_file", {"path": "../../etc/passwd"})), _ok)
    assert r.ok is False and r.denied_by == "sandbox"

async def test_sandbox_passes_normal_path_through():
    mw = SandboxMiddleware(MiddlewareSpec(name="sandbox"))
    assert (await mw.handle(_ctx(ToolCall("c1", "read_file", {"path": "a.py"})), _ok)).ok is True

async def test_denied_result_short_circuits_but_is_still_a_result():
    """短路必须返回 ToolResult（而非 None/异常）—— 否则评测器看到悬空配对。"""
    mw = PermissionMiddleware(MiddlewareSpec(name="permission"))
    r = await mw.handle(_ctx(ToolCall("c1", "nope", {}), _spec(allow=["read_file"])), _ok)
    assert isinstance(r, ToolResult)
    assert r.call_id == "c1"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/core/middleware/ -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现**

```python
# src/harness/core/middleware/permission.py
"""权限中间件：按 ToolPolicy 的 allow/deny 拦截工具调用。

与 SandboxMiddleware 是两层独立防线：
  - Permission 管「这个工具能不能调」
  - Sandbox  管「这个参数能不能传」
两者都短路，但 denied_by 不同 —— 这对 FailureClassifier 有区分意义。
"""
from __future__ import annotations

from typing import Any

from harness.contracts.protocols import ToolResult
from harness.contracts.spec import MiddlewareSpec


class PermissionMiddleware:
    name = "permission"

    def __init__(self, spec: MiddlewareSpec) -> None:
        self._spec = spec

    async def handle(self, ctx: Any, nxt: Any) -> ToolResult:
        policy = ctx.spec.tools
        name = ctx.call.name

        denied = (policy.allow is not None and name not in policy.allow) or name in policy.deny
        if denied:
            allowed = policy.allow if policy.allow is not None else "all except denied"
            return ToolResult(
                call_id=ctx.call.call_id, name=name, ok=False,
                error=f"tool {name!r} not permitted (allowed: {allowed})",
                error_type="permission_denied", denied_by="permission")
        return await nxt(ctx)
```

```python
# src/harness/core/middleware/policy.py
"""通用策略中间件：配置驱动的声明式规则。

规则形态（config["rules"] 是列表，任一命中即拒绝）：
    {"tool": "run_command", "deny_if_arg_contains": "pip install"}
    {"tool": "*", "deny_if_arg_matches": "rm -rf"}
"""
from __future__ import annotations

import re
from typing import Any

from harness.contracts.protocols import ToolResult
from harness.contracts.spec import MiddlewareSpec


class PolicyMiddleware:
    name = "policy"

    def __init__(self, spec: MiddlewareSpec) -> None:
        self._rules: list[dict[str, Any]] = spec.config.get("rules", [])

    def _violates(self, call) -> str | None:
        blob = " ".join(str(v) for v in call.arguments.values())
        for rule in self._rules:
            tool = rule.get("tool", "*")
            if tool != "*" and tool != call.name:
                continue
            if (needle := rule.get("deny_if_arg_contains")) and needle in blob:
                return f"argument contains {needle!r}"
            if (pat := rule.get("deny_if_arg_matches")) and re.search(pat, blob):
                return f"argument matches /{pat}/"
        return None

    async def handle(self, ctx: Any, nxt: Any) -> ToolResult:
        if (reason := self._violates(ctx.call)) is not None:
            return ToolResult(call_id=ctx.call.call_id, name=ctx.call.name, ok=False,
                              error=f"blocked by policy: {reason}",
                              error_type="policy_denied", denied_by="policy")
        return await nxt(ctx)
```

```python
# src/harness/core/middleware/sandbox.py
"""沙箱中间件：参数级边界检查。

目前做路径越狱检测。工具层也有一份同样的检查 —— 
刻意冗余：中间件层能拦住**所有**工具的路径参数（包括未来新增的），
工具层能拦住绕过管道的直接调用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.contracts.protocols import ToolResult
from harness.contracts.spec import MiddlewareSpec

_PATH_ARG_KEYS = ("path", "file", "filename", "cwd", "dir")


class SandboxMiddleware:
    name = "sandbox"

    def __init__(self, spec: MiddlewareSpec) -> None:
        self._spec = spec

    async def handle(self, ctx: Any, nxt: Any) -> ToolResult:
        root = Path(getattr(ctx.ws, "root", ".")).resolve()
        for key in _PATH_ARG_KEYS:
            value = ctx.call.arguments.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                target = (root / value).resolve()
            except (OSError, ValueError):
                target = None
            if target is None or not target.is_relative_to(root):
                return ToolResult(
                    call_id=ctx.call.call_id, name=ctx.call.name, ok=False,
                    error=f"path escapes workspace: {value!r}",
                    error_type="path_escape", denied_by="sandbox")
        return await nxt(ctx)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/core/middleware/ -v`
预期：8 passed

- [ ] **步骤 5：Commit**

```bash
git add src/harness/core/middleware/permission.py src/harness/core/middleware/policy.py \
        src/harness/core/middleware/sandbox.py tests/core/middleware/
git commit -m "feat(middleware): permission, policy and sandbox guards"
```

---

### 任务 20：`TelemetryMW`

**文件：**
- 创建：`src/harness/core/middleware/telemetry.py`
- 创建：`tests/core/middleware/test_telemetry.py`

> **本任务的隐藏考点**：异常路径也必须落事件。设计文档 R3 明确要求 `try/finally` 包裹 `nxt`，且 `CancelledError`（`BaseException` 子类）不得被吞。

- [ ] **步骤 1：编写失败的测试**

```python
# tests/core/middleware/test_telemetry.py
import asyncio

import pytest

from harness.contracts.protocols import ToolCall, ToolResult
from harness.contracts.spec import MiddlewareSpec
from harness.core.middleware.telemetry import TelemetryMiddleware


def _ctx(call: ToolCall, events: list):
    from types import SimpleNamespace
    return SimpleNamespace(call=call, scratch={}, emit=events.append, turn=0, run_id="r1")

async def _ok(ctx):
    return ToolResult(call_id=ctx.call.call_id, name=ctx.call.name, ok=True, content="out")

async def _boom(ctx):
    raise RuntimeError("sandbox exploded")

async def test_successful_call_emits_one_result_event():
    events: list = []
    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    await mw.handle(_ctx(ToolCall("c1", "read_file", {}), events), _ok)
    results = [e for e in events if e.type.value == "tool.result"]
    assert len(results) == 1 and results[0].ok is True

async def test_exception_still_emits_result_event():
    """沙箱炸了也必须有结果事件 —— 否则评测器看到悬空配对。"""
    events: list = []
    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    with pytest.raises(RuntimeError):
        await mw.handle(_ctx(ToolCall("c1", "read_file", {}), events), _boom)
    results = [e for e in events if e.type.value == "tool.result"]
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error_type == "middleware_error"

async def test_cancelled_error_is_not_swallowed():
    """CancelledError 是 BaseException —— 必须继续向上传播，不能被 except Exception 吞掉。"""
    events: list = []

    async def cancelled(ctx):
        raise asyncio.CancelledError

    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    with pytest.raises(asyncio.CancelledError):
        await mw.handle(_ctx(ToolCall("c1", "f", {}), events), cancelled)

async def test_denied_result_is_also_recorded():
    events: list = []

    async def denied(ctx):
        return ToolResult(call_id="c1", name="f", ok=False, denied_by="permission")

    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    await mw.handle(_ctx(ToolCall("c1", "f", {}), events), denied)
    r = [e for e in events if e.type.value == "tool.result"][0]
    assert r.denied_by == "permission"

async def test_duration_is_recorded():
    events: list = []
    mw = TelemetryMiddleware(MiddlewareSpec(name="telemetry"))
    await mw.handle(_ctx(ToolCall("c1", "f", {}), events), _ok)
    assert [e for e in events if e.type.value == "tool.result"][0].duration_ms >= 0
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/core/middleware/test_telemetry.py -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现**

```python
# src/harness/core/middleware/telemetry.py
"""埋点中间件 —— 过程级评测的全部数据来源。

## 两条不可违反的规则（设计文档 R3）

1. **异常路径也必须落事件**。用 try/finally 保证。
   否则评测器看到「有 TOOL_CALL 无 TOOL_RESULT」的悬空配对。

2. **CancelledError 必须继续传播**。它继承自 BaseException 而非 Exception，
   所以 `except Exception` 天然不会吞它 —— 但绝不要写成 `except BaseException`。
   并发场景下吞掉取消会导致任务悬挂。
"""
from __future__ import annotations

import time
from typing import Any

from harness.contracts.protocols import ToolResult
from harness.contracts.spec import MiddlewareSpec
from harness.events.types import EventType, ToolResultEvent


class TelemetryMiddleware:
    name = "telemetry"

    def __init__(self, spec: MiddlewareSpec) -> None:
        self._spec = spec

    async def handle(self, ctx: Any, nxt: Any) -> ToolResult:
        started = time.monotonic()
        try:
            result = await nxt(ctx)
        except asyncio.CancelledError:
            raise                                   # 必须继续传播
        except Exception as exc:                    # noqa: BLE001
            result = ToolResult(call_id=ctx.call.call_id, name=ctx.call.name, ok=False,
                                error=str(exc), error_type="middleware_error")
            self._emit(ctx, result, started)
            raise
        else:
            self._emit(ctx, result, started)
            return result

    def _emit(self, ctx: Any, result: ToolResult, started: float) -> None:
        ctx.emit(ToolResultEvent(
            run_id=ctx.run_id, seq=ctx.next_seq(), type=EventType.TOOL_RESULT,
            turn=ctx.turn, call_id=result.call_id, name=result.name, ok=result.ok,
            content=result.content, error=result.error, error_type=result.error_type,
            duration_ms=result.duration_ms or int((time.monotonic() - started) * 1000),
            truncated=result.truncated, denied_by=result.denied_by))
```

> 注意：`asyncio.CancelledError` 需要 `import asyncio`。上面的 `except asyncio.CancelledError: raise` 是显式声明意图——它本来就不会被 `except Exception` 捕获，但写出来让审查者一眼看到这条约束。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/core/middleware/ -v`
预期：全部 passed

- [ ] **步骤 5：Commit**

```bash
git add src/harness/core/middleware/telemetry.py tests/core/middleware/test_telemetry.py
git commit -m "feat(middleware): telemetry emitting on all paths including exceptions"
```

---

### 任务 21：`BudgetGovernor` 与 `BudgetMW`

**文件：**
- 创建：`src/harness/core/budget.py`
- 创建：`src/harness/core/middleware/budget.py`
- 创建：`tests/core/test_budget.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/core/test_budget.py
import pytest

from harness.contracts.protocols import ToolCall
from harness.contracts.results import Usage
from harness.contracts.spec import Budget, MiddlewareSpec, RunStatus
from harness.core.budget import BudgetExceeded, BudgetGovernor
from harness.core.middleware.budget import BudgetMiddleware


def _gov(budget: Budget, events: list) -> BudgetGovernor:
    return BudgetGovernor(budget, run_id="r1", emit=events.append, next_seq=lambda: len(events))

async def test_turn_limit_terminates_with_budget_exceeded_not_error():
    """预算耗尽是独立终态 —— 与 LLM_ERROR 是完全不同的失败模式。"""
    events: list = []
    gov = _gov(Budget(max_turns=2), events)
    assert gov.check_turn(0) is None
    assert gov.check_turn(2) is RunStatus.BUDGET_EXCEEDED

async def test_warn_threshold_emits_event():
    events: list = []
    gov = _gov(Budget(max_turns=10, warn_at=0.8), events)
    gov.check_turn(8)
    warns = [e for e in events if e.action == "warn"]
    assert len(warns) == 1 and warns[0].dimension == "turns"

async def test_tool_call_limit_raises_budget_exceeded():
    gov = _gov(Budget(max_tool_calls=2), [])
    gov.check_tool_call()
    gov.check_tool_call()
    with pytest.raises(BudgetExceeded):
        gov.check_tool_call()

async def test_usd_limit_raises_and_reports_dimension():
    gov = _gov(Budget(max_usd=0.01), [])
    gov.charge_usage(Usage(cost_usd=0.02, calls=1))
    with pytest.raises(BudgetExceeded) as ei:
        gov.check_tool_call()
    assert ei.value.dimension == "usd"

async def test_usage_accumulates_monotonically():
    gov = _gov(Budget(), [])
    gov.charge_usage(Usage(input_tokens=10, cost_usd=0.01))
    gov.charge_usage(Usage(input_tokens=5, cost_usd=0.01))
    assert gov.snapshot()["input_tokens"] == 15

async def test_remaining_never_goes_negative():
    gov = _gov(Budget(max_turns=5), [])
    gov.check_turn(99)
    assert gov.view.remaining("turns") >= 0

async def test_budget_middleware_short_circuits_on_exhaustion():
    events: list = []

    class _WS: ...
    def _ctx(call):
        from types import SimpleNamespace
        return SimpleNamespace(call=call, scratch={}, spec=None, ws=_WS(), turn=0, run_id="r1",
                               emit=events.append, next_seq=lambda: len(events))

    gov = _gov(Budget(max_tool_calls=0), events)
    mw = BudgetMiddleware(MiddlewareSpec(name="budget"), governor=gov)

    async def _ok(ctx):
        raise AssertionError("should not reach downstream")

    r = await mw.handle(_ctx(ToolCall("c1", "f", {})), _ok)
    assert r.ok is False and r.denied_by == "budget"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/core/test_budget.py -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现**

```python
# src/harness/core/budget.py
"""预算治理。

预算必须在**三处**强制，缺一不可：
  1. loop 每轮开头   —— turns / wall_clock（终止性保证，防死循环）
  2. LLM 调用前后    —— 调用前估算 input 上限，调用后 charge 实际 usage
  3. 每次工具调用前  —— check_tool_call

关键语义：BUDGET_EXCEEDED 是**独立终态**，不是 ERROR。
FailureClassifier 需要区分「预算耗尽未收敛」与「模型报错」。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from harness.contracts.results import Usage
from harness.contracts.spec import Budget, RunStatus
from harness.events.types import BudgetEvent, EventType


class BudgetExceeded(Exception):
    def __init__(self, dimension: str, limit: float, consumed: float) -> None:
        super().__init__(f"budget exceeded: {dimension} {consumed} > {limit}")
        self.dimension, self.limit, self.consumed = dimension, limit, consumed

_DIMENSIONS = ("turns", "tool_calls", "input_tokens", "output_tokens", "usd", "wall_clock")


@dataclass
class BudgetView:
    _gov: "BudgetGovernor"

    def remaining(self, dimension: str) -> float:
        return max(0.0, self._gov.limit_of(dimension) - self._gov.consumed_of(dimension))

    def consumed(self, dimension: str) -> float:
        return self._gov.consumed_of(dimension)


class BudgetGovernor:
    def __init__(self, budget: Budget, *, run_id: str,
                 emit: Callable[[Any], None], next_seq: Callable[[], int]) -> None:
        self._b = budget
        self._run_id, self._emit, self._next_seq = run_id, emit, next_seq
        self._started = time.monotonic()
        self._consumed: dict[str, float] = {d: 0.0 for d in _DIMENSIONS}
        self._warned: set[str] = set()

    # ---- 限额 ----
    def limit_of(self, dimension: str) -> float:
        return {
            "turns": self._b.max_turns,
            "tool_calls": self._b.max_tool_calls,
            "input_tokens": self._b.max_input_tokens,
            "output_tokens": self._b.max_output_tokens,
            "usd": self._b.max_usd,
            "wall_clock": self._b.max_wall_clock_s,
        }[dimension]

    def consumed_of(self, dimension: str) -> float:
        if dimension == "wall_clock":
            return time.monotonic() - self._started
        return self._consumed[dimension]

    @property
    def view(self) -> BudgetView:
        return BudgetView(self)

    # ---- 检查点 ----
    def _check(self, dimension: str, *, raise_on_exceed: bool) -> bool:
        limit, used = self.limit_of(dimension), self.consumed_of(dimension)
        if used >= limit:
            if raise_on_exceed:
                self._emit_warn(dimension, limit, used, action="deny")
                raise BudgetExceeded(dimension, limit, used)
            return True
        if used >= limit * self._b.warn_at:
            self._emit_warn(dimension, limit, used, action="warn")
        return False

    def _emit_warn(self, dimension: str, limit: float, used: float, *, action: str) -> None:
        key = f"{dimension}:{action}"
        if key in self._warned:
            return
        self._warned.add(key)
        self._emit(BudgetEvent(
            run_id=self._run_id, seq=self._next_seq(), type=EventType.BUDGET_EVENT,
            dimension=dimension, limit=limit, consumed=used,
            threshold=self._b.warn_at, action=action))

    def check_turn(self, turn: int) -> RunStatus | None:
        self._consumed["turns"] = turn
        if self._check("turns", raise_on_exceed=False):
            return RunStatus.BUDGET_EXCEEDED
        if self._check("wall_clock", raise_on_exceed=False):
            return RunStatus.BUDGET_EXCEEDED
        return None

    def check_tool_call(self) -> None:
        self._consumed["tool_calls"] += 1
        self._check("tool_calls", raise_on_exceed=True)

    def precheck_llm(self, est_input_tokens: int) -> None:
        if self._consumed["input_tokens"] + est_input_tokens > self._b.max_input_tokens:
            raise BudgetExceeded("input_tokens", self._b.max_input_tokens,
                                 self._consumed["input_tokens"] + est_input_tokens)

    def charge_usage(self, usage: Usage) -> None:
        self._consumed["input_tokens"] += usage.input_tokens
        self._consumed["output_tokens"] += usage.output_tokens
        self._consumed["usd"] += usage.cost_usd
        self._check("input_tokens", raise_on_exceed=True)
        self._check("output_tokens", raise_on_exceed=True)
        self._check("usd", raise_on_exceed=True)

    def snapshot(self) -> dict[str, float]:
        return {d: self.consumed_of(d) for d in _DIMENSIONS}
```

```python
# src/harness/core/middleware/budget.py
"""预算中间件：每次工具调用前校验。

注意状态作用域：governor 是 run 级共享状态（正确），
但 BudgetMiddleware 自身不持有调用级状态。
"""
from __future__ import annotations

from typing import Any

from harness.contracts.protocols import ToolResult
from harness.contracts.spec import MiddlewareSpec
from harness.core.budget import BudgetExceeded, BudgetGovernor


class BudgetMiddleware:
    name = "budget"

    def __init__(self, spec: MiddlewareSpec, *, governor: BudgetGovernor) -> None:
        self._spec, self._governor = spec, governor

    async def handle(self, ctx: Any, nxt: Any) -> ToolResult:
        try:
            self._governor.check_tool_call()
        except BudgetExceeded as exc:
            return ToolResult(call_id=ctx.call.call_id, name=ctx.call.name, ok=False,
                              error=str(exc), error_type="budget_exceeded", denied_by="budget")
        return await nxt(ctx)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/core/ -v`
预期：全部 passed

- [ ] **步骤 5：Commit**

```bash
git add src/harness/core/budget.py src/harness/core/middleware/budget.py tests/core/test_budget.py
git commit -m "feat(core): budget governor with independent BUDGET_EXCEEDED terminal state"
```

---

### 任务 22：`ContextManager` 与 `CONTEXT_COMPACT`

**文件：**
- 修改：`src/harness/core/context.py`（任务 10 已建最小版：消息累积 + `build_request`）
- 创建：`tests/core/test_context.py`

> **本任务扩展任务 10 留下的最小实现**，加入 token 估算、`needs_compaction()`、`compact()` 与 `CONTEXT_COMPACT` 事件。接口保持不变，因此**不需要改动 loop**。

> **设计要点**：我们**不需要做好**压缩，只需要**记录**压缩。`CONTEXT_COMPACT` 是一等事件——上下文被压缩后丢失关键信息是真实的失败模式（MAST FM-1.4）。

- [ ] **步骤 1：编写失败的测试**

```python
# tests/core/test_context.py
import pytest

from harness.contracts.protocols import LLMResponse, Message, ToolCall, ToolResult, Usage
from harness.core.context import ContextManager


def _resp(text: str, calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(model="m", content=[{"type": "text", "text": text}], text=text,
                       tool_calls=calls or [], finish_reason="stop",
                       usage=Usage(), latency_ms=0)


def _cm(token_budget: int = 1000) -> ContextManager:
    return ContextManager(system_prompt="sys", token_budget=token_budget)


def test_build_request_includes_system_prompt_first():
    req = _cm().build_request(turn=0)
    assert req.messages[0].role == "system"


def test_assistant_message_is_appended():
    cm = _cm()
    cm.append_assistant(_resp("hello"))
    assert cm.build_request(turn=1).messages[-1].content[0]["text"] == "hello"


def test_tool_result_is_appended_as_tool_role():
    cm = _cm()
    cm.append_tool_result(ToolResult(call_id="c1", name="f", ok=True, content="out"))
    assert cm.build_request(turn=1).messages[-1].role == "tool"


def test_no_compaction_needed_when_under_budget():
    cm = _cm(token_budget=100_000)
    cm.append_assistant(_resp("x" * 100))
    assert cm.needs_compaction() is False
    assert cm.compact() is None


def test_compaction_triggers_over_budget():
    cm = _cm(token_budget=50)
    for _ in range(20):
        cm.append_assistant(_resp("y" * 200))
    assert cm.needs_compaction() is True


def test_compact_returns_event_and_drops_oldest():
    cm = _cm(token_budget=200)
    for i in range(30):
        cm.append_assistant(_resp(f"msg-{i}-" + "z" * 100))
    before = len(cm.build_request(turn=0).messages)
    event = cm.compact()
    after = len(cm.build_request(turn=0).messages)
    assert event is not None
    assert after < before
    assert event.tokens_before > event.tokens_after


def test_compact_event_records_strategy_and_reason():
    cm = _cm(token_budget=100)
    for _ in range(30):
        cm.append_assistant(_resp("q" * 100))
    event = cm.compact()
    assert event.strategy == "drop_oldest_tool_results"
    assert event.reason == "token_pressure"


def test_system_prompt_survives_compaction():
    """系统提示词绝不能被压缩掉。"""
    cm = _cm(token_budget=100)
    for _ in range(30):
        cm.append_assistant(_resp("r" * 100))
    cm.compact()
    assert cm.build_request(turn=0).messages[0].role == "system"


def test_estimate_tokens_is_deterministic():
    """同样的消息序列必须给出同样的估算 —— 否则金轨迹与重放会漂移。"""
    a, b = _cm(), _cm()
    a.append_assistant(_resp("same text"))
    b.append_assistant(_resp("same text"))
    assert a.estimated_tokens() == b.estimated_tokens()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/core/test_context.py -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现**

```python
# src/harness/core/context.py
"""上下文管理：消息累积 + token 估算 + 压缩事件。

设计决策：**我们不追求压缩质量，只要求压缩可观测。**
CONTEXT_COMPACT 是一等事件 —— 压缩后 agent 丢失关键信息是真实失败模式
（MAST FM-1.4 Loss of conversation history）。

## Token 估算的双轨
provider 上报的 usage 是权威值（落在 LLMResponseEvent 上）。
这里的本地估算是**独立的一路**，仅用于触发压缩决策。
两者都落盘，且来源可区分 —— 否则评测数字不可信。
tiktoken 对 DeepSeek/通义无效，这里用字符数启发式（中文 1.5 字符/token，英文 4 字符/token）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.contracts.protocols import LLMRequest, LLMResponse, Message, ToolResult
from harness.events.types import ContextCompactEvent, EventType

_CHARS_PER_TOKEN_ASCII = 4.0
_CHARS_PER_TOKEN_CJK = 1.5


def estimate_tokens(text: str) -> int:
    """字符数启发式。确定性 —— 同样的输入必须给同样的输出。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    ascii_chars = len(text) - cjk
    return int(cjk / _CHARS_PER_TOKEN_CJK + ascii_chars / _CHARS_PER_TOKEN_ASCII) + 1


@dataclass
class _Built:
    messages: list[Message]
    digest: str


class ContextManager:
    def __init__(self, *, system_prompt: str, token_budget: int = 100_000) -> None:
        self._system = Message(role="system", content=[{"type": "text", "text": system_prompt}])
        self._system_prompt = system_prompt
        self._history: list[Message] = []
        self._token_budget = token_budget
        self._last_compaction: ContextCompactEvent | None = None

    def append_assistant(self, resp: LLMResponse) -> None:
        self._history.append(Message(role="assistant", content=resp.content))

    def append_tool_result(self, result: ToolResult) -> None:
        self._history.append(Message(role="tool", content=[{
            "type": "tool_result", "tool_call_id": result.call_id,
            "content": result.content if result.ok else f"ERROR: {result.error}",
        }]))

    def build_request(self, turn: int) -> _Built:
        messages = [self._system, *self._history]
        return _Built(messages=messages, digest=f"t{turn}-{len(messages)}-{self.estimated_tokens()}")

    def estimated_tokens(self) -> int:
        total = estimate_tokens(self._system_prompt)
        for m in self._history:
            for block in m.content:
                total += estimate_tokens(str(block.get("text") or block.get("content") or ""))
        return total

    def needs_compaction(self) -> bool:
        return self.estimated_tokens() > self._token_budget

    def compact(self) -> ContextCompactEvent | None:
        """丢弃最旧的 tool 结果，保留最近的对话结构。

        刻意选择最简单的策略 —— 目标是**产生一个可被评测的压缩事件**，
        而不是实现一个聪明的压缩算法。
        """
        if not self.needs_compaction():
            return None

        tokens_before = self.estimated_tokens()
        messages_before = len(self._history)
        dropped: list[str] = []

        # 从最旧的 tool 消息开始丢，但保留最后 4 条（保证当前上下文连贯）
        keep_tail = 4
        kept: list[Message] = []
        for i, m in enumerate(self._history):
            if i < len(self._history) - keep_tail and m.role == "tool":
                dropped.append(str(hash(str(m.content)))[:12])
                continue
            kept.append(m)
        self._history = kept

        self._last_compaction = ContextCompactEvent(
            run_id="", seq=0, type=EventType.CONTEXT_COMPACT,
            reason="token_pressure",
            messages_before=messages_before, messages_after=len(self._history),
            tokens_before=tokens_before, tokens_after=self.estimated_tokens(),
            dropped_message_digests=dropped,
            strategy="drop_oldest_tool_results")
        return self._last_compaction

    @property
    def last_compaction(self) -> ContextCompactEvent | None:
        return self._last_compaction
```

> **实现提示**：`ContextCompactEvent` 的 `run_id` / `seq` 由 `RunContext` 在发事件前填充（`compact()` 返回的事件是模板）。在 `agent_loop` 里调用 `compact()` 后要 `ctx.emit(event.model_copy(update={"run_id": ctx.run_id, "seq": ctx.next_seq()}))`。并把 `needs_compaction()` 检查加进 loop 的每轮结尾。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/core/ -v`
预期：全部 passed

- [ ] **步骤 5：接入 loop 并验证端到端**

在 `agent_loop` 每轮末尾加入压缩检查：

```python
        if ctx.context.needs_compaction():
            tpl = ctx.context.compact()
            if tpl is not None:
                ctx.emit(tpl.model_copy(update={"run_id": ctx.run_id,
                                                "seq": ctx.next_seq()}))
```

跑：`uv run pytest tests/e2e/ -v`，预期仍全绿。

- [ ] **步骤 6：Commit**

```bash
git add src/harness/core/context.py tests/core/test_context.py src/harness/core/loop.py
git commit -m "feat(core): context manager emitting first-class CONTEXT_COMPACT events"
```

---

## M5：评测器框架与首批过程级评测器

> **本里程碑的核心命题**：让「声明式订阅」从文档变成**可执行的行为**，并让「评测器不依赖 core」由架构测试强制。

### 任务 23：评测器框架与 `evalrunner`

**文件：**
- 创建：`src/harness/evaluators/base.py`
- 创建：`src/harness/orchestration/evalrunner.py`
- 创建：`tests/evaluators/test_base.py`

> **声明式订阅必须是真行为，不只是文档。** 本任务的测试要用计数器证明：不需要某事件的评测器**根本没被实例化**。

- [ ] **步骤 1：编写失败的测试**

```python
# tests/evaluators/test_base.py
import pytest

from harness.contracts.results import EvalResult, EvalStatus, Finding, Severity
from harness.contracts.protocols import EvalContext
from harness.evaluators.base import BaseEvaluator, run_evaluators
from harness.events.trajectory import Trajectory
from harness.events.types import EventType, RunEndEvent, RunStartEvent


def _traj(*types: EventType) -> Trajectory:
    events = []
    for i, t in enumerate(types):
        if t is EventType.RUN_START:
            events.append(RunStartEvent(run_id="r1", seq=i, type=t,
                                        role="sut", model="m", provider="fake"))
        elif t is EventType.RUN_END:
            events.append(RunEndEvent(run_id="r1", seq=i, type=t, status="ok"))
    return Trajectory.from_events("r1", events)


class _Spy(BaseEvaluator):
    name = "spy"
    subscribes = frozenset({EventType.CONTEXT_COMPACT})
    instances = 0
    calls = 0

    def __init__(self) -> None:
        type(self).instances += 1

    def evaluate(self, traj, ctx) -> EvalResult:
        type(self).calls += 1
        return EvalResult(evaluator=self.name, run_id=traj.run_id, status=EvalStatus.PASS)

async def test_evaluator_is_skipped_when_subscribed_event_absent():
    _Spy.instances = _Spy.calls = 0
    results = list(await run_evaluators([_Spy], _traj(EventType.RUN_START, EventType.RUN_END),
                                        EvalContext()))
    assert results == []
    assert _Spy.calls == 0, "评测器不该被调用"

async def test_evaluator_runs_when_subscribed_event_present():
    _Spy.instances = _Spy.calls = 0
    traj = _traj(EventType.RUN_START, EventType.RUN_END)
    from harness.events.types import ContextCompactEvent
    traj = Trajectory.from_events("r1", [*traj.events,
        ContextCompactEvent(run_id="r1", seq=99, type=EventType.CONTEXT_COMPACT,
                            reason="token_pressure", messages_before=1, messages_after=0,
                            tokens_before=1, tokens_after=0, dropped_message_digests=[],
                            strategy="drop_oldest_tool_results")])
    results = list(await run_evaluators([_Spy], traj, EvalContext()))
    assert len(results) == 1 and _Spy.calls == 1

async def test_evaluator_exception_becomes_error_status_not_crash():
    class Boom(BaseEvaluator):
        name = "boom"
        subscribes = frozenset({EventType.RUN_END})
        def evaluate(self, traj, ctx):
            raise RuntimeError("evaluator bug")

    results = list(await run_evaluators([Boom], _traj(EventType.RUN_START, EventType.RUN_END),
                                        EvalContext()))
    assert len(results) == 1
    assert results[0].status is EvalStatus.ERROR
    assert "evaluator bug" in results[0].error

async def test_sync_and_async_evaluators_both_work():
    class Sync(BaseEvaluator):
        name = "sync"; subscribes = frozenset({EventType.RUN_END})
        def evaluate(self, traj, ctx):
            return EvalResult(evaluator=self.name, run_id=traj.run_id, status=EvalStatus.PASS)

    class Async(BaseEvaluator):
        name = "async"; subscribes = frozenset({EventType.RUN_END})
        async def evaluate(self, traj, ctx):
            return EvalResult(evaluator=self.name, run_id=traj.run_id, status=EvalStatus.PASS)

    results = list(await run_evaluators([Sync, Async],
                                        _traj(EventType.RUN_START, EventType.RUN_END),
                                        EvalContext()))
    assert {r.evaluator for r in results} == {"sync", "async"}


def test_finding_defaults_to_minor_severity():
    assert Finding(code="x", message="y").severity is Severity.MINOR
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/evaluators/ -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现**

```python
# src/harness/evaluators/base.py
"""评测器基类。

## 两条规约

1. **评测器只依赖 `events` 与 `contracts`，绝不 import `core`**。
   需要 judge 的评测器通过 EvalContext 里的 JudgeClient 协议触达，
   由 orchestration 层注入真实实现。这是「评测器与 agent 零耦合」的落点。

2. **临时字段进 Finding.data，不进事件 schema**。
   想给事件加字段前先自问：能否从已有事件派生？
   绝大多数「我需要 X 字段」其实是「我能从 TOOL_CALL/TOOL_RESULT 配对算出来」。

## 契约
- subscribes 的事件不存在 → 返回 SKIPPED（不抛异常）
- 自身抛异常 → evalrunner 兜底为 ERROR（与 FAIL 严格区分）
- 退化输入（空轨迹 / 无 RUN_END / 截断）→ 绝不抛异常
"""
from __future__ import annotations

from typing import Any

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalResult, EvalStatus
from harness.events.trajectory import Trajectory
from harness.events.types import EventType


class BaseEvaluator:
    name: str = "unnamed"
    version: str = "0.1.0"
    subscribes: frozenset[EventType] = frozenset()

    def evaluate(self, traj: Trajectory, ctx: EvalContext) -> Any:
        raise NotImplementedError

    def skipped(self, traj: Trajectory, reason: str) -> EvalResult:
        return EvalResult(evaluator=self.name, evaluator_version=self.version,
                          run_id=traj.run_id, status=EvalStatus.SKIPPED, summary=reason)
```

```python
# src/harness/orchestration/evalrunner.py
"""评测器调度器。

**声明式订阅在这里变成真实行为**：事件类型不在轨迹里的评测器，
根本不实例化、不调用。这既省成本，也证明抽象是真的。
"""
from __future__ import annotations

import inspect
import time
from collections.abc import Sequence
from typing import Any

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalResult, EvalStatus
from harness.events.trajectory import Trajectory

async def run_evaluators(
    evaluator_classes: Sequence[type],
    traj: Trajectory,
    ctx: EvalContext,
) -> list[EvalResult]:
    present = {e.type for e in traj.events}
    out: list[EvalResult] = []

    for cls in evaluator_classes:
        if not (cls.subscribes & present):
            continue                          # 跳过：不实例化、不调用
        started = time.monotonic()
        try:
            instance = cls()
            result = instance.evaluate(traj, ctx)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:              # noqa: BLE001
            out.append(EvalResult(
                evaluator=getattr(cls, "name", cls.__name__), run_id=traj.run_id,
                status=EvalStatus.ERROR, error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000)))
            continue
        result.duration_ms = int((time.monotonic() - started) * 1000)
        out.append(result)

    return out
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/evaluators/ -v`
预期：5 passed，其中「跳过」测试断言 `calls == 0`

- [ ] **步骤 5：Commit**

```bash
git add src/harness/evaluators/base.py src/harness/orchestration/evalrunner.py \
        tests/evaluators/test_base.py
git commit -m "feat(evaluators): base class and runner with declarative subscription"
```

---

### 任务 24：`TrajectoryBuilder`

**文件：**
- 创建：`src/harness/testing/__init__.py`
- 创建：`src/harness/testing/builder.py`
- 创建：`tests/testing/test_builder.py`

> **它作为产品的一部分发布**（不是测试私有工具）——用户写自己的评测器时也能用。这正是「评测器可独立单测」这个卖点的兑现。

- [ ] **步骤 1：编写失败的测试**

```python
# tests/testing/test_builder.py
import pytest

from harness.events.types import EventType
from harness.testing import TrajectoryBuilder as TB


def test_builder_produces_well_formed_trajectory():
    traj = (TB(run_id="r1", task="fix bug")
            .turn()
              .llm_response(text="looking", tool_calls=[("read_file", {"path": "a.py"})])
              .tool_result(name="read_file", content="code", ok=True)
            .turn()
              .llm_response(tool_calls=[("finish", {"summary": "done"})])
              .tool_result(name="finish", content="done", ok=True)
            .run_end(status="ok")
            .build())
    assert traj.tool_sequence() == ("read_file", "finish")
    assert traj.status == "ok"


def test_seq_is_auto_assigned_and_monotonic():
    traj = (TB(run_id="r1").turn().llm_response(text="a")
            .turn().llm_response(text="b").run_end().build())
    seqs = [e.seq for e in traj.events]
    assert seqs == list(range(len(seqs)))


def test_tool_result_auto_pairs_with_last_unmatched_call():
    """自动维护 call_id 配对 —— 手写轨迹最容易错的地方。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("read_file", {"path": "a.py"})])
            .tool_result(name="read_file", content="x", ok=True)
            .run_end().build())
    call = traj.tool_calls()[0]
    assert traj.result_for(call.call_id).content == "x"


def test_explicit_call_id_is_respected():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {}, "custom_id")])
            .tool_result(name="f", content="x", ok=True, call_id="custom_id")
            .run_end().build())
    assert traj.tool_calls()[0].call_id == "custom_id"


def test_raw_emit_allows_malformed_sequences():
    """评测器健壮性测试需要注入畸形序列。"""
    traj = (TB(run_id="r1").raw_emit(EventType.TOOL_CALL, call_id="dangling",
                                     name="f", arguments={})
            .run_end().build())
    assert traj.result_for("dangling") is None


def test_builder_can_omit_run_end():
    """退化输入：缺 RUN_END。评测器必须不崩。"""
    traj = TB(run_id="r1").turn().llm_response(text="a").build()
    assert traj.end() is None


def test_empty_trajectory_can_be_built():
    traj = TB(run_id="r1").build()
    assert traj.events == ()


def test_truncated_flag_is_auto_detected_from_marker():
    """与 LocalExecutor._cap() 的输出格式对齐 —— 否则 GroundingChecker 的截断分支是死代码。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("run_command", {"argv": ["pytest"]})])
            .tool_result(name="run_command",
                         content="head\n... [truncated] ...\ntail", ok=True)
            .run_end().build())
    assert traj.tool_results()[0].truncated is True


def test_truncated_flag_can_be_set_explicitly():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("f", {})])
            .tool_result(name="f", content="full output", ok=True, truncated=True)
            .run_end().build())
    assert traj.tool_results()[0].truncated is True
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/testing/ -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现**

```python
# src/harness/testing/builder.py
"""TrajectoryBuilder —— 构造事件序列，让评测器可在无 LLM 条件下单测。

作为产品的一部分发布：用户写自己的评测器时也能用。

自动维护 seq 与 call_id 配对（.tool_result 自动匹配上一个未配对的同名 tool_call），
并提供 raw_emit() 注入畸形序列来测评测器的健壮性。
"""
from __future__ import annotations

from typing import Any

from harness.contracts.protocols import ToolCall
from harness.events.trajectory import Trajectory
from harness.events.types import (
    EventType, LLMRequestEvent, LLMResponseEvent, RunEndEvent, RunStartEvent,
    ToolCallEvent, ToolResultEvent, TurnStartEvent,
)

# 与 core/executors/local.py 的 LocalExecutor._cap() 输出格式保持一致。
# 两处必须同步修改：这里是「构造侧」，那里是「生产侧」。
TRUNCATION_MARKER = "... [truncated] ..."


class TrajectoryBuilder:
    def __init__(self, run_id: str = "run_test", *, task: str | None = None,
                 role: str = "sut", model: str = "fake") -> None:
        self._run_id = run_id
        self._events: list[Any] = []
        self._seq = 0
        self._turn = 0
        self._pending: list[ToolCall] = []
        self._call_counter = 0
        self._start = RunStartEvent(run_id=run_id, seq=self._next(), type=EventType.RUN_START,
                                    role=role, task=task, model=model, provider="builder")

    def _next(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def turn(self) -> "TrajectoryBuilder":
        self._turn += 1
        self._events.append(TurnStartEvent(run_id=self._run_id, seq=self._next(),
                                           type=EventType.TURN_START, turn=self._turn))
        return self

    def llm_response(self, *, text: str = "", tool_calls: list | None = None,
                     finish_reason: str | None = None) -> "TrajectoryBuilder":
        calls: list[ToolCall] = []
        for spec in (tool_calls or []):
            name, args = spec[0], spec[1]
            cid = spec[2] if len(spec) > 2 else f"call_{self._call_counter}"
            self._call_counter += 1
            calls.append(ToolCall(call_id=cid, name=name, arguments=args))
            self._pending.append(calls[-1])

        self._events.append(LLMResponseEvent(
            run_id=self._run_id, seq=self._next(), type=EventType.LLM_RESPONSE,
            turn=self._turn, model="fake", content=[{"type": "text", "text": text}],
            text=text,
            tool_calls=[{"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                        for c in calls],
            finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
            latency_ms=0))
        return self

    def tool_result(self, *, name: str, content: str = "", ok: bool = True,
                    call_id: str | None = None, error: str | None = None,
                    error_type: str | None = None,
                    truncated: bool | None = None) -> "TrajectoryBuilder":
        if call_id is None:
            match = next((c for c in self._pending if c.name == name), None)
            call_id = match.call_id if match else f"unmatched_{self._call_counter}"
            if match:
                self._pending.remove(match)
        if truncated is None:
            # 与 LocalExecutor._cap() 的输出格式对齐，自动推断截断状态。
            # 没有这层推断，GroundingChecker 的截断分支永远触发不了（truncated 恒为 False），
            # 「输出被截断时只出 WARN 而不判 FAIL」这条规则就成了死代码。
            truncated = TRUNCATION_MARKER in content
        self._events.append(ToolResultEvent(
            run_id=self._run_id, seq=self._next(), type=EventType.TOOL_RESULT,
            turn=self._turn, call_id=call_id, name=name, ok=ok, content=content,
            error=error, error_type=error_type, truncated=truncated))
        return self

    def raw_emit(self, event_type: EventType, **fields: Any) -> "TrajectoryBuilder":
        from harness.events.types import parse_event
        self._events.append(parse_event({
            "type": event_type.value, "run_id": self._run_id,
            "seq": self._next(), **fields}))
        return self

    def run_end(self, *, status: str = "ok", final_output: str | None = None) -> "TrajectoryBuilder":
        self._events.append(RunEndEvent(
            run_id=self._run_id, seq=self._next(), type=EventType.RUN_END,
            status=status, final_output=final_output))
        return self

    def build(self) -> Trajectory:
        return Trajectory.from_events(self._run_id, [self._start, *self._events])
```

> `src/harness/testing/__init__.py` 导出：`from harness.testing.builder import TrajectoryBuilder`。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/testing/ -v`
预期：9 passed

- [ ] **步骤 5：Commit**

```bash
git add src/harness/testing/ tests/testing/
git commit -m "feat(testing): TrajectoryBuilder published as part of the product"
```

---

### 任务 25：`TrajectoryMatcher`

**文件：**
- 创建：`src/harness/evaluators/trajectory_match.py`
- 创建：`tests/evaluators/test_trajectory_match.py`

> **两个正交维度**（设计文档 §4.1）：模式（`strict`/`unordered`/`subset`/`superset`/`in_order`）× 参数匹配（`exact`/`ignore`/`subset`/`superset`）。
> **`arg_normalizers` 是必须实现项而非可选项** —— 代码修复任务中命令串、路径、时间戳天然不确定，没有它整套匹配极其脆弱。

- [ ] **步骤 1：编写失败的测试**

```python
# tests/evaluators/test_trajectory_match.py
import pytest

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalStatus
from harness.evaluators.trajectory_match import TrajectoryMatcher
from harness.testing import TrajectoryBuilder as TB


def _traj(*tools: str):
    b = TB(run_id="r1").turn()
    for t in tools:
        b = b.llm_response(tool_calls=[(t, {})]).tool_result(name=t, content="ok", ok=True)
    return b.run_end().build()


def _run(traj, **kw):
    return TrajectoryMatcher(**kw).evaluate(traj, EvalContext())


def test_strict_requires_exact_sequence():
    assert _run(_traj("a", "b"), mode="strict", expected=[("a", {}), ("b", {})]).status is EvalStatus.PASS
    assert _run(_traj("b", "a"), mode="strict", expected=[("a", {}), ("b", {})]).status is EvalStatus.FAIL


def test_unordered_ignores_order_but_not_set():
    assert _run(_traj("b", "a"), mode="unordered",
                expected=[("a", {}), ("b", {})]).status is EvalStatus.PASS
    assert _run(_traj("a"), mode="unordered",
                expected=[("a", {}), ("b", {})]).status is EvalStatus.FAIL


def test_superset_allows_extra_exploration():
    """允许探索性多余调用 —— 代码修复任务最常见的情况。"""
    r = _run(_traj("search", "read", "write"), mode="superset",
             expected=[("read", {}), ("write", {})])
    assert r.status is EvalStatus.PASS


def test_subset_forbids_extra_calls():
    """白名单语义：禁止多余调用。"""
    r = _run(_traj("search", "read"), mode="subset", expected=[("read", {})])
    assert r.status is EvalStatus.FAIL
    assert r.findings[0].code == "trajectory.unexpected_tool"


def test_in_order_requires_ordered_subsequence():
    assert _run(_traj("search", "read", "write", "finish"), mode="in_order",
                expected=[("read", {}), ("write", {})]).status is EvalStatus.PASS
    assert _run(_traj("write", "read"), mode="in_order",
                expected=[("read", {}), ("write", {})]).status is EvalStatus.FAIL


def test_tool_recall_and_precision_are_reported():
    r = _run(_traj("a", "x"), mode="superset", expected=[("a", {}), ("b", {})])
    assert r.metrics["tool_recall"] == 0.5
    assert r.metrics["tool_precision"] == 0.5


def test_tool_args_match_mode_ignore_compares_names_only():
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("run_command", {"argv": ["pytest", "-q"]})])
            .tool_result(name="run_command", content="1 passed", ok=True)
            .run_end().build())
    r = TrajectoryMatcher(mode="strict", expected=[("run_command", {"argv": ["pytest", "-x"]})],
                          tool_args_match_mode="ignore").evaluate(traj, EvalContext())
    assert r.status is EvalStatus.PASS


def test_arg_normalizer_handles_nondeterministic_paths():
    """临时工作目录前缀必须能被归一化掉，否则 golden 永远匹配不上。"""
    traj = (TB(run_id="r1").turn()
            .llm_response(tool_calls=[("read_file", {"path": "/tmp/ws_abc123/a.py"})])
            .tool_result(name="read_file", content="x", ok=True)
            .run_end().build())
    r = TrajectoryMatcher(
        mode="strict", expected=[("read_file", {"path": "a.py"})],
        arg_normalizers={"read_file": lambda a: {"path": a["path"].rsplit("/", 1)[-1]}},
    ).evaluate(traj, EvalContext())
    assert r.status is EvalStatus.PASS


def test_empty_trajectory_does_not_crash():
    r = _run(TB(run_id="r1").build(), mode="strict", expected=[("a", {})])
    assert r.status in {EvalStatus.FAIL, EvalStatus.SKIPPED}
    assert r.status is not EvalStatus.ERROR


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="mode"):
        TrajectoryMatcher(mode="bogus", expected=[]).evaluate(TB(run_id="r1").build(), EvalContext())
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/evaluators/test_trajectory_match.py -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现**

```python
# src/harness/evaluators/trajectory_match.py
"""轨迹匹配评测器。

## 两个正交维度

维度 1 — 轨迹模式（mode）：
    strict     顺序与内容完全一致
    unordered  工具调用集合相同，顺序任意
    subset     actual ⊆ expected（白名单，禁止多余调用）
    superset   actual ⊇ expected（允许探索性多余调用）
    in_order   expected 是 actual 的有序子序列

维度 2 — 参数匹配（tool_args_match_mode）：
    exact(默认) / ignore / subset / superset

## arg_normalizers 是必须实现项
代码修复任务中命令串、文件路径、时间戳天然不确定。
没有 per-tool 的归一化，整套匹配会极其脆弱。
归一化函数与 golden 生成共用同一份代码（orchestration/golden.py::normalize_call），
避免「生成时归一化了、匹配时没归一化」的不一致。
"""
from __future__ import annotations

from typing import Any, Callable

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalResult, EvalStatus, Finding, Severity
from harness.evaluators.base import BaseEvaluator
from harness.events.trajectory import Trajectory
from harness.events.types import EventType

_MODES = {"strict", "unordered", "subset", "superset", "in_order"}
_ARG_MODES = {"exact", "ignore", "subset", "superset"}


class TrajectoryMatcher(BaseEvaluator):
    name = "TrajectoryMatcher"
    subscribes = frozenset({EventType.TOOL_CALL, EventType.RUN_END})

    def __init__(self, *, mode: str, expected: list[tuple[str, dict]],
                 tool_args_match_mode: str = "exact",
                 arg_normalizers: dict[str, Callable[[dict], dict]] | None = None) -> None:
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {sorted(_MODES)}, got {mode!r}")
        if tool_args_match_mode not in _ARG_MODES:
            raise ValueError(f"tool_args_match_mode must be one of {sorted(_ARG_MODES)}")
        self.mode = mode
        self.expected = expected
        self.arg_mode = tool_args_match_mode
        self.normalizers = arg_normalizers or {}

    # ---- 归一化 ----
    def _norm(self, name: str, args: dict) -> dict:
        fn = self.normalizers.get(name)
        return fn(args) if fn else args

    def _calls(self, traj: Trajectory) -> list[tuple[str, dict]]:
        return [(c.name, self._norm(c.name, c.arguments)) for c in traj.tool_calls()]

    def _args_match(self, a: dict, b: dict) -> bool:
        if self.arg_mode == "ignore":
            return True
        if self.arg_mode == "exact":
            return a == b
        if self.arg_mode == "subset":
            return all(b.get(k) == v for k, v in a.items())
        return all(a.get(k) == v for k, v in b.items())     # superset

    def _call_matches(self, actual: tuple[str, dict], exp: tuple[str, dict]) -> bool:
        name_a, args_a = actual
        name_e, args_e = exp
        return name_a == name_e and self._args_match(args_a, args_e)

    # ---- 各模式的判定 ----
    def _matches(self, actual: list, expected: list) -> tuple[bool, list[Finding]]:
        if self.mode == "strict":
            if len(actual) != len(expected):
                return False, [Finding(code="trajectory.length_mismatch", severity=Severity.MAJOR,
                                       message=f"expected {len(expected)} calls, got {len(actual)}")]
            bad = [i for i, (a, e) in enumerate(zip(actual, expected))
                   if not self._call_matches(a, e)]
            return (not bad), [Finding(code="trajectory.step_mismatch", severity=Severity.MAJOR,
                                       message=f"steps differ at {bad}")] if bad else []

        if self.mode == "unordered":
            pool = list(actual)
            missing = []
            for e in expected:
                hit = next((i for i, a in enumerate(pool) if self._call_matches(a, e)), None)
                if hit is None:
                    missing.append(e[0])
                else:
                    pool.pop(hit)
            return (not missing and not pool), (
                [Finding(code="trajectory.missing_tool" if missing else "trajectory.unexpected_tool",
                         message=f"missing={missing} extra={[p[0] for p in pool]}",
                         severity=Severity.MAJOR)] if (missing or pool) else [])

        if self.mode == "superset":
            pool = list(actual)
            missing = []
            for e in expected:
                hit = next((i for i, a in enumerate(pool) if self._call_matches(a, e)), None)
                if hit is None:
                    missing.append(e[0])
                else:
                    pool.pop(hit)
            return (not missing), ([Finding(code="trajectory.missing_tool", severity=Severity.MAJOR,
                                            message=f"missing required tools: {missing}")]
                                   if missing else [])

        if self.mode == "subset":
            extras = [a for a in actual
                      if not any(self._call_matches(a, e) for e in expected)]
            return (not extras), ([Finding(code="trajectory.unexpected_tool",
                                           severity=Severity.MAJOR,
                                           message=f"tools outside allowlist: {[x[0] for x in extras]}")]
                                  if extras else [])

        # in_order：expected 是 actual 的有序子序列
        it = iter(actual)
        for e in expected:
            if not any(self._call_matches(a, e) for a in it):
                return False, [Finding(code="trajectory.order_violation", severity=Severity.MAJOR,
                                       message=f"{e[0]!r} not found in order")]
        return True, []

    # ---- 主入口 ----
    def evaluate(self, traj: Trajectory, ctx: EvalContext) -> EvalResult:
        actual = self._calls(traj)
        expected = [(n, self._norm(n, a)) for n, a in self.expected]

        if not traj.tool_calls() and expected:
            return self.skipped(traj, "no tool calls in trajectory")

        ok, findings = self._matches(actual, expected)

        actual_names = [n for n, _ in actual]
        expected_names = [n for n, _ in expected]
        hits = sum(1 for n in expected_names if n in actual_names)
        recall = hits / len(expected_names) if expected_names else 1.0
        precision = (sum(1 for n in actual_names if n in expected_names) / len(actual_names)
                     if actual_names else 1.0)

        return EvalResult(
            evaluator=self.name, run_id=traj.run_id,
            status=EvalStatus.PASS if ok else EvalStatus.FAIL,
            score=1.0 if ok else 0.0,
            summary=f"{self.mode} match {'succeeded' if ok else 'failed'}",
            findings=findings,
            metrics={"tool_recall": recall, "tool_precision": precision,
                     "actual_calls": float(len(actual)),
                     "expected_calls": float(len(expected))})
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/evaluators/ -v`
预期：全部 passed

- [ ] **步骤 5：Commit**

```bash
git add src/harness/evaluators/trajectory_match.py tests/evaluators/test_trajectory_match.py
git commit -m "feat(evaluators): TrajectoryMatcher with 5 modes and arg normalizers"
```

---

### 任务 26：`EfficiencyAnalyzer`

**文件：**
- 创建：`src/harness/evaluators/efficiency.py`
- 创建：`tests/evaluators/test_efficiency.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/evaluators/test_efficiency.py
from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalStatus
from harness.evaluators.efficiency import EfficiencyAnalyzer
from harness.testing import TrajectoryBuilder as TB


def _traj(steps: list[str], *, optimal: int = 4):
    b = TB(run_id="r1").turn()
    for s in steps:
        b = b.llm_response(tool_calls=[(s, {"path": "a.py"})]).tool_result(
            name=s, content="ok", ok=True)
    return b.run_end().build()


def _run(traj, **kw):
    return EfficiencyAnalyzer(**kw).evaluate(traj, EvalContext())


def test_efficient_path_scores_high():
    r = _run(_traj(["read_file", "write_file", "run_command", "finish"]), optimal_steps=4)
    assert r.status is EvalStatus.PASS
    assert r.metrics["step_ratio"] == 1.0


def test_step_ratio_exceeds_one_when_wasteful():
    r = _run(_traj(["read_file"] * 12), optimal_steps=4)
    assert r.metrics["step_ratio"] == 3.0
    assert r.status is EvalStatus.WARN


def test_redundant_calls_are_counted():
    """同工具同参数重复调用 —— 循环检测的基础。"""
    r = _run(_traj(["read_file", "read_file", "read_file"]), optimal_steps=3)
    assert r.metrics["redundant_calls"] == 2


def test_redundant_calls_emit_finding():
    r = _run(_traj(["read_file"] * 5), optimal_steps=5)
    assert any(f.code == "efficiency.redundant_calls" for f in r.findings)


def test_different_args_are_not_redundant():
    b = TB(run_id="r1").turn()
    for p in ("a.py", "b.py", "c.py"):
        b = b.llm_response(tool_calls=[("read_file", {"path": p})]).tool_result(
            name="read_file", content="x", ok=True)
    r = EfficiencyAnalyzer(optimal_steps=3).evaluate(b.run_end().build(), EvalContext())
    assert r.metrics["redundant_calls"] == 0


def test_failed_tool_results_are_counted():
    b = (TB(run_id="r1").turn()
         .llm_response(tool_calls=[("f", {})]).tool_result(name="f", content="", ok=False,
                                                           error="boom", error_type="nonzero_exit")
         .run_end().build())
    r = EfficiencyAnalyzer(optimal_steps=1).evaluate(b, EvalContext())
    assert r.metrics["failed_tool_calls"] == 1


def test_empty_trajectory_does_not_crash():
    r = _run(TB(run_id="r1").build(), optimal_steps=4)
    assert r.status is not EvalStatus.ERROR


def test_zero_optimal_steps_does_not_divide_by_zero():
    r = _run(_traj(["a"]), optimal_steps=0)
    assert r.status is not EvalStatus.ERROR
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/evaluators/test_efficiency.py -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：编写实现**

```python
# src/harness/evaluators/efficiency.py
"""效率评测器 —— 纯规则，零 LLM 成本。

指标：步数比、冗余调用率、失败调用数、无效循环计数。
步数比 = 实际工具调用数 / 用例声明的 optimal_steps。
"""
from __future__ import annotations

import json
from collections import Counter

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalResult, EvalStatus, Finding, Severity
from harness.evaluators.base import BaseEvaluator
from harness.events.trajectory import Trajectory
from harness.events.types import EventType


class EfficiencyAnalyzer(BaseEvaluator):
    name = "EfficiencyAnalyzer"
    subscribes = frozenset({EventType.TOOL_CALL, EventType.RUN_END})

    def __init__(self, *, optimal_steps: int = 0, redundancy_tolerance: int = 3) -> None:
        self.optimal_steps = optimal_steps
        self.redundancy_tolerance = redundancy_tolerance

    def evaluate(self, traj: Trajectory, ctx: EvalContext) -> EvalResult:
        calls = traj.tool_calls()
        results = {r.call_id: r for r in traj.tool_results()}

        # 冗余：同工具 + 同参数的重复调用
        fingerprints = Counter(
            f"{c.name}:{json.dumps(c.arguments, sort_keys=True, default=str)}" for c in calls)
        redundant = sum(n - 1 for n in fingerprints.values() if n > 1)

        failed = sum(1 for r in results.values() if not r.ok)
        step_ratio = (len(calls) / self.optimal_steps) if self.optimal_steps else 0.0

        findings: list[Finding] = []
        if redundant >= self.redundancy_tolerance:      # 用配置值，不是模块常量
            findings.append(Finding(
                code="efficiency.redundant_calls", severity=Severity.MINOR,
                message=f"{redundant} redundant calls (same tool + same args)",
                data={"redundant": redundant, "total": len(calls)}))

        if self.optimal_steps and step_ratio > 2.0:
            findings.append(Finding(
                code="efficiency.excessive_steps", severity=Severity.MAJOR,
                message=f"used {len(calls)} steps vs {self.optimal_steps} optimal "
                        f"({step_ratio:.1f}x)",
                data={"step_ratio": step_ratio}))

        if failed and failed >= max(2, len(results) // 2):
            findings.append(Finding(
                code="efficiency.high_failure_rate", severity=Severity.MINOR,
                message=f"{failed}/{len(results)} tool calls failed",
                data={"failed": failed, "total": len(results)}))

        has_step_finding = any(f.code == "efficiency.excessive_steps" for f in findings)
        status = (EvalStatus.WARN if has_step_finding or findings else
                  EvalStatus.PASS)

        return EvalResult(
            evaluator=self.name, run_id=traj.run_id, status=status,
            score=(1.0 / step_ratio) if step_ratio else None,
            summary=f"{len(calls)} calls, ratio {step_ratio:.2f}, {redundant} redundant",
            findings=findings,
            metrics={"step_ratio": step_ratio, "tool_calls": float(len(calls)),
                     "redundant_calls": float(redundant),
                     "failed_tool_calls": float(failed)})
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/evaluators/ -v`
预期：8 passed

- [ ] **步骤 5：Part 2 收尾验证**

```bash
uv run pytest -v
uv run lint-imports
uv run pyright src/harness
```

预期：全绿；`lint-imports` 报告 `0 broken`，**特别是确认 `harness.evaluators` 不依赖 `harness.core`**。

- [ ] **步骤 6：Commit**

```bash
git add src/harness/evaluators/efficiency.py tests/evaluators/test_efficiency.py
git commit -m "feat(evaluators): EfficiencyAnalyzer with step ratio and redundancy metrics"
```

---

## Part 2 验收标准

- [ ] `uv run pytest` 全绿
- [ ] `uv run lint-imports` 报告 `0 broken`，`evaluators` 不依赖 `core`
- [ ] Provider 一致性套件覆盖 ≥2 个实现 × 8 个场景，且**不联网**
- [ ] `uv run harness run -s examples/hello.yaml --provider deepseek` 能跑通真实模型
- [ ] `--record` 产出 ResponsePool 文件，`--replay` 离线复跑结果一致
- [ ] 中间件管道顺序可断言：Permission → Sandbox → Budget → Telemetry → Policy → Executor
- [ ] 超预算 run 以 `budget_exceeded` 结束（不是 `llm_error`）
- [ ] 压缩发生时 `CONTEXT_COMPACT` 事件落盘
- [ ] `TrajectoryMatcher` 5 种模式 + `arg_normalizers` 全部可测
- [ ] 所有评测器测试**零 LLM 调用**（用 `TrajectoryBuilder` + `CannedJudge`）

**Part 2 完成后进入 [Part 3：编排、报告与双 Harness 对称](2026-09-14-agent-eval-harness-part3-orchestration.md)。**
