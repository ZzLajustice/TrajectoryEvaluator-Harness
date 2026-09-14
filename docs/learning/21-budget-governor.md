# 任务 21：`BudgetGovernor` 与 `BudgetMW`

> **所属里程碑**：M4 · **前置任务**：任务 4（`Budget` / `RunStatus` 契约）、任务 9（管道与状态作用域规约）、任务 10（loop 的三个检查点） · **代码位置**：`src/harness/core/budget.py`、`src/harness/core/middleware/budget.py`

## 1. 总体目标

用六个维度（`turns` / `tool_calls` / `input_tokens` / `output_tokens` / `usd` / `wall_clock`）给一次 run 上保险，并保证**超限时 run 以一个独立的终态结束**：

```python
RunStatus.BUDGET_EXCEEDED = "budget_exceeded"   # 独立终态，不是 ERROR
```

为什么必须是独立终态，而不能归到 `LLM_ERROR` 里——这正是本任务的核心：

- **它是评测要观测的失败模式，不是 harness 的故障。** 设计文档 §4.2 的四个"单 agent 专属补充模式"里就有一个「**预算内未收敛** — 耗尽 token/步数预算仍未完成」，检测方式是规则：`RunStatus.BUDGET_EXCEEDED`。这条模式 MAST 里没有（它不考虑资源预算），是我们补的。如果预算耗尽被记成 `LLM_ERROR`，这条检测规则就永远命中不了。
- **它会污染统计口径。** "模型报错率"和"任务未在预算内完成率"是两个独立的指标，一个指向稳定性（provider / 网络 / 配额），一个指向能力（模型是否收敛、任务是否过难、`optimal_steps` 是否估错）。混在一起，两个数字都不能作为决策依据：调 prompt 会看错方向，扩容 provider 也会看错方向。
- **它决定退出码语义。** 任务 11 的 CLI 已经约定：`0` 通过 / `1` 门禁未达标 / `3` **预算超限** —— 预算超限在 CI 里是一个需要单独处理的信号。

## 2. 实现流程

1. **先写失败测试**（7 条）。第一条就是终态断言（`check_turn(2) is RunStatus.BUDGET_EXCEEDED`），最后一条是中间件短路。
2. **跑测试确认失败**（`ModuleNotFoundError`）。
3. **写 `core/budget.py`**：`BudgetExceeded` 异常 → `BudgetView` → `BudgetGovernor`。
4. **写 `core/middleware/budget.py`**：`BudgetMiddleware` 只是"捕获异常 → 返回 `ToolResult`"的薄层，放在 governor 之后写，它就没有任何需要判断的逻辑。
5. **跑测试验证通过**（`tests/core/`）。
6. Commit。

顺序上的关键点是 **3 先于 4**：中间件的职责边界取决于"超限时抛出的是什么"。先写好 `BudgetExceeded`（带 `dimension` / `limit` / `consumed` 三个结构化字段），中间件才能只是搬运它。

## 3. 具体技术实现

### 预算必须在三处强制，缺一不可

| 位置 | 方法 | 覆盖维度 | 形态 |
|---|---|---|---|
| loop 每轮开头 | `check_turn(turn)` | `turns` / `wall_clock` | 返回 `RunStatus \| None` |
| LLM 调用前 | `precheck_llm(est)` | `input_tokens`（估算） | 抛 `BudgetExceeded` |
| LLM 调用后 | `charge_usage(usage)` | `input_tokens` / `output_tokens` / `usd`（实测） | 抛 `BudgetExceeded` |
| 每次工具调用前 | `check_tool_call()` | `tool_calls` | 抛 `BudgetExceeded` |

为什么不能只在一个地方查：**终止性保证来自最里层**。只在 loop 顶部查，一个每轮发起 50 个工具调用、或在单轮里无限重试的 agent 会绕过它。设计文档注释写得很直白——第一处是"终止性保证，防死循环"。

### 同一个 `_check`，两种控制流形状

```python
def _check(self, dimension: str, *, raise_on_exceed: bool) -> bool:
    limit, used = self.limit_of(dimension), self.consumed_of(dimension)
    if used >= limit:
        if raise_on_exceed:
            self._emit_warn(dimension, limit, used, action="deny")
            raise BudgetExceeded(dimension, limit, used)
        return True
    if used >= limit * self._b.warn_at:
        self._emit_warn(dimension, limit, used, action="warn")
    return False
```

`turns` / `wall_clock` 用返回值（`False` = 不在 loop 顶部；`True` = 该停了，由 loop 转成 `RunStatus`），其余用异常。这不是随意的风格差异：

- loop 顶部的检查点只需要**告诉调用方"该结束了"**，结束是一条正常控制流，返回值就够。
- 管道内的检查点需要**立刻中止当前调用链并携带结构化信息**（哪个维度、限额多少、已用多少）。异常天然穿过整个中间件栈把这三样东西送到能处理它的地方；用返回值则要求链路上每一层都记得往下传。

### 累计 vs 赋值：`turns` 与 `tool_calls` 的差别

```python
def check_turn(self, turn):        self._consumed["turns"] = turn       # 赋值
def check_tool_call(self):         self._consumed["tool_calls"] += 1    # 累加
```

