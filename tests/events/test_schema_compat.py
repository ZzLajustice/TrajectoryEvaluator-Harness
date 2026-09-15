"""Schema 稳定性的绊线测试。

## 为什么需要它

事件 schema 是评测器依赖的契约。没有这层保护，任何一次"顺手加个字段"
都会让已提交的历史轨迹无法解析 —— 而这类破坏是**沉默的**：
评测器不会报错，只会算出错误的结果。

字段集快照让任何 schema 改动在 code review 中**显式可见**。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from harness.events import types as T

FIXTURE = Path(__file__).parent.parent / "fixtures" / "event_fields.json"


def _field_sets() -> dict[str, list[str]]:
    """收集每个事件子类的字段名。基类 Event 与类型别名被排除。"""
    out: dict[str, list[str]] = {}
    for name, obj in vars(T).items():
        if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
            continue
        if obj is T.Event or "type" not in obj.model_fields:
            continue
        out[name] = sorted(obj.model_fields)
    return dict(sorted(out.items()))


def test_event_field_sets_are_frozen():
    actual = _field_sets()

    if not FIXTURE.exists():  # 首次运行生成基线
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        # 显式 newline="\n"：Windows 上 write_text 默认把 \n 翻译成 \r\n，
        # 会让生成的 fixture 与 .gitattributes 的 eol=lf 不一致。
        with FIXTURE.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(actual, indent=2, ensure_ascii=False) + "\n")

    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert actual == expected, (
        "事件字段集变了。若是有意为之，请更新 tests/fixtures/event_fields.json "
        "并在提交信息中说明理由；若是顺手改的，请改用 attrs 逃生舱。"
    )


def test_all_events_share_the_base_fields():
    """每个事件都必须带 run_id / seq / ts / attrs。"""
    base = {"run_id", "seq", "ts", "attrs", "type"}
    for name, fields in _field_sets().items():
        missing = base - set(fields)
        assert not missing, f"{name} 缺基类字段: {missing}"
