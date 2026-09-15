"""工具注册表。

职责只有一个：把注册的工具按 `ToolPolicy` 过滤成"要暴露给模型的 schema 列表"。

## 为什么不在这里做权限判定

权限是**执行期**的关注点（工具可能被动态禁用、策略可能依赖运行时状态），
由 `PermissionMiddleware` 在管道里处理。
注册表只管**暴露面** —— 模型根本看不到被策略禁止的工具，这比"看到了但被拒"更好：
它减少了模型尝试越权的机会。
"""

from __future__ import annotations

from typing import Any

from harness.contracts.protocols import Tool
from harness.contracts.spec import ToolPolicy


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            available = ", ".join(sorted(self._tools)) or "<none>"
            raise KeyError(f"unknown tool {name!r}; available: {available}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, policy: ToolPolicy) -> list[dict[str, Any]]:
        """按策略过滤出要暴露的工具 schema。

        `allow=None` 表示全部允许 —— 这是默认值，不能被误实现成"一个都不给"。
        deny 优先于 allow。
        """
        out: list[dict[str, Any]] = []
        for name in self.names():
            if policy.allow is not None and name not in policy.allow:
                continue
            if name in policy.deny:
                continue
            out.append(self._tools[name].schema())
        return out
