"""JSONL 轨迹存储 —— 真相源。

## 为什么是真相源

SQLite 索引是**派生数据**，坏了可以重建；JSONL 是原始记录，丢了就没了。
因此这里的每一行都必须忠实、完整、可独立解析。

## 两个设计选择

1. **`append()` 非阻塞**（只入内存队列），后台批量落盘。
   评测的常态是 `--concurrency 8`，同步写会让 8 路 run 在 store 上互锁。

2. **`seq` 严格单调检查在写入前做。**
   重复或倒退的 seq 意味着上游的计数器有 bug —— 这种错误在 JSONL 里
   极难事后发现（读回来看着都正常），必须写入时就拒绝。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from harness.events.trajectory import Trajectory
from harness.events.types import dump_event


class JsonlStore:
    """每 run 一个 `<run_id>.jsonl` 文件，追加写。"""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._buffers: dict[str, list[str]] = {}
        self._last_seq: dict[str, int] = {}
        self._lock = asyncio.Lock()
        # 落盘串行化。与 `_lock` 分开是刻意的：`append` 只需 `_lock`
        # （换缓冲区是内存操作，必须快），而真正的写盘由 `_write_lock` 排队。
        self._write_lock = asyncio.Lock()
        self._dirty = False

    async def append(self, event: Any) -> None:
        """入内存队列即返回。seq 检查在临界区内做，避免并发下漏检。"""
        async with self._lock:
            rid = event.run_id
            last = self._last_seq.get(rid)
            if last is not None and event.seq <= last:
                raise ValueError(
                    f"non-monotonic seq for run {rid}: got {event.seq}, last was {last}. "
                    "seq must be strictly increasing — a duplicate means the counter upstream is buggy."
                )
            self._last_seq[rid] = event.seq
            line = json.dumps(dump_event(event), ensure_ascii=False)
            self._buffers.setdefault(rid, []).append(line)
            self._dirty = True

    async def flush(self) -> None:
        """把队列里的内容落盘。幂等。

        ## 为什么整个函数都要持 `_write_lock`

        并发 suite（`--concurrency 8`）共享同一个 store，flush 会被多个 run 同时调用。
        换缓冲区在锁内、写盘在锁外的话有两个后果，且**都是静默的**：

        1. 两个 flush 可能同时往同一个 `<run_id>.jsonl` 写 —— 两个 `open("a")`
           句柄交错写，文件里出现半行 JSON。症状是读回来时 `JSONDecodeError`。
        2. `flush()` 可能在别人的写还没落地时就返回（它只看 `_dirty`，
           而别人已经清掉了）。`Run.execute` 结尾 `store.get(run_id)` 会先 flush 再读，
           于是拿到 `KeyError: no trajectory for run ...`。

        两种症状都只在并发下出现，单 run 永远复现不了 —— M6 把它们暴露出来，
        正是因为第一次出现了共享 store 的多路 run。
        """
        async with self._write_lock:
            async with self._lock:
                if not self._dirty:
                    return
                pending, self._buffers, self._dirty = self._buffers, {}, False
            for rid, lines in pending.items():
                await asyncio.to_thread(
                    self._append_lines, self._root / f"{rid}.jsonl", lines
                )

    @staticmethod
    def _append_lines(path: Path, lines: list[str]) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(line + "\n" for line in lines))

    async def get(self, run_id: str) -> Trajectory:
        """隐式 flush —— 调用方无需记得先 flush（极易漏掉）。"""
        await self.flush()
        path = self._root / f"{run_id}.jsonl"
        if not path.exists():
            raise KeyError(f"no trajectory for run {run_id!r} under {self._root}")
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return Trajectory.from_jsonl(text)

    async def close(self) -> None:
        """先 flush 再返回 —— Windows 上句柄不能带着未完成的写入关闭。"""
        await self.flush()
