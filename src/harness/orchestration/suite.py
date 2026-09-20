"""用例集加载。

## 三条硬性约束

1. **只用 `yaml.safe_load`** —— `yaml.load` 能构造任意 Python 对象，
   而 suite 文件是可以从别处拿来的数据。有测试用 `!!python/object/apply` 盯着。

2. **配置错误在加载期暴露，不跑到一半才炸。**
   一次真模型 suite 要 5 分钟 5 美元。评测器名拼错这种事如果到第 4 条用例
   才暴露，前 3 条的钱已经花了，而且报告缺的那块**看起来和"这条用例没配评测器"
   一模一样** —— 缺失伪装成正常。所以未知评测器名、未知中间件名、重复 case_id、
   `case_id` 与 `task.case_id` 不一致，全部在 `load_suite` 抛错。

3. **白名单不与调度器分家。** 评测器可选项来自 `EVALUATOR_REGISTRY` 本身，
   不另抄一份清单 —— 两份清单必然漂移，而漂移的方向恰好是"加载期放行、
   运行期才炸"，正是上一条想避免的。

## 格式：defaults + cases

```yaml
name: codefix
defaults:
  model: {provider: deepseek, model: deepseek-chat}
  budget: {max_turns: 12}
  middlewares: [permission, sandbox, budget, telemetry]
  concurrency: 8
cases:
  - case_id: bug_007
    tier: medium
    task: {case_id: bug_007, prompt: "修好 utils.py 的越界"}
    workspace: {kind: copy, source: examples/toyrepo}
    graders: [{name: TrajectoryMatcher, config: {mode: in_order, ...}}]
```

逐 case 可覆写 `middlewares` / `fake_script`；其余走 defaults。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.contracts.spec import (
    Budget,
    MiddlewareSpec,
    ModelRef,
    TaskSpec,
    WorkspaceSpec,
)
from harness.core.middleware.factory import CANONICAL_ORDER, KNOWN_MIDDLEWARES
from harness.core.workspace import HIDDEN_TEST_RELPATH
from harness.orchestration.evalrunner import EVALUATOR_REGISTRY


class SuiteConfigError(ValueError):
    """suite 文件格式或内容错误 —— 映射到 CLI 退出码 2。"""


# case_id 会成为工作目录名（workdir/<case_id>/<run_id>），
# 这些字符在 Windows 上是非法的。**必须在加载期拦**：
# 留给运行期的话，症状是沙箱建立时抛 NotADirectoryError，
# 看起来像"环境有问题"而不是"配置写错了"。
_ILLEGAL_IN_PATH = frozenset('<>:"/\\|?*')


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluatorSpec(_Model):
    name: str
    config: dict[str, Any] = Field(default_factory=dict)


class GoldenSpec(_Model):
    """参考路径。

    形状是 `[[工具名, 参数子集], ...]` 的**有序子序列**（`in_order` 模式：
    expected 是 actual 的有序子序列）。加载器把它透传给
    `TrajectoryMatcher.expected` —— 转换过一次就多一处会漂移的地方。

    **只存工具名 + 参数子集，绝不存 LLM 原文** —— 存自然语言会让模型
    换个措辞就误判。`alternatives` 是必需的：代码修复任务的合法路径极多，
    单条 golden 会把好 run 判成 fail（实测数字见 known-gaps §1.6.5）。

    ⚠️ 这个类型此前标成 `list[list[dict]]`，而实际形状是对（pair）。
    一直没被发现是因为 M11 之前**没有任何代码读过 `golden`** ——
    一个没人读的字段，标注错了也不会有人遇到。
    """

    alternatives: list[list[tuple[str, dict[str, Any]]]] = Field(default_factory=list)
    generated_by: dict[str, Any] = Field(default_factory=dict)


class JudgeSpec(_Model):
    """judge 的 suite 侧配置。

    与 `JudgeConfig`（orchestration/judge.py）分开：这里是**YAML 数据形状**，
    那里是**运行期配置**。分开的好处是 suite 的字段增删不会牵动 judge 实现，
    而 `fake_script` 这类纯测试用的键不该出现在运行期配置里。
    """

    model: str = "fake"
    provider: str = "fake"
    rubric: str = "Judge whether the task was completed correctly and verified."
    # 判几次。1 次无法谈一致性（MetaEvaluator 会报"不适用"而非完美）
    repeat: int = 1
    injection_probe: bool = False
    max_usd: float = 0.5
    max_turns: int = 8
    # 仅 fake provider 下使用；真实模型下被忽略
    fake_script: list[dict[str, Any]] = Field(default_factory=list)


class SUTOverride(_Model):
    """逐 case 覆盖 SUT 配置。`None` 表示沿用 defaults。"""

    system_prompt: str | None = None
    tools: Any = None
    budget: Budget | None = None


class CaseSpec(_Model):
    case_id: str
    tier: Literal["easy", "medium", "hard"] = "medium"
    task: TaskSpec
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)
    sut: SUTOverride = Field(default_factory=SUTOverride)
    graders: list[EvaluatorSpec] = Field(default_factory=list)
    golden: GoldenSpec | None = None
    # 人工标注的预期失败模式（MAST 命名）。规则分类器在 M7 用它算命中率。
    expected_failure_modes: list[str] = Field(default_factory=list)
    # 重复次数。>1 才能观测 flaky —— 单次运行的 pass 无法区分"稳定通过"
    # 和"这次恰好蒙对"。
    repeat: int = 1
    tags: list[str] = Field(default_factory=list)
    # 覆写 defaults；None = 继承
    middlewares: list[str] | None = None
    fake_script: list[dict[str, Any]] | None = None
    # 隐藏验收测试，路径相对**本用例的目录**（如 `tests/test_hidden.py`）。
    # 它不给被测 agent 看 —— 在 run 结束、评测开始前才被拷进工作目录。
    hidden_tests: str | None = None
    #: 加载期解析出的绝对路径。`exclude=True` 是因为它是派生的：
    #: 进指纹会让同一份用例在不同机器上指纹不同，从而让 diff 误判"不可比"。
    hidden_tests_path: Path | None = Field(default=None, exclude=True)
    #: 这条用例的 `case.yaml` 所在目录。目录形状的 suite 用它解析
    #: `hidden_tests` 与 `bug.patch` 这类相对路径。同样是派生字段。
    source_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check(self) -> CaseSpec:
        if self.repeat < 1:
            raise SuiteConfigError(f"case {self.case_id!r}: repeat must be >= 1")
        if self.hidden_tests and not any(
                g.name == "OutcomeGrader" for g in self.graders):
            # 配了隐藏测试却没有结果级评测器 = 那份测试永远不会被跑，
            # 而"配了"看起来像"评了"。
            raise SuiteConfigError(
                f"case {self.case_id!r}: hidden_tests is set but no OutcomeGrader "
                f"is configured to run it — the tests would never be executed")
        bad = sorted(set(self.case_id) & _ILLEGAL_IN_PATH)
        if bad:
            raise SuiteConfigError(
                f"case_id {self.case_id!r} contains characters that are illegal "
                f"in a path on Windows: {bad}. case_id becomes a directory name "
                f"(workdir/<case_id>/<run_id>)."
            )
        # 两处都写 case_id，不一致会让轨迹里的 id 和报告里的对不上
        if self.task.case_id != self.case_id:
            raise SuiteConfigError(
                f"case_id mismatch: case has {self.case_id!r} but "
                f"task.case_id is {self.task.case_id!r}"
            )
        for g in self.graders:
            if g.name not in EVALUATOR_REGISTRY:
                raise SuiteConfigError(
                    f"unknown grader {g.name!r} in case {self.case_id!r}; "
                    f"known: {sorted(EVALUATOR_REGISTRY)}"
                )
        return self


class SuiteDefaults(_Model):
    model: ModelRef = ModelRef(provider="fake", model="fake")
    budget: Budget = Field(default_factory=Budget)
    middlewares: list[str] = Field(default_factory=list)
    concurrency: int = 4
    system_prompt: str = (
        "You are a careful coding agent. Use the tools to fix the problem."
    )
    fake_script: list[dict[str, Any]] = Field(default_factory=list)
    # 没有 judge 块 = 不启用 judge。评测器里的 LLM 兜底会走"没注入 judge"分支。
    judge: JudgeSpec | None = None

    @model_validator(mode="after")
    def _check_middlewares(self) -> SuiteDefaults:
        _require_known_middlewares(self.middlewares)
        return self


def _require_known_middlewares(names: list[str]) -> None:
    """拼错的中间件名会让人以为策略生效了 —— 必须在加载期拦下。"""
    unknown = [n for n in names if n not in KNOWN_MIDDLEWARES]
    if unknown:
        raise SuiteConfigError(
            f"unknown middleware {unknown}; known: {sorted(KNOWN_MIDDLEWARES)}"
        )


class Suite(_Model):
    name: str
    version: str = "1"
    defaults: SuiteDefaults = Field(default_factory=SuiteDefaults)
    cases: list[CaseSpec]
    source_path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check_unique_ids(self) -> Suite:
        ids = [c.case_id for c in self.cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise SuiteConfigError(f"duplicate case_id: {dupes}")
        if not self.cases:
            raise SuiteConfigError(f"suite {self.name!r} has no cases")
        return self

    # ---- 逐 case 解析 ----
    def judge_config(self) -> JudgeSpec | None:
        return self.defaults.judge

    def middlewares_for(self, case: CaseSpec) -> list[MiddlewareSpec]:
        """按**规范顺序**返回 —— 书写顺序不代表管道顺序，两处不一致会误导读者。"""
        names = case.middlewares if case.middlewares is not None else self.defaults.middlewares
        _require_known_middlewares(names)
        return [MiddlewareSpec(name=n) for n in CANONICAL_ORDER if n in names]

    def fake_script_for(self, case: CaseSpec) -> list[dict[str, Any]]:
        if case.fake_script is not None:
            return case.fake_script
        return self.defaults.fake_script

    def budget_for(self, case: CaseSpec) -> Budget:
        return case.sut.budget or self.defaults.budget

    def system_prompt_for(self, case: CaseSpec) -> str:
        return case.sut.system_prompt or self.defaults.system_prompt


def load_suite(path: Suite | Path | str) -> Suite:
    """加载并**完整校验** suite。任何配置错误在这里抛错。

    接受两种形状：

    | 形状 | 用途 |
    |---|---|
    | 单个 `.yaml` 文件（`defaults` + `cases`） | 绝大多数 suite；`examples/` 下都是 |
    | **一个目录**（内含 `suite.yaml` + `cases/*/case.yaml`） | 用例带补丁/隐藏测试这类多文件产物时 |

    目录形状存在的理由是**用例不再是一个 YAML 片段，而是一个小目录**：
    codefix 类用例除了 `case.yaml` 还带 `bug.patch` / `fix.patch` /
    `tests/test_hidden.py`。把这些塞进一个 YAML 里会让它变成不可读的
    字符串团，而**补丁与隐藏测试本来就该是真实文件** ——
    它们要被 `git apply` 和应用、被 pytest 收集。

    两种形状都走同一份 `CaseSpec` 校验 —— 这里只负责把目录拼成一个
    内存里的 suite 文档，**不放松任何校验**。

    **幂等**：传入一个已经加载好的 `Suite` 会原样返回。
    这样调用方（`RunBuilder`、测试）可以统一写 `load_suite(x)`，
    而不必判断 x 是路径还是对象 —— 那种判断写两遍必然漂移。
    """
    if isinstance(path, Suite):
        return path
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"suite not found: {path}")
    if path.is_dir():
        return _load_suite_dir(path)

    # ★ 只允许 safe_load —— 绝不执行 YAML 里的任意 Python 对象
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SuiteConfigError(
            f"suite root must be a mapping, got {type(raw).__name__}"
        )
    suite = Suite.model_validate(raw)
    suite.source_path = path
    _resolve_workspace_paths(suite, base=path.parent)
    _resolve_hidden_tests(suite, base=path.parent)
    # ★ 单文件 suite 也要走这一步。
    #
    # 它没有 `case.yaml`，因此**永远没有 golden.yaml** —— 而
    # `TrajectoryMatcher` 的 `expected` 是必填的，少了它评测器报 ERROR
    # 而不是 SKIPPED（实测：`config={'mode': 'in_order'}` → `error`）。
    #
    # 这里曾经只在 `_load_suite_dir` 里调用，于是**目录套件修好了、单文件套件没有**，
    # 而 `examples/*.yaml` 全是单文件套件。
    _resolve_golden(suite)
    return suite


def _load_suite_dir(root: Path) -> Suite:
    """目录形状：`suite.yaml` 提供 defaults，`cases/*/case.yaml` 各提供一条用例。"""
    manifest = root / "suite.yaml"
    if not manifest.exists():
        raise SuiteConfigError(
            f"{root} looks like a suite directory but has no suite.yaml")
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SuiteConfigError(f"{manifest}: root must be a mapping")

    case_files = sorted((root / "cases").glob("*/case.yaml"))
    if not case_files:
        raise SuiteConfigError(f"{root}: no cases/*/case.yaml found")
    repo_root = _repo_root(root)
    # 目录名与 case_id 必须一致 —— 不一致时 `test_cases_are_solvable` 那样的
    # 自检脚本会去错目录找补丁，而"找不到"很容易被当成"这条没配补丁"。
    for case_file in case_files:
        doc = yaml.safe_load(case_file.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise SuiteConfigError(f"{case_file}: root must be a mapping")
        if doc.get("case_id") != case_file.parent.name:
            raise SuiteConfigError(
                f"{case_file}: case_id {doc.get('case_id')!r} does not match its "
                f"directory name {case_file.parent.name!r}")
        # 目录形状下 `bug.patch` 是约定的固定文件名，不必每条 case 都写
        workspace = doc.setdefault("workspace", {})
        if isinstance(workspace, dict):
            workspace.setdefault("patch", str(case_file.parent / "bug.patch"))
            # `fixture/` 存在时自动当作 overlay —— 用例私有的场景文件
            # （诱导注入的注释、撑爆上下文的数据）不必每条 case 手写路径
            fixture = case_file.parent / "fixture"
            if fixture.is_dir():
                workspace.setdefault("overlay", str(fixture))
            # ★ 三个路径都解析成**绝对路径**：`source` 相对仓库根写，
            # `patch`/`overlay` 相对用例目录写（都是可入库的写法）。
            #
            # 解析成绝对的而不是留着相对的：相对的靠"进程恰好从仓库根启动"
            # 才成立，而 `run` / `ci` / 测试都可能从别处调用。
            # 漏掉 `source` 的后果尤其严重 —— 不是报错，而是**工作目录是空的**：
            # 没有东西可拷 → 补丁的目标文件不存在 → 而 `git apply` 在 git 仓库
            # 内部对目标不存在的补丁会打出 "Skipped patch" 并**返回 0**。
            # 于是 SUT 拿到一个空目录、一路对着空气干活，
            # 最后报告上写的是"模型不会修 bug"。
            for key in ("source", "patch", "overlay"):
                raw_path = workspace.get(key)
                if not raw_path or Path(str(raw_path)).is_absolute():
                    continue
                # `source` 以仓库根为基准（生成器就是这么写的），
                # `patch` / `overlay` 以用例目录为基准
                base = repo_root if key == "source" else None
                workspace[key] = str(
                    (base / raw_path) if base else Path(raw_path).resolve())
        # 相对路径（hidden_tests）以**用例自己的目录**为基准
        doc.setdefault("source_dir", str(case_file.parent))
        raw.setdefault("cases", []).append(doc)

    suite = Suite.model_validate(raw)
    suite.source_path = manifest
    _resolve_hidden_tests(suite, base=root)
    _resolve_golden(suite)
    return suite


def _resolve_golden(suite: Suite) -> None:
    """读每条的 `golden.yaml`，并把参考路径**注入** `TrajectoryMatcher`。

    为什么注入而不是让人在 `case.yaml` 里再抄一遍：抄一遍就有两处真相，
    漂移的方向恰好是「报告里的过程分与人对不上」—— 那是读报告的人
    最没法自己发现的一种错。

    `golden.yaml` 的来源是**真实 run 录制 → 归一化 → 人工审核**
    （见 docs/known-gaps.md §1.6.5）。没有它时这条用例不挂过程分，
    而不是拿一个编出来的路径充数。
    """
    for case in suite.cases:
        # ★ `source_dir is None`（单文件 suite）也必须走这条路 ——
        # 它同样没有 golden，而"没有 golden"与"用例是目录还是单文件"无关。
        #
        # 分开处理的话，单文件 suite 里声明 `TrajectoryMatcher` 而不写
        # `expected` 仍会在**实例化时** TypeError → 评测器报 ERROR。
        # **实测确认过**：`config={'mode': 'in_order'}` → `error`，
        # 加了 `expected: []` 才是 `skipped`。修目录套件时漏了这条，
        # 而 `examples/*.yaml` 全是单文件套件。
        path = (case.source_dir / "golden.yaml") if case.source_dir else None
        if path is None or not path.is_file():
            # ★ 没有 golden ≠ 崩溃。`TrajectoryMatcher.__init__` 把 `expected`
            #   设成**必填**是有意的：手写 suite 时把它拼错要当场炸，
            #   而不是静默判 SKIPPED。所以"这条用例没有参考路径"必须由
            #   **这里**表达成一个空的 expected，再由 `evaluate()` 转 SKIPPED。
            #
            #   这里曾经只是 `continue` —— 于是配置里少了 `expected`，
            #   构造时就 `TypeError: missing 1 required keyword-only argument`。
            #   上面那句"没有它时这条用例不挂过程分"写在文档里，
            #   但在行为上不成立，而 17 条用例**全都有 golden**，
            #   所以这条分支从来没被执行过。
            #
            #   实测（2026-09-18）：Track B 两条没有 golden 的用例第一次真跑，
            #   `TrajectoryMatcher` 直接崩（状态 ERROR 而不是 SKIPPED）。
            for grader in case.graders:
                if grader.name == "TrajectoryMatcher":
                    grader.config.setdefault("expected", [])
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            raise SuiteConfigError(f"{path}: root must be a mapping")
        case.golden = GoldenSpec.model_validate(doc)
        if not case.golden.alternatives:
            raise SuiteConfigError(f"{path}: `alternatives` is empty")
        # ★ `TrajectoryMatcher.expected` 是**一条路径**（step 的列表），
        # 而 `alternatives` 是**路径的列表** —— 差一层。直接把 alternatives
        # 塞进 expected 会让 `_expected()` 去解包一个 step 的两半，
        # 报 `TypeError: unhashable type: 'list'`。
        #
        # 多条 alternative 的「任一命中即算匹配」是设计文档的要求，
        # 但匹配器目前只支持单条。**这里报错而不是取第一条** ——
        # 静默只用第一条会让另外几条看起来「配了」，实际从不参与判定。
        if len(case.golden.alternatives) > 1:
            raise SuiteConfigError(
                f"case {case.case_id!r}: {len(case.golden.alternatives)} "
                f"alternatives declared, but TrajectoryMatcher only supports "
                f"a single path today. Either keep one alternative, or "
                f"implement multi-alternative matching (see "
                f"docs/known-gaps.md §4.2) — silently using only the first "
                f"would make the others look configured while never taking "
                f"part in the verdict.")
        expected = [list(step) for step in case.golden.alternatives[0]]
        for grader in case.graders:
            if grader.name == "TrajectoryMatcher":
                grader.config.setdefault("expected", expected)


def _repo_root(start: Path) -> Path:
    """从 `start` 向上找含 `pyproject.toml` 的那一层。

    不写死 `parents[N]`：suite 目录将来可能挪位置，而写死层数在挪动后
    会**静默**解析到错的地方（或者解析不到而报一个看不懂的错）。
    """
    for candidate in (start.resolve(), *start.resolve().parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise SuiteConfigError(
        f"cannot locate the repository root above {start} (no pyproject.toml found)")


def _resolve_workspace_paths(suite: Suite, *, base: Path) -> None:
    """把单文件 suite 的 `source` / `patch` / `overlay` 解析成绝对路径。

    ## 为什么需要这个函数

    目录形 suite 一直在 `_load_suite_dir` 里做这件事，而**单文件路径从来没做过** ——
    路径被原样留下，于是 `patch: ../suites/.../bug.patch` 会相对 **cwd** 解析。
    实测确认过：`load_suite()` 之后 `workspace.patch` 还是那串相对路径。

    这个 bug 的形状与项目里其它几个一样 —— **不报错，只是解析到别处**。
    最终会以"补丁打不上"的形式暴露，而没人会想到是路径基准错了。

    ## 基准与目录形保持一致

        `source`   → **仓库根**（生成器就是这么写的，也是 README 里写的约定）
        `patch` / `overlay` → **suite 文件所在目录**

    ## 什么时候找仓库根

    只在 `source` **存在且是相对路径**时才找。否则一个用 `tempdir`、
    又没有 `source` 的 suite（`examples/traps.yaml` 就是）会被这个函数
    逼着去当仓库的一部分 —— 而它与仓库根没有任何关系。
    """
    needs_root = any(
        case.workspace.kind == "copy" and case.workspace.source
        and not Path(case.workspace.source).is_absolute()
        for case in suite.cases
    )
    root = _repo_root(base) if needs_root else None

    for case in suite.cases:
        ws = case.workspace
        if ws.source and not Path(ws.source).is_absolute():
            # `needs_root` 为真时 root 一定有值
            ws.source = str((root if root else base) / ws.source)
        for key in ("patch", "overlay"):
            raw_path = getattr(ws, key)
            if raw_path and not Path(raw_path).is_absolute():
                setattr(ws, key, str((base / raw_path).resolve()))


def _resolve_hidden_tests(suite: Suite, *, base: Path) -> None:
    """把 `hidden_tests` 的相对路径解析成绝对路径，并**在这里**校验存在。

    校验放在加载期而不是评测期：隐藏测试缺失时，评测期才发现的话
    OutcomeGrader 会返回 SKIPPED，而 **SKIPPED 会让这条 case 在
    `pass_rate` 里被静默排除** —— 一条本该判对错的用例就这样消失了。
    """
    for case in suite.cases:
        if not case.hidden_tests:
            continue
        resolved = (case.source_dir or base) / case.hidden_tests
        if not resolved.exists():
            raise SuiteConfigError(
                f"case {case.case_id!r}: hidden_tests not found: {resolved}")
        case.hidden_tests_path = resolved
        # 补上跑它的命令。argv 由加载器给默认值而不是让每条 case 手写 ——
        # 手写意味着 17 条用例里迟早有一条拼错，而拼错的表现是
        # "pytest 找不到文件"被记成 **agent 没修好**。
        for grader in case.graders:
            if grader.name == "OutcomeGrader":
                grader.config.setdefault(
                    "argv", [sys.executable, "-m", "pytest",
                             HIDDEN_TEST_RELPATH, "-q"])
