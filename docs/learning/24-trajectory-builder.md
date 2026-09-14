# 任务 24：`TrajectoryBuilder`

> **所属里程碑**：M5 · **前置任务**：2（事件模型与 `parse_event`）、3（`Trajectory` 只读视图） · **代码位置**：`src/harness/testing/builder.py`、`src/harness/testing/__init__.py`

## 1. 总体目标

让评测器可以在**没有 LLM、没有网络、没有 agent loop** 的条件下被单测。

评测器的输入是一条轨迹（事件序列），而轨迹在真实系统里只有一个来源：跑完一次 run。如果测试必须这么做，评测器单测就退化成了集成测试——慢（每次几十秒）、贵（每次真金白银）、还不稳定（模型输出不可复现）。更糟的是，**最需要被测的恰恰是真实 run 造不出来的输入**：悬空的 `TOOL_CALL`、缺 `RUN_END` 的截断轨迹、空轨迹。这些"畸形样本"正是评测器健壮性的分水岭，而它们永远无法从一次正常运行中得到。

`TrajectoryBuilder` 就是这层基建：链式 API 构造事件序列，自动维护两件手写最容易错的事——`seq` 单调分配与 `TOOL_CALL` / `TOOL_RESULT` 的 `call_id` 自动配对，同时留一个 `raw_emit()` 出口专门注入畸形序列。

**它作为产品的一部分发布**（`src/harness/testing/`，不是 `tests/`）。设计文档 §7.1 说得很直白：用户写自己的评测器时也能用它，"这正是「评测器可独立单测」这个卖点的兑现"。内部基建与对外扩展点共用一份实现，避免了"我们内部用一个、文档教用户用另一个"的漂移。

## 2. 实现流程

1. `__init__` 立刻分配 `seq=0` 给 `RUN_START`（此时不放进 `_events`），并初始化 `seq` 计数器、`turn` 计数器、`_pending` 配对队列。
2. `turn()`：`turn += 1`，写 `TURN_START`，返回 `self`。
3. `llm_response(...)`：先把 `(name, args[, call_id])` 元组展开成 `ToolCall` 并压入 `_pending`，再写 `LLMResponseEvent`；`finish_reason` 按"有没有 tool_calls"推导（`tool_calls` / `stop`）。
4. `tool_result(...)`：未显式给 `call_id` 时从 `_pending` 里找同名未配对项并消费掉它。
5. `raw_emit(event_type, **fields)`：走 `parse_event()` 构造事件，绕过一切便利逻辑。
6. `run_end(...)` / `build()`：`build()` 把 `RUN_START` 与后续事件合成 `Trajectory`。

顺序上有两处是硬约束：

- **`RUN_START` 在 `__init__` 里就拿到 `seq=0`，且不进 `_events`**。这样 `seq` 严格等于"构造调用的先后顺序"，而 `build()` 再把起点拼回最前面。若改成"把 `RUN_START` 放进 `_events`、最后按类型排序"，`seq` 的分配顺序就与事件逻辑顺序解耦了，测试 `test_seq_is_auto_assigned_and_monotonic` 断言的"`seq == range(len)`"也就不再是构造顺序的自然结果，而是一个需要维护的巧合。
- **`_pending` 在写 `LLMResponseEvent` 之前登记**。配对状态必须先于输出建立，否则后面的 `tool_result()` 无从匹配。

## 3. 具体技术实现

### 自动配对：把"最容易错的两件事"变成不可能错

```python
if call_id is None:
    match = next((c for c in self._pending if c.name == name), None)
    call_id = match.call_id if match else f"unmatched_{self._call_counter}"
    if match:
        self._pending.remove(match)
```

手写轨迹时最容易错的就是 `seq` 忘了递增、`call_id` 抄错——两个错误都不会报错，只会让评测器拿到一条"看起来正常但配对是断的"轨迹，然后产出一个莫名其妙的结论。让 builder 自动维护，等于把"必须小心"变成"不可能出错"。

匹配规则是"**最近的同名未配对调用**"，这是刻意的简化，也带来一个坑：**同名调用并行出现时无法自动配对**。两个 `read_file` 交错（`llm_response` 带两个 call，然后只有一条 `tool_result`）时，自动配到的是列表里第一个，不一定是你想要的那个。要精确控制必须显式传 `call_id`——`test_explicit_call_id_is_respected` 就是这条出口的守卫。

匹配失败时伪造 `unmatched_{n}`，仍然写出一条 `TOOL_RESULT`。这保证了"有结果无调用"的畸形轨迹也能构造出来，而不是抛异常——builder 的职责是**构造**，不是**校验**。

### `raw_emit`：健壮性测试的入口

