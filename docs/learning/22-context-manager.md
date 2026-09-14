# 任务 22：`ContextManager` 与 `CONTEXT_COMPACT`

> **所属里程碑**：M4 · **前置任务**：任务 10（agent loop 的接入点）、任务 4（`Budget`） · **代码位置**：`src/harness/core/context.py`（另改 `src/harness/core/loop.py`）

## 1. 总体目标

`ContextManager` 做三件事：累积消息历史（system / assistant / tool）、估算 token 用量、在超预算时压缩并**产出一条 `CONTEXT_COMPACT` 事件**。

它的设计取舍是本任务的核心，请先看清楚这句话：

> **我们不追求压缩质量，只要求压缩可观测。**

压缩本身实现得极其朴素——丢掉最旧的 tool 结果，保留最近 4 条。这不重要。重要的是**压缩这件事必须留下可归因的证据**，因为"上下文被压缩后丢失关键信息"是真实的失败模式。设计文档 §3.1 决策 2 把 `CONTEXT_COMPACT` 提升为**一等事件**，并在 §4.2 里把它对应到 MAST 的 FM-1.4「Loss of conversation history」——检测方式是规则：**细粒度归因到 `CONTEXT_COMPACT` 事件**。

也就是说，这条失败模式的检测能力**完全依赖于压缩有没有留下事件**。如果没有事件，agent 在压缩之后突然开始重复劳动、忘记已经改过的文件、重新读一遍已经读过的代码，评测器只能看到一个"变笨了的模型"，而看不到"它是因为丢了上下文才变笨"。设计文档 §5.2 里还有一条专门的过程陷阱用例 `trap_context_pressure` 就是为此准备的。

## 2. 实现流程

1. **先写失败测试**（9 条）。注意顺序：**"不需要压缩"的测试排在"需要压缩"之前**——先把"什么都不做"的语义钉死（`needs_compaction() is False`、`compact() is None`），再定义"做"的语义。反过来写很容易得到一个"无条件丢消息"的 `compact()`，它在所有"应该压缩"的测试上都通过。
2. **跑测试确认失败**（`ModuleNotFoundError`）。
3. **写实现**：`estimate_tokens` → `ContextManager`（累积 → 估算 → 压缩）。
4. **跑测试验证通过**（`tests/core/`）。
5. **接入 agent loop**，并跑 `tests/e2e/` 确认仍全绿。
6. Commit。

步骤 5 必须排在步骤 4 之后：压缩逻辑是纯函数式的（给一串消息，决定丢哪些、返回什么事件），**必须先独立验证它是对的**，再把它接到会跑真实事件流的 loop 上。顺序反了，压缩逻辑的 bug 和接入的 bug 会混在一起，而 loop 的调试成本高得多。接完再跑 e2e 是同一个道理：默认 `token_budget=100_000` 时不会触发压缩，**e2e 全绿证明的是"接入没有改变既有行为"**。

## 3. 具体技术实现

### 双轨 token 计数：估算用于决策，实测用于记账

| 轨道 | 来源 | 用途 |
|---|---|---|
| **实测** | provider 上报的 `usage`，落在 `LLMResponseEvent` 上 | 权威值，计费与统计 |
| **本地估算** | `estimate_tokens()` 字符数启发式 | **仅**用于触发压缩决策 |

两者都落盘，且来源可区分——tech-stack §4.4 的原话是"否则评测数字不可信"。为什么要费这个劲：`tiktoken` 只有 OpenAI 的编码（cl100k / o200k），**对 DeepSeek / 通义 / Moonshot 无效**，用错编码偏差可超 30%，而且同族不同代也不通用（Qwen1 vs Qwen2、DeepSeek v1 vs v2 的 vocab 不同）。一个"看起来精确"的库用在错误的适用范围内，比粗糙的启发式更危险——它会让人以为数字是对的。

```python
def estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    ascii_chars = len(text) - cjk
    return int(cjk / _CHARS_PER_TOKEN_CJK + ascii_chars / _CHARS_PER_TOKEN_ASCII) + 1
```

它是**确定性**的：同样的输入给同样的输出（测试 `test_estimate_tokens_is_deterministic` 就是在钉这一点）。确定性不是洁癖——金轨迹生成与 replay 都依赖它，估算一旦漂移，"同一份产物跑两次逐字节相同"这条验收标准就守不住。

### 系统提示词由数据结构保护，而不是由算法步骤保护

```python
self._system = Message(role="system", content=[{"text": system_prompt}])
self._history: list[Message] = []
```

system prompt 存在**独立字段**里，不在 `_history` 里。于是 `compact()` 无论怎么改（丢最旧的、丢最长的、换成摘要），都不可能删掉它——`build_request()` 永远是 `[self._system, *self._history]`。测试 `test_system_prompt_survives_compaction` 断言的就是这个不变量。

