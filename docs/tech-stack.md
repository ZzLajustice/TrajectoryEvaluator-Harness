# 技术选型文档

- **日期**：2026-09-14
- **适用项目**：agent 过程级评测 harness（Python 3.12）
- **选型原则**：对齐业内标准生态，优先采用被主流评测项目（inspect_ai / deepeval / ragas / lm-evaluation-harness / promptfoo / mlflow / langfuse）实际验证过的方案

> 所有版本号与依赖关系均**实测自 PyPI JSON API 与各项目真实依赖文件**，非凭记忆。核实命令见本文附录。

---

## 0. 贯穿全局的前提：HTTP 栈已分叉

**2026 年最重要的生态变化，它决定了下面 1、2、8 三节的选型。**

| 包 | 当前版本 | 状态 |
|---|---|---|
| `httpx` | 0.28.1（2024-12） | **已停更** |
| `httpx2` | 2.12.0 | pydantic 团队 fork，新栈 |
| `openai` | 3.13.0 | 依赖 `httpx2<3,>=2.7.0` |
| `anthropic` | 1.5.0 | 依赖 `httpx2<3,>=2.0.0` |
| `vcrpy` | 8.3.0 | 新旧两栈都支持 |
| `respx` | 0.23.1 | 仍只支持 `httpx>=0.25.0` — **用不了** |
| `litellm` | 1.100.1 | 仍钉 `httpx<1.0` + `openai<3` — **与 openai 3.x 硬冲突** |
| `instructor` | 1.17.0 | 钉 `openai<4,>=2` — 未跟进 |

**结论：必须选边，且同一虚拟环境内无法共存。**

**我们的选择：站 `httpx2` 这一边。** 理由：官方 SDK（openai / anthropic）都已迁移，这是生态的实际方向；而 litellm / respx / instructor 是"还没跟上"。

**必须固化这条约束**——用 ruff 的 `banned-api` 禁止 `import httpx`，否则半年内一定会有人在某个模块误用旧栈，造成类型错误：

```toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"httpx".msg = "Use httpx2. Mixing httpx and httpx2 breaks the OpenAI SDK."
```

---

## 1. LLM 客户端层

### 决策：自研 provider 抽象 + **官方 SDK**（`openai` 进核心依赖，`anthropic` 进 extra），**不用 litellm**

**参考实现是 inspect_ai**（UK AISI 官方评测框架）。它的做法：

- `src/inspect_ai/model/_providers/` 下每个厂商一个模块：`anthropic.py`、`google.py`、`groq.py`、`deepseek.py`、`moonshot.py`、`openrouter.py`、`vllm.py`、`ollama.py`…
- 共同基类 `OpenAICompatibleAPI`，DeepSeek 就是 `class DeepSeekAPI(OpenAICompatibleAPI)`
- **核心 `requirements.txt` 里没有 `openai`**，厂商 SDK 全在 `requirements-dev.txt`（可插拔）

**为什么不用 litellm**——inspect_ai 的 `deepseek.py` 源码注释直接说明了问题：每家厂商都有必须逐条处理的 quirk，统一抽象层会把这些细节抹平：

```python
DEEPSEEK_EFFORT_MAP = {"minimal": "low", "medium": "high", "xhigh": "max"}
DEEPSEEK_RESPONSE_SCHEMA_WARNING = (
    "DeepSeek does not support schema-enforced structured output, so the "
    'response_schema for {model} is submitted as JSON mode ("json_object"). ...')
DEEPSEEK_TOOL_CHOICE_WARNING = (
    "Forcing tool use ({choice}) is not supported by {model} while thinking is enabled ...")
```

**对评测 harness 而言这是致命的**——评测的核心诉求是**精确复现 payload**，抽象层抹平差异等于让你看不见差异。

连最主打"多厂商"的 promptfoo 也是走官方 SDK 路线（`openai ^7.1.0` + `@anthropic-ai/sdk` + 各厂商 SDK 并置）。

### `openai` SDK 满足三个硬需求

| 需求 | SDK 支持 |
|---|---|
| **多厂商** | `base_url: str \| httpx2.URL \| None` → 覆盖 DeepSeek / 通义 / Moonshot / Groq / vLLM / Ollama |
| **精确控制 payload** | 直接用强类型参数，不走中间层的"参数翻译" |
| **record/replay** | `http_client: httpx2.Client \| None = None` → 唯一的 transport 注入口 |

### 替代品评估

