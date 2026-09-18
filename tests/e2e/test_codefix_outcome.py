"""端到端：codefix 用例的**结果级评测**整条链路。

## 这条链路是新加的，而且是本项目最容易"看起来跑通了"的一条

它跨了五层：

    组装层改 keep → Run 建工作目录 → agent 改代码 → Run 结束但目录留着
    → 组装层把隐藏测试拷进去 → 造一个不调 setup() 的 Workspace
    → 复用 run_command 在沙箱里跑 pytest → OutcomeGrader 判 PASS/FAIL
    → 组装层按需清理目录

每一环单独测都过、连起来不工作，是这类链路的典型失败方式 ——
而症状是 OutcomeGrader 拿到 SKIPPED（"没有 runner"），
在报告里与"这条用例没配结果级评测"长得一样。

## 用 FakeProvider 跑，所以离线且零成本

FakeProvider 的脚本用 `run_command` 真的去改文件 —— 这样被测的
"agent 产物"是真的落在磁盘上的，与真模型跑出来的形状一致。
（用 `write_file` 也行，但那要求脚本里内嵌整份修正后的源码，
题面一改就要跟着改，维护成本高得多。）
"""

from __future__ import annotations

import sys

import pytest
import yaml

from harness.contracts.spec import ModelRef
from harness.orchestration.deps import RunBuilder
from harness.orchestration.suite import EvaluatorSpec, load_suite

REPO = __import__("pathlib").Path(__file__).resolve().parents[2]
CASE_ID = "bug_mean_offbyone"

# ★ `encoding='utf-8'` 是必须的：`Path.read_text()` 不传编码时用**本地**编码，
# 在中文 Windows 上就是 GBK，读这个含中文注释的 UTF-8 文件会当场
# `UnicodeDecodeError`。而报错发生在被测 agent 的脚本里，
# 表现成"agent 改不动文件"，与本用例要考的东西毫无关系。
_SRC = "pathlib.Path('csvlite/stats.py')"
_ENC = "encoding='utf-8'"

# 真的把 bug 修掉：把 `(len(values) - 1)` 换回 `len(values)`
_FIX = (f"import pathlib; p = {_SRC}; "
        f"p.write_text(p.read_text({_ENC}).replace('(len(values) - 1)', 'len(values)'), {_ENC})")
# 只改注释、声称修好了 —— 这正是"过程漂亮但没修好"的典型
_FAKE_FIX = (f"import pathlib; p = {_SRC}; "
             f"p.write_text(p.read_text({_ENC}) + chr(10) + '# fixed' + chr(10), {_ENC})")
# 什么都不做
_NOOP = "print('nothing to do')"


def _suite(tmp_path, script: list[dict], *, graders: list[dict] | None = None):
    """加载**真正的** `suites/codefix`，只把 provider 换成假的和注入脚本。

    ★ 曾经这里是手写一份 suite 文件（自己填 `source` / `patch` /
    `hidden_tests` 的绝对路径）。那样测出来的东西**不是**用户会跑到的东西：
    手写版把 `source` 填对了，而生成出来的 `case.yaml` 当时**漏了这一项** ——
    于是测试全绿，而真实运行拿到的是一个空工作目录，
    SUT 对着空气干活，报告上写"模型不会修 bug"。

    改成加载真 suite 之后，"case.yaml 里的配置能不能真的跑起来"
    才第一次进入了测试范围。**测试替身与被测对象的差别，就是漏掉 bug 的地方。**
    """
    suite = load_suite(REPO / "suites" / "codefix")
    suite.defaults.model = ModelRef(provider="fake", model="fake")
    suite.defaults.fake_script = script
    # 只留一条用例：这套 e2e 测的是"结果级评测的链路通不通"，
    # 不是 17 条用例的内容（那个由 tests/suites/ 的自检 + 真模型跑覆盖）
    suite.cases = [c for c in suite.cases if c.case_id == CASE_ID]
    if graders is not None:
        suite.cases[0].graders = [EvaluatorSpec(**g) for g in graders]
    return suite


def _outcome(evals):
    return next(e for e in evals if e.evaluator == "OutcomeGrader")


def _run(tmp_path, script, *, graders=None):
    suite = _suite(tmp_path, script, graders=graders)
    # evaluate=True 是必须的：默认不评测（不评就不付评测的代价），
    # 忘了传的表现是 evals 为空 —— 而"空"看起来像"评测器都跳过了"
    return RunBuilder(
        out_dir=tmp_path / "runs", workdir=tmp_path / "wd"
    ).run_suite_sync(suite, evaluate=True)[0]


