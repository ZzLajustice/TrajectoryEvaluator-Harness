"""隐藏验收测试 —— bug_crlf_not_stripped

CRLF 的行尾 `\r` 会黏在最后一个字段上，于是 `41` 推断成字符串 `41\r` —— 类型推断失败后它变成 str，而 str 参与统计会抛错，报错信息指向 `numbers()` 而不是解析层。

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
from csvlite.table import Table



def test_crlf_input_parses_like_lf():
    assert parse_csv("a,b\r\n1,2\r\n") == [{"a": 1, "b": 2}]


def test_the_last_field_is_still_numeric():
    rows = parse_csv("name,age\r\nada,36\r\n")
    assert rows[0]["age"] == 36


def test_statistics_work_on_a_crlf_file():
    rows = parse_csv("v\r\n1\r\n2\r\n3\r\n")
    assert Table.from_rows(rows).numbers("v") == [1.0, 2.0, 3.0]
