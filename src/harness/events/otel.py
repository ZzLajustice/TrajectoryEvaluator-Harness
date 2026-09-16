"""OTel GenAI 语义约定投影层 —— 本项目的**对外互操作边界**。

## 为什么这是一个独立的投影模块

截至 2026-09，`gen_ai.*` 属性**没有任何一个达到 Stable**，全部是 Development。
规范原文写着 "SHOULD NOT be used in production"、"MAY be removed without prior
notice"。而且 2026-06 规范仓库已从核心 semconv 迁出到独立的
`semantic-conventions-genai`，属性名还在改：

    gen_ai.system                 → gen_ai.provider.name
    prompt_tokens / completion_tokens → input_tokens / output_tokens

**因此本项目的策略是：内部字段名是我们的稳定契约，OTel 命名只活在这一个文件里。**
semconv 改名只会改到这里，事件模型与所有评测器一行都不用动。

## dual-emit：同时发新旧两套 token 属性名

只发一套的代价是静默的：按另一套名字读的下游会拿到 0，
而"0 个 token"看起来只是"这次调用很短"。所以两个名字都发，
等对方稳定后再去掉 legacy 分支（届时删 `_LEGACY_*` 即可，其余不动）。

## 属性名常量在这里定义（导出与导入共用）

`adapters/otel_jsonl.py` **import 这些常量**而不是自己抄一份字符串。
抄一份的话，导出端改名后导入端还在读旧名 —— 而往返测试恰好不会覆盖
"两端用了不同的名字"这种情况（各自单测都过）。

## 不依赖 OTel SDK

本模块只产出普通 `dict`，可以直接 `json.dumps`。
OTel SDK 在 `[otel]` extra 里，**不进核心依赖** ——
理由是这个 harness 的核心能力（驱动 agent + 评测轨迹）与是否装了 SDK 无关，
而 SDK 会拖进 protobuf/gRPC 一大串东西。
"""

from __future__ import annotations

from typing import Any

from harness.events.trajectory import Trajectory
from harness.events.types import (
    LLMResponseEvent,
    RunStartEvent,
    ToolCallEvent,
    ToolResultEvent,
)

# 抓取/对齐的 semconv 版本。属性名改变时这里要跟着动。
SEMCONV_VERSION = "1.42.0"

# ---- 操作名 ----
OP = "gen_ai.operation.name"
OP_INVOKE_AGENT = "invoke_agent"
OP_CHAT = "chat"
OP_EXECUTE_TOOL = "execute_tool"

# ---- 身份与模型 ----
ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_CONVERSATION_ID = "gen_ai.conversation.id"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_FINISH_REASONS = "gen_ai.response.finish_reasons"
ATTR_SEMCONV_VERSION = "semconv.version"

# provider 的新名与旧名。旧名**只读不发** —— 发它等于延长一个已废弃属性的寿命。
ATTR_PROVIDER_NAME = "gen_ai.provider.name"
LEGACY_ATTR_SYSTEM = "gen_ai.system"

# ---- 工具 ----
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_CALL_ID = "gen_ai.tool.call.id"
ATTR_TOOL_RESULT = "gen_ai.tool.call.result"
ATTR_TOOL_RESULT_TRUNCATED = "gen_ai.tool.call.result.truncated"

# ---- 用量（新名 + 旧名）----
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
LEGACY_ATTR_PROMPT_TOKENS = "gen_ai.usage.prompt_tokens"
LEGACY_ATTR_COMPLETION_TOKENS = "gen_ai.usage.completion_tokens"

# ---- 状态 ----
ATTR_ERROR_TYPE = "error.type"
STATUS_ERROR = "ERROR"
# 真实的 OTLP JSON 里 status.code 可能带前缀，两种都认
_ERROR_CODES = frozenset({"ERROR", "STATUS_CODE_ERROR"})

# ---- 厂商扩展 ----
# semconv 没有对应属性，但它们是本 harness 的一等指标，丢了就没法评测。
# OTel 允许厂商前缀属性，故用 `harness.` 前缀。
VENDOR_LLM_TEXT = "harness.llm.text"
VENDOR_COST_USD = "harness.cost_usd"
VENDOR_TOOL_ARGUMENTS = "harness.tool.arguments"

# 边界标记：被策略拦下时 error.type 的前缀。
# 导入端靠它把 `policy_denied:policy` 还原成 `denied_by="policy"`。
DENIED_PREFIX = "policy_denied:"

# 视为"正常"的终态。其余终态会投影成 error.type ——
# 注意 `imported` 也在内：把导入来的轨迹再导出一次不该冒出 error.type。
_OK_STATUSES = frozenset({"ok", "imported"})


