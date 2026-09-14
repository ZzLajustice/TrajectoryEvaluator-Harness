# 任务 23：评测器框架与 `evalrunner`

> **所属里程碑**：M5 · **前置任务**：2（事件模型）、3（`Trajectory` 只读视图）、5（`EvalResult` / `EvalContext` / `JudgeClient`） · **代码位置**：`src/harness/evaluators/base.py`、`src/harness/orchestration/evalrunner.py`

## 1. 总体目标

把"评测器"从一个函数升级成一个有**契约**的角色：它自己声明关心哪些事件，调度器据此决定谁该跑；它只认识 `events` 与 `contracts`，不知道 agent 的存在。

三件事必须同时成立，缺一个整套架构就不成立：

| 要成立的事 | 不成立的后果 |
|---|---|
| 评测器**不 import `core`** | `core` 依赖 `events`、评测器依赖 `core` 的 `Run` → 依赖成环，设计文档 §2.2 的 L0 叶子层失效 |
| 评测器**能触发 judge** | 没有 agent-as-a-judge，`FailureClassifier` 的 LLM 兜底与 `MetaEvaluator` 都做不了 |
| 评测器**声明式订阅** | 调度器只能"全都跑"，成本与噪声都上去，抽象退化成文档 |

前两条表面互斥：要能用 judge，就得碰组装层；要不依赖 `core`，就不能碰组装层。设计文档 §2.3 用**依赖倒置**让它们同时成立——评测器依赖 `contracts` 里的 `JudgeClient` 协议，真实实现由 `orchestration` 层注入（任务 35）。

第三条是本任务的隐藏考点：**声明式订阅必须是可验证的真行为**，不是一句注释。

## 2. 实现流程

1. 定义 `BaseEvaluator`：三个类属性 `name` / `version` / `subscribes: frozenset[EventType]`，加 `evaluate()` 与 `skipped()` 辅助方法。
2. 写 `run_evaluators(evaluator_classes, traj, ctx)`：
   1. 循环外一次性算出 `present = {e.type for e in traj.events}`；
   2. 对每个类做 `cls.subscribes & present`，交集为空就 `continue`——**在实例化之前**；
   3. 命中才 `cls()`、才调 `evaluate()`；
   4. 结果 `inspect.isawaitable` 则 await，让同步/异步评测器共用一条路径；
   5. 任何异常兜底成 `EvalStatus.ERROR` 的结果，不向上抛。
3. 跑测试：全绿，其中"跳过"用例断言 `_Spy.calls == 0`（计划正文写的是"6 passed"，但给出的测试片段只有 5 条 `test_` 函数，实现时以实际收集到的用例数为准）。

顺序上有两处不能动：

- **`present` 必须在循环外算**。它是循环不变量——表达的是"这条轨迹里有什么"，不是"这个评测器看到了什么"。放进循环里就是重复扫描。
- **订阅判断必须在 `cls()` 之前**。这是全章的核心。若写成"先实例化，再决定调不调用"，`subscribes` 就只是个提示，调度器照样要为每条轨迹构造全部评测器对象；更糟的是，这种写法在**行为上无法与正确写法区分**——评测器该跳过时依然跳过，只是白构造了一次。测试里的 `_Spy` 用类变量同时记 `instances` 与 `calls`，实现里 `continue` 出现在构造之前，于是"没被调用"和"没被实例化"是同一件事实。

## 3. 具体技术实现

### 订阅判定是一行集合运算，但语义有三层

```python
present = {e.type for e in traj.events}
if not (cls.subscribes & present):
    continue
```

- `subscribes` 是 `frozenset[EventType]`，取交集非空即"命中"。
- **默认值 `frozenset()` 意味着永不命中**。一个忘了声明 `subscribes` 的评测器不会被调度器调用，且**不报错、不出现在结果里**——它会安静地消失。这是抽象给你的一份"沉默的失败"，写新评测器时第一件事就是声明订阅。
- `present` 来自轨迹**实际出现过**的事件类型，不是用例声明"要检查什么"。所以一条被 `PermissionMiddleware` 全部拦下的轨迹（没有 `TOOL_RESULT` 之外的任何东西）不会唤醒 `GroundingChecker`。

### 四态契约：`SKIPPED` / `PASS` / `FAIL` / `ERROR`

`EvalStatus` 有五个值，其中四个的边界必须钉死：

| 状态 | 含义 | 谁负责产生 |
|---|---|---|
| `SKIPPED` | 订阅的事件不在轨迹里，或输入不足以判定 | 评测器自己（走 `self.skipped()`） |
| `PASS` / `FAIL` / `WARN` | 评测结论 | 评测器自己 |
| `ERROR` | **评测器自身**崩了 | 调度器兜底 |

`ERROR ≠ FAIL` 是评测可信度的底线：**评测器有 bug 不能被记成"被测 agent 失败"**。这条约束反过来也成立——评测器对退化输入（空轨迹、缺 `RUN_END`、悬空配对）必须返回 `SKIPPED` 或 `FAIL`，绝不能抛异常，因为抛出去就是 `ERROR`，而 `ERROR` 在报告里既不是通过也不是失败，统计时会变成第三种东西。

### 评测器只认协议：`JudgeClient` 的依赖倒置

