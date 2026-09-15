"""search 工具测试。

## 为什么要有这个工具

代码修复类任务里，agent 通常得先**定位**再修改。没有 `search` 它只能
`list_dir` 逐个目录翻，或者用 `run_command` 调 grep —— 后者依赖平台
（Windows 没有 grep），会让"能不能找到文件"变成一个环境问题而非模型能力问题。

`search` 用 Python 自己扫描（经 Executor 读文件），行为跨平台一致。
"""

from __future__ import annotations

from pathlib import Path

from harness.contracts.protocols import ToolCall
from harness.core.executors.local import LocalExecutor
from harness.core.tools.search import SearchTool


class _WS:
    def __init__(self, root: Path) -> None:
        self.root = str(root)
        self.executor = LocalExecutor()
        self.keep = True


def _tree(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8")
    (root / "src" / "b.py").write_text("import os\n\nVALUE = 42\n", encoding="utf-8")
    (root / "README.md").write_text("# Project\n\nhas an add function\n", encoding="utf-8")


async def test_finds_a_pattern_across_files(tmp_path):
    _tree(tmp_path)
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": "def add"}), _WS(tmp_path))
    assert r.ok is True
    assert "a.py" in r.content
    assert "def add" in r.content


async def test_reports_line_numbers(tmp_path):
    """行号是 agent 定位修改点的依据 —— 缺了它搜索结果的可用性大打折扣。"""
    _tree(tmp_path)
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": "VALUE = 42"}), _WS(tmp_path))
    assert "b.py:3" in r.content


async def test_searches_all_file_types_by_default(tmp_path):
    _tree(tmp_path)
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": "add function"}), _WS(tmp_path))
    assert "README.md" in r.content


async def test_path_argument_narrows_the_search(tmp_path):
    _tree(tmp_path)
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": "add", "path": "src"}), _WS(tmp_path))
    assert "a.py" in r.content
    assert "README.md" not in r.content


async def test_regex_patterns_are_supported(tmp_path):
    _tree(tmp_path)
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": r"def \w+\("}), _WS(tmp_path))
    assert r.ok is True
    assert "def add(" in r.content and "def sub(" in r.content


async def test_no_match_is_a_successful_empty_result(tmp_path):
    """搜不到不是错误 —— 它是有效信息（"这里没有"）。"""
    _tree(tmp_path)
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": "zzz_absent_zzz"}), _WS(tmp_path))
    assert r.ok is True
    assert "no matches" in r.content.lower()


async def test_invalid_regex_is_a_bad_arguments_failure(tmp_path):
    """正则语法错误是 agent 的输入问题，报错要指向参数而非崩溃。"""
    _tree(tmp_path)
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": "([unclosed"}), _WS(tmp_path))
    assert r.ok is False
    assert r.error_type == "bad_arguments"


async def test_missing_pattern_is_rejected(tmp_path):
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"path": "."}), _WS(tmp_path))
    assert r.ok is False
    assert r.error_type == "bad_arguments"


async def test_path_escape_is_blocked(tmp_path):
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": "x", "path": "../.."}), _WS(tmp_path))
    assert r.ok is False
    assert r.error_type == "path_escape"


async def test_max_results_caps_the_output(tmp_path):
    """大仓库里搜索结果可能极长 —— 必须能封顶，否则会撑爆上下文。"""
    (tmp_path / "many.txt").write_text("hit\n" * 500, encoding="utf-8")
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": "hit", "max_results": 5}), _WS(tmp_path))
    assert r.ok is True
    assert r.content.count("many.txt") == 5


async def test_binary_files_are_skipped_not_crashed(tmp_path):
    (tmp_path / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (tmp_path / "ok.txt").write_text("needle\n", encoding="utf-8")
    r = await SearchTool().invoke(
        ToolCall("c1", "search", {"pattern": "needle"}), _WS(tmp_path))
    assert r.ok is True
    assert "ok.txt" in r.content


async def test_schema_declares_pattern_required():
    s = SearchTool().schema()
    assert s["name"] == "search"
    assert "pattern" in s["parameters"]["required"]
