"""从 OTel GenAI 风格的 JSONL 导入轨迹。

## 输入格式

每行一个 span 的 JSON，形状与 `events/otel.py::trajectory_to_otlp` 的输出
**互为逆运算**（往返测试见 `tests/adapters/test_otel_jsonl.py`）：

    {"name": "invoke_agent sut", "attributes": {"gen_ai.operation.name": "invoke_agent", ...}}
    {"name": "chat deepseek-flash", "attributes": {"gen_ai.operation.name": "chat", ...}}
    {"name": "execute_tool read_file", "attributes": {"gen_ai.operation.name": "execute_tool", ...}}

## 与设计草图的**有意偏差**：多认一种 operation

设计文档 §3.8 的映射表只有两行（invoke_agent / execute_tool）。实测发现
**只映射这两行会让导入的轨迹全是 0 token** —— 因为真实 OTel 轨迹里
LLM 调用是独立的 `chat` span，token 与成本都挂在它上面。

后果不是"少一点信息"，而是**效率分析、成本门禁、judge 成本全部归零**，
而 0 看起来像"这次很省"。所以这里额外映射 `chat`，并按其上的用量记账。

## 属性名从投影层 import，**不在这里抄一遍**

两处各抄一份字符串时，改了一处就会出现"导出用新名、导入读旧名"——
而两端各自的单测都会通过。共用常量让这种漂移在编译期就不成立。

## 不认识的 span 一律跳过

第三方轨迹里必然有 `embeddings`、`invoke_workflow`、自定义 span。
为一条不认识的 span 崩掉整个导入，等于宣布"只支持我们自己的导出格式"，
通用性就没了。**但坏 JSON 要报错并指行号** —— 静默跳过坏行会得到
"少了几个工具调用"的轨迹，评测器会把它当成 agent 没做那几步。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.adapters.base import TrajectorySource
from harness.events.otel import (
    _ERROR_CODES,
    ATTR_CONVERSATION_ID,
    ATTR_ERROR_TYPE,
    ATTR_FINISH_REASONS,
    ATTR_INPUT_TOKENS,
    ATTR_OUTPUT_TOKENS,
    ATTR_PROVIDER_NAME,
    ATTR_REQUEST_MODEL,
    ATTR_RESPONSE_MODEL,
    ATTR_TOOL_CALL_ID,
    ATTR_TOOL_NAME,
    ATTR_TOOL_RESULT,
    ATTR_TOOL_RESULT_TRUNCATED,
    DENIED_PREFIX,
    LEGACY_ATTR_COMPLETION_TOKENS,
    LEGACY_ATTR_PROMPT_TOKENS,
    LEGACY_ATTR_SYSTEM,
    OP,
    OP_CHAT,
    OP_EXECUTE_TOOL,
    OP_INVOKE_AGENT,
    VENDOR_COST_USD,
    VENDOR_LLM_TEXT,
    VENDOR_TOOL_ARGUMENTS,
)
from harness.events.trajectory import Trajectory
from harness.events.types import (
    EventType,
    LLMResponseEvent,
    RunEndEvent,
    RunStartEvent,
    ToolCallEvent,
    ToolResultEvent,
)

#: 导入的轨迹统一用这个终态 —— 与 `RunStatus.IMPORTED` 对应。
IMPORTED_STATUS = "imported"

#: 第三方 span 缺字段时的兜底值。不用 `None` ——
#: `RunStartEvent.model` 是必填的，缺了会让下游拿到空的模型名。
UNKNOWN = "unknown"


class OtelJsonlSource:
    """读 OTel GenAI 风格 JSONL 的 `TrajectorySource`。"""

    name = "otel_jsonl"

    def can_load(self, ref: str) -> bool:
        path = Path(ref)
        if not path.is_file() or path.suffix != ".jsonl":
            return False
        first = _first_nonblank_line(path)
        if first is None:
            return False
        try:
            span = json.loads(first)
        except ValueError:
            return False
        return _is_otel_span(span)

    async def load(self, ref: str) -> Trajectory:
        path = Path(ref)
        spans = _parse_spans(path)
        run_id = _run_id(spans, path)
        return Trajectory.from_events(run_id, _to_events(spans, run_id))


# ---- 解析 ----
def _first_nonblank_line(path: Path) -> str | None:
    """读首个非空行。

    用迭代而非 `read_text` —— `can_load` 会被调度器对所有 adapter 依次调用，
    不该为了看一眼而把整个文件读进内存。
    """
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                return line
    return None


def _is_otel_span(span: Any) -> bool:
    """判据是"attributes 里有 gen_ai.* 键"，不是后缀。

    这条判据刻意排除本 harness 自己写的轨迹文件（它的键是 `type`），
    否则 `can_load` 会把它们也认领，然后导入一条空轨迹 ——
    而"导入成功但什么都没有"比"读不了"难排查得多。
    """
    if not isinstance(span, dict):
        return False
    attrs = span.get("attributes")
    if not isinstance(attrs, dict):
        return False
    return any(str(key).startswith("gen_ai.") for key in attrs)


def _parse_spans(path: Path) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            span = json.loads(line)
        except ValueError as exc:
            raise ValueError(
                f"{path}: malformed JSON at line {lineno}: {exc}") from exc
        if isinstance(span, dict):
            spans.append(span)
    return spans


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    attrs = span.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _get(attrs: dict[str, Any], *names: str, default: Any = None) -> Any:
    """按顺序取第一个存在且非 None 的值 —— 新属性名优先，旧名兜底。"""
    for name in names:
        value = attrs.get(name)
        if value is not None:
            return value
    return default


def _run_id(spans: list[dict[str, Any]], path: Path) -> str:
    """优先取 `gen_ai.conversation.id`，缺失时才退回文件名。

    取自文件名会在"把轨迹复制成 trace-1.jsonl"时静默改名，
    而报告里所有指向该 run 的深链都会指错。
    """
    for span in spans:
        attrs = _attrs(span)
        if attrs.get(OP) == OP_INVOKE_AGENT:
            conversation = attrs.get(ATTR_CONVERSATION_ID)
            if conversation:
                return str(conversation)
    return path.stem


# ---- 事件构造 ----
def _to_events(spans: list[dict[str, Any]], run_id: str) -> list[Any]:
    events: list[Any] = []
    seq = 0
    input_tokens = 0
    output_tokens = 0
    cost_usd = 0.0
    turns = 0
    tool_index = 0

    # 先补 run.start：看不到 invoke_agent span 的片段也要有起点。
    # 缺它的话 `RunStartEvent.model/provider` 全缺，而一大堆评测器与报告
    # 会静默拿到空值（而不是报错），症状是"某个面板没数据"。
    start_attrs = next(
        (_attrs(s) for s in spans if _attrs(s).get(OP) == OP_INVOKE_AGENT), {})
    events.append(RunStartEvent(
        run_id=run_id, seq=seq, type=EventType.RUN_START,
        # 导入的一律是 external —— 这段过程发生在别的系统里，不走我们的 loop
        role="external",
        model=str(start_attrs.get(ATTR_REQUEST_MODEL, UNKNOWN)),
        provider=str(_get(start_attrs, ATTR_PROVIDER_NAME,
                          LEGACY_ATTR_SYSTEM, default=UNKNOWN)),
        spec_json=json.dumps({"imported_by": OtelJsonlSource.name}),
    ))
    seq += 1

    # 用量优先从 per-call 的 `chat` span 读；**没有 chat span 时才用根 span 的**。
    #
    # 两种来源不能相加：根 span 的用量是聚合值，加了就翻倍。而"只有根 span
    # 有用量"是常见情况（有些系统把总量记在 invoke_agent 上）。
    # 只认 chat span 的后果是 token 与成本全为 0 —— 而 0 看起来像"很省"。
    root_tokens_in = int(_get(start_attrs, ATTR_INPUT_TOKENS,
                              LEGACY_ATTR_PROMPT_TOKENS, default=0))
    root_tokens_out = int(_get(start_attrs, ATTR_OUTPUT_TOKENS,
                               LEGACY_ATTR_COMPLETION_TOKENS, default=0))
    has_chat_spans = any(_attrs(s).get(OP) == OP_CHAT for s in spans)
    if not has_chat_spans and (root_tokens_in or root_tokens_out):
        synthetic_cost = float(start_attrs.get(VENDOR_COST_USD) or 0.0)
        input_tokens += root_tokens_in
        output_tokens += root_tokens_out
        cost_usd += synthetic_cost
        turns += 1
        events.append(LLMResponseEvent(
            run_id=run_id, seq=seq, type=EventType.LLM_RESPONSE, turn=0,
            model=str(start_attrs.get(ATTR_REQUEST_MODEL, UNKNOWN)),
            # 这里刻意**不编造** per-call 明细：只知道总量，
            # 造一条假的"第 0 轮"比缺一条更容易误导（步数统计会偏）。
            input_tokens=root_tokens_in, output_tokens=root_tokens_out,
            cost_usd=synthetic_cost,
        ))
        seq += 1

    for span in spans:
        attrs = _attrs(span)
        op = attrs.get(OP)

        if op == OP_CHAT:
            tokens_in = int(_get(attrs, ATTR_INPUT_TOKENS,
                                 LEGACY_ATTR_PROMPT_TOKENS, default=0))
            tokens_out = int(_get(attrs, ATTR_OUTPUT_TOKENS,
                                  LEGACY_ATTR_COMPLETION_TOKENS, default=0))
            cost = float(attrs.get(VENDOR_COST_USD) or 0.0)
            input_tokens += tokens_in
            output_tokens += tokens_out
            cost_usd += cost
            turns += 1
            events.append(LLMResponseEvent(
                run_id=run_id, seq=seq, type=EventType.LLM_RESPONSE,
                turn=turns - 1,
                model=str(_get(attrs, ATTR_RESPONSE_MODEL, ATTR_REQUEST_MODEL,
                               default=UNKNOWN)),
                text=str(attrs.get(VENDOR_LLM_TEXT, "")),
                finish_reason=_first_finish_reason(attrs),
                input_tokens=tokens_in, output_tokens=tokens_out,
                cost_usd=cost,
            ))
            seq += 1

        elif op == OP_EXECUTE_TOOL:
            # 缺 call.id 时按**文件内位置**生成 id：确定性，且两次导入一致。
            # 用随机/时间戳会让同一条轨迹的评测结果抖动。
            call_id = str(attrs.get(ATTR_TOOL_CALL_ID) or f"c{tool_index}")
            tool_index += 1
            name = str(attrs.get(ATTR_TOOL_NAME, UNKNOWN))
            events.append(ToolCallEvent(
                run_id=run_id, seq=seq, type=EventType.TOOL_CALL,
                call_id=call_id, name=name,
                arguments=dict(attrs.get(VENDOR_TOOL_ARGUMENTS) or {}),
            ))
            seq += 1
            if _has_result(span, attrs):
                events.append(_tool_result(span, attrs, run_id, seq, call_id, name))
                seq += 1

        # 其他 operation（embeddings / invoke_workflow / 自定义）一律跳过。
        # invoke_agent 的重复出现同理：turn 不是 span，第一条已经是 run.start。

    events.append(RunEndEvent(
        run_id=run_id, seq=seq, type=EventType.RUN_END,
        status=IMPORTED_STATUS, turns=turns, tool_calls=tool_index,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost_usd,
    ))
    return events


def _has_result(span: dict[str, Any], attrs: dict[str, Any]) -> bool:
    """这条工具 span 是否带着"调用已结束"的证据。

    ★ 判据不能只是"有没有结果内容"：内容为空是合法的（`write_file` 成功就返回空），
    而**悬空配对**（进程被杀、只发了 call）是截断轨迹的常态。
    把后者补成一条 `ok=True` 的空结果，会让"agent 调了工具但没拿到结果"
    看起来像"工具成功了" —— 而 FailureClassifier 正是靠这个区分失败模式。
    """
    return (
        ATTR_TOOL_RESULT in attrs
        or ATTR_ERROR_TYPE in attrs
        or _status_code(span) is not None
    )


def _tool_result(span: dict[str, Any], attrs: dict[str, Any], run_id: str,
                 seq: int, call_id: str, name: str) -> ToolResultEvent:
    error_type = attrs.get(ATTR_ERROR_TYPE)
    denied_by = None
    if isinstance(error_type, str) and error_type.startswith(DENIED_PREFIX):
        # 还原被拦下这件事：报告里"被策略拦下"与"执行失败"必须能分辨。
        denied_by = error_type[len(DENIED_PREFIX):]
        error_type = "denied"

    return ToolResultEvent(
        run_id=run_id, seq=seq, type=EventType.TOOL_RESULT,
        call_id=call_id, name=name,
        # 判失败的依据是 OTLP 的 status.code，**外加** error.type 的存在：
        # 第三方实现里常见"只填 error.type 不填 status"，只认 status 会把
        # 失败导入成成功 —— 那比不导入更糟。
        ok=_status_code(span) not in _ERROR_CODES and attrs.get(ATTR_ERROR_TYPE) is None,
        content=str(attrs.get(ATTR_TOOL_RESULT, "")),
        error_type=str(error_type) if error_type else None,
        truncated=attrs.get(ATTR_TOOL_RESULT_TRUNCATED) is True,
        denied_by=denied_by,
    )


def _status_code(span: dict[str, Any]) -> str | None:
    status = span.get("status")
    if not isinstance(status, dict):
        return None
    code = status.get("code")
    return str(code) if code is not None else None


def _first_finish_reason(attrs: dict[str, Any]) -> str | None:
    """`gen_ai.response.finish_reasons` 是数组，取第一个。"""
    reasons = attrs.get(ATTR_FINISH_REASONS)
    if isinstance(reasons, list) and reasons:
        return str(reasons[0])
    if isinstance(reasons, str):
        return reasons
    return None


# 让类型检查器确认这个类真的满足协议（静态双向检查，runtime_checkable
# 只能校验方法名存在与否）。
_source_is_a_trajectory_source: type[TrajectorySource] = OtelJsonlSource
