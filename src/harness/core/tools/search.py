"""`search` 工具 —— 在工作目录内按正则搜索。

## 为什么用 Python 扫描而不是 shell grep

`run_command` 调 grep 在 Windows 上没有 `grep` 可执行文件 ——
"能不能找到文件"会因此变成一个**环境问题**而不是模型能力问题，
直接污染评测结果。

用 Python 自己扫（经 Executor 读文件）则跨平台行为一致，
且天然限制在 workspace 内。

## 为什么"搜不到"是成功而非失败

`no matches` 是**有效信息**（"这里没有"），agent 据此排除假设。
把它当失败会让 agent 反复重试同一个搜索。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.contracts.protocols import ToolCall, ToolResult

_DEFAULT_MAX_RESULTS = 50
_MAX_FILE_BYTES = 1_000_000  # 跳过超大文件，避免一个二进制文件拖垮整个搜索


class SearchTool:
    name = "search"

    @property
    def description(self) -> str:
        return (
            "Search file contents with a regular expression. Returns matches as "
            "'path:line: text'. Use this to locate code before editing it."
        )

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {"type": "string", "description": "Directory to search, default '.'."},
                    "max_results": {"type": "integer", "default": _DEFAULT_MAX_RESULTS},
                },
                "required": ["pattern"],
            },
        }

    async def invoke(self, call: ToolCall, ws: Any) -> ToolResult:
        pattern = call.arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(call.call_id, self.name, False,
                              error="pattern must be a non-empty string",
                              error_type="bad_arguments")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult(call.call_id, self.name, False,
                              error=f"invalid regular expression: {exc}",
                              error_type="bad_arguments")

        rel = str(call.arguments.get("path") or ".")
        root = Path(ws.root).resolve()
        try:
            target = (root / rel).resolve()
        except (OSError, ValueError):
            target = None
        if target is None or (target != root and not target.is_relative_to(root)):
            return ToolResult(call.call_id, self.name, False,
                              error=f"path escapes workspace: {rel!r}",
                              error_type="path_escape")

        limit = int(call.arguments.get("max_results") or _DEFAULT_MAX_RESULTS)
        hits = await self._scan(ws, root, target, regex, limit)
        if not hits:
            return ToolResult(call.call_id, self.name, True,
                              content=f"no matches for {pattern!r} under {rel}")
        return ToolResult(call.call_id, self.name, True, content="\n".join(hits))

    async def _scan(self, ws: Any, root: Path, target: Path,
                    regex: re.Pattern[str], limit: int) -> list[str]:
        hits: list[str] = []
        for path in self._walk(target):
            if len(hits) >= limit:
                break
            try:
                # 经 Executor 读 —— 为将来的容器化留出替换点
                data = await ws.executor.read_bytes(str(path), max_bytes=_MAX_FILE_BYTES)
            except (OSError, PermissionError):
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue  # 二进制文件跳过，不报错也不中断搜索
            display = path.relative_to(root).as_posix()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{display}:{lineno}: {line.strip()[:200]}")
                    if len(hits) >= limit:
                        break
        return hits

    @staticmethod
    def _walk(target: Path) -> list[Path]:
        if target.is_file():
            return [target]
        skip = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
        return [p for p in sorted(target.rglob("*"))
                if p.is_file() and not any(part in skip for part in p.parts)]
