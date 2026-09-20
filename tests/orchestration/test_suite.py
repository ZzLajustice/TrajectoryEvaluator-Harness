"""Suite 加载器测试。

## 本文件的核心命题

**配置错误必须在加载期暴露，不是跑到一半才炸。**

一次真模型 suite 跑 5 分钟 5 美元。如果「评测器名拼错了」这种事要到第 4 条用例
才暴露，前 3 条的算力已经花掉了，而且报告会缺一块 —— 缺失的那块看起来
和「这条用例没配评测器」一模一样。所以未知评测器名、未知中间件名、
重复 case_id、task.case_id 与 case_id 不一致，全部在 `load_suite` 时抛错。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from harness.orchestration.suite import CaseSpec, Suite, load_suite

SUITE_YAML = """
name: demo
version: "1"
defaults:
  model: {provider: fake, model: fake}
  budget: {max_turns: 6}
  middlewares: [permission, telemetry]
  concurrency: 4
cases:
  - case_id: c1
    tier: easy
    task: {case_id: c1, prompt: "fix it"}
    workspace: {kind: tempdir}
    graders: [{name: EfficiencyAnalyzer, config: {optimal_steps: 4}}]
    expected_failure_modes: [step_repetition]
    repeat: 2
    tags: [slicing]
"""


def _write(tmp_path: Path, text: str = SUITE_YAML, name: str = "suite.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def test_loads_suite_with_defaults(tmp_path):
    s = load_suite(_write(tmp_path))
    assert s.name == "demo"
    assert len(s.cases) == 1
    assert s.defaults.budget.max_turns == 6


def test_defaults_apply_to_case(tmp_path):
    s = load_suite(_write(tmp_path))
    assert s.cases[0].repeat == 2
    assert s.cases[0].tier == "easy"


def test_missing_required_field_raises_actionable_error(tmp_path):
    p = _write(tmp_path, "name: x\nversion: '1'\ndefaults: {}\ncases:\n  - tier: easy\n",
               "bad.yaml")
    with pytest.raises(Exception) as ei:
        load_suite(p)
    assert "case_id" in str(ei.value) or "task" in str(ei.value)


def test_unknown_grader_name_is_rejected_at_load_time(tmp_path):
    """配置错误必须在加载期暴露，不是跑到一半才炸。"""
    p = _write(tmp_path, SUITE_YAML.replace("EfficiencyAnalyzer", "NoSuchEvaluator"),
               "bad.yaml")
    with pytest.raises(ValueError, match="unknown grader"):
        load_suite(p)


def test_grader_whitelist_is_not_a_second_source_of_truth(tmp_path):
    """白名单必须与调度器的注册表**同源**。

    两份清单会漂移：suite 放行了一个 `build_evaluators` 不认识的评测器，
    加载期不报错，跑到评测那一步才炸 —— 正好是加载期校验想避免的事。
    """
    from harness.orchestration.evalrunner import EVALUATOR_REGISTRY

    for name in EVALUATOR_REGISTRY:
        text = SUITE_YAML.replace("EfficiencyAnalyzer", name)
        assert load_suite(_write(tmp_path, text, f"{name}.yaml")).cases[0]


def test_duplicate_case_ids_are_rejected(tmp_path):
    p = _write(tmp_path, SUITE_YAML + SUITE_YAML.split("cases:")[1], "dup.yaml")
    with pytest.raises(ValueError, match="duplicate case_id"):
        load_suite(p)


def test_unknown_middleware_name_is_rejected_at_load_time(tmp_path):
    """同一个道理：拼错的中间件名会让人以为策略生效了。"""
    p = _write(tmp_path, SUITE_YAML.replace("[permission, telemetry]", "[parmission]"),
               "bad.yaml")
    with pytest.raises(ValueError, match="unknown middleware"):
        load_suite(p)


def test_task_case_id_must_agree_with_the_case(tmp_path):
    """`case_id` 与 `task.case_id` 是两处配置，不一致必须报错。

    放过去的话，轨迹里的 case_id 会和报告里的对不上，
    而症状只是「某条用例的数据看起来不对」。
    """
    p = _write(tmp_path, SUITE_YAML.replace(
        'task: {case_id: c1, prompt: "fix it"}', 'task: {case_id: c9, prompt: "fix it"}'),
        "bad.yaml")
    with pytest.raises(ValueError, match="case_id"):
        load_suite(p)


def test_yaml_uses_safe_load_only(tmp_path):
    """yaml.safe_load 是硬性要求 —— 绝不用 yaml.load。

    断言具体异常类型而不是 `Exception`：`yaml.YAMLError` 才是"safe_load
    拒绝了这个标签"的证据。改成裸 `Exception` 的话，一个拼错的路径
    （`FileNotFoundError`）也会让测试通过，而那时安全性根本没被验证。
    """
    p = _write(tmp_path, "name: !!python/object/apply:os.system ['echo pwned']\n",
               "evil.yaml")
    with pytest.raises(yaml.YAMLError):
        load_suite(p)


def test_suite_root_must_be_a_mapping(tmp_path):
    p = _write(tmp_path, "- just\n- a\n- list\n", "bad.yaml")
    with pytest.raises(ValueError, match="mapping"):
        load_suite(p)


# ---- 中间件解析 ----
def test_middlewares_for_returns_specs_in_canonical_order(tmp_path):
    """顺序由工厂强制，但 suite 侧也不该按书写顺序返回 —— 两处不一致会误导读者。"""
    s = load_suite(_write(tmp_path))
    names = [m.name for m in s.middlewares_for(s.cases[0])]
    assert names == ["telemetry", "permission"]


def test_case_can_override_middlewares(tmp_path):
    """策略类用例需要单独开 policy —— 默认里不带，逐 case 加。"""
    text = SUITE_YAML.replace(
        "    tags: [slicing]", "    middlewares: [policy, telemetry]\n    tags: [slicing]")
    s = load_suite(_write(tmp_path, text))
    assert [m.name for m in s.middlewares_for(s.cases[0])] == ["telemetry", "policy"]


# ---- fake_script 的继承 ----
def test_case_inherits_fake_script_from_defaults(tmp_path):
    text = SUITE_YAML.replace(
        "  concurrency: 4", '  concurrency: 4\n  fake_script: [{tool: finish, arguments: {}}]')
    s = load_suite(_write(tmp_path, text))
    assert s.fake_script_for(s.cases[0]) == [{"tool": "finish", "arguments": {}}]


def test_case_fake_script_wins_over_defaults(tmp_path):
    text = SUITE_YAML.replace(
        "  concurrency: 4", '  concurrency: 4\n  fake_script: [{text: "default"}]').replace(
        "    tags: [slicing]", '    fake_script: [{text: "case"}]\n    tags: [slicing]')
    s = load_suite(_write(tmp_path, text))
    assert s.fake_script_for(s.cases[0]) == [{"text": "case"}]


# ---- 模型 ----
def test_case_spec_coerces_a_dict_task_into_taskspec():
    """YAML 里 task 是 mapping，加载后必须是 `TaskSpec` —— 下游直接取 `.prompt`。"""
    c = CaseSpec(case_id="x", task={"case_id": "x", "prompt": "p"})  # type: ignore[arg-type]
    assert c.task.prompt == "p"


def test_suite_is_a_public_type():
    """`Suite` 是装配层与报告层共同引用的公开类型，不是内部细节。"""
    assert Suite.model_fields["cases"].is_required()


# --------------------------------------------------------------------------
# 没有 golden.yaml 的用例：必须是 SKIPPED，不是崩溃
# --------------------------------------------------------------------------
_NO_GOLDEN = """\
name: demo
defaults:
  model: {provider: fake, model: fake}
  budget: {max_turns: 6}
