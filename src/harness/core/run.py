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
import itertools
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.contracts.protocols import (
    LLMProvider,
    ToolCall,
    ToolResult,
    TrajectoryStore,
)
from harness.contracts.results import Usage
from harness.contracts.spec import MiddlewareSpec, RunSpec, RunStatus
from harness.core.budget import BudgetGovernor
from harness.core.context import ContextManager
from harness.core.executors.local import LocalExecutor
from harness.core.middleware.context import ToolCallContext
from harness.core.middleware.telemetry import TelemetryMiddleware
from harness.core.pipeline import build_pipeline
from harness.core.registry import ToolRegistry
from harness.core.workspace import Workspace
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


_RUN_SEQ = itertools.count()


def default_run_id() -> str:
    """默认 run id 生成器。

    **毫秒时间戳单独用是不够的**：`--concurrency 8` 下调度器在同一个事件循环
    tick 里创建全部 run，`time.time()` 完全相同 → run_id 全部撞车 →
    多个 run 往同一个 `<run_id>.jsonl` 与控制台行里写，数据互相覆盖。
    加一个进程内自增序号把同毫秒的 run 区分开。
    """
    return f"run_{int(time.time() * 1000)}_{next(_RUN_SEQ):04d}"


@dataclass(slots=True)
class RunDeps:
    """依赖注入点。Run 不认识任何具体实现，因此可用 Fake 替换。"""

    provider: LLMProvider
    store: TrajectoryStore
    tools: ToolRegistry
    middlewares: Sequence[Any] = ()
    # 工作目录与执行器。为 None 时不建工作目录 —— 但**文件类工具会因此失败**，
    # 实测踩过：工具拿到 `ctx.ws = None` 后全部报 AttributeError，
    # 而沙箱中间件会因拿不到 root 而静默放行路径越狱。
    executor: Any = None
    workdir: Path | str = "workdir"
    id_gen: Callable[[], str] = field(default=default_run_id)
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
            # ★ `max_context_tokens` 而不是 `max_input_tokens`。
            # 后者是**累计**花费上限（跨轮次），拿它当上下文窗口用会让
            # "压缩"永远轮不到执行：累计值会先撞上限。
            token_budget=spec.budget.max_context_tokens,
            task=spec.task.prompt if spec.task else None,
        )
        # 工作目录由 Run.execute 在 setup 之后填入；工具与沙箱中间件都靠它
        self.ws: Any = None

        # 管道只构建一次，复用整个 run。
        # **自动补上 telemetry**：没有它就没有 TOOL_RESULT 事件，
        # 评测器会看到「有 TOOL_CALL 无 TOOL_RESULT」的悬空配对。
        # 「轨迹必须完整」是 Run 的不变量，不该由调用方记得配置。
        self.tool_chain = build_pipeline(self._ensure_telemetry(deps.middlewares),
                                         self._execute_tool)

    @staticmethod
    def _ensure_telemetry(middlewares: Sequence[Any]) -> list[Any]:
        """保证 telemetry 存在，且**在最外层**。

        它在最外层才能记录到所有中间件的拒绝 —— 在内层时，
        外层短路后它根本没机会执行，轨迹只剩悬空配对。
        """
        chain = [m for m in middlewares if getattr(m, "name", "") != "telemetry"]
        chain.insert(0, TelemetryMiddleware(MiddlewareSpec(name="telemetry")))
        return chain

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
            ws=self.ws, executor=self.deps.executor,
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
        """管道最内层：真正调用工具。

        **工具异常在这里就被转成 ToolResult**，不让它逃逸到中间件层。
        否则同一次失败会有两个名字：TelemetryMW 记成 `middleware_error`
        （它无法知道异常来自哪一层），而这里返回 `sandbox_error` ——
        事件流与返回值说法不一致，下游分析会分裂。
        """
        call = ctx.call
        if call.name not in self.tool_names:
            return ToolResult(
                call_id=call.call_id, name=call.name, ok=False,
                error=f"unknown tool: {call.name}", error_type="unknown_tool",
            )
        started = time.monotonic()
        try:
            result = await self.deps.tools.get(call.name).invoke(call, ctx.ws)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.call_id, name=call.name, ok=False,
                error=f"{type(exc).__name__}: {exc}", error_type="sandbox_error",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result