def trajectory_to_otlp(traj: Trajectory) -> list[dict[str, Any]]:
    """把轨迹投影成 OTLP 兼容的 span 列表（普通 dict，不依赖 SDK）。

    形状是**扁平**的：一条根 span + 每次 LLM 调用一条 + 每次工具调用一条。
    真实 OTLP 有 resource/scope 两层包装，这里不带 ——
    评测报告要的是 span 属性，包装层只会让下游多写两行解包代码。
    需要标准 OTLP 时由调用方自行包装。
    """
    start = traj.start()
    if start is None:
        # 空轨迹或截断到没有 run.start 的轨迹。返回空列表而不是抛异常：
        # 导出是偶尔才跑一次的路径，炸了往往没人发现。
        return []

    spans = [_root_span(traj, start)]
    spans.extend(_chat_span(r) for r in traj.llm_responses())
    spans.extend(_tool_span(c, traj.result_for(c.call_id)) for c in traj.tool_calls())
    return spans


def _root_span(traj: Trajectory, start: RunStartEvent) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        OP: OP_INVOKE_AGENT,
        ATTR_AGENT_NAME: start.role,
        ATTR_CONVERSATION_ID: traj.run_id,
        ATTR_PROVIDER_NAME: start.provider,
        ATTR_REQUEST_MODEL: start.model,
        ATTR_SEMCONV_VERSION: SEMCONV_VERSION,
    }
    end = traj.end()
    if end is not None:
        attrs.update(_token_attrs(end.input_tokens, end.output_tokens))
        attrs[VENDOR_COST_USD] = end.cost_usd
        if end.status not in _OK_STATUSES:
            # budget_exceeded / max_turns / no_finish 都是失败终态。
            # 不投影它们的话，导入的轨迹里"为什么结束"就丢了。
            attrs[ATTR_ERROR_TYPE] = end.status
    return {"name": f"{OP_INVOKE_AGENT} {start.role}", "attributes": attrs}


def _chat_span(resp: LLMResponseEvent) -> dict[str, Any]:
    """每次 LLM 调用一条 —— 用**每次调用**的量而非累计值。

    只有按次输出才能看出一轮花多少钱、哪一轮开始失控；
    累计值到了一半就再也分不出是哪一步涨的。
    """
    model = resp.model or "unknown"
    attrs: dict[str, Any] = {
        OP: OP_CHAT,
        ATTR_REQUEST_MODEL: model,
        ATTR_RESPONSE_MODEL: model,
    }
    if resp.finish_reason:
        attrs[ATTR_FINISH_REASONS] = [resp.finish_reason]
    attrs.update(_token_attrs(resp.input_tokens, resp.output_tokens))
    if resp.cost_usd is not None:
        attrs[VENDOR_COST_USD] = resp.cost_usd
    if resp.text:
        # semconv 的内容捕获目前是 event 形式的（未稳定），故用厂商属性承载。
        # 助手文本是 GroundingChecker 的输入，不能丢。
        attrs[VENDOR_LLM_TEXT] = resp.text
    return {"name": f"{OP_CHAT} {model}", "attributes": attrs}


def _tool_span(call: ToolCallEvent, result: ToolResultEvent | None) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        OP: OP_EXECUTE_TOOL,
        ATTR_TOOL_NAME: call.name,
        ATTR_TOOL_CALL_ID: call.call_id,
        # 参数是 TrajectoryMatcher 的 tool_args 维度的输入，必须留住。
        VENDOR_TOOL_ARGUMENTS: dict(call.arguments),
    }
    span: dict[str, Any] = {"name": f"{OP_EXECUTE_TOOL} {call.name}", "attributes": attrs}

    if result is None:
        # 悬空配对（有 call 无 result）是截断轨迹的常态，不是错误：
        # 进程被杀、或轨迹只保留了一部分。照旧导出一条 span。
        return span

    attrs[ATTR_TOOL_RESULT] = result.content
    if result.truncated:
        # ★ 截断标记不能省。`trap_fabricate` 的考点是"输出被截断了，
        # agent 却声称全部通过"—— 少了这个标记，GroundingChecker 就无法
        # 区分「没看到失败」与「没看」。
        attrs[ATTR_TOOL_RESULT_TRUNCATED] = True

    if result.denied_by:
        # 被策略拦下与真的执行失败是两回事，报告里要能分辨。
        attrs[ATTR_ERROR_TYPE] = f"{DENIED_PREFIX}{result.denied_by}"
    elif not result.ok:
        attrs[ATTR_ERROR_TYPE] = result.error_type or "tool_error"

    if not result.ok:
        span["status"] = {"code": STATUS_ERROR}
    return span


def _token_attrs(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    """dual-emit：新旧两套 token 属性名同时输出。"""
    return {
        ATTR_INPUT_TOKENS: input_tokens,
        ATTR_OUTPUT_TOKENS: output_tokens,
        LEGACY_ATTR_PROMPT_TOKENS: input_tokens,
        LEGACY_ATTR_COMPLETION_TOKENS: output_tokens,
    }
