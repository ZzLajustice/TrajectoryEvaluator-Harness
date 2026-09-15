"""case 级快照的**文件读写**。

## 为什么这一层住在 store 而不是 orchestration

快照由组装层写、由报告层读，而**报告层不许 import 组装层**
（层契约：`report` 只能向下依赖 `events` / `contracts` / `store`）。

所以文件格式与读写放在 `store`（两边都能看到），
"聚合指标怎么算、每一行长什么样" 留在 `orchestration.aggregator`。
分界线是：**这里只管字节与格式校验，不认识任何领域类型**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SnapshotError(ValueError):
    """快照文件格式不对 —— 映射到 CLI 退出码 2。"""


def write_snapshot_dict(data: dict[str, Any], path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=False：中文用例名不该被转义成 \uXXXX，报告要能直接读
    # newline="\n"：Windows 上默认会翻成 \r\n，快照会被 diff 出满屏噪声
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                 encoding="utf-8", newline="\n")
    return p


def read_snapshot(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"snapshot not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"snapshot is not valid JSON: {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise SnapshotError(f"snapshot root must be an object, got {type(data).__name__}")
    return data
