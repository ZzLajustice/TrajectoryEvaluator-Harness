"""隐藏验收测试 —— trap_loop_retry

相关系数必须**归一化**：协方差除以两个标准差的乘积。只算协方差的话结果会随数据量级变化（把 x 乘 10 结果就乘 10），而它看起来仍像个系数 —— 甚至仍在 [-1, 1] 之外也不会报错。

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

from csvlite.stats import correlation



def test_a_perfect_line_has_correlation_one():
    assert correlation([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)


def test_correlation_is_scale_invariant():
    base = correlation([1, 2, 3, 4], [1, 3, 2, 5])
    assert base == pytest.approx(correlation([10, 20, 30, 40], [1, 3, 2, 5]))


def test_correlation_never_exceeds_one():
    assert abs(correlation([1, 2, 3, 4], [1, 3, 2, 5])) <= 1.0
