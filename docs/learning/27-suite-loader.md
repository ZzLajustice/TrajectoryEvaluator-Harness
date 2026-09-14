# 任务 27：Suite loader

> **所属里程碑**：M6 · **前置任务**：4（`contracts/spec.py`：`Budget` / `ModelRef` / `TaskSpec` / `WorkspaceSpec` / `MiddlewareSpec`） · **代码位置**：`src/harness/orchestration/suite.py`、`src/harness/orchestration/deps.py`、`src/harness/suites/example/suite.yaml`

## 1. 总体目标

`suite.yaml` 是用户进入这个 harness 的入口：一份 YAML 声明 `defaults` 与一组 `cases`。加载器要把它变成**已校验的** `Suite` 对象，且在**加载期**就把错误全部暴露。

四类痛点，每一个都真实存在：

| 痛点 | 不解决会怎样 |
|---|---|
| 配置错误暴露太晚 | 未知评测器名若在跑到那条 case 时才报错，一次 17 条用例的 suite 会在几分钟后、花掉真金白银之后才告诉你"`NoSuchEvaluator`" |
| YAML 是不可信输入 | `yaml.load` 能构造任意 Python 对象（`!!python/object/apply:os.system`），加载别人的 suite 等于执行别人的代码 |
| `case_id` 重复 | 报告、baseline diff 都按 `case_id` 对齐（任务 34），重复会让两条用例静默互相覆盖 |
| 评测器名是能力引用 | 若允许写任意 import 路径，配置就升级成了代码，等于把 `suite.yaml` 变成执行入口 |

## 2. 实现流程

校验从外到内、从粗到细，每层失败的粒度不同：

1. 读文件 + `yaml.safe_load` —— 失败在 YAML 语法层。
2. 根节点必须是 mapping，否则直接 `ValueError`（不让 pydantic 去猜一个列表怎么变成 `Suite`）。
3. `Suite.model_validate(raw)` —— pydantic 做结构校验：字段类型、必填、`extra="forbid"`。
4. `CaseSpec` 的 `model_validator(mode="after")` 校验 grader 白名单。
5. `Suite` 的 `model_validator(mode="after")` 查 `case_id` 重复。

为什么是这个顺序：**越早的检查越便宜**。safe_load 失败是纯文本问题，`extra="forbid"` 失败是拼写问题，语义校验（白名单、重复 ID）才需要构造完整模型。同时，第 3 步交给 pydantic 的价值在于**错误会聚合**——一次 `ValidationError` 能报出十几条字段问题，而不是"改一条跑一次"。自己手写一串 `if ...: raise`，修 10 个错误就要跑 10 遍。

还有一条顺序上的约束：**白名单校验必须在 `model_validator(mode="after")` 里做**，而不是等 `run_suite` 组装评测器时再做。它是"这份配置能不能用"的问题，不是"这次运行顺不顺利"的问题，两者的成本差三个数量级。

## 3. 具体技术实现

### `safe_load` 的安全约束要用测试钉住

```python
raw = yaml.safe_load(path.read_text(encoding="utf-8"))   # ★ 只允许 safe_load
```

`yaml.safe_load` 只构造标准 YAML 标签（str / int / list / dict 等），不解析 `!!python/...` 这类厂商标签；`yaml.load`（以及宽松的 `FullLoader` 用法）会真的去实例化对象。

计划里的测试是这个约束的执行者：

```python
p.write_text("name: !!python/object/apply:os.system ['echo pwned']\n")
with pytest.raises(Exception):
    load_suite(p)
```

**注释说"必须 safe_load"没有约束力，一条能让违规立刻失败的测试有。** 这是"安全规约如何落地"的标准做法：找到能让错误用法当场爆炸的最小输入，把它变成一条测试。

### 白名单而不是"任意可达"

```python
_KNOWN_GRADERS = {
    "TrajectoryMatcher", "EfficiencyAnalyzer", "GroundingChecker",
    "FailureClassifier", "MetaEvaluator",
}
```

计划里的注释写明了动机：**防止配置注入任意类**。`graders[].name` 最终会被实例化，所以它本质上是一个"按名字取类"的通道；把它限制成 5 个已知名字，通道就从"任意"收缩成"受控"。将来要支持第三方评测器，正确做法是显式登记（entry point、注册表），而不是放开 `importlib` 任意路径——**白名单总会漏，但它漏的方式是可枚举的；任意可达漏的方式不可枚举。**

反面参照是黑名单：`DisallowedGrader` 列表永远追不上新出现的危险名字。

### `extra="forbid"` 是"拼写错误立刻可见"的主力

```python
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")
```

配置里写了 `optimal_step` 而不是 `optimal_steps`，默认行为是**静默忽略**——评测器照默认值跑，用户看着配置以为生效了，直到结果不对才回头查。`extra="forbid"` 把这个沉默的失败变成一条加载期错误。这一个配置项的价值，可能超过本任务其它所有校验的总和，因为**拼写错误是最高频的配置错误**。

`tier: Literal["easy", "medium", "hard"]` 同理：设计文档 §5.1 的三层分级是封闭集合，写错一个字母就该报错。

