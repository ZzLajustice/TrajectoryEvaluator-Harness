# 任务 11：CLI `run` 子命令与端到端验证

> **所属里程碑**：M1 · **前置任务**：任务 10（及其全部前置 2–9） · **代码位置**：`src/harness/cli.py`、`examples/hello.yaml`、`tests/e2e/test_hello.py`

## 1. 总体目标

**这是 Part 1 的验收点**：`harness run --suite examples/hello.yaml --script '[{"tool":"finish","summary":"hello"}]'` 跑通一条完整任务并落下轨迹，`harness trace --run-id <id>` 把事件流打印出来。

**为什么一个 CLI 命令值得单列一个任务。** 在垂直切片完成之前，「地基已经打完」这句话是无法证伪的——2 到 10 号任务各自的单测全绿，只说明零件合格，不说明它们能接上。`tests/e2e/test_hello.py` 是**唯一能证明这条路径真的通了**的证据，而且它以「一条命令 + 一个退出码」的形式存在，任何人 10 秒内能复现。

**它也是一条刻意设计成离线、确定、零成本的路径**：provider 用 `FakeProvider`（任务 6），workspace 用 `tempdir`（任务 15），grader 列表为空。整条链路不产生任何 LLM 网络调用（Part 1 验收标准第一条），因此它可以进 CI、可以被反复重跑、不会因为模型升级而变红。M1 之后的每个任务都拿它当回归基线。

**退出码约定**：`0` 通过 / `1` 门禁未达标 / `2` 配置错误 / `3` 预算超限 / `4` 基线缺失。Part 1 只用到前几个，后两个是给 Part 3 的 CI 门禁（`harness ci`）预留的接口——**退出码是 CLI 唯一的机器可读输出**，它必须在第一天就定义好，否则 CI 只能靠 grep 终端文本。

## 2. 实现流程

1. **先写 e2e 测试**。第一段：`CliRunner` 调 `run --suite examples/hello.yaml --script '[{"tool":"finish","summary":"hello"}]' --out tmp_path`，断言 `exit_code == 0`、`*.jsonl` 恰好一个、首事件 `run.start`、末事件 `run.end`、中间含 `tool.call`。第二段：先 `run` 再 `trace`，断言 `trace` 输出里含 `tool.call`。
   为什么先写：e2e 测试是「完成」的定义。先写才能避免用实现细节反推验收标准（反过来做，测试很容易写成「断言我实现的东西」）。
2. **写 `examples/hello.yaml`**：一条 case，`sut.script` 是一步 `finish` 调用，`workspace.kind=tempdir`，`graders: []`。它是「最小可跑用例」，同时也是 suite YAML 格式的第一个真实样例。
3. **写 `cli.py` 的 `run` / `trace` 两条命令**——保持薄壳，真正的组装交给 `RunBuilder`。
4. **跑测试 + 手工验证**：`uv run harness run ...` 看终端摘要，`uv run harness trace --run-id <上一步的 id>` 看完整事件流（`run.start / turn.start / llm.request / llm.response / tool.call / tool.result / run.end`）。

顺序理由：**CLI 是装配点，必须最后写。** 它依赖所有下层接口定型；提前动手会写成一个「什么都自己 `new` 一遍」的上帝函数，然后下层每次改接口都要动 CLI。

## 3. 具体技术实现

### typer：类型注解即 CLI 定义

```python
app = typer.Typer(no_args_is_help=True, add_completion=False)

@app.command()
def run(
    suite: Path = typer.Option(..., "--suite", "-s"),
    provider: str = typer.Option("fake", "--provider"),
    out: Path = typer.Option(Path("runs"), "--out"),
    evaluate: bool = typer.Option(True, "--evaluate/--no-evaluate"),
) -> None: ...
```

`...`（Ellipsis）表示必填。`Path` 注解让 typer 自动做路径转换。`--evaluate/--no-evaluate` 是 typer 的布尔双写形式，比 `--no-evaluate` 单开一个开关更不容易出现「两个 flag 同时给」的冲突状态。

