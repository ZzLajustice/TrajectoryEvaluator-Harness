"""隐藏验收测试 —— bug_stdev_population

样本标准差除以 n-1（贝塞尔校正），总体标准差除以 n。两者都不报错，只是数字差一点点 —— 所以必须靠 docstring 才能发现。

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

from csvlite.stats import StatsError, stdev



SERIES = [2, 4, 4, 4, 5, 5, 7, 9]


def test_sample_stdev_uses_n_minus_one():
    assert stdev(SERIES) == pytest.approx(2.13809, rel=1e-4)


def test_population_stdev_uses_n():
    assert stdev(SERIES, sample=False) == pytest.approx(2.0)


def test_the_two_conventions_differ():
    assert stdev(SERIES) != pytest.approx(stdev(SERIES, sample=False))


def test_sample_stdev_still_needs_two_values():
    with pytest.raises(StatsError):
        stdev([1])
