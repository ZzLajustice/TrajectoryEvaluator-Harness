"""终端报告测试。

渲染成字符串再断言 —— rich 的输出是给人看的，但**结构**是可以断言的：
三个指标必须各自独立出现，flaky 必须在它自己那一节里。
"""

from __future__ import annotations

import io

from rich.console import Console

from harness.report.terminal import (
    METRIC_DEFINITIONS,
    build_terminal_report,
    metric_footnote,
    print_terminal_report,
)


def _render(agg, **kw) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=100, no_color=True, highlight=False)
    print_terminal_report(agg, console=console, **kw)
    return buf.getvalue()


def _agg(**over) -> dict:
    base = {
        "cases": 3, "pass_rate": 0.67, "pass@k": 1.0, "flaky_rate": 0.33,
        "flaky_cases": ["bug_007"], "status_distribution": {"ok": 2, "no_finish": 1},
        "total_cost_usd": 0.42, "total_turns": 9, "total_tool_calls": 12,
        "total_reasoning_tokens": 20089,
        "golden_score_mean": 0.8,
    }
    base.update(over)
    return base


def test_three_metrics_appear_as_separate_columns():
    """★ 三个指标并排且**不合并** —— 这是本报告最核心的排版主张。

    合并成一个总分能让所有数字都好看，代价是过程评测的意义被 outcome 淹没。
    """
    out = _render(_agg())
    for name in ("pass_rate", "pass@k", "flaky_rate"):
        assert name in out, f"{name} 没出现在报告里"
    assert "67.00%" in out
    assert "100.00%" in out
    assert "33.00%" in out


def test_golden_score_is_shown_next_to_outcome_metrics():
    out = _render(_agg())
    assert "golden_score" in out
    assert "0.80" in out


def test_missing_golden_score_shows_na_not_zero():
    """没跑轨迹匹配时显示 n/a —— 0.00 会被读成"过程分是零"，那是错的。"""
    out = _render(_agg(golden_score_mean=None))
    assert "n/a" in out
    assert "0.00" not in out.split("golden_score")[1][:20]


def test_flaky_cases_get_their_own_section():
    out = _render(_agg())
    assert "flaky cases" in out
    assert "bug_007" in out


def test_flaky_section_says_what_flaky_means():
    """光列个名字不够 —— 读者要知道为什么这件事严重。"""
    out = _render(_agg())
    assert "时好时坏" in out


def test_no_flaky_cases_says_so_explicitly():
    """没有 flaky 也要打一行字。

    什么都不打的话，"确实没有"和"这一节忘了渲染"看起来一模一样。
    """
    out = _render(_agg(flaky_cases=[], flaky_rate=0.0))
    assert "no flaky cases" in out


def test_status_distribution_is_rendered():
    out = _render(_agg())
    assert "status distribution" in out
    assert "budget_exceeded" not in out
    assert "no_finish" in out


def test_failure_modes_section_renders_when_present():
    out = _render(_agg(), failure_modes={"step_repetition": 2, "hallucinated_tool": 1})
    assert "failure modes" in out
    assert "step_repetition" in out


def test_failure_modes_section_is_absent_when_empty():
    """没有失败模式就不打这一节 —— 空表格会被误读成"渲染失败"。"""
    assert "failure modes" not in _render(_agg(), failure_modes={})


def test_judge_section_renders_when_present():
    out = _render(_agg(), judge={"consistency": 0.85, "cost_usd": 0.05})
    assert "judge reliability" in out
    assert "0.85" in out


def test_suite_name_is_shown():
    assert "codefix" in _render(_agg(), suite_name="codefix")


def test_empty_everything_does_not_crash():
    out = _render({})
    assert "suite report" in out
    assert "pass_rate" in out


def test_failure_modes_are_sorted_by_count_descending():
    """人看的是"最常出的那个"，不是字母序。"""
    out = _render(_agg(), failure_modes={"aaa": 1, "zzz": 9})
    assert out.index("zzz") < out.index("aaa")


def test_report_object_is_buildable_without_a_console():
    """`build_terminal_report` 只组装不打印 —— 让 HTML 渲染器也能复用同一份结构。"""
    assert build_terminal_report(_agg()) is not None


def test_metric_definitions_are_documented_in_the_report_footnote():
    """★ 口径必须写在报告里。

    `pass@k` 在本项目里是「至少一次通过的 case 占比」，不是经典那个无偏估计量。
    名字沿用业界叫法但语义不同，不写明就一定会被读错。

    ★ 断言刻意是**子集**不是相等。原来写的是 `== {四条}`，那等于说
    "**只有**这四条" —— 于是加第五条指标（`reasoning_tokens`）时它红了，
    而它想说的其实是"这四条**必须**在"。测"必须包含"就别写成"恰好等于"：
    后者会让每一次合理的扩充都变成一次假警报，久了就没人看红灯了。
    """
    required = {"pass_rate", "pass@k", "flaky_rate", "golden_score"}
    assert required <= set(METRIC_DEFINITIONS), (
        f"这些口径没有写进报告：{sorted(required - set(METRIC_DEFINITIONS))}")
    note = metric_footnote()
    assert "无偏估计量" in note
    assert "分列" in note


def test_reasoning_volume_is_shown_and_labelled_as_volume():
    """★ 推理 token 数必须在报告里出现 —— 否则那个指标等于没做。

    ★ 这条是被打脸写下的：评测器里先算好了 `reasoning_tokens`，
    跑到报告这一步才发现快照不带 `metrics`、终端报告的列是写死的六个，
    于是它被算出来然后扔掉。**"算了但没人看得见"与"没算"没有区别。**

    标签刻意叫 `reasoning_tok` 而不是 `reasoning` 之类 ——
    它量的是体量（多少产出是不可见的），不是质量分。
    """
    text = _render(_agg())
    assert "reasoning_tok" in text
    assert "20089" in text


def test_missing_reasoning_volume_renders_as_zero_not_blank():
    """没跑效率评测器时是 0，不是空 —— 空格在表格里读起来像"坏了"。"""
    text = _render(_agg(total_reasoning_tokens=0))
    assert "reasoning_tok" in text
