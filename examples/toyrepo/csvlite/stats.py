"""描述统计 —— 均值、中位数、标准差、分位数、相关系数。

## 每个函数都要说清"是哪一个"

统计量的名字几乎都有两种约定，混用不会报错、只会让数字悄悄偏掉：

  - 标准差：**样本**（除以 n-1）还是**总体**（除以 n）
  - 分位数：线性插值还是最近秩

所以这里的每个函数都在 docstring 里写明用的是哪一种，并且
`stdev` 用**关键字参数** `sample` 强制调用者表态 ——
位置参数会让 `stdev(xs, False)` 读起来完全不知道 False 指什么。
"""

from __future__ import annotations

import math
from collections.abc import Sequence


class StatsError(ValueError):
    """样本不足或输入不合法。"""


def _require_nonempty(values: Sequence[float], what: str) -> None:
    if not values:
        raise StatsError(f"{what} needs at least one value")


def mean(values: Sequence[float]) -> float:
    """算术平均：`sum / n`。

    空序列抛错而非返回 0 —— 返回 0 会让"没有数据"看起来像"平均值为 0"。
    """
    _require_nonempty(values, "mean")
    return sum(values) / len(values)


def median(values: Sequence[float]) -> float:
    """中位数。

    偶数个观测时取**中间两个的算术平均** —— 取其中一个（下中位数或上中位数）
    也是合法定义，但那样结果会随排序方向偏，且与主流库不一致。
    """
    _require_nonempty(values, "median")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def stdev(values: Sequence[float], *, sample: bool = True) -> float:
    """标准差。

    `sample=True`（默认）用**样本**口径，除以 `n-1`（贝塞尔校正）；
    `sample=False` 用总体口径，除以 `n`。

    默认样本口径是因为数据通常是"从更大的总体里抽出来的一批"。
    只有拿到全体数据时才该用总体口径 —— 而那时差别本来也不大。

    样本口径下 n 必须 ≥ 2：n=1 时 `n-1=0`，除以 0 会得到
    `ZeroDivisionError` 或 inf，都不是"标准差为 0"。
    """
    _require_nonempty(values, "stdev")
    if sample and len(values) < 2:
        raise StatsError("sample stdev needs at least two values")
    average = mean(values)
    squared = sum((value - average) ** 2 for value in values)
    divisor = len(values) - 1 if sample else len(values)
    return math.sqrt(squared / divisor)


def percentile(values: Sequence[float], p: float) -> float:
    """分位数，`p` 取 0~100，使用**线性插值**（与 numpy 默认口径一致）。

    插值位置是 `(n-1) * p/100`：这个 `n-1` 是"下标"而非"个数"，
    写错成 `n * p/100` 会让 p=100 越界、p=0 也偏一格。
    """
    if not 0 <= p <= 100:
        raise StatsError(f"percentile must be in [0, 100], got {p}")
    _require_nonempty(values, "percentile")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    """皮尔逊相关系数，取值 [-1, 1]。

    **必须归一化**：协方差除以两个标准差的乘积。只算协方差的话结果会
    随数据量级变化（把 x 乘 10，相关系数就乘 10），而它看起来仍像个系数。
    """
    if len(xs) != len(ys):
        raise StatsError(f"length mismatch: {len(xs)} vs {len(ys)}")
    if len(xs) < 2:
        raise StatsError("correlation needs at least two points")
    mean_x, mean_y = mean(xs), mean(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    spread_x = sum((x - mean_x) ** 2 for x in xs)
    spread_y = sum((y - mean_y) ** 2 for y in ys)
    if spread_x == 0 or spread_y == 0:
        # 任一维完全没有变化时相关系数没有定义。返回 0.0 而不是抛错，
        # 因为"这一列是常数"在真实数据里很常见，调用方通常只想跳过它。
        return 0.0
    return covariance / math.sqrt(spread_x * spread_y)


def describe(values: Sequence[float], *, sample: bool = True) -> dict[str, float]:
    """一次性算出常用描述量。

    键名固定，便于报告层直接取用。`count` 是**观测个数**，
    它必须单独列出来 —— 少了它，两组均值相同的样本
    在报告里会看起来一样。
    """
    _require_nonempty(values, "describe")
    return {
        "count": float(len(values)),
        "mean": mean(values),
        "median": median(values),
        "stdev": stdev(values, sample=sample) if len(values) >= 2 else 0.0,
        "min": min(values),
        "max": max(values),
        "p25": percentile(values, 25),
        "p75": percentile(values, 75),
    }
