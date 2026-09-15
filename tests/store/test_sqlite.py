"""SQLite 索引层测试。

## 三个必测点

1. **WAL 模式** —— 没有它，并发读会阻塞写，8 路 run 立刻退化成串行。
2. **20 路并发不出现 `database is locked`** —— 单写者队列的核心价值。
3. **`close()` 必须先 flush** —— Windows 上句柄不能带着未完成的写入关闭，
   否则 `PermissionError: [WinError 32]`，且症状是"文件删不掉"而非"数据丢了"。
"""

from __future__ import annotations

import asyncio
import threading
import time

from harness.contracts.results import EvalResult, EvalStatus
from harness.events.types import EventType, RunEndEvent, RunStartEvent
from harness.store.composite import CompositeStore
from harness.store.sqlite import SqliteIndex


def _start(rid: str, seq: int = 0) -> RunStartEvent:
    return RunStartEvent(run_id=rid, seq=seq, type=EventType.RUN_START,
                         role="sut", model="m", provider="fake")


def _end(rid: str, seq: int = 1) -> RunEndEvent:
    return RunEndEvent(run_id=rid, seq=seq, type=EventType.RUN_END,
                       status="ok", turns=3, tool_calls=4, cost_usd=0.02)


async def test_index_records_run_row(tmp_path):
    idx = SqliteIndex(tmp_path / "i.db")
    await idx.append(_start("r1"))
    await idx.append(_end("r1"))
    await idx.flush()
    rows = await idx.query_runs()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["turns"] == 3
    await idx.close()


async def test_run_row_starts_as_running(tmp_path):
    """Run 还没结束时索引里就该有一行 —— 否则看板上"正在跑的 run"是隐形的。"""
    idx = SqliteIndex(tmp_path / "i.db")
    await idx.append(_start("r1"))
    await idx.flush()
    assert (await idx.query_runs())[0]["status"] == "running"
    await idx.close()


async def test_wal_mode_is_enabled(tmp_path):
    idx = SqliteIndex(tmp_path / "i.db")
    assert await idx.journal_mode() == "wal"
    await idx.close()


async def test_concurrent_writes_do_not_lock(tmp_path):
    """20 路并发写入不得出现 database is locked。"""
    idx = SqliteIndex(tmp_path / "i.db")

    async def one(n: int) -> None:
        for i in range(10):
            await idx.append(_start(f"r{n}", seq=i))

    await asyncio.gather(*(one(n) for n in range(20)))
    await idx.flush()
    assert len(await idx.query_runs()) == 20
    await idx.close()


async def test_reads_and_writes_never_overlap(tmp_path, monkeypatch):
    """读与写共用一条连接，必须严格串行 —— 探针断言二者从不重叠。

    为什么光靠 `flush()` 不够：它只保证**已入队**的写落盘。`flush()` 返回之后、
    SELECT 执行之前，新的 `append` 完全可能入队并被 writer 线程开始 apply ——
    那个窗口里两个线程同时碰一条连接，轻则读到未提交的数据，重则驱动抛错。
    所以读也走同一把 `_io_lock`。

    ## 探针是怎么来的（以及它的局限）

    最初这条测试只断言「并发读写不报错」，把读路径的锁整个删掉依然全绿 ——
    什么都没证明。改成探针后实测：

        有锁    写 23  读 40  重叠 0
        去锁    写/读区间相互嵌套，连按线程配对的记账本身都崩溃

    坦白说：去锁那一侧不是被 `assert` 抓住的，是观测本身先失效了。
    所以这条测试是**绊线**而非证明 —— 它锁住的是当前实现"0 重叠"这个已测事实，
    真正的推理在上面的注释里。

    ## 两个容易写错的地方

    - **读窗口也要拉长**。只在写侧延时的话，读的窗口只有一次 SELECT 的几微秒，
      重叠几乎不可能被观测到，探针就成了摆设。
    - **写者必须散布在整个测试期间**。写者一口气跑完的话，剩下的读操作
      根本没有写可重叠，同样是摆设。
    """
    idx = SqliteIndex(tmp_path / "i.db")

    active = {"W": 0, "R": 0}
    guard = threading.Lock()
    seen = {"overlap": False}

    def enter(kind: str) -> None:
        with guard:
            active[kind] += 1
            # 任一时刻同时有读和写在进行 → 违规
            if active["W"] and active["R"]:
                seen["overlap"] = True

    def leave(kind: str) -> None:
        with guard:
            active[kind] -= 1

    real_apply, real_read = SqliteIndex._apply, SqliteIndex._read

    def probing_apply(self, batch):  # type: ignore[no-untyped-def]
        enter("W")
        try:
            time.sleep(0.002)
            real_apply(self, batch)
        finally:
            leave("W")

    def probing_read(self, sql, args):  # type: ignore[no-untyped-def]
        enter("R")
        try:
            time.sleep(0.002)
            return real_read(self, sql, args)
        finally:
            leave("R")

    monkeypatch.setattr(SqliteIndex, "_apply", probing_apply)
    monkeypatch.setattr(SqliteIndex, "_read", probing_read)

    async def writer(n: int) -> None:
        for i in range(30):
            await idx.append(_start(f"w{n}", seq=i))
            await asyncio.sleep(0.001)

    async def reader() -> None:
        for _ in range(20):
            await idx.query_runs()

    await asyncio.gather(*(writer(n) for n in range(3)), reader(), reader())
    await idx.close()
    assert seen["overlap"] is False, "读与写在同一连接上重叠了"


