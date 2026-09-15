"""`ToolCallContext` —— 中间件链的调用契约。

## 为什么它住在 middleware/ 而不是 contracts/

它是**中间件之间**的接口，会引用 core 侧的具体概念（workspace、executor、budget）。
放进 L0 就需要把那些概念也下沉，或者退化成一堆 `Any` —— 两者都不划算。
放在这里，`middleware/` 与 `core/run.py` 都从本模块导入，没有循环依赖。

## 状态作用域（配合包级 docstring 阅读）

本对象**每次工具调用新建一个实例**，因此并发安全。
调用级可变状态一律放 `scratch`，绝不要挂在中间件实例上。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harness.contracts.protocols import ToolCall
from harness.contracts.spec import RunSpec


@dataclass(slots=True)
class ToolCallContext:
    """一次工具调用的完整上下文。"""

    run_id: str
    turn: int
    call: ToolCall
    spec: RunSpec
    # 以下三个在 M1 阶段是占位（工作目录与预算治理分别在任务 15 / 21 落地）。
    # 用 Any 而非前向引用，避免在 core 内部制造不必要的导入依赖。
    ws: Any = None
    executor: Any = None
    budget: Any = None
    # 中间件之间传递信息的通道 —— 调用级状态一律放这里
    scratch: dict[str, Any] = field(default_factory=dict)
    # 落事件用的回调。刻意是**同步**的：hot path 上没有 await 点，
    # 并发工具调用不会在埋点上串行化。
    emit: Callable[[Any], None] = field(default=lambda _e: None)
    next_seq: Callable[[], int] = field(default=lambda: 0)