| 方案 | 实测事实 | 判断 |
|---|---|---|
| `litellm` | deps 含 `aiohttp`+`boto3`+`tokenizers`+`tiktoken`+`jinja2`+`jsonschema`+`fastuuid`+`pydantic-settings`，且钉 `openai<3` | ❌ 依赖重 + 锁死旧栈 + 抹平厂商差异 |
| 裸 `httpx2` | — | 仅用于少数需手写 payload 的 provider，不做主力 |
| `instructor` | 钉 `openai<4,>=2`，未跟进 3.x | ❌ 会把项目锁回旧栈 |
| `pydantic-ai` | 技术栈最现代（`httpx2>=2.7`） | ⚠️ 它是 **agent 框架**，我们已有自研 agent loop，职责重叠。可参考其结构化输出手法 |
| 官方 SDK 多套 | promptfoo 同款做法 | ✅ **采用** |

### 风险

- `openai` SDK 3.x 是大版本跃迁（相对 1.x/2.x 有 breaking change），**网络上的中文教程大多是旧写法**，需要看官方 README 而非博客
- 通义 / Moonshot 的 OpenAI 兼容层各有偏差（`response_format`、thinking 字段名不同），**必须像 inspect_ai 那样写 provider 子类，不要指望"改个 base_url 就完事"**

---

## 2. HTTP 录制与回放

### 决策：双层策略

| 层 | 方案 | 用途 |
|---|---|---|
| **日常单测** | `httpx2.MockTransport` + 注入 `http_client=` | 断言发出的 payload，**无文件、最精确** |
| **真机打样** | `vcrpy` + `pytest-recording` | transport 级录制，防上游 API 漂移 |

**`respx` 不可用**——它绑定 `httpx` 类型，PR #317「Support httpx2 via httpcore2」至今 open。作者另发了 `pytest-httpx2` 作为桥接，但底层仍是 respx。
**`pytest-httpx` 直接排除**——硬钉 `httpx==0.28.*`。

### 为什么是 vcrpy

`vcrpy` 在 **transport 层**打补丁（`vcr/stubs/` 下有 `httpx_stubs.py` / `requests_stubs.py` / `aiohttp_stubs.py` / `boto3_stubs.py`），因此与客户端库解耦。它的源码注释明确写了这一点：

> "The httpx module to patch against is passed in (`httpx` or its interoperable fork `httpx2`) rather than imported at module level, so a single stub serves both."

这正是 httpx→httpx2 迁移中它**唯一活下来**的原因。respx 反过来绑定 httpx 类型，于是被 fork 甩下。

**vcrpy 是 transport 级录制，能完整拿到原始请求 body**——满足我们"精确校验 payload"的需求。

### 补充：自研 `ResponsePool`（不是 HTTP cassette）

设计文档里原本规划的自研 `CassetteStore` 其实**不是 HTTP 录制**，而是「同一请求返回 N 个不同样本」的 LLM 响应采样池——`MetaEvaluator` 的 judge consistency 需要它。

**vcrpy 做不到这件事**（它的 `allow_playback_repeats` 是重复同一个响应，不是给不同响应）。

**因此拆成两个独立关注点**：

| 组件 | 归属 | 职责 |
|---|---|---|
| `vcrpy` cassette | `tests/` 的 fixture | HTTP 层录制回放 |
| `ResponsePool`（自研） | `providers/` | 采样一致性测量，非 HTTP 关注点 |

原设计文档中的 `CassetteStore` 更名为 `ResponsePool`，避免与 HTTP cassette 概念混淆。

### 风险

- vcrpy 8.3 有未修回归——重放 `reason_phrase` 为 null 的 cassette 会崩（issue #1028 / PR #1029 open）
- SSE / 流式响应的 cassette 支持较弱；本项目不用流式，影响可控
- cassette 必须配 `filter_headers` 去掉 API key

---

## 3. 数据模型与序列化

| 项 | 选型 | 理由 |
|---|---|---|
| 数据模型 | **`pydantic` 2.13.x** | 判别联合（discriminated union）官方推荐：*"more performant and more predictable than untagged unions"* |
| 配置 | **`pydantic-settings` 2.15.x** | API key / 并发度 / base_url 覆盖，deepeval 同款 |
| YAML | **`PyYAML` 6.0.3** | 事实标准（inspect_ai / lm-eval / mlflow 全用）。**必须 `yaml.safe_load`** |
| 序列化 | **`model_dump_json()`** | pydantic-core 是 Rust 实现，比 `json.dumps(model_dump())` 快很多且类型安全 |
| 高性能 JSON | `orjson`（extra） | 仅用于**非 pydantic 结构 + 大批量 JSONL 写入**。**不混用两套模型体系** |

### pydantic 2.13 的两个关键行为变更

