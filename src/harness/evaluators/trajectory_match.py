"""轨迹匹配评测器。

## 两个正交维度

**维度 1 — 轨迹模式（`mode`）**

| 模式 | 语义 |
|---|---|
| `strict` | 顺序与内容完全一致 |
| `unordered` | 工具调用集合相同，顺序任意 |
| `subset` | actual ⊆ expected（**白名单**，禁止多余调用） |
| `superset` | actual ⊇ expected（允许探索性多余调用） |
| `in_order` | expected 是 actual 的有序子序列 |

命名以 **LangChain agentevals** 为准（`strict`/`unordered`/`subset`/`superset`）；
`in_order` 来自 agentv 项目，实践中很有用。
注意**不是** exact / in-order / any-order —— 那是另一套命名。

**维度 2 — 参数匹配（`tool_args_match_mode`）**：`exact` / `ignore` / `subset` / `superset`

两维正交：可以"顺序严格但参数忽略"，也可以"顺序任意但参数全等"。

## `arg_normalizers` 是必须实现项，不是可选项

代码修复任务里命令串、文件路径、时间戳天然不确定。
没有 per-tool 的归一化，**临时工作目录前缀一变，golden 就永远匹配不上** ——
而症状是"模型变差了"，不是"匹配器有问题"。

归一化**对两侧都应用**：只归一化实际值，expected 侧仍然对不上。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalResult, EvalStatus, Finding, Severity
from harness.evaluators.base import BaseEvaluator
from harness.events.trajectory import Trajectory
from harness.events.types import EventType

_MODES = ("strict", "unordered", "subset", "superset", "in_order")
_ARG_MODES = ("exact", "ignore", "subset", "superset")


class TrajectoryMatcher(BaseEvaluator):
    name = "TrajectoryMatcher"
    subscribes = frozenset({EventType.TOOL_CALL, EventType.RUN_END})

    def __init__(
        self,
        *,
        mode: str,
        expected: list[tuple[str, dict[str, Any]]],
        tool_args_match_mode: str = "exact",
        arg_normalizers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {list(_MODES)}, got {mode!r}")
        if tool_args_match_mode not in _ARG_MODES:
            raise ValueError(
                f"tool_args_match_mode must be one of {list(_ARG_MODES)}, "
                f"got {tool_args_match_mode!r}"
            )
        self.mode = mode
        self.expected = expected
        self.arg_mode = tool_args_match_mode
        self.normalizers = arg_normalizers or {}

    # ---- 归一化 ----
    def _norm(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = self.normalizers.get(name)
        return fn(args) if fn else args

    def _calls(self, traj: Trajectory) -> list[tuple[str, dict[str, Any]]]:
        return [(c.name, self._norm(c.name, c.arguments)) for c in traj.tool_calls()]

    def _expected(self) -> list[tuple[str, dict[str, Any]]]:
        return [(n, self._norm(n, a)) for n, a in self.expected]

    # ---- 匹配 ----
    def _args_match(self, actual: dict[str, Any], exp: dict[str, Any]) -> bool:
        if self.arg_mode == "ignore":
            return True
        if self.arg_mode == "exact":
            return actual == exp
        if self.arg_mode == "subset":
            # actual 是 expected 的子集：actual 的每个键在 expected 中都有且相等
            return all(exp.get(k) == v for k, v in actual.items())
        # superset：expected 的每个键在 actual 中都有且相等
        return all(actual.get(k) == v for k, v in exp.items())

    def _matches(self, a: tuple[str, dict], e: tuple[str, dict]) -> bool:
        return a[0] == e[0] and self._args_match(a[1], e[1])

    def _check(self, actual: list, expected: list) -> tuple[bool, list[Finding]]:
        if self.mode == "strict":
            if len(actual) != len(expected):
                return False, [Finding(
                    code="trajectory.length_mismatch", severity=Severity.MAJOR,
                    message=f"expected {len(expected)} calls, got {len(actual)}")]
            bad = [i for i, (a, e) in enumerate(zip(actual, expected))
                   if not self._matches(a, e)]
            if bad:
                return False, [Finding(
                    code="trajectory.step_mismatch", severity=Severity.MAJOR,
                    message=f"steps differ at indices {bad}")]
            return True, []

        if self.mode == "unordered":
            pool = list(actual)
            missing = []
            for e in expected:
                hit = next((i for i, a in enumerate(pool) if self._matches(a, e)), None)
                if hit is None:
                    missing.append(e[0])
                else:
                    pool.pop(hit)
            if missing or pool:
                return False, [Finding(
                    code="trajectory.missing_tool" if missing else "trajectory.unexpected_tool",
                    severity=Severity.MAJOR,
                    message=f"missing={missing} extra={[p[0] for p in pool]}")]
            return True, []

        if self.mode == "superset":
            pool = list(actual)
            missing = []
            for e in expected:
                hit = next((i for i, a in enumerate(pool) if self._matches(a, e)), None)
                if hit is None:
                    missing.append(e[0])
                else:
                    pool.pop(hit)
            if missing:
                return False, [Finding(
                    code="trajectory.missing_tool", severity=Severity.MAJOR,
                    message=f"missing required tools: {missing}")]
            return True, []

        if self.mode == "subset":
            extras = [a for a in actual
                      if not any(self._matches(a, e) for e in expected)]
            if extras:
                return False, [Finding(
                    code="trajectory.unexpected_tool", severity=Severity.MAJOR,
                    message=f"tools outside allowlist: {[x[0] for x in extras]}")]
            return True, []

        # in_order：expected 是 actual 的有序子序列
        it = iter(actual)
        for e in expected:
            if not any(self._matches(a, e) for a in it):
                return False, [Finding(
                    code="trajectory.order_violation", severity=Severity.MAJOR,
                    message=f"{e[0]!r} not found in order")]
        return True, []

    # ---- 主入口 ----
    def evaluate(self, traj: Trajectory, ctx: EvalContext) -> EvalResult:
        actual = self._calls(traj)
        expected = self._expected()

        if not expected:
            return self.skipped(traj, "no expected trajectory configured")
        if not actual:
            return self.skipped(traj, "no tool calls recorded")

        ok, findings = self._check(actual, expected)

        actual_names = [n for n, _ in actual]
        expected_names = [n for n, _ in expected]
        hits = sum(1 for n in expected_names if n in actual_names)
        recall = hits / len(expected_names) if expected_names else 1.0
        precision = (sum(1 for n in actual_names if n in expected_names) / len(actual_names)
                     if actual_names else 1.0)

        return EvalResult(
            evaluator=self.name,
            evaluator_version=self.version,
            run_id=traj.run_id,
            status=EvalStatus.PASS if ok else EvalStatus.FAIL,
            score=1.0 if ok else 0.0,
            summary=f"{self.mode} match {'succeeded' if ok else 'failed'} "
                    f"({len(actual)} calls vs {len(expected)} expected)",
            findings=findings,
            metrics={
                "tool_recall": recall,
                "tool_precision": precision,
                "actual_calls": float(len(actual)),
                "expected_calls": float(len(expected)),
            },
        )
