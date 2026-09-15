"""JSONL 轨迹存储测试。

JSONL 是**真相源** —— SQLite 索引只是查询层，坏了可以重建。
因此这里的重点不是性能，而是：并发 run 互不干扰、seq 严格单调、flush 语义可靠。
"""

from __future__ import annotations

import asyncio
import time

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


# ---- 并发 flush（M6 分享同一个 store 之后才暴露出来）----
async def test_flush_waits_for_an_in_flight_write(tmp_path, monkeypatch):
    """★ `flush()` 返回时，数据必须已经在磁盘上。

    这是 `Run.execute` 结尾 `store.get(run_id)` 能工作的前提 —— 它内部先 flush 再读文件。

    没修之前：第一个 flush 换完缓冲区（`_dirty` 已置 False）就去写盘，
    第二个 flush 一看 `_dirty` 是 False **立刻返回**，而写盘还在飞 ——
    于是 `get()` 撞上 `KeyError: no trajectory for run ...`。
    并发跑 suite 时它以随机几条 case 的形式出现，单 run 永远复现不了。
    """
    store = JsonlStore(tmp_path)
    await store.append(_start("r1"))

    real = JsonlStore._append_lines
    started = asyncio.Event()

    def slow(path, lines):  # type: ignore[no-untyped-def]
        started.set()
        time.sleep(0.05)  # 模拟一次慢写盘
        real(path, lines)

    monkeypatch.setattr(JsonlStore, "_append_lines", staticmethod(slow))

    first = asyncio.create_task(store.flush())
    await asyncio.wait_for(started.wait(), timeout=1)  # 确认第一次 flush 真的在写
    await store.flush()  # 第二次 —— 必须等到第一次写完才返回

    assert (tmp_path / "r1.jsonl").exists(), "flush() 在写盘完成前就返回了"
    await first  # 收尾，避免留下未 await 的任务


async def test_concurrent_flushes_never_corrupt_a_trajectory(tmp_path):
    """同一个 store 被多路 run 共享时，flush 会并发发生 —— 文件不能出现半行。

    ⚠️ **这是冒烟测试，不是证明。** 实测把写锁去掉后它依然通过（写盘太快，
    交错窗口极小，抓不到）。真正卡住这个 bug 的是上一条
    `test_flush_waits_for_an_in_flight_write` —— 它用一个人为的慢写盘
    把窗口撑开，去锁后**必然**失败。这条留着是因为它跑真实并发路径，
    能在回归时提供额外信号，但不能拿它当保障。
    """
    store = JsonlStore(tmp_path)

    async def worker(n: int) -> None:
        for i in range(20):
            await store.append(_start(f"r{n}", seq=i))
            if i % 2 == 0:
                await store.flush()

    await asyncio.gather(*(worker(n) for n in range(4)))
    await store.flush()

    for n in range(4):
        text = (tmp_path / f"r{n}.jsonl").read_text(encoding="utf-8")
        assert len(text.splitlines()) == 20, f"r{n} 的行数不对"
        # 解析不抛即证明没有半行/交错
        from harness.events.trajectory import Trajectory

        assert len(Trajectory.from_jsonl(text).events) == 20


async def test_get_never_raises_for_an_append_that_already_happened(tmp_path):
    """已 append 的事件，`get()` 必须能读到 —— 并发 flush 下也不能漏。"""
    store = JsonlStore(tmp_path)

    async def worker(n: int) -> None:
        for i in range(10):
            await store.append(_start(f"r{n}", seq=i))
            await store.get(f"r{n}")  # 每次写完立刻读回，模拟 Run.execute 结尾

    await asyncio.gather(*(worker(n) for n in range(6)))
