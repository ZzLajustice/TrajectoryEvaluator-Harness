"""权限中间件：按 `ToolPolicy` 拦截工具调用。

## 与 SandboxMiddleware 的分工

    Permission  管「这个**工具**能不能调」
    Sandbox     管「这个**参数**能不能传」

两层独立防线，短路时 `denied_by` 不同 —— `FailureClassifier` 依赖这个区分
把「agent 越权」与「agent 传错参数」分成两类失败。

## 与注册表的冗余

`ToolRegistry.schemas()` 已经按策略过滤了暴露面（模型看不到被禁的工具），
这里再拦一次是**刻意冗余**：模型仍可能幻觉出一个被禁的工具名，
那时注册表拦不住，只有中间件能拦。
"""

from __future__ import annotations

from typing import Any

from harness.contracts.protocols import NextToolHandler, ToolResult
from harness.contracts.spec import MiddlewareSpec


class PermissionMiddleware:
    name = "permission"

    def __init__(self, spec: MiddlewareSpec) -> None:
        self._spec = spec

    async def handle(self, ctx: Any, nxt: NextToolHandler) -> ToolResult:
        policy = ctx.spec.tools
        name = ctx.call.name

        denied = (policy.allow is not None and name not in policy.allow) or name in policy.deny
        if not denied:
            return await nxt(ctx)

        allowed = policy.allow if policy.allow is not None else "all except denied"
        return ToolResult(
            call_id=ctx.call.call_id,
            name=name,
            ok=False,
            error=f"tool {name!r} not permitted (allowed: {allowed})",
            error_type="permission_denied",
            denied_by="permission",
        )
