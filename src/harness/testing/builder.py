"""TrajectoryBuilder —— 构造事件序列，让评测器可在无 LLM 条件下单测。

## 它为什么作为产品的一部分发布

不是测试私有工具。**任何人写自己的评测器时都需要它** ——
「评测器可独立单测」这个卖点的兑现方式就是把这个能力交出去。

## 它替使用者挡掉的两个坑

1. **seq 与 call_id 配对**：手写轨迹最容易错的地方。错了会让评测器看到
   悬空配对，而症状是"评测结果不对"而非报错 —— 极难归因。
2. **畸形序列的注入**：测评测器的健壮性需要故意构造坏数据
   （悬空 call、缺 RUN_END、乱序 seq），`raw_emit` 提供这个能力。
"""

from __future__ import annotations

from typing import Any

from harness.contracts.protocols import ToolCall
from harness.events.trajectory import Trajectory
from harness.events.types import (
    EventType,
    LLMResponseEvent,
    RunEndEvent,
    RunStartEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnStartEvent,
    parse_event,
)

# 与 core/executors/local.py 的 LocalExecutor._cap() 输出格式保持一致。
# **两处必须同步修改**：这里是「构造侧」，那里是「生产侧」。
TRUNCATION_MARKER = "... [truncated] ..."


class TrajectoryBuilder:
    """链式构造轨迹。

    典型用法:

        traj = (TB(run_id="r1", task="fix bug")
                .turn()
                .llm_response(tool_calls=[("read_file", {"path": "a.py"})])
                .tool_result(name="read_file", content="code", ok=True)
                .turn()
                .llm_response(tool_calls=[("finish", {"summary": "done"})])
                .tool_result(name="finish", content="done", ok=True)
                .run_end(status="ok")
                .build())
    """

    def __init__(
        self,
        run_id: str = "run_test",
        *,
        task: str | None = None,
        role: str = "sut",
        model: str = "fake",
    ) -> None:
        self._run_id = run_id
        self._seq = 0
        self._turn = 0
        self._pending: list[ToolCall] = []
        self._call_counter = 0
        self._events: list[Any] = []
        self._start = RunStartEvent(
            run_id=run_id, seq=self._next_seq(), type=EventType.RUN_START,
            role=role, task=task, model=model, provider="builder",
        )

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    # ---- 构造 ----
    def turn(self) -> TrajectoryBuilder:
        self._turn += 1
        self._events.append(TurnStartEvent(
            run_id=self._run_id, seq=self._next_seq(),
            type=EventType.TURN_START, turn=self._turn,
        ))
        return self

    def llm_response(
        self,
        *,
        text: str = "",
        tool_calls: list[tuple] | None = None,
        finish_reason: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> TrajectoryBuilder:
        """记录一次模型响应。

        `tool_calls` 元素形态：`(name, arguments)` 或 `(name, arguments, call_id)`。
        省略 call_id 时自动生成，并登记为待配对。

        用量参数是必要的：`MetaEvaluator` 要从 judge 轨迹里读出它自己的成本，
        而成本是**一等指标**（judge_cost 与 sut 的 cost 严格分列）。
        不支持它们的话，那条路径只能靠 `of()` 手搓事件来测 —— 而手搓的
        事件形状与生产不一致，测了等于没测。
        """
        calls: list[ToolCall] = []
        for spec in tool_calls or []:
            name, args = spec[0], spec[1]
            if len(spec) > 2:
                cid = str(spec[2])
            else:
                cid = f"call_{self._call_counter}"
                self._call_counter += 1
            call = ToolCall(call_id=cid, name=name, arguments=args)
            calls.append(call)
            self._pending.append(call)

        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        content.extend({"type": "tool_use", "id": c.call_id, "name": c.name,
                        "input": c.arguments} for c in calls)

        self._events.append(LLMResponseEvent(
            run_id=self._run_id, seq=self._next_seq(),
            type=EventType.LLM_RESPONSE, turn=self._turn, model="fake",
            content=content, text=text,
            tool_calls=[{"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                        for c in calls],
            finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=0,
        ))
        # 为每个调用单独发 TOOL_CALL —— 真实 loop 就是这么做的，
        # builder 必须镜像它。漏掉这一步会让 `trajectory.tool_calls()` 为空，
        # 而评测器全都在读它。
        for call in calls:
            self._events.append(ToolCallEvent(
                run_id=self._run_id, seq=self._next_seq(),
                type=EventType.TOOL_CALL, turn=self._turn,
                call_id=call.call_id, name=call.name, arguments=call.arguments,
            ))
        return self

    def tool_result(
        self,
        *,
        name: str,
        content: str = "",
        ok: bool = True,
        call_id: str | None = None,
        error: str | None = None,
        error_type: str | None = None,
        denied_by: str | None = None,
        truncated: bool | None = None,
    ) -> TrajectoryBuilder:
        """记录工具结果。`call_id` 省略时自动匹配上一个未配对的同名调用。"""
        if call_id is None:
            match = next((c for c in self._pending if c.name == name), None)
            if match is not None:
                call_id = match.call_id
                self._pending.remove(match)
            else:
                call_id = f"unmatched_{self._call_counter}"
                self._call_counter += 1

        if truncated is None:
            # 与生产侧的输出格式对齐，自动推断截断状态。
            # 没有这层推断，GroundingChecker 的截断分支永远触发不了（恒为 False），
            # 「截断时只出 WARN 而不判 FAIL」这条规则就成了死代码。
            truncated = TRUNCATION_MARKER in content

        self._events.append(ToolResultEvent(
            run_id=self._run_id, seq=self._next_seq(),
            type=EventType.TOOL_RESULT, turn=self._turn,
            call_id=call_id, name=name, ok=ok, content=content,
            error=error, error_type=error_type, denied_by=denied_by,
            truncated=truncated,
        ))
        return self

    def raw_emit(self, event_type: EventType, **fields: Any) -> TrajectoryBuilder:
        """注入任意事件 —— 用于构造畸形序列测评测器健壮性。"""
        self._events.append(parse_event({
            "type": event_type.value,
            "run_id": self._run_id,
            "seq": self._next_seq(),
            **fields,
        }))
        return self

    def run_end(
        self,
        *,
        status: str = "ok",
        final_output: str | None = None,
        **fields: Any,
    ) -> TrajectoryBuilder:
        self._events.append(RunEndEvent(
            run_id=self._run_id, seq=self._next_seq(),
            type=EventType.RUN_END, status=status,
            final_output=final_output, turns=self._turn,
            **fields,
        ))
        return self

    def build(self) -> Trajectory:
        return Trajectory.from_events(self._run_id, [self._start, *self._events])
