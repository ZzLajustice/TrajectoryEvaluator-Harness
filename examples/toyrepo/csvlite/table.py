"""表格操作 —— 选择、改名、过滤、排序、分组。

`Table` 是**不可变**的：每个操作返回新表。可变表在链式调用里
会产生"改了一个副本却以为改了原表"这类难查的 bug，而代价只是几次拷贝。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from csvlite.reader import Cell

#: 表的列名顺序**是语义的一部分**：它决定 `select("*")` 与导出的列序。
Columns = tuple[str, ...]


class TableError(ValueError):
    """列名不存在、列数不匹配等结构性错误。"""


@dataclass(frozen=True, slots=True)
class Table:
    columns: Columns
    rows: tuple[Mapping[str, Cell], ...]

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Cell]],
                  columns: Iterable[str] | None = None) -> Table:
        """从字典序列建表。

        `columns` 省略时按**首次出现顺序**推导，而不是排序 ——
        排序会让 CSV 的列序在往返中丢失。
        """
        materialised = tuple(dict(row) for row in rows)
        if columns is None:
            seen: dict[str, None] = {}
            for row in materialised:
                for name in row:
                    seen.setdefault(name, None)
            derived: Columns = tuple(seen)
        else:
            derived = tuple(columns)
        for row in materialised:
            missing = [name for name in derived if name not in row]
            if missing:
                raise TableError(f"row is missing columns: {missing}")
        return cls(columns=derived, rows=materialised)

    # ---- 变换 ----
    def select(self, *names: str) -> Table:
        """取列。**按调用者给的顺序**返回，不按原表顺序。

        `t.select("b", "a")` 与 `t.select("a", "b")` 结果不同 ——
        列序在报告与导出里可见，所以它是一个被显式指定的结果，不是实现细节。
        """
        unknown = [name for name in names if name not in self.columns]
        if unknown:
            raise TableError(f"unknown columns: {unknown}; have {list(self.columns)}")
        return Table(columns=tuple(names),
                     rows=tuple({name: row[name] for name in names} for row in self.rows))

    def rename(self, mapping: Mapping[str, str]) -> Table:
        """改名。改完出现重名抛错 —— 重名列会让后续所有按名取值的操作静默取到其中一个。"""
        unknown = [name for name in mapping if name not in self.columns]
        if unknown:
            raise TableError(f"cannot rename unknown columns: {unknown}")
        new_columns = tuple(mapping.get(name, name) for name in self.columns)
        if len(set(new_columns)) != len(new_columns):
            raise TableError(f"rename would produce duplicate columns: {new_columns}")
        return Table(
            columns=new_columns,
            rows=tuple({mapping.get(k, k): v for k, v in row.items()}
                       for row in self.rows),
        )

    def where(self, predicate: Callable[[Mapping[str, Cell]], bool]) -> Table:
        return replace(self, rows=tuple(row for row in self.rows if predicate(row)))

    def gt(self, column: str, value: float) -> Table:
        """保留 `column` **严格大于** `value` 的行。

        ★ 严格大于。边界值（恰好等于）不入选 —— 用 `>=` 会让
        "超过阈值"这类口径整体偏移一格，而结果看起来仍然合理。
        """
        self._require(column)
        return self.where(lambda row: _as_number(row[column]) > value)

    def lt(self, column: str, value: float) -> Table:
        """保留 `column` **严格小于** `value` 的行。"""
        self._require(column)
        return self.where(lambda row: _as_number(row[column]) < value)

    def sort_by(self, column: str, *, descending: bool = False) -> Table:
        """按列排序。默认升序；`descending=True` 时降序。

        排序是**稳定**的：同值行的相对顺序保持原样。
        不稳定排序会让同一个输入产出不同的表，而评测里"结果不一致"
        是最难归因的一类问题。
        """
        self._require(column)
        # 缺失值（None）排在最后 —— 它们不是"最小值"，是"没有值"。
        present = [row for row in self.rows if row[column] is not None]
        absent = [row for row in self.rows if row[column] is None]
        ordered = sorted(present, key=lambda row: _sort_key(row[column]),
                         reverse=descending)
        return replace(self, rows=tuple(ordered) + tuple(absent))

    def group_count(self, column: str) -> dict[Cell, int]:
        """按列分组计数。**返回所有组**，包括计数为 1 的。

        结果按 key 排序后返回（dict 保序），这样两次调用的迭代顺序一致。
        """
        self._require(column)
        counts: dict[Cell, int] = {}
        for row in self.rows:
            key = row[column]
            counts[key] = counts.get(key, 0) + 1
        return {key: counts[key] for key in sorted(counts, key=_sort_key)}

    # ---- 辅助 ----
    def column_values(self, column: str) -> list[Cell]:
        """取一列的值。缺失值保留为 `None` —— 由调用者决定怎么处理。"""
        self._require(column)
        return [row[column] for row in self.rows]

    def numbers(self, column: str) -> list[float]:
        """取一列的数值，**丢掉缺失值**。

        统计量只在有观测的地方定义。把 `None` 当 0 会把均值拉低，
        而拉低之后的数字看起来仍然合理。
        """
        out: list[float] = []
        for value in self.column_values(column):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TableError(f"column {column!r} holds non-numeric value {value!r}")
            out.append(float(value))
        return out

    def _require(self, column: str) -> None:
        if column not in self.columns:
            raise TableError(f"unknown column {column!r}; have {list(self.columns)}")

    def __len__(self) -> int:
        return len(self.rows)


def _as_number(value: Cell) -> float:
    """比较用的数值化。`None` 当作负无穷 —— 缺失值不该被"大于阈值"选中。"""
    if value is None:
        return float("-inf")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TableError(f"cannot compare non-numeric value {value!r}")
    return float(value)


def _sort_key(value: Cell) -> tuple[int, float | str]:
    """排序键：`None` 单独一档，数字与字符串各自可比。

    不用元组会踩到 `TypeError: '<' not supported between 'int' and 'str'` ——
    而混合类型的列在真实 CSV 里很常见（一列里既有数字又有 "N/A"）。
    """
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, float(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    return (2, str(value))
