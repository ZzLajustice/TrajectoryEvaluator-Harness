"""终端报告。

## 三条排版上的主张

1. **`pass_rate` / `pass@k` / `flaky_rate` 是三个独立字段，不是一个总分。**
   三个数字并排放在同一行，中间不做任何加权 —— 一合并，过程评测的意义
   就被 outcome 淹没了。

2. **flaky case 单独一节，且放在指标之前。** 它在报告里的位置就是它的重要性：
   一个稳定失败的用例是可以修的，一个时好时坏的用例连"是不是真的修好了"都答不了。

3. **失败模式分布与成本列在最后。** 它们是解释性信息，不是结论。
"""

from __future__ import annotations

from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def build_terminal_report(
    agg: dict[str, Any],
    *,
    suite_name: str = "",
    failure_modes: dict[str, int] | None = None,
    judge: dict[str, Any] | None = None,
) -> Group:
    """把聚合结果组装成 rich 可渲染对象。"""
    parts: list[Any] = []

    title = f"suite: {suite_name}" if suite_name else "suite report"
    parts.append(Text(title, style="bold"))

    flaky = agg.get("flaky_cases") or []
    if flaky:
        body = Text()
        for case_id in flaky:
            body.append(f"  {case_id}\n", style="yellow")
        body.append(
            "\n时好时坏 —— 单次通过说明不了任何事，先看它的重复运行分布。",
            style="dim",
        )
        parts.append(Panel(body, title="[yellow]flaky cases[/yellow]",
                           border_style="yellow"))
    else:
        parts.append(Text("no flaky cases", style="dim"))

    parts.append(Group(Text("metrics", style="bold"), _metrics_table(agg)))
    # 通过率的基准必须紧跟数字 —— 同一个 0.235 在两种基准下含义完全不同，
    # 而读者无法从数字本身分辨（实测两者一个是 0.235、一个是 0.706）。
    parts.append(Text(pass_basis_note(agg.get("pass_basis")), style="dim"))

    dist = agg.get("status_distribution") or {}
    if dist:
        parts.append(_section("run status distribution", dist))

    if failure_modes:
        parts.append(_section(
            "failure modes", dict(sorted(failure_modes.items(), key=lambda kv: -kv[1]))
        ))

    if judge:
        parts.append(_section("judge reliability", judge))

    return Group(*parts)


def _metrics_table(agg: dict[str, Any]) -> Table:
    """三个指标并排，各自独立 —— 没有"总分"这一列。"""
    t = Table(show_header=True, header_style="bold")
    t.add_column("pass_rate", justify="right")
    t.add_column("pass@k", justify="right")
    t.add_column("flaky_rate", justify="right")
    t.add_column("golden_score", justify="right")
    t.add_column("cases", justify="right")
    t.add_column("cost_usd", justify="right")

    golden = agg.get("golden_score_mean")
    t.add_row(
        _pct(agg.get("pass_rate")),
        _pct(agg.get("pass@k")),
        _pct(agg.get("flaky_rate")),
        # 没跑轨迹匹配时显示 n/a 而不是 0.00 —— 那是"不适用"，不是"零分"
        f"{golden:.2f}" if golden is not None else "n/a",
        str(agg.get("cases", 0)),
        f"{agg.get('total_cost_usd', 0.0):.4f}",
    )
    return t


def _section(heading: str, data: dict[str, Any]) -> Group:
    """小标题 + 两列表。

    **标题走独立的 Text 行，不用 `Table(title=...)`。** 表格窄的时候
    rich 会把 title 折行（"status distribution" 断成两行），
    既难看又让"按标题找这一节"这件事在测试里失效。独立一行永远不会折。
    """
    t = Table(show_header=True, header_style="bold")
    t.add_column("key")
    t.add_column("value", justify="right")
    for k, v in data.items():
        t.add_row(str(k), str(v))
    return Group(Text(heading, style="bold"), t)


def _pct(value: Any) -> str:
    return f"{float(value):.2%}" if value is not None else "n/a"


def print_terminal_report(
    agg: dict[str, Any],
    *,
    suite_name: str = "",
    failure_modes: dict[str, int] | None = None,
    judge: dict[str, Any] | None = None,
    console: Console | None = None,
) -> None:
    """渲染到终端。测试传一个写进 StringIO 的 Console 就能断言输出。"""
    (console or Console()).print(build_terminal_report(
        agg, suite_name=suite_name, failure_modes=failure_modes, judge=judge
    ))


# ---- 报告脚注：指标口径必须写在报告里，否则数字会被误读 ----
#
# ★ 通过率还多一个维度：**按哪个基准判"通过"**。同一个数字在两种基准下
#   差别可以很大（实测 0.235 vs 0.706），所以快照里带 `pass_basis`，
#   报告必须把它印出来 —— 不说基准的通过率是个歧义数字。
METRIC_DEFINITIONS = {
    "pass_rate": "所有 repeat 都通过的 case 占比",
    "pass@k": "至少一次通过的 case 占比（注意：不是经典 pass@k 无偏估计量）",
    "flaky_rate": "通过率严格落在 (0,1) 之间的 case 占比",
    "golden_score": "轨迹匹配分（过程分），与 outcome 分列，不做加权",
}

#: `pass_basis` 的两种取值，以及各自"通过"的判据。
PASS_BASIS_DEFINITIONS = {
    "outcome": "隐藏验收测试通过 —— 判据是被测代码，不是 agent 的说法",
    "run_status": "run 以 `ok` 收尾（agent 调了 finish）—— 该 suite 没配结果级评测器",
}


def pass_basis_note(basis: str | None) -> str:
    """报告里那一行"通过率按什么算的"。"""
    if not basis:
        return "pass_rate 基准: 未记录（快照来自更早的版本）"
    return f"pass_rate 基准: {basis} —— {PASS_BASIS_DEFINITIONS.get(basis, '未知')}"


def metric_footnote() -> str:
    return "\n".join(f"{k}: {v}" for k, v in METRIC_DEFINITIONS.items())
