"""真模型凭据解析测试。

## 全部离线

这些测试**不发任何请求**。构造 `OpenAICompatProvider` 只创建一个 httpx 客户端，
不建立连接 —— 所以"分派到真 provider"这件事可以在离线状态下断言。

真机验证是另一回事，见 docs/known-gaps.md §1.1。

## 为什么凭据要单独一层

因为它是**三处不同的人会去查的东西**的汇合点：suite 作者查"我 provider 写对了吗"、
运维查"key 配了吗"、CI 查"环境变量传了吗"。
散在 `_build_provider` 里的话，出错时给不出"我试过哪些变量"这种信息。
"""

from __future__ import annotations

import pytest

from harness.contracts.spec import ModelRef
from harness.orchestration.credentials import (
    KNOWN_BASE_URLS,
    HarnessSettings,
    ProviderConfigError,
    is_fake,
    resolve_endpoint,
)

# 会污染结果的真实环境变量 —— 每个测试前清干净
_ENV_NAMES = (
    "HARNESS_API_KEY", "HARNESS_JUDGE_API_KEY", "HARNESS_BASE_URL",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
    "QWEN_API_KEY", "QWEN_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    # 强制 cwd 到一个没有 .env 的目录，否则仓库根的 .env 会漏进来
    monkeypatch.chdir(tmp_path)


def _model(provider: str = "deepseek", model: str = "deepseek-v4-flash",
           **extra) -> ModelRef:
    return ModelRef(provider=provider, model=model, extra=extra)


def _settings(**kw) -> HarnessSettings:
    return HarnessSettings(_env_file=None, **kw)  # type: ignore[call-arg]


# ---- 厂商判定 ----
def test_fake_providers_are_recognised():
    assert is_fake("fake")
    assert is_fake("replay")
    assert not is_fake("deepseek")


def test_deepseek_is_in_the_vendor_table():
    assert KNOWN_BASE_URLS["deepseek"] == "https://api.deepseek.com"


# ---- base_url 阶梯 ----
def test_base_url_comes_from_the_vendor_table(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    assert resolve_endpoint(_model()).base_url == "https://api.deepseek.com"


def test_explicit_base_url_in_the_suite_wins(monkeypatch):
    """自建网关 / 区域端点走这条 —— 厂商表再准也不该挡住它。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://env.example/v1")
    ep = resolve_endpoint(_model(base_url="https://gateway.internal/v1"))
    assert ep.base_url == "https://gateway.internal/v1"


def test_vendor_env_var_beats_the_table(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://proxy.internal/v1")
    assert resolve_endpoint(_model()).base_url == "https://proxy.internal/v1"


def test_generic_base_url_env_is_used(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "sk-x")
    ep = resolve_endpoint(_model(provider="qwen"), settings=_settings(
        base_url="https://generic.internal/v1"))
    assert ep.base_url == "https://generic.internal/v1"


def test_unknown_provider_without_a_base_url_fails_loudly(monkeypatch):
    """未知厂商且没给 base_url —— 必须报清楚，而不是拼一个猜的 URL 出去。"""
    monkeypatch.setenv("MYSELF_API_KEY", "sk-x")
    with pytest.raises(ProviderConfigError, match="unknown provider"):
        resolve_endpoint(_model(provider="myself"))


# ---- api_key 阶梯 ----
def test_vendor_specific_key_wins(monkeypatch):
    """多厂商共存时它是不打架的那个写法（SUT 用便宜模型、judge 用强模型）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-vendor")
    monkeypatch.setenv("HARNESS_API_KEY", "sk-generic")
    assert resolve_endpoint(_model()).api_key == "sk-vendor"


def test_generic_key_is_the_fallback(monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "sk-generic")
    assert resolve_endpoint(_model()).api_key == "sk-generic"


def test_judge_key_is_used_for_the_judge_role(monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "sk-sut")
    monkeypatch.setenv("HARNESS_JUDGE_API_KEY", "sk-judge")
    assert resolve_endpoint(_model(), role="judge").api_key == "sk-judge"


def test_judge_falls_back_to_the_generic_key(monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "sk-sut")
    assert resolve_endpoint(_model(), role="judge").api_key == "sk-sut"


def test_vendor_key_beats_the_judge_key(monkeypatch):
    """厂商专属变量优先级最高 —— 明确指定了厂商就别再猜角色。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-vendor")
    monkeypatch.setenv("HARNESS_JUDGE_API_KEY", "sk-judge")
    assert resolve_endpoint(_model(), role="judge").api_key == "sk-vendor"


def test_missing_key_names_every_variable_it_tried(monkeypatch):
    """★ 报错要告诉人**去哪配**，而不是只说"没找到 key"。"""
    with pytest.raises(ProviderConfigError) as ei:
        resolve_endpoint(_model(), role="judge")
    message = str(ei.value)
    assert "DEEPSEEK_API_KEY" in message
    assert "HARNESS_JUDGE_API_KEY" in message
    assert "HARNESS_API_KEY" in message
    assert ".env" in message


def test_a_key_is_never_read_from_the_suite(monkeypatch):
    """★ suite 文件是入库的 —— 把 key 写进去等于提交上去。

    `ModelRef` 里刻意没有 api_key 字段。这条测试把那个设计决定钉住：
    往 extra 里塞 key 不该生效。
    """
    with pytest.raises(ProviderConfigError):
        resolve_endpoint(_model(api_key="sk-in-suite"))


# ---- .env 文件 ----
def test_dotenv_file_is_read(tmp_path, monkeypatch):
    """`.env` 是本地开发的便利路径；环境变量仍然优先。"""
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=sk-from-dotenv\n", encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_endpoint(_model()).api_key == "sk-from-dotenv"


def test_environment_beats_the_dotenv_file(tmp_path, monkeypatch):
    """CI 里没有 .env，只有环境变量 —— 它必须能覆盖本地文件。"""
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=sk-from-dotenv\n", encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    assert resolve_endpoint(_model()).api_key == "sk-from-env"


def test_dotenv_can_carry_the_base_url(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=sk-x\nDEEPSEEK_BASE_URL=https://dotenv.example/v1\n",
        encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_endpoint(_model()).base_url == "https://dotenv.example/v1"


def test_unknown_keys_in_dotenv_are_ignored(tmp_path, monkeypatch):
    """`.env` 里常有别的工具写的键，不该因此报错。"""
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=sk-x\nSOME_OTHER_TOOL=whatever\n",
        encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_endpoint(_model()).api_key == "sk-x"


# ---- 解析结果 ----
def test_resolved_endpoint_carries_the_provider_name(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    ep = resolve_endpoint(_model())
    assert ep.provider == "deepseek"
    assert ep.api_key == "sk-x"


def test_provider_names_with_dashes_are_normalised(monkeypatch):
    """`openai-compat` 与 `openai_compat` 该指向同一个变量名。"""
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://x.example/v1")
    ep = resolve_endpoint(_model(provider="openai-compat"))
    assert ep.api_key == "sk-x"
