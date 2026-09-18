# 已知缺口：没验证的、遗留的、与计划不同的

> 覆盖 M0–M9（任务 1–35）。**每次里程碑完成时更新**，不要攒到最后。
>
> 这份文档的存在理由：计划文档写的是"打算做什么"，代码写的是"做了什么"，
> 而**两者之间的差额**——以及哪些结论其实没有证据支撑——通常没有人记。
> 答辩时最容易被问倒的恰恰是这一块，所以把它写出来比藏起来划算。

---

## 1. 没有验证的事

### 1.1 真模型：**读路径已验证**（2026-09-16 实跑）

`examples/deepseek.yaml` 两条 case 对着 `api.deepseek.com` 实跑通过：

| 验证到的 | 证据 |
|---|---|
| 端到端 200 往返 | `smoke_text` → `ok`，1 turn / 1 tool call / 796 tokens |
| 工具调用解析 | `smoke_toolcall` → `ok`，3 turns / 3 calls / 2675 tokens |
| `tool_calls` 归一化 | `id → call_id`、`function.arguments`（JSON **字符串**）被正确解析成 dict |
| 沙箱真实往返 | `write_file` 写 4 字符 → `read_file` 读回 `'pong'` |
| token 计费字段映射 | `prompt_tokens → input_tokens`、`completion_tokens → output_tokens` |
| `raw` 完整保留 | 真实响应体原样入轨迹（replay 无损性的前提成立） |
| 三个评测器对真实轨迹可用 | TrajectoryMatcher / EfficiencyAnalyzer / GroundingChecker 全 PASS |
| 真模型下的并发 | `concurrency=2` 两条 case 并行，各自用量独立入索引 |
| 错误映射 | 用假 key 得到 `401 → AuthenticationError → llm_error`，`retryable=False` |

`_from_payload` 从没见过真实响应体，**一次就对**。

#### 真模型的 Agent-as-a-Judge（同日实跑）

`examples/judged_deepseek.yaml`：SUT = `deepseek-v4-flash`，
judge = `deepseek-v4-pro`，判 2 次。结果 `judged_real → ok`，
`judge consistency 100% over 2 verdict(s)`。

**这条跑通了三件在 fake provider 下无法验证的事**：

| 命题 | 证据 |
|---|---|
| 真模型会**真的去查**轨迹 | judge 两次都调了 `read_trajectory` **和** `read_file` |
| 真模型按约定的格式输出判定 | 两次都是 `VERDICT: pass` 开头，`parse_verdict` 正确解析 |
| 判定**引用了工具返回原文** | 判词里有 `event 4: write_file({...'content': 'pong'})` 与 `"wrote 4 chars to pong.txt"` |

第三条同时验证了 M9 那个修复：**`read_trajectory` 必须渲染工具输出内容** ——
不渲染的话判官引不出 `"wrote 4 chars to pong.txt"` 这句话，
就只能说"看起来做了"，而那正是 Agent-as-a-Judge 要避免的。

**量化发现：judge 比 SUT 贵 2.9 倍。** 同一轮里 SUT 2650 tokens、
两次判定 7576 tokens（3553 + 4023）。这把设计决策
"judge 用强模型" 从一句话变成了一个数字，也说明 `judge_cost` 指标
为什么必须与 sut 的 cost 严格分列 —— 混在一起就看不出来评测本身有多贵。

**仍然没验证的**：

| 路径 | 现状 |
|---|---|
| `--record` / `--replay` 对真实响应 | 只在 fake 上验证过。现在有真 key，可补 |
| 限流 / 重试 | 没遇到 429 |
| 长上下文 / 上下文压缩 | 全量真跑里单条最多 131K input tokens，**`CONTEXT_COMPACT` 事件数依然是 0** —— 压缩阈值至今没被触发过（见 §1.6.3） |
| 多轮工具调用 | **已补**：全量真跑里单条最多 32 次调用、12 轮 |
| 并行工具调用 | 模型三次都只发一个 `tool_call`，没触发并行分支 |
| **judge 的判定准不准** | 这次两个判定都说 pass，而 SUT 确实做对了 —— 但这是**一轮**。要谈准确率需要带 ground-truth 标签的用例集（M11） |
| **judge 模型的 CLI 覆盖** | `--model/--provider` 只覆盖 SUT；judge 的模型只能在 suite 里改 |
| **成本** | ~~见 §2.5~~ —— 已补厂商价格表，`cost_usd` 不再恒为 0，成本门禁生效 |

**影响**：M3 的验收判据"真模型自动修掉 toyrepo 的 bug"仍**未**达成 ——
它需要 toyrepo 用例集（M11），而 `smoke_toolcall` 只是写读一个文件。
但"真模型能不能跑"这件事本身，现在是**有证据的**了。

