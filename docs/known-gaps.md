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
| 限流 / 重试 / 长上下文 | 没遇到 429、没跑过大到触发 `CONTEXT_COMPACT` 的轨迹 |
| 多轮工具调用 | 最多跑到 3 次调用，没测过十几轮的场景 |
| 并行工具调用 | 模型三次都只发一个 `tool_call`，没触发并行分支 |
| **judge 的判定准不准** | 这次两个判定都说 pass，而 SUT 确实做对了 —— 但这是**一轮**。要谈准确率需要带 ground-truth 标签的用例集（M11） |
| **judge 模型的 CLI 覆盖** | `--model/--provider` 只覆盖 SUT；judge 的模型只能在 suite 里改 |
| **成本** | 见 §2.5 —— `cost_usd` 恒为 0，**成本门禁是失效的** |

**影响**：M3 的验收判据"真模型自动修掉 toyrepo 的 bug"仍**未**达成 ——
它需要 toyrepo 用例集（M11），而 `smoke_toolcall` 只是写读一个文件。
但"真模型能不能跑"这件事本身，现在是**有证据的**了。

**⚠️ 模型名**：`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 弃用。
实跑用的是 `deepseek-v4-flash`，厂商接受了这个名字。

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

### 1.6 其它未验证项

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

### 2.5 成本门禁**失效**（真模型下）⚠️ 实跑发现

`Usage.cost_usd` 对真实模型**恒为 0** —— 没有厂商价格表，provider 只搬 token 数。

后果是 `--max-cost` 与 `Budget.max_usd` **都不会触发**。
而它们看起来像在保护你：`max_usd: 0.10` 写在 suite 里，
读的人会以为单条 case 最多花一毛钱。**一个静默失效的安全机制比没有更糟。**

**已做的补救**：设了 `--max-cost` 而实际花了 token 却报 0 成本时，
`RunBuilder` 会发一条 `RuntimeWarning` 明说"这个上限没能生效"。
把静默的洞变成吵闹的洞。

**没做的**：真正的价格表。**我不打算凭记忆写厂商价格** ——
价格会变，写错的价格比 0 更危险（它会给出一个看起来合理的错误数字）。
建议的实现是**可配置**的价目表（suite 或环境变量），而不是硬编码：

```yaml
# 设想形态
pricing:
  deepseek-v4-flash: {input_per_1m: 0.28, output_per_1m: 0.42}
```

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

### 2.7 其它

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

---

## 4. 剩余工作

**M11（任务 36）未做**：

- [ ] `adapters/`（`TrajectorySource` + 1 个第三方适配器）
- [ ] `events/otel.py`（OTel 投影，dual-emit 新旧 token 属性名）
- [ ] `tests/test_architecture.py`（纯 ast，与 `lint-imports` 刻意冗余）
- [ ] 17 条用例集（Track A 13 条 toy repo + Track B 4 条真实仓库）
- [ ] `tests/suites/test_cases_are_solvable.py`（自检：打了 fix.patch 隐藏测试必须过）
- [ ] README

**以及第 1 节列出的验证缺口** —— 其中 1.1（真模型）与 1.2（浏览器看报告）
是最该优先补的两项。
