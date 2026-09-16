"""隐藏验收测试 —— bug_report_width_excludes_header

只按数据算列宽，表头比数据长时整列视觉错位。输出看起来「差不多对」，所以肉眼扫一遍很容易放过。

## 这些测试不给被测 agent 看

它们在 run 结束、评测开始前才被拷进工作目录（`_hidden/test_hidden.py`）。
可见测试（`tests/`）覆盖的是"改完之后别把已有的东西弄坏"，
这里覆盖的是"这个 bug 真的被修掉了" —— 两者刻意不重合。

## 为什么 import 在 `sys.path` 操作**之后**

被测包（`csvlite`）的根目录要从 cwd 往上找：隐藏测试既可能躺在
被测 agent 的工作目录里，也可能被自检脚本从别处调用。
所以不能靠相对导入，也不能假设 pytest 的 rootdir 在哪。

这个顺序会让 ruff 报 `E402`（module level import not at top），
而 `suites/` 已经在 `pyproject.toml` 里排除出 ruff ——
那里写了理由（生成物、且"数据不是模块"）。
"""

import sys
from pathlib import Path


def _repo_root() -> Path:
    """往上找到含 `csvlite/` 的那一层。"""
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "csvlite").is_dir():
            return candidate
    raise RuntimeError("csvlite not found above cwd")


sys.path.insert(0, str(_repo_root()))

from csvlite.report import render_table
from csvlite.table import Table



def test_header_is_included_in_the_column_width():
    text = render_table(Table.from_rows([{"long_header_name": "x"}]))
    lines = text.splitlines()
    assert len(lines[0]) == len(lines[2]) == len("long_header_name")


def test_alignment_holds_with_ragged_data():
    table = Table.from_rows([{"a": "1"}, {"a": "12345"}])
    lines = render_table(table).splitlines()
    assert len(lines[0]) == len(lines[1]) == len(lines[2]) == len(lines[3])
