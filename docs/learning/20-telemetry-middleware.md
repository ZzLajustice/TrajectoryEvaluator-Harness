# 任务 20：`TelemetryMW`

> **所属里程碑**：M4 · **前置任务**：任务 9（洋葱管道）、任务 19（三道守卫中间件已就位） · **代码位置**：`src/harness/core/middleware/telemetry.py`

## 1. 总体目标

`TelemetryMiddleware` 是**过程级评测的全部数据来源**：每一个工具调用经过它时，落一条 `TOOL_RESULT` 事件。它自己不判定对错、不做拦截，只保证一件事——**事实必须留下**。

它的难点全在**异常路径**。设计文档 R3 写死了两条不可违反的规则：

1. **异常路径也必须落事件**。否则评测器看到「有 `TOOL_CALL` 无 `TOOL_RESULT`」的**悬空配对**。
2. **`CancelledError` 必须继续传播**。绝不能写成 `except BaseException`。

### 为什么悬空配对会破坏评测器

假设沙箱在 `read_file` 上炸了，而埋点只写在成功路径上：

| 评测器 | 依赖 | 悬空后失效的方式 |
|---|---|---|
| `GroundingChecker` | `result_for(call_id)` 拿工具原文，比对"模型声称"与"工具实际输出" | 结果缺失时，模型那句"我读到了 X"**既不能被证实也不能被证伪** |
| `EfficiencyAnalyzer` | 统计 `ok=False` 的结果数 | `failed_tool_calls` **漏计最严重的失败**，指标系统性偏低 |
| `TrajectoryMatcher` | 用 `TOOL_CALL` 定序列、用 `TOOL_RESULT` 判成败 | 两者数量对不上，`strict` 模式给出误导性的长度断言 |

更糟的是**这种损坏是沉默的**：轨迹不报错、不标记残缺，只是少了一条事件。崩溃会有人修，数据静默缺失不会——而异常时刻恰好是最值得分析的时刻（沙箱炸了、工具超时、权限被拒），**最该留下的数据最容易丢**。

## 2. 实现流程

1. **先写失败测试**，五条，核心是第二条（异常时也必须有事件且 `ok=False`、`error_type="middleware_error"`）与第三条（`CancelledError` 不被吞、继续抛出）。
2. **跑测试确认失败**（`ModuleNotFoundError`）。
3. **写实现**：`started = time.monotonic()` 必须在 `await nxt(ctx)` **之前**取，否则测不出耗时。
4. **跑全套中间件测试**（`tests/core/middleware/`），确认与任务 19 的三道守卫共存无冲突。
5. Commit。

顺序上不可调换的是**测试 2 与 3**：如果先写实现，最自然的写法是 `result = await nxt(ctx)` 之后统一 emit——它在 happy path 上完全正确，异常路径上静默漏事件，而且**任何"正常跑一遍"的验证都不会发现**。先写异常测试，才能逼出正确的控制流结构。

## 3. 具体技术实现

### 控制流：`except` / `else` 而不是裸 `finally`

```python
try:
    result = await nxt(ctx)
except asyncio.CancelledError:
    raise                       # 必须继续传播
except Exception as exc:
    result = ToolResult(call_id=ctx.call.call_id, name=ctx.call.name, ok=False,
                        error=str(exc), error_type="middleware_error")
    self._emit(ctx, result, started)
    raise
else:
    self._emit(ctx, result, started)
    return result
```

设计文档 R3 的表述是"用 `try/finally` 包 `nxt`"，意图是**任何退出路径都必须落事件**；计划里用的是 `except` / `else`，因为两条路径要 emit 的**内容不同**：正常路径记真实结果，异常路径先合成一个失败结果再记。`finally` 只知道"结束了"，不知道"是怎么结束的"。

异常路径是 **先 emit、后 `raise`**，顺序不能反：一旦抛出，这条栈帧就结束了。

### `CancelledError` 的两层语义

`asyncio.CancelledError` 继承自 `BaseException` 而非 `Exception`，于是：

- `except Exception` **天然不会**捕获它——取消会正常向上传播，这是语言层面给的正确默认值。
- 但**绝不能**为了"更保险"写成 `except BaseException`：那会吞掉取消信号，在并发场景下导致任务悬挂、资源不释放。取消不是错误，是控制流。

