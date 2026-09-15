"""LLM 响应采样池。

## 它不是 HTTP cassette

HTTP 层的录制回放交给 `vcrpy`（`tests/fixtures/cassettes/`）。本模块解决的是
另一个问题：**同一请求返回 N 个不同样本**，供 `MetaEvaluator` 测量
judge consistency —— 没有多样的样本就谈不上一致性。

vcrpy 的 `allow_playback_repeats` 只能重复**同一个**响应，做不到这件事，
所以自研。两者是独立关注点，不要混为一谈。

## 存储形态

    {key: [resp, resp, ...]}

是 **list + 游标**，不是单个响应。游标在调用方（`ReplayProvider`）维护，
因为"取到第几个样本"是回放会话的状态，不该写进持久化的池子里。

## key 的构成

`sha256(model + 消息 + 工具 + 温度 + tool_choice)`。

**温度必须进 key** —— 它影响采样，不进 key 会让不同温度下的样本混在一起，
consistency 测量就失去意义。反过来，不影响输出的字段（如超时设置）不该进 key。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness.contracts.protocols import LLMRequest


def _key_of(req: LLMRequest, model: str) -> str:
    payload = {
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        "tools": req.tools,
        "temperature": req.temperature,
        "tool_choice": req.tool_choice,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class ResponsePool:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._data: dict[str, list[dict[str, Any]]] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    key_of = staticmethod(_key_of)

    def record(self, req: LLMRequest, response: dict[str, Any]) -> None:
        self._data.setdefault(_key_of(req, req.model), []).append(response)

    def replay(self, req: LLMRequest, *, occurrence: int) -> dict[str, Any]:
        """取第 `occurrence` 个样本。超出则回绕。

        回绕而非报错：consistency 测量会跑很多次，硬性上限会打断实验。
        """
        key = _key_of(req, req.model)
        samples = self._data.get(key)
        if not samples:
            raise KeyError(
                f"response not recorded for key {key[:12]}... "
                f"(model={req.model}, {len(req.messages)} messages). "
                "Record first with the recording provider, or check that the "
                "prompt and sampling parameters are unchanged."
            )
        return samples[occurrence % len(samples)]

    def occurrence_count(self, req: LLMRequest) -> int:
        return len(self._data.get(_key_of(req, req.model), []))

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" + 手工加换行：Windows 上 write_text 会把 \n 变成 \r\n，
        # 与 .gitattributes 的 eol=lf 冲突，也让跨平台对拍失败
        with self._path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(self._data, indent=2, ensure_ascii=False) + "\n")
