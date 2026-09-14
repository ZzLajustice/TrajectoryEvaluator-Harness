# 任务 29：`Scheduler`

> **所属里程碑**：M6 · **前置任务**：无（只依赖 stdlib `asyncio`；被 suite 编排调用） · **代码位置**：`src/harness/orchestration/scheduler.py`

## 1. 总体目标

17 条用例、每条几十秒到几分钟，串行跑完不现实——必须并发。但并发会立刻带来三个新问题：

| 问题 | 不管会怎样 |
|---|---|
| 结果顺序 | 按完成顺序收集结果，同一份 suite 跑两次产出不同的报告顺序——报告 diff、baseline 对比全部变成噪声 |
| 故障扩散 | 一条用例卡死或抛异常拖垮整个 suite；或者被悄悄吞掉，报告看起来"少了两条" |
| 无界资源占用 | 8 路、80 路并发的差别是"跑得完"和"被限流/打爆" |

外加一个现实问题：单条用例可能卡住（模型不返回、子进程僵死），必须有**每 case 独立超时**，而不是整体超时——整体超时会让"卡住一条"变成"全部失败"，而且失败之后没人知道其余用例本来是好是坏。

所以 `Scheduler` 要同时给出四个语义：并发上限、每 case 超时、异常隔离、**结果按输入顺序返回**。

## 2. 实现流程

1. 构造参数：`concurrency`（`max(1, ...)`——0 会让信号量永久阻塞，直接钳死）、`case_timeout_s`、`fail_fast`。
2. `gather(items)`：`items` 是 `(key, work)` 的**有序**序列，`work` 是 `Callable[[], Awaitable]`（工厂，不是协程对象）。
3. 建 `asyncio.Semaphore(concurrency)` 与 `results: dict[key, Any]`。
4. 每个 case 包一层 `guarded(key, work)`：`async with sem` → 有超时就用 `async with asyncio.timeout(...)`，没有就直接 await → 异常存进 `results[key]`，`fail_fast` 时再 `raise`。
5. `async with asyncio.TaskGroup()` 批量 `create_task`。
6. 最后**按输入顺序重建列表**：`[(key, results.get(key)) for key, _ in items]`。

第 6 步是整个任务的核心，也是顺序上唯一不能挪的一步：排序必须发生在**最后**，而不是收集时。若边完成边 `append`，顺序就变成了完成顺序，且这个 bug 在单条用例、无并发的测试里完全看不出来。

还有一处：`work` 是**工厂函数**而不是协程对象。传协程对象的话，"何时开始"在创建时就定了，而且无法在信号量拿到之后再启动；工厂让调度器决定启动时机（`async with sem` 之后），也留出了重试/重放的空间。

## 3. 具体技术实现

### 为什么必须按输入顺序返回

报告的可复现性有两层：一是"同样的输入给同样的分"，二是"同样的结果集渲染出同样的报告"。第二层正是这里丢的——若顺序来自完成时间，两次运行的报告在 diff 里**全是位置噪声**，基线对比（任务 34 的 `diff_runs`）会把噪声当成回归，CI 门禁会随机红。

代价是最后那次 O(n) 的重排；收益是整条链路（报告、diff、CI artifact、快照测试）的确定性。**执行可以乱序，呈现必须定序**——这一条在整个项目的其它地方反复出现：事件按 `seq` 全序（任务 2）、聚合按 `case_id` 对齐（任务 32/34）、索引按 `(run_id, seq)` 主键（任务 28）。

### `asyncio.TaskGroup` 的 `ExceptionGroup` 包装问题

`asyncio.TaskGroup` 是结构化并发原语：`async with` 块退出时，所有子任务必然已经结束或被取消，不会留下游离 task。代价是**它不把子任务的异常原样抛出，而是包成 `ExceptionGroup`**（PEP 654）。于是：

- `pytest.raises(RuntimeError)` 会失败（收到的是 `ExceptionGroup`）；
- 调用方写 `except RuntimeError` 也接不到。

计划因此明确要求：**在 `fail_fast` 分支里实现异常解包**，让"恰好一个异常"的场景把它原样抛出。两种做法计划都点了名——用 `except*` 按类型匹配后重新抛出，或写一个 `_flatten_exception` 辅助函数（inspect_ai 同款做法）。

实现时的要点：

- **只在组内恰好一个异常时解包**。多于一个时应当保留 `ExceptionGroup` 并把全部信息带出去——安静地丢掉一半异常，比抛一个丑陋的类型更糟。
- **`ExceptionGroup` 不是"多此一举的包装"，它是结构化并发不丢错误的代价**。它存在的原因是：并行跑 8 个任务时，失败的从来不只是第一个，把其余异常吞掉才是真正的 bug。所以解包应该发生在**库的对外边界**（让调用方拿到它期望的形状），而不是在内部取消组语义。
- 这条约束有测试探针：`test_fail_fast_cancels_remaining` 里的 `pytest.raises(RuntimeError)`。

顺带一个观察：计划给出的骨架里 `if self.fail_fast:` 与 `else:` 两个分支**代码逐字相同**。也就是说真正要写的代码不是照抄骨架，而是补上 fail_fast 分支应有的差异（解包）。**"两个分支一样"本身就是一句提示：差异还没被实现。**

### 超时：`asyncio.timeout` 与"失败即结果"

```python
async with asyncio.timeout(self.case_timeout_s):
    results[key] = await work()
```

`asyncio.timeout` 是 3.11+ 的上下文管理器（本项目 requires-python `>=3.12`）。超时抛 `TimeoutError`，而它是 `Exception` 的子类，于是被 `except Exception` 收进 `results`——`fail_fast=False` 时"超时"与"失败"在结果里表现为同一种东西（一个 Exception 实例），`test_case_timeout_is_enforced` 正是这么断言的：`isinstance(out[0][1], Exception)`。

