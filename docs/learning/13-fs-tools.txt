# 任务 13：文件工具（走 Executor）

> **所属里程碑**：M2 · **前置任务**：任务 12（`LocalExecutor` 与 `Executor` 协议）、任务 7（`ToolRegistry` 与工具协议） · **代码位置**：`src/harness/core/tools/fs.py`

## 1. 总体目标

给 agent 三个文件工具：`read_file` / `write_file` / `list_dir`。全部经过 `ws.executor`，**绝不直接触碰本地文件系统**。

**痛点一：路径越狱。** 工具的参数是模型生成的。模型可以、也确实会生成 `../../etc/passwd`、`C:/Windows/win.ini` 这类路径。如果没有 workspace 边界，`read_file` 就是一个任意文件读取漏洞——而且它跑在评测服务里，读的是评测机的文件。计划对这个问题的定位很清楚：「路径越狱必须被拦 —— 这是沙箱边界的一部分。」

**痛点二：绕过抽象导致容器化失效。** 如果这里写 `Path(target).read_text()`，Docker 化的那天文件工具会**在宿主机上**读文件。任务 12 花了力气把执行边界收进 `Executor`，这里绕过它，前面就白做了。

**为什么是「两层防线」而不是一层。** 任务 13 做的是**静态边界**（这条路径在不在工作目录里，与调用者是谁无关），任务 19 的 `PermissionMW` 做的是**动态策略**（这个 agent 此刻允不允许读文件）。两者不可互相替代：静态边界不知道预算与策略，动态策略挡不住绝对路径。**防线分层的前提是每层都能独立说清自己拦什么。**

**另一个不显眼但重要的目标：错误也不许抛异常。** 文件不存在、路径越狱、目标是目录——全部返回 `ToolResult(ok=False, error_type=...)`。因为 `ToolResult` 是 agent 的输入，异常会打断整条对话的因果链（对应设计文档 §6 R3 的配对完整性要求）。

## 2. 实现流程

1. **先写 7 条测试**：正常读、文件不存在、`../..` 越狱、绝对路径越狱、写读往返、写时建父目录、`list_dir` 用 `/` 标注目录。为什么越狱两条要第一批写：**安全属性必须先有失败用例**，否则「实现完再补测试」永远补不到边界上。
2. **实现 `_resolve_in_workspace(ws, rel)`** —— 单一入口，三个工具共用。
3. **依次实现三个工具**，schema 统一形态（`name` / `description` / `parameters` / `required`）。
4. 跑 `tests/core/tools/`。

**为什么路径解析必须先于所有工具存在：** 如果每个工具各自写一遍解析，出现一处漏判的概率就乘以 3；而且安全逻辑的 review 面从 1 个函数变成 3 个。**安全属性不允许「每个实现各自正确」，只允许「共用同一个实现」。**

## 3. 具体技术实现

### 归一化之后再比对，不要做字符串前缀比较

```python
root = Path(ws.root).resolve()
target = (root / rel).resolve()
if not target.is_relative_to(root):
    return None
```

`resolve()` 会把 `..` 与符号链接展开成最终绝对路径，因此 `root/../../etc/passwd` 会被展开到 root 之外，`is_relative_to` 如实报 false。

**反例**：`str(target).startswith(str(root))`。看着更简单，但它比较的是字符串而不是路径分量——`root = C:/ws` 时，`C:/ws_evil/secret` 会通过前缀检查。**路径比较必须按分量，不能按字符串**；`pathlib` 的 `is_relative_to` 就是为这件事存在的标准库方法。

**绝对路径为什么天然被拦：** `root / "C:/Windows/win.ini"` 的结果是 `C:/Windows/win.ini`——pathlib 在右操作数是绝对路径时会丢弃左边。于是它经同一条归一化逻辑被判出界，不需要额外写「是不是绝对路径」的检查。**一条判定规则覆盖两类攻击，比两条规则各覆盖一类更不容易出漏洞。**

### 失败即拒绝（fail closed）

`resolve()` 可能抛异常（非法路径、Windows 上的保留名或超长路径）。计划用 `try/except (OSError, ValueError): return None` 兜住，于是异常路径的默认答案是「拒绝」。

**这是安全默认值的教科书案例**：安全判定只有两种默认值，fail closed（解析不了就拒绝）与 fail open（解析不了就放行）。任何 fail open 的兜底写法都会变成漏洞，因为攻击者最喜欢的就是「让解析器出错」这一路径。**注意 `resolve()` 默认 `strict=False`**，对不存在的文件也能工作——`read_file` 一个不存在的路径仍然走同一条归一化逻辑，不会在解析阶段就抛 `FileNotFoundError`。

### 错误分类进 `error_type`，不要只写进 message

| `error_type` | 含义 | 为什么要单独区分 |
|---|---|---|
| `path_escape` | 越狱被拦 | **这是行为信号**：agent 试图访问工作目录之外，可能是 prompt injection 的结果，也可能是它对任务边界的误解 |
| `not_found` | 文件不存在 | 模型能力信号：拼错了路径、或对仓库结构有错误假设（对应 FailureClassifier 的「工具参数幻觉」模式） |
| `is_directory` | 目标是目录 | 参数类型错误 |

