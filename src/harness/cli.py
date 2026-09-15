"""harness CLI。

本文件在任务 1 只建最小骨架 —— 两个原因：
  1. `[project.scripts] harness = "harness.cli:app"` 需要 `app` 存在
  2. import-linter 的 layers 契约要求每个具名模块存在，
     缺了 `harness.cli` 会让 `lint-imports` 直接报错而非校验通过

`run` / `trace` / `report` / `diff` / `ci` 子命令在任务 11 及之后补齐。

退出码约定（CI 门禁用）：
    0 通过 / 1 门禁未达标 / 2 配置或加载错误 / 3 预算超限 / 4 基线缺失
"""
from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Agent 过程级评测 harness。"""
