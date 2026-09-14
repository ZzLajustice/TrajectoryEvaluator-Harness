# 任务 6：`FakeProvider`

> **所属里程碑**：M1 · **前置任务**：任务 5 · **代码位置**：`src/harness/providers/fake.py`、`tests/providers/test_fake.py`

## 1. 总体目标

一个**脚本化、无网络、确定性**的 `LLMProvider` 实现：按预设脚本依次返回 `LLMResponse`。计划文件把它标为 M1 的关键："没有它，M1 就得依赖真实 API，测试会变慢、变贵、不确定。"

三个具体价值：

1. **让端到端路径离线可跑。** M1 要一次性打通事件模型 → 轨迹 → 工具注册表 → loop → store → CLI，任务 11 的 `examples/hello.yaml` 直接声明 `provider: fake`。若依赖真 API，每次跑测试都要网络、要 key、要花钱，且失败原因无法区分：是 harness 有 bug，还是模型这次没答对？
2. **把模型的不确定性从被测系统里剔除。** 剩下的失败一定来自系统本身，这是"可复现"的前置条件。
3. **为后续评测器测试定调。** 设计文档 §7.1 要求评测器用 `CannedJudge` 注入固定判词——`FakeProvider` 与 `CannedJudge` 是同一思路的不同层：**把不可控的外部依赖换成可控的替身，测试才是在测你的代码**。

## 2. 实现流程

1. 写 5 个失败测试：序列顺序返回 / 脚本耗尽抛 `IndexError` / 记录所有请求 / callable 脚本可按工具结果分支 / `tool_call_response` 带 `call_id`
2. 跑出 `ModuleNotFoundError`（红）
3. 实现 `text_response` / `tool_call_response` 两个工厂函数 + `FakeProvider`
4. 跑 `tests/providers/` 5 passed（绿）
5. commit

顺序理由：

- **"记录所有请求"必须在第一版就有。** 它决定了 provider 的形态：不只是"返回响应"，同时是一个 **spy**（记录收到的 `LLMRequest`）。事后补会发现很多调用点根本没把请求传进来。Spy 能力对齐 tech-stack 的核心诉求——**精确复现与校验 payload**。
- **"callable 脚本可按工具结果分支"也不能缓。** 纯序列脚本只能表达"第一次答这个、第二次答那个"，**表达不了反馈回路**（看到 `ok=False` 就换策略）。而过程级评测最关心的失败模式之一恰是"看到报错但未修正就重试"（设计文档 §4.2 的"忽略工具返回"）。**测试替身的能力边界要在第一版定下来**，否则后面要么改接口，要么写出测不了反馈回路的假测试。
- **"耗尽抛 `IndexError`"紧跟"顺序返回"**：脚本耗尽意味着"agent 比预期多调了一次 LLM"，这通常正是 bug 信号。静默返回空响应会让这个信号消失，测试沦为假绿。

## 3. 具体技术实现

**双形态脚本**：`Sequence[LLMResponse] | Callable[[LLMRequest], LLMResponse]`。序列用 `iter(script)` 而非 list + 下标——`iter` 天然带"已耗尽"语义，不需要自己维护游标。

**耗尽时抛 `IndexError`，不让 `StopIteration` 冒出去**：`StopIteration` 在生成器/协程上下文里有特殊含义（会被包装成 `RuntimeError`），直接冒泡会产生"看起来像框架 bug"的报错。显式的 `raise IndexError("fake provider script exhausted") from exc` 让断言能精确匹配 message，也让人一眼看出是脚本用完了。

**工厂函数集中构造知识**：`text_response()` / `tool_call_response()` 把"一个合法的 `LLMResponse` 长什么样"（content blocks + 解包后的 `tool_calls` + `usage` + `finish_reason`）收在一处。反例：让每个测试自己手搓 `LLMResponse(...)`，一旦 content 的结构约定变化（例如新增 cache 相关 block），几十个测试文件全要改。

**`tool_call_response` 同时填 content 里的 `tool_use` block 与顶层 `tool_calls`**：这是"富 → 简归一化"（设计文档 §3.6）的具体体现——`content` 是富表示（与 Anthropic 风格一致、支持 text + tool_use 混排），`tool_calls` 是给 loop 用的便利视图。由同一个工厂保证两者一致，不会出现"content 里三个 tool_use、tool_calls 里只有一个"。

**`latency_ms=0` 与固定 `Usage(input_tokens=10, output_tokens=5, calls=1)`**：确定性是刻意选的。设计文档 §7.2 要求"同一 cassette 跑两次，`trajectory.jsonl` 归一化后逐字节相同"——若 usage 随机，预算与成本统计测试就无法写出确定断言。

**先判 `callable` 再当序列用**：`Sequence` 不可调用、函数不可迭代，但顺序写反会让边界情况（同时实现 `__iter__` 与 `__call__` 的对象）行为不确定。

**`FakeProvider` 不继承任何基类**：靠任务 5 的 `LLMProvider` Protocol 结构化满足契约，这是 Protocol 选型的第一次兑现。

| 替身 | 所在层 | 替换掉什么 |
|---|---|---|
| `FakeProvider` | provider 层（`LLMProvider`） | 整个模型调用（脚本化响应） |
| `httpx2.MockTransport` | transport 层（注入 `http_client=`） | 只替换 HTTP，仍走真实 SDK 的序列化与解析（tech-stack §2） |

两者互补：`FakeProvider` 快、覆盖 loop 与 store；`MockTransport` 能验证"我们真的发对了 payload"。

## 4. 使用的技术栈简介

本任务**零新库**，只用标准库与任务 5 的类型：

- `typing.Sequence` / `Callable`：联合类型表达"两种脚本形态"，pyright 可据此发现 `FakeProvider(42)` 这类错误。
- `LLMResponse` / `ToolCall` / `Usage`：任务 5 定义的值对象。
- `async def complete(...)`：协议要求异步。注意 `FakeProvider.complete` 内部**没有 `await`**，却仍是 `async def`——**异步接口不代表内部一定要并发**，很多替身就是为了"零延迟地满足异步签名"。

下一层（Part 2 的任务 16）才实现 `OpenAICompatProvider`，那时引入 `openai` 3.x SDK 与 `httpx2`。本任务刻意把"离线路径"与"真实网络路径"隔开，让 M1 完全不碰网络。

## 5. 工程化思想

**（1）测试替身的能力上限要按"要测什么"设计，而不是按"最省事"设计。** 序列脚本最省事，但它测不了反馈回路。**判断替身是否够用的方法：把它放进你要测的最复杂场景里走一遍**——如果那个场景根本表达不出来，说明能力边界画错了。

**（2）替身本身也要有错误处理。** "脚本耗尽抛 `IndexError`"是刻意的失败设计：**测试替身在异常状态下的行为，决定了它会不会掩盖 bug**。一个"耗尽后返回空响应"的替身，会让"loop 多调了一次 LLM"静默通过。给替身的失败模式写测试，与给生产代码写同等重要。

**（3）Spy 比返回值更有信息量。** 断言"拿到了什么"只验证一半，记录"我们发了什么"才验证整条链路。这个模式在 HTTP 层叫 cassette / payload 断言，在函数层叫 mock 的 `assert_called_with`，本质相同：**把交互本身变成可断言的对象**。

**（4）确定性的资源值让强断言成为可能。** 固定的 usage 与 `latency_ms=0` 看似偷懒，实际是"逐字节相同"这类最强断言可写的前提。**凡希望以后能对输出做精确比对的系统，都要在早期消除随机源与时钟源**（同理适用于时间戳、UUID、ID 生成）。
