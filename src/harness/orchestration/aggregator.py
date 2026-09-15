"""结果聚合与快照。

## 指标语义（必须分列，绝不合并成单一"总分"）

    pass_rate           所有 repeat 都通过
    pass@k              k 次中至少一次通过
    flaky_rate          通过率严格落在 (0,1) 之间的 case 占比
    golden_score_mean   过程分（轨迹匹配），**与 outcome 分列**

合并成一个总分会让过程评测的意义被 outcome 淹没 —— 那正是本项目要反对的。
flaky case 显式列出，不平均掉。

## `pass@k` 的语义在此固定下来

本项目里它是**「至少一次通过的 case 占全部 case 的比例」**，
不是经典 pass@k 那个无偏估计量。报告要回答的是「有多少用例存在可行解」，
而不是「采样 k 次的期望通过率」。名字沿用业界叫法，语义在报告脚注写明 ——
不写就会有歧义，而歧义的指标比没有指标更糟。

## `flaky` 是 case 级概念，不属于 `RunStatus`

`RunStatus` 描述**一条 run 的终态**；单条 run 永远不会"flaky"。
flaky 只有在把一个 case 的多次 repeat 放在一起看时才成立。
所以它只出现在本模块产出的 case 级快照里。

## 快照：diff 与 ci 的输入

`write_snapshot` 产出的 JSON 是 `diff_runs` 的输入格式。它刻意是 **case 级**的
（一 case 一行），而不是 run 级 —— 否则 flaky 表达不出来，diff 也就无法区分
「变差了」和「一直不稳定」。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.contracts.spec import RunStatus
from harness.store.snapshot import SnapshotError, read_snapshot, write_snapshot_dict

# 过程分的来源。刻意只认轨迹匹配 —— 把效率分混进来会让 golden_score 语义失焦
# （效率分的 1/step_ratio 与"是否走对了路"根本不是一回事）。
_GOLDEN_EVALUATOR = "TrajectoryMatcher"

# case 级状态（diff 的三态）
CASE_OK = "ok"
CASE_FAIL = "fail"
CASE_FLAKY = "flaky"

SNAPSHOT_VERSION = 1


@dataclass
class CaseOutcome:
    """一个 case 及其全部 repeat 的结果。"""

    case_id: str
    statuses: list[RunStatus]
    golden_score: float | None = None
    cost_usd: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    tier: str = "medium"
    spec_fingerprint: str | None = None

    @property
    def successes(self) -> int:
        return sum(1 for s in self.statuses if s is RunStatus.OK)

    @property
    def pass_rate(self) -> float:
        return self.successes / len(self.statuses) if self.statuses else 0.0

    @property
    def case_status(self) -> str:
        """Case 级三态。判据与 `flaky_rate` 用的是同一条边界。"""
        if not self.statuses:
            return CASE_FAIL
        if self.successes == len(self.statuses):
            return CASE_OK
        if self.successes == 0:
            # 稳定的失败比 flaky 好办得多 —— 混进 flaky 会掩盖真正的不确定性
            return CASE_FAIL
        return CASE_FLAKY


def aggregate(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """把 case 级结果聚合成报告指标。"""
    n = len(outcomes)
    if n == 0:
        return {
            "cases": 0, "pass_rate": 0.0, "pass@k": 0.0, "flaky_rate": 0.0,
            "flaky_cases": [], "status_distribution": {}, "total_cost_usd": 0.0,
            "total_turns": 0, "total_tool_calls": 0, "golden_score_mean": None,
            "tiers": {},
        }

    all_pass = sum(1 for o in outcomes if o.pass_rate == 1.0)
    at_least_one = sum(1 for o in outcomes if o.successes >= 1)
    # 排序：报告与 diff 都要可复现，集合遍历顺序不行
    flaky = sorted(o.case_id for o in outcomes if 0.0 < o.pass_rate < 1.0)

    status_dist: Counter[str] = Counter()
    for o in outcomes:
        for s in o.statuses:
            status_dist[s.value] += 1

    # `None` 是"不适用"而非 0 分 —— 混进均值会把分数凭空拉低
    golden = [o.golden_score for o in outcomes if o.golden_score is not None]

    return {
        "cases": n,
        "pass_rate": all_pass / n,
        "pass@k": at_least_one / n,
        "flaky_rate": len(flaky) / n,
        "flaky_cases": flaky,
        "status_distribution": dict(status_dist),
        "total_cost_usd": sum(o.cost_usd for o in outcomes),
        "total_turns": sum(o.turns for o in outcomes),
        "total_tool_calls": sum(o.tool_calls for o in outcomes),
        "golden_score_mean": (sum(golden) / len(golden)) if golden else None,
        "tiers": dict(Counter(o.tier for o in outcomes)),
    }


def to_case_outcomes(runs: list[Any]) -> list[CaseOutcome]:
    """把 `RunOutcome` 列表按 case 归并成 `CaseOutcome` 列表。

    **归并是按 case_id 做的，不是按 run。** `--repeat 3` 会产生三条 run，
    它们是同一个 case 的三次采样 —— 只有合起来才谈得上 flaky。
    按 run 逐条聚合的话 flaky 永远算不出来，而症状是 flaky_rate 恒为 0。
    """
    grouped: dict[str, list[Any]] = {}
    for run in runs:
        grouped.setdefault(run.case_id or run.result.run_id, []).append(run)

    out: list[CaseOutcome] = []
    for case_id in sorted(grouped):
        items = sorted(grouped[case_id], key=lambda r: r.repeat_index)
        scores = [
            ev.score
            for item in items for ev in item.evals
            if ev.evaluator == _GOLDEN_EVALUATOR and ev.score is not None
        ]
        out.append(CaseOutcome(
            case_id=case_id,
            statuses=[item.result.status for item in items],
            # 多次 repeat 的轨迹匹配分取均值 —— 单次命中不代表稳定命中
            golden_score=(sum(scores) / len(scores)) if scores else None,
            cost_usd=sum(item.result.usage.cost_usd for item in items),
            turns=sum(item.result.turns for item in items),
            tool_calls=sum(item.result.tool_calls for item in items),
            tier=_tier_of(items[0]),
            spec_fingerprint=_fingerprint_of(items[0]),
        ))
    return out


def _tier_of(run: Any) -> str:
    meta = getattr(run.result.trajectory, "metadata", None) or {}
    if isinstance(meta, dict) and isinstance(meta.get("tier"), str):
        return meta["tier"]
    # RunSpec 的 metadata 里记了 tier；轨迹没带就退回默认
    start = run.result.trajectory.start()
    if start is not None and start.spec_json:
        try:
            return str(json.loads(start.spec_json).get("metadata", {}).get("tier", "medium"))
        except (ValueError, AttributeError):
            pass
    return "medium"


def _fingerprint_of(run: Any) -> str | None:
    """从 RUN_START 的 spec_json 重建 RunSpec 指纹。

    轨迹是自解释的，所以不必回头查 suite —— 这正是当初把 `spec_json`
    整个塞进 RUN_START 的回报。
    """
    start = run.result.trajectory.start()
    if start is None or not start.spec_json:
        return None
    try:
        from harness.contracts.spec import RunSpec
        return RunSpec.model_validate_json(start.spec_json).fingerprint()
    except Exception:  # noqa: BLE001 — 老轨迹或手写轨迹可能不符合当前 schema
        return None


# ---- 快照 ----
def snapshot_dict(
    agg: dict[str, Any],
    cases: list[CaseOutcome],
    *,
    suite_name: str = "",
) -> dict[str, Any]:
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "suite_name": suite_name,
        "aggregate": agg,
        "runs": [
            {
                "case_id": c.case_id,
                "status": c.case_status,
                "tier": c.tier,
                "pass_rate": c.pass_rate,
                "repeats": len(c.statuses),
                "spec_fingerprint": c.spec_fingerprint,
                "golden_score": c.golden_score,
                "cost_usd": c.cost_usd,
            }
            for c in cases
        ],
    }


def write_snapshot(
    agg: dict[str, Any],
    cases: list[CaseOutcome],
    path: Path | str | None,
    *,
    suite_name: str = "",
    fingerprints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """写 case 级快照。`path` 为 None 时只构造不落盘。

    `fingerprints` 用于补齐轨迹里拿不到的指纹（比如手工构造的 case）。
    """
    data = snapshot_dict(agg, cases, suite_name=suite_name)
    if fingerprints:
        for row in data["runs"]:
            if row["spec_fingerprint"] is None:
                row["spec_fingerprint"] = fingerprints.get(row["case_id"])
    if path is not None:
        write_snapshot_dict(data, path)
    return data


__all__ = [
    "CASE_FAIL",
    "CASE_FLAKY",
    "CASE_OK",
    "CaseOutcome",
    "SnapshotError",
    "aggregate",
    "read_snapshot",
    "snapshot_dict",
    "to_case_outcomes",
    "write_snapshot",
]
