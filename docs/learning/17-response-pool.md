# 任务 17：`ResponsePool` 与录制回放

> **所属里程碑**：M3 · **前置任务**：任务 6（`FakeProvider`，录制/回放的 inner provider）、任务 16（`OpenAICompatProvider`） · **代码位置**：`src/harness/providers/response_pool.py`、`src/harness/providers/recording.py`

## 1. 总体目标

给 provider 套一层**录制 / 回放**能力：`RecordingProvider` 包住真实 provider，把每次响应写进池子；`ReplayProvider` 只从池子里取，完全不碰网络。

**先澄清一个概念：它不是 HTTP cassette，是 LLM 响应采样池。** 这是设计文档 §3.6 与 tech-stack §2 特意分开的两个关注点：

| 组件 | 归属 | 职责 |
|---|---|---|
| `vcrpy` cassette | `tests/` 的 fixture | HTTP 层录制回放，防上游 API 漂移 |
| `ResponsePool`（自研） | `providers/` | **同一请求返回 N 个不同样本**，供 `MetaEvaluator` 的 judge consistency 使用 |

原设计文档里它叫 `CassetteStore`，**改名就是为了不和 HTTP cassette 混淆**。动机很具体：`vcrpy` 的 `allow_playback_repeats` 只能**重复同一个响应**，给不出 N 个不同样本。而 judge consistency 要测的恰恰是"同一个 prompt 在多次采样下判定是否稳定"——如果回放永远给同一个响应，这项指标就是恒定的 0 方差，测了个寂寞。

## 2. 实现流程

1. **先写失败测试**，覆盖五类行为：同请求返回不同样本、样本用尽后 wrap-around、不同请求互不干扰、跨进程 key 稳定、缺 key 报可操作的错。测试先行的意义在这里格外大：这个类的**契约就是它的语义**（occurrence 怎么算、key 由什么决定），不先钉死就一定会漂。
2. **跑测试确认失败**（`ModuleNotFoundError`）。
3. **写 `ResponsePool`**：`key_of` / `record` / `replay` / `occurrence_count` / `save`。
4. **写 `RecordingProvider` / `ReplayProvider`**。装饰器只是薄薄一层转发，放在池子之后写——池子的语义正确了，装饰器才只是"胶水"。
5. **跑测试验证通过**。
6. Commit。

顺序上的硬约束只有一条：**`key_of` 必须先于 `record` / `replay` 定稿**。key 是录制与回放之间唯一的握手协议，两边算法不一致，录制时写进去的东西回放时永远找不到——这种 bug 不会报错，只会表现为"池子空了"。

## 3. 具体技术实现

### 存储形态：list + 游标，不是一个响应

```python
# 存储形态：{key: [resp, resp, ...]} —— list + 游标，不是单个响应
def record(self, req, response) -> None:
    self._data.setdefault(self.key_of(req, req.model), []).append(response)

def replay(self, req, *, occurrence: int):
    return samples[occurrence % len(samples)]   # 用尽后 wrap-around
```

`occurrence` 是**第几次遇到这个请求**，不是"哪个样本"。取模让"样本比请求少"成为一个可接受的常态（跑 10 次只有 3 个样本时循环使用），而不是需要调用方处理的错误。

### key 的确定性：canonical JSON + 字段白名单

```python
blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
```

两点都是刻意的：

- `sort_keys=True` 消除 dict 顺序带来的抖动（Python 的 dict 有序，但构造顺序不保证跨代码路径一致）。
- **只哈希影响模型输出的字段**：`model` / `messages` / `tools` / `temperature` / `tool_choice`。`temperature` 必须进 key——judge consistency 正是要观测"同一 prompt 在不同采样下的判定差异"，把它排除在 key 之外，不同采样就会混进同一个槽位。

反例是很容易写出来的：用 `hash()`、`id()` 或直接 `str(dict)`。前两者是**进程内标识**，跨进程并不保证一致；后者对 dict 顺序敏感。**key 一旦跨进程不稳定，录制与回放就对不上，而且失败方式是"池子空了"这种沉默的失败**。

### 缺 key 的报错必须可操作

