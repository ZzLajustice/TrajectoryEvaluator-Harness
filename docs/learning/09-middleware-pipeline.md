# 任务 9：洋葱管道

> **所属里程碑**：M1 · **前置任务**：任务 5（`contracts/protocols.py` 的 `Middleware` 协议与 `ToolResult` 值对象） · **代码位置**：`src/harness/core/pipeline.py`、`src/harness/core/middleware/__init__.py`、`src/harness/core/middleware/base.py`

## 1. 总体目标

把「工具调用前后要做什么」从 agent loop 里彻底剥离出来，变成一条可插拔的中间件链。

**痛点一：核心 loop 会被污染。** 权限校验、沙箱检查、预算扣减、遥测埋点、策略拒绝——这五件事如果写成 `loop.py` 里的 if/else，评测逻辑就住进了执行逻辑里。设计文档 §3.4 把话写死了：**「评测埋点、越权检测、预算控制全是插件，核心 loop 里不得出现任何评测代码。」** 这条不是代码风格，而是架构支点：`evaluators` 包之所以能完全不 import `core`（import-linter 的 `forbidden` 契约），前提就是评测能力不寄生在 loop 上。

**痛点二：顺序语义错了，整套防线失效。** 计划注释写得很直白：顺序语义错了，评测埋点、越权检测、预算控制全部失效。规范顺序是 `TOOL_CALL → Permission → Sandbox → Budget → Telemetry → Policy → Executor → TOOL_RESULT`。如果顺序反了，最外层的会变成 Policy，于是 Permission 拦下的调用仍然扣了预算、埋了点；更糟的是 Sandbox 落到 Executor 里面，检查发生在进程已经起来之后——等于没有检查。

**为什么它必须在 M1 就做。** 任务 10 的 `RunContext.__init__` 要调用 `build_pipeline` 构建整条链；管道没定型，`Run` 无法定型，垂直切片就跑不起来。它是「中间件设计的地基」这句话的字面含义。

## 2. 实现流程

1. **先写顺序回归测试。** 用一个只往 `list` 里记日志的 `Recording` 中间件，断言 `["a.before", "b.before", "executor", "b.after", "a.after"]`。为什么先写这条：顺序是本任务唯一容易写错又最致命的东西，而它只有在多层同时存在时才暴露出来（单层测试对顺序不敏感）。这条测试是唯一能防住未来重构把 `reversed` 删掉的机制。
2. **定义 `PrePostMiddleware` 基类**（`before` / `after` 两个钩子 + 默认 `handle` 模板方法）。覆盖 80% 场景（前置检查 + 后置加工），需要短路或重试的中间件自己实现 `handle()`。
3. **实现 `build_pipeline`**：`reduce(wrap, reversed(middlewares), terminal)`。
4. **补三类边界测试**：空列表（直接调 terminal）、短路（terminal 不被调用）、异常传播（管道不吃异常）。
5. **把状态作用域规约写进 `middleware/__init__.py` 的 docstring**（见 §5）。

顺序理由：第 1 步先于第 3 步是 TDD 的常规要求；第 4 步必须在管道定型前完成，因为边界用例定义的是「管道该不该吞掉异常」「短路返回什么」这类语义，而这些语义一旦被实现固化就很难回头改。

## 3. 具体技术实现

### `reduce` 为什么能得到「`middlewares[0]` 在最外层」

`functools.reduce(f, [x1, x2, x3], init)` 的语义是左折叠：`f(f(f(init, x1), x2), x3)`。每调用一次 `f` 就得到一个「已经包好的 handler」，它成为下一次调用的输入。

```python
def wrap(nxt, mw):
    async def handler(ctx):
        return await mw.handle(ctx, nxt)
    return handler

return reduce(wrap, reversed(middlewares), terminal)
```

传入 `reversed(middlewares)` 得到 `[mN-1, ..., m1, m0]`，于是最后一次 `wrap` 处理的是 `m0`，而它产出的 handler 就是最终返回值——**最外层**。如果不 `reversed`，数组里第一个元素反而会被包在中心，配置写 `[Permission, Sandbox, ...]` 却得到 Policy 在最外面的管道。

### 反例：递归 `call_next` 的闭包捕获陷阱

网上常见的洋葱写法是循环里反复重新赋值一个 `nxt` 变量：

```python
# 反例（示意）：所有闭包共享同一个名字空间
handler = terminal
for mw in reversed(middlewares):
    handler = lambda ctx: mw.handle(ctx, handler)
```

Python 的闭包是**晚绑定**的：`lambda` 体里的 `mw` 与 `handler` 只在被调用时才求值，那时循环早已结束，两个名字都停在最后一次迭代的值上。结果是每层都调用同一个中间件，且 `handler` 自己引用自己——无限递归。

`reduce` 版本不存在这个问题，因为 `nxt` 和 `mw` 是 `wrap` 的**形参**：每次调用 `wrap` 都创建新的栈帧和新的闭包作用域，捕获的是当时传入的对象。**「把变量变成参数」是把晚绑定变成早绑定的标准手法**，识别出这个模式比记住 `reduce` 的写法更有迁移价值。

### 短路必须返回完整结果对象

