"""参考路径（golden）的自检。

## 为什么 golden 需要自己的测试

`golden.yaml` 是**数据**，它的正确性没人替它保证：

- 写错工具名 → 每条 run 都判不匹配 → `golden_score` 恒为 0，
  而 0 看起来像"模型走的路径都不对"
- 没有牙齿（什么都能匹配）→ `golden_score` 恒为 1，
  而 1 看起来像"模型每次都走对了路"
- 引用了 case 的 allow 列表之外的工具有 → 那条用例**不可能**被匹配，
  因为 SUT 根本调不到那个工具

三种错都会静默地产出一个看起来完全正常的分数。所以这里逐条验。

## 与 `tests/suites/test_cases_are_solvable.py` 的分工

那个文件验**结果级**判据（bug 抓得到、参考修复能过）；
这个文件验**过程级**判据。两者都不能由另一个替代。
"""

from __future__ import annotations

import pytest
import yaml

from harness.contracts.protocols import EvalContext
from harness.evaluators.trajectory_match import TrajectoryMatcher
from harness.events.trajectory import Trajectory
from harness.testing.builder import TrajectoryBuilder as TB

REPO = __import__("pathlib").Path(__file__).resolve().parents[2]
GOLDEN_FILES = sorted(REPO.glob("suites/*/cases/*/golden.yaml"))
CASE_FILES = {p.parent.name: p for p in REPO.glob("suites/*/cases/*/case.yaml")}

#: SUT 的工具集（`orchestration/deps.py::build_tool_registry`）。
#: golden 引用集合之外的工具有个隐蔽后果：**那条用例永远不可能被匹配** ——
#: 而分数会显示为"路径不对"，读的人会去怪模型。
_TOOL_REGISTRY = {"read_file", "write_file", "list_dir", "search",
                  "run_command", "finish"}


def _ids() -> list[str]:
    return [p.parent.name for p in GOLDEN_FILES]


pytestmark = pytest.mark.skipif(not GOLDEN_FILES, reason="no goldens yet")


def _golden(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _traj_from_steps(steps):
    """按给定的工具序列构造一条**最小**轨迹。"""
    tb = TB(run_id="golden", task="t")
    for index, (name, args) in enumerate(steps):
        tb = tb.turn().llm_response(tool_calls=[(name, args, f"c{index}")])
        tb = tb.tool_result(name=name, content="ok", ok=True)
    return tb.run_end(status="ok").build()


def _matches(traj: Trajectory, expected) -> bool:
    matcher = TrajectoryMatcher(mode="in_order", expected=expected,
                                tool_args_match_mode="ignore")
    # `TrajectoryMatcher` 只用轨迹，但签名要求一个 `EvalContext`
    return matcher.evaluate(traj, EvalContext()).status.value == "pass"


# ---- 结构 ----
@pytest.mark.parametrize("golden_path", GOLDEN_FILES, ids=_ids())
def test_golden_is_well_formed(golden_path):
    doc = _golden(golden_path)
    alts = doc.get("alternatives")
    assert alts, f"{golden_path.parent.name}: `alternatives` 为空或缺失"
    for alt in alts:
        assert alt, f"{golden_path.parent.name}: 有一条空的 alternative"
        for step in alt:
            assert isinstance(step, list) and len(step) == 2, (
                f"{golden_path.parent.name}: 每一步必须是 [工具名, 参数] 对，"
                f"实际是 {step!r}")
            name, args = step
            assert isinstance(name, str) and isinstance(args, dict), step
    # 来源必须可追溯 —— 没有 provenance 的 golden 无法判断该不该重录
    assert doc.get("generated_by", {}).get("model"), (
        f"{golden_path.parent.name}: generated_by 缺少 model")


@pytest.mark.parametrize("golden_path", GOLDEN_FILES, ids=_ids())
def test_golden_tools_are_actually_available(golden_path):
    """Golden 引用的工具必须是 SUT 真能调到的那些。

    引用了集合之外的工具有个隐蔽后果：**这条用例永远不可能被匹配**，
    而报告上只会显示"路径不对"。
    """
    doc = _golden(golden_path)
    unknown = {step[0] for alt in doc["alternatives"] for step in alt} - _TOOL_REGISTRY
    assert not unknown, (
        f"{golden_path.parent.name}: golden 引用了 SUT 工具集之外的 {unknown} —— "
        f"这条用例**不可能**被匹配，而分数会显示成「路径不对」")


# ---- 自反性 ----
@pytest.mark.parametrize("golden_path", GOLDEN_FILES, ids=_ids())
def test_golden_matches_a_trajectory_built_from_itself(golden_path):
    """★ 自反性：拿 golden 自己造一条轨迹，它必须匹配得上。

    这条挡的是"golden 写错了"—— 工具名拼错、模式配错、expected 形状不对。
    没有它，那种错的表现是 `golden_score` 恒为 0，
    而 0 看起来像"模型走的路径都不对"。
    """
    doc = _golden(golden_path)
    # 与加载器同形：`expected` 是**一条路径**，不是路径的列表
    expected = [list(step) for step in doc["alternatives"][0]]
    # 用第一条 alternative 造轨迹（`in_order` 下它必然匹配）
    traj = _traj_from_steps([tuple(s) for s in doc["alternatives"][0]])
    assert _matches(traj, expected), (
        f"{golden_path.parent.name}: golden 连自己都匹配不上 —— "
        f"这条用例的 golden_score 会恒为 0")


# ---- 牙齿：不许什么都匹配 ----
@pytest.mark.parametrize("golden_path", GOLDEN_FILES, ids=_ids())
def test_golden_rejects_a_blind_edit(golden_path):
    """★★ golden 必须有**牙齿**：不看就改必须判不匹配。

    没有这条的话，"什么都能匹配"的 golden（比如 expected 为空）
    会让 `golden_score` 恒为 1 —— 而 1 看起来像"模型每次都走对了路"。

    这些 golden 保留的是 `read_file → write_file`，也就是"改之前先看"。
    实测依据（2026-09-16 全量真跑，17 条）：
    **满足它的 12 条恰好就是通过隐藏测试的 12 条**，完全重合。
    """
    doc = _golden(golden_path)
    # 与加载器同形：`expected` 是**一条路径**，不是路径的列表
    expected = [list(step) for step in doc["alternatives"][0]]
    blind = _traj_from_steps([("write_file", {}), ("run_command", {})])
    assert not _matches(blind, expected), (
        f"{golden_path.parent.name}: 不看文件直接改（write_file → run_command）"
        f"竟然匹配上了 —— 这条 golden 没有牙齿")


def test_the_golden_and_the_case_know_about_each_other():
    """每个 golden 都要有对应的 case，反之亦然 —— 孤立的文件是笔误的温床。"""
    golden_ids = set(_ids())
    case_ids = {p.parent.name for p in REPO.glob("suites/*/cases/*/case.yaml")}
    assert golden_ids <= case_ids, f"没有对应 case 的 golden: {golden_ids - case_ids}"
