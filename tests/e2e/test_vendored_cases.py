"""端到端：**vendored 真实仓库**用例（Track B）跑在那条真链路上。

## 为什么这个文件必须存在

`tests/suites/test_cases_are_solvable.py` 也会做"注入 bug → 隐藏测试失败 →
打上 fix → 通过"这套双向验证，但它**自己重新实现了一遍工作目录的搭建**
（`_materialise`：拷 source → 覆写 fixture → 打 patch → 拷隐藏测试 → 跑 pytest）。
它甚至在 docstring 里写着"顺序必须与 `core/workspace.py::Workspace.setup` 一致"。

那句"必须一致"没有任何东西在保证。真正的 `Workspace.setup` 还多做几件事：
`git init`、`_commit_baseline()`、`overlay` 识别、`_apply_patch` 的字节校验 ——
任何一件在 `src/` 布局的 vendored 仓库上失效，那个自检**照样全绿**，
因为跑的根本不是同一条路。

所以这里跑真 suite、真 `RunBuilder`、真 `Workspace`、真 `WorkspaceCommandRunner`。
**只用 FakeProvider**：离线、零成本、确定性。

## 这两条用例还顺带守住一件 Track A 守不住的事

Track A 的 17 条源树都是 `examples/toyrepo`（平铺布局，包在根目录）。
Track B 的两条是 `src/` 布局、不装包、靠一个 `conftest.py` 把 `src/` 塞进
`sys.path`。也就是说**"被测包能不能被 import"这件事第一次有了第二种形状**，
而它在 Track A 上是靠 `_repo_root()` 往上找 `csvlite/` 解决的 —— 那套逻辑
对 `src/itsdangerous` 完全不适用。
"""

from __future__ import annotations

import pytest

from harness.contracts.spec import ModelRef
from harness.orchestration.deps import RunBuilder
from harness.orchestration.suite import load_suite

REPO = __import__("pathlib").Path(__file__).resolve().parents[2]
SUITE = REPO / "suites" / "codefix"

#: (case_id, 相对工作目录的路径, **修复的指纹**)
#:
#: "指纹"取 `fix.patch` 里**新增的一行**（两条都是源码行，不取 CHANGES.rst ——
#: changelog 是同一次提交一起改的，拿它当判据测的是"补丁打了"
#: 而不是"代码被改坏了"）。语义统一为：**在修复态存在、在 bug 态不存在**。
#:
#: 方向必须统一。曾经第一条取的是"bug 态独有的字符串"，第二条取的是
#: "修复态独有的字符串"，于是同一个断言在两条用例上要求相反 ——
#: 而失败信息只会说"marker 不在源码里"，指不出是这个方向反了。
CASES = [
    # 上游补回了 `if age < 0: raise SignatureExpired(...)` 这一段
    ("vendor_future_timestamp", "src/itsdangerous/timed.py", "if age < 0"),
    # 上游在填 `date_signed` 之前加了一道 `is not None` 守卫
    ("vendor_date_signed_type", "src/itsdangerous/timed.py",
     "if timestamp is not None:"),
]

#: 两条用例都读同一个文件；这个字符串在**两种状态**下都在，
#: 用来确认"文件真的读到了"而不是拿到一段错误信息。
#: 断言它的**存在**、与上面断言指纹的**不存在**配成一对。
TRACER = "class TimestampSigner"


def _run(case_id: str, script: list[dict], tmp_path, *, graders=None):
    """加载**真正的** `suites/codefix`，只把它缩到一条用例并换掉 provider。"""
    suite = load_suite(SUITE)
    suite.defaults.model = ModelRef(provider="fake", model="fake")
    suite.defaults.fake_script = script
    suite.cases = [c for c in suite.cases if c.case_id == case_id]
    assert suite.cases, f"{case_id} 不在 suites/codefix 里"
    if graders is not None:
        suite.cases[0].graders = graders
    return RunBuilder(
        out_dir=tmp_path / "runs", workdir=tmp_path / "wd"
    ).run_suite_sync(suite, evaluate=True)[0]


def _eval(outcome, name: str):
    return next(e for e in outcome.evals if e.evaluator == name)


def _fix_script(case_id: str) -> dict:
    """用 `git apply` 把该用例的参考修复打进工作目录。

    不把修正后的源码内嵌进脚本：那样题面一改就要跟着改，
    而且**内嵌的那份和 `fix.patch` 迟早不一致** —— 不一致时
    这条测试说的就不是仓库里那条用例了。
    """
    patch = SUITE / "cases" / case_id / "fix.patch"
    return {"tool": "run_command",
            "arguments": {"argv": ["git", "apply", str(patch)]}}


