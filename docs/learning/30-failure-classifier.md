# 任务 30：`FailureClassifier`（12 个失败模式）

> **所属里程碑**：M7 · **前置任务**：23（`BaseEvaluator` 与 `Finding` 契约）、24（`TrajectoryBuilder`） · **代码位置**：`src/harness/evaluators/failure_classify.py`

## 1. 总体目标

结果级指标只能告诉你「这条 case 没通过」，回答不了「为什么没通过」。而「为什么」恰恰是 agent 工程里唯一能行动的信息：失败是因为模型能力不足（换模型）、还是它在同一个调用上打转（改工具返回格式）、还是它不知道何时停止（改 prompt 或 `finish` 契约）、还是上下文被压缩后丢了关键信息（改压缩策略）？四种原因的修复手段完全不同，但在 pass/fail 这一列数字里长得一模一样。

`FailureClassifier` 给失败打上机器可读的标签（`Finding.category`），失败从此可以跨 case、跨 run 聚合：报告能回答「这次评测最主要的失败模式是 `step_repetition`，29 次重复调用中占 7 条用例」。这是从「观测到退化」到「定位到原因」的转折点。

它在系统里是 M7 规则类评测器之首：不依赖 LLM、不依赖网络、不依赖 judge，因此也是报告与 CI 门禁中最可靠的那一层信号。

## 2. 实现流程

1. **先定分类法**——分类法是本任务的设计产物，代码只是它的执行体。12 个模式、每个模式的检测方式（规则/LLM）先固化在模块 docstring 里。
2. 每个模式一个独立检测器，签名统一 `_rule(traj) -> Finding | None`：纯函数、无状态、可单独构造轨迹单测。
3. 用 `_RULES` 元组注册，主入口遍历调用。
4. 收集完成后才做 `expected_modes` 过滤。
5. 规则层零发现时才允许 LLM 兜底。
6. `status` 由 severity 推导：CRITICAL → FAIL，有发现 → WARN，无发现 → PASS。

其中两处顺序不能颠倒。

**过滤必须在检测之后。** 如果按 `expected_modes` 提前跳过不关心的模式，`unexpected_modes` 就永远算不出来——而「意料之外的失败」恰是最有价值的信息：用例声明只考察循环重试，模型却幻觉了工具名，这才是新闻。过滤是报告层的裁剪，不是检测层的开关。

**LLM 兜底必须在规则层全部跑完之后。** 反过来先问 LLM，会同时丢掉三样东西：规则层的零成本优势、`llm_fallback_triggered` 这个「规则够不够用」的度量、以及可复现性。

## 3. 具体技术实现

### 3.1 分类法：诚实裁剪 + 主动补充

分类法来自 MAST（Cemri et al., NeurIPS 2025，标注者间一致性 κ=0.88——κ 高说明类别本身可靠，不是随手拍的）。但 MAST 面向**多智能体**系统。

采用其中单 agent 适用的 8 个（FC1 规范类 5 + FC3 验证类 3），**不采用 FC2「Inter-Agent Misalignment」的 6 个**：`conversation_reset`（没有两个 agent 的会话可重置）、`fail_to_ask_clarification`（没有可询问的对端）、`task_derailment`（定义是偏离与另一个 agent 的约定）、`information_withholding`（定义是 A 有信息、交接给 B 时没说——没有交接对象）、`ignored_other_agent_input`（没有 other agent）、`reasoning_action_mismatch`（定义是对同伴宣称的动作与实际不符）。

关键在措辞：这 6 个不是「罕见」，而是**结构上不可能发生**。区分「罕见」和「不可能」是论证质量的分水岭——前者说明你做了统计，后者说明你读懂了定义。

再补 4 个 MAST 未覆盖的模式，理由同样具体：MAST 假设工具集固定且正确，故没有前两个；MAST 不考虑资源预算，故没有后两个。

| 补充模式 | 为什么 MAST 没有 | 检测规则 |
|---|---|---|
| `hallucinated_tool` | 假设工具集固定且正确 | `error_type == "unknown_tool"` |
| `hallucinated_tool_args` | 同上 | `error_type ∈ {not_found, path_escape}` |
| `ignored_tool_result` | MAST 无此观察 | `ok=False` 后同类调用参数完全未变 |
| `budget_not_converged` | 不考虑资源预算 | `RunStatus.BUDGET_EXCEEDED` |

12 个模式中 **9 个走规则、3 个走 LLM**（`disobey_task_specification` / `disobey_role_specification` / `incorrect_verification`）。

### 3.2 为什么「规则优先」是可信度的保障

三个走 LLM 的模式有共同点：它们问的都是「这个行为算不算违反了规格」——**语义判断**，没有可判定的形式规则。其余 9 个都有确定性的结构信号。

把不确定的部分压到最小面积，评测结论就不被 judge 的不确定性污染：判官今天温度 0、明天温度 1，`pass_rate` 不该跟着抖。规则层覆盖设计文档估计的约 80% 机械失败，这部分逐字节可复现。

