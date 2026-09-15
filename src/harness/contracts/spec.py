"""RunSpec 及其组成。全部 pydantic 模型，全部可完整 JSON 序列化。

## 为什么可序列化是硬要求

`RunSpec` 会被整个塞进 `RunStartEvent.spec_json`。这样评测器**不需要回查 suite 配置**
就能理解一次 run 的完整上下文 —— 轨迹文件因此是自解释的。

这条约束传导到 `MiddlewareSpec`：它只存**名字 + 配置**，不存中间件实例。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RunRole(StrEnum):
    SUT = "sut"
    JUDGE = "judge"
    CLASSIFIER = "classifier"
    EXTERNAL = "external"


class RunStatus(StrEnum):
    """一次 run 的终态。

    `BUDGET_EXCEEDED` 与 `LLM_ERROR` **刻意分开** —— 它们是完全不同的失败模式：
      - `budget_exceeded`：agent 有资源但没收敛（单 agent 专属的失败模式）
      - `llm_error`：外部依赖出错，与 agent 能力无关
    FailureClassifier 依赖这个区分，混淆会让稳定性与能力两个指标同时失真。
    """

    OK = "ok"
    NO_FINISH = "no_finish"
    MAX_TURNS = "max_turns"
    BUDGET_EXCEEDED = "budget_exceeded"
    POLICY_TERMINATED = "policy_terminated"
    LLM_ERROR = "llm_error"
    SANDBOX_ERROR = "sandbox_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    IMPORTED = "imported"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelRef(_Model):
    provider: str
    model: str
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Budget(_Model):
    """预算上限。

    **轮次上限的唯一真相源就是这里的 `max_turns`** —— RunSpec 刻意不设同名字段，
    避免两个都能配、实现读哪个不明确。
    """

    max_turns: int = 20
    max_tool_calls: int = 60
    max_input_tokens: int = 400_000
    max_output_tokens: int = 40_000
    max_usd: float = 2.0
    max_wall_clock_s: float = 300.0
    warn_at: float = 0.8


class ToolPolicy(_Model):
    allow: list[str] | None = None  # None = 全部允许
    deny: list[str] = Field(default_factory=list)


class MiddlewareSpec(_Model):
    """只存名字与配置 —— 这是 RunSpec 可序列化的前提。"""

    name: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class WorkspaceSpec(_Model):
    kind: Literal["copy", "git_worktree", "tempdir"] = "copy"
    source: str | None = None  # 相对仓库根的路径
    patch: str | None = None  # case 私有补丁（如 bug.patch）
    keep: bool = False
    keep_on_failure: bool = True


class TaskSpec(_Model):
    case_id: str
    prompt: str
    visible_tests: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


# 不进指纹的字段：不影响 agent 行为
_FP_IGNORED = ("metadata",)
_FP_IGNORED_WORKSPACE = ("keep", "keep_on_failure")


class RunSpec(_Model):
    role: RunRole
    system_prompt: str
    model: ModelRef
    task: TaskSpec | None = None
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    middlewares: list[MiddlewareSpec] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    workspace: WorkspaceSpec | None = None
    agent_name: str = "sut"
    metadata: dict[str, Any] = Field(default_factory=dict)

    def fingerprint(self) -> str:
        """影响行为的字段的 sha256（取前 16 位）。

        用途：baseline diff 时判断两次 run 是否**可比**。模型、prompt、工具集变了
        就不是同一个实验，直接比数字会得出错误结论。

        `metadata` 与 workspace 的 `keep` / `keep_on_failure` 被排除 ——
        它们是调试开关，不改变 agent 行为。
        """
        data = self.model_dump(mode="json", exclude=set(_FP_IGNORED))
        ws = data.get("workspace")
        if isinstance(ws, dict):
            for k in _FP_IGNORED_WORKSPACE:
                ws.pop(k, None)
        blob = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