如果 system prompt 混在 `_history[0]` 里，这条不变量就要靠"压缩时记得跳过第 0 条"来维持——**那是一条会随着算法演进而失效的约定**。

### 压缩策略与事件字段

```python
self._last_compaction = ContextCompactEvent(
    reason="token_pressure",
    messages_before=..., messages_after=len(self._history),
    tokens_before=...,   tokens_after=self.estimated_tokens(),
    dropped_message_digests=dropped,
    strategy="drop_oldest_tool_results")
```

`strategy` 与 `reason` 都是**机器可读的枚举值**，不是给人看的描述文本：`reason="token_pressure"` 说明触发原因是预算压力（将来可能还有 `manual`、`turn_limit`），`strategy` 说明用了哪种算法（将来可能换成摘要式）。评测器可以据此区分"压缩本身有问题"和"这个压缩策略有问题"。

`keep_tail = 4` 保留最近的对话结构，理由是"保证当前上下文连贯"——刚发生的工具结果几乎一定和当前决策相关，丢它们等于让 agent 立刻失忆。这个 4 是刻意选的**最简单**的值：目标是产生一个可被评测的压缩事件，而不是实现一个聪明的压缩算法。

`compact()` 在不需要压缩时返回 `None`（而不是空事件），让调用方少一个判空分支。

### 事件模板：纯逻辑与基础设施解耦

`compact()` 返回的事件 `run_id=""` / `seq=0`，由 `RunContext` 在 emit 前填充：

```python
tpl = ctx.context.compact()
ctx.emit(tpl.model_copy(update={"run_id": ctx.run_id, "seq": ctx.next_seq()}))
```

为什么不让 `ContextManager` 自己带上 `run_id` / `seq`：它就该不知道 `emit` 机制、不知道 seq 分配器的存在。**纯逻辑（何时压缩、丢哪些、压掉多少）留在 `ContextManager` 里，于是它可以完全不依赖 run 的上下文被离线单测**；`run_id` / `seq` 这类基础设施信息由组装层填。`model_copy(update=...)` 让"模板 + 填充"不需要 pydantic 模型本身是可变的。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| `tiktoken` `>=0.14,<1` | 核心依赖，**OpenAI 系 + 粗略近似**；本任务的实现没有调用它，走的是最粗的一档字符数启发式 |
| `tokenizers` extra（`>=0.23,<1`） | 非 OpenAI tokenizer 的**精确**估算（DeepSeek / Qwen / Moonshot），加载对应 `tokenizer.json`。按需安装，不进核心依赖 |
| `pydantic` 2.13（`model_copy`） | 事件模板填充 |
| JSONL + `zstandard`（§5） | `CONTEXT_COMPACT` 事件最终落进轨迹文件，是它"可被评测"的物质基础 |
| MAST（Cemri et al., NeurIPS 2025） | FM-1.4「Loss of conversation history」的归因锚点 |

## 5. 工程化思想

**"我们不追求做好 X，只要求 X 可观测"是一个值得主动做出的取舍。** 大多数工程决策的默认假设是"这个功能要做得更好"，但对评测 harness 来说恰恰相反：压缩算法越聪明（比如引入 LLM 摘要），引入的成本、不确定性、不可复现性就越多，**而评测系统自身的非确定性会直接污染它要测量的东西**。把不可控的部分降级为一个可观测信号，把可控的部分（事件、字段、归因）做扎实——这个模式可以迁移到任何"我负责观察，不负责改变"的系统里：GC、数据库 compaction、日志轮转、限流器，都是同一个形状。判断标准只有一条：**这条信息会不会被下游用来做判断？** 会，就必须可观测；不会，就不必追求质量。

**双轨测量必须标注来源。** 估算值和实测值都落盘，但必须能区分哪条是哪条。理由很具体：`tiktoken` 在 OpenAI 模型上偏差极小，在 DeepSeek / 通义上可能超过 30%。如果两者混在一个 `tokens` 字段里，半年后没人说得清报表上的那个数字是怎么来的。**"精确"是一种适用范围内才成立的属性，跨出范围后它比粗糙更危险**——粗糙的方法至少知道自己不准。

**关键不变量交给数据结构，不要交给算法步骤。** "system prompt 不能被压缩掉"如果靠"压缩时记得跳过第 0 条"来保证，那么每一次改压缩算法都是一次重新犯错的机会。把它放进另一个字段，不变量就变成结构性的——**换算法不会破坏它，因为它根本不在算法的操作范围内**。设计约束时，优先选择"违反它需要刻意破坏结构"的那种写法。

**确定性是评测基础设施的硬要求。** 同一个输入必须给同一个输出，否则金轨迹会漂、replay 会对不上、"跑两次结果一致"无法验证。这个要求会反向约束实现选择：宁可用一个可解释的启发式，也不用一个依赖环境、依赖库版本的"更准的"方案。
