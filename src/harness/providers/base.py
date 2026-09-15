"""Provider 共用的归一化辅助。

各厂商的兼容层在字段名与缺省行为上都有偏差，归一化逻辑集中在这里，
避免每个 provider 各写一份。
"""

from __future__ import annotations

from typing import Any

from harness.contracts.results import Usage


def usage_from_openai(raw: dict[str, Any] | None) -> Usage:
    """OpenAI 风格的 usage → 内部 `Usage`。

    **缺 usage 是常态**（不少兼容层不返回它），因此降级为全 0 而非抛异常。
    记 0 比崩掉好 —— 上层至少能拿到结果，代价只是成本统计缺失。
    """
    if not raw:
        return Usage()
    details = raw.get("prompt_tokens_details") or {}
    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cache_read_tokens=int(details.get("cached_tokens") or 0),
        calls=1,
    )
