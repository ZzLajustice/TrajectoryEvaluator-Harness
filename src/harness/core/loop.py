"""Agent 主循环。

**核心 loop 里不得出现任何评测代码** —— 评测埋点全部经中间件管道。
本模块只负责：构造请求 → 调模型 → 落事件 → 执行工具 → 判断终止。

## 三种终止语义（FailureClassifier 依赖这个区分）

    finish 调用成功      → OK        （正常完成）
    纯文本、无 tool_calls → NO_FINISH （agent 停止行动了，但没说完成）
    轮次耗尽             → MAX_TURNS  （持续行动但从不收敛）

后两者都归入 MAST 的「Unaware of termination」，但分开记录能看出行为差异：
NO_FINISH 是模型"以为说完了"，MAX_TURNS 是模型"陷在循环里"。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.contracts.pricing import with_cost
from harness.contracts.protocols import LLMRequest, Message
from harness.contracts.spec import RunStatus
from harness.core.budget import BudgetExceeded
from harness.events.types import (
    ErrorEvent,
    EventType,
    LLMRequestEvent,
    LLMResponseEvent,
    ToolCallEvent,
    TurnStartEvent,
)

if TYPE_CHECKING:
    from harness.core.run import RunContext


async def agent_loop(ctx: RunContext) -> RunStatus:
    # `while True` 而非 `range(max_turns)`：**governor 是轮次上限的唯一权威**。
    #
    # 早先用 `range(max_turns)` 时，轮次上限被两处强制 —— 循环边界与
    # governor 各管一次，结果是 `range` 先退出、返回 MAX_TURNS，
    # governor 那条分支永远走不到（终态语义因此变得不可预测）。
    #
    # 终止性由 governor 保证：`check_turn(turn)` 在 `turn >= max_turns` 时
    # 必定返回非 None，而 `turn` 每轮递增。
    turn = 0
    while True:
        # 预算检查放循环开头 —— 这是**终止性保证**，防死循环。
        # 超限返回独立终态而非抛异常：终止不是"出错"。
        if (stop := ctx.governor.check_turn(turn)) is not None:
            return stop

        ctx.current_turn = turn
        ctx.turns_executed = turn + 1

        ctx.emit(TurnStartEvent(
            run_id=ctx.run_id, seq=ctx.next_seq(), type=EventType.TURN_START, turn=turn,
        ))

        built = ctx.context.build_request(turn)
        ctx.emit(LLMRequestEvent(
            run_id=ctx.run_id, seq=ctx.next_seq(), type=EventType.LLM_REQUEST,
            turn=turn, model=ctx.spec.model.model,
            messages_digest=built.digest, message_count=len(built.messages),
            context_tokens_est=ctx.context.estimated_tokens(),
            tools_offered=ctx.tool_names,
        ))

        try:
            resp = await ctx.provider.complete(
                _to_provider_request(ctx, built.messages)
            )
        except Exception as exc:  # noqa: BLE001
            ctx.emit(ErrorEvent(
                run_id=ctx.run_id, seq=ctx.next_seq(), type=EventType.ERROR,
                turn=turn, where="llm", error_type=type(exc).__name__,
                message=str(exc), retryable=False,
            ))
            return RunStatus.LLM_ERROR

        # ★ 成本**先算一次**，事件与计费共用同一个数。
        #
        # 曾经的写法是事件里记 `resp.usage.cost_usd`（provider 只搬 token，
        # 所以是 0），只给 governor 补成本。后果是**轨迹说这次调用免费**，
        # 而报告里的钱来自 `RunResult.usage` —— 两处不一致，且以错的那处为准
        # （轨迹是真相源）。实测踩的：索引里 $0.000067，事件里 0.0。
        #
        # provider 只搬 token（它不该认识价格表），换算在 L0 的 pricing。
        usage = with_cost(resp.usage, resp.model)

        ctx.emit(LLMResponseEvent(
            run_id=ctx.run_id, seq=ctx.next_seq(), type=EventType.LLM_RESPONSE,
            turn=turn, model=resp.model, content=resp.content, text=resp.text,
            tool_calls=[
                {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                for c in resp.tool_calls
            ],
            finish_reason=resp.finish_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=resp.latency_ms,
            raw=resp.raw,
        ))
        try:
            ctx.governor.charge_usage(usage)
        except BudgetExceeded:
            # 用量超限同样是**独立终态** —— 不是 LLM 错误
            return RunStatus.BUDGET_EXCEEDED
        ctx.context.append_assistant(resp)

        if not resp.tool_calls:
            # 模型停止行动但没调 finish —— 立即终止，不白烧剩余轮次
            return RunStatus.NO_FINISH

        for call in resp.tool_calls:
            ctx.tool_calls_count += 1
            ctx.emit(ToolCallEvent(
                run_id=ctx.run_id, seq=ctx.next_seq(), type=EventType.TOOL_CALL,
                turn=turn, call_id=call.call_id, name=call.name,
                arguments=call.arguments,
            ))
            # **不在这里发 TOOL_RESULT** —— 那是 TelemetryMW 的职责。
            # 两处都发会产生重复事件（实测踩过），而重复的 tool.result
            # 会让 GroundingChecker / EfficiencyAnalyzer 重复计数。
            result = await ctx.invoke_tool(call, turn)
            ctx.context.append_tool_result(result)

            if call.name == "finish" and result.ok:
                ctx.final_output = result.content
                return RunStatus.OK

        # 压缩检查放轮末：本轮的工具结果已经进上下文，此时判断最准。
        # 事件由这里填充 run_id / seq —— ContextManager 不该知道自己在哪个 run 里。
        if ctx.context.needs_compaction():
            template = ctx.context.compact()
            if template is not None:
                ctx.emit(template.model_copy(update={
                    "run_id": ctx.run_id, "seq": ctx.next_seq(), "turn": turn,
                }))

        turn += 1


def _to_provider_request(ctx: RunContext, messages: list[Message]) -> LLMRequest:
    return LLMRequest(
        model=ctx.spec.model.model,
        messages=list(messages),
        tools=ctx.deps.tools.schemas(ctx.spec.tools) or None,
        temperature=ctx.spec.model.temperature,
        max_output_tokens=ctx.spec.model.max_output_tokens,
    )
