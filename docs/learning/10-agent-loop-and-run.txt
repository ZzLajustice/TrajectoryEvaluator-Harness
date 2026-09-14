# 任务 10：agent loop 与 `Run`

> **所属里程碑**：M1 · **前置任务**：任务 2/3（事件模型与 `Trajectory`）、任务 4（`RunSpec`）、任务 5（协议与值对象）、任务 6（`FakeProvider`）、任务 7（`ToolRegistry` 与 `finish`）、任务 8（`JsonlStore`）、任务 9（洋葱管道） · **代码位置**：`src/harness/core/loop.py`、`src/harness/core/run.py`

## 1. 总体目标

把前九个任务攒下的零件装成一台能跑一次的机器：**事件模型 + 只读轨迹 + `RunSpec` + 协议 + 脚本化 provider + 工具注册表 + store + 中间件管道 → agent loop + `Run`**。

**为什么需要 `Run` 这个类，而不是一个 `run_agent()` 函数。** `Run` 是**双 Harness 对称的落点**：被测 agent（`sut`）、judge agent、语义兜底时的 classifier，用的是**同一个类、同一条代码路径**，差异全部来自 `RunSpec` 的取值（设计文档 §3.3）。这条对称性是整个项目的卖点之一——「能看到 judge 的 Run 轨迹，且其事件结构与 sut 同构」是验收标准第 3 条。一旦 loop 里出现一行「如果是 judge 就……」，对称性就散了，元评测（`MetaEvaluator` 消费 judge 自己的轨迹）也就无从谈起。

**为什么 loop 里一行评测代码都不许有。** 评测埋点全在中间件（任务 9 的管道），loop 只负责「按协议推进对话」。这是 `evaluators` 包能不 import `core` 的前提，也是 R2（事件 schema 被评测器倒逼改动）的第一道防线——评测器提出的新需求，第一反应应该是「我能不能从已有事件派生」，而不是「给事件加字段」。

**它同时解决三个具体的工程问题：**

| 问题 | 做法 |
|---|---|
| 并发工具调用时事件乱序 | `seq` 由计数器分配保证全序；因果性交给 `span_id` / `parent_span_id` |
| 模型幻觉出不存在的工具 | 不崩，发 `TOOL_RESULT(ok=False, error_type="unknown_tool")` —— 这条错误类型是 `FailureClassifier`「幻觉工具」模式的唯一输入 |
| 模型报错把整个 suite 炸掉 | `ERROR` 事件 + `RunStatus.LLM_ERROR` 返回，run 有终态、轨迹完整 |

## 2. 实现流程

1. **先写 run 级契约测试**：`finish` 终止为 `OK`、无 `finish` 耗尽预算、工具结果回喂给下一次请求、事件流含完整 7 类事件、`seq` 严格从 0 单调、未知工具被报告而非崩溃。为什么先写这 6 条：它们定义的是 `Run` 的**对外契约**（轨迹完整性、事件全序、失败可分类），不是内部实现。先钉死这些，后面任何优化都不能悄悄破坏它们。
2. **实现 `agent_loop`**：turn 循环 → `TURN_START` → 构造请求 → `LLM_REQUEST` → `provider.complete` → `LLM_RESPONSE` → 追加 assistant 消息 → 无工具调用则走终止分支 → 逐个执行工具调用（`TOOL_CALL` → invoke → `TOOL_RESULT` → 追加 tool 消息）→ `finish` 命中即返回 `OK`。
3. **实现 `RunContext`**：`seq` 计数器、`emit`、管道、`invoke_tool`（异常兜底）、`_execute_tool`（耗时统计 + 未知工具判定）。
4. **实现 `Run.execute()`**：发 `RUN_START`（含 `spec_json` 快照）→ `await agent_loop(ctx)` → 发 `RUN_END` → `await store.flush()` → **从 store 读回轨迹**构造 `RunResult`。
5. 跑 `tests/core/` 全量。

顺序理由：loop 依赖 `ctx`，`ctx` 依赖管道；`execute()` 的读回必须在 `flush()` 之后——真相源是文件，不是内存对象。

## 3. 具体技术实现

### `emit` 是同步方法，这是一个有意的设计

