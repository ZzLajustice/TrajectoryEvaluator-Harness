"""评测器基类。

## 两条规约（违反它们会让架构约束名存实亡）

1. **评测器只依赖 `events` 与 `contracts`，绝不 import `core`。**
   需要 judge 的评测器通过 `EvalContext.judge`（`JudgeClient` 协议）触达，
   真实实现在组装层注入。这是「评测器与 agent 零耦合」的落点 ——
   有一条架构测试盯着 `evaluators/` 下的每一个文件。

2. **临时字段进 `Finding.data` 或事件的 `attrs`，不改事件 schema。**
   想给事件加字段前先自问：**能否从已有事件派生？**
   绝大多数「我需要 X 字段」其实是「我能从 TOOL_CALL / TOOL_RESULT 配对算出来」。

## 契约

- `subscribes` 的事件不存在 → 返回 `SKIPPED`（不抛异常）
- 自身抛异常 → 由调度器兜底为 `ERROR`（**与 `FAIL` 严格区分**）
- 退化输入（空轨迹 / 无 RUN_END / 截断）→ **绝不抛异常**
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Sequence
from typing import Any

from harness.contracts.protocols import EvalContext
from harness.contracts.results import EvalResult, EvalStatus
from harness.events.trajectory import Trajectory
from harness.events.types import EventType

__all__ = ["BaseEvaluator", "run_evaluators"]


class BaseEvaluator:
    """评测器基类。子类覆写 `name` / `subscribes` / `evaluate`。"""

    name: str = "unnamed"
    version: str = "0.1.0"
    # 声明关心哪些事件。调度器据此跳过不需要的评测器 —— 不实例化、不调用。
    subscribes: frozenset[EventType] = frozenset()

    def evaluate(self, traj: Trajectory, ctx: EvalContext) -> Any:
        raise NotImplementedError

    def skipped(self, traj: Trajectory, reason: str) -> EvalResult:
        """前置条件不满足时的标准返回。

        **用 SKIPPED 而非 FAIL** —— "我没法判"和"判定为失败"是两回事，
        混淆会虚高失败率。
        """
        return EvalResult(
            evaluator=self.name,
            evaluator_version=self.version,
            run_id=traj.run_id,
            status=EvalStatus.SKIPPED,
            summary=reason,
        )


async def run_evaluators(
    evaluator_classes: Sequence[type],
    traj: Trajectory,
    ctx: EvalContext,
) -> list[EvalResult]:
    """跑一组评测器，返回结果。顺序与传入顺序一致。

    住在 `evaluators/` 而不是 `orchestration/` 是**刻意**的：
    它只用到 `Trajectory` / `EvalContext` / `EvalResult` 三个 L0 类型，
    因此第三方写自己的评测器时，import 一个模块就同时拿到基类和调度器。
    一旦它挪进组装层，评测器的单测就得依赖 `orchestration` 才能跑。
    """
    present = {e.type for e in traj.events}
    out: list[EvalResult] = []

    for cls in evaluator_classes:
        # 声明式订阅在这里变成真行为：订阅的事件一个都不在轨迹里
        # → **在 cls() 之前** continue。判断放在构造之后的话，
        # subscribes 就只是个提示，调度器照样为每条轨迹构造全部对象。
        if not (cls.subscribes & present):
            continue

        started = time.monotonic()
        try:
            instance = cls()
            result = instance.evaluate(traj, ctx)
            # 同步与异步评测器共用一条路径 —— 纯函数评测器不必为了
            # 迎合调度器而写成 async。
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001
            # ERROR 表示「评测器自己有 bug」，FAIL 表示「被测 agent 有问题」。
            # 混淆会让最该被修的评测器 bug 伪装成 agent 的失败；
            # 且单个评测器崩掉不能中断其余的。
            out.append(EvalResult(
                evaluator=getattr(cls, "name", cls.__name__),
                run_id=traj.run_id,
                status=EvalStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            ))
            continue

        result.duration_ms = int((time.monotonic() - started) * 1000)
        out.append(result)

    return out
