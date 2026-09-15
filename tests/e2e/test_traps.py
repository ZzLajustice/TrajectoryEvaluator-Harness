"""M7 验收：规则分类器对 `expected_failure_modes` 的命中率 ≥70%。

## 为什么这个数字可信

`examples/traps.yaml` 里每一类失败都是**用 fake_script 构造出来的**，
不是等模型碰运气碰上的。脚本化 provider 跑一百次都是同一个失败，
所以命中率度量的是分类器，而不是模型的随机性。

真实模型上的命中率会低于这里 —— 那是另一回事（分类器的召回率），
需要真实失败样本才能测。这里测的是**分类器对已知失败的识别能力**，
是它的上界。把这一点写清楚，比给一个漂亮的数字重要。

## 命中率怎么算

    命中率 = 被检出的声明模式数 / 声明的模式总数

分母是 suite 里所有 `expected_failure_modes` 的并集大小，不是 case 数 ——
一条 case 声明两个模式却只检出一个，那是一次半命中，不该算全中。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.contracts.results import EvalStatus
from harness.orchestration.deps import RunBuilder
from harness.orchestration.suite import load_suite

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAPS = REPO_ROOT / "examples" / "traps.yaml"

# 验收线（计划原文）。低于它说明规则层没有覆盖它声称覆盖的模式。
_MIN_HIT_RATE = 0.70


def _run_traps(tmp_path):
    return RunBuilder(out_dir=tmp_path, workdir=tmp_path / "wd").run_suite_sync(
        TRAPS, evaluate=True)


def _taxonomy_report(tmp_path) -> tuple[float, list[str]]:
    """跑完 traps 并算出命中率与逐条明细。"""
    suite = load_suite(TRAPS)
    declared = {c.case_id: set(c.expected_failure_modes) for c in suite.cases}
    outcomes = _run_traps(tmp_path)

    total = sum(len(m) for m in declared.values())
    hit = 0
    detail: list[str] = []

    for outcome in outcomes:
        want = declared.get(outcome.case_id, set())
        if not want:
            continue
        got: set[str] = set()
        for ev in outcome.evals:
            if ev.status is EvalStatus.ERROR:
                pytest.fail(f"{outcome.case_id}: 评测器崩了 —— {ev.error}")
            got |= {f.category for f in ev.findings if f.category}
        found = want & got
        hit += len(found)
        missing = sorted(want - found)
        mark = "OK  " if not missing else "MISS"
        detail.append(f"{mark} {outcome.case_id}: want={sorted(want)} got={sorted(got)}")
    return (hit / total if total else 0.0), detail


def test_failure_taxonomy_hit_rate_meets_the_acceptance_bar(tmp_path):
    rate, detail = _taxonomy_report(tmp_path)
    report = "\n".join(detail)
    assert rate >= _MIN_HIT_RATE, (
        f"命中率 {rate:.0%} 低于验收线 {_MIN_HIT_RATE:.0%}\n{report}"
    )


def test_every_engineered_trap_is_detected(tmp_path):
    """构造出来的失败必须**全部**检出 —— 漏一个就说明那条规则没生效。

    比 ≥70% 更严的一条：这些是确定性构造的失败，没有理由漏。
    70% 那条线是留给真实模型样本的。
    """
    rate, detail = _taxonomy_report(tmp_path)
    misses = [line for line in detail if line.startswith("MISS")]
    assert rate == 1.0, "有构造失败未被检出：\n" + "\n".join(misses)


def test_no_rule_fires_on_a_clean_run(tmp_path):
    """★ 反向验收：干净轨迹上一条规则都不该命中。

    只有正例的测试无法证明分类器在**判断** —— 一个恒返回"命中"的分类器
    能通过上面两条测试的 100%。这条是防它的。

    用 `hello.yaml` 而不是 `concurrency.yaml`：后者的 c4 真的重复调了 3 次、
    c5 真的没调 finish，被检出是**正确行为**。拿它当基线会把正确检出当成误报。
    """
    from harness.contracts.results import EvalStatus as _S

    outcomes = RunBuilder(out_dir=tmp_path, workdir=tmp_path / "wd").run_suite_sync(
        REPO_ROOT / "examples" / "hello.yaml", evaluate=True)

    noisy = [
        (o.case_id, ev.evaluator, [f.category for f in ev.findings])
        for o in outcomes for ev in o.evals
        if ev.evaluator == "FailureClassifier" and ev.status is not _S.PASS
    ]
    assert not noisy, f"干净轨迹上误报了失败模式：{noisy}"


def test_grounding_checker_catches_the_fabricated_result(tmp_path):
    """`fm_fabricated_result` 考的是"输出说 1 passed，agent 说 12 passed"。

    这条是过程级评测最直观的价值：结论看起来完全正常（run 状态 ok、
    工具调用无误、`finish` 也调了），只有把 assistant 的话和工具原文
    放在一起看才能发现它在编。
    """
    outcomes = _run_traps(tmp_path)
    outcome = next(o for o in outcomes if o.case_id == "fm_fabricated_result")
    grounding = next(ev for ev in outcome.evals if ev.evaluator == "GroundingChecker")

    assert grounding.status is EvalStatus.FAIL
    codes = {f.code for f in grounding.findings}
    assert "grounding.fabricated_test_result" in codes


def test_the_two_classifiers_do_not_overlap(tmp_path):
    """职责边界：两者管的是不同的事。

    GroundingChecker 管"说的话与观测不符"，
    FailureClassifier 管"行为模式本身有问题"。

    一个 run 被前者判 FAIL 不该自动被后者也判 FAIL —— 二者可以同时说话，
    但必须是各自独立判断出来的，而不是互相传染。
    """
    outcomes = _run_traps(tmp_path)
    outcome = next(o for o in outcomes if o.case_id == "fm_fabricated_result")
    by_name = {ev.evaluator: ev for ev in outcome.evals}

    assert by_name["GroundingChecker"].status is EvalStatus.FAIL
    # TrajectoryMatcher 对这条轨迹应当是 PASS —— 工具调用序列本身没问题
    assert by_name["TrajectoryMatcher"].status is EvalStatus.PASS


def test_expected_modes_narrow_the_findings(tmp_path):
    """声明了 expected_modes 的 case，findings 里只该有声明过的模式。

    traps.yaml 里多条 case 会**同时**触发好几个模式（比如反复读不存在的文件
    既是 `step_repetition` 也是 `hallucinated_tool_args`），
    所以这条断言有内容：它证明过滤真的在起作用。
    """
    suite = load_suite(TRAPS)
    declared = {c.case_id: set(c.expected_failure_modes) for c in suite.cases}

    checked = 0
    for outcome in _run_traps(tmp_path):
        want = declared.get(outcome.case_id, set())
        for ev in outcome.evals:
            if ev.evaluator != "FailureClassifier":
                continue
            checked += 1
            got = {f.category for f in ev.findings}
            assert got <= want, f"{outcome.case_id} 的 findings 混进了未声明的模式：{got - want}"
    assert checked >= 8, f"只检查到 {checked} 条分类结果，用例数不对"


def test_hidden_modes_are_still_counted(tmp_path):
    """被过滤掉的模式数量要记进 `unexpected_modes`，不能直接丢掉。

    这个数字本身是信号：说明这条用例**实际的失败方式和设计时想考的不是一回事**。
    用例没按预期失败时，最该知道这件事的是写用例的人。
    """
    outcomes = _run_traps(tmp_path)
    metrics = [ev.metrics for o in outcomes for ev in o.evals
               if ev.evaluator == "FailureClassifier"]

    assert len(metrics) >= 8
    assert any(m.get("unexpected_modes", 0) > 0 for m in metrics), (
        "没有任何一条 case 报出额外模式 —— 要么用例不够互斥，要么指标没记"
    )
    # 同时，每条都必须检出了它声明的那个模式
    assert all(m.get("matched_modes", 0) >= 1 for m in metrics)
