"""`finish` 工具 —— agent 显式声明任务完成的**唯一契约**。

## 为什么它值得单独一个文件

它不只是一个工具。`FailureClassifier` 检测「Unaware of termination」
（MAST FM-1.5，原论文占比 12.4%）正是靠"有没有调用 `finish`"：

    - 没有 finish 调用 → `RunStatus.NO_FINISH` / `MAX_TURNS` → 判定为该失败模式
    - 有 finish 调用   → 正常终止

所以它的**语义必须稳定**：不能因为模型没给 summary 就报错，
那会把"agent 完成了任务"变成"run 出错了"，污染整份评测结果。
"""

from __future__ import annotations

from typing import Any

from harness.contracts.protocols import ToolCall, ToolResult


class FinishTool:
    name = "finish"

    @property
    def description(self) -> str:
        return (
            "Signal that the task is complete. Call this when you are done — "
            "the run terminates as soon as it succeeds."
        )

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What you did, in one or two sentences.",
                    }
                },
                "required": ["summary"],
            },
        }

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:
        # ws 未使用 —— finish 不碰文件系统。参数保留是为了满足 Tool 协议。
        summary = call.arguments.get("summary")
        return ToolResult(
            call_id=call.call_id,
            name=self.name,
            ok=True,
            content=summary if isinstance(summary, str) else "",
        )
