# 任务 19：Permission / Policy / Sandbox 中间件

> **所属里程碑**：M4 · **前置任务**：任务 9（洋葱管道与 `Middleware` 协议）、任务 5（`ToolResult` 值对象） · **代码位置**：`src/harness/core/middleware/{permission,policy,sandbox}.py`

## 1. 总体目标

在 `TOOL_CALL` 与 Executor 之间插入三道独立的守卫，把"越权"变成**可被评测的数据**：

| 中间件 | 管什么 | 判定依据 | `denied_by` |
|---|---|---|---|
| `PermissionMiddleware` | 这个工具**能不能调** | `ToolPolicy.allow` / `deny`（名单） | `permission` |
| `SandboxMiddleware` | 这个**参数能不能传** | 路径参数是否越出 workspace | `sandbox` |
| `PolicyMiddleware` | 参数**内容违不违规** | `config["rules"]` 的声明式规则 | `policy` |

为什么必须是中间件而不是写进工具里：设计文档 §3.4 的硬约束是"**核心 loop 里不得出现任何评测代码**，评测埋点、越权检测、预算控制全是插件"。越权检测如果散落在 `read_file` / `write_file` / `run_command` 里，那么（a）新增一个工具就要记得再写一遍，（b）失败模式只能靠日志还原，进不了事件流。

而被拒本身是有价值的信号：设计文档 §3.1 决策 2 把 `POLICY_DENY` 提升为**一等事件**——"越权被拦截后的降级行为"是真实的失败模式，模型撞了墙之后是换路径还是死磕，是过程级评测最想看的东西之一。

## 2. 实现流程

1. **先写失败测试**（8 条），重点在最后一条：被拒绝时返回的必须是一个**带 `call_id` 的 `ToolResult`**，而不是 `None` 或异常。
2. **跑测试确认失败**（`ModuleNotFoundError`）。
3. **写三个中间件**，顺序上先写最内聚的 `SandboxMiddleware`（它只依赖路径逻辑），再写 `PermissionMiddleware`（依赖 `ctx.spec.tools`），最后写 `PolicyMiddleware`（依赖 `config["rules"]` 解析）——复杂度递增。
4. **跑测试验证通过**：8 passed。
5. Commit。

顺序上的关键约束是**第 1 步的最后一条测试**。如果先写实现，很容易顺手写成 `return None` 或 `raise PermissionDenied`——两种写法都能"拦住"，且单测实现时看起来都合理。先把"拒绝必须是一个结果"钉成测试，实现才会自然走对路径。

## 3. 具体技术实现

### 短路必须产出 ToolResult，不能产出"没有结果"

```python
if denied:
    return ToolResult(call_id=ctx.call.call_id, name=name, ok=False,
                      error=f"tool {name!r} not permitted (allowed: {allowed})",
                      error_type="permission_denied", denied_by="permission")
```

这是设计文档 R3 的第一条：**短路时也必须发 `TOOL_RESULT` 事件**，否则评测器看到「有 TOOL_CALL 无 TOOL_RESULT」的悬空配对。`ToolCallContext` 里的 `call_id` 必须原样带出，因为它就是配对的键（`Trajectory.result_for(call_id)` 靠它做 O(1) 查找）。

同理，拒绝的语义要**结构化**：`error_type` 是机器可读的分类（`permission_denied` / `policy_denied` / `path_escape`），`denied_by` 是拦截者。三个值分开，`FailureClassifier` 才能区分"模型越权了"和"工作目录配错了"。

### 三层不同的判定逻辑

**Permission** 是名单逻辑，一行就够——但有个细节：`allow` 为 `None` 表示"全部允许"，所以不能写成 `name not in (policy.allow or [])`，那会把"没配 allow"误判成"什么都不允许"。正确写法是显式区分 `None`：

```python
denied = (policy.allow is not None and name not in policy.allow) or name in policy.deny
```

**Sandbox** 的关键在路径解析顺序：**先 `resolve()` 再比较**。

```python
target = (root / value).resolve()
if target is None or not target.is_relative_to(root):
```

反例是直接比较字符串前缀（`value.startswith("..")` 或 `str(target).startswith(str(root))`）：`..` 的多级变体、符号链接、绝对路径、以及 Windows 上的大小写与盘符差异都会绕过它。`resolve()` 把这一切折叠成真实路径，`is_relative_to` 再做包含判断。

