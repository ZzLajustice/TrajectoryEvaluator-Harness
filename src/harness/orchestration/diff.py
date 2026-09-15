"""基线对比。

## 核心设计：指纹不同 → 标记 incomparable

模型、prompt、工具集变了就不是同一个实验，直接比数字会得出**错误结论** ——
"pass_rate 掉了 20%"可能只是换了模型，而不是代码变差了。
`RunSpec.fingerprint()` 就是为这件事存在的。

## case 级三态与它们的序

    fail < flaky < ok

用**序**而不是二值 `ok / not ok` 来判回归。二值判法会漏掉一类真实退化：
`flaky → fail`（本来时好时坏，现在彻底不工作了）在二值下两边都不是 ok，
于是既不进 regression 也不进 fix —— **被静默丢弃**。有测试盯着这条。

`flaky` 仍然单独成列（"本来是好的，现在时好时坏"最值得先看），
但它同时也会体现在 rank 下降上，两者是互补的视角而非重复计数：
`flaky` 那一列只装 `ok → flaky`，其余退化进 `regressions`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.store.snapshot import SnapshotError, read_snapshot

# case 级状态的序。数值大小即好坏。
_RANK = {"fail": 0, "flaky": 1, "ok": 2}

# 快照里 case 级状态用的键名
_STATUS = "status"
_FP = "spec_fingerprint"


def _load(path: Path | str) -> dict[str, dict]:
    data = read_snapshot(path)
    runs = data.get("runs", [])
    if not isinstance(runs, list):
        raise SnapshotError(f"snapshot 'runs' must be a list, got {type(runs).__name__}")
    out: dict[str, dict] = {}
    for row in runs:
        if not isinstance(row, dict) or "case_id" not in row:
            raise SnapshotError(f"snapshot row needs a 'case_id': {row!r}")
        out[str(row["case_id"])] = row
    return out


def _rank(row: dict) -> int:
    status = str(row.get(_STATUS, "fail"))
    if status not in _RANK:
        # 未知状态按最差处理 —— 悄悄当成 ok 会让真实退化消失
        return _RANK["fail"]
    return _RANK[status]


def diff_runs(baseline: Path | str, candidate: Path | str) -> dict[str, list[str]]:
    """对比两次 run 的 case 级快照。"""
    base, cand = _load(baseline), _load(candidate)

    regressions: list[str] = []
    fixes: list[str] = []
    flaky: list[str] = []
    incomparable: list[str] = []

    for case_id in sorted(base.keys() & cand.keys()):
        b, c = base[case_id], cand[case_id]
        if b.get(_FP) != c.get(_FP):
            # 指纹不同（含一边缺失）→ 不可比。缺失也归到这一类：
            # 无法确认可比时，宁可说"不可比"也不要给出一个错的结论。
            incomparable.append(case_id)
            continue

        b_rank, c_rank = _rank(b), _rank(c)
        if b_rank == c_rank:
            continue
        if b_rank == _RANK["ok"] and str(c.get(_STATUS)) == "flaky":
            # 单列出来：稳定 → 不稳定，是最该先看的一类
            flaky.append(case_id)
        elif c_rank < b_rank:
            regressions.append(case_id)
        else:
            fixes.append(case_id)

    return {
        "regressions": regressions,
        "fixes": fixes,
        "flaky": flaky,
        "incomparable": incomparable,
        "added": sorted(cand.keys() - base.keys()),
        "removed": sorted(base.keys() - cand.keys()),
    }


def format_diff(d: dict[str, list[str]]) -> str:
    """人类可读的对比结果。空的那几类也打出来 —— 空列表本身就是结论。"""
    lines = []
    for key in ("regressions", "fixes", "flaky", "incomparable", "added", "removed"):
        values = d.get(key, [])
        mark = {"regressions": "!", "flaky": "~"}.get(key, " ")
        lines.append(f"{mark} {key:<13} {values if values else '[]'}")
    return "\n".join(lines)


def has_regressions(d: dict[str, Any], *, max_regressions: int = 0) -> bool:
    return len(d.get("regressions", [])) > max_regressions