1. **序列化时不再回退尝试其他 union 成员**（#12825）。以前 discriminator 选中的变体序列化失败会静默换一个成员，现在直接报错。**对轨迹存储是好事**——暴露 bug 而非产生脏数据。
2. **`Discriminator` 用 callable 时，序列化阶段也会调用它，输入是 model instance 而非 dict**。只处理 dict 的 callable 会告警或崩；分支必须带 `Tag`，否则 `PydanticUserError`。

### 本项目的具体形态

事件流用判别联合，且**从第一版就带 `type` + `schema_version`**——版本化 schema 必须靠 discriminator 才不会被静默解析成旧版：

```python
EventUnion = Annotated[
    Union[RunStartEvent, TurnStartEvent, ..., RunEndEvent],
    Field(discriminator="type"),
]
```

> 不要用 `ruamel.yaml`（那是给"保留注释的 round-trip 编辑"用的，本项目不需要）。不要用 `msgspec`（更快但会引入第二套模型体系）。

---

## 4. 并发与可靠性

### 4.1 异步原语：**裸 `asyncio`**（不用 anyio）

**调研结论**：inspect_ai 用 `anyio`（核心依赖 `anyio>=4.14.0`，注释明确 *"4.14 fixes asyncio Lock/Semaphore waiter deadlock after cancellation"*），并在 asyncio / trio 双后端跑测试（`--runtrio`）。`openai` / `anthropic` SDK 也依赖 `anyio<5`。

**但本项目选裸 `asyncio`。** 理由：

| 考量 | 判断 |
|---|---|
| **anyio 的核心价值是 trio 兼容** | 那是给**库作者**的——他们无法预知使用者跑在哪个后端。本项目是**应用**，自己决定后端，trio 兼容不产生任何收益 |
| **stdlib 已足够** | Python 3.11+ 已有 `asyncio.TaskGroup` / `asyncio.timeout` / `Semaphore` / `Lock`，覆盖面与 anyio 重叠 |
| **概念负担** | anyio 的认知度低于 asyncio，中文资料少。对一个要讲清楚的教学项目，多一层抽象是净成本 |
| **那条 deadlock 修复** | 触发条件是「cancellation 后仍有 waiter」，我们的用法（`TaskGroup` + `Semaphore` 限流）不命中该模式 |
| **SDK 内部用 anyio 不影响我们** | `openai` / `anthropic` 自己处理自己的异步栈，我们的代码与它们通过 `await` 交互，不共享原语 |

**同步的 `anyio.to_thread.run_sync` 一律用 `asyncio.to_thread` 替代**（`asyncio` 下的等价物，且 `aiosqlite` / stdlib `sqlite3` 场景下语义一致）。

> **注意**：`anyio` 仍会作为 `openai` / `anthropic` 的**传递依赖**出现在环境里，但我们的代码不直接 import 它。若将来要抽成库给他人用，再迁到 anyio 的改动是机械的（`asyncio.Lock` → `anyio.Lock`，`asyncio.to_thread` → `anyio.to_thread.run_sync`），届时再迁不迟。

### 4.2 异步测试：**`pytest-asyncio`**（配 `asyncio_mode = "auto"`）

**与 §4.1 的选择保持一致**：既然运行时用裸 `asyncio`，测试插件也用 `pytest-asyncio`。

| 方案 | 判断 |
|---|---|
| **`pytest-asyncio`** 1.4.x | ✅ **采用**。与 asyncio 代码同栈；`asyncio_mode = "auto"` 下无需给每个测试打 marker；deepeval / langfuse 都用它 |
| `anyio` 自带 pytest 插件 | ❌ 不采用。inspect_ai 用它是因为它同时跑 trio 后端；我们不需要 |