还有一处是 **fail-closed**：`resolve()` 抛 `OSError` / `ValueError`（非法路径、路径过长）时，代码把 `target` 置为 `None`，而 `None` 走的是拒绝分支。**判断不出来的时候拒绝，而不是放行**——安全边界的默认值只能是"否"。

**Policy** 是配置驱动的声明式规则，任一命中即拒：

```python
{"tool": "run_command", "deny_if_arg_contains": "pip install"}
{"tool": "*",           "deny_if_arg_matches": "rm -rf"}
```

规则放 `MiddlewareSpec.config` 而不写成代码，有一个不显眼但重要的后果：设计文档 §3.3 要求 `RunSpec` 能完整 JSON 序列化并塞进 `RunStartEvent.spec_json`。**策略如果是代码，历史轨迹就无法回答"当时生效的策略是什么"**——评测器回查不到，diff 也没法判断两次 run 可比不可比。

### 与工具层的刻意冗余

任务 13 / 14 的工具里也各有一份路径越狱检查。两处都在，是刻意的：

- **中间件层**能拦住**所有**工具的路径参数，包括未来新增的工具，且调用方不需要记得写。
- **工具层**能拦住绕过管道的直接调用（例如测试里直接 `ReadFileTool().invoke(...)`，或在别的编排路径上被人调用）。

两层覆盖的是**不同的调用路径**，不是同一路径的两次检查——这也是为什么它们不合并。

### 顺序：Permission 在最外层

设计文档 §3.4 规定 `Permission → Sandbox → Budget → Telemetry → Policy → Executor`。Permission 排最前，因为它是最便宜、最粗粒度的判定：被 allowlist 挡掉的 `write_file` 不应该先被报成 `path_escape`。**中间件的顺序决定了 `denied_by` 的归因质量**，这就是任务 9 里"顺序语义错了，越权检测全部失效"的具体含义。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| 无新依赖 | 三个中间件只用标准库（`re`、`pathlib`）与 `contracts` 的 `ToolResult` / `MiddlewareSpec` |
| `pathlib.Path.is_relative_to` | 路径包含判断，替代手写字符串前缀比较（本项目 Python 3.12） |
| `pydantic` 2.13（`MiddlewareSpec`） | 中间件只存**名字 + 配置**、不存实例（任务 4），这是 `RunSpec` 可完整序列化的前提 |
| `anyio` pytest 插件 | `pytestmark = pytest.mark.anyio` |
| `import-linter` | 这三个中间件在 `harness.core`，按分层契约只允许 import `events` / `contracts` |

## 5. 工程化思想

**"拒绝"必须是一种结果，不能是一种缺席。** 这是本任务最有迁移价值的一条。任何终止数据流的动作——网关拒掉请求、编译器报错、ETL 丢弃一条记录、队列拒绝入队——都要产出与正常路径**同构**的产物（这里是一个带 `call_id` 的 `ToolResult`）。如果拒绝表现为 `None` 或异常，下游就分不清"被拒绝了"和"根本没发生"，配对断裂、统计漏计，而且这种缺失是**沉默的**：轨迹看起来只是少了一条事件。把拒绝做成结果，数据流才是完备的。

**分层防线不是重复，是覆盖不同的路径。** 判断"两处检查是否冗余"的标准，不是"它们检查的内容是否相同"，而是"它们覆盖的调用路径是否相同"。相同内容的两次检查如果覆盖的是不同入口，就是必要的；否则才是浪费。

**安全边界的默认值只能是拒绝。** `resolve()` 失败 → `target = None` → 拒绝。反过来的写法（异常时放行、只拦已知的坏模式）在 happy path 上表现一样，只在真实攻击或异常输入下分叉——而那时已经来不及补。

**把策略外置成数据，历史才可复现。** 策略写进代码的代价，不在当下（代码跑得好好的），而在事后：半年前那条轨迹当时执行的是哪套规则？用 `config` 承载规则，`RunSpec` 就能把当时的策略一并冻进 `spec_json`，评测器回查即可。**任何"影响结果但可能变化"的东西，都应该随结果一起被记录。**