把 `path_escape` 和 `not_found` 都写成一句 message 字符串，就等于丢掉了这两类信号——评测器只能做字符串匹配，脆弱且不可维护。**任何要被评测消费的错误信息，都必须有稳定的机器可读标识。**

### 编码策略：宁可替换字符，不要抛异常

`data.decode("utf-8", errors="replace")`。工具的输出是给模型读的；一个 `UnicodeDecodeError` 会让整次调用失败，而替换字符只让几个字节失真。**工具永远要给 agent 一个可用结果**，这是「工具」与「断言」的区别。

### 文件系统语义归 Executor，工具只管参数与边界

`write_file` 不自己建父目录——任务 12 的 `LocalExecutor.write_bytes` 里已经有 `p.mkdir(parents=True, exist_ok=True)`。工具不该重复实现「写文件」的语义，否则容器实现和本地实现的行为会漂移。**判断一个职责该放哪层：问「换一个执行器，这个行为该不该变」**——该变（路径怎么映射、父目录建不建）就归 Executor；不该变（越狱判定、错误分类）就归工具。

### 输出格式是给模型看的，稳定性影响可比性

`list_dir` 的约定：目录 `name/`、文件 `name (123B)`、空目录 `(empty)`。这是给模型读的文本，同时也会进 `ToolResultEvent.content` 并参与 golden 轨迹的比对（任务 25 的 `arg_normalizers` 与 golden 生成共用归一化代码，就是为了防止「生成时归一化了、匹配时没归一化」的不一致）。**格式一旦定下，改动要同时考虑模型的理解和评测的可比性。**

### `description` 是 prompt 的一部分

`"Read a UTF-8 text file relative to the workspace root."` 里的 "relative to the workspace root" 不是注释，是给模型的约束提示。**工具的 `description` 与 `schema` 是 agent 能看到的唯一文档**——把限制写在这里，比在失败时反复纠正便宜得多。

## 4. 使用的技术栈简介

**本任务零第三方依赖**，全部是标准库：

| 能力 | 说明 |
|---|---|
| `pathlib`（`Path` / `resolve` / `is_relative_to`） | `is_relative_to` 是 Python 3.9 起提供的方法；`resolve()` 在 `strict=False`（默认）下对不存在的路径也能归一化 |
| `typing.Protocol` | `Tool` 协议（任务 5）是 `@runtime_checkable` 的结构化类型：任何有 `name` / `description` / `schema` / `invoke` 的对象都算工具，所以三个工具类**不需要继承任何基类**，测试里也用 `_StubTool` 这种临时类即可（任务 7 的注册表测试就是这么做的） |
| `asyncio.to_thread` | 在 Executor 内部使用，工具层看不到 |

工具层「薄」本身就是收益：工具不引入生态，替换成本为零，也不会把第三方库的生命周期绑进评测 harness。相比之下，`run_command`（任务 14）涉及的正则与子进程同样来自标准库——**本项目所有工具加起来没有引入任何一个新依赖**。

测试侧：`pytest` + anyio marker。测试里的 `_WS` 假工作目录只提供 `root` / `executor` / `keep` 三个属性——**这正是 Protocol 化的收益：文件工具的单测不需要真实 `Workspace`（那是任务 15）**，依赖面越窄，测试越快越稳。

## 5. 工程化思想

- **失败即拒绝。** 任何「解析不了就按原样用」的兜底都是在制造漏洞。这条能迁移到所有安全判定：权限检查、路径校验、反序列化、签名验证——**默认值必须是拒绝，放行必须是显式判定的结果**。
- **边界只写一次。** 安全逻辑收敛到一个函数，review 面从 N 降到 1，漏判概率也从「每处分别正确」变成「一处正确即可」。**凡是被多个调用点共享的安全属性，都不允许各自实现。**
- **抽象的价值在于替换点，不在于「看起来解耦」。** 判断标准：能不能写一个只会抛 `NotImplementedError` 的 Docker 实现，并且不需要改任何工具代码？任务 12 的 `docker.py` 空壳就是对这条的检验。
- **错误类型是接口的一部分。** `error_type` 是机器可读枚举，不是日志文案；它决定了下游能做什么归因。**设计错误信息时先问「谁会消费它、拿它做什么决策」**，再决定粒度。
- **工具的 description 是产品文案，不是注释。** 模型按它决定怎么调用，所以它属于 prompt 工程：写清边界（relative to the workspace root）、写清后果（Network access is disabled）、写清参数形状（Pass argv as a list）。**在接口里表达约束，比在失败后反复纠正便宜一个数量级。**
- **宁可降质也不要失败。** 编码替换字符、截断保留头尾、目录列为文本——工具层的每一次「降级」都是为了保住 agent 的因果链。**在 agent 系统里，一次可用但不完美的观察，胜过一次干净利落的异常。**