```python
def raw_emit(self, event_type: EventType, **fields: Any) -> "TrajectoryBuilder":
    from harness.events.types import parse_event
    self._events.append(parse_event({"type": event_type.value, "run_id": self._run_id,
                                     "seq": self._next(), **fields}))
    return self
```

两个设计要点：

1. **走 `parse_event` 而不是直接 `ToolCallEvent(...)`**。任务 2 的事件工厂是 schema 的唯一真相源，builder 也遵守这条——否则测试工具会先于产品代码漂移到一套陈旧的字段名上。
2. **它的存在意义是"能造出合法 API 造不出的东西"**。`test_raw_emit_allows_malformed_sequences` 用它写了一条 `call_id="dangling"` 的 `TOOL_CALL`，然后断言 `traj.result_for("dangling") is None`——这测的是评测器在悬空配对下不崩。任何 validator / parser 的测试工具，都需要这样一个"绕过便利封装"的通道。

### 允许退化输入是一等公民

`build()` 不检查任何前置条件：允许没有 `RUN_END`（`test_builder_can_omit_run_end` 断言 `traj.end() is None`）、允许空轨迹（`test_empty_trajectory_can_be_built` 断言 `traj.events == ()`）。

这不是偷懒，而是设计文档 §7.1 的第四条要求："退化输入 → **绝不抛异常**，转 `ERROR` 并带 `error` 字段"。要让评测器达到这条标准，测试就必须先能造出退化输入。**测试工具的能力边界，决定了被它测试的代码的健壮性上限**——造不出畸形输入，就永远发现不了那些分支。

### 链式调用的返回值

每个方法返回 `self`，让一段轨迹读起来像声明：

```python
(TB(run_id="r1").turn()
   .llm_response(tool_calls=[("read_file", {"path": "a.py"})])
   .tool_result(name="read_file", content="code", ok=True)
   .run_end(status="ok").build())
```

链式 API 的代价是无法给出中间状态的类型帮助，但对"构造一段固定序列"这种场景，可读性收益远大于代价。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| `pydantic` 2.13 判别联合（tech-stack §3） | 事件是 `Field(discriminator="type")` 的联合类型，`parse_event` 按 `type` 分派——所以 `raw_emit` 必须走它而不是手动选类 |
| 无新依赖 | builder 只用 stdlib + `events` / `contracts`，与评测器同一层的依赖约束 |
| `harness/testing` 作为产品代码 | 与 `tests/` 的区别在于它随 wheel 发布：用户写评测器时可以直接 `from harness.testing import TrajectoryBuilder` |
| 任务 3 的 `Trajectory` | 构造时一次建好 `_by_seq` / `_by_call_id` 索引，`result_for(call_id)` 是 O(1)——评测器会高频调它，builder 产出的轨迹因此不必为查询性能担心 |

> 一处需要注意：`harness.testing` 不在 tech-stack §11 的 `[[tool.importlinter.contracts]]` layers 清单里，也不在 `tests/test_architecture.py` 的 `ALLOWED_IMPORTS` 字典里。它遵守 L0 约束（只依赖 `events` / `contracts`）靠的是与 `evaluators` 相同的约定和 code review，而不是自动检查。若要让它同样受约束，把它加进上面任一份清单即可。

## 5. 工程化思想

**把测试工具作为产品的一部分发布，价值是双向放大的。** 对内，它是我们 5 个评测器全部离线测试的基建；对外，它是"用户可以写自己的评测器"这个承诺的基础设施。如果它躺在 `tests/` 里，用户就得自己重写一遍——而重写的那份一定与我们的行为不一致，文档里的例子会先过时。**判断一个内部工具该不该产品化，看它是否服务于一个对外承诺的扩展点**：是，就必须发；不是，才留在 `tests/`。

**测试工具的 API 目标不是"能构造合法输入"，而是"能构造非法输入"。** 一个只能产出合法数据的 builder，会把被测代码的防御分支变成永远无法到达的死代码。`raw_emit` 就是为此存在的出口——它的 API 丑一点无所谓，重要的是它让"悬空 call、缺 `RUN_END`、乱序 seq"从"意外"变成"可复现的测试用例"。

**让约束自动满足，而不是靠人记住。** `seq` 自动分配、`call_id` 自动配对——这两件事恰好是手写时最容易错、错了又最不报错的。凡是"错了不会立刻炸"的机械性工作，都应该由工具承担。可迁移：生成 ID、维护版本号、同步两份清单，都属于这一类。

**自动化的边界要写清楚。** builder 的同名配对是启发式，遇到并行同名调用会挑错。这个限制没有藏起来，而是给了显式 `call_id` 的出口和一条测试。**自动化不完美不是问题，"用户不知道它不完美"才是问题**——尤其是当错误的配对会静默产出一个看似合理的评测结论时。
