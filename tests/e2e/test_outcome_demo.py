"""`examples/outcome.yaml` —— 结果级评测的离线演示，必须真的演示出那件事。

## 这个演示本身就是一条论证，所以它得有人看着

README 里写着「6 个评测器必须有第 6 个是结果级的」，理由是那类
"过程完全干净但活没干"的答案只有隐藏测试戳得穿。`examples/outcome.yaml`
就是那件事的可运行版本 —— 不配 API key 也能跑给任何人看。

而**演示会烂**：路径改了、补丁改写了、假脚本与真实 API 漂移了，
症状是两条用例都 PASS 或都 FAIL —— 那时它就不再演示任何东西，
而命令仍然"跑通了"（退出码 0）。

所以这里钉住三件事：

  ① 两条用例的**结果级判定必须相反**（一条 PASS、一条 FAIL）
  ② 两条的**轨迹级终态必须相同**（都是 `ok`）—— 这是全篇的重点
  ③ 第二条的失败必须是 `outcome.hidden_tests_failed`，不是 ERROR

第 ② 条尤其重要：如果哪天第二条变成 `no_finish` 之类，它仍然会 FAIL，
但那时它证明的是"agent 没收工"，而不是"过程干净但活没干"——
**论证被换掉了，而测试仍然绿**。
"""

from __future__ import annotations

import pathlib

from harness.contracts.results import EvalResult
from harness.contracts.spec import RunStatus
from harness.orchestration.deps import RunBuilder, RunOutcome

REPO = pathlib.Path(__file__).resolve().parents[2]
SUITE = REPO / "examples" / "outcome.yaml"

_CASE_PASSES = "outcome_the_fix_lands"
_CASE_CLAIMS = "outcome_a_claim_is_not_a_fix"


def _run(tmp_path) -> dict[str, RunOutcome]:
    outcomes = RunBuilder(
        out_dir=tmp_path / "runs", workdir=tmp_path / "wd"
    ).run_suite_sync(SUITE, evaluate=True)
    return {(o.case_id or o.result.run_id): o for o in outcomes}


def _outcome(outcome: RunOutcome) -> EvalResult:
    return next(e for e in outcome.evals if e.evaluator == "OutcomeGrader")


def test_the_two_cases_disagree_on_the_result(tmp_path):
    """★★ 同一个 bug、同一个工作目录、同一个评测器 —— 结果必须相反。

    这是这条演示的全部内容。两边都 PASS（补丁没打上）或都 FAIL
    （假脚本没改成功）时，它就不再演示任何东西了。
    """
    got = _run(tmp_path)
    fixed = _outcome(got[_CASE_PASSES])
    claimed = _outcome(got[_CASE_CLAIMS])

    assert fixed.status.value == "pass", fixed.model_dump_json(indent=2)
    assert claimed.status.value == "fail", claimed.model_dump_json(indent=2)


def test_the_two_cases_look_identical_at_the_process_level(tmp_path):
    """★★ 而且它们**在过程上分不出来** —— 这才是重点。

    两条的 run 终态都是 `ok`（agent 正常收了工）、调用次数相同、
    token 数相同。只看过程或只看效率，这是一个五五开的问题；
    只有跑一遍隐藏测试才知道哪条真的改了代码。

    ★ 断言终态而不是只断言"都跑完了"：如果哪天第二条变成 `no_finish`，
    它仍然会 FAIL，但那时它证明的是"agent 没收工"——
    **论证被悄悄换掉了，而上面那条测试照样绿。**
    """
    got = _run(tmp_path)
    a, b = got[_CASE_PASSES].result, got[_CASE_CLAIMS].result

    assert a.status is RunStatus.OK, a.status
    assert b.status is RunStatus.OK, b.status
    assert a.tool_calls == b.tool_calls
    assert (a.usage.input_tokens + a.usage.output_tokens
            == b.usage.input_tokens + b.usage.output_tokens)


def test_the_claim_fails_because_the_hidden_tests_failed(tmp_path):
    """★ 失败的理由必须**正是**隐藏测试没过，不是用例坏了。

    `FAIL` 与 `ERROR` 都是红的，但前者说"agent 没做对"、后者说"评测自己炸了"。
    混为一谈的话，这条演示会在自己的机制坏掉时**继续看起来有效**。
    """
    claimed = _outcome(_run(tmp_path)[_CASE_CLAIMS])
    assert claimed.metrics["outcome_pass"] == 0.0
    codes = {f.code for f in claimed.findings}
    assert "outcome.hidden_tests_failed" in codes, codes
