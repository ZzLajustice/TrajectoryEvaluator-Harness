# 任务 34：`diff` 与 `ci` 命令

> **所属里程碑**：M8 · **前置任务**：11（CLI 垂直切片）、32（聚合结果） · **代码位置**：`src/harness/orchestration/diff.py`、`src/harness/cli.py`

## 1. 总体目标

把 harness 从「一个能跑评测的工具」变成「一个会拦住人的门禁」。两个痛点：

**没有基线，数字没有含义。** 单看「`pass_rate` 0.8」无法判断这是进步还是退步——可能是相对上一版涨了，也可能是从 0.95 掉下来的。`diff` 回答「哪些 case 变了、往哪个方向变」。

**没有门禁，评测只是报告。** CI 里跑完评测却不拦截，等于把判断成本推给每个人。`ci` 命令把结论变成**退出码**，让流水线自己决定放行还是拦住。

这一章也是「评测结论进入工程流程」的落点：在此之前所有的评测能力都还只是「你主动去看」。

## 2. 实现流程

1. 先写 `diff_runs`（纯函数，吃两个 JSON 文件，输出分类结果），离线可测。
2. 再写 CLI 的 `diff` 与 `ci` 命令（编排：跑 suite → 聚合 → 比阈值 → 比基线 → 退出码）。
3. 最后**用 shell 验证真实退出码**。

顺序的关键在于：`ci` 的回归判定依赖 `diff`，所以 `diff` 必须先能被独立、离线地测试——它没有网络、没有 provider、没有 store，是整条链上唯一纯数据的一环。

`diff_runs` 内部顺序也有讲究：

1. 加载两个文件，缺文件立即 `FileNotFoundError`（错误尽早、信息具体）；
2. 取交集 `base.keys() & cand.keys()` 并 **`sorted`**，保证输出可复现、可直接 diff；
3. 逐 case **先判 fingerprint，再判状态**；
4. 最后算 `added` / `removed`（只在一边出现的 case）。

**fingerprint 判断必须先于状态比较，且必须 `continue`。** 这是本任务最重要的一条：模型、prompt、工具集变了，两次 run 就不是同一个实验，比出来的「回归」是假的。宁可报告「不可比」，也不能给出一个错误的结论。

## 3. 具体技术实现

### 3.1 五类结果

| 结果 | 条件 |
|---|---|
| `regressions` | baseline `ok` → candidate 非 `ok` |
| `fixes` | baseline 非 `ok` → candidate `ok` |
| `flaky` | baseline `ok` → candidate `flaky` |
| `incomparable` | 两侧 `spec_fingerprint` 不同 |
| `added` / `removed` | 只在一侧出现的 case |

`spec_fingerprint` 来自 `RunSpec.fingerprint()`（设计文档 §3.3：影响行为的字段的 sha256），它存在的目的就是这个可比性判断。

注意 `incomparable` 是**第三种状态**，不是 pass 也不是 fail。「不确定」必须有自己的位置——塌缩到任何一边都会产生错误信息。

### 3.2 一个分支顺序的陷阱

```python
b_ok, c_ok = b["status"] == _PASS, c["status"] == _PASS
if b_ok and not c_ok:
    regressions.append(case_id)
elif not b_ok and c_ok:
    fixes.append(case_id)
elif b_ok and c.get("status") == "flaky":     # ★ 不可达
    flaky.append(case_id)
```

第三条分支永远进不去：baseline 通过、candidate `flaky` 时，`c_ok` 为 `False`，第一条 `b_ok and not c_ok` 已经命中，case 被算成 regression。于是专门的测试 `test_flaky_is_detected_when_baseline_passed_and_candidate_flaked` 会失败。

正解是把 flaky 判定**前置为独立分支**再落一般规则：

```python
if b_ok and c.get("status") == "flaky":
    flaky.append(case_id)
elif b_ok and not c_ok:
    regressions.append(case_id)
...
```

不只是排序问题，而是**语义问题**：flaky 是 regression 的子类（「不再稳定通过」同时是「不再通过」），必须先识别特例再落通例。这类错误的可怕之处在于**它不报错**——只是静默给出错误的分类结论。唯一能抓住它的方式是「每个分类各有一条用例」。

### 3.3 CI 退出码是接口，不是实现细节