`pyproject.toml`：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"          # async def 测试自动识别，无需 pytestmark
```

> **`auto` 模式的收益**：测试文件里**不需要** `pytestmark = pytest.mark.anyio` 或 `@pytest.mark.asyncio`。少一行样板 × 几十个测试文件。

### 4.3 重试：**`tenacity` 做通用退避，厂商特定的可重试分类自研**

- `tenacity` 9.1.4，inspect_ai 核心依赖含它
- 但 inspect_ai **自己实现了厂商级重试判定**：`_util/retry.py` 的 `report_http_retry`、`openai_classify_retry` / `openai_should_retry`
- 细节值得抄：`HttpxHooks` 把 `Retry-After` 存成**绝对单调时钟截止时间**，注释写明"消费时重算剩余秒数，因为 SDK 之间可能已经做过自己的 backoff"

**照这个模式做**：通用指数退避交给 tenacity，`429 vs 5xx vs throttling` 的分类自己写。

### 4.4 Token 计数：**tiktoken 覆盖不了国内模型，必须双轨**

**问题**：`tiktoken` 只有 OpenAI 的编码（cl100k / o200k）。**对 DeepSeek / 通义 / Moonshot 无效**，用错编码偏差可超 30%，且同族不同代也不通用（Qwen1 vs Qwen2、DeepSeek v1 vs v2 的 vocab 不同）。

**方案**：

| 轨道 | 做法 |
|---|---|
| **实测优先** | 所有 OpenAI 兼容厂商都返回 `usage` 字段 → **轨迹里记录 provider 上报的 usage**，作为计费与统计的权威值 |
| **本地估算** | `tiktoken` 兜底（OpenAI 系 + 粗略近似）；精确估算用 `tokenizers` extra 加载对应 `tokenizer.json` |

**务必把「估算值」与「实测值」两个字段都落盘并标注来源**，否则评测数字不可信。

---

## 5. 存储

### 决策：JSONL（+ zstd）作真相源，SQLite 作索引，DuckDB 只作可选分析层

**这就是业内主流**，与设计文档原方案一致：

| 项目 | 轨迹/日志 | 索引 |
|---|---|---|
| **inspect_ai** | JSONL（`jsonlines`）+ `zstandard` | **stdlib `sqlite3`**（`_util/kvstore.py`） |
| **lm-eval** | 结果 JSON | `sqlitedict`；`archiver` extra = `["jsonlines", "zstandard"]` |
| **mlflow** | SQLAlchemy → SQLite/Postgres | 同左 |

### `aiosqlite` vs stdlib `sqlite3` + `asyncio.to_thread`

**选后者**。证据：**inspect_ai 是重度异步项目，却选了同步 stdlib `sqlite3`**。理由是小 KV 读写不值得引入异步驱动的复杂度，且 SQLite 本身是单写者模型。`aiosqlite` 最后发版 2025-12，维护放缓。

**收益**：零依赖、可预测、与 inspect_ai 同构。在"每次评测只写几百行索引"的场景里，`aiosqlite` 的收益接近于零。

### zstd 压缩默认开启

轨迹文本压缩比很高。inspect_ai 核心依赖有 `zstandard>=0.20.0`，lm-eval 的 archiver extra 也是 `["jsonlines", "zstandard"]`。

### DuckDB 的正确用法

`duckdb` 1.5.5。**不要当主存储**——并发写与 WAL 语义不适合流式追加。正确用法：

```
JSONL 落盘 → duckdb.read_json_auto() 直接查 → 跨 run 聚合分析
          → COPY ... TO 'x.parquet' 归档（极大样本时）
