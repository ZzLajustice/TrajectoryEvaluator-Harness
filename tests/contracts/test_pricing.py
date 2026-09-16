"""价格表与成本计算测试。

## 价格是**外部事实**，所以这里的断言分两类

1. **算术**：给定价格与 token 数，算出来的钱对不对 —— 这部分可以精确断言
2. **表里的数字**：只断言"存在且结构合理"，**不断言具体数值** ——
   断言 4.0 元只会让厂商调价时多一处要改的地方，
   而它并不能证明那个数字是对的（我就是从官网抄的）

价格来源与抓取日期记在 `contracts/pricing.py` 的顶部注释里。

## 一处容易写错的测试样本

高峰窗口是**北京时间** 9:00-12:00 与 14:00-18:00。
用 UTC 造样本时很容易把 06:00 UTC（= 14:00 北京）当成空闲，
而它正好在高峰窗口的**左闭**边界上。下面的样本都显式标注了北京时刻。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from harness.contracts.pricing import (
    DEFAULT_USD_PER_CNY,
    PRICES,
    ModelPrice,
    cost_of,
    is_peak,
    price_for,
    usd_per_cny,
    with_cost,
)
from harness.contracts.results import Usage

# 2026-09-16 是周三
_WED = 2026, 9, 16


def _utc(hour: int, minute: int = 0) -> datetime:
    """北京时刻 → UTC datetime（北京 = UTC+8，无夏令时）。"""
    return datetime(*_WED, (hour - 8) % 24, minute, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("HARNESS_USD_PER_CNY",):
        monkeypatch.delenv(name, raising=False)
    for key in list(PRICES):
        monkeypatch.delenv(f"HARNESS_PRICE_{key.upper().replace('-', '_')}",
                           raising=False)


# ---- 峰谷判定 ----
@pytest.mark.parametrize("beijing_hour,expected", [
    (8, False),    # 上班前
    (9, True),     # 上午高峰左闭
    (11, True),
    (12, False),   # 午休
    (13, False),
    (14, True),    # 下午高峰左闭 —— 这条最容易写错
    (17, True),
    (18, False),   # 下班
    (22, False),
])
def test_peak_windows_in_beijing_time(beijing_hour, expected):
    assert is_peak(_utc(beijing_hour)) is expected


@pytest.mark.parametrize("day", [19, 20])  # 周六、周日
def test_weekends_are_never_peak(day):
    for hour in range(24):
        moment = datetime(2026, 9, day, (hour - 8) % 24, tzinfo=timezone.utc)
        assert is_peak(moment) is False


def test_beijing_offset_has_no_dst():
    """北京 1991 年后没有夏令时，所以固定 +8 就够 —— 也因此不需要 tzdata。

    这条测试防的是"顺手改成 ZoneInfo"，那会在 Windows 上引入 tzdata 依赖。
    """
    jan = _utc(12)
    jul = datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)  # 北京 12:00
    assert is_peak(jan) is is_peak(jul)  # 冬夏同一时刻，判定一致


# ---- 算术 ----
def test_cost_uses_the_published_formula():
    """Cost = (cache_hit×hit价 + miss×in价 + out×out价) / 1e6 × 倍率 × 汇率"""
    price = price_for("deepseek-flash")
    assert price is not None
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    expected_cny = price.input_per_1m + price.output_per_1m
    assert cost_of(usage, "deepseek-flash", when=_utc(13)) == pytest.approx(
        expected_cny * DEFAULT_USD_PER_CNY, rel=1e-6)


def test_peak_costs_exactly_twice_off_peak():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    off = cost_of(usage, "deepseek-flash", when=_utc(13))
    peak = cost_of(usage, "deepseek-flash", when=_utc(10))
    assert peak == pytest.approx(off * 2, rel=1e-6)


def test_cache_hits_are_far_cheaper_than_misses():
    """命中与未命中价差 50 倍（0.02 vs 1 元）。

    不单列这一项的话，长对话的成本会被严重高估 —— 而长对话恰恰是
    代价最高的那类用例，高估它会让成本门禁误报。
    """
    all_miss = Usage(input_tokens=1_000_000)
    all_hit = Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert cost_of(all_hit, "deepseek-flash", when=_utc(13)) < \
        cost_of(all_miss, "deepseek-flash", when=_utc(13)) / 10


def test_cache_hits_are_a_subset_of_input_not_extra():
    """命中数是 input 的**子集** —— 超出来也不能算成负数或双倍。"""
    usage = Usage(input_tokens=100, cache_read_tokens=999_999)
    assert cost_of(usage, "deepseek-flash", when=_utc(13)) >= 0.0


def test_zero_usage_costs_nothing():
    assert cost_of(Usage(), "deepseek-flash", when=_utc(13)) == 0.0


# ---- 未知模型与 fake ----
def test_fake_provider_costs_nothing_and_does_not_warn():
    """假 provider 本来就没花钱 —— 警告它是误报，而误报会被学会忽略。"""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert cost_of(Usage(input_tokens=100), "fake") == 0.0


def test_unknown_model_warns_and_records_zero():
    """★ 查不到价格必须**吵** —— 静默记 0 正是成本门禁失效那个洞的成因。"""
    with pytest.warns(RuntimeWarning, match="no price known"):
        assert cost_of(Usage(input_tokens=100), "gpt-4o") == 0.0


def test_the_warning_names_the_override_variable():
    """报错要告诉人**怎么修**，而不是只说"不知道价格"。"""
    with pytest.warns(RuntimeWarning) as record:
        cost_of(Usage(input_tokens=1), "some-new-model")
    assert "HARNESS_PRICE_SOME_NEW_MODEL" in str(record[0].message)


# ---- 覆盖机制 ----
def test_price_can_be_overridden_by_env(monkeypatch):
    """厂商调价时不必改代码。"""
    monkeypatch.setenv("HARNESS_PRICE_DEEPSEEK_FLASH", "10,20")
    price = price_for("deepseek-flash")
    assert price is not None
    assert price.input_per_1m == 10.0
    assert price.output_per_1m == 20.0


def test_override_can_include_the_cache_hit_price(monkeypatch):
    monkeypatch.setenv("HARNESS_PRICE_DEEPSEEK_FLASH", "10,20,1")
    price = price_for("deepseek-flash")
    assert price is not None
    assert price.cache_hit_input_per_1m == 1.0


def test_malformed_override_warns_and_falls_back(monkeypatch):
    """覆盖值写坏了要用表里的价并警告，**不能**让成本变成 0。"""
    monkeypatch.setenv("HARNESS_PRICE_DEEPSEEK_FLASH", "not,a,number")
    with pytest.warns(RuntimeWarning, match="malformed"):
        price = price_for("deepseek-flash")
    assert price is not None
    assert price.input_per_1m == PRICES["deepseek-flash"].input_per_1m


def test_exchange_rate_is_overridable(monkeypatch):
    monkeypatch.setenv("HARNESS_USD_PER_CNY", "0.5")
    assert usd_per_cny() == 0.5


def test_malformed_exchange_rate_warns_and_falls_back(monkeypatch):
    monkeypatch.setenv("HARNESS_USD_PER_CNY", "abc")
    with pytest.warns(RuntimeWarning, match="HARNESS_USD_PER_CNY"):
        assert usd_per_cny() == DEFAULT_USD_PER_CNY


# ---- 表本身 ----
def test_the_table_has_the_models_we_actually_use():
    for model in ("deepseek-flash", "deepseek-v4-pro"):
        assert model in PRICES, f"{model} 不在价格表里"


def test_legacy_model_names_share_the_current_price():
    """官方页面明说旧名仍可调用但按 Flash 价计费 —— 表里要一致。"""
    assert PRICES["deepseek-v4-flash"] == PRICES["deepseek-flash"]


def test_every_price_entry_is_positive_and_ordered_sensibly():
    """结构合理性：输出比输入贵、缓存命中比未命中便宜。

    只断言**关系**不断言数值 —— 数值会随厂商调价变，关系不会。
    """
    for model, price in PRICES.items():
        assert price.input_per_1m > 0, model
        assert price.output_per_1m > 0, model
        assert price.output_per_1m > price.input_per_1m, model
        assert 0 <= price.cache_hit_input_per_1m < price.input_per_1m, model


def test_price_for_returns_none_for_unknown():
    assert price_for("no-such-model") is None


# ---- with_cost ----
def test_with_cost_fills_in_the_cost():
    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    filled = with_cost(usage, "deepseek-flash", when=_utc(13))
    assert filled.cost_usd > 0
    assert filled.input_tokens == usage.input_tokens  # token 数没被动


def test_with_cost_does_not_overwrite_an_existing_cost():
    """Replay 的用量来自 cassette，成本已记在案 —— 不该被重算覆盖。"""
    usage = Usage(input_tokens=1_000_000, cost_usd=0.123)
    assert with_cost(usage, "deepseek-flash").cost_usd == 0.123


def test_with_cost_returns_a_new_object():
    """`Usage` 是不可变的 pydantic 模型，不能原地改。"""
    usage = Usage(input_tokens=1_000_000)
    filled = with_cost(usage, "deepseek-flash", when=_utc(13))
    assert filled is not usage
    assert usage.cost_usd == 0.0


def test_model_price_is_a_value_object():
    assert ModelPrice(1.0, 2.0) == ModelPrice(1.0, 2.0)

