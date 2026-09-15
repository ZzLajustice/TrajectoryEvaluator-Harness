"""文件工具测试。

两个重点：
  1. **路径越狱防御** —— 这是沙箱边界的一部分，与 PermissionMW 是两层独立防线
  2. **文件操作走 Executor** —— 不直接碰本地文件系统，为将来的容器化留出替换点
"""

from __future__ import annotations

from pathlib import Path

from harness.contracts.protocols import ToolCall
from harness.core.executors.local import LocalExecutor
from harness.core.tools.fs import ListDirTool, ReadFileTool, WriteFileTool


class _WS:
    def __init__(self, root: Path) -> None:
        self.root = str(root)
        self.executor = LocalExecutor()
        self.keep = True


async def test_read_file_returns_content(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    r = await ReadFileTool().invoke(
        ToolCall("c1", "read_file", {"path": "a.txt"}), _WS(tmp_path))
    assert r.ok is True
    assert r.content == "hello"


async def test_read_missing_file_is_not_found_not_an_exception(tmp_path):
    r = await ReadFileTool().invoke(
        ToolCall("c1", "read_file", {"path": "nope.txt"}), _WS(tmp_path))
    assert r.ok is False
    assert r.error_type == "not_found"


async def test_read_directory_is_reported(tmp_path):
    (tmp_path / "sub").mkdir()
    r = await ReadFileTool().invoke(
        ToolCall("c1", "read_file", {"path": "sub"}), _WS(tmp_path))
    assert r.ok is False
    assert r.error_type == "is_directory"


async def test_relative_path_escape_is_blocked(tmp_path):
    r = await ReadFileTool().invoke(
        ToolCall("c1", "read_file", {"path": "../../etc/passwd"}), _WS(tmp_path))
    assert r.ok is False
    assert r.error_type == "path_escape"


async def test_absolute_path_outside_workspace_is_blocked(tmp_path):
    r = await ReadFileTool().invoke(
        ToolCall("c1", "read_file", {"path": "C:/Windows/win.ini"}), _WS(tmp_path))
    assert r.ok is False
    assert r.error_type == "path_escape"


async def test_sneaky_dotdot_inside_a_valid_prefix_is_still_blocked(tmp_path):
    """`sub/../../outside` 必须在解析**之后**判断 —— 只看前缀会被绕过。"""
    r = await ReadFileTool().invoke(
        ToolCall("c1", "read_file", {"path": "sub/../../outside.txt"}), _WS(tmp_path))
    assert r.ok is False
    assert r.error_type == "path_escape"


async def test_write_then_read_roundtrip(tmp_path):
    ws = _WS(tmp_path)
    w = await WriteFileTool().invoke(
        ToolCall("c1", "write_file", {"path": "sub/b.txt", "content": "data"}), ws)
    assert w.ok is True
    r = await ReadFileTool().invoke(ToolCall("c2", "read_file", {"path": "sub/b.txt"}), ws)
    assert r.content == "data"


async def test_write_creates_parent_directories(tmp_path):
    r = await WriteFileTool().invoke(
        ToolCall("c1", "write_file", {"path": "a/b/c.txt", "content": "x"}), _WS(tmp_path))
    assert r.ok is True
    assert (tmp_path / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "x"


async def test_write_path_escape_is_blocked(tmp_path):
    r = await WriteFileTool().invoke(
        ToolCall("c1", "write_file", {"path": "../evil.txt", "content": "x"}), _WS(tmp_path))
    assert r.ok is False
    assert r.error_type == "path_escape"
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_list_dir_marks_directories(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    r = await ListDirTool().invoke(ToolCall("c1", "list_dir", {"path": "."}), _WS(tmp_path))
    assert r.ok is True
    assert "d/" in r.content
    assert "f.txt" in r.content


async def test_list_dir_on_empty_directory(tmp_path):
    r = await ListDirTool().invoke(ToolCall("c1", "list_dir", {"path": "."}), _WS(tmp_path))
    assert r.ok is True
    assert "(empty)" in r.content


async def test_schemas_are_well_formed():
    for tool in (ReadFileTool(), WriteFileTool(), ListDirTool()):
        s = tool.schema()
        assert s["name"] == tool.name
        assert s["parameters"]["type"] == "object"
        assert isinstance(s["description"], str) and s["description"]