# ---- 工作目录：真的拷了、真的带 bug ----
@pytest.mark.parametrize("case_id,path,marker", CASES, ids=[c[0] for c in CASES])
def test_the_agent_starts_with_the_buggy_vendored_source(
        case_id, path, marker, tmp_path):
    """★ 开局那一屏里必须有这个包，而且必须是**带 bug** 的那一版。

    两件事一起断言是有意的：只查"目录非空"的话，补丁没打上照样过，
    而那时任务已经变成"这段代码本来是对的，你改什么？"
    """
    outcome = _run(case_id, [
        {"tool": "read_file", "arguments": {"path": path}},
        {"tool": "finish", "arguments": {"summary": "read"}},
    ], tmp_path)
    source = next(r for r in outcome.result.trajectory.tool_results()
                  if r.name == "read_file")
    assert TRACER in source.content, (
        f"{case_id}: {path} 没读到内容 —— 工作目录是空的，或者这个路径下"
        f"什么都没有（`workspace.source` 没解析到 vendored 树）：\n"
        f"{source.content[:300]}")
    assert marker not in source.content, (
        f"{case_id}: bug.patch 没打上 —— 工作目录里是**修好的**代码\n"
        f"（{marker!r} 在，而它只由修复引入）：\n{source.content[:400]}")


# ---- 结果级：修好了必须 PASS ----
@pytest.mark.parametrize("case_id,path,marker", CASES, ids=[c[0] for c in CASES])
def test_the_reference_fix_passes_the_hidden_tests(
        case_id, path, marker, tmp_path):
    """★★ 整条链路的正向证据 —— 也是"这条 vendored 用例可解"的**唯一**强证据。

    它一次性证掉四件事：

      ① `src/` 布局的树能被拷进工作目录并 `git apply` 上补丁
      ② 隐藏测试在**工作目录里**能把 `itsdangerous` import 起来
         （那个 `conftest.py` 适配 + 隐藏测试自己的 `sys.path` 插入）
      ③ `WorkspaceCommandRunner` 真的把 pytest 跑起来了（不是 SKIPPED）
      ④ 判据本身是**可满足的** —— 修对了就通过

    少任何一件，这条用例都会变成"看起来很难、实际无解"的脏数据，
    而报告上只会写"模型没修好"。
    """
    outcome = _run(case_id, [
        {"tool": "read_file", "arguments": {"path": path}},
        _fix_script(case_id),
        {"tool": "finish", "arguments": {"summary": "fixed"}},
    ], tmp_path)
    result = _eval(outcome, "OutcomeGrader")
    assert result.status.value == "pass", result.model_dump_json(indent=2)
    assert result.metrics["outcome_pass"] == 1.0
    # 区分"评了且通过"与"根本没评"：SKIPPED 会让这条用例在 pass_rate 里
    # 被静默排除，而报告上看不出区别。
    assert "hidden tests passed" in result.summary


@pytest.mark.parametrize("case_id,path,marker", CASES, ids=[c[0] for c in CASES])
def test_doing_nothing_fails_rather_than_errors(
        case_id, path, marker, tmp_path):
    """★ 什么都不改必须判 **FAIL**，不是 ERROR、更不是 SKIPPED。

    四态分级的全部意义在这里：FAIL 是"agent 没修好"，
    ERROR 是"评测本身炸了"。把后者当前者会让报告把
    "隐藏测试 import 不到被测包"记成"模型不会修 bug" ——
    而那正是 Track B 第一次真跑时暴露出来的形态
    （见 `docs/known-gaps.md` §4.2.1）。
    """
    outcome = _run(case_id, [
        {"tool": "finish", "arguments": {"summary": "nothing to do"}},
    ], tmp_path)
    result = _eval(outcome, "OutcomeGrader")
    assert result.status.value == "fail", result.model_dump_json(indent=2)
    assert result.metrics["outcome_pass"] == 0.0


# ---- 守卫：上面那张参数表本身 ----
def test_the_table_matches_what_is_on_disk():
    """★ 参数表是手写的，所以它自己也要有人盯。

    它写错的后果不是"报错"，而是**上面四条参数化测试全都在验别的东西**：
    `marker` 拼错 → `test_the_agent_starts_with_the_buggy_vendored_source`
    会红（还算幸运）；`path` 指向一个不存在的文件 → `read_file` 失败 →
    那条也会红。所以真正危险的是**它们全都红成一片**时被当成"新用例还没配好"。
    """
    import yaml

    for case_id, path, marker in CASES:
        case_dir = SUITE / "cases" / case_id
        assert (case_dir / "fix.patch").is_file(), case_id
        assert (case_dir / "tests" / "test_hidden.py").is_file(), case_id

        case = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
        source = REPO / case["workspace"]["source"]
        assert source.is_dir(), (
            f"{case_id}: case.yaml 指向的源树不存在：{source}")
        # `path` 必须是**源树里真实存在**的文件 —— 它同时被
        # `read_file`（相对工作目录）和这里的断言用，两处必须指同一个东西。
        assert (source / path).is_file(), (
            f"{case_id}: 参数表里的 {path!r} 不在源树 {source.name} 里")

        text = (source / path).read_text(encoding="utf-8")
        # 源树是**修复态**（bug 是它的反向），所以指纹必须在这里出现。
        # 不出现说明这个指纹挑错了 —— 它区分不了两种状态，
        # 于是 `test_the_agent_starts_with_the_buggy_vendored_source`
        # 会永远绿，测不出补丁有没有打上。
        assert marker in text, (
            f"{case_id}: {marker!r} 在**修复态**的源码里也不存在 —— "
            f"它区分不了修复态与 bug 态，拿去当「补丁打上了没」的判据是空断言")
        assert TRACER in text, (
            f"{case_id}: {TRACER!r} 不在 {path} 里 —— "
            f"TRACER 是「文件读到了」的判据，它挑错了那半个断言就没意义")
