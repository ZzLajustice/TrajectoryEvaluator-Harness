# 任务 18：Provider 一致性测试套件

> **所属里程碑**：M3 · **前置任务**：任务 16（`OpenAICompatProvider`）、任务 6（`FakeProvider`） · **代码位置**：`tests/providers/test_conformance.py`（另补 `tests/fixtures/provider/`）

## 1. 总体目标

把 provider 的**契约**从"实现里长什么样"提炼成**一组跨实现都必须成立的不变量**，然后参数化跑遍所有 implementation。

套件是 8 个场景 × N 个实现。当前 N=2（`openai_compat`、`fake`），跑出 16 项。

它的价值不在"多测了几个用例"，而在于**新增一家 provider 只需在 `FACTORIES` 里加一行**：

```python
FACTORIES = {
    "openai_compat:text": _openai_with("text_response.json"),
    "fake:text": lambda: FakeProvider([text_response("hello")]),
}
```

不改任何测试函数、不改任何断言、不复制粘贴。这是"抽象是否正确"的**可执行证据**——如果加一个实现要改二十处测试，说明抽象漏了（或者根本没有抽象）；如果只加一行，说明契约是真的。

## 2. 实现流程

1. **写套件本体**：`FACTORIES` 字典 + 参数化 fixture + 8 个场景函数。
2. **跑测试，预期部分 FAIL**（边界 fixture 缺失）。此时失败是**有信息量的**：它告诉你现有 fixture 覆盖不到哪些边界。
3. **补边界 fixture，并修正实现中暴露的缺陷**。计划里点名两个：`empty_content.json`（`content` 为 `""`）、`parallel_tool_calls.json`（两个 `tool_calls`）。若测试暴露 provider 的缺陷（例如 `content` 为空时 `text` 属性崩溃），在这一步修。
4. **跑全套验证通过**：2 provider × 8 场景 = 16 项。
5. Commit。

顺序上有一条不能动：**步骤 3 必须在步骤 2 之后**。因为"要补哪些边界 fixture、实现会不会在边界上崩"在测试跑起来之前只是猜测。先让套件红，红的那些才是真问题；预先设想边界，往往补的是一堆没人踩的坑，真正会崩的反而漏了。

## 3. 具体技术实现

### 用 `params=sorted(FACTORIES)` 做参数化

```python
@pytest.fixture(params=sorted(FACTORIES), ids=sorted(FACTORIES))
def provider(request):
    return FACTORIES[request.param]()
```

`sorted()` 不只是好看：pytest-xdist 的 `--dist worksteal` 会把用例分片到多个 worker，**参数化 ID 的稳定排序决定了分片结果可复现**。顺序随机的参数字典会让"同一份代码两次 CI 跑出不同分片"。

每个场景函数只写一次，自动 ×N。这是 pytest 参数化的常规用法，但这里的**用意**不同：常规用法是"同一逻辑多组数据"，这里是"**同一契约多个实现**"。后者要求场景函数必须只依赖 `LLMProvider` 协议（`complete` / `aclose`），不依赖任何实现细节——所以场景里看不到 `httpx2`，也看不到 `FakeProvider`。

### 8 个场景，每个都是一条不变量

| 场景 | 断言 | 为什么它是不变量 |
|---|---|---|
| 1 纯文本 | `r.text` 是非空 `str` | 下游 loop 直接把它塞进消息历史 |
| 2 `finish_reason` | ∈ `{stop, length, tool_calls}` | `length` 被归一化程序丢掉的话，截断就不可见了 |
| 3 `usage` 永不为 `None` | `input_tokens >= 0` | 计费与预算依赖它，`None` 会在统计时爆掉 |
| 4 content blocks 良构 | `type` ∈ `{text, tool_use}` | 判别联合的取值域，SequenceMatcher 之类都按它分派 |
| 5 tool_calls 有 id 和 name | `tc.call_id and tc.name` | call_id 是 TOOL_CALL ↔ TOOL_RESULT 配对的键 |
| 6 `raw` 可 JSON 序列化 | `json.dumps(r.raw, default=str)` | **raw 要落盘进事件流**（JSONL） |
| 7 `latency_ms >= 0` | — | 不变量：耗时不可能是负的 |
| 8 `aclose` 幂等 | 连调两次不抛 | run 的收尾路径可能走多次（含异常路径） |