这和任务 19 的结论是同一条：**失败必须是一种结果，而不是一种缺席**。把异常转成值存进 `results`，下游才能逐条渲染、统计"3 条超时、2 条断言失败"，而不是"跑到第 5 条就没了"。

### 一个要留意的边界

`fail_fast=True` 时，被取消或从未启动的 case 在结果里是 `results.get(key) → None`，与"正常返回了 `None` 的结果"无法区分。报告层应当把 fail_fast 场景整体当作"run 失败"处理，而不是逐条渲染——否则会把未执行的用例画成空结果。

另一个规模上的说明：信号量在任务**内部**获取，因此 `create_task` 是一次性全建的，500 条用例就是 500 个 task 对象（可接受，但不适合更大规模）。要支持上万条用例，应改成固定 N 个 worker 消费队列的 worker-pool 模式——这是实现时值得知道的分界线，不是当前任务的要求。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| `asyncio.Semaphore` / `asyncio.TaskGroup` / `asyncio.timeout`（stdlib，3.11+） | 结构化并发的三个原语：限流、生命周期、超时。本项目 Python 3.12 |
| `ExceptionGroup` / PEP 654 | 结构化并发的伴生语义；`TaskGroup` 用它保证并行失败不被静默丢弃 |
| **裸 `asyncio`，不引入 `anyio`**（tech-stack §4.1、§12①） | §4.1 的结论：**用裸 `asyncio`**。`Scheduler` 正是这条结论最集中的落点——它要的三个原语（`Semaphore` / `TaskGroup` / `timeout`）stdlib 3.11+ 全部具备，本项目 Python 3.12 |
| 为什么不用 anyio | anyio 的核心价值是 **trio 兼容**，"那是给**库作者**的——他们无法预知使用者跑在哪个后端"；本项目是应用，自己决定后端，收益为零。**概念负担**也计入成本：anyio 认知度低于 asyncio、中文资料少，对教学项目是净成本（§4.1 原文） |
| 调研事实与决策的差别 | §4.1 记录了 inspect_ai 用 anyio（核心依赖 `anyio>=4.14.0`，注释明确"4.14 fixes asyncio Lock/Semaphore waiter deadlock after cancellation"），且 `openai` / `anthropic` SDK 也依赖 `anyio<5`。但 SDK 用不用 anyio 与我们无关——"我们的代码与它们通过 `await` 交互，不共享原语" |
| 环境里仍会有 anyio | 它作为 `openai` / `anthropic` 的**传递依赖**存在，但我们的代码不直接 import 它；`anyio.to_thread.run_sync` 一律用 `asyncio.to_thread` 替代（任务 28 用的就是后者）。将来若要抽成库给他人用，迁移是机械的（`asyncio.Lock` → `anyio.Lock`、`asyncio.to_thread` → `anyio.to_thread.run_sync`） |
| `anyio` 自带 pytest 插件（§4.2） | 测试仍用 `pytestmark = pytest.mark.anyio`，不用 `pytest-asyncio`（inspect_ai 同款）；§12 承认这条存在合理分歧——deepeval / langfuse 都用 `pytest-asyncio`，替换成本是一行 conftest。**注意 §4.2 的理由段落仍写着"既然代码用 anyio 原语"，与 §4.1 的新结论不一致**：插件选择与运行时原语是两件事，以 §4.2 的结论为准 |

## 5. 工程化思想

**确定性输出是并发系统的第一要求，不是收尾时的优化。** 执行乱序是并发的本质，无法消除；但**呈现定序是 API 的承诺**，随时可以给。一个 fan-out / fan-in 的批处理接口如果不承诺输出顺序，下游的 diff、缓存、增量处理、快照测试全部失效——而且失效方式很隐蔽：功能都对，只是每次输出不一样。**把"顺序"写进契约（按输入顺序返回），比在文档里提醒"结果可能乱序"有用得多。**

**结构化并发把任务生命周期变成词法作用域。** `async with TaskGroup` 的语义是"退出这个块时，所有子任务都已经结束或被取消"——不需要 `gather` 之外再手工 await 一堆 task，也不会留下游离任务在后台偷偷写数据。代价是错误被包装成 `ExceptionGroup`。这是**一笔划算的交易，但边界层必须负责翻译**：库内部用组语义保证"绝不丢失并发错误"，对外 API 把错误还原成调用方期望的形状（恰好一个就抛它本身，多个才给组）。

**故障策略要显式成参数，不要隐式默认。** 批处理场景（跑完整套用例、看全貌、做统计）要 `fail_fast=False`；门禁场景（早失败早省资源）要 `True`。两种需求相反且都合理，做成参数比做两次实现便宜得多。**凡是"取决于使用场景"的行为，都该被提到接口上而不是藏进默认值。**

**每单元独立的 deadline 优于整体 deadline。** 整体超时把"一条卡住"变成"全部失败"，而且失败之后你无从知道其余用例本来能不能过——信息损失是不可逆的。可迁移：任何 fan-out 都要给每个单元配自己的超时，并让超时**作为一种结果**被记录。

**骨架里两个分支逐字相同，就是在告诉你活儿还没干完。** 计划给了结构，实现要补齐语义。这类"看起来完整的空白"最容易被跳过，而 TDD 恰好能挡住它：`pytest.raises(RuntimeError)` 这条断言不通过，就说明解包没写。**测试先行的价值不只在"先写测试"，更在于它把"哪些地方必须与骨架不同"钉成了可执行的清单。**
