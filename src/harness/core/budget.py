"""预算治理。

## 为什么 `BUDGET_EXCEEDED` 必须与错误分开

    budget_exceeded   agent 有资源但**没收敛**（单 agent 专属的失败模式）
    llm_error         外部依赖出错，**与 agent 能力无关**

混在一起会让两组指标同时失真：稳定性统计把"烧完预算"记成"服务不稳"，
能力统计把"没找到路"记成"运气不好"。
`FailureClassifier` 的「预算内未收敛」模式正是靠这个区分才有意义。

## 三处强制，缺一不可

    loop 每轮开头    check_turn()      turns / wall_clock —— 终止性保证
    LLM 调用前       precheck_llm()    估算，避免"为发现超预算而先花掉预算"
    LLM 调用后       charge_usage()    记账实际用量
    每次工具调用前   check_tool_call() 工具调用计数

## 告警去重

同一维度同一动作只发一次 `BUDGET_EVENT` —— 重复告警会淹没事件流，
让真正重要的那条信号被埋掉。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harness.contracts.results import Usage
from harness.contracts.spec import Budget, RunStatus
from harness.events.types import BudgetDimension, BudgetEvent, EventType

_DIMENSIONS: tuple[BudgetDimension, ...] = (
    "turns", "tool_calls", "input_tokens", "output_tokens", "usd", "wall_clock",
)
_USAGE_DIMENSIONS: tuple[BudgetDimension, ...] = (
    "input_tokens", "output_tokens", "usd",
)


class BudgetExceeded(Exception):
    """超出预算。携带维度信息 —— 上层据此判断是哪一类资源用尽。"""

    def __init__(self, dimension: str, limit: float, consumed: float) -> None:
        super().__init__(f"budget exceeded: {dimension} {consumed} > {limit}")
        self.dimension = dimension
        self.limit = limit
        self.consumed = consumed


@dataclass(slots=True)
class BudgetView:
    """暴露给工具与中间件的只读视图。"""

    _gov: "BudgetGovernor"

    def remaining(self, dimension: BudgetDimension) -> float:
        return max(0.0, self._gov.limit_of(dimension) - self._gov.consumed_of(dimension))

    def consumed(self, dimension: BudgetDimension) -> float:
        return self._gov.consumed_of(dimension)


class BudgetGovernor:
    """单次 run 的预算账本。run 级共享状态 —— 不持有调用级状态。"""

    def __init__(
        self,
        budget: Budget,
        *,
        run_id: str,
        emit: Callable[[Any], None],
        next_seq: Callable[[], int],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._b = budget
        self._run_id = run_id
        self._emit = emit
        self._next_seq = next_seq
        self._clock = clock
        self._started = clock()
        self._consumed: dict[str, float] = dict.fromkeys(_DIMENSIONS, 0.0)
        self._warned: set[str] = set()
        self._calls = 0

    # ---- 查询 ----
    def limit_of(self, dimension: BudgetDimension) -> float:
        return {
            "turns": self._b.max_turns,
            "tool_calls": self._b.max_tool_calls,
            "input_tokens": self._b.max_input_tokens,
            "output_tokens": self._b.max_output_tokens,
            "usd": self._b.max_usd,
            "wall_clock": self._b.max_wall_clock_s,
        }[dimension]

    def consumed_of(self, dimension: BudgetDimension) -> float:
        if dimension == "wall_clock":
            return self._clock() - self._started
        return self._consumed[dimension]

    @property
    def view(self) -> BudgetView:
        return BudgetView(self)

    def snapshot(self) -> dict[str, float]:
        return {d: self.consumed_of(d) for d in _DIMENSIONS}

    def usage(self) -> Usage:
        """累计用量。

        governor 已经是 token 与成本的**唯一账本**，让 `RunEndEvent` 从这里取，
        避免两处各记一份、迟早对不上。
        """
        return Usage(
            input_tokens=int(self._consumed["input_tokens"]),
            output_tokens=int(self._consumed["output_tokens"]),
            cost_usd=self._consumed["usd"],
            calls=self._calls,
        )

    # ---- 内部 ----
    def _warn_once(self, dimension: BudgetDimension) -> None:
        key = f"{dimension}:warn"
        if key in self._warned:
            return
        self._warned.add(key)
        self._emit(BudgetEvent(
            run_id=self._run_id, seq=self._next_seq(), type=EventType.BUDGET_EVENT,
            dimension=dimension, limit=self.limit_of(dimension),
            consumed=self.consumed_of(dimension),
            threshold=self._b.warn_at, action="warn"))

    def _terminate_event(self, dimension: BudgetDimension) -> None:
        self._emit(BudgetEvent(
            run_id=self._run_id, seq=self._next_seq(), type=EventType.BUDGET_EVENT,
            dimension=dimension, limit=self.limit_of(dimension),
            consumed=self.consumed_of(dimension), threshold=None, action="terminate"))

    def _check(self, dimension: BudgetDimension) -> bool:
        """返回 True 表示已超限。同时负责发告警/终止事件。"""
        limit, used = self.limit_of(dimension), self.consumed_of(dimension)
        if used >= limit:
            self._terminate_event(dimension)
            return True
        if limit > 0 and used >= limit * self._b.warn_at:
            self._warn_once(dimension)
        return False

    # ---- 检查点 ----
    def check_turn(self, turn: int) -> RunStatus | None:
        """Loop 每轮开头调用。**轮次上限的唯一权威。**

        超限返回终态而非抛异常 —— 终止性保证不该用异常表达。

        两个终态的语义不同，刻意区分：
          `MAX_TURNS`       轮次用尽 —— agent 一直在行动但没收敛
          `BUDGET_EXCEEDED` 其他资源用尽（wall_clock / token / 金额）
        这样 `FailureClassifier` 能区分"陷入循环"与"烧完预算"。
        """
        self._consumed["turns"] = turn
        if self._check("turns"):
            return RunStatus.MAX_TURNS
        if self._check("wall_clock"):
            return RunStatus.BUDGET_EXCEEDED
        return None

    def check_tool_call(self) -> None:
        """先检查额度再计数 —— 否则 `max_tool_calls=N` 会变成"只允许 N-1 次"。

        注意与 `check_turn` 的差别：那里 `turn` 是 0-indexed 且直接赋值，
        所以用 `>=` 判断正好；这里是自己累加，必须前置判断。
        """
        if self._consumed["tool_calls"] >= self._b.max_tool_calls:
            self._terminate_event("tool_calls")
            raise BudgetExceeded("tool_calls", self._b.max_tool_calls,
                                 self._consumed["tool_calls"])
        self._consumed["tool_calls"] += 1
        self._check("tool_calls")  # 仅在接近阈值时发告警

    def precheck_llm(self, est_input_tokens: int) -> None:
        projected = self._consumed["input_tokens"] + est_input_tokens
        if projected > self._b.max_input_tokens:
            raise BudgetExceeded("input_tokens", self._b.max_input_tokens, projected)

    def charge_usage(self, usage: Usage) -> None:
        self._calls += usage.calls or 1
        self._consumed["input_tokens"] += usage.input_tokens
        self._consumed["output_tokens"] += usage.output_tokens
        self._consumed["usd"] += usage.cost_usd
        for dim in _USAGE_DIMENSIONS:
            if self._check(dim):
                raise BudgetExceeded(dim, self.limit_of(dim), self._consumed[dim])