### `defaults` 与 `case` 的边界是显式设计

`SuiteDefaults` 收 `model` / `budget` / `middlewares` / `concurrency` / `system_prompt`；单条 case 想覆盖时走 `SUTOverride`（`system_prompt` / `tools` / `budget`）。两组字段**不是同一份清单**——"哪些能被单条 case 覆盖"是被明确设计过的，不是"父类字段随便继承"。

这个区分很值钱：defaults 里的字段改了会同时影响 17 条用例，必须让人一眼看出影响面；而 `SUTOverride` 只影响一条。把它们混成一个"继承体系"，会导致"改一个默认值，某条用例悄悄变了行为"——这类漂移在评测系统里表现为"分数莫名其妙变了"。

`Suite.middlewares_for(case)` 返回 `[MiddlewareSpec(name=n) for n in ...]`——只存**名字 + 配置**、不存实例。这是设计文档 §3.3 的硬约束：`RunSpec` 必须能完整 JSON 序列化进 `RunStartEvent.spec_json`，否则历史轨迹无法回答"当时生效的是什么配置"（同任务 19 的结论）。

### `GoldenSpec` 预留了多候选

```python
class GoldenSpec(_Model):
    alternatives: list[list[dict[str, Any]]] = Field(default_factory=list)
```

设计文档 §5.3 明确要求"必须支持 `alternatives: list[list[Step]]`，任一命中即算匹配"——代码修复任务的合法路径极多，单条 golden 会把好 run 判成 fail。把它留在 schema 里（而不是事后扩展），是因为**golden 的形态一旦定下来，历史 golden 文件就都要迁移**。

> 一处需要注意：计划的文件清单里还列了 `src/harness/orchestration/deps.py`，但正文没有给出对应的实现步骤。它属于组装层（`orchestration` 是唯一允许 import 全部的层），实现时应按"运行依赖的组装"来定位，不要把它塞进 `contracts`。

## 4. 使用的技术栈简介

| 组件 | 说明 |
|---|---|
| **PyYAML 6.0.3**（tech-stack §3） | YAML 解析的**事实标准**——inspect_ai / lm-evaluation-harness / mlflow 全用；约束 `pyyaml>=6.0.3,<7` 在核心依赖里。硬性要求：只用 `yaml.safe_load` |
| YAML 的替代品 | `ruamel.yaml` 是给"保留注释的 round-trip 编辑"用的，本项目只需要读，不需要；`msgspec` 更快但会引入第二套模型体系（tech-stack §3 明确排除）。**不要为了解析速度引入第二套模型** |
| `pydantic` 2.13（tech-stack §3） | `model_validator(mode="after")` 做跨字段语义校验，`ValidationError` 天然聚合多条错误；`ConfigDict(extra="forbid")` 拒绝未知字段 |
| `pydantic-settings` 2.15（tech-stack §3） | **另一层配置**：API key、并发度、`base_url` 覆盖等**运行环境**配置（deepeval 同款）。与 `suite.yaml` 的"评测内容"配置分层，不要混用——环境配置随机器变，suite 应该可提交进 git |
| `orchestration` 层的依赖特权 | 按设计文档 §2.4 的白名单，只有 `orchestration` 可以 import 全部包——它正是"把配置变成对象"的地方 |

## 5. 工程化思想

**配置错误的暴露时机就是成本，而且差距是数量级的。** 加载期失败：毫秒、零成本、无副作用、报错能指到行。运行期失败：分钟级、花掉真实成本、可能已经产生了半套脏结果。可迁移的措施：给部署加一步 `config validate --dry-run`，让 90% 的配置错误在 CI 里就死掉。**凡是"配置驱动"的系统，都值得把校验单独做成一个可以不执行副作用就跑的命令。**

**校验要能一次报出全部错误。** 修一条跑一次的往返成本是 O(错误数) 倍，而配置错误往往成批出现（新写一份 suite 时）。优先选能聚合错误的框架（pydantic 的 `ValidationError`），而不是手写一串抛异常。这是"框架选择"层面的收益，写代码时省不下来。

**白名单优于黑名单，优于"任意可达"。** 判断标准不是"能不能拦住已知的坏东西"，而是"漏的时候能漏出什么形状"。白名单漏掉的是"某个合法项没登记"（可枚举、可修复），任意可达漏掉的是"某条路径执行了任意代码"（不可枚举、不可审计）。

**"绝不能这么做"的规约，要找一条能让违规当场失败的测试。** 注释、文档、code review 都拦不住三个月后的自己。`!!python/object/apply` 那条测试的存在，让"误用 `yaml.load`"从一个可能被放过的 review 意见，变成一个必然红的 CI。可迁移：每个安全规约都配一条"探针测试"，探针本身就是规约的可执行形式。

**默认值集中管理，但覆盖面必须显式设计。** `defaults` 块消灭了 17 条用例的重复配置，代价是"改一个值影响面很大"。控制这个代价的办法不是不用 defaults，而是**明确划分"哪些字段可以被单条 case 覆盖"**（这里用独立的 `SUTOverride` 表达），让影响面在类型层面就是可见的。
