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
    """参考轨迹。

    **只存工具名 + 参数子集，绝不存 LLM 原文** —— 存自然语言会让模型
    换个措辞就误判。`alternatives` 是必需的：代码修复任务的合法路径极多，
    单条 golden 会把好 run 判成 fail。
    """

    alternatives: list[list[dict[str, Any]]] = Field(default_factory=list)
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

    @model_validator(mode="after")
    def _check(self) -> CaseSpec:
        if self.repeat < 1:
            raise SuiteConfigError(f"case {self.case_id!r}: repeat must be >= 1")
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


def load_suite(path: Path | str) -> Suite:
    """加载并**完整校验** suite 文件。任何配置错误在这里抛错。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"suite not found: {path}")
    # ★ 只允许 safe_load —— 绝不执行 YAML 里的任意 Python 对象
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SuiteConfigError(
            f"suite root must be a mapping, got {type(raw).__name__}"
        )
    suite = Suite.model_validate(raw)
    suite.source_path = path
    return suite
