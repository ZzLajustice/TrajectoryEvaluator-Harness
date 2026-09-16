"""隐藏验收测试 —— trap_context_pressure

`describe` 少了 `count`。看起来只是少一个键，但 `count` 是**唯一能区分「两组均值相同的样本」的信息** —— 报告里缺了它，5 个观测和 5000 个观测看起来一样可信。

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

from csvlite.stats import describe



def test_describe_reports_the_count():
    stats = describe([1, 2, 3, 4])
    assert stats["count"] == 4


def test_count_is_a_float_like_the_other_metrics():
    assert isinstance(describe([1, 2])["count"], float)


def test_the_other_metrics_are_still_there():
    stats = describe([1, 2, 3, 4])
    assert stats["mean"] == pytest.approx(2.5)
    assert stats["median"] == pytest.approx(2.5)


def test_describe_with_a_missing_heavy_column():
    assert describe([1.0, 2.0, 3.0])["count"] == 3
