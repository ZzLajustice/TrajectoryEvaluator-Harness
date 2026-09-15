"""评测结果模型。

## 两条刻意的区分

1. **`EvalStatus.ERROR` 与 `EvalStatus.FAIL` 严格分开。**
   `ERROR` 表示**评测器自己有 bug**，`FAIL` 表示**被测 agent 有问题**。
   混淆会把「评测器崩了」记成「agent 失败了」，污染整份报告。

2. **`score=None` 表示"不适用"，不是 0 分。**
   例如 judge 只判一次时无法谈一致性 —— 此时应报告 `None` 而非假装完美。

`Finding.evidence` 指回事件流的 `seq`，让 HTML 报告能深链到具体某一步。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvalStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIPPED = "skipped"  # subscribes 的事件不存在，或前置条件不满足
    ERROR = "error"  # 评测器自身抛异常 —— 与 FAIL 严格区分


class Severity(StrEnum):
    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Usage(_Model):
    """token 与成本用量。

    **兼容估算值与实测值的关键约定**：`input_tokens` / `output_tokens` 一律填
    provider 上报的**权威值**；本地估算只用于触发压缩决策，不写进这里。
    两者混用会让评测数字不可信（tiktoken 对 DeepSeek / Qwen 无效，偏差可超 30%）。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            calls=self.calls + other.calls,
        )


class EvidenceRef(_Model):
    """指回事件流，让 HTML 报告能深链到具体某一步。"""

    seq: int | None = None
    span_id: str | None = None
    note: str = ""


class Finding(_Model):
    """一条具体发现。`code` 是机器可读的稳定标识，供报告聚合与回归比较。"""

    code: str  # 如 "grounding.fabricated_result"
    message: str
    severity: Severity = Severity.MINOR
    evidence: list[EvidenceRef] = Field(default_factory=list)
    # 失败模式分类（MAST 适用子集 + 单 agent 补充，见设计文档 §4.2）
    category: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EvalResult(_Model):
    evaluator: str
    evaluator_version: str = "0.1.0"
    run_id: str
    status: EvalStatus
    score: float | None = None  # 归一化到 [0,1]；None = 不适用
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    # 该评测器自身的成本。judge 兜底时有意义 —— 与 sut 的成本严格分离
    usage: Usage | None = None
    duration_ms: int = 0
    error: str | None = None
