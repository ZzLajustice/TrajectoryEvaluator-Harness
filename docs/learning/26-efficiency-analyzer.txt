# 任务 26：`EfficiencyAnalyzer`

> **所属里程碑**：M5 · **前置任务**：23（`BaseEvaluator` 与四态契约）、24（`TrajectoryBuilder`） · **代码位置**：`src/harness/evaluators/efficiency.py`

## 1. 总体目标

把"贵不贵、绕不绕"变成数字——**纯规则，零 LLM 成本**。

这是 outcome 级指标最典型的盲区：一条用例通过了，但它用了 4 倍于最优的步数、把同一个文件读了三遍、还在一堆失败调用里泡了半程。这些在 `pass_rate` 里统统看不见。而它们不是学术问题——设计文档 §4.2 引的 MAST 数据里，**step repetition 占全部失败的 15.7%**，是单一占比最高的机械失败模式之一。更关键的是，这类失败模式的判定根本不需要 LLM：它是"跨多次调用做统计"，规则层能算得又快又准。

所以这个评测器承担的是"规则层覆盖约 80% 机械失败"里的效率那部分（设计文档 §4.3 列了它的指标清单：步数比、token、成本、延迟、冗余调用率、无效循环计数）。本任务实现其中可由轨迹直接算出的一批：`step_ratio`、`redundant_calls`、`failed_tool_calls`，加一条 `tool_calls` 计数。

## 2. 实现流程

1. 收集 `traj.tool_calls()`，并把 `tool_results()` 按 `call_id` 建索引（后续查"这次调用成功了没"是 O(1)）。
2. 算冗余指纹：`name + json.dumps(arguments, sort_keys=True)` 计数，冗余数 = Σ(count - 1)。
3. 数失败调用：`not r.ok` 的结果个数。
4. `step_ratio = len(calls) / optimal_steps`（`optimal_steps` 为 0 时兜底 0.0，不做除法）。
5. 按阈值产 `Finding`：冗余 ≥ 3、ratio > 2.0、失败数过半。
6. `status` 由 findings 决定，`score = 1 / ratio`，六个指标进 `metrics`。

顺序上有一条要紧：**先算完全部指标，再产 findings**。所有阈值判断基于同一批中间结果，重复计算既不必要又会让"哪个数才是判定依据"变得含糊；而 `metrics` 无论有没有 findings 都要返回——报告要用它画成本-质量散点图（任务 33），分布信息不能只在报警时才出现。

## 3. 具体技术实现

### 指纹：`sort_keys=True` 不是可选的

```python
fingerprints = Counter(
    f"{c.name}:{json.dumps(c.arguments, sort_keys=True, default=str)}" for c in calls)
```

两个细节都是必须的：

- **`sort_keys=True`**。dict 的键序在构造路径不同时不保证一致，`{"a": 1, "b": 2}` 与 `{"b": 2, "a": 1}` 是同一次调用，不排序就会数成两次——冗余率直接翻倍。
- **`default=str`**。参数里可能有 `Path`、`datetime` 这类不可 JSON 序列化的值，`json.dumps` 默认会抛 `TypeError`。评测器不允许因为参数类型而崩（退化输入绝不抛异常）。

### 冗余的定义刻意简单

只看工具名 + 参数，**不看返回内容**。所以"读同一个文件两次、第二次内容已经变了"仍然算冗余。这是有意的：设计文档 §4.3 把它定位为纯规则指标，简单定义换来的是可复现、无歧义、零成本。要更聪明的判断（例如"结果变了就不算冗余"）应该做成另一个评测器，而不是把这个指标改得需要解释。

### 分母保护：`0` 与"没配"要有区别

```python
step_ratio = (len(calls) / self.optimal_steps) if self.optimal_steps else 0.0
```

`optimal_steps` 默认 0（用例没声明最优步数），此时直接兜底为 0.0——测试 `test_zero_optimal_steps_does_not_divide_by_zero` 断言这种情况下绝不能是 `ERROR`。**代价**要说清楚：`metrics["step_ratio"] == 0.0` 无法区分"没配最优步数"与"配了 0"，两者在 metrics 里长得一样。这是**有意的降级**：宁可少报一个指标，也不要让一次除零炸掉整轮评测。派生影响是 `score` 也变成 `None`（因为 `1/0` 无意义）——报告层需要容忍 `score is None`。

### 阈值地板：小样本不要下重判