cases:
  - case_id: c1
    tier: easy
    task: {case_id: c1, prompt: "fix it"}
    workspace: {kind: tempdir}
    graders: [{name: TrajectoryMatcher, config: {mode: in_order}}]
"""


async def test_a_case_without_a_golden_loads_and_skips(tmp_path):
    """★★ 没有 `golden.yaml` 时，`TrajectoryMatcher` 必须是 **SKIPPED**。

    这里曾经是崩溃：`_resolve_golden` 在没有 golden 时只 `continue`，
    于是配置里少了 `expected` —— 而 `TrajectoryMatcher.__init__` 把它设成了
    **必填**（有意必填：手写 suite 拼错名字时要当场炸）。
    结果是构造期 `TypeError: missing 1 required keyword-only argument`，
    评测器报 **ERROR 而不是 SKIPPED**，而函数 docstring 里那句
    "没有它时这条用例不挂过程分"只写在文档里，行为上不成立。

    **17 条用例全都有 golden，所以那条分支从来没被执行过** ——
    直到 Track B 两条没有 golden 的用例第一次真跑把它撞出来。

    ★ 这条测试原来住在 `tests/e2e/test_vendored_cases.py`，靠"Track B
    恰好没有 golden"成立。那两条**现在有 golden 了**（2026-09-19 录制），
    于是那条测试连同它的守卫一起被删掉，覆盖搬到这里 ——
    不再依赖任何具体用例的数据状态，测的是加载器本身的行为。
    """
    from harness.contracts.protocols import EvalContext
    from harness.contracts.results import EvalStatus
    from harness.evaluators.base import run_evaluators
    from harness.testing.builder import TrajectoryBuilder

    suite = load_suite(_write(tmp_path, _NO_GOLDEN))
    (case,) = suite.cases
    assert case.golden is None, "这条用例本来就没有 golden"

    matcher = next(g for g in case.graders if g.name == "TrajectoryMatcher")
    assert matcher.config.get("expected") == [], (
        "没有 golden 时应当注入**空的** expected —— 由 evaluate() 转成 SKIPPED。"
        f" 实际是 {matcher.config.get('expected')!r}，那会在构造期 TypeError")

    # 端到端确认它真的 SKIP 而不是 ERROR：用配置实例化并跑一次
    traj = (TrajectoryBuilder(run_id="r", task="t").turn()
            .llm_response(text="hi").run_end(status="ok").build())
    from harness.orchestration.evalrunner import build_evaluators

    results = await run_evaluators(
        build_evaluators([{"name": matcher.name, "config": matcher.config}]),
        traj, EvalContext())
    assert results[0].status is EvalStatus.SKIPPED, results[0].model_dump_json()


# --------------------------------------------------------------------------
# 假 provider 配空脚本 = 19 条 llm_error
# --------------------------------------------------------------------------
_NO_SCRIPT = """\
name: demo
defaults:
  model: {provider: fake, model: fake}
  budget: {max_turns: 6}
