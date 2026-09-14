# 任务 36：adapter、用例集、架构测试与 README

> **所属里程碑**：M11 · **前置任务**：1–35（收尾任务，依赖前面全部产出） · **代码位置**：`src/harness/adapters/`、`tests/test_architecture.py`、`tests/suites/`、`suites/codefix/`、`examples/toyrepo/`

## 1. 总体目标

收尾任务要交付四件性质不同的东西，共同点是**它们都是「论证」而不是「功能」**：

1. **`TrajectorySource` adapter** —— 通用性的落点。说「这个 harness 不只评测自研 agent」必须有第二个可运行的数据源，否则只是宣称。
2. **架构约束的可执行化** —— 设计文档 §2.2 说「L0 叶子层不 import 上层」、§2.3 说「评测器不依赖 core」，这些约束如果只写在文档里，半年后必然腐烂：新人不知道，老人会忘。要变成测试。
3. **用例集与可解性自检** —— 用例集是判据本身，而判据自己先要被验证。把 `fix.patch` 打上去、隐藏测试必须通过，否则会出现「任务无解但被记成模型失败」的脏数据。设计文档点名这是**评测数据集最隐蔽的污染源**：它不报错，只是悄悄把所有 run 都记成失败。
4. **README + CI** —— 把答辩材料固化进仓库（行业空白论证、架构图、双 Harness 对称的解释与验证方法、失败模式分类法、指标语义）。

## 2. 实现流程

1. adapter：**先定协议**（`TrajectorySource`，`runtime_checkable` 的 `Protocol`），再实现 `OtelJsonlSource`。
2. 架构测试：先写 ast 导入提取器（`_imports`），再写三条断言。
3. 用例集可解性自检（`test_cases_are_solvable.py`）。
4. 构造 `examples/toyrepo/` 与 17 条用例。
5. golden 生成与自检。
6. README。
7. CI 配置。
8. **全量验证**。

顺序里最值得说的一条：**架构测试排在用例集之前**。用例集要新增 `suites/`、`examples/` 大量文件，先立起约束，后面加的东西自动受约束；反过来做，等发现违规时已经积了一堆改动，没人愿意回头修。

另一个理由来自 adapter 的位置：adapter 的产出必须是 `Trajectory`（L0 的类型），所以它只能依赖 `events/`——协议在前、实现在后，是为了让「adapter 属于哪一层」在写第一行代码前就确定。

## 3. 具体技术实现

### 3.1 adapter：只读轨迹源

```python
@runtime_checkable
class TrajectorySource(Protocol):
    name: str
    def can_load(self, ref: str) -> bool: ...
    async def load(self, ref: str) -> Trajectory: ...
```

用 `Protocol` 而不是 ABC：**结构化子类型**——第三方不需要 import 你的基类（甚至不需要装你的包）就能实现它。`runtime_checkable` 让 `isinstance(x, TrajectorySource)` 可用于运行时选择，但要记住它的已知限制：**只检查方法名存在，不检查签名**，别把它当类型安全保障。

`can_load` 的判定必须便宜且保守（后缀 + 存在性）：多个 source 并存时是「谁先说能加载就用谁」，一个过于宽松的 `can_load` 会把别的 source 的活抢走。

OTel GenAI 风格的 span 到内部事件的映射（设计文档 §3.8）：

| OTel span | 产出的内部事件 |
|---|---|
| `gen_ai.operation.name=invoke_agent` | `RUN_START`（`role="external"`，model 取 `gen_ai.request.model`，provider 取 `gen_ai.provider.name`） |
| `gen_ai.operation.name=execute_tool` | `TOOL_CALL` + `TOOL_RESULT`（`gen_ai.tool.name` / `gen_ai.tool.call.id`） |
| 文件末尾 | `RUN_END(status="imported")` |

三个坑：

- **`gen_ai.system` 已废弃**，新名是 `gen_ai.provider.name`（tech-stack §8）。adapter 只读新名，于是老 trace 的 provider 会落到 `"unknown"`——导入的数据要能看出"这是老格式"，而不是静默降级。
- **一个 span 展开成两个事件时，`seq` 必须手动占用两次**（`TOOL_CALL` 之后 `seq += 1` 再发 `TOOL_RESULT`，循环末尾再 `+= 1`）。事件流的 `(run_id, seq)` 是主键、`seq` 要求全序（任务 2、28），外部数据转内部事件流最容易出的错就是 seq 重复或跳号。
- **`ok` 的判定是刻意宽容的**：`ok = span["status"]["code"] != "ERROR"`。OTel 的 status code 是 `UNSET` / `OK` / `ERROR`，只把 `ERROR` 当失败意味着 `UNSET` 算成功。对导入的数据这是保守的默认（未设置不该被当成失败），代价是可能漏掉真实的失败。

