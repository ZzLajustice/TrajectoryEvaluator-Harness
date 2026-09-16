"""把 suite 里的 `graders:` 配置变成评测器类。

## 调度逻辑不在这里

「该不该跑、跑了崩了怎么办」全在 `harness/evaluators/base.py::run_evaluators`。
本模块只管**配置 → 类**这一段，因为它需要认识具体的评测器类清单，
而那是组装层的知识。

## 白名单而非动态导入

suite 是数据，数据里的字符串不该能加载任意类。
评测器名字拼错时直接报错 —— 静默忽略会让人以为评测跑了。
"""

from __future__ import annotations

from typing import Any

from harness.evaluators.efficiency import EfficiencyAnalyzer
from harness.evaluators.failure_classify import FailureClassifier
from harness.evaluators.grounding import GroundingChecker
from harness.evaluators.meta import MetaEvaluator
from harness.evaluators.trajectory_match import TrajectoryMatcher

# 允许在 suite 里引用的评测器。
# **这是白名单的唯一一份** —— suite.py 直接引用它做加载期校验，
# 不另抄一份清单（两份必然漂移，且方向恰好是"加载期放行、运行期才炸"）。
EVALUATOR_REGISTRY: dict[str, type] = {
    "TrajectoryMatcher": TrajectoryMatcher,
    "EfficiencyAnalyzer": EfficiencyAnalyzer,
    "FailureClassifier": FailureClassifier,
    "GroundingChecker": GroundingChecker,
    "MetaEvaluator": MetaEvaluator,
}


def build_evaluators(specs: list[dict[str, Any]]) -> list[type]:
    """把 suite 的 graders 配置转成评测器类。

    未知名字直接报错 —— 拼错时静默忽略会让人以为评测跑了。
    """
    out: list[type] = []
    for spec in specs:
        name = str(spec.get("name", ""))
        cls = EVALUATOR_REGISTRY.get(name)
        if cls is None:
            raise ValueError(
                f"unknown evaluator {name!r}; known: {sorted(EVALUATOR_REGISTRY)}"
            )
        out.append(_configured(cls, spec.get("config") or {}))
    return out


def _configured(cls: type, config: dict[str, Any]) -> type:
    """把配置绑到类上，返回一个零参可实例化的子类。

    `run_evaluators` 只接受类（它负责实例化），因此这里做一层适配，
    而不是把配置透传到调用点 —— 保持调度器的接口简单。
    """
    if not config:
        return cls

    class _Configured(cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__(**config)

    _Configured.__name__ = cls.__name__
    _Configured.name = cls.name
    _Configured.subscribes = cls.subscribes
    return _Configured
