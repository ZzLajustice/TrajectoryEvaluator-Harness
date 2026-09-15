"""JSONL 轨迹存储测试。

JSONL 是**真相源** —— SQLite 索引只是查询层，坏了可以重建。
因此这里的重点不是性能，而是：并发 run 互不干扰、seq 严格单调、flush 语义可靠。
"""

from __future__ import annotations

import asyncio

import pytest

from harness.events.types import EventType, RunStartEvent, ToolCallEvent
from harness.store.jsonl import JsonlStore


def _start(rid: str, seq: int = 0) -> RunStartEvent:
    return RunStartEvent(run_id=rid, seq=seq, type=EventType.RUN_START,
                         role="sut", model="m", provider="fake")


async def test_append_and_get_roundtrip(tmp_path):
    store = JsonlStore(tmp_path)
    await store.append(_start("r1", 0))
    await store.append(ToolCallEvent(run_id="r1", seq=1, type=EventType.TOOL_CALL,
                                     call_id="c1", name="f", arguments={}))
    await store.flush()

    traj = await store.get("r1")
    assert traj.tool_sequence() == ("f",)
    await store.close()


async def test_seq_must_be_strictly_increasing(tmp_path):
    store = JsonlStore(tmp_path)
    await store.append(_start("r1", 0))
    with pytest.raises(ValueError, match="non-monotonic"):
        await store.append(_start("r1", 0))
    await store.close()


async def test_seq_may_skip_forward(tmp_path):
    """只要求单调，不要求连续 —— 压缩事件可能跳过 seq。"""
    store = JsonlStore(tmp_path)
    await store.append(_start("r1", 0))
    await store.append(_start("r1", 10))
    await store.flush()
    assert len((await store.get("r1")).events) == 2
    await store.close()


async def test_file_is_written_only_after_flush(tmp_path):
    store = JsonlStore(tmp_path)
    await store.append(_start("r1"))
    assert not (tmp_path / "r1.jsonl").exists(), "append 应只入内存队列"
    await store.flush()
    assert (tmp_path / "r1.jsonl").exists()
    await store.close()


async def test_get_flushes_implicitly(tmp_path):
    """Get 之前不需要显式 flush —— 否则调用方极易漏掉。"""
    store = JsonlStore(tmp_path)
    await store.append(_start("r1"))
    assert (await store.get("r1")).run_id == "r1"
    await store.close()


async def test_get_unknown_run_raises_keyerror(tmp_path):
    store = JsonlStore(tmp_path)
    with pytest.raises(KeyError):
        await store.get("nope")
    await store.close()


async def test_concurrent_runs_do_not_interfere(tmp_path):
    """并发是评测的常态（--concurrency 8）。各 run 的 seq 必须独立单调。"""
    store = JsonlStore(tmp_path)

    async def one(n: int) -> None:
        for i in range(20):
            await store.append(_start(f"r{n}", seq=i))

    await asyncio.gather(*(one(n) for n in range(8)))
    await store.flush()
    for n in range(8):
        assert len((await store.get(f"r{n}")).events) == 20
    await store.close()


async def test_flush_is_idempotent(tmp_path):
    store = JsonlStore(tmp_path)
    await store.append(_start("r1"))
    await store.flush()
    await store.flush()
    assert len((await store.get("r1")).events) == 1
    await store.close()


async def test_close_flushes_pending_events(tmp_path):
    """Close 必须先把待写事件落盘 —— Windows 上句柄不能带未完成写入关闭。"""
    store = JsonlStore(tmp_path)
    await store.append(_start("r1"))
    await store.close()
    assert (tmp_path / "r1.jsonl").exists()


async def test_appends_accumulate_across_flushes(tmp_path):
    store = JsonlStore(tmp_path)
    await store.append(_start("r1", 0))
    await store.flush()
    await store.append(_start("r1", 1))
    await store.flush()
    assert len((await store.get("r1")).events) == 2
    await store.close()