### 3.2 架构测试：与 import-linter 刻意冗余

| | `import-linter` | `tests/test_architecture.py` |
|---|---|---|
| 形式 | 声明式配置（`pyproject.toml`） | 纯 ast 自研断言，零依赖 |
| 跑在哪 | CI（`lint-imports`） | 单测（`pytest`） |
| 失败时给出 | 契约名 + 违规链 | **精确的文件路径 + import 语句 + 修复提示** |

冗余是刻意的：配置即文档（`source_modules` / `forbidden_modules` 一眼看懂架构意图），而 ast 测试能在本地快速反馈并给出可操作的错误信息——断言消息里直接写着 `Use JudgeClient from harness.contracts.protocols instead.`。**把修复方案写进失败信息**，违规者看到的是出路而不是规则编号。

三条断言：evaluators 不依赖 core/orchestration/store/providers；`events` / `contracts` 是叶子层；每层 import 白名单。

> **一处需要对齐的规则**：测试放过了 `events` import `harness.contracts`（`test_events_and_contracts_are_leaf_layers` 里的 `continue` 分支，`ALLOWED_IMPORTS["events"]` 也含 `harness.contracts`），但设计文档 §2.4 的表写的是「`events`：仅标准库 + pydantic」、「`contracts`：`events`」，import-linter 的 `layers` 契约同样把 `harness.contracts` 排在 `harness.events` 之上（contracts 可依赖 events，反之不可）。两者判定不一致，宽松的那条（ast 测试）会放过 `events ↔ contracts` 的环。

### 3.3 用例集可解性自检

```python
subprocess.run(["git", "apply", str(d / "fix.patch")], cwd=repo, check=True)
proc = subprocess.run([sys.executable, "-m", "pytest", str(d / "tests" / "test_hidden.py"), "-q"], ...)
assert proc.returncode == 0, f"{d.name} is UNSOLVABLE:\n{proc.stdout}\n{proc.stderr}"
```

失败信息里带上 stdout/stderr——「用例无解」这个结论必须自带证据，否则排查者还要自己复现一遍。

用例被 `rglob("case.yaml")` 发现后参数化进测试矩阵：

```python
CASES = sorted(SUITES.rglob("case.yaml"))
@pytest.mark.parametrize("case_yaml", CASES, ids=_case_ids())
```

**数据即测试参数**：新增一条 case 自动进入测试矩阵，不需要改代码。这是本项目里第三次出现同一个模式（任务 30 的 `_RULES` 注册表、任务 27 的评测器白名单、这里的用例发现）——「注册表 + 遍历」是可扩展性在 Python 里的通用形态。

> 可移植性提示：该测试依赖 `cp -r` / `git init` / `git apply` 这些 POSIX 命令，在 Windows 上要靠 Git Bash 提供（与其余纯 Python 的测试不同）。CI 跑 ubuntu 无碍，本地跨平台时要知道这个前提。

用例集分布（共 17 条）：easy 5（Track A 5）、medium 7（Track A 5 + Track B 2）、hard 5（Track A 3 + Track B 2）。其中 4 条**过程陷阱用例**是过程级评测的招牌，每条对应一个评测器：

| case | 考察 |
|---|---|
| `trap_loop_retry` | `FailureClassifier` 的循环规则 |
| `trap_context_pressure` | `CONTEXT_COMPACT` 作为一等事件的价值 |
| `trap_fabricate` | `GroundingChecker`（输出被截断却声称全部通过） |
| `trap_injection` | `MetaEvaluator` 的抗注入指标 |

### 3.4 CI 流水线

`uv sync --all-groups` → `ruff check` → `pyright` → `lint-imports` → `pytest`（**不含 `-m live`**）→ 单文件 HTML 报告作为 artifact 上传。

顺序理由：便宜的检查在前（lint 秒级失败，不必等测试跑完）；`live` 用例需要真实 API key 与网络，靠 marker 默认跳过（设计文档 §7）；报告作为 artifact 是「评测产物进入工程流程」的最后一环。

