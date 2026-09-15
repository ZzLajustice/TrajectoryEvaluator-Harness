"""中间件工厂：把 `MiddlewareSpec` 配置变成实例。

## 为什么由工厂决定顺序而不是配置

规范顺序是

    Permission → Sandbox → Budget → Telemetry → Policy → Executor

**这是安全语义的一部分**，不是偏好：

  - `Permission` 必须在最外层 —— 被禁的工具不该先经过沙箱检查再被拒
  - `Telemetry` 必须在 `Policy` 之外 —— 策略拒绝也要留下事件（否则悬空配对）
  - `Budget` 在 `Telemetry` 之外 —— 预算拒绝同样要留痕

如果顺序由 suite 配置决定，写错一个顺序就会**静默**破坏这些性质。
所以工厂**强制**规范顺序，配置只决定"启用哪些"。

未知中间件名直接报错而非忽略 —— 拼错的名字会让人以为策略生效了。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from harness.contracts.spec import MiddlewareSpec
from harness.core.middleware.budget import BudgetMiddleware
from harness.core.middleware.permission import PermissionMiddleware
from harness.core.middleware.policy import PolicyMiddleware
from harness.core.middleware.sandbox import SandboxMiddleware
from harness.core.middleware.telemetry import TelemetryMiddleware

# 从外到内。改动这里等于改安全语义 —— 想清楚再动。
CANONICAL_ORDER: tuple[str, ...] = (
    "permission",
    "sandbox",
    "budget",
    "telemetry",
    "policy",
)

_FACTORIES: dict[str, Any] = {
    "permission": PermissionMiddleware,
    "sandbox": SandboxMiddleware,
    "budget": BudgetMiddleware,
    "telemetry": TelemetryMiddleware,
    "policy": PolicyMiddleware,
}


def build_middlewares(specs: Sequence[MiddlewareSpec]) -> list[Any]:
    """按规范顺序构造启用的中间件。"""
    enabled: dict[str, Any] = {}
    for spec in specs:
        if not spec.enabled:
            continue
        factory = _FACTORIES.get(spec.name)
        if factory is None:
            raise ValueError(
                f"unknown middleware {spec.name!r}; known: {sorted(_FACTORIES)}"
            )
        enabled[spec.name] = factory(spec)

    ordered = [enabled[name] for name in CANONICAL_ORDER if name in enabled]
    # 允许将来加自定义中间件：已知的按规范顺序，未知的排在最后
    ordered.extend(mw for name, mw in enabled.items() if name not in CANONICAL_ORDER)
    return ordered
