# 任务 7：`ToolRegistry` 与 `finish` 工具

> **所属里程碑**：M1 · **前置任务**：任务 5 · **代码位置**：`src/harness/core/registry.py`、`src/harness/core/tools/finish.py`、`src/harness/core/tools/__init__.py`、`tests/core/test_registry.py`

## 1. 总体目标

两件事：

1. **`ToolRegistry`——工具注册与按策略过滤。** `schemas(policy)` 依据 `ToolPolicy` 过滤出要发给模型的工具 schema。它落实的是"权限在**暴露层**先收敛一次"：模型看不到某工具就不会调用它，这比"模型调了再拦截"便宜一个数量级。但两层都要（纵深防御），因为中间件层的拦截（任务 19 的 Permission / Policy 中间件）仍是必需的安全边界。
2. **`finish` 工具——agent 显式声明完成的唯一方式。** 它的存在是 `FailureClassifier` 检测 "Unaware of termination"（MAST 原论文占比 12.4%）的**前提**：没有 `finish` 调用就意味着 agent 没有意识到该结束了；而 "Premature termination" 同样依赖它——太早调用 `finish` 才是"过早终止"。

顺带解决的一个具体问题：`get()` 在未知工具名时抛出带 `available` 列表的 `KeyError`。这个报错文本有两个用途：① traceback 自解释；② `FailureClassifier` 的"幻觉工具"规则就是"调用的 tool name ∉ registry 的已知集合"（设计文档 §4.2），`names()` 就是那条判据的来源。

## 2. 实现流程

1. 写 5 个失败测试：注册后能取到 / 未知工具抛 `KeyError` 且信息含可用名 / `allow` 白名单生效 / `deny` 黑名单生效 / `finish` 返回 `ok=True` 且 content 含 summary
2. 跑出 `ModuleNotFoundError`（红）
3. 先实现 `registry.py`，再实现 `finish.py`
4. 跑 `tests/core/` 5 passed（绿）
5. commit

顺序理由：**`finish` 与 registry 放在同一任务里，而不是拆成两个。** 因为 `FinishTool` 是 `Tool` 协议的第一个真实实现——**写它的过程就是在验证协议够不够用**（例如 `description` 该是类属性还是 property、`invoke` 的签名能否表达"我这个工具不需要 workspace"）。如果先写 registry 再隔几个任务才写第一个工具，协议的问题会在最贵的时候暴露。

另外，测试 `allow` 白名单需要**两个** stub 工具（`_StubTool` 注册两次）。这本身就是信号：**过滤逻辑必须在"多个工具"的集合上才测得出来**——只有一个工具时白名单与黑名单的表现趋同。设计测试用例时的通用提醒：**边界逻辑要用能区分两种行为的样本。**

## 3. 具体技术实现

**过滤顺序决定语义：`deny` 是最终否决。**

```python
if policy.allow is not None and name not in policy.allow:
    continue
if name in policy.deny:
    continue
```

先 allow 后 deny，两者关系是"允许集合减去禁止集合"。若调换顺序或改成 `else` 结构，`ToolPolicy(allow=["x"], deny=["x"])` 的行为就会变。**这种优先级要在代码结构上一眼可见，而不是靠注释说明。**

**`names()` 返回 `sorted(...)`**：工具注册顺序会随导入顺序变化，排序后 schema 列表稳定 → 发给模型的 payload 稳定 → 可断言、可 replay。`schemas()` 也走 `names()`，所以整条链路顺序一致。**"稳定输出顺序"是确定性系统最廉价的基础设施。**

**`get()` 的错误信息带 `available`**（`f"unknown tool {name!r}; available: {available}"`）。错误信息是给人看的接口：只说 `unknown tool 'read_fil'` 会让人去 grep 注册代码，带上候选列表则拼写错误一眼可见。这条原则对 LLM 驱动的系统还有额外价值——**错误信息会进模型上下文，对 agent 也是有意义的反馈**。

**`FinishTool.schema()` 手写 JSON Schema，不从签名自动生成**：pydantic 可以 `model_json_schema()` 自动生成，但那会让 schema 变成实现的投影（实现细节一改，schema 就漂移）。评测 harness 的诉求是**精确复现 payload**，模型看到的 schema 必须显式、可控、可 review。代价是重复，收益是零意外。

**`invoke` 里 `ws: Any` 且不使用它**：`finish` 是纯声明、无副作用，不需要工作区。这暴露了 `Tool` 协议的一处取舍——统一签名让不需要 ws 的工具"多收一个参数"。这比"为不同工具设计两套签名"划算：**统一签名的价值在于管道可以无差别地调用任何工具**。

**`ToolResult.content` 放 summary 而非空串**：轨迹里 `tool.result` 的 content 是 `GroundingChecker` 的输入，有内容比空更有信息量——agent 声称完成了什么，本身就是有价值的记录。

## 4. 使用的技术栈简介

本任务不引入新库。涉及的概念：

| 概念 | 说明 |
|---|---|
| JSON Schema | 工具描述的标准形态，对应 OpenAI function calling 的 `tools[].function.parameters`。此处手写而非生成 |
| `KeyError` | Python 内置异常。选它是因语义与 `get(name)` 的字典式查找一致，且 `pytest.raises(KeyError, match="finish")` 可精确断言 |
| `Tool` Protocol（任务 5） | 结构化契约，`FinishTool` 与 `_StubTool` 都不继承任何基类 |

替代品：用 `pydantic` 的 `model_json_schema()` 自动生成工具 schema——**重复更少，但 schema 变成实现的投影**。对一个要求"精确复现 payload"的评测 harness，这个取舍与整体非目标一致：宁可显式重复。

## 5. 工程化思想

**（1）权限要在"看不见"和"拦得住"两层各做一次。** 只做暴露层过滤：模型被 prompt injection 诱导时仍可能请求；只做拦截层：每次都浪费一次昂贵的 LLM 往返。**纵深防御的成本是"两处维护同一份策略"，收益是任何一层失效时系统仍安全。** 通用判据：**失败代价不可接受时，做两层，而不是做一层加注释。**

**（2）循环系统必须有显式的终止信号。** 隐式终止（"跑够 N 轮就结束"）让"完成"与"被截断"不可区分，而两者的归因完全相反。**给 agent 一个"我认为我完成了"的工具，等于给系统一个可观测的自我判断**——这也是过程级评测能成立的前提之一。

**（3）稳定输出顺序是可测试性的基础设施。** 一行 `sorted()` 消除了一整类"测试偶尔失败"。**凡会被序列化、比对、replay 的集合，都要在输出前固定顺序。**

**（4）错误信息要包含"正确选项"。** `available: ...` 让报错从"你错了"变成"你错了，可选项是这些"，几乎零成本地把一次 grep 换成一次阅读。可迁移到所有用户可见的报错（CLI 参数、配置校验、API 拒绝）。

**（5）第一个实现是接口的试金石。** 抽象写完后要立刻写一个真实实现去用它——**这是检验抽象是否够用的唯一方式**。`FinishTool` 只用到了 `Tool` 协议的一小部分，恰好暴露了"统一签名里有些参数对某些实现是多余的"这一事实。
