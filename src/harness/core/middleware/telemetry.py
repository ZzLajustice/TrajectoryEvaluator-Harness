"""埋点中间件 —— 过程级评测的**全部**数据来源。

## 两条不可违反的规则（设计文档 R3）

1. **异常路径也必须落事件。** 否则评测器看到「有 TOOL_CALL 无 TOOL_RESULT」
   的悬空配对。这种损坏是**沉默的**：
     - `GroundingChecker` 失去比对判据
     - `EfficiencyAnalyzer` 漏计最严重的失败
     - `TrajectoryMatcher` 的长度断言产生误导
   而且是**逆向的** —— 越严重的失败越容易丢事件。

2. **`CancelledError` 必须继续传播。** 它继承自 `BaseException`，
   所以 `except Exception` 天然不会捕获它；但**绝不能写成 `except BaseException`** ——
   并发场景下吞掉取消会让任务悬挂。

## 记录者与决策者的分工

本中间件只**记录**：异常路径先发事件，然后**原样上抛**。
**决策**在 `RunContext.invoke_tool` 做（转成 `sandbox_error` 并让 loop 继续）。

中间件不知道上层想怎么处理异常（重试？终止？降级？），
把策略固化在中间件层是错误的分层。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from harness.contracts.protocols import NextToolHandler, ToolResult
from harness.contracts.spec import MiddlewareSpec
from harness.events.types import EventType, ToolResultEvent


class TelemetryMiddleware:
    name = "telemetry"

    def __init__(self, spec: MiddlewareSpec) -> None:
        self._spec = spec

    async def handle(self, ctx: Any, nxt: NextToolHandler) -> ToolResult:
        started = time.monotonic()
        try:
            result = await nxt(ctx)
        except asyncio.CancelledError:
            # 取消不是"工具失败"—— 不伪造结果事件，直接继续上抛
            raise
        except Exception as exc:  # noqa: BLE001
            failed = ToolResult(
                call_id=ctx.call.call_id, name=ctx.call.name, ok=False,
                error=f"{type(exc).__name__}: {exc}", error_type="middleware_error",
            )
            self._emit(ctx, failed, started)
            raise
        self._emit(ctx, result, started)
        return result

    def _emit(self, ctx: Any, result: ToolResult, started: float) -> None:
        # 工具自己测的耗时更准（它知道哪部分算执行）—— 有就不覆盖
        duration = result.duration_ms or int((time.monotonic() - started) * 1000)
        ctx.emit(ToolResultEvent(
            run_id=ctx.run_id,
            seq=ctx.next_seq(),
            type=EventType.TOOL_RESULT,
            turn=ctx.turn,
            call_id=result.call_id,
            name=result.name,
            ok=result.ok,
            content=result.content,
            error=result.error,
            error_type=result.error_type,
            duration_ms=duration,
            truncated=result.truncated,
            denied_by=result.denied_by,
        ))
