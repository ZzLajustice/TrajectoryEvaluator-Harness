"""洋葱模型中间件管道。

## 顺序语义

`middlewares[0]` 是**最外层**。给

    [Permission, Sandbox, Budget, Telemetry, Policy] + terminal=Executor

得到规范顺序：

    TOOL_CALL → Permission → Sandbox → Budget → Telemetry → Policy → Executor → TOOL_RESULT

## 为什么用 `reduce(reversed(...))` 而不是递归 `call_next`

递归写法要在闭包里捕获"下一层"，容易写出捕获了错误变量的 bug
（Python 的闭包是延迟绑定的）。`reduce` 从最内层向外逐层包裹，
每次 `wrap` 的 `nxt` 都是**已经固化好的**上一轮结果，没有延迟绑定问题。

管道在 `Run.__init__` 只构建一次，复用整个 run。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from functools import reduce
from typing import Any, TypeVar

Ctx = TypeVar("Ctx")
Out = TypeVar("Out")
Handler = Callable[[Ctx], Awaitable[Out]]


def build_pipeline(middlewares: Sequence[Any], terminal: Handler[Ctx, Out]) -> Handler[Ctx, Out]:
    """把中间件链与终端处理器折叠成一个可等待的处理器。

    `terminal` 是链条最内层（通常是真正的工具执行）。
    中间件不调用 `nxt` 即短路。
    """

    def wrap(nxt: Handler[Ctx, Out], mw: Any) -> Handler[Ctx, Out]:
        async def handler(ctx: Ctx) -> Out:
            return await mw.handle(ctx, nxt)  # type: ignore[no-any-return]

        return handler

    return reduce(wrap, reversed(middlewares), terminal)
