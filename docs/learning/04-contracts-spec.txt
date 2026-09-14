# 任务 4：`contracts/spec.py`

> **所属里程碑**：M1 · **前置任务**：任务 1 · **代码位置**：`src/harness/contracts/spec.py`、`tests/contracts/test_spec.py`

## 1. 总体目标

`RunSpec` 是"一次 run 要做什么"的**完整、可 JSON 序列化的描述**：角色（`RunRole`）、system prompt、模型引用、任务、工具策略、中间件配置、预算、工作区、最大轮数。它只有 pydantic 依赖，因此前置条件只是任务 1 的包骨架。

要解决的两个问题：

1. **评测器不能回查 suite 配置。** 评测器住在 `evaluators/`，只允许依赖 L0。若它需要知道"这次 run 用了什么模型、开放了哪些工具"就得去读 suite YAML——那一刻架构就破了（`evaluators` → `orchestration`）。解法是把完整 spec 快照存进 `RunStartEvent.spec_json`（设计文档 §3.1），评测器从轨迹里就能拿到上下文。
2. **两次 run 能不能比？** diff、baseline、回归判定都要回答"这两次跑的是同一个配置吗"。逐字段比不可靠（字段顺序、无关字段都会干扰），所以需要 `fingerprint()`：影响行为字段的 sha256。

## 2. 实现流程

1. 写 5 个失败测试：`model_dump_json` 可序列化 / fingerprint 稳定且对行为字段敏感 / fingerprint 忽略 metadata / 额外字段被拒 / `BUDGET_EXCEEDED` 与 `LLM_ERROR` 是两个值
2. 跑出 `ModuleNotFoundError`（红）
3. 实现 `spec.py`（`_Model` 基类 + 各组成模型 + `RunSpec.fingerprint()`）
4. 跑 `tests/contracts/` 5 passed（绿）
5. commit

顺序理由：**fingerprint 的 3 条测试必须在实现之前写完**。fingerprint 的正确性几乎全在"忽略哪些字段"这条规则上，而这条规则有两种错误方向：忽略太多（两次行为不同的 run 指纹相同 → 回归被漏报）、忽略太少（改个 metadata 指纹就变 → 每次跑都不可比）。两者都不是类型系统能发现的，只能靠测试钉死。

另外，`BUDGET_EXCEEDED != LLM_ERROR` 这条测试看着像废话，实际是**把设计决策写进测试**：预算耗尽要被 `FailureClassifier` 当成独立失败模式（"预算内未收敛"），而不是被归入"模型报错"。

## 3. 具体技术实现

**`_Model` 基类统一 `extra="forbid"`**：spec 是用户手写 YAML 的落点，字段名写错（`modle:`）必须立刻报错，而不是静默忽略后跑出一个"看着怪但没报错"的结果。

**`MiddlewareSpec` 只存 name + enabled + config，不存实例**——这是整份 spec 可序列化的前提。反例：`middlewares: list[Middleware]` 保存对象 → `spec_json` 无法落地 → 第 1 节的问题立刻复活。**可迁移判据：凡需要持久化的配置对象，只能持有数据，不能持有连接、实例、句柄、回调。**

**`fingerprint()` 的三个细节**：

| 细节 | 为什么 |
|---|---|
| `model_dump(mode="json")` + `json.dumps(..., sort_keys=True)` | dict 迭代顺序不稳定会让同一配置算出不同哈希，`sort_keys` 消除这个变量 |
| `exclude={"metadata", "keep", "keep_on_failure"}` | 它们不影响 agent 行为。"保留工作目录"影响的是调试体验，不是被测行为 |
| 手动 `data["workspace"].pop(...)` | `exclude` 只作用于顶层，嵌套模型里的同名字段不会被排除——**这是最容易漏的一处**，漏了就会出现"只改了 `keep_on_failure`，指纹却变了" |

**枚举全部用 `StrEnum`**：落进 JSON 是 `"sut"` / `"budget_exceeded"`，可直接与设计文档、报告、HTML 中的字符串对齐。

**`ToolPolicy.allow=None` 与 `allow=[]` 语义不同**：`None` = 全部允许，`[]` = 全部禁止。这类"未设置 vs 空集合"的区分必须在类型层表达（用 `| None` 而不是给个空列表默认值），否则使用者无法表达"一个工具都不给"。**这是配置 API 最常见的陷阱之一**，最后一道防线（`deny`）与幻觉工具检测都依赖它。

**`RunStatus` 是终态枚举而非布尔**：`OK` / `NO_FINISH` / `MAX_TURNS` / `BUDGET_EXCEEDED` / `POLICY_TERMINATED` / `LLM_ERROR` / `SANDBOX_ERROR` / `TIMEOUT` / `CANCELLED` / `IMPORTED`。把 `success: bool` 换成枚举，下游才能做失败模式分布统计——这份统计本身就是过程级评测的输出。

## 4. 使用的技术栈简介

| 技术 | 说明 |
|---|---|
| `pydantic` 2.13 `BaseModel` | spec 的载体：字段校验、`model_dump_json()`（Rust 实现，比 `json.dumps(model_dump())` 快且类型安全）、`model_copy(update=...)` |
| `ConfigDict(extra="forbid")` | 拒绝未声明字段 |
| `hashlib.sha256` | 指纹，取前 16 位十六进制：对本场景（几百次 run 的内部比对）碰撞概率可忽略，可读性更好 |
| `pydantic-settings` 2.15.x | 与 spec 同层的配置来源（API key / base_url 覆盖），沿用 deepeval 的选择 |

替代品：`dataclass` + 手写 `to_dict`（无校验、无 `model_copy`）；`msgspec`（更快，但引入第二套模型体系，tech-stack §3 已排除）。选 pydantic 的核心原因是**它用同一份声明同时解决了"校验用户输入"与"输出 JSON"两件事**——这正是 spec 需要的。

## 5. 工程化思想

**（1）"可完整序列化"是一条极强的设计约束。** 只能活在内存里的对象，其信息只在当前进程有效；能完整序列化意味着它可以存进事件、写进报告、进 diff、跨进程传递。**反过来说：判断"这个对象该不该持有那个依赖"时，先问"它要不要被存下来"。**

**（2）可比性靠"显式声明不参与比较的字段"。** 默认全参与 + 白名单排除，比默认全排除 + 白名单包含更安全——前者漏排除的后果是"看起来不可比"（噪音，人会发现），后者漏包含的后果是"看起来可比但实际不同"（静默的错误结论）。**这个方向性判断可以迁移到任何缓存键、去重键、baseline 比对的设计上。**

**（3）把失败模式做成枚举，而不是布尔。** `success: bool` 丢掉了"为什么失败"，而这恰是过程级评测的全部意义。枚举的成本是几个常量，收益是下游能做分布统计、能按模式聚合、能在报告里画图。**判据：如果一个布尔字段未来会被用来回答"为什么"，现在就给它枚举。**

**（4）"空值 vs 空集合"必须在类型上可区分。** `None` 与 `[]` 承载不同语义时，不要用同一个默认值糊过去——类型是唯一无歧义的说明，尤其在靠 YAML 手写配置的系统里。