`ctx.emit_*` 全部是同步方法（内部 `put_nowait` 入内存队列），hot path 上**没有 await 点**。为什么重要：如果埋点要 `await`，并发工具调用就会在埋点上串行化，埋点变成性能瓶颈，进而诱导别人「为了跑得快把埋点删了」。落盘交给后台 writer 批量做（任务 8 的设计：`append()` 非阻塞，后台 writer 落盘）。这就是「真相源是文件，但写入是异步」的分工。

### `seq` 与 `span_id`：全序不等于因果

`seq` 由锁保护的自增计数器分配，保证全序（测试断言 `seqs == list(range(len(seqs)))`）。但设计文档 §6 R3 明确写了另一半：**因果性由 `span_id` / `parent_span_id` 保证，评测器按因果分组必须用 span，不能用 `seq` 相邻性。** 两个并发工具调用的事件在 `seq` 上会交错，「`seq` 相邻 = 同一次调用」的推理是错的。这条规则要写进任何读到轨迹的评测器的注释里。

### 异常边界：三层，各有归属

| 层 | 行为 | 为什么 |
|---|---|---|
| `provider.complete` 抛异常 | 发 `ErrorEvent(where="llm")`，返回 `RunStatus.LLM_ERROR` | 一次模型调用失败不该炸掉整个 suite；失败模式要能被 `FailureClassifier` 归因 |
| 工具执行抛异常 | `RunContext.invoke_tool` 捕获 → `ToolResult(ok=False, error_type="sandbox_error")`，loop 继续 | agent 必须看到「这一步失败了」，否则对话历史断链 |
| store 写失败 | **允许冒泡** | 轨迹是真相源。写不进去的 run 结果没有意义，必须响亮地失败 |

注意捕获的是 `except Exception`，**不是 `except BaseException`**：`asyncio.CancelledError` 从 3.8 起继承自 `BaseException`（不继承 `Exception`），正是为了不让它被 `except Exception` 顺手吞掉。写了 `except BaseException` 的代码会吃掉协程取消信号，run 再也停不下来——这是异步 Python 里最经典的自伤。

### 未知工具：不崩，但要留下机器可读的痕迹

`_execute_tool` 先查 `self.tool_names`，未命中直接返回 `error_type="unknown_tool"`。这不是单纯的健壮性措施：**「幻觉工具」（调用了不存在的工具）是 MAST 未覆盖、本设计补充的 4 个单 agent 专属失败模式之一**，它的检测完全依赖这个 `error_type`。如果这里改成抛 `KeyError` 冒泡，评测器就再也拿不到这个信号了。

### `finish` 短路与配对完整性

loop 里显式判断 `call.name == "finish" and result.ok` 后立即返回 `OK`。设计文档 §6 R3 还有一条要求：`finish` 短路时要**取消同 turn 内其余 tool task，并给被取消的调用补一条 `TOOL_RESULT(ok=False, error_type="cancelled")`**，保持配对完整。Part 1 的实现是同 turn 内顺序执行（`for call in resp.tool_calls`），天然不会产生「半执行的并发调用」；但一旦有人把这里改成 `asyncio.gather` 并发（Part 2 的预算治理很可能会这么改），补 `cancelled` 结果就是必须的，否则评测器会看到悬空配对。

### `RunResult.trajectory` 从 store 读回，不用内存对象

`execute()` 的最后一步是 `await store.flush()` 再 `await store.get(run_id)`。这样「你看到的轨迹」与「落盘的东西」永远一致——`seq` 严格单调、7 类事件齐全这些断言，验证的是**产品**（轨迹文件），不是内存里的临时状态。

### 终端状态的语义

| 状态 | 触发条件 |
|---|---|
| `OK` | `finish` 被调用且返回 `ok=True` |
| `NO_FINISH` | 模型不再产出工具调用，或循环结束仍未调用过 `finish` |
| `MAX_TURNS` | turn 上限耗尽而模型仍在调用工具 |
| `LLM_ERROR` | provider 抛异常 |

四者是**不同归因**，绝对不能合并成一个 `failed`：`LLM_ERROR` 是基础设施问题、`BUDGET_EXCEEDED`（设计文档 §3.1 独立列为一个终态）是资源问题、`NO_FINISH` 是模型能力问题、`MAX_TURNS` 是预算配置问题。**状态枚举的粒度就是将来能做归因的粒度。**