```python
"""CI 门禁。退出码：0 通过 / 1 未达标 / 2 配置错误 / 3 预算超限 / 4 基线缺失。"""
```

| 码 | 含义 |
|---|---|
| 0 | 通过 |
| 1 | 未达标（`pass_rate` 低于 `--fail-under`，或回归数超过 `--max-regressions`） |
| 2 | 配置错误（suite 不存在、字段非法） |
| 3 | 预算超限（`--max-cost` 是硬门禁，设计文档 R4） |
| 4 | 基线缺失 |

为什么不统一用 1 表示所有失败：CI 脚本需要区分「模型退步了」（要人看）与「配置写错了 / 预算爆了」（要改配置）。混在一起，流水线就只能去解析日志文本——**退出码是给机器读的接口，必须有语义分辨率**。

判定顺序：配置错误(2)/预算(3) → `pass_rate` 门禁(1) → 基线缺失(4) → 回归数(1) → PASS(0)。

两个默认值值得留意：`--max-regressions` 默认 0（默认严格，任何回归都拦），`--fail-under` 必须显式给值。**门禁的默认值应该是不放行，放宽必须由人显式写出来。**

> 实现提示：计划片段里 `ConfigError` / `BudgetExceededError` 带 `# noqa: F821`，说明是从配置层与预算层导入的示意代码，落地时要接上真实类型。

### 3.4 退出码只能用真实进程验证

```bash
uv run harness ci -s suites/codefix/suite.yaml --fail-under 0.99
echo "exit=$?"    # 期望 1
```

单测里断言 `typer.Exit` 的 code 不等于 CI 里真的返回了那个码：中间隔着 typer、click、shell。**验证要在正确的层级做**——分类逻辑用单元测试，退出码用 shell 验证，门禁是否真能拦住人用端到端跑一次验证。

## 4. 使用的技术栈简介

| 工具 | 版本 | 事实 |
|---|---|---|
| `typer` | 0.27.2 | ragas / deepeval 都选它；类型注解即 CLI 定义，内置 rich 集成 |
| `click` | 8.5.0 | typer 的底层；**必须排除已知缺陷版本** |
| `json` / `pathlib` | stdlib | 基线文件是普通 JSON，不引入额外格式 |

**click 的版本约束照抄 inspect_ai**：

```
click>=8.1.3,!=8.2.0,!=8.2.2,!=8.3.0,!=8.3.1
```

注释说明 8.2.2 / 8.3.0 / 8.3.1 **破坏了 optional flag values**（pallets/click#3084，8.3.2 才修）。typer 依赖 click，我们会传递性踩到——这就是为什么约束要写进 pyproject 而不是靠运气。

**为什么不用 argparse**：子命令树场景样板代码爆炸。**为什么不用裸 click**：只有需要极细粒度控制（动态子命令、参数回调顺序）才直接写。

## 5. 工程化思想

**「不可比」必须是显式状态。** 任何对比系统（性能基准、A/B 实验、模型评测）都要有一条「这两组数据不可比」的路径。没有它，差异会被当成信号，而基于错误前提的结论比没有结论更糟。`fingerprint` 就是这个判断的可执行形式——**把「能不能比」变成代码能算的哈希，而不是人记得住的规矩**。

**特例先于通例。** 分支判断里先识别子类（flaky ⊂ regression），再落一般规则。顺序错了不会抛异常，只会静默给出错误结论——这类 bug 只能靠「每个分类一条用例」的测试粒度暴露。凡是写出分类逻辑的地方，都值得问一句：每个类别都有对应的测试样本吗？

**工具的退出码是 API。** 定义语义、写进文档字符串、单独验证。任何会被 CI 调用的命令，退出码就是它最重要的输出——比 stdout 的措辞重要得多。

**门禁默认严格，放宽需显式。** 与「安全默认 + 显式豁免」（任务 33）同一条原则：默认值决定了绝大多数人的实际行为，因此默认值应该朝保守方向设。

**验证要在正确的层级做。** 单元测试证明分类逻辑，shell 证明退出码，端到端证明门禁真的会拦住人。用「单元测试全绿」宣称「CI 能拦住回归」是层级错配——中间每多一个抽象层，就多一份未被覆盖的可能。