`turn` 由 loop 传入**实际轮号**，是权威值，重复调用不会双计；工具调用次数没有外部权威来源，只能自己累加。`wall_clock` 则两者都不是——`consumed_of("wall_clock")` 每次用 `time.monotonic() - started` 现算，因为"已耗时间"本来就不存在累计值。

`remaining()` 用 `max(0.0, ...)` 夹住：超限后剩余量会是负数，而对外展示的"剩余额度"永远不该是负的（测试 `test_remaining_never_goes_negative` 就是钉这个）。

### 告警幂等：边沿触发，而不是电平触发

```python
def _emit_warn(self, dimension, limit, used, *, action):
    key = f"{dimension}:{action}"
    if key in self._warned:
        return
    self._warned.add(key)
    self._emit(BudgetEvent(..., action=action))
```

没有这个 `_warned` 集合，`warn_at=0.8` 之后每一轮都会发一条 warn，而 warn 一旦发出就再也不会消失——事件流会被同一条告警刷满，真正有用的信号（`action="deny"` 那条）反而淹没在里面。**告警的语义是"状态发生了迁移"，不是"当前处于这个状态"。**

### `precheck_llm` 与 `charge_usage`：估算用于决策，实测用于记账

```python
def precheck_llm(self, est_input_tokens: int) -> None:
    if self._consumed["input_tokens"] + est_input_tokens > self._b.max_input_tokens:
        raise BudgetExceeded("input_tokens", ..., self._consumed["input_tokens"] + est_input_tokens)

def charge_usage(self, usage: Usage) -> None:   # provider 上报的实测值
    self._consumed["input_tokens"] += usage.input_tokens
    ...
```

调用前用**估算**拦（防止"这一发就要爆表"），调用后用**实测**记账。这对应 tech-stack §4.4 的双轨 token 计数：provider 上报的 `usage` 是权威值，本地估算只用于触发决策。**估算可以不准（只要保守），记账必须准**——两者的精度要求不同，所以不能用同一套数字。

### BudgetMW：run 级状态，无调用级状态

`BudgetGovernor` 由外部以构造参数注入（`BudgetMiddleware(spec, governor=gov)`），中间件本身**不持有任何调用级可变状态**——这是任务 9 的状态作用域规约：`ToolCallContext` 每次调用新建，`Middleware` 实例在 run 内共享，因此它只能持有 run 级状态。超限时短路返回 `ToolResult(denied_by="budget", error_type="budget_exceeded")`，与任务 19 的守卫同构：拒绝是一个结果。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| 无新依赖 | 只用标准库（`time.monotonic`、`dataclass`）与 `contracts` |
| `Budget` / `RunStatus`（任务 4） | `Budget` 的 7 个字段（含 `warn_at` 默认 0.8）；`RunStatus.BUDGET_EXCEEDED` 在任务 4 就有专门的区分测试 |
| `BudgetEvent` / `EventType.BUDGET_EVENT`（任务 2） | 告警与拒绝都落事件，评测器可订阅 |
| MAST（Cemri et al., NeurIPS 2025，κ=0.88） | 「预算内未收敛」是我们补的第 4 个单 agent 专属模式，MAST 原论文不覆盖 |

设计文档 §4.5 还留了一条与这里直接相关的约束：**judge 的预算必须独立**——`JudgeClient` 每次 judge 创建独立的 `BudgetGovernor`，judge 成本单列到 `EvalResult.usage`，**绝不混进 sut 的 cost**。`judge_cost` 这个指标只有靠这个分离才有意义。本任务把 governor 设计成"一个 run 一个实例、由外部注入"，正是为了让这条约束可以自然满足。

## 5. 工程化思想

**把"独立终态"与"错误"分开，下游分析才准。** 一条运行终止的原因至少有两类：**系统没做到**（超时、崩溃、provider 报错）和**系统正常运转但结果不理想**（预算耗尽未收敛、门禁未达标）。前者指向稳定性，后者指向能力。把它们压成一个 `ERROR`，等于同时毁掉两个指标：稳定性看起来变差（能力问题被算成故障），能力问题变得不可见（淹没在错误里）。**判断一条状态该不该独立，只看一件事：它会不会导向不同的行动。** 如果两种原因会引发不同的修复动作，它们就必须是两个值。同样的推理出现在 HTTP 的 `429` vs `5xx`、编译器诊断的错误 vs 警告。

**预检查与后记账是两种不同的精度需求，不要用同一套数字。** 决策只需要保守（宁可早停，不可超支），记账必须准确（它是成本报表的依据）。**用估算做决策、用实测做记账**，两者各司其职；反过来（用实测做决策 → 拦不住；用估算做记账 → 数字不可信）都会出问题。

**终止性保证必须锚在最内层，而不是最外层。** 只在 loop 顶部检查预算，等于假设"每轮的工作量是有界的"——而 agent 恰恰是那种会在单轮内发几十个工具调用的东西。**任何"总量上限"，都要在每一个会累加它的地方检查。**

**告警要边沿触发。** 持续状态用"当前值"表达，状态迁移才用"事件"表达。把电平当事件发，会得到一份被同一条告警刷满的日志，而真正的那次迁移被埋掉了。
