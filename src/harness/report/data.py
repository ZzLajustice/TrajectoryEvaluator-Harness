"""组装报告输入。

## 为什么读两个来源

  - **聚合指标与 case 明细** ← `<runs>/latest.json` 快照。它是 case 级的，
    flaky 这类概念只在那里表达得出来。
  - **失败模式分布** ← SQLite 索引里的 `eval_results`。逐条 `Finding` 存在
    评测结果里，快照没有它。这也正是 M6 说的「report 从 SQLite 读回」——
    报告不必重跑评测。

两条来源互为补充，都不需要重跑任何东西。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

from harness.store.composite import CompositeStore
from harness.store.layout import INDEX_NAME, SNAPSHOT_NAME
from harness.store.snapshot import read_snapshot

# 图表最多画多少条失败模式 —— 再多图就没法看了，表格仍然给全
_TOP_MODES = 12


def latest_snapshot_path(runs_dir: Path | str) -> Path:
    return Path(runs_dir) / SNAPSHOT_NAME


def collect(runs_dir: Path | str, *, snapshot: Path | str | None = None) -> dict[str, Any]:
    """读取快照 + 索引，产出 `render_report` 需要的字典。"""
    runs_dir = Path(runs_dir)
    snap_path = Path(snapshot) if snapshot else latest_snapshot_path(runs_dir)
    snap = read_snapshot(snap_path)

    agg = snap.get("aggregate") or {}
    cases = snap.get("runs") or []

    return {
        "suite_name": snap.get("suite_name") or "",
        "aggregate": agg,
        "cases": cases,
        "flaky": agg.get("flaky_cases") or [],
        "failure_modes": _failure_modes(runs_dir),
        "cost_quality": [
            {"case_id": c["case_id"], "cost": c.get("cost_usd") or 0.0,
             "pass_rate": c.get("pass_rate") or 0.0}
            for c in cases
        ],
        # judge 可靠性属 M10；没有真数据时留 None，报告不摆空壳
        "judge": snap.get("judge"),
    }


def _failure_modes(runs_dir: Path) -> dict[str, int]:
    """从索引里数每个失败模式命中了几次。

    索引不存在时返回空字典而不是报错 —— 只跑了 agent 没跑评测
    （`--evaluate` 没开）是**合法状态**，不该让报告命令直接失败。
    """
    if not (runs_dir / INDEX_NAME).exists():
        return {}

    async def read() -> Counter[str]:
        store = CompositeStore(root=runs_dir)
        counter: Counter[str] = Counter()
        try:
            for row in await store.query_runs():
                for ev in await store.get_evals(row["run_id"]):
                    for finding in ev.findings:
                        if finding.category:
                            counter[finding.category] += 1
        finally:
            await store.close()
        return counter

    return dict(asyncio.run(read()))


def top_failure_modes(modes: dict[str, int], limit: int = _TOP_MODES) -> dict[str, int]:
    """按命中次数降序取前 N 个 —— 图的横轴放不下几十个模式。"""
    ordered = sorted(modes.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(ordered[:limit])
