"""隐藏验收测试 —— bug_column_order_derived_by_sorting

按字典序推导列名，会让源文件的列序在往返中丢失。受影响的不止 `from_rows`：`select` / `render_table` / `table_to_rows` 都按列序工作，所以一处改动会在三个地方同时暴露。

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

from csvlite.reader import parse_csv
from csvlite.report import render_table, table_to_rows
from csvlite.table import Table



SAMPLE = "name,team,score\nada,red,90\n"


def test_column_order_matches_the_source():
    assert Table.from_rows(parse_csv(SAMPLE)).columns == ("name", "team", "score")


def test_columns_are_not_sorted():
    table = Table.from_rows([{"z": 1, "a": 2}])
    assert table.columns == ("z", "a")


def test_rendering_follows_the_source_order():
    text = render_table(Table.from_rows(parse_csv(SAMPLE)))
    header = text.splitlines()[0]
    assert header.index("name") < header.index("team") < header.index("score")


def test_export_follows_the_source_order():
    rows = table_to_rows(Table.from_rows(parse_csv(SAMPLE)))
    assert list(rows[0]) == ["name", "team", "score"]


def test_selection_still_works_after_the_fix():
    assert Table.from_rows(parse_csv(SAMPLE)).select("score").columns == ("score",)
