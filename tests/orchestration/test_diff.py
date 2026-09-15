"""基线对比测试。

## 本文件的两条主线

1. **指纹不同必须判不可比。** 模型/prompt 变了就不是同一个实验，
   直接比数字会得出"变差了"这种错误结论。
2. **三态序 fail < flaky < ok，不是二值 ok/not-ok。**
   二值判法会静默丢掉 `flaky → fail` 这类真实退化。
"""

from __future__ import annotations

import json

import pytest

from harness.orchestration.diff import SnapshotError, diff_runs, format_diff


def _write(path, runs: list[dict]) -> None:
    path.write_text(json.dumps({"runs": runs}), encoding="utf-8", newline="\n")


def _run(case_id: str, status: str, fp: str | None = "fp1") -> dict:
    return {"case_id": case_id, "status": status, "spec_fingerprint": fp}


def test_regression_is_detected(tmp_path):
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "ok"), _run("x", "ok")])
    _write(c, [_run("a", "ok"), _run("x", "fail")])
    assert diff_runs(b, c)["regressions"] == ["x"]


def test_fix_is_detected(tmp_path):
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "fail")])
    _write(c, [_run("a", "ok")])
    assert diff_runs(b, c)["fixes"] == ["a"]


def test_flaky_is_detected_when_baseline_passed_and_candidate_flaked(tmp_path):
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "ok")])
    _write(c, [_run("a", "flaky")])
    assert diff_runs(b, c)["flaky"] == ["a"]


def test_flaky_to_fail_is_a_regression_not_dropped(tmp_path):
    """★ 这是计划里的二值判法会**静默丢掉**的一类真实退化。

    原先的判据是 `b_ok = status == "ok"` / `c_ok = status == "ok"`：
    `flaky → fail` 两边都不是 ok，于是既不进 regression 也不进 fix，
    从报告里彻底消失 —— 而它明明是"本来时好时坏，现在彻底不工作了"。

    改成按序比较（fail < flaky < ok）后它落进 regressions。
    """
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "flaky")])
    _write(c, [_run("a", "fail")])
    d = diff_runs(b, c)
    assert d["regressions"] == ["a"]
    assert d["fixes"] == [] and d["flaky"] == []


def test_flaky_to_ok_is_a_fix(tmp_path):
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "flaky")])
    _write(c, [_run("a", "ok")])
    assert diff_runs(b, c)["fixes"] == ["a"]


def test_fail_to_flaky_is_an_improvement(tmp_path):
    """从"从来不过"到"有时候过"是进步 —— 二值判法同样会丢掉它。"""
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "fail")])
    _write(c, [_run("a", "flaky")])
    assert diff_runs(b, c)["fixes"] == ["a"]


def test_unchanged_cases_appear_nowhere(tmp_path):
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "ok"), _run("b", "fail")])
    _write(c, [_run("a", "ok"), _run("b", "fail")])
    d = diff_runs(b, c)
    assert all(not d[k] for k in ("regressions", "fixes", "flaky", "incomparable"))


def test_changed_fingerprint_is_reported_as_incomparable(tmp_path):
    """模型/prompt 变了就不是同一实验，不能直接对比。"""
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "ok", fp="fp1")])
    _write(c, [_run("a", "ok", fp="fp2")])
    assert diff_runs(b, c)["incomparable"] == ["a"]


def test_a_changed_fingerprint_masks_the_regression(tmp_path):
    """不可比时**不能**顺手报个 regression —— 那正是"模型换了"被读成"代码变差了"。"""
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "ok", fp="fp1")])
    _write(c, [_run("a", "fail", fp="fp2")])
    d = diff_runs(b, c)
    assert d["incomparable"] == ["a"]
    assert d["regressions"] == []


def test_missing_fingerprint_on_one_side_is_incomparable(tmp_path):
    """一边有指纹一边没有 → 无法确认可比。宁可说不可比，也不要给个错的结论。"""
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [{"case_id": "a", "status": "ok"}])
    _write(c, [_run("a", "fail", fp="fp1")])
    assert diff_runs(b, c)["incomparable"] == ["a"]


def test_new_and_removed_cases_are_reported(tmp_path):
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("old", "ok")])
    _write(c, [_run("new", "ok")])
    d = diff_runs(b, c)
    assert d["added"] == ["new"]
    assert d["removed"] == ["old"]


def test_missing_baseline_raises_specific_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        diff_runs(tmp_path / "nope.json", tmp_path / "nope2.json")


def test_unknown_status_is_treated_as_worst(tmp_path):
    """快照里出现没见过的状态时按最差处理。

    顺手当成 ok 会让真实退化从 diff 里消失，而那种错误没有任何症状。
    """
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("a", "ok")])
    _write(c, [_run("a", "some_new_status")])
    assert diff_runs(b, c)["regressions"] == ["a"]


def test_malformed_snapshot_raises_snapshot_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8", newline="\n")
    with pytest.raises(SnapshotError):
        diff_runs(p, p)


def test_snapshot_row_without_case_id_raises(tmp_path):
    p = tmp_path / "bad.json"
    _write(p, [{"status": "ok"}])
    with pytest.raises(SnapshotError):
        diff_runs(p, p)


def test_results_are_sorted_for_reproducibility(tmp_path):
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    _write(b, [_run("z", "ok"), _run("a", "ok"), _run("m", "ok")])
    _write(c, [_run("z", "fail"), _run("a", "fail"), _run("m", "fail")])
    assert diff_runs(b, c)["regressions"] == ["a", "m", "z"]


def test_format_diff_always_lists_every_category(tmp_path):
    """空列表也要打出来 —— "这一类是空的"本身就是结论。"""
    out = format_diff({"regressions": ["x"], "fixes": [], "flaky": [],
                       "incomparable": [], "added": [], "removed": []})
    for key in ("regressions", "fixes", "flaky", "incomparable", "added", "removed"):
        assert key in out
    assert "['x']" in out


def test_diff_against_a_real_snapshot_roundtrip(tmp_path):
    """与聚合器产出的真实快照对接 —— 两侧的键名必须对得上。"""
    from tests.orchestration.test_aggregator import _run_outcome

    from harness.contracts.spec import RunStatus
    from harness.orchestration.aggregator import (
        aggregate,
        read_snapshot,
        to_case_outcomes,
        write_snapshot,
    )

    base_cases = to_case_outcomes([
        _run_outcome("a", RunStatus.OK), _run_outcome("b", RunStatus.OK)])
    write_snapshot(aggregate(base_cases), base_cases, tmp_path / "b.json",
                   suite_name="demo")

    cand_cases = to_case_outcomes([
        _run_outcome("a", RunStatus.OK), _run_outcome("b", RunStatus.NO_FINISH)])
    write_snapshot(aggregate(cand_cases), cand_cases, tmp_path / "c.json",
                   suite_name="demo")

    d = diff_runs(tmp_path / "b.json", tmp_path / "c.json")
    # 指纹来自真实的 RunSpec，两边一致 → 可比
    assert d["incomparable"] == []
    assert d["regressions"] == ["b"]
    assert read_snapshot(tmp_path / "b.json")["suite_name"] == "demo"
