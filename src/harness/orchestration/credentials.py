"""真模型的凭据与端点解析。

## 三条原则

1. **key 只从环境（或 `.env`）来，绝不从 suite 文件来。**
   suite 是入库的，把 key 写进去等于把它提交上去 —— 而且不会有任何症状，
   直到某天有人搜到了它。所以 `ModelRef` 里刻意**没有** api_key 字段。

2. **缺失要报"配置错误"，不能让 SDK 抛原始异常。**
   `AsyncOpenAI(api_key=None)` 抛的是 `OpenAIError`，消息里可能带上半个
   key，而且退出码会是 1（"门禁未达标"）而不是 2（"配置错误"）——
   让该去改配置的人去查门禁，是最浪费时间的一种误导。

3. **`.env` 与环境变量都支持，环境变量优先。**
   `.env` 方便本地；环境变量是 CI 唯一可行的方式。
   优先级由 `pydantic-settings` 天然保证（环境变量覆盖 env_file）。

## 解析阶梯（先命中先赢）

    api_key：  <VENDOR>_API_KEY  →  HARNESS_JUDGE_API_KEY（仅 judge）
                                 →  HARNESS_API_KEY
    base_url： ModelRef.extra["base_url"]  →  <VENDOR>_BASE_URL
                                          →  内置厂商表
                                          →  报错

`<VENDOR>` 是 `ModelRef.provider` 的大写形式，例如 `DEEPSEEK_API_KEY`。

## `.env` 的位置

`pydantic-settings` 从**当前工作目录**读 `.env`。所以命令要在仓库根跑
（本项目所有命令本来就都从仓库根跑）。换目录跑的话要自己导出环境变量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from harness.contracts.spec import ModelRef

# 走 FakeProvider 的 provider 名 —— 除它们之外都按真厂商处理
FAKE_PROVIDERS = frozenset({"fake", "replay"})

# 已知厂商的 OpenAI 兼容端点。
#
# ⚠️ 这些是**写死的表**，会过时。厂商换域名、加区域端点、
# 或者你用的是自建网关，都要用 `ModelRef.extra.base_url` 或
# `<VENDOR>_BASE_URL` 覆盖。
KNOWN_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "openai": "https://api.openai.com/v1",
}


class ProviderConfigError(ValueError):
    """真模型的配置不完整 —— 映射到 CLI 退出码 2（配置错误）。"""


class HarnessSettings(BaseSettings):
    """`.env` / 环境变量里的通用配置。

    `extra="ignore"`：`.env` 里常有别的工具写的键，不该因此报错。

    ## 为什么用显式 alias 而不是 `env_prefix`

    直觉写法是 `env_prefix="HARNESS_"`，但**那个前缀会同时作用在 `.env` 的键上**，
    于是 `DEEPSEEK_API_KEY` 这类厂商专属变量永远查不到 —— 而它恰恰是
    最自然的写法。实测踩过：`.env` 里写了 `DEEPSEEK_API_KEY`，
    读出来是 None，报"没配 key"。

    改成逐字段写全名，前缀没有任何魔法。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 有了 validation_alias 之后，**按字段名构造会被忽略**（默认只认别名）——
        # 于是 `HarnessSettings(base_url=...)` 静默变成一个空设置。
        # 显式打开按名传参，Python 侧用字段名、环境侧用全名别名，两边都成立。
        validate_by_name=True,
        validate_by_alias=True,
    )

    # SUT 的 key。厂商专属变量（DEEPSEEK_API_KEY）优先于它。
    api_key: str | None = Field(default=None, validation_alias="HARNESS_API_KEY")
    # judge 的 key。设计上 SUT 用便宜模型、judge 用强模型，
    # 两者常常是**不同厂商**的 key，所以要能分别指定。
    judge_api_key: str | None = Field(
        default=None, validation_alias="HARNESS_JUDGE_API_KEY")
    base_url: str | None = Field(default=None, validation_alias="HARNESS_BASE_URL")

    def lookup(self, name: str) -> str | None:
        """查一个任意名字的变量：**环境变量优先于 `.env`**。

        厂商专属变量走这里（`DEEPSEEK_API_KEY` 之类），
        因为它们不在上面那张固定字段表里。

        pydantic-settings 只把**它自己认识的**字段从 `.env` 加载进来，
        所以这里要另读一次 `.env` —— 否则"key 写在 .env 里"这件事
        只对 `HARNESS_*` 生效，对厂商专属变量不生效，而后者才是主流写法。
        """
        if (value := os.environ.get(name)) is not None:
            return value
        return _dotenv_values().get(name)


@lru_cache(maxsize=1)
def _dotenv_values_cached(path: str, mtime: float) -> dict[str, str]:
    # 以 mtime 做键的一部分：改了 .env 不必重启进程（测试里会改）
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _dotenv_values() -> dict[str, str]:
    """读当前工作目录的 `.env`。不做缓存失效的聪明事，只按 mtime 区分。"""
    path = Path(".env")
    if not path.exists():
        return {}
    try:
        return _dotenv_values_cached(str(path.resolve()), path.stat().st_mtime)
    except OSError:
        return {}


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    provider: str
    base_url: str
    api_key: str


def is_fake(provider: str) -> bool:
    return provider in FAKE_PROVIDERS


def resolve_endpoint(
    model: ModelRef, *, role: str = "sut", settings: HarnessSettings | None = None
) -> ResolvedEndpoint:
    """按阶梯解析出 (base_url, api_key)。缺东西就抛 `ProviderConfigError`。"""
    settings = settings or HarnessSettings()
    provider = model.provider
    vendor = provider.upper().replace("-", "_")

    api_key = _resolve_api_key(vendor, role, settings)
    if not api_key:
        raise ProviderConfigError(
            f"no API key for provider {provider!r} (role={role}). Tried "
            f"{vendor}_API_KEY, "
            + ("HARNESS_JUDGE_API_KEY, " if role == "judge" else "")
            + "HARNESS_API_KEY. Put one in .env or export it — "
            "keys are deliberately not read from suite files, which get committed."
        )

    base_url = _resolve_base_url(provider, vendor, model, settings)
    return ResolvedEndpoint(provider=provider, base_url=base_url, api_key=api_key)


def _resolve_api_key(vendor: str, role: str, settings: HarnessSettings) -> str | None:
    """厂商专属变量优先 —— 多厂商共存时它是唯一不打架的写法。"""
    if direct := settings.lookup(f"{vendor}_API_KEY"):
        return direct
    if role == "judge" and settings.judge_api_key:
        return settings.judge_api_key
    return settings.api_key


def _resolve_base_url(
    provider: str, vendor: str, model: ModelRef, settings: HarnessSettings
) -> str:
    # ① suite 里显式写的（自建网关、区域端点走这条）
    explicit = model.extra.get("base_url")
    if isinstance(explicit, str) and explicit:
        return explicit

    # ② 厂商专属变量（环境变量或 .env）
    if env := settings.lookup(f"{vendor}_BASE_URL"):
        return env

    # ③ 通用环境变量
    if settings.base_url:
        return settings.base_url

    # ④ 内置表
    if known := KNOWN_BASE_URLS.get(provider.lower()):
        return known

    raise ProviderConfigError(
        f"unknown provider {provider!r} and no base_url given. "
        f"Known vendors: {sorted(KNOWN_BASE_URLS)}. "
        f'Set it in the suite as model.extra.base_url, or export {vendor}_BASE_URL.'
    )