```

**JSONL 始终保留为 source layer。** 对本项目：`JSONL + SQLite 索引 + 可选 DuckDB extra` 是最优解，别一上来堆点击流式 DB。

---

## 6. CLI 与终端输出

### 决策：`typer` + `rich`

| 工具 | 版本 | 采用者 |
|---|---|---|
| `typer` | 0.27.2 | ragas、deepeval（两条评测赛道都选它） |
| `click` | 8.5.0 | inspect_ai 直接用 click |
| `rich` | 15.0.0 | deepeval / ragas / inspect_ai 全用 |

**typer 赢在**：类型注解即 CLI 定义，内置 rich 集成。argparse 在子命令树场景样板代码爆炸；click 是 typer 的底层，只有需要极细粒度控制（动态子命令、参数回调顺序）才直接写。

### 两个必须照抄的版本约束

**1. click 必须排除已知缺陷版本**——inspect_ai 的约束带一串排除：

```
click>=8.1.3,!=8.2.0,!=8.2.2,!=8.3.0,!=8.3.1
```

注释说明 **8.2.2 / 8.3.0 / 8.3.1 破坏了 optional flag values**（pallets/click#3084，8.3.2 才修）。typer 依赖 click，我们会传递性踩到。

**2. rich 避开 15**——rich 15 是 2026-04 的新大版本，生态未全跟上（deepeval 仍钉 `rich>=13.6,<15`）。用 `rich>=14.1,<16`，并避开 14.0.0（inspect_ai 特意排除）。

**不做 TUI**：`textual` 跳过（deepeval 用于 `inspect` extra，inspect_ai 用于 TUI）。

---

## 7. 报告与可视化

### 7.1 模板引擎：`Jinja2`（知道它已进入维护模式仍用它）

`Jinja2` 3.1.6 发布于 2025-03，仓库最后提交 2025-06-14，**此后无活动 → 事实上的维护模式**。

**但它不阻塞使用**——成熟、稳定、生态最广（deepeval core dep、ragas、lm-eval、promptfoo 的 TS 侧 nunjucks 同源）。

替代品 `minijinja` 2.24.0（mitsuhiko 本人的 Rust 实现 Python binding）速度快、零依赖，但自述 *"An experimental Python binding"*，且**模板语法与 Jinja2 有差异**（沙箱 / autoescape 语义不同）。**本项目的 HTML 报告用 Jinja2 更稳**，只有渲染成为性能瓶颈时才考虑 minijinja。

### 7.2 图表库：**ECharts**

实测 min bundle 体积与维护活跃度：

| 库 | 版本 | min bundle | 最后更新 | 判断 |
|---|---|---|---|---|
| Chart.js | 4.5.1 | **203 KB** | **2025-10（11 个月停更）** | ⚠️ 体积最小但半停更 |
| **ECharts** | 6.1.0 | 1095 KB | 2026-05（活跃） | ✅ **采用** |
| Vega-Lite | 6.4.3 | 244 KB **+ 需 vega runtime** | 2026-05 | ❌ 两段运行时，默认从 CDN 取 |
| Recharts | 3 | 579 KB | — | ❌ React 依赖 |
| Plotly.js | 4.1.0 | **4191 KB** | 2026-09（最活跃） | ❌ 体积与离线单文件场景不匹配 |

**选 ECharts 而非 Chart.js 的三个理由**：

1. **评测报告的典型图表 Chart.js 做不了** —— 模型×任务矩阵**热力图**、judge 可靠性**雷达图**、工具调用**桑基图**都需要 ECharts 原生能力或 Chart.js 插件
2. **维护活跃度** —— Chart.js 停在 4.5.1 已 11 个月；ECharts 仍在发版
3. **中文文档与国内生态** —— Apache-2.0，国内看板/报告的事实标准

代价是单文件报告从 ~400KB 涨到 ~1.5MB。**对 CI artifact 和邮件附件完全可接受。**

> 真实项目参考：promptfoo 的 Web UI 用 `chart.js` + `recharts`（因为它是 Web 应用，不是单文件）。inspect_ai 的 viewer 是独立 TS 子模块预构建后随 wheel 分发——**两者都不走单文件内联路线**，所以它们的选择不能直接照搬。

**工程细节**：把 `echarts.min.js` 作为 package data 存进 `src/harness/static/`，渲染时由 Jinja2 读文件**内联进 `<script>`**，避免运行时联网。在 `static/README.md` 里记录 vendored 版本号与升级步骤。

---

## 8. 可观测性

### 决策：只产出 OTLP 兼容的 dict，OTel SDK 进 optional extra

**这个判断被调研强化了**：

1. **截至 2026-07，没有任何一个 `gen_ai.*` 属性达到 Stable**，全部是 Development。规范原文：*"SHOULD NOT be used in production"*、*"MAY be removed without prior notice"*。只有继承自核心约定的 `error.type` / `server.address` / `server.port` 是 Stable。
2. **GenAI 部分没有独立 PyPI 包**，但规范仓库已迁到 `open-telemetry/semantic-conventions-genai`（无 tag release，从 main 发布）。
3. Python 侧官方工具包是 `opentelemetry-util-genai` 1.1b0（**beta**）。

**因此**：harness 的一等产物是自己的 JSONL 轨迹 + 一个投影层，让 span 属性名**可集中改**。OTel SDK / exporter 放 `[project.optional-dependencies] otel`。

### 必须知道的坑（与 §0 的 httpx2 直接相关）

`opentelemetry-instrumentation-httpx` 0.65b0 内部**已包含 httpx2 支持**，导出：

- `HTTPX2ClientInstrumentor`（对应 `HTTPXClientInstrumentor`）
- `SyncOpenTelemetryTransportHttpx2` / `AsyncOpenTelemetryTransportHttpx2`
- `HTTPX2ClientInstrumentor.instrument_client(...)`

**也就是说：`httpx2` 的流量不会出现在 `HTTPXClientInstrumentor` 下，会静默丢 span。**

另外：**PyPI 上的 `opentelemetry-instrumentation-httpx2` 是 0.0.0 的空壳占位包，别装。**

### 关键属性（Development，但已跨 SDK 收敛）

`gen_ai.operation.name`（`chat` / `invoke_agent` / `execute_tool`）· `gen_ai.provider.name`（**`gen_ai.system` 已废弃**）· `gen_ai.request.model` / `gen_ai.response.model` · `gen_ai.usage.input_tokens` / `output_tokens` · `gen_ai.usage.cache_read.input_tokens` / `cache_creation.input_tokens` · `gen_ai.tool.name` / `call.id` · `gen_ai.agent.name` · `gen_ai.conversation.id`

**采样相关属性（`operation.name` / `provider.name` / `request.model` / `server.address`）必须在 span 创建时给出**，否则 tail sampling 失效。

内容捕获靠 opt-in：`gen_ai.input.messages` / `gen_ai.output.messages`，由 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` 控制；稳定性开关是 `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`（**没有 stable 值**）。

