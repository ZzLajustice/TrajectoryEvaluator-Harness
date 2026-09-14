# Agent 过程级评测 Harness — 教学文档

本目录按**实现计划的任务**逐章讲解，一个任务一章。每章包含五个部分：

| 部分 | 内容 |
|---|---|
| **1. 总体目标** | 这个任务在系统里处于什么位置，解决什么痛点 |
| **2. 实现流程** | 逻辑顺序，以及为什么是这个顺序 |
| **3. 具体技术实现** | 关键 API、易踩的坑、设计取舍 |
| **4. 使用的技术栈简介** | 涉及的库是什么、为什么选它、替代品有哪些 |
| **5. 工程化思想** | 能迁移到其他项目的通用原则 |

> **阅读建议**：第 3 节和第 5 节是重点。第 3 节讲「怎么做的和为什么这么做」，第 5 节讲「这件事教会你什么通用的东西」。如果时间有限，优先读第 5 节。

---

## 目录

### 第一部分：地基与垂直切片（M0–M2）

| 章 | 任务 | 核心内容 |
|---|---|---|
| [01](01-project-scaffold.md) | 项目脚手架 | 环境隔离、uv 管理、架构约束基线 |
| [02](02-event-model.md) | 事件模型 | 判别联合、schema 快照测试、`extra="forbid"` |
| [03](03-trajectory-view.md) | 只读轨迹视图 | 索引预建、不可变返回、O(1) 查找 |
| [04](04-contracts-spec.md) | `RunSpec` | 可完整序列化的配置、指纹用于可比性判断 |
| [05](05-contracts-protocols.md) | 协议与值对象 | `Protocol` 结构化子类型、值对象与事件解耦 |
| [06](06-fake-provider.md) | `FakeProvider` | 脚本化 provider，让端到端测试完全离线 |
| [07](07-tool-registry.md) | 工具注册表 | 策略过滤、`finish` 作为终止契约 |
| [08](08-jsonl-store.md) | JSONL 存储 | 非阻塞入队、并发 run 不互锁 |
| [09](09-middleware-pipeline.md) | 洋葱管道 | `reduce(reversed())` 的顺序语义、状态作用域规约 |
| [10](10-agent-loop-and-run.md) | agent loop 与 `Run` | 同步 emit 的 hot path、seq 全序 |
| [11](11-cli-vertical-slice.md) | CLI 垂直切片 | 最早跑通端到端的最小路径 |
| [12](12-local-executor.md) | `LocalExecutor` ⚠️ | **Windows 进程树杀死、`WinError 32`** |
| [13](13-fs-tools.md) | 文件工具 | 路径越狱防御、文件操作走 Executor 抽象 |
| [14](14-run-command-sandbox.md) | `run_command` 与沙箱 | 禁网、危险命令黑名单的定位 |
| [15](15-workspace-lifecycle.md) | Workspace 生命周期 | `keep_on_failure` 保留现场 |

### 第二部分：真实模型与评测核心（M3–M5）

| 章 | 任务 | 核心内容 |
|---|---|---|
| [16](16-openai-compat-provider.md) | `OpenAICompatProvider` | **HTTP 栈分叉**、为什么不用 litellm、`raw` 字段 |
| [17](17-response-pool.md) | `ResponsePool` | 多样本采样、录制/回放装饰器 |
| [18](18-provider-conformance.md) | 一致性测试套件 | 参数化后新增实现只加一个 factory |
| [19](19-guard-middlewares.md) | 权限/策略/沙箱中间件 | 三层独立防线、短路仍返回完整结果 |
| [20](20-telemetry-middleware.md) | `TelemetryMW` | **异常路径埋点**、`CancelledError` 不吞 |
| [21](21-budget-governor.md) | 预算治理 | **独立终态 vs 错误**、三处强制 |
| [22](22-context-manager.md) | 上下文管理 | 只要求压缩可观测、token 双轨估算 |
| [23](23-evaluator-framework.md) | 评测器框架 ⭐ | **声明式订阅变成真行为**、`JudgeClient` 依赖倒置 |
| [24](24-trajectory-builder.md) | `TrajectoryBuilder` | 测试工具作为产品一部分发布 |
| [25](25-trajectory-matcher.md) | `TrajectoryMatcher` | 两个正交维度、`arg_normalizers` 必须性 |
| [26](26-efficiency-analyzer.md) | `EfficiencyAnalyzer` | 步数比、冗余检测 |

### 第三部分：编排与报告（M6–M8）

| 章 | 任务 | 核心内容 |
|---|---|---|
| [27](27-suite-loader.md) | Suite loader | `yaml.safe_load`、加载期校验 |
| [28](28-sqlite-index.md) | `SqliteIndex` | **stdlib sqlite3 而非 aiosqlite**、单写者队列 |
| [29](29-scheduler.md) | `Scheduler` | 按输入顺序返回、异常隔离、`ExceptionGroup` |
| [30](30-failure-classifier.md) | `FailureClassifier` ⭐ | **MAST 分类法适配**、9 规则 / 3 LLM |
| [31](31-grounding-checker.md) | `GroundingChecker` ⭐ | 幻觉工具输出、截断不判 FAIL |
| [32](32-aggregator-and-terminal-report.md) | 聚合与终端报告 | `pass_rate`/`pass@k`/`flaky_rate` 分列 |
| [33](33-html-report.md) | HTML 报告 | 自包含单文件、ECharts 内联、autoescape |
| [34](34-diff-and-ci-gate.md) | diff 与 CI 门禁 | 指纹不可比、退出码约定 |

### 第四部分：架构高潮与收尾（M9–M11）

| 章 | 任务 | 核心内容 |
|---|---|---|
| [35](35-dual-harness-and-meta-eval.md) | **双 Harness 对称与元评测** ⭐⭐ | judge 复用同一个 `Run`、元评测三指标 |
| [36](36-adapters-and-finale.md) | adapter、用例集、架构测试 | 通用性论证、架构约束的可执行契约 |

---

## 配套文档

| 文档 | 用途 |
|---|---|
| [设计文档](../superpowers/specs/2026-09-14-agent-eval-harness-design.md) | 系统设计、架构决策与理由、风险 |
| [技术选型](../tech-stack.md) | 每个依赖的选择依据、版本约束、核实证据 |
| [实现计划 Part 1](../superpowers/plans/2026-09-14-agent-eval-harness-part1-foundation.md) | 任务 1–15 的 TDD 步骤 |
| [实现计划 Part 2](../superpowers/plans/2026-09-14-agent-eval-harness-part2-eval-core.md) | 任务 16–26 的 TDD 步骤 |
| [实现计划 Part 3](../superpowers/plans/2026-09-14-agent-eval-harness-part3-orchestration.md) | 任务 27–36 的 TDD 步骤 |

---

## 三条主线（读完全部章节后应能回答）

1. **过程级评测怎么做** —— 12 个失败模式（9 规则 / 3 LLM）、5 种轨迹匹配模式、grounding 检测、元评测
2. **架构如何保证可扩展** —— L0 叶子层 + `JudgeClient` 依赖倒置使「评测器与 agent 零耦合」成为**可执行验证**的约束；双 Harness 对称使元评测成为可能
3. **工程严谨性体现在哪** —— 可复现（record/replay）、可审计（judge 自带轨迹）、有边界（预算治理 + 沙箱）
