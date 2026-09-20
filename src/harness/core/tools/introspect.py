"""自省工具：给 judge agent 用。

## 这些工具是 Agent-as-a-Judge 的全部意义所在

把整条轨迹塞进 prompt 让 LLM 一次性打分，那是"单次调用 + 长 prompt"。
judge 之所以是个 **agent**，就是因为它能**自己去查**：按需取轨迹片段、
自己读文件核对，而不是接受被测 agent 的总结。

没有这些工具，双 Harness 对称性架构就只是一个更贵的单次调用。

## 主体轨迹在**构造时注入**，不从 workspace 上取

开发计划里写的是 `getattr(ws, "trajectory", None)`。改成构造注入的实质差别是
**失败模式**：从 ws 上取的话，装配层忘了绑定就会静默返回 `not_available` ——
那看起来像"这条轨迹恰好没有内容"，而不是"接线错了"。
构造注入下，没有主体就是构造参数缺失，在装配点立刻暴露。

## 关于 `run_test`

计划里的 judge 工具白名单含 `run_test`（"独立验证被测 agent 的测试是否真的过"）。
**本任务没有实现它**，因为它在当前架构下会给出一个假的独立验证：
judge 有自己的沙箱（与 SUT 的 workspace 是两回事），
在 judge 自己的空目录里跑 pytest 什么也证明不了。
要真正实现，得让 judge 拿到**被测 workspace 的只读句柄** ——
那是一个独立的架构决定，不该顺手塞进这里。
"""

from __future__ import annotations

from typing import Any

from harness.contracts.protocols import ToolCall, ToolResult
from harness.events.reasoning import reasoning_of
from harness.events.trajectory import Trajectory


class ReadTrajectoryTool:
    """把（元）轨迹渲染成可读文本，支持按 seq 区间取片段。

    按需取片段是必须的：一条长轨迹动辄几百个事件，
    整条塞进 context 既贵又会把真正要看的那几步淹掉。
    """

    name = "read_trajectory"

    def __init__(self, subject: Trajectory | None = None) -> None:
        self._subject = subject

    @property
    def description(self) -> str:
        return (
            "Read the trajectory under evaluation as numbered event lines. "
            "Use start/end (event indexes) to inspect a slice."
        )

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "integer",
                              "description": "first event index (inclusive)"},
                    "end": {"type": "integer",
                            "description": "last event index (exclusive)"},
                },
                "required": [],
            },
        }

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:  # noqa: ARG002
        if self._subject is None:
            # 装配错误，不是"轨迹为空" —— 两者必须能区分
            return ToolResult(
                call.call_id, self.name, False,
                error="no subject trajectory bound to this judge tool",
                error_type="not_available",
            )

        try:
            start = _as_index(call.arguments.get("start"), default=0)
            end = _as_index(call.arguments.get("end"), default=len(self._subject.events))
        except ValueError as exc:
            return ToolResult(call.call_id, self.name, False, error=str(exc),
                              error_type="bad_arguments")

        events = list(self._subject.events[start:end])
        lines = [_render(e) for e in events]
        return ToolResult(call.call_id, self.name, True,
                          content="\n".join(lines) if lines else "(empty)")


def _as_index(raw: Any, *, default: int) -> int:
    """把参数转成索引。

    LLM 给出 `"3"` 这种字符串是常态 —— 能转就转，转不动就报清楚，
    而不是让 `int()` 的 TypeError 冒出去变成一次沙箱异常。
    """
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected an integer index, got {raw!r}") from exc


# 单条事件在渲染里最多占多少字符。太长会把上下文撑爆，
# 而 judge 需要的通常是"有没有这句话"，不是逐字全文。
_EXCERPT = 200


def _render(event: Any) -> str:
    """一行一个事件。带 seq 与类型，judge 才能用 start/end 精确取片段。

    ## 工具**结果的内容**必须渲染出来

    初版只渲染了工具名（`tool.result read_file`），那让 judge 根本无法
    完成它唯一的核心任务：核对"agent 说的话与工具实际返回的是否一致"。
    证据在 `content` 里，不渲染它，Agent-as-a-Judge 就退化成
    "读一遍事件目录然后猜"。

    这条是实测踩出来的：抗注入探针永远找不到注射内容，
    因为注射语在被测轨迹的 `tool.result.content` 里，而 judge 看不见它。
    """
    kind = event.type.value
    if kind == "llm.response":
        return _render_response(event)
    if kind == "tool.result":
        mark = "ok" if event.ok else f"FAIL {event.error_type or ''}".strip()
        body = (event.content or event.error or "").strip().replace("\n", " ")
        return f"{event.seq:>4}  {kind:<18}{event.name} [{mark}] {body[:_EXCERPT]}"
    if kind == "tool.call":
        args = str(getattr(event, "arguments", {})).replace("\n", " ")
        return f"{event.seq:>4}  {kind:<18}{event.name}({args[:_EXCERPT]})"
    if kind == "run.end":
        return f"{event.seq:>4}  {kind:<18}status={getattr(event, 'status', '')}"
    return f"{event.seq:>4}  {kind:<18}"


def _render_response(event: Any) -> str:
    """`llm.response` 那一行：**工具名或正文，加上模型的推理**。

    ## 推理必须渲染（本次新增）

    在此之前这一行只有工具名或 `text` —— 而实测里 `text` 经常是空字符串：
    17 条真轨迹里有轮次 `content` 为空、推理一千多字。那些轮次里模型在想什么，
    judge 一个字都看不到。

    实测症状（2026-09-16 全量真跑的某一条）：模型连续 5 轮在推理里写
    "no output? ... maybe the harness suppresses output"，
    而那些轮次的 `content` 全空、工具调用全是 `run_command`。
    judge 看到的是"重复调用同一个命令"，真因（它在排查环境）只存在于推理里。

    ## 为什么截断

    推理是可见输出的 **10.8 倍**（实测 79,306 vs 7,336 字符）。
    整段渲染会把 judge 的上下文撑爆，而它要看的那几步反而被淹掉 ——
    理由与 `tool.result` 的截断完全相同（见 `_EXCERPT`）。
    代价是长推理的后半段看不到；"按需读更长片段"仍未实现（known-gaps §2.2）。
    """
    calls = getattr(event, "tool_calls", None) or []
    if calls:
        head = f"tool_calls={[c.get('name') for c in calls]}"
    else:
        head = (getattr(event, "text", "") or "").strip().replace("\n", " ")[:_EXCERPT]

    found = reasoning_of(event)
    if found is None:
        return f"{event.seq:>4}  {'llm.response':<18}{head}"
    body = " ".join(found.text.split())[:_EXCERPT]
    return f"{event.seq:>4}  {'llm.response':<18}{head}  thinking: {body}"
