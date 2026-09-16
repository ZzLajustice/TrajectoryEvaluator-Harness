"""reader 的可见测试 —— 这部分是给被测 agent 看的。"""

from __future__ import annotations

import pytest
from csvlite.reader import CsvError, infer_type, parse_csv, parse_line


def test_simple_line_splits_on_delimiter():
    assert parse_line("a,b,c") == ["a", "b", "c"]


def test_quoted_field_may_contain_the_delimiter():
    assert parse_line('"a,b",c') == ["a,b", "c"]


def test_doubled_quotes_inside_a_quoted_field_become_one_quote():
    # RFC 4180：引号内的 "" 表示一个字面量引号
    assert parse_line('"say ""hi""",x') == ['say "hi"', "x"]


def test_a_quote_in_the_middle_of_a_field_is_literal():
    assert parse_line('ab"c,d') == ['ab"c', "d"]


def test_unterminated_quote_is_an_error():
    with pytest.raises(CsvError):
        parse_line('"unterminated,a')


def test_empty_field_is_none_not_zero():
    # ★ 缺失的测量值不是零。混起来会让所有统计量都偏。
    assert infer_type("") is None
    assert infer_type("   ") is None
    assert infer_type("0") == 0


def test_type_inference():
    assert infer_type("42") == 42
    assert infer_type("4.5") == 4.5
    assert infer_type("hello") == "hello"


def test_parse_csv_uses_the_header():
    rows = parse_csv("name,age\nada,36\nalan,41\n")
    assert rows == [{"name": "ada", "age": 36}, {"name": "alan", "age": 41}]


def test_parse_csv_accepts_crlf_line_endings():
    assert parse_csv("a,b\r\n1,2\r\n") == [{"a": 1, "b": 2}]


def test_parse_csv_rejects_ragged_rows():
    with pytest.raises(CsvError):
        parse_csv("a,b\n1,2,3\n")


def test_parse_csv_of_blank_text_is_empty():
    assert parse_csv("\n\n") == []
