# 任务 25：`TrajectoryMatcher`

> **所属里程碑**：M5 · **前置任务**：23（`BaseEvaluator` 与 `EvalStatus` 契约）、24（`TrajectoryBuilder`，测试输入源） · **代码位置**：`src/harness/evaluators/trajectory_match.py`

## 1. 总体目标

回答"agent 走的路对不对"——把过程本身变成可判定、可打分的对象。

只看 outcome 的评测有个盲区：隐藏测试通过与否，无法区分"一次做对"和"试错十二次最后蒙对"。而"与 golden 轨迹比对"这个直白做法，一下手就会撞上两个现实：

- **参数天然不确定**。同一份正确解法两次运行，命令串（`pytest -q` vs `pytest tests/ -q`）、文件路径（`/tmp/ws_abc123/a.py` vs `a.py`）、时间戳都不一样。逐字段全等比较，等于要求两次运行连临时目录名都相同。
- **正确路径不止一条**。代码修复任务里先 `search` 还是先 `read`、要不要多读一个文件、是否多跑一次测试，都是合法路径。单条 golden 会把好 run 判成失败。

于是这个评测器要做的事被拆成两个问题：**怎么算"顺序对了"**，和**怎么算"参数对了"**。设计文档 §4.1 把这两件事做成两个互不干扰的参数——`mode`（轨迹模式）与 `tool_args_match_mode`（参数匹配模式）。

## 2. 实现流程

1. 构造期校验两个模式参数，非法值当场 `ValueError`（与任务 27 的"配置错误在加载期暴露"同一条原则）。
2. `evaluate()`：
   1. 取 `actual`：轨迹里的 `(name, 归一化后的参数)` 序列；
   2. 取 `expected`：**同样走一遍归一化**；
   3. 空轨迹且 `expected` 非空 → `skipped()`；
   4. 按 `mode` 分派 `_matches()`，拿到 `(bool, list[Finding])`；
   5. 算 recall / precision 指标，组装 `EvalResult`。

顺序上有两条不能动：

- **归一化必须在匹配之前，且对两侧同时做**。只归一化 actual 会让比较偏向模型，只归一化 expected 会偏向 golden，两者都会产生系统性的假阴性/假阳性。代码里 `expected = [(n, self._norm(n, a)) for n, a in self.expected]` 这一行看着不起眼，但它决定了整套匹配是不是对称的。
- **`skipped()` 的判断在 `_matches()` 之前**。空轨迹 + 非空期望返回 `SKIPPED` 而不是 `FAIL`：agent 没调工具不代表它做错了（可能压根没跑起来），把它记成 `FAIL` 就是让模型背基础设施的锅——这与任务 23 的四态契约是同一条。

## 3. 具体技术实现

### 两个正交维度，20 种组合，5 + 1 个分支

| 维度 | 取值 | 语义 |
|---|---|---|
| `mode`（轨迹模式） | `strict` | 顺序与内容完全一致（长度也要相等） |
| | `unordered` | 工具调用集合相同，顺序任意 |
| | `subset` | actual ⊆ expected，白名单语义，**禁止多余调用** |
| | `superset` | actual ⊇ expected，允许探索性多余调用 |
| | `in_order` | expected 是 actual 的有序子序列 |
| `tool_args_match_mode` | `exact`（默认）/ `ignore` / `subset` / `superset` | 参数级别的包含关系 |

5 × 4 = 20 种组合，但代码只需要 5 个模式分支加 1 个 `_args_match()`。**正交拆解的价值就在这里**：新增"允许乱序但参数必须全等"的需求，只改一个参数；新增"命令串忽略大小写"的需求，只加一个 normalizer。若把组合枚举成 20 个模式字符串，每加一条约束都要乘一遍。

两个维度的方向约定是**一致的**，这一点必须记住而不是靠直觉：`subset` 表示"actual 被 expected 包含"，`superset` 表示"actual 包含 expected"。轨迹模式和参数模式都遵守它。参数名的方向性是多维配置里最容易写反的地方，唯一可靠的防御是给方向写一条断言——`test_subset_forbids_extra_calls`（actual 多了一个 `search` → `FAIL`，`findings[0].code == "trajectory.unexpected_tool"`）测的就是轨迹模式的方向。

### `in_order` 的迭代器写法

```python
it = iter(actual)
for e in expected:
    if not any(self._call_matches(a, e) for a in it):
        return False, [Finding(code="trajectory.order_violation", ...)]
```

`any(... for a in it)` 在命中时就停止消费迭代器，于是"只能往后找"这个子序列语义由迭代器本身保证，不需要手写下标和 `last_index` 变量。**反例**：对每个 expected 都在完整的 actual 列表里线性查找，然后判断"是否找到"——那样 `write, read` 会错误地匹配上 `[read, write]`（顺序反了却通过），因为查找不记得"已经走到哪儿了"。

### `unordered` 与 `superset` 的差别只在一步

