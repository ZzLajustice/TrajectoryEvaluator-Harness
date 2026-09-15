"""值对象与全部 Protocol —— 本项目的架构支点所在。

## 为什么这些类型在 L0 而不是 core

评测器需要触发 judge agent，但**绝不能 import `core` 或 `orchestration`** ——
否则「评测器与 agent 零耦合」这条约束无法成立。

解法是**依赖倒置**：把 `JudgeClient` 协议放在这里，评测器只认协议，
真实实现在组装层（`orchestration/judge.py`）注入，单测注入 `CannedJudge`。
没有这一层，「评测器零耦合」与「评测器能用 agent judge」会互相排斥。

## 值对象与事件模型刻意解耦

`ToolCall` / `ToolResult` 是中间件契约（稳定值对象）；
`ToolCallEvent` / `ToolResultEvent` 是可观测性 schema（会演进、要对齐 OTel）。
**二者不共用类** —— 否则「改 OTel 映射」会顺手改掉中间件签名。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from harness.contracts.results import Usage

if TYPE_CHECKING:
    from harness.events.trajectory import Trajectory


# --------------------------------------------------------------------------
# 值对象：中间件契约
# --------------------------------------------------------------------------
@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    call_id: str
    name: str
    ok: bool
    content: str = ""
    error: str | None = None
    error_type: str | None = None
    duration_ms: int = 0
    truncated: bool = False
    # 被哪个中间件拦下 —— FailureClassifier 依赖它区分失败来源
    denied_by: str | None = None


@dataclass(slots=True)
class ProcessResult:
    """子进程执行结果。放在 L0 是因为 `Executor` 协议要引用它。"""

    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    truncated: bool = False


@dataclass(slots=True)
class DirEntry:
    name: str
    is_dir: bool
    size: int = 0


@dataclass(slots=True)
class Message:
    """归一化的对话消息。

    content 是 Anthropic 风格的 content blocks —— 表达能力更强，
    映射到 OpenAI chat 是**可预测的有损**；反向会凭空发明结构。
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: list[dict[str, Any]]

    @staticmethod
    def user_text(text: str) -> Message:
        return Message(role="user", content=[{"type": "text", "text": text}])

    @staticmethod
    def system_text(text: str) -> Message:
        return Message(role="system", content=[{"type": "text", "text": text}])

    @staticmethod
    def tool_result(call_id: str, content: str, ok: bool) -> Message:
        # 错误也走同一条 content 通道 —— GroundingChecker 依赖原文可见
        return Message(
            role="tool",
            content=[{
                "type": "tool_result",
                "tool_call_id": call_id,
                "content": content if ok else f"ERROR: {content}",
            }],
        )


@dataclass(slots=True)
class LLMRequest:
    model: str
    messages: list[Message]
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    tool_choice: Literal["auto", "none", "required"] = "auto"


@dataclass(slots=True)
class LLMResponse:
    model: str
    content: list[dict[str, Any]]
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: Usage
    latency_ms: int
    # ★ provider 原始响应 —— replay 无损性的唯一保证
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# L3 核心 Protocol
# --------------------------------------------------------------------------
@runtime_checkable
class Tool(Protocol):
    name: str

    @property
    def description(self) -> str: ...

    def schema(self) -> dict[str, Any]:
        """JSON Schema，直接喂给 provider。"""
        ...

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult: ...


@runtime_checkable
class Executor(Protocol):
    """隔离边界。

    **文件操作也必须经过 Executor** —— 否则将来 Docker 化时文件工具会绕过容器，
    读到宿主机的文件。
    """

    name: str

    async def setup(self, ws: Any) -> None: ...
    async def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> ProcessResult: ...
    async def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes: ...
    async def write_bytes(self, path: str, data: bytes) -> None: ...
    async def list_dir(self, path: str) -> list[DirEntry]: ...
    async def teardown(self, ws: Any) -> None: ...


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def complete(self, req: LLMRequest) -> LLMResponse: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class TrajectoryStore(Protocol):
    """`append` 必须非阻塞（入队即返回）；`flush` 在 run 结束时 await。"""

    async def append(self, event: Any) -> None: ...
    async def flush(self) -> None: ...
    async def get(self, run_id: str) -> Trajectory: ...
    async def close(self) -> None: ...


NextToolHandler = Callable[[Any], Awaitable[ToolResult]]


@runtime_checkable
class Middleware(Protocol):
    """洋葱模型的一环。`handle` 不调用 `nxt` 即为短路。

    **短路时也必须返回完整的 `ToolResult`**（而非 None 或抛异常），
    否则评测器会看到「有 TOOL_CALL 无 TOOL_RESULT」的悬空配对。
    """

    name: str

    async def handle(self, ctx: Any, nxt: NextToolHandler) -> ToolResult: ...


# --------------------------------------------------------------------------
# 评测器侧（依赖倒置的接口面）
# --------------------------------------------------------------------------
@dataclass(slots=True)
class EvalContext:
    """评测器能触达的外部能力。刻意只有协议，没有具体类型。"""

    judge: JudgeClient | None = None
    store: TrajectoryStore | None = None
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class JudgeCase:
    case_id: str
    task: str
    traj: Trajectory
    rubric: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class JudgeVerdict:
    verdict: Literal["pass", "fail", "partial", "uncertain"]
    score: float | None
    rationale: str
    # ★ 指向 judge **自己的**轨迹 —— MetaEvaluator 靠它取轨迹做一致性分析
    judge_run_id: str | None = None
    usage: Usage | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class JudgeClient(Protocol):
    """评测器通过它触达 judge run，而无需 import core。

    真实实现在 `harness/orchestration/judge.py`（组装层注入）；
    单测注入 `CannedJudge`。
    """

    async def judge(self, case: JudgeCase, *, repeat: int = 1) -> list[JudgeVerdict]: ...
