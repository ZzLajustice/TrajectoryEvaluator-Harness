# Agent 过程级评测 Harness

驱动被测 agent 完成任务、完整记录过程事件、用多维度评测器打分并生成报告。
**不只回答「过没过」，而是回答「为什么没过」。**

> **状态**：M0–M11 完成，**1210 条测试**全绿，四个门全绿
> （`pytest` / `ruff` / `pyright` / `lint-imports`），全部测试**离线、零 LLM 调用**。
>
> 19 条用例三次真实全量跑（`deepseek-flash`，$0.227）：
> **pass_rate 0.842 · pass@k 0.947 · flaky_rate 0.105**。
> 已知边界与「这些数字不能证明什么」见 [docs/known-gaps.md](docs/known-gaps.md)。

---

## 快速开始

本项目用 [uv](https://docs.astral.sh/uv/) 管环境与依赖。装好 uv 后：

```bash
uv sync --all-groups        # 建环境并装依赖
uv run pytest               # 确认装对了：1210 条测试，零 LLM 调用
```

不需要自己准备 Python —— `uv sync` 会按 `requires-python` 自动拉 3.12，
本机已有别的版本也不影响。之后所有命令都是 `uv run <命令>`，**不用手动 activate**。

### 没有 API key

全部业务链路都能跑 —— 用假 provider，零网络、零成本、结果确定性。

| 能做什么 | 命令 |
|---|---|
| 最小闭环（1 条用例） | `uv run harness run -s examples/hello.yaml --evaluate` |
| **双 harness 对称**（judge + 元评测） | `uv run harness run -s examples/judged.yaml --evaluate` |
| 9 条脚本化失败模式 | `uv run harness run -s examples/traps.yaml --evaluate` |
| 并发压测 | `uv run harness run -s examples/concurrency.yaml` |
| 报告（终端 / HTML） | `uv run harness report -f html --out report.html` |
| 回归门禁 | `uv run harness ci -s examples/traps.yaml --baseline baselines/traps.json --fail-under 0.5` |
| 轨迹 / 对比 | `uv run harness trace --run-id <id>` · `uv run harness diff --baseline <a> --current <b>` |
| 校验用例集可解性 | `uv run python scripts/build_cases.py --check` |
| 全部测试 | `uv run pytest` |

### 有 API key

多出来的是**评测真实模型**这件事本身 —— 没有被测对象就没有评测。

```bash
cp .env.example .env          # 填上 DEEPSEEK_API_KEY
```

| 能做什么 | 命令 |
|---|---|
| 真 provider 冒烟 | `uv run harness run -s examples/deepseek.yaml -m deepseek-flash --provider deepseek` |
| **19 条 codefix 用例**（含 2 条真实 OSS 仓库），约 $0.07 / 次 | `uv run harness run -s suites/codefix --evaluate -m deepseek-flash --provider deepseek` |
| 多次采样看 flaky（`--repeat 3` ≈ $0.22） | 上面加 `--repeat 3` |
| 真模型 judge + 元评测 | `uv run harness run -s examples/judged_deepseek.yaml --evaluate` |

> 不给 `-m/--provider` 的话（默认是假 provider）会在**启动时**报配置错并说明原因，
> 而不是给你 19 条 `llm_error`。

五个命令：`run` / `trace` / `report` / `diff` / `ci`。
缺 key / 配错套件一律是**配置错误（退出码 2）**，并在消息里点名它试过哪几个变量。

---

## 业务功能

### 要解决的问题

现有 agent 评测工具几乎全部停留在**结果级**（pass@k、最终答案对错），无法回答
「agent 为什么失败」。调研 GitHub 高星项目后确认**过程级评测与 CI 门禁存在明显空白** ——
过程级做得最深的 [agentevals](https://github.com/langchain-ai/agentevals) 仅 720 stars，
且只覆盖轨迹匹配单一维度。

**一个具体的例子。** `trap_fabricate` 这条用例里，被测 agent 跑完可见测试、看到全绿、
然后说「测试全过，任务完成」。**这句话是真的** —— 可见测试确实全过，但验收要求的那条行为
没被任何可见测试覆盖：

- 结果级（只跑可见测试）：**满分**
- 轨迹级（无重复调用、无幻觉工具、确实跑了验证）：**过程干净**
- 本 harness：`OutcomeGrader` 跑隐藏验收测试 → **FAIL**，附上 pytest 原文

6 个评测器里有 5 个是轨迹级的，但**必须**有第 6 个是结果级的 —— 少了它，上面那个 run
会拿满分。

### 6 个评测器

| 评测器 | 层次 | 驱动方式 | 回答的问题 |
|---|---|---|---|
| `TrajectoryMatcher` | 轨迹级 | 规则 | 走的是不是一条合理路径（5 种匹配模式 + 参数归一化） |
| `EfficiencyAnalyzer` | 轨迹级 | 规则 | 步数 / token / 成本 / 冗余调用 / 推理体量 |
| `FailureClassifier` | 轨迹级 | 规则优先 + LLM 兜底 | 失败属于 12 个模式中的哪一个 |
| `GroundingChecker` | 轨迹级 | 规则优先 + LLM 兜底 | 有没有声称「文件里是 X」而工具输出里没有 X |
| `MetaEvaluator` | 元 | 消费 judge 轨迹 | judge 一致吗 / 判一次多少钱 / 抗注入吗 |
| **`OutcomeGrader`** | **结果级** | 规则（跑隐藏测试） | **代码到底修对没有** |

失败模式分类采用 [MAST](https://arxiv.org/abs/2503.13657) 作骨架，但做了**单 agent 适配**：
FC2「智能体间失调」那 6 个模式在单 agent 架构下**结构上不存在**，一律不采用；
另补 4 个单 agent 专属模式。合计 12 个，其中 9 个规则驱动（重跑数字不变）、
3 个需要语义判断的才交给 LLM。逐条论证见[设计文档](docs/superpowers/specs/2026-09-14-agent-eval-harness-design.md) §4.2。

### 指标**分列，不合并**

```
pass_rate      所有 repeat 都通过的 case 占比
pass@k         至少一次通过的 case 占比（注意：不是经典无偏估计量）
flaky_rate     通过率严格落在 (0,1) 之间的 case 占比
golden_score   轨迹匹配分（过程分）
outcome_pass   隐藏验收测试通过（结果分）
```

合并成一个总分会让过程评测的意义被 outcome 淹没 —— 那样这个工具就退化成了 pytest 的包装。
反过来，只有过程分则无法回答「到底修对没有」。口径写在报告脚注里
（`report/terminal.py::METRIC_DEFINITIONS`，终端与 HTML 共用同一份）。

### 用例集：19 条 codefix 用例

| 层级 | 条数 | 特征 |
|---|---|---|
| easy | 5 | 单文件、bug 明显 |
| medium | 7 | 需要读 docstring 才知道口径 / 跨 2-3 个模块 |
| hard | 7 | 多轮「改-跑-看-再改」；含 4 条过程陷阱与 2 条真实 OSS 仓库用例 |

**Track A（17 条）** 跑在 [`examples/toyrepo/`](examples/toyrepo/) 上 —— 自包含的 CSV +
统计工具库（551 行 + 43 条可见测试），不依赖第三方包，每条用例的循环都在几百毫秒内。

**Track B（2 条）** 跑在 [`pallets/itsdangerous`](https://github.com/pallets/itsdangerous)
的真实源码与测试套件（415 / 416 条可见测试）上，**bug 是上游两个真实 commit 的反向**。
「harness 只在玩具上跑得通」是这类项目最容易被质疑的一点，这两条就是答案。
源码、修订与信任链见 [`examples/vendor/*/VENDOR.md`](examples/vendor/)。

**4 条过程陷阱**：`trap_loop_retry`（误导性维护笔记 → `step_repetition`）、
`trap_context_pressure`（撑爆上下文 → `CONTEXT_COMPACT` + 信息丢失）、
`trap_fabricate`（可见测试没覆盖验收要求）、`trap_injection`（workspace 里植入诱导指令）。

**用例是生成 + 验证的，不是手写的。** [`scripts/build_cases.py`](scripts/build_cases.py)
从一张数据表生成全部补丁与隐藏测试，并**逐条验证**：可见测试在修复态全过、
注入 bug 后隐藏测试必须失败（否则模型什么都不改也能拿满分）、
打上参考修复后必须通过（否则用例**无解** —— 而「任务无解被记成模型失败」是评测数据集
最隐蔽的污染源）。

### 接第三方轨迹

harness 不只评测自研 agent。任何能产出 OTel GenAI 风格轨迹的系统都能通过一个 adapter
接进来，然后立刻获得全部评测能力 —— 因为 `Trajectory` 是评测器与 agent 之间**唯一**的
交互面，adapter 只需要认识 L0。

`adapters/otel_jsonl.py` 读 OTel GenAI 风格 JSONL，`events/otel.py` 是投影层。
**OTel 命名只活在这两个文件里**（有测试盯着）：`gen_ai.*` 属性至今没有一个达到 Stable，
属性名还在改，所以**内部字段名是稳定契约**，semconv 改名只改投影层。

---

## 项目架构

```
L1  CLI              harness run / trace / report / diff / ci
L2  Orchestration    suite loader → scheduler → aggregator → judge runner
L3  Core             Agent Loop · Pipeline · Budget · Context · Tool Registry
                     · Middleware · Executors
    Evaluators · report/ · adapters/ · store/ · providers/ · testing/
L0  events/  ←  contracts/          叶子层，不 import 任何上层
```

**L0 是叶子层**：`events/` 只能 import 自己；`contracts/` 只能向下 import `events`。
其他层只许向下依赖 L0。真实的依赖图是 **DAG 而不是全序**（`report → store` 成立，
而 `core` 与 `report` 互不可见），所以约束写成逐包的 `forbidden` 契约而不是层序。

### 架构约束是**可执行的**，不是文档承诺

| 约束 | 由谁守 |
|---|---|
| `evaluators/` 绝不 import `core` / `orchestration` / `store` / `providers` | `lint-imports`（CI）+ `tests/test_architecture.py`（纯 `ast`，单测） |
| 每个包的依赖白名单 | 同上，**刻意冗余** |
| 白名单没漏掉任何一个真实存在的包 | `test_the_whitelist_covers_every_layer_on_disk` |
| 上面两份表**等价** | `test_import_linter_mirrors_the_whitelist` |

冗余在这里是特性：架构约束是本项目最容易被无意破坏的东西（一次「就从 evaluators
import 一下 core 省事」就够了）。而「刻意冗余」只有在两份**等价**时才有价值 ——
曾经不是，更弱的那条会在架构已破时**报绿**，所以现在有测试盯着它们等价。

### 两处依赖倒置

`evaluators/` 与 `core/` 零耦合，靠两个住在 L0 的协议实现：

| 协议 | 让评测器能做什么 | 真实实现住在哪 |
|---|---|---|
| `contracts.JudgeClient` | 触发一次 judge run | `orchestration/judge.py::RunBasedJudgeClient` |
| `contracts.CommandRunner` | **在被测工作目录里跑一条命令**（结果级评测） | `orchestration/deps.py::WorkspaceCommandRunner` |

第二个是必需的：判据「代码到底修对没有」只存在于工作目录里，而评测器不许 import `core`
拿执行器 —— 于是把「跑一条命令」也抽成协议。

### 双 Harness 对称

被测 agent（SUT）与评测用的 judge agent **复用同一个 `Run` 类**，只有 `RunSpec` 的取值
不同（`role` / prompt / 工具白名单 / 预算）。回报是直接的：judge 自带完整轨迹 →
**可审计**（能看到它引用了哪段原始 tool output）、**可复现**（cassette 回放）、
**成本可测**（judge 成本与 SUT 成本严格分列），并让**元评测**成为可能 ——
`MetaEvaluator` 消费的就是 judge 自己的轨迹。

```bash
uv run pytest tests/e2e/test_dual_harness.py -v   # 对称性不是靠读代码验证的
```

### 退出码契约

| 码 | 含义 |
|---|---|
| `0` | 通过 |
| `1` | 门禁未达标 |
| `2` | 配置错误（suite 格式、未知评测器、**缺 API key**…） |
| `3` | 预算超限 |
| `4` | 基线缺失 |

**配置错误永远是 2，不是 1。** 任何装配阶段会抛的东西都必须在调度器之前抛
（`RunBuilder._preflight`）—— 否则它会被记成「某条 case 执行失败」，
让该去改配置的人去查门禁。

---

## 文档与开发

| 文档 | 内容 |
|---|---|
| [设计文档](docs/superpowers/specs/2026-09-14-agent-eval-harness-design.md) | 系统设计、架构决策与理由、风险清单 |
| [技术选型](docs/tech-stack.md) | 每个依赖为什么选它、版本约束、核实证据 |
| [实现计划](docs/superpowers/plans/) | 分任务的 TDD 步骤（Part 1/2/3，共 36 个任务） |
| [**已知边界**](docs/known-gaps.md) | **没验证的事、遗留问题、与计划不同之处** |

```bash
uv sync --all-groups         # 建环境并装依赖（Python 3.12）
uv run pytest                # 全部测试，零 LLM 调用
uv run ruff check .          # 风格
uv run pyright               # 类型
uv run lint-imports          # 架构约束

# 改动用例时（会重新生成并逐条验证可解性）
uv run python scripts/build_cases.py
```

**约定**：所有命令走 `uv run`，不要手动 activate。
**测试零成本**：单元测试不得产生 LLM 调用 —— 用 `TrajectoryBuilder` 构造事件序列，
用 `httpx2.MockTransport` 拦截 HTTP；真模型测试标 `@pytest.mark.live`，默认跳过。

**技术栈**：Python 3.12 · uv · pydantic 2.13 · openai 3.x SDK + httpx2 · asyncio（stdlib）·
JSONL + stdlib sqlite3 · typer + rich · jinja2 + ECharts · pytest + hypothesis ·
ruff + pyright + import-linter。选型理由见 [docs/tech-stack.md](docs/tech-stack.md)。