`PrePostMiddleware.handle` 里 `before` 返回非 `None` 即直接 `return`，不 `await nxt(ctx)`，所以 terminal（Executor）根本不会被调用。关键约束是：**短路返回的必须是完整的 `ToolResult`（带 `call_id` / `name`），不能是 `None`。**

设计文档 §6 R3 对应的是：「短路时也必须发 `TOOL_RESULT` 事件，否则评测器看到『有 `TOOL_CALL` 无 `TOOL_RESULT`』的悬空配对」。悬空配对会直接打坏两类评测器：`TrajectoryMatcher` 的工具序列匹配、`GroundingChecker` 的 `result_for(call_id)` 查询（任务 3 的 O(1) 索引查不到东西时只能返回 `None`，评测器无从判断是「没被调用」还是「被拦了」）。

### 异常穿透，责任在调用方

管道本身**不捕获任何异常**（`test_exception_propagates_through_chain` 断言 `RuntimeError` 一路冒到调用者）。捕获的责任留给 `RunContext.invoke_tool`（任务 10），由它把异常转成 `ToolResult(ok=False, error_type="sandbox_error")`。

这样分工的理由：管道只负责「组合」，它不知道 `ToolResult` 该填什么错误语义；而调用方知道这次调用处在 run 的哪个阶段、该不该继续。**组合器不应该有业务判断。**

### 只构建一次

`RunContext.__init__` 里构建一次，整个 run 复用。每次调用重建不仅浪费分配，更会把「管道结构」这个不变量从构造期漂移到调用期——而管道结构是安全属性，它必须是一个不随调用变化的事实。

## 4. 使用的技术栈简介

本任务**纯标准库**，一个第三方依赖都没有。涉及的几项能力：

| 能力 | 说明 |
|---|---|
| `functools.reduce` | 左折叠，函数式组合的基础工具。等价手写 `for` 循环更啰嗦且容易写错初始值 |
| `typing.Protocol` + `@runtime_checkable` | `Middleware` 是结构化类型（任务 5 定义）。任何有 `name` 与 `handle` 的对象都算中间件，测试里可以注入临时类，不必继承基类 |
| `async` / `await` | 管道层唯一的异步需求是「处理函数是可等待的」，`TypeVar` 把 `Handler[Ctx, Out]` 参数化即可，不依赖任何异步框架 |

关于异步原语的选择：技术选型文档 §4.1 倾向 `anyio`（`openai` / `anthropic` SDK 共同依赖它，inspect_ai 核心依赖 `anyio>=4.14`，该版本修了 asyncio Lock/Semaphore 在取消后的 deadlock），但 §12 ① 把它列为**待拍板项**，本计划的实现代码用的是 stdlib `asyncio`。切换成本很低（`anyio.Lock` ↔ `asyncio.Lock` 这类逐点替换），**管道层完全不受影响**——它只要求处理函数返回 awaitable，不碰任何锁或信号量。

测试侧：`pytest`（tech-stack §11 的约束是 `pytest>=8.4,<10`）。异步测试插件存在一处文档不一致：tech-stack §4.2 选的是 **anyio 自带 pytest 插件**（明确不装 `pytest-asyncio`，inspect_ai 同款，且能验证 trio 后端），而设计文档 §7 的配置写的是 `asyncio_mode = "auto"`（pytest-asyncio 的开关）。两者替换成本都很低（改一行 conftest），但实现前必须定一个。

## 5. 工程化思想

- **顺序语义要可断言，不要靠文档。** 「middlewares[0] 是最外层」这句话写在 docstring 里只能提醒人，写在断言里能拦住人。凡是影响**安全属性**的约定（谁在外层、谁先执行），都必须落成回归测试，否则它就不是约定，只是愿望。这条可以迁移到任何插件系统：中间件顺序、Web 框架的 handler 链、编译器的 pass 顺序。
- **把顺序表达成数据，而不是控制流。** 整条链是一个 list，`build_pipeline` 一行 `reduce` 消费它。好处是 review 安全策略时只需要读一行配置，而不是沿着嵌套的 if/else 在脑子里执行。**结构可见性是安全审查的前提。**
- **短路用「返回值」表达，不用「异常」表达。** 返回 `ToolResult(ok=False, denied_by="permission")` 与抛异常在评测系统里语义完全不同：前者是「agent 被正常拒绝了」（agent 行为信号），后者是「harness 出故障了」（系统故障信号）。用异常做控制流会把这个区分抹掉——这是本任务里最容易被忽略的设计点。
- **状态作用域必须显式化。** `ToolCallContext` 每次调用新建 → 调用级状态放 `ctx.scratch`；`Middleware` 实例在 run 内跨调用共享 → 只能持有 run 级状态（如 BudgetMW 的累计计数器）；**绝不允许中间件实例持有调用级可变状态**。这条规则没有类型系统能表达，所以它被写进包的顶部 docstring。**并发安全里「谁可以被共享」的规则，如果只能靠人记，就要放在最显眼的位置，再在 code review 清单里重复一遍。**
- **看「语义量 / 代码量」之比判断抽象是否切对了位置。** 整个管道 6 行代码，却承载了三条可断言的语义（顺序、短路、异常传播）。反过来，如果一个抽象有 200 行代码却说不清它保证了什么，那就是切错了。
