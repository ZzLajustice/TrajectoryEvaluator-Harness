"""厂商价格表与成本计算。

## 为什么这个模块住在 `contracts/`（L0）

它是一个**纯函数 + 常量表**，没有 I/O。而计费发生在 `core/loop.py` 的
`charge_usage` 那一处 —— core 只被允许 import `events` / `contracts`。
所以价格表必须住在 L0，否则要么 core 越界，要么成本永远算不出来。

## 价格来源（**必须注明抓取日期**）

DeepSeek 官方定价页 <https://api-docs.deepseek.com/zh-cn/quick_start/pricing>
**2026-09-16 抓取**。页面自己写着"产品价格可能发生变动"，
所以：

  - 表里的数字带日期，改的时候一并更新注释
  - 用 `HARNESS_PRICE_<MODEL>` 环境变量可以覆盖单个模型的价格
  - 完全查不到价格的模型 → 成本记 0 并**发警告**（见 `cost_of`）

## 两处容易搞错的地方

1. **模型名会变。** 抓取时页面明说：现行名是 `deepseek-flash`，
   `deepseek-v4-flash` 是**旧名**（仍可调用，但模型已下线，
   由 V4.1-Flash 按 Flash 价提供服务）。两个名字都列在表里。

2. **峰谷价差一倍。** 高峰 = 空闲 × 2，高峰时段是
   **北京时间周一至周五 9:00-12:00、14:00-18:00**，其余为空闲。
   用 `ZoneInfo("Asia/Shanghai")` 在 Windows 上要装 `tzdata`，
   而北京**没有夏令时**，所以直接用固定 +8 偏移 —— 少一个依赖，且完全正确。

## 币种

厂商按**人民币**计价，而 `Usage.cost_usd` 的字段名是美元。
这里按可配置汇率折算（`HARNESS_USD_PER_CNY`），默认值见 `DEFAULT_USD_PER_CNY`。
**这是一个近似** —— 汇率会动，用途是让成本门禁有个量级正确的数字，
不是财务对账。见 docs/known-gaps.md。
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from harness.contracts.results import Usage

# 抓取日期。更新价格表时一并更新它 —— 没有日期的价格表无法判断是否过期。
PRICES_FETCHED_ON = "2026-09-16"

# 北京没有夏令时（1991 年后），固定偏移就够，省掉 tzdata 依赖
_BEIJING = timezone(timedelta(hours=8))

# 高峰时段（北京时间，周一到周五）
_PEAK_WINDOWS = ((9, 12), (14, 18))
_PEAK_MULTIPLIER = 2.0

# CNY → USD 的近似汇率。**会过时**，用环境变量覆盖。
DEFAULT_USD_PER_CNY = 0.141  # ≈ 1 USD / 7.1 CNY

_OVERRIDE_PREFIX = "HARNESS_PRICE_"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """每百万 tokens 的价格，**空闲时段**（高峰 = 空闲 × `_PEAK_MULTIPLIER`）。"""

    input_per_1m: float
    output_per_1m: float
    # 缓存命中的输入价。DeepSeek 这一项比未命中便宜 50 倍，
    # 不单列的话长对话的成本会被严重高估。
    cache_hit_input_per_1m: float = 0.0


# 单位：人民币元 / 百万 tokens。**空闲时段价**。
DEEPSEEK_PRICES: dict[str, ModelPrice] = {
    "deepseek-flash": ModelPrice(input_per_1m=1.0, output_per_1m=4.0,
                                 cache_hit_input_per_1m=0.02),
    # 旧名，同价（页面明说按 Flash 价格计费）
    "deepseek-v4-flash": ModelPrice(input_per_1m=1.0, output_per_1m=4.0,
                                    cache_hit_input_per_1m=0.02),
    "deepseek-v4-pro": ModelPrice(input_per_1m=4.5, output_per_1m=13.5,
                                  cache_hit_input_per_1m=0.15),
}

PRICES: dict[str, ModelPrice] = dict(DEEPSEEK_PRICES)


def is_peak(when: datetime | None = None) -> bool:
    """当前（或给定时刻）是否处于高峰时段。

    北京时间周一至周五 9:00-12:00、14:00-18:00。
    """
    moment = (when or datetime.now(timezone.utc)).astimezone(_BEIJING)
    if moment.weekday() >= 5:  # 周六周日全天空闲
        return False
    return any(start <= moment.hour < end for start, end in _PEAK_WINDOWS)


def price_for(model: str) -> ModelPrice | None:
    """查模型价格。`HARNESS_PRICE_<MODEL>` 可覆盖单个模型。

    覆盖值的格式是 `输入价,输出价[,缓存命中价]`（元/百万 tokens），
    例如 `HARNESS_PRICE_DEEPSEEK_FLASH=1.2,4.8`。厂商调价时不必改代码。
    """
    override = os.environ.get(
        f"{_OVERRIDE_PREFIX}{model.upper().replace('-', '_').replace('.', '_')}")
    if override:
        try:
            parts = [float(x) for x in override.split(",")]
        except ValueError:
            warnings.warn(f"ignoring malformed price override {override!r} for {model}",
                          RuntimeWarning, stacklevel=2)
        else:
            if len(parts) >= 2:
                return ModelPrice(input_per_1m=parts[0], output_per_1m=parts[1],
                                  cache_hit_input_per_1m=parts[2] if len(parts) > 2 else 0.0)
    return PRICES.get(model)


def usd_per_cny() -> float:
    raw = os.environ.get("HARNESS_USD_PER_CNY")
    if raw:
        try:
            return float(raw)
        except ValueError:
            warnings.warn(f"ignoring malformed HARNESS_USD_PER_CNY={raw!r}",
                          RuntimeWarning, stacklevel=2)
    return DEFAULT_USD_PER_CNY


def cost_of(usage: Usage, model: str, *, when: datetime | None = None) -> float:
    """算出这次用量的成本（美元）。查不到价格时返回 0 **并警告**。

    ## 查不到就警告，不静默记 0

    静默记 0 正是"成本门禁失效"那个洞的成因：`cost_usd` 恒为 0，
    而 `--max-cost` / `Budget.max_usd` 看起来像在保护你。
    所以这里区分两种情况：

      - **假 provider**（`fake`）：本来就没花钱，返回 0 且**不警告**
      - **真 provider 但查不到价格**：返回 0 且**警告**

    调用方不必自己判分支。
    """
    if model in ("fake", "") or model.startswith("fake"):
        return 0.0

    price = price_for(model)
    if price is None:
        warnings.warn(
            f"no price known for model {model!r} (prices fetched {PRICES_FETCHED_ON}); "
            f"cost recorded as 0 and any cost cap will not work. "
            f"Set HARNESS_PRICE_{model.upper().replace('-', '_')}='in,out[,cache_hit]' "
            f"(CNY per 1M tokens) or add it to contracts/pricing.py.",
            RuntimeWarning, stacklevel=2,
        )
        return 0.0

    multiplier = _PEAK_MULTIPLIER if is_peak(when) else 1.0
    # 缓存命中的那部分是 input_tokens 的**子集**，不是额外的
    cache_hit = min(usage.cache_read_tokens, usage.input_tokens)
    cache_miss = usage.input_tokens - cache_hit

    cny = (
        cache_hit * price.cache_hit_input_per_1m
        + cache_miss * price.input_per_1m
        + usage.output_tokens * price.output_per_1m
    ) / 1_000_000 * multiplier

    return round(cny * usd_per_cny(), 8)


def with_cost(usage: Usage, model: str, *, when: datetime | None = None) -> Usage:
    """返回一个补上 `cost_usd` 的 `Usage`。

    `Usage` 是不可变的 pydantic 模型，所以是复制而非原地改。
    """
    if usage.cost_usd:
        return usage  # 已经算过就别覆盖（replay 的用量来自 cassette）
    return usage.model_copy(update={"cost_usd": cost_of(usage, model, when=when)})
