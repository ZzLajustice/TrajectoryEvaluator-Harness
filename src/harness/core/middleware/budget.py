"""预算中间件：每次工具调用前校验。

## governor 从 `ctx` 取而非构造时注入

`BudgetGovernor` 是 **run 级**对象，在 `RunContext` 里创建；
而中间件实例在 `RunDeps` 里构造，早于 run 的创建 ——
构造时注入会造成"先有鸡还是先有蛋"。

因此 governor 经 `ToolCallContext.budget` 传入，每个调用读取。
这也符合状态作用域规约：中间件实例不持有调用级状态。
"""

from __future__ import annotations

from typing import Any

from harness.contracts.protocols import NextToolHandler, ToolResult
from harness.contracts.spec import MiddlewareSpec
from harness.core.budget import BudgetExceeded


class BudgetMiddleware:
    name = "budget"

    def __init__(self, spec: MiddlewareSpec) -> None:
        self._spec = spec

    async def handle(self, ctx: Any, nxt: NextToolHandler) -> ToolResult:
        governor = getattr(ctx, "budget", None)
        if governor is None:
            return await nxt(ctx)  # 未接预算治理时不该拦路

        try:
            governor.check_tool_call()
        except BudgetExceeded as exc:
            return ToolResult(
                call_id=ctx.call.call_id,
                name=ctx.call.name,
                ok=False,
                error=str(exc),
                error_type="budget_exceeded",
                denied_by="budget",
            )
        return await nxt(ctx)
