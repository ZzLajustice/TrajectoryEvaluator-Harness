"""推理内容的投影层 —— 从 provider 原始响应里取出模型的推理轨迹。

## 与 `events/otel.py` 是同一个角色

L0 里唯一允许认识**外部 payload 形状**的地方，各认识一份：

| 模块 | 认识什么 | 为什么住在这里 |
|---|---|---|
| `otel.py` | OTel GenAI 的属性名 | 属性名还在改，隔离在一个模块里就能跟着改 |
| `reasoning.py` | 推理字段在 OpenAI 兼容响应里的路径 | 让评测器不必各自去掏 `raw` |

## 为什么不给 `LLMResponseEvent` 加一个 `reasoning` 字段

项目规约：**加字段前先自问能否从已有事件派生**。推理能派生 ——
`raw` 是刻意保留的原始响应（replay 无损性的前提），它在里面。

但"能派生"≠"到处派生"：路径是厂商形状，散在评测器里的话，
换一个厂商要改 N 处，而**漏改的那一处不会报错，只会静默地永远取不到推理**——
那看起来与"这个模型本来就不产生推理"一模一样。所以派生逻辑只有一份。

## 体量取自 provider，不数中文字符

`usage.completion_tokens_details.reasoning_tokens` 是 provider 直接上报的。
实测（2026-09-16 全量真跑）**194/194 个响应都带它**，
所以这里**没有**"回退到字符数"的路径 —— 那条路径永远不会被执行，
却要一直维护（且它给出的数字与 token 不同量纲，混用会得到无意义的比率）。

字符数只在需要**内容**的地方用（judge 要读、报告要展示）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.events.types import LLMResponseEvent

#: 推理字段在 OpenAI 兼容响应里的路径。
#:
#: DeepSeek 用 `reasoning_content`；OpenAI 的 o 系列用 `reasoning`，
#: 且放在 content blocks 里而不是 message 上 —— 那个形状这里**不认**，
#: 因为它不是"同一件事换个名字"，取法不同。真要支持时在这里加分支，
#: 而分叉点只此一处。
REASONING_KEY = "reasoning_content"


@dataclass(frozen=True, slots=True)
class Reasoning:
    """一轮响应的推理。

    `seq` 是**必须**的：报告与 judge 要指回具体某个事件
    （设计文档的 `EvidenceRef` 靠 `seq`/`span_id` 深链），
    只给文本的话，"哪一轮在困惑"就说不清了。
    """

    #: 该响应事件的 `seq`，供深链
    seq: int
    #: 轮次；老轨迹里可能没有
    turn: int | None
    #: 推理正文（原文，未截断 —— 截断由消费方按自己的上下文预算决定）
    text: str
    #: provider 上报的 reasoning tokens；缺省 0
    tokens: int


def reasoning_of(event: LLMResponseEvent) -> Reasoning | None:
    """取出一条响应的推理；没有就是 `None`（不是空 `Reasoning`）。

    **不抛异常**：`raw` 是逃生舱，形状不保证 —— 回放旧 cassette、
    接一个不返回这个字段的网关、甚至 `raw` 里放的不是 dict，
    都必须退化成"这条没有推理"，而不是让整条评测路径炸掉。
    """
    text = _text(event.raw)
    if text is None:
        return None
    return Reasoning(seq=event.seq, turn=event.turn, text=text,
                     tokens=_tokens(event.raw))


def _text(raw: Any) -> str | None:
    """从原始响应里取推理正文；取不到或为空白返回 `None`。

    ★ 空白也算"没有"。`if text:` 与 `if text.strip():` 在这里差别很实：
    前者会让报告把"有 0 字符推理的响应"计入"带推理的响应数"，
    而那个数字正是用来说明"推理被浪费了多少"的。
    """
    if not isinstance(raw, dict):
        return None
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    value = message.get(REASONING_KEY)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _tokens(raw: Any) -> int:
    """Provider 上报的推理 token 数；没有就 0。"""
    if not isinstance(raw, dict):
        return 0
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return 0
    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        return 0
    value = details.get("reasoning_tokens")
    # `bool` 是 `int` 的子类 —— 别把 True 当成 1 个 token
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)
