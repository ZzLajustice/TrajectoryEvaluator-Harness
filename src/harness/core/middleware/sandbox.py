"""沙箱中间件：**参数级**边界检查。

## 只管一件事：路径越狱

工具层（`fs.py` / `search.py`）也有一份同样的检查 —— **刻意冗余**：

    中间件层  拦得住**所有**工具的路径参数，包括将来新增的、忘记自己检查的工具
    工具层    拦得住绕过管道直接调用工具的路径（单测、将来的其他调用方）

## 为什么在 resolve 之后判断而不是看前缀

`sub/../../outside.txt` 的前缀 `sub/` 是合法的 ——
只有 `resolve()` 之后才知道它跑出去了。按前缀判断是最常见的写错方式，
所以有一条专门的测试盯住它。

## 不在这里做的事

**危险命令黑名单不在这层** —— 它属于 `run_command` 工具自身（命令语义是
工具的知识，不是通用的参数约束）。放在这里会让沙箱中间件需要理解
每种工具的参数语义，那就不是沙箱了。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.contracts.protocols import NextToolHandler, ToolResult
from harness.contracts.spec import MiddlewareSpec

# 各工具表示"路径"的参数名。新增工具时若用了别的名字，需要在这里补上。
_PATH_ARG_KEYS = ("path", "file", "filename", "cwd", "dir")


class SandboxMiddleware:
    name = "sandbox"

    def __init__(self, spec: MiddlewareSpec) -> None:
        self._spec = spec

    async def handle(self, ctx: Any, nxt: NextToolHandler) -> ToolResult:
        root_raw = getattr(ctx.ws, "root", None)
        if root_raw is None:
            return await nxt(ctx)
        try:
            root = Path(root_raw).resolve()
        except OSError:
            return await nxt(ctx)

        for key in _PATH_ARG_KEYS:
            value = ctx.call.arguments.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                target = (root / value).resolve()
            except (OSError, ValueError):
                target = None
            if target is None or (target != root and not target.is_relative_to(root)):
                return ToolResult(
                    call_id=ctx.call.call_id,
                    name=ctx.call.name,
                    ok=False,
                    error=f"path escapes workspace: {value!r}",
                    error_type="path_escape",
                    denied_by="sandbox",
                )
        return await nxt(ctx)
