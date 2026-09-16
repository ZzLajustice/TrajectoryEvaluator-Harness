"""文本报告渲染 —— 把表与统计量排成给人看的对齐文本。

故意不依赖任何第三方表格库：这个包是被评测的**被测对象**，
依赖越少，出题时越不用操心环境。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from csvlite.reader import Cell
from csvlite.stats import describe
from csvlite.table import Table

#: 缺失值的显示形式。用 `-` 而不是空字符串 ——
#: 空字符串在等宽字体里看不出这一格到底有没有内容。
NULL_DISPLAY = "-"


def format_cell(value: Cell) -> str:
    """单元格 → 文本。浮点数保留 2 位小数。"""
    if value is None:
        return NULL_DISPLAY
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_table(table: Table) -> str:
    """把表排成等宽文本，每列宽度取该列**最长内容**。

    列宽必须包含表头本身的长度：只按数据算宽会让表头比列宽长，
    于是所有列在视觉上错位一格 —— 而输出看起来"差不多对"。
    """
    cells: list[list[str]] = [[format_cell(row[name]) for name in table.columns]
                              for row in table.rows]
    widths = [len(name) for name in table.columns]
    for row in cells:
        for index, text in enumerate(row):
            widths[index] = max(widths[index], len(text))

    lines = [_join(table.columns, widths)]
    lines.append("-+-".join("-" * width for width in widths))
    lines.extend(_join(row, widths) for row in cells)
    return "\n".join(lines)


def render_column_stats(table: Table, column: str) -> str:
    """一列的描述统计，逐行一个指标。"""
    values = table.numbers(column)
    stats = describe(values)
    width = max(len(name) for name in stats)
    return "\n".join(
        f"{name.ljust(width)}  {value:.2f}" for name, value in stats.items())


def render_summary(table: Table, columns: Iterable[str]) -> str:
    """多列的统计摘要，每列一段。

    列序**照调用者给的顺序**，与 `Table.select` 一致 ——
    报告里的列序变了对读的人就是一次无谓的重新适应。
    """
    blocks = []
    for column in columns:
        header = f"[{column}]"
        blocks.append(f"{header}\n{render_column_stats(table, column)}")
    return "\n\n".join(blocks)


def _join(values: Iterable[str], widths: list[int]) -> str:
    return " | ".join(text.ljust(width)
                      for text, width in zip(values, widths, strict=True))


def table_to_rows(table: Table) -> list[Mapping[str, Cell]]:
    r"""导出为字典列表（列序由 `Table.columns` 决定）。

    导出层不做类型转换：`None` 保持 `None`，
    由写文件的人决定 NULL 表示成空串还是 `\\N`。
    """
    return [dict(row) for row in table.rows]