```python
raise KeyError(
    f"response not recorded for key {key[:12]}... "
    f"(model={req.model}, {len(req.messages)} messages). "
    "Record first with --record, or check that the prompt is unchanged.")
```

带上 key 前缀、model、消息条数，因为它要回答的问题是"为什么没录到"——最常见的两个原因是没开 `--record`，以及 prompt 被改过了。三个字段刚好能区分这两者。

### 装饰器而不是子类

`RecordingProvider(inner, pool)` / `ReplayProvider(pool, fallback=None)` 都只实现 `name` / `complete` / `aclose`，与厂商完全解耦。选装饰器而不是"给每个 provider 加录制开关"，是因为**录制/回放与厂商是两个正交维度**：N 个 provider × 2 种模式不需要 N×2 个类，任意 provider（包括 `FakeProvider`）都能被同一套逻辑包住。`ReplayProvider` 的 `fallback` 参数则提供"池子里没有就走真实 provider"的渐进迁移路径。

## 4. 使用的技术栈简介

| 组件 | 版本/状态 | 说明 |
|---|---|---|
| `vcrpy` | 8.3.0 | **新旧两栈都支持**。它在 **transport 层**打补丁（`vcr/stubs/` 下有 `httpx_stubs.py` 等），与客户端库解耦；源码注释明确：要 patch 的 httpx 模块是**传进来的**（`httpx` 或其 fork `httpx2`），所以一个 stub 同时服务两栈。这正是 httpx→httpx2 迁移中它**唯一活下来**的原因 |
| `respx` | 0.23.1 | 绑定 `httpx` 类型，PR #317「Support httpx2」至今 open → **用不了** |
| `pytest-httpx` | — | 硬钉 `httpx==0.28.*`，**直接排除** |

`vcrpy` 的已知风险（tech-stack §2）：8.3 有未修回归，重放 `reason_phrase` 为 null 的 cassette 会崩（issue #1028 / PR #1029 open）；SSE / 流式响应支持较弱，**本项目不用流式，影响可控**；cassette 必须配 `filter_headers` 去掉 API key。

**注意 §12 的待拍板项 ③**：若认为 `vcrpy` 的收益不足以抵消一个新 dev 依赖，可以只保留 `ResponsePool` + `httpx2.MockTransport`，完全不引入 vcrpy。代价是失去"防上游 API 漂移"的能力。本任务的两层设计（`ResponsePool` 自研 + `vcrpy` 留在 `tests/`）就是为了让这个决定可以在不改动产品代码的前提下做出。

## 5. 工程化思想

**"录制原始响应"与"采样 N 个响应"是两个不同的问题，混在一起会同时做不好。** 前者要的是**同一次调用的忠实副本**（防上游漂移），后者要的是**同一请求的多个不同样本**（测采样方差）。它们在数据形态上就冲突：一个 key 对一个响应，vs 一个 key 对一列响应。把这两件事塞进一个叫作 `CassetteStore` 的类里，最终会得到一个既要满足 `allow_playback_repeats` 又要满足采样语义的四不像。**当两个需求的语义在数据结构层面就冲突时，正确做法是拆组件，而不是加参数。**

**哈希 key 时必须用 canonical 形式，而不是语言内置的快捷方式。** `hash()` 有进程随机化、`str(dict)` 对顺序敏感、`repr()` 依赖实现细节——它们都能"跑起来"，但会在跨进程、跨版本、跨代码路径时静默失效。**判断一个 key 方案是否合格，只需问一句：同样的语义输入，在另一台机器上会得到同样的值吗？** 答案是否，用在录制回放场景里就是定时炸弹。

**正交维度用装饰器，不用继承。** 继承会把"是不是可录制"烧进类型结构，于是新增一种 provider 就要重写一遍录制逻辑，新增一种录制模式就要改所有 provider。装饰器让两个维度各自组合爆炸，而不是相乘。

**只有"池子空了"这种沉默失败，才需要把报错信息当作接口来设计。** 崩溃式的失败不需要解释，沉默的失败才需要——而且信息要正好指向可能的原因（没录 / prompt 变了），否则一次排查就是半小时。