class Run:
    """sut / judge / classifier 共用的唯一实现。差异全部来自 RunSpec。"""

    def __init__(self, spec: RunSpec, deps: RunDeps) -> None:
        self.spec = spec
        self.deps = deps
        self.run_id = deps.id_gen()

    async def _open_workspace(self, ctx: RunContext) -> Any:
        """按 RunSpec 建工作目录。

        没有 workspace 配置时不建 —— 但那时文件类工具会失败，
        这是配置问题而非实现问题。
        """
        if self.spec.workspace is None:
            return None
        executor = self.deps.executor or LocalExecutor()
        ws = Workspace(
            self.spec.workspace,
            workdir=self.deps.workdir,
            run_id=self.run_id,
            executor=executor,
            case_id=self.spec.task.case_id if self.spec.task else "case",
        )
        await ws.setup()
        ctx.ws = ws
        return ws

    async def execute(self) -> RunResult:
        from harness.core.loop import agent_loop

        started = self.deps.clock()
        ctx = RunContext(self.run_id, self.spec, self.deps)

        # ★ 从 `RunContext` 建好那一刻起就必须有 finally 兜住 sink。
        # `RunContext.__init__` 会起一个后台 drain 任务，而 `_open_workspace`
        # 完全可能抛（非法路径、沙箱建不起来）。原先它不在任何 try 里，
        # 于是异常路径下 drain 任务永远悬着 —— 症状是进程退出时打一行
        # "Task was destroyed but it is pending!"，轨迹文件也一个字节都没写。
        # （实测踩出来的：judge 的 case_id 带冒号，Windows 上直接 NotADirectoryError。）
        try:
            # 工作目录：没有它，文件工具全部失败、沙箱检查静默放行
            workspace = await self._open_workspace(ctx)

            ctx.emit(RunStartEvent(
                run_id=self.run_id, seq=ctx.next_seq(), type=EventType.RUN_START,
                role=self.spec.role.value,
                task=self.spec.task.prompt if self.spec.task else None,
                model=self.spec.model.model,
                provider=self.spec.model.provider,
                tools=ctx.tool_names,
                spec_json=self.spec.model_dump_json(),
            ))

            # 先给 status 一个默认值：`CancelledError` 是 BaseException，
            # 不会被下面的 `except Exception` 捕获，此时 finally 里的引用会 NameError。
            status: RunStatus = RunStatus.CANCELLED
            error: str | None = None
            try:
                status = await agent_loop(ctx)
            except Exception as exc:  # noqa: BLE001
                status, error = RunStatus.SANDBOX_ERROR, f"{type(exc).__name__}: {exc}"
            finally:
                if workspace is not None:
                    # 失败时保留现场供调试（keep_on_failure）
                    await workspace.teardown(failed=(status is not RunStatus.OK))

            duration = self.deps.clock() - started
            # RUN_END 必须在 sink 关闭**之前**发 —— 放到外面就没人接它了，
            # 而轨迹缺尾部事件的症状是"评测器看到悬空配对"，极难反查。
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
        finally:
            # 等队列排空 —— 漏掉它会让轨迹静默丢尾部事件
            await ctx.sink.aclose()

        # aclose 之后轨迹才完整，这时读回来才是全的
        trajectory = await self.deps.store.get(self.run_id)
        end = trajectory.end()
        return RunResult(
            run_id=self.run_id, status=status, final_output=ctx.final_output,
            trajectory=trajectory, usage=ctx.governor.usage(),
            turns=ctx.turns_executed,
            tool_calls=end.tool_calls if end else 0,
            duration_s=duration, error=error,
        )
