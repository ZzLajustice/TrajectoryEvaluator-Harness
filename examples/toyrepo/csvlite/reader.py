"""CSV 解析 —— 带引号、转义与类型推断的小实现。

只支持 RFC 4180 里真正被用到的部分：引号包裹、引号内换行、双写引号转义。
不支持的（注释行、多字符分隔符、BOM 之外的编码探测）在 `parse_csv` 的
docstring 里明说，免得用的人猜。
"""

from __future__ import annotations

from pathlib import Path

#: 一个单元格的取值。`None` 表示**空字段**，与 `""` 和 `0` 都不同。
Cell = str | int | float | None


class CsvError(ValueError):
    """CSV 结构错误（引号未闭合）。"""


def parse_line(line: str, delimiter: str = ",") -> list[str]:
    """切分一行，处理引号。

    规则：
      - `"a,b"` 是一个字段，值为 `a,b`
      - `""` 在引号**内**是一个字面量引号（RFC 4180 的双写转义）
      - 引号只在字段开头才有特殊含义（`ab"c` 里的引号是普通字符）

    引号未闭合抛 `CsvError` —— 静默吞掉会产出一条错位的行，
    而错位的行在统计里只会表现成"数字不对"。
    """
    fields: list[str] = []
    current: list[str] = []
    in_quotes = False
    at_field_start = True
    index = 0

    while index < len(line):
        char = line[index]

        if in_quotes:
            if char == '"':
                # 双写引号 = 一个字面量引号
                if index + 1 < len(line) and line[index + 1] == '"':
                    current.append('"')
                    index += 2
                    continue
                in_quotes = False
            else:
                current.append(char)
        elif char == '"' and at_field_start:
            in_quotes = True
        elif char == delimiter:
            fields.append("".join(current))
            current = []
            at_field_start = True
            index += 1
            continue
        else:
            current.append(char)

        at_field_start = False
        index += 1

    if in_quotes:
        raise CsvError(f"unterminated quoted field: {line!r}")

    fields.append("".join(current))
    return fields


def infer_type(raw: str) -> Cell:
    """把原始文本推断成 int / float / str / None。

    空字符串推断为 `None` 而不是 `0` —— 这两者在数据里含义完全不同：
    缺失的测量值不是零。把它们混起来会让所有统计量都偏。
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return raw


def parse_csv(text: str, delimiter: str = ",") -> list[dict[str, Cell]]:
    r"""解析整段 CSV 文本，返回每行的字典。

    首行是表头。字段数与表头不一致的行抛 `CsvError` ——
    宁可报错也不要产出一批长度不齐的记录，那种数据里的 bug
    会以"某个统计量不对"的形式出现，排查成本极高。

    换行统一按 `\\r\\n` 与 `\\n` 两种处理。
    """
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        return []

    header = parse_line(lines[0], delimiter)
    rows: list[dict[str, Cell]] = []
    for lineno, line in enumerate(lines[1:], start=2):
        raw_fields = parse_line(line, delimiter)
        if len(raw_fields) != len(header):
            raise CsvError(
                f"line {lineno}: expected {len(header)} fields, got {len(raw_fields)}")
        rows.append({name: infer_type(value)
                     for name, value in zip(header, raw_fields, strict=True)})
    return rows


def read_csv(path: str | Path, delimiter: str = ",") -> list[dict[str, Cell]]:
    """从文件读。编码固定 utf-8（不做探测，猜错编码比报错更难查）。"""
    return parse_csv(Path(path).read_text(encoding="utf-8"), delimiter)