async def test_eval_results_roundtrip(tmp_path):
    idx = SqliteIndex(tmp_path / "i.db")
    await idx.put_evals("r1", [EvalResult(evaluator="EfficiencyAnalyzer", run_id="r1",
                                          status=EvalStatus.PASS, score=0.8)])
    await idx.flush()
    got = await idx.get_evals("r1")
    assert got[0].evaluator == "EfficiencyAnalyzer"
    assert got[0].score == 0.8
    await idx.close()


async def test_eval_results_survive_a_reopen(tmp_path):
    """索引是派生数据，但**落盘必须真的落盘** —— 重开后报告还能读出来。"""
    idx = SqliteIndex(tmp_path / "i.db")
    await idx.put_evals("r1", [EvalResult(evaluator="X", run_id="r1",
                                          status=EvalStatus.FAIL, score=0.0)])
    await idx.close()

    idx2 = SqliteIndex(tmp_path / "i.db")
    assert (await idx2.get_evals("r1"))[0].status is EvalStatus.FAIL
    await idx2.close()


async def test_unknown_run_has_no_evals(tmp_path):
    idx = SqliteIndex(tmp_path / "i.db")
    assert await idx.get_evals("nope") == []
    await idx.close()


async def test_close_flushes_before_closing_handle(tmp_path):
    """Windows 上 SQLite 文件句柄不能被并发关闭 —— close 必须先 flush。"""
    idx = SqliteIndex(tmp_path / "i.db")
    await idx.append(_start("r1"))
    await idx.close()  # 不显式 flush 也能正确落盘
    idx2 = SqliteIndex(tmp_path / "i.db")
    assert len(await idx2.query_runs()) == 1
    await idx2.close()


async def test_close_is_idempotent(tmp_path):
    idx = SqliteIndex(tmp_path / "i.db")
    await idx.append(_start("r1"))
    await idx.close()
    await idx.close()


async def test_a_second_connection_sees_committed_rows(tmp_path):
    """写入必须 commit，不能留在未结束的事务里。"""
    a = SqliteIndex(tmp_path / "i.db")
    await a.append(_start("r1"))
    await a.flush()
    b = SqliteIndex(tmp_path / "i.db")
    assert len(await b.query_runs()) == 1
    await a.close()
    await b.close()


# ---- Composite ----
async def test_composite_writes_both_jsonl_and_index(tmp_path):
    store = CompositeStore(root=tmp_path)
    await store.append(_start("r1"))
    await store.close()
    assert (tmp_path / "r1.jsonl").exists()

    reopened = CompositeStore(root=tmp_path)
    assert (await reopened.query_runs())[0]["run_id"] == "r1"
    await reopened.close()


async def test_composite_serves_trajectories_from_jsonl(tmp_path):
    """轨迹从 JSONL 读（真相源），不从索引重建。"""
    store = CompositeStore(root=tmp_path)
    await store.append(_start("r1"))
    await store.append(_end("r1"))
    traj = await store.get("r1")
    assert traj.run_id == "r1"
    assert traj.events[-1].type is EventType.RUN_END
    await store.close()


async def test_composite_persists_evals_to_the_index(tmp_path):
    """评测结果落索引 —— M8 的聚合报告靠它读回。"""
    store = CompositeStore(root=tmp_path)
    await store.put_evals("r1", [EvalResult(evaluator="E", run_id="r1",
                                            status=EvalStatus.PASS, score=1.0)])
    await store.close()

    reopened = CompositeStore(root=tmp_path)
    assert (await reopened.get_evals("r1"))[0].score == 1.0
    await reopened.close()
