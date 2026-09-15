"""M6 端到端验收：`--concurrency 8` 跑 5 条用例，且数据互不串。

## 为什么这些测试值得写

并发跑 suite 最危险的失败不是"报错"，而是**数据张冠李戴**：
两条 run 撞了 run_id、store 串了、轨迹写进同一个文件 ——
外表看是"跑完了、有结果"，但报告里的每一条都可能对应错的用例。
所以这里除了断言条数，还断言每条 run 的轨迹内容与它自己的 case 对得上。
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
CONCURRENCY = REPO_ROOT / "examples" / "concurrency.yaml"

CASE_IDS = ["c1_read", "c2_write", "c3_search", "c4_repeat", "c5_no_finish"]


def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


def _run(out: Path, *extra: str):
    return _invoke(["run", "--suite", str(CONCURRENCY), "--out", str(out),
                    "--workdir", str(out / "wd"), *extra])


def _events(out: Path, run_id: str) -> list[dict]:
    path = out / f"{run_id}.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_five_cases_all_run(tmp_path):
    result = _run(tmp_path)
    assert result.exit_code == 0, result.output
    for case_id in CASE_IDS:
        assert case_id in result.output, f"{case_id} 没有出现在输出里"
    # 每条 case 一条轨迹 —— 少一条就是静默丢数据
    assert len(list(tmp_path.glob("*.jsonl"))) == 5


def test_every_run_gets_a_distinct_run_id(tmp_path):
    """★ 毫秒时间戳单独当 id 是不够的。

    调度器在同一个事件循环 tick 里创建全部 run，`time.time()` 完全相同。
    撞了的话 5 条 case 会写进同一个 `<run_id>.jsonl`，轨迹互相覆盖。
    """
    _run(tmp_path)
    files = list(tmp_path.glob("*.jsonl"))
    assert len({f.stem for f in files}) == 5


def test_trajectories_are_not_cross_contaminated(tmp_path):
    """★ 每条轨迹的工具调用必须与它自己那条 case 的 fake_script 一致。

    这才是并发出错的真正症状：不是崩溃，而是 A 的轨迹里出现 B 的调用序列。
    """
    _run(tmp_path)
    # 用 prompt 反查 case —— RUN_START 的 `task` 字段装的是提示词，不是 case_id
    expected = {
        "read then finish": ["list_dir", "finish"],
        "write then finish": ["write_file", "read_file", "finish"],
        "search then finish": ["search", "finish"],
        "list twice then finish": ["list_dir", "list_dir", "list_dir", "finish"],
    }

    seen: dict[str, list[str]] = {}
    for path in tmp_path.glob("*.jsonl"):
        events = _events(tmp_path, path.stem)
        prompt = next(e["task"] for e in events if e["type"] == "run.start")
        seen[prompt] = [e["name"] for e in events if e["type"] == "tool.call"]

    assert set(seen) == set(expected) | {"just talk"}, f"轨迹条数不对：{sorted(seen)}"
    for prompt, expected_tools in expected.items():
        assert seen[prompt] == expected_tools, f"{prompt!r} 的轨迹被串了"


def test_no_finish_case_is_recorded_as_a_failure_not_dropped(tmp_path):
    """没调 finish 的 case 以 no_finish 结束 —— 它必须照常出现在结果里。

    "失败的用例被静默丢掉"会让 pass_rate 虚高，是报告里最隐蔽的一类错误。
    """
    _run(tmp_path)
    for path in tmp_path.glob("*.jsonl"):
        events = _events(tmp_path, path.stem)
        prompt = next(e["task"] for e in events if e["type"] == "run.start")
        if prompt == "just talk":
            assert events[-1]["status"] == "no_finish"
            return
    raise AssertionError("c5_no_finish 的轨迹不存在 —— 失败的用例被丢掉了")


def test_concurrency_flag_overrides_the_suite_default(tmp_path):
    """`--concurrency` 覆盖 defaults，且不影响结果条数。"""
    result = _run(tmp_path, "--concurrency", "2")
    assert result.exit_code == 0, result.output
    assert len(list(tmp_path.glob("*.jsonl"))) == 5


def test_case_filter_runs_only_the_selected_case(tmp_path):
    result = _run(tmp_path, "--case", "c3_search")
    assert result.exit_code == 0, result.output
    assert len(list(tmp_path.glob("*.jsonl"))) == 1
    assert "c3_search" in result.output
    assert "c1_read" not in result.output


def test_unknown_case_id_is_a_config_error(tmp_path):
    """点了不存在的 case 却静默跑完全部，是最坏的行为 —— 钱花了，跑的不是你要的。"""
    result = _run(tmp_path, "--case", "nope")
    assert result.exit_code == 2
    assert "unknown case" in result.output


def test_index_is_populated_and_readable_after_the_run(tmp_path):
    """M6 验收的另一半：report 能从 SQLite 读回。

    这里直接开一个新的 CompositeStore 读 —— 证明数据真的落了盘，
    而不是只活在进程内存里。
    """
    from harness.store.composite import CompositeStore

    _run(tmp_path, "--evaluate")

    async def read() -> tuple[int, int]:
        store = CompositeStore(root=tmp_path)
        rows = await store.query_runs()
        evals = await store.get_evals(rows[0]["run_id"])
        await store.close()
        return len(rows), len(evals)

    import asyncio

    n_runs, n_evals = asyncio.run(read())
    assert n_runs == 5
    assert n_evals >= 1, "评测结果没有落索引，M8 的聚合报告将无从读起"


def test_run_end_status_is_recorded_in_the_index(tmp_path):
    """索引里的 status 必须从 running 更新为终态 —— 否则看板上全是"在跑"。"""
    import asyncio

    from harness.store.composite import CompositeStore

    _run(tmp_path)

    async def read() -> list[dict]:
        store = CompositeStore(root=tmp_path)
        rows = await store.query_runs()
        await store.close()
        return rows

    rows = asyncio.run(read())
    assert len(rows) == 5
    assert all(r["status"] != "running" for r in rows), rows


def test_output_labels_each_run_with_its_case_id(tmp_path):
    """并发 5 条时，光有 run_id 无法把某行和某条用例对上。"""
    result = _run(tmp_path)
    assert "c1_read" in result.output
    assert "c5_no_finish" in result.output