**默认 `provider="fake"` 是一个有意的安全默认值**：设计文档 §6 R4 的应对是「开发期成本失控 → 默认 `FakeProvider`；真模型必须显式 `--model`」。默认值决定了误操作的代价——默认离线，误操作最多是没结果；默认联网，误操作可能是一笔账单。

### `CliRunner` 在进程内跑，所以入口必须是同步的

`typer.testing.CliRunner` 直接调用命令函数、捕获输出与退出码，不起子进程。这决定了 CLI 的形状：**同步入口函数 + 内部 `asyncio.run`**。如果命令函数本身是 `async def`，`CliRunner` 拿不到结果。

### 函数体内 import 装配层

`run` 命令体里才 `from harness.orchestration.deps import RunBuilder`。收益是 `--help`、参数解析、shell 补全这些只读路径不必加载整条依赖链；代价是首次执行命令时多一次导入延迟。对一个要打进 `[project.scripts]` 被频繁调用的 CLI，这个取舍是划算的。

### `trace` 命令：证明轨迹可读不依赖任何 UI

```python
for ev in read_events(out / f"{run_id}.jsonl"):
    typer.echo(f"{ev['seq']:>4}  {ev['type']:<18} {_brief(ev)}")
```

`_brief` 按事件类型裁摘要（`tool.call` 显示 `name(args)`、`tool.result` 显示 `ok` 与内容片段、`llm.response` 显示文本前 40 字或 `<tool_calls>`）。这个命令的价值不在于好看，而在于**用 20 行代码验证「JSONL 真相源 + 事件流」这个设计是可用的**。Part 3 的 HTML 报告是在这条路径上加渲染层，而不是另开一条数据通路。

### 组装缺口要先认领

`run` 命令体里调用的 `RunBuilder`（`builder.run_suite_sync(suite)`）**在 Part 1 里没有对应的实现任务**：`orchestration/deps.py` 是在 Part 3 任务 27 才被创建的，而 `RunBuilder` 这个类名在三份计划文档里只出现在这个代码片段中。`RunContext.emit` 的订阅端同理——示例里是 `...`，而事件必须最终进 `JsonlStore`。

（相比之下，loop 里用到的 `ContextManager` 已经在任务 10 里被明确划了范围：本任务只实现 `build_request` / `append_assistant` / `append_tool_result` 三个纯消息累积方法，`needs_compaction()` 固定返回 `False`、`compact()` 返回 `None`，压缩与 `CONTEXT_COMPACT` 事件留给任务 22 在同一文件上扩展。**这就是「缺口要显式记账」的正确示范**：先给最小实现 + 冻结接口，让垂直切片不被后续功能阻塞。）

所以任务 11 的实现范围实际上要包含**一个最小组装层**：读 `suite.yaml`（`yaml.safe_load`）→ 为每个 case 构造 `RunSpec` + `RunDeps`（`provider=fake` 时按 `sut.script` 构造 `FakeProvider`、`store=JsonlStore(out)`、`tools=ToolRegistry(finish)`、`middlewares=[]`）→ 顺序执行 → 返回 `RunResult` 列表，外加一条 emit→store 的通道。**动手前先确认这些归属，而不是临时发明接口**——临时接口会变成事实标准，然后在 Part 3 与真正的编排层打架。

### suite YAML 必须只用 `yaml.safe_load`

`examples/hello.yaml` 是所有后续用例集的模板。tech-stack §3 把这条列成硬性要求（PyYAML 是事实标准，inspect_ai / lm-eval / mlflow 全用），Part 3 任务 27 还配了一条「投毒 YAML 必须报错」的测试：`name: !!python/object/apply:os.system [...]` 必须抛异常。**配置文件的解析器是一个可执行入口**，`yaml.load` 等价于 `eval`。

## 4. 使用的技术栈简介