场景 6 值得单独说：它把一个**存储需求**表达成了 provider 契约。`LLMResponseEvent.raw` 是 replay 无损性的保证，而它最终要写进 JSONL。如果某个 provider 往 `raw` 里塞了不可序列化的对象（比如 SDK 的响应对象本身、`datetime`、bytes），崩溃点会出现在很远的存储层，而且报错信息完全指不到 provider。**把约束前移到产生数据的边界上，错误才有正确的归因位置。**

### 边界 fixture 暴露的是实现缺陷，不是测试缺陷

`empty_content.json` 与 `parallel_tool_calls.json` 属于"正常路径想不到、生产环境常遇到"的一类：

- `content` 为 `""` 时，如果实现写成 `text = msg["content"]`，`KeyError` 或 `None` 会一路传到 `LLMResponse.text`（协议里它是 `str`），在很远的地方才炸。
- 两个 `tool_calls` 时，如果实现只取 `tool_calls[0]`，模型并行调用的另一半会被**静默丢弃**——轨迹看起来完全正常，只是少了一半调用。

**静默丢弃比崩溃危险得多**，这也是为什么套件要写的是"良构性"断言而不是"数量断言"。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| `pytest`（`>=8.4,<10`）+ 参数化 fixture | 场景 × 实现的笛卡尔积由框架展开，测试代码只写一遍 |
| `pytest-xdist`（`--dist worksteal`） | 分片执行。inspect_ai 的注释指出默认的 `load` 会让 worker 空转（"4 of 10 legs stranded 76-80s on a single worker"），故用 `worksteal` |
| `anyio` 自带 pytest 插件 | `pytestmark = pytest.mark.anyio`。**不用 `pytest-asyncio`**：代码用 anyio 原语，测试插件用 anyio 才匹配，且能验证 trio 后端 |
| `httpx2.MockTransport` | 套件对所有实现**都不联网**（含 `openai_compat`），这是它能进 CI 高频跑的前提 |
| `ruff` / `pyright` | 套件与实现同在 CI gate 覆盖内（`pyright` 的 `include` 含 `tests`） |

## 5. 工程化思想

**可扩展性要用"新增一个实现需要改多少行测试"来度量，而不是用"架构图好看"来断言。** 一致性套件的 `FACTORIES` 就是那把尺子：一行 factory = 契约真的抽象出来了；二十处改动 = 什么都没有抽象。这个度量方式可以迁移到任何"多实现 + 统一协议"的场景（存储后端、执行器、评测器、告警通道）。

**单测与契约测试是两种不同的东西，不能互相替代。** 单测验"这个实现这么写对不对"（对着实现写）；契约测试验"所有实现都满足同一组不变量"（对着协议写）。前者会跟着实现一起漂——实现改了，单测跟着改，永远绿；后者只在**协议被破坏**时才红。一个只有单测的项目，会在第三次加实现时发现两个实现的行为早就分叉了。

**把需求前移成契约。** "`raw` 要能写进 JSONL"本质上是存储层的需求，但它被表达成了 provider 层的断言（场景 6）。约束离数据产生点越近，归因越准；等到存储层再报错，排查路径上隔着三四个模块。

**边界用例是契约最容易破的地方，也是最有价值的 fixture。** 空内容、并行调用这类输入在 happy path 里永远遇不到，但在真实模型上天天发生。测试套件红的那几项，不是测试有问题，而是**套件终于把实现里的假设问出来了**。
