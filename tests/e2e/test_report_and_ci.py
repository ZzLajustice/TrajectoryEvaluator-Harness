"""M8 端到端验收：报告、diff、CI 门禁。

## 这一层测什么

单元测试证明了聚合算法、diff 分类、模板渲染各自正确；
这里证明**它们接在一起能用**：跑一次 suite → 产物落盘 → 报告能读回来 →
两次运行能对比 → `ci` 的退出码符合契约。

退出码是 `ci` 的对外契约，CI 流水线靠它决定红绿。每个码对应一类
**不同的人要采取的行动**，所以每个都要有一条测试盯着。
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app
from harness.store.layout import SNAPSHOT_NAME

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAPS = REPO_ROOT / "examples" / "traps.yaml"
HELLO = REPO_ROOT / "examples" / "hello.yaml"


def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


def _run_suite(suite: Path, out: Path):
    return _invoke(["run", "--suite", str(suite), "--out", str(out),
                    "--workdir", str(out / "wd"), "--evaluate"])


# ---- 快照 ----
def test_a_run_writes_a_case_level_snapshot(tmp_path):
    """★ 快照是 diff / ci / report 三个命令的共同输入。

    它由 `run_suite` 自己写，而不是让 CLI 记得写 —— 忘了写的话
    diff 会**安静地**拿一份过期基线去比，而那看起来像"没有回归"。
    """
    _run_suite(TRAPS, tmp_path)
    snap = json.loads((tmp_path / SNAPSHOT_NAME).read_text(encoding="utf-8"))

    assert snap["suite_name"] == "traps"
    assert len(snap["runs"]) == 9
    row = snap["runs"][0]
    for key in ("case_id", "status", "spec_fingerprint", "pass_rate", "repeats"):
        assert key in row, f"快照缺少 {key}"
    assert row["status"] in {"ok", "fail", "flaky"}


def test_snapshot_is_written_even_without_evaluate(tmp_path):
    """不评测也要落快照 —— diff 比的是 outcome，跟开不开评测无关。"""
    _invoke(["run", "--suite", str(HELLO), "--out", str(tmp_path),
             "--workdir", str(tmp_path / "wd")])
    assert (tmp_path / SNAPSHOT_NAME).exists()


def test_fingerprints_are_recorded_per_case(tmp_path):
    """没有指纹就没法判断两次运行可不可比。"""
    _run_suite(HELLO, tmp_path)
    snap = json.loads((tmp_path / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert snap["runs"][0]["spec_fingerprint"]


# ---- 报告 ----
def test_terminal_report_reads_back_the_products(tmp_path):
    _run_suite(TRAPS, tmp_path)
    result = _invoke(["report", "--runs", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for token in ("pass_rate", "pass@k", "flaky_rate", "failure modes"):
        assert token in result.output, f"{token} 没出现在报告里"


def test_terminal_report_prints_the_metric_definitions(tmp_path):
    """口径随数字一起交付。"""
    _run_suite(TRAPS, tmp_path)
    out = _invoke(["report", "--runs", str(tmp_path)]).output
    assert "无偏估计量" in out


def test_failure_modes_come_from_the_sqlite_index(tmp_path):
    """★ 失败模式从索引读回，不重跑评测 —— M6 埋的伏笔在这里兑现。"""
    _run_suite(TRAPS, tmp_path)
    out = _invoke(["report", "--runs", str(tmp_path)]).output
    # traps.yaml 构造出来的模式应当出现在报告里
    assert "step_repetition" in out
    assert "hallucinated_tool" in out


def test_html_report_is_written_and_self_contained(tmp_path):
    _run_suite(TRAPS, tmp_path)
    out_file = tmp_path / "r.html"
    result = _invoke(["report", "--runs", str(tmp_path),
                      "--format", "html", "--out", str(out_file)])
    assert result.exit_code == 0, result.output

    text = out_file.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<!DOCTYPE html>")
    assert "<script src=" not in text, "报告不得引用外部脚本"
    assert "flaky_rate" in text


def test_html_report_defaults_next_to_the_runs(tmp_path):
    _run_suite(HELLO, tmp_path)
    _invoke(["report", "--runs", str(tmp_path), "--format", "html"])
    assert (tmp_path / "report.html").exists()


def test_report_on_a_directory_without_a_snapshot_is_a_config_error(tmp_path):
    result = _invoke(["report", "--runs", str(tmp_path)])
    assert result.exit_code == 2


def test_unknown_format_is_a_config_error(tmp_path):
    _run_suite(HELLO, tmp_path)
    result = _invoke(["report", "--runs", str(tmp_path), "--format", "pdf"])
    assert result.exit_code == 2
    assert "unknown format" in result.output


# ---- diff ----
def test_diff_detects_an_engineered_regression(tmp_path):
    """★ M8 的验收判据：`harness diff --baseline` 输出回归/修复/flaky 三分类。

    做法：同一份 suite 跑两次，第二次的 case 集少一条且脚本不同 —— 但那样
    指纹也变了，会被判不可比。所以这里直接改快照文件造出一个回归，
    测的是 **diff 命令的接线**（分类算法本身在 tests/orchestration/test_diff.py）。
    """
    _run_suite(HELLO, tmp_path)
    (tmp_path / "baseline.json").write_text(
        (tmp_path / SNAPSHOT_NAME).read_text(encoding="utf-8"), encoding="utf-8")

    snap = json.loads((tmp_path / SNAPSHOT_NAME).read_text(encoding="utf-8"))
    snap["runs"][0]["status"] = "fail"
    (tmp_path / SNAPSHOT_NAME).write_text(
        json.dumps(snap, ensure_ascii=False), encoding="utf-8", newline="\n")

    result = _invoke(["diff", "--baseline", str(tmp_path / "baseline.json"),
                      "--current", str(tmp_path / SNAPSHOT_NAME)])
    assert "regressions" in result.output
    assert "hello" in result.output
    # 有回归 → 非零退出，否则这条命令进不了流水线
    assert result.exit_code == 1


def test_diff_lists_every_category_even_when_empty(tmp_path):
    _run_suite(HELLO, tmp_path)
    (tmp_path / "b.json").write_text(
        (tmp_path / SNAPSHOT_NAME).read_text(encoding="utf-8"), encoding="utf-8")

    result = _invoke(["diff", "--baseline", str(tmp_path / "b.json"),
                      "--current", str(tmp_path / SNAPSHOT_NAME)])
    for key in ("regressions", "fixes", "flaky", "incomparable", "added", "removed"):
        assert key in result.output
    assert result.exit_code == 0


def test_diff_with_a_missing_baseline_is_a_config_error(tmp_path):
    _run_suite(HELLO, tmp_path)
    result = _invoke(["diff", "--baseline", str(tmp_path / "nope.json"),
                      "--current", str(tmp_path / SNAPSHOT_NAME)])
    assert result.exit_code == 2


# ---- ci 退出码契约 ----
def test_ci_passes_on_a_healthy_suite(tmp_path):
    result = _invoke(["ci", "-s", str(HELLO), "--out", str(tmp_path / "runs"),
                      "--workdir", str(tmp_path / "wd"), "--fail-under", "0.9"])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_ci_exit_1_when_pass_rate_is_below_the_bar(tmp_path):
    result = _invoke(["ci", "-s", str(HELLO), "--out", str(tmp_path / "runs"),
                      "--workdir", str(tmp_path / "wd"), "--fail-under", "1.01"])
    assert result.exit_code == 1
    assert "pass_rate" in result.output


def test_ci_exit_2_on_a_missing_suite(tmp_path):
    result = _invoke(["ci", "-s", str(tmp_path / "nope.yaml"),
                      "--out", str(tmp_path / "runs")])
    assert result.exit_code == 2


def test_ci_exit_3_when_the_cost_cap_is_exceeded(tmp_path):
    """成本上限是 suite 级的硬门禁。这里把上限设成 0 来触发它。"""
    result = _invoke(["ci", "-s", str(TRAPS), "--out", str(tmp_path / "runs"),
                      "--workdir", str(tmp_path / "wd"), "--max-cost", "0"])
    assert result.exit_code == 3
    assert "budget" in result.output.lower()


def test_ci_exit_4_when_the_baseline_is_missing(tmp_path):
    """基线缺失是**配置问题** —— 该在烧掉一整轮 suite 的钱之前就拦下。"""
    result = _invoke(["ci", "-s", str(HELLO), "--out", str(tmp_path / "runs"),
                      "--workdir", str(tmp_path / "wd"),
                      "--baseline", str(tmp_path / "nope.json")])
    assert result.exit_code == 4
    assert "baseline missing" in result.output


def test_ci_with_a_baseline_reports_the_diff(tmp_path):
    runs = tmp_path / "runs"
    first = _invoke(["ci", "-s", str(HELLO), "--out", str(runs),
                     "--workdir", str(tmp_path / "wd"), "--fail-under", "0.9"])
    assert first.exit_code == 0, first.output

    import shutil

    baseline = tmp_path / "baseline.json"
    shutil.copy(runs / SNAPSHOT_NAME, baseline)

    second = _invoke(["ci", "-s", str(HELLO), "--out", str(runs),
                      "--workdir", str(tmp_path / "wd"), "--fail-under", "0.9",
                      "--baseline", str(baseline)])
    assert second.exit_code == 0, second.output
    assert "regressions" in second.output


# ---- 成本上限的语义 ----
def test_cost_cap_stops_launching_new_cases(tmp_path):
    """上限击穿后**不再启动新 case**，而不是把整轮砍掉。

    已经在跑的那几条让它们跑完 —— 中途掐断会留下半截沙箱，
    而"少跑一条"比"跑一条半"更容易解释。
    """
    from harness.orchestration.deps import RunBuilder, SuiteCostExceeded

    builder = RunBuilder(out_dir=tmp_path / "runs", workdir=tmp_path / "wd")
    try:
        builder.run_suite_sync(TRAPS, max_cost=0.0)
    except SuiteCostExceeded as exc:
        assert "not started" in str(exc)
    else:
        raise AssertionError("成本上限为 0 却没有触发 SuiteCostExceeded")


def test_no_cost_cap_means_no_stopping(tmp_path):
    from harness.orchestration.deps import RunBuilder

    outcomes = RunBuilder(out_dir=tmp_path / "runs",
                          workdir=tmp_path / "wd").run_suite_sync(HELLO)
    assert len(outcomes) == 1
