"""csvlite —— 一个小型 CSV 解析与统计工具库。

四个模块，职责单一：

    reader   文本 → 行字典（引号、转义、类型推断）
    table    行字典上的可变操作（选择/改名/过滤/排序/分组）
    stats    描述统计（均值/中位数/标准差/分位数/相关系数）
    report   渲染成对齐文本

设计约定（改动前先读）：
  - **所有操作返回新对象**，不改调用者的数据
  - **缺失值一律是 `None`**，既不是 `0` 也不是 `""`
  - 统计量在 docstring 里写明是哪种口径（样本/总体、插值/最近秩）
"""

from csvlite.reader import Cell, CsvError, infer_type, parse_csv, parse_line, read_csv
from csvlite.report import (
    NULL_DISPLAY,
    format_cell,
    render_column_stats,
    render_summary,
    render_table,
    table_to_rows,
)
from csvlite.stats import (
    StatsError,
    correlation,
    describe,
    mean,
    median,
    percentile,
    stdev,
)
from csvlite.table import Table, TableError

__all__ = [
    "NULL_DISPLAY", "Cell", "CsvError", "StatsError", "Table", "TableError",
    "correlation", "describe", "format_cell", "infer_type", "mean", "median",
    "parse_csv", "parse_line", "percentile", "read_csv", "render_column_stats",
    "render_summary", "render_table", "stdev", "table_to_rows",
]
