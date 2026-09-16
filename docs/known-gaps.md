# 已知缺口：没验证的、遗留的、与计划不同的

> 覆盖 M0–M9（任务 1–35）。**每次里程碑完成时更新**，不要攒到最后。
>
> 这份文档的存在理由：计划文档写的是"打算做什么"，代码写的是"做了什么"，
> 而**两者之间的差额**——以及哪些结论其实没有证据支撑——通常没有人记。
> 答辩时最容易被问倒的恰恰是这一块，所以把它写出来比藏起来划算。

---

## 1. 没有验证的事

### 1.1 真模型：传输链路已验证，**成功响应未验证** ⚠️ 最高风险

**接线已经补完**（`credentials.py` + `_build_provider` 分派 + `--model/--provider`），
并用一个**故意无效的 key** 打了一次真实请求。返回：

```
error_type: AuthenticationError
message: Error code: 401 - {'error': {'message':
         'Authentication Fails, Your api key: ****test is invalid', ...}}
```

这一次往返**证实了四件事**：

| 被证实的 | 为什么这个证据成立 |
|---|---|
| DNS / TLS / 端点路径正确 | `https://api.deepseek.com` 真的响应了 |
| 请求体形状被接受 | 否则是 400 而不是 401 |
| `Authorization` 头真的发出去了 | 服务端回显了假 key 的后四位 `****test` |
| 错误映射正确 | `error_type=AuthenticationError`、`retryable=False`、run 终态 `llm_error` |

**仍然没验证的**（都需要一个**有效的** key）：

| 路径 | 现状 |
|---|---|
| **200 响应的解析** | `_from_payload` 的字段映射从未见过真实响应体 |
| 真实模型的工具调用格式 | `tool_calls` 的结构、参数是否被包成字符串、并行调用 |
| 真实 token 用量与计费 | `Usage` 能否被真实响应填满；`cost_usd` 目前恒为 0（无价目表） |
| `--record` / `--replay` 对真实响应 | 只在 fake 上验证过 |
| 上下文压缩的真实触发 | `CONTEXT_COMPACT` 阈值在真实 token 计数下是否合理 |

**影响**：M3 的验收判据"真模型自动修掉 toyrepo 的 bug"仍未达成。
现在的状态是"M3 的代码路径已实现、传输链路已证实、但成功路径未跑过"。

**怎么补**：`cp .env.example .env` 填上 key，然后

```bash
uv run harness run -s examples/deepseek.yaml --evaluate
```

`deepseek.yaml` 的两条 case 是**分层**的：`smoke_text` 只测纯文本往返，
`smoke_toolcall` 加一次工具调用 —— 挂了能定位到不同环节。

**⚠️ 模型名**：`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 弃用，
现行名字是 `deepseek-v4-flash` / `deepseek-v4-pro`（信息来自公开文档检索，
跑之前请对一下厂商当前文档）。名字不对会得到 "Model Not Found"，那至少是清楚的错。

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

### 2.5 其它

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
