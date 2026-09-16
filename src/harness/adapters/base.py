"""第三方轨迹源适配器接口 —— 通用性论证的落点。

## 这个 Protocol 解决什么问题

Harness 不只评测自研 agent。任何能产出 OTel GenAI 风格轨迹的系统
（LangGraph / AutoGen / 自研 agent 框架）都可以通过一个 adapter 接进来，
然后**立刻**获得本 harness 的全部评测能力：4+1 个评测器、双 Harness 对称的
judge、报告、baseline diff、CI 门禁。

这条边界的形状很窄：一个 `can_load` + 一个 `load` 返回 `Trajectory`。
一旦返回 `Trajectory`，下游所有东西都不需要知道数据从哪来 ——
因为 `Trajectory` 是评测器与被测 agent 之间**唯一**的交互面。

## 为什么 `Trajectory` 而不是别的东西

这是本项目分层的结果而不是巧合：`Trajectory` 住在 L0（`events/`），
不含任何 I/O，因此 adapter 只需要认识 L0 就能接入，
不需要 import `core` / `store` / `orchestration` —— 见
`tests/test_architecture.py::test_layer_imports_match_whitelist`。

## 与 `RunRole.EXTERNAL` 的关系

导入的轨迹固定标记为 `external` 角色。它在调度器里与 sut/judge 一样满足
`RunLike` 协议，因此能被聚合、比对、出报告 —— 但**不走 agent loop**
（那段过程已经发生在别处了）。把外部轨迹硬塞进 loop 是错的抽象：
loop 会试图驱动一个已经结束的会话。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from harness.events.trajectory import Trajectory


@runtime_checkable
class TrajectorySource(Protocol):
    """从外部格式导入轨迹。

    `runtime_checkable` 只能校验方法名是否存在（Python 的 Protocol 限制），
    但它至少让组装层可以按协议编程并做一次廉价的形状检查。
    """

    #: 适配器名，用于日志与错误信息（"没有 adapter 能读这个文件"）
    name: str

    def can_load(self, ref: str) -> bool:
        """这个 adapter 认不认这个引用。

        必须是**廉价且无副作用**的：调度器会对多个 adapter 依次调用它。
        且必须比"后缀对不对"更严 —— 只看后缀会让任意 `.jsonl` 都被认领，
        而认领之后拿到一条空轨迹，比明确报"读不了"更难排查。
        """
        ...

    async def load(self, ref: str) -> Trajectory:
        """读入并转成 `Trajectory`。

        `ref` 是不透明的字符串（路径 / URL / 对象存储 key），
        由各 adapter 自行解释 —— 接口不假设它是文件系统路径。
        """
        ...