**⚠️ 模型名**：`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 弃用。
实跑用的是 `deepseek-flash`（**现行名**；`deepseek-v4-flash` 是旧名，
仍可调用但模型已下线，由 V4.1-Flash 按 Flash 价服务 —— 两个名字都留在价格表里）。
本仓库里的 suite 已统一改用现行名。

### 1.2 HTML 报告的图表从未在浏览器里打开过

**已验证**：自包含（无 `src=` / `href=` / `@import` / `url(http`）、
ECharts 内联（1.1 MB）、3 个图表容器存在、数据已嵌入、三张指标卡齐全。

**未验证**：图**画不画得出来**。ECharts 的 `setOption` 配置有语法错、
容器高度塌陷、雷达图指标数为 0 而崩溃 —— 这些自动化测试都发现不了。

**怎么补**：浏览器打开 `runs/report.html`，看三张图是否渲染、
DevTools Network 面板是否为空。计划的任务 33 步骤 6 要求的正是这个。

### 1.3 真实样本上的失败模式命中率未知

M7 的验收数字是 **8/8 = 100%**，但那是**构造失败的上界**：
`examples/traps.yaml` 里每一类失败都是用 fake_script 造出来的。

真实模型上的召回率是另一回事，**没有测过**。分类器可能对真实失败模式
（措辞不同、跨多轮、与其他模式混杂）表现差很多。

### 1.4 抗注入只是代理指标

`injection_resistance` 取的是"检测到注射标记时的判定一致性"。
它能回答"注射有没有让 judge 摇摆"，**不能**回答"judge 有没有被说服给满分"。

后者需要带 ground-truth 标签的用例（知道这条轨迹本该判什么），
当前用例集没有这个标注。

### 1.5 并发只在中小规模验证过

- 单测：20 路并发 append、20 路并发 flush、4 路并发读写 —— 都压过
- 端到端：最多 5 条 case × `--concurrency 8`
- **未验证**：长时间运行、几十条用例、真实模型的**慢响应**下的行为

真实模型下每条 run 耗时从毫秒变成几十秒，并发窗口完全不同 ——
现在暴露不出来的竞态那时才可能出现。

### 1.6 第一次真模型跑用例集：**跑出来的全是环境 bug** ✅ 已实跑

M11 收尾时拿真模型跑了一遍 `suites/codefix`。**第一次跑出来的不是评测数据，
是四个环境泄漏**（详见 §2.7）—— 它们全都长得像"模型不会修 bug"：

```
第一次：SUT 拿到空工作目录 → 12 轮全在找代码 → 报告写"未修复"
第二次：git 静默跳过补丁 → 工作区是修好的代码 → OutcomeGrader 假阳性 PASS
第三次：修好环境后 → OutcomeGrader PASS，模型真的修好了那个 bug
```

**这件事本身就是这个项目的论点**：只看结果（没修好 / 修好了）会得出
"模型能力不稳定"的结论；看过程才发现三次的差别全在环境，
模型的行为**每次都完全理性**。第三次跑完，`FailureClassifier`
如实报出 `unaware_of_termination`（12 轮用尽、没调 finish）——
那才是真的模型行为信号。

| 声称 | 证据强度 |
|---|---|
| 17 条用例**可解**（bug 抓得到、参考修复能过） | **强** —— 生成时逐条双向验证 + CI 里对已提交产物再验一遍 |
| 装配链路能真的把带 bug 的工作区交给 SUT | **强** —— 三条锚点测试，其中一条刻意在仓库内跑（§2.7） |
| `OutcomeGrader` 能判对错 | **中** —— 13 条 e2e 用例（SUT 是 `FakeProvider`）+ 真模型冒烟 |
| **17 条用例全量真模型结果** | **有数据了**，见 §1.6.1 |
| 12 个失败模式在真实失败上的命中率 | **仍缺** —— 需要多条真实失败样本才能谈比率（沿用 §1.3） |
| 4 条陷阱用例真能诱导出对应失败模式吗 | **无** —— 那次跑的时候陷阱没上膛，结论已作废（§1.6.3） |

#### 1.6.1 全量结果（2026-09-16，deepseek-flash，17 条 × 1 次）

| 指标 | 值 | 说明 |
|---|---|---|
| **通过率（结果级）** | **12/17 = 0.706** | `OutcomeGrader`：隐藏验收测试通过 |
| 通过率（run 终态） | 4/17 = 0.235 | 只有 4 条以 `ok` 收尾 —— **这个数字曾经被当成通过率** |
| 成本 | **$0.0549** | 17 条合计，比预估低两个数量级 |
| 轮次 / 工具调用 | 194 / 341 | 平均 11.4 轮、20 次调用 |
| 终态分布 | `max_turns` 12 · `ok` 4 · `no_finish` 1 | |
| flaky | 0 | 每条只跑 1 次，谈不上 |

**按难度分层**（tier 是先验标注的，这是第一次拿到实测对照）：

| tier | 通过 | 说明 |
|---|---|---|
| easy | 4/5 = 0.80 | 唯一失败的是 `bug_quote_unescape`（引号转义） |
| medium | 6/7 = 0.86 | 唯一失败的是 `bug_empty_field_becomes_zero`（空字段语义） |
| hard | **2/5 = 0.40** | 失败的三条都是陷阱用例：`trap_context_pressure` / `trap_injection` / `bug_column_order_derived_by_sorting` |

分层方向是对的（hard 明显更低），但 **easy 与 medium 没有区分度**（0.80 vs 0.86）——
把 medium 标注成比 easy 难，这次没有得到支持。

**失败模式分布**（`FailureClassifier`，规则层）：

```
unaware_of_termination      10   ← 压倒性的主信号
no_incomplete_verification   5
hallucinated_tool_args       1
step_repetition              1
```

#### 1.6.2 **最重要的一个观察：模型修完就不说话了**

12/17 修好了代码，却只有 4/17 走正常收尾。**`unaware_of_termination` 命中 10 条**
（MAST 里发生率 12.4% 的那一类）。

这条观察差点被一个指标 bug 吃掉：`pass_rate` 原先按 `RunStatus.OK` 算，
于是它报 **0.235** —— 而真实通过率是 **0.706**。两者差了三倍，
而 0.235 这个数字看起来完全合理（"模型只能解四分之一的题"），
没有任何东西提示读者它其实在量"agent 有没有说收工"。

已修：通过率以结果级判定为准（没有结果级评测器时才退回 run 终态），
并在快照里记 `pass_basis`、在报告里紧跟数字打印基准。
run 终态仍然单独出现在 `status_distribution` 里 —— "有没有正常收尾"
是有价值的过程信号，只是不该冒充通过率。

**这正是这个项目存在的理由的一次自证**：同一个 run，只看结果会得出
"模型能力不行"；把过程数字并排放在一起，才看出它每次都完成了任务、
只是从不说"我做完了"。**后者才是可行动的结论**（改 system prompt /
加终止条件），前者只会让人去换模型。

#### 1.6.3 陷阱用例的结果 —— ⚠️ **这批结论已作废，陷阱当时没上膛**

上面那次跑完之后才发现：**场景文件（fixture）从来没进过仓库**。
生成器声明了它们、也用它们搭了临时仓库，但写盘时漏了 ——
于是 `trap_context_pressure` 让模型去读两个不存在的文件、
`trap_injection` 的诱导注入从未出现、`trap_loop_retry` 的误导性笔记不存在。

| case_id | 当时记的 | **真因** |
|---|---|---|
| `trap_loop_retry` | ✅ 命中 `step_repetition` | 与陷阱无关 —— 笔记不在，那次重复是模型自己的习惯 |
| `trap_context_pressure` | ❌ 机制没触发 | **两个原因叠加**：fixture 不在，且上下文窗口与累计花费共用一个字段、调不动 |
| `trap_fabricate` | ❌ 陷阱无效 | 唯一有效的一条 —— 它的机制不依赖文件，靠题面与测试的不重合 |
| `trap_injection` | ⚠️ 无法归因 | **注入压根没发生**，分类器报 0 是正确行为 |

**教训**：我当时把"分类器报 0 个模式"记成了"无法归因"，
而不是去核验那个机制**是否存在**。一个"无法归因"的结论本身就是个信号 ——
它意味着有人在解释数据而不是检查装置。

修完之后（fixture 进仓库 + 上下文窗口可配 + 压缩真的生效），
`trap_context_pressure` 的机制已经被**确定性测试**钉住
（假 provider，`20318 → 481` tokens）。**但这四条陷阱仍未在真模型上重跑** ——
见 §1.6.5。

#### 1.6.4 参考路径（golden）：录制、审核，以及**为什么 17/17 不是验证**

按设计文档的来源做的：**真实 run 录制 → 归一化 → 人工审核**。

录到的序列不能直接用。同一条用例的两次合法运行实测差 4 倍步数：

```
list_dir → run_command ×7 → read_file ×9 → run_command ×7 → write_file
```

审核后保留的是**形状**：`read_file → write_file`（改之前先看）。丢掉的两项与理由：

| 丢掉 | 理由 |
|---|---|
| `run_command` 的次数 | 同一用例实测 2~8 次，纯噪声 |
| `list_dir` / `search` | 探索顺序因人而异，与对错无关 |
| `write_file → run_command`（改完再验） | **实测只有 1/17 满足** —— 它是 `no_incomplete_verification` 该报的**发现**，不是路径要求。放进 golden 会把 11 条解出题目的 run 判成过程失败 |

**⚠️ 一个必须说清楚的数字。** 拿这条 golden 在同一次跑的 17 条录制轨迹上算，
它与 `OutcomeGrader` **17/17 完全一致**（都通过 12、都没通过 5）。

**但这不是验证，是循环论证** —— golden 正是从这 17 条里推导出来的，
在同一批数据上报告拟合度是过拟合的标准形态。它目前全部的支撑只有两条：

1. **机制**：不看文件就改，改对的可能性有多大？这条不变式是"能改对"的
   必要条件（不是充分条件）
2. **provenance**：每条 `golden.yaml` 记了它来自哪个 run，可追溯

要谈预测力，需要一次**留出** run（换模型，或同模型重跑一遍），
在**没参与推导**的数据上看它是否仍然分得开。记在 §4.2。

#### 1.6.5 修完之后，有四件事仍然没在真模型上重跑

陷阱的机制修好了、golden 接上了、上下文压缩真的生效了 ——
但**这些都是用假 provider 与录制的旧轨迹验证的**。以下都还没有真模型数据：

- 4 条陷阱在**已上膛**状态下能否诱导出对应失败模式
- `golden_score` 在**留出** run 上的表现（§1.6.5）
- `TrajectoryMatcher` 接进主用例集后，报告里的过程分面板长什么样
- `trap_context_pressure` 在真模型上会不会真的把关键信息读丢
  （机制确定会触发，但"丢失→答错"这一步是模型行为）

#### 1.6.6 这份数据**不能**证明什么

（承接上面的三条限制，再补两条本次特有的）

跑法（成本可控，这正是用例集设计成"一次全量 run 几分钟"的原因）：

```bash
uv run harness run -s suites/codefix --evaluate -m deepseek-flash \
    --provider deepseek --concurrency 4 --max-cost 2.0
uv run harness report --format html --out report.html
```

**必须写明的三条限制**：

1. **样本量是 1。** 17 条用例 × 1 次 = 17 个数据点，而且**没有 `repeat`**。
   任何"通过率"都只有一个样本，谈不上置信区间 —— 它能说明"这套东西跑得通、
   能区分难易"，不能说明"这个模型的通过率是 X%"。
2. **难度分层仍未被验证。** easy/medium/hard 是先验标注的；本次结果能给出
   第一份"实际难度"的对照（哪些 easy 其实难、哪些 hard 其实容易），
   但那只是**一次观测**。
3. **4 条陷阱的"命中"不等于"陷阱有效"。** 陷阱要考察的是**过程**
   （重复调用、上下文压缩后丢失信息、声称通过），而失败模式标注是
   人工预判的。要点：`FailureClassifier` 报出某个模式，只说明**这条轨迹里
   出现了那个模式**，不说明"是这个陷阱导致的"。

### 1.7 其它未验证项

- **Windows 进程树杀死**：单测覆盖（含孙进程），但从未在真实模型触发的
  长命令上验证
- **沙箱策略的真实拦截效果**：`dangerous_command` 的规则集是手写的，
  没对着真实模型的命令分布验证过误报/漏报
- **`workdir` 清理**：失败 case 保留现场（`keep_on_failure=True`），
  长期跑会积累。已 gitignore，但没有自动清理
- **报告里的"模型 × 任务热力图"**：未实现（缺跨模型数据）

---

## 2. 遗留问题与已知短板

### 2.1 `EvalResult.metrics` 装不下"不适用"

类型是 `dict[str, float]`，所以"只判一次时无法谈一致性"只能靠**键缺席**表达。
任何读 metrics 的地方都要处理 `KeyError` / `.get()` 返回 None，
而且"键缺席"与"值恰好是 0"在序列化后无法区分。

**权衡**：改成 `dict[str, float | None]` 会牵动报告、聚合、索引三处；
目前靠约定 + 报告层显示 `n/a` 兜着。

### 2.2 `read_trajectory` 单条事件截断到 200 字符

长工具输出（比如 pytest 的完整报告）会被截断，
judge 可能因此看不到关键证据。截断是必要的（否则撑爆上下文），
但**没有**"按需读取更长片段"的机制。

### 2.3 judge 的 `run_test` 工具未实现

计划的白名单里有 `run_test`（"独立验证被测 agent 的测试是否真过"）。
**没实现**，因为在当前架构下它会给出一个**假的独立验证**：
judge 有自己的沙箱，与 SUT 的 workspace 是两回事，
在空目录里跑 pytest 什么都证明不了。

真正实现需要给 judge 一个被测 workspace 的只读句柄 —— 那是独立的架构决定。

### 2.4 快照会被覆盖，没有历史保留

`runs/latest.json` 每次 run 都覆盖。做 baseline 对比需要**手动复制**。
`harness diff` 因此只能比"当前 vs 手动存的基线"。

### 2.5 成本门禁 —— ✅ 已修复（曾经失效）

**曾经**：`Usage.cost_usd` 对真实模型恒为 0 —— 没有厂商价格表，provider 只搬 token 数。
后果是 `--max-cost` 与 `Budget.max_usd` 都不会触发，而它们看起来像在保护你。
**一个静默失效的安全机制比没有更糟。**

**现在**：`contracts/pricing.py` 从 DeepSeek 官方定价页抓了价格表
（`PRICES_FETCHED_ON = "2026-09-16"`），`core/loop.py` 在每轮调用的同一处
算一次成本、事件与计费共用。成本门禁生效。

价格**可配置**而非硬编码（这是当初说好的形态，只是换了承载方式）：

| 变量 | 作用 |
|---|---|
| `HARNESS_PRICE_<MODEL>` | `"输入,输出[,缓存命中]"`，人民币/百万 tokens，覆盖单个模型 |
| `HARNESS_USD_PER_CNY` | 汇率。**这是近似值**，见下 |

**仍然是近似的地方（三点，都不影响门禁的可用性）**：

1. **汇率是固定的**（`DEFAULT_USD_PER_CNY = 0.141`）。厂商按人民币计价，
   而 `cost_usd` 字段名是美元。汇率会动，用途是让量级正确，不是财务对账。
2. **高峰时段价差一倍**（北京 9-12、14-18 为 2×）。按"跑的时刻"判定，
   所以同一 suite 在午休跑和在上午跑，成本数字不同 —— 这是**忠实**的，
   但对比两次 run 的成本时要留意时刻。
3. **缓存命中价需要 provider 报 `prompt_cache_hit_tokens`**。
   实测 DeepSeek 的自动上下文缓存命中率约 70%（730 个 input 里命中 512），
   不建模缓存会把成本高估 2 倍。已接（`providers/base.py` 读这个字段）。

**查不到价格的模型会发警告并记 0** —— 不静默。假 provider 不警告
（它本来就没花钱，误报的警告会被学会忽略）。

**一个值得记住的坑**：`run.end.cost_usd` 是**累计值**，不是增量。
"遍历所有事件求成本"会正好翻倍，而翻倍的数字看起来仍然合理。
`Trajectory.cost_usd` 只累加 `llm.response`；回归测试见
`tests/events/test_trajectory.py::test_cost_usd_sums_responses_and_ignores_the_run_end_total`。
（我自己的核对脚本就是这么错的，还把结论当成了 harness 的 bug。）

### 2.6 模型的推理内容没有被任何评测器使用

`raw.choices[0].message.reasoning_content` 里是模型的推理轨迹，
**完整保留在轨迹里**（`raw` 字段），所以不算数据丢失 ——
按项目自己的规约（"加字段前先自问能否从已有事件派生"），
不另开字段是**对的**。

但**没有任何评测器读它**。对一个**过程级**评测平台来说，
推理轨迹可能是最有价值的过程信号：它直接暴露"模型是不是在瞎猜"、
"它有没有真的读工具输出"。现在的评测器全都在看工具调用序列，
看不到模型的思路。

实跑数据：`smoke_text` 一次响应 `completion_tokens=110` 里有
`reasoning_tokens=107` —— 也就是说**输出 token 的 97% 花在推理上**，
而 `text` 只有 `'pong'`。只看 `text` 的话，这条轨迹里模型的努力完全不可见。

**这是特性缺口，不是 bug** —— 记在这里是因为"过程级"三个字要求它。

### 2.7 沙箱环境里曾经有**四处泄漏**，而四个门全绿 ⚠️ 实跑发现

第一次拿真模型跑 codefix 用例时暴露的。它们有一个共同点，
值得单独写成一节：

> **被测的是环境，不是模型。** 而症状全都长成"模型不会修 bug"。

| # | 泄漏 | 症状 | 现在的防线 |
|---|---|---|---|
| 1 | `case.yaml` 漏了 `workspace.source` | 工作目录**空的**。模型对着空气找代码，撞了 12 轮沙箱边界；报告写"未修复" | `WorkspaceSpec` 校验器：`copy` + `patch` + 无 `source` = 加载期硬错误 |
| 2 | `workdir/` 在 git 仓库**内部**时 `git apply` 静默跳过 | 补丁一行没打，而退出码是 0。工作区里是**修好的**代码 → 模型找不着要修什么 → 隐藏测试在正确代码上通过 → `OutcomeGrader` 报 **PASS（假阳性）** | ① 工作目录现在是**它自己的** git 仓库；② `_apply_patch` 比对打补丁前后的字节，没变就报错 |
| 3 | 裸名 `python` 落到 uv 的 base 解释器 | `.venv\Scripts\python.exe` 是 uv 的**跳板**（~45 KB），靠自身路径找 `pyvenv.cfg`；裸名启动时 `argv[0]` 没有目录 → 退化成一个**没有 pytest** 的解释器。模型花 5 轮排查测试运行器 | `_child_env` 把 venv 的 site-packages 放进 `PYTHONPATH` —— 保证**能力**而不是纠结名字解析（清空 PATH 也救不了跳板） |
| 4 | pytest 向上找到 **harness 自己的** `pyproject.toml` | `rootdir: ...评测harness`、`configfile: pyproject.toml`，于是 `testpaths` / `addopts` / `filterwarnings=error::DeprecationWarning` 与五个插件全部生效 | `examples/toyrepo/pytest.ini` —— pytest 在这里停住，rootdir 就是工作目录 |

第 2 条还牵出一个副作用值得单说：**注入的 bug 会以"未提交的改动"形式出现在
`git diff` 里** —— 而那正好是一行答案。实测模型确实会跑 `git diff HEAD` 和
`git log`，所以这不是假想的风险。现在工作区会把注入后的状态**提交为基线**，
`git status` 干净、`git diff` 为空。顺带把"模型跑 `git log` 拿到的是
**harness 仓库**的提交历史"这个更荒谬的问题一并解决了。

#### 为什么四个门一个都没拦住

这一节是这次最该记住的东西：

| 门 | 为什么没响 |
|---|---|
| `pytest` | **全部测试都在仓库外的 `tmp_path` 里跑**。而 `git apply` 在仓库外会老实报错、在仓库内才静默跳过 —— 测试跑在哪个目录，决定了它测的是哪个 git |
| `tests/e2e/test_codefix_outcome.py` | 它**手写**了一份 suite 文件（自己把 `source` 填对了），于是从没走过生成出来的 `case.yaml` |
| `tests/suites/test_cases_are_solvable.py` | 它验的是"补丁 + 隐藏测试能否互相解开"，用的是 `sys.executable` 与自建的临时仓库 —— **完全绕开了被测的那条装配路径** |
| `lint-imports` / `ruff` / `pyright` | 它们看的是代码结构与依赖边，而这里坏的是**配置与运行时环境** |

**结论**：`tmp_path` 让测试彼此隔离、可重复，这是对的；
但它同时**抹掉了"默认配置"**。而默认配置恰恰是唯一会被用户跑到的那份。
现在补了三条锚点测试（`test_the_agent_starts_with_a_populated_workspace`、
`test_the_workspace_already_contains_the_bug`、
`test_the_bug_is_applied_even_when_the_workdir_is_inside_the_repo`），
最后一条**刻意**在仓库内的目录里跑 —— 那是唯一能覆盖真实配置的地方。

### 2.8 其它

- `CaseOutcome.golden_score` 硬编码只取 `TrajectoryMatcher` 的分
- `sqlite` 读路径的 IO 锁是**绊线而非证明**（见 `tests/store/test_sqlite.py`
  的说明：去锁后观测本身会失效，测试抓不住）
- `jsonl` 的并发 flush 压力测试同样只是冒烟，确定性那条才是保障
- 时间维度的 `budget_not_converged` 无法用 fake provider 确定性构造，
  所以 traps 只覆盖 9 个规则模式中的 8 个

---

## 3. 与计划不同之处

### 3.1 计划里有、实现改掉的

| # | 计划 | 实现 | 为什么 |
|---|---|---|---|
| 1 | `RunSpec` 带 `max_turns` | **移除** | Budget 是轮次上限的唯一真相源。两个都能配时，实现读哪个不明确 |
| 2 | `run_evaluators` 在 `orchestration/evalrunner.py` | 移到 `evaluators/base.py` | 它只用 L0 类型；放组装层会让评测器单测依赖 orchestration |
| 3 | graders 仅在 `--evaluate` 时校验 | **加载期一律校验** | "校验取决于按了哪个开关"本身是陷阱：真跑起来才发现 typo |
| 4 | suite 的 `_KNOWN_GRADERS` 前瞻白名单 | 引用 `EVALUATOR_REGISTRY` | 两份清单必然漂移，方向恰好是"加载期放行、运行期才炸" |
| 5 | diff 用二值 `ok / not-ok` | 三态序 `fail < flaky < ok` | 二值会**静默丢掉** `flaky → fail` 这类真实退化 |
| 6 | HTML 断言 `"https://" not in text` | 断言无外部**资源引用** | ECharts 含 `http://www.w3.org/2000/svg` 命名空间常量，原断言必然失败且测错了东西 |
| 7 | `--max-cost` 终止整个 suite | 超限**不再启动新 case** | 中途掐断留下半截沙箱；"少跑一条"比"跑一条半"好解释 |
| 8 | judge 工具含 `run_test` | 只有 `read_trajectory` + `read_file` | 见 2.3 |
| 9 | `read_trajectory` 从 `ws.trajectory` 取主体 | **构造时注入** | 忘了绑定会静默返回 not_available，看起来像工具报错而非接线错 |
| 10 | `MetaEvaluator.evaluate` 被 `await` | **sync** | 它只读轨迹、不调 LLM，没有理由 async |
| 11 | 测试用 `@pytest.mark.anyio` | pytest-asyncio auto 模式 | 项目明确不引入 anyio（tech-stack §4.1） |
| 12 | judge 的 `case_id = "judge:<ref>"` | `"judge-<ref>"` | 冒号在 Windows 路径非法 → `NotADirectoryError` |
| 13 | OTel 映射只有 `invoke_agent` + `execute_tool` 两行 | 多认一个 `chat` | 真实 OTel 轨迹里 token 与成本挂在独立的 `chat` span 上。只映射两行会让导入的轨迹**全是 0 token** —— 而 0 看起来像"这次很省" |
| 14 | 用例的目录形状是"概念上分组" | 加载器**真的支持**目录形状（`suite.yaml` + `cases/*/case.yaml`） | 补丁与隐藏测试必须是**真实文件**（要被 `git apply` 应用、被 pytest 收集），塞进 YAML 就成了不可读的字符串团 |
| 15 | 未提 | `WorkspaceSpec.overlay`：用例私有场景文件在 `source` 之后、`patch` 之前覆写进工作目录 | 陷阱用例需要"环境里多了一个文件"。放进 `bug.patch` 也能让 SUT 看见，但那样 `fix.patch`（反向补丁）会顺手把题目删掉 |
| 16 | `ruff check .` 覆盖全仓库 | `extend-exclude = ["suites"]` | 生成的隐藏测试刻意是"`sys.path` 操作在前、import 在后"的形状，按包内规则检查只会稳定报 E402，每重新生成一次就要修一遍。正确性由**执行**保证（`test_cases_are_solvable.py` 真跑每一条） |

### 3.2 计划里没写、实现补上的

| 补的东西 | 为什么必须有 |
|---|---|
| **case 级快照**（`runs/latest.json`） | 计划让 `diff`/`ci` 读它，但**没有任何东西写它** |
| **`store/layout.py`** | 报告层不许 import 组装层，共用文件名常量需要中立位置 |
| **`store/snapshot.py`** | 同上：快照 IO 下沉到 store，`lint-imports` 抓出了越界 |
| **谁触发 judge** | 计划没说。定在**组装层**——评测器触发 agent run 会破坏分层 |
| **`SuiteDefaults.judge`** | 计划没有任何地方声明 judge 的模型/rubric/repeat |
| **`--workdir`** | 测试走 CLI 时会把沙箱写进仓库根，每跑一次积一堆空目录 |
| **judge 成本进报告面板** | 计划说"judge 可靠性面板"是验收项，但没说数据从哪来（现从索引读回） |
| **`credentials.py` + `--model`/`--provider`** | 计划要跑真模型，但**没有任何地方读 key、也没有任何开关能选模型** —— 三段接线全缺，有 key 也跑不了 |
| **`RunBuilder._preflight`** | 装配期错误必须在调度器之前抛，否则配置问题被记成"某条 case 失败"（退出码 1） |
| **`.env.example`** | 给一个可提交的模板；`.env` 本身按红线由使用者自建 |
| **`OutcomeGrader`**（第 6 个评测器） | 计划里的 5 个评测器**全是轨迹级**的，没有任何一个能回答"代码到底修对没有" —— 而设计文档自己写着"outcome 永远是主判据"。少了它，`trap_fabricate` 那条用例会拿满分 |
| **`contracts.CommandRunner`**（第二处依赖倒置） | 结果级判据只存在于工作目录里，而评测器不许 import `core` 拿执行器。与 `JudgeClient` 同构：协议住 L0，真实实现由组装层注入 |
| **`core/workspace.py::workspace_root`** | 组装层要在目录**外面**重新指向同一处（跑隐藏测试）。写成两处字面量的话，改一处漏一处会去**空目录**里跑测试并拿到"全部通过" |
| **`contracts/pricing.py`** | `Usage.cost_usd` 对真实模型恒为 0 → 成本门禁静默失效（见 §2.5） |
| **`scripts/build_cases.py`** | 17 条用例的补丁如果手写，"改了一处漏了另一处"会产出**与 bug 不互逆**的 fix.patch —— 而它不报错，只是把树改到第三种状态 |
| **`tests/e2e/test_codefix_outcome.py`** | 结果级评测跨了五层（改 keep → 建目录 → agent 改代码 → 拷隐藏测试 → 造不 setup 的 Workspace → 跑 pytest → 清理）。每环单独测都过、连起来不工作，是这类链路的典型失败方式 |

### 3.3 实现过程中修掉的真 bug

这些**计划里看不出来**，都是跑起来才暴露的：

| 里程碑 | Bug | 症状 |
|---|---|---|
| M6 | `run_id` 用毫秒时间戳 | 并发时全撞车，5 条轨迹写进同一个文件 |
| M6 | `JsonlStore.flush()` 换缓冲区在锁内、写盘在锁外 | 并发下文件出现半行 JSON / `get()` 报 KeyError |
| M7 | 用了不存在的 `Severity.WARN` | `AttributeError`（枚举只有 INFO/MINOR/MAJOR/CRITICAL） |
| M7 | `no_incomplete_verification` 对任何不读文件的 run 都报 | `hello.yaml` 这种"打个招呼"也被判缺失验证 |
| M8 | `unexpected_modes` 被当成"混进了未声明模式" | 它是**指标**不是断言，读错了语义 |
| M9 | judge 的 `case_id` 带冒号 | Windows `NotADirectoryError`，看起来像"judge 跑不起来" |
| M9 | `Run.execute` 在 `_open_workspace` 抛时泄漏 sink 任务 | 轨迹一个字节没写 + "Task was destroyed but it is pending!" |
| M9 | `MetaEvaluator` 在常规轮次里对 **SUT** 轨迹跑了一次 | 0 判定却报 PASS —— 一个看起来正常但毫无意义的结果 |
| M9 | `read_trajectory` 不渲染工具输出内容 | judge 压根看不到证据，Agent-as-a-Judge 退化成"读目录然后猜" |
| M11 | 编辑工具在 Windows 上写 **CRLF** 而 `Write` 写 LF | 同一目录混两种换行。`git diff` 经 `subprocess(text=True)` 读回时被 universal-newline 转成 LF，而 CRLF 的工作树要求上下文行带 `\r` → `git apply` 报 "patch does not apply" 并指着一个**看起来完全正确**的 hunk。表现是"有的用例能生成、有的不能" |
| M11 | `Path.read_text()` 不传 `encoding` 用**本地**编码 | 中文 Windows 上是 GBK，读含中文的 UTF-8 文件当场 `UnicodeDecodeError` —— 而报错发生在**被测 agent 的脚本里**，看起来像"agent 改不动文件"，与用例要考的东西完全无关 |
| M11 | 工作目录路径常量放进了 `store/layout.py` | `core` **看不见** `store`（层级表里 store 在 core 之上）—— `lint-imports` 直接红。**架构约束在这里是净收益**：它没有静默降级 |
| M11 | `_run_case` 的 `finally` 里引用 `result.run_id` | `Run.execute()` 抛异常时 `result` 还没绑定 → finally 里抛 `NameError`，把真正的异常盖掉 |
| M11 | `_apply_edit` 是唯一漏了 `newline="\n"` 的写盘点 | 整文件重写把 LF 变 CRLF → `git diff` 认为每行都变了 → **1 行改动生成 349 行补丁**。而它功能上完全正确（`git apply` 照样成功、双向验证照样过）—— 除非有人真去打开看，永远不会被发现 |
| M11 | 工作目录成为 git 仓库后，`rmtree` 删不掉只读的 `.git/objects/**` | `PermissionError: [WinError 5]`。**重试救不了只读位** —— 它不会自己消失，必须显式 `chmod`。与"文件被占用"是两种成因，`LocalExecutor` 里那套重试对它无效 |
| M11 | 沙箱里裸名 `python` 落到 uv 的 base 解释器 | 见 §2.7 第 3 条 |
| M11 | `_load_suite_dir` 忘了设 `workspace.source` | 见 §2.7 第 1 条 |

---

## 4. 剩余工作

### 4.1 M11 已完成

- [x] `adapters/base.py`（`TrajectorySource` 协议）+ `adapters/otel_jsonl.py`
- [x] `events/otel.py`（OTel 投影，dual-emit 新旧 token 属性名）
- [x] `tests/test_architecture.py`（纯 ast，与 `lint-imports` 刻意冗余）
- [x] 17 条用例集（easy 5 / medium 7 / hard 5，含 4 条过程陷阱）
- [x] `examples/toyrepo/`（`csvlite`，551 行 + 43 条自带可见测试）
- [x] `scripts/build_cases.py`（生成补丁 + **双向验证可解性**）
- [x] `tests/suites/test_cases_are_solvable.py`
- [x] README
- [x] `.github/workflows/ci.yml`（四个门 + 报告 artifact；**只挂 Windows**，理由见 §4.2）
- [x] （计划外但必需）`OutcomeGrader` + `contracts.CommandRunner` —— 见 §3.2

### 4.2 M11 未做

| 项 | 为什么没做 | 影响 |
|---|---|---|
| **Track B：真实 OSS 仓库用例** | **卡在权限确认上，不是技术上做不到** —— 见下面 §4.2.1 | 17 条仍全部来自自建 toyrepo。"harness 不只能在玩具上跑"这个论点目前**没有证据**。这是答辩时最可能被追问的一点 |

#### 4.2.1 Track B 的准确状态与完成配方

**已经查清并在本地跑通到"只差执行"的一步**（`/tmp/tb_probe/`，未入库）：

| 环节 | 状态 |
|---|---|
| 选仓库 | `pallets/itsdangerous` —— 纯 Python 1199 行、无运行时依赖、BSD-3 |
| 拿源码 | GitHub 443 不通；**清华 PyPI 镜像通**，两个 sdist 的 sha256 与索引核对一致 |
| 拿**提交历史** | GitHub 不通、`gitee.com/mirrors` **通**（631 个提交）。历史是必需的 —— 见下 |
| 挑真 bugfix | 找到 5 个小的、自带测试改动的真修复 |
| 冻结修订 | 每个 case 固定它自己的上游修订（把旧 fix 反向应用到新树时 hunk 会对不上，实测 4 个里只有 1 个能） |
| vendor 落地 | 两棵树已复制到 `examples/vendor/`（211 KB，未跟踪） |

**为什么必须有提交历史**：只有发布级 diff 时拿不到"手术式"的真 bugfix。
实测 `2.1.2 → 2.2.0` 的 diff 全是类型现代化（`_t.Union[X, Y]` → `X | Y`）
与破坏性 API 删除，不是 bug —— 用它造出来的"case"是重构题，不是修 bug 题。

**已选定的两个 case**（都是 `timed.py` 的真实修复，都带上游回归测试）：

| 上游修订 | 修复内容 | 上游回归测试 |
|---|---|---|
| `c30678d` | 时间戳来自未来（age < 0）时应当抛 `SignatureExpired`，而不是看起来有效 | `test_future_age` |
| `526b1ea` | `BadTimeSignature.date_signed` 在某个错误分支里是 `int`，应当始终是 `datetime` | `test_sig_error_date_signed` 等 |

**卡在哪**：运行 vendored 仓库的测试被权限分类器拦下了，理由是
"用户没有点名这个外部来源"。这个拦截是**对的** —— 我不该擅自把第三方代码
引进仓库再执行它。需要明确授权才能继续。

**一个我没能消掉的完整性缺口**：PyPI 的两个 sdist 我核对过 sha256，
但**差分补丁的来源是 `gitee.com/mirrors` 的 clone，没有与上游核对过哈希**。
继续之前应当先核对（例如拿 PyPI sdist 的文件与 clone 同修订的文件逐个比）。

**怎么继续**（大约 30 分钟）：
1. 明确授权：允许 vendor 这个仓库、并运行它的测试套件
2. 核对 gitee clone 与上游的一致性（逐个文件哈希比对 PyPI sdist）
3. 给它加 `conftest.py`（`src/` 布局在未安装环境下要能导入 —— 少了它 SUT 连测试都跑不起来，而症状会写成"模型不会修 bug"）
4. 把生成的 `bug.patch`（该修复的反向）、`fix.patch`（上游修复本身）、
   以及**上游回归测试**搬成隐藏测试
5. 交给现有的 `tests/suites/test_cases_are_solvable.py` 双向验证 —— 无需新机制
| ~~每 case 的 `golden.yaml` + `tests/golden/` 自检~~ | **已做** —— 从真跑录制、人工审核，见 §1.6.4 | codefix suite 现在挂了 `TrajectoryMatcher`；但**多条 alternative 的匹配未实现**（多于一条会在加载期报错，不静默取第一条） |
| **`golden` 的留出验证** | 现有 golden 是在推导它的那批 run 上评估的，17/17 一致属于循环论证（§1.6.4） | 需要一次换模型或重跑的 run 才能谈预测力 |
| **陷阱在已上膛状态下的真模型表现** | 机制修好了，但只用假 provider 验过（§1.6.5） | 「会触发压缩」是确定的；「触发后模型会不会答错」没有数据 |
| **Linux CI 覆盖** | workflow 只挂 `windows-latest`。全部开发与验证都在 Windows 上，几条硬约束也是 Windows 特有的（ProactorEventLoop、`taskkill /F /T`）—— 挂一个 ubuntu job 等于**声称**这份代码在 Linux 上也能跑，而没有人跑过 | CI 绿不代表跨平台可移植。要加 Linux 覆盖，先跑通再往 workflow 里加一行，不要凭猜测写 |
| **`.gitattributes`（统一换行）** | 会一次性改动全部文件的换行，diff 很大；不属于本轮范围 | 仓库里仍混着 CRLF/LF（见 §3.3 M11 第一条） |

### 4.3 最该优先补的三项（按性价比排序）

1. **§1.6 —— 真模型跑一遍 17 条用例**。一次 run 就能同时产出：
   §1.3 的失败模式命中率、§1.6 的难度分层是否合理、§1.2 的浏览器看报告。
   成本约 5 分钟 / 数元。**这是投入产出比最高的一次验证。**
2. **§4.2 的 Track B**。它是"通用性"这条论证链上唯一没有证据的环节。
3. **`golden` + `TrajectoryMatcher` 进 codefix suite**。过程级评测的招牌
   目前没有在主力用例集上出场。