# ---- 通过路径 ----
def test_the_agent_starts_with_a_populated_workspace(tmp_path):
    """★ 直接盯住那次事故的**症状**：SUT 开局拿到一个空目录。

    真实经过（真模型冒烟跑）：

        turn 0  list_dir(".")      → (empty)
                search("def mean") → no matches
        turn 1+ 一路向外找代码，撞了 12 轮沙箱边界

    报告上写的是"12 轮 / 27 次工具调用 / 未修复"，读起来完全是模型能力问题。
    根因在环境：`case.yaml` 漏了 `workspace.source`，于是没有东西可拷，
    而 `git apply` 在 git 仓库内部对目标不存在的补丁**静默跳过并返回 0**。

    这条断言故意做得又笨又直接 —— 它不问配置对不对，只问
    "agent 第一眼看到的东西里有没有那个包"。上面那一整串配置链路里的任何
    一环断掉，它都会红。
    """
    outcome = _run(tmp_path, [
        {"tool": "list_dir", "arguments": {"path": "."}},
        {"tool": "finish", "arguments": {"summary": "listed"}},
    ])
    listing = next(r for r in outcome.result.trajectory.tool_results()
                   if r.name == "list_dir")
    assert "csvlite" in listing.content, (
        f"the SUT started in an empty workspace:\n{listing.content}")


def test_the_workspace_already_contains_the_bug(tmp_path):
    """★ 除了有东西，还得是**带 bug 的**那个版本。

    上一条只保证目录非空。补丁没打上的话目录同样非空，而任务变成
    "这段代码本来是对的，你改什么？" —— 模型会去改一个不存在的问题。
    """
    outcome = _run(tmp_path, [
        {"tool": "read_file", "arguments": {"path": "csvlite/stats.py"}},
        {"tool": "finish", "arguments": {"summary": "read"}},
    ])
    source = next(r for r in outcome.result.trajectory.tool_results()
                  if r.name == "read_file")
    assert "(len(values) - 1)" in source.content, (
        f"bug.patch was not applied — the workspace holds the FIXED code:\n"
        f"{source.content[:400]}")


def test_the_bug_is_applied_even_when_the_workdir_is_inside_the_repo():
    """★★ 这条盯的是**默认配置**，而默认配置正是唯一会坏的那个。

    默认 `--workdir workdir` 落在**仓库内部**。而在仓库内部时，
    `git apply` 会把补丁里的路径解析到**外层仓库根**，找不到就
    `Skipped patch` 然后**返回 0** —— 补丁一行没打，调用方看到的是成功。

    于是：工作区里是**修好的**代码 → 模型花 12 轮找"要我修什么" →
    隐藏测试在正确代码上通过 → `OutcomeGrader` 报 **PASS（假阳性）**。

    而所有既有测试都在 `tmp_path` 里跑（仓库外，`git apply` 会老实报错），
    所以**只有这一条能抓到它**。这不是"再多一条测试"，而是唯一
    覆盖真实配置的那一条 —— 测试跑在哪个目录，决定了它测的是哪个 git。
    """
    wd = REPO / "workdir" / "_pytest_inrepo"
    try:
        outcome = RunBuilder(
            out_dir=wd.parent / "_pytest_runs", workdir=wd
        ).run_suite_sync(_suite(None, [
            {"tool": "read_file", "arguments": {"path": "csvlite/stats.py"}},
            {"tool": "finish", "arguments": {"summary": "read"}},
        ]), evaluate=True)[0]
        source = next(r for r in outcome.result.trajectory.tool_results()
                      if r.name == "read_file")
        assert "(len(values) - 1)" in source.content, (
            "the workspace is inside a git repo, and git apply silently "
            "skipped the patch — the SUT was handed already-correct code:\n"
            + source.content[:400])
        # 顺带确认结果级判据没被这个假阳性带偏
        assert _outcome(outcome.evals).status.value == "fail"
    finally:
        import shutil

        shutil.rmtree(wd, ignore_errors=True)
        shutil.rmtree(wd.parent / "_pytest_runs", ignore_errors=True)


def test_a_real_fix_passes_the_hidden_tests(tmp_path):
    """★ 整条链路的正向证据：改对 → 跑隐藏测试 → PASS。"""
    outcome = _run(tmp_path, [
        {"tool": "read_file", "arguments": {"path": "csvlite/stats.py"}},
        {"tool": "run_command", "arguments": {"argv": [sys.executable, "-c", _FIX]}},
        {"tool": "finish", "arguments": {"summary": "fixed"}},
    ])
    result = _outcome(outcome.evals)
    assert result.status.value == "pass", result.model_dump_json(indent=2)
    assert result.metrics["outcome_pass"] == 1.0


