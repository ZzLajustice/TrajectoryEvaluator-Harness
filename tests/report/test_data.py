"""报告数据装配：快照、失败模式、judge 面板必须来自**同一次 run**。

## 这个文件是被一次真跑逼出来的

2026-09-19 第一次在浏览器里打开报告（known-gaps §1.2 记了很久的"从未打开过"），
三张图里只有一张画得出来。原因是两个分开的缺陷：

  ① `harness report --snapshot <别的目录>/latest.json` 只换了快照。
     失败模式与 judge 面板仍然从 `--runs`（默认 `runs/`）读 ——
     于是报告会把 **A 目录的图表**配上 **B 目录的指标**。
     两边都是真数据，所以读起来毫无异样，只是没人能从报告里看出来它们不同源。

  ② judge 未启用时雷达图 `return` 掉，留一个**空白框**。
     空白框与"图画崩了"在读者眼里完全一样。

两个都属于"自动化测试发现不了"的那一类：断言的是**有没有那段 HTML/JS**，
而缺陷在于**那段 HTML/JS 拿到的数据是从哪来的**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.report import data as report_data


def _snapshot(path: Path, *, suite: str = "s") -> Path:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "snapshot_version": 1, "suite_name": suite,
        "aggregate": {"cases": 1, "pass_rate": 1.0},
        "runs": [{"case_id": "a", "cost_usd": 0.1, "pass_rate": 1.0}],
    }), encoding="utf-8")
    return path


# ---- ① 快照在哪，配套的面板数据就从哪读 ----
def test_panels_are_read_from_the_snapshots_own_directory(tmp_path, monkeypatch):
    """★★ `--snapshot` 指向别处时，失败模式与 judge 必须从**那个**目录读。

    不这么做的话，报告会把 A 目录的图表配上 B 目录的指标 ——
    两边都是真数据，所以没有一处会报错，也没有一处看起来可疑。

    ★ 写法说明：这里替换掉两个面板函数并记录**它们收到的目录**，
    而不是去断言图表里的数字。要测的是"从哪读"，不是"读到了什么"——
    后者需要造一个真的 sqlite 索引，而它测不出这个错。
    """
    runs_a = tmp_path / "a"
    runs_b = tmp_path / "b"
    snap_b = _snapshot(runs_b / "latest.json")

    seen: list[Path] = []

    def _spy(dir_path, *a, **kw):
        seen.append(Path(dir_path))
        return {} if dir_path == runs_b else {"WRONG": 1}

    monkeypatch.setattr(report_data, "_failure_modes", _spy)
    monkeypatch.setattr(report_data, "_judge_panel",
                        lambda d: (seen.append(Path(d)), None)[1])

    out = report_data.collect(runs_a, snapshot=snap_b)

    assert seen == [runs_b, runs_b], (
        f"面板数据应该从快照所在的目录读，实际读了 {seen}")
    # 指标本身仍然来自快照（它才是权威）
    assert out["aggregate"]["cases"] == 1
    assert out["failure_modes"] == {}


def test_without_an_explicit_snapshot_the_runs_dir_is_used(tmp_path, monkeypatch):
    """守卫：默认路径不能因为上面的改动而变。"""
    runs = tmp_path / "r"
    _snapshot(runs / "latest.json")
    seen: list[Path] = []
    monkeypatch.setattr(report_data, "_failure_modes",
                        lambda d: (seen.append(Path(d)), {})[1])
    monkeypatch.setattr(report_data, "_judge_panel",
                        lambda d: (seen.append(Path(d)), None)[1])

    report_data.collect(runs)
    assert seen == [runs, runs]


# ---- ② judge 未启用要有空态 ----
def test_the_judge_chart_says_why_it_is_empty(tmp_path):
    """★ 空白框与"图画崩了"在读者眼里完全一样。

    本次真跑没有启用 judge（suite 里默认关闭），于是雷达图那个容器
    是一个纯白方块 —— 打开报告的人无从判断是"没数据"还是"坏了"。
    """
    from harness.report.html import render_report

    data = {
        "suite_name": "s",
        "aggregate": {"cases": 1, "pass_rate": 1.0, "total_reasoning_tokens": 0},
        "cases": [{"case_id": "a", "tier": "easy", "status": "ok",
                   "repeats": 1, "golden_score": 1.0,
                   "cost_usd": 0.1, "pass_rate": 1.0}],
        "failure_modes": {"step_repetition": 3},
        "judge": None,
    }
    html = render_report(data, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "未启用 judge" in html, "judge 缺失时应当是**说明**，不是一个空白框"


def test_the_judge_chart_renders_when_there_is_data(tmp_path):
    """反向守卫：有 judge 数据时不能反过来只显示占位文案。"""
    from harness.report.html import render_report

    data = {
        "suite_name": "s",
        "aggregate": {"cases": 1, "pass_rate": 1.0, "total_reasoning_tokens": 0},
        "cases": [{"case_id": "a", "tier": "easy", "status": "ok",
                   "repeats": 1, "golden_score": 1.0,
                   "cost_usd": 0.1, "pass_rate": 1.0}],
        "failure_modes": {"step_repetition": 3},
        "judge": {"consistency": 0.9, "cost_usd": 0.05, "injection_resistance": 1.0},
    }
    html = render_report(data, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "judge 可靠性" in html
    assert "未启用 judge" not in html


def test_an_empty_failure_mode_chart_also_says_so(tmp_path):
    """同一个毛病在左图上也存在：没有失败模式时是一条空轴线。

    它比空白框更糟 —— 空轴线看起来像"画出来了，只是没有数据"，
    而读者会把它读成"这次跑没有任何失败模式"，那是**另一个结论**。
    """
    from harness.report.html import render_report

    data = {
        "suite_name": "s",
        "aggregate": {"cases": 1, "pass_rate": 1.0, "total_reasoning_tokens": 0},
        "cases": [{"case_id": "a", "tier": "easy", "status": "ok",
                   "repeats": 1, "golden_score": 1.0,
                   "cost_usd": 0.1, "pass_rate": 1.0}],
        "failure_modes": {},
        "judge": None,
    }
    html = render_report(data, tmp_path / "r.html").read_text(encoding="utf-8")
    assert "没有失败模式" in html


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
