# Agent 过程级评测 Harness

一个 agent **过程级**评测平台：驱动被测 agent 完成任务、完整记录过程事件、用多维度评测器打分并生成报告。

**要解决的问题**：现有工具几乎只评结果（pass@k、答案对错），无法回答「agent 为什么失败」。调研 GitHub 高星项目后确认过程级评测与 CI 门禁存在明显空白——过程级做得最深的 [agentevals](https://github.com/langchain-ai/agentevals) 仅 720 stars 且只覆盖轨迹匹配单一维度。

## 当前状态（重要）

**M0–M7 已完成**（任务 1–31 / 共 36）。`src/harness/` 58 个文件，668 条测试全绿。

已完成的能力：事件模型与只读 `Trajectory`、`RunSpec`/`Run` 双 harness 骨架、6 个 SUT 工具、Windows 进程树执行器、5 个中间件（telemetry 最外层）、budget governor、真 provider + record/replay、suite 加载器（defaults + cases）+ 并发调度器 + SQLite 索引、**4 个评测器**（`TrajectoryMatcher` / `EfficiencyAnalyzer` / `FailureClassifier` / `GroundingChecker`）。

`harness run` 支持的开关：`--evaluate` / `--concurrency` / `--case` / `--record` / `--replay` / `--out` / `--workdir`。

**尚未实现**（后续里程碑）：报告与 diff（M8）、judge 工具与 `JudgeClient`（M9）、`MetaEvaluator`（M10）、adapters/`ci`/用例集（M11）。`tests/test_architecture.py` 属 M11，目前**还不存在** —— 架构约束暂时只由 `uv run lint-imports` 守着。

下一步是 [Part 3 计划](docs/superpowers/plans/) 的 M8（任务 32–34）。

## 文档地图

| 要找什么 | 去哪 |
|---|---|
| 系统设计、架构决策与理由、风险清单 | [docs/superpowers/specs/2026-09-14-agent-eval-harness-design.md](docs/superpowers/specs/2026-09-14-agent-eval-harness-design.md) |
| 每个依赖为什么选它、版本约束、核实证据 | [docs/tech-stack.md](docs/tech-stack.md) |
| 分任务的 TDD 实现步骤（含真实代码与命令） | [docs/superpowers/plans/](docs/superpowers/plans/)（Part 1/2/3） |
| 逐任务的教学讲解（五节式：目标/流程/实现/技术栈/工程思想） | [docs/learning/](docs/learning/) |

## 关键技术决策（已论证，不要随手推翻）

| 决策 | 理由 | 详见 |
|---|---|---|
| LLM 客户端用 **官方 `openai` SDK**，不用 litellm | 2026 年 HTTP 栈已分叉：`openai`/`anthropic` 依赖 `httpx2`，而 `litellm`/`respx`/`instructor` 锁在旧 `httpx`，**同环境无法共存**。且 litellm 会抹平厂商差异，而评测需要精确复现 payload | tech-stack §0、§1 |
| 异步用 **裸 `asyncio`**，不引入 anyio | anyio 的核心价值（trio 兼容）只对库作者成立；本项目是应用，stdlib 3.11+ 已够用 | tech-stack §4.1 |
| 类型检查用 **`pyright`**，不用 mypy/ty | 本项目 pydantic 密集，`ty` 无 plugin 系统会误报 | tech-stack §9 |
| 报告图表用 **ECharts**，不用 Chart.js | 评测报告需要热力图/雷达图，Chart.js 做不了且已 11 个月停更 | tech-stack §7.2 |
| **`evaluators/` 绝不 import `core`** | 架构支点。评测器只认 `contracts/` 里的 `JudgeClient` 协议，真实实现由组装层注入 | 设计文档 §2.3 |
| 失败模式分类 = **MAST 单 agent 适用 8 个 + 补充 4 个** | MAST 面向多智能体，FC2「智能体间失调」6 个模式在单 agent 架构下**结构上不存在**，必须在报告中写明 | 设计文档 §4.2 |
| 指标 **`pass_rate`/`pass@k`/`flaky_rate` 分列** | 合并成单一总分会让过程评测的意义被 outcome 淹没 | 设计文档 §4.6 |

## 实施约定

- **所有命令走 `uv run`**，不要手动 activate。项目内 `.venv` 锁 Python 3.12（本机默认是 3.14.5，不要用）
- **TDD 五步循环**：写失败测试 → 确认失败 → 最小实现 → 确认通过 → commit
- **架构约束可执行**：`uv run lint-imports`（契约写在 `pyproject.toml`）。M11 会再加一份纯 ast 的 `tests/test_architecture.py`，两者**刻意冗余**
- **L0 叶子层规则**：`events/` 只能 import 自己；`contracts/` 只能向下 import `events`。其他层只许向下依赖 L0
- **suite 格式是 `defaults` + `cases`**（`orchestration/suite.py`）。配置错误一律在 `load_suite` 抛错：未知评测器名/中间件名、重复 case_id、`case_id` 与 `task.case_id` 不一致
- **评测器白名单只有一份**：`evalrunner.EVALUATOR_REGISTRY`。suite 加载器引用它，不另抄一份清单 —— 两份必然漂移，且方向恰好是"加载期放行、运行期才炸"
- **一次 suite 共用一个 `CompositeStore`**。多路 run 并发 append，靠单写者队列落盘。改 store 的并发语义前先看 `tests/store/` 里的并发测试
- **评测器调度住在 `evaluators/base.py::run_evaluators`**，不在组装层。它只用到 L0 类型，第三方写评测器时 import 一个模块就同时拿到基类和调度器
- **提交前必须四个门全绿**：`uv run pytest` / `ruff check .` / `pyright` / `lint-imports`。四条命令与 `git commit` **分开执行**，先读输出再提交（曾把 ruff 错误一起提交过）
- **Windows 特有约束**（踩过坑，勿改）：
  - `asyncio.create_subprocess_exec` 只支持 `ProactorEventLoop`，**绝不设置 `WindowsSelectorEventLoopPolicy`**
  - `asyncio.wait_for` 超时**不杀子进程**，必须 `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T`，否则留下孤儿进程并导致 `WinError 32`
- **测试零成本**：单元测试不得产生 LLM 调用。用 `TrajectoryBuilder` 构造事件序列，用 `httpx2.MockTransport` 拦截 HTTP

## Git

- 仓库根是 `评测harness/`，首次提交只含文档
- commit message 用简洁英文
- 提交前先展示变更摘要

<!-- superpowers-zh:begin (do not edit between these markers) -->
# Superpowers-ZH 中文增强版

本项目已安装 superpowers-zh 技能框架（20 个 skills）。

## 核心规则

1. **收到任务时，先检查是否有匹配的 skill** — 哪怕只有 1% 的可能性也要检查
2. **设计先于编码** — 收到功能需求时，先用 brainstorming skill 做需求分析
3. **测试先于实现** — 写代码前先写测试（TDD）
4. **验证先于完成** — 声称完成前必须运行验证命令

## 可用 Skills

Skills 位于 `.claude/skills/` 目录，每个 skill 有独立的 `SKILL.md` 文件。

- **brainstorming**: 在任何创造性工作之前必须使用此技能——创建功能、构建组件、添加功能或修改行为。在实现之前先探索用户意图、需求和设计。
- **chinese-code-review**: 中文 review 沟通参考——话术模板、分级标注（必须修复/建议修改/仅供参考）、国内团队常见反模式应对。仅在用户显式 /chinese-code-review 时调用，不要根据上下文自动触发。
- **chinese-commit-conventions**: 中文 commit 与 changelog 配置参考——Conventional Commits 中文适配、commitlint/husky/commitizen 中文模板、conventional-changelog 中文配置。仅在用户显式 /chinese-commit-conventions 时调用，不要根据上下文自动触发。
- **chinese-documentation**: 中文文档排版参考——中英文空格、全半角标点、术语保留、链接格式、中文文案排版指北约定。仅在用户显式 /chinese-documentation 时调用，不要根据上下文自动触发。
- **chinese-git-workflow**: 国内 Git 平台配置参考——Gitee、Coding.net、极狐 GitLab、CNB 的 SSH/HTTPS/凭据/CI 接入差异与镜像同步配置。仅在用户显式 /chinese-git-workflow 时调用，不要根据上下文自动触发。
- **dispatching-parallel-agents**: 当面对 2 个以上可以独立进行、无共享状态或顺序依赖的任务时使用
- **executing-plans**: 当你有一份书面实现计划需要在单独的会话中执行，并设有审查检查点时使用
- **finishing-a-development-branch**: 当实现完成、所有测试通过、需要决定如何集成这份工作时使用
- **mcp-builder**: MCP 服务器构建方法论 — 系统化构建生产级 MCP 工具，让 AI 助手连接外部能力
- **receiving-code-review**: 收到代码审查反馈后、实施建议之前使用，尤其当反馈不明确或技术上有疑问时——需要技术严谨性和验证，而非敷衍附和或盲目执行
- **requesting-code-review**: 完成任务、实现重要功能或合并前使用，用于验证工作成果是否符合要求
- **subagent-driven-development**: 当在当前会话中执行包含独立任务的实现计划时使用
- **systematic-debugging**: 遇到任何 bug、测试失败或异常行为时使用，在提出修复方案之前执行
- **test-driven-development**: 在实现任何功能或修复 bug 时使用，在编写实现代码之前
- **using-git-worktrees**: 当需要开始与当前工作区隔离的功能开发，或在执行实现计划之前使用——通过原生工具或 git worktree 回退机制确保隔离工作区存在
- **using-superpowers**: 在开始任何对话时使用——确立如何查找和使用技能，要求在任何响应（包括澄清性问题）之前调用 Skill 工具
- **verification-before-completion**: 在宣称工作完成、已修复或测试通过之前使用，在提交或创建 PR 之前——必须运行验证命令并确认输出后才能声称成功；始终用证据支撑断言
- **workflow-runner**: 在 Claude Code / OpenClaw / Cursor 中直接运行 agency-orchestrator YAML 工作流——无需 API key，使用当前会话的 LLM 作为执行引擎。当用户提供 .yaml 工作流文件或要求多角色协作完成任务时触发。
- **writing-plans**: 当你有规格说明或需求用于多步骤任务时使用，在动手写代码之前
- **writing-skills**: 当创建新技能、编辑现有技能或在部署前验证技能是否有效时使用

## 如何使用

当任务匹配某个 skill 时，使用 `Skill` 工具加载对应 skill 并严格遵循其流程。绝不要用 Read 工具读取 SKILL.md 文件。

如果你认为哪怕只有 1% 的可能性某个 skill 适用于你正在做的事情，你必须调用该 skill 检查。
<!-- superpowers-zh:end -->