def test_the_hidden_test_actually_ran_not_just_skipped(tmp_path):
    """★ 区分"评了且通过"与"根本没评"。

    OutcomeGrader 在没有 runner 时返回 SKIPPED —— 而 SKIPPED 会让这条用例
    在 `pass_rate` 里被静默排除。所以正向用例必须确认它**真的跑了**：
    退出码 0 的判断只能来自一次真实执行。
    """
    outcome = _run(tmp_path, [
        {"tool": "run_command", "arguments": {"argv": [sys.executable, "-c", _FIX]}},
        {"tool": "finish", "arguments": {"summary": "fixed"}},
    ])
    result = _outcome(outcome.evals)
    assert result.status.value == "pass"
    assert "hidden tests passed" in result.summary


# ---- 失败路径（这条才是结果级评测存在的理由）----
def test_a_claim_without_a_fix_fails(tmp_path):
    """★ 只加了一行注释说"修好了" —— **必须**判失败。

    这是 5 个轨迹级评测器都抓不到的情形：
    没有重复调用、没有幻觉工具、没有绕过验证（它确实跑了命令），
    过程干净得很。只有真的跑一遍隐藏测试才能戳穿它。
    """
    outcome = _run(tmp_path, [
        {"tool": "run_command", "arguments": {"argv": [sys.executable, "-c", _FAKE_FIX]}},
        {"tool": "finish", "arguments": {"summary": "fixed the off-by-one"}},
    ])
    result = _outcome(outcome.evals)
    assert result.status.value == "fail"
    assert result.metrics["outcome_pass"] == 0.0
    assert result.findings[0].code == "outcome.hidden_tests_failed"


def test_a_no_op_agent_fails(tmp_path):
    """什么都不做也必须判失败 —— 防的是"用例抓不到自己的 bug"造成的假阳性。"""
    outcome = _run(tmp_path, [
        {"tool": "run_command", "arguments": {"argv": [sys.executable, "-c", _NOOP]}},
        {"tool": "finish", "arguments": {"summary": "done"}},
    ])
    assert _outcome(outcome.evals).status.value == "fail"


def test_the_failure_finding_carries_the_pytest_output(tmp_path):
    """证据要能直接看出"哪条测试挂了"，否则报告只是一句"没修好"。"""
    outcome = _run(tmp_path, [
        {"tool": "finish", "arguments": {"summary": "done"}},
    ])
    finding = _outcome(outcome.evals).findings[0]
    assert "test_mean" in finding.data["output"]


# ---- 与轨迹级评测器**并列**，不互相替代 ----
def test_outcome_and_process_graders_coexist(tmp_path):
    """★ 结果级与过程级**分列**（设计文档 §4.6）。

    合并成一个总分会让过程评测的意义被 outcome 淹没；
    而只有 outcome 则与直接跑 pytest 没有区别。
    """
    outcome = _run(tmp_path, [
        {"tool": "run_command", "arguments": {"argv": [sys.executable, "-c", _FIX]}},
        {"tool": "finish", "arguments": {"summary": "fixed"}},
    ], graders=[{"name": "OutcomeGrader"}, {"name": "EfficiencyAnalyzer"}])
    names = {e.evaluator for e in outcome.evals}
    assert {"OutcomeGrader", "EfficiencyAnalyzer"} <= names
    assert all(e.status.value != "error" for e in outcome.evals), [
        (e.evaluator, e.error) for e in outcome.evals]


# ---- 工作目录的生命周期 ----
def _run_dirs(tmp_path) -> list:
    """`<workdir>/<case_id>/` 下还剩几个 run 目录。"""
    return [p for p in (tmp_path / "wd" / CASE_ID).glob("*") if p.is_dir()]


def test_the_workspace_is_torn_down_after_grading(tmp_path):
    """★ 结果级评测需要 keep=True，但那是**临时的**。

    忘了清理的话 `workdir/` 会随每条用例无限长 ——
    而它本来只在失败时保留现场。成功路径上 run 目录必须消失。

    断言的是 **run 目录**（`wd/<case_id>/<run_id>`）而不是 case 目录：
    执行器的 teardown 只删前者，空的 case 目录会留下 ——
    这是原本就有的行为（`LocalExecutor.teardown` 只 rmtree root），
    这里刻意与它保持一致，而不是自己发明一套更"干净"的清理。
    """
    _run(tmp_path, [
        {"tool": "run_command", "arguments": {"argv": [sys.executable, "-c", _FIX]}},
        {"tool": "finish", "arguments": {"summary": "fixed"}},
    ])
    assert _run_dirs(tmp_path) == []


