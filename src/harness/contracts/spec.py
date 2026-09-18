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

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    #: **当前上下文窗口**的上限 —— 超过它触发 `CONTEXT_COMPACT`。
    #:
    #: ★ 必须与 `max_input_tokens` 分开。两者量纲看似相同，但语义完全不同：
    #:
    #:     max_input_tokens     **累计**输入 token（跨所有轮次）→ 花钱上限
    #:     max_context_tokens    **当前**上下文大小（单次请求）  → 容量上限
    #:
    #: 合成一个字段的后果是**两个旋钮互相锁死**：想造一个"上下文被迫压缩"
    #: 的场景就得把这个值调低，而调低会先撞上 governor 的累计上限、
    #: 让 run 以 `budget_exceeded` 结束 —— 压缩逻辑根本轮不到执行。
    #:
    #: 实测：`trap_context_pressure` 想考的就是压缩，而 `CONTEXT_COMPACT`
    #: 事件数一直是 0（峰值 9.3K，而当时两用的那个字段是 400K，差 43 倍）。
    #: 表面上"配了值"，实际上那个值管的是另一件事。
    max_context_tokens: int = 100_000


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
    #: case 私有的附加文件目录，在 `source` 之后、`patch` 之前覆写进工作目录。
    #:
    #: 为什么需要它：有些用例的考点是**环境里多了一个文件**
    #: （一段诱导注入的注释、一份大到会撑爆上下文的数据）。
    #: 把这类文件塞进 `bug.patch` 也能让 SUT 看见，但补丁就同时承担了
    #: "制造 bug"和"布置场景"两件事 —— 而 `fix.patch`（反向补丁）会顺手
    #: 把这些文件删掉，读起来像"修复等于删掉题目"。
    #: 分开之后补丁只含代码改动，场景由 overlay 负责，参考修复不会误伤它。
    overlay: str | None = None
    keep: bool = False
    keep_on_failure: bool = True

    @model_validator(mode="after")
    def _check_patch_has_something_to_patch(self) -> WorkspaceSpec:
        """★ 「要打补丁，但没有东西可打」必须是**加载期**的硬错误。

        漏掉 `source` 的后果不是报错，而是**工作目录是空的**：
        没有东西可拷 → 补丁的目标文件不存在 → 而 `git apply` 在 git 仓库
        内部对目标不存在的补丁会打出 `Skipped patch` 并**返回 0**。
        于是 SUT 拿到一个空目录、一路对着空气干活，
        最后报告上写的是「模型不会修 bug」。

        实测踩的：`_load_suite_dir` 忘了给 `source` 补默认值，
        17 条用例全部在空目录里跑，而四个门全绿 —— 因为整套测试
        都在仓库外的 `tmp_path` 里跑（那里 `git apply` 会**报错**而不是静默跳过）。

        确实想从空目录起步就写 `kind: tempdir` —— 那个值是显式的，不会被误读。
        """
        if self.patch and self.kind in {"copy", "git_worktree"} and not self.source:
            raise ValueError(
                f"workspace.kind={self.kind!r} with a patch but no `source`: "
                f"there is nothing to patch, and `git apply` inside a git repo "
                f"silently skips such patches (exit 0), leaving an EMPTY "
                f"workspace. Set `source`, or use `kind: tempdir` if the patch "
                f"is meant to create every file.")
        return self


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
