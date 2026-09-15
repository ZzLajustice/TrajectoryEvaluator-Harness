"""M1 端到端验收：`harness run` 能跑通一条完整任务并落下轨迹。

这是 M1 的价值所在 —— 把事件模型、Trajectory、Tool、store、loop、CLI
串成一条**可运行**的路径。全离线，零网络调用。
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO = REPO_ROOT / "examples" / "hello.yaml"


def _run_hello(out: Path):
    return CliRunner().invoke(
        app, ["run", "--suite", str(HELLO), "--out", str(out)]
    )


def test_hello_suite_runs_end_to_end(tmp_path):
    result = _run_hello(tmp_path)
    assert result.exit_code == 0, result.output

    logs = list(tmp_path.glob("*.jsonl"))
    assert len(logs) == 1, f"expected exactly one trajectory, got {logs}"


def test_trajectory_has_the_full_event_stream(tmp_path):
    _run_hello(tmp_path)
    events = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    types = [e["type"] for e in events]
    assert types[0] == "run.start"
    assert types[-1] == "run.end"
    for expected in ("turn.start", "llm.request", "llm.response",
                     "tool.call", "tool.result"):
        assert expected in types, f"missing {expected} in {types}"


def test_seq_is_contiguous_from_zero(tmp_path):
    _run_hello(tmp_path)
    events = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [e["seq"] for e in events] == list(range(len(events)))


def test_run_start_carries_the_spec_snapshot(tmp_path):
    """轨迹必须自解释 —— 评测器不该需要回查 suite 配置。"""
    _run_hello(tmp_path)
    first = json.loads(
        next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    )
    assert first["role"] == "sut"
    spec = json.loads(first["spec_json"])
    assert spec["system_prompt"]
    assert spec["budget"]["max_turns"] == 5


def test_run_summary_is_printed(tmp_path):
    result = _run_hello(tmp_path)
    assert "ok" in result.output
    assert "turns=" in result.output


def test_trace_command_prints_the_event_stream(tmp_path):
    runner = CliRunner()
    runner.invoke(app, ["run", "--suite", str(HELLO), "--out", str(tmp_path)])
    run_id = next(tmp_path.glob("*.jsonl")).stem

    result = runner.invoke(app, ["trace", "--run-id", run_id, "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "tool.call" in result.output
    assert "finish" in result.output


def test_trace_on_unknown_run_exits_nonzero(tmp_path):
    result = CliRunner().invoke(
        app, ["trace", "--run-id", "nope", "--out", str(tmp_path)]
    )
    assert result.exit_code != 0


def test_missing_suite_exits_with_config_error(tmp_path):
    result = CliRunner().invoke(
        app, ["run", "--suite", str(tmp_path / "nope.yaml"), "--out", str(tmp_path)]
    )
    assert result.exit_code == 2  # 约定：2 = 配置或加载错误


def test_deps_module_is_importable_as_the_assembly_point():
    """任务 11 的最小装配层 —— 任务 27 会扩展它，但接口从一开始就成立。"""
    from harness.orchestration.deps import RunBuilder

    assert RunBuilder(out_dir=Path("runs")) is not None


def test_sut_gets_the_full_documented_tool_set():
    """设计文档 §3.3 规定了 6 个 SUT 工具。

    工具集不完整时，测出来的不是模型能力而是**环境限制** ——
    少一个 `search`，agent 就只能靠 list_dir 逐个目录翻。
    """
    from harness.orchestration.deps import build_tool_registry

    assert build_tool_registry().names() == [
        "finish", "list_dir", "read_file", "run_command", "search", "write_file",
    ]


def test_hello_run_records_the_tools_it_offered(tmp_path):
    """轨迹必须记下当次暴露了哪些工具 —— 否则结果不可解释。"""
    _run_hello(tmp_path)
    first = json.loads(
        next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    )
    assert set(first["tools"]) == {
        "finish", "list_dir", "read_file", "run_command", "search", "write_file",
    }


# ---- 录制 / 回放 ----
def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


def test_record_then_replay_is_reproducible(tmp_path):
    """录制后离线回放，结果必须一致 —— 这是评测可信度的基础。"""
    cassette = tmp_path / "cassette.json"
    run_dir = tmp_path / "runs"

    rec = _invoke(["run", "--suite", str(HELLO), "--out", str(run_dir),
                   "--record", str(cassette)])
    assert rec.exit_code == 0, rec.output
    assert cassette.exists(), "record must persist the cassette"

    replay_dir = tmp_path / "replayed"
    rep = _invoke(["run", "--suite", str(HELLO), "--out", str(replay_dir),
                   "--replay", str(cassette)])
    assert rep.exit_code == 0, rep.output

    # 两次运行的轨迹除时间戳与 run_id 外应完全一致
    def _events(d: Path) -> list[dict]:
        f = next(d.glob("*.jsonl"))
        return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    a, b = _events(run_dir), _events(replay_dir)
    assert [e["type"] for e in a] == [e["type"] for e in b]
    assert [e["seq"] for e in a] == [e["seq"] for e in b]


def test_replay_without_a_cassette_fails_loudly(tmp_path):
    """没录过就回放必须报错，而不是静默返回空响应。"""
    result = _invoke(["run", "--suite", str(HELLO), "--out", str(tmp_path / "o"),
                      "--replay", str(tmp_path / "absent.json")])
    assert result.exit_code == 2


def test_record_and_replay_are_mutually_exclusive(tmp_path):
    result = _invoke(["run", "--suite", str(HELLO), "--out", str(tmp_path / "o"),
                      "--record", str(tmp_path / "a.json"),
                      "--replay", str(tmp_path / "b.json")])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


# ---- 中间件真的接进了端到端路径 ----
def test_configured_middlewares_do_not_break_a_normal_run(tmp_path):
    """hello.yaml 启用了四个中间件 —— 正常路径不能被拦死。"""
    result = _run_hello(tmp_path)
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_task_prompt_from_suite_reaches_the_model(tmp_path):
    """Suite 里的 task 必须出现在轨迹的 RUN_START 上。

    缺了它评测器无法知道这个 run 在做什么。
    """
    _run_hello(tmp_path)
    first = json.loads(
        next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    )
    assert first["task"] == "Say hello and finish."