### 日志：stdlib `logging`

`structlog` 26.1.0 活跃，但 **inspect_ai 核心依赖里没有它**，用的是 stdlib `getLogger(__name__)`。

**对一个库 + CLI 的 harness，stdlib logging + 自定义 JSON formatter 足够且零依赖**。需要结构化上下文绑定时再上 structlog（放 extra）。

---

## 9. 测试与质量工具链

| 项 | 选型 | 依据 |
|---|---|---|
| 测试框架 | `pytest` 9.x | — |
| 异步测试 | **`anyio` 自带 pytest 插件** | 见 §4.2 |
| 属性测试 | `hypothesis` 6.168+ | 无竞争者 |
| HTTP mock | `httpx2.MockTransport` | 见 §2 |
| 并行测试 | `pytest-xdist` + **`--dist worksteal`** | inspect_ai 注释：默认 `load` 会让 worker 空转，"4 of 10 legs stranded 76-80s on a single worker" |
| Lint / Format | **`ruff` 0.16.x** | **已是事实标准**，inspect_ai / lm-eval / ragas / mlflow / langfuse 全部使用。`black` 仍发版但已无新增采用者 |
| 类型检查 | **`pyright` 作 CI gate** | 见下 |
| 架构约束 | **`import-linter` 2.15** | 见下 |
| pre-commit | `pre-commit` 4.6.x | 标准 |

### 类型检查的 2026 现状（本次调研变化最大的一项）

| 工具 | 状态 | conformance | 关键缺陷 |
|---|---|---|---|
| **`pyright`** 1.1.414 | 成熟 | **96.8%** | 无显著 |
| `Pyrefly` 1.0（Meta） | 已 1.0，CI-ready | ~97.9% | — |
| `ty` 0.0.80（Astral） | 未到 1.0 | ~86.5% | **无 plugin 系统** → pydantic 项目误报 |
| `mypy` 2.3.1 | 2.0 于 2026-05 发布，主打并行 `-n8` | ~77% | 弃用 Python 3.9 目标 |

**决策：`pyright` 作 CI gate。**

理由：**本项目 pydantic 密集，而 `ty` 没有 plugin 系统会产生误报**——直接踩中。ragas 用 pyright（`pyright>=1.1.403`）；langfuse 用 mypy strict。

**`ty` 可作编辑器内快速反馈**，但先别把 CI 交给它（0.0.x）。`mypy` 2.x 的并行检查值得关注，但本项目规模大概率用不上 5x。

### 架构约束：`import-linter`

用 Grimp（Rust）建静态导入图，支持 `forbidden` / `layers` / `independence` / `protected` / `acyclic siblings` / `custom` 契约，配 `pyproject.toml`，跑 `lint-imports`。

**对本项目，「评测器不依赖 core」这条用 `forbidden` 契约一条就够**，这正是它赢的地方。

`tach` 0.35.0 曾一度无人维护，2026-02 由 Gauge 复兴，可视化更好，但更适合需要持续可视化依赖图的大仓。

> **重要**：`import-linter` 是**声明式约束**，而设计文档里规划的 `tests/test_architecture.py` 是**纯 ast 的自研断言**。两者**都保留**——import-linter 给 CI 用（配置即文档），ast 测试给单测用（能在测试里给出精确的违规文件路径）。它们检查的是同一件事，冗余是刻意的。

---

## 10. 包管理与构建

### 10.1 uv 已是事实标准

`uv` 0.12.13。

- **PEP 735 `[dependency-groups]` 已标准化**——langfuse 的 pyproject 实测同时使用 `[dependency-groups]` 和 `uv_build`
- **`pip-tools` 于 2026-03 由 Jazzband 宣布 sunset**
- Open edX 的 OEP-67 **直接把 uv + pyproject 定为后端标准**并归档 OEP-18
- `pip 25.1+` 可读 groups，`pip 26.1+` 可装 PEP 751 `pylock.toml`

**决策：`uv.lock` + `pyproject.toml` + `[dependency-groups]` 完全替代 requirements.txt。**

**供应链防护**（照抄 langfuse）：

```toml
[tool.uv]
exclude-newer = "7 days"   # 只解析发布满 7 天的版本
```

### 10.2 构建后端：`hatchling`

| 后端 | 采用者 |
|---|---|
| `uv_build` | **langfuse**（生产验证） |
| `hatchling` | 广泛 |
| `setuptools` + `setuptools_scm` | inspect_ai（需带 binaries）、lm-eval、ragas |
| `poetry-core` | deepeval |

**决策：`hatchling` + `hatch-vcs`。**