这条原则在测试里被写成硬断言，而不是文档里的一句愿望：

```python
traj = TB(run_id="r1").turn().llm_response(text="x").run_end(status="no_finish").build()
FailureClassifier(use_llm_fallback=True).evaluate(traj, EvalContext(judge=SpyJudge()))
assert SpyJudge.calls == 0, "规则命中时不该调 LLM"
```

`calls == 0` 守的是方法论，不是实现细节。

### 3.3 容易踩的坑

**调用指纹必须同时用 `sort_keys=True` 与 `default=str`**：

```python
prints = Counter(f"{c.name}:{json.dumps(c.arguments, sort_keys=True, default=str)}"
                 for c in traj.tool_calls())
```

`sort_keys` 因为不同 provider 回传的 dict 键序不稳定，同样的调用会被算成两个指纹；`default=str` 因为参数里可能有 `Path`、`datetime` 这类非 JSON 原生类型，不兜底直接 TypeError。少任何一个，重复检测都会静默失灵——不是报错，是失灵，更糟。

**空轨迹安全**：用 `max(prints.values(), default=0)`，而不是 `Counter.most_common(1)[0]`。后者在空 counter 上返回空列表，`[0]` 直接 IndexError。「零发现」必须是「没发现问题」，不是「崩了」。

**测试命令匹配是刻意粗糙的**：`_TEST_COMMANDS` 里既有 `"pytest"` 也有 `"make test"`、`"npm test"`，匹配方式是把所有调用参数 `json.dumps` 扁平化后做子串搜索。这不如按 argv 首 token 精确判断「干净」，但后者会漏掉 `bash -c "pytest -q"` 这类包装调用。误差方向偏保守（宁可少报 `premature_termination`，不误伤），与任务 31 的取舍一致。

**severity 与 status 的关系**：任何一条 finding 都会把结果推到 WARN，`MINOR` 也不例外。severity 的细分是给报告排序和人工研判用的，不改变门禁结论——评测器不该悄悄吞掉任何发现。

**LLM 兜底在本任务只是桩**：`metrics["llm_fallback_triggered"] = 1.0` 就是全部实现，语义分类的落地在任务 35 同期。桩要先立对形状，否则接真实 judge 时接口会返工。

## 4. 使用的技术栈简介

本任务**没有引入任何新的第三方库**，这是有意的：规则层的存在理由就是零成本、零网络、可复现，一旦引入 LLM 或重依赖，这个理由就没了。

| 组件 | 来源 | 作用 |
|---|---|---|
| `BaseEvaluator` | 任务 23 | `name` / `subscribes` 约定与 `skipped()` 等 helper |
| `Trajectory` 只读视图 | 任务 3 | `tool_calls()` / `tool_results()` / `compactions()` / `end()` |
| `TrajectoryBuilder` | 任务 24 | 离线构造轨迹，让每条规则都能无 LLM 单测 |
| `Finding` / `EvalResult` / `Severity` | `harness.contracts.results` | pydantic 值对象，`code` 机器可读、`evidence` 指回事件 seq |
| `Counter` + `json` | stdlib | 指纹计数 |

值得一提：正因为事件模型（任务 2）把 `error_type`、`RunEndEvent.status`、`ContextCompactEvent.dropped_message_digests` 定成结构化字段，这里才可能不写一行正则就覆盖 9 个规则。**上游 schema 的质量决定下游评测器的复杂度上限**——若 `error_type` 只是个 message 字符串，此处就得写文本匹配。

## 5. 工程化思想

**「诚实裁剪 + 主动补充」比「照搬」更有说服力。** 采用外部方法论时，说清「哪部分不适用、为什么不适用、缺的怎么补」才是真正的理解。判断标准：你能否给出「这里不适用」的**结构性理由**。说「我们用不上」是把问题推给读者；说「单 agent 下没有交接对象，信息隐瞒在结构上不可能发生」是把问题解决掉。这个模式可迁移到任何「引入外部标准/框架/规范」的场景。

**把不确定性挡在核心指标之外。** 混合了确定性组件与非确定性组件的系统，应让确定性路径先行、非确定性路径只处理残余。更关键的是：这个约束要**能被测试证明**（`SpyJudge.calls == 0`），否则半年后有人为了「提高召回」把 LLM 提前，没有任何东西会拦住他。

**统一的函数签名是可扩展性的具体形态。** `Finding | None` 这个约定让「加一个失败模式」= 加一个函数 + 加一行注册，不动主入口、不改已有测试。开闭原则在实践里长得就是这样，不是抽象类继承树。

**过滤信息时必须区分「裁剪报告」与「关闭检测」。** 前者服务读者（只看关心的），后者破坏证据（永远不知道被过滤掉了什么）。评测、监控、日志系统最常犯的自伤就是把两者混为一谈。
