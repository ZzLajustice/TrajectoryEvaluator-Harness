"""文件工具。

## 两条设计约束

1. **全部经 Executor 操作，绝不直接碰本地文件系统。**
   否则将来 DockerExecutor 落地时，文件工具会绕过容器读到宿主机的文件 ——
   而这类 bug 在本地开发时完全不可见。

2. **路径越狱防御在解析之后判断，而非看前缀。**
   `sub/../../outside.txt` 的前缀是合法的 `sub/`，只有 `resolve()` 之后
   才知道它跑出去了。这是最容易被写错的防御，所以有一条专门的测试盯住。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.contracts.protocols import ToolCall, ToolResult


def _resolve_in_workspace(ws: Any, rel: str) -> Path | None:
    """把相对路径解析到工作目录内。越狱返回 None。

    Windows 上 `C:/Windows/win.ini` 这类绝对路径会被 `root / rel` 忽略 root，
    因此解析后必须再判断一次是否仍在 root 下。
    """
    try:
        root = Path(ws.root).resolve()
    except OSError:
        return None
    try:
        target = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if target != root and not target.is_relative_to(root):
        return None
    return target


def _escape_result(call_id: str, name: str, rel: str) -> ToolResult:
    """路径越狱的统一返回。集中一处，避免各工具写出不同的 error_type。"""
    return ToolResult(call_id=call_id, name=name, ok=False,
                      error=f"path escapes workspace: {rel!r}",
                      error_type="path_escape")


class _FsTool:
    """文件工具的共同骨架。子类实现 name / description / schema / invoke。"""

    name: str = ""

    @property
    def description(self) -> str:
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        raise NotImplementedError


class ReadFileTool(_FsTool):
    name = "read_file"

    @property
    def description(self) -> str:
        return "Read a UTF-8 text file, relative to the workspace root."

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:
        rel = str(call.arguments.get("path", ""))
        target = _resolve_in_workspace(ws, rel)
        if target is None:
            return _escape_result(call.call_id, self.name, rel)
        # 显式判断而非依赖异常类型：Windows 上读目录抛 PermissionError 而非
        # IsADirectoryError，靠异常分支会得到平台相关的 error_type，
        # 而 FailureClassifier 要按 error_type 分类。
        if target.is_dir():
            return ToolResult(call.call_id, self.name, False,
                              error=f"is a directory: {rel}", error_type="is_directory")
        try:
            data = await ws.executor.read_bytes(str(target))
        except FileNotFoundError:
            return ToolResult(call.call_id, self.name, False,
                              error=f"not found: {rel}", error_type="not_found")
        except (IsADirectoryError, PermissionError):
            return ToolResult(call.call_id, self.name, False,
                              error=f"cannot read: {rel}", error_type="permission_denied")
        return ToolResult(call.call_id, self.name, True,
                          content=data.decode("utf-8", errors="replace"))


class WriteFileTool(_FsTool):
    name = "write_file"

    @property
    def description(self) -> str:
        return "Write UTF-8 text to a file. Parent directories are created as needed."

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        }

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:
        rel = str(call.arguments.get("path", ""))
        target = _resolve_in_workspace(ws, rel)
        if target is None:
            return _escape_result(call.call_id, self.name, rel)
        content = str(call.arguments.get("content", ""))
        await ws.executor.write_bytes(str(target), content.encode("utf-8"))
        return ToolResult(call.call_id, self.name, True,
                          content=f"wrote {len(content)} chars to {rel}")


class ListDirTool(_FsTool):
    name = "list_dir"

    @property
    def description(self) -> str:
        return "List files and directories. Directories are suffixed with '/'."

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "required": [],
            },
        }

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:
        rel = str(call.arguments.get("path") or ".")
        target = _resolve_in_workspace(ws, rel)
        if target is None:
            return _escape_result(call.call_id, self.name, rel)
        try:
            entries = await ws.executor.list_dir(str(target))
        except FileNotFoundError:
            return ToolResult(call.call_id, self.name, False,
                              error=f"not found: {rel}", error_type="not_found")
        if not entries:
            return ToolResult(call.call_id, self.name, True, content="(empty)")
        lines = [f"{e.name}/" if e.is_dir else f"{e.name}  ({e.size}B)" for e in entries]
        return ToolResult(call.call_id, self.name, True, content="\n".join(lines))