```python
if failed and failed >= max(2, len(results) // 2):
```

`failed` 为真（0 和 1 都是真值）不够，还要"至少 2 次失败且过半"。这条地板是为了压住小样本噪声：一次调用失败就被判"高失败率"，等于让偶发抖动变成告警。**任何比例型阈值在小分母下都会失去意义**，加地板是最省事的处理。

### 状态由 findings 决定，而不是由 ratio 决定

```python
status = EvalStatus.WARN if has_step_finding or findings else EvalStatus.PASS
```

注意 `or findings` 这一段：只要产出了任意一条 finding（包括 severity 为 `MINOR` 的冗余），状态就是 `WARN` 而不是 `PASS`。`has_step_finding` 只是让"超标步数"这个更严重的信号在逻辑上显式，不改变结果。这样设计的好处是"有没有发现"与"状态"一一对应，不会出现"有 warning 但状态是 PASS"的别扭组合。

### 一处实现时要收口的地方

构造函数收了 `redundancy_tolerance: int = 2`，但实现里判定用的是模块级常量 `_REDUNDANT_WARN = 3`，**这个参数从头到尾没有被读取**。计划给出的骨架里两者并存，属于实现时必须收口的点：要么把阈值判断改成用 `self.redundancy_tolerance`，要么删掉这个参数。留着它比没有更糟——它是一个"看起来可配"的谎言，使用者调了没反应，会先怀疑自己的配置写错了。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| `collections.Counter`（stdlib） | 指纹统计；`sum(n - 1 for n in counts if n > 1)` 就是冗余总数，不需要手写字典累加 |
| `json`（stdlib） | 参数指纹的规范化序列化；`sort_keys` + `default=str` 是让它可用的两个开关 |
| **零新依赖、零 LLM 成本** | 与 `TrajectoryMatcher` 同属规则层。设计文档 §4.2 的分工是"规则层覆盖约 80% 机械失败，LLM 只处理语义级残余"——效率问题全在规则层这一侧 |
| `pydantic` 2.13（tech-stack §3） | `EvalResult.metrics: dict[str, float]` 承载连续值；`Finding.data` 承载临时诊断字段（不污染事件 schema） |
| 任务 3 的 `Trajectory` 索引 | `result_for(call_id)` 是 O(1)，使得"遍历 calls 查各自结果"是线性而非平方 |

## 5. 工程化思想

**指标与判定必须分列。** `EvalResult` 同时带 `metrics`（连续值：ratio、计数）与 `status` / `findings`（离散判定）。把两者压成一个"效率分"会同时丢掉两样东西：分布信息（报告画不出散点、看不出趋势）和判定的可解释性（调阈值等于改分值）。可迁移到任何打分系统——**原始测量**与**阈值化结论**是两种消费者（图表 vs 门禁），合成一个数就要服务两种互相冲突的需求。

**凡是比例，先问分母能不能为 0。** `optimal_steps` 来自用例配置，完全可能缺失或为 0。KPI 计算、增长率、命中率、覆盖率——所有 `a / b` 的写法都要先回答"b 为 0 时返回什么"。答案通常是"返回 None 并显式标注不可用"，而不是"返回 0"或"抛异常"：0 会被当成"最差"，异常会炸掉整轮评测。

**退化输入的正确处理是降级，不是报错。** 空轨迹、`optimal_steps=0`、参数不可序列化——三条路径都不产生 `ERROR`。这继承自任务 23 的契约（退化输入绝不抛异常）。反之，如果这里抛了异常，调度器兜底成 `ERROR`，报告里就会出现一条"评测器坏了"，而真正的原因是"用例没配最优步数"——**错误的归因方向**。

**配置项一旦暴露就必须被使用。** `redundancy_tolerance` 的悬空是个小 bug，但它揭示了一类更普遍的问题：**未被读取的配置参数是负价值**。使用者按文档调它、没看到行为变化、开始怀疑别的地方，排查成本远超删掉这个参数。可迁移的检查方式——在实现收尾时，对每个构造参数问一句"它在哪一行被读了"，答不上来的就删掉或接上。

**用规则解决的问题不要留给 LLM。** 冗余检测、步数比这类统计问题的答案是确定的、可复现的、免费的。把这部分交给 LLM 只会引入成本、延迟和不确定性，而结果还不如 `Counter` 准。**先穷尽规则层的表达力，再考虑语义层**——这个次序决定了整个评测结果的成本结构和可信度。
