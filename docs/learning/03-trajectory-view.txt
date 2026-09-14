# 任务 3：`Trajectory` 只读视图

> **所属里程碑**：M1 · **前置任务**：任务 2 · **代码位置**：`src/harness/events/trajectory.py`、`tests/events/test_trajectory.py`

## 1. 总体目标

`Trajectory` 是**事件流之上的只读视图**：把一串 `EventUnion` 包成评测器友好的查询接口（`tool_calls()` / `tool_results()` / `result_for(call_id)` / `tool_sequence()` / `compactions()` / `denials()`）。

它在系统中的位置体现为一个架构决策：**它住在 `events/` 而不是 `store/`**（设计文档 §2.2）。原因——若 `Trajectory` 住在 `store/`，评测器就必须 `import store`，而 `store` 要 import `core` 的错误类型 → 循环依赖立刻出现，「评测器不依赖 core」这条铁律随之破产。`Trajectory` 不含任何 I/O（`from_jsonl` 收的是**字符串**而非路径），所以它能安全地留在 L0。

它解决的另一个痛点是**口径分散**：如果每个评测器自己遍历事件列表算工具调用序列、自己按 `call_id` 找结果、自己汇总成本，那么 5 个评测器就有 5 份略有差异的实现——一旦口径不一致（例如一个把被 deny 的调用算进序列、另一个不算），评测结果之间就无法互相解释。

## 2. 实现流程

1. 写 5 个失败测试：`tool_sequence()` 保序 / `result_for` 按 `call_id` 命中 / 缺失 `call_id` 返回 `None` / 访问器返回 `tuple` / JSONL roundtrip
2. 跑测试确认 `ModuleNotFoundError`（红）
3. 实现 `from_events`（构造时建索引）+ 各访问器 + `to_jsonl` / `from_jsonl`
4. 跑整个 `tests/events/` 全绿（含任务 2 的测试，确认无回退）
5. commit

顺序理由：

- **第 4 步跑整个 `tests/events/` 而不只跑新文件**：`Trajectory` 复用 `parse_event` / `dump_event`，roundtrip 测试是两组代码的接缝；只跑新文件会漏掉接缝上的回归。
- **"缺失 `call_id` 返回 `None`"必须在第一步就写成断言**：这是 API 语义的分水岭。抛 `KeyError` 会让每个评测器都写 try/except；返回 `None` 则让"这个调用没有结果"与"我没查"在类型上无法区分。两者都可接受，但**必须选一个并用测试钉死**，而不是让第一个使用者随手决定。

## 3. 具体技术实现

**构造时一次性建好索引**（`_by_seq` / `_by_call_id`）。收益是可计算的：`GroundingChecker` 与 `TrajectoryMatcher` 都要按 `call_id` 频繁查 `ToolResultEvent`，一次 run 的轨迹可能上千事件，`O(n)` 扫描 × 每个工具调用 = `O(n²)`。代价是构造变慢、内存增加，以及**必须保证索引与事件元组同源**（因此 `events` 是构造参数且无 setter）。注意 `@dataclass(slots=True)` 下不能动态挂属性——索引字段必须在 dataclass 上显式声明（`field(init=False, repr=False)`）再在 `__post_init__` 里赋值；`slots=True` 换来的内存收益与防拼写错误，代价就是这个显式声明。

**返回 `tuple` 而非 `list`**：文档写"评测器不得修改轨迹"是约定，返回不可变类型是**机制**。调用方拿不到 `append`，就没有"改一下试试"的机会。代价（要排序、拼接时得自己转 list）应该由调用方付。

**`of(*types)` 作为统一过滤入口**：`tool_calls()` / `compactions()` / `denials()` 都是它的薄封装。新增 `EventType` 时只加一行封装，不需要在多处重写过滤逻辑。

**`to_jsonl()` / `from_jsonl()` 必须走 `dump_event` / `parse_event`**：绝不能自己 `json.dumps(ev.model_dump())`——那会绕过 `mode="json"` 的 datetime 转换与判别联合的 `type` 标签。JSONL 一行一条的格式收益：可流式读、可 grep、可对单事件 diff，且追加写天然 crash-safe。

**派生属性（`status` / `cost_usd` / `input_tokens` 等）集中定义**：状态从 `RunEndEvent` 取、成本从 `Usage` 汇总，这些口径全系统只允许有一份实现。这是"评测结果可比"的前提。

**`from_jsonl` 收字符串而不是路径**：这是"视图"与"I/O"分离的具体手法——`JsonlStore`（任务 8）负责读文件，读到的文本交给 `Trajectory`。若这里收路径，`events/` 就隐式依赖了文件系统语义（编码、压缩、路径规范化），L0 的纯度随即消失。

## 4. 使用的技术栈简介

本任务不引入新库。用到的标准库能力：

- `dataclasses` 的 `@dataclass(slots=True)`（3.10+）：生成 `__slots__`，减少每实例内存占用并阻止动态属性。
- `classmethod` 承担"多来源构造"（`from_events` / `from_jsonl`）：Python 里表达多态构造的惯用手法，比 `__init__` 里塞分支清晰。

上游依赖是任务 2 的 `EventUnion` / `parse_event` / `dump_event`。下游的 store（任务 8）会直接调用 `Trajectory.from_jsonl()`——**复用视图而不是自己写一份反序列化**，这保证了"内存里的轨迹"与"从盘上读回的轨迹"是同一类型、同一套访问器。

## 5. 工程化思想

**（1）消除循环依赖最有效的办法是"把纯数据视图下移"，而不是加 `if TYPE_CHECKING`。** 惰性 import 或类型检查专用 import 只是把依赖藏起来，依赖规则仍然是破的（架构测试一查就现形）。真正干净的解法是问：这个对象到底需要 I/O 吗？不需要，就把它放到依赖图更靠下的地方。**判据：一个模块只要不碰 I/O 与外部状态，它就应该住在底层。**

**（2）只读视图是"接口防呆"，不是"接口礼仪"。** 返回不可变容器的收益不是"更优雅"，而是消除了整整一类 bug（调用方无意中修改共享状态），且这个收益**不依赖调用方的自觉**。设计任何被多方消费的数据结构时都可以问：我能不能让修改在类型层面就不可能？

**（3）预建索引只在"读多写零"时划算。** `Trajectory` 构造后不再变化，这是索引永不失效的前提。反过来，若对象可变，索引就要处理失效（版本号、观察者、惰性重建），复杂度立刻上一个台阶。**先确认数据结构的可变性，再决定要不要建索引。**

**（4）把聚合口径放在数据旁边，而不是散在消费者里。** 成本、状态、token 数这类派生量一旦有第二份实现，就一定会与第一份产生分歧。可迁移做法：让"数据 + 它的派生量"住在一起，消费者只允许通过这一条路读取。