两者都从 actual 里掏池子找 expected，区别是最后**是否检查池子被掏空**：`unordered` 要求集合完全相等（missing 与剩余都为空），`superset` 只要求 expected 全部命中。`subset` 则反过来——检查 actual 里有没有多余的。三个模式共用同一个"掏出匹配项"的动作，因此它们的差异天然只落在收尾条件上。

### `arg_normalizers` 是必须实现项，不是可选项

```python
r = TrajectoryMatcher(
    mode="strict", expected=[("read_file", {"path": "a.py"})],
    arg_normalizers={"read_file": lambda a: {"path": a["path"].rsplit("/", 1)[-1]}},
).evaluate(traj, EvalContext())
```

设计文档 §4.1 把它定性为"**必须实现项，不是可选项**"：代码修复类任务中命令串、文件路径、时间戳天然不确定，"没有这个机制整套匹配会极其脆弱"。

没有它会发生什么？两种都很难看：

1. **假阴性泛滥**——golden 里写 `a.py`，实际是 `<WS>/a.py`，明明是正确解法却永远判 `FAIL`。为压制噪声，只能把 `tool_args_match_mode` 调成 `ignore`；
2. **信息量归零**——`ignore` 只比工具名，于是"读了正确的文件"和"读了任意一个文件"不再有区别。参数匹配这一整个维度被放弃了。

正确的取舍是保留检查、在比较之前显式消解不确定性。还有一条设计文档点名的约束：**归一化函数与 golden 生成共用同一份代码**（`orchestration/golden.py::normalize_call`），避免"生成时归一化了、匹配时没归一化"——同一语义只能有一处实现，两侧各自维护必然漂移。

### 两个诊断指标

`tool_recall` 与 `tool_precision` 只比较**工具名集合**（归一化后的名字），不看参数。所以"参数全错但工具名都对"会得到 `recall = 1.0`。这是刻意的：指标用于看分布、找趋势（进了报告的成本-质量散点图），判定则交给 `findings`——不要拿这两个数字代替 `status`。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| LangChain `agentevals`（设计文档 §1.1 调研） | 轨迹匹配的模式命名来源——`strict` / `unordered` / `subset` / `superset`。它是调研中过程级做得最深但也只有 720 stars 的项目，覆盖单一维度 |
| 另一套命名：`exact` / `in-order` / `any-order` | 来自 **agentv** 项目，**不是** agentevals 的命名。写代码与写文档时不要混用，否则读的人会去翻错文档。本项目的第 5 个模式 `in_order`（有序子序列）正是从这套语义借来的（设计文档 §4.1） |
| `pydantic` 2.13（tech-stack §3） | `EvalResult` / `Finding` / `Severity` / `metrics`；`Findings.data` 是给"临时字段"用的逃生舱（设计文档 R2 第 2 层防线），不要为它改事件 schema |
| `Callable[[dict], dict]`（stdlib `typing`） | normalizer 的签名——参数进、参数出，无副作用，因此可以在匹配与 golden 生成两侧复用 |
| 零 LLM 成本 | 纯规则评测器，与 `EfficiencyAnalyzer` 一样属于"规则层覆盖约 80% 机械失败"的那一半（设计文档 §4.2） |

## 5. 工程化思想

**正交维度拆解优于模式枚举。** 当配置空间是 n × m 时，暴露两个独立参数永远优于枚举 n×m 个名字：前者的组合语义由代码保证，后者要靠人记住每一个名字意味着什么。可迁移的判据——如果两个约束**可以独立变化**（改顺序不影响参数怎么比），它们就是两个维度；只有当约束之间互相耦合（例如"乱序时参数必须全等"）才值得做成新的枚举值。

**但正交的前提是每个维度的方向必须有统一约定，并写下测试。** 多维度配置的失败模式不是"某一维写错"，而是"两维的方向约定不一致，错误在组合里互相掩盖"。`subset` / `superset` 在两个维度上方向一致，这不是巧合而是必须显式维持的约定——一旦有维度反着来，使用者凭直觉写对的概率就是 50%。

**不确定性要在比较之前消解，而不是在比较之后宽容。** 把阈值放宽、把模式改成 `ignore`，都是"比较之后宽容"——它同时抹掉了噪声和信号。`arg_normalizers` 是"比较之前消解"：先说清楚"什么差异不算差异"，再对剩下的做严格判定。可迁移到任何"期望值 vs 实际值"的系统：快照测试、配置漂移检测、API 契约测试、golden file 测试，它们都需要一个显式的归一化层，否则维护者只能靠不断放宽断言来换取安静。

**同一语义只能有一处实现。** 归一化逻辑被 golden 生成和匹配共同引用，因为"生成时归一化了、匹配时忘了"这类不一致不会报错——它只会让所有 golden 静默失效，而且失效方式看起来像"模型变笨了"。可迁移：凡是同时出现在**生产代码**和**测试/工具代码**里的业务规则，都要抽成一份共享实现。

**配置错误的暴露时机就是成本。** 非法的 `mode` 在构造期就抛 `ValueError`，而不是等 `evaluate()` 时静默返回一个 `FAIL`。一个写错的模式名如果表现为"这条用例没通过"，排查成本是几十分钟；如果表现为异常堆栈，成本是五秒。
