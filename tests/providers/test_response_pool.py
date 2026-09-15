"""响应采样池与录制/回放装饰器测试。

## 概念澄清（容易混淆，值得先说清）

`ResponsePool` **不是 HTTP cassette**。它是 **LLM 响应采样池**，
为 `MetaEvaluator` 的 judge consistency 提供「同一请求返回 N 个不同样本」的能力。

HTTP 层的录制回放（防上游 API 漂移）交给 `vcrpy`，放在 `tests/fixtures/cassettes/`。
两者是独立关注点 —— vcrpy 的 `allow_playback_repeats` 只能重复**同一个**响应，
给不出 N 个**不同**样本，所以这件事必须自己做。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.contracts.protocols import LLMRequest, Message
from harness.providers.fake import FakeProvider, text_response
from harness.providers.recording import RecordingProvider, ReplayProvider
from harness.providers.response_pool import ResponsePool


def _req(text: str = "hi") -> LLMRequest:
    return LLMRequest(model="m", messages=[Message.user_text(text)])


def _sample(text: str) -> dict:
    return {
        "model": "m", "content": [{"type": "text", "text": text}], "text": text,
        "tool_calls": [], "finish_reason": "stop",
        "usage": {"input_tokens": 1, "output_tokens": 1, "calls": 1},
        "latency_ms": 0, "raw": {},
    }


# ---- 采样池 ----
def test_same_request_yields_distinct_samples(tmp_path: Path):
    """★ judge consistency 的核心需求：同请求返回**不同**样本。

    vcrpy 做不到这件事 —— 这是自研这个类的原因。
    """
    pool = ResponsePool(tmp_path / "pool.json")
    for t in ("a", "b", "c"):
        pool.record(_req(), _sample(t))
    got = [pool.replay(_req(), occurrence=i)["text"] for i in range(3)]
    assert got == ["a", "b", "c"]


def test_occurrence_wraps_around_when_exhausted(tmp_path: Path):
    pool = ResponsePool(tmp_path / "pool.json")
    pool.record(_req(), _sample("only"))
    assert pool.replay(_req(), occurrence=5)["text"] == "only"


def test_different_requests_get_separate_slots(tmp_path: Path):
    pool = ResponsePool(tmp_path / "pool.json")
    pool.record(_req("a"), _sample("for-a"))
    pool.record(_req("b"), _sample("for-b"))
    assert pool.replay(_req("a"), occurrence=0)["text"] == "for-a"
    assert pool.replay(_req("b"), occurrence=0)["text"] == "for-b"


def test_key_is_deterministic():
    """Key 必须可复现，否则录制与回放对不上（跨进程、跨 run 都要对得上）。"""
    assert ResponsePool.key_of(_req(), "m") == ResponsePool.key_of(_req(), "m")


def test_key_changes_with_model():
    assert ResponsePool.key_of(_req(), "m1") != ResponsePool.key_of(_req(), "m2")


def test_key_changes_with_temperature():
    """温度影响采样 —— 必须进 key，否则 consistency 测量会取错样本。"""
    cold = LLMRequest(model="m", messages=[Message.user_text("hi")], temperature=0.0)
    hot = LLMRequest(model="m", messages=[Message.user_text("hi")], temperature=1.0)
    assert ResponsePool.key_of(cold, "m") != ResponsePool.key_of(hot, "m")


def test_missing_key_raises_with_actionable_message(tmp_path: Path):
    pool = ResponsePool(tmp_path / "pool.json")
    with pytest.raises(KeyError, match="not recorded"):
        pool.replay(_req(), occurrence=0)


def test_occurrence_count(tmp_path: Path):
    pool = ResponsePool(tmp_path / "pool.json")
    assert pool.occurrence_count(_req()) == 0
    pool.record(_req(), _sample("x"))
    assert pool.occurrence_count(_req()) == 1


def test_save_and_reload_roundtrip(tmp_path: Path):
    path = tmp_path / "pool.json"
    pool = ResponsePool(path)
    pool.record(_req(), _sample("persisted"))
    pool.save()

    assert ResponsePool(path).replay(_req(), occurrence=0)["text"] == "persisted"


def test_empty_pool_starts_clean(tmp_path: Path):
    assert ResponsePool(tmp_path / "absent.json").occurrence_count(_req()) == 0


# ---- 装饰器 ----
async def test_recording_provider_passes_through_and_records(tmp_path: Path):
    pool = ResponsePool(tmp_path / "pool.json")
    p = RecordingProvider(FakeProvider([text_response("real")]), pool)
    assert (await p.complete(_req())).text == "real"
    assert pool.replay(_req(), occurrence=0)["text"] == "real"


async def test_replay_provider_serves_from_the_pool(tmp_path: Path):
    pool = ResponsePool(tmp_path / "pool.json")
    pool.record(_req(), _sample("canned"))
    assert (await ReplayProvider(pool).complete(_req())).text == "canned"


async def test_replay_provider_raises_on_cache_miss_by_default(tmp_path: Path):
    """默认必须抛错 —— 静默返回空响应会让评测结果无声地错掉。"""
    p = ReplayProvider(ResponsePool(tmp_path / "pool.json"))
    with pytest.raises(KeyError, match="not recorded"):
        await p.complete(_req())


async def test_replay_provider_can_fall_back_when_asked(tmp_path: Path):
    """显式传 fallback 时才降级 —— 默认保守，需要时才放宽。"""
    p = ReplayProvider(ResponsePool(tmp_path / "pool.json"),
                       fallback=FakeProvider([text_response("live")]))
    assert (await p.complete(_req())).text == "live"


async def test_replay_advances_through_samples_on_repeated_calls(tmp_path: Path):
    """连续调用同一个请求应依次取不同样本 —— 这是 consistency 测量的机制。"""
    pool = ResponsePool(tmp_path / "pool.json")
    for t in ("a", "b"):
        pool.record(_req(), _sample(t))
    p = ReplayProvider(pool)
    assert (await p.complete(_req())).text == "a"
    assert (await p.complete(_req())).text == "b"


async def test_recording_roundtrip_preserves_usage(tmp_path: Path):
    """录制 → 落盘 → 重新加载 → 回放，用量必须完整保留。

    `save()` 是显式的（而非每次调用自动写盘）—— 调用方控制持久化时机，
    避免每条响应都做一次 I/O。
    """
    path = tmp_path / "pool.json"
    rec = RecordingProvider(FakeProvider([text_response("x")]), ResponsePool(path))
    await rec.complete(_req())
    rec.save()

    replayed = await ReplayProvider(ResponsePool(path)).complete(_req())
    assert replayed.usage.input_tokens == 10
    assert replayed.usage.calls == 1
    assert replayed.text == "x"
