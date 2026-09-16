"""stats 的可见测试。"""

from __future__ import annotations

import pytest
from csvlite.stats import (
    StatsError,
    correlation,
    describe,
    mean,
    median,
    percentile,
    stdev,
)


def test_mean_is_the_arithmetic_average():
    assert mean([1, 2, 3, 4]) == 2.5


def test_mean_of_nothing_is_an_error():
    # 返回 0 会让"没有数据"看起来像"平均值是 0"
    with pytest.raises(StatsError):
        mean([])


def test_median_of_an_odd_count_is_the_middle_one():
    assert median([3, 1, 2]) == 2


def test_median_of_an_even_count_averages_the_two_middle():
    assert median([1, 2, 3, 4]) == 2.5


def test_sample_stdev_divides_by_n_minus_one():
    # [2,4,4,4,5,5,7,9] 的样本标准差是 2.138…（总体标准差是 2.0）
    assert stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.13809, rel=1e-4)


def test_population_stdev_is_available_explicitly():
    assert stdev([2, 4, 4, 4, 5, 5, 7, 9], sample=False) == pytest.approx(2.0)


def test_sample_stdev_needs_two_values():
    with pytest.raises(StatsError):
        stdev([1])


def test_percentile_interpolates_linearly():
    # n=5，p=50 → 位置 (5-1)*0.5 = 2 → 恰好第 3 个
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    # p=25 → 位置 1.0 → 第 2 个
    assert percentile([1, 2, 3, 4, 5], 25) == 2
    # p=10 → 位置 0.4 → 在 1 和 2 之间插值
    assert percentile([1, 2, 3, 4, 5], 10) == pytest.approx(1.4)


def test_percentile_endpoints_are_the_extremes():
    assert percentile([3, 1, 2], 0) == 1
    assert percentile([3, 1, 2], 100) == 3


def test_percentile_rejects_out_of_range_p():
    with pytest.raises(StatsError):
        percentile([1, 2], 101)


def test_correlation_of_a_perfect_line_is_one():
    assert correlation([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)


def test_correlation_of_an_inverse_line_is_minus_one():
    assert correlation([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)


def test_correlation_does_not_depend_on_scale():
    # ★ 只算协方差的话，把 x 乘 10 结果就乘 10 —— 而它看起来仍像个系数
    base = correlation([1, 2, 3, 4], [1, 3, 2, 5])
    scaled = correlation([10, 20, 30, 40], [1, 3, 2, 5])
    assert base == pytest.approx(scaled)


def test_correlation_of_a_constant_column_is_zero():
    assert correlation([1, 1, 1], [1, 2, 3]) == 0.0


def test_correlation_needs_equal_lengths():
    with pytest.raises(StatsError):
        correlation([1, 2], [1, 2, 3])


def test_describe_reports_the_count():
    stats = describe([1, 2, 3, 4])
    assert stats["count"] == 4
    assert stats["mean"] == 2.5
    assert stats["median"] == 2.5
    assert stats["min"] == 1
    assert stats["max"] == 4
