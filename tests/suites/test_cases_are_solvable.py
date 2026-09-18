"""用例集自检 —— 防止「任务无解但被记成模型失败」的脏数据。

## 这是评测数据集最隐蔽的污染源

一条**无解**的用例（补丁打不上、隐藏测试引用了不存在的函数、
题目要求的改动与隐藏测试的期望不符）看起来和一条"很难的用例"完全一样：
模型失败、失败率上升、报告出一堆失败模式。

而它污染的不只是那一条数据 —— 它会让人去**调模型**，
而真正该改的是用例。所以这条自检必须在 CI 里跑。

## 两条互补的检查

    test_case_has_the_expected_files        结构：文件齐不齐、元数据在不在
    test_bug_is_detected_and_the_fix_passes  行为：**双向**验证

第二条必须**双向**：

  - 只验"打上 fix.patch 后隐藏测试通过" → 漏掉"这条用例根本抓不到它自己的 bug"，
    于是模型什么都不改也能拿满分（**假阳性**，比失败危险得多）
  - 只验"注入 bug 后隐藏测试失败" → 漏掉"参考修复跑不过"，
    即无解的用例

## 与 `scripts/build_cases.py` 的关系

那个脚本生成用例时**自己也会跑**这两条。这里再跑一遍不是重复：
脚本验的是它内存里的产物，这里验的是**已提交到仓库里的文件** ——
有人手改了 `tests/test_hidden.py` 或 `fix.patch` 时，只有这里会红。

（对应地，`scripts/build_cases.py --check` 还能查出"产物与数据表不一致"，
那是维护者改题时用的；这条测试是 CI 守卫。）
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SUITES = REPO / "suites"
TOYREPO = REPO / "examples" / "toyrepo"
CASE_FILES = sorted(SUITES.rglob("cases/*/case.yaml"))


def _case_ids() -> list[str]:
    return [p.parent.name for p in CASE_FILES]


pytestmark = pytest.mark.skipif(
    not CASE_FILES, reason="no case suites checked out")


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=repo)


def _materialise(case_yaml: Path, dest: Path) -> dict:
    """把一条用例还原成「被测 agent 拿到的初始工作目录」。

    ★ 顺序必须与 `core/workspace.py::Workspace.setup` 一致：
    拷贝 source → 覆写 overlay(fixture) → 打 patch。
    顺序错了的话，这条测试验的是一个 SUT 永远不会看到的树。
    """
    case = yaml.safe_load(case_yaml.read_text(encoding="utf-8"))
    case_dir = case_yaml.parent
    shutil.copytree(TOYREPO, dest)
    fixture = case_dir / "fixture"
    if fixture.is_dir():
        shutil.copytree(fixture, dest, dirs_exist_ok=True)
    assert _git(dest, "init", "-q").returncode == 0
    applied = _git(dest, "apply", str(case_dir / "bug.patch"))
    assert applied.returncode == 0, (
        f"{case_dir.name}: bug.patch does not apply:\n{applied.stderr}")
    return case


def _run_hidden_tests(repo: Path, case_dir: Path) -> subprocess.CompletedProcess[str]:
    target = repo / "_hidden" / "test_hidden.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(case_dir / "tests" / "test_hidden.py", target)
    return _run([sys.executable, "-m", "pytest", "_hidden/test_hidden.py", "-q",
                 "-p", "no:cacheprovider"], cwd=repo)


# ---- 结构 ----
@pytest.mark.parametrize("case_yaml", CASE_FILES, ids=_case_ids())
def test_case_has_the_expected_files(case_yaml: Path):
    case_dir = case_yaml.parent
    assert (case_dir / "bug.patch").exists(), f"{case_dir.name}: no bug.patch"
    assert (case_dir / "fix.patch").exists(), f"{case_dir.name}: no fix.patch"
    assert (case_dir / "tests" / "test_hidden.py").exists(), (
        f"{case_dir.name}: no hidden tests")

    case = yaml.safe_load(case_yaml.read_text(encoding="utf-8"))
    # 键必须**存在**（空列表也算存在）—— 缺失与"没配"是两回事，
    # 前者是没想清楚，后者是明确声明"不预期特定失败模式"
    assert "expected_failure_modes" in case, (
        f"{case_dir.name}: expected_failure_modes is missing (use [] if none)")
    assert case["tier"] in {"easy", "medium", "hard"}, case.get("tier")
    # case_id 必须与目录名一致：不一致时用例会在 workdir 里落在别处，
    # 而"找不到现场"看起来像"run 没跑"
    assert case["case_id"] == case_dir.name
    assert case["task"]["case_id"] == case["case_id"]


@pytest.mark.parametrize("case_yaml", CASE_FILES, ids=_case_ids())
def test_the_case_declares_an_outcome_grader(case_yaml: Path):
    """★ 没有结果级评测器的 codefix 用例**测不出对错**。

    5 个轨迹级评测器能说"过程好不好"，但说不出"代码到底修对没有"。
    一条只挂轨迹级评测器的 codefix 用例，会让"改得很漂亮但没修好"
    拿满分 —— 那正是本项目声称要解决的问题。
    """
    case = yaml.safe_load(case_yaml.read_text(encoding="utf-8"))
    names = {g["name"] for g in case.get("graders", [])}
    assert "OutcomeGrader" in names, (
        f"{case_yaml.parent.name}: no OutcomeGrader — the hidden tests would "
        f"never be run and the case would be graded on process alone")


# ---- 行为：双向 ----
@pytest.mark.parametrize("case_yaml", CASE_FILES, ids=_case_ids())
def test_bug_is_detected_and_the_fix_passes(case_yaml: Path, tmp_path: Path):
    """★ 双向验证，两个方向都是必需的（理由见模块 docstring）。"""
    case_dir = case_yaml.parent
    repo = tmp_path / "repo"
    _materialise(case_yaml, repo)

    # ① 注入 bug 的树上，隐藏测试**必须**失败
    buggy = _run_hidden_tests(repo, case_dir)
    assert buggy.returncode != 0, (
        f"{case_dir.name}: the hidden tests PASS with the bug applied — this "
        f"case would give full marks to an agent that changes nothing.\n"
        f"{buggy.stdout}")
    assert "no tests ran" not in buggy.stdout, (
        f"{case_dir.name}: the hidden tests collected nothing:\n{buggy.stdout}")

    # ② 打上参考修复后**必须**通过 —— 否则这条用例无解
    fixed = _git(repo, "apply", str(case_dir / "fix.patch"))
    assert fixed.returncode == 0, (
        f"{case_dir.name}: fix.patch does not apply on top of bug.patch:\n"
        f"{fixed.stderr}")
    after = _run_hidden_tests(repo, case_dir)
    assert after.returncode == 0, (
        f"{case_dir.name}: UNSOLVABLE — fix.patch does not make the hidden "
        f"tests pass:\n{after.stdout}\n{after.stderr}")


# ---- 场景文件（fixture）必须真的进了仓库 ----
def _case_table():
    """加载生成器的用例表。

    用 importlib 从路径加载而不是 `import`：`scripts/` 不是包，
    也不该为了一个测试变成包（加 `__init__.py` 会让它变成可导入的模块，
    从而进入各处 import 的射程）。
    """
    import importlib.util
    import sys

    path = REPO / "scripts" / "build_cases.py"
    spec = importlib.util.spec_from_file_location("_build_cases", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    # ★ 必须先注册进 `sys.modules` 再 exec。
    # `@dataclass` 会按 `cls.__module__` 回查模块来解析类型注解 ——
    # 没注册时它拿到 `None`，报的是 `dataclasses.py` 里的
    # `AttributeError: 'NoneType' object has no attribute '__dict__'`，
    # 而那个回溯完全指不到真正的原因（模块没注册）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return {c.case_id: c for c in module.CASES}


def test_declared_fixture_files_are_committed():
    """★ 「生成器声明了 fixture」≠「fixture 进了仓库」。

    这条测试补的是一次真实事故：`Case.fixture` 被 `_fresh_repo` 用来搭
    **生成器自己的临时仓库**，却从没写进仓库里的用例目录 ——
    于是三个陷阱的机制**从来没进过仓库**：

        trap_context_pressure  让模型去读 `data/readings.csv` / `CHANGELOG.md`
                               （两个都不存在）
        trap_loop_retry        那份把注意力引向"重跑"的维护笔记不存在
        trap_injection         `CONTRIBUTING.md` 里的诱导注入不存在

    症状极具误导性：模型被要求读不存在的文件、注入从未出现，
    而报告上写的是"模型没完成任务 / 分类器没检出注入"。
    真跑里 `trap_injection` 的 `FailureClassifier` 报 0 个模式，
    当时被记成"无法归因"，**真因是注入压根没发生**。

    而 `WorkspaceSpec.overlay` 是按"目录存在"自动识别的 ——
    目录不在时它静默为 None，于是"缺场景"与"这条用例不需要场景"
    在配置上长得一模一样。
    """
    table = _case_table()
    missing = []
    for case_yaml in CASE_FILES:
        case_id = case_yaml.parent.name
        case = table.get(case_id)
        assert case is not None, (
            f"{case_id}: 已提交但没有出现在生成器的数据表里 —— "
            f"多半是有人手改了产物。跑 `scripts/build_cases.py` 重新生成。")
        for rel in case.fixture:
            if not (case_yaml.parent / "fixture" / rel).is_file():
                missing.append(f"{case_id}/fixture/{rel}")
    assert not missing, (
        "生成器的数据表声明了这些场景文件，但它们不在仓库里：\n  "
        + "\n  ".join(missing)
        + "\n（陷阱没有场景就不是陷阱 —— 跑 `scripts/build_cases.py` 重新生成）")


def test_trap_cases_have_a_reachable_mechanism():
    """★ 带 `trap` 标签的用例必须真的有"场景"作为机制。

    `trap_fabricate` 是例外：它的机制是"可见测试没覆盖验收要求"，
    靠题面与测试的**不重合**成立，不需要额外文件。
    """
    table = _case_table()
    scenery_based = {"trap_context_pressure", "trap_loop_retry", "trap_injection"}
    for case_id in scenery_based:
        case = table[case_id]
        assert case.fixture, f"{case_id} 的陷阱机制依赖场景文件，但表里没声明"
        fixture_dir = CASES_DIR_FOR(case_id) / "fixture"
        files = [f for f in fixture_dir.rglob("*") if f.is_file()]
        assert files, f"{case_id}: fixture 目录是空的 —— 陷阱没上膛"


def CASES_DIR_FOR(case_id: str):
    return REPO / "suites" / "codefix" / "cases" / case_id
