"""runs/ 目录的落地布局。

## 为什么这个常量需要一个共用位置

`latest.json` 由组装层（`orchestration.deps`）写，由报告层（`report.data`）
与对比层（`orchestration.diff`）读。**报告层不许 import 组装层**
（层契约：`report` 只能向下依赖 `events` / `contracts` / `store`），
所以这个文件名不能住在 `orchestration` 里。

放进 `store` 是合适的：runs 目录的布局本来就是 store 层的事
（`<run_id>.jsonl` 与 `index.db` 同理）。

写成两处字面量的话，改一处漏一处时 diff 会**安静地**拿一份过期快照去比 ——
而"没有回归"这个结论看起来完全正常。
"""

from __future__ import annotations

# case 级聚合快照。diff / ci / report 的输入。
SNAPSHOT_NAME = "latest.json"

# SQLite 查询索引
INDEX_NAME = "index.db"

# 每条 run 一个 JSONL 轨迹文件（真相源）
TRAJECTORY_SUFFIX = ".jsonl"