## 4. 使用的技术栈简介

### OTel GenAI 语义约定（全部仍是 Development）

这是本章最重要的选型事实：**截至 2026-07，没有任何一个 `gen_ai.*` 属性达到 Stable**，全部是 Development。规范原文写着 *"SHOULD NOT be used in production"*、*"MAY be removed without prior notice"*；只有继承自核心约定的 `error.type` / `server.address` / `server.port` 是 Stable。GenAI 部分没有独立 PyPI 包，规范仓库已迁到 `open-telemetry/semantic-conventions-genai`（无 tag release，从 main 发布）；Python 侧官方工具包 `opentelemetry-util-genai` 1.1b0 是 **beta**。

因此本项目的做法是：**内部字段名才是稳定契约，OTel 命名只活在 `harness/events/otel.py` 这一个投影模块里**，属性改名只改这一个文件。OTel SDK 不进核心依赖，放 `[otel]` extra。adapter 因此是「只读属性名」的薄层，而不是一组类型绑定。

配套的两个已知地雷（tech-stack §8）：`opentelemetry-instrumentation-httpx` 0.65b0 内部已含 httpx2 支持（`HTTPX2ClientInstrumentor`），**httpx2 流量的 span 不会出现在 `HTTPXClientInstrumentor` 下，会静默丢失**；PyPI 上的 `opentelemetry-instrumentation-httpx2` 是 0.0.0 空壳占位包，别装。

### import-linter

用 Grimp（Rust）建静态导入图，支持 `forbidden` / `layers` / `independence` / `protected` / `acyclic siblings` / `custom` 契约，配 `pyproject.toml`，跑 `lint-imports`。**「评测器不依赖 core」用一条 `forbidden` 契约就够**——这正是它赢的地方。`tach` 0.35.0 曾一度无人维护，2026-02 由 Gauge 复兴，可视化更好，但更适合需要持续可视化依赖图的大仓。

### 其余质量工具链

`ruff` 0.16.x（事实标准：inspect_ai / lm-eval / ragas / mlflow / langfuse 全用）；`pyright` 1.1.414 作 CI gate（conformance 96.8%；本项目 pydantic 密集，`ty` 无 plugin 系统会误报，mypy 2.3.1 conformance 约 77%）；`uv` 0.12.13 + PEP 735 `[dependency-groups]`（`pip-tools` 已于 2026-03 宣布 sunset）。

## 5. 工程化思想

**架构约束写成可执行的测试，而不是文档。** 文档会腐烂，测试不会。更进一步：**把修复方案写进断言消息**（「用 `JudgeClient` 而不是 import `core`」），违规者看到的是出路。可迁移到任何有分层/边界要求的项目：约束的可执行性决定了它能活多久。

**刻意的冗余防线在特定条件下是理性的，但前提是规则一致。** 两条防线服务于不同场景（CI vs 本地单测）、失败信息可操作性不同，所以保留合理。但规则不一致的冗余不是冗余，是漏洞——宽松的那条会成为绕行路径。**用一条「应该失败」的样例定期验证每道防线都会响**，是这类配置必须付的维护成本。

**通用性论证要有可运行的第二个实例。** 声称「不限自研 agent」就必须有一个能跑通的外部数据源。判断一个「可扩展」设计是真扩展还是宣称，看的不是接口定义得多漂亮，而是**有没有一个非本体的实例在跑**。

**判据自己必须先被验证。** 测试要自检（隐藏测试可解性）、golden 要自检（自反性 + 工具白名单）、报告要自检（无外部 URL）。「校验器本身不被校验」是测试体系最常见的盲区：判据一旦错了，它会安静地把所有结果染成同一个颜色，而且看起来非常正常。

**数据即测试参数，验收标准是端到端组合。** 用文件发现（`rglob`）+ 参数化让数据集扩充不需要改代码——「注册表 + 遍历」是 Python 里实现可扩展性最省力的形态：**新增一个声明式制品，而不是新增一段逻辑**。而收尾的验收标准是 `uv run pytest` + `lint-imports` + `pyright` + 真跑 17 条用例 + 生成 HTML + diff + ci：每一条命令都在验证前 35 个任务的合成效果，**能跑通全部验收命令才叫完成**，「我改的这几个文件测试过了」不是完成。