```
evaluators/  ──依赖──▶  JudgeClient (Protocol, 住在 contracts/)
                              ▲
                              │ 实现
                     orchestration/judge.py（组装层）
```

需要 judge 的评测器只做两件事：从 `EvalContext` 里取 `ctx.judge`，调用 `await ctx.judge.judge(case, repeat=n)`。它**看不到** `Run`、`RunSpec`、`BudgetGovernor`、provider——那些全在协议另一侧。

这条路径不是靠自觉维持的：`tests/test_architecture.py` 用纯 `ast` 遍历 `evaluators/` 下每个文件，只要出现 `harness.core` / `harness.orchestration` / `harness.providers` / `harness.store` 前缀的 import 就断言失败，并在报错信息里直接指向替代方案（"Use JudgeClient from harness.contracts.protocols instead"）。`import-linter` 的 `forbidden` 契约在 CI 再查一遍——两条不同粒度的检查，刻意冗余（tech-stack §9）。

### 同步与异步共用一条路径

```python
result = instance.evaluate(traj, ctx)
if inspect.isawaitable(result):
    result = await result
```

用 `inspect.isawaitable` 而不是 `asyncio.iscoroutine`：前者认 `Future`、认实现了 `__await__` 的对象，后者只认原生协程。设计文档 §3.5 明确"同步返回或协程均可——纯函数评测器无需 async 样板"，这样 `TrajectoryMatcher` / `EfficiencyAnalyzer` 这类纯规则评测器可以老老实实写成同步函数。

### 两个实现细节

`duration_ms` 由调度器回填：`result.duration_ms = int(...)`。注意 `EvalResult` 是可变的，而 L0 的事件模型是 `frozen=True` 的——调度器需要补一个它事先不知道的字段，这是两层的合理差异。

异常路径也要计时，且顺序是先构造再 `append`：

```python
except Exception as exc:
    out.append(EvalResult(..., duration_ms=int((time.monotonic() - started) * 1000)))
    continue
```

`except Exception` 而不是 `BaseException`：结构化并发下的取消（`CancelledError`）必须继续向上传播，不能被评测器调度层吞掉（同任务 20 的约束）。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| `pydantic` 2.13（tech-stack §3） | `EvalResult` / `Finding` / `EvidenceRef` 都是 pydantic 模型；落盘走 `model_dump_json()`（Rust 实现，比 `json.dumps(model_dump())` 快且类型安全） |
| `frozenset`（stdlib） | 订阅集合用不可变集合：类属性层面的常量，且交集运算天然适合"任一命中即订阅" |
| `inspect`（stdlib） | `isawaitable` 统一同步/异步两种评测器，避免为纯规则评测器强制 `async` 样板 |
| `import-linter` 2.15（tech-stack §9） | `forbidden` 契约一条就够表达"评测器不得依赖 core"；与 `tests/test_architecture.py` 的 ast 断言刻意冗余 |
| `anyio` pytest 插件（tech-stack §4.2） | `pytest.mark.anyio`；inspect_ai 同样不用 `pytest-asyncio` |
| 无新依赖、零 LLM 调用 | 框架层本身不碰任何 provider |

## 5. 工程化思想

**依赖倒置是打破循环依赖的标准手法，判断标准是"依赖方向与 import 方向相反"。** 当 A 需要调用 B、B 又需要 A 提供的东西时，直觉方案是把 A 和 B 缝在一起，结果是两块再也分不开。正确做法是把**接口**下沉到比两者都低的层（这里是 `contracts`），让 A 依赖接口、B 实现接口——`evaluators` 依赖 `JudgeClient`，`orchestration` 实现 `JudgeClient`，两者之间没有任何一条 import 边。可迁移的检查方式：画出 import 图，如果出现环，问"这个环上哪条边可以换成协议"。

**声明式声明依赖优于运行时判断。** "我需要 `CONTEXT_COMPACT` 事件"是评测器的**静态属性**，不是运行时才知道的事。写成类属性后，宿主可以据此做调度决策（跳过、不实例化），而不是只能事后判断"这个结果该不该信"。可迁移：任何插件体系，让插件声明它的**输入前提**，宿主就能裁剪工作集、给出"为什么没跑"的解释，而不是让插件自己在运行时 return 一个空结果。

**"没执行"必须与"执行失败"和"执行成功"三态分开。** 把 `SKIPPED` 并进 `PASS` 会让缺数据的用例看起来是绿的；并进 `FAIL` 会让模型背不属于它的锅。测试框架里 `skip` 与 `fail` 的区分、监控里 `no_data` 与 `ok` 的区分，都是同一条。评测系统尤其致命，因为它的输出直接决定"这个模型行不行"。

**契约的边界值比主干更值得写测试。** 本任务的测试里只有一条测 happy path，其余全在测跳过、异常、同步/异步与默认值。主干逻辑一眼就对，真正会写错的是"订阅的事件不存在时返回什么"。

**架构约束要变成可执行的断言。** 一句"评测器不许依赖 core"写在文档里，三个月后必然有人违反，而且违反时不会有人发现。把它写成 ast 断言，违规者当场红——文档的约束力来自"违反了会失败"，而不是来自"写下来了"。
