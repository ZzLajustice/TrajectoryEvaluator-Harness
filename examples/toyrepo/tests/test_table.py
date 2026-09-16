"""table 与 report 的可见测试。"""

from __future__ import annotations

import pytest
from csvlite.reader import parse_csv
from csvlite.report import format_cell, render_summary, render_table
from csvlite.table import Table, TableError

SAMPLE = """
name,team,score
ada,red,90
alan,red,72
grace,blue,88
linus,blue,65
"""


def _table() -> Table:
    return Table.from_rows(parse_csv(SAMPLE))


def test_from_rows_keeps_the_column_order_of_the_source():
    # 不是排序 —— 排序会让 CSV 的列序在往返中丢失
    assert _table().columns == ("name", "team", "score")


def test_select_returns_columns_in_the_requested_order():
    # ★ 列序是被显式指定的结果，不是实现细节
    assert _table().select("score", "name").columns == ("score", "name")


def test_select_rejects_unknown_columns():
    with pytest.raises(TableError):
        _table().select("nope")


def test_gt_is_strictly_greater():
    # 边界值 88 不入选
    kept = _table().gt("score", 88)
    assert [row["name"] for row in kept.rows] == ["ada"]


def test_lt_is_strictly_less():
    kept = _table().lt("score", 88)
    assert [row["name"] for row in kept.rows] == ["alan", "linus"]


def test_sort_by_ascending_by_default():
    assert [row["score"] for row in _table().sort_by("score").rows] == [65, 72, 88, 90]


def test_sort_by_descending():
    assert [row["score"]
            for row in _table().sort_by("score", descending=True).rows] == [90, 88, 72, 65]


def test_sort_puts_missing_values_last():
    table = Table.from_rows([{"v": 2}, {"v": None}, {"v": 1}])
    assert [row["v"] for row in table.sort_by("v").rows] == [1, 2, None]


def test_rename_rejects_collisions():
    with pytest.raises(TableError):
        _table().rename({"name": "score"})


def test_group_count_counts_every_group():
    assert _table().group_count("team") == {"blue": 2, "red": 2}


def test_numbers_drops_missing_values():
    table = Table.from_rows([{"v": 1}, {"v": None}, {"v": 3}])
    assert table.numbers("v") == [1.0, 3.0]


def test_numbers_rejects_non_numeric():
    with pytest.raises(TableError):
        _table().numbers("name")


def test_render_table_aligns_on_the_widest_cell_including_the_header():
    text = render_table(Table.from_rows([{"a": "x"}]))
    lines = text.splitlines()
    assert set(lines[0]) == {"a"}          # 表头行
    assert set(lines[1]) == {"-"}          # 分隔行
    assert "x" in lines[2]
    # 列宽取 max(表头, 数据) —— 这里表头 "a" 比数据 "x" 一样宽
    assert len(lines[0]) == len(lines[2])


def test_format_cell_marks_missing_values():
    assert format_cell(None) == "-"
    assert format_cell(1.5) == "1.50"
    assert format_cell(2) == "2"


def test_render_summary_has_one_block_per_column_in_order():
    # 摘要只对数值列有意义，所以这里用一个两列都是数字的表
    table = Table.from_rows([{"a": 1, "b": 10}, {"a": 3, "b": 20}])
    out = render_summary(table, ["b", "a"])
    assert out.index("[b]") < out.index("[a]")
    assert "mean" in out


def test_render_column_stats_refuses_a_non_numeric_column():
    """摘要对文本列无意义 —— 报错比渲染出一堆 NaN 好。"""
    with pytest.raises(TableError):
        render_summary(_table(), ["name"])
