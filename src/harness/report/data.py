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
    snap_path = Path(snapshot) if snapshot else latest_snapshot_path(Path(runs_dir))
    # ★ 面板数据必须与快照**同源**。
    #
    # 曾经这里是 `runs_dir = Path(runs_dir)`，于是
    # `harness report --snapshot <别的目录>/latest.json` 只换了快照，
    # 失败模式与 judge 面板仍然从 `--runs` 读 ——报告会把 **A 目录的图表**
    # 配上 **B 目录的指标**。两边都是真数据，所以没有一处会报错，
    # 也没有一处看起来可疑；只有把报告**打开**才看得出来（见 known-gaps §1.2）。
    #
    # 指向快照所在目录是唯一说得通的选择：用户说"这份报告讲的是这次 run"，
    # 而快照就是那次 run 的身份证。
    runs_dir = snap_path.parent
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
        # judge 可靠性来自 MetaEvaluator 的结果（索引里读回）。
        # 没启用 judge 时是 None，报告不摆空壳 —— 见 _judge_panel。
        "judge": _judge_panel(runs_dir),
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


def _judge_panel(runs_dir: Path) -> dict[str, Any] | None:
    """Judge 可靠性面板 —— 从索引里 MetaEvaluator 的结果汇总。

    **没有 judge 时返回 None**，报告就不渲染这一节。
    摆一个全 0 的空面板比不摆更糟：0 分看起来像"judge 很差"，
    而实际是"这次没启用 judge"。
    """
    if not (runs_dir / INDEX_NAME).exists():
        return None

    async def read() -> list[Any]:
        store = CompositeStore(root=runs_dir)
        out: list[Any] = []
        try:
            for row in await store.query_runs():
                for ev in await store.get_evals(row["run_id"]):
                    if ev.evaluator == "MetaEvaluator":
                        out.append(ev)
        finally:
            await store.close()
        return out

    results = asyncio.run(read())
    if not results:
        return None

    def _mean(key: str) -> float | None:
        # 缺席 = 不适用（只判一次时算不出一致性）。混进均值会凭空拉低它。
        values = [ev.metrics[key] for ev in results if key in ev.metrics]
        return round(sum(values) / len(values), 4) if values else None

    panel: dict[str, Any] = {
        # 数的是**元评测结果**条数，不是 judge run 条数 —— 一次元评测
        # 通常汇总了 N 条 judge run（N = judge_repeat）。
        # 键名说准：叫 judge_runs 会让人把它当成"判了几次"。
        "meta_evaluations": float(len(results)),
        "judge_cost_usd": round(sum(ev.metrics.get("judge_cost_usd", 0.0)
                                    for ev in results), 6),
        "judge_tokens": sum(ev.metrics.get("judge_tokens", 0.0) for ev in results),
    }
    for key in ("judge_consistency", "injection_resistance"):
        value = _mean(key)
        if value is not None:
            panel[key] = value
    return panel


def top_failure_modes(modes: dict[str, int], limit: int = _TOP_MODES) -> dict[str, int]:
    """按命中次数降序取前 N 个 —— 图的横轴放不下几十个模式。"""
    ordered = sorted(modes.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(ordered[:limit])
