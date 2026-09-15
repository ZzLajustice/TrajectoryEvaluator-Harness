"""M5 端到端验收：`harness run --evaluate` 输出评测器结果。

这一层测的不是评测器**算法**（那在 tests/evaluators/ 里），而是
「声明式配置 → 装配 → 调度 → 打印」这条链路真的通了。
算法正确但没接进 CLI，等于没做。
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO = REPO_ROOT / "examples" / "hello.yaml"


def _invoke(args: list[str]):
    return CliRunner().invoke(app, args)


def _run_hello(out: Path, *extra: str):
    return _invoke(["run", "--suite", str(HELLO), "--out", str(out),
                    "--workdir", str(out / "wd"), *extra])


def test_evaluate_prints_both_evaluators(tmp_path):
    """M5 的验收判据原文：输出 2 个评测器结果。"""
    result = _run_hello(tmp_path, "--evaluate")
    assert result.exit_code == 0, result.output
    assert "TrajectoryMatcher" in result.output
    assert "EfficiencyAnalyzer" in result.output


def test_evaluate_reports_pass_for_a_correct_run(tmp_path):
    """hello.yaml 的 fake_script 正好是 golden 路径 —— 必须判 PASS 而不是 FAIL。"""
    result = _run_hello(tmp_path, "--evaluate")
    assert "PASS" in result.output
    assert "FAIL" not in result.output


def test_without_the_flag_nothing_is_evaluated(tmp_path):
    """跑 agent 与评 agent 是两件事，默认不评。"""
    result = _run_hello(tmp_path)
    assert result.exit_code == 0, result.output
    assert "TrajectoryMatcher" not in result.output


def test_a_wrong_golden_makes_the_matcher_fail(tmp_path):
    """反向用例 —— 评测器必须真的在判，而不是无脑回 PASS。

    用一条期望 `read_file` 的 golden 去匹配只调了 `finish` 的轨迹。
    """
    suite = tmp_path / "wrong.yaml"
    payload = HELLO.read_text(encoding="utf-8").replace(
        "- [finish, {summary: \"hello\"}]", "- [read_file, {path: \"nope.py\"}]"
    )
    suite.write_text(payload, encoding="utf-8", newline="\n")

    result = _invoke(["run", "--suite", str(suite), "--out", str(tmp_path / "o"),
                      "--evaluate"])
    assert result.exit_code == 0, result.output
    assert "FAIL" in result.output
    # 发现项要打印出来，否则报告只有结论没有理由
    assert "trajectory" in result.output


def test_unknown_evaluator_name_is_a_config_error(tmp_path):
    """拼错评测器名必须报错 —— 静默忽略会让人以为评测跑了。"""
    suite = tmp_path / "typo.yaml"
    payload = HELLO.read_text(encoding="utf-8").replace(
        "name: TrajectoryMatcher", "name: TrajectoryMatchr"
    )
    suite.write_text(payload, encoding="utf-8", newline="\n")

    result = _invoke(["run", "--suite", str(suite), "--out", str(tmp_path / "o"),
                      "--evaluate"])
    assert result.exit_code == 2
    assert "unknown grader" in result.output


def test_a_broken_evaluator_reports_error_not_fail(tmp_path):
    """评测器自己抛异常必须记 ERROR —— 记成 FAIL 会让 agent 背锅。"""
    suite = tmp_path / "broken.yaml"
    payload = HELLO.read_text(encoding="utf-8").replace(
        "mode: strict", "mode: not-a-real-mode"
    )
    suite.write_text(payload, encoding="utf-8", newline="\n")

    result = _invoke(["run", "--suite", str(suite), "--out", str(tmp_path / "o"),
                      "--evaluate"])
    # 评测器崩了不该让整个 run 失败 —— CLI 仍然正常退出
    assert result.exit_code == 0, result.output
    assert "ERROR" in result.output
    assert "crashed" in result.output


def test_unknown_grader_is_rejected_even_without_the_flag(tmp_path):
    """配置错误在**加载期**暴露，与开不开 `--evaluate` 无关。

    这是 M5 到 M6 的一次语义收紧。M5 时 graders 只在 `--evaluate` 时才解析，
    于是同一个拼错的名字，加不加 flag 是两种行为 —— 而"配置校验取决于你按了哪个
    开关"本身就是个陷阱：真跑起来才发现 typo，前面几条用例的钱已经花了。
    """
    suite = tmp_path / "typo.yaml"
    # 用一个**真的不存在**的名字 —— 之前借用了 FailureClassifier，
    # 它在 M7 注册之后这条测试就失去了意义（会变成"合法配置被误拒"）
    payload = HELLO.read_text(encoding="utf-8").replace(
        "name: TrajectoryMatcher", "name: NoSuchGrader"
    )
    suite.write_text(payload, encoding="utf-8", newline="\n")

    result = _invoke(["run", "--suite", str(suite), "--out", str(tmp_path / "o"),
                      "--workdir", str(tmp_path / "wd")])
    assert result.exit_code == 2
    assert "unknown grader" in result.output


def test_outcomes_pair_the_run_with_its_evals(tmp_path):
    """装配层返回配对好的对象 —— 让调用方自己配迟早会错位且不报错。"""
    from harness.orchestration.deps import RunBuilder

    outcomes = RunBuilder(out_dir=tmp_path, workdir=tmp_path / "wd").run_suite_sync(HELLO, evaluate=True)
    assert len(outcomes) == 1

    outcome = outcomes[0]
    assert outcome.result.status.value == "ok"
    assert len(outcome.evals) == 2
    # 每个结果都必须指向同一条 run，否则报告会张冠李戴
    for ev in outcome.evals:
        assert ev.run_id == outcome.result.run_id


def test_evaluation_does_not_perturb_the_run(tmp_path):
    """评测是**旁观者**：开不开评测，被测 run 自身的结果必须一模一样。

    如果打开评测后 run 的终态变了，那说明评测器通过某种共享状态
    反向影响了 agent —— 整份报告的可信度就没了。
    """
    from harness.orchestration.deps import RunBuilder

    plain = RunBuilder(out_dir=tmp_path / "a", workdir=tmp_path / "wd").run_suite_sync(HELLO)[0].result
    graded = RunBuilder(out_dir=tmp_path / "b", workdir=tmp_path / "wd").run_suite_sync(HELLO, evaluate=True)[0].result

    assert plain.status == graded.status
    assert plain.turns == graded.turns
    assert plain.tool_calls == graded.tool_calls
    assert plain.usage == graded.usage


def test_trajectory_stays_read_only_after_evaluation(tmp_path):
    """评测器只拿到 tuple 视图 —— 想改也改不了（架构层面的只读保证）。"""
    from harness.orchestration.deps import RunBuilder

    outcome = RunBuilder(out_dir=tmp_path, workdir=tmp_path / "wd").run_suite_sync(HELLO, evaluate=True)[0]
    assert isinstance(outcome.result.trajectory.events, tuple)
    assert outcome.result.trajectory.run_id == outcome.result.run_id