理由：uv 0.12.0 起 `uv init` 默认生成 `uv_build`，它零配置、Rust 直调（跳过 PEP 517 子进程）——**但它不支持动态元数据（`dynamic = ["version"]` 直接失败）、不支持构建 hook、无插件系统**。

本 harness 要打 HTML 模板 + 内联图表 JS（package data），且想用 `hatch-vcs` 从 git tag 取版本——**`uv_build` 这条路走不通**。

> 若接受静态版本号，`uv_build` 完全可用（langfuse 是生产验证）。

---

## 11. 完整依赖清单

```toml
[project]
name = "harness"
requires-python = ">=3.12"
dynamic = ["version"]

dependencies = [
  # ---- LLM 客户端 ----
  # openai 3.x 覆盖 OpenAI 及所有 OpenAI 兼容厂商 (DeepSeek/通义/Moonshot/Groq/vLLM/Ollama)
  # 注意：它依赖 httpx2，与 litellm 的 openai<3 + httpx<1 互斥
  "openai>=3.13,<4",
  "httpx2>=2.12,<3",              # http_client 注入 / MockTransport 必需

  # ---- 数据模型与配置 ----
  "pydantic>=2.13,<3",
  "pydantic-settings>=2.15,<3",
  "python-dotenv>=1.1,<2",

  # ---- 并发与可靠性 ----
  # 异步原语用裸 asyncio（stdlib），不引入 anyio —— 理由见 §4.1
  "tenacity>=9.1.4,<10",
  "tiktoken>=0.14,<1",            # OpenAI 系 + 兜底近似

  # ---- 存储 ----
  "jsonlines>=4,<5",
  "zstandard>=0.24,<1",           # 轨迹压缩

  # ---- 数据格式 ----
  "pyyaml>=6.0.3,<7",             # 只用 yaml.safe_load

  # ---- CLI ----
  "typer>=0.27,<1",
  # typer 传递依赖 click。必须显式排除已知缺陷版本：
  # 8.2.0/8.2.2/8.3.0/8.3.1 破坏 optional flag values (pallets/click#3084)，8.3.2 才修
  "click>=8.3.2",
  "rich>=14.1,<16",               # 避开 14.0.0；生态尚未全支持 15

  # ---- 报告 ----
  "jinja2>=3.1.6,<4",
]

[project.scripts]
harness = "harness.cli:app"

[project.optional-dependencies]
# 额外厂商 SDK（按需，避免核心依赖膨胀）
anthropic = ["anthropic>=1.5,<2"]

# 隔离的逃生舱：与核心 openai>=3 硬冲突，必须独立 venv，不做默认后端
litellm = ["litellm>=1.100,<2"]

# 非 OpenAI tokenizer 精确估算（DeepSeek / Qwen / Moonshot）
tokenizers = ["tokenizers>=0.23,<1"]

# 可观测性：GenAI semconv 全部仍为 Development，故整包可选
otel = [
  "opentelemetry-sdk>=1.44,<2",
  "opentelemetry-exporter-otlp-proto-http>=1.44,<2",
  "opentelemetry-instrumentation-httpx>=0.65b0,<1",  # 内含 HTTPX2ClientInstrumentor
  "opentelemetry-util-genai>=1.1b0,<2",
  # 切勿安装 PyPI 上的 opentelemetry-instrumentation-httpx2（0.0.0 空壳）
]

# 分析层（DuckDB 直查 JSONL，不做主存储）
analytics = ["duckdb>=1.5,<2", "pyarrow>=17,<27"]

# 高性能 JSON 编码（仅在 profiling 显示瓶颈时启用）
fastjson = ["orjson>=3.12,<4"]

[dependency-groups]
dev = [
  "pytest>=8.4,<10",
  "pytest-asyncio>=1.4,<2",       # 配 asyncio_mode="auto"，与 §4.1 的裸 asyncio 一致
  "pytest-cov>=7,<8",
  "pytest-mock>=3.14,<4",
  "pytest-xdist[psutil]>=3.8",    # 配 --dist worksteal
  "pytest-timeout>=2.4,<3",
  "hypothesis>=6.168,<7",
  "vcrpy>=8.3,<9",                # 已支持 httpx2（respx 尚不支持）
  "pytest-recording>=0.13.4,<1",
  "ruff>=0.16,<0.17",
  "pyright>=1.1.414",             # CI gate
  "pre-commit>=4.6,<5",
  "import-linter>=2.15,<3",
]

[build-system]
requires = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"

[tool.hatch.version]
source = "vcs"

[tool.hatch.build.targets.wheel]
packages = ["src/harness"]

# HTML 报告模板 + ECharts (6.1.0 min = 1095KB) 作为 package data
[tool.hatch.build.targets.wheel.force-include]
"src/harness/static" = "harness/static"

[tool.uv]
exclude-newer = "7 days"

[tool.ruff]
line-length = 100
src = ["src"]

[tool.ruff.lint]
select = ["E", "W", "F", "D", "I", "B", "DTZ", "TID251", "PLE", "SIM101"]
ignore = ["E203", "E501", "D10", "D212", "D415", "B006", "B008", "B904", "B905"]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"httpx".msg = "Use httpx2. Mixing httpx and httpx2 breaks the OpenAI SDK."

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"            # 与 §4.2 一致：async def 自动识别，无需 pytestmark
addopts = "-ra --color=yes --dist worksteal"
norecursedirs = ["tests/fixtures/cassettes"]

[tool.pyright]
pythonVersion = "3.12"
typeCheckingMode = "standard"
include = ["src", "tests"]

[tool.importlinter]
root_packages = ["harness"]

[[tool.importlinter.contracts]]
name = "评测器不得依赖 core"
type = "forbidden"
source_modules = ["harness.evaluators"]
forbidden_modules = [
  "harness.core", "harness.orchestration", "harness.providers",
  "harness.store", "harness.report", "harness.adapters", "harness.cli",
]

[[tool.importlinter.contracts]]
name = "分层架构"
type = "layers"
layers = [
  "harness.cli",
  "harness.orchestration",
  "harness.report | harness.adapters | harness.evaluators",
  "harness.core | harness.store | harness.providers",
  "harness.contracts",
  "harness.events",
]
```