def test_a_failed_case_keeps_its_workspace(tmp_path):
    """失败时保留现场是这个项目一贯的调试策略 —— 结果级评测不该破坏它。

    ★ 用**不调 finish** 来造失败，而不是让隐藏测试失败：
      后者仍然是 `RunStatus.OK`（agent 正常收尾了，只是没修对），
      而 `keep_on_failure` 判的是 **run 的终态**，不是评测结果。
      两者混为一谈的话，这条测试会以为自己验了 keep-on-failure，
      实际验的是"成功路径也保留现场"—— 恰好相反。
    """
    outcome = _run(tmp_path, [{"text": "I think that should do it."}])
    assert outcome.result.status.value != "ok"
    assert len(_run_dirs(tmp_path)) == 1


def test_the_staged_hidden_test_is_not_visible_to_the_agent(tmp_path):
    """★ 隐藏测试在 **run 结束之后**才被拷进工作目录。

    提前放进去的话，被测 agent 直接读它就能拿到答案 ——
    而它还会留下 `_hidden/test_hidden.py` 这个显眼的路径。
    """
    outcome = _run(tmp_path, [
        {"tool": "run_command", "arguments": {"argv": [
            sys.executable, "-c",
            "import pathlib, sys; "
            "print(sorted(p.name for p in pathlib.Path('.').iterdir()))"]}},
        {"tool": "finish", "arguments": {"summary": "listed"}},
    ])
    listing = next(r for r in outcome.result.trajectory.tool_results()
                   if r.name == "run_command")
    assert "_hidden" not in listing.content, listing.content


# ---- 配置错误在加载期暴露 ----
def test_hidden_tests_without_an_outcome_grader_is_a_config_error(tmp_path):
    """★ 配了隐藏测试却没有评测器去跑它 = 那份测试永远不会执行。

    而"配了"看起来像"评了"。所以必须在加载期拦下（退出码 2），
    不能等跑完才发现指标里少了 outcome。

    ★ 这条测试**真的去加载一份 suite 文件**，而不是在已加载的对象上改字段。
      理由：校验器是 `model_validator`，而 pydantic **默认不在赋值时重跑它** ——
      在加载好的对象上 `case.graders = [...]` 会静默绕过全部校验。
      那样写出来的测试是绿的，而它想守的那条规则其实没被验到。
    """
    from harness.orchestration.suite import load_suite

    doc = {
        "name": "bad-suite",
        "defaults": {"model": {"provider": "fake", "model": "fake"}},
        "cases": [{
            "case_id": "bad",
            "task": {"case_id": "bad", "prompt": "x"},
            "workspace": {"kind": "tempdir"},
            # 配了隐藏测试，却没有任何评测器会去跑它
            "hidden_tests": str(tmp_path / "test_hidden.py"),
            "graders": [{"name": "EfficiencyAnalyzer"}],
        }],
    }
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")

    # pydantic 会把 validator 里抛的 `SuiteConfigError` 包成 `ValidationError`。
    # 两者都是 `ValueError`，而 CLI 的 `_CONFIG_ERRORS` 收的正是 `ValueError` ——
    # 所以退出码仍然是 2（"配置错误"而不是"agent 失败"），这条断言盯的就是它。
    with pytest.raises(ValueError, match="OutcomeGrader"):
        load_suite(path)


def test_a_patch_without_a_source_is_a_config_error(tmp_path):
    """★ 这条盯的是**真实发生过的事故**，值得单独一条测试。

    生成器漏了 `workspace.source` → 工作目录是空的 → 补丁的目标文件不存在
    → 而 `git apply` 在 git 仓库内部对目标不存在的补丁打出 "Skipped patch"
    后**返回 0**。于是 SUT 拿到一个空目录、一路对着空气干活，
    报告上写的是"模型不会修 bug"。

    四个门当时全绿 —— 因为整套测试都在仓库外的 `tmp_path` 里跑，
    而那里 `git apply` 会老实报错。所以这条错误必须在**加载期**就拦下，
    不能指望运行期的 git 行为。
    """
    from harness.orchestration.suite import load_suite

    doc = {
        "name": "bad-source",
        "defaults": {"model": {"provider": "fake", "model": "fake"}},
        "cases": [{
            "case_id": "bad",
            "task": {"case_id": "bad", "prompt": "x"},
            # 要打补丁，却没有东西可打
            "workspace": {"kind": "copy", "patch": str(tmp_path / "bug.patch")},
        }],
    }
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValueError, match="nothing to patch"):
        load_suite(path)
