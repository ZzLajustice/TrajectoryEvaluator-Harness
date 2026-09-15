"""中间件便捷基类。

覆盖约 80% 的场景（前置检查 + 后置加工）。需要短路或重试的中间件
直接实现 `handle()` —— 例如 `TelemetryMW` 需要 `try/except/else` 才能
保证异常路径也落事件。
"""

from __future__ import annotations

from typing import Any

from harness.contracts.protocols import NextToolHandler, ToolResult


class PrePostMiddleware:
    """`before → nxt → after` 形态。

    - `before` 返回非 None 即**短路**，不再调用下游
    - `after` 可以加工结果（例如补充耗时、脱敏）
    - `name` 必须设置 —— 它出现在 `RunSpec.middlewares` 的配置里
    """

    name: str = "unnamed"

    async def before(self, ctx: Any) -> ToolResult | None:
        return None

    async def after(self, ctx: Any, result: ToolResult) -> ToolResult:
        return result

    async def handle(self, ctx: Any, nxt: NextToolHandler) -> ToolResult:
        if (short := await self.before(ctx)) is not None:
            return short
        return await self.after(ctx, await nxt(ctx))