> `ruff` 的 `DTZ` 规则集值得注意——**强制时区安全**，轨迹时间戳必须带 tz，这正是我们需要的。

---

## 12. 需要拍板的三个取舍

### ① ~~`anyio` 还是裸 `asyncio`？~~ → **已决定：裸 `asyncio`**

理由见 §4.1。anyio 的核心价值（trio 兼容）是给库作者的，本项目是应用，收益为零；stdlib 3.11+ 的 `TaskGroup` / `timeout` / `Semaphore` 已覆盖需求；且能降低教学项目的概念负担。

不引入 anyio 依赖，`asyncio.to_thread` 替代 `anyio.to_thread.run_sync`。

### ② `pyright` 还是 `mypy`？

**推荐 `pyright`**（pydantic 密集项目的稳妥选择，conformance 96.8% vs mypy 77%）。

**代价**：`mypy` 在招聘市场的辨识度略高。两者可并存（mypy 放本地、pyright 进 CI），但会有一致性维护成本。

### ③ `vcrpy` cassette 与自研 `ResponsePool` 的边界？

**推荐拆分**（见 §2）：vcrpy 管 HTTP 层录制，`ResponsePool` 管 judge consistency 的多样本采样。

**待确认**：若认为引入 vcrpy 的价值不足以抵消一个新的 dev 依赖，可以**只保留 `ResponsePool` + `httpx2.MockTransport`**，完全不用 vcrpy。这会让 dev 依赖更少，但失去"防止上游 API 漂移"的能力。

---

## 附录：核实命令

本文所有版本号与依赖关系可用以下命令复现：

```bash
# 查任意包的当前版本与依赖
curl -s https://pypi.org/pypi/<package>/json | python -c "
import sys,json; d=json.load(sys.stdin); i=d['info']
print(i['name'], i['version']); print([r for r in (i.get('requires_dist') or [])])
"

# 看真实项目的依赖文件
curl -s https://raw.githubusercontent.com/UKGovernmentBEIS/inspect_ai/main/requirements.txt
curl -s https://raw.githubusercontent.com/UKGovernmentBEIS/inspect_ai/main/requirements-dev.txt
```

**关键结论的核实证据**：

| 结论 | 证据来源 |
|---|---|
| httpx2 是 pydantic fork，openai/anthropic 已迁移 | PyPI JSON API 实测 `requires_dist` |
| respx 不支持 httpx2 | `respx 0.23.1` deps 仍为 `httpx>=0.25.0`；PR #317 open |
| vcrpy 支持双栈 | 源码注释 + 测试 extra 含 `httpx2` |
| inspect_ai 用 anyio 不用 pytest-asyncio | `requirements.txt` / `requirements-dev.txt` / `tests/conftest.py` |
| inspect_ai 不用 litellm，用 per-provider 子类 | `src/inspect_ai/model/_providers/` 目录结构 + `deepseek.py` 注释 |
| inspect_ai 用 stdlib sqlite3 不用 aiosqlite | `_util/kvstore.py` |
| GenAI semconv 全部 Development | 规范原文 + 迁移到独立仓库 |
| OTel httpx 插桩含 HTTPX2ClientInstrumentor | `opentelemetry-instrumentation-httpx` 源码 `__init__.py` |
