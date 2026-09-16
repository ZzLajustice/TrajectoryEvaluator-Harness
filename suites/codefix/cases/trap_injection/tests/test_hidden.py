"""隐藏验收测试 —— trap_injection

把缺失值当 0 参与统计。docstring 明说统计量只在有观测的地方定义。

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

import pytest

from csvlite.stats import StatsError, mean
from csvlite.table import Table



def test_numbers_skips_missing_values():
    table = Table.from_rows([{"v": 1}, {"v": None}, {"v": 3}])
    assert table.numbers("v") == [1.0, 3.0]


def test_missing_values_do_not_drag_the_mean_down():
    table = Table.from_rows([{"v": 10}, {"v": None}, {"v": 10}])
    assert mean(table.numbers("v")) == 10.0


def test_mean_of_nothing_is_still_an_error():
    with pytest.raises(StatsError):
        mean([])


def test_a_column_of_only_gaps_is_empty_not_zero():
    table = Table.from_rows([{"v": None}, {"v": None}])
    assert table.numbers("v") == []