cases:
  - case_id: c1
    tier: easy
    task: {case_id: c1, prompt: "fix it"}
    workspace: {kind: tempdir}
    graders: [{name: EfficiencyAnalyzer}]
"""

_WITH_SCRIPT = _NO_SCRIPT.replace(
    "  budget: {max_turns: 6}",
    "  budget: {max_turns: 6}\n  fake_script: [{text: done}]")


def _preflight_ok(tmp_path, text: str) -> bool:
    """跑一次 preflight，返回它是否放行。"""
    from harness.orchestration.deps import RunBuilder

    suite = load_suite(_write(tmp_path, text))
    try:
        RunBuilder(out_dir=tmp_path / "o", workdir=tmp_path / "w")._preflight(suite)
        return True
    except Exception as exc:  # noqa: BLE001 - 这里就是要看它抛不抛
        print(f"  preflight 拒绝: {type(exc).__name__}: {exc}")
        return False


def test_a_fake_provider_with_no_script_is_rejected_before_any_run(tmp_path):
    """★★ 假 provider 配空脚本**必须**在跑之前就报错。

    实测（干净 clone 上跑 `suites/codefix`）：默认 provider 是 `fake`
    而用例没有 `fake_script`，于是**19 条全部 `llm_error`** ——
    退出码 1、报告里一片红，而真因是"这个 suite 没给假模型台词"。

    `FakeProvider` 对空脚本抛的是 `IndexError('script exhausted')`，
    那是**为"agent 多调了一次模型"设计的**（刻意不静默重复最后一条响应）。
    可**空脚本不是"用尽了"** —— 两者完全不同，而消息把它们说成一回事。
    于是新 clone 的人看到 19 个 `llm_error`，第一反应是"这项目坏了"。

    ★ 校验放在 `_preflight` 而**不是** `load_suite`：加载器跑在
    `--model/--provider` 覆盖**之前**，放那儿的话 `--model deepseek`
    也会被拦下 —— 而那正是这条 suite 的正确用法。
    """
    assert not _preflight_ok(tmp_path, _NO_SCRIPT), (
        "假 provider 配空脚本应当被拒绝 —— 否则 19 条用例全部 llm_error，"
        "而这看起来像项目坏了")


def test_a_fake_provider_with_a_script_still_passes_preflight(tmp_path):
    """守卫：有脚本（哪怕只有一句 text）就放行 —— 否则这条校验会误伤全部假跑。"""
    assert _preflight_ok(tmp_path, _WITH_SCRIPT)


def test_a_real_provider_still_passes_preflight_without_a_script(tmp_path):
    """守卫：真 provider 不需要 `fake_script`（它的台词来自模型）。"""
    real = _NO_SCRIPT.replace("provider: fake", "provider: deepseek")
    assert _preflight_ok(tmp_path, real)
