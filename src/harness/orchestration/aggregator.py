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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.contracts.results import EvalStatus
from harness.contracts.spec import RunStatus
from harness.store.snapshot import SnapshotError, read_snapshot, write_snapshot_dict

# 过程分的来源。刻意只认轨迹匹配 —— 把效率分混进来会让 golden_score 语义失焦
# （效率分的 1/step_ratio 与"是否走对了路"根本不是一回事）。
_GOLDEN_EVALUATOR = "TrajectoryMatcher"

# 结果级判定的来源。见 `CaseOutcome.comparables` 的说明。
_OUTCOME_EVALUATOR = "OutcomeGrader"

# 推理体量的来源。评测器的 metrics 只活在 `EvalResult` 里，而报告与快照
# 读的是聚合层 —— 不在这里接一手，"算了但没人看得见"与"没算"没有区别。
_REASONING_EVALUATOR = "EfficiencyAnalyzer"

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
    #: 每次 repeat 的**结果级**判定：`True` 通过 / `False` 没通过 / `None` 判不了。
    #:
    #: `None` 覆盖两种"判不了"：没配结果级评测器（SKIPPED），
    #: 以及用例本身坏了（ERROR）—— 后者绝不能算作 agent 的失败。
    outcome_flags: list[bool | None] = field(default_factory=list)
    golden_score: float | None = None
    cost_usd: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    #: provider 上报的推理 token 数（`EfficiencyAnalyzer` 汇总的）。
    #:
    #: 它不是"推理质量"，是**体量**：模型把多少产出花在了所有评测器都
    #: 看不到的地方。实测全量真跑是 completion tokens 的 34.5%。
    reasoning_tokens: int = 0
    tier: str = "medium"
    spec_fingerprint: str | None = None

    @property
    def comparables(self) -> list[bool]:
        """用于算通过率的逐次判定。

        ★ **结果级判定优先于 run 终态。**

        设计文档写着「outcome 永远是主判据」，而这里原先只看
        `RunStatus.OK` —— 也就是"agent 有没有调 finish"。两者实测差得离谱：
        一次 17 条用例的真模型 run 里，`OutcomeGrader` 说 12 条修好了，
        而按 run 终态只有 4 条 —— 因为模型修完 bug 就继续干活直到轮次耗尽，
        从不调 finish。

        于是 `pass_rate` 报 **0.235**，而真实通过率是 **0.706**。
        读报告的人会得出"模型只能解 24%"，真相是"它解了 71%，只是没说收工"。
        一个把"没有 outcome 判据"与"没有修好"混为一谈的指标，
        正是本项目声称要反对的那种东西。

        run 终态仍然单独保留在 `status_distribution` 里 ——
        "它有没有正常收尾"是有价值的过程信号，只是不该冒充通过率。
        """
        flags = [f for f in self.outcome_flags if f is not None]
        if flags:
            return flags
        return [s is RunStatus.OK for s in self.statuses]

    @property
    def pass_basis(self) -> str:
        """这次通过率是按哪个基准算的。报告要能说出这一点。"""
        return "outcome" if any(f is not None for f in self.outcome_flags) \
            else "run_status"

    @property
    def successes(self) -> int:
        return sum(1 for ok in self.comparables if ok)

    @property
    def pass_rate(self) -> float:
        values = self.comparables
        return self.successes / len(values) if values else 0.0

    @property
    def case_status(self) -> str:
        """Case 级三态。判据与 `flaky_rate` 用的是同一条边界。"""
        values = self.comparables
        if not values:
            return CASE_FAIL
        if self.successes == len(values):
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
            "pass_basis": "run_status",
            "flaky_cases": [], "status_distribution": {}, "total_cost_usd": 0.0,
            "total_turns": 0, "total_tool_calls": 0, "golden_score_mean": None,
            "total_reasoning_tokens": 0,
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
        # ★ 这次通过率是按哪个基准算的。不说出来的话，
        # 同一个数字在两份报告里可能含义不同，而读者无从分辨。
        "pass_basis": ("outcome" if any(o.pass_basis == "outcome" for o in outcomes)
                       else "run_status"),
        "flaky_rate": len(flaky) / n,
        "flaky_cases": flaky,
        "status_distribution": dict(status_dist),
        "total_cost_usd": sum(o.cost_usd for o in outcomes),
        "total_turns": sum(o.turns for o in outcomes),
        "total_tool_calls": sum(o.tool_calls for o in outcomes),
        "total_reasoning_tokens": sum(o.reasoning_tokens for o in outcomes),
        "golden_score_mean": (sum(golden) / len(golden)) if golden else None,
        "tiers": dict(Counter(o.tier for o in outcomes)),
    }


def _outcome_flag(item: Any) -> bool | None:
    """从一条 run 的评测结果里取"结果级判定"，取不到就是 `None`。

    只认 PASS / FAIL 两种状态：`SKIPPED`（没配结果级评测器）与
    `ERROR`（用例本身坏了）都属于**判不了**。

    ★ 把它们算成"失败"会让 `pass_rate` 虚低，而虚低的通过率会让真失败
    淹没在噪声里 —— 与 `OutcomeGrader` 内部区分三态是同一个理由。
    """
    for ev in item.evals:
        if ev.evaluator != _OUTCOME_EVALUATOR:
            continue
        if ev.status not in (EvalStatus.PASS, EvalStatus.FAIL):
            return None
        return ev.status is EvalStatus.PASS
    return None


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
        # 推理体量是**累计量**，所以跨 repeat 求和而不是取均值 ——
        # 与 cost/turns 一致。取均值会得到"每次 repeat 平均想多少"，
        # 而报告要回答的是"整个 suite 有多少产出是不可见的"。
        reason_tokens = sum(
            ev.metrics.get("reasoning_tokens", 0.0)
            for item in items for ev in item.evals
            if ev.evaluator == _REASONING_EVALUATOR
        )
        out.append(CaseOutcome(
            case_id=case_id,
            statuses=[item.result.status for item in items],
            outcome_flags=[_outcome_flag(item) for item in items],
            # 多次 repeat 的轨迹匹配分取均值 —— 单次命中不代表稳定命中
            golden_score=(sum(scores) / len(scores)) if scores else None,
            cost_usd=sum(item.result.usage.cost_usd for item in items),
            turns=sum(item.result.turns for item in items),
            tool_calls=sum(item.result.tool_calls for item in items),
            reasoning_tokens=int(reason_tokens),
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
                "reasoning_tokens": c.reasoning_tokens,
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