| 组件 | 版本约束 | 说明 |
|---|---|---|
| `typer` | `>=0.27,<1` | 类型注解即 CLI 定义，内置 rich 集成。采用者：ragas、deepeval（两条评测赛道都选它）。替代品：`argparse`（子命令树场景样板代码爆炸）、`click`（typer 的底层，只在需要动态子命令、参数回调顺序这类细粒度控制时才直接写） |
| `click` | 传递依赖 | **必须排除已知缺陷版本**：inspect_ai 的约束带一串排除 `click>=8.1.3,!=8.2.0,!=8.2.2,!=8.3.0,!=8.3.1`——8.2.2 / 8.3.0 / 8.3.1 破坏了 optional flag values（pallets/click#3084，8.3.2 才修）。typer 依赖 click，我们会传递性踩到 |
| `rich` | `>=14.1,<16` | 避开 14.0.0（inspect_ai 特意排除），也避开 15（2026-04 的新大版本，生态未全跟上，deepeval 仍钉 `rich>=13.6,<15`）。Part 1 只用 `typer.echo`，表格与进度条是 Part 3 的事 |
| `pyyaml` | `>=6.0.3,<7` | 只用 `yaml.safe_load`。不用 `ruamel.yaml`（那是给「保留注释的 round-trip 编辑」用的，本项目不需要） |
| `pytest` | `>=8.4,<10` | `typer.testing.CliRunner` 提供进程内调用 |

不必引入的东西：**不做 TUI**（`textual` 跳过，设计文档 §1.3 明确列为非目标），**不做 Web 形态的报告**（静态 HTML 已满足）。

设计文档 §7 把 `tests/e2e/` 定义为「cassette replay，离线确定性，3-5 条冒烟」。Part 1 连 cassette 都不需要——`FakeProvider` 本身就是离线的。这也从另一个角度解释了为什么任务 6 被标为 M1 的关键：**没有它，M1 的端到端验证就得依赖真实 API，测试会变慢、变贵、不确定。**

## 5. 工程化思想

- **验收点必须是一条可执行的命令。** `harness run --suite examples/hello.yaml --script '[{"tool":"finish","summary":"hello"}]'` 同时是文档、验收标准和回归测试。把「做完了」定义成一条任何人能在 10 秒内复现的命令，比写一段描述强得多——描述会腐坏，命令不会。
- **先垂直切片，再横向扩充实。** M1 的里程碑说明写着「绝不把 loop / 事件 / store 放在后面」。地基类项目最大的风险是「每层都 90% 完成，但没有一条路径能跑」：接口错误被埋在各自的单测里，直到集成那天集中爆发。垂直切片用最小成本强制接口在真实路径上被走一遍。
- **配置错误要在加载期失败，不是运行期。** 未知评测器名、重复 `case_id`、缺必填字段（Part 3 任务 27 的三条硬约束）——CLI 是唯一能保证这条生效的入口。在加载期报错成本几乎为零，跑到一半才炸的成本是「已经烧掉的 token + 无法解释的报告」。
- **默认值就是安全策略。** 默认 `fake` provider、默认离线、默认 `keep_on_failure=True`（任务 15）——一组安全默认值让误操作变成无害操作。反过来，任何「默认走真实链路」的设计，都在把误操作的代价外包给用户的信用卡。
- **机器可读的输出要提前定，人类可读的输出可以后改。** 退出码 `0/1/2/3/4` 第一天就定死，因为 CI 门禁会依赖它；终端表格长什么样可以随时改。**决定接口稳定性的是「谁会消费它」，不是「它长什么样」。**
- **计划里的缺口要显式记账。** 当验收点依赖一个尚未定义归属的组件（`RunBuilder` / `ContextManager` / emit 订阅端）时，正确做法是把它写进文档并确认由谁补，而不是在实现时顺手发明一个接口——临时接口会变成事实标准，然后在后面的部分与真正的编排层冲突。
