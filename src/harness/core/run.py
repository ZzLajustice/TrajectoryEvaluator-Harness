"""`Run` 抽象 —— sut / judge / classifier 共用的**唯一实现**。

## 双 Harness 对称的落点

被测 agent 与评测用的 judge agent 是**同一个类**，差异全部来自 `RunSpec` 的取值，
而非类型。因此 judge 自带完整轨迹 → 可审计、可复现、可测成本、可被元评测。

## 依赖注入

`Run` 不认识任何具体的 provider / store / executor —— 全部经 `RunDeps` 传入。
这是能用 `FakeProvider` 跑完整端到端路径的前提，也是单测不联网的前提。

## 事件落盘的并发设计

`emit()` 是**同步**的：只把事件塞进队列，由后台任务批量写 store。
两个收益：
  1. hot path 上没有 await 点，同一 turn 内的并行工具调用不会在埋点上串行化
  2. store 的锁不会成为并发 run 的瓶颈

代价是要管好后台任务的生命周期 —— `aclose()` 必须等到队列排空，
否则 run 结束时会有事件还在飞行中（表现为轨迹缺尾部事件，极难排查）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from harness.contracts.protocols import (
    LLMProvider,
    ToolCall,
    ToolResult,
    TrajectoryStore,
)
from harness.contracts.results import Usage
from harness.contracts.spec import RunSpec, RunStatus
from harness.core.budget import BudgetGovernor
from harness.core.context import ContextManager
from harness.core.middleware.context import ToolCallContext
from harness.core.pipeline import build_pipeline
from harness.core.registry import ToolRegistry
from harness.events.trajectory import Trajectory
from harness.events.types import EventType, RunEndEvent, RunStartEvent

_STOP = object()


class _EventSink:
    """同步 emit + 后台批量落盘。

    `emit` 不阻塞调用方；`aclose` 保证队列排空后才返回。
    """

    def __init__(self, store: TrajectoryStore) -> None:
        self._store = store
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task = asyncio.create_task(self._drain())

    def emit(self, event: Any) -> None:
        """同步入队 —— 这是 hot path 上唯一的埋点调用点。"""
        self._queue.put_nowait(event)

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            if event is _STOP:
                self._queue.task_done()
                return
            try:
                await self._store.append(event)
            finally:
                self._queue.task_done()

    async def aclose(self) -> None:
        """等队列排空。**漏掉这一步会让轨迹静默丢尾部事件。**"""
        self._queue.put_nowait(_STOP)
        await self._queue.join()
        await self._task


@dataclass(slots=True)
class RunDeps:
    """依赖注入点。Run 不认识任何具体实现，因此可用 Fake 替换。"""

    provider: LLMProvider
    store: TrajectoryStore
    tools: ToolRegistry
    middlewares: Sequence[Any] = ()
    id_gen: Callable[[], str] = field(
        default_factory=lambda: lambda: f"run_{int(time.time() * 1000) % 10_000_000}"
    )
    clock: Callable[[], float] = time.monotonic


@dataclass(slots=True)
class RunResult:
    run_id: str
    status: RunStatus
    final_output: str | None
    trajectory: Trajectory
    usage: Usage
    turns: int
    tool_calls: int
    duration_s: float
    error: str | None = None


class RunContext:
    """单次 run 的可变状态。贯穿 loop 与中间件。"""

    def __init__(self, run_id: str, spec: RunSpec, deps: RunDeps) -> None:
        self.run_id = run_id
        self.spec = spec
        self.deps = deps
        self.provider = deps.provider
        self.tool_names = deps.tools.names()

        self._seq = 0
        self.turns_executed = 0
        self.tool_calls_count = 0
        self.current_turn = 0
        self.final_output: str | None = None

        self.sink = _EventSink(deps.store)
        # governor 必须先建：BudgetMiddleware 从 ctx.budget 取它
        self.governor = BudgetGovernor(
            spec.budget, run_id=run_id, emit=self.emit, next_seq=self.next_seq
        )
        # 任务提示词在这里进入上下文 —— 缺了它 agent 不知道要做什么
        self.context = ContextManager(
            system_prompt=spec.system_prompt,
            token_budget=spec.budget.max_input_tokens,
            task=spec.task.prompt if spec.task else None,
        )
        # 管道只构建一次，复用整个 run
        self.tool_chain = build_pipeline(list(deps.middlewares), self._execute_tool)

    # ---- 事件 ----
    def next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def emit(self, event: Any) -> None:
        self.sink.emit(event)

    # ---- 工具调用 ----
    async def invoke_tool(self, call: ToolCall, turn: int) -> ToolResult:
        """经中间件管道执行工具。

        **异常在这里被转换成失败的 ToolResult** —— 中间件只负责记录后上抛，
        决策权在这里。这样设计是因为中间件不知道上层想怎么处理
        （重试？终止？降级？），把策略固化在中间件层是错的。
        """
        ctx = ToolCallContext(
            run_id=self.run_id, turn=turn, call=call, spec=self.spec,
            budget=self.governor,
            emit=self.emit, next_seq=self.next_seq,
        )
        try:
            return await self.tool_chain(ctx)
        except Exception as exc:  # noqa: BLE001
            # 注意：CancelledError 继承自 BaseException，不会被这里捕获 ——
            # 这是刻意的，并发场景下吞掉取消会导致任务悬挂。
            return ToolResult(
                call_id=call.call_id, name=call.name, ok=False,
                error=str(exc), error_type="sandbox_error",
            )

    async def _execute_tool(self, ctx: ToolCallContext) -> ToolResult:
        call = ctx.call
        if call.name not in self.tool_names:
            return ToolResult(
                call_id=call.call_id, name=call.name, ok=False,
                error=f"unknown tool: {call.name}", error_type="unknown_tool",
            )
        started = time.monotonic()
        result = await self.deps.tools.get(call.name).invoke(call, ctx.ws)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result


class Run:
    """sut / judge / classifier 共用的唯一实现。差异全部来自 RunSpec。"""

    def __init__(self, spec: RunSpec, deps: RunDeps) -> None:
        self.spec = spec
        self.deps = deps
        self.run_id = deps.id_gen()

    async def execute(self) -> RunResult:
        from harness.core.loop import agent_loop

        started = self.deps.clock()
        ctx = RunContext(self.run_id, self.spec, self.deps)

        ctx.emit(RunStartEvent(
            run_id=self.run_id, seq=ctx.next_seq(), type=EventType.RUN_START,
            role=self.spec.role.value,
            task=self.spec.task.prompt if self.spec.task else None,
            model=self.spec.model.model,
            provider=self.spec.model.provider,
            tools=ctx.tool_names,
            spec_json=self.spec.model_dump_json(),
        ))

        error: str | None = None
        try:
            status = await agent_loop(ctx)
        except Exception as exc:  # noqa: BLE001
            status, error = RunStatus.SANDBOX_ERROR, f"{type(exc).__name__}: {exc}"

        duration = self.deps.clock() - started
        ctx.emit(RunEndEvent(
            run_id=self.run_id, seq=ctx.next_seq(), type=EventType.RUN_END,
            status=status.value, final_output=ctx.final_output,
            turns=ctx.turns_executed,
            tool_calls=ctx.tool_calls_count,
            input_tokens=ctx.governor.usage().input_tokens,
            output_tokens=ctx.governor.usage().output_tokens,
            cost_usd=ctx.governor.usage().cost_usd,
            duration_s=duration,
        ))
        await ctx.sink.aclose()

        trajectory = await self.deps.store.get(self.run_id)
        end = trajectory.end()
        return RunResult(
            run_id=self.run_id, status=status, final_output=ctx.final_output,
            trajectory=trajectory, usage=ctx.governor.usage(),
            turns=ctx.turns_executed,
            tool_calls=end.tool_calls if end else 0,
            duration_s=duration, error=error,
        )
