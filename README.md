# Agent 过程级评测 Harness

一个 agent **过程级**评测平台：驱动被测 agent 完成任务、完整记录过程事件、用多维度评测器打分并生成报告。

> **状态**：设计完成，正在实现。目前处于 M0（环境与骨架），尚无可用功能。

## 要解决的问题

现有 agent 评测工具几乎全部停留在**结果级**（pass@k、最终答案对错），无法回答「agent 为什么失败」。

对 GitHub 高星项目的调研结论：

| 项目 | Stars | 覆盖的过程级能力 |
|---|---|---|
| langfuse | 34.6k | 无（可观测性平台，eval 是附加） |
| mlflow | 27.9k | 无 |
| promptfoo | 25.1k | 无（prompt / 红队测试） |
| comet-ml/opik | 22.0k | 无 |
| deepeval | 18.3k | 无（pytest 风格断言） |
| **langchain-ai/agentevals** | **720** | 仅轨迹匹配单一维度 |

**过程级评测与 CI 门禁两个方向存在明显空白**——过程级做得最深的工具只有 720 stars，且只覆盖单一维度。

## 三条设计主线

1. **过程级评测的方法论** —— 12 个失败模式（9 个规则驱动 / 3 个 LLM 驱动）、5 种轨迹匹配模式、幻觉工具输出检测、元评测
2. **可执行的架构约束** —— L0 叶子层 + `JudgeClient` 依赖倒置，使「评测器与 agent 零耦合」成为**可执行验证**的约束而非文档承诺
3. **工程严谨性** —— 可复现（record/replay）、可审计（judge 自带轨迹）、有边界（预算治理 + 沙箱）

### 双 Harness 对称

被测 agent 与评测用的 judge agent **复用同一个 `Run` 类**，只是 `RunSpec` 取值不同。因此 judge 自带完整轨迹 → 可审计、可复现、可测成本，并让**元评测**（judge consistency / judge cost / 抗注入）成为可能。

## 文档

| 文档 | 内容 |
|---|---|
| [设计文档](docs/superpowers/specs/2026-09-14-agent-eval-harness-design.md) | 系统设计、架构决策与理由、风险清单 |
| [技术选型](docs/tech-stack.md) | 每个依赖为什么选它、版本约束、核实证据 |
| [实现计划](docs/superpowers/plans/) | 分任务的 TDD 步骤（Part 1/2/3，共 36 个任务） |
| [教学文档](docs/learning/) | 逐任务讲解（目标 / 流程 / 实现 / 技术栈 / 工程思想） |

## 开发

```bash
uv sync --all-groups      # 建环境并装依赖（Python 3.12）
uv run pytest             # 单元测试，零 LLM 调用
uv run lint-imports       # 架构约束检查
uv run pyright src        # 类型检查
```

**约定**：所有命令走 `uv run`，不要手动 activate。提交信息用简洁英文。

## 技术栈

Python 3.12 · uv · pydantic 2.13 · openai 3.x SDK + httpx2 · asyncio（stdlib）· JSONL + zstd + stdlib sqlite3 · typer + rich · jinja2 + ECharts · pytest + hypothesis · ruff + pyright + import-linter

选型理由见 [docs/tech-stack.md](docs/tech-stack.md)。