代码里仍然显式写了 `except asyncio.CancelledError: raise`。它不改变行为，作用是把这条约束**写在审查者一眼能看到的地方**——"这里为什么不用管取消"是一个必然会被问到的问题，与其口头解释，不如让代码自己说明。tech-stack §4.1 提到 inspect_ai 选用 `anyio>=4.14` 的理由正是"4.14 fixes asyncio Lock/Semaphore waiter deadlock after cancellation"：取消语义在并发原语里是硬约束，不是理论洁癖。

### 记录者不做决策

异常被 emit 之后**重新抛出**，由 `RunContext.invoke_tool`（任务 10）兜底转成 `ToolResult(ok=False, error_type="sandbox_error")`，run 继续。这个分工是刻意的：

- `TelemetryMW` 只知道"这次调用出事了"，它不知道整个 run 的状态，也不该决定 run 要不要继续。
- `RunContext` 是唯一知道全局状态的地方，由它决定降级还是中止。

职责单一带来的直接好处是两边可以独立测试：中间件测"事件一定落了"，loop 测"异常不会崩 run"。

### 其他两个细节

- **`duration_ms` 优先取下游已计的值**：`result.duration_ms or int(...)`。工具自己测得更准（它在更内层），中间件只在没人计时时兜底。
- **被拒绝的结果同样落事件**：`denied_by="permission"` 被原样写进事件。拒绝是结果的一种，不是结果的对立面（任务 19）。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| `anyio` `>=4.14,!=4.15.0,<5` | 取消/超时语义的边界所在；4.15.0 被 mlflow 排除 |
| `asyncio.CancelledError` | 继承自 `BaseException`（不是 `Exception`），是本任务所有控制流的依据 |
| `time.monotonic()` | 单调时钟；计划里所有计时都用它（`latency_ms` / `duration_ms` / `wall_clock`） |
| `pytest` + anyio 自带插件 | `pytestmark = pytest.mark.anyio`（不用 `pytest-asyncio`） |
| 事件模型（任务 2） | `ToolResultEvent` + `EventType.TOOL_RESULT` |

**一个容易混淆的点**：`TelemetryMW` 落的是**内部事件**，不是 OTel span。按设计文档 §3.8，内部字段名才是稳定契约，OTel 命名只活在 `harness/events/otel.py` 这一个投影模块里（`TOOL_CALL..TOOL_RESULT` 在那里才被映射成 `execute_tool` span）。所以 GenAI semconv 改名不会碰到这个文件。

## 5. 工程化思想

**可观测性系统的正确性，等于它漏了多少，而不是它记录了多少。** happy path 上的埋点谁都会写，也不产生额外信息——那部分数据本来就能从返回值推出来。真正有信息量的是异常路径，而它恰好最容易被漏掉。所以**评估一套埋点，要问的不是"它记了什么"，而是"哪些退出路径不会走到它"**。同样的推理适用于审计日志、指标上报、事务日志。

**用不变量描述约束，而不是用代码路径枚举。** "每个 `TOOL_CALL` 恰好对应一个 `TOOL_RESULT`"是一条可以在任何地方验证的不变量；而"记得 try 里 emit、except 里也 emit"是一份需要随分支增加而不断维护的清单。任务 19 的三道守卫给这条不变量加了新的短路出口，而埋点代码不需要任何改动——因为不变量描述的是结果，不是路径。

**不要把"更保险"当成扩大捕获范围的理由。** `except BaseException` 看起来更安全，实际吞掉取消信号，把可控的中止变成悬挂的任务。**捕获范围的每一次放大，都在悄悄改变语义**——当 `except Exception` 已经足够时，多写的那个 `BaseException` 只会带来 bug 和需要注释解释的困惑。

**记录与决策分离，因为它们是两种不同的失败模式。** 埋点写错的代价是丢数据（沉默、事后才发现），决策写错的代价是行为错误（当场可见）。把两者塞进同一个组件，"埋点漏了"和"该不该继续跑"这两类 bug 就会混在一起，各自的测试也无法独立写。