> **实现时必须先对齐的一处歧义**：`max_turns` 在 `RunSpec.max_turns`（默认 20）和 `Budget.max_turns`（默认 20）里各有一份，而示例 loop 读的是 `ctx.spec.max_turns`；测试 `test_no_finish_exhausts_turns_as_no_finish` 却用 `Budget(max_turns=3)` 配置上限并断言 `turns == 3`。同时该测试用 5 条纯文本响应，而示例 loop 在第一个「无工具调用」的响应上就 `return NO_FINISH`（那样 `turns` 只会是 1）。动手前必须定死两件事：**上限读哪一处**，以及**纯文本回复是否立即终止 run**——从测试断言的语义看，正确行为应是纯文本回复不终止 run（否则模型一句「让我先看看」就会把 run 判成没完成），一直推进到上限后以 `NO_FINISH` 收尾。

### `RunDeps` 全部可注入

`provider` / `store` / `tools` / `middlewares` / `executor_factory` / `id_gen` / `clock` 全是构造参数。`id_gen` 与 `clock` 可注入意味着 `run_id` 和耗时在测试里是确定的。**`Run` 不认识任何具体 provider / store / executor**——这正是 `FakeProvider`（任务 6）能让整条路径离线跑通的原因，也是 M1 能在无网络条件下完成验收的原因。

## 4. 使用的技术栈简介

本任务**纯标准库**：`asyncio`（唯一的异步运行时）、`dataclasses`（`RunDeps` / `RunResult` 用 `slots=True`）、`time.monotonic`。

- **为什么用 `time.monotonic` 而不是 `time.time`**：`time.time` 会被系统时钟回拨影响（NTP 校时、手动改表），测出的耗时可能为负。测量时长必须用单调时钟——`clock` 可注入的默认值就是 `time.monotonic`。这和 ruff 的 `DTZ` 规则集强制时间戳必须带时区是同一类「时钟卫生」问题（tech-stack §11 特意点出 `DTZ` 值得注意，因为轨迹时间戳必须带 tz）。
- **`LLMProvider` / `TrajectoryStore` 都是 `@runtime_checkable` 的 `Protocol`**（任务 5 定义在 `contracts/`，属 L0 叶子层）。因此依赖注入不需要任何 DI 框架：`FakeProvider` 和未来真实的 `OpenAICompatProvider` 都只是「有 `complete` 与 `aclose` 的对象」。**判据是「测试需要替换什么」，不是「能否抽象」。**
- **事件模型是 pydantic 2.13**（frozen + `extra="forbid"` + 判别联合），loop 只做组装、不改事件——事件一旦构造就是不可变值，可以安全地被并发读取、也可以直接进 `dict` 做 key。
- 异步原语选型（`anyio` vs 裸 `asyncio`）见任务 9 §4：技术文档倾向 anyio、计划代码用 asyncio，这是 tech-stack §12 ① 的待拍板项。

## 5. 工程化思想

- **「观测」与「执行」分离的判据是可发布节奏，不是代码美观。** 核心 loop 里不许有评测代码，真正的收益是：改一个评测规则不需要重新验证 agent 的执行逻辑；新增一个评测器不需要改任何现有文件（设计文档 §3.5 把这称为「正确抽象的可验证证据」）。**判断一条边界该不该划，问「这两边的变更频率与验证成本是否不同」。**
- **失败要分类，不要合并。** 任何把失败压成一个布尔值的系统，都在放弃未来的诊断能力。推广到 CI：区分「编译失败」「测试失败」「超时」「基础设施故障」，比一个红色叉号有用得多。
- **可观测性数据一旦成为评测输入，就升格为产品接口。** 轨迹要冻结 schema、要快照测试、要向后兼容（设计文档 §6 R2 的四层防线）；`RunResult` 从 store 读回而不是用内存对象，也是同一条原则的落地——**你交付的东西就是你验证的东西。**
- **字段能派生就不要加。** R2 的规约是「加字段前先自问『能否从已有事件派生』」，绝大多数「我需要 X 字段」其实是「我能从 `TOOL_CALL`/`TOOL_RESULT` 配对算出来」。这跟数据库不存冗余列是同一个道理：冗余一旦存在，就必须有人负责让它保持一致。
- **发现语义重复定义要立即收敛。** `max_turns` 存两处这类问题，成本不是「多写一行」，而是「看起来能跑、但行为取决于读哪一处」——这类 bug 不会在测试里红，只会在报告里产生无法解释的数字。**歧义要在实现前消灭，不要在结果里解释。**
