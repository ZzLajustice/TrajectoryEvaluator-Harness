"""通用策略中间件：配置驱动的声明式规则。

## 规则形态

`config["rules"]` 是列表，**任一命中即拒绝**：

    {"tool": "run_command", "deny_if_arg_contains": "pip install"}
    {"tool": "*",           "deny_if_arg_matches": "rm -rf"}
    {"deny_if_arg_contains": "SECRET"}          # 省略 tool 等价于 "*"

## 为什么把规则做成配置而非代码

策略会随场景变（不同 case 关心不同的越权形式），写死在代码里意味着
每加一条规则都要改代码、跑发布。做成声明式配置后，规则属于 suite 的一部分，
和用例一起被版本管理。
"""

from __future__ import annotations

import re
from typing import Any

from harness.contracts.protocols import NextToolHandler, ToolResult
from harness.contracts.spec import MiddlewareSpec


def _flatten(value: Any) -> str:
    """把参数结构展平成用于子串匹配的文本。

    **不能用 `str(value)` 了事**：`str(["pip", "install"])` 得到
    `"['pip', 'install']"` —— 引号与逗号会插进词之间，
    于是 `"pip install"` 这个子串根本不存在，规则静默失效。
    （这正是本项目真实踩过的 bug，测试抓住了它。）

    嵌套结构递归展开、用空格连接，让 `["pip", "install", "x"]`
    展平成 `"pip install x"`，与人的直觉一致。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


class PolicyMiddleware:
    name = "policy"

    def __init__(self, spec: MiddlewareSpec) -> None:
        rules = spec.config.get("rules") or []
        self._rules: list[tuple[str, str, str | None, re.Pattern[str] | None]] = []
        for rule in rules:
            self._rules.append((
                str(rule.get("tool", "*")),
                str(rule.get("deny_if_arg_contains", "")),
                rule.get("deny_if_arg_matches"),
                re.compile(str(rule["deny_if_arg_matches"]))
                if rule.get("deny_if_arg_matches") else None,
            ))

    def _violation(self, name: str, arguments: dict[str, Any]) -> str | None:
        blob = _flatten(arguments)
        for tool, needle, _raw, pattern in self._rules:
            if tool != "*" and tool != name:
                continue
            if needle and needle in blob:
                return f"argument contains {needle!r}"
            if pattern is not None and pattern.search(blob):
                return f"argument matches /{pattern.pattern}/"
        return None

    async def handle(self, ctx: Any, nxt: NextToolHandler) -> ToolResult:
        reason = self._violation(ctx.call.name, ctx.call.arguments)
        if reason is None:
            return await nxt(ctx)
        return ToolResult(
            call_id=ctx.call.call_id,
            name=ctx.call.name,
            ok=False,
            error=f"blocked by policy: {reason}",
            error_type="policy_denied",
            denied_by="policy",
        )
